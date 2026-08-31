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
PROPOSAL_SCHEMA_VERSION = 2
RESULT_SCHEMA_VERSION = 2
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
RECEIPTS_REL = CONTROLLER_STATE_REL / "receipts"
EXPERIMENTS_REL = Path("optimization/EXPERIMENTS.md")
BACKLOG_REL = Path("optimization/BACKLOG.md")
ALWAYS_LEASE_PATHS = (STATE_REL, RESULT_REL, PRE_RUN_RESULTS_REL, EXPERIMENTS_REL, BACKLOG_REL)
FINALIZATION_PATHS = (STATE_REL, RESULT_REL, EXPERIMENTS_REL)
PROTECTED_PATHS = (GOAL_REL, GATE_REL, CONTROL_REL, PROPOSAL_REL)
DECISIONS = {"CONTINUE", "REPLICATE", "SWITCH", "ROLLBACK", "PAUSE_REQUIRED", "COMPLETE"}
OUTCOMES = {"positive", "negative", "zero_progress", "inconclusive", "invalid"}
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
READ_ONLY_PREFIXES = (
    "pwd", "ls", "cat", "echo", "printf", "rg", "grep", "sed -n", "head", "tail", "wc", "stat", "sha256sum",
    "git status", "git diff", "git log", "git show", "git rev-parse", "git branch --show-current",
    "jq",
)
MUTATING_SHELL_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|mv|cp|install|chmod|chown|mkdir|touch|tee|truncate|dd|sed\s+-i|git\s+(?:add|commit|push|reset|checkout|switch|merge|rebase|clean))\b|(?:^|\s)(?:>|>>)(?:\s|$)",
    re.I,
)
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
    return gate


def gate_profile(gate: dict[str, Any]) -> str:
    profile = gate.get("profile", "fast")
    if profile not in {"fast", "strict"}:
        raise GuardError("gate profile must be fast or strict")
    return str(profile)


def default_control() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "active_lease": None,
        "chains": {},
        "poll": None,
        "last_checkpoint": None,
        "runtime": {"state": "ACTIVE", "wait": None, "seen_events": []},
    }


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
    runtime = control["runtime"]
    runtime.setdefault("state", "ACTIVE")
    runtime.setdefault("wait", None)
    runtime.setdefault("seen_events", [])
    if runtime["state"] not in {"ACTIVE", "WAITING_EXTERNAL_EVENT"} or not isinstance(runtime["seen_events"], list):
        raise GuardError("CONTROL.json runtime state is invalid")
    return control


def save_control(project: Path, control: dict[str, Any]) -> None:
    atomic_json(project / CONTROL_REL, control)


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
    require_external_monitor: bool = False,
    require_preflight_failure: bool = False,
    require_remote_submission: bool = False,
) -> dict[str, Any]:
    if not isinstance(review, dict) or review.get("decision") != "ALLOW":
        raise GuardError("proposal requires an ALLOW review attestation")
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
        "reviewer": reviewer,
        "reason": ensure_text(review.get("reason"), "review.reason"),
        "checks": {name: True for name in sorted(required_checks)},
    }


