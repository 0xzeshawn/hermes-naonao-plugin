"""Phase 3F: tool status never becomes a Message; 👀 temporary ACK cleanup."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or (Path.home() / ".hermes" / "hermes-agent"))
PLUGIN_DIR = Path(__file__).resolve().parents[1]

if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

if not (HERMES_ROOT / "gateway" / "platforms" / "base.py").is_file():
    raise unittest.SkipTest(f"Hermes source not found at {HERMES_ROOT}")


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "naonao_platform_adapter_p3f", PLUGIN_DIR / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
NaoNaoAdapter = adapter_mod.NaoNaoAdapter
register = adapter_mod.register
_install_naonao_display_defaults = adapter_mod._install_naonao_display_defaults

from gateway.config import PlatformConfig
from gateway.display_config import resolve_display_setting, resolve_tool_progress
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


register(_Ctx())

OWNER_PROGRESS_PHRASE = "🔍 Searching the web for AI news 2026-10-13"


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body


class _Http:
    def __init__(self, handler=None):
        self.handler = handler or (lambda *a: _Response(200, {"ok": True}))
        self.calls = []

    async def request(self, method, url, headers=None, json=None, follow_redirects=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "json": json})
        return self.handler(method, url, headers or {}, json)

    async def aclose(self):
        return None


def _adapter(http: _Http) -> NaoNaoAdapter:
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "bot_token": "nn_bot_test_token_phase3f_______",
            "api_base": "https://naonaoai.com",
        },
    )
    ad = NaoNaoAdapter(cfg, http_factory=lambda: http)
    ad._http = http
    ad._bot_id = "bot_1"
    ad._bot_name = "我的测试机器人"
    try:
        ad._asyncio_loop = asyncio.get_event_loop()
    except RuntimeError:
        ad._asyncio_loop = None
    return ad


def _source_event(ad: NaoNaoAdapter, message_id: str = "src_user_1") -> MessageEvent:
    source = ad.build_source(
        chat_id="community_a:channel_x",
        chat_name="ch",
        chat_type="channel",
        user_id="u1",
        user_name="member",
        message_id=message_id,
        scope_id="community_a",
    )
    return MessageEvent(
        text="@bot hi",
        message_type=MessageType.TEXT,
        user_id="u1",
        user_name="member",
        source=source,
        message_id=message_id,
    )


class Phase3FStatusRoutingTests(unittest.TestCase):
    def test_display_defaults_tool_progress_off_streaming_follows_global(self):
        _install_naonao_display_defaults()
        user_config = {}
        mode, explicit = resolve_tool_progress(user_config, "naonao")
        self.assertEqual(mode, "off")
        self.assertFalse(explicit)
        streaming = resolve_display_setting(user_config, "naonao", "streaming")
        # None / missing means follow top-level streaming.enabled — not Forced False.
        self.assertIn(streaming, (None, True))
        self.assertIsNot(streaming, False)

    def test_owner_progress_phrase_activity_only_never_message(self):
        """Exact Owner FAIL phrase → Activity search; Message POST count = 0."""
        http = _Http()
        ad = _adapter(http)
        chat = "community_a:channel_x"

        async def run():
            # Hermes live-status / status routing into adapter (not send).
            ad.set_status_text(chat, OWNER_PROGRESS_PHRASE)
            await ad._flush_pending_activity(chat)
            ad.set_status_text(chat, None)
            await ad._flush_pending_activity(chat)

        asyncio.run(run())

        activity = [c for c in http.calls if c["url"].endswith("/activity")]
        messages = [
            c for c in http.calls
            if c["method"] == "POST" and c["url"].endswith("/messages")
        ]
        self.assertGreaterEqual(len(activity), 1)
        on = next(c for c in activity if (c["json"] or {}).get("active"))
        self.assertEqual(on["json"]["category"], "search")
        self.assertEqual(set(on["json"].keys()), {"active", "category"})
        self.assertEqual(len(messages), 0)
        raw = str(http.calls)
        self.assertNotIn(OWNER_PROGRESS_PHRASE, raw)
        self.assertNotIn("AI news 2026-10-13", raw)
        self.assertNotIn("🔍", raw)
        self.assertTrue(any(not (c["json"] or {}).get("active") for c in activity))

    def test_set_status_text_never_calls_send(self):
        send_calls = []
        http = _Http()
        ad = _adapter(http)
        original_send = ad.send

        async def spy_send(*args, **kwargs):
            send_calls.append((args, kwargs))
            return await original_send(*args, **kwargs)

        ad.send = spy_send  # type: ignore[method-assign]

        async def run():
            ad.set_status_text("c:ch", OWNER_PROGRESS_PHRASE)
            await ad._flush_pending_activity("c:ch")

        asyncio.run(run())
        self.assertEqual(send_calls, [])
        self.assertEqual(
            len([c for c in http.calls if c["method"] == "POST" and c["url"].endswith("/messages")]),
            0,
        )

    def test_supports_status_text_true(self):
        self.assertIs(NaoNaoAdapter.supports_status_text, True)
        ad = _adapter(_Http())
        self.assertTrue(getattr(ad, "supports_status_text", False))


class Phase3FReactionCleanupTests(unittest.TestCase):
    def test_success_put_then_delete_eyes(self):
        http = _Http()
        ad = _adapter(http)
        event = _source_event(ad, "src_ok")

        async def run():
            await ad.on_processing_start(event)
            r = await ad.send("community_a:channel_x", "final answer")
            self.assertTrue(r.success)
            await ad.on_processing_complete(event, "SUCCESS")

        asyncio.run(run())
        puts = [
            c for c in http.calls
            if c["method"] == "PUT" and "/reactions/" in c["url"] and "src_ok" in c["url"]
        ]
        deletes = [
            c for c in http.calls
            if c["method"] == "DELETE" and "/reactions/" in c["url"] and "src_ok" in c["url"]
        ]
        self.assertEqual(len(puts), 1)
        self.assertEqual(len(deletes), 1)
        emoji = unquote(puts[0]["url"].rstrip("/").split("/")[-1])
        self.assertEqual(emoji, "👀")

    def test_tool_turn_activity_streaming_then_delete_eyes(self):
        order = []

        def handler(method, url, headers, body):
            if "/reactions/" in url:
                order.append((method, "reaction"))
                return _Response(200, {"ok": True})
            if url.endswith("/activity"):
                order.append(("ACTIVITY", bool((body or {}).get("active")), (body or {}).get("category")))
                return _Response(200, {"ok": True})
            if url.endswith("/typing"):
                order.append(("typing", bool((body or {}).get("active"))))
                return _Response(200, {"ok": True})
            if method == "POST" and url.endswith("/messages"):
                order.append(("POST", "messages"))
                return _Response(201, {"ok": True, "message": {"id": "bot_m1"}})
            if method == "PATCH" and "/messages/" in url:
                order.append(("PATCH", "messages"))
                return _Response(200, {"ok": True, "message": {"id": "bot_m1"}})
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)
        chat = "community_a:channel_x"
        event = _source_event(ad, "src_tool")

        async def run():
            await ad.on_processing_start(event)
            await ad.send_typing(chat)
            ad.set_status_text(chat, OWNER_PROGRESS_PHRASE)
            await ad._flush_pending_activity(chat)
            ad.set_status_text(chat, None)
            await ad._flush_pending_activity(chat)
            r = await ad.send(chat, "标题：新闻")
            self.assertTrue(r.success)
            await ad.edit_message(chat, "bot_m1", "标题：新闻\n摘要：…", finalize=True)
            await ad.on_processing_complete(event, "SUCCESS")

        asyncio.run(run())
        self.assertIn(("PUT", "reaction"), order)
        self.assertIn(("ACTIVITY", True, "search"), order)
        self.assertEqual(order.count(("POST", "messages")), 1)
        self.assertIn(("PATCH", "messages"), order)
        self.assertIn(("DELETE", "reaction"), order)
        # Raw progress phrase never in Message bodies
        msg_bodies = [
            c["json"] for c in http.calls
            if c["method"] in ("POST", "PATCH") and "/messages" in c["url"]
        ]
        self.assertNotIn(OWNER_PROGRESS_PHRASE, str(msg_bodies))

    def test_reaction_put_failure_response_continues(self):
        def handler(method, url, headers, body):
            if method == "PUT" and "/reactions/" in url:
                return _Response(500, {"ok": False})
            if method == "POST" and url.endswith("/messages"):
                return _Response(201, {"ok": True, "message": {"id": "m1"}})
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)
        event = _source_event(ad)

        async def run():
            await ad.on_processing_start(event)
            r = await ad.send("community_a:channel_x", "ok")
            self.assertTrue(r.success)
            await ad.on_processing_complete(event, "SUCCESS")

        asyncio.run(run())

    def test_reaction_delete_failure_final_remains_successful(self):
        def handler(method, url, headers, body):
            if method == "DELETE" and "/reactions/" in url:
                return _Response(500, {"ok": False})
            if method == "POST" and url.endswith("/messages"):
                return _Response(201, {"ok": True, "message": {"id": "m1"}})
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)
        event = _source_event(ad)

        async def run():
            await ad.on_processing_start(event)
            r = await ad.send("community_a:channel_x", "ok")
            self.assertTrue(r.success)
            await ad.on_processing_complete(event, "FAILURE")
            # Cleanup failure must not raise / undo send success.
            self.assertTrue(r.success)

        asyncio.run(run())

    def test_cleanup_idempotent_duplicate_complete(self):
        deletes = []

        def handler(method, url, headers, body):
            if method == "DELETE" and "/reactions/" in url:
                deletes.append(url)
                # Second call: already gone
                return _Response(404 if len(deletes) > 1 else 200, {"ok": True})
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)
        event = _source_event(ad, "src_dup")

        async def run():
            await ad.on_processing_complete(event, "SUCCESS")
            await ad.on_processing_complete(event, "SUCCESS")

        asyncio.run(run())
        self.assertEqual(len(deletes), 2)

    def test_delete_targets_stable_source_message_id_not_last_bot(self):
        urls = []

        def handler(method, url, headers, body):
            if "/reactions/" in url:
                urls.append(method + " " + url)
            if method == "POST" and url.endswith("/messages"):
                return _Response(201, {"ok": True, "message": {"id": "bot_last"}})
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)
        event = _source_event(ad, "stable_src_99")

        async def run():
            await ad.on_processing_start(event)
            await ad.send("community_a:channel_x", "answer")
            await ad.on_processing_complete(event, "CANCELLED")

        asyncio.run(run())
        self.assertTrue(any("stable_src_99" in u for u in urls if u.startswith("DELETE ")))
        self.assertFalse(any("bot_last" in u and u.startswith("DELETE ") for u in urls))


if __name__ == "__main__":
    unittest.main()
