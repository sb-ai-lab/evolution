# Local service and CLI reference

> **Who this is for.** Anyone running Lab through the `lab` command or talking to the local service over its Unix socket from another program. For a guided tour of the commands, read [CLI and local service](../../../docs/guide/cli.md) first. This page is the exact reference: routes, request and response shapes, states, error codes and the service's guarantees.

`evolution_lab.service` exposes the existing preparation and Owner APIs over one
local Unix-domain socket. It is optional: direct Python `PreparationStore` and
`Owner` use remains available without FastAPI or uvicorn. The `lab` console is a
small UDS client, not a daemon launcher or a typed HTTP SDK.

This is a Linux, one-UID interface. It has no TCP listener, remote users, UI,
queue, retries, recovery, state migration, worker event history, or automatic
target/check/conformance action. Evolution remains the campaign authority.

## State and private inputs

Use a fresh state directory whenever either the Lab or core version changes.
Keep earlier state directories with the environments that created them. Lab
provides no migration and does not enforce this version rule for you; core
checks some version mismatches, but cannot catch a Lab-only version change.
Do not copy old databases, cleanup receipts or run trees into the new state.
Owner's SQLite state schema is `user_version` 10 for dev65. It adds
confirmed execution policies and per-start policy history, retaining earlier
probe/Core-terminal facts. V9 and earlier are refused with
`unsupported_schema`; a new empty database initializes to v10. There is no
migration, folder copying or import of old rows. A matching schema does not
make another pair's state reusable. A fresh private state and separate
dev65/b15 pair acceptance are required; this reference creates no state.

