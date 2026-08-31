# Operating protocol

Use this detailed protocol for `strict` profile or when deterministic runtime binding/external-monitor evidence requires a lease. In default `fast` profile, routine local work skips proposal, external review, admission, exact command policies, and mutation accounting; restore the frontier, run one bounded experiment directly, evaluate it, and update semantic evidence. A denial skips one high-impact action and must not stop the Goal or trigger a user authorization request when other safe work remains.

## Restore the frontier

Confirm these facts before choosing work:

- frozen primary metric, baseline, target or stopping rule, and minimum meaningful delta;
- evaluation command, input/data/workload and evaluator versions, repetition policy, and holdout rule where applicable;
- current best verified candidate, not merely the latest workspace state;
- guardrail status;
- remaining token, wall-clock, compute, and experiment budgets where applicable;
- current run or next decision event;
- recent falsified or inconclusive hypotheses.
- the core progress unit, current end-to-end yield, stable chain ID and causal bottleneck, parent or closed-chain reference, and consecutive no-progress count;
- the mechanism stop line and remaining allowance for implementation, experiments, and non-core overhead.

If a critical fact is missing or evaluation integrity is uncertain, return `PAUSE_REQUIRED`. Do not fill the time with cleanup.

## Admit the next experiment

Generate no more than three candidates. A medium- or high-cost task is admissible only when all answers are concrete:

1. What falsifiable hypothesis does it test?
2. Through what causal path could it affect the primary metric or a hard guardrail?
3. What is the smallest valid test?
4. What result causes keep, replicate, switch, or rollback?
5. What repository evidence, failure cluster, ablation, or prior experiment supports it?
6. What implementation and experiment cost will it consume?
7. What useful uncertainty disappears if it fails?
8. Is it reversible and attributable to one primary change?
9. Does it test end-to-end progress, or merely prove an internal representation, contract, or transport property again?

Reject the task to the backlog when any of the first four answers is missing.

Rank candidates lexicographically instead of inventing precise confidence scores:

1. evaluation integrity and hard guardrails;
2. direct evidence about the largest current failure mode;
3. ability to falsify cheaply;
4. information gained on failure;
5. lower compute and implementation cost;
6. reversibility and narrower scope;
7. mechanism diversity relative to recent failed attempts.

## Obtain and enforce one lease

Put the selected experiment in schema-v2 `optimization/PROPOSAL.json`. Declare and freeze `existing_evidence`, future `lease_mutations`, `checkpoint_artifacts`, `pre_run_gates`, and structured `bash_policies`. Prefer ordered `argv` literal tokens; use binding tokens only for controller-frozen runtime values. Every proposal requires one fresh read-only subagent or user review at the experiment boundary. The reviewer confirms evidence sufficiency, bounded lease mutations, sufficient pre-run gates, and that mutation is expected only after admission. The reviewer must not demand that planned mutations already exist. The controller records this as a behavioral attestation and validates its shape; it does not authenticate the reviewer identity.

After `ALLOW`, use the bundled controller to admit the proposal. A proposal is rejected deterministically when another lease is active, state exceeds its cap, the same causal bottleneck is renamed, a chain is closed, its stop line fired without an unused final discriminator, non-core allowance is exhausted, or the proposal attempts to authorize protected control files.

The lease limits mutation paths/scopes/operations, exact Bash executable and argv, cwd, declared output paths, GPU resources, expiry, mutation count, work class, and cost. It stores both the canonical proposal SHA and exact proposal-file SHA. MCP mutation fails closed until a parameter-level scope adapter exists. Hook denial means return to admission or checkpoint. Never evade it with another tool, alternate command spelling, direct control-file edits, or gate deactivation. Non-polling read-only inspection does not need a mutation lease but is not a license for adjacent cleanup.

## Execute minimally

- Start from the best verified candidate or an explicitly selected frontier, not automatically from `latest`.
- Make one primary causal change.
- Record the success and failure threshold before running.
- Record a stable chain ID, its causal bottleneck and parent or closed-chain reference, the core progress expected, and the chain stop line before running.
- Keep the change reversible and preserve the exact commit/config/artifact identity.
- Use a cheap pilot only for screening. Promote only under the contract's full-evaluation rule.
- Distinguish an invalid run from a falsified hypothesis. A frozen evaluator that completes normally and reports failure or zero yield is a valid no-progress result; mark a run invalid only when evaluation integrity failed.
- Do not count a valid schema, parser, envelope, transport, terminal, or protocol result as core progress unless the contract defines it as the optimized outcome or it measurably restores end-to-end yield.

Keep the chain identity stable across component renames and adjacent internal layers when they address the same causal bottleneck. A new diagnostic or optimization chain requires a materially different causal path plus a reference to the closed parent or predecessor and the evidence that triggered the switch. A verification/promotion child may inherit the successful path, but it is limited to replication, contract-required validation, promotion, or rollback; it cannot patch the mechanism or reopen its diagnostic chain.

Infrastructure work requires all of the following:

- it blocks the admitted experiment now;
- no smaller workaround exists;
- its scope ends when the blocker is removed;
- its cost remains within the non-core budget;
- work returns immediately to the experiment.

