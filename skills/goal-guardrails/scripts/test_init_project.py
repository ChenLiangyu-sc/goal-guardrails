from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import init_project


class InitProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_templates_and_agents_fragment(self) -> None:
        messages = init_project.apply_project(self.root, dry_run=False)
        self.assertEqual(len(init_project.TARGETS) + 1, len(messages))
        for relative in init_project.TARGETS.values():
            self.assertTrue((self.root / relative).is_file())
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(1, agents.count(init_project.START_MARKER))
        self.assertEqual(1, agents.count(init_project.END_MARKER))

    def test_state_template_respects_declared_default_cap(self) -> None:
        init_project.apply_project(self.root, dry_run=False)
        goal = (self.root / "optimization/GOAL.md").read_text(encoding="utf-8")
        state = (self.root / "optimization/STATE.md").read_text(encoding="utf-8")
        self.assertIn("`STATE.md` maximum nonblank lines: `25`", goal)
        self.assertLessEqual(sum(bool(line.strip()) for line in state.splitlines()), 25)

    def test_gate_starts_disabled_and_machine_state_is_created(self) -> None:
        init_project.apply_project(self.root, dry_run=False)
        gate = (self.root / "optimization/GATE.json").read_text(encoding="utf-8")
        control = (self.root / "optimization/CONTROL.json").read_text(encoding="utf-8")
        pre_run = (self.root / "optimization/PRE_RUN_RESULTS.json").read_text(encoding="utf-8")
        self.assertIn('"enabled": false', gate)
        self.assertIn('"active_lease": null', control)
        self.assertIn('"state": "ACTIVE"', control)
        self.assertIn('"schema_version": 2', pre_run)
        self.assertEqual(str(self.root), json.loads(gate)["project_root"])

    def test_rerun_is_idempotent(self) -> None:
        init_project.apply_project(self.root, dry_run=False)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        messages = init_project.apply_project(self.root, dry_run=False)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(all(message.startswith("skip-") for message in messages))

    def test_preserves_existing_files_and_agents_content(self) -> None:
        goal = self.root / "optimization/GOAL.md"
        goal.parent.mkdir()
        goal.write_text("custom goal\n", encoding="utf-8")
        agents = self.root / "AGENTS.md"
        agents.write_text("# Existing guidance\n\nKeep this.\n", encoding="utf-8")
        init_project.apply_project(self.root, dry_run=False)
        self.assertEqual("custom goal\n", goal.read_text(encoding="utf-8"))
        updated = agents.read_text(encoding="utf-8")
        self.assertTrue(updated.startswith("# Existing guidance\n\nKeep this.\n"))
        self.assertIn(init_project.START_MARKER, updated)

    def test_dry_run_does_not_write(self) -> None:
        messages = init_project.apply_project(self.root, dry_run=True)
        self.assertEqual(len(init_project.TARGETS) + 1, len(messages))
        self.assertEqual([], list(self.root.iterdir()))

    def test_incomplete_agents_marker_fails_closed(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text(init_project.START_MARKER + "\n", encoding="utf-8")
        with self.assertRaises(init_project.InitError):
            init_project.apply_project(self.root, dry_run=False)
        self.assertFalse((self.root / "optimization").exists())

    def test_legacy_agents_marker_is_not_duplicated(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text(
            init_project.LEGACY_START_MARKER + "\nlegacy rules\n" + init_project.LEGACY_END_MARKER + "\n",
            encoding="utf-8",
        )
        messages = init_project.apply_project(self.root, dry_run=False)
        self.assertTrue(messages[-1].startswith("skip-marked"))
        updated = agents.read_text(encoding="utf-8")
        self.assertNotIn(init_project.START_MARKER, updated)
        self.assertEqual(1, updated.count(init_project.LEGACY_START_MARKER))

    def test_main_handles_multiple_projects(self) -> None:
        second = self.root / "second"
        second.mkdir()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = init_project.main([str(self.root), str(second)])
        self.assertEqual(0, result)
        self.assertTrue((self.root / "optimization/GOAL.md").exists())
        self.assertTrue((second / "optimization/GOAL.md").exists())

    def test_rejects_optimization_directory_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        project = self.root / "project"
        project.mkdir()
        (project / "optimization").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(init_project.InitError):
            init_project.apply_project(project, dry_run=False)
        self.assertEqual([], list(outside.iterdir()))

    def test_rejects_agents_symlink(self) -> None:
        outside = self.root / "outside-agents.md"
        outside.write_text("outside\n", encoding="utf-8")
        project = self.root / "project"
        project.mkdir()
        (project / "AGENTS.md").symlink_to(outside)
        with self.assertRaises(init_project.InitError):
            init_project.apply_project(project, dry_run=False)
        self.assertTrue((project / "AGENTS.md").is_symlink())
        self.assertEqual("outside\n", outside.read_text(encoding="utf-8"))

    def test_preserves_agents_crlf_and_mode(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_bytes(b"# Existing\r\n\r\nKeep this.\r\n")
        os.chmod(agents, 0o640)
        original_stat = agents.stat()
        init_project.apply_project(self.root, dry_run=False)
        updated = agents.read_bytes()
        self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))
        self.assertIn(init_project.START_MARKER.encode("utf-8"), updated)
        self.assertEqual(0o640, stat.S_IMODE(agents.stat().st_mode))
        self.assertEqual(original_stat.st_uid, agents.stat().st_uid)
        self.assertEqual(original_stat.st_gid, agents.stat().st_gid)
        self.assertEqual(0o644, stat.S_IMODE((self.root / "optimization/GOAL.md").stat().st_mode))

    def test_requests_original_agents_ownership_before_replace(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text("existing\n", encoding="utf-8")
        original_stat = agents.stat()
        with mock.patch.object(init_project.os, "chown", wraps=os.chown) as chown:
            init_project.apply_project(self.root, dry_run=False)
        chown.assert_called_once()
        _, uid, gid = chown.call_args.args
        self.assertEqual(original_stat.st_uid, uid)
        self.assertEqual(original_stat.st_gid, gid)

    def test_project_write_failure_rolls_back_created_files(self) -> None:
        original = init_project.atomic_write
        calls = 0

        def fail_second(path: Path, content: bytes, preserve_from: Path | None = None) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected failure")
            original(path, content, preserve_from)

        with mock.patch.object(init_project, "atomic_write", side_effect=fail_second):
            with self.assertRaises(init_project.InitError):
                init_project.apply_project(self.root, dry_run=False)
        self.assertFalse((self.root / "optimization").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_batch_preflight_failure_writes_nothing(self) -> None:
        valid = self.root / "valid"
        valid.mkdir()
        invalid = self.root / "missing"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = init_project.main([str(valid), str(invalid)])
        self.assertEqual(2, result)
        self.assertEqual([], list(valid.iterdir()))


if __name__ == "__main__":
    unittest.main()
