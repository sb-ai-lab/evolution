---
name: evolution-lab-diagnostics
description: Read Lab check and conformance results, unknown and cleanup states, reports, diffs and exports without over-claiming.
---

# Diagnostics

For live monitoring or choosing a continuation after a pause or refusal, use [campaign operations](../campaign-operations/SKILL.md).

Use this guide to interpret what Lab returns after a person has run an action.
It does not authorize running anything: `check`, `conformance`, target start,
campaigns and evaluations are explicit actions that a person requests. Never
run them automatically, as a precaution, or to "verify" a package.

Lab keeps separate facts that must be read separately: process outcome, core
result, cleanup, and Lab persistence. One of them never stands in for another.
A successful task, an exit code or an HTTP success is not a quality, cost or
cleanup claim.

## Current campaign reader

The development source with exact core 2.0.0b22 reads family 14 and family 15.
The latest released pair is Lab `0.0.3.dev80` / exact core `2.0.0b21`; the schemas below describe development source, newer than that release. See `docs/RELEASES.md` for delivery history. Campaign view is
`lab-campaign-view/15` with `family: 14 | 15`; family 14 keeps the `/7` content (no round `cohort` or attempt
`alpha_total` keys, including null). Owner report is `lab-owner-report/15`, which includes `lab_independent`. Status/list
`lab-operation-status/8` and `lab-operation-list/8` carry the latest Core terminal. A matching schema does not make another pair's state reusable. Core owns intervals, final contrasts and promotion.
The panel shows every recorded final entry in core order, settings, roles,
connection and baseline refreshes, plus confidence `margin` and estimate `exact`.
Final entries include members, contrasts, selection, `listed` and next baseline.
Finalist, final decision and Champion transition are separate facts: only present
`champions[]` lineage with `origin: promoted_attempt` and the matching attempt ID
confirms promotion. A recorded transition does not assert the current Champion.

