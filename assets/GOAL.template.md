# Optimization Contract

## Objective

- Target, system, or process: `TODO`
- Primary metric: `TODO`
- Direction: `TODO: maximize | minimize`
- Baseline: `TODO`
- Target or stopping rule: `TODO`
- Minimum meaningful delta: `TODO`
- Baseline commit/artifact: `TODO`
- Core progress unit: `TODO: an end-to-end usable output or other goal-level unit`
- No-progress definition: `TODO: what counts as zero core progress despite a valid experiment`

The primary metric, target, and evaluation definition may change only through an explicit user-approved contract revision.

## Evaluation contract

- Evaluation command: `TODO`
- Input/data/workload snapshot: `TODO`
- Evaluator/grader version: `TODO`
- Repetitions/trials/seeds: `TODO`
- Machine-readable result artifact: `TODO`
- Independent validation / reserved holdout rule: `TODO: define or mark not applicable`

Where applicable, do not tune repeatedly on reserved validation, change the evaluator to create an improvement, discard unfavorable repetitions selectively, or compare results produced under incompatible conditions.

## Guardrails

| Metric | Baseline | Required threshold | Check command |
|---|---:|---:|---|
| `TODO` | `TODO` | `TODO` | `TODO` |

A candidate cannot become the incumbent while a hard guardrail fails.

## Scope

Allowed active scope:

- `TODO`

Excluded unless the user revises this contract:

- general repository cleanup or redesign;
- non-blocking warnings, formatting, documentation, and tooling;
- evaluator, frozen input/workload, reserved-validation, metric, or success-criterion changes;
- new platforms, dashboards, schedulers, or abstraction layers.

## Budgets

- Goal token budget: `TODO`
- Wall-clock budget: `TODO`
- Resource/cost/compute budget: `TODO`
- Maximum valid experiments or iterations: `TODO`
- Maximum experiments or budget per mechanism/diagnostic chain: `TODO`
- Maximum consecutive valid experiments with no core progress: `3`
- Non-core work limit: `10%` of the applicable active budget
- Final independent-validation reserve: `TODO: for example 15% of the applicable budget, or not applicable`
- `STATE.md` maximum nonblank lines: `25`

Budget exhaustion is not completion.

## Admission rule

A medium- or high-cost task must state:

1. a falsifiable hypothesis;
2. a causal path to the primary metric or hard guardrail;
3. the smallest valid test;
4. predeclared decision thresholds;
5. supporting evidence;
6. implementation and experiment cost;
7. information learned on failure;
8. rollback identity.

If the first four items are not concrete, defer the task to `BACKLOG.md`. Keep at most three active candidates. Prefer measured evidence, cheap falsification, information gain, lower cost, reversibility, and one primary change; do not rank by self-reported confidence scores.

Infrastructure, refactoring, documentation, tooling, and cleanup are admissible only when they block the highest-value experiment now, no smaller workaround exists, and the change stops when the blocker is removed.

Repeated monitoring, recovery, identity/SHA checks, schema/contract proofs, and reviewer passes are non-core unless they restore evaluation integrity required by the admitted experiment. Internal validity does not reset the no-progress counter unless it produces the declared core progress unit.

Each mechanism or diagnostic chain must have a stable ID and causal bottleneck. Component renames or movement among internal representations, adapters, contracts, or transport layers do not create a new chain when the causal bottleneck is unchanged. A new diagnostic or optimization chain must cite the closed predecessor or parent and state the materially different causal path. A verification/promotion child may inherit a successful path but may only replicate, run applicable validation, promote, or roll back; it cannot patch or reopen the diagnostic chain. A frozen evaluator that completes normally with failure or zero yield is a valid no-progress result; only broken evaluation integrity makes the run invalid.

## Checkpoints

Audit after every valid experiment and after resume, compaction, guardrail regression, scope expansion, or material cost overrun. If no experiment event occurs, audit every `TODO: budget fraction or time interval`. Keep protocol maintenance below 5% of the adjacent experiment cycle.

Every experiment ends with exactly one decision:

`CONTINUE | REPLICATE | SWITCH | ROLLBACK | PAUSE_REQUIRED | COMPLETE`

When a mechanism or consecutive-no-progress limit fires, do not add another patch or audit in that chain. One explicitly predeclared final discriminator may exceed the limit only if it changes one variable, uses the frozen end-to-end evaluator, and names mutually exclusive next paths beforehand. It closes the diagnostic chain regardless of outcome. A positive result may enter only the separately named verification/promotion path for replication and applicable independent validation; every other result follows its named switch, rollback, or pause path.

## Stop conditions

Return `PAUSE_REQUIRED` instead of starting new work when evaluation is untrusted, scope or budget must change, no candidate passes admission, an unresolved metric tradeoff needs the user, or remaining work is only low contribution.

Return `COMPLETE` only when the target is reached, the gain exceeds the meaningful-delta rule, all hard guardrails pass, and every replication, independent-validation, and reproducibility condition declared as applicable in this contract passes. Reproducibility evidence may include frozen code, configuration, inputs/data/workload, environment, and repetitions as appropriate to the optimization.