## Checkpoint on semantic events

Audit after every valid experiment, after resume or compaction, before expanding scope, after a guardrail regression, or when a task consumes materially more than estimated. Use a budget checkpoint when no experiment event occurs for the interval defined in the contract.

Keep the audit short:

1. What is the best verified metric and delta from baseline?
2. What did the last work prove or rule out?
3. Is the current path still the shortest defensible route to the target?
4. Did scope or non-core spend expand?
5. What is the single next decision and action?
6. How many consecutive valid experiments on this mechanism produced no core progress, and has its stop line fired?

Update `STATE.md` by replacement, not accumulation, and keep it within the contract's nonblank-line cap. Append one concise fact row to `EXPERIMENTS.md`. Store raw metrics and logs in project artifacts, not in the state file. Repeated polling, recovery, identity checks, and unchanged status must not create narrative state growth or reset a no-progress counter.

Before workload or postflight commands, record required gate results and their preregistered evidence through `PRE_RUN_RESULTS.json`. A required `FAIL` is frozen as a legitimate preflight outcome: workload and ordinary mutation stay denied and the evidence cannot be overwritten or replayed as `PASS`. Run `goal_guard.py abort --project .` to make the controller materialize and validate the exact `valid=false`, `evaluation_integrity=FAIL`, `core_progress=false`, `outcome=invalid`, and `decision=PAUSE_REQUIRED` result from frozen evidence. Manual `RESULT.json` staging remains correctable if checkpoint rejects its shape. A valid checkpoint keeps the causal chain open and requires a fresh review before another admission. On PASS, write the machine-readable outcome to schema-v2 `RESULT.json`, including every required artifact's preregistered path, current SHA-256, and the unchanged gate results. The controller verifies the evidence contract and updates the chain counter; it does not infer whether a model or other business metric is good. Do not edit `CONTROL.json` manually. If Markdown evidence and controller state disagree, pause and reconcile from the frozen evaluator artifact rather than selecting the more favorable record.

## Wait for an external event

For a healthy asynchronous workload, use `WAITING_EXTERNAL_EVENT` after launch. Register a stable event key and a preregistered terminal artifact. The controller suspends the active lease clock, denies mutation and status polling, permits ordinary non-polling inspection, and instructs repeated Goal activations to end immediately. A wake is accepted only when the same artifact has a new SHA-256; repeated identical events are deduplicated. The accepted terminal SHA is then immutable and must match checkpoint. Wake the same lease for postflight and checkpoint instead of creating monitoring-only leases.

This is a plugin execution state, not a Codex scheduler control API. It cannot disable generic Goal activation or configure an event bridge. If the surrounding runtime cannot honor the waiting instruction or deliver a terminal artifact, report that integration limitation rather than marking the optimization blocked or weakening the quality gate.

### External monitor evidence

If the controller host has no Slurm client, use the native SSH helper contract in [remote-slurm.md](remote-slurm.md). Do not put the controller or `CONTROL.json` on the compute host and do not express submission as raw `ssh ... sbatch`.

Use an external monitor contract instead of a project relay when the monitor owns immutable evidence outside the repository. Admission must freeze:

- one `slurm_job_id` runtime binding and its one-shot capture policy;
- a monitor-start policy whose ordered argv contains that binding;
- provider and contract version, state root, host, expected scheduler owner, job name, and partition;
- the private event-binding and bridge-config paths in the frozen monitor-start argv.

In strict profile, the fresh reviewer must additionally attest `external_monitor_contract_bounded=true`, covering the one-shot capture, binding consumers, event binding, state root, scheduler identity, and controller receipt boundary. Fast profile performs deterministic controller validation instead.

The relevant proposal fields have this shape; keep the executable paths and literal arguments specific to the installed monitor:

```json
{
  "runtime_bindings": [
    {"id": "slurm-job", "kind": "slurm_job_id", "source_policy_id": "submit-slurm", "required": true}
  ],
  "bash_policies": [
    {
      "id": "submit-slurm", "phase": "workload", "executable": "sbatch",
      "argv": [{"literal": "--parsable"}, {"literal": "train.sbatch"}],
      "cwd": ".", "output_paths": [], "resources": {"gpu": 0},
      "capture_binding": "slurm-job", "max_uses": 1, "timeout_seconds": 120
    },
    {
      "id": "start-monitor", "phase": "workload", "executable": "python3",
      "argv": [
        {"literal": "/absolute/supervise_slurm_job.py"}, {"literal": "start"}, {"binding": "slurm-job"},
        {"literal": "--host"}, {"literal": "hpc142"},
        {"literal": "--state-dir"}, {"literal": "/home/USER/.cache/codex-hpc-monitor"},
        {"literal": "--expected-owner"}, {"literal": "USER"},
        {"literal": "--expected-job-name"}, {"literal": "JOB"},
        {"literal": "--expected-partition"}, {"literal": "PARTITION"},
        {"literal": "--event-binding"}, {"literal": "/home/USER/.config/codex-monitor/event-binding.json"},
        {"literal": "--bridge-config"}, {"literal": "/home/USER/.config/codex-monitor/bridge.json"},
        {"literal": "--require-auto-resume"}
      ],
      "cwd": ".", "output_paths": [], "resources": {"gpu": 0}
    }
  ],
  "external_monitors": [
    {
      "id": "scheduler", "provider": "codex-hpc-monitor", "contract_version": 1,
      "binding_id": "slurm-job", "start_policy_id": "start-monitor",
      "state_root": "/home/USER/.cache/codex-hpc-monitor", "host": "hpc142",
      "expected_owner": "USER", "expected_job_name": "JOB", "expected_partition": "PARTITION",
      "required": true
    }
  ]
}
```

