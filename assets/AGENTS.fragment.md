<!-- codex-model-optimization-guardrails:start -->
## Long-running model optimization

When `optimization/GOAL.md` governs an active task, its primary metric and hard guardrails take precedence over general repository improvement.

- Do not expand scope proactively. Record findings that do not directly serve the primary metric, a hard guardrail, evaluation integrity, or an admitted blocker in `optimization/BACKLOG.md`.
- Before medium- or high-cost training, evaluation, refactoring, infrastructure, documentation, or tooling work, state a falsifiable hypothesis, causal path to the metric, smallest valid test, predeclared decision threshold, cost, and information learned on failure. If these are not concrete, do not execute the task.
- Do not interrupt the highest-value experiment for cleaner code, broader abstractions, more documentation, fewer warnings, general tests, or work that may only be useful later.
- Infrastructure or refactoring is allowed only when it blocks the highest-value admitted experiment now, no smaller workaround exists, and the change is limited to removing that blocker.
- Prefer one primary causal change per experiment. Record the baseline, candidate result, guardrails, cost, decision, artifact, and rollback identity.
- Treat budget exhaustion, untrusted evaluation, required scope changes, and absence of admissible candidates as reasons to pause, not permission to fill time with low-contribution work.
<!-- codex-model-optimization-guardrails:end -->
