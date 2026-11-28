# Legacy runner: `recipe` → `runner` → `report`

**Who this is for.** The legacy runner is the original programmatic path for
running a native Evolution campaign from Python: `recipe` validates the inputs,
`runner` starts or resumes the campaign under a Lab supervisor, and `report`
reads the results. Read this page if you maintain code that already calls these
modules or need the exact start, resume and stop semantics; if you are new to
Lab, start with the user guide page [Running a campaign](../../../docs/guide/campaign.md).
New integrations should use the [Python Owner](owner-s4.md) or the
[service and CLI](service-s5.md) instead. The path is supported within the
current exact Lab and Core wheel pair.

The legacy programmatic path keeps its signatures: `recipe.prepare_inputs`
validates the native manifest `/15`, the corpus, the seed, the roles, the grouping
and the exact installed wheels; a manifest with `linked_from` is refused
(`linked_run_unsupported`), and so is a `/15` manifest whose `grouper.kind` is `agent_cli`
(`recipe.GrouperKindUnsupported`, a `ValueError` with `code` `grouper_kind_unsupported` and the message "Lab
supports grouper kinds fields, command, chat (grouper.kind)."), both before anything is pinned or started;
`runner.run_native` starts the Evolution CLI with a
mandatory `dispatch_guard`; `report.read_campaign_report` reads the core results.
V5 compatibility means legacy calls within the producing pair and no mandatory
`env` in the old signatures; it does not mean automatic conversion of `/3` or
cross-version reading of old journals. The local current pair dev55 / exact b8 reads family 14 and 15 through the Owner;
the legacy runner itself takes manifest `/15` only. Old runs, resume and cleanup stay with their producing wheels and state;
there is no migration or copying run folders into `~/lab-state/dev55-b8`.
Owner schema7 refuses v6/dev49; PreparationStore and project schemas are unchanged.
Direct `read_campaign_report` preserves typed core exceptions: confirmed foreign
family is `CampaignFamilyUnsupported`, missing validated identity is
`CampaignUnreadable` (`campaign_unreadable`). Empty/torn creation is unreadable,
not proof of another family. Wrong exact pin is separately
`CoreRawUnavailable("core_raw_unsupported")`. Legacy resume maps unsupported
family to `campaign_family_unsupported` and unreadable to `campaign_unreadable`,
with no dispatch; producing-pair/locks/liveness checks remain first.

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

`runner.run_native` only starts a new campaign. A campaign that was cut short by
the death of its process is continued by an explicit `runner.resume_native` call
in the same run folder. Before starting any process, it refuses with
`ResumeRefused` and a closed code when: the folder is not a Lab campaign, the
Lab/core pair is not pinned or differs, the previous start is still alive, the
journal is missing or unreadable, or core reports `complete is True`.
A suspended campaign is not treated as completed.
A folder counts
as a Lab campaign if its `run_native` created `<run>-lab-logs/supervisor.lock`.
Whether it is the same campaign is decided by core: a core refusal is exit 2,
recorded as `core_refused` in `start-<n>/process-result.json`
(`lab-process-result/2`). `kill_on_caller_exit=True` hard-kills the process tree
when the caller dies, so that the campaign is left cut short rather than stopped.
A cooperative stop and `BudgetExhausted` are final; after a forced stop,
continuation is possible but not guaranteed, and the core projection decides. On
core a17 and a21 (campaign families 7 and 10), core retries an episode that was cut short by
process death, spending one `sample_retries` attempt; once the attempts are
exhausted, the sample becomes indeterminate. The report takes `lab_result` from
the last start that has a result (`lab_starts[-1].process_result` shows whether
the last start has one) and shows `lab_starts`. On exact core a17 (and a21, where the
resume test still records the defect; the symptom below was read on a17), continuation
does not work if the process died in the middle of an open measurement: every
attempt ends with `persistence_failed` (exit 1,
`measurement_journal`/`journal_unavailable`). It does work if no measurement was
open at the moment of death (for example, right after the baseline). With
`policy.parallel_attempts > 1`, an edit or a reflection can run at the same time
as an open measurement of another attempt, so death during an edit or a
reflection does not guarantee that no measurement was open.
A fix has been requested from core: request on the recovery order after a
measurement failure;
after a new pin, Lab will lift the limitation. This `resume_native` behaviour
is unchanged in this version. The Owner, the service and the CLI have a
separate, explicit continuation path instead of a same-folder resume: once an
operation is `finished` or `released` with confirmed target cleanup,
`Owner.resume(...)` / `lab resume <id> --target … --generation …` opens a new
start on any `ready` target of the same Bundle, in the same generation or a
later one ([Owner API reference](owner-s4.md#resume)). H1 still has no
resume/reattach.

**Suspended campaigns.** Family 7 leaves an infrastructure failure inside a
rollout as a non-terminal `campaign_suspended` record instead of closing the
campaign: `complete` stays `false`. `_campaign_state` only refuses
`resume_native` when the campaign is finished (`complete: true`), so a
suspended campaign is not refused: fix whatever made the target's calls fail,
then call `resume_native` in the same run folder to continue it, no code
change or new authorization needed. `report.read_campaign_report` shows the
suspension the same way as elsewhere: `core["suspended"]`,
`core["suspensions"]`; see [Read the
results](../../../docs/guide/results.md#suspended-campaigns).

## Standalone evaluation

For a standalone evaluation, `standalone.evaluate_target`,
`evaluation.evaluate_fixed` and `report.read_evaluation_report` are available.
The shared modules do not define domain metrics or roles.
Both evaluation functions return `unanswered`, `unanswered_state`, and
`answered_reported` alongside coverage. These count admitted samples using the
scorer's `answered` verdict, with explicit partial, unreported, and unavailable
states. The saved `lab-evaluation-result/2` receipt retains its existing shape.

## Supervisor and stopping

Native `run_native` requires Linux with `/proc` and child-subreaper support.
It gives core a cooperative stop window derived from the pinned manifest
(editor + rollout + scorer + target I/O timeouts and cleanup margin).
An explicit positive `grace_seconds` can shorten that window; after it expires,
a separate supervisor kills and reaps its owned descendants, including detached
scorer/editor sessions. This does not stop an external target or gateway.
A forced stop need not produce a terminal core journal. Lab records the process
outcome beside the run in `<run>-lab-logs/process-result.json`; evaluation records
`evaluation-result.json`. Reports show these diagnostics without changing core outcomes.
The native launcher verifies producing wheels in its isolated `python -I` context.
