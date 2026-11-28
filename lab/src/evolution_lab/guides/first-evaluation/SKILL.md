---
name: evolution-lab-first-evaluation
description: Use when measuring a Python agent for the first time with lab new-project and lab evaluate, or diagnosing that first baseline.
---

# First evaluation

Use this path for a first baseline from a directory with Lab installed. The command runs its own evaluation locally. Start from [connect](../scenarios/connect/SKILL.md) when the user wants readiness recorded for a registered project.

## Measure the skeleton first

Keep the agent's existing files. If the evaluation files already exist, use that project and skip `new-project`. Otherwise choose a new or empty directory for the skeleton. Run these commands from the Lab checkout's `lab/` directory after setup.

```sh
just new-project DIR
just evaluate DIR
```

For an installed `lab` outside the checkout, use `lab new-project DIR` and `lab evaluate DIR --wheel LAB_WHEEL --wheel CORE_WHEEL`. Supply the exact wheels installed in the evaluation environment. The checkout recipes supply both wheels. The untouched skeleton runs offline and typically reports `3/3 samples scored` and mean `0.67`. That score belongs to the stub agent and its example cases.

## Measure the user's agent

1. Wire the actual agent through `agent/adapter.py`. Put code needed by the adapter and scorer in the project or install it as a dependency in their Python environment. An agent kept in another directory can be installed in a prepared environment selected with `just evaluate DIR --python PATH` or `lab evaluate DIR --python PATH --wheel LAB_WHEEL --wheel CORE_WHEEL`. Install the same Lab and Core wheels there. Lab copies the project for each run and excludes `.venv`, so declare what the run needs in that environment and [follow the target contract](../target-connector/SKILL.md#neutral-contract). Keep required settings in `launch.json`, pass declared secrets through the environment, and preserve `lab:auto` for fields Lab can resolve.
2. Replace the sample `corpus.jsonl` and `scorer/score.py` with representative cases and scoring rules. Check the named primary metric in `evaluation-input.json`. Use [evaluation inputs](../evaluation-inputs/SKILL.md) for the exact forms.
3. Run `evaluate` again. Get approval with expected calls and a cost cap before an evaluation that uses a paid agent.

## Read the result

| Exit | What to report or inspect |
|---|---|
| `0` | Accepted baseline. Report scored versus expected samples, the primary metric and value, and the run path. Acceptance records coverage, persistence and cleanup. The metric measures quality. |
| `1` | Measurement rejected. Read the recorded reason and coverage. Inspect the conformance or scorer evidence. |
| `2` | Project or argument error. Fix the reported file, field, corpus line or argument. |
| `3` | Target, environment or Lab failure. Report the recorded reason, failure stage and available evidence. Read the run's `evaluate.log` when present. For `target_unavailable`, open the target stderr named in the readout and check adapter import, construction and declared settings. If the run stopped before writing files, use its stderr. |
| `130` | Interrupted run. Preserve the state path. A later `evaluate` is a fresh run. |

Open the run directory printed by Lab, under `DIR/.lab/runs/`. Use `summary.json` for acceptance and coverage, `report.md` for the report, `conversations.md` for sample evidence and `resolved.json` for the resolved configuration. Keep run data private. For an experiment that improves the agent after this baseline, see [campaign package](../campaign-package/SKILL.md#choose-a-project-campaigns-first-baseline).
