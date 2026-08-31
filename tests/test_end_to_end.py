from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "skills/goal-guardrails/scripts/init_project.py"
GUARD = ROOT / "hooks/goal_guard.py"


def goal_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def attest_review(project: Path, proposal: dict, *, profile: str = "strict") -> None:
    contract = dict(proposal)
    contract.pop("review", None)
    payload = {
        "schema": "goal-guardrails.review-subject/v1",
        "goal_sha256": goal_hash(project / "optimization/GOAL.md"),
        "profile": profile,
        "review_epoch": json.loads((project / "optimization/CONTROL.json").read_text(encoding="utf-8")).get("review_epoch", 0),
        "proposal": contract,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    proposal["review"]["subject_sha256"] = "sha256:" + hashlib.sha256(canonical).hexdigest()


class EndToEndLeaseFlowTests(unittest.TestCase):
    def run_cli(self, *args: object, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *(str(value) for value in args)],
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
        )

    def hook(self, project: Path, tool: str, tool_input: dict) -> dict | None:
        event = {"hook_event_name": "PreToolUse", "cwd": str(project), "tool_name": tool, "tool_input": tool_input}
        completed = self.run_cli(GUARD, "hook", input_text=json.dumps(event))
        return json.loads(completed.stdout) if completed.stdout.strip() else None

    def test_fast_unattended_flow_runs_without_lease_review_or_poll_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.run_cli(INIT, project)
            optimization = project / "optimization"
            (optimization / "GOAL.md").write_text("# Goal\n\nMetric: accepted output yield\n", encoding="utf-8")
            (optimization / "STATE.md").write_text("# State\n\n- frontier: baseline\n", encoding="utf-8")
            self.run_cli(GUARD, "activate", "--approved-by", "user", "--project", project)

            status = json.loads(self.run_cli(GUARD, "status", "--project", project).stdout)
            self.assertEqual("fast", status["profile"])
            self.assertEqual("CONTINUE_FAST", status["next_action"]["kind"])
            self.assertIsNone(self.hook(project, "apply_patch", {"patch": "*** Begin Patch\n*** Add File: src/candidate.py\n+x = 1\n*** End Patch"}))
            self.assertIsNone(self.hook(project, "Bash", {"command": "python3 train.py --overnight && pytest -q"}))
            self.assertIsNone(self.hook(project, "mcp__filesystem__write_file", {"path": "artifacts/result.json", "content": "{}"}))

            for _ in range(5):
                event = {
                    "hook_event_name": "PostToolUse", "cwd": str(project), "tool_name": "Bash",
                    "tool_input": {"command": "squeue -j 12345"}, "tool_response": {"state": "RUNNING"},
                }
                completed = self.run_cli(GUARD, "hook", input_text=json.dumps(event))
                self.assertEqual("", completed.stdout)

            protected = self.hook(project, "apply_patch", {"patch": "*** Begin Patch\n*** Update File: optimization/GOAL.md\n@@\n-Metric\n+Other metric\n*** End Patch"})
            self.assertEqual("deny", protected["hookSpecificOutput"]["permissionDecision"])
            self.assertIn("do not stop the Goal", protected["hookSpecificOutput"]["permissionDecisionReason"])

    def test_initialize_activate_and_run_two_checkpointed_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.run_cli(INIT, project)
            optimization = project / "optimization"
            (optimization / "GOAL.md").write_text("# Goal\n\nMetric: accepted output yield\n", encoding="utf-8")
            (optimization / "STATE.md").write_text("# State\n\n- frontier: baseline\n", encoding="utf-8")
            reports = project / "reports"
            reports.mkdir()
            (reports / "baseline.json").write_text('{"accepted": 0}\n', encoding="utf-8")
            artifacts = project / "artifacts"
            artifacts.mkdir()
            self.run_cli(GUARD, "activate", "--approved-by", "user", "--project", project)
            self.run_cli(GUARD, "mode", "strict", "--approved-by", "user", "--project", project)

            for index in range(2):
                experiment = f"E{index + 1:03d}"
                proposal = {
                    "schema_version": 2,
                    "experiment_id": experiment,
                    "chain_id": "C-e2e",
                    "chain_kind": "optimization",
                    "parent_chain": None,
                    "causal_bottleneck": "end to end renderer quality",
                    "hypothesis": "one renderer parameter increases accepted outputs",
                    "core_progress_expected": "one more accepted output",
                    "lease_phase": "synchronous",
                    "existing_evidence": [{"id": "baseline", "path": "reports/baseline.json", "sha256": goal_hash(reports / "baseline.json"), "claim": "baseline accepted output yield is zero"}],
                    "lease_mutations": [{"path": "artifacts", "scope": "tree", "operations": ["add", "update"]}],
                    "checkpoint_artifacts": [{"id": "primary-result", "path": f"artifacts/{experiment}.json", "required": True}],
                    "pre_run_gates": [],
                    "bash_policies": [{"id": "eval", "phase": "evaluation", "executable": "python3", "fixed_args": ["run_eval.py", "--small"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}}],
                    "expires_minutes": 10,
                    "max_mutations": 2,
                    "work_class": "core",
                    "cost_units": 1,
                    "final_discriminator": False,
                    "next_paths": None,
                    "review": {"decision": "ALLOW", "reviewer": f"subagent:r{index}", "reason": "bounded direct test", "checks": {"evidence_sufficient": True, "lease_mutations_bounded": True, "pre_run_gates_sufficient": True, "mutation_not_required_before_admission": True}},
                }
                attest_review(project, proposal)
                proposal_path = optimization / "PROPOSAL.json"
                proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
                self.run_cli(GUARD, "admit", proposal_path, "--project", project)
                if index == 0:
                    admitted = json.loads(self.run_cli(GUARD, "status", "--project", project).stdout)
                    self.run_cli(
                        GUARD, "release", "--expected-proposal-sha256", admitted["active_proposal_sha256"],
                        "--reason", "correct an unconsumed output contract", "--project", project,
                    )
                    stale_review = self.run_cli(GUARD, "admit", proposal_path, "--project", project, check=False)
                    self.assertNotEqual(0, stale_review.returncode)
                    self.assertIn("fresh review attestation", stale_review.stderr)
                    proposal["review"]["reviewer"] = "subagent:r0-fresh"
                    proposal["review"]["reason"] = "fresh review after safe release"
                    attest_review(project, proposal)
                    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
                    self.run_cli(GUARD, "admit", proposal_path, "--project", project)

                hook_event = {
                    "hook_event_name": "PreToolUse",
                    "cwd": str(project),
                    "tool_name": "Bash",
                    "tool_input": {"command": "python3 run_eval.py --small"},
                }
                hook = self.run_cli(GUARD, "hook", input_text=json.dumps(hook_event))
                self.assertEqual("", hook.stdout)

                artifact_path = artifacts / f"{experiment}.json"
                artifact_path.write_text(json.dumps({"experiment": experiment, "accepted": index}) + "\n", encoding="utf-8")
                result = {
                    "schema_version": 2,
                    "experiment_id": experiment,
                    "valid": True,
                    "evaluation_integrity": "PASS",
                    "core_progress": index == 1,
                    "metric_delta": "0" if index == 0 else "+1 accepted output",
                    "outcome": "zero_progress" if index == 0 else "positive",
                    "decision": "CONTINUE",
                    "artifact": f"artifacts/{experiment}.json",
                    "artifact_results": [{"id": "primary-result", "path": f"artifacts/{experiment}.json", "sha256": goal_hash(artifact_path)}],
                    "pre_run_gate_results": [],
                }
                result_path = optimization / "RESULT.json"
                result_path.write_text(json.dumps(result), encoding="utf-8")
                self.run_cli(GUARD, "checkpoint", result_path, "--project", project)

            status = json.loads(self.run_cli(GUARD, "status", "--project", project).stdout)
            self.assertIsNone(status["active_experiment"])
            self.assertEqual(0, status["chains"]["C-e2e"]["no_progress_count"])
            self.assertEqual("E002", status["last_checkpoint"]["experiment_id"])
            staged_goal = project / "GOAL.next.md"
            staged_goal.write_text("# Goal\n\nMetric: accepted final output yield\n", encoding="utf-8")
            self.run_cli(
                GUARD, "update-goal", "--approved-by", "user",
                "--expected-sha256", goal_hash(optimization / "GOAL.md"),
                "--from-file", staged_goal, "--reason", "clarify the user-approved metric", "--project", project,
            )
            updated = json.loads(self.run_cli(GUARD, "status", "--project", project).stdout)
            self.assertTrue(updated["enabled"])
            self.assertEqual(1, updated["goal"]["revision"])

    def test_gated_async_lifecycle_from_disabled_admission_through_old_lease_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.run_cli(INIT, project)
            optimization = project / "optimization"
            (optimization / "GOAL.md").write_text("# Goal\n\nMetric: final quality\n", encoding="utf-8")
            (optimization / "STATE.md").write_text("# State\n\n- frontier: baseline\n", encoding="utf-8")
            reports = project / "reports"
            reports.mkdir()
            baseline = reports / "baseline.json"
            baseline.write_text('{"quality": 0}\n', encoding="utf-8")
            artifacts = project / "artifacts"
            artifacts.mkdir()
            proposal = {
                "schema_version": 2, "experiment_id": "ASYNC-001", "chain_id": "C-async",
                "chain_kind": "optimization", "parent_chain": None, "causal_bottleneck": "final renderer quality",
                "hypothesis": "one frozen workload improves final quality", "core_progress_expected": "one accepted final output",
                "lease_phase": "workload",
                "existing_evidence": [{"id": "baseline", "path": "reports/baseline.json", "sha256": goal_hash(baseline), "claim": "baseline quality is zero"}],
                "lease_mutations": [{"path": "artifacts", "scope": "tree", "operations": ["add", "update"]}],
                "checkpoint_artifacts": [
                    {"id": "gate-proof", "path": "artifacts/zero-gpu.json", "required": True},
                    {"id": "terminal-event", "path": "artifacts/job-terminal.json", "required": True},
                    {"id": "primary-result", "path": "artifacts/final.json", "required": True},
                ],
                "pre_run_gates": [{"id": "zero-gpu", "kind": "resource", "description": "preparation uses zero GPU", "required": True, "artifact_id": "gate-proof", "resource": "gpu", "operator": "max", "value": 0}],
                "bash_policies": [
                    {"id": "prepare", "phase": "preparation", "executable": "python3", "fixed_args": ["prepare.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                    {"id": "workload", "phase": "workload", "executable": "python3", "fixed_args": ["submit.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                    {"id": "postflight", "phase": "postflight", "executable": "python3", "fixed_args": ["postflight.py"], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                ],
                "expires_minutes": 10, "max_mutations": 6, "work_class": "core", "cost_units": 1,
                "final_discriminator": False, "next_paths": None,
                "review": {"decision": "ALLOW", "reviewer": "subagent:e2e-review", "reason": "future mutations and gates are bounded", "checks": {"evidence_sufficient": True, "lease_mutations_bounded": True, "pre_run_gates_sufficient": True, "mutation_not_required_before_admission": True}},
            }
            proposal["review"]["checks"]["preflight_failure_closure_reviewed"] = True
            attest_review(project, proposal)
            proposal_path = optimization / "PROPOSAL.json"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            disabled = self.run_cli(GUARD, "admit", proposal_path, "--project", project, check=False)
            self.assertNotEqual(0, disabled.returncode)
            self.assertIn("gate is not activated", disabled.stderr)

            self.run_cli(GUARD, "activate", "--approved-by", "user", "--project", project)
            self.run_cli(GUARD, "mode", "strict", "--approved-by", "user", "--project", project)
            self.run_cli(GUARD, "admit", proposal_path, "--project", project)
            outside_patch = "*** Begin Patch\n*** Add File: docs/drift.md\n+drift\n*** End Patch"
            self.assertEqual("deny", self.hook(project, "apply_patch", {"patch": outside_patch})["hookSpecificOutput"]["permissionDecision"])
            self.assertIsNone(self.hook(project, "Bash", {"command": "python3 prepare.py"}))
            self.assertEqual("deny", self.hook(project, "Bash", {"command": "torchrun train.py"})["hookSpecificOutput"]["permissionDecision"])

            proof = artifacts / "zero-gpu.json"
            proof.write_text('{"gpu": 0}\n', encoding="utf-8")
            gate_results = [{"id": "zero-gpu", "status": "PASS", "artifact_id": "gate-proof"}]
            pre_run = {"schema_version": 2, "experiment_id": "ASYNC-001", "artifact_results": [{"id": "gate-proof", "path": "artifacts/zero-gpu.json", "sha256": goal_hash(proof)}], "pre_run_gate_results": gate_results}
            pre_run_path = optimization / "PRE_RUN_RESULTS.json"
            pre_run_path.write_text(json.dumps(pre_run), encoding="utf-8")
            self.run_cli(GUARD, "gates", pre_run_path, "--project", project)
            self.assertIsNone(self.hook(project, "Bash", {"command": "python3 submit.py"}))

            self.run_cli(GUARD, "wait", "--event-key", "job-122020", "--event-path", "artifacts/job-terminal.json", "--project", project)
            waiting_poll = self.hook(project, "Bash", {"command": "tail -f artifacts/job.log"})
            self.assertEqual("deny", waiting_poll["hookSpecificOutput"]["permissionDecision"])
            self.assertIsNone(self.hook(project, "Bash", {"command": "cat optimization/STATE.md"}))
            terminal = artifacts / "job-terminal.json"
            terminal.write_text('{"state": "COMPLETED"}\n', encoding="utf-8")
            self.run_cli(GUARD, "wake", "--event-key", "job-122020", "--event-path", "artifacts/job-terminal.json", "--project", project)
            duplicate = self.run_cli(GUARD, "wake", "--event-key", "job-122020", "--event-path", "artifacts/job-terminal.json", "--project", project)
            self.assertEqual("duplicate", duplicate.stdout.strip())
            terminal_patch = "*** Begin Patch\n*** Update File: artifacts/job-terminal.json\n@@\n-{\"state\": \"COMPLETED\"}\n+{\"state\": \"FAILED\"}\n*** End Patch"
            frozen_event = self.hook(project, "apply_patch", {"patch": terminal_patch})
            self.assertEqual("deny", frozen_event["hookSpecificOutput"]["permissionDecision"])
            self.assertIsNone(self.hook(project, "Bash", {"command": "python3 postflight.py"}))

            primary = artifacts / "final.json"
            primary.write_text('{"quality": 1}\n', encoding="utf-8")
            artifact_results = [
                {"id": "gate-proof", "path": "artifacts/zero-gpu.json", "sha256": goal_hash(proof)},
                {"id": "terminal-event", "path": "artifacts/job-terminal.json", "sha256": goal_hash(terminal)},
                {"id": "primary-result", "path": "artifacts/final.json", "sha256": goal_hash(primary)},
            ]
            result = {"schema_version": 2, "experiment_id": "ASYNC-001", "valid": True, "evaluation_integrity": "PASS", "core_progress": True, "metric_delta": "+1 accepted output", "outcome": "positive", "decision": "CONTINUE", "artifact": "artifacts/final.json", "artifact_results": artifact_results, "pre_run_gate_results": gate_results}
            result_path = optimization / "RESULT.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            self.run_cli(GUARD, "checkpoint", result_path, "--project", project)
            old_lease = self.hook(project, "Bash", {"command": "python3 postflight.py"})
            self.assertEqual("deny", old_lease["hookSpecificOutput"]["permissionDecision"])

    def test_cli_submit_bind_freezes_dynamic_argv_and_rejects_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.run_cli(INIT, project)
            optimization = project / "optimization"
            (optimization / "GOAL.md").write_text("# Goal\n\nMetric: completed work\n", encoding="utf-8")
            (optimization / "STATE.md").write_text("# State\n\n- frontier: ready\n", encoding="utf-8")
            reports = project / "reports"
            reports.mkdir()
            baseline = reports / "baseline.json"
            baseline.write_text('{"completed": 0}\n', encoding="utf-8")
            artifacts = project / "artifacts"
            artifacts.mkdir()
            sbatch = project / "sbatch"
            sbatch.write_text("#!/usr/bin/env python3\nprint('24680;cluster')\n", encoding="utf-8")
            sbatch.chmod(0o700)
            proposal = {
                "schema_version": 2, "experiment_id": "BIND-001", "chain_id": "C-bind",
                "chain_kind": "optimization", "parent_chain": None,
                "causal_bottleneck": "external workload not yet submitted",
                "hypothesis": "one reviewed submission creates the intended job",
                "core_progress_expected": "one completed project artifact", "lease_phase": "workload",
                "existing_evidence": [{"id": "baseline", "path": "reports/baseline.json", "sha256": goal_hash(baseline), "claim": "no completed work"}],
                "lease_mutations": [{"path": "artifacts", "scope": "tree", "operations": ["add", "update"]}],
                "checkpoint_artifacts": [{"id": "primary-result", "path": "artifacts/final.json", "required": True}],
                "pre_run_gates": [],
                "runtime_bindings": [{"id": "job", "kind": "slurm_job_id", "source_policy_id": "submit", "required": True}],
                "bash_policies": [
                    {"id": "submit", "phase": "workload", "executable": "./sbatch", "argv": [{"literal": "--parsable"}, {"literal": "train.sbatch"}], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}, "capture_binding": "job", "max_uses": 1, "timeout_seconds": 10},
                    {"id": "consume", "phase": "postflight", "executable": sys.executable, "argv": [{"literal": "consume.py"}, {"binding": "job"}], "cwd": ".", "output_paths": [], "resources": {"gpu": 0}},
                ],
                "external_monitors": [], "expires_minutes": 10, "max_mutations": 3,
                "work_class": "core", "cost_units": 1, "final_discriminator": False, "next_paths": None,
                "review": {"decision": "ALLOW", "reviewer": "subagent:bind-review", "reason": "one-shot submission is bounded", "checks": {"evidence_sufficient": True, "lease_mutations_bounded": True, "pre_run_gates_sufficient": True, "mutation_not_required_before_admission": True}},
            }
            attest_review(project, proposal)
            proposal_path = optimization / "PROPOSAL.json"
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
            self.run_cli(GUARD, "activate", "--approved-by", "user", "--project", project)
            self.run_cli(GUARD, "mode", "strict", "--approved-by", "user", "--project", project)
            self.run_cli(GUARD, "admit", proposal_path, "--project", project)
            bound = self.run_cli(GUARD, "submit-bind", "--policy", "submit", "--project", project)
            self.assertEqual("24680", json.loads(bound.stdout)["value"])
            retry = self.run_cli(GUARD, "submit-bind", "--policy", "submit", "--project", project, check=False)
            self.assertNotEqual(0, retry.returncode)
            self.assertIn("already consumed", retry.stderr)
            consume = f"{sys.executable} consume.py 24680"
            self.assertIsNone(self.hook(project, "Bash", {"command": consume}))
            wrong = f"{sys.executable} consume.py 13579"
            self.assertEqual("deny", self.hook(project, "Bash", {"command": wrong})["hookSpecificOutput"]["permissionDecision"])


if __name__ == "__main__":
    unittest.main()
