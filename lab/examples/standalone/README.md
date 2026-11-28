# Standalone integration on the A3 preview

> **Historical example — Lab dev2/core a3.** These commands describe that
> environment. For the current pair, follow the
> [Owner walkthrough](../../../docs/guide/first-evaluation.md).

This example is model-free. It exercises installed Lab/core bytes, both core
conformance suites, a server-owned snapshot, two declared repeats, a subprocess
scorer with persisted inputs, and a report. Its backend and data are synthetic.

Install the delivered Lab `0.0.3.dev2` and core `0.3.0a3` wheels with
`evolution-lab[dev,gateway]` and `evo-skills[tree]`. Then, from any directory:

```sh
/path/to/venv/bin/python -I /path/to/examples/standalone/offline.py \
  --output /path/to/fresh-output \
  --wheel /path/to/evolution_lab-0.0.3.dev2-py3-none-any.whl \
  --wheel /path/to/evo_skills-0.3.0a3-py3-none-any.whl
```

Outputs: `conformance.json`, `evaluation/artifacts/`,
`evaluation/measurement/`, `evaluation/evaluation-inputs.json`, `traces/`,
and `report/`. The scorer's private request traces contain oracle and full
scoring input; do not publish them as public status. Reusing output is refused.

## What the integration agent supplies

- A backend implementing the published core `TargetBackend` protocol. Use
  `TargetServer` / `serve_target`; do not write another wire server.
- The agent's runtime, isolated world/session resources, settings and honest
  semantic identity. Read candidate materials only from `LoadedTree.root`.
  Bound callbacks and response size; every reply must contain an event.
- An `ArtifactPolicyV1` defining permitted changes and fixed dependencies.
- Native `EvalCase` objects with separate public/private/oracle data, an
  `Environment`, and a scorer. A subprocess scorer reads one JSON object from
  stdin and writes core's score JSON to stdout; use `SubprocessScorer` to bind
  its program/dependencies and preserve evidence. See `score.py` and core docs.
- Explicit repeat seeds, turn/time limits, metric plan, exact wheel pins and
  the run's dispatch conditions. The sample's no-op guard is only for its
  synthetic no-model backend/scorer, never a paid-run recipe.

Call `evolution_lab.standalone.evaluate_target` with these inputs. Lab creates
the copied ArtifactStore, preflights/materializes the target, constructs native
measurement identity and the case-major schedule, invokes `evaluate_fixed`,
and releases its target instance. There is no editor or promotion. Use
`read_evaluation_report(run_directory / "measurement", distributions=pins)`
for the result. Lab source code does not need agent-specific edits.

`repeat_seeds=(None, None)` means two unseeded repeats, not deterministic
pairing. Seed mode `exact` requires explicit integer seeds. This entry point
is sequential (`max_concurrency=1` for execution and scoring).

The operation requires a fresh output directory separate from the source and
inside the target's declared allowed artifact root. The caller starts and
stops its own target/gateway processes; external services are not stopped.
Cleanup failure propagates. Crash recovery and automatic resume are absent.

`dispatch_guard` runs before preparation/dispatch and between execution and
scoring/samples; `RolloutBudget.cancelled` is preserved. Each provider request
must additionally pass through the budget gateway. The callback is not a paid
authorization or a complete cost preflight. The integration agent prepares
models/settings, retry bounds, call counts and spend cap for human approval.

The Python function is an experimental pinned-wheel interface, not a stable
serialized recipe schema. `metric_plan` is recorded and hashed as declared;
the scorer defines metrics, and Lab does not invent statistical claims.
Persist external world/model configuration and all relevant producing pins
alongside the integration. `evaluation-inputs.json` alone is not a complete
portable recipe or a token authorizing later runs.

Agent adapters, domain scorers, prompts and concrete experiment recipes belong
to the agent's integration outside Lab. This directory demonstrates only the
general contract with synthetic inputs.
