# Owner results API: resume, diff, export and deletion

> **Who this is for.** Anyone driving Lab from Python who needs the exact behaviour of `Owner.resume` and the Owner's result methods: `diff`, `export`, `read_export` and `delete_results`. For a walkthrough of what these give you and when to use them, read [Read the results](../../../docs/guide/results.md) first; for the whole Owner lifecycle see [Run your first evaluation](../../../docs/guide/first-evaluation.md) and the [Owner guide](../../src/evolution_lab/guides/owner/README.md). This page is the exact reference.

Use these async methods to inspect the change from a campaign's seed to its
final champion, export the changed files, or delete saved results. Evolution
selects the champion; Lab reads that decision and compares the saved files.
Exporting does not apply the changes or establish that they improved the agent.
For the full run lifecycle, see the
[Owner guide](../../src/evolution_lab/guides/owner/README.md).

<a id="execution-policy"></a>
## Execution policy and routing

`Owner.confirm_execution_policy`, `execution_policy`, `execution_policies`,
`start_target(..., execution_policy=digest)` and `operation_routing` use the
[execution policy API](execution-policy.md). Policy confirmations require an
expected digest and remain in the pair state. Lab runs the optional gateway
with the target; without a policy it starts no gateway. The routing read is
`lab-operation-routing/1`; report `/15` includes it only for policy operations.
Target rows are `/2`, campaign view `/15`, operation status/list `/8`.

## Worker failures

`await owner.failure(ResourceRef("operation", operation_id)) -> WorkerFailureRecord | None` reads the latest start's validated worker exception diagnostic. It does not search earlier starts. Missing, invalid, retained older-format diagnostics without exception metadata, and deleted results return `None`; an unknown operation raises `not_found`.

The closed `lab-operation-failure/1` payload has exactly `{schema, operation_id, start, scope_id, exception, log}`. `exception` has exactly `{class, code}`: a bounded qualified Python exception class and a nullable typed Core error code. `log` is `scopes/<scope_id>/stderr.log`, relative to the Owner state directory. No exception message is exposed. Campaign diagnostics use `lab-campaign-diagnostic/5`; evaluation diagnostics use `lab-evaluation-diagnostic/2`. Previous diagnostic versions remain readable for their existing failure, refusal and terminal data. Operation status `/8` and its fixed-message `Failure` stay unchanged.

## Linked starts

`await owner.start(prepared_digest, *, target_ref, secrets, client_ref=None,
linked_from=None, baseline="inherit", seed="champion")` starts a linked successor
when `linked_from` names a campaign operation of this state. Unknown operations
refuse as `linked_predecessor_unknown`; deleted results refuse as
`linked_predecessor_deleted`. Core decides whether the predecessor is finished
and whether its mode and baseline are compatible.

The default copies the predecessor's verified final Champion into the successor's
owned inputs. `seed="project"` explicitly selects the Bundle seed and preserves
Core's different-condition caveat. `baseline="fresh"` selects a fresh baseline;
the default asks Core to inherit compatible evidence. The manifest inside a
Bundle still cannot declare `linked_from`. Owner passes Core an admitted manifest
copy with the link and selected seed, keeping its private metadata outside the
Core run directory. Continuation reuses that link and seed.

## Baseline from an evaluation

`Owner.start(..., baseline_import=BaselineImportChoice(source_operation_id, source_state, source))` selects a fixed evaluation with retained results and an existing measurement journal. Use the canonical measurement directory for `source`. A project run may resolve the choice from an earlier pair state. Service callers name an evaluation of their own state through start request `/3`.

Owner keeps the Bundle seed, writes the canonical source into a runtime manifest copy and stores the selection in `baseline-import.json` beside the operation. Its schema is `lab-operation-baseline-import/1` with exactly `{schema, operation_id, source_operation_id, source_state, source}`. A retry after a stored Core refusal uses that saved choice. Use one of `baseline_import` and `linked_from`. Core checks compatibility and authorizes imports through its existing public decisions.

Unknown or unavailable evaluation results give `baseline_source_unknown`. Deleted, deleting and delete-failed results give `baseline_source_deleted`. A Bundle manifest that declares the key gives `baseline_import_unsupported`.

## Refusal findings

`await owner.refusal(ResourceRef("operation", operation_id)) -> RefusalRecord | None`
reads all Core findings stored for the operation's last start that reached Core admission/execution. A record contains
`operation_id`, `start` and `findings`; each finding keeps `code`, `path`, `class`,
`instance_digest`, `effects`, `effect_texts` and `differs`. Hard findings have a null
digest. Effect explanations come from Core; an effect without an explanatory
sentence has an empty text. The payload schema is `lab-operation-refusal/1`.

The read returns `None` if that Core start has no saved full refusal, including
starts made by older workers. A later start that fails before Core admission
keeps the prior refusal available, including after reopening Owner. Existing
Core decision files stay in the same run; failed writes can be retried with
the same explicit acceptances. A later Core start without a refusal clears
this recovery exception. Cleanup, deletion and real-directory guards still apply.
The legacy status continues to describe the latest accepted start.
The existing four-key `Refusal` and `lab-operation-status/8` remain unchanged.

<a id="resume"></a>
## Resume

