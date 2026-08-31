from __future__ import annotations

import argparse
import contextlib
import hashlib
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
        self.external_temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.monitor_state = Path(self.external_temporary.name) / "codex-hpc-monitor"
        optimization = self.project / "optimization"
        optimization.mkdir()
        (optimization / "GOAL.md").write_text("# Goal\n\nPrimary metric: accepted outputs\n", encoding="utf-8")
        (optimization / "STATE.md").write_text("# State\n\n- frontier: baseline\n", encoding="utf-8")
        (optimization / "EXPERIMENTS.md").write_text("# Experiments\n", encoding="utf-8")
        (optimization / "BACKLOG.md").write_text("# Backlog\n", encoding="utf-8")
        reports = self.project / "reports"
        reports.mkdir()
        (reports / "baseline.json").write_text('{"accepted": 0}\n', encoding="utf-8")
        (self.project / "artifacts").mkdir()
        self.write_json("GATE.json", {
            "schema_version": 1,
            "enabled": True,
            "profile": "strict",
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
        self.external_temporary.cleanup()

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
            "schema_version": 2,
            "experiment_id": experiment,
            "chain_id": chain,
            "chain_kind": kind,
            "parent_chain": parent,
            "causal_bottleneck": bottleneck,
            "hypothesis": "one bounded change improves accepted output yield",
            "core_progress_expected": "at least one accepted end to end output",
            "lease_phase": "synchronous",
            "existing_evidence": [{
                "id": "baseline",
                "path": "reports/baseline.json",
                "sha256": goal_guard.file_hash(self.project / "reports/baseline.json"),
                "claim": "baseline has zero accepted outputs",
            }],
            "lease_mutations": [
                {"path": "src", "scope": "tree", "operations": ["add", "update", "delete", "move"]},
                {"path": "artifacts", "scope": "tree", "operations": ["add", "update"]},
            ],
            "checkpoint_artifacts": [{"id": "primary-result", "path": f"artifacts/{experiment}.json", "required": True}],
            "pre_run_gates": [],
            "bash_policies": [
                {"id": "eval", "phase": "evaluation", "executable": "python3", "fixed_args": ["run_eval.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                {"id": "eval-case", "phase": "evaluation", "executable": "python3", "fixed_args": ["run_eval.py", "--case", "small"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                {"id": "pytest-q", "phase": "evaluation", "executable": "pytest", "fixed_args": ["-q"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                {"id": "pytest-more", "phase": "evaluation", "executable": "pytest", "fixed_args": ["tests/test_more.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
            ],
            "expires_minutes": 60,
            "max_mutations": 3,
            "work_class": work_class,
            "cost_units": 1,
            "final_discriminator": final,
            "next_paths": {"positive": "verification child", "other": "switch representation"} if final else None,
            "review": {
                "decision": "ALLOW", "reviewer": "subagent:review-1", "reason": "bounded causal test",
                "checks": {
                    "evidence_sufficient": True,
                    "lease_mutations_bounded": True,
                    "pre_run_gates_sufficient": True,
                    "mutation_not_required_before_admission": True,
                },
            },
        }

    def external_monitor_proposal(self) -> dict:
        proposal = self.proposal()
        submit = self.project / "sbatch"
        submit.write_text("#!/usr/bin/env python3\nprint('12345;cluster')\n", encoding="utf-8")
        submit.chmod(0o700)
        monitor = self.project / "monitor_fake.py"
        monitor.write_text("raise SystemExit(0)\n", encoding="utf-8")
        proposal["lease_phase"] = "workload"
        proposal["runtime_bindings"] = [{
            "id": "slurm-job", "kind": "slurm_job_id", "source_policy_id": "submit-slurm", "required": True,
        }]
        proposal["bash_policies"] = [
            {
                "id": "submit-slurm", "phase": "workload", "executable": "./sbatch",
                "argv": [{"literal": "--parsable"}, {"literal": "train.sbatch"}], "cwd": ".", "output_paths": [],
                "resources": {"gpu": 0}, "capture_binding": "slurm-job", "max_uses": 1,
                "timeout_seconds": 10,
            },
            {
                "id": "start-monitor", "phase": "workload", "executable": sys.executable,
                "argv": [
                    {"literal": "monitor_fake.py"}, {"literal": "start"}, {"binding": "slurm-job"},
                    {"literal": "--host"}, {"literal": "fakehost"},
                    {"literal": "--state-dir"}, {"literal": str(self.monitor_state)},
                    {"literal": "--expected-owner"}, {"literal": "alice"},
                    {"literal": "--expected-job-name"}, {"literal": "H25"},
                    {"literal": "--expected-partition"}, {"literal": "gpu"},
                    {"literal": "--event-binding"}, {"literal": str(self.monitor_state / "event-binding.json")},
                    {"literal": "--bridge-config"}, {"literal": str(self.monitor_state / "bridge.json")},
                    {"literal": "--bridge-service-name"}, {"literal": "codex-monitor-test-bridge"},
                    {"literal": "--require-auto-resume"},
                ],
                "cwd": ".", "output_paths": [], "resources": {"gpu": 0}, "max_uses": 1,
            },
            {
                "id": "postflight", "phase": "postflight", "executable": sys.executable,
                "argv": [{"literal": "postflight.py"}, {"binding": "slurm-job"}],
                "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
            },
        ]
        proposal["external_monitors"] = [{
            "id": "scheduler", "provider": "codex-hpc-monitor", "contract_version": 1,
            "binding_id": "slurm-job", "start_policy_id": "start-monitor",
            "state_root": str(self.monitor_state), "host": "fakehost", "expected_owner": "alice",
            "expected_job_name": "H25", "expected_partition": "gpu", "required": True,
        }]
        proposal["review"]["checks"]["external_monitor_contract_bounded"] = True
        return proposal

    def remote_submission_proposal(self) -> dict:
        proposal = self.proposal()
        train_script = self.project / "train.sbatch"
        train_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_ssh = self.project / "ssh"
        fake_ssh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_ssh.chmod(0o700)
        known_hosts = self.project / "known_hosts"
        known_hosts.write_text("hpc142 ssh-ed25519 AAAATEST\n", encoding="utf-8")
        identity_file = self.project / "id_remote"
        identity_file.write_text("TEST-PRIVATE-KEY\n", encoding="utf-8")
        identity_file.chmod(0o600)
        proposal["runtime_bindings"] = [{
            "id": "slurm-job", "kind": "slurm_job_id", "source_policy_id": "submit-slurm", "required": True,
        }]
        proposal["bash_policies"] = [{
            "id": "submit-slurm", "phase": "workload", "executable": "/usr/bin/sbatch",
            "argv": [{"literal": "--parsable"}, {"literal": "train.sbatch"}],
            "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
            "capture_binding": "slurm-job", "max_uses": 1, "timeout_seconds": 10,
            "transport": {
                "kind": "ssh-helper-v1", "ssh_executable": str(fake_ssh),
                "ssh_executable_sha256": goal_guard.file_hash(fake_ssh),
                "host": "hpc142", "user": "alice", "port": 22,
                "known_hosts_file": str(known_hosts), "known_hosts_sha256": goal_guard.file_hash(known_hosts),
                "identity_file": str(identity_file), "identity_file_sha256": goal_guard.file_hash(identity_file),
                "helper_path": "/opt/goal-guardrails/remote_submit_helper.py",
                "helper_sha256": goal_guard.file_hash(ROOT / "hooks/remote_submit_helper.py"),
                "sbatch_path": "/usr/bin/sbatch", "remote_workdir": "/shared/project",
                "receipt_root": "/home/alice/.cache/goal-guardrails/submissions",
                "remote_files": [{"path": "train.sbatch", "sha256": goal_guard.file_hash(train_script)}],
                "timeout_seconds": 10,
            },
        }]
        proposal["review"]["checks"]["remote_submission_contract_bounded"] = True
        return proposal

    def write_external_monitor_terminal(
        self,
        *,
        terminal_verified: bool = True,
        owner: str = "alice",
        workspace: str | None = None,
    ) -> tuple[Path, Path]:
        root = self.monitor_state
        run_id = "run_test"
        run = root / "supervisors/fakehost-12345/runs" / run_id
        run.mkdir(parents=True, exist_ok=True)
        event_binding = {
            "schema": "codex-monitor.event-binding/v1",
            "codex_home_id": "sha256:" + "a" * 64,
            "app_server_instance": "workstation-1",
            "thread_id": "thr_test_1",
            "workspace": workspace or str(self.project.resolve()),
        }
        binding_bytes = (json.dumps(event_binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
        watcher_argv = [
            sys.executable, "/opt/codex-hpc-monitor/watch_slurm_job.py", "12345",
            "--host", "fakehost", "--state-dir", str(root),
            "--expected-owner", "alice", "--expected-job-name", "H25", "--expected-partition", "gpu",
        ]
        manifest = {
            "schema_version": "codex-hpc-monitor.manifest/v1", "run_id": run_id,
            "host": "fakehost", "job_id": "12345", "watcher_argv": watcher_argv,
            "watcher_path_sha256": "1" * 64, "state_dir": str(root),
            "scope": "slurm_only", "project_gate_evaluated": False,
            "event_binding": event_binding,
            "event_binding_digest": "sha256:" + hashlib.sha256(binding_bytes).hexdigest(),
            "created_at": goal_guard.iso_time(goal_guard.utc_now()),
        }
        manifest_path = run / "manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        current = root / "supervisors/fakehost-12345/current.json"
        current.write_text(json.dumps({
            "schema_version": "codex-hpc-monitor.current/v1", "host": "fakehost",
            "job_id": "12345", "run_id": run_id,
        }) + "\n", encoding="utf-8")
        terminal = {
            "schema_version": "codex-hpc-monitor.terminal/v1", "run_id": run_id,
            "host": "fakehost", "job_id": "12345", "scope": "slurm_only",
            "project_gate_evaluated": False, "observer_state": "exited",
            "observer_outcome": "watcher_exit_zero", "watcher_exit_code": 0,
            "manifest_sha256": goal_guard.file_hash(manifest_path),
            "watcher_result": {"verified": terminal_verified, "payload": {
                "job_id": "12345", "owner": owner, "job_name": "H25", "partition": "gpu",
                "state": "COMPLETED", "exit_code": "0:0", "slurm_classification": "scheduler_success",
            }},
        }
        terminal_path = run / "terminal.json"
        terminal_path.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
        event_name = "transport_success" if terminal_verified else "contract_violation"
        event_monitor = {
            "backend": "slurm", "handle": "fakehost-12345", "generation": run_id,
            "terminal_digest": "sha256:" + goal_guard.file_hash(terminal_path),
        }
        identity = {
            "schema": "codex-monitor.event/v1", "monitor": event_monitor,
            "event": event_name, "binding": event_binding,
        }
        identity_bytes = (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()
        event_id = "sha256:" + hashlib.sha256(identity_bytes).hexdigest()
        event = {
            "schema": "codex-monitor.event/v1", "event_id": event_id,
            "created_at": goal_guard.iso_time(goal_guard.utc_now()), "monitor": event_monitor,
            "event": event_name, "exit_code": 0, "business_verdict": "pending",
            "binding": event_binding,
        }
        event_path = root / "outbox" / event_id.removeprefix("sha256:") / "event.json"
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        (run / "semantic_event.json").write_text(json.dumps({
            "schema_version": "codex-hpc-monitor.semantic-event/v1", "run_id": run_id,
            "event_id": event_id, "event": event_name, "state": "published",
            "published_at": goal_guard.iso_time(goal_guard.utc_now()),
        }) + "\n", encoding="utf-8")
        return terminal_path, event_path

    def submit_binding(self) -> int:
        args = argparse.Namespace(project=str(self.project), policy="submit-slurm")
        with contextlib.redirect_stdout(io.StringIO()):
            return goal_guard.command_submit_bind(args)

    def wait_monitor(self) -> int:
        args = argparse.Namespace(project=str(self.project), monitor="scheduler")
        with contextlib.redirect_stdout(io.StringIO()):
            return goal_guard.command_wait_monitor(args)

    def wake_monitor(self, event_id: str | None = None) -> int:
        args = argparse.Namespace(project=str(self.project), monitor="scheduler", event_id=event_id)
        with contextlib.redirect_stdout(io.StringIO()):
            return goal_guard.command_wake_monitor(args)

    def result(
        self,
        experiment: str,
        *,
        core_progress: bool = False,
        outcome: str = "zero_progress",
        decision: str = "CONTINUE",
        valid: bool = True,
    ) -> dict:
        artifact_path = self.project / "artifacts" / f"{experiment}.json"
        artifact_path.write_text(json.dumps({"experiment": experiment, "core_progress": core_progress}) + "\n", encoding="utf-8")
        return {
            "schema_version": 2,
            "experiment_id": experiment,
            "valid": valid,
            "evaluation_integrity": "PASS" if valid else "FAIL",
            "core_progress": core_progress,
            "metric_delta": "+1 accepted output" if core_progress else "0 accepted outputs",
            "outcome": outcome,
            "decision": decision,
            "artifact": f"artifacts/{experiment}.json",
            "artifact_results": [{"id": "primary-result", "path": f"artifacts/{experiment}.json", "sha256": goal_guard.file_hash(artifact_path)}],
            "pre_run_gate_results": [],
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

    def record_gates(self, payload: dict) -> int:
        path = self.write_json("PRE_RUN_RESULTS.json", payload)
        args = argparse.Namespace(project=str(self.project), results=str(path))
        with contextlib.redirect_stdout(io.StringIO()):
            return goal_guard.command_gates(args)

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

    def test_admit_fails_when_gate_is_disabled(self) -> None:
        gate = json.loads((self.project / "optimization/GATE.json").read_text())
        gate["enabled"] = False
        self.write_json("GATE.json", gate)
        with self.assertRaisesRegex(goal_guard.GuardError, "not activated"):
            self.admit(self.proposal())

    def test_reviewed_preparation_proposal_allows_mutation_only_after_admission(self) -> None:
        proposal = self.proposal()
        proposal["lease_phase"] = "preparation"
        proposal["bash_policies"] = [{
            "id": "prepare", "phase": "preparation", "executable": "python3",
            "fixed_args": ["prepare.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
        }]
        add_patch = "*** Begin Patch\n*** Add File: src/prepared.py\n+ready = True\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", add_patch)["hookSpecificOutput"]["permissionDecision"])
        self.admit(proposal)
        self.assertIsNone(self.pre("apply_patch", add_patch))

    def test_proposal_and_existing_evidence_are_frozen_after_admission(self) -> None:
        self.admit(self.proposal())
        original_proposal = (self.project / "optimization/PROPOSAL.json").read_text()
        proposal = json.loads((self.project / "optimization/PROPOSAL.json").read_text())
        proposal["hypothesis"] = "semantic drift after review"
        self.write_json("PROPOSAL.json", proposal)
        denial = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("PROPOSAL.json changed", denial["hookSpecificOutput"]["permissionDecisionReason"])

        (self.project / "optimization/PROPOSAL.json").write_text(original_proposal, encoding="utf-8")
        (self.project / "reports/baseline.json").write_text('{"accepted": 99}\n', encoding="utf-8")
        denial = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("frozen existing evidence", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_no_lease_allows_inspection_and_proposal_only(self) -> None:
        self.assertIsNone(self.pre("Bash", "git status --short"))
        self.assertIsNone(self.pre("Bash", "cat skills/goal-guardrails/references/operating-protocol.md"))
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

    def test_structured_bash_policy_freezes_args_cwd_and_output_path(self) -> None:
        (self.project / "work").mkdir()
        proposal = self.proposal()
        proposal["bash_policies"] = [{
            "id": "prepare", "phase": "preparation", "executable": "python3",
            "fixed_args": ["prepare.py", "--output=../artifacts/E001.json"],
            "cwd": "work", "output_paths": ["artifacts/E001.json"], "resources": {"gpu": 0},
        }]
        self.admit(proposal)
        command = "python3 prepare.py --output=../artifacts/E001.json"
        self.assertEqual("deny", self.pre("Bash", command)["hookSpecificOutput"]["permissionDecision"])
        allowed = self.hook({
            "hook_event_name": "PreToolUse", "cwd": str(self.project / "work"),
            "tool_name": "Bash", "tool_input": {"command": command},
        })
        self.assertIsNone(allowed)
        extra = self.hook({
            "hook_event_name": "PreToolUse", "cwd": str(self.project / "work"),
            "tool_name": "Bash", "tool_input": {"command": command + " --extra"},
        })
        self.assertEqual("deny", extra["hookSpecificOutput"]["permissionDecision"])

    def test_zero_gpu_gate_rejects_gpu_command_and_blocks_workload_before_gate_recording(self) -> None:
        proposal = self.proposal()
        proposal["checkpoint_artifacts"].append({"id": "zero-gpu-proof", "path": "artifacts/zero-gpu.json", "required": True})
        proposal["pre_run_gates"] = [{
            "id": "zero-gpu", "kind": "resource", "description": "preparation must use no GPU",
            "required": True, "artifact_id": "zero-gpu-proof", "resource": "gpu", "operator": "max", "value": 0,
        }]
        proposal["review"]["checks"]["preflight_failure_closure_reviewed"] = True
        proposal["bash_policies"] = [{
            "id": "gpu-run", "phase": "workload", "executable": "torchrun", "fixed_args": ["train.py"],
            "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
        }]
        with self.assertRaisesRegex(goal_guard.GuardError, "conflicts with the pre-run GPU gate"):
            self.admit(proposal)

        proposal["bash_policies"] = [{
            "id": "cpu-run", "phase": "workload", "executable": "python3", "fixed_args": ["run_eval.py"],
            "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
        }]
        self.admit(proposal)
        denial = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("pre-run gates", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_checkpoint_verifies_preregistered_artifacts_sha_and_gate_results(self) -> None:
        proposal = self.proposal()
        proposal["checkpoint_artifacts"].append({"id": "zero-gpu-proof", "path": "artifacts/zero-gpu.json", "required": True})
        proposal["pre_run_gates"] = [{
            "id": "zero-gpu", "kind": "resource", "description": "preparation must use no GPU",
            "required": True, "artifact_id": "zero-gpu-proof", "resource": "gpu", "operator": "max", "value": 0,
        }, {
            "id": "optional-audit", "kind": "manual", "description": "optional audit note",
            "required": False, "artifact_id": "zero-gpu-proof",
        }]
        proposal["review"]["checks"]["preflight_failure_closure_reviewed"] = True
        proposal["bash_policies"] = [
            {"id": "prepare", "phase": "preparation", "executable": "python3", "fixed_args": ["prepare.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
            {"id": "workload", "phase": "workload", "executable": "python3", "fixed_args": ["run_eval.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
        ]
        self.admit(proposal)
        self.assertIsNone(self.pre("Bash", "python3 prepare.py"))
        proof = self.project / "artifacts/zero-gpu.json"
        proof.write_text('{"gpu": 0}\n', encoding="utf-8")
        proof_result = {"id": "zero-gpu-proof", "path": "artifacts/zero-gpu.json", "sha256": goal_guard.file_hash(proof)}
        gates = [{"id": "zero-gpu", "status": "PASS", "artifact_id": "zero-gpu-proof"}]
        gate_payload = {"schema_version": 2, "experiment_id": "E001", "artifact_results": [proof_result], "pre_run_gate_results": gates}
        self.record_gates(gate_payload)
        self.record_gates(gate_payload)
        changed_gates = gates + [{"id": "optional-audit", "status": "PASS", "artifact_id": "zero-gpu-proof"}]
        with self.assertRaisesRegex(goal_guard.GuardError, "different replay"):
            self.record_gates({**gate_payload, "pre_run_gate_results": changed_gates})
        self.assertEqual("deny", self.pre("Bash", "python3 prepare.py")["hookSpecificOutput"]["permissionDecision"])
        proof_patch = "*** Begin Patch\n*** Update File: artifacts/zero-gpu.json\n@@\n-{\"gpu\": 0}\n+{\"gpu\": 1}\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", proof_patch)["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("Bash", "python3 run_eval.py"))

        result = self.result("E001")
        result["artifact_results"].append(proof_result)
        result["pre_run_gate_results"] = gates
        bad = json.loads(json.dumps(result))
        bad["artifact_results"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(goal_guard.GuardError, "SHA-256 mismatched"):
            self.checkpoint(bad)
        self.checkpoint(result)

    def test_required_gate_fail_has_frozen_invalid_checkpoint_and_fresh_review_reentry(self) -> None:
        proposal = self.proposal()
        proposal["checkpoint_artifacts"].append({"id": "preflight-proof", "path": "artifacts/preflight.json", "required": True})
        proposal["pre_run_gates"] = [{
            "id": "preflight", "kind": "manual", "description": "runtime access monitor must pass",
            "required": True, "artifact_id": "preflight-proof",
        }]
        proposal["bash_policies"] = [
            {"id": "prepare", "phase": "preparation", "executable": "python3", "fixed_args": ["prepare.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
            {"id": "workload", "phase": "workload", "executable": "python3", "fixed_args": ["run_eval.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
        ]
        with self.assertRaisesRegex(goal_guard.GuardError, "preflight_failure_closure_reviewed"):
            self.admit(proposal)
        proposal["review"]["checks"]["preflight_failure_closure_reviewed"] = True
        self.admit(proposal)
        proof = self.project / "artifacts/preflight.json"
        proof.write_text('{"runtime_access_monitor": "FAIL"}\n', encoding="utf-8")
        proof_result = {"id": "preflight-proof", "path": "artifacts/preflight.json", "sha256": goal_guard.file_hash(proof)}
        failed_gates = [{"id": "preflight", "status": "FAIL", "artifact_id": "preflight-proof"}]
        gate_payload = {"schema_version": 2, "experiment_id": "E001", "artifact_results": [proof_result], "pre_run_gate_results": failed_gates}
        self.record_gates(gate_payload)
        control = goal_guard.load_control(self.project)
        self.assertTrue(control["active_lease"]["preflight_failed"])
        workload = self.pre("Bash", "python3 run_eval.py")
        self.assertIn("preflight gate failed", workload["hookSpecificOutput"]["permissionDecisionReason"])
        evidence_patch = "*** Begin Patch\n*** Update File: artifacts/preflight.json\n@@\n-FAIL\n+PASS\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", evidence_patch)["hookSpecificOutput"]["permissionDecision"])
        changed = {**gate_payload, "pre_run_gate_results": [{"id": "preflight", "status": "PASS", "artifact_id": "preflight-proof"}]}
        with self.assertRaisesRegex(goal_guard.GuardError, "different replay"):
            self.record_gates(changed)

        invalid = {
            "schema_version": 2, "experiment_id": "E001", "valid": False,
            "evaluation_integrity": "FAIL", "core_progress": False,
            "metric_delta": "preflight failed before workload", "outcome": "invalid",
            "decision": "PAUSE_REQUIRED", "artifact": "artifacts/preflight.json",
            "artifact_results": [proof_result], "pre_run_gate_results": failed_gates,
            "external_monitor_results": [],
        }
        state_patch = "*** Begin Patch\n*** Update File: optimization/STATE.md\n@@\n-old\n+changed\n*** End Patch"
        experiments_patch = "*** Begin Patch\n*** Update File: optimization/EXPERIMENTS.md\n@@\n-old\n+changed\n*** End Patch"
        result_patch = "*** Begin Patch\n*** Update File: optimization/RESULT.json\n@@\n-{}\n+{}\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", state_patch)["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("deny", self.pre("apply_patch", experiments_patch)["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("apply_patch", result_patch))

        for field, value in (
            ("valid", "__missing__"),
            ("valid", None),
            ("evaluation_integrity", "__missing__"),
            ("evaluation_integrity", "UNKNOWN"),
            ("core_progress", "__missing__"),
            ("core_progress", 0),
        ):
            with self.subTest(field=field, value=value):
                malformed = json.loads(json.dumps(invalid))
                if value == "__missing__":
                    malformed.pop(field)
                else:
                    malformed[field] = value
                with self.assertRaisesRegex(goal_guard.GuardError, "requires valid=false"):
                    self.checkpoint(malformed)
        self.assertIsNone(self.pre("apply_patch", result_patch))
        self.checkpoint(invalid)
        control = goal_guard.load_control(self.project)
        self.assertIsNone(control["active_lease"])
        self.assertFalse(control["chains"]["C-main"]["closed"])
        self.assertTrue(control["last_checkpoint"]["preflight_failed"])

        retry = self.proposal(experiment="E002")
        retry["review"]["reviewer"] = "subagent:fresh-review-2"
        self.admit(retry)
        self.assertEqual("E002", goal_guard.load_control(self.project)["active_lease"]["experiment_id"])

    def test_abort_preflight_materializes_checkpoint_and_releases_lease(self) -> None:
        proposal = self.proposal()
        proposal["checkpoint_artifacts"].append({"id": "preflight-proof", "path": "artifacts/preflight.json", "required": True})
        proposal["pre_run_gates"] = [{
            "id": "preflight", "kind": "manual", "description": "required runtime check",
            "required": True, "artifact_id": "preflight-proof",
        }]
        proposal["review"]["checks"]["preflight_failure_closure_reviewed"] = True
        self.admit(proposal)
        with self.assertRaisesRegex(goal_guard.GuardError, "only after a frozen required preflight FAIL"):
            goal_guard.command_abort_preflight(argparse.Namespace(project=str(self.project)))
        proof = self.project / "artifacts/preflight.json"
        proof.write_text('{"check": "FAIL"}\n', encoding="utf-8")
        proof_result = {"id": "preflight-proof", "path": "artifacts/preflight.json", "sha256": goal_guard.file_hash(proof)}
        failed_gates = [{"id": "preflight", "status": "FAIL", "artifact_id": "preflight-proof"}]
        self.record_gates({
            "schema_version": 2, "experiment_id": "E001",
            "artifact_results": [proof_result], "pre_run_gate_results": failed_gates,
        })
        status = goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), goal_guard.load_control(self.project))
        self.assertEqual("ABORT_PREFLIGHT", status["next_action"]["kind"])
        abort_command = f"{sys.executable} {ROOT / 'hooks/goal_guard.py'} abort --project {self.project}"
        abort_alias_command = f"{sys.executable} {ROOT / 'hooks/goal_guard.py'} abort-preflight --project {self.project}"
        self.assertIsNone(self.pre("Bash", abort_command))
        self.assertIsNone(self.pre("Bash", abort_alias_command))
        fake_abort = self.pre("Bash", "python3 /tmp/goal_guard.py abort --project .")
        self.assertEqual("deny", fake_abort["hookSpecificOutput"]["permissionDecision"])
        proof.write_text('{"check": "TAMPERED"}\n', encoding="utf-8")
        rejected = subprocess.run(
            [sys.executable, str(ROOT / "hooks/goal_guard.py"), "abort", "--project", str(self.project)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(2, rejected.returncode)
        self.assertIn("recorded pre-run gate evidence changed", rejected.stderr)
        self.assertIsNotNone(goal_guard.load_control(self.project)["active_lease"])
        proof.write_text('{"check": "FAIL"}\n', encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(ROOT / "hooks/goal_guard.py"), "abort", "--project", str(self.project)],
            text=True, capture_output=True, check=True,
        )
        self.assertIn('"preflight_failed": true', completed.stdout)
        result = json.loads((self.project / "optimization/RESULT.json").read_text(encoding="utf-8"))
        self.assertIs(result["valid"], False)
        self.assertEqual("FAIL", result["evaluation_integrity"])
        self.assertIs(result["core_progress"], False)
        self.assertEqual("invalid", result["outcome"])
        self.assertEqual("PAUSE_REQUIRED", result["decision"])
        control = goal_guard.load_control(self.project)
        self.assertIsNone(control["active_lease"])
        self.assertFalse(control["chains"]["C-main"]["closed"])
        self.assertEqual("FRESH_REVIEW", goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)["next_action"]["kind"])

    def test_long_lease_accepts_seven_days_and_rejects_more(self) -> None:
        proposal = self.proposal()
        proposal["expires_minutes"] = 7 * 24 * 60
        self.admit(proposal)
        control = goal_guard.load_control(self.project)
        issued = goal_guard.parse_time(control["active_lease"]["issued_at"])
        expires = goal_guard.parse_time(control["active_lease"]["expires_at"])
        self.assertEqual(7 * 24 * 60 * 60, int((expires - issued).total_seconds()))

        self.write_json("CONTROL.json", goal_guard.default_control())
        too_long = self.proposal(experiment="E002")
        too_long["expires_minutes"] = 7 * 24 * 60 + 1
        with self.assertRaisesRegex(goal_guard.GuardError, "1..10080"):
            self.admit(too_long)

    def test_waiting_external_event_preserves_lease_deduplicates_wake_and_stops_polling(self) -> None:
        proposal = self.proposal()
        proposal["lease_phase"] = "workload"
        proposal["checkpoint_artifacts"].append({"id": "terminal-event", "path": "artifacts/job-terminal.json", "required": True})
        proposal["bash_policies"].append({
            "id": "postflight", "phase": "postflight", "executable": "python3", "fixed_args": ["postflight.py"],
            "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
        })
        self.admit(proposal)
        args = argparse.Namespace(project=str(self.project), event_key="job-122020", event_path="artifacts/job-terminal.json")
        with contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_wait(args)
        control = goal_guard.load_control(self.project)
        self.assertEqual("WAITING_EXTERNAL_EVENT", control["runtime"]["state"])
        self.assertIn("remaining_seconds", control["active_lease"])
        self.assertIsNone(self.pre("Bash", "cat optimization/STATE.md"))
        self.assertEqual("deny", self.pre("Bash", "tail -f artifacts/job.log")["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("deny", self.pre("apply_patch", "*** Begin Patch\n*** Add File: src/adjacent.py\n+x = 1\n*** End Patch")["hookSpecificOutput"]["permissionDecision"])
        session = self.hook({"hook_event_name": "SessionStart", "cwd": str(self.project), "source": "resume"})
        self.assertIn("end this activation", session["hookSpecificOutput"]["additionalContext"])

        terminal = self.project / "artifacts/job-terminal.json"
        terminal.write_text('{"state": "COMPLETED"}\n', encoding="utf-8")
        original_proposal = (self.project / "optimization/PROPOSAL.json").read_text(encoding="utf-8")
        drifted = json.loads(original_proposal)
        drifted["hypothesis"] = "drift while waiting"
        self.write_json("PROPOSAL.json", drifted)
        with self.assertRaisesRegex(goal_guard.GuardError, "PROPOSAL.json changed"):
            goal_guard.command_wake(args)
        (self.project / "optimization/PROPOSAL.json").write_text(original_proposal, encoding="utf-8")
        gate = json.loads((self.project / "optimization/GATE.json").read_text())
        gate["enabled"] = False
        self.write_json("GATE.json", gate)
        with self.assertRaisesRegex(goal_guard.GuardError, "not activated"):
            goal_guard.command_wake(args)
        gate["enabled"] = True
        self.write_json("GATE.json", gate)
        with contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_wake(args)
        self.assertIsNone(self.pre("Bash", "python3 postflight.py"))
        terminal_patch = "*** Begin Patch\n*** Update File: artifacts/job-terminal.json\n@@\n-{\"state\": \"COMPLETED\"}\n+{\"state\": \"FAILED\"}\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", terminal_patch)["hookSpecificOutput"]["permissionDecision"])
        duplicate = io.StringIO()
        with contextlib.redirect_stdout(duplicate):
            goal_guard.command_wake(args)
        self.assertEqual("duplicate", duplicate.getvalue().strip())

        terminal.write_text('{"state": "MUTATED_AFTER_WAKE"}\n', encoding="utf-8")
        drifted_result = self.result("E001")
        drifted_result["artifact_results"].append({"id": "terminal-event", "path": "artifacts/job-terminal.json", "sha256": goal_guard.file_hash(terminal)})
        with self.assertRaisesRegex(goal_guard.GuardError, "frozen wake event"):
            self.checkpoint(drifted_result)
        terminal.write_text('{"state": "COMPLETED"}\n', encoding="utf-8")
        result = self.result("E001")
        result["artifact_results"].append({"id": "terminal-event", "path": "artifacts/job-terminal.json", "sha256": goal_guard.file_hash(terminal)})
        gate["enabled"] = False
        self.write_json("GATE.json", gate)
        with self.assertRaisesRegex(goal_guard.GuardError, "not activated"):
            self.checkpoint(result)
        gate["enabled"] = True
        self.write_json("GATE.json", gate)
        self.checkpoint(result)
        denial = self.pre("Bash", "python3 postflight.py")
        self.assertIn("missing or expired", denial["hookSpecificOutput"]["permissionDecisionReason"])

    def test_external_monitor_binds_once_consumes_bound_argv_and_materializes_receipt(self) -> None:
        proposal = self.external_monitor_proposal()
        self.admit(proposal)
        start_command = (
            f"{sys.executable} monitor_fake.py start 12345 --host fakehost --state-dir {self.monitor_state} "
            "--expected-owner alice --expected-job-name H25 --expected-partition gpu "
            f"--event-binding {self.monitor_state / 'event-binding.json'} "
            f"--bridge-config {self.monitor_state / 'bridge.json'} "
            "--bridge-service-name codex-monitor-test-bridge --require-auto-resume"
        )
        self.assertEqual("deny", self.pre("Bash", start_command)["hookSpecificOutput"]["permissionDecision"])
        direct_submit = "./sbatch --parsable train.sbatch"
        direct_denial = self.pre("Bash", direct_submit)
        self.assertIn("submit-bind", direct_denial["hookSpecificOutput"]["permissionDecisionReason"])

        self.submit_binding()
        control = goal_guard.load_control(self.project)
        self.assertEqual("12345", control["active_lease"]["binding_values"]["slurm-job"]["value"])
        with self.assertRaisesRegex(goal_guard.GuardError, "already consumed"):
            self.submit_binding()
        self.assertIsNone(self.pre("Bash", start_command))
        wrong_job = start_command.replace(" 12345 ", " 99999 ")
        self.assertEqual("deny", self.pre("Bash", wrong_job)["hookSpecificOutput"]["permissionDecision"])

        terminal, semantic_event = self.write_external_monitor_terminal()
        terminal.unlink()
        semantic_event.unlink()
        self.wait_monitor()
        waiting = goal_guard.load_control(self.project)
        self.assertEqual("external_monitor", waiting["runtime"]["wait"]["kind"])
        self.assertEqual("12345", waiting["runtime"]["wait"]["job_id"])
        _terminal, semantic_event = self.write_external_monitor_terminal()
        event_id = "sha256:" + semantic_event.parent.name
        self.assertFalse((self.monitor_state / "bridges").exists())
        with self.assertRaisesRegex(goal_guard.GuardError, "requested semantic event"):
            self.wake_monitor("sha256:" + "0" * 64)
        self.wake_monitor(event_id)
        active = goal_guard.load_control(self.project)["active_lease"]
        receipt = active["monitor_receipts"]["scheduler"]
        receipt_path = self.project / receipt["path"]
        self.assertTrue(receipt_path.is_file())
        receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("goal-guardrails.external-monitor-receipt/v2", receipt_payload["schema_version"])
        self.assertEqual(event_id, receipt_payload["source"]["semantic_event_id"])
        self.assertNotIn("bridge_receipt_sha256", receipt_payload["source"])
        self.assertEqual("pending", receipt_payload["business_verdict"])
        self.assertFalse(receipt_payload["project_gate_evaluated"])
        receipt_patch = f"*** Begin Patch\n*** Update File: {receipt['path']}\n@@\n-x\n+y\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", receipt_patch)["hookSpecificOutput"]["permissionDecision"])

        result = self.result("E001", core_progress=True, outcome="positive")
        result["external_monitor_results"] = [{"id": "scheduler", "path": receipt["path"], "sha256": receipt["sha256"]}]
        self.checkpoint(result)
        self.assertIsNone(goal_guard.load_control(self.project)["active_lease"])

    def test_external_monitor_rejects_unverified_or_identity_drifted_terminal(self) -> None:
        for terminal_verified, owner, expected_error in (
            (False, "alice", "verified monitor evidence chain"),
            (True, "mallory", "identity drifted"),
        ):
            with self.subTest(terminal_verified=terminal_verified, owner=owner):
                if goal_guard.load_control(self.project).get("active_lease") is not None:
                    self.write_json("CONTROL.json", goal_guard.default_control())
                self.monitor_state = Path(self.external_temporary.name) / f"monitor-{terminal_verified}-{owner}"
                proposal = self.external_monitor_proposal()
                self.admit(proposal)
                self.submit_binding()
                terminal, semantic_event = self.write_external_monitor_terminal()
                terminal.unlink()
                semantic_event.unlink()
                self.wait_monitor()
                self.write_external_monitor_terminal(terminal_verified=terminal_verified, owner=owner)
                with self.assertRaisesRegex(goal_guard.GuardError, expected_error):
                    self.wake_monitor()

    def test_external_monitor_rejects_semantic_event_or_workspace_drift(self) -> None:
        proposal = self.external_monitor_proposal()
        self.admit(proposal)
        self.submit_binding()
        terminal, semantic_event = self.write_external_monitor_terminal()
        terminal.unlink()
        semantic_event.unlink()
        self.wait_monitor()
        _terminal, semantic_event = self.write_external_monitor_terminal()
        event = json.loads(semantic_event.read_text(encoding="utf-8"))
        event["monitor"]["terminal_digest"] = "sha256:" + "0" * 64
        semantic_event.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(goal_guard.GuardError, "semantic event does not match"):
            self.wake_monitor()

        self.write_json("CONTROL.json", goal_guard.default_control())
        self.monitor_state = Path(self.external_temporary.name) / "monitor-wrong-workspace"
        proposal = self.external_monitor_proposal()
        self.admit(proposal)
        self.submit_binding()
        wrong_workspace = str(Path(self.external_temporary.name).resolve())
        terminal, semantic_event = self.write_external_monitor_terminal(workspace=wrong_workspace)
        terminal.unlink()
        semantic_event.unlink()
        self.wait_monitor()
        self.write_external_monitor_terminal(workspace=wrong_workspace)
        with self.assertRaisesRegex(goal_guard.GuardError, "different project workspace"):
            self.wake_monitor()

    def test_external_monitor_wakes_v060_wait_without_event_id_or_delivery_receipt(self) -> None:
        proposal = self.external_monitor_proposal()
        self.admit(proposal)
        self.submit_binding()
        terminal, semantic_event = self.write_external_monitor_terminal()
        terminal.unlink()
        semantic_event.unlink()
        self.wait_monitor()
        self.write_external_monitor_terminal()
        self.assertFalse((self.monitor_state / "bridges").exists())
        self.assertFalse(any(self.monitor_state.glob("outbox/*/delivery.json")))
        self.wake_monitor()
        control = goal_guard.load_control(self.project)
        self.assertEqual("ACTIVE", control["runtime"]["state"])
        receipt = control["active_lease"]["monitor_receipts"]["scheduler"]
        self.assertTrue(receipt["source_semantic_event_id"].startswith("sha256:"))
        self.assertEqual(0, self.wake_monitor(receipt["source_semantic_event_id"]))

    def test_required_external_monitor_rejects_project_artifact_wait(self) -> None:
        self.admit(self.external_monitor_proposal())
        args = argparse.Namespace(
            project=str(self.project), event_key="job-12345",
            event_path="artifacts/E001.json",
        )
        with self.assertRaisesRegex(goal_guard.GuardError, "must use wait-monitor"):
            goal_guard.command_wait(args)
        control = goal_guard.load_control(self.project)
        self.assertEqual("ACTIVE", control["runtime"]["state"])
        self.assertIsNone(control["runtime"]["wait"])

    def test_waiting_external_monitor_reports_pending_then_dead_letter(self) -> None:
        self.admit(self.external_monitor_proposal())
        self.submit_binding()
        terminal, semantic_event = self.write_external_monitor_terminal()
        terminal.unlink()
        semantic_event.unlink()
        self.wait_monitor()
        _terminal, semantic_event = self.write_external_monitor_terminal()
        event_id = "sha256:" + semantic_event.parent.name
        delivery_path = semantic_event.with_name("delivery.json")
        delivery = {
            "schema": "codex-monitor.delivery/v1",
            "event_id": event_id,
            "state": "pending",
            "attempts": 1,
            "last_error": {"code": None, "safe_message": None},
        }
        delivery_path.write_text(json.dumps(delivery) + "\n", encoding="utf-8")

        control = goal_guard.load_control(self.project)
        pending = goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)
        self.assertEqual("pending", pending["runtime"]["delivery"]["state"])
        self.assertEqual("WAIT", pending["next_action"]["kind"])

        delivery.update({
            "state": "dead_letter",
            "attempts": 2,
            "last_error": {"code": "operator_interaction_required", "safe_message": "manual action required"},
        })
        delivery_path.write_text(json.dumps(delivery) + "\n", encoding="utf-8")
        paused = goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)
        self.assertEqual("dead_letter", paused["runtime"]["delivery"]["state"])
        self.assertEqual("PAUSE_REQUIRED", paused["next_action"]["kind"])
        self.assertIn(event_id, paused["next_action"]["instruction"])
        self.assertEqual("WAITING_EXTERNAL_EVENT", goal_guard.load_control(self.project)["runtime"]["state"])

    def test_controller_state_migration_tracks_installed_and_current_hook_versions(self) -> None:
        legacy = goal_guard.default_control()
        legacy.pop("controller")
        self.write_json(goal_guard.CONTROL_REL.name, legacy)
        args = argparse.Namespace(project=str(self.project))
        with contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_status(args)
        migrated = goal_guard.load_control(self.project)
        metadata = migrated["controller"]
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], metadata["installed_plugin_version"])
        self.assertEqual(goal_guard.HOOK_VERSION, metadata["current_hook_version"])
        saved = json.loads((self.project / goal_guard.CONTROL_REL).read_text(encoding="utf-8"))
        self.assertEqual(metadata, saved["controller"])

    def test_external_monitor_checkpoint_requires_controller_receipt(self) -> None:
        proposal = self.external_monitor_proposal()
        self.admit(proposal)
        result = self.result("E001")
        result["external_monitor_results"] = []
        with self.assertRaisesRegex(goal_guard.GuardError, "required external monitor results"):
            self.checkpoint(result)

    def test_remote_submission_requires_reviewed_transport_and_doctor(self) -> None:
        proposal = self.remote_submission_proposal()
        proposal["review"]["checks"].pop("remote_submission_contract_bounded")
        with self.assertRaisesRegex(goal_guard.GuardError, "remote_submission_contract_bounded"):
            self.admit(proposal)

        proposal = self.remote_submission_proposal()
        self.admit(proposal)
        control = goal_guard.load_control(self.project)
        policy = control["active_lease"]["bash_policies"][0]
        ssh_argv = goal_guard.ssh_helper_argv(policy["transport"])
        self.assertEqual([policy["transport"]["ssh_executable"], "-F", "none", "-T"], ssh_argv[:4])
        for option in (
            "ProxyCommand=none", "ProxyJump=none", "GlobalKnownHostsFile=/dev/null",
            "IdentityAgent=none", "IdentitiesOnly=yes", "CanonicalizeHostname=no",
            "ClearAllForwardings=yes", "UpdateHostKeys=no",
        ):
            self.assertIn(option, ssh_argv)
        self.assertEqual(policy["transport"]["helper_path"], ssh_argv[-1])
        self.assertEqual("DOCTOR", goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)["next_action"]["kind"])
        direct_ssh = f"{self.project / 'ssh'} alice@hpc142 /opt/goal-guardrails/remote_submit_helper.py"
        self.assertEqual("deny", self.pre("Bash", direct_ssh)["hookSpecificOutput"]["permissionDecision"])
        with self.assertRaisesRegex(goal_guard.GuardError, "successful doctor"):
            self.submit_binding()

        def doctor_response(policy: dict, request: dict) -> tuple[dict, dict]:
            return ({
                "schema_version": goal_guard.REMOTE_DOCTOR_SCHEMA,
                "ready": True,
                "contract_sha256": request["contract_sha256"],
                "helper_sha256": policy["transport"]["helper_sha256"],
                "sbatch_sha256": "2" * 64,
                "remote_files": policy["transport"]["remote_files"],
            }, {"ssh_exit_code": 0, "stdout_sha256": "3" * 64, "stderr_sha256": "4" * 64})

        args = argparse.Namespace(project=str(self.project), policy="submit-slurm")
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=doctor_response), contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_doctor(args)
        control = goal_guard.load_control(self.project)
        self.assertEqual("READY", control["active_lease"]["transport_doctors"]["submit-slurm"]["state"])
        self.assertEqual(1, control["active_lease"]["budget_plan"]["required_one_shot_submissions"])
        self.assertEqual(0, control["active_lease"]["mutations_used"])
        control["runtime"] = {"state": "WAITING_EXTERNAL_EVENT", "wait": {"kind": "artifact"}}
        self.write_json("CONTROL.json", control)
        with self.assertRaisesRegex(goal_guard.GuardError, "while waiting"):
            goal_guard.command_doctor(args)

    def test_remote_doctor_runs_before_required_gates_without_consuming_budget(self) -> None:
        proposal = self.remote_submission_proposal()
        proposal["checkpoint_artifacts"].append({
            "id": "preflight-proof", "path": "artifacts/preflight.json", "required": True,
        })
        proposal["pre_run_gates"] = [{
            "id": "preflight", "kind": "manual", "description": "runtime check",
            "required": True, "artifact_id": "preflight-proof",
        }]
        proposal["review"]["checks"]["preflight_failure_closure_reviewed"] = True
        self.admit(proposal)
        status = goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), goal_guard.load_control(self.project))
        self.assertEqual("DOCTOR", status["next_action"]["kind"])

        def doctor_response(policy: dict, request: dict) -> tuple[dict, dict]:
            return ({
                "schema_version": goal_guard.REMOTE_DOCTOR_SCHEMA, "ready": True,
                "contract_sha256": request["contract_sha256"],
                "helper_sha256": policy["transport"]["helper_sha256"],
                "sbatch_sha256": "2" * 64, "remote_files": policy["transport"]["remote_files"],
            }, {"ssh_exit_code": 0, "stdout_sha256": "3" * 64, "stderr_sha256": "4" * 64})

        args = argparse.Namespace(project=str(self.project), policy="submit-slurm")
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=doctor_response), contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_doctor(args)
        control = goal_guard.load_control(self.project)
        self.assertEqual(0, control["active_lease"]["mutations_used"])
        self.assertEqual("RECORD_GATES", goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)["next_action"]["kind"])
        with self.assertRaisesRegex(goal_guard.GuardError, "pre-run gates are not recorded"):
            self.submit_binding()

    def test_remote_submission_uncertain_reconciles_without_resubmit(self) -> None:
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

        submit_calls = 0

        def uncertain_response(_policy: dict, request: dict) -> tuple[dict, dict]:
            nonlocal submit_calls
            submit_calls += 1
            return ({
                "schema_version": goal_guard.REMOTE_RECEIPT_SCHEMA,
                "submission_nonce": request["submission_nonce"], "contract_sha256": request["contract_sha256"],
                "helper_sha256": _policy["transport"]["helper_sha256"], "sbatch_sha256": request["sbatch_sha256"],
                "state": "UNCERTAIN", "exit_code": None,
                "stdout_sha256": "5" * 64, "stderr_sha256": "6" * 64,
            }, {"ssh_exit_code": 0, "stdout_sha256": "7" * 64, "stderr_sha256": "8" * 64})

        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=uncertain_response):
            with self.assertRaisesRegex(goal_guard.GuardError, "never resubmit"):
                self.submit_binding()
        control = goal_guard.load_control(self.project)
        binding = control["active_lease"]["binding_values"]["slurm-job"]
        self.assertEqual("UNCERTAIN", binding["state"])
        self.assertEqual(1, control["active_lease"]["mutations_used"])
        self.assertEqual("RECONCILE_BIND", goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)["next_action"]["kind"])
        with self.assertRaisesRegex(goal_guard.GuardError, "already consumed"):
            self.submit_binding()
        self.assertEqual(1, submit_calls)

        def reconciled_response(_policy: dict, request: dict) -> tuple[dict, dict]:
            return ({
                "schema_version": goal_guard.REMOTE_RECEIPT_SCHEMA,
                "submission_nonce": request["submission_nonce"], "contract_sha256": request["contract_sha256"],
                "helper_sha256": _policy["transport"]["helper_sha256"], "sbatch_sha256": request["sbatch_sha256"],
                "state": "SUCCEEDED", "job_id": "24680",
            }, {"ssh_exit_code": 0, "stdout_sha256": "9" * 64, "stderr_sha256": "a" * 64})

        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=reconciled_response), contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_reconcile_bind(args)
        control = goal_guard.load_control(self.project)
        self.assertEqual("BOUND", control["active_lease"]["binding_values"]["slurm-job"]["state"])
        self.assertEqual("24680", control["active_lease"]["binding_values"]["slurm-job"]["value"])
        self.assertEqual(1, control["active_lease"]["mutations_used"])

    def test_remote_submission_transport_failure_stays_uncertain(self) -> None:
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
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=RuntimeError("ssh disconnected")):
            with self.assertRaisesRegex(goal_guard.GuardError, "reconcile"):
                self.submit_binding()
        control = goal_guard.load_control(self.project)
        self.assertEqual("UNCERTAIN", control["active_lease"]["binding_values"]["slurm-job"]["state"])
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=RuntimeError("still disconnected")):
            with self.assertRaisesRegex(goal_guard.GuardError, "remains uncertain"):
                goal_guard.command_reconcile_bind(args)
        self.assertEqual("UNCERTAIN", goal_guard.load_control(self.project)["active_lease"]["binding_values"]["slurm-job"]["state"])

    def test_remote_doctor_unexpected_failure_is_recoverable(self) -> None:
        self.admit(self.remote_submission_proposal())
        args = argparse.Namespace(project=str(self.project), policy="submit-slurm")
        with mock.patch.object(goal_guard, "run_remote_helper", side_effect=RuntimeError("unexpected transport failure")):
            with self.assertRaisesRegex(RuntimeError, "unexpected transport failure"):
                goal_guard.command_doctor(args)
        control = goal_guard.load_control(self.project)
        self.assertEqual("FAILED", control["active_lease"]["transport_doctors"]["submit-slurm"]["state"])
        self.assertEqual("DOCTOR", goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), control)["next_action"]["kind"])
        self.assertEqual(0, control["active_lease"]["mutations_used"])

    def test_remote_submission_full_fake_ssh_helper_path(self) -> None:
        proposal = self.remote_submission_proposal()
        transport = proposal["bash_policies"][0]["transport"]
        fake_ssh = Path(transport["ssh_executable"])
        fake_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess, sys\n"
            "completed = subprocess.run([sys.executable, sys.argv[-1]], input=sys.stdin.read(), text=True, capture_output=True)\n"
            "sys.stdout.write(completed.stdout)\n"
            "sys.stderr.write(completed.stderr)\n"
            "raise SystemExit(completed.returncode)\n",
            encoding="utf-8",
        )
        fake_ssh.chmod(0o700)
        transport["ssh_executable_sha256"] = goal_guard.file_hash(fake_ssh)
        remote_sbatch = self.project / "remote-bin/sbatch"
        remote_sbatch.parent.mkdir()
        remote_sbatch.write_text("#!/usr/bin/env python3\nprint('97531;cluster')\n", encoding="utf-8")
        remote_sbatch.chmod(0o700)
        receipt_root = Path(self.external_temporary.name) / "submission-receipts"
        receipt_root.mkdir()
        receipt_root.chmod(0o700)
        proposal["bash_policies"][0]["executable"] = str(remote_sbatch)
        transport["helper_path"] = str(ROOT / "hooks/remote_submit_helper.py")
        transport["sbatch_path"] = str(remote_sbatch)
        transport["remote_workdir"] = str(self.project)
        transport["receipt_root"] = str(receipt_root)
        self.admit(proposal)
        args = argparse.Namespace(project=str(self.project), policy="submit-slurm")
        with contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_doctor(args)
            goal_guard.command_submit_bind(args)
        control = goal_guard.load_control(self.project)
        binding = control["active_lease"]["binding_values"]["slurm-job"]
        self.assertEqual("BOUND", binding["state"])
        self.assertEqual("97531", binding["value"])
        receipts = list(receipt_root.glob("*.json"))
        self.assertEqual(1, len(receipts))
        self.assertEqual("SUCCEEDED", json.loads(receipts[0].read_text(encoding="utf-8"))["state"])

    def test_remote_submission_rejects_transport_binary_or_file_drift(self) -> None:
        proposal = self.remote_submission_proposal()
        proposal["bash_policies"][0]["transport"]["ssh_executable_sha256"] = "0" * 64
        with self.assertRaisesRegex(goal_guard.GuardError, "executable SHA-256 mismatched"):
            self.admit(proposal)

        proposal = self.remote_submission_proposal()
        proposal["bash_policies"][0]["transport"]["helper_path"] = "/tmp/$(touch-owned)"
        with self.assertRaisesRegex(goal_guard.GuardError, "safe absolute path"):
            self.admit(proposal)

        proposal = self.remote_submission_proposal()
        self.admit(proposal)
        transport = proposal["bash_policies"][0]["transport"]
        Path(transport["known_hosts_file"]).write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(goal_guard.GuardError, "known_hosts_file changed"):
            goal_guard.command_doctor(argparse.Namespace(project=str(self.project), policy="submit-slurm"))

        self.write_json("CONTROL.json", goal_guard.default_control())
        proposal = self.remote_submission_proposal()
        self.admit(proposal)
        (self.project / "train.sbatch").write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        with self.assertRaisesRegex(goal_guard.GuardError, "local file changed"):
            goal_guard.command_doctor(argparse.Namespace(project=str(self.project), policy="submit-slurm"))

    def test_external_monitor_admission_requires_review_and_exact_start_identity(self) -> None:
        proposal = self.external_monitor_proposal()
        proposal["review"]["checks"].pop("external_monitor_contract_bounded")
        with self.assertRaisesRegex(goal_guard.GuardError, "external_monitor_contract_bounded"):
            self.admit(proposal)
        proposal = self.external_monitor_proposal()
        for token in proposal["bash_policies"][1]["argv"]:
            if token.get("literal") == "alice":
                token["literal"] = "mallory"
        with self.assertRaisesRegex(goal_guard.GuardError, "--expected-owner alice"):
            self.admit(proposal)
        proposal = self.external_monitor_proposal()
        proposal["bash_policies"][1]["argv"] = [
            token for token in proposal["bash_policies"][1]["argv"]
            if token.get("literal") != "--require-auto-resume"
        ]
        with self.assertRaisesRegex(goal_guard.GuardError, "--require-auto-resume"):
            self.admit(proposal)

    def test_slurm_runtime_binding_requires_sbatch_parsable_source(self) -> None:
        proposal = self.external_monitor_proposal()
        proposal["bash_policies"][0]["executable"] = sys.executable
        with self.assertRaisesRegex(goal_guard.GuardError, "sbatch"):
            self.admit(proposal)

        proposal = self.external_monitor_proposal()
        proposal["bash_policies"][0]["argv"] = [{"literal": "train.sbatch"}]
        with self.assertRaisesRegex(goal_guard.GuardError, "--parsable"):
            self.admit(proposal)

    def test_submit_bind_malformed_output_consumes_one_shot_policy(self) -> None:
        proposal = self.external_monitor_proposal()
        (self.project / "sbatch").write_text("#!/usr/bin/env python3\nprint('not-a-job')\n", encoding="utf-8")
        self.admit(proposal)
        with self.assertRaisesRegex(goal_guard.GuardError, "parsable Slurm Job ID"):
            self.submit_binding()
        control = goal_guard.load_control(self.project)
        self.assertEqual("FAILED", control["active_lease"]["binding_values"]["slurm-job"]["state"])
        with self.assertRaisesRegex(goal_guard.GuardError, "already consumed"):
            self.submit_binding()

    def test_legacy_active_lease_remains_hook_and_checkpoint_compatible(self) -> None:
        legacy_proposal = {"schema_version": 1, "experiment_id": "LEGACY-1", "review": {"decision": "ALLOW"}}
        self.write_json("PROPOSAL.json", legacy_proposal)
        lease = {
            "schema_version": 1, "lease_id": "legacy", "experiment_id": "LEGACY-1", "chain_id": "C-legacy",
            "allowed_paths": ["src"], "allowed_command_prefixes": ["python3 legacy.py"],
            "issued_at": goal_guard.iso_time(goal_guard.utc_now()),
            "expires_at": goal_guard.iso_time(goal_guard.utc_now() + goal_guard.timedelta(minutes=10)),
            "max_mutations": 2, "mutations_used": 0, "finalization_used": False,
            "final_discriminator": False, "goal_sha256": goal_guard.file_hash(self.project / "optimization/GOAL.md"),
            "proposal_sha256": goal_guard.canonical_hash(legacy_proposal),
        }
        control = goal_guard.default_control()
        control["active_lease"] = lease
        control["chains"]["C-legacy"] = {
            "chain_kind": "optimization", "parent_chain": None, "causal_bottleneck": "legacy",
            "no_progress_count": 0, "non_core_cost_units": 0, "stopline_fired": False,
            "final_discriminator_used": False, "closed": False, "close_outcome": None,
        }
        goal_guard.save_control(self.project, control)
        self.assertIsNone(self.pre("Bash", "python3 legacy.py --small"))
        legacy_result = {
            "schema_version": 1, "experiment_id": "LEGACY-1", "valid": True,
            "evaluation_integrity": "PASS", "core_progress": False, "metric_delta": "0",
            "outcome": "zero_progress", "decision": "CONTINUE", "artifact": "legacy-report",
        }
        self.checkpoint(legacy_result)
        self.assertIsNone(goal_guard.load_control(self.project)["active_lease"])

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

    def test_fast_profile_defaults_for_legacy_gate_and_allows_routine_yolo_work(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate.pop("profile", None)
        self.write_json("GATE.json", gate)
        self.assertEqual("fast", goal_guard.load_gate(self.project)["profile"])

        routine_patch = "*** Begin Patch\n*** Add File: src/fast.py\n+value = 1\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", routine_patch))
        self.assertIsNone(self.pre("Bash", "python3 train.py --resume latest && pytest -q"))
        self.assertIsNone(self.pre("Bash", "rm -rf build"))
        self.assertIsNone(self.pre("Bash", "git add src/fast.py && git commit -m fast && git push origin main"))
        self.assertIsNone(self.pre("Bash", "ssh hpc142 nvidia-smi"))
        self.assertIsNone(self.pre("Bash", "sudo apt-get install -y jq"))
        self.assertIsNone(self.pre_input("mcp__filesystem__write_file", {"path": "src/fast.py", "content": "x"}))

        status = goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), goal_guard.load_control(self.project))
        self.assertEqual("fast", status["profile"])
        self.assertEqual("CONTINUE_FAST", status["next_action"]["kind"])

    def test_fast_profile_blocks_only_high_impact_boundary_and_says_continue(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate["profile"] = "fast"
        self.write_json("GATE.json", gate)

        protected_patch = "*** Begin Patch\n*** Update File: optimization/GOAL.md\n@@\n-Primary metric\n+Different metric\n*** End Patch"
        protected = self.pre("apply_patch", protected_patch)
        self.assertEqual("deny", protected["hookSpecificOutput"]["permissionDecision"])
        reason = protected["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Do not ask the user merely", reason)
        self.assertIn("do not stop the Goal", reason)

        destructive = self.pre("Bash", "git reset --hard HEAD")
        self.assertEqual("deny", destructive["hookSpecificOutput"]["permissionDecision"])
        protected_mcp = self.pre_input(
            "mcp__filesystem__write_file",
            {"path": "optimization/GOAL.md", "content": "different objective"},
        )
        self.assertEqual("deny", protected_mcp["hookSpecificOutput"]["permissionDecision"])
        protected_mcp_command = self.pre_input(
            "mcp__remote__execute_command",
            {"command": "python3 -c \"open('optimization/GATE.json','w').write('{}')\""},
        )
        self.assertEqual("deny", protected_mcp_command["hookSpecificOutput"]["permissionDecision"])
        protected_python = self.pre("Bash", "python3 -c \"open('optimization/GOAL.md','w').write('x')\"")
        self.assertEqual("deny", protected_python["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("Bash", "echo optimization/GOAL.md"))
        self.assertIsNone(self.pre("Bash", "printf '%s\\n' optimization/GATE.json"))
        self.assertIsNone(self.pre("Bash", "git status --short; python3 run_eval.py"))

    def test_fast_admission_needs_no_external_review_and_does_not_scope_routine_work(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate["profile"] = "fast"
        self.write_json("GATE.json", gate)
        proposal = self.proposal()
        proposal.pop("review")
        self.admit(proposal)
        control = goal_guard.load_control(self.project)
        self.assertEqual("controller:fast", control["active_lease"]["review"]["reviewer"])

        outside_patch = "*** Begin Patch\n*** Add File: docs/notes.md\n+routine notes\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", outside_patch))
        self.assertIsNone(self.pre("Bash", "python3 unrelated_but_in_scope.py --small"))
        evidence_patch = "*** Begin Patch\n*** Update File: reports/baseline.json\n@@\n-{\"accepted\": 0}\n+{\"accepted\": 99}\n*** End Patch"
        self.assertEqual("deny", self.pre("apply_patch", evidence_patch)["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual(
            "deny",
            self.pre("Bash", "sed -i s/0/99/ reports/baseline.json")["hookSpecificOutput"]["permissionDecision"],
        )
        self.assertEqual(
            "deny",
            self.pre("Bash", "python3 -c \"open('reports/baseline.json','w').write('{}')\"")["hookSpecificOutput"]
            ["permissionDecision"],
        )
        self.assertEqual(
            "deny",
            self.pre_input("mcp__filesystem__write_file", {"path": "reports/baseline.json", "content": "{}"})[
                "hookSpecificOutput"
            ]["permissionDecision"],
        )
        self.assertEqual(0, goal_guard.load_control(self.project)["active_lease"]["mutations_used"])

    def test_fast_one_shot_capture_cannot_be_wrapped_or_redirected(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate["profile"] = "fast"
        self.write_json("GATE.json", gate)
        proposal = self.external_monitor_proposal()
        proposal.pop("review")
        self.admit(proposal)

        exact = self.pre("Bash", "./sbatch --parsable train.sbatch")
        redirected = self.pre("Bash", "./sbatch --parsable train.sbatch >/tmp/job-id")
        compounded = self.pre("Bash", "./sbatch --parsable train.sbatch && echo submitted")
        self.assertEqual("deny", exact["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("deny", redirected["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("deny", compounded["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("Bash", "echo './sbatch --parsable train.sbatch'"))

    def test_fast_remote_submission_input_is_frozen_against_arbitrary_script_write(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate["profile"] = "fast"
        self.write_json("GATE.json", gate)
        proposal = self.remote_submission_proposal()
        proposal.pop("review")
        self.admit(proposal)

        overwrite = self.pre("Bash", "python3 -c \"open('train.sbatch','w').write('changed')\"")
        self.assertEqual("deny", overwrite["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("Bash", "cat train.sbatch"))

    def test_fast_waiting_allows_polling_but_not_mutation_and_session_says_unattended(self) -> None:
        gate = goal_guard.load_gate(self.project)
        gate["profile"] = "fast"
        self.write_json("GATE.json", gate)
        control = goal_guard.load_control(self.project)
        control["runtime"] = {"state": "WAITING_EXTERNAL_EVENT", "wait": {"kind": "artifact"}, "seen_events": []}
        self.write_json("CONTROL.json", control)

        self.assertIsNone(self.pre("Bash", "tail -f artifacts/job.log"))
        self.assertIsNone(self.pre("Bash", "squeue -j 12345 | tail -n 1"))
        compound_poll = self.pre("Bash", "squeue -j 12345; python3 mutate.py")
        self.assertEqual("deny", compound_poll["hookSpecificOutput"]["permissionDecision"])
        redirected_poll = self.pre("Bash", "squeue -j 12345 >/tmp/status")
        self.assertEqual("deny", redirected_poll["hookSpecificOutput"]["permissionDecision"])
        gpu_mutation = self.pre("Bash", "nvidia-smi --gpu-reset")
        self.assertEqual("deny", gpu_mutation["hookSpecificOutput"]["permissionDecision"])
        gpu_assignment = self.pre("Bash", "nvidia-smi --power-limit=250")
        self.assertEqual("deny", gpu_assignment["hookSpecificOutput"]["permissionDecision"])
        preprocessor_poll = self.pre("Bash", "squeue -j 12345 | rg --pre dangerous")
        self.assertEqual("deny", preprocessor_poll["hookSpecificOutput"]["permissionDecision"])
        status = goal_guard.compact_status(self.project, goal_guard.load_gate(self.project), goal_guard.load_control(self.project))
        self.assertEqual("WAIT", status["next_action"]["kind"])
        self.assertIn("do not ask the user", status["next_action"]["instruction"])
        self.assertNotIn("End this activation", status["next_action"]["instruction"])
        mutation = self.pre("apply_patch", "*** Begin Patch\n*** Add File: src/while-waiting.py\n+x = 1\n*** End Patch")
        self.assertEqual("deny", mutation["hookSpecificOutput"]["permissionDecision"])
        session = self.hook({"hook_event_name": "SessionStart", "cwd": str(self.project), "source": "resume"})
        context = session["hookSpecificOutput"]["additionalContext"]
        self.assertIn("unattended execution", context)
        self.assertIn("without a lease, external review, or user approval", context)
        self.assertIn("Controller checkpoint is only for an optional active lease", context)
        self.assertIn("do not stop the Goal", context)

    def test_mode_command_changes_profile_only_with_user_attestation(self) -> None:
        with self.assertRaisesRegex(goal_guard.GuardError, "require --approved-by user"):
            goal_guard.command_mode(argparse.Namespace(project=str(self.project), profile="fast", approved_by="agent"))
        with contextlib.redirect_stdout(io.StringIO()):
            goal_guard.command_mode(argparse.Namespace(project=str(self.project), profile="fast", approved_by="user"))
        self.assertEqual("fast", goal_guard.load_gate(self.project)["profile"])

    def test_result_remains_correctable_after_mutation_cap(self) -> None:
        proposal = self.proposal()
        proposal["max_mutations"] = 1
        self.admit(proposal)
        self.assertIsNone(self.pre("Bash", "python3 run_eval.py"))
        result_patch = "*** Begin Patch\n*** Update File: optimization/RESULT.json\n@@\n-{}\n+{}\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", result_patch))
        self.assertEqual("deny", self.pre("Bash", "python3 run_eval.py")["hookSpecificOutput"]["permissionDecision"])
        self.assertIsNone(self.pre("apply_patch", result_patch))
        state_patch = "*** Begin Patch\n*** Update File: optimization/STATE.md\n@@\n-old\n+new\n*** End Patch"
        self.assertIsNone(self.pre("apply_patch", state_patch))
        self.assertEqual("deny", self.pre("apply_patch", state_patch)["hookSpecificOutput"]["permissionDecision"])

    def test_protected_contract_file_is_never_in_proposal_scope(self) -> None:
        proposal = self.proposal()
        proposal["lease_mutations"] = [{"path": "optimization/GOAL.md", "scope": "exact", "operations": ["update"]}]
        with self.assertRaisesRegex(goal_guard.GuardError, "protected path"):
            self.admit(proposal)

    def test_compound_shell_and_interpreter_prefixes_are_rejected(self) -> None:
        proposal = self.proposal()
        proposal["bash_policies"][0]["executable"] = "bash"
        with self.assertRaisesRegex(goal_guard.GuardError, "non-shell"):
            self.admit(proposal)
        self.admit(self.proposal())
        denial = self.pre("Bash", "python3 run_eval.py && python3 unrelated.py")
        self.assertIn("structured lease policy", denial["hookSpecificOutput"]["permissionDecisionReason"])
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
        self.assertIn("recoverable control transition", context)
        self.assertIn('"next_action"', context)

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
        self.assertIn("not Goal completion", specific["permissionDecisionReason"])

    def test_cli_status_is_valid_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "hooks/goal_guard.py"), "status", "--project", str(self.project)],
            text=True, capture_output=True, check=True,
        )
        status = json.loads(completed.stdout)
        self.assertTrue(status["enabled"])
        self.assertEqual("ADMIT_NEXT", status["next_action"]["kind"])
        self.assertIsNone(status["active_experiment"])

    def test_only_the_bundled_controller_gets_controller_bypass(self) -> None:
        real = f"{sys.executable} {ROOT / 'hooks/goal_guard.py'} status --project {self.project}"
        self.assertTrue(goal_guard.is_controller_command(real))
        doctor = f"{sys.executable} {ROOT / 'hooks/goal_guard.py'} doctor --policy submit-slurm --project {self.project}"
        reconcile = f"{sys.executable} {ROOT / 'hooks/goal_guard.py'} reconcile-bind --policy submit-slurm --project {self.project}"
        self.assertTrue(goal_guard.is_controller_command(doctor))
        self.assertTrue(goal_guard.is_controller_command(reconcile))
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
            proposal["lease_mutations"] = [{"path": "src/link", "scope": "tree", "operations": ["add", "update"]}]
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
