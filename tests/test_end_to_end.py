from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "skills/goal-guardrails/scripts/init_project.py"
GUARD = ROOT / "hooks/goal_guard.py"


class EndToEndLeaseFlowTests(unittest.TestCase):
    def run_cli(self, *args: object, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *(str(value) for value in args)],
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_initialize_activate_and_run_two_checkpointed_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self.run_cli(INIT, project)
            optimization = project / "optimization"
            (optimization / "GOAL.md").write_text("# Goal\n\nMetric: accepted output yield\n", encoding="utf-8")
            (optimization / "STATE.md").write_text("# State\n\n- frontier: baseline\n", encoding="utf-8")
            self.run_cli(GUARD, "activate", "--approved-by", "user", "--project", project)

            for index in range(2):
                experiment = f"E{index + 1:03d}"
                proposal = {
                    "schema_version": 1,
                    "experiment_id": experiment,
                    "chain_id": "C-e2e",
                    "chain_kind": "optimization",
                    "parent_chain": None,
                    "causal_bottleneck": "end to end renderer quality",
                    "hypothesis": "one renderer parameter increases accepted outputs",
                    "core_progress_expected": "one more accepted output",
                    "allowed_paths": ["src", "optimization/RESULT.json"],
                    "allowed_command_prefixes": ["python3 run_eval.py"],
                    "expires_minutes": 10,
                    "max_mutations": 2,
                    "work_class": "core",
                    "cost_units": 1,
                    "final_discriminator": False,
                    "next_paths": None,
                    "review": {"decision": "ALLOW", "reviewer": f"subagent:r{index}", "reason": "bounded direct test"},
                }
                proposal_path = optimization / "PROPOSAL.json"
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

                result = {
                    "schema_version": 1,
                    "experiment_id": experiment,
                    "valid": True,
                    "evaluation_integrity": "PASS",
                    "core_progress": index == 1,
                    "metric_delta": "0" if index == 0 else "+1 accepted output",
                    "outcome": "zero_progress" if index == 0 else "positive",
                    "decision": "CONTINUE",
                    "artifact": f"reports/{experiment}.json",
                }
                result_path = optimization / "RESULT.json"
                result_path.write_text(json.dumps(result), encoding="utf-8")
                self.run_cli(GUARD, "checkpoint", result_path, "--project", project)

            status = json.loads(self.run_cli(GUARD, "status", "--project", project).stdout)
            self.assertIsNone(status["active_experiment"])
            self.assertEqual(0, status["chains"]["C-e2e"]["no_progress_count"])
            self.assertEqual("E002", status["last_checkpoint"]["experiment_id"])


if __name__ == "__main__":
    unittest.main()
