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
CONTROLLER_PATH = Path(__file__).resolve()
GATE_REL = Path("optimization/GATE.json")
CONTROL_REL = Path("optimization/CONTROL.json")
GOAL_REL = Path("optimization/GOAL.md")
STATE_REL = Path("optimization/STATE.md")
PROPOSAL_REL = Path("optimization/PROPOSAL.json")
RESULT_REL = Path("optimization/RESULT.json")
EXPERIMENTS_REL = Path("optimization/EXPERIMENTS.md")
BACKLOG_REL = Path("optimization/BACKLOG.md")
ALWAYS_LEASE_PATHS = (STATE_REL, RESULT_REL, EXPERIMENTS_REL, BACKLOG_REL)
FINALIZATION_PATHS = (STATE_REL, RESULT_REL, EXPERIMENTS_REL)
PROTECTED_PATHS = (GOAL_REL, GATE_REL, CONTROL_REL)
DECISIONS = {"CONTINUE", "REPLICATE", "SWITCH", "ROLLBACK", "PAUSE_REQUIRED", "COMPLETE"}
OUTCOMES = {"positive", "negative", "zero_progress", "inconclusive", "invalid"}
CHAIN_KINDS = {"optimization", "diagnostic", "verification"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
POLL_RE = re.compile(r"\b(squeue|sacct|qstat|kubectl\s+get|docker\s+ps|systemctl\s+status|nvidia-smi|tail\b|ps\b)", re.I)
READ_ONLY_PREFIXES = (
    "pwd", "ls", "rg", "grep", "sed -n", "head", "tail", "wc", "stat", "sha256sum",
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
    return {"schema_version": 1, "active_lease": None, "chains": {}, "poll": None, "last_checkpoint": None}


def load_control(project: Path) -> dict[str, Any]:
    path = project / CONTROL_REL
    if not path.exists():
        return default_control()
    control = load_json(path)
    if control.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("unsupported CONTROL.json schema_version")
    if not isinstance(control.get("chains"), dict):
        raise GuardError("CONTROL.json chains must be an object")
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
    return {"decision": "ALLOW", "reviewer": reviewer, "reason": ensure_text(review.get("reason"), "review.reason")}


def validate_proposal(project: Path, gate: dict[str, Any], control: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    if proposal.get("schema_version") != SCHEMA_VERSION:
        raise GuardError("unsupported proposal schema_version")
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

    raw_paths = proposal.get("allowed_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise GuardError("allowed_paths must be a non-empty list")
    allowed_paths = sorted(set(normalize_relative(project, item) for item in raw_paths))
    raw_prefixes = proposal.get("allowed_command_prefixes")
    if not isinstance(raw_prefixes, list) or not raw_prefixes:
        raise GuardError("allowed_command_prefixes must be a non-empty list")
    prefixes = [ensure_text(item, "allowed_command_prefix", maximum=240) for item in raw_prefixes]
    if any(re.search(r"[\n;&|`<>]|\$\(", item) for item in prefixes):
        raise GuardError("allowed command prefixes must be single-command prefixes")
    if any(re.match(r"^(?:ba|z|c|fi)?sh\b|^env\b|^python3?\s+-c\b", item) for item in prefixes):
        raise GuardError("shell interpreters, env, and inline Python cannot be allowed command prefixes")
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
        "allowed_paths": allowed_paths,
        "allowed_command_prefixes": prefixes,
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


def command_checkpoint(args: argparse.Namespace) -> int:
    project = explicit_project(args.project)
    result_path = Path(args.result).resolve()
    if result_path != (project / RESULT_REL).resolve():
        raise GuardError("checkpoint must use optimization/RESULT.json from the guarded project")
    result = load_json(result_path)
    with state_lock(project):
        gate = load_gate(project)
        control = load_control(project)
        lease = control.get("active_lease")
        if not isinstance(lease, dict):
            raise GuardError("no active lease to checkpoint")
        if result.get("schema_version") != 1 or result.get("experiment_id") != lease["experiment_id"]:
            raise GuardError("result schema or experiment_id does not match active lease")
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
        ensure_text(result.get("artifact"), "artifact")
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
            "time": iso_time(utc_now()), "artifact": result["artifact"],
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


def patch_paths(project: Path, cwd: Path, patch: str) -> list[Path]:
    raw_paths = PATCH_PATH_RE.findall(patch) + PATCH_MOVE_RE.findall(patch)
    if not raw_paths:
        raise GuardError("cannot determine apply_patch target paths")
    return [relative_tool_path(project, cwd, raw) for raw in raw_paths]


def path_allowed(path: Path, allowed: list[str]) -> bool:
    for raw in allowed:
        base = Path(raw)
        if path == base or base in path.parents:
            return True
    return False


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
    return script == CONTROLLER_PATH and tokens[2] in {"status", "admit", "checkpoint", "activate", "deactivate"}


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
    raw = control.get("active_lease")
    lease = current_lease(control)
    if lease is None:
        return None, "experiment lease is missing or expired; obtain a fresh review-attested lease before mutating work"
    if file_hash(project / GOAL_REL) != lease.get("goal_sha256"):
        return None, "GOAL.md changed after admission; the lease is invalid and must be reviewed again"
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
    if tool == "Bash" and (is_controller_command(command) or is_read_only_command(command)):
        poll = control.get("poll")
        if poll and poll.get("blocked_command_sha256") == hashlib.sha256(command.strip().encode()).hexdigest():
            return deny("unchanged polling limit reached; wait for a semantic event or checkpoint instead of repeating the same status query")
        return None
    if isinstance(tool, str) and tool.startswith("mcp__") and is_read_only_mcp(tool):
        return None
    if tool == "apply_patch":
        try:
            targets = patch_paths(project, cwd, command)
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
            targets = patch_paths(project, cwd, command)
        except GuardError as exc:
            return deny(str(exc))
        allowed = list(lease["allowed_paths"]) + [path.as_posix() for path in ALWAYS_LEASE_PATHS]
        blocked = [path.as_posix() for path in targets if not path_allowed(path, allowed)]
        if blocked:
            return deny(f"patch targets are outside the admitted lease: {blocked}")
        if any(path in PROTECTED_PATHS for path in targets):
            return deny("contract and gate-control files cannot be changed under an experiment lease")
    elif tool == "Bash":
        if re.search(r"[\n;&|`<>]|\$\(", command):
            return deny("compound shell commands and redirections are outside a bounded lease; use one reviewed wrapper command")
        if not any(command.strip() == prefix or command.strip().startswith(prefix + " ") for prefix in lease["allowed_command_prefixes"]):
            return deny("Bash command is outside the admitted command prefixes")
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
                output = hook_context("Goal Guardrails enforcement is active. Current gate status: " + json.dumps(status, ensure_ascii=False, separators=(",", ":")) + ". Do not self-attest proposals; obtain a fresh subagent or user review before admission. The controller validates attestation shape, not reviewer identity.")
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
