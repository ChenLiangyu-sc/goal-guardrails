# Goal Guardrails

Goal Guardrails keeps long-running, metric-driven optimization from drifting into low-contribution cleanup, repeated diagnostics, and ever-heavier execution paths.

It applies beyond model training: prompt quality, performance, latency, cost, reliability, search, recommendation, conversion, data pipelines, and other optimization with a measurable objective and evaluator.

Version 0.6.4 keeps the v0.6.3 fast path and adds three bounded recovery improvements: strict reviews are cryptographically bound to the current GOAL/proposal subject, an unconsumed lease can be released without disabling the gate, and an explicitly user-approved GOAL update can run as a recoverable compare-and-swap transaction while the gate stays enabled. Query-only `date`, `wc`, `cat`, `rg`, and recognized read pipelines remain available during waiting; write-capable interpreters and ambiguous Git pager, textconv, external-diff, and signature-helper paths stay fail-closed.

The fast workflow introduced in 0.6.0 is:

```text
GOAL.md + STATE.md
        -> choose one bounded high-value experiment
        -> edit / test / evaluate directly in YOLO mode
        -> semantic evidence checkpoint
        -> continue / switch / rollback / pause / complete

optional strict or external one-shot path
        -> deterministic proposal validation (strict adds review)
        -> frozen lease + runtime binding + monitor receipt
```

The default `fast` profile is intended for Codex YOLO/full-access and unattended overnight Goals. It adds no per-command approval, lease, mutation counter, exact Bash policy, or reviewer requirement to routine local work. It protects the objective/controller files, frozen run inputs/evidence, duplicate one-shot submissions, and broadly destructive commands. `strict` remains an explicit opt-in. There is no database, dashboard, resident service, or per-command LLM review.

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

The initializer creates missing files under `optimization/` and additively manages one marked block in `AGENTS.md`; unrelated guidance is preserved. Re-running it upgrades an old plugin-owned strict block to the fast/unattended policy without replacing project files. `optimization/GATE.json` starts with `enabled: false`.

Version 0.6.0 adds `GATE.json.profile`. New projects use `fast`; existing projects without the field also resolve to `fast`, so upgrading immediately removes legacy routine lease denials. Select strict mode only when wanted:

```bash
python3 <plugin-root>/hooks/goal_guard.py mode strict --approved-by user --project .
```

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

The default fast workflow is:

1. Restores `GOAL.md`, `STATE.md`, and recent evidence.
2. Chooses one bounded experiment with a direct causal path and stop condition.
3. Runs local edits, tests, builds, evaluation, diagnostics, and recovery directly without asking for approval.
4. Records a semantic result and updates the concise frontier.
5. Continues, switches, rolls back, or pauses according to evidence and budget.

Routine fast work does not use `PROPOSAL.json`, `admit`, reviewer attestation, exact command whitelists, or mutation accounting. A denial skips only a high-impact action; it explicitly tells Codex to continue another safe action without asking the user. Proposal admission remains available for strict mode and deterministic external one-shot submission; fast admission validates it automatically without external review.

Strict/external controller commands remain available but are not the normal fast loop:

```bash
python3 <plugin-root>/hooks/goal_guard.py status --project .
python3 <plugin-root>/hooks/goal_guard.py subject optimization/PROPOSAL.json --project .
python3 <plugin-root>/hooks/goal_guard.py admit optimization/PROPOSAL.json --project .
python3 <plugin-root>/hooks/goal_guard.py gates optimization/PRE_RUN_RESULTS.json --project .
python3 <plugin-root>/hooks/goal_guard.py doctor --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py submit-bind --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py reconcile-bind --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py abort --project .
python3 <plugin-root>/hooks/goal_guard.py checkpoint optimization/RESULT.json --project .
```

In strict mode, freeze the completed proposal, run `subject`, and give that digest to the fresh reviewer. Admission rejects an attestation for any other GOAL/proposal/review-epoch contract. Every safe release advances a monotonic epoch, so every older subject remains invalid rather than becoming reusable after another release. If admission exposed a contract mistake before any lease-authorized action ran, use the proposal digest reported by `status` to remove authority safely:

```bash
python3 <plugin-root>/hooks/goal_guard.py release --expected-proposal-sha256 <digest> --reason "correct unconsumed contract" --project .
```