def automatic_fast_review_attestation(
    *,
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
        if item.get("provider") != "codex-hpc-monitor" or item.get("contract_version") != 1:
            raise GuardError("external monitors currently support codex-hpc-monitor contract_version 1")
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
        if not any(token.get("literal") == "--require-auto-resume" for token in template):
            raise GuardError("external monitor start policy must freeze --require-auto-resume")
        monitors.append({
            "id": monitor_id,
            "provider": "codex-hpc-monitor",
            "contract_version": 1,
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


def validate_proposal(project: Path, gate: dict[str, Any], control: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    if proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise GuardError(f"proposal schema_version must be {PROPOSAL_SCHEMA_VERSION}")
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
    pre_run_gates = validate_pre_run_gates(proposal.get("pre_run_gates"), {item["id"] for item in checkpoint_artifacts})
    runtime_bindings = validate_runtime_bindings(proposal.get("runtime_bindings"))
    bash_policies = validate_bash_policies(project, proposal.get("bash_policies"), lease_mutations, pre_run_gates, runtime_bindings)
    external_monitors = validate_external_monitors(proposal.get("external_monitors"), runtime_bindings, bash_policies)
    review_requirements = {
        "require_external_monitor": bool(external_monitors),
        "require_preflight_failure": any(item["required"] for item in pre_run_gates),
        "require_remote_submission": any(
            policy.get("transport", {}).get("kind") == "ssh-helper-v1" for policy in bash_policies
        ),
    }
    if gate_profile(gate) == "fast":
        reviewer = automatic_fast_review_attestation(**review_requirements)
    else:
        reviewer = validate_review_attestation(proposal.get("review"), **review_requirements)
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
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "lease_phase": lease_phase,
        "existing_evidence": existing_evidence,
        "lease_mutations": lease_mutations,
        "checkpoint_artifacts": checkpoint_artifacts,
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
        if control["runtime"]["state"] == "WAITING_EXTERNAL_EVENT":
            raise GuardError("cannot record pre-run gates while waiting for an external event")
        lease = control.get("active_lease")
        if not isinstance(lease, dict) or current_lease(control) is None:
            raise GuardError("a live experiment lease is required")
        verify_frozen_lease(project, lease)
        if payload.get("schema_version") != RESULT_SCHEMA_VERSION or payload.get("experiment_id") != lease["experiment_id"]:
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


def command_wait_monitor(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    monitor_id = ensure_id(args.monitor, "monitor")
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control["runtime"]["state"] == "WAITING_EXTERNAL_EVENT":
            raise GuardError("runtime is already waiting for an external event")
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
        remaining = max(1, int((parse_time(lease["expires_at"]) - utc_now()).total_seconds()))
        lease["suspended_at"] = iso_time(utc_now())
        lease["remaining_seconds"] = remaining
        control["active_lease"] = lease
        control["runtime"]["state"] = "WAITING_EXTERNAL_EVENT"
        control["runtime"]["wait"] = {
            "kind": "external_monitor",
            "monitor_id": monitor_id,
            "provider": monitor["provider"],
            "job_id": run["job_id"],
            "run_id": run["run_id"],
            "manifest_sha256": run["manifest_sha256"],
            "baseline_terminal_sha256": baseline,
            "entered_at": iso_time(utc_now()),
        }
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["runtime"]["wait"], ensure_ascii=False, indent=2))
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
        seen_events = control["runtime"].get("seen_events", [])
        if requested_event_id is not None and any(
            isinstance(item, dict)
            and item.get("event_key") == requested_event_id
            and item.get("monitor_id") == monitor_id
            for item in seen_events
        ):
            print("duplicate")
            return 0
        wait = control["runtime"].get("wait")
        if control["runtime"]["state"] != "WAITING_EXTERNAL_EVENT" or not isinstance(wait, dict):
            raise GuardError("runtime is not waiting for an external event")
        if wait.get("kind") != "external_monitor" or wait.get("monitor_id") != monitor_id:
            raise GuardError("wake-monitor does not match the registered external monitor wait")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("waiting runtime lost its active lease")
        verify_frozen_lease(project, lease)
        monitor = monitor_contract(lease, monitor_id)
        evidence = verify_monitor_terminal(project, lease, monitor, wait, requested_event_id)
        receipt_rel = RECEIPTS_REL / lease["lease_id"] / f"{monitor_id}.json"
        receipt_path = project / receipt_rel
        if receipt_path.exists() or receipt_path.is_symlink():
            raise GuardError("controller monitor receipt already exists; refusing overwrite")
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
        atomic_json(receipt_path, project_receipt)
        receipt_sha256 = file_hash(receipt_path)
        lease.setdefault("monitor_receipts", {})[monitor_id] = {
            "path": receipt_rel.as_posix(),
            "sha256": receipt_sha256,
            "source_terminal_sha256": evidence["terminal_sha256"],
            "source_semantic_event_id": evidence["event_id"],
            "source_semantic_event_sha256": evidence["event_sha256"],
        }
        lease["wake_event"] = {
            "event_key": evidence["event_id"],
            "monitor_id": monitor_id,
            "path": receipt_rel.as_posix(),
            "sha256": receipt_sha256,
            "time": iso_time(utc_now()),
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
            "time": iso_time(utc_now()),
        })
        control["active_lease"] = lease
        control["runtime"] = {"state": "ACTIVE", "wait": None, "seen_events": seen[-32:]}
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(lease["monitor_receipts"][monitor_id], ensure_ascii=False, indent=2))
    return 0


def command_wait(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control["runtime"]["state"] == "WAITING_EXTERNAL_EVENT":
            raise GuardError("runtime is already waiting for an external event")
        lease = current_lease(control)
        if lease is None:
            raise GuardError("a live experiment lease is required before waiting")
        verify_frozen_lease(project, lease)
        if lease.get("wake_event") is not None:
            raise GuardError("this lease already consumed its one terminal wake event")
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


def command_checkpoint(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    result_path = Path(args.result).resolve()
    if result_path != (project / RESULT_REL).resolve():
        raise GuardError("checkpoint must use optimization/RESULT.json from the guarded project")
    result = load_json(result_path)
    with state_lock(project):
        gate = load_gate(project)
        if not gate.get("enabled"):
            raise GuardError("gate is not activated")
        control = load_control(project)
        if control["runtime"]["state"] == "WAITING_EXTERNAL_EVENT":
            raise GuardError("wake the registered external event before checkpoint")
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("no active lease to checkpoint")
        verify_frozen_lease(project, lease)
        structured = lease.get("proposal_schema_version") == PROPOSAL_SCHEMA_VERSION
        expected_result_schema = RESULT_SCHEMA_VERSION if structured else 1
        if result.get("schema_version") != expected_result_schema or result.get("experiment_id") != lease["experiment_id"]:
            raise GuardError("result schema or experiment_id does not match active lease")
        decision = result.get("decision")
        outcome = result.get("outcome")
        if decision not in DECISIONS or outcome not in OUTCOMES:
            raise GuardError("result decision or outcome is invalid")
        valid = result.get("valid") is True and result.get("evaluation_integrity") == "PASS"
        core_progress = result.get("core_progress") is True
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
        artifact_results: list[dict[str, str]] = []
        gate_results: list[dict[str, str]] = []
        external_monitor_results: list[dict[str, str]] = []
        if structured:
            artifact_results = verify_artifact_results(project, lease, result.get("artifact_results"), require_all=not preflight_failed)
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
        if valid and core_progress:
            chain["no_progress_count"] = 0
        elif valid:
            chain["no_progress_count"] = int(chain.get("no_progress_count", 0)) + 1
        if chain["no_progress_count"] >= int(gate["max_consecutive_no_progress"]):
            chain["stopline_fired"] = True
            if decision in {"CONTINUE", "REPLICATE"}:
                raise GuardError("no-progress stop line fired; decision must switch, rollback, pause, or complete")
        closes = False if preflight_failed else bool(lease["final_discriminator"]) or decision in {"SWITCH", "ROLLBACK", "COMPLETE"}
        if not preflight_failed and decision == "PAUSE_REQUIRED" and not chain.get("stopline_fired"):
            closes = True
        if chain.get("chain_kind") == "verification" and chain.get("stopline_fired"):
            closes = True
        if closes:
            chain["closed"] = True
            chain["close_outcome"] = outcome
        control["last_checkpoint"] = {
            "experiment_id": lease["experiment_id"], "chain_id": lease["chain_id"],
            "decision": decision, "outcome": outcome, "core_progress": core_progress,
            "preflight_failed": preflight_failed,
            "time": iso_time(utc_now()), "artifact": primary_artifact,
            "artifact_results": artifact_results, "pre_run_gate_results": gate_results,
            "external_monitor_results": external_monitor_results,
        }
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
        if control.get("runtime", {}).get("state") == "WAITING_EXTERNAL_EVENT":
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
            "schema_version": RESULT_SCHEMA_VERSION,
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
        atomic_json(project / RESULT_REL, result)
    return command_checkpoint(argparse.Namespace(project=str(project), result=str(project / RESULT_REL)))


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


def recommended_next_action(gate: dict[str, Any], control: dict[str, Any]) -> dict[str, str]:
    if not gate.get("enabled"):
        return {"kind": "ACTIVATE", "instruction": "Obtain explicit user approval and activate the project gate."}
    runtime = control.get("runtime", {})
    if runtime.get("state") == "WAITING_EXTERNAL_EVENT":
        wait = runtime.get("wait") if isinstance(runtime.get("wait"), dict) else {}
        command = "wake-monitor" if wait.get("kind") == "external_monitor" else "wake"
        if gate_profile(gate) == "fast":
            return {
                "kind": "WAIT",
                "instruction": (
                    f"Continue unattended with read-only bounded polling or process the registered {command} event; "
                    "do not ask the user or mark the Goal blocked/complete merely because the event is pending."
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
    if isinstance(last, dict) and last.get("preflight_failed"):
        if gate_profile(gate) == "fast":
            return {
                "kind": "CONTINUE_FAST",
                "instruction": "Correct the failed preflight path and continue; fast profile does not require an external re-review.",
            }
        return {"kind": "FRESH_REVIEW", "instruction": "Prepare a corrected proposal on the same causal chain and obtain one fresh experiment review."}
    if isinstance(last, dict) and last.get("decision") == "PAUSE_REQUIRED":
        return {"kind": "AWAIT_DECISION", "instruction": "The last valid checkpoint explicitly requires a user decision; do not invent adjacent work."}
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
    return {
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
        "runtime": {"state": control.get("runtime", {}).get("state", "ACTIVE"), "wait": control.get("runtime", {}).get("wait")},
        "next_action": recommended_next_action(gate, control),
        "chains": {key: {field: value.get(field) for field in ("no_progress_count", "stopline_fired", "closed", "close_outcome")} for key, value in chains.items()},
        "last_checkpoint": control.get("last_checkpoint"),
    }


def command_status(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    print(json.dumps(compact_status(project, load_gate(project), load_control(project)), ensure_ascii=False, indent=2))
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


def hook_context(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


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
    return mentions_protected and (MUTATING_SHELL_RE.search(command) is not None or not is_read_only_command(command))


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


def is_fast_bounded_poll(command: str) -> bool:
    stripped = command.strip()
    if not stripped or not POLL_RE.search(stripped):
        return False
    if re.search(r"[\n;&`<>]|\$\(|\|\||&&", stripped):
        return False
    allowed_first = {"squeue", "sacct", "qstat", "kubectl", "docker", "systemctl", "nvidia-smi", "tail", "ps"}
    allowed_filters = {"head", "tail", "grep", "rg", "jq", "wc"}
    stages = stripped.split("|")
    try:
        tokens = [shlex.split(stage) for stage in stages]
    except ValueError:
        return False
    if not tokens or any(not stage for stage in tokens):
        return False
    first = Path(tokens[0][0]).name
    if first not in allowed_first:
        return False
    if first == "kubectl" and (len(tokens[0]) < 2 or tokens[0][1] != "get"):
        return False
    if first == "docker" and (len(tokens[0]) < 2 or tokens[0][1] != "ps"):
        return False
    if first == "systemctl" and (len(tokens[0]) < 2 or tokens[0][1] != "status"):
        return False
    if first == "nvidia-smi":
        mutating_options = {
            "--gpu-reset", "-r", "--reset-ecc-errors", "-pm", "--persistence-mode", "-pl", "--power-limit",
            "-ac", "--applications-clocks",
        }
        if any(
            token == option or token.startswith(option + "=")
            for token in tokens[0][1:]
            for option in mutating_options
        ):
            return False
    if any(Path(stage[0]).name == "rg" and any(token == "--pre" or token.startswith("--pre=") for token in stage[1:]) for stage in tokens[1:]):
        return False
    return all(Path(stage[0]).name in allowed_filters for stage in tokens[1:])


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
        "checkpoint", "abort", "abort-preflight", "activate", "deactivate", "mode",
    }


def is_read_only_mcp(tool_name: str) -> bool:
    lowered = tool_name.casefold()
    mutating = ("write", "edit", "create", "update", "delete", "remove", "move", "copy", "upload", "publish", "deploy", "execute", "run", "send", "apply")
    readable = ("read", "get", "list", "search", "find", "query", "fetch", "open", "view", "inspect", "status", "show")
    return any(word in lowered for word in readable) and not any(word in lowered for word in mutating)


def is_read_only_command(command: str) -> bool:
    stripped = command.strip()
    if not stripped or re.search(r"[\n;&|`<>]|\$\(", stripped) or MUTATING_SHELL_RE.search(stripped) or "--output" in stripped:
        return False
    if stripped.startswith("sed -n") and re.search(r"(?:^|\s)-i(?:\s|$)", stripped):
        return False
    return any(stripped == prefix or stripped.startswith(prefix + " ") for prefix in READ_ONLY_PREFIXES)


def lease_error(project: Path, gate: dict[str, Any], control: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if control.get("runtime", {}).get("state") == "WAITING_EXTERNAL_EVENT":
        return None, "runtime is WAITING_EXTERNAL_EVENT; do not mutate or poll until a deduplicated wake event arrives"
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
    waiting = control.get("runtime", {}).get("state") == "WAITING_EXTERNAL_EVENT"

    if tool == "Bash" and is_controller_command(command):
        return None
    if waiting:
        if tool == "Bash":
            if is_read_only_command(command) or is_fast_bounded_poll(command):
                return None
        if isinstance(tool, str) and tool.startswith("mcp__") and is_read_only_mcp(tool):
            return None
        return fast_deny(
            "the runtime is WAITING_EXTERNAL_EVENT and this call could mutate state before the registered event",
            "Inspect or poll read-only evidence, process the registered wake event, or end this activation until it arrives.",
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
    waiting = control.get("runtime", {}).get("state") == "WAITING_EXTERNAL_EVENT"
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
        if lease.get("proposal_schema_version") == PROPOSAL_SCHEMA_VERSION:
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
        if lease.get("proposal_schema_version") == PROPOSAL_SCHEMA_VERSION:
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
            output: dict[str, Any] | None = None
            if name == "SessionStart":
                status = compact_status(project, gate, control, session_frontier_only=True)
                waiting = control.get("runtime", {}).get("state") == "WAITING_EXTERNAL_EVENT"
                if gate_profile(gate) == "fast":
                    suffix = (
                        " Runtime is WAITING_EXTERNAL_EVENT: read-only inspection and polling are allowed, but mutation must wait for the registered wake event."
                        if waiting else ""
                    )
                    output = hook_context(
                        "Goal Guardrails fast profile is active for unattended execution. Current status: "
                        + json.dumps(status, ensure_ascii=False, separators=(",", ":"))
                        + ". This runtime profile supersedes older project-managed text that says every mutation needs a review-attested lease. "
                        + " Continue routine in-scope local editing, tests, builds, evaluation, diagnostics, recovery, and semantic evidence recording without a lease, external review, or user approval. Controller checkpoint is only for an optional active lease. "
                        + "A tool denial skips only that high-impact action: do not stop the Goal or ask the user merely because of it; record it in BACKLOG.md and continue the next safe action. "
                        + "Request user input only when progress truly requires changing the objective/metric, budget or material scope, executing an irreversible external action, or overriding a fired stop line."
                        + suffix
                    )
                else:
                    suffix = " Runtime is WAITING_EXTERNAL_EVENT: do not poll or start adjacent work; end this activation unless a registered wake event arrived." if waiting else ""
                    output = hook_context(
                        "Goal Guardrails enforcement is active in strict profile. Current gate status: "
                        + json.dumps(status, ensure_ascii=False, separators=(",", ":"))
                        + ". A single denied tool call is a recoverable control transition, not permission to mark the Goal complete or blocked; follow next_action. "
                        + "Do not self-attest proposals; obtain one fresh subagent or user review at the experiment boundary. The controller validates attestation shape, not reviewer identity."
                        + suffix
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
        if args.command == "checkpoint":
            return command_checkpoint(args)
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
