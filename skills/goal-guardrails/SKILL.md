---
name: goal-guardrails
description: Initialize, run, resume, or audit fast anti-drift guardrails for unattended metric-driven optimization. Use for long-running iterative work with a measurable objective, evidence loop, budget, and stopping rule. The plugin defaults to autonomous local execution without per-command approval; strict review-attested leases remain optional for high-assurance runs.
---

# Goal Guardrails

Keep the optimization target stable while making the next experiment small, measurable, and worth its cost. Project Markdown remains the human-readable contract and evidence. The default `fast` profile is designed for YOLO/full-access and unattended overnight Goals: routine local work must not wait for a lease, reviewer, or user approval. `strict` is an explicit opt-in for high-assurance workflows.

## Choose the mode

- **`init` — initialize or adopt:** scaffold the protocol, fill supported facts, then activate enforcement only with explicit user approval.
- **`run` — run or resume:** restore the frontier, execute the highest-value bounded experiment autonomously, evaluate it, and checkpoint the semantic result.
- **`audit` — audit:** inspect alignment and report evidence without changing code or launching work unless the user also requests changes.

Read [references/operating-protocol.md](references/operating-protocol.md) only when strict leases or detailed state-machine recovery are actually needed. Fast local execution follows the shorter loop below.

## Initialize safely

Run the initializer from the repository root:

```bash
python3 <skill-dir>/scripts/init_project.py .
```

Use `--dry-run` first for multiple repositories. It creates only missing templates and additively manages one marked block in `AGENTS.md`; unrelated guidance and existing optimization files are preserved. Re-running it upgrades the plugin-owned block from older strict instructions to the current fast/unattended policy. `GATE.json` starts disabled, so scaffolding cannot unexpectedly block work.

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

Before selecting work, read `GOAL.md`, `STATE.md`, recent relevant experiment rows, and the backlog only to avoid repetition. Keep at most three candidates and select exactly one experiment. State its hypothesis, causal path, smallest valid test, success/failure threshold, and stop condition briefly; then execute it directly.

In `fast` profile:

- local editing, tests, builds, evaluation, diagnostics, recovery, MCP work, and evidence updates need no proposal, lease, reviewer, or user approval;
- an absent/expired lease, one rejected tool call, a failed test, or a malformed intermediate result is recoverable and must not stop the Goal;
- skip a protected high-impact action, record it in `BACKLOG.md`, and continue the next safe, high-contribution action instead of asking the user;
- ask the user only when progress truly requires changing the objective/metric, budget or material scope, an irreversible external action, or a fired stop line;
- update `STATE.md` concisely and append the experiment fact after a semantic result, not after every command.

Use a schema-v3 proposal and controller lease only when the run needs deterministic one-shot runtime binding, immutable external-monitor evidence, or the user explicitly selected `strict`. Fast admission performs deterministic validation and does not require an external reviewer. Existing active schema-v2 leases remain compatible. Strict profile retains the full review/lease/gates/checkpoint protocol in [references/operating-protocol.md](references/operating-protocol.md).

For strict admission, run `goal_guard.py subject optimization/PROPOSAL.json --project .` after the proposal is complete and require the fresh reviewer to return that exact `subject_sha256`. If an admitted lease contract is wrong but no lease-authorized effect has occurred, use `goal_guard.py release --expected-proposal-sha256 <digest-from-status> --reason <reason> --project .`; never deactivate the gate merely to discard authority. The controller refuses release after any mutation, gate, transport, binding, monitor, wait/wake, or finalization effect, preserves the chain, and requires a fresh strict review.

## Wait for asynchronous work

When a frozen external workload is running normally and no semantic event is available, enter `WAITING_EXTERNAL_EVENT` instead of returning `blocked`:

```bash
python3 <plugin-root>/hooks/goal_guard.py wait --event-key <stable-job-id> --event-path <preregistered-terminal-artifact> --project .
```

Use `wait` only when a trusted event bridge is configured. It preserves the gate and active lease and blocks mutation until wake. Without an event bridge, keep the process attached or use bounded controller-managed monitoring instead of entering a state that cannot wake itself. A trusted event bridge writes a changed terminal artifact and invokes:

```bash
python3 <plugin-root>/hooks/goal_guard.py wake --event-key <same-id> --event-path <same-artifact> --project .
```

Wake events are deduplicated by key and artifact SHA-256. A successful wake freezes that terminal artifact into the lease; postflight cannot overwrite it and checkpoint must present the same SHA. On SessionStart, automatically reconcile a durable external semantic event even if its notification is pending, dead-lettered, or was lost. Notification failure is transport recovery, never a business pause. Resume the same lease for postflight and checkpoint; do not create workload, monitoring, and postflight leases merely because the job waited. Generic project-artifact wait remains behavioral; only external-monitor contract v2 with the patched Codex 0.151 continuation API may claim zero idle Goal turns.

