---
name: goal-guardrails
description: Initialize, run, resume, or audit anti-drift guardrails for long-running metric-driven optimization. Use for iterative work with a measurable objective, evidence loop, budget, and stopping rule, including model quality, prompts, performance, latency, cost, reliability, search, recommendation, conversion, and system tuning. When installed as a plugin, enforce review-attested experiment leases through Codex lifecycle hooks. Do not use for ordinary one-off changes, goals without a measurable evaluation, or general project management.
---

# Goal Guardrails

Keep the optimization target stable while making the next experiment small, measurable, and worth its cost. Project Markdown remains the human-readable contract and evidence. When plugin hooks are available, use the small lease controller as the execution gate; do not replace it with a database, dashboard, scheduler, or per-tool model review.

## Choose the mode

- **`init` — initialize or adopt:** scaffold the protocol, fill supported facts, then activate enforcement only with explicit user approval.
- **`run` — run or resume:** restore the frontier, obtain a fresh proposal review, attest it, admit one lease, execute it, and checkpoint the result.
- **`audit` — audit:** inspect alignment and report evidence without changing code or launching work unless the user also requests changes.

Read [references/operating-protocol.md](references/operating-protocol.md) before running, resuming, or auditing an optimization. Initialization alone can follow the steps below.

## Initialize safely

Run the initializer from the repository root:

```bash
python3 <skill-dir>/scripts/init_project.py .
```

Use `--dry-run` first for multiple repositories. It creates only missing templates and additively inserts one marked block in `AGENTS.md`; existing optimization files are never overwritten. `GATE.json` starts disabled, so scaffolding cannot unexpectedly block work.

After scaffolding:

1. Inspect the repository's implementation, evaluation, and project documentation.
2. Fill only values supported by code, configs, artifacts, or user instructions. Preserve `TODO` for unknown facts.
3. Do not launch expensive work until the primary metric, baseline, stopping rule, evaluator, guardrails, and budgets are usable.
4. Keep `STATE.md` within its declared cap.
5. Confirm that the plugin hook is installed and trusted through `/hooks`.
6. Only after explicit user approval, activate the gate:

```bash
python3 <plugin-root>/hooks/goal_guard.py activate --approved-by user --project .
```

Never claim enforcement is active when only the standalone Skill is installed. Return a short `/goal` command pointing to `optimization/GOAL.md`; do not copy the contract into the Goal.

## Run one bounded loop

Before selecting work, read `GOAL.md`, `STATE.md`, the recent relevant experiment rows, and the backlog only to avoid repeating deferred work. Keep at most three candidates and select exactly one experiment.

Write the bounded proposal to `optimization/PROPOSAL.json`. Obtain one fresh read-only reviewer decision before every admission; this is deliberately once per experiment rather than once per tool call. Use a subagent when available and give it only the contract, frontier, recent evidence, and proposal; do not reveal a desired verdict. It must return exactly one of:

```text
ALLOW | REJECT_TO_BACKLOG | SWITCH_CHAIN | PAUSE_REQUIRED
```

If no reviewer independent of the main agent is available, ask the user instead of self-approving. Record an allowed reviewer as `subagent:<id>` or `user:<id>` with a short reason. This is a behavioral attestation: the controller validates its shape but cannot authenticate its author. Record it only after the review actually occurred. Review once per proposed experiment, not once per command.

Admit an allowed proposal through the controller:

```bash
python3 <plugin-root>/hooks/goal_guard.py admit optimization/PROPOSAL.json --project .
```

The lease bounds the experiment ID, stable chain, causal bottleneck, file paths, Bash prefixes, expiry, mutation count, work class, and cost. Inspection can proceed without a lease. Mutating work must stay inside it. MCP writes and unknown MCP operations currently fail closed because the controller cannot safely infer their path effects; use scoped `apply_patch` or an admitted Bash command. Never edit `GOAL.md`, `GATE.json`, or `CONTROL.json` to bypass a denial, and do not switch tool paths to evade a hook.

After evaluation, write `optimization/RESULT.json`, update the concise Markdown evidence, and checkpoint:

```bash
python3 <plugin-root>/hooks/goal_guard.py checkpoint optimization/RESULT.json --project .
```

Do not begin a new experiment before the prior lease is checkpointed. A failed, expired, or exhausted lease is a decision boundary, not permission to work outside the controller.

## Stop unproductive chains

Track the **core progress unit**, stable chain ID and causal bottleneck, and consecutive valid experiments with no core progress. A normally completed frozen evaluator reporting failure or zero yield is a valid no-progress result. Renaming a component or moving among internal contracts cannot reset the chain.

At the configured no-progress limit, switch, roll back, pause, or use the single predeclared final discriminator. That discriminator closes the diagnostic chain regardless of outcome. A positive result may enter only a separately named verification child restricted to replication, applicable validation, promotion, or rollback.

Waiting does not authorize cleanup. Repeated status recovery, identity/SHA checks, schema proofs, monitoring, and reviewer passes are non-core unless they restore evaluation integrity. The plugin blocks the same polling command after the configured number of unchanged results; resume at a semantic event instead of changing the command to evade it.

## Preserve the boundary

- Findings outside the metric, guardrails, evaluation integrity, or an admitted blocker go to `BACKLOG.md`.
- Infrastructure and refactoring are allowed only when they block the admitted experiment now, no smaller workaround exists, and work stops when the blocker is removed.
- Never change the metric, target, evaluator, frozen workload, holdout rule, guardrails, or budget merely to show progress.
- Budget exhaustion is `PAUSE_REQUIRED`, never `COMPLETE`.
- Keep `STATE.md` as a replaceable frontier snapshot; raw detail belongs in artifacts.
- Do not disable the gate, revise its limits, or deactivate hooks without explicit user approval.
- Do not create or activate a Codex Goal unless the user explicitly asks.

## Enforcement boundary

Hooks reduce accidental drift; they are not a security sandbox. Review and activation fields are attestations rather than authenticated identities. Bash effects cannot be inferred perfectly, hosted tools may not pass through lifecycle hooks, MCP read-only classification is conservative and name-based, and project/plugin hooks require trust. If repeated intentional bypass remains, pause and ask the user before proposing managed hooks or an external orchestrator.
