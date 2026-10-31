"""Tests: response_language is deprecated and must not inject language policy.

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

_UNSET = adapter_mod._RESPONSE_LANGUAGE_UNSET


def _ensure_naonao_platform_registered():
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
         unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=_UNSET), \
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


def _load_plugin_yaml():
    try:
        import yaml
    except ImportError:
        from utils import fast_safe_load

        return fast_safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))


class ResponseLanguageDeprecatedTest(unittest.TestCase):
    def test_normalize_compat_still_accepts_legacy_values(self):
        self.assertEqual(adapter_mod.normalize_response_language("zh-CN"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("en"), "en")
        self.assertEqual(adapter_mod.normalize_response_language("auto"), "auto")
        self.assertEqual(adapter_mod.normalize_response_language("nope"), "zh-CN")

    def test_language_instruction_always_empty(self):
        for lang in ("zh-CN", "en", "auto", "", None):
            self.assertEqual(adapter_mod.language_preference_instruction(lang), "")

    def test_platform_hint_has_no_language_policy(self):
        for lang in ("zh-CN", "en", "auto"):
            hint = adapter_mod.build_naonao_platform_hint(lang)
            self.assertEqual(hint, adapter_mod.NAONAO_CAPABILITY_HINT)
            self.assertNotIn("default response language", hint)
            self.assertNotIn("Simplified Chinese", hint)
            self.assertNotIn("Use English", hint)
            self.assertNotIn("regardless of the language", hint)

    def test_schema_keeps_response_language_for_compat(self):
        data = _load_plugin_yaml()
        self.assertEqual(str(data.get("version")), "1.4.0")
        schema = (data.get("config_schema") or {}).get("response_language") or {}
        self.assertEqual(schema.get("type"), "str")
        self.assertEqual(list(schema.get("choices") or []), ["auto", "zh-CN", "en"])
        desc = str(schema.get("description") or "").lower()
        self.assertIn("deprecated", desc)
        self.assertIn("ignored", desc)

    def test_old_config_does_not_break_resolve(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value="en"), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value="zh-CN"):
            self.assertEqual(adapter_mod.resolve_naonao_response_language(), "zh-CN")

    def test_zh_cn_user_message_forwarded_unchanged_without_language_prompt(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value="zh-CN"):
            ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra={"response_language": "zh-CN"}))
        ad._bot_id = "bot_1"
        ad._bot_name = "bot"
        event = ad.build_mention_event(_mention("@bot 你好，你能做什么？"), 1)
        self.assertIsNotNone(event)
        self.assertIn("你好", event.text)
        self.assertIsNone(event.channel_prompt)
        self.assertNotIn("Simplified Chinese", event.text)
        self.assertNotIn("default response language", event.text or "")

    def test_english_user_message_forwarded_unchanged_without_language_prompt(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value="en"):
            ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra={"response_language": "en"}))
        ad._bot_id = "bot_1"
        ad._bot_name = "bot"
        event = ad.build_mention_event(_mention("@bot What can you do?"), 1)
        self.assertIsNotNone(event)
        self.assertIn("What can you do?", event.text)
        self.assertIsNone(event.channel_prompt)
        self.assertNotIn("Use English as the default response language", event.channel_prompt or "")

    def test_register_platform_hint_has_no_language_injection(self):
        captured = {}

        class _Ctx:
            def get_config(self, key, default=None):
                if key == "response_language":
                    return "zh-CN"
                return default

            def register_platform(self, **kwargs):
                captured.update(kwargs)

        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value="zh-CN"), \
             unittest.mock.patch.object(adapter_mod, "_install_naonao_display_defaults"):
            adapter_mod.register(_Ctx())
        hint = captured.get("platform_hint") or ""
        self.assertEqual(hint, adapter_mod.NAONAO_CAPABILITY_HINT)
        self.assertNotIn("default response language", hint)


if __name__ == "__main__":
    unittest.main()
