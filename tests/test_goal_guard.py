from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("goal_guard", ROOT / "hooks/goal_guard.py")
assert SPEC and SPEC.loader
goal_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(goal_guard)


class GoalGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        optimization = self.project / "optimization"
        optimization.mkdir()
        (optimization / "GOAL.md").write_text("# Goal\n\nPrimary metric: accepted outputs\n", encoding="utf-8")
        (optimization / "STATE.md").write_text("# State\n\n- frontier: baseline\n", encoding="utf-8")
        (optimization / "EXPERIMENTS.md").write_text("# Experiments\n", encoding="utf-8")
        (optimization / "BACKLOG.md").write_text("# Backlog\n", encoding="utf-8")
        self.write_json("GATE.json", {
            "schema_version": 1,
            "enabled": True,
            "state_max_nonblank_lines": 25,
            "max_consecutive_no_progress": 3,
            "max_unchanged_polls": 2,
            "default_lease_minutes": 60,
            "default_max_mutations": 12,
            "max_non_core_cost_units_per_chain": 1,
        })
        self.write_json("CONTROL.json", goal_guard.default_control())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_json(self, name: str, value: dict) -> Path:
        path = self.project / "optimization" / name
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def proposal(
        self,
        experiment: str = "E001",
        chain: str = "C-main",
        *,
        final: bool = False,
        kind: str = "optimization",
        parent: str | None = None,
        bottleneck: str = "end to end representation mismatch",
        work_class: str = "core",
    ) -> dict:
        return {
            "schema_version": 1,
            "experiment_id": experiment,
            "chain_id": chain,
            "chain_kind": kind,
            "parent_chain": parent,
            "causal_bottleneck": bottleneck,
            "hypothesis": "one bounded change improves accepted output yield",
            "core_progress_expected": "at least one accepted end to end output",
            "allowed_paths": ["src", "optimization/STATE.md", "optimization/RESULT.json"],
            "allowed_command_prefixes": ["python3 run_eval.py", "pytest"],
            "expires_minutes": 60,
            "max_mutations": 3,
            "work_class": work_class,
            "cost_units": 1,
            "final_discriminator": final,
            "next_paths": {"positive": "verification child", "other": "switch representation"} if final else None,
            "review": {"decision": "ALLOW", "reviewer": "subagent:review-1", "reason": "bounded causal test"},
        }

    def result(
        self,
        experiment: str,
        *,
        core_progress: bool = False,
        outcome: str = "zero_progress",
        decision: str = "CONTINUE",
        valid: bool = True,
    ) -> dict:
        return {
            "schema_version": 1,
            "experiment_id": experiment,
            "valid": valid,
            "evaluation_integrity": "PASS" if valid else "FAIL",
            "core_progress": core_progress,
            "metric_delta": "+1 accepted output" if core_progress else "0 accepted outputs",
            "outcome": outcome,
            "decision": decision,
            "artifact": f"reports/{experiment}.json",
        }

    def admit(self, proposal: dict) -> int:
        path = self.write_json("PROPOSAL.json", proposal)
        args = argparse.Namespace(project=str(self.project), proposal=str(path))
        with contextlib.redirect_stdout(io.StringIO()):
            return goal_guard.command_admit(args)

    def checkpoint(self, result: dict) -> int:
        path = self.write_json("RESULT.json", result)
        args = argparse.Namespace(project=str(self.project), result=str(path))
        with contextlib.redirect_stdout(io.StringIO()):
            return goal_guard.command_checkpoint(args)

    def hook(self, event: dict) -> dict | None:
        stdin = io.StringIO(json.dumps(event))
        stdout = io.StringIO()
        with mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, goal_guard.command_hook())
        text = stdout.getvalue().strip()
        return json.loads(text) if text else None

    def pre(self, tool: str, command: str) -> dict | None:
        return self.hook({
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "tool_name": tool,
            "tool_input": {"command": command},
        })

    def pre_patch_native(self, patch: str) -> dict | None:
        return self.hook({
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "tool_name": "apply_patch",
            "tool_input": {"patch": patch},
        })

    def pre_input(self, tool: str, tool_input: dict) -> dict | None:
        return self.hook({
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "tool_name": tool,
            "tool_input": tool_input,
        })

    def post(self, command: str, response: object) -> dict | None:
        return self.hook({
            "hook_event_name": "PostToolUse",
            "cwd": str(self.project),
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": response,
        })

    def test_uninitialized_and_disabled_projects_do_not_intervene(self) -> None:
        outside = self.project / "outside"
        outside.mkdir()
        event = {"hook_event_name": "PreToolUse", "cwd": str(outside), "tool_name": "Bash", "tool_input": {"command": "rm x"}}
        # The parent is guarded, so explicitly disable it to test the inert path.
        gate = json.loads((self.project / "optimization/GATE.json").read_text())
        gate["enabled"] = False
        self.write_json("GATE.json", gate)
        self.assertIsNone(self.hook(event))

    def test_no_lease_allows_inspection_and_proposal_only(self) -> None:
        self.assertIsNone(self.pre("Bash", "git status --short"))
        proposal_patch = "*** Begin Patch\n*** Update File: optimization/PROPOSAL.json\n@@\n-{}\n+{}\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", proposal_patch))
        source_patch = "*** Begin Patch\n*** Update File: src/model.py\n@@\n-x\n+y\n*** End Patch"
        denial = self.pre("apply_patch", source_patch)
        self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])

    def test_apply_patch_accepts_native_patch_input_field(self) -> None:
        proposal_patch = "*** Begin Patch\n*** Update File: optimization/PROPOSAL.json\n*** End Patch"
        self.assertIsNone(self.pre_patch_native(proposal_patch))
        source_patch = "*** Begin Patch\n*** Update File: src/model.py\n*** End Patch"
        denial = self.pre_patch_native(source_patch)
        self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])
        denial = self.pre("Bash", "python3 train.py")
        self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])

    def test_admitted_lease_enforces_paths_commands_and_mutation_cap(self) -> None:
        self.admit(self.proposal())
        allowed_patch = "*** Begin Patch\n*** Update File: src/model.py\n@@\n-x\n+y\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", allowed_patch))
        blocked_patch = "*** Begin Patch\n*** Update File: docs/notes.md\n@@\n-x\n+y\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", blocked_patch)["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("Bash", "python3 run_eval.py --case small"))
        self.assertEqual("deny", self.pre("Bash", "python3 unrelated.py")["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("Bash", "pytest -q"))
        exhausted = self.pre("Bash", "pytest tests/test_more.py")
        self.assertEqual("deny", exhausted["hookSpecificOutput"]["permissionDecision"])

    def test_goal_change_invalidates_lease(self) -> None:
        self.admit(self.proposal())
        (self.project / "optimization/GOAL.md").write_text("changed\n", encoding="utf-8")
        denial = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("changed after admission", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_expired_lease_blocks_mutation(self) -> None:
        self.admit(self.proposal())
        control = goal_guard.load_control(self.project)
        control["active_lease"]["expires_at"] = "2000-01-01T00:00:00Z"
        goal_guard.save_control(self.project, control)
        denial = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("missing or expired", denial["hookSpecificOutput"]["permissionDecisionReason"])
        with self.assertRaisesRegex(goal_guard.GuardError, "awaiting checkpoint"):
            self.admit(self.proposal(experiment="E002", chain="C-next", bottleneck="different bottleneck"))
        result_patch = "*** Begin Patch\n*** Update File: optimization/RESULT.json\n@@\n-{}\n+{}\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", result_patch))

    def test_checkpoint_files_remain_writable_after_mutation_cap(self) -> None:
        proposal = self.proposal()
        proposal["max_mutations"] = 1
        self.admit(proposal)
        self.assertIsNone(self.pre("Bash", "python3 run_eval.py"))
        result_patch = "*** Begin Patch\n*** Update File: optimization/RESULT.json\n@@\n-{}\n+{}\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", result_patch))
        self.assertEqual("deny", self.pre("Bash", "python3 run_eval.py")["hookSpecificOutput"]["permissionDecision"])
        second_finalize = self.pre("apply_patch", result_patch)
        self.assertEqual("deny", second_finalize["hookSpecificOutput"]["permissionDecision"])

    def test_protected_contract_file_is_never_in_proposal_scope(self) -> None:
        proposal = self.proposal()
        proposal["allowed_paths"] = ["optimization/GOAL.md"]
        with self.assertRaisesRegex(goal_guard.GuardError, "protected path"):
            self.admit(proposal)

    def test_compound_shell_and_interpreter_prefixes_are_rejected(self) -> None:
        proposal = self.proposal()
        proposal["allowed_command_prefixes"] = ["bash -c"]
        with self.assertRaisesRegex(goal_guard.GuardError, "interpreters"):
            self.admit(proposal)
        self.admit(self.proposal())
        denial = self.pre("Bash", "python3 run_eval.py && python3 unrelated.py")
        self.assertIn("compound shell", denial["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual("deny", self.pre("Bash", "python3 run_eval.py-malicious")["hookSpecificOutput"]["permissionDecision"])

    def test_read_only_prefix_cannot_hide_a_compound_mutation(self) -> None:
        denial = self.pre("Bash", "git status; python3 train.py")
        self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])

    def test_self_review_and_rejected_review_cannot_admit(self) -> None:
        proposal = self.proposal()
        proposal["review"]["reviewer"] = "self:main"
        with self.assertRaisesRegex(goal_guard.GuardError, "fresh subagent or user"):
            self.admit(proposal)
        proposal["review"] = {"decision": "REJECT_TO_BACKLOG", "reviewer": "subagent:r", "reason": "low contribution"}
        with self.assertRaisesRegex(goal_guard.GuardError, "requires an ALLOW"):
            self.admit(proposal)

    def test_oversized_state_blocks_mutation(self) -> None:
        self.admit(self.proposal())
        (self.project / "optimization/STATE.md").write_text("\n".join(f"line {i}" for i in range(26)), encoding="utf-8")
        denial = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("exceeds its cap", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_oversized_state_can_be_compacted_after_checkpoint(self) -> None:
        (self.project / "optimization/STATE.md").write_text("\n".join(f"line {i}" for i in range(26)), encoding="utf-8")
        state_patch = "*** Begin Patch\n*** Update File: optimization/STATE.md\n@@\n-old\n+short\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", state_patch))

    def test_repeated_identical_poll_is_blocked_and_stays_blocked(self) -> None:
        command = "squeue -j 123"
        self.assertIsNone(self.post(command, {"output": "RUNNING"}))
        feedback = self.post(command, {"output": "RUNNING"})
        self.assertEqual("block", feedback["decision"])
        denial = self.pre("Bash", command)
        self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])

    def test_three_no_progress_rounds_force_stopline_then_one_final_discriminator(self) -> None:
        for experiment, decision in (("E009", "CONTINUE"), ("E010", "CONTINUE"), ("E011", "PAUSE_REQUIRED")):
            self.admit(self.proposal(experiment=experiment))
            self.checkpoint(self.result(experiment, decision=decision))
        control = goal_guard.load_control(self.project)
        self.assertTrue(control["chains"]["C-main"]["stopline_fired"])
        self.assertFalse(control["chains"]["C-main"]["closed"])
        with self.assertRaisesRegex(goal_guard.GuardError, "only one declared final discriminator"):
            self.admit(self.proposal(experiment="E012"))
        self.admit(self.proposal(experiment="E013", final=True))
        self.checkpoint(self.result("E013", outcome="negative", decision="SWITCH"))
        control = goal_guard.load_control(self.project)
        self.assertTrue(control["chains"]["C-main"]["closed"])
        with self.assertRaisesRegex(goal_guard.GuardError, "chain is closed"):
            self.admit(self.proposal(experiment="E014"))

    def test_renaming_chain_cannot_reset_same_bottleneck(self) -> None:
        self.admit(self.proposal(experiment="E001"))
        self.checkpoint(self.result("E001", outcome="negative", decision="SWITCH"))
        renamed = self.proposal(experiment="E002", chain="C-adapter-renamed")
        with self.assertRaisesRegex(goal_guard.GuardError, "renaming cannot reset"):
            self.admit(renamed)

    def test_positive_final_allows_restricted_verification_child(self) -> None:
        self.admit(self.proposal(experiment="E013", final=True))
        self.checkpoint(self.result("E013", core_progress=True, outcome="positive", decision="SWITCH"))
        verification = self.proposal(
            experiment="V001",
            chain="V-main",
            kind="verification",
            parent="C-main",
        )
        self.admit(verification)
        control = goal_guard.load_control(self.project)
        self.assertEqual("verification", control["active_lease"]["chain_kind"])

    def test_final_discriminator_cannot_continue_original_chain(self) -> None:
        self.admit(self.proposal(experiment="E013", final=True))
        with self.assertRaisesRegex(goal_guard.GuardError, "must close or switch"):
            self.checkpoint(self.result("E013", core_progress=True, outcome="positive", decision="CONTINUE"))

    def test_complete_requires_positive_core_progress(self) -> None:
        self.admit(self.proposal(experiment="E001"))
        with self.assertRaisesRegex(goal_guard.GuardError, "COMPLETE requires"):
            self.checkpoint(self.result("E001", decision="COMPLETE"))

    def test_non_core_allowance_is_bounded(self) -> None:
        self.admit(self.proposal(experiment="N001", work_class="non_core"))
        self.checkpoint(self.result("N001", core_progress=True, outcome="positive", decision="CONTINUE"))
        with self.assertRaisesRegex(goal_guard.GuardError, "non-core allowance"):
            self.admit(self.proposal(experiment="N002", work_class="non_core"))

    def test_session_start_reinjects_compact_gate_status(self) -> None:
        output = self.hook({"hook_event_name": "SessionStart", "cwd": str(self.project), "source": "compact"})
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("enforcement is active", context)
        self.assertIn("fresh subagent", context)

    def test_session_start_context_does_not_grow_with_closed_chain_history(self) -> None:
        control = goal_guard.default_control()
        for index in range(300):
            control["chains"][f"C-{index:03d}"] = {
                "no_progress_count": 3,
                "stopline_fired": True,
                "closed": True,
                "close_outcome": "negative",
            }
        control["last_checkpoint"] = {
            "experiment_id": "E299",
            "chain_id": "C-299",
            "decision": "SWITCH",
            "outcome": "negative",
            "core_progress": False,
            "time": "2026-08-29T00:00:00Z",
            "artifact": "reports/E299.json",
        }
        self.write_json("CONTROL.json", control)
        output = self.hook({"hook_event_name": "SessionStart", "cwd": str(self.project), "source": "compact"})
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("C-299", context)
        self.assertNotIn("C-000", context)
        self.assertLess(len(context), 1200)

    def test_cli_hook_protocol_emits_official_pretooluse_deny_shape(self) -> None:
        event = {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.project),
            "tool_name": "Bash",
            "tool_input": {"command": "python3 train.py"},
        }
        completed = subprocess.run(
            [sys.executable, str(ROOT / "hooks/goal_guard.py"), "hook"],
            input=json.dumps(event), text=True, capture_output=True, check=True,
        )
        output = json.loads(completed.stdout)
        specific = output["hookSpecificOutput"]
        self.assertEqual("PreToolUse", specific["hookEventName"])
        self.assertEqual("deny", specific["permissionDecision"])

    def test_cli_status_is_valid_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "hooks/goal_guard.py"), "status", "--project", str(self.project)],
            text=True, capture_output=True, check=True,
        )
        status = json.loads(completed.stdout)
        self.assertTrue(status["enabled"])
        self.assertIsNone(status["active_experiment"])

    def test_only_the_bundled_controller_gets_controller_bypass(self) -> None:
        real = f"{sys.executable} {ROOT / 'hooks/goal_guard.py'} status --project {self.project}"
        self.assertTrue(goal_guard.is_controller_command(real))
        self.assertFalse(goal_guard.is_controller_command("python3 /tmp/goal_guard.py status"))
        self.assertFalse(goal_guard.is_controller_command(f"echo {ROOT / 'hooks/goal_guard.py'} status"))

    def test_mcp_mutation_fails_closed_even_if_proposal_requests_it(self) -> None:
        self.assertIsNone(self.pre("mcp__filesystem__read_file", ""))
        denial = self.pre("mcp__filesystem__write_file", "")
        self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])
        proposal = self.proposal()
        proposal["allowed_tool_names"] = ["mcp__filesystem__write_file"]
        with self.assertRaisesRegex(goal_guard.GuardError, "not admissible"):
            self.admit(proposal)

        self.admit(self.proposal())
        cases = [
            ("mcp__filesystem__write_file", {"path": "src/allowed.py", "content": "x"}),
            ("mcp__filesystem__write_file", {"path": "optimization/GATE.json", "content": "{}"}),
            ("mcp__filesystem__write_file", {"path": "optimization/CONTROL.json", "content": "{}"}),
            ("mcp__filesystem__write_file", {"path": "optimization/GOAL.md", "content": "changed"}),
            ("mcp__filesystem__write_file", {"path": "/tmp/outside", "content": "x"}),
            ("mcp__filesystem__move_file", {"source": "src/a", "destination": "/tmp/a"}),
            ("mcp__filesystem__copy_file", {"source": "src/a", "destination": "src/b"}),
        ]
        for tool, tool_input in cases:
            with self.subTest(tool=tool, tool_input=tool_input):
                denial = self.pre_input(tool, tool_input)
                self.assertEqual("deny", denial["hookSpecificOutput"]["permissionDecision"])

    def test_symlink_in_allowed_path_or_patch_target_is_rejected(self) -> None:
        (self.project / "src").mkdir()
        with tempfile.TemporaryDirectory() as external_raw:
            external = Path(external_raw)
            (self.project / "src/link").symlink_to(external, target_is_directory=True)
            proposal = self.proposal()
            proposal["allowed_paths"] = ["src/link"]
            with self.assertRaisesRegex(goal_guard.GuardError, "Symbolic|symbolic"):
                self.admit(proposal)
            self.admit(self.proposal())
            patch = "*** Begin Patch\n*** Add File: src/link/escaped.txt\n+escaped\n*** End Patch"
            denial = self.pre("apply_patch", patch)
            self.assertIn("symbolic links", denial["hookSpecificOutput"]["permissionDecisionReason"])
            self.assertFalse((external / "escaped.txt").exists())

    def test_enabled_parent_does_not_capture_nested_git_repository(self) -> None:
        nested = self.project / "nested"
        (nested / ".git").mkdir(parents=True)
        output = self.hook({
            "hook_event_name": "PreToolUse",
            "cwd": str(nested),
            "tool_name": "Bash",
            "tool_input": {"command": "python3 train.py"},
        })
        self.assertIsNone(output)

    def test_verification_stopline_closes_without_final_discriminator_deadlock(self) -> None:
        self.admit(self.proposal(experiment="E013", final=True))
        self.checkpoint(self.result("E013", core_progress=True, outcome="positive", decision="SWITCH"))
        for experiment, decision in (("V001", "CONTINUE"), ("V002", "CONTINUE"), ("V003", "PAUSE_REQUIRED")):
            self.admit(self.proposal(experiment=experiment, chain="V-main", kind="verification", parent="C-main"))
            self.checkpoint(self.result(experiment, decision=decision))
        chain = goal_guard.load_control(self.project)["chains"]["V-main"]
        self.assertTrue(chain["stopline_fired"])
        self.assertTrue(chain["closed"])


if __name__ == "__main__":
    unittest.main()
