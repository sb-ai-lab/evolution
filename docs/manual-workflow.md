# Manual task and skill workflow

Use this guide when you want to manage individual tasks, saved skills and traces with `evo task`, `evo skill` and `evo evolve`. For campaigns that evaluate an agent through a Target Service, start with the [campaign quickstart](../QUICKSTART.md). These are separate workflows in the same package.

This workflow improves saved Agent Skills through task attempts. [The installation guide](../QUICKSTART.md#1-install-the-beta) covers both a checkout and a supplied wheel.

## 1. Install

From the repository root, install the model-backend extra for this checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[llm]"
```

For an offline checkout install, omit the extra: `python -m pip install -e .`. With a supplied wheel, request `[llm]` on that wheel path.

PDF files are always listed in solver context. To include PDF form-field names, types, and current values when they are available, install the optional enrichment dependency:

```bash
python -m pip install 'PyMuPDF>=1.24'
```

Task-specific evaluator dependencies belong in the operator environment that runs the task.

## 2. Bootstrap A Project

```bash
mkdir my-evolution-project
cd my-evolution-project
evo quickstart -m mock
```

The command creates `tasks/hello-evolution` and `.agents/skills/hello-skill`, then runs an offline attempt. A project starts with no bundled tasks or skills.

## 3. Create Your Own Task And Skill

```bash
evo task new my-task --skill my-skill
evo skill create my-skill -d "Reusable instructions for this task family"
# Edit tasks/my-task/instruction.md and tasks/my-task/tests/test_outputs.py.
export OPENROUTER_API_KEY=...
evo doctor -m openrouter/anthropic/claude-sonnet-4 \
  --task my-task --skill my-skill
```

Set the matching provider credential in the environment. For example, use `OPENAI_API_KEY` with an OpenAI model. The CLI has no credential option.

Task discovery uses an explicit `--dir` first, then `EVO_TASKS_ROOT`, then `tasks/` below the current directory.

## 4. Run The Manual Lifecycle

```bash
evo run solve my-task -m openrouter/anthropic/claude-sonnet-4 \
  --skill my-skill
evo trace list my-skill --task my-task
evo trace show my-skill --last --section result
evo evolve reflect my-skill -m openrouter/anthropic/claude-sonnet-4 \
  --task my-task
evo evolve rewrite my-skill -m openrouter/anthropic/claude-sonnet-4 \
  --task my-task
evo skill diff my-skill
evo skill commit my-skill -m "Improve the general procedure"
```

The CLI has grouped commands only. A solve injects the active immutable version, not an edited working copy, so commit the accepted rewrite before measuring it. Version identity covers the complete managed tree: `SKILL.md`, `scripts/`, `references/`, and `assets/`.

After a rewrite, inspect `.agents/skills/my-skill/.evolution/last_rewrite_audit.json`. The default policy uses promotion-count decay and one editor call. A `candidate_edit_budget_exceeded` result keeps the parent body. Use `--no-version-decay` for a constant budget, or use `--max-rewrite-attempts 2` for one brake-only retry.

The model output budget is `--max-tokens`, then `EVO_LLM_MAX_TOKENS`, then `16384`. Agent CLI backends default to `--permission safe`. Request unsafe mode explicitly. Safe calls require Linux filesystem containment and fail if it is unavailable. Use `--seed N` only with a solver backend that supports seed forwarding. Accepted seeds appear in trace provenance. Agent-CLI and GigaChat solver calls reject the option.

Pass selected environment variables into generated code and the evaluator with a repeatable option:

```bash
evo run solve my-task -m openai/local-model \
  --api-base http://127.0.0.1:8000/v1 \
  --pass-env HTTPS_PROXY --pass-env SSL_CERT_FILE
```

## 5. Optimize With Held-Back Validation

```bash
evo evolve optimize my-skill \
  --train train-a --train train-b \
  --validate val-a --validate val-b \
  --solver-model openrouter/anthropic/claude-sonnet-4 \
  --editor-model openrouter/anthropic/claude-sonnet-4 \
  --trials 4
```

The solver and editor may use separate runtimes. This exact example uses a local Qwen solver and a local GPT-OSS editor:

```bash
evo evolve optimize my-skill \
  --train train-a --train train-b \
  --validate val-a --validate val-b \
  --solver-model openai/Qwen/Qwen3.5-35B-A3B \
  --solver-api-base http://127.0.0.1:32035/v1 \
  --solver-temperature 0 --solver-reasoning-effort off \
  --editor-model openai/openai/gpt-oss-120b \
  --editor-api-base http://127.0.0.1:30004/v1 \
  --editor-temperature 1 --editor-reasoning-effort medium \
  --trials 4
```

`--solver-api-base` and `--editor-api-base` override the shared `--api-base` fallback. Explicit role temperature and reasoning settings override the legacy `EVO_LLM_TEMPERATURE` and `EVO_LLM_REASONING_EFFORT` environment fallbacks.

Training and validation tasks must be explicit and disjoint, and the solver backend must support explicit seed forwarding. Parent and candidate use shared validation seeds in counterbalanced order. At least four pairs are required; any per-task regression or failure of the paired sign-flip gate keeps the parent active. Validation state contains scalar scores only.

The optimizer checkpoints after each phase. Its resume identity includes the edit-brake metric revision, budget, decay schedule, promotion-count source, and attempt limit. Old partial runs without that identity require a fresh run. Use `--dry-run` to preview its plan, `--resume RUN_ID` to continue an interrupted run, and `evo evolve status my-skill [--run RUN_ID]` to inspect state.

Next: [CLI reference](cli.md), [Python API](api-usage.md), and [configuration](configuration.md).