**Family 15 (manifest `/16`).** The view's `independent` block carries core's settings (mode, repeats R, verification,
execution noise, statistics), each attempt with its outcome and reason, and per scope the statement for **that scope's own
rule**: `bound` reads "gain <estimate>; with <c> confidence not below <L>; threshold <t>"; `point` reads "gain
<estimate>; threshold <t>"; `no_threshold` shows the gain and says there is no threshold; `status` shows core's recorded status
or unavailable code with no numbers. There is never a two-sided interval, and the enclosure's upper end is not a bound.
Every comparison is against the **round's champion**, labelled with a stored measurement, a fresh measurement, or "not
recorded"; there is no "Parent" reference. An attempt with no comparison shows its reason and core's attainability facts.
Each attempt retains separate `gate_comparison` and `global_comparison`, each nullable,
with its own scopes and `reference_measurement_id` (string or null). `comparison`
is Core's global comparison when present, otherwise its gate comparison. Lab copies
each field independently, without deriving layers or reference ids. CLI, markdown
and panel show both recorded levels; an absent level is omitted.
The view shows a one-sided lower bound. Promotion is automatic and L3 is unavailable. Lab does not recompute any gate. See the 2026-10-19 amendments of PDR-0015 and PDR-0017. For linked starts, use [campaign operations](../campaign-operations/SKILL.md#propose-continuation-or-a-successor).

`Owner.suspensions`, GET `/v1/operations/{id}/suspensions` and
`lab suspensions OPERATION [--json]` share the closed `lab-suspension-evidence/1`
read. Its identifiers and codes explain recorded pauses, not all remaining work;
counts, digests and truncation remain explicit. The panel reads current evidence
on the suspended screen and loads history after resume when opened. New sequence
values invalidate old answers; failures mark retained evidence stale, and result
deletion clears it. Unavailable or stale history neither proves there were no
pauses nor confirms a current cause.

A confirmed foreign family raises typed `CampaignFamilyUnsupported`; empty/torn
creation without validated identity is `CampaignUnreadable` (`campaign_unreadable`)
before family classification. Wrong exact pin is separate (`core_raw_unsupported`).
Owner preserves process/cleanup/persistence facts while fresh campaign reads are
unavailable; direct report reads preserve core exception types. H1 typed
family/unreadable errors give `campaign_unavailable`; wrong-pin status gives
`unknown` / `lab_error`, and H1 report reason is `unknown`. Unknown is never success.
A cached family14 result may survive temporary reread failure, even after restart,
but a confirmed foreign family suppresses it; it proves no fresh readability.
Before creation and after deletion, `not_created` and `results_deleted` remain distinct.

For family 15, view `/15` also carries `checks`, `checks_state` and
`checks_reason`; the nested Owner report `/15` carries `lab_checks`,
`lab_checks_state` and `lab_checks_reason`. An admitted projection has state
`present` and null reason: checks is null if Core has neither map, otherwise
`{modes: {...}, shadow_hits: {...}}` with an absent map copied as `{}`.
An unreadable projection has null checks, state `unavailable`, reason `unreadable`.
These fields remain independent of binding-read failures and `independent`.
Family 14 has none of these keys. Modes and individual counts are copied from
Core; counts of different checks are not comparable and must not be summed.

Choose a fresh private state path for the producing pair, not the old dev58/b10 state;
this guide creates no directory. Owner SQLite schema13 upgrades a schema11 or schema12 state in place at
connect, after which an older Lab refuses it, and refuses earlier Owner schemas with `unsupported_schema`.
There is no other migration and no copying of old state/run folders. Read, resume and clean up old runs with their producing wheels and state.

Owner SQLite schema13 records each start's prepared digest; schema12 added each start's Core refusal findings
and schema11 durable start initiation metadata. Schema10 added execution-policy confirmation and per-start policy history, retaining reflector-probe scopes and the separate per-start
Core-terminal columns introduced in v8. Connect upgrades a schema11 or schema12 state in place to schema13, which
an older Lab then refuses; earlier Owner schemas are refused with `unsupported_schema`, without migration. Deploy with a fresh private state for the producing pair. A matching
schema does not make another producing pair's state reusable. These instructions
name a prerequisite; they do not create state or upgrade old runs.

<a id="scorer-content-on-exact-dev55b8"></a>
## Scorer content on exact Core b22

Lab uses core's public `ScorerTraceReader` for directory and pack traces,
addressed by the selected measurement and scoring request. Completed journal
records still gate disclosure and determine scoring state; identity/hash checks,
`lab-sample/1`, campaign `/15`, families 14 and 15, private disclosure and key masking stay
unchanged. Missing records are `trace_missing`; unknown request sizes are
`trace_missing` for packs and conservatively `unreadable` for directories.
Verified input/dialogue survives missing stdout with `trace_missing` (pack) or
`unreadable` (directory), even for failed scoring. Empty failed stdout remains
available; admitted scoring still requires valid JSON. A failed payload read
preserves no unverified input. Existing private failed-scorer `exit_status` can
carry core's returncode; directory traces give null. No stderr is read.

One core `ScorerTraceReader(root, run_boundary="nofollow")` is opened per
request. Metadata (`sections=()`) and payload use that same reader; payload reads
pass `max_section_bytes=33554432` (32 MiB per selected section). Request/stdout
metadata and returned bytes are also checked before parsing/disclosure. Oversize
rejects the whole trace as `too_large`, without partial input. Typed `too_large`
maps to that code; other typed read failures map to `unreadable`.

Strict no-follow traversal and bounded payload reads replace the prior b5
root-swap/file-growth exceptions. The section limit does not cap the whole pack
index or process RSS and does not make files immutable to their owner. This
residual limit is explicitly accepted for exact dev54/b6 (separately, as for local dev53/b6 before it); no successor, including a
Lab-only version on b6, inherits acceptance automatically. For exact dev56/b8 the user accepted it separately on 2026-10-20 (not inherited). For exact dev57/b9 the user separately accepted it on 2026-10-20; dev56 acceptance did not transfer. For exact dev58/b10 the user separately accepted it on 2026-10-22; dev57 acceptance did not transfer. For exact dev59/b11 the user separately accepted it on 2026-10-23; dev58 acceptance did not transfer. The Core reader modules are unchanged: nofollow and 32 MiB per selected section remain, without an aggregate pack-index/RSS cap or owner-file immutability. For exact dev60/b12 the user separately accepted this same residual limit on
2026-10-25; b11 acceptance did not transfer. Every next pair needs its own assessment. Old pairs/runs remain
with their producing environments; there is no automatic shutdown or migration.
Other raw readers retain descriptor-relative no-follow reading.

For exact dev62/b14, the user separately accepted this residual on 2026-10-27
under ADR-0019, including the audited additive root exports and writer's
process-local Lock-to-RLock change. Reader implementations and pack format
remain unchanged; nofollow and 32 MiB per selected request/stdout section remain,
without an aggregate pack-index/RSS cap or owner-file immutability. Dev61/b13
acceptance did not transfer. Every next pair needs its own assessment and
explicit acceptance. This does not authorize release, migration or state creation.

## Core result codes (b9 and later)

`artifact_ledger_missing` means the run's `artifacts.jsonl` ledger is missing or empty;
the retained `artifacts/` directory is not removed. Restore the ledger from a
trusted copy of that run directory, then explicitly repeat the intended action
with its producing pair. Do not fabricate a ledger from the files you see.
On Owner start/resume, this refusal arrives as `failure: core_refused` with
`refusal.code: artifact_ledger_missing`; `program_inside_seed` uses the same
Owner refusal form. The closed failure table is unchanged. Legacy/H1 keep
exit `2` and the reason in stderr (legacy also has `core_refused: true`);
they do not gain Owner's structured fields.

The #263/#268/#269 `rebaseline` workaround is no longer needed since b9. Old runs
keep their producing pair; this does not introduce a Lab continuation action.

On exact Core b22, reflection `input_too_large` remains the journal's exact
`error_class` and `reason`; core's internal `unknown` classification does not
replace those codes. The existing suspension is `reflector_failing`.
Retrying the same oversized Bundle does not fix it. Authorization or editing
sealed inputs is not a remedy; use a corrected package and a new campaign under
the existing continuation rules.

The campaign worker preserves core's result codes separately from Attempt and
suspension reasons: `storage_inodes_insufficient` accompanies terminal
`storage_capacity_exhausted`, and `reflector_budget_unavailable` accompanies
`persistence_failed`, both with owner `artifact_store`. This does not add a new
panel/CLI fault screen or infer successful settlement from a normal library
return. Fixed evaluation uses the library measurement path, without copying
core CLI's inode preflight; its existing generic `lab_error`, cleanup and
settlement behavior remain.

<a id="check"></a>
## `check`

`check` is the H1 preflight against a started target: public health,
capabilities, identity and compatibility. It is not conformance, not a seed
attestation, not paid readiness and not permission to run. While its task
runs, it holds the target. Read its terminal `TaskStatus` (below).

<a id="conformance-result"></a>
## `conformance`

Runtime conformance is available only when the launch file declares
`{"kind": "core-session-public/1"}`. The historical
`core-a5-session-public/1` token is readable but does not authorize it. See
[Conformance mode](../target-connector/SKILL.md#conformance).

After conformance may have dispatched, its finalizer always cleans the helper
scope and the managed target scope. In the result payload, read `process`,
`cleanup` and `target_cleanup` as separate facts, and read the task's
`persistence` independently.

- A received `core_report` with `ok=false` is a completed conformance result
  that found problems, not a process failure.
- The full report is private.
- The target is always stopped afterwards. Before any evaluation, require both
  cleanup facts to be complete and the target to be `stopped`, then start the
  target again explicitly and use the new target ref.

<a id="task-states"></a>
## Task states and `unknown`

A task's `state` is `pending`, `running`, `succeeded`, `failed` or `unknown`.
When an Owner opens, tasks of an older Owner generation that were still
pending or running become `unknown`: nobody knows how they ended. `unknown` is
never success and never failure. Inspect the related resources and decide
explicitly; Owner does not resume or repeat such work. An interrupted export
is recovered by an explicit repeat export; an interrupted delete by an
explicit repeat `delete_results`.

Through the service CLI:

- exit `125` (`acceptance_unknown`) — the mutation may or may not have been
  accepted. Repeat the same command with the printed `request_id` and
  `owner_generation` to learn the outcome without a second action. The CLI
  never repeats on its own.
- exit `124` or `130` after a printed task ref — only the local wait ended;
  the accepted work stays with Owner. Read it later with `task status`.
- `request_unknown_generation` — the key belongs to an older service
  generation; nothing was done. Inspect state and decide.

<a id="persistence"></a>
## Persistence

Every task carries `persistence` with its own state, a closed set:
`not_attempted` (the default — no result was recorded, for example because the
task never reached a terminal state or produced no result), `saved` (Lab
recorded the result) or `failed` (the result could not be recorded, even if
the action itself ended) (`src/evolution_lab/owner/models.py:309-313`). Check
it before using a task's `result.payload`.

<a id="core-campaign-persistence-failed"></a>
### Core campaign `persistence_failed`

`persistence_failed` is a core campaign terminal (exit `1`) on any journal or
artifact boundary fault, whether the fault happens at campaign start or
mid-run, including `journal_busy` from a lease another live process still
holds (core `campaign.py:83-106`, `append_log.py:285-287`). It appears as the
campaign result's `terminal` (`src/evolution_lab/owner/_campaign_worker.py:147–160`)
and in the core report. It is an infrastructure outcome, not a score, and is a
separate fact from the Lab task `persistence` state above.

For #262, unavailable baseline/grouping evidence during resume can return
`terminal: persistence_failed`, `owner: measurement_journal`,
`code: journal_unavailable`. The campaign worker prints these exact fields in
stdout and exits `1`; Owner does not turn them into a new structured failure
or create a new suspension. Owner rereads public journal status; if readable,
results remain present and the campaign view copies existing suspensions. If
unreadable, fresh results are unavailable; a cached result cannot authorize resume.

Restore the missing evidence from a trusted copy of the same run and inspect
fresh status and cleanup facts. `lab resume` still requires a settled previous
start, the required cleanup, and a fresh readable, incomplete, suspended campaign.
Unreadable data gives `campaign_unreadable`; readable data without a suspension
gives `campaign_not_suspended`. An existing suspended journal may permit resume,
but Core can return the same fault until its evidence is restored. There is no
automatic restoration, retry, or general resume of every `persistence_failed`.

On b10, `connection_incompatible` with `connection.detail.code:
connection_description_secret` means the target instance's `describe()` repeats
a value from the role or scorer environments. It is not a `metric_contract`
error: fix `describe()`, build a new package and start a new campaign. No baseline
has been measured in this preflight case. If a later candidate instance does
the same, Core returns `persistence_failed`, owner `campaign_journal`, code
`record_matches_secret`. In Lab that campaign cannot continue: fix `describe()`,
build a new package and start a new campaign. Core's plain-command recovery is
not a Lab resume path; `campaign_not_suspended` is unchanged (PDR-0015).

For each campaign start or continuation, endpoint WARNING and "without
credentials" lines go to the worker's stderr process log:
`<state>/scopes/<start.scope_id>/stderr.log`. They precede the evolve call.
HTTP to any host is allowed; only non-loopback HTTP gets an unencrypted WARNING
from the public helpers. An absent chat `api_key_env` means no authorization
headers; declared but empty is refused. Adding or removing the declared key is
one `run_identity_changed` (`rebase`) and one operator `evo tree authorize`, not
a changed-identity Lab continuation.

An unexpected campaign or fixed-evaluation worker exception also leaves its
traceback in that scope's `stderr.log`, with environment values redacted. Before
the worker has built its redactions, it records only the exception class, without
the message or traceback. Each exception record is limited to 65,536 UTF-8 bytes;
an oversized record ends with `[traceback truncated]`. A broken stderr does not
change the worker's exit result.

`lab status` reports this as `failure.code: lab_error`, with the worker's
`before_start` or `running` stage and Lab's fixed public message. Human status
also prints `worker exception: <qualified class>` (with ` (code <code>)` for a
typed Core error) and `traceback: scopes/<scope_id>/stderr.log (in the service's state directory)`.
The class and nullable code are in `diagnostic.json` (`lab-campaign-diagnostic/5`
or `lab-evaluation-diagnostic/2`); no message is stored there. The separate
`lab-operation-failure/1` service route reads the latest start only. Older
diagnostics remain readable but have no exception metadata. JSON status output
stays unchanged. Exception text stays in the private process log. Core refusals and Core terminals keep their
existing fields and meaning. An exception is still an abnormal worker return:
a quiet settlement alone does not confirm target cleanup or permit reuse.

Recovered provider attempts retain `evo tree evolve: note: <text>` in that log.
Failed/suspended results print each public `provider_attempt_lines` entry there
as `evo tree evolve: <line>`, after the JSON result in the same scope's
`stdout.log`. The secret-description persistence hint is also in stderr, not a
new status or report field. For per-request details inspect the private role
journals under the run's `editor-logs` and `reflector-logs` (including Grouper
logs): their `provider_attempt` records carry `http_status` and `provider_code`
(nullable safe status/code, not provider message text). Lab does not expose these
service events as new content fields or add them to campaign journals.

### `CampaignInvariantViolated: invalid journal record` after `reflection_failed`

On dev58/b10, this `/16` failure is Core defect #318, fixed in b11: the round
closes `unplanned` with no Attempt, retains the Champion and lets the campaign
continue. In Lab, start a new campaign on the producing pair with a fresh, pair-specific state directory; there is no state migration or runtime wrong-pair gate.
Read, continue and clean up old runs with their producing wheels and state.

Core itself can continue the stopped run after a producing-version rebase with
`evo tree authorize`, then the same `evo tree evolve` command. This is a Core
capability, not a supported Lab continuation path. For the #313 frozen-Attempt
exception, see Core b11's `CHANGELOG.md`, “Upgrade”, and the `HANDOFF.md`
delivered with the Core b11 wheel.

<a id="cleanup-required"></a>
## `cleanup_required` and release

A target is `starting`, `ready`, `stopping`, `stopped`, `unknown` or
`cleanup_required`. An operation or target moves to `cleanup_required` when
cleanup could not be confirmed: a timeout, a lost mutating response, a forced
stop, a missing or invalid receipt, a changed target boot, a failed delete, a
persistence failure, or a lost Owner. Resources left by an older Owner
generation become orphaned or `cleanup_required` in the same way.

While cleanup is required, `diff`, `export` and `delete_results` refuse with
`cleanup_required`, and the target cannot be reused. Release paths:

- `cleanup` — an explicit task that cleans the operation scope and its target
  scope. Call it after Stop if the operation is still running (otherwise
  `busy`). The target is stopped; a later target needs a new explicit start.
- Manual release — an operator's assertion that external remnants and the
  fixed target port were verified clear. It releases only the linked
  reservations, keeps cleanup evidence unknown, and does not prove campaign
  success.

The full conditions are in the [Owner guide](../owner/README.md).

<a id="report"></a>
## Reports

`report` is independent of process status and is private: it can contain the
full core report and integration data. For evaluation, the report wraps the
public measurement projection under `core`; `lab_result` carries coverage,
`stopped_reason`, `unmeasured`, `min_coverage` and persistence, and `measurements`
contains scored-sample metrics and coverage. An unscored sample is retried by
Core's `sample_retries`, then evaluation continues; only a global stop sets
`stopped_reason`. A finished or released evaluation with positive expected coverage, no stop
reason, saved persistence and coverage at least `min_coverage` can qualify as a baseline (default 0.95; pre-dev60 prepared
evaluations retain 1.0). Accepted partial coverage has `coverage.state: "accepted"`
in the report's measurement after a cross-check against Core coverage.
Missing facts stay missing: before a confirmed identity
coverage is `None`; a missing journal is `not_created`, an unreadable one
`unavailable`; missing stop facts give `stopped_reason="unknown"`. None of these
is a zero score or a successful evaluation.

<a id="diff-export"></a>
## Diff and export

- They exist only for a finished campaign, and only for the final champion
  selected by Evolution against the saved seed. Fixed evaluation has no
  champion, so its diff/export is `export_unavailable`.
- `no_change` means verified identical identities; it has no ZIP and does not
  mean a missing result became an improvement.
- A `lab-change-set/1` ZIP carries added, modified, deleted and mode-changed
  writable files. It cannot represent changed empty directories or
  file-to-directory replacement (`export_unavailable` instead).
- Lab never applies a change to an external project. Applying it is a
  separate decision by a person.
- A ZIP is not an improvement or cost claim: its manifest records the known
  campaign outcome (possibly unknown) and separate campaign and measurement
  completeness.
- After `delete_results`, new diff/export/read return `results_deleted`.


Core storage failures such as `persistence_failed` appear as `core_terminal`
`{owner, terminal, code, hint}` in status/list `/8` and Owner report `/15`.
`hint` is bilingual `{ru, en}` when a repair hint is known, otherwise null.
Each `starts[]` entry keeps its own terminal; the top-level field copies the last
start. An accepted resume opens a start with null `core_terminal`. Successful
finishes, suspensions and refusals have null `core_terminal`; suspension and
refusal retain their existing fields. CLI status/watch print the terminal and
its hint. The closed Lab `failure` codes and cleanup rules are unchanged.

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
