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
import sys
import tempfile
import uuid
from typing import Any, Iterator


SCHEMA_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 2
RESULT_SCHEMA_VERSION = 2
CONTROLLER_PATH = Path(__file__).resolve()
GATE_REL = Path("optimization/GATE.json")
CONTROL_REL = Path("optimization/CONTROL.json")
GOAL_REL = Path("optimization/GOAL.md")
STATE_REL = Path("optimization/STATE.md")
PROPOSAL_REL = Path("optimization/PROPOSAL.json")
RESULT_REL = Path("optimization/RESULT.json")
PRE_RUN_RESULTS_REL = Path("optimization/PRE_RUN_RESULTS.json")
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
GPU_COMMAND_RE = re.compile(r"(?:^|[/_-])(nvidia-smi|torchrun|deepspeed)(?:$|\s)|\baccelerate\s+launch\b|CUDA_VISIBLE_DEVICES", re.I)
POLL_RE = re.compile(r"\b(squeue|sacct|qstat|kubectl\s+get|docker\s+ps|systemctl\s+status|nvidia-smi|tail\b|ps\b)", re.I)
READ_ONLY_PREFIXES = (
    "pwd", "ls", "cat", "rg", "grep", "sed -n", "head", "tail", "wc", "stat", "sha256sum",
    "git status", "git diff", "git log", "git show", "git rev-parse", "git branch --show-current",
    "jq",
)
MUTATING_SHELL_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|mv|cp|install|chmod|chown|mkdir|touch|tee|truncate|dd|git\s+(?:add|commit|push|reset|checkout|switch|merge|rebase|clean))\b|(?:^|\s)(?:>|>>)(?:\s|$)",
    re.I,
)


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
    return gate


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
    if any(relative == protected for protected in PROTECTED_PATHS):
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


