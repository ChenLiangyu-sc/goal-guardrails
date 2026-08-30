#!/usr/bin/env python3
"""Restricted remote helper for idempotent Slurm submission receipts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any


REQUEST_SCHEMA = "goal-guardrails.remote-submit.request/v1"
RESPONSE_SCHEMA = "goal-guardrails.remote-submit.receipt/v1"
DOCTOR_SCHEMA = "goal-guardrails.remote-submit.doctor/v1"
NONCE_RE = re.compile(r"^[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_RE = re.compile(r"^[0-9]+(?:_[0-9]+)?$")


class HelperError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise HelperError(f"{field} must be a lowercase SHA-256")
    return value


def require_absolute(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or any(char in value for char in "\n\r\0;&|`<>"):
        raise HelperError(f"{field} must be a safe absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise HelperError(f"{field} must be absolute")
    return path


def regular_executable(path: Path, field: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise HelperError(f"{field} must be a regular non-symlink file")
    mode = path.stat().st_mode
    if not mode & stat.S_IXUSR:
        raise HelperError(f"{field} must be owner-executable")
    return path


def atomic_json(path: Path, value: dict[str, Any], *, replace: bool) -> None:
    if path.is_symlink():
        raise HelperError(f"refusing symlink receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and path.exists():
        raise FileExistsError(path)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if not replace:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise
            finally:
                temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HelperError(f"receipt is missing or unsafe: {path}")
    status = path.stat()
    if status.st_uid != os.getuid() or status.st_nlink != 1 or stat.S_IMODE(status.st_mode) & 0o077:
        raise HelperError(f"receipt ownership, link count, or mode is unsafe: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HelperError("receipt root must be an object")
    return value


def validate_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != REQUEST_SCHEMA:
        raise HelperError("unsupported remote-submit request schema")
    operation = raw.get("operation")
    if operation not in {"doctor", "submit", "reconcile"}:
        raise HelperError("operation must be doctor, submit, or reconcile")
    contract_sha = require_sha(raw.get("contract_sha256"), "contract_sha256")
    helper_sha = require_sha(raw.get("helper_sha256"), "helper_sha256")
    receipt_root = require_absolute(raw.get("receipt_root"), "receipt_root")
    if receipt_root.is_symlink() or not receipt_root.is_dir():
        raise HelperError("receipt_root must already exist as a regular directory")
    root_status = receipt_root.stat()
    if root_status.st_uid != os.getuid() or stat.S_IMODE(root_status.st_mode) & 0o077:
        raise HelperError("receipt_root must be owned by the helper user and inaccessible to group/other")
    if operation != "reconcile" and not os.access(receipt_root, os.W_OK):
        raise HelperError("receipt_root is not writable")
    nonce = None
    if operation != "doctor":
        nonce = raw.get("submission_nonce")
        if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
            raise HelperError("submission_nonce must be 32 lowercase hex characters")
    if operation == "reconcile":
        return {
            "operation": operation,
            "contract_sha256": contract_sha,
            "helper_sha256": helper_sha,
            "sbatch_sha256": require_sha(raw.get("sbatch_sha256"), "sbatch_sha256"),
            "receipt_root": receipt_root,
            "submission_nonce": nonce,
        }
    sbatch_path = regular_executable(require_absolute(raw.get("sbatch_path"), "sbatch_path"), "sbatch_path")
    if sbatch_path.name != "sbatch":
        raise HelperError("sbatch_path basename must be sbatch")
    workdir = require_absolute(raw.get("remote_workdir"), "remote_workdir")
    if workdir.is_symlink() or not workdir.is_dir():
        raise HelperError("remote_workdir must be a regular directory")
    remote_files = raw.get("remote_files")
    if not isinstance(remote_files, list) or not remote_files:
        raise HelperError("remote_files must be a non-empty list")
    verified_files: list[dict[str, str]] = []
    for index, item in enumerate(remote_files):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise HelperError(f"remote_files[{index}] must contain path and sha256")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise HelperError("remote file paths must stay below remote_workdir")
        target = workdir / relative
        if target.is_symlink() or not target.is_file():
            raise HelperError(f"remote file is missing or unsafe: {relative}")
        expected = require_sha(item.get("sha256"), f"remote_files[{index}].sha256")
        actual = digest_file(target)
        if actual != expected:
            raise HelperError(f"remote file SHA-256 mismatched: {relative}")
        verified_files.append({"path": relative.as_posix(), "sha256": actual})
    argv = raw.get("argv", [])
    if operation == "submit":
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and "\0" not in item for item in argv):
            raise HelperError("submit argv must be a non-empty string list")
        if sum(item == "--parsable" for item in argv) != 1:
            raise HelperError("submit argv requires exactly one --parsable")
    expected_sbatch_sha = raw.get("sbatch_sha256")
    if operation == "submit":
        expected_sbatch_sha = require_sha(expected_sbatch_sha, "sbatch_sha256")
        if digest_file(sbatch_path) != expected_sbatch_sha:
            raise HelperError("sbatch binary changed after doctor")
    return {
        "operation": operation,
        "contract_sha256": contract_sha,
        "helper_sha256": helper_sha,
        "sbatch_path": sbatch_path,
        "sbatch_sha256": digest_file(sbatch_path),
        "remote_workdir": workdir,
        "receipt_root": receipt_root,
        "remote_files": verified_files,
        "submission_nonce": nonce,
        "argv": argv,
        "timeout_seconds": int(raw.get("timeout_seconds", 120)),
    }


def verify_helper_sha(request: dict[str, Any]) -> str:
    helper_sha = digest_file(Path(__file__).resolve())
    if helper_sha != request["helper_sha256"]:
        raise HelperError("remote helper SHA-256 mismatched")
    return helper_sha


def receipt_path(request: dict[str, Any]) -> Path:
    return request["receipt_root"] / f"{request['submission_nonce']}.json"


def validate_receipt(receipt: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    if (
        receipt.get("schema_version") != RESPONSE_SCHEMA
        or receipt.get("submission_nonce") != request["submission_nonce"]
        or receipt.get("contract_sha256") != request["contract_sha256"]
        or receipt.get("helper_sha256") != request["helper_sha256"]
        or receipt.get("sbatch_sha256") != request["sbatch_sha256"]
    ):
        raise HelperError("existing receipt does not match this frozen submission contract")
    return receipt


def doctor(request: dict[str, Any], helper_sha: str) -> dict[str, Any]:
    return {
        "schema_version": DOCTOR_SCHEMA,
        "ready": True,
        "contract_sha256": request["contract_sha256"],
        "helper_sha256": helper_sha,
        "sbatch_path": os.fspath(request["sbatch_path"]),
        "sbatch_sha256": request["sbatch_sha256"],
        "remote_workdir": os.fspath(request["remote_workdir"]),
        "receipt_root": os.fspath(request["receipt_root"]),
        "remote_files": request["remote_files"],
        "checked_at": now(),
    }


def reconcile(request: dict[str, Any]) -> dict[str, Any]:
    path = receipt_path(request)
    if not path.exists():
        return {
            "schema_version": RESPONSE_SCHEMA,
            "submission_nonce": request["submission_nonce"],
            "contract_sha256": request["contract_sha256"],
            "helper_sha256": request["helper_sha256"],
            "sbatch_sha256": request["sbatch_sha256"],
            "state": "ABSENT",
            "checked_at": now(),
        }
    return validate_receipt(load_json(path), request)


def submit(request: dict[str, Any]) -> dict[str, Any]:
    path = receipt_path(request)
    running = {
        "schema_version": RESPONSE_SCHEMA,
        "submission_nonce": request["submission_nonce"],
        "contract_sha256": request["contract_sha256"],
        "state": "RUNNING",
        "created_at": now(),
        "helper_sha256": request["helper_sha256"],
        "sbatch_sha256": request["sbatch_sha256"],
        "argv_sha256": digest_bytes(json.dumps(request["argv"], separators=(",", ":")).encode()),
    }
    try:
        atomic_json(path, running, replace=False)
    except FileExistsError:
        return validate_receipt(load_json(path), request)
    try:
        completed = subprocess.run(
            [os.fspath(request["sbatch_path"]), *request["argv"]],
            cwd=request["remote_workdir"],
            text=True,
            capture_output=True,
            check=False,
            timeout=request["timeout_seconds"],
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        raw_value = lines[0] if len(lines) == 1 else ""
        job_id = raw_value.split(";", 1)[0]
        if completed.returncode == 0 and JOB_ID_RE.fullmatch(job_id) is not None:
            state = "SUCCEEDED"
        elif completed.returncode == 0:
            state = "UNCERTAIN"
        else:
            state = "FAILED"
        terminal = {
            **running,
            "state": state,
            "finished_at": now(),
            "exit_code": completed.returncode,
            "job_id": job_id if state == "SUCCEEDED" else None,
            "stdout_sha256": digest_bytes(completed.stdout.encode()),
            "stderr_sha256": digest_bytes(completed.stderr.encode()),
        }
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        terminal = {
            **running,
            "state": "UNCERTAIN",
            "finished_at": now(),
            "exit_code": None,
            "job_id": None,
            "stdout_sha256": digest_bytes(stdout.encode() if isinstance(stdout, str) else stdout),
            "stderr_sha256": digest_bytes(stderr.encode() if isinstance(stderr, str) else stderr),
        }
    atomic_json(path, terminal, replace=True)
    return terminal


def main() -> int:
    try:
        raw = json.load(sys.stdin)
        request = validate_request(raw)
        helper_sha = verify_helper_sha(request)
        if request["operation"] == "doctor":
            response = doctor(request, helper_sha)
        elif request["operation"] == "reconcile":
            response = reconcile(request)
        else:
            response = submit(request)
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (HelperError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
