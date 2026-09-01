#!/usr/bin/env python3
"""Small deterministic experiment-lease controller and Codex hook handler."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Iterator


SCHEMA_VERSION = 1
HOOK_VERSION = "0.8.0"
PROPOSAL_SCHEMA_VERSION = 3
RESULT_SCHEMA_VERSION = 3
SUPPORTED_PROPOSAL_SCHEMA_VERSIONS = {2, 3}
SUPPORTED_RESULT_SCHEMA_VERSIONS = {2, 3}
MAX_LEASE_MINUTES = 7 * 24 * 60
CONTROLLER_PATH = Path(__file__).resolve()
REMOTE_HELPER_PATH = CONTROLLER_PATH.with_name("remote_submit_helper.py")
REMOTE_REQUEST_SCHEMA = "goal-guardrails.remote-submit.request/v1"
REMOTE_RECEIPT_SCHEMA = "goal-guardrails.remote-submit.receipt/v1"
REMOTE_DOCTOR_SCHEMA = "goal-guardrails.remote-submit.doctor/v1"
GATE_REL = Path("optimization/GATE.json")
CONTROL_REL = Path("optimization/CONTROL.json")
GOAL_REL = Path("optimization/GOAL.md")
STATE_REL = Path("optimization/STATE.md")
PROPOSAL_REL = Path("optimization/PROPOSAL.json")
RESULT_REL = Path("optimization/RESULT.json")
PRE_RUN_RESULTS_REL = Path("optimization/PRE_RUN_RESULTS.json")
CONTROLLER_STATE_REL = Path("optimization/.goal-guardrails")
GOAL_UPDATE_REL = CONTROLLER_STATE_REL / "goal-update.json"
RECEIPTS_REL = CONTROLLER_STATE_REL / "receipts"
CHECKPOINTS_REL = CONTROLLER_STATE_REL / "checkpoints"
EXPERIMENTS_REL = Path("optimization/EXPERIMENTS.md")
BACKLOG_REL = Path("optimization/BACKLOG.md")
ALWAYS_LEASE_PATHS = (STATE_REL, RESULT_REL, PRE_RUN_RESULTS_REL, EXPERIMENTS_REL, BACKLOG_REL)
FINALIZATION_PATHS = (STATE_REL, RESULT_REL, EXPERIMENTS_REL)
PROTECTED_PATHS = (GOAL_REL, GATE_REL, CONTROL_REL, PROPOSAL_REL)
DECISIONS = {"CONTINUE", "REPLICATE", "SWITCH", "ROLLBACK", "PAUSE_REQUIRED", "COMPLETE"}
OUTCOMES = {"positive", "negative", "zero_progress", "inconclusive", "invalid"}
EVALUATOR_STATES = {"complete", "missing", "corrupt"}
COMPLETENESS_STATES = {"complete", "incomplete"}
DETERMINACY_STATES = {"determinate", "indeterminate"}
EVALUATION_RESULTS = {"pass", "fail", "not_evaluated"}
RECOVERY_KINDS = {"recovery", "rollback", "switch"}
HARD_BLOCK_REASONS = {
    "objective_change_required",
    "high_risk_external_action_required",
    "external_input_unavailable",
    "all_paths_exhausted",
    "global_stopline_fired",
}
CHAIN_KINDS = {"optimization", "diagnostic", "verification"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PATCH_DIRECTIVE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.MULTILINE)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_PREFIX_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SLURM_JOB_ID_RE = re.compile(r"^[0-9]+(?:_[0-9]+)?$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
REMOTE_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
REMOTE_SHELL_PATH_RE = re.compile(r"^/[A-Za-z0-9_./:+-]+$")
RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]+$")
GPU_COMMAND_RE = re.compile(r"(?:^|[/_-])(nvidia-smi|torchrun|deepspeed)(?:$|\s)|\baccelerate\s+launch\b|CUDA_VISIBLE_DEVICES", re.I)
POLL_RE = re.compile(r"\b(squeue|sacct|qstat|kubectl\s+get|docker\s+ps|systemctl\s+status|nvidia-smi|tail\b|ps\b)", re.I)
FAST_DESTRUCTIVE_RE = re.compile(
    r"(?:^|\s)(?:rm\s+(?:-[A-Za-z]*[rf][A-Za-z]*\s+)+(?:/|~|\$HOME|\.\.?)(?:\s|$)|"
    r"git\s+reset\s+--hard\b|git\s+clean\s+-[A-Za-z]*[fdx][A-Za-z]*\b|"
    r"git\s+push\b[^\n]*(?:--force(?:-with-lease)?\b|-f(?:\s|$))|"
    r"terraform\s+destroy\b|kubectl\s+delete\b|docker\s+system\s+prune\b|"
    r"(?:shutdown|reboot|poweroff|mkfs(?:\.[A-Za-z0-9]+)?)\b|"
    r"DROP\s+(?:DATABASE|SCHEMA|TABLE)\b)",
    re.I,
)
FAST_PROTECTED_PATHS = (GOAL_REL, GATE_REL, CONTROL_REL)


class GuardError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GuardError(f"required regular file is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GuardError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise GuardError(f"JSON root must be an object: {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise GuardError(f"refusing symbolic-link state path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, value: str) -> None:
    if path.is_symlink():
        raise GuardError(f"refusing symbolic-link state path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def state_lock(project: Path) -> Iterator[None]:
    lock_path = project / "optimization/.goal-guardrails.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass


def find_project(cwd: Path) -> Path | None:
    current = Path(os.path.abspath(os.fspath(cwd)))
    for candidate in (current, *current.parents):
        if (candidate / GATE_REL).is_file():
            return candidate
        if (candidate / ".git").exists():
            return None
    return None


def explicit_project(raw: str | None) -> Path:
    root = find_project(Path(raw or os.getcwd()))
    if root is None:
        raise GuardError("no optimization/GATE.json found in this directory or its parents")
    return root


def load_gate(project: Path) -> dict[str, Any]:
    gate = load_json(project / GATE_REL)
    if gate.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("unsupported GATE.json schema_version")
    declared_root = gate.get("project_root")
    if declared_root is not None and Path(str(declared_root)).resolve() != project.resolve():
        raise GuardError("GATE.json project_root does not match the guarded project")
    profile = gate.get("profile", "fast")
    if profile not in {"fast", "strict"}:
        raise GuardError("GATE.json profile must be fast or strict")
    gate["profile"] = profile
    gate.setdefault("max_recovery_attempts", 3)
    if isinstance(gate["max_recovery_attempts"], bool) or not isinstance(gate["max_recovery_attempts"], int) or not 0 <= gate["max_recovery_attempts"] <= 100:
        raise GuardError("GATE.json max_recovery_attempts must be an integer from 0 to 100")
    return gate


def gate_profile(gate: dict[str, Any]) -> str:
    profile = gate.get("profile", "fast")
    if profile not in {"fast", "strict"}:
        raise GuardError("gate profile must be fast or strict")
    return str(profile)


def default_control() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "controller": controller_metadata(),
        "active_lease": None,
        "chains": {},
        "poll": None,
        "last_checkpoint": None,
        "checkpoint_history": [],
        "continuation": None,
        "recovery": {"used": 0, "history": []},
        "consumed_output_roots": [],
        "recovery_path_usage": {},
        "review_epoch": 0,
        "runtime": {"state": "ACTIVE", "wait": None, "seen_events": []},
    }


def controller_metadata() -> dict[str, str]:
    manifest_path = CONTROLLER_PATH.parent.parent / ".codex-plugin/plugin.json"
    manifest = load_json(manifest_path)
    installed = manifest.get("version")
    if not isinstance(installed, str) or not installed:
        raise GuardError("plugin manifest version is missing")
    return {
        "schema_version": "goal-guardrails.controller-metadata/v1",
        "installed_plugin_version": installed,
        "current_hook_version": HOOK_VERSION,
    }


def migrate_control(control: dict[str, Any]) -> bool:
    expected = controller_metadata()
    changed = False
    if control.get("controller") != expected:
        control["controller"] = expected
        changed = True
    defaults = {
        "checkpoint_history": [],
        "continuation": None,
        "recovery": {"used": 0, "history": []},
        "consumed_output_roots": [],
        "recovery_path_usage": {},
    }
    for key, value in defaults.items():
        if key not in control:
            control[key] = value
            changed = True
    recovery = control.get("recovery")
    if not isinstance(recovery, dict):
        control["recovery"] = {"used": 0, "history": []}
        changed = True
    else:
        if "used" not in recovery:
            recovery["used"] = 0
            changed = True
        if "history" not in recovery:
            recovery["history"] = []
            changed = True
    return changed


def load_control(project: Path) -> dict[str, Any]:
    path = project / CONTROL_REL
    if not path.exists():
        return default_control()
    control = load_json(path)
    if control.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("unsupported CONTROL.json schema_version")
    if not isinstance(control.get("chains"), dict):
        raise GuardError("CONTROL.json chains must be an object")
    if not isinstance(control.get("runtime"), dict):
        control["runtime"] = {"state": "ACTIVE", "wait": None, "seen_events": []}
    control.setdefault("review_epoch", 0)
    if isinstance(control["review_epoch"], bool) or not isinstance(control["review_epoch"], int) or control["review_epoch"] < 0:
        raise GuardError("CONTROL.json review_epoch must be a nonnegative integer")
    runtime = control["runtime"]
    runtime.setdefault("state", "ACTIVE")
    runtime.setdefault("wait", None)
    runtime.setdefault("seen_events", [])
    if runtime["state"] not in {"ACTIVE", "ARMING_EXTERNAL_WAIT", "WAITING_EXTERNAL_EVENT"} or not isinstance(runtime["seen_events"], list):
        raise GuardError("CONTROL.json runtime state is invalid")
    migrated = migrate_control(control)
    recovery_persisted_control = recover_goal_update(project, control)
    if migrated and not recovery_persisted_control:
        save_control(project, control)
    return control


def save_control(project: Path, control: dict[str, Any]) -> None:
    atomic_json(project / CONTROL_REL, control)


def external_wait_in_progress(control: dict[str, Any]) -> bool:
    return control.get("runtime", {}).get("state") in {
        "ARMING_EXTERNAL_WAIT",
        "WAITING_EXTERNAL_EVENT",
    }


def persist_control_migration(project: Path, control: dict[str, Any]) -> bool:
    path = project / CONTROL_REL
    if path.exists() and load_json(path).get("controller") == control.get("controller"):
        return False
    save_control(project, control)
    return True


def recover_goal_update(project: Path, control: dict[str, Any]) -> bool:
    transaction_path = project / GOAL_UPDATE_REL
    if not transaction_path.exists():
        return False
    transaction = load_json(transaction_path)
    if transaction.get("schema_version") != 1:
        raise GuardError("unsupported goal update transaction schema")
    state = transaction.get("state")
    if state in {"COMMITTED", "ABORTED"}:
        return False
    if state != "PREPARED":
        raise GuardError("goal update transaction state is invalid")
    goal_path = project / GOAL_REL
    if goal_path.is_symlink() or not goal_path.is_file():
        raise GuardError("cannot recover goal update because GOAL.md is missing or unsafe")
    current_sha256 = file_hash(goal_path)
    old_sha256 = ensure_sha256(transaction.get("old_sha256"), "goal update old_sha256")
    new_sha256 = ensure_sha256(transaction.get("new_sha256"), "goal update new_sha256")
    if current_sha256 == old_sha256:
        transaction["state"] = "ABORTED"
        transaction["recovered_at"] = iso_time(utc_now())
        atomic_json(transaction_path, transaction)
        return False
    if current_sha256 != new_sha256:
        raise GuardError("GOAL.md matches neither side of the prepared controller transaction")
    metadata = transaction.get("goal_metadata")
    if not isinstance(metadata, dict) or metadata.get("sha256") != new_sha256:
        raise GuardError("prepared goal update metadata is invalid")
    control["goal"] = metadata
    # CONTROL is the durable roll-forward target. Keep the journal PREPARED
    # until this write succeeds so a crash can retry recovery idempotently.
    save_control(project, control)
    transaction["state"] = "COMMITTED"
    transaction["recovered_at"] = iso_time(utc_now())
    atomic_json(transaction_path, transaction)
    return True


def state_nonblank_lines(project: Path) -> int:
    path = project / STATE_REL
    if path.is_symlink() or not path.is_file():
        raise GuardError("optimization/STATE.md must be a regular file")
    return sum(bool(line.strip()) for line in path.read_text(encoding="utf-8").splitlines())


def ensure_text(value: Any, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "TODO" in value:
        raise GuardError(f"{name} must be a concrete non-TODO string of at most {maximum} characters")
    return value.strip()


def ensure_id(value: Any, name: str) -> str:
    text = ensure_text(value, name, maximum=80)
    if ID_RE.fullmatch(text) is None:
        raise GuardError(f"{name} has an invalid identifier")
    return text


def ensure_sha256(value: Any, name: str) -> str:
    text = ensure_text(value, name, maximum=64).casefold()
    if SHA256_RE.fullmatch(text) is None:
        raise GuardError(f"{name} must be a lowercase SHA-256 digest")
    return text


def ensure_prefixed_sha256(value: Any, name: str) -> str:
    text = ensure_text(value, name, maximum=71).casefold()
    if SHA256_PREFIX_RE.fullmatch(text) is None:
        raise GuardError(f"{name} must be a sha256:<lowercase digest> identifier")
    return text


def normalize_sha256(value: Any, name: str) -> str:
    text = ensure_text(value, name, maximum=71).casefold()
    if text.startswith("sha256:"):
        text = text.removeprefix("sha256:")
    if SHA256_RE.fullmatch(text) is None:
        raise GuardError(f"{name} must be a lowercase SHA-256 digest, with optional sha256: prefix")
    return text


def review_subject_sha256(
    project: Path,
    proposal: dict[str, Any],
    *,
    profile: str = "strict",
    review_epoch: int = 0,
) -> str:
    proposal_contract = dict(proposal)
    proposal_contract.pop("review", None)
    return "sha256:" + canonical_hash({
        "schema": "goal-guardrails.review-subject/v1",
        "goal_sha256": file_hash(project / GOAL_REL),
        "profile": profile,
        "review_epoch": review_epoch,
        "proposal": proposal_contract,
    })


def ensure_safe_token(value: Any, name: str, *, maximum: int = 240) -> str:
    text = ensure_text(value, name, maximum=maximum)
    if any(character in text for character in ("\x00", "\n", "\r")):
        raise GuardError(f"{name} contains control characters")
    return text


def is_protected_path(path: Path) -> bool:
    return path in PROTECTED_PATHS or path == CONTROLLER_STATE_REL or CONTROLLER_STATE_REL in path.parents


def normalize_external_root(raw: Any) -> str:
    text = ensure_safe_token(raw, "external monitor state_root", maximum=500)
    candidate = Path(os.path.abspath(os.path.expanduser(text)))
    if candidate == Path(candidate.anchor):
        raise GuardError("external monitor state_root cannot be a filesystem root")
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GuardError(f"external monitor state_root contains a symbolic link: {cursor}")
        if not cursor.exists():
            break
    return os.fspath(candidate)


def load_owned_external_json(path: Path, state_root: Path) -> dict[str, Any]:
    try:
        lexical_root = Path(os.path.abspath(os.fspath(state_root)))
        lexical_target = Path(os.path.abspath(os.fspath(path)))
        relative = lexical_target.relative_to(lexical_root)
        cursor = lexical_root
        if cursor.is_symlink():
            raise GuardError(f"external monitor state root became a symbolic link: {cursor}")
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise GuardError(f"external monitor artifact path contains a symbolic link: {cursor}")
        root = state_root.resolve(strict=True)
        target = path.resolve(strict=True)
        target.relative_to(root)
        info = path.lstat()
    except (FileNotFoundError, OSError, ValueError) as error:
        raise GuardError(f"external monitor artifact is missing or escapes its frozen state root: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise GuardError(f"external monitor artifact must be a regular non-symlink file: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise GuardError(f"external monitor artifact has an unexpected owner: {path}")
    if info.st_mode & 0o022:
        raise GuardError(f"external monitor artifact must not be group/world writable: {path}")
    return load_json(path)


def load_private_external_json(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except (FileNotFoundError, OSError) as error:
        raise GuardError(f"{label} is missing or unreadable: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise GuardError(f"{label} must be a regular non-symlink file: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise GuardError(f"{label} has an unexpected owner: {path}")
    if info.st_mode & 0o077:
        raise GuardError(f"{label} must not be group/world accessible: {path}")
    if info.st_size > 64 * 1024:
        raise GuardError(f"{label} is unexpectedly large: {path}")
    return load_json(path)


def monitor_canonical_digest(value: Any) -> str:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def normalize_relative(project: Path, raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise GuardError("allowed paths must be non-empty strings")
    candidate = Path(raw)
    absolute = candidate if candidate.is_absolute() else project / candidate
    normalized = Path(os.path.abspath(os.fspath(absolute)))
    try:
        relative = normalized.relative_to(project)
    except ValueError as error:
        raise GuardError(f"path escapes project: {raw}") from error
    if relative == Path(".") or ".." in relative.parts:
        raise GuardError(f"path is too broad or unsafe: {raw}")
    if is_protected_path(relative):
        raise GuardError(f"proposal cannot authorize protected path: {relative}")
    reject_symlink_escape(project, normalized)
    return relative.as_posix()


def normalize_project_path(project: Path, raw: str, *, allow_root: bool = False) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise GuardError("project paths must be non-empty strings")
    candidate = Path(raw)
    absolute = candidate if candidate.is_absolute() else project / candidate
    normalized = Path(os.path.abspath(os.fspath(absolute)))
    try:
        relative = normalized.relative_to(project)
    except ValueError as error:
        raise GuardError(f"path escapes project: {raw}") from error
    if relative == Path(".") and not allow_root:
        raise GuardError(f"path is too broad: {raw}")
    reject_symlink_escape(project, normalized)
    return relative.as_posix()


def normalize_cwd(project: Path, raw: str) -> str:
    relative = normalize_project_path(project, raw, allow_root=True)
    target = project if relative == "." else project / relative
    if not target.is_dir():
        raise GuardError(f"bash policy cwd must be an existing directory: {raw}")
    return relative


def reject_symlink_escape(project: Path, target: Path) -> None:
    project_real = project.resolve(strict=True)
    cursor = project
    try:
        relative = Path(os.path.abspath(os.fspath(target))).relative_to(project)
    except ValueError as error:
        raise GuardError(f"path escapes project: {target}") from error
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise GuardError(f"symbolic links are not allowed in admitted paths: {cursor}")
        if not cursor.exists():
            break
    existing = cursor
    while not existing.exists() and existing != project:
        existing = existing.parent
    try:
        existing.resolve(strict=True).relative_to(project_real)
    except ValueError as error:
        raise GuardError(f"resolved path escapes project: {target}") from error


def normalized_bottleneck(text: str) -> str:
    return " ".join(text.casefold().split())


def current_lease(control: dict[str, Any]) -> dict[str, Any] | None:
    lease = control.get("active_lease")
    if not isinstance(lease, dict):
        return None
    try:
        if lease.get("suspended_at") and int(lease.get("remaining_seconds", 0)) > 0:
            expired = False
        else:
            expired = parse_time(str(lease["expires_at"])) <= utc_now()
    except (KeyError, TypeError, ValueError):
        expired = True
    return None if expired else lease


def validate_review_attestation(
    review: Any,
    *,
    expected_subject_sha256: str,
    require_external_monitor: bool = False,
    require_preflight_failure: bool = False,
    require_remote_submission: bool = False,
) -> dict[str, Any]:
    if not isinstance(review, dict) or review.get("decision") != "ALLOW":
        raise GuardError("proposal requires an ALLOW review attestation")
    subject_sha256 = ensure_prefixed_sha256(review.get("subject_sha256"), "review.subject_sha256")
    if subject_sha256 != expected_subject_sha256:
        raise GuardError(
            "review subject does not match the current GOAL/proposal/review-epoch contract; "
            f"obtain a fresh review attestation for {expected_subject_sha256}"
        )
    reviewer = ensure_text(review.get("reviewer"), "review.reviewer", maximum=120)
    if not reviewer.startswith(("subagent:", "user:")):
        raise GuardError("review attestation must identify a fresh subagent or user")
    checks = review.get("checks")
    required_checks = {"evidence_sufficient", "lease_mutations_bounded", "pre_run_gates_sufficient", "mutation_not_required_before_admission"}
    if require_external_monitor:
        required_checks.add("external_monitor_contract_bounded")
    if require_preflight_failure:
        required_checks.add("preflight_failure_closure_reviewed")
    if require_remote_submission:
        required_checks.add("remote_submission_contract_bounded")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        raise GuardError(f"review attestation must affirm checks: {sorted(required_checks)}")
    return {
        "decision": "ALLOW",
        "subject_sha256": subject_sha256,
        "reviewer": reviewer,
        "reason": ensure_text(review.get("reason"), "review.reason"),
        "checks": {name: True for name in sorted(required_checks)},
    }


def automatic_fast_review_attestation(
    *,
    subject_sha256: str,
    require_external_monitor: bool = False,
    require_preflight_failure: bool = False,
    require_remote_submission: bool = False,
) -> dict[str, Any]:
    checks = {
        "evidence_sufficient",
        "lease_mutations_bounded",
        "pre_run_gates_sufficient",
        "mutation_not_required_before_admission",
    }
    if require_external_monitor:
        checks.add("external_monitor_contract_bounded")
    if require_preflight_failure:
        checks.add("preflight_failure_closure_reviewed")
    if require_remote_submission:
        checks.add("remote_submission_contract_bounded")
    return {
        "decision": "ALLOW",
        "subject_sha256": subject_sha256,
        "reviewer": "controller:fast",
        "reason": "fast profile uses deterministic proposal validation without an external admission review",
        "checks": {name: True for name in sorted(checks)},
    }


def mutation_allows(path: str, operation: str, mutations: list[dict[str, Any]]) -> bool:
    target = Path(path)
    for mutation in mutations:
        base = Path(mutation["path"])
        in_scope = target == base or (mutation["scope"] == "tree" and base in target.parents)
        if in_scope and operation in mutation["operations"]:
            return True
    return False


def validate_existing_evidence(project: Path, raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise GuardError("existing_evidence must be a list")
    evidence: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"existing_evidence[{index}] must be an object")
        evidence_id = ensure_id(item.get("id"), f"existing_evidence[{index}].id")
        if evidence_id in seen:
            raise GuardError(f"duplicate existing evidence id: {evidence_id}")
        seen.add(evidence_id)
        path = normalize_project_path(project, item.get("path"))
        target = project / path
        if not target.is_file() or target.is_symlink():
            raise GuardError(f"existing evidence must be a regular file: {path}")
        digest = ensure_sha256(item.get("sha256"), f"existing_evidence[{index}].sha256")
        if file_hash(target) != digest:
            raise GuardError(f"existing evidence SHA-256 mismatch: {path}")
        evidence.append({
            "id": evidence_id,
            "path": path,
            "sha256": digest,
            "claim": ensure_text(item.get("claim"), f"existing_evidence[{index}].claim"),
        })
    return evidence


def validate_lease_mutations(project: Path, raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise GuardError("lease_mutations must be a non-empty list")
    mutations: list[dict[str, Any]] = []
    allowed_operations = {"add", "update", "delete", "move"}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"lease_mutations[{index}] must be an object")
        path = normalize_relative(project, item.get("path"))
        scope = item.get("scope")
        if scope not in {"exact", "tree"}:
            raise GuardError(f"lease_mutations[{index}].scope must be exact or tree")
        raw_operations = item.get("operations")
        if not isinstance(raw_operations, list) or not raw_operations:
            raise GuardError(f"lease_mutations[{index}].operations must be a non-empty list")
        operations = sorted(set(ensure_text(value, f"lease_mutations[{index}].operation", maximum=12).casefold() for value in raw_operations))
        if any(value not in allowed_operations for value in operations):
            raise GuardError(f"lease_mutations[{index}] has unsupported operations")
        mutations.append({"path": path, "scope": scope, "operations": operations})
    canonical = {(item["path"], item["scope"], tuple(item["operations"])) for item in mutations}
    if len(canonical) != len(mutations):
        raise GuardError("lease_mutations contains duplicate contracts")
    return sorted(mutations, key=lambda item: (item["path"], item["scope"], item["operations"]))


def validate_checkpoint_artifacts(project: Path, raw: Any, mutations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise GuardError("checkpoint_artifacts must be a non-empty list")
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"checkpoint_artifacts[{index}] must be an object")
        artifact_id = ensure_id(item.get("id"), f"checkpoint_artifacts[{index}].id")
        if artifact_id in seen:
            raise GuardError(f"duplicate checkpoint artifact id: {artifact_id}")
        seen.add(artifact_id)
        path = normalize_project_path(project, item.get("path"))
        if is_protected_path(Path(path)) or Path(path) == RESULT_REL:
            raise GuardError(f"checkpoint artifact path is protected or recursive: {path}")
        if not (mutation_allows(path, "add", mutations) or mutation_allows(path, "update", mutations)):
            raise GuardError(f"checkpoint artifact is outside lease_mutations: {path}")
        artifacts.append({"id": artifact_id, "path": path, "required": item.get("required") is not False})
    return artifacts


def validate_pre_run_gates(raw: Any, artifact_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GuardError("pre_run_gates must be a list")
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"pre_run_gates[{index}] must be an object")
        gate_id = ensure_id(item.get("id"), f"pre_run_gates[{index}].id")
        if gate_id in seen:
            raise GuardError(f"duplicate pre-run gate id: {gate_id}")
        seen.add(gate_id)
        kind = item.get("kind")
        if kind not in {"resource", "command", "manual"}:
            raise GuardError(f"pre_run_gates[{index}].kind is unsupported")
        artifact_id = ensure_id(item.get("artifact_id"), f"pre_run_gates[{index}].artifact_id")
        if artifact_id not in artifact_ids:
            raise GuardError(f"pre-run gate references unknown checkpoint artifact: {artifact_id}")
        gate: dict[str, Any] = {
            "id": gate_id,
            "kind": kind,
            "description": ensure_text(item.get("description"), f"pre_run_gates[{index}].description"),
            "required": item.get("required") is not False,
            "artifact_id": artifact_id,
        }
        if kind == "resource":
            resource = item.get("resource")
            operator = item.get("operator")
            if resource != "gpu" or operator not in {"eq", "max"}:
                raise GuardError("resource pre-run gates currently support gpu with eq or max")
            value = int(item.get("value"))
            if not 0 <= value <= 64:
                raise GuardError("GPU gate value must be 0..64")
            gate.update({"resource": resource, "operator": operator, "value": value})
        gates.append(gate)
    return gates


def validate_runtime_bindings(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GuardError("runtime_bindings must be a list")
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"runtime_bindings[{index}] must be an object")
        binding_id = ensure_id(item.get("id"), f"runtime_bindings[{index}].id")
        if binding_id in seen:
            raise GuardError(f"duplicate runtime binding id: {binding_id}")
        seen.add(binding_id)
        kind = item.get("kind")
        if kind != "slurm_job_id":
            raise GuardError("runtime bindings currently support only slurm_job_id")
        bindings.append({
            "id": binding_id,
            "kind": kind,
            "source_policy_id": ensure_id(item.get("source_policy_id"), f"runtime_bindings[{index}].source_policy_id"),
            "required": item.get("required") is not False,
        })
    return sorted(bindings, key=lambda item: item["id"])


def validate_argv_template(raw: Any, fixed_args: Any, binding_ids: set[str], index: int) -> list[dict[str, str]]:
    if raw is not None and fixed_args is not None:
        raise GuardError(f"bash_policies[{index}] cannot declare both argv and fixed_args")
    if raw is None:
        if not isinstance(fixed_args, list):
            raise GuardError(f"bash_policies[{index}] requires argv or fixed_args")
        raw = [{"literal": value} for value in fixed_args]
    if not isinstance(raw, list):
        raise GuardError(f"bash_policies[{index}].argv must be a list")
    tokens: list[dict[str, str]] = []
    for token_index, token in enumerate(raw):
        if not isinstance(token, dict) or set(token) not in ({"literal"}, {"binding"}):
            raise GuardError(f"bash_policies[{index}].argv[{token_index}] must contain exactly literal or binding")
        if "literal" in token:
            value = ensure_safe_token(token["literal"], f"bash_policies[{index}].argv[{token_index}].literal")
            if re.search(r"[;&|`<>]", value):
                raise GuardError("Bash argv literals cannot contain shell control characters")
            tokens.append({"literal": value})
        else:
            binding_id = ensure_id(token["binding"], f"bash_policies[{index}].argv[{token_index}].binding")
            if binding_id not in binding_ids:
                raise GuardError(f"Bash argv references unknown runtime binding: {binding_id}")
            tokens.append({"binding": binding_id})
    return tokens


def validate_submission_transport(project: Path, raw: Any, *, capture_binding: str | None, index: int) -> dict[str, Any]:
    if raw is None:
        return {"kind": "local"}
    if capture_binding is None:
        raise GuardError("submission transport is allowed only on a binding-capture policy")
    if not isinstance(raw, dict) or raw.get("kind") != "ssh-helper-v1":
        raise GuardError("submission transport currently supports only ssh-helper-v1")
    ssh_executable = Path(ensure_text(raw.get("ssh_executable"), f"bash_policies[{index}].transport.ssh_executable", maximum=300))
    if not ssh_executable.is_absolute() or ssh_executable.name != "ssh" or ssh_executable.is_symlink() or not ssh_executable.is_file() or not os.access(ssh_executable, os.X_OK):
        raise GuardError("ssh transport requires an absolute regular executable named ssh")
    ssh_executable_sha256 = ensure_sha256(
        raw.get("ssh_executable_sha256"),
        f"bash_policies[{index}].transport.ssh_executable_sha256",
    )
    if file_hash(ssh_executable) != ssh_executable_sha256:
        raise GuardError("ssh transport executable SHA-256 mismatched")
    host = ensure_safe_token(raw.get("host"), f"bash_policies[{index}].transport.host", maximum=128)
    user = ensure_safe_token(raw.get("user"), f"bash_policies[{index}].transport.user", maximum=64)
    if HOST_RE.fullmatch(host) is None or REMOTE_USER_RE.fullmatch(user) is None:
        raise GuardError("ssh transport host or user has an invalid format")
    port = int(raw.get("port", 22))
    if not 1 <= port <= 65535:
        raise GuardError("ssh transport port must be 1..65535")
    known_hosts_file = Path(ensure_text(raw.get("known_hosts_file"), f"bash_policies[{index}].transport.known_hosts_file", maximum=500))
    if not known_hosts_file.is_absolute() or known_hosts_file.is_symlink() or not known_hosts_file.is_file():
        raise GuardError("ssh transport known_hosts_file must be an absolute regular file")
    known_hosts_sha256 = ensure_sha256(raw.get("known_hosts_sha256"), f"bash_policies[{index}].transport.known_hosts_sha256")
    if file_hash(known_hosts_file) != known_hosts_sha256:
        raise GuardError("ssh transport known_hosts_file SHA-256 mismatched")
    identity_path = Path(ensure_text(raw.get("identity_file"), f"bash_policies[{index}].transport.identity_file", maximum=500))
    if not identity_path.is_absolute() or identity_path.is_symlink() or not identity_path.is_file():
        raise GuardError("ssh transport identity_file must be an absolute regular file")
    identity_file_sha256 = ensure_sha256(
        raw.get("identity_file_sha256"),
        f"bash_policies[{index}].transport.identity_file_sha256",
    )
    if file_hash(identity_path) != identity_file_sha256:
        raise GuardError("ssh transport identity_file SHA-256 mismatched")
    identity_file = os.fspath(identity_path)
    helper_path = ensure_text(raw.get("helper_path"), f"bash_policies[{index}].transport.helper_path", maximum=500)
    sbatch_path = ensure_text(raw.get("sbatch_path"), f"bash_policies[{index}].transport.sbatch_path", maximum=500)
    remote_workdir = ensure_text(raw.get("remote_workdir"), f"bash_policies[{index}].transport.remote_workdir", maximum=500)
    receipt_root = ensure_text(raw.get("receipt_root"), f"bash_policies[{index}].transport.receipt_root", maximum=500)
    for field, value in (("helper_path", helper_path), ("sbatch_path", sbatch_path), ("remote_workdir", remote_workdir), ("receipt_root", receipt_root)):
        if REMOTE_SHELL_PATH_RE.fullmatch(value) is None:
            raise GuardError(f"ssh transport {field} must be a safe absolute path")
    if Path(sbatch_path).name != "sbatch":
        raise GuardError("ssh transport sbatch_path basename must be sbatch")
    helper_sha256 = ensure_sha256(raw.get("helper_sha256"), f"bash_policies[{index}].transport.helper_sha256")
    if not REMOTE_HELPER_PATH.is_file() or file_hash(REMOTE_HELPER_PATH) != helper_sha256:
        raise GuardError("ssh transport helper_sha256 must match the bundled remote helper")
    raw_files = raw.get("remote_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise GuardError("ssh transport remote_files must be a non-empty list")
    remote_files: list[dict[str, str]] = []
    seen: set[str] = set()
    for file_index, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise GuardError(f"transport.remote_files[{file_index}] must be an object")
        path = normalize_project_path(project, item.get("path"))
        if path in seen:
            raise GuardError(f"duplicate ssh transport remote file: {path}")
        seen.add(path)
        digest = ensure_sha256(item.get("sha256"), f"transport.remote_files[{file_index}].sha256")
        local = project / path
        if local.is_symlink() or not local.is_file() or file_hash(local) != digest:
            raise GuardError(f"ssh transport local/remote file contract is missing or drifted: {path}")
        remote_files.append({"path": path, "sha256": digest})
    timeout_seconds = int(raw.get("timeout_seconds", 30))
    if not 1 <= timeout_seconds <= 300:
        raise GuardError("ssh transport timeout_seconds must be 1..300")
    return {
        "kind": "ssh-helper-v1",
        "ssh_executable": os.fspath(ssh_executable),
        "ssh_executable_sha256": ssh_executable_sha256,
        "host": host,
        "user": user,
        "port": port,
        "known_hosts_file": os.fspath(known_hosts_file),
        "known_hosts_sha256": known_hosts_sha256,
        "identity_file": identity_file,
        "identity_file_sha256": identity_file_sha256,
        "helper_path": helper_path,
        "helper_sha256": helper_sha256,
        "sbatch_path": sbatch_path,
        "remote_workdir": remote_workdir,
        "receipt_root": receipt_root,
        "remote_files": sorted(remote_files, key=lambda item: item["path"]),
        "timeout_seconds": timeout_seconds,
    }


def validate_bash_policies(
    project: Path,
    raw: Any,
    mutations: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise GuardError("bash_policies must be a non-empty list")
    gpu_limits = [int(gate["value"]) for gate in gates if gate["required"] and gate["kind"] == "resource" and gate["resource"] == "gpu"]
    gpu_limit = min(gpu_limits) if gpu_limits else None
    policies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"bash_policies[{index}] must be an object")
        policy_id = ensure_id(item.get("id"), f"bash_policies[{index}].id")
        if policy_id in seen:
            raise GuardError(f"duplicate Bash policy id: {policy_id}")
        seen.add(policy_id)
        executable = ensure_text(item.get("executable"), f"bash_policies[{index}].executable", maximum=160)
        if re.search(r"\s|[;&|`<>]", executable) or Path(executable).name in {"sh", "bash", "zsh", "csh", "fish", "env"}:
            raise GuardError("Bash policy executable must be one exact non-shell executable token")
        argv = validate_argv_template(item.get("argv"), item.get("fixed_args"), {binding["id"] for binding in bindings}, index)
        phase = item.get("phase")
        if phase not in {"preparation", "workload", "postflight", "evaluation"}:
            raise GuardError(f"bash_policies[{index}].phase is unsupported")
        cwd = normalize_cwd(project, item.get("cwd"))
        raw_outputs = item.get("output_paths")
        if not isinstance(raw_outputs, list):
            raise GuardError(f"bash_policies[{index}].output_paths must be a list")
        outputs = sorted(set(normalize_project_path(project, value) for value in raw_outputs))
        for output in outputs:
            if not (mutation_allows(output, "add", mutations) or mutation_allows(output, "update", mutations)):
                raise GuardError(f"Bash output path is outside lease_mutations: {output}")
            cwd_path = project if cwd == "." else project / cwd
            output_argument = os.path.relpath(project / output, cwd_path)
            literals = [token["literal"] for token in argv if "literal" in token]
            if not any(argument == output_argument or argument.endswith("=" + output_argument) for argument in literals):
                raise GuardError(f"Bash output path must be frozen as a literal argv token: {output}")
        resources = item.get("resources")
        if not isinstance(resources, dict):
            raise GuardError(f"bash_policies[{index}].resources must be an object")
        gpu = int(resources.get("gpu", 0))
        if not 0 <= gpu <= 64:
            raise GuardError("Bash policy GPU count must be 0..64")
        command_text = " ".join([executable, *(token.get("literal", "") for token in argv)])
        if gpu_limit is not None and (gpu > gpu_limit or (gpu_limit == 0 and GPU_COMMAND_RE.search(command_text))):
            raise GuardError(f"Bash policy {policy_id} conflicts with the pre-run GPU gate")
        capture_binding = item.get("capture_binding")
        if capture_binding is not None:
            capture_binding = ensure_id(capture_binding, f"bash_policies[{index}].capture_binding")
            if capture_binding not in {binding["id"] for binding in bindings}:
                raise GuardError(f"Bash policy captures unknown runtime binding: {capture_binding}")
            if any("binding" in token for token in argv):
                raise GuardError("a binding-capture policy cannot depend on runtime bindings")
        max_uses = item.get("max_uses")
        if max_uses is not None:
            max_uses = int(max_uses)
            if not 1 <= max_uses <= 100:
                raise GuardError("Bash policy max_uses must be 1..100")
        if capture_binding is not None and max_uses != 1:
            raise GuardError("a binding-capture policy must declare max_uses=1")
        transport = validate_submission_transport(project, item.get("transport"), capture_binding=capture_binding, index=index)
        if transport["kind"] == "ssh-helper-v1" and executable != transport["sbatch_path"]:
            raise GuardError("remote submission policy executable must equal transport.sbatch_path")
        timeout_seconds = int(item.get("timeout_seconds", 120))
        if not 1 <= timeout_seconds <= 1800:
            raise GuardError("Bash policy timeout_seconds must be 1..1800")
        policies.append({
            "id": policy_id,
            "phase": phase,
            "executable": executable,
            "argv": argv,
            "cwd": cwd,
            "output_paths": outputs,
            "resources": {"gpu": gpu},
            "capture_binding": capture_binding,
            "transport": transport,
            "max_uses": max_uses,
            "timeout_seconds": timeout_seconds,
        })
    return sorted(policies, key=lambda item: item["id"])


def validate_external_monitors(
    project: Path,
    raw: Any,
    bindings: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GuardError("external_monitors must be a list")
    binding_map = {item["id"]: item for item in bindings}
    policy_map = {item["id"]: item for item in policies}
    monitors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"external_monitors[{index}] must be an object")
        monitor_id = ensure_id(item.get("id"), f"external_monitors[{index}].id")
        if monitor_id in seen:
            raise GuardError(f"duplicate external monitor id: {monitor_id}")
        seen.add(monitor_id)
        contract_version = item.get("contract_version")
        if item.get("provider") != "codex-hpc-monitor" or contract_version not in {1, 2}:
            raise GuardError("external monitors currently support codex-hpc-monitor contract_version 1 or 2")
        binding_id = ensure_id(item.get("binding_id"), f"external_monitors[{index}].binding_id")
        binding = binding_map.get(binding_id)
        if binding is None or binding.get("kind") != "slurm_job_id":
            raise GuardError("codex-hpc-monitor requires a slurm_job_id runtime binding")
        start_policy_id = ensure_id(item.get("start_policy_id"), f"external_monitors[{index}].start_policy_id")
        start_policy = policy_map.get(start_policy_id)
        if start_policy is None or not any(token.get("binding") == binding_id for token in start_policy["argv"]):
            raise GuardError("external monitor start policy must consume its runtime binding")
        if start_policy.get("capture_binding") is not None:
            raise GuardError("external monitor start policy cannot also capture a binding")
        host = ensure_safe_token(item.get("host"), f"external_monitors[{index}].host", maximum=128)
        if HOST_RE.fullmatch(host) is None:
            raise GuardError("external monitor host has an invalid format")
        state_root = normalize_external_root(item.get("state_root"))
        expected_owner = ensure_safe_token(item.get("expected_owner"), f"external_monitors[{index}].expected_owner")
        expected_job_name = ensure_safe_token(item.get("expected_job_name"), f"external_monitors[{index}].expected_job_name")
        expected_partition = ensure_safe_token(item.get("expected_partition"), f"external_monitors[{index}].expected_partition")
        template = start_policy["argv"]
        literal_values = [token.get("literal") for token in template]
        if "start" not in literal_values:
            raise GuardError("external monitor start policy must invoke start")
        required_pairs = {
            "--host": host,
            "--state-dir": state_root,
            "--expected-owner": expected_owner,
            "--expected-job-name": expected_job_name,
            "--expected-partition": expected_partition,
        }
        for option, expected in required_pairs.items():
            if not any(
                token.get("literal") == option and next_token.get("literal") == expected
                for token, next_token in zip(template, template[1:])
            ):
                raise GuardError(f"external monitor start policy must freeze {option} {expected}")
        for option in ("--event-binding", "--bridge-config"):
            values = [
                next_token.get("literal")
                for token, next_token in zip(template, template[1:])
                if token.get("literal") == option
            ]
            if len(values) != 1 or not isinstance(values[0], str) or not Path(values[0]).is_absolute():
                raise GuardError(f"external monitor start policy must freeze one absolute {option} path")
            ensure_safe_token(values[0], f"external monitor {option} path", maximum=500)
        service_names = [
            next_token.get("literal")
            for token, next_token in zip(template, template[1:])
            if token.get("literal") == "--bridge-service-name"
        ]
        if len(service_names) != 1 or not isinstance(service_names[0], str):
            raise GuardError("external monitor start policy must freeze one --bridge-service-name")
        ensure_safe_token(service_names[0], "external monitor bridge service name", maximum=128)
        if not any(token.get("literal") == "--require-auto-resume" for token in template):
            raise GuardError("external monitor start policy must freeze --require-auto-resume")
        if contract_version == 2:
            script_tokens = [token.get("literal") for token in template if isinstance(token.get("literal"), str)]
            if not script_tokens:
                raise GuardError("contract_version 2 monitor policy must freeze supervise_slurm_job.py")
            policy_cwd = project if start_policy.get("cwd") == "." else project / str(start_policy.get("cwd"))
            monitor_script = Path(script_tokens[0])
            if not monitor_script.is_absolute():
                monitor_script = policy_cwd / monitor_script
            monitor_script = Path(os.path.abspath(os.fspath(monitor_script)))
            bridge_script = monitor_script.with_name("app_server_bridge.py")
            if (
                monitor_script.name != "supervise_slurm_job.py"
                or not monitor_script.is_file()
                or monitor_script.is_symlink()
                or not bridge_script.is_file()
                or bridge_script.is_symlink()
            ):
                raise GuardError(
                    "contract_version 2 requires regular supervise_slurm_job.py and app_server_bridge.py sibling files"
                )
        monitors.append({
            "id": monitor_id,
            "provider": "codex-hpc-monitor",
            "contract_version": contract_version,
            "binding_id": binding_id,
            "start_policy_id": start_policy_id,
            "state_root": state_root,
            "host": host,
            "expected_owner": expected_owner,
            "expected_job_name": expected_job_name,
            "expected_partition": expected_partition,
            "required": item.get("required") is not False,
        })
    capture_pairs = [(policy["capture_binding"], policy["id"]) for policy in policies if policy.get("capture_binding")]
    if len({binding_id for binding_id, _policy_id in capture_pairs}) != len(capture_pairs):
        raise GuardError("each runtime binding must have exactly one capture policy")
    capture_sources = dict(capture_pairs)
    for binding in bindings:
        if capture_sources.get(binding["id"]) != binding["source_policy_id"]:
            raise GuardError(f"runtime binding {binding['id']} must have exactly its declared capture policy")
        source_policy = policy_map.get(binding["source_policy_id"])
        if not isinstance(source_policy, dict):
            raise GuardError(f"runtime binding {binding['id']} source policy is missing")
        if binding["kind"] == "slurm_job_id":
            parsable_count = sum(token.get("literal") == "--parsable" for token in source_policy["argv"])
            if Path(source_policy["executable"]).name != "sbatch" or parsable_count != 1:
                raise GuardError("slurm_job_id capture policy must execute sbatch with exactly one literal --parsable")
    return sorted(monitors, key=lambda item: item["id"])


def validate_evaluation_contract(raw: Any, artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GuardError("schema-v3 proposals require evaluation_contract")
    expected_attempts = raw.get("expected_attempts")
    minimum_result_rows = raw.get("minimum_result_rows")
    if isinstance(expected_attempts, bool) or not isinstance(expected_attempts, int) or expected_attempts < 1:
        raise GuardError("evaluation_contract.expected_attempts must be a positive integer")
    if isinstance(minimum_result_rows, bool) or not isinstance(minimum_result_rows, int) or minimum_result_rows < 1:
        raise GuardError("evaluation_contract.minimum_result_rows must be a positive integer")
    artifact_ids = {item["id"] for item in artifacts}
    evaluator_artifact_id = ensure_id(raw.get("evaluator_artifact_id"), "evaluation_contract.evaluator_artifact_id")
    if evaluator_artifact_id not in artifact_ids:
        raise GuardError("evaluation_contract.evaluator_artifact_id must name a checkpoint artifact")
    required_raw = raw.get("required_artifact_ids", [evaluator_artifact_id])
    if not isinstance(required_raw, list) or not required_raw:
        raise GuardError("evaluation_contract.required_artifact_ids must be a non-empty list")
    required_artifact_ids = sorted({ensure_id(value, "evaluation_contract.required_artifact_ids[]") for value in required_raw})
    if not set(required_artifact_ids).issubset(artifact_ids):
        raise GuardError("evaluation_contract.required_artifact_ids contains an unknown checkpoint artifact")
    return {
        "expected_attempts": expected_attempts,
        "minimum_result_rows": minimum_result_rows,
        "evaluator_artifact_id": evaluator_artifact_id,
        "required_artifact_ids": required_artifact_ids,
    }


def validate_recovery_paths(project: Path, raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GuardError("recovery_paths must be a list")
    paths: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"recovery_paths[{index}] must be an object")
        path_id = ensure_id(item.get("id"), f"recovery_paths[{index}].id")
        if path_id in seen:
            raise GuardError(f"duplicate recovery path id: {path_id}")
        seen.add(path_id)
        kind = item.get("kind")
        if kind not in RECOVERY_KINDS:
            raise GuardError(f"recovery_paths[{index}].kind must be recovery, rollback, or switch")
        description = ensure_text(item.get("description"), f"recovery_paths[{index}].description")
        maximum = item.get("max_attempts", 1)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100:
            raise GuardError(f"recovery_paths[{index}].max_attempts must be 1..100")
        root = normalize_project_path(project, item.get("write_once_output_root"))
        paths.append({
            "id": path_id,
            "kind": kind,
            "description": description,
            "max_attempts": maximum,
            "write_once_output_root": root,
        })
    return sorted(paths, key=lambda item: item["id"])


def project_paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return left_path == right_path or left_path in right_path.parents or right_path in left_path.parents


def fixed_outcome_mapping() -> dict[str, dict[str, Any]]:
    """Controller-owned mapping; projects provide facts, not a self-serving verdict table."""
    return {
        "evaluator_unavailable": {"valid": False, "evaluation_integrity": "FAIL", "outcome": "invalid", "decisions": ["PAUSE_REQUIRED"]},
        "threshold_failed": {"valid": True, "evaluation_integrity": "PASS", "outcome": "negative", "decisions": ["ROLLBACK", "SWITCH"]},
        "guardrail_failed": {"valid": True, "evaluation_integrity": "PASS", "outcome": "negative", "decisions": ["ROLLBACK"]},
        "all_passed": {"valid": True, "evaluation_integrity": "PASS", "outcome": "positive", "decisions": ["CONTINUE", "REPLICATE", "COMPLETE", "SWITCH"]},
    }


def validate_proposal(project: Path, gate: dict[str, Any], control: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    proposal_schema_version = proposal.get("schema_version")
    if proposal_schema_version not in SUPPORTED_PROPOSAL_SCHEMA_VERSIONS:
        raise GuardError(f"proposal schema_version must be one of {sorted(SUPPORTED_PROPOSAL_SCHEMA_VERSIONS)}")
    current_continuation = control.get("continuation")
    if (
        proposal_schema_version != 3
        and isinstance(current_continuation, dict)
        and current_continuation.get("state") == "AVAILABLE"
        and current_continuation.get("proposal_schema_version") == 3
    ):
        raise GuardError("an available schema-v3 continuation requires a schema-v3 bound successor")
    if isinstance(control.get("active_lease"), dict):
        raise GuardError("an experiment lease is still active or awaiting checkpoint")
    if gate_profile(gate) == "strict" and state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
        raise GuardError("STATE.md exceeds its nonblank-line cap; compact it before admission")
    if not (project / GOAL_REL).is_file():
        raise GuardError("optimization/GOAL.md is missing")

    experiment_id = ensure_id(proposal.get("experiment_id"), "experiment_id")
    chain_id = ensure_id(proposal.get("chain_id"), "chain_id")
    kind = proposal.get("chain_kind")
    if kind not in CHAIN_KINDS:
        raise GuardError(f"chain_kind must be one of {sorted(CHAIN_KINDS)}")
    bottleneck = ensure_text(proposal.get("causal_bottleneck"), "causal_bottleneck")
    hypothesis = ensure_text(proposal.get("hypothesis"), "hypothesis")
    core_progress = ensure_text(proposal.get("core_progress_expected"), "core_progress_expected")
    lease_phase = proposal.get("lease_phase")
    if lease_phase not in {"preparation", "workload", "postflight", "synchronous"}:
        raise GuardError("lease_phase must be preparation, workload, postflight, or synchronous")
    existing_evidence = validate_existing_evidence(project, proposal.get("existing_evidence"))
    lease_mutations = validate_lease_mutations(project, proposal.get("lease_mutations"))
    for evidence in existing_evidence:
        if any(mutation_allows(evidence["path"], operation, lease_mutations) for operation in ("add", "update", "delete", "move")):
            raise GuardError(f"existing evidence cannot also be mutable under the lease: {evidence['path']}")
    checkpoint_artifacts = validate_checkpoint_artifacts(project, proposal.get("checkpoint_artifacts"), lease_mutations)
    evaluation_contract = (
        validate_evaluation_contract(proposal.get("evaluation_contract"), checkpoint_artifacts)
        if proposal_schema_version == 3 else None
    )
    recovery_paths = validate_recovery_paths(project, proposal.get("recovery_paths"))
    write_once_output_root = None
    if proposal_schema_version == 3:
        raw_output_root = proposal.get("write_once_output_root")
        if raw_output_root is None:
            evaluator_id = evaluation_contract["evaluator_artifact_id"]
            write_once_output_root = next(item["path"] for item in checkpoint_artifacts if item["id"] == evaluator_id)
        else:
            write_once_output_root = normalize_project_path(project, raw_output_root)
        if any(project_paths_overlap(write_once_output_root, str(root)) for root in control.get("consumed_output_roots", [])):
            raise GuardError("schema-v3 successor must use a fresh write_once_output_root")
        if any(project_paths_overlap(str(item.get("write_once_output_root")), write_once_output_root) for item in recovery_paths):
            raise GuardError("a successor recovery path must use a different write-once output root")
        continuation = control.get("continuation")
        if isinstance(continuation, dict) and continuation.get("state") == "AVAILABLE":
            candidate_paths = [item for item in continuation.get("paths", []) if isinstance(item, dict)]
            matching_paths = [
                item for item in candidate_paths
                if not isinstance(item.get("write_once_output_root"), str)
                or write_once_output_root == item["write_once_output_root"]
                or Path(item["write_once_output_root"]) in Path(write_once_output_root).parents
            ]
            if candidate_paths and not matching_paths:
                raise GuardError("successor write_once_output_root is outside every frozen continuation path")
            continuation_binding = {
                "source_checkpoint_id": continuation.get("source_checkpoint_id"),
                "path_id": matching_paths[0].get("id") if matching_paths else None,
                "usage_key": matching_paths[0].get("usage_key") if matching_paths else None,
            }
        else:
            continuation_binding = None
    pre_run_gates = validate_pre_run_gates(proposal.get("pre_run_gates"), {item["id"] for item in checkpoint_artifacts})
    runtime_bindings = validate_runtime_bindings(proposal.get("runtime_bindings"))
    bash_policies = validate_bash_policies(project, proposal.get("bash_policies"), lease_mutations, pre_run_gates, runtime_bindings)
    external_monitors = validate_external_monitors(project, proposal.get("external_monitors"), runtime_bindings, bash_policies)
    review_epoch = int(control.get("review_epoch", 0))
    if review_epoch < 0:
        raise GuardError("CONTROL.json review_epoch is invalid")
    subject_sha256 = review_subject_sha256(
        project, proposal, profile=gate_profile(gate), review_epoch=review_epoch
    )
    review_requirements = {
        "require_external_monitor": bool(external_monitors),
        "require_preflight_failure": any(item["required"] for item in pre_run_gates),
        "require_remote_submission": any(
            policy.get("transport", {}).get("kind") == "ssh-helper-v1" for policy in bash_policies
        ),
    }
    if gate_profile(gate) == "fast":
        reviewer = automatic_fast_review_attestation(subject_sha256=subject_sha256, **review_requirements)
    else:
        reviewer = validate_review_attestation(
            proposal.get("review"), expected_subject_sha256=subject_sha256, **review_requirements
        )
    parent = proposal.get("parent_chain")
    if parent is not None:
        parent = ensure_id(parent, "parent_chain")

    chains = control["chains"]
    chain = chains.get(chain_id)
    same_bottleneck = [
        other_id for other_id, other in chains.items()
        if other_id != chain_id and normalized_bottleneck(str(other.get("causal_bottleneck", ""))) == normalized_bottleneck(bottleneck)
    ]
    if chain is None and same_bottleneck and kind != "verification":
        raise GuardError(f"same causal bottleneck already exists under chain(s) {same_bottleneck}; renaming cannot reset it")
    if kind == "verification":
        if parent is None or parent not in chains or not chains[parent].get("closed"):
            raise GuardError("verification chain requires a closed parent_chain")
        if chains[parent].get("close_outcome") != "positive":
            raise GuardError("verification chain requires a positively closed parent")
    elif chain is None and parent is not None and parent not in chains:
        raise GuardError("parent_chain does not exist")

    if chain is not None:
        if chain.get("closed"):
            raise GuardError("chain is closed")
        if normalized_bottleneck(str(chain.get("causal_bottleneck"))) != normalized_bottleneck(bottleneck):
            raise GuardError("existing chain_id cannot change its causal bottleneck")

    final_discriminator = bool(proposal.get("final_discriminator", False))
    if kind == "verification" and final_discriminator:
        raise GuardError("verification chains cannot declare another final discriminator")
    if chain and chain.get("stopline_fired") and not final_discriminator:
        raise GuardError("chain stop line fired; only one declared final discriminator may proceed")
    if chain and chain.get("final_discriminator_used"):
        raise GuardError("this chain already consumed its final discriminator")
    next_paths = proposal.get("next_paths")
    if final_discriminator:
        if not isinstance(next_paths, dict) or not {"positive", "other"}.issubset(next_paths):
            raise GuardError("final discriminator requires named positive and other next_paths")
        ensure_text(next_paths["positive"], "next_paths.positive")
        ensure_text(next_paths["other"], "next_paths.other")

    raw_tools = proposal.get("allowed_tool_names", [])
    if not isinstance(raw_tools, list):
        raise GuardError("allowed_tool_names must be a list")
    if raw_tools:
        raise GuardError("mutating MCP tools are not admissible until a parameter-level scope adapter exists")

    minutes = int(proposal.get("expires_minutes", gate["default_lease_minutes"]))
    mutations = int(proposal.get("max_mutations", gate["default_max_mutations"]))
    if not 1 <= minutes <= MAX_LEASE_MINUTES or not 1 <= mutations <= 50:
        raise GuardError(f"lease minutes must be 1..{MAX_LEASE_MINUTES} and mutations must be 1..50")
    required_binding_ids = {binding["id"] for binding in runtime_bindings if binding.get("required")}
    minimum_controller_mutations = sum(1 for policy in bash_policies if policy.get("capture_binding") in required_binding_ids)
    if mutations < minimum_controller_mutations:
        raise GuardError(
            f"mutation budget cannot cover required one-shot submissions: required={minimum_controller_mutations}, max_mutations={mutations}"
        )
    work_class = proposal.get("work_class")
    if work_class not in {"core", "non_core"}:
        raise GuardError("work_class must be core or non_core")
    cost_units = int(proposal.get("cost_units", 1))
    if not 1 <= cost_units <= 100:
        raise GuardError("cost_units must be 1..100")
    prior_non_core = int((chain or {}).get("non_core_cost_units", 0))
    if work_class == "non_core" and prior_non_core + cost_units > int(gate["max_non_core_cost_units_per_chain"]):
        raise GuardError("non-core allowance for this chain is exhausted")

    now = utc_now()
    return {
        "schema_version": 1,
        "lease_id": uuid.uuid4().hex,
        "experiment_id": experiment_id,
        "chain_id": chain_id,
        "chain_kind": kind,
        "parent_chain": parent,
        "causal_bottleneck": bottleneck,
        "hypothesis": hypothesis,
        "core_progress_expected": core_progress,
        "proposal_schema_version": proposal_schema_version,
        "lease_phase": lease_phase,
        "existing_evidence": existing_evidence,
        "lease_mutations": lease_mutations,
        "checkpoint_artifacts": checkpoint_artifacts,
        "evaluation_contract": evaluation_contract,
        "outcome_mapping": fixed_outcome_mapping() if proposal_schema_version == 3 else None,
        "recovery_paths": recovery_paths,
        "write_once_output_root": write_once_output_root,
        "continuation_binding": continuation_binding if proposal_schema_version == 3 else None,
        "pre_run_gates": pre_run_gates,
        "pre_run_gate_results": None,
        "bash_policies": bash_policies,
        "runtime_bindings": runtime_bindings,
        "binding_values": {},
        "policy_runs": {},
        "transport_doctors": {},
        "external_monitors": external_monitors,
        "monitor_receipts": {},
        "allowed_tool_names": [],
        "issued_at": iso_time(now),
        "expires_at": iso_time(now + timedelta(minutes=minutes)),
        "max_mutations": mutations,
        "mutations_used": 0,
        "budget_plan": {
            "required_one_shot_submissions": minimum_controller_mutations,
            "submit_bind_cost_each": 1,
            "doctor_cost": 0,
            "reconcile_cost": 0,
            "wait_wake_receipt_cost": 0,
        },
        "finalization_used": False,
        "work_class": work_class,
        "cost_units": cost_units,
        "final_discriminator": final_discriminator,
        "next_paths": next_paths,
        "review": reviewer,
        "review_subject_sha256": subject_sha256,
        "review_epoch": review_epoch,
        "goal_sha256": file_hash(project / GOAL_REL),
        "proposal_sha256": canonical_hash(proposal),
        "proposal_file_sha256": file_hash(project / PROPOSAL_REL),
    }


def command_admit(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    proposal_path = Path(args.proposal).resolve()
    if proposal_path != (project / PROPOSAL_REL).resolve():
        raise GuardError("admission must use optimization/PROPOSAL.json from the guarded project")
    proposal = load_json(proposal_path)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control["runtime"]["state"] != "ACTIVE":
            raise GuardError("cannot admit while runtime is waiting for an external event")
        lease = validate_proposal(project, gate, control, proposal)
        chain = control["chains"].setdefault(lease["chain_id"], {
            "chain_kind": lease["chain_kind"], "parent_chain": lease["parent_chain"],
            "causal_bottleneck": lease["causal_bottleneck"], "no_progress_count": 0,
            "non_core_cost_units": 0, "stopline_fired": False,
            "final_discriminator_used": False, "closed": False, "close_outcome": None,
        })
        if lease["final_discriminator"]:
            chain["final_discriminator_used"] = True
        if lease["work_class"] == "non_core":
            chain["non_core_cost_units"] = int(chain.get("non_core_cost_units", 0)) + lease["cost_units"]
        continuation = control.get("continuation")
        if isinstance(continuation, dict) and continuation.get("state") == "AVAILABLE":
            binding = lease.get("continuation_binding")
            if lease.get("proposal_schema_version") == 3:
                if not isinstance(binding, dict) or binding.get("source_checkpoint_id") != continuation.get("source_checkpoint_id"):
                    raise GuardError("schema-v3 successor is not bound to the current continuation checkpoint")
                matching = next(
                    (item for item in continuation.get("paths", []) if isinstance(item, dict) and item.get("id") == binding.get("path_id")),
                    None,
                )
                if not isinstance(matching, dict):
                    raise GuardError("schema-v3 successor continuation path is missing or stale")
                usage_key = matching.get("usage_key")
                if isinstance(usage_key, str):
                    usage = control.setdefault("recovery_path_usage", {})
                    usage[usage_key] = int(usage.get(usage_key, 0)) + 1
            continuation["state"] = "CONSUMED"
            continuation["consumed_by_experiment_id"] = lease["experiment_id"]
            continuation["consumed_path_id"] = binding.get("path_id") if isinstance(binding, dict) else None
            continuation["consumed_at"] = iso_time(utc_now())
            control["continuation"] = continuation
        control["active_lease"] = lease
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(lease, ensure_ascii=False, indent=2))
    return 0


def verify_artifact_results(project: Path, lease: dict[str, Any], raw: Any, *, require_all: bool) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise GuardError("artifact_results must be a list")
    contracts = {item["id"]: item for item in lease.get("checkpoint_artifacts", [])}
    verified: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"artifact_results[{index}] must be an object")
        artifact_id = ensure_id(item.get("id"), f"artifact_results[{index}].id")
        if artifact_id in seen or artifact_id not in contracts:
            raise GuardError(f"duplicate or unregistered checkpoint artifact: {artifact_id}")
        seen.add(artifact_id)
        path = normalize_project_path(project, item.get("path"))
        if path != contracts[artifact_id]["path"]:
            raise GuardError(f"checkpoint artifact path drifted for {artifact_id}")
        digest = ensure_sha256(item.get("sha256"), f"artifact_results[{index}].sha256")
        target = project / path
        if target.is_symlink() or not target.is_file() or file_hash(target) != digest:
            raise GuardError(f"checkpoint artifact missing, unsafe, or SHA-256 mismatched: {path}")
        verified.append({"id": artifact_id, "path": path, "sha256": digest})
    if require_all:
        missing = sorted(item["id"] for item in contracts.values() if item["required"] and item["id"] not in seen)
        if missing:
            raise GuardError(f"required checkpoint artifacts are missing: {missing}")
    return sorted(verified, key=lambda item: item["id"])


def verify_gate_results(
    lease: dict[str, Any],
    raw: Any,
    artifacts: list[dict[str, str]],
    *,
    allow_required_fail: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise GuardError("pre_run_gate_results must be a list")
    contracts = {item["id"]: item for item in lease.get("pre_run_gates", [])}
    artifact_ids = {item["id"] for item in artifacts}
    verified: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"pre_run_gate_results[{index}] must be an object")
        gate_id = ensure_id(item.get("id"), f"pre_run_gate_results[{index}].id")
        if gate_id in seen or gate_id not in contracts:
            raise GuardError(f"duplicate or unregistered pre-run gate result: {gate_id}")
        seen.add(gate_id)
        status = item.get("status")
        if status not in {"PASS", "FAIL"}:
            raise GuardError(f"pre-run gate {gate_id} status must be PASS or FAIL")
        artifact_id = ensure_id(item.get("artifact_id"), f"pre_run_gate_results[{index}].artifact_id")
        if artifact_id != contracts[gate_id]["artifact_id"] or artifact_id not in artifact_ids:
            raise GuardError(f"pre-run gate {gate_id} lacks its preregistered verified artifact")
        verified.append({"id": gate_id, "status": status, "artifact_id": artifact_id})
    missing = sorted(item["id"] for item in contracts.values() if item["required"] and item["id"] not in seen)
    failed = sorted(item["id"] for item in verified if contracts[item["id"]]["required"] and item["status"] != "PASS")
    if missing or (failed and not allow_required_fail):
        raise GuardError(f"required pre-run gates missing or failed: missing={missing}, failed={failed}")
    return sorted(verified, key=lambda item: item["id"])


def command_gates(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    results_path = Path(args.results).resolve()
    if results_path != (project / PRE_RUN_RESULTS_REL).resolve():
        raise GuardError("gate recording must use optimization/PRE_RUN_RESULTS.json")
    payload = load_json(results_path)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if external_wait_in_progress(control):
            raise GuardError("cannot record pre-run gates while waiting for an external event")
        lease = control.get("active_lease")
        if not isinstance(lease, dict) or current_lease(control) is None:
            raise GuardError("a live experiment lease is required")
        verify_frozen_lease(project, lease)
        expected_schema = lease.get("proposal_schema_version", 1)
        if payload.get("schema_version") != expected_schema or payload.get("experiment_id") != lease["experiment_id"]:
            raise GuardError("pre-run result schema or experiment_id does not match active lease")
        artifacts = verify_artifact_results(project, lease, payload.get("artifact_results"), require_all=False)
        gates = verify_gate_results(lease, payload.get("pre_run_gate_results"), artifacts, allow_required_fail=True)
        existing = lease.get("pre_run_gate_results")
        if isinstance(existing, dict):
            if canonical_hash({"gates": gates, "artifacts": artifacts}) != canonical_hash({"gates": existing.get("gates"), "artifacts": existing.get("artifacts")}):
                raise GuardError("pre-run gate results are already frozen and a different replay is rejected")
            print(json.dumps(existing, ensure_ascii=False, indent=2))
            return 0
        required_gate_ids = {item["id"] for item in lease.get("pre_run_gates", []) if item.get("required")}
        failed_required = sorted(item["id"] for item in gates if item["id"] in required_gate_ids and item["status"] == "FAIL")
        lease["pre_run_gate_results"] = {
            "gates": gates,
            "artifacts": artifacts,
            "recorded_at": iso_time(utc_now()),
            "failed_required": failed_required,
        }
        lease["preflight_failed"] = bool(failed_required)
        control["active_lease"] = lease
        save_control(project, control)
    print(json.dumps(lease["pre_run_gate_results"], ensure_ascii=False, indent=2))
    return 0


def render_policy_argv(policy: dict[str, Any], lease: dict[str, Any]) -> list[str]:
    rendered = [policy["executable"]]
    values = lease.get("binding_values") if isinstance(lease.get("binding_values"), dict) else {}
    for token in policy.get("argv", []):
        if "literal" in token:
            rendered.append(str(token["literal"]))
            continue
        binding_id = token.get("binding")
        binding = values.get(binding_id) if isinstance(values, dict) else None
        if not isinstance(binding, dict) or binding.get("state") != "BOUND":
            raise GuardError(f"runtime binding is not frozen yet: {binding_id}")
        rendered.append(str(binding["value"]))
    return rendered


def enforce_policy_phase(lease: dict[str, Any], policy: dict[str, Any]) -> None:
    if lease.get("preflight_failed"):
        raise GuardError("required preflight gate failed; only an invalid checkpoint may release the lease")
    required_gates = [gate for gate in lease.get("pre_run_gates", []) if gate.get("required")]
    if policy["phase"] != "preparation" and required_gates and lease.get("pre_run_gate_results") is None:
        raise GuardError("required pre-run gates are not recorded before this policy")
    if policy["phase"] == "preparation" and lease.get("pre_run_gate_results") is not None:
        raise GuardError("preparation phase is closed after pre-run gates are recorded")


def transport_contract_sha256(policy: dict[str, Any]) -> str:
    return canonical_hash({
        "policy_id": policy.get("id"),
        "executable": policy.get("executable"),
        "argv": policy.get("argv"),
        "cwd": policy.get("cwd"),
        "capture_binding": policy.get("capture_binding"),
        "transport": policy.get("transport"),
    })


def ssh_helper_argv(transport: dict[str, Any]) -> list[str]:
    argv = [
        transport["ssh_executable"], "-F", "none", "-T",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no",
        "-o", "RequestTTY=no",
        "-o", "ProxyCommand=none",
        "-o", "ProxyJump=none",
        "-o", "CanonicalizeHostname=no",
        "-o", "IdentityAgent=none",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"UserKnownHostsFile={transport['known_hosts_file']}",
        "-p", str(transport["port"]),
        "-i", transport["identity_file"],
    ]
    argv.extend(["--", f"{transport['user']}@{transport['host']}", transport["helper_path"]])
    return argv


def remote_helper_request(
    policy: dict[str, Any],
    *,
    operation: str,
    submission_nonce: str | None = None,
    sbatch_sha256: str | None = None,
) -> dict[str, Any]:
    transport = policy["transport"]
    request: dict[str, Any] = {
        "schema_version": REMOTE_REQUEST_SCHEMA,
        "operation": operation,
        "contract_sha256": transport_contract_sha256(policy),
        "helper_sha256": transport["helper_sha256"],
        "sbatch_path": transport["sbatch_path"],
        "remote_workdir": transport["remote_workdir"],
        "receipt_root": transport["receipt_root"],
        "remote_files": transport["remote_files"],
    }
    if operation != "doctor":
        request["submission_nonce"] = submission_nonce
        request["sbatch_sha256"] = sbatch_sha256
    if operation == "submit":
        request["argv"] = [token["literal"] for token in policy["argv"]]
        request["timeout_seconds"] = policy["timeout_seconds"]
    return request


def run_remote_helper(policy: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    transport = policy["transport"]
    ssh_executable = Path(transport["ssh_executable"])
    if (
        ssh_executable.is_symlink()
        or not ssh_executable.is_file()
        or not os.access(ssh_executable, os.X_OK)
        or file_hash(ssh_executable) != transport["ssh_executable_sha256"]
    ):
        raise GuardError("ssh transport executable changed after admission")
    known_hosts = Path(transport["known_hosts_file"])
    if known_hosts.is_symlink() or not known_hosts.is_file() or file_hash(known_hosts) != transport["known_hosts_sha256"]:
        raise GuardError("ssh transport known_hosts_file changed after admission")
    if transport.get("identity_file"):
        identity_file = Path(transport["identity_file"])
        if (
            identity_file.is_symlink()
            or not identity_file.is_file()
            or file_hash(identity_file) != transport.get("identity_file_sha256")
        ):
            raise GuardError("ssh transport identity_file changed after admission")
    timeout = int(transport["timeout_seconds"])
    if request["operation"] == "submit":
        timeout += int(policy["timeout_seconds"])
    try:
        completed = subprocess.run(
            ssh_helper_argv(transport),
            input=json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuardError(f"ssh helper transport failed before a verified receipt: {type(error).__name__}") from error
    metadata = {
        "ssh_exit_code": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
    }
    if completed.returncode != 0:
        raise GuardError("ssh helper returned a nonzero status without a verified receipt")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise GuardError("ssh helper must emit exactly one JSON response line")
    try:
        response = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise GuardError("ssh helper emitted malformed JSON") from error
    if not isinstance(response, dict) or response.get("contract_sha256") != request["contract_sha256"]:
        raise GuardError("ssh helper response drifted from the frozen transport contract")
    if request["operation"] == "doctor":
        if response.get("schema_version") != REMOTE_DOCTOR_SCHEMA or response.get("ready") is not True:
            raise GuardError("ssh helper doctor did not report ready")
        if response.get("helper_sha256") != transport["helper_sha256"]:
            raise GuardError("ssh helper version digest mismatched")
        if canonical_hash(response.get("remote_files")) != canonical_hash(transport["remote_files"]):
            raise GuardError("ssh helper remote file evidence drifted")
        ensure_sha256(response.get("sbatch_sha256"), "remote doctor sbatch_sha256")
    else:
        if response.get("schema_version") != REMOTE_RECEIPT_SCHEMA or response.get("submission_nonce") != request["submission_nonce"]:
            raise GuardError("ssh helper receipt identity mismatched")
        if response.get("helper_sha256") != transport["helper_sha256"] or response.get("sbatch_sha256") != request.get("sbatch_sha256"):
            raise GuardError("ssh helper receipt binary identity mismatched")
        if response.get("state") not in {"SUCCEEDED", "FAILED", "UNCERTAIN", "RUNNING", "ABSENT"}:
            raise GuardError("ssh helper receipt state is invalid")
        if response.get("state") == "SUCCEEDED" and SLURM_JOB_ID_RE.fullmatch(str(response.get("job_id", ""))) is None:
            raise GuardError("ssh helper success lacks a parsable Slurm Job ID")
    return response, metadata


def command_doctor(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    policy_id = ensure_id(args.policy, "policy")
    reservation = uuid.uuid4().hex
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control.get("runtime", {}).get("state") != "ACTIVE":
            raise GuardError("cannot run transport doctor while waiting for an external event")
        lease = current_lease(control)
        if lease is None:
            raise GuardError("a live experiment lease is required")
        verify_frozen_lease(project, lease)
        policy = next((item for item in lease.get("bash_policies", []) if item.get("id") == policy_id), None)
        if not isinstance(policy, dict) or policy.get("transport", {}).get("kind") != "ssh-helper-v1":
            raise GuardError("doctor requires a reviewed ssh-helper-v1 submission policy")
        if lease.get("preflight_failed"):
            raise GuardError("required preflight gate failed; transport doctor is no longer actionable")
        existing = lease.setdefault("transport_doctors", {}).get(policy_id)
        if isinstance(existing, dict) and existing.get("state") == "RUNNING":
            raise GuardError("ssh transport doctor is already running")
        lease["transport_doctors"][policy_id] = {
            "state": "RUNNING", "reservation": reservation,
            "contract_sha256": transport_contract_sha256(policy), "started_at": iso_time(utc_now()),
        }
        lease_id = lease["lease_id"]
        control["active_lease"] = lease
        save_control(project, control)
    try:
        request = remote_helper_request(policy, operation="doctor")
        response, metadata = run_remote_helper(policy, request)
    except Exception:
        with state_lock(project):
            control = load_control(project)
            lease = control.get("active_lease")
            if isinstance(lease, dict) and lease.get("lease_id") == lease_id:
                current = lease.setdefault("transport_doctors", {}).get(policy_id)
                if isinstance(current, dict) and current.get("reservation") == reservation:
                    lease["transport_doctors"][policy_id] = {
                        "state": "FAILED", "contract_sha256": transport_contract_sha256(policy),
                        "finished_at": iso_time(utc_now()),
                    }
                    control["active_lease"] = lease
                    save_control(project, control)
        raise
    with state_lock(project):
        control = load_control(project)
        lease = control.get("active_lease")
        if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
            raise GuardError("active lease changed while ssh transport doctor was running")
        current = lease.setdefault("transport_doctors", {}).get(policy_id)
        if not isinstance(current, dict) or current.get("reservation") != reservation:
            raise GuardError("ssh transport doctor reservation changed unexpectedly")
        record = {
            "state": "READY",
            "contract_sha256": request["contract_sha256"],
            "checked_at": iso_time(utc_now()),
            "helper_sha256": response["helper_sha256"],
            "sbatch_sha256": response["sbatch_sha256"],
            "remote_files": response["remote_files"],
            **metadata,
        }
        lease["transport_doctors"][policy_id] = record
        control["active_lease"] = lease
        save_control(project, control)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def command_submit_bind(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    policy_id = ensure_id(args.policy, "policy")
    reservation = uuid.uuid4().hex
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control["runtime"]["state"] != "ACTIVE":
            raise GuardError("cannot submit while waiting for an external event")
        lease = current_lease(control)
        if lease is None:
            raise GuardError("a live experiment lease is required")
        verify_frozen_lease(project, lease)
        policy = next((item for item in lease.get("bash_policies", []) if item.get("id") == policy_id), None)
        if not isinstance(policy, dict) or not policy.get("capture_binding"):
            raise GuardError("submit-bind requires a binding-capture Bash policy")
        binding_id = policy["capture_binding"]
        binding_contract = next((item for item in lease.get("runtime_bindings", []) if item.get("id") == binding_id), None)
        if not isinstance(binding_contract, dict) or binding_contract.get("source_policy_id") != policy_id:
            raise GuardError("capture policy does not match the frozen runtime binding source")
        if policy_id in lease.get("policy_runs", {}) or binding_id in lease.get("binding_values", {}):
            raise GuardError("submission policy or runtime binding was already consumed")
        enforce_policy_phase(lease, policy)
        if int(lease.get("mutations_used", 0)) >= int(lease.get("max_mutations", 0)):
            raise GuardError("experiment mutation allowance is exhausted")
        argv = render_policy_argv(policy, lease)
        cwd = project if policy["cwd"] == "." else project / policy["cwd"]
        transport_kind = policy.get("transport", {}).get("kind", "local")
        doctor_record = None
        if transport_kind == "ssh-helper-v1":
            doctor_record = lease.get("transport_doctors", {}).get(policy_id)
            if (
                not isinstance(doctor_record, dict)
                or doctor_record.get("state") != "READY"
                or doctor_record.get("contract_sha256") != transport_contract_sha256(policy)
            ):
                raise GuardError("ssh submission requires a successful doctor for the frozen transport contract")
        lease.setdefault("policy_runs", {})[policy_id] = {
            "state": "RUNNING",
            "reservation": reservation,
            "submission_nonce": reservation,
            "transport": transport_kind,
            "started_at": iso_time(utc_now()),
            "argv_sha256": canonical_hash(argv),
        }
        lease.setdefault("binding_values", {})[binding_id] = {
            "state": "CAPTURING",
            "reservation": reservation,
        }
        lease["mutations_used"] = int(lease.get("mutations_used", 0)) + 1
        lease_id = lease["lease_id"]
        control["active_lease"] = lease
        control["poll"] = None
        save_control(project, control)

    receipt_state = "FAILED"
    if transport_kind == "ssh-helper-v1":
        try:
            request = remote_helper_request(
                policy,
                operation="submit",
                submission_nonce=reservation,
                sbatch_sha256=str(doctor_record["sbatch_sha256"]),
            )
            response, metadata = run_remote_helper(policy, request)
            receipt_state = str(response["state"])
            success = receipt_state == "SUCCEEDED"
            job_id = str(response.get("job_id") or "")
            failure = None if success else (
                "remote submission failed definitively" if receipt_state == "FAILED"
                else "remote submission outcome is uncertain; reconcile the frozen nonce and never resubmit"
            )
            exit_code = response.get("exit_code")
            stdout_sha256 = str(response.get("stdout_sha256") or metadata["stdout_sha256"])
            stderr_sha256 = str(response.get("stderr_sha256") or metadata["stderr_sha256"])
        except Exception:
            success = False
            receipt_state = "UNCERTAIN"
            failure = "remote submission transport failed without a verified receipt; reconcile the frozen nonce and never resubmit"
            exit_code = None
            stdout_sha256 = hashlib.sha256(b"").hexdigest()
            stderr_sha256 = hashlib.sha256(b"").hexdigest()
            job_id = ""
    else:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
                timeout=int(policy["timeout_seconds"]),
            )
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            raw_value = lines[0] if len(lines) == 1 else ""
            job_id = raw_value.split(";", 1)[0]
            success = completed.returncode == 0 and SLURM_JOB_ID_RE.fullmatch(job_id) is not None
            receipt_state = "SUCCEEDED" if success else "FAILED"
            failure = None if success else "submission must exit 0 and emit exactly one parsable Slurm Job ID line"
            exit_code = completed.returncode
            stdout_sha256 = hashlib.sha256(completed.stdout.encode()).hexdigest()
            stderr_sha256 = hashlib.sha256(completed.stderr.encode()).hexdigest()
        except subprocess.TimeoutExpired as error:
            success = False
            receipt_state = "UNCERTAIN"
            failure = "submission timed out; outcome is uncertain and the one-shot policy remains consumed"
            exit_code = None
            stdout_sha256 = hashlib.sha256((error.stdout or "").encode() if isinstance(error.stdout, str) else (error.stdout or b"")).hexdigest()
            stderr_sha256 = hashlib.sha256((error.stderr or "").encode() if isinstance(error.stderr, str) else (error.stderr or b"")).hexdigest()
            job_id = ""
        except OSError:
            success = False
            receipt_state = "FAILED"
            failure = "local submission executable could not be started; the one-shot policy remains consumed"
            exit_code = None
            stdout_sha256 = hashlib.sha256(b"").hexdigest()
            stderr_sha256 = hashlib.sha256(b"").hexdigest()
            job_id = ""
        except Exception:
            success = False
            receipt_state = "UNCERTAIN"
            failure = "local submission raised an unexpected error; outcome is uncertain and the one-shot policy remains consumed"
            exit_code = None
            stdout_sha256 = hashlib.sha256(b"").hexdigest()
            stderr_sha256 = hashlib.sha256(b"").hexdigest()
            job_id = ""

    with state_lock(project):
        control = load_control(project)
        lease = control.get("active_lease")
        if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
            raise GuardError("active lease changed while the one-shot submission was running")
        run = lease.get("policy_runs", {}).get(policy_id)
        binding = lease.get("binding_values", {}).get(binding_id)
        if not isinstance(run, dict) or not isinstance(binding, dict) or run.get("reservation") != reservation or binding.get("reservation") != reservation:
            raise GuardError("submission reservation changed unexpectedly")
        finished_at = iso_time(utc_now())
        run.update({
            "state": "SUCCEEDED" if success else receipt_state,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "stdout_sha256": stdout_sha256,
            "stderr_sha256": stderr_sha256,
        })
        run.pop("reservation", None)
        if success:
            binding.clear()
            binding.update({
                "state": "BOUND",
                "kind": binding_contract["kind"],
                "value": job_id,
                "source_policy_id": policy_id,
                "bound_at": finished_at,
                "submission_stdout_sha256": stdout_sha256,
            })
        else:
            binding.clear()
            binding.update({
                "state": "FAILED" if receipt_state == "FAILED" else "UNCERTAIN",
                "source_policy_id": policy_id,
                "submission_nonce": reservation,
                "failed_at": finished_at,
            })
        control["active_lease"] = lease
        save_control(project, control)
    if not success:
        raise GuardError(failure or "submission binding failed")
    print(json.dumps(lease["binding_values"][binding_id], ensure_ascii=False, indent=2))
    return 0


def command_reconcile_bind(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    policy_id = ensure_id(args.policy, "policy")
    reservation = uuid.uuid4().hex
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control.get("runtime", {}).get("state") != "ACTIVE":
            raise GuardError("cannot reconcile while waiting for an external event")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("an active experiment lease is required")
        verify_frozen_lease(project, lease)
        policy = next((item for item in lease.get("bash_policies", []) if item.get("id") == policy_id), None)
        if not isinstance(policy, dict) or policy.get("transport", {}).get("kind") != "ssh-helper-v1":
            raise GuardError("reconcile-bind requires an ssh-helper-v1 submission policy")
        binding_id = policy.get("capture_binding")
        binding = lease.get("binding_values", {}).get(binding_id)
        run = lease.get("policy_runs", {}).get(policy_id)
        if not isinstance(binding, dict) or binding.get("state") != "UNCERTAIN" or not isinstance(run, dict):
            raise GuardError("reconcile-bind is available only for an uncertain consumed submission")
        if isinstance(run.get("reconcile"), dict) and run["reconcile"].get("state") == "RUNNING":
            raise GuardError("submission reconciliation is already running")
        nonce = binding.get("submission_nonce") or run.get("submission_nonce")
        if not isinstance(nonce, str) or re.fullmatch(r"[a-f0-9]{32}", nonce) is None:
            raise GuardError("uncertain submission lost its frozen nonce")
        doctor_record = lease.get("transport_doctors", {}).get(policy_id)
        if not isinstance(doctor_record, dict) or doctor_record.get("state") != "READY":
            raise GuardError("uncertain submission lost its frozen doctor result")
        run["reconcile"] = {"state": "RUNNING", "reservation": reservation, "started_at": iso_time(utc_now())}
        lease_id = lease["lease_id"]
        control["active_lease"] = lease
        save_control(project, control)
    try:
        request = remote_helper_request(
            policy,
            operation="reconcile",
            submission_nonce=nonce,
            sbatch_sha256=str(doctor_record["sbatch_sha256"]),
        )
        response, metadata = run_remote_helper(policy, request)
    except Exception as error:
        with state_lock(project):
            control = load_control(project)
            lease = control.get("active_lease")
            if isinstance(lease, dict) and lease.get("lease_id") == lease_id:
                run = lease.get("policy_runs", {}).get(policy_id)
                reconcile = run.get("reconcile") if isinstance(run, dict) else None
                if isinstance(reconcile, dict) and reconcile.get("reservation") == reservation:
                    run["reconcile"] = {"state": "UNCERTAIN", "finished_at": iso_time(utc_now())}
                    control["active_lease"] = lease
                    save_control(project, control)
        raise GuardError("reconciliation transport failed; submission remains uncertain and must not be retried") from error
    with state_lock(project):
        control = load_control(project)
        lease = control.get("active_lease")
        if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
            raise GuardError("active lease changed while submission reconciliation was running")
        run = lease.get("policy_runs", {}).get(policy_id)
        binding = lease.get("binding_values", {}).get(binding_id)
        reconcile = run.get("reconcile") if isinstance(run, dict) else None
        if (
            not isinstance(run, dict)
            or not isinstance(binding, dict)
            or not isinstance(reconcile, dict)
            or reconcile.get("reservation") != reservation
            or binding.get("state") != "UNCERTAIN"
        ):
            raise GuardError("submission reconciliation reservation changed unexpectedly")
        state = response["state"]
        finished_at = iso_time(utc_now())
        run["reconcile"] = {"state": state, "finished_at": finished_at, **metadata}
        if state == "SUCCEEDED":
            binding.clear()
            binding.update({
                "state": "BOUND",
                "kind": "slurm_job_id",
                "value": str(response["job_id"]),
                "source_policy_id": policy_id,
                "bound_at": finished_at,
                "reconciled": True,
            })
            run["state"] = "SUCCEEDED"
        elif state == "FAILED":
            binding["state"] = "FAILED"
            binding["reconciled_at"] = finished_at
            run["state"] = "FAILED"
        else:
            binding["state"] = "UNCERTAIN"
            binding["reconciled_at"] = finished_at
            run["state"] = "UNCERTAIN"
        control["active_lease"] = lease
        save_control(project, control)
    if state != "SUCCEEDED":
        raise GuardError(f"submission remains {state}; never rerun submit-bind for this lease")
    print(json.dumps(binding, ensure_ascii=False, indent=2))
    return 0


def argv_option(argv: list[Any], option: str) -> str | None:
    for index, token in enumerate(argv[:-1]):
        if token == option and isinstance(argv[index + 1], str):
            return argv[index + 1]
    return None


def monitor_contract(lease: dict[str, Any], monitor_id: str) -> dict[str, Any]:
    monitor = next((item for item in lease.get("external_monitors", []) if item.get("id") == monitor_id), None)
    if not isinstance(monitor, dict):
        raise GuardError(f"unknown external monitor: {monitor_id}")
    return monitor


def verify_monitor_run(lease: dict[str, Any], monitor: dict[str, Any]) -> dict[str, Any]:
    binding = lease.get("binding_values", {}).get(monitor["binding_id"])
    if not isinstance(binding, dict) or binding.get("state") != "BOUND":
        raise GuardError("external monitor runtime binding is not frozen")
    job_id = str(binding.get("value", ""))
    if SLURM_JOB_ID_RE.fullmatch(job_id) is None:
        raise GuardError("external monitor has an invalid frozen Slurm Job ID")
    root = Path(monitor["state_root"])
    base = root / "supervisors" / f"{monitor['host']}-{job_id}"
    current = load_owned_external_json(base / "current.json", root)
    run_id = current.get("run_id")
    if (
        current.get("schema_version") != "codex-hpc-monitor.current/v1"
        or current.get("host") != monitor["host"]
        or current.get("job_id") != job_id
        or not isinstance(run_id, str)
        or RUN_ID_RE.fullmatch(run_id) is None
    ):
        raise GuardError("external monitor current pointer does not match its frozen contract")
    run_dir = base / "runs" / run_id
    manifest_path = run_dir / "manifest.json"
    manifest = load_owned_external_json(manifest_path, root)
    try:
        manifest_created = parse_time(str(manifest.get("created_at")))
        binding_time = parse_time(str(binding.get("bound_at")))
    except (TypeError, ValueError) as error:
        raise GuardError("external monitor manifest or binding timestamp is invalid") from error
    if (
        manifest.get("schema_version") != "codex-hpc-monitor.manifest/v1"
        or manifest.get("run_id") != run_id
        or manifest.get("host") != monitor["host"]
        or manifest.get("job_id") != job_id
        or manifest.get("scope") != "slurm_only"
        or manifest.get("project_gate_evaluated") is not False
        or manifest.get("state_dir") != os.fspath(root)
        or manifest_created < binding_time
    ):
        raise GuardError("external monitor manifest does not match its frozen contract")
    watcher_argv = manifest.get("watcher_argv")
    if not isinstance(watcher_argv, list) or not all(isinstance(token, str) for token in watcher_argv):
        raise GuardError("external monitor watcher argv is invalid")
    if len(watcher_argv) < 3 or watcher_argv[2] != job_id:
        raise GuardError("external monitor watcher argv has the wrong Job ID")
    expected_options = {
        "--host": monitor["host"],
        "--state-dir": os.fspath(root),
        "--expected-owner": monitor["expected_owner"],
        "--expected-job-name": monitor["expected_job_name"],
        "--expected-partition": monitor["expected_partition"],
    }
    for option, expected in expected_options.items():
        if argv_option(watcher_argv, option) != expected:
            raise GuardError(f"external monitor watcher argv drifted for {option}")
    return {
        "job_id": job_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "manifest_path": manifest_path,
        "manifest_sha256": file_hash(manifest_path),
    }


def monitor_policy(lease: dict[str, Any], monitor: dict[str, Any]) -> dict[str, Any]:
    policy = next(
        (item for item in lease.get("bash_policies", []) if item.get("id") == monitor.get("start_policy_id")),
        None,
    )
    if not isinstance(policy, dict):
        raise GuardError("external monitor lost its frozen start policy")
    return policy


def scheduler_gate_binding_id(lease: dict[str, Any], monitor: dict[str, Any], run: dict[str, Any]) -> str:
    return "gg-" + canonical_hash({
        "schema": "goal-guardrails.scheduler-gate-binding/v1",
        "lease_id": lease.get("lease_id"),
        "monitor_id": monitor.get("id"),
        "job_id": run.get("job_id"),
        "run_id": run.get("run_id"),
    })[:48]


def scheduler_gate_command(
    project: Path,
    lease: dict[str, Any],
    monitor: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    """Arm and read back the Codex idle-continuation marker without creating a model turn."""
    policy = monitor_policy(lease, monitor)
    monitor_argv = render_policy_argv(policy, lease)
    if len(monitor_argv) < 2:
        raise GuardError("external monitor start policy cannot locate its provider script")
    policy_cwd = project if policy.get("cwd") == "." else project / str(policy.get("cwd"))
    monitor_script = Path(monitor_argv[1])
    if not monitor_script.is_absolute():
        monitor_script = policy_cwd / monitor_script
    monitor_script = Path(os.path.abspath(os.fspath(monitor_script)))
    bridge_script = monitor_script.with_name("app_server_bridge.py")
    if monitor_script.name != "supervise_slurm_job.py" or not bridge_script.is_file() or bridge_script.is_symlink():
        raise GuardError("contract_version 2 requires app_server_bridge.py beside supervise_slurm_job.py")
    required_options = {
        "--state-dir": monitor.get("state_root"),
        "--bridge-config": argv_option(monitor_argv, "--bridge-config"),
        "--event-binding": argv_option(monitor_argv, "--event-binding"),
    }
    if required_options["--state-dir"] != argv_option(monitor_argv, "--state-dir"):
        raise GuardError("external monitor start policy state directory drifted before scheduler-gate arm")
    for option in ("--bridge-config", "--event-binding"):
        value = required_options[option]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise GuardError(f"external monitor start policy lost its frozen {option} path")
    binding_path = Path(str(required_options["--event-binding"]))
    config_path = Path(str(required_options["--bridge-config"]))
    event_binding = load_private_external_json(binding_path, "external monitor event binding")
    bridge_config = load_private_external_json(config_path, "external monitor bridge config")
    expected_binding_fields = {"schema", "codex_home_id", "app_server_instance", "thread_id", "workspace"}
    expected_config_fields = {
        "schema", "enabled", "instance_id", "codex_home", "codex_home_id", "workspace", "transport",
        "request_timeout_seconds", "poll_seconds", "lease_seconds", "max_attempts", "backoff_initial_seconds",
        "backoff_max_seconds", "turn_completion_timeout_seconds",
    }
    transport = bridge_config.get("transport")
    transport_command = transport.get("command") if isinstance(transport, dict) else None
    if (
        set(event_binding) != expected_binding_fields
        or event_binding.get("schema") != "codex-monitor.event-binding/v1"
        or not isinstance(event_binding.get("thread_id"), str)
        or THREAD_ID_RE.fullmatch(event_binding["thread_id"]) is None
        or set(bridge_config) != expected_config_fields
        or bridge_config.get("schema") != "codex-monitor.bridge-config/v1"
        or bridge_config.get("enabled") is not True
        or not isinstance(transport, dict)
        or set(transport) != {"type", "command"}
        or transport.get("type") != "stdio"
        or not isinstance(transport_command, list)
        or len(transport_command) < 2
        or not all(isinstance(token, str) and token for token in transport_command)
        or not Path(transport_command[0]).is_absolute()
        or transport_command[1] != "app-server"
    ):
        raise GuardError("external monitor event binding or bridge config has an invalid scheduler-gate contract")
    try:
        project_real = project.resolve(strict=True)
        binding_workspace = Path(str(event_binding["workspace"])).resolve(strict=True)
        config_workspace = Path(str(bridge_config["workspace"])).resolve(strict=True)
        configured_executable = Path(transport_command[0]).resolve(strict=True)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise GuardError("external monitor scheduler-gate identity cannot be resolved") from error
    if (
        binding_workspace != project_real
        or config_workspace != project_real
        or event_binding.get("workspace") != bridge_config.get("workspace")
        or event_binding.get("codex_home_id") != bridge_config.get("codex_home_id")
        or event_binding.get("app_server_instance") != bridge_config.get("instance_id")
    ):
        raise GuardError("external monitor event binding and bridge config identify a different Goal workspace")
    expected_binding_digest = monitor_canonical_digest(event_binding)
    expected_config_digest = monitor_canonical_digest(bridge_config)
    expected_executable_sha256 = "sha256:" + file_hash(configured_executable)
    expected_workspace_id = "sha256:" + hashlib.sha256(str(bridge_config["workspace"]).encode()).hexdigest()
    binding_id = scheduler_gate_binding_id(lease, monitor, run)
    command = [
        monitor_argv[0],
        os.fspath(bridge_script),
        "continuation-gate",
        "arm",
        "--state-dir",
        str(required_options["--state-dir"]),
        "--bridge-config",
        str(required_options["--bridge-config"]),
        "--event-binding",
        str(required_options["--event-binding"]),
        "--binding-id",
        binding_id,
        "--timeout-seconds",
        "10",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=policy_cwd,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuardError("scheduler continuation gate could not be reached; retry wait-monitor without resubmitting the workload") from error
    try:
        response = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise GuardError("scheduler continuation gate returned an unreadable response") from error
    if (
        completed.returncode != 0
        or not isinstance(response, dict)
        or response.get("schema_version") != "codex-monitor.bridge.continuation-gate/v1"
        or response.get("state") != "ok"
        or response.get("action") != "arm"
        or response.get("binding_id") != binding_id
        or response.get("goal_status") != "active"
        or response.get("deferred") is not True
        or response.get("receipt_state") != "armed"
        or response.get("model_turn_created") is not False
        or not isinstance(response.get("thread_id"), str)
        or THREAD_ID_RE.fullmatch(response["thread_id"]) is None
        or response.get("thread_id") != event_binding["thread_id"]
        or not isinstance(response.get("goal_id"), str)
        or not response["goal_id"]
    ):
        reason = response.get("reason") if isinstance(response, dict) else None
        suffix = f" ({reason})" if isinstance(reason, str) and reason else ""
        raise GuardError(
            "scheduler continuation gate was not armed and read back"
            + suffix
            + "; retry wait-monitor without resubmitting the workload"
        )
    receipt_path = Path(str(response.get("receipt", "")))
    state_root = Path(str(monitor["state_root"]))
    receipt = load_owned_external_json(receipt_path, state_root)
    expected_receipt_fields = {
        "schema", "binding_id", "binding_digest", "config_digest", "instance_id", "codex_home_id",
        "workspace_id", "thread_id", "executable", "executable_sha256", "codex_version", "goal_id",
        "state", "armed_at", "cleared_at",
    }
    if (
        set(receipt) != expected_receipt_fields
        or receipt.get("schema") != "codex-monitor.continuation-gate-receipt/v1"
        or receipt.get("binding_id") != binding_id
        or receipt.get("binding_digest") != expected_binding_digest
        or receipt.get("config_digest") != expected_config_digest
        or receipt.get("instance_id") != bridge_config["instance_id"]
        or receipt.get("codex_home_id") != bridge_config["codex_home_id"]
        or receipt.get("workspace_id") != expected_workspace_id
        or receipt.get("thread_id") != response["thread_id"]
        or receipt.get("goal_id") != response["goal_id"]
        or receipt.get("executable") != os.fspath(configured_executable)
        or receipt.get("executable_sha256") != expected_executable_sha256
        or receipt.get("codex_version") != "0.151.0"
        or receipt.get("state") != "armed"
        or receipt.get("cleared_at") is not None
    ):
        raise GuardError("scheduler continuation gate receipt does not match its read-back")
    try:
        parse_time(str(receipt.get("armed_at")))
    except (TypeError, ValueError) as error:
        raise GuardError("scheduler continuation gate receipt has an invalid timestamp") from error
    return {
        "schema_version": "goal-guardrails.scheduler-gate/v1",
        "state": "ARMED",
        "binding_id": binding_id,
        "thread_id": response["thread_id"],
        "goal_id": response["goal_id"],
        "receipt_path": os.fspath(receipt_path),
        "receipt_sha256": file_hash(receipt_path),
        "armed_at": receipt["armed_at"],
        "model_turn_created": False,
    }


def finalize_scheduler_wait_locked(project: Path, control: dict[str, Any]) -> dict[str, Any]:
    """Roll an idempotent ARMING state forward only after the scheduler gate is verified."""
    runtime = control.get("runtime", {})
    wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else None
    if runtime.get("state") != "ARMING_EXTERNAL_WAIT" or not isinstance(wait, dict):
        raise GuardError("runtime is not arming an external wait")
    lease = control.get("active_lease")
    if not isinstance(lease, dict) or current_lease(control) is None:
        raise GuardError("external wait arming lost its live experiment lease")
    verify_frozen_lease(project, lease)
    monitor = monitor_contract(lease, str(wait.get("monitor_id")))
    if monitor.get("contract_version") != 2:
        raise GuardError("only external-monitor contract_version 2 has a scheduler gate")
    run = verify_monitor_run(lease, monitor)
    for key in ("job_id", "run_id", "manifest_sha256"):
        if wait.get(key) != run.get(key):
            raise GuardError(f"external monitor {key} drifted while arming its scheduler gate")
    scheduler_gate = scheduler_gate_command(project, lease, monitor, run)
    remaining = int(lease.get("remaining_seconds", 0))
    if remaining <= 0:
        remaining = max(1, int((parse_time(lease["expires_at"]) - utc_now()).total_seconds()))
    lease["scheduler_gate"] = scheduler_gate
    lease["suspended_at"] = lease.get("suspended_at") or iso_time(utc_now())
    lease["remaining_seconds"] = remaining
    wait["scheduler_gate"] = scheduler_gate
    wait["entered_at"] = iso_time(utc_now())
    runtime["state"] = "WAITING_EXTERNAL_EVENT"
    runtime["wait"] = wait
    control["active_lease"] = lease
    control["runtime"] = runtime
    control["poll"] = None
    save_control(project, control)
    return wait


def rearm_scheduler_wait_locked(project: Path, control: dict[str, Any]) -> dict[str, Any]:
    """Restore the deferral cleared by an explicit no-event SessionStart."""
    runtime = control.get("runtime", {})
    wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else None
    lease = control.get("active_lease")
    if runtime.get("state") != "WAITING_EXTERNAL_EVENT" or not isinstance(wait, dict) or not isinstance(lease, dict):
        raise GuardError("runtime is not waiting on a scheduler-gated external monitor")
    monitor = monitor_contract(lease, str(wait.get("monitor_id")))
    if monitor.get("contract_version") != 2:
        raise GuardError("external monitor does not use the scheduler-gated contract")
    run = verify_monitor_run(lease, monitor)
    scheduler_gate = scheduler_gate_command(project, lease, monitor, run)
    lease["scheduler_gate"] = scheduler_gate
    wait["scheduler_gate"] = scheduler_gate
    runtime["wait"] = wait
    control["active_lease"] = lease
    control["runtime"] = runtime
    save_control(project, control)
    return scheduler_gate


def command_wait_monitor(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    monitor_id = ensure_id(args.monitor, "monitor")
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        runtime_state = control["runtime"]["state"]
        if runtime_state == "WAITING_EXTERNAL_EVENT":
            raise GuardError("runtime is already waiting for an external event")
        if runtime_state == "ARMING_EXTERNAL_WAIT":
            wait = control["runtime"].get("wait")
            lease = current_lease(control)
            if not isinstance(wait, dict) or wait.get("monitor_id") != monitor_id or lease is None:
                raise GuardError("a different external wait is already being armed")
            monitor = monitor_contract(lease, monitor_id)
            if monitor.get("contract_version") != 2:
                raise GuardError("legacy external-monitor waits cannot enter ARMING_EXTERNAL_WAIT")
            output = finalize_scheduler_wait_locked(project, control)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        lease = current_lease(control)
        if lease is None:
            raise GuardError("a live experiment lease is required before waiting")
        verify_frozen_lease(project, lease)
        if lease.get("wake_event") is not None:
            raise GuardError("this lease already consumed its one terminal wake event")
        monitor = monitor_contract(lease, monitor_id)
        run = verify_monitor_run(lease, monitor)
        terminal_path = run["run_dir"] / "terminal.json"
        baseline = file_hash(terminal_path) if terminal_path.is_file() and not terminal_path.is_symlink() else None
        wait = {
            "kind": "external_monitor",
            "monitor_id": monitor_id,
            "provider": monitor["provider"],
            "contract_version": monitor["contract_version"],
            "job_id": run["job_id"],
            "run_id": run["run_id"],
            "manifest_sha256": run["manifest_sha256"],
            "baseline_terminal_sha256": baseline,
            "arming_started_at": iso_time(utc_now()),
        }
        control["active_lease"] = lease
        if monitor.get("contract_version") == 2:
            remaining = max(1, int((parse_time(lease["expires_at"]) - utc_now()).total_seconds()))
            lease["suspended_at"] = iso_time(utc_now())
            lease["remaining_seconds"] = remaining
            control["active_lease"] = lease
            control["runtime"]["state"] = "ARMING_EXTERNAL_WAIT"
            control["runtime"]["wait"] = wait
            control["poll"] = None
            save_control(project, control)
            wait = finalize_scheduler_wait_locked(project, control)
        else:
            remaining = max(1, int((parse_time(lease["expires_at"]) - utc_now()).total_seconds()))
            lease["suspended_at"] = iso_time(utc_now())
            lease["remaining_seconds"] = remaining
            wait["entered_at"] = iso_time(utc_now())
            control["active_lease"] = lease
            control["runtime"]["state"] = "WAITING_EXTERNAL_EVENT"
            control["runtime"]["wait"] = wait
            control["poll"] = None
            save_control(project, control)
    print(json.dumps(wait, ensure_ascii=False, indent=2))
    return 0


def verify_monitor_terminal(
    project: Path,
    lease: dict[str, Any],
    monitor: dict[str, Any],
    wait: dict[str, Any],
    requested_event_id: str | None = None,
) -> dict[str, Any]:
    root = Path(monitor["state_root"])
    run_dir = root / "supervisors" / f"{monitor['host']}-{wait['job_id']}" / "runs" / wait["run_id"]
    manifest_path = run_dir / "manifest.json"
    manifest = load_owned_external_json(manifest_path, root)
    if file_hash(manifest_path) != wait.get("manifest_sha256") or manifest.get("job_id") != wait["job_id"]:
        raise GuardError("external monitor manifest changed while waiting")
    terminal_path = run_dir / "terminal.json"
    terminal = load_owned_external_json(terminal_path, root)
    terminal_sha256 = file_hash(terminal_path)
    watcher_exit_code = terminal.get("watcher_exit_code")
    watcher_result = terminal.get("watcher_result")
    watcher_payload = watcher_result.get("payload") if isinstance(watcher_result, dict) else None
    if (
        terminal.get("schema_version") != "codex-hpc-monitor.terminal/v1"
        or terminal.get("host") != monitor["host"]
        or terminal.get("job_id") != wait["job_id"]
        or terminal.get("run_id") != wait["run_id"]
        or terminal.get("scope") != "slurm_only"
        or terminal.get("project_gate_evaluated") is not False
        or terminal.get("manifest_sha256") != wait.get("manifest_sha256")
        or type(watcher_exit_code) is not int
        or not isinstance(watcher_result, dict)
        or watcher_result.get("verified") is not True
        or not isinstance(watcher_payload, dict)
    ):
        raise GuardError("external terminal does not preserve the verified monitor evidence chain")
    expected_identity = {
        "job_id": wait["job_id"],
        "owner": monitor["expected_owner"],
        "job_name": monitor["expected_job_name"],
        "partition": monitor["expected_partition"],
    }
    for key, expected in expected_identity.items():
        if watcher_payload.get(key) != expected:
            raise GuardError(f"external terminal scheduler identity drifted for {key}")
    publication_path = run_dir / "semantic_event.json"
    publication = load_owned_external_json(publication_path, root)
    event_id = ensure_prefixed_sha256(publication.get("event_id"), "external semantic event ID")
    if requested_event_id is not None and event_id != ensure_prefixed_sha256(requested_event_id, "requested event ID"):
        raise GuardError("requested semantic event does not match the frozen monitor run")
    if (
        set(publication) != {"schema_version", "run_id", "event_id", "event", "state", "published_at"}
        or publication.get("schema_version") != "codex-hpc-monitor.semantic-event/v1"
        or publication.get("run_id") != wait["run_id"]
        or publication.get("state") not in {"published", "duplicate"}
    ):
        raise GuardError("external monitor semantic-event publication is invalid")
    try:
        parse_time(str(publication.get("published_at")))
    except (TypeError, ValueError) as error:
        raise GuardError("external monitor semantic-event timestamp is invalid") from error

    event_path = root / "outbox" / event_id.removeprefix("sha256:") / "event.json"
    event = load_owned_external_json(event_path, root)
    expected_event_fields = {
        "schema", "event_id", "created_at", "monitor", "event", "exit_code", "business_verdict", "binding",
    }
    event_monitor = event.get("monitor")
    event_binding = event.get("binding")
    manifest_binding = manifest.get("event_binding")
    expected_binding_fields = {"schema", "codex_home_id", "app_server_instance", "thread_id", "workspace"}
    if (
        set(event) != expected_event_fields
        or event.get("schema") != "codex-monitor.event/v1"
        or event.get("event_id") != event_id
        or event.get("event") != publication.get("event")
        or event.get("business_verdict") != "pending"
        or not isinstance(event_monitor, dict)
        or set(event_monitor) != {"backend", "handle", "generation", "terminal_digest"}
        or event_monitor.get("backend") != "slurm"
        or event_monitor.get("handle") != f"{monitor['host']}-{wait['job_id']}"
        or event_monitor.get("generation") != wait["run_id"]
        or event_monitor.get("terminal_digest") != f"sha256:{terminal_sha256}"
        or type(event.get("exit_code")) is not int
        or event.get("exit_code") != watcher_exit_code
        or not isinstance(event_binding, dict)
        or set(event_binding) != expected_binding_fields
        or event_binding != manifest_binding
        or event_binding.get("schema") != "codex-monitor.event-binding/v1"
        or not isinstance(event_binding.get("codex_home_id"), str)
        or SHA256_PREFIX_RE.fullmatch(event_binding["codex_home_id"]) is None
        or not isinstance(event_binding.get("app_server_instance"), str)
        or HOST_RE.fullmatch(event_binding["app_server_instance"]) is None
        or not isinstance(event_binding.get("thread_id"), str)
        or THREAD_ID_RE.fullmatch(event_binding["thread_id"]) is None
        or not isinstance(event_binding.get("workspace"), str)
    ):
        raise GuardError("external semantic event does not match the frozen monitor and terminal contract")
    binding_bytes = (json.dumps(event_binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if manifest.get("event_binding_digest") != f"sha256:{hashlib.sha256(binding_bytes).hexdigest()}":
        raise GuardError("external monitor event binding digest is missing or drifted")
    try:
        if Path(event_binding["workspace"]).resolve(strict=True) != project.resolve(strict=True):
            raise GuardError("external semantic event is bound to a different project workspace")
        parse_time(str(event.get("created_at")))
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise GuardError("external semantic event binding or timestamp is invalid") from error
    event_identity = {
        "schema": event.get("schema"),
        "monitor": event_monitor,
        "event": event.get("event"),
        "binding": event_binding,
    }
    identity_bytes = (json.dumps(event_identity, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if event_id != f"sha256:{hashlib.sha256(identity_bytes).hexdigest()}":
        raise GuardError("external semantic event ID does not match its immutable identity")
    expected_events = {
        0: "transport_success", 3: "transport_failure", 5: "lost_observability",
        7: "contract_violation", 8: "lost_observability", 9: "contract_violation",
        10: "deadline_exceeded",
    }
    if expected_events.get(watcher_exit_code) != event.get("event"):
        raise GuardError("external semantic event does not match the verified watcher outcome")
    return {
        "event_id": event_id,
        "event_path": event_path,
        "event_sha256": file_hash(event_path),
        "publication_path": publication_path,
        "publication_sha256": file_hash(publication_path),
        "terminal_path": terminal_path,
        "terminal_sha256": terminal_sha256,
        "terminal": terminal,
        "watcher_payload": watcher_payload,
    }


def wake_monitor_locked(
    project: Path,
    control: dict[str, Any],
    monitor_id: str,
    requested_event_id: str | None,
) -> tuple[dict[str, str] | None, bool]:
    seen_events = control["runtime"].get("seen_events", [])
    if requested_event_id is not None and any(
        isinstance(item, dict)
        and item.get("event_key") == requested_event_id
        and item.get("monitor_id") == monitor_id
        for item in seen_events
    ):
        return None, True
    wait = control["runtime"].get("wait")
    runtime_state = control["runtime"]["state"]
    if runtime_state not in {"ARMING_EXTERNAL_WAIT", "WAITING_EXTERNAL_EVENT"} or not isinstance(wait, dict):
        raise GuardError("runtime is not waiting for an external event")
    if wait.get("kind") != "external_monitor" or wait.get("monitor_id") != monitor_id:
        raise GuardError("wake-monitor does not match the registered external monitor wait")
    lease = control.get("active_lease")
    if not isinstance(lease, dict):
        raise GuardError("waiting runtime lost its active lease")
    verify_frozen_lease(project, lease)
    monitor = monitor_contract(lease, monitor_id)
    if runtime_state == "ARMING_EXTERNAL_WAIT" and monitor.get("contract_version") != 2:
        raise GuardError("only a contract-version-2 monitor may consume a terminal event while arming")
    evidence = verify_monitor_terminal(project, lease, monitor, wait, requested_event_id)
    receipt_rel = RECEIPTS_REL / lease["lease_id"] / f"{monitor_id}.json"
    receipt_path = project / receipt_rel
    watcher_payload = evidence["watcher_payload"]
    project_receipt = {
            "schema_version": "goal-guardrails.external-monitor-receipt/v2",
            "lease_id": lease["lease_id"],
            "experiment_id": lease["experiment_id"],
            "monitor_id": monitor_id,
            "provider": monitor["provider"],
            "binding": {
                "id": monitor["binding_id"],
                "value": wait["job_id"],
                "source_policy_id": lease["binding_values"][monitor["binding_id"]]["source_policy_id"],
            },
            "monitor": {
                "host": monitor["host"],
                "run_id": wait["run_id"],
                "manifest_sha256": wait["manifest_sha256"],
            },
            "source": {
                "semantic_event_id": evidence["event_id"],
                "semantic_event_sha256": evidence["event_sha256"],
                "semantic_publication_sha256": evidence["publication_sha256"],
                "terminal_sha256": evidence["terminal_sha256"],
                "watcher_exit_code": evidence["terminal"].get("watcher_exit_code"),
                "slurm_classification": watcher_payload.get("slurm_classification"),
                "scheduler_state": watcher_payload.get("state"),
                "scheduler_exit_code": watcher_payload.get("exit_code"),
                "owner": watcher_payload.get("owner"),
                "job_name": watcher_payload.get("job_name"),
                "partition": watcher_payload.get("partition"),
            },
            "scope": "scheduler_only",
            "project_gate_evaluated": False,
            "business_verdict": "pending",
            "materialized_at": iso_time(utc_now()),
    }
    if receipt_path.is_symlink():
        raise GuardError("controller monitor receipt path is an unsafe symbolic link")
    if receipt_path.exists():
        existing = load_json(receipt_path)
        expected_stable = {key: value for key, value in project_receipt.items() if key != "materialized_at"}
        existing_stable = {key: value for key, value in existing.items() if key != "materialized_at"}
        if existing_stable != expected_stable:
            raise GuardError("existing controller monitor receipt conflicts with the verified terminal")
        project_receipt = existing
    else:
        atomic_json(receipt_path, project_receipt)
    receipt_sha256 = file_hash(receipt_path)
    receipt_ref = {
            "path": receipt_rel.as_posix(),
            "sha256": receipt_sha256,
            "source_terminal_sha256": evidence["terminal_sha256"],
            "source_semantic_event_id": evidence["event_id"],
            "source_semantic_event_sha256": evidence["event_sha256"],
    }
    lease.setdefault("monitor_receipts", {})[monitor_id] = receipt_ref
    event_time = str(project_receipt.get("materialized_at") or iso_time(utc_now()))
    lease["wake_event"] = {
            "event_key": evidence["event_id"],
            "monitor_id": monitor_id,
            "path": receipt_rel.as_posix(),
            "sha256": receipt_sha256,
            "time": event_time,
    }
    remaining = int(lease.pop("remaining_seconds", 0))
    lease.pop("suspended_at", None)
    lease["expires_at"] = iso_time(utc_now() + timedelta(seconds=max(1, remaining)))
    seen = control["runtime"].get("seen_events", [])
    seen.append({
        "event_key": lease["wake_event"]["event_key"],
        "monitor_id": monitor_id,
        "path": receipt_rel.as_posix(),
        "sha256": receipt_sha256,
        "source_sha256": evidence["event_sha256"],
        "time": event_time,
    })
    control["active_lease"] = lease
    control["runtime"] = {"state": "ACTIVE", "wait": None, "seen_events": seen[-32:]}
    control["poll"] = None
    save_control(project, control)
    return receipt_ref, False


def command_wake_monitor(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    monitor_id = ensure_id(args.monitor, "monitor")
    requested_event_id = getattr(args, "event_id", None)
    if requested_event_id is not None:
        requested_event_id = ensure_prefixed_sha256(requested_event_id, "requested event ID")
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        receipt, duplicate = wake_monitor_locked(project, control, monitor_id, requested_event_id)
    if duplicate:
        print("duplicate")
    else:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


def command_wait(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if external_wait_in_progress(control):
            raise GuardError("runtime is already waiting for an external event")
        lease = current_lease(control)
        if lease is None:
            raise GuardError("a live experiment lease is required before waiting")
        verify_frozen_lease(project, lease)
        if lease.get("wake_event") is not None:
            raise GuardError("this lease already consumed its one terminal wake event")
        if any(item.get("required") is not False for item in lease.get("external_monitors", [])):
            raise GuardError("a required external monitor must use wait-monitor, not project-artifact wait")
        event_key = ensure_id(args.event_key, "event_key")
        event_path = normalize_project_path(project, args.event_path)
        event_contracts = {item["path"]: item for item in lease.get("checkpoint_artifacts", [])}
        if event_path not in event_contracts or not event_contracts[event_path].get("required"):
            raise GuardError("wait event path must be a required preregistered checkpoint artifact")
        event_file = project / event_path
        baseline = file_hash(event_file) if event_file.is_file() and not event_file.is_symlink() else None
        remaining = max(1, int((parse_time(lease["expires_at"]) - utc_now()).total_seconds()))
        lease["suspended_at"] = iso_time(utc_now())
        lease["remaining_seconds"] = remaining
        control["active_lease"] = lease
        control["runtime"]["state"] = "WAITING_EXTERNAL_EVENT"
        control["runtime"]["wait"] = {
            "kind": "project_artifact",
            "event_key": event_key,
            "event_path": event_path,
            "baseline_sha256": baseline,
            "entered_at": iso_time(utc_now()),
        }
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["runtime"]["wait"], ensure_ascii=False, indent=2))
    return 0


def command_wake(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        event_key = ensure_id(args.event_key, "event_key")
        event_path = normalize_project_path(project, args.event_path)
        event_file = project / event_path
        if event_file.is_symlink() or not event_file.is_file():
            raise GuardError("wake event file must be a regular file")
        digest = file_hash(event_file)
        seen = control["runtime"].get("seen_events", [])
        if any(item.get("event_key") == event_key and item.get("sha256") == digest for item in seen if isinstance(item, dict)):
            print("duplicate")
            return 0
        wait = control["runtime"].get("wait")
        if control["runtime"]["state"] != "WAITING_EXTERNAL_EVENT" or not isinstance(wait, dict):
            raise GuardError("runtime is not waiting for an external event")
        if wait.get("kind", "project_artifact") != "project_artifact":
            raise GuardError("registered wait requires wake-monitor, not project-artifact wake")
        if event_key != wait.get("event_key") or event_path != wait.get("event_path"):
            raise GuardError("wake event does not match the registered wait contract")
        if digest == wait.get("baseline_sha256"):
            raise GuardError("wake event is unchanged from the waiting baseline")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("waiting runtime lost its active lease")
        verify_frozen_lease(project, lease)
        artifact_contract = next((item for item in lease.get("checkpoint_artifacts", []) if item.get("path") == event_path), None)
        if not isinstance(artifact_contract, dict):
            raise GuardError("wake event lost its checkpoint artifact contract")
        lease["wake_event"] = {
            "event_key": event_key,
            "artifact_id": artifact_contract["id"],
            "path": event_path,
            "sha256": digest,
            "time": iso_time(utc_now()),
        }
        remaining = int(lease.pop("remaining_seconds", 0))
        lease.pop("suspended_at", None)
        lease["expires_at"] = iso_time(utc_now() + timedelta(seconds=max(1, remaining)))
        control["active_lease"] = lease
        seen.append({"event_key": event_key, "path": event_path, "sha256": digest, "time": iso_time(utc_now())})
        control["runtime"] = {"state": "ACTIVE", "wait": None, "seen_events": seen[-32:]}
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["runtime"], ensure_ascii=False, indent=2))
    return 0


def verify_external_monitor_results(project: Path, lease: dict[str, Any], raw: Any) -> list[dict[str, str]]:
    monitors = {item["id"]: item for item in lease.get("external_monitors", [])}
    if raw is None and not monitors:
        return []
    if not isinstance(raw, list):
        raise GuardError("external_monitor_results must be a list")
    receipts = lease.get("monitor_receipts") if isinstance(lease.get("monitor_receipts"), dict) else {}
    verified: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuardError(f"external_monitor_results[{index}] must be an object")
        monitor_id = ensure_id(item.get("id"), f"external_monitor_results[{index}].id")
        if monitor_id in seen or monitor_id not in monitors:
            raise GuardError(f"duplicate or unregistered external monitor result: {monitor_id}")
        seen.add(monitor_id)
        receipt = receipts.get(monitor_id) if isinstance(receipts, dict) else None
        if not isinstance(receipt, dict):
            raise GuardError(f"external monitor did not materialize a project receipt: {monitor_id}")
        path = normalize_project_path(project, item.get("path"))
        digest = ensure_sha256(item.get("sha256"), f"external_monitor_results[{index}].sha256")
        target = project / path
        if path != receipt.get("path") or digest != receipt.get("sha256"):
            raise GuardError(f"external monitor result drifted from the controller receipt: {monitor_id}")
        if target.is_symlink() or not target.is_file() or file_hash(target) != digest:
            raise GuardError(f"external monitor project receipt is missing or changed: {path}")
        verified.append({"id": monitor_id, "path": path, "sha256": digest})
    missing = sorted(monitor_id for monitor_id, monitor in monitors.items() if monitor.get("required") and monitor_id not in seen)
    if missing:
        raise GuardError(f"required external monitor results are missing: {missing}")
    return sorted(verified, key=lambda item: item["id"])


def validate_evaluation_summary(
    lease: dict[str, Any],
    result: dict[str, Any],
    artifact_results: list[dict[str, str]],
) -> dict[str, Any]:
    raw = result.get("evaluation_summary")
    if not isinstance(raw, dict):
        raise GuardError("schema-v3 results require evaluation_summary")
    contract = lease.get("evaluation_contract")
    if not isinstance(contract, dict):
        raise GuardError("schema-v3 lease lost its evaluation_contract")
    expected_attempts = raw.get("expected_attempts")
    completed_attempts = raw.get("completed_attempts")
    result_rows = raw.get("result_rows")
    for name, value in (
        ("expected_attempts", expected_attempts),
        ("completed_attempts", completed_attempts),
        ("result_rows", result_rows),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GuardError(f"evaluation_summary.{name} must be a nonnegative integer")
    if expected_attempts != contract["expected_attempts"]:
        raise GuardError("evaluation_summary.expected_attempts drifted from the proposal")
    evaluator = raw.get("evaluator_completion")
    completeness = raw.get("artifact_completeness")
    determinacy = raw.get("gate_determinacy")
    threshold = raw.get("threshold_result")
    guardrail = raw.get("guardrail_result")
    if evaluator not in EVALUATOR_STATES:
        raise GuardError(f"evaluation_summary.evaluator_completion must be one of {sorted(EVALUATOR_STATES)}")
    if completeness not in COMPLETENESS_STATES:
        raise GuardError(f"evaluation_summary.artifact_completeness must be one of {sorted(COMPLETENESS_STATES)}")
    if determinacy not in DETERMINACY_STATES:
        raise GuardError(f"evaluation_summary.gate_determinacy must be one of {sorted(DETERMINACY_STATES)}")
    if threshold not in EVALUATION_RESULTS or guardrail not in EVALUATION_RESULTS:
        raise GuardError("evaluation_summary threshold_result and guardrail_result must be pass, fail, or not_evaluated")
    present_ids = {item["id"] for item in artifact_results}
    artifacts_complete = set(contract["required_artifact_ids"]).issubset(present_ids)
    mechanically_incomplete = (
        evaluator != "complete"
        or completed_attempts != expected_attempts
        or result_rows < contract["minimum_result_rows"]
        or completeness != "complete"
        or not artifacts_complete
        or determinacy != "determinate"
    )
    if mechanically_incomplete:
        semantic_class = "evaluator_unavailable"
    elif guardrail == "fail":
        semantic_class = "guardrail_failed"
    elif threshold == "fail":
        semantic_class = "threshold_failed"
    elif threshold == "pass" and guardrail == "pass":
        semantic_class = "all_passed"
    else:
        semantic_class = "evaluator_unavailable"
    expected = lease["outcome_mapping"][semantic_class]
    if result.get("valid") is not expected["valid"] or result.get("evaluation_integrity") != expected["evaluation_integrity"]:
        raise GuardError(f"evaluation facts require valid={expected['valid']} and evaluation_integrity={expected['evaluation_integrity']}")
    if result.get("outcome") != expected["outcome"]:
        if semantic_class == "threshold_failed" and result.get("outcome") == "invalid":
            raise GuardError("a complete, determinate threshold failure is a valid negative; outcome=invalid is forbidden")
        raise GuardError(f"evaluation facts require outcome={expected['outcome']}")
    if result.get("decision") not in expected["decisions"]:
        raise GuardError(f"evaluation facts require decision in {expected['decisions']}")
    expected_core = semantic_class == "all_passed"
    if result.get("core_progress") is not expected_core:
        raise GuardError(f"evaluation facts require core_progress={expected_core}")
    return {
        "semantic_class": semantic_class,
        "expected_attempts": expected_attempts,
        "completed_attempts": completed_attempts,
        "result_rows": result_rows,
        "evaluator_completion": evaluator,
        "artifact_completeness": completeness,
        "gate_determinacy": determinacy,
        "threshold_result": threshold,
        "guardrail_result": guardrail,
        "required_artifacts_present": artifacts_complete,
    }


def available_continuation_paths(
    gate: dict[str, Any],
    control: dict[str, Any],
    lease: dict[str, Any],
    *,
    outcome: str,
    decision: str,
    semantic_class: str | None,
) -> list[dict[str, Any]]:
    allowed_kinds: set[str]
    if semantic_class == "evaluator_unavailable":
        allowed_kinds = {"recovery", "rollback", "switch"}
    elif semantic_class == "guardrail_failed":
        allowed_kinds = {"rollback"}
    elif semantic_class == "threshold_failed" or outcome in {"negative", "zero_progress", "inconclusive"}:
        allowed_kinds = {"rollback", "switch"}
    else:
        allowed_kinds = set()
    recovery_state = control.get("recovery") if isinstance(control.get("recovery"), dict) else {"used": 0}
    global_remaining = max(0, int(gate.get("max_recovery_attempts", 3)) - int(recovery_state.get("used", 0)))
    path_usage = control.get("recovery_path_usage") if isinstance(control.get("recovery_path_usage"), dict) else {}
    paths: list[dict[str, Any]] = []
    for raw_path in lease.get("recovery_paths", []):
        if not isinstance(raw_path, dict) or raw_path.get("kind") not in allowed_kinds:
            continue
        item = dict(raw_path)
        usage_key = f"{lease.get('chain_id')}:{item.get('id')}"
        used = int(path_usage.get(usage_key, 0))
        if used >= int(item.get("max_attempts", 1)):
            continue
        if semantic_class == "evaluator_unavailable" and item.get("kind") == "recovery" and global_remaining <= 0:
            continue
        item["usage_key"] = usage_key
        item["used"] = used
        paths.append(item)
    if lease.get("final_discriminator") and outcome != "positive" and isinstance(lease.get("next_paths"), dict):
        paths.append({
            "id": "final-other",
            "kind": "switch",
            "description": str(lease["next_paths"].get("other")),
            "max_attempts": 1,
            "write_once_output_root": None,
            "usage_key": f"{lease.get('chain_id')}:final-other",
        })
    if decision in {"SWITCH", "ROLLBACK"} and not any(item.get("kind") == decision.lower() for item in paths):
        paths.append({
            "id": f"decision-{decision.lower()}",
            "kind": decision.lower(),
            "description": f"continue the declared {decision.lower()} decision on a successor attempt",
            "max_attempts": 1,
            "write_once_output_root": None,
            "usage_key": f"{lease.get('chain_id')}:decision-{decision.lower()}",
        })
    remaining = global_remaining
    if semantic_class == "evaluator_unavailable" and remaining > 0 and not any(item.get("kind") == "recovery" for item in paths):
        paths.append({
            "id": "recover-execution",
            "kind": "recovery",
            "description": "repair the reversible execution/evaluator failure and run a fresh successor attempt",
            "max_attempts": remaining,
            "write_once_output_root": None,
            "usage_key": f"{lease.get('chain_id')}:recover-execution",
        })
    deduplicated: dict[str, dict[str, Any]] = {}
    for item in paths:
        deduplicated[str(item["id"])] = item
    return [deduplicated[key] for key in sorted(deduplicated)]


def build_blocking_proof(
    gate: dict[str, Any],
    control: dict[str, Any],
    *,
    safe_paths: list[dict[str, Any]],
    global_stopline_fired: bool = False,
) -> dict[str, Any]:
    recovery = control.get("recovery") if isinstance(control.get("recovery"), dict) else {"used": 0}
    used = int(recovery.get("used", 0))
    maximum = int(gate.get("max_recovery_attempts", 3))
    exhausted = used >= maximum
    open_chains = sorted(
        chain_id for chain_id, chain in control.get("chains", {}).items()
        if isinstance(chain, dict) and not chain.get("closed")
    )
    block_allowed = not safe_paths and not open_chains and (exhausted or global_stopline_fired)
    hard_reason = None
    if block_allowed:
        hard_reason = "global_stopline_fired" if global_stopline_fired else "all_paths_exhausted"
    proof = {
        "schema_version": "goal-guardrails.blocking-proof/v1",
        "block_allowed": block_allowed,
        "hard_reason": hard_reason,
        "safe_paths": safe_paths,
        "open_chains": open_chains,
        "recovery_budget": {"used": used, "maximum": maximum, "exhausted": exhausted},
        "global_stopline_fired": global_stopline_fired,
    }
    proof["sha256"] = f"sha256:{canonical_hash(proof)}"
    return proof


def append_checkpoint_record(project: Path, payload: dict[str, Any]) -> tuple[str, str, str]:
    body = dict(payload)
    identity = {
        "schema_version": body.get("schema_version"),
        "kind": body.get("kind"),
        "supersedes": body.get("supersedes"),
        "experiment_id": body.get("experiment_id"),
        "chain_id": body.get("chain_id"),
        "lease_id": body.get("lease_id"),
        "result_sha256": body.get("result_sha256"),
    }
    checkpoint_id = f"sha256:{canonical_hash(identity)}"
    body["checkpoint_id"] = checkpoint_id
    experiment_id = ensure_id(body.get("experiment_id"), "checkpoint experiment_id")
    receipt_rel = CHECKPOINTS_REL / experiment_id / f"{checkpoint_id.removeprefix('sha256:')}.json"
    receipt_path = project / receipt_rel
    if receipt_path.exists() or receipt_path.is_symlink():
        existing = load_json(receipt_path)
        stable_existing = {key: value for key, value in existing.items() if key != "recorded_at"}
        stable_body = {key: value for key, value in body.items() if key != "recorded_at"}
        if stable_existing != stable_body:
            raise GuardError("checkpoint receipt identity collision")
    else:
        atomic_json(receipt_path, body)
    return checkpoint_id, receipt_rel.as_posix(), file_hash(receipt_path)


def canonical_checkpoint_for_correction(
    project: Path,
    control: dict[str, Any],
    supersedes: str,
) -> dict[str, Any]:
    if isinstance(control.get("active_lease"), dict):
        raise GuardError("cannot correct a checkpoint while an experiment lease is active")
    last = control.get("last_checkpoint")
    if not isinstance(last, dict) or last.get("checkpoint_id") != supersedes:
        raise GuardError("correct-checkpoint may supersede only the latest canonical checkpoint")
    history = control.get("checkpoint_history")
    if not isinstance(history, list) or not history or history[-1].get("checkpoint_id") != supersedes:
        raise GuardError("checkpoint history does not identify the requested checkpoint as canonical latest")
    continuation = control.get("continuation")
    if not isinstance(continuation, dict) or continuation.get("source_checkpoint_id") != supersedes:
        raise GuardError("latest checkpoint continuation state is missing or no longer canonical")
    if continuation.get("state") == "CONSUMED" or continuation.get("consumed_by_experiment_id"):
        raise GuardError("cannot correct a checkpoint after its continuation was consumed by a successor")
    receipt_rel = normalize_project_path(project, last.get("checkpoint_path"))
    if not receipt_rel.startswith(f"{CHECKPOINTS_REL.as_posix()}/"):
        raise GuardError("latest checkpoint receipt path is outside the append-only checkpoint store")
    receipt_path = project / receipt_rel
    expected_sha256 = ensure_sha256(last.get("checkpoint_sha256"), "latest checkpoint receipt SHA-256")
    if receipt_path.is_symlink() or not receipt_path.is_file() or file_hash(receipt_path) != expected_sha256:
        raise GuardError("latest checkpoint receipt is missing or changed")
    receipt = load_json(receipt_path)
    if receipt.get("checkpoint_id") != supersedes:
        raise GuardError("latest checkpoint receipt identity does not match CONTROL.json")
    lease = receipt.get("lease_snapshot")
    chain_before = receipt.get("chain_before")
    chain_after = receipt.get("chain_after")
    if not isinstance(lease, dict) or not isinstance(chain_before, dict) or not isinstance(chain_after, dict):
        raise GuardError("latest checkpoint receipt lacks an immutable lease or chain snapshot")
    current_chain = control.get("chains", {}).get(receipt.get("chain_id"))
    if not isinstance(current_chain, dict) or canonical_hash(current_chain) != canonical_hash(chain_after):
        raise GuardError("chain state changed after the checkpoint and can no longer be corrected safely")
    return receipt


def validate_corrected_result(
    project: Path,
    lease: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    proposal_schema = lease.get("proposal_schema_version")
    structured = isinstance(proposal_schema, int) and proposal_schema >= 2
    expected_result_schema = proposal_schema if structured else 1
    if result.get("schema_version") != expected_result_schema or result.get("experiment_id") != lease.get("experiment_id"):
        raise GuardError("corrected result schema or experiment_id does not match the frozen lease")
    if type(result.get("valid")) is not bool or type(result.get("core_progress")) is not bool:
        raise GuardError("corrected result valid and core_progress must be strict booleans")
    decision = result.get("decision")
    outcome = result.get("outcome")
    if decision not in DECISIONS or outcome not in OUTCOMES:
        raise GuardError("corrected result decision or outcome is invalid")
    integrity = result.get("evaluation_integrity")
    if integrity not in {"PASS", "FAIL"} or result["valid"] is not (integrity == "PASS"):
        raise GuardError("corrected result valid must be true exactly when evaluation_integrity is PASS")
    preflight_failed = bool(lease.get("preflight_failed"))
    if preflight_failed and (
        result["valid"] is not False
        or integrity != "FAIL"
        or result["core_progress"] is not False
        or outcome != "invalid"
        or decision != "PAUSE_REQUIRED"
    ):
        raise GuardError("a corrected failed-preflight result must remain invalid and PAUSE_REQUIRED")

    artifact_results: list[dict[str, str]] = []
    gate_results: list[dict[str, str]] = []
    external_monitor_results: list[dict[str, str]] = []
    semantic_summary: dict[str, Any] | None = None
    if structured:
        artifact_results = verify_artifact_results(
            project,
            lease,
            result.get("artifact_results"),
            require_all=not preflight_failed and proposal_schema < 3,
        )
        gate_results = verify_gate_results(
            lease,
            result.get("pre_run_gate_results"),
            artifact_results,
            allow_required_fail=preflight_failed,
        )
        if preflight_failed:
            if result.get("external_monitor_results") not in (None, []):
                raise GuardError("failed preflight cannot claim external monitor results")
        else:
            external_monitor_results = verify_external_monitor_results(
                project, lease, result.get("external_monitor_results")
            )
        recorded_gates = lease.get("pre_run_gate_results")
        if lease.get("pre_run_gates") and not isinstance(recorded_gates, dict):
            raise GuardError("frozen required pre-run gates were never recorded")
        if isinstance(recorded_gates, dict) and canonical_hash(gate_results) != canonical_hash(recorded_gates.get("gates")):
            raise GuardError("corrected result pre-run gate evidence drifted from the frozen decision")
        if isinstance(recorded_gates, dict):
            current_artifacts = {item["id"]: item for item in artifact_results}
            for recorded in recorded_gates.get("artifacts", []):
                if current_artifacts.get(recorded["id"], {}).get("sha256") != recorded.get("sha256"):
                    raise GuardError("corrected result pre-run gate artifact evidence changed")
        wake_event = lease.get("wake_event")
        if isinstance(wake_event, dict):
            if wake_event.get("monitor_id"):
                wake_receipt = next(
                    (item for item in external_monitor_results if item["id"] == wake_event.get("monitor_id")), None
                )
                if not isinstance(wake_receipt, dict) or wake_receipt.get("path") != wake_event.get("path") or wake_receipt.get("sha256") != wake_event.get("sha256"):
                    raise GuardError("corrected result external monitor receipt does not match the frozen wake event")
            else:
                wake_artifact = next(
                    (item for item in artifact_results if item["id"] == wake_event.get("artifact_id")), None
                )
                if not isinstance(wake_artifact, dict) or wake_artifact.get("path") != wake_event.get("path") or wake_artifact.get("sha256") != wake_event.get("sha256"):
                    raise GuardError("corrected result terminal artifact does not match the frozen wake event")
        if proposal_schema == 3:
            semantic_summary = validate_evaluation_summary(lease, result, artifact_results)

    valid = result["valid"]
    core_progress = result["core_progress"]
    if not valid and outcome != "invalid":
        raise GuardError("failed corrected evaluation integrity requires outcome invalid")
    if valid and not core_progress and outcome == "positive":
        raise GuardError("corrected positive outcome requires core_progress")
    if valid and outcome == "invalid":
        raise GuardError("a valid corrected evaluation cannot have outcome invalid")
    if decision == "COMPLETE" and (not core_progress or outcome != "positive"):
        raise GuardError("corrected COMPLETE requires positive core progress")
    if lease.get("final_discriminator") and decision in {"CONTINUE", "REPLICATE"}:
        raise GuardError("a corrected final discriminator must close or switch the diagnostic chain")
    ensure_text(result.get("metric_delta"), "metric_delta")
    primary_artifact = ensure_text(result.get("artifact"), "artifact")
    if structured:
        primary_artifact = normalize_project_path(project, primary_artifact)
        if primary_artifact not in {item["path"] for item in artifact_results}:
            raise GuardError("corrected primary artifact is not a verified preregistered checkpoint artifact")
    return {
        "artifact_results": artifact_results,
        "pre_run_gate_results": gate_results,
        "external_monitor_results": external_monitor_results,
        "semantic_summary": semantic_summary,
        "primary_artifact": primary_artifact,
        "preflight_failed": preflight_failed,
    }


def restore_recovery_before_checkpoint(
    control: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    recovery = json.loads(json.dumps(control.get("recovery", {"used": 0, "history": []})))
    original_summary = receipt.get("semantic_summary")
    original_invalid = (
        isinstance(original_summary, dict) and original_summary.get("semantic_class") == "evaluator_unavailable"
    ) or (
        original_summary is None and receipt.get("raw_result", {}).get("outcome") == "invalid"
    )
    if not original_invalid:
        return recovery
    history = recovery.get("history") if isinstance(recovery.get("history"), list) else []
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if (
            isinstance(item, dict)
            and item.get("experiment_id") == receipt.get("experiment_id")
            and item.get("chain_id") == receipt.get("chain_id")
            and item.get("kind") == "evaluation_or_execution_recovery"
        ):
            del history[index]
            recovery["used"] = max(0, int(recovery.get("used", 0)) - 1)
            break
    recovery["history"] = history
    return recovery


def corrected_chain_state(
    gate: dict[str, Any],
    lease: dict[str, Any],
    result: dict[str, Any],
    chain_before: dict[str, Any],
) -> dict[str, Any]:
    chain = json.loads(json.dumps(chain_before))
    valid = result["valid"]
    core_progress = result["core_progress"]
    decision = result["decision"]
    outcome = result["outcome"]
    if valid and core_progress:
        chain["no_progress_count"] = 0
    elif valid:
        chain["no_progress_count"] = int(chain.get("no_progress_count", 0)) + 1
    if int(chain.get("no_progress_count", 0)) >= int(gate["max_consecutive_no_progress"]):
        chain["stopline_fired"] = True
        if decision in {"CONTINUE", "REPLICATE"}:
            raise GuardError("corrected result fires the no-progress stop line and must switch, rollback, pause, or complete")
    preflight_failed = bool(lease.get("preflight_failed"))
    closes = False if preflight_failed else bool(lease.get("final_discriminator")) or decision in {"SWITCH", "ROLLBACK", "COMPLETE"}
    if not preflight_failed and decision == "PAUSE_REQUIRED" and outcome != "invalid" and not chain.get("stopline_fired"):
        closes = True
    if chain.get("chain_kind") == "verification" and chain.get("stopline_fired"):
        closes = True
    if closes:
        chain["closed"] = True
        chain["close_outcome"] = outcome
    else:
        chain["closed"] = False
        chain["close_outcome"] = None
    return chain


def command_correct_checkpoint(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    result_path = Path(args.result).resolve()
    if result_path != (project / RESULT_REL).resolve():
        raise GuardError("correct-checkpoint must use optimization/RESULT.json from the guarded project")
    result = load_json(result_path)
    result_sha256 = file_hash(result_path)
    supersedes = ensure_prefixed_sha256(args.supersedes, "superseded checkpoint ID")
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        receipt = canonical_checkpoint_for_correction(project, control, supersedes)
        if receipt.get("result_sha256") == result_sha256:
            print("duplicate")
            return 0
        lease = receipt["lease_snapshot"]
        verified = validate_corrected_result(project, lease, result)
        for field in ("artifact_results", "pre_run_gate_results", "external_monitor_results"):
            if canonical_hash(verified[field]) != canonical_hash(receipt.get(field, [])):
                raise GuardError(f"correct-checkpoint cannot change frozen {field} evidence")
        chain = corrected_chain_state(gate, lease, result, receipt["chain_before"])
        control["chains"][lease["chain_id"]] = chain
        control["recovery"] = restore_recovery_before_checkpoint(control, receipt)
        semantic_summary = verified["semantic_summary"]
        semantic_class = semantic_summary.get("semantic_class") if isinstance(semantic_summary, dict) else (
            "evaluator_unavailable" if result["outcome"] == "invalid" else None
        )
        correction_time = iso_time(utc_now())
        if semantic_class == "evaluator_unavailable":
            recovery = control["recovery"]
            recovery["used"] = int(recovery.get("used", 0)) + 1
            recovery.setdefault("history", []).append({
                "experiment_id": lease["experiment_id"],
                "chain_id": lease["chain_id"],
                "kind": "evaluation_or_execution_recovery",
                "time": correction_time,
                "corrects": supersedes,
            })
            recovery["history"] = recovery["history"][-100:]
        safe_paths = available_continuation_paths(
            gate,
            control,
            lease,
            outcome=result["outcome"],
            decision=result["decision"],
            semantic_class=semantic_class,
        )
        if semantic_class == "evaluator_unavailable" and not safe_paths:
            chain["closed"] = True
            chain["close_outcome"] = "invalid"
        blocking_proof = build_blocking_proof(
            gate,
            control,
            safe_paths=safe_paths,
            global_stopline_fired=bool(chain.get("stopline_fired") and not safe_paths),
        )
        record_payload = {
            "schema_version": "goal-guardrails.checkpoint/v1",
            "kind": "correction",
            "supersedes": supersedes,
            "superseded_checkpoint_sha256": ensure_sha256(
                control["last_checkpoint"]["checkpoint_sha256"], "superseded checkpoint receipt SHA-256"
            ),
            "experiment_id": lease["experiment_id"],
            "chain_id": lease["chain_id"],
            "lease_id": lease["lease_id"],
            "recorded_at": correction_time,
            "result_sha256": result_sha256,
            "raw_result": result,
            "semantic_summary": semantic_summary,
            "blocking_proof": blocking_proof,
            "lease_snapshot": lease,
            "chain_before": receipt["chain_before"],
            "chain_after": chain,
            "artifact_results": verified["artifact_results"],
            "pre_run_gate_results": verified["pre_run_gate_results"],
            "external_monitor_results": verified["external_monitor_results"],
        }
        checkpoint_id, checkpoint_path, checkpoint_sha256 = append_checkpoint_record(project, record_payload)
        output_root = lease.get("write_once_output_root")
        if isinstance(output_root, str) and output_root not in control.setdefault("consumed_output_roots", []):
            control["consumed_output_roots"].append(output_root)
        control["continuation"] = {
            "source_checkpoint_id": checkpoint_id,
            "source_experiment_id": lease["experiment_id"],
            "proposal_schema_version": lease.get("proposal_schema_version"),
            "paths": safe_paths,
            "state": "AVAILABLE" if safe_paths else "NONE",
        }
        control["last_checkpoint"] = {
            "experiment_id": lease["experiment_id"],
            "chain_id": lease["chain_id"],
            "decision": result["decision"],
            "outcome": result["outcome"],
            "valid": result["valid"],
            "evaluation_integrity": result["evaluation_integrity"],
            "core_progress": result["core_progress"],
            "preflight_failed": verified["preflight_failed"],
            "time": correction_time,
            "artifact": verified["primary_artifact"],
            "artifact_results": verified["artifact_results"],
            "pre_run_gate_results": verified["pre_run_gate_results"],
            "external_monitor_results": verified["external_monitor_results"],
            "semantic_summary": semantic_summary,
            "blocking_proof": blocking_proof,
            "checkpoint_id": checkpoint_id,
            "checkpoint_path": checkpoint_path,
            "checkpoint_sha256": checkpoint_sha256,
            "result_sha256": result_sha256,
            "lease_id": lease["lease_id"],
            "supersedes": supersedes,
        }
        history = control.setdefault("checkpoint_history", [])
        history.append({
            "checkpoint_id": checkpoint_id,
            "path": checkpoint_path,
            "sha256": checkpoint_sha256,
            "experiment_id": lease["experiment_id"],
            "supersedes": supersedes,
        })
        control["checkpoint_history"] = history[-256:]
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["last_checkpoint"], ensure_ascii=False, indent=2))
    return 0


def command_checkpoint(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    result_path = Path(args.result).resolve()
    if result_path != (project / RESULT_REL).resolve():
        raise GuardError("checkpoint must use optimization/RESULT.json from the guarded project")
    result = load_json(result_path)
    result_sha256 = file_hash(result_path)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if external_wait_in_progress(control):
            raise GuardError("wake the registered external event before checkpoint")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            last = control.get("last_checkpoint")
            if isinstance(last, dict) and last.get("result_sha256") == result_sha256:
                print("duplicate")
                return 0
            raise GuardError("no active lease to checkpoint; a changed result requires correct-checkpoint")
        verify_frozen_lease(project, lease)
        proposal_schema = lease.get("proposal_schema_version")
        structured = isinstance(proposal_schema, int) and proposal_schema >= 2
        expected_result_schema = proposal_schema if structured else 1
        if result.get("schema_version") != expected_result_schema or result.get("experiment_id") != lease["experiment_id"]:
            raise GuardError("result schema or experiment_id does not match active lease")
        decision = result.get("decision")
        outcome = result.get("outcome")
        if decision not in DECISIONS or outcome not in OUTCOMES:
            raise GuardError("result decision or outcome is invalid")
        preflight_failed = bool(lease.get("preflight_failed"))
        if preflight_failed and (
            result.get("valid") is not False
            or result.get("evaluation_integrity") != "FAIL"
            or result.get("core_progress") is not False
            or outcome != "invalid"
            or decision != "PAUSE_REQUIRED"
        ):
            raise GuardError(
                "failed required preflight requires valid=false, evaluation_integrity=FAIL, "
                "core_progress=false, outcome=invalid, and decision=PAUSE_REQUIRED"
            )
        if type(result.get("valid")) is not bool or type(result.get("core_progress")) is not bool:
            raise GuardError("result valid and core_progress must be strict booleans")
        integrity = result.get("evaluation_integrity")
        if integrity not in {"PASS", "FAIL"}:
            raise GuardError("evaluation_integrity must be PASS or FAIL")
        if result["valid"] is not (integrity == "PASS"):
            raise GuardError("valid must be true exactly when evaluation_integrity is PASS")
        valid = result["valid"]
        core_progress = result["core_progress"]
        artifact_results: list[dict[str, str]] = []
        gate_results: list[dict[str, str]] = []
        external_monitor_results: list[dict[str, str]] = []
        semantic_summary: dict[str, Any] | None = None
        if structured:
            artifact_results = verify_artifact_results(
                project,
                lease,
                result.get("artifact_results"),
                require_all=not preflight_failed and proposal_schema < 3,
            )
            gate_results = verify_gate_results(
                lease, result.get("pre_run_gate_results"), artifact_results, allow_required_fail=preflight_failed,
            )
            if preflight_failed:
                if result.get("external_monitor_results") not in (None, []):
                    raise GuardError("failed preflight cannot claim external monitor results")
            else:
                external_monitor_results = verify_external_monitor_results(project, lease, result.get("external_monitor_results"))
            recorded_gates = lease.get("pre_run_gate_results")
            if lease.get("pre_run_gates") and not isinstance(recorded_gates, dict):
                raise GuardError("required pre-run gates were not recorded before workload execution")
            if isinstance(recorded_gates, dict) and canonical_hash(gate_results) != canonical_hash(recorded_gates.get("gates")):
                raise GuardError("checkpoint pre-run gate results drifted from the recorded gate decision")
            if isinstance(recorded_gates, dict):
                current_artifacts = {item["id"]: item for item in artifact_results}
                for recorded in recorded_gates.get("artifacts", []):
                    if current_artifacts.get(recorded["id"], {}).get("sha256") != recorded.get("sha256"):
                        raise GuardError("pre-run gate evidence changed after gate recording")
            wake_event = lease.get("wake_event")
            if isinstance(wake_event, dict):
                if wake_event.get("monitor_id"):
                    wake_receipt = next((item for item in external_monitor_results if item["id"] == wake_event.get("monitor_id")), None)
                    if not isinstance(wake_receipt, dict) or wake_receipt.get("path") != wake_event.get("path") or wake_receipt.get("sha256") != wake_event.get("sha256"):
                        raise GuardError("checkpoint external monitor receipt does not match the frozen wake event")
                else:
                    wake_artifact = next((item for item in artifact_results if item["id"] == wake_event.get("artifact_id")), None)
                    if not isinstance(wake_artifact, dict) or wake_artifact.get("path") != wake_event.get("path") or wake_artifact.get("sha256") != wake_event.get("sha256"):
                        raise GuardError("checkpoint terminal artifact does not match the frozen wake event")
            if proposal_schema == 3:
                semantic_summary = validate_evaluation_summary(lease, result, artifact_results)
        if not valid and outcome != "invalid":
            raise GuardError("failed evaluation integrity requires outcome invalid")
        if valid and not core_progress and outcome == "positive":
            raise GuardError("positive outcome requires core_progress")
        if valid and outcome == "invalid":
            raise GuardError("a valid evaluation cannot have outcome invalid")
        if decision == "COMPLETE" and (not core_progress or outcome != "positive"):
            raise GuardError("COMPLETE requires positive core progress")
        if lease.get("final_discriminator") and decision in {"CONTINUE", "REPLICATE"}:
            raise GuardError("a final discriminator must close or switch the diagnostic chain")
        ensure_text(result.get("metric_delta"), "metric_delta")
        primary_artifact = ensure_text(result.get("artifact"), "artifact")
        if structured:
            primary_artifact = normalize_project_path(project, primary_artifact)
        if structured and primary_artifact not in {item["path"] for item in artifact_results}:
            raise GuardError("primary artifact is not a verified preregistered checkpoint artifact")
        chain = control["chains"][lease["chain_id"]]
        chain_before = json.loads(json.dumps(chain))
        if valid and core_progress:
            chain["no_progress_count"] = 0
        elif valid:
            chain["no_progress_count"] = int(chain.get("no_progress_count", 0)) + 1
        if chain["no_progress_count"] >= int(gate["max_consecutive_no_progress"]):
            chain["stopline_fired"] = True
            if decision in {"CONTINUE", "REPLICATE"}:
                raise GuardError("no-progress stop line fired; decision must switch, rollback, pause, or complete")
        closes = False if preflight_failed else bool(lease["final_discriminator"]) or decision in {"SWITCH", "ROLLBACK", "COMPLETE"}
        if not preflight_failed and decision == "PAUSE_REQUIRED" and outcome != "invalid" and not chain.get("stopline_fired"):
            closes = True
        if chain.get("chain_kind") == "verification" and chain.get("stopline_fired"):
            closes = True
        if closes:
            chain["closed"] = True
            chain["close_outcome"] = outcome
        semantic_class = semantic_summary.get("semantic_class") if isinstance(semantic_summary, dict) else (
            "evaluator_unavailable" if outcome == "invalid" else None
        )
        if semantic_class == "evaluator_unavailable":
            recovery = control.setdefault("recovery", {"used": 0, "history": []})
            recovery["used"] = int(recovery.get("used", 0)) + 1
            recovery.setdefault("history", []).append({
                "experiment_id": lease["experiment_id"],
                "chain_id": lease["chain_id"],
                "kind": "evaluation_or_execution_recovery",
                "time": iso_time(utc_now()),
            })
            recovery["history"] = recovery["history"][-100:]
        safe_paths = available_continuation_paths(
            gate,
            control,
            lease,
            outcome=outcome,
            decision=decision,
            semantic_class=semantic_class,
        )
        if semantic_class == "evaluator_unavailable" and not safe_paths:
            chain["closed"] = True
            chain["close_outcome"] = "invalid"
        blocking_proof = build_blocking_proof(
            gate,
            control,
            safe_paths=safe_paths,
            global_stopline_fired=bool(chain.get("stopline_fired") and not safe_paths),
        )
        checkpoint_time = iso_time(utc_now())
        record_payload = {
            "schema_version": "goal-guardrails.checkpoint/v1",
            "kind": "original",
            "supersedes": None,
            "experiment_id": lease["experiment_id"],
            "chain_id": lease["chain_id"],
            "lease_id": lease["lease_id"],
            "recorded_at": checkpoint_time,
            "result_sha256": result_sha256,
            "raw_result": result,
            "semantic_summary": semantic_summary,
            "blocking_proof": blocking_proof,
            "lease_snapshot": lease,
            "chain_before": chain_before,
            "chain_after": chain,
            "artifact_results": artifact_results,
            "pre_run_gate_results": gate_results,
            "external_monitor_results": external_monitor_results,
        }
        checkpoint_id, checkpoint_path, checkpoint_sha256 = append_checkpoint_record(project, record_payload)
        output_root = lease.get("write_once_output_root")
        if isinstance(output_root, str) and output_root not in control.setdefault("consumed_output_roots", []):
            control["consumed_output_roots"].append(output_root)
        control["continuation"] = {
            "source_checkpoint_id": checkpoint_id,
            "source_experiment_id": lease["experiment_id"],
            "proposal_schema_version": lease.get("proposal_schema_version"),
            "paths": safe_paths,
            "state": "AVAILABLE" if safe_paths else "NONE",
        }
        control["last_checkpoint"] = {
            "experiment_id": lease["experiment_id"], "chain_id": lease["chain_id"],
            "decision": decision, "outcome": outcome, "core_progress": core_progress,
            "valid": valid, "evaluation_integrity": integrity,
            "preflight_failed": preflight_failed,
            "time": checkpoint_time, "artifact": primary_artifact,
            "artifact_results": artifact_results, "pre_run_gate_results": gate_results,
            "external_monitor_results": external_monitor_results,
            "semantic_summary": semantic_summary,
            "blocking_proof": blocking_proof,
            "checkpoint_id": checkpoint_id,
            "checkpoint_path": checkpoint_path,
            "checkpoint_sha256": checkpoint_sha256,
            "result_sha256": result_sha256,
            "lease_id": lease["lease_id"],
        }
        history = control.setdefault("checkpoint_history", [])
        history.append({
            "checkpoint_id": checkpoint_id,
            "path": checkpoint_path,
            "sha256": checkpoint_sha256,
            "experiment_id": lease["experiment_id"],
            "supersedes": None,
        })
        control["checkpoint_history"] = history[-256:]
        control["active_lease"] = None
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["last_checkpoint"], ensure_ascii=False, indent=2))
    return 0


def command_abort_preflight(args: argparse.Namespace) -> int:
    """Materialize and checkpoint the only legal result after a frozen required-gate failure."""
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if external_wait_in_progress(control):
            raise GuardError("cannot abort preflight while waiting for an external event")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("no active lease to abort")
        verify_frozen_lease(project, lease)
        recorded = lease.get("pre_run_gate_results")
        if not lease.get("preflight_failed") or not isinstance(recorded, dict):
            raise GuardError("abort is available only after a frozen required preflight FAIL")
        artifacts = recorded.get("artifacts")
        gates = recorded.get("gates")
        if not isinstance(artifacts, list) or not isinstance(gates, list):
            raise GuardError("frozen preflight failure evidence is incomplete")
        artifact_by_id = {
            item.get("id"): item
            for item in artifacts
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        required_gate_ids = {
            item.get("id") for item in lease.get("pre_run_gates", [])
            if isinstance(item, dict) and item.get("required")
        }
        failed_artifact_ids = [
            item.get("artifact_id")
            for item in gates
            if isinstance(item, dict) and item.get("id") in required_gate_ids and item.get("status") == "FAIL"
        ]
        primary = next(
            (artifact_by_id[artifact_id] for artifact_id in failed_artifact_ids if artifact_id in artifact_by_id),
            None,
        )
        if not isinstance(primary, dict):
            raise GuardError("frozen required preflight FAIL lacks a verified primary artifact")
        result = {
            "schema_version": lease.get("proposal_schema_version", 1),
            "experiment_id": lease["experiment_id"],
            "valid": False,
            "evaluation_integrity": "FAIL",
            "core_progress": False,
            "metric_delta": "preflight failed before workload",
            "outcome": "invalid",
            "decision": "PAUSE_REQUIRED",
            "artifact": primary["path"],
            "artifact_results": artifacts,
            "pre_run_gate_results": gates,
            "external_monitor_results": [],
        }
        if lease.get("proposal_schema_version") == 3:
            contract = lease.get("evaluation_contract", {})
            result["evaluation_summary"] = {
                "expected_attempts": contract.get("expected_attempts", 1),
                "completed_attempts": 0,
                "result_rows": 0,
                "evaluator_completion": "missing",
                "artifact_completeness": "incomplete",
                "gate_determinacy": "indeterminate",
                "threshold_result": "not_evaluated",
                "guardrail_result": "not_evaluated",
            }
        atomic_json(project / RESULT_REL, result)
    return command_checkpoint(argparse.Namespace(project=str(project), result=str(project / RESULT_REL)))


def command_review_subject(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    proposal_path = Path(args.proposal).resolve()
    if proposal_path != (project / PROPOSAL_REL).resolve():
        raise GuardError("review subject must use optimization/PROPOSAL.json from the guarded project")
    proposal = load_json(proposal_path)
    with state_lock(project):
        gate = load_gate(project)
        control = load_control(project)
        subject_sha256 = review_subject_sha256(
            project,
            proposal,
            profile=gate_profile(gate),
            review_epoch=int(control.get("review_epoch", 0)),
        )
    print(subject_sha256)
    return 0


def release_blockers(lease: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if int(lease.get("mutations_used", 0)) != 0:
        blockers.append("mutations_used")
    for field in ("binding_values", "policy_runs", "transport_doctors", "monitor_receipts"):
        if lease.get(field):
            blockers.append(field)
    if lease.get("pre_run_gate_results") is not None:
        blockers.append("pre_run_gate_results")
    for field in ("preflight_failed", "finalization_used", "wake_event", "suspended_at", "remaining_seconds"):
        if lease.get(field):
            blockers.append(field)
    return blockers


def command_release_lease(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    expected_proposal_sha256 = normalize_sha256(
        args.expected_proposal_sha256, "expected proposal SHA-256"
    )
    reason = ensure_text(args.reason, "reason")
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control.get("runtime", {}).get("state") != "ACTIVE":
            raise GuardError("cannot release a lease while waiting for an external event")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("there is no active lease to release")
        if lease.get("proposal_sha256") != expected_proposal_sha256:
            raise GuardError("expected proposal SHA-256 does not match the active lease")
        blockers = release_blockers(lease)
        if blockers:
            raise GuardError(f"lease authority was already consumed and cannot be released: {sorted(blockers)}")
        chain = control.get("chains", {}).get(lease.get("chain_id"))
        if isinstance(chain, dict):
            if lease.get("work_class") == "non_core":
                chain["non_core_cost_units"] = max(
                    0, int(chain.get("non_core_cost_units", 0)) - int(lease.get("cost_units", 0))
                )
            if lease.get("final_discriminator"):
                chain["final_discriminator_used"] = False
        continuation = control.get("continuation")
        binding = lease.get("continuation_binding")
        if (
            isinstance(continuation, dict)
            and isinstance(binding, dict)
            and continuation.get("state") == "CONSUMED"
            and continuation.get("source_checkpoint_id") == binding.get("source_checkpoint_id")
            and continuation.get("consumed_by_experiment_id") == lease.get("experiment_id")
        ):
            usage_key = binding.get("usage_key")
            if isinstance(usage_key, str):
                usage = control.setdefault("recovery_path_usage", {})
                if int(usage.get(usage_key, 0)) > 0:
                    usage[usage_key] = int(usage[usage_key]) - 1
                    if usage[usage_key] == 0:
                        usage.pop(usage_key)
            continuation["state"] = "AVAILABLE"
            for field in ("consumed_by_experiment_id", "consumed_path_id", "consumed_at"):
                continuation.pop(field, None)
            control["continuation"] = continuation
        previous_review_epoch = int(control.get("review_epoch", 0))
        control["review_epoch"] = previous_review_epoch + 1
        control["last_lease_release"] = {
            "schema_version": 1,
            "lease_id": lease.get("lease_id"),
            "experiment_id": lease.get("experiment_id"),
            "chain_id": lease.get("chain_id"),
            "proposal_sha256": lease.get("proposal_sha256"),
            "review_subject_sha256": lease.get("review_subject_sha256"),
            "review_attestation_sha256": canonical_hash(lease.get("review")),
            "previous_review_epoch": previous_review_epoch,
            "next_review_epoch": control["review_epoch"],
            "reason": reason,
            "released_at": iso_time(utc_now()),
        }
        control["active_lease"] = None
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["last_lease_release"], ensure_ascii=False, indent=2))
    return 0


def command_update_goal(args: argparse.Namespace) -> int:
    if args.approved_by != "user":
        raise GuardError("goal updates require --approved-by user")
    project = explicit_project(args.project)
    source_lexical = Path(os.path.abspath(os.fspath(Path(args.from_file).expanduser())))
    if source_lexical.is_symlink() or not source_lexical.is_file():
        raise GuardError("goal update source must be a regular non-symbolic-link file")
    if source_lexical.resolve() == (project / GOAL_REL).resolve():
        raise GuardError("goal update source must be a separate staging file")
    payload = source_lexical.read_bytes()
    if len(payload) > 1024 * 1024:
        raise GuardError("goal update source exceeds the 1 MiB limit")
    try:
        new_text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GuardError("goal update source must be UTF-8") from error
    if not new_text.strip():
        raise GuardError("goal update source cannot be empty")
    expected_sha256 = normalize_sha256(args.expected_sha256, "expected GOAL SHA-256")
    reason = ensure_text(args.reason, "reason")
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control.get("runtime", {}).get("state") != "ACTIVE":
            raise GuardError("cannot update GOAL.md while waiting for an external event")
        if isinstance(control.get("active_lease"), dict):
            raise GuardError("cannot update GOAL.md while an experiment lease is active")
        goal_path = project / GOAL_REL
        if goal_path.is_symlink() or not goal_path.is_file():
            raise GuardError("GOAL.md is missing or unsafe")
        old_sha256 = file_hash(goal_path)
        if old_sha256 != expected_sha256:
            raise GuardError("expected GOAL SHA-256 does not match the current file")
        new_sha256 = hashlib.sha256(payload).hexdigest()
        if new_sha256 == old_sha256:
            raise GuardError("goal update does not change GOAL.md")
        previous = control.get("goal") if isinstance(control.get("goal"), dict) else {}
        metadata = {
            "schema_version": 1,
            "revision": int(previous.get("revision", 0)) + 1,
            "sha256": new_sha256,
            "previous_sha256": old_sha256,
            "approved_by": "user",
            "reason": reason,
            "updated_at": iso_time(utc_now()),
        }
        transaction = {
            "schema_version": 1,
            "transaction_id": uuid.uuid4().hex,
            "state": "PREPARED",
            "old_sha256": old_sha256,
            "new_sha256": new_sha256,
            "goal_metadata": metadata,
            "prepared_at": iso_time(utc_now()),
        }
        transaction_path = project / GOAL_UPDATE_REL
        atomic_json(transaction_path, transaction)
        atomic_text(goal_path, new_text)
        control["goal"] = metadata
        save_control(project, control)
        transaction["state"] = "COMMITTED"
        transaction["committed_at"] = iso_time(utc_now())
        atomic_json(transaction_path, transaction)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


def command_toggle(args: argparse.Namespace, enabled: bool) -> int:
    if args.approved_by != "user":
        raise GuardError("activation changes require --approved-by user")
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        if enabled:
            if gate_profile(gate) == "strict" and state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
                raise GuardError("STATE.md exceeds its cap")
            if not (project / GOAL_REL).is_file():
                raise GuardError("GOAL.md is missing")
        gate["enabled"] = enabled
        gate["updated_at"] = iso_time(utc_now())
        if not enabled:
            gate["disabled_reason"] = ensure_text(args.reason, "reason")
        atomic_json(project / GATE_REL, gate)
        if not (project / CONTROL_REL).exists():
            save_control(project, default_control())
    print("enabled" if enabled else "disabled")
    return 0


def command_mode(args: argparse.Namespace) -> int:
    if args.approved_by != "user":
        raise GuardError("profile changes require --approved-by user")
    project = explicit_project(args.project)
    if args.profile not in {"fast", "strict"}:
        raise GuardError("profile must be fast or strict")
    with state_lock(project):
        gate = load_gate(project)
        gate["profile"] = args.profile
        gate["updated_at"] = iso_time(utc_now())
        atomic_json(project / GATE_REL, gate)
    print(args.profile)
    return 0


def external_wait_delivery(control: dict[str, Any]) -> dict[str, Any] | None:
    runtime = control.get("runtime")
    wait = runtime.get("wait") if isinstance(runtime, dict) else None
    lease = control.get("active_lease")
    if not isinstance(wait, dict) or wait.get("kind") != "external_monitor" or not isinstance(lease, dict):
        return None
    monitor = next(
        (item for item in lease.get("external_monitors", []) if item.get("id") == wait.get("monitor_id")),
        None,
    )
    if not isinstance(monitor, dict):
        return {"state": "contract_error", "detail": "registered external monitor is missing"}
    root = Path(monitor["state_root"])
    run_dir = root / "supervisors" / f"{monitor['host']}-{wait.get('job_id')}" / "runs" / str(wait.get("run_id"))
    publication_path = run_dir / "semantic_event.json"
    if not publication_path.exists():
        return {"state": "pending", "event_id": None}
    event_id: str | None = None
    try:
        publication = load_owned_external_json(publication_path, root)
        event_id = ensure_prefixed_sha256(publication.get("event_id"), "external semantic event ID")
        delivery_path = root / "outbox" / event_id.removeprefix("sha256:") / "delivery.json"
        if not delivery_path.exists():
            return {"state": "pending", "event_id": event_id}
        delivery = load_owned_external_json(delivery_path, root)
        state = delivery.get("state")
        if (
            delivery.get("schema") != "codex-monitor.delivery/v1"
            or delivery.get("event_id") != event_id
            or state not in {"pending", "leased", "delivered", "dead_letter"}
        ):
            raise GuardError("external monitor delivery record is invalid")
        result = {"state": state, "event_id": event_id, "attempts": delivery.get("attempts")}
        last_error = delivery.get("last_error")
        if isinstance(last_error, dict):
            result["last_error"] = {
                "code": last_error.get("code"),
                "safe_message": last_error.get("safe_message"),
            }
        return result
    except GuardError as error:
        return {"state": "contract_error", "detail": str(error), "event_id": event_id}


def recommended_next_action(gate: dict[str, Any], control: dict[str, Any]) -> dict[str, str]:
    if not gate.get("enabled"):
        return {"kind": "ACTIVATE", "instruction": "Obtain explicit user approval and activate the project gate."}
    runtime = control.get("runtime", {})
    if runtime.get("state") == "ARMING_EXTERNAL_WAIT":
        wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else {}
        monitor_id = wait.get("monitor_id") or "<id>"
        return {
            "kind": "RETRY_WAIT_MONITOR",
            "instruction": (
                f"Re-run goal_guard.py wait-monitor --monitor {monitor_id} --project . to reconcile the same "
                "scheduler gate. Never resubmit the consumed workload or create a new monitor run."
            ),
        }
    if runtime.get("state") == "WAITING_EXTERNAL_EVENT":
        wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else {}
        command = "wake-monitor" if wait.get("kind") == "external_monitor" else "wake"
        delivery = external_wait_delivery(control)
        reconciliation = runtime.get("external_reconciliation") if isinstance(runtime.get("external_reconciliation"), dict) else {}
        if reconciliation.get("state") == "recoverable_transport_error" and (
            not isinstance(delivery, dict) or reconciliation.get("event_id") == delivery.get("event_id")
        ):
            return {
                "kind": "RECOVER_TRANSPORT",
                "instruction": "The durable event exists but terminal verification needs a reversible transport repair. Continue autonomously within recovery budget; never resubmit the consumed Job.",
            }
        if isinstance(delivery, dict) and delivery.get("event_id"):
            return {
                "kind": "WAKE_MONITOR",
                "instruction": f"Consume durable semantic event {delivery['event_id']} with {command}; notification delivery state has no business effect.",
            }
        if isinstance(delivery, dict) and delivery.get("state") == "contract_error":
            return {
                "kind": "RECOVER_TRANSPORT",
                "instruction": "Repair or retry the reversible terminal/notification transport within the recovery budget; do not pause the business Goal and never resubmit the consumed Job.",
            }
        if gate_profile(gate) == "fast":
            return {
                "kind": "WAIT",
                "instruction": (
                    f"No durable {command} event exists yet. End this activation without model polling; "
                    "the managed monitor will resume the exact thread; do not ask the user or mark the Goal blocked/complete."
                ),
            }
        return {"kind": "WAIT", "instruction": f"End this activation without polling; resume only through the registered {command} event."}
    raw_lease = control.get("active_lease")
    lease = raw_lease if isinstance(raw_lease, dict) else None
    if lease is not None:
        if lease.get("preflight_failed"):
            return {"kind": "ABORT_PREFLIGHT", "instruction": "Run goal_guard.py abort --project .; it will freeze the invalid checkpoint and release the lease."}
        uncertain = next(
            (
                policy_id for policy_id, binding in lease.get("binding_values", {}).items()
                if isinstance(binding, dict) and binding.get("state") == "UNCERTAIN"
            ),
            None,
        )
        if uncertain is not None:
            source_policy = lease["binding_values"][uncertain].get("source_policy_id")
            return {"kind": "RECONCILE_BIND", "instruction": f"Run goal_guard.py reconcile-bind --policy {source_policy} --project .; never resubmit the consumed policy."}
        if current_lease(control) is None:
            return {"kind": "CHECKPOINT", "instruction": "The lease expired; stage or correct RESULT.json and checkpoint instead of abandoning the Goal."}
        if int(lease.get("mutations_used", 0)) >= int(lease.get("max_mutations", 0)):
            return {"kind": "CHECKPOINT", "instruction": "The mutation allowance is exhausted; stage or correct RESULT.json and checkpoint."}
        remote_policy = next(
            (
                policy for policy in lease.get("bash_policies", [])
                if policy.get("transport", {}).get("kind") == "ssh-helper-v1"
                and policy.get("id") not in lease.get("policy_runs", {})
                and lease.get("transport_doctors", {}).get(policy.get("id"), {}).get("state") != "READY"
            ),
            None,
        )
        if isinstance(remote_policy, dict):
            return {"kind": "DOCTOR", "instruction": f"Run goal_guard.py doctor --policy {remote_policy['id']} --project . before the one-shot remote submission."}
        required_gates = [item for item in lease.get("pre_run_gates", []) if item.get("required")]
        if required_gates and lease.get("pre_run_gate_results") is None:
            return {"kind": "RECORD_GATES", "instruction": "Complete preparation and record PRE_RUN_RESULTS.json before workload execution."}
        return {"kind": "CONTINUE_LEASE", "instruction": "Continue the admitted experiment within its frozen paths and command policies."}
    last = control.get("last_checkpoint")
    if isinstance(last, dict) and last.get("decision") == "COMPLETE" and last.get("outcome") == "positive" and last.get("core_progress") is True:
        return {"kind": "COMPLETE", "instruction": "The declared metric and guardrails passed at a valid checkpoint; complete the Goal without inventing more work."}
    if isinstance(last, dict) and last.get("preflight_failed"):
        if gate_profile(gate) == "fast":
            return {
                "kind": "CONTINUE_FAST",
                "instruction": "Correct the failed preflight path and continue; fast profile does not require an external re-review.",
            }
        return {"kind": "FRESH_REVIEW", "instruction": "Prepare a corrected proposal on the same causal chain and obtain one fresh experiment review."}
    continuation = control.get("continuation")
    if isinstance(continuation, dict) and continuation.get("state") == "AVAILABLE" and continuation.get("paths"):
        path = continuation["paths"][0]
        kind = str(path.get("kind", "recovery")).upper()
        return {
            "kind": f"{kind}_NEXT",
            "instruction": f"Continue unattended through safe path {path.get('id')}: {path.get('description')}. Use a fresh attempt and a new write-once output root; never resubmit a consumed Job.",
        }
    if isinstance(last, dict) and last.get("decision") == "PAUSE_REQUIRED":
        proof = last.get("blocking_proof") if isinstance(last.get("blocking_proof"), dict) else {}
        if proof.get("block_allowed") is True and proof.get("hard_reason") in HARD_BLOCK_REASONS:
            return {"kind": "AWAIT_DECISION", "instruction": f"Machine blocking proof permits a Goal pause: {proof.get('hard_reason')}."}
        if gate_profile(gate) == "fast":
            return {"kind": "RECOVER_FAST", "instruction": "The experiment paused, not the Goal. Repair the reversible failure or choose the next safe in-scope path without user approval."}
        return {"kind": "ADMIT_RECOVERY", "instruction": "The experiment paused, not the Goal. Admit a bounded recovery/rollback/switch successor; do not mark the Goal blocked."}
    if gate_profile(gate) == "fast":
        return {
            "kind": "CONTINUE_FAST",
            "instruction": "Continue autonomous in-scope work; routine local actions need no lease or external review.",
        }
    return {"kind": "ADMIT_NEXT", "instruction": "Prepare and admit the next bounded experiment; review occurs once at the experiment boundary."}


def compact_status(project: Path, gate: dict[str, Any], control: dict[str, Any], *, session_frontier_only: bool = False) -> dict[str, Any]:
    raw_lease = control.get("active_lease")
    lease = raw_lease if isinstance(raw_lease, dict) else None
    lease_valid = current_lease(control) is not None
    chains = control["chains"]
    if session_frontier_only:
        frontier_ids: list[str] = []
        if lease and isinstance(lease.get("chain_id"), str):
            frontier_ids.append(lease["chain_id"])
        last = control.get("last_checkpoint")
        if isinstance(last, dict) and isinstance(last.get("chain_id"), str):
            frontier_ids.append(last["chain_id"])
        chains = {key: control["chains"][key] for key in dict.fromkeys(frontier_ids) if key in control["chains"]}
    runtime_status = {
        "state": control.get("runtime", {}).get("state", "ACTIVE"),
        "wait": control.get("runtime", {}).get("wait"),
    }
    delivery = external_wait_delivery(control)
    if delivery is not None:
        runtime_status["delivery"] = delivery
    status = {
        "enabled": bool(gate.get("enabled")),
        "profile": gate_profile(gate),
        "active_experiment": lease.get("experiment_id") if lease else None,
        "active_chain": lease.get("chain_id") if lease else None,
        "lease_expires_at": lease.get("expires_at") if lease else None,
        "lease_valid": lease_valid if lease else None,
        "mutations": f"{lease.get('mutations_used')}/{lease.get('max_mutations')}" if lease else None,
        "mutation_budget": {
            "used": int(lease.get("mutations_used", 0)),
            "maximum": int(lease.get("max_mutations", 0)),
            "remaining": max(0, int(lease.get("max_mutations", 0)) - int(lease.get("mutations_used", 0))),
            "plan": lease.get("budget_plan", {}),
        } if lease else None,
        "runtime_bindings": lease.get("binding_values", {}) if lease else {},
        "transport_doctors": lease.get("transport_doctors", {}) if lease else {},
        "external_monitor_receipts": lease.get("monitor_receipts", {}) if lease else {},
        "runtime": runtime_status,
        "next_action": recommended_next_action(gate, control),
        "chains": {key: {field: value.get(field) for field in ("no_progress_count", "stopline_fired", "closed", "close_outcome")} for key, value in chains.items()},
        "last_checkpoint": control.get("last_checkpoint"),
        "continuation": control.get("continuation"),
        "blocking_proof": control.get("last_checkpoint", {}).get("blocking_proof") if isinstance(control.get("last_checkpoint"), dict) else None,
    }
    if not session_frontier_only:
        status["controller"] = control.get("controller", controller_metadata())
        status["active_lease_id"] = lease.get("lease_id") if lease else None
        status["active_proposal_sha256"] = lease.get("proposal_sha256") if lease else None
        status["mutation_accounting"] = {
            "routine_local_actions": "not_counted" if gate_profile(gate) == "fast" else "lease_counted",
            "controller_one_shot_actions": "lease_counted",
            "release_eligible": not release_blockers(lease) if lease else False,
        }
        status["last_lease_release"] = control.get("last_lease_release")
        status["goal"] = control.get("goal")
        status["review_epoch"] = int(control.get("review_epoch", 0))
    else:
        status = {key: value for key, value in status.items() if value not in (None, {}, [])}
    return status


def command_status(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        control = load_control(project)
        persist_control_migration(project, control)
        status = compact_status(project, gate, control)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def deny(reason: str) -> dict[str, Any]:
    prefix = "This tool call was denied, but a denial alone is not Goal completion or blocked status. "
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": prefix + reason + " Inspect controller status and take its next_action."}}


def fast_deny(reason: str, recovery: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Goal Guardrails fast profile skipped one high-impact action: "
                + reason
                + " "
                + recovery
                + " Do not ask the user merely because this call was denied and do not stop the Goal; "
                "continue with the next safe, in-scope action. Ask only if the objective truly cannot progress "
                "without changing the protected boundary."
            ),
        }
    }


def hook_context(
    text: str,
    *,
    continue_turn: bool = True,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}
    }
    if not continue_turn:
        output["continue"] = False
        output["stopReason"] = stop_reason or "Waiting for the registered external event."
    return output


def relative_tool_path(project: Path, cwd: Path, raw: str) -> Path:
    candidate = Path(raw.strip())
    absolute = candidate if candidate.is_absolute() else cwd / candidate
    normalized = Path(os.path.abspath(os.fspath(absolute)))
    try:
        relative = normalized.relative_to(project)
    except ValueError as error:
        raise GuardError(f"patch path escapes guarded project: {raw}") from error
    reject_symlink_escape(project, normalized)
    return relative


def patch_mutations(project: Path, cwd: Path, patch: str) -> list[tuple[Path, str]]:
    directives = list(PATCH_DIRECTIVE_RE.finditer(patch))
    if not directives:
        raise GuardError("cannot determine apply_patch target paths")
    mutations: list[tuple[Path, str]] = []
    for index, directive in enumerate(directives):
        action = directive.group(1).casefold()
        source = relative_tool_path(project, cwd, directive.group(2))
        end = directives[index + 1].start() if index + 1 < len(directives) else len(patch)
        move = PATCH_MOVE_RE.search(patch, directive.end(), end)
        if move:
            if action != "update":
                raise GuardError("apply_patch Move to is valid only with Update File")
            mutations.append((source, "move"))
            mutations.append((relative_tool_path(project, cwd, move.group(1)), "move"))
        else:
            mutations.append((source, action))
    return mutations


def patch_paths(project: Path, cwd: Path, patch: str) -> list[Path]:
    return [path for path, _operation in patch_mutations(project, cwd, patch)]


def is_fast_protected_path(path: Path) -> bool:
    return (
        path in FAST_PROTECTED_PATHS
        or path == CONTROLLER_STATE_REL
        or CONTROLLER_STATE_REL in path.parents
    )


def command_mutates_fast_protected_path(command: str) -> bool:
    normalized = command.replace("\\", "/")
    protected_names = (
        "optimization/GOAL.md",
        "optimization/GATE.json",
        "optimization/CONTROL.json",
        "optimization/.goal-guardrails",
    )
    mentions_protected = any(name in normalized for name in protected_names)
    return mentions_protected and not is_read_only_command(command)


def fast_frozen_lease_paths(lease: dict[str, Any]) -> set[Path]:
    paths = {PROPOSAL_REL}
    paths.update(
        Path(item["path"])
        for item in lease.get("existing_evidence", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    )
    paths.update(
        Path(item["path"])
        for item in (lease.get("pre_run_gate_results") or {}).get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    )
    wake_event = lease.get("wake_event")
    if isinstance(wake_event, dict) and isinstance(wake_event.get("path"), str):
        paths.add(Path(wake_event["path"]))
    for policy in lease.get("bash_policies", []):
        transport = policy.get("transport") if isinstance(policy, dict) else None
        if isinstance(transport, dict) and transport.get("kind") == "ssh-helper-v1":
            paths.update(
                Path(item["path"])
                for item in transport.get("remote_files", [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
    return paths


def command_mutates_fast_frozen_path(command: str, paths: set[Path]) -> bool:
    normalized = command.replace("\\", "/")
    mentions_frozen = any(path.as_posix() in normalized for path in paths)
    return mentions_frozen and not is_read_only_command(command)


def visible_mcp_project_paths(project: Path, cwd: Path, payload: dict[str, Any]) -> set[Path]:
    paths: set[Path] = set()
    path_keys = {
        "path", "paths", "file", "filename", "file_path", "filepath", "target", "target_file", "target_path",
        "destination", "destination_file", "destination_path", "source_path",
    }

    def add(raw: str) -> None:
        candidate = Path(raw)
        absolute = Path(os.path.abspath(os.fspath(candidate if candidate.is_absolute() else cwd / candidate)))
        try:
            paths.add(absolute.relative_to(project))
        except ValueError:
            pass

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in path_keys:
            add(value)

    visit(payload)
    return paths


def visible_mcp_commands(payload: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    command_keys = {"command", "cmd", "shell_command", "shell"}

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).casefold())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in command_keys:
            commands.append(value)

    visit(payload)
    return commands


def resembles_capture_policy(command: str, policy: dict[str, Any]) -> bool:
    if is_read_only_command(command):
        return False
    executable = str(policy.get("executable", ""))
    if not executable:
        return False
    words = re.findall(r"[A-Za-z0-9_./:+-]+", command.replace("\\", "/"))
    mentioned = any(word == executable or Path(word).name == Path(executable).name for word in words)
    if not mentioned:
        return False
    literals = [token.get("literal") for token in policy.get("argv", []) if isinstance(token, dict) and isinstance(token.get("literal"), str)]
    if Path(executable).name == "sbatch":
        return "--parsable" in command
    return not literals or any(literal in command for literal in literals)


def path_allowed(path: Path, allowed: list[str]) -> bool:
    for raw in allowed:
        base = Path(raw)
        if path == base or base in path.parents:
            return True
    return False


def verify_frozen_lease(project: Path, lease: dict[str, Any]) -> None:
    if file_hash(project / GOAL_REL) != lease.get("goal_sha256"):
        raise GuardError("GOAL.md changed after admission; the lease is invalid and must be reviewed again")
    if lease.get("proposal_file_sha256") and file_hash(project / PROPOSAL_REL) != lease.get("proposal_file_sha256"):
        raise GuardError("PROPOSAL.json changed after admission; the lease is invalid")
    if lease.get("proposal_sha256") and canonical_hash(load_json(project / PROPOSAL_REL)) != lease.get("proposal_sha256"):
        raise GuardError("PROPOSAL.json semantics changed after admission; the lease is invalid")
    for evidence in lease.get("existing_evidence", []):
        target = project / str(evidence["path"])
        if target.is_symlink() or not target.is_file() or file_hash(target) != evidence.get("sha256"):
            raise GuardError(f"frozen existing evidence changed after admission: {evidence.get('path')}")
    for policy in lease.get("bash_policies", []):
        transport = policy.get("transport") if isinstance(policy, dict) else None
        if not isinstance(transport, dict) or transport.get("kind") != "ssh-helper-v1":
            continue
        ssh_executable = Path(str(transport.get("ssh_executable")))
        if (
            ssh_executable.is_symlink()
            or not ssh_executable.is_file()
            or not os.access(ssh_executable, os.X_OK)
            or file_hash(ssh_executable) != transport.get("ssh_executable_sha256")
        ):
            raise GuardError("ssh transport executable changed after admission")
        known_hosts = Path(str(transport.get("known_hosts_file")))
        if known_hosts.is_symlink() or not known_hosts.is_file() or file_hash(known_hosts) != transport.get("known_hosts_sha256"):
            raise GuardError("ssh transport known_hosts_file changed after admission")
        if transport.get("identity_file"):
            identity_file = Path(str(transport["identity_file"]))
            if (
                identity_file.is_symlink()
                or not identity_file.is_file()
                or file_hash(identity_file) != transport.get("identity_file_sha256")
            ):
                raise GuardError("ssh transport identity_file changed after admission")
        if file_hash(REMOTE_HELPER_PATH) != transport.get("helper_sha256"):
            raise GuardError("bundled remote helper changed after admission")
        for remote_file in transport.get("remote_files", []):
            target = project / str(remote_file["path"])
            if target.is_symlink() or not target.is_file() or file_hash(target) != remote_file.get("sha256"):
                raise GuardError(f"ssh transport local file changed after admission: {remote_file.get('path')}")
    recorded = lease.get("pre_run_gate_results")
    if isinstance(recorded, dict):
        for artifact in recorded.get("artifacts", []):
            target = project / str(artifact["path"])
            if target.is_symlink() or not target.is_file() or file_hash(target) != artifact.get("sha256"):
                raise GuardError(f"recorded pre-run gate evidence changed: {artifact.get('path')}")
    wake_event = lease.get("wake_event")
    if isinstance(wake_event, dict):
        target = project / str(wake_event["path"])
        if target.is_symlink() or not target.is_file() or file_hash(target) != wake_event.get("sha256"):
            raise GuardError(f"frozen wake event evidence changed: {wake_event.get('path')}")


def matching_bash_policy(project: Path, cwd: Path, command: str, lease: dict[str, Any]) -> dict[str, Any] | None:
    if re.search(r"[\n;&|`<>]|\$\(", command):
        return None
    try:
        tokens = shlex.split(command)
        actual_cwd = normalize_cwd(project, os.fspath(cwd))
    except (ValueError, GuardError):
        return None
    if not tokens:
        return None
    for policy in lease.get("bash_policies", []):
        try:
            expected = render_policy_argv(policy, lease)
        except GuardError:
            continue
        if tokens == expected and actual_cwd == policy["cwd"]:
            return policy
    return None


def is_controller_command(command: str) -> bool:
    if re.search(r"[\n;&|`<>]|\$\(", command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 3 or Path(tokens[0]).name not in {"python", "python3"}:
        return False
    try:
        script = Path(tokens[1]).resolve(strict=True)
    except OSError:
        return False
    return script == CONTROLLER_PATH and tokens[2] in {
        "status", "admit", "gates", "doctor", "submit-bind", "reconcile-bind", "wait", "wake", "wait-monitor", "wake-monitor",
        "checkpoint", "abort", "abort-preflight", "activate", "deactivate", "mode", "subject", "review-subject",
        "release", "release-lease", "update-goal",
    }


def is_read_only_mcp(tool_name: str) -> bool:
    lowered = tool_name.casefold()
    mutating = ("write", "edit", "create", "update", "delete", "remove", "move", "copy", "upload", "publish", "deploy", "execute", "run", "send", "apply")
    readable = ("read", "get", "list", "search", "find", "query", "fetch", "open", "view", "inspect", "status", "show")
    return any(word in lowered for word in readable) and not any(word in lowered for word in mutating)


def has_unquoted_shell_glob(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for character in command:
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in {"*", "?", "["}:
            return True
    return False


def shell_read_stages(command: str) -> list[list[str]] | None:
    """Split a small shell expression without executing or expanding it.

    Sequential/conditional operators and ordinary pipes are acceptable only
    when every resulting command is independently read-only. Redirections,
    background execution, substitutions, and multiline shell are rejected.
    """
    stripped = command.strip()
    if (
        not stripped
        or "\n" in stripped
        or "\r" in stripped
        or "`" in stripped
        or "$(" in stripped
        or has_unquoted_shell_glob(stripped)
    ):
        return None
    try:
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens:
        return None
    separators = {";", "&&", "||", "|"}
    stages: list[list[str]] = []
    stage: list[str] = []
    for token in tokens:
        if token in separators:
            if not stage:
                return None
            stages.append(stage)
            stage = []
            continue
        if token and set(token) <= {";", "&", "|", "<", ">"}:
            return None
        stage.append(token)
    if not stage:
        return None
    stages.append(stage)
    return stages


def option_present(tokens: list[str], *options: str) -> bool:
    return any(token == option or token.startswith(option + "=") for token in tokens for option in options)


def git_read_only(tokens: list[str]) -> bool:
    index = 1
    no_pager = False
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        if token == "--no-pager":
            no_pager = True
            index += 1
        elif token == "-C" and index + 1 < len(tokens):
            index += 2
        else:
            return False
    if index >= len(tokens):
        return False
    subcommand = tokens[index]
    args = tokens[index + 1:]
    if subcommand not in {"status", "diff", "log", "show", "rev-parse", "branch"}:
        return False
    if subcommand == "branch" and args != ["--show-current"]:
        return False
    if option_present(
        args,
        "--output", "--ext-diff", "--textconv", "--show-signature", "--verify-signatures",
        "--format", "--pretty",
    ):
        return False
    if subcommand in {"diff", "log", "show"}:
        return (
            no_pager
            and option_present(args, "--no-ext-diff")
            and option_present(args, "--no-textconv")
        )
    return True


def date_read_only(tokens: list[str]) -> bool:
    args = tokens[1:]
    utc_seen = False
    format_seen = False
    for token in args:
        if token in {"-u", "--utc", "--universal"} and not utc_seen:
            utc_seen = True
        elif token.startswith("+") and len(token) > 1 and not format_seen:
            format_seen = True
        else:
            return False
    return True


def sed_read_only(tokens: list[str]) -> bool:
    args = tokens[1:]
    if not args or not option_present(args, "-n", "--quiet", "--silent"):
        return False
    if option_present(args, "-i", "--in-place"):
        return False
    # Keep the allowed sed surface intentionally small: numeric print ranges
    # cover the controller/document inspection commands without sed's write or
    # execute opcodes.
    scripts: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-n", "--quiet", "--silent"}:
            index += 1
        elif token in {"-e", "--expression"} and index + 1 < len(args):
            scripts.append(args[index + 1])
            index += 2
        elif token.startswith("--expression="):
            scripts.append(token.split("=", 1)[1])
            index += 1
        elif token.startswith("-"):
            return False
        elif not scripts:
            scripts.append(token)
            index += 1
        else:
            index += 1
    return bool(scripts) and all(re.fullmatch(r"\s*(?:\d+|\$)(?:,(?:\d+|\$))?p\s*", script) for script in scripts)


def simple_command_is_read_only(tokens: list[str]) -> bool:
    if not tokens or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        return False
    executable = tokens[0]
    if "/" in executable:
        return False
    args = tokens[1:]
    if executable in {"pwd", "ls", "cat", "echo", "head", "tail", "wc", "stat", "sha256sum", "jq"}:
        return True
    if executable == "date":
        return date_read_only(tokens)
    if executable in {"rg", "grep"}:
        return not option_present(args, "--pre", "--pre-glob")
    if executable == "sed":
        return sed_read_only(tokens)
    if executable == "find":
        dangerous_actions = {
            "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprint0", "-fprintf",
        }
        return not any(token in dangerous_actions for token in args)
    if executable == "git":
        return git_read_only(tokens)
    if executable in {"squeue", "sacct", "qstat", "ps"}:
        return True
    if executable == "systemctl":
        return bool(args) and args[0] == "status"
    if executable == "docker":
        return bool(args) and args[0] == "ps"
    if executable == "kubectl":
        return bool(args) and args[0] == "get"
    if executable == "nvidia-smi":
        value_options = {"-i", "--id", "-l", "--loop", "-lms", "--loop-ms"}
        flag_options = {
            "-L", "--list-gpus", "-q", "--query", "-x", "--xml-format", "--dtd", "-h", "--help", "--version",
        }
        index = 0
        while index < len(args):
            token = args[index]
            if token in flag_options or token.startswith("--query-") or token.startswith("--format="):
                index += 1
            elif token in value_options and index + 1 < len(args):
                index += 2
            elif any(token.startswith(option + "=") for option in value_options):
                index += 1
            else:
                return False
        return True
    return False


def is_read_only_command(command: str) -> bool:
    stages = shell_read_stages(command)
    return stages is not None and all(simple_command_is_read_only(stage) for stage in stages)


def lease_error(project: Path, gate: dict[str, Any], control: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if external_wait_in_progress(control):
        return None, "runtime is arming or waiting for an external event; do not mutate or poll until the scheduler gate or wake event is reconciled"
    raw = control.get("active_lease")
    lease = current_lease(control)
    if lease is None:
        return None, "experiment lease is missing or expired; obtain a fresh review-attested lease before mutating work"
    try:
        verify_frozen_lease(project, lease)
    except GuardError as error:
        return None, str(error)
    if lease.get("preflight_failed"):
        return None, "required preflight gate failed; freeze the invalid result and checkpoint before more work"
    if state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
        return None, "STATE.md exceeds its cap; compact the frontier before more work"
    if int(lease.get("mutations_used", 0)) >= int(lease.get("max_mutations", 0)):
        return None, "experiment mutation allowance is exhausted; checkpoint before continuing"
    if raw is not lease:
        return None, "experiment lease is invalid"
    return lease, None


def hook_pre_tool_fast(project: Path, event: dict[str, Any], control: dict[str, Any]) -> dict[str, Any] | None:
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "apply_patch":
        command = str(tool_input.get("patch", tool_input.get("input", tool_input.get("command", ""))))
    else:
        command = str(tool_input.get("command", ""))
    cwd = Path(str(event.get("cwd", project)))
    waiting = external_wait_in_progress(control)

    if tool == "Bash" and is_controller_command(command):
        return None
    if waiting:
        if tool == "Bash":
            if is_read_only_command(command) and not POLL_RE.search(command):
                return None
            if is_read_only_command(command) and POLL_RE.search(command):
                return fast_deny(
                    "the runtime is waiting and this command is status polling without a semantic event",
                    "End this activation; the managed monitor will resume the exact thread when durable state changes.",
                )
        if isinstance(tool, str) and tool.startswith("mcp__") and is_read_only_mcp(tool):
            return None
        return fast_deny(
            "the runtime is WAITING_EXTERNAL_EVENT and this call could mutate state before the registered event",
            "Inspect non-polling read-only evidence, process the registered wake event, or end this activation until it arrives.",
        )

    if tool == "apply_patch":
        try:
            mutations = patch_mutations(project, cwd, command)
        except GuardError as error:
            return fast_deny(str(error), "Use a project-relative, non-symlink patch target.")
        protected = sorted(path.as_posix() for path, _operation in mutations if is_fast_protected_path(path))
        if protected:
            return fast_deny(
                f"the patch changes protected goal/controller files: {protected}",
                "Leave the objective and controller boundary unchanged; put optional changes in BACKLOG.md.",
            )
        lease = control.get("active_lease")
        if isinstance(lease, dict):
            frozen_paths = fast_frozen_lease_paths(lease)
            drifted = sorted(path.as_posix() for path, _operation in mutations if path in frozen_paths)
            if drifted:
                return fast_deny(
                    f"the patch would overwrite frozen run evidence or submitted inputs: {drifted}",
                    "Finish/reconcile the active run, then create new evidence for a later attempt.",
                )
        return None

    if tool == "Bash":
        if FAST_DESTRUCTIVE_RE.search(command):
            return fast_deny(
                "the command is broadly destructive or difficult to recover",
                "Use a narrower recoverable command or defer it to BACKLOG.md.",
            )
        if command_mutates_fast_protected_path(command):
            return fast_deny(
                "the command mutates protected goal/controller files",
                "Continue without changing the objective or controller state.",
            )
        lease = control.get("active_lease")
        if isinstance(lease, dict):
            if command_mutates_fast_frozen_path(command, fast_frozen_lease_paths(lease)):
                return fast_deny(
                    "the command would overwrite a frozen proposal, evidence file, or submitted input",
                    "Finish/reconcile the active run, then create new evidence for a later attempt.",
                )
            capture_policy = next(
                (
                    policy for policy in lease.get("bash_policies", [])
                    if isinstance(policy, dict) and policy.get("capture_binding") and resembles_capture_policy(command, policy)
                ),
                None,
            )
            if isinstance(capture_policy, dict):
                return fast_deny(
                    "a one-shot runtime-binding submission was invoked directly",
                    "Run controller submit-bind so a timeout cannot cause a duplicate external job.",
                )
        return None

    if isinstance(tool, str) and tool.startswith("mcp__") and not is_read_only_mcp(tool):
        paths = visible_mcp_project_paths(project, cwd, tool_input)
        commands = visible_mcp_commands(tool_input)
        if any(FAST_DESTRUCTIVE_RE.search(candidate) for candidate in commands):
            return fast_deny(
                "the MCP call contains a broadly destructive command",
                "Use a narrower recoverable command or defer it to BACKLOG.md.",
            )
        if any(command_mutates_fast_protected_path(candidate) for candidate in commands):
            return fast_deny(
                "the MCP call contains a command that could mutate protected goal/controller files",
                "Leave the objective and controller boundary unchanged.",
            )
        protected = sorted(path.as_posix() for path in paths if is_fast_protected_path(path))
        if protected:
            return fast_deny(
                f"the MCP call targets protected goal/controller files: {protected}",
                "Leave the objective and controller boundary unchanged.",
            )
        lease = control.get("active_lease")
        if isinstance(lease, dict):
            if any(command_mutates_fast_frozen_path(candidate, fast_frozen_lease_paths(lease)) for candidate in commands):
                return fast_deny(
                    "the MCP call contains a command that could mutate frozen run evidence or submitted inputs",
                    "Finish/reconcile the active run, then create new evidence for a later attempt.",
                )
            capture_policy = next(
                (
                    policy for policy in lease.get("bash_policies", [])
                    if isinstance(policy, dict)
                    and policy.get("capture_binding")
                    and any(resembles_capture_policy(candidate, policy) for candidate in commands)
                ),
                None,
            )
            if isinstance(capture_policy, dict):
                return fast_deny(
                    "the MCP call could directly invoke a one-shot runtime-binding submission",
                    "Run controller submit-bind so a timeout cannot cause a duplicate external job.",
                )
            drifted = sorted(path.as_posix() for path in paths if path in fast_frozen_lease_paths(lease))
            if drifted:
                return fast_deny(
                    f"the MCP call targets frozen run evidence or submitted inputs: {drifted}",
                    "Finish/reconcile the active run, then create new evidence for a later attempt.",
                )

    # YOLO/full-access mode already owns ordinary authorization. Fast profile adds no
    # per-tool MCP admission or mutation accounting beyond visible protected paths.
    return None


def hook_pre_tool(project: Path, event: dict[str, Any], gate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any] | None:
    if gate_profile(gate) == "fast":
        return hook_pre_tool_fast(project, event, control)
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool == "apply_patch":
        command = str(tool_input.get("patch", tool_input.get("input", tool_input.get("command", ""))))
    else:
        command = str(tool_input.get("command", ""))
    cwd = Path(str(event.get("cwd", project)))
    waiting = external_wait_in_progress(control)
    if waiting:
        if tool == "Bash" and is_controller_command(command):
            return None
        if tool == "Bash" and is_read_only_command(command) and not POLL_RE.search(command):
            return None
        if isinstance(tool, str) and tool.startswith("mcp__") and is_read_only_mcp(tool):
            return None
        return deny("runtime is WAITING_EXTERNAL_EVENT; only non-polling inspection or the registered wake event is allowed")
    if tool == "Bash" and (is_controller_command(command) or is_read_only_command(command)):
        poll = control.get("poll")
        if poll and poll.get("blocked_command_sha256") == hashlib.sha256(command.strip().encode()).hexdigest():
            return deny("unchanged polling limit reached; wait for a semantic event or checkpoint instead of repeating the same status query")
        return None
    if isinstance(tool, str) and tool.startswith("mcp__") and is_read_only_mcp(tool):
        return None
    if tool == "apply_patch":
        try:
            patch_targets = patch_mutations(project, cwd, command)
            targets = [path for path, _operation in patch_targets]
        except GuardError as error:
            return deny(str(error))
        raw_lease = control.get("active_lease")
        if not isinstance(raw_lease, dict) and all(path == PROPOSAL_REL for path in targets):
            return None
        if not isinstance(raw_lease, dict) and all(path == STATE_REL for path in targets):
            try:
                if state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
                    return None
            except (GuardError, OSError, UnicodeError):
                pass
    try:
        lease, error = lease_error(project, gate, control)
    except (GuardError, OSError, UnicodeError) as exc:
        return deny(str(exc))
    if (error or lease is None) and tool == "apply_patch":
        raw_lease = control.get("active_lease")
        preflight_failure_result = (
            isinstance(raw_lease, dict)
            and raw_lease.get("preflight_failed")
            and all(path == RESULT_REL for path in targets)
        )
        ordinary_result_correction = (
            isinstance(raw_lease, dict)
            and not raw_lease.get("preflight_failed")
            and all(path == RESULT_REL for path in targets)
        )
        ordinary_finalization = (
            isinstance(raw_lease, dict)
            and not raw_lease.get("preflight_failed")
            and all(path in FINALIZATION_PATHS for path in targets)
        )
        if preflight_failure_result or ordinary_result_correction:
            return None
        if ordinary_finalization and not raw_lease.get("finalization_used"):
            raw_lease["finalization_used"] = True
            control["active_lease"] = raw_lease
            save_control(project, control)
            return None
    if error or lease is None:
        return deny(error or "invalid experiment lease")
    if tool == "apply_patch":
        try:
            patch_targets = patch_mutations(project, cwd, command)
            targets = [path for path, _operation in patch_targets]
        except GuardError as exc:
            return deny(str(exc))
        if isinstance(lease.get("proposal_schema_version"), int) and lease.get("proposal_schema_version") >= 2:
            blocked = [
                f"{path.as_posix()}:{operation}"
                for path, operation in patch_targets
                if path not in ALWAYS_LEASE_PATHS and not mutation_allows(path.as_posix(), operation, lease.get("lease_mutations", []))
            ]
        else:
            legacy_allowed = list(lease.get("allowed_paths", [])) + [path.as_posix() for path in ALWAYS_LEASE_PATHS]
            blocked = [path.as_posix() for path in targets if not path_allowed(path, legacy_allowed)]
        if blocked:
            return deny(f"patch targets are outside the admitted lease: {blocked}")
        if any(is_protected_path(path) for path in targets):
            return deny("contract and gate-control files cannot be changed under an experiment lease")
        recorded_gate_paths = {
            Path(item["path"])
            for item in (lease.get("pre_run_gate_results") or {}).get("artifacts", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        if any(path in recorded_gate_paths for path in targets):
            return deny("recorded pre-run gate evidence is frozen for the remainder of the lease")
        wake_event = lease.get("wake_event")
        if isinstance(wake_event, dict) and any(path.as_posix() == wake_event.get("path") for path in targets):
            return deny("terminal wake event evidence is frozen for the remainder of the lease")
    elif tool == "Bash":
        if isinstance(lease.get("proposal_schema_version"), int) and lease.get("proposal_schema_version") >= 2:
            policy = matching_bash_policy(project, cwd, command, lease)
            if policy is None:
                return deny("Bash command, fixed arguments, or cwd is outside the structured lease policy")
            if policy.get("capture_binding"):
                return deny("binding-capture policies must run through controller submit-bind")
            try:
                enforce_policy_phase(lease, policy)
            except GuardError as error:
                return deny(str(error))
            wake_event = lease.get("wake_event")
            if isinstance(wake_event, dict) and wake_event.get("path") in policy.get("output_paths", []):
                return deny("structured Bash output would overwrite frozen terminal wake evidence")
        elif re.search(r"[\n;&|`<>]|\$\(", command) or not any(command.strip() == prefix or command.strip().startswith(prefix + " ") for prefix in lease.get("allowed_command_prefixes", [])):
            return deny("Bash command is outside the legacy admitted command prefixes")
    elif isinstance(tool, str) and tool.startswith("mcp__"):
        return deny("mutating or unknown MCP tools fail closed; use apply_patch or an admitted Bash command with enforceable scope")
    lease["mutations_used"] = int(lease["mutations_used"]) + 1
    control["active_lease"] = lease
    control["poll"] = None
    save_control(project, control)
    return None


def hook_post_tool(project: Path, event: dict[str, Any], gate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any] | None:
    if gate_profile(gate) == "fast":
        return None
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command", "")).strip()
    if not POLL_RE.search(command):
        return None
    response_hash = canonical_hash(event.get("tool_response"))
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    poll = control.get("poll") if isinstance(control.get("poll"), dict) else {}
    repeats = int(poll.get("repeats", 0)) + 1 if poll.get("command_sha256") == command_hash and poll.get("response_sha256") == response_hash else 1
    control["poll"] = {"command_sha256": command_hash, "response_sha256": response_hash, "repeats": repeats}
    if repeats >= int(gate["max_unchanged_polls"]):
        control["poll"]["blocked_command_sha256"] = command_hash
        save_control(project, control)
        return {"decision": "block", "reason": "Repeated polling returned no new information. Stop monitoring work and resume only at a semantic event or checkpoint."}
    save_control(project, control)
    return None


def command_hook() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
        project = find_project(Path(str(event.get("cwd", os.getcwd()))))
        if project is None:
            return 0
        gate = load_gate(project)
        if not gate.get("enabled"):
            return 0
        with state_lock(project):
            control = load_control(project)
            name = event.get("hook_event_name")
            if name == "SessionStart":
                persist_control_migration(project, control)
                runtime = control.get("runtime", {})
                terminal_recovery_pending = False
                armed_this_session = False
                wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else None
                if (
                    runtime.get("state") in {"ARMING_EXTERNAL_WAIT", "WAITING_EXTERNAL_EVENT"}
                    and isinstance(wait, dict)
                    and wait.get("kind") == "external_monitor"
                ):
                    delivery = external_wait_delivery(control)
                    event_id = delivery.get("event_id") if isinstance(delivery, dict) else None
                    if isinstance(event_id, str):
                        try:
                            wake_monitor_locked(project, control, str(wait.get("monitor_id")), event_id)
                        except (GuardError, OSError, UnicodeError, ValueError) as error:
                            terminal_recovery_pending = True
                            observation = {
                                "schema_version": "goal-guardrails.external-reconciliation/v1",
                                "state": "recoverable_transport_error",
                                "event_id": event_id,
                                "safe_message": str(error),
                                "business_effect": "none",
                            }
                            observation["sha256"] = f"sha256:{canonical_hash(observation)}"
                            if runtime.get("external_reconciliation", {}).get("sha256") != observation["sha256"]:
                                runtime["external_reconciliation"] = observation
                                control["runtime"] = runtime
                                save_control(project, control)
                    runtime = control.get("runtime", {})
                    wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else None
                    if runtime.get("state") == "ARMING_EXTERNAL_WAIT" and not terminal_recovery_pending:
                        finalize_scheduler_wait_locked(project, control)
                        armed_this_session = True
                        runtime = control.get("runtime", {})
                        wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else None
                    if (
                        runtime.get("state") == "WAITING_EXTERNAL_EVENT"
                        and isinstance(wait, dict)
                        and wait.get("contract_version") == 2
                        and not terminal_recovery_pending
                        and not armed_this_session
                    ):
                        rearm_scheduler_wait_locked(project, control)
            output: dict[str, Any] | None = None
            if name == "SessionStart":
                status = compact_status(project, gate, control, session_frontier_only=True)
                waiting = external_wait_in_progress(control)
                session_runtime = control.get("runtime", {})
                wait = session_runtime.get("wait")
                reconciliation = (
                    session_runtime.get("external_reconciliation")
                    if isinstance(session_runtime.get("external_reconciliation"), dict)
                    else {}
                )
                scheduler_gated_wait = (
                    session_runtime.get("state") == "WAITING_EXTERNAL_EVENT"
                    and isinstance(wait, dict)
                    and wait.get("contract_version") == 2
                    and isinstance(wait.get("scheduler_gate"), dict)
                    and wait["scheduler_gate"].get("state") == "ARMED"
                    and reconciliation.get("state") != "recoverable_transport_error"
                )
                transport_paused = status.get("next_action", {}).get("kind") == "PAUSE_REQUIRED"
                if gate_profile(gate) == "fast":
                    if transport_paused:
                        suffix = " External-event delivery cannot wake this Goal; report PAUSE_REQUIRED/transport failure and do not remain in WAIT."
                    elif scheduler_gated_wait:
                        suffix = " Runtime is scheduler-gated in WAITING_EXTERNAL_EVENT; this no-event activation is ending before a model request."
                    elif waiting:
                        suffix = " Runtime is WAITING_EXTERNAL_EVENT: read-only inspection is allowed, but mutation must wait for the registered wake event."
                    else:
                        suffix = ""
                    output = hook_context(
                        "Goal Guardrails fast profile is active for unattended execution. Current status: "
                        + json.dumps(status, ensure_ascii=False, separators=(",", ":"))
                        + ". This runtime profile supersedes older project-managed text that says every mutation needs a review-attested lease. "
                        + " Continue routine in-scope local editing, tests, builds, evaluation, diagnostics, recovery, and semantic evidence recording without a lease, external review, or user approval. Controller checkpoint is only for an optional active lease. "
                        + "A tool denial skips only that high-impact action: do not stop the Goal or ask the user merely because of it; record it in BACKLOG.md and continue the next safe action. "
                        + "Experiment failure and chain closure are not Goal blocking. Never mark the persistent Goal blocked unless next_action is AWAIT_DECISION and blocking_proof.block_allowed=true. "
                        + "Request user input only when progress truly requires changing the objective/metric, budget or material scope, executing an irreversible external action, unavailable external input, or overriding a fired global stop line."
                        + suffix,
                        continue_turn=not scheduler_gated_wait,
                        stop_reason="No registered terminal event is available; the exact-thread continuation gate remains armed.",
                    )
                else:
                    if transport_paused:
                        suffix = " External-event delivery cannot wake this Goal; report PAUSE_REQUIRED/transport failure instead of waiting."
                    elif scheduler_gated_wait:
                        suffix = " Runtime is scheduler-gated in WAITING_EXTERNAL_EVENT; this no-event activation is ending before a model request."
                    elif waiting:
                        suffix = " Runtime is WAITING_EXTERNAL_EVENT: do not poll or start adjacent work; end this activation unless a registered wake event arrived."
                    else:
                        suffix = ""
                    output = hook_context(
                        "Goal Guardrails enforcement is active in strict profile. Current gate status: "
                        + json.dumps(status, ensure_ascii=False, separators=(",", ":"))
                        + ". A single denied tool call is a recoverable control transition, not permission to mark the Goal complete or blocked; follow next_action. "
                        + "Experiment failure and chain closure are not Goal blocking. Never mark the persistent Goal blocked unless next_action is AWAIT_DECISION and blocking_proof.block_allowed=true. "
                        + "Do not self-attest proposals; obtain one fresh subagent or user review at the experiment boundary. The controller validates attestation shape, not reviewer identity."
                        + suffix,
                        continue_turn=not scheduler_gated_wait,
                        stop_reason="No registered terminal event is available; the exact-thread continuation gate remains armed.",
                    )
            elif name == "PreToolUse":
                output = hook_pre_tool(project, event, gate, control)
            elif name == "PostToolUse":
                output = hook_post_tool(project, event, gate, control)
            if output is not None:
                print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as error:
        event_name = locals().get("event", {}).get("hook_event_name") if isinstance(locals().get("event"), dict) else None
        if event_name == "PreToolUse":
            loaded_gate = locals().get("gate")
            if isinstance(loaded_gate, dict) and loaded_gate.get("profile", "fast") == "fast":
                payload = fast_deny(
                    f"the guard configuration could not be evaluated safely: {error}",
                    "Skip this call, record the configuration defect, and continue other safe work.",
                )
            else:
                payload = deny(f"Goal Guardrails failed closed: {error}")
            print(json.dumps(payload, ensure_ascii=False))
        elif event_name == "SessionStart":
            print(json.dumps(hook_context(f"Goal Guardrails configuration error: {error}"), ensure_ascii=False))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goal Guardrails experiment-lease controller")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    for name in ("status",):
        item = sub.add_parser(name)
        item.add_argument("--project")
    admit = sub.add_parser("admit")
    admit.add_argument("proposal")
    admit.add_argument("--project")
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("result")
    checkpoint.add_argument("--project")
    correct_checkpoint = sub.add_parser("correct-checkpoint", aliases=["supersede-checkpoint"])
    correct_checkpoint.add_argument("result")
    correct_checkpoint.add_argument("--supersedes", required=True)
    correct_checkpoint.add_argument("--project")
    abort = sub.add_parser("abort", aliases=["abort-preflight"])
    abort.add_argument("--project")
    gates = sub.add_parser("gates")
    gates.add_argument("results")
    gates.add_argument("--project")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--policy", required=True)
    doctor.add_argument("--project")
    submit_bind = sub.add_parser("submit-bind")
    submit_bind.add_argument("--policy", required=True)
    submit_bind.add_argument("--project")
    reconcile_bind = sub.add_parser("reconcile-bind")
    reconcile_bind.add_argument("--policy", required=True)
    reconcile_bind.add_argument("--project")
    wait = sub.add_parser("wait")
    wait.add_argument("--event-key", required=True)
    wait.add_argument("--event-path", required=True)
    wait.add_argument("--project")
    wake = sub.add_parser("wake")
    wake.add_argument("--event-key", required=True)
    wake.add_argument("--event-path", required=True)
    wake.add_argument("--project")
    wait_monitor = sub.add_parser("wait-monitor")
    wait_monitor.add_argument("--monitor", required=True)
    wait_monitor.add_argument("--project")
    wake_monitor = sub.add_parser("wake-monitor")
    wake_monitor.add_argument("--monitor", required=True)
    wake_monitor.add_argument("--event-id")
    wake_monitor.add_argument("--project")
    subject = sub.add_parser("subject", aliases=["review-subject"])
    subject.add_argument("proposal")
    subject.add_argument("--project")
    release = sub.add_parser("release", aliases=["release-lease"])
    release.add_argument("--expected-proposal-sha256", required=True)
    release.add_argument("--reason", required=True)
    release.add_argument("--project")
    update_goal = sub.add_parser("update-goal")
    update_goal.add_argument("--approved-by", required=True)
    update_goal.add_argument("--expected-sha256", required=True)
    update_goal.add_argument("--from-file", required=True)
    update_goal.add_argument("--reason", required=True)
    update_goal.add_argument("--project")
    for name in ("activate", "deactivate"):
        item = sub.add_parser(name)
        item.add_argument("--project")
        item.add_argument("--approved-by", required=True)
        item.add_argument("--reason", default="explicit user-approved gate change")
    mode = sub.add_parser("mode")
    mode.add_argument("profile", choices=("fast", "strict"))
    mode.add_argument("--project")
    mode.add_argument("--approved-by", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "hook":
            return command_hook()
        if args.command == "status":
            return command_status(args)
        if args.command == "admit":
            return command_admit(args)
        if args.command == "gates":
            return command_gates(args)
        if args.command == "doctor":
            return command_doctor(args)
        if args.command == "submit-bind":
            return command_submit_bind(args)
        if args.command == "reconcile-bind":
            return command_reconcile_bind(args)
        if args.command == "wait":
            return command_wait(args)
        if args.command == "wake":
            return command_wake(args)
        if args.command == "wait-monitor":
            return command_wait_monitor(args)
        if args.command == "wake-monitor":
            return command_wake_monitor(args)
        if args.command in {"subject", "review-subject"}:
            return command_review_subject(args)
        if args.command in {"release", "release-lease"}:
            return command_release_lease(args)
        if args.command == "update-goal":
            return command_update_goal(args)
        if args.command == "checkpoint":
            return command_checkpoint(args)
        if args.command in {"correct-checkpoint", "supersede-checkpoint"}:
            return command_correct_checkpoint(args)
        if args.command in {"abort", "abort-preflight"}:
            return command_abort_preflight(args)
        if args.command == "activate":
            return command_toggle(args, True)
        if args.command == "deactivate":
            return command_toggle(args, False)
        if args.command == "mode":
            return command_mode(args)
        raise GuardError("unknown command")
    except (GuardError, OSError, UnicodeError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