Release is refused after a mutation, gate result, doctor, binding, policy run, monitor receipt, wait, wake, or finalization effect. It preserves the causal chain and requires a fresh strict review before readmission.

When the user explicitly changes the objective, stage the replacement in a separate UTF-8 file and use the controller instead of briefly deactivating the gate:

```bash
python3 <plugin-root>/hooks/goal_guard.py update-goal --approved-by user --expected-sha256 <current-goal-digest> --from-file GOAL.next.md --reason "user-approved objective change" --project .
```

This command requires an active gate, no active lease, no external wait, and an exact old digest. A bounded controller journal recovers an interrupted write; `GATE.json` is never disabled.

For an asynchronous workload, keep the same lease while waiting for one preregistered terminal artifact:

```bash
python3 <plugin-root>/hooks/goal_guard.py wait --event-key job-122020 --event-path artifacts/job-terminal.json --project .
python3 <plugin-root>/hooks/goal_guard.py wake --event-key job-122020 --event-path artifacts/job-terminal.json --project .
```

Use `wait` only when a real bridge can invoke `wake`. Mutation stops until the registered event; fast profile permits read-only bounded polling, while strict also blocks repeated polling. Without a bridge, keep the process attached or use controller-managed monitoring so an unattended Goal does not enter a state that cannot wake itself.

### External monitor integration

Use this path when a deterministic monitor correctly keeps its terminal evidence outside the project. Admission freezes a `runtime_binding`, a one-shot capture policy, an ordered-argv monitor-start policy (including event-binding/bridge-config paths and `--require-auto-resume`), and the external monitor's provider, state root, host, scheduler identity, and binding.

Run the frozen submission through the controller rather than executing it directly:

```bash
python3 <plugin-root>/hooks/goal_guard.py submit-bind --policy submit-slurm --project .
```

With local Slurm, the controller runs the exact frozen executable and argv without a shell. When `sbatch` exists only on a remote host, add the frozen `ssh-helper-v1` transport described in [remote Slurm submission](skills/goal-guardrails/references/remote-slurm.md). Fast mode validates the contract deterministically; strict mode additionally requires review. The local controller remains the sole owner of the lease, lock, and `CONTROL.json`; SSH carries one JSON request to a fixed, digest-pinned helper. Run `doctor` once, then `submit-bind`. Never run raw `ssh ... sbatch`, move the controller to the compute host, or share controller state across hosts.

The controller accepts one parsable Slurm Job ID, freezes it, and consumes the submission policy before the remote call. If SSH disconnects or the result is ambiguous, the binding becomes `UNCERTAIN`: run `reconcile-bind` with the same policy. Reconciliation reads the immutable nonce receipt and costs no mutation; it never submits again. Bound argv tokens can then be used only through frozen policies. After starting `codex-hpc-monitor`, enter and leave waiting with:

```bash
python3 <plugin-root>/hooks/goal_guard.py wait-monitor --monitor scheduler --project .
python3 <plugin-root>/hooks/goal_guard.py wake-monitor --monitor scheduler --event-id sha256:<event-id> --project .
```

The monitor start argv must freeze `--event-binding <private-binding.json>` (and normally `--bridge-config <private-config.json> --require-auto-resume`). `wait-monitor` freezes the run and manifest SHA. The event bridge publishes `codex-monitor.event/v1` to its private outbox and wakes the thread; it never edits the project. `wake-monitor` accepts the event ID from the fixed wake message, or derives it from the frozen run for v0.6.0 `CONTROL.json` compatibility. It verifies the immutable semantic event, event binding/workspace, event identity digest, terminal digest, Job ID, run ID, watcher result, and scheduler owner/name/partition before creating a `goal-guardrails.external-monitor-receipt/v2` file under `optimization/.goal-guardrails/receipts/<lease>/<monitor>.json`. A repeated delivery of the same event ID returns `duplicate` without changing state.

`delivery.json` is deliberately not terminal evidence: while the wake turn is running it may still be pending or leased. The immutable outbox event is notification identity; the independently verified terminal remains scheduler authority. The protected project receipt therefore remains scheduler-only evidence with `business_verdict=pending`; checkpoint still requires the project's business evaluator.

## What the hook blocks

Fast profile blocks only:

