# Evolution

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](pyproject.toml)

This repository is the code for **Managing Procedural Memory in LLM Agents:
Control, Adaptation, and Evaluation**. Evolution is a trace-driven skill
evolution framework and the evaluation harness used in the paper. It stores
reusable Agent Skills in `SKILL.md` format, runs task attempts, records
prompt/response/code/result traces, reflects on failures via a
COLLECT–DIAGNOSE–REVISE–PROMOTE cycle, rewrites candidate skills, and
versions the skill lineage.

## AFTER Benchmark

**AFTER** is the benchmark introduced in the paper for evaluating procedural
skill transfer in LLM agents. It contains 382 realistic workplace tasks
spanning six professional roles and 22 procedural skills, with controlled
train/test splits that measure both *specificity* (in-context gain) and
*generality* (cross-task, cross-role, and cross-model transfer).

| | |
|---|---|
| Roles | DE, DS, GenAI, Infra, PM, SWE |
| Skills | 22 (document processing, data ops, ML/AI, infrastructure, software engineering) |
| Tasks | 382 — 318 single-skill / 64 multi-skill |
| Difficulty | 165 easy / 126 medium / 73 hard / 18 extra-hard |
| Transfer splits | cross-task · cross-role · cross-model |

Dataset: [HuggingFace — coming soon](https://huggingface.co/datasets/TODO/AFTER)

Dataset release: [AFTER-anonymous/](AFTER-anonymous/)

The corpus is external to this repository. Mount it and set `AFTER_DATA_ROOT`
(see `.env.example`). The dataset layout is documented in `datasets/AFTER/README.md`
once mounted; a brief summary: each task lives under
`tasks/{role}/{task_id}/` with `instruction.md`, `environment/data/`, and
`output/` as the agent-visible surface; everything else is oracle territory.

## What Works From This Checkout

From the committed repository alone you can install the package, run the
offline tests, create or import tasks, run the core `evo` lifecycle against the
tracked sample tasks, and record local traces. You do not need the AFTER corpus,
baseline repositories, or table artifacts for normal framework usage.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e ".[dev,llm]"

pytest -m "not live and not paper and not external_repo" -q
evo --help
evo task list
```

The default test selection is offline and does not require API keys, the full
AFTER corpus, or sibling baseline repositories.

## Generic Lifecycle

```bash
evo task list
evo skill list

# Run a task and record traces. `mock` is offline and may fail the task; the
# useful smoke check is that traces are saved and inspectable.
evo run solve court-form-filling -m mock -w /tmp/evo-pdf-doc-smoke
evo trace list pdf --task court-form-filling
evo trace show pdf --last -s result

# Evolve from traces.
evo evolve reflect pdf -m <reflector-model> --task court-form-filling
evo evolve rewrite pdf -m <rewriter-model> --task court-form-filling
evo skill diff pdf
evo skill commit pdf -m "Improve general procedure"
evo skill history pdf
```

Every `evo solve` run records the full prompt, model response, extracted code,
execution result, and skill version metadata.

For real model runs, replace `-m mock` with a configured LiteLLM/OpenRouter,
OpenAI-compatible, or Codex backend. The old flat commands
(`evo solve`, `evo traces`, `evo commit`, and so on) remain as compatibility
wrappers, but the grouped lifecycle is the documented interface.

## Custom Tasks

Evolution tasks are plain directories. The minimal contract is:

- `task.toml` with metadata, required skills, timeout, and evaluator path;
- `instruction.md` with the user-facing task;
- optional `inputs/` copied into each workspace;
- evaluator file, usually `tests/test_outputs.py`, run with `TASK_WORKSPACE`
  pointing at the agent workspace.

Use `--dir <tasks-root>` on task/run commands for a custom corpus:

```bash
evo task list --dir ./my-tasks
evo run solve my-task -m openai/gpt-4o --dir ./my-tasks -w /tmp/evo-my-task
evo task eval my-task --dir ./my-tasks -w /tmp/evo-my-task
```

See [`docs/custom-tasks.md`](docs/custom-tasks.md) for a complete minimal
task layout and Python API example.

The same lifecycle is available as Python:

```python
from evolution.workflow import solve, list_traces, reflect, rewrite, commit_skill

solve("my-task", model="mock", project_dir=".", tasks_dir="./my-tasks")
list_traces("my-skill", project_dir=".")
reflect("my-skill", model="openrouter/anthropic/claude-sonnet-4", task="my-task", tasks_dir="./my-tasks")
rewrite("my-skill", model="codex/gpt-5.5", task="my-task", tasks_dir="./my-tasks", apply=True)
commit_skill("my-skill", project_dir=".")
```

## Paper Experiments

Paper experiment code is under `experiments/`:

- `experiments/_shared/` — shared evaluation infrastructure (schema, materializer, telemetry, adapter bridges)
- `experiments/baselines/` — multi-framework comparison loop: hermes · memento · memp · evoskill · evolution
- `experiments/iterative_evolution/` — iterative evolution and canonical skill analysis

The entry point for a single framework–skill cell is `experiments/baselines/afterbench_loop.py`:

```bash
export AFTER_DATA_ROOT=/path/to/AFTER
python experiments/baselines/afterbench_loop.py \
  --framework evolution --skill pdf \
  --subset experiments/baselines/subset.json \
  --rounds 5 --n-trials 5 --confirm-trials 3
```

Offline paper-alignment guards (no API key or corpus required):

```bash
python -m pytest tests/test_paper_alignment.py tests/test_v3_contract.py \
                 tests/test_reflect.py tests/test_solver.py -q
```

The paper-marked tests (`-m paper`) require the AFTER corpus mounted at
`AFTER_DATA_ROOT` and configured model endpoints.

## Integrity Model

- Reflection uses fresh controlled training traces from the run-local isolated
  skill root; import-time seeded `.traces/` are not trusted as experiment
  evidence.
- `EVO_SOURCE_FEEDBACK=1` allows sanitized source-task execution feedback in
  diagnosis. Verifier source, expected values, held-out/test feedback, and
  promotion-validation feedback are excluded from reflection.
- Rewrites are candidate skills. The paper pipeline validates parent and
  candidate on a disjoint promotion pool, promotes only when the candidate
  clears the configured margin, and otherwise keeps the parent active while
  recording the rejected candidate in lineage.

## Layout

```text
src/evolution/          Python package
  core/                 SKILL.md parser, validator, stores
  eval/                 workspace setup, solver, pytest runner
  lineage/              versions, lineage metadata, traces, diffs
  llm/                  LLM backend adapters
  workflow.py           public Python lifecycle API
  adapters/             optional baseline/research adapter support
  cli/                  evo Typer CLI

.agents/skills/         curated sample skills and version histories
tasks/                  small tracked task set for local development
splits/                 split-builder scripts (manifests are external)
experiments/            paper experiment source, artifacts ignored
docs/                   artifact and API documentation
tests/                  offline, paper-gated, and external-gated tests
```

## Reproducibility Notes

- `splits/` scripts are part of the artifact and should remain version-controlled.
- `tasks/` contains only a small tracked sample set. The full AFTER corpus is
  external data, not repository source.
- Results in the draft PDF should be regenerated from the publication pipeline
  before release; do not treat stale local artifacts as paper results.

## License

This artifact is intended for Apache License 2.0 release. The `LICENSE` file
is not present in the current committed source snapshot. Do not use this
checkout as a redistributable release until the release metadata files listed
in `docs/data-release.md` are committed.
