"""NaoNao platform adapter for Hermes Gateway.

Uses NaoNao Public Bot API v1 only:

    Authorization: Bot <nn_bot_>
    POST /api/v1/bot/gateway-session
    WSS <api-base-host>/gateway/v1
    message.bot_mention
    command.invoked
    POST /api/v1/channels/:channelId/messages
    PATCH /api/v1/channels/:channelId/messages/:messageId
    POST /api/v1/channels/:channelId/typing
    POST /api/v1/channels/:channelId/activity
    PUT  /api/v1/channels/:channelId/messages/:messageId/reactions/:emoji
    DELETE /api/v1/channels/:channelId/messages/:messageId/reactions/:emoji

Hermes Gateway runs the agent. This module does not call Hermes /v1/runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse, urlunparse

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
# Adapter-level coalesce for denser Hermes edit ticks; pairs with ~180/min API edit limit.
EDIT_THROTTLE_SECONDS = 0.35
EDIT_MAX_ATTEMPTS = 3
EDIT_RETRY_AFTER_CAP_SECONDS = 10.0
# Phase 2/3F: temporary processing ACK on the triggering user message.
# Added on_processing_start; removed on_processing_complete (final/abort).
ACK_REACTION_EMOJI = "👀"
# Phase 3: Hermes tool name → NaoNao safe activity category (never raw names/args).
ACTIVITY_CATEGORIES = frozenset({
    "processing", "search", "browser", "file", "code", "external",
})
# Conservative mapping of Hermes built-in tool names. Unknown → processing.
_TOOL_ACTIVITY_CATEGORY: Dict[str, str] = {
    "web_search": "search",
    "search_files": "search",
    "session_search": "search",
    "tool_search": "search",
    "web_extract": "browser",
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_type": "browser",
    "browser_scroll": "browser",
    "browser_back": "browser",
    "browser_press": "browser",
    "browser_snapshot": "browser",
    "browser_get_images": "browser",
    "browser_vision": "browser",
    "browser_exec": "browser",
    "read_file": "file",
    "write_file": "file",
    "patch": "file",
    "skill_view": "file",
    "terminal": "code",
    "execute_code": "code",
    "tool_call": "external",
    "tool_describe": "external",
}
_SECRET_RE = re.compile(r"nn_(?:bot|bgs|gwr)_[A-Za-z0-9_-]+")

# response_language (DEPRECATED — ignored at runtime since v1.3.4).
# Kept only so existing Desktop/YAML settings (zh-CN|en|auto) do not break
# config load. Hermes / the model chooses reply language from the user message
# and conversation context. This adapter must not inject language instructions.
RESPONSE_LANGUAGE_DEFAULT = "zh-CN"
RESPONSE_LANGUAGE_VALUES = frozenset({"auto", "zh-CN", "en"})
# Sentinel: plugin-settings key absent (distinct from explicit null/empty).
_RESPONSE_LANGUAGE_UNSET = object()
NAONAO_CAPABILITY_HINT = (
    "You are replying in a NaoNao community channel. "
    "Send plain text. Files, images, and voice are not available on this adapter. "
    "The triggering message may still contain an @mention."
)


def normalize_response_language(raw: Any) -> str:
    """Normalize a legacy configured value to auto|zh-CN|en (compat only).

    Invalid/empty → default zh-CN. Runtime no longer injects language prompts
    from this value.
    """
    if raw is None:
        return RESPONSE_LANGUAGE_DEFAULT
    text = str(raw).strip().replace("_", "-")
    if not text:
        return RESPONSE_LANGUAGE_DEFAULT
    key = text.lower()
    if key == "auto":
        return "auto"
    if key in ("en", "en-us", "en-gb"):
        return "en"
    if key in ("zh-cn", "zh", "zh-hans"):
        return "zh-CN"
    if text in RESPONSE_LANGUAGE_VALUES:
        return text
    logger.debug(
        "[naonao] ignoring invalid deprecated response_language=%r (compat normalize → %s)",
        raw,
        RESPONSE_LANGUAGE_DEFAULT,
    )
    return RESPONSE_LANGUAGE_DEFAULT


def language_preference_instruction(response_language: str = "") -> str:
    """Always empty: NaoNao Plugin does not force agent response language.

    ``response_language`` is accepted for call-site compatibility and ignored.
    """
    del response_language
    return ""


def build_naonao_platform_hint(response_language: str = RESPONSE_LANGUAGE_DEFAULT) -> str:
    """Capability-only platform hint (no language policy).

    ``response_language`` is accepted for call-site compatibility and ignored.
    """
    del response_language
    return NAONAO_CAPABILITY_HINT


def _read_platforms_naonao_response_language() -> Any:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        block = (cfg.get("platforms") or {}).get("naonao")
        if not isinstance(block, dict):
            return None
        if "response_language" in block:
            return block.get("response_language")
        extra = block.get("extra")
        if isinstance(extra, dict) and "response_language" in extra:
            return extra.get("response_language")
    except Exception:
        logger.debug("[naonao] platforms.naonao response_language read skipped", exc_info=True)
    return None


def _read_plugin_settings_response_language() -> Any:
    """Return the stored plugin setting if the key exists; else ``_RESPONSE_LANGUAGE_UNSET``.

    Key presence is required — schema default alone does not count as explicit.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
        entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
        # Install id is usually "naonao"; symlink path installs may use "platforms/naonao".
        for plugin_id in ("naonao", "platforms/naonao"):
            entry = entries.get(plugin_id)
            if not isinstance(entry, dict):
                continue
            settings = entry.get("settings")
            if not isinstance(settings, dict):
                settings = entry.get("config") if isinstance(entry.get("config"), dict) else {}
            if isinstance(settings, dict) and "response_language" in settings:
                return settings.get("response_language")
    except Exception:
        logger.debug("[naonao] plugin settings response_language read skipped", exc_info=True)
    return _RESPONSE_LANGUAGE_UNSET


