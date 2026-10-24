"""Phase 1D E2E / stability harness for NaoNao Hermes streaming.

Simulates: send → PATCH stream → final flush with mock HTTP only.
No production API, no real Bot Token.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location("naonao_platform_adapter_p1d", PLUGIN_DIR / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
NaoNaoAdapter = adapter_mod.NaoNaoAdapter
register = adapter_mod.register
EDIT_THROTTLE_SECONDS = adapter_mod.EDIT_THROTTLE_SECONDS

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry


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


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body


class StreamingStore:
    """In-memory NaoNao Bot API mock: one Message doc + Socket-like emits."""

    def __init__(self):
        self.messages = {}  # id -> content
        self.posts = []
        self.patches = []
        self.socket_events = []  # {ev, id, content}
        self.gateway_input_events = []
        self.in_flight_patches = 0
        self.max_concurrent_patches = 0
        self.patch_gate = None  # optional asyncio.Event: hold PATCH until set
        self.patch_started = None
        self.fail_mode = None  # None | '500_once' | '403' | '404' | '429' | 'timeout_then_ok'
        self._fail_left = 0
        self.next_id = 1

    def handler(self, method, url, headers, body):
        method = method.upper()
        url_s = str(url)
        if method == "POST" and url_s.rstrip("/").endswith("/messages"):
            mid = f"msg_{self.next_id}"
            self.next_id += 1
            content = (body or {}).get("content", "")
            self.messages[mid] = content
            self.posts.append({"id": mid, "content": content, "url": url_s})
            # Bot create does not become Public Bot Gateway input
            self.socket_events.append({"ev": "receive_message", "id": mid, "content": content})
            return _Response(201, {"ok": True, "message": {"id": mid, "content": content}})

        if method == "PATCH" and "/messages/" in url_s:
            mid = url_s.rstrip("/").rsplit("/", 1)[-1]
            content = (body or {}).get("content", "")
            self.patches.append({"id": mid, "content": content, "url": url_s})

            if self.fail_mode == "403":
                return _Response(403, {"ok": False, "err": "insufficient_permissions"})
            if self.fail_mode == "404":
                return _Response(404, {"ok": False, "err": "resource_not_found"})
            if self.fail_mode == "429":
                self.fail_mode = None
                return _Response(429, {"ok": False, "err": "rate_limited", "retryAfterSec": 0.05})
            if self.fail_mode == "500_once":
                self.fail_mode = None
                return _Response(503, {"ok": False, "err": "internal_error"})
            if self.fail_mode == "timeout_then_ok":
                self.fail_mode = None
                raise TimeoutError("simulated timeout")

            if mid not in self.messages:
                return _Response(404, {"ok": False, "err": "resource_not_found"})
            self.messages[mid] = content
            # Phase 1B double broadcast pattern
            self.socket_events.append({"ev": "receive_message", "id": mid, "content": content})
            self.socket_events.append({"ev": "message_updated", "id": mid, "content": content})
            return _Response(200, {"ok": True, "message": {"id": mid, "content": content}})

        return _Response(404, {"ok": False, "err": "resource_not_found"})


class _Http:
    def __init__(self, store: StreamingStore):
        self.store = store
        self.calls = []

    async def request(self, method, url, headers=None, json=None, follow_redirects=None):
        self.calls.append({"method": method, "url": url, "json": json})
        method_u = method.upper()
        if method_u == "PATCH":
            self.store.in_flight_patches += 1
            self.store.max_concurrent_patches = max(
                self.store.max_concurrent_patches, self.store.in_flight_patches
            )
            if self.store.patch_started is not None:
                self.store.patch_started.set()
            try:
                if self.store.patch_gate is not None:
                    await self.store.patch_gate.wait()
                if self.store.fail_mode == "timeout_then_ok":
                    # consume one-shot timeout before handler
                    self.store.fail_mode = None
                    raise TimeoutError("simulated timeout")
                return self.store.handler(method, url, headers or {}, json)
            finally:
                self.store.in_flight_patches -= 1
        return self.store.handler(method, url, headers or {}, json)

    async def aclose(self):
        return None


def upsert_client_messages(prev, msg):
    """Mirrors App.jsx receive_message / message_updated same-id merge."""
    if not isinstance(prev, list):
        return [msg]
    mid = str(msg.get("_id") or msg.get("id") or "")
    if not mid:
        return prev + [msg]
    idx = next(
        (i for i, old in enumerate(prev) if str(old.get("_id") or old.get("id") or "") == mid),
        -1,
    )
    if idx >= 0:
        next_list = prev[:]
        next_list[idx] = {**prev[idx], **msg}
        return next_list
    return prev + [msg]


class Phase1DE2ETests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        register(_Ctx())

    def _make(self, store=None):
        store = store or StreamingStore()
        http = _Http(store)
        adapter = NaoNaoAdapter(
            PlatformConfig(extra={"bot_token": "nn_bot_example", "api_base": "https://naonaoai.com"}),
            http_factory=lambda: http,
        )
        adapter._http = http
        return adapter, store, http

    async def test_01_normal_chinese_streaming_e2e(self):
        adapter, store, http = self._make()
        chat = "community_a:channel_ai"
        user_input = "中国古代四大名著有哪些？"
        # Gateway input only for the user mention — never for bot edits
        store.gateway_input_events.append({"t": "message.bot_mention", "content": user_input})

        versions = [
            "中国古代",
            "中国古代四大名著",
            "中国古代四大名著包括《三国演义》",
            "中国古代四大名著包括《三国演义》《水浒传》《西游记》《红楼梦》。",
        ]
        created = await adapter.send(chat, versions[0] + " ▉")
        self.assertTrue(created.success)
        mid = created.message_id
        client = [{"_id": mid, "content": versions[0] + " ▉"}]

        for text in versions[1:-1]:
            r = await adapter.edit_message(chat, mid, text + " ▉")
            self.assertTrue(r.success)
            self.assertEqual(r.message_id, mid)
            client = upsert_client_messages(client, {"_id": mid, "id": mid, "content": text + " ▉"})
            client = upsert_client_messages(client, {"_id": mid, "id": mid, "content": text + " ▉"})  # double broadcast

        final = versions[-1]
        r = await adapter.edit_message(chat, mid, final, finalize=True)
        self.assertTrue(r.success)
        client = upsert_client_messages(client, {"_id": mid, "content": final})
        client = upsert_client_messages(client, {"_id": mid, "content": final})

        self.assertEqual(len(store.posts), 1)
        self.assertGreaterEqual(len(store.patches), 1)
        self.assertEqual(set(store.messages.keys()), {mid})
        self.assertEqual(store.messages[mid], final)
        self.assertNotIn("▉", store.messages[mid])
        self.assertEqual(len(client), 1)
        self.assertEqual(client[0]["content"], final)
        self.assertEqual(len(store.gateway_input_events), 1)
        self.assertEqual(store.gateway_input_events[0]["t"], "message.bot_mention")

    async def test_02_rapid_coalescing_100_callbacks(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "x0")
        mid = created.message_id
        callbacks = 120
        # Slow first PATCH so later edits coalesce
        store.patch_gate = asyncio.Event()
        store.patch_started = asyncio.Event()

        t0 = asyncio.create_task(adapter.edit_message(chat, mid, "x1"))
        await store.patch_started.wait()
        tasks = [
            asyncio.create_task(adapter.edit_message(chat, mid, f"x{i}"))
            for i in range(2, callbacks + 1)
        ]
        tasks.append(asyncio.create_task(
            adapter.edit_message(chat, mid, f"x{callbacks}_FINAL", finalize=True)
        ))
        await asyncio.sleep(0.05)
        store.patch_gate.set()
        await t0
        await asyncio.gather(*tasks)

        self.assertEqual(len(store.posts), 1)
        self.assertLess(len(store.patches), callbacks)
        self.assertLessEqual(store.max_concurrent_patches, 1)
        self.assertEqual(store.messages[mid], f"x{callbacks}_FINAL")
        self.assertTrue(len(store.patches) < callbacks * 0.5)

    async def test_03_slow_network_ordering_no_stale_overwrite(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "A")
        mid = created.message_id
        store.patch_gate = asyncio.Event()
        store.patch_started = asyncio.Event()

        t_a = asyncio.create_task(adapter.edit_message(chat, mid, "A"))
        await store.patch_started.wait()
        t_b = asyncio.create_task(adapter.edit_message(chat, mid, "B"))
        t_c = asyncio.create_task(adapter.edit_message(chat, mid, "C"))
        t_d = asyncio.create_task(adapter.edit_message(chat, mid, "D", finalize=True))
        await asyncio.sleep(0.05)
        self.assertEqual(store.max_concurrent_patches, 1)
        store.patch_gate.set()
        await asyncio.gather(t_a, t_b, t_c, t_d)
        self.assertEqual(store.messages[mid], "D")
        self.assertLessEqual(store.max_concurrent_patches, 1)
        # Last successful content must be D (not A)
        self.assertEqual(store.patches[-1]["content"], "D")

    async def test_04_final_flush_waits_and_wins(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "v1")
        mid = created.message_id
        store.patch_gate = asyncio.Event()
        store.patch_started = asyncio.Event()

        t_mid = asyncio.create_task(adapter.edit_message(chat, mid, "v2 ▉"))
        await store.patch_started.wait()
        t_fin = asyncio.create_task(
            adapter.edit_message(chat, mid, "v3 FINAL COMPLETE", finalize=True)
        )
        await asyncio.sleep(0.05)
        store.patch_gate.set()
        await asyncio.gather(t_mid, t_fin)
        self.assertEqual(store.messages[mid], "v3 FINAL COMPLETE")
        self.assertNotIn("▉", store.messages[mid])

    async def test_05_duplicate_regression_partial_plus_final(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        # Old bug: POST preview then POST final. Now: POST once + PATCH final.
        created = await adapter.send(chat, "partial ▉")
        mid = created.message_id
        await adapter.edit_message(chat, mid, "partial growing ▉")
        await adapter.edit_message(chat, mid, "complete final answer", finalize=True)
        self.assertEqual(len(store.posts), 1)
        self.assertGreaterEqual(len(store.patches), 1)
        self.assertEqual(len(store.messages), 1)
        self.assertEqual(store.messages[mid], "complete final answer")

    async def test_06_temporary_500_retry_same_id(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "hi")
        mid = created.message_id
        store.fail_mode = "500_once"
        r = await adapter.edit_message(chat, mid, "recovered", finalize=True)
        self.assertTrue(r.success)
        self.assertEqual(store.messages[mid], "recovered")
        self.assertEqual(len(store.posts), 1)

    async def test_07_403_no_replacement_post(self):
        adapter, store, http = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "partial")
        mid = created.message_id
        store.fail_mode = "403"
        before = len(store.posts)
        r = await adapter.edit_message(chat, mid, "try", finalize=True)
        self.assertTrue(r.success)
        self.assertTrue(r.raw_response.get("naonao_edit_dead") or r.raw_response.get("skipped"))
        self.assertEqual(len(store.posts), before)
        self.assertEqual(store.messages[mid], "partial")

    async def test_08_404_no_replacement_post(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "partial")
        mid = created.message_id
        store.fail_mode = "404"
        before = len(store.posts)
        r = await adapter.edit_message(chat, mid, "try", finalize=True)
        self.assertTrue(r.success)
        self.assertEqual(len(store.posts), before)

    async def test_09_429_backoff_then_latest(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "hi")
        mid = created.message_id
        store.fail_mode = "429"
        r = await adapter.edit_message(chat, mid, "after429", finalize=True)
        self.assertTrue(r.success)
        self.assertEqual(store.messages[mid], "after429")
        self.assertEqual(len(store.posts), 1)

    async def test_10_generation_interrupt_keeps_partial(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "partial only")
        mid = created.message_id
        # Simulate generation abort: no further edit, no error POST
        self.assertEqual(len(store.posts), 1)
        self.assertEqual(store.messages[mid], "partial only")
        self.assertEqual(len(store.messages), 1)

    async def test_11_markdown_and_code_fence(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "# 标题 ▉")
        mid = created.message_id
        interim = "## 标题\n- item\n```js\nconsole.log(1)"
        await adapter.edit_message(chat, mid, interim + " ▉")
        final = "## 标题\n- item\n```js\nconsole.log(1)\n```\n> quote\n**bold** [link](https://a.test)"
        await adapter.edit_message(chat, mid, final, finalize=True)
        self.assertEqual(store.messages[mid], final)

    async def test_12_chinese_emoji_unicode(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        text = "你好 👋 Hermes 正在回答。"
        created = await adapter.send(chat, "…")
        await adapter.edit_message(chat, created.message_id, text, finalize=True)
        self.assertEqual(store.messages[created.message_id], text)

    async def test_13_4000_boundary(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "seed")
        mid = created.message_id
        for n in (3999, 4000):
            body = "字" * n
            r = await adapter.edit_message(chat, mid, body, finalize=True)
            self.assertTrue(r.success, n)
            self.assertEqual(len(store.messages[mid]), n)
        over = "字" * 4001
        r = await adapter.edit_message(chat, mid, over, finalize=True)
        self.assertFalse(r.success)
        self.assertEqual(r.error_kind, "too_long")
        # DB unchanged (no second message)
        self.assertEqual(len(store.posts), 1)
        self.assertEqual(len(store.messages), 1)

    async def test_14_socket_double_broadcast_same_id_no_append(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "a")
        mid = created.message_id
        await adapter.edit_message(chat, mid, "ab", finalize=True)
        client = []
        for ev in store.socket_events:
            client = upsert_client_messages(
                client, {"_id": ev["id"], "id": ev["id"], "content": ev["content"]}
            )
        self.assertEqual(len(client), 1)
        self.assertEqual(client[0]["content"], "ab")
        # Both event types present for edits
        kinds = {e["ev"] for e in store.socket_events if e["id"] == mid}
        self.assertIn("receive_message", kinds)
        self.assertIn("message_updated", kinds)

    async def test_15_gateway_self_loop_bot_edit_ignored(self):
        adapter, store, _ = self._make()
        chat = "community_a:channel_x"
        created = await adapter.send(chat, "hi")
        await adapter.edit_message(chat, created.message_id, "hi2", finalize=True)
        # Simulate what would happen if gateway wrongly delivered bot update — adapter ignores.
        frames = []

        class Sock:
            async def send(self, raw):
                frames.append(json.loads(raw))

        await adapter._on_event(Sock(), {
            "t": "message.created",
            "s": 1,
            "d": {"id": "evt_bot_self", "message": {"id": created.message_id, "content": "hi2"}},
        })
        self.assertTrue(any(f.get("op") == "ACK" for f in frames))
        # Still only the original create POST
        self.assertEqual(len(store.posts), 1)

    async def test_16_multi_channel_isolation(self):
        adapter, store, _ = self._make()
        a = await adapter.send("c1:channel_a", "A0")
        b = await adapter.send("c1:channel_b", "B0")
        await adapter.edit_message("c1:channel_a", a.message_id, "A_FINAL", finalize=True)
        await adapter.edit_message("c1:channel_b", b.message_id, "B_FINAL", finalize=True)
        self.assertEqual(store.messages[a.message_id], "A_FINAL")
        self.assertEqual(store.messages[b.message_id], "B_FINAL")
        self.assertEqual(len(store.posts), 2)
        self.assertNotEqual(a.message_id, b.message_id)

    async def test_17_concurrent_messages_independent(self):
        adapter, store, _ = self._make()
        a = await adapter.send("c1:ch", "A")
        b = await adapter.send("c1:ch", "B")
        # Parallel finals on independent messageIds
        await asyncio.gather(
            adapter.edit_message("c1:ch", a.message_id, "A_OK", finalize=True),
            adapter.edit_message("c1:ch", b.message_id, "B_OK", finalize=True),
        )
        self.assertEqual(store.messages[a.message_id], "A_OK")
        self.assertEqual(store.messages[b.message_id], "B_OK")
        self.assertEqual(len(store.posts), 2)
        self.assertNotEqual(a.message_id, b.message_id)
        # Kill B only; A still editable
        store.fail_mode = "403"
        dead = await adapter.edit_message("c1:ch", b.message_id, "B_DEAD", finalize=True)
        self.assertTrue(dead.raw_response.get("skipped") or dead.raw_response.get("naonao_edit_dead"))
        store.fail_mode = None
        await adapter.edit_message("c1:ch", a.message_id, "A_STILL", finalize=True)
        self.assertEqual(store.messages[a.message_id], "A_STILL")
        self.assertEqual(store.messages[b.message_id], "B_OK")

    async def test_18_channel_switch_does_not_retarget_patch(self):
        adapter, store, _ = self._make()
        # Streaming identity is chat_id + message_id from original send — not UI channel.
        created = await adapter.send("c1:channel_a", "start")
        mid = created.message_id
        # UI "switches" to channel_b — adapter still PATCHes channel_a URL
        await adapter.edit_message("c1:channel_a", mid, "done", finalize=True)
        self.assertTrue(any("/channels/channel_a/messages/" + mid in p["url"] for p in store.patches))
        self.assertFalse(any("/channels/channel_b/" in p["url"] for p in store.patches))

    def test_19_throttle_constant(self):
        self.assertGreaterEqual(EDIT_THROTTLE_SECONDS, 0.3)
        self.assertLessEqual(EDIT_THROTTLE_SECONDS, 0.4)
        self.assertIs(NaoNaoAdapter.SUPPORTS_MESSAGE_EDITING, True)


if __name__ == "__main__":
    unittest.main()
