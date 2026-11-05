# Run Storage Layout

For a tree campaign, start with [the v2 run directory](#the-v2-run-directory) or the [read-only inspection example](running-campaigns.md#inspect-a-run-without-running-it-again). Task/skill optimizer files and tree-campaign files are separate and cannot be exchanged.

## Product rewrite and optimizer evidence

`evo evolve rewrite` writes `rewrite-audit-v1` to `.agents/skills/<name>/.evolution/last_rewrite_audit.json`. Optimizer checkpoints are under `.agents/skills/<name>/.evolution/runs/<run-id>/`. Keep these files when you need to resume or audit an optimizer run. See [configuration](configuration.md#traces-and-auditing) for the task/skill workflow.

## The v2 run directory

`evo tree evolve --run-dir DIR` keeps campaign records and artifacts under `DIR`. Declared programs and the Target Service run under your account and can have their own side effects. Put the run directory outside `seed_root` and outside declared program directories.

```
<run_dir>/
├─ campaign.jsonl          append-only campaign journal
├─ artifacts.jsonl         append-only artifact ledger
├─ measurements/           one journal per baseline or attempt
├─ artifacts/              immutable Champion and Candidate copies
├─ scorer-traces/          scorer request and output records
├─ editor-logs/            editor diagnostics
├─ reflector-logs/         reflection, grouping, and probe diagnostics
├─ environment-docs/       declared document snapshots, when configured
├─ connection/             family 14 connection-preflight journals
├─ connection-logs/        family 14 preflight traces
├─ logs/                   family 15 and evaluation process logs
├─ .grouping/              temporary custom-grouping helpers
└─ .reflections/           temporary, read-only Parent copies for reflection
```

Family 15 (`/16`) also stores immutable comparison inputs under `artifacts/auxiliary/<content_digest>/` and inherited diagnostics under `diagnostic-imports/<bundle_digest>/`. The ledger and its committed records, rather than a path alone, establish what was admitted.

`artifacts/` copies and temporary reflection copies are read-only. If you intentionally remove a completed run directory, make it writable first as described in [move, copy or delete a run directory](running-campaigns.md#move-copy-or-delete-a-run-directory).

## Durable evidence and recovery

`campaign.jsonl`, `artifacts.jsonl`, and each `measurements/<id>/events.jsonl` are durable evidence. Do not edit them by hand. Use `evo tree watch --once` for a status snapshot and the inspection commands in [Running campaigns](running-campaigns.md) to verify a run.

Scorer traces may use a packed `.pack` file or an older per-request layout. Read them through `ScorerTraceReader`, which supports both layouts. A missing trace returns `None`; an unsafe, corrupt, ambiguous, invalid, or oversized trace raises `ScorerTraceUnreadable`.

```python
from evolution.tree import ScorerTraceReader, ScorerTraceUnreadable

try:
    with ScorerTraceReader("runs/demo", run_boundary="nofollow") as reader:
        record = reader.read(
            "3c0bb7c82c594a558c1cdb9bd89023f7",
            measurement_id="m-r01-a02",
            sections=("request", "stdout"),
            max_section_bytes=32 * 2**20,
        )
except ScorerTraceUnreadable as error:
    print(error.code)
else:
    print("missing" if record is None else record.layout)
```

Take the request ID from the measurement journal's `scoring_started` record. In strict `run_boundary="nofollow"` mode, supply a physical path with no symlinked components. Construct a new reader to observe later packed writes.

If a scorer-trace segment is torn, recovery truncates only an incomplete final record. A corrupt segment remains in place and later records use a new segment. If resumption reports missing measurement evidence, a scorer trace, or `artifact_ledger_missing`, restore the named file from a trusted copy and repeat the same command in the same run directory. Do not delete `artifacts/` or ledger records to make a run continue. When required evidence cannot be restored, start a new campaign or a linked successor.

## Role diagnostics

Editor and reflector logs are private diagnostics. They are not campaign-journal evidence and are never role input. Inspect them only when a role check or campaign report identifies the corresponding operation.

Connection preflight uses `connection/<operation_id>/events.jsonl` and `connection-logs/<operation_id>/<operation_id>.pack` for family 14. The preflight verifies the target connection and role visibility before a campaign proceeds. Restore these files from a trusted copy if a recovery report names them; do not invent replacement records.

Scorer traces can contain unredacted request or output data. `evo tree trace --manifest` writes a separately redacted export; handle raw traces as private run data.

## Process logs

`logs/` contains invocation diagnostics only. Process logs are not campaign evidence and may be removed after the command ends. They are never read by campaign recovery, verification, links, or role inputs. For their limits and failure behavior, see [process-log limits](running-campaigns.md#process-log-limits).

## Safe movement and retention

Copy a completed run with its complete directory tree so journals, ledger, measurements, artifacts, and traces remain together. Keep owner-only permissions on run data. Before resuming, use the same directory and command after repairing the external cause. A changed manifest, target, seed, or other bound input can require a new or linked run instead of a resume.
