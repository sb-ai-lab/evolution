# Custom Tasks

Evolution does not require SkillsBench, SB-Bench, or paper manifests. A task is a directory that describes what the agent should do and how the produced workspace is evaluated.

## Minimal Layout

```text
my-tasks/
  write-report/
    task.toml
    instruction.md
    inputs/
      source.csv
    tests/
      test_outputs.py
```

`task.toml`:

```toml
version = "1.0"

[metadata]
name = "write-report"
difficulty = "easy"
category = "reports"
origin = "custom"

[skills]
required = ["csv-reporting"]

[evaluation]
timeout_sec = "60"
test_file = "tests/test_outputs.py"
```

`instruction.md` is the task prompt. Files under `inputs/` are copied to the workspace before each solve. The evaluator runs with `TASK_WORKSPACE` set to the workspace path.

## Run The Lifecycle

```bash
evo skill create csv-reporting -d "Reusable CSV reporting procedure"
evo task list --dir ./my-tasks
evo run solve write-report -m mock --dir ./my-tasks -w /tmp/evo-write-report
evo trace list csv-reporting --task write-report
evo trace show csv-reporting --last -s code
```

After real model runs produce useful failures:

```bash
evo evolve reflect csv-reporting -m <reflector-model> --task write-report --tasks-dir ./my-tasks
evo evolve rewrite csv-reporting -m <rewriter-model> --task write-report --tasks-dir ./my-tasks
evo skill diff csv-reporting
evo skill commit csv-reporting -m "Improve report writing procedure"
```

## Python API

```python
from evolution.workflow import solve, reflect, rewrite, commit_skill

solve("write-report", model="mock", tasks_dir="./my-tasks", workspace="/tmp/evo-write-report")
reflect("csv-reporting", model="openrouter/anthropic/claude-sonnet-4", task="write-report", tasks_dir="./my-tasks")
rewrite("csv-reporting", model="codex/gpt-5.5", task="write-report", tasks_dir="./my-tasks", apply=True)
commit_skill("csv-reporting")
```

The API uses generic task, skill, trace, and version objects. Paper-specific terms such as table, row, cell, anchor, and tier belong in `experiments/`, not in custom task code.
