# Codex Model Optimization Guardrails

A lightweight Codex skill that keeps long-running model training, prompt optimization, and evaluation-driven research focused on measurable gains instead of low-contribution cleanup and infrastructure work.

It installs a small project-local protocol:

```text
short /goal
  -> optimization/GOAL.md
  -> optimization/STATE.md
  -> admission of one bounded experiment
  -> optimization/EXPERIMENTS.md
  -> CONTINUE / REPLICATE / SWITCH / ROLLBACK / PAUSE_REQUIRED / COMPLETE
```

The first version intentionally has no database, dashboard, hook, supervisor agent, or training scheduler. It can work alongside existing dispatch, Slurm, and deterministic monitoring systems without changing their authority boundaries.

## Install

Ask Codex:

```text
$skill-installer install codex-model-optimization-guardrails from https://github.com/ChenLiangyu-sc/codex-model-optimization-guardrails
```

Or install manually:

```bash
git clone https://github.com/ChenLiangyu-sc/codex-model-optimization-guardrails \
  ~/.codex/skills/codex-model-optimization-guardrails
```

Restart Codex or use `/skills` if the skill does not appear immediately.

## Initialize one project

From a model-training repository, ask Codex:

```text
Use $codex-model-optimization-guardrails to initialize the current project.
Inspect existing training and evaluation files, fill only verifiable contract facts,
and list the critical TODOs I must decide before an expensive run.
```

The deterministic initializer can also be run directly:

```bash
python3 ~/.codex/skills/codex-model-optimization-guardrails/scripts/init_project.py .
```

It creates missing files under `optimization/` and additively inserts one marked policy block into `AGENTS.md`. Existing optimization files are never overwritten.

## Initialize several projects

Preview first:

```bash
python3 ~/.codex/skills/codex-model-optimization-guardrails/scripts/init_project.py \
  --dry-run /path/project-a /path/project-b /path/project-c
```

Then repeat without `--dry-run`. The script scaffolds templates only; use Codex separately in each repository to fill project-specific metrics, evaluation commands, targets, and budgets.

The initializer preflights every supplied path before writing and rolls back files created in a project if that project's initialization fails. A batch is not transactional across repositories: if a later runtime write fails, earlier completed repositories remain initialized and the command reports a partial batch. Symbolic-link project roots, `optimization/` directories, template targets, and `AGENTS.md` files are rejected rather than followed or replaced. When appending to an existing `AGENTS.md`, the initializer preserves its bytes, newline style, mode, owner, group, timestamps, and supported metadata; it fails closed if the operating system does not allow that preservation.

## Run or audit

```text
Use $codex-model-optimization-guardrails to resume the optimization from the
project contract and current state. Admit only one bounded next experiment.
```

```text
Use $codex-model-optimization-guardrails to audit whether the current long-running
optimization has drifted. Do not modify files or launch work.
```

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