def _explicit_plugin_response_language(*, plugin_get_config: Any = None) -> Any:
    """Raw plugin setting when the key is stored; else ``_RESPONSE_LANGUAGE_UNSET``.

    Hermes ``ctx.get_config(key, default)`` returns *default* when the key is absent and
    does not apply ``config_schema.default``. Passing ``_RESPONSE_LANGUAGE_UNSET`` as that
    default distinguishes absent from an explicit stored value (including ``zh-CN``).
    """
    if callable(plugin_get_config):
        try:
            value = plugin_get_config("response_language", _RESPONSE_LANGUAGE_UNSET)
            if value is not _RESPONSE_LANGUAGE_UNSET:
                return value
        except Exception:
            logger.debug("[naonao] plugin get_config response_language skipped", exc_info=True)
    return _read_plugin_settings_response_language()


def resolve_naonao_response_language(
    *,
    config: Any = None,
    plugin_get_config: Any = None,
) -> str:
    """Resolve preference: explicit plugin settings → legacy platform → default zh-CN.

    Only a key actually present under ``plugins.entries.*.settings`` (or legacy
    ``config``) counts as explicit plugin settings. Schema default is not explicit.
    An explicit but invalid/empty plugin value normalizes to ``zh-CN`` and does not
    fall through to platform config (the user already chose plugin settings).
    """
    plugin_raw = _explicit_plugin_response_language(plugin_get_config=plugin_get_config)
    if plugin_raw is not _RESPONSE_LANGUAGE_UNSET:
        return normalize_response_language(plugin_raw)

    platform_raw = None
    if config is not None:
        extra = getattr(config, "extra", None) or {}
        if isinstance(extra, dict) and "response_language" in extra:
            platform_raw = extra.get("response_language")
    if platform_raw is None or str(platform_raw).strip() == "":
        platform_raw = _read_platforms_naonao_response_language()

    if platform_raw is not None and str(platform_raw).strip() != "":
        return normalize_response_language(platform_raw)
    return RESPONSE_LANGUAGE_DEFAULT


