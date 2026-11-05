# Python API

This page covers the Python API for the manual task/skill workflow and its optimizer. For tree campaigns, start with [Running campaigns](running-campaigns.md); for an agent integration, use the [target server API](target-server.md). The two workflows have different inputs and run storage.

From the repository root, install this checkout before importing it. If you use a supplied wheel instead, follow the [installation guide](../QUICKSTART.md#1-install-the-beta) and request the `[llm]` extra:

```bash
python -m pip install -e ".[llm]"
```

`evolution.workflow` is the supported manual lifecycle API. The production optimizer is exported from `evolution.evolve`.

Provider credentials are read from environment variables. Prefer passing a constructed backend when an application needs custom authentication; do not put credentials in command arguments or examples.

## Discover Tasks And Skills

```python
from evolution.workflow import list_tasks, skill_store

for task in list_tasks(project_dir="."):
    print(task.name, task.skills_required)

store = skill_store(project_dir=".")
for entry in store.list():
    print(entry.name, entry.status)
```

Task roots resolve as explicit `tasks_dir`, then `EVO_TASKS_ROOT`, then `<project_dir>/tasks` (or `<cwd>/tasks` when `project_dir` is omitted).

## Solve And Evaluate

```python
from evolution.workflow import evaluate_task, solve

result = solve(
    "my-task",
    model="mock",
    project_dir=".",
    workspace="/tmp/evo-my-task",
    agent_permission="safe",
    pass_env=("SSL_CERT_FILE",),
)

print(result.success, result.pass_rate, result.trace_ids)

evaluation = evaluate_task(
    "my-task",
    "/tmp/evo-my-task",
    project_dir=".",
    pass_env=("SSL_CERT_FILE",),
)
print(evaluation.success)
```

`model="mock"` checks the offline task/workspace/trace path. A real run uses a configured provider model or `api_base`. `max_tokens=None` resolves `EVO_LLM_MAX_TOKENS`, then `16384`.

By default, `solve` binds every task requirement to its active immutable snapshot. A binding covers the full managed tree (`SKILL.md`, `scripts/`, `references/`, and `assets/`) and carries its version and SHA-256 into trace provenance. `skill_name=` replaces only the primary task requirement; supporting requirements remain present and pinned. Uncommitted working content is not injected.

Agent CLI backends default to `agent_permission="safe"`. Unsafe execution must be explicit. All safe Hermes calls use file tools only and fail closed unless Linux filesystem containment can start.

## Inspect Traces

```python
from evolution.workflow import list_traces, load_trace

for trace in list_traces("my-skill", project_dir=".", task="my-task"):
    print(trace["id"], trace["success"], trace["trace_context"]["skill_binding"])

latest = load_trace(
    "my-skill",
    project_dir=".",
    last=True,
    sections=("result", "code"),
)
print(latest.trace_id)
print(latest.sections["result"])
```

## Reflect, Rewrite, And Version

```python
from evolution.evolve.rewrite_outcome import PROMOTED_ELIGIBLE
from evolution.workflow import commit_skill, reflect, rewrite

reflections = reflect(
    "my-skill",
    model="openrouter/anthropic/claude-sonnet-4",
    project_dir=".",
    task="my-task",
)
print(len(reflections))

candidate = rewrite(
    "my-skill",
    model="openrouter/anthropic/claude-sonnet-4",
    project_dir=".",
    task="my-task",
    rewrite_mode="edit-ops",
    version_decay=True,
    max_rewrite_attempts=1,
    apply=True,
)
print(candidate.candidate_status, candidate.reason)

if candidate.candidate_status == PROMOTED_ELIGIBLE:
    version = commit_skill("my-skill", project_dir=".")
    print(version)
```

`rewrite(..., apply=True)` changes the working `SKILL.md` only when guards mark the candidate eligible. `commit_skill` snapshots and promotes the complete managed tree. Manual callers are responsible for independent validation; use the optimizer below when promotion must be automatic and conservative.

## Production Optimizer

```python
from evolution.evolve import load_optimization_status, optimize_skill

result = optimize_skill(
    "my-skill",
    train_tasks=["train-a", "train-b"],
    validate_tasks=["val-a", "val-b"],
    solver_model="openrouter/anthropic/claude-sonnet-4",
    editor_model="openrouter/anthropic/claude-sonnet-4",
    project_dir=".",
    rounds=1,
    trials=4,
    seed=0,
    rewrite_mode="edit-ops",
    version_decay=True,
    max_rewrite_attempts=1,
    agent_permission="safe",
    pass_env=("SSL_CERT_FILE",),
)

print(result.run_id, result.status, result.reason, result.recommendation)
print(result.initial_version, result.final_version)
state = load_optimization_status("my-skill", result.run_id, project_dir=".")
print(state["phase"], state["status"])
```

Training and validation task IDs must be explicit, unique, non-empty, and disjoint. The solver backend must support seed forwarding. Agent-CLI and GigaChat solver backends reject optimizer calls. Every task must require the target exactly once. Supporting skills remain exact active bindings. With `manifest=`, training tasks must be in `train` and validation tasks in `val`.

Fewer than four planned validation pairs are rejected before model calls. Each round checks parent validation saturation on a separate seed namespace. Candidate train diagnostics reuse collect seeds after rewrite. They do not affect promotion. Promotion validation uses new shared parent/candidate seeds and counterbalanced order. It requires no per-task mean regression and a conservative paired sign-flip decision.

Saturated validation and positive-but-underpowered gain return terminal `status="inconclusive"`. The latter keeps the parent active and recommends more `trials`. All non-collect measurements persist only scalar `seed`, `passed`, `total`, and `pass_rate` values; prompts, responses, code, outputs, and evaluator feedback are not persisted.

Use `dry_run=True` for a read-only preflight and call plan. For an interrupted run, call again with `resume=result.run_id` or a saved run ID and identical inputs. Durable resume verifies task hashes, skill trees, bindings, model IDs, and run settings. It also verifies rewrite policy, tokens, permissions, manifests, and passed environment names. A partial legacy run without policy identity requires a fresh run.

Execution failures raise `OptimizationRunError` with a secret-free `reason_code`; the original exception remains available as `__cause__` for an explicit local diagnostic. Checkpoint JSON and CLI output never serialize the original exception text.
