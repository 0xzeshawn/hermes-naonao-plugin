"""Phase 2: Typing + Reaction lifecycle for NaoNao Hermes adapter.

Asserts call order and best-effort failure (does not block AI reply).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or (Path.home() / ".hermes" / "hermes-agent"))
PLUGIN_DIR = Path(__file__).resolve().parents[1]

if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

if not (HERMES_ROOT / "gateway" / "platforms" / "base.py").is_file():
    raise unittest.SkipTest(f"Hermes source not found at {HERMES_ROOT}")


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "naonao_platform_adapter_p2", PLUGIN_DIR / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
NaoNaoAdapter = adapter_mod.NaoNaoAdapter
register = adapter_mod.register
ACK_REACTION_EMOJI = adapter_mod.ACK_REACTION_EMOJI

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.event import MessageEvent, MessageType


class _Ctx:
    def register_platform(self, **kwargs):
        platform_registry.register(PlatformEntry(
            name=kwargs["name"],
            label=kwargs["label"],
            adapter_factory=kwargs["adapter_factory"],
            check_fn=kwargs["check_fn"],
            ensure_deps_fn=kwargs.get("ensure_deps_fn"),
            validate_config=kwargs.get("validate_config"),
            is_connected=kwargs.get("is_connected"),
            required_env=list(kwargs.get("required_env") or []),
            install_hint=kwargs.get("install_hint") or "",
            allowed_users_env=kwargs.get("allowed_users_env") or "",
            allow_all_env=kwargs.get("allow_all_env") or "",
            cron_deliver_env_var=kwargs.get("cron_deliver_env_var") or "",
            source="plugin",
        ))


# Platform("naonao") requires registry membership via plugin register().
register(_Ctx())


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body


class _Http:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, url, headers=None, json=None, follow_redirects=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": json,
            }
        )
        return self.handler(method, url, headers or {}, json)

    async def aclose(self):
        return None


def _adapter(http: _Http) -> NaoNaoAdapter:
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "bot_token": "nn_bot_test_token_phase2________",
            "api_base": "https://naonaoai.com",
        },
    )
    ad = NaoNaoAdapter(cfg, http_factory=lambda: http)
    ad._http = http
    ad._bot_id = "bot_1"
    ad._bot_name = "我的测试机器人"
    return ad


class Phase2TypingReactionTests(unittest.TestCase):
    def test_ack_emoji_is_eyes(self):
        self.assertEqual(ACK_REACTION_EMOJI, "👀")

    def test_lifecycle_order_typing_reaction_send_stop(self):
        order = []

        def handler(method, url, headers, body):
            if method == "POST" and url.endswith("/typing"):
                active = bool((body or {}).get("active"))
                order.append(("typing", active))
                return _Response(200, {"ok": True, "active": active})
            if method == "PUT" and "/reactions/" in url:
                order.append(("reaction", "👀"))
                return _Response(200, {"ok": True})
            if method == "POST" and url.endswith("/messages"):
                order.append(("send", (body or {}).get("content")))
                return _Response(
                    201,
                    {"ok": True, "message": {"id": "bot_msg_1", "content": body.get("content")}},
                )
            if method == "PATCH":
                order.append(("patch", (body or {}).get("content")))
                return _Response(
                    200,
                    {"ok": True, "message": {"id": "bot_msg_1", "content": body.get("content")}},
                )
            return _Response(500, {"ok": False})

        http = _Http(handler)
        ad = _adapter(http)
        chat_id = "community_a:channel_x"

        async def run():
            source = ad.build_source(
                chat_id=chat_id,
                chat_name="channel_x",
                chat_type="channel",
                user_id="user_1",
                user_name="member",
                message_id="msg_src_1",
                scope_id="community_a",
            )
            event = MessageEvent(
                text="请介绍一下你自己",
                message_type=MessageType.TEXT,
                user_id="user_1",
                user_name="member",
                source=source,
                message_id="msg_src_1",
            )
            await ad.send_typing(chat_id)
            await ad.on_processing_start(event)
            send_r = await ad.send(chat_id, "你好，我是测试机器人。")
            self.assertTrue(send_r.success)
            self.assertEqual(send_r.message_id, "bot_msg_1")
            edit_r = await ad.edit_message(
                chat_id, "bot_msg_1", "你好，我是测试机器人。完整回答。", finalize=True
            )
            self.assertTrue(edit_r.success)

        asyncio.run(run())

        # typing ON → reaction → send → typing OFF (from send success) → patch
        typing_ops = [c for c in order if c[0] == "typing"]
        self.assertEqual(typing_ops[0], ("typing", True))
        self.assertIn(("reaction", "👀"), order)
        self.assertTrue(any(c[0] == "send" for c in order))
        self.assertTrue(any(c[0] == "typing" and c[1] is False for c in order))
        self.assertTrue(any(c[0] == "patch" for c in order))

        # Reaction targets source message id
        put = next(c for c in http.calls if c["method"] == "PUT")
        enc = quote("👀", safe="")
        self.assertIn(f"/messages/msg_src_1/reactions/{enc}", put["url"])

    def test_reaction_failure_does_not_block_send(self):
        def handler(method, url, headers, body):
            if method == "PUT":
                return _Response(500, {"ok": False, "err": "internal_error"})
            if method == "POST" and url.endswith("/typing"):
                return _Response(200, {"ok": True})
            if method == "POST" and url.endswith("/messages"):
                return _Response(201, {"ok": True, "message": {"id": "m1"}})
            return _Response(404)

        http = _Http(handler)
        ad = _adapter(http)

        async def run():
            source = ad.build_source(
                chat_id="c:ch",
                chat_name="ch",
                chat_type="channel",
                user_id="u",
                user_name="m",
                message_id="src1",
                scope_id="c",
            )
            event = MessageEvent(
                text="hi",
                message_type=MessageType.TEXT,
                user_id="u",
                user_name="m",
                source=source,
                message_id="src1",
            )
            await ad.on_processing_start(event)
            r = await ad.send("c:ch", "answer")
            self.assertTrue(r.success)

        asyncio.run(run())

    def test_typing_start_failure_does_not_block_send(self):
        def handler(method, url, headers, body):
            if method == "POST" and url.endswith("/typing"):
                return _Response(500, {"ok": False})
            if method == "POST" and url.endswith("/messages"):
                return _Response(201, {"ok": True, "message": {"id": "m1"}})
            return _Response(404)

        http = _Http(handler)
        ad = _adapter(http)

        async def run():
            await ad.send_typing("c:ch")
            r = await ad.send("c:ch", "answer")
            self.assertTrue(r.success)

        asyncio.run(run())

    def test_on_processing_complete_is_noop_keeps_eyes(self):
        """Phase 2 must not swap 👀 → ✅."""
        calls = []

        def handler(method, url, headers, body):
            calls.append(method + " " + url)
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)

        async def run():
            source = ad.build_source(
                chat_id="c:ch",
                chat_name="ch",
                chat_type="channel",
                user_id="u",
                user_name="m",
                message_id="src1",
                scope_id="c",
            )
            event = MessageEvent(
                text="hi",
                message_type=MessageType.TEXT,
                user_id="u",
                user_name="m",
                source=source,
                message_id="src1",
            )
            await ad.on_processing_complete(event, "SUCCESS")

        asyncio.run(run())
        self.assertEqual(calls, [])

    def test_source_message_id_from_gateway_mention(self):
        ad = _adapter(_Http(lambda *a: _Response(200)))
        event = ad.build_mention_event(
            {
                "version": "1",
                "id": "evt_1",
                "type": "message.bot_mention",
                "createdAt": "2026-10-13T04:00:00.000Z",
                "application": {"id": "app_1"},
                "bot": {"id": "bot_1", "name": "我的测试机器人"},
                "community": {"id": "community_a"},
                "channel": {"id": "channel_x"},
                "actor": {"type": "user", "username": "member", "id": "user_1"},
                "message": {
                    "id": "msg_stable_src",
                    "content": "@我的测试机器人 请介绍一下你自己",
                    "createdAt": "2026-10-13T04:00:00.000Z",
                },
                "installation": {"id": "inst_1"},
            },
            1,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.message_id, "msg_stable_src")


if __name__ == "__main__":
    unittest.main()
