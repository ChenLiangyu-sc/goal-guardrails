from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "hooks/remote_submit_helper.py"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RemoteSubmitHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.receipts = self.root / "receipts"
        self.work.mkdir()
        self.receipts.mkdir()
        self.receipts.chmod(0o700)
        self.script = self.work / "train.sbatch"
        self.script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.counter = self.root / "submissions.txt"
        self.sbatch = self.root / "bin/sbatch"
        self.sbatch.parent.mkdir()
        self.write_sbatch("print('12345;cluster')")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_sbatch(self, body: str) -> None:
        self.sbatch.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"counter = Path({str(self.counter)!r})\n"
            "counter.write_text(counter.read_text() + '1\\n' if counter.exists() else '1\\n')\n"
            + body + "\n",
            encoding="utf-8",
        )
        self.sbatch.chmod(0o700)

    def request(self, operation: str, nonce: str = "a" * 32) -> dict:
        payload = {
            "schema_version": "goal-guardrails.remote-submit.request/v1",
            "operation": operation,
            "contract_sha256": "1" * 64,
            "helper_sha256": sha(HELPER),
            "sbatch_path": str(self.sbatch),
            "remote_workdir": str(self.work),
            "receipt_root": str(self.receipts),
            "remote_files": [{"path": "train.sbatch", "sha256": sha(self.script)}],
        }
        if operation != "doctor":
            payload.update({
                "submission_nonce": nonce,
                "sbatch_sha256": sha(self.sbatch),
            })
        if operation == "submit":
            payload.update({"argv": ["--parsable", "train.sbatch"], "timeout_seconds": 2})
        return payload

    def run_helper(self, payload: dict, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER)], input=json.dumps(payload), text=True,
            capture_output=True, check=check,
        )

    def test_doctor_submit_reconcile_and_duplicate_are_idempotent(self) -> None:
        doctor = json.loads(self.run_helper(self.request("doctor")).stdout)
        self.assertTrue(doctor["ready"])
        self.assertEqual(sha(self.sbatch), doctor["sbatch_sha256"])

        submit_request = self.request("submit")
        first = json.loads(self.run_helper(submit_request).stdout)
        self.assertEqual("SUCCEEDED", first["state"])
        self.assertEqual("12345", first["job_id"])
        second = json.loads(self.run_helper(submit_request).stdout)
        self.assertEqual(first, second)
        self.assertEqual(["1"], self.counter.read_text(encoding="utf-8").splitlines())

        reconciled = json.loads(self.run_helper(self.request("reconcile")).stdout)
        self.assertEqual("SUCCEEDED", reconciled["state"])
        self.assertEqual("12345", reconciled["job_id"])

    def test_existing_running_receipt_never_resubmits(self) -> None:
        nonce = "b" * 32
        receipt = {
            "schema_version": "goal-guardrails.remote-submit.receipt/v1",
            "submission_nonce": nonce,
            "contract_sha256": "1" * 64,
            "state": "RUNNING",
            "created_at": "2026-08-30T00:00:00Z",
            "helper_sha256": sha(HELPER),
            "sbatch_sha256": sha(self.sbatch),
        }
        (self.receipts / f"{nonce}.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        (self.receipts / f"{nonce}.json").chmod(0o600)
        response = json.loads(self.run_helper(self.request("submit", nonce)).stdout)
        self.assertEqual("RUNNING", response["state"])
        self.assertFalse(self.counter.exists())

    def test_timeout_is_uncertain_and_same_nonce_does_not_retry(self) -> None:
        self.write_sbatch("import time\ntime.sleep(2)\nprint('12345')")
        request = self.request("submit", "c" * 32)
        request["sbatch_sha256"] = sha(self.sbatch)
        request["timeout_seconds"] = 1
        first = json.loads(self.run_helper(request).stdout)
        self.assertEqual("UNCERTAIN", first["state"])
        second = json.loads(self.run_helper(request).stdout)
        self.assertEqual("UNCERTAIN", second["state"])
        self.assertEqual(["1"], self.counter.read_text(encoding="utf-8").splitlines())

    def test_doctor_rejects_remote_file_drift(self) -> None:
        request = self.request("doctor")
        request["remote_files"][0]["sha256"] = "0" * 64
        completed = self.run_helper(request, check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("remote file SHA-256 mismatched", completed.stderr)

    def test_reconcile_absent_is_identity_bound_and_never_submits(self) -> None:
        response = json.loads(self.run_helper(self.request("reconcile", "d" * 32)).stdout)
        self.assertEqual("ABSENT", response["state"])
        self.assertEqual(sha(HELPER), response["helper_sha256"])
        self.assertEqual(sha(self.sbatch), response["sbatch_sha256"])
        self.assertFalse(self.counter.exists())

    def test_unsafe_existing_receipt_is_rejected_without_resubmit(self) -> None:
        nonce = "e" * 32
        receipt = {
            "schema_version": "goal-guardrails.remote-submit.receipt/v1",
            "submission_nonce": nonce, "contract_sha256": "1" * 64,
            "helper_sha256": sha(HELPER), "sbatch_sha256": sha(self.sbatch),
            "state": "RUNNING", "created_at": "2026-08-30T00:00:00Z",
        }
        path = self.receipts / f"{nonce}.json"
        path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        path.chmod(0o644)
        completed = self.run_helper(self.request("submit", nonce), check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("mode is unsafe", completed.stderr)
        self.assertFalse(self.counter.exists())


if __name__ == "__main__":
    unittest.main()
