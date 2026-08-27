# Model Optimization Contract

## Objective

- Model or system: `TODO`
- Primary metric: `TODO`
- Direction: `TODO: maximize | minimize`
- Baseline: `TODO`
- Target or stopping rule: `TODO`
- Minimum meaningful delta: `TODO`
- Baseline commit/artifact: `TODO`

The primary metric, target, and evaluation definition may change only through an explicit user-approved contract revision.

## Evaluation contract

- Evaluation command: `TODO`
- Dataset snapshot/version: `TODO`
- Grader/judge version: `TODO`
- Seeds/trials: `TODO`
- Machine-readable result artifact: `TODO`
- Final holdout rule: `TODO`

Do not tune repeatedly on the final holdout, change the evaluator to create an improvement, discard unfavorable seeds selectively, or compare results produced under incompatible conditions.

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
- evaluator, dataset, holdout, metric, or success-criterion changes;
- new platforms, dashboards, schedulers, or abstraction layers.

## Budgets

- Goal token budget: `TODO`
- Wall-clock budget: `TODO`
- Compute/GPU budget: `TODO`
- Maximum valid experiments: `TODO`
- Non-core work limit: `10%` of the applicable active budget
- Final replication/holdout reserve: `15%` of compute budget

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

## Checkpoints

Audit after every valid experiment and after resume, compaction, guardrail regression, scope expansion, or material cost overrun. If no experiment event occurs, audit every `TODO: budget fraction or time interval`. Keep protocol maintenance below 5% of the adjacent experiment cycle.

Every experiment ends with exactly one decision:

`CONTINUE | REPLICATE | SWITCH | ROLLBACK | PAUSE_REQUIRED | COMPLETE`

## Stop conditions

Return `PAUSE_REQUIRED` instead of starting new work when evaluation is untrusted, scope or budget must change, no candidate passes admission, an unresolved metric tradeoff needs the user, or remaining work is only low contribution.

Return `COMPLETE` only when the target is reached, the gain exceeds the meaningful-delta rule, all hard guardrails pass, required replications and final holdout pass, and the result is reproducible from frozen code, configuration, data, and seeds.
