# Evolving Skills — Reference

Read [SKILL.md](SKILL.md) for the workflow. This reference describes the durable evidence, selection rules, supported backends, and Python API for a saved skill.

## Fitness and lineage

`evo skill history <skill>` derives a version's fitness from its traces. Produce fresh traces for a version before comparing it with its parent. A version without traces displays `-`, not a score.

If `.evolution/lineage.json` is malformed, preview recovery and then rebuild it from immutable snapshots:

```bash
evo skill repair <skill> --dry-run
evo skill repair <skill>
```

Recovery reports any active-version remap. It does not fabricate trace history.

## Traces and editing evidence

Each skill-bound `evo run solve` writes a trace under `.agents/skills/<skill>/.traces/<id>/`. A trace contains `prompt.md`, `response.md`, `solve.py`, `result.json`, and, when collected, `evidence.json`. Reflection adds `reflection.json`.

`result.json` records the task, skill version, model, timestamp, pass counts, pass rate, elapsed time, and errors. Treat missing evidence as missing evidence, not success. Use training traces to diagnose and edit; keep validation and test evidence out of editing prompts.

The public loader is `evolution.lineage.traces.load_trace_evidence(trace_dir)`.

## Promotion gate

`evo evolve optimize` finalizes a candidate, checks its integrity and edit budget, and compares it with the parent on held-back validation tasks. The parent stays active when any validation task regresses, there is no gain, or the result has insufficient power. At least four validation pairs are required. A validation failure fails the run; it is never treated as a zero score.

For a manual workflow, commit a candidate with `--no-promote`, measure both versions on held-back tasks, and use `evo skill promote` only when the evidence supports it.

## Split manifests

`evo run solve`, `evo evolve reflect`, and `evo evolve rewrite` accept `--manifest <split.json>`.

- `evo run solve` also requires `--manifest-scope`: `skill_evolution`, `dev`, `reporting`, or `all`.
- With a manifest, reflection and rewrite use training-scope traces only.
- Inspect membership with `evo split list` and `evo split check`.

See [Train, Validation, And Test Splits](../splits.md) for the operational rules.

## Containers and snapshots

Containers (`.agents/containers/*.yaml`) name groups of skills. A snapshot records exact versions:

```bash
evo skill container snapshot <container> -m "baseline"
evo skill container snapshots <container>
evo run solve <task> -m <model> --snapshot <snapshot>
```

Take a snapshot before an evolution run when you need a reproducible baseline.

## Model backends

`-m` accepts `mock`, `claude`, `openai`, a LiteLLM model string such as `openrouter/...`, `openai/<model> --api-base` for a compatible server, `gigachat[-pro|-max]`, and `codex` or `codex/<model>`.

Codex authentication is file-based. Before a long run, test the selected model interactively with `codex exec -m <model>`. Keep credentials out of task files, manifests, and command output.

## Python API

Use `evolution.workflow` for supported Python composition. The [Python API guide](../api-usage.md) contains runnable examples. `EvolutionEngine` is not a public API.
