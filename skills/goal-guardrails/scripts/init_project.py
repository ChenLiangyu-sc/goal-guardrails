#!/usr/bin/env python3
"""Safely scaffold lightweight goal guardrails in repositories."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


SKILL_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = SKILL_ROOT / "assets"
TARGETS = {
    "GOAL.template.md": Path("optimization/GOAL.md"),
    "STATE.template.md": Path("optimization/STATE.md"),
    "EXPERIMENTS.template.md": Path("optimization/EXPERIMENTS.md"),
    "BACKLOG.template.md": Path("optimization/BACKLOG.md"),
    "GATE.template.json": Path("optimization/GATE.json"),
    "CONTROL.template.json": Path("optimization/CONTROL.json"),
    "PROPOSAL.template.json": Path("optimization/PROPOSAL.json"),
    "PRE_RUN_RESULTS.template.json": Path("optimization/PRE_RUN_RESULTS.json"),
    "RESULT.template.json": Path("optimization/RESULT.json"),
}
AGENTS_FRAGMENT = ASSET_DIR / "AGENTS.fragment.md"
START_MARKER = "<!-- goal-guardrails:start -->"
END_MARKER = "<!-- goal-guardrails:end -->"
LEGACY_START_MARKER = "<!-- codex-model-optimization-guardrails:start -->"
LEGACY_END_MARKER = "<!-- codex-model-optimization-guardrails:end -->"
MARKER_PAIRS = (
    (START_MARKER, END_MARKER),
    (LEGACY_START_MARKER, LEGACY_END_MARKER),
)


class InitError(RuntimeError):
    """A fail-closed initialization error."""


Action = tuple[str, Path, bytes | None]


def atomic_write(path: Path, content: bytes, preserve_from: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if preserve_from is None:
            os.chmod(temporary, 0o644)
        else:
            source_stat = preserve_from.stat(follow_symlinks=False)
            os.chown(temporary, source_stat.st_uid, source_stat.st_gid)
            shutil.copystat(preserve_from, temporary, follow_symlinks=False)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def normalize_project(project: Path) -> Path:
    project = project.expanduser()
    project = Path(os.path.abspath(os.fspath(project)))
    if project.is_symlink():
        raise InitError(f"project root must not be a symbolic link: {project}")
    if not project.exists():
        raise InitError(f"project does not exist: {project}")
    if not project.is_dir():
        raise InitError(f"project is not a directory: {project}")
    return project


def plan_project(project: Path) -> list[Action]:
    project = normalize_project(project)
    optimization = project / "optimization"
    if optimization.is_symlink():
        raise InitError(f"optimization directory must not be a symbolic link: {optimization}")
    if optimization.exists() and not optimization.is_dir():
        raise InitError(f"optimization path is not a directory: {optimization}")

    actions: list[Action] = []
    for asset_name, relative_target in TARGETS.items():
        target = project / relative_target
        if target.is_symlink():
            raise InitError(f"template target must not be a symbolic link: {target}")
        if target.exists():
            if not target.is_file():
                raise InitError(f"template target is not a regular file: {target}")
            actions.append(("skip-existing", target, None))
        else:
            content = (ASSET_DIR / asset_name).read_bytes()
            if asset_name == "GATE.template.json":
                gate = json.loads(content.decode("utf-8"))
                gate["project_root"] = os.fspath(project)
                content = (json.dumps(gate, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            actions.append(("create", target, content))

    agents = project / "AGENTS.md"
    if agents.is_symlink():
        raise InitError(f"AGENTS.md must not be a symbolic link: {agents}")
    if agents.exists() and not agents.is_file():
        raise InitError(f"AGENTS.md is not a regular file: {agents}")
    existing = agents.read_bytes() if agents.exists() else b""
    complete_marker_found = False
    for start_text, end_text in MARKER_PAIRS:
        has_start = start_text.encode("utf-8") in existing
        has_end = end_text.encode("utf-8") in existing
        if has_start != has_end:
            raise InitError(f"AGENTS.md contains an incomplete guardrails marker block: {agents}")
        complete_marker_found = complete_marker_found or has_start
    if complete_marker_found:
        actions.append(("skip-marked", agents, None))
    else:
        newline = b"\r\n" if b"\r\n" in existing else b"\n"
        fragment = AGENTS_FRAGMENT.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n")
        fragment = fragment.replace(b"\n", newline) + newline
        prefix = existing
        if prefix and not prefix.endswith((b"\n", b"\r")):
            prefix += newline
        if prefix and not prefix.endswith(newline + newline):
            prefix += newline
        actions.append(("create" if not agents.exists() else "append", agents, prefix + fragment))
    return actions


def apply_actions(project: Path, actions: list[Action], dry_run: bool) -> list[str]:
    messages: list[str] = []
    if dry_run:
        return [f"{action}: {target}" for action, target, _ in actions]

    created: list[Path] = []
    optimization_existed = (project / "optimization").exists()
    try:
        for action, target, content in actions:
            messages.append(f"{action}: {target}")
            if content is None:
                continue
            preserve_from = target if action == "append" else None
            atomic_write(target, content, preserve_from=preserve_from)
            if action == "create":
                created.append(target)
    except OSError as error:
        rollback_errors: list[str] = []
        for target in reversed(created):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"{target}: {rollback_error}")
        optimization = project / "optimization"
        if not optimization_existed and optimization.exists():
            try:
                optimization.rmdir()
            except OSError:
                pass
        suffix = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise InitError(f"initialization failed and project changes were rolled back: {error}{suffix}") from error
    return messages


def apply_project(project: Path, dry_run: bool) -> list[str]:
    project = normalize_project(project)
    return apply_actions(project, plan_project(project), dry_run)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create missing optimization templates and add an idempotent AGENTS.md policy block."
    )
    parser.add_argument("projects", nargs="*", default=["."], help="Explicit project directories (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Print intended actions without modifying files")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    planned: list[tuple[Path, list[Action]]] = []
    for raw_project in args.projects:
        try:
            project = normalize_project(Path(raw_project))
            planned.append((project, plan_project(project)))
        except (InitError, OSError, UnicodeError) as error:
            print(f"preflight error: {error}", file=sys.stderr)
            return 2

    failures = 0
    completed = 0
    for project, actions in planned:
        print(f"[{project}]")
        try:
            for message in apply_actions(project, actions, args.dry_run):
                print(message)
        except (InitError, OSError, UnicodeError) as error:
            failures += 1
            print(f"error: {error}", file=sys.stderr)
        else:
            completed += 1
    if failures and completed:
        print(
            f"partial batch: {completed} project(s) completed and {failures} project(s) failed; "
            "completed projects were not rolled back",
            file=sys.stderr,
        )
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