def map_tool_to_activity_category(tool_name: str) -> str:
    """Map a Hermes tool name to a Public Bot API activity category.

    Never returns raw tool names. Unknown / empty → processing.
    """
    name = str(tool_name or "").strip()
    if not name or name == "_thinking":
        return "processing"
    mapped = _TOOL_ACTIVITY_CATEGORY.get(name)
    if mapped in ACTIVITY_CATEGORIES:
        return mapped
    # MCP / plugin tools often look like mcp__server__tool — treat as external.
    lower = name.lower()
    if lower.startswith("mcp") or "__" in name:
        return "external"
    return "processing"
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
    # Progressive edit via PATCH same messageId. Must stay True only while
    # edit_message() is implemented — Hermes falls back to a second send() on edit failure.
    SUPPORTS_MESSAGE_EDITING = True
    # Never replace a streamed preview with a fresh final POST (duplicate risk).
    FALLBACK_ON_FINAL_EDIT_FLOOD = False
    # Phase 3: Gateway _progress_live_status → set_status_text on tool.started/completed.
    supports_status_text = True

    def __init__(self, config: PlatformConfig, *, http_factory=None, ws_connect=None):
        super().__init__(config=config, platform=Platform("naonao"))
        # One NaoNao channel stays one Hermes session. user_id is the auth
        # principal only; Hermes would otherwise append it to the session key.
        if isinstance(config.extra, dict):
            config.extra["group_sessions_per_user"] = False
        extra = config.extra or {}
        self._response_language = resolve_naonao_response_language(config=config)
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
        # Per messageId: single in-flight PATCH + pending coalesce.
        self._edit_lanes: Dict[str, Dict[str, Any]] = {}
        # Phase 3: last safe activity category per chat; pending flush for sync→async bridge.
        self._activity_category_by_chat: Dict[str, str] = {}
        self._activity_flush_needed: Dict[str, Optional[str]] = {}  # chat_id → category|None(clear)
        self._asyncio_loop: Optional[asyncio.AbstractEventLoop] = None

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
        try:
            self._asyncio_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._asyncio_loop = None
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
            # First streamed POST clears Typing + Activity; server also clears on create.
            await self._set_channel_typing(channel_id, False)
            await self._set_channel_activity(channel_id, False)
        else:
            logger.info(
                "[naonao] outbound send failed chat_id=%s status=%s error=%s",
                chat_id, getattr(result, "_http_status", "") or "-", redact(result.error),
            )
        return result

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Best-effort Typing ON (Hermes _keep_typing refreshes ~2s). Never raises.

        Also flushes any pending Tool Activity posted from sync set_status_text.
        Tool Activity > Typing: skip typing refresh while activity is active for this chat.
        """
        del metadata
        await self._flush_pending_activity(str(chat_id))
        if self._activity_category_by_chat.get(str(chat_id)):
            return
        try:
            _community, channel_id = split_chat_id(chat_id)
        except ValueError:
            return
        await self._set_channel_typing(channel_id, True)

    async def stop_typing(self, chat_id: str) -> None:
        """Best-effort Typing OFF. Never raises."""
        try:
            _community, channel_id = split_chat_id(chat_id)
        except ValueError:
            return
        await self._set_channel_typing(channel_id, False)

    def set_status_text(self, chat_id: str, text: Optional[str] = None) -> None:
        """Hermes Gateway live-status hook (tool.started / tool.completed).

        Gateway may pass a human phrase that includes previews (paths/URLs/queries).
        We NEVER forward that phrase to NaoNao — only a safe enum category.
        Sync-safe: queues a flush; HTTP runs on the adapter event loop / typing tick.
        """
        super().set_status_text(chat_id, text)
        chat_key = str(chat_id or "")
        if not chat_key:
            return
        try:
            split_chat_id(chat_key)
        except ValueError:
            return

        if not text:
            self._activity_category_by_chat.pop(chat_key, None)
            self._activity_flush_needed[chat_key] = None  # clear
        else:
            category = self._category_from_status_phrase(str(text))
            self._activity_category_by_chat[chat_key] = category
            self._activity_flush_needed[chat_key] = category
        self._kick_activity_flush(chat_key)

    def set_activity_for_tool(self, chat_id: str, tool_name: str) -> None:
        """Explicit helper (tests): map tool name → activity without using phrase text."""
        chat_key = str(chat_id or "")
        try:
            split_chat_id(chat_key)
        except ValueError:
            return
        category = map_tool_to_activity_category(tool_name)
        self._activity_category_by_chat[chat_key] = category
        self._activity_flush_needed[chat_key] = category
        self._kick_activity_flush(chat_key)

    def clear_activity(self, chat_id: str) -> None:
        """Explicit helper (tests): clear activity for a chat."""
        self.set_status_text(chat_id, None)

    @staticmethod
    def _category_from_status_phrase(phrase: str) -> str:
        """Derive safe category from Hermes status/progress phrase; never forward phrase.

        Matches live-status stems (``build_status_phrase``) and curated tool verbs
        (``get_tool_verb``) so both Slack-style ``is searching…`` and progress-style
        ``🔍 Searching the web for …`` classify to the same safe enum. Classification
        only — the raw phrase is discarded before any NaoNao API call.
        """
        raw = str(phrase or "").strip()
        if not raw:
            return "processing"
        raw_l = raw.lower()
        try:
            from agent.display import build_status_phrase, get_tool_verb
        except Exception:
            return "processing"
        best = ("processing", 0)
        for tool_name, category in _TOOL_ACTIVITY_CATEGORY.items():
            score = 0
            try:
                stem_phrase = build_status_phrase(tool_name, None)
            except Exception:
                stem_phrase = None
            if stem_phrase:
                stem = str(stem_phrase).rstrip("…").rstrip(".").strip()
                if stem and (raw.startswith(stem) or stem.lower() in raw_l):
                    score = max(score, len(stem))
            try:
                verb = get_tool_verb(tool_name)
            except Exception:
                verb = None
            if verb:
                verb_s = str(verb).strip()
                if verb_s and verb_s.lower() in raw_l:
                    score = max(score, len(verb_s))
            if score > best[1]:
                best = (category, score)
        return best[0] if best[0] in ACTIVITY_CATEGORIES else "processing"

    def _kick_activity_flush(self, chat_id: str) -> None:
        """Schedule async flush onto the gateway loop when available."""
        loop = self._asyncio_loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._flush_pending_activity(chat_id), loop)
        except Exception:
            logger.debug("[naonao] activity flush kick failed")

    async def _flush_pending_activity(self, chat_id: str) -> None:
        if chat_id not in self._activity_flush_needed:
            return
        category = self._activity_flush_needed.pop(chat_id)
        try:
            _community, channel_id = split_chat_id(chat_id)
        except ValueError:
            return
        if category is None:
            await self._set_channel_activity(channel_id, False)
        else:
            await self._set_channel_activity(channel_id, True, category=category)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add 👀 on the triggering user message. Best-effort; never blocks the turn."""
        chat_id = getattr(getattr(event, "source", None), "chat_id", None) or ""
        message_id = str(getattr(event, "message_id", None) or "").strip()
        if not chat_id or not message_id:
            return
        try:
            _community, channel_id = split_chat_id(str(chat_id))
        except ValueError:
            return
        await self._put_reaction(channel_id, message_id, ACK_REACTION_EMOJI)

    async def on_processing_complete(self, event: MessageEvent, outcome: Any) -> None:
        """Phase 3F: 👀 is a temporary ACK — remove after final delivery / abort.

        Hermes fires this after final text delivery (or on cancel/failure), so
        removal timing matches "final answer delivery complete". Best-effort;
        never raises; never blocks the turn. Only deletes this Bot's own reactor
        (actor-scoped DELETE API).
        """
        del outcome
        chat_id = getattr(getattr(event, "source", None), "chat_id", None) or ""
        message_id = str(getattr(event, "message_id", None) or "").strip()
        if chat_id:
            # Best-effort Activity clear if a tool turn aborted without set_status_text(None).
            try:
                self.set_status_text(str(chat_id), None)
            except Exception:
                pass
        if not chat_id or not message_id:
            return
        try:
            _community, channel_id = split_chat_id(str(chat_id))
        except ValueError:
            return
        await self._delete_reaction(channel_id, message_id, ACK_REACTION_EMOJI)

    async def _set_channel_typing(self, channel_id: str, active: bool) -> None:
        if self._http is None or self.has_fatal_error or not channel_id:
            return
        try:
            response = await self._http.request(
                "POST",
                f"{self._api_base}/api/v1/channels/{channel_id}/typing",
                headers={
                    "Authorization": f"Bot {self._bot_token}",
                    "Content-Type": "application/json",
                },
                json={"active": bool(active)},
                follow_redirects=False,
            )
            if response.status_code == 401:
                await self._fatal_auth()
                return
            if response.status_code >= 400:
                logger.debug(
                    "[naonao] typing %s failed status=%s",
                    "on" if active else "off",
                    response.status_code,
                )
        except _FatalAuth:
            return
        except Exception as exc:
            logger.debug("[naonao] typing request failed: %s", redact(exc))

    async def _set_channel_activity(
        self, channel_id: str, active: bool, *, category: Optional[str] = None
    ) -> None:
        """Best-effort Activity API. Never raises; never sends free-form text."""
        if self._http is None or self.has_fatal_error or not channel_id:
            return
        body: Dict[str, Any] = {"active": bool(active)}
        if active:
            cat = str(category or "processing")
            if cat not in ACTIVITY_CATEGORIES:
                cat = "processing"
            body["category"] = cat
        try:
            response = await self._http.request(
                "POST",
                f"{self._api_base}/api/v1/channels/{channel_id}/activity",
                headers={
                    "Authorization": f"Bot {self._bot_token}",
                    "Content-Type": "application/json",
                },
                json=body,
                follow_redirects=False,
            )
            if response.status_code == 401:
                await self._fatal_auth()
                return
            if response.status_code >= 400:
                logger.debug(
                    "[naonao] activity %s failed status=%s",
                    "on" if active else "off",
                    response.status_code,
                )
        except _FatalAuth:
            return
        except Exception as exc:
            logger.debug("[naonao] activity request failed: %s", redact(exc))

    async def _put_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        if self._http is None or self.has_fatal_error or not channel_id or not message_id:
            return
        enc = quote(str(emoji or ""), safe="")
        if not enc:
            return
        try:
            response = await self._http.request(
                "PUT",
                f"{self._api_base}/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{enc}",
                headers={"Authorization": f"Bot {self._bot_token}"},
                follow_redirects=False,
            )
            if response.status_code == 401:
                await self._fatal_auth()
                return
            if response.status_code >= 400:
                logger.debug(
                    "[naonao] reaction put failed status=%s emoji=%s",
                    response.status_code,
                    emoji,
                )
        except _FatalAuth:
            return
        except Exception as exc:
            logger.debug("[naonao] reaction request failed: %s", redact(exc))

    async def _delete_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """Best-effort remove this Bot's reactor. Never raises. Idempotent."""
        if self._http is None or self.has_fatal_error or not channel_id or not message_id:
            return
        enc = quote(str(emoji or ""), safe="")
        if not enc:
            return
        try:
            response = await self._http.request(
                "DELETE",
                f"{self._api_base}/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{enc}",
                headers={"Authorization": f"Bot {self._bot_token}"},
                follow_redirects=False,
            )
            if response.status_code == 401:
                await self._fatal_auth()
                return
            # 404 = already gone / never added — success for cleanup.
            if response.status_code >= 400 and response.status_code != 404:
                logger.debug(
                    "[naonao] reaction delete failed status=%s emoji=%s",
                    response.status_code,
                    emoji,
                )
        except _FatalAuth:
            return
        except Exception as exc:
            logger.debug("[naonao] reaction delete failed: %s", redact(exc))

    def prefers_fresh_final_streaming(
        self, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Always edit in place — a fresh final POST would duplicate the preview."""
        del content, metadata
        return False

    def _edit_lane(self, message_id: str) -> Dict[str, Any]:
        mid = str(message_id or "")
        lane = self._edit_lanes.get(mid)
        if lane is None:
            lane = {
                "lock": asyncio.Lock(),
                "last_flush_at": 0.0,
                "dead": False,
                "pending": None,
                "pending_finalize": False,
                "flushing": False,
                "flush_done": asyncio.Event(),
                "channel_id": "",
                "last_result": None,
            }
            lane["flush_done"].set()
            self._edit_lanes[mid] = lane
        return lane

    @staticmethod
    def _skipped_edit(message_id: str, **extra: Any) -> SendResult:
        """Coalesce/defer only — Hermes must NOT treat this as final delivery.

        Used when a newer edit superseded this one, or the lane is mid-flush.
        Must never be used for permanent HTTP failures (403/404) or exhausted
        retries: those must return success=False so GatewayStreamConsumer can
        enter fallback and the turn reconciler does not set already_sent.
        """
        payload = {"skipped": True, **extra}
        return SendResult(success=True, message_id=str(message_id or "") or None, raw_response=payload)

    @staticmethod
    def _failed_edit(
        message_id: str,
        *,
        error: str,
        error_kind: str,
        retryable: bool = False,
        **extra: Any,
    ) -> SendResult:
        """Permanent / exhausted edit failure — surface to Hermes fallback contract."""
        payload = {"naonao_edit_failed": True, **extra}
        result = SendResult(
            success=False,
            message_id=str(message_id or "") or None,
            error=str(error or "edit_failed"),
            error_kind=str(error_kind or "unknown"),
            retryable=bool(retryable),
            raw_response=payload,
        )
        return result

    def _dead_edit_result(self, message_id: str, *, finalize: bool = False) -> SendResult:
        lane = self._edit_lanes.get(str(message_id or "")) or {}
        last = lane.get("last_result")
        if (
            last is not None
            and not getattr(last, "success", True)
            and getattr(last, "error_kind", None)
        ):
            # Preserve the original permanent failure for Hermes reconcile/fallback.
            return last
        return self._failed_edit(
            message_id,
            error="edit_lane_dead",
            error_kind="not_found",
            naonao_edit_dead=True,
            finalize=finalize,
        )

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """PATCH full accumulated content onto an existing Bot message.

        Hermes stream consumer passes the full buffer (cursor-suffixed until finalize).
        Never POST a replacement message from this path.
        """
        try:
            _community, channel_id = split_chat_id(chat_id)
        except ValueError as exc:
            return SendResult(success=False, error=str(exc), error_kind="not_found")
        mid = str(message_id or "").strip()
        if not mid or mid == "__no_edit__":
            return SendResult(success=False, error="invalid_message_id", error_kind="not_found")
        text = str(content or "")
        if len(text) > MAX_MESSAGE_LENGTH:
            return SendResult(success=False, error="message_too_long", error_kind="too_long")
        if self.has_fatal_error:
            return SendResult(success=False, error="invalid_bot_token", error_kind="forbidden")

        lane = self._edit_lane(mid)
        become_flusher = False
        async with lane["lock"]:
            lane["channel_id"] = channel_id
            if lane["dead"]:
                logger.warning(
                    "[naonao] edit aborted; message is dead message_id=%s finalize=%s",
                    mid, finalize,
                )
                return self._dead_edit_result(mid, finalize=finalize)

            # Newest full accumulated content wins; intermediate versions are dropped.
            lane["pending"] = text
            if finalize:
                lane["pending_finalize"] = True

            if lane["flushing"]:
                if finalize:
                    # Wait outside the lock for the active flusher, then become flusher.
                    done_event = lane["flush_done"]
                else:
                    return self._skipped_edit(mid, naonao_edit_coalesced=True)
            else:
                lane["flushing"] = True
                lane["flush_done"].clear()
                become_flusher = True
                done_event = None

        if not become_flusher:
            await done_event.wait()
            async with lane["lock"]:
                if lane["dead"]:
                    return self._dead_edit_result(mid, finalize=finalize)
                if lane["flushing"]:
                    return lane["last_result"] or self._skipped_edit(mid, naonao_edit_coalesced=True)
                if lane["pending"] is None and not lane["pending_finalize"]:
                    return lane["last_result"] or self._skipped_edit(mid, naonao_edit_coalesced=True)
                lane["flushing"] = True
                lane["flush_done"].clear()
                become_flusher = True

        try:
            result = await self._flush_edit_lane(mid, force_immediate=finalize)
            async with lane["lock"]:
                lane["last_result"] = result
            return result
        finally:
            async with lane["lock"]:
                lane["flushing"] = False
                lane["flush_done"].set()

    async def _flush_edit_lane(self, message_id: str, *, force_immediate: bool) -> SendResult:
        lane = self._edit_lane(message_id)
        last_result: Optional[SendResult] = None
        while True:
            async with lane["lock"]:
                if lane["dead"]:
                    return self._dead_edit_result(message_id, finalize=force_immediate)
                channel_id = str(lane.get("channel_id") or "")
                latest = lane["pending"]
                do_finalize = bool(lane["pending_finalize"])
                if latest is None:
                    return last_result or self._skipped_edit(message_id, naonao_edit_empty=True)
                lane["pending"] = None
                lane["pending_finalize"] = False
                last_flush_at = float(lane["last_flush_at"] or 0.0)

            if not force_immediate and not do_finalize and last_flush_at:
                wait = EDIT_THROTTLE_SECONDS - (time.monotonic() - last_flush_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                    async with lane["lock"]:
                        if lane["pending"] is not None:
                            continue
                        if lane["pending_finalize"]:
                            do_finalize = True

            result = await self._patch_message_with_retries(
                channel_id, message_id, str(latest), finalize=do_finalize or force_immediate
            )
            last_result = result
            async with lane["lock"]:
                raw = result.raw_response if isinstance(result.raw_response, dict) else {}
                if raw.get("naonao_edit_dead") or (
                    not result.success
                    and (getattr(result, "error_kind", None) in ("forbidden", "not_found"))
                ):
                    lane["dead"] = True
                    return result
                if result.success and not raw.get("naonao_edit_gave_up"):
                    lane["last_flush_at"] = time.monotonic()
                if not result.success and raw.get("naonao_edit_gave_up"):
                    # Exhausted retries: do not mark dead forever — Hermes may
                    # still finalize via fallback send; avoid fake success.
                    return result
                if lane["pending"] is not None or lane["pending_finalize"]:
                    force_immediate = bool(lane["pending_finalize"] or force_immediate)
                    continue
                return result

    async def _patch_message_with_retries(
        self, channel_id: str, message_id: str, content: str, *, finalize: bool
    ) -> SendResult:
        last: Optional[SendResult] = None
        for attempt in range(1, EDIT_MAX_ATTEMPTS + 1):
            last = await self._patch_message(channel_id, message_id, content)
            if last.success:
                return last
            kind = getattr(last, "error_kind", None) or "unknown"
            status = getattr(last, "_http_status", None)
            if kind in ("forbidden", "not_found") or status in (403, 404):
                logger.error(
                    "[naonao] edit permanently failed message_id=%s status=%s error=%s — "
                    "signaling failure for Hermes fallback (no fake delivery)",
                    message_id, status or "-", redact(last.error),
                )
                failed = self._failed_edit(
                    message_id,
                    error=str(last.error or kind),
                    error_kind=str(kind if kind in ("forbidden", "not_found") else (
                        "forbidden" if status == 403 else "not_found"
                    )),
                    naonao_edit_dead=True,
                    finalize=finalize,
                )
                failed._http_status = status
                return failed
            if not getattr(last, "retryable", False):
                break
            if attempt >= EDIT_MAX_ATTEMPTS:
                break
            delay = float(getattr(last, "retry_after", None) or 0.25 * attempt)
            delay = max(0.05, min(delay, EDIT_RETRY_AFTER_CAP_SECONDS))
            logger.info(
                "[naonao] edit retry message_id=%s attempt=%s/%s after=%.2fs",
                message_id, attempt, EDIT_MAX_ATTEMPTS, delay,
            )
            await asyncio.sleep(delay)

        # Exhausted / non-retryable: surface failure so Hermes can fallback-send
        # the complete answer instead of suppressing final delivery via already_sent.
        logger.error(
            "[naonao] edit gave up message_id=%s finalize=%s error=%s — signaling failure for fallback",
            message_id, finalize, redact(last.error if last else ""),
        )
        failed = self._failed_edit(
            message_id,
            error=str((last.error if last else None) or "edit_gave_up"),
            error_kind=str(getattr(last, "error_kind", None) or "transient"),
            retryable=False,
            naonao_edit_gave_up=True,
            finalize=finalize,
            last_error=redact(last.error if last else ""),
        )
        if last is not None:
            failed._http_status = getattr(last, "_http_status", None)
        return failed

    async def _patch_message(self, channel_id: str, message_id: str, content: str) -> SendResult:
        if self._http is None:
            result = SendResult(success=False, error="http_unavailable", error_kind="transient", retryable=True)
            result._http_status = "-"
            return result
        try:
            response = await self._http.request(
                "PATCH",
                f"{self._api_base}/api/v1/channels/{channel_id}/messages/{message_id}",
                headers={
                    "Authorization": f"Bot {self._bot_token}",
                    "Content-Type": "application/json",
                },
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
        if response.status_code == 200:
            message = body.get("message") if isinstance(body, dict) else {}
            mid = str((message or {}).get("id") or message_id) or message_id
            result = SendResult(success=True, message_id=mid, raw_response=body)
            result._http_status = response.status_code
            return result
        if response.status_code == 401:
            await self._fatal_auth()
            result = SendResult(success=False, error="invalid_bot_token", error_kind="forbidden")
            result._http_status = response.status_code
            return result
        if response.status_code == 403:
            result = SendResult(
                success=False,
                error=str(body.get("err") or "insufficient_permissions"),
                error_kind="forbidden",
            )
            result._http_status = response.status_code
            return result
        if response.status_code == 404:
            result = SendResult(
                success=False,
                error=str(body.get("err") or "resource_not_found"),
                error_kind="not_found",
            )
            result._http_status = response.status_code
            return result
        if response.status_code == 429:
            result = SendResult(
                success=False,
                error="rate_limited",
                error_kind="rate_limited",
                retryable=True,
                retry_after=float(body.get("retryAfterSec") or 1),
            )
            result._http_status = response.status_code
            return result
        if response.status_code == 400 and str(body.get("err") or "") == "message_too_long":
            result = SendResult(success=False, error="message_too_long", error_kind="too_long")
            result._http_status = response.status_code
            return result
        if response.status_code >= 500:
            result = SendResult(
                success=False,
                error=str(body.get("err") or "internal_error"),
                error_kind="transient",
                retryable=True,
            )
            result._http_status = response.status_code
            return result
        result = SendResult(
            success=False,
            error=str(body.get("err") or response.status_code),
            error_kind="unknown",
        )
        result._http_status = response.status_code
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
        # Hermes MessageEvent first-class reply fields (NOT metadata).
        # Admission may reject nonempty metadata; reply context must use reply_to_*.
        reply = message.get("reply") if isinstance(message.get("reply"), dict) else {}
        reply_id = str(message.get("replyToMessageId") or reply.get("messageId") or "").strip() or None
        reply_excerpt = str(reply.get("excerpt") or reply.get("content") or "").strip()
        if reply_id and not reply_excerpt and (reply.get("deleted") or reply.get("removed")):
            reply_excerpt = "(original message unavailable)"
        reply_author_name = str(reply.get("authorName") or "").strip() or None
        reply_author_id = str(reply.get("authorId") or "").strip() or None
        reply_author_type = str(reply.get("authorType") or "user").strip().lower()
        reply_to_is_own = bool(
            reply_id
            and reply_author_type == "bot"
            and self._bot_id
            and reply_author_id
            and str(reply_author_id) == str(self._bot_id)
        )
        return MessageEvent(
            text=strip_leading_mention(content, self._bot_name),
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=username or None,
            source=source,
            message_id=str(message.get("id") or "") or None,
            timestamp=timestamp,
            reply_to_message_id=reply_id,
            reply_to_text=(reply_excerpt or None) if reply_id else None,
            reply_to_author_id=reply_author_id if reply_id else None,
            reply_to_author_name=reply_author_name if reply_id else None,
            reply_to_is_own_message=reply_to_is_own,
            # Do not inject response-language policy; Hermes follows conversation.
            channel_prompt=None,
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
            channel_prompt=None,
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


def _install_naonao_display_defaults() -> None:
    """Declare NaoNao display defaults (telegram pattern): tool_progress off.

    Exact root cause of Phase 3 Owner FAIL: Hermes global default
    ``tool_progress=all`` for plugin platforms without ``_PLATFORM_DEFAULTS``
    routes ``_progress_build_message`` → ``_progress_emit`` → progress_queue →
    ``_send_progress_text`` → ``adapter.send()``, creating a chat Message such as
    ``🔍 Searching the web for …``.

    Live status (``supports_status_text`` / ``set_status_text`` → Activity API) is
    independent and keeps working when tool_progress is off.

    Do NOT use ``display_tier="low"``: that also sets ``streaming=False``, which
    disables Phase 1 same-message streaming via ``StreamingConfig.enabled_for``.
    Injecting a telegram-shaped row keeps ``streaming=None`` (follow global
    ``streaming.enabled``) while silencing progress Message bubbles.
    """
    try:
        from gateway import display_config as dc

        dc._PLATFORM_DEFAULTS["naonao"] = {
            **dc._TIER_HIGH,
            "tool_progress": "off",
            "busy_ack_detail": False,
        }
    except Exception:
        logger.debug("[naonao] display defaults install skipped", exc_info=True)


def register(ctx) -> None:
    _install_naonao_display_defaults()
    # Resolve legacy response_language for compat logging only — never inject.
    _ = resolve_naonao_response_language(
        plugin_get_config=getattr(ctx, "get_config", None)
    )
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
        platform_hint=build_naonao_platform_hint(),
    )
