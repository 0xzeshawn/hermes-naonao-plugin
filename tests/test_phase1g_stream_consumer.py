"""Phase 1G: GatewayStreamConsumer + NaoNao adapter edit-failure contract.

Reproduces the Phase 1F production failure chain:
  POST partial OK → first PATCH 404 → provider completes → final must NOT be
  swallowed by a fake success/skipped edit that sets already_sent.

Uses real Hermes GatewayStreamConsumer against the NaoNao adapter with a fake
HTTP backend. No production network.
"""

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

if not (HERMES_ROOT / "gateway" / "stream_consumer.py").is_file():
    raise unittest.SkipTest(f"Hermes source not found at {HERMES_ROOT}")


def _load_adapter():
    spec = importlib.util.spec_from_file_location(
        "naonao_platform_adapter_p1g", PLUGIN_DIR / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
NaoNaoAdapter = adapter_mod.NaoNaoAdapter

from gateway.config import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body


class _FakeHttp:
    """POST succeeds; every PATCH returns 404 (Phase 1F Bug A shape)."""

    def __init__(self):
        self.calls = []
        self.posts = []
        self.patches = []
        self._msg_seq = 0

    async def request(self, method, url, headers=None, json=None, follow_redirects=None):
        method_u = str(method or "").upper()
        self.calls.append({"method": method_u, "url": url, "json": json})
        if method_u == "POST":
            self._msg_seq += 1
            mid = f"msg_phase1g_{self._msg_seq}"
            body = {
                "ok": True,
                "message": {"id": mid, "content": (json or {}).get("content")},
            }
            self.posts.append({"url": url, "json": json, "id": mid})
            return _Response(201, body)
        if method_u == "PATCH":
            self.patches.append({"url": url, "json": json})
            return _Response(404, {"ok": False, "err": "resource_not_found"})
        return _Response(404, {"ok": False, "err": "resource_not_found"})

    async def aclose(self):
        return None


class Phase1GStreamConsumerTests(unittest.IsolatedAsyncioTestCase):
    def _make_adapter(self):
        http = _FakeHttp()
        adapter = NaoNaoAdapter(
            PlatformConfig(extra={"bot_token": "nn_bot_example", "api_base": "https://naonaoai.com"}),
            http_factory=lambda: http,
        )
        adapter._http = http
        return adapter

    async def test_patch_404_does_not_suppress_complete_final_via_fallback(self):
        """Phase 1F chain: fake success/skipped must not set already_sent semantics.

        After permanent PATCH 404, GatewayStreamConsumer must enter fallback and
        deliver the complete answer via adapter.send (degraded: partial + final).
        """
        adapter = self._make_adapter()
        http = adapter._http
        chat = "c8ci4iimb:0558e3d7db32"
        final_text = (
            "中国古代四大名著包括《红楼梦》《西游记》《水浒传》《三国演义》。"
            "每部作品都有作者、成书年代和主要内容。"
        )

        cfg = StreamConsumerConfig(
            transport="edit",
            chat_type="group",
            edit_interval=0.02,
            buffer_threshold=8,
            cursor=" ▉",
        )
        sc = GatewayStreamConsumer(adapter, chat, cfg)
        self.assertTrue(getattr(adapter, "SUPPORTS_MESSAGE_EDITING", False))

        task = asyncio.create_task(sc.run())
        # Grow past buffer threshold so first send + edit path engage.
        sc.on_delta("中国古代")
        await asyncio.sleep(0.05)
        sc.on_delta("四大名著包括")
        await asyncio.sleep(0.05)
        sc.on_delta(final_text[len("中国古代四大名著包括"):])
        await asyncio.sleep(0.08)
        sc.finish(final_text)
        await asyncio.wait_for(task, timeout=5.0)

        # At least one PATCH was attempted and returned 404.
        self.assertGreaterEqual(len(http.patches), 1)

        post_bodies = [
            str((p.get("json") or {}).get("content") or "")
            for p in http.posts
        ]
        # Phase 1F bug was: success=True/skipped → already_sent with ONLY the
        # partial POST and zero fallback. Degraded recovery may be:
        #   partial (+ cursor) + continuation tail  (two POSTs), or
        #   a single POST carrying the full final.
        # Either way the complete answer must be reconstructible.
        def _strip_cursor(s: str) -> str:
            return str(s or "").replace(" ▉", "").replace("▉", "")

        cleaned = [_strip_cursor(b) for b in post_bodies]
        joined = "".join(cleaned)
        has_full_single = any(final_text == b or final_text in b for b in cleaned)
        # Hermes fallback may POST only the missing suffix after the visible prefix.
        has_full_via_parts = (
            final_text in joined
            or (
                cleaned
                and final_text.startswith(cleaned[0])
                and cleaned[0]
                and final_text[len(cleaned[0]) :] in "".join(cleaned[1:])
            )
        )
        self.assertTrue(
            has_full_single or has_full_via_parts,
            f"complete final not delivered; posts={post_bodies!r} "
            f"patches={len(http.patches)} already_sent={sc._already_sent} "
            f"final_content_delivered={getattr(sc, 'final_content_delivered', None)} "
            f"fallback_flag={getattr(sc, '_fallback_final_send', None)} "
            f"matches={sc.delivered_final_matches(final_text)!r}",
        )
        # More than the initial partial alone — fallback/continuation fired.
        self.assertGreaterEqual(
            len(http.posts),
            2,
            f"expected degraded fallback POST after PATCH 404; posts={post_bodies!r}",
        )
        # Consumer must acknowledge final content was delivered (via fallback),
        # not silently stop after a fake skipped success with only partial.
        self.assertTrue(
            bool(getattr(sc, "final_content_delivered", False))
            or sc.delivered_final_matches(final_text) is True,
            "Hermes did not mark final content delivered after edit failure recovery",
        )

    async def test_adapter_404_edit_returns_failure_not_skipped_success(self):
        adapter = self._make_adapter()
        chat = "c8ci4iimb:0558e3d7db32"
        created = await adapter.send(chat, "partial ▉")
        r = await adapter.edit_message(chat, created.message_id, "grown ▉", finalize=False)
        self.assertFalse(r.success)
        self.assertEqual(r.error_kind, "not_found")
        self.assertFalse((r.raw_response or {}).get("skipped"))
        # Dead lane must keep failing (not fake success) so finalize cannot
        # mark already_sent.
        r2 = await adapter.edit_message(
            chat, created.message_id, "complete answer", finalize=True
        )
        self.assertFalse(r2.success)
        self.assertEqual(r2.error_kind, "not_found")


if __name__ == "__main__":
    unittest.main()