<a id="lab-home"></a>
**The Lab home** is the one directory that outlives version pairs: it holds the
project registry (`projects.sqlite3`), Lab's clones and the single-writer lock.
`lab serve --state S --home H` names it; without `--home` the service takes
`$LAB_HOME`, else `$XDG_DATA_HOME/evolution-lab`, else
`~/.local/share/evolution-lab`. The same rules as for the state directory apply
(absolute, no symlinks, owned by the current UID, mode `0700`); Lab creates it on
first use. The service holds an exclusive lock on `H/home.lock` for its whole
life, so a second `lab serve` on the same home exits `1` with `home_busy`
(stage `project`) before it creates a state directory or a socket; run two
pairs at once with two homes. The state root may live inside the home (for
example `H/pairs/dev28`) but not in its `clones/` or `workspaces/` (not
checked yet — keep it out yourself). The
registry is migrated in place by adding only, never replaced per pair, so a
new pair keeps every project; the previous pair's state root is never opened.
Tests and scripts that start a service should pass their own `--home`. See
[Projects](projects.md#the-lab-home).

This reference follows dev65 development source. The prepared source pair is
Lab `0.0.3.dev72` with Core `2.0.0b17`. See the [Lab release history](../RELEASES.md).
Use the fresh-directory rule above for a new run.

`lab serve` requires absolute paths. The state directory and socket parent must
be owned by the current UID and mode `0700`; the service creates a socket with
mode `0600`. Bundle request paths are local paths on the machine running the
service. They may identify source files, declared roots, runtime files and exact
wheel archives, so request JSON, state, reports, ZIPs, and stderr logs are
private inputs/outputs and should not be placed in a shared directory.

**`--socket` is optional (W49).** Without it, `lab serve` listens on
`<home>/service.sock` — the Lab home from `--home`, else `$LAB_HOME`, else
`$XDG_DATA_HOME/evolution-lab`, else `~/.local/share/evolution-lab`
([the Lab home](#lab-home)). Every client command resolves the socket the
same way (`resolve_socket`): an explicit `--socket` wins, else the client's
own `--home`, else the same environment and default fall-through. A
home-derived path that would not fit a Unix socket address (108 bytes,
including the terminating NUL) is refused with `socket_path_too_long` before
anything is created — by `lab serve` (stage `transport`) and, for the
client's own resolution, as a usage error; pass `--socket` explicitly with a
shorter path. `lab guides` and `lab scenarios` never resolve a socket: they
reject both `--socket` and `--home`.

If a socket pathname already exists, serve refuses it with
`socket_path_exists`. It never probes or unlinks it. After establishing that
the previous service is gone, the operator may manually remove that stale
socket and start again. Do not remove a pathname you do not own or cannot
identify as stale.

Start one foreground service, socket in the home:

```bash
lab serve --state /private/lab-state --home /private/lab-home \
  --shutdown-wait-seconds 30
```

All client globals (`--socket`, `--home`, `--json`, `--timeout`, and the S7
request-key options described in [Mutation keys](#mutation-keys-s7)) precede
the ordinary command:

```bash
lab --home /private/lab-home --json health
```

Use `--json` for the complete response, especially when another program
needs IDs, warnings or result fields. Human output is a compact summary.
Malformed request, secret or policy file content currently exits `1` rather
than the intended usage exit `2`; account for that when handling CLI errors.

## Execution policy confirmation and routing

The [execution policy API](execution-policy.md) records the exact closed
policy/confirmation forms, Owner methods and routing rows. `lab exec-policy digest
FILE` is offline and prints a bare digest. `policy confirm FILE --expect DIGEST
--author AUTHOR --channel CHANNEL [--note TEXT]` sends
`lab-service-policy-confirm-request/1` with required nullable `note`.
`POST /connections/C/target` accepts `/2` with `schema`, `secrets`,
`execution_policy` (digest or null); `/1` without a policy remains accepted.
Policy lives in pair state, not the Lab home; no policy means no Lab gateway.
Routed-role manifest pins are refused only under a policy. Confirmed routes
change through target stop/cleanup → new confirmation → target start → resume,
with the original gateway block and Bundle retained. Report `/13` has optional
`routing`; no-policy reports omit it and `report.md` is unchanged.

## Suspension evidence reads

`GET /v1/operations/O/suspensions` calls public `Owner.suspensions` under the
service-owned read lifetime. `lab suspensions O [--json]` validates and prints
the identical closed response. See [Owner suspension evidence](owner-s4.md#suspension-evidence)
for every field and nullable reason. The route is also allowed by the read-only
panel gateway. It requires a valid operation identifier and empty body, rejects
all query parameters (including `disclose`) and mutation-key headers, and uses
the ordinary GET validation and `Cache-Control: no-store` rules.

A successful empty history is HTTP 200 available with `suspensions: []`.
Unavailable journals, unsupported pin/family/shape and non-campaign operations
are HTTP 200 with a reason and `suspensions: null`; deletion is different:
`deleted` gives 409 `results_deleted`, `deleting`/`delete_failed` and missing
operations give 404 `not_found`. No partial or cached deleted content is served.
Request cancellation does not cancel the service-owned Owner read.

Text output includes every returned record and row, seq/reason/owner/kind,
count/truncated, fault reason and nullable code, and all closed non-row evidence
fields. A successful empty list prints `no suspensions`; unavailable is not empty.
Rows explain a historical cause, not the complete remaining worklist or current
pause after resume, and never imply promotion.

## HTTP map and request files

The server exposes the `/v1` HTTP routes and fixed request shapes below. `POST` request objects use
the listed service schema on the wire; CLI `bundle create` and `prepare` files
omit that schema because the client supplies it. GET and DELETE requests do not
accept a body. All JSON responses and failures use `Cache-Control: no-store`.

| HTTP route | CLI command | Public result |
| --- | --- | --- |
| `POST /bundles`, `GET /bundles/B` | `bundle create`, `bundle show` | Bundle summary |
| `POST /prepared-executions`, `GET /prepared-executions/P` | `prepare`, `prepared show` | Prepared summary |
| `POST /connections/from-bundle` | `connect B` | in-memory Connection |
| `POST /connections/C/target` | `target start C [--execution-policy DIGEST] [--wait]` | TaskRef; target row `lab-owner-target/2` |
| `GET`/`DELETE /targets/T?owner_generation=G` | `target status`/`target stop` | TargetStatus/TaskRef |
| `POST /operations`, `GET /operations/O` | `start`, `status` | OperationStatus (`lab-operation-status/8`) |
| `GET /operations[?project=P&implementation=I&view=summary]` | `list [--summary]` | `lab-operation-list/8`; with `view=summary`, `lab-operation-summary-list/1` |
| `POST /operations/O/resume` | `resume` | OperationStatus |
| `GET /operations/O/events`, `/report` | `events`, `report` | SSE `lab-operation-event/3` / `lab-owner-report/15` |
| `GET /operations/O/report.md` | `report --markdown [--output FILE]` | `text/markdown; charset=utf-8` (`report.md`) |
| `GET /operations/O/diff[?from=R&to=R]` | `diff [--from R --to R]` | `lab-operation-diff/2` |
| `POST /execution-policies`, `GET /execution-policies[/DIGEST]` | `policy confirm`, `policy list`, `policy show` | `lab-execution-policy-record/1` or `{policies: [records]}` |
| `GET /operations/O/routing` | `operation routing O [--json]` | `lab-operation-routing/1` |
| `GET /operations/O/suspensions` | `suspensions O [--json]` | `lab-suspension-evidence/1` |
| `GET /operations/O/campaign` | `campaign` | `lab-campaign-view/15` (`/14` and earlier are not served) |
| `GET /operations/O/measurements[/M]` | `measurements [M]` | `lab-measurement-list/4` / `lab-measurement/4` |
| `GET /operations/O/measurements/M/cases/CASE/samples/SAMPLE[?disclose=private]` | `sample O M CASE SAMPLE [--disclose-private]` | `lab-sample/1` |
| `GET /operations/O/attempts/ATTEMPT[?disclose=private]` | `attempt O ATTEMPT [--disclose-private]` | `lab-attempt/1` |
| `GET /operations/O/reflections/REFLECTION[?disclose=private]` | `reflection O REFLECTION [--disclose-private]` | `lab-reflection/1` |
| `GET /targets[?bundle=B&state=ready]` | `target list [--bundle B] [--ready]` (`ready` is the live state: a running target's in-memory status wins over its stored row) | `lab-owner-target-list/1` |
| `POST /operations/O/export`, `GET .../exports/H/content` | `export`, `export-content` | TaskRef/verified ZIP bytes |
| `DELETE /operations/O/results` | `delete-results [--wait]` | TaskRef |
| `GET /tasks/K?owner_generation=G`, `POST /tasks/K/stop` | `task status`, `task stop` | TaskStatus/StopReceipt |
| `POST /cleanup`, `POST /releases`, `GET /health` | `cleanup`, `release`, `health` | TaskRef/ReleaseReceipt/Health |
| `GET /guides/requirements` | `guides requirements --json` | The requirements catalogue (`lab-requirements/1`), the same object the CLI prints; no project session, no Lab home |
| `/projects…` (15 routes) | `project …` | Project records, links, facts, confirmations, acceptances — see [Projects](projects.md#routes) |

`GET /operations/O/report.md` answers the report as Markdown with `Content-Disposition: attachment;
filename="report-O.md"`. Refusals are the usual JSON envelope: `results_deleted` (409) when the results are deleted,
and `report_unavailable` (409, stage `transport`) otherwise, its message naming Owner's reason (`… Owner reason:
io_error.` or `unavailable`). The JSON route remains `GET /operations/O/report`; its current envelope is
`lab-owner-report/15`, including the dev60 Core-terminal projection. Adding the
Markdown route did not change the JSON report contract at that time.

`GET /operations` also accepts `?project=P[&implementation=I]`: the current
pair's operations started through that project's routes, by Owner's
`client_ref` label. An unknown project gives an empty list, and
`implementation` without `project` is `invalid_request`; the history across
pairs is `GET /projects/P/operations`.

The other explicit task-producing calls are `check` and `conformance`; they are
never prerequisites for evaluation start. Every route validates identifiers,
digests, refs, schemas, body size, and media type before it dispatches a public
call. Unknown connections/resources return `not_found`; after acceptance, a
failure belongs to the returned task/status instead of becoming a transport
success claim.

For example, a portable evaluation bundle file has this shape, with every path
absolute on the service host and every collection present:

```json
{
  "source": {"kind": "evaluation", "input_path": "/private/input.json", "launch_path": "/private/launch.json"},
  "environments": [{"executable": "/private/venv/bin/python", "wheel_paths": ["/private/evolution-lab.whl", "/private/evo-skills.whl"], "runtime_files": []}],
  "env": {}, "secret_names": [],
  "roots": [{"alias": "project", "source_path": "/private/source"}],
  "path_bindings": []
}
```

The corresponding evaluation prepare file names the just-created bundle and its
complete one-case schedule:

```json
{
  "kind": "evaluation", "bundle_digest": "BUNDLE_DIGEST",
  "selection": {"case_ids": ["case-1"]}, "repeat_seeds": [17],
  "evaluation_set_id": "fixed-set", "max_turns": 2,
  "timeout_seconds": 30, "metric_plan": {"primary": "score"},
  "runtime_policy": null, "max_failed_turns": null, "call_retries": null,
  "split": null,
  "sample_retries": 2, "min_coverage": 0.9,
  "concurrency": 1, "scorer_concurrency": 1,
  "max_consecutive_unmeasured": null
}
```

`lab-service-prepare-request/5` extends evaluation `/4` with nullable
`max_consecutive_unmeasured` (positive integer N or `"off"`; null resolves to 3
unless the evaluation input supplies another value). `/4` adds nullable
`sample_retries`, `min_coverage`, `concurrency`, `scorer_concurrency` to `/3`;
`/3` adds `split` to `/2`. Every key of the selected closed raw form is required.
`/2`, `/3` and `/4` remain accepted with their own key sets. Campaign requests
reject evaluation-only keys. The new-preparation omission defaults apply to all
accepted request majors; historical saved plans retain their semantics.

CLI request files omit `schema`. `lab prepare` selects `/5` when
`max_consecutive_unmeasured` is named, even null; only `/4` settings select `/4`,
only `split` selects `/3`, otherwise `/2`. It fills remaining nullable keys for
the selected form. Project wrappers retain `/1` and `/2`; use generic preparation
for new settings.

The example above names `/4` settings; adding `max_consecutive_unmeasured` selects
`/5`. `sample_retries` is integer 0–8, `min_coverage` finite >0 and ≤1, concurrency
values positive integers (booleans refused); sample concurrency must fit
`limits.max_concurrent_sessions`. Null uses input defaults or the current `retry`
fallback: 3 sample retries, coverage 0.95, 3 consecutive unmeasured samples,
`max_failed_turns = max_turns`, call retries `{max_attempts: 3, turn_max_attempts: 2}`,
and both concurrency values 1. The campaign Bundle path still requires its
explicit schedule, not explicit failure settings. See
[Evaluation budget](../../../docs/guide/evaluation-inputs.md#the-fixed-evaluation-budget).

<a id="bundle-measurements"></a>
## Measurements for a Bundle

`GET /v1/bundles/{digest}` and `lab bundle show DIGEST` return
`lab-bundle-summary/3` (`/2` plus nullable `measurement_basis`). The basis is a
SHA-256 of the saved fixed-evaluation inputs, not of the entire campaign manifest.
A null basis is unknown/unavailable, never a match. Unknown Bundle digests have
the same refusal as Bundle show.

`GET /v1/bundles/{digest}/measurements`, `lab bundle measurements DIGEST` and
`await owner.bundle_measurements(digest)` return the closed
`lab-bundle-measurements/1` form. The CLI service client uses
`ServiceClient.request_json("GET", f"/v1/bundles/{digest}/measurements",
expected_schema="lab-bundle-measurements/1")`. The top-level fields are exactly
`schema`, `bundle_digest`, nullable `measurement_basis` and `items`. Only fixed
evaluations from this Owner state with the same basis are included; a null
requested basis gives an empty list. The read creates no Bundle, captures no
environment and does not start work.

Each `items[]` object has `operation_id`, `prepared_digest`, the original
`bundle_digest`, `same_bundle`, `control_state`, `results_state`, nullable
`failure`, `times`, nullable `outcome` and nested `plan`. `same_bundle: false`
means the original evaluation used another Bundle with the same basis. Unfinished
and incomplete evaluations remain visible: these are input-equivalent candidates,
not decisions that the measurement is eligible as a baseline.

| Nested object | Fields |
|---|---|
| `plan` | `evaluation_set_id`, `split`, `case_count`, `case_ids_digest`, `repeat_seeds`, `max_turns`, `timeout_seconds`, `metric_plan_digest`, `concurrency`, `scorer_concurrency`, `sample_retries`, `min_coverage`, `max_consecutive_unmeasured`, `call_retries`, `max_failed_turns` |
| `outcome` | `complete`, `coverage`, `stopped_reason`, `unmeasured`, `min_coverage` |

`split` is train/test for campaign inputs and null for evaluation-only inputs.
Case and metric digests use canonical saved plan data; ordered `case_ids` define
the case digest. Old plans can have null `sample_retries`; that describes the saved
plan, not the current retry default. Repeat seeds may contain null according to
the target's seed mode. `max_consecutive_unmeasured` shows effective 3 even when
canonical storage omits it; `call_retries` is the saved object or null when call retries are disabled.
`outcome` is null if no result is available; `outcome.complete` and `coverage` may
also be null. Status/failure fields retain the public operation meanings;
unavailable/unknown results do not prove success. `times` carries public event
times, which can be missing. Terminal candidates precede active candidates;
within each group newer `last_event_at` (terminal) or `first_event_at` (active)
comes first, missing times last, with deterministic operation-id ties. These
are event-time proxies, not a separate persisted finish timestamp.

Choose candidates by the saved plan conditions and outcome. Different failure
settings do not themselves prevent a basis match; they describe how the result
was obtained. For first-measurement readiness use shared `baseline.measured` and
`connected` facts (`lab-project-facts/5`), which apply Lab's latest-terminal-failure
policy as well as the freshness rules below.

A successful baseline remains current when its effective `inputs_digest` already matches the current
inputs, without requiring a readable original Bundle, a current Bundle or a source receipt. An exact,
unbroken non-revoked acceptance chain is also sufficient. Only when neither route holds can the original
evaluation Bundle's non-null measurement basis rescue the stale fact. That basis-reuse route requires
valid current materials and matching request metadata, descriptor and source receipt in this pair/state;
reading facts never creates a Bundle or captures an environment.

Compatibility limitation: request `env`, `secret_names`, paths or `path_bindings` outside `inputs_digest`
do not by themselves invalidate an unchanged-input fact. Descriptor/receipt proof protects basis reuse
after material changes, not that legacy unchanged-input route. A missing proof disables only basis reuse.
`connected` still requires all three evidence facts to hold, check and conformance to share an effective
inputs digest, and baseline to match that digest or the original basis of the checked Bundle.

Fresh checks and conformance remain required on changed materials. The basis uses
the original evaluation Bundle, not an acceptance's destination: scorer A →
acceptance B → campaign-only edit C still compares original basis A with C and can
need a new acceptance. Revocation breaks the acceptance chain; basis reuse still
requires matching pair/place and the current valid package. Scope of basis reuse
is this pair/state only. Old runs retain their producing wheels/state, and no quality
claim or campaign-baseline reuse follows from discovery.

## Explicit evaluation lifecycle

The following is a command map, not a recipe that runs on its own. Fill the
private files with declared, agent-neutral inputs before issuing each command.
No command below calls check or conformance unless explicitly named.

1. Create a bundle request with exactly the documented keys: evaluation `source`, explicit
   `environments`, `env`, `secret_names`, `roots`, and `path_bindings`; then
   prepare exactly the selected evaluation.

   ```bash
   lab --socket /private/lab.sock --json bundle create --request /private/bundle.json
   lab --socket /private/lab.sock --json prepare --request /private/prepare.json
   ```

   The request files contain no `schema` field; the CLI adds the closed request
   schema. A Bundle or Prepared summary is immutable input/provenance, not a
   target launch, scorer execution, conformance result, or readiness claim.

2. Bind the saved Bundle digest. A `Connection` is in-memory for this Owner
   generation, so bind it again after a service restart.

   ```bash
   lab --socket /private/lab.sock --json connect BUNDLE_DIGEST
   ```

3. Start the declared target and explicitly wait only if the caller chooses to.
   The final task status contains the target resource ref. Keep its `id` and
   `owner_generation` together; do not substitute the current health generation.

   ```bash
   lab --socket /private/lab.sock --json target start CONNECTION --wait
   lab --socket /private/lab.sock --json target status TARGET --generation GENERATION
   ```

   With `--wait`, stdout carries two JSON lines: TaskRef, then the final TaskStatus; read the last line
   (on exit 124, a local wait timeout, only the task reference was printed).

4. Start the prepared evaluation with that target ref, then read its public
   status, one sampled event observation, and its report.

   ```bash
   lab --socket /private/lab.sock --json start PREPARED_DIGEST \
     --target TARGET --generation GENERATION
   lab --socket /private/lab.sock --json status OPERATION
   lab --socket /private/lab.sock --json events OPERATION
   lab --socket /private/lab.sock --json report OPERATION
   ```

   SSE starts with the current summary, sends only changed summaries, and has no
   replay or reconnect cursor. A fast completed evaluation may therefore yield
   an already-terminal first event. `status`, a process exit, partial coverage,
   or a report availability value does not prove quality, completeness, cost, or
   cleanup.

5. Stop the target explicitly. Once the completed evaluation is released, delete
   only that operation's owned results. Deletion is an accepted task; use the
   returned TaskRef to check its terminal status before claiming deletion.

   ```bash
   lab --socket /private/lab.sock --json target stop TARGET --generation GENERATION --wait
   lab --socket /private/lab.sock --json delete-results OPERATION --wait
   ```

For a completed campaign, `diff` is a direct read and `export` is an explicit
task. `no_change` is a task result with no ZIP; it does not mean a missing result
became an improvement. `export-content` verifies an explicitly named ZIP hash
and publishes to a new caller-selected absolute path without clobbering it.
Fixed evaluations do not have campaign diff/export output.

```bash
lab --socket /private/lab.sock --json diff OPERATION
lab --socket /private/lab.sock --json export OPERATION --wait
lab --socket /private/lab.sock --json export-content OPERATION ZIP_SHA256 \
  --output /private/export.zip
```

<a id="resume"></a>
## Resume

`POST /v1/operations/{id}/resume` — CLI `lab resume OPERATION --target TARGET
--generation GENERATION [secrets as for start]` — continues a **campaign**
operation whose core journal is suspended, on any `ready` target of the same
Bundle (not necessarily the one the earlier start used, and not necessarily
in the generation that ran it). It is the same mutation class ("work") and
mutation-key rule as `start`, over the closed body
`lab-service-resume-request/1`:

```json
{"schema": "lab-service-resume-request/1",
 "target_ref": {"schema": "lab-target-ref/1", "id": "TARGET", "owner_generation": "GENERATION"},
 "secrets": {}}
```

The response is `lab-operation-status/8`, the same schema `status` and
`start` return. Refusal codes, checked in order before any new scope,
reservation or helper is created — the full admission rule, with the "why",
is in the [Owner API reference](owner-s4.md#resume):

| Code | When |
| --- | --- |
| `not_found` | No such operation. |
| `invalid_argument` | The operation is not a campaign. |
| `results_deleted` | Results are deleted, deleting, or deletion failed. |
| `resource_busy` (stage `resume`) | The operation is `reserved`/`running`/`stopping`. |
| `cleanup_required` | The operation is `cleanup_required`/`orphaned` — run `cleanup` first. |
| `cleanup_unconfirmed` | `released` after a manual `release` (target state unknown). |
| `campaign_unreadable` | Journal evidence is missing or unreadable; the stored first-refusal exception is described below. |
| `campaign_not_suspended` | Core says the campaign is complete or not suspended. |
| `target_unstable_fresh_required` (409, stage `before_start`) | The last suspension is `target_unstable` and the target is one the campaign has already used (any start's, see [`resume.target`](#resume-readiness)). Stop it, start a new target of the same Bundle, resume on that. |
| `target_not_running` / `binding_mismatch` / `busy` / … | Same target/slot checks as `start`. |

The target requirement is checked twice: from the verdict before the target
checks, and again inside the durable `accept_resume` transaction, against the
starts recorded at that moment, so a start accepted since the verdict was read
counts as a used target too.

An identity mismatch or a related-run conflict that only core can detect
(for example `run_identity_changed`) is not one of these: it is dispatched to
the worker and comes back as a worker refusal, exactly as it would for a
`start`, not as one of the refusals above.

<a id="resume-readiness"></a>
**`resume`: is the operation itself ready to continue?** Since
`lab-operation-status/3` (Lab 0.0.3.dev28) every status carries `resume`;
since `/4` (Lab 0.0.3.dev29) it has a third key, `target`,
computed by the same function that admits `resume`, from the operation's
durable record and core's projection:

| `resume` | When | Next step |
| --- | --- | --- |
| `null` | Not a campaign, or core does not show the campaign suspended (never suspended, complete, or no readable projection — for example after results were deleted). | Nothing to continue. |
| `{"state": "ready", "reason": null}` | `finished`, or `released` with target cleanup `confirmed`/`not_needed`. | `resume`. |
| `{"state": "cleanup_first", "reason": "cleanup_required"}` | `cleanup_required`/`orphaned`. | `cleanup`, then `resume`. |
| `{"state": "cleanup_first", "reason": "cleanup_unconfirmed"}` | `released`, but the cleanup fact was lost (`pending`/`unconfirmed`, a crash between two cleanup writes). | `cleanup` (it repairs the fact when the scope's cleanup is confirmed), then `resume`. |
| `{"state": "busy", "reason": "resource_busy"}` | `reserved`/`running`/`stopping`. | Wait. |
| `{"state": "refused", "reason": …}` | `cleanup_unconfirmed` after a manual `release`; `results_deleted`; `campaign_unreadable`. | None: the run directory is kept until results are deleted. |

**`resume.target`** is `null` unless core's last suspension is `target_unstable`
and the state is `ready`, `cleanup_first` or `busy`; then it is a target
requirement, and the verdict shows it under every one of those states:

```json
{"state": "ready", "reason": null,
 "target": {"requirement": "fresh_instance",
            "reason": "target_unstable_fresh_required",
            "excluded": [{"schema": "lab-target-ref/1", "id": "TARGET", "owner_generation": "GENERATION"}]}}
```

`excluded` is the target of every start of the campaign, first seen first
(the status's `starts[].target_ref`; first occurrences only, so starts A, B, A give
`[A, B]`). The last element is therefore not necessarily the current target: the
operation's current `target_ref` (top-level, the latest start's target) is the one to
stop (`lab agent stop … --wait`: `target start` reuses a still-ready target, so
wait for the stop before starting the fresh one). `fresh_instance` means: any `ready`
target of the same Bundle except those. `reason` is the code `resume` refuses
an excluded target with. Lab still does not compare target *identity* for
core: it only excludes the instances the campaign has used.

`reason` is the refusal code `resume` would return on the same facts. `ready`
is the operation's own readiness, **not** admission at call time: `resume`
can still refuse because another operation holds a slot (`busy`, or
`cleanup_required` for an uncleaned scope), a result read or export is in
progress (`resource_busy`), the target is not `ready` (`target_not_running`),
or the Bundle or secrets do not match (`binding_mismatch`).

`lab-operation-status/2` changed the shape for every path that used to
return `/1` — `start`, `status`, `resume` themselves, `list`, and events —
separating the campaign from its starts; `/3` (`list`: `lab-operation-list/3`)
keeps that shape and adds `resume` ([above](#resume-readiness)); `/4`
(`lab-operation-list/4`) changes only the shape of `resume` (the `target`
key); `/5` adds the top-level `entrypoint`, `progress` and `times`
([below](#progress-times-and-summaries)); `/6` (`lab-operation-list/6`,
`lab-project-start-result/4`) adds `refusal`, the core's refusal, at the top
level and in every `starts[]` entry ([below](#core-refusal)); `/7`
(`lab-operation-list/7`) adds start-owned `core_terminal` and the
`lab-operation-result/2` evaluation fields. Current `/8` adds `auto_resume`
and start-owned `initiated_by` and `auto_resume_attempt`:

- Campaign-level: `id`, `prepared_digest`, `kind`, `control_state`,
  `results_state`, `result` (core's cumulative projection over the whole
  journal, all starts — not just the latest one).
- `starts`: an ordered list, one entry per start, `{start, scope_id,
  owner_generation, target_ref, process, target_cleanup, failure,
  settlement, refusal, core_terminal, initiated_by, auto_resume_attempt}`; `settlement` is `"positive"`, `"not_positive"`, or
  `"unknown"` (no settlement ran for that start yet — it is still running,
  or it failed before dispatch). An evaluation operation's `starts` always
  has exactly one entry.
- The top-level `owner_generation`, `target_ref`, `process`,
  `target_cleanup` and `failure` stay, as a **copy of the latest start** —
  so a client written against `/1` keeps reading them the same way; the
  schema does not extend that copy to `result`, which is always the
  campaign's.

<a id="core-refusal"></a>
**Core refusal (`lab-operation-status/8`).** When the core refuses the
campaign (`EvolveRefused`), the status carries `failure: {"code":
"core_refused", "stage": "running", "message": "The core refused the
campaign."}` and `refusal: {code, path, class, instance_digest}`: the core's
first finding, `class` being `hard` or `authorizable`. `refusal` is a copy of
the latest start at the top level and sits in every `starts[]` entry; it is
`null` when the start was not refused, and an accepted `resume` opens a new
start with `refusal: null` (the refusal of the earlier start stays in its
`starts[]` entry). After a refusal the operation goes through the normal
cleanup (`cleanup_required`, `target_cleanup: unconfirmed`, then `cleanup`);
cleanup keeps `failure` and `refusal`. The class does not say whether the
campaign can continue: the `resume` field does. `lab-operation-event/3` is
unchanged. `lab status` prints `refused: <code>[ at <path>] (<class>)` for the
latest start.

`lab-owner-report/2` (`GET /operations/O/report`; `/3` since dev30, see [below](#progress-times-and-summaries); `/4`
since dev49; `/5` since dev55 added `lab_independent` with `lab_independent_state`/`lab_independent_reason`;
`/6` added both comparison levels and the nullable reference measurement id;
`/8` added the top-level latest `core_terminal` and start terminal facts;
`/9` added the family-15 checks triplet; `/10` added the independent top-level probe triplet; current `/12` adds focus-first attempt stages) adds the
same `starts` list next to its existing `report` (from `/4` each start carries `refusal`, as in
the status); for an Owner campaign operation the report dict now
also carries `owner_run: true` (which switches the Markdown's continuation
sentence to `lab resume`) and, when the status's `resume.target` is set,
`owner_resume_target` (the same requirement object; the sentence then names a
fresh target), while H1 reports and the `legacy_run` key are
unchanged. `lab-operation-event/2` (`/3` since dev30; `GET
/operations/O/events`, SSE) adds `start` inside each event's `payload`, the
number of the latest start (`1` before the operation has recorded any); the
stream still ends at a terminal `control_state`, so a client subscribes again
after a successful `resume`.

**Current evaluation result and Core terminal.** Status/list `/7` return
`result.stopped_reason`, `result.unmeasured` and `result.min_coverage`; report `/8` wraps the
Core projection under `report.core`, alongside `report.lab_result` and
`report.measurements`. Read [the exact Owner forms](owner-s4.md#current-evaluation-and-terminal-fields)
for closed keys, acceptance and legacy readers. `core_terminal` is null or
`{owner, terminal, code, hint}` (`hint`: null or `{ru, en}`). It belongs to each
start; top-level status/report copies the last start. A new accepted resume,
success, suspension or refusal has null terminal. Earlier start terminals stay
in `starts[]`; `failure`, `refusal` and cleanup keep their existing semantics.

<a id="progress-times-and-summaries"></a>
## Progress, times and summaries (dev30)

Everything here is read from public projections of the core; Lab stores no campaign state of
its own. Every section that could not be read is `null` with its state next to it, never an error
for the whole answer, and `not_started` (nothing to read yet) is always distinct from
`unavailable` (a read failed).

**`lab-operation-status/5`** (and the elements of `lab-operation-list/5`) adds six top-level
fields next to `result`, plus `entrypoint`:

- `entrypoint`: the prepared manifest's name (for example `inputs/project/lab/manifest.json`), so
  a run is recognisable in a list; `null` when unknown.
- `progress` / `progress_state`: `progress_state` is `not_started` (the campaign journal does not
  exist yet — `progress: null`), `present`, or `unavailable` (the journal exists but the read
  failed — `progress: null`).
- `times` / `times_state`: `{first_event_at, last_event_at}` from the journals of the campaign
  and its measurements, with the same three states.
- `times_source` / `times_reason` (ADR-0019), next to `times`:
  - `times_source` names where present times come from: `"core_raw"` for a campaign (its
    campaign journal is read only through Lab's pinned core adapter), `"core_public"` for an
    evaluation (the public measurement journal only); `null` exactly when `times` is `null`.
  - `times_reason` is `null` unless `times_state` is `unavailable`, and then always set:
    `"core_raw_unsupported"` (the installed core is not the one the adapter was written for, so it
    refused rather than guess — campaigns only) or `"unreadable"` (any other failed read,
    including a removed or deleting result and a read during a result write).
- `results_state` of a **launched** operation (`process.launched: true`) whose run directory is
  missing is `unavailable`, not `not_created`: Lab creates the run directory before it launches
  the child, so after launch its absence is a failed read. `progress_state` and `times_state` then
  follow as `unavailable` (`times_reason: "unreadable"`). Before launch a missing run stays
  `not_created` / `not_started`. For an evaluation the worker writes its measurement journal after
  launch and may stop before it (cancelled), so a missing journal under an existing run directory
  stays `not_created`.
- These are top-level: current `result` is `lab-operation-result/3`, and none of these
  forms carries a project link (no `client_ref`, `project`, `implementation` or `label`).

The `progress` object is **closed**. A campaign:

```json
{"kind": "campaign", "phase": "reflection", "stages": ["reflection"],
 "rounds": {"completed": 0, "current": 1, "planned": 2},
 "attempts": {"current_round": {"closed": 0, "running": 0, "planned": 1}, "total": 0},
 "coverage": null, "policy_state": "present", "observed_at": "2026-09-29T14:53:37.274070Z"}
```

An evaluation is `{"kind": "evaluation", "coverage": {"expected": 1, "scored": 1}, "observed_at": "…"}`.

- `phase` is Lab's derivation, not a claim of the core: `suspended` when the core is suspended
  (there is no separate Owner "suspended" state, ADR-0015), `finished` when it is complete;
  otherwise the most advanced stage among the actions the core lists (`stages` is all stages with
  actions, in round order: `baseline`, `reflection`, `generation`, `evaluation`, `selection`,
  `finishing`). No actions is `waiting`; an action the table does not know (a newer core) is
  `unknown`. The closed phase list is `baseline`, `reflection`, `generation`, `evaluation`,
  `selection`, `finishing`, `suspended`, `finished`, `waiting`, `unknown`. A finished campaign has
  `stages: []`.
- `rounds`/`attempts` are for the whole campaign, not for one start (ADR-0015). `planned` values
  come from the campaign policy in the prepared manifest, read once per prepared execution; if
  that read fails both are `null` and `policy_state` is `unavailable`.
- `coverage` sums the still-open measurements (`{expected, scored}`), `null` when none is open.
- **Extension rule:** a new key or a new phase value is a new schema version of the status and
  event, never a silent addition; an unfamiliar core action shows as `unknown`, which is itself in
  the closed list.
- **Why `result` was empty at the start of a campaign:** until the core creates its journal there
  is nothing to project, so `results_state` is `unavailable`, `result` carries no core data and
  `progress_state` is `not_started`. This is the expected window before the journal, not a fault.

**`lab-operation-event/3`** (`GET /operations/O/events`) carries `progress`, `progress_state`,
`times`, `times_state`, `times_source` and `times_reason` in `payload`, in the same forms as the status, in place of the earlier
`progress.rounds_seen`/`attempts_seen` (removed); a live client needs no extra status call. The
stream still ends at a terminal `control_state`.

**Summaries.** `GET /operations?view=summary` answers `lab-operation-summary-list/1`, a list of
`lab-operation-summary/1` rows `{id, kind, entrypoint, control_state, results_state, resume,
progress, progress_state, times, times_state, times_source, times_reason, outcome}` without
`result.core`; `outcome` is the
short campaign outcome `{outcome, reason, champion}` (`null` for an evaluation or an unfinished
campaign). The `project` and `implementation` filters work with it. Settled operations are answered
from an in-memory cache of a fully read row; a degraded row is read again next time. CLI: `lab list
--summary`. The project-level variant is `GET /projects/P/operations?view=summary`
(`lab-project-operation-list/2`, [Projects API](projects.md#routes)); a plain
`/projects/P/operations` stays `/1`.

**Other reads added in dev30** (shapes in the [Owner API](owner-s4.md#reading-a-campaign-dev30)):
`GET /operations/O/campaign` (added as `/1`; dev33: `/2`, dev36: `/3`, see the [Owner API](owner-s4.md#campaign-details)),
`GET /operations/O/measurements[/M]`
(`lab-measurement-list/4`, `lab-measurement/4` on dev72; notice codes, counters and
`lab_notices_state` are described in the [Owner reference](owner-s4.md#measurements)), `GET /operations/O/diff?from=&to=`
(`lab-operation-diff/2`), `GET /targets` (`lab-owner-target-list/1`), and the extra `usage` section of
`lab-owner-report/15`. Campaign `/15` and report `/15` carry the same closed family-15
comparison shape: nullable `comparison`, `gate_comparison` and `global_comparison` in each attempt,
with nullable string `reference_measurement_id` in each comparison. Core's `comparison` is global when present,
otherwise gate; Lab copies each independently. See the [Owner comparison contract](owner-s4.md#campaign-view)
for all closed keys, including the independent `checks`/`checks_state`/`checks_reason`
triplet in view `/14` and `lab_checks`/`lab_checks_state`/`lab_checks_reason` in
the nested report. Modes remain visible when binding publications make independent
acceptance unavailable. Family 14 has no checks keys. The routes retain their paths; current status is
`lab-operation-status/8`, including the dev60 evaluation and Core-terminal fields.
Earlier campaign-view and Owner-report majors are not produced or decoded. All are GET routes; each has a `lab …` command in the table above.

<a id="content-reads"></a>
**Content reads (panel 2b).** The three GET routes above take no body or mutation key. With no
query flag they return public content; only `disclose=private` requests private sections.
Other values (including `disclose=public`) are `invalid_request` with `field: disclose`.
Owner's equivalent is the boolean keyword `disclose=True`; CLI uses `--disclose-private`.
The [Owner reference](owner-s4.md#sample-attempt-and-reflection-content) describes all three forms,
partial availability, masking and the hard 1 MiB limit with exact section fallback variants.

`case_id` and `sample_id` use core's Unicode grammar `[\w][\w.:@+\-]{0,255}`. Encode each as
one UTF-8 percent-encoded path segment, once (for example `кейс+1@a`); CLI and panel do this for you.
ASGI decodes once and the codec then validates; double encoding leaves `%` and is refused, as are
slashes (`%2F`), empty ids and `.`/`..`. Invalid ids give `invalid_request` naming `case_id` or
`sample_id`. Attempt ids are `r<NN>-a<MM>`, reflection ids `r<NN>-x<MM>` (at least two digits,
allowed to grow); refusals name `attempt_id` or `reflection_id`. Measurement ids use the existing
epoch rule below. No identifiers are inferred from display labels.

**Measurement id with an epoch (dev32, core a21).** In `GET /operations/O/measurements/M` the id `M`
is a base id of the usual form (`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`) with an optional epoch suffix
`@e<k>`: `k` is a positive integer without leading zeros (`@e0`, `@e01` and a second `@e…` are
`invalid_request`). Core (a18 and later) adds the epoch to measurements of a campaign whose
comparable condition was changed by an authorization (`m-r01-champion@e1`); the suffix is not accepted in any other identifier, including the
operation id. The route decodes `%40` to `@` before the service codec sees it (`decode_measurement_id`),
a literal `%` inside the value is refused, and `lab measurements OPERATION m-r01-champion@e1` sends the
`@` as `%40`. `lab panel`'s GET allow-list admits the suffix (`@e<k>` or `%40e<k>`) only in this
segment. The measurement forms above are unchanged by the epoch suffix.

**Errors.** `invalid_request` now says what is wrong in `message`, for example `The service request
is invalid. missing: stop_grace_seconds, cleanup_wait_seconds; unexpected: 1 field(s)`: missing
fields are named, unexpected ones are only counted (the service never echoes request data). The
error form stays `{code, stage, message}`. New code `tree_unavailable` (HTTP 404, stage `diff`): a
candidate attempt has no saved tree (for example a cancelled one). An unrecognised diff ref is
`invalid_argument` (400).

**`lab serve` readiness (W56).** Once the socket is bound the service prints `lab serve: listening
on <socket>` on stdout; stderr stays JSON, as before. The demo project's display name is `Demo
(<id>)`.

<a id="fixed-evaluation-budget"></a>
## Fixed-evaluation budget

`lab-service-prepare-request/5` requires keys `max_failed_turns` (positive
integer or null) and `call_retries` (closed `{max_attempts, turn_max_attempts}`
with integers 1–10, `"off"` or null), as do older accepted request forms.
Null means not given; new preparations use input defaults or `max_turns` and
`{max_attempts: 3, turn_max_attempts: 2}` respectively. This also applies to
campaign Bundles: failure settings are never inherited from the manifest.
Explicit values override defaults. Stored old plans keep their semantics.
The applied budget remains visible in the prepared summary and
`lab-evaluation-outcome/2`. See
[Evaluation budget](../../../docs/guide/evaluation-inputs.md#the-fixed-evaluation-budget).

## Refs, acceptance, and shutdown

`TargetRef` and `TaskRef` are public objects with `schema`, `id`, and
`owner_generation`; target/task reads require that producing generation. Cleanup
and manual release use a closed `{kind, id}` resource ref. A connection is not a
durable ref. Unknown or old-generation resources are facts to handle explicitly,
not prompts for guessed replacement identifiers.

A mutation response can be lost after the service accepted work. In that case
the CLI returns `acceptance_unknown` with exit `125` and performs no retry,
discovery, stop, check, or report call. Since S7 the output names the mutation
key it used, so a manual retry of the same command is safe; see
[Mutation keys](#mutation-keys-s7). Once a TaskRef is printed, `--wait` may
return `124` (local wait timeout) or `130` (interrupted local wait); either keeps
the accepted work with Owner. Inspect it later with `task status` using its saved
generation.

For orderly shutdown, an external operator signals the **service PID only** with
`SIGTERM` or `SIGINT`. The runtime closes admission, waits for admitted Owner
calls, requests explicit Owner stops, waits for preparation IO, closes Owner, and
then removes only its own socket inode. Terminal `Ctrl-C` targets the foreground
process group, so target/helper/worker may stop concurrently; it has weaker
ordering and cleanup guarantees. It does not change S2--S4 spawn semantics or
turn unknown cleanup into success.

## Service environment (S7)

A Bundle request normally names its execution environment explicitly: the
interpreter, the exact Lab/core wheel archives, and runtime files. S7 lets the
service own that declaration so that a client does not need the service host's
paths.

### Configuring `lab serve`

```bash
lab serve --socket /private/lab.sock --state /private/lab-state \
  --execution-wheel /private/evolution-lab.whl \
  --execution-wheel /private/evo-skills.whl \
  [--execution-runtime-file /private/runtime-file]...
```

- `--execution-wheel PATH` is repeatable and names an exact wheel archive by
  absolute path; `--execution-runtime-file PATH` is repeatable and absolute.
  Runtime files without at least one wheel are a usage error.
- There is no interpreter flag. The execution environment is, by definition,
  the interpreter running Lab, so the service always uses its own
  `sys.executable`.
- With at least one wheel, the service captures the environment at startup,
  **before** it creates the state directory, opens the Owner, or binds the
  socket. A refused capture exits `lab serve` with `environment_invalid` or
  `environment_unavailable` on stderr and leaves no socket or state behind.
- On success the service keeps the environment input and the digest of the
  captured `EnvironmentDescriptor` in memory for its lifetime ("the service
  environment"). Without these flags the service starts exactly as in S5 and has
  no service environment.

### Bundle request `lab-service-bundle-request/2`

Each `environments[]` item is either the explicit S5 form
`{executable, wheel_paths, runtime_files}` or exactly `{"kind": "service"}`:

```json
{
  "source": {"kind": "evaluation", "input_path": "/private/input.json", "launch_path": "/private/launch.json"},
  "environments": [{"kind": "service"}],
  "env": {}, "secret_names": [],
  "roots": [{"alias": "project", "source_path": "/private/source"}],
  "path_bindings": []
}
```

- The marker is allowed only as the **first** item and at most once: preparation
  requires the first environment to be the execution environment, and only one
  environment may use the service interpreter. A marker in any other position,
  or with extra keys, is `invalid_request`. Explicit items after a leading marker
  that name the service interpreter are refused by preparation, as in S5.
- `lab-service-bundle-request/1` is still accepted, with the explicit form only.
  The new schema id makes an S5/S6 service answer a `/2` client with
  `unsupported_schema` rather than a generic `invalid_request`. CLI
  `bundle create` sends `/2`; its request file still omits `schema`.
- Without a configured service environment, a marker is refused with
  `service_environment_unconfigured` (409) and preparation is not called.
- Otherwise the route substitutes the stored environment input and calls
  `PreparationStore.create_bundle(..., expected_execution_environment=DIGEST)`
  with the startup digest. `create_bundle` compares it with the descriptor of its
  own fresh capture, **before** publication: a mismatch (for example, a wheel
  archive changed after the service started) is `environment_invalid` and no
  Bundle is published. There is no window between comparison and publication.

A Bundle does not record that its environment came from the marker; it stores
the same descriptor as the explicit form with the same paths. Therefore, while
every file the request refers to (source roots, manifest or input, launch,
wheels, runtime files) is byte-for-byte unchanged, repeating a marker request on
the same service yields the same `bundle_digest`, and so does the explicit form
with the service's paths. A service running from another venv has another
descriptor and therefore other digests.

The keyword-only `expected_execution_environment` (default `None`, the S5
behaviour) is the only S7 change to the Python API. The marker itself is
service-only.

### Health `lab-service-health/2`

`GET /v1/health` (CLI `health`) returns `lab-service-health/2`: every field of
`lab-owner-health/1` with unchanged meaning, plus `service_environment`. It is
`null` without a configured service environment, and otherwise a
`lab-service-environment/2` projection:

```json
{
  "schema": "lab-service-environment/2",
  "digest": "ENVIRONMENT_DIGEST",
  "executable": "/abs/path/to/venv/bin/python",
  "python_version": "3.14.7",
  "executable_sha256": "EXECUTABLE_SHA256",
  "platform": {"system": "Linux", "machine": "x86_64"},
  "distributions": [{"name": "evo-skills", "version": "0.3.0a21", "wheel_sha256": "WHEEL_SHA256"},
                    {"name": "evolution-lab", "version": "0.0.3.dev32", "wheel_sha256": "WHEEL_SHA256"}],
  "runtime_files": [{"sha256": "RUNTIME_FILE_SHA256"}],
  "proof_profile": "wheel-members/1"
}
```

`digest` is the digest of the full `EnvironmentDescriptor`: the same value the
Bundle lists in its environment digests and that the launch file expects in
`environment_ref`. `runtime_files` keeps descriptor order. `executable` is the
service interpreter path exactly as stored in the environment descriptor; wheel
and runtime-file paths are not shown. The Bundle summary does not contain the
descriptor. Python `Owner.health()` is unchanged.

`POST /bundles` and `GET /bundles/B` answer with `lab-bundle-summary/3`: `/2` plus nullable
`measurement_basis` ([reading](#bundle-measurements)). `/2` added the
same fields as `/1` plus a closed `warnings` list (`[{"code": str, "detail":
{...}}, ...]`, empty by default). A warning never refuses the request and never
enters the Bundle digest. The only code today is `lab_version_mismatch`,
emitted only for a `lab-target-launch/2` or `/3` launch (its launcher runs in the
target environment): the service compares the `evolution-lab` pin of
`environments[0]` (the execution environment) against the `evolution-lab` pin
of the target environment (the descriptor whose `digest` equals
`launch.environment_ref`), and reports `{"expected": "...", "found": "..." |
null}` on a mismatch or a missing target pin. A `/1` launch never warns. There
is no `/1` fallback on the wire; only the current `lab-bundle-summary/3` is served.

A client that writes its own launch file and role commands reads `executable`
and `digest` first:

```bash
lab --socket /private/lab.sock --json health
# service_environment.executable -> role argv[0]
# service_environment.digest -> launch.json "environment_ref"
```

For the current `lab-target-launch/2` or `/3` format, declare `agent` and
`environment_ref`; Lab selects the interpreter and launches the target.
Only a custom `lab-target-launch/1` launcher also writes `executable` into
`command.executable`. See the
[launch template](../../../docs/guide/connect-agent.md#the-launch-file).

## Mutation keys (S7)

A mutation key lets a client repeat a mutation whose response it lost without
running the action twice. Keys live in the memory of one Owner generation.

### Headers and routes

Mutating requests may carry both of these headers, or neither:

- `Lab-Request-Id`: `[A-Za-z0-9._:-]{1,128}`, chosen by the client;
- `Lab-Owner-Generation`: the current `owner_generation`, with the same syntax as
  that field.

One header without the other, a malformed value, or a repeated header is
`invalid_request`. Without the headers a request behaves exactly as in S5.
Request bodies and their schemas are unchanged (apart from the Bundle request
above), and `DELETE` still has no body. Key headers on a `GET` are
`invalid_request`.

The key applies to policy confirmation (`POST /execution-policies`) and the
routes in these four classes:

| Class | Routes |
| --- | --- |
| work (8) | `POST /bundles`, `POST /prepared-executions`, `POST /connections/from-bundle`, `POST /connections/C/target`, `POST /connections/C/check`, `POST /connections/C/conformance`, `POST /operations`, `POST /operations/O/export` |
| lifecycle (6) | `DELETE /targets/T`, `POST /tasks/K/stop`, `POST /operations/O/stop`, `DELETE /operations/O/results`, `POST /cleanup`, `POST /releases` |
| project (10) | `POST /projects`, `PATCH /projects/P`, `POST /projects/P/bundles`, `POST /projects/P/prepared-executions`, `POST /projects/P/targets`, `POST /projects/P/operations`, `POST /projects/P/confirmations`, `POST /projects/P/confirmations/C/revoke`, `POST /projects/P/acceptances` (a dry run too), `POST /projects/P/acceptances/A/revoke` |
| probe (4, dev37) | `POST /projects/P/probes`, `POST /probes/S/turns`, `POST /probes/S/reset`, `DELETE /probes/S` |

The two project revoke routes share one route name (`project_revoke`) but not
a fingerprint, so one key cannot serve both.

### Processing order

The service keeps `request_id → (route, fingerprint, task)` for its generation.
The fingerprint is SHA-256 over canonical JSON (sorted keys, no whitespace) of the
route name, decoded path parameters, query parameters, and the decoded body.
Secret values in the body contribute to the hash; neither the hash nor the table
is written to disk or printed.

A keyed request is handled in this order:

1. Header syntax is checked. A `Lab-Owner-Generation` other than the current one
   is `request_unknown_generation` (409), before any request state is read, so
   after a restart an old key receives this refusal on every route, rather than
   `not_found` for a Connection that no longer exists. Nothing is recorded.
2. Body, path and query are fully decoded and validated as in S5, including
   resolving `connections/C` to the in-memory Connection. Any failure, including
   `not_found`, is answered without a record: no action happened.
3. If the key is already recorded: another route or fingerprint is
   `request_conflict` (409) without action; otherwise the response waits for the
   recorded task and returns its result, either the same success object or the
   same post-dispatch error.
4. If the key is new: a closed admission (`service_stopping`) is answered without
   a record; a full class is `request_table_full` (503) without a record;
   otherwise the record and the task are created before the first `await` and the
   call is dispatched as in S5.

Steps 2–4 run synchronously on the service event loop, so two concurrent requests
with one key never create two actions.

### What is remembered

- A remembered post-dispatch error (for example `busy`) stays the key's answer
  for the whole generation. To try again, use a new key.
- Refusals in steps 1 and 2 and admission refusals are not remembered: no action
  happened, so repeating them with the same key is safe.
- If a recorded task ends with `not_found` (missing Bundle, Prepared, target,
  task, or operation), which means no action happened, the record is **removed**
  when the task settles. Waiters on that task receive the same `not_found`; the
  next request with the key runs again. Keys for nonexistent resources therefore
  cannot fill the table.
- Other post-dispatch refusals (`busy`, `cleanup_required`, ...) stay recorded.
  This includes the project failures: after `link_failed` (the Bundle or
  PreparedExecution was made, its project link was not written) repeat the
  request with a **new** key to write the link; the old key keeps answering
  `link_failed`. The CLI makes a fresh key per invocation, so re-running the
  command is enough.

### Limits and memory

Each generation allows 10,000 work records, 10,000 lifecycle records,
10,000 project records and 1,000 probe records (`PROBE_LIMIT`, dev37: the probe
routes are paid turns, so their class is small and separate). There is no eviction: forgetting a key would allow a
completed action to run again. A full work class does not block stops, cleanup,
releases, result deletion or project mutations. Requests
without a key are never counted and always work as in S5; they are the intended
way out when a class is full. Only a client of the same UID that deliberately
spends keys can fill the table.

A record holds the route, a 32-byte fingerprint, and the settled task with its
public result (ref, receipt, or summary) or service error. The largest results
are Bundle/Prepared summaries, typically kilobytes to tens of kilobytes; export
ZIPs are not stored (export returns a TaskRef). The worst case, with both classes
full of summaries, is on the order of hundreds of megabytes, which is acceptable
for a local one-UID service.

### Generation and restart

The table is process memory, and every Owner open has a new generation. After a
restart, a key sent with the previous `Lab-Owner-Generation` receives
`request_unknown_generation`: an explicit refusal without action. Read the
current generation from `GET /v1/health`. Bundle and Prepared creation are
content-addressed and idempotent on their own; keys follow the same rules there
for uniformity, and a repeat without a key still returns the same digest.

### Errors

| Code | HTTP | When |
| --- | --- | --- |
| `service_environment_unconfigured` | 409 | `{"kind": "service"}` without a configured service environment |
| `request_conflict` | 409 | the key is known, but the route or parameters differ |
| `request_unknown_generation` | 409 | the header generation is not the current one |
| `request_table_full` | 503 | the key table limit for the route class is reached |
| `probe_sessions_open` | 409 | (dev37, Owner, stage `before_start`) `check`, `conformance` or an operation on a target that has unreleased probe sessions; close or reset them, or stop the target |
| `bundle_probe_only` | 409 | (dev37, Owner, stage `before_start`) a campaign, evaluation or conformance on a Bundle of kind `probe` |
| `target_slot_held` | 409 | (dev46, Owner, stage `before_start`) `target start` while another target of this Owner is `ready` with a different Bundle, launch or project; stop it first (`lab agent list --ready`, `lab agent stop`). The same Bundle with other secrets stays `binding_mismatch`. |
| `core_refused` | — | (dev49) not an HTTP error: the `failure.code` of an operation whose start the core refused; details in `refusal` ([above](#core-refusal)) |

The probe session codes (`probe_session_not_found`, `probe_session_closed`, `probe_ceiling_reached`, `probe_ceiling_unverifiable`, `probe_limit_reached`, `target_not_ready`, `target_undeclared`, `target_entry_invalid`) are in [Projects](projects.md#errors), and the current Owner state is at `user_version` 10: earlier state is refused with `unsupported_schema` (ADR-0004), so start a new pair with a new state directory.

Project routes fail with `ProjectError` codes in the same body, with stage
`project` — for example
`{"error": {"code": "active_operation", "stage": "project", "message": "..."}}`.
The closed set (`project_not_found`, `active_operation`, `link_failed`,
`home_busy`, …) and each code's HTTP status are in
[Projects](projects.md#errors). The CLI prints them as ordinary service
refusals (exit `1`); `lab serve` prints `home_busy` as its one startup error
line.

### CLI

`lab --help` groups primary commands by task. Agent processes use `lab agent`,
execution policy uses `lab exec-policy`, routing uses `lab routing OPERATION`,
and Owner jobs use `lab job`. `lab probe` is the probe entry point;
`lab view OPERATION` reads a campaign, while `lab campaign DIR` runs one.
Manual operation release is `lab release-operation ID --reason TEXT`.
Old names remain compatible; see [Old names](../../../docs/guide/cli.md#old-names).

- Global `--request-id ID --generation G` apply to mutating commands only; on a
  reading command, or on `serve`, any request-key option is a usage error. Both
  must be given together.
- Without them, every mutating command first reads `health` for the generation
  and generates a random key, so every CLI mutation carries a key.
- `--no-request-id` sends the mutation without a key (S5 behaviour). It cannot be
  combined with `--request-id` or `--generation`.
- `request_conflict`, `request_unknown_generation`, and `request_table_full` are
  printed as ordinary service refusals (exit `1`). A `request_table_full` output
  adds a hint to retry with `--no-request-id`; the CLI never applies it itself.
- On `acceptance_unknown` (exit `125`), the output names the key and generation
  it used:

  ```json
  {"error": {"code": "acceptance_unknown", "stage": "transport", "message": "The mutation acceptance is unknown."},
   "acceptance": "unknown", "action": "bundle create", "cause": "timeout",
   "request_id": "REQUEST_ID", "owner_generation": "GENERATION"}
  ```

  Repeating the same command with those values is safe; the CLI does not repeat
  it on its own:

  ```bash
  lab --socket /private/lab.sock --json --request-id REQUEST_ID --generation GENERATION \
    bundle create --request /private/bundle.json
  ```

See the [service guide](../../src/evolution_lab/guides/service/README.md) for
operator boundaries.

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
status/list `/7` and event `/3` retain their forms. On exact b13 every declared
probe uses suspend admission and unavailable health refuses before worker
spend. `/16` `on_unavailable: continue` is conditional on the future
b14 repin. For launch consistency, required `/3` fields, bounded restarts,
explicit-resume counts and helper cleanup, see the
[campaign probe launch guide](../../src/evolution_lab/guides/campaign-package/SKILL.md#reflector-probe).

## Explicit continuation additions

These additions keep `lab-operation-status/8`, the four-key refusal, and the original start/resume `/1` requests unchanged. An old request cannot include any new field.

| Route | Shape and behavior |
| --- | --- |
| `GET /v1/operations/O/refusal` | `lab-operation-refusal/1`: `{schema, operation_id, start, findings}`. Each finding has exactly `{code, path, class, instance_digest, effects, effect_texts, differs}`. Reads the last start that reached Core admission/execution; `404 not_found` if no full record is stored. |
| `GET /v1/operations/O/inputs` | `lab-operation-inputs/1`: `{schema, operation_id, starts}`. Each start has exactly `{start, prepared_digest, bundle_digest}`. |
| `POST /v1/operations/O/resume` | Also accepts `lab-service-resume-request/2`: `{schema, target_ref, secrets, accept, reason, prepared_digest}`. `accept` is an array of `{code, digest, effect}`; nullable `effect` means Core `default`. `reason` and `prepared_digest` may be null. |
| `POST /v1/operations` | Also accepts `lab-service-start-request/2`: `{schema, prepared_digest, target_ref, secrets, linked_from, baseline, seed}`. `linked_from` is a same-state campaign operation or null; `baseline` is `inherit` or `fresh`; `seed` is `champion` or `project`. |

New mutation shapes return the existing `lab-operation-status/8`. The same request-key rules apply. A nonempty acceptance requires a nonblank reason (`authorization_reason_required`); each item must name a current stored authorizable finding (`authorization_unknown_finding`), and an explicit effect must be offered (`authorization_effect_invalid`). Lab validates the whole batch before writing decisions. It prints the resolution of default before writing through Core's public API. Automatic resume never writes authorizations.

`prepared_digest` selects a campaign PreparedExecution of this state. Owner records its history, and the ready target must match its Bundle. Core alone judges changed input identity. A latest-start Core refusal with saved findings can retry before journal creation only with a real run directory and confirmed cleanup; it remains `not_created` until Core writes a journal. Existing deletion, busy, orphan, manual-cleanup and fresh-target checks remain.

A linked start refuses `linked_predecessor_unknown` (404) for an absent or non-campaign predecessor, or `linked_predecessor_deleted` (409) after deletion. Core owns unfinished/mode/baseline/seed admission. Owner copies the verified final Champion by default and passes a runtime manifest copy with Core's `linked_from`; a Bundle manifest containing that field still refuses `linked_run_unsupported`. `seed: "project"` is explicit and carries Core's caveat. See the [Owner API](owner-s4.md#linked-starts).

The `/8` `resume` field retains its Core-suspension-only meaning. A null field does not forbid the new explicit refusal continuation through resume request `/2`; consult `/refusal` for the named findings. No existing response field is repurposed.

## Start from a fixed evaluation

`POST /v1/operations` accepts the closed `lab-service-start-request/3` shape `{schema, prepared_digest, target_ref, secrets, linked_from, baseline, seed, baseline_from}`. The new `baseline_from` is a fixed evaluation operation ID of this service's state or null. Supply at most one of `linked_from` and `baseline_from`. `baseline` and `seed` shape a linked start: with `baseline_from` they must be `inherit` and `champion`, and `lab start --baseline-from` refuses `--baseline` and `--seed`. The response is `lab-operation-status/8` and uses the same mutation-key rules. Use `lab start PREPARED --baseline-from OPERATION` with the usual target, generation and secrets options.

| Code | HTTP | Meaning |
| --- | --- | --- |
| `baseline_source_unknown` | 404 | The source is absent, has no measurement journal, or is unavailable as a fixed evaluation. |
| `baseline_source_deleted` | 409 | The evaluation's results are deleted, deleting or delete-failed. |
| `baseline_import_unsupported` | 422 | Bundle preparation received a manifest with `baseline_import`. Select the source as a start condition. |

Core's import findings are stored and read through the existing refusal and explicit continuation surfaces. See the [Owner API](owner-s4.md#baseline-from-an-evaluation).

## Worker failure details

`GET /v1/operations/{operation_id}/failure` reads worker exception identifiers for the latest start. It returns `404 not_found` when the operation is unknown or no validated worker exception record is available for that start. It never falls back to an earlier start. Like `/refusal`, it rejects mutation-key headers and uses the service's reader admission.

The response is the closed `lab-operation-failure/1` shape:

```json
{"schema": "lab-operation-failure/1", "operation_id": "operation", "start": 1,
 "scope_id": "scope", "exception": {"class": "builtins.RuntimeError", "code": null},
 "log": "scopes/scope/stderr.log"}
```

`class` matches `^[A-Za-z_][A-Za-z0-9_.]{0,255}$` (or is `unknown`). A non-null `code` matches `^[a-z][a-z0-9_]{0,63}$` and comes only from an exception whose module starts with `evolution.`. The exception message stays in the private process log. `log` is relative to the service's state directory. Human `lab status` reads this route when its fixed failure code is `lab_error`; JSON output and `lab-operation-status/8` are unchanged. The panel may read this route but adds no view.

## Private campaign trace export

`POST /v1/operations/{operation_id}/trace` takes the new closed `lab-service-trace-request/1` object with exactly `schema`, `out` (absolute file path on the service host), `secrets` (string map), `disclose_private` (boolean), `measurements` (list of measurement IDs) and `max_field_bytes` (positive integer, normally 65536). Existing request and response schemas are unchanged. Standard mutation-key semantics apply.

The response is `lab-trace-summary/1` with exactly `schema`, `tries`, `scored`, `failed`, `unavailable`, `unverified`, `in_flight` and `unset_secrets`; all counters are nonnegative integers. Core creates a new owner-only output file and owns its bytes, authentication and partial-output cleanup. Private case data is in that file, not the summary response. The service has no private-stdout export mode.

Missing disclosure refuses with `private_disclosure_required` (403); fixed evaluation operations refuse with `trace_not_a_campaign` (409). Unavailable exports, including an existing destination or Core authentication/read refusal, use the existing `export_unavailable` (409), without filesystem paths in the message. Deletion and unknown-operation refusals retain their existing meanings. Owner-held result work retains its read lease when the HTTP caller disconnects.

CLI: `lab trace OPERATION --out FILE --disclose-private [--measurement ID] [--max-field-bytes N] [--secrets-file FILE | --secrets-fd N]`. The project form `lab trace DIR [OPERATION] ...` reads the same saved campaign locally without a service socket or wheel arguments.

The project DIR trace wrapper resolves target/role environment names from the selected operation's saved Bundle. Caller environment values are overlaid by explicit file/FD secrets, with saved public Bundle environment as the base. Missing required values refuse before output creation. Owner and service trace keep their explicit-map contract; the service does not read client process environment.