Invoke `submit-bind --policy <id>` once. For a local policy the controller executes the frozen argv without a shell. For `ssh-helper-v1`, run `doctor --policy <id>` first; the local controller sends a versioned JSON request to the pinned helper. It parses the single `sbatch --parsable` result, freezes the Job ID, and closes the capture policy. An exit failure, timeout, malformed output, or uncertain result consumes the attempt. A definitive failure requires a fresh proposal (and fresh review only in strict profile); an uncertain remote result uses `reconcile-bind --policy <id>` against the same nonce and must never be resubmitted. Only controller-frozen values may fill binding tokens.

After the deterministic monitor starts, invoke `wait-monitor --monitor <id>`. It resolves the provider's canonical run and freezes its manifest SHA before suspending the lease. The bridge publishes a versioned semantic event to its private outbox and delivers only a fixed notification. It must not write the project or decide the business outcome.

On notification, invoke `wake-monitor --monitor <id> --event-id <sha256:...from-wake...>`. The event ID argument is optional only for compatibility with an already-waiting v0.6.0 controller; when present it must match the frozen run's publication. The controller verifies private artifact ownership/path, semantic-event publication, immutable outbox event identity and binding, project workspace, frozen Job ID/run ID, terminal digest, monitor manifest, watcher verification, and scheduler identity. It then materializes one protected `goal-guardrails.external-monitor-receipt/v2` file below `optimization/.goal-guardrails/receipts/`. Repeated delivery of the same event ID is an idempotent `duplicate`. The controller does not require `delivery.json` to be delivered because that mutable transport record normally completes after the wake turn. Include the project receipt and SHA in `RESULT.json.external_monitor_results`. The receipt proves scheduler observation only; the project finalizer, validator, and evaluator still determine evaluation integrity and core progress.

Treat the mechanism as exhausted when its declared experiment, wall-clock, or consecutive-no-progress limit is reached. One final discriminator is admissible only when it changes exactly one variable, has a frozen end-to-end evaluator, and names mutually exclusive next paths in advance. A positive result may enter a separately named verification/promotion chain limited to replication and contract-required independent validation. A negative or zero-progress result must enter the named switch or rollback path. An inconclusive result or failed evaluation integrity must enter the named switch, rollback, or pause path. Every outcome closes the original diagnostic/patch chain and cannot authorize another experiment in it.

## Decide and stop

- `CONTINUE`: the current mechanism has valid positive evidence and the next test remains bounded.
- `REPLICATE`: a positive signal needs another repetition, trial, seed, or independent check.
- `SWITCH`: evidence rejects or exhausts the current mechanism, its no-progress stop line fires, or a clearly stronger candidate exists.
- `ROLLBACK`: no verified gain, a guardrail failed, evaluation was invalid, or complexity increased without benefit.
- `PAUSE_REQUIRED`: the contract is incomplete, evaluation is untrusted, scope or budget must change, no candidate passes admission, or human tradeoff is required.
- `COMPLETE`: the target and every guardrail, replication, independent-validation or holdout, and reproducibility condition declared applicable in the contract are satisfied.

Never equate budget exhaustion, process completion, or lack of ideas with successful completion.

## Short Goal templates

Default fast/unattended template; adapt only the bracketed values:

```text
/goal Follow optimization/GOAL.md to optimize [TARGET, SYSTEM, OR PROCESS].

Run unattended in fast profile. Restore the concise frontier, choose one bounded
high-contribution experiment, edit/test/evaluate directly, and record semantic
evidence. Routine in-scope work needs no lease, reviewer, or user authorization.
A denied call, failed test, expired optional lease, or recoverable controller error
must not stop the Goal: defer that action and continue the next safe path.

Ask the user only if progress truly requires changing the objective/metric, budget
or material scope, an irreversible external action, or overriding a fired stop line.
Complete only after the target and applicable validation conditions pass.
```

Strict opt-in template:

```text
/goal Follow optimization/GOAL.md to optimize [TARGET, SYSTEM, OR PROCESS].

Before each experiment, restore optimization/STATE.md, obtain fresh external review,
attest it, and admit one bounded lease for a reversible test with a causal path
to the primary metric. Record valid results in optimization/EXPERIMENTS.md and
defer non-core work to optimization/BACKLOG.md.

Checkpoint after each valid experiment and at the budget interval in the
contract. Treat hook denial or lease exhaustion as a controller transition. Stop with
PAUSE_REQUIRED when evaluation is untrusted, scope or budget must change, or no
candidate passes admission. Complete only after the target and all applicable
guardrail and validation conditions pass.
```
