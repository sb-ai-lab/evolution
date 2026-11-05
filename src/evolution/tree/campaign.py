"""The campaign executor: `run_campaign`, `resume_campaign` and the linked run.

This is the thin asynchronous driver spec §2 selected: it owns no campaign state,
assembles a read-only `CampaignView` after every durable step, asks the pure
`next_actions` what to do next, and performs that one action through the S2 store,
the R1 rollout protocols and the v2 `CandidateEditor`. Every barrier, look, alpha
spend, selection and round outcome is a record in `campaign.jsonl`; resume is
replay of the same three logs into the same view, so a resumed campaign cannot
take a step the original would not have taken (spec §11 step 7).

The campaign invariants in this module amend the
task brief and spec:

1. Both entry points take `_hooks: Mapping[str, Callable[[], None]] | None`. The
   campaign seams — `after_campaign_created`, `after_identity_rebased`,
   `after_baseline_bound`, `after_measurement_bound`,
   `after_editor_finished`, `after_look_evaluated`, `after_selection_decided`,
   `after_round_closed` — fire with no arguments right after the named durable
   step. Every other name is forwarded to `ArtifactStore.create` / `.resume`, so
   S2's own `after_store_created`, `after_seal_finished`, `after_pending` and
   `after_committed` seams reach the store unchanged. A hook may raise: the
   executor cancels its tasks, closes its handles and re-raises, which is how the
   acceptance fixtures simulate a crash at an exact durable boundary.
2. The campaign log is `AppendLog.open(run_root, file_name="campaign.jsonl",
   lock_name=".campaign.lock", …, require_empty=False)` — the run root is shared
   with S2 and S1 — and `run_campaign` itself refuses a run root already holding
   `campaign.jsonl`, `artifacts.jsonl` or `measurements/` (`CampaignExists`,
   `run_root_not_empty`).
3. Declare, then create (spec §4): `campaign_created` carries the S2 identity
   computed from `scan_tree(seed_root, artifact_policy)` and is durable before
   `ArtifactStore.create(..., expected=…)` is called; `measurement_bound` is
   durable before `Journal.create`.
4. `instance_attested` (the 22nd record) is written right after a stage whose
   `StageOutcome.attested` names an instance the campaign journal does not yet
   carry, and carries the whole attested `TargetIdentity` as `identity`.
   Promotion admission (spec §8) runs R3's own `verify_attestation` over each of
   the winner's rows — the required attested set and both digests — against the
   sealed Candidate's `expected_attestation()`; no attested instance, an
   unparseable identity, or any refusal from the verifier is
   `campaign_finished(infrastructure_failed, attestation_mismatch, none)` with
   nothing of the selection written.

   Which of the two digests a live target can actually move, and what stops it:

   * `loaded_candidate_digest` — R1 refuses the instance at materialization
     (`verify_attestation`), and `run_stage` turns that `AttestationError` into
     the same `attestation_mismatch` terminal (ruling 13). Even with R1's check
     stood down the campaign lifecycle refuses the record, because
     `CampaignLifecycle._instance_attested` compares it with the measurement's
     own `candidate_digest`; the row never becomes durable.
   * `dependency_digest` — the lifecycle does **not** compare it, so the row is
     durable. R1's *per-sample* identity check is the guard that fires: every
     sample of that instance fails `target_identity`, so the stage produces only
     `rollout_failed(infrastructure_failed)` rows and the attempt closes
     `insufficient_evidence(infrastructure_failed)` long before a selection. No
     live target therefore reaches `admitted()`'s dependency half; it holds for a
     journal some other writer produced, which is what proves it.

   So in a live run `admitted()` decides exactly one reachable case — a winner
   whose measurement attested no instance at all. The verifier still runs over
   every durable identity, because the attested-set half has no earlier guard at
   all and a journal another writer produced is exactly what admission is for.
5. `decision_ref` is the `journal_record` reference of the `selection_decided`
   record just appended — the append log's byte count before and after the append
   gives the range, and the record's own encoded bytes give the digest. Resume
   rebuilds the identical reference from the durable record, so S2 recognises the
   same decision (spec §8).
6. Reconciliation order on resume (spec §11 step 6): a dangling `alpha_consumed`
   is written **first**, because the lifecycle requires it immediately after its
   `look_evaluated`; then the interrupted samples of every resumed measurement are
   accounted; only then are the attempts reconciled.
7. An S1 sample whose execution is terminal but whose scoring never reached one
   (`disposition == "unscored"`) is classified at resume as an interrupted sample:
   `indeterminate_observed(owner="rollout", reason="rollout_interrupted")`, which
   takes the ordinary baseline-incomplete / attempt-insufficient path.
   `blocked_rows` merges those with the R2a `indeterminate` terminals in schedule
   order, so §6 step 1's "first such sample's owner" names the real first sample.
8. A measurement whose `resume` reported leaked instance ids and which still has
   rows to run can never be dispatched again — S1 will not leave recovery while
   it still owns an instance, so its journal refuses every further append with
   `JournalRecoveryRequired("journal_busy")`. C1 decides nothing of its own about
   it: the next dispatch touches that journal, `_boundary` classifies the refusal
   like any other, and §7's own sentence about a recovery refusal ("surfaces as
   the same `persistence_failed` with the log's code") is the terminal —
   `persistence_failed {measurement_journal, journal_busy}` (amendment 20). A
   *complete* measurement with a leaked instance is merely `leaked` — the status
   projection carries it as the S1 `owned_instances` of that measurement — and the
   campaign continues; that projection is also where the leak behind the
   persistence terminal stays visible.
9. Persistence faults (spec §7): only `JournalFault(code =
   "storage_capacity_exhausted")` and *every* `ArtifactCapacityError` — both S2
   codes, the preflight `storage_capacity_insufficient` included — are
   `storage_capacity_exhausted`; every other journal boundary error, `ArtifactError`
   or corruption is `persistence_failed {owner, code}`. `_classify` is the one
   place that decides, and `_boundary(owner, measurement)` wraps every S1/S2/R1
   call so no boundary error escapes unclassified or with the wrong owner. A
   typed S2 *admission* refusal is not a fault and travels unchanged. Neither
   terminal cancels running work — the executor stops opening stages and attempts
   and drains; it never cancels a task itself, since §7 forbids cancelling a
   sample and an editor is cancelled cooperatively inside `run_editor`.
   `capacity_exhausted` is written only for the capacity terminal, only while the
   campaign log still appends, and it names the faulting measurement journal.
10. A record the campaign lifecycle refuses is an executor bug, never a storage
   failure. Every append is proved through the lifecycle's own `prepare` first
   (`_Campaign.prove`); a refusal raises `CampaignInvariantViolated(rule)` with the
   log untouched and still healthy, instead of letting `AppendLog.append` poison
   the writer and report `persistence_failed`.
11. Spec §11: `stage_opened` is durable before any of its samples runs, so an
   interruption can leave a stage opened but only partly dispatched. The planner
   sees that through the measurement's S1 status (`stage_progress`) and yields the
   *same* stage again; `run_stage` writes no second `stage_opened`, dispatches only
   the rows S1 has no terminal for, and reads the stage's consequences over the
   full plan.
12. On resume a target that can no longer serve the policy raises
   `CapabilitiesInsufficient`, and PDR-0010 lets it travel out of `adopt` as
   itself: it is a hard refusal, because no operator decision makes an
   oversubscribed policy runnable. A shrink the policy still fits is not a
   capability refusal at all — `admit_capabilities` admits it and it surfaces as
   an `identity.capabilities.*` difference, which an authorization may accept
   (ADR-0015).
13. An `AttestationError` from a stage — R1 refusing an instance at
   materialization, releasing it and starting no sample — is a campaign-level
   fault of the attestation kind: neither a persistence fault (ruling 9) nor an
   executor bug (ruling 10). `run_stage` catches it from any stage (baseline,
   winner completion or attempt) and stops with
   `campaign_finished(infrastructure_failed, attestation_mismatch, none)`
   carrying the measurement's prefix as evidence, after the ordinary §7 barrier.
   It is the second cause of that terminal, beside promotion admission (ruling 4).
   ADR-0008 requires every opened stage to run to completion, so a **first** open
   materializes and attests before `stage_opened` is appended and a refusal there
   opens no stage. A **resumed**, already-open stage re-materializes inside
   `Measurement.run_stage` — its `stage_opened` is durable and
   `Measurement.resume` cleared the instances — so a target that starts lying
   between two sessions cannot dispatch that stage's remaining rows;
   `Measurement.refuse_rows` gives each of them S1's own target-owned
   indeterminate terminal (`rollout_finished(target_failed)`), so the stage runs
   to completion, its rows are accounted like any other indeterminate, and the
   campaign still stops at this terminal (spec §11, amendment 10). A family-12
   component-drift refusal (`ComponentsChanged`) of such a stage is the
   exception (PDR-0020, ADR-0008 amended 2026-10-02): its rows get no terminal
   and the stage is not finished, so nothing closes the Attempt; the campaign
   suspends `target_components_changed` and the stage completes on the resume
   after repair (`leave_drifted_stage_open`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, cast
from uuid import uuid4

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.affected_cases import (
    BaselineUnavailable,
    baseline_inputs,
    cited_baseline_prefix,
    parent_mode,
    select_affected,
    tree_changes,
)
from evolution.tree.affected_report import read_affected_items
from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCapacityError,
    ArtifactCorruptionError,
    ArtifactStoreStopped,
    ArtifactUnsafeError,
)
from evolution.tree.artifacts.artifact_ledger import LEDGER_FILE, ArtifactLedger
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, changes, scan_tree
from evolution.tree.artifacts.artifact_projections import CANDIDATE_LOCATOR, CHAMPION_LOCATOR
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import (
    ArtifactRef,
    ArtifactStore,
    CapacityEstimate,
    ExpectedAttestation,
    SelectionDecision,
)
from evolution.tree.artifacts.attestation import (
    ComponentsChanged,
    PromotionCapableAdapter,
    verify_attestation,
)
from evolution.tree.baseline_import import (
    BaselineImport,
    BaselineSourceUnavailable,
    FrozenSourceCorrupt,
    SourceMeasurement,
    admit_rows,
    carried_traces,
    fields_between,
    import_pairs_digest,
    read_frozen_source,
    read_source,
)
from evolution.tree.campaign_caveats import aggregate_lineage
from evolution.tree.campaign_editor import build_brief, run_editor, unchanged
from evolution.tree.campaign_epochs import (
    admissible_effects,
    base_measurement_id,
    epoch_identity,
    measurement_id_for_epoch,
    policy_contradiction,
    rounds_begun,
    rounds_remaining,
    visibility_forces_close,
)
from evolution.tree.campaign_identity import (
    PARTITION_PROFILES,
    AdaptationIdentityV1,
    CampaignIdentityV1,
    LinkedFrom,
    _link_context,
    adaptation_identity_slot,
    admit_capabilities,
    build_identity,
    final_measurement_id_for,
    measurement_id_for,
    measurement_identity,
    observe_case_repeats,
    parent_measurement_id_for,
    probe_continues,
    probe_identity_payload,
    refresh_measurement_id_for,
)
from evolution.tree.campaign_inspection import (
    INSPECTIONS_DIR,
    InspectionFailed,
    inspect_candidate,
    inspection_born,
    inspection_path,
    release_leftover,
)
from evolution.tree.campaign_measurement import (
    MEASUREMENTS_DIR,
    FrozenMeasurement,
    Measurement,
    MeasurementError,
    StageDispatch,
    StageOutcome,
    StagePlan,
    _partition,
    _partition_by_scope,
    run_interleaved,
    stage_plans,
)
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    CampaignPolicyV12,
    GateMode,
    admit_reflection_capacity,
    comparable_policy_payload,
    decides_at_once,
    policy_class_for_payload,
)
from evolution.tree.campaign_projections import (
    CampaignProjection,
    CampaignUnreadable,
    ProjectionInvalid,
    project_result,
    project_status,
    read_measurement_tries,
)
from evolution.tree.campaign_reflector import run_reflector
from evolution.tree.campaign_schema import (
    CANCELLATION_TERMINALS,
    CHAMPION_SIDE,
    COHORT_ORDER,
    CONNECTION_FAILED_EVENT,
    CONNECTION_FINISHED_EVENT,
    CONNECTION_INTERRUPTED_EVENT,
    CONNECTION_START_EVENT,
    FINAL_CAMPAIGN_FAMILY_VERSION,
    GROUPER_SUSPENSIONS,
    MAX_EXCLUDED_PAIRS,
    MAX_FAULT_ROWS,
    RESILIENCE_SUSPENSIONS,
    SEED_VERSION,
    SUSPENDABLE_NO_OPERATION_EVENTS,
    SUSPENSION_REASONS,
    AffectedLayers,
    AttemptState,
    CampaignLifecycle,
    CampaignSnapshot,
    RoundState,
    adaptation_position,
    affected_layers,
    calibration_owed,
    campaign_family_for,
    champion_member,
    cohort_selection,
    confidence_scopes,
    creation_alpha_attempt,
    current_members,
    editor_input_reason,
    editor_replayable,
    effective_attempt_feedback,
    fault_rows_digest,
    feedback_outcomes,
    finalist_level,
    finalist_member,
    grouper_chain_limit,
    held_repair_required,
    member_diagnostic,
    member_identity,
    pairs_digest,
    primary_range_required,
    recorded_complete,
    reduce,
    refusal_prefix_required,
    running_members,
    selection_finalists,
    selection_records,
    side_of_entry,
    skip_chain_length,
    skip_chain_limit,
    skip_due,
    stage_redispatch_required,
    subset_of,
    superseding_comparison_owed,
)
from evolution.tree.campaign_statistics import (
    CoverageMissing,
    MissingMetric,
    PrimaryRange,
    SampleRow,
    arbitrate_owner,
    case_groups,
    declined_tries,
    latest_rows,
    primary_range_for,
    restored_tries,
    stage_floor,
    stage_horizon,
    thresholds_reached,
)
from evolution.tree.campaign_view import (
    STORE_SIBLING,
    THRESHOLD_TERMINAL,
    Action,
    BindBaseline,
    BindBaselineRefresh,
    BindCohortMember,
    BindComparison,
    BindFinalists,
    BindMeasurement,
    BindParent,
    Birth,
    CalibrateNoise,
    CampaignView,
    CloseAttempt,
    CompleteBaseline,
    CompleteBaselineRefresh,
    CompleteCohortMember,
    DecideFocus,
    DecideRound,
    EvaluateLook,
    FailPreflight,
    FinishCampaign,
    MeasureBaselineStage,
    OpenAttempt,
    RunAttemptStage,
    RunCohortStage,
    RunEditor,
    RunGrouping,
    RunReflection,
    SealAttempt,
    SelectAffected,
    SelectFocusPortion,
    SelectGateScope,
    _owed_sample_rows,
    action_attempt_id,
    adaptive_grouping_for,
    assemble,
    attempt_action,
    bound_grouping,
    classify_siblings,
    classify_store,
    held_attempts,
    measurement_side,
    next_actions,
    owed_repairs,
    owed_rows,
    rebound_held_attempts,
    refresh_due_for,
    threshold_owed,
)
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.champion_gaps import (
    CHAMPION_INCOMPLETE,
    champion_gap_rows,
    champion_owner,
    champion_rows_evidence,
)
from evolution.tree.component_drift import (
    ComponentsCheck,
    DriftReference,
    baseline_parent_reference,
    candidate_reference,
    champion_reference,
    checked_selection,
)
from evolution.tree.confidence_cohort import (
    Member,
    cohort_owner,
    cohort_rows_evidence,
    global_comparison_record,
    incomplete_rows,
    member_completion,
)
from evolution.tree.confidence_gate import (
    GateInputs,
    GateInvariant,
    StageAdmission,
    calibration_record,
    comparison_record,
    gate_inputs,
    look_record,
    stage_admission,
)
from evolution.tree.confidence_gate import (
    look_missing as confidence_look_missing,
)
from evolution.tree.execution import durable_reference as _durable_reference
from evolution.tree.execution.authorizations import (
    ADAPTATION_PROFILE_FIELD,
    Authorization,
    AuthorizationConflict,
    AuthorizationError,
    Effect,
    RefusalDifference,
    baseline_import_instance_digest,
    comparable_changed,
    comparable_field,
    identity_differences,
    identity_instance_digest,
    identity_record,
)
from evolution.tree.execution.authorizations import match as match_authorization
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.check_modes import (
    ENFORCE_ALL,
    CheckModes,
    HitSink,
    Mode,
    check_predicate,
    combine_checks,
    redact_for_journal,
    scan_grouping_value,
)
from evolution.tree.execution.interaction import (
    AttestationError,
    Environment,
    EvalCase,
    MaterializationStrategy,
    RolloutBudget,
    Scorer,
    TargetIdentity,
)
from evolution.tree.execution.journal import (
    EVENTS_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    inspect_journal,
)
from evolution.tree.execution.journal_identity import (
    IdentityError,
    canonical_json,
    resolve_package_version,
)
from evolution.tree.execution.journal_rollout import ConnectionAdmission
from evolution.tree.execution.journal_schema import Lifecycle
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault
from evolution.tree.execution.wire_v2 import IDENTIFIER, MAX_RUN_ID, validate_instance_description
from evolution.tree.gate_scope import derive_gate_scope
from evolution.tree.roles.editor.editor_brief import _observation_evidence_id
from evolution.tree.roles.editor.editor_evidence import (
    project_attempt_feedback,
    project_history,
    project_observations,
)
from evolution.tree.roles.editor_contracts import (
    BRIEF_LIMITS_V2,
    BriefHistory,
    CandidateEditor,
    Diagnosis,
    DiagnosisFocus,
    EditBrief,
    EditRequest,
    RoleProjection,
    brief_digest,
    diagnosis_for_brief,
    note_digest,
    with_attempt_feedback,
)
from evolution.tree.roles.environment_docs import (
    EnvironmentDocs,
    _v2_load_document_snapshot,
    _v2_repair_document_snapshot,
    _V2DocumentSnapshot,
    write_snapshot,
)
from evolution.tree.roles.grouping.grouping_runtime import (
    AdaptiveCampaignRuntimeConfig,
    GroupingRuntime,
)
from evolution.tree.roles.grouping.reflect_grouping import Cohort as GroupingCohort
from evolution.tree.roles.grouping.reflect_grouping import (
    GroupingAdmissionError,
    GroupingResult,
    admit_grouping_result,
    cohorts_from_grouping,
    select_portion,
)
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.roles.reflection_reader import ReaderIntegrityError
from evolution.tree.roles.reflector.reflect_brief import (
    AdaptiveReflectBrief,
    ReflectBrief,
    build_adaptive_reflect_brief,
    catalog_case_ids,
    reflect_brief_payload,
    render_reflect_catalog,
    with_history,
)
from evolution.tree.roles.reflector.reflect_budget import (
    PROBE_URL_PLACEHOLDER,
    bound_reflection_history,
    budget_record,
    measure_first_request,
    request_budget_of,
    write_budget_record,
)
from evolution.tree.roles.reflector.reflect_change import excerpt_screen
from evolution.tree.roles.reflector.reflect_history import (
    AttemptFacts,
    HistoryReader,
    HistorySource,
    MemoKey,
    build_history_source,
    initial_layout,
    render_history,
)
from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
from evolution.tree.roles.reflector_contracts import (
    REFLECT_LIMITS_V1,
    CandidateReflector,
    Group,
    GroupError,
    NeedleWitness,
    ProbeAccess,
    ReflectRequest,
    ReflectResult,
    admit_reflection_result,
    encode_pointer,
    find_needle,
    first_needle,
    locate_needle,
    oracle_needles,
    reflection_needles,
    result_note_digest,
)
from evolution.tree.roles.role_call import AttemptRecorder, AttemptReport
from evolution.tree.roles.role_failures import (
    LIFECYCLE_ERRORS,
    GrouperRoleFailure,
    GroupingExecutionError,
    editor_class,
    grouper_suspension_for,
    journaled_editor_reason,
    reflection_class,
    reflection_reason,
)
from evolution.tree.roles.role_visibility import RoleVisibilityView
from evolution.tree.roles.workspace import (
    ControlCollision,
    FileState,
    _without_control,
    capture_tree,
    discard,
    discard_directory,
    freeze_projection,
    open_attempt_log,
    project,
    role_projection_digest,
)
from evolution.tree.storage.append_log import AppendLog, Snapshot, inspect_log
from evolution.tree.storage.append_log import _fault as _journal_fault
from evolution.tree.storage.envelope import Envelope, SchemaError, secret_match
from evolution.tree.storage.inode_floor import (
    CampaignTerms,
    InodeNeed,
    _low_floor_note,
    campaign_need,
    count_tree,
    existing_ancestor,
    shares_tmp,
    whole_run_note,
)
from evolution.tree.storage.journal_errors import JournalBoundaryError, JournalFault
from evolution.tree.target.config import ProbeSection
from evolution.tree.target.connection_contract import MAX_POINTER_BYTES, ConnectionContract
from evolution.tree.target.connection_probe import (
    CONNECTION_DIR,
    binding_digest,
    description_match,
    epoch_binding,
    journalable_path,
    probe_identity,
    probe_path,
    probe_seed,
    probe_settlement,
    release_probe_leftover,
    run_probe,
    secret_path,
    utf8_bounded,
)
from evolution.tree.target.probe_mediator import ProbeMediator
from evolution.tree.target.probe_session import (
    ProbeSession,
    check_probe_health,
    write_probe_diagnostic,
)

if TYPE_CHECKING:  # `bindings` pulls in every backend; only the type is needed here.
    from evolution.tree.execution.output_admission import HygieneFinding
    from evolution.tree.roles.grouping.consolidation import BatchAnswer
    from evolution.tree.roles.grouping.model_grouping_runtime import ModelGroupingOutcome
    from evolution.tree.roles.grouping.model_grouping_v2 import Adopted
    from evolution.tree.roles.grouping.portion_session import PortionOutcome

CAMPAIGN_FILE = "campaign.jsonl"
CAMPAIGN_LOCK = ".campaign.lock"

_BIND_HIT_FIELDS = frozenset(
    {
        "identity.checks",
        "identity.metric_contract",
        "identity.role_visibility",
        "identity.verbatim_answers",
        "identity.secret_oracle_paths",
        "identity.preflight_sample_retries",
    }
)

#: The hook names this module fires itself; every other name reaches S2.
CAMPAIGN_HOOKS = frozenset(
    {
        "after_campaign_created",
        # PDR-0010: right after `identity_rebased` is durable and before the
        # ledger's `store_rebased` completes it, so a fixture can cut the power
        # exactly between the two halves of one rebase.
        "after_identity_rebased",
        # Right after the round's baseline is bound *and born*, so a fixture can
        # leave a real, incomplete sibling behind.
        "after_baseline_bound",
        "after_baseline_complete",
        # Family 14 (H7, F0 R43): right after a `baseline_refresh_bound` and a
        # `baseline_refresh_completed` are durable, so a fixture can cut the
        # power before the refresh's baseline is bound or before grouping.
        "after_baseline_refresh_bound",
        "after_baseline_refresh_completed",
        "after_grouping_started",
        "after_grouping_finished",
        "after_grouping_failed",
        "after_focus_portion_selected",
        "after_attempt_input_frozen",
        "after_attempt_closed",
        "after_comparison_preflight_failed",
        "after_attempt_opened",
        "after_comparison_data_declared",
        "after_comparison_data_published",
        "after_gate_measurement_completed",
        "after_comparison_completed",
        "after_editor_started",
        "after_measurement_bound",
        # G2b3a: right after a family-12 Attempt's `parent_measurement_bound`
        # is durable and before the fresh Parent's journal is created.
        "after_parent_measurement_bound",
        # #78: right after a `repairs_dispatched` is durable and before its
        # repairs start, so a fixture can cut the power between the two.
        "after_repairs_dispatched",
        # #109 (a28 spec §3.3): right after a `stage_redispatched` is durable
        # and before the carried stage's dispatch materializes, so a fixture
        # can cut the power between the two.
        "after_stage_redispatched",
        # G2b2: right after a family-12 Attempt's `candidate_inspected` and its
        # `affected_cases_selected`, before anything is bound.
        "after_candidate_inspected",
        "after_affected_cases_selected",
        "after_editor_finished",
        "after_reflection_finished",
        "after_reflection_failed",
        "after_probe_checked",
        "after_reflection_started",
        "after_selection_decided",
        "after_round_closed",
        # Right after a crossing is durable and before the terminal it names is
        # decided, so a fixture can leave a campaign that owes its
        # `campaign_finished` (PDR-0010).
        "after_threshold_reached",
        # Comparable-links spec §5.3: right after an applied close or terminal
        # is durable and before the terminal it owes, so a fixture can leave a
        # campaign whose prefix ends on `authorization_applied(effect: close)`.
        "after_close_applied",
        # I2 spec §5.4: the seams of an applied `continue` block -- right after
        # its `authorization_applied`, after each `measurement_rebound`, and
        # before an owed re-bound measurement is born -- so a fixture can cut
        # the power anywhere inside it (`after_identity_rebased` is shared with
        # the rebase path).
        "after_condition_applied",
        "after_measurement_rebound",
        "before_rebound_birth",
        # The independent Candidate's owed completion copy, before its birth.
        "before_completion_birth",
        # Family 13: after the round barrier's `close_held`, where slice F5's
        # cohort begins.
        "before_finalists_bound",
        # Family 13: right after a `comparison_bound` is durable.
        "after_comparison_bound",
        # Family 13: right after a `noise_calibration_bound` is durable.
        "after_noise_calibration_bound",
        # Family 13: right after a `noise_calibration_completed` is durable.
        "after_noise_calibration_completed",
        # Family 13: right after a `comparison_look_evaluated` is durable.
        "after_comparison_look_evaluated",
        # Family 13 (design §6.7): after `finalists_bound` is durable.
        "after_finalists_bound",
        # Compatibility hooks also serve family 14; each fires once per real
        # record, so old crash fixtures retain their cuts without duplicate writes.
        # After a member's binding, before its journal exists.
        "after_cohort_measurement_bound",
        # Family 13 (design §6.9): after a member's completion record.
        "after_cohort_measurement_completed",
        # Family 13: right before any `selection_decided` -- the null one of a
        # round without finalists, or the selection over a cohort.
        "before_selection_decided",
        # Family 13: a complete cohort, on entry to `decide_cohort`.
        "before_global_comparison",
        # Family 13 (design §6.10): right after each `global_comparison_completed`.
        "after_global_comparison_completed",
        # Family 14 (H10, F0 R57-R62): the connection preflight's seams. After
        # `connection_preflight_started` is durable and before its probe journal
        # is born; after the probe journal is born and before any Target call;
        # after the preflight's terminal (for a failed one, before its
        # suspension, inside the writer mutex); after the connection suspension.
        "after_connection_preflight_started",
        "after_connection_probe_journal_born",
        "after_connection_preflight_terminal",
        "after_connection_suspended",
    }
)

#: Where a round's read-only Reflector projections are made (spec §3.3): a
#: private copy of the Parent per reflection, removed as soon as the call
#: returns, kept under the run directory so nothing of a run leaves it.
REFLECTIONS_DIR = ".reflections"

# A deterministic preference order over the strategies an adapter may declare, so
# the frozen `materialization_strategy` of the identity is a function of the
# adapter's capabilities alone.
_STRATEGY_ORDER: tuple[MaterializationStrategy, ...] = (
    "adapter_apply_manifest",
    "filesystem_overlay",
    "artifact_root_injection",
)

# PDR-0038 / ADR-0046: only native /15 and /16 policies produce campaigns.
_ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION = 15

#: PDR-0038 / ADR-0046: the campaign families
#: `run_campaign` creates and `resume_campaign` resumes: family 14 (`/15`)
#: and opt-in family 15 (`/16`); removed families are refused with
#: `campaign_family_unsupported` before anything is written. Read at call time:
#: the evolve-side probe reads it too. Readers admit these same two families.
_ADMITTED_CAMPAIGN_FAMILIES: frozenset[int] = frozenset({FINAL_CAMPAIGN_FAMILY_VERSION, 15})


def unavailable_probe_check(snapshot: CampaignSnapshot) -> bool:
    """Does the prefix record an unavailable probe check (it owes `probe_unavailable`)?"""

    return not probe_continues(snapshot.identity, snapshot.family_version) and any(
        state.probe_check is not None and state.probe_check.get("status") == "unavailable"
        for state in snapshot.rounds.values()
    )


def probe_unavailable_evidence(
    records: Sequence[Envelope], snapshot: CampaignSnapshot
) -> tuple[dict[str, Any], ...] | None:
    """The evidence `probe_unavailable` carries, rebuilt from the prefix alone.

    One `journal_record` reference over the latest unavailable probe check --
    its run id, its `campaign.jsonl#bytes=` range and the digest of its encoded
    record -- which is exactly what the lifecycle's `_check_finished` compares
    against its own `_probe_check_locators` and the check's `record_digest`.
    `None` when the prefix records no unavailable check. The live executor,
    an accepted `terminal` effect and the state-first recovery of an owed
    terminal all build it here (codex r1, PDR-0012).
    """

    if probe_continues(snapshot.identity, snapshot.family_version):
        return None
    for state in reversed(tuple(snapshot.rounds.values())):
        check = state.probe_check
        if check is None or check["status"] != "unavailable":
            continue
        offset = 0
        for record in records:
            encoded = record.encode()
            payload = record.to_payload()
            if payload["sequence"] == check["seq"]:
                return (
                    dict(
                        version=1,
                        kind="journal_record",
                        run_id=str(payload["run_id"]),
                        locator=f"{CAMPAIGN_FILE}#bytes={offset}:{offset + len(encoded)}",
                        content_digest=hashlib.sha256(encoded).hexdigest(),
                    ),
                )
            offset += len(encoded)
        raise CampaignInvariantViolated("the campaign has no record at that sequence")
    return None


def _advances_visitation(closed: Mapping[str, Any], *, confidence: bool = False) -> bool:
    """Does a closed round count its focus portion as visited?

    A `stalled` or `unscored` round examined its cohorts. Family 9 adds the
    `unplanned` round whose Reflector gave a valid terminal answer -- a decline
    or an empty reflection -- but not an operational `reflection_failed`, whose
    group stays eligible (spec §14, ADR-0022). No earlier family writes
    `unplanned`, so their visitation is unchanged. Family 10 adds the skip, a
    failed reflection's round that carries `visitation: "skipped"` (role
    resilience spec §4.2, ADR-0023's exception to ADR-0012); no earlier
    family writes that key.

    Family 13 (`confidence`, #148, ADR-0012 amendment of 2026-10-06) adds
    `retained`: a focused, examined close with no Promotion, so it advances
    visitation like `stalled`. Only family 13 writes `retained`; the flag keeps
    families 4 to 12 on the rule above.
    """

    outcome = closed["outcome"]
    if outcome in {"stalled", "unscored"}:
        return True
    if confidence and outcome == "retained":
        return True
    if closed.get("visitation") == "skipped":
        return True
    return outcome == "unplanned" and closed["reason"] in {
        "reflection_declined",
        "reflection_empty",
    }


def visitation_chain_start(snapshot: CampaignSnapshot, round_number: int) -> int:
    """The first round of round `round_number`'s family-13 visitation chain.

    Pre-F8 design §3.3 step 1 (#148, ADR-0012 amendment of 2026-10-06): the
    latest of the campaign's first round, the round the latest `rebaseline`
    started when it is at most `round_number`, and the round after the latest
    earlier `promoted` close. A Promotion and a `rebaseline` begin a new cycle;
    a `retained` refresh does not, nor does a family-14 baseline refresh
    (`m-r<NN>-refresh`, ADR-0012 amendment of 2026-10-09).
    """

    start = snapshot.first_round
    rebaseline = snapshot.rebaseline_from_round
    if rebaseline is not None and rebaseline <= round_number:
        start = max(start, rebaseline)
    for number, state in snapshot.rounds.items():
        if (
            number < round_number
            and state.closed is not None
            and state.closed["outcome"] == "promoted"
        ):
            start = max(start, number + 1)
    return start


def visitation_in_cycle(
    rounds: Mapping[int, Any],
    round_number: int,
    *,
    binding: str,
    policy_digest: str,
    chain_start: int | None,
) -> tuple[frozenset[str], frozenset[str], int]:
    """`(visited_cohort_ids, carried_case_ids, cycle)` before round `round_number`.

    Earlier rounds with a portion and a close, the same `policy_digest` and a
    close that advances visitation count, in round order: a higher portion
    `cycle` clears both sets and raises the cycle; the same cycle adds the
    portion's ids. A placeholder portion (`none`, `unmeasurable`) writes cycle 1
    and no ids, so it never raises the cycle and adds nothing.

    `chain_start=None` is families 4 to 12's per-binding loop: a round on
    another `baseline_binding` is skipped and `carried` stays empty. With a
    chain start (family 13, pre-F8 design §3.3 step 2, ADR-0012 amendment of
    2026-10-06), rounds before it are skipped, `retained` advances visitation,
    and a round on another binding -- one before a `retained` refresh --
    contributes its `case_ids` to `carried` rather than its cohort ids, which
    no later binding shares.
    """

    confidence = chain_start is not None
    visited: set[str] = set()
    carried: set[str] = set()
    cycle = 1
    for number in sorted(rounds):
        prior = rounds[number]
        if number >= round_number or prior.focus_portion is None or prior.closed is None:
            continue
        if chain_start is not None and number < chain_start:
            continue
        portion = prior.focus_portion
        same_binding = portion["baseline_binding"] == binding
        if (
            (not confidence and not same_binding)
            or portion["policy_digest"] != policy_digest
            or not _advances_visitation(prior.closed, confidence=confidence)
        ):
            continue
        prior_cycle = int(portion["cycle"])
        if prior_cycle > cycle:
            visited.clear()
            carried.clear()
            cycle = prior_cycle
        if prior_cycle == cycle:
            if same_binding:
                visited.update(portion["cohort_ids"])
            else:
                carried.update(portion["case_ids"])
    return frozenset(visited), frozenset(carried), cycle


def carried_cohort_ids(
    cohorts: Iterable[GroupingCohort], carried: Collection[str]
) -> frozenset[str]:
    """Every cohort of a carried case counts as visited, in whatever group it is.

    Pre-F8 design §3.3 step 3. A carried case with no cohort on this baseline
    (every pair excluded, or it no longer fails) marks nothing, so a carried id
    never blocks `select_portion`'s cycle reset.
    """

    return frozenset(cohort.cohort_id for cohort in cohorts if cohort.case_id in carried)


def unresponsive_terminal(snapshot: CampaignSnapshot) -> bool:
    """Does an unfinished prefix owe an unresponsive role's terminal (ruling 6)?

    A durable `editor_failed(unresponsive)` owes `editor_unresponsive` and a
    durable `reflection_failed(unresponsive)` owes `reflector_unresponsive` --
    the facts the lifecycle's own flags record and
    `resumed_unresponsive`/`resumed_reflector_unresponsive` derive on resume.
    The lifecycle refuses a close over either and owes the pair to an applied
    `terminal` effect.

    Family 7 (I1 spec §5.4) owes no terminal at all: a failure is a suspension
    cause, and only one recorded *after* the latest `campaign_suspended` is
    still unresolved -- the suspension cleared every earlier one. So there the
    answer is whether such a cause is still outstanding.
    """

    return bool(_outstanding_causes(snapshot, "editor_unresponsive", "reflector_unresponsive"))


def _outstanding_causes(
    snapshot: CampaignSnapshot, *reasons: str
) -> tuple[tuple[int, str | None], ...]:
    """Family 7: the `(seq, operation_id)` of every record since the latest
    suspension that decides one of `reasons` and no suspension has resolved yet,
    oldest first (the lifecycle's own `suspension_causes`)."""

    return tuple(
        (seq, operation)
        for seq, (reason, operation) in sorted(snapshot.suspension_causes.items())
        if reason in reasons
    )


def _family_for_policy_class(
    policy_class: type[CampaignPolicyV11], adaptation_version: str | None
) -> int:
    """The native policy/profile pair, with historical labels for corrupt evidence.

    Non-/7 profiles are never admitted. Their old numeric label is used only
    by the misfiled-policy probe so damaged current journals keep their
    persistence diagnosis, rather than being coerced into a current family.
    """
    if issubclass(policy_class, CampaignPolicyV11):
        if adaptation_version == "campaign_adaptation/7":
            return 15 if issubclass(policy_class, CampaignPolicyV12) else 14
        return {
            "campaign_adaptation/6": 13,
            "campaign_adaptation/5": 11,
            "campaign_adaptation/4": 10,
            "campaign_adaptation/3": 9,
            "campaign_adaptation/2": 8,
        }.get(adaptation_version or "", 7)
    raise ValueError("adaptive_runtime_requires_policy_v5")


def _refuse_misfiled(selected: int, journaled: int | None) -> None:
    """A policy across a family-13 or family-14 boundary is malformed evidence
    (ADR-0008; F2 plan P-R9; codex F8 round 1; family-14 design R14).

    Two directions: a `/14` or `/15` creation policy in a journal of another
    family, and a family-13 or family-14 journal whose creation policy selects
    another family. The resume probe catches the refusal and scans the journal
    in its own family, which classifies it as `persistence_failed`. Every other
    pair passes: the family check below the probe decides it, so a family 6-12
    journal whose policy selects another pre-13 family keeps its typed refusal.
    """

    if type(journaled) is not int:
        return
    # Both boundaries, 13 then 14 (R14), so a family-13 cross keeps its code.
    for boundary, code in (
        (13, "policy_v10_not_admitted"),
        (FINAL_CAMPAIGN_FAMILY_VERSION, "policy_v11_not_admitted"),
        (15, "policy_v12_not_admitted"),
    ):
        if (selected >= boundary) != (journaled >= boundary):
            raise PolicyNotAdmitted(code)


def _family_for_policy(
    policy: CampaignPolicyV11, adaptive: AdaptiveCampaignRuntimeConfig | None
) -> int:
    """The family a campaign created from `policy` and `adaptive` journals."""

    return _family_for_policy_class(
        type(policy), None if adaptive is None else adaptive.identity.profile
    )


def _runtime_family_version(
    policy: CampaignPolicyV11, adaptive: AdaptiveCampaignRuntimeConfig | None
) -> int:
    family = _family_for_policy(policy, adaptive)
    # The staged successor has its own writer gate, never a family-14 identity.
    if family == 15:
        return family
    return min(family, _ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION)


def _admit_active_runtime(
    policy: CampaignPolicyV11,
    adaptive: AdaptiveCampaignRuntimeConfig | None,
    connection: ConnectionContract | None = None,
) -> None:
    """Require the one writable current-family input set at the public boundary.

    Family 14 also requires its `ConnectionContract`, whose digests its
    identity binds (R17), and no older family takes one.
    """

    if adaptive is None:
        raise ValueError("adaptive_runtime_required")
    if not isinstance(adaptive, AdaptiveCampaignRuntimeConfig):
        raise TypeError("adaptive_runtime_invalid")
    if adaptive.model_grouping is not None and adaptive.model_credentials is None:
        # `evolve` binds them (`bindings.grouping_model_credentials`); a library
        # caller that omits them would otherwise fail only at the first grouping.
        raise ValueError("model_grouping_credentials_required")
    family = _runtime_family_version(policy, adaptive)
    _admit_runtime_policy(policy, family)
    if connection is None:
        raise ValueError("connection_contract_required")


def _refuse_unadmitted_family(family: int | None) -> None:
    """The writer gate (PDR-0038, ADR-0046): refuse a family this
    release neither creates nor resumes, before anything is written."""

    if family not in _ADMITTED_CAMPAIGN_FAMILIES:
        raise CampaignFamilyUnsupported(family, reader=FINAL_CAMPAIGN_FAMILY_VERSION)


def _admit_runtime_policy(policy: CampaignPolicyV11, family_version: int) -> None:
    """Require the native policy for its exact current family."""
    if not isinstance(policy, CampaignPolicyV11):
        raise ValueError("adaptive_runtime_requires_policy_v5")
    if isinstance(policy, CampaignPolicyV12) != (family_version == 15):
        raise ValueError("adaptive_runtime_policy_v12_family_mismatch")
    # Keep the established malformed-native-profile diagnostics, without
    # retaining a retired policy constructor or an admitted old writer.
    if family_version < 13:
        raise ValueError("adaptive_runtime_policy_v10_family_mismatch")
    if family_version < FINAL_CAMPAIGN_FAMILY_VERSION:
        raise ValueError("adaptive_runtime_policy_v11_family_mismatch")


#: The identity fields spec §7's comparability contract compares across a link.
#: `corpus_cases` sits next to `corpus_digest`: the digest alone is order-blind
#: (it sorts by `case_id` before hashing), but corpus order is statistically
#: significant to the bootstrap comparison (ADR-0008), so a reordered corpus
#: must refuse the link even when its digest is unchanged.
_COMPARABLE = (
    "policy_digest",
    "target",
    "environment",
    "scorer",
    "evaluation_set_id",
    "corpus_digest",
    "corpus_cases",
    "metric_plan_digest",
    "metric_contract",
    "materialization_strategy",
)


class CampaignExists(ValueError):
    """The run root already holds a campaign, a store or a measurement."""


class CampaignMissing(ValueError):
    """`resume_campaign` requires `campaign.jsonl` (spec §4 step 3)."""


class PolicyNotAdmitted(ValueError):
    """A creation policy across a family-13 or family-14 journal boundary (ADR-0008).

    `_refuse_misfiled` raises this when a V10- or V11-shaped creation policy
    selects a family across either boundary from its journal. Resume's
    journal-family probe catches it: such a creation policy in a journal of a
    family this reader has is malformed evidence of that family, which C1's
    own scan classifies as `persistence_failed` (ADR-0008).
    """


#: ADR-0046's one migration sentence for removed or unversioned campaigns.
REMOVED_FAMILY_GUIDANCE: Final = (
    "this release reads campaign families 14 and 15; read this run with evolution "
    "2.0.0b9 or seed a new /15 campaign from its champion copy"
)


class CampaignFamilyUnsupported(ValueError):
    """The run root holds a campaign journal of another record family (spec §4).

    `journaled` is the `family_version` the creation record carries, `None`
    for the previous, unversioned family; `reader` is this package's
    constant. Raised by `resume_campaign` before the log is opened, so the
    run root is untouched; W1 surfaces it as `campaign_family_unsupported at
    family/<journaled>`. The writer gate (`_refuse_unadmitted_family`) raises
    it too, from `run_campaign` with the family it would create.

    `str()` is always the bare refusal code; `path` and `expected` carry the
    public location and migration guidance for presentation at command boundaries.
    """

    def __init__(self, journaled: int | None, *, reader: int = 3) -> None:
        super().__init__("campaign_family_unsupported")
        self.journaled = journaled
        self.reader = reader
        self.path = f"family/{'absent' if journaled is None else journaled}"
        self.expected = (
            REMOVED_FAMILY_GUIDANCE
            if journaled is None or journaled < FINAL_CAMPAIGN_FAMILY_VERSION
            else None
        )


class CampaignInvariantViolated(ValueError):
    """The executor offered the campaign log a record its own lifecycle refuses.

    That is an executor bug, never a storage failure. `AppendLog.append` turns a
    `SchemaError` from `prepare` into a poisoned writer plus a `JournalFault`,
    which §7's classification would then report as `persistence_failed` — a false
    diagnosis that hides the real defect. Every append is therefore proved through
    the lifecycle's own `prepare` first (`_Campaign.prove`), and a refusal raises
    this, naming the rule, with the log untouched and still healthy.
    """

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


class IdentityChanged(IdentityError):
    """`run_identity_changed` with what differs and, unless hard, the digest to quote.

    Spec §4: core detects the difference and reports it as data. `differences`
    is the dotted diff — fields and digests, never values — `instance_digest` is
    what an `Authorization` must name to accept this exact transition, and
    `hard` says that no authorization can: a comparable field moved, or the run
    has nothing left to continue. A hard refusal carries no digest at all, so a
    decision directory cannot be filled for one.

    It remains an `IdentityError` whose `str()` is `run_identity_changed`, so a
    caller written before PDR-0010 sees exactly what it saw.

    `effect` is what an accepted decision will do (comparable-links spec
    §5.1): `rebase` for a non-comparable move, `close` for a comparable one on
    an unfinished family-6 campaign, `terminal` when that campaign already owes
    its threshold terminal, an unavailable probe check's `probe_unavailable`
    (codex r1) or an unresponsive editor's or Reflector's (controller ruling
    6). On an unfinished family-7 campaign a comparable move names its
    default, `continue` or `rebaseline` included (I2 spec §3.3).

    `effects` is every effect an accepted decision may choose, default first
    (I2 spec §4.3): the admissible set of a family-7 comparable move, and
    `(effect,)` for every other authorizable change. A hard refusal has none.
    """

    def __init__(
        self,
        differences: tuple[RefusalDifference, ...],
        instance_digest: str | None,
        *,
        hard: bool,
        effect: Effect = "rebase",
        effects: tuple[Effect, ...] | None = None,
    ) -> None:
        super().__init__("run_identity_changed")
        self.differences = differences
        self.instance_digest = instance_digest
        self.hard = hard
        self.effect: Effect = effect
        self.effects: tuple[Effect, ...] = (
            effects if effects is not None else () if hard else (effect,)
        )


#: The attribute `_mark_first_write` sets on an exception raised after this
#: invocation's first durable write.
_AFTER_FIRST_WRITE = "after_first_write"


def _mark_first_write(exc: BaseException, campaign: _Campaign) -> None:
    """Record on `exc` whether `campaign` had written when it was raised."""

    if campaign.wrote:
        setattr(exc, _AFTER_FIRST_WRITE, True)


def raised_after_first_write(exc: BaseException) -> bool:
    """Was `exc` raised after the invocation's first durable write?

    The I2 refusal types (`AuthorizedIdentityChanged`, `ReboundSourceCorrupt`,
    `PolicyContradictsPrefix`, `AuthorizationConflict`, `AuthorizationError`)
    are refusals only before it. `ReboundSourceCorrupt` in particular can
    also come out of a frozen source read while the block's own records are
    already durable -- an owed re-bound birth, the rebaseline round close --
    and a refusal there would claim the run directory untouched.
    """

    return bool(getattr(exc, _AFTER_FIRST_WRITE, False))


class AuthorizedIdentityChanged(IdentityError):
    """I2 spec §5.4: `authorized_identity_changed` -- a block's recovery found another identity.

    A crash after an applied `continue`/`rebaseline` leaves the journaled
    `authorization_applied` as standing authority for exactly the identity it
    recorded (`observed`). A resume that rebuilds any other triple -- the
    manifest changed again since the decision -- cannot finish that block and
    must not open another: hard, before any write. `differences` names both
    triples, key by key (`journaled` is the recorded `observed`, `observed`
    the rebuilt one).
    """

    def __init__(self, differences: tuple[RefusalDifference, ...]) -> None:
        super().__init__("authorized_identity_changed")
        self.differences = differences
        #: The keys that actually differ -- `producing_version` alone when the
        #: package was upgraded between the crash and this resume.
        self.changed = tuple(d.field for d in differences if d.journaled != d.observed)


class ReboundSourceCorrupt(IdentityError):
    """I2 spec §5.3: `rebound_source_corrupt` -- a frozen measurement no longer reads back.

    A frozen measurement is this run's own and is never written again, so
    whatever it can no longer yield -- the journal is unreadable, bound under
    another identity, or its carry recomputes to anything but the declared
    `imported`/`pairs_digest` -- is damage to the run directory, not a lost
    source: hard (the `stored_identity_digest_mismatch` class), and never an
    authorizable `fresh_baseline`. `measurement_id` names the measurement.
    """

    def __init__(self, measurement_id: str) -> None:
        super().__init__("rebound_source_corrupt")
        self.measurement_id = measurement_id


class PolicyContradictsPrefix(IdentityError):
    """I2 spec §3.4: `policy_contradicts_prefix` -- a new policy below what the run has spent.

    Raised at admission, before any write, when an accepted `rebaseline`
    would move the policy to a campaign-wide budget the durable prefix has
    already exceeded (`campaign_epochs.policy_contradiction`). Hard: it is an
    impossibility, not a claim-protecting rule. `key` names the budget,
    `spent` the journaled spend and `value` the new policy's value; the doors
    are to correct the value or to accept `close`.
    """

    def __init__(self, key: str, spent: str, value: str) -> None:
        super().__init__("policy_contradicts_prefix")
        self.key = key
        self.spent = spent
        self.value = value


class BaselineImportUnavailable(ValueError):
    """`baseline_import_unavailable`, authorizable (baseline inheritance §5.3).

    A declared baseline copy is still owed and its source cannot be read
    again, its prefix no longer has the declared `source_prefix_digest`, or
    the recomputed set does not match the declared `pairs_digest`.
    `instance_digest` is `baseline_import_instance_digest(run_id,
    pairs_digest)`, which an accepted `fresh_baseline` decision must name.
    """

    def __init__(self, instance_digest: str) -> None:
        super().__init__("baseline_import_unavailable")
        self.instance_digest = instance_digest

    def __str__(self) -> str:
        return "baseline_import_unavailable"


class HygieneNotAuthorizable(ValueError):
    """`run_dir_unsafe_entry`, hard: a finished campaign takes no authorization.

    PDR-0010 (amended 2026-09-17) says a campaign that has written
    `campaign_finished` takes no authorization at all, and the lifecycle refuses
    every record after that terminal. An invocation that nevertheless arrives
    holding a decision for one of the run directory's entries would discover
    that as `CampaignInvariantViolated` from inside `_write_consequences` --
    after the first append of the invocation, which is no longer a refusal. So
    `adopt` refuses it here instead, with the run directory untouched.

    `str()` is the code and `name` is the entry, exactly as `EvolveRefused`
    prints them. No instance digest travels with it: there is nothing a decision
    could accept, which is the whole of the refusal.
    """

    def __init__(self, name: str, guard: str) -> None:
        super().__init__("run_dir_unsafe_entry")
        self.code = "run_dir_unsafe_entry"
        self.name = name
        self.guard = guard


class UndeclaredSibling(ValueError):
    """A sibling the campaign journal never declared: a foreign writer."""


class ArtifactLedgerMissing(ValueError):
    """`artifact_ledger_missing`: the artifact ledger is lost though the store was born.

    ADR-0008: a born store is resumed by its owner and never removed. The campaign
    journal declaring a measurement proves the store was born (`create` births it
    before any measurement is bound), so a missing or empty `artifacts.jsonl` is
    lost evidence, not an unborn store. Refused before any write (#261).
    """

    def __init__(self) -> None:
        super().__init__("artifact_ledger_missing")


class LineageNotFinished(ValueError):
    """`lineage_not_finished`: the predecessor wrote no `campaign_finished` (spec §6.3, §7).

    An unfinished predecessor could still be resumed by its own version, and two
    successors would then spend the same alpha. Comparable-links spec §5.4: from
    an unfinished family-5 predecessor the refusal is authorizable and carries
    the instance digest a decision must quote (`lineage_instance_digest`); from
    an unfinished family-6 one it stays hard -- resume it, or close it (§5.2) --
    and `instance_digest` is `None`.
    """

    def __init__(self, instance_digest: str | None = None) -> None:
        super().__init__("lineage_not_finished")
        self.instance_digest = instance_digest


class FindingsPending(ValueError):
    """Collected hygiene findings no decision accepted, and nothing else to report (§6).

    Comparable-links spec §6: `evolve` collects every `run_dir_unsafe_entry` of
    the run directory instead of raising the first, and hands the unaccepted
    ones to C1 so the identity finding (resume) or the family-5 link finding
    (new run) can be reported beside them. When C1 raises neither, it raises
    this instead, before its first write: the collected findings alone are the
    refusal, and no write of this invocation may precede their acceptance.
    """

    def __init__(self, findings: tuple[HygieneFinding, ...]) -> None:
        super().__init__("findings_pending")
        self.findings = findings


#: The pre-0.3.0a13 name, kept because `evolution.tree` exports it.
LineageNotContinuable = LineageNotFinished


class LineageFamilyUnsupported(ValueError):
    """`lineage_family_unsupported`: the predecessor is of a family this producer cannot link.

    `journaled` is the predecessor's `family_version` (`None` for the
    unversioned family). Raised before the predecessor's journal is replayed.
    """

    def __init__(self, journaled: int | None) -> None:
        super().__init__("lineage_family_unsupported")
        self.journaled = journaled


class LineageModeUnsupported(ValueError):
    """`lineage_mode_unsupported`: an `observe` predecessor under a `statistical` successor.

    Family-13 D10 (ADR-0027 amendment 3, R3): a statistical campaign may not
    start from a Champion an `observe` run decided, so a predecessor any of
    whose identities -- its creation's or any rebase's -- ran `observe` is
    refused before anything is written. An `observe` successor may follow a
    `statistical` predecessor.
    """

    def __init__(self) -> None:
        super().__init__("lineage_mode_unsupported")


class LineageChampionUnverified(ValueError):
    """`lineage_champion_unverified`: the champion copy does not verify (spec §5.1).

    The predecessor's final champion copy under `artifacts/champions/` is
    re-hashed against its artifact ledger, and the champion the campaign journal
    finished with must be one of the ledger's, uncorrupted (not necessarily its
    latest: codex r1).
    """

    def __init__(self) -> None:
        super().__init__("lineage_champion_unverified")


#: The effects whose consequence records include `identity_rebased`.
_REBASING_EFFECTS = frozenset({"rebase", "continue", "rebaseline"})

#: The smallest `authorization` slot a decision can journal: admission measures
#: an offered rebase with it before any decision is matched (a lower bound).
_SHORTEST_AUTHORIZATION = {
    "id": "0" * 64,
    "reason": "x",
    "issued_at": "2026-01-01T00:00:00Z",
    "source": "file",
}


class CorpusTooLarge(ValueError):
    """A campaign identity record exceeds the journal's record bound (spec §3).

    The creation record, before the run root exists; or the `identity_rebased`
    an accepted identity change would write, before the invocation's first
    append (codex branch review r1).
    """

    def __init__(self) -> None:
        super().__init__("corpus_too_large")


class RecordMatchesSecret(ValueError):
    """A string of an identity record contains a secret of the run (spec §3).

    The creation record, before the run root exists; or the `identity_rebased`
    an accepted identity change would write, before the invocation's first
    append (codex branch review r2). `location` names where —
    `identity.<path>`, `adaptation_payload.<path>`, `envelope.<field>`, or the
    opaque `record-key` for a key name — and never the value or the secret.
    """

    def __init__(self, location: str) -> None:
        super().__init__("record_matches_secret")
        self.location = location


def secret_location(envelope: Mapping[str, Any], secrets: Sequence[str]) -> str | None:
    """Where the envelope's redaction would refuse it, in the public location grammar.

    Keys are checked before values at every level, so a key collision is
    reported as `record-key` even when a value under it would also match;
    any path that named the key would spell it.
    """

    found = secret_match(envelope, secrets)
    return None if found is None else found.location


@dataclass(frozen=True)
class CampaignResult:
    """What a campaign returns: a terminal, the barrier's verdict and two views."""

    terminal: Literal["finished", "suspended", "storage_capacity_exhausted", "persistence_failed"]
    quiescent: bool
    finished: dict[str, Any] | None
    owner: str | None
    code: str | None
    result: CampaignProjection | None
    status: CampaignProjection
    #: Family 7 (I1 spec §5.2): the `campaign_suspended` payload this session
    #: wrote, when `terminal == "suspended"`; `None` otherwise. `owner` and
    #: `code` then name the suspension's owner and reason.
    suspended: dict[str, Any] | None = None
    #: ADR-0040 D7/D8: the storage fault's private, path-free detail line (the
    #: trace entry that failed, or free and required inodes); Python only, never
    #: part of the stdout document. `evo tree evolve` prints it on stderr.
    fault_detail: str | None = None
    #: #290 §4.4: one line per role invocation of this session that ended in
    #: a provider failure, naming its failed attempts' statuses. Python only,
    #: never journaled and never part of the stdout document: `evo tree
    #: evolve` prints them on stderr right after the suspension report.
    provider_attempt_lines: tuple[str, ...] = ()


# Not frozen: an exception must be able to carry its own `__traceback__`.
@dataclass
class _Fault(Exception):
    """A persistence fault, already classified into its §7 terminal.

    `measurement` names the measurement journal that faulted, so a capacity
    terminal can record it in `capacity_exhausted` (spec §7).
    """

    terminal: Literal["storage_capacity_exhausted", "persistence_failed"]
    owner: str
    code: str
    measurement: str | None = None
    #: ADR-0040 D7/D8: the boundary error's `detail`, when it carries one.
    detail: str | None = None


#: Admission codes that compare the stored result with the registry rebuilt from the
#: frozen baseline evidence. Only these can mean that evidence is missing; every
#: other code says the stored result itself is invalid (#278 F2).
REGISTRY_DEPENDENT_GROUPING_CODES: Final = frozenset(
    {
        "registry_digest",
        "assignment_count",
        "nonfailing_membership",
        "unavailable_membership",
        "unused_description",
        "variant_index",
        "variant_count",
    }
)


#: The Grouper kinds whose results are admitted with unavailable failing
#: observations unassigned (`model_grouping.project`, spec §4): the `chat`
#: (`model_grouping`) and `agent_cli` (`agent_grouping`) Groupers.
_UNASSIGNING_GROUPER_KINDS = frozenset({"chat", "agent_cli"})


def _unassigns_unavailable(grouper_kind: object) -> bool:
    """Whether a grouping replays with unavailable failing observations unassigned.

    It is read off the `grouper_kind` its `grouping_started` recorded, so a
    replay admits the result exactly as its writer did, whatever Grouper the
    current identity declares (b16 §11.1).
    """

    return grouper_kind in _UNASSIGNING_GROUPER_KINDS


def _admission_code(exc: Exception) -> str:
    """The bounded refusal code: only a `GroupingAdmissionError` carries one."""
    return str(exc) if isinstance(exc, GroupingAdmissionError) else "invalid_policy_identity"


def _grouping_evidence_fault(
    exc: GroupingAdmissionError | ReaderIntegrityError | BaselineUnavailable | ValueError,
    measurement_id: str,
) -> _Fault:
    """A stored grouping cannot be replayed without its frozen baseline evidence."""
    if isinstance(exc, (ReaderIntegrityError, BaselineUnavailable)):
        entry = f"measurements/{measurement_id}/events.jsonl"
    elif _admission_code(exc) in REGISTRY_DEPENDENT_GROUPING_CODES:
        entry = f"scorer-traces/ for measurement {measurement_id} ({_admission_code(exc)})"
    else:
        # A shape or policy refusal is not missing evidence: do not send the operator
        # to restore scorer traces.
        return _Fault(
            "persistence_failed",
            "measurement_journal",
            "journal_unavailable",
            measurement_id,
            f"the stored grouping result is invalid ({_admission_code(exc)}); "
            "restore it from a trusted copy, or start a new or linked run",
        )
    return _Fault(
        "persistence_failed",
        "measurement_journal",
        "journal_unavailable",
        measurement_id,
        f"baseline evidence unavailable at {entry}; "
        "restore it from a trusted copy, or start a new or linked run",
    )


@dataclass(frozen=True)
class _Written:
    """One appended record and the byte range the log laid it down at."""

    start: int
    end: int
    record: Envelope
    operation_id: str | None


class _GroupingCheckpoints:
    """The campaign's `CheckpointSink` for one family-15 grouping (b16 §9, ADR-0008).

    Each finished portion and leaf batch becomes one operationless journal
    record naming the grouping, written before the runtime uses the decision.
    The planner and the split rules keep every record within
    `family15_schema.MAX_RECORD_BYTES`, so a record the schema refuses is a
    programming error and leaves as `CampaignInvariantViolated`.
    """

    def __init__(self, campaign: _Campaign, grouping_id: str) -> None:
        self.campaign = campaign
        self.grouping_id = grouping_id

    def portion(self, outcome: PortionOutcome, plan_digest: str, input_digest: str) -> None:
        from evolution.tree.roles.grouping.portion_session import checkpoint_body

        body = {
            "grouping_id": self.grouping_id,
            "plan_digest": plan_digest,
            "input_digest": input_digest,
            **checkpoint_body(outcome),
        }
        self._write("grouping_portion_finished", body, "after_grouping_portion_finished")

    def batch(
        self, level: int, index: str, answer: BatchAnswer, plan_digest: str, input_digest: str
    ) -> None:
        from evolution.tree.roles.grouping.consolidation import BatchCheckpoint, batch_body

        body = batch_body(BatchCheckpoint(level, index, input_digest, answer))
        body["index"] = body.pop("batch_id")
        body = {"grouping_id": self.grouping_id, "plan_digest": plan_digest, **body}
        self._write("grouping_batch_finished", body, "after_grouping_batch_finished")

    def _write(self, event: str, body: dict[str, Any], hook: str) -> None:
        campaign = self.campaign
        campaign.append(event, campaign.journal_value(body, campaign.secrets))
        campaign.hook(hook)


def _resumable_grouping(grouping: Any) -> bool:
    """A grouping whose checkpoints a new grouping may adopt (b16 §9).

    An interrupted grouping, or one a provider failure suspended; any other
    terminal ends what can be carried.
    """

    return grouping.status == "failed" and (
        grouping.code == "interrupted"
        or (grouping.code == "role_failed" and grouping.error_class == "provider")
    )


@dataclass(frozen=True)
class _Pending:
    """A decided `campaign_finished`, written once the terminal barrier is clear.

    `continuation` is the family-5 literal the deciding branch chose. Call sites
    still pass `"none"` for most terminals, but that value is read only by a
    family-5 lifecycle: `write_terminal` asks `_continuation`, which in family 6
    replaces it with `linked_run` and the terminal's own reason (continuable
    terminals §5.1). The literals are left in place, not refactored away, because
    the lifecycle refuses `kind: none` in family 6 anyway.
    """

    outcome: str
    reason: str
    continuation: str
    continuation_reason: str | None = None
    evidence: tuple[dict[str, Any], ...] | None = None
    #: Family 7 (I1 spec §5.1-§5.2): `True` once `_suspend_or_finish` turned the
    #: infrastructure terminal above into a `campaign_suspended{reason, owner,
    #: evidence}`; `reason` is then the suspension's, `suspension` its evidence.
    suspend: bool = False
    owner: str | None = None
    suspension: Mapping[str, Any] | None = None
    #: What a deciding site knows that a suspension's evidence needs: the
    #: threshold's crossing, and the stage's fault rows (each located by its
    #: `measurement_id`, with `retry_ordinal` and `code`). Unread below family 7.
    crossing: Mapping[str, Any] | None = None
    faults: tuple[Mapping[str, Any], ...] | None = None
    #: A deciding site that names its suspension itself (fix round 1): the
    #: reason, and the `(seq, operation_id)` of the one campaign record its
    #: `stage_record` cites -- an attestation refusal's binding or attesting
    #: record (`target_changed`), a probe setup's ready check
    #: (`probe_unavailable`).
    suspend_as: str | None = None
    record: tuple[int, str | None] | None = None
    #: Family 12 (ADR-0019 as amended 2026-10-02): a drift refusal's measurement
    #: journal prefix as it stood at the refusal, the `prefix` its
    #: `target_components_changed` evidence carries; from `0.3.0a27` also a
    #: `target_changed` refusal's at materialization (a27 spec §4.1).
    prefix: str | None = None
    #: Family 13 (design §6.13, D6): the cohort barrier's `cohort_rows`
    #: evidence. `_suspend_or_finish` suspends it as `cohort_incomplete` with
    #: the arbitrated `owner` the barrier sets, not a reason's fixed owner.
    cohort_rows: Mapping[str, Any] | None = None
    #: Families 13-14 (#194 spec §3.2): `close_measurement`'s `champion_rows`
    #: evidence. `_suspend_or_finish` suspends it as `champion_incomplete`
    #: with the arbitrated `owner`, as it does `cohort_rows`.
    champion_rows: Mapping[str, Any] | None = None

    @property
    def evidence_kind(self) -> str | None:
        return None if self.suspension is None else str(self.suspension["evidence_kind"])


#: The family-6 infrastructure terminals a threshold decides, by reason.
_THRESHOLD_REASONS = frozenset(reason for _outcome, reason in THRESHOLD_TERMINAL.values())
#: The suspension reasons whose evidence is the one record that decided them.
_STAGE_RECORD_REASONS = frozenset(
    reason for reason, (_owner, kind) in SUSPENSION_REASONS.items() if kind == "stage_record"
)
#: Family 14 (plan decision 8): what leaves a `chat` grouping's role call on
#: a route the existing code already owns, and so never becomes
#: `grouping_failed(role_failed, exception)` (`_Campaign._chat_role_call`).
_GROUPING_ROUTED_FAULTS: tuple[type[Exception], ...] = (
    GrouperRoleFailure,
    ValueError,
    TypeError,
    KeyError,
    WorkspaceError,
    OSError,
    _Fault,
    JournalBoundaryError,
)


def _fault_reason(row: Mapping[str, Any]) -> str:
    """The suspension reason one fault row names (I1 spec §3.1, §5.1).

    A missing primary metric is the scorer's; a target failure is structural
    (`target_changed`) when its code is a mismatch or an attestation refusal,
    and a contract violation otherwise -- including a row whose failure left no
    code, since only a contract or structural failure ends a try as a fault.
    """

    from evolution.tree.execution.call_failures import STRUCTURAL_CODES

    if row["reason"] == "scorer_metric_missing":
        return "scorer_metric_missing"
    if row["reason"] == "scorer_metric_out_of_range":
        return "scorer_metric_out_of_range"
    code = row.get("code")
    if code in STRUCTURAL_CODES or code == "attestation_mismatch":
        return "target_changed"
    return "target_contract_violated"


def _fault_rows_evidence(faults: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A `fault_rows` suspension's evidence over located fault rows, in schedule order."""

    keys = [
        (
            str(row["measurement_id"]),
            str(row["case_id"]),
            str(row["sample_id"]),
            int(row["retry_ordinal"]),
            str(row["reason"]),
        )
        for row in faults
    ]
    rows = [
        dict(
            measurement_id=key[0],
            case_id=key[1],
            sample_id=key[2],
            retry_ordinal=key[3],
            reason=key[4],
            code=None if row.get("code") is None else str(row["code"]),
        )
        for key, row in zip(keys, faults, strict=True)
    ]
    return dict(
        evidence_kind="fault_rows",
        count=len(rows),
        rows_digest=fault_rows_digest(keys),
        rows=rows[:MAX_FAULT_ROWS],
        truncated=len(rows) > MAX_FAULT_ROWS,
    )


def _classify(exc: BaseException, owner: str, measurement: str | None = None) -> _Fault | None:
    """Spec §7 / ruling 11: the one place a boundary error becomes a terminal.

    Only the typed capacity signals are `storage_capacity_exhausted`: *every* S2
    `ArtifactCapacityError` — the preflight estimate's
    `storage_capacity_insufficient` as much as the write-time
    `storage_capacity_exhausted` — and `JournalFault(code =
    "storage_capacity_exhausted")` from any of the three logs. Every other
    journal boundary error, `ArtifactError` or corruption is
    `persistence_failed {owner, code}`. A typed S2 *admission* refusal is neither:
    `run_identity_changed` from `create(expected=…)` and the seal refusals are
    ordinary answers the caller acts on, so they travel unchanged.
    """

    if isinstance(exc, ArtifactCapacityError):
        return _Fault("storage_capacity_exhausted", "artifact_store", exc.code, detail=exc.detail)
    if isinstance(exc, ArtifactStoreStopped):
        return _Fault("persistence_failed", "artifact_store", exc.code)
    if isinstance(exc, ArtifactAdmissionRefused):
        return None
    if isinstance(exc, ArtifactError):
        return _Fault("persistence_failed", "artifact_store", exc.code)
    if isinstance(exc, JournalBoundaryError):
        terminal = (
            "storage_capacity_exhausted"
            if isinstance(exc, JournalFault) and exc.code == "storage_capacity_exhausted"
            else "persistence_failed"
        )
        detail = exc.detail if isinstance(exc, ScorerTraceStorageFault) else None
        return _Fault(terminal, owner, exc.code, measurement, detail)  # type: ignore[arg-type]
    return None


class _Refused:
    """What `_Campaign._open_stage` returns once the target refused attestation."""


_REFUSED = _Refused()


@contextmanager
def _boundary(owner: str, measurement: str | None = None) -> Iterator[None]:
    """Classify every S1/S2/R1 boundary error raised inside the block (ruling 11)."""

    try:
        yield
    except _Fault:
        raise
    except (ArtifactError, JournalBoundaryError) as exc:
        fault = _classify(exc, owner, measurement)
        if fault is None:
            raise
        raise fault from exc


@contextmanager
def _file_boundary(code: str) -> Iterator[None]:
    """Classify the run's own private file work the way §7 classifies a sibling's.

    `project`, `freeze_projection`, `discard` and `write_snapshot` are file
    operations the campaign performs itself, outside the three logs: a private
    copy of the Parent for one reflection, and the environment documentation's
    snapshot. They raise `OSError` and `WorkspaceError`, neither of which
    `_boundary` recognises, so an unwritable run directory would leave the
    campaign as an unclassified exception out of `evolve()` instead of the one
    `persistence_failed` document §7 promises.

    The owner is `artifact_store`: these are copies of, and files beside, the
    published trees S2 owns, and §7's owner registry has no fourth name. `code`
    says which of the two failed, in the same grammar the sibling codes use.
    """

    try:
        yield
    except _Fault:
        raise
    except (OSError, WorkspaceError) as exc:
        raise _Fault("persistence_failed", "artifact_store", code) from exc


def _reflector_changed_projection(
    root: Path, control_prefix: str | None, frozen: Mapping[str, FileState]
) -> bool:
    """Whether a family-14 Reflector changed its frozen projection (F0 R67).

    The tree outside the role's own control subtree is captured again and
    compared with the capture taken right after `freeze_projection`: content,
    raw mode and kind of every entry, and the set of paths. A walk the
    no-follow capture refuses -- a symlink, FIFO or hard link the role made,
    an entry it made unreadable -- is a change too (routine choice 7), and so
    is a tree nested deeper than the recursive walk can descend: the walk's
    `RecursionError` is the role's own doing (`mkdir -p` under the writable
    control subtree suffices), not a campaign fault. The refusal's text, which
    may name the planted entry, is never kept.
    """

    try:
        return _without_control(capture_tree(root), control_prefix) != frozen
    except (WorkspaceError, OSError, RecursionError):
        return True


def _strategy_for(materialization: frozenset[str]) -> MaterializationStrategy:
    for strategy in _STRATEGY_ORDER:
        if strategy in materialization:
            return strategy
    raise IdentityError("run_identity_changed")


def _changed_paths(
    parent_root: Path, child_root: Path, policy: ArtifactPolicyV1
) -> tuple[str, ...]:
    """`artifact_policy.changes` over two freshly scanned roots (`_Campaign.candidate_leak`)."""

    return changes(
        scan_tree(parent_root, policy), scan_tree(child_root, policy), parent_root, child_root
    )


def _text_lines(path: Path) -> list[str] | None:
    """A path's text lines, or `None` for a missing, binary or unreadable file.

    Line endings are kept: the lines feed an ordered diff (`_added_lines`), and
    a line that gained or lost its terminator is a change of that line.
    """

    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, OSError):
        return None


def _added_lines(before: Sequence[str], after: Sequence[str]) -> list[str]:
    """The `+` lines of an ordered line diff of `before` into `after`.

    Set subtraction was wrong here and PDR-0007 says why: "the Candidate's own
    diff is refused at seal". A set discards multiplicity, so a Parent holding
    one line with an oracle needle and a Candidate adding an identical second
    one have equal line *sets* and the actual added line vanished -- the leak
    sealed (branch review round 1, finding `candidate-leak-set-diff`). An
    ordered diff keeps multiplicity: `insert` and `replace` opcodes name
    exactly the lines the Candidate added, in file order, once per copy.
    """

    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(after[j1:j2])
    return added


def _latest_identity_payload(records: Sequence[Envelope]) -> dict[str, Any]:
    """The creation payload, or the last `identity_rebased` payload (ADR-0008 amended).

    Both carry the same four fields the identity comparison reads — `identity`,
    `identity_digest`, `producing_version` and the adaptation slot — so one
    reader serves whichever record the prefix ends its identity on (PDR-0010).
    """

    latest = records[0].payload
    for record in records:
        if record.event == "identity_rebased":
            latest = record.payload
    return dict(latest)


def _check_visible_targets(
    diagnosis: Diagnosis,
    visible: Callable[[str], bool],
    history: BriefHistory | None = None,
) -> None:
    """Family 14's invariant guard at Editor brief assembly (H11 Task 6).

    Every diagnosis target path -- the brief's "Likely files", the current
    round's and each shown history round's -- must be visible to the roles.
    `validate_groups` admits a target only when the artifact matcher puts it in
    `writable_paths`, and the view applies that matcher to `readable ∪
    writable`, so this cannot fire for an admitted diagnosis; if it does, the
    journal or the code is wrong, and the message names no path.
    """

    targets = [path for group in diagnosis.groups for path in group.target_paths]
    if history is not None:
        for shown in history.rounds:
            if shown.diagnosis is not None:
                targets.extend(
                    str(path)
                    for group in shown.diagnosis.get("groups", ())
                    for path in group.get("target_paths", ())
                )
    if not all(visible(path) for path in targets):
        raise CampaignInvariantViolated("diagnosis target path is outside role visibility")


def _rebind_chain(measurements: Mapping[str, Mapping[str, Any]], mid: str) -> tuple[str, ...]:
    """`mid` and every frozen measurement it was re-bound from, newest first.

    I2 spec §5.3: a `continue` re-binds an unfinished measurement as
    `<id>@e<k>` with `rebound_from` naming its frozen predecessor. The one
    walk promotion admission (`attested_chain`), a cohort completion's
    `evidence` (`attestation_evidence`) and a family-13 attestation refusal's
    record (`target_record`) read.
    """

    chain = [mid]
    while (source := measurements[chain[-1]].get("rebound_from")) is not None:
        chain.append(str(source))
    return tuple(chain)


# -- the executor ---------------------------------------------------------


#: #290 §4.4: the reason an operator line names when the runner, not the
#: role, ended a chat role's invocation: the role deadline is the retry
#: layer's own `deadline_exhausted`; a stop is the classifier's `cancelled`;
#: a task still alive after its grace is the outcome's `unresponsive`.
_LIFECYCLE_ATTEMPT_REASONS: Final = {
    "timeout": "deadline_exhausted",
    "cancelled": "cancelled",
    "unresponsive": "unresponsive",
}


def _role_attempts(attempts: AttemptRecorder, error_class: str | None) -> AttemptReport | None:
    """One Editor Attempt's or reflection's attempt report, read from the
    executor's own recorder whatever ended the role task (#290 §4.4).

    A deadline or a stop that cancelled the task is a failed invocation: its
    one line lists the attempts already made and names the lifecycle reason,
    unless a provider failure already ended it."""

    reason = _LIFECYCLE_ATTEMPT_REASONS.get(error_class or "")
    if reason is not None and attempts.failure is None:
        attempts.fail(reason)
    return attempts.report()


def _fire(process_log: Callable[[Path], None] | None, root: Path) -> None:
    """ADR-0036: signal this invocation's last refusal point; never a failure."""
    if process_log is not None:
        try:
            process_log(root)
        except Exception:  # a log is never a campaign failure
            pass


class _Campaign:
    """One campaign session over one run root; every public entry point uses it."""

    def __init__(
        self,
        policy: CampaignPolicyV11,
        *,
        run_root: Path,
        seed_root: Path,
        artifact_policy: ArtifactPolicyV1,
        adapter: PromotionCapableAdapter,
        environment: Environment,
        scorer: Scorer,
        editor: CandidateEditor,
        reflector: CandidateReflector,
        environment_docs: EnvironmentDocs | _V2DocumentSnapshot | None,
        environment_docs_repair: bool = False,
        corpus: Sequence[EvalCase],
        cancelled: Callable[[], bool],
        secrets: Sequence[str],
        hooks: Mapping[str, Callable[[], None]] | None,
        secret_paths: Sequence[str] = (),
        secret_names: Sequence[str | None] = (),
        adaptive: AdaptiveCampaignRuntimeConfig | None = None,
        probe: ProbeSection | None = None,
        authorizations: Sequence[Authorization] = (),
        hygiene_applications: Sequence[tuple[HygieneFinding, Authorization]] = (),
        pending_findings: Sequence[HygieneFinding] = (),
        family_version: int | None = None,
        connection: ConnectionContract | None = None,
        process_log: Callable[[Path], None] | None = None,
        notes: Callable[[str], None] | None = None,
        bind_hits: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.bind_hits = tuple(dict(hit) for hit in bind_hits)
        self.policy = policy
        #: The caller's sink for a remark about this invocation (ADR-0040 D8's
        #: whole-run inode note); `None` drops it.
        self.notes = notes
        #: `CampaignResult.provider_attempt_lines`, in the order they occurred.
        self._provider_attempt_lines: list[str] = []
        self.run_root = Path(run_root)
        self.seed_root = Path(seed_root)
        self.artifact_policy = artifact_policy
        self.adapter = adapter
        self.environment = environment
        self.scorer = scorer
        self.editor = editor
        self.reflector = reflector
        self.environment_docs = environment_docs
        self.corpus = tuple(corpus)
        #: ADR-0038: `/15`'s declared verbatim answers; every needle set below
        #: drops an oracle value equal to or contained in one of them.
        self.declared_answers: tuple[str, ...] = (
            ()
            if connection is None or connection.verbatim_answers is None
            else connection.verbatim_answers.strings
        )
        self.cancelled = cancelled
        self.secrets = tuple(secrets)
        #: #287: the manifest path of each secret's name, index for index; the
        #: connection probe names a matched one by it, never by its value.
        self.secret_paths = tuple(secret_paths)
        #: The declared environment name beside each path (`None` for an
        #: undeclared bound value), for the order by sorted name in a role.
        self.secret_names = tuple(secret_names)
        self.hooks: Mapping[str, Callable[[], None]] = dict(hooks or {})
        # The public boundary admits this explicit current-family input before
        # any write. Keeping it on the session gives creation and resume the
        # same capacity and identity admission point.
        assert adaptive is not None
        self.adaptive = adaptive
        self.connection = connection
        # Creation takes the family its policy implies; resume passes the
        # journal's own, which its creation policy chose (I1 spec §6).
        self._family_version = (
            _runtime_family_version(policy, adaptive) if family_version is None else family_version
        )
        self.checks = (
            (connection.checks or ENFORCE_ALL)
            if self._family_version == 15 and connection is not None
            else ENFORCE_ALL
        )
        self.secret_oracle_paths: tuple[str, ...] | None = (
            connection.secret_oracle_paths.pointers
            if self._family_version == 15
            and connection is not None
            and connection.secret_oracle_paths is not None
            else None
        )
        # Every oracle string of the corpus, longest first, less the declared
        # answers: the needle set the Candidate seal and attempt feedback scan
        # against (spec §5).
        self.needles = (
            oracle_needles(self.corpus, self.declared_answers)
            if self.secret_oracle_paths is None
            else oracle_needles(
                self.corpus, self.declared_answers, secret_paths=self.secret_oracle_paths
            )
        )
        # Grouping output, the Reflector's note and its claimed groups are
        # scanned against this set before any of it becomes durable (spec
        # §3.3). From family 12 it omits the oracle strings equal to an
        # identifier the Editor already reads (#172); earlier families keep
        # `self.needles` itself. Every family drops the declared answers, as
        # `self.needles` does. Family 14 narrows it per frozen baseline
        # (`_reflection_needles_for`), so its sites never read this directly.
        self.reflection_needles = reflection_needles(
            self.corpus,
            family_version=self._family_version,
            declared=self.declared_answers,
            secret_paths=self.secret_oracle_paths,
        )
        self.probe = probe
        self._probe_startups: dict[str, str] = {}
        self.active_probe_session: ProbeSession | None = None
        self.active_probe_mediator: ProbeMediator | None = None
        self.log: AppendLog | None = None
        self.store: ArtifactStore | None = None
        self.identity: CampaignIdentityV1 | None = None
        self.run_id = ""
        self.seed_mode = "exact"
        self.strategy: MaterializationStrategy = _STRATEGY_ORDER[-1]
        self.expected_semantic: Any = None
        self.measurements: dict[str, Measurement] = {}
        # Task 10: the workspace `run_editor` materialized for an attempt, kept
        # because `ArtifactStore.materialize_candidate` refuses a second call
        # for the same attempt -- `candidate_leak` reads this path rather than
        # re-materializing. Empty after a resume; `reconcile_attempts` finds
        # such an attempt already published and reads the published copy
        # instead (spec §5.6, PDR-0007).
        self.workspaces: dict[str, Path] = {}
        self.brief_cache: tuple[int, EditBrief] | None = None
        self.adaptive_inputs_cache: tuple[str, FrozenReflectionInputs] | None = None
        #: Family 14 only: the reflection-side needle set of one frozen
        #: baseline, keyed by its `baseline_binding` (`_reflection_needles_for`).
        self.baseline_needles_cache: tuple[str, tuple[str, ...]] | None = None
        self.attempt_seal_hits: dict[str, HitSink] = {}
        #: Family 14 (#228): closed Attempts' history facts, memoized per
        #: process; a closed round never changes, and the memo changes no byte.
        self.history_memo: dict[MemoKey, AttemptFacts] = {}
        self.siblings: dict[str, Birth] = {}
        self.stopping = False
        self.unresponsive = False
        #: The `campaign_suspended` payload this session wrote (family 7).
        self.suspended: dict[str, Any] | None = None
        self.editor_alive = False
        self.reflector_alive = False
        # PDR-0010. The decisions this invocation was handed, the rebase they
        # admitted (decided in `adopt`, written in `_write_consequences`), every
        # producing version the run has admitted, and the `store_rebased` the
        # ledger owes — either for the rebase about to be written, or for one an
        # earlier invocation declared and crashed before completing (§6.2).
        self.authorizations = tuple(authorizations)
        self.hygiene_applications = tuple(hygiene_applications)
        # Comparable-links spec §6: the run directory's collected hygiene
        # findings no decision accepted. They are reported beside the identity
        # or link finding, and while any remains nothing is written.
        self.pending_findings = tuple(pending_findings)
        # Baseline inheritance §5.1-§5.2 (reading 3): the `sample_imported`
        # payloads a fresh inheriting run declared at creation and owes its
        # first-round baseline's birth. Held in memory, never journaled; a
        # resume before the birth has none (`owed_import`).
        self.pending_import: tuple[Mapping[str, Any], ...] = ()
        self.pending_diagnostics: Any = None
        self.pending_diagnostic_data: dict[str, Any] | None = None
        self.diagnostic_staging: Any = None
        # Baseline inheritance §5.2 (reading 3): the predecessor's run
        # directory a resume re-reads when the copy is still owed, and the
        # accepted `fresh_baseline` decision `adopt` matched for a lost source
        # (§5.3), written by `_write_consequences`.
        self.linked_from_path: Path | None = None
        self.pending_fresh_baseline: tuple[Authorization, str, str] | None = None
        # Comparable-links spec §5.2: set once an accepted close or terminal
        # has been written by `adopt`, so `resume_campaign` reports the
        # campaign without entering the drive loop.
        self.closed = False
        self.pending_rebase: (
            tuple[
                CampaignIdentityV1,
                dict[str, Any],
                tuple[RefusalDifference, ...],
                str,
                Authorization,
            ]
            | None
        ) = None
        # I2 spec §5.4: the in-place condition change `adopt` matched (or found
        # open in the prefix and must finish), written by `_write_consequences`:
        # the rebuilt identity and record, the differences, the instance, the
        # decision and its effect. `change_resumed` says the block's
        # `authorization_applied` is already durable (recovery, no decision).
        self.pending_change: (
            tuple[
                CampaignIdentityV1,
                dict[str, Any],
                tuple[RefusalDifference, ...],
                str,
                Authorization,
                str,
            ]
            | None
        ) = None
        self.change_resumed = False
        # Set once this invocation may have written anything durable: its first
        # campaign append, or the admission scan that may repair a torn tail.
        # An I2 refusal type raised after it is a fault, not a refusal
        # (`raised_after_first_write`).
        self.wrote = False
        self.process_log = process_log
        self._process_log_fired = False
        # I2 spec §3.2 row 2, §5.4: a matched `rebaseline` (any family) -- or,
        # on family 15, also a `close` or `continue` (#263, ADR-0044) -- over a
        # round whose selection is already durable, held (same shape as
        # `pending_change`) until `drive` has finished that round in the old
        # epoch; `drive` then writes the decision at the boundary
        # (`_release_held_change`).
        self.held_change: (
            tuple[
                CampaignIdentityV1,
                dict[str, Any],
                tuple[RefusalDifference, ...],
                str,
                Authorization,
                str,
            ]
            | None
        ) = None
        # #263: the identity records a held family-15 `close` passes to
        # `_write_close` at the round boundary -- `(journaled, rebuilt)`, the
        # triples its `authorization_applied` carries. `None` otherwise.
        self.held_close: tuple[dict[str, Any], dict[str, Any]] | None = None
        # #268: a held close over an artifact-store change completes its
        # selected round under the recorded store identity and policy.
        self.settlement_store_identity: ArtifactStoreIdentityV1 | None = None
        # Family 15 (#269): admission consequences and caller cancellation
        # wait until a durable selection has completed its exact round.
        self.deferred_consequences: list[Callable[[], None]] = []
        self.deferred_cancel: bool = False
        # The store identity `adopt` admitted, for the ledger half of a
        # block `drive` writes after the store is open (a held `rebaseline`).
        self.store_identity: ArtifactStoreIdentityV1 | None = None
        # I2 spec §5.3: the carried rows of each re-bound measurement, by its id,
        # recomputed from the frozen source and checked against the declaration
        # before the birth (`rebound_import`).
        self.rebound_rows: dict[str, tuple[Mapping[str, Any], ...]] = {}
        # G2 spec §3.2, §5: the verified carry of each family-12 completion, by
        # its id, cached between the bind (or an owed birth's read-only
        # re-read) and the birth (`completion_import`).
        self.completion_rows: dict[str, tuple[Mapping[str, Any], ...]] = {}
        # G2b3b: each side's reference map of a component-mode Attempt, by
        # `(attempt_id, side)`; fixed once the Attempt's selection is durable.
        # A family-13 Champion cohort member's is the round's, by
        # `(str(round), "champion")` (F7 plan choice 10).
        self.drift_references: dict[tuple[str, str], DriftReference | None] = {}
        # Measurements whose opened stage a component-drift refusal left open
        # and undispatched this session (`leave_drifted_stage_open`).
        self.drifted_open: set[str] = set()
        self.admitted_versions: frozenset[str] = frozenset()
        self.owed_store_rebases: list[dict[str, str]] = []
        # Set once the born store has been re-opened, so `adopt` can resume it
        # early -- an owed `store_rebased` is written before any new consequence
        # record -- and `adopt_siblings` then knows not to open it twice (§6.2).
        self.store_resumed = False
        # §6.2: admission *read* the environment-documentation snapshot and
        # found the stored copy missing or damaged while the live source still
        # had the journaled digest. Admission never repairs it; `adopt` does,
        # after its consequence records, so a refusal leaves the run directory
        # untouched (ADR-0015).
        self.environment_docs_repair = bool(environment_docs_repair)
        self.pending: _Pending | None = None
        self.fault: _Fault | None = None
        self.error: BaseException | None = None
        self.tasks: dict[str, asyncio.Task[None]] = {}
        # Family-14 design R58: this controller invocation's own id. It is
        # minted here and never read from a prefix, so a resume is a new
        # invocation whose admissions a crash cannot reuse (C§4.1).
        self.invocation_id = uuid4().hex
        # R57, R59: the admission token of each connection binding `D` this
        # invocation may dispatch under -- a compatible preflight's, or a
        # binding-only spent one once `D`'s boundary is already durable.
        self.connection_tokens: dict[str, ConnectionAdmission] = {}
        # Single flight (choice 3): one probe at a time, whatever binding;
        # concurrent Attempts reaching dispatch wait for its result.
        self.connection_lock = asyncio.Lock()
        # R61, ruling 5: set once the gate has written a failed preflight's
        # terminal and its suspension; `write_terminal` writes nothing more.
        self.connection_suspended = False

    # -- plumbing ---------------------------------------------------------

    @property
    def store_hooks(self) -> dict[str, Callable[[], None]]:
        return {name: hook for name, hook in self.hooks.items() if name not in CAMPAIGN_HOOKS}

    def _reach_last_refusal_point(self) -> None:
        """Open the CLI's process log once this invocation may write (ADR-0036)."""
        if not self._process_log_fired:
            self._process_log_fired = True
            _fire(self.process_log, self.run_root)

    def hook(self, name: str) -> None:
        callback = self.hooks.get(name)
        if callback is not None:
            callback()

    def snapshot(self) -> CampaignSnapshot:
        assert self.log is not None
        state = self.log.state
        assert isinstance(state, CampaignLifecycle)
        return state.snapshot

    @property
    def envelope_family(self):
        """The exact closed envelope family selected by the private assembly."""

        return campaign_family_for(self._family_version)

    @property
    def family_version(self) -> int:
        return self._family_version

    @property
    def model_grouping(self) -> bool:
        """Family 9 (ADR-0022): the model error grouping lifecycle."""

        return True

    @property
    def resilience(self) -> bool:
        """Family 10 (PDR-0019, ADR-0023): the role resilience lifecycle."""

        return True

    def round_closed_attempts(self, state: RoundState) -> dict[str, int]:
        """The mandatory number of Attempts opened before this round closed.

        This is zero when a failed or declined Reflector closes the round before
        any Attempt.  Every supported campaign family records the same field.
        """

        return {"attempts_opened": len(state.attempts)}

    def round_reason(self) -> dict[str, Any]:
        """Family 9's `round_closed.reason` of every close but `unplanned`: `null`.

        Earlier families' records carry no such key, byte for byte.
        """

        return {"reason": None}

    def view(self) -> CampaignView:
        return assemble(
            self.run_root,
            self.snapshot(),
            self.measurements,
            self.store,
            siblings=self.siblings,
            primary=self.policy.metric_primary,
            global_preflights=self.independent_global_preflights(),
            lacking=(self.lacking_attempts()),
            rebound_held=(self.rebound_held_attempts()),
        )

    def independent_global_preflights(self) -> dict[int, Any]:
        from evolution.tree.family15_report import global_preflight

        if self.family_version != 15 or self.policy.global_mode == "off":
            return {}
        snapshot = self.snapshot()
        result = {}
        for number, state in snapshot.rounds.items():
            mid = state.baseline_measurement_id
            if (
                state.closed is not None
                or not state.baseline_complete
                or state.grouping is not None
                or mid is None
            ):
                continue
            result[number] = global_preflight(
                snapshot,
                self.policy,
                number,
                self.independent_measurement(mid, state.baseline_reference),
                self.cited_baseline_rows(number, mid),
            )
        return result

    def lacking_attempts(self) -> frozenset[str]:
        """Attempts whose latest stage ended with exhausted rows (held).

        #91 spec §3.1, ruling R13: `campaign_view.held_attempts` over this
        process's open measurements -- the one selection the read-only
        `inspect_view` applies too (#114). A measurement this process has not
        opened names nothing (a closed campaign's report opens none).
        """

        return held_attempts(
            self.snapshot(),
            lambda mid, horizon: self.measurements[mid].sample_rows(horizon),
            lambda mid: self.measurements[mid].primary_range,
            lambda mid: self.measurements[mid].max_repeat,
            self.measurements,
            self.policy.looks,
            self.policy.metric_primary,
        )

    def rebound_held_attempts(self) -> frozenset[str]:
        """Re-bound, still stageless Attempts whose frozen source was held (R17).

        `campaign_view.rebound_held_attempts` over this process's measurements,
        each source's rows judged under `range_of` -- the one selection the
        read-only `inspect_view` applies too (#114).
        """

        return rebound_held_attempts(
            self.snapshot(),
            lambda mid, horizon: self.measurements[mid].sample_rows(horizon),
            self.range_of,
            lambda mid: self.measurements[mid].max_repeat,
            self.measurements,
            self.policy.looks,
            self.policy.metric_primary,
        )

    def stage_admission(self, attempt: AttemptState, look: int) -> StageAdmission:
        """The stage deciding `look`, on the Candidate and fresh Parent sides,
        as `confidence_gate.stage_admission` admits it: the one check the
        audit re-derives before it recomputes a family-13 look or calibration
        (codex branch round 1, ADR-0027).

        Each side is read whole for its fault rows (`attempt_fault_rows`'
        set) and to the stage's horizon for its held rows (design §5): the
        look, or on the last look a `case_repeats` Parent's last repeat. The
        stage's floor stays the declared look before it.
        """

        measurements = [
            self.measurements[mid]
            for mid in (attempt.measurement_id, attempt.parent_measurement_id)
            if mid is not None
        ]
        horizon = stage_horizon(
            self.policy.looks, look, max(measurement.max_repeat for measurement in measurements)
        )
        return stage_admission(
            [
                (
                    _owed_sample_rows(
                        self.snapshot(),
                        measurement.measurement_id,
                        measurement.sample_rows(measurement.max_repeat),
                    ),
                    measurement.primary_range,
                )
                for measurement in measurements
            ],
            horizon=horizon,
            floor=stage_floor(self.policy.looks, look),
            primary=self.policy.metric_primary,
            lacking=held_repair_required(self.snapshot().producing_version),
        )

    def lacking_at(self, attempt: AttemptState, look: int) -> bool:
        """Does the stage deciding `look`, on the Candidate or fresh Parent side,
        leave the look without pairs no try will fill? Rulings R13, R16: what
        holds a settled Attempt; an earlier look's rows do not.

        Under a producing version exempt from held repair, an exhausted row
        (`stage_exhausted`); from `0.3.0a27` (#120) any row of the stage that
        did not score (`stage_lacking`), a forced stop's `cancelled` rows
        included -- the stage test of `campaign_view.held_attempts`' rule.
        The held rows of `stage_admission`, the check the audit shares.
        """

        return bool(self.stage_admission(attempt, look).held)

    def journal_value(self, value: Any, secrets: Sequence[str]) -> Any:
        """Redact role/Target text, refusing an unrepresentable fixed marker."""
        redacted = redact_for_journal(value, secrets)
        if secret_match({"payload": redacted}, secrets) is not None:
            raise _Fault(
                "persistence_failed",
                "campaign_journal",
                "record_matches_secret",
                detail="the fixed redaction marker still matches a run secret",
            )
        return redacted

    def prove(self, event: str, payload: dict[str, Any], operation_id: str | None) -> None:
        """Offer the record to the lifecycle's own `prepare` before the log sees it.

        `prepare` mutates nothing — the append log commits the delta only after
        fsync — so this is a pure admission check. A refusal is a `SchemaError`
        naming the rule, and raising `CampaignInvariantViolated` here keeps
        `AppendLog.append` from turning that bug into a poisoned writer and a
        `JournalFault` the §7 classification would call `persistence_failed`.
        """

        assert self.log is not None
        with self.log.guard() as state:
            try:
                record = Envelope.from_payload(
                    dict(
                        schema_version=1,
                        run_id=self.run_id,
                        sequence=state.sequence + 1,
                        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        event=event,
                        operation_id=operation_id,
                        payload=payload,
                    ),
                    secrets=self.log.secrets,
                    family=self.envelope_family,
                )
                state.prepare(record)
            except SchemaError as exc:
                raise CampaignInvariantViolated(str(exc)) from None

    def prove_envelope(self, record: Envelope) -> None:
        """Offer an already-built envelope to the lifecycle's own `prepare`.

        `create` admits the creation envelope once, in `admit_creation`, and
        must prove *that* record, not a fresh one built from its payload —
        a second build repeats `admit_creation`'s own secret check and
        timestamp, and a secret hit in it would raise after `open_log` has
        already created the run root. This mirrors `prove` exactly, over the
        caller's own envelope instead of one built from parts.
        """

        assert self.log is not None
        with self.log.guard() as state:
            try:
                state.prepare(record)
            except SchemaError as exc:
                raise CampaignInvariantViolated(str(exc)) from None

    def append(
        self,
        event: str,
        payload: dict[str, Any],
        operation_id: str | None = None,
        *,
        shadowed: list[dict[str, Any]] | None = None,
    ) -> _Written:
        if event == "attempt_closed" and self.family_version == 15:
            seal_fields = self._seal_shadow_fields(str(payload["attempt_id"]))
            if seal_fields:
                shadowed = [*(shadowed or []), *seal_fields["shadowed"]]
        if shadowed:
            payload = {**payload, "shadowed": shadowed}
        assert self.log is not None
        op = operation_id
        # Family 7's operationless set is the older one plus `measurement_rebound`,
        # a record no earlier family can write, so it serves every family.
        operationless = (
            self.envelope_family.no_operation_events
            if self.family_version == 15
            else SUSPENDABLE_NO_OPERATION_EVENTS
        )
        if op is None and event not in operationless:
            op = uuid4().hex
        # `prove` reads the log's own state through `guard()`, which re-checks the
        # writer's health, so it belongs *inside* the boundary: once the campaign
        # log is unhealthy the terminal's own append must still come back as a
        # typed `_Fault` and not as a bare `JournalFault` past every handler.
        with _boundary("campaign_journal"):
            self.prove(event, payload, op)
            start = self.log.bytes
            self.wrote = True
            self._reach_last_refusal_point()
            self.log.append(self.run_id, event, payload, op)
            record = self.log.snapshot().records[-1]
        written = _Written(start, self.log.bytes, record, op)
        if event == "identity_rebased" and _BIND_HIT_FIELDS.intersection(payload["changed_fields"]):
            self._write_bind_hits()
        return written

    def _write_bind_hits(self) -> None:
        """Complete the latest binding's deterministic batch, including a torn batch."""
        if self.family_version != 15 or not self.bind_hits:
            return
        assert self.log is not None
        records = self.log.snapshot().records
        anchor = max(
            index
            for index, record in enumerate(records)
            if record.event == "campaign_created"
            or (
                record.event == "identity_rebased"
                and _BIND_HIT_FIELDS.intersection(record.payload["changed_fields"])
            )
        )
        written = [
            record.payload
            for record in records[anchor + 1 :]
            if record.event == "check_shadowed"
            and record.payload["round"] is None
            and record.payload["operation_id"] is None
        ]
        for hit in self.bind_hits:
            payload = {**hit, "round": None, "operation_id": None}
            if payload not in written:
                self.append("check_shadowed", payload)
                written.append(payload)

    def stop(self, pending: _Pending) -> None:
        """Decide a terminal; it is written after the §7 terminal barrier.

        In family 7 the infrastructure terminal becomes a suspension here
        (`_suspend_or_finish`), after the precedence rules have chosen it, so
        every deciding site suspends through this one door.
        """

        evidence = self.probe_failure_evidence()
        if evidence:
            pending = _Pending(
                "infrastructure_failed", "probe_unavailable", "none", evidence=evidence
            )
        if self.pending is None or evidence:
            self.pending = self._suspend_or_finish(pending)
        self.stopping = True

    def _decide_stop_is_cancellation(self) -> bool:
        """Family 10: does the stop that cut a running role short end the
        campaign `stopped`/`cancelled` or `stopped`/`condition_changed`?

        The role's lifecycle terminal names what stopped the campaign (spec
        §4.1, §5.3, ADR-0023): `cancelled` for those two stops only (ADR-0008),
        `interrupted` for every other, a suspension included. The answer must
        agree with the terminal the barrier will write, which the audit checks
        against it, so a caller's token seen before any terminal is decided
        decides the cancellation here, exactly as the drive loop would; a
        terminal decided first stands (`stop`), and then the token alone does
        not make the stop a cancellation.
        """

        if self.pending is None and self.cancelled():
            self.stop(_Pending("stopped", "cancelled", "none"))
        pending = self.pending
        return (
            pending is not None
            and not pending.suspend
            and (pending.outcome, pending.reason) in CANCELLATION_TERMINALS
        )

    def _role_stop_class(self, error_class: str) -> str:
        """A role outcome's `error_class` as the barrier writes it: family 10
        turns a stop's `cancelled` into `interrupted` unless the stop is a
        cancellation (`_decide_stop_is_cancellation`); earlier families keep it."""

        if error_class == "cancelled" and (not self._decide_stop_is_cancellation()):
            return "interrupted"
        return error_class

    def _suspend_or_finish(self, pending: _Pending) -> _Pending:
        """The family-7 suspension a decided infrastructure terminal becomes (I1 §5.1).

        Families up to 6 get `pending` back unchanged, and so does every
        terminal that is not an infrastructure cause (`completed`, `stopped`,
        an applied close). Otherwise the reason and evidence come from what the
        deciding site knew and the prefix holds:

        - a threshold terminal carries its `crossing` over the owner's window,
          and `window_start` is where that window starts: the latest
          suspension's sequence, or the later `identity_rebased` of an
          in-place change of the owner's field (I2 spec §6.5);
        - a fault terminal carries the stage's located fault rows; the reason
          is the first row's (schedule order), as family 6 names the first;
        - `grouping_failed`, `probe_unavailable` and the two unresponsive
          roles name the durable record that decided them, the oldest such
          record since the latest suspension (`suspension_causes`).

        A cause this cannot evidence is left as it was, and the lifecycle then
        refuses it -- an executor invariant, never a silent terminal.
        """

        if pending.suspend:
            return pending
        if pending.outcome != "infrastructure_failed" and (pending.outcome, pending.reason) != (
            "budget_exhausted",
            "rollout_time_budget_exhausted",
        ):
            return pending
        if pending.champion_rows is not None:
            # #194 spec §3.2: a Champion measurement's gaps; like the cohort's,
            # its owner is arbitrated over its rows.
            return replace(
                pending,
                reason=CHAMPION_INCOMPLETE,
                suspend=True,
                owner=pending.owner,
                suspension=dict(pending.champion_rows),
            )
        if pending.cohort_rows is not None:
            # Family-13 design §6.13 (D6): the cohort's own suspension; its
            # owner is arbitrated over its rows, not fixed by its reason.
            return replace(
                pending,
                reason=campaign_vocabulary_for(self.family_version).incomplete_reason,
                suspend=True,
                owner=pending.owner,
                suspension=dict(pending.cohort_rows),
            )
        snapshot = self.snapshot()
        reason = pending.reason
        evidence: dict[str, Any] | None = None
        if pending.suspend_as is not None and pending.record is not None:
            reason = pending.suspend_as
            seq, operation = pending.record
            evidence = dict(evidence_kind="stage_record", seq=seq, operation_id=operation)
            if pending.prefix is not None:
                evidence["prefix"] = pending.prefix
        elif reason in _THRESHOLD_REASONS:
            crossing = pending.crossing
            if crossing is None and snapshot.threshold is not None:
                crossing = {
                    name: snapshot.threshold[name]
                    for name in ("owner", "kind", "count", "denominator", "max_count", "max_rate")
                }
            if crossing is not None:
                evidence = dict(
                    evidence_kind="threshold",
                    crossing=dict(crossing),
                    window_start=snapshot.window_start_for(str(crossing["owner"])),
                )
        elif reason in _STAGE_RECORD_REASONS:
            causes = _outstanding_causes(snapshot, reason)
            if causes:
                seq, operation = causes[0]
                evidence = dict(evidence_kind="stage_record", seq=seq, operation_id=operation)
        elif pending.faults:
            reason = _fault_reason(pending.faults[0])
            evidence = _fault_rows_evidence(pending.faults)
        if evidence is None:
            return pending
        return replace(
            pending,
            reason=reason,
            suspend=True,
            owner=SUSPENSION_REASONS[reason][0],
            suspension=evidence,
        )

    def attempt_faults(self, attempt: AttemptState) -> tuple[dict[str, Any], ...] | None:
        """Family 7: the located fault rows of a closed Attempt's measurement.

        The family-7 executor keeps an Attempt with fault rows open and
        suspends on them (`attempt_stage`), so this is reached only by an
        Attempt `evaluate` closed `scorer_metric_missing`; the round-level
        suspension still rests on its rows. `None` below family 7.
        """

        measurement_id = attempt.measurement_id
        if measurement_id is None or measurement_id not in self.measurements:
            return None
        rows = self.measurements[measurement_id].fault_rows(self.policy.metric_primary)
        return self.located_faults(measurement_id, rows)

    def attempt_fault_rows(self, attempt: AttemptState) -> tuple[dict[str, Any], ...]:
        """#97 spec §4.1: the located fault rows of an Attempt's measurements.

        The Candidate's and, in family 12, the fresh Parent's -- the rows the
        stage barrier would have suspended on.
        """

        primary = self.policy.metric_primary
        located: list[dict[str, Any]] = []
        for mid in (attempt.measurement_id, attempt.parent_measurement_id):
            if mid is not None and mid in self.measurements:
                located.extend(self.located_faults(mid, self.measurements[mid].fault_rows(primary)))
        return tuple(located)

    def repair_rows(self, measurement_id: str) -> frozenset[tuple[str, str]]:
        """Family 7: the `(case_id, sample_id)` rows of a measurement owed a repair try.

        I1 spec §5.4: every fault row the latest `campaign_suspended` names --
        when it is a `fault_rows` suspension -- gets exactly one repair try, the
        next ordinal past the named one. A row owes it while its latest try is
        still the one named; a repair already started (or finished) owes
        nothing. When the evidence was truncated, the complete named set is
        re-derived from the measurement prefix (`owed_repairs`). Empty below
        family 7 and after any other kind of suspension. The planner reads the
        very same set off the view (`CampaignView.repairs`), so the two can
        never disagree about what is owed.
        """

        return owed_repairs(
            self.snapshot(), self.measurements[measurement_id], self.policy.metric_primary
        )

    def located_faults(
        self, measurement_id: str, faults: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        """A measurement's fault rows, each named by its `measurement_id`."""

        return tuple({**row, "measurement_id": measurement_id} for row in faults)

    # -- references -------------------------------------------------------

    def ledger_reference(self, match: Callable[[Envelope], bool]) -> dict[str, Any] | None:
        """A `ledger_record` reference over `artifacts.jsonl`, by byte range.

        The **last** match wins: a seal re-run at recovery publishes through a
        second operation pair, and the reference must name the record that stands.
        """

        if self.store is None:
            return None
        offset = 0
        found: dict[str, Any] | None = None
        for record in self.store.ledger.snapshot().records:
            encoded = record.encode()
            if match(record):
                found = _durable_reference.DurableReference(
                    version=1,
                    kind="ledger_record",
                    run_id=self.store.run_id,
                    locator=f"{LEDGER_FILE}#bytes={offset}:{offset + len(encoded)}",
                    content_digest=hashlib.sha256(encoded).hexdigest(),
                ).to_payload()
            offset += len(encoded)
        return found

    def publication_reference(self, attempt_id: str) -> dict[str, Any] | None:
        """S2 publishes a Candidate with a started/finished pair; the terminal
        record carries only `latency_ms`, so the copy is identified through the
        operation id its `artifact_copy_started` opened."""

        if self.store is None:
            return None
        operations = {
            record.to_payload()["operation_id"]
            for record in self.store.ledger.snapshot().records
            if record.event == "artifact_copy_started"
            and record.payload["kind"] == "candidate"
            and record.payload["artifact_id"] == attempt_id
        }
        return self.ledger_reference(
            lambda record: (
                record.event == "artifact_copy_finished"
                and record.to_payload()["operation_id"] in operations
            )
        )

    def decision_reference(self, written: _Written) -> dict[str, Any]:
        return dict(
            version=1,
            kind="journal_record",
            run_id=self.run_id,
            locator=f"{CAMPAIGN_FILE}#bytes={written.start}:{written.end}",
            content_digest=hashlib.sha256(written.record.encode()).hexdigest(),
        )

    def stored_decision_reference(self, round_number: int) -> dict[str, Any]:
        """Rebuild a durable `selection_decided` record's reference (spec §8)."""

        assert self.log is not None
        offset = 0
        for record in self.log.snapshot().records:
            encoded = record.encode()
            if record.event == "selection_decided" and record.payload["round"] == round_number:
                return self.decision_reference(
                    _Written(offset, offset + len(encoded), record, None)
                )
            offset += len(encoded)
        raise ValueError("the round has no durable selection")

    def record_reference(self, seq: int) -> dict[str, Any]:
        """A `journal_record` reference over this campaign's own record at `seq`.

        Family 3's two derived rows cite records of the very log they are
        appended to: a `gate_scope_selected` cites the reflection terminal it
        read, and a focus-less `round_closed` cites the `focus_selected` that
        decided it. The byte range is the log's own -- the same accumulation
        `stored_decision_reference` uses -- so an audit resolves it exactly as
        it resolves a `selection_decided` reference.
        """

        assert self.log is not None
        offset = 0
        for record in self.log.snapshot().records:
            encoded = record.encode()
            if record.to_payload()["sequence"] == seq:
                return self.decision_reference(
                    _Written(offset, offset + len(encoded), record, None)
                )
            offset += len(encoded)
        raise CampaignInvariantViolated("the campaign has no record at that sequence")

    def _record_sequence(self, event: str, **match: Any) -> int | None:
        """The sequence of this campaign's last `event` record whose payload
        holds every `match` item, or `None` when it has none."""

        assert self.log is not None
        for record in reversed(self.log.snapshot().records):
            if record.event == event:
                payload = record.payload
                if all(payload.get(key) == value for key, value in match.items()):
                    return int(record.to_payload()["sequence"])
        return None

    def prefix(self, measurement_id: str, *, round_number: int | None = None) -> dict[str, Any]:
        """Current journal head, or an explicitly selected round's frozen baseline."""
        if round_number is not None:
            state = self.snapshot().rounds[round_number]
            if state.baseline_measurement_id != measurement_id or state.baseline_reference is None:
                raise CampaignInvariantViolated(
                    "the round has no matching complete baseline reference"
                )
            return dict(state.baseline_reference)
        return self.measurements[measurement_id].prefix_reference().to_payload()

    # -- measurements -----------------------------------------------------

    def derive(
        self,
        measurement_id: str,
        candidate_digest: str,
        side: str,
        *,
        case_subset: Sequence[int] | None = None,
        case_repeats: Sequence[Sequence[int]] | None = None,
        fresh_final: bool = False,
    ) -> Measurement:
        assert self.identity is not None
        return self.derive_with(
            self.identity,
            measurement_id,
            candidate_digest,
            side,
            case_subset=case_subset,
            case_repeats=case_repeats,
            fresh_final=fresh_final,
        )

    def derive_with(
        self,
        identity: CampaignIdentityV1,
        measurement_id: str,
        candidate_digest: str,
        side: str,
        *,
        case_subset: Sequence[int] | None = None,
        case_repeats: Sequence[Sequence[int]] | None = None,
        fresh_final: bool = False,
    ) -> Measurement:
        """`derive` over an identity the session has not adopted yet.

        Resume proves every sibling's derived identity *before* it writes
        anything (§5), which is before `self.identity` may be moved by a rebase,
        so the rebuilt identity is passed in rather than read off the session.
        `case_subset` is a family-12 measurement's selector (G2 spec §3.1);
        `None` derives today's full-corpus identity. `case_repeats` is a
        family-13 fresh Parent's (design §5); `None` adds no key.
        """

        derived = measurement_identity(
            identity,
            self.corpus,
            candidate_digest,
            side,  # type: ignore[arg-type]
            case_subset=case_subset,
            case_repeats=case_repeats,
        )
        if fresh_final:
            assert self.family_version == 15
            from evolution.tree.campaign_identity import fresh_final_identity

            derived = fresh_final_identity(derived, measurement_id, self.policy.repeats)
        return Measurement.declare(
            run_root=self.run_root,
            campaign_run_id=self.run_id,
            measurement_id=measurement_id,
            identity=derived,
            corpus=self.corpus,
            secrets=self.secrets,
            primary_range=self.range_of(measurement_id),
        )

    def range_of(self, measurement_id: str) -> PrimaryRange:
        """#97 spec §4.1, ADR-0019 (amended 2026-10-03): `measurement_id`'s range rule.

        Its live rows under the producing version in force, its carried rows
        also under the version their carry was admitted under
        (`import_versions`). A measurement not bound yet carries nothing
        recorded, so its carried rows follow the version in force; the declared
        baseline import's birth re-reads the rule once bound (`bind_baseline`).
        """

        if self.log is None:
            return primary_range_for(self.family_version, resolve_package_version(), None)
        snapshot = self.snapshot()
        return primary_range_for(
            self.family_version,
            snapshot.producing_version,
            snapshot.import_versions.get(measurement_id),
        )

    def derive_bound(self, snapshot: CampaignSnapshot, measurement_id: str) -> Measurement:
        """A bound measurement, derived from the identity of the epoch it was bound in.

        ADR-0020 / I2 spec §5.1: not from the latest campaign identity. For
        every run with a single epoch -- every run today -- the two agree,
        since only comparable fields enter `derived_identity` and only a
        comparable change opens an epoch.
        """

        entry = snapshot.measurements[measurement_id]
        # G2b3a: a family-12 entry keeps its role and selector, so a subset
        # measurement and a fresh Parent re-derive exactly as they were bound;
        # a family-13 Parent keeps its `case_repeats` too (design §5).
        return self.derive_with(
            epoch_identity(snapshot, measurement_id),
            measurement_id,
            str(entry["candidate_digest"]),
            side_of_entry(entry),
            case_subset=entry.get("case_subset"),
            case_repeats=entry.get("case_repeats"),
            fresh_final=entry.get("role") in {"family15_final", "family15_champion"},
        )

    def plans_for(self, measurement_id: str) -> Any:
        snapshot = self.snapshot()
        side = measurement_side(snapshot, measurement_id)
        # G2 spec §3.1: a measurement plans over the rows its identity
        # schedules. A full-corpus measurement's are the corpus x `1..R`, so
        # families 5-11 plan exactly as before; a `case_repeats` Parent's extra
        # rows enter its last plan (family-13 design §5).
        measurement = self.measurements.get(measurement_id)
        entry = snapshot.measurements.get(measurement_id)
        if (
            self.family_version == 15
            and isinstance(self.policy, CampaignPolicyV12)
            and self.policy.focus_first
            and entry is not None
            and entry.get("role") in {"candidate", "parent"}
        ):
            attempt = self.attempt_state(snapshot, entry["attempt_id"])
            assert attempt is not None
            layers = self.attempt_layers(snapshot, attempt)
            owned = (
                measurement
                if measurement is not None
                else self.derive_bound(snapshot, measurement_id)
            )
            return _partition_by_scope(
                self.policy.repeats,
                owned.identity.to_payload()["schedule"],
                frozenset(layers.layer_one),
                frozenset(layers.layer_two),
            )
        if measurement is None:
            plans = stage_plans(
                self.policy,
                self.corpus,
                self.seed_mode,
                side,  # type: ignore[arg-type]
            )
        else:
            plans = _partition(self.policy.looks, measurement.identity.to_payload()["schedule"])
        entry = snapshot.measurements.get(measurement_id)
        after = None if entry is None else entry.get("rebound_after_look")
        if after is None:
            return plans
        # I2 spec §5.2: an open Attempt re-bound after it evaluated looks goes
        # on from the next look. The new measurement's first stage is that
        # look over every repeat up to it -- the earlier repeats are carried
        # where they were scored and measured again where they were not -- and
        # its later stages are the policy's own.
        rest = [plan for plan in plans if plan.look > after]
        first = StagePlan(
            1,
            rest[0].look,
            tuple(row for plan in plans if plan.look <= rest[0].look for row in plan.rows),
        )
        return (
            first,
            *(
                StagePlan(index, plan.look, plan.rows)
                for index, plan in enumerate(rest[1:], start=2)
            ),
        )

    def abandon(self, attempt_id: str) -> None:
        """Release an attempt's workspace through S2.

        `workspace_not_abandonable` is an answer, not a fault — an attempt that
        never materialized one has nothing to release. A stopped store or a
        capacity failure is a fault and is classified like every other boundary.
        """

        if self.store is None:
            return
        with _boundary("artifact_store"):
            try:
                self.store.abandon(attempt_id)
            except ArtifactStoreStopped:
                raise
            except ArtifactAdmissionRefused:
                return

    async def release(self, measurement_id: str) -> None:
        """Release a measurement's instances; a journal fault here is classified.

        `ValueError` is the measurement layer's "this journal is not open", which
        is not a fault — a released or never-created measurement has nothing to
        give back.
        """

        measurement = self.measurements.get(measurement_id)
        if measurement is None:
            return
        with _boundary("measurement_journal", measurement_id):
            try:
                await measurement.release_all(self.adapter)
            except ValueError:
                return

    # -- accounting -------------------------------------------------------

    def attested_ids(self) -> set[str]:
        return {
            str(entry["instance_id"])
            for measurement in self.snapshot().measurements.values()
            for entry in measurement["instances"]
        }

    def record_attestations(self, measurement_id: str, outcome: StageOutcome) -> None:
        known = self.attested_ids()
        prefix = self.prefix(measurement_id)
        payloads: list[dict[str, Any]] = []
        for instance_id, identity in outcome.attested.items():
            if instance_id in known:
                continue
            payload = dict(
                measurement_id=measurement_id,
                instance_id=instance_id,
                loaded_candidate_digest=identity.instance.loaded_candidate_digest,
                dependency_digest=identity.semantic.dependency_digest,
                boot_id=identity.instance.boot_id,
                identity=identity.to_payload(),
                evidence=[prefix],
            )
            # PDR-0014: what the backend described about this instance, written
            # once per instance because the record is one per instance -- a
            # sample's link to the description is the link it already has to its
            # instance. The key is absent, not null, when the backend described
            # nothing, so a journal written before `describe()` existed reads
            # exactly as one written by a target that declines it.
            description = outcome.attested_description.get(instance_id)
            if description is not None:
                if self.family_version == 15:
                    description = self.journal_value(description, self.secrets)
                    try:
                        validate_instance_description(description)
                    except ValueError as exc:
                        raise _Fault(
                            "persistence_failed",
                            "campaign_journal",
                            "record_matches_secret",
                            measurement_id,
                            "the redacted instance description does not fit the wire grammar",
                        ) from exc
                payload["description"] = dict(description)
            # G2 spec §4.2: the attestation points at the measurement's own
            # `components_declared`; the campaign journal does not repeat the map.
            digest = outcome.attested_components_digest.get(instance_id)
            if digest is not None:
                payload["components_digest"] = digest
            payloads.append(payload)
        # #287 design §4.2: every new instance's description is checked before
        # the first append, so a refusal appends nothing.
        self.refuse_description_secret(
            measurement_id,
            {
                instance_id: outcome.attested_description.get(instance_id)
                for instance_id in outcome.attested
            },
        )
        for payload in payloads:
            if self.checks.mode("connection_description_secret") == "shadow":
                description = outcome.attested_description.get(str(payload["instance_id"]))
                if (
                    description is not None
                    and description_match(
                        description, self.secrets, self.secret_paths, self.secret_names
                    )
                    is not None
                ):
                    hits = HitSink()
                    hits.hit("connection_description_secret", "connection_description_secret")
                    self.append(
                        "check_shadowed",
                        {
                            **hits.entries()[0],
                            "round": self.snapshot().measurements[measurement_id].get("round"),
                            "operation_id": measurement_id,
                        },
                    )
            self.append("instance_attested", payload)

    def refuse_description_secret(
        self, measurement_id: str, descriptions: Mapping[str, Mapping[str, Any] | None]
    ) -> None:
        """A new instance description holding a run secret ends this invocation (#287).

        The connection probe sees only its own instance's description; a
        Candidate's can differ. The journal would refuse the `instance_attested`
        that carries it with `CampaignInvariantViolated`, so the same predicate
        (`secret_match`, through `description_match`, which places the key path
        of the secret it names) is asked first, over every instance not yet
        attested, and a match is the existing resumable `persistence_failed` with the
        journal's own code: no record, no schema change, the run unfinished.
        `description` is not part of the attested identity, so the same command
        finishes once `describe()` is repaired. The detail names the
        description key path (`<path>` in the `secret_location` grammar) and
        the manifest path of the matched name, never the value. When either
        would itself match, the manifest path falls back as `journalable_path`
        does and the key path (like the opaque `record-key`) to `description`.
        """

        if self.checks.mode("connection_description_secret") != "enforce":
            return
        known = self.attested_ids()
        for instance_id, description in descriptions.items():
            if instance_id in known or description is None:
                continue
            found = description_match(
                description, self.secrets, self.secret_paths, self.secret_names
            )
            if found is not None:
                break
        else:
            return
        # A key path spells Target-provided key names, so one that matches
        # falls back whole: a role-prefix fallback (`journalable_path`) could
        # keep a matching key of the Target's. It is also cut to the trace's
        # bound and made UTF-8 safe (`utf8_bounded`), so a lone surrogate or a
        # very long key cannot break or flood the printed detail; the uncut
        # path is tested too, because a cut can leave a secret's prefix.
        location = utf8_bounded(found.location)
        if location == "record-key" or any(
            secret_match({"payload": {"path": path}}, self.secrets) is not None
            for path in (found.location, location)
        ):
            location = "description"
        manifest_path = journalable_path(secret_path(found.index, self.secret_paths), self.secrets)
        raise _Fault(
            "persistence_failed",
            "campaign_journal",
            "record_matches_secret",
            measurement_id,
            f"instance_attested.{location} holds the value of {manifest_path}; "
            "the measurement journal is intact; remove the value from the "
            "target's describe() and run the same command again",
        )

    def account(self, measurement_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Write one `indeterminate_observed` per not-yet-accounted sample (§7).

        Family 6 (continuable-terminals spec §3.5) accounts every indeterminate
        **try**: the key carries the try's `retry_ordinal`, and so does the
        record, so a sample that failed and was retried counts once per try.

        The record also says whether a further try *follows* this one, which is
        the one denominator slot it reserves. A try the journal already carries
        a successor for always reserves it; otherwise a retry follows only when
        the policy still allows one and the caller has not cancelled, because a
        cancelled run starts no further try (ADR-0008 as amended, ADR-0016,
        codex branch review round 1). So the denominator counts the terminals
        the run really produced, and replays from the records alone.
        """

        retries = self.policy.sample_retries
        cancelled = self.cancelled()
        measurement = self.measurements[measurement_id]
        for row in rows:
            snapshot = self.snapshot()
            case_id, sample_id = str(row["case_id"]), str(row["sample_id"])
            key: tuple[Any, ...] = (measurement_id, case_id, sample_id)
            key = (*key, int(row["retry_ordinal"]))
            if key in snapshot.accounted:
                continue
            extra: dict[str, Any] = {}
            ordinal = int(row["retry_ordinal"])
            assert retries is not None and measurement is not None
            # I1 spec §5.4: only a repair follows a repair, and a repair
            # try that ended indeterminate exhausts its sample -- it
            # reserves no retry slot for a try that will never run.
            # I2 spec §5.2 (Task 9): nor does a try of a frozen
            # measurement, which is never appended to again. The retry
            # its pair still owes runs as the re-bound measurement's own
            # dispatched sample, whose stage already counts it, so the
            # pair's next try is counted once.
            frozen = bool(snapshot.measurements[measurement_id].get("frozen", False))
            follows = ordinal + 1 < measurement.try_count(case_id, sample_id) or (
                ordinal < retries
                and not cancelled
                and not frozen
                and not measurement.repair_count(case_id, sample_id)
            )
            extra = {"retry_ordinal": ordinal, "retry_follows": follows}
            owner = str(row["owner"])
            self.append(
                "indeterminate_observed",
                dict(
                    owner=owner,
                    reason=row["reason"],
                    measurement_id=measurement_id,
                    case_id=row["case_id"],
                    sample_id=row["sample_id"],
                    count=snapshot.counts[owner] + 1,
                    evidence=[row["reference"].to_payload()],
                    **extra,
                ),
            )

    def blocked_rows(self, measurement_id: str) -> tuple[dict[str, Any], ...]:
        """Every sample that keeps a measurement from completing, in schedule order.

        Two kinds travel together because spec §6 step 1 names "the first such
        sample's owner (in schedule order)": an R2a `indeterminate` terminal, and
        (ruling 7) an `unscored` row — a durable `rollout_finished` whose scoring
        never reached a terminal — which resume classifies as
        `indeterminate(rollout_interrupted)`. Merging them in one pass is what
        keeps the owner arbitration on the real first sample.
        """

        measurement = self.measurements[measurement_id]
        return measurement.indeterminate_rows()

    def barrier_evidence(self, round_number: int, measurement_id: str | None) -> list[Any]:
        """The completed stage(s) that made this §7 barrier (amendment 21).

        A **baseline-stage** barrier is one stage's completion, so it cites that
        measurement's prefix. A **round** barrier follows every attempt of the
        round, so it cites the round's own measurements — the baseline and every
        attempt that opened one, which are exactly the measurements whose
        indeterminates the barrier is counting.
        """

        if measurement_id is not None:
            return [self.prefix(measurement_id)]
        state = self.snapshot().rounds[round_number]
        evidence = [self.prefix(str(state.baseline_measurement_id))]
        for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
            if attempt.measurement_id in self.measurements:
                evidence.append(self.prefix(str(attempt.measurement_id)))
        return evidence

    def thresholds(
        self, round_number: int, barrier: str, measurement_id: str | None = None
    ) -> bool:
        crossed = thresholds_reached(self.snapshot(), self.policy)
        if not crossed:
            return False
        if self.pending is not None:
            # §7's first decision stands, with no exception: a terminal decided
            # earlier in this very barrier -- the attestation refusal of
            # amendment 10 is the only path that can -- is the campaign's
            # answer, so the crossing is *not* recorded behind it. The
            # indeterminates that would have crossed are accounted like any
            # others and stay visible in the status projection's counters; what
            # does not happen is a `threshold_reached` the campaign will not
            # honour, which §5 would then require as the terminal.
            return True
        owner = arbitrate_owner([str(entry["owner"]) for entry in crossed])
        entry = next(item for item in crossed if item["owner"] == owner)
        outcome, reason = THRESHOLD_TERMINAL[owner]
        self.stop(
            _Pending(
                outcome,
                reason,
                "none",
                crossing={
                    name: entry[name]
                    for name in (
                        "owner",
                        "kind",
                        "count",
                        "denominator",
                        "max_count",
                        "max_rate",
                    )
                },
            )
        )
        return True

    # -- stages -----------------------------------------------------------

    def _empty_outcome(self, measurement: Measurement, plan: StagePlan) -> StageOutcome:
        """A stage that dispatched nothing of its own, over what the measurement holds."""

        return StageOutcome(
            stage=plan.stage,
            look=plan.look,
            horizon=plan.horizon,
            terminals=0,
            indeterminate=(),
            faults=(),
            critical_failed=False,
            leaked=measurement.leaked,
            attested=measurement.attested,
            attested_description=measurement.attested_description,
            attested_components_digest=measurement.attested_components_digest,
        )

    def refuse_attestation(self, measurement_id: str, error: AttestationError) -> None:
        """The `attestation_mismatch` terminal an `AttestationError` decides (§7, §8).

        A target that attests another Candidate is a campaign-level fault of the
        attestation kind: not a persistence fault (§7 ruling 9) and not an
        executor bug (§5 ruling 10). R1 refused the instance at materialization
        and released it. The campaign stops at the terminal barrier and finishes
        `infrastructure_failed(attestation_mismatch, none)` with the
        measurement's prefix as evidence -- the same admissible E branch
        promotion admission uses. A `ComponentsChanged` (family 12, G2b3b) is
        refused the same way and suspends `target_components_changed` instead;
        an already-open stage it refuses is left open
        (`leave_drifted_stage_open`). Its evidence also carries the prefix
        `Measurement._materialize` recorded right after it journaled the refused
        instance's release, so the audit decides the drift over the journal as
        it stood at the refusal (ADR-0019 as amended 2026-10-02). From
        `0.3.0a27` a `target_changed` cites the same recorded prefix (a27 spec
        §4.1, #94): on a resumed open stage `refuse_rows` has appended after
        that release, so a prefix taken here would end elsewhere. Below family
        7 the terminal's evidence is the prefix as it stands, as before.
        """

        recorded = self.measurements[measurement_id].refusal_prefix
        if recorded is None:
            raise CampaignInvariantViolated("an attestation refusal records its prefix")
        prefix = recorded
        drift = isinstance(error, ComponentsChanged)
        cites = drift or refusal_prefix_required(self.snapshot().producing_version)
        self.stop(
            _Pending(
                "infrastructure_failed",
                "attestation_mismatch",
                "none",
                evidence=(prefix,),
                suspend_as="target_components_changed" if drift else "target_changed",
                record=self.target_record(measurement_id),
                prefix=str(prefix["locator"]) if cites else None,
            )
        )

    def arm_components_check(self, measurement_id: str, measurement: Measurement) -> None:
        """G2 spec §4.6: the map `measurement`'s next instance must render.

        Set right before each materialization, with the instance-description
        check (#287). Only a family-12 Attempt's own
        and fresh-Parent measurements are checked, against the side's
        `drift_reference`; inside its reference epoch a different declared
        map suspends, after it only when the measurement already rendered the
        reference. An instance that declares no map is not compared.

        Family 13 (F7 plan choice 10, design §9) checks the final cohort's
        members the same way, `@e<k>` re-binds included: a finalist against
        its Attempt's Candidate reference (none when the Attempt was not
        checked), the Champion against the round's `champion_reference`.
        """

        measurement.components_check = None
        # #287: every materialization -- a stage's first open and a resumed
        # open stage's re-materialization inside `Measurement.run_stage` --
        # refuses a description holding a run secret before any row runs on
        # the instance, so no row is served by an instance that can never be
        # attested (which would fail promotion admission on every resume).
        measurement.description_check = lambda instance_id, description: (
            self.refuse_description_secret(measurement_id, {instance_id: description})
        )
        snapshot = self.snapshot()
        entry = snapshot.measurements[measurement_id]
        role = entry.get("role")
        member_role = campaign_vocabulary_for(14 if self.family_version == 14 else 13).registry_role
        if role == "family15_champion" or (
            role == member_role and entry["cohort_role"] == "champion"
        ):
            reference = self.champion_drift_reference(int(entry["round"]))
        elif role in ("candidate", "parent", member_role, "family15_final", "family15_completion"):
            attempt = self.attempt_state(snapshot, str(entry["attempt_id"]))
            assert attempt is not None
            # A finalist member renders its Attempt's Candidate.
            reference = self.drift_reference(
                attempt,
                "candidate"
                if role in (member_role, "family15_final", "family15_completion")
                else str(role),
            )
        else:
            return
        if reference is not None:
            measurement.components_check = ComponentsCheck(
                reference.digest, reference.epoch == int(entry["epoch"])
            )

    def target_record(
        self, measurement_id: str, instance_id: str | None = None
    ) -> tuple[int, str | None] | None:
        """Family 7: the campaign record an attestation refusal's suspension cites.

        The family-6 `attestation_mismatch` terminal cites the refused
        measurement's journal; the campaign record that stands for it is the
        latest one binding it (`baseline_bound`/`measurement_bound`), or, when
        the refused instance is named, its `instance_attested`. `None` below
        family 7.

        F7 plan choice 11: in family 13 the record is looked up over the
        measurement's whole re-bind chain (`_rebind_chain`), the chain
        promotion admission reads (`attested_chain`), so an instance of a
        frozen predecessor that admission refuses is found under its own
        `instance_attested`. Families 7 to 12 keep the exact match.
        """

        snapshot = self.snapshot()
        chain = frozenset(_rebind_chain(snapshot.measurements, measurement_id))
        found: tuple[int, str | None] | None = None
        for seq, (measurement, instance, operation) in sorted(snapshot.target_records.items()):
            if measurement in chain and instance == instance_id:
                found = (seq, operation)
        return found

    async def run_stage(self, measurement_id: str, stage: int) -> StageOutcome:
        """Open a stage if it is not open yet, then run every one of its rows.

        ADR-0008 requires every opened stage to run to completion, so on a
        **first** open the target is materialized and attested before
        `stage_opened` is appended: an `AttestationError` there refuses the
        target while no stage is open, and the campaign's journal shows no
        opened stage without a completion. On a **resumed**, already-open stage
        the `stage_opened` is durable from the earlier session and
        `Measurement.resume` has cleared `_instances`, so re-materialization
        happens inside `Measurement.run_stage` as before. A target that starts
        lying between the two sessions cannot dispatch that stage's remaining
        rows, so they are closed with S1's own target-owned indeterminate
        terminal and the stage still runs to completion (spec §11, ADR-0008).
        A component-drift refusal there leaves the stage open instead
        (`leave_drifted_stage_open`).

        A crash between `stage_opened` and the last of its samples leaves an
        opened stage only partly dispatched. `stage_opened` is written once —
        the lifecycle refuses a second one — and the rows S1 already carries a
        terminal for are skipped, so resuming the stage finishes it (spec §11).
        """

        measurement = self.measurements[measurement_id]
        plan = self.plans_for(measurement_id)[stage - 1]
        # Family 14 (R57): the connection gate comes before the stage opens, so
        # a failed probe opens no stage and dispatches nothing.
        connection = await self.ensure_connection(
            lambda: self._read_will_dispatch(measurement_id, plan)
        )
        if isinstance(connection, _Refused):
            return self._empty_outcome(measurement, plan)
        opened = len(self.snapshot().measurements[measurement_id]["stages"])
        assert self.store is not None
        # Read once, and only on a path that needs it: on the champion side
        # `candidate_ref` reads the store's committed champion, and a stage that
        # neither opens nor dispatches -- a resumed stage every row of which is
        # already terminal -- has no use for the reference.
        candidate: ArtifactRef | None = None
        if stage > opened:
            opening = await self._open_stage(measurement_id, plan)
            if isinstance(opening, _Refused):
                return self.finish_stage(
                    measurement_id, plan, self._empty_outcome(measurement, plan)
                )
            candidate = opening
            self._append_stage_opened(measurement_id, plan)
        remaining, repairs, restored = self._stage_dispatch_rows(measurement_id, plan)
        if remaining.rows:
            self.carry_stage_slots(measurement_id)
            self.reserve_threshold_repairs(measurement_id)
            if candidate is None:
                candidate = self.candidate_ref(measurement_id)
            self.arm_components_check(measurement_id, measurement)
            try:
                with _boundary("measurement_journal", measurement_id):
                    outcome = await measurement.run_stage(
                        remaining,
                        adapter=self.adapter,
                        candidate=candidate,
                        strategy=self.strategy,
                        environment=self.environment,
                        scorer=self.scorer,
                        policy=self.policy,
                        expected=self.expected_semantic,
                        cancelled=self.cancelled,
                        # A restored row takes the D1 path (spec §2.4); every
                        # other read below keeps it among the repairs.
                        repair_rows=repairs - restored,
                        restored_rows=restored,
                        connection=connection,
                    )
            except AttestationError as error:
                if isinstance(error, ComponentsChanged):
                    return self.leave_drifted_stage_open(measurement_id, plan, error)
                # A resumed stage re-materializes over samples an earlier session
                # made durable, and this refusal means its remaining rows can
                # never be dispatched. ADR-0008 requires an opened stage to run
                # to completion, so each of them is given S1's own target-owned
                # indeterminate terminal (`refuse_rows`) before the campaign
                # stops: the stage ends with as many terminals as it scheduled.
                # The branch then falls through to the same tail every other
                # stage takes, so those rows and the attestations are recorded
                # exactly once. A threshold those rows would cross is *not*
                # recorded behind this refusal: §7's first decision stands, and
                # §5 would otherwise require the crossing's own terminal.
                with _boundary("measurement_journal", measurement_id):
                    measurement.refuse_rows(remaining, connection=connection)
                self.refuse_attestation(measurement_id, error)
                outcome = self._empty_outcome(measurement, plan)
        else:
            outcome = self._empty_outcome(measurement, plan)
        return self.finish_stage(measurement_id, plan, outcome, repairs)

    def leave_drifted_stage_open(
        self, measurement_id: str, plan: StagePlan, error: ComponentsChanged
    ) -> StageOutcome:
        """A component-drift refusal of an already-open stage (family 12, G2b3b).

        PDR-0020: drift suspends and closes nothing. Target terminals for the
        rows the refused instance cannot dispatch would exhaust them and close
        the Attempt while the suspension is pending, so they get none and the
        stage is not finished: it stays open, and the planner returns to it on
        the resume after repair, where it completes (ADR-0008, amended
        2026-10-02). The instances attested before the refused one are recorded
        exactly once, as `finish_stage` would; `attempt_stage` reads no
        consequence of a stage left open.
        """

        outcome = self._empty_outcome(self.measurements[measurement_id], plan)
        self.refuse_attestation(measurement_id, error)
        self.record_attestations(measurement_id, outcome)
        self.drifted_open.add(measurement_id)
        return outcome

    async def _open_stage(
        self, measurement_id: str, plan: StagePlan
    ) -> ArtifactRef | None | _Refused:
        """Materialize a stage about to open; `stage_opened` is the caller's to append.

        Returns the reference it read, `None` when the stage dispatches
        nothing, or `_REFUSED` once an `AttestationError` refused the target
        (`refuse_attestation` has decided the terminal by then).
        """

        measurement = self.measurements[measurement_id]
        # Baseline inheritance §3.3: a carried row is neither a try nor in
        # any denominator, so the stage declares only the rows it
        # dispatches, and a stage that dispatches nothing -- every row
        # carried -- has no target to materialize.
        dispatched = len(plan.rows) - len(_carried(measurement, plan))
        candidate: ArtifactRef | None = None
        try:
            if dispatched:
                candidate = self.candidate_ref(measurement_id)
                self.arm_components_check(measurement_id, measurement)
                with _boundary("measurement_journal", measurement_id):
                    await measurement.materialize(
                        self.adapter, candidate, self.strategy, self.policy
                    )
                # #287: a backstop. `Measurement.description_check`, armed by
                # `arm_components_check` above, already refused a description
                # holding a run secret inside `materialize`, per instance; this
                # re-check over every new instance stays before `stage_opened`,
                # so a refused description leaves no row served by an instance
                # that can never be attested. A resume opens the stage on the
                # repaired target; refused after its rows
                # (`record_attestations`), a measurement with no other
                # attestation would fail promotion admission forever.
                self.refuse_description_secret(measurement_id, measurement.attested_description)
        except AttestationError as error:
            # No `stage_opened` is written. The stage is not therefore
            # empty: a measurement materializes several instances, so an
            # earlier one of this very call may already be attested, and the
            # caller's tail records it exactly once.
            self.refuse_attestation(measurement_id, error)
            return _REFUSED
        return candidate

    def _append_stage_opened(self, measurement_id: str, plan: StagePlan) -> None:
        """`stage_opened`, declaring only the rows the stage dispatches (§3.3)."""

        measurement = self.measurements[measurement_id]
        self.append(
            "stage_opened",
            dict(
                measurement_id=measurement_id,
                stage=plan.stage,
                look=plan.look,
                sample_count=len(plan.rows) - len(_carried(measurement, plan)),
                **({"scope": plan.scope} if plan.scope is not None else {}),
            ),
        )

    def _stage_dispatch_rows(
        self, measurement_id: str, plan: StagePlan
    ) -> tuple[StagePlan, frozenset[tuple[str, str]], frozenset[tuple[str, str]]]:
        """The rows an opened stage still dispatches, exam first, its owed repairs
        and, among those, the granted rows that restore a declined D1 retry.

        Family 13 (pre-F8 spec §2.4, §2.8): a row whose latest try the campaign
        declined to retry is dispatched only when a grant names it, and a
        granted row whose latest try S1 still admits a D1 retry after (an
        indeterminate or unscored try below its limit, never repaired) is
        `restored`. The caller dispatches it outside `repair_rows`, so it takes
        the D1 path and keeps the rest of its retry budget; it stays in
        `repairs` for every other read. Both sets are empty below family 13.

        #194 (G3): a granted row whose latest try is a `cancelled` one a
        `champion_rows` listing restored (`listing_restored`) is `restored`
        too; it is passed as `restored_rows`, which lets a D1 retry follow a
        closed `cancelled` try.
        """

        measurement = self.measurements[measurement_id]
        repairs = self.repair_rows(measurement_id)
        # A row owed a repair may sit in an earlier stage than the one being
        # resumed (a crash after a truncated suspension's listed repairs): it is
        # dispatched from the measurement's whole schedule, never only from
        # this stage's plan, or the planner would return here forever.
        planned = {(row["case_id"], row["sample_id"]) for row in plan.rows}
        owed_elsewhere = tuple(
            row
            for other in self.plans_for(measurement_id)
            for row in other.rows
            if (row["case_id"], row["sample_id"]) in repairs - planned
        )
        dispatch = replace(plan, rows=(*plan.rows, *owed_elsewhere))
        withheld: frozenset[tuple[str, str]] = frozenset()
        restored: frozenset[tuple[str, str]] = frozenset()
        rows = {
            (row.case_id, row.sample_id): row for row in measurement.sample_rows(dispatch.horizon)
        }
        snapshot = self.snapshot()
        declined = declined_tries(snapshot.retries_declined, measurement_id)
        # #194 (G3): the cancelled tries a Champion listing restored. Only a
        # `champion_rows` listing restores a cancelled try, so this set is
        # empty for every other measurement and for a run that never lists.
        listed = restored_tries(snapshot.listing_restored, measurement_id)
        # Pre-F8 spec §2.8: a declined row is dispatched only under a grant.
        withheld = frozenset(
            key
            for key, row in rows.items()
            if (row.case_id, row.sample_id, row.retry_ordinal) in declined and key not in repairs
        )
        # Spec §2.4: a granted declined try resumes its D1 budget (`repair: false`),
        # and so (#194, G3) does a granted cancelled try the listing restored.
        restored = frozenset(
            key
            for key in repairs
            if (row := rows.get(key)) is not None
            and (
                (row.disposition in ("indeterminate", "unscored") and not row.exhausted)
                or (
                    row.outcome == "cancelled"
                    and (row.case_id, row.sample_id, row.retry_ordinal) in listed
                )
            )
        )
        remaining = self.exam_first(
            measurement_id, _remaining(measurement, dispatch, repairs, withheld)
        )
        return remaining, repairs, restored

    async def run_joint_stage(
        self,
        measurement_ids: tuple[str, ...],
        stage: int,
        *,
        exam_first: bool = True,
        independent_order: bool = False,
    ) -> tuple[StageOutcome, ...]:
        """One joint stage over several measurements (G2 spec §4.6; family-13 design §7).

        A family-12 Attempt passes its Candidate and fresh Parent; a family-13
        cohort passes every member in `members` order with `exam_first=False`.
        Every measurement this stage opens is materialized first, then each
        `stage_opened` is appended in `measurement_ids` order, so no stage is
        open while an earlier side's target is refused. The remaining rows of
        all sides are then started in one merged order -- exam cases first (as
        `exam_first` orders one measurement; skipped with `exam_first=False`,
        which gives the cohort's `case_repeat_member` order), then corpus
        order, repeat, then the side's position in `measurement_ids` -- under
        one `max_concurrency` and one scorer semaphore, so the sides are
        measured at the same time and never exceed the concurrency the policy
        grants one measurement.

        A refusal while opening appends no `stage_opened` at all, but ADR-0008
        still holds for a side whose stage is already open -- the Candidate's,
        when a crash or a fault fell between the two `stage_opened` records:
        its rows are dispatched alone and settle, so its opened stage
        completes. The refused side stays unopened and the campaign stops on
        the refusal -- in family 12 a `target_changed` suspension, after which
        the planner returns to the stage (`attempt_stage_progress`) and opens
        it. A side whose stage is already open and whose re-materialization is
        refused for component drift (G2b3b) is left open and undispatched, not
        finished; the other open side still runs and finishes.

        Any other failure of one side before the dispatch is held the same way
        (ADR-0008, codex rounds 1 and 2): its plan or its materialization here,
        and its whole preparation in `_prepare_side`. The failing side
        dispatches nothing and is not finished; every side whose stage is open
        -- opened before this call, or by it -- still runs and finishes, and
        the first failure is then raised, to end the campaign as `run_stage`'s
        would. A side opens its stage only while every side this call opens
        before it has opened, so the Candidate's always opens first: when the
        Parent's `stage_opened` fails after the Candidate's was written, the
        Candidate's open stage runs to completion (on resume, for a
        campaign-journal fault; see below) and the Parent's stays unopened for
        the planner to open again. Only an `Exception` is held: a
        cancellation or an interpreter exit propagates at once.

        A campaign-journal fault is the exception (maintainer decision on codex
        round 2): once the campaign log is unhealthy, nothing is dispatched or
        finished, and the open stage completes on resume, as after a crash. An
        artifact-store, target or measurement-journal fault does not: it drains
        the already-open side. Only the campaign log's poisoned writer stops new
        dispatch, because an open side run now could not write its
        `instance_attested`, which no resume re-proves.

        Family 14's connection gate (R57) runs first, once for the whole
        stage, and reads each side's plan to decide whether anything
        dispatches. A measurement-journal fault in that read is not held as
        one side's failure: it raises from the gate before any side is
        prepared, so a later side whose stage is already open does not drain
        in this invocation; it completes on resume, as after a crash.
        """

        assert self.store is not None
        # Family 14 (R57): one gate for the joint stage, if any side dispatches.
        connection = await self.ensure_connection(
            lambda: self._joint_will_dispatch(measurement_ids, stage)
        )
        if isinstance(connection, _Refused):
            return tuple(
                self._empty_outcome(self.measurements[mid], self.plans_for(mid)[stage - 1])
                for mid in measurement_ids
            )
        snapshot = self.snapshot()
        opening = [
            mid for mid in measurement_ids if stage > len(snapshot.measurements[mid]["stages"])
        ]
        # Shared by both sides and read before either opens: a failure here is
        # no one side's, and it leaves no stage this call opened.
        exam = (self._exam_of(measurement_ids[0]) or frozenset()) if exam_first else frozenset()
        position = {case.case_id: index for index, case in enumerate(self.corpus)}
        permits = asyncio.Semaphore(self.policy.max_concurrency)
        scoring = asyncio.Semaphore(self.policy.max_scorer_concurrency)
        # The first failure of any side, raised once every open side finished:
        # an opening's or a preparation's comes before a settle's, and a
        # settle's before a finish's.
        failure: Exception | None = None
        # Whether a side this call opens may still open: not once a side this
        # call opens before it failed to open or was refused.
        may_open = True
        refused = False
        plans: dict[str, StagePlan] = {}
        refs: dict[str, ArtifactRef | None] = {}
        for mid in measurement_ids:
            try:
                plans[mid] = self.plans_for(mid)[stage - 1]
                if mid in opening and may_open:
                    ref = await self._open_stage(mid, plans[mid])
                    if isinstance(ref, _Refused):
                        refused = True
                        may_open = False
                    else:
                        refs[mid] = ref
            except Exception as exc:
                failure = failure or exc
                if mid in opening:
                    may_open = False
        outcomes: dict[str, StageOutcome] = {}
        dispatches: dict[str, StageDispatch] = {}
        repairs: dict[str, frozenset[tuple[str, str]]] = {}
        for mid in measurement_ids:
            if mid not in plans:
                continue
            opens = mid in opening
            if opens and not may_open and not refused:
                # Kept from opening by its own failure or an earlier side's: it
                # is not finished, as `run_stage` records nothing for a stage
                # whose opening raised.
                continue
            try:
                if opens and refused:
                    # No side opens: a side materialized above gets its
                    # attestations recorded by `finish_stage` below, over a
                    # stage it never opened.
                    outcomes[mid] = self._empty_outcome(self.measurements[mid], plans[mid])
                    continue
                prepared, repairs[mid] = await self._prepare_side(
                    mid,
                    plans[mid],
                    opens=opens,
                    candidate=refs.get(mid),
                    permits=permits,
                    scoring_permits=scoring,
                    connection=connection,
                )
            except Exception as exc:
                failure = failure or exc
                # Read from the journal: a side whose `stage_opened` was not
                # written keeps every later side's stage unopened too.
                if opens and stage > len(self.snapshot().measurements[mid]["stages"]):
                    may_open = False
                continue
            if isinstance(prepared, StageDispatch):
                dispatches[mid] = prepared
            else:
                outcomes[mid] = prepared
        assert self.log is not None
        if not self.log.healthy:
            # A campaign-journal fault poisoned the writer (store, target and
            # measurement-journal faults do not reach here: they drain the open
            # side). This session could not write the stage's
            # `instance_attested`, and a lost attestation is never re-proven on
            # resume, so nothing is dispatched or finished here: the open stage
            # completes on resume, as after a crash. The single-measurement
            # `run_stage` has no log-health check, so this is stricter. Without a held
            # failure (another task poisoned the log), the fault is the one any
            # append would now raise.
            if failure is None:
                with _boundary("campaign_journal"):
                    self.log.check()
            assert failure is not None
            raise failure
        order_of = list(dispatches)

        def order(index: int, row: Mapping[str, Any]) -> tuple[Any, ...]:
            case_id = str(row["case_id"])
            if independent_order:
                return (case_id, int(row["repeat"]), order_of[index])
            return (
                case_id not in exam,
                position[case_id],
                int(row["repeat"]),
                measurement_ids.index(order_of[index]),
            )

        results = await run_interleaved([dispatches[mid] for mid in order_of], order)
        # Each side settles even when the other's settle raised: an unclassified
        # failure of one never leaves the other's stage unread, and every side
        # that settled records its consequences, even when the other's
        # `finish_stage` raised. The first error -- an opening's or a
        # preparation's, then a settle's, then a finish's -- is raised once
        # that is done.
        for mid, result in zip(order_of, results, strict=True):
            try:
                with _boundary("measurement_journal", mid):
                    outcomes[mid] = await self.measurements[mid].settle_stage(
                        dispatches[mid], result, adapter=self.adapter, policy=self.policy
                    )
            except Exception as exc:
                failure = failure or exc
        finished: dict[str, StageOutcome] = {}
        for mid in measurement_ids:
            if mid not in outcomes:
                continue
            if mid in self.drifted_open:
                # Left open by its drift refusal: not finished (G2b3b).
                finished[mid] = outcomes[mid]
                continue
            try:
                finished[mid] = self.finish_stage(
                    mid, plans[mid], outcomes[mid], repairs.get(mid, frozenset())
                )
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
        return tuple(finished[mid] for mid in measurement_ids)

    async def _prepare_side(
        self,
        measurement_id: str,
        plan: StagePlan,
        *,
        opens: bool,
        candidate: ArtifactRef | None,
        permits: asyncio.Semaphore,
        scoring_permits: asyncio.Semaphore,
        connection: ConnectionAdmission | None = None,
    ) -> tuple[StageDispatch | StageOutcome, frozenset[tuple[str, str]]]:
        """One side of a joint stage, from its `stage_opened` to its prepared rows.

        The whole of one side's preparation, so `run_joint_stage` holds a
        failure of any step as that side's alone (ADR-0008, codex round 2):
        `stage_opened` when `opens` (the side was materialized, and `candidate`
        is the reference it read), the rows it still dispatches and the repairs
        it owes, a carried stage's slots, the threshold reservation, the
        reference and `prepare_stage`.
        Returns the side's dispatch -- or, for a side that dispatches nothing or
        whose resumed target was refused, its outcome -- with its repairs. A
        side refused for component drift is left open (`drifted_open`) and
        `run_joint_stage` does not finish it. `connection` is the joint stage's
        family-14 admission token (R59), shared by every side.
        """

        measurement = self.measurements[measurement_id]
        if opens:
            self._append_stage_opened(measurement_id, plan)
        remaining, repairs, restored = self._stage_dispatch_rows(measurement_id, plan)
        if not remaining.rows:
            return self._empty_outcome(measurement, plan), repairs
        self.carry_stage_slots(measurement_id)
        self.reserve_threshold_repairs(measurement_id)
        if candidate is None:
            candidate = self.candidate_ref(measurement_id)
        self.arm_components_check(measurement_id, measurement)
        try:
            with _boundary("measurement_journal", measurement_id):
                dispatch = await measurement.prepare_stage(
                    remaining,
                    adapter=self.adapter,
                    candidate=candidate,
                    strategy=self.strategy,
                    environment=self.environment,
                    scorer=self.scorer,
                    policy=self.policy,
                    expected=self.expected_semantic,
                    cancelled=self.cancelled,
                    repair_rows=repairs - restored,
                    restored_rows=restored,
                    permits=permits,
                    scoring_permits=scoring_permits,
                    connection=connection,
                )
        except AttestationError as error:
            if isinstance(error, ComponentsChanged):
                return self.leave_drifted_stage_open(measurement_id, plan, error), repairs
            # As `run_stage`'s resumed branch: the rows this side can never
            # dispatch get S1's target terminal and the campaign stops; the
            # other side's rows still run, so the opened stage completes.
            with _boundary("measurement_journal", measurement_id):
                measurement.refuse_rows(remaining, connection=connection)
            self.refuse_attestation(measurement_id, error)
            return self._empty_outcome(measurement, plan), repairs
        if (
            self.family_version == 14
            and self.snapshot().measurements[measurement_id].get("role")
            == campaign_vocabulary_for(self.family_version).registry_role
        ):
            # A final member can finish its rows before another member loses
            # power; D7 can also settle an exhausted finalist without retrying.
            # Neither rematerializes on resume. Checkpoint its real verified
            # instance before dispatch: that identity otherwise lives only in
            # memory and cannot be reconstructed from S1 after process loss.
            self.record_attestations(measurement_id, self._empty_outcome(measurement, plan))
        return dispatch, repairs

    def carry_stage_slots(self, measurement_id: str) -> None:
        """#109 (a28 spec §3.3): a carried stage's unfilled slots move into this window.

        A stage positioned before the latest suspension -- in practice one a
        drift left open (ADR-0008 as amended 2026-10-02) -- is about to be
        dispatched again. Its unfilled slots (`_unfilled_slots`, over the live
        journal) were booked in an earlier window; `stage_redispatched` moves
        them into this one, where its rows run, citing the measurement's
        journal as it stands right before the dispatch materializes. At most
        once per measurement per window: the record moves the stage's position
        past the latest suspension. A crash before the first row resumes in
        this window under the record it has.

        Written before `reserve_threshold_repairs`, so a window that holds the
        measurement's repair reservation also holds its carried stage's slots
        and an I2 release positioned by the later of the two stays exact (a28
        spec §3.6). Family 12 and later, from producing version `0.3.0a28`.
        """

        snapshot = self.snapshot()
        if not stage_redispatch_required(snapshot.producing_version):
            return
        # Carried: positioned before the latest suspension. This window holds
        # no `stage_redispatched` of it yet, or the position would follow it.
        position = snapshot.stage_positions.get(measurement_id)
        if position is None or snapshot.window_start is None or position >= snapshot.window_start:
            return
        count = _unfilled_slots(
            snapshot, measurement_id, self.measurements[measurement_id], self.policy.looks
        )
        if not count:
            return
        self.append(
            "stage_redispatched",
            dict(
                measurement_id=measurement_id,
                stage=int(snapshot.measurements[measurement_id]["stages"][-1]["stage"]),
                count=count,
                evidence=[self.prefix(measurement_id)],
            ),
        )
        self.hook("after_stage_redispatched")

    def reserve_threshold_repairs(self, measurement_id: str) -> None:
        """#78 (spec §3.5): the slots of this window's threshold repairs, once.

        Written before the dispatch, as `stage_opened` is before a stage's, and
        at most once per measurement per window: a dispatch interrupted before
        its repairs started resumes under the reservation it has; after a new
        suspension the still-unspent grants are reserved again.
        """

        snapshot = self.snapshot()
        owed = threshold_owed(snapshot, self.measurements[measurement_id])
        if not owed:
            return
        reserved = snapshot.repairs_reserved.get(measurement_id)
        # A grant needs a suspension, so `window_start` is never None
        # here and the fall-through cannot reserve the same window twice.
        if (
            reserved is not None
            and snapshot.window_start is not None
            and reserved[0] > snapshot.window_start
        ):
            return
        self.append(
            "repairs_dispatched",
            dict(
                measurement_id=measurement_id,
                count=len(owed),
                evidence=[self.prefix(measurement_id)],
            ),
        )
        self.hook("after_repairs_dispatched")

    def exam_first(self, measurement_id: str, plan: StagePlan) -> StagePlan:
        """The stage's rows with the promotion gate's own cases dispatched first.

        Ordering only: the same rows, the same count, the same terminals. An
        attempt's measurement runs under its round's `gate_scope`, so the cases
        the gate will actually read are taken while the target is healthiest
        and the budget is whole; a baseline measurement has no scope yet (the
        focus is selected from it) and is left exactly as it was.
        """

        if plan.scope is not None:
            return plan
        exam = self._exam_of(measurement_id)
        if exam is None:
            return plan
        return replace(
            plan, rows=tuple(sorted(plan.rows, key=lambda row: row["case_id"] not in exam))
        )

    def _exam_of(self, measurement_id: str) -> frozenset[str] | None:
        """The gate-scope cases of the round whose Attempt measures `measurement_id`.

        `None` for a measurement no Attempt's `measurement_id` names -- a
        baseline has no scope yet (the focus is selected from it).
        """

        snapshot = self.snapshot()
        for state in snapshot.rounds.values():
            if any(attempt.measurement_id == measurement_id for attempt in state.attempts.values()):
                if state.gate_scope is None:
                    return None
                return frozenset(state.gate_scope["cases"])
        return None

    def finish_stage(
        self,
        measurement_id: str,
        plan: StagePlan,
        outcome: StageOutcome,
        repairs: frozenset[tuple[str, str]] = frozenset(),
    ) -> StageOutcome:
        """Read a stage's consequences and record them exactly once.

        The dispatch covers only the rows S1 has no terminal for; the stage's
        *consequences* are read over every row of the full plan, so a sample
        taken before an interruption still decides §6's order. The fault rows
        also cover the `repairs` this dispatch owed (I1 spec §5.4), wherever
        their stage: a repair that faulted again suspends here.
        """

        measurement = self.measurements[measurement_id]
        outcome = _full_outcome(measurement, plan, self.policy, outcome, repairs)
        self.record_attestations(measurement_id, outcome)
        # Accounting reads every indeterminate try of the stage and of every
        # repair this dispatch carried, whatever its stage (#78, spec §3.3); the
        # outcome's own `indeterminate` is the latest-try view decisions use.
        keys = {(row["case_id"], row["sample_id"]) for row in plan.rows} | set(repairs)
        self.account(
            measurement_id,
            [
                row
                for row in measurement.indeterminate_rows()
                if (row["case_id"], row["sample_id"]) in keys
            ],
        )
        if self.family_version == 15:
            self.complete_independent_side(measurement_id)
        return outcome

    def candidate_ref(self, measurement_id: str) -> ArtifactRef:
        """The published artifact a measurement measures.

        Every digest here is durable — the campaign journal's own
        `measurement_bound` / `attempt_sealed` records — and the copy's location
        is the S2 locator grammar, so a resumed campaign rebuilds the same
        reference without holding anything from the session that sealed it.
        """

        snapshot = self.snapshot()
        entry = snapshot.measurements[measurement_id]
        digest = str(entry["candidate_digest"])
        artifact_policy = (
            self.artifact_policy
            if self.settlement_store_identity is None
            else self.settlement_store_identity.policy
        )
        if entry.get("role") == "parent":
            # G2 spec §4.6: a fresh Parent measures the Attempt's Parent tree,
            # the published champion copy its `attempt_opened` names -- as
            # `select_attempt_cases` reads it. The ledger's copy must be the
            # tree the Parent was bound to measure: a copy published under that
            # version with other bytes is the store's corruption, never a
            # reason to measure another tree under the bound identity.
            attempt = self.attempt_state(snapshot, str(entry["attempt_id"]))
            assert attempt is not None and self.store is not None
            with _boundary("artifact_store"):
                parent = self.store.published_ref("champion", attempt.parent_version)
                if parent.digest != digest:
                    raise ArtifactCorruptionError("artifact_digest_mismatch")
            return parent
        if entry["attempt_id"] == CHAMPION_SIDE:
            # Only the champion side needs the open store: the seed champion's
            # own copy is the store's, while a Candidate's reference is rebuilt
            # from the durable digests alone -- which is what lets `admitted()`
            # run over a run root nothing has opened a store on.
            assert self.store is not None and self.identity is not None
            champion = self.store.champion()
            if champion.digest == digest:
                return champion
            store = self.identity.to_payload()["artifact_store"]
            return ArtifactRef(
                "champion",
                SEED_VERSION,
                self.run_root / (CHAMPION_LOCATOR + SEED_VERSION),
                digest,
                str(store["seed_dependency_digest"]),
                artifact_policy,
            )
        attempt_id = str(entry["attempt_id"])
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.dependency_digest is not None
        return ArtifactRef(
            "candidate",
            attempt_id,
            self.run_root / (CANDIDATE_LOCATOR + attempt_id),
            digest,
            attempt.dependency_digest,
            artifact_policy,
        )

    # -- the drive loop ---------------------------------------------------

    def _owed_closure(self) -> bool:
        return self.family_version == 15 and (
            _selected_open_round(self.snapshot()) is not None
            or bool(_rejected_focus_attempts(self.snapshot()))
        )

    async def drive(self) -> None:
        while True:
            if self.error is not None or self.fault is not None or self.pending is not None:
                break
            if self.stopping:
                break
            if self.cancelled():
                if self.family_version == 15:
                    # Keep the request even if the caller's token clears while
                    # the selected round completes without any target call.
                    self.deferred_cancel = True
                else:
                    self.stop(_Pending("stopped", "cancelled", "none"))
                    break
            if not self._owed_closure():
                consequences, self.deferred_consequences = self.deferred_consequences, []
                for write in consequences:
                    write()
                if consequences:
                    # These declarations were deferred past `adopt_siblings`:
                    # pay their ledger half before any further publication.
                    if self.store_identity is None:
                        raise CampaignInvariantViolated(
                            "a deferred store rebase needs the adopted store identity"
                        )
                    self._complete_owed_store_rebases(self.store_identity)
                released_change = (
                    self.held_change is not None and _selected_open_round(self.snapshot()) is None
                )
                if released_change:
                    self._release_held_change()
                    if not self.closed and any(
                        mid not in self.measurements and not entry.get("frozen", False)
                        for mid, entry in self.snapshot().measurements.items()
                    ):
                        # Unlike a closed round, a rejected focus can leave other
                        # parallel Attempts open. Their declared carries must be
                        # born before the next action can use their new ids.
                        for measurement in self.measurements.values():
                            measurement.close()
                        self.measurements.clear()
                        assert self.store_identity is not None
                        await self.adopt_siblings(self.store_identity)
                if self.closed:
                    # A held close's terminal is final, including when the
                    # caller also requested cancellation during settlement.
                    break
                if self.deferred_cancel:
                    self.stop(_Pending("stopped", "cancelled", "none"))
                    break
                if released_change:
                    continue
            rejected = _rejected_focus_attempts(self.snapshot())
            actions = (
                tuple(_rejected_focus_action(a) for a in rejected)
                if rejected
                else next_actions(self.view(), self.policy)
            )
            if not actions:
                break
            if (
                self.held_change is not None
                and not rejected
                and not all(
                    # Family 13 (plan choice 19): `BindFinalists` is proposed
                    # exactly where `DecideRound` was, and `decide_round` performs it.
                    isinstance(action, (DecideRound, BindFinalists))
                    for action in actions
                )
            ):
                # I2 spec §3.2 row 2 (and #263 for a family-15 `close` or
                # `continue`): the target has already changed, so the span a
                # held decision waits through may only decide the round --
                # never measure, call the target, or finish the campaign and
                # silently drop the operator's decision. The message names the
                # held effect (`rebaseline`, `continue` or `close`).
                raise AssertionError(f"a held {self.held_change[5]} admits no {actions[0]!r}")
            spawned = False
            for action in actions:
                attempt_id = action_attempt_id(action)
                if attempt_id is not None and attempt_id not in self.tasks:
                    self.tasks[attempt_id] = asyncio.ensure_future(self.run_attempt(attempt_id))
                    spawned = True
            if self.tasks:
                await self.reap()
                continue
            campaign_level = [a for a in actions if action_attempt_id(a) is None]
            if not campaign_level:
                if not spawned:
                    break
                continue
            try:
                await self.perform(campaign_level[0])
            except _Fault as exc:
                self.fault = exc
                self.stopping = True
                break

    def _release_held_change(self) -> None:
        """§3.2 row 2, §5.4: write a held decision at the round boundary.

        The round the decision waited for has closed in the old epoch. A held
        `rebaseline` or (family 15, #263) `continue` writes its block
        (`authorization_applied`, `identity_rebased`), which now finds no open
        round, so it ends there and the next round binds under the new
        condition. The store is open by now, so the ledger half of the rebase
        is completed at once, before the store publishes anything else.

        A held family-15 `close` (#263) writes `authorization_applied` and the
        terminal it owes through `_write_close`, exactly as an immediate close
        does; it declares no `identity_rebased`, so no store rebase is owed.

        Either way a documentation repair `adopt` held back for the decision
        runs after its records (#278 F3); a block that moved the digest has
        already re-taken the documentation after its `identity_rebased`. A
        close that accepted a moved digest skips the repair: the journal keeps
        the old digest, and ADR-0012 admits only bytes matching it.
        """

        assert self.held_change is not None
        if self.held_change[5] == "close":
            assert self.held_close is not None
            journaled, rebuilt = self.held_close
            instance, authorization = self.held_change[3], self.held_change[4]
            changed = {difference.field for difference in self.held_change[2]}
            self.held_change = self.held_close = None
            self._write_close("close", journaled, rebuilt, instance, authorization)
            if "identity.reflector.environment_docs_digest" in changed:
                # ADR-0012: repair uses only bytes matching the journaled
                # identity. A close declares no `identity_rebased`, so the
                # journal still names the old digest and the changed live
                # documentation is not re-taken.
                self.environment_docs_repair = False
            else:
                self._repair_environment_docs()
            return
        if self.store_identity is None:
            raise CampaignInvariantViolated("a held store rebase needs the adopted store identity")
        # A held `continue`'s re-binds are written here, after `adopt_siblings`
        # ran, so a re-bound measurement would be declared but not born in
        # this invocation. On family 15 the set is empty at this boundary:
        # no round is open, and the closed round's promoted winner completed
        # before its promotion (ADR-0041), so `_rebind_sources` finds nothing.
        self.pending_change, self.held_change = self.held_change, None
        self._write_condition_change()
        self._repair_environment_docs()
        self._complete_owed_store_rebases(self.store_identity)

    # -- the connection gate (family 14, F0 R57-R61) -----------------------

    def connection_binding(self) -> tuple[str, dict[str, Any]]:
        """`D` and its `connection_binding/1` fields, over the current epoch (choice 4).

        Recomputed on every dispatch: an authorized comparable change opens a
        new condition epoch and so a new binding (ADR-0020).
        """

        snapshot = self.snapshot()
        fields = epoch_binding(len(snapshot.epochs) - 1, snapshot.epochs[-1])
        return binding_digest(**fields), fields

    def _stage_will_dispatch(self, measurement_id: str, plan: StagePlan) -> bool:
        """Whether this stage of `measurement_id` starts any rollout (choice 2).

        A pure read. A stage not opened yet dispatches the rows it declares
        (`_open_stage`'s count) and any repair owed from elsewhere; an open
        one the rows `_stage_dispatch_rows` still returns.
        """

        measurement = self.measurements[measurement_id]
        opened = len(self.snapshot().measurements[measurement_id]["stages"])
        if plan.stage > opened and len(plan.rows) > len(_carried(measurement, plan)):
            return True
        return bool(self._stage_dispatch_rows(measurement_id, plan)[0].rows)

    def _read_will_dispatch(self, measurement_id: str, plan: StagePlan) -> bool:
        with _boundary("measurement_journal", measurement_id):
            return self._stage_will_dispatch(measurement_id, plan)

    def _joint_will_dispatch(self, measurement_ids: tuple[str, ...], stage: int) -> bool:
        """`_stage_will_dispatch` over a joint stage's sides; a side whose plan
        cannot be read dispatches nothing (`run_joint_stage` holds its failure)."""

        for mid in measurement_ids:
            try:
                plan = self.plans_for(mid)[stage - 1]
            except (IndexError, KeyError, ValueError):
                # The side's own plan failure, which `run_joint_stage` holds.
                continue
            # A measurement-journal fault is classified and propagates: a
            # storage fault starts no dispatch (ADR-0008).
            with _boundary("measurement_journal", mid):
                if self._stage_will_dispatch(mid, plan):
                    return True
        return False

    async def ensure_connection(
        self, dispatch: Callable[[], bool]
    ) -> ConnectionAdmission | None | _Refused:
        """R57: no family-14 rollout starts under a binding without a compatible probe.

        The first statement of `run_stage` and `run_joint_stage`, before any
        stage opens. Below family 14, or for a stage that dispatches nothing,
        it returns `None` at once and changes nothing. Otherwise it returns
        the admission token of the current binding `D` (choice 3):

        - the token of a compatible preflight this invocation already wrote
          for `D`;
        - a binding-only spent token when `D`'s boundary -- a measurement
          `rollout_started` under `D` carrying an admission -- is already
          durable (a resume after the boundary needs no probe);
        - otherwise one probe, single flight: the first caller probes, every
          concurrent one waits and reads its result.

        It returns `_REFUSED`, and the caller dispatches nothing, when the
        probe failed (its terminal and suspension are written back to back),
        when it was interrupted, or when no probe may start because the
        campaign is stopping, cancelled, past a threshold or held by an
        unavailable probe check (ruling 18), or while the journal owes a
        connection record from an unsettled earlier preflight (review round 1,
        item 1).
        """

        if not dispatch():
            return None
        digest, fields = self.connection_binding()
        if self.checks.mode("connection_preflight") == "off":
            # Preserve S1's binding fields without issuing an admission.
            return ConnectionAdmission.spent(digest)
        if self._connection_shadow_settled(digest):
            return ConnectionAdmission.spent(digest)
        token = self._connection_token(digest)
        if token is not None:
            return token
        async with self.connection_lock:
            if self._connection_shadow_settled(digest):
                return ConnectionAdmission.spent(digest)
            token = self._connection_token(digest)
            if token is not None:
                return token
            if self._connection_record_owed():
                # The caller that held the lock left its preflight unsettled --
                # a classified storage fault (or a
                # crash) after its start or failed terminal. That fault
                # surfaces from its own task, but only once `reap` runs, and
                # this waiter woke first. No second start (the lifecycle
                # refuses one) and no dispatch; stopping here is what keeps
                # this caller's Attempt from re-planning the refused stage
                # without ever yielding to that `reap`.
                self.stopping = True
                return _REFUSED
            if not self._connection_may_probe():
                return _REFUSED
            return await self._run_connection_probe(digest, fields)

    def _connection_shadow_settled(self, digest: str) -> bool:
        if self.checks.mode("connection_preflight") != "shadow":
            return False
        latest = self.snapshot().connection.latest.get(digest)
        if latest is None:
            return False
        assert self.log is not None
        return any(
            record.event == "check_shadowed"
            and record.payload["check"] == "connection_preflight"
            and record.payload["operation_id"] == latest["operation_id"]
            for record in self.log.snapshot().records
        )

    def _connection_token(self, digest: str) -> ConnectionAdmission | None:
        token = self.connection_tokens.get(digest)
        if token is None and self._connection_boundary(digest):
            # C§4.1: the boundary is the binding's first local start; once it
            # is durable no probe runs again, and every later start names the
            # binding with a null admission.
            token = self.connection_tokens[digest] = ConnectionAdmission.spent(digest)
        return token

    def _connection_boundary(self, digest: str) -> bool:
        """Whether a declared measurement journal holds `D`'s admitted start."""

        snapshot = self.snapshot()
        for measurement_id, end in snapshot.cited_ends.items():
            # Ruling 24 (ADR-0008): a journal shorter than what the campaign
            # already cites from it was lost or truncated -- even one
            # `adopt_siblings` re-created empty -- and it may have held the
            # boundary. A storage fault starts no dispatch, so no probe and no
            # second admission run over it.
            events = Measurement.directory_for(self.run_root, measurement_id) / EVENTS_FILE
            with _boundary("measurement_journal", measurement_id):
                try:
                    size = events.stat().st_size
                except FileNotFoundError:
                    size = 0
                except OSError as exc:
                    raise _journal_fault(exc) from exc
                if size < end:
                    raise JournalFault("journal_unavailable")
        for measurement_id in snapshot.measurements:
            if Measurement.classify(self.run_root, measurement_id) != "born":
                continue
            for record in self.measurement_records(measurement_id):
                if (
                    record.event == "rollout_started"
                    and record.payload.get("connection_binding_digest") == digest
                    and record.payload.get("connection_admission") is not None
                ):
                    return True
        return False

    def _connection_record_owed(self) -> bool:
        """Whether the journal owes a connection record before any new start: an
        open start's terminal, or a failed terminal's suspension (choice 17).
        The lifecycle refuses a `connection_preflight_started` in both states."""

        connection = self.snapshot().connection
        return connection.open is not None or connection.owes_suspension is not None

    def _connection_may_probe(self) -> bool:
        """Ruling 18: no probe starts once the campaign is stopping or cancelled
        (the caller's token, or a durable cancelled role terminal), past a
        threshold, owing an applied close's terminal, or held by an unavailable
        probe check -- the grammar admits no preflight there. Nor while a
        Grouper suspension is owed (H8 merge): its audit wants it next."""

        if self.stopping or self.pending is not None or self.cancelled():
            return False
        snapshot = self.snapshot()
        if (
            snapshot.threshold is not None
            or snapshot.owed_terminal is not None
            or snapshot.finished is not None
            or snapshot.cancelled_role_seq is not None
            # H8 merge (ADR-0034, F0 R50): a Grouper `role_failed` owes its
            # suspension next; no preflight record may land in between.
            or any(
                reason in GROUPER_SUSPENSIONS for reason, _ in snapshot.suspension_causes.values()
            )
        ):
            return False
        return probe_continues(snapshot.identity, snapshot.family_version) or not any(
            state.probe_check is not None and state.probe_check.get("status") == "unavailable"
            for state in snapshot.rounds.values()
        )

    def _connection_stopped(self) -> bool:
        """The probe's cooperative stop: the caller's token or another task's stop."""

        return self.stopping or self.cancelled()

    async def _run_connection_probe(
        self, digest: str, fields: Mapping[str, Any]
    ) -> ConnectionAdmission | _Refused:
        """One preflight of binding `digest`: its start, its probe and its terminal (R53-R56).

        The start is durable before the probe journal is born (ADR-0008). A
        compatible probe admits `D` for this invocation; a failed one writes
        its terminal and the connection suspension back to back (R61); a
        probe stopped by the caller's token or by another task's stop writes
        `connection_preflight_interrupted` and never a suspension (ruling 14).
        A probe-journal or trace storage fault takes the measurement-journal
        fault path, leaving the start open for recovery; a crash leaves it
        open too.
        """

        assert self.store is not None and self.identity is not None
        assert self.connection is not None
        self.note_independent_report()
        snapshot = self.snapshot()
        champion_version, champion_digest = snapshot.champion
        seed = probe_seed(digest, champion_digest)
        with _boundary("artifact_store"):
            champion = self.store.champion()
            if champion.digest != champion_digest:
                champion = self.store.published_ref("champion", champion_version)
        identity = probe_identity(
            measurement_identity(self.identity, self.corpus, champion_digest, "champion"),
            self.corpus,
            seed,
            **(
                {"sample_retries": self.connection.preflight_sample_retries}
                if self.connection.preflight_sample_retries is not None
                else {}
            ),
        )
        operation_id = uuid4().hex
        hits = HitSink() if self.family_version == 15 else None
        check_options: dict[str, Any] = {} if hits is None else {"modes": self.checks, "hits": hits}
        self.append(
            CONNECTION_START_EVENT,
            dict(
                operation_id=operation_id,
                invocation_id=self.invocation_id,
                binding_digest=digest,
                epoch=fields["epoch"],
                case_id=fields["case_id"],
                case_index=fields["case_index"],
                target=fields["target"],
                environment=fields["environment"],
                scorer=fields["scorer"],
                metric_contract=fields["metric_contract"],
                primary_metric=fields["primary_metric"],
                candidate_digest=champion_digest,
                dependency_digest=snapshot.champion_dependency_digest,
                probe_seed=seed,
            ),
            operation_id,
        )
        self.hook("after_connection_preflight_started")
        budget = self.policy.budget
        try:
            # As an inspection journal: an S1 journal that is no bound
            # measurement, so its storage faults name none (ruling 11).
            with _boundary("measurement_journal"):
                result = await run_probe(
                    run_root=self.run_root,
                    operation_id=operation_id,
                    campaign_run_id=self.run_id,
                    identity=identity,
                    corpus=self.corpus,
                    secrets=self.secrets,
                    secret_paths=self.secret_paths,
                    secret_names=self.secret_names,
                    adapter=self.adapter,
                    candidate=champion,
                    strategy=self.strategy,
                    call_attempts=self.call_attempts(),
                    environment=self.environment,
                    scorer=self.scorer,
                    budget=RolloutBudget(
                        budget.max_turns,
                        budget.timeout_seconds,
                        max_failed_turns=budget.max_failed_turns,
                        call_retries=budget.call_retries,
                        cancelled=self._connection_stopped,
                    ),
                    expected=self.expected_semantic,
                    contract=self.connection.metric_contract,
                    primary_metric=str(fields["primary_metric"]),
                    on_born=lambda: self.hook("after_connection_probe_journal_born"),
                    **check_options,
                )
        except asyncio.CancelledError as cancelled:
            # Ruling 14: the caller cancelled the task while the probe ran. The
            # cancellation is never lost: a fault closing the start is chained
            # to it, and the open start is recovery's (Task 8).
            try:
                await self._interrupt_connection(operation_id, "cancelled")
            except BaseException as fault:
                raise cancelled from fault
            raise
        # The probe polls its cooperative stop only between turns, so a stop
        # can arrive in a window it never saw: materialize and attestation, the
        # last turn, the field check, scoring. It is read again here, with no
        # `await` before the record it decides (fix round 1).
        cancelled_role = self.snapshot().cancelled_role_seq is not None
        caller = self.cancelled() or cancelled_role
        stopped = caller or self.stopping or self.pending is not None
        compatible = result.outcome is not None and result.outcome.status == "compatible"
        if result.outcome is None or (stopped and not compatible):
            # A stop: the caller's token (or a durable cancelled role
            # terminal, Task 6 rule a), or another task's. Never a suspension
            # (choice 8): the stop's own terminal must stay writable.
            reason: Literal["cancelled", "interrupted"] = "cancelled" if caller else "interrupted"
            self.append(CONNECTION_INTERRUPTED_EVENT, result.interrupted(reason), operation_id)
            self.hook("after_connection_preflight_terminal")
            return _REFUSED
        if hits is not None:
            for entry in hits.entries():
                self.append(
                    "check_shadowed", {**entry, "round": None, "operation_id": operation_id}
                )
        event, payload = result.terminal()
        if event == CONNECTION_FINISHED_EVENT:
            self.append(event, payload, operation_id)
            self.hook("after_connection_preflight_terminal")
            if stopped:
                # Compatible, but the campaign stopped meanwhile: nothing is
                # dispatched and nothing is admitted in this invocation.
                return _REFUSED
            token = ConnectionAdmission(digest, operation_id, self.invocation_id)
            self.connection_tokens[digest] = token
            return token
        self._suspend_connection(operation_id, payload)
        if self._connection_shadow_settled(digest):
            return ConnectionAdmission.spent(digest)
        return _REFUSED

    def _suspend_connection(
        self, operation_id: str, payload: dict[str, Any], *, stop: bool = True
    ) -> None:
        """R61, choice 13: the failed terminal and its suspension, back to back.

        Both appends run under the campaign writer's reentrant mutex with no
        `await` between them, so neither another coroutine nor another thread
        can append in between: every `AppendLog` append takes the same lock.
        A record another task appends afterwards is admitted, because any
        record may follow a suspension (ADR-0019). Nothing else is written as
        this invocation's terminal (`write_terminal`); the barrier still drains
        the tasks and releases every instance.

        Recovery's unsettled terminal goes through this same section (Task 8).
        `stop=False` is the close's: an accepted close writes its own terminal
        after the pair, so this invocation is not made a suspended one.
        """

        assert self.log is not None
        with self.log.mutex:
            self.append(CONNECTION_FAILED_EVENT, payload, operation_id)
            # A sync seam inside the mutex: a crash here leaves the suspension
            # owed, and recovery writes it first.
            self.hook("after_connection_preflight_terminal")
            owed = self.snapshot().connection.owes_suspension
            assert owed is not None and owed["operation_id"] == operation_id
            if self._settle_shadow_connection(owed):
                return
            suspension = _connection_suspension(owed)
            self.append("campaign_suspended", suspension)
            if stop:
                self._connection_stop(suspension)
        self.hook("after_connection_suspended")

    def _shadow_connection_failure(self, status: str) -> bool:
        family15 = self.snapshot().family15
        return (
            family15 is not None
            and family15.check_modes.get("connection_preflight") == "shadow"
            and status in {"connection_preflight_unavailable", "connection_preflight_inconclusive"}
        )

    def _settle_shadow_connection(self, owed: Mapping[str, Any]) -> bool:
        if not self._shadow_connection_failure(str(owed["reason"])):
            return False
        self.append(
            "check_shadowed",
            dict(
                check="connection_preflight",
                code=owed["reason"],
                count=1,
                round=None,
                operation_id=owed["operation_id"],
            ),
        )
        return True

    def _connection_stop(self, suspension: dict[str, Any]) -> None:
        """This invocation ends on the connection suspension it wrote (ruling 5)."""

        self.suspended = suspension
        self.connection_suspended = True
        self.stopping = True

    def _write_owed_connection_suspension(
        self, owed: Mapping[str, Any], *, stop: bool = True
    ) -> None:
        """Choice 17: the suspension a failed terminal owes, the only record it admits.

        A crash between the two records of `_suspend_connection` left it owed;
        it is written before any other record of this invocation, under the
        same mutex, and (outside a close) this invocation then ends suspended
        as the live one would have. The next run probes afresh.
        """

        assert self.log is not None
        with self.log.mutex:
            if self._settle_shadow_connection(owed):
                return
            suspension = _connection_suspension(owed)
            self.append("campaign_suspended", suspension)
            if stop:
                self._connection_stop(suspension)
        self.hook("after_connection_suspended")

    async def recover_connection(
        self,
        admitted_versions: Collection[str],
        *,
        reason: Literal["cancelled", "interrupted"] = "interrupted",
        stop: bool = True,
    ) -> None:
        """R62, C§4.4: settle what a crash left of a connection preflight.

        Runs after every refusal point and before any other record of the
        invocation (`adopt` before `_write_consequences`, a close before its
        records, a cancellation before its terminal), so a refused invocation
        writes nothing, and every start ends with exactly one terminal:

        - a failed terminal whose suspension is owed gets it first (choice 17);
        - an open start's leftover is released under the probe journal's own
          identity, then the start closes `interrupted` with the cleanup read
          back; an unborn probe journal (a crash before its first record) holds
          nothing durable and is removed (ruling 22);
        - a leftover the Target still refuses to release cannot be settled:
          outside a cancellation the start closes `failed / unavailable /
          cleanup_failed` and its suspension follows in the same mutex
          section (R61). Under a durable cancellation only `interrupted`
          stands (ruling 14), with `cleanup: failed`.

        Before that, every closed probe whose terminal recorded `cleanup:
        failed` is offered its release again (best effort, no campaign
        record), so a repaired Target gets its instance back. An
        `IdentityError` reopening an open probe's journal propagates with
        nothing written (ruling 16); a closed one's is left as it is.
        """

        view = self.snapshot().connection
        if view.owes_suspension is not None:
            self._write_owed_connection_suspension(view.owes_suspension, stop=stop)
            return
        for entry in view.operations:
            terminal = entry["terminal"]
            if terminal is None or terminal.get("cleanup") != "failed":
                continue
            with _boundary("measurement_journal"), suppress(IdentityError):
                await release_probe_leftover(
                    probe_path(self.run_root, str(entry["operation_id"])),
                    self.corpus,
                    self.secrets,
                    self.adapter,
                    admitted_versions,
                )
        if view.open is None:
            return
        operation_id = str(view.open["operation_id"])
        with _boundary("measurement_journal"):
            await release_probe_leftover(
                probe_path(self.run_root, operation_id),
                self.corpus,
                self.secrets,
                self.adapter,
                admitted_versions,
            )
            settlement = probe_settlement(self.run_root, operation_id)
            if not settlement.evidence:
                _remove_unborn_probe(self.run_root, operation_id)
        if reason == "cancelled" or settlement.cleanup != "failed":
            self.append(CONNECTION_INTERRUPTED_EVENT, settlement.interrupted(reason), operation_id)
            self.hook("after_connection_preflight_terminal")
            return
        _event, payload = settlement.unsettled()
        self._suspend_connection(operation_id, payload, stop=stop)

    async def _interrupt_connection(
        self, operation_id: str, reason: Literal["cancelled", "interrupted"]
    ) -> None:
        """Close an interrupted probe's start (ruling 14, Task 5 handoff).

        The leftover is released first, so the recorded cleanup reflects the
        reconciliation; a refused release is recorded as `cleanup: failed`.
        """

        with _boundary("measurement_journal"):
            await release_probe_leftover(
                probe_path(self.run_root, operation_id),
                self.corpus,
                self.secrets,
                self.adapter,
                self.admitted_versions,
            )
            settlement = probe_settlement(self.run_root, operation_id)
        self.append(CONNECTION_INTERRUPTED_EVENT, settlement.interrupted(reason), operation_id)
        self.hook("after_connection_preflight_terminal")

    async def reap(self) -> None:
        done, _pending = await asyncio.wait(
            set(self.tasks.values()), return_when=asyncio.FIRST_COMPLETED
        )
        for attempt_id, task in list(self.tasks.items()):
            if task not in done:
                continue
            del self.tasks[attempt_id]
            if task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, _Fault):
                self.fault = self.fault or exc
                self.stopping = True
            elif exc is not None and self.error is None:
                self.error = exc
                self.stopping = True

    async def run_attempt(self, attempt_id: str) -> None:
        while True:
            view = self.view()
            attempt = self.attempt_state(view.snapshot, attempt_id)
            if attempt is None:
                round_number, index = self.slot_of(attempt_id)
                check = view.global_preflights.get(round_number)
                path = "early_global" if check is not None and check.impossible else "candidate"
                action: Action | None = OpenAttempt(round_number, index, attempt_id, path)
            else:
                action = (
                    _rejected_focus_action(attempt)
                    if attempt in _rejected_focus_attempts(view.snapshot)
                    else attempt_action(view, self.policy, attempt)
                )
            if action is None:
                return
            # Spec §7's terminal barrier: once the campaign stops opening work the
            # only step an attempt still takes is its own closure — which is how an
            # attempt whose editor the barrier cancelled reaches
            # `abandoned(editor_failed, cancelled)` rather than staying open. The
            # caller's token is read here too, not only through `self.stopping`:
            # the drive loop that sets `stopping` may still be parked in `reap()`
            # when the token is set, and an attempt must take no new step in that
            # window either.
            if (self.stopping or self.cancelled()) and not isinstance(action, CloseAttempt):
                return
            await self.perform(action)

    @staticmethod
    def slot_of(attempt_id: str) -> tuple[int, int]:
        head, tail = attempt_id.split("-", 1)
        return int(head[1:]), int(tail[1:])

    @staticmethod
    def attempt_state(snapshot: CampaignSnapshot, attempt_id: str) -> AttemptState | None:
        for state in snapshot.rounds.values():
            attempt = state.attempts.get(attempt_id)
            if attempt is not None:
                return attempt
        return None

    def note_independent_report(self, profile: Any = None) -> None:
        """Synchronous pre-spend presentation; observer failure changes no work."""
        if self.family_version != 15 or self.notes is None:
            return
        from evolution.tree.family15_report import (
            campaign15_projection,
            explain_preflight,
            preflight_lines,
            report_lines,
        )

        try:
            from evolution.tree.case_comparison import preflight

            report = campaign15_projection(
                self.snapshot(), preflights=self.independent_global_preflights()
            )
            lines = report_lines(report)
            if profile is not None:
                check = explain_preflight(preflight(profile, None))
                lines.extend(preflight_lines(check, label="local"))
            marker = tuple(lines)
            if getattr(self, "_independent_note_marker", None) == marker:
                return
            self._independent_note_marker = marker
            for line in lines:
                try:
                    self.notes(line)
                except Exception:
                    pass
        except Exception:
            pass  # This optional presentation must never own execution.

    async def perform(self, action: Action) -> None:
        from evolution.tree.campaign_view import AdvanceCandidateCompletion

        if isinstance(
            action, (MeasureBaselineStage, RunGrouping, RunReflection, RunEditor, FailPreflight)
        ):
            self.note_independent_report()

        if isinstance(action, AdvanceCandidateCompletion):
            await self.advance_candidate_completion(action.attempt_id)
            return
        if isinstance(action, BindBaseline):
            self.bind_baseline(action)
        elif isinstance(action, BindBaselineRefresh):
            self.bind_baseline_refresh(action)
        elif isinstance(action, CompleteBaselineRefresh):
            self.complete_baseline_refresh(action)
        elif isinstance(action, MeasureBaselineStage):
            await self.baseline_stage(action.round, action.measurement_id, action.stage)
        elif isinstance(action, CompleteBaseline):
            await self.close_measurement(action.round, action.measurement_id, "baseline_complete")
        elif isinstance(action, RunGrouping):
            await self.run_grouping(action.round)
        elif isinstance(action, SelectFocusPortion):
            await self.select_focus_portion(action.round)
        elif isinstance(action, RunReflection):
            await self.run_reflection(action.round)
        elif isinstance(action, SelectGateScope):
            await self.select_gate_scope(action.round)
        elif isinstance(action, DecideRound):
            await self.decide_round(action.round)
        elif isinstance(action, FinishCampaign):
            self.stop(
                _Pending(
                    action.outcome,
                    action.reason,
                    action.continuation,
                    evidence=self.grouping_failure_evidence()
                    if action.reason == "grouping_failed"
                    else self.probe_failure_evidence()
                    if action.reason == "probe_unavailable"
                    else None,
                )
            )
        elif isinstance(action, OpenAttempt):
            self.open_attempt(action)
        elif isinstance(action, FailPreflight):
            self.fail_independent_preflight(action.attempt_id)
        elif isinstance(action, RunEditor):
            await self.run_editor(action.attempt_id)
        elif isinstance(action, SealAttempt):
            self.seal_attempt(action.attempt_id)
        elif isinstance(action, SelectAffected):
            await self.select_attempt_cases(action.attempt_id)
        elif isinstance(action, BindMeasurement):
            self.bind_measurement(action.attempt_id)
        elif isinstance(action, BindParent):
            self.bind_parent(action.attempt_id)
        elif isinstance(action, BindComparison):
            self.bind_comparison(action.attempt_id)
        elif isinstance(action, CalibrateNoise):
            await self.calibrate(action.attempt_id)
        elif isinstance(action, RunAttemptStage):
            await self.attempt_stage(action.attempt_id, action.stage)
        elif isinstance(action, DecideFocus):
            attempt = self.attempt_state(self.snapshot(), action.attempt_id)
            assert attempt is not None and attempt.measurement_id is not None
            current_policy = self.policy
            try:
                # A held policy change cannot reinterpret an old durable verdict.
                self.policy = self._bound_policy(attempt.measurement_id)
                self.decide_focus(action.attempt_id)
            finally:
                self.policy = current_policy
        elif isinstance(action, EvaluateLook):
            await self.evaluate(action.attempt_id, action.look)
        elif isinstance(action, CloseAttempt):
            await self.close_attempt(
                action.attempt_id, action.outcome, action.reason, action.look, None
            )
        elif isinstance(action, BindFinalists):
            if self.family_version == 14 and self.policy.round_stop == "first_accepted":
                # Immediate Attempt decisions have already run their barriers.
                # A later binding must not re-decide the previous final.
                self.hook("before_finalists_bound")
                self.bind_finalists(action.round)
                await self.close_family_13_selection(action.round)
            else:
                await self.decide_round(action.round)
        elif isinstance(action, BindCohortMember):
            self.bind_cohort_member(action)
        elif isinstance(action, RunCohortStage):
            await self.cohort_stage(action)
        elif isinstance(action, CompleteCohortMember):
            self.complete_cohort_member(action)
        else:  # pragma: no cover - the planner emits nothing else
            raise AssertionError(f"unknown campaign action: {action!r}")

    # -- baselines and winners --------------------------------------------

    def bind_baseline(self, action: BindBaseline) -> None:
        self.publish_pending_diagnostics()
        snapshot = self.snapshot()
        version, digest = snapshot.champion
        fresh = action.measurement_id not in snapshot.measurements
        state = snapshot.rounds.get(action.round)
        if state is None or state.baseline_measurement_id is None:
            self.append(
                "baseline_bound",
                dict(
                    round=action.round,
                    measurement_id=action.measurement_id,
                    champion_version=version,
                    candidate_digest=digest,
                ),
            )
        else:
            # #106: a resume after a cut between the two records. The round
            # already names this baseline; only its declaration and birth
            # are owed.
            assert fresh and state.baseline_measurement_id == action.measurement_id
        if fresh:
            measurement = self.derive(action.measurement_id, digest, "champion")
            self.append(
                "measurement_bound",
                dict(
                    attempt_id=CHAMPION_SIDE,
                    round=action.round,
                    measurement_id=action.measurement_id,
                    candidate_digest=digest,
                    identity_digest=measurement.identity.digest,
                ),
            )
            self.hook("after_measurement_bound")
            # ADR-0019 (amended 2026-10-03): the binding names the version the
            # declared import was admitted under.
            measurement.primary_range = self.range_of(action.measurement_id)
            imported = self.birth_import(action.round)
            with _boundary("measurement_journal", action.measurement_id):
                measurement.create(imported=imported)
            self.measurements[action.measurement_id] = measurement
            self.siblings[action.measurement_id] = "born"
        # The baseline is bound and, when it was fresh, born: the seam a fixture
        # cuts to leave a real incomplete champion measurement behind.
        self.hook("after_baseline_bound")

    def bind_baseline_refresh(self, action: BindBaselineRefresh) -> None:
        """Family 14 (F0 R43, ADR-0032 amended 2026-10-09): open a due refresh.

        The payload is the planner's and the lifecycle's shared derivation
        (`refresh_due_for`): N, the counted rounds and the replaced
        measurement, whose prefix is the previous round's frozen
        `baseline_reference`. The record also cites the last counted
        `round_closed`, the closure that reached N. The refresh measurement
        itself is bound next by the ordinary `bind_baseline`.
        """

        snapshot = self.snapshot()
        due = refresh_due_for(snapshot, self.policy, action.round)
        assert due is not None, "the planner proposes only a due refresh"
        previous = snapshot.rounds[action.round - 1]
        reference = previous.baseline_reference
        assert reference is not None and previous.baseline_measurement_id == due.replaced
        assert self.log is not None
        closure = next(
            record.to_payload()["sequence"]
            for record in self.log.snapshot().records
            if record.event == "round_closed" and record.payload["round"] == due.last
        )
        self.append(
            "baseline_refresh_bound",
            dict(
                round=action.round,
                measurement_id=refresh_measurement_id_for(action.round),
                baseline_refresh_after_rounds=due.after_rounds,
                counted_rounds={"first": due.first, "last": due.last},
                replaced={
                    "measurement_id": due.replaced,
                    "prefix_digest": reference["content_digest"],
                },
                evidence=[dict(reference), self.record_reference(int(closure))],
            ),
        )
        self.hook("after_baseline_refresh_bound")

    def complete_baseline_refresh(self, action: CompleteBaselineRefresh) -> None:
        """Family 14 (F0 R43): close a refresh once its baseline is complete.

        It names the round's current baseline (the refresh measurement, or its
        `@e<k>` rebind after a `continue`) and cites the round's frozen
        `baseline_reference`; grouping may then read the refreshed baseline.
        """

        state = self.snapshot().rounds[action.round]
        assert state.baseline_measurement_id == action.measurement_id
        reference = state.baseline_reference
        assert reference is not None
        self.append(
            "baseline_refresh_completed",
            dict(
                round=action.round,
                measurement_id=action.measurement_id,
                evidence=[dict(reference)],
            ),
        )
        self.hook("after_baseline_refresh_completed")

    def prepare_link_diagnostics(
        self,
        source: SourceMeasurement,
        rows: Sequence[Mapping[str, Any]],
        target: Any,
        *,
        source_root: Path,
    ) -> None:
        if self.family_version != 15 or not rows:
            return
        import tempfile

        from evolution.tree.artifacts.artifact_payloads import describe_auxiliary_files
        from evolution.tree.baseline_diagnostics import prepare_import_diagnostics
        from evolution.tree.execution.interaction import CandidateView

        self.diagnostic_staging = tempfile.TemporaryDirectory(prefix="evo-diagnostics-")
        candidate = CandidateView(
            self.seed_root,
            target.to_payload()["candidate_digest"],
            (),
            verifier=lambda root: scan_tree(root, self.artifact_policy).candidate_digest,
        )
        prepared = prepare_import_diagnostics(
            source,
            rows,
            target=target,
            source_root=source_root,
            destination=Path(self.diagnostic_staging.name) / "bundle",
            corpus=self.corpus,
            candidate=candidate,
        )
        files = describe_auxiliary_files(prepared.directory, purpose="baseline_diagnostics")
        declaration = dict(
            family_version=15,
            purpose="baseline_diagnostics",
            format="baseline-diagnostic-bundle/1",
            content_digest=prepared.digest,
            files=[f.to_payload() for f in files],
        )
        # The first diagnostic declaration freezes the exact greedy subset,
        # including gap bytes. Re-preparation may not silently lower coverage.
        if (self.run_root / CAMPAIGN_FILE).exists():
            observed = inspect_log(
                self.run_root,
                file_name=CAMPAIGN_FILE,
                max_record_bytes=MAX_RECORD_BYTES,
                max_log_bytes=MAX_JOURNAL_BYTES,
                state_factory=lambda: CampaignLifecycle(selector=None, family_version=15),
                envelope_family=self.envelope_family,
            )
            previous = [
                r
                for r in observed.records
                if r.event == "auxiliary_payload_declared"
                and r.payload["purpose"] == "baseline_diagnostics"
            ]
            if previous and any(dict(r.payload) != declaration for r in previous):
                declared = observed.records[0].payload["identity"]["linked_from"]["baseline"]
                raise BaselineImportUnavailable(
                    baseline_import_instance_digest(self.run_id, str(declared["pairs_digest"]))
                )
        count = prepared.gaps.scheduled
        authentication = dict(
            score="journal_bound",
            stdout="captured_at_import",
            request="authenticated"
            if prepared.included == count
            else "partial"
            if prepared.included
            else "unavailable",
        )
        raw_bytes = source.valid_bytes or sum(len(r.encode()) for r in source.records)
        self.pending_diagnostics = prepared
        self.pending_diagnostic_data = dict(
            format="comparison-data/1",
            kind="diagnostic_import",
            destination=dict(
                measurement_id=measurement_id_for(None),
                identity=target.to_payload(),
                identity_digest=target.digest,
                prefix=None,
            ),
            source_prefixes=[
                dict(
                    measurement_id=source.measurement_id,
                    identity_digest=source.identity_digest,
                    candidate_digest=source.records[0].payload["identity"]["candidate_digest"],
                    prefix=dict(
                        version=1,
                        kind="journal_prefix",
                        run_id=source.records[0].to_payload()["run_id"],
                        locator=f"measurements/{source.measurement_id}/events.jsonl#bytes=0:{raw_bytes}",
                        content_digest=source.prefix_digest,
                    ),
                )
            ],
            rows_digest=import_pairs_digest(rows),
            slot_index=list(rows),
            gaps_digest=prepared.gaps.digest,
            counts=dict(prepared.gaps.counts),
            authentication=authentication,
        )

    def publish_pending_diagnostics(self) -> None:
        if self.family_version != 15 or self.pending_diagnostics is None:
            return
        from evolution.tree.artifacts.artifact_payloads import describe_auxiliary_files
        from evolution.tree.baseline_diagnostics import commit_diagnostics

        assert self.log is not None and self.store is not None
        if self.snapshot().baseline_abandoned is not None:
            return
        if any(r.event == "diagnostic_import_declared" for r in self.log.snapshot().records):
            return
        prepared = self.pending_diagnostics
        body = self.pending_diagnostic_data
        assert body is not None
        declaration = dict(
            family_version=15,
            purpose="baseline_diagnostics",
            format="baseline-diagnostic-bundle/1",
            content_digest=prepared.digest,
            files=[
                f.to_payload()
                for f in describe_auxiliary_files(
                    prepared.directory, purpose="baseline_diagnostics"
                )
            ],
        )
        old = next(
            (
                r
                for r in self.log.snapshot().records
                if r.event == "auxiliary_payload_declared" and dict(r.payload) == declaration
            ),
            None,
        )
        if old is None:
            written = self.append("auxiliary_payload_declared", declaration)
            reference = self.decision_reference(written)
            self.hook("after_diagnostics_declared")
        else:
            reference = self.record_reference(old.to_payload()["sequence"])
        with _boundary("artifact_store"):
            publication = commit_diagnostics(
                prepared,
                run_root=self.run_root,
                store=self.store,
                declaration={**declaration, "declaration_ref": reference},
            )
        self.hook("after_diagnostics_published")
        data = self.publish_comparison_data(body)
        self.append(
            "diagnostic_import_declared",
            dict(
                measurement_id=body["destination"]["measurement_id"],
                data=data,
                bundle=publication.to_payload(),
                rows_digest=body["rows_digest"],
                scheduled=prepared.gaps.scheduled,
                included=prepared.included,
                sufficiency="complete"
                if prepared.included == prepared.gaps.scheduled
                else "partial"
                if prepared.included
                else "unavailable",
                authentication=body["authentication"],
            ),
        )
        self.hook("after_diagnostic_import_bound")

    def declared_baseline(
        self, identity: CampaignIdentityV1 | None = None
    ) -> Mapping[str, Any] | None:
        """The identity's `linked_from.baseline` declaration, or `None` (§5.1)."""

        identity = identity or self.identity
        if identity is None:
            return None
        linked = identity.to_payload().get("linked_from")
        return None if linked is None else linked.get("baseline")

    def birth_import(self, round_number: int) -> tuple[Mapping[str, Any], ...]:
        """The rows the first-round baseline is born with (§5.2), verified.

        Only the successor's first round's baseline carries a declared import;
        every other birth, and a `fresh` link's, is empty. The rows are checked
        against the declaration's `imported` and `pairs_digest` before the
        birth, so a measurement is either absent or born with its whole
        declared set.
        """

        declared = self.declared_baseline()
        snapshot = self.snapshot()
        if declared is None or round_number != snapshot.first_round:
            return ()
        # §5.3: an accepted `fresh_baseline` releases the copy; the baseline
        # is born empty and measured in full.
        if declared["imported"] == 0 or snapshot.baseline_abandoned is not None:
            return ()
        rows = self.owed_import()
        if len(rows) != declared["imported"] or (
            import_pairs_digest(rows) != declared["pairs_digest"]
        ):
            raise CampaignInvariantViolated("the baseline import does not match its declaration")
        return rows

    def owed_import(
        self, identity: CampaignIdentityV1 | None = None, creation_version: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        """The declared import's rows (§5.2).

        A fresh run holds them since creation. A resume before the birth
        re-reads the predecessor through the manifest's `linked_from.run_dir`
        (reading 3) -- the only point at which a resume reads a predecessor --
        and caches the verified rows. Ruling R1: a declaration with
        `imported == 0` owes nothing, so nothing is read. `creation_version`
        is the creation record's producing version (default: the campaign's
        own), under which the declaration was admitted.
        """

        identity = identity or self.identity
        declared = self.declared_baseline(identity)
        if declared is None or declared["imported"] == 0:
            return ()
        if not self.pending_import:
            assert identity is not None
            if creation_version is None:
                creation_version = self.snapshot().creation_version
            self.pending_import = self._reread_import(identity, declared, creation_version)
        return self.pending_import

    def _reread_import(
        self, identity: CampaignIdentityV1, declared: Mapping[str, Any], creation_version: str
    ) -> tuple[Mapping[str, Any], ...]:
        """§5.2-§5.3: recompute the declared set from the source, or refuse.

        The admission rule is the one the creation record declared the set
        under (`creation_version`, ADR-0019 as amended 2026-10-03), so a
        version rebase since does not move the set.

        Read-only. Every way the source cannot yield the declared set -- no
        path, a missing, rewritten or other-family predecessor, an unreadable
        measurement, a prefix that is not `source_prefix_digest`, a set whose
        digest is not `pairs_digest` -- is `BaselineImportUnavailable`.
        """

        instance = baseline_import_instance_digest(self.run_id, str(declared["pairs_digest"]))
        if self.linked_from_path is None:
            raise BaselineImportUnavailable(instance)
        payload = identity.to_payload()
        linked = payload["linked_from"]
        root = Path(self.linked_from_path)
        # Only the reads of the source are the source's failures: a missing,
        # unreadable, other-family or unfinished predecessor, or a champion
        # measurement `read_source` cannot open. The recomputation below them
        # is this run's own, and an error there is an invariant bug that must
        # surface, never be offered to the operator as an authorizable refusal.
        try:
            predecessor = _predecessor(root, expected_family=self.family_version)
            source = read_source(root, predecessor.reduced)
        except (
            BaselineSourceUnavailable,
            CampaignMissing,
            CampaignUnreadable,
            LineageFamilyUnsupported,
            LineageNotFinished,
            JournalBoundaryError,
            SchemaError,
            OSError,
        ):
            raise BaselineImportUnavailable(instance) from None
        if (
            predecessor.reduced.run_id != linked["run_id"]
            or source.measurement_id != declared["source_measurement_id"]
            or source.prefix_digest != declared["source_prefix_digest"]
        ):
            raise BaselineImportUnavailable(instance)
        target = measurement_identity(
            identity,
            self.corpus,
            str(payload["artifact_store"]["seed_candidate_digest"]),
            "champion",
        )
        imported = admit_rows(
            source,
            target,
            primary=self.policy.metric_primary,
            condition_changed=tuple(linked.get("condition_changed", ())),
            dotted=False,
            primary_range=primary_range_required(self.family_version, creation_version),
        )
        if (
            imported.pairs_digest != declared["pairs_digest"]
            or imported.imported != declared["imported"]
        ):
            raise BaselineImportUnavailable(instance)
        self.prepare_link_diagnostics(source, imported.rows, target, source_root=root)
        return imported.rows

    def _admit_owed_import(self, reduced: CampaignSnapshot, identity: CampaignIdentityV1) -> None:
        """§5.2-§5.3, before the first write: complete an owed copy, or accept its loss.

        The copy is owed while the identity declares a non-empty import, no
        `fresh_baseline` released it, and the seed champion measurement is not
        born. A source that cannot yield the declared set is refused unless the
        caller handed an accepted decision for exactly this instance, whose
        `authorization_applied(effect: fresh_baseline)` `_write_consequences`
        then writes.
        """

        declared = self.declared_baseline(identity)
        if declared is None or declared["imported"] == 0:
            return
        if reduced.baseline_abandoned is not None:
            return
        # Ruling R9: a `rebaseline` before round 1 abandons the declared
        # import in its own block (the baseline is measured from empty under
        # the new identity), so the predecessor is neither read nor owed.
        if (
            self.pending_change is not None
            and self.pending_change[5] == "rebaseline"
            and rounds_begun(reduced) == 0
        ):
            return
        # §5.2, §7: a campaign that finished, or owes an applied close's
        # terminal, will never measure its baseline, so nothing is owed and
        # the predecessor is not read.
        if reduced.finished is not None or reduced.owed_terminal is not None:
            return
        if Measurement.classify(self.run_root, measurement_id_for(None)) == "born":
            return
        try:
            self.owed_import(identity, reduced.creation_version)
        except BaselineImportUnavailable as exc:
            decision = match_authorization(
                self.authorizations, "baseline_import_unavailable", exc.instance_digest
            )
            if decision is None:
                raise
            self.pending_fresh_baseline = (
                decision,
                exc.instance_digest,
                str(declared["pairs_digest"]),
            )

    async def baseline_stage(self, round_number: int, measurement_id: str, stage: int) -> None:
        outcome = await self.run_stage(measurement_id, stage)
        self.evaluate_measured_stage(
            round_number, measurement_id, outcome.faults, outcome.indeterminate
        )

    def evaluate_measured_stage(
        self,
        round_number: int,
        measurement_id: str,
        faults: Sequence[Mapping[str, Any]],
        indeterminate: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Spec §6 step 1: fault, then thresholds, then baseline-incomplete.

        Family 6 has no baseline-incomplete terminal (continuable-terminals spec
        §4.4): a stage returns with every sample scored or exhausted, and the
        exhausted ones are excluded when the measurement completes
        (`close_measurement`), so the third branch belongs to families 3-5.
        """

        if faults:
            first = faults[0]
            self.stop(
                _Pending(
                    "infrastructure_failed",
                    str(first["reason"]),
                    "none",
                    evidence=(first["reference"].to_payload(),),
                    faults=self.located_faults(measurement_id, faults),
                )
            )
            return True
        if self.thresholds(round_number, "baseline_stage", measurement_id):
            return True
        return False

    async def close_measurement(self, round_number: int, measurement_id: str, event: str) -> None:
        measurement = self.measurements[measurement_id]
        faults = measurement.fault_rows(self.policy.metric_primary)
        if faults and self.evaluate_measured_stage(round_number, measurement_id, faults, ()):
            return
        if not measurement.complete():
            # Resume reaches this with a measurement whose interrupted samples S1
            # reclassified; §6 step 1 rules on them exactly as a live stage would.
            blocked = self.blocked_rows(measurement_id)
            self.account(measurement_id, blocked)
            faults = measurement.fault_rows(self.policy.metric_primary)
            indeterminate = blocked
            if not faults and measurement.settled():
                # Continuable-terminals spec §4.2: every sample scored or
                # exhausted its tries. The §7 barrier rules first; then the
                # exhausted pairs are excluded and the measurement completes.
                if self.thresholds(round_number, "baseline_stage", measurement_id):
                    return
                self.write_pairs_excluded(round_number, measurement_id)
            elif not self.evaluate_measured_stage(
                round_number, measurement_id, faults, indeterminate
            ):
                self._list_champion_gaps(measurement_id)
                return
            else:
                return
        await self.release(measurement_id)
        self.append(
            event,
            dict(
                round=round_number,
                measurement_id=measurement_id,
                evidence=[self.prefix(measurement_id)],
            ),
        )
        if self.family_version == 15 and event == "baseline_complete":
            self.hook("after_baseline_complete")

    def _list_champion_gaps(self, measurement_id: str) -> None:
        """Families 13-14 (#194 spec G1, §3.1-§3.2): suspend on a Champion
        measurement's gaps, `close_measurement`'s last ruling.

        Reached only after the fault rows and the thresholds found nothing, on
        a measurement neither complete nor settled. While this invocation
        holds another decision (G6) -- a decided terminal, a stop, the
        caller's token -- nothing is listed and that decision is written
        instead. Any other refusal is an executor invariant breach, and
        raising keeps the planner from planning `CompleteBaseline` again
        forever: a durable record the run still owes is written by its own
        path before any planning, the lifecycle would refuse the listing
        anyway (plan choice 9), and this invocation's I2 block is written
        before any planning too (`_write_consequences`) while a held
        `rebaseline` admits only the round's decision (`drive`). A
        measurement with no gap either is the same breach (G1): the
        `infrastructure_failed` close it used to fall into is refused from
        family 7 on.
        """

        if not self._champion_listing_admissible(measurement_id):
            if self.pending is not None or self.stopping or self.cancelled():
                return
            raise CampaignInvariantViolated(
                f"{measurement_id} is unfinished while the campaign owes another record"
            )
        measurement = self.measurements[measurement_id]
        snapshot = self.snapshot()
        rows = champion_gap_rows(
            measurement_id,
            measurement.identity.to_payload()["schedule"],
            measurement.sample_rows(measurement.max_repeat),
            declined_tries(snapshot.retries_declined, measurement_id),
        )
        if not rows:
            raise CampaignInvariantViolated(
                f"{measurement_id} is neither complete nor settled and lists no gap"
            )
        owner = champion_owner(rows)
        outcome, reason = THRESHOLD_TERMINAL[owner]
        self.stop(
            _Pending(
                outcome,
                reason,
                "none",
                owner=owner,
                champion_rows=champion_rows_evidence(measurement_id, rows),
            )
        )

    def _champion_listing_admissible(self, measurement_id: str) -> bool:
        """G6 (plan choice 8): may this invocation list `measurement_id`'s gaps now?

        Modelled on `_connection_may_probe`. Not while this invocation is
        stopping, holds a decided record, sees the caller's token or holds an
        I2 block (`pending_change`, `held_change`); nor while the journal
        holds a terminal, an owed terminal, a pending threshold, a durable
        cancelled role terminal, an unfinished I2 block, an outstanding
        suspension cause, an open or owed connection record, or an
        unavailable probe check; nor for a measurement frozen or bound in an
        earlier epoch. A `probe_setup` cause is skipped exactly as the
        lifecycle's `_champion_listing` skips it: every ready probe check
        leaves one, and it is not an owed record.
        """

        if (
            self.stopping
            or self.pending is not None
            or self.cancelled()
            or self.pending_change is not None
            or self.held_change is not None
        ):
            return False
        snapshot = self.snapshot()
        if (
            snapshot.finished is not None
            or snapshot.owed_terminal is not None
            or snapshot.threshold is not None
            or snapshot.cancelled_role_seq is not None
            or snapshot.pending_change is not None
            or any(reason != "probe_setup" for reason, _ in snapshot.suspension_causes.values())
            or self._connection_record_owed()
            or (
                not probe_continues(snapshot.identity, snapshot.family_version)
                and any(
                    state.probe_check is not None
                    and state.probe_check.get("status") == "unavailable"
                    for state in snapshot.rounds.values()
                )
            )
        ):
            return False
        entry = snapshot.measurements.get(measurement_id)
        return (
            entry is not None
            and not entry.get("frozen", False)
            and int(entry["epoch"]) == len(snapshot.epochs) - 1
        )

    def write_pairs_excluded(self, round_number: int, measurement_id: str) -> None:
        """Record a settled champion-side measurement's exhausted pairs (spec §4.2).

        One `pairs_excluded` per measurement, naming its latest stage, before its
        `baseline_complete` / `champion_complete`; nothing when no sample is
        exhausted. The pairs are listed in schedule order, at most the first
        `256`, each with the owner and reason of its latest try and the number of
        tries it took; `count`, `by_owner` and `pairs_digest` cover the whole
        set. A resumed campaign that already made the record durable writes
        nothing again.
        """

        snapshot = self.snapshot()
        if snapshot.excluded.get(measurement_id):
            return
        measurement = self.measurements[measurement_id]
        rows = measurement.exhausted_rows()
        if not rows:
            return
        latest = {
            (row["case_id"], row["sample_id"], row["retry_ordinal"]): row
            for row in measurement.indeterminate_rows()
        }
        pairs = []
        by_owner: dict[str, int] = {}
        for row in rows:
            entry = latest[(row.case_id, row.sample_id, row.retry_ordinal)]
            owner = str(entry["owner"])
            by_owner[owner] = by_owner.get(owner, 0) + 1
            pairs.append(
                dict(
                    case_id=row.case_id,
                    sample_id=row.sample_id,
                    owner=owner,
                    reason=str(entry["reason"]),
                    tries=row.retry_ordinal + 1,
                )
            )
        stages = snapshot.measurements[measurement_id]["stages"]
        self.append(
            "pairs_excluded",
            dict(
                round=round_number,
                measurement_id=measurement_id,
                stage=int(stages[-1]["stage"]),
                count=len(pairs),
                by_owner=by_owner,
                pairs_digest=pairs_digest([(row.case_id, row.sample_id) for row in rows]),
                pairs=pairs[:MAX_EXCLUDED_PAIRS],
                truncated=len(pairs) > MAX_EXCLUDED_PAIRS,
                evidence=[self.prefix(measurement_id)],
            ),
        )

    def _gate_inputs(
        self,
        comparison: Mapping[str, Any],
        look: int,
        max_repeat: int,
        *,
        masked: bool = True,
    ) -> GateInputs:
        """`gate_inputs` over the baseline's exclusions to the comparison's last look.

        With `masked=False` no exclusion is read and the mask is empty: for a
        caller that reads only the horizon, as the audit's calibration does.
        Its `GateInvariant` is an executor invariant, as `evaluate_confidence`
        maps it (plan F10): every caller reads it outside that mapping.
        """

        excluded = (
            self.excluded_pairs(
                str(comparison["baseline_measurement_id"]),
                max(int(entry) for entry in comparison["looks"]),
            )
            if masked
            else frozenset()
        )
        try:
            return gate_inputs(comparison, max_repeat=max_repeat, look=look, excluded=excluded)
        except GateInvariant as exc:
            raise CampaignInvariantViolated(str(exc)) from exc

    def excluded_pairs(
        self,
        measurement_id: str,
        look: int,
        *,
        rows: Sequence[SampleRow] | None = None,
    ) -> frozenset[tuple[str, int]]:
        """The pairs a champion-side measurement excluded, restricted to repeats `<= look`.

        Recomputed from the measurement's own rows and checked against the
        journaled `pairs_excluded` summary (spec §4.2-§4.3): the set is exactly
        the exhausted rows, so a journal whose `count` or `pairs_digest` disagrees
        with the measurement is an executor invariant, never a decision input.
        Families before 6 exclude nothing. `rows`, when supplied, is the whole
        authoritative cited prefix, including repeats above `look`; both the
        summary check and the returned mask then avoid the live measurement.
        """

        exhausted = (
            self.measurements[measurement_id].exhausted_rows()
            if rows is None
            else tuple(row for row in rows if row.exhausted)
        )
        every = [(row.case_id, row.sample_id) for row in exhausted]
        journaled = dict(self.snapshot().excluded.get(measurement_id, {}))
        expected = {} if not every else {"count": len(every), "digest": pairs_digest(every)}
        recorded = (
            {}
            if not journaled
            else {
                "count": sum(count for count, _digest in journaled.values()),
                "digest": next(iter(journaled.values()))[1] if len(journaled) == 1 else None,
            }
        )
        if recorded != expected:
            raise CampaignInvariantViolated("pairs_excluded does not match the measurement")
        # Ruling R13: evaluation removes `(case_id, repeat)` from both sides, since
        # under `unpaired`/`none` seeds the Candidate's sample ids differ; the
        # record and its digest keep the baseline's sample ids.
        return frozenset((row.case_id, row.repeat) for row in exhausted if row.repeat <= look)

    # -- the reflector prelude (spec §3.3) ---------------------------------

    def carried_traces(
        self, records: Sequence[Envelope]
    ) -> dict[tuple[str, str], tuple[Envelope, Envelope]] | None:
        """A baseline's in-run carried rows' source scoring records (ADR-0017, 2026-10-02).

        Family 12 only: a row carried from a measurement of this same run -- a
        completion's import from its sealed winner, and its later in-run
        re-binds -- is read by focus, Reflector and editor through its source
        try's scorer trace (`baseline_import.carried_traces`). `None` below
        family 12, so families 5-11 see exactly their journal's records; a
        row carried across a link is never offered (PDR-0013's limitation).
        Family 15 additionally resolves cross-run rows through authenticated
        local diagnostic bundles; corruption remains a persistence fault.
        A pure function of durable bytes, so a resume recomputes the same view.
        """

        with _boundary("artifact_store"):
            return carried_traces(records, run_root=self.run_root, run_id=self.run_id)

    def verify_prefix_bytes(self, reference: Mapping[str, Any], *, exact: bool = False) -> None:
        """Read the admitted byte range without repairing or adopting journal bytes."""
        locator, bounds = reference["locator"].split("#bytes=")
        start, end = map(int, bounds.split(":"))
        path = self.run_root / locator
        descriptor = None
        try:
            if start != 0 or any(
                part.is_symlink() for part in (path, *path.parents) if part != self.run_root.parent
            ):
                raise ReaderIntegrityError("measurement_prefix_changed")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size < end
                or (exact and info.st_size != end)
            ):
                raise ReaderIntegrityError("measurement_prefix_changed")
            digest = hashlib.sha256()
            remaining = end
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise ReaderIntegrityError("measurement_prefix_changed")
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != reference["content_digest"]:
                raise ReaderIntegrityError("measurement_prefix_changed")
        except OSError as exc:
            raise ReaderIntegrityError("measurement_prefix_changed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def verify_adaptive_control(self) -> None:
        assert self.log is not None
        snapshot = self.log.snapshot()
        self.verify_prefix_bytes(
            {
                "locator": f"{CAMPAIGN_FILE}#bytes=0:{snapshot.valid_bytes}",
                "content_digest": snapshot.prefix_digest,
            },
            exact=True,
        )

    def probe_failure_evidence(self) -> tuple[dict[str, Any], ...] | None:
        assert self.log is not None
        return probe_unavailable_evidence(self.log.snapshot().records, self.snapshot())

    async def close_probes(self) -> bool:
        """Drain reserved calls, then revoke their durable append authority."""
        session = self.active_probe_session
        mediator = self.active_probe_mediator
        if session is None and mediator is None:
            return True
        from evolution.tree.campaign_reflector import EDITOR_GRACE_SECONDS

        if session is not None:
            session.stop_admission()
        quiescent = False
        cleanup_error: BaseException | None = None
        try:
            async with asyncio.timeout(EDITOR_GRACE_SECONDS):
                if mediator is not None:
                    quiescent = await mediator.close(grace_seconds=EDITOR_GRACE_SECONDS)
                elif session is not None:
                    quiescent = await session.close(grace_seconds=EDITOR_GRACE_SECONDS)
        except TimeoutError:
            pass
        except BaseException as exc:
            cleanup_error = exc
        finally:
            if session is not None:
                session.revoke_event_authority()
            if not quiescent:
                self.reflector_alive = True
                if mediator is not None:
                    mediator.abort()
            self.active_probe_session = None
            self.active_probe_mediator = None
        if session is not None:
            session.raise_if_failed()
        if cleanup_error is not None:
            raise cleanup_error
        return quiescent

    def grouping_failure_evidence(self) -> tuple[dict[str, Any], ...]:
        for state in reversed(tuple(self.snapshot().rounds.values())):
            if (
                state.grouping is not None
                and state.grouping["status"] == "failed"
                and state.grouping["code"] != "interrupted"
            ):
                return (self.record_reference(int(state.grouping["seq"])),)
        raise CampaignInvariantViolated("grouping failure has no terminal")

    def _reflection_needles_for(self, inputs: FrozenReflectionInputs) -> tuple[str, ...]:
        """The reflection-side needle set for role text built from `inputs`' baseline.

        Families before 14 return the session set (`self.reflection_needles`)
        unchanged. Family 14 (F0 R51, A§6, PDR-0017/PDR-0028 amendments) also
        drops every oracle string exactly equal to a verdict criterion id the
        scorer published on that frozen baseline, cached by its
        `baseline_binding`. The declared verbatim answers (ADR-0038) leave
        every family's set. The Candidate seal and attempt feedback keep
        `self.needles` in every family.
        """

        cache_key = inputs.baseline_binding
        if inputs.pin is not None:
            cache_key += ":" + hashlib.sha256(canonical_json(inputs.pin.payload())).hexdigest()
        cached = self.baseline_needles_cache
        if cached is None or cached[0] != cache_key:
            cached = (
                cache_key,
                reflection_needles(
                    self.corpus,
                    family_version=self.family_version,
                    published_criteria=inputs.published_criteria,
                    declared=self.declared_answers,
                    secret_paths=self.secret_oracle_paths,
                ),
            )
            self.baseline_needles_cache = cached
        return cached[1]

    def adaptive_inputs_for(
        self, round_number: int, *, hits: HitSink | None = None
    ) -> FrozenReflectionInputs:
        """Rebuild one lazy, baseline-bound read view from durable facts."""

        cached = self.adaptive_inputs_cache
        if (
            hits is None
            and cached is not None
            and cached[0] == str(self.snapshot().rounds[round_number].baseline_measurement_id)
            and cached[1].pin == self.snapshot().round_evidence(round_number)
        ):
            return cached[1]
        assert self.store is not None
        state = self.snapshot().rounds[round_number]
        measurement_id = str(state.baseline_measurement_id)
        try:
            measurement = self.measurements[measurement_id]
            parent = self.store.published_ref("champion", self.snapshot().champion[0])
            # Earlier families do not persist a round baseline reference.
            prefix = self.prefix(
                measurement_id,
                round_number=round_number,
            )

            def verify_baseline_sources() -> None:
                self.verify_prefix_bytes(prefix)
                try:
                    parent.verify()
                except ArtifactError as exc:
                    raise ReaderIntegrityError("parent_changed") from exc

            verify_baseline_sources()
            records = measurement.records()
            inputs = FrozenReflectionInputs(
                baseline_binding=prefix["content_digest"],
                rows=measurement.sample_rows(self.policy.repeats),
                records=records,
                run_root=self.run_root,
                corpus=self.corpus,
                baseline=parent,
                environment_kind=self.environment.identity.kind,
                metric_primary=self.policy.metric_primary,
                reflection_metadata=self.adaptive.reflection_metadata,
                families=self.adaptive.family_membership(self.corpus),
                families_configured=self.adaptive.families.get("kind") == "metadata",
                documents=self.environment_docs
                if isinstance(self.environment_docs, _V2DocumentSnapshot)
                else None,
                integrity_check=self.verify_adaptive_control,
                admission_check=verify_baseline_sources,
                context_page_size=self.adaptive.selection.context_page_size,
                scorer_diagnostics=True,
                model_grouping_family=self.model_grouping,
                family15=self.family_version == 15,
                carried=self.carried_traces(records),
                # Family 14 (PDR-0023): `read_evidence` filters hidden `skill_read`
                # paths for the Reflector; the grouping readers stay unfiltered.
                role_visible=None if self.role_view is None else self.role_view.visible,
                # #313: a scorer output carrying a run secret is unavailable to
                # the Grouper and the Reflector, as an unreadable one is.
                secrets=self.secrets,
                pin=self.snapshot().round_evidence(round_number),
                hits=hits,
            )
            if hits is None:
                self.adaptive_inputs_cache = (measurement_id, inputs)
            return inputs
        except (ReaderIntegrityError, BaselineUnavailable) as exc:
            raise _grouping_evidence_fault(exc, measurement_id) from exc

    def _history_source(
        self,
        snapshot: CampaignSnapshot,
        *,
        round_number: int | None = None,
        hits: HitSink | None = None,
    ) -> HistorySource:
        """Family 14 (#228): every closed round of this run, from the snapshot
        the reflection's brief is built from (ADR-0039 D2)."""

        pin = None if round_number is None else snapshot.round_evidence(round_number)
        mode = "enforce" if pin is None else pin.input_modes["history_excerpt_screen"]
        assert self.store is not None
        return build_history_source(
            snapshot,
            measurements=self.measurements,
            store=self.store,
            artifact_policy=self.artifact_policy,
            corpus=self.corpus,
            policy=self.policy,
            visible=self._role_visible(),
            # Addendum §4.11: the Candidate seal's needles and the run secrets.
            screen=excerpt_screen(
                self.needles,
                self.secrets,
                mode=mode,
                hits=hits,
            ),
            memo=self.history_memo if mode == "enforce" else None,
        )

    def _bound_reflection(
        self,
        source: HistorySource,
        reflection_id: str,
        brief: AdaptiveReflectBrief,
        inputs: FrozenReflectionInputs,
    ) -> tuple[AdaptiveReflectBrief, HistoryReader, bool]:
        """Bound the opening's history for the bound Reflector kind (merged D3,
        D4; addendum §4.7), write the private size record (merged D8) and build
        this reflection's history readers. `True` means the minimum first
        request is above R − H and the reflection is refused (merged D5)."""

        budget = request_budget_of(self.reflector)
        probe = (
            None
            if self.probe is None
            else ProbeAccess(
                PROBE_URL_PLACEHOLDER,
                self.probe.budget,
                self.probe.methods,
                self.probe.description,
                self.probe.string_body,
                health_unavailable=self.family_version == 15
                and self.probe.on_unavailable == "continue",
                mediator_failed=self.family_version == 15
                and self.probe.on_unavailable == "continue",
            )
        )
        editor_max = self._editor_file_budget()

        def measure(history: Mapping[str, Any]) -> Any:
            return measure_first_request(
                self.reflector,
                with_history(brief, history),
                editable_paths=self.artifact_policy.writable_paths,
                probe=probe,
                editor_max_changed_files=editor_max,
            )

        bound = bound_reflection_history(source, budget=budget, measure=measure)
        with _file_boundary("reflector_budget_unavailable"):
            write_budget_record(self.run_root, reflection_id, budget_record(bound, budget=budget))
        reader = HistoryReader(source, reflection_id=reflection_id, cache=inputs.body_cache)
        return with_history(brief, bound.history), reader, bound.decision == "refused"

    @staticmethod
    def _empty_reads() -> dict[str, Any]:
        return {"entries": [], "omitted_reads": 0, "omitted_errors": 0}

    def _reader_reads(self, reader: Any) -> dict[str, Any]:
        if reader is None:
            return self._empty_reads()
        summary = reader.summary
        if hasattr(summary, "to_payload"):
            return summary.to_payload()
        return {
            "entries": [dict(entry) for entry in summary.entries],
            "omitted_reads": summary.omitted_reads,
            "omitted_errors": summary.omitted_errors,
        }

    def _round_grouping(self, round_number: int, baseline_binding: str):
        """The stored grouping a round resolves, as `adaptive_grouping_for` returns it.

        b16 §11.1: a bound round (`bound_grouping`) keeps the grouping that
        produced it, whatever identity is current; only an unbound round looks
        for a reusable one under the current policy digest.
        """

        snapshot = self.snapshot()
        bound = bound_grouping(snapshot, round_number)
        if bound is not None:
            return bound
        return adaptive_grouping_for(
            snapshot,
            round_number,
            baseline_binding=baseline_binding,
            policy_digest=self.adaptive.policy_digest,
        )

    def _round_policy_digest(self, round_number: int) -> str:
        """The policy digest a round's portion and visitation use (b16 §11.1):
        its bound grouping's, else the current identity's."""

        bound = bound_grouping(self.snapshot(), round_number)
        return self.adaptive.policy_digest if bound is None else str(bound["policy_digest"])

    def _admitted_grouping(self, round_number: int):
        measurement_id = str(self.snapshot().rounds[round_number].baseline_measurement_id)
        try:
            inputs = self.adaptive_inputs_for(round_number)
            grouping = self._round_grouping(round_number, inputs.registry.baseline_binding)
            if grouping is None:
                return None
            result = grouping["result"]
            try:
                # b16 §11.1: the grouping's own policy digest and the Grouper
                # kind recorded when it started, so a bound round keeps its
                # group and cohort ids across a Grouper rebase.
                return admit_grouping_result(
                    inputs.registry,
                    GroupingResult(
                        result["registry_digest"],
                        tuple(result["assignments"]),
                        result["descriptions"],
                        result["variants"],
                        result["version"],
                    ),
                    policy_identity=str(grouping["policy_digest"]),
                    unavailable_unassigned=_unassigns_unavailable(grouping["grouper_kind"]),
                )
            except GroupingAdmissionError:
                raise
            except ValueError as exc:
                # Only `_require_text` on the policy identity makes
                # `admit_grouping_result` raise a plain ValueError, classified
                # here; one from the rest of the outer body (the inputs, the
                # journal lookup) propagates unclassified.
                raise _grouping_evidence_fault(exc, measurement_id) from exc
        except (GroupingAdmissionError, ReaderIntegrityError, BaselineUnavailable) as exc:
            raise _grouping_evidence_fault(exc, measurement_id) from exc

    def _grouping_checks(
        self, witness: NeedleWitness, hits: HitSink | None = None
    ) -> tuple[Callable[[str], bool], Callable[[Any], bool]]:
        def contains_secret(value: Any) -> bool:
            return secret_location({"payload": value}, self.secrets) is not None

        return (
            check_predicate(
                "grouping_oracle_leak",
                "oracle_leak",
                witness,
                modes=self.checks,
                hits=hits,
                leak=lambda: self.leak_field(witness.matched),
            ),
            check_predicate(
                "grouping_run_secret",
                "secret_detected",
                contains_secret,
                modes=self.checks,
                hits=hits,
            ),
        )

    async def _model_grouping(
        self,
        runtime: GroupingRuntime,
        inputs: Any,
        operation: str,
        attempts: AttemptRecorder | None = None,
        *,
        witness: NeedleWitness | None = None,
        hits: HitSink | None = None,
        reader: Any = None,
        checkpoints: _GroupingCheckpoints | None = None,
        adopted: Adopted | None = None,
        provenance: Mapping[str, tuple[str, ...] | None] | None = None,
    ) -> ModelGroupingOutcome:
        """Run the grouping model once, interrupting an in-flight call on cancel.

        The runtime checks `cancelled` only between requests, so the stop signal
        is also polled here and cancels the task mid-call (spec §7.2).
        ``provenance`` is family 15's stored `used` union by evidence ID
        (`_grouping_provenance`); family 14 passes none.
        """

        credentials = self.adaptive.model_credentials
        assert credentials is not None  # `_admit_active_runtime`

        def stopping() -> bool:
            return bool(self.cancelled() or self.stopping)

        needles = self._reflection_needles_for(inputs)
        oracle_check, secret_check = self._grouping_checks(
            witness if witness is not None else NeedleWitness(needles), hits
        )
        bindings: dict[str, Any] = {}
        if self.family_version == 15:
            # `model_grouping/2` (b16 spec §3, §9): the checkpoint bound of a
            # family-15 record, and the run-secret redaction the other role
            # inputs apply, for the component IDs a case header shows.
            from evolution.tree.family15_schema import MAX_RECORD_BYTES as RECORD_BYTES15
            from evolution.tree.roles.grouping.grouping_portions import (
                CHECKPOINT_ENVELOPE_BYTES,
            )
            from evolution.tree.roles.input_secrets import input_secret_value

            secrets, pin = self.secrets, getattr(inputs, "pin", None)

            def redact(value: str) -> str:
                # One location per component ID: the headers, the cards and
                # the reported set each redact it, and a shadowed
                # `input_run_secret_redaction` counts one hit per component,
                # as the other role inputs count one per source. The digest
                # keeps the value out of the location.
                location = f"component/{hashlib.sha256(value.encode()).hexdigest()}"
                return str(
                    input_secret_value(value, secrets, pin=pin, hits=hits, location=location)
                )

            bindings = {
                "record_budget": RECORD_BYTES15 - CHECKPOINT_ENVELOPE_BYTES,
                "redact": redact,
                # PDR-0044: a checkpointed decision is used, sized and kept in
                # the form the journal keeps (`_GroupingCheckpoints._write`
                # applies `journal_value`, the same redaction and its check).
                "normalize": lambda value: redact_for_journal(value, secrets),
                "checkpoints": checkpoints,
                "adopted": adopted,
                "provenance": provenance,
            }
        task = asyncio.create_task(
            runtime.model(
                inputs.registry,
                operation=operation,
                credentials=credentials,
                log_root=self.run_root / "reflector-logs",
                secrets=self.secrets,
                cancelled=stopping,
                forbidden_secret=secret_check,
                oracle_needle=oracle_check,
                metric_primary=self.policy.metric_primary,
                client_factory=self.adaptive.model_client_factory,
                family14=True,
                reader=reader,
                # `_chat_role_call` converts every other exception; the
                # runtime names its class in the private log.
                routed_faults=_GROUPING_ROUTED_FAULTS,
                attempts=attempts,
                family15=self.family_version == 15,
                **bindings,
            )
        )
        return await self._await_grouping(task)

    async def _agent_grouping(
        self,
        runtime: GroupingRuntime,
        inputs: Any,
        operation: str,
        reader: Any,
        control: Path,
        *,
        witness: NeedleWitness | None = None,
        hits: HitSink | None = None,
    ) -> ModelGroupingOutcome:
        """Poll an agent role call for stops, including its last failing call."""

        def stopping() -> bool:
            return bool(self.cancelled() or self.stopping)

        needles = self._reflection_needles_for(inputs)
        oracle_check, secret_check = self._grouping_checks(
            witness if witness is not None else NeedleWitness(needles), hits
        )
        task = asyncio.create_task(
            runtime.agent(
                inputs.registry,
                family15=self.family_version == 15,
                reader=reader,
                control_root=control,
                operation=operation,
                log_root=self.run_root / "reflector-logs",
                secrets=self.secrets,
                parent_digest=self.snapshot().champion[1],
                verify_admission=inputs.verify_admission,
                cancelled=stopping,
                forbidden_secret=secret_check,
                oracle_needle=oracle_check,
            )
        )
        return await self._await_grouping(task)

    async def _await_grouping(
        self, task: asyncio.Task[ModelGroupingOutcome]
    ) -> ModelGroupingOutcome:
        """Keep the grouping stop poll and task lifetime identical across model kinds."""
        try:
            while not task.done():
                await asyncio.wait((task,), timeout=0.025)
                if not task.done() and (self.cancelled() or self.stopping):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise GroupingExecutionError("cancelled")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _chat_role_call(
        self,
        runtime: GroupingRuntime,
        inputs: Any,
        operation: str,
        attempts: AttemptRecorder | None = None,
        *,
        witness: NeedleWitness | None = None,
        hits: HitSink | None = None,
        reader: Any = None,
        checkpoints: _GroupingCheckpoints | None = None,
        adopted: Adopted | None = None,
        provenance: Mapping[str, tuple[str, ...] | None] | None = None,
    ) -> ModelGroupingOutcome:
        """`_model_grouping`, with family 14's `role_failed / exception` (F0 R47).

        ADR-0034's amendment: `exception` covers only an unexpected exception
        of the `chat` role call; every fault the existing code already routes
        keeps its route (plan decision 8, ruling R6). So these pass through
        unchanged: a `GrouperRoleFailure` (its own reason); `ValueError`,
        `TypeError` and `KeyError` (`malformed_result`), which include
        `GroupingExecutionError` (cancellation, a changed seal, a secret, a
        missing provider SDK), `ReaderIntegrityError` and the run-directory
        hygiene refusal `EvolveRefused`; `CampaignInvariantViolated`, a record
        the lifecycle refused (a checkpoint or the terminal), which
        `run_grouping` lets propagate as an executor fault instead of
        journaling it `malformed_result`, so the grouping stays open and the
        resume settles it `interrupted`; the campaign's own persistence faults
        (`_Fault`, `JournalBoundaryError`);
        and `WorkspaceError` and `OSError`, which propagate as before. Those two
        are the run directory's: opening, writing or closing the Grouper's
        attempt log, its private directory, the prompt read (already
        `command_changed`). The role call's own `OSError`s are separated at
        their source in family 14: the retry layer counts one a provider
        request raises as a provider failure (`role_call._provider_exceptions`),
        and `run_model_grouping` raises `GrouperRoleFailure("exception",
        "unknown")` for one from building or closing the provider client. A
        `BaseException` that is not an `Exception` (cancellation) is never
        caught. Families 9-13 are unchanged: there everything passes through.
        """

        hit_options: dict[str, Any] = {"hits": hits} if hits is not None else {}
        try:
            return await self._model_grouping(
                runtime,
                inputs,
                operation,
                attempts,
                witness=witness,
                reader=reader,
                checkpoints=checkpoints,
                adopted=adopted,
                provenance=provenance,
                **hit_options,
            )
        except _GROUPING_ROUTED_FAULTS:
            raise
        except Exception as exc:
            raise GrouperRoleFailure("exception", "unknown") from exc

    def _grouping_adoption(self, baseline_binding: str, policy_digest: str) -> Adopted:
        """Every checkpoint a new grouping on this baseline and policy may adopt (b16 §9).

        The groupings of the same ``(baseline_binding, policy_digest)`` that an
        interruption or a provider suspension ended, newest first, back to the
        first other terminal: a resumed grouping that was interrupted again
        records only what it asked itself, so the checkpoints it adopted stay
        with the grouping that wrote them. A newer checkpoint of one portion or
        batch replaces an older one. Nothing is filtered here: the runtime
        replays only what names its plan and its inputs now (C6). The
        lifecycle keeps the checkpoints, so this reads no journal.
        """

        from evolution.tree.roles.grouping.consolidation import batch_from_body
        from evolution.tree.roles.grouping.model_grouping_v2 import (
            Adopted,
            AdoptedBatch,
            AdoptedPortion,
        )
        from evolution.tree.roles.grouping.portion_session import PortionOutcome

        family15 = self.snapshot().family15
        assert family15 is not None
        chain = []
        for grouping in reversed(tuple(family15.groupings.values())):
            if (grouping.baseline_binding, grouping.policy_digest) != (
                baseline_binding,
                policy_digest,
            ):
                continue
            if not _resumable_grouping(grouping):
                break
            chain.append(grouping)
        portions: dict[str, AdoptedPortion] = {}
        batches: dict[tuple[int, str], AdoptedBatch] = {}
        for grouping in reversed(chain):
            for portion_id, record in grouping.portions.items():
                p = record.payload
                portions[portion_id] = AdoptedPortion(
                    p["plan_digest"],
                    p["input_digest"],
                    # A checkpoint keeps no repair count: `calls` counts this
                    # grouping's own repairs only.
                    PortionOutcome(
                        portion_id, tuple(p.get("groups", ())), p.get("outcome"), p["requests"], 0
                    ),
                )
            for key, record in grouping.batches.items():
                p = record.payload
                batches[key] = AdoptedBatch(
                    p["plan_digest"], batch_from_body({**p, "batch_id": p["index"]})
                )
        return Adopted(portions, batches)

    def _grouping_provenance(self, round_number: int) -> dict[str, tuple[str, ...] | None]:
        """The round baseline's stored `used` union of every observation, by evidence ID.

        b16 §5.3 (Builder decision 5): `baseline_provenance` keeps every
        sample's latest try, by case in schedule order, whatever
        `gate.provenance.kind` is; the schedule names each sample's repeat,
        and the observation is `obs:<case>:<repeat>` as in the registry. A
        union is `None` when the target reported none. Only the Grouper's
        case headers and cards count these; no role input carries them.
        """

        from evolution.tree.affected_cases import baseline_provenance

        measurement_id = str(self.snapshot().rounds[round_number].baseline_measurement_id)
        records = self.measurements[measurement_id].records()
        if not records:
            return {}
        repeats: dict[str, list[int]] = {}
        for entry in records[0].payload["identity"]["schedule"]:
            repeats.setdefault(str(entry["case_id"]), []).append(int(entry["repeat"]))
        return {
            _observation_evidence_id(case_id, repeat): sample.used
            for case_id, samples in baseline_provenance(records).items()
            for repeat, sample in zip(repeats[case_id], samples, strict=True)
        }

    async def run_grouping(self, round_number: int) -> None:
        """Run the configured pure grouping once, then persist its compact result."""

        inputs = self.adaptive_inputs_for(round_number)
        # No failing observations never start a policy operation.
        if not any(record.failing for record in inputs.registry.records):
            await self.select_focus_portion(round_number)
            return
        if self._admitted_grouping(round_number) is not None:
            await self.select_focus_portion(round_number)
            return
        operation = uuid4().hex
        grouping_id = f"r{round_number:02d}-g-{operation}"
        role_started = False

        def log_hit(value: dict[str, Any]) -> None:
            if not role_started:
                return
            with _boundary("artifact_store"):
                with open_attempt_log(
                    self.run_root / "reflector-logs",
                    f"grouping-{operation}",
                    secrets=self.secrets,
                    admitted=self.adaptive.admitted_hygiene,
                ) as log:
                    log.record(value)

        hits = HitSink(log=log_hit)
        hit_options: dict[str, Any] = {"hits": hits} if self.family_version == 15 else {}
        if self.family_version == 15:
            inputs = self.adaptive_inputs_for(round_number, hits=hits)
        started: dict[str, Any] = {
            "round": round_number,
            "grouping_id": grouping_id,
            "baseline_binding": inputs.registry.baseline_binding,
            "policy_digest": self.adaptive.policy_digest,
            "registry_digest": inputs.registry.digest,
        }
        started["grouper_kind"] = self.adaptive.identity.to_payload()["grouping"]["kind"]
        # b16 §9: a family-15 chat Grouper checkpoints its decisions and adopts
        # those of the groupings an interruption or a provider suspension left
        # on this baseline and policy, looked up before this one starts.
        checkpoint_options: dict[str, Any] = {}
        if self.family_version == 15 and self.adaptive.model_grouping is not None:
            checkpoint_options = {
                "checkpoints": _GroupingCheckpoints(self, grouping_id),
                "adopted": self._grouping_adoption(
                    started["baseline_binding"], started["policy_digest"]
                ),
                # b16 §5.3: the reported provenance the case headers count.
                "provenance": self._grouping_provenance(round_number),
            }
        self.append("grouping_started", started, operation)
        self.hook("after_grouping_started")
        role_started = True
        reader = None
        reads = self._empty_reads()
        # Family 9's `grouping_finished.fallback`/`.calls`: `null` unless the
        # policy is `model` (spec §10).
        fallback: dict[str, Any] | None = None
        calls: dict[str, Any] | None = None
        # The `command` and `fields` scans below; `_model_grouping` takes the
        # same set for `chat` (family 14 narrows it per baseline, R51).
        needles = self._reflection_needles_for(inputs)
        witness = NeedleWitness(needles)
        oracle_check, secret_check = self._grouping_checks(witness, hits)
        try:
            runtime = GroupingRuntime(self.adaptive)
            if self.adaptive.agent_grouping is not None:
                grouping_root = ensure_private_directory(self.run_root / ".grouping")
                control = Path(tempfile.mkdtemp(prefix="op-", dir=grouping_root))
                try:
                    reader = inputs.reader_view().open_operation(
                        f"group-{operation}",
                        {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"},
                    )
                    outcome = await self._agent_grouping(
                        runtime, inputs, operation, reader, control, witness=witness, **hit_options
                    )
                    grouped = outcome.grouped
                    fallback = None if outcome.fallback is None else asdict(outcome.fallback)
                    calls = dict(outcome.calls)
                    reader.verify_admission(inputs.verify_admission)
                    reads = self._reader_reads(reader)
                finally:
                    try:
                        if reader is not None:
                            reader.close()
                    finally:
                        discard_directory(control)
            elif self.adaptive.model_grouping is not None:
                chat_methods = {
                    "list_grouping_inputs",
                    "read_grouping_input",
                    "stream_grouping_inputs",
                }
                if inputs.traces is not None:
                    chat_methods.add("read_trace")
                reader = inputs.reader_view().open_operation(f"group-{operation}", chat_methods)
                # #290 §4.4: the operation's provider attempts, told once
                # whatever ends it -- a result, a role failure, an exception.
                attempts = AttemptRecorder()
                try:
                    try:
                        outcome = await self._chat_role_call(
                            runtime,
                            inputs,
                            operation,
                            attempts,
                            witness=witness,
                            reader=reader,
                            **checkpoint_options,
                            **hit_options,
                        )
                    finally:
                        credentials = self.adaptive.model_credentials
                        self.note_provider_attempts(
                            "grouper",
                            f"r{round_number:02d}",
                            attempts.report(
                                unauthenticated=credentials is not None
                                and credentials.api_key is None
                            ),
                        )
                    grouped = outcome.grouped
                    fallback = None if outcome.fallback is None else asdict(outcome.fallback)
                    calls = dict(outcome.calls)
                    # The model read the registry's lazy sources while it ran;
                    # the admission scan closes that interval before the result
                    # can become durable, as it does for a command policy.
                    reader.verify_admission(inputs.verify_admission)
                    reads = self._reader_reads(reader)
                finally:
                    reader.close()
            elif self.adaptive.command_grouping:
                grouping_root = ensure_private_directory(self.run_root / ".grouping")
                control = Path(tempfile.mkdtemp(prefix="op-", dir=grouping_root))
                reader = inputs.reader_view().open_operation(
                    f"group-{operation}",
                    {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"},
                )
                try:
                    grouped = await runtime.command(
                        inputs.registry,
                        reader=reader,
                        control_root=control,
                        log_root=self.run_root / "reflector-logs",
                        parent_digest=self.snapshot().champion[1],
                        cancelled=lambda: self.cancelled() or self.stopping,
                        forbidden=combine_checks(oracle_check, secret_check),
                        scorer_diagnostics=inputs.scorer_diagnostics,
                    )
                    reader.verify_admission(inputs.verify_admission)
                    reads = self._reader_reads(reader)
                finally:
                    reader.close()
            else:
                reader = inputs.reader_view().open_operation(
                    f"group-{operation}",
                    {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"},
                )

                def admit_builtin() -> None:
                    nonlocal grouped
                    grouped = runtime.builtin(
                        inputs.registry,
                        family15=self.family_version == 15,
                        forbidden_string_predicates=(
                            (
                                "oracle_leak",
                                oracle_check,
                            ),
                        )
                        + (
                            (("secret_detected", secret_check),)
                            if self.family_version == 15
                            else ()
                        ),
                    )
                    inputs.verify_admission()

                try:
                    reader.verify_admission(admit_builtin)
                    reads = self._reader_reads(reader)
                finally:
                    reader.close()
            if self.cancelled() or self.stopping:
                raise GroupingExecutionError("cancelled")
            # The admitted result keys its sparse maps by group number and
            # record index; the journal's JSON keys are their decimal strings
            # (`_validate_grouping_result`), which re-admission reads back
            # (`_mapping_key`). Every family: families 6-8 could never journal
            # a non-empty map (#58), and an empty one keeps its bytes.
            result = {
                "version": grouped.result.version,
                "registry_digest": grouped.result.registry_digest,
                "assignments": list(grouped.result.assignments),
                "descriptions": {
                    str(key): value for key, value in grouped.result.descriptions.items()
                },
                "variants": {str(key): value for key, value in grouped.result.variants.items()},
            }
            if scan_grouping_value(
                secret_check, result, tuple(row.evidence_id for row in inputs.registry.records)
            ):
                raise GroupingExecutionError("secret_detected")
            if (
                fallback is not None
                and fallback["scope"] == "all"
                and self.snapshot().grouper_chain
                >= grouper_chain_limit(self.adaptive.identity.to_payload()) - 1
            ):
                # Family 14 (ADR-0034, F0 R50, plan decision 10): the second
                # consecutive full fallback is the Grouper's content failure.
                # It is decided before any append, so its fallback result is
                # never journaled or applied: the one terminal is
                # `grouping_failed(role_failed)`, then `grouper_failing`. It
                # comes after every integrity check of the result (admission
                # scan, cancellation, secret scan), which keep their own
                # routes (ADR-0034, ADR-0022 amendments). A scope-`all` result
                # never trips the secret scan in practice: it ignores the
                # model's groups, so its only strings are `version` and
                # `registry_digest`, which this run's journal already wrote
                # (and checked for secrets) in the first link's
                # `grouping_finished` and this grouping's `grouping_started`.
                raise GrouperRoleFailure(str(fallback["reason"]), "content")
            if self.family_version == 15:
                result = self.journal_value(result, self.secrets)
                admit_grouping_result(
                    inputs.registry,
                    GroupingResult(
                        result["registry_digest"],
                        tuple(result["assignments"]),
                        result["descriptions"],
                        result["variants"],
                        result["version"],
                    ),
                    policy_identity=started["policy_digest"],
                    unavailable_unassigned=_unassigns_unavailable(started["grouper_kind"]),
                )
            self.append(
                "grouping_finished",
                {
                    "round": round_number,
                    "grouping_id": grouping_id,
                    "result": result,
                    "result_digest": hashlib.sha256(canonical_json(result)).hexdigest(),
                    "reads": reads,
                    **({"fallback": fallback, "calls": calls}),
                },
                operation,
                **hits.terminal_fields(),
            )
            self.hook("after_grouping_finished")
        except GrouperRoleFailure as exc:
            if self.cancelled() or self.stopping:
                # A§5.1: cancellation during grouping stops the campaign. A
                # stop that arrives while the role call's last failing attempt
                # settles is still a stop, as the success path's check above
                # and family 13's fallback make it, never a role failure.
                self.append(
                    "grouping_failed",
                    {
                        "round": round_number,
                        "grouping_id": grouping_id,
                        "code": "interrupted",
                        "reads": self._reader_reads(reader),
                    },
                    operation,
                )
                self.stop(_Pending("stopped", "cancelled", "none"))
                return
            # Family 14 (ADR-0034, F0 R46, R47): a `chat` Grouper's provider or
            # unknown failure is a role failure: its record, then the
            # suspension of its class, with no round closed (plan decision 9).
            # A provider or unknown failure leaves the role call before
            # `reader.verify_admission` runs, so this route skips the
            # admission scan: a provider failure that coincides with damaged
            # evidence is written `role_failed`, not `reader_integrity`
            # (a low-probability case). The second
            # link's content failure is raised after the scan.
            self.append(
                "grouping_failed",
                {
                    "round": round_number,
                    "grouping_id": grouping_id,
                    "code": "role_failed",
                    "reason": exc.reason,
                    "error_class": exc.error_class,
                    "reads": self._reader_reads(reader),
                },
                operation,
                **hits.terminal_fields(),
            )
            self.hook("after_grouping_failed")
            self.stop(
                _Pending("infrastructure_failed", grouper_suspension_for(exc.error_class), "none")
            )
        except (GroupingExecutionError, ReaderIntegrityError) as exc:
            reads = self._reader_reads(reader)
            if isinstance(exc, GroupingExecutionError) and exc.code == "cancelled":
                self.append(
                    "grouping_failed",
                    {
                        "round": round_number,
                        "grouping_id": grouping_id,
                        "code": "interrupted",
                        "reads": reads,
                    },
                    operation,
                )
                self.stop(_Pending("stopped", "cancelled", "none"))
                return
            failure_codes = {
                "timeout": "command_timeout",
                "command_changed": "changed_seal",
                "command_failed": "command_failed",
                "helper_unavailable": "control_collision",
                "descriptor_too_large": "descriptor_too_large",
                "invalid_result": "malformed_result",
                "result_too_large": "output_too_large",
            }
            code = (
                "reader_integrity"
                if isinstance(exc, ReaderIntegrityError)
                else failure_codes.get(
                    exc.code,
                    exc.code
                    if exc.code in {"secret_detected", "oracle_leak"}
                    else "malformed_result",
                )
            )
            # A command's combined predicate used to conflate oracle and credential hits.
            # Attribute /16 oracle refusals from the in-memory witness; /15 keeps its code.
            if (
                self.family_version == 15
                and self.checks.mode("grouping_oracle_leak") == "enforce"
                and code == "secret_detected"
                and witness.matched is not None
            ):
                code = "oracle_leak"
            leak = self.leak_field(witness.matched) if code == "oracle_leak" else None
            self.append(
                "grouping_failed",
                {
                    "round": round_number,
                    "grouping_id": grouping_id,
                    "code": code,
                    "reads": reads,
                    **({"leak": leak} if leak is not None else {}),
                },
                operation,
                **hits.terminal_fields(),
            )
            self.hook("after_grouping_failed")
            if isinstance(exc, ReaderIntegrityError) and exc.code == "diagnostic_bundle_changed":
                with _boundary("artifact_store"):
                    raise ArtifactCorruptionError("artifact_digest_mismatch")
            self.stop(
                _Pending(
                    "infrastructure_failed",
                    "grouping_failed",
                    "none",
                    evidence=self.grouping_failure_evidence(),
                )
            )
        except CampaignInvariantViolated:
            # A record the lifecycle refuses -- a checkpoint (b16 §9) or the
            # terminal itself -- is an executor bug, not the Grouper's output:
            # it propagates unchanged, the grouping stays open, and the resume
            # settles it `interrupted`, so its written checkpoints stay
            # adoptable.
            raise
        except (ValueError, TypeError, KeyError):
            self.append(
                "grouping_failed",
                {
                    "round": round_number,
                    "grouping_id": grouping_id,
                    "code": "malformed_result",
                    "reads": self._reader_reads(reader),
                },
                operation,
                **hits.terminal_fields(),
            )
            self.hook("after_grouping_failed")
            self.stop(
                _Pending(
                    "infrastructure_failed",
                    "grouping_failed",
                    "none",
                    evidence=self.grouping_failure_evidence(),
                )
            )

    def focus_cohorts(self, round_number: int, grouped: Any) -> tuple[GroupingCohort, ...]:
        """The round's cohorts for focus selection, without the excluded pairs.

        Continuable-terminals spec §4.3 (final branch review, human decision
        2026-09-17): a pair the baseline could not measure is gone from every
        later look, so a cohort built over it would send the round's focus to a
        case no Attempt can compare -- every Attempt would close
        `no_evaluable_focus`. The pairs leave the cohorts here, after the
        grouping and before the selection, so the registry, the admitted
        grouping and the Reflector's own evidence still carry those samples as
        unavailable. The set is recomputed from the baseline measurement's own
        durable rows (`excluded_pairs`), so a resumed session selects the same
        portion, and it is empty below family 6 -- families 3 to 5 are unchanged.

        When the filter empties every cohort -- only unmeasurable samples
        failed -- nothing comes back. The round then opens no Attempt and
        closes `unmeasurable` (`select_focus_portion`, `decide_round`), the way
        a clean round does, instead of restoring the very pairs the filter
        removed and spending every Attempt on a case no look can compare
        (ADR-0016, PDR-0011, codex branch review round 1).
        """

        state = self.snapshot().rounds[round_number]
        excluded = self.excluded_pairs(str(state.baseline_measurement_id), self.policy.repeats)
        return cohorts_from_grouping(grouped, excluded=excluded)

    def adaptive_portion(self, round_number: int, grouped: Any):
        inputs = self.adaptive_inputs_for(round_number)
        snapshot = self.snapshot()
        # Family 13 (#148, ADR-0012 amendment of 2026-10-06): visitation walks
        # the chain back to the last Promotion or `rebaseline`, across
        # `retained` refreshes, and carries the cases visited on earlier
        # bindings; families 4 to 12 keep the per-binding loop.
        visited, carried, cycle = visitation_in_cycle(
            snapshot.rounds,
            round_number,
            binding=inputs.registry.baseline_binding,
            # b16 §11.1: a bound round visits under its grouping's digest.
            policy_digest=self._round_policy_digest(round_number),
            chain_start=(visitation_chain_start(snapshot, round_number)),
        )
        cohorts = self.focus_cohorts(round_number, grouped)
        visited = visited | carried_cohort_ids(cohorts, carried)
        selection = select_portion(
            cohorts,
            registry=inputs.registry,
            visited_in_cycle=frozenset(visited),
            focus_cases=self.adaptive.selection.focus_cases,
            representatives_per_case=self.adaptive.selection.representatives_per_case,
            variants=grouped.variants,
            # Spec §9: family 9 breaks a distinct-case tie by failing
            # observations before the content key; earlier families unchanged.
            **({"rank_observations": True}),
        )
        return selection, cycle + 1 if selection.cycle_reset else cycle

    async def select_focus_portion(self, round_number: int) -> None:
        inputs = self.adaptive_inputs_for(round_number)
        state = self.snapshot().rounds[round_number]
        available = sum(record.available for record in inputs.registry.records)
        failing = sum(record.failing for record in inputs.registry.records)
        unavailable = len(inputs.registry.records) - available
        common = {
            "round": round_number,
            "baseline_prefix_digest": self.prefix(str(state.baseline_measurement_id))[
                "content_digest"
            ],
            "baseline_binding": inputs.registry.baseline_binding,
            "policy_digest": self.adaptive.policy_digest,
            "registry_digest": inputs.registry.digest,
            "available": available,
            "unavailable": unavailable,
            "failing": failing,
            "cycle": 1,
        }
        if failing == 0:
            self.append(
                "focus_portion_selected",
                {
                    **common,
                    "reason": "none",
                    "grouping_digest": None,
                    "neighborhood_id": None,
                    "cohort_ids": [],
                    "case_ids": [],
                    "evidence": [self.prefix(str(state.baseline_measurement_id))],
                },
            )
            return
        grouped = self._admitted_grouping(round_number)
        if grouped is None:
            return
        selection, selected_cycle = self.adaptive_portion(round_number, grouped)
        stored_grouping = self._round_grouping(round_number, inputs.registry.baseline_binding)
        assert stored_grouping is not None
        # b16 §11.1: the portion records the digest of the grouping that
        # produced it -- the round's own, after a Grouper rebase too.
        common["policy_digest"] = stored_grouping["policy_digest"]
        if not selection.cohorts:
            # Spec §4.3: something failed, but the excluded baseline pairs left
            # every failing cohort empty. The round has nothing an Attempt could
            # be judged on, so it records that state and closes on it.
            self.append(
                "focus_portion_selected",
                {
                    **common,
                    "reason": "unmeasurable",
                    "grouping_digest": stored_grouping["result_digest"],
                    "neighborhood_id": None,
                    "cohort_ids": [],
                    "case_ids": [],
                    "evidence": [self.prefix(str(state.baseline_measurement_id))],
                },
            )
            self.hook("after_focus_portion_selected")
            return
        neighborhood = next(
            entry
            for entry in grouped.neighborhoods
            if entry.group_id == selection.cohorts[0].group_id
        )
        self.append(
            "focus_portion_selected",
            {
                **common,
                "reason": "selected",
                "grouping_digest": stored_grouping["result_digest"],
                "neighborhood_id": neighborhood.group_id,
                "cohort_ids": list(selection.selected_cohort_ids),
                "case_ids": sorted({cohort.case_id for cohort in selection.cohorts}),
                "cycle": selected_cycle,
                "evidence": [self.prefix(str(state.baseline_measurement_id))],
            },
        )
        self.hook("after_focus_portion_selected")

    async def run_reflection(self, round_number: int) -> None:
        """One reflection of one round: start, call, terminal (spec §3.3, §9).

        The Reflector reads a frozen private copy of the Parent and never the
        published copy itself; the brief it is handed carries no oracle, no
        private case data and no target address. Its claimed groups are proved
        against the catalog it was shown -- and against the corpus's oracle
        needles -- before any of them becomes durable, and a refusal there is
        this reflection's own failure, not the campaign's.
        """

        assert self.store is not None
        state = self.snapshot().rounds[round_number]
        reflection_id = f"r{round_number:02d}-x{len(state.reflections) + 1:02d}"
        role_started = False

        def log_hit(value: dict[str, Any]) -> None:
            if not role_started:
                return
            with _boundary("artifact_store"):
                with open_attempt_log(
                    self.run_root / "reflector-logs",
                    reflection_id,
                    secrets=self.secrets,
                    admitted=getattr(self.reflector, "admitted_hygiene", ()),
                ) as log:
                    log.record(value)

        hits = HitSink(log=log_hit)
        adaptive_brief: AdaptiveReflectBrief | None = None
        brief: ReflectBrief | AdaptiveReflectBrief
        reader = None
        history_reader: HistoryReader | None = None
        refused = False
        # The note and diagnosis scan below. An adaptive Reflector's text is
        # built from one frozen baseline, whose published criterion ids family
        # 14 drops from the set (R51); `/15` always configures adaptation.
        needles = self.reflection_needles
        inputs = (
            self.adaptive_inputs_for(round_number, hits=hits)
            if self.family_version == 15
            else self.adaptive_inputs_for(round_number)
        )
        needles = self._reflection_needles_for(inputs)
        grouping = self._admitted_grouping(round_number)
        assert grouping is not None
        state_for_portion = self.snapshot().rounds[round_number]
        stored_portion = state_for_portion.focus_portion
        assert stored_portion is not None
        chosen = set(stored_portion["cohort_ids"])
        selected, selected_cycle = self.adaptive_portion(round_number, grouping)
        if set(selected.selected_cohort_ids) != chosen or selected_cycle != stored_portion["cycle"]:
            raise CampaignInvariantViolated("adaptive focus portion is not reproducible")
        if list(stored_portion["case_ids"]) != sorted(
            {cohort.case_id for cohort in selected.cohorts}
        ):
            raise CampaignInvariantViolated("adaptive focus portion is not reproducible")
        snapshot = self.snapshot()
        history = project_history(
            snapshot,
            measurements=self.measurements,
            store=self.store,
            artifact_policy=self.artifact_policy,
            corpus=self.corpus,
            policy=self.policy,
            limits={**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1},
            excerpts=True,
            visible=self._role_visible(),
        )
        source = (
            self._history_source(snapshot, round_number=round_number, hits=hits)
            if self.family_version == 15
            else self._history_source(snapshot)
        )
        # #282 (R3, design C6): the round's limit, fixed when the round opened,
        # never `self.adaptive`'s current one, so a reflection retried after a
        # mid-round rebase keeps its brief, tool schema and input digest.
        group_text_chars = snapshot.round_group_text_chars(round_number)
        adaptive_brief = build_adaptive_reflect_brief(
            champion_digest=snapshot.champion[1],
            operator_context=self.policy.reflector_operator_context,
            history=history,
            editable_paths=self.artifact_policy.writable_paths,
            grouping=grouping,
            selection=selected,
            inputs=inputs,
            cycle=int(stored_portion["cycle"]),
            focus_cases=self.adaptive.selection.focus_cases,
            compact_history=None
            if source is None
            else render_history(source, initial_layout(len(source.rounds))),
            group_text_chars=group_text_chars,
        )
        if source is not None:
            adaptive_brief, history_reader, refused = self._bound_reflection(
                source, reflection_id, adaptive_brief, inputs
            )
        brief = adaptive_brief
        with _boundary("artifact_store"):
            parent = self.store.published_ref("champion", self.snapshot().champion[0])
        # Family 14 (F0 R64/R67, routine choices 1 and 2): the Reflector reads
        # the same sparse projection as the round's Editors. `D` comes from the
        # verified published Parent before `reflection_started`, enters its
        # digest object, and `project` re-derives it from its own capture.
        view = self.role_view
        role_digest = None if view is None else self._role_projection_digest(parent)
        docs = self.environment_docs

        def started_digest() -> str:
            return hashlib.sha256(
                canonical_json(
                    {
                        "evidence": reflect_brief_payload(brief),
                        "environment_docs": None if docs is None else docs.digest,
                        **(
                            {"probe": probe_identity_payload(self.probe)}
                            if self.probe is not None
                            else {}
                        ),
                        **({"role_projection": role_digest} if role_digest is not None else {}),
                    }
                )
            ).hexdigest()

        continuing = (
            self.family_version == 15
            and self.probe is not None
            and self.probe.on_unavailable == "continue"
        )
        ready = True
        mediator_failed = False
        session: ProbeSession | None = None
        mediator: ProbeMediator | None = None
        access: ProbeAccess | None = None
        runtime_secrets = self.secrets
        projection = None
        frozen_view: dict[str, FileState] | None = None
        projection_changed = False
        collision: ControlCollision | None = None
        quiescent = True
        integrity_failed = False
        role_returned = False
        if refused:
            # #228 (ADR-0039 D5): the minimum first request exceeds R − H. Refuse
            # before any probe health check, mediator or provider call; the class
            # is `unknown`, so `fail_reflection` suspends `reflector_failing`.
            operation = uuid4().hex
            self.append(
                "reflection_started",
                dict(
                    round=round_number,
                    reflection_id=reflection_id,
                    operation_id=operation,
                    input_digest=started_digest(),
                ),
                operation,
            )
            self.hook("after_reflection_started")
            self.fail_reflection(round_number, reflection_id, operation, "input_too_large", None)
            return
        try:
            if self.probe is not None:
                try:
                    ready = await check_probe_health(
                        self.probe, cancelled=lambda: self.cancelled() or self.stopping
                    )
                except asyncio.CancelledError:
                    if self.cancelled() or self.stopping:
                        return
                    raise
                if self.cancelled() or self.stopping:
                    return
                checked = self.append(
                    "probe_checked",
                    dict(
                        round=round_number,
                        reflection_id=reflection_id,
                        status="ready" if ready else "unavailable",
                    ),
                )
                self.hook("after_probe_checked")
                if not ready and not continuing:
                    return
                capability = uuid4().hex
                runtime_secrets = (*self.secrets, capability)
                mediator = ProbeMediator(capability=capability)
                self.active_probe_mediator = mediator
                try:
                    await mediator.start()
                except OSError:
                    with _file_boundary("probe_diagnostic_unavailable"):
                        ensure_private_directory(self.run_root / "reflector-logs")
                        write_probe_diagnostic(
                            self.run_root
                            / "reflector-logs"
                            / "probes"
                            / reflection_id
                            / "startup.json",
                            {
                                "error": "probe_mediator_startup_failed",
                                "detail": "probe listener could not start",
                            },
                        )
                    mediator_failed = True
                    if not continuing:
                        self.stop(
                            _Pending(
                                "infrastructure_failed",
                                "infrastructure_failed",
                                "none",
                                evidence=(
                                    self.record_reference(checked.record.to_payload()["sequence"]),
                                ),
                                # Family 7 (fix round 1): the ready check this
                                # setup failed after; a resume retries the setup.
                                suspend_as="probe_unavailable",
                                record=(
                                    checked.record.to_payload()["sequence"],
                                    checked.operation_id,
                                ),
                            )
                        )
                        return
                if continuing:
                    self._probe_startups[reflection_id] = "failed" if mediator_failed else "started"
            operation = uuid4().hex
            self.append(
                "reflection_started",
                dict(
                    round=round_number,
                    reflection_id=reflection_id,
                    operation_id=operation,
                    input_digest=started_digest(),
                ),
                operation,
            )
            deadline_at = (
                time.monotonic() + self.policy.reflection_timeout_seconds
                if self.probe is not None
                else None
            )
            self.hook("after_reflection_started")
            if self.probe is not None:
                assert deadline_at is not None
                with _file_boundary("probe_diagnostic_unavailable"):
                    diagnostic_root = ensure_private_directory(self.run_root / "reflector-logs")

                def diagnostic_writer(path: Path, payload: Mapping[str, object]) -> None:
                    with _file_boundary("probe_diagnostic_unavailable"):
                        write_probe_diagnostic(path, payload)

                def append_probe(payload: dict[str, object]) -> None:
                    self.append("probe_completed", dict(payload))

                session = ProbeSession(
                    self.probe,
                    round=round_number,
                    reflection_id=reflection_id,
                    diagnostic_root=diagnostic_root,
                    deadline_at=deadline_at,
                    append=append_probe,
                    secrets=runtime_secrets,
                    diagnostic_writer=diagnostic_writer,
                    on_unavailable="continue" if continuing else "suspend",
                    disabled=mediator_failed,
                )
                self.active_probe_session = session
                assert mediator is not None
                if not mediator_failed:
                    mediator.bind_session(session)
                access = ProbeAccess(
                    None if mediator_failed else mediator.url,
                    self.probe.budget,
                    self.probe.methods,
                    self.probe.description,
                    self.probe.string_body,
                    health_unavailable=not ready,
                    on_unavailable="continue" if continuing else None,
                )
            # A kind that declares a control prefix (the `agent_cli` Reflector, whose
            # CLI writes its result file there) gets that one subtree created and
            # left writable by `freeze_projection`; every other kind gets a wholly
            # read-only tree.
            control_prefix = getattr(self.reflector, "control_prefix", None)
            with _file_boundary("reflection_workspace_unavailable"):
                reflections_root = ensure_private_directory(self.run_root / REFLECTIONS_DIR)
                try:
                    # Family 14: only `readable ∪ writable`, bound to `D`. A
                    # mismatch is `WorkspaceChanged`: the published Parent
                    # changed between two reads, this boundary's file fault.
                    projection = project(
                        parent.root,
                        control_prefix=control_prefix,
                        tmp_root=reflections_root,
                        visible=None if view is None else view.visible,
                        expected_digest=role_digest,
                    )
                except ControlCollision as exc:
                    collision = exc
            if collision is not None:
                raise collision
            assert projection is not None
            integrity_failed = False

            def verify_reflection_sources() -> None:
                try:
                    inputs.verify_admission()
                    parent.verify()
                    if isinstance(docs, _V2DocumentSnapshot):
                        _v2_load_document_snapshot(self.run_root, expected_digest=docs.digest)
                except (ArtifactError, ValueError) as exc:
                    raise ReaderIntegrityError("reflection_source_changed") from exc

            with _file_boundary("reflection_workspace_unavailable"):
                freeze_projection(projection)
                if view is not None:
                    # What the Reflector may read and never change: the frozen
                    # tree outside its own control subtree (F0 R67).
                    frozen_view = _without_control(capture_tree(projection.root), control_prefix)
            extra = {} if history_reader is None else history_reader.handlers()
            with _file_boundary("reflection_workspace_unavailable"):
                reader = inputs.reader_view(extra_handlers=extra).open_operation(
                    f"reflect-{operation}",
                    {
                        name
                        for name in inputs.handlers()
                        if name
                        in {
                            "list_evidence",
                            "read_evidence",
                            "read_trace",
                            "list_documents",
                            "read_document",
                        }
                    }
                    | set(extra),
                )
            attempts = AttemptRecorder()
            # #318 (ADR-0048): a `/16` chat Reflector checks each attempted
            # `finish_reflection` with this same closure, leak scans first,
            # before it may end; `/15` and every other `/16` kind keep the
            # earlier order and see the closure only at final admission below.
            in_role = (
                self.family_version == 15
                and getattr(self.reflector, "result_repair", False) is True
            )

            last_checked: ReflectResult | None = None

            def admit_terminal(result: ReflectResult, *, collect: bool = True) -> tuple[Group, ...]:
                nonlocal last_checked
                if collect:
                    last_checked = result
                current = self.snapshot().rounds[round_number].reflections[-1]
                probe_seqs = (
                    {int(probe["seq"]) for probe in current["probes"]}
                    if self.probe is not None
                    else None
                )
                # `runtime_secrets`, not `self.secrets`: once a probe mediator
                # started, the reflection's set also holds its capability.
                if adaptive_brief is not None:
                    return admit_reflection_result(
                        result,
                        catalog_ids=adaptive_brief.observation_ids,
                        case_ids={case.case_id for case in self.corpus},
                        editable_paths=self.artifact_policy.writable_paths,
                        probe_seqs=probe_seqs,
                        needles=needles,
                        runtime_secrets=runtime_secrets,
                        observation_ids=adaptive_brief.observation_ids,
                        failing_evidence_ids=adaptive_brief.failing_evidence_ids,
                        document_ids=adaptive_brief.document_ids,
                        group_text_chars=group_text_chars,
                        leak_first=in_role,
                        normalize_empty_keys=self.family_version == 15,
                        modes=self.checks,
                        hits=hits if collect else None,
                        leak_field=self.leak_field,
                    )
                catalog = render_reflect_catalog(brief)
                return admit_reflection_result(
                    result,
                    catalog_ids=catalog_case_ids(catalog),
                    case_ids={case.case_id for case in self.corpus},
                    editable_paths=self.artifact_policy.writable_paths,
                    probe_seqs=probe_seqs,
                    needles=needles,
                    runtime_secrets=runtime_secrets,
                    leak_first=in_role,
                    normalize_empty_keys=self.family_version == 15,
                    modes=self.checks,
                    hits=hits if collect else None,
                    leak_field=self.leak_field,
                )

            role_started = True
            outcome = await run_reflector(
                self.reflector,
                ReflectRequest(
                    reflection_id=reflection_id,
                    round=round_number,
                    workspace=projection.root,
                    evidence=brief,
                    environment_docs=docs if isinstance(docs, EnvironmentDocs) else None,
                    probe=access,
                    probe_session=session,
                    editable_paths=self.artifact_policy.writable_paths,
                    deadline_seconds=self.policy.reflection_timeout_seconds,
                    reader=reader,
                    editor_max_changed_files=self._editor_file_budget(),
                    attempts=attempts,
                    result_admission=admit_terminal if in_role else None,
                    preview_admission=(lambda result: admit_terminal(result, collect=False))
                    if in_role
                    else None,
                    normalize_empty_keys=self.family_version == 15,
                ),
                cancelled=lambda: self.cancelled() or self.stopping,
                deadline_at=deadline_at,
            )
            role_returned = True
            self.note_provider_attempts(
                "reflector",
                f"r{round_number:02d}",
                _role_attempts(attempts, outcome.error_class),
            )
            self.reflector_alive = self.reflector_alive or outcome.alive
            if reader is not None:
                try:
                    reader.verify_admission(verify_reflection_sources)
                except ReaderIntegrityError:
                    integrity_failed = True
            if frozen_view is not None:
                projection_changed = _reflector_changed_projection(
                    projection.root, control_prefix, frozen_view
                )
        except ControlCollision as exc:
            collision = exc
        finally:
            try:
                try:
                    if (
                        role_returned
                        and continuing
                        and mediator_failed
                        and session is not None
                        and projection is not None
                        and control_prefix is not None
                    ):
                        from evolution.tree.roles.reflection_role_helpers import read_probe_call_log

                        await session.record_unavailable_calls(
                            read_probe_call_log(projection.root, control_prefix)
                        )
                finally:
                    quiescent = await self.close_probes()
            finally:
                try:
                    try:
                        if reader is not None:
                            with _file_boundary("reflection_workspace_unavailable"):
                                reader.close()
                    finally:
                        if projection is not None:
                            with _file_boundary("reflection_workspace_unavailable"):
                                discard(projection)
                finally:
                    if session is not None:
                        session.raise_if_failed()
        reads = self._reader_reads(reader)
        if self.reflector_alive or not quiescent:
            # Spec §7's editor rule, for the other role: a task still running
            # past its grace has violated the protocol, so the round's
            # diagnosis is refused and the campaign ends at the barrier. The
            # task is still running, so the run is not quiescent -- and the
            # flag says so *before* the append, exactly as `run_editor` sets
            # the editor's, because a fault inside that write must not leave
            # the result claiming quiescence over a live Reflector.
            self.reflector_alive = True
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                "unresponsive",
                None,
                reads=reads,
                **hits.terminal_fields(),
            )
            self.stop(_Pending("infrastructure_failed", "reflector_unresponsive", "none"))
            return
        if collision is not None:
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                "control_collision",
                str(collision),
                reads=reads,
                **hits.terminal_fields(),
            )
            return
        if integrity_failed:
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                "reader_integrity",
                outcome.note,
                reads=reads,
                **hits.terminal_fields(),
            )
            if reader is not None and reader.integrity_fault == "diagnostic_bundle_changed":
                with _boundary("artifact_store"):
                    raise ArtifactCorruptionError("artifact_digest_mismatch")
            return
        if projection_changed and not (
            outcome.terminal == "failed"
            and self._role_stop_class(str(outcome.error_class)) in LIFECYCLE_ERRORS
        ):
            # Family 14 (routine choice 7): a changed projection outranks every
            # answer the role produced -- reflected, declined or a settled
            # failure -- and is itself a counted `content` failure. A stopping
            # campaign still writes its lifecycle terminal, as below, so the
            # reflection re-runs from a fresh projection; a lifecycle outcome
            # falls through to the `failed` branch for the same reason.
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                self._role_stop_class("cancelled")
                if self.stopping or self.cancelled()
                else "projection_changed",
                outcome.note,
                reads=reads,
                **hits.terminal_fields(),
            )
            return
        if outcome.terminal == "failed":
            # `run_reflector` reports a stop seen by its predicate as
            # `cancelled`; family 10 names the stop instead (spec §4.1).
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                self._role_stop_class(str(outcome.error_class)),
                outcome.note,
                reads=reads,
                **hits.terminal_fields(),
                code=outcome.code,
                detail=None if outcome.rejection is None else outcome.rejection.to_payload(),
            )
            return
        if self.stopping or self.cancelled():
            # The terminal barrier: a reflection that finished inside the poll
            # window is a true terminal, but nothing of it is admitted once the
            # campaign is stopping -- the same rule `run_editor` keeps. Family
            # 10 writes `interrupted` unless the stop is a cancellation, and
            # the reflection re-runs on resume (spec §4.1, ADR-0023).
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                self._role_stop_class("cancelled"),
                outcome.note,
                reads=reads,
                **hits.terminal_fields(),
            )
            return
        reflection = self.snapshot().rounds[round_number].reflections[-1]
        probe_seqs = (
            {int(probe["seq"]) for probe in reflection["probes"]}
            if self.probe is not None
            else None
        )
        try:
            assert outcome.status in {"reflected", "declined"}
            final_result = ReflectResult(
                cast(Literal["reflected", "declined"], outcome.status),
                outcome.groups,
                outcome.note,
            )
            groups = admit_terminal(final_result, collect=final_result != last_checked)
        except GroupError as exc:
            rejection = exc.rejection()
            self.fail_reflection(
                round_number,
                reflection_id,
                operation,
                exc.code,
                outcome.note,
                reads=reads,
                **hits.terminal_fields(),
                leak=self.leak_field(exc.needle) if exc.code == "oracle_leak" else None,
                detail=None if rejection is None else rejection.to_payload(),
            )
            return
        journal_groups = [group.to_payload() for group in groups]
        journal_note = outcome.note
        if self.family_version == 15:
            journal_groups = self.journal_value(journal_groups, runtime_secrets)
            journal_note = self.journal_value(journal_note, runtime_secrets)
        self.append(
            "reflection_finished",
            dict(
                round=round_number,
                reflection_id=reflection_id,
                operation_id=operation,
                status=outcome.status,
                groups=journal_groups,
                out_of_scope_paths=[],
                probes_used=len(probe_seqs or ()),
                note_digest=result_note_digest(journal_note),
                **({"reads": reads}),
                **self._reflection_probe_availability(round_number, reflection_id),
            ),
            operation,
            **hits.terminal_fields(),
        )
        self.hook("after_reflection_finished")

    def _reflection_probe_availability(
        self, round_number: int, reflection_id: str
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        if not probe_continues(snapshot.identity, snapshot.family_version):
            return {}
        state = snapshot.rounds[round_number]
        check = state.probe_check
        if check is None or check["reflection_id"] != reflection_id:
            return {}
        reflection = next(
            entry for entry in state.reflections if entry["reflection_id"] == reflection_id
        )
        mediator = self._probe_startups.get(reflection_id)
        if mediator is None:
            # An interrupted role has no live session. Recover the already
            # written, fixed startup diagnostic; call counters remain journal-owned.
            mediator = "started"
            path = self.run_root / "reflector-logs" / "probes" / reflection_id / "startup.json"
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    metadata = os.fstat(descriptor)
                    if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= 1024:
                        value = json.loads(os.read(descriptor, 1025))
                        if value == {
                            "error": "probe_mediator_startup_failed",
                            "detail": "probe listener could not start",
                        }:
                            mediator = "failed"
                finally:
                    os.close(descriptor)
            except (OSError, ValueError, UnicodeError, RecursionError):
                pass
        return {
            "probe_availability": {
                "health": check["status"],
                "mediator": mediator,
                "unreachable": sum(
                    entry["error"] == "unreachable" for entry in reflection["probes"]
                ),
                "transport_failures": sum(
                    entry["error"] == "transport" for entry in reflection["probes"]
                ),
            }
        }

    def fail_reflection(
        self,
        round_number: int,
        reflection_id: str,
        operation: str,
        error_class: str,
        note: str | None,
        *,
        reads: dict[str, Any] | None = None,
        shadowed: list[dict[str, Any]] | None = None,
        code: str | None = None,
        leak: Mapping[str, str] | None = None,
        detail: Mapping[str, str] | None = None,
    ) -> None:
        """One `reflection_failed`, whatever refused it (§3.3's failure classes).

        Family 10 (role resilience spec §4.1-§4.2, ADR-0023): a settled
        failure also carries its closed `reason`, mapped from `error_class`
        and the role's `code`, and a failure of class `provider` or `unknown`
        then stops the campaign into a `reflector_provider_failed` or
        `reflector_failing` suspension without closing the round. A lifecycle
        terminal carries no reason and decides nothing here.
        """

        reason = reflection_reason(error_class, code)
        self.append(
            "reflection_failed",
            dict(
                round=round_number,
                reflection_id=reflection_id,
                operation_id=operation,
                error_class=error_class,
                note_digest=None if not note else result_note_digest(note),
                **({"reads": reads or self._empty_reads()}),
                **self._reflection_probe_availability(round_number, reflection_id),
                **({"reason": reason} if reason is not None else {}),
                **({"leak": dict(leak)} if self.family_version == 15 and leak is not None else {}),
                **(
                    {"detail": dict(detail)}
                    if self.family_version == 15 and detail is not None
                    else {}
                ),
            ),
            operation,
            **({"shadowed": shadowed} if shadowed and reason is not None else {}),
        )
        self.hook("after_reflection_failed")
        if reason is None:
            return
        klass = reflection_class(error_class, reason)
        if klass in ("provider", "unknown"):
            self.stop(
                _Pending(
                    "infrastructure_failed",
                    "reflector_provider_failed" if klass == "provider" else "reflector_failing",
                    "none",
                )
            )

    async def select_gate_scope(self, round_number: int) -> None:
        """The promotion gate's scope, from the round's settled diagnosis (spec §3.3).

        A diagnosis that claims two or more cases scopes the gate to exactly
        those (`claim`); anything less -- a failed reflection, a declined one,
        a claim over a single case -- falls back to the focus cluster's own
        cases, under the source that says which of the three it was. The scope
        is evidence-bearing: it cites the reflection record it read.
        """

        state = self.snapshot().rounds[round_number]
        entry = state.diagnosis(self.resilience)
        assert entry is not None
        assert state.focus_portion is not None
        focus_cases = list(state.focus_portion["case_ids"])
        # Family 8 (ADR-0021): the selection widens to whole families. The
        # membership is the one journaled in the adaptation identity -- never
        # recomputed from the corpus -- so the audit re-derives the same scope
        # from the same bytes; the evaluable cases use the exclusion the looks
        # apply.
        membership = None
        evaluable = None
        membership = self.adaptive.identity.family_membership
        baseline = str(state.baseline_measurement_id)
        # The baseline epoch's repeats, as the audit's `policy_for` reads them.
        repeats = self._bound_repeats(baseline)
        excluded = self.excluded_pairs(baseline, repeats)
        evaluable = frozenset(
            row.case_id
            for row in self.measurements[baseline].sample_rows(repeats)
            if (row.case_id, row.repeat) not in excluded
        )
        scope = derive_gate_scope(
            reflection_failed=entry["status"] == "failed",
            groups=entry["groups"],
            fallback=focus_cases,
            family_membership=membership,
            corpus_order=[case.case_id for case in self.corpus],
            evaluable_cases=evaluable,
        )
        payload: dict[str, Any] = dict(
            round=round_number, source=scope.source, cases=list(scope.cases)
        )
        payload.update(
            claimed_cases=list(scope.claimed_cases),
            selected_cases=list(scope.selected_cases),
            unevaluable_cases=list(scope.unevaluable_cases),
        )
        payload["evidence"] = [self.record_reference(int(entry["seq"]))]
        self.append("gate_scope_selected", payload)

    # -- attempts ---------------------------------------------------------

    def open_attempt(self, action: OpenAttempt) -> None:
        version, digest = self.snapshot().champion
        self.append(
            "attempt_opened",
            dict(
                round=action.round,
                attempt_index=action.index,
                attempt_id=action.attempt_id,
                parent_version=version,
                parent_digest=digest,
                **({"path": action.path} if self.family_version == 15 else {}),
            ),
        )
        if self.family_version == 15:
            self.hook("after_attempt_opened")

    def baseline_of(self, attempt: AttemptState) -> str:
        return str(self.snapshot().rounds[attempt.round].baseline_measurement_id)

    @cached_property
    def role_view(self) -> RoleVisibilityView | None:
        """Family 14's role visibility (F0 R64), or `None` for families up to 13.

        Both inputs are fixed for the session: a `role_visibility` change is a
        bound-identity change that closes the campaign, never one applied in
        place, so the view is computed once.
        """

        return RoleVisibilityView.for_campaign(self.connection, self.artifact_policy)

    def _role_visible(self) -> Callable[[str], bool] | None:
        """The role path predicate the evidence projections filter by (H11 Task 6).

        `None` for families up to 13, whose projections stay byte for byte.
        """

        view = self.role_view
        return None if view is None else view.visible

    def _role_projection_digest(self, parent: ArtifactRef) -> str:
        """The `role_projection/1` digest `D` of the verified published Parent (F0 R67).

        Routine choice 2: the frozen role inputs precede every projection, so
        `D` is taken from a no-follow capture of the published Parent itself,
        after `ArtifactRef.verify` proves the copy matches the ledger. Each role
        then re-derives it from its own projection (`project(...,
        expected_digest=D)`), which binds the Parent and the role's workspace.

        `visible` applies no artifact exclusions, and need not: `project(visible=
        ...)` and this capture only ever read store-derived trees -- a published
        copy or an Attempt workspace copied from one -- and an excluded path
        never reaches a store copy (`ArtifactRef.verify` refuses one that holds
        it). A corrupt Parent is the store's `persistence_failed` fault; an
        unreadable capture is the run's own file fault.
        """

        view = self.role_view
        assert view is not None
        with _boundary("artifact_store"):
            parent.verify()
        with _file_boundary("role_projection_unavailable"):
            return role_projection_digest(capture_tree(parent.root), view.visible)

    def inode_floor(self, *, fresh: bool) -> Callable[[int], InodeNeed] | None:
        """ADR-0040 D8: the campaign's inode floor as a function of the seed's F.

        Family 14 only: every earlier family's writer keeps its byte check
        alone (ADR-0030, PDR-0026). At a resume F is the largest of the seed,
        the current Champion copy and the open round's sealed Candidates; a
        fresh run adds what it writes before round 1.
        """

        terms = self._inode_terms()
        held = 0 if fresh else self._held_tree_inodes()
        documents = (
            count_tree(self.run_root / "environment-docs")
            if fresh and self.environment_docs is not None
            else 0
        )

        def need(seed: int) -> InodeNeed:
            return campaign_need(terms, max(seed, held), fresh=fresh, documents=documents)

        return need

    def _inode_terms(self) -> CampaignTerms:
        policy = self.policy
        return CampaignTerms(
            attempts=policy.attempts_per_round,
            parallel=policy.parallel_attempts,
            rounds=policy.rounds,
            cases=len(self.corpus),
            repeats=policy.repeats,
            fresh_parent=bool(policy.fresh_parent),
            refresh=bool(policy.baseline_refresh_after_rounds),
            stability=policy.stability or "unstable",
            round_stop=policy.round_stop or "first_accepted",
            probe_budget=None if self.probe is None else self.probe.budget,
            growth=self._editor_file_budget() or 0,
            shared_tmp=shares_tmp(existing_ancestor(self.run_root)),
            scorer_concurrency=policy.max_scorer_concurrency,
        )

    def _held_tree_inodes(self) -> int:
        """The largest copy a resume can still copy from this prefix."""

        try:
            observed = inspect_log(
                self.run_root,
                file_name=CAMPAIGN_FILE,
                max_record_bytes=MAX_RECORD_BYTES,
                max_log_bytes=MAX_JOURNAL_BYTES,
                state_factory=lambda: CampaignLifecycle(
                    selector=None, family_version=self.family_version
                ),
                envelope_family=self.envelope_family,
            )
            snapshot = reduce(observed.records, selector=None, family_version=self.family_version)
            open_round = _open_round(snapshot)
            open_attempts = (
                set(snapshot.rounds[open_round].attempts) if open_round is not None else set()
            )
            lineage = ArtifactStore.inspect(self.run_root)
        except Exception:  # the resume reports its own fault
            return 0
        champion = lineage.champion
        if champion is None:
            return 0
        trees = [count_tree(self.run_root / champion.reference.locator)]
        if open_round is not None:
            trees.extend(
                count_tree(self.run_root / candidate.reference.locator)
                for candidate in lineage.candidates
                if candidate.state == "sealed"
                and candidate.attempt_id in open_attempts
                and candidate.reference is not None
            )
        return max(trees)

    def note_inodes(self, estimate: CapacityEstimate | None) -> None:
        """Emit at most one advisory inode note per invocation."""

        if (
            self.notes is None
            or getattr(self, "_inode_note_emitted", False)
            or estimate is None
            or estimate.free_inodes is None
            or estimate.inode_need is None
        ):
            return
        note = _low_floor_note(estimate.free_inodes, estimate.inode_need.floor) or whole_run_note(
            estimate.free_inodes, estimate.inode_need
        )
        if note is not None:
            self._inode_note_emitted = True
            with suppress(Exception):
                self.notes(note)

    def note_provider_attempts(
        self, role: str, operation: str, report: AttemptReport | None
    ) -> None:
        """#290 §4.4 (G6): tell the operator about one role invocation's failed
        provider attempts. A phase that went on is noted now, as the invocation
        returns; the phase that ended it in a provider failure is kept for the
        result, which the CLI prints after the suspension report. Presentation
        only: nothing here is journaled or projected."""

        if report is None:
            return
        recovered, failed = report.lines(role, operation)
        self._provider_attempt_lines.extend(failed)
        if self.notes is None:
            return
        for line in recovered:
            with suppress(Exception):
                self.notes(line)

    def _editor_file_budget(self) -> int | None:
        """ADR-0037 D10 (PDR-0023 amendment): the bound Editor's `max_changed_files`
        for the family-14 Reflector opening; `None` in earlier families and for an
        Editor kind without the key (`command`, test fakes)."""

        budget = getattr(self.editor, "max_changed_files", None)
        return budget if isinstance(budget, int) else None

    def brief_for(self, attempt: AttemptState, *, hits: HitSink | None = None) -> EditBrief:
        """The round's one brief, built at the first attempt's editor start (§4.3).

        Every attempt of a round is handed the same object: the history reads
        only closed rounds, the baseline measurement is complete before any
        attempt opens, and the published copies are immutable, so a rebuild
        on resume — the cache is memory only — yields byte-identical bytes.

        The cache is one `(round, brief)` pair, replaced when the round
        changes. Rounds are entered in order and no closed round's brief is
        ever asked for again, so one pair is the whole working set and nothing
        accumulates over a long campaign.
        """

        cached = self.brief_cache
        if hits is None and cached is not None and cached[0] == attempt.round:
            return cached[1]
        assert self.store is not None
        baseline = self.baseline_of(attempt)
        measurement = self.measurements[baseline]
        rows = measurement.sample_rows(self.policy.repeats)
        with _boundary("artifact_store"):
            parent = self.store.published_ref("champion", attempt.parent_version)
        records = measurement.records()
        # Family 14 (PDR-0023, H11 Task 6): neither projection names a hidden
        # path; `visible` is `None` for every earlier family.
        visible = self._role_visible()
        hit_options: dict[str, Any] = {"hits": hits} if hits is not None else {}
        observations = project_observations(
            records=records,
            run_root=self.run_root,
            corpus=self.corpus,
            baseline=parent,
            carried=self.carried_traces(records),
            visible=visible,
            secrets=self.secrets,
            pin=self.snapshot().round_evidence(attempt.round),
            **hit_options,
        )
        history = project_history(
            self.snapshot(),
            measurements=self.measurements,
            store=self.store,
            artifact_policy=self.artifact_policy,
            corpus=self.corpus,
            policy=self.policy,
            visible=visible,
        )
        state = self.snapshot().rounds[attempt.round]
        grouped = self._admitted_grouping(attempt.round)
        assert grouped is not None and state.focus_portion is not None
        # The same cohorts the round's focus was selected from (§4.3), so a
        # brief's cohort masses and memberships name what the gate can read.
        cohorts = self.focus_cohorts(attempt.round, grouped)
        diagnosis = diagnosis_for_brief(
            state,
            resilient=self.resilience,
            focus_cases=state.focus_portion["case_ids"],
            cohort_masses={cohort.cohort_id: len(cohort.member_ids) for cohort in cohorts},
            cohort_of={
                evidence: cohort.cohort_id for cohort in cohorts for evidence in cohort.member_ids
            },
        )
        diagnosis = replace(
            diagnosis, focus=DiagnosisFocus("selected", tuple(state.focus_portion["case_ids"]))
        )
        if visible is not None:
            _check_visible_targets(diagnosis, visible, history)
        brief = build_brief(
            rows,
            self.corpus,
            self.policy,
            attempt.parent_digest,
            diagnosis=diagnosis,
            observations=observations,
            history=history,
            # #282: the Attempt's round limit, as for its reflection, so a
            # resumed Attempt's frozen common input survives a mid-round rebase.
            sentence_chars=self.snapshot().round_group_text_chars(attempt.round),
            pin=self.snapshot().round_evidence(attempt.round),
        )
        self.brief_cache = (attempt.round, brief)
        return brief

    async def run_editor(self, attempt_id: str) -> None:
        assert self.store is not None
        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None
        freeze_hits = HitSink()
        tool_hits = self._editor_hits(attempt_id)
        pin = snapshot.round_evidence(attempt.round)
        input_modes = (
            ENFORCE_ALL if pin is None else CheckModes(cast(Mapping[str, Mode], pin.input_modes))
        )
        view = self.role_view
        role_digest: str | None = None
        if view is not None:
            # Family 14 (F0 R67, H11 choice 1): the frozen common input binds the
            # role projection of the Attempt's Parent. `D` is recomputed on every
            # run, first or resumed, and must reproduce the frozen digest below.
            with _boundary("artifact_store"):
                parent_ref = self.store.published_ref("champion", attempt.parent_version)
            role_digest = self._role_projection_digest(parent_ref)

        def common_input(brief: EditBrief) -> str:
            if role_digest is None:
                return brief_digest(brief)
            return hashlib.sha256(
                canonical_json({"brief": brief_digest(brief), "role_projection": role_digest})
            ).hexdigest()

        if attempt.frozen_input is None:
            state = snapshot.rounds[attempt.round]
            common = common_input(
                self.brief_for(attempt, hits=freeze_hits)
                if self.family_version == 15
                else self.brief_for(attempt)
            )
            mode = self.policy.attempt_feedback or "none"
            feedback_options: dict[str, Any] = {}
            if self.family_version == 15:
                feedback_options["redact"] = lambda text: redact_for_journal(text, self.secrets)
            feedback = project_attempt_feedback(
                state,
                current_index=attempt.index,
                measurements=self.measurements,
                store=self.store,
                artifact_policy=self.artifact_policy,
                policy=self.policy,
                exam_cases=() if state.gate_scope is None else state.gate_scope["cases"],
                mode=mode,
                forbidden=check_predicate(
                    "attempt_feedback_screen",
                    "withheld",
                    lambda text: (
                        find_needle(text, self.needles)
                        or secret_location({"payload": text}, self.secrets) is not None
                    ),
                    modes=input_modes,
                    hits=freeze_hits,
                ),
                source_refs={},
                **feedback_options,
                outcomes=feedback_outcomes(self.family_version),
                family_version=self.family_version,
                visible=None if view is None else view.visible,
            )
            assert self.log is not None
            source_refs = []
            for row in feedback["attempts"]:
                earlier = state.attempts[row["attempt_id"]]
                assert earlier.closed_seq is not None
                source_refs.append(self.record_reference(earlier.closed_seq))
                _, comparison = effective_attempt_feedback(
                    state, earlier, family_version=self.family_version
                )
                if comparison is not None:
                    source_refs.append(self.record_reference(comparison["seq"]))
            if self.family_version == 15:
                feedback = self.journal_value(feedback, self.secrets)
            frozen = {
                "common_input_digest": common,
                "feedback": feedback,
                "feedback_mode": mode,
                "source_refs": source_refs,
            }
            self.append(
                "attempt_input_frozen",
                {
                    "attempt_id": attempt_id,
                    "round": attempt.round,
                    **frozen,
                    "input_digest": hashlib.sha256(canonical_json(frozen)).hexdigest(),
                },
                **freeze_hits.terminal_fields(),
            )
            self.hook("after_attempt_input_frozen")
            attempt = self.attempt_state(self.snapshot(), attempt_id)
            assert attempt is not None and attempt.frozen_input is not None
        with _boundary("artifact_store"):
            workspace = (
                self.store.restore_candidate_workspace(
                    attempt_id,
                    parent_id=attempt.parent_version,
                    parent_digest=attempt.parent_digest,
                )
                if self.store.ledger.state.workspaces.get(attempt_id) == "ready"
                else self.store.materialize_candidate(attempt_id)
            )
        self.workspaces[attempt_id] = workspace
        brief = self.brief_for(attempt)
        assert attempt.frozen_input is not None
        if common_input(brief) != attempt.frozen_input["common_input_digest"]:
            raise CampaignInvariantViolated("frozen editor common input changed")
        brief = with_attempt_feedback(brief, attempt.frozen_input["feedback"])
        operation = uuid4().hex
        self.append(
            "editor_started",
            dict(
                attempt_id=attempt_id,
                operation_id=operation,
                brief_digest=brief_digest(brief),
                **(
                    {
                        "input_digest": attempt.frozen_input["input_digest"],
                        "common_input_digest": attempt.frozen_input["common_input_digest"],
                    }
                    if attempt.frozen_input is not None
                    else {}
                ),
            ),
            operation,
        )
        self.hook("after_editor_started")
        traces = None
        if pin is not None:
            traces = self.adaptive_inputs_for(attempt.round).traces
            if traces is not None:
                traces = traces.with_hits(tool_hits)
        request = EditRequest(
            attempt_id=attempt_id,
            round=attempt.round,
            workspace=workspace,
            writable_paths=self.artifact_policy.writable_paths,
            brief=brief,
            deadline_seconds=self.policy.editor_timeout_seconds,
            projection=(
                None
                if view is None or role_digest is None
                else RoleProjection(view.visible, role_digest)
            ),
            attempts=AttemptRecorder(),
            traces=traces,
        )
        outcome = await run_editor(
            self.editor, request, cancelled=lambda: self.cancelled() or self.stopping
        )
        assert request.attempts is not None
        self.note_provider_attempts(
            "editor", attempt_id, _role_attempts(request.attempts, outcome.error_class)
        )
        if outcome.alive:
            self.editor_alive = True
            self.unresponsive = True
            self.append(
                "editor_failed",
                dict(
                    attempt_id=attempt_id,
                    operation_id=operation,
                    error_class="unresponsive",
                    note_digest=None,
                ),
                operation,
            )
            self.hook("after_editor_finished")
            self.stop(_Pending("infrastructure_failed", "editor_unresponsive", "none"))
            await self.close_attempt(attempt_id, "abandoned", "editor_unresponsive", None, None)
            return
        if (
            outcome.terminal == "finished"
            and (self.cancelled() or self.stopping)
            and (not self._decide_stop_is_cancellation())
        ):
            # Family 10 (ADR-0023, spec §8 item 8a):
            # an Editor that finished inside the poll window a stop other than
            # the caller's cancellation opened is cut short like a running one.
            # Its result is not committed -- no `editor_finished`, no close --
            # so the Attempt stays open and resume re-runs it from Parent.
            self.append(
                "editor_failed",
                dict(
                    attempt_id=attempt_id,
                    operation_id=operation,
                    error_class="interrupted",
                    note_digest=None,
                ),
                operation,
            )
            self.hook("after_editor_finished")
            return
        if outcome.terminal == "finished":
            self.append(
                "editor_finished",
                dict(
                    attempt_id=attempt_id,
                    operation_id=operation,
                    status=outcome.status,
                    note_digest=note_digest(outcome.note),
                    **({"continued": outcome.continued}),
                ),
                operation,
                **tool_hits.terminal_fields(),
            )
        else:
            # Family 10 (spec §5.3, ADR-0023): an Editor a stop cut short is
            # `interrupted` unless the stop is a cancellation; the adaptive
            # replay then keeps its Attempt open and re-runs it from Parent.
            # A settled failure carries its closed `reason`, mapped from
            # `error_class` and the role's `code`.
            error_class = self._role_stop_class(str(outcome.error_class))
            reason = journaled_editor_reason(
                error_class, outcome.code, family_version=self.family_version
            )
            self.append(
                "editor_failed",
                dict(
                    attempt_id=attempt_id,
                    operation_id=operation,
                    error_class=error_class,
                    note_digest=note_digest(outcome.note) if outcome.note else None,
                    **({"reason": reason} if reason is not None else {}),
                ),
                operation,
                **(tool_hits.terminal_fields() if reason is not None else {}),
            )
            if reason is not None and editor_class(error_class, reason) != "content":
                # A `provider`/`unknown` failure suspends and leaves the
                # Attempt open: it re-runs from Parent on resume. A `content`
                # failure falls through to the planner's `editor_failed` close.
                self.hook("after_editor_finished")
                self.stop(
                    _Pending(
                        "infrastructure_failed",
                        "editor_provider_failed"
                        if editor_class(error_class, reason) == "provider"
                        else "editor_failing",
                        "none",
                    )
                )
                return
        self.hook("after_editor_finished")
        if outcome.terminal == "finished" and (self.cancelled() or self.stopping):
            # Spec §7's terminal barrier: nothing is published once the campaign
            # is stopping. An editor that finished inside the poll window the
            # barrier opened leaves `run_editor` as an ordinary success -- the
            # editor did finish, and its terminal says so truthfully -- but the
            # campaign must publish nothing of it. Abandoning the workspace is
            # what revokes the attempt's seal eligibility, so the bytes it wrote
            # can never be published, and the attempt is closed rather than left
            # open for resume to reconcile.
            #
            # The reason names what actually stopped the campaign: `cancelled`
            # is the caller's token (amendment 15, §5's registry), so a fault or
            # a threshold that set `stopping` closes `abandoned(interrupted)` --
            # the reason §11 already gives an attempt an interruption caught
            # mid-flight. Neither claims an editor failure that did not happen.
            reason = "cancelled" if self.cancelled() else "interrupted"
            await self.close_attempt(attempt_id, "abandoned", reason, None, None)
            return
        if outcome.terminal == "finished" and outcome.status == "edited":
            if unchanged(workspace, self.artifact_policy, attempt.parent_digest):
                await self.close_attempt(attempt_id, "abandoned", "editor_no_change", None, None)

    def leak_field(self, needle: str | None) -> dict[str, str] | None:
        if self.family_version != 15 or needle is None:
            return None
        found = locate_needle(self.corpus, needle, secret_paths=self.secret_oracle_paths)
        if found is None:
            return None
        case_id, tokens = found
        leak = {"case_id": case_id}
        pointer = encode_pointer(tokens)
        if len(pointer.encode("utf-8")) <= MAX_POINTER_BYTES and not any(
            secret and secret in pointer for secret in self.secrets
        ):
            leak["oracle_pointer"] = pointer
        return leak

    def _editor_hits(self, attempt_id: str) -> HitSink:
        def log_hit(value: dict[str, Any]) -> None:
            with _boundary("artifact_store"):
                with open_attempt_log(
                    self.run_root / "editor-logs",
                    attempt_id,
                    secrets=self.secrets,
                    admitted=getattr(self.editor, "admitted_hygiene", ()),
                ) as log:
                    log.record(value)

        return HitSink(log=log_hit)

    def _seal_shadow_fields(self, attempt_id: str) -> dict[str, Any]:
        if self.checks.mode("seal_oracle_leak") != "shadow":
            return {}
        hits = self.attempt_seal_hits.get(attempt_id)
        if hits is None:
            attempt = self.attempt_state(self.snapshot(), attempt_id)
            if attempt is None or attempt.candidate_digest is None:
                return {}
            hits = HitSink()
            needle = self.candidate_leak(attempt_id)
            if needle is not None:
                hits.hit("seal_oracle_leak", "oracle_leak", leak=self.leak_field(needle))
            self.attempt_seal_hits[attempt_id] = hits
        return hits.terminal_fields()

    def candidate_leak(self, attempt_id: str) -> str | None:
        """Any added line of a changed file carrying an oracle needle (spec §5.6, PDR-0007).

        Compares the Candidate against the Parent's own published copy over
        the paths `artifact_policy.changes` names changed; an added line --
        a `+` line of the ordered diff of the Parent's text into the
        Candidate's (`_added_lines`) -- that carries a needle refuses the
        seal. A needle already present in the Parent (an unchanged line, or
        one only removed) is not a leak: the Candidate did not introduce it.
        But multiplicity counts, so a *second* copy of a line the Parent
        already had is an added line and does refuse. A binary or unreadable Candidate path
        is skipped, not treated as a leak; a missing or binary Parent path
        reads as no prior lines, so every one of the Candidate's own lines
        there counts as added.

        Reads `self.workspaces[attempt_id]` -- the same workspace `run_editor`
        materialized, since a second `materialize_candidate` call for a known
        attempt refuses -- falling back to the published Candidate when the
        workspace is not in memory (a resume: `reconcile_attempts` finds the
        store already holds the published copy, and checks that tree instead).

        The scan/diff runs inside `_boundary("artifact_store")`, exactly as
        `store.seal`'s own scan/diff does: a bare `OSError` is `store.seal`'s
        own `io_failure` (wrapped into `ArtifactError` here so the boundary
        classifies it into the `persistence_failed` fault); an
        `ArtifactUnsafeError` (an unsafe tree shape -- a symlink, say) is
        caught inside the boundary so it is never reclassified, and instead
        propagates to the caller unchanged, for `seal_attempt` to map into
        `seal_refused` exactly as it already maps `store.seal`'s own refusals.
        """

        if self.checks.mode("seal_oracle_leak") == "off" or not self.needles:
            return None
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None
        assert self.store is not None
        unsafe: ArtifactUnsafeError | None = None
        leak: str | None = None
        with _boundary("artifact_store"):
            parent = self.store.published_ref("champion", attempt.parent_version)
            workspace = self.workspaces.get(attempt_id)
            child_root = (
                workspace
                if workspace is not None
                else self.store.published_ref("candidate", attempt_id).root
            )
            try:
                for relative in _changed_paths(parent.root, child_root, self.artifact_policy):
                    before = _text_lines(parent.root / relative)
                    after = _text_lines(child_root / relative)
                    if after is None:
                        continue
                    added = _added_lines(before or [], after)
                    for line in added:
                        leak = first_needle(line, self.needles)
                        if leak is not None:
                            break
                    if leak is not None:
                        break
            except ArtifactUnsafeError as exc:
                # Handled fully inside the boundary so `_boundary` never sees
                # it -- it would otherwise reclassify it as `persistence_failed`,
                # the way it does every other `ArtifactError` subtype.
                unsafe = exc
            except OSError as exc:
                raise ArtifactError("io_failure") from exc
        if unsafe is not None:
            raise unsafe
        return leak

    def refuse_leaked_seal(self, attempt_id: str) -> bool:
        """`True` when `candidate_leak` refuses the seal, having appended the refusal itself.

        Shared by `seal_attempt` and `reconcile_attempts`'s own resume-time
        check: an `ArtifactUnsafeError` from the scan maps to `seal_refused`
        with the error's own code, exactly as `store.seal`'s refusals do.
        """

        try:
            leak = self.candidate_leak(attempt_id)
        except ArtifactUnsafeError as exc:
            self.append_seal_refusal(attempt_id, exc.code)
            return True
        if self.checks.mode("seal_oracle_leak") == "shadow":
            if attempt_id not in self.attempt_seal_hits:
                hits = HitSink()
                if leak is not None:
                    hits.hit("seal_oracle_leak", "oracle_leak", leak=self.leak_field(leak))
                self.attempt_seal_hits[attempt_id] = hits
            return False
        if leak is not None:
            self.append_seal_refusal(attempt_id, "oracle_leak", leak=self.leak_field(leak))
            return True
        return False

    def seal_attempt(self, attempt_id: str) -> None:
        assert self.store is not None
        if self.refuse_leaked_seal(attempt_id):
            return
        with _boundary("artifact_store"):
            try:
                ref = self.store.seal(attempt_id)
            except ArtifactStoreStopped:
                # A stopped store is a persistence fault everywhere else, and the
                # seal boundary must not disagree by calling it `seal_refused`.
                raise
            except (ArtifactUnsafeError, ArtifactAdmissionRefused) as exc:
                self.append_seal_refusal(attempt_id, exc.code)
                return
        evidence = self.publication_reference(attempt_id)
        assert evidence is not None
        self.append(
            "attempt_sealed",
            dict(
                attempt_id=attempt_id,
                candidate_digest=ref.digest,
                dependency_digest=ref.dependency_digest,
                artifact_locator=CANDIDATE_LOCATOR + attempt_id,
                evidence=[evidence],
            ),
        )

    def append_seal_refusal(
        self, attempt_id: str, code: str, *, leak: Mapping[str, str] | None = None
    ) -> None:
        self.abandon(attempt_id)
        self.append(
            "attempt_closed",
            dict(
                attempt_id=attempt_id,
                outcome="abandoned",
                reason="seal_refused",
                look=None,
                code=code,
                **({"leak": dict(leak)} if self.family_version == 15 and leak is not None else {}),
            ),
        )

    def attempt_layers(self, snapshot: CampaignSnapshot, attempt: AttemptState) -> AffectedLayers:
        """The selected Attempt's case sets over the identity and policy in force (G2 spec §4.6).

        The same `affected_layers` the lifecycle applies when it admits the
        binds: `self.policy` is the policy in force, as the lifecycle's is.
        """

        assert attempt.selection is not None
        scope = snapshot.rounds[attempt.round].gate_scope
        assert scope is not None
        return affected_layers(
            attempt.selection,
            snapshot.identity["corpus_cases"],
            scope["cases"],
            global_layer=bool(self.policy.global_layer),
        )

    def bind_measurement(self, attempt_id: str) -> None:
        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.candidate_digest is not None
        measurement_id = measurement_id_for(attempt_id)
        # G2 spec §4.6: a family-12 Candidate measures layers 1 and 2, or the
        # whole corpus with the global layer; `None` is the whole corpus.
        case_subset = None
        layers = self.attempt_layers(snapshot, attempt)
        case_subset = subset_of(snapshot.identity["corpus_cases"], layers.candidate_cases)
        measurement = self.derive(
            measurement_id, attempt.candidate_digest, "candidate", case_subset=case_subset
        )
        self.append(
            "measurement_bound",
            dict(
                attempt_id=attempt_id,
                round=attempt.round,
                measurement_id=measurement_id,
                candidate_digest=attempt.candidate_digest,
                identity_digest=measurement.identity.digest,
            ),
        )
        self.hook("after_measurement_bound")
        with _boundary("measurement_journal", measurement_id):
            measurement.create()
        self.measurements[measurement_id] = measurement
        self.siblings[measurement_id] = "born"

    def bind_parent(self, attempt_id: str) -> None:
        """G2 spec §4.6: bind and create the fresh Parent over the Attempt's layer 1.

        It measures the Parent tree on the champion side, after the
        Candidate's `measurement_bound` and before the Attempt's first stage;
        `parent_measurement_bound` is durable before its journal is created
        (ADR-0008), as `bind_measurement` orders its own. In family 13 it
        measures F ∪ C instead, by the lifecycle's own rule.
        """

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.parent_digest is not None
        case_subset: Sequence[int] | None
        assert attempt.selection is not None
        scope = snapshot.rounds[attempt.round].gate_scope
        assert scope is not None
        focus, causal = confidence_scopes(
            attempt.selection, snapshot.identity["corpus_cases"], scope["cases"]
        )
        case_subset = sorted(focus + causal)
        case_repeats = (
            None if self.family_version == 15 else observe_case_repeats(self.policy, causal)
        )
        measurement_id = parent_measurement_id_for(attempt_id)
        measurement = self.derive(
            measurement_id,
            attempt.parent_digest,
            "champion",
            case_subset=case_subset,
            case_repeats=case_repeats,
        )
        self.append(
            "parent_measurement_bound",
            dict(
                attempt_id=attempt_id,
                round=attempt.round,
                measurement_id=measurement_id,
                candidate_digest=attempt.parent_digest,
                identity_digest=measurement.identity.digest,
            ),
        )
        self.hook("after_parent_measurement_bound")
        with _boundary("measurement_journal", measurement_id):
            measurement.create()
        self.measurements[measurement_id] = measurement
        self.siblings[measurement_id] = "born"

    def bind_finalists(self, round_number: int) -> None:
        """Freeze all finalists, or the next single first-accepted finalist (R38)."""

        snapshot = self.snapshot()
        state = snapshot.rounds[round_number]
        vocabulary = campaign_vocabulary_for(self.family_version)
        accepted = sorted(
            (a for a in state.attempts.values() if a.outcome == vocabulary.gate_pass_outcome),
            key=lambda a: a.index,
        )
        if self.policy.round_stop == "first_accepted":
            accepted = [a for a in accepted if a.attempt_id not in state.global_comparisons][-1:]
        if not accepted:
            # D5: the null `selection_decided`, in the family-13 key set. The
            # caller, `decide_round`, then closes the round `stalled` or
            # `unscored` in the same action (plan choice 8), and the round
            # keeps its baseline (design §6.12).
            # A family-13 policy always has a global mode (v10).
            assert self.policy.global_mode is not None
            self.hook("before_selection_decided")
            self.append(
                "selection_decided",
                {
                    **cohort_selection(
                        state, self.policy.global_mode, family_version=self.family_version
                    ),
                    "evidence": self.selection_evidence(round_number, None),
                },
            )
            self.hook("after_selection_decided")
            return
        version, digest = snapshot.champion

        def member_id(index: int | None) -> str:
            return final_measurement_id_for(round_number, len(state.finals) + 1, index)

        self.append(
            "finalists_bound",
            dict(
                round=round_number,
                champion=dict(version=version, digest=digest),
                finalists=[
                    dict(
                        attempt_id=a.attempt_id,
                        candidate_digest=a.candidate_digest,
                        level=finalist_level(self.policy, a),
                    )
                    for a in accepted
                ],
                members=[
                    *([member_id(None)] if self.policy.stability == "unstable" else []),
                    *(member_id(a.index) for a in accepted),
                ],
                **{"global": self.policy.global_mode},
                mode=self.policy.gate_mode,
                repeats=self.policy.repeats,
                looks=list(self.policy.looks),
                seed_mode=snapshot.identity["target"]["seed_mode"],
                epoch=len(snapshot.epochs) - 1,
                order=COHORT_ORDER,
                evidence=[self.record_reference(_closed_seq(a)) for a in accepted],
            ),
        )
        self.hook("after_finalists_bound")

    def decide_cohort(self, round_number: int) -> None:
        """Design §6.10, §6.11, §7: compare each finalist, then select (plan choices 4-8).

        Each owed `global_comparison_completed` is written in finalist order; a
        resume writes only the missing ones and never redraws a written one.
        The selection follows. Promotion (or the retained close) is the
        caller's next step in the same action -- `decide_round` calls itself
        and takes the branch every family takes for a durable selection --
        so a crash anywhere after `selection_decided` resumes the same
        transaction and no orderly stop falls inside it (ADR-0008, plan
        choice 8). Rows are cut at the bound R
        (F5 choice 14); a completed member takes no further try, so its live
        rows are its completion prefix's (plan choice 5). The members are the
        current ones: a member a `continue` re-bound is compared under its
        `<id>@e<k>` (F7 plan choice 7).
        """

        self.hook("before_global_comparison")
        snapshot = self.snapshot()
        state = snapshot.rounds[round_number]
        cohort = state.cohort
        assert cohort is not None and self.identity is not None
        assert state.cohort_current is not None
        members = list(current_members(state))
        repeats = int(cohort["repeats"])
        corpus_ids = [str(case["case_id"]) for case in self.identity.corpus_cases]
        for position, finalist in enumerate(cohort["finalists"]):
            if str(finalist["attempt_id"]) in state.global_comparisons:
                continue
            stability = self.policy.stability
            assert stability is not None  # family 14 binds V11's required declaration
            champion = champion_member(cohort, members=members, stability=stability)
            member = finalist_member(cohort, position, members=members, stability=stability)
            stored_champion = None
            excluded: frozenset[tuple[str, int]] = frozenset()
            if stability == "stable":
                entry = snapshot.measurements[member]
                if entry.get("stability") != "stable" or entry.get("champion_side") != "stored":
                    raise CampaignInvariantViolated(
                        "a stable comparison requires its stored binding"
                    )
                stored_champion = entry["stored_champion"]
                champion = str(stored_champion["measurement_id"])
                champion_rows = self.cited_baseline_rows(round_number, champion)
                excluded = self.excluded_pairs(champion, repeats, rows=champion_rows)
                champion_evidence = self.prefix(champion, round_number=round_number)
            else:
                assert champion is not None
                champion_rows = self.measurements[champion].sample_rows(repeats)
                champion_evidence = self.record_reference(
                    int(state.cohort_completed[champion]["seq"])
                )
            try:
                record = global_comparison_record(
                    cohort,
                    state.cohort_completed,
                    members=members,
                    position=position,
                    finalist_rows=self.measurements[member].sample_rows(repeats),
                    champion_rows=champion_rows,
                    groups=self.corpus_groups(),
                    corpus_ids=corpus_ids,
                    primary=self.policy.metric_primary,
                    campaign_seed=self.policy.random_seed,
                    family_version=self.family_version,
                    stability=stability,
                    stored_champion=stored_champion,
                    excluded=excluded,
                    removal_evidence=[
                        self.record_reference(int(state.cohort_completed[member]["seq"])),
                        champion_evidence,
                    ],
                )
            except CoverageMissing as exc:
                raise CampaignInvariantViolated(
                    member_diagnostic(
                        "a completed cohort member lacks coverage",
                        family_version=self.family_version,
                    )
                ) from exc
            except GateInvariant as exc:
                raise CampaignInvariantViolated(str(exc)) from exc
            except ValueError as exc:
                raise CampaignInvariantViolated(
                    member_diagnostic(
                        "the cohort comparison refused its own inputs",
                        family_version=self.family_version,
                    )
                ) from exc
            if isinstance(record, MissingMetric):
                raise CampaignInvariantViolated(
                    member_diagnostic(
                        "a completed cohort member's missing primary is a fault row",
                        family_version=self.family_version,
                    )
                )
            self.append(
                "global_comparison_completed",
                {
                    **record,
                    "evidence": [
                        self.record_reference(int(state.cohort_completed[member]["seq"])),
                        champion_evidence,
                    ],
                },
            )
            self.hook("after_global_comparison_completed")
        if not self.cohort_admitted(round_number):
            return
        state = self.snapshot().rounds[round_number]
        if (
            self.policy.round_stop == "first_accepted"
            and (not any(c["accepted"] for c in state.global_comparisons.values()))
            and (len(state.attempts) < self.policy.attempts_per_round)
        ):
            # R37: a settled losing finalist returns to the frozen round's
            # search. The planner opens the next Attempt, without selecting.
            return
        assert self.policy.global_mode is not None  # a family-13 policy has one (v10)
        self.hook("before_selection_decided")
        self.append(
            "selection_decided",
            {
                **cohort_selection(
                    state, self.policy.global_mode, family_version=self.family_version
                ),
                "evidence": [
                    self.record_reference(int(source["seq"])) for source in selection_records(state)
                ],
            },
        )
        self.hook("after_selection_decided")

    async def close_family_13_selection(self, round_number: int) -> None:
        """Plan choice 8 (ADR-0008): a family-13 selection is promoted or closed in
        the action that wrote it, as family 12's is, so no `drive` iteration --
        a cancellation check, a budget stop -- falls between `selection_decided`
        and its Promotion or `round_closed`. Nothing happens when the selection
        is not durable (a cohort was just bound, or admission stopped the run);
        a crash in between is finished by the planner's next `DecideRound`.
        """

        if self.pending is not None:
            return
        if self.snapshot().rounds[round_number].selection is not None:
            await self.decide_round(round_number)

    def bind_cohort_member(self, action: BindCohortMember) -> None:
        """Design §6.8: declare the member, then create its journal (declare before birth)."""

        state = self.snapshot().rounds[action.round]
        cohort = state.cohort
        assert cohort is not None
        position = list(cohort["members"]).index(action.measurement_id)
        stability = self.policy.stability
        role, attempt_id = member_identity(cohort, position, stability=stability)
        if role == "champion":
            digest, side = str(cohort["champion"]["digest"]), "champion"
        else:
            finalist = next(f for f in cohort["finalists"] if f["attempt_id"] == attempt_id)
            digest, side = str(finalist["candidate_digest"]), "candidate"
        measurement = self.derive(action.measurement_id, digest, side)
        stored: dict[str, Any] = {}
        if stability == "stable":
            baseline = str(state.baseline_measurement_id)
            # Read and verify the recorded prefix, never the live journal head.
            # An unavailable stored Champion is a persistence fault (ADR-0032).
            self.cited_baseline_records(action.round, baseline)
            reference = self.prefix(baseline, round_number=action.round)
            stored.update(
                stored_champion=dict(
                    measurement_id=baseline, prefix_digest=reference["content_digest"]
                ),
                evidence=[reference],
            )
        self.append(
            campaign_vocabulary_for(self.family_version).measurement_bound,
            dict(
                round=action.round,
                measurement_id=action.measurement_id,
                role=role,
                attempt_id=attempt_id,
                candidate_digest=digest,
                identity_digest=measurement.identity.digest,
                **(
                    dict(
                        stability=self.policy.stability,
                        round_stop=self.policy.round_stop,
                        champion_side="fresh" if self.policy.stability == "unstable" else "stored",
                    )
                ),
                **stored,
            ),
        )
        self.hook("after_cohort_measurement_bound")
        with _boundary("measurement_journal", action.measurement_id):
            measurement.create()
        self.measurements[action.measurement_id] = measurement
        self.siblings[action.measurement_id] = "born"

    async def cohort_stage(self, action: RunCohortStage) -> None:
        """Design §7: one joint stage of every member, then the cohort barrier (§8).

        The previous stage's barrier runs again first: a crash between a stage
        and its barrier must not let the next stage start past a suspension
        (ADR-0008: the checks run before every step that builds on the rows).
        The stage runs on the members still running (`running_members`, F7
        plan choice 8): no stage opens on a completed member, and a member a
        `continue` re-bound runs every stage again, its carried rows filling them.
        """

        snapshot = self.snapshot()
        members = running_members(snapshot.rounds[action.round])
        unopened = all(len(snapshot.measurements[mid]["stages"]) < action.stage for mid in members)
        # Plan choice 20: only before opening stage k; a re-run of stage k must
        # not re-check stage k - 1 over stage k's own unrepaired rows.
        if (
            action.stage > 1
            and unopened
            and not self.cohort_barrier(action.round, action.stage - 1)
        ):
            return
        outcomes = await self.run_joint_stage(members, action.stage, exam_first=False)
        if not self.cohort_stage_settled(members, action.stage):
            return
        if self.stage_owes_a_try(tuple(zip(members, outcomes, strict=True))):
            # Plan choice 8, as `attempt_stage`: a stage whose rows still owe a
            # try has no barrier consequence; the planner runs it again. Step
            # 3's premise, that a gap owes nothing, rests on this.
            return
        # ADR-0008 as amended: whenever the caller's token is set the run ends
        # `stopped(cancelled)` and step 3 is skipped; the stage it cut short
        # stays unsettled, so its `cancelled` rows are not gaps here. They are
        # gaps only to a resume without the token (a crash before
        # `campaign_finished`), whose barrier runs again before the next
        # cohort step (plan choice 20). Steps 1 and 2 still run.
        self.cohort_barrier(action.round, action.stage, gaps=not self.cancelled())

    def cohort_stage_settled(self, members: tuple[str, ...], stage: int) -> bool:
        """As `attempt_stage`: a stage a refusal left unopened on a member, or a
        member left open by a drift refusal, has no barrier."""

        snapshot = self.snapshot()
        if self.drifted_open.intersection(members):
            return False
        return all(len(snapshot.measurements[mid]["stages"]) >= stage for mid in members)

    def cohort_barrier(self, round_number: int, stage: int, *, gaps: bool = True) -> bool:
        """Design §8, in order: fault rows, thresholds at `cohort_stage`, and
        `cohort_incomplete`. True when nothing suspended.

        `gaps=False` skips step 3 whenever the caller's token is set (the
        run is stopping `cancelled`, `cohort_stage`): ADR-0008 leaves the
        stage the token cut short unsettled, so its rows are no gaps here.

        `attempt_stage` reads `outcome.faults` from the stage it just ran; this
        barrier reads `Measurement.fault_rows` instead, because it also runs
        before the next cohort step, when no outcome is at hand. The two agree
        on a settled stage: a row repaired since the suspension has a later,
        scored try. `stage` names the barrier; the checks read the whole
        journal of every member still running (`running_members`, F7 plan
        choice 8), the members its stages ran on. A completed member owes
        nothing: its own barrier passed before its completion.
        """

        state = self.snapshot().rounds[round_number]
        cohort = state.cohort
        assert cohort is not None
        running = running_members(state)
        for mid in running:
            faults = self.measurements[mid].fault_rows(self.policy.metric_primary)
            if faults:
                # I1 spec §5.4, as `attempt_stage`: the campaign suspends on the
                # fault rows. One member's rows per suspension (plan choice 8),
                # so the audit's one-measurement check holds; `attempt_stage`
                # may put a Candidate's and a fresh Parent's rows in one.
                self.stop(
                    _Pending(
                        "infrastructure_failed",
                        str(faults[0]["reason"]),
                        "none",
                        faults=self.located_faults(mid, faults),
                    )
                )
                return False
        if self.thresholds(
            round_number, campaign_vocabulary_for(self.family_version).stage_barrier
        ):
            return False
        if not gaps:
            return True
        # Design §8 (3), D6, D7 (PDR-0022 / ADR-0027, 2026-10-05): below the
        # thresholds, every gap of the Champion -- a pair the settled stage
        # left without a score: exhausted, `cancelled`, or a try whose retry
        # the cancelled run declined -- and every infrastructure-owned gap of
        # a finalist suspends the cohort. A member has no `case_repeats`, so
        # its horizon is the stage's look (plan choice 10).
        look = int(cohort["looks"][stage - 1])
        snapshot = self.snapshot()
        members = [
            Member(
                mid,
                str(snapshot.measurements[mid]["cohort_role"]),
                self.measurements[mid].sample_rows(look),
                declined_tries(snapshot.retries_declined, mid),
            )
            for mid in running
        ]
        rows = incomplete_rows(members, [case.case_id for case in self.corpus])
        if rows:
            owner = cohort_owner(rows)
            outcome, reason = THRESHOLD_TERMINAL[owner]
            self.stop(
                _Pending(
                    outcome,
                    reason,
                    "none",
                    owner=owner,
                    cohort_rows=cohort_rows_evidence(rows, family_version=self.family_version),
                )
            )
            return False
        return True

    def stage_owes_a_try(self, sides: Sequence[tuple[str, StageOutcome]]) -> bool:
        """Continuable-terminals spec §3.4/§4.1: does a stage just run still owe a try?

        The stage is complete only when every sample scored or exhausted its
        tries. A sample still owed a try (a stage the caller's token cut short)
        leaves the stage for the planner to run again; an exhausted one does not.
        """

        for mid, outcome in sides:
            exhausted = {
                (row.case_id, row.sample_id)
                for row in self.measurements[mid].exhausted_rows(outcome.horizon)
            }
            if any(
                (row["case_id"], row["sample_id"]) not in exhausted for row in outcome.indeterminate
            ):
                return True
        return False

    def complete_cohort_member(self, action: CompleteCohortMember) -> None:
        """Design §6.9: one member's completion, after the last stage's barrier (§8) and D7.

        The barrier runs again first: it writes nothing when nothing suspends,
        and it makes a crash between the last stage and its barrier safe (plan
        choice 20). It suspends on any gap of the Champion, a crash-cut forced
        stop's `cancelled` pair included (2026-10-05), so the Champion always
        completes `complete: true` and the invariant below is unreachable on a
        genuine journal. Never `close_measurement`: a cohort member excludes no pair
        (ADR-0027). The rows are cut at the bound R, so `missing` and
        `critical` count only the pairs `point` samples.
        """

        snapshot = self.snapshot()
        cohort = snapshot.rounds[action.round].cohort
        assert cohort is not None and self.identity is not None
        if not self.cohort_barrier(action.round, len(cohort["looks"])):
            return
        mid = action.measurement_id
        repeats = int(cohort["repeats"])
        result = member_completion(
            self.measurements[mid].sample_rows(repeats),
            groups=self.corpus_groups(),
            corpus=[str(case["case_id"]) for case in self.identity.corpus_cases],
            primary=self.policy.metric_primary,
            repeats=repeats,
        )
        if isinstance(result, MissingMetric):
            raise CampaignInvariantViolated(
                member_diagnostic(
                    "a cohort member's missing primary is a fault row",
                    family_version=self.family_version,
                )
            )
        if snapshot.measurements[mid]["cohort_role"] == "champion" and not result.complete:
            raise CampaignInvariantViolated("an incomplete Champion member suspends at its barrier")
        self.append(
            campaign_vocabulary_for(self.family_version).measurement_completed,
            dict(
                round=action.round,
                measurement_id=mid,
                prefix=str(self.prefix(mid)["locator"]),
                complete=result.complete,
                point=result.point,
                missing=result.missing,
                critical=result.critical,
                evidence=self.attestation_evidence(mid),
            ),
        )
        self.hook("after_cohort_measurement_completed")

    def attestation_evidence(self, measurement_id: str) -> list[dict[str, Any]]:
        """`journal_record` references to the `instance_attested` campaign
        records of a measurement's re-bind chain, in journal order (design
        §6.9 `evidence`).

        F7 plan choice 9: a member a `continue` re-bound may hold only carried
        rows, so it cites every attestation of its `rebound_from` chain -- the
        chain promotion admission reads (`attested_chain`). Each predecessor
        was frozen before its successor was bound, so journal order is oldest
        epoch first. Without a re-bind it is the measurement's own. One pass
        over the log, with `record_reference`'s byte accumulation.
        """

        assert self.log is not None
        chain = frozenset(_rebind_chain(self.snapshot().measurements, measurement_id))
        references: list[dict[str, Any]] = []
        offset = 0
        for record in self.log.snapshot().records:
            encoded = record.encode()
            if record.event == "instance_attested" and record.payload["measurement_id"] in chain:
                references.append(
                    self.decision_reference(_Written(offset, offset + len(encoded), record, None))
                )
            offset += len(encoded)
        return references

    def publish_comparison_data(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Declare exact bytes once, publish through S2, then let the caller consume."""
        import tempfile

        from evolution.tree.artifacts.artifact_payloads import (
            AuxiliaryDeclaration,
            describe_auxiliary_files,
        )
        from evolution.tree.family15_schema import validate_comparison_data

        if (
            any(
                "fallback" in s
                or s.get("tolerance") == "measured_noise"
                or (body.get("kind") == "result" and "threshold" in s)
                for s in body.get("scopes", ())
            )
            or "calibration" in body
            or "calibration_unavailable" in body
        ):
            from evolution.tree.campaign_projections import _evidence_of
            from evolution.tree.family15_audit import Family15Audit

            context, _ = _evidence_of(self.run_root)
            Family15Audit(context).validate_data(dict(body))
        else:
            validate_comparison_data(dict(body))
        if secret_location({"payload": body}, self.secrets) is not None:
            raise RecordMatchesSecret("comparison_data")
        assert self.store is not None and self.log is not None
        if body.get("kind") == "diagnostic_import":
            from evolution.tree.baseline_diagnostics import retain_diagnostic_binding

            retain_diagnostic_binding(self.run_root, body)
        with tempfile.TemporaryDirectory(prefix="comparison-", dir=self.run_root) as directory:
            prepared = Path(directory)
            (prepared / "payload.json").write_bytes(canonical_json(body))
            files = describe_auxiliary_files(prepared, purpose="comparison")
            declaration = dict(
                family_version=15,
                purpose="comparison",
                format="comparison-data/1",
                content_digest=files[0].digest,
                files=[f.to_payload() for f in files],
            )
            old = next(
                (
                    r
                    for r in self.log.snapshot().records
                    if r.event == "auxiliary_payload_declared" and dict(r.payload) == declaration
                ),
                None,
            )
            if old is None:
                written = self.append("auxiliary_payload_declared", declaration)
                reference = self.decision_reference(written)
                self.hook("after_comparison_data_declared")
            else:
                reference = self.record_reference(old.to_payload()["sequence"])
            publication = self.store.publish_auxiliary(
                AuxiliaryDeclaration.from_payload({**declaration, "declaration_ref": reference}),
                prepared,
            )
            self.hook("after_comparison_data_published")
            return publication.to_payload()

    def independent_data(self, record: Any) -> dict[str, Any]:
        from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
        from evolution.tree.family15_audit import authenticate_auxiliary

        body = authenticate_auxiliary(
            self.run_root, AuxiliaryPublication.from_payload(record.payload["data"])
        )
        assert body is not None
        return body

    def independent_measurement(self, mid: str, prefix: Any = None) -> dict[str, Any]:
        measurement = self.measurements[mid]
        return dict(
            measurement_id=mid,
            identity=measurement.identity.to_payload(),
            identity_digest=measurement.identity.digest,
            prefix=None if prefix is None else dict(prefix),
        )

    def bind_independent_comparison(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import build_profile, source_summary

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        state = snapshot.rounds[attempt.round]
        assert attempt.selection is not None and state.gate_scope is not None
        assert attempt.measurement_id is not None
        reference_id = (
            attempt.parent_measurement_id
            if self.policy.fresh_parent
            else state.baseline_measurement_id
        )
        assert reference_id is not None
        focus, causal = confidence_scopes(
            attempt.selection, snapshot.identity["corpus_cases"], state.gate_scope["cases"]
        )
        candidate = self.independent_measurement(
            attempt.measurement_id,
            None
            if extra.gate_candidate is None
            else extra.gate_candidate.payload["source"]["prefix"],
        )
        reference = self.independent_measurement(
            reference_id,
            (None if extra.gate_parent is None else extra.gate_parent.payload["source"]["prefix"])
            if self.policy.fresh_parent
            else state.baseline_reference,
        )
        profile = build_profile(
            policy=self.policy,
            corpus=snapshot.identity["corpus_cases"],
            scopes={
                name: [snapshot.identity["corpus_cases"][i]["case_id"] for i in indices]
                for name, indices in (("focus", focus), ("causal", causal))
            },
            candidate=candidate,
            reference=reference,
            reference_rows=()
            if self.policy.fresh_parent
            else self.cited_baseline_rows(attempt.round, reference_id),
            epoch=len(snapshot.epochs) - 1,
            reference_kind="fresh" if self.policy.fresh_parent else "stored",
        )
        from evolution.tree.case_comparison import preflight

        self.note_independent_report(profile)
        if self.policy.gate_mode != "statistical":
            check = preflight(profile, None)
            if check.impossible:
                self.fail_independent_preflight(attempt_id, check)
                return
        data = self.publish_comparison_data(profile.to_payload())
        self.append(
            "comparison_bound",
            dict(
                round=attempt.round,
                attempt_id=attempt_id,
                epoch=len(snapshot.epochs) - 1,
                data=data,
                candidate=source_summary(candidate),
                reference=source_summary(reference),
                reference_kind="fresh" if self.policy.fresh_parent else "stored",
                **(
                    {
                        "focus": dict(
                            seq=extra.focus_decision.envelope.to_payload()["sequence"],
                            digest=extra.focus_decision.reference.content_digest,
                            outcome="passed",
                        )
                    }
                    if extra.focus_decision is not None
                    else {}
                ),
            ),
        )
        self.hook("after_comparison_bound")

    def fail_independent_preflight(self, attempt_id: str, result: Any = None) -> None:
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None
        if result is None:
            result = self.independent_global_preflights()[attempt.round]
        if not result.impossible:
            raise CampaignInvariantViolated("early-global reference no longer proves impossibility")
        body = result.to_payload()
        assert body is not None
        data = self.publish_comparison_data(body)
        self.append(
            "comparison_preflight_failed",
            dict(
                round=attempt.round,
                attempt_id=attempt_id,
                epoch=body["epoch"],
                phase=body["phase"],
                mode=body["mode"],
                data=data,
                reason="structurally_unattainable",
            ),
        )
        self.hook("after_comparison_preflight_failed")

    def complete_independent_side(self, mid: str) -> None:
        """Settle each owned side before any later suspension/condition adoption."""
        from evolution.tree.case_comparison import admissible, source_summary

        snapshot = self.snapshot()
        attempt = next(
            (
                a
                for r in snapshot.rounds.values()
                for a in r.attempts.values()
                if mid in (a.measurement_id, a.parent_measurement_id)
            ),
            None,
        )
        if attempt is None or attempt.family15 is None:
            return
        extra = attempt.family15
        if extra.comparison_binding is None or snapshot.measurements[mid]["complete"]:
            return
        rows = self.measurements[mid].sample_rows(self.measurements[mid].max_repeat)
        scopes = None
        if isinstance(self.policy, CampaignPolicyV12) and self.policy.focus_first:
            if extra.focus_decision is None:
                return
            scopes = (
                ["focus", "causal"]
                if extra.focus_decision.payload["outcome"] == "passed"
                else ["focus"]
            )
            binding = self.independent_data(extra.comparison_binding)
            cases = {
                case
                for scope in binding["scopes"]
                if scope["name"] in scopes
                for case in scope["declared_cases"]
            }
            rows = tuple(row for row in rows if row.case_id in cases)
        if not rows or not all(admissible(r, self._bound_policy(mid).metric_primary) for r in rows):
            return
        if thresholds_reached(snapshot, self.policy):
            return
        source = source_summary(self.independent_measurement(mid, self.prefix(mid)))
        self.append(
            "gate_measurement_completed",
            dict(
                round=attempt.round,
                attempt_id=attempt.attempt_id,
                binding=extra.comparison_binding.reference.to_payload(),
                side="candidate" if mid == attempt.measurement_id else "parent",
                source=source,
                **({"scopes": scopes} if scopes is not None else {}),
                required=len(rows),
                admissible=len(rows),
            ),
        )
        self.hook("after_gate_measurement_completed")

    def decide_focus(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import ComparisonProfile, evaluate_focus, source_summary

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        assert extra.comparison_binding is not None
        if extra.focus_decision is None:
            body = self.independent_data(extra.comparison_binding)
            candidate_id = body["candidate"]["measurement_id"]
            reference_id = body["reference"]["measurement_id"]
            policy = self._bound_policy(candidate_id)
            profile = ComparisonProfile.from_payload(
                body,
                primary=policy.metric_primary,
                groups={c["case_id"]: c["group_id"] for c in snapshot.identity["corpus_cases"]},
                policy=policy,
            )
            result = evaluate_focus(
                profile,
                self.measurements[candidate_id].sample_rows(
                    self.measurements[candidate_id].max_repeat
                ),
                self.measurements[reference_id].sample_rows(
                    self.measurements[reference_id].max_repeat
                )
                if body["reference_kind"] == "fresh"
                else self.cited_baseline_rows(attempt.round, reference_id),
                candidate_source=source_summary(
                    self.independent_measurement(candidate_id, self.prefix(candidate_id))
                ),
                reference_source=source_summary(
                    self.independent_measurement(reference_id, self.prefix(reference_id))
                )
                if body["reference_kind"] == "fresh"
                else extra.comparison_binding.payload["reference"],
                campaign_seed=policy.random_seed,
            ).to_payload()
            if result["reason"] in {"candidate_incomplete", "reference_incomplete"}:
                raise CampaignInvariantViolated("focus decision lacks owned admissible evidence")
            data = self.publish_comparison_data(result)
            self.append(
                "focus_decided",
                dict(
                    round=attempt.round,
                    attempt_id=attempt_id,
                    binding=extra.comparison_binding.reference.to_payload(),
                    data=data,
                    outcome=result["outcome"],
                    reason=result["reason"],
                    **({"seed": result["bootstrap"]} if "bootstrap" in result else {}),
                ),
            )
            self.hook("after_focus_decided")
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert (
            attempt is not None
            and attempt.family15 is not None
            and attempt.family15.focus_decision is not None
        )
        if attempt.family15.focus_decision.payload["outcome"] != "passed":
            self.evaluate_independent(attempt_id)

    def evaluate_independent(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import (
            ComparisonProfile,
            ComparisonResult,
            evaluate_causal_after_focus,
            evaluate_comparison,
            focus_rejection,
        )

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        assert extra.comparison_binding is not None
        for mid in (attempt.measurement_id, attempt.parent_measurement_id):
            if mid is not None:
                self.complete_independent_side(mid)
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        assert extra.gate_candidate is not None and extra.comparison_binding is not None
        body = self.independent_data(extra.comparison_binding)
        candidate_id, reference_id = (
            body["candidate"]["measurement_id"],
            body["reference"]["measurement_id"],
        )
        reference_rows = (
            self.measurements[reference_id].sample_rows(self.measurements[reference_id].max_repeat)
            if body["reference_kind"] == "fresh"
            else self.cited_baseline_rows(attempt.round, reference_id)
        )
        profile = ComparisonProfile.from_payload(
            body,
            primary=self._bound_policy(candidate_id).metric_primary,
            groups={c["case_id"]: c["group_id"] for c in snapshot.identity["corpus_cases"]},
            policy=self._bound_policy(candidate_id),
            focus_binding_digest=self.independent_data(extra.focus_decision)["binding_digest"]
            if "focus" in extra.comparison_binding.payload and extra.focus_decision is not None
            else None,
        )

        candidate_rows = self.measurements[candidate_id].sample_rows(
            self.measurements[candidate_id].max_repeat
        )
        options: dict[str, Any] = dict(
            candidate_source=extra.gate_candidate.payload["source"],
            reference_source=extra.gate_parent.payload["source"]
            if extra.gate_parent is not None
            else extra.comparison_binding.payload["reference"],
            campaign_seed=self._bound_policy(candidate_id).random_seed,
        )
        if isinstance(self.policy, CampaignPolicyV12) and self.policy.focus_first:
            assert extra.focus_decision is not None
            focus = ComparisonResult(canonical_json(self.independent_data(extra.focus_decision)))
            result = (
                evaluate_causal_after_focus(
                    profile, focus, candidate_rows, reference_rows, **options
                )
                if focus.outcome == "passed"
                else focus_rejection(profile, focus)
            ).to_payload()
        else:
            result = evaluate_comparison(
                profile, candidate_rows, reference_rows, **options
            ).to_payload()
        data = self.publish_comparison_data(result)
        self.append(
            "comparison_completed",
            dict(
                round=attempt.round,
                attempt_id=attempt_id,
                binding=extra.comparison_binding.reference.to_payload(),
                data=data,
                **{
                    k: result[k]
                    for k in ("candidate", "reference", "outcome", "reason", "deciding_scope")
                },
            ),
        )
        self.hook("after_comparison_completed")

    def bind_comparison(self, attempt_id: str) -> None:
        """Family-13 design §6.2, §6.3, G6, G10: bind the Attempt's comparison
        and, when it owes one, its noise calibration (plan choice 2).

        Both are written after the Attempt's measurements are bound and before
        their first stage, each at most once: a resume after a crash between
        the two writes only the missing half, as `bind_baseline` does (#106).
        Every bound value is the policy's in force (PDR-0005) and the epoch is
        the one in force (ADR-0020). `retained` is the baseline's exclusions
        removed from F and C (G6, plan choice 10); a binding takes no draw. A
        comparison that closes the Attempt at binding (G6, D3, R1) owes no
        calibration: the planner's next step is its `owed_close`.

        G11 (plan choices 2-4): after a `continue` re-bound either side of an
        undecided comparison, the superseding binding is the governing one's
        payload with only `supersedes`, the epoch and the two measurement ids
        changed. A fresh calibration it set aside (`superseded_calibration`)
        is then bound again under it, naming the one it supersedes.
        """

        if self.family_version == 15:
            self.bind_independent_comparison(attempt_id)
            return
        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None
        state = snapshot.rounds[attempt.round]
        if attempt.comparison is not None and superseding_comparison_owed(
            attempt, family_version=snapshot.family_version
        ):
            assert self.log is not None
            governing = int(attempt.comparison["seq"])
            self.append(
                "comparison_bound",
                {
                    **self.log.snapshot().records[governing - 1].payload,
                    "supersedes": governing,
                    "epoch": len(snapshot.epochs) - 1,
                    "candidate_measurement_id": attempt.measurement_id,
                    "parent_measurement_id": attempt.parent_measurement_id,
                },
            )
            self.hook("after_comparison_bound")
            snapshot = self.snapshot()
            attempt = self.attempt_state(snapshot, attempt_id)
            assert attempt is not None
        if attempt.comparison is None:
            assert self.log is not None and state.gate_scope is not None
            baseline = str(state.baseline_measurement_id)
            selection_seq = self._record_sequence("affected_cases_selected", attempt=attempt_id)
            if selection_seq is None:
                raise CampaignInvariantViolated("a comparison needs the attempt's selection")
            evidence = [self.record_reference(selection_seq)]
            exclusions: dict[str, Any] | None = None
            excluded_seq = self._record_sequence("pairs_excluded", measurement_id=baseline)
            if excluded_seq is not None:
                record = self.log.snapshot().records[excluded_seq - 1].payload
                exclusions = {
                    "prefix": record["evidence"][0]["locator"],
                    "pairs_digest": record["pairs_digest"],
                    "count": record["count"],
                }
                evidence.append(self.record_reference(excluded_seq))
            excluded = self.excluded_pairs(baseline, self.policy.repeats)
            baseline_schedule = [
                (str(row["case_id"]), int(row["repeat"]))
                for row in self.measurements[baseline].identity.to_payload()["schedule"]
            ]
            self.append(
                "comparison_bound",
                comparison_record(
                    self.policy,
                    attempt=attempt,
                    baseline_measurement_id=baseline,
                    corpus_cases=snapshot.identity["corpus_cases"],
                    exam=state.gate_scope["cases"],
                    epoch=len(snapshot.epochs) - 1,
                    baseline_schedule=baseline_schedule,
                    excluded=excluded,
                    exclusions=exclusions,
                    evidence=evidence,
                    family_version=snapshot.family_version,
                ),
            )
            self.hook("after_comparison_bound")
            snapshot = self.snapshot()
            attempt = self.attempt_state(snapshot, attempt_id)
            assert attempt is not None
            state = snapshot.rounds[attempt.round]
        comparison = attempt.comparison
        assert comparison is not None
        if calibration_owed(comparison) and attempt.calibration is None:
            fresh = comparison["parent_source"] == "fresh_parent"
            self.append(
                "noise_calibration_bound",
                dict(
                    round=attempt.round,
                    attempt_id=attempt_id,
                    comparison=comparison["seq"],
                    supersedes=attempt.superseded_calibration,
                    source_measurement_id=(
                        comparison["parent_measurement_id"]
                        if fresh
                        else comparison["baseline_measurement_id"]
                    ),
                    source=comparison["parent_source"],
                    prefix=None if fresh else state.baseline_prefix,
                    sizes={"first": comparison["repeats"], "second": comparison["repeats"]},
                    method=dict(comparison["method"]),
                ),
            )
            self.hook("after_noise_calibration_bound")

    async def calibrate(self, attempt_id: str) -> None:
        """Family-13 design §6.4, G10: complete the Attempt's noise calibration
        (plan choices 6, 11, 12).

        A baseline source reads the round's settled baseline prefix before the
        first stage; a fresh source reads the fresh Parent after its final
        stage, under the same fault and held checks a look runs first, so a
        stage a repair may still fill is never calibrated. The mask is the
        baseline's exclusions on both sources (plan choice 10). A written
        result is never recomputed (ADR-0027): the planner proposes this step
        only while `calibration_result` is unset.
        """

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.comparison is not None
        calibration = attempt.calibration
        assert calibration is not None and attempt.calibration_result is None
        comparison = attempt.comparison
        source = str(calibration["source_measurement_id"])
        final = max(int(look) for look in comparison["looks"])
        if calibration["source"] == "baseline":
            prefix = str(calibration["prefix"])
            # Plan choice 12: live rows are the bound prefix's rows only while
            # the completed baseline journal has not grown past it.
            if self.prefix(source)["locator"] != prefix:
                raise CampaignInvariantViolated("the baseline grew after its settled prefix")
            horizon = int(comparison["repeats"])
        else:
            if await self._look_prelude(attempt_id, final):
                return
            prefix = str(self.prefix(source)["locator"])
            horizon = self._gate_inputs(
                comparison, final, self.measurements[source].max_repeat, masked=False
            ).horizon
        baseline = str(comparison["baseline_measurement_id"])
        try:
            record = calibration_record(
                comparison,
                calibration,
                parent_rows=self.measurements[source].sample_rows(horizon),
                excluded=self.excluded_pairs(baseline, int(comparison["repeats"])),
                groups=self.corpus_groups(),
                corpus_ids=[str(case["case_id"]) for case in snapshot.identity["corpus_cases"]],
                primary=self._bound_policy(source).metric_primary,
                horizon=horizon,
                prefix=prefix,
                campaign_seed=self._bound_policy(source).random_seed,
            )
        except GateInvariant as exc:
            # Plan F10, as `evaluate_confidence` maps it.
            raise CampaignInvariantViolated(str(exc)) from exc
        except ValueError as exc:
            # The kernel's own refusal, as family 12 maps it.
            raise CampaignInvariantViolated("the gate refused its own inputs") from exc
        if isinstance(record, MissingMetric):
            await self.close_attempt(
                attempt_id, "insufficient_evidence", "scorer_metric_missing", None, None
            )
            return
        self.append("noise_calibration_completed", record)
        self.hook("after_noise_calibration_completed")

    def seal_independent_gate(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import source_summary

        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        assert extra.comparison is not None and extra.gate_candidate is not None
        source = extra.gate_candidate.payload["source"]
        measurement = self.independent_measurement(source["measurement_id"], source["prefix"])
        body = dict(
            format="comparison-data/1",
            kind="gate_seal",
            measurement=measurement,
            slots_digest=hashlib.sha256(
                canonical_json(measurement["identity"]["schedule"])
            ).hexdigest(),
        )
        self.append(
            "candidate_gate_sealed",
            dict(
                round=attempt.round,
                attempt_id=attempt_id,
                data=self.publish_comparison_data(body),
                source=source_summary(measurement),
                slots_digest=body["slots_digest"],
            ),
        )
        self.hook("after_candidate_gate_sealed")

    def independent_completion_import(
        self,
        attempt_id: str,
        measurement: Measurement,
        snapshot: CampaignSnapshot | None = None,
        *,
        condition_changed: Sequence[str] = (),
    ) -> BaselineImport:
        from evolution.tree.candidate_completion import (
            MeasurementBinding,
            prepare_candidate_completion,
        )

        snapshot = self.snapshot() if snapshot is None else snapshot
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        seal = attempt.family15.gate_seal
        assert seal is not None
        body = self.independent_data(seal)
        sealed = body["measurement"]
        mid = sealed["measurement_id"]
        try:
            source = read_frozen_source(
                self.run_root, self.run_id, mid, expected_identity_digest=sealed["identity_digest"]
            )
            return prepare_candidate_completion(
                source,
                MeasurementBinding(
                    measurement.measurement_id,
                    measurement.identity,
                    self.policy.metric_primary,
                    tuple(
                        sorted(
                            set(condition_changed)
                            | set(
                                fields_between(
                                    snapshot,
                                    mid,
                                    measurement.measurement_id
                                    if measurement.measurement_id in snapshot.measurements
                                    else None,
                                )
                            )
                        )
                    ),
                ),
                gate_seal=body,
            )
        except FrozenSourceCorrupt:
            raise self._rebound_corrupt(mid) from None

    def bind_independent_final_prerequisites(self, attempt_id: str) -> None:
        """Bindings shared with final orchestration; no Champion dispatch here."""
        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        state = snapshot.rounds[attempt.round]
        if self.policy.round_stop == "all_attempts" and (
            state.family15 is None or state.family15.finalists is None
        ):
            ids = [
                a.attempt_id
                for a in sorted(state.attempts.values(), key=lambda a: a.index)
                if a.outcome == "finalist"
            ]
            self.append(
                "finalists_bound",
                dict(
                    round=attempt.round,
                    count=len(ids),
                    attempts_digest=hashlib.sha256(canonical_json(ids)).hexdigest(),
                ),
            )
            self.hook("after_finalists_bound")
        snapshot = self.snapshot()
        state = snapshot.rounds[attempt.round]
        owner = None if self.policy.round_stop == "all_attempts" else attempt_id
        if self.policy.stability == "unstable" and not any(
            r.payload["attempt_id"] == owner
            for r in (() if state.family15 is None else state.family15.champions)
        ):
            mid = f"m-r{attempt.round:02d}-final-champion" + (
                "" if owner is None else f"-{attempt.index:02d}"
            )
            measurement = self.derive(mid, snapshot.champion[1], "champion", fresh_final=True)
            finalist_ref = None
            if owner is None:
                assert state.family15 is not None and state.family15.finalists is not None
                finalist_ref = state.family15.finalists.reference.to_payload()
            self.append(
                "champion_final_bound",
                dict(
                    round=attempt.round,
                    attempt_id=owner,
                    finalists=finalist_ref,
                    measurement_id=mid,
                    identity_digest=measurement.identity.digest,
                    candidate_digest=snapshot.champion[1],
                ),
            )
            # The fresh Champion's birth/dispatch belongs to the shared final scheduler.
            self.measurements[mid] = measurement
            self.siblings[mid] = "unborn"
            self.hook("after_champion_final_bound")

    def bind_independent_candidate_final(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import build_profile, source_summary

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        assert extra.gate_seal is not None
        state = snapshot.rounds[attempt.round]
        assert isinstance(self.policy, CampaignPolicyV12)
        assert state.baseline_measurement_id is not None
        assert attempt.closed_seq is not None
        closure = self.record_reference(attempt.closed_seq)
        reuse = self.policy.verification == "reuse"
        mid = f"m-{attempt_id}-{'completion' if reuse else 'final'}"
        if extra.completion_binding is not None:
            assert snapshot.family15 is not None
            mid = snapshot.family15.current_measurements[
                extra.completion_binding.payload["measurement_id"]
            ]
        measurement = self.measurements.get(mid) or self.derive(
            mid, str(attempt.candidate_digest), "candidate", fresh_final=not reuse
        )
        if reuse and extra.completion_binding is None:
            admitted = self.independent_completion_import(attempt_id, measurement)
            destination: dict[str, Any] = dict(
                measurement_id=mid,
                identity=measurement.identity.to_payload(),
                identity_digest=measurement.identity.digest,
                prefix=None,
            )
            imported = {(r["case_id"], r["sample_id"]) for r in admitted.rows}
            missing = [
                s
                for s in destination["identity"]["schedule"]
                if (s["case_id"], s["sample_id"]) not in imported
            ]
            body = dict(
                format="comparison-data/1",
                kind="completion",
                finalist_closure=closure,
                gate_seal=extra.gate_seal.reference.to_payload(),
                destination=destination,
                source=self.independent_data(extra.gate_seal)["measurement"],
                imports=admitted.declaration(),
                rows=list(admitted.rows),
                missing_slots=missing,
            )
            self.append(
                "candidate_completion_bound",
                dict(
                    round=attempt.round,
                    attempt_id=attempt_id,
                    data=self.publish_comparison_data(body),
                    finalist_closure=closure,
                    gate_seal=extra.gate_seal.reference.to_payload(),
                    measurement_id=mid,
                    identity_digest=measurement.identity.digest,
                    pairs_digest=admitted.pairs_digest,
                    missing_count=len(missing),
                    required=admitted.scheduled,
                    imported=admitted.imported,
                ),
            )
            self.completion_rows[mid] = admitted.rows
            self.hook("after_candidate_completion_bound")
            self.hook("before_candidate_completion_birth")
            with _boundary("measurement_journal", mid):
                measurement.create(imported=admitted.rows)
            self.measurements[mid] = measurement
            self.siblings[mid] = "born"
            self.hook("after_candidate_completion_birth")
            attempt = self.attempt_state(self.snapshot(), attempt_id)
            assert attempt is not None and attempt.family15 is not None
            extra = attempt.family15
        else:
            self.measurements[mid] = measurement
        candidate = self.independent_measurement(mid)
        if reuse:
            assert extra.completion_binding is not None
            source_ref = extra.completion_binding.reference.to_payload()
            carried = {
                (r.case_id, r.repeat)
                for r in measurement.sample_rows(measurement.max_repeat)
                if r.imported
            }
            work = [
                s
                for s in candidate["identity"]["schedule"]
                if (s["case_id"], s["repeat"]) not in carried
            ]
        else:
            assert extra.gate_seal is not None
            source_ref = extra.gate_seal.reference.to_payload()
            work = candidate["identity"]["schedule"]
        if self.policy.stability == "stable":
            champion = self.independent_measurement(
                state.baseline_measurement_id, state.baseline_reference
            )
        else:
            round_extra = self.snapshot().rounds[attempt.round].family15
            assert round_extra is not None
            champions = round_extra.champions
            bound = next(r for r in champions if r.payload["attempt_id"] in (None, attempt_id))
            latest = self.snapshot()
            assert latest.family15 is not None
            original = bound.payload["measurement_id"]
            champion = self.independent_measurement(
                latest.family15.current_measurements.get(original, original)
            )
        profile = build_profile(
            policy=self.policy,
            corpus=snapshot.identity["corpus_cases"],
            scopes={"global": [c["case_id"] for c in snapshot.identity["corpus_cases"]]},
            candidate=candidate,
            reference=champion,
            reference_rows=self.cited_baseline_rows(attempt.round, state.baseline_measurement_id)
            if self.policy.stability == "stable"
            else (),
            epoch=len(snapshot.epochs) - 1,
            reference_kind="stored" if self.policy.stability == "stable" else "fresh",
        )
        order = [dict(measurement_id=mid, slot=s) for s in work]
        if self.policy.stability == "unstable":
            order.extend(
                dict(measurement_id=champion["measurement_id"], slot=s)
                for s in champion["identity"]["schedule"]
            )
            order.sort(
                key=lambda r: (r["slot"]["case_id"], r["slot"]["repeat"], r["measurement_id"])
            )
        body = dict(
            format="comparison-data/1",
            kind="final",
            verification=self.policy.verification,
            candidate_source="completion" if reuse else "fresh",
            candidate=candidate,
            champion_side="stored" if self.policy.stability == "stable" else "fresh",
            champion=champion,
            finalist_closure=closure,
            source_ref=source_ref,
            comparison=profile.to_payload(),
            candidate_work=work,
            schedule_order=order,
        )
        self.append(
            "final_measurement_bound",
            dict(
                round=attempt.round,
                attempt_id=attempt_id,
                data=self.publish_comparison_data(body),
                **{
                    k: body[k]
                    for k in (
                        "verification",
                        "candidate_source",
                        "champion_side",
                        "finalist_closure",
                        "source_ref",
                    )
                },
                candidate=source_summary(candidate),
                champion=source_summary(champion),
            ),
        )
        self.hook("after_independent_final_bound")
        if not reuse:
            with _boundary("measurement_journal", mid):
                measurement.create()
            self.siblings[mid] = "born"
            self.hook("after_candidate_final_birth")

    async def advance_candidate_completion(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import admissible, source_summary

        assert isinstance(self.policy, CampaignPolicyV12)
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        if extra.gate_seal is None:
            self.seal_independent_gate(attempt_id)
            return
        if extra.final_binding is None:
            self.bind_independent_final_prerequisites(attempt_id)
            self.bind_independent_candidate_final(attempt_id)
            return
        snapshot = self.snapshot()
        state = snapshot.rounds[attempt.round]
        finalists = [
            a
            for a in sorted(state.attempts.values(), key=lambda a: a.index)
            if a.outcome == "finalist"
            and (self.policy.round_stop == "all_attempts" or a.attempt_id == attempt_id)
        ]
        for finalist in finalists:
            assert finalist.family15 is not None
            if finalist.family15.final_binding is None:
                self.bind_independent_candidate_final(finalist.attempt_id)
                return
        snapshot = self.snapshot()
        assert snapshot.family15 is not None
        state = snapshot.rounds[attempt.round]
        current = snapshot.family15.current_measurements
        bindings = []
        for finalist in finalists:
            ax = state.attempts[finalist.attempt_id].family15
            assert ax is not None and ax.final_binding is not None
            if ax.completion is None and ax.final_completion is None:
                bound = (
                    ax.completion_binding
                    if self.policy.verification == "reuse"
                    else ax.final_binding
                )
                assert bound is not None
                original = ax.final_binding.payload["candidate"]["measurement_id"]
                bindings.append(
                    (current.get(original, original), finalist.attempt_id, "candidate", bound)
                )
        for bound in () if state.family15 is None else state.family15.champions:
            assert state.family15 is not None
            if bound.payload["attempt_id"] not in (None, attempt_id):
                continue
            if any(
                c.payload["binding"] == bound.reference.to_payload()
                for c in state.family15.champion_completions
            ):
                continue
            original = bound.payload["measurement_id"]
            mid = current.get(original, original)
            bindings.append((mid, bound.payload["attempt_id"], "champion", bound))
            if self.siblings.get(mid) == "unborn":
                with _boundary("measurement_journal", mid):
                    self.measurements[mid].create()
                self.siblings[mid] = "born"
        active = tuple(
            mid
            for mid, _, _, _ in bindings
            if snapshot.measurements[mid]["stages"]
            or not all(
                admissible(r, self.policy.metric_primary)
                for r in self.measurements[mid].sample_rows(self.measurements[mid].max_repeat)
            )
        )
        if active:
            self.hook("before_candidate_completion_dispatch")
            if self.policy.stability == "unstable":
                outcomes = await self.run_joint_stage(
                    active, 1, exam_first=False, independent_order=True
                )
            else:
                outcomes = tuple([await self.run_stage(mid, 1) for mid in active])
            if self.pending is not None or not self.cohort_stage_settled(active, 1):
                return
            if self.stage_owes_a_try(tuple(zip(active, outcomes, strict=True))):
                return
        if bindings and not self.candidate_completion_barrier(
            attempt.round, bindings[0][0], members=tuple(mid for mid, _, _, _ in bindings)
        ):
            return
        for mid, owner, side, bound in bindings:
            measurement = self.measurements[mid]
            rows = measurement.sample_rows(measurement.max_repeat)
            required = len(measurement.identity.to_payload()["schedule"])
            if len(rows) != required or not all(
                admissible(r, self.policy.metric_primary) for r in rows
            ):
                return
            source = source_summary(self.independent_measurement(mid, self.prefix(mid)))
            reuse = side == "candidate" and self.policy.verification == "reuse"
            payload = dict(
                round=attempt.round,
                attempt_id=owner,
                binding=bound.reference.to_payload(),
                source=source,
                required=required,
                admissible=len(rows),
            )
            if not reuse:
                payload["side"] = side
            self.append(
                "candidate_completion_completed" if reuse else "final_measurement_completed",
                payload,
            )
            self.hook(
                "after_candidate_completion_completed"
                if reuse
                else "after_final_candidate_completed"
                if side == "candidate"
                else "after_final_champion_completed"
            )
        self.evaluate_independent_global(attempt_id)

    def evaluate_independent_global(self, attempt_id: str) -> None:
        from evolution.tree.case_comparison import ComparisonProfile, evaluate_comparison

        assert isinstance(self.policy, CampaignPolicyV12)

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.family15 is not None
        extra = attempt.family15
        if extra.global_result is not None:
            return
        assert extra.final_binding is not None
        final = self.independent_data(extra.final_binding)
        completion = extra.completion or extra.final_completion
        assert completion is not None
        candidate = completion.payload["source"]
        reference = extra.final_binding.payload["champion"]
        state = snapshot.rounds[attempt.round]
        if self.policy.stability == "unstable":
            assert state.family15 is not None
            champion = next(
                b for b in state.family15.champions if b.payload["attempt_id"] in (None, attempt_id)
            )
            reference = next(
                c.payload["source"]
                for c in state.family15.champion_completions
                if c.payload["binding"] == champion.reference.to_payload()
            )
        cmid, rmid = candidate["measurement_id"], reference["measurement_id"]
        assert attempt.measurement_id is not None
        for mid in (
            attempt.measurement_id,
            cmid,
            *((rmid,) if self.policy.stability == "unstable" else ()),
        ):
            if (
                not self.attested_chain(mid)
                and mid == cmid
                and self.policy.verification == "reuse"
                and all(
                    row.imported
                    for row in self.measurements[mid].sample_rows(self.measurements[mid].max_repeat)
                )
            ):
                continue
            if not self.admitted_measurement(mid):
                self.stop(
                    _Pending(
                        "infrastructure_failed",
                        "attestation_mismatch",
                        "none",
                        suspend_as="target_changed",
                        record=self.target_record(mid, self.refused_in(mid)),
                    )
                )
                return
        profile = ComparisonProfile.from_payload(
            final["comparison"],
            primary=self._bound_policy(final["candidate"]["measurement_id"]).metric_primary,
            groups={c["case_id"]: c["group_id"] for c in snapshot.identity["corpus_cases"]},
            policy=self._bound_policy(final["candidate"]["measurement_id"]),
        )
        result = evaluate_comparison(
            profile,
            self.measurements[cmid].sample_rows(self.measurements[cmid].max_repeat),
            self.cited_baseline_rows(attempt.round, rmid)
            if self.policy.stability == "stable"
            else self.measurements[rmid].sample_rows(self.measurements[rmid].max_repeat),
            candidate_source=candidate,
            reference_source=reference,
            schedules={
                "candidate": self.measurements[cmid].identity.to_payload()["schedule"],
                "reference": self.measurements[rmid].identity.to_payload()["schedule"],
            },
            campaign_seed=self._bound_policy(cmid).random_seed,
        ).to_payload()
        self.append(
            "global_comparison_completed",
            dict(
                round=attempt.round,
                attempt_id=attempt_id,
                binding=extra.final_binding.reference.to_payload(),
                data=self.publish_comparison_data(result),
                accepted=result["outcome"] == "passed",
                **{
                    k: result[k]
                    for k in ("candidate", "reference", "outcome", "reason", "deciding_scope")
                },
            ),
        )
        self.hook("after_global_comparison_completed")

    def candidate_completion_barrier(
        self, round_number: int, mid: str, *, members: tuple[str, ...] = ()
    ) -> bool:
        """Run the ordinary final fault, threshold and gap owners on every replay."""
        from evolution.tree.confidence_cohort import cohort_gap, gap_owner

        members = members or (mid,)
        for member in members:
            faults = self.measurements[member].fault_rows(self.policy.metric_primary)
            if faults:
                self.stop(
                    _Pending(
                        "infrastructure_failed",
                        str(faults[0]["reason"]),
                        "none",
                        faults=self.located_faults(member, faults),
                    )
                )
                return False
        if self.thresholds(round_number, "final_stage", mid):
            return False
        if self.cancelled():
            return False
        snapshot = self.snapshot()
        gaps = [
            dict(
                measurement_id=member,
                case_id=row.case_id,
                sample_id=row.sample_id,
                retry_ordinal=row.retry_ordinal,
                owner=gap_owner(row),
            )
            for member in members
            for row in self.measurements[member].sample_rows(self.measurements[member].max_repeat)
            if cohort_gap(row, declined_tries(snapshot.retries_declined, member))
        ]
        if gaps:
            owner = cohort_owner(gaps)
            outcome, reason = THRESHOLD_TERMINAL[owner]
            self.stop(
                _Pending(
                    outcome,
                    reason,
                    "none",
                    owner=owner,
                    cohort_rows=cohort_rows_evidence(gaps, family_version=15),
                )
            )
            return False
        return True

    def completion_import(
        self, snapshot: CampaignSnapshot, measurement_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        """G2 spec §5: an owed completion copy, re-read from its sealed source, verified.

        The source cannot move, so a re-read that does not reproduce the
        declared `imported` and `pairs_digest` is corruption -- the hard
        `rebound_source_corrupt` before this invocation's first write, a
        measurement-journal fault after it -- never an authorizable loss.
        """

        cached = self.completion_rows.get(measurement_id)
        if cached is not None:
            return cached
        entry = snapshot.measurements[measurement_id]
        assert entry.get("role") == "family15_completion"
        attempt = self.attempt_state(snapshot, str(entry["attempt_id"]))
        assert attempt is not None and attempt.family15 is not None
        binding = attempt.family15.completion_binding
        assert binding is not None
        admitted = self.independent_completion_import(
            attempt.attempt_id, self.derive_bound(snapshot, measurement_id), snapshot
        )
        if (admitted.imported, admitted.pairs_digest) != (
            binding.payload["imported"],
            binding.payload["pairs_digest"],
        ):
            raise self._rebound_corrupt(measurement_id)
        self.completion_rows[measurement_id] = admitted.rows
        return admitted.rows

    async def select_attempt_cases(self, attempt_id: str) -> None:
        """G2 spec §4.4, §4.5: freeze the affected set before anything is bound.

        In component mode the Candidate is inspected first, unless an earlier
        invocation already journaled its `candidate_inspected`: every
        inspection boots a Target instance. A failed inspection falls back to
        all cases (`inspection_failed`).
        """

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.candidate_digest is not None
        assert self.store is not None
        state = snapshot.rounds[attempt.round]
        # The identity in force, as the lifecycle reads it (`identity_rebased`
        # moves it); its corpus is the one the record's indices name.
        kind = str(snapshot.identity["policy"]["gate"]["provenance"]["kind"])
        corpus_ids = tuple(str(case["case_id"]) for case in snapshot.identity["corpus_cases"])
        assert state.baseline_measurement_id is not None
        # The published copies are read inside the artifact boundary, as
        # `candidate_leak` reads them: a bare `OSError` from the scan or a
        # file read is the store's own `io_failure`, so `_boundary` classifies
        # it as `persistence_failed`. An `ArtifactUnsafeError`
        # is left to `_boundary` too: a published copy that no longer has the
        # shape it was sealed with is a store fault here, not a seal refusal.
        with _boundary("artifact_store"):
            parent = self.store.published_ref("champion", attempt.parent_version)
            child = self.store.published_ref("candidate", attempt_id)
            try:
                changes = tree_changes(parent.root, child.root, self.artifact_policy)
            except OSError as exc:
                raise ArtifactError("io_failure") from exc
        baseline, known = baseline_inputs(
            self.cited_baseline_records(attempt.round, state.baseline_measurement_id),
            self.run_id,
            self.same_run_records,
        )
        candidate_map = attempt.inspected_components
        failed = False
        if kind == "reported" and attempt.inspections == 0:
            mode, _digest, fallback = parent_mode(baseline, known)
            if mode == "component" and fallback is None:
                inspection = attempt.inspections + 1
                async with self.connection_lock:
                    pass
                try:
                    # §7 / ruling 11: the inspection journal is an S1 journal,
                    # so its storage faults are classified as a measurement
                    # journal's are. It is not a bound measurement, so the
                    # fault names none (`capacity_exhausted` refuses an
                    # unbound `measurement_id`).
                    with _boundary("measurement_journal"):
                        candidate_map = await inspect_candidate(
                            run_root=self.run_root,
                            campaign_run_id=self.run_id,
                            attempt_id=attempt_id,
                            inspection=inspection,
                            identity=self.derive(
                                measurement_id_for(attempt_id),
                                attempt.candidate_digest,
                                "candidate",
                            ).identity,
                            corpus=self.corpus,
                            secrets=self.secrets,
                            adapter=self.adapter,
                            candidate=child,
                            strategy=self.strategy,
                            call_attempts=self.call_attempts(),
                            admitted_versions=self.admitted_versions,
                        )
                except InspectionFailed:
                    failed = True
                else:
                    self.append(
                        "candidate_inspected",
                        dict(
                            round=attempt.round,
                            attempt=attempt_id,
                            inspection=inspection,
                            components=candidate_map,
                        ),
                    )
                    self.hook("after_candidate_inspected")
        selection = select_affected(
            kind=kind,
            corpus_ids=corpus_ids,
            baseline=baseline,
            known=known,
            changes=changes,
            labels=self.adaptation_in_force().provenance_labels,
            candidate_map=candidate_map,
            inspection_failed=failed,
        )
        self.append(
            "affected_cases_selected",
            selection.payload(attempt.round, attempt_id, kind, corpus_ids),
        )
        self.hook("after_affected_cases_selected")

    async def release_inspection_leftovers(self) -> None:
        """Release what an interrupted inspection left, whatever the Attempt does next.

        Final review Important 1: a crash inside an inspection leaves journal
        `inspections/<attempt>-i<k>` owning a Target instance, and only a
        re-inspection of the same `(attempt, k)` used to release it. An
        Attempt that closes, or a campaign that stops, before that would leak
        it. So every family-12 open releases the instances of each journal no
        `candidate_inspected` cites, under the journal's own stored identity
        (the condition may have moved since). A journal a `candidate_inspected`
        cites is never touched: the audit reads it, and its release already
        ran or is a refusal the Target made. A release the Target refuses, or
        a journal that cannot be reopened (a version the run never admitted, a
        corpus it no longer binds), stays owned and unreported -- the known
        limitation of the G2b2 note. Nothing is removed here; `inspect_candidate`
        replaces a released journal when the Attempt inspects again.
        """

        root = self.run_root / INSPECTIONS_DIR
        if not root.is_dir():
            return
        snapshot = self.snapshot()
        for directory in sorted(root.iterdir()):
            attempt_id, separator, ordinal = directory.name.rpartition("-i")
            if not separator or not ordinal.isdecimal():
                continue
            inspection = int(ordinal)
            if directory != inspection_path(self.run_root, attempt_id, inspection):
                continue
            attempt = self.attempt_state(snapshot, attempt_id)
            if attempt is not None and attempt.inspections >= inspection:
                continue
            with _boundary("measurement_journal"):
                try:
                    if not inspection_born(directory):
                        continue
                    await release_leftover(
                        directory,
                        directory.name,
                        self.corpus,
                        self.secrets,
                        self.adapter,
                        self.admitted_versions,
                    )
                except IdentityError:
                    continue

    def adaptation_in_force(self) -> AdaptationIdentityV1:
        """The adaptation identity the journal holds in force now (family 8+).

        Read from the journal rather than `self.adaptive`, through the same
        `adaptation_position` the audit's `_Evidence.adaptation_identity`
        uses, so a selection's labels are the ones its re-derivation reads.
        """

        assert self.log is not None
        records = self.log.snapshot().records
        return AdaptationIdentityV1.from_payload(
            records[adaptation_position(records)].payload["adaptation_payload"]
        )

    def call_attempts(self) -> int | None:
        """`call_retries.max_attempts`, or `None` before `/8` (as `Measurement` reads it)."""

        retries = self.policy.budget.call_retries
        return None if retries is None else retries.max_attempts

    def measurement_records(self, measurement_id: str) -> tuple[Envelope, ...]:
        """A bound measurement's S1 records, read without a lease."""

        with _boundary("measurement_journal", measurement_id):
            return inspect_journal(Measurement.directory_for(self.run_root, measurement_id)).records

    def cited_baseline_records(
        self, round_number: int, measurement_id: str
    ) -> tuple[Envelope, ...]:
        """The round's baseline as its `baseline_complete` cites it (§4.4).

        `cited_baseline_prefix` is the audit's reading too (final review
        Minor 2), so the selection and its re-derivation read one bound. A
        citation that does not read back is the baseline journal's fault.
        """

        assert self.log is not None

        def read_file(relative: str) -> bytes:
            try:
                return (self.run_root / relative).read_bytes()
            except OSError as exc:
                raise _journal_fault(exc) from exc

        with _boundary("measurement_journal", measurement_id):
            try:
                if self.family_version == 14:
                    reference = self.prefix(measurement_id, round_number=round_number)
                    self.verify_prefix_bytes(reference)
                return cited_baseline_prefix(
                    self.log.snapshot().records, round_number, measurement_id, read_file
                )
            except (BaselineUnavailable, ReaderIntegrityError) as exc:
                raise JournalFault("journal_unavailable") from exc

    def cited_baseline_rows(self, round_number: int, measurement_id: str) -> tuple[SampleRow, ...]:
        """All rows of the immutable baseline citation, with carried provenance intact.

        `latest_rows` resolves `sample_imported` scores and provenance from the
        cited records. No current measurement head or epoch substitutes for
        that evidence. The full horizon also verifies the exclusion summary.
        """

        records = self.cited_baseline_records(round_number, measurement_id)
        state = Lifecycle()
        for record in records:
            state.accept(record)
        target = records[0].payload["identity"]["target"]
        return latest_rows(
            records,
            state,
            seed_mode=str(target["seed_mode"]),
            dependency_digest=str(target["dependency_digest"]),
        )

    def same_run_records(self, measurement_id: str) -> tuple[Envelope, ...] | None:
        """`measurement_records` of a born measurement of this run, else `None`."""

        if measurement_id not in self.snapshot().measurements:
            return None
        if self.siblings.get(measurement_id) != "born":
            return None
        return self.measurement_records(measurement_id)

    def drift_reference(self, attempt: AttemptState, side: str) -> DriftReference | None:
        """G2b3b: the reference map and epoch one side of `attempt` relied on.

        The Candidate's is its inspection's; the Parent's is the one the
        round's baseline rows name, read as `select_attempt_cases` reads them,
        with the epochs of the measurements declaring it. `None` for an
        Attempt that did not select in component mode without a fallback.
        Cached for the session: both inputs are durable once the selection is.
        """

        key = (attempt.attempt_id, side)
        if key not in self.drift_references:
            reference: DriftReference | None = None
            if side == "candidate":
                reference = candidate_reference(attempt)
            elif checked_selection(attempt):
                snapshot = self.snapshot()
                baseline_id = snapshot.rounds[attempt.round].baseline_measurement_id
                assert baseline_id is not None
                reference = baseline_parent_reference(
                    snapshot,
                    attempt,
                    baseline_id,
                    self.cited_baseline_records(attempt.round, baseline_id),
                    self.same_run_records,
                )
            self.drift_references[key] = reference
        return self.drift_references[key]

    def champion_drift_reference(self, round_number: int) -> DriftReference | None:
        """F7 plan choice 10: the reference of round `round_number`'s Champion member.

        `champion_reference` over the round's cited baseline and its same-run
        sources, the reading `drift_reference` gives a Parent, with no
        Attempt. Cached for the session: the baseline is complete before the
        cohort binds.
        """

        key = (str(round_number), "champion")
        if key not in self.drift_references:
            snapshot = self.snapshot()
            baseline_id = snapshot.rounds[round_number].baseline_measurement_id
            assert baseline_id is not None
            self.drift_references[key] = champion_reference(
                snapshot,
                round_number,
                baseline_id,
                self.cited_baseline_records(round_number, baseline_id),
                self.same_run_records,
            )
        return self.drift_references[key]

    async def attempt_stage(self, attempt_id: str, stage: int) -> None:
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None and attempt.measurement_id is not None
        focus_first = isinstance(self.policy, CampaignPolicyV12) and self.policy.focus_first
        if self.family_version == 15:
            active = tuple(
                mid
                for mid in (attempt.measurement_id, attempt.parent_measurement_id)
                if mid is not None and not self.snapshot().measurements[mid]["complete"]
            )
            if not active:
                return
            measurement_ids = active
            outcomes = (
                (await self.run_stage(active[0], stage),)
                if len(active) == 1
                else await self.run_joint_stage(active, stage, exam_first=not focus_first)
            )
            if focus_first and any(
                len(self.snapshot().measurements[mid]["stages"]) < stage for mid in active
            ):
                return
        elif attempt.parent_measurement_id is None:
            measurement_ids = (attempt.measurement_id,)
            outcomes = (await self.run_stage(attempt.measurement_id, stage),)
        else:
            # G2 spec §4.6: a family-12 Attempt with a fresh Parent measures
            # both sides in one stage; every rule below reads both of them,
            # the critical veto apart, which is the Candidate's alone.
            measurement_ids = (attempt.measurement_id, attempt.parent_measurement_id)
            outcomes = await self.run_joint_stage(measurement_ids, stage)
            snapshot = self.snapshot()
            if any(len(snapshot.measurements[mid]["stages"]) < stage for mid in measurement_ids):
                # A refusal kept one side from opening this stage (the campaign
                # is stopping on it): the stage is unfinished, so no look of it
                # is read and nothing closes over one side's rows alone. The
                # planner returns here once the target is repaired.
                return
        if self.drifted_open.intersection(measurement_ids):
            # A drift refusal left a side's opened stage open (G2b3b,
            # PDR-0020): as above, nothing is read or closed over it.
            return
        sides = tuple(zip(measurement_ids, outcomes, strict=True))
        faulted = [(mid, outcome) for mid, outcome in sides if outcome.faults]
        if faulted:
            # I1 spec §5.4: a fault-row suspension must be able to make
            # progress, so the Attempt is not closed over its faults -- a
            # closed Attempt could never receive the repair try. It stays open
            # at its completed stage, and the campaign suspends on the stage's
            # fault rows exactly as a baseline stage does.
            self.stop(
                _Pending(
                    "infrastructure_failed",
                    str(faulted[0][1].faults[0]["reason"]),
                    "none",
                    faults=tuple(
                        fault
                        for mid, outcome in faulted
                        for fault in self.located_faults(mid, outcome.faults)
                    ),
                )
            )
            return
        if faulted:
            await self.close_attempt(
                attempt_id,
                "insufficient_evidence",
                str(faulted[0][1].faults[0]["reason"]),
                None,
                None,
            )
            return
        if self.stage_owes_a_try(sides):
            # Continuable-terminals spec §3.4/§4.1: a sample still owed a try
            # leaves the Attempt open for the resumed stage; an exhausted one
            # closes it below exactly as in family 5 -- an Attempt never excludes.
            return
        if any(outcome.indeterminate for _mid, outcome in sides):
            if decides_at_once(self.policy):
                # #91 spec §3.1: one active Attempt, no concurrent stage --
                # decided at once, before the next Attempt opens or freezes input.
                await self.decide_held(attempt_id, outcomes[0].look)
            # Otherwise held: the round barrier decides it (ADR-0008).
            return
        if focus_first and stage == 1:
            self.hook("after_focus_stage")

    async def decide_held(self, attempt_id: str, look: int) -> None:
        """#91 spec §3.1: the threshold first; below it, bar the look and close.

        Called with no other Attempt stage in flight: under sequential feedback or
        first-accepted stopping, right after the held Attempt's stage (or its
        `EvaluateLook` on a resume).
        A crossing suspends the campaign with the Attempt open.
        """

        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None
        if self.thresholds(attempt.round, "round"):
            return
        self.bar_look(attempt_id, look)
        await self.close_attempt(
            attempt_id, "insufficient_evidence", "incomplete_sample", None, None
        )

    def bar_look(self, attempt_id: str, look: int) -> bool:
        """Write `look_not_started` when the look's condition fails (spec §8).

        Returns whether the look was barred. A look the attempt already evaluated
        or already barred is left alone: the lifecycle refuses a second record for
        it, and re-reading a durable prefix must never produce one.
        """

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.measurement_id is not None
        if look in attempt.looks or look in attempt.not_started:
            return False
        missing = self.look_missing(attempt_id, look)
        if not missing:
            return False
        if self.family_version == 15:
            # Owned operational gaps close/suspend through their existing owner;
            # family 15 has no interim-look record or statistical gap terminal.
            return True
        evidence = [self.prefix(self.baseline_of(attempt)), self.prefix(attempt.measurement_id)]
        if attempt.parent_measurement_id is not None:
            # G2 spec §4.6: a family-12 look cites the fresh Parent too.
            evidence.append(self.prefix(attempt.parent_measurement_id))
        self.append(
            "look_not_started",
            dict(attempt_id=attempt_id, look=look, missing=missing, evidence=evidence),
        )
        return True

    def look_missing(self, attempt_id: str, look: int) -> list[dict[str, Any]]:
        """The rows `look` lacks on each side: `look_not_started.missing`, or `[]`.

        Empty exactly when the look is evaluable. `bar_look` writes the list.
        """

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.measurement_id is not None
        if self.family_version == 15:
            return [
                missing
                for mid in (attempt.measurement_id, attempt.parent_measurement_id)
                if mid is not None
                for missing in _missing_rows(self.measurements[mid].sample_rows(look), mid, look)
            ]
        assert attempt.comparison is not None
        measurements = {
            mid: self.measurements[mid]
            for mid in (attempt.measurement_id, attempt.parent_measurement_id)
            if mid is not None
        }
        bound = self._gate_inputs(
            attempt.comparison,
            look,
            max(m.max_repeat for m in measurements.values()),
        )
        return confidence_look_missing(
            attempt.comparison,
            {mid: m.sample_rows(bound.horizon) for mid, m in measurements.items()},
            corpus_ids=[str(case["case_id"]) for case in snapshot.identity["corpus_cases"]],
            excluded=bound.mask,
            horizon=bound.horizon,
        )

    def corpus_groups(self) -> dict[str, tuple[str, str]]:
        """`case_id -> (group_id, stratum)` of the identity in force, as the audit builds it."""

        assert self.identity is not None
        return case_groups(self.identity.corpus_cases)

    async def evaluate_confidence(self, attempt_id: str, look: int) -> None:
        """Family 13's look (design §6.5, §6.6; plan choices 4, 8, 9, 10).

        The shared prelude ran and the look was not barred, so every pair the
        comparison owes at `look` is scored: a coverage gap is a defect, never
        an infrastructure close. `look_record` builds the payload, the
        critical veto included (a look record, never a direct close); the
        planner then writes the close it owes (`owed_close`). No alpha moves.
        """

        snapshot = self.snapshot()
        attempt = self.attempt_state(snapshot, attempt_id)
        assert attempt is not None and attempt.comparison is not None
        comparison = attempt.comparison
        candidate = str(comparison["candidate_measurement_id"])
        parent_side = str(
            comparison["parent_measurement_id"] or comparison["baseline_measurement_id"]
        )
        references = [self.prefix(candidate), self.prefix(parent_side)]
        try:
            record = look_record(
                comparison,
                attempt.calibration_result,
                look=look,
                candidate_rows=self.measurements[candidate].sample_rows(look),
                parent_rows=self.measurements[parent_side].sample_rows(look),
                excluded=self.excluded_pairs(
                    str(comparison["baseline_measurement_id"]),
                    max(int(entry) for entry in comparison["looks"]),
                ),
                groups=self.corpus_groups(),
                corpus_ids=[str(case["case_id"]) for case in snapshot.identity["corpus_cases"]],
                primary=self._bound_policy(candidate).metric_primary,
                prefixes={
                    "candidate": str(references[0]["locator"]),
                    "parent": str(references[1]["locator"]),
                },
                campaign_seed=self._bound_policy(candidate).random_seed,
                family_version=snapshot.family_version,
            )
        except CoverageMissing as exc:
            raise CampaignInvariantViolated("an unbarred family-13 look lacks coverage") from exc
        except GateInvariant as exc:
            # Plan F10: the pure module cannot raise the executor's error.
            raise CampaignInvariantViolated(str(exc)) from exc
        except ValueError as exc:
            # The kernel's own refusal, as family 12 maps it.
            raise CampaignInvariantViolated("the gate refused its own inputs") from exc
        if isinstance(record, MissingMetric):
            await self.close_attempt(
                attempt_id, "insufficient_evidence", "scorer_metric_missing", None, None
            )
            return
        self.append("comparison_look_evaluated", {**record, "evidence": references})
        self.hook("after_comparison_look_evaluated")

    async def _look_prelude(self, attempt_id: str, look: int) -> bool:
        """The checks every look runs before it is decided, and a fresh noise
        calibration before it reads the Parent's final stage (plan choice 6).

        True when the step must stop: the run suspended on fault rows, or the
        Attempt is held (or an immediate-decision policy decided it held) on a stage
        a repair may still fill (PDR-0015).
        """

        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None
        if self.stage_admission(attempt, look).faults:
            faults = self.attempt_fault_rows(attempt)
            assert faults, "the stage's fault rows are its measurements' own"
            self.stop(
                _Pending("infrastructure_failed", str(faults[0]["reason"]), "none", faults=faults)
            )
            return True
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None
        if self.lacking_at(attempt, look):
            # Ruling R13: exhausted rows hold the Attempt, whether or not
            # the look lacks pairs; from 0.3.0a27 (#120) so does any other
            # row the settled stage left unscored (`cancelled` by a forced
            # stop): the threshold is checked before the look is barred.
            if decides_at_once(self.policy):
                await self.decide_held(attempt_id, look)
            elif attempt_id not in self.lacking_attempts():
                # Ruling R16: the planner would plan this look again forever.
                raise CampaignInvariantViolated("an evaluated look is held but not lacking")
            # Otherwise held; the planner gives it no step (#91 spec §3.1).
            return True
        return False

    async def evaluate(self, attempt_id: str, look: int) -> None:
        if await self._look_prelude(attempt_id, look):
            return
        if self.family_version == 15:
            self.evaluate_independent(attempt_id)
            return
        if self.bar_look(attempt_id, look):
            return
        await self.evaluate_confidence(attempt_id, look)
        return

    async def close_attempt(
        self,
        attempt_id: str,
        outcome: str,
        reason: str,
        look: int | None,
        code: str | None,
    ) -> None:
        if outcome == "abandoned" and reason != "seal_refused":
            self.abandon(attempt_id)
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        extra = None if attempt is None else attempt.family15
        result = None if extra is None else extra.preflight or extra.comparison
        if self.family_version == 15 and result is not None:
            assert extra is not None
            self.append(
                "attempt_closed",
                dict(
                    attempt_id=attempt_id,
                    outcome=outcome,
                    reason=reason,
                    path=extra.path,
                    evidence=result.reference.to_payload(),
                ),
            )
        else:
            self.append(
                "attempt_closed",
                dict(attempt_id=attempt_id, outcome=outcome, reason=reason, look=look, code=code),
            )
        self.hook("after_attempt_closed")
        released: tuple[str | None, ...] = (measurement_id_for(attempt_id),)
        attempt = self.attempt_state(self.snapshot(), attempt_id)
        assert attempt is not None
        released = (attempt.measurement_id, attempt.parent_measurement_id)
        for measurement_id in released:
            if measurement_id is not None and measurement_id in self.measurements:
                await self.release(measurement_id)

    # -- the round barrier ------------------------------------------------

    def refused_instance(self, winner: AttemptState) -> str | None:
        """The first attested instance of `winner` that `admitted` refuses, if any."""

        assert winner.measurement_id is not None
        return self.refused_in(winner.measurement_id)

    def refused_in(self, measurement_id: str) -> str | None:
        """The first attested instance behind `measurement_id` that R3's verifier refuses."""

        entries = self.attested_chain(measurement_id)
        if not entries:
            return None
        expected = self.candidate_ref(measurement_id).expected_attestation()
        for entry in entries:
            try:
                verify_attestation(TargetIdentity.from_payload(entry["identity"]), expected)
            except (AttestationError, ValueError):
                return str(entry["instance_id"])
        return None

    def admitted(self, winner: AttemptState) -> bool:
        """Spec §8 promotion admission: R3's verifier over the durable identities.

        ADR-0007 assigns C1 a second, independent attestation check at the
        promotion boundary, and it must be the same verifier materialization
        used — not a hand-rolled digest comparison, which would miss the
        required-attested-set half. Every `instance_attested` row of the winner's
        measurement carries the full `TargetIdentity` the target reported, so the
        check reads only durable records and a resumed campaign reaches the same
        verdict. A measurement that attested nothing is not admitted (ruling 4).
        """

        assert winner.measurement_id is not None
        return self.admitted_measurement(winner.measurement_id)

    def admitted_measurement(self, measurement_id: str) -> bool:
        """`admitted` over one measurement's attested chain (family 13 reads two per finalist)."""

        if not self.attested_chain(measurement_id):
            return False
        return self.refused_in(measurement_id) is None

    def cohort_admitted(self, round_number: int) -> bool:
        """Design §7 "Attestation" (plan choice 7): every finalist's gate chain
        and cohort chain pass promotion admission before `selection_decided`.

        In finalist order, the gate measurement and then the cohort member.
        The first refusal is the existing promotion-admission suspension
        (`target_changed`, `stage_record` evidence naming the refused
        instance's record), never an ineligibility. The Champion member is the
        reference and is not checked, as no baseline is. A member a `continue`
        re-bound is checked under its current id over its whole re-bind chain
        (`attested_chain`).
        """

        state = self.snapshot().rounds[round_number]
        cohort = state.cohort
        assert cohort is not None and state.cohort_current is not None
        for finalist, member in selection_finalists(state):
            gate = state.attempts[str(finalist["attempt_id"])].measurement_id
            assert gate is not None
            for measurement_id in (gate, member):
                if self.admitted_measurement(measurement_id):
                    continue
                self.stop(
                    _Pending(
                        "infrastructure_failed",
                        "attestation_mismatch",
                        "none",
                        evidence=tuple(self.selection_evidence(round_number, None)),
                        suspend_as="target_changed",
                        record=self.target_record(measurement_id, self.refused_in(measurement_id)),
                    )
                )
                return False
        return True

    def attested_chain(self, measurement_id: str) -> tuple[Mapping[str, Any], ...]:
        """The durable `instance_attested` rows behind a measurement's rows.

        I2 spec §5.3: a measurement re-bound under `continue` holds rows other
        instances served -- those of the frozen measurements it was re-bound
        from -- and may hold nothing but carried rows. Each of those instances
        loaded the same Candidate, so promotion admission reads them all.
        """

        measurements = self.snapshot().measurements
        return tuple(
            instance
            for mid in _rebind_chain(measurements, measurement_id)
            for instance in measurements[mid]["instances"]
        )

    def deciding_measurement(self, winner: AttemptState) -> str:
        """The measurement in force when `winner`'s deciding look was written (R19).

        I2 spec §5.2: a `continue` re-binds an accepted Attempt, and a carry
        that drops rows leaves the re-bound measurement without the rows that
        decided it. The selection cites the rows its deciding `look_evaluated`
        read, so the re-bind chain is walked back past every
        `measurement_rebound` written after that look.
        """

        assert self.log is not None and winner.measurement_id is not None
        records = self.log.snapshot().records
        deciding = None
        rebound: dict[str, tuple[int, str]] = {}
        for index, record in enumerate(records):
            payload = record.payload
            if (
                record.event == "look_evaluated"
                and payload["attempt_id"] == winner.attempt_id
                and int(payload["look"]) == winner.look
            ):
                deciding = index
            elif record.event == "measurement_rebound":
                rebound[str(payload["measurement_id"])] = (
                    index,
                    str(payload["from_measurement_id"]),
                )
        current = winner.measurement_id
        if deciding is None:
            return current
        while current in rebound and rebound[current][0] > deciding:
            current = rebound[current][1]
        return current

    def selection_evidence(self, round_number: int, winner: AttemptState | None) -> list[Any]:
        snapshot = self.snapshot()
        state = snapshot.rounds[round_number]
        if winner is not None and winner.measurement_id is not None and winner.look is not None:
            measurement = self.measurements[self.deciding_measurement(winner)]
            evidence = [
                measurement.record_reference(row.case_id, row.sample_id).to_payload()
                for row in measurement.sample_rows(winner.look)
                if row.disposition == "score"
            ]
            published = self.publication_reference(winner.attempt_id)
            if published is not None:
                evidence.append(published)
            return evidence
        evidence = [self.prefix(str(state.baseline_measurement_id))]
        for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
            if attempt.measurement_id in self.measurements:
                evidence.append(self.prefix(str(attempt.measurement_id)))
        return evidence

    def adaptive_coverage(
        self, state: Any, measurements: Mapping[str, Measurement] | None = None
    ) -> dict[str, Any]:
        measurements = self.measurements if measurements is None else measurements
        portion = state.focus_portion or {}
        diagnosis = state.diagnosis(self.resilience)
        claimed = (
            set()
            if diagnosis is None or editor_input_reason(diagnosis) is not None
            else {case for group in diagnosis["groups"] for case in group["affected_cases"]}
        )
        measured: set[str] = set()
        assert self.log is not None
        look_refs = [
            self.record_reference(record.to_payload()["sequence"])
            for record in self.log.snapshot().records
            # Family 13 decides its looks with `comparison_look_evaluated`
            # (plan choice 12); the lifecycle reads both as the Attempt's looks.
            if (
                record.event in ("look_evaluated", "comparison_look_evaluated")
                or (self.family_version == 15 and record.event == "comparison_completed")
            )
            and record.payload["attempt_id"] in state.attempts
        ]
        for attempt in state.attempts.values():
            if attempt.measurement_id in measurements:
                measured.update(
                    row.case_id
                    for row in measurements[attempt.measurement_id].sample_rows(
                        self._bound_repeats(attempt.measurement_id)
                    )
                    if row.disposition == "score"
                )
        return {
            "selected_cohort_visits": list(portion.get("cohort_ids", [])),
            "claimed_cases": len(claimed),
            "examined_cases": len(state.gate_scope["cases"]) if state.gate_scope else 0,
            "measured_cases": len(measured),
            "had_assessed_candidate": any(
                bool(
                    attempt.looks
                    or (
                        self.family_version == 15
                        and attempt.family15 is not None
                        and attempt.family15.comparison is not None
                    )
                )
                for attempt in state.attempts.values()
            ),
            "cycle": int(portion.get("cycle", 1)),
            "closure_refs": look_refs[-8:],
            "omitted_closure_refs": max(0, len(look_refs) - 8),
        }

    def held_attempts(self, round_number: int) -> list[AttemptState]:
        """The round's open Attempts at its barrier, in index order; each is held.

        #91 spec §3.1, ruling R3: the planner decides a round only when every
        Attempt is closed or held (`lacking_attempts`) with no repair owed
        (§3.2). An open Attempt that is not held, or a held one whose Candidate
        or fresh Parent still owes a repair, is a planner bug: deciding over it
        would close a live Attempt, and returning undecided would plan the same
        `DecideRound` again forever, so the run stops loudly instead (R12).
        """

        state = self.snapshot().rounds[round_number]
        attempts = sorted(
            (attempt for attempt in state.attempts.values() if attempt.outcome is None),
            key=lambda item: item.index,
        )
        if not attempts:
            return []
        lacking = self.lacking_attempts()
        for attempt in attempts:
            if attempt.measurement_id is None or attempt.attempt_id not in lacking:
                raise CampaignInvariantViolated(
                    f"the round barrier found {attempt.attempt_id} open and not held"
                )
            for mid in (attempt.measurement_id, attempt.parent_measurement_id):
                if mid is not None and self.repair_rows(mid):
                    # R12: a return would loop -- `next_actions` is pure, so the
                    # same view plans this `DecideRound` again.
                    raise CampaignInvariantViolated(
                        f"the round barrier found {attempt.attempt_id} owing a repair on {mid}"
                    )
        return attempts

    async def close_held(self, round_number: int) -> None:
        """#91 spec §3.1 step 2: bar each held Attempt's look and close it.

        Below the threshold, the records `attempt_stage` wrote at a stage's end
        before #91, now at the round barrier, in attempt-index order.
        """

        for attempt in self.held_attempts(round_number):
            assert attempt.measurement_id is not None
            stages = self.snapshot().measurements[attempt.measurement_id]["stages"]
            self.bar_look(attempt.attempt_id, int(stages[-1]["look"]))
            await self.close_attempt(
                attempt.attempt_id, "insufficient_evidence", "incomplete_sample", None, None
            )

    def close_independent_empty_round(self, round_number: int) -> None:
        """Task5's fully settled empty set; successful finalist work is separate."""
        from evolution.tree.case_comparison import source_summary

        assert isinstance(self.policy, CampaignPolicyV12)
        snapshot = self.snapshot()
        state = snapshot.rounds[round_number]
        assert state.baseline_measurement_id is not None
        assert all(
            a.outcome is not None and a.outcome != "finalist" for a in state.attempts.values()
        )
        if self.policy.round_stop == "all_attempts" and (
            state.family15 is None or state.family15.finalists is None
        ):
            self.append(
                "finalists_bound",
                dict(
                    round=round_number,
                    count=0,
                    attempts_digest=hashlib.sha256(canonical_json([])).hexdigest(),
                ),
            )
            self.hook("after_finalists_bound")
        if state.selection is None:
            data = self.publish_comparison_data(
                dict(
                    format="comparison-data/1",
                    kind="ranking",
                    round_stop=self.policy.round_stop,
                    entries=[],
                    proposed_attempt=None,
                )
            )
            source = source_summary(
                self.independent_measurement(
                    state.baseline_measurement_id, state.baseline_reference
                )
            )
            self.append(
                "selection_decided",
                dict(
                    round=round_number,
                    data=data,
                    reason="no_eligible_candidate",
                    selected_candidate=None,
                    selected_evidence=None,
                    next_champion=source,
                ),
            )
            self.hook("after_selection_decided")
        version, digest = snapshot.champion
        self.append(
            "round_closed",
            dict(
                round=round_number,
                **self.round_closed_attempts(state),
                outcome="retained",
                champion_version=version,
                champion_digest=digest,
                evidence=None,
                coverage=self.adaptive_coverage(state),
                **self.round_reason(),
            ),
        )
        self.hook("after_round_closed")

    def independent_ranking(self, snapshot: CampaignSnapshot, round_number: int) -> dict[str, Any]:
        """The one deterministic full-evidence ranking of a settled round."""
        from evolution.tree.case_comparison import full_primary_score, ranking_payload

        assert isinstance(self.policy, CampaignPolicyV12)
        assert self.policy.round_stop is not None
        state = snapshot.rounds[round_number]
        entries = []
        for attempt in sorted(state.attempts.values(), key=lambda a: a.index):
            ax = attempt.family15
            if ax is None or ax.global_result is None:
                continue
            result = ax.global_result
            evidence = result.payload["candidate"]
            measurement = self.measurements[evidence["measurement_id"]]
            entries.append(
                dict(
                    attempt_id=attempt.attempt_id,
                    attempt_index=attempt.index,
                    candidate_digest=attempt.candidate_digest,
                    result=result.reference.to_payload(),
                    evidence=evidence,
                    accepted=result.payload["accepted"],
                    point=full_primary_score(
                        measurement.sample_rows(measurement.max_repeat),
                        measurement.identity.to_payload()["schedule"],
                        corpus=snapshot.identity["corpus_cases"],
                        primary=self.policy.metric_primary,
                    ),
                )
            )
        ranking = ranking_payload(entries, self.policy.round_stop)
        return ranking

    def decide_independent_round(self, round_number: int) -> None:
        from evolution.tree.case_comparison import source_summary

        assert isinstance(self.policy, CampaignPolicyV12)
        assert self.policy.round_stop is not None
        snapshot = self.snapshot()
        state = snapshot.rounds[round_number]
        if state.selection is None:
            ranking = self.independent_ranking(snapshot, round_number)
            entries = ranking["entries"]
            proposed = ranking["proposed_attempt"]
            selected = next((e for e in entries if e["attempt_id"] == proposed), None)
            assert state.baseline_measurement_id is not None
            incumbent = (
                state.family15.champion_completions[-1].payload["source"]
                if state.family15 is not None and state.family15.champion_completions
                else source_summary(
                    self.independent_measurement(
                        state.baseline_measurement_id, state.baseline_reference
                    )
                )
            )
            data = self.publish_comparison_data(ranking)
            self.append(
                "selection_decided",
                dict(
                    round=round_number,
                    data=data,
                    reason="automatic" if selected else "no_eligible_candidate",
                    selected_candidate=None if selected is None else selected["candidate_digest"],
                    selected_evidence=None if selected is None else selected["evidence"],
                    next_champion=incumbent if selected is None else selected["evidence"],
                ),
            )
            self.hook("after_selection_decided")
            state = self.snapshot().rounds[round_number]
        assert state.selection is not None
        selected_id = state.selection["attempt_id"]
        evidence = None
        version, digest = snapshot.champion
        if selected_id is not None:
            winner = state.attempts[selected_id]
            assert (
                self.store is not None
                and winner.candidate_digest is not None
                and winner.dependency_digest is not None
            )
            with _boundary("artifact_store"):
                champion = self.store.promote(
                    SelectionDecision(
                        round_id=round_number,
                        attempt_id=selected_id,
                        candidate_digest=winner.candidate_digest,
                        dependency_digest=winner.dependency_digest,
                        decision_ref=self.stored_decision_reference(round_number),
                    )
                )
            version, digest = champion.artifact_id, champion.digest
            committed = self.ledger_reference(
                lambda r: (
                    r.event == "promotion_committed" and r.payload.get("round_id") == round_number
                )
            )
            assert committed is not None
            evidence = [committed]
        self.append(
            "round_closed",
            dict(
                round=round_number,
                **self.round_closed_attempts(state),
                outcome="promoted" if selected_id is not None else "retained",
                champion_version=version,
                champion_digest=digest,
                evidence=evidence,
                coverage=self.adaptive_coverage(state),
                **self.round_reason(),
            ),
        )
        self.hook("after_round_closed")

    async def decide_round(self, round_number: int) -> None:
        assert self.store is not None
        snapshot = self.snapshot()
        state = snapshot.rounds[round_number]
        if state.focus_portion is not None and state.focus_portion["reason"] in (
            "none",
            "unmeasurable",
        ):
            version, digest = snapshot.champion
            # `unmeasurable` is family 6's own empty round (spec §4.3): the
            # baseline failed somewhere, but every failing cohort was emptied by
            # the excluded pairs, so no Attempt was opened and none could have
            # been judged. `clean` says nothing failed; `stalled` says some of
            # what failed could not be read.
            if state.focus_portion["reason"] == "unmeasurable":
                outcome = "unmeasurable"
            else:
                outcome = "clean" if state.focus_portion["unavailable"] == 0 else "stalled"
            self.append(
                "round_closed",
                {
                    "round": round_number,
                    **self.round_closed_attempts(state),
                    "outcome": outcome,
                    "champion_version": version,
                    "champion_digest": digest,
                    "evidence": [self.record_reference(int(state.focus_portion["seq"]))],
                    "coverage": self.adaptive_coverage(state),
                    **self.round_reason(),
                },
            )
            self.hook("after_round_closed")
            return
        diagnosis = state.diagnosis(self.resilience)
        reason = (
            editor_input_reason(diagnosis)
            if diagnosis is not None and state.gate_scope is None
            else None
        )
        if reason is not None:
            # Family 9 (spec §14, ADR-0022): the settled reflection is no valid
            # Editor input, so the round closes with no gate scope, Attempt,
            # Candidate measurement or alpha spend, citing the portion it
            # focused and the reflection terminal it read.
            assert diagnosis is not None and state.focus_portion is not None
            version, digest = snapshot.champion
            # Family 10 (role resilience spec §4.2-§4.3): the threshold-th
            # consecutive content failure on the selected group is a skip,
            # which advances visitation past it (`_advances_visitation`).
            skipped = (
                self.resilience
                and snapshot.max_consecutive_failures is not None
                and skip_due(snapshot, round_number, snapshot.max_consecutive_failures)
            )
            self.append(
                "round_closed",
                {
                    "round": round_number,
                    **self.round_closed_attempts(state),
                    "outcome": "unplanned",
                    "reason": reason,
                    "champion_version": version,
                    "champion_digest": digest,
                    "evidence": [
                        self.record_reference(int(state.focus_portion["seq"])),
                        self.record_reference(int(diagnosis["seq"])),
                    ],
                    "coverage": self.adaptive_coverage(state),
                    **({"visitation": "skipped"} if skipped else {}),
                },
            )
            self.hook("after_round_closed")
            if skipped and skip_chain_length(self.snapshot(), round_number) >= skip_chain_limit(
                self.adaptive.identity.to_payload()
            ):
                # A second skip in a row looks like a misconfigured role: the
                # skip stands, and the campaign suspends for the operator.
                self.stop(_Pending("infrastructure_failed", "reflector_failing", "none"))
            return
        if state.focus is not None and state.focus["reason"] == "none":
            # A round with nothing to focus on opened no attempt and ran no
            # reflection, so there is no selection to decide: it closes on its
            # focus alone (spec §3.3). `clean` says the baseline showed no
            # failure at all; `stalled` says some of it could not be read.
            version, digest = snapshot.champion
            self.append(
                "round_closed",
                dict(
                    round=round_number,
                    **self.round_closed_attempts(state),
                    outcome="clean" if state.focus["unavailable"] == 0 else "stalled",
                    champion_version=version,
                    champion_digest=digest,
                    evidence=[self.record_reference(int(state.focus["seq"]))],
                    **({"coverage": self.adaptive_coverage(state)}),
                    **self.round_reason(),
                ),
            )
            self.hook("after_round_closed")
            return
        if self.family_version == 15 and any(
            a.outcome == "finalist" for a in state.attempts.values()
        ):
            self.decide_independent_round(round_number)
            return
        if (
            self.family_version == 15
            and state.selection is not None
            and state.selection["reason"] == "no_eligible_candidate"
        ):
            self.close_independent_empty_round(round_number)
            return
        if state.selection is None:
            if self.family_version == 15 and thresholds_reached(snapshot, self.policy):
                # The driver reaches this barrier only after active work drains.
                # Fully scored sides may be parked here: L refuses their completion
                # once another Attempt crosses. Preserve fault precedence before
                # the threshold owner; the ordinary held invariant applies below.
                for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
                    if attempt.outcome is None:
                        parked_faults = self.attempt_fault_rows(attempt)
                        if parked_faults:
                            self.stop(
                                _Pending(
                                    "infrastructure_failed",
                                    str(parked_faults[0]["reason"]),
                                    "none",
                                    faults=parked_faults,
                                )
                            )
                            return
                self.thresholds(round_number, "round")
                return
            if state.cohort is not None:
                # Family-13 design §6.14: every cohort stage ran its own barrier
                # (§8), so no prelude runs here; the comparisons and the
                # selection follow, then Promotion or the retained close.
                self.decide_cohort(round_number)
                await self.close_family_13_selection(round_number)
                return
            self.held_attempts(round_number)
            for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
                if attempt.outcome == "insufficient_evidence" and attempt.reason in (
                    "scorer_metric_missing",
                    "infrastructure_failed",
                ):
                    faults = self.attempt_faults(attempt)
                    faults = self.attempt_fault_rows(attempt)
                    if attempt.reason == "infrastructure_failed" and not faults:
                        # #97 spec §4.4, PDR-0015: an `infrastructure_failed`
                        # closure with no located fault rows is the historical
                        # exception (pre-fix evidence). The stop could not be
                        # evidenced, so the round proceeds; the Attempt is
                        # simply not selectable.
                        continue
                    self.stop(
                        _Pending(
                            "infrastructure_failed",
                            str(attempt.reason),
                            "none",
                            evidence=tuple(self.selection_evidence(round_number, None)),
                            faults=faults,
                        )
                    )
                    return
            if self.thresholds(round_number, "round"):
                return
            await self.close_held(round_number)
            snapshot = self.snapshot()
            state = snapshot.rounds[round_number]
            if self.family_version == 15 and all(
                a.outcome != "finalist" for a in state.attempts.values()
            ):
                self.close_independent_empty_round(round_number)
                return
            self.hook("before_finalists_bound")
            self.bind_finalists(round_number)
            # A round without finalists wrote its null selection: close it.
            await self.close_family_13_selection(round_number)
            return
        else:
            attempt_id = state.selection["attempt_id"]
            winner = None if attempt_id is None else state.attempts[str(attempt_id)]
            decision_ref = self.stored_decision_reference(round_number)
        if winner is None:
            version, digest = self.snapshot().champion
            self.append(
                "round_closed",
                dict(
                    round=round_number,
                    **self.round_closed_attempts(state),
                    # Design §6.12: a family-13 cohort that selected no
                    # finalist retains the Champion; otherwise the existing rule.
                    outcome="retained"
                    if state.cohort is not None
                    else "unscored"
                    if not any(a.looks for a in state.attempts.values())
                    else "stalled",
                    champion_version=version,
                    champion_digest=digest,
                    evidence=None,
                    **({"coverage": self.adaptive_coverage(state)}),
                    **self.round_reason(),
                ),
            )
            self.hook("after_round_closed")
            return
        assert winner.candidate_digest is not None and winner.dependency_digest is not None
        with _boundary("artifact_store"):
            champion = self.store.promote(
                SelectionDecision(
                    round_id=round_number,
                    attempt_id=winner.attempt_id,
                    candidate_digest=winner.candidate_digest,
                    dependency_digest=winner.dependency_digest,
                    decision_ref=decision_ref,
                )
            )
        committed = self.ledger_reference(
            lambda record: (
                record.event == "promotion_committed"
                and record.payload.get("round_id") == round_number
            )
        )
        assert committed is not None
        self.append(
            "round_closed",
            dict(
                round=round_number,
                **self.round_closed_attempts(state),
                outcome="promoted",
                champion_version=champion.artifact_id,
                champion_digest=champion.digest,
                evidence=[committed],
                **({"coverage": self.adaptive_coverage(state)}),
                **self.round_reason(),
            ),
        )
        self.hook("after_round_closed")

    # -- the terminal barrier ---------------------------------------------

    async def barrier(self) -> None:
        """Spec §7: stop opening work, drain every external operation, release."""

        # `stopping` is the only stop signal: an attempt task takes no further
        # step, `run_editor`'s cancellation predicate reads it, and every running
        # sample runs to its own terminal. The executor never cancels a task
        # itself — spec §7 forbids cancelling a sample, and an editor is cancelled
        # cooperatively inside `run_editor`, so a stage is never cut mid-write.
        self.stopping = True
        try:
            await self.close_probes()
        except _Fault as exc:
            self.fault = self.fault or exc
        if self.tasks:
            tasks = list(self.tasks.values())
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.tasks.clear()
            for result in results:
                if isinstance(result, _Fault):
                    self.fault = self.fault or result
                elif isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self.error = self.error or result
        for measurement_id in list(self.measurements):
            try:
                await self.release(measurement_id)
            except _Fault as exc:
                self.fault = self.fault or exc

    def _continuation(self, pending: _Pending) -> tuple[str, str | None]:
        """The continuation a decided terminal carries in this campaign's family.

        Continuable-terminals spec §5.1: every family-6 `campaign_finished` offers
        `linked_run`, naming the terminal's own reason. Earlier families keep the
        literal the deciding branch chose.
        """

        return "linked_run", pending.reason

    def write_terminal(self) -> None:
        if self.connection_suspended:
            # R61, ruling 5: the gate already wrote this invocation's
            # connection suspension right after its failed preflight. Nothing
            # more is written -- not a concurrent stop's pending terminal, not
            # an unresponsive editor's, not a capacity record. Family 15
            # refuses that suspension while a selection owes its round
            # closure, so returning before the check below hides no owed one.
            return
        if self.fault is not None:
            # A classified fault writes at most its nonterminal capacity fact
            # (any other fault writes nothing), so it is settled here even
            # when it left the closure owed (#278 F4).
            self._write_fault_consequences()
            return
        if self._owed_closure():
            raise CampaignInvariantViolated("a terminal cannot precede the owed round closure")
        if self.pending is None:
            if self.unresponsive:
                self.pending = self._suspend_or_finish(
                    _Pending("infrastructure_failed", "editor_unresponsive", "none")
                )
            else:
                return
        pending = self.pending
        if pending.suspend:
            # I1 spec §5.2: a family-7 infrastructure cause is recorded by the
            # non-terminal `campaign_suspended`, at the place its family-6
            # terminal would be; no `campaign_finished` follows it.
            assert pending.owner is not None and pending.suspension is not None
            suspension = dict(
                reason=pending.reason,
                owner=pending.owner,
                evidence=dict(pending.suspension),
            )
            try:
                self.append("campaign_suspended", suspension)
            except _Fault as exc:
                self.fault = exc
                return
            self.suspended = suspension
            return
        kind, continuation_reason = self._continuation(pending)
        payload = dict(
            outcome=pending.outcome,
            reason=pending.reason,
            continuation=dict(kind=kind, reason=continuation_reason),
            evidence=None
            if pending.evidence is None
            else [dict(item) for item in pending.evidence],
        )
        try:
            self.append("campaign_finished", payload)
        except _Fault as exc:
            self.fault = exc

    def _write_fault_consequences(self) -> None:
        """Preserve a capacity fault's nonterminal fact, even before closure."""

        assert self.fault is not None
        if self.fault.terminal == "storage_capacity_exhausted":
            with suppress(_Fault):
                self.append(
                    "capacity_exhausted",
                    dict(
                        owner=self.fault.owner,
                        measurement_id=self.fault.measurement,
                        code=self.fault.code,
                    ),
                )

    def result(self) -> CampaignResult:
        snapshot = self.snapshot()
        view = self.view()
        status: CampaignProjection | None = None
        try:
            if self.closed:
                # Comparable-links spec §5.2: an immediately closed campaign
                # admitted no sibling and opened no store, so its writer
                # holds none of them; the report reads them the way
                # `inspect_view` does, read-only, over the snapshot this
                # writer just reduced. A held family-15 close (#263) did
                # admit them; the read-only re-read is still consistent,
                # because nothing is appended after its terminal.
                from evolution.tree.campaign_projections import inspect_view

                view = replace(inspect_view(self.run_root), snapshot=snapshot)
            else:
                view = replace(
                    view,
                    tries=read_measurement_tries(self.run_root, snapshot, view.siblings),
                    # G2 spec §4.6, §4.7: the completion checks and the
                    # drift caveats read the same durable journals
                    # `inspect_view` does. Computed once here and shared
                    # by both projections.
                    affected_items=read_affected_items(self.run_root, snapshot, view.siblings),
                )
        except (JournalBoundaryError, OSError) as exc:
            # A measurement journal that cannot be read after the terminal is
            # durable is a storage fault like any other (spec §7): it never
            # ends the run in a traceback and never becomes a null `tries`.
            # The run reports `persistence_failed` over the measurement
            # journal -- unless an earlier fault already classified it -- with
            # the read-only inspection as its status, which is exactly how
            # `inspect_view` surfaces a sibling it cannot read (it raises, and
            # `_unopened_status` falls back to the unreadable view).
            if self.fault is None:
                # `read_measurement_tries` raises an unreducible prefix as
                # `JournalFault`, so no other `ValueError` is caught here.
                self.fault = (
                    _classify(exc, "measurement_journal")
                    if isinstance(exc, JournalBoundaryError)
                    else None
                ) or _Fault("persistence_failed", "measurement_journal", "journal_unavailable")
            status = _unopened_status(self)
        try:
            if status is None:
                status = project_status(view, secrets=self.secrets, run_root=self.run_root)
            if self.fault is not None:
                return CampaignResult(
                    terminal=self.fault.terminal,
                    quiescent=not (self.editor_alive or self.reflector_alive),
                    finished=None,
                    owner=self.fault.owner,
                    code=self.fault.code,
                    result=None,
                    status=status,
                    fault_detail=self.fault.detail,
                    provider_attempt_lines=tuple(self._provider_attempt_lines),
                )
            if self.suspended is not None:
                return CampaignResult(
                    terminal="suspended",
                    quiescent=not (self.editor_alive or self.reflector_alive),
                    finished=None,
                    owner=str(self.suspended["owner"]),
                    code=str(self.suspended["reason"]),
                    result=None,
                    status=status,
                    suspended=dict(self.suspended),
                    provider_attempt_lines=tuple(self._provider_attempt_lines),
                )
            return CampaignResult(
                terminal="finished",
                quiescent=not (self.editor_alive or self.reflector_alive),
                finished=None if snapshot.finished is None else dict(snapshot.finished),
                owner=None,
                code=None,
                result=project_result(
                    snapshot,
                    secrets=self.secrets,
                    tries=view.tries,
                    affected_items=view.affected_items,
                    independent_report=status.to_payload().get("independent"),
                ),
                status=status,
                provider_attempt_lines=tuple(self._provider_attempt_lines),
            )
        except ProjectionInvalid:
            if snapshot.finished is None and self.suspended is None:
                raise
            return CampaignResult(
                terminal="suspended" if self.suspended is not None else "finished",
                quiescent=not (self.editor_alive or self.reflector_alive),
                finished=None if snapshot.finished is None else dict(snapshot.finished),
                suspended=None if self.suspended is None else dict(self.suspended),
                owner="campaign_projection",
                code="projection_invalid",
                result=None,
                status=_inspected_status(self.run_root, self.run_id, self.secrets, minimal=True),
                fault_detail=None if self.fault is None else self.fault.detail,
                provider_attempt_lines=tuple(self._provider_attempt_lines),
            )

    def close(self) -> None:
        if self.active_probe_session is not None:
            self.active_probe_session.revoke_event_authority()
            self.active_probe_session.abort()
        if self.active_probe_mediator is not None:
            self.active_probe_mediator.abort()
        for measurement in self.measurements.values():
            with suppress(Exception):
                measurement.close()
        if self.store is not None:
            with suppress(Exception):
                self.store.close()
        if self.log is not None:
            with suppress(Exception):
                self.log.close()
        if self.diagnostic_staging is not None:
            self.diagnostic_staging.cleanup()

    # -- creation ---------------------------------------------------------

    async def preflight(self) -> tuple[ArtifactStoreIdentityV1, Any]:
        _admit_runtime_policy(self.policy, self.family_version)
        admit_reflection_capacity(len(self.corpus), self.policy)
        self.adaptive.family_membership(self.corpus)
        scan = scan_tree(self.seed_root, self.artifact_policy)
        store_identity = ArtifactStoreIdentityV1(
            self.artifact_policy, scan.candidate_digest, scan.dependency_digest
        )
        preflight = await self.adapter.preflight(
            ExpectedAttestation(scan.candidate_digest, scan.dependency_digest)
        )
        admit_capabilities(self.policy, preflight.capabilities)
        self.seed_mode = preflight.semantic.seed_mode
        self.strategy = _strategy_for(preflight.capabilities.materialization)
        self.expected_semantic = preflight.semantic
        return store_identity, preflight

    def build(
        self, store_identity: ArtifactStoreIdentityV1, preflight: Any, linked: LinkedFrom | None
    ) -> CampaignIdentityV1:
        identity: EditorIdentity = self.editor.identity
        return build_identity(
            self.policy,
            preflight,
            self.environment.identity,
            self.scorer,
            identity,
            self.corpus,
            store_identity,
            self.strategy,
            linked,
            reflector=self.reflector.identity,
            environment_docs_digest=(
                None if self.environment_docs is None else self.environment_docs.digest
            ),
            probe=probe_identity_payload(self.probe),
            connection=None if self.connection is None else self.connection.identity_fields(),
        )

    def open_log(self, *, create: bool) -> None:
        with _boundary("campaign_journal"):
            self.log = AppendLog.open(
                self.run_root,
                file_name=CAMPAIGN_FILE,
                lock_name=CAMPAIGN_LOCK,
                create=create,
                state_factory=lambda: CampaignLifecycle(
                    selector=None, family_version=self.family_version
                ),
                max_record_bytes=MAX_RECORD_BYTES,
                max_log_bytes=MAX_JOURNAL_BYTES,
                secrets=self.secrets,
                envelope_family=self.envelope_family,
                require_empty=False,
            )

    def admit_creation(self, identity: CampaignIdentityV1, run_id: str) -> Envelope:
        """Build and admit `campaign_created` before the run root exists (spec §3).

        The same `Envelope.from_payload` and `encode()` the log uses, with the
        run's secrets: a redaction refusal is `RecordMatchesSecret` at its
        location, an encoded length past the record bound is `CorpusTooLarge`.
        The returned envelope is the one `create` appends, unchanged.
        """

        envelope = dict(
            schema_version=1,
            run_id=run_id,
            sequence=1,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            event="campaign_created",
            operation_id=None,
            payload=dict(
                identity=identity.to_payload(),
                identity_digest=identity.digest,
                producing_version=resolve_package_version(),
                family_version=self.family_version,
                **(
                    {"diagnostic_import_format": "baseline-diagnostic-bundle/1"}
                    if self.family_version == 15
                    else {}
                ),
                alpha_attempt=creation_alpha_attempt(self.policy),
                **({"adaptation": adaptation_identity_slot(self.adaptive.identity)}),
                # ADR-0021: family 8 journals the full adaptation identity, so
                # an audit over the journal alone reads `family_membership`.
                **self._adaptation_payload_field(),
            ),
        )
        try:
            record = Envelope.from_payload(
                envelope, secrets=self.secrets, family=self.envelope_family
            )
        except SchemaError as exc:
            location = secret_location(envelope, self.secrets)
            if location is None:
                raise CampaignInvariantViolated(str(exc)) from None
            raise RecordMatchesSecret(location) from None
        if len(record.encode()) > MAX_RECORD_BYTES:
            raise CorpusTooLarge()
        return record

    def create(
        self,
        identity: CampaignIdentityV1,
        store_identity: ArtifactStoreIdentityV1,
        run_id: str,
        record: Envelope,
    ) -> None:
        self.identity = identity
        self.run_id = run_id
        self.open_log(create=True)
        assert self.log is not None
        with _boundary("campaign_journal"):
            self.prove_envelope(record)
            self.wrote = True
            self.log.append_envelope(record)
            self._reach_last_refusal_point()
        self._write_bind_hits()
        # §6.1: a new run records every accepted hygiene finding between
        # `campaign_created` and its first step, so the next invocation matches
        # the same instance from the journal and needs no decision again.
        self._write_hygiene_applications()
        # The snapshot is written once the campaign's first durable record
        # stands: the identity that names its digest is now on disk, so a
        # resumed round re-renders the same documentation from the run
        # directory instead of the operator's live one (spec §5.5).
        self._retake_environment_docs()
        self.hook("after_campaign_created")
        with _boundary("artifact_store"):
            self.store = ArtifactStore.create(
                self.run_root,
                self.seed_root,
                self.artifact_policy,
                parallel_attempts=self.policy.parallel_attempts,
                secrets=self.secrets,
                expected=store_identity,
                inodes=self.inode_floor(fresh=True),
                _estimate_observer=self.note_inodes,
                _hooks=self.store_hooks or None,
            )
        self.siblings = {STORE_SIBLING: "born"}

    # -- resume -----------------------------------------------------------

    async def adopt(self, run_id: str | None = None) -> None:
        """Spec §11 steps 1-6, in order, before the drive loop continues.

        `run_id` is the caller's expectation, not an override: the journal stays
        the authority, and a caller whose id differs -- an adapter built on
        another run's id, which would then refuse every instance -- is refused
        before this campaign writes anything (W1 §6 step 5). Identity admission
        inspects the accepted prefix read-only, then runs again as the append
        log's pre-repair validator: a refused torn tail is not truncated merely
        because a writer opened it.

        PDR-0010 fixes the order of the whole method: compare against the
        *latest* identity record, admit or refuse every sibling read-only, scan
        the prefix, discharge the owed alpha row, and only then write the
        consequences of the decisions the caller handed in. Everything before
        `_write_consequences` may refuse with the run directory untouched.
        """

        assert self.log is not None

        observed = inspect_log(
            self.run_root,
            file_name=CAMPAIGN_FILE,
            max_record_bytes=MAX_RECORD_BYTES,
            max_log_bytes=MAX_JOURNAL_BYTES,
            state_factory=lambda: CampaignLifecycle(
                selector=None, family_version=self.family_version
            ),
            envelope_family=self.envelope_family,
        )
        # Preserve the append log's own empty/corrupt-prefix classification. It
        # refuses both before repair, while a valid prefix plus an incomplete
        # final record can proceed to identity admission below.
        if not observed.records or observed.journal_integrity == "corrupt_suffix":
            self.log.scan()
            raise AssertionError("campaign scan returned an inadmissible prefix")
        created = observed.records[0]
        if created.event != "campaign_created":
            raise IdentityError("run_identity_changed")
        # ADR-0015 amends ADR-0008: the creation record is history and never
        # rewritten, but what the run's identity *is* now is the latest of it
        # and any `identity_rebased`.
        latest = _latest_identity_payload(observed.records)
        reduced = reduce(observed.records, selector=None, family_version=self.family_version)
        # PDR-0010 (amended 2026-09-17), before anything else this method may
        # write: a finished campaign takes no authorization, so a hygiene
        # decision it has not already applied is refused here rather than
        # discovered as a lifecycle violation inside `_write_consequences`. A
        # finding the journal already carries is not pending -- it needs no
        # decision and writes no record -- and a finished campaign that merely
        # resumes to report its terminal stays admissible. The
        # identity half of the same rule is the `reduced.finished` arm of the
        # hard branch below.
        if reduced.finished is not None:
            for pending, _ in self.hygiene_applications:
                if pending.instance_digest not in reduced.hygiene_admitted:
                    raise HygieneNotAuthorizable(pending.name, pending.guard)
        self.run_id = str(created.to_payload()["run_id"])
        if run_id is not None and run_id != self.run_id:
            raise CampaignInvariantViolated("run_id_mismatch")

        def validate_prefix(snapshot: Snapshot) -> None:
            current = snapshot.records[0]
            if current.encode() != created.encode():
                raise IdentityError("run_identity_changed")
            if str(current.to_payload()["run_id"]) != self.run_id:
                raise IdentityError("run_identity_changed")
            # By digest only, and deliberately: a rebase that moved nothing but
            # the producing version leaves the identity digest where it was and
            # is invisible to this comparison. That is the reconciliation the
            # check is for -- the prefix this invocation compared against is
            # still the prefix it is about to append to -- and the campaign lock
            # makes a second writer's rebase between the comparison and this
            # scan unreachable, so the digest is the whole question.
            now = _latest_identity_payload(snapshot.records)
            if now["identity_digest"] != latest["identity_digest"]:
                raise IdentityError("run_identity_changed")

        if reduced.finished is None and reduced.cancelled_role_seq is not None:
            # Family 10 (PDR-0019, ADR-0023 amended 2026-09-28): a durable
            # `cancelled` role terminal is the caller's
            # stop and its terminal is owed. Like an applied close's
            # (`finish_owed_terminal`, comparable-links spec §5.3) it is
            # written state-first: no target preflight, no identity
            # comparison, no decision matched or consequence written, no
            # sibling or store. Supplied decisions stay unconsumed (`evolve`
            # notes them `authorization not needed`).
            await self._finish_owed_cancellation(reduced, validate_prefix)
            return
        if self.adaptive.identity.profile in PARTITION_PROFILES:
            previous_partition = latest.get("adaptation_payload", {}).get("corpus_partition")
            current_partition = self.adaptive.identity.to_payload()["corpus_partition"]
            if previous_partition != current_partition:
                raise IdentityError("corpus_partition_changed")
        stored = latest["identity"]
        # A target that can no longer serve the policy raises
        # `CapabilitiesInsufficient` out of `admit_capabilities`, and it travels
        # as itself: PDR-0010 classes it hard (ruling 12). A shrink the policy
        # still fits is admitted here and shows up below as a difference.
        store_identity, preflight = await self.preflight()
        linked = (
            None
            if stored["linked_from"] is None
            else LinkedFrom.from_payload(
                stored["linked_from"], successor_family=_link_context(stored)
            )
        )
        identity = self.build(store_identity, preflight, linked)
        rebuilt = {
            "identity": identity.to_payload(),
            "identity_digest": identity.digest,
            "producing_version": resolve_package_version(),
            "adaptation": (adaptation_identity_slot(self.adaptive.identity)),
        }
        journaled_record, rebuilt_record = identity_record(latest), identity_record(rebuilt)
        differences = identity_differences(journaled_record, rebuilt_record)
        # ADR-0021: the adaptation profile chose the family (`/1` is family 7,
        # `/2` family 8, `/3` family 9 per ADR-0022), so a profile move is a
        # family change. No record of either family can carry the other's
        # slot, so it is hard, finished or not -- never an offered `rebase` --
        # and the door is a linked run.
        journaled_slot = latest.get("adaptation")
        journaled_profile = (
            journaled_slot.get("version") if isinstance(journaled_slot, Mapping) else None
        )
        rebuilt_profile = self.adaptive.identity.profile
        if (
            journaled_profile is not None
            and rebuilt_profile is not None
            and journaled_profile != rebuilt_profile
        ):
            raise IdentityChanged(
                (
                    RefusalDifference(
                        ADAPTATION_PROFILE_FIELD, str(journaled_profile), str(rebuilt_profile)
                    ),
                    *differences,
                ),
                None,
                hard=True,
            )

        if reduced.pending_change is not None:
            # I2 spec §5.4: the prefix ends inside an applied in-place change's
            # block. Its `authorization_applied` is standing authority for the
            # identity it recorded, and nothing else: no decision is matched.
            self._resume_change(observed.records, reduced, identity, rebuilt, differences)
        elif differences:
            # Hard (§3, §4): a family that has no record to say so; a campaign
            # that already wrote `campaign_finished`, where nothing is left to
            # continue and no consequence record could be appended even under
            # a decision; and a comparable move on a family that cannot close.
            #
            # A comparable move on an unfinished family-6 campaign is
            # authorizable (comparable-links spec §5.1): the accepted effect
            # closes it as `stopped`/`condition_changed`, or -- past
            # `threshold_reached` -- writes the terminal the prefix already
            # owes. A campaign that crossed its threshold and crashed before
            # writing the terminal still owes one record, so a non-comparable
            # change -- typically the producing version of the package that
            # resumes it -- keeps today's rebase under its recorded producing
            # version (PDR-0010, amended 2026-09-17).
            comparable = comparable_changed(differences)
            if reduced.finished is not None:
                raise IdentityChanged(differences, None, hard=True)
            instance = identity_instance_digest(journaled_record, rebuilt_record)
            # PDR-0012: an unresponsive
            # editor or Reflector, or an unavailable probe check, already
            # determines the terminal, which only the `terminal` effect writes
            # -- a close would replace it with `condition_changed`. The
            # lifecycle picks the pair; `_write_close` supplies the probe
            # check's evidence from the prefix.
            #
            # Family 7 (controller ruling, PDR-0015): no infrastructure terminal
            # exists there to be owed -- each of those states is a suspension
            # cause -- so a comparable change on an unfinished family-7
            # campaign is always a `close`.
            closing: Literal["close", "terminal"] | None = ("close") if comparable else None
            # Family 14 (PDR-0023, ADR-0029; H11 choice 9): the Reflector's and
            # the Editor's frozen inputs are bound to the role projection the
            # `role_visibility` declaration derives, so its change offers only
            # `close` -- alone or beside any other difference, comparable or
            # not. No rebase and no in-place block carries role state across
            # it; `admissible_effects` and the `identity_rebased` shape repeat
            # the rule as backstops.
            visibility_close = visibility_forces_close(self.family_version, differences)
            if visibility_close:
                closing = "close"
            # I2 spec §3.2-§3.3: on family 7 a comparable move admits the
            # effects its changed fields allow, and `rebaseline` only while the
            # rebuilt policy's budget leaves this run a round to begin. Family
            # 6 keeps its one effect, and a non-comparable move its rebase.
            effects: tuple[Effect, ...] = (
                ("close",)
                if visibility_close
                else admissible_effects(
                    self.family_version,
                    differences,
                    rounds_remaining(reduced, self.policy.rounds),
                )
                if closing is not None
                else (closing or "rebase",)
            )
            # I2 spec §3.4: a new policy below what the prefix has spent cannot
            # govern the next round, so `rebaseline` is not offered over it (a
            # round budget below the rounds begun already leaves none), and a
            # decision that chooses it anyway is refused hard below. `close`
            # stays the door. A family-14 visibility close is no exception
            # (PDR-0016): it offers no `rebaseline`, but a
            # decision naming one over a contradicted policy is still the
            # hard refusal, which precedes the offered-effect check.
            contradiction = (
                policy_contradiction(
                    reduced,
                    self.policy.rounds,
                    None,
                )
                if closing is not None
                else None
            )
            if contradiction is not None:
                effects = tuple(effect for effect in effects if effect != "rebaseline")
            # ADR-0020: `rebase`, `continue`
            # and `rebaseline` each write `identity_rebased`, whose size grows
            # with the corpus (and, on family 8, the family membership). An
            # effect whose record cannot be journaled is not offered -- as
            # `rebaseline` over a contradicted policy is not -- and when none
            # is left the change is `corpus_too_large`, before any write. The
            # record is admitted with the run's secrets too, so a
            # membership value that is a forwarded secret withholds the same
            # effects and, with none left, is `record_matches_secret` at its
            # location. The lower bound here quotes a one-character reason;
            # the matched decision's own record is admitted again below.
            last_sequence = int(observed.records[-1].to_payload()["sequence"])
            previous_digest = str(reduced.identity_digest)
            rebasing = [effect for effect in effects if effect in _REBASING_EFFECTS]
            withheld = (
                self._rebase_record_refusal(
                    last_sequence,
                    rebuilt,
                    differences,
                    previous_digest,
                    instance,
                    _SHORTEST_AUTHORIZATION,
                )
                if rebasing
                else None
            )
            if withheld is not None:
                effects = tuple(effect for effect in effects if effect not in _REBASING_EFFECTS)
                if not effects:
                    raise withheld
            # Ruling R5: decisions naming this instance conflict only when what
            # they do differs, and `AuthorizationConflict` refuses them all
            # before any write (§4.2).
            authorization = match_authorization(
                self.authorizations, "run_identity_changed", instance, offered=effects
            )
            if authorization is None:
                raise IdentityChanged(
                    differences,
                    instance,
                    hard=False,
                    effect=effects[0],
                    effects=effects,
                )
            # Ruling R1 (§3.3): a decision whose effect this instance does not
            # admit is refused before any write.
            chosen = authorization.resolved_effect(effects)
            if chosen == "rebaseline" and contradiction is not None:
                # §3.4: hard, before any write, naming the key, the journaled
                # spend and the new value.
                raise PolicyContradictsPrefix(*contradiction)
            if chosen in rebasing:
                # Withheld above, or the decision's own reason is what tips the
                # record over the bound (or carries a secret a library caller
                # did not screen): refused before the block's first append.
                refusal = withheld or self._rebase_record_refusal(
                    last_sequence,
                    rebuilt,
                    differences,
                    previous_digest,
                    instance,
                    authorization.journal_payload(),
                )
                if refusal is not None:
                    raise refusal
            if chosen not in effects:
                raise AuthorizationError("authorization_invalid", "effect")
            if closing is None:
                self.pending_rebase = (identity, rebuilt, differences, instance, authorization)
            elif chosen == "rebaseline":
                if self.pending_findings:
                    raise FindingsPending(self.pending_findings)
                change = (identity, rebuilt, differences, instance, authorization, chosen)
                if _selected_open_round(reduced) is not None or _rejected_focus_attempts(reduced):
                    # §3.2 row 2, §5.4: the open round already selected. Its
                    # owed selection, promotion and `round_closed` finish in the
                    # old epoch -- they need no target call and no measurement --
                    # so the decision is held and `drive` writes the block at
                    # that boundary. A crash before then leaves the decision
                    # unconsumed; the next `evolve` matches it again.
                    self.held_change = change
                else:
                    self.pending_change = change
            elif chosen == "continue":
                # §5.4: the block is written by `_write_consequences` after the
                # same refusal points as a rebase -- sibling admission, the owed
                # import, the carried rows read back, the scan and the owed
                # alpha row -- and the ordinary adoption then continues.
                if self.pending_findings:
                    raise FindingsPending(self.pending_findings)
                continued = (identity, rebuilt, differences, instance, authorization, chosen)
                if self.family_version == 15 and (
                    _selected_open_round(reduced) is not None or _rejected_focus_attempts(reduced)
                ):
                    # #263, ADR-0044 / ADR-0020: the durable selection's owed
                    # promotion and `round_closed` come first -- the family-15
                    # lifecycle admits nothing else after `selection_decided`
                    # (ADR-0041) -- so the decision is held like a `rebaseline`
                    # and `drive` writes the block at that boundary. The
                    # re-binds' carried rows are then computed when the block
                    # is written, not read back below (`_planned_rebinds` runs
                    # for a `pending_change` only): a failure there is a
                    # post-write fault rather than a refusal, the trade-off a
                    # held `rebaseline` already accepts. A crash before the
                    # boundary leaves the decision unconsumed.
                    self.held_change = continued
                else:
                    self.pending_change = continued
            else:
                # §6: a matched close with hygiene still unaccepted is refused
                # before its first write, exactly like any other admission.
                if self.pending_findings:
                    raise FindingsPending(self.pending_findings)
                if self.family_version == 15 and (
                    _selected_open_round(reduced) is not None or _rejected_focus_attempts(reduced)
                ):
                    # #263, ADR-0044 / ADR-0020: the durable selection's
                    # promotion and `round_closed` come first; `drive` writes
                    # the close at that boundary through
                    # `_release_held_change`. Admission continues as an
                    # ordinary resume (siblings, store, alpha, connection,
                    # hygiene) because the owed promotion needs the store. A
                    # crash before the boundary leaves the decision unconsumed.
                    assert closing == "close"
                    self.held_change = (
                        identity,
                        rebuilt,
                        differences,
                        instance,
                        authorization,
                        "close",
                    )
                    self.held_close = (dict(journaled_record), dict(rebuilt_record))
                    if any(
                        difference.field.startswith("identity.artifact_store.")
                        for difference in differences
                    ):
                        self.settlement_store_identity = ArtifactStoreIdentityV1.from_payload(
                            stored["artifact_store"]
                        )
                        store_identity = self.settlement_store_identity
                        # Keep the rebuilt observed triple above intact: the
                        # close still reports every accepted difference. Only
                        # settlement's artifact-store field stays in the old
                        # epoch; no identity/store rebase is declared.
                        identity = CampaignIdentityV1(
                            {**identity.to_payload(), "artifact_store": store_identity.to_payload()}
                        )
                else:
                    # §5.2: the close needs nothing it would otherwise admit -- no
                    # sibling, no store, no environment documentation. The prefix is
                    # scanned before the first append exactly as the rebase path
                    # scans it, so a torn tail is refused rather than appended past,
                    # and an owed alpha row is discharged before the close record,
                    # which the lifecycle follows with nothing but its terminal.
                    self.log.scan(validate_prefix=validate_prefix)
                    if not _BIND_HIT_FIELDS.intersection(d.field for d in differences):
                        self._write_bind_hits()
                    # Family 14 (ruling 19): the close's records wait for an open
                    # preflight's terminal and a failed one's owed suspension; both
                    # are settled first, so the close's own terminal follows them
                    # and no applied close ever stands beside an open preflight.
                    await self.recover_connection(
                        frozenset({*reduced.admitted_versions, str(rebuilt["producing_version"])}),
                        stop=False,
                    )
                    # Controller ruling 3 (D2a §6, §9): hygiene accepted in the same
                    # invocation is journaled before the close record -- after the
                    # owed alpha row, which is a completion rather than a
                    # consequence -- because the lifecycle admits nothing but the
                    # terminal after the close, and an accepted decision that was
                    # never journaled would read `pending` forever.
                    self._write_hygiene_applications()
                    self._write_close(
                        closing, journaled_record, rebuilt_record, instance, authorization
                    )
                    return
        # Comparable-links spec §6: the identity is admitted and the collected
        # hygiene is not. Refused here, before the sibling checks: siblings are
        # not in the collected set, and a hard sibling finding must not reach
        # the operator ahead of hygiene they have not accepted yet.
        #
        # ADR-0015: the
        # owed-copy check is authorizable, so it joins the collected set. It
        # runs read-only first: an unmatched `baseline_import_unavailable`
        # is raised and `evolve` prints it after the collected hygiene, in one
        # refusal with one `--accept` command; a copy that is not owed, reads
        # back, or whose loss a decision matched leaves hygiene the whole
        # refusal, as before.
        if self.pending_findings:
            self._admit_owed_import(reduced, identity)
            raise FindingsPending(self.pending_findings)
        # §6.4: every version this run has been written by, so a sibling journal
        # an earlier version stamped is resumed under the run's own authority.
        versions = set(reduced.admitted_versions)
        if (
            self.pending_rebase is not None
            or self.pending_change is not None
            or self.held_change is not None
        ):
            versions.add(str(rebuilt["producing_version"]))
        self.admitted_versions = frozenset(versions)

        # §5: every check that can refuse runs to completion before the first
        # write, over the prefix this invocation observed.
        self._admit_siblings_read_only(reduced, store_identity)
        # Baseline inheritance §5.2-§5.3: an owed copy is re-read (or its loss
        # matched to a decision) read-only, as the last refusal point before
        # the scan.
        self._admit_owed_import(reduced, identity)
        # I2 spec §5.3: the carry of every measurement the change will re-bind
        # is read back from its frozen source now, so a source that cannot
        # yield it refuses while nothing is written.
        if self.pending_change is not None and self.pending_change[5] == "continue":
            self._planned_rebinds(reduced, identity, self.pending_change[2])
        self.log.scan(validate_prefix=validate_prefix)
        # The last refusal point: from here on this invocation writes.
        self.wrote = True
        # ADR-0036, E1 clarification: a finished reopen fires only through a
        # campaign-journal `append`; docs/store repairs alone get no log.
        if reduced.finished is None:
            self._reach_last_refusal_point()
        self.identity = identity
        self.store_identity = store_identity
        # A durable identity can precede all or part of its bind-hit batch.
        # Complete that debt before recovery/consequences. If bind inputs are
        # changing now, only the new rebase may own the current inputs' hits.
        if not _BIND_HIT_FIELDS.intersection(d.field for d in differences):
            self._write_bind_hits()
        # §6.2: a rebase an earlier invocation declared and crashed before
        # completing is the ledger's debt, and ADR-0015 puts it "first": the
        # store is resumed here and every owed `store_rebased` is appended
        # *before* any new consequence record of this invocation, so an audit
        # reads each declaration beside its completion instead of finding a
        # newer declaration wedged between them. The rows go in through the
        # resume itself, before its own recovery writes anything. It costs the
        # store resume `adopt_siblings` would otherwise make, which is why that
        # method then skips it. Nothing above this line writes, so the last
        # refusal point is still `self.log.scan` and `reconcile_alpha`. A debt
        # on a store that was never born is paid by
        # `adopt_siblings`, which creates the store and stamps it in one go.
        if self.owed_store_rebases and classify_store(self.run_root) == "born":
            self._resume_artifact_store(self.snapshot())
        # Family 14 (R62, C§4.4): a preflight the crash left open closes, and
        # a failed one's owed suspension is written, before any consequence
        # record -- after `_failed` the journal admits nothing else.
        await self.recover_connection(self.admitted_versions)
        # §5: only now, with every refusal settled and the owed alpha row
        # discharged, does the invocation write its first consequence record.
        self._write_consequences()
        # §6.2: admission only *reads* the environment-documentation snapshot;
        # a stored copy that was missing or damaged is repaired here, after the
        # consequence records, so a refused invocation leaves the run directory
        # exactly as it found it (ADR-0015). A rebase that moved
        # the documentation digest has already re-taken it above, and cleared
        # the flag with it. A rebase that waits -- held until the selected
        # round closes, or deferred behind that closure -- takes the repair
        # with it, so the live documentation is never re-taken before the
        # `identity_rebased` that names its digest (#278 F3); a held decision's
        # repair is `_release_held_change`'s.
        if self.held_change is None:
            if self._owed_closure():
                self.deferred_consequences.append(self._repair_environment_docs)
            else:
                self._repair_environment_docs()
        await self.adopt_siblings(store_identity)
        await self.release_inspection_leftovers()
        await self.account_resumed()
        await self.reconcile_attempts()
        if self.resumed_unresponsive():
            # Spec §7: an unresponsive editor *ends* the campaign. The live path
            # decides that terminal in memory right after `editor_failed` is
            # durable, so a process that stopped between the two left a journal
            # saying the editor's publication authority was revoked for a
            # protocol violation and nothing more. Resume derives the terminal
            # from that durable record before the drive loop runs, so no further
            # attempt is opened and no further edit is dispatched; the record is
            # written at the §7 barrier like any other pending terminal. It is
            # decided before the drive loop, so the editor violation is the
            # terminal even if a later dispatch would have faulted.
            self.unresponsive = True
            self.stop(_Pending("infrastructure_failed", "editor_unresponsive", "none"))
        elif self.resumed_reflector_unresponsive():
            self.stop(_Pending("infrastructure_failed", "reflector_unresponsive", "none"))
        elif (cause := self.resumed_resilience_cause()) is not None:
            # Family 10 (spec §4.2): a role failure of class
            # `provider`/`unknown`, or a second skip in a row, decided a
            # suspension the process did not live to write. It is written
            # first, before any planning, so the round is neither closed from
            # that record nor reflected again until the operator resumes.
            self.stop(_Pending("infrastructure_failed", cause, "none"))

    async def _finish_owed_cancellation(
        self, reduced: CampaignSnapshot, validate_prefix: Callable[[Snapshot], None]
    ) -> None:
        """Write the terminal a family-10 `cancelled` role terminal owes.

        The prefix is scanned (and checked to be the one `adopt` read) and an
        owed alpha row discharged, exactly as `_write_close` does; a role the
        crash left open gets its `interrupted` terminal, as the barrier's
        drain would have written one; then `campaign_finished` -- `stopped`/
        `cancelled`, or the terminal an applied close owes -- and nothing
        else. No Attempt is reconciled, sealed or bound and no role re-runs.

        Family 14 (H10 Task 8): a connection preflight the crash left open is
        settled first, its leftover released and its start closed
        `interrupted{reason: cancelled}` (ruling 14) -- `campaign_finished`
        waits for it, and under a durable cancellation no failed terminal or
        suspension is admitted.
        """

        assert self.log is not None
        self.log.scan(validate_prefix=validate_prefix)
        for state in self.snapshot().rounds.values():
            if state.closed is None:
                self._settle_open_operations(state)
        await self.recover_connection(
            frozenset(reduced.admitted_versions), reason="cancelled", stop=False
        )
        outcome, reason = reduced.owed_terminal or ("stopped", "cancelled")
        self.pending = _Pending(outcome, reason, "none")
        self.write_terminal()
        self.closed = True

    def _write_close(
        self,
        effect: Literal["close", "terminal"],
        journaled: Mapping[str, Any],
        rebuilt: Mapping[str, Any],
        instance: str,
        authorization: Authorization,
    ) -> None:
        """Write an accepted close (or owed terminal) and the terminal it owes.

        Comparable-links spec §5.1-§5.2 and ADR-0015: `authorization_applied`
        carries the effect, the full decision and both identity triples -- the
        inputs of the instance digest, so the lifecycle recomputes it without
        the decision file -- and no `path`. The lifecycle then owes exactly one
        `campaign_finished`: `stopped`/`condition_changed` for a close, the
        recorded threshold's for a terminal; its reduction names the pair, so
        the terminal written here is the one the prefix determined, not one
        this method decides. A crash between the two records leaves the
        terminal owed (§5.3).

        Family 14: `adopt` has already settled a connection preflight the
        crash left open, before the hygiene records and this close
        (`recover_connection`), so the terminal never waits on one.
        """

        self.append(
            "authorization_applied",
            dict(
                effect=effect,
                code="run_identity_changed",
                instance_digest=instance,
                authorization=authorization.decision_payload(),
                journaled=_triple(journaled),
                observed=_triple(rebuilt),
            ),
        )
        self.hook("after_close_applied")
        owed = self.snapshot().owed_terminal
        assert owed is not None, "an applied close owes its terminal"
        # `probe_unavailable` is the one owed terminal that carries evidence:
        # the check itself, rebuilt from the prefix exactly as the live path
        # builds it.
        self.pending = _Pending(
            owed[0],
            owed[1],
            "none",
            evidence=self.probe_failure_evidence() if owed[1] == "probe_unavailable" else None,
        )
        self.write_terminal()
        self.closed = True

    # -- an in-place condition change (I2 spec §5.2-§5.4) --------------------

    def _resume_change(
        self,
        records: Sequence[Envelope],
        reduced: CampaignSnapshot,
        identity: CampaignIdentityV1,
        rebuilt: dict[str, Any],
        differences: tuple[RefusalDifference, ...],
    ) -> None:
        """§5.4: finish a block whose `authorization_applied` is durable, without a decision.

        The recorded `observed` triple is the whole authority: a rebuilt triple
        that differs -- the manifest changed again after the decision -- is the
        hard `authorized_identity_changed`, naming both triples, before any
        write. When they agree, the missing records of the block are written
        from its `phase` by `_write_consequences`.
        """

        block = reduced.pending_change
        assert block is not None
        applied = records[int(block["seq"]) - 1].to_payload()["payload"]
        recorded = dict(applied["observed"])
        now = _triple(identity_record(rebuilt))
        if now != recorded:
            raise AuthorizedIdentityChanged(
                tuple(
                    RefusalDifference(
                        key,
                        None if recorded.get(key) is None else str(recorded[key]),
                        None if now.get(key) is None else str(now[key]),
                    )
                    for key in ("identity_digest", "producing_version", "adaptation_digest")
                )
            )
        decision = Authorization.from_decision_payload(applied["authorization"])
        effect = str(block["effect"])
        self.pending_change = (
            identity,
            rebuilt,
            differences,
            str(applied["instance_digest"]),
            decision,
            effect,
        )
        self.change_resumed = True

    def _change_target(
        self, snapshot: CampaignSnapshot, differences: Sequence[RefusalDifference]
    ) -> tuple[int, list[str]]:
        """The epoch the change's re-bound measurements belong to, and its changed fields.

        Before the block's `identity_rebased` the epoch is the next one and the
        fields come from the differences; after it, the latest epoch and the
        fields its change recorded.
        """

        block = snapshot.pending_change
        if block is not None and block["phase"] != "rebase":
            return len(snapshot.epochs) - 1, list(snapshot.condition_changes[-1]["fields"])
        return len(snapshot.epochs), _changed_comparable(
            differences, family_version=snapshot.family_version
        )

    def _carry(
        self,
        snapshot: CampaignSnapshot,
        source_id: str,
        target: Measurement,
        fields: Sequence[str],
        version: str,
    ) -> tuple[tuple[Mapping[str, Any], ...], int, str]:
        """§5.3: the rows `target` carries from the frozen `source_id`, re-read now.

        ADR-0017's primitive over the in-run source, under the range rule of
        `version` -- the one in force at the `measurement_rebound` (ADR-0019
        as amended 2026-10-03). A source that
        was bound and never born holds no row and carries none. A source that
        cannot be read, or reads back under another identity, is
        `rebound_source_corrupt`.
        """

        if not self._frozen_born(source_id):
            entry = snapshot.measurements[source_id]
            if self.family_version == 15 and entry.get("role") == "family15_completion":
                if entry.get("frozen"):
                    raise self._rebound_corrupt(source_id)
                # Plan the exact gate -> original completion -> rebound
                # admission chain. The old declaration may have omitted rows
                # that the new target would admit directly from the gate.
                # The ordinary atomic-birth builder validates this in memory;
                # no projected prefix becomes persisted source authority.
                from evolution.tree.baseline_import import SourceMeasurement
                from evolution.tree.execution.journal import Journal
                from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
                from evolution.tree.storage.append_log import _scan

                original = self.derive_bound(snapshot, source_id)
                rows = self.completion_import(snapshot, source_id)
                raw = Journal._birth_bytes(
                    source_id,
                    dict(
                        identity=original.identity.to_payload(),
                        identity_digest=original.identity.digest,
                        producing_version=version,
                    ),
                    rows,
                    secrets=self.secrets,
                    max_record_bytes=MAX_RECORD_BYTES,
                    max_journal_bytes=MAX_JOURNAL_BYTES,
                )
                log, _ = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
                source = SourceMeasurement(
                    snapshot.run_id,
                    source_id,
                    original.identity.digest,
                    hashlib.sha256(raw).hexdigest(),
                    log.records,
                )
                carried = admit_rows(
                    source,
                    target.identity,
                    primary=self.policy.metric_primary,
                    condition_changed=fields,
                    primary_range=primary_range_required(self.family_version, version),
                )
                return carried.rows, carried.imported, carried.pairs_digest
            return (), 0, import_pairs_digest(())
        try:
            source = read_frozen_source(
                self.run_root,
                self.run_id,
                source_id,
                expected_identity_digest=str(snapshot.measurements[source_id]["identity_digest"]),
            )
        except FrozenSourceCorrupt:
            raise self._rebound_corrupt(source_id) from None
        carried = admit_rows(
            source,
            target.identity,
            primary=self.policy.metric_primary,
            condition_changed=tuple(fields),
            primary_range=primary_range_required(self.family_version, version),
        )
        return carried.rows, carried.imported, carried.pairs_digest

    def _planned_rebinds(
        self,
        snapshot: CampaignSnapshot,
        identity: CampaignIdentityV1,
        differences: Sequence[RefusalDifference],
    ) -> list[dict[str, Any]]:
        """The `measurement_rebound` payloads `continue` still owes, in planner order.

        Each re-bound measurement is derived from the new identity, and its
        carry is read back from the frozen source (and cached for the birth).
        Read-only; `adopt` calls it before the first write so a damaged source
        refuses with nothing written.
        """

        epoch, fields = self._change_target(snapshot, differences)
        payloads: list[dict[str, Any]] = []
        for source_id in _rebind_sources(snapshot, epoch):
            entry = snapshot.measurements[source_id]
            measurement_id = measurement_id_for_epoch(base_measurement_id(source_id), epoch)
            # G2 spec §3.6: a re-bound measurement keeps its source's side and
            # case set; `measurement_rebound` copies both onto its entry. A
            # family-13 cohort member has neither selector, so it derives as
            # `bind_cohort_member` derives it (F7, D9).
            target = self.derive_with(
                identity,
                measurement_id,
                str(entry["candidate_digest"]),
                side_of_entry(entry),
                case_subset=entry.get("case_subset"),
                case_repeats=entry.get("case_repeats"),
                fresh_final=entry.get("role") in {"family15_final", "family15_champion"},
            )
            rows, imported, pairs = self._carry(
                snapshot, source_id, target, fields, snapshot.producing_version
            )
            self.rebound_rows[measurement_id] = rows
            payloads.append(
                dict(
                    round=int(entry["round"]),
                    from_measurement_id=source_id,
                    measurement_id=measurement_id,
                    epoch=epoch,
                    identity_digest=target.identity.digest,
                    imported=imported,
                    pairs_digest=pairs,
                    released=self._released(snapshot, source_id),
                )
            )
        return payloads

    def _released(self, snapshot: CampaignSnapshot, source_id: str) -> int:
        """The slots the frozen `source_id` reserved and never started (codex round 1).

        `_unfilled_slots` over the frozen journal: the latest stage the
        campaign opened on it and the retries its accounted tries reserved. A
        source with no stage reserved nothing; a source that cannot be read is
        `rebound_source_corrupt`.
        """

        stages = snapshot.measurements[source_id]["stages"]
        if not stages or not self._frozen_born(source_id):
            return 0
        try:
            frozen = Measurement.open_frozen(self.run_root, source_id)
        except MeasurementError:
            raise self._rebound_corrupt(source_id) from None
        released = _unfilled_slots(snapshot, source_id, frozen, self.policy.looks)
        return released + self._unspent_reservation(snapshot, source_id, frozen)

    def _unspent_reservation(
        self, snapshot: CampaignSnapshot, source_id: str, frozen: FrozenMeasurement
    ) -> int:
        """#78 (spec §3.5): this window's threshold slots whose repairs never ran.

        A reservation made since the latest suspension holds one slot per
        granted pair; a pair whose repair has not started, or started with no
        terminal, never filled it. A reservation of an earlier window was
        never counted in this one.
        """

        reservation = snapshot.repairs_reserved.get(source_id)
        if (
            reservation is None
            or snapshot.window_start is None
            or reservation[0] < snapshot.window_start
        ):
            return 0
        named = snapshot.threshold_named.get(source_id) or {}
        return min(reservation[1], frozen.unfilled_threshold_repairs(named))

    def _freezing(self, snapshot: CampaignSnapshot) -> frozenset[str]:
        """The measurements a pending `rebaseline` block will freeze, not frozen yet.

        I2 spec §5.1, §3.2: the block's `identity_rebased` opens a new epoch and
        freezes every measurement bound before it. Until it is durable --
        admission before the block, a resumed block still owing its rebase, and
        the whole span a held `rebaseline` waits through -- those measurements
        already belong to the earlier condition: after a corpus change the
        session corpus cannot derive them, and after any change nothing appends
        to them again. They are opened read-only (`_open_frozen`), exactly as
        the block will leave them. `continue` re-binds what it appends to
        instead, so it freezes nothing early.
        """

        change = self.pending_change or self.held_change
        if change is None or change[5] != "rebaseline":
            return frozenset()
        block = snapshot.pending_change
        if block is not None and block["phase"] != "rebase":
            return frozenset()  # the rebase is durable: the lifecycle froze them
        return frozenset(
            measurement_id
            for measurement_id, entry in snapshot.measurements.items()
            if not entry.get("frozen", False)
        )

    def _bound_policy(self, measurement_id: str) -> CampaignPolicyV11:
        """The policy `measurement_id` was bound under (its epoch's, §5.1).

        The audit's `policy_for` reads the same epoch policy, so a family-13
        gate or calibration takes its seed and primary metric from one source
        on both sides (F7, the F4 hand-off). A measurement without an epoch
        entry reads the policy in force.
        """

        snapshot = self.snapshot()
        entry = snapshot.measurements.get(measurement_id)
        if entry is None:
            return self.policy
        raw = snapshot.epochs[int(entry["epoch"])]["policy"]
        return policy_class_for_payload(raw).from_payload(raw)

    def _bound_repeats(self, measurement_id: str) -> int:
        """The repeats of the policy `measurement_id` was bound under (its epoch's, §5.1)."""

        return self._bound_policy(measurement_id).repeats

    def _rebound_corrupt(self, measurement_id: str) -> Exception:
        """A frozen measurement that no longer reads back: refusal or fault (§5.3).

        Before this invocation's first durable write it is the hard refusal
        `rebound_source_corrupt`. After it -- an owed re-bound birth, the rest
        of a block, the rebaseline round close -- the run directory is no
        longer untouched, so it is a persistence fault of the measurement
        journal carrying the same code, and the result reports it like every
        other post-write fault (codex branch review round 1).
        """

        if self.wrote:
            return _Fault(
                "persistence_failed",
                "measurement_journal",
                "rebound_source_corrupt",
                measurement_id,
            )
        return ReboundSourceCorrupt(measurement_id)

    def _frozen_born(self, measurement_id: str) -> bool:
        """`Measurement.classify(...) == "born"`, a journal it cannot read being corrupt."""

        try:
            return Measurement.classify(self.run_root, measurement_id) == "born"
        except (JournalBoundaryError, OSError, ValueError):
            raise self._rebound_corrupt(measurement_id) from None

    def _open_frozen(self, snapshot: CampaignSnapshot, measurement_id: str) -> Measurement:
        """A frozen measurement's read-only reader, or `rebound_source_corrupt` (§5.1).

        Opened with `Measurement.open_frozen` -- no lease, no recovery, no
        adapter, no corpus check -- and required to still be the journal the
        campaign bound: nothing may write a frozen measurement again.
        """

        try:
            frozen = Measurement.open_frozen(self.run_root, measurement_id)
        except MeasurementError:
            raise self._rebound_corrupt(measurement_id) from None
        if frozen.identity_digest != snapshot.measurements[measurement_id]["identity_digest"]:
            raise self._rebound_corrupt(measurement_id)
        return frozen.reader(
            self.run_root,
            campaign_run_id=self.run_id,
            corpus=self.corpus,
            secrets=self.secrets,
        )

    def rebound_import(
        self, snapshot: CampaignSnapshot, measurement_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        """§5.3: the rows an owed re-bound measurement is born with, verified.

        Recomputed from its frozen source against the identity of the epoch it
        was bound in and checked against the declared `imported` and
        `pairs_digest`: the source cannot move, so a mismatch is
        `rebound_source_corrupt`, never a lost source.
        """

        entry = snapshot.measurements[measurement_id]
        epoch = int(entry["epoch"])
        fields = list(snapshot.condition_changes[epoch - 1]["fields"])
        target = self.derive_bound(snapshot, measurement_id)
        version = snapshot.import_versions.get(measurement_id, snapshot.producing_version)
        rows, imported, pairs = self._carry(
            snapshot, str(entry["rebound_from"]), target, fields, version
        )
        if imported != int(entry["imported"]) or pairs != entry["rebound_pairs_digest"]:
            raise self._rebound_corrupt(str(entry["rebound_from"]))
        self.rebound_rows[measurement_id] = rows
        return rows

    def _adaptation_payload_field(self) -> dict[str, Any]:
        """Families 8 and 9: `adaptation_payload` beside the slot of a record that journals one.

        `campaign_created` and every `identity_rebased` carry the full
        adaptation identity the slot digests (ADR-0021), so the gate-scope
        audit reads the `family_membership` in force, not the creation one.
        """

        return {"adaptation_payload": self.adaptive.identity.to_payload()}

    def _identity_rebased_payload(
        self,
        rebuilt: Mapping[str, Any],
        differences: Sequence[RefusalDifference],
        previous_digest: str,
        instance: str,
        authorization: Mapping[str, str],
    ) -> dict[str, Any]:
        """The one `identity_rebased` payload both writers append and admission measures."""

        return dict(
            identity=rebuilt["identity"],
            identity_digest=rebuilt["identity_digest"],
            producing_version=rebuilt["producing_version"],
            adaptation=rebuilt["adaptation"],
            previous_digest=previous_digest,
            changed_fields=[difference.field for difference in differences],
            instance_digest=instance,
            authorization=dict(authorization),
            **self._adaptation_payload_field(),
        )

    def _rebase_record_refusal(
        self,
        last_sequence: int,
        rebuilt: Mapping[str, Any],
        differences: Sequence[RefusalDifference],
        previous_digest: str,
        instance: str,
        authorization: Mapping[str, str],
    ) -> CorpusTooLarge | RecordMatchesSecret | None:
        """Why the journal would refuse the `identity_rebased` this change owes, if it would.

        Codex branch review r1/r2: the record carries the rebuilt identity and,
        on family 8, the full adaptation identity -- both corpus-sized, and the
        membership operator-supplied -- and ADR-0020's block appends
        `authorization_applied` before it. A record that cannot be journaled
        after that would strand the campaign, since every resume owes the same
        record. So admission builds the payload the writer will append and
        admits it exactly as the journal does, before anything is written, with
        `admit_creation`'s outcomes:

        - validation is `Envelope.from_payload` with the run's secret set (the
          log's own), on an envelope shaped as the append shapes it; a refusal
          located as a secret is `RecordMatchesSecret` at that location, and any
          other schema refusal is `CampaignInvariantViolated` -- never "fits";
        - the size is the encoded envelope against `MAX_RECORD_BYTES`, with the
          fields the payload does not fix taken at their widest, so the measure
          never undershoots: a timestamp with microseconds, an operation id
          where the family gives one, and a sequence one digit wider than the
          next (the records an invocation writes before its rebase -- hygiene,
          an owed alpha row, the block's `authorization_applied` -- are far
          fewer than that). Past the bound is `CorpusTooLarge`.

        What the journal's lifecycle check (`prove`) adds is about the record's
        position, not its content, and is not repeated here.
        """

        event = "identity_rebased"
        payload = self._identity_rebased_payload(
            rebuilt, differences, previous_digest, instance, authorization
        )
        operationless = event in SUSPENDABLE_NO_OPERATION_EVENTS
        shaped = dict(
            schema_version=1,
            run_id=self.run_id,
            sequence=last_sequence + 1,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            event=event,
            operation_id=None if operationless else uuid4().hex,
            payload=payload,
        )
        try:
            Envelope.from_payload(shaped, secrets=self.secrets, family=self.envelope_family)
        except SchemaError as exc:
            location = secret_location(shaped, self.secrets)
            if location is None:
                raise CampaignInvariantViolated(str(exc)) from None
            return RecordMatchesSecret(location)
        widest = dict(
            shaped,
            sequence=10 ** (len(str(last_sequence + 1)) + 1) - 1,
            timestamp="9999-12-31T23:59:59.999999Z",
            operation_id=None if operationless else "0" * 32,
        )
        record = Envelope.from_payload(widest, family=self.envelope_family)
        if len(record.encode()) > MAX_RECORD_BYTES:
            return CorpusTooLarge()
        return None

    def birth_owed_candidate_completions(self) -> None:
        """Pay declared imports while their epoch is still live, before freezing it."""
        snapshot = self.snapshot()
        for mid, entry in snapshot.measurements.items():
            if (
                entry.get("role") != "family15_completion"
                or entry.get("frozen")
                or self._frozen_born(mid)
            ):
                continue
            measurement = self.derive_bound(snapshot, mid)
            rows = self.completion_import(snapshot, mid)
            self.hook("before_owed_candidate_completion_birth")
            with _boundary("measurement_journal", mid):
                Measurement.remove_unborn(self.run_root, mid)
                try:
                    measurement.create(imported=rows)
                finally:
                    measurement.close()
            self.hook("after_owed_candidate_completion_birth")

    def _write_condition_change(self) -> None:
        """§5.4 (3)-(5): the block of an accepted `continue`, or its owed rest.

        `authorization_applied` carries the close record's shape with the new
        effect, `identity_rebased` the new identity with its comparable
        `changed_fields`, and one `measurement_rebound` follows per measurement
        the planner would still append to, in planner order. A resumed block
        starts wherever its prefix stopped (`phase`), with no new decision.
        """

        assert self.log is not None and self.pending_change is not None
        identity, rebuilt, differences, instance, decision, effect = self.pending_change
        block = self.snapshot().pending_change
        if block is None:
            journaled = identity_record(_latest_identity_payload(self.log.snapshot().records))
            self.append(
                "authorization_applied",
                dict(
                    effect=effect,
                    code="run_identity_changed",
                    instance_digest=instance,
                    authorization=decision.decision_payload(),
                    journaled=_triple(journaled),
                    observed=_triple(identity_record(rebuilt)),
                ),
            )
            self.hook("after_condition_applied")
            block = self.snapshot().pending_change
        assert block is not None
        if block["phase"] == "rebase":
            if self.family_version == 15 and effect == "continue":
                self.birth_owed_candidate_completions()
            changed = [difference.field for difference in differences]
            self.append(
                "identity_rebased",
                self._identity_rebased_payload(
                    rebuilt,
                    differences,
                    self.snapshot().identity_digest,
                    instance,
                    decision.journal_payload(),
                ),
            )
            if "identity.reflector.environment_docs_digest" in changed:
                self.environment_docs_repair = False
                self._retake_environment_docs()
            self.hook("after_identity_rebased")
            # ADR-0015 §6.2: every `identity_rebased` owes its ledger half.
            self.owed_store_rebases.append(
                dict(
                    campaign_rebase=str(rebuilt["identity_digest"]),
                    authorization_id=decision.id,
                    producing_version=str(rebuilt["producing_version"]),
                )
            )
        snapshot = self.snapshot()
        if snapshot.pending_change is not None and snapshot.pending_change["phase"] == "rebind":
            for payload in self._planned_rebinds(snapshot, identity, differences):
                self.append("measurement_rebound", payload)
                self.hook("after_measurement_rebound")
        elif (
            snapshot.pending_change is not None
            and snapshot.pending_change["phase"] == "close_round"
        ):
            self._close_round_for_rebaseline(snapshot)
        self.pending_change = None
        self.change_resumed = False

    def _close_round_for_rebaseline(self, snapshot: CampaignSnapshot) -> None:
        """§3.2 row 1, §5.4 (5): the rest of a `rebaseline` block over an open round.

        Every Attempt of the round without an outcome is closed `abandoned`
        `condition_changed`, in attempt order -- an open stage of its
        measurement stays unfinished, as under an accepted close (ADR-0008's
        third exception) -- and then the round closes `condition_changed`
        with the current champion, no evidence and no promotion. A resumed
        block writes only the closures its prefix still lacks. The Attempt's
        workspace is released as any abandonment releases it (a store that is
        not open yet abandons it at its own recovery).
        """

        change = snapshot.condition_changes[-1]
        number = int(change["round"])
        if self.store is not None and self.store_identity is not None:
            # A store `adopt` already re-opened (to pay an earlier debt) takes
            # this block's `store_rebased` before anything else of the block
            # reaches its ledger (§6.2).
            self._complete_owed_store_rebases(self.store_identity)
        for attempt in snapshot.open_attempts(number):
            self.abandon(attempt.attempt_id)
            self.append(
                "attempt_closed",
                dict(
                    attempt_id=attempt.attempt_id,
                    outcome="abandoned",
                    reason="condition_changed",
                    look=None,
                    code=None,
                ),
            )
            self.hook("after_attempt_closed")
        state = self.snapshot().rounds[number]
        version, digest = self.snapshot().champion
        coverage: dict[str, Any] = {}
        measured = {
            attempt.measurement_id: self._open_frozen(snapshot, attempt.measurement_id)
            for attempt in state.attempts.values()
            if attempt.measurement_id is not None and self._frozen_born(attempt.measurement_id)
        }
        coverage = {"coverage": self.adaptive_coverage(state, measured)}
        self.append(
            "round_closed",
            dict(
                round=number,
                **self.round_closed_attempts(state),
                outcome="condition_changed",
                champion_version=version,
                champion_digest=digest,
                evidence=None,
                **coverage,
                **self.round_reason(),
            ),
        )
        self.hook("after_round_closed")

    def _settle_open_operations(self, state: RoundState) -> None:
        """Give every role operation of `state` still `started` its `interrupted` terminal.

        A started grouping, reflection or editor that a resume finds has lost
        the process that ran it. `reconcile_attempts` settles them on every
        resume -- the round's operations first, then each editor interleaved
        with its Attempt's other steps, in attempt order. A `rebaseline` block
        that closes the round settles all of them first (this method), because
        the lifecycle admits the block only once none is open (I2 Task 8).
        """

        self._settle_round_operations(state)
        for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
            if attempt.outcome is None:
                self._settle_open_editor(attempt)

    def _settle_round_operations(self, state: RoundState) -> None:
        """A started grouping and a started reflection of `state` get `interrupted`."""

        if state.grouping is not None and state.grouping["status"] == "started":
            self.append(
                "grouping_failed",
                {
                    "round": state.round,
                    "grouping_id": state.grouping["grouping_id"],
                    "code": "interrupted",
                    "reads": self._empty_reads(),
                },
                state.grouping["operation_id"],
            )
        if state.reflections and state.reflections[-1]["status"] == "started":
            open_reflection = state.reflections[-1]
            self.fail_reflection(
                state.round,
                str(open_reflection["reflection_id"]),
                str(open_reflection["operation_id"]),
                "interrupted",
                None,
            )

    def _settle_open_editor(self, attempt: AttemptState) -> None:
        """An adaptive Attempt's started editor gets its `interrupted` terminal."""

        if attempt.editor != "started":
            return
        assert self.log is not None
        opened = next(
            record
            for record in reversed(self.log.snapshot().records)
            if record.event == "editor_started"
            and record.payload["attempt_id"] == attempt.attempt_id
        )
        operation = opened.to_payload()["operation_id"]
        self.append(
            "editor_failed",
            {
                "attempt_id": attempt.attempt_id,
                "operation_id": operation,
                "error_class": "interrupted",
                "note_digest": None,
            },
            operation,
        )

    def _resume_artifact_store(self, snapshot: CampaignSnapshot) -> None:
        """Re-open the born store of this run root, once per invocation.

        Spec §11: only a store that never published `v0` needs the seed bytes
        again; S2 refuses a `seed_root` once its seed is published. Split out of
        `adopt_siblings` because §6.2's owed `store_rebased` has to be written
        before the first new consequence record, which is before siblings are
        adopted: `adopt` calls this, and `adopt_siblings` then skips it.

        Whatever the ledger owes travels with the resume: S2 appends those rows
        after it has proved the identity and taken the lease and before its own
        recovery writes an `operation_interrupted`, so a pending artifact
        operation cannot come between a declaration and its completion (codex
        round 2).
        """

        owed, self.owed_store_rebases = self.owed_store_rebases, []
        with _boundary("artifact_store"):
            unseeded = not ArtifactStore.inspect(self.run_root).champions
            self.store = ArtifactStore.resume(
                self.run_root,
                (
                    self.artifact_policy
                    if self.settlement_store_identity is None
                    else self.settlement_store_identity.policy
                ),
                seed_root=self.seed_root if unseeded else None,
                secrets=self.secrets,
                recover_editor_workspaces=frozenset(
                    attempt.attempt_id
                    for state in snapshot.rounds.values()
                    for attempt in state.attempts.values()
                    if self.adaptive is not None
                    and attempt.outcome is None
                    and attempt.frozen_input is not None
                    and (
                        attempt.editor in (None, "started")
                        or editor_replayable(attempt, adaptive=True, resilient=self.resilience)
                    )
                ),
                admitted_versions=self.admitted_versions,
                owed_rebases=owed,
                _hooks=self.store_hooks or None,
            )
        self.store_resumed = True

    async def adopt_siblings(self, store_identity: ArtifactStoreIdentityV1) -> None:
        snapshot = self.snapshot()
        births = classify_siblings(self.run_root, snapshot)
        if self.store_resumed:
            # `adopt` already resumed it, to complete the `store_rebased` an
            # earlier invocation owed before writing anything of its own (§6.2).
            pass
        elif births[STORE_SIBLING] == "born":
            self._resume_artifact_store(snapshot)
        else:
            # A store that was never born owes every rebase the campaign
            # declared before it existed; they are stamped as the ledger's first
            # records after `store_created`, before `v0` is published (§6.2).
            owed, self.owed_store_rebases = self.owed_store_rebases, []
            if snapshot.measurements:
                raise CampaignInvariantViolated("a born store is never removed (ADR-0008)")
            with _boundary("artifact_store"):
                _remove_unborn_store(self.run_root)
                self.store = ArtifactStore.create(
                    self.run_root,
                    self.seed_root,
                    self.artifact_policy,
                    parallel_attempts=self.policy.parallel_attempts,
                    secrets=self.secrets,
                    expected=store_identity,
                    owed_rebases=owed,
                    inodes=self.inode_floor(fresh=True),
                    _estimate_observer=self.note_inodes,
                    _hooks=self.store_hooks or None,
                )
        # §6.2: the ledger record that completes a declared rebase is the first
        # thing written through the store, before it publishes anything of this
        # invocation, so an audit reads the pair in order.
        self._complete_owed_store_rebases(store_identity)
        self.siblings = dict(births)
        self.siblings[STORE_SIBLING] = "born"
        self.publish_pending_diagnostics()
        freezing = self._freezing(snapshot)
        for measurement_id, entry in snapshot.measurements.items():
            if entry.get("frozen", False) or measurement_id in freezing:
                # ADR-0020 / I2 spec §5.1: a frozen measurement is only read. It
                # is never derived from the session (its epoch's corpus may not
                # be the session's), resumed, released through the adapter or
                # born: one that was never born holds nothing, and the instances
                # its journal still owns on the old target are its `leaked`.
                if births[measurement_id] == "born":
                    self.measurements[measurement_id] = self._open_frozen(snapshot, measurement_id)
                continue
            measurement = self.derive_bound(snapshot, measurement_id)
            if measurement.identity.digest != entry["identity_digest"]:
                raise IdentityError("stored_identity_digest_mismatch")
            with _boundary("measurement_journal", measurement_id):
                if births[measurement_id] == "born":
                    await measurement.resume(self.adapter, admitted_versions=self.admitted_versions)
                elif entry.get("rebound_from") is not None:
                    # §5.3: a declared, unborn re-bound measurement is an owed
                    # copy, born atomically with its whole verified carry.
                    rows = self.rebound_rows.get(measurement_id)
                    if rows is None:
                        rows = self.rebound_import(snapshot, measurement_id)
                    self.hook("before_rebound_birth")
                    Measurement.remove_unborn(self.run_root, measurement_id)
                    measurement.create(imported=rows)
                elif entry.get("role") == "family15_completion":
                    # G2 spec §5: a declared, unborn completion is an owed copy
                    # of its sealed source, born with its whole verified carry.
                    # Never `birth_import`: a linked run's declared baseline
                    # belongs to the seed baseline, not to a completion.
                    rows = self.completion_import(snapshot, measurement_id)
                    self.hook("before_completion_birth")
                    Measurement.remove_unborn(self.run_root, measurement_id)
                    measurement.create(imported=rows)
                elif (
                    entry.get("role")
                    == campaign_vocabulary_for(
                        14 if self.family_version == 14 else 13
                    ).registry_role
                ):
                    # Family-13 design §6.8, ADR-0027: a declared, unborn cohort
                    # member is born empty. It imports no row, so it never
                    # reaches `birth_import` -- although the Champion member's
                    # `attempt_id` is the seed side's own.
                    Measurement.remove_unborn(self.run_root, measurement_id)
                    measurement.create()
                else:
                    assert entry.get("role") != "completion"
                    # §5.2: an owed copy is completed here, as the birth;
                    # `birth_import` is empty for every other measurement, and a
                    # finished (or terminal-owing) campaign's unborn sibling is
                    # born empty as before: it owes no copy. The identity
                    # is `derive_bound`'s above, so a family-12 fresh Parent is
                    # born on the champion side over layer 1 (`side_of_entry`);
                    # it measures afresh and is never the baseline that owes
                    # the copy, hence the seed side's own `attempt_id` here.
                    owes_copy = (
                        entry["attempt_id"] == CHAMPION_SIDE
                        and snapshot.finished is None
                        and snapshot.owed_terminal is None
                    )
                    imported = self.birth_import(int(entry["round"])) if owes_copy else ()
                    Measurement.remove_unborn(self.run_root, measurement_id)
                    measurement.create(imported=imported)
            self.measurements[measurement_id] = measurement
            self.siblings[measurement_id] = "born"

    def _admit_siblings_read_only(
        self,
        reduced: CampaignSnapshot,
        store_identity: ArtifactStoreIdentityV1,
    ) -> None:
        """Spec §5: every sibling check that can refuse runs before the first write.

        It also *notes* the `store_rebased` the ledger owes (§6.2) without
        writing it: an invocation that goes on to refuse leaves the ledger one
        record behind, and the next admitted resume completes it.
        """

        birth = classify_store(self.run_root)
        if birth != "born" and reduced.measurements:
            # ADR-0008 / #261: `create` births the store before any measurement
            # is bound, so a journal that declares one proves the store was born.
            # Its ledger is lost evidence, not an unborn store: refuse before any
            # write (and before any reader that would call it corrupt) and leave
            # `artifacts/` exactly as it is.
            raise ArtifactLedgerMissing()
        if self.family_version == 15 and birth == "born":
            # An unborn store has no ledger to read diagnostics from; reading an
            # empty one as a ledger would call a crash between the file's
            # creation and its first record `artifact_unsafe`.
            from evolution.tree.baseline_diagnostics import DiagnosticTraceReader

            with _boundary("artifact_store"):
                DiagnosticTraceReader(self.run_root).close()
            from evolution.tree.candidate_completion import validate_candidate_gate

            for state in reduced.rounds.values():
                for attempt in state.attempts.values():
                    seal = None if attempt.family15 is None else attempt.family15.gate_seal
                    if seal is None:
                        continue
                    body = self.independent_data(seal)
                    source = body["measurement"]
                    mid = source["measurement_id"]
                    try:
                        frozen = read_frozen_source(
                            self.run_root,
                            reduced.run_id,
                            mid,
                            expected_identity_digest=source["identity_digest"],
                        )
                        validate_candidate_gate(frozen, body)
                    except FrozenSourceCorrupt:
                        raise self._rebound_corrupt(mid) from None

        directory = self.run_root / MEASUREMENTS_DIR
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if child.name not in reduced.measurements:
                    raise UndeclaredSibling("undeclared_sibling")
        # Family 14 (choice 12, R53): a probe journal is declared by its
        # `connection_preflight_started` before it is born, so a child no start
        # names is no journal of this campaign.
        probes = self.run_root / CONNECTION_DIR
        if probes.is_dir():
            declared = {str(entry["operation_id"]) for entry in reduced.connection.operations}
            for child in sorted(probes.iterdir()):
                if child.name not in declared:
                    raise UndeclaredSibling("undeclared_sibling")
        completed: set[tuple[str, str]] = set()
        if birth == "born":
            with _boundary("artifact_store"):
                ledger = ArtifactLedger.inspect(self.run_root)
            record = ArtifactLedger.latest_store_record(ledger)
            stamp = record.payload
            if stamp["identity_digest"] != store_identity.digest:
                raise IdentityError("run_identity_changed")
            if stamp["producing_version"] not in self.admitted_versions:
                raise IdentityError("run_identity_changed")
            # Which declared rebases the ledger has already been stamped for,
            # by the pair that identifies one declaration: the campaign rebase
            # it completes and the decision that admitted it. Both are in every
            # `store_rebased` payload, and the pair is what makes the two halves
            # one-to-one. The digest alone is not: a rebase that moves nothing
            # but the producing version leaves the identity digest where it was,
            # so two consecutive version-only rebases declare the same digest
            # twice, and a completion for the first would otherwise be read as
            # the second's -- leaving the second's debt unpaid forever.
            completed = {
                (str(entry.payload["campaign_rebase"]), str(entry.payload["authorization_id"]))
                for entry in ledger.records
                if entry.event == "store_rebased"
            }
        # An unborn store has completed nothing -- it has no ledger to complete
        # anything in -- so every rebase the campaign declared is owed, and
        # `adopt_siblings` writes them straight after `store_created`. The debt
        # is reconstructed whatever the store's birth state: a crash between the
        # campaign's first declaration and the store's creation is as ordinary
        # as one between the two halves of the pair.
        self.owed_store_rebases = [
            dict(
                campaign_rebase=str(entry["identity_digest"]),
                authorization_id=str(entry["authorization_id"]),
                # Each declaration's own version, read back from the record
                # that made it: the ledger half of a rebase `0.3.0a12`
                # declared stays stamped `0.3.0a12` however many versions
                # later it is completed (§6.2).
                producing_version=str(entry["producing_version"]),
            )
            for entry in reduced.rebases
            if (str(entry["identity_digest"]), str(entry["authorization_id"])) not in completed
        ]
        current_version = resolve_package_version()
        freezing = self._freezing(reduced)
        for measurement_id, entry in reduced.measurements.items():
            if entry.get("frozen", False) or measurement_id in freezing:
                # I2 spec §5.1: read back, never derived; see `_open_frozen`. A
                # frozen measurement that was never born holds nothing to read.
                if self._frozen_born(measurement_id):
                    self._open_frozen(reduced, measurement_id)
            else:
                if entry.get("rebound_from") is not None and (
                    Measurement.classify(self.run_root, measurement_id) != "born"
                ):
                    # §5.3: an owed re-bound birth is recomputed and verified
                    # here, read-only, so a damaged source refuses before any
                    # write.
                    self.rebound_import(reduced, measurement_id)
                elif entry.get("role") == "family15_completion" and (
                    Measurement.classify(self.run_root, measurement_id) != "born"
                ):
                    # G2 spec §5: an owed completion birth is re-read and
                    # verified here, read-only, so a damaged source refuses
                    # before any write.
                    self.completion_import(reduced, measurement_id)
                # ADR-0020: each sibling from the epoch it was bound in, not
                # from the rebuilt identity this invocation is about to adopt.
                measurement = self.derive_bound(reduced, measurement_id)
                if measurement.identity.digest != entry["identity_digest"]:
                    raise IdentityError("stored_identity_digest_mismatch")
            # ADR-0015 §5: the sibling's *version* is admitted here too, and not
            # in `Journal.resume` where `validate_resume` would reach it only
            # after `_write_consequences` had already appended this invocation's
            # `identity_rebased` and its ledger half. A born
            # measurement's creation record is inspected read-only, exactly as
            # the ledger's stamp is a few lines above, and a version outside the
            # set the campaign has admitted -- its own creation version, every
            # version an `identity_rebased` admitted, the version a pending
            # rebase is about to admit, and the running package's -- is a
            # difference on `producing_version` that no decision names, so it is
            # hard and it is raised before the first write.
            sibling = Measurement.directory_for(self.run_root, measurement_id)
            if not (sibling / EVENTS_FILE).is_file():
                continue  # absent or unborn: nothing durable carries a version yet
            with _boundary("measurement_journal", measurement_id):
                stored = inspect_journal(sibling)
            if not stored.records:
                continue
            stamped = str(stored.records[0].payload["producing_version"])
            if stamped != current_version and stamped not in self.admitted_versions:
                raise IdentityChanged(
                    (RefusalDifference("producing_version", stamped, current_version),),
                    None,
                    hard=True,
                )

    def _write_hygiene_applications(self) -> None:
        """§6.1: one `authorization_applied` per accepted run-directory finding.

        §5 source 1: an instance digest the journal already carries is standing
        authority for itself, so a caller that resupplies the same decision --
        the CLI passes the whole decision directory every time -- adds no second
        record. Duplicates inside one invocation collapse the same way.
        """

        admitted = set(self.snapshot().hygiene_admitted)
        for finding, authorization in self.hygiene_applications:
            if finding.instance_digest in admitted:
                continue
            admitted.add(finding.instance_digest)
            with _boundary("campaign_journal"):
                self.append(
                    "authorization_applied",
                    dict(
                        code="run_dir_unsafe_entry",
                        path=finding.name,
                        instance_digest=finding.instance_digest,
                        authorization=authorization.journal_payload(),
                    ),
                )

    def _write_consequences(self) -> None:
        """The consequence records of every decision this invocation matched (§5, §6).

        Admission has succeeded, so a fault is no longer a refusal. Family 15
        first settles any round whose durable selection still owes closure.
        """

        assert self.log is not None
        if self._owed_closure():
            # Preserve the existing order of admission consequences as one
            # deferred writer. No hygiene, rebase or fresh-baseline decision
            # consequence may interrupt the selection's exact round closure.
            self.deferred_consequences.append(self._write_consequences)
            return
        if self.pending_change is not None:
            # I2 spec §5.4: hygiene accepted in this invocation precedes a new
            # block's `authorization_applied`; a resumed block's owed records
            # come first, since nothing else may interrupt it.
            if not self.change_resumed:
                self._write_hygiene_applications()
                if self.pending_change[5] == "rebaseline":
                    open_round = _open_round(self.snapshot())
                    if open_round is not None:
                        self._settle_open_operations(self.snapshot().rounds[open_round])
            self._write_condition_change()
        if self.pending_rebase is not None:
            _, rebuilt, differences, instance, authorization = self.pending_rebase
            changed = [difference.field for difference in differences]
            payload = self._identity_rebased_payload(
                rebuilt,
                differences,
                self.snapshot().identity_digest,
                instance,
                authorization.journal_payload(),
            )
            with _boundary("campaign_journal"):
                self.append("identity_rebased", payload)
            if "identity.reflector.environment_docs_digest" in changed:
                # §6.2: the run directory must end up holding the snapshot the
                # record's identity names. It is re-taken *after* the record is
                # durable, not before: a crash in between leaves a journal whose
                # digest is the live source's and a stored snapshot that is
                # merely stale, which the next resume notes during admission and
                # repairs after its own consequence records. Re-taking first
                # would instead leave a run directory no journal accounts for.
                self.environment_docs_repair = False
                self._retake_environment_docs()
            self.hook("after_identity_rebased")
            self.owed_store_rebases.append(
                dict(
                    campaign_rebase=str(rebuilt["identity_digest"]),
                    authorization_id=authorization.id,
                    producing_version=str(rebuilt["producing_version"]),
                )
            )
            self.pending_rebase = None
        self._write_hygiene_applications()
        if self.pending_fresh_baseline is not None:
            # Baseline inheritance §5.3, §7: the full decision and the declared
            # `pairs_digest` its instance digest was computed over.
            decision, instance, pairs = self.pending_fresh_baseline
            with _boundary("campaign_journal"):
                self.append(
                    "authorization_applied",
                    dict(
                        effect="fresh_baseline",
                        code="baseline_import_unavailable",
                        instance_digest=instance,
                        authorization=decision.decision_payload(),
                        pairs_digest=pairs,
                    ),
                )
            self.pending_fresh_baseline = None

    def _repair_environment_docs(self) -> None:
        """§6.2: re-take a stored copy admission found missing or damaged, once.

        A rebase that moved the documentation digest re-takes it after its
        record and clears the flag, which leaves nothing to do here.
        """

        if self.environment_docs_repair:
            self.environment_docs_repair = False
            self._retake_environment_docs()

    def _retake_environment_docs(self) -> None:
        """Re-take the run directory's documentation snapshot from the live source."""

        if self.environment_docs is None:
            return
        with _file_boundary("environment_docs_unwritable"):
            if isinstance(self.environment_docs, _V2DocumentSnapshot):
                self.environment_docs = _v2_repair_document_snapshot(
                    self.environment_docs, self.run_root
                )
            else:
                write_snapshot(self.environment_docs, self.run_root)

    def _complete_owed_store_rebases(self, store_identity: ArtifactStoreIdentityV1) -> None:
        """§6.2: the ledger half of a rebase declared while the store was open.

        The debt an *earlier* invocation left is paid by the store open itself --
        `ArtifactStore.resume(owed_rebases=...)` and `create(owed_rebases=...)`
        write those rows before any other record of the session. What is left
        here is this invocation's own declaration, appended by
        `_write_consequences` after the store was opened, and it is still
        written before the store publishes anything of this round.
        """

        if not self.owed_store_rebases:
            return
        assert self.store is not None
        owed, self.owed_store_rebases = self.owed_store_rebases, []
        with _boundary("artifact_store"):
            for entry in owed:
                self.store.rebase_ledger(store_identity, **entry)

    async def account_resumed(self) -> None:
        for measurement_id in self.measurements:
            self.account(measurement_id, self.blocked_rows(measurement_id))

    def resumed_unresponsive(self) -> bool:
        """Is a durable `editor_failed(unresponsive)` the campaign's terminal (§7)?

        True for any attempt of an unfinished campaign whose editor terminal is
        `failed(unresponsive)` — closed or not, since the process may have
        stopped on either side of `attempt_closed`. A finished campaign has its
        own last record and resume decides nothing.
        """

        snapshot = self.snapshot()
        if snapshot.finished is not None:
            return False
        return bool(_outstanding_causes(snapshot, "editor_unresponsive"))

    def resumed_reflector_unresponsive(self) -> bool:
        """Is a durable `reflection_failed(unresponsive)` the campaign's terminal?

        `resumed_unresponsive`'s twin for the other role, for the same window:
        the live path decides the terminal in memory right after the record is
        durable, so a process that stopped between the two left a journal saying
        a Reflector survived its grace and nothing more (spec §7, §11).
        """

        snapshot = self.snapshot()
        if snapshot.finished is not None:
            return False
        return bool(_outstanding_causes(snapshot, "reflector_unresponsive"))

    def resumed_resilience_cause(self) -> str | None:
        """Family 10: the oldest outstanding `RESILIENCE_SUSPENSIONS` cause of an
        unfinished prefix -- a durable failure whose suspension is owed -- else
        `None` (always `None` below family 10). Family 14 also owes the
        Grouper's `GROUPER_SUSPENSIONS` (F0 R46, plan decision 11)."""

        snapshot = self.snapshot()
        if snapshot.finished is not None:
            return None
        causes = sorted(
            (seq, reason)
            for seq, (reason, _operation) in snapshot.suspension_causes.items()
            if reason in RESILIENCE_SUSPENSIONS or reason in GROUPER_SUSPENSIONS
        )
        return causes[0][1] if causes else None

    async def reconcile_attempts(self) -> None:
        """Spec §11 step 6, one branch per durable editor prefix and reflection."""

        assert self.store is not None
        snapshot = self.snapshot()
        with _boundary("artifact_store"):
            lineage = self.store.inspect(self.run_root)
        published = {
            entry.attempt_id: entry
            for entry in lineage.candidates
            if entry.candidate_digest is not None
        }
        for state in snapshot.rounds.values():
            if state.closed is not None:
                continue
            # A reflection the interruption caught mid-call has a start and no
            # terminal; the lifecycle admits no second start while one is open,
            # and an `interrupted` terminal is not a diagnosis, so the planner
            # runs the round's next reflection (spec §11, §3.3). An adaptive
            # editor caught mid-call gets its `interrupted` terminal the same
            # way, in attempt order with the other Attempts' steps, and is
            # dispatched again.
            self._settle_round_operations(state)
            for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
                if attempt.outcome is not None:
                    continue
                if attempt.editor in (None, "started"):
                    self._settle_open_editor(attempt)
                    continue
                if editor_replayable(attempt, adaptive=True, resilient=True):
                    # Family 10 adds a `provider`/`unknown` failure (spec
                    # §5.3): its owed suspension is written before planning
                    # (`resumed_resilience_cause`), then it re-runs.
                    continue
                if attempt.editor in (None, "started"):
                    await self.close_attempt(
                        attempt.attempt_id, "abandoned", "interrupted", None, None
                    )
                    continue
                if attempt.editor == "declined":
                    await self.close_attempt(
                        attempt.attempt_id, "abandoned", "editor_declined", None, None
                    )
                    continue
                if attempt.editor == "failed":
                    reason = (
                        "editor_unresponsive"
                        if attempt.editor_error == "unresponsive"
                        else "editor_failed"
                    )
                    await self.close_attempt(attempt.attempt_id, "abandoned", reason, None, None)
                    continue
                if attempt.candidate_digest is None:
                    entry = published.get(attempt.attempt_id)
                    if entry is None or entry.candidate_digest is None:
                        await self.close_attempt(
                            attempt.attempt_id, "abandoned", "interrupted", None, None
                        )
                        continue
                    # The store already published this Candidate before the
                    # interruption; the workspace `run_editor` used is gone
                    # (`ArtifactStore.seal` removes it), so `candidate_leak`
                    # reads the published copy instead (spec §5.6, PDR-0007).
                    if self.refuse_leaked_seal(attempt.attempt_id):
                        continue
                    reference = self.publication_reference(attempt.attempt_id)
                    assert reference is not None
                    self.append(
                        "attempt_sealed",
                        dict(
                            attempt_id=attempt.attempt_id,
                            candidate_digest=entry.candidate_digest,
                            dependency_digest=entry.dependency_digest,
                            artifact_locator=CANDIDATE_LOCATOR + attempt.attempt_id,
                            evidence=[reference],
                        ),
                    )
                if self.attempt_state(self.snapshot(), attempt.attempt_id) is not None:
                    current = self.attempt_state(self.snapshot(), attempt.attempt_id)
                    assert current is not None


def _connection_suspension(owed: Mapping[str, Any]) -> dict[str, Any]:
    """`campaign_suspended{owner: connection}` naming the failed terminal it follows."""

    return dict(
        reason=str(owed["reason"]),
        owner="connection",
        evidence=dict(
            evidence_kind="stage_record",
            seq=int(owed["seq"]),
            operation_id=str(owed["operation_id"]),
        ),
    )


def _remove_unborn_probe(run_root: Path, operation_id: str) -> None:
    """Remove `connection/<op>/` when a crash left it with no durable record.

    As `campaign_inspection._open` replaces an unborn journal (ruling 22): a
    directory without a record holds nothing anything cites, and the
    operation id is never used again, so replacing it means removing it.
    Only that directory goes; the caller has read it as unborn.
    """

    path = Path(run_root) / CONNECTION_DIR / operation_id
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    except OSError as exc:
        raise _journal_fault(exc) from exc


def _triple(record: Mapping[str, Any]) -> dict[str, Any]:
    """The identity triple an `authorization_applied` records (comparable-links §5.1)."""

    return {
        "identity_digest": record["identity_digest"],
        "producing_version": record["producing_version"],
        "adaptation_digest": record.get("adaptation_digest"),
    }


def _changed_comparable(
    differences: Sequence[RefusalDifference], *, family_version: int | None = None
) -> list[str]:
    """The changed comparable top-level fields, as the lifecycle records them (§4.4)."""

    return sorted(
        {
            difference.field.split(".")[1]
            for difference in differences
            if comparable_field(difference.field, family_version=family_version)
        }
    )


def _open_round(snapshot: CampaignSnapshot) -> int | None:
    """The latest round that has begun and has no `round_closed` (I2 spec §3.1)."""

    begun = [
        number
        for number, state in snapshot.rounds.items()
        if state.baseline_measurement_id is not None and state.closed is None
    ]
    return max(begun) if begun else None


def _closed_seq(attempt: AttemptState) -> int:
    """A closed Attempt's `attempt_closed` sequence (family-13 `finalists_bound` cites it)."""

    assert attempt.closed_seq is not None
    return attempt.closed_seq


def _rejected_focus_attempts(snapshot: CampaignSnapshot) -> tuple[AttemptState, ...]:
    """Already decided rejections owe their old-epoch comparison and close first."""
    if snapshot.family_version != 15:
        return ()
    return tuple(
        a
        for state in snapshot.rounds.values()
        for a in state.attempts.values()
        if a.outcome is None
        and a.family15 is not None
        and a.family15.focus_decision is not None
        and a.family15.focus_decision.payload["outcome"] != "passed"
    )


def _rejected_focus_action(attempt: AttemptState) -> DecideFocus | CloseAttempt:
    assert attempt.family15 is not None and attempt.family15.focus_decision is not None
    if attempt.family15.comparison is None:
        return DecideFocus(attempt.attempt_id)
    return CloseAttempt(
        attempt.attempt_id, "rejected", attempt.family15.focus_decision.payload["reason"]
    )


def _selected_open_round(snapshot: CampaignSnapshot) -> int | None:
    """The open round when its selection is durable: a `rebaseline` waits for its close."""

    number = _open_round(snapshot)
    if number is None or snapshot.rounds[number].selection is None:
        return None
    return number


def _rebind_sources(snapshot: CampaignSnapshot, epoch: int) -> tuple[str, ...]:
    """What `continue` re-binds into `epoch`, in planner order (I2 spec §5.2, §5.4).

    Every unfinished measurement of an earlier epoch the planner would still
    append to (ruling R13): a closed round's promoted winner awaiting its
    completion, the open round's incomplete baseline, and the measurement of
    each open-round Attempt with no outcome or an `accepted` one (a candidate
    winner whose completion follows the round's close: every accepted one
    until the round's `selection_decided`, only the selected one after it --
    ADR-0020 as amended 2026-09-24), and the fresh Parent of each open-round
    Attempt with no outcome (G2 spec §3.6). In family 12 it is a promoted
    round's unfinished completion instead of its winner; a sealed winner and
    an accepted Attempt are never appended to (G2 spec §4.7).
    In family 13 (F7, D9, plan choice 6) also every current member of the
    open round's undecided cohort (no `selection_decided`) that has no
    `cohort_measurement_completed`, in `members` order, after the Attempts; a
    member bound after the block is bound in the new epoch under its plain
    id instead. A measurement recorded complete (`recorded_complete`, plan
    choice 14) is never re-bound -- a completed member serving as the open
    round's baseline among them.
    Rounds ascending, and inside the open round the baseline before the
    Attempts in attempt order, each Attempt's Candidate before its Parent,
    then the cohort members, which is the order `next_actions` reaches them.
    It is the executor's side of the lifecycle's `_append_target`.
    """

    open_round = _open_round(snapshot)
    sources: list[str] = []
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        if number == open_round:
            if not state.baseline_complete:
                sources.append(str(state.baseline_measurement_id))
            for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
                if (attempt.outcome is None) and attempt.measurement_id is not None:
                    sources.append(attempt.measurement_id)
                # G2 spec §3.6: an open Attempt's fresh Parent, after its
                # Candidate. An accepted Attempt's gate is done.
                if attempt.outcome is None and attempt.parent_measurement_id is not None:
                    sources.append(attempt.parent_measurement_id)
            if (
                snapshot.family_version == 15
                and snapshot.family15 is not None
                and state.selection is None
            ):
                sources.extend(
                    mid
                    for mid in snapshot.family15.current_measurements.values()
                    if snapshot.measurements[mid]["round"] == number
                    and snapshot.measurements[mid].get("role")
                    in {"family15_completion", "family15_final", "family15_champion"}
                )
            if state.cohort is not None and state.selection is None:
                # Plan choice 6: the members a `continue` re-binds, in
                # `members` order; one not yet bound has no entry (below).
                assert state.cohort_current is not None
                sources.extend(
                    member
                    for member in state.cohort_current
                    if member not in state.cohort_completed
                )
    ordered: dict[str, None] = {}
    for measurement_id in sources:
        entry = snapshot.measurements.get(measurement_id)
        if (
            entry is None
            or recorded_complete(entry, snapshot.rounds)
            or int(entry["epoch"]) >= epoch
        ):
            continue
        ordered[measurement_id] = None
    return tuple(ordered)


def _unfilled_slots(
    snapshot: CampaignSnapshot,
    measurement_id: str,
    view: Measurement | FrozenMeasurement,
    looks: Sequence[int],
) -> int:
    """The measurement's slots no try filled, over its journal `view` (frozen or live).

    `unfilled_slots` over the latest stage the campaign opened on it and the
    retries its accounted tries reserved -- a `retry_follows: true` try, not
    one the campaign declined. I2's `measurement_rebound.released` reads it
    over a frozen source; #109's `stage_redispatched` over a live measurement
    whose stage was carried across a suspension (a28 spec §3.1). No stage
    reserved nothing. The stage is read up to its horizon over the policy's
    `looks` (design §5): its look, or a `case_repeats` measurement's last
    repeat on its last stage.

    Family 13 (pre-F8 spec §2.4): a started try after a declined one is a
    restored D1 retry, which takes its grant's repair slot, not a stage slot
    (`declined`). #194 (G3): so is a started try after a cancelled one a
    Champion listing restored (`listing_restored`).
    """

    stages = snapshot.measurements[measurement_id]["stages"]
    if not stages:
        return 0
    reserved = {
        (str(key[1]), str(key[2]), int(key[3]))
        for key in snapshot.accounted
        if key[0] == measurement_id and len(key) == 4 and key not in snapshot.retries_declined
    }
    declined: frozenset[tuple[str, str, int]] = frozenset()
    declined = declined_tries(snapshot.retries_declined, measurement_id) | restored_tries(
        snapshot.listing_restored, measurement_id
    )
    horizon = stage_horizon(looks, int(stages[-1]["look"]), view.max_repeat)
    total = view.unfilled_slots(horizon, reserved, declined=declined)
    if snapshot.family_version == 15 and any(stage.get("scope") for stage in stages):
        keys = {
            (row["case_id"], row["sample_id"])
            for row in owed_rows(snapshot, measurement_id, opened_only=True)
        }
        total -= sum(
            row.disposition == "not_started" and (row.case_id, row.sample_id) not in keys
            for row in view.sample_rows(horizon)
        )
    return max(0, total)


def _carried(measurement: Measurement, plan: StagePlan) -> frozenset[tuple[str, str]]:
    """The rows of a stage its measurement already holds as `sample_imported` (§3.3)."""

    keys = {(row["case_id"], row["sample_id"]) for row in plan.rows}
    return frozenset(
        (row.case_id, row.sample_id)
        for row in measurement.sample_rows(plan.horizon)
        if row.imported and (row.case_id, row.sample_id) in keys
    )


def _remaining(
    measurement: Measurement,
    plan: StagePlan,
    repairs: frozenset[tuple[str, str]] = frozenset(),
    withheld: frozenset[tuple[str, str]] = frozenset(),
) -> StagePlan:
    """The rows of an opened stage that still owe a try.

    A durable try is never re-dispatched — S1 does not replay a nondeterministic
    role — so a resumed stage runs exactly what the interruption left undone: a
    row with no execution terminal, and (R8b, continuable-terminals spec §3.4)
    a row whose latest try left it unscored while a further try is admitted.
    `Measurement.run_stage` takes the next ordinal for the latter itself.
    Family 7 adds the fault rows the latest suspension still owes a repair try
    (`repairs`, I1 spec §5.4), once each. Family 13 (pre-F8 spec §2.8) keeps
    out the `withheld` rows: a declined try no grant names stays a gap, and
    no re-dispatch for another owed repair retries it.
    """

    pending = {
        (row.case_id, row.sample_id)
        for row in measurement.sample_rows(plan.horizon)
        if row.disposition == "not_started"
        or (
            row.disposition in ("indeterminate", "unscored")
            and not row.exhausted
            and (row.case_id, row.sample_id) not in withheld
        )
        or (row.case_id, row.sample_id) in repairs
    }
    rows = tuple(row for row in plan.rows if (row["case_id"], row["sample_id"]) in pending)
    return replace(plan, rows=rows)


def _full_outcome(
    measurement: Measurement,
    plan: StagePlan,
    policy: CampaignPolicyV11,
    outcome: StageOutcome,
    repairs: frozenset[tuple[str, str]] = frozenset(),
) -> StageOutcome:
    """Re-read the stage's facts over every row of the full plan, not the dispatch.

    `faults` also reads the rows owed a repair try (family 7), whose stage may
    be an earlier one. `horizon` is the full plan's too (design §5): a resumed
    dispatch of a `case_repeats` last stage may owe only rows below it.
    """

    keys = {(row["case_id"], row["sample_id"]) for row in plan.rows}
    rows = [
        row for row in measurement.sample_rows(plan.horizon) if (row.case_id, row.sample_id) in keys
    ]
    latest = {(row.case_id, row.sample_id): row.retry_ordinal for row in rows}
    return replace(
        outcome,
        horizon=plan.horizon,
        terminals=sum(1 for row in rows if row.disposition != "not_started"),
        # Decisions read each sample's *latest* try (R8a): a sample that failed
        # once and then scored is not indeterminate here, and one still owed a
        # try is (it keeps the stage open until it settles).
        indeterminate=tuple(
            row
            for row in measurement.indeterminate_rows()
            if (row["case_id"], row["sample_id"]) in latest
            and row["retry_ordinal"] == latest[(row["case_id"], row["sample_id"])]
        ),
        faults=tuple(
            row
            for row in measurement.fault_rows(policy.metric_primary)
            if (row["case_id"], row["sample_id"]) in keys | repairs
        ),
        critical_failed=any(row.critical_pass is False for row in rows),
    )


def _missing_rows(
    rows: Sequence[SampleRow], measurement_id: str, look: int
) -> list[dict[str, Any]]:
    """One entry per blocking row of one side, in `look_evaluable`'s own order."""

    entries: list[dict[str, Any]] = []
    for row in rows:
        if row.repeat > look or row.disposition == "score":
            continue
        entries.append(
            dict(
                measurement_id=measurement_id,
                case_id=row.case_id,
                sample_id=row.sample_id,
                state=row.disposition,
            )
        )
    return entries


def _remove_unborn_store(run_root: Path) -> None:
    """Remove an S2 store that never wrote its identity record (spec §4 step 3)."""

    import shutil

    for name in (LEDGER_FILE, ".artifacts.lock"):
        path = run_root / name
        if path.exists():
            path.unlink()
    artifacts = run_root / "artifacts"
    if artifacts.is_dir():
        shutil.rmtree(artifacts)


# -- the linked run -------------------------------------------------------


def _linkable_families(expected_family: int) -> tuple[int, ...]:
    """New links admit only current predecessor families (ADR-0046)."""

    if expected_family == 15:
        return (FINAL_CAMPAIGN_FAMILY_VERSION, 15)
    if expected_family == FINAL_CAMPAIGN_FAMILY_VERSION:
        return (FINAL_CAMPAIGN_FAMILY_VERSION,)
    return ()


@dataclass(frozen=True)
class _Predecessor:
    """The read-only replay of a predecessor, and the facts a link derives from it."""

    reduced: CampaignSnapshot
    prefix_digest: str
    #: The identity the prefix ends on: creation's, or the latest rebase's.
    identity: dict[str, Any]
    family: int
    #: The highest round number any record of the prefix names, closed or not.
    highest_round: int | None
    #: The `linked_from` payload of the identity the prefix ends on, or `None`
    #: for a predecessor that was itself not linked (D2a §4: the lineage
    #: caveats the successor carries forward are derived from it).
    linked: Mapping[str, Any] | None = None
    #: `(round, share)` of the alpha row an unfinished prefix owes: set when its
    #: last record is a `look_evaluated`, whose `alpha_consumed` the lifecycle
    #: requires next (D2a §5.4: the successor inherits every alpha the prefix
    #: charged, and an evaluated look has charged its share).
    owed_alpha: tuple[int, str] | None = None
    #: Every `gate.mode` the creation's and each rebase's identity journaled:
    #: empty below family 13, whose policies carry no mode (D10, E3).
    gate_modes: frozenset[str] = frozenset()


def _predecessor(run_root: Path, *, expected_family: int) -> _Predecessor:
    """Replay a predecessor read-only in its own linkable family (spec §6.3, §7)."""

    from evolution.tree.campaign_projections import read_family_version

    family = read_family_version(run_root)
    if family is None or family not in _linkable_families(expected_family):
        raise LineageFamilyUnsupported(family)
    snapshot = inspect_log(
        run_root,
        file_name=CAMPAIGN_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=lambda: CampaignLifecycle(selector=None, family_version=family),
        envelope_family=campaign_family_for(family),
    )
    if not snapshot.records:
        raise LineageNotFinished()
    # §6.2: a continuation reads the predecessor's *latest* identity record,
    # which an authorized rebase may have moved. The producing version that
    # wrote it is not compared at all: a link reads its predecessor and never
    # resumes it, so any producing version of a linkable family is admitted
    # (continuable-terminals spec §7). A finished campaign takes no authorization, so comparing it
    # would leave a campaign finished by one release impossible to continue by
    # the next. Resume keeps its own version rule (`journal_identity`).
    latest = _latest_identity_payload(snapshot.records)
    reduced = reduce(snapshot.records, selector=None, family_version=family)
    named = list(reduced.rounds)
    named.extend(
        record.payload["round"]
        for record in snapshot.records
        if type(record.payload.get("round")) is int
    )
    owed_alpha: tuple[int, str] | None = None
    last = snapshot.records[-1]
    if last.event == "look_evaluated":
        look_round = next(
            number
            for number, state in reduced.rounds.items()
            if last.payload["attempt_id"] in state.attempts
        )
        owed_alpha = (look_round, str(last.payload["alpha"]))
    return _Predecessor(
        reduced=reduced,
        prefix_digest=snapshot.prefix_digest,
        identity=dict(latest["identity"]),
        family=family,
        highest_round=max(named) if named else None,
        linked=(
            None
            if latest["identity"].get("linked_from") is None
            else dict(latest["identity"]["linked_from"])
        ),
        owed_alpha=owed_alpha,
        # E3: every identity's mode, not only the latest one's. Only a
        # family-13 (V10) policy has a `gate.mode`, and every one has it.
        gate_modes=(
            frozenset(
                str(record.payload["identity"]["policy"]["gate"]["mode"])
                for record in snapshot.records
                if record.event in ("campaign_created", "identity_rebased")
            )
            if family >= 13
            else frozenset()
        ),
    )


def _verify_champion(run_root: Path, reduced: CampaignSnapshot) -> None:
    """The journal's final champion copy must re-hash to a champion of the ledger.

    Spec §5.1: `lineage_champion_unverified`, before any write. The store's
    ledger is re-verified byte by byte (`verify=True`), and the champion the
    campaign journal reduced to -- its version and both digests -- must be one
    of the ledger's champions, uncorrupted, whose copy under
    `artifacts/champions/<version>` re-hashes.

    It need not be the ledger's *latest* (codex r1, PDR-0011): a crash between
    the store's promotion and the journal's `round_closed(promoted)` leaves the
    ledger one champion ahead, and a campaign then closed or finished from that
    prefix still finished on the journal's champion -- the one
    `campaign_outcome.champion_path` names and a successor seeds from. The
    later champion was never the campaign's. Read-only: nothing is written to
    the predecessor, and a champion absent from the ledger or corrupted is
    still refused.
    """

    try:
        projection = ArtifactStore.inspect(run_root, verify=True)
    except (ArtifactError, JournalBoundaryError, OSError, ValueError):
        raise LineageChampionUnverified() from None
    version, candidate_digest = reduced.champion
    if not any(
        champion.version_id == version
        and champion.corruption is None
        and champion.candidate_digest == candidate_digest
        and champion.dependency_digest == reduced.champion_dependency_digest
        for champion in projection.champions
    ):
        raise LineageChampionUnverified()


class LinkDerivation(NamedTuple):
    """What a new run's link derives from its predecessor, read-only (§5, §7).

    `source` is the predecessor's champion measurement, read only when the
    manifest declares `baseline: inherit` (baseline inheritance §5.0, §5.1).
    """

    linked: LinkedFrom
    predecessor_identity: dict[str, Any]
    source: SourceMeasurement | None = None


def derive_linked_from(
    run_root: Path,
    *,
    expected_family: int,
    authorizations: Sequence[Authorization] = (),
    baseline: Literal["fresh", "inherit"] = "fresh",
    successor_mode: GateMode | None = None,
) -> LinkDerivation:
    """Derive the inherited ledger from the predecessor's own journal (spec §5, §7).

    Any finished predecessor of a linkable family is admitted, whatever its
    continuation (continuable-terminals spec §5.1, §6.3). A family-6 successor
    starts one round after the highest round any predecessor record names
    (§5.2), and inherits the lineage's counts and denominator as totals: a
    family-6 predecessor's own tries plus the lineage it inherited itself.

    Comparable-links spec §5.4: an unfinished family-5 predecessor is admitted
    under an accepted `lineage_not_finished` decision bound to its `run_id` and
    prefix digest, so one that grew since the refusal is another instance. The
    link then inherits every alpha the prefix charged -- including the share of
    a trailing `look_evaluated` whose `alpha_consumed` it still owes -- and
    carries `predecessor_finished: false` with the full decision. An unfinished
    family-6 predecessor stays hard: its doors are resume and the close (§5.2).

    Baseline inheritance §5.0: with `baseline="inherit"` the predecessor's
    champion measurement is read here, after its champion is verified and
    before anything contacts the target or writes; one that cannot be read is
    `BaselineSourceUnavailable`.

    Family-13 D10: `successor_mode` is the successor's `gate.mode` (`None`
    below family 13). A `statistical` successor refuses a predecessor whose
    creation or any rebase ran `observe` (`LineageModeUnsupported`, E3),
    after the family check -- a family-12 policy carries no mode -- and before
    the predecessor's finish, since finishing it would not change the answer.
    """

    root = Path(run_root)
    predecessor = _predecessor(root, expected_family=expected_family)
    if successor_mode == "statistical" and "observe" in predecessor.gate_modes:
        raise LineageModeUnsupported()
    reduced = predecessor.reduced
    decision: Authorization | None = None
    if reduced.finished is None:
        raise LineageNotFinished()
    _verify_champion(root, reduced)
    source = read_source(root, reduced) if baseline == "inherit" else None
    closed = [number for number, state in reduced.rounds.items() if state.closed is not None]
    rounds_closed = max(closed) if closed else reduced.first_round - 1
    first_round: int | None = None
    highest = (
        predecessor.highest_round
        if predecessor.highest_round is not None
        else reduced.first_round - 1
    )
    first_round = max(highest, rounds_closed) + 1
    counts = dict(reduced.counts)
    denominator = reduced.denominator
    if reduced.lineage_counts is not None:
        counts = {owner: count + reduced.lineage_counts[owner] for owner, count in counts.items()}
        denominator += reduced.lineage_denominator or 0
    alpha_rounds = dict(reduced.alpha_rounds)
    linked = LinkedFrom(
        run_id=reduced.run_id,
        prefix_digest=predecessor.prefix_digest,
        final_champion_digest=reduced.champion[1],
        final_champion_dependency_digest=reduced.champion_dependency_digest,
        rounds_closed=rounds_closed,
        alpha_rounds=alpha_rounds,
        indeterminate_counts=counts,
        denominator=denominator,
        first_round=first_round,
        predecessor_family=(predecessor.family),
        # D2a §4: the bounded per-kind aggregate of every caveat up the chain.
        # A family-5 predecessor's link has no such key and no condition
        # change, so it contributes nothing; the key is omitted when empty.
        lineage_caveats=(
            aggregate_lineage(
                predecessor.linked,
                reduced.run_id,
                predecessor_baseline_abandoned=reduced.baseline_abandoned is not None,
                # I2 §6.3: the predecessor's in-place changes, one folded item
                # -- for a family-7 successor only. A family-6 one (PDR-0016)
                # journals what 0.3.0a17
                # would: it sees the change through the ordinary
                # `condition_changed` comparison against the latest identity.
                predecessor_changes=reduced.condition_changes,
                in_place=True,
            )
        ),
        # §5.4: absent for a finished predecessor, so every other link's
        # payload -- and its identity digest -- is unchanged.
        predecessor_finished=None if decision is None else False,
        authorization=None if decision is None else decision.decision_payload(),
        # H12a: the successor's family frames the declared baseline's field
        # names (a bare `metric_contract` from family 14). Never journaled.
        successor_family=(expected_family),
    )
    return LinkDerivation(linked, predecessor.identity, source)


def _comparable_projection(identity: Mapping[str, Any]) -> dict[str, Any]:
    """The identity fields spec §6.4 compares across a link, as `identity_differences` reads them.

    `policy_digest` is recomputed over the policy payload without
    `sample_retries`, so a retry limit never refuses a link and a family-5
    `/4` policy compares by the same projection. The artifact store compares its
    policy digest only.
    A family-13 predecessor has no `metric_contract`; it projects as `None` (R17).
    """

    projected = {field: identity.get(field) for field in _COMPARABLE if field != "policy_digest"}
    projected["policy_digest"] = hashlib.sha256(
        canonical_json(comparable_policy_payload(identity["policy"]))
    ).hexdigest()
    projected["artifact_store"] = {"policy_digest": identity["artifact_store"]["policy_digest"]}
    return projected


def link_differences(
    identity: CampaignIdentityV1, predecessor: Mapping[str, Any], linked: LinkedFrom, scan: Any
) -> tuple[RefusalDifference, ...]:
    """Every differing comparable field across the link (D2a §3; spec §6.3, §6.4).

    Reported, never refused (PDR-0012): the caller records the dotted fields as
    `linked_from.condition_changed` and prints one `differs:` line per field --
    fields and digests, never values. `identity` must be built with the link
    *before* `condition_changed` is set, which the comparison never reads.
    """

    differences = list(
        identity_differences(
            {"identity": _comparable_projection(predecessor)},
            {"identity": _comparable_projection(identity.to_payload())},
        )
    )
    for key, journaled, observed in (
        ("candidate_digest", linked.final_champion_digest, scan.candidate_digest),
        ("dependency_digest", linked.final_champion_dependency_digest, scan.dependency_digest),
    ):
        if journaled != observed:
            differences.append(RefusalDifference(f"seed_root.{key}", journaled, observed))
    return tuple(differences)


# -- entry points ---------------------------------------------------------


def _validate_run_id(run_id: str | None) -> None:
    """ADR-0007: an id a `<run_id>-<32 hex>` instance id can still be built from.

    `HttpSessionAdapter` prefixes every instance id it accepts with the `run_id`
    it was built on, so a campaign id the wire grammar would refuse is a caller
    bug that only shows up at the first materialization. It is refused here, on
    the way in, before the run root is touched (W1 §6 step 5).
    """

    if run_id is None:
        return
    if IDENTIFIER.match(run_id) is None or len(run_id) > MAX_RUN_ID:
        raise ValueError("run_id_invalid")


async def run_campaign(
    policy: CampaignPolicyV11,
    *,
    run_root: Path,
    seed_root: Path,
    artifact_policy: ArtifactPolicyV1,
    adapter: PromotionCapableAdapter,
    environment: Environment,
    scorer: Scorer,
    editor: CandidateEditor,
    reflector: CandidateReflector,
    environment_docs: EnvironmentDocs | _V2DocumentSnapshot | None = None,
    environment_docs_repair: bool = False,
    corpus: Sequence[EvalCase],
    linked_from: Path | None = None,
    run_id: str | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    secrets: Sequence[str] = (),
    secret_paths: Sequence[str] = (),
    secret_names: Sequence[str | None] = (),
    _hooks: Mapping[str, Callable[[], None]] | None = None,
    adaptive: AdaptiveCampaignRuntimeConfig | None = None,
    probe: ProbeSection | None = None,
    authorizations: Sequence[Authorization] = (),
    hygiene_applications: Sequence[tuple[HygieneFinding, Authorization]] = (),
    on_condition_changed: Callable[[tuple[RefusalDifference, ...]], None] | None = None,
    pending_findings: Sequence[HygieneFinding] = (),
    linked_baseline: Literal["fresh", "inherit"] = "fresh",
    connection: ConnectionContract | None = None,
    on_baseline_inherited: Callable[[BaselineImport, bool], None] | None = None,
    process_log: Callable[[Path], None] | None = None,
    notes: Callable[[str], None] | None = None,
    bind_hits: Sequence[Mapping[str, Any]] = (),
) -> CampaignResult:
    """Run a campaign in a fresh run root (spec §4 declare-then-create, §6, §7).

    `process_log` signals the last refusal point once, ignoring its errors (ADR-0036).

    A family outside `_ADMITTED_CAMPAIGN_FAMILIES` (currently every
    family but 14 or 15) is refused with `CampaignFamilyUnsupported` before the run
    root is touched (PDR-0026, ADR-0030).

    `connection` is family 14's `ConnectionContract`, required for family 14
    and refused for every other (R17).

    `secret_paths` names each of `secrets` by the manifest path of its name
    (`evolve._run_secret_paths`), for the connection probe's refusal of a
    description that holds one (#287); a secret beyond it is named
    `secrets[<index>]`. `secret_names` carries each one's declared environment
    name beside its path (`evolve._run_secret_names`), for the order by sorted
    name within a role (ADR-0029 amendment 2026-10-21).

    `run_id` is the id the caller shares with the target adapter (W1 §6 step 5);
    `None` keeps C1's own generated one. `on_condition_changed` receives, once
    and before admission, the comparable fields that differ across the link,
    when any do (D2a §3). `pending_findings` is the run directory's collected
    hygiene no decision accepted (D2a §6): a `LineageNotFinished` carries them,
    and otherwise `FindingsPending` refuses them before the target is contacted.

    `linked_baseline="inherit"` (baseline inheritance §4.2, §5.1) reads the
    predecessor's champion measurement before the target is contacted, journals
    the admitted set's declaration in `linked_from.baseline` -- even an empty
    one (ruling R1) -- and births the first-round baseline with its rows.
    `on_baseline_inherited` receives, once and before admission, the admitted
    set and whether the seed tree differs from the predecessor's champion.
    """

    _validate_run_id(run_id)
    _admit_active_runtime(policy, adaptive, connection)
    _refuse_unadmitted_family(_runtime_family_version(policy, adaptive))
    root = Path(run_root)
    siblings: tuple[str, ...] = (
        CAMPAIGN_FILE,
        LEDGER_FILE,
        MEASUREMENTS_DIR,
        INSPECTIONS_DIR,
        CONNECTION_DIR,
    )
    assert adaptive is not None
    for name in siblings:
        if (root / name).exists():
            raise CampaignExists("run_root_not_empty")
    campaign = _Campaign(
        policy,
        run_root=root,
        seed_root=seed_root,
        artifact_policy=artifact_policy,
        adapter=adapter,
        environment=environment,
        scorer=scorer,
        editor=editor,
        reflector=reflector,
        environment_docs=environment_docs,
        environment_docs_repair=environment_docs_repair,
        corpus=corpus,
        cancelled=cancelled,
        secrets=secrets,
        secret_paths=secret_paths,
        secret_names=secret_names,
        hooks=_hooks,
        process_log=process_log,
        notes=notes,
        bind_hits=bind_hits,
        adaptive=adaptive,
        connection=connection,
        probe=probe,
        authorizations=authorizations,
        hygiene_applications=hygiene_applications,
        pending_findings=pending_findings,
    )
    # W1 §6 step 5: the caller's id when it has one to share with the target
    # adapter, C1's own otherwise, for a library caller that passes nothing —
    # generated here so the creation probe and `create` share exactly one id.
    run_id = run_id if run_id is not None else f"c-{uuid4().hex}"
    linked: LinkedFrom | None = None
    predecessor: Mapping[str, Any] | None = None
    source: SourceMeasurement | None = None
    if linked_from is not None:
        # §5.4: the re-validated decisions `_Campaign` holds, so a link from
        # an unfinished family-5 predecessor matches exactly what the
        # identity path would.
        linked, predecessor, source = derive_linked_from(
            Path(linked_from),
            expected_family=campaign.family_version,
            authorizations=campaign.authorizations,
            baseline=linked_baseline,
            successor_mode=campaign.policy.gate_mode,
        )
    # Comparable-links spec §6: a new run directory's collected hygiene is
    # reported after the link finding and refused before the target is
    # contacted and before the run root is written.
    if campaign.pending_findings:
        raise FindingsPending(campaign.pending_findings)
    store_identity, preflight = await campaign.preflight()
    identity = campaign.build(store_identity, preflight, linked)
    if linked is not None and predecessor is not None:
        differences = link_differences(
            identity, predecessor, linked, scan_tree(Path(seed_root), artifact_policy)
        )
        if differences:
            # PDR-0012 / D2a §3: the manifest is the declaration; the tool
            # reports. `linked_from` is part of the identity, so the identity is
            # rebuilt with the noted link rather than patched; `build` is pure
            # over its arguments, so only the link differs.
            linked = replace(
                linked, condition_changed=tuple(sorted({d.field for d in differences}))
            )
            identity = campaign.build(store_identity, preflight, linked)
            if on_condition_changed is not None:
                on_condition_changed(differences)
    if linked is not None and source is not None:
        # Baseline inheritance §5.1: the admission set is computed against the
        # successor's own first-round baseline identity, once `condition_changed`
        # is final, and declared in the creation record before any row is
        # copied. Ruling R1: an empty set is declared too (`imported: 0`), and
        # nothing is then owed.
        target = measurement_identity(
            identity, campaign.corpus, store_identity.seed_candidate_digest, "champion"
        )
        imported = admit_rows(
            source,
            target,
            primary=policy.metric_primary,
            condition_changed=linked.condition_changed,
            dotted=False,
            # ADR-0019 (amended 2026-10-03): the creation record's own version.
            primary_range=primary_range_required(
                campaign.family_version, resolve_package_version()
            ),
        )
        linked = replace(linked, baseline=imported.declaration())
        identity = campaign.build(store_identity, preflight, linked)
        campaign.pending_import = imported.rows
        assert linked_from is not None
        campaign.prepare_link_diagnostics(
            source, imported.rows, target, source_root=Path(linked_from)
        )
        if on_baseline_inherited is not None:
            on_baseline_inherited(
                imported,
                any(field.startswith("seed_root.") for field in linked.condition_changed),
            )
    # Admission before the run root exists (spec §3): a corpus the creation
    # record cannot hold, or one whose strings match a secret, is refused here
    # -- `CorpusTooLarge` / `RecordMatchesSecret` propagate uncaught, and
    # nothing of this campaign is created.
    record = campaign.admit_creation(identity, run_id)
    try:
        try:
            campaign.create(identity, store_identity, run_id, record)
        except _Fault as exc:
            # A campaign whose own log never opened has no durable prefix and no
            # writer state to project, but it is still a §6 terminal the operator
            # must be able to read: a run root that passes admission and preflight
            # and then refuses `.campaign.lock` -- an empty directory the process
            # cannot write into -- is `persistence_failed`, not a traceback out of
            # `evolve()`. It takes the same `_unopened` shaping the resume paths
            # use, whose projection tolerates a prefix that names nothing.
            if campaign.log is None:
                return _unopened(campaign, exc)
            campaign.fault = exc
            await campaign.barrier()
            campaign.write_terminal()
            return campaign.result()
        return await _finish(campaign)
    except (IdentityError, AuthorizationError, AuthorizationConflict) as exc:
        _mark_first_write(exc, campaign)
        raise
    finally:
        campaign.close()


async def resume_campaign(
    policy: CampaignPolicyV11,
    *,
    run_root: Path,
    seed_root: Path,
    artifact_policy: ArtifactPolicyV1,
    adapter: PromotionCapableAdapter,
    environment: Environment,
    scorer: Scorer,
    editor: CandidateEditor,
    reflector: CandidateReflector,
    environment_docs: EnvironmentDocs | _V2DocumentSnapshot | None = None,
    environment_docs_repair: bool = False,
    corpus: Sequence[EvalCase],
    run_id: str | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    secrets: Sequence[str] = (),
    secret_paths: Sequence[str] = (),
    secret_names: Sequence[str | None] = (),
    _hooks: Mapping[str, Callable[[], None]] | None = None,
    adaptive: AdaptiveCampaignRuntimeConfig | None = None,
    probe: ProbeSection | None = None,
    authorizations: Sequence[Authorization] = (),
    hygiene_applications: Sequence[tuple[HygieneFinding, Authorization]] = (),
    pending_findings: Sequence[HygieneFinding] = (),
    linked_from: Path | None = None,
    connection: ConnectionContract | None = None,
    process_log: Callable[[Path], None] | None = None,
    notes: Callable[[str], None] | None = None,
    bind_hits: Sequence[Mapping[str, Any]] = (),
) -> CampaignResult:
    """Continue a campaign from its last durable boundary (spec §11 steps 0-7).

    `process_log` signals the last refusal point once, ignoring its errors (ADR-0036).

    A journaled family outside `_ADMITTED_CAMPAIGN_FAMILIES` (currently
    every family but 14 or 15) is refused with `CampaignFamilyUnsupported` before
    the log is opened; a legible journal's own family decides it, not the
    policy passed (PDR-0026, ADR-0030). A policy whose own family is not
    admitted is refused the same way first, so an older policy cannot reach an
    admitted journal's owed terminal before `preflight` refuses it.

    `connection` is family 14's `ConnectionContract`, required for family 14
    and refused for every other (R17). `secret_paths` and `secret_names` are
    `run_campaign`'s.

    `run_id` is what the caller built its adapter on; the journal is the
    authority, and a differing id is refused in `adopt` before any new record
    (W1 §6 step 5). `pending_findings` is the run directory's collected hygiene
    no decision accepted (D2a §6): an identity refusal carries them, and
    otherwise `adopt` refuses them as `FindingsPending` before its first write.

    `linked_from` is the manifest's predecessor run directory. It is read only
    while a declared baseline copy is still owed (baseline inheritance §5.2);
    one that cannot yield the declared set is `BaselineImportUnavailable`.
    """

    _validate_run_id(run_id)
    _admit_active_runtime(policy, adaptive, connection)
    _refuse_unadmitted_family(_runtime_family_version(policy, adaptive))
    root = Path(run_root)
    if not (root / CAMPAIGN_FILE).exists():
        raise CampaignMissing("campaign_absent")
    from evolution.tree.campaign_projections import (  # circular at module scope
        CampaignUnreadable,
        read_creation_adaptation_version,
        read_creation_policy,
        read_family_version,
    )

    expected_family = _runtime_family_version(policy, adaptive)
    try:
        family = read_family_version(root)
        # I1 spec §6: the journal's own creation policy (and, for family 8,
        # its adaptation profile) chose its family, so a family-6 run resumes
        # as family 6 whatever policy this resume carries (an accepted
        # identity change may have moved it to `/8`), and a family-7 run as
        # family 7 under a `/9` manifest.
        selected = _family_for_policy_class(
            policy_class_for_payload(read_creation_policy(root)),
            read_creation_adaptation_version(root),
        )
        _refuse_misfiled(selected, family)
        expected_family = min(selected, _ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION)
    except CampaignUnreadable:
        # Illegible is C1's own persistence classification, made by the scan
        # below over the opened log; the probe only recognizes families.
        family = expected_family
    except PolicyNotAdmitted:
        # A V10 creation policy (`gate.mode`) in a journal whose own family is
        # not 13, or a family-13 journal whose creation policy is not V10, is
        # malformed evidence (ADR-0008, `_refuse_misfiled`): the journal's
        # own family, when this reader has it, scans and classifies it. Any
        # other journaled family keeps the typed refusal.
        if type(family) is int and 6 <= family <= _ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION:
            expected_family = family
    if family != expected_family:
        raise CampaignFamilyUnsupported(family, reader=expected_family)
    _refuse_unadmitted_family(expected_family)
    campaign = _Campaign(
        policy,
        run_root=root,
        seed_root=seed_root,
        artifact_policy=artifact_policy,
        adapter=adapter,
        environment=environment,
        scorer=scorer,
        editor=editor,
        reflector=reflector,
        environment_docs=environment_docs,
        environment_docs_repair=environment_docs_repair,
        corpus=corpus,
        cancelled=cancelled,
        secrets=secrets,
        secret_paths=secret_paths,
        secret_names=secret_names,
        hooks=_hooks,
        process_log=process_log,
        notes=notes,
        bind_hits=bind_hits,
        adaptive=adaptive,
        connection=connection,
        probe=probe,
        authorizations=authorizations,
        hygiene_applications=hygiene_applications,
        pending_findings=pending_findings,
        family_version=expected_family,
    )
    campaign.linked_from_path = None if linked_from is None else Path(linked_from)
    # The id this resume was attempted under, before the journal has been read.
    # `adopt` replaces it with the journaled one and refuses a mismatch; until
    # then it is the only id a result built over an illegible prefix can name.
    campaign.run_id = run_id or ""
    if classify_store(root) == "born":
        try:
            with _boundary("artifact_store"):
                ArtifactStore.preflight(
                    root,
                    Path(seed_root),
                    artifact_policy,
                    policy.parallel_attempts,
                    inodes=campaign.inode_floor(fresh=False),
                    _estimate_observer=campaign.note_inodes,
                )
        except _Fault as exc:
            return _unopened(campaign, exc)
    try:
        campaign.open_log(create=False)
    except _Fault as exc:
        return _unopened(campaign, exc)
    try:
        try:
            with _boundary("campaign_journal"):
                await campaign.adopt(run_id)
        except _Fault as exc:
            # `adopt` is what makes the writer's own state real: a fault raised
            # inside it -- a corrupt suffix the append log's scan refuses, an S1
            # or S2 boundary error while the siblings are adopted -- leaves that
            # state empty, so the result must be built from the valid prefix by
            # inspection, exactly as a resume that never opened the log builds
            # it. Projecting the un-adopted writer instead reports nothing and
            # the projection's own validator refuses it (§7, §11 step 0).
            await campaign.barrier()
            return _unopened(campaign, exc)
        if campaign.closed:
            # Comparable-links spec §5.2: an accepted close (or owed terminal)
            # wrote its two records inside `adopt` and nothing else runs --
            # no drive step, no measurement, no store.
            return campaign.result()
        if campaign.fault is not None:
            await campaign.barrier()
            campaign.write_terminal()
            return campaign.result()
        return await _finish(campaign)
    except (IdentityError, AuthorizationError, AuthorizationConflict) as exc:
        _mark_first_write(exc, campaign)
        raise
    finally:
        campaign.close()


def owes_applied_terminal(run_root: Path) -> bool:
    """Does this run directory's valid prefix end on an applied close or terminal? (§5.3)

    Read-only and best effort, like the first half of `finish_owed_terminal`:
    an absent, unreadable or other-family journal answers `False`. `evolve`
    asks it only when the run directory's own link is unaccepted, to refuse
    that one finding alone -- accepting it is all the recovery needs -- rather
    than send the operator to decide an identity change the journal already
    decided (comparable-links spec §6).
    """

    return _applied_terminal_prefix(Path(run_root)) is not None


def _applied_terminal_prefix(root: Path) -> tuple[Snapshot, int] | None:
    """The read-only scan of a family-14 or -15 prefix ending on an applied
    close/terminal, with the family it was read in, or `None`."""

    from evolution.tree.campaign_projections import (  # circular at module scope
        CampaignUnreadable,
        read_family_version,
    )

    if not (root / CAMPAIGN_FILE).exists():
        return None
    try:
        journaled = read_family_version(root)
        if journaled is None or journaled not in (
            FINAL_CAMPAIGN_FAMILY_VERSION,
            15,
        ):
            return None
        family = journaled
        observed = inspect_log(
            root,
            file_name=CAMPAIGN_FILE,
            max_record_bytes=MAX_RECORD_BYTES,
            max_log_bytes=MAX_JOURNAL_BYTES,
            state_factory=lambda: CampaignLifecycle(selector=None, family_version=family),
            envelope_family=campaign_family_for(family),
        )
    except (CampaignMissing, CampaignUnreadable, JournalBoundaryError, SchemaError, OSError):
        return None
    if not observed.records:
        return None
    last = observed.records[-1]
    if last.event != "authorization_applied" or last.payload.get("effect") not in (
        "close",
        "terminal",
    ):
        return None
    return observed, family


def finish_owed_terminal(
    run_root: Path, *, process_log: Callable[[Path], None] | None = None
) -> CampaignResult | None:
    """Comparable-links spec §5.3: write the terminal an applied decision owes.

    `process_log` fires once before the owed append, ignoring its errors (ADR-0036).

    A crash between `authorization_applied(effect: close|terminal)` and
    `campaign_finished` leaves a prefix that owes exactly one record, and the
    prefix alone names it (`CampaignSnapshot.owed_terminal`). Recovery is
    state-first: no binding, no target, no identity comparison, no
    environment-documentation check, no sibling. The journaled decision is
    standing authority (ADR-0015); the terminal carries no operator string, so
    no secret set applies.

    `None` unless the root holds a family-6 to -14 journal whose valid prefix ends
    on such a decision: the ordinary path then handles the run directory. Once the
    prefix is known to owe its terminal, every journal fault is this function's
    to report, exactly as the ordinary resume path would -- a
    `persistence_failed` or `storage_capacity_exhausted` result over the
    campaign journal, with `_inspected_status` as its status -- so a manifest
    that cannot bind or a target that is down never masks it. The one
    exception is a second writer holding the campaign lock:
    `JournalFault("journal_busy")` propagates, and `evolve` refuses it.
    """

    from evolution.tree.campaign_projections import inspect_view  # circular at module scope

    root = Path(run_root)
    # Read-only first: a run directory that owes nothing is never locked,
    # repaired or truncated by this check.
    applied = _applied_terminal_prefix(root)
    if applied is None:
        return None
    observed, family = applied
    run_id = str(observed.records[0].to_payload()["run_id"])

    def faulted(exc: JournalBoundaryError) -> CampaignResult:
        fault = _classify(exc, "campaign_journal") or _Fault(
            "persistence_failed", "campaign_journal", "journal_unavailable"
        )
        return CampaignResult(
            terminal=fault.terminal,
            quiescent=True,
            finished=None,
            owner=fault.owner,
            code=fault.code,
            result=None,
            status=_inspected_status(root, run_id, ()),
        )

    try:
        log = AppendLog.open(
            root,
            file_name=CAMPAIGN_FILE,
            lock_name=CAMPAIGN_LOCK,
            create=False,
            state_factory=lambda: CampaignLifecycle(selector=None, family_version=family),
            max_record_bytes=MAX_RECORD_BYTES,
            max_log_bytes=MAX_JOURNAL_BYTES,
            secrets=(),
            envelope_family=campaign_family_for(family),
            require_empty=False,
        )
    except JournalFault as exc:
        if exc.code == "journal_busy":
            raise
        return faulted(exc)
    except JournalBoundaryError as exc:
        return faulted(exc)
    try:
        # The integrity check is the scan's: a corrupt suffix behind the
        # applied decision is `JournalFault()` here, reported below as the
        # ordinary resume reports it, never handed on to a path that would
        # first build bindings and contact the target.
        try:
            log.scan()
        except JournalBoundaryError as exc:
            return faulted(exc)
        except SchemaError:
            return None
        state = log.state
        assert isinstance(state, CampaignLifecycle)
        snapshot = state.snapshot
        if snapshot.owed_terminal is None:
            # The prefix moved on between the read-only look and the lock.
            return None
        _fire(process_log, root)
        outcome, reason = snapshot.owed_terminal
        # `probe_unavailable` carries its check as evidence, rebuilt from the
        # prefix as the live path and `_write_close` build it.
        evidence = (
            probe_unavailable_evidence(log.snapshot().records, snapshot)
            if reason == "probe_unavailable"
            else None
        )
        # `campaign_finished` is not in `NO_OPERATION_EVENTS`: one fresh
        # operation id, exactly as `_Campaign.append` assigns it.
        try:
            log.append(
                snapshot.run_id,
                "campaign_finished",
                dict(
                    outcome=outcome,
                    reason=reason,
                    continuation=dict(kind="linked_run", reason=reason),
                    evidence=None if evidence is None else [dict(item) for item in evidence],
                ),
                uuid4().hex,
            )
        except JournalBoundaryError as exc:
            return faulted(exc)
        state = log.state
        assert isinstance(state, CampaignLifecycle)
        snapshot = state.snapshot
    finally:
        log.close()

    assert snapshot.finished is not None
    try:
        # As `_Campaign.result` reports a closed campaign: the siblings read
        # read-only, over the snapshot the writer just reduced.
        view = replace(inspect_view(root), snapshot=snapshot)
    except (JournalBoundaryError, OSError) as exc:
        # `_Campaign.result`'s own fallback, and its status shape.
        fault = (
            _classify(exc, "measurement_journal") if isinstance(exc, JournalBoundaryError) else None
        ) or _Fault("persistence_failed", "measurement_journal", "journal_unavailable")
        return CampaignResult(
            terminal=fault.terminal,
            quiescent=True,
            finished=None,
            owner=fault.owner,
            code=fault.code,
            result=None,
            status=_inspected_status(root, snapshot.run_id, ()),
        )
    try:
        status = project_status(view, run_root=root)
        result = project_result(
            snapshot,
            tries=view.tries,
            affected_items=view.affected_items,
            independent_report=status.to_payload().get("independent"),
        )
    except ProjectionInvalid:
        return CampaignResult(
            terminal="finished",
            quiescent=True,
            finished=dict(snapshot.finished),
            owner="campaign_projection",
            code="projection_invalid",
            result=None,
            status=_inspected_status(root, snapshot.run_id, (), minimal=True),
        )
    return CampaignResult(
        terminal="finished",
        quiescent=True,
        finished=dict(snapshot.finished),
        owner=None,
        code=None,
        result=result,
        status=status,
    )


def _unopened(campaign: _Campaign, fault: _Fault) -> CampaignResult:
    """A terminal decided before the campaign's own state was adopted (§11 step 0).

    Two paths reach it: a fault before the log is even opened, and one raised
    inside `adopt`, which leaves the writer's lifecycle state empty. Neither has
    a state worth projecting, so the status comes from a read-only inspection of
    whatever prefix is readable — `inspect_campaign` scans the log itself and
    `assemble` tolerates an unusable sibling — and the terminal is the fault's.
    `quiescent` reflects the barrier the caller has already run: nothing of this
    campaign is left running, because nothing of it started.
    """

    return CampaignResult(
        terminal=fault.terminal,
        quiescent=not campaign.editor_alive,
        finished=None,
        owner=fault.owner,
        code=fault.code,
        result=None,
        status=_unopened_status(campaign),
        fault_detail=fault.detail,
    )


def _unopened_status(campaign: _Campaign) -> CampaignProjection:
    """The §12 projection of a run root whose campaign prefix may name no id.

    A process that died between the journal's `open` and its first completed
    append leaves a file with no readable `campaign_created` in it, so the
    reduction of that prefix carries an empty `run_id` and the projection's own
    validator refuses the payload. The campaign is not absent, though -- the run
    root holds one and its persistence fault is exactly what this result reports
    -- so the id the session was opened under stands in for the one the prefix
    does not name, and the fault is reported instead of a `SchemaError` from
    inside the inspection.

    A journal this process cannot read at all -- permissions removed, a file past
    `MAX_JOURNAL_BYTES`, a read the reader refuses -- is the same class of fault
    one step further out: the inspection that would describe the prefix is
    reading the very file whose unreadability is the terminal being reported, and
    it raises again. Re-raising there would end an `evo tree evolve` in a
    traceback instead of one `persistence_failed` document, so the projection
    falls back to the minimal one the caller's own ids can support: no records,
    no measurements, no lineage, the run id this session was opened under. It
    stays projection code -- no new record family, no sidecar (ADR-0008) -- and
    the terminal, owner and code the result carries are the fault's, unchanged.
    """

    return _inspected_status(campaign.run_root, campaign.run_id, campaign.secrets)


def _inspected_status(
    run_root: Path, run_id: str, secrets: Sequence[str], *, minimal: bool = False
) -> CampaignProjection:
    """`_unopened_status` over its three inputs, for a caller that has no `_Campaign`.

    `finish_owed_terminal` reports its faults through this same fallback, so a
    fault met while recovering an owed terminal and one met by the ordinary
    resume path report one status shape.
    """

    from evolution.tree.campaign_projections import inspect_view

    try:
        view = _unreadable_view(run_root) if minimal else inspect_view(run_root)
    except (JournalFault, SchemaError, OSError):
        view = _unreadable_view(run_root)
    if not view.snapshot.run_id:
        # A library caller that resumed without an id of its own, over a prefix
        # that names none, has nothing to name the campaign by; the projection's
        # validator would refuse an empty id with a `SchemaError` out of the very
        # report of the fault. `unknown` is the honest identifier: the run root
        # holds a campaign whose id this process could not read (W2 fix pack).
        view = replace(view, snapshot=replace(view.snapshot, run_id=run_id or "unknown"))
    try:
        return project_status(view, secrets=secrets)
    except ProjectionInvalid:
        if minimal:
            raise
        return _inspected_status(run_root, run_id, secrets, minimal=True)


def _unreadable_view(run_root: Path) -> CampaignView:
    """The empty view of a run root whose campaign journal cannot be read at all.

    Every fact this view could carry lives in the file that cannot be read, so it
    carries none: the reduction of an empty prefix, no measurement status, no
    lineage. The siblings are classified when the file system still answers --
    the S2 ledger is a different file and is usually readable -- and reported as
    `absent` when it does not, rather than turning this recovery into a second
    fault.
    """

    snapshot = reduce((), selector=None, family_version=FINAL_CAMPAIGN_FAMILY_VERSION)
    try:
        siblings: Mapping[str, Birth] = classify_siblings(run_root, snapshot)
    except (JournalFault, SchemaError, OSError):
        siblings = {STORE_SIBLING: "absent"}
    return CampaignView(snapshot=snapshot, measurements={}, lineage=None, siblings=siblings)


async def _finish(campaign: _Campaign) -> CampaignResult:
    try:
        await campaign.drive()
    except _Fault as exc:
        campaign.fault = exc
    await campaign.barrier()
    if campaign.error is not None:
        raise campaign.error
    if not campaign.closed:
        # #263: a held family-15 close wrote its terminal inside `drive`
        # (`_write_close`). That is the only way `closed` is set before here:
        # `resume_campaign` returns early on an immediate close. A fault that
        # left the closure owed is settled by `write_terminal` itself; any
        # other unsettled exit reaches its owed-closure invariant.
        campaign.write_terminal()
    return campaign.result()


# -- read-only inspection -------------------------------------------------


def inspect_campaign(
    run_root: Path,
    *,
    verify: bool = False,
    secrets: Sequence[str] = (),
    corpus: Sequence[EvalCase] | None = None,
) -> CampaignProjection:
    """The §12 status projection of any run root: no lease, no write, no repair."""

    from evolution.tree.campaign_projections import inspect_view, verify_evidence

    root = Path(run_root)
    return project_status(
        inspect_view(root),
        secrets=secrets,
        run_root=root,
        verification=verify_evidence(root, corpus=corpus) if verify else None,
    )
