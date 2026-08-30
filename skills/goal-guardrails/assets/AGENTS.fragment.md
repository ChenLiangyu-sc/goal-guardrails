<!-- goal-guardrails:start -->
## Long-running metric-driven optimization

When `optimization/GOAL.md` governs an active task, its primary metric and hard guardrails take precedence over general repository improvement.

- Do not expand scope proactively. Record findings that do not directly serve the primary metric, a hard guardrail, evaluation integrity, or an admitted blocker in `optimization/BACKLOG.md`.
- Before medium- or high-cost experimentation, evaluation, refactoring, infrastructure, documentation, or tooling work, state a falsifiable hypothesis, causal path to the metric, smallest valid test, predeclared decision threshold, cost, and information learned on failure. If these are not concrete, do not execute the task.
- Do not interrupt the highest-value experiment for cleaner code, broader abstractions, more documentation, fewer warnings, general tests, or work that may only be useful later.
- Infrastructure or refactoring is allowed only when it blocks the highest-value admitted experiment now, no smaller workaround exists, and the change is limited to removing that blocker.
- Prefer one primary causal change per experiment. Record the baseline, candidate result, guardrails, cost, decision, artifact, and rollback identity.
- Track the contract's end-to-end core progress unit under a stable chain ID and causal bottleneck. Renaming a component does not reset the chain. When a chain reaches its experiment, budget, or consecutive-no-progress limit, switch, roll back, or pause; internal schema/protocol success and repeated recovery or review do not reset the limit.
- Keep `optimization/STATE.md` as a bounded current snapshot, not an accumulated work log.
- When `optimization/GATE.json` is enabled, mutating work requires one unexpired review-attested experiment lease. Obtain a real fresh subagent or user review; the controller validates the attestation but cannot authenticate its author. Do not bypass a denial by changing tools, commands, chain names, or control files; checkpoint or return to admission.
- Review the proposed future `lease_mutations`; do not require those mutations to exist before admission. Treat existing evidence, proposal semantics, pre-run gates, structured Bash invocation, and checkpoint artifact paths/SHA as frozen lease contracts.
- For a healthy asynchronous job with no new terminal evidence, enter `WAITING_EXTERNAL_EVENT`, stop polling and end the activation. Resume the same lease only through the deduplicated registered wake artifact; normal waiting is neither blocked nor complete.
- Treat budget exhaustion, untrusted evaluation, required scope changes, and absence of admissible candidates as reasons to pause, not permission to fill time with low-contribution work.
<!-- goal-guardrails:end -->
