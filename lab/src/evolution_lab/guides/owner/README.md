# Python Owner: run prepared work and manage results

`evolution_lab.owner.Owner` is the agent-neutral, in-process owner for one
prepared campaign or fixed evaluation and one managed target. Preparation
remains a separate synchronous API; the Owner consumes its saved digests,
starts the external launcher declared by the Bundle, and runs one explicit
campaign, evaluation, or conformance at a time. Process, core result, cleanup,
and Lab persistence evidence remain separate. Evolution alone owns campaign
journals and promotion; fixed evaluation is a Lab scenario, not a second
campaign engine.

This is a local Linux or macOS Python API. On macOS, a process that leaves the group, escapes the tree between polls and hides the environment marker can survive a stop. It is not a daemon, queue, target pool,
installer, migration tool, or paid-run admission service.

For the optional one-UID Unix-socket service and CLI facade, see the
[local service guide](../service/README.md). It does not replace this Python API
or make target start, check, conformance, cleanup, or deletion implicit.

Owner SQLite schema13 records each start's prepared digest; schema12 added each start's Core refusal findings
and schema11 durable start initiation metadata. Schema10 added execution-policy confirmation and per-start policy history, retaining reflector-probe scopes and the separate per-start
Core-terminal columns introduced in v8. Connect upgrades a schema11 or schema12 state in place to schema13, which
an older Lab then refuses; earlier Owner schemas are refused with `unsupported_schema`, without migration. Deploy with a fresh private state for the producing pair. A matching
schema does not make another producing pair's state reusable. These instructions
name a prerequisite; they do not create state or upgrade old runs.

## Prerequisites

Prepare either a neutral or integration-owned native manifest `/15` (or `/16`), or a closed
evaluation-only input `/1` or `/2` (`/2` adds an explicit fixed-evaluation
budget, `defaults.max_failed_turns`/`defaults.call_retries`), together with a
target launch declaration through
`evolution_lab.preparation`. The
`examples/preparation_s1` (in the Lab repository: `examples/preparation_s1/README.md`) flow
preserves the earlier campaign preparation walkthrough. Evaluation-only input
contains seed/policy/target, corpus, environment, subprocess scorer, metric plan,
and defaults; it has no editor or reflector. The launcher, scorer, corpus and,
for campaigns, role programs and prompts remain owned by the external
integration. Lab does not provide connected-agent examples. Campaign role
programs must use the contracts for their manifest version below.

Custom subprocess Reflectors must accept `reflect_brief/5` for `/15`
and `reflect_brief/6` for `/16`. Editors receive `edit_brief/4` for `/15`
(or `edit_brief/5` with sequential attempt feedback), and `edit_brief/6`
for `/16` (or `edit_brief/7` with that feedback).
Keep older role programs and runs in their producing environments; do not
upgrade an old run in place.

