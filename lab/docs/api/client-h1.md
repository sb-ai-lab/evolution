# Single-campaign client API (H1)

> **Who this is for.** Programs that want to own exactly one native campaign inside their own process — connect, check, prepare, run, watch, stop and read the report — without Lab's durable state. It is the earlier programmatic route; for durable preparation and results use the [Owner](owner-s4.md) or the [service and CLI](service-s5.md). New to Lab? Start with the [user guide](../../../docs/guide/README.md) and [How Lab works](../../../docs/guide/concepts.md). This page is the exact reference: shapes, states and error codes.

`evolution_lab.client` is the agent-neutral Python interface for owning one
native Evolution campaign operation in one process and event loop. H1 provides
explicit connection and health checks, pinned in-memory preparation, lifecycle
control, and separate process/campaign/report evidence. Evolution remains the
authority for campaign semantics, journals, gates, and promotion.

The local current pair is `evolution-lab==0.0.3.dev55` with exact
`evo-skills==2.0.0b8`, accepting only campaign manifest `evolution-v2-manifest/15` on H1 (the Owner paths also accept `/16`), which Lab writes with the observe smoke profile.
A manifest core refuses (for example a `/15` without `campaign.gate.mode`) is `ClientError` `manifest_invalid`; its `detail` and the exception text carry core's key path and `expected …` phrase, while `Failure` keeps its `{code, stage}`.
Older H1 deliveries used other manifests; their runs remain with the Python
environments and wheels that produced them.

For durable Owner/service work, use a fresh `~/lab-state/dev55-b8`; keep old
runs and state with their producing environments.

On exact dev55/b8, Lab delegates corpus placement to
`evolution.tree.evolve.check_corpus_placement(manifest)` without `run_root` or
`environ`. It checks the core's declared role/grouping paths; it does not look
up an agent CLI on `PATH`. Runtime CLI resolution/checks remain core-owned.
A placement refusal keeps `corpus_inside_role_workspace`. Other helper failures
are not a successful check: H1 uses `manifest_invalid`/`io_error`; legacy recipe
uses `PreparationArtifactsInvalid`; Bundle uses `manifest_invalid`/`io_error`
with existing source-drift precedence; project package checks use
`manifest_invalid`/`unsupported_input`, with safe details. A missing corpus stays
with the existing corpus-validation stage. This exact-pin import is not a
promise of a stable beta facade.

## Public calls

```python
connection = connect(manifest_path)
checked = await check(connection)
prepared = prepare(
    connection,
    wheel_paths=(lab_wheel, core_wheel),
    extra_paths=(),
    env=dict(os.environ),
)
operation = create_run(
    prepared,
    run_directory,
    dispatch_guard=admit,
    grace_seconds=None,
)
task = asyncio.create_task(operation.run(on_event=on_event))
snapshot = await status(operation, secrets=())
receipt = stop(operation)
result = await task
report_value = report(run_directory)
```

`check` is always explicit. It is neither an automatic prerequisite of
`prepare` nor an execution admission decision. `create_run` is inert: it starts
no tasks or processes and creates no directories. `RunOperation.run()` may be
called once. `stop()` records intent; its receipt does not by itself prove that
the process tree was cleaned up.

The manifest, producing wheels, seed, corpus, role programs, Reflector inputs,
environment docs, and `extra_paths` are pinned by preparation. `prepare` requires
an explicit `Mapping[str, str]`; missing values are not merged from the ambient
environment. H1 passes a copy of that mapping as the core process environment
(`Popen(..., env=...)`). It does not normalize Python startup, and roles
receive core-filtered environments. Any environment change requires a new
preparation.

Legacy `prepare_inputs(..., env=None)` remains ambient: `verify()` compares the
current process environment and `verify(env=...)` requires exact equality with
that ambient snapshot. Legacy `run_native` uses that ambient check before and
after its run. For an arbitrary explicit environment, use the H1 client rather
than expecting the legacy supervisor protocol to transport it.

## Public response fields

Every `.public()` call returns a fresh JSON-safe dictionary. Unknown facts are
`null`; private ownership objects and environment values are omitted.

`Connection.public()`:

```text
schema: lab-connection/1
manifest_digest: string
editor_kind: subprocess | chat | agent_cli
```

`editor_kind` describes only `workflow.editor`. It neither identifies the
Reflector kind nor proves that the campaign has no paid calls.

`CheckResult.public()`:

```text
schema: lab-check/1
state: passed | failed
failure: Failure | null
```