- changes to `GOAL.md`, `GATE.json`, `CONTROL.json`, or controller-owned state;
- overwriting frozen preflight/terminal evidence or inputs of an active one-shot remote submission;
- direct execution of a frozen one-shot binding-capture command instead of `submit-bind`;
- broadly destructive commands such as hard reset, force clean/push, infrastructure destroy, or deleting a filesystem root;
- mutation while `WAITING_EXTERNAL_EVENT` (read-only inspection and polling remain available).

It does **not** block routine project edits, tests, builds, training/evaluation commands, diagnostics, recovery, MCP writes, result correction, or work because no lease exists.

Strict profile additionally blocks:

- mutation without a live review-attested lease;
- edits outside admitted mutation path, scope, and operation contracts;
- Bash whose executable, complete argv, cwd, output path, phase, or GPU declaration differs from the reviewed policy;
- direct execution, retry, or rebinding of a one-shot submission policy;
- raw SSH submission, changed SSH/known-hosts/helper binaries, remote-file drift, and automatic retry after an uncertain remote outcome;
- external monitor runs, manifests, receipts, terminal SHA values, or scheduler identities that drift from admission;
- all MCP writes and unknown MCP operations (until a parameter-level scope adapter exists);
- work after expiry or mutation-budget exhaustion;
- reuse of a closed chain or renaming the same causal bottleneck;
- repeated non-core leases beyond the configured allowance;
- proposal or existing-evidence changes after admission;
- workload execution before required pre-run gates are recorded;
- workload or ordinary mutation after a required pre-run gate records `FAIL`;
- checkpoint artifacts with unregistered paths, missing files, or mismatched SHA-256;
- mutation and repeated polling while `WAITING_EXTERNAL_EVENT`;
- continued work when `STATE.md` exceeds its cap;
- identical polling after repeated unchanged results.

In both profiles a denied tool call does not complete or block the Goal. In fast mode Codex must not turn it into a user authorization request: it records/defer the skipped action and continues the next safe path. User input is reserved for an unavoidable objective/metric change, budget or material-scope expansion, irreversible external action, or stop-line override.

## Boundaries

This is a behavioral direction guardrail, not a security sandbox. Fast profile intentionally relies on the user's Codex YOLO/full-access authorization for ordinary in-scope execution. Its high-impact file checks cover direct `apply_patch`, recognizable Bash path references, and conventional visible MCP path/command fields; arbitrary scripts or opaque hosted tools can have side effects the hook cannot infer. Strict review fields are behavioral attestations; the controller validates their shape and subject digest but cannot authenticate who produced them.

Shell effects cannot be inferred perfectly, hosted tools may not traverse local lifecycle hooks, and users can disable non-managed hooks. Fast mode deliberately avoids pretending it can authorize every local command: it focuses on direction, a small protected boundary, and unattended continuation.

`WAITING_EXTERNAL_EVENT` is a plugin state, not a platform scheduler API. It makes repeated Goal activations terminate cheaply through injected context and denied polling, but it cannot pause or reconfigure Codex Goal scheduling itself. A project-artifact bridge updates the preregistered file and invokes `wake`; an external-monitor bridge delivers the immutable semantic event ID, after which the controller invokes `wake-monitor`, independently verifies the canonical terminal, and materializes the project receipt.

Remote Slurm submission deliberately does not accept runtime argv after admission. A transport policy freezes the local SSH executable and digest, host, user, port, dedicated known-hosts file and digest, dedicated identity path and digest, remote helper path and digest, remote `sbatch` path, work directory, receipt root, and submitted file digests. The controller ignores ambient SSH configuration, proxy commands, jump hosts, and agents. `status` reports the controller budget plan: each one-shot submission costs one mutation; doctor, reconciliation, waiting, waking, and receipt materialization cost zero. OpenSSH still invokes the remote command through the remote user's shell, so the helper path is restricted to a safe absolute token; a forced-command SSH key is recommended for a stronger administrative boundary.

Strict mode retains the v0.5 lease state machine for high-assurance and external-monitor use. Fast mode is the recommended default when throughput and unattended execution matter more than command-level enforcement.

## Validation

```bash
python3 -m unittest discover -s skills/goal-guardrails/scripts -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 /path/to/skill-creator/scripts/quick_validate.py skills/goal-guardrails
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```

## License

MIT
