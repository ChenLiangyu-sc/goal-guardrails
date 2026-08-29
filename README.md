# Goal Guardrails

Goal Guardrails keeps long-running, metric-driven optimization from drifting into low-contribution cleanup, repeated diagnostics, and ever-heavier execution paths.

It applies beyond model training: prompt quality, performance, latency, cost, reliability, search, recommendation, conversion, data pipelines, and other optimization with a measurable objective and evaluator.

Version 0.2 combines:

```text
GOAL.md + STATE.md
        -> fresh proposal review + attestation
        -> one bounded experiment lease
        -> Codex PreToolUse/PostToolUse hooks
        -> RESULT.json checkpoint
        -> continue / switch / rollback / pause / complete
```

The Markdown files remain readable project memory. A small JSON state machine enforces lease expiry, mutation count, allowed paths and Bash commands, stable causal-chain identity, no-progress stop lines, non-core allowance, and repeated-poll limits. There is no database, dashboard, resident service, or per-command LLM review.

## Install as a Codex plugin

Add the public marketplace:

```bash
codex plugin marketplace add ChenLiangyu-sc/goal-guardrails
```

Install **Goal Guardrails** from the Plugins Directory, then open `/hooks` and review/trust its hook definition. Plugin installation alone does not trust non-managed hooks.

The repository remains compatible with standalone Skill installation, but standalone mode cannot enforce lifecycle hooks:

```text
$skill-installer install goal-guardrails from https://github.com/ChenLiangyu-sc/goal-guardrails
```

## Initialize

From an optimization repository:

```text
$goal-guardrails init
```

The initializer creates missing files under `optimization/` and additively inserts one marked block into `AGENTS.md`. Existing files are never overwritten. `optimization/GATE.json` starts with `enabled: false`.

After the metric, evaluator, budgets, state, and stop rules are concrete, explicitly approve activation:

```bash
python3 <plugin-root>/hooks/goal_guard.py activate --approved-by user --project .
```

The Skill resolves `<plugin-root>` when running inside Codex. Confirm `/hooks` shows the plugin hook as trusted before relying on enforcement.

## Run an experiment

The short user command remains:

```text
$goal-guardrails run
```

The workflow then:

1. Restores `GOAL.md`, `STATE.md`, and recent evidence.
2. Writes one bounded `PROPOSAL.json`.
3. Obtains one fresh read-only subagent or user review.
4. Admits the proposal to create a temporary lease.
5. Allows only bounded mutation while the lease is live.
6. Writes `RESULT.json` and checkpoints the lease.

Manual controller commands are:

```bash
python3 <plugin-root>/hooks/goal_guard.py status --project .
python3 <plugin-root>/hooks/goal_guard.py admit optimization/PROPOSAL.json --project .
python3 <plugin-root>/hooks/goal_guard.py checkpoint optimization/RESULT.json --project .
```

## What the hook blocks

- mutation without a live review-attested lease;
- edits outside admitted paths;
- Bash outside admitted command prefixes;
- all MCP writes and unknown MCP operations (until a parameter-level scope adapter exists);
- work after expiry or mutation-budget exhaustion;
- reuse of a closed chain or renaming the same causal bottleneck;
- repeated non-core leases beyond the configured allowance;
- contract changes after admission;
- continued work when `STATE.md` exceeds its cap;
- identical polling after repeated unchanged results.

Read-only inspection stays available. `PROPOSAL.json` can be prepared before a lease. `GOAL.md`, `GATE.json`, and `CONTROL.json` are protected during experiments.

## Boundaries

This is a strong behavioral guardrail, not a security sandbox. The workflow requires a genuinely fresh subagent or user review, but the local controller can validate only the recorded attestation's shape; it cannot authenticate who produced it. `--approved-by user` likewise records an explicit-approval attestation and must be used only after the user actually approves. Cryptographically or administratively unforgeable approval requires a managed hook or external trusted service.

Shell effects cannot be inferred perfectly, hosted tools may not traverse local lifecycle hooks, read-only MCP classification is conservative name-based policy, and users can disable non-managed hooks. MCP mutation currently fails closed; perform scoped project writes through `apply_patch` or an admitted Bash command. A hook denial must be treated as a decision boundary rather than an invitation to find another execution path.

## Validation

```bash
python3 -m unittest discover -s skills/goal-guardrails/scripts -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 /path/to/skill-creator/scripts/quick_validate.py skills/goal-guardrails
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```

## License

MIT