Passing means the pinned core preflight accepted the target's reported
health/binding/capabilities/policy at that moment. It does not attest target
candidate digests, call materialization or a scenario, prove conformance or
scorer quality, guarantee continued readiness, or authorize spending.
Operational unavailability maps to `target_unavailable`; supported pin,
capability, limit, policy, attestation, and response disagreements map to
`target_mismatch`. Unknown core failures remain `lab_error`, without matching
exception text. Since core 0.3.0a32 (#140, #169) a target reply cut off before
the end of its framing is a core `transport_error`, so it maps to
`target_unavailable`. Before a32 a reply cut inside its headers was an
`invalid_response`, which mapped to `target_mismatch`.

`PreparedCampaign.public()`:

```text
schema: lab-prepared/1
manifest_digest: string
seed: {candidate_digest: string, dependency_digest: string}
case_count: integer
distributions: [{name: string, version: string, wheel_sha256: string}]
```

This is metadata for an in-memory preparation, not a persistent prepared
bundle.

`case_count` is the corpus total: on a `/15` corpus labelled `train`/`test` it counts both parts, while the
campaign itself runs on the train part only.

`RunResult.public()`:

```text
schema: lab-run-result/1
operation_id: string
state: exited | stopped | failed | unknown
stage: before_start | spawning | running | stopping | finalizing | done
launched: boolean | null
exit_code: integer | null
stop_requested: boolean
stop_reason: user | cancelled | budget_exhausted | dispatch_refused |
             callback_failed | lab_error | null
stop_effect: not_requested | prevented_start | delivered | too_late | unknown
core_force_requested: boolean | null
cleanup_complete: boolean | null
failure: Failure | null
secondary_failures: [Failure]
persistence: {state: saved | not_attempted | failed, failure: Failure | null}
```

The immutable result is assigned once to `operation.result`. `exit_code` is the
core exit code, never the supervisor code; `exited` is a process fact, not a
successful campaign verdict. A persistence failure is confined to the
`persistence` receipt and does not erase or reclassify the in-memory execution
result. `not_attempted` means no owned logs directory existed; it is not an I/O
failure. The stored `h1-result.json` contains the execution snapshot under
schema `lab-execution-result/1` and intentionally omits the self-referential
persistence receipt.

`StatusSnapshot.public()`:

```text
schema: lab-status/1
operation_id: string
state: created | active | exited | stopped | failed | unknown
stage: before_start | spawning | running | stopping | finalizing | done
launched: boolean | null
exit_code: integer | null
stop_requested: boolean
cleanup_complete: boolean | null
result: RunResult | null
campaign: {
  availability: not_started | unknown | available | unavailable,
  projection: core campaign status payload | null,
  failure: Failure | null
}
```

`status` takes one local `RunOperation`; it does not attach to another process.
It reads the public core campaign projection with verification disabled and does
not call `check` or `report`. Process and journal snapshots are not atomic. Only family14 is readable.
Typed unsupported-family/unreadable failures give campaign availability
`unavailable`, a null projection and `Failure("campaign_unavailable", "status")`.
Empty/torn creation without validated identity is unreadable before family
classification. A wrong exact core pin is separate: status gives `unknown`,
a null projection and `Failure("lab_error", "status")`; report gives reason
`unknown`. Process facts remain independent. Old runs remain readable, resumable
and cleanable only with their producing wheels; H1 still has no resume/reattach.
`secrets` is an explicit sequence of strings forwarded to the core projection;
all environment values are not automatically declared secret.

A **suspended** campaign (family 7: core replaces an infrastructure terminal
with a non-terminal `campaign_suspended` record and keeps the run directory)
shows up inside `campaign.projection` exactly as core reports it —
`suspended: true` and a `suspensions` list, same shape as everywhere else in
Lab. H1 passes this through unchanged: it does not summarize, flag or offer a
continuation for it, and this does not change in this version — PDR-0009,
non-goals. The Owner, the service and the CLI have their own explicit
continuation instead (`Owner.resume` / `lab resume`, [Owner API
reference](owner-s4.md#resume)); H1 callers who need to continue a suspended
run must use one of those. `report(run_directory)` shows the same facts
through the core report. See [Read the
results](../../../docs/guide/results.md#suspended-campaigns).

`StopReceipt.public()`:

```text
schema: lab-stop/1
operation_id: string
disposition: requested | already_requested | already_terminal
```

Lifecycle callbacks receive exactly these keys:

```text
schema: lab-lifecycle/1
operation_id: string
sequence: integer, starting at 1
kind: core_started | stop_requested | core_exited | completed
launched: boolean | null
exit_code: integer | null
stop_requested: boolean
cleanup_complete: boolean | null
```

Each kind is emitted at most once. Missing evidence is not synthesized.
Callbacks are synchronous and must return quickly. `completed` ends lifecycle
evidence collection but occurs before final persistence and assignment of
`operation.result`.

A `Failure` is exactly `{code, stage}`. Codes are `manifest_invalid`,
`linked_run_unsupported`, `grouper_kind_unsupported`, `artifacts_invalid`, `environment_invalid`,
`inputs_changed`, `target_unavailable`, `target_mismatch`, `path_conflict`,
`invalid_argument`, `dispatch_refused`, `spawn_failed`, `lifecycle_unavailable`,
`campaign_unavailable`, `callback_failed`, `io_error`, or `lab_error`. Stages
are `connect`, `check`, `prepare`, `before_start`, `spawning`, `running`,
`stopping`, `finalizing`, or `status`. Expected failures from
`connect`/`prepare`/`create_run` use `ClientError`; `check` returns a failed
value; ordinary run failures are represented in `RunResult`. `connect` refuses
a manifest that declares `linked_from` with `linked_run_unsupported`:
continuing a campaign from another run is not a Lab capability (PDR-0013). `connect` refuses a `/15` manifest whose
`grouper.kind` is `agent_cli` with `grouper_kind_unsupported` (stage `connect`), before any side effect: Lab supports
the Grouper kinds `fields`, `command` and `chat` (PDR-0015). Bundle preparation, the project form and the legacy
`recipe.prepare_inputs` refuse it the same way.

## Three separate projections

| Projection | Authority and meaning |
| --- | --- |
| Lifecycle/result | Lab's evidence about dispatch, core exit, stop effect, cleanup, and local evidence publication. |
| Campaign | Evolution's public view of rounds, measurements, integrity, gate, and outcome. Lab does not recompute it. |
| Report | Authorized read combining the existing core report with valid Lab result evidence when present. |

`report(run_directory)` returns the existing `Report`; `Report.data` retains the
core report and places Lab's stored execution snapshot in `lab_result` when
available. The snapshot has no persistence receipt. Missing H1 evidence keeps
legacy behavior. Competing legacy and H1 results, or invalid H1 evidence, are
not silently ignored. `ReportUnavailable.reason` is one of `unavailable`,
`invalid`, `io_error`, or `unknown`. Ambiguous core schema/version errors remain
`unknown`; H1 does not parse exception messages or private journal headers to
guess a cause. A readable partial core projection remains partial rather than
being converted to a failure.

For the core report, typed journal-unavailable/busy/capacity failures and
`CampaignUnreadable` and `CampaignFamilyUnsupported` are `unavailable`; a typed journal limit/integrity failure
may be `invalid`. An ambiguous `ValueError` remains `unknown`. Direct I/O errors
while reading Lab-owned evidence are `io_error`. These late read failures never
rewrite an already collected `RunResult` or Evolution campaign outcome. Status
uses its own closed `campaign_unavailable`/`lab_error` projection boundary and
likewise does not mutate the process result.

Reports and campaign projections are for an authorized local consumer. They may
contain core evidence; the API does not promise a general DLP scrubber. Raw
stdout/stderr and environment values are not included in the closed Lab values.

## Ownership and limitations

Keep the Python object, process and event loop alive for the whole operation.
H1 cannot reattach after that process dies, and its preparation is held only
in memory. Use the Owner for durable Bundles and managed targets; H1 itself
provides no target launcher, installer, daemon, shared manager, archive,
transport or idempotency across processes. If supervisor evidence is lost, terminal
`unknown` with `cleanup_complete=null` can mean descendants are still running.
An `operation_id` is correlation, not a process capability.

The default grace is the sum of editor timeout, reflection timeout, scorer
timeout, campaign budget timeout, target connect/read timeouts, and 10 seconds.
It also bounds readiness. The extra 10 seconds is not a sufficiency guarantee
against core's 30-second editor/Reflector wrapper grace; callers may supply an
explicit positive `grace_seconds` suitable for their processes. A forced stop
is reported rather than hidden.

**Historical core a5 note.** That version had a known command-identity/PATH collision for some manifests,
including a bare `argv[0]` that collides with a file in `cwd`. H1 does not fix it
or add another rejection gate. Prefer explicit absolute executable and script
paths, as in the walkthrough, and let canonical core binding remain the
authority.

Paid execution remains restricted by the consuming integration's admission
rules. Explicit `check` and an editor kind do not relax that policy. A dispatch
guard failure prevents or stops dispatch, but H1 does not create a universal
cost model or certify an external target.

## Producing-version compatibility

Compatibility is scoped to an exact installed Lab/core pair. The legacy
functions `prepare_inputs`, `run_native`, `NativeRun`, `read_campaign_report`
and `read_evaluation_report` remain available; their existence does not imply
that current code can read an older journal or execute an older manifest.

There is no automatic manifest conversion or migration of runs. Keep old
runs with their original wheels and environments. An external consumer such
as auto-evolve must explicitly adopt a new pair and map its public API.

**Historical compatibility evidence.** The original H1 a5 delivery exercised
those legacy signatures on neutral `/3` fixtures without requiring `env`.
That established compatibility within that pair, not `/2` execution or
cross-version journal reading.

See the [installed neutral walkthrough](../../examples/client/README.md).
