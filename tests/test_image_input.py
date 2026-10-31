"""Hermes-native image input tests. All HTTP and cache writes are mocked."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from test_adapter import adapter_mod as mod, _mention, _Ctx, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType

PNG = b"\x89PNG\r\n\x1a\n" + b"test image bytes"


def attachment(index=0, **extra):
    return {"index": index, "kind": "image", "mimeType": "image/png", "size": len(PNG), **extra}


class Response:
    def __init__(self, data=PNG, status=200, mime="image/png", headers=None, chunks=None):
        self.status_code = status
        self.data = data
        self.headers = {"content-type": mime, **(headers or {})}
        self.chunks = chunks

    def json(self):
        return json.loads(self.data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def aiter_bytes(self, chunk_size=65536):
        for chunk in self.chunks if self.chunks is not None else [self.data]:
            yield chunk


class Http:
    def __init__(self, response=None, history=None):
        self.response = response or Response()
        self.history = history or []
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(json.dumps({"ok": True, "messages": self.history}).encode(), mime="application/json")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mod.register(_Ctx())
        self.adapter = mod.NaoNaoAdapter(PlatformConfig(extra={"bot_token": "nn_bot_test"}))
        self.adapter._bot_name = "测试机器人"
        self.http = Http()
        self.adapter._http = self.http
        self.cache = AsyncMock(return_value="/mock/cache/image.png")
        self.patcher = patch("gateway.platforms.base.cache_image_from_bytes_async", self.cache)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.payload = _mention(actor_id="user1")
        self.payload["message"]["createdAt"] = datetime.now(timezone.utc).isoformat()

    async def enrich(self):
        event = self.adapter.build_mention_event(self.payload, 1)
        return await self.adapter.enrich_mention_event(event, self.payload)

    def direct(self, images=None):
        self.payload["message"]["attachments"] = images or [attachment()]

    def history(self, author=None, age=20, mid="parent1", **extra):
        return {"id": mid, "channelId": "channel_x", "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                "author": author or {"id": "user1", "name": "member"}, "attachments": [attachment()], **extra}

    def ask_image(self):
        self.payload["message"]["content"] = "@测试机器人 看看截图"

    async def test_direct_native_event_and_cache(self):
        self.direct()
        event = await self.enrich()
        self.assertIsInstance(event, MessageEvent)
        self.assertEqual(event.message_type, MessageType.PHOTO)
        self.assertEqual(event.media_urls, ["/mock/cache/image.png"])
        self.assertEqual(event.media_types, ["image/png"])
        self.cache.assert_awaited_once_with(PNG, ".png")
        self.assertEqual(self.http.calls[0][2]["headers"]["Authorization"], "Bot nn_bot_test")
        self.assertIn("/channel_x/messages/msg_1/attachments/0", self.http.calls[0][1])

    async def test_reply_image_and_native_reply_fields(self):
        self.payload["message"]["reply"] = {"messageId": "parent1", "excerpt": "原图", "authorName": "Amy", "authorId": "user2", "attachments": [attachment()]}
        event = await self.enrich()
        self.assertEqual(event.reply_to_message_id, "parent1")
        self.assertEqual(event.reply_to_text, "原图")
        self.assertEqual(event.reply_to_author_id, "user2")
        self.assertEqual(event.reply_to_author_name, "Amy")
        self.assertIn("/parent1/attachments/0", self.http.calls[0][1])
        self.assertEqual(len(event.media_urls), 1)

    async def test_history_same_sender(self):
        self.ask_image()
        self.http.history = [self.history(author={"id": "other", "name": "Other"}), self.history()]
        event = await self.enrich()
        self.assertEqual(len(event.media_urls), 1)
        self.assertTrue(self.http.calls[0][1].endswith("messages?limit=10"))
        self.assertEqual(len(self.http.calls), 2)

    async def test_history_display_name_alone_is_not_identity(self):
        self.ask_image()
        self.http.history = [self.history(author={"name": "member"})]
        self.assertEqual((await self.enrich()).media_urls, [])
        self.cache.assert_not_awaited()

    async def test_history_legacy_username(self):
        self.ask_image()
        self.http.history = [self.history(author={"username": "member"})]
        self.assertEqual(len((await self.enrich()).media_urls), 1)

    async def test_history_multiple_messages_ambiguous_no_download(self):
        self.ask_image()
        self.http.history = [self.history(mid="a"), self.history(mid="b", age=30)]
        event = await self.enrich()
        self.assertFalse(event.media_urls)
        self.assertEqual(len(self.http.calls), 1)
        self.assertIn("ambiguous", event.channel_prompt)

    async def test_history_other_sender_no_download(self):
        self.ask_image()
        self.http.history = [self.history(author={"id": "other", "name": "member"})]
        self.assertFalse((await self.enrich()).media_urls)
        self.assertEqual(len(self.http.calls), 1)

    async def test_history_legacy_collision_no_download(self):
        self.ask_image()
        self.http.history = [self.history(author={"name": "member"}), self.history(author={"id": "other", "name": "member"}, mid="b")]
        self.assertFalse((await self.enrich()).media_urls)
        self.assertEqual(len(self.http.calls), 1)

    async def test_history_old_future_wrong_channel_no_download(self):
        self.ask_image()
        self.http.history = [self.history(age=301), self.history(age=-10), self.history(channelId="elsewhere")]
        self.assertFalse((await self.enrich()).media_urls)
        self.assertEqual(len(self.http.calls), 1)

    async def test_history_image_after_trigger_is_not_selected(self):
        self.ask_image()
        self.http.history = [self.history(age=1)]
        event = self.adapter.build_mention_event(self.payload, 1)
        event.timestamp = datetime.now(timezone.utc) - timedelta(seconds=60)
        await self.adapter.enrich_mention_event(event, self.payload)
        self.assertEqual(event.media_urls, [])
        self.assertEqual(len(self.http.calls), 1)
        self.cache.assert_not_awaited()

    async def test_history_no_image_keyword_no_request(self):
        self.assertFalse((await self.enrich()).media_urls)
        self.assertFalse(self.http.calls)

    async def test_explicit_attachment_blocks_history(self):
        self.ask_image()
        self.payload["message"]["hasAttachments"] = True
        event = await self.enrich()
        self.assertFalse(self.http.calls)
        self.assertIn("failed", event.channel_prompt)

    async def test_reply_blocks_history(self):
        self.ask_image()
        self.payload["message"]["replyToMessageId"] = "parent1"
        event = await self.enrich()
        self.assertFalse(self.http.calls)
        self.assertIn("failed", event.channel_prompt)

    async def test_old_text_payload_sync_compatibility(self):
        event = self.adapter.build_mention_event(self.payload, 1)
        self.assertEqual(event.message_type, MessageType.TEXT)
        self.assertFalse(event.media_urls)
        self.assertFalse(self.http.calls)
        self.assertIsNone((await self.enrich()).channel_prompt)

    async def test_old_attachment_flags_no_fake_vision(self):
        self.payload["message"]["attachmentCount"] = 1
        event = await self.enrich()
        self.assertEqual(event.message_type, MessageType.TEXT)
        self.assertIn("Do not claim", event.channel_prompt)
        self.cache.assert_not_awaited()

    async def test_three_images_max(self):
        self.direct([attachment(i) for i in range(5)])
        event = await self.enrich()
        self.assertEqual(len(event.media_urls), 3)
        self.assertEqual(len(self.http.calls), 3)
        self.assertIn("remaining images unavailable", event.channel_prompt)

    async def test_oversized_metadata_no_download(self):
        self.direct([attachment(size=mod.MAX_IMAGE_BYTES + 1)])
        self.assertFalse((await self.enrich()).media_urls)
        self.assertFalse(self.http.calls)

    async def test_oversized_content_length(self):
        self.direct()
        self.http.response = Response(headers={"content-length": str(mod.MAX_IMAGE_BYTES + 1)})
        self.assertFalse((await self.enrich()).media_urls)
        self.cache.assert_not_awaited()

    async def test_streaming_limit_without_length(self):
        self.direct()
        self.http.response = Response(chunks=[b"x" * 6, b"y" * 6])
        with patch.object(mod, "MAX_IMAGE_BYTES", 10):
            self.payload["message"]["attachments"][0].pop("size")
            self.assertFalse((await self.enrich()).media_urls)
        self.cache.assert_not_awaited()

    async def test_total_budget_limits_images(self):
        self.direct([attachment(i) for i in range(3)])
        with patch.object(mod, "MAX_IMAGE_TOTAL_BYTES", len(PNG) + 1):
            event = await self.enrich()
        self.assertEqual(len(event.media_urls), 1)
        self.assertIn("failed", event.channel_prompt)
        self.assertEqual(len(self.http.calls), 1)

    async def test_permission_denied_no_fallback(self):
        self.direct()
        self.http.response = Response(status=403)
        event = await self.enrich()
        self.assertFalse(event.media_urls)
        self.assertIn("permission", event.channel_prompt)
        self.assertEqual(len(self.http.calls), 1)

    async def test_history_permission_denied_no_download(self):
        self.ask_image()
        self.http.request = AsyncMock(return_value=Response(b'{"ok":false,"err":"forbidden"}', status=403, mime="application/json"))
        self.assertFalse((await self.enrich()).media_urls)
        self.cache.assert_not_awaited()

    async def test_timeout_no_fake_vision(self):
        self.direct()
        async def stalled(*args):
            await asyncio.sleep(1)
        with patch.object(self.adapter, "_download_image", stalled), patch.object(mod, "IMAGE_TIMEOUT_SECONDS", .01):
            event = await self.enrich()
        self.assertFalse(event.media_urls)
        self.assertIn("network", event.channel_prompt)

    async def test_bad_magic_rejected(self):
        self.direct()
        self.http.response = Response(b"<html>not an image</html>")
        self.assertFalse((await self.enrich()).media_urls)
        self.cache.assert_not_awaited()

    async def test_magic_determines_mime_not_event(self):
        self.direct([attachment(mimeType="image/jpeg")])
        self.assertEqual((await self.enrich()).media_types, ["image/png"])

    async def test_payload_url_never_used(self):
        self.direct([attachment(url="http://127.0.0.1/private")])
        await self.enrich()
        self.assertEqual(len(self.http.calls), 1)
        self.assertNotIn("127.0.0.1", self.http.calls[0][1])

    def signed(self, **extra):
        self.direct()
        self.http.response = Response(json.dumps({"ok": True, "url": "https://cdn.example/test?sig=secret", "expiresIn": 60, "size": len(PNG), **extra}).encode(), mime="application/json")

    async def test_signed_download_has_no_bot_token(self):
        self.signed()
        client = Http()
        with patch.object(mod, "_validate_image_url", AsyncMock()), patch.object(mod, "_open_image_http", return_value=client):
            event = await self.enrich()
        self.assertEqual(len(event.media_urls), 1)
        self.assertNotIn("Authorization", client.calls[0][2]["headers"])
        self.assertFalse(client.calls[0][2]["follow_redirects"])

    async def test_permanent_url_rejected(self):
        for expiry in (None, 0, -1, 901, "60", True, float("inf"), float("nan")):
            with self.subTest(expiry=expiry):
                self.signed(expiresIn=expiry)
                with patch.object(mod, "_open_image_http") as factory:
                    self.assertFalse((await self.enrich()).media_urls)
                    factory.assert_not_called()

    async def test_signed_ssrf_rejected(self):
        self.signed(url="http://169.254.169.254/latest")
        with patch.object(mod, "_open_image_http") as factory:
            self.assertFalse((await self.enrich()).media_urls)
            factory.assert_not_called()

    async def test_private_dns_rejected(self):
        for address in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"):
            with self.subTest(address=address), patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", (address, 443))]):
                with self.assertRaises(ValueError):
                    await mod._validate_image_url("https://cdn.example/test")

    async def test_dns_failure_fails_closed(self):
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror):
            with self.assertRaises(socket.gaierror):
                await mod._validate_image_url("https://cdn.example/test")

    async def test_redirect_refused_without_following(self):
        self.signed()
        client = Http(Response(status=302, headers={"location": "http://127.0.0.1/private"}))
        with patch.object(mod, "_validate_image_url", AsyncMock()), patch.object(mod, "_open_image_http", return_value=client):
            self.assertFalse((await self.enrich()).media_urls)
        self.assertEqual(len(client.calls), 1)
        self.cache.assert_not_awaited()

    async def test_api_redirect_refused(self):
        self.direct()
        self.http.response = Response(status=302, headers={"location": "https://elsewhere.example"})
        self.assertFalse((await self.enrich()).media_urls)
        self.assertFalse(self.http.calls[0][2]["follow_redirects"])

    async def test_invalid_attachment_index(self):
        for index in (-1, 3, 32, "0", True):
            self.direct([attachment(index=index)])
            self.assertFalse((await self.enrich()).media_urls)
        self.assertFalse(self.http.calls)

    async def test_dispatch_enriches_before_handle(self):
        self.direct()
        received = []
        async def handle(event):
            received.append(event)
            event._gateway_accepted = True
        self.adapter.handle_message = handle
        sock = type("Sock", (), {"send": AsyncMock()})()
        await self.adapter._on_event(sock, {"t": "message.bot_mention", "s": 1, "d": self.payload})
        self.assertEqual(received[0].message_type, MessageType.PHOTO)
        sock.send.assert_awaited_once()

    async def test_direct_images_take_priority_over_reply(self):
        self.direct()
        self.payload["message"]["reply"] = {"messageId": "parent", "attachments": [attachment()]}
        await self.enrich()
        self.assertEqual(len(self.http.calls), 1)
        self.assertIn("/msg_1/attachments/0", self.http.calls[0][1])

    async def test_deleted_reply_keeps_native_context(self):
        self.payload["message"]["reply"] = {"messageId": "parent", "deleted": True}
        event = self.adapter.build_mention_event(self.payload, 1)
        self.assertEqual(event.reply_to_text, "(original message unavailable)")
        self.assertFalse(self.http.calls)

    async def test_cache_failure_never_reports_photo(self):
        self.direct()
        self.cache.side_effect = ValueError("cache failed")
        event = await self.enrich()
        self.assertFalse(event.media_urls)
        self.assertEqual(event.message_type, MessageType.TEXT)
        self.assertIn("failed", event.channel_prompt)

    async def test_signed_oversize_rejected_before_external_request(self):
        self.signed(size=mod.MAX_IMAGE_BYTES + 1)
        with patch.object(mod, "_open_image_http") as factory:
            self.assertFalse((await self.enrich()).media_urls)
            factory.assert_not_called()

    async def test_auth_failure_does_not_fallback(self):
        self.direct()
        self.http.response = Response(status=401)
        with patch.object(self.adapter, "_fatal_auth", AsyncMock()) as fatal:
            event = await self.enrich()
            fatal.assert_awaited_once()
        self.assertFalse(event.media_urls)
        self.assertEqual(len(self.http.calls), 1)

    async def test_all_supported_magic_formats(self):
        for data, mime in ((b"\xff\xd8\xff" + b"data", "image/jpeg"),
                           (b"GIF89a" + b"data", "image/gif"),
                           (b"RIFF1234WEBPdata", "image/webp")):
            with self.subTest(mime=mime):
                self.direct()
                self.http.response = Response(data)
                self.assertEqual((await self.enrich()).media_types, [mime])

    async def test_strict_backend_rechecks_dns(self):
        client = mod._open_image_http()
        try:
            backend = client._transport._pool._network_backend
            with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]):
                with self.assertRaises(ValueError):
                    backend._connect_ips("cdn.example", 443)
            self.assertFalse(client.follow_redirects)
            self.assertNotIn("authorization", client.headers)
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
