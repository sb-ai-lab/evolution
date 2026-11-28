# Parallel native campaign through Lab

> **Historical example — Lab dev2/core a3.** These commands describe that
> environment. For the current pair, follow the
> [Owner walkthrough](../../../docs/guide/campaign.md).

This synthetic example runs two Evolution rounds through `prepare_inputs` →
`run_native` → `read_campaign_report`, using the actual A3 target server and
subprocess scorer/editor. No models or credentials are used. It is a connection
example with synthetic inputs and outcomes.

Run in an environment with Lab and the exact core `evo-skills==0.3.0a3`
installed, including core's `tree` dependencies:

```bash
.venv/bin/python -I examples/campaign/offline.py \
  --output /tmp/lab-parallel-campaign-1 \
  --wheel /path/to/evo_skills-0.3.0a3-py3-none-any.whl \
  --max-concurrency 4 \
  --max-scorer-concurrency 2
```

Use a fresh output path. `--wheel` is repeatable for other producing wheels;
each supplied wheel is verified against its installed distribution. Retain the
example source and environment with the run. The program creates `recipe/`,
`run/`, `run-lab-logs/` and `report/`. The example owns and closes its HTTP
server; core owns the campaign's instances and sessions. The admission guard
is empty only because all bindings in this example are synthetic.

## Configuration for a real integration

The existing native manifest is the configuration interface. Set these fields
in its `campaign` object **before** `prepare_inputs`:

```json
{
  "max_concurrency": 4,
  "max_scorer_concurrency": 2
}
```

This is a fragment, not a complete manifest. Lab passes the sealed manifest to
core without additional runtime overrides. Core validates the values, includes
them in run identity and limits rollout/scorer execution. Changing the manifest
after preparation is refused. These settings parallelize episodes within a
measurement; they do not parallelize dependent rounds or start other campaigns.

For the pinned A3 `TargetServer`, callbacks are serialized per instance. Its
default `max_concurrent_sessions=8` describes session capacity, not eight
simultaneous turns. For concurrent turns on separate instances configure the
server with `max_concurrent_sessions=1` and sufficient `max_instances`.
Core computes instances per measurement as
`ceil(max_concurrency / max_concurrent_sessions)` and admits peak demand using
`parallel_attempts * instances_per_measurement`. The example keeps the scaffold's
one attempt at a time and sets server capacity to match. Core materializes and
releases the instances; Lab does not pre-create another pool.

This server profile is specific to A3, not a universal constraint on agents.
An integration must actually isolate world/runtime state and provide the
declared capacity. If capacity is insufficient core refuses admission; Lab does
not silently reduce requested parallelism. Separate repository copies do not
coordinate shared provider quotas or spending limits.

Each integration supplies its own backend, settings, corpus, scorer and budgeted
bindings outside Lab. Do not transfer the synthetic seed guarantee, editor, capacity or
empty guard to a paid experiment. Existing producing environments stay pinned.
Standalone parallelism and the multi-experiment manager are separate deferred
work; this example uses neither.