When the terminal belongs in a deterministic monitor's private state directory, read the external-monitor section of [references/operating-protocol.md](references/operating-protocol.md). Do not add a project relay. Run the frozen `sbatch --parsable` submission only through `submit-bind`; direct execution is denied before it runs and therefore does not consume the policy. Fast admission is deterministic; strict additionally requires review. If Slurm exists only on another host, read [references/remote-slurm.md](references/remote-slurm.md), use `ssh-helper-v1`, and run `doctor` first. Never use raw SSH submission or a remote controller. Once the controller starts the one-shot submission, success, failure, malformed output, timeout, or uncertain outcome consumes it. For `UNCERTAIN`, run `reconcile-bind`; never repeat `submit-bind`. Start the monitor with a frozen event-binding argv. For new unattended Goals use external-monitor `contract_version: 2`: invoke `wait-monitor` synchronously inside the target Goal's current turn, never from an unrelated background shell. It must enter ARMING, persist/read back the exact-thread continuation marker before that turn can end, and only then enter WAITING. Retry a failed arm with the same in-turn `wait-monitor`; never resubmit. Use `wake-monitor --event-id <id-from-wake>` on terminal delivery. The bridge must remain project-read-only. Only the controller may verify the immutable semantic event and canonical terminal, then materialize the protected project receipt. Include that receipt path and SHA under `external_monitor_results` at checkpoint, but never treat scheduler success as the business verdict. Do not require outbox delivery state to be completed inside the wake turn.

## Stop unproductive chains

Track the **core progress unit**, stable chain ID and causal bottleneck, and consecutive valid experiments with no core progress. A normally completed frozen evaluator reporting failure or zero yield is a valid no-progress result. Renaming a component or moving among internal contracts cannot reset the chain.

At the configured no-progress limit, switch, roll back, pause, or use the single predeclared final discriminator. That discriminator closes the diagnostic chain regardless of outcome. A positive result may enter only a separately named verification child restricted to replication, applicable validation, promotion, or rollback.

Waiting does not authorize cleanup. Repeated status recovery, identity/SHA checks, schema proofs, monitoring, and reviewer passes are non-core unless they restore evaluation integrity. Use `WAITING_EXTERNAL_EVENT` for a healthy asynchronous job. Do not classify normal waiting as blocked, complete, or active execution.

Treat experiment failure, chain closure, and Goal blocking as different states. A complete determinate threshold failure is a valid negative and must automatically switch or roll back. Evaluator missing/corrupt/indeterminate is invalid but consumes bounded recovery rather than blocking the Goal. A negative final discriminator closes only its chain and follows the frozen `other` path. Never mark the Goal blocked unless the controller's `blocking_proof.block_allowed` is true. Reversible runner, dependency, path, monitor, terminal, notification, or checkpoint failures are autonomous recovery in fast profile.

## Keep long runs recoverable

In fast profile, do not create leases for routine work. Keep long local processes attached, or use a deterministic monitor with a real wake path. A Hook denial rejects one high-impact call, not the Goal: continue another safe action without asking for permission. Do not mark the Goal complete or blocked merely because a command was denied, a lease expired, a test failed, or a result needed correction.

## Preserve the boundary

- Findings outside the metric, guardrails, evaluation integrity, or an admitted blocker go to `BACKLOG.md`.
- Infrastructure and refactoring are allowed only when they block the admitted experiment now, no smaller workaround exists, and work stops when the blocker is removed.
- Never change the metric, target, evaluator, frozen workload, holdout rule, guardrails, or budget merely to show progress.
- One experiment or chain exhausting its budget is not a Goal pause; switch/rollback/recover first. Only a machine-proven global exhaustion is `PAUSE_REQUIRED`, never `COMPLETE`.
- Keep `STATE.md` as a replaceable frontier snapshot; raw detail belongs in artifacts.
- Do not disable the gate, revise its limits, or deactivate hooks without explicit user approval.
- For an explicit user-approved GOAL change, stage a separate UTF-8 file and use `goal_guard.py update-goal --approved-by user --expected-sha256 <current> --from-file <staged> --reason <reason> --project .`. It requires no active lease or wait and keeps the gate enabled through a recoverable compare-and-swap transaction.
- Do not create or activate a Codex Goal unless the user explicitly asks.

## Enforcement boundary

Hooks reduce accidental drift; they are not a security sandbox. Fast profile intentionally delegates ordinary authorization to Codex YOLO/full-access mode and guards only a small high-impact boundary through direct patches, recognizable Bash path references, and conventional visible MCP path/command fields; opaque tool or script side effects cannot be inferred. Strict structured Bash contracts freeze observable invocation fields but cannot infer every process side effect. `submit-bind` inherits the controller environment while avoiding a shell; keep secrets out of argv and output. Plugin wait cannot reconfigure Codex Goal scheduling.
