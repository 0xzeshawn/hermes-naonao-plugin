"""使用实际 Hermes 图像路由及内容构造器验证原生视觉协议；不调用模型。"""
import base64
import tempfile
import unittest
from pathlib import Path

from test_adapter import adapter_mod  # 将当前 Hermes checkout 加入导入路径
from agent.image_routing import build_native_content_parts, decide_image_input_mode


class NativeVisionContractTests(unittest.TestCase):
    def test_model_supports_vision_is_under_model(self):
        cfg = {"model": {"supports_vision": True}}
        self.assertEqual(decide_image_input_mode("custom", "local-test-model", cfg), "native")

    def test_explicit_native_mode_remains_available(self):
        cfg = {"agent": {"image_input_mode": "native"}}
        self.assertEqual(decide_image_input_mode("custom", "local-test-model", cfg), "native")

    def test_cached_image_becomes_real_base64_image_part(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.png"
            path.write_bytes(png)
            parts, skipped = build_native_content_parts("请识别图片", [str(path)])
        self.assertEqual(skipped, [])
        self.assertEqual([part["type"] for part in parts], ["text", "image_url"])
        image_url = parts[1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(image_url.split(",", 1)[1]), png)

    def test_missing_cache_file_does_not_fabricate_image_part(self):
        parts, skipped = build_native_content_parts("请识别图片", ["/nonexistent/naonao-image.png"])
        self.assertEqual([part["type"] for part in parts], ["text"])
        self.assertEqual(len(skipped), 1)
