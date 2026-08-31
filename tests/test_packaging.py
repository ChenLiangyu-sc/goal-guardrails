from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_manifest_points_to_canonical_skill_and_default_hook_exists(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("goal-guardrails", manifest["name"])
        self.assertEqual("0.7.0", manifest["version"])
        self.assertEqual("./skills/", manifest["skills"])
        self.assertTrue((ROOT / "skills/goal-guardrails/SKILL.md").is_file())
        self.assertTrue((ROOT / "hooks/hooks.json").is_file())
        helper = ROOT / "hooks/remote_submit_helper.py"
        self.assertTrue(helper.is_file())
        self.assertTrue(helper.stat().st_mode & 0o100)
        spec = importlib.util.spec_from_file_location("goal_guard_packaging", ROOT / "hooks/goal_guard.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(manifest["version"], module.HOOK_VERSION)

    def test_hook_handlers_use_plugin_root_and_supported_events(self) -> None:
        payload = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
        hooks = payload["hooks"]
        self.assertEqual({"SessionStart", "PreToolUse", "PostToolUse"}, set(hooks))
        handlers = [handler for groups in hooks.values() for group in groups for handler in group["hooks"]]
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertEqual("command", handler["type"])
            self.assertIn("${PLUGIN_ROOT}/hooks/goal_guard.py", handler["command"])

    def test_public_marketplace_targets_plugin_at_repository_root(self) -> None:
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        entry = marketplace["plugins"][0]
        self.assertEqual("goal-guardrails", entry["name"])
        self.assertEqual("url", entry["source"]["source"])
        self.assertTrue(entry["source"]["url"].endswith("/goal-guardrails.git"))


if __name__ == "__main__":
    unittest.main()
