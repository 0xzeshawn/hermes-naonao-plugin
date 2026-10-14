"""NaoNao platform adapter for Hermes Gateway.

Uses NaoNao Public Bot API v1 only:

    Authorization: Bot <nn_bot_>
    POST /api/v1/bot/gateway-session
    WSS <api-base-host>/gateway/v1
    message.bot_mention
    command.invoked
    POST /api/v1/channels/:channelId/messages

Hermes Gateway runs the agent. This module does not call Hermes /v1/runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret as _extra_or_secret
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms._shared import seed_extra_from_env as _seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://naonaoai.com"
MAX_MESSAGE_LENGTH = 4000
BACKOFF_SECONDS = (1, 2, 5, 10, 30)
DEDUP_MAX = 1000
SESSION_SKEW_SECONDS = 15
_SECRET_RE = re.compile(r"nn_(?:bot|bgs|gwr)_[A-Za-z0-9_-]+")
_COMMAND_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
_NOT_ASSIGNED = (
    "NaoNao Bot is not currently assigned to an external connector. "
    "Check the Bot connection mode in NaoNao."
)
_TOKEN_DEAD = "NaoNao Bot Token is no longer valid. Update the Bot Token and reconnect."


def redact(value: Any) -> str:
    return _SECRET_RE.sub(lambda match: match.group(0).split("_", 2)[0] + "_" + match.group(0).split("_")[1] + "_[redacted]", str(value or ""))


def normalize_api_base(raw: str) -> str:
    value = (raw or "").strip() or DEFAULT_API_BASE
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("NAONAO_API_BASE must be an http(s) origin")
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def gateway_ws_url(api_base: str, gateway_path: str) -> str:
    path = str(gateway_path or "/gateway/v1")
    if "://" in path or path.startswith("//"):
        raise ValueError("gatewayPath must be a path")
    if not path.startswith("/"):
        path = "/" + path
    parsed = urlparse(api_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def chat_id_for(community_id: str, channel_id: str) -> str:
    return f"{community_id}:{channel_id}"


def split_chat_id(chat_id: str) -> tuple[str, str]:
    community, sep, channel = str(chat_id or "").partition(":")
    if not sep or not community or not channel or ":" in channel:
        raise ValueError("chat_id must be communityId:channelId")
    return community, channel


def strip_leading_mention(content: str, bot_name: str) -> str:
    text = str(content or "")
    name = (bot_name or "").strip()
    if not name:
        return text
    pattern = re.compile(rf"^@{re.escape(name)}(?:\s+|$)", re.IGNORECASE)
    return pattern.sub("", text, count=1).strip() or text


class NaoNaoAdapter(BasePlatformAdapter):
    """Text-only NaoNao Gateway adapter."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    splits_long_messages = True
    supports_code_blocks = False

    def __init__(self, config: PlatformConfig, *, http_factory=None, ws_connect=None):
        super().__init__(config=config, platform=Platform("naonao"))
        # One NaoNao channel stays one Hermes session. user_id is the auth
        # principal only; Hermes would otherwise append it to the session key.
        if isinstance(config.extra, dict):
            config.extra["group_sessions_per_user"] = False
        extra = config.extra or {}
        self._bot_token = str(_extra_or_secret(extra, "bot_token", "NAONAO_BOT_TOKEN") or "").strip()
        self._api_base = normalize_api_base(str(_extra_or_secret(extra, "api_base", "NAONAO_API_BASE", DEFAULT_API_BASE)))
        self._http_factory = http_factory
        self._ws_connect = ws_connect
        self._http = None
        self._ws = None
        self._loop_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX, ttl_seconds=24 * 60 * 60)
        self._session: Dict[str, Any] = {}
        self._resume: Dict[str, Any] = {}
        self._last_acked = 0
        self._resume_rejected = False
        self._last_auth_op = ""
        self._bot_id = ""
        self._bot_name = ""
        self._event_for_chat: Dict[str, str] = {}
        self._send_index: Dict[str, int] = {}
        self._send_key: Dict[str, Dict[str, Any]] = {}
        self._shutdown = False
        self._attempt = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._shutdown:
            return False
        try:
            if not ensure_naonao_requirements():
                raise NaoNaoDependencyError("NaoNao runtime dependencies are not importable")
        except NaoNaoDependencyError as exc:
            self._set_fatal_error("missing_runtime_deps", str(exc), retryable=False)
            logger.error("[naonao] %s", exc)
            return False
        if not self._bot_token:
            self._set_fatal_error("missing_bot_token", "NAONAO_BOT_TOKEN is required", retryable=False)
            logger.error("[naonao] NAONAO_BOT_TOKEN is required")
            return False
        try:
            self._http = self._open_http()
            await self._load_identity()
            await self._bootstrap()
        except _FatalAuth:
            await self._close_http()
            return False
        except Exception as exc:
            logger.warning("[naonao] connect failed: %s", redact(exc))
            await self._close_http()
            return False
        self._shutdown = False
        self._loop_task = asyncio.create_task(self._connection_loop())
        self._mark_connected()
        logger.info("[naonao] gateway session ready bot=%s", self._bot_name or "bot")
        return True

    async def disconnect(self) -> None:
        self._shutdown = True
        self._mark_disconnected()
        await self._stop_tasks()
        await self._close_http()
        logger.info("[naonao] disconnected")

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to
        try:
            _community, channel_id = split_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        text = str(content or "")
        if len(text) > MAX_MESSAGE_LENGTH:
            return SendResult(success=False, error="message_too_long", error_kind="too_long")
        if self.has_fatal_error:
            return SendResult(success=False, error="invalid_bot_token", error_kind="forbidden")
        event_id = str((metadata or {}).get("naonao_event_id") or self._event_for_chat.get(chat_id) or "")
        key = self._claim_idempotency_key(event_id)
        logger.info("[naonao] outbound send start chat_id=%s event_id=%s", chat_id, event_id or "-")
        result = await self._post_message(channel_id, text, key)
        self._settle_idempotency_key(event_id, result)
        if result.success:
            logger.info(
                "[naonao] outbound send success chat_id=%s message_id=%s",
                chat_id, result.message_id or "",
            )
        else:
            logger.info(
                "[naonao] outbound send failed chat_id=%s status=%s error=%s",
                chat_id, getattr(result, "_http_status", "") or "-", redact(result.error),
            )
        return result

    def _claim_idempotency_key(self, event_id: str) -> str:
        """Reuse the key when the previous POST for this event never got a terminal answer.

        Hermes retries by calling send() again. A new key after a lost response would
        create a second bot message.
        """
        if not event_id:
            return ""
        slot = self._send_key.get(event_id)
        if slot and slot.get("reuse"):
            return str(slot.get("key") or "")
        index = self._send_index.get(event_id, 0) + 1
        self._send_index[event_id] = index
        key = f"{event_id}:{index}"[:128]
        self._send_key[event_id] = {"key": key, "reuse": False}
        return key

    def _settle_idempotency_key(self, event_id: str, result: SendResult) -> None:
        slot = self._send_key.get(event_id)
        if not slot:
            return
        slot["reuse"] = bool(getattr(result, "retryable", False)) and not result.success

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        del chat_id, image_url, caption, reply_to, metadata
        return self._unsupported("image")

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        del chat_id, audio_path, caption, reply_to, metadata, kwargs
        return self._unsupported("voice")

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        del chat_id, file_path, caption, file_name, reply_to, metadata, kwargs
        return self._unsupported("file")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": str(chat_id or ""), "type": "channel"}

    def build_mention_event(self, payload: Dict[str, Any], sequence: Any) -> Optional[MessageEvent]:
        data = payload or {}
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        channel = data.get("channel") if isinstance(data.get("channel"), dict) else {}
        community = data.get("community") if isinstance(data.get("community"), dict) else {}
        actor = data.get("actor") if isinstance(data.get("actor"), dict) else {}
        bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
        event_id = str(data.get("id") or "")
        channel_id = str(channel.get("id") or "")
        community_id = str(community.get("id") or "")
        content = str(message.get("content") or "")
        if not event_id or not channel_id or not community_id:
            return None
        if self._bot_id and bot.get("id") and str(bot.get("id")) != self._bot_id:
            return None
        username = str(actor.get("username") or "").strip()
        actor_id = str(actor.get("id") or "").strip()
        user_id = actor_id if actor_id and actor_id != username else None
        logger.info("[naonao] authorization identity available=%s", "true" if user_id else "false")
        chat_id = chat_id_for(community_id, channel_id)
        created = str(message.get("createdAt") or data.get("createdAt") or "")
        try:
            timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            timestamp = datetime.now(tz=timezone.utc)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=channel_id,
            chat_type="channel",
            user_id=user_id,
            user_name=username or None,
            message_id=str(message.get("id") or "") or None,
            scope_id=community_id,
        )
        return MessageEvent(
            text=strip_leading_mention(content, self._bot_name),
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=username or None,
            source=source,
            message_id=str(message.get("id") or "") or None,
            timestamp=timestamp,
            metadata={
                "naonao_event_id": event_id,
                "naonao_sequence": sequence,
                "naonao_raw_content": content,
                "naonao_community_id": community_id,
                "naonao_installation_id": str((data.get("installation") or {}).get("id") or ""),
            },
        )

    def build_command_event(self, payload: Dict[str, Any], sequence: Any) -> Optional[MessageEvent]:
        """Turn one NaoNao command.invoked into the plaintext Hermes already parses."""
        data = payload or {}
        invocation = data.get("invocation") if isinstance(data.get("invocation"), dict) else {}
        command = invocation.get("command") if isinstance(invocation.get("command"), dict) else {}
        channel = data.get("channel") if isinstance(data.get("channel"), dict) else {}
        community = data.get("community") if isinstance(data.get("community"), dict) else {}
        actor = data.get("actor") if isinstance(data.get("actor"), dict) else {}
        bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
        event_id = str(data.get("id") or "")
        channel_id = str(channel.get("id") or "")
        community_id = str(community.get("id") or "")
        name = str(command.get("name") or "").strip().lower()
        if not event_id or not channel_id or not community_id or not _COMMAND_NAME.match(name):
            return None
        if self._bot_id and bot.get("id") and str(bot.get("id")) != self._bot_id:
            return None
        username = str(actor.get("username") or "").strip()
        actor_id = str(actor.get("id") or "").strip()
        user_id = actor_id if actor_id and actor_id != username else None
        logger.info("[naonao] authorization identity available=%s", "true" if user_id else "false")
        chat_id = chat_id_for(community_id, channel_id)
        invocation_id = str(invocation.get("id") or "")
        created = str(data.get("createdAt") or "")
        try:
            timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            timestamp = datetime.now(tz=timezone.utc)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=channel_id,
            chat_type="channel",
            user_id=user_id,
            user_name=username or None,
            message_id=invocation_id or None,
            scope_id=community_id,
        )
        return MessageEvent(
            text=f"/{name}",
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=username or None,
            source=source,
            message_id=invocation_id or None,
            timestamp=timestamp,
            metadata={
                "naonao_event_id": event_id,
                "naonao_sequence": sequence,
                "naonao_command_name": name,
                "naonao_community_id": community_id,
                "naonao_installation_id": str((data.get("installation") or {}).get("id") or ""),
            },
        )

    async def _connection_loop(self) -> None:
        while not self._shutdown and not self.has_fatal_error:
            try:
                if not self._session_fresh():
                    await self._bootstrap()
                await self._serve_socket()
                self._attempt = 0
            except _FatalAuth:
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._shutdown or self.has_fatal_error:
                    return
                logger.warning("[naonao] gateway session failed: %s", redact(exc))
            if self._shutdown or self.has_fatal_error:
                return
            delay = BACKOFF_SECONDS[min(self._attempt, len(BACKOFF_SECONDS) - 1)]
            self._attempt += 1
            logger.info("[naonao] reconnecting")
            await asyncio.sleep(delay + random.uniform(0, delay * 0.2))

    async def _serve_socket(self) -> None:
        url = gateway_ws_url(self._api_base, self._session.get("gatewayPath") or "/gateway/v1")
        logger.info("[naonao] gateway connecting")
        connect = self._ws_connect or _default_ws_connect
        async with connect(url) as sock:
            self._ws = sock
            try:
                while not self._shutdown:
                    raw = await sock.recv()
                    if raw is None:
                        return
                    await self._on_frame(sock, raw)
            finally:
                code = getattr(sock, "close_code", None)
                self._ws = None
                await self._cancel_heartbeat()
                if code:
                    await self._note_close(int(code))

    async def _on_frame(self, sock, raw: Any) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[naonao] gateway bad frame")
            return
        if not isinstance(frame, dict):
            logger.warning("[naonao] gateway bad frame")
            return
        op = frame.get("op")
        if op == "HELLO":
            await self._on_hello(sock, frame)
        elif op == "READY":
            data = frame.get("d") or {}
            self._resume = {
                "sessionId": data.get("sessionId") or "",
                "resumeToken": data.get("resumeToken") or "",
                "resumeExpiresAt": data.get("resumeExpiresAt") or "",
            }
            self._resume_rejected = False
            self._last_acked = int(data.get("startSequence") or 0)
            logger.info("[naonao] gateway ready")
        elif op == "RESUMED":
            data = frame.get("d") or {}
            self._last_acked = int(data.get("lastAckedSequence") or self._last_acked)
            self._resume_rejected = False
            logger.info("[naonao] gateway resumed")
        elif op == "HEARTBEAT_ACK":
            return
        elif op == "EVENT":
            await self._on_event(sock, frame)

    async def _on_hello(self, sock, frame: Dict[str, Any]) -> None:
        interval = int(((frame.get("d") or {}).get("heartbeatIntervalMs")) or 30000)
        await self._cancel_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat(sock, interval))
        if self._can_resume():
            self._last_auth_op = "RESUME"
            await self._send_op(sock, "RESUME", {
                "token": self._session.get("token"),
                "sessionId": self._resume.get("sessionId"),
                "resumeToken": self._resume.get("resumeToken"),
                "sequence": self._last_acked,
            })
            return
        self._last_auth_op = "IDENTIFY"
        await self._send_op(sock, "IDENTIFY", {"token": self._session.get("token")})

    async def _heartbeat(self, sock, interval_ms: int) -> None:
        try:
            while not self._shutdown:
                await asyncio.sleep(max(interval_ms, 1) / 1000)
                await self._send_op(sock, "HEARTBEAT", None)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("[naonao] heartbeat stopped: %s", redact(exc))

    async def _on_event(self, sock, frame: Dict[str, Any]) -> None:
        event_type = frame.get("t")
        sequence = frame.get("s")
        logger.info("[naonao] inbound event type=%s sequence=%s", event_type, sequence)
        if event_type not in ("message.bot_mention", "command.invoked"):
            logger.info("[naonao] ignoring event type=%s sequence=%s", event_type, sequence)
            await self._ack(sock, sequence)
            return
        data = frame.get("d") if isinstance(frame.get("d"), dict) else {}
        event_id = str(data.get("id") or "")
        if event_id and self._dedup.is_duplicate(event_id):
            logger.info("[naonao] duplicate event ignored event_id=%s sequence=%s", event_id, sequence)
            await self._ack(sock, sequence)
            return
        if event_type == "command.invoked":
            event = self.build_command_event(data, sequence)
        else:
            event = self.build_mention_event(data, sequence)
        if event is None:
            logger.warning(
                "[naonao] skipping malformed or foreign %s event_id=%s sequence=%s",
                "command" if event_type == "command.invoked" else "mention",
                event_id, sequence,
            )
            await self._ack(sock, sequence)
            return
        chat_id = event.source.chat_id
        self._event_for_chat[chat_id] = event_id
        logger.info(
            "[naonao] %s accepted event_id=%s chat_id=%s message_id=%s",
            "command" if event_type == "command.invoked" else "mention",
            event_id, chat_id, event.message_id or "",
        )
        logger.info("[naonao] submitting event to Hermes event_id=%s chat_id=%s", event_id, chat_id)
        await self.handle_message(event)
        accepted = bool(getattr(event, "_gateway_accepted", False))
        logger.info(
            "[naonao] Hermes admission result event_id=%s accepted=%s",
            event_id, str(accepted).lower(),
        )
        if accepted:
            logger.info("[naonao] acking event event_id=%s sequence=%s", event_id, sequence)
            await self._ack(sock, sequence)
            return
        logger.info("[naonao] event not accepted by Hermes event_id=%s sequence=%s", event_id, sequence)

    async def _ack(self, sock, sequence: Any) -> None:
        if sequence is None:
            return
        await self._send_op(sock, "ACK", {"sequence": sequence})
        try:
            self._last_acked = int(sequence)
        except (TypeError, ValueError):
            return

    async def _send_op(self, sock, op: str, data: Optional[Dict[str, Any]]) -> None:
        payload: Dict[str, Any] = {"op": op}
        if data is not None:
            payload["d"] = data
        await sock.send(json.dumps(payload))

    async def _load_identity(self) -> None:
        body = await self._api("GET", "/api/v1/bot/me")
        bot = body.get("bot") if isinstance(body, dict) else {}
        self._bot_id = str((bot or {}).get("id") or "")
        self._bot_name = str((bot or {}).get("name") or "")
        logger.info("[naonao] bot identity ok name=%s", self._bot_name or "bot")

    async def _bootstrap(self) -> None:
        body = None
        for _attempt in range(3):
            try:
                body = await self._api("POST", "/api/v1/bot/gateway-session")
                break
            except _RateLimited as exc:
                logger.info("[naonao] gateway bootstrap rate limited, retry after %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
        if body is None:
            raise _RateLimited(1)
        owner = body.get("autoReplyOwner")
        if owner != "simple_gateway":
            self._set_fatal_error("not_assigned", _NOT_ASSIGNED, retryable=False)
            logger.error("[naonao] %s", _NOT_ASSIGNED)
            raise _FatalAuth()
        token = str(body.get("token") or "")
        if not token.startswith("nn_bgs_"):
            raise RuntimeError("gateway session did not return a session token")
        self._session = {
            "token": token,
            "expiresAt": body.get("expiresAt") or "",
            "gatewayPath": body.get("gatewayPath") or "/gateway/v1",
        }

    def _session_fresh(self) -> bool:
        expires = self._session.get("expiresAt")
        if not self._session.get("token") or not expires:
            return False
        try:
            exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return exp > datetime.now(tz=timezone.utc).timestamp() + SESSION_SKEW_SECONDS

    def _can_resume(self) -> bool:
        if self._resume_rejected or not self._resume.get("sessionId") or not self._resume.get("resumeToken"):
            return False
        if not self._session_fresh():
            return False
        expires = self._resume.get("resumeExpiresAt")
        if not expires:
            return True
        try:
            exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return exp > datetime.now(tz=timezone.utc).timestamp()

    async def _api(self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = self._api_base + path
        request_headers = {"Authorization": f"Bot {self._bot_token}"}
        if headers:
            request_headers.update(headers)
        response = await self._http.request(method, url, headers=request_headers, json=json_body, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError("redirect refused")
        if response.status_code == 401:
            await self._fatal_auth()
            raise _FatalAuth()
        if response.status_code == 429:
            body = _json_or_empty(response)
            wait = float(body.get("retryAfterSec") or 1)
            raise _RateLimited(wait)
        if response.status_code >= 400:
            raise RuntimeError(f"naonao http {response.status_code}")
        return _json_or_empty(response)

    async def _post_message(self, channel_id: str, content: str, idempotency_key: str) -> SendResult:
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._http.request(
                "POST",
                f"{self._api_base}/api/v1/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {self._bot_token}", **headers},
                json={"content": content},
                follow_redirects=False,
            )
        except _FatalAuth:
            return SendResult(success=False, error="invalid_bot_token", error_kind="forbidden")
        except Exception as exc:
            result = SendResult(success=False, error=redact(exc), error_kind="transient", retryable=True)
            result._http_status = "-"
            return result
        body = _json_or_empty(response)
        if response.status_code in (200, 201):
            message = body.get("message") if isinstance(body, dict) else {}
            result = SendResult(success=True, message_id=str((message or {}).get("id") or "") or None, raw_response=body)
            result._http_status = response.status_code
            return result
        if response.status_code == 401:
            await self._fatal_auth()
            result = SendResult(success=False, error="invalid_bot_token", error_kind="forbidden")
            result._http_status = response.status_code
            return result
        if response.status_code == 403:
            result = SendResult(success=False, error=str(body.get("err") or "insufficient_permissions"), error_kind="forbidden")
        elif response.status_code == 409:
            result = SendResult(success=False, error="idempotency_conflict", error_kind="unknown")
        elif response.status_code == 429:
            result = SendResult(success=False, error="rate_limited", error_kind="rate_limited", retryable=True, retry_after=float(body.get("retryAfterSec") or 1))
        else:
            result = None
        if result is not None:
            result._http_status = response.status_code
            return result
        if response.status_code >= 500:
            result = SendResult(success=False, error=str(body.get("err") or "internal_error"), error_kind="transient", retryable=True)
        else:
            result = SendResult(success=False, error=str(body.get("err") or response.status_code), error_kind="unknown")
        result._http_status = response.status_code
        return result

    def _open_http(self):
        if self._http_factory:
            return self._http_factory()
        import httpx
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)

    async def _close_http(self) -> None:
        client = self._http
        self._http = None
        close = getattr(client, "aclose", None)
        if close:
            await close()

    async def _fatal_auth(self) -> None:
        self._set_fatal_error("invalid_bot_token", _TOKEN_DEAD, retryable=False)
        logger.error("[naonao] %s", _TOKEN_DEAD)
        await self._close_live_socket()

    async def _close_live_socket(self) -> None:
        sock = self._ws
        self._ws = None
        if sock is None:
            return
        close = getattr(sock, "close", None)
        if not close:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.info("[naonao] live socket close failed: %s", redact(exc))

    async def _stop_tasks(self) -> None:
        await self._cancel_heartbeat()
        task = self._loop_task
        self._loop_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        sock = self._ws
        self._ws = None
        if sock is not None:
            close = getattr(sock, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def _cancel_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _note_close(self, code: int) -> None:
        if code == 4007:
            self._set_fatal_error("credential_revoked", _TOKEN_DEAD, retryable=False)
            logger.error("[naonao] %s", _TOKEN_DEAD)
        elif code == 4005:
            self._session = {}
            if self._last_auth_op == "RESUME":
                self._resume = {}
            self._last_auth_op = ""
            logger.info("[naonao] gateway session expired, bootstrap required")
        elif code == 4004:
            self._resume = {}
            logger.info("[naonao] gateway resume invalid, identify required")
        elif code in (4006, 4008, 4010):
            logger.info("[naonao] gateway closed %s", code)

    @staticmethod
    def _unsupported(kind: str) -> SendResult:
        return SendResult(success=False, error=f"{kind} is not part of the NaoNao text adapter", error_kind="unknown")


class _FatalAuth(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__("rate_limited")
        self.retry_after = retry_after


def _json_or_empty(response) -> Dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


class _SocketContext:
    def __init__(self, connect):
        self._connect = connect
        self.sock = None

    async def __aenter__(self):
        self.sock = await self._connect
        return self.sock

    async def __aexit__(self, exc_type, exc, tb):
        close = getattr(self.sock, "close", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        return False


def _default_ws_connect(url: str):
    try:
        import websockets
    except ImportError:
        websockets = None
    if websockets is not None:
        return websockets.connect(url, max_size=1024 * 1024, open_timeout=30)

    async def _aiohttp_connect():
        import aiohttp
        session = aiohttp.ClientSession()
        sock = await session.ws_connect(url, max_msg_size=1024 * 1024, heartbeat=None)
        return _AiohttpSocket(session, sock)

    return _SocketContext(_aiohttp_connect())


class _AiohttpSocket:
    def __init__(self, session, sock):
        self._session = session
        self._sock = sock

    async def send(self, data: str) -> None:
        await self._sock.send_str(data)

    async def recv(self):
        message = await self._sock.receive()
        if message.type in (message.type.CLOSE, message.type.CLOSED, message.type.ERROR):
            return None
        return message.data

    async def close(self) -> None:
        await self._sock.close()
        await self._session.close()


class NaoNaoDependencyError(RuntimeError):
    """Runtime dependencies could not be made importable. Never includes credentials."""


def _importable(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def check_naonao_requirements() -> bool:
    """PASSIVE registry check_fn. Import probe only; never installs or mutates the runtime.

    websockets is required so the adapter does not fall through to aiohttp, which does not
    speak macOS SOCKS by itself. python_socks is required because websockets 15 selects the
    system SOCKS proxy and raises if that module is missing.
    """
    return _importable("websockets") and _importable("python_socks")


def _activate_synced_environment() -> None:
    """Same post-sync adoption as pm.extras.ensure_import."""
    import sys
    from pathlib import Path

    from pm.environments import selected_venv, site_packages
    from pm.environments_adopt import adopt_selected, restart_needed
    from pm.paths import repo_root, runtime_facts_path

    if not runtime_facts_path().is_file():
        return
    root = repo_root()
    adopt_selected(root)
    selected = site_packages(selected_venv(root)).resolve()
    loaded = {Path(entry).resolve() for entry in sys.path if entry}
    if selected not in loaded:
        reason = restart_needed(root) or "this process does not run from the install's dependency environment"
        raise NaoNaoDependencyError(
            "NaoNao dependencies were installed into the Hermes gateway runtime; "
            f"restart the gateway to load them ({reason})"
        )


def _sync_runtime_dependencies() -> None:
    """ACTIVE install via Hermes PM. Declared plugin requirements land in the selected environment."""
    from pm.client import sync_venv

    sync_venv()
    _activate_synced_environment()


def ensure_naonao_requirements() -> bool:
    """ACTIVE registry ensure_deps_fn. Called from create_adapter() when the passive probe fails."""
    if check_naonao_requirements():
        return True
    try:
        _sync_runtime_dependencies()
    except NaoNaoDependencyError:
        raise
    except Exception as exc:
        raise NaoNaoDependencyError(
            "Hermes could not install NaoNao runtime dependencies "
            f"(websockets>=13, python-socks>=2): {redact(exc)}"
        ) from exc
    if not check_naonao_requirements():
        raise NaoNaoDependencyError(
            "websockets and python-socks are still not importable after Hermes dependency sync. "
            "Confirm the NaoNao platform plugin is enabled, then restart the Hermes gateway."
        )
    return True


def check_requirements() -> bool:
    """Backward-compatible alias of the passive probe."""
    return check_naonao_requirements()


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(str(_extra_or_secret(extra, "bot_token", "NAONAO_BOT_TOKEN") or "").strip())


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    token = str(_get_scoped_secret("NAONAO_BOT_TOKEN", "") or "").strip()
    if not token:
        return None
    seed = _seed_extra_from_env((
        ("NAONAO_BOT_TOKEN", "bot_token", None),
        ("NAONAO_API_BASE", "api_base", lambda value: normalize_api_base(value)),
    ))
    return seed


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id=None, media_files=None, force_document: bool = False) -> dict:
    """Out-of-process text delivery for Hermes cron when the gateway is not co-resident."""
    del thread_id, media_files, force_document
    adapter = NaoNaoAdapter(pconfig)
    if not adapter._bot_token:
        return {"error": "NAONAO_BOT_TOKEN is required"}
    try:
        adapter._http = adapter._open_http()
        result = await adapter.send(str(chat_id or ""), str(message or ""))
    except Exception as exc:
        return {"error": redact(exc)}
    finally:
        await adapter._close_http()
    if result.success:
        return {"success": True, "message_id": result.message_id or "", "chat_id": str(chat_id or "")}
    return {"error": redact(result.error) or "send_failed"}


def register(ctx) -> None:
    ctx.register_platform(
        name="naonao",
        label="NaoNao",
        adapter_factory=lambda cfg: NaoNaoAdapter(cfg),
        check_fn=check_naonao_requirements,
        ensure_deps_fn=ensure_naonao_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["NAONAO_BOT_TOKEN"],
        allowed_users_env="NAONAO_ALLOWED_USERS",
        allow_all_env="NAONAO_ALLOW_ALL_USERS",
        cron_deliver_env_var="NAONAO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        install_hint="Enable the plugin so Hermes syncs websockets>=13 and python-socks>=2 into the gateway runtime, then restart the gateway.",
        env_enablement_fn=_env_enablement,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=True,
        platform_hint=(
            "You are replying in a NaoNao community channel. "
            "Send plain text. Files, images, and voice are not available on this adapter. "
            "The triggering message may still contain an @mention."
        ),
    )
