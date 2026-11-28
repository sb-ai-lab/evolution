# Local observations: neutral example

Use this example when you already have recorded traces and want to select
requests for later evaluation. It uses hand-authored sorting data, so you can
learn the import flow without running an agent or calling a provider.

Run from the repository checkout with Lab installed in your Python environment
and a new output path:

```sh
python examples/observations/offline.py --output /tmp/my-observation-example
```

It imports two batches of one trace, selects the observed request as a case, and
writes an inventory of the recorded execution. It does not validate the answer,
produce a score, or admit a baseline to a campaign. No automatic replay occurs.

## What you get

```text
my-observation-example/
  observations/    normalized trace data and mapped executions
  basket/          selected requests and their source references
  recorded-plan/   which recorded executions are available for those requests
```

The script prints a JSON summary with `observations`, `basket` and
`recorded_plan`. The stages answer different questions:

| Object | Question it answers in this example |
|---|---|
| Trace | What events were recorded during the sorting request? |
| `ObservationSet` | Which execution can the mapper reconstruct from those events? |
| `Basket` | Which observed request did you select for later use? |
| `RecordedEvaluationPlan` | Is a recorded execution available for that selection, and what conditions are known? |

The last object is an inventory, not a score or an executed evaluation. To
adapt the flow, implement your own mapper outside Lab, decide which data to
retain, and select the requests you need. Scoring recorded answers and using
them as a campaign baseline require additional contracts; the importer does
not perform those steps.

## Python API and handling data

Python entry points are in `evolution_lab.observations`:

- `import_traces(groups, output_directory)` returns an `ObservationSet`.
- `build_basket(observations, selections, output_directory, purpose=..., declared_usage=...)`
  returns a `Basket`. Only `input` and `context` are target input; source references
  and historical output remain provenance/scorer data.
- `prepare_recorded_evaluation(basket, observations, output_directory)` returns a
  `RecordedEvaluationPlan`: availability and known conditions, no scoring/coverage/repeats.

Use `ObservationSet.open(path)` and `Basket.open(path)` to read saved artifacts.
These explicit reads check their recorded file digests; they do not reopen raw
exports or launch any external checks. `public()` returns only generated IDs,
counts and fixed diagnostic codes. Full artifacts contain private observations.

`RetentionPolicy` selects exact attribute/metadata keys; the default retains no
payload. Reserved keys `status_message`, `events`, `links`, `resource_attributes`,
`scope_attributes` retain the corresponding entire block. This does not scrub
secrets inside an allowed field. Pass a trusted mapper object; trace strings
never name executable code. Domain-specific mappers belong to external integrations.

Each ImportGroup is isolated. Batches within it merge by trace/span identity;
conflicting or ambiguous representations stay in `spans.jsonl` with diagnostics
and are not mapped. If necessary, explicitly import one representation. An unknown
producer namespace is operation-local, never proof that two imports are different
executions. Recorded/declarative facts remain separate.

Artifacts publish their manifest last and never overwrite an existing directory.
An interrupted write can leave a private directory without a manifest; it is not
complete. Select a new output path after a failure. The importer does not delete
user directories. Operation errors expose `ObservationError.code`; per-source or
mapper problems are recorded as partial-import diagnostics without exception text.

## Next: adapt the mapper

1. Read [`mapper.py`](mapper.py): it turns selected sorting-trace attributes into an execution.
2. `src/evolution_lab/observations/__init__.py` for exported Python objects.

AEF SDK installation and actual agent runs are not required for this example.
Support for a real connected agent has not been demonstrated by these fixtures.
