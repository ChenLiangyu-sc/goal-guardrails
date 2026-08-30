# Goal Guardrails

Goal Guardrails keeps long-running, metric-driven optimization from drifting into low-contribution cleanup, repeated diagnostics, and ever-heavier execution paths.

It applies beyond model training: prompt quality, performance, latency, cost, reliability, search, recommendation, conversion, data pipelines, and other optimization with a measurable objective and evaluator.

Version 0.5.0 combines:

```text
GOAL.md + STATE.md
        -> fresh proposal review + attestation
        -> one frozen evidence/mutation/gate lease
        -> Codex PreToolUse/PostToolUse hooks
        -> static argv or local/SSH-helper one-shot runtime binding
        -> active / waiting-external-event / active
        -> verified external evidence -> controller receipt
        -> RESULT.json checkpoint
        -> continue / switch / rollback / pause / complete
```

The Markdown files remain readable project memory. A small JSON state machine freezes existing evidence, planned mutations, pre-run gates, ordered Bash argv/cwd/output contracts, one-shot runtime bindings, external-monitor identities, artifact paths, lease expiry, mutation count, stable causal-chain identity, no-progress stop lines, non-core allowance, and repeated-poll limits. There is no database, dashboard, resident service, or per-command LLM review.

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

Version 0.5.0 keeps proposal/result schema v2 and adds an optional reviewed `ssh-helper-v1` transport to one-shot runtime binding policies. Existing local and `fixed_args` proposals remain compatible. New projects default to a 24-hour, 24-mutation lease; reviewed proposals may use up to seven days without widening their path or command scope. Re-run `init` only to add missing files; do not replace `CONTROL.json`. Existing projects may explicitly raise `default_lease_minutes` and `default_max_mutations` in `GATE.json` while the gate is disabled, or declare the values per proposal.

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
2. Writes one bounded schema-v2 `PROPOSAL.json`, including `existing_evidence`, future `lease_mutations`, `pre_run_gates`, checkpoint artifacts, and structured Bash policies.
3. Obtains one fresh read-only subagent or user review of that future mutation contract. Admission does not require the mutations to exist yet.
4. Admits the proposal to create a temporary lease.
5. Allows only bounded mutation while the lease is live.
6. Records required gate evidence before workload execution.
7. Writes artifact paths and SHA-256 values to `RESULT.json`, then checkpoints the lease.

If a required pre-run gate genuinely fails, `gates` freezes the `FAIL` and its evidence. Workload and ordinary mutation stay denied. Run `abort`; the controller materializes and verifies the strict invalid checkpoint, clears the lease, and leaves the causal chain open for a newly reviewed attempt. Manual `RESULT.json` staging remains available when inspection or correction is necessary.

Manual controller commands are:

```bash
python3 <plugin-root>/hooks/goal_guard.py status --project .
python3 <plugin-root>/hooks/goal_guard.py admit optimization/PROPOSAL.json --project .
python3 <plugin-root>/hooks/goal_guard.py gates optimization/PRE_RUN_RESULTS.json --project .
python3 <plugin-root>/hooks/goal_guard.py doctor --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py submit-bind --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py reconcile-bind --policy submit-slurm --project .
python3 <plugin-root>/hooks/goal_guard.py abort --project .
python3 <plugin-root>/hooks/goal_guard.py checkpoint optimization/RESULT.json --project .
```

For an asynchronous workload, keep the same lease while waiting for one preregistered terminal artifact:

```bash
python3 <plugin-root>/hooks/goal_guard.py wait --event-key job-122020 --event-path artifacts/job-terminal.json --project .
python3 <plugin-root>/hooks/goal_guard.py wake --event-key job-122020 --event-path artifacts/job-terminal.json --project .
```

`wait` freezes the lease's remaining lifetime and enters `WAITING_EXTERNAL_EVENT`. Mutation and polling stop; ordinary non-polling inspection remains available. `wake` requires a changed regular event artifact, freezes its SHA-256 into the lease, and deduplicates it before restoring the same lease for postflight work. The terminal artifact cannot change again and checkpoint must reference that exact SHA.

### External monitor integration

Use this path when a deterministic monitor correctly keeps its terminal evidence outside the project. Admission freezes a `runtime_binding`, a one-shot capture policy, an ordered-argv monitor-start policy, and the external monitor's provider, state root, host, scheduler identity, and binding.

Run the frozen submission through the controller rather than executing it directly:

```bash
python3 <plugin-root>/hooks/goal_guard.py submit-bind --policy submit-slurm --project .
```

