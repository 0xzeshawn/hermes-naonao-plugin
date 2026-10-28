"""Tests for NaoNao soft response_language preference.

Run from this plugin directory with Hermes on PYTHONPATH:

    HERMES_AGENT_ROOT=/path/to/hermes-agent python -m unittest tests.test_response_language
"""

from __future__ import annotations

import importlib.util
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
    spec = importlib.util.spec_from_file_location("naonao_platform_adapter_lang", PLUGIN_DIR / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter_mod = _load_adapter()
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry


def _ensure_naonao_platform_registered():
    """Platform('naonao') is dynamic; register once so NaoNaoAdapter can construct."""
    if platform_registry.get("naonao") is not None:
        return

    class _Ctx:
        def get_config(self, key, default=None):
            return default

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

    with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
         unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None), \
         unittest.mock.patch.object(adapter_mod, "_install_naonao_display_defaults"):
        adapter_mod.register(_Ctx())


_ensure_naonao_platform_registered()


def _mention(content="@bot hello"):
    return {
        "id": "evt_1",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "message": {"id": "msg_1", "content": content, "createdAt": "2026-01-01T00:00:00.000Z"},
        "channel": {"id": "channel_x"},
        "community": {"id": "community_a"},
        "actor": {"id": "507f1f77bcf86cd799439011", "username": "alice"},
        "bot": {"id": "bot_1", "name": "bot"},
        "installation": {"id": "inst_1"},
    }


class ResponseLanguageHelpersTest(unittest.TestCase):
    def test_default_and_missing(self):
        self.assertEqual(adapter_mod.normalize_response_language(None), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language(""), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("   "), "zh-CN")

    def test_canonical_values(self):
        self.assertEqual(adapter_mod.normalize_response_language("zh-CN"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("en"), "en")
        self.assertEqual(adapter_mod.normalize_response_language("auto"), "auto")

    def test_case_normalization(self):
        self.assertEqual(adapter_mod.normalize_response_language("zh-cn"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("ZH-CN"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("EN"), "en")
        self.assertEqual(adapter_mod.normalize_response_language("Auto"), "auto")

    def test_invalid_falls_back_zh_cn(self):
        self.assertEqual(adapter_mod.normalize_response_language("abc"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language(123), "zh-CN")

    def test_zh_cn_prompt_soft_preference(self):
        text = adapter_mod.language_preference_instruction("zh-CN")
        self.assertIn("Simplified Chinese", text)
        self.assertIn("explicitly requests", text)
        self.assertNotIn("Always", text)
        self.assertNotIn("Never", text)

    def test_en_prompt_soft_preference(self):
        text = adapter_mod.language_preference_instruction("en")
        self.assertIn("Prefer English", text)
        self.assertIn("explicitly requests", text)
        self.assertNotIn("Always", text)

    def test_auto_has_no_fixed_preference(self):
        self.assertEqual(adapter_mod.language_preference_instruction("auto"), "")
        hint = adapter_mod.build_naonao_platform_hint("auto")
        self.assertEqual(hint, adapter_mod.NAONAO_CAPABILITY_HINT)
        self.assertNotIn("Simplified Chinese", hint)
        self.assertNotIn("Prefer English", hint)

    def test_platform_hint_includes_zh_cn_by_default(self):
        hint = adapter_mod.build_naonao_platform_hint("zh-CN")
        self.assertIn("NaoNao community channel", hint)
        self.assertIn("Simplified Chinese", hint)
        self.assertIn("explicitly requests", hint)


class ResponseLanguageRuntimeTest(unittest.TestCase):
    def test_adapter_default_missing_extra_is_zh_cn(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None):
            ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra={}))
        self.assertEqual(ad._response_language, "zh-CN")
        event = ad.build_mention_event(_mention(), 1)
        self.assertIsNotNone(event)
        self.assertIn("Simplified Chinese", event.channel_prompt or "")

    def test_adapter_en_from_extra(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None):
            ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra={"response_language": "en"}))
        self.assertEqual(ad._response_language, "en")
        event = ad.build_mention_event(_mention(), 1)
        self.assertIn("Prefer English", event.channel_prompt or "")
        self.assertNotIn("Simplified Chinese", event.channel_prompt or "")

    def test_adapter_auto_clears_channel_prompt(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None):
            ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra={"response_language": "auto"}))
        event = ad.build_mention_event(_mention(), 1)
        self.assertIsNone(event.channel_prompt)

    def test_platforms_block_wins_over_plugin_settings(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value="en"), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value="zh-CN"):
            lang = adapter_mod.resolve_naonao_response_language()
        self.assertEqual(lang, "en")

    def test_register_platform_hint_default_zh_cn(self):
        class _Ctx:
            def __init__(self):
                self.kwargs = {}

            def get_config(self, key, default=None):
                return default

            def register_platform(self, **kwargs):
                self.kwargs = kwargs

        ctx = _Ctx()
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_install_naonao_display_defaults"):
            adapter_mod.register(ctx)
        hint = ctx.kwargs.get("platform_hint") or ""
        self.assertIn("Simplified Chinese", hint)
        self.assertIn("NaoNao community channel", hint)
        self.assertEqual(ctx.kwargs.get("name"), "naonao")

    def test_register_auto_omits_language_lock(self):
        class _Ctx:
            def __init__(self):
                self.kwargs = {}

            def get_config(self, key, default=None):
                return "auto" if key == "response_language" else default

            def register_platform(self, **kwargs):
                self.kwargs = kwargs

        ctx = _Ctx()
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_install_naonao_display_defaults"):
            adapter_mod.register(ctx)
        hint = ctx.kwargs.get("platform_hint") or ""
        self.assertNotIn("Simplified Chinese", hint)
        self.assertNotIn("Prefer English", hint)

    def test_reply_mapping_unchanged_with_language_prompt(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=None):
            ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra={}))
        payload = _mention()
        payload["message"]["replyToMessageId"] = "parent_1"
        payload["message"]["reply"] = {
            "messageId": "parent_1",
            "excerpt": "你好！我是你的专属助手",
            "authorId": "bot_1",
            "authorName": "my bot",
            "authorType": "bot",
        }
        ad._bot_id = "bot_1"
        event = ad.build_mention_event(payload, 1)
        self.assertEqual(event.reply_to_message_id, "parent_1")
        self.assertIn("专属助手", event.reply_to_text or "")
        self.assertTrue(event.reply_to_is_own_message)
        self.assertNotIn("reply", event.metadata)
        self.assertIn("Simplified Chinese", event.channel_prompt or "")


if __name__ == "__main__":
    unittest.main()
