# Goal Guardrails

A lightweight Codex skill that keeps long-running, metric-driven optimization focused on measurable gains instead of low-contribution cleanup and infrastructure work.

It applies beyond model training: prompt and model quality, performance, latency, cost, reliability, search and recommendation, conversion, data pipelines, and other iterative optimization with a measurable objective, evidence loop, budget, and stopping rule.

It installs a small project-local protocol:

```text
short /goal
  -> optimization/GOAL.md
  -> optimization/STATE.md
  -> admission of one bounded experiment
  -> optimization/EXPERIMENTS.md
  -> CONTINUE / REPLICATE / SWITCH / ROLLBACK / PAUSE_REQUIRED / COMPLETE
```

The first version intentionally has no database, dashboard, hook, supervisor agent, or task scheduler. It can work alongside existing dispatch, Slurm, CI, benchmark runners, and deterministic monitoring systems without changing their authority boundaries.

## Install

Ask Codex:

```text
$skill-installer install goal-guardrails from https://github.com/ChenLiangyu-sc/goal-guardrails
```

Or install manually:

```bash
git clone https://github.com/ChenLiangyu-sc/goal-guardrails \
  ~/.codex/skills/goal-guardrails
```

Restart Codex or use `/skills` if the skill does not appear immediately.

## Initialize one project

From an optimization repository, the short form is:

```text
$goal-guardrails init
```

Add context only when needed, for example: `$goal-guardrails init; optimize p95 latency without reducing throughput.`

The deterministic initializer can also be run directly:

```bash
python3 ~/.codex/skills/goal-guardrails/scripts/init_project.py .
```

It creates missing files under `optimization/` and additively inserts one marked policy block into `AGENTS.md`. Existing optimization files are never overwritten.

## Initialize several projects

Preview first:

```bash
python3 ~/.codex/skills/goal-guardrails/scripts/init_project.py \
  --dry-run /path/project-a /path/project-b /path/project-c
```

Then repeat without `--dry-run`. The script scaffolds templates only; use Codex separately in each repository to fill project-specific metrics, evaluation commands, targets, and budgets.

The initializer preflights every supplied path before writing and rolls back files created in a project if that project's initialization fails. A batch is not transactional across repositories: if a later runtime write fails, earlier completed repositories remain initialized and the command reports a partial batch. Symbolic-link project roots, `optimization/` directories, template targets, and `AGENTS.md` files are rejected rather than followed or replaced. When appending to an existing `AGENTS.md`, the initializer preserves its bytes, newline style, mode, owner, group, timestamps, and supported metadata; it fails closed if the operating system does not allow that preservation.

## Short commands

```text
$goal-guardrails init
$goal-guardrails run
$goal-guardrails audit
```

`init` scaffolds/adopts the protocol, `run` starts or resumes one bounded loop, and `audit` checks alignment without modifying files or launching work.

## Design principles

- One stable metric and evaluation contract.
- At most three active candidate hypotheses.
- One primary causal change per experiment.
- Low-contribution findings go to a backlog rather than interrupting the run.
- Infrastructure work must unblock the highest-value experiment now.
- Every valid experiment ends in one explicit decision.
- Budget exhaustion and process completion are not successful Goal completion.
- Escalate to wrappers, hooks, reviewers, or controllers only after measured soft-governance failures.

## Validation

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 /path/to/skill-creator/scripts/quick_validate.py .
```

## License

MIT
