"""Tests: response_language (1.3.3 behavior restored in 1.4.0; explicit-only).

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


ZH = "Use Simplified Chinese (简体中文) as the default response language"
EN = "Use English as the default response language"


def _adapter(plugin_value=_UNSET, platform_value=None, extra=None):
    with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=platform_value), \
         unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=plugin_value):
        ad = adapter_mod.NaoNaoAdapter(PlatformConfig(enabled=True, extra=dict(extra or {})))
    ad._bot_id = "bot_1"
    ad._bot_name = "bot"
    return ad


def _command():
    return {
        "id": "evt_c", "createdAt": "2026-01-01T00:00:00.000Z",
        "invocation": {"id": "inv_1", "command": {"name": "help"}},
        "channel": {"id": "channel_x"}, "community": {"id": "community_a"},
        "actor": {"id": "507f1f77bcf86cd799439011", "username": "alice"},
        "bot": {"id": "bot_1", "name": "bot"}, "installation": {"id": "inst_1"},
    }


def _register(get_config_value=_UNSET, plugin_value=_UNSET, platform_value=None):
    captured = {}

    class _Ctx:
        def get_config(self, key, default=None):
            if key == "response_language" and get_config_value is not _UNSET:
                return get_config_value
            return default

        def register_platform(self, **kwargs):
            captured.update(kwargs)

    with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=platform_value), \
         unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=plugin_value), \
         unittest.mock.patch.object(adapter_mod, "_install_naonao_display_defaults"):
        adapter_mod.register(_Ctx())
    return captured.get("platform_hint") or ""


class ResponseLanguageTest(unittest.TestCase):
    def test_normalize_accepts_legacy_values(self):
        self.assertEqual(adapter_mod.normalize_response_language("zh-CN"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("zh_cn"), "zh-CN")
        self.assertEqual(adapter_mod.normalize_response_language("en-US"), "en")
        self.assertEqual(adapter_mod.normalize_response_language("auto"), "auto")
        self.assertEqual(adapter_mod.normalize_response_language("nope"), "zh-CN")

    def test_instruction_by_value(self):
        self.assertIn(ZH, adapter_mod.language_preference_instruction("zh-CN"))
        self.assertIn(EN, adapter_mod.language_preference_instruction("en"))
        for value in ("auto", "", None):
            self.assertEqual(adapter_mod.language_preference_instruction(value), "")

    def test_wording_identical_to_1_3_3(self):
        self.assertEqual(
            adapter_mod.language_preference_instruction("zh-CN"),
            "Use Simplified Chinese (简体中文) as the default response language, "
            "regardless of the language of the user's message. "
            "If the user explicitly requests a different language, follow that explicit request for that response.",
        )

    def test_resolve_unconfigured_is_empty_not_zh(self):
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value=None), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=_UNSET):
            self.assertEqual(adapter_mod.resolve_naonao_response_language(), "")

    def test_resolve_priority_plugin_over_platform(self):
        # Production shape: plugin settings zh-CN + legacy platforms.naonao en -> zh-CN.
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value="en"), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value="zh-CN"):
            self.assertEqual(adapter_mod.resolve_naonao_response_language(), "zh-CN")
        with unittest.mock.patch.object(adapter_mod, "_read_platforms_naonao_response_language", return_value="en"), \
             unittest.mock.patch.object(adapter_mod, "_read_plugin_settings_response_language", return_value=_UNSET):
            self.assertEqual(adapter_mod.resolve_naonao_response_language(), "en")

    def test_zh_cn_mention_gets_soft_chinese_prompt_text_untouched(self):
        event = _adapter(plugin_value="zh-CN").build_mention_event(_mention("@bot What can you do?"), 1)
        self.assertIn(ZH, event.channel_prompt)
        self.assertEqual(event.text, "What can you do?")
        self.assertNotIn("简体中文", event.text)

    def test_en_mention_gets_soft_english_prompt(self):
        event = _adapter(plugin_value="en").build_mention_event(_mention("@bot 你好"), 1)
        self.assertIn(EN, event.channel_prompt)
        self.assertNotIn(ZH, event.channel_prompt)

    def test_auto_and_unconfigured_have_no_prompt(self):
        for ad in (_adapter(plugin_value="auto"), _adapter()):
            event = ad.build_mention_event(_mention("@bot 你好，你能做什么？"), 1)
            self.assertIsNone(event.channel_prompt)
            self.assertIn("你好", event.text)

    def test_legacy_platform_extra_only(self):
        event = _adapter(extra={"response_language": "zh-CN"}).build_mention_event(_mention(), 1)
        self.assertIn(ZH, event.channel_prompt)

    def test_command_event_gets_same_prompt(self):
        self.assertIn(ZH, _adapter(plugin_value="zh-CN").build_command_event(_command(), 1).channel_prompt)
        self.assertIsNone(_adapter().build_command_event(_command(), 1).channel_prompt)

    def test_register_platform_hint(self):
        self.assertIn(ZH, _register(get_config_value="zh-CN"))
        self.assertTrue(_register(get_config_value="zh-CN").startswith(adapter_mod.NAONAO_CAPABILITY_HINT))
        self.assertIn(EN, _register(get_config_value="en"))
        self.assertEqual(_register(get_config_value="auto"), adapter_mod.NAONAO_CAPABILITY_HINT)
        self.assertEqual(_register(), adapter_mod.NAONAO_CAPABILITY_HINT)

    def test_schema_keeps_response_language(self):
        data = _load_plugin_yaml()
        self.assertEqual(str(data.get("version")), "1.4.0")
        schema = (data.get("config_schema") or {}).get("response_language") or {}
        self.assertEqual(schema.get("type"), "str")
        self.assertEqual(list(schema.get("choices") or []), ["auto", "zh-CN", "en"])
        self.assertNotIn("ignored", str(schema.get("description") or "").lower())


class LanguageWithImageContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_status_appends_after_language_prompt(self):
        ad = _adapter(plugin_value="zh-CN")
        event = ad.build_mention_event(_mention(), 1)
        ad._image_context(event, "image loading failed: x")
        lines = event.channel_prompt.split("\n")
        self.assertIn(ZH, lines[0])
        self.assertIn("NaoNao image input status", lines[1])


if __name__ == "__main__":
    unittest.main()
