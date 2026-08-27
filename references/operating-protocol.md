# Operating protocol

Use this protocol when running, resuming, or auditing a long-running metric-driven optimization.

## Restore the frontier

Confirm these facts before choosing work:

- frozen primary metric, baseline, target or stopping rule, and minimum meaningful delta;
- evaluation command, input/data/workload and evaluator versions, repetition policy, and holdout rule where applicable;
- current best verified candidate, not merely the latest workspace state;
- guardrail status;
- remaining token, wall-clock, compute, and experiment budgets where applicable;
- current run or next decision event;
- recent falsified or inconclusive hypotheses.

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

Reject the task to the backlog when any of the first four answers is missing.

Rank candidates lexicographically instead of inventing precise confidence scores:

1. evaluation integrity and hard guardrails;
2. direct evidence about the largest current failure mode;
3. ability to falsify cheaply;
4. information gained on failure;
5. lower compute and implementation cost;
6. reversibility and narrower scope;
7. mechanism diversity relative to recent failed attempts.

## Execute minimally

- Start from the best verified candidate or an explicitly selected frontier, not automatically from `latest`.
- Make one primary causal change.
- Record the success and failure threshold before running.
- Keep the change reversible and preserve the exact commit/config/artifact identity.
- Use a cheap pilot only for screening. Promote only under the contract's full-evaluation rule.
- Distinguish an invalid run from a falsified hypothesis.

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

Update `STATE.md` by replacement, not accumulation. Append one concise fact row to `EXPERIMENTS.md`. Store raw metrics and logs in project artifacts, not in the state file.

## Decide and stop

- `CONTINUE`: the current mechanism has valid positive evidence and the next test remains bounded.
- `REPLICATE`: a positive signal needs another repetition, trial, seed, or independent check.
- `SWITCH`: evidence rejects or exhausts the current mechanism, or a clearly stronger candidate exists.
- `ROLLBACK`: no verified gain, a guardrail failed, evaluation was invalid, or complexity increased without benefit.
- `PAUSE_REQUIRED`: the contract is incomplete, evaluation is untrusted, scope or budget must change, no candidate passes admission, or human tradeoff is required.
- `COMPLETE`: the target and every required guardrail, replication, holdout, and reproducibility condition are satisfied.

Never equate budget exhaustion, process completion, or lack of ideas with successful completion.

## Short Goal template

Adapt only the bracketed values:

```text
/goal Follow optimization/GOAL.md to optimize [TARGET, SYSTEM, OR PROCESS].

Before each experiment, restore optimization/STATE.md and admit only a minimal,
reversible, attributable test with a causal path to the primary metric. Record
valid results in optimization/EXPERIMENTS.md and defer non-core work to
optimization/BACKLOG.md.

Checkpoint after each valid experiment and at the budget interval in the
contract. Stop with PAUSE_REQUIRED when evaluation is untrusted, scope or budget
must change, or no candidate passes admission. Complete only after the target,
guardrails, replication, holdout, and reproducibility conditions all pass.
```
