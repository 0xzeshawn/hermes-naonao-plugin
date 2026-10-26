"""Hermes-compatible tests for the NaoNao platform plugin.

Run from this plugin directory with the Hermes checkout on PYTHONPATH:

    HERMES_AGENT_ROOT=/path/to/hermes-agent python -m unittest tests.test_adapter
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or (Path.home() / ".hermes" / "hermes-agent"))
PLUGIN_DIR = Path(__file__).resolve().parents[1]

if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

if not (HERMES_ROOT / "gateway" / "platforms" / "base.py").is_file():
    raise unittest.SkipTest(f"Hermes source not found at {HERMES_ROOT}")


def _load_adapter():
    spec = importlib.util.spec_from_file_location("naonao_platform_adapter", PLUGIN_DIR / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
NaoNaoAdapter = adapter_mod.NaoNaoAdapter
register = adapter_mod.register

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.session import build_session_key


class _Ctx:
    def __init__(self):
        self.kwargs = {}

    def register_platform(self, **kwargs):
        self.kwargs = kwargs
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
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers or {},
            "json": json,
            "follow_redirects": follow_redirects,
        })
        return self.handler(method, url, headers or {}, json)

    async def aclose(self):
        return None


class _Socket:
    def __init__(self, frames, close_code=1000, hold=False):
        self.frames = list(frames)
        self.sent = []
        self.close_code = None
        self._terminal = close_code
        self.hold = hold
        self._closed = asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.frames:
            if self.hold:
                await self._closed.wait()
            self.close_code = self._terminal
            return None
        item = self.frames.pop(0)
        if isinstance(item, str):
            return item
        return json.dumps(item)

    async def close(self):
        self.close_code = self.close_code or self._terminal or 1000
        self._closed.set()


def _mention(event_id="evt_1", community="community_a", channel="channel_x", content="@测试机器人 你好", bot_id="bot_1", actor_id="", reply=None, reply_to_message_id=""):
    message = {
        "id": "msg_1",
        "content": content,
        "createdAt": "2026-10-13T04:00:00.000Z",
        "hasAttachments": False,
        "attachmentCount": 0,
        "replyToMessageId": reply_to_message_id or "",
    }
    if reply is not None:
        message["reply"] = reply
        if not message["replyToMessageId"]:
            message["replyToMessageId"] = str(reply.get("messageId") or "")
    return {
        "version": "1",
        "id": event_id,
        "type": "message.bot_mention",
        "createdAt": "2026-10-13T04:00:00.000Z",
        "application": {"id": "app_1"},
        "bot": {"id": bot_id, "name": "测试机器人"},
        "community": {"id": community},
        "channel": {"id": channel},
        "actor": {"type": "user", "username": "member", **({"id": actor_id} if actor_id else {})},
        "message": message,
        "installation": {"id": "inst_1"},
    }


def _command(event_id="evt_cmd", name="sethome", community="community_a", channel="channel_x", bot_id="bot_1", actor_id="507f1f77bcf86cd799439011", username="member"):
    actor = {"type": "user", "username": username}
    if actor_id:
        actor["id"] = actor_id
    return {
        "version": "1",
        "id": event_id,
        "type": "command.invoked",
        "createdAt": "2026-10-13T04:00:00.000Z",
        "application": {"id": "app_1"},
        "bot": {"id": bot_id, "name": "测试机器人"},
        "community": {"id": community},
        "channel": {"id": channel},
        "actor": actor,
        "installation": {"id": "inst_1"},
        "invocation": {
            "id": "inv_1",
            "command": {"id": "cmd_1", "name": name, "version": 1},
            "options": {},
        },
    }


def _ok_api(owner="simple_gateway", reply_status=201, reply_err=""):
    def handler(method, url, headers, body):
        if url.endswith("/api/v1/bot/me"):
            if headers.get("Authorization") != "Bot nn_bot_example":
                return _Response(401, {"ok": False, "err": "invalid_bot_token"})
            return _Response(200, {"ok": True, "bot": {"id": "bot_1", "name": "测试机器人"}, "application": {"id": "app_1", "name": "App"}})
        if url.endswith("/api/v1/bot/gateway-session"):
            return _Response(201, {
                "ok": True,
                "token": "nn_bgs_example",
                "expiresAt": "2099-01-01T00:00:00+00:00",
                "gatewayPath": "/gateway/v1",
                "autoReplyOwner": owner,
            })
        if url.endswith("/messages"):
            return _Response(reply_status, {"ok": reply_status < 400, "err": reply_err, "message": {"id": "sent_1"}, "replay": reply_status == 200})
        return _Response(404, {"ok": False, "err": "resource_not_found"})
    return handler


class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = _Ctx()
        register(cls.ctx)

    def test_manifest_and_registration(self):
        text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("name: naonao", text)
        self.assertIn("kind: platform", text)
        self.assertIn("NAONAO_BOT_TOKEN", text)
        self.assertIn("password: true", text)
        self.assertEqual(self.ctx.kwargs["name"], "naonao")
        self.assertEqual(self.ctx.kwargs["required_env"], ["NAONAO_BOT_TOKEN"])
        self.assertEqual(self.ctx.kwargs["allowed_users_env"], "NAONAO_ALLOWED_USERS")
        self.assertEqual(self.ctx.kwargs["allow_all_env"], "NAONAO_ALLOW_ALL_USERS")
        self.assertEqual(self.ctx.kwargs["cron_deliver_env_var"], "NAONAO_HOME_CHANNEL")
        self.assertTrue(callable(self.ctx.kwargs["standalone_sender_fn"]))
        entry = platform_registry.get("naonao")
        self.assertEqual(entry.allowed_users_env, "NAONAO_ALLOWED_USERS")
        self.assertEqual(entry.allow_all_env, "NAONAO_ALLOW_ALL_USERS")
        self.assertEqual(entry.cron_deliver_env_var, "NAONAO_HOME_CHANNEL")
        self.assertIn("NAONAO_API_BASE", text)
        self.assertIn("websockets>=13,<16", text)
        self.assertIn("python-socks>=2,<4", text)
        self.assertFalse(adapter_mod.validate_config(PlatformConfig()))
        self.assertTrue(Platform("naonao") is Platform("naonao"))
        created = self.ctx.kwargs["adapter_factory"](PlatformConfig(extra={"bot_token": "nn_bot_example"}))
        self.assertIsInstance(created, NaoNaoAdapter)

    def test_missing_token_is_fatal(self):
        adapter = NaoNaoAdapter(PlatformConfig())

        async def run():
            return await adapter.connect()

        self.assertFalse(asyncio.run(run()))
        self.assertEqual(adapter._fatal_error_code, "missing_bot_token")

    def test_passive_probe_succeeds_when_dependencies_import(self):
        self.assertTrue(adapter_mod.check_naonao_requirements())

    def test_passive_probe_fails_when_a_dependency_is_missing(self):
        original = adapter_mod._importable

        def missing_socks(name):
            if name == "python_socks":
                return False
            return original(name)

        with unittest.mock.patch.object(adapter_mod, "_importable", missing_socks):
            self.assertFalse(adapter_mod.check_naonao_requirements())

    def test_passive_probe_does_not_install(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("passive probe must not sync or spawn a process")

        with unittest.mock.patch.object(adapter_mod, "_importable", lambda _name: False), \
                unittest.mock.patch.object(adapter_mod, "_sync_runtime_dependencies", explode), \
                unittest.mock.patch("subprocess.run", explode), \
                unittest.mock.patch("subprocess.Popen", explode):
            self.assertFalse(adapter_mod.check_naonao_requirements())

    def test_ensure_syncs_through_hermes_when_dependency_missing(self):
        calls = []
        seen = {"python_socks": False}

        def importable(name):
            if name == "python_socks":
                return seen["python_socks"]
            return True

        def sync():
            calls.append("sync")
            seen["python_socks"] = True

        with unittest.mock.patch.object(adapter_mod, "_importable", importable), \
                unittest.mock.patch.object(adapter_mod, "_sync_runtime_dependencies", sync):
            self.assertTrue(adapter_mod.ensure_naonao_requirements())
        self.assertEqual(calls, ["sync"])

    def test_ensure_reports_install_failure_without_secrets(self):
        token = "nn_bot_example_secret"
        with unittest.mock.patch.object(adapter_mod, "_importable", lambda _name: False), \
                unittest.mock.patch.object(
                    adapter_mod, "_sync_runtime_dependencies",
                    side_effect=RuntimeError(f"resolver failed near {token}"),
                ):
            with self.assertRaises(adapter_mod.NaoNaoDependencyError) as caught:
                adapter_mod.ensure_naonao_requirements()
        message = str(caught.exception)
        self.assertIn("python-socks", message)
        self.assertNotIn(token, message)
        self.assertIn("[redacted]", message)

    def test_platform_registration_hooks_match_hermes_create_adapter(self):
        self.assertIs(self.ctx.kwargs["check_fn"], adapter_mod.check_naonao_requirements)
        self.assertIs(self.ctx.kwargs["ensure_deps_fn"], adapter_mod.ensure_naonao_requirements)
        entry = platform_registry.get("naonao")
        self.assertIs(entry.check_fn, adapter_mod.check_naonao_requirements)
        self.assertIs(entry.ensure_deps_fn, adapter_mod.ensure_naonao_requirements)
        calls = []
        original_check, original_ensure = entry.check_fn, entry.ensure_deps_fn
        entry.check_fn = lambda: calls.append("check") or False
        entry.ensure_deps_fn = lambda: calls.append("ensure") or True
        try:
            created = platform_registry.create_adapter(
                "naonao", PlatformConfig(extra={"bot_token": "nn_bot_example"}),
            )
        finally:
            entry.check_fn, entry.ensure_deps_fn = original_check, original_ensure
        self.assertEqual(calls, ["check", "ensure"])
        self.assertIsInstance(created, NaoNaoAdapter)

    def test_missing_socks_fails_before_websocket(self):
        """The Manual E2E failure: websockets is present, python-socks is not, a SOCKS proxy is selected."""
        original = adapter_mod._importable

        def websockets_without_socks(name):
            if name == "python_socks":
                return False
            return original(name)

        with unittest.mock.patch.object(adapter_mod, "_importable", websockets_without_socks):
            self.assertFalse(adapter_mod.check_naonao_requirements())
            with unittest.mock.patch.object(
                adapter_mod, "_sync_runtime_dependencies",
                side_effect=RuntimeError("python-socks is required to use a SOCKS proxy"),
            ):
                with self.assertRaises(adapter_mod.NaoNaoDependencyError) as caught:
                    adapter_mod.ensure_naonao_requirements()
        self.assertIn("SOCKS", str(caught.exception))
        self.assertNotIn("nn_bgs_", str(caught.exception))

    def test_user_docs_match_the_env_contract(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        example = (PLUGIN_DIR / "examples" / "config.yaml.example").read_text(encoding="utf-8")
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        source = (PLUGIN_DIR / "adapter.py").read_text(encoding="utf-8")
        for name in (
            "NAONAO_BOT_TOKEN",
            "NAONAO_API_BASE",
            "NAONAO_ALLOWED_USERS",
            "NAONAO_ALLOW_ALL_USERS",
        ):
            self.assertIn(name, readme)
            self.assertIn(name, manifest)
            self.assertIn(name, source)
        self.assertIn("NAONAO_HOME_CHANNEL", readme)
        self.assertIn("Do not set `NAONAO_HOME_CHANNEL`", readme)
        self.assertIn("Do not set NAONAO_HOME_CHANNEL", example)
        self.assertNotIn("NAONAO_HOME_CHANNEL:", example)
        self.assertIn("https://naonaoai.com", readme)
        self.assertIn("https://naonaoai.com", example)
        self.assertIn("https://naonaoai.com", manifest)
        self.assertIn('DEFAULT_API_BASE = "https://naonaoai.com"', source)
        self.assertIn("platforms/naonao", readme)
        self.assertIn("hermes gateway start", readme)
        self.assertIn("hermes gateway status", readme)
        self.assertNotIn("/Users/", readme)
        self.assertNotIn("Step 6", readme)
        self.assertNotIn("bot_id", example)
        self.assertNotIn("gateway_url", example)
        for secret in ("nn_bot_", "nn_bgs_", "nn_gwr_"):
            self.assertNotIn(secret + "live", readme)
        self.assertIn("nn_bot_xxxxxxxxxxxxxxxxx", readme)

    def test_fresh_hermes_home_discovers_the_platform(self):
        import shutil
        import tempfile

        from hermes_cli import plugins_discovery

        home = Path(tempfile.mkdtemp(prefix="naonao-hermes-home-"))
        try:
            dest = home / "plugins" / "platforms" / "naonao"
            dest.mkdir(parents=True)
            shutil.copy(PLUGIN_DIR / "plugin.yaml", dest / "plugin.yaml")
            with unittest.mock.patch.object(plugins_discovery, "get_hermes_home", lambda: home):
                manifests = plugins_discovery.collect_directory_manifests()
            found = [item for item in manifests if item.name == "naonao" and item.source == "user"]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].kind, "platform")
            self.assertEqual(plugins_discovery.manifest_key(found[0]), "platforms/naonao")
            declared = {entry["name"] for entry in (found[0].requires_env or []) if isinstance(entry, dict)}
            self.assertEqual(declared, {"NAONAO_BOT_TOKEN"})
            self.assertIn("websockets>=13,<16", found[0].python_dependencies)
            self.assertIn("python-socks>=2,<4", found[0].python_dependencies)
            copied = (dest / "plugin.yaml").read_text(encoding="utf-8")
            self.assertIn("NAONAO_API_BASE", copied)
            self.assertFalse(adapter_mod.validate_config(PlatformConfig()))
        finally:
            shutil.rmtree(home, ignore_errors=True)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not platform_registry.is_registered("naonao"):
            register(_Ctx())

    def _adapter(self, handler, sockets):
        def ws_connect(_url):
            sock = sockets.pop(0)
            class _CM:
                async def __aenter__(self_nonlocal):
                    return sock

                async def __aexit__(self_nonlocal, exc_type, exc, tb):
                    return False
            return _CM()

        http = _Http(handler)
        adapter = NaoNaoAdapter(
            PlatformConfig(extra={"bot_token": "nn_bot_example", "api_base": "https://naonaoai.com"}),
            http_factory=lambda: http,
            ws_connect=ws_connect,
        )

        async def background(event, session_key):
            adapter._seen.append((event, session_key))

        async def handler(_event):
            return None

        adapter._message_handler = handler
        adapter._process_message_background = background
        adapter._seen = []
        adapter._http_box = http
        return adapter

    def test_build_mention_event_maps_structured_reply_context(self):
        adapter = self._adapter(_ok_api(), [])
        adapter._bot_id = "bot_1"
        event = adapter.build_mention_event(
            _mention(
                content="@测试机器人 对，就是这个。",
                reply_to_message_id="parent_1",
                reply={
                    "messageId": "parent_1",
                    "authorName": "alice",
                    "authorId": "user_alice",
                    "authorType": "user",
                    "excerpt": "数据库是不是 MongoDB？",
                    "deleted": False,
                    "removed": False,
                },
            ),
            3,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.reply_to_message_id, "parent_1")
        self.assertIn("MongoDB", event.reply_to_text or "")
        self.assertEqual(event.reply_to_author_name, "alice")
        self.assertEqual(event.reply_to_author_id, "user_alice")
        self.assertFalse(event.reply_to_is_own_message)
        # Reply must not be stuffed into metadata (admission / PR #1 concern).
        self.assertNotIn("reply", event.metadata)
        self.assertNotIn("replyToMessageId", event.metadata)

        deleted = adapter.build_mention_event(
            _mention(
                content="@测试机器人 ok",
                reply_to_message_id="gone_1",
                reply={
                    "messageId": "gone_1",
                    "authorName": "",
                    "authorId": "",
                    "authorType": "user",
                    "excerpt": "",
                    "deleted": True,
                    "removed": False,
                },
            ),
            4,
        )
        self.assertEqual(deleted.reply_to_message_id, "gone_1")
        self.assertEqual(deleted.reply_to_text, "(original message unavailable)")

        plain = adapter.build_mention_event(_mention(content="@测试机器人 你好"), 5)
        self.assertIsNone(plain.reply_to_message_id)
        self.assertIsNone(plain.reply_to_text)

    async def test_identify_reply_ack_and_dedupe(self):
        sock = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "heartbeatIntervalMs": 30000, "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 7, "t": "message.bot_mention", "d": _mention()},
            {"op": "EVENT", "v": 1, "s": 8, "t": "message.bot_mention", "d": _mention()},
        ])
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: any(frame.get("op") == "ACK" and frame.get("d", {}).get("sequence") == 8 for frame in sock.sent)), 2)
        identify = next(frame for frame in sock.sent if frame["op"] == "IDENTIFY")
        self.assertEqual(identify["d"]["token"], "nn_bgs_example")
        self.assertFalse(identify["d"]["token"].startswith("nn_bot_"))
        self.assertEqual(len(adapter._seen), 1)
        event = adapter._seen[0][0]
        self.assertEqual(event.metadata, {})
        self.assertEqual(adapter._event_for_chat[event.source.chat_id], "evt_1")
        self.assertEqual(event.message_id, "msg_1")
        self.assertIsNone(event.user_id)
        self.assertEqual(event.user_name, "member")
        self.assertEqual(event.text, "你好")
        reply = await adapter.send(event.source.chat_id, "Hermes reply")
        self.assertTrue(reply.success)
        second = await adapter.send(event.source.chat_id, "Hermes reply again")
        self.assertTrue(second.success)
        keys = [call["headers"].get("Idempotency-Key") for call in adapter._http_box.calls if call["url"].endswith("/messages")]
        self.assertEqual(keys, ["evt_1:1", "evt_1:2"])
        self.assertTrue(all(call["follow_redirects"] is False for call in adapter._http_box.calls))
        joined = "\n".join(str(call["headers"]) for call in adapter._http_box.calls)
        self.assertNotIn("nn_bgs_example", "\n".join(rec.getMessage() for rec in []))
        await adapter.disconnect()
        self.assertIsNone(adapter._loop_task)

    async def test_owner_agent_does_not_connect_gateway(self):
        sockets = []
        adapter = self._adapter(_ok_api(owner="agent"), sockets)
        self.assertFalse(await adapter.connect())
        self.assertEqual(adapter._fatal_error_code, "not_assigned")
        self.assertEqual(sockets, [])

    async def test_owner_none_and_invalid_token(self):
        none_adapter = self._adapter(_ok_api(owner="none"), [])
        self.assertFalse(await none_adapter.connect())
        self.assertEqual(none_adapter._fatal_error_code, "not_assigned")

        def invalid(method, url, headers, body):
            return _Response(401, {"ok": False, "err": "invalid_bot_token"})

        bad = self._adapter(invalid, [])
        self.assertFalse(await bad.connect())
        self.assertEqual(bad._fatal_error_code, "invalid_bot_token")
        self.assertFalse(bad._fatal_error_retryable)

    async def test_bootstrap_429_then_success(self):
        state = {"hits": 0}

        def handler(method, url, headers, body):
            if url.endswith("/api/v1/bot/me"):
                return _Response(200, {"ok": True, "bot": {"id": "bot_1", "name": "测试机器人"}, "application": {"id": "app_1"}})
            state["hits"] += 1
            if state["hits"] == 1:
                return _Response(429, {"ok": False, "err": "rate_limited", "retryAfterSec": 0})
            return _ok_api()(method, url, headers, body)

        sock = _Socket([{"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}}])
        adapter = self._adapter(handler, [sock])
        self.assertTrue(await adapter.connect())
        self.assertGreaterEqual(state["hits"], 2)
        await adapter.disconnect()

    async def test_resume_after_reconnect(self):
        first = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "heartbeatIntervalMs": 30000, "startSequence": 3}},
        ], close_code=4009)
        second = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "RESUMED", "v": 1, "d": {"sessionId": "sess_1", "lastAckedSequence": 3, "heartbeatIntervalMs": 30000}},
        ])
        adapter = self._adapter(_ok_api(), [first, second])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: any(frame.get("op") == "RESUME" for frame in second.sent)), 2)
        resume = next(frame for frame in second.sent if frame["op"] == "RESUME")
        self.assertEqual(resume["d"]["sessionId"], "sess_1")
        self.assertEqual(resume["d"]["token"], "nn_bgs_example")
        self.assertEqual(resume["d"]["sequence"], 3)
        await adapter.disconnect()

    async def test_reply_errors_and_unsupported_media(self):
        adapter = self._adapter(_ok_api(reply_status=403, reply_err="insufficient_permissions"), [])
        adapter._http = _Http(_ok_api(reply_status=403, reply_err="insufficient_permissions"))
        adapter._event_for_chat["community_a:channel_x"] = "evt_9"
        denied = await adapter.send("community_a:channel_x", "hello")
        self.assertFalse(denied.success)
        self.assertEqual(denied.error_kind, "forbidden")
        adapter._http = _Http(_ok_api(reply_status=409, reply_err="idempotency_conflict"))
        conflict = await adapter.send("community_a:channel_x", "other")
        self.assertEqual(conflict.error, "idempotency_conflict")
        adapter._http = _Http(_ok_api(reply_status=429, reply_err="rate_limited"))
        limited = await adapter.send("community_a:channel_x", "again")
        self.assertTrue(limited.retryable)
        self.assertEqual(limited.error_kind, "rate_limited")
        image = await adapter.send_image("community_a:channel_x", "https://example.com/a.png")
        self.assertFalse(image.success)
        document = await adapter.send_document("community_a:channel_x", "/tmp/a.pdf")
        self.assertFalse(document.success)

    async def test_malformed_and_foreign_bot_do_not_dispatch(self):
        frames = [
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 1, "t": "message.bot_mention", "d": {"id": "evt_bad"}},
            {"op": "EVENT", "v": 1, "s": 2, "t": "message.bot_mention", "d": _mention(event_id="evt_other", bot_id="bot_other")},
        ]
        sock = _Socket(frames)
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: sum(1 for frame in sock.sent if frame.get("op") == "ACK") >= 2), 2)
        self.assertEqual(adapter._seen, [])
        await adapter.disconnect()

    async def test_heartbeat_uses_hello_interval(self):
        sock = _Socket([{"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 20}}], hold=True)
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: any(frame.get("op") == "HEARTBEAT" for frame in sock.sent)), 1)
        await adapter.disconnect()

    async def test_session_isolation_and_no_username_as_user_id(self):
        adapter = self._adapter(_ok_api(), [])
        adapter._bot_id = "bot_1"
        adapter._bot_name = "测试机器人"
        first = adapter.build_mention_event(_mention(community="community_a"), 1)
        second = adapter.build_mention_event(_mention(community="community_b", event_id="evt_2"), 2)
        self.assertNotEqual(build_session_key(first.source), build_session_key(second.source))
        self.assertIsNone(first.source.user_id)
        self.assertEqual(first.source.user_name, "member")
        named = adapter.build_mention_event(_mention(actor_id="member"), 3)
        self.assertIsNone(named.source.user_id)
        stable = adapter.build_mention_event(_mention(actor_id="507f1f77bcf86cd799439011"), 4)
        self.assertEqual(stable.source.user_id, "507f1f77bcf86cd799439011")
        self.assertEqual(stable.user_id, "507f1f77bcf86cd799439011")
        self.assertEqual(stable.user_name, "member")
        other = adapter.build_mention_event(
            _mention(actor_id="507f1f77bcf86cd799439012", event_id="evt_3"), 5,
        )
        self.assertEqual(build_session_key(stable.source, group_sessions_per_user=False), build_session_key(other.source, group_sessions_per_user=False))
        self.assertFalse(adapter.config.extra["group_sessions_per_user"])
        self.assertIn("community_a", first.source.chat_id)
        self.assertIn("community_b", second.source.chat_id)
        self.assertEqual(first.metadata, {})

    async def test_diagnostic_logs_keep_event_and_ack_behavior(self):
        frames = [
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 3, "t": "message.created", "d": {"id": "evt_plain"}},
            {"op": "EVENT", "v": 1, "s": 4, "t": "message.bot_mention", "d": _mention(content="@测试机器人 你好，你是谁？")},
            {"op": "EVENT", "v": 1, "s": 5, "t": "message.bot_mention", "d": _mention(content="@测试机器人 你好，你是谁？")},
            {"op": "EVENT", "v": 1, "s": 6, "t": "message.bot_mention", "d": {"id": "evt_bad"}},
        ]
        sock = _Socket(frames)
        adapter = self._adapter(_ok_api(), [sock])
        with self.assertLogs("naonao_platform_adapter", level="INFO") as captured:
            self.assertTrue(await adapter.connect())
            await asyncio.wait_for(self._until(lambda: sum(1 for frame in sock.sent if frame.get("op") == "ACK") >= 4), 2)
            reply = await adapter.send(adapter._seen[0][0].source.chat_id, "Hermes reply", metadata={"naonao_event_id": "evt_1"})
        self.assertTrue(reply.success)
        self.assertEqual(len(adapter._seen), 1)
        acked = [frame["d"]["sequence"] for frame in sock.sent if frame.get("op") == "ACK"]
        self.assertEqual(acked, [3, 4, 5, 6])
        text = "\n".join(captured.output)
        self.assertIn("inbound event type=message.bot_mention sequence=4", text)
        self.assertIn("ignoring event type=message.created sequence=3", text)
        self.assertIn("duplicate event ignored event_id=evt_1 sequence=5", text)
        self.assertIn("mention accepted event_id=evt_1 chat_id=community_a:channel_x message_id=msg_1", text)
        self.assertIn("submitting event to Hermes event_id=evt_1 chat_id=community_a:channel_x", text)
        self.assertIn("Hermes admission result event_id=evt_1 accepted=true", text)
        self.assertIn("acking event event_id=evt_1 sequence=4", text)
        self.assertIn("skipping malformed or foreign mention event_id=evt_bad sequence=6", text)
        self.assertIn("outbound send start chat_id=community_a:channel_x event_id=evt_1", text)
        self.assertIn("outbound send success chat_id=community_a:channel_x message_id=sent_1", text)
        self.assertNotIn("nn_bot_example", text)
        self.assertNotIn("nn_bgs_example", text)
        self.assertNotIn("nn_gwr_example", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("你好，你是谁？", text)
        self.assertNotIn("Hermes reply", text)
        await adapter.disconnect()

    async def test_unaccepted_event_is_not_acked(self):
        sock = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 9, "t": "message.bot_mention", "d": _mention(event_id="evt_reject")},
        ])
        adapter = self._adapter(_ok_api(), [sock])

        async def reject(event):
            event._gateway_accepted = False

        adapter.handle_message = reject
        with self.assertLogs("naonao_platform_adapter", level="INFO") as captured:
            self.assertTrue(await adapter.connect())
            await asyncio.wait_for(self._until(lambda: any("accepted=false" in line for line in captured.output)), 2)
            await asyncio.sleep(0.05)
        text = "\n".join(captured.output)
        self.assertIn("event not accepted by Hermes event_id=evt_reject sequence=9", text)
        self.assertFalse(any(frame.get("op") == "ACK" for frame in sock.sent))
        self.assertNotIn("nn_bot_example", text)
        await adapter.disconnect()

    async def test_outbound_failure_log_redacts_secrets(self):
        adapter = self._adapter(_ok_api(reply_status=403, reply_err="nn_bot_example leaked"), [])
        adapter._http = _Http(_ok_api(reply_status=403, reply_err="nn_bot_example leaked"))
        with self.assertLogs("naonao_platform_adapter", level="INFO") as captured:
            denied = await adapter.send("community_a:channel_x", "secret reply body", metadata={"naonao_event_id": "evt_9"})
        self.assertFalse(denied.success)
        self.assertEqual(denied.error_kind, "forbidden")
        text = "\n".join(captured.output)
        self.assertIn("outbound send failed chat_id=community_a:channel_x status=403 error=", text)
        self.assertNotIn("nn_bot_example", text)
        self.assertNotIn("secret reply body", text)
        self.assertIn("[redacted]", text)

    def test_sethome_command_becomes_hermes_text_command(self):
        adapter = NaoNaoAdapter(PlatformConfig(extra={"bot_token": "nn_bot_example"}))
        adapter._bot_id = "bot_1"
        event = adapter.build_command_event(_command(), 11)
        self.assertEqual(event.text, "/sethome")
        self.assertEqual(event.user_id, "507f1f77bcf86cd799439011")
        self.assertEqual(event.user_name, "member")
        self.assertEqual(event.source.chat_id, "community_a:channel_x")
        self.assertEqual(event.source.chat_type, "channel")
        self.assertEqual(event.message_id, "inv_1")
        self.assertEqual(event.metadata, {})
        self.assertTrue(event.is_command())
        self.assertEqual(event.get_command(), "sethome")
        unnamed = adapter.build_command_event(_command(actor_id="member", username="member", event_id="evt_name"), 12)
        self.assertIsNone(unnamed.user_id)
        foreign = adapter.build_command_event(_command(bot_id="bot_other"), 13)
        self.assertIsNone(foreign)
        self.assertIsNone(adapter.build_command_event(_command(name="Set Home"), 14))

    async def test_command_event_is_submitted_and_plain_text_is_not(self):
        frames = [
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 1, "t": "message.created", "d": {"id": "evt_plain", "message": {"content": "今天吃什么"}}},
            {"op": "EVENT", "v": 1, "s": 2, "t": "command.invoked", "d": _command()},
        ]
        sock = _Socket(frames)
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: len(adapter._seen) >= 1), 2)
        texts = [seen.text for seen, _seq in adapter._seen]
        self.assertEqual(texts, ["/sethome"])
        self.assertEqual(adapter._seen[0][0].source.chat_type, "channel")
        self.assertEqual(adapter._seen[0][0].user_id, "507f1f77bcf86cd799439011")
        acked = [frame["d"]["sequence"] for frame in sock.sent if frame.get("op") == "ACK"]
        self.assertIn(1, acked)
        await adapter.disconnect()

    async def test_replayed_command_event_id_is_deduped(self):
        payload = _command(event_id="evt_same")
        frames = [
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 4, "t": "command.invoked", "d": payload},
            {"op": "EVENT", "v": 1, "s": 5, "t": "command.invoked", "d": payload},
        ]
        sock = _Socket(frames)
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: len(adapter._seen) >= 1 and sum(1 for frame in sock.sent if frame.get("op") == "ACK") >= 2), 2)
        self.assertEqual(len(adapter._seen), 1)
        self.assertEqual(adapter._seen[0][0].text, "/sethome")
        await adapter.disconnect()

    async def test_standalone_send_posts_home_channel_text(self):
        cfg = PlatformConfig(extra={"bot_token": "nn_bot_example", "api_base": "https://naonaoai.example"})
        calls = []

        class _Client:
            async def aclose(self):
                return None

        real_open = NaoNaoAdapter._open_http
        real_send = NaoNaoAdapter.send

        def open_http(self):
            return _Client()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            calls.append((chat_id, content))
            from gateway.platforms.base import SendResult
            return SendResult(success=True, message_id="sent_home")

        NaoNaoAdapter._open_http = open_http
        NaoNaoAdapter.send = send
        try:
            result = await adapter_mod._standalone_send(cfg, "community_a:channel_x", "proactive")
        finally:
            NaoNaoAdapter._open_http = real_open
            NaoNaoAdapter.send = real_send
        self.assertEqual(result["success"], True)
        self.assertEqual(calls, [("community_a:channel_x", "proactive")])
        self.assertNotIn("nn_bot_example", str(result))

    async def test_disconnect_does_not_reconnect(self):
        sock = _Socket([{"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}}])
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await adapter.disconnect()
        await asyncio.sleep(0.05)
        self.assertTrue(adapter._shutdown)
        self.assertTrue(adapter._loop_task is None or adapter._loop_task.cancelled() or adapter._loop_task.done())

    async def test_reconnect_backoff_steps_then_stops(self):
        adapter = self._adapter(_ok_api(), [])
        attempts = {"n": 0}

        def fail(_url):
            attempts["n"] += 1
            raise ConnectionError("reset")

        adapter._ws_connect = fail
        delays = []
        original_uniform = adapter_mod.random.uniform
        real_sleep = asyncio.sleep
        adapter_mod.random.uniform = lambda _a, _b: 0

        async def fake_sleep(seconds):
            if seconds >= 1:
                delays.append(seconds)
                if len(delays) >= 3:
                    adapter._shutdown = True
            await real_sleep(0)
            return None

        adapter_mod.asyncio.sleep = fake_sleep
        try:
            self.assertTrue(await adapter.connect())
            for _ in range(50):
                if adapter._loop_task.done():
                    break
                await real_sleep(0.01)
            self.assertTrue(adapter._loop_task.done())
        finally:
            adapter_mod.random.uniform = original_uniform
            adapter_mod.asyncio.sleep = real_sleep
            await adapter.disconnect()
        self.assertEqual(delays, [1, 2, 5])
        self.assertEqual(attempts["n"], 3)
        self.assertIsNone(adapter._ws)

    def _speed_reconnect(self):
        original = adapter_mod.asyncio.sleep

        async def quick(seconds):
            if seconds >= 30:
                await original(3600)
                return
            await original(0)

        adapter_mod.asyncio.sleep = quick
        return original

    async def test_resume_uses_last_acked_sequence(self):
        original_sleep = self._speed_reconnect()
        try:
            await self._resume_uses_last_acked_sequence()
        finally:
            adapter_mod.asyncio.sleep = original_sleep

    async def _resume_uses_last_acked_sequence(self):
        first = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_a", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 100, "t": "message.bot_mention", "d": _mention(event_id="evt_100")},
        ], close_code=4009)
        second = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "RESUMED", "v": 1, "d": {"sessionId": "sess_a", "lastAckedSequence": 100}},
        ], hold=True)
        adapter = self._adapter(_ok_api(), [first, second])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: any(frame.get("op") == "RESUME" for frame in second.sent)), 2)
        resume = next(frame for frame in second.sent if frame["op"] == "RESUME")
        self.assertEqual(resume["d"]["sessionId"], "sess_a")
        self.assertEqual(resume["d"]["resumeToken"], "nn_gwr_example")
        self.assertEqual(resume["d"]["sequence"], 100)
        self.assertFalse(any(frame.get("op") == "IDENTIFY" for frame in second.sent))
        self.assertEqual([item[0].message_id for item in adapter._seen], ["msg_1"])
        await adapter.disconnect()

    async def test_resume_rejected_then_identify_with_new_session(self):
        original_sleep = self._speed_reconnect()
        tokens = []

        def handler(method, url, headers, body):
            if url.endswith("/api/v1/bot/gateway-session"):
                token = f"nn_bgs_{len(tokens) + 1}"
                tokens.append(token)
                return _Response(201, {
                    "ok": True, "token": token, "expiresAt": "2099-01-01T00:00:00+00:00",
                    "gatewayPath": "/gateway/v1", "autoReplyOwner": "simple_gateway",
                })
            return _ok_api()(method, url, headers, body)

        ready = {"op": "READY", "v": 1, "d": {"sessionId": "sess_old", "resumeToken": "nn_gwr_old", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}}
        first = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            ready,
        ], close_code=4009)
        rejected = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
        ], close_code=4005)
        fresh = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_new", "resumeToken": "nn_gwr_new", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
        ], hold=True)
        adapter = self._adapter(handler, [first, rejected, fresh])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: any(frame.get("op") == "IDENTIFY" for frame in fresh.sent)), 2)
        self.assertEqual(rejected.sent[0]["op"], "RESUME")
        self.assertEqual(rejected.sent[0]["d"]["token"], "nn_bgs_1")
        self.assertEqual(fresh.sent[0]["op"], "IDENTIFY")
        self.assertEqual(fresh.sent[0]["d"]["token"], "nn_bgs_2")
        self.assertFalse(any(frame.get("op") == "RESUME" for frame in fresh.sent))
        await adapter.disconnect()
        adapter_mod.asyncio.sleep = original_sleep

    async def test_close_4005_bootstraps_a_new_gateway_session(self):
        original_sleep = self._speed_reconnect()
        tokens = []

        def handler(method, url, headers, body):
            if url.endswith("/api/v1/bot/gateway-session"):
                token = f"nn_bgs_{len(tokens) + 1}"
                tokens.append(token)
                return _Response(201, {
                    "ok": True, "token": token, "expiresAt": "2099-01-01T00:00:00+00:00",
                    "gatewayPath": "/gateway/v1", "autoReplyOwner": "simple_gateway",
                })
            return _ok_api()(method, url, headers, body)

        first = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 4}},
        ], close_code=4005)
        second = _Socket([{"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}}], hold=True)
        adapter = self._adapter(handler, [first, second])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: second.sent), 2)
        auth = second.sent[0]
        self.assertEqual(auth["d"]["token"], "nn_bgs_2")
        self.assertNotEqual(auth["d"]["token"], "nn_bgs_1")
        await adapter.disconnect()
        adapter_mod.asyncio.sleep = original_sleep

    async def test_close_4007_does_not_reconnect(self):
        created = {"n": 0}
        sock = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
        ], close_code=4007)

        def connect(_url):
            created["n"] += 1
            class _CM:
                async def __aenter__(self_nonlocal):
                    return sock
                async def __aexit__(self_nonlocal, exc_type, exc, tb):
                    return False
            return _CM()

        adapter = self._adapter(_ok_api(), [])
        adapter._ws_connect = connect
        try:
            self.assertTrue(await adapter.connect())
            await asyncio.wait_for(self._until(lambda: adapter.has_fatal_error and adapter._loop_task.done()), 2)
            self.assertEqual(adapter._fatal_error_code, "credential_revoked")
            self.assertFalse(adapter._fatal_error_retryable)
            self.assertEqual(created["n"], 1)
        finally:
            await adapter.disconnect()

    async def test_same_event_id_is_deduped_across_sequences(self):
        frames = [
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 101, "t": "message.bot_mention", "d": _mention(event_id="evt_123", content="@测试机器人 你好")},
            {"op": "EVENT", "v": 1, "s": 102, "t": "message.bot_mention", "d": _mention(event_id="evt_123", content="@测试机器人 你好")},
        ]
        sock = _Socket(frames, hold=True)
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: sum(1 for frame in sock.sent if frame.get("op") == "ACK") >= 2 and len(adapter._seen) == 1), 2)
        acks = [frame["d"]["sequence"] for frame in sock.sent if frame.get("op") == "ACK"]
        self.assertEqual(acks, [101, 102])
        await adapter.disconnect()

    async def test_same_text_different_event_ids_both_dispatch(self):
        frames = [
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "EVENT", "v": 1, "s": 1, "t": "message.bot_mention", "d": _mention(event_id="evt_a", content="@测试机器人 你好")},
            {"op": "EVENT", "v": 1, "s": 2, "t": "message.bot_mention", "d": _mention(event_id="evt_b", content="@测试机器人 你好")},
        ]
        sock = _Socket(frames, hold=True)
        adapter = self._adapter(_ok_api(), [sock])

        async def background(event, session_key):
            adapter._seen.append((event, session_key))
            pending = adapter._pending_messages.pop(session_key, None)
            adapter._release_session_guard(session_key)
            if pending is not None:
                await background(pending, session_key)

        adapter._process_message_background = background
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: len(adapter._seen) == 2), 2)
        self.assertEqual([item[0].metadata for item in adapter._seen], [{}, {}])
        self.assertEqual([item[0].text for item in adapter._seen], ["你好", "你好"])
        await adapter.disconnect()

    async def test_lost_response_reuses_idempotency_key(self):
        state = {"n": 0, "keys": []}

        def handler(method, url, headers, body):
            if url.endswith("/messages"):
                state["keys"].append(headers.get("Idempotency-Key"))
                state["n"] += 1
                if state["n"] == 1:
                    raise ConnectionError("response lost")
                return _Response(201, {"ok": True, "message": {"id": "sent_1"}})
            return _ok_api()(method, url, headers, body)

        adapter = self._adapter(handler, [])
        adapter._http = _Http(handler)
        chat = "community_a:channel_x"
        meta = {"naonao_event_id": "evt_abc"}
        lost = await adapter.send(chat, "hello", metadata=meta)
        self.assertTrue(lost.retryable)
        replayed = await adapter.send(chat, "hello", metadata=meta)
        self.assertTrue(replayed.success)
        fresh = await adapter.send(chat, "second", metadata=meta)
        self.assertTrue(fresh.success)
        self.assertEqual(state["keys"], ["evt_abc:1", "evt_abc:1", "evt_abc:2"])

    async def test_409_is_terminal_and_429_keeps_the_key(self):
        state = {"phase": "conflict", "keys": []}

        def handler(method, url, headers, body):
            if url.endswith("/messages"):
                state["keys"].append(headers.get("Idempotency-Key"))
                if state["phase"] == "conflict":
                    return _Response(409, {"ok": False, "err": "idempotency_conflict"})
                if state["phase"] == "limited":
                    state["phase"] = "ok"
                    return _Response(429, {"ok": False, "err": "rate_limited", "retryAfterSec": 7})
                return _Response(200, {"ok": True, "replay": True, "message": {"id": "sent_1"}})
            return _ok_api()(method, url, headers, body)

        adapter = self._adapter(handler, [])
        adapter._http = _Http(handler)
        chat = "community_a:channel_x"
        conflict = await adapter.send(chat, "one", metadata={"naonao_event_id": "evt_conflict"})
        self.assertFalse(conflict.success)
        self.assertFalse(conflict.retryable)
        self.assertEqual(len(state["keys"]), 1)
        state["phase"] = "limited"
        limited = await adapter.send(chat, "wait", metadata={"naonao_event_id": "evt_limit"})
        self.assertTrue(limited.retryable)
        self.assertEqual(limited.retry_after, 7)
        done = await adapter.send(chat, "wait", metadata={"naonao_event_id": "evt_limit"})
        self.assertTrue(done.success)
        self.assertEqual(state["keys"][-2:], ["evt_limit:1", "evt_limit:1"])

    async def test_oversize_text_fails_once(self):
        calls = {"n": 0}

        def handler(method, url, headers, body):
            if url.endswith("/messages"):
                calls["n"] += 1
            return _ok_api()(method, url, headers, body)

        adapter = self._adapter(handler, [])
        adapter._http = _Http(handler)
        result = await adapter.send("community_a:channel_x", "x" * 4001, metadata={"naonao_event_id": "evt_long"})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "message_too_long")
        self.assertFalse(result.retryable)
        self.assertEqual(calls["n"], 0)
        voice = await adapter.send_voice("community_a:channel_x", "/tmp/a.ogg")
        self.assertFalse(voice.success)
        self.assertIn("voice", voice.error)

    async def test_outbound_401_stops_the_connection(self):
        posts = {"n": 0}

        def handler(method, url, headers, body):
            if url.endswith("/messages"):
                posts["n"] += 1
                return _Response(401, {"ok": False, "err": "invalid_bot_token"})
            return _ok_api()(method, url, headers, body)

        sock = _Socket([
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
        ], hold=True)
        adapter = self._adapter(handler, [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: any(frame.get("op") == "IDENTIFY" for frame in sock.sent)), 2)
        failed = await adapter.send("community_a:channel_x", "nope", metadata={"naonao_event_id": "evt_auth"})
        self.assertFalse(failed.success)
        self.assertTrue(adapter.has_fatal_error)
        self.assertFalse(adapter._fatal_error_retryable)
        again = await adapter.send("community_a:channel_x", "nope", metadata={"naonao_event_id": "evt_auth"})
        self.assertEqual(again.error, "invalid_bot_token")
        self.assertEqual(posts["n"], 1)
        await asyncio.wait_for(self._until(lambda: adapter._loop_task.done()), 2)
        self.assertTrue(sock._closed.is_set())

    async def test_owner_disabled_does_not_connect(self):
        created = {"n": 0}

        def connect(_url):
            created["n"] += 1
            return _Socket([])

        adapter = self._adapter(_ok_api(owner="disabled"), [])
        adapter._ws_connect = connect
        self.assertFalse(await adapter.connect())
        self.assertEqual(created["n"], 0)
        self.assertTrue(adapter.has_fatal_error)

    async def test_unknown_opcode_and_bad_json_do_not_kill_the_socket(self):
        frames = [
            "not-json",
            "\"just-a-string\"",
            {"op": "HELLO", "v": 1, "d": {"heartbeatIntervalMs": 60000}},
            {"op": "READY", "v": 1, "d": {"sessionId": "sess_1", "resumeToken": "nn_gwr_example", "resumeExpiresAt": "2099-01-01T00:00:00+00:00", "startSequence": 0}},
            {"op": "FUTURE_OP", "v": 1, "d": {}},
            {"op": "EVENT", "v": 1, "s": 8, "t": "message.future_kind", "d": {"id": "evt_future"}},
            {"op": "EVENT", "v": 1, "s": 9, "t": "message.bot_mention", "d": _mention(event_id="evt_ok")},
        ]
        sock = _Socket(frames, hold=True)
        adapter = self._adapter(_ok_api(), [sock])
        self.assertTrue(await adapter.connect())
        await asyncio.wait_for(self._until(lambda: len(adapter._seen) == 1), 2)
        acks = [frame["d"]["sequence"] for frame in sock.sent if frame.get("op") == "ACK"]
        self.assertEqual(acks, [8, 9])
        await adapter.disconnect()

    async def test_fresh_process_forgets_dedupe_cache(self):
        first = NaoNaoAdapter(PlatformConfig(extra={"bot_token": "nn_bot_example"}))
        second = NaoNaoAdapter(PlatformConfig(extra={"bot_token": "nn_bot_example"}))
        self.assertFalse(first._dedup.is_duplicate("evt_restart"))
        self.assertTrue(first._dedup.is_duplicate("evt_restart"))
        self.assertFalse(second._dedup.is_duplicate("evt_restart"))

    def test_http_and_websocket_timeouts_are_bounded(self):
        import httpx
        seen = {}

        def wrapped(**kwargs):
            seen.update(kwargs)
            raise RuntimeError("constructed")

        adapter = NaoNaoAdapter(PlatformConfig(extra={"bot_token": "nn_bot_example"}))
        with unittest.mock.patch("httpx.AsyncClient", wrapped):
            with self.assertRaises(RuntimeError):
                adapter._open_http()
        self.assertFalse(seen["follow_redirects"])
        self.assertEqual(seen["timeout"].connect, 30)
        captured = {}

        def ws_connect(url, **kwargs):
            captured.update(kwargs)
            return "socket"

        with unittest.mock.patch.dict("sys.modules", {"websockets": unittest.mock.Mock(connect=ws_connect)}):
            result = adapter_mod._default_ws_connect("wss://example.test/gateway/v1")
        self.assertEqual(result, "socket")
        self.assertEqual(captured["open_timeout"], 30)
        self.assertEqual(captured["max_size"], 1024 * 1024)

    async def _until(self, predicate):
        while not predicate():
            await asyncio.sleep(0.01)


class StreamingCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """NaoNao supports progressive edit; Hermes may wire stream deltas."""

    @classmethod
    def setUpClass(cls):
        # Platform("naonao") requires registry membership via plugin register().
        register(_Ctx())

    def _adapter(self):
        posts = []
        patches = []

        def handler(method, url, headers, body):
            url_s = str(url)
            if method.upper() == "POST" and url_s.rstrip("/").endswith("/messages"):
                posts.append(body)
                return _Response(201, {"ok": True, "message": {"id": "msg_stream_1"}})
            if method.upper() == "PATCH" and "/messages/" in url_s:
                patches.append({"url": url_s, "body": body})
                mid = url_s.rstrip("/").rsplit("/", 1)[-1]
                return _Response(200, {"ok": True, "message": {"id": mid, "content": (body or {}).get("content")}})
            return _Response(200, {})

        http = _Http(handler)
        adapter = NaoNaoAdapter(
            PlatformConfig(extra={"bot_token": "nn_bot_example", "api_base": "https://naonaoai.com"}),
            http_factory=lambda: http,
        )
        adapter._http = http
        adapter._posts = posts
        adapter._patches = patches
        adapter._http_calls = http.calls
        return adapter

    def _decide(self, adapter, *, interim):
        from types import SimpleNamespace
        from gateway.config import StreamingConfig
        from gateway.run_turn import GatewayTurnMixin
        from gateway.run_turn_runner import TurnRunner
        from gateway.session import SessionSource

        source = SessionSource(
            platform=Platform("naonao"),
            chat_id="community_a:channel_x",
            chat_type="channel",
            user_id="actor_1",
        )
        runner = SimpleNamespace(
            config=SimpleNamespace(streaming=StreamingConfig(enabled=True)),
            _delivery_adapter_for=lambda _source: adapter,
        )
        runner._build_stream_consumer_config = (
            lambda source, scfg, adapter, on_missing_cursor: GatewayTurnMixin._build_stream_consumer_config(
                runner, source, scfg, adapter, on_missing_cursor=on_missing_cursor,
            )
        )
        ctx = SimpleNamespace(
            mute_notification_reply=False,
            streaming_tts_consumer_holder=[None],
            scheduled_heartbeat=False,
            interim_assistant_messages_enabled=interim,
            user_config={},
            resolve_display_setting=lambda _user_config, _platform_key, _setting: None,
            stream_consumer_holder=[None],
            source=source,
            _status_thread_metadata=None,
            progress_queue=None,
            event_message_id="msg_user",
            _run_still_current=lambda: True,
        )
        host = SimpleNamespace(_ctx=ctx, _runner=runner)
        return TurnRunner._setup_stream_consumer(host, "naonao")

    def test_editing_capability_is_true(self):
        self.assertIs(NaoNaoAdapter.SUPPORTS_MESSAGE_EDITING, True)
        adapter = self._adapter()
        self.assertFalse(adapter.prefers_fresh_final_streaming("x"))

    def test_global_streaming_wires_preview_deltas(self):
        adapter = self._adapter()
        consumer, delta_cb, _interim_cb, _want_interim = self._decide(adapter, interim=False)
        self.assertIsNotNone(delta_cb)
        self.assertIsNotNone(consumer)
        self.assertTrue(consumer.stream_deltas_enabled)
        self.assertTrue(bool(consumer.cfg.cursor))

    async def test_send_then_edit_lifecycle_same_message_id(self):
        adapter = self._adapter()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "中", metadata={"naonao_event_id": "evt_s"})
        self.assertTrue(created.success)
        self.assertEqual(created.message_id, "msg_stream_1")
        self.assertEqual(len(adapter._posts), 1)

        e1 = await adapter.edit_message(chat, created.message_id, "中国 ▉")
        e2 = await adapter.edit_message(chat, created.message_id, "中国古代 ▉")
        e3 = await adapter.edit_message(
            chat, created.message_id, "中国古代四大名著。", finalize=True,
        )
        self.assertTrue(e1.success)
        self.assertTrue(e2.success)
        self.assertTrue(e3.success)
        self.assertEqual(e3.message_id, "msg_stream_1")
        self.assertEqual(len(adapter._posts), 1)
        self.assertGreaterEqual(len(adapter._patches), 1)
        for p in adapter._patches:
            self.assertIn("/channels/channel_x/messages/msg_stream_1", p["url"])
            self.assertIn("content", p["body"])
        self.assertEqual(adapter._patches[-1]["body"]["content"], "中国古代四大名著。")
        self.assertNotIn("▉", adapter._patches[-1]["body"]["content"])

    async def test_edit_body_is_full_accumulated_not_delta(self):
        adapter = self._adapter()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "A")
        await adapter.edit_message(chat, created.message_id, "AB")
        await adapter.edit_message(chat, created.message_id, "ABC", finalize=True)
        self.assertEqual(adapter._patches[-1]["body"]["content"], "ABC")
        self.assertNotEqual(adapter._patches[-1]["body"]["content"], "C")

    async def test_rapid_edits_coalesce_to_latest(self):
        adapter = self._adapter()
        # Slow PATCH so concurrent edits pile up while one is in flight.
        release = asyncio.Event()
        started = asyncio.Event()
        posts = []
        patches = []

        async def slow_request(method, url, headers=None, json=None, follow_redirects=None):
            adapter._http_calls.append({"method": method, "url": url, "json": json})
            if method.upper() == "POST":
                posts.append(json)
                return _Response(201, {"ok": True, "message": {"id": "msg_c"}})
            if method.upper() == "PATCH":
                started.set()
                await release.wait()
                patches.append(json)
                return _Response(200, {"ok": True, "message": {"id": "msg_c", "content": (json or {}).get("content")}})
            return _Response(200, {})

        adapter._http.request = slow_request
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "A")
        self.assertEqual(created.message_id, "msg_c")
        t1 = asyncio.create_task(adapter.edit_message(chat, created.message_id, "A"))
        await started.wait()
        t2 = asyncio.create_task(adapter.edit_message(chat, created.message_id, "AB"))
        t3 = asyncio.create_task(adapter.edit_message(chat, created.message_id, "ABCD", finalize=True))
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(t1, t2, t3)
        self.assertTrue(all(r.success for r in results))
        self.assertEqual(len(posts), 1)
        self.assertEqual(patches[-1]["content"], "ABCD")
        self.assertLess(len(patches), 4)

    async def test_403_signals_failure_does_not_post_replacement(self):
        adapter = self._adapter()

        def handler(method, url, headers, body):
            if method.upper() == "POST":
                return _Response(201, {"ok": True, "message": {"id": "msg_x"}})
            return _Response(403, {"ok": False, "err": "insufficient_permissions"})

        adapter._http.handler = handler
        adapter._http.calls.clear()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "hi")
        result = await adapter.edit_message(chat, created.message_id, "nope", finalize=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "forbidden")
        self.assertTrue(result.raw_response.get("naonao_edit_dead"))
        self.assertFalse(result.raw_response.get("skipped"))
        post_count = sum(1 for c in adapter._http.calls if c["method"].upper() == "POST")
        self.assertEqual(post_count, 1)

    async def test_404_signals_failure_does_not_post_replacement(self):
        adapter = self._adapter()

        def handler(method, url, headers, body):
            if method.upper() == "POST":
                return _Response(201, {"ok": True, "message": {"id": "msg_y"}})
            return _Response(404, {"ok": False, "err": "resource_not_found"})

        adapter._http.handler = handler
        adapter._http.calls.clear()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "hi")
        result = await adapter.edit_message(chat, created.message_id, "gone", finalize=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "not_found")
        self.assertTrue(result.raw_response.get("naonao_edit_dead"))
        self.assertFalse(result.raw_response.get("skipped"))
        post_count = sum(1 for c in adapter._http.calls if c["method"].upper() == "POST")
        self.assertEqual(post_count, 1)

    async def test_transient_retry_same_message_id_no_extra_post(self):
        adapter = self._adapter()
        attempts = {"n": 0}

        def handler(method, url, headers, body):
            if method.upper() == "POST":
                return _Response(201, {"ok": True, "message": {"id": "msg_r"}})
            attempts["n"] += 1
            if attempts["n"] < 2:
                return _Response(503, {"ok": False, "err": "internal_error"})
            return _Response(200, {"ok": True, "message": {"id": "msg_r", "content": body.get("content")}})

        adapter._http.handler = handler
        adapter._http.calls.clear()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "hi")
        result = await adapter.edit_message(chat, created.message_id, "ok", finalize=True)
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "msg_r")
        self.assertGreaterEqual(attempts["n"], 2)
        post_count = sum(1 for c in adapter._http.calls if c["method"].upper() == "POST")
        self.assertEqual(post_count, 1)

    async def test_markdown_code_unicode_preserved(self):
        adapter = self._adapter()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, ".")
        body = "你好 **粗体**\n```js\nconsole.log(1)\n```\n🎉"
        result = await adapter.edit_message(chat, created.message_id, body, finalize=True)
        self.assertTrue(result.success)
        self.assertEqual(adapter._patches[-1]["body"]["content"], body)

    async def test_gateway_event_filter_ignores_non_mention(self):
        adapter = self._adapter()
        frames = []

        class Sock:
            async def send(self, raw):
                frames.append(json.loads(raw))

        await adapter._on_event(Sock(), {
            "t": "message.created",
            "s": 9,
            "d": {"id": "evt_noise", "message": {"id": "m1", "content": "hi"}},
        })
        self.assertTrue(any(f.get("op") == "ACK" for f in frames))
        self.assertEqual(adapter._posts, [])

    async def test_duplicate_regression_one_create_many_edits(self):
        adapter = self._adapter()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "中国 ▉")
        for text in ("中国古代 ▉", "中国古代四大名著 ▉", "中国古代四大名著包括《三国演义》。"):
            await adapter.edit_message(
                chat, created.message_id, text,
                finalize=text.endswith("。"),
            )
        self.assertEqual(len(adapter._posts), 1)
        self.assertEqual(created.message_id, "msg_stream_1")
        self.assertEqual(adapter._patches[-1]["body"]["content"], "中国古代四大名著包括《三国演义》。")
        patch_ids = {p["url"].rsplit("/", 1)[-1] for p in adapter._patches}
        self.assertEqual(patch_ids, {"msg_stream_1"})


if __name__ == "__main__":
    unittest.main()