def validate_review_attestation(review: Any) -> dict[str, str]:
    if not isinstance(review, dict) or review.get("decision") != "ALLOW":
        raise GuardError("proposal requires an ALLOW review attestation")
    reviewer = ensure_text(review.get("reviewer"), "review.reviewer", maximum=120)
    if not reviewer.startswith(("subagent:", "user:")):
        raise GuardError("review attestation must identify a fresh subagent or user")
    checks = review.get("checks")
    required_checks = {"evidence_sufficient", "lease_mutations_bounded", "pre_run_gates_sufficient", "mutation_not_required_before_admission"}
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        raise GuardError(f"review attestation must affirm checks: {sorted(required_checks)}")
    return {
        "decision": "ALLOW",
        "reviewer": reviewer,
        "reason": ensure_text(review.get("reason"), "review.reason"),
        "checks": {name: True for name in sorted(required_checks)},
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
        if Path(path) in PROTECTED_PATHS or Path(path) == RESULT_REL:
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


def validate_bash_policies(project: Path, raw: Any, mutations: list[dict[str, Any]], gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        raw_args = item.get("fixed_args")
        if not isinstance(raw_args, list):
            raise GuardError(f"bash_policies[{index}].fixed_args must be a list")
        fixed_args = [ensure_text(value, f"bash_policies[{index}].fixed_arg", maximum=240) for value in raw_args]
        if any(re.search(r"[\n;&|`<>]", value) for value in fixed_args):
            raise GuardError("Bash fixed arguments cannot contain shell control characters")
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
            if not any(argument == output_argument or argument.endswith("=" + output_argument) for argument in fixed_args):
                raise GuardError(f"Bash output path must be frozen in fixed_args: {output}")
        resources = item.get("resources")
        if not isinstance(resources, dict):
            raise GuardError(f"bash_policies[{index}].resources must be an object")
        gpu = int(resources.get("gpu", 0))
        if not 0 <= gpu <= 64:
            raise GuardError("Bash policy GPU count must be 0..64")
        command_text = " ".join([executable, *fixed_args])
        if gpu_limit is not None and (gpu > gpu_limit or (gpu_limit == 0 and GPU_COMMAND_RE.search(command_text))):
            raise GuardError(f"Bash policy {policy_id} conflicts with the pre-run GPU gate")
        policies.append({
            "id": policy_id,
            "phase": phase,
            "executable": executable,
            "fixed_args": fixed_args,
            "cwd": cwd,
            "output_paths": outputs,
            "resources": {"gpu": gpu},
        })
    return sorted(policies, key=lambda item: item["id"])


def validate_proposal(project: Path, gate: dict[str, Any], control: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    if proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        raise GuardError(f"proposal schema_version must be {PROPOSAL_SCHEMA_VERSION}")
    if isinstance(control.get("active_lease"), dict):
        raise GuardError("an experiment lease is still active or awaiting checkpoint")
    if state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
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
    bash_policies = validate_bash_policies(project, proposal.get("bash_policies"), lease_mutations, pre_run_gates)
    reviewer = validate_review_attestation(proposal.get("review"))
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
    if not 1 <= minutes <= 240 or not 1 <= mutations <= 50:
        raise GuardError("lease minutes must be 1..240 and mutations must be 1..50")
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
        "allowed_tool_names": [],
        "issued_at": iso_time(now),
        "expires_at": iso_time(now + timedelta(minutes=minutes)),
        "max_mutations": mutations,
        "mutations_used": 0,
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


def verify_gate_results(lease: dict[str, Any], raw: Any, artifacts: list[dict[str, str]]) -> list[dict[str, str]]:
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
    if missing or failed:
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
        gates = verify_gate_results(lease, payload.get("pre_run_gate_results"), artifacts)
        existing = lease.get("pre_run_gate_results")
        if isinstance(existing, dict):
            if canonical_hash({"gates": gates, "artifacts": artifacts}) != canonical_hash({"gates": existing.get("gates"), "artifacts": existing.get("artifacts")}):
                raise GuardError("pre-run gate results are already frozen and a different replay is rejected")
            print(json.dumps(existing, ensure_ascii=False, indent=2))
            return 0
        lease["pre_run_gate_results"] = {"gates": gates, "artifacts": artifacts, "recorded_at": iso_time(utc_now())}
        control["active_lease"] = lease
        save_control(project, control)
    print(json.dumps(lease["pre_run_gate_results"], ensure_ascii=False, indent=2))
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
        artifact_results: list[dict[str, str]] = []
        gate_results: list[dict[str, str]] = []
        if structured:
            artifact_results = verify_artifact_results(project, lease, result.get("artifact_results"), require_all=True)
            gate_results = verify_gate_results(lease, result.get("pre_run_gate_results"), artifact_results)
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
                wake_artifact = next((item for item in artifact_results if item["id"] == wake_event.get("artifact_id")), None)
                if not isinstance(wake_artifact, dict) or wake_artifact.get("path") != wake_event.get("path") or wake_artifact.get("sha256") != wake_event.get("sha256"):
                    raise GuardError("checkpoint terminal artifact does not match the frozen wake event")
        decision = result.get("decision")
        outcome = result.get("outcome")
        if decision not in DECISIONS or outcome not in OUTCOMES:
            raise GuardError("result decision or outcome is invalid")
        valid = result.get("valid") is True and result.get("evaluation_integrity") == "PASS"
        core_progress = result.get("core_progress") is True
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
        closes = bool(lease["final_discriminator"]) or decision in {"SWITCH", "ROLLBACK", "COMPLETE"}
        if decision == "PAUSE_REQUIRED" and not chain.get("stopline_fired"):
            closes = True
        if chain.get("chain_kind") == "verification" and chain.get("stopline_fired"):
            closes = True
        if closes:
            chain["closed"] = True
            chain["close_outcome"] = outcome
        control["last_checkpoint"] = {
            "experiment_id": lease["experiment_id"], "chain_id": lease["chain_id"],
            "decision": decision, "outcome": outcome, "core_progress": core_progress,
            "time": iso_time(utc_now()), "artifact": primary_artifact,
            "artifact_results": artifact_results, "pre_run_gate_results": gate_results,
        }
        control["active_lease"] = None
        control["poll"] = None
        save_control(project, control)
    print(json.dumps(control["last_checkpoint"], ensure_ascii=False, indent=2))
    return 0


def command_toggle(args: argparse.Namespace, enabled: bool) -> int:
    if args.approved_by != "user":
        raise GuardError("activation changes require --approved-by user")
    project = explicit_project(args.project)
    with state_lock(project):
        gate = load_gate(project)
        if enabled:
            if state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
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
        "active_experiment": lease.get("experiment_id") if lease else None,
        "active_chain": lease.get("chain_id") if lease else None,
        "lease_expires_at": lease.get("expires_at") if lease else None,
        "lease_valid": lease_valid if lease else None,
        "mutations": f"{lease.get('mutations_used')}/{lease.get('max_mutations')}" if lease else None,
        "runtime": {"state": control.get("runtime", {}).get("state", "ACTIVE"), "wait": control.get("runtime", {}).get("wait")},
        "chains": {key: {field: value.get(field) for field in ("no_progress_count", "stopline_fired", "closed", "close_outcome")} for key, value in chains.items()},
        "last_checkpoint": control.get("last_checkpoint"),
    }


def command_status(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    print(json.dumps(compact_status(project, load_gate(project), load_control(project)), ensure_ascii=False, indent=2))
    return 0


def deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


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
        if tokens == [policy["executable"], *policy["fixed_args"]] and actual_cwd == policy["cwd"]:
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
    return script == CONTROLLER_PATH and tokens[2] in {"status", "admit", "gates", "wait", "wake", "checkpoint", "activate", "deactivate"}


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
    if state_nonblank_lines(project) > int(gate["state_max_nonblank_lines"]):
        return None, "STATE.md exceeds its cap; compact the frontier before more work"
    if int(lease.get("mutations_used", 0)) >= int(lease.get("max_mutations", 0)):
        return None, "experiment mutation allowance is exhausted; checkpoint before continuing"
    if raw is not lease:
        return None, "experiment lease is invalid"
    return lease, None


def hook_pre_tool(project: Path, event: dict[str, Any], gate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any] | None:
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
        if isinstance(raw_lease, dict) and all(path in FINALIZATION_PATHS for path in targets) and not raw_lease.get("finalization_used"):
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
        if any(path in PROTECTED_PATHS for path in targets):
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
            required_gates = [gate for gate in lease.get("pre_run_gates", []) if gate.get("required")]
            if policy["phase"] != "preparation" and required_gates and lease.get("pre_run_gate_results") is None:
                return deny("required pre-run gates are not recorded; run the controller gates command before workload or postflight")
            if policy["phase"] == "preparation" and lease.get("pre_run_gate_results") is not None:
                return deny("preparation phase is closed after pre-run gates are recorded")
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
                suffix = " Runtime is WAITING_EXTERNAL_EVENT: do not poll or start adjacent work; end this activation unless a registered wake event arrived." if waiting else ""
                output = hook_context("Goal Guardrails enforcement is active. Current gate status: " + json.dumps(status, ensure_ascii=False, separators=(",", ":")) + ". Do not self-attest proposals; obtain a fresh subagent or user review before admission. The controller validates attestation shape, not reviewer identity." + suffix)
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
            print(json.dumps(deny(f"Goal Guardrails failed closed: {error}"), ensure_ascii=False))
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
    gates = sub.add_parser("gates")
    gates.add_argument("results")
    gates.add_argument("--project")
    wait = sub.add_parser("wait")
    wait.add_argument("--event-key", required=True)
    wait.add_argument("--event-path", required=True)
    wait.add_argument("--project")
    wake = sub.add_parser("wake")
    wake.add_argument("--event-key", required=True)
    wake.add_argument("--event-path", required=True)
    wake.add_argument("--project")
    for name in ("activate", "deactivate"):
        item = sub.add_parser(name)
        item.add_argument("--project")
        item.add_argument("--approved-by", required=True)
        item.add_argument("--reason", default="explicit user-approved gate change")
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
        if args.command == "wait":
            return command_wait(args)
        if args.command == "wake":
            return command_wake(args)
        if args.command == "checkpoint":
            return command_checkpoint(args)
        if args.command == "activate":
            return command_toggle(args, True)
        if args.command == "deactivate":
            return command_toggle(args, False)
        raise GuardError("unknown command")
    except (GuardError, OSError, UnicodeError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