`await owner.resume(ResourceRef("operation", operation_id), *, target_ref, secrets,
accept=(), reason=None, prepared=None) -> OperationStatus` continues a **campaign**
operation whose Core journal is suspended, or whose last Core start ended in a
stored Core refusal (PDR-0055). It is not conformance, evaluation, or a second
run of the same start: it opens a new **start** — a new scope with a new,
non-reusable ID — over the same campaign journal and run directory, on a
`target_ref` you pass explicitly (Lab does not pick or start a target). Any
`ready` target of the same Bundle is accepted, not only the one the first
start used; this also works from an Owner generation later than the one that
ran the original start, once the operation itself has settled (below).
`prepared` optionally selects a campaign PreparedExecution of this state; omission
keeps the operation's current prepared digest. The selected Bundle must match the
ready target. `await owner.inputs(ref)` returns `lab-operation-inputs/1` with each
start's `prepared_digest` and `bundle_digest`; status `/8` remains unchanged.
Lab does not write supplied secrets to disk, state, logs or
status (ADR-0004), and they must be supplied again, exactly as for
`owner.start`. Core b17 does not mask subprocess Editor stderr or scorer traces
(#200). Components must not print secrets there, since Core may persist that
output.

Pass `Accepted(code, digest, effect=None)` items in `accept`, with a nonblank
`reason`, to authorize exactly the saved findings of the last Core start. Lab refuses
an unknown finding, a missing reason or an effect Core did not offer before
writing any decision. Omission writes `effect: "default"` through Core's public
`Authorization`/`write_decision`; the worker enables Core's decision-directory
reader. Lab prints the effect that default resolves to before writing. Automatic
resume never supplies acceptances or writes decisions.

**Admission**, checked in this order, before any new scope, reservation or
helper is created (§2.3 of the design spec); all codes are closed `OwnerError`
codes, only `campaign_unreadable` and `campaign_not_suspended` are new:

1. No such operation → `not_found`. Not a campaign operation (an evaluation
   or conformance ref) → `invalid_argument`.
2. `results_state` is `deleting`, `deleted`, or `delete_failed` → `results_deleted`.
3. `control_state`:
   - `reserved`/`running`/`stopping` → `resource_busy` (stage `resume`) —
     something is already using this operation; the same refusal while a
     result read, export, diff, report or delete of the operation is in
     flight (checked again right before the new start is recorded);
   - `cleanup_required`/`orphaned` → `cleanup_required` — run
     `owner.cleanup(ref)` / `lab cleanup operation <id>` first;
   - `released` with `target_cleanup.state == "manual"` (a manual
     `release_after_manual_cleanup`, so the old target's state is unknown) →
     `cleanup_unconfirmed`;
   - `finished`, or `released` with `target_cleanup.state` `confirmed` or
     `not_needed` (the previous start refused before it touched a target —
     `settle_pre_dispatch_failure` — otherwise a pre-dispatch refusal would
     close `resume` forever), are admitted; `released` with any other
     `target_cleanup.state` is refused `cleanup_unconfirmed`, like `manual`.
4. `results_state` is `not_created` (no journal was ever created) or
   `unavailable` (a journal exists but Lab's own read of it failed) →
   `campaign_unreadable`. This is a local store check, from Lab's own
   record of the run, decided before Core is asked anything. The PDR-0055 exception
   admits `not_created` with saved latest-start findings, a real existing run
   directory and confirmed cleanup. The retry keeps that directory and leaves
   its status as `not_created` until Core writes a campaign.
5. Core's own projection (`inspect_campaign(run).to_payload()`, verification
   disabled): an unreadable journal or an unsupported campaign family →
   `campaign_unreadable` (the same code as step 4, this time from core's
   read rather than Lab's); a complete campaign, or one whose `suspended` is
   not `True` (including one cut short only by the Owner process dying —
   crash recovery is out of scope, it goes through `orphaned` → `cleanup`
   instead) → `campaign_not_suspended`.
6. Target requirement (dev29): when core's last suspension is `target_unstable`,
   a `target_ref` the campaign has already used (any start's) →
   `target_unstable_fresh_required` (stage `before_start`), closed `OwnerError`
   code, HTTP 409. `OperationStatus.resume.target` carries the same
   requirement (`fresh_instance`, `excluded`) before you call. The check runs
   again inside `accept_resume`'s durable transaction against the starts
   recorded at that moment, so a start accepted since the verdict was read
   counts as used. `accept_resume` also takes `observed_start`, the start
   number the verdict was computed from; if the operation has a different
   durable start by then (a concurrent resume finished and core suspended
   again, possibly with a requirement the verdict never saw), it refuses with
   `resource_busy` (stage `resume`): re-read the status and retry. After a role
   suspension (`reflector_provider_failed`, `reflector_failing`,
   `editor_provider_failed`, `editor_failing`) or `final_incomplete` there is no target requirement:
   `resume.target` is `null` and any `ready` target of the Bundle is accepted.
7. The rest of `owner.start`'s checks, unchanged: the target must be `ready`
   on the current generation and share the `bundle_digest` (else
   `target_not_running`/`binding_mismatch`), the `execution`/`target_use`
   slot must be free (`busy`), and any unrelated unresolved scope on the
   operation is `cleanup_required`. The original policy presence and complete
   gateway block must match (`policy_continuation_mismatch`,
   `policy_gateway_changed`); routed-role manifest provider pins are refused
   before calls (`manifest_provider_pin`). See the
   [stop → confirm → start → resume workflow](../../../docs/guide/budget.md#execution-policy-and-budget-gateway).

Whether the target actually belongs to *this* campaign is decided by core at
worker start, not by Lab: an identity mismatch, a busy journal lock, or a
related-run conflict comes back as a **worker refusal**, the same path and
shape as a `start` worker refusal (for example `run_identity_changed`), not
as an `OwnerError` from `resume` itself. Lab does not compare target identity
for core and does not rewrite the manifest; the one thing it enforces is the
exclusion of used instances above (row 8).

On acceptance, the operation's `owner_generation` moves to the generation
that accepted `resume`; the new start's own `owner_generation` is that same
value, while earlier starts keep the generation that ran them (see the
`starts[]` list on `lab-operation-status/8`,
[Service and CLI reference](service-s5.md#resume)). A
`stop` during the new start, and `delete_results` afterwards, work exactly as
for the first start, but `delete_results` requires every start's scope of
the operation to be released and removes the whole run directory (all
starts) at once.

`OperationStatus.resume` (`ResumeVerdict | None`) shows whether the
operation itself is ready to continue, computed by the same rule as this
admission (`owner/resume_rules.py`): `None` when core does not show the
campaign suspended; otherwise `ready`, `cleanup_first`, `busy` or `refused`
with the refusal code for a suspended campaign, plus `target` (`null`, or the
target requirement, admission row 8). `ready` does not cover the
call-time checks (target, Bundle, secrets, slots, result leases); see
[Service and CLI reference](service-s5.md#resume-readiness).

## Current evaluation and terminal fields

Current status/list use `lab-operation-status/8` and `lab-operation-list/8`;
Owner reports use `lab-owner-report/15`. Campaign view uses
`lab-campaign-view/15`. These are distinct closed forms, not interchangeable
versions of one payload.

Each start records `initiated_by` (`human` or `auto_resume`) and an
`auto_resume_attempt` number, null for a human start. The status's `auto_resume`
block reports the policy digest, state, next time, used and maximum attempts,
blocked reason and temporary busy detail. Rules come from the last target's
confirmed execution policy and are off when its policy has no block. Automatic
continuation runs only while the Owner process holds the operation's secrets in
memory. A new Owner generation cannot recover them; a paused operation reports
`secrets_unavailable` until a manual resume supplies them again. A stop during
waiting cancels that wait; a later manual resume may enable the policy again.

`OperationResult.to_payload()` is `lab-operation-result/3`, with exactly
`{schema, kind, prepared_digest, process, core, coverage, stopped_reason,
unmeasured, min_coverage, campaign_execution, persistence, unanswered,
unanswered_state, answered_reported}`. For evaluation,
`core` is Core's public measurement projection, `coverage` is null until the
schedule is known or `{expected, scored}`, and `campaign_execution` is null.
`unmeasured` is null when unavailable or a list of
`{case_id, sample_id, outcome, tries}` (nonempty identifier/outcome strings,
integer `tries >= 1`). `min_coverage` is finite, greater than zero and at most
one. A missing optional outcome gives `stopped_reason: "unknown"`, not success.
The serialized `/1` reader retains its old `stop_reason` input and supplies
`unmeasured: null`, `min_coverage: 1.0`; the Python attribute is now
`stopped_reason` with no `stop_reason` alias. This reader compatibility does
not permit opening or migrating old user state.

The worker's saved `lab-evaluation-outcome/3` has exactly
`{schema, operation_id, scope_id, run_id, measurement_returned, coverage,
stopped_reason, evaluation_budget, unmeasured, min_coverage}`;
`evaluation_budget` is `{max_failed_turns, call_retries}`. Its `/2` reader keeps
the earlier fields and supplies unavailable `unmeasured` and threshold 1.0.
Standalone `evaluation-result.json` uses `version: "lab-evaluation-result/2"`
and `{stopped_reason, coverage, unmeasured, min_coverage}` alongside that version;
it has no Owner persistence receipt.

An unmeasured sample follows Core's retry rule for the declared
`sample_retries`, then the evaluation continues. Exhausted target-call retries
are a sample outcome. Global stops include cancellation, exhausted Lab budget,
a Core `exclude` outcome, Core failure without a closed try, and three
consecutive unmeasured samples (`consecutive_unmeasured`). Lab does not add a
campaign coverage threshold or resume fixed evaluation.

For evaluation, `ReportResult.report` is the existing report wrapper, with
`core`, `lab_result`, `measurements` and its provenance/cost fields. The open
evaluation report body also carries `lab_notices` and `lab_notices_state`, with
the same focus-signal check as the evaluation measurement below; the enclosing
`lab-owner-report/15` also contains the current start metadata.
Owner `lab_result` contains exactly `{coverage, stopped_reason, unmeasured,
min_coverage, persistence}`. A report can mark partial measurement coverage
`accepted` when expected is positive, the scored fraction meets the threshold,
there is no stop, Owner persistence is saved, and Lab coverage equals the Core
projection's coverage. Full coverage is `complete`; other partial coverage is
`incomplete`. Owner baseline admission additionally requires a finished or
released evaluation (see [baseline.measured](projects.md)). Standalone reports
legitimately lack Owner persistence and do not establish an Owner baseline fact.
Always retain “N of M” and the unmeasured list beside an accepted partial result.

`core_terminal` is null or exactly `{owner, terminal, code, hint}`: `owner` and
`terminal` are strings, `code` is a nullable string, `hint` is null or bilingual
`{ru, en}`. It copies Core's failure terminal and optional repair hint; it is
separate from Lab `failure`, Core `refusal` and campaign outcome. Each
`starts[]` entry owns its terminal, and the top level of status/report copies
the last start (null when absent). An accepted resume starts with a null
terminal while earlier starts retain theirs. Successful finishes, suspensions
and refusals have null `core_terminal`; their existing outcome/refusal fields
keep their meanings. Cleanup and the closed Lab failure table are unchanged.

## Calls and results

All four calls require an existing `ResourceRef("operation", operation_id)` in
the Owner's own state. `TaskRef` and target refs are not accepted as operation
refs. Use the Python environment and state directory that created the result.
These methods do not import runs from another state directory or version pair.

| Call | Return | Meaning |
|---|---|---|
| `await owner.diff(ref)` | `DiffResult` | Direct read; no durable diff task or ZIP. |
| `await owner.export(ref)` | `TaskRef` | Accepted Owner-held export; inspect with `task` or `wait`. |
| `await owner.read_export(ref, export_id)` | `bytes` | Local read of the operation's published ZIP, verified against its ID/hash. |
| `await owner.delete_results(ref)` | `TaskRef` | Accepted explicit deletion of that operation's recorded owned results. |

`DiffResult` has `operation_id`, `source_digest`, `selected_digest`,
`state` (`changed` or `no_change`), `text`, and `nontext_changes`. `text` is a
UTF-8 unified diff with `a/<path>`, `b/<path>`, and `/dev/null` for additions or
deletions; it preserves missing-final-newline evidence. `nontext_changes` has
`{path, kind}` rows for `binary` and `mode` changes, including mode changes
alongside changed bytes. A no-change result has empty text/nontext changes and
verified equal candidate/dependency identities. Missing or unreliable champion
evidence is `export_unavailable`, never a no-change result.

For an accepted export, `await owner.wait(export_ref)` returns `TaskStatus` with
`action="export"`. Check `state`, `failure`, and independent `persistence` before
using `result`. On success, `result.kind="export"` and its payload has exactly these fields:

```text
{operation_id, state: ready|no_change, export_id, sha256, manifest}
```

Ready has a private manifest and `export_id == sha256` for the final ZIP. No
change has null ID/hash/manifest and creates no ZIP. The small no-change task
result remains discoverable after restart and after result deletion. A ready
task retains its durable state/failure/persistence but loses its private result
payload after deletion. `task`/`wait` reconstruct a ready manifest from the
verified published archive, rather than trusting a stale cached copy; a corrupt
archive may make the later *read* raise `OwnerError` without rewriting the
original task's execution facts.

For an accepted delete, `TaskStatus.action="delete"`; its small
`DeletionResult` has `{operation_id, state: deleted|failed, failure}`. A failed
deletion has a safe `Failure` in both the deletion result and task status. A
final persistence failure is independent evidence and cannot create a false
durable deleted receipt. Inspect `owner.status(ref)` for the retained small
operation identity/outcome/process/cleanup/disposition facts, even when its
private report is gone. `OperationStatus.retained_summary` is null before the
first deletion intent. After intent, `status` and `list_operations` expose the
same immutable saved summary through deleting, delete_failed, deleted and restart:

```text
{campaign_outcome: {outcome, reason, continuation: {kind, reason}} | null,
 dispositions: [{scope_id, disposition: normal|confirmed_cleanup|manual, reason}]}
```

An unknown campaign outcome stays null. Manual disposition retains the operator's
reason; other dispositions have a null reason. Repeated deletion preserves this
snapshot. Full `result.core` and `result.campaign_execution` stay null after intent;
the summary contains no private report, manifest, inventory or remaining paths.
Process, cleanup and coverage remain separate facts in the existing status fields.

While a run exists, `campaign_outcome` in `OperationResult.core` and in reports is
core's family14 object as is. The retained summary does not preserve paths into
the run directory. The retained summary after
`delete_results` and the `lab-change-set/1` manifest keep Lab's triple
`{outcome, reason, continuation: {kind, reason}}`: Lab projects core's outcome to
these keys, because the summary must outlive private run files. A value that is not a well-formed core outcome stays
null. Neither schema changes. When `campaign_outcome` is a well-formed
outcome, `continuation` is `{kind: "linked_run", reason: <the terminal's own reason>}` for the current `/15` campaign family14; it is a fact of core's outcome object, not something Lab
offers or authorizes (PDR-0013).

**Suspended campaigns.** Family 7 replaces an infrastructure terminal with a
non-terminal `campaign_suspended` record: the campaign is not finished, and
`campaign_outcome` stays `null` while it is suspended — the same `null` as
"no terminal outcome yet". The signal to look at is separate:
`OperationResult.core["suspended"]` (`true`/`false`) and
`OperationResult.core["suspensions"]` (`seq`, `reason`, `owner`,
`evidence_kind`, most recent last). `status.control_state` still reaches
`"finished"` with `process.exit_code == 1`. Because `campaign_outcome` is
`null` while suspended, the retained triple after `delete_results` is also
`null` for that operation until the campaign resolves one way or the other.
Once the run's operation is `finished` or `released` (with confirmed target
cleanup), [`owner.resume`](#resume) continues it explicitly on a ready target
of the same Bundle; a suspended run stuck in `cleanup_required`/`orphaned`
needs `owner.cleanup` first. See [Read the
results](../../../docs/guide/results.md#suspended-campaigns).

## Reading a campaign (dev30)

Read-only calls added with the panel data layer (PDR-0017, part 1a). All take an operation
`ResourceRef` and answer from the core's public projections; they never change the run. Each has a
route in the [service reference](service-s5.md#progress-times-and-summaries) and a `lab …` command.

**Status `lab-operation-status/5`.** `OperationStatus` gains `entrypoint`, `progress` +
`progress_state` and `times` + `times_state` + `times_source` + `times_reason`
([forms and states](service-s5.md#progress-times-and-summaries)). `times_source` is `"core_raw"`
(campaign, ADR-0019 adapter) or `"core_public"` (evaluation) when `times` is present;
`times_reason` is `"core_raw_unsupported"` or `"unreadable"` exactly when `times_state` is
`unavailable`. A launched operation whose run directory is gone reads `results_state:
"unavailable"` (not `not_created`).
`owner.list_summaries(ids)` returns `lab-operation-summary/1` rows.

<a id="campaign-view"></a>
**Campaign view `lab-campaign-view/15`** (`lab campaign O`, `GET /operations/O/campaign`).
Only `/15` is served and accepted; `/14` and earlier views have no fallback. The shape (family 14; see the family-15 block below) is
`{operation_id, family, gate_mode, caveats, roles, settings, connection, policy,
progress, outcome, champions, champions_state, rounds, measurements, suspensions,
details_state, details_reason, details_source, lab_notices, lab_notices_state, reflector_probe,
reflector_probe_state, reflector_probe_reason}`. `family` is `14` or `15`.
An evaluation operation is `invalid_argument`.

The family-14 content introduced in `/7` differed from `/6` by the schema string, family14-only admission,
and removal of `rounds[].cohort` and `rounds[].attempts[].alpha_total` (even null
keys are invalid). All other family14 shapes remain:

- `roles`: `{editor: {kind}, reflector: {kind}, grouper: {kind}}`.
- `settings`: `{stability, round_stop, baseline_refresh}`; refresh is `"off"` or
  `{every_rounds, count, performed}`.
- `connection`: `{state, outcome, attempts, binding_digest, operation_id, detail}`;
  detail contains codes and paths, never values.
- Round `final` is null or a list of `{ordinal, stability, champion_side,
  stored_measurement_id, mode, global, members, contrasts, selection, listed,
  next_baseline_measurement_id, condition_changed_in_place}`. Members are
  `{measurement_id, role, attempt_id, complete, point, missing, critical}`.
  Contrasts are `{attempt_id, point, lower, level, unavailable, pairing, accepted,
  reason, exact, removed_pairs, removed_cases, condition_changed_in_place}`;
  unavailable/removed counts belong to each contrast. Selection is null or
  `{reason, attempt_id, measurement_id}`. `listed` counts unmeasured pairs.
- Attempt `look`, `confidence.margin`, `confidence.latest.focus|causal.exact`,
  `final_decision`, `measurement_id`, `has_tree` and failure remain. A finalist's
  `final_decision` is `{state: accepted|rejected|pending|unavailable, reason}`;
  null for a non-finalist. Core's final contrast determines acceptance. A finalist
  without a contrast is `unavailable` with the round's reason once Core records
  `complete: true` or a campaign outcome; it remains `pending` only while unfinished.
- `group` is a presentation of core's codes: `accepted`, `agent_worse`,
  `not_enough_data`, `infrastructure`, `no_candidate`, `stopped`, `in_progress`,
  `unknown`. A finalist follows its final decision; `focus_below_margin` depends
  on the evaluated look and point. An attempt without an outcome is `unknown`
  once Core records completion, and `in_progress` otherwise. Completion uses only
  Core's `complete` or campaign outcome, never Lab operation state. Missing evidence stays unknown.
- `champions` and `has_tree` use public lineage. Unreadable lineage gives
  `champions_state: unavailable`, `champions: null` and `has_tree: null`.
  Champion `measurement_id` follows core's final/next-baseline/refresh mapping;
  an unreadable mapping is null with details unavailable, never guessed.

**Family 15 in `/14`.** For `family: 15` (manifest `/16`) the view carries an `independent` block in place of the family-14
confidence, cohort and final blocks, a Lab projection of core's public record: `settings` (mode, repeats, verification,
stability, `fresh_parent`, `global_rule`, train cases and slots, `execution_noise`, `statistics`), `attempts[]` (round,
attempt id, outcome, reason, group, `eligible_final_score`, `comparison`, `gate_comparison`, `global_comparison`,
`preflight`, `conditional_cost`),
`attainability` (round `preflight[]` and `local_guidance`), `work` (planned, committed, remaining), `evidence` and
`next_action`. Each of the three comparison fields is nullable and has the same closed shape:
`{outcome, reason, layer, deciding_layer, reference, reference_kind, reference_measurement_id, verification, eligible,
mode, confidence_scope, selection_bias_possible, excluded_reference_slots, coverage, noise, scopes}`.
`reference_measurement_id` is a string or null, copied from that Core field without inference.
Core records `comparison` as `global_comparison` when present, otherwise `gate_comparison`; Lab copies each field
independently and does not derive or recompute a comparison. The gate level has deciding layer `focus` or `causal`;
the global level has deciding layer `global`. A finalist follows `global_comparison`; a missing global check remains
in progress in an open round and unknown in a closed round or finished campaign. A comparison has `layer` (`gate` or `global`), `reference` (`stored`, `fresh` or `unknown`, always
against the round's champion; there is no Parent reference), coverage, noise and one row per scope. A scope row carries
`rule` (`bound`, `point` or `off`), `status`, `display` (`bound`, `point`, `no_threshold` or `status`, chosen from that
scope's own rule and status, never from the campaign mode), the point estimate, threshold, confidence and the recorded
`enclosure` `{lower, upper, steps}`. Statistical quantities are `{value, exact}`: `value` is a number rounded to 6
decimals for display, `exact` is core's rational unchanged. `enclosure.lower` is the one-sided lower confidence bound L;
`enclosure.upper` brackets that computation and is not an upper bound, so no two-sided interval is shown. The view has
no promotion or manual fields. CLI, markdown and panel show both recorded levels, omitting absent levels; the
one-sided lower bound remains the only confidence bound. A b8-shaped independent block without the new fields is
unreadable (`details_state: unavailable`, `details_reason: unreadable`), while the rest of the view remains readable.
For `focus_first`, each attempt with Core-published `slots` also carries `stages`, the shared Owner projection of Core's recorded focus and
causal stages. Panel, CLI and Lab Markdown use the same states: `waiting`, `open`, `passed`, `rejected`,
`unavailable`, `not_run`, `incomplete` or `unknown`. An unavailable comparison remains `unavailable`, never
`rejected`. These are stage facts, not an additional Lab gate verdict. Slot counts stay in JSON; the human-facing
stage summaries do not report savings. Attempts without `focus_first` omit `stages`.

The current Owner envelope is `lab-owner-report/15`. Its nested `report` contains
`lab_independent`, the same projection for the report, next to `lab_independent_state`
(`present`/`unavailable`) and `lab_independent_reason` (null/`unreadable`), all three absent for
family 14. These family-15 fields were introduced with Owner report `/5`; `/8` added the
Core-terminal projection described above. One Owner report request makes one Core acquisition,
shared by the report, its status metadata and usage. Current status is `lab-operation-status/8`,
including the dev60 evaluation and Core-terminal fields; measurement forms are
`/3` since dev69, while `lab-sample/1` and `lab-suspension-evidence/1` retain their forms.

**Check modes in view `/15` and report `/15`.** Family 15 also carries the
required top-level triplet `checks`, `checks_state`, `checks_reason`, separate
from `independent`. For an admitted Core projection, `checks_state: "present"`
and `checks_reason: null`; `checks` is null when both Core maps are absent, or
`{modes: {...}, shadow_hits: {...}}`, with a missing map copied as `{}`.
Modes contain Core's non-`enforce` entries; counts are copied without recalculation.
An unreadable or rejected projection yields `checks: null`,
`checks_state: "unavailable"`, `checks_reason: "unreadable"`.
Unreadable binding publications can make `independent` unavailable while checks
remain present. The nested Owner `report` uses `lab_checks`, `lab_checks_state`
and `lab_checks_reason` with the same rules. Family 14 gets none of these keys.
CLI, Markdown and panel show modes and individual shadow-hit counts even when
independent acceptance is unavailable; counts from different checks are not
comparable and must not be summed. No check result becomes a Lab notice.

Lab does not recompute intervals, contrasts, selection or promotion. CLI and
panel accept `/15` only; the panel displays every recorded final entry. A finalist,
accepted contrast or final selection does not prove promotion: only matching
`champions[]` records with `origin: promoted_attempt` establish transitions when
`champions_state: present`. Historical transitions do not imply current champion
status. Missing lineage remains unavailable; an absent match is not a rejection.

**`lab_notices` (`/5`).** Lab's own notices beside core's outcome (PDR-0025); codes and counts only, never texts.

- A candidate is *measured* when its attempt's `group` is `accepted`, `agent_worse` or `not_enough_data` (a decision
  was taken on a measurement). `infrastructure` is not measured, even when the attempt has a `measurement_id`.
- The code `no_candidate_measured`: core has an `outcome`, no attempt of any round was measured, and no attempt
  is `in_progress` or `unknown` (Lab does not claim what it cannot tell). Otherwise this code is absent; it is absent while core has no outcome. Other notices can still be present.
- Shape: `{code, rounds, by_reason, reflection_errors?}`. `rounds` is the number of core's rounds; `by_reason` counts
  each attempt's core `reason`, and for a round without attempts the round's own core reason (e.g.
  `reflection_failed`); `reflection_errors` counts the `error_class` (else `reason`) of every round reflection in state
  `failed` or `unsettled`. `reflection_errors` is absent when `details_state` is `unavailable` (the journal was not
  read), and `{}` when it was read and no reflection failed.
- It is not a `caveat`: caveats are core's words about its result; a notice is Lab's observation. Core's `outcome`
  stays the word on how the campaign ended — a notice never replaces or recolours it.

**Gate and confidence.** `gate_mode` is `observe`, `statistical` or null when
unknown. `caveats` are `{kind, round, attempt_id}` from core. Observe carries
`observe_gate_not_enforced` and `uncalibrated_point_estimates`; it is not evidence
of quality. Attempt `confidence` is null or `{mode, repeats, margin, focus_cases,
causal_cases, calibration, latest}`; latest is null or the last evaluated look
`{look, final, decision, reason, focus, causal}`, with point/bounds/`exact`.
Unreadable local details stay unavailable/null. Readable caveats and an observe
mark survive failure of another block; unknown gate never becomes statistical.

**Reader refusals and retained facts.** All campaign reads use the exact b15
family 14 and family 15 readers. A confirmed foreign family raises core's typed
`CampaignFamilyUnsupported(actual_family, reader=14)`; missing validated creation
identity (including empty/torn creation) raises `CampaignUnreadable` with
`campaign_unreadable` before family classification. Direct report reads preserve
core exception types. A wrong exact pin is separate:
`CoreRawUnavailable("core_raw_unsupported")`.
Owner status/list/SSE retain process, cleanup and persistence facts while fresh
campaign availability is unavailable. Campaign/content reads use existing
`not_found` when no usable core exists; report is unavailable; diff/export use
`export_unavailable`; resume refuses `campaign_unreadable` at `before_start`.
Pre-creation `not_created` and deleted `results_deleted` keep their meanings.
Since schema7, a prior family14 result may survive a transient reread failure,
including after Owner restart; it does not prove fresh availability or supply a
fresh report. A confirmed unsupported family suppresses cached core and cannot
be bypassed via summary/content/diff/export/resume. Other core and programming
errors retain their types at the shared reader boundary.

<a id="suspension-evidence"></a>
## Suspension evidence

`await owner.suspensions(ResourceRef("operation", operation_id)) -> SuspensionEvidenceResult`
returns the same public `lab-suspension-evidence/1` form as
`GET /v1/operations/O/suspensions` and `lab suspensions O [--json]`:

```json
{"schema":"lab-suspension-evidence/1","operation_id":"O","state":"available","source":"core_raw","reason":null,"suspensions":[]}
```

The top-level keys are closed. Available means `reason: null` and a list,
including an empty list. Unavailable means a reason below and `suspensions: null`;
`source` is always `core_raw`. Each record has exactly `seq`, `reason`, `owner`,
`evidence_kind`, `evidence`, `rows` in journal order.

| Kind | Evidence fields | Row fields |
| --- | --- | --- |
| `fault_rows` | `count`, `rows_digest`, `truncated` | `measurement_id`, `case_id`, `sample_id`, `retry_ordinal`, `reason`, nullable `code` |
| `final_rows` | `count`, `rows_digest`, `truncated` | `measurement_id`, `case_id`, `sample_id`, `retry_ordinal`, `owner` |
| `champion_rows` | `measurement_id`, `count`, `rows_digest`, `truncated` | same as `final_rows` |
| `threshold` | `crossing`, nullable `window_start` | `rows: null` |
| `stage_record` | `seq`, nullable `operation_id`, optional `prefix` | `rows: null` |

`crossing` is exactly `{owner, kind, count, denominator, max_count, max_rate}`;
`max_rate` is a decimal string. Core returns at most 256 rows per pause. Preserve
`count`, digest and `truncated`; returned rows are causal evidence, not a full
remaining worklist. Prefix locators are text, never a file-reading capability.
Unknown reason/owner/code values in a valid form remain literal. Unsupported
kinds, keys or shapes make the whole history unavailable/`unsupported_schema`,
without partial history; independent operation/campaign reads remain usable.

The Owner holds the operation result read guard, reads durable status, and
applies deletion before checking kind, exact pin and the core adapter:

| Condition | Result |
| --- | --- |
| No durable operation | `OwnerError(not_found)`, HTTP 404 |
| `results_state: deleted` | `OwnerError(results_deleted)`, HTTP 409 |
| `deleting` / `delete_failed` | `OwnerError(not_found)`, HTTP 404; no partial results |
| Existing non-campaign OperationRef (currently evaluation), even before results | HTTP 200 unavailable/`not_campaign` |
| Wrong installed exact core | HTTP 200 unavailable/`core_raw_unsupported` |
| Valid identity of another campaign family | HTTP 200 unavailable/`campaign_family_unsupported` |
| Journal not yet created | HTTP 200 unavailable/`campaign_missing` |
| Empty/unreadable journal, invalid identity or corrupt suffix | HTTP 200 unavailable/`campaign_unreadable` |
| Unsupported evidence projection | HTTP 200 unavailable/`unsupported_schema` |

A core-validated identity and intact prefix may yield history with an incomplete
trailing record; Lab does not repair it. A corrupt suffix is refused. Unexpected
`Exception` from core journal inspection/reduction maps to `campaign_unreadable`;
projection programming errors and cancellation are not hidden by this boundary.
This nullable read does not change `Owner.campaign`'s evaluation refusal.
Only identifiers/codes and the closed fields above are disclosed: no case text,
responses, reference, diagnostics or reasoning, and no `disclose` option.
Deletion excludes cached payloads; deleted results are transport errors rather
than nullable unavailable reasons. The panel invalidates history on a newer
known suspension sequence, labels retained old data stale, and clears it on
operation changes or deletion. History never changes resume admission or promotion.

<a id="campaign-details"></a>
**Campaign details (dev33).** `/2` keeps every field of `/1` with the same meaning and adds, per round,
`baseline_version` and `reflection`, and per attempt, `failure`. They come from the core's campaign
journal (`campaign.jsonl` of the run directory), read through the ADR-0019 adapter
(`owner/core_raw.py`); no other run file is opened.

*`details_state`, `details_reason`, `details_source`.* `details_state` is `present` or `unavailable`.
`details_reason` is `null` exactly when `present`, otherwise `core_raw_unsupported` (another core than the
pinned `2.0.0b15`) or `unreadable`. `details_source` is always `"core_raw"`. When `unavailable`, every
round has `baseline_version: null` and `reflection: null`, every attempt `failure: null`, and the rest of
the form is served as in `/1` (a failed read is a state next to an empty value, never a response error).
When `present`, a `null` in these fields means "the core did not record it", not a failure. Journal rules:

- No `campaign.jsonl` and no rounds in the public projection: the window before the start, `present`
  without data. No journal but rounds exist: the journal was lost after results, `unavailable` /
  `unreadable`.
- A journal in which the core's reader finds no valid record (empty file, broken first line), or reports
  a corrupt suffix: `unavailable` / `unreadable`, whatever the rounds are.
- An incomplete tail (`incomplete_tail`) is the normal state of a live campaign: the valid prefix is
  read and `details_state` is `present`.

*`baseline_version`.* The `champion_version` of the round's last `baseline_bound` record (for example
`"v1"`); `null` until the round has bound its baseline. To compare an attempt with its round's baseline:
`lab diff O --from champion:<baseline_version> --to attempt:<attempt_id>` (an attempt without a saved
tree answers `tree_unavailable`).

*`reflection`* (per round; `null` when details are unavailable). `{state, reflection_id | null,
error_class | null, reason | null, hypotheses[]}`, derived from the core's own reducer and its diagnosis
rule for the campaign's family:

| `state` | When | Other fields |
|---|---|---|
| `present` | the core counts the reflection as a diagnosis (`reflected`) | `hypotheses` from its groups (an empty list if the core journaled none) |
| `declined` | the diagnosis is `declined` | `hypotheses: []` |
| `failed` | the diagnosis is `failed` | `error_class`, `reason` (the role's reason or `null`), `hypotheses: []` |
| `in_progress` | no diagnosis yet and the round's last reflection record is a start | `reflection_id` of that record, `hypotheses: []` |
| `unsettled` | no diagnosis and the last reflection record is finished: interrupted, cancelled with the campaign, a provider/unknown role failure, or a failure that became a suspension and was not resumed yet | `reflection_id`, `error_class`, `reason` of that record, `hypotheses: []` |
| `absent` | the core journaled no reflection for the round | `reflection_id: null`, `hypotheses: []` |

`error_class` and `reason` are non-null only for `failed` and `unsettled`. For `unsettled`, read the
view's `outcome` and `suspensions` to see what follows: after a resume the core starts a new reflection,
after a cancel it does not.

On dev61/b13, reflection `input_too_large` remains the journal's exact
`error_class` and `reason`; core's internal `unknown` classification does not
replace those codes. The existing suspension is `reflector_failing`.
Retrying the same oversized Bundle does not fix it. Authorization or editing
sealed inputs is not a remedy; use a corrected package and a new campaign under
the existing continuation rules.

Each hypothesis has exactly `index`, `root_cause`, `suggested_fix`, `affected_cases` (case ids),
`target_paths`, `counterevidence`, `regression_risks`, `why_this_layer`; the last three are `null` when
the group does not carry them. **`index` is the position of the group in the core journal record, counted
from 1. It is not the `hK` number the Editor's brief uses:** the brief reorders the groups by a mass that
the journal does not hold. The core's internal references (`evidence_ids`, `probe_refs`,
`document_ids`) and the mass are not exposed. Hypotheses belong to the round, not to an attempt: every
attempt of the round saw all of them, so there is no per-attempt hypothesis and no class of cases.

*`failure`* (per attempt). Only for a closed attempt whose `outcome` is `abandoned` or
`insufficient_evidence`; `null` for `accepted`, `rejected` and unclosed attempts (a role suspension of an
unclosed attempt is explained by `suspensions`). `{stage, role, error_class, role_reason, code}`;
`stage` and `role` follow the attempt's `reason`:

| `reason` | `stage` | `role` |
|---|---|---|
| `editor_failed`, `editor_unresponsive`, `changed_file_budget`, `written_byte_budget`, `editor_declined`, `editor_no_change` | `editor` | `editor` |
| `seal_refused` | `seal` | `null` |
| `incomplete_sample`, `scorer_metric_missing`, `infrastructure_failed`, `no_evaluable_pairs`, `no_evaluable_focus`, `no_exam_impact`, `insufficient_calibration`, `insufficient_repeats`, `focus_below_margin` | `measurement` | `null` |
| `interrupted`, `cancelled`, `condition_changed` | `lifecycle` | `null` |
| any other | `unknown` | `null` |

`error_class` and `role_reason` are the Editor's last terminal record and appear only for `editor_failed`
and `editor_unresponsive`; they are `null` for `editor_declined` and `editor_no_change`. `code` is the
core's refusal code of the attempt's closing record (for example of a refused seal). An Editor decline is
not a failure, but it also carries `failure` because it never reached measurement; its `group`
(`no_candidate`) still tells it from `stopped`. `unknown` is a guard for a newer pin, not a normal path.

**Disclosure.** The hypothesis text is shown as the core journaled it: Lab does not mask or truncate it.
It is the same text the core hands to the Editor, and the only protection against leaking private data is
the exact pinned family14 core's own disclosure/refusal checks at write time. The text
**may** still contain short fragments or a paraphrase of a case's private data;
treat it as private. Lab preserves its masking boundary.
The reader is the experiment's owner on this machine (the service on its Unix socket, the panel on loopback
under a token). The content readers below read selected role logs and scorer traces through the
pinned `core_raw` adapter; the campaign view itself still carries failure codes only.

### Sample, attempt and reflection content

These async Owner methods are read-only; `ref` is the operation reference:

```python
await owner.sample(ref, measurement_id, case_id, sample_id, disclose=False)
await owner.attempt(ref, attempt_id, disclose=False)
await owner.reflection(ref, reflection_id, disclose=False)
```

`disclose` is a boolean keyword argument. These return `lab-sample/1`, `lab-attempt/1` and
`lab-reflection/1`, respectively. Attempt and reflection require a campaign; a missing sample
pair, attempt or reflection is `not_found`. Sample identity is `(case_id, sample_id)`.
The [service reference](service-s5.md#content-reads) gives paths and identifier rules.

All three forms have `schema`, `operation_id`, `disclosure: "public" | "private"`,
`masking: "keys"`, `truncated` (boolean) and `truncation` (always a list). Raw content carries
`source: "core_raw"`: at the top level for sample/reflection, inside `editor` for attempt.
Partial read failures preserve readable sections and expose the affected section's state/reason.
The adapter is pinned to core 2.0.0b15; another distribution gives `core_raw_unsupported`.

**`lab-sample/1`.** The remaining fields are:

- `measurement_id`, `case_id`, `sample_id`, `execution_state` (from the measurement);
- `trace`: `{state: "available"}` or `{state: "unavailable", reason}`. Reasons are `not_scored`,
  `imported`, `incomplete`, `trace_missing`, `core_raw_unsupported`, `unreadable`, `too_large`, `missing`;
- `try`: `null` or `{score_request_id, retry_ordinal, tries_total}`, for the last completed scoring
  try (`finished` or `failed`). `retry_ordinal` can be `null`: standalone journals may omit it (core a34 and 2.0.0b1).
  There is no scoring-try history;
- `input`: `null` or `{public}` from the trace; `turns` (list), `turns_omitted` (count), `terminal`
  (string or `null`). Each turn carries `index`, `message: {content, fields}`, `events`, `usage`,
  `latency_ms`, `outcome`, `reason`; tool events carry `type` and applicable `text`, `name`,
  `arguments`, `result`. Turn `usage` describes target calls, not role or scorer tokens;
- `scoring`: `{state, metrics, critical_pass, verdicts, public_feedback}`. Only journal-confirmed
  `admitted` scoring exposes a metrics object, `critical_pass: bool | null`, a possibly empty
  list of verdicts `{criterion, passed}` and feedback. Text output shows a null `critical_pass` as `—`.
  For `failed` or
  `not_scored` all four result fields are `null`; a readable dialogue survives scoring failure;
- `private`: exactly `{"withheld": "private"}` without disclosure, otherwise
  `{oracle, diagnostics, details, verdict_notes}` with notes `{criterion, note}`. Failed scoring
  additionally exposes `scorer_output: {stdout, exit_status}` here; it is diagnostic text, never a
  verdict. A missing exit status is `null`.

**`lab-attempt/1`.** The remaining fields are `attempt_id`, `round`, `confidence`, `editor`.
`confidence` is the family14 public core projection (or `null` when absent):
`{method, levels, tolerance, retained, parent_source, repeats, focus_cases, causal_cases, calibration, looks}`.
`looks` contains the full evaluated history: `{look, final, decision, reason, would_pass_point, caveats,
focus, causal, overall}`; each estimate is `null` or `{point, lower, upper, level, unavailable}`.
This block comes from core's public projection, not raw files. Its content schema
stays `/1`: the campaign view's `confidence.margin` and estimate `exact` are not
added to this separate content shape by the `/7` change.

`editor` has `{source, kind, state, reason, events, events_omitted, tool_calls, stderr_tail,
changed, changed_count, failure}`. `kind` is `chat`, `agent_cli`, `subprocess` or `null` when unknown;
`state` is `available` or `unavailable`. `reason` is `null`, `not_recorded`, `incomplete`,
`core_raw_unsupported`, `unreadable`, `too_large`, `log_missing` or `missing`. An available partial
JSONL can carry `reason: "unreadable"` alongside its readable events. `changed` is a path list or
`null`; when core records only a count, `changed: null` and `changed_count` holds that count.
`failure` is `null` or `{error_class, reason}` with optional paired `text` and
`text_source: "editor_log" | "stderr"`.

**`lab-reflection/1`.** The remaining fields are `reflection_id`, `round`, `source`, `kind`,
`state`, `reason`, `events`, `events_omitted`, `tool_calls`, `stderr_tail`, `skipped`.
Role state/reason and kind have the same meanings as for the Editor. `skipped` counts unknown
role records; hypotheses remain in the campaign view and are not duplicated here.

Role events are projections (`input`, `model_message`, `tool_call`, `tool_result`, `published`
for Editor, `reflected` for Reflector, `rejected`, `failed`, `agent_summary`), in journal order,
including appended sessions after retries. Service records and role `usage` are omitted; no Editor
or scorer tokens or money are exposed. `tool_calls` is `recorded` or `not_recorded`: `agent_cli`
provides a summary rather than recorded tool calls. Subprocess roles have no saved brief/stdout;
`events: []`, `reason: "not_recorded"`, and a stderr tail when present. A retry can overwrite that
stderr file; it is not a history. `agent_cli` has unit coverage only, no real-run contract coverage.

**Disclosure and masking.** Oracle, scorer diagnostics/details/verdict notes, Reflector input/model
messages, private tool arguments/results and other private Reflector text (including subprocess stderr)
require explicit disclosure. Entire private events become `{type, withheld: "private"}` in place;
partly private events keep public fields and replace private fields with `{"withheld": "private"}`.
Masking applies after disclosure, including revealed content: credential/reasoning key names are
matched recursively, including JSON encoded inside strings, and hidden reasoning blocks are replaced
with `[REDACTED]`. `masking: "keys"` means key-based masking: Lab does **not** compare arbitrary
text with actual secret values. `[REDACTED]` already written by core is preserved.

**Character-paged role results.** In public and private disclosure, tool-result
text contains only explicitly allowed metadata: identifiers, offsets, total
lengths and content digests. Paged `content` and arbitrary extension fields are
never projected, including for a single/final page. This covers `read_trace`,
`read_evidence`, `read_document`, `read_history` and other tools returning the
same character-page structure. Both Core forms (`total_characters`/`offset`
and `total_chars`/`id`) follow this rule. Known page tools also suppress raw
unexpected or malformed result shapes. Errors from these four known readers contain only an identifier
error code (empty text if no admissible code was recorded), without arguments
or free text. Ordinary tool calls and nonpaged results retain their existing
projection. Unknown `check_shadowed` role records remain skipped. This changes
no role-event schema. Read the masked episode through the sample view.

**Scorer traces.** The adapter uses core b13's public `ScorerTraceReader` for
both directory and pack layouts. It addresses the selected measurement and
`score_request_id`, never searches all packs or falls back to an operation id.
Campaigns use the core run root; fixed evaluations use `<run>/measurement` and
the journal's run id. Journal completion and input identity/canonical SHA-256
verification remain required; reader status never replaces scoring state.

A missing record yields `trace_missing`. An unknown/missing request size is
`trace_missing` for a pack and conservatively `unreadable` for a directory,
whose metadata cannot distinguish absence from an unsafe trace. If request
input is independently read and verified but stdout is missing or unreadable,
`trace` is unavailable with `trace_missing` (pack) or `unreadable` (directory),
while input/dialogue survives, including failed scoring. A failed common
payload read preserves no partially verified input. Empty stdout is present:
failed scoring retains available empty diagnostic text; admitted scoring still
requires valid scorer JSON, so empty/invalid JSON is `unreadable` while journal
metrics remain. Existing private failed-scorer `exit_status` uses core's
returncode when known (directory layout supplies `null`). No reader location,
status, stderr payload or new public returncode field is exposed.

**Raw paths and bounded scorer reads.** Other raw readers retain descriptor-relative
`O_NOFOLLOW` traversal through the root and parents and regular-file checks.
For scorer traces, one public `ScorerTraceReader(root, run_boundary="nofollow")`
is created per request inside the typed-error handler and closed on exit.
Metadata (`sections=()`) and payload use the same reader; payload reads pass
`max_section_bytes=33554432`. The existing root precheck preserves absent/unsafe
root semantics; the strict reader supplies the no-follow run boundary.

Request/stdout metadata sizes and returned byte lengths are checked against
**32 MiB** before parsing/disclosure. Oversize in either rejects the whole trace
as `too_large`, without partial input/dialogue. Known size/returned-length mismatch
is `unreadable`. Typed `ScorerTraceUnreadable.code == "too_large"` maps to
`too_large`; other typed failures map to `unreadable`, including construction
failures. Stderr is never requested. No local pack parser/writer is introduced.

This replaces the b5 root-swap/file-growth exception with strict bounded API
reads. The section bound is not an aggregate pack-index or process-RSS cap, nor
does it make files immutable to their owner. That residual limitation is accepted
explicitly for exact Lab dev54/core b6 (separately, as for local dev53/b6 before it); any next pair, including another Lab-only
version on b6, needs its own acceptance. For exact dev56/b8 and exact dev57/b9 the user accepted it separately on 2026-10-20, and for exact dev58/b10 on 2026-10-22 (not inherited): nofollow and 32 MiB per selected section remain, with no aggregate pack-index or process-RSS cap. The user separately accepted the same residual limit for exact dev59/b11 on 2026-10-23; dev58 acceptance did not transfer, and the Core reader modules are unchanged. For exact dev60/b12 the user separately accepted this residual on 2026-10-25; b11 acceptance did not transfer. Any next pair needs its own acceptance. There is no runtime timer or automatic
old-pair shutdown.

For exact dev61/b13, the user conditionally accepted this residual on
2026-10-27 under ADR-0019. The exact-tag audit confirmed unchanged reader/storage
modules, so that condition is satisfied: nofollow and 32 MiB per selected
section remain, with no aggregate pack-index/RSS cap or owner-file immutability.
This is a separate acceptance; dev60/b12 acceptance did not transfer.
It does not authorize release, migration or state creation.

Only completed content is read, as established by public journal events: sample scoring finished/failed,
attempt closed, reflection finished/failed. Otherwise the relevant content is `unavailable / incomplete`;
file existence does not imply completion. Trace integrity failures and missing files remain explicit.

**Limits and exact section unions.** The entire serialized JSON, including metadata, is at most
**1 MiB (1,048,576 bytes)**. Text/list tiers are **16,384 characters / 200 entries → 4,096 / 100 →
1,024 / 40**. Text keeps head and tail; subprocess stderr keeps the tail. Bounded lists keep first
and last halves; `turns_omitted` / `events_omitted` count omitted entries. A role raw file over 32 MiB is
`unavailable / too_large`; scorer traces use the per-section whole-trace refusal
above. These limits are provisional pending live traces in the track E pilot;
small demo traces do not establish their suitability.

If the last tier still exceeds 1 MiB, the largest permitted sections (including their metadata size)
are replaced one at a time with **exactly** `{"state": "unavailable", "reason": "too_large"}`.
This exact object is the additional union variant only at these paths:

| Form | Sections permitting the exact fallback object |
| --- | --- |
| `lab-sample/1` | `input`, `turns`, `terminal`, `scoring`, `private` |
| `lab-attempt/1` | `confidence`, `editor.events`, `editor.changed`, `editor.failure`, `editor.stderr_tail` |
| `lab-reflection/1` | `events`, `stderr_tail` |

Identifiers, disclosure/masking/source metadata and sections that fit remain. `truncated` is `true`.
For a public sample, `private` stays **exactly** `{"withheld": "private"}`, even on overflow.
Arbitrary user fields named `state` are not interpreted as the fallback object.

`truncation: [{path, size}]` is always present (`[]` when no emitted text was clipped). Each entry
points to one clipped string in the emitted response; `path` is a JSON Pointer (`~` → `~0`, `/` →
`~1`), and `size` is its character count **after disclosure and masking, before clipping**. Values
keep their types. Hidden fields and discarded list entries never appear in metadata. Replacing a
section with the fallback object removes entries for that section and its descendants. List omission
or section fallback also sets `truncated: true`, even if no text metadata remains.

<a id="measurements"></a>
**Measurements** (`lab measurements O [M]`): `lab-measurement-list/4` (summaries) and
`lab-measurement/4` (one, with `samples[]`; the id may end in `@e<k>`, an epoch — grammar in the
[service reference](service-s5.md)). A summary has `measurement_id`, `role`
(`baseline`, `attempt`, `evaluation`, or `other` for a measurement no round references — defensive),
`round`, `attempt_id`, `candidate_digest`, `coverage {expected, scored, excluded_pairs}`,
`metrics {name: {mean, count}}`, `baseline` (`null` for a baseline itself), `times`, `usage` +
`usage_state`, `retries`, `call_failures_by_code`, `repairs`, `classification`,
`lab_notices`, `lab_notices_state`, `unanswered`, `unanswered_state`, and
`answered_reported`. A `present` count covers all scored samples; `partial` counts
only the `answered_reported` samples. `not_reported` uses a null count and zero
reported; `unavailable` uses null for both. These are read from scorer verdicts,
do not change coverage or campaign outcomes, and omit unscored samples. Real excerpt (baseline of the
demo run, corpus of 6 samples). An attempt's `baseline.state` is `unavailable` (`delta`/`cases` null, samples `comparison: "unknown"`) when the primary metric is unknown (campaign policy unread) or the baseline measurement is missing. Facts (times, usage) are read per measurement: one unreadable journal leaves only that row with `times: null` and `usage_state: "unavailable"`; the operation's `times_state` is then `unavailable`/`unreadable` (aggregate incomplete):

```json
{"measurement_id": "m-r00-champion", "role": "baseline", "round": 1, "attempt_id": null,
 "coverage": {"expected": 6, "scored": 6, "excluded_pairs": 0}, "metrics": {"score": {"mean": 0.0, "count": 6}},
 "baseline": null, "times": {"started_at": "2026-09-29T14:53:35.771740Z", "last_event_at": "2026-09-29T14:53:36.076200Z"},
 "usage": {"input_tokens": 0, "output_tokens": 0, "unknown_calls": 6, "coverage": "target_calls_only"},
 "usage_state": "present", "lab_notices": [], "lab_notices_state": "present",
 "unanswered": null, "unanswered_state": "not_reported", "answered_reported": 0}
```

- `lab_notices` is a list of Lab codes and counters, separate from Core's result.
  A terminal fixed evaluation with at least one admitted sample carries
  `{"code": "scorer_without_focus_signal", "admitted": N, "not_completed": K}`
  only when every admitted sample has `critical_pass: null` and empty verdicts.
  `admitted` counts admitted samples; `not_completed` counts evaluation samples whose
  execution outcome is not `completed`. Metrics alone do not build campaign focus;
  execution failures can still build it. This notice describes the observed evaluation,
  not future campaign rounds. Campaign measurement rows have an empty notice list.
- `lab_notices_state` is `present` when the check is complete, including an empty
  list when a signal exists, the evaluation is nonterminal, or no sample is admitted.
  It is `unavailable` with an empty list if any admitted sample's scorer output cannot
  be read/parsed or its journal-confirmed `critical_pass` is unavailable. CLI and
  `report.md` then explicitly say the focus-signal check is unavailable. Unavailable
  checks are not cached. The evaluation report repeats the same fields and counters.
- `repairs` (since dev36, core 0.3.0a24): `{tries, pairs_scored}`, for example `{tries: 3, pairs_scored: 1}`.
  `tries` is the number of repair tries started in the measurement (`repair: true`), cumulatively over all
  resumes, both after a fault row and on a threshold suspension (core #78); `pairs_scored` is the number of
  unique pairs `(case, sample)` that had a repair try whose last row was scored, so three tries of one pair
  with the last one scored read `{tries: 3, pairs_scored: 1}` and `pairs_scored <= tries`. Both counters are
  informational: neither changes the statistics or the gate's decision, and they are not the count of
  successful tries. The value is `null` when the core's entry lacks either key (campaign family below 7) and
  always for a fixed evaluation (`role: "evaluation"`). Lab copies the core's projection and recomputes nothing.
- `baseline` of an attempt: `{measurement_id, state, delta {metric: {mean, pairs}}, cases {better,
  same, worse, unpaired}}`, computed over the pairs `(case_id, sample_id)` present on both sides,
  minus excluded pairs. It is a view, not the gate's decision. `state` is `present`,
  `exclusions_truncated` (the core lists at most 256 excluded pairs; Lab then leaves `delta` and
  `cases` `null` and `comparison` `unknown` rather than guess) or `unavailable`. A pair whose primary
  metric is missing on either side counts as `unpaired` in `cases` while the sample's `comparison`
  stays `unknown`.
- Tokens are **only** the target's calls (`target_send_finished.usage`), `coverage:
  "target_calls_only"`; a call without numbers is counted in `unknown_calls`. There are no money
  figures and `cost` stays `null`; editor, scorer and reflector tokens are not included.
- Refusals: results deleted -> `results_deleted`; `deleting` or `delete_failed` -> `not_found`
  (the report answers `io_error` for `delete_failed` — a difference to keep in mind). The evaluation
  measurement id is the core's `c-<operation id>`.
- Summaries are cached in memory by `(operation_id, run_id, measurement_id, last_sequence)`; a
  closed measurement is not re-read and deleting the results drops the entries.

**Report additions introduced in `lab-owner-report/3` (dev30; current `/15`).**
The envelope gains `usage`: one row per core measurement
`{measurement_id, usage, usage_state}`; `usage` is `null` with `usage_state: "unavailable"` when the
journal could not be read. The report body's `owner_resume_target` gains `ready_target`
(`{id, owner_generation}` or `null`): a ready target of the same Bundle that the campaign has not
used. The body of a campaign report also gained additive fields at that time
(without changing the then-current `/3`): `lab_rounds`,
one `{round, reflection: {state, error_class, reason}, source: "core_raw"}` per core round (or `null` when the round details were not read);
next to it `lab_rounds_state` (`present` or `unavailable`) and `lab_rounds_reason` (`null`, or the code of the failed
read — `unreadable` when the round details could not be read), so a failed read is an explicit state, not a bare
`null` (ADR-0019 p.3; `report.md` then says *Подробности рефлексии недоступны (<reason>)*);
and `lab_notices`, the same list as in the campaign view `/15`; all of them hold codes and counts only, never texts.
The report body is not a closed form.

**`report_markdown` — the report as `report.md`.** `await owner.report_markdown(ref)` returns
`ReportMarkdownResult(operation_id, state, markdown, reason)`: `state` `available` with the text `Report.markdown()`
builds (`markdown` is a `str`), or `unavailable` with `markdown: None` and the reasons of `report()` —
`results_deleted`; `io_error` (results `deleting` or `delete_failed`, or an IO failure while reading); `unavailable`
(the journal could not be read). A campaign's text comes from the same `Report` as the JSON answer of `report()`
(`read_campaign_report` with Owner's resume target); an evaluation's from `read_evaluation_report` on the measurement
directory. It is built on every call under the same result read lease and is not stored; equal inputs give the same
bytes (a suspended campaign's text follows the live target inventory, as the JSON report does). Owner passes no cost,
so the money line reads *Расход: неизвестен*.

**Diff `lab-operation-diff/2`** (`lab diff O [--from R --to R]`): the change between any two saved
trees of the campaign. `R` is `initial`, `champion:vN` or `attempt:<attempt_id>`; without
parameters it is `initial` to the final champion, as before. The answer adds `from` and `to` to the
fields listed under [Calls and results](#calls-and-results) (the earlier answer had no `schema`
field and is treated as `/1`). An attempt without a saved tree (for example a cancelled one) is
`tree_unavailable` (HTTP 404); a malformed ref is `invalid_argument` (400); an unknown champion or
attempt is `not_found`.

**Targets `lab-owner-target-list/1`** (`lab agent list [--bundle D] [--ready]`,
`GET /targets[?bundle=&state=ready]`): rows are `lab-owner-target/2` with no `client_ref`. When the
resume requirement is `fresh_instance`, `lab status` in text mode names a ready fresh target in
its `continuation:` line if one exists, and otherwise gives the earlier `lab agent start` advice.

One target lives per Owner state. `start_target` reuses a `ready` target of the same Bundle, launch, secrets and
project and execution policy; a `ready` target of another Bundle, launch or project refuses `target_slot_held` (dev46), the same Bundle with
other secrets or another execution-policy digest `binding_mismatch`.

## Handling errors and interrupted tasks

`diff` and `read_export` are direct reads: a refusal raises `OwnerError` with
safe fixed `code` and `stage` (`diff` or `read`). `export` and `delete_results`
raise `OwnerError` before acceptance (`export` or `delete` stage); after they
return a `TaskRef`, failures appear in `TaskStatus.failure` and, separately,
`TaskStatus.persistence`. An unknown exception is mapped to safe `lab_error`.
Cancelling a caller's accepted wait does not cancel Owner-held file IO. The
Owner holds its read/write guard through that IO, and close drains accepted
file tasks before closing the state store.

| Condition | Diff/export/read | Delete |
|---|---|---|
| Bad operation ref | `invalid_argument` | `invalid_argument` |
| Malformed `read_export` ID | `invalid_argument` for read | — |
| Unknown operation | `not_found` | `not_found` |
| Unknown scoped archive ID | `not_found` for read | — |
| Active operation or competing result IO | `resource_busy` | `resource_busy` |
| Orphan without verified release | `cleanup_required` for diff/export | `cleanup_required` |
| Fixed evaluation or missing/invalid final champion | `export_unavailable` for diff/export | Eligible once released |
| Deleted result | `results_deleted` | Repeat returns the same deleted meaning without unlinking again |
| Interrupted/failed deletion | `export_unavailable` for new diff/export/read | Explicit repeat can resume the recorded inventory |

`read_export` also rejects a mismatched/unknown scoped ID and unavailable or
corrupt archive. A second export while the first export task of the same
operation is active gets `resource_busy`. After a successful publication,
explicit repeat export verifies and reuses the published archive and hash ID
only within the same start: after `resume` the operation's earlier archives no
longer answer a new export (a new one is built from the current start), though
`read_export` by an earlier export ID still returns that archive until results
are deleted.
An interrupted export task is not automatically resumed; its durable unknown
task evidence remains, and an explicit repeat is the recovery path for a lost
reply. An interrupted delete remains `deleting` or `delete_failed`; no restart
automatically deletes files. An explicit repeat uses the same recorded inventory.

Diff/export require completed operation control and no live finalizer. A
manual release is an operator assertion that external cleanup already happened;
it permits access to a saved result but leaves machine cleanup evidence
unknown. It is not a process kill, campaign-success proof, or reason to read a
tree that a process is still writing. Delete additionally requires a released
operation scope and no reservations for that operation. A live target may serve
another operation without blocking deletion of the released earlier one.

## `lab-change-set/1` ZIP

An archive contains canonical `manifest.json` and opaque numbered
`payload/<index>` entries for added/modified files. The manifest records
operation/prepared/Bundle identities, saved producing distribution pins with
exact archive hashes, source/selected candidate digests, selected version,
known `campaign_outcome` or null (see the outcome note above), and separate
`campaign_complete` and `measurement_complete` booleans. Changes are ordered by
normalized relative POSIX path. Each row has `path`, `kind` (`add`, `modify`, `delete`, `mode`),
before/after SHA-256 and executable values, and a payload name only when bytes
for add/modify are present. An absent side has null hash/executable. A byte and
mode change on one path is one `modify` row.

Only included writable artifact files are represented. Ordinary parent
directories follow from those file rows; changed empty directories and
file-to-directory replacements cannot be represented and are refused with
`export_unavailable`. Executable is normalized integer `0` or `1` (any execute
bit is `1`); group/user/other POSIX permission bits are not preserved. The ZIP
does not carry the dependency tree, original project, virtual environment,
runtime secrets, or a patch-apply instruction. The published archive is
deterministic for the same verified manifest and bytes, and its ID is its
SHA-256. Presence of a ZIP, unknown outcome, or incomplete measurement says
nothing by itself about improvement; Lab does not derive a cost or treat absent
cost evidence as zero.

The ZIP, manifest, and payloads can be private. The caller chooses and writes
any external copy; Lab's `read_export` returns the complete archive as one
`bytes` value and provides no streaming or arbitrary-path overwrite API.
Snapshot/diff/export construction also holds content in memory. Plan capacity
for the whole artifact/archive and protect any copied bytes. Deleting results
removes the operation's exact owned result tree, including published exports and
staging, and its listed scope log/result files. Owner keeps the id of each published export as a
deleted mark (`Owner.cycle_starts` reports it with `export_deleted: true`); `read_export` and export
tasks never return a marked archive. Deletion leaves immutable
Bundle/PreparedExecution, target snapshots, control receipts, and external
copies alone. New reads after successful deletion refuse private results; the
small historical task and operation facts remain.

This reference follows dev65 development source on exact
`evo-skills==2.0.0b15` (Core archive SHA-256
`56855139bdac1750c28d7ec837e28d5b0c005ad2745b856de9d93c4934ef5352`).
Record the source revision and exact Lab wheel hash; guide edits change its
bytes. Owner SQLite schema10 adds confirmed policies and per-start policy
history and refuses v9/earlier with `unsupported_schema`, without migration.
A fresh private pair state and separate dev65/b15 acceptance are required;
this page does not create state or imply release/consumer migration. Keep old
runs, resume and cleanup with their producing wheels and state. Historical
reader acceptance notes above do not transfer acceptance to this pair.

The [local service](service-s5.md) exposes these actions to
CLI clients. Retention policies, automatic recovery and state migration are
outside this API.

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

### Baseline provenance notice (campaign view /15 and Owner report /15)

`lab_notices_state` is `present` when the baseline provenance check completed, or
`unavailable` when its evidence could not be read. A `baseline_provenance_unknown`
notice contains `round`, `measurement_id`, and `samples`. It identifies the latest completed baseline when it has admitted samples and no stored
provenance for any sample in its cited journal prefix, and the manifest requests
reported provenance. The target may not have
reported provenance, or may not have reported it on every turn. Core then treats
all cases as affected and runs the causal check on all train for each attempt.
Partial provenance and baselines with no admitted samples do not produce this notice.

The `/8` resume projection remains suspension-only for compatibility. A saved Core refusal may be continued explicitly through `Owner.resume` after cleanup even when `resume` is null; inspect the separate latest-start `Owner.refusal` record. It does not claim that Core suspended the campaign.

## Private campaign trace export

`await owner.trace(ref, out=Path(...), secrets={...}, disclose_private=True, measurements=(), max_field_bytes=65536)` returns Core's public `TraceSummary`. It delegates the trace file to public `evolution.tree.export_run`, with the saved Bundle manifest and explicit secret map. The export includes private case evidence and requires `disclose_private=True`. Fixed evaluation operations refuse with `trace_not_a_campaign`; they keep their existing sample reads.

The existing result read lease blocks deletion while the export thread runs, including after caller cancellation. Core performs independent manifest/corpus authentication and writes the same owner-only trace bytes as its CLI. The caller chooses a new file outside the Core run; `out='-'` is not admitted. Export failures use the existing bounded `export_unavailable` error. The separate service projection is `lab-trace-summary/1`.
