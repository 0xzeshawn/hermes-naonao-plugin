"""Lightweight capability markers for NaoNao plugin v1.3.2+."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapter.py"
MANIFEST = ROOT / "plugin.yaml"


class CapabilityParityTests(unittest.TestCase):
    def test_plugin_yaml_version_is_1_3_2(self) -> None:
        text = MANIFEST.read_text(encoding="utf-8")
        m = re.search(r"(?m)^version:\s*([^\s#]+)", text)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1.3.2")

    def test_supports_message_editing_true(self) -> None:
        tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
        found = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                        and item.targets[0].id == "SUPPORTS_MESSAGE_EDITING"
                    ):
                        found = item.value
        self.assertIsNotNone(found)
        self.assertIsInstance(found, ast.Constant)
        self.assertIs(found.value, True)

    def test_edit_message_method_exists(self) -> None:
        tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
        methods = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.add(item.name)
        self.assertIn("edit_message", methods)
        self.assertIn("send", methods)
        self.assertIn("send_typing", methods)
        self.assertIn("stop_typing", methods)
        self.assertIn("on_processing_start", methods)
        self.assertIn("on_processing_complete", methods)
        self.assertIn("set_status_text", methods)
        self.assertIn("_delete_reaction", methods)
        self.assertIn("_put_reaction", methods)

    def test_ack_reaction_constant(self) -> None:
        text = ADAPTER.read_text(encoding="utf-8")
        self.assertIn('ACK_REACTION_EMOJI = "👀"', text)

    def test_supports_status_text_true(self) -> None:
        tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
        found = None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "NaoNaoAdapter":
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                        and item.targets[0].id == "supports_status_text"
                    ):
                        found = item.value
        self.assertIsNotNone(found)
        self.assertIsInstance(found, ast.Constant)
        self.assertIs(found.value, True)


if __name__ == "__main__":
    unittest.main()
