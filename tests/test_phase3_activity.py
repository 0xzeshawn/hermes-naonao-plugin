"""Phase 3: Agent Activity State for NaoNao Hermes adapter."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or (Path.home() / ".hermes" / "hermes-agent"))
PLUGIN_DIR = Path(__file__).resolve().parents[1]

if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

if not (HERMES_ROOT / "gateway" / "platforms" / "base.py").is_file():
    raise unittest.SkipTest(f"Hermes source not found at {HERMES_ROOT}")


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "naonao_platform_adapter_p3", PLUGIN_DIR / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
NaoNaoAdapter = adapter_mod.NaoNaoAdapter
register = adapter_mod.register
map_tool_to_activity_category = adapter_mod.map_tool_to_activity_category

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


register(_Ctx())


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
            "bot_token": "nn_bot_test_token_phase3________",
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


class Phase3ActivityTests(unittest.TestCase):
    def test_supports_status_text(self):
        self.assertIs(NaoNaoAdapter.supports_status_text, True)

    def test_tool_mapping_safe(self):
        self.assertEqual(map_tool_to_activity_category("web_search"), "search")
        self.assertEqual(map_tool_to_activity_category("browser_navigate"), "browser")
        self.assertEqual(map_tool_to_activity_category("read_file"), "file")
        self.assertEqual(map_tool_to_activity_category("terminal"), "code")
        self.assertEqual(map_tool_to_activity_category("tool_call"), "external")
        self.assertEqual(map_tool_to_activity_category("mcp__foo__bar"), "external")
        self.assertEqual(map_tool_to_activity_category("totally_unknown_xyz"), "processing")
        self.assertEqual(map_tool_to_activity_category("_thinking"), "processing")

    def test_no_tool_does_not_post_activity(self):
        http = _Http()
        ad = _adapter(http)

        async def run():
            r = await ad.send("c:ch", "你好，我是测试机器人。")
            self.assertTrue(r.success)

        asyncio.run(run())
        activity_calls = [c for c in http.calls if c["url"].endswith("/activity")]
        # send() clears activity (active:false) — OK; must not claim search
        for c in activity_calls:
            body = c["json"] or {}
            self.assertNotEqual(body.get("category"), "search")
            if body.get("active"):
                self.fail("no-tool turn must not activate search/tool activity")

    def test_search_tool_lifecycle_order(self):
        order = []

        def handler(method, url, headers, body):
            if url.endswith("/typing"):
                order.append(("typing", bool((body or {}).get("active"))))
                return _Response(200, {"ok": True})
            if url.endswith("/activity"):
                order.append(("activity", bool((body or {}).get("active")), (body or {}).get("category")))
                return _Response(200, {"ok": True})
            if "/reactions/" in url:
                order.append(("reaction",))
                return _Response(200, {"ok": True})
            if method == "POST" and url.endswith("/messages"):
                order.append(("send",))
                return _Response(201, {"ok": True, "message": {"id": "m1"}})
            if method == "PATCH":
                order.append(("patch",))
                return _Response(200, {"ok": True, "message": {"id": "m1"}})
            return _Response(404)

        http = _Http(handler)
        ad = _adapter(http)
        chat = "community_a:channel_x"

        async def run():
            await ad.send_typing(chat)
            ad.set_activity_for_tool(chat, "web_search")
            await ad._flush_pending_activity(chat)
            ad.clear_activity(chat)
            await ad._flush_pending_activity(chat)
            r = await ad.send(chat, "标题：…")
            self.assertTrue(r.success)
            await ad.edit_message(chat, "m1", "标题：完整", finalize=True)

        asyncio.run(run())
        self.assertIn(("activity", True, "search"), order)
        self.assertTrue(any(c[0] == "activity" and c[1] is False for c in order))
        self.assertIn(("send",), order)

    def test_status_phrase_never_forwarded(self):
        """Secret-laden Hermes phrase must not appear in Activity HTTP body."""
        secret = "https://evil.example/x?token=SECRET_KEY&path=/Users/me/.ssh/id_rsa"
        bodies = []

        def handler(method, url, headers, body):
            if url.endswith("/activity"):
                bodies.append(body)
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)
        chat = "c:ch"

        async def run():
            ad.set_status_text(chat, f"is searching for {secret}…")
            await ad._flush_pending_activity(chat)

        asyncio.run(run())
        self.assertTrue(bodies)
        raw = str(bodies)
        self.assertNotIn("SECRET_KEY", raw)
        self.assertNotIn("/Users/me", raw)
        self.assertNotIn("evil.example", raw)
        self.assertNotIn("token=", raw)
        # Category is allowlisted only; phrase content never forwarded.
        self.assertIn(bodies[0].get("category"), adapter_mod.ACTIVITY_CATEGORIES)
        self.assertNotIn("text", bodies[0] or {})
        self.assertEqual(set(bodies[0].keys()), {"active", "category"})

    def test_unknown_tool_is_processing(self):
        http = _Http()
        ad = _adapter(http)

        async def run():
            ad.set_activity_for_tool("c:ch", "weird_custom_tool_99")
            await ad._flush_pending_activity("c:ch")

        asyncio.run(run())
        act = [c for c in http.calls if c["url"].endswith("/activity")][0]
        self.assertEqual(act["json"]["category"], "processing")
        self.assertNotIn("weird_custom_tool_99", str(act["json"]))

    def test_activity_api_failure_does_not_block_send(self):
        def handler(method, url, headers, body):
            if url.endswith("/activity"):
                return _Response(500, {"ok": False})
            if url.endswith("/messages"):
                return _Response(201, {"ok": True, "message": {"id": "m1"}})
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)

        async def run():
            ad.set_activity_for_tool("c:ch", "web_search")
            await ad._flush_pending_activity("c:ch")
            r = await ad.send("c:ch", "ok")
            self.assertTrue(r.success)

        asyncio.run(run())

    def test_multiple_tools_replace_category(self):
        cats = []

        def handler(method, url, headers, body):
            if url.endswith("/activity") and (body or {}).get("active"):
                cats.append((body or {}).get("category"))
            return _Response(200, {"ok": True})

        http = _Http(handler)
        ad = _adapter(http)

        async def run():
            ad.set_activity_for_tool("c:ch", "web_search")
            await ad._flush_pending_activity("c:ch")
            ad.set_activity_for_tool("c:ch", "browser_navigate")
            await ad._flush_pending_activity("c:ch")

        asyncio.run(run())
        self.assertEqual(cats, ["search", "browser"])


if __name__ == "__main__":
    unittest.main()