The latest released pair is Lab `0.0.3.dev80` / exact core `2.0.0b21`.
This guide describes development source, including schemas newer than that release;
install the exact Lab and Core distributions producing the run in the execution environment. Keep the wheel archives and declared runtime files at the paths
recorded during preparation. The core archive SHA-256 is
`578f26a0c9c27f63a56fb049f90e4eb51c96e7180c429e333b25255247e0f6aa` (from the delivered wheel's `SHA256SUMS`).
Record your Lab wheel's hash as well: bundled guide edits change the wheel
bytes even when the version string is unchanged. See `docs/RELEASES.md` for delivery history and provenance.
Consumer upgrades remain separate decisions.

Owner checks installed packages, module origins, wheel archives and runtime
files before starting work. Use the installed packages; a checkout supplied
through `PYTHONPATH` does not replace them. Owner does not install or repair
the environment.

Use one fresh private state directory on a local Linux or macOS filesystem for
`PreparationStore` and Owner. Keep old Bundles and results with the environment
that created them for read/resume/cleanup. Choose a new private pair-specific state
path, not the old dev58/b10 state; this guide creates no directory. Owner
SQLite schema13 upgrades a schema11 or schema12 state in place at connect, after which an older Lab
refuses it, and refuses earlier Owner schemas with `unsupported_schema`, without migration. A matching schema does not make another pair's state reusable. The new directory must never have been opened by an older version
pair; do not copy an older database or cleanup receipts into it.
One live Owner locks the directory. Call it from the Python thread
and event loop that opened it.

## Normal lifecycle

One Owner has one managed target slot, including a target that stays `ready`
after evaluation. Starting a target for another Bundle while that slot is
held is refused with `target_slot_held`. Explicitly stop the existing target
and wait for confirmed cleanup before starting another Bundle's target:
`await owner.wait(await owner.stop_target(target_ref))`, or
`lab agent stop TARGET --wait` through the service CLI.

Create or reopen a Bundle with `PreparationStore`, then prepare it separately:

```python
from evolution_lab.preparation import PreparationStore

preparation = PreparationStore(state_directory)
prepared = preparation.prepare_campaign(bundle_digest)
```

`prepare_campaign` reads the saved Bundle and returns a saved execution plan.
It starts no processes. Readiness checks and approval for paid calls remain
separate actions.

When documentation is declared, `PreparedInputs.environment_docs_digest` is the
Lab full-tree inventory digest over every regular UTF-8 file and its byte hash.
It is provenance for the new producing pair, not the core snapshot digest or the
older rendered-text digest; do not compare those values.

The corresponding Owner flow is explicit:

```python
import asyncio

from evolution_lab.owner import Owner, ResourceRef, TargetRef


async def run(prepared, target_secrets, execution_secrets):
    async with Owner.open(state_directory) as owner:
        connection = await owner.bind(prepared.bundle_digest)

        startup_ref = await owner.start_target(
            connection,
            secrets=target_secrets,
        )
        startup = await owner.wait(startup_ref)
        if startup.state != "succeeded" or startup.ref is None:
            return startup.to_payload()

        target_ref = TargetRef(startup.ref.id, startup.owner_generation)
        target = await owner.target(target_ref)

        operation = await owner.start(
            prepared.digest,
            target_ref=target_ref,
            secrets=execution_secrets,
        )
        operation_ref = ResourceRef("operation", operation.id)

        while operation.control_state in {"reserved", "running", "stopping"}:
            await asyncio.sleep(0.5)
            operation = await owner.status(operation_ref)

        report = await owner.report(operation_ref)

        if (await owner.target(target_ref)).state == "ready":
            target_stop_ref = await owner.stop_target(target_ref)
            await owner.wait(target_stop_ref)

        return operation.to_payload(), report
```

The method called `Start` in the Owner contract is the lowercase Python method
`owner.start(...)`. It returns an `OperationStatus` after durable reservation,
not a wait handle. Address it with
`ResourceRef("operation", operation.id)` and poll `status`; Owner intentionally has
no operation `wait` convenience. `task` and `wait` accept only `TaskRef` values
returned by startup, target stop, check, conformance, cleanup, export, and delete
operations.

`report` is independent of process status. Its `.report` value can contain the
full core report and arbitrary integration data, so treat it as private; a
worker exit code is not a campaign outcome. Evolution alone owns campaign
journals and promotion.

`Owner.suspensions`, GET `/v1/operations/{id}/suspensions` and
`lab suspensions OPERATION [--json]` share the closed `lab-suspension-evidence/1`
read. Its identifiers and codes explain recorded pauses, not all remaining work;
counts, digests and truncation remain explicit. The panel reads current evidence
on the suspended screen and loads history after resume when opened. New sequence
values invalidate old answers; failures mark retained evidence stale, and result
deletion clears it. Unavailable or stale history neither proves there were no
pauses nor confirms a current cause.

`async with` closes the Owner. An explicit `await owner.close()` returns a
`ShutdownResult`; its blockers must be inspected. Closing first requests
operation stop, then target stop, and its response deadline is not proof that
all cleanup completed.

## Saved campaign changes and owned result deletion

After a campaign operation is finished and its scope is released, address its
saved result with `ResourceRef("operation", operation.id)`. A direct `diff` reads
the saved Bundle seed and the final champion selected by Evolution; it returns
`DiffResult` without creating a durable task. `text` contains UTF-8 unified
diffs, while `nontext_changes` lists binary and mode changes separately.
`state="no_change"` requires the verified candidate and dependency identities
to agree; it does not mean an unavailable champion or an incomplete result.
Fixed evaluation has no campaign champion, so its diff/export is unavailable.

`export` accepts an Owner-held task. Check its `TaskStatus.state`, `failure`,
and `persistence` before using `result.payload`. A successful export payload
has `state="ready"`, an `export_id` equal to the ZIP SHA-256, and a private
`manifest`. `state="no_change"` has no archive or ID. Read a ready archive by
the same operation ref and ID; `read_export` returns all ZIP bytes. The caller
chooses any external copy destination and owns its write policy. This neutral
snippet assumes an existing completed operation and caller-selected path; it
does not launch an experiment:

```python
from evolution_lab.owner import ResourceRef

operation_ref = ResourceRef("operation", completed_operation.id)
change = await owner.diff(operation_ref)
export_ref = await owner.export(operation_ref)
export_task = await owner.wait(export_ref)
if (export_task.state == "succeeded"
        and export_task.persistence.state == "saved"
        and export_task.result is not None):
    export_result = export_task.result.payload
    if export_result["state"] == "ready":
        archive_bytes = await owner.read_export(
            operation_ref, export_result["export_id"]
        )
        with external_destination.open("xb") as output:
            output.write(archive_bytes)

delete_ref = await owner.delete_results(operation_ref)
delete_task = await owner.wait(delete_ref)
```

`export` and `delete_results` can raise `OwnerError` before acceptance, with a
fixed safe `code` and `stage`. After a `TaskRef` is accepted, inspect the
terminal task's `failure` and its independent `persistence`; a failed delete
also carries a small `DeletionResult` with `state="failed"`. Direct `diff` and
`read_export` raise `OwnerError` themselves. Caller cancellation of an accepted
task wait does not cancel Owner-held export or deletion. `task(ref)` and
`wait(ref)` can rediscover accepted work, while an interrupted delete requires
an explicit repeat `delete_results`; Owner does not automatically resume it.

The `lab-change-set/1` ZIP includes `manifest.json` and payloads for added or
modified writable artifact files. Its manifest records seed/selected digests,
selected version, saved producing distribution pins and archive hashes, known
campaign outcome, and separate campaign/measurement completeness. The outcome
may be unknown or the campaign incomplete; a ZIP is not an improvement or cost
claim. Costs are not calculated here. The file-only format represents add,
modify, delete, and mode changes, with executable normalized to `0` or `1`:
any execute bit maps to `1`, not full POSIX permissions. It cannot describe
changed empty directories or file-to-directory replacement; those changes
return `export_unavailable` instead of silently becoming `no_change`. It does
not apply changes to an external project.

The ZIP and its manifest can contain private artifact content. Diff/export
snapshot and archive construction hold file bytes in memory, and `read_export`
returns the whole archive as one `bytes` object; plan memory for the complete
content and protect any external copy. Lab does not stream or overwrite a
caller-selected destination. A verified published export may be read again or
rediscovered by an explicit repeat export, which reuses its archive hash.

`delete_results` accepts only a released campaign or evaluation OperationRef,
never a conformance TaskRef. It removes the exact owned result tree, including
Lab staging and published exports, and the listed operation-scope log/result
files. Preparation, target snapshots, control receipts and externally copied
archives remain outside that deletion. During deletion, private reports and
ready-export payloads are unavailable; after success, new diff/export/read
return `results_deleted`. A prior small `no_change` task result remains as
historical evidence. If deletion fails, retained state and failure evidence
remain and the caller may explicitly retry. Manual release is an operator's
assertion that external cleanup was already performed; it does not turn
unknown machine cleanup or campaign outcome into confirmed evidence.

`status` and `list_operations` expose `OperationStatus.retained_summary`: null
before deletion intent, then the saved `{campaign_outcome, dispositions}` snapshot.
Outcome stays null when unknown; each disposition contains only `scope_id`,
`disposition` and `reason`. This small snapshot survives partial deletion, retry
and restart, while full core/report and export manifest payloads remain unavailable.

See the Owner results API reference (in the Lab repository: `docs/api/owner-s4.md`) for the
result shapes and refusal conditions.

## Fixed evaluation and explicit conformance

`PreparationStore.create_bundle` accepts `EvaluationSource(input_path,
launch_path)` as well as `CampaignSource`. Evaluation-only `/1` keeps its corpus,
scorer, environment and target inputs immutable without inventing editor or
reflector roles. `prepare_evaluation` always requires one explicit selection:
`{"all": true}`, `{"count": N}`, or a nonempty ordered
`{"case_ids": [...]}`. There is no random selection. Repeat seeds are ordered;
an exact-seed target requires integers, while none/unpaired requires `null`.
Selection describes the scheduled cases and makes no representativeness claim.

Evaluation-only inputs may supply defaults, but callers can override every
evaluation field explicitly. Preparing fixed evaluation from a campaign Bundle
requires explicit selection, repeat seeds, evaluation-set ID, max turns,
timeout and metric plan. Optional failure settings use Lab's `retry` defaults,
never campaign keys. The example chooses explicit failure settings:

```python
prepared = preparation.prepare_evaluation(
    bundle.digest,
    selection={"case_ids": ["case-b", "case-a"]},
    repeat_seeds=[17, 29],
    evaluation_set_id="fixed-v1",
    max_turns=4,
    timeout_seconds=30,
    metric_plan={"primary": "score"},
    max_failed_turns=3,
    call_retries={"max_attempts": 4, "turn_max_attempts": 1},
)
```

Omitted failure settings resolve as described in
[Evaluation budget](../evaluation-inputs/SKILL.md#evaluation-budget). Existing
stored plans keep their original semantics and digests.

Preparation does not run the scorer, target, conformance, or evaluation. Bind
the Bundle, explicitly start the target, then request conformance only when the
launch has the offline declaration `{"kind":"core-session-public/1"}`:

```python
first_start = await owner.wait(await owner.start_target(connection, secrets={}))
first_target = TargetRef(first_start.ref.id, first_start.owner_generation)
conformance = await owner.wait(
    await owner.conformance(connection, target_ref=first_target)
)
first_after = await owner.target(first_target)
```

The declaration is an integration commitment that the complete public core
conformance sequence is offline. It is not evidence that arbitrary integration
code is free, nor paid readiness. The historical
`core-a5-session-public/1` token remains readable but cannot authorize this
runtime conformance. Conformance is never called automatically by create, bind,
prepare, check, start_target, or start.

After conformance may have dispatched, its finalizer always cleans the helper
scope and the managed target scope. Inspect `conformance.result.payload` for the
separate `process`, `cleanup`, and `target_cleanup` facts and inspect
`conformance.persistence` independently. A received `core_report` with
`ok=false` is a completed conformance result, not a process failure. The full
report is private. A successful task, report, or exit code cannot replace
confirmed cleanup. Before evaluation, require both cleanup facts complete and
`first_after.state == "stopped"`, then call `start_target` again and use the new
`TargetRef`; Owner never restarts it implicitly.

Start the fixed evaluation through the common operation API and poll it to a
terminal state:

```python
second_start = await owner.wait(await owner.start_target(connection, secrets={}))
second_target = TargetRef(second_start.ref.id, second_start.owner_generation)
accepted = await owner.start(prepared.digest, target_ref=second_target, secrets={})
operation_ref = ResourceRef("operation", accepted.id)
operation = await owner.status(operation_ref)
```

For evaluation, `OperationResult.core` is the public core measurement projection,
`coverage` compares the confirmed schedule with scored samples, `stopped_reason`
records early stop when known, `unmeasured` lists samples left without a score,
and `campaign_execution` is always `None`. A sample without a score is retried
according to Core's `sample_retries`, then evaluation continues; exhausted
target-call retries are also a sample outcome. Global stops include cancellation,
exhausted budget, Core `exclude` outcomes, a Core failure without a closed try,
and the configured consecutive-unmeasured threshold (`consecutive_unmeasured`,
default 3; `"off"` disables only this stop).

The `.report` returned by `await owner.report(operation_ref)` wraps the projection
under `core`, with `lab_result` carrying coverage, `stopped_reason`, `unmeasured`,
`min_coverage` and persistence, and `measurements` carrying coverage and
scored-sample metrics.
A finished or released evaluation with positive expected coverage, no stop reason
and saved persistence can qualify as a baseline at `min_coverage` (default 0.95; prepared evaluations from before dev60 retain
1.0); accepted partial coverage has `coverage.state: "accepted"` in the report's
measurement, after cross-checking Lab coverage against Core. The report and all trajectories, corpus data, target/scorer logs
and artifacts remain private. Process facts, target cleanup, core result,
coverage and Lab result persistence must be interpreted independently.

Before a confirmed identity, coverage is `None`; a missing journal is
`not_created`, an unreadable journal is `unavailable`, and missing optional stop
facts produce `stopped_reason="unknown"`. Owner does not turn those states into a
zero score or successful evaluation. Unknown mutation or cleanup evidence keeps
the corresponding blocker.

The complete executable path is
`examples/owner/evaluation.py` (in the Lab repository: `examples/owner/evaluation.py`). It
uses core's neutral scaffold and offline sample scorer, the Lab target launcher
(`lab-target-launch/2`), and a synthetic neutral agent module the example
writes itself — no model, it answers only from the message content — prints
lifecycle facts without dumping private reports, and explicitly stops the
second target after evaluation. The older campaign-only
`examples/owner/offline.py` (in the Lab repository: `examples/owner/offline.py`) remains for
its dev7/a8 producing pair.

## Optional check and cooperative Stop

Check remains explicit:

```python
check_ref = await owner.check(connection, target_ref=target_ref)
check_status = await owner.wait(check_ref)
```

It preserves the H1 preflight meaning. It is not conformance, seed attestation,
paid readiness, or permission to run. It holds target use until its task ends.

Request cooperative campaign or evaluation stop with the operation resource
reference:

```python
stop_receipt = await owner.stop(operation_ref)
```

Repeated Stop can report `already_requested`; a completed operation reports
`too_late`. Stop is an intent, not terminal evidence, so keep polling
`owner.status(operation_ref)`. Cancelling the coroutine that is waiting for
admission or task completion cancels only that caller wait once the action may
already be accepted; it does not cancel the Owner-held action. `owner.stop` can
also address a startup, check, or conformance `TaskRef`, but it intentionally
rejects cleanup tasks. Stopping conformance still leaves its one finalizer
responsible for both helper and target cleanup.

## Cleanup after uncertain evidence

Here a **scope** is the group of processes and resources that Lab tracks
for one action. A **receipt** records what its helper confirmed about those
processes. **Settlement** means accounting for the worker's target calls
before releasing the resources it reserved.

Normal or cooperative completion permits reuse only after all of these agree:

- a worker settlement that returned normally, whose core target-call ledger is
  closed, has no in-flight, pending, or uncertain mutating calls, has at least
  one bound transport, and whose namespace equals the run;
- a non-forced helper receipt proving the process tree empty;
- the same managed target boot;
- a successful wire-level sweep of the campaign/evaluation exact instance namespace;
- durable release of the corresponding reservations.

The ledger counts only `POST`/`DELETE` calls made by the current worker
process. Conformance does not use this ledger; after any dispatched
conformance the managed target is always stopped, with no reuse path.

Only then may a later `start` reuse the same live target. A timeout, lost
mutating response, forced stop, missing or invalid receipt, changed boot, failed
DELETE, persistence failure, or lost Owner keeps a blocker. Cleanup for these
cases includes both the operation scope and its associated target scope, so the
target is stopped and a later target requires a new explicit `start_target`.

Cleanup is a separate user action:

```python
cleanup_ref = await owner.cleanup(operation_ref)
cleanup_status = await owner.wait(cleanup_ref)
```

Call it after Stop when a live operation is still running; otherwise it returns
`busy`. `wait_seconds` changes only the cleanup task's response deadline. It
does not change the pinned helper grace period or take ownership of helper
lifetime. Cancelling `wait` likewise does not cancel cleanup. Confirm success
from the returned `TaskStatus`, then inspect `status`, `target`, and `health` as
needed. There is no recursive artifact deletion or general GC.

If the Python process dies, helper EOF initiates stop and reaping. A later
Owner marks retained old-generation resources orphaned and can read them or
perform explicit cleanup, but never resumes, reattaches, or automatically
cleans them. Caller code must keep the original Owner and event loop alive for
normal operation.

## Manual release is an operator attestation

Use manual release only after a human has checked that the leftover processes
are gone and the target port is free. The call records that person's statement
(an attestation); it does not perform the cleanup:

```python
receipt = await owner.release_after_manual_cleanup(
    operation_ref,
    reason="External remnants and fixed port verified clear",
)
```

The reason must be a nonempty string of at most 256 characters and must not
contain secrets. This action releases only the linked reservations. It preserves
unknown cleanup and the existing campaign outcome; it does not create a process
receipt, kill a PID, prove campaign success, or make a busy port safe. Late
evidence for the old scope cannot release a replacement target reservation.

## Inputs and privacy

Bundle inputs are immutable and read-only during execution. The campaign worker
opens the saved copy, not the source project. Scorer and editor processes must
write only to explicit integration-owned output or artifact paths supplied for
that purpose, never to their copied input working directory.

Let `S` be all Bundle secret names, `T` the exact target-launch subset, and `E`
the exact names forwarded to execution roles by the pinned core bindings. For
command grouping, `E` includes its declared `allowed_env` names intersected with
`S`; fields grouping contributes none. Grouping `config` is opaque and Lab does
not rewrite path-looking values inside it. The integration must declare real
file inputs and make remaining locators valid in the saved layout.
`start_target(..., secrets=...)` requires exactly `T`; `start(..., secrets=...)`
requires exactly `E`. Shared names must have identical values or the target must
be stopped and restarted. Names in `S - (T ∪ E)` are deliberately unused. Owner
does not merge ambient environment variables.

Secret values travel through caller memory, private pipes, and the explicit
subprocess environment. Their values and hashes are not stored in SQLite,
receipts, or descriptors. Python does not promise secure memory erasure. Worker,
launcher, scorer, and editor output plus full reports remain private material:
Lab cannot universally redact arbitrary logs, short secret values subject to
core limitations, or values incorrectly declared as nonsecret.

A campaign's optional `reflector.probe` can be launched by Owner through
`reflector_probe` in launch `/3`, or run by the operator when that field is
null. Preparation never calls it. Owner cleans up only its own helper scopes;
it never stops an operator-run probe or deletes its external state. See the
[probe launch reference](../campaign-package/SKILL.md#reflector-probe).
The campaign-only `offline.py` example prompts for secret values by declared
name instead of accepting values in argv, keeps check and cleanup opt-in, and
exposes manual release only as a separate explicit operator command.

<a id="reflector-probe"></a>
## Reflector probe evidence and start refusals (dev62)

Campaign view `lab-campaign-view/15` (both families) and Owner report
`lab-owner-report/15` add the same **top-level** closed triplet, separate from
Core's nested report:

| Field | Closed meaning |
|---|---|
| `reflector_probe` | A record with exactly `launched_by`, `state`, `restarts`, `last_exit`, or null when unavailable |
| `reflector_probe_state` | `present` or `unavailable` |
| `reflector_probe_reason` | null when present; `unreadable` when unavailable |

`launched_by` is `lab` or `operator`; `state` is `running`, `stopped` or
`exhausted`; `restarts` is a nonnegative integer count of replacement attempts
in this worker run; `last_exit` is a signed integer exit code or null. Only an
explicit record `{launched_by: null, state: not_declared, restarts: 0,
last_exit: null}` means no declaration. Missing or malformed Owner evidence is
null/unavailable/unreadable, never inferred absence. This availability is
independent of Core results, checks, binding reads and independent acceptance.
JSON report retains readable Owner facts even if Core is unavailable; Markdown
keeps its preexisting `report_unavailable` contract without a Core report.

An operator probe is checked once at start (also on explicit resume), using
`reflector.probe.request_timeout_seconds`. Its `running`/`stopped` values record
participation in that worker run, not fresh external liveness; Lab never stops
that server and its `last_exit` is null. Raw launch/exit/restart/exhaustion notes
are in private worker `stderr.log`. CLI/watch consume sampled status and show
aggregate probe facts, not a raw-note event stream. The panel shows all four
fields beside checks with independent availability.

| Lab failure code | Fixed message |
|---|---|
| `reflector_probe_unavailable` | The declared reflector probe is unavailable. |
| `reflector_probe_port_busy` | The reflector probe port is already in use. |

These are Lab failures at `before_start`, separate from Core `refusal`;
status/list `/8` and event `/3` retain their forms. On exact b22, `/15` and
`/16`'s default or explicit `suspend` refuse unavailable health before worker
spend. Explicit `/16` `on_unavailable: continue` dispatches with the operator
note and lets Core own reflection availability. Runs declaring that key must
stay with their producing pair; b13 cannot read this declaration. For launch consistency, required `/3` fields, bounded restarts,
explicit-resume counts and helper cleanup, see the
[campaign probe launch guide](../campaign-package/SKILL.md#reflector-probe).
