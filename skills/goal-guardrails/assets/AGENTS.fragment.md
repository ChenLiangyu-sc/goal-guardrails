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
- Read `optimization/GATE.json.profile`. Missing values and `fast` mean unattended YOLO execution: ordinary in-scope local edits, tests, builds, evaluation, diagnostics, recovery, MCP work, and evidence updates require no lease, reviewer, or user approval. `strict` alone uses review-attested per-experiment leases.
- A denied tool call skips one high-impact action, not the Goal. In fast profile, do not ask the user merely because of a denial, missing/expired lease, test failure, or recoverable controller error; record the issue and continue the next safe, high-contribution action. Ask only when progress truly requires changing the objective/metric, budget or material scope, an irreversible external action, or overriding a stop line.
- Use proposal/review/admission only for strict mode or deterministic one-shot runtime binding/external-monitor evidence. Fast admission is controller-validated and does not require a subagent/user review.
- In strict mode, bind review to the controller-reported proposal subject. If an admitted lease is still unconsumed, use controller `release` and obtain a fresh review instead of deactivating the gate. For an explicit user-approved GOAL change with no lease or wait, use controller `update-goal`; never create a gate-disabled edit window.
- Enter `WAITING_EXTERNAL_EVENT` only when a real event bridge can wake the Goal. Fast profile may use read-only bounded polling; without a bridge, keep the process attached or use controller-managed monitoring.
- For controller-integrated external monitors, capture dynamic IDs only through a one-shot `submit-bind` policy. The monitor remains project-read-only; `wake-monitor` validates its immutable evidence chain and lets the controller materialize the protected project receipt.
- When Slurm is remote, keep the controller and `CONTROL.json` local. Use only a frozen, doctored `ssh-helper-v1` policy; strict mode additionally requires review. Never submit through raw SSH. An uncertain call consumes the attempt and may only use `reconcile-bind` with its frozen nonce, never another submission.
- Treat budget exhaustion, untrusted evaluation, required scope changes, and absence of useful safe work as reasons to pause, not permission to fill time with low-contribution work.
<!-- goal-guardrails:end -->
