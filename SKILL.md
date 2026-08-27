---
name: goal-guardrails
description: Initialize, run, resume, or audit a lightweight anti-drift workflow for long-running, metric-driven optimization. Use for iterative work with a measurable objective, evidence loop, budget, and stopping rule, including model quality, prompts, performance, latency, cost, reliability, search, recommendation, conversion, and system tuning. Do not use for ordinary one-off changes, goals without a measurable evaluation, or general project management.
---

# Goal Guardrails

Keep the optimization target stable while making the next experiment small, measurable, and worth its cost. Start with project-local files and soft governance; do not build a controller, database, hook, reviewer agent, or dashboard unless observed failures justify it.

## Choose the mode

- **`init` — initialize or adopt:** scaffold the protocol, then fill only facts supported by the repository and the user.
- **`run` — run or resume:** restore the execution frontier, admit one experiment, execute it, and checkpoint the result.
- **`audit` — audit:** inspect alignment and report evidence without changing code or launching work unless the user also requests changes.

Read [references/operating-protocol.md](references/operating-protocol.md) before running, resuming, or auditing an optimization. Initialization alone can follow the steps below.

## Initialize safely

Run the deterministic initializer from the repository root:

```bash
python3 <skill-dir>/scripts/init_project.py .
```

Use `--dry-run` first for multiple repositories. The script only creates missing templates and additively inserts one marked block in `AGENTS.md`; it never overwrites existing optimization files.

After scaffolding:

1. Inspect the repository's implementation, measurement/evaluation, and project documentation.
2. Fill values that are directly supported by code, configs, artifacts, or user instructions.
3. Preserve `TODO` for unknown values. Never invent a baseline, target, metric, budget, input/data/workload version, or evaluation command.
4. Ask only for unresolved facts that materially change the optimization contract.
5. Do not launch expensive work until the primary metric, baseline, target or stopping rule, measurement/evaluation command, guardrails, and budget are usable.
6. Return a short `/goal` command that points at `optimization/GOAL.md`; do not copy the full contract into the Goal.

When adopting a repository that already has similar files, preserve them. Use the templates as a checklist and make additive edits only when the user asked to integrate the protocol.

## Run one bounded loop

Before selecting work, read:

1. `optimization/GOAL.md` completely.
2. `optimization/STATE.md` completely.
3. The most recent relevant rows in `optimization/EXPERIMENTS.md`.
4. `optimization/BACKLOG.md` only to avoid repeating deferred work; backlog presence never grants execution priority.

Then follow the operating protocol. Keep at most three active candidates and admit exactly one next experiment. Prefer measured evidence, fast falsification, information gain, lower cost, reversibility, and a single primary change. Do not use a numerical priority formula based on the model's self-reported confidence.

After a valid result, record the fact, update the short state, and choose exactly one decision:

```text
CONTINUE | REPLICATE | SWITCH | ROLLBACK | PAUSE_REQUIRED | COMPLETE
```

Waiting for a long-running experiment does not authorize cleanup or unrelated improvements. Use an existing deterministic monitor when available; do not introduce a new monitoring system through this skill.

## Preserve the boundary

- New findings that do not directly serve the metric, a guardrail, evaluation integrity, or an admitted blocker go to `optimization/BACKLOG.md`.
- Infrastructure, refactoring, documentation, tooling, and general cleanup are allowed only when they directly block the highest-value experiment, no smaller workaround exists, and the change is limited to removing that blocker.
- Never change the primary metric, target, evaluator, frozen inputs/workload, reserved-validation policy, guardrails, or budget merely to show progress.
- Budget exhaustion is `PAUSE_REQUIRED`, never `COMPLETE`.
- A finished process is transport evidence, not optimization-success evidence. Apply the frozen evaluation and guardrails before promotion.
- Do not create or activate a Codex Goal unless the user explicitly asks to start or follow one. Otherwise, provide the ready-to-copy `/goal` text.

## Escalate only from evidence

Recommend a wrapper, hook, read-only reviewer, or deterministic controller only after observed failures such as repeated gate bypass, repeated false completion, non-core spend remaining above the agreed threshold, or one mistaken experiment costing more than the proposed control. Keep the existing project files as the contract and evidence layer if escalation becomes necessary.