With local Slurm, the controller runs the exact reviewed executable and argv without a shell. When `sbatch` exists only on a remote host, add the reviewed `ssh-helper-v1` transport described in [remote Slurm submission](skills/goal-guardrails/references/remote-slurm.md). The local controller remains the sole owner of the lease, lock, and `CONTROL.json`; SSH carries one JSON request to a fixed, digest-pinned helper. Run `doctor` once, then `submit-bind`. Never run raw `ssh ... sbatch`, move the controller to the compute host, or share controller state across hosts.

The controller accepts one parsable Slurm Job ID, freezes it, and consumes the submission policy before the remote call. If SSH disconnects or the result is ambiguous, the binding becomes `UNCERTAIN`: run `reconcile-bind` with the same policy. Reconciliation reads the immutable nonce receipt and costs no mutation; it never submits again. Bound argv tokens can then be used only through reviewed policies. After starting `codex-hpc-monitor`, enter and leave waiting with:

```bash
python3 <plugin-root>/hooks/goal_guard.py wait-monitor --monitor scheduler --project .
python3 <plugin-root>/hooks/goal_guard.py wake-monitor --monitor scheduler --project .
```

`wait-monitor` freezes the monitor run and manifest SHA. The event bridge continues to write only its private cache receipt and wake the thread; it never edits the project. `wake-monitor` resolves the canonical receipt path, verifies owner/mode/symlink safety, bridge manifest, Job ID, run ID, terminal SHA, `terminal_verified=true`, monitor manifest, and scheduler owner/name/partition. Only then does the controller create `optimization/.goal-guardrails/receipts/<lease>/<monitor>.json`. That protected receipt remains scheduler-only evidence with `business_verdict=pending`; checkpoint still requires the project's business evaluator.

## What the hook blocks

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

Read-only inspection, including `cat`, stays available without a mutation lease. `PROPOSAL.json` can be prepared and reviewed before a lease. `GOAL.md`, `GATE.json`, `CONTROL.json`, the admitted proposal, and controller receipt tree are protected during experiments. A denied tool call does not by itself complete or block the Goal: `status.next_action` identifies the legal continuation. After expiry, budget exhaustion, or a malformed checkpoint, `RESULT.json` may still be corrected and checkpointed without another review.

## Boundaries

This is a strong behavioral guardrail, not a security sandbox. The workflow requires a genuinely fresh subagent or user review, but the local controller can validate only the recorded attestation's shape; it cannot authenticate who produced it. `--approved-by user` likewise records an explicit-approval attestation and must be used only after the user actually approves. Cryptographically or administratively unforgeable approval requires a managed hook or external trusted service.

Shell effects cannot be inferred perfectly, hosted tools may not traverse local lifecycle hooks, read-only MCP classification is conservative name-based policy, and users can disable non-managed hooks. MCP mutation currently fails closed; perform scoped project writes through `apply_patch` or an admitted Bash command. A hook denial must be treated as a decision boundary rather than an invitation to find another execution path.

`WAITING_EXTERNAL_EVENT` is a plugin state, not a platform scheduler API. It makes repeated Goal activations terminate cheaply through injected context and denied polling, but it cannot pause or reconfigure Codex Goal scheduling itself. A project-artifact bridge updates the preregistered file and invokes `wake`; an external-monitor bridge publishes its private immutable receipt, after which the controller invokes `wake-monitor` and materializes the project receipt.

Remote Slurm submission deliberately does not accept runtime argv after review. A transport policy freezes the local SSH executable and digest, host, user, port, dedicated known-hosts file and digest, dedicated identity path and digest, remote helper path and digest, remote `sbatch` path, work directory, receipt root, and submitted file digests. The controller ignores ambient SSH configuration, proxy commands, jump hosts, and agents. `status` reports the controller budget plan: each one-shot submission costs one mutation; doctor, reconciliation, waiting, waking, and receipt materialization cost zero. OpenSSH still invokes the remote command through the remote user's shell, so the helper path is restricted to a safe absolute token; a forced-command SSH key is recommended for a stronger administrative boundary.

Version 0.5.0 does not yet ingest rejected reviewer packets, so proposal-revision time before admission is not part of the chain stop-line budget. A later schema revision should introduce one controller-owned `record-review` contract that binds every verdict to the proposal SHA, counts distinct rejected attempts and observable admission wall time by causal bottleneck, and fires a pause/switch stop line. Project-specific completeness checks belong in a bounded declarative lint profile rather than hard-coded domain fields. Read-only shell recognition remains intentionally conservative; a future improvement may allow a small audited pipeline grammar, not arbitrary “safe shell AST” inference.

## Validation

```bash
python3 -m unittest discover -s skills/goal-guardrails/scripts -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 /path/to/skill-creator/scripts/quick_validate.py skills/goal-guardrails
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```

## License

MIT
