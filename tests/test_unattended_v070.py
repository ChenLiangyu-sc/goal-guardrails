"""Focused regression tests for unattended v0.7 state-machine behavior.

These tests intentionally reuse the existing hermetic fixture but remain in a
separate file so the unattended-run contract can be audited independently from
the historical v0.6 regression suite.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from test_goal_guard import goal_guard


def _new_fixture():
    # Keep the TestCase class out of this module's globals; unittest otherwise
    # rediscovers the entire historical suite when this file is run directly.
    from test_goal_guard import GoalGuardTests
    return GoalGuardTests()


class UnattendedV070Tests(unittest.TestCase):
    """Fault-oriented checks for long-running, unattended continuation."""

    def setUp(self) -> None:
        self._fixture = _new_fixture()
        self._fixture.setUp()

    def tearDown(self) -> None:
        self._fixture.tearDown()

    def __getattr__(self, name: str):
        fixture = self.__dict__.get("_fixture")
        if fixture is not None:
            return getattr(fixture, name)
        raise AttributeError(name)

    def _last_action(self) -> dict:
        control = goal_guard.load_control(self.project)
        return goal_guard.recommended_next_action(goal_guard.load_gate(self.project), control)

    def test_waiting_external_event_does_not_consume_lease_or_recovery_budget(self) -> None:
        proposal = self.semantic_proposal(recovery_paths=[{
            "id": "retry-evaluator",
            "kind": "recovery",
            "description": "rerun the evaluator with the same frozen inputs",
            "max_attempts": 2,
            "write_once_output_root": "artifacts/retry-evaluator",
        }])
        self.admit(proposal)
        before = goal_guard.load_control(self.project)
        lease_before = before["active_lease"]
        # A generic artifact wait exercises the lease suspension path without
        # requiring a scheduler or monitor fixture.
        args = argparse.Namespace(
            project=str(self.project), event_key="pending-terminal", event_path="artifacts/E001.json",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, goal_guard.command_wait(args))
        after = goal_guard.load_control(self.project)
        self.assertEqual("WAITING_EXTERNAL_EVENT", after["runtime"]["state"])
        self.assertEqual(before["recovery"], after["recovery"])
        self.assertEqual(lease_before["lease_id"], after["active_lease"]["lease_id"])
        self.assertNotEqual(lease_before["expires_at"], "")
        self.assertEqual(lease_before["expires_at"], after["active_lease"]["expires_at"])
        self.assertIn(self._last_action()["kind"], {"WAIT", "WAIT_EXTERNAL_EVENT"})

    def test_missing_terminal_keeps_goal_waiting_instead_of_business_pause(self) -> None:
        self.admit(self.external_monitor_proposal())
        self.submit_binding()
        terminal, event = self.write_external_monitor_terminal()
        terminal.unlink()
        event.unlink()
        self.wait_monitor()
        action = self._last_action()
        self.assertNotEqual("AWAIT_DECISION", action["kind"])
        self.assertNotEqual("PAUSE_REQUIRED", action["kind"])
        self.assertIn(action["kind"], {"WAIT_EXTERNAL_EVENT", "WAKE_MONITOR"})
        self.assertEqual("WAITING_EXTERNAL_EVENT", goal_guard.load_control(self.project)["runtime"]["state"])

    def test_duplicate_terminal_event_and_checkpoint_receipt_are_noops(self) -> None:
        self.admit(self.external_monitor_proposal())
        self.submit_binding()
        terminal, event = self.write_external_monitor_terminal()
        terminal.unlink()
        event.unlink()
        self.wait_monitor()
        _terminal, event = self.write_external_monitor_terminal()
        event_id = "sha256:" + event.parent.name
        self.wake_monitor(event_id)
        first = goal_guard.load_control(self.project)
        receipt_sha = first["active_lease"]["monitor_receipts"]["scheduler"]["sha256"]
        seen = list(first["runtime"]["seen_events"])
        self.wake_monitor(event_id)
        second = goal_guard.load_control(self.project)
        self.assertEqual(receipt_sha, second["active_lease"]["monitor_receipts"]["scheduler"]["sha256"])
        self.assertEqual(seen, second["runtime"]["seen_events"])

    def test_malformed_delivery_does_not_hide_durable_terminal_from_session_start(self) -> None:
        self.admit(self.external_monitor_proposal())
        self.submit_binding()
        terminal, event = self.write_external_monitor_terminal()
        terminal.unlink()
        event.unlink()
        self.wait_monitor()
        _terminal, event = self.write_external_monitor_terminal()
        event.with_name("delivery.json").write_text("{not-json\n", encoding="utf-8")
        before_runs = dict(goal_guard.load_control(self.project)["active_lease"]["policy_runs"])

        self.hook({"hook_event_name": "SessionStart", "cwd": str(self.project), "source": "natural-malformed-delivery"})
        control = goal_guard.load_control(self.project)
        self.assertEqual("ACTIVE", control["runtime"]["state"])
        self.assertIn("scheduler", control["active_lease"]["monitor_receipts"])
        self.assertEqual(before_runs, control["active_lease"]["policy_runs"])

    def test_declared_recovery_path_and_global_budget_are_both_bounded(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate["max_recovery_attempts"] = 2
        self.write_json("GATE.json", gate)
        path = {
            "id": "retry-once",
            "kind": "recovery",
            "description": "retry evaluator once",
            "max_attempts": 1,
            "write_once_output_root": "artifacts/retry-once",
        }
        self.admit(self.semantic_proposal(recovery_paths=[path]))
        self.checkpoint(self.semantic_result("E001", semantic_class="evaluator_unavailable"))
        first = goal_guard.load_control(self.project)
        self.assertEqual("RECOVERY_NEXT", self._last_action()["kind"])

        successor = self.semantic_proposal(experiment="E002", recovery_paths=[{
            "id": "later-recovery",
            "kind": "recovery",
            "description": "would retry again if budget allowed",
            "max_attempts": 5,
            "write_once_output_root": "artifacts/later-recovery",
        }])
        successor["write_once_output_root"] = "artifacts/retry-once/E002"
        self.admit(successor)
        self.assertEqual(1, goal_guard.load_control(self.project)["recovery_path_usage"]["C-main:retry-once"])
        self.checkpoint(self.semantic_result("E002", semantic_class="evaluator_unavailable"))
        final = goal_guard.load_control(self.project)
        self.assertEqual(2, final["recovery"]["used"])
        self.assertEqual("NONE", final["continuation"]["state"])
        self.assertTrue(final["last_checkpoint"]["blocking_proof"]["block_allowed"])
        self.assertEqual("AWAIT_DECISION", self._last_action()["kind"])

    def test_guardrail_failure_is_valid_negative_rollback_not_invalid_pause(self) -> None:
        self.admit(self.semantic_proposal(recovery_paths=[{
            "id": "rollback-safe",
            "kind": "rollback",
            "description": "restore the previous candidate",
            "max_attempts": 1,
            "write_once_output_root": "artifacts/rollback-safe",
        }]))
        self.checkpoint(self.semantic_result("E001", semantic_class="guardrail_failed"))
        control = goal_guard.load_control(self.project)
        self.assertEqual("negative", control["last_checkpoint"]["outcome"])
        self.assertTrue(control["last_checkpoint"]["valid"])
        self.assertEqual("PASS", control["last_checkpoint"]["evaluation_integrity"])
        self.assertFalse(control["last_checkpoint"]["blocking_proof"]["block_allowed"])
        self.assertEqual("ROLLBACK_NEXT", self._last_action()["kind"])

    def test_successor_cannot_reuse_consumed_write_once_output_root(self) -> None:
        recovery = {
            "id": "switch-root",
            "kind": "switch",
            "description": "switch representation",
            "max_attempts": 1,
            "write_once_output_root": "artifacts/successor-root",
        }
        self.admit(self.semantic_proposal(recovery_paths=[recovery]))
        self.checkpoint(self.semantic_result("E001", semantic_class="threshold_failed"))

        successor = self.semantic_proposal(experiment="E002", chain="C-next")
        successor["causal_bottleneck"] = "a distinct successor bottleneck"
        successor["checkpoint_artifacts"][0]["path"] = "artifacts/successor-root/result.json"
        successor["recovery_paths"] = [dict(recovery)]
        with self.assertRaisesRegex(goal_guard.GuardError, "write-once output root"):
            self.admit(successor)

    def test_submit_bind_consumption_never_allows_resubmission_after_transport_uncertainty(self) -> None:
        self.admit(self.remote_submission_proposal())
        args = argparse.Namespace(project=str(self.project), policy="submit-slurm")
        def doctor_response(policy: dict, request: dict) -> tuple[dict, dict]:
            return ({
                "schema_version": goal_guard.REMOTE_DOCTOR_SCHEMA, "ready": True,
                "contract_sha256": request["contract_sha256"],
                "helper_sha256": policy["transport"]["helper_sha256"],
                "sbatch_sha256": "2" * 64, "remote_files": policy["transport"]["remote_files"],
            }, {"ssh_exit_code": 0, "stdout_sha256": "3" * 64, "stderr_sha256": "4" * 64})
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=doctor_response), contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_doctor(args)
        # The helper exits nonzero, but the one-shot submission policy is still
        # consumed and must remain uncertain rather than being retried.
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=RuntimeError("ssh disconnected")):
            with self.assertRaises(goal_guard.GuardError):
                self.submit_binding()
        control = goal_guard.load_control(self.project)
        self.assertEqual("UNCERTAIN", control["active_lease"]["binding_values"]["slurm-job"]["state"])
        self.assertEqual("UNCERTAIN", control["active_lease"]["policy_runs"]["submit-slurm"]["state"])
        with self.assertRaisesRegex(goal_guard.GuardError, "already consumed"):
            self.submit_binding()

    def test_v2_lease_still_accepts_old_result_shape_after_v070_schema_upgrade(self) -> None:
        self.admit(self.proposal(experiment="E002"))
        self.checkpoint(self.result("E002", outcome="zero_progress", decision="CONTINUE"))
        control = goal_guard.load_control(self.project)
        self.assertIsNone(control["active_lease"])
        self.assertEqual("E002", control["last_checkpoint"]["experiment_id"])
        self.assertEqual("zero_progress", control["last_checkpoint"]["outcome"])

    def test_v2_new_proposal_cannot_consume_available_v3_continuation(self) -> None:
        self.admit(self.semantic_proposal())
        self.checkpoint(self.semantic_result("E001", semantic_class="threshold_failed"))
        legacy_successor = self.proposal(experiment="E002", chain="C-legacy-next")
        legacy_successor["causal_bottleneck"] = "legacy successor bypass"
        with self.assertRaisesRegex(goal_guard.GuardError, "schema-v3 bound successor"):
            self.admit(legacy_successor)
        self.assertEqual("AVAILABLE", goal_guard.load_control(self.project)["continuation"]["state"])

    def test_receipt_write_then_control_failure_rolls_forward_without_duplicate_record(self) -> None:
        self.admit(self.semantic_proposal())
        result = self.semantic_result("E001", semantic_class="threshold_failed")
        path = self.write_json("RESULT.json", result)
        args = argparse.Namespace(project=str(self.project), result=str(path))
        with mock.patch.object(goal_guard, "save_control", side_effect=OSError("simulated control write failure")):
            with self.assertRaisesRegex(OSError, "simulated control write failure"):
                goal_guard.command_checkpoint(args)
        receipt_root = self.project / "optimization/.goal-guardrails/checkpoints/E001"
        self.assertEqual(1, len(list(receipt_root.glob("*.json"))))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, goal_guard.command_checkpoint(args))
        self.assertEqual(1, len(list(receipt_root.glob("*.json"))))
        self.assertIsNone(goal_guard.load_control(self.project)["active_lease"])


if __name__ == "__main__":
    unittest.main()
