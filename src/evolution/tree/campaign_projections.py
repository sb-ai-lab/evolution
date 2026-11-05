"""The two campaign projections and the read-only inspection view (spec §12).

`project_result(snapshot, tries=...)` needs only campaign facts, measurement ids and (family 6)
each measurement's try facts; `project_status(view)` reports everything the
`CampaignView` adds — each sibling's absent/unborn/born classification, the S1
status of a born measurement, the S2 workspace and promotion states, and the §11
actions a resume would take. Both follow the S1 projection discipline: pure,
canonical bytes, a closed key set, `schema_version 1`, a `kind`, a `run_id`, a
`consistency` and a `complete`, with every secret refused at construction.

The projection invariants in this module amend the
task brief:

1. `CampaignProjection` is a **sibling** of `journal_projections.Projection`, not
   an extension of it. S1's validator is closed over the kinds `status | events |
   result` and hard-codes `claim_scope == "rollout_measurement"` with the four
   reserved campaign fields all `None`; widening it would weaken the very
   assertion S1 §7 relies on ("a reader combining both never counts a sample
   twice"). This class keeps S1's shape rules and adds the campaign kinds.
2. `project_status` takes a keyword-only `verification` payload. Re-verifying an
   E record's `evidence` needs the sibling *files*, which the view deliberately
   does not carry; `inspect_campaign(verify=True)` computes it and hands it in,
   and it is `null` everywhere else.
3. A born measurement's S1 status payload is nested verbatim under
   `measurements[].status`. Spec §12 asks for "the S1 status of a born one", and
   an S1 projection is already redacted and secret-checked.
4. `verify_evidence` re-verifies both halves of an E record (ADR-0008, spec §5):
   the cited bytes are the ones the record hashed, *and* they satisfy the
   precondition the record claims. The second half re-evaluates each record kind
   with the executor's own pure functions over the referenced bytes -- S1's
   `recovery_plan` for a measurement prefix, S2's ledger records for a
   publication or a promotion commit, `select` over the campaign prefix for a
   selection, and -- since spec §3 put the corpus's `group_id`/`stratum` on the
   campaign identity's durable `corpus_cases` -- `derived_identity` and the
   shared `sample_row`/`score_samples_of` reduction for a `look_evaluated`'s
   whole verdict, bound three ways (the durable binding record, the identity
   `derived_identity` recomputes, and the cited prefix's own identity) before
   any of it is trusted -- so a record another writer produced whose evidence
   hashes perfectly but does not say what it claims is reported
   `precondition_failed`. Nothing is guessed: a fact the durable bytes still
   cannot express (a target's lie at materialization) is not a mismatch, and
   an explicit `corpus` argument that disagrees with the journal's own
   `corpus_cases` is reported once, as `corpus_mismatch`, rather than
   recomputed against. The one E-fact with no sibling to check against is
   `instance_attested.identity`, which R1 does not persist: it is first-hand
   C1 evidence, re-checked against its own row and run through R3's
   `verify_attestation` against the sealed Candidate's durable digests, so the
   *verdict* recorded over it -- a selection, an admission refusal -- is what
   the audit reports when it no longer follows (amendment 17).
5. A sample's *final* disposition is a fact about its whole measurement journal,
   not about one record in it, so the checks that need one read that journal --
   once, cached -- rather than the cited range alone. Reference integrity is
   still checked over the cited range, and the cited record must still be a
   terminal in that measurement's own journal and the sample's last; what the
   whole file adds is the ability to say a cited `rollout_finished` was later
   scored. The cost of a pass therefore scales with journal size rather than
   with the size of the ranges cited. The payload counts the two passes
   separately -- `checked` is references, `preconditions_checked` is E records --
   so neither number changes meaning as the other grows. See spec amendment 16.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from bisect import bisect_left
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from evolution.tree.affected_cases import (
    BaselineUnavailable,
    baseline_inputs,
    cited_baseline_prefix,
    parent_mode,
    select_affected,
    tree_changes,
)
from evolution.tree.affected_gate import (
    LookSides,
    gate_input_rows,
)
from evolution.tree.affected_report import (
    AFFECTED_REPORT_KEYS,
    affected_caveats,
    attempt_affected_entry,
    read_affected_items,
    round_policies,
)
from evolution.tree.artifacts.artifact_ledger import (
    ARTIFACTS_DIR,
    LEDGER_FILE,
    MAX_LEDGER_BYTES,
    MAX_LEDGER_RECORD_BYTES,
)
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_projections import (
    CANDIDATE_LOCATOR,
    CHAMPION_LOCATOR,
    project_lineage,
)
from evolution.tree.artifacts.artifact_schema import (
    ARTIFACT_FAMILY,
    ArtifactLifecycle,
    ArtifactStoreIdentityV1,
)
from evolution.tree.artifacts.artifacts import CANDIDATES, CHAMPIONS, ExpectedAttestation
from evolution.tree.artifacts.attestation import verify_attestation
from evolution.tree.campaign_caveats import (
    AFFECTED_CAVEAT_KINDS,
    AFFECTED_CAVEAT_KINDS14,
    CAVEAT_KINDS,
    CAVEAT_KINDS14,
    CHAMPION_NOT_CONTEMPORANEOUS_KIND,
    COMPLETION_REGRESSION_KIND,
    COMPONENTS_CHANGED_KIND,
    FINAL_CAMPAIGN_KINDS,
    FOCUS_BIAS_KIND,
    GROUPING_FALLBACK_KIND,
    IN_PLACE_KIND,
    LINEAGE_CAVEAT_KINDS,
    LOOK_CAVEAT_KINDS,
    LOOK_CAVEAT_KINDS14,
    OBSERVE_CAMPAIGN_KINDS,
    PROVENANCE_DRIFT_KIND,
    STORED_PAIRS_EXCLUDED_KIND,
    grouping_caveat_entries,
    in_place_caveats,
    own_caveats,
)
from evolution.tree.campaign_epochs import (
    base_measurement_id,
    epoch_of,
    fresh_baseline_id,
    restarted_owners,
)
from evolution.tree.campaign_identity import (
    AdaptationIdentityV1,
    CampaignIdentityV1,
    derived_identity,
    measurement_id_for,
)
from evolution.tree.campaign_inspection import inspection_path
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    Owner,
    OwnerWindow,
    PolicyError,
    policy_class_for_payload,
)
from evolution.tree.campaign_schema import (
    AFFECTED_SUSPENSIONS,
    ALTERNATE_SUSPENSION_KINDS,
    CHAMPION_SIDE,
    CHAMPION_SUSPENSIONS,
    CONDITIONAL_E_RECORDS,
    CONFIDENCE_SUSPENSIONS,
    CONNECTION_FAILED_EVENT,
    CONNECTION_FINISHED_EVENT,
    CONNECTION_INTERRUPTED_EVENT,
    CONNECTION_START_EVENT,
    CONNECTION_SUSPENSIONS,
    CONNECTION_TERMINALS,
    CONTINUABLE_E_RECORDS,
    FINAL_CAMPAIGN_FAMILY_VERSION,
    GROUPER_SUSPENSIONS,
    GROUPING_FALLBACK_KINDS,
    GROUPING_FALLBACK_REASONS,
    GROUPING_FALLBACK_REASONS14,
    GROUPING_FALLBACK_SCOPES,
    MAX_EXCLUDED_PAIRS,
    OWNERS,
    SUSPENSION_REASONS,
    UNPLANNED_REASONS,
    AffectedLayers,
    AttemptState,
    CampaignLifecycle,
    CampaignSnapshot,
    RoundState,
    adaptation_position,
    affected_layers,
    attempt_suspension_required,
    campaign_family_for,
    champion_member,
    current_members,
    editor_input_reason,
    effective_attempt_feedback,
    finalist_member,
    grouper_chain_limit,
    held_repair_required,
    journal_citations,
    latest_look_barred,
    member_diagnostic,
    pairs_digest,
    primary_range_required,
    recorded_complete,
    reduce,
    retained_cases,
    running_members,
    selection_finalists,
    selection_records,
    side_of_entry,
    skip_chain_length,
    skip_chain_limit,
    skip_due,
    stage_redispatch_required,
    threshold_repair_required,
    written_before_fix,
)
from evolution.tree.campaign_statistics import (
    PRIMARY_OUT_OF_RANGE,
    CoverageMissing,
    MeasurementTries,
    MissingMetric,
    PrimaryRange,
    SampleRow,
    case_groups,
    critical_vetoed,
    declined_tries,
    latest_rows,
    measurement_tries,
    owed_threshold_rows,
    pre_fix_out_of_range,
    primary_range_for,
    recompute_fault_rows,
    restored_tries,
    row_fault,
    rows_held,
    sample_row,
    stage_floor,
    stage_horizon,
)
from evolution.tree.campaign_view import (
    STORE_SIBLING,
    Action,
    CampaignView,
    InspectionBirth,
    classify_siblings,
    held_attempts,
    next_actions,
    owed_repairs_in,
    rebound_held_attempts,
    refresh_due_for,
    refresh_for,
)
from evolution.tree.campaign_vocabulary import CampaignVocabulary, campaign_vocabulary_for
from evolution.tree.champion_gaps import (
    CHAMPION_ROWS,
    champion_gap_rows,
    champion_owner,
    champion_rows_evidence,
)
from evolution.tree.component_drift import (
    DriftReference,
    baseline_parent_reference,
    candidate_reference,
    champion_reference,
    checked_selection,
    drift_suspends,
)
from evolution.tree.confidence_cohort import (
    Member,
    cohort_owner,
    cohort_rows_evidence,
    global_comparison_record,
    incomplete_rows,
    member_completion,
    rollout_owned_gap,
)
from evolution.tree.confidence_gate import (
    GateInputs,
    GateInvariant,
    calibration_record,
    look_record,
    stage_admission,
)
from evolution.tree.confidence_gate import gate_inputs as _gate_inputs
from evolution.tree.confidence_gate import look_missing as confidence_look_missing
from evolution.tree.confidence_report import (
    validate_cohort_block,
    validate_confidence_block,
)
from evolution.tree.confidence_statistics import DRAWS, derive_seed
from evolution.tree.execution.authorizations import comparable_field
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.execution.interaction import (
    ATTESTATION_CODES,
    AttestationError,
    EvalCase,
    TargetIdentity,
)
from evolution.tree.execution.journal import (
    EVENTS_FILE,
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    inspect_journal,
)
from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json, corpus_digest
from evolution.tree.execution.journal_projections import (
    project_status as project_measurement_status,
)
from evolution.tree.execution.journal_recovery import recovery_plan
from evolution.tree.execution.journal_schema import (
    S1_FAMILY,
    Lifecycle,
    components_digest,
    ordinal_limit,
)
from evolution.tree.execution.sample_outcomes import SampleOutcome, disposition_for
from evolution.tree.final_report import (
    attempt_confidence_entry14,
    connection_block,
    roles_block,
    round_final_entries,
    settings_block,
    validate_confidence_block14,
    validate_connection_block,
    validate_final_entries,
    validate_roles_block,
    validate_settings_block,
)
from evolution.tree.gate_scope import derive_gate_scope
from evolution.tree.roles.role_failures import editor_class, grouper_class, reflection_class
from evolution.tree.storage.append_log import _scan, inspect_log
from evolution.tree.storage.envelope import Envelope, EnvelopeFamily, SchemaError, is_identifier
from evolution.tree.storage.journal_errors import JournalFault
from evolution.tree.target.connection_probe import (
    _INCONCLUSIVE_TERMINALS,
    _SCORER_OPERATIONAL,
    _UNAVAILABLE_TERMINALS,
    CAUSE_CODES,
    SCORER_FAILED_CAUSE,
    _cleanup_of,
    binding_digest,
    epoch_binding,
)

#: Family 14's names for what family 13 calls the cohort (F0 M4).
FINAL_VOCABULARY = campaign_vocabulary_for(FINAL_CAMPAIGN_FAMILY_VERSION)

CAMPAIGN_FILE = "campaign.jsonl"
MEASUREMENTS_DIR = "measurements"

_COMMON = {
    "schema_version",
    "kind",
    "run_id",
    "consistency",
    "complete",
    # PDR-0010: an accepted identity change is visible in both public
    # projections. `identity_digest` is the identity the prefix ends on --
    # `null` before `campaign_created` -- and `rebases` the moves that got it
    # there, oldest first.
    "identity_digest",
    "rebases",
}
#: What a `rebases` entry publishes, in this order. The snapshot's own entry
#: also carries `authorization_id`, which is executor state, not a projection.
_REBASE_KEYS = (
    "previous_digest",
    "identity_digest",
    "producing_version",
    "changed_fields",
    "reason",
)
_RESULT = {
    "claim_scope",
    "campaign_outcome",
    "selection",
    "initial_champion",
    "final_champion",
    "rounds",
    "alpha",
    "indeterminate",
    "measurements",
}
_STATUS = {
    "claim_scope",
    "campaign_outcome",
    "final_champion",
    "rounds",
    "alpha",
    "indeterminate",
    "measurements",
    "siblings",
    "lineage",
    "actions",
    "verification",
}
#: Continuable-terminals spec §8: what family 6 adds to both projections. The
#: keys exist only in a family-6 projection, so families 3-5 keep their exact
#: key sets; the top-level `linked_from` key marks the family-6 shape.
_CONTINUABLE_OUTCOME = {"outcome", "reason", "continuation", "champion_path"}
_CONTINUABLE_INDETERMINATE = {"counts", "denominator", "threshold_reached", "lineage"}
_CONTINUABLE_MEASUREMENT = {"tries", "excluded_pairs", "imported"}
#: D2a spec §4: the top-level keys a family-6 projection adds beside `linked_from`.
_CONTINUABLE_TOP = {"linked_from", "caveats"}
#: I1 spec §5.2-§5.3: what family 7 adds to both projections, on top of family
#: 6's. The `suspensions` key marks the family-7 shape, exactly as `linked_from`
#: marks family 6's, so families 3-6 keep their exact key sets.
_SUSPENDABLE_TOP = {"suspensions", "suspended", "condition_changes"}
#: H12b plan choice 2 (F0 §13, C§6.3, M7): what family 14 adds at top level.
#: The `connection` object marks the family-14 shape; `roles` and `settings`
#: come with it.
_FINAL_TOP = {"connection", "roles", "settings"}
#: Role resilience spec §6.3: the family-10 top-level counts, present whenever
#: the round shape carries `visitation` (the family-10 mark), and on a
#: family-10 document that has no rounds yet.
_RESILIENCE_TOP = {"resilience"}
#: Spec §6.3: skipped rounds, Attempts with a continuation turn, and those of
#: them whose Editor finished `edited` (`attempts_saved <= continuation_turns`).
_RESILIENCE_COUNTS = {"rounds_skipped", "continuation_turns", "attempts_saved"}
_SUSPENDABLE_INDETERMINATE = _CONTINUABLE_INDETERMINATE | {"window"}
#: I1 spec §3.4: what the call retries cost one measurement.
_SUSPENDABLE_MEASUREMENT = {
    "call_retries",
    "call_failures_by_code",
    "repair_tries",
    "repairs_scored",
    "condition_epoch",
}
_SUSPENSION = {"seq", "reason", "owner", "evidence_kind"}
_WINDOW = {"counts", "denominator", "start"}
#: I2 spec §6.5: one owner's window an in-place change of its own field
#: restarted after the latest suspension (`window.restarted[owner]`).
_RESTARTED_WINDOW = {"start", "count", "denominator"}
#: I2 spec §4.4: one applied in-place condition change, in journal order.
_CONDITION_CHANGE = ("seq", "effect", "fields", "round", "authorization_id", "identity_digest")
_IN_PLACE_EFFECTS = ("continue", "rebaseline")
_ATTEMPT = {
    "attempt_id",
    "index",
    "outcome",
    "reason",
    "look",
    "alpha_total",
    "measurement_id",
    "artifact_locator",
}
_LINKED = {
    "run_id",
    "family",
    "first_round",
    "rounds_closed",
    "alpha_rounds",
    "lineage_caveats",
    "baseline",
}
_BASELINE_REPORT = {"mode", "imported", "scheduled", "abandoned"}
#: Scorer-diagnostics spec §5: a family-8 round's `gate_scope`. The key marks
#: the family-8 shape on every round entry, so families 3-7 keep theirs.
_GATE_SCOPE_REPORT = {
    "source",
    "cases",
    "claimed_cases",
    "selected_cases",
    "unevaluable_cases",
}
_BIRTHS = ("absent", "unborn", "born")
_UNREADABLE_COUNTS = {
    "tries",
    "imported",
    "call_retries",
    "repair_tries",
    "repairs_scored",
    "call_failures_by_code",
}
_SHA256 = re.compile("[a-f0-9]{64}")


def adaptive_state_payload(snapshot: CampaignSnapshot) -> dict[str, Any]:
    """Compact adaptive-family grouping, focus, coverage and frozen-input replay facts."""

    rounds: list[dict[str, Any]] = []
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        rounds.append(
            {
                "round": number,
                "grouping": state.grouping,
                "focus_portion": state.focus_portion,
                "reflections": list(state.reflections),
                "coverage": None if state.closed is None else state.closed.get("coverage"),
                "attempt_inputs": {
                    attempt.attempt_id: attempt.frozen_input
                    for attempt in sorted(state.attempts.values(), key=lambda row: row.index)
                    if attempt.frozen_input is not None
                },
            }
        )
    return {"schema": "campaign_adaptation_state/1", "rounds": rounds}


def _require(condition: bool) -> None:
    if not condition:
        raise SchemaError("invalid public projection")


def _keys(value: Any, names: set[str]) -> None:
    _require(type(value) is dict and set(value) == names)


def _affected_block(block: Any) -> None:
    _keys(block, set(AFFECTED_REPORT_KEYS))
    _require(block["source"] in ("reported", "metadata", "none"))
    _require(block["mode"] in ("component", "path", None))
    _require(block["fallback_reason"] is None or type(block["fallback_reason"]) is str)
    for name in ("layer_one", "layer_two"):
        _require(type(block[name]) is int and block[name] >= 0)
    _require(type(block["fresh_parent"]) is bool)
    _require(
        block["alpha_statement"] in ("layer_one", "layer_one_with_global_filter", "global", None)
    )


def _identifier(value: Any) -> None:
    _require(type(value) is str and is_identifier(value))


def _sha(value: Any) -> None:
    _require(type(value) is str and _SHA256.fullmatch(value) is not None)


def _validate(data: Any) -> None:
    _require(type(data) is dict)
    kind = data.get("kind")
    _require(kind in ("campaign_result", "campaign_status"))
    independent = "family_version" in data
    unreadable = independent and kind == "campaign_status"
    if independent:
        _require(type(data["family_version"]) is int and data["family_version"] == 15)
    continuable = "linked_from" in data
    suspendable = "suspensions" in data
    # Spec §6.3: the family-10 counts. Its top-level presence must match the
    # round-shape `resilience` mark below, checked once that is known.
    resilience_top = "resilience" in data
    # Family 14 marks its shape with the top-level `connection` object (H12b
    # plan choice 2); `roles` and `settings` come with it, and it implies every
    # family-13 mark, checked below once `alpha` is read.
    final = "connection" in data
    _require(not independent or final)
    _require(not suspendable or continuable)
    _keys(
        data,
        _COMMON
        | ({"family_version", "independent"} if independent else set())
        | (_RESULT if kind == "campaign_result" else _STATUS)
        | (_CONTINUABLE_TOP if continuable else set())
        | (_SUSPENDABLE_TOP if suspendable else set())
        | (_RESILIENCE_TOP if resilience_top else set())
        | (_FINAL_TOP if final else set())
        | ({"corpus_split"} if "corpus_split" in data else set())
        | (
            {name for name in ("check_modes", "shadow_hits") if name in data}
            if independent
            else set()
        ),
    )
    if independent:
        from evolution.tree.family15_report import validate_campaign15_projection

        validate_campaign15_projection(data["independent"])
        for name in ("check_modes", "shadow_hits"):
            _require(data.get(name) == data["independent"].get(name))
    if "corpus_split" in data:
        split = data["corpus_split"]
        _keys(split, {"scope", "mode", "train_cases", "test_cases", "partition_digest"})
        _require(split["scope"] == "train" and split["mode"] in ("explicit", "implicit_train"))
        _require(type(split["train_cases"]) is int and split["train_cases"] > 0)
        _require(type(split["test_cases"]) is int and split["test_cases"] >= 0)
        _sha(split["partition_digest"])
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    _identifier(data["run_id"])
    _require(data["consistency"] in ("observed", "durable"))
    _require(type(data["complete"]) is bool)
    _require(data["claim_scope"] == "campaign")
    if data["identity_digest"] is not None:
        _sha(data["identity_digest"])
    _require(type(data["rebases"]) is list)
    for rebase in data["rebases"]:
        _keys(rebase, set(_REBASE_KEYS))
        _sha(rebase["previous_digest"])
        _sha(rebase["identity_digest"])
        _identifier(rebase["producing_version"])
        _require(
            type(rebase["changed_fields"]) is list
            and all(type(field) is str for field in rebase["changed_fields"])
        )
        _require(type(rebase["reason"]) is str)
    outcome = data["campaign_outcome"]
    if outcome is not None:
        _keys(
            outcome, _CONTINUABLE_OUTCOME if continuable else {"outcome", "reason", "continuation"}
        )
        if continuable and outcome["champion_path"] is not None:
            # Exactly `artifacts/champions/<version>`: one identifier component,
            # so no `..`, no absolute path and nothing past the version escapes.
            path = outcome["champion_path"]
            _require(type(path) is str and path.startswith(CHAMPION_LOCATOR))
            _identifier(path[len(CHAMPION_LOCATOR) :])
    _require(type(data["rounds"]) is list)
    # Family 8 marks its shape on the round entries: all of them carry
    # `gate_scope` or none does, and only on a family-7 (or later) shape.
    diagnostic = any(type(entry) is dict and "gate_scope" in entry for entry in data["rounds"])
    _require(not diagnostic or suspendable)
    # Family 9 marks its shape the same way, with `reason`, on a family-8 one.
    # A `grouping_fallback` caveat names a round, so its document has the mark.
    model_grouping = any(type(entry) is dict and "reason" in entry for entry in data["rounds"])
    _require(not model_grouping or diagnostic)
    # Family 10 marks its shape the same way, with `visitation`, on a
    # family-9 (`model_grouping`) shape (spec §7).
    resilience = any(type(entry) is dict and "visitation" in entry for entry in data["rounds"])
    _require(not resilience or model_grouping)
    # Family 12 marks its shape with a per-Attempt `affected` key (spec §4.9).
    affected = any(
        type(a) is dict and "affected" in a
        for e in data["rounds"]
        if type(e) is dict
        for a in e.get("attempts", [])
    )
    _require(not affected or resilience)
    # Family 13 marks its shape with `alpha: null` (F8 plan choice 3): it
    # spends no alpha, and a document may have no Attempt to carry any other
    # mark (a crash after `campaign_created`, or rounds closed `unplanned`),
    # as a family-10 document may have no round. It sits on the family-10 top
    # shape; every Attempt then carries `confidence` and `affected`, with a
    # null `alpha_total`, every round carries `cohort`, and neither a
    # `confidence` nor a `cohort` key appears anywhere else.
    confidence = data["alpha"] is None
    _require(not confidence or resilience_top)
    # A family-14 document is never read as family 13 and vice versa: its
    # rounds carry `final` (never `cohort`) and its Attempts the family-14
    # `confidence` block (plan choice 2).
    _require(not final or (confidence and suspendable))
    if final:
        validate_connection_block(data["connection"])
        validate_roles_block(data["roles"])
        validate_settings_block(data["settings"])
    # The top-level `resilience` counts are required whenever the round shape
    # carries the mark. A family-10 document with no rounds yet (cancelled or
    # crashed before the first round) has no round to carry it, so there the
    # counts stand alone -- on the suspendable (family-7 or later) shape only.
    _require(not resilience or resilience_top)
    _require(not resilience_top or resilience or (data["rounds"] == [] and suspendable))
    if resilience_top:
        counts = data["resilience"]
        _keys(counts, _RESILIENCE_COUNTS)
        for name in _RESILIENCE_COUNTS:
            _require(type(counts[name]) is int and counts[name] >= 0)
        _require(counts["attempts_saved"] <= counts["continuation_turns"])
    if continuable:
        _validate_continuable(
            data,
            model_grouping=model_grouping,
            # Family 12: where each Attempt sits in the document, for the caveat
            # order. A family-13 document may have no Attempt to carry the
            # `affected` mark yet still carry the campaign-level observe items.
            attempt_order=_attempt_order(data["rounds"]) if affected or confidence else None,
            confidence=confidence,
            observe=confidence and _observe_shown(data["rounds"], final=final),
            final=final,
            stable=final and _stable_shown(data),
            unreadable=unreadable,
        )
    if suspendable:
        _validate_suspendable(data, final=final, confidence=confidence, unreadable=unreadable)
    champion = data["final_champion"]
    if champion is not None:
        _keys(
            champion,
            {"version", "digest", "measurement_id", "baseline_origin", "measurement_complete"},
        )
        _require(champion["baseline_origin"] in ("seed", "promoted_attempt"))
        _require(type(champion["measurement_complete"]) is bool)
    for entry in data["rounds"]:
        _keys(
            entry,
            {
                "round",
                "baseline_measurement_id",
                "baseline_complete",
                "outcome",
                "champion_complete",
                "attempts",
            }
            | ({"gate_scope"} if diagnostic else set())
            | ({"reason"} if model_grouping else set())
            | ({"visitation"} if resilience else set())
            | ({"final"} if final else {"cohort"} if confidence else set()),
        )
        _require(type(entry["round"]) is int and type(entry["attempts"]) is list)
        if final:
            validate_final_entries(entry["final"])
        elif confidence and entry["cohort"] is not None:
            validate_cohort_block(entry["cohort"])
        if model_grouping:
            if entry["outcome"] == "unplanned":
                _require(entry["reason"] in UNPLANNED_REASONS and entry["attempts"] == [])
            else:
                _require(entry["reason"] is None)
        if resilience:
            _require(entry["visitation"] in ("skipped", None))
        if diagnostic and entry["gate_scope"] is not None:
            scope = entry["gate_scope"]
            _keys(scope, _GATE_SCOPE_REPORT)
            _require(type(scope["source"]) is str)
            for name in ("cases", "claimed_cases", "selected_cases", "unevaluable_cases"):
                _require(type(scope[name]) is list)
                _require(all(type(case_id) is str for case_id in scope[name]))
            _require(set(scope["selected_cases"]) <= set(scope["cases"]))
            _require(set(scope["unevaluable_cases"]) <= set(scope["cases"]))
        for attempt in entry["attempts"]:
            _keys(
                attempt,
                _ATTEMPT
                | ({"looks"} if continuable else set())
                | ({"continued"} if resilience else set())
                | ({"affected"} if affected or confidence else set())
                | ({"confidence"} if confidence else set()),
            )
            _identifier(attempt["attempt_id"])
            if affected and attempt["affected"] is not None:
                _affected_block(attempt["affected"])
            if resilience:
                _require(type(attempt["continued"]) is bool)
            if confidence:
                _require(attempt["alpha_total"] is None)
                if attempt["confidence"] is not None:
                    if final:
                        validate_confidence_block14(attempt["confidence"])
                    else:
                        validate_confidence_block(attempt["confidence"])
    if not confidence:
        _keys(data["alpha"], {"per_round", "per_attempt"})
    _keys(
        data["indeterminate"],
        (_SUSPENDABLE_INDETERMINATE if suspendable else _CONTINUABLE_INDETERMINATE)
        if continuable
        else {"counts", "denominator", "threshold_reached"},
    )
    _require(set(data["indeterminate"]["counts"]) == set(OWNERS))
    _require(type(data["measurements"]) is list)
    if kind == "campaign_result":
        _require(type(data["selection"]) is list)
        for entry in data["selection"]:
            _keys(entry, {"round", "attempt_id", "data" if independent else "ranking", "outcome"})
            if independent:
                from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication

                try:
                    AuxiliaryPublication.from_payload(entry["data"])
                except (TypeError, ValueError):
                    raise SchemaError("invalid public projection") from None
        initial = data["initial_champion"]
        if initial is not None:
            _keys(initial, {"version", "digest", "measurement_id"})
        for entry in data["measurements"]:
            _keys(
                entry,
                {"measurement_id", "candidate_digest", "locator"}
                | (_CONTINUABLE_MEASUREMENT if continuable else set())
                | (_SUSPENDABLE_MEASUREMENT if suspendable else set()),
            )
        return
    for entry in data["measurements"]:
        _keys(
            entry,
            {"measurement_id", "candidate_digest", "locator", "classification", "status"}
            | (_CONTINUABLE_MEASUREMENT if continuable else set())
            | (_SUSPENDABLE_MEASUREMENT if suspendable else set()),
        )
        _require(
            entry["classification"] in (*_BIRTHS, "unreadable")
            if unreadable
            else entry["classification"] in _BIRTHS
        )
        if unreadable:
            _require(entry["classification"] == data["siblings"].get(entry["measurement_id"]))
        if entry["classification"] == "unreadable":
            _require(entry["status"] is None)
            _require(data["siblings"].get(entry["measurement_id"]) == "unreadable")
    _require(type(data["siblings"]) is dict)
    for value in data["siblings"].values():
        _require(value in (*_BIRTHS, "unreadable") if unreadable else value in _BIRTHS)
    _require(type(data["actions"]) is list)
    if "unreadable" in data["siblings"].values():
        _require(data["actions"] == [])
    if data["siblings"].get(STORE_SIBLING) == "unreadable":
        _require(data["lineage"] is None)
    for action in data["actions"]:
        _keys(action, {"kind", "fields"})
        _identifier(action["kind"])
        # A family-14 status names the planner's cohort steps "final" (F0 M4).
        _require(not final or action["kind"] not in _FINAL_ACTION_KINDS)
    verification = data["verification"]
    if verification is not None:
        _keys(verification, {"checked", "preconditions_checked", "mismatches", "corpus"})
        _require(type(verification["checked"]) is int)
        _require(type(verification["preconditions_checked"]) is int)
        _require(type(verification["mismatches"]) is list)
        _require(verification["corpus"] in ("proven", "journaled", "mismatch"))


def _count(value: Any, *, nullable: bool = False) -> None:
    _require((nullable and value is None) or (type(value) is int and value >= 0))


def _observe_shown(rounds: list[Any], *, final: bool = False) -> bool:
    """Whether a family-13 document's own rounds show `observe`: an Attempt's
    `confidence.mode` or a round's `cohort.mode` (codex F8 round 1). Such a
    document must carry the campaign-level observe items. A family-14
    document (`final`) shows it in any entry of a round's `final` list."""

    for entry in rounds:
        if final:
            if any(binding["mode"] == "observe" for binding in entry["final"]):
                return True
        else:
            cohort = entry["cohort"]
            if cohort is not None and cohort["mode"] == "observe":
                return True
        for attempt in entry["attempts"]:
            block = attempt["confidence"]
            if block is not None and block["mode"] == "observe":
                return True
    return False


def _stable_shown(data: Mapping[str, Any]) -> bool:
    """Whether a family-14 document shows `stable`: its `settings` or any
    round's final entry (H12b carry from U2). PDR-0028: every `stable` result
    carries `champion_not_contemporaneous`, as an observe one carries the
    observe pair."""

    return data["settings"]["stability"] == "stable" or any(
        binding["stability"] == "stable" for entry in data["rounds"] for binding in entry["final"]
    )


def _attempt_order(rounds: list[Any]) -> dict[str, tuple[int, int]]:
    """Each Attempt id of the document's rounds -> (its round, its place in that round)."""

    return {
        attempt["attempt_id"]: (entry["round"], place)
        for entry in rounds
        for place, attempt in enumerate(entry["attempts"])
    }


def _json_number(value: Any) -> bool:
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def _decimal_string(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _validate_affected_item(
    item: dict[str, Any],
    order: Mapping[str, tuple[int, int]],
    *,
    confidence: bool = False,
    final: bool = False,
) -> tuple:
    """One family-12 caveat's shape (G2 spec §4.7, §4.9); returns its order key.

    `confidence` marks a family-13 document: it admits the method caveats
    (design §6.5, §10; F8 plan choice 5) and never `completion_regression`
    (D8); any other document admits no method caveat. It also admits the two
    campaign-level observe items (`OBSERVE_CAMPAIGN_KINDS`), exactly
    `{kind, round: null, attempt_id: null}`, which sort before every round's
    items (codex F8 round 1, PDR-0022).

    `final` marks a family-14 document and implies `confidence` (H12b plan
    choices 12-13): its kinds and sort keys are `AFFECTED_CAVEAT_KINDS14`, its
    method kinds `LOOK_CAVEAT_KINDS14` and its campaign-level items
    `FINAL_CAMPAIGN_KINDS`; it adds `stored_pairs_excluded` and says
    `final_*` for the drift sides. It never admits `completion_regression`,
    `observe_calibration_degenerate` or a `cohort_*` side.
    """

    confidence = confidence or final
    kinds = AFFECTED_CAVEAT_KINDS14 if final else AFFECTED_CAVEAT_KINDS
    kind = item["kind"]
    _require(kind in kinds)
    if "round" in item and item["round"] is None:
        _require(confidence and kind in (FINAL_CAMPAIGN_KINDS if final else OBSERVE_CAMPAIGN_KINDS))
        _keys(item, {"kind", "round", "attempt_id"})
        _require(item["attempt_id"] is None)
        return (-math.inf, -math.inf, kinds.index(kind))
    if kind in (LOOK_CAVEAT_KINDS14 if final else LOOK_CAVEAT_KINDS):
        _require(confidence)
        _keys(item, {"kind", "round", "attempt_id"})
    elif kind == FOCUS_BIAS_KIND:
        _keys(item, {"kind", "round", "attempt_id"})
    elif kind == COMPLETION_REGRESSION_KIND:
        _require(not confidence)
        _keys(
            item,
            {
                "kind",
                "round",
                "attempt_id",
                "measurement_id",
                "cases",
                "lower",
                "upper",
                "alpha",
                "margin",
            },
        )
        _require(type(item["cases"]) is int and item["cases"] > 0)
        _require(_json_number(item["lower"]) and _json_number(item["upper"]))
        _require(item["lower"] <= item["upper"])
        _require(_decimal_string(item["alpha"]) and _decimal_string(item["margin"]))
    elif kind == PROVENANCE_DRIFT_KIND:
        _keys(item, {"kind", "round", "attempt_id", "measurement_id", "cases"})
        cases = item["cases"]
        _require(type(cases) is list and bool(cases))
        _require(all(type(case_id) is str for case_id in cases) and len(set(cases)) == len(cases))
    elif kind == STORED_PAIRS_EXCLUDED_KIND:
        # Family 14 (F0 R27, plan choice 13): the stored measurement and its
        # positive count of removed pairs, per finalist.
        _keys(item, {"kind", "round", "attempt_id", "measurement_id", "count"})
        _require(type(item["count"]) is int and item["count"] > 0)
    elif kind == COMPONENTS_CHANGED_KIND:
        _keys(item, {"kind", "round", "attempt_id", "sides"})
        sides = item["sides"]
        _require(type(sides) is list and bool(sides))
        if confidence:
            # Family 13 (F8 plan choice 8): an Attempt item's sides are a
            # non-empty ordered subsequence of the three; the Champion
            # member's item is a round item, with no Attempt.
            # Family 14 says "final" where family 13 says "cohort" (M4).
            member = "final" if final else "cohort"
            if item["attempt_id"] is None:
                _require(sides == [f"{member}_champion"])
            else:
                order_of = ("candidate", "parent", f"{member}_finalist")
                _require(
                    all(side in order_of for side in sides)
                    and sides == sorted(set(sides), key=order_of.index)
                )
        else:
            _require(sides in (["candidate"], ["parent"], ["candidate", "parent"]))
    else:
        _require(False)
    if kind in (COMPLETION_REGRESSION_KIND, PROVENANCE_DRIFT_KIND, STORED_PAIRS_EXCLUDED_KIND):
        _identifier(item["measurement_id"])
    _require(type(item["round"]) is int)
    if item["attempt_id"] is None:
        # Only the Champion member's drift names no Attempt: it follows every
        # Attempt item of its round (order key place `inf`).
        _require(confidence and kind == COMPONENTS_CHANGED_KIND)
        return (item["round"], math.inf, kinds.index(kind))
    # The item names an Attempt of the document's own rounds, in that round.
    placed = order.get(item["attempt_id"]) if type(item["attempt_id"]) is str else None
    _require(placed is not None and placed[0] == item["round"])
    assert placed is not None
    return (item["round"], placed[1], kinds.index(kind))


def _validate_caveats(
    caveats: Any,
    *,
    model_grouping: bool = False,
    attempt_order: Mapping[str, tuple[int, int]] | None = None,
    confidence: bool = False,
    observe: bool = False,
    final: bool = False,
    stable: bool = False,
) -> None:
    """D2a spec §4: the run's own caveats, in `CAVEAT_KINDS` order.

    One per lineage kind; `condition_changed_in_place` (I2 spec §4.4) once per
    applied change and, in a family-9 document only (`model_grouping`),
    `grouping_fallback` (model error grouping spec §10) once per round whose
    grouping fell back -- both after every lineage kind, in journal order.

    A family-12 document (`attempt_order` given: Attempt id -> (round, place in
    its round's `attempts`)) may end with the `AFFECTED_CAVEAT_KINDS` items,
    after every other item and strictly ordered by `(round, the Attempt's place
    in the document, kind position)`. The place comes from the document's own
    rounds, never from the id's spelling. A family-13 document (`confidence`)
    adds the method caveat kinds to them (`_validate_affected_item`).

    A family-13 document carries the two campaign-level observe items both or
    neither, first among the affected items. It must carry them when its own
    rounds show `observe` (`observe`, from `_observe_shown`) or when an
    Attempt carries an observe item. A document with no Attempt and no cohort
    cannot show its mode, so there they are admitted, not required.

    A family-14 document (`final`, H12b plan choices 12-13) is held to
    `CAVEAT_KINDS14`: its affected items follow `_validate_affected_item(final=
    True)`, `champion_not_contemporaneous` may follow the observe pair among
    the campaign-level items, and a `grouping_fallback` names a reason of
    `GROUPING_FALLBACK_REASONS14` (F0 R45). It must carry
    `champion_not_contemporaneous` when it shows `stable` (`stable`, from
    `_stable_shown`; PDR-0028), as an observe one carries the observe pair.
    """

    affected = AFFECTED_CAVEAT_KINDS14 if final else AFFECTED_CAVEAT_KINDS
    _require(type(caveats) is list)
    seen: list[str] = []
    in_place_seq = 0
    grouping_round = 0
    repeatable = False
    last_affected: tuple | None = None
    campaign_kinds: list[str] = []
    for item in caveats:
        _require(
            type(item) is dict and item.get("kind") in (CAVEAT_KINDS14 if final else CAVEAT_KINDS)
        )
        kind = item["kind"]
        if kind in affected:
            _require(attempt_order is not None)
            assert attempt_order is not None
            key = _validate_affected_item(item, attempt_order, confidence=confidence, final=final)
            _require(last_affected is None or key > last_affected)
            last_affected = key
            if item["round"] is None:
                campaign_kinds.append(kind)
            elif kind in OBSERVE_CAMPAIGN_KINDS:
                observe = True
            continue
        # Once a family-12 item is seen, only family-12 items may follow.
        _require(last_affected is None)
        if kind == IN_PLACE_KIND:
            _keys(item, {"kind", "seq", "effect", "fields", "round"})
            _require(type(item["seq"]) is int and item["seq"] > in_place_seq)
            in_place_seq = item["seq"]
            _validate_change(item)
            repeatable = True
            continue
        if kind == GROUPING_FALLBACK_KIND:
            _require(model_grouping)
            _keys(item, {"kind", "round", "reason", "fallback_kind", "scope"})
            _require(type(item["round"]) is int and item["round"] > grouping_round)
            grouping_round = item["round"]
            _require(
                item["reason"]
                in (GROUPING_FALLBACK_REASONS14 if final else GROUPING_FALLBACK_REASONS)
            )
            _require(item["fallback_kind"] in GROUPING_FALLBACK_KINDS)
            _require(item["scope"] in GROUPING_FALLBACK_SCOPES)
            repeatable = True
            continue
        _require(not repeatable and kind not in seen)
        seen.append(kind)
        if kind == "condition_changed":
            _keys(item, {"kind", "from_run", "fields"})
            fields = item["fields"]
            _require(
                type(fields) is list
                and fields == sorted(set(fields))
                and all(type(field) is str for field in fields)
            )
        elif kind == "baseline_inherited":
            _keys(
                item,
                {
                    "kind",
                    "from_run",
                    "source_measurement_id",
                    "imported",
                    "scheduled",
                    "fields",
                    "origin_runs",
                },
            )
            _identifier(item["source_measurement_id"])
            _count(item["imported"])
            _count(item["scheduled"])
            fields = item["fields"]
            _require(
                type(fields) is list
                and fields == sorted(set(fields))
                and all(type(field) is str for field in fields)
            )
            _require(type(item["origin_runs"]) is int and item["origin_runs"] >= 0)
        else:  # predecessor_unfinished
            _keys(item, {"kind", "from_run", "authorization_id"})
            _sha(item["authorization_id"])
        _identifier(item["from_run"])
    _require(seen == [kind for kind in LINEAGE_CAVEAT_KINDS if kind in seen])
    # The order keys above already make the campaign items unique, ordered and
    # leading; here the observe pair is both or neither, and present where
    # observe shows (family 14's stored-Champion item stands alone).
    observed = [kind for kind in campaign_kinds if kind in OBSERVE_CAMPAIGN_KINDS]
    _require(observed in ([], list(OBSERVE_CAMPAIGN_KINDS)))
    _require(not observe or bool(observed))
    _require(not stable or CHAMPION_NOT_CONTEMPORANEOUS_KIND in campaign_kinds)


def _validate_change(item: Mapping[str, Any]) -> None:
    """The fields a `condition_changes` entry and its caveat share (I2 spec §4.4)."""

    _require(item["effect"] in _IN_PLACE_EFFECTS)
    fields = item["fields"]
    _require(
        type(fields) is list
        and bool(fields)
        and fields == sorted(set(fields))
        and all(type(field) is str for field in fields)
    )
    _count(item["round"], nullable=True)


def _validate_lineage_caveats(caveats: Any) -> None:
    """D2a spec §4: the bounded per-kind lineage aggregate, one item per kind."""

    _require(type(caveats) is list)
    seen: list[str] = []
    for item in caveats:
        _require(type(item) is dict)
        _keys(item, {"kind", "runs", "fields", "nearest_run"})
        _require(item["kind"] in LINEAGE_CAVEAT_KINDS and item["kind"] not in seen)
        seen.append(item["kind"])
        _require(type(item["runs"]) is int and item["runs"] >= 1)
        fields = item["fields"]
        _require(
            type(fields) is list
            and fields == sorted(set(fields))
            and all(type(field) is str for field in fields)
        )
        _identifier(item["nearest_run"])
    _require(seen == [kind for kind in LINEAGE_CAVEAT_KINDS if kind in seen])


def _validate_continuable(
    data: dict[str, Any],
    *,
    model_grouping: bool = False,
    attempt_order: Mapping[str, tuple[int, int]] | None = None,
    confidence: bool = False,
    observe: bool = False,
    final: bool = False,
    stable: bool = False,
    unreadable: bool = False,
) -> None:
    """The family-6 additions (continuable-terminals spec §8), closed like the rest.

    `model_grouping` marks a family-9 document, whose caveats may also carry
    `grouping_fallback` items, `attempt_order` (family 12) the family-12 ones,
    and `confidence` (family 13) the method caveats; `observe` says its own
    rounds show `observe` (`_observe_shown`). `final` marks a family-14
    document, whose caveats follow the family-14 paths, and `stable` says it
    shows `stable` (`_stable_shown`).
    """

    linked = data["linked_from"]
    if linked is not None:
        _keys(linked, _LINKED)
        _identifier(linked["run_id"])
        for name in ("family", "first_round", "rounds_closed"):
            _count(linked[name])
        _require(type(linked["alpha_rounds"]) is dict)
        _validate_lineage_caveats(linked["lineage_caveats"])
        baseline = linked["baseline"]
        if baseline is not None:
            _require(type(baseline) is dict)
            abandoned = baseline.get("abandoned")
            _require(type(abandoned) is bool)
            _keys(baseline, _BASELINE_REPORT | ({"authorization_id"} if abandoned else set()))
            _require(baseline["mode"] == "inherit")
            _count(baseline["imported"])
            _count(baseline["scheduled"])
            if abandoned:
                _sha(baseline["authorization_id"])
    _validate_caveats(
        data["caveats"],
        model_grouping=model_grouping,
        attempt_order=attempt_order,
        confidence=confidence,
        observe=observe,
        final=final,
        stable=stable,
    )
    # Only family 7 changes a condition in place; its caveat needs the
    # `condition_changes` it restates (checked in `_validate_suspendable`).
    _require(
        "condition_changes" in data
        or all(item["kind"] != IN_PLACE_KIND for item in data["caveats"])
    )
    lineage = data["indeterminate"]["lineage"]
    if lineage is not None:
        _keys(lineage, {"counts", "denominator"})
        _require(set(lineage["counts"]) == set(OWNERS))
        for value in (*lineage["counts"].values(), lineage["denominator"]):
            _count(value)
    for entry in data["rounds"]:
        for attempt in entry["attempts"]:
            _require(type(attempt["looks"]) is list)
            for look in attempt["looks"]:
                _keys(look, {"look", "baseline_excluded"})
                _count(look["look"])
                _count(look["baseline_excluded"])
    for entry in data["measurements"]:
        if unreadable and entry.get("classification") == "unreadable":
            _require(entry["tries"] is None and entry["imported"] is None)
        else:
            _count(entry["tries"])
            _count(entry["imported"])
        excluded = entry["excluded_pairs"]
        if excluded is None:
            continue
        _keys(excluded, {"count", "by_owner", "pairs", "truncated"})
        _count(excluded["count"])
        _require(type(excluded["by_owner"]) is dict and set(excluded["by_owner"]) <= set(OWNERS))
        _require(type(excluded["truncated"]) is bool and type(excluded["pairs"]) is list)
        for pair in excluded["pairs"]:
            _keys(pair, {"case_id", "sample_id", "owner", "reason", "tries"})


def _validate_suspendable(
    data: dict[str, Any], *, final: bool = False, confidence: bool = False, unreadable: bool = False
) -> None:
    """The family-7 additions (I1 spec §5.2-§5.3), closed like the rest.

    A suspension entry is the record's four public fields -- the rest of its
    evidence stays in the journal -- and each is held to the registry: the
    reason names its owner, and its `evidence_kind` is the one that reason
    fixes (or the alternate the registry allows it). The window's `start` is
    the latest suspension's sequence, or `null` before the first, which is what
    makes the reported counts readable as "since the last suspension".

    The registry is read by family (H12b plan choice 15). A family-14 document
    (`final`) admits every `SUSPENSION_REASONS` reason, the Grouper's and the
    connection's included, its own incomplete-final reason with any threshold
    owner, and #194's `CHAMPION_SUSPENSIONS`; it refuses family 13's
    `cohort_incomplete`. A document without the mark refuses the Grouper and
    connection reasons, which no lifecycle below family 14 can write.
    `CHAMPION_SUSPENSIONS` needs the family-13 mark (`confidence`, which a
    family-14 document implies): ADR-0019's 2026-10-13 correction keeps #194's
    grant out of family 12.
    """

    _require(type(data["suspended"]) is bool)
    _require(type(data["suspensions"]) is list)
    previous = 0
    for entry in data["suspensions"]:
        _keys(entry, _SUSPENSION)
        seq = entry["seq"]
        _require(type(seq) is int and seq > previous)
        previous = seq
        reason = entry["reason"]
        if final and reason == FINAL_VOCABULARY.incomplete_reason:
            # Family 14 (F0 M4): family 13's `cohort_incomplete` renamed, its
            # owner arbitrated over its `final_rows` likewise.
            _require(entry["owner"] in OWNERS)
            _require(entry["evidence_kind"] == FINAL_VOCABULARY.suspension_evidence_kind)
            continue
        if reason in CONFIDENCE_SUSPENSIONS:
            # Family 13 (plan choice 12): `cohort_incomplete`'s owner is
            # arbitrated over its rows, so any threshold owner may hold it.
            _require(not final and entry["owner"] in OWNERS)
            _require(entry["evidence_kind"] == CONFIDENCE_SUSPENSIONS[reason][1])
            continue
        if reason in CHAMPION_SUSPENSIONS:
            # #194 spec §3.5: `champion_incomplete`'s owner is arbitrated over
            # its rows too. Families 13 and 14 only (ADR-0019, 2026-10-13).
            _require(confidence and entry["owner"] in OWNERS)
            _require(entry["evidence_kind"] == CHAMPION_SUSPENSIONS[reason][1])
            continue
        _require(reason in SUSPENSION_REASONS)
        _require(
            final or (reason not in GROUPER_SUSPENSIONS and reason not in CONNECTION_SUSPENSIONS)
        )
        owner, primary = SUSPENSION_REASONS[reason]
        _require(entry["owner"] == owner)
        _require(
            entry["evidence_kind"] == primary
            or entry["evidence_kind"] in ALTERNATE_SUSPENSION_KINDS.get(reason, frozenset())
        )
    _require(not data["suspended"] or bool(data["suspensions"]))
    window = data["indeterminate"]["window"]
    _keys(window, _WINDOW | ({"restarted"} if "restarted" in window else set()))
    _require(set(window["counts"]) == set(OWNERS))
    for value in window["counts"].values():
        _count(value)
    _count(window["denominator"])
    _count(window["start"], nullable=True)
    _require(window["start"] == (previous or None))
    if "restarted" in window:
        restarted = window["restarted"]
        _require(type(restarted) is dict and bool(restarted) and set(restarted) <= set(OWNERS))
        for owner_window in restarted.values():
            _keys(owner_window, _RESTARTED_WINDOW)
            _count(owner_window["start"])
            _require(owner_window["start"] > previous)
            _count(owner_window["count"])
            _count(owner_window["denominator"])
    _require(type(data["condition_changes"]) is list)
    change_seq = 0
    for change in data["condition_changes"]:
        _keys(change, set(_CONDITION_CHANGE))
        _require(type(change["seq"]) is int and change["seq"] > change_seq)
        change_seq = change["seq"]
        _validate_change(change)
        _sha(change["authorization_id"])
        _sha(change["identity_digest"])
    # One caveat per change, naming the same change (I2 spec §4.4).
    caveats = [item for item in data["caveats"] if item["kind"] == IN_PLACE_KIND]
    _require(
        [(c["seq"], c["effect"], c["fields"], c["round"]) for c in caveats]
        == [(c["seq"], c["effect"], c["fields"], c["round"]) for c in data["condition_changes"]]
    )
    epochs = len(data["condition_changes"])
    for entry in data["measurements"]:
        _require(type(entry["condition_epoch"]) is int and 0 <= entry["condition_epoch"] <= epochs)
        if unreadable and entry.get("classification") == "unreadable":
            _require(
                all(entry[name] is None for name in _UNREADABLE_COUNTS - {"tries", "imported"})
            )
            continue
        _count(entry["call_retries"])
        _count(entry["repair_tries"])
        _count(entry["repairs_scored"])
        failures = entry["call_failures_by_code"]
        _require(type(failures) is dict)
        for code, count in failures.items():
            _identifier(code)
            _require(type(count) is int and count >= 1)


class ProjectionInvalid(SchemaError):
    """A public projection failed validation; secret refusals remain SchemaError."""


@dataclass(frozen=True, init=False)
class CampaignProjection:
    """A versioned public campaign view; canonical bytes are the whole state."""

    _canonical: bytes

    def __init__(self, payload: Mapping[str, Any], *, secrets: Sequence[str] = ()) -> None:
        try:
            raw = canonical_json(payload)
            data = json.loads(raw)
            _validate(data)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise ProjectionInvalid("invalid public projection") from None
        if any(
            type(secret) is not str or (secret and secret in raw.decode()) for secret in secrets
        ):
            raise SchemaError("sensitive public projection")
        object.__setattr__(self, "_canonical", raw)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, secrets: Sequence[str] = ()
    ) -> CampaignProjection:
        return cls(payload, secrets=secrets)

    @classmethod
    def from_bytes(cls, raw: bytes, *, secrets: Sequence[str] = ()) -> CampaignProjection:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result

        try:
            return cls(json.loads(raw, object_pairs_hook=unique), secrets=secrets)
        except ProjectionInvalid:
            raise
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise SchemaError("invalid public projection") from None

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    def encode(self) -> bytes:
        return self._canonical + b"\n"


# -- shared reductions ----------------------------------------------------


def champion_measurement(snapshot: CampaignSnapshot) -> tuple[str | None, str]:
    """The measurement that measures the current champion, and its origin.

    The latest round that bound a baseline decides (I2 spec §6.3): its promoted
    winner's measurement, or else the baseline it bound, which measured the
    champion that round started from and that no later round replaced. The
    origin is `promoted_attempt` when any round promoted, `seed` otherwise.

    Without an in-place change this is exactly the promoted winner of the
    latest promoting round, or the seed measurement: a later round's baseline
    is that winner's measurement or the previous baseline (`campaign_view.
    _baseline_measurement`). An in-place change is what makes them differ: a
    `continue` re-binds an open baseline to `<id>@e<k>` and moves the round's
    `baseline_measurement_id` there, and a `rebaseline` binds a fresh
    `m-r<NN>-champion@e<k>`. Either way the frozen measurement the chain would
    otherwise name is not the one the champion was last measured by.
    In family 12 the promoting round's completion is that measurement once it is
    bound (G2 spec §4.7). In family 13 a round that promoted or retained names
    its selected cohort measurement.
    """

    promoted = any(
        state.closed is not None and state.closed["outcome"] == "promoted"
        for state in snapshot.rounds.values()
    )
    origin = "promoted_attempt" if promoted else "seed"
    for number in sorted(snapshot.rounds, reverse=True):
        state = snapshot.rounds[number]
        selected = None if state.selection is None else state.selection.get("measurement_id")
        if (
            selected is not None
            and state.closed is not None
            and state.closed["outcome"] in ("promoted", "retained")
        ):
            # Family-13 design §6.12: the selected cohort measurement measures
            # the champion; a linked successor imports from it (F7, D10).
            return str(selected), origin
        if state.closed is not None and state.closed["outcome"] == "promoted":
            if state.completion_measurement_id is not None:
                # G2b4: a family-12 winner is measured over the corpus by its
                # completion; a linked successor imports from it.
                return state.completion_measurement_id, origin
            assert state.selection is not None
            winner = state.attempts[str(state.selection["attempt_id"])]
            return winner.measurement_id, origin
        if state.baseline_measurement_id is not None:
            return state.baseline_measurement_id, origin
    seed = measurement_id_for(None)
    return (seed if seed in snapshot.measurements else None), origin


#: Baseline-import consumers (`baseline_import.py`, spec §5.1) read the source's
#: champion measurement through this public name; kept as an alias so this
#: module's own internal callers need no rewrite.
_champion_measurement = champion_measurement


def _resilience_counts(snapshot: CampaignSnapshot) -> dict[str, int]:
    """Spec §6.3: how many rounds a skip closed, how many Attempts took a
    continuation turn, and how many of those the Editor then finished
    `edited` -- so the operator can see whether the turn pays for itself,
    without recounting the per-entry marks by hand."""

    rounds_skipped = sum(
        1
        for state in snapshot.rounds.values()
        if state.closed is not None and state.closed.get("visitation") == "skipped"
    )
    continued = [
        attempt
        for state in snapshot.rounds.values()
        for attempt in state.attempts.values()
        if attempt.editor_continued
    ]
    return dict(
        rounds_skipped=rounds_skipped,
        continuation_turns=len(continued),
        attempts_saved=sum(1 for attempt in continued if attempt.editor == "edited"),
    )


def _suspensions(snapshot: CampaignSnapshot) -> list[dict[str, Any]]:
    """The public half of every `campaign_suspended`, oldest first (spec §5.2).

    What the operator has to act on -- which barrier suspended, why, whose
    fault and what kind of evidence stands behind it. The evidence itself is
    the journal's; the audit is what re-checks it.
    """

    return [
        dict(
            seq=entry["seq"],
            reason=entry["reason"],
            owner=entry["owner"],
            evidence_kind=entry["evidence_kind"],
        )
        for entry in snapshot.suspensions
    ]


def _baseline_excluded(
    snapshot: CampaignSnapshot,
    baseline_id: str | None,
    look: int,
    tries: Mapping[str, MeasurementTries],
) -> int:
    """How many baseline pairs a look excluded (spec §4.3): exhausted repeats `<= look`.

    `0` when the baseline recorded no `pairs_excluded`.
    """

    if baseline_id is None or baseline_id not in snapshot.exclusions:
        return 0
    facts = _facts(tries, baseline_id)
    return sum(1 for repeat in facts.exhausted_repeats if repeat <= look)


def _facts(tries: Mapping[str, MeasurementTries], measurement_id: str) -> MeasurementTries:
    """A declared measurement's try facts; a projection is never built without them."""

    facts = tries.get(measurement_id)
    if facts is None:
        raise SchemaError("invalid public projection: a measurement has no try facts")
    return facts


def _rounds(
    snapshot: CampaignSnapshot, tries: Mapping[str, MeasurementTries]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # Family 12: each round's own condition-epoch policy, never the policy in
    # force -- a later `rebaseline` must not rewrite an earlier round's claim.
    policies = round_policies(snapshot)
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        attempts = []
        for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
            entry: dict[str, Any] = dict(
                attempt_id=attempt.attempt_id,
                index=attempt.index,
                outcome=attempt.outcome,
                reason=attempt.reason,
                look=attempt.look,
                # Family 13 spends no alpha (plan choice 3): `null`, never a zero.
                alpha_total=(None),
                measurement_id=attempt.measurement_id,
                artifact_locator=(
                    None
                    if attempt.candidate_digest is None
                    else CANDIDATE_LOCATOR + attempt.attempt_id
                ),
            )
            entry["continued"] = attempt.editor_continued
            if policies is not None:
                entry["affected"] = attempt_affected_entry(
                    snapshot, attempt, policies[number], confidence=True
                )
            entry["confidence"] = attempt_confidence_entry14(attempt)
            entry["looks"] = [
                dict(
                    look=look,
                    baseline_excluded=_baseline_excluded(
                        snapshot, state.baseline_measurement_id, look, tries
                    ),
                )
                for look in sorted(attempt.looks)
            ]
            attempts.append(entry)
        row: dict[str, Any] = dict(
            round=number,
            baseline_measurement_id=state.baseline_measurement_id,
            baseline_complete=state.baseline_complete,
            outcome=None if state.closed is None else state.closed["outcome"],
            champion_complete=state.champion_complete,
            attempts=attempts,
        )
        scope = state.gate_scope
        row["gate_scope"] = (
            None if scope is None else {name: scope[name] for name in sorted(_GATE_SCOPE_REPORT)}
        )
        row["reason"] = None if state.closed is None else state.closed["reason"]
        row["visitation"] = None if state.closed is None else state.closed.get("visitation")
        row["final"] = round_final_entries(snapshot, number)
        rows.append(row)
    return rows


def _alpha(snapshot: CampaignSnapshot) -> dict[str, Any] | None:
    return None


def _indeterminate(snapshot: CampaignSnapshot) -> dict[str, Any]:
    payload = dict(
        counts={owner: snapshot.counts.get(owner, 0) for owner in OWNERS},
        denominator=snapshot.denominator,
        threshold_reached=None if snapshot.threshold is None else dict(snapshot.threshold),
    )
    payload["lineage"] = (
        None
        if snapshot.lineage_counts is None
        else dict(
            counts={owner: snapshot.lineage_counts[owner] for owner in OWNERS},
            denominator=snapshot.lineage_denominator,
        )
    )
    window: dict[str, Any] = dict(
        counts={owner: snapshot.window_counts.get(owner, 0) for owner in OWNERS},
        denominator=snapshot.window_denominator,
        start=snapshot.window_start,
    )
    if snapshot.owner_window:
        # I2 spec §6.5: an in-place change of an owner's own field restarted
        # that owner's window after the latest suspension; the threshold
        # counts it from there. Present only then, so a run without such a
        # change keeps the I1 shape.
        window["restarted"] = {
            owner: dict(start=start, count=count, denominator=denominator)
            for owner, (start, count, denominator) in sorted(snapshot.owner_window.items())
        }
    payload["window"] = window
    return payload


def _linked_from(snapshot: CampaignSnapshot) -> dict[str, Any] | None:
    """Spec §8's `linked_from`: the predecessor, its family, the first round, inherited alpha.

    `baseline` (spec §5.1/§6) is `None` for a fresh link and, for an inherit
    link, `{mode, imported, scheduled, abandoned}` plus `authorization_id`
    when `snapshot.baseline_abandoned` names the decision that abandoned the
    owed copy (spec §5.3).
    """

    linked = snapshot.identity.get("linked_from") if snapshot.identity else None
    if linked is None:
        return None
    declared = linked.get("baseline")
    baseline: dict[str, Any] | None = None
    if declared is not None:
        abandoned = snapshot.baseline_abandoned is not None
        baseline = dict(
            mode=declared["mode"],
            imported=declared["imported"],
            scheduled=declared["scheduled"],
            abandoned=abandoned,
        )
        if abandoned:
            assert snapshot.baseline_abandoned is not None
            baseline["authorization_id"] = snapshot.baseline_abandoned["authorization_id"]
    return dict(
        run_id=linked["run_id"],
        family=linked.get("predecessor_family"),
        first_round=linked.get("first_round"),
        rounds_closed=linked["rounds_closed"],
        alpha_rounds=dict(linked["alpha_rounds"]),
        lineage_caveats=list(linked.get("lineage_caveats", ())),
        baseline=baseline,
    )


def _measurement_entry(
    snapshot: CampaignSnapshot,
    measurement_id: str,
    entry: Mapping[str, Any],
    tries: Mapping[str, MeasurementTries],
    *,
    unreadable: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(
        measurement_id=measurement_id,
        candidate_digest=entry["candidate_digest"],
        locator=_measurement_locator(measurement_id),
    )
    exclusion = snapshot.exclusions.get(measurement_id)
    facts = None if unreadable else _facts(tries, measurement_id)
    payload["tries"] = None if facts is None else facts.tries
    payload["imported"] = None if facts is None else facts.imported
    payload["excluded_pairs"] = (
        None
        if exclusion is None
        else dict(
            count=exclusion["count"],
            by_owner=dict(exclusion["by_owner"]),
            pairs=[dict(pair) for pair in exclusion["pairs"]],
            truncated=exclusion["truncated"],
        )
    )
    facts = None if unreadable else _facts(tries, measurement_id)
    payload["call_retries"] = None if facts is None else facts.call_retries
    payload["call_failures_by_code"] = (
        None if facts is None else dict(sorted(facts.call_failures_by_code.items()))
    )
    # #78 spec §3.6: what a threshold resume's repairs did.
    payload["repair_tries"] = None if facts is None else facts.repair_tries
    payload["repairs_scored"] = None if facts is None else facts.repairs_scored
    # I2 spec §4.4: the condition epoch the measurement was bound under
    # (`0` for the creation identity).
    payload["condition_epoch"] = epoch_of(snapshot, measurement_id)
    return payload


def _measurement_locator(measurement_id: str) -> str:
    return f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"


def _final_champion(snapshot: CampaignSnapshot) -> dict[str, Any] | None:
    version, digest = snapshot.champion
    if not version or not digest:
        return None
    measurement_id, origin = _champion_measurement(snapshot)
    entry = snapshot.measurements.get(measurement_id or "")
    if entry is None:
        complete = False
    else:
        complete = recorded_complete(entry, snapshot.rounds)
    return dict(
        version=version,
        digest=digest,
        measurement_id=measurement_id,
        baseline_origin=origin,
        measurement_complete=complete,
    )


def _outcome(snapshot: CampaignSnapshot) -> dict[str, Any] | None:
    if snapshot.finished is None:
        return None
    finished = dict(snapshot.finished)
    payload = dict(
        outcome=finished["outcome"],
        reason=finished["reason"],
        continuation=dict(finished["continuation"]),
    )
    version = snapshot.champion[0]
    payload["champion_path"] = f"{CHAMPION_LOCATOR}{version}" if version else None
    return payload


def _add_condition_changes(snapshot: CampaignSnapshot, payload: dict[str, Any]) -> None:
    """I2 spec §4.4: family 7's applied in-place changes and their caveats.

    `condition_changes` is `[]` for a run without one; each change also adds
    one `condition_changed_in_place` caveat after the lineage kinds. Neither
    changes a statistic, a gate decision, an outcome or an exit code.
    """

    payload["condition_changes"] = [
        {key: (list(change[key]) if key == "fields" else change[key]) for key in _CONDITION_CHANGE}
        for change in snapshot.condition_changes
    ]
    payload["caveats"] = [*payload["caveats"], *in_place_caveats(snapshot.condition_changes)]


def _add_affected_caveats(
    snapshot: CampaignSnapshot,
    payload: dict[str, Any],
    affected_items: Mapping[int, Sequence[Mapping[str, Any]]],
) -> None:
    """Family 12 (G2 spec §4.6, §4.7, §4.9): the report-only caveats, after every other item.

    `affected_items` are computed once per report by `read_affected_items`:
    the completion checks' items, under each round's own epoch policy and the
    two measurements' own journal targets, and each Attempt's
    `components_changed_by_condition`, from the attested component maps; the
    focus-bias items read the same per-round policies. Family 13 adds each
    Attempt's method caveats and reads its finalists' cohort rows for
    `provenance_drift` (design §10, D8). Family 14 reads its own kinds and
    sort keys, leads with `final_campaign_caveats` and adds
    `stored_pairs_excluded` (H12b plan choices 12-13). Nothing is journaled
    and no alpha is charged.
    """

    items = affected_caveats(
        snapshot,
        round_policies(snapshot),
        affected_items,
        confidence=True,
        final=True,
    )
    payload["caveats"] = [*payload["caveats"], *items]


def _add_grouping_caveats(snapshot: CampaignSnapshot, payload: dict[str, Any]) -> None:
    """Model error grouping spec §10: family 9's `grouping_fallback` caveats.

    Derived from the durable grouping results, never journaled on their own;
    after the lineage kinds they merge with the in-place items by journal
    sequence, each keeping its own shape. Neither changes a statistic, a gate
    decision, an outcome or an exit code.
    """

    lineage = [item for item in payload["caveats"] if item["kind"] != IN_PLACE_KIND]
    ordered = [
        (int(item["seq"]), item) for item in payload["caveats"] if item["kind"] == IN_PLACE_KIND
    ]
    ordered.extend(grouping_caveat_entries(snapshot))
    payload["caveats"] = [*lineage, *(item for _, item in sorted(ordered, key=lambda e: e[0]))]


def _add_final_blocks(snapshot: CampaignSnapshot, payload: dict[str, Any]) -> None:
    """Family 14's top-level blocks (H12b plan choices 9-11; F0 M7, C§6.3).

    The latest connection preflight, the kinds of the three roles in force and
    the comparison settings in force, each a pure function of the snapshot.
    The `connection` object is the family-14 shape mark. None changes a
    statistic, a gate decision, an outcome or an exit code.
    """

    payload["connection"] = connection_block(snapshot)
    payload["roles"] = roles_block(snapshot)
    payload["settings"] = settings_block(snapshot)


# -- the two projections --------------------------------------------------


def project_result(
    snapshot: CampaignSnapshot,
    *,
    tries: Mapping[str, MeasurementTries],
    affected_items: Mapping[int, Sequence[Mapping[str, Any]]] = MappingProxyType({}),
    independent_report: Mapping[str, Any] | None = None,
    secrets: Sequence[str] = (),
) -> CampaignProjection:
    """Spec §12: the campaign's own result claim, over campaign facts alone.

    Family 6 also reports each measurement's `tries` and each evaluated look's
    `baseline_excluded` (continuable-terminals spec §8), which are facts of the
    measurement journals. `tries` is required and must name every declared
    measurement (`read_measurement_tries`); families 3-5 ignore it. A family-6
    projection without a measurement's facts is refused, never reported null.
    Family 12 reads `affected_items` (`read_affected_items`), the
    report-only completion and component-drift caveats already computed;
    without them none is reported.
    """

    selection = []
    for number in sorted(snapshot.rounds):
        state = snapshot.rounds[number]
        if state.selection is None:
            continue
        selection.append(
            dict(
                round=number,
                attempt_id=state.selection["attempt_id"],
                **(
                    {"data": dict(state.family15.selection.payload["data"])}
                    if snapshot.family_version == 15
                    and state.family15 is not None
                    and state.family15.selection is not None
                    else {"ranking": [dict(row) for row in state.selection["ranking"]]}
                ),
                outcome=None if state.closed is None else state.closed["outcome"],
            )
        )
    store = snapshot.identity.get("artifact_store") if snapshot.identity else None
    seed = measurement_id_for(None)
    initial = (
        None
        if store is None
        else dict(
            version="v0",
            digest=store["seed_candidate_digest"],
            measurement_id=seed if seed in snapshot.measurements else None,
        )
    )
    payload = dict(
        schema_version=1,
        kind="campaign_result",
        run_id=snapshot.run_id,
        identity_digest=snapshot.identity_digest or None,
        rebases=[{key: entry[key] for key in _REBASE_KEYS} for entry in snapshot.rebases],
        consistency="durable",
        complete=snapshot.finished is not None,
        claim_scope="campaign",
        campaign_outcome=_outcome(snapshot),
        selection=selection,
        initial_champion=initial,
        final_champion=_final_champion(snapshot),
        rounds=_rounds(snapshot, tries),
        alpha=_alpha(snapshot),
        indeterminate=_indeterminate(snapshot),
        measurements=[
            _measurement_entry(snapshot, measurement_id, entry, tries)
            for measurement_id, entry in sorted(snapshot.measurements.items())
        ],
    )
    if snapshot.family_version == 15:
        from evolution.tree.family15_report import campaign15_projection

        payload["family_version"] = 15
        payload["independent"] = (
            dict(independent_report)
            if independent_report is not None
            else campaign15_projection(snapshot)
        )
    if snapshot.family_version == 15:
        for name in ("check_modes", "shadow_hits"):
            independent = cast(dict[str, Any], payload["independent"])
            if independent.get(name):
                payload[name] = dict(independent[name])
    payload["linked_from"] = _linked_from(snapshot)
    payload["caveats"] = own_caveats(
        snapshot.identity.get("linked_from") if snapshot.identity else None,
        baseline_abandoned=snapshot.baseline_abandoned is not None,
    )
    payload["suspensions"] = _suspensions(snapshot)
    payload["suspended"] = snapshot.suspended
    _add_condition_changes(snapshot, payload)
    _add_grouping_caveats(snapshot, payload)
    _add_affected_caveats(snapshot, payload, affected_items)
    payload["resilience"] = _resilience_counts(snapshot)
    _add_final_blocks(snapshot, payload)
    if snapshot.corpus_partition is not None:
        payload["corpus_split"] = _partition_summary(snapshot.corpus_partition)
    return CampaignProjection(payload, secrets=secrets)


def _partition_summary(partition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope": "train",
        "mode": partition["mode"],
        "train_cases": sum(split == "train" for _, split in partition["assignments"]),
        "test_cases": sum(split == "test" for _, split in partition["assignments"]),
        "partition_digest": hashlib.sha256(canonical_json(partition)).hexdigest(),
    }


#: Family 14 says "final" where family 13 says "cohort" (PDR-0028's
#: 2026-10-09 amendment, F0 M4). The planner's classes keep their names, which
#: the executor dispatches on; a family-14 status names them by its own word.
_FINAL_ACTION_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "BindCohortMember": "BindFinalMember",
        "RunCohortStage": "RunFinalStage",
        "CompleteCohortMember": "CompleteFinalMember",
    }
)


def _action_payload(action: Action, *, final: bool = False) -> dict[str, Any]:
    assert is_dataclass(action)
    kind = type(action).__name__
    return dict(
        kind=_FINAL_ACTION_KINDS.get(kind, kind) if final else kind,
        fields={
            field.name: getattr(action, field.name)
            for field in fields(action)
            if not (field.name == "path" and getattr(action, field.name) == "candidate")
        },
    )


def project_status(
    view: CampaignView,
    *,
    secrets: Sequence[str] = (),
    verification: Mapping[str, Any] | None = None,
    run_root: Path | None = None,
    independent_report: Mapping[str, Any] | None = None,
) -> CampaignProjection:
    """Spec §12: what the view adds — siblings, S2 lineage and the §11 actions."""

    snapshot = view.snapshot
    actions: list[dict[str, Any]] = []
    raw_policy = snapshot.identity.get("policy") if snapshot.identity else None
    if raw_policy is not None:
        # Chosen by key presence, matching every other journaled-policy reader
        # (`campaign_identity.py`, `campaign_schema._adaptive_identity_and_policy`).
        policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
        actions = [_action_payload(action, final=True) for action in next_actions(view, policy)]
    lineage = None
    if view.lineage is not None:
        lineage = dict(
            store_state=view.lineage.store_state,
            stopped_code=view.lineage.stopped_code,
            champion=(None if view.lineage.champion is None else view.lineage.champion.version_id),
            champions=[entry.version_id for entry in view.lineage.champions],
            candidates=[
                dict(attempt_id=entry.attempt_id, state=entry.state)
                for entry in view.lineage.candidates
            ],
        )
    measurements = []
    for measurement_id, entry in sorted(snapshot.measurements.items()):
        status = view.measurements.get(measurement_id)
        measurements.append(
            dict(
                _measurement_entry(
                    snapshot,
                    measurement_id,
                    entry,
                    view.tries,
                    unreadable=view.siblings.get(measurement_id) == "unreadable",
                ),
                classification=view.siblings.get(measurement_id, "absent"),
                status=None if status is None else dict(status),
            )
        )
    payload: dict[str, Any] = dict(
        schema_version=1,
        kind="campaign_status",
        run_id=snapshot.run_id,
        identity_digest=snapshot.identity_digest or None,
        rebases=[{key: entry[key] for key in _REBASE_KEYS} for entry in snapshot.rebases],
        consistency="durable",
        complete=snapshot.finished is not None,
        claim_scope="campaign",
        campaign_outcome=_outcome(snapshot),
        final_champion=_final_champion(snapshot),
        rounds=_rounds(snapshot, view.tries),
        alpha=_alpha(snapshot),
        indeterminate=_indeterminate(snapshot),
        measurements=measurements,
        siblings=dict(sorted(view.siblings.items())),
        lineage=lineage,
        actions=actions,
        verification=None if verification is None else dict(verification),
    )
    if not snapshot.identity:
        # No admitted creation means no role, policy or family-specific facts.
        # Keep the existing minimal fault status even though an empty replay
        # now defaults to family 14 (ADR-0046), not the removed base family.
        payload["alpha"] = {"per_round": {}, "per_attempt": {}}
        payload["indeterminate"] = {
            key: payload["indeterminate"][key]
            for key in ("counts", "denominator", "threshold_reached")
        }
        return CampaignProjection(payload, secrets=secrets)
    if snapshot.family_version == 15:
        from evolution.tree.family15_report import campaign15_projection

        payload["family_version"] = 15
        payload["independent"] = (
            dict(independent_report)
            if independent_report is not None
            else campaign15_projection(
                snapshot,
                run_root=run_root,
                preflights=view.global_preflights,
                unavailable="unreadable" in view.siblings.values(),
            )
        )
    if snapshot.family_version == 15:
        for name in ("check_modes", "shadow_hits"):
            independent = cast(dict[str, Any], payload["independent"])
            if independent.get(name):
                payload[name] = dict(independent[name])
    payload["linked_from"] = _linked_from(snapshot)
    payload["caveats"] = own_caveats(
        snapshot.identity.get("linked_from") if snapshot.identity else None,
        baseline_abandoned=snapshot.baseline_abandoned is not None,
    )
    payload["suspensions"] = _suspensions(snapshot)
    payload["suspended"] = snapshot.suspended
    _add_condition_changes(snapshot, payload)
    _add_grouping_caveats(snapshot, payload)
    _add_affected_caveats(snapshot, payload, view.affected_items)
    payload["resilience"] = _resilience_counts(snapshot)
    _add_final_blocks(snapshot, payload)
    if snapshot.corpus_partition is not None:
        payload["corpus_split"] = _partition_summary(snapshot.corpus_partition)
    return CampaignProjection(payload, secrets=secrets)


# -- read-only inspection -------------------------------------------------


_LOCATOR = re.compile(r"(?P<file>[^#]+)#bytes=(?P<start>[0-9]+):(?P<end>[0-9]+)\Z")


def _reader_family_version(run_root: Path) -> int:
    """Select an exact supported reader family from the creation record."""

    from evolution.tree.campaign import CampaignFamilyUnsupported  # circular at module scope

    try:
        family_version = read_family_version(run_root)
    except (ValueError, JournalFault, OSError):
        # No legible creation is not an unversioned creation (ADR-0046).
        # Preserve the empty/faulted-prefix scan, including watch's half-line
        # handling, without selecting a removed family's grammar.
        return FINAL_CAMPAIGN_FAMILY_VERSION
    if family_version is None or family_version < FINAL_CAMPAIGN_FAMILY_VERSION:
        raise CampaignFamilyUnsupported(family_version, reader=FINAL_CAMPAIGN_FAMILY_VERSION)
    try:
        campaign_family_for(family_version)
    except SchemaError as exc:
        raise CampaignFamilyUnsupported(family_version, reader=5) from exc
    return family_version


def read_measurement_tries(
    run_root: Path, snapshot: CampaignSnapshot, siblings: Mapping[str, str]
) -> dict[str, MeasurementTries]:
    """Every declared measurement's try facts, read from its durable journal (spec §8).

    The one source both the executor's result and `inspect_view` use, so a live
    result and a read-back result report the same `tries`. A measurement whose
    journal is not born has run no try (`0`). Families before 6 report none, so
    nothing is read for them. A journal that cannot be read raises `JournalFault`
    (or `OSError` from the file system), a prefix that cannot be reduced included.
    """

    facts: dict[str, MeasurementTries] = {}
    for measurement_id in snapshot.measurements:
        if siblings.get(measurement_id) == "unreadable":
            continue
        if siblings.get(measurement_id) != "born":
            facts[measurement_id] = MeasurementTries(tries=0, exhausted_repeats=())
            continue
        journal = inspect_journal(Path(run_root) / MEASUREMENTS_DIR / measurement_id)
        try:
            facts[measurement_id] = measurement_tries(journal.records)
        except ValueError as exc:
            # A prefix S1's lifecycle refuses to reduce is an unreadable
            # measurement journal: it travels as that journal fault, so callers
            # classify it without widening their own `ValueError` handling.
            raise JournalFault("journal_unavailable") from exc
    return facts


def inspection_prefix_readable(snapshot: CampaignSnapshot, name: str, log: Any) -> bool:
    """The existing inspection integrity rule, shared by observational costs.

    ADR-0020 R11 permits only an unfinished frozen S1's admitted prefix.
    Active/completed S1 and S2 remain strict; no corrupt suffix is admitted.
    """
    entry = snapshot.measurements.get(name)
    frozen_prefix = (
        entry is not None
        and entry["frozen"]
        and not recorded_complete(entry, snapshot.rounds)
        and log.journal_integrity == "incomplete_tail"
        and bool(log.records)
        and log.records[0].payload["identity_digest"] == entry["identity_digest"]
        and log.records[0].payload["identity"]["candidate_digest"] == entry["candidate_digest"]
    )
    return log.journal_integrity == "complete" or frozen_prefix


def _inspection_siblings(run_root: Path, snapshot: CampaignSnapshot) -> dict[str, InspectionBirth]:
    """Family 15 keeps trusted campaign facts despite unsafe sibling evidence.

    Executor classification stays absent/unborn/born. Only inspection can emit
    unreadable, which neither supplies live statuses nor authorizes actions.
    """
    if snapshot.family_version != 15:
        return dict(classify_siblings(run_root, snapshot))
    from evolution.tree.family15_audit import EvidenceMismatch, _read_owned

    births: dict[str, InspectionBirth] = {}
    for name in (STORE_SIBLING, *snapshot.measurements):
        store = name == STORE_SIBLING
        directory = run_root if store else run_root / MEASUREMENTS_DIR / name
        path = directory / (LEDGER_FILE if store else "events.jsonl")
        try:
            raw = _read_owned(path)
            if not raw:
                births[name] = "unborn"
                continue
            log, _ = _scan(
                raw,
                MAX_LEDGER_RECORD_BYTES if store else MAX_RECORD_BYTES,
                ArtifactLifecycle if store else Lifecycle,
                ARTIFACT_FAMILY if store else S1_FAMILY,
            )
            births[name] = (
                "unreadable"
                if not inspection_prefix_readable(snapshot, name, log)
                else "born"
                if log.records
                else "unborn"
            )
        except FileNotFoundError:
            births[name] = "unborn" if not store and directory.is_dir() else "absent"
        except (OSError, JournalFault, EvidenceMismatch):
            births[name] = "unreadable"
    return births


def inspect_view(run_root: Path, *, verify: bool = False) -> CampaignView:
    """Assemble a view of any run root read-only: no lease, no write, no repair."""

    run_root = Path(run_root)
    family_version = _reader_family_version(run_root)
    log = inspect_log(
        run_root,
        file_name=CAMPAIGN_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=lambda: CampaignLifecycle(selector=None, family_version=family_version),
        envelope_family=campaign_family_for(family_version),
    )
    snapshot = reduce(log.records, selector=None, family_version=family_version)
    siblings = _inspection_siblings(run_root, snapshot)
    measurements: dict[str, Mapping[str, Any]] = {}
    repairs: dict[str, frozenset[tuple[str, str]]] = {}
    prefixes: dict[str, tuple[Envelope, ...]] = {}
    raw_policy = snapshot.identity.get("policy") if snapshot.identity else None
    primary = None if raw_policy is None else str(raw_policy["metric"]["primary"])
    for measurement_id in snapshot.measurements:
        if siblings.get(measurement_id) != "born":
            continue
        journal = inspect_journal(run_root / MEASUREMENTS_DIR / measurement_id)
        measurements[measurement_id] = project_measurement_status(journal).to_payload()
        prefixes[measurement_id] = journal.records
        if primary is not None:
            # The executor's own owed-repair derivation (I1 spec §5.4).
            owed = owed_repairs_in(snapshot, measurement_id, journal.records, primary)
            if owed:
                repairs[measurement_id] = owed
    lacking: frozenset[str] = frozenset()
    rebound_held: frozenset[str] = frozenset()
    if (raw_policy is not None) and ("unreadable" not in siblings.values()):
        # #114: the executor's held and re-bound held Attempts, by the same
        # selection (`held_attempts`, `rebound_held_attempts` over
        # `rows_held`) over the measurement prefixes, so the §12 report plans
        # what the executor would. With the owed repairs above, that is every
        # sample-row fact the planner reads.
        policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
        states: dict[str, Lifecycle] = {}

        def rows_at(measurement_id: str, look: int) -> tuple[SampleRow, ...]:
            records = prefixes[measurement_id]
            state = states.get(measurement_id)
            if state is None:
                state = states[measurement_id] = Lifecycle()
                for record in records:
                    state.accept(record)
            return latest_rows(records, state, look=look)

        def range_of(measurement_id: str) -> PrimaryRange:
            # `_Campaign.range_of`: the producing version in force and the
            # version the measurement's carried rows were admitted under.
            return primary_range_for(
                family_version,
                snapshot.producing_version,
                snapshot.import_versions.get(measurement_id),
            )

        def max_repeat_of(measurement_id: str) -> int:
            # `Measurement.max_repeat` (family-13 design §5).
            return _scheduled_max_repeat(prefixes[measurement_id])

        selection = (
            rows_at,
            range_of,
            max_repeat_of,
            prefixes,
            policy.looks,
            policy.metric_primary,
        )
        lacking = held_attempts(snapshot, *selection)
        rebound_held = rebound_held_attempts(snapshot, *selection)
    tries = read_measurement_tries(run_root, snapshot, siblings)
    affected_items = read_affected_items(run_root, snapshot, siblings, records=log.records)
    lineage = None
    if siblings[STORE_SIBLING] == "born":
        lineage = project_lineage(
            inspect_log(
                run_root,
                file_name=LEDGER_FILE,
                max_record_bytes=MAX_LEDGER_RECORD_BYTES,
                max_log_bytes=MAX_LEDGER_BYTES,
                state_factory=ArtifactLifecycle,
                envelope_family=ARTIFACT_FAMILY,
            )
        )
    preflights = {}
    if family_version == 15 and raw_policy is not None and "unreadable" not in siblings.values():
        from evolution.tree.case_comparison import admissible
        from evolution.tree.family15_audit import EvidenceMismatch, _read_owned
        from evolution.tree.family15_report import global_preflight

        policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
        if policy.global_mode != "off":
            for number, round_state in snapshot.rounds.items():
                mid = round_state.baseline_measurement_id
                if (
                    round_state.closed is not None
                    or not round_state.baseline_complete
                    or round_state.grouping is not None
                    or mid is None
                ):
                    continue
                try:
                    source_records = cited_baseline_prefix(
                        log.records, number, mid, lambda name: _read_owned(run_root / name)
                    )
                    source_state = Lifecycle()
                    for record in source_records:
                        source_state.accept(record)
                    source_rows = latest_rows(source_records, source_state)
                    # Both modes require a settled baseline before preflight.
                    # A valid shorter prefix may only contain missing rows;
                    # fresh-reference arithmetic must not bypass this barrier.
                    if any(
                        not admissible(row, policy.metric_primary)
                        and not (row.exhausted and row.disposition != "score")
                        for row in source_rows
                    ):
                        raise BaselineUnavailable("the cited baseline is not settled evidence")
                except (BaselineUnavailable, EvidenceMismatch, JournalFault, OSError):
                    # A readable S1 can still have an unavailable cited prefix.
                    # Keep campaign facts and let E explain the broken citation;
                    # unavailable evidence cannot authorize executor actions.
                    siblings[mid] = "unreadable"
                    measurements.pop(mid, None)
                    tries.pop(mid, None)
                    repairs.pop(mid, None)
                    continue
                created = source_records[0].payload
                reference = dict(
                    measurement_id=mid,
                    identity=created["identity"],
                    identity_digest=created["identity_digest"],
                    prefix=round_state.baseline_reference,
                )
                preflights[number] = global_preflight(
                    snapshot, policy, number, reference, source_rows
                )
    return CampaignView(
        global_preflights=preflights,
        snapshot=snapshot,
        measurements=measurements,
        lineage=lineage,
        siblings=siblings,
        tries=tries,
        repairs=repairs,
        affected_items=affected_items,
        lacking=lacking,
        rebound_held=rebound_held,
    )


class CampaignUnreadable(ValueError):
    """`campaign.jsonl` is present but this process cannot read a creation out of it.

    Distinct from `CampaignMissing("campaign_absent")` on purpose: the file exists,
    so a campaign was started in this run root, and its outcome is C1's to classify
    through its own scan (`persistence_failed`), never an orchestrator's refusal.
    It covers both ways of being illegible -- a readable file whose first record is
    not a creation, and a file the process cannot read at all.
    """


_PREVIOUS_CREATION_KEYS = frozenset(
    {"identity", "identity_digest", "producing_version", "alpha_attempt"}
)

# A probe family (spec §4): payload validation is a
# no-op and the operationless set holds `campaign_created`, so the envelope's
# common shape -- key types, duplicate-key and bound rules -- is exactly every
# other reader's, and only the campaign payload's own validation is bypassed.
# `CAMPAIGN_FAMILY`'s payload validation would reject the very record this
# probe exists to recognize (a previous-family `campaign_created`, or one of a
# later family), so no family other than this one can be used here.
_PROBE_FAMILY = EnvelopeFamily(
    lambda event, payload: None, frozenset({"campaign_created"}), lambda event: False
)


def read_family_version(run_root: Path) -> int | None:
    """The record family of the campaign journal's creation record (spec §4).

    Reads the first line only, through `Envelope.from_bytes` with the probe
    family above, deliberately *without* `CAMPAIGN_FAMILY`'s payload
    validation: that validation rejects the very record this probe exists to
    recognize. The envelope's common shape is still enforced in full -- a
    `schema_version` of `true`, a missing `run_id`, a repeated key are
    illegible, not a family (codex branch round 1) -- and only the campaign
    payload's own validation is skipped. Two steps then classify the payload:
    the unversioned previous-family shape, matched exactly, is `None`; any
    other payload whose `family_version` is an `int` (never a `bool`) is that
    integer, whatever else it holds — a later family may add keys *and* may
    drop them, because this probe reads only the shape and the one field,
    never the other values it would otherwise have to trust. Everything else
    — no journal is `CampaignMissing`; an empty file, garbage, a torn or
    over-bound first line, an illegible envelope, another event, another key
    set without a version, a version of another type — is `CampaignUnreadable`,
    and no value from the file reaches a message.
    """

    payload = _creation_payload(run_root)
    if set(payload) == _PREVIOUS_CREATION_KEYS:
        return None
    version = payload.get("family_version")
    if type(version) is int:
        return version
    raise CampaignUnreadable("campaign_unreadable")


def read_creation_policy(run_root: Path) -> object:
    """The raw `identity.policy` of the journal's creation record, or `None`.

    The same first-line probe as `read_family_version`, and the same refusals
    for a journal that is absent or illegible. Resume passes the value to
    `policy_class_for_payload` to learn the family the creation policy implies
    (I1 spec §6); the value is never trusted beyond its keys.
    """

    identity = _creation_payload(run_root).get("identity")
    return identity.get("policy") if isinstance(identity, Mapping) else None


def read_creation_adaptation_version(run_root: Path) -> str | None:
    """The adaptation slot's `version` in the journal's creation record, or `None`.

    The same first-line probe as `read_creation_policy`. Resume uses it with
    the creation policy to tell family 7 (`campaign_adaptation/1`) from family
    8 (`campaign_adaptation/2`, ADR-0021), family 9 (`campaign_adaptation/3`,
    ADR-0022) and family 10 (`campaign_adaptation/4`, ADR-0023); a non-string
    value is `None`.
    """

    adaptation = _creation_payload(run_root).get("adaptation")
    version = adaptation.get("version") if isinstance(adaptation, Mapping) else None
    return version if isinstance(version, str) else None


def _creation_payload(run_root: Path) -> Mapping[str, Any]:
    """The first record's payload, legible as a `campaign_created` envelope."""

    from evolution.tree.campaign import CampaignMissing  # circular at module scope

    path = Path(run_root) / CAMPAIGN_FILE
    if not path.exists():
        raise CampaignMissing("campaign_absent")
    try:
        with path.open("rb") as handle:
            first = handle.readline(MAX_RECORD_BYTES + 1)
    except OSError as exc:
        raise CampaignUnreadable("campaign_unreadable") from exc
    if not first.endswith(b"\n") or len(first) > MAX_RECORD_BYTES:
        raise CampaignUnreadable("campaign_unreadable")
    try:
        record = Envelope.from_bytes(first, family=_PROBE_FAMILY)
    except (SchemaError, ValueError, TypeError):
        raise CampaignUnreadable("campaign_unreadable") from None
    if record.event != "campaign_created":
        raise CampaignUnreadable("campaign_unreadable")
    return record.payload


def read_run_id(run_root: Path) -> str:
    """The `run_id` journaled in the envelope of `campaign_created` (W1 §6 step 5).

    An orchestrator must build the target adapter on the campaign's own id before
    it can resume — `HttpSessionAdapter` accepts only instance ids prefixed by the
    id given to its constructor — and the id is a fact of the journal, not of a
    sidecar (ADR-0008): it lives in the envelope of the first record and nowhere
    else. This reads it through the same reader, bounds and family as
    `inspect_view`, so a journal this refuses is exactly a journal a resume would
    refuse; only the first envelope is used, so a corrupt or torn tail behind it
    is irrelevant.

    The two ways to have no id are not the same fact and do not share a signal. A
    run root with no journal at all is `CampaignMissing("campaign_absent")`: there
    is no campaign, and a caller may say so. A journal that exists but yields no
    readable `campaign_created` is `CampaignUnreadable`: a campaign was started
    here, its id is simply not legible, and only C1's own scan may say what that
    means. Collapsing the second into the first told the operator the campaign was
    absent and bypassed C1's persistence classification of the very journal it was
    looking at.

    Illegible is not only malformed *contents*. An empty file, garbage bytes and a
    creation record torn by a crash between `open` and the completed append are one
    half; a file this process cannot read at all -- its permissions removed, a
    journal past `MAX_JOURNAL_BYTES`, a torn read the reader refuses -- is the
    other, and it arrives as `JournalFault` from the reader or as `OSError` from
    the file system. Both are the same fact for a caller that only wanted the id,
    and letting the second escape as an exception ended the CLI in a traceback
    rather than in C1's `persistence_failed` document.
    """

    from evolution.tree.campaign import CampaignMissing  # circular at module scope

    run_root = Path(run_root)
    if not (run_root / CAMPAIGN_FILE).exists():
        raise CampaignMissing("campaign_absent")
    try:
        family_version = _reader_family_version(run_root)
        log = inspect_log(
            run_root,
            file_name=CAMPAIGN_FILE,
            max_record_bytes=MAX_RECORD_BYTES,
            max_log_bytes=MAX_JOURNAL_BYTES,
            state_factory=lambda: CampaignLifecycle(selector=None, family_version=family_version),
            envelope_family=campaign_family_for(family_version),
        )
    except (JournalFault, OSError) as exc:
        raise CampaignUnreadable("campaign_unreadable") from exc
    if not log.records or log.records[0].event != "campaign_created":
        raise CampaignUnreadable("campaign_unreadable")
    return str(log.records[0].to_payload()["run_id"])


def verify_evidence(run_root: Path, corpus: Sequence[EvalCase] | None = None) -> dict[str, Any]:
    """Re-verify every E record against the sibling logs (spec §5, ADR-0008).

    Two checks per record, in order. **Reference integrity**: the locator is
    safe, the byte range exists and its `sha256` is the one the record cited.
    **The E-precondition itself**: the executor's own pure functions are
    re-evaluated over the bytes the evidence names, so a record whose evidence
    hashes perfectly but does not support what the record claims — a
    `baseline_complete` over an intact but incomplete measurement prefix, a
    `selection_decided` naming a loser — is reported `precondition_failed`. ADR
    -0008 asks for exactly this: evidence an audit over the view can re-verify.

    A record whose own references are already broken gets no precondition check:
    the specific failure is reported once, and a check over bytes that failed
    integrity would only say the same thing less clearly. The whole pass is
    read-only over the run root — the referenced ranges are parsed in memory,
    never written out — and reports rather than raises.

    `checked` keeps its original meaning, the number of references verified; the
    semantic pass is counted separately as `preconditions_checked`, so neither
    number changes meaning when the other grows.

    `corpus`, when supplied, is compared against the campaign identity's own
    durable `corpus_cases` (spec §3): a match makes `corpus` proof that the
    caller's corpus is the one the campaign identity and every recomputed
    `look_evaluated` verdict were built from (`"corpus": "proven"`), and a
    disagreement is reported as its own `corpus_mismatch` finding rather than
    a flood of downstream `look_evaluated` mismatches -- recomputation against
    a corpus already known to disagree with the journal would only repeat the
    same fact under a different name, so it is skipped (`_Evidence.recompute`)
    once a mismatch is found. Omitting `corpus` leaves every `look_evaluated`
    verdict recomputed from the journal's own `corpus_cases` alone
    (`"corpus": "journaled"`).
    """

    run_root = Path(run_root)
    context, log = _evidence_of(run_root)
    from evolution.tree.family15_audit import EVENTS as FAMILY15_AUDIT_EVENTS
    from evolution.tree.family15_audit import Family15Audit

    references = 0
    preconditions = 0
    mismatches: list[dict[str, Any]] = _refused_suffix(run_root, log)
    corpus_state = "journaled"
    if corpus is not None and log.records:
        # ADR-0020: the caller's corpus is today's manifest's, so it is proof
        # against the latest condition epoch; each look is still recomputed
        # from its own epoch's journaled `corpus_cases`.
        identity = context.latest_identity()
        projection = [
            dict(case_id=case.case_id, group_id=case.group_id, stratum=case.stratum)
            for case in corpus
        ]
        journaled_cases = [dict(case) for case in identity.corpus_cases]
        detail: str | None = None
        if projection != journaled_cases:
            if len(projection) != len(journaled_cases):
                detail = (
                    f"corpus_cases has {len(projection)} cases, "
                    f"the identity has {len(journaled_cases)}"
                )
            else:
                differing = next(
                    (
                        proposed["case_id"]
                        for proposed, journaled in zip(projection, journaled_cases, strict=False)
                        if proposed != journaled
                    ),
                    "order",
                )
                detail = f"corpus_cases differs at {differing}"
        elif corpus_digest(corpus) != identity.corpus_digest:
            # Same case ids, groups and strata, but a difference `corpus_cases`
            # cannot see -- tags, public, private or oracle content.
            detail = "the corpus digest is not the identity's"
        if detail is not None:
            corpus_state = "mismatch"
            context = replace(context, recompute=False)
            first = log.records[0].encode()
            mismatches.append(
                dict(
                    sequence=1,
                    event="campaign_created",
                    locator=f"{CAMPAIGN_FILE}#bytes=0:{len(first)}",
                    code="corpus_mismatch",
                    detail=detail,
                )
            )
        else:
            corpus_state = "proven"
    independent = (
        Family15Audit(context, corpus=corpus if corpus_state == "proven" else None)
        if context.family_version == 15
        else None
    )
    evidence_events = CONTINUABLE_E_RECORDS
    evidence_events = evidence_events | {context.vocabulary.measurement_completed}
    evidence_events = evidence_events | {
        context.vocabulary.measurement_bound,
        "attempt_input_frozen",
        "baseline_refresh_bound",
        "baseline_refresh_completed",
        # H10 (F0 R60, C§6.1): each preflight terminal cites its probe journal.
        *CONNECTION_TERMINALS,
    }
    offset = 0
    for index, record in enumerate(log.records, start=1):
        start, offset = offset, offset + len(record.encode())
        if independent is not None and record.event in FAMILY15_AUDIT_EVENTS:
            finding15 = independent.finding(record)
            if finding15 is not None:
                mismatches.append(finding15)
            continue
        if record.event in _SELF_LOCATED:
            # Family 7 (I1 spec §5.2): `campaign_suspended`'s evidence is a
            # discriminated object rather than a list of durable references;
            # family 12's `candidate_inspected` and `affected_cases_selected`
            # (G2b2) cite none. There is no reference integrity to check
            # first, and the record locates itself in the campaign log.
            preconditions += 1
            finding = _precondition_finding(context, record)
            if finding is not None:
                mismatches.append(
                    dict(
                        sequence=index,
                        event=record.event,
                        locator=f"{CAMPAIGN_FILE}#bytes={start}:{offset}",
                        code=finding[0],
                        detail=finding[1],
                    )
                )
            continue
        if record.event not in evidence_events and record.event not in CONDITIONAL_E_RECORDS:
            continue
        evidence = record.payload.get(
            "source_refs" if (record.event == "attempt_input_frozen") else "evidence"
        )
        if not evidence:
            continue
        intact = True
        for reference in evidence:
            references += 1
            code = _verify_reference(
                run_root, dict(reference), context.cache, nofollow=context.family_version == 15
            )
            if code is not None:
                intact = False
                mismatches.append(
                    dict(
                        sequence=index,
                        event=record.event,
                        locator=reference["locator"],
                        code=code,
                        detail=None,
                    )
                )
        if not intact:
            continue
        preconditions += 1
        finding = _precondition_finding(context, record)
        if finding is not None:
            mismatches.append(
                dict(
                    sequence=index,
                    event=record.event,
                    locator=evidence[0]["locator"],
                    code=finding[0],
                    detail=finding[1],
                )
            )
    mismatches.extend(_imported_copy_findings(context))
    mismatches.extend(_repair_findings(context))
    if log.journal_integrity != "corrupt_suffix":
        # #109: a refused suffix hides the campaign records the measurement
        # journals' later dispatches belong to; it is reported once, above.
        mismatches.extend(_stage_redispatch_findings(context))
    mismatches.extend(_attempt_closure_findings(context))
    mismatches.extend(_condition_findings(context))
    mismatches.extend(_refresh_findings(context))
    mismatches.extend(_grouper_chain_findings(context))
    mismatches.extend(_connection_findings(context))
    return dict(
        checked=references + (independent.checked if independent is not None else 0),
        preconditions_checked=preconditions
        + (independent.preconditions if independent is not None else 0),
        mismatches=mismatches,
        corpus=corpus_state,
    )


def _evidence_of(run_root: Path) -> tuple[_Evidence, Any]:
    """The audit's read of a run root: the lifecycle-admitted campaign prefix."""

    family_version = _reader_family_version(run_root)
    log = inspect_log(
        run_root,
        file_name=CAMPAIGN_FILE,
        max_record_bytes=MAX_RECORD_BYTES,
        max_log_bytes=MAX_JOURNAL_BYTES,
        state_factory=lambda: CampaignLifecycle(selector=None, family_version=family_version),
        envelope_family=campaign_family_for(family_version),
    )
    return _Evidence(run_root, log.records, {}, family_version=family_version), log


def _imported_copy_findings(context: _Evidence) -> list[dict[str, Any]]:
    """ADR-0017 audit scope over every born measurement, completed or not.

    Declaration-to-copy integrity is a fact about the measurement journal, not
    about a completion record citing it: a run cut after its carried baseline
    was born and before `baseline_complete` holds the whole copy already
    (codex branch review round 2). Every measurement the campaign bound is read
    whole, read-only, the way the sibling checks read a measurement prefix;
    one that is not born (an owed copy, no `events.jsonl` records) says
    nothing. A born one holding any `sample_imported` -- or that is the
    declared first-round baseline -- is held to the declaration by
    `_check_baseline_import`, and a failure is reported once, under the
    measurement's own `measurement_bound`. This is the only site: the
    completion preconditions do not repeat it.

    G2b4: a family-12 completion (`completion_bound`) is a born measurement
    too, held by `_check_completion_import` to its sealed source's re-read
    as well as to its own declaration, and reported under its
    `completion_bound`. A completion re-bound by `continue` is created by a
    `measurement_rebound`, whose carry `_check_rebound_carry` audits.

    Family 13 (design §6.8, ADR-0027): a cohort member carries nothing, so a
    born member journal holding any `sample_imported` is a finding, reported
    under its `cohort_measurement_bound`.
    """

    if not context.records:
        # No admitted creation record: the refused suffix is the finding, and
        # there is no declaration to hold a copy to.
        return []
    identity = context.records[0].payload.get("identity")
    linked = identity.get("linked_from") if isinstance(identity, Mapping) else None
    declared = linked.get("baseline") if isinstance(linked, Mapping) else None
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record in enumerate(context.records, start=1):
        if context.family_version == 15 and record.event == "final_measurement_bound":
            # Its candidate/champion summaries replace the old cohort shape;
            # family15_audit checks completion imports and fresh work explicitly.
            continue
        if record.event not in (
            "measurement_bound",
            "parent_measurement_bound",
            context.vocabulary.measurement_bound,
        ):
            continue
        measurement_id = str(record.payload["measurement_id"])
        if measurement_id in seen:
            continue
        seen.add(measurement_id)
        name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
        path = context.run_root / name
        raw = context.cache.get(name)
        if raw is None:
            if not path.is_file():
                continue
            try:
                raw = context.read_file(path)
            except _PreconditionFailed:
                findings.append(
                    _campaign_finding(
                        context,
                        index - 1,
                        "evidence_unsafe",
                        "measurement evidence is unsafe or unreadable",
                    )
                )
                continue
            context.cache[name] = raw
        # The valid prefix: a torn tail is the ordinary crash artefact, and the
        # carried rows are written at birth, ahead of anything a crash can tear.
        snapshot, _state = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        records = snapshot.records
        if not records:
            continue
        try:
            if record.event == context.vocabulary.measurement_bound:
                # ADR-0027: no gate or baseline row enters a cohort measurement.
                if any(entry.event == "sample_imported" for entry in records):
                    raise _PreconditionFailed(
                        member_diagnostic(
                            "a cohort member imports no rows", family_version=context.family_version
                        ),
                        "baseline_import_mismatch",
                    )
            else:
                holds = any(entry.event == "sample_imported" for entry in records)
                if not holds and not (
                    declared is not None and measurement_id == measurement_id_for(None)
                ):
                    continue
                _check_baseline_import(context, measurement_id, records)
        except _PreconditionFailed as failed:
            findings.append(
                dict(
                    sequence=index,
                    event=record.event,
                    locator=f"{name}#bytes=0:{len(raw)}",
                    code=failed.code,
                    detail=failed.detail,
                )
            )
        except _Unverifiable:
            continue
        except (KeyError, IndexError, TypeError, ValueError):
            findings.append(
                dict(
                    sequence=index,
                    event=record.event,
                    locator=f"{name}#bytes=0:{len(raw)}",
                    code="baseline_import_mismatch",
                    detail="a carried row does not have the shape of a sample_imported",
                )
            )
    return findings


class _Unverifiable(Exception):
    """The referenced bytes cannot support this check, so nothing is reported.

    Verification never guesses: a fact the durable bytes do not carry — a
    target's honesty — is not a mismatch.
    """


#: #78: the records whose threshold-repair checks read the lifecycle as it
#: stood just before them. #91/#97 spec §5 adds the records the held-Attempt
#: checks read it before: a barred look, a closure, an opening and a suspension.
_THRESHOLD_POINT_EVENTS = frozenset(
    {
        "repairs_dispatched",
        "pairs_excluded",
        "baseline_complete",
        "champion_complete",
        "measurement_rebound",
        "look_not_started",
        "attempt_closed",
        "attempt_opened",
        "campaign_suspended",
    }
)


@dataclass(frozen=True)
class _AttemptPoint:
    """#91 spec §3.1: what the held-Attempt checks read of one open Attempt.

    `sides` are its Candidate and, in family 12, fresh Parent measurements;
    `look` is its latest stage's look (`None` before any stage) and `decided`
    whether that look is evaluated or barred; `baseline` is its round's.
    `barred` is the lifecycle's `latest_look_barred`: an `incomplete_sample`
    closure after the bar is not re-checked against a threshold.
    """

    round: int
    sides: tuple[str, ...]
    look: int | None
    decided: bool
    baseline: str | None
    barred: bool = False


@dataclass(frozen=True)
class _ThresholdPoint:
    """#78: what the threshold-repair checks read of the lifecycle before one record.

    Only these fields of the `CampaignSnapshot`, so a long journal holds a few
    small mappings per checked record rather than whole snapshots. #91/#97
    (spec §5) adds whether a threshold is crossed over the windows there, the
    open Attempts, the completion versions and the slot policy in force.
    """

    threshold_named: Mapping[str, Mapping[tuple[str, str], tuple[int, int]]]
    repairs_reserved: Mapping[str, tuple[int, int]]
    window_start: int | None
    producing_version: str
    frozen: frozenset[str]
    crossed: bool = False
    attempts: Mapping[str, _AttemptPoint] = field(default_factory=dict)
    completion_versions: Mapping[str, str] = field(default_factory=dict)
    import_versions: Mapping[str, str] = field(default_factory=dict)
    parallel_attempts: int = 1
    independent: bool = True
    #: #120: the open Attempts' declined retries (`retries_declined`), which a
    #: held-repair version counts as settled (`rows_held(declined=...)`).
    retries_declined: frozenset[tuple[Any, ...]] = frozenset()

    def named(self, measurement_id: str) -> Mapping[tuple[str, str], tuple[int, int]]:
        return self.threshold_named.get(measurement_id) or {}


@dataclass(frozen=True)
class _ThresholdReplay:
    """#78 (spec §3.2, §3.4): one lifecycle replay of the campaign prefix.

    The audit does not re-derive which pairs a threshold suspension names: it
    replays `CampaignLifecycle` once and reads its `threshold_named`.
    `points` is the lifecycle before each record of `_THRESHOLD_POINT_EVENTS`,
    by index into the campaign records. `grants` is, per measurement and pair,
    `(ordinal, naming_seq)` for each suspension that granted a repair after
    try `ordinal` (a threshold one, or from a27 any one naming an open
    Attempt's pair) -- read right after that suspension, where its
    own `naming_seq` is the suspension's sequence, so a pair is granted at
    most once per suspension.
    A record the lifecycle refuses (a forged context) ends the replay: points
    past it are absent and their checks are unverifiable.
    """

    points: Mapping[int, _ThresholdPoint]
    grants: Mapping[str, Mapping[tuple[str, str], tuple[tuple[int, int], ...]]]
    #: ADR-0019 (amended 2026-10-03): per measurement, the version its carried
    #: rows were admitted under, as the replay ended. Fixed once its binding
    #: is accepted, so it holds for every record after that binding.
    import_versions: Mapping[str, str] = field(default_factory=dict)


def _threshold_point(state: CampaignLifecycle) -> _ThresholdPoint:
    snapshot = state.snapshot
    attempts: dict[str, _AttemptPoint] = {}
    for round_state in snapshot.rounds.values():
        for attempt in round_state.attempts.values():
            if attempt.outcome is not None:
                continue
            entry = snapshot.measurements.get(str(attempt.measurement_id))
            stages = entry["stages"] if entry is not None else []
            look = int(stages[-1]["look"]) if stages else None
            attempts[attempt.attempt_id] = _AttemptPoint(
                round=attempt.round,
                sides=tuple(
                    str(mid)
                    for mid in (attempt.measurement_id, attempt.parent_measurement_id)
                    if mid is not None
                ),
                look=look,
                decided=look is not None and (look in attempt.looks or look in attempt.not_started),
                baseline=round_state.baseline_measurement_id,
                barred=latest_look_barred(attempt, snapshot.measurements),
            )
    try:
        policy: CampaignPolicyV11 | None = state.policy
    except SchemaError:
        policy = None  # no creation record yet: the lifecycle refuses the record itself
    return _ThresholdPoint(
        threshold_named=snapshot.threshold_named,
        repairs_reserved=snapshot.repairs_reserved,
        window_start=snapshot.window_start,
        producing_version=snapshot.producing_version,
        frozen=frozenset(
            name for name, entry in snapshot.measurements.items() if entry.get("frozen", False)
        ),
        # #91/#97 spec §5: the lifecycle's own crossing over the windows here.
        crossed=policy is not None and state._threshold_crossed(),
        attempts=attempts,
        completion_versions=snapshot.completion_versions,
        import_versions=snapshot.import_versions,
        retries_declined=frozenset(
            key
            for key in snapshot.retries_declined
            if any(key[0] in attempt.sides for attempt in attempts.values())
        ),
        parallel_attempts=1 if policy is None else int(policy.parallel_attempts),
        independent=policy is None or (policy.attempt_feedback or "none") == "none",
    )


def _replay_thresholds(records: Sequence[Envelope], family_version: int) -> _ThresholdReplay:
    points: dict[int, _ThresholdPoint] = {}
    grants: dict[str, dict[tuple[str, str], list[tuple[int, int]]]] = {}
    state = CampaignLifecycle(selector=None, family_version=family_version)
    for index, record in enumerate(records):
        if record.event in _THRESHOLD_POINT_EVENTS:
            points[index] = _threshold_point(state)
        try:
            state.accept(record)
        except SchemaError:
            break
        # a27 spec §3.1 (#116): any suspension can name an open Attempt's pair.
        if record.event == "campaign_suspended":
            seq = int(record.to_payload()["sequence"])
            for measurement_id, pairs in state.snapshot.threshold_named.items():
                for key, (ordinal, naming) in pairs.items():
                    if naming == seq:
                        grants.setdefault(measurement_id, {}).setdefault(key, []).append(
                            (ordinal, naming)
                        )
    return _ThresholdReplay(
        points=points,
        grants={
            name: {key: tuple(ordinals) for key, ordinals in pairs.items()}
            for name, pairs in grants.items()
        },
        import_versions=dict(state.snapshot.import_versions),
    )


#: A measurement's schedule rows by `(case_id, sample_id)`.
_ScheduleIndex = Mapping[tuple[str, str], Mapping[str, Any]]


@dataclass(frozen=True)
class _Evidence:
    """Everything a precondition re-check may read: durable bytes and nothing else."""

    run_root: Path
    records: tuple[Envelope, ...]
    cache: dict[str, bytes]
    family_version: int = field(kw_only=True)
    #: Per measurement, the attestation verdict its durable identities support.
    _verdicts: dict[str, str | None] = field(default_factory=dict)
    #: Per measurement, its whole durable record prefix.
    _journals: dict[str, tuple[Envelope, ...]] = field(default_factory=dict)
    #: The campaign identity, parsed once and cached.
    _identity_cache: dict[str, CampaignIdentityV1] = field(default_factory=dict)
    #: Family 8: per journal position, the adaptation identity in force there.
    _adaptation_cache: dict[str, AdaptationIdentityV1] = field(default_factory=dict)
    #: Per reference locator, its parsed prefix and S1 lifecycle, read once.
    _states: dict[str, tuple[tuple[Envelope, ...], Lifecycle]] = field(default_factory=dict)
    #: Per measurement, S1's lifecycle over its whole durable prefix.
    _whole_states: dict[str, Lifecycle] = field(default_factory=dict)
    #: #95: per measurement, the repair tries of its whole durable prefix, and
    #: its run identity with the schedule indexed by `(case_id, sample_id)`.
    _repairs: dict[str, frozenset[tuple[str, str, int]]] = field(default_factory=dict)
    _schedules: dict[str, tuple[Mapping[str, Any], _ScheduleIndex]] = field(default_factory=dict)
    #: #95: one forward replay of the campaign records, `(lifecycle, records
    #: accepted)`, that the family-12 layer and drift checks read prefixes from;
    #: and per born measurement, the valid prefix `_born_records` scanned.
    _replay: dict[str, tuple[CampaignLifecycle, int]] = field(default_factory=dict)
    _born: dict[str, tuple[Envelope, ...] | None] = field(default_factory=dict)
    #: Every `measurement_rebound` by the measurement it binds, read once.
    _rebind_cache: dict[str, Mapping[str, tuple[int, Envelope]]] = field(default_factory=dict)
    #: Family 12: the artifact policy the S2 ledger's store identity persists.
    _store_policy: dict[str, ArtifactPolicyV1] = field(default_factory=dict)
    #: Family 7 (#78): the one lifecycle replay the threshold-repair checks read.
    _thresholds: dict[str, _ThresholdReplay] = field(default_factory=dict)
    #: #91 (ruling R9): per measurement events file, `(record index, cited end)`
    #: of every campaign record citing a prefix of it, in order; and per
    #: `(measurement, end)` S1's parse of that prefix, read once.
    _citations: dict[str, Mapping[str, tuple[tuple[int, int], ...]]] = field(default_factory=dict)
    _prefixes_at: dict[tuple[str, int], tuple[tuple[Envelope, ...], Lifecycle]] = field(
        default_factory=dict
    )
    #: G2b3a: every bound family-12 measurement's snapshot entry, reduced once.
    _entries: dict[str, Mapping[str, Mapping[str, Any]]] = field(default_factory=dict)
    #: G2b3b: per `(attempt_id, side)`, the drift reference, derived once.
    _drift_references: dict[tuple[str, str], DriftReference | None] = field(default_factory=dict)
    #: #109 (a28 spec §3.8): what places a dispatch in a window, read once from
    #: the campaign records; and per measurement, its journal's dispatches.
    _windows: dict[str, _RedispatchWindows] = field(default_factory=dict)
    _dispatches: dict[str, _DispatchIndex] = field(default_factory=dict)
    #: Family 13 (design §7 "Audit cost"): each recomputed gate computation --
    #: a look's replicates or a noise calibration's D0 -- keyed by `(purpose,
    #: seed, sha256 of its canonical inputs)`, so a record checked from
    #: several places is drawn once per `verify` (compare #95).
    _draws: dict[tuple[str, int, str], Any] = field(default_factory=dict)
    #: `False` after a `corpus_mismatch`: a `look_evaluated`'s step 0 (the
    #: three-way binding) and completeness are still checked, but its verdict
    #: is not recomputed against a corpus already known to disagree with the
    #: journal.
    recompute: bool = True

    @property
    def adaptive(self) -> bool:
        return True

    @property
    def continuable(self) -> bool:
        """Family 6: sample tries, excluded baseline pairs, per-try accounting."""

        return True

    @property
    def vocabulary(self) -> CampaignVocabulary:
        """Shared final/cohort spellings; older audit paths retain family 13's names."""

        return campaign_vocabulary_for(self.family_version)

    def computed(self, purpose: str, seed: int, inputs: Any, thunk: Callable[[], Any]) -> Any:
        """The cached result of one family-13 computation, or `thunk()` once (F20).

        `inputs` is everything the result depends on besides the seed, as
        canonical JSON; a computation that raises is not cached, so every
        check that reaches it reports the same finding.
        """

        key = (purpose, seed, hashlib.sha256(canonical_json(inputs)).hexdigest())
        if key not in self._draws:
            self._draws[key] = thunk()
        return self._draws[key]

    def threshold_replay(self) -> _ThresholdReplay:
        """#78: the lifecycle replayed once over the campaign records (empty below family 7)."""

        cached = self._thresholds.get("replay")
        if cached is None:
            cached = _replay_thresholds(self.records, self.family_version)
            self._thresholds["replay"] = cached
        return cached

    def version_before(self, before: int) -> str:
        """The producing version in force before index `before`: the creation
        record's or the latest `identity_rebased`'s among the records before it."""

        version = ""
        for record in self.records[:before]:
            if isinstance(record.payload.get("producing_version"), str):
                version = record.payload["producing_version"]
        return version

    def primary_range(self, before: int, measurement_id: str) -> PrimaryRange:
        """#97 spec §4.1, §5: which of `measurement_id`'s scored rows a primary
        outside [0, 1] makes a fault row before index `before`.

        Families 7-12 under a producing version in force there that owes the
        rule (`version_before`); an earlier producer's journal audits as it was
        written. Carried rows also need the version their carry was admitted
        under to owe it (ADR-0019 as amended 2026-10-03): the executor's
        `primary_range_for`.
        """

        return primary_range_for(
            self.family_version,
            self.version_before(before),
            self.threshold_replay().import_versions.get(measurement_id),
        )

    def threshold_point(self, record: Envelope) -> _ThresholdPoint:
        """#78: the lifecycle's threshold-repair state just before `record`."""

        point = self.threshold_replay().points.get(self.records.index(record))
        if point is None:
            raise _Unverifiable
        return point

    def measurement_at(
        self, measurement_id: str, index: int
    ) -> tuple[tuple[Envelope, ...], Lifecycle]:
        """#91 spec §5 (ruling R9): a measurement's prefix as it stood at campaign record `index`.

        The two logs are ordered against each other only where a campaign
        record cites a measurement prefix, and the executor always cites the
        whole prefix as it stands (`prefix_reference`). So the prefix at
        `index` is the one the first citation at or after it names -- the
        record's own, for a `look_not_started` -- or, with none, the whole
        journal. For an Attempt held at `index` that is exact: nothing appends
        a try to its measurements until its look is decided or barred, or a
        `repairs_dispatched` reserves its repairs, and each of those cites
        them first. A stage still in flight at `index` that completes before
        the next citation reads as complete: the bytes order no further.
        """

        name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
        self.measurement_journal(measurement_id)
        raw = self.cache[name]
        end = len(raw)
        for at, cited in self.citations().get(name, ()):
            if at >= index:
                end = cited
                break
        cached = self._prefixes_at.get((measurement_id, end))
        if cached is not None:
            return cached
        if end > len(raw):
            raise _Unverifiable
        snapshot, state = _scan(raw[:end], MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        if snapshot.journal_integrity != "complete" or not snapshot.records:
            raise _Unverifiable
        result = (snapshot.records, cast(Lifecycle, state))
        self._prefixes_at[(measurement_id, end)] = result
        return result

    def citations(self) -> Mapping[str, tuple[tuple[int, int], ...]]:
        """Per cited file, `(record index, end)` of each `journal_prefix` citing it, in order."""

        cached = self._citations.get("all")
        if cached is None:
            found: dict[str, list[tuple[int, int]]] = {}
            for index, record in enumerate(self.records):
                evidence = record.payload.get("evidence")
                if not isinstance(evidence, list):
                    continue
                for reference in evidence:
                    if not isinstance(reference, Mapping):
                        continue
                    match = _LOCATOR.fullmatch(str(reference.get("locator")))
                    if reference.get("kind") == "journal_prefix" and match is not None:
                        found.setdefault(match["file"], []).append((index, int(match["end"])))
            cached = {name: tuple(entries) for name, entries in found.items()}
            self._citations["all"] = cached
        return cached

    def journal_state(self, measurement_id: str) -> Lifecycle:
        """S1's lifecycle over a measurement's whole durable prefix, read once."""

        self.measurement_journal(measurement_id)
        return self._whole_states[measurement_id]

    def repair_ordinals(self, measurement_id: str) -> frozenset[tuple[str, str, int]]:
        """`_repair_ordinals` over a measurement's whole durable prefix, read once (#95)."""

        cached = self._repairs.get(measurement_id)
        if cached is None:
            cached = frozenset(_repair_ordinals(self.measurement_journal(measurement_id)))
            self._repairs[measurement_id] = cached
        return cached

    def run_identity(self, measurement_id: str) -> tuple[Mapping[str, Any], _ScheduleIndex]:
        """A measurement's `run_created` identity and its schedule rows by
        `(case_id, sample_id)`, parsed once (#95). Callers only read them."""

        cached = self._schedules.get(measurement_id)
        if cached is None:
            identity = self.measurement_journal(measurement_id)[0].payload["identity"]
            rows: dict[tuple[str, str], Mapping[str, Any]] = {}
            for row in identity["schedule"]:
                rows.setdefault((row["case_id"], row["sample_id"]), row)
            cached = (identity, rows)
            self._schedules[measurement_id] = cached
        return cached

    def snapshot_before(self, index: int) -> CampaignSnapshot:
        """`reduce` over the records before `index`, from one forward replay (#95).

        The audit checks records in journal order, so the positions it asks
        for move forward and each record is accepted once per audit rather
        than once per look. A position behind the replay starts a new one. A
        record the lifecycle refuses drops the replay and reduces the prefix
        from scratch, so the caller sees exactly what `reduce` raises.
        """

        index = min(index, len(self.records))
        replay = self._replay.get("campaign")
        if replay is None or replay[1] > index:
            replay = (CampaignLifecycle(selector=None, family_version=self.family_version), 0)
        state, accepted = replay
        try:
            while accepted < index:
                state.accept(self.records[accepted])
                accepted += 1
        except Exception:
            self._replay.pop("campaign", None)
            return reduce(self.records[:index], selector=None, family_version=self.family_version)
        self._replay["campaign"] = (state, accepted)
        return state.snapshot

    def refusal_at(self, index: int) -> str | None:
        """What the lifecycle replayed to just before `index` says of the record there.

        #109 (a28 spec §3.8): the audit re-proves a record's admission with the
        lifecycle's own rules rather than a copy of them, on the forward replay
        `snapshot_before` keeps (#95). `prepare` changes nothing; a record it
        admits is `None`, one it refuses its rule. A replay that a record
        before `index` already ended cannot say.
        """

        self.snapshot_before(index)
        replay = self._replay.get("campaign")
        if replay is None or replay[1] != index or index >= len(self.records):
            raise _Unverifiable
        try:
            replay[0].prepare(self.records[index])
        except SchemaError as refusal:
            return str(refusal)
        return None

    def redispatch_windows(self) -> _RedispatchWindows:
        """#109: the campaign records' windows and dispatch evidence, read once."""

        cached = self._windows.get("all")
        if cached is None:
            cached = _redispatch_windows(self.records)
            self._windows["all"] = cached
        return cached

    def dispatch_index(self, measurement_id: str) -> _DispatchIndex:
        """#109: a measurement journal's dispatches, indexed once (#95's rule)."""

        cached = self._dispatches.get(measurement_id)
        if cached is None:
            cached = _dispatch_index(self.measurement_journal(measurement_id))
            self._dispatches[measurement_id] = cached
        return cached

    def identity(self) -> CampaignIdentityV1:
        """The campaign identity `campaign_created` carries, parsed once and cached."""

        cached = self._identity_cache.get("identity")
        if cached is not None:
            return cached
        identity = CampaignIdentityV1.from_payload(self.records[0].payload["identity"])
        self._identity_cache["identity"] = identity
        return identity

    def adaptation_identity(self, before: int | None = None) -> AdaptationIdentityV1:
        """Family 8: the full adaptation identity in force before record `before`.

        `campaign_created` and every `identity_rebased` carry one beside their
        slot, so the identity in force at a position is the latest such record
        before it -- an accepted `rebase` or an in-place change (ADR-0020) can
        move the reflection metadata, and with it `family_membership`. With
        `before` omitted it is the latest in the journal. The schema already
        proved each payload's digest and profile equal its slot's; an earlier
        family journals only the slot and has none to read.
        """

        position = adaptation_position(self.records, before)
        key = f"adaptation:{position}"
        cached = self._adaptation_cache.get(key)
        if cached is None:
            cached = AdaptationIdentityV1.from_payload(
                self.records[position].payload["adaptation_payload"]
            )
            self._adaptation_cache[key] = cached
        return cached

    def store_policy(self) -> ArtifactPolicyV1:
        """The artifact policy of the run's own store, read once (family 12).

        The policy in the store identity of the latest store record
        (`ArtifactLedger.latest_store_record`'s choice) -- the policy
        `ArtifactStore.open` proved equal to the campaign's own before any copy
        was published, and which a `store_rebased` cannot move. Read from the
        ledger bytes this audit already holds (`ledger`), never re-read from
        disk: a ledger changed under the audit must not move its answer.
        """

        cached = self._store_policy.get("policy")
        if cached is not None:
            return cached
        records = self.ledger()
        if not records:
            raise _PreconditionFailed("the artifact ledger holds no store identity")
        latest = records[0]
        for record in records:
            if record.event == "store_rebased":
                latest = record
        policy = ArtifactStoreIdentityV1.from_payload(latest.payload["identity"]).policy
        self._store_policy["policy"] = policy
        return policy

    def latest_identity(self) -> CampaignIdentityV1:
        """The identity of the latest condition epoch: the creation one until a
        comparable in-place change opens another (ADR-0020)."""

        latest = self.records[0].payload["identity"]
        epochs = 0
        for record in self.records:
            if record.event == "identity_rebased" and any(
                comparable_field(str(name), family_version=self.family_version)
                for name in record.payload["changed_fields"]
            ):
                latest, epochs = record.payload["identity"], epochs + 1
        if not epochs:
            return self.identity()
        return CampaignIdentityV1.from_payload(dict(latest))

    def identity_for(self, measurement_id: str) -> CampaignIdentityV1:
        """The identity of the condition epoch `measurement_id` was bound in (ADR-0020).

        Rebuilt from the records alone, as the lifecycle builds it: the
        creation record opens epoch `0` and each `identity_rebased` whose
        `changed_fields` name a comparable field opens the next one. The
        measurement belongs to the epoch in force at the position of the
        record that created it: its `measurement_bound` (a family-12 fresh
        Parent's `parent_measurement_bound`), or the `measurement_rebound` of
        a family-7 `continue` (I2 spec §5.2).
        """

        if self.family_version == 15:
            from evolution.tree.campaign_epochs import epoch_identity

            return epoch_identity(self.snapshot_before(len(self.records)), measurement_id)
        epochs: list[Mapping[str, Any]] = []
        for record in self.records:
            if record.event == "campaign_created":
                epochs = [record.payload["identity"]]
            elif record.event == "identity_rebased" and any(
                comparable_field(str(name), family_version=self.family_version)
                for name in record.payload["changed_fields"]
            ):
                epochs.append(record.payload["identity"])
            elif (
                record.event in _MEASUREMENT_CREATIONS
                and record.payload["measurement_id"] == measurement_id
            ):
                if not epochs:
                    break
                key = f"epoch:{len(epochs) - 1}"
                cached = self._identity_cache.get(key)
                if cached is None:
                    cached = CampaignIdentityV1.from_payload(dict(epochs[-1]))
                    self._identity_cache[key] = cached
                return cached
        raise _PreconditionFailed("the look's measurement is not bound in this campaign")

    def measurement_state(
        self, reference: Mapping[str, Any]
    ) -> tuple[tuple[Envelope, ...], Lifecycle]:
        """The cited prefix and S1's own lifecycle over it -- `executions`/`scoring` per sample.

        Cached on the reference's own locator, the way `measurement_journal`
        caches on the measurement id: `_check_look_evaluated` reads each side's
        prefix once for the binding and once more per sample row, and a
        `look_not_started`/`look_evaluated` pair over the same evidence would
        otherwise re-parse it again.
        """

        key = str(reference["locator"])
        cached = self._states.get(key)
        if cached is not None:
            return cached
        snapshot, state = _scan(self.raw(reference), MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        if snapshot.journal_integrity != "complete" or not snapshot.records:
            raise _PreconditionFailed("the referenced measurement prefix is not a valid journal")
        result = (snapshot.records, cast(Lifecycle, state))
        self._states[key] = result
        return result

    def raw(self, reference: Mapping[str, Any]) -> bytes:
        """The referenced byte range. Its integrity is already proven."""

        match = _LOCATOR.fullmatch(str(reference["locator"]))
        if match is None:  # pragma: no cover - the reference check ran first
            raise _Unverifiable
        raw = self.cache.get(match["file"])
        if raw is None:  # pragma: no cover - the reference check filled the cache
            raise _Unverifiable
        return raw[int(match["start"]) : int(match["end"])]

    def range_of(self, reference: Mapping[str, Any]) -> tuple[int, int]:
        match = _LOCATOR.fullmatch(str(reference["locator"]))
        if match is None:  # pragma: no cover - the reference check ran first
            raise _Unverifiable
        return int(match["start"]), int(match["end"])

    def file_of(self, reference: Mapping[str, Any]) -> str:
        match = _LOCATOR.fullmatch(str(reference["locator"]))
        if match is None:  # pragma: no cover - the reference check ran first
            raise _Unverifiable
        return str(match["file"])

    def measurement_records(self, reference: Mapping[str, Any]) -> tuple[Envelope, ...]:
        """The referenced measurement prefix, reduced with S1's own lifecycle.

        The range is parsed in memory: the run root is never written to, not even
        with a temporary file, because `verify_evidence` reads any run root —
        including one it has no authority over. `measurement_state` does the
        actual parse and caches it; this is only its records half.
        """

        return self.measurement_state(reference)[0]

    def read_file(self, path: Path) -> bytes:
        """Family15 shared passes use the same owned no-follow evidence reader."""
        if self.family_version == 15:
            from evolution.tree.family15_audit import EvidenceMismatch, _read_owned

            try:
                return _read_owned(path)
            except (OSError, JournalFault, EvidenceMismatch):
                raise _PreconditionFailed("evidence is missing, unsafe or corrupt") from None
        return path.read_bytes()

    def measurement_journal(self, measurement_id: str) -> tuple[Envelope, ...]:
        """The measurement's whole durable prefix, read once and cached.

        A sample's *final* disposition is a fact about the journal, not about one
        record in it, so the checks that need it read the file the reference
        points into rather than the cited range alone.
        """

        if measurement_id in self._journals:
            return self._journals[measurement_id]
        name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
        raw = self.cache.get(name)
        if raw is None:
            path = self.run_root / name
            if not path.is_file():
                raise _PreconditionFailed("the measurement journal is missing")
            raw = self.read_file(path)
            self.cache[name] = raw
        snapshot, state = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
        if snapshot.journal_integrity != "complete" or not snapshot.records:
            raise _PreconditionFailed("the measurement journal is not a valid journal")
        self._journals[measurement_id] = snapshot.records
        self._whole_states[measurement_id] = cast(Lifecycle, state)
        return snapshot.records

    def one_record(self, reference: Mapping[str, Any], family: Any) -> Envelope:
        try:
            return Envelope.from_bytes(self.raw(reference), family=family)
        except (ValueError, TypeError):
            raise _PreconditionFailed("the referenced record is not a record of its log") from None

    def ledger(self) -> tuple[Envelope, ...]:
        raw = self.cache.get(LEDGER_FILE)
        if raw is None:
            raise _Unverifiable
        snapshot, _state = _scan(raw, MAX_LEDGER_RECORD_BYTES, ArtifactLifecycle, ARTIFACT_FAMILY)
        return snapshot.records

    def measurement_of(self, attempt_id: str, before: Envelope | None = None) -> str | None:
        """The measurement bound for an attempt, from the durable binding alone.

        I2 spec §5.2: a `measurement_rebound` replaces the binding for everything
        downstream, so with `before` the id is the one in force when that
        record was written (the re-bind chain followed up to it).
        """

        for record in self.records:
            if record.event == "measurement_bound" and record.payload["attempt_id"] == attempt_id:
                return self.current(str(record.payload["measurement_id"]), before)
        return None

    def rebinds(self) -> Mapping[str, tuple[int, Envelope]]:
        """Every `measurement_rebound` and its position, keyed by the measurement it binds.

        I2 spec §5.2; read once.
        """

        cached = self._rebind_cache.get("all")
        if cached is None:
            cached = {
                str(record.payload["measurement_id"]): (index, record)
                for index, record in enumerate(self.records)
                if record.event == "measurement_rebound"
            }
            self._rebind_cache["all"] = cached
        return cached

    def current(self, measurement_id: str, before: Envelope | None = None) -> str:
        """`measurement_id`, followed along the re-bind chain written before `before`.

        Without `before`, the whole journal. A measurement is re-bound at most
        once per epoch, and only from the latest frozen one, so the chain is a
        line.
        """

        limit = len(self.records) if before is None else self.records.index(before)
        current = measurement_id
        moved = True
        while moved:
            moved = False
            for name, (index, record) in self.rebinds().items():
                if str(record.payload["from_measurement_id"]) == current and index < limit:
                    current, moved = name, True
                    break
        return current

    def chain(self, measurement_id: str) -> tuple[str, ...]:
        """`measurement_id` and every frozen measurement it was re-bound from, newest first."""

        chain = [measurement_id]
        while chain[-1] in self.rebinds():
            chain.append(str(self.rebinds()[chain[-1]][1].payload["from_measurement_id"]))
        return tuple(chain)

    def expected_attestation(self, measurement_id: str) -> ExpectedAttestation:
        """What the target had to attest for this measurement, from durable bytes.

        Never from a live store: the two digests come from the campaign journal's
        own records — `attempt_sealed` for a Candidate, and for the champion side
        the campaign identity's declared seed pair or, once a round promoted, the
        S2 `promotion_committed` record the winning `round_closed` cites. A
        family-12 fresh Parent measures its Attempt's Parent, a champion tree,
        so it is held to the champion side's pair (G2 spec §4.6), as is a
        family-12 completion, which measures the promoted champion (G2b4).
        """

        if self.family_version == 15:
            snapshot = self.snapshot_before(len(self.records))
            entry = snapshot.measurements[measurement_id]
            digest = str(entry["candidate_digest"])
            attempt_id = entry["attempt_id"]
            if attempt_id != CHAMPION_SIDE and entry.get("role") != "parent":
                attempt = snapshot.rounds[entry["round"]].attempts[attempt_id]
                if attempt.candidate_digest != digest or attempt.dependency_digest is None:
                    raise _PreconditionFailed("final measurement changes its sealed Candidate")
                return ExpectedAttestation(digest, attempt.dependency_digest)
            store = snapshot.identity["artifact_store"]
            if digest == store["seed_candidate_digest"]:
                return ExpectedAttestation(digest, str(store["seed_dependency_digest"]))
            for record in self.records:
                if (
                    record.event == "round_closed"
                    and record.payload["outcome"] == "promoted"
                    and record.payload["champion_digest"] == digest
                ):
                    evidence = record.payload["evidence"]
                    if not evidence:
                        raise _PreconditionFailed("the promotion cites no commit")
                    committed = self.one_record(evidence[0], ARTIFACT_FAMILY)
                    if committed.event != "promotion_committed":
                        raise _PreconditionFailed("the promotion cites no commit")
                    return ExpectedAttestation(digest, str(committed.payload["dependency_digest"]))
            raise _Unverifiable
        bound = None
        # A re-bound measurement measures its origin's Candidate (I2 spec §5.2).
        origin = self.chain(measurement_id)[-1]
        for record in self.records:
            if (
                record.event
                in (
                    "measurement_bound",
                    "parent_measurement_bound",
                    self.vocabulary.measurement_bound,
                )
                and record.payload["measurement_id"] == origin
            ):
                bound = record
        if bound is None:
            raise _PreconditionFailed("the measurement is not bound")
        digest = str(bound.payload["candidate_digest"])
        # Family-13 design §6.8: the cohort's Champion member names no Attempt
        # and is held to the champion side's pair; a finalist member to its
        # Attempt's `attempt_sealed`, as the Attempt's own measurement is.
        champion_member = (
            bound.event == self.vocabulary.measurement_bound and bound.payload["attempt_id"] is None
        )
        attempt_id = CHAMPION_SIDE if champion_member else str(bound.payload["attempt_id"])
        if attempt_id != CHAMPION_SIDE and bound.event in (
            "measurement_bound",
            self.vocabulary.measurement_bound,
        ):
            for record in self.records:
                if record.event == "attempt_sealed" and record.payload["attempt_id"] == attempt_id:
                    if record.payload["candidate_digest"] != digest:
                        raise _PreconditionFailed("the measurement measures another Candidate")
                    return ExpectedAttestation(digest, str(record.payload["dependency_digest"]))
            raise _PreconditionFailed("the measured attempt was never sealed")
        store = self.records[0].payload["identity"]["artifact_store"]
        if digest == store["seed_candidate_digest"]:
            return ExpectedAttestation(digest, str(store["seed_dependency_digest"]))
        for record in self.records:
            if (
                record.event == "round_closed"
                and record.payload["outcome"] == "promoted"
                and record.payload["champion_digest"] == digest
            ):
                evidence = record.payload["evidence"]
                if not evidence:
                    raise _PreconditionFailed("the promotion cites no commit")
                committed = self.one_record(evidence[0], ARTIFACT_FAMILY)
                if committed.event != "promotion_committed":
                    raise _PreconditionFailed("the promotion cites no commit")
                return ExpectedAttestation(digest, str(committed.payload["dependency_digest"]))
        # A champion the campaign identity does not declare and no promotion
        # committed: nothing durable says what it had to attest, so nothing is
        # claimed about it. The audit never guesses an expectation it would then
        # judge a truthful journal against.
        raise _Unverifiable

    def attested_rows(self, measurement_id: str) -> tuple[Envelope, ...]:
        """The `instance_attested` rows behind a measurement's rows.

        I2 spec §5.3 (the executor's `attested_chain`): a measurement re-bound
        under `continue` holds rows the instances of the frozen measurements it
        was re-bound from served, each loading the same Candidate, so promotion
        admission reads them all. Without a re-bind it is the measurement's own.
        """

        chain = set(self.chain(measurement_id))
        return tuple(
            record
            for record in self.records
            if record.event == "instance_attested" and record.payload["measurement_id"] in chain
        )

    def attestation_verdict(self, measurement_id: str) -> str | None:
        """Re-run R3's verifier over every durable identity of a measurement.

        `None` when each attested instance admits the sealed Candidate — the
        verdict `admitted()` reaches over the same rows — and otherwise why the
        verifier refused one. The result is cached: a measurement is read once
        however many records ask about it.
        """

        if measurement_id in self._verdicts:
            return self._verdicts[measurement_id]
        verdict = self.verdict_over(measurement_id, self.attested_rows(measurement_id))
        self._verdicts[measurement_id] = verdict
        return verdict

    def verdict_over(self, measurement_id: str, rows: Sequence[Envelope]) -> str | None:
        """`attestation_verdict` over `rows` alone, some of the measurement's, uncached."""

        expected = self.expected_attestation(measurement_id)
        for row in rows:
            try:
                identity = TargetIdentity.from_payload(row.payload["identity"])
            except ValueError:
                return "an attested identity cannot be read"
            try:
                verify_attestation(identity, expected)
            except AttestationError as refusal:
                return f"an attested identity is refused: {refusal.code}"
        return None

    def policy(self) -> CampaignPolicyV11:
        try:
            raw_policy = self.records[0].payload["identity"]["policy"]
            return policy_class_for_payload(raw_policy).from_payload(raw_policy)
        except (KeyError, TypeError, PolicyError):  # pragma: no cover - a created log has one
            raise _Unverifiable from None

    def policy_for(self, measurement_id: str) -> CampaignPolicyV11:
        """The policy of the condition epoch `measurement_id` was bound in (ADR-0020).

        A `rebaseline` may change the policy from the next round (I2 spec §6.2),
        so a measurement is judged under the policy of its own epoch. Without a
        comparable change that is the creation record's, as `policy()`.
        """

        try:
            raw_policy = self.identity_for(measurement_id).to_payload()["policy"]
            return policy_class_for_payload(raw_policy).from_payload(raw_policy)
        except (KeyError, TypeError, PolicyError):  # pragma: no cover - a bound one has one
            raise _Unverifiable from None


class _PreconditionFailed(Exception):
    """The referenced evidence does not satisfy the E-precondition it was cited for.

    `code` is the mismatch code the finding is reported under: every record's
    `precondition_failed`, with two distinct codes -- `pairs_excluded`, whose
    summary the audit recomputes and reports as `pairs_excluded_mismatch`
    (continuable-terminals spec §4.2), and a cited measurement whose
    `sample_imported` records disagree with the creation record's declared
    carried baseline, reported as `baseline_import_mismatch` (baseline
    inheritance §3.3). The family-7 audit of in-place changes reports a
    re-bound measurement whose carried rows are not its frozen source's under
    that same code (I2 spec §5.3: one ADR-0017 carry, one code).
    """

    def __init__(self, detail: str, code: str = "precondition_failed") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


# -- family 6: tries and excluded pairs over a measurement prefix ------------


def _owner_reason(row: SampleRow) -> tuple[str, str] | None:
    """The accounted owner and reason of a try that left its sample unscored.

    `CampaignMeasurement._owner_reason`: an `unscored` try is what resume
    classifies `indeterminate(rollout, rollout_interrupted)`, and an
    indeterminate one carries its registry disposition.
    """

    if row.disposition == "unscored":
        return ("rollout", "rollout_interrupted")
    if row.disposition != "indeterminate":
        return None
    disposition = disposition_for(cast(SampleOutcome, row.outcome))
    return (str(disposition.owner), str(disposition.reason))


def _excluded_summary(records: Sequence[Envelope], state: Lifecycle) -> dict[str, Any]:
    """The `pairs_excluded` summary a settled prefix supports (spec §4.2).

    The executor's `write_pairs_excluded` over `exhausted_rows()`: every
    exhausted pair in schedule order, each with its latest try's owner and
    reason and its try count, `count`/`by_owner`/`pairs_digest` over the whole
    set and at most the first `MAX_EXCLUDED_PAIRS` listed.
    """

    rows = latest_rows(records, state)
    exhausted = [row for row in rows if row.exhausted]
    pairs = []
    by_owner: dict[str, int] = {}
    for row in exhausted:
        owner, reason = _owner_reason(row) or ("", "")
        by_owner[owner] = by_owner.get(owner, 0) + 1
        pairs.append(
            dict(
                case_id=row.case_id,
                sample_id=row.sample_id,
                owner=owner,
                reason=reason,
                tries=row.retry_ordinal + 1,
            )
        )
    return dict(
        settled=all(row.disposition == "score" or row.exhausted for row in rows),
        count=len(pairs),
        by_owner=by_owner,
        pairs_digest=pairs_digest([(row.case_id, row.sample_id) for row in exhausted]),
        pairs=pairs[:MAX_EXCLUDED_PAIRS],
        truncated=len(pairs) > MAX_EXCLUDED_PAIRS,
    )


def _recorded_exclusion(context: _Evidence, record: Envelope, measurement_id: str) -> bool:
    """Whether a `pairs_excluded` for the measurement precedes `record`."""

    for earlier in context.records[: context.records.index(record)]:
        if (
            earlier.event == "pairs_excluded"
            and earlier.payload["measurement_id"] == measurement_id
        ):
            return True
    return False


def _check_pairs_excluded(context: _Evidence, record: Envelope) -> None:
    """`pairs_excluded`: the summary is the one its measurement prefix supports (§4.2).

    The authoritative set is recomputed from the cited prefix with the shared
    row reduction; `count`, `pairs_digest`, `by_owner`, the listed pairs with
    their tries, and `truncated` must all be what it gives, and the prefix must
    be settled -- every sample scored or out of tries -- because the executor
    writes the record only then. Every finding is `pairs_excluded_mismatch`.
    """

    code = "pairs_excluded_mismatch"
    payload = record.payload
    reference = _only_reference(record)
    try:
        _measurement_prefix(context, reference, str(payload["measurement_id"]))
    except _PreconditionFailed as failed:
        raise _PreconditionFailed(failed.detail, code) from None
    records, state = context.measurement_state(reference)
    measurement_id = str(payload["measurement_id"])
    _refuse_out_of_range(
        context,
        record,
        [(measurement_id, latest_rows(records, state))],
        context.policy_for(measurement_id).metric_primary,
    )
    expected = _excluded_summary(records, state)
    if not expected["settled"]:
        raise _PreconditionFailed("the cited prefix is not settled", code)
    for name in ("count", "pairs_digest", "by_owner", "pairs", "truncated"):
        if payload[name] != expected[name]:
            raise _PreconditionFailed(f"{name} is not what the measurement prefix supports", code)
    _refuse_owed_threshold_repair(context, record, str(payload["measurement_id"]), records)


def _latest_ordinals(records: Sequence[Envelope]) -> dict[tuple[str, str], int]:
    """Each pair's latest started try ordinal in a measurement prefix (`owed_repairs_in`)."""

    latest: dict[tuple[str, str], int] = {}
    for entry in records:
        if entry.event == "rollout_started":
            key = (str(entry.payload["case_id"]), str(entry.payload["sample_id"]))
            latest[key] = int(entry.payload.get("retry_ordinal", 0))
    return latest


def _refuse_owed_threshold_repair(
    context: _Evidence, record: Envelope, measurement_id: str, records: Sequence[Envelope]
) -> None:
    """#78 (spec §3.7): no exclusion or completion while a threshold repair is owed.

    Under the producing version in force before `record` (the lifecycle's,
    the creation record's or the latest rebase's), from the version that
    ships the fix on: the pairs the lifecycle names for this measurement whose
    repair has not started in the cited prefix (spec §3.2's condition 5). A
    frozen measurement owes nothing -- its pairs run on its successor -- and
    an earlier version owed no repair, so a journal it wrote audits unchanged.
    """

    point = context.threshold_point(record)
    if measurement_id in point.frozen or not threshold_repair_required(point.producing_version):
        return
    owed = owed_threshold_rows(point.named(measurement_id), _latest_ordinals(records).get)
    if owed:
        raise _PreconditionFailed(f"{len(owed)} threshold repair(s) owed", "threshold_repair_owed")


# -- #91/#97 spec §5: held Attempts and primary ranges ----------------------


def _refuse_out_of_range(
    context: _Evidence,
    record: Envelope,
    sides: Sequence[tuple[str, Sequence[SampleRow]]],
    primary: str,
) -> None:
    """#97 spec §5: no decision record rests on a scored primary outside [0, 1].

    `sides` are the rows the record rests on, by measurement. Families 7-12
    under the producing version in force before `record` that owes the rule;
    an earlier producer's journal audits as it was written, and a carried row
    as its carry was admitted (`_Evidence.primary_range`).
    """

    index = context.records.index(record)
    for measurement_id, rows in sides:
        primary_range = context.primary_range(index, measurement_id)
        for row in rows:
            if row_fault(row, primary, primary_range=primary_range) == PRIMARY_OUT_OF_RANGE:
                raise _PreconditionFailed(
                    f"{row.case_id}/{row.sample_id} holds a primary outside [0, 1]",
                    "primary_out_of_range",
                )


def _held_threshold(context: _Evidence, index: int, attempt_id: str) -> str | None:
    """#91 spec §5: why an Attempt may not be barred or closed `incomplete_sample` here.

    The lifecycle's own two conditions, re-proved over the bytes: a threshold
    crossed over the windows just before record `index`, or a threshold
    repair owed on one of the Attempt's measurements -- named by a threshold
    suspension and not started in its prefix at `index` (the lifecycle sees
    only the reservation). `None` when neither holds, under an exempt
    producing version, or when the replay never reached the record.
    """

    point = context.threshold_replay().points.get(index)
    if point is None or not attempt_suspension_required(point.producing_version):
        return None
    if point.crossed:
        return "a threshold is crossed"
    attempt = point.attempts.get(attempt_id)
    if attempt is None:
        return None
    owed = 0
    for measurement_id in attempt.sides:
        named = point.named(measurement_id)
        if not named or measurement_id in point.frozen:
            continue
        records, _state = context.measurement_at(measurement_id, index)
        owed += len(owed_threshold_rows(named, _latest_ordinals(records).get))
    return f"{owed} threshold repair(s) owed" if owed else None


def _scheduled_max_repeat(records: Sequence[Envelope]) -> int:
    """`Measurement.max_repeat` over a prefix: the largest repeat its identity schedules."""

    schedule = records[0].payload["identity"]["schedule"] if records else ()
    return max((int(row["repeat"]) for row in schedule), default=0)


def _last_stage_horizon(
    context: _Evidence, measurement_id: str, look: int, schedule: Sequence[Mapping[str, Any]]
) -> int:
    """Family 13 (design §5): the largest repeat a measurement's stage at `look`
    holds -- on its last look a `case_repeats` measurement's last scheduled
    repeat, else the look. Families 3-12 keep the look, byte for byte (ADR-0008).
    """

    return stage_horizon(
        context.policy_for(measurement_id).looks,
        look,
        max((int(row["repeat"]) for row in schedule), default=0),
    )


def _held_at(
    context: _Evidence, index: int, attempt: _AttemptPoint, point: _ThresholdPoint
) -> bool:
    """#91 spec §3.1, ruling R13: is the Attempt held by its measurements at record `index`?

    The executor's one definition (`rows_held`, which `lacking_attempts`
    reads) over each side's prefix at `index` (`measurement_at`), with the
    primary range in force there; its latest look must be undecided. #120:
    under a producing version that owes held repair there, the widened rule
    (`lacking`, with the declined retries settled), as `campaign_view`'s
    `_sides_held` applies it.
    """

    if attempt.look is None or attempt.decided or not attempt.sides:
        return False
    look = attempt.look
    policy = context.policy_for(attempt.sides[0])
    prefixes = [context.measurement_at(measurement_id, index) for measurement_id in attempt.sides]
    # Design §5 (`campaign_view._sides_held`): the stage is read to its
    # horizon, on the last look a `case_repeats` Parent's last scheduled
    # repeat. Without `case_repeats` that is the look, byte for byte.
    horizon = stage_horizon(
        policy.looks, look, max(_scheduled_max_repeat(records) for records, _state in prefixes)
    )
    sides = [
        (
            latest_rows(records, state, look=horizon),
            context.primary_range(index, measurement_id),
        )
        for measurement_id, (records, state) in zip(attempt.sides, prefixes, strict=True)
    ]
    lacking = held_repair_required(point.producing_version)
    return rows_held(
        sides,
        horizon,
        stage_floor(policy.looks, look),
        policy.metric_primary,
        lacking=lacking,
        declined=(
            [declined_tries(point.retries_declined, mid) for mid in attempt.sides]
            if lacking
            else ()
        ),
    )


def _parked_scored_at(
    context: _Evidence, index: int, attempt: _AttemptPoint, point: _ThresholdPoint
) -> bool:
    """Family 15 defers complete scored sides while another Attempt crosses."""
    from evolution.tree.case_comparison import admissible

    if context.family_version != 15 or not point.crossed or not attempt.sides:
        return False
    if attempt.decided:
        return False
    for mid in attempt.sides:
        records, state = context.measurement_at(mid, index)
        if not records or state.pending:
            return False
        schedule = records[0].payload["identity"]["schedule"]
        rows = latest_rows(records, state, look=_scheduled_max_repeat(records))
        if {(r.case_id, r.sample_id) for r in rows} != {
            (slot["case_id"], slot["sample_id"]) for slot in schedule
        }:
            return False
        if not all(admissible(row, context.policy_for(mid).metric_primary) for row in rows):
            return False
        if owed_threshold_rows(point.named(mid), _latest_ordinals(records).get):
            return False
    return True


def _historical_closure(
    context: _Evidence,
    index: int,
    record: Envelope,
    point: _ThresholdPoint,
    attempt_id: str,
    attempt: _AttemptPoint,
) -> bool:
    """#97 spec §4.4, ADR-0019 (amended 2026-10-03): a row the Attempt's look
    gate reads holds an out-of-range primary written before the fix.

    The executor's `historical_out_of_range`: the same gate inputs
    (`gate_input_rows`, ADR-0024) over each side's prefix at the closure, and
    the same `pre_fix_out_of_range` over the completion and import versions
    in force there. A look whose inputs cannot be rebuilt proves nothing.
    """

    if attempt.baseline is None or attempt.look is None or not attempt.sides:
        return False
    look = attempt.look
    try:
        policy = context.policy_for(attempt.sides[0])

        def rows_of(measurement_id: str) -> tuple[SampleRow, ...]:
            return latest_rows(*context.measurement_at(measurement_id, index), look=look)

        baseline = rows_of(attempt.baseline)
        parent_id = attempt.sides[1] if len(attempt.sides) > 1 else None
        sides = LookSides(
            baseline_id=attempt.baseline,
            baseline=baseline,
            candidate_id=attempt.sides[0],
            candidate=rows_of(attempt.sides[0]),
            parent_id=parent_id,
            parent=() if parent_id is None else rows_of(parent_id),
            # The executor's `excluded_pairs`: the baseline's exhausted pairs.
            excluded=frozenset((row.case_id, row.repeat) for row in baseline if row.exhausted),
        )
        layers = None
        layers = _affected_layers_at(
            context,
            record,
            attempt_id,
            context.identity_for(attempt.sides[0]).corpus_cases,
            global_layer=bool(policy.global_layer),
        )
    except (_PreconditionFailed, _Unverifiable):
        return False
    return any(
        pre_fix_out_of_range(
            rows,
            policy.metric_primary,
            completed_before_fix=written_before_fix(point.completion_versions.get(measurement_id)),
            imported_before_fix=written_before_fix(point.import_versions.get(measurement_id)),
        )
        for measurement_id, rows in gate_input_rows(layers, sides, look)
    )


def _attempt_record_finding(
    context: _Evidence, index: int, record: Envelope, point: _ThresholdPoint
) -> tuple[str, str] | None:
    """One `attempt_closed`, `attempt_opened` or threshold suspension's #91/#97 rule."""

    payload = record.payload
    if record.event == "attempt_closed":
        attempt_id = str(payload["attempt_id"])
        if (payload["outcome"], payload["reason"]) == (
            "insufficient_evidence",
            "incomplete_sample",
        ):
            barred = point.attempts.get(attempt_id)
            if barred is not None and barred.barred:
                # The lifecycle's rule: the bar was checked when it was written.
                return None
            detail = _held_threshold(context, index, attempt_id)
            return None if detail is None else ("attempt_held_threshold", detail)
        if (payload["outcome"], payload["reason"]) == (
            "insufficient_evidence",
            "infrastructure_failed",
        ):
            attempt = point.attempts.get(attempt_id)
            if attempt is None or not _historical_closure(
                context, index, record, point, attempt_id, attempt
            ):
                return (
                    "infrastructure_closure_unproven",
                    "the look's gate inputs hold no out-of-range primary written before the fix",
                )
        return None
    if record.event == "attempt_opened":
        # Ruling R9: in independent mode a held Attempt frees its slot, and the
        # lifecycle cannot tell held from mid-stage; the measurements can.
        if not point.independent:
            return None
        active = [
            attempt_id
            for attempt_id, attempt in point.attempts.items()
            if attempt.round == int(payload["round"])
            and not _held_at(context, index, attempt, point)
            and not _parked_scored_at(context, index, attempt, point)
        ]
        if len(active) >= point.parallel_attempts:
            return (
                "attempt_held_unproven",
                f"{len(active)} Attempt(s) not held by their measurements fill "
                f"{point.parallel_attempts} slot(s): " + ", ".join(sorted(active)),
            )
        return None
    if payload["evidence"]["evidence_kind"] != "threshold":
        return None
    # ADR-0008, ruling R9: a threshold is decided only once every opened stage
    # has completed, so each Attempt still open at it is held.
    unheld = sorted(
        attempt_id
        for attempt_id, attempt in point.attempts.items()
        if not _held_at(context, index, attempt, point)
        and not _parked_scored_at(context, index, attempt, point)
    )
    if unheld:
        return (
            "attempt_held_unproven",
            "open at the threshold and not held by their measurements: " + ", ".join(unheld),
        )
    return None


def _attempt_closure_findings(context: _Evidence) -> list[dict[str, Any]]:
    """#91/#97 spec §5: the Attempt records the executor admits, re-proved.

    Families 7-12, each under the producing version in force at its record
    that is not exempt (an earlier producer's journal audits as written):

    - `attempt_closed {insufficient_evidence, incomplete_sample}` while a
      threshold is crossed or a threshold repair is owed on the Attempt
      (`attempt_held_threshold`);
    - `attempt_closed {insufficient_evidence, infrastructure_failed}` outside
      §4.4's historical case (`infrastructure_closure_unproven`);
    - ruling R9: an `attempt_opened` in independent mode that fills more than
      `parallel_attempts` slots counting the Attempts not held by their
      measurements, and a threshold suspension with an open Attempt not held
      (a stage still in flight) -- both `attempt_held_unproven`.

    A record whose measurements cannot be read is left to the records citing them.
    """

    if not context.records:
        return []
    points = context.threshold_replay().points
    findings: list[dict[str, Any]] = []
    for index, record in enumerate(context.records):
        if record.event not in ("attempt_closed", "attempt_opened", "campaign_suspended"):
            continue
        point = points.get(index)
        if point is None or not attempt_suspension_required(point.producing_version):
            continue
        try:
            found = _attempt_record_finding(context, index, record, point)
        except (_PreconditionFailed, _Unverifiable):
            continue
        if found is not None:
            findings.append(_campaign_finding(context, index, *found))
    return findings


def _check_repairs_dispatched(context: _Evidence, record: Envelope) -> None:
    """`repairs_dispatched`: `count` is the threshold repairs its prefix owes (#78, §3.5).

    The lifecycle admits any `count` up to the pairs it names; the cited
    prefix says which of them have not started their repair yet, and that is
    the number of slots the dispatch reserves. Every finding is
    `repairs_dispatched_mismatch`.
    """

    code = "repairs_dispatched_mismatch"
    try:
        _, owed = _dispatched_owed(context, record)
    except _PreconditionFailed as failed:
        raise _PreconditionFailed(failed.detail, code) from None
    if int(record.payload["count"]) != len(owed):
        raise _PreconditionFailed("count is not the threshold repairs the prefix owes", code)


def _dispatched_owed(
    context: _Evidence, record: Envelope
) -> tuple[int, frozenset[tuple[str, str]]]:
    """A `repairs_dispatched`'s cited prefix length and the pairs it owes there (#78, §3.5).

    The pairs the replayed lifecycle names just before the record, less those
    whose repair the cited measurement prefix already started: the slots the
    record reserves, and the only repairs it can cover.
    """

    measurement_id = str(record.payload["measurement_id"])
    records = _measurement_prefix(context, _only_reference(record), measurement_id)
    named = context.threshold_point(record).named(measurement_id)
    return len(records), owed_threshold_rows(named, _latest_ordinals(records).get)


# -- #109 (a28 spec §3.8): a stage carried across a suspension --------------


#: A measurement journal's file, read back to its measurement id.
_MEASUREMENT_EVENTS = re.compile(
    rf"{MEASUREMENTS_DIR}/(?P<measurement_id>[^/]+)/{re.escape(EVENTS_FILE)}\Z"
)


@dataclass(frozen=True)
class _Dispatch:
    """#109 (a28 spec §3.8): one materialization of an instance and the tries it ran.

    `start` is where its first materialization record starts in the
    measurement journal, `end` where the last record of its materialization
    or of a try it ran ends. `tries` are the `(case_id, sample_id,
    retry_ordinal)` it ran, `rows` the `(case_id, sample_id)` of those that
    were not repair tries.
    """

    instance_id: str
    start: int
    end: int
    tries: frozenset[tuple[str, str, int]]
    rows: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class _DispatchIndex:
    """A measurement journal's dispatches in order, and where its rollout records start."""

    dispatches: tuple[_Dispatch, ...]
    starts: tuple[int, ...]
    rollout_starts: tuple[int, ...]

    def first_after(self, end: int) -> _Dispatch | None:
        """The first dispatch whose materialization starts at or after byte `end`."""

        at = bisect_left(self.starts, end)
        return self.dispatches[at] if at < len(self.dispatches) else None

    def rollout_between(self, start: int, end: int | None) -> bool:
        """Does a rollout record start in bytes `[start, end)` (`None`: the journal's end)?"""

        at = bisect_left(self.rollout_starts, start)
        return at < len(self.rollout_starts) and (end is None or self.rollout_starts[at] < end)


def _dispatch_index(records: Sequence[Envelope]) -> _DispatchIndex:
    """Each instance a measurement journal materialized, with the tries it ran.

    A try ran on the instance its target-call records name. One that started
    and named none (the power went first) ran on the instance materialized
    last before its `rollout_started`: its own session's, since a session
    materializes before it dispatches a row. A rollout record is any record of
    a try, one that carries a `sample_id` (a carried pair's `sample_imported`
    is not a try).
    """

    first: dict[str, int] = {}
    last: dict[str, int] = {}
    latest: str | None = None
    named: dict[tuple[str, str, int], str] = {}
    started_on: dict[tuple[str, str, int], str] = {}
    ends: dict[tuple[str, str, int], int] = {}
    repairs: set[tuple[str, str, int]] = set()
    rollout: list[int] = []
    offset = 0
    for record in records:
        start, offset = offset, offset + len(record.encode())
        payload = record.payload
        instance = payload.get("instance_id")
        if record.event.startswith("target_materialize_") and isinstance(instance, str):
            first.setdefault(instance, start)
            last[instance] = offset
            latest = instance
            continue
        if "sample_id" not in payload or record.event == "sample_imported":
            continue
        rollout.append(start)
        key = (
            str(payload.get("case_id")),
            str(payload["sample_id"]),
            int(payload.get("retry_ordinal", 0)),
        )
        if record.event == "rollout_started":
            if payload.get("repair") is True:
                repairs.add(key)
            if latest is not None:
                started_on.setdefault(key, latest)
        if isinstance(instance, str) and instance in first:
            named.setdefault(key, instance)
        ends[key] = offset
    tries: dict[str, set[tuple[str, str, int]]] = {name: set() for name in first}
    for key in ends:
        owner = named.get(key, started_on.get(key))
        if owner is not None:
            tries[owner].add(key)
    dispatches = tuple(
        _Dispatch(
            instance_id=name,
            start=first[name],
            end=max([last[name], *(ends[key] for key in tries[name])]),
            tries=frozenset(tries[name]),
            rows=frozenset((key[0], key[1]) for key in tries[name] if key not in repairs),
        )
        for name in sorted(first, key=first.__getitem__)
    )
    return _DispatchIndex(
        dispatches, tuple(dispatch.start for dispatch in dispatches), tuple(rollout)
    )


@dataclass(frozen=True)
class _RedispatchWindows:
    """#109 (a28 spec §3.8): what the campaign records say about where a dispatch ran.

    `window_of[i]` is the window of record `i`, the one the latest suspension
    before it started: the number of suspensions before it, so a suspension
    belongs to the window it ends. `attested` and `observed` are the first
    `instance_attested` of each `(measurement_id, instance_id)` and the first
    `indeterminate_observed` of each `(measurement_id, case_id, sample_id,
    retry_ordinal)`. Per measurement, `cited` is `(index, end)` of every
    citation of a prefix of its journal, wherever `journal_citations` finds
    it (a suspension's `prefix` and a family-13 calibration's included), and
    `redispatched` that of each `stage_redispatched`, in order. `drifts` are
    the drift suspensions written under a version that owes the record, as
    `(index, measurement_id, prefix end, (low, high))`, where the stage the
    drift left open holds the repeats in `(low, high]`.
    """

    window_of: tuple[int, ...]
    suspensions: frozenset[int]
    attested: Mapping[tuple[str, str], int]
    observed: Mapping[tuple[str, str, str, int], int]
    cited: Mapping[str, tuple[tuple[int, int], ...]]
    redispatched: Mapping[str, tuple[tuple[int, int], ...]]
    drifts: tuple[tuple[int, str, int, tuple[int, int]], ...]
    #: The index of every `stage_redispatched`.
    carriers: frozenset[int] = frozenset()

    def place(self, measurement_id: str, dispatch: _Dispatch) -> int | None:
        """The window `dispatch` ran in, or `None` when the journals cannot say.

        Spec §3.8 rules 1-4, from two facts the journals guarantee: a
        suspension is the last record its session writes, and an instance's
        `instance_attested` is written only in the session that materialized
        it. So the window is (1) that of the instance's attestation; else (2)
        that of the first accounted try it ran, which its own session or,
        after a crash, the next one's `account_resumed` writes before anything
        there can suspend; else (3) the one between the latest citation of the
        journal ending at or before the dispatch and the earliest ending after
        it, when no suspension lies between them; else (4) none.

        Rule 3 reads the end of the campaign log as the latest bound when no
        citation ends after the dispatch: a crashed session that nothing has
        cited since, with no suspension after the earlier citation, ran in the
        last window (§3.8's case 4 needs a suspension before the next
        citation). The lower bound is then the latest citation that is not a
        `stage_redispatched`: a record's own citation does not vouch for its
        window there, or a record moved past a later suspension would place
        the dispatch it missed in its own window (Task 4 review, R-i). The
        caller holds the log to be whole, not a refused prefix.
        """

        attested = self.attested.get((measurement_id, dispatch.instance_id))
        if attested is not None:
            return self.window_of[attested]
        observed = [
            self.observed[(measurement_id, case_id, sample_id, ordinal)]
            for case_id, sample_id, ordinal in dispatch.tries
            if (measurement_id, case_id, sample_id, ordinal) in self.observed
        ]
        if observed:
            return self.window_of[min(observed)]
        cited = self.cited.get(measurement_id, ())
        after = min((index for index, end in cited if end >= dispatch.end), default=None)
        # Residual of rule 3 as written, a forgery only, accepted by the
        # maintainer as a known limitation (ADR-0019 as amended 2026-10-03):
        # with a later citation in its window, a stale
        # `stage_redispatched` can still be the lower bound that places its
        # own dispatch. An honest record cites the head right before the
        # dispatch materializes, so it lies in that dispatch's window anyway.
        before = max(
            (
                index
                for index, end in cited
                if end <= dispatch.start and (after is not None or index not in self.carriers)
            ),
            default=None,
        )
        if before is None or (after is not None and before >= after):
            return None
        # What follows a suspension's own citation runs in the window it starts.
        opened = self.window_of[before] + int(before in self.suspensions)
        closed = len(self.suspensions) if after is None else self.window_of[after]
        return opened if opened == closed else None


def _redispatch_windows(records: Sequence[Envelope]) -> _RedispatchWindows:
    """`_RedispatchWindows` over the campaign records, in one pass.

    The version in force at a drift is the latest `producing_version` written
    before it (`_Evidence.version_before`'s rule, ADR-0015).
    """

    window_of: list[int] = []
    suspensions: set[int] = set()
    attested: dict[tuple[str, str], int] = {}
    observed: dict[tuple[str, str, str, int], int] = {}
    cited: dict[str, list[tuple[int, int]]] = {}
    redispatched: dict[str, list[tuple[int, int]]] = {}
    drifts: list[tuple[int, str, int, tuple[int, int]]] = []
    looks: dict[str, list[int]] = {}
    version = ""

    def prefix_of(locator: object) -> tuple[str, int] | None:
        match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
        named = None if match is None else _MEASUREMENT_EVENTS.fullmatch(match["file"])
        if match is None or named is None or int(match["start"]) != 0:
            return None
        return str(named["measurement_id"]), int(match["end"])

    for index, record in enumerate(records):
        payload = record.payload
        window_of.append(len(suspensions))
        prefixes: list[tuple[str, int]] = []
        for kind, locator in journal_citations(record.event, payload):
            found = prefix_of(locator) if kind == "journal_prefix" else None
            if found is not None:
                prefixes.append(found)
        for measurement_id, end in prefixes:
            cited.setdefault(measurement_id, []).append((index, end))
        if record.event == "stage_opened":
            looks.setdefault(str(payload["measurement_id"]), []).append(int(payload["look"]))
        elif record.event == "instance_attested":
            key = (str(payload["measurement_id"]), str(payload["instance_id"]))
            attested.setdefault(key, index)
        elif record.event == "indeterminate_observed":
            observed.setdefault(
                (
                    str(payload["measurement_id"]),
                    str(payload["case_id"]),
                    str(payload["sample_id"]),
                    int(payload.get("retry_ordinal", 0)),
                ),
                index,
            )
        elif record.event == "stage_redispatched":
            for measurement_id, end in prefixes:
                redispatched.setdefault(measurement_id, []).append((index, end))
        elif record.event == "campaign_suspended":
            if (
                payload.get("reason") == "target_components_changed"
                and prefixes
                and stage_redispatch_required(version)
            ):
                measurement_id, end = prefixes[0]
                opened = looks.get(measurement_id, [])
                if opened:
                    low = opened[-2] if len(opened) > 1 else 0
                    drifts.append((index, measurement_id, end, (low, opened[-1])))
            suspensions.add(index)
        if isinstance(payload.get("producing_version"), str):
            version = payload["producing_version"]
    return _RedispatchWindows(
        window_of=tuple(window_of),
        suspensions=frozenset(suspensions),
        attested=attested,
        observed=observed,
        cited={name: tuple(entries) for name, entries in cited.items()},
        redispatched={name: tuple(entries) for name, entries in redispatched.items()},
        drifts=tuple(drifts),
        carriers=frozenset(index for entries in redispatched.values() for index, _end in entries),
    )


class _PrefixReads:
    """`unfilled_slots`'s two row reads, over S1's lifecycle of a cited prefix."""

    def __init__(self, state: Lifecycle) -> None:
        self._state = state

    def sample_records(
        self, case_id: str, sample_id: str
    ) -> tuple[Envelope | None, Envelope | None]:
        key = (case_id, sample_id)
        return self._state.executions.get(key), self._state.scoring.get(key)

    def imported_record(self, case_id: str, sample_id: str) -> Envelope | None:
        return self._state.imported.get((case_id, sample_id))


def _check_stage_redispatched(context: _Evidence, record: Envelope) -> None:
    """`stage_redispatched`: a carried stage's unfilled slots, at its journal's head (#109).

    a28 spec §3.8. The lifecycle bounds `count` and holds the cited prefix to
    its form and to every earlier citation of the journal, from locators
    alone; here the bytes are readable, and three things are re-proven:

    - the lifecycle's own admission, replayed to just before the record
      (`refusal_at`): family, version in force, the measurement, its carried
      latest stage, the bound and the lower bound;
    - `count` is the unfilled slots over the cited prefix -- `unfilled_slots`,
      the derivation the executor and I2's `released` share -- with the
      retries the campaign's tries accounted before the record reserved;
    - the prefix is the journal's head at the dispatch the record covers: the
      first dispatch after its cited end, when that dispatch lies in the
      record's window, begins with no rollout record in between. A record
      whose window ends with no dispatch of the measurement covers nothing
      (codex spec round 2), and so does one whose first dispatch ran no try,
      refused at materialization: it used no slot (controller ruling R8). A
      first dispatch the journals cannot place in a window is
      `stage_redispatched_unverifiable`.

    Every other finding is `stage_redispatched_mismatch`.
    """

    from evolution.tree.campaign_measurement import unfilled_slots  # circular at module scope

    code = "stage_redispatched_mismatch"
    payload = record.payload
    measurement_id = str(payload["measurement_id"])
    reference = _only_reference(record)
    try:
        prefix = _measurement_prefix(context, reference, measurement_id)
    except _PreconditionFailed as failed:
        raise _PreconditionFailed(failed.detail, code) from None
    index = context.records.index(record)
    refusal = context.refusal_at(index)
    if refusal is not None:
        raise _PreconditionFailed(f"the lifecycle does not admit it here: {refusal}", code)
    snapshot = context.snapshot_before(index)
    look = int(snapshot.measurements[measurement_id]["stages"][-1]["look"])
    reserved = {
        (str(key[1]), str(key[2]), int(key[3]))
        for key in snapshot.accounted
        if key[0] == measurement_id and len(key) == 4 and key not in snapshot.retries_declined
    }
    # Family 13 (pre-F8 spec §2.4): a restored D1 retry holds no stage slot,
    # nor (#194, G3) one after a cancelled try a Champion listing restored.
    declined = declined_tries(snapshot.retries_declined, measurement_id) | restored_tries(
        snapshot.listing_restored, measurement_id
    )
    _prefix, state = context.measurement_state(reference)
    schedule = prefix[0].payload["identity"]["schedule"]
    # The executor's `_unfilled_slots` reads the stage to its horizon (design §5).
    horizon = _last_stage_horizon(context, measurement_id, look, schedule)
    count = unfilled_slots(schedule, _PrefixReads(state), horizon, reserved, declined)
    if count != int(payload["count"]):
        raise _PreconditionFailed(
            "count is not the stage's unfilled slots over the cited prefix", code
        )
    end = context.range_of(reference)[1]
    journal = context.dispatch_index(measurement_id)
    dispatch = journal.first_after(end)
    # Whatever follows, no rollout record lies between the cited end and the
    # next materialization (or the journal's end): a cited prefix that cuts
    # through a dispatch's rows is not the head at any dispatch.
    if journal.rollout_between(end, None if dispatch is None else dispatch.start):
        raise _PreconditionFailed(
            "the cited prefix is not the journal's head at the dispatch it precedes", code
        )
    if dispatch is None or not dispatch.tries:
        # A dispatch that ran no try -- refused at materialization, the
        # record's window ending with the drift again -- used no slot and
        # needs no window: the window keeps the moved slots unused, as spec
        # §7 says (controller ruling R8). The next record owes the next one.
        return
    windows = context.redispatch_windows()
    placed = windows.place(measurement_id, dispatch)
    if placed is None:
        raise _PreconditionFailed(
            f"the journals cannot place the dispatch of {dispatch.instance_id} after it in a window",
            "stage_redispatched_unverifiable",
        )
    own = windows.window_of[index]
    if placed > own:
        return  # its window ended with no dispatch: the next window's record owes that one
    if placed < own:
        raise _PreconditionFailed(
            "the cited prefix is not the journal's head at the dispatch it precedes", code
        )


def _stage_redispatch_findings(context: _Evidence) -> list[dict[str, Any]]:
    """#109 (a28 spec §3.8 "Required in the window"): a carried dispatch has its record.

    Every dispatch past the `prefix` of a drift suspension written under a
    version that owes `stage_redispatched`, that runs a row of the stage the
    drift left open other than a repair try, must lie in a window holding a
    `stage_redispatched` of its measurement whose cited end is at or before
    the dispatch's first record. Such a dispatch starts with unfilled slots,
    so without one its rows run without them -- #109's false crossing:
    `stage_redispatched_missing` (codex spec round 1). A dispatch the
    journals cannot place is `stage_redispatched_unverifiable`, not proven
    rather than proven wrong -- except the first dispatch after a record,
    which that record's own check reports. A finding is located at the latest
    drift suspension it follows.
    """

    if not context.records:
        return []
    windows = context.redispatch_windows()
    owed: dict[str, list[tuple[int, int, tuple[int, int]]]] = {}
    for index, measurement_id, end, looks in windows.drifts:
        owed.setdefault(measurement_id, []).append((index, end, looks))
    findings: list[dict[str, Any]] = []
    for measurement_id, drifts in owed.items():
        try:
            journal = context.dispatch_index(measurement_id)
            _identity, schedule = context.run_identity(measurement_id)
            # Design §5: the stage a drift left open holds its repeats to its horizon.
            horizon = {
                high: _last_stage_horizon(context, measurement_id, high, list(schedule.values()))
                for _index, _end, (_low, high) in drifts
            }
        except _PreconditionFailed:
            continue  # an unreadable journal is reported by whichever record cites it
        records = windows.redispatched.get(measurement_id, ())
        covered = {
            first.instance_id
            for _index, end in records
            if (first := journal.first_after(end)) is not None
        }
        for dispatch in journal.dispatches:
            repeats = [int(schedule[row]["repeat"]) for row in dispatch.rows if row in schedule]
            carried = [
                index
                for index, end, (low, high) in drifts
                if end <= dispatch.start
                and any(low < repeat <= horizon[high] for repeat in repeats)
            ]
            if not carried:
                continue
            placed = windows.place(measurement_id, dispatch)
            if placed is None:
                if dispatch.instance_id in covered:
                    continue
                code = "stage_redispatched_unverifiable"
                detail = (
                    f"the journals cannot place {measurement_id}'s dispatch of "
                    f"{dispatch.instance_id} in a window"
                )
            elif any(
                windows.window_of[index] == placed and end <= dispatch.start
                for index, end in records
            ):
                continue
            else:
                code = "stage_redispatched_missing"
                detail = (
                    f"{measurement_id}'s dispatch of {dispatch.instance_id} runs the stage "
                    "a drift left open with no stage_redispatched in its window"
                )
            findings.append(_campaign_finding(context, carried[-1], code, detail))
    return findings


def _measurement_prefix(
    context: _Evidence, reference: Mapping[str, Any], measurement_id: str
) -> tuple[Envelope, ...]:
    """The referenced prefix, required to be that measurement's own journal."""

    expected = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    if reference.get("kind") != "journal_prefix" or context.file_of(reference) != expected:
        raise _PreconditionFailed("the evidence does not name the measurement's own prefix")
    return context.measurement_records(reference)


def _unscored(records: Sequence[Envelope], look: int | None = None) -> tuple[str, ...]:
    """Scheduled samples of a measurement prefix that have no `score` disposition.

    S1's own `recovery_plan` decides each sample's final disposition, and the
    schedule comes from the prefix's own `run_created`, so the answer is a
    function of the referenced bytes alone.

    This is not the *same call* the executor makes: `Measurement.complete()`
    reads `sample_rows` → `Journal.sample_records` → `Measurement._row`. The two
    are different S1-owned reductions that agree on the only question asked here
    — did this sample score — because both answer it the same way: a scoreable
    execution terminal *and* a `scoring_finished`. Every other state, an
    `unscored` execution terminal whose scoring never finished included, is not a
    score in either. A divergence on that question would make this audit report a
    false `precondition_failed`, so it is the equivalence to preserve if either
    reduction ever changes.
    """

    try:
        plan = recovery_plan(records)
    except (SchemaError, KeyError, TypeError):
        raise _PreconditionFailed("the referenced prefix is not a measurement journal") from None
    repeats = {
        (row["case_id"], row["sample_id"]): int(row["repeat"])
        for row in records[0].payload["identity"]["schedule"]
    }
    missing = []
    for decision in plan.executions:
        key = (decision.case_id, decision.sample_id)
        if look is not None and repeats.get(key, 1) > look:
            continue
        if decision.evaluation is None or decision.evaluation.kind != "score":
            missing.append(f"{decision.case_id}/{decision.sample_id}")
    return tuple(missing)


#: Every S1 record that can be a sample's terminal, so a reference to anything
#: else is not evidence about a sample at all. A carried pair's terminal is its
#: `sample_imported` record: the lifecycle admits nothing after it for the pair.
_SAMPLE_TERMINALS = frozenset(
    {
        "sample_imported",
        "rollout_finished",
        "rollout_failed",
        "rollout_interrupted",
        "scoring_finished",
        "scoring_failed",
        "scoring_interrupted",
    }
)


def _final_disposition(
    records: Sequence[Envelope], case_id: str, sample_id: str
) -> tuple[str, str | None, str | None] | None:
    """One sample's `(kind, owner, reason)` over a whole measurement prefix.

    Derived through S1's own reduction — `recovery_plan`, whose `evaluation` is
    the `SampleDisposition` the registry assigns — so every terminal kind is
    covered by construction rather than by a table this module would have to
    keep in step. The one case S1 leaves open is C1's own (amendment 4): a
    scoreable execution terminal whose scoring never reached one is `unscored`,
    which the executor accounts as `indeterminate(rollout, rollout_interrupted)`.
    `None` means the sample has no terminal at all.
    """

    try:
        plan = recovery_plan(records)
    except (SchemaError, KeyError, TypeError):
        raise _PreconditionFailed("the referenced prefix is not a measurement journal") from None
    for decision in plan.executions:
        if (decision.case_id, decision.sample_id) != (case_id, sample_id):
            continue
        if decision.evaluation is not None:
            return (
                decision.evaluation.kind,
                decision.evaluation.owner,
                decision.evaluation.reason,
            )
        if decision.outcome is None:
            return None
        return ("indeterminate", "rollout", "rollout_interrupted")
    return None


def _cited_terminal(
    context: _Evidence, reference: Mapping[str, Any], measurement_id: str
) -> tuple[Envelope, tuple[Envelope, ...]]:
    """The cited sample terminal and its measurement's prefix, or a refusal.

    The record must be a sample terminal in that measurement's own journal and
    the sample's **final** record: a `rollout_finished` a `scoring_finished` came
    after says nothing about how the sample ended.
    """

    if reference.get("kind") != "journal_record":
        raise _PreconditionFailed("the evidence is not a sample's terminal record")
    expected = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    if context.file_of(reference) != expected:
        raise _PreconditionFailed("the evidence is not in the measurement's own journal")
    terminal = context.one_record(reference, S1_FAMILY)
    if terminal.event not in _SAMPLE_TERMINALS:
        raise _PreconditionFailed("the evidence is not a sample terminal record")
    records = context.measurement_journal(measurement_id)
    case_id = terminal.payload.get("case_id")
    sample_id = terminal.payload.get("sample_id")
    end = context.range_of(reference)[1]
    offset = 0
    for entry in records:
        encoded = entry.encode()
        start, offset = offset, offset + len(encoded)
        if start < end:
            continue
        if (entry.payload.get("case_id"), entry.payload.get("sample_id")) == (case_id, sample_id):
            raise _PreconditionFailed("the cited record is not the sample's final one")
    return terminal, records


def _only_reference(record: Envelope) -> Mapping[str, Any]:
    """The one reference a single-evidence §5 row is held to.

    "The evidence a record relied on" is that record's own; a further intact
    reference no precondition reads is not evidence, it is decoration, and an
    audit that hashes it without reading it says more than it checked.
    """

    evidence = record.payload["evidence"]
    if len(evidence) != 1:
        raise _PreconditionFailed("the record cites more than the evidence it relies on")
    return dict(evidence[0])


def _check_measurement_complete(context: _Evidence, record: Envelope) -> None:
    """`baseline_complete` / `champion_complete`: every scheduled sample scored (§6)."""

    payload = record.payload
    reference = _only_reference(record)
    _measurement_prefix(context, reference, payload["measurement_id"])
    records, state = context.measurement_state(reference)
    rows = latest_rows(records, state)
    _refuse_out_of_range(
        context,
        record,
        [(str(payload["measurement_id"]), rows)],
        context.policy_for(str(payload["measurement_id"])).metric_primary,
    )
    unsettled = [
        f"{row.case_id}/{row.sample_id}"
        for row in rows
        if row.disposition != "score" and not row.exhausted
    ]
    if unsettled:
        raise _PreconditionFailed(
            f"the referenced prefix has {len(unsettled)} unsettled sample(s): "
            + ", ".join(unsettled[:3])
        )
    if any(row.exhausted for row in rows) and not _recorded_exclusion(
        context, record, str(payload["measurement_id"])
    ):
        raise _PreconditionFailed("exhausted samples complete with no pairs_excluded")
    _refuse_owed_threshold_repair(context, record, str(payload["measurement_id"]), records)
    # The carried set is audited once per born measurement, completed or
    # not, by `_imported_copy_findings` (ADR-0017 audit scope), not here.
    return


def _baseline_abandoned(context: _Evidence) -> bool:
    """Whether an applied decision freed the owed copy (§5.3).

    `authorization_applied(effect: fresh_baseline)`, or -- family 7, ruling R9
    of I2 -- the block of a `rebaseline` accepted before any round of the run
    bound its baseline: round 1 then binds its ordinary `m-r00-champion` in the
    new epoch and measures it from empty, exactly as a `fresh_baseline` would.
    Either is admitted at most once and only while the first-round baseline
    has no recorded stage, so once it appears anywhere in the campaign journal
    the copy stayed owed and unmeasured -- order relative to the record under
    check does not matter.
    """

    begun = False
    for record in context.records:
        if record.event == "baseline_bound":
            begun = True
        elif record.event == "authorization_applied":
            effect = record.payload.get("effect")
            if effect == "fresh_baseline" or (effect == "rebaseline" and not begun):
                return True
    return False


def _check_baseline_import(
    context: _Evidence, measurement_id: str, records: Sequence[Envelope]
) -> None:
    """`sample_imported` is only the declared carried baseline, and matches it (§3.3).

    What this proves, and does not: declaration-to-copy integrity -- the
    complete `sample_imported` payloads of the born measurement journal, in
    record order, digest to the creation record's own `pairs_digest`, and
    their count is its own `imported`, each holds the run's `metric.primary`
    -- and that no other measurement ever holds one. It does not re-prove the
    source side of admission; that is
    attested only by the `source` digests each row carries (§3.3 last
    paragraph).
    """

    from evolution.tree.baseline_import import import_pairs_digest  # circular at module scope

    imported = [entry.payload for entry in records if entry.event == "sample_imported"]
    linked = context.identity().to_payload().get("linked_from")
    declared = linked.get("baseline") if linked is not None else None
    if (
        measurement_id == measurement_id_for(None)
        and declared is not None
        and not _baseline_abandoned(context)
    ):
        if len(imported) != declared["imported"] or (
            import_pairs_digest(imported) != declared["pairs_digest"]
        ):
            raise _PreconditionFailed(
                "the carried baseline does not match its declared pairs_digest",
                "baseline_import_mismatch",
            )
        # §3.3 target-side admission: a carried row is a score of this run's
        # own primary metric, or it was never admissible.
        primary = context.policy_for(measurement_id).metric_primary
        if any(primary not in row["metrics"] for row in imported):
            raise _PreconditionFailed(
                "a carried row lacks the run's primary metric",
                "baseline_import_mismatch",
            )
        return
    if imported:
        raise _PreconditionFailed(
            "a measurement other than the declared carried baseline holds sample_imported",
            "baseline_import_mismatch",
        )


def _check_attempt_sealed(context: _Evidence, record: Envelope) -> None:
    """`attempt_sealed`: S2 published this Candidate with these digests (§5)."""

    payload = record.payload
    reference = _only_reference(record)
    if reference.get("kind") != "ledger_record":
        raise _PreconditionFailed("the evidence is not a ledger record")
    finished = context.one_record(reference, ARTIFACT_FAMILY)
    if finished.event != "artifact_copy_finished":
        raise _PreconditionFailed("the evidence is not S2's publication record")
    operation = finished.to_payload()["operation_id"]
    for started in context.ledger():
        if started.event != "artifact_copy_started":
            continue
        if started.to_payload()["operation_id"] != operation:
            continue
        opened = started.payload
        if (
            opened["kind"] != "candidate"
            or opened["artifact_id"] != payload["attempt_id"]
            or opened["candidate_digest"] != payload["candidate_digest"]
            or opened["dependency_digest"] != payload["dependency_digest"]
        ):
            raise _PreconditionFailed("the publication names another Candidate")
        return
    raise _PreconditionFailed("the publication has no opening record in the ledger")


def _check_instance_attested(context: _Evidence, record: Envelope) -> None:
    """`instance_attested`: the measurement did materialize that instance (§8)."""

    payload = record.payload
    prefix = _measurement_prefix(context, _only_reference(record), payload["measurement_id"])
    run_identity = prefix[0].payload["identity"]
    if run_identity["candidate_digest"] != payload["loaded_candidate_digest"]:
        raise _PreconditionFailed("the measurement loaded another Candidate")
    if not any(
        entry.event.startswith("target_materialize_")
        and entry.payload.get("instance_id") == payload["instance_id"]
        for entry in prefix
    ):
        raise _PreconditionFailed("the referenced prefix never materialized that instance")
    # The identity is first-hand C1 evidence: R1's materialization records carry
    # no reported `TargetIdentity`, so no sibling can corroborate it. What an
    # audit can still do is re-check it against the row that carries it — the
    # lifecycle's own consistency rule, re-run over a journal it never admitted.
    # Whether that identity *admits* the Candidate is not this record's
    # precondition (a target may truthfully attest too little); it is the
    # promotion verdict's, and `attestation_verdict` answers it there.
    identity = TargetIdentity.from_payload(payload["identity"])
    if (
        identity.instance.loaded_candidate_digest != payload["loaded_candidate_digest"]
        or identity.semantic.dependency_digest != payload["dependency_digest"]
        or identity.instance.boot_id != payload["boot_id"]
    ):
        raise _PreconditionFailed("the attested identity disagrees with its own row")
    # G2 spec §4.2: family 12 points at the map by digest only; the map itself is
    # the measurement's own `components_declared`, which precedes the attestation
    # and so lies inside the very prefix the record cites.
    digest = payload.get("components_digest")
    if digest is None:
        return  # an instance that declares no map is not compared (G2b3b)
    declared = [
        str(entry.payload["components_digest"])
        for entry in prefix
        if entry.event == "components_declared"
    ]
    if digest not in declared:
        raise _PreconditionFailed("the referenced prefix never declared that component map")
    _check_attested_drift(context, record, str(digest), declared)


def _check_attested_drift(
    context: _Evidence, record: Envelope, digest: str, declared: Sequence[str]
) -> None:
    """G2b3b: an attested map other than the side's reference was not drift.

    Inside its reference epoch a different map suspends and is never attested
    (`drift_suspends`); after an accepted change it may be, until the
    measurement has rendered the reference itself. So an attestation of a
    Candidate or fresh-Parent measurement of a component-mode Attempt whose map
    is not the reference is a finding when the measurement is bound in the
    reference epoch, or when its journal (`declared`, in journal order)
    declared the reference before it first declared the attested map. A
    family-13 cohort member is held to its reference the same way
    (`_entry_drift_reference`, F7 plan choice 10).
    """

    measurement_id = str(record.payload["measurement_id"])
    entry = _bound_entry(context, measurement_id)

    def at() -> tuple[int, CampaignSnapshot]:
        index = context.records.index(record)
        return index, context.snapshot_before(index)

    reference = _entry_drift_reference(context, entry, at)
    if reference is None or digest == reference.digest:
        return
    rendered_first = reference.digest in declared and declared.index(
        reference.digest
    ) < declared.index(digest)
    if reference.epoch == int(entry["epoch"]) or rendered_first:
        raise _PreconditionFailed("an attested drift inside its reference epoch")


def _check_indeterminate_observed(context: _Evidence, record: Envelope) -> None:
    """`indeterminate_observed`: the cited terminal *is* that owner's indeterminate.

    §5: "the sample's terminal record has disposition `indeterminate`". The audit
    derives `(kind, owner, reason)` from the cited sample's final disposition
    through S1's own reduction, so every terminal kind is compared, not only
    `rollout_failed` — a `scoring_failed` cannot support a target-owned claim.
    """

    reference = _only_reference(record)
    _check_indeterminate_try(context, record, reference)
    return


def _sample_tries(state: Lifecycle, key: tuple[str, str]) -> tuple[tuple[Envelope, Any], ...]:
    """Every try of one sample in order, latest last (`Journal.sample_tries`)."""

    if key not in state.executions:
        return ()
    return (*state.tries.get(key, ()), (state.executions[key], state.scoring.get(key)))


def _repair_ordinals(records: Sequence[Envelope]) -> set[tuple[str, str, int]]:
    """`(case_id, sample_id, retry_ordinal)` of every try started as a repair.

    I1 spec §5.4: the flag sits on the `rollout_started` record alone -- a
    try's terminal carries the ordinary sample payload -- so a reader that
    holds only the latest terminal cannot tell a repair from a D1 retry.
    """

    return {
        (
            str(record.payload["case_id"]),
            str(record.payload["sample_id"]),
            int(record.payload.get("retry_ordinal", 0)),
        )
        for record in records
        if record.event == "rollout_started" and record.payload.get("repair") is True
    }


def _try_terminal(execution: Envelope, scoring: Envelope | None) -> Envelope | None:
    """A try's terminal record (`CampaignMeasurement._terminal_of`), `None` while open."""

    target = scoring if scoring is not None and scoring.event != "scoring_started" else execution
    return None if target.event.endswith("_started") else target


def _check_indeterminate_try(
    context: _Evidence, record: Envelope, reference: Mapping[str, Any]
) -> None:
    """Family 6 (spec §3.5): one `indeterminate_observed` per indeterminate **try**.

    The lifecycle admits `retry_ordinal` `k` without try `k - 1` and without
    reading the journal, so the audit resolves the try itself: try `k` must exist
    in the measurement journal and carry ordinal `k`, the evidence must be that
    try's own terminal record, and the try's latest-row reduction must be
    indeterminate with the recorded owner and reason (audit item 4).
    """

    payload = record.payload
    measurement_id = str(payload["measurement_id"])
    if reference.get("kind") != "journal_record":
        raise _PreconditionFailed("the evidence is not a sample's terminal record")
    if context.file_of(reference) != f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}":
        raise _PreconditionFailed("the evidence is not in the measurement's own journal")
    terminal = context.one_record(reference, S1_FAMILY)
    if terminal.event not in _SAMPLE_TERMINALS:
        raise _PreconditionFailed("the evidence is not a sample terminal record")
    key = (str(payload["case_id"]), str(payload["sample_id"]))
    if (terminal.payload.get("case_id"), terminal.payload.get("sample_id")) != key:
        raise _PreconditionFailed("the evidence names another sample")
    state = context.journal_state(measurement_id)
    ordinal = int(payload["retry_ordinal"])
    tries = _sample_tries(state, key)
    if ordinal >= len(tries):
        raise _PreconditionFailed(f"try {ordinal} of the sample never ran")
    execution, scoring = tries[ordinal]
    if int(execution.payload.get("retry_ordinal", 0)) != ordinal:
        raise _PreconditionFailed(f"try {ordinal} of the sample carries another ordinal")
    closing = _try_terminal(execution, scoring)
    if closing is None:
        raise _PreconditionFailed(f"try {ordinal} of the sample has no terminal")
    if closing.encode() != context.raw(reference):
        raise _PreconditionFailed(f"the evidence is not try {ordinal}'s terminal")
    identity, schedule = context.run_identity(measurement_id)
    entry = schedule.get(key)
    if entry is None:
        raise _PreconditionFailed("the sample is not in the measurement's schedule")
    repairs = context.repair_ordinals(measurement_id)
    repair = (*key, ordinal) in repairs
    row = sample_row(
        entry,
        execution,
        scoring,
        seed_mode="none",
        dependency_digest="",
        boot_id="",
        retry_ordinal=ordinal,
        limit=ordinal_limit(int(identity.get("sample_retries") or 0), state.repairs.get(key, 0)),
        repaired=repair,
    )
    accounted = _owner_reason(row)
    if accounted is None:
        raise _PreconditionFailed(f"try {ordinal} is {row.disposition}, not indeterminate")
    if accounted != (payload["owner"], payload["reason"]):
        raise _PreconditionFailed("the cited terminal carries another owner or reason")
    # A record may deny the retry's denominator slot only when the run really
    # started no further try: a cancelled stage stops, and the measurement
    # journal is where that is visible (ADR-0008 as amended). The reverse -- a
    # reserved slot whose try the run never reached -- is the executor's own
    # admission and is not re-derivable here.
    # #78 (spec §3.4): unless that try is a repair a threshold suspension
    # granted after this one; whether the grant was spent once, and on the
    # pair it named, is `_repair_findings`' to report, once per measurement.
    # Family 13 (pre-F8 spec §2.6): or the D1 retry a listing's grant
    # restored (`repair: false`), which `_repair_findings` audits the same way.
    if (
        not payload["retry_follows"]
        and ordinal + 1 < len(tries)
        and not (
            any(
                granted == ordinal
                for granted, _naming in context.threshold_replay()
                .grants.get(measurement_id, {})
                .get(key, ())
            )
        )
    ):
        raise _PreconditionFailed(f"try {ordinal + 1} ran after the record denied a retry")
    # I1 spec §5.4: a repair try is outside `sample_retries` and reserves no
    # slot in the denominator, whatever its ordinal. The lifecycle admits
    # `retry_follows` below the limit because it cannot tell a repair from a
    # D1 retry; the execution record says which this is.
    if repair and payload["retry_follows"]:
        raise _PreconditionFailed(f"the repair try {ordinal} reserves a retry")
    # I2 spec §5.2 (Task 9): nothing is appended to a frozen measurement again,
    # so a try of one accounted after the change that froze it can reserve a
    # retry only when its successor already ran there. The retry its pair still
    # owes runs as the re-bound measurement's dispatched sample.
    if (
        payload["retry_follows"]
        and ordinal + 1 >= len(tries)
        and _frozen_before(
            context.records, record, measurement_id, family_version=context.family_version
        )
    ):
        raise _PreconditionFailed(f"try {ordinal} of a frozen measurement reserves a retry")


def _frozen_before(
    records: Sequence[Envelope],
    record: Envelope,
    measurement_id: str,
    *,
    family_version: int | None = None,
) -> bool:
    """Was `measurement_id` frozen when `record` was written (ADR-0020)?

    An `identity_rebased` moving a comparable field -- an in-place condition
    change, the only one that may -- opens an epoch and freezes every
    measurement declared before it, as the lifecycle does (`_frozen_at`).
    """

    bound = False
    for earlier in records[: records.index(record)]:
        if (
            earlier.event in _MEASUREMENT_CREATIONS
            and earlier.payload["measurement_id"] == measurement_id
        ):
            bound = True
        elif (
            bound
            and earlier.event == "identity_rebased"
            and any(
                comparable_field(str(name), family_version=family_version)
                for name in earlier.payload["changed_fields"]
            )
        ):
            return True
    return False


def _baseline_exclusion(
    context: _Evidence,
    record: Envelope,
    baseline_id: str,
    records: Sequence[Envelope],
    state: Lifecycle,
    look: int,
) -> frozenset[tuple[str, int]]:
    """The `(case_id, repeat)` pairs a look's baseline excludes at `look` (spec §4.3).

    The executor's `_Campaign.excluded_pairs`, from the cited baseline prefix: the
    baseline's exhausted rows with repeat `<= look`, keyed by case and repeat so
    both sides lose the same pair under any seed mode (ruling R13). Families before 6 exclude nothing. Exhausted rows the
    campaign never recorded as `pairs_excluded` are not an exclusion the executor
    could have applied, so a look that relies on them is refused.
    """

    excluded = frozenset(
        (row.case_id, row.repeat) for row in latest_rows(records, state, look=look) if row.exhausted
    )
    if excluded and not _recorded_exclusion(context, record, baseline_id):
        raise _PreconditionFailed("the baseline's exhausted samples were never excluded")
    return excluded


def _comparison_measurements(
    context: _Evidence, attempt_id: str, record: Envelope
) -> tuple[str, ...]:
    """The measurements a look compares, derived from the campaign prefix.

    §8 compares the round's baseline with the attempt's own measurement, and
    both ids are durable: `baseline_bound` for the round the attempt belongs to,
    `measurement_bound` for the attempt. A record's evidence has to be those two
    and nothing else — checking whatever it supplied lets a look cite one side
    twice, or a stranger's prefix, and still verify. A family-12 Attempt with
    a fresh Parent adds its `parent_measurement_bound` third (G2 spec §4.6).
    """

    # The prefix before `record`: a later `continue` re-binds what the look
    # compared (I2 spec §5.2), and the look is about what it compared then.
    snapshot = reduce(
        context.records[: context.records.index(record)],
        selector=None,
        family_version=context.family_version,
    )
    for state in snapshot.rounds.values():
        if attempt_id not in state.attempts:
            continue
        baseline = state.baseline_measurement_id
        attempt = state.attempts[attempt_id].measurement_id
        if baseline is None or attempt is None:
            raise _PreconditionFailed("the look's two measurements are not both bound")
        parent = state.attempts[attempt_id].parent_measurement_id
        if parent is None:
            return str(baseline), str(attempt)
        return str(baseline), str(attempt), str(parent)
    raise _PreconditionFailed("the look names no attempt of this campaign")


def _cited_measurements(context: _Evidence, references: Sequence[Mapping[str, Any]]) -> list[str]:
    """The measurements a list of `journal_prefix` references names, in order."""

    cited = []
    for reference in references:
        if reference.get("kind") != "journal_prefix":
            raise _PreconditionFailed("the evidence is not a measurement prefix")
        cited.append(_measurement_of_reference(context, reference))
    return cited


def _check_gate_scope_selected(context: _Evidence, record: Envelope) -> None:
    """The frozen gate scope is exactly the prior settled reflection's scope."""

    payload = record.payload
    reference = _only_reference(record)
    if reference.get("kind") != "journal_record" or context.file_of(reference) != CAMPAIGN_FILE:
        raise _PreconditionFailed("the gate scope does not cite a campaign reflection")
    checked = context.one_record(reference, campaign_family_for(context.family_version))
    if checked.event not in ("reflection_finished", "reflection_failed"):
        raise _PreconditionFailed("the gate scope does not cite a reflection terminal")
    index = context.records.index(record)
    snapshot = reduce(context.records[:index], selector=None, family_version=context.family_version)
    state = snapshot.rounds.get(int(payload["round"]))
    diagnosis = None if state is None else state.diagnosis(True)
    if (
        diagnosis is None
        or checked.to_payload()["sequence"] != diagnosis["seq"]
        or checked.payload["round"] != payload["round"]
        or checked.payload["reflection_id"] != diagnosis["reflection_id"]
    ):
        raise _PreconditionFailed("the gate scope does not cite the round's prior diagnosis")
    if state is None or state.focus_portion is None:
        raise _PreconditionFailed("the adaptive round has no frozen focus portion")
    fallback = list(state.focus_portion["case_ids"])
    membership = None
    evaluable = None
    corpus_order: list[str] = []
    assert state is not None
    membership = context.adaptation_identity(before=index).family_membership
    baseline_id = str(state.baseline_measurement_id)
    repeats = context.policy_for(baseline_id).repeats
    corpus_order = [str(case["case_id"]) for case in context.identity_for(baseline_id).corpus_cases]
    evaluable = frozenset(
        row.case_id
        for row in latest_rows(
            context.measurement_journal(baseline_id),
            context.journal_state(baseline_id),
            look=repeats,
        )
        if not row.exhausted
    )
    expected = derive_gate_scope(
        reflection_failed=checked.event == "reflection_failed",
        groups=checked.payload.get("groups", []),
        fallback=fallback,
        family_membership=membership,
        corpus_order=corpus_order,
        evaluable_cases=evaluable,
    )
    if payload["source"] != expected.source or payload["cases"] != list(expected.cases):
        raise _PreconditionFailed("the gate scope differs from the prior reflection")
    if (
        payload["claimed_cases"] != list(expected.claimed_cases)
        or payload["selected_cases"] != list(expected.selected_cases)
        or payload["unevaluable_cases"] != list(expected.unevaluable_cases)
    ):
        raise _PreconditionFailed("the gate scope differs from the prior reflection")


def _check_look_not_started(context: _Evidence, record: Envelope) -> None:
    """`look_not_started`: the samples it names are not `score` (§5, §8).

    §5's precondition is about *the named samples*, so each one is resolved in
    its own measurement's schedule and checked; a record naming a sample that
    scored is a forgery even when some other sample really is short. The
    shortness itself is checked too: a look nothing bars was not barred.

    #91 spec §5: from family 7, under a producing version that is not exempt,
    an Attempt's look is barred only while no threshold is crossed and no
    threshold repair is owed on its measurements (`attempt_held_threshold`).
    """

    try:
        held = _held_threshold(
            context, context.records.index(record), str(record.payload["attempt_id"])
        )
    except (_PreconditionFailed, _Unverifiable):
        # An unreadable measurement is reported by the records citing it;
        # the record's own checks below still run.
        held = None
    if held is not None:
        raise _PreconditionFailed(held, "attempt_held_threshold")
    _check_confidence_bar(context, record)
    return


def _affected_layers_at(
    context: _Evidence,
    record: Envelope,
    attempt_id: str,
    corpus_cases: Sequence[Any],
    *,
    global_layer: bool,
) -> AffectedLayers:
    """A family-12 Attempt's layer sets, from the campaign prefix before `record`.

    The executor's `attempt_layers`: the Attempt's frozen selection and its
    round's frozen gate scope over `corpus_cases` (G2 spec §4.6).
    """

    prefix = context.snapshot_before(context.records.index(record))
    for state in prefix.rounds.values():
        attempt = state.attempts.get(attempt_id)
        if attempt is None:
            continue
        if attempt.selection is None:
            raise _PreconditionFailed("the look's Attempt has no affected-case selection")
        if state.gate_scope is None:
            raise _PreconditionFailed("the attempt's round has no prior gate scope")
        try:
            return affected_layers(
                attempt.selection,
                corpus_cases,
                state.gate_scope["cases"],
                global_layer=global_layer,
            )
        except (IndexError, KeyError, TypeError) as exc:
            raise _PreconditionFailed(f"the Attempt's layers cannot be derived: {exc}") from None
    raise _PreconditionFailed("the look names no attempt of this campaign")


def _check_confidence_bar(context: _Evidence, record: Envelope) -> None:
    """Family 13's `look_not_started` (design §5, §6.5; ADR-0016 as amended).

    The cited measurements are the look's -- the round's baseline, the
    Candidate and a fresh Parent, as `bar_look` cites them -- and `missing`
    is exactly what the executor's `look_missing` names over the cited
    Candidate and fresh-Parent prefixes, read to the look's stage horizon (on
    the last look a `case_repeats` Parent's last repeat), with the baseline's
    exclusions at the look. A bar that names nothing, or over a look nothing
    bars, was not barred.
    """

    payload = record.payload
    if not payload["missing"]:
        raise _PreconditionFailed("the record names no missing sample")
    look = int(payload["look"])
    expected = _comparison_measurements(context, str(payload["attempt_id"]), record)
    cited_order = _cited_measurements(context, payload["evidence"])
    if sorted(cited_order) != sorted(expected):
        raise _PreconditionFailed("the evidence is not the look's measurements")
    comparison = _family13_attempt(context, record).comparison
    if comparison is None:
        raise _PreconditionFailed("the bar names no governing comparison")
    references = dict(zip(cited_order, payload["evidence"], strict=True))
    states = {mid: context.measurement_state(references[mid]) for mid in expected}
    owing = expected[1:]  # the Candidate, then a fresh Parent: a baseline is settled
    bound = gate_inputs(
        comparison,
        max_repeat=max(_scheduled_max_repeat(states[mid][0]) for mid in owing),
        look=look,
        excluded=_baseline_exclusion(
            context,
            record,
            expected[0],
            *states[expected[0]],
            max(int(entry) for entry in comparison["looks"]),
        ),
    )
    owed = confidence_look_missing(
        comparison,
        {mid: latest_rows(*states[mid], look=bound.horizon) for mid in owing},
        corpus_ids=[
            str(case["case_id"]) for case in context.identity_for(expected[1]).corpus_cases
        ],
        excluded=bound.mask,
        horizon=bound.horizon,
    )
    if not owed:
        raise _PreconditionFailed("every cited measurement is complete to the look")
    named = [dict(entry) for entry in payload["missing"]]
    if named != owed:
        raise _PreconditionFailed(
            f"the missing samples are not the look's: the record names {_named(named)}, "
            f"the look owes {_named(owed)}"
        )


def _measurement_binding(context: _Evidence, measurement_id: str) -> Mapping[str, Any]:
    """The one durable `measurement_bound` a measurement was created with (spec §5 step 0).

    A measurement's *creation side* and its *role in a look* are different
    facts (codex branch round 1). Every measurement -- the seed champion's own
    (`attempt_id == CHAMPION_SIDE`) and every attempt's -- has exactly one
    `measurement_bound`, written before it is created, so it is found here by
    `measurement_id` alone, regardless of which round or look currently cites
    it: a promoted Candidate reused as a later round's baseline is not
    re-measured, and its binding is still the one its own attempt wrote. A
    family-12 fresh Parent's is its `parent_measurement_bound` (G2b3a), and a
    family-12 completion's its `completion_bound`, which names no
    `attempt_id`: it measures the champion side (G2b4).
    """

    if context.family_version == 15:
        return _bound_entry(context, measurement_id)
    for record in context.records:
        if (
            record.event
            in (
                "measurement_bound",
                "parent_measurement_bound",
                context.vocabulary.measurement_bound,
            )
            and record.payload["measurement_id"] == measurement_id
        ):
            if (
                # Family-13 design §6.8: the Champion member names no Attempt.
                record.event == context.vocabulary.measurement_bound
                and record.payload["attempt_id"] is None
            ):
                return {**record.payload, "attempt_id": CHAMPION_SIDE}
            return record.payload
    rebound = context.rebinds().get(measurement_id)
    if rebound is not None:
        # I2 spec §5.2: a re-bound measurement measures its source's Candidate
        # on the source's side; only the identity (its own epoch's) is new.
        source = _measurement_binding(context, str(rebound[1].payload["from_measurement_id"]))
        return {
            **source,
            "measurement_id": measurement_id,
            "identity_digest": rebound[1].payload["identity_digest"],
        }
    raise _PreconditionFailed("the look's measurement is not bound in this campaign")


def _bound_entry(context: _Evidence, measurement_id: str) -> Mapping[str, Any]:
    """A bound measurement's snapshot entry: its side, Candidate and selector.

    G2b3a: the lifecycle keeps a family-12 measurement's `role` and
    `case_subset` on its entry (a `measurement_rebound` copies its source's),
    computed from the selection, the round's gate scope and the identity in
    force -- inputs the audit already re-derives -- so the audit reads the
    selector there rather than from any record (G2 spec §3.5). An entry is
    complete once its creation record is admitted and never changes after,
    so one reduction up to the latest creation record serves every
    measurement. Families 5-11 keep neither key, so their entry is the
    durable binding itself and no reduction is replayed.

    G2b4: a completion's entry is fixed by its `completion_bound` alone (the
    champion side over the whole corpus, no selector), so it is read from
    that record, and the reduction ends at the latest *other* creation
    record, where it ended before the completion existed: a completion
    follows its round's looks and close, which no other entry depends on.
    A completion re-bound by a `continue` is created by a
    `measurement_rebound`, an ordinary creation record: the reduction runs
    up to it, and that prefix still holds the earlier `completion_bound` the
    re-bound entry copies its side, role and case set from.
    """

    if context.family_version == 15:
        entry15 = context.snapshot_before(len(context.records)).measurements.get(measurement_id)
        if entry15 is None:
            raise _PreconditionFailed("measurement is not bound in this campaign")
        return entry15
    entries = context._entries.get("all")
    if entries is None:
        created = [
            index
            for index, record in enumerate(context.records)
            if record.event in _MEASUREMENT_CREATIONS
        ]
        end = created[-1] + 1 if created else 0
        snapshot = reduce(
            context.records[:end], selector=None, family_version=context.family_version
        )
        entries = snapshot.measurements
        context._entries["all"] = entries
    entry = entries.get(measurement_id)
    if entry is None:
        raise _PreconditionFailed("the look's measurement is not bound in this campaign")
    return entry


def _derive_bound_identity(context: _Evidence, measurement_id: str) -> RunIdentityV1:
    """The identity a bound measurement derives from its epoch, side and selector.

    G2 spec §5: every subset measurement's identity is re-derived from the
    campaign identity and its `case_subset`. Without one (families 5-11, a
    full-corpus family-12 measurement) it is the full-corpus derivation on the
    measurement's creation side, byte for byte today's.

    Family-13 design §5 "Audit": a fresh Parent's entry also carries the
    `case_repeats` the lifecycle derived from the selection, the exam and the
    policy in force, and only under the four conditions. The derivation
    passes it, so a bound digest or a journal identity that carries a selector
    the entry lacks, or lacks one the entry carries, is a binding mismatch.
    """

    entry = _bound_entry(context, measurement_id)
    subset = entry.get("case_subset")
    identity = derived_identity(
        context.identity_for(measurement_id),
        str(entry["candidate_digest"]),
        side_of_entry(entry),  # type: ignore[arg-type]
        case_subset=None if subset is None else tuple(subset),
        case_repeats=entry.get("case_repeats"),
    )
    if context.family_version == 15 and entry.get("role") in {
        "family15_final",
        "family15_champion",
    }:
        from evolution.tree.campaign_identity import fresh_final_identity

        return fresh_final_identity(
            identity, measurement_id, context.policy_for(measurement_id).repeats
        )
    return identity


def _instance_ids_of(records: Sequence[Envelope]) -> dict[tuple[str, str], str]:
    """Each sample's `instance_id`, from one prefix's own `target_open_started` records.

    Built once per prefix so `_check_look_evaluated`'s row loop looks a sample
    up rather than re-scanning the whole prefix for every row.
    """

    instances: dict[tuple[str, str], str] = {}
    for record in records:
        if record.event == "target_open_started":
            key = (str(record.payload.get("case_id")), str(record.payload.get("sample_id")))
            instances[key] = str(record.payload["instance_id"])
    return instances


def _attested_boot_ids(context: _Evidence) -> dict[tuple[str, str], str]:
    """Every campaign `instance_attested`'s boot id, keyed by `(measurement_id, instance_id)`.

    Built once per campaign, not once per sample row: a `(measurement_id,
    instance_id)` pair missing from this map is exactly a sample whose
    instance the campaign never attested — the executor's own `""` value for
    "another process's" instance. Not part of the exact-pairing proof, so it
    cannot change a verdict.
    """

    boots: dict[tuple[str, str], str] = {}
    for record in context.records:
        if record.event == "instance_attested":
            key = (str(record.payload["measurement_id"]), str(record.payload["instance_id"]))
            boots[key] = str(record.payload["boot_id"])
    return boots


def _bind_cited(
    context: _Evidence, references: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, tuple[tuple[Envelope, ...], Lifecycle]], dict[str, RunIdentityV1]]:
    """Step 0's per-measurement binding of a look's cited prefixes (spec §5 step 0).

    Three ways, runtime-free, unconditional, on each measurement's own
    creation side (and, in family 12, its own selector): the derived identity,
    the bound `identity_digest`, and the identity the cited prefix's own
    `run_created` carries. Returns each cited prefix with its lifecycle, and
    each derived identity.
    """

    prefixes: dict[str, tuple[tuple[Envelope, ...], Lifecycle]] = {}
    derived: dict[str, RunIdentityV1] = {}
    for measurement_id, reference in references.items():
        bound = _measurement_binding(context, measurement_id)
        expected_identity = _derive_bound_identity(context, measurement_id)
        if expected_identity.digest != bound["identity_digest"]:
            raise _PreconditionFailed("the bound identity is not the derived one")
        records, state = context.measurement_state(reference)
        try:
            journaled = RunIdentityV1.from_payload(records[0].payload["identity"])
        except (ValueError, TypeError, KeyError):
            raise _PreconditionFailed("the cited prefix is not the bound measurement") from None
        if journaled.digest != expected_identity.digest:
            raise _PreconditionFailed("the cited prefix is not the bound measurement")
        prefixes[measurement_id] = (records, state)
        derived[measurement_id] = expected_identity
    return prefixes, derived


def _gate_rows(
    context: _Evidence,
    prefixes: Mapping[str, tuple[tuple[Envelope, ...], Lifecycle]],
    derived: Mapping[str, RunIdentityV1],
    look: int,
    *,
    groups: Mapping[str, tuple[str, str]],
    seed_mode: str,
) -> dict[str, tuple[SampleRow, ...]]:
    """Each cited measurement's gate rows to `look`, built as the executor builds them.

    The latest try, numbered and bounded as the executor numbers it: the
    shared `latest_rows` (audit item 1), over the derived schedule, with each
    sample's attested boot id.
    """

    boot_ids = _attested_boot_ids(context)
    rows: dict[str, tuple[SampleRow, ...]] = {}
    for measurement_id, (records, state) in prefixes.items():
        run_identity = derived[measurement_id].to_payload()
        instances = _instance_ids_of(records)
        for entry in run_identity["schedule"]:
            if int(entry["repeat"]) <= look and str(entry["case_id"]) not in groups:
                raise _PreconditionFailed("a scheduled case is not in the campaign's corpus")

        def boot_of(
            key: tuple[str, str],
            measurement_id: str = measurement_id,
            instances: Mapping[tuple[str, str], str] = instances,
        ) -> str:
            instance_id = instances.get(key)
            return "" if instance_id is None else boot_ids.get((measurement_id, instance_id), "")

        rows[measurement_id] = latest_rows(
            records,
            state,
            schedule=run_identity["schedule"],
            look=look,
            seed_mode=seed_mode,
            dependency_digest=str(run_identity["target"]["dependency_digest"]),
            boot_id=boot_of,
        )
    return rows


def _check_comparison_bound(context: _Evidence, record: Envelope) -> None:
    """Family 13's `comparison_bound` (design §6.2), what needs journal bytes.

    The evidence is exactly the Attempt's `affected_cases_selected` and, when
    `exclusions` is set, the round baseline's `pairs_excluded`, whose count,
    digest and prefix the record repeats; `retained` then follows those
    exclusions (G6, `retained_cases`). Order and every policy-derived value are
    the lifecycle's, which this audit replays.
    """

    payload = record.payload
    attempt_id = str(payload["attempt_id"])
    family = campaign_family_for(context.family_version)
    index = context.records.index(record)
    selection = next(
        (
            earlier
            for earlier in reversed(context.records[:index])
            if earlier.event == "affected_cases_selected"
            and str(earlier.payload["attempt"]) == attempt_id
        ),
        None,
    )
    if selection is None:
        raise _PreconditionFailed("the comparison names no selected Attempt")
    references = payload["evidence"]
    first = references[0]
    if (
        first.get("kind") != "journal_record"
        or context.file_of(first) != CAMPAIGN_FILE
        or context.one_record(first, family).to_payload() != selection.to_payload()
    ):
        raise _PreconditionFailed("the comparison does not cite its Attempt's selection")
    exclusions = payload["exclusions"]
    if exclusions is None:
        if len(references) != 1:
            raise _PreconditionFailed("the comparison cites more than its selection")
        return
    if len(references) != 2:
        raise _PreconditionFailed("the comparison does not cite the baseline's exclusions")
    second = references[1]
    if second.get("kind") != "journal_record" or context.file_of(second) != CAMPAIGN_FILE:
        raise _PreconditionFailed("the comparison does not cite the baseline's exclusions")
    excluded_record = context.one_record(second, family)
    baseline = str(payload["baseline_measurement_id"])
    if (
        excluded_record.event != "pairs_excluded"
        or str(excluded_record.payload["measurement_id"]) != baseline
    ):
        raise _PreconditionFailed("the comparison does not cite the baseline's exclusions")
    summary = excluded_record.payload
    cited_prefix = summary["evidence"][0]
    if (summary["count"], summary["pairs_digest"], cited_prefix["locator"]) != (
        exclusions["count"],
        exclusions["pairs_digest"],
        exclusions["prefix"],
    ):
        raise _PreconditionFailed("the exclusions are not the cited record's")
    records, state = context.measurement_state(cited_prefix)
    repeats = int(payload["repeats"])
    excluded = _baseline_exclusion(context, record, baseline, records, state, repeats)
    corpus_ids = [
        str(case["case_id"])
        for case in context.identity_for(str(payload["candidate_measurement_id"])).corpus_cases
    ]
    schedule = [
        (str(row["case_id"]), int(row["repeat"]))
        for row in records[0].payload["identity"]["schedule"]
    ]
    expected = {
        name: retained_cases(payload[name], corpus_ids, schedule, excluded, repeats)
        for name in ("focus", "causal")
    }
    if dict(payload["retained"]) != expected:
        raise _PreconditionFailed("the retained cases do not follow the cited exclusions")


def _family13_attempt(context: _Evidence, record: Envelope) -> AttemptState:
    """The Attempt a family-13 record names, as the campaign prefix before it has it."""

    attempt_id = str(record.payload["attempt_id"])
    snapshot = context.snapshot_before(context.records.index(record))
    for state in snapshot.rounds.values():
        if attempt_id in state.attempts:
            return state.attempts[attempt_id]
    raise _PreconditionFailed("the record names no attempt of this campaign")


def _journal_head(
    context: _Evidence, locator: object, measurement_id: str, what: str
) -> dict[str, Any]:
    """A payload `prefix` (no digest of its own) as a reference to a head of
    `measurement_id`'s journal: its file, from byte 0, within the journal,
    ending on a record boundary (design §6.1 "Prefixes")."""

    match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    if match is None or match["file"] != name or int(match["start"]) != 0:
        raise _PreconditionFailed(f"{what} is not a prefix of {measurement_id}'s journal")
    if context.family_version == 14:
        # Final facts depend only on the cited bytes. A later append is checked
        # separately, never allowed to change the rows of this numerical read.
        if name not in context.cache:
            path = context.run_root / name
            if not path.is_file():
                raise _PreconditionFailed("the measurement journal is missing")
            context.cache[name] = path.read_bytes()
    else:
        context.measurement_journal(measurement_id)
    if int(match["end"]) > len(context.cache[name]):
        raise _PreconditionFailed(f"{what} runs past {measurement_id}'s journal")
    reference = {"kind": "journal_prefix", "locator": str(locator)}
    try:
        context.measurement_state(reference)
    except _PreconditionFailed:
        raise _PreconditionFailed(f"{what} is not a journal head of {measurement_id}") from None
    return reference


def gate_inputs(
    comparison: Mapping[str, Any],
    *,
    max_repeat: int,
    look: int,
    excluded: Collection[tuple[str, int]],
) -> GateInputs:
    """`confidence_gate.gate_inputs`, its refusal a failed precondition (plan F10)."""

    try:
        return _gate_inputs(comparison, max_repeat=max_repeat, look=look, excluded=excluded)
    except GateInvariant as exc:
        raise _PreconditionFailed(str(exc)) from None


def _comparison_excluded(
    context: _Evidence, record: Envelope, comparison: Mapping[str, Any], look: int
) -> frozenset[tuple[str, int]]:
    """The executor's `excluded_pairs(baseline, look)` for a family-13 comparison.

    The baseline settled before the comparison was bound, so its exclusions
    are read from the prefix its `pairs_excluded` cites, which the comparison
    repeats as `exclusions.prefix` (`_check_comparison_bound` holds the two
    equal); nothing when the comparison names no exclusions.
    """

    exclusions = comparison["exclusions"]
    if exclusions is None:
        return frozenset()
    baseline = str(comparison["baseline_measurement_id"])
    reference = _journal_head(context, exclusions["prefix"], baseline, "the exclusions' prefix")
    return _baseline_exclusion(
        context, record, baseline, *context.measurement_state(reference), look
    )


def _row_facts(rows: Sequence[SampleRow], primary: str) -> list[list[Any]]:
    """A cache key's view of gate rows: what a draw reads of each.

    It omits `requested_seed`, which the pairing proof (`pairing_of`) reads
    too. The key is still safe: the cache key also holds the cited prefix
    locators, which pin the rows' bytes, `requested_seed` among them, so two
    inputs with one key cannot differ in it.
    """

    return [
        [
            row.case_id,
            row.sample_id,
            row.repeat,
            None if row.metrics is None else row.metrics.get(primary),
            row.disposition,
            row.critical_pass,
            row.effective_seed,
            row.boot_id,
            row.dependency_digest,
        ]
        for row in rows
    ]


def _named(rows: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(f"{row['case_id']}/{row['sample_id']}" for row in rows[:3])


def _check_noise_calibration_bound(context: _Evidence, record: Envelope) -> None:
    """Family 13's `noise_calibration_bound` (design §6.3), what needs journal bytes.

    A baseline source reads the settled prefix: the one its round's
    `baseline_complete` cites, which must read back as a head of the source
    measurement's journal. A fresh Parent's prefix is cited at completion.
    Order, source and sizes are the lifecycle's, which this audit replays.
    """

    payload = record.payload
    if payload["source"] != "baseline":
        return
    source = str(payload["source_measurement_id"])
    index = context.records.index(record)
    settled = next(
        (
            earlier
            for earlier in reversed(context.records[:index])
            if earlier.event == "baseline_complete"
            and int(earlier.payload["round"]) == int(payload["round"])
        ),
        None,
    )
    if settled is None or str(settled.payload["measurement_id"]) != source:
        raise _PreconditionFailed("the calibration's baseline has no baseline_complete")
    if _only_reference(settled)["locator"] != payload["prefix"]:
        raise _PreconditionFailed(
            "the calibration's prefix is not the one its round's baseline_complete cites"
        )
    _journal_head(context, payload["prefix"], source, "the calibration's prefix")


def _refuse_unadmitted_stage(
    context: _Evidence,
    record: Envelope,
    sides: Sequence[str],
    look: int,
    prefixes: Mapping[str, tuple[tuple[Envelope, ...], Lifecycle]],
    what: str,
) -> None:
    """Family 13 (codex branch round 1, ADR-0027): the executor's stage
    admission (`_look_prelude`), re-derived before a look or a calibration is
    recomputed. A record computed over a stage with fault rows -- the executor
    suspends on them -- or with rows that hold the Attempt is refused.

    `sides` are the Attempt's scheduled measurements, the Candidate's then a
    fresh Parent's, each read at its cited prefix in `prefixes` or, when the
    record cites none, as it stood at the record (`measurement_at`). The rule
    is the shared `stage_admission`, with the range rule in force there and
    the held rule of the producing version before the record. Only
    family-13 checks call this; families 3-12 audit as before (ADR-0008).
    """

    index = context.records.index(record)
    read = [prefixes.get(mid) or context.measurement_at(mid, index) for mid in sides]
    policy = context.policy_for(sides[0])
    horizon = stage_horizon(
        policy.looks, look, max(_scheduled_max_repeat(records) for records, _state in read)
    )
    admission = stage_admission(
        [
            (latest_rows(records, state), context.primary_range(index, mid))
            for mid, (records, state) in zip(sides, read, strict=True)
        ],
        horizon=horizon,
        floor=stage_floor(policy.looks, look),
        primary=policy.metric_primary,
        lacking=held_repair_required(context.snapshot_before(index).producing_version),
    )
    if admission.faults:
        named = ", ".join(
            f"{sides[side]} {row.case_id}/{row.sample_id} {reason}"
            for side, row, reason in admission.faults[:3]
        )
        raise _PreconditionFailed(f"{what} was computed over a stage with fault rows: {named}")
    if admission.held:
        named = ", ".join(
            f"{sides[side]} {row.case_id}/{row.sample_id}" for side, row in admission.held[:3]
        )
        raise _PreconditionFailed(f"{what} was computed over a stage with held rows: {named}")


def _check_noise_calibration_completed(context: _Evidence, record: Envelope) -> None:
    """Family 13's `noise_calibration_completed`: D0 recomputed (design §6.4, ADR-0027).

    The cited prefix is the bound source's journal head, bound three ways
    (`_bind_cited`); a fresh Parent's must complete its final stage to the
    horizon. Then `calibration_record` -- the executor's own producer -- over
    the prefix's rows, the baseline's exclusions at R and the derived seed,
    at `DRAWS`, must give the recorded `allowance`, `degenerate`,
    `unavailable` and `result_digest`.
    """

    payload = record.payload
    attempt = _family13_attempt(context, record)
    comparison, calibration = attempt.comparison, attempt.calibration
    if comparison is None or calibration is None or payload["binding"] != calibration["seq"]:
        raise _PreconditionFailed("the calibration names no governing binding")
    source = str(calibration["source_measurement_id"])
    fresh = calibration["source"] == "fresh_parent"
    prefix = str(payload["prefix"])
    if not fresh and prefix != calibration["prefix"]:
        raise _PreconditionFailed("the calibration's prefix is not its bound prefix")
    reference = _journal_head(context, prefix, source, "the calibration's prefix")
    prefixes, derived = _bind_cited(context, {source: reference})
    repeats = int(comparison["repeats"])
    looks = [int(look) for look in comparison["looks"]]
    horizon = (
        gate_inputs(
            comparison,
            max_repeat=_scheduled_max_repeat(prefixes[source][0]),
            look=max(looks),
            excluded=(),
        ).horizon
        if fresh
        else repeats
    )
    identity = context.identity_for(source)
    policy = context.policy_for(source)
    corpus_ids = [str(case["case_id"]) for case in identity.corpus_cases]
    groups = case_groups(identity.corpus_cases)
    excluded = _comparison_excluded(context, record, comparison, repeats)
    seed_mode = str(identity.to_payload()["target"]["seed_mode"])
    rows = _gate_rows(context, prefixes, derived, horizon, groups=groups, seed_mode=seed_mode)
    if fresh:
        # G10: a fresh source completes after the Parent's final stage, read
        # to its horizon (a `case_repeats` Parent's second observation).
        short = confidence_look_missing(
            comparison,
            {source: rows[source]},
            corpus_ids=corpus_ids,
            excluded=excluded,
            horizon=horizon,
        )
        if short:
            raise _PreconditionFailed(
                f"the calibration's prefix is short of the Parent's final stage: {_named(short)}"
            )
        # `calibrate` runs the look prelude's fault and
        # held checks over both scheduled sides before it reads the Parent.
        _refuse_unadmitted_stage(
            context,
            record,
            [str(comparison["candidate_measurement_id"]), source],
            max(looks),
            {source: prefixes[source]},
            "the calibration",
        )
    if not context.recompute:
        return
    seed = derive_seed(
        campaign_seed=policy.random_seed,
        binding=str(calibration["digest"]),
        scope="noise",
        prefixes=[prefix],
        look=None,
        purpose="noise",
    )
    inputs = {
        "binding": calibration["digest"],
        "comparison": comparison["digest"],
        "prefixes": [prefix],
        "horizon": horizon,
        "excluded": sorted(excluded),
        "rows": _row_facts(rows[source], policy.metric_primary),
    }
    try:
        expected = context.computed(
            "noise",
            seed,
            inputs,
            lambda: calibration_record(
                comparison,
                calibration,
                parent_rows=rows[source],
                excluded=excluded,
                groups=groups,
                corpus_ids=corpus_ids,
                primary=policy.metric_primary,
                horizon=horizon,
                prefix=prefix,
                campaign_seed=policy.random_seed,
                draws=DRAWS,
            ),
        )
    except GateInvariant as exc:
        raise _PreconditionFailed(str(exc)) from None
    if isinstance(expected, MissingMetric):
        raise _PreconditionFailed(
            f"{expected.case_id}/{expected.sample_id} has no primary metric to calibrate"
        )
    named = ("allowance", "degenerate", "unavailable", "result_digest")
    for name in (*named, *sorted(set(expected) - set(named))):
        if payload.get(name) != expected[name]:
            raise _PreconditionFailed(
                f"{name} is {expected[name]!r}, the record says {payload.get(name)!r}"
            )


#: Design §6.5: the published fields of one estimate.
_ESTIMATE_FIELDS = ("point", "lower", "upper", "level", "unavailable")


def _check_comparison_look_evaluated(context: _Evidence, record: Envelope) -> None:
    """Family 13's look (design §6.5, ADR-0027; plan choices 1, 4, 14).

    The evidence is exactly the governing comparison's Candidate prefix and its
    Parent-side prefix (the fresh Parent's, or the baseline's), each the bound
    measurement's own journal three ways (`_bind_cited`). Then, in the
    executor's order: the cited prefixes complete the look's stage to its
    horizon on the Candidate and a fresh Parent (`look_missing`; a baseline
    is complete by its own record); the critical veto is the Candidate's
    rows' (a recorded veto needs a vetoing row, any other decision none);
    and `look_record` -- the executor's own producer -- over the same rows,
    seeds and prefixes, at `DRAWS`, must reproduce every estimate field,
    the tolerance, decision, reason, `would_pass_point` and caveats.
    """

    payload = record.payload
    attempt = _family13_attempt(context, record)
    comparison = attempt.comparison
    if comparison is None or comparison["seq"] != payload["comparison"]:
        raise _PreconditionFailed("the look names no governing comparison")
    candidate = str(comparison["candidate_measurement_id"])
    fresh = comparison["parent_measurement_id"] is not None
    parent_side = str(comparison["parent_measurement_id"] or comparison["baseline_measurement_id"])
    cited = _cited_measurements(context, payload["evidence"])
    if cited != [candidate, parent_side]:
        raise _PreconditionFailed("the evidence is not the comparison's two prefixes")
    prefixes, derived = _bind_cited(context, dict(zip(cited, payload["evidence"], strict=True)))
    look = int(payload["look"])
    looks = [int(entry) for entry in comparison["looks"]]
    owing = [candidate, parent_side] if fresh else [candidate]
    bound = gate_inputs(
        comparison,
        max_repeat=max(_scheduled_max_repeat(prefixes[mid][0]) for mid in owing),
        look=look,
        excluded=_comparison_excluded(context, record, comparison, max(looks)),
    )
    horizon = bound.horizon
    excluded = bound.mask
    identity = context.identity_for(candidate)
    corpus_ids = [str(case["case_id"]) for case in identity.corpus_cases]
    short = confidence_look_missing(
        comparison,
        {
            mid: latest_rows(
                *prefixes[mid], schedule=derived[mid].to_payload()["schedule"], look=horizon
            )
            for mid in owing
        },
        corpus_ids=corpus_ids,
        excluded=excluded,
        horizon=horizon,
    )
    if short:
        raise _PreconditionFailed(
            f"the look's measurements are short of look {look}: {_named(short)}"
        )
    # The look prelude's fault and held checks.
    _refuse_unadmitted_stage(
        context, record, owing, look, {mid: prefixes[mid] for mid in owing}, "the look"
    )
    groups = case_groups(identity.corpus_cases)
    seed_mode = str(identity.to_payload()["target"]["seed_mode"])
    rows = _gate_rows(context, prefixes, derived, look, groups=groups, seed_mode=seed_mode)
    # Plan choice 4: the veto reads the Candidate's rows to the look, unexcluded.
    vetoed = critical_vetoed([row for row in rows[candidate] if row.repeat <= look])
    if payload["reason"] == "critical_failure" and not vetoed:
        raise _PreconditionFailed("a recorded critical veto has no critical Candidate row")
    if payload["reason"] != "critical_failure" and vetoed:
        raise _PreconditionFailed("a Candidate sample failed its critical check")
    if not context.recompute:
        return
    policy = context.policy_for(candidate)
    locators = {
        "candidate": str(payload["evidence"][0]["locator"]),
        "parent": str(payload["evidence"][1]["locator"]),
    }
    seed = derive_seed(
        campaign_seed=policy.random_seed,
        binding=str(comparison["digest"]),
        scope="joint" if look == max(looks) else "focus",
        prefixes=[locators["candidate"], locators["parent"]],
        look=look,
        purpose="gate",
    )
    inputs = {
        "family_version": context.family_version,
        "comparison": comparison["digest"],
        "calibration": attempt.calibration_result,
        "look": look,
        "prefixes": locators,
        "excluded": sorted(excluded),
        "rows": {
            side: _row_facts(rows[mid], policy.metric_primary)
            for side, mid in (("candidate", candidate), ("parent", parent_side))
        },
    }
    try:
        expected = context.computed(
            "gate",
            seed,
            inputs,
            lambda: look_record(
                comparison,
                attempt.calibration_result,
                look=look,
                candidate_rows=rows[candidate],
                parent_rows=rows[parent_side],
                excluded=excluded,
                groups=groups,
                corpus_ids=corpus_ids,
                primary=policy.metric_primary,
                prefixes=locators,
                campaign_seed=policy.random_seed,
                draws=DRAWS,
                family_version=context.family_version,
            ),
        )
    except (GateInvariant, CoverageMissing) as exc:
        # Plan F10: the pure module's invariant is a finding here, never raised.
        raise _PreconditionFailed(str(exc)) from None
    if isinstance(expected, MissingMetric):
        raise _PreconditionFailed(
            f"{expected.case_id}/{expected.sample_id} has no primary metric to compare"
        )
    for scope in ("focus", "causal", "overall"):
        recorded, derived_estimate = payload[scope], expected[scope]
        if (recorded is None) != (derived_estimate is None):
            raise _PreconditionFailed(f"{scope} nullability does not follow the comparison")
        if recorded is None:
            continue
        fields = (*_ESTIMATE_FIELDS, "exact") if (scope != "overall") else _ESTIMATE_FIELDS
        for name in fields:
            if recorded.get(name) != derived_estimate[name]:
                raise _PreconditionFailed(
                    f"{scope} {name} is {derived_estimate[name]!r}, "
                    f"the record says {recorded.get(name)!r}"
                )
    named = ("tolerance", "decision", "reason", "would_pass_point", "caveats")
    for name in (*named, *sorted(set(expected) - set(named) - {"focus", "causal", "overall"})):
        if payload.get(name) != expected[name]:
            raise _PreconditionFailed(
                f"{name} is {expected[name]!r}, the record says {payload.get(name)!r}"
            )


def _unterminated(
    records: Sequence[Envelope],
    snapshot: CampaignSnapshot,
    measurement_id: str,
    context: _Evidence,
) -> tuple[str, ...]:
    """Rows of a measurement's opened stages that have no S1 terminal at all.

    The barrier's own fact: a stage is *completed* when every row it scheduled
    reached a terminal, whatever its disposition — which is why this is not
    `_unscored`. The opened stages and their looks come from the campaign
    prefix; the terminals are read from the **cited** bytes, because what a
    barrier claims is that those stages had completed when it was written.
    """

    entry = snapshot.measurements.get(measurement_id)
    if entry is None:
        raise _PreconditionFailed("the cited measurement is not bound in this campaign")
    stages = entry["stages"]
    if not stages:
        return ()
    schedule = records[0].payload["identity"]["schedule"]
    # Family 13 (design §5): the latest stage holds its rows to its horizon.
    look = _last_stage_horizon(
        context, measurement_id, max(int(stage["look"]) for stage in stages), schedule
    )
    missing = []
    for row in schedule:
        if int(row["repeat"]) > look:
            continue
        if _final_disposition(records, str(row["case_id"]), str(row["sample_id"])) is None:
            missing.append(f"{row['case_id']}/{row['sample_id']}")
    return tuple(missing)


def _check_threshold_reached(context: _Evidence, record: Envelope) -> None:
    """`threshold_reached`: the stage(s) that made this barrier really completed.

    §7 puts a threshold at exactly two barriers, and both are defined by
    *completed stages* — a fact the campaign prefix cannot carry, which is why
    amendment 21 makes this an E record. The audit re-checks what the evidence
    names: the cited measurements belong to this round, every row of their
    opened stages reached a terminal, and each cited measurement's indeterminate
    samples for this owner are exactly the ones the prefix accounted to it.
    """

    payload = record.payload
    owner = str(payload["owner"])
    index = context.records.index(record)
    snapshot = reduce(context.records[:index], selector=None, family_version=context.family_version)
    round_number = int(payload["round"])
    cited = _cited_measurements(context, payload["evidence"])
    if len(set(cited)) != len(cited):
        raise _PreconditionFailed("the evidence cites a measurement twice")
    prefixes = dict(zip(cited, payload["evidence"], strict=True))
    state = snapshot.rounds.get(round_number)
    if state is None:
        raise _PreconditionFailed("the threshold names no round of this campaign")
    # A round's baseline is *reused*, not rebound (a stalled round keeps it, a
    # promotion makes the winner's own measurement the next one), so which round
    # a measurement belongs to is what `baseline_bound`/`measurement_bound` say
    # for *this* round -- never the round its entry was first bound at, which
    # reports an honest round-2 threshold as a forgery.
    baseline = str(state.baseline_measurement_id)
    attempts = {
        str(attempt.measurement_id)
        for attempt in state.attempts.values()
        if attempt.measurement_id is not None
    }
    if payload["barrier"] == "baseline_stage":
        if len(cited) != 1:
            raise _PreconditionFailed("a baseline-stage barrier is one completed stage")
        # The window decides which stage completed, so it decides the citation:
        # before the round's first attempt that is the baseline, and while a
        # promoted winner completes it is the winner's own measurement. The
        # lifecycle admits a `baseline_stage` barrier in exactly those two
        # windows, and each names one measurement.
        promoted = state.closed is not None and state.closed["outcome"] == "promoted"
        winner = state.selection["attempt_id"] if state.selection is not None else None
        expected = baseline
        if promoted and winner is not None:
            completion = context.measurement_of(str(winner), record)
            if completion is None:
                raise _PreconditionFailed("the promoted winner has no bound measurement")
            expected = completion
        if cited[0] != expected:
            raise _PreconditionFailed(
                "the evidence is not the round's baseline or winner-completion measurement"
            )
    elif set(cited) != {baseline} | attempts:
        raise _PreconditionFailed("the evidence is not the round's own measurements")
    for measurement_id in cited:
        records = context.measurement_records(prefixes[measurement_id])
        missing = _unterminated(records, snapshot, measurement_id, context)
        if missing:
            raise _PreconditionFailed(
                f"{measurement_id}'s opened stages are not complete: " + ", ".join(missing[:3])
            )
        held = _held_tries(records, context.measurement_state(prefixes[measurement_id])[1], owner)
        accounted = {
            _accounted_key(entry_row, True)
            for entry_row in _accounted_rows(context.records[:index], measurement_id, owner)
        }
        if held != accounted:
            raise _PreconditionFailed(
                f"{measurement_id}'s {owner}-owned indeterminates are not the accounted ones"
            )


def _held_tries(records: Sequence[Envelope], state: Lifecycle, owner: str) -> set[tuple[str, ...]]:
    """`(case_id, sample_id, retry_ordinal)` of every try of `owner`'s indeterminates."""

    identity = records[0].payload["identity"]
    retries = int(identity.get("sample_retries") or 0)
    repairs = _repair_ordinals(records)
    held: set[tuple[str, ...]] = set()
    for entry in identity["schedule"]:
        key = (str(entry["case_id"]), str(entry["sample_id"]))
        # I1 spec §5.4: the sample's own bound, `sample_retries` plus the
        # repair tries it started, through the one shared expression.
        limit = ordinal_limit(retries, state.repairs.get(key, 0))
        for ordinal, (execution, scoring) in enumerate(_sample_tries(state, key)):
            row = sample_row(
                entry,
                execution,
                scoring,
                seed_mode="none",
                dependency_digest="",
                boot_id="",
                retry_ordinal=ordinal,
                limit=limit,
                repaired=(*key, ordinal) in repairs,
            )
            accounted = _owner_reason(row)
            if accounted is not None and accounted[0] == owner:
                held.add((*key, str(int(execution.payload.get("retry_ordinal", 0)))))
    return held


def _accounted_key(payload: Mapping[str, Any], per_try: bool) -> tuple[str, ...]:
    key = (str(payload["case_id"]), str(payload["sample_id"]))
    return (*key, str(int(payload["retry_ordinal"]))) if per_try else key


def _accounted_rows(
    records: Sequence[Envelope], measurement_id: str, owner: str
) -> list[Mapping[str, Any]]:
    """Every `indeterminate_observed` of one owner over one measurement.

    In family 6 a row is one try and carries its `retry_ordinal`; the threshold
    check compares them by `(case_id, sample_id, retry_ordinal)`.
    """

    return [
        entry.payload
        for entry in records
        if entry.event == "indeterminate_observed"
        and entry.payload["measurement_id"] == measurement_id
        and entry.payload["owner"] == owner
    ]


def _check_selection_decided(context: _Evidence, record: Envelope) -> None:
    """Verify the final selection's durable evidence after lifecycle replay."""

    _check_cohort_selection(context, record)


def _check_selection_evidence(context: _Evidence, record: Envelope) -> None:
    """A selection without a final binding cites the round's measurement prefixes.

    The lifecycle admits no winner without a final binding; selections with a
    binding use the ordered member-completion proof in `_check_cohort_selection`.
    """
    payload = record.payload
    snapshot = reduce(
        context.records[: context.records.index(record)],
        selector=None,
        family_version=context.family_version,
    )
    state = snapshot.rounds[int(payload["round"])]
    expected = {str(state.baseline_measurement_id)} | {
        str(attempt.measurement_id)
        for attempt in state.attempts.values()
        if attempt.measurement_id is not None
    }
    if set(_cited_measurements(context, payload["evidence"])) != expected:
        raise _PreconditionFailed("the evidence is not the round's own measurements")


def _check_cohort_selection(context: _Evidence, record: Envelope) -> None:
    """Family-13 design §6.11, D5: what the lifecycle cannot check (plan choice 2).

    The lifecycle re-derives every field during replay, with or without a
    selector. The audit checks the evidence: without a cohort, family 12's
    null evidence (`_check_selection_evidence`); with one, exactly every
    member's completion in binding/member order and then every finalist's
    comparison in Attempt order, and promotion admission over every finalist's
    two chains. Each historical binding resolves its own current members,
    including `<id>@e<k>` rebounds (F7 plan choice 9, family-14 R39).
    """

    payload = record.payload
    index = context.records.index(record)
    state = context.snapshot_before(index).rounds[int(payload["round"])]
    cohort = state.cohort
    if cohort is None:
        _check_selection_evidence(context, record)
        return
    members = list(current_members(state))
    finalists = selection_finalists(state)
    stable = context.policy_for(members[0]).stability == "stable"
    expected_mid = (
        state.baseline_measurement_id if stable else champion_member(cohort, members=members)
    )
    if payload["attempt_id"] is not None:
        expected_mid = next(
            member
            for finalist, member in finalists
            if finalist["attempt_id"] == payload["attempt_id"]
        )
    if payload["measurement_id"] != expected_mid:
        raise _PreconditionFailed("the selection names another next baseline measurement")
    # Which measurement the next round binds is checked by
    # `_refresh_findings`, in its single forward pass over the rounds.
    # Retention preserves the original baseline identity; Promotion uses the
    # fresh finalist's current (possibly rebound) measurement identity.
    if payload["attempt_id"] is None and stable:
        _stored_final_reference(
            context,
            state,
            {"measurement_id": expected_mid, "prefix_digest": state.baseline_prefix_digest},
            state.baseline_reference or {},
        )
    else:
        assert expected_mid is not None
        reference = _journal_head(
            context,
            state.cohort_completed[expected_mid]["prefix"],
            expected_mid,
            "the selected finalist's prefix",
        )
        _bind_cited(context, {expected_mid: reference})
    expected = [int(source["seq"]) for source in selection_records(state)]
    family = campaign_family_for(context.family_version)
    cited = [
        context.one_record(ref, family).to_payload()["sequence"]
        for ref in payload["evidence"]
        if ref.get("kind") == "journal_record" and context.file_of(ref) == CAMPAIGN_FILE
    ]
    if len(cited) != len(payload["evidence"]) or cited != expected:
        raise _PreconditionFailed(
            member_diagnostic(
                "the selection does not cite every completion and comparison of its cohort",
                family_version=context.family_version,
            )
        )
    # Design §7, plan choice 7: the selection was written only after every
    # finalist's gate chain and cohort chain passed promotion admission.
    for finalist, member in finalists:
        gate = context.measurement_of(str(finalist["attempt_id"]), record)
        if gate is None:
            raise _PreconditionFailed("a finalist has no bound gate measurement")
        for measurement_id in (gate, member):
            if not context.attested_rows(measurement_id):
                raise _PreconditionFailed(f"{measurement_id} attested no instance")
            verdict = context.attestation_verdict(measurement_id)
            if verdict is not None:
                raise _PreconditionFailed(f"promotion admission cannot have passed: {verdict}")


def _check_attempt_input_frozen(context: _Evidence, record: Envelope) -> None:
    """Authenticate retained feedback rows against their own frozen prefix.

    Family 14 cites each close followed by its settled losing comparison.
    Numerical recomputation is immaterial to these journal facts; each global
    source separately receives its row audit at its own prefix.
    """

    payload = record.payload
    state = context.snapshot_before(context.records.index(record)).rounds[int(payload["round"])]
    expected: list[int] = []
    for row in payload["feedback"]["attempts"]:
        attempt = state.attempts[str(row["attempt_id"])]
        reason, comparison = effective_attempt_feedback(
            state, attempt, family_version=context.family_version
        )
        if (row["index"], row["outcome"], row["reason"], row["assessed"]) != (
            attempt.index,
            attempt.outcome,
            reason,
            bool(
                attempt.looks
                or (
                    context.family_version == 15
                    and attempt.family15 is not None
                    and attempt.family15.comparison is not None
                )
            ),
        ):
            raise _PreconditionFailed("the frozen feedback differs from its closed Attempt")
        assert attempt.closed_seq is not None
        expected.append(attempt.closed_seq)
        if comparison is not None:
            expected.append(int(comparison["seq"]))
    family = campaign_family_for(context.family_version)
    cited = [
        context.one_record(ref, family).to_payload()["sequence"]
        for ref in payload["source_refs"]
        if ref.get("kind") == "journal_record" and context.file_of(ref) == CAMPAIGN_FILE
    ]
    if len(cited) != len(payload["source_refs"]) or cited != expected:
        raise _PreconditionFailed("the frozen feedback does not cite its closes and comparisons")


def _check_finalists_bound(context: _Evidence, record: Envelope) -> None:
    """Family 14's binding cites its finalists' successful Attempt closures.

    Policy, epoch, IDs, levels and member ordering are already re-derived by
    lifecycle admission. Verify the actual bytes behind its closure digests.
    """

    family = campaign_family_for(context.family_version)
    cited = [context.one_record(ref, family) for ref in record.payload["evidence"]]
    finalists = record.payload["finalists"]
    if len(cited) != len(finalists) or any(
        closed.event != "attempt_closed"
        or closed.payload["attempt_id"] != finalist["attempt_id"]
        or closed.payload["outcome"] != context.vocabulary.gate_pass_outcome
        for closed, finalist in zip(cited, finalists, strict=True)
    ):
        raise _PreconditionFailed("the binding does not cite its finalists' successful closures")


def _check_final_measurement_bound(context: _Evidence, record: Envelope) -> None:
    """Validate stored references even when numerical proof is disabled."""

    if record.payload["stability"] != "stable":
        return
    index = context.records.index(record)
    state = context.snapshot_before(index).rounds[int(record.payload["round"])]
    _stored_final_reference(
        context, state, record.payload["stored_champion"], _only_reference(record)
    )


def _stored_final_reference(
    context: _Evidence,
    state: RoundState,
    stored: Mapping[str, Any],
    reference: Mapping[str, Any],
) -> None:
    """Bind stored bytes to the baseline and the measurement's original epoch."""

    mid = str(stored["measurement_id"])
    if (
        mid != state.baseline_measurement_id
        or dict(reference) != dict(state.baseline_reference or {})
        or stored["prefix_digest"] != state.baseline_prefix_digest
    ):
        raise _PreconditionFailed("the stored reference is not the round's complete baseline")
    code = _verify_reference(context.run_root, dict(reference), context.cache)
    if code is not None:
        raise _PreconditionFailed("the stored baseline prefix cannot be verified", code)
    _journal_head(context, reference["locator"], mid, "the stored baseline prefix")
    _bind_cited(context, {mid: reference})


def _stored_final_excluded(
    context: _Evidence, record: Envelope, mid: str, rows: Sequence[SampleRow], repeats: int
) -> frozenset[tuple[str, int]]:
    """Check the full historical exhaustion summary before restricting it to R."""

    state = context.snapshot_before(context.records.index(record))
    exhausted = [row for row in rows if row.exhausted]
    journaled = dict(state.excluded.get(mid, {}))
    expected = (
        {}
        if not exhausted
        else {
            "count": len(exhausted),
            "digest": pairs_digest([(row.case_id, row.sample_id) for row in exhausted]),
        }
    )
    recorded = (
        {}
        if not journaled
        else {
            "count": sum(count for count, _digest in journaled.values()),
            "digest": next(iter(journaled.values()))[1] if len(journaled) == 1 else None,
        }
    )
    if recorded != expected:
        raise _PreconditionFailed("stored exclusions do not match the cited prefix")
    return frozenset((row.case_id, row.repeat) for row in exhausted if row.repeat <= repeats)


# -- family 14 (H7): the baseline refresh ------------------------------------


def _policy_in_force(snapshot: CampaignSnapshot) -> CampaignPolicyV11:
    """The policy of the latest condition epoch, as the lifecycle holds it there.

    A `rebaseline` may move the policy (I2 spec §6.2); `snapshot.identity` is
    the latest identity, whose policy the lifecycle's `policy` mirrors.
    """

    try:
        raw_policy = snapshot.identity["policy"]
        return policy_class_for_payload(raw_policy).from_payload(raw_policy)
    except (KeyError, TypeError, PolicyError):  # pragma: no cover - a created log has one
        raise _Unverifiable from None


def _family14_next_baseline_finding(
    before: CampaignSnapshot, number: int, selected: str, bound: str
) -> str | None:
    """F0 R43, PDR-0016: round `number`'s baseline after a family-14 selection.

    `before` is the reduced prefix before round `number`'s `baseline_bound`,
    which names `bound`. It names the selection's measurement `selected`,
    with two exceptions read from that prefix, never from the id's shape.
    When round `number` is the round a `rebaseline` starts, it names that
    epoch's fresh `m-rNN-champion@e<k>`; the rebaseline supersedes a refresh
    (H7 plan decision 7). Otherwise, when a `baseline_refresh_bound` for
    round `number` precedes it, it names the refresh measurement, and the
    refresh must replace exactly `selected`. Returns the failed rule, or
    `None` when it holds.
    """

    refresh = refresh_for(before, number)
    if before.rebaseline_from_round == number:
        expected = fresh_baseline_id(number, len(before.epochs) - 1)
    elif refresh is not None:
        if refresh["replaced"]["measurement_id"] != selected:
            return "the next round's refresh replaces another measurement than the selection's"
        expected = str(refresh["measurement_id"])
    else:
        expected = selected
    if bound != expected:
        return "the next round binds another baseline measurement"
    return None


def _check_baseline_refresh_bound(context: _Evidence, record: Envelope) -> None:
    """F0 R42-R43, ADR-0032 (amended 2026-10-09): the refresh its prefix owes.

    The prefix before the record is reduced and the due refresh re-derived
    with the policy in force there (`refresh_due_for`, the planner's and the
    lifecycle's own rule). N, the counted range and the replaced measurement
    must be exactly that refresh. `evidence[0]` is the replaced baseline's
    complete prefix: the previous round's `baseline_reference`, with the same
    content digest, resolved and bound by H5's stored-reference check.
    `evidence[1]` is the `round_closed` of the last counted round. The
    lifecycle leaves that citation to the audit.
    """

    payload = record.payload
    index = context.records.index(record)
    snapshot = context.snapshot_before(index)
    number = int(payload["round"])
    policy = _policy_in_force(snapshot)
    if not getattr(policy, "baseline_refresh_after_rounds", None):
        raise _PreconditionFailed("the policy in force refreshes no baseline")
    due = refresh_due_for(snapshot, policy, number)
    if due is None:
        raise _PreconditionFailed("no refresh is due for the round at its prefix")
    if payload["baseline_refresh_after_rounds"] != due.after_rounds:
        raise _PreconditionFailed(
            "the refresh names another baseline_refresh_after_rounds than the policy in force"
        )
    if dict(payload["counted_rounds"]) != {"first": due.first, "last": due.last}:
        raise _PreconditionFailed(
            "the refresh's counted rounds are not the count re-derived at its prefix"
        )
    replaced = payload["replaced"]
    if replaced["measurement_id"] != due.replaced:
        raise _PreconditionFailed(
            "the refresh replaces another measurement than the derived baseline"
        )
    _stored_final_reference(context, snapshot.rounds[number - 1], replaced, payload["evidence"][0])
    closure = next(
        (
            entry
            for entry in context.records[:index]
            if entry.event == "round_closed" and entry.payload["round"] == due.last
        ),
        None,
    )
    reference = payload["evidence"][1]
    cited = (
        context.one_record(reference, campaign_family_for(context.family_version))
        if reference.get("kind") == "journal_record" and context.file_of(reference) == CAMPAIGN_FILE
        else None
    )
    if (
        closure is None
        or cited is None
        or cited.to_payload()["sequence"] != closure.to_payload()["sequence"]
    ):
        raise _PreconditionFailed(
            "the refresh does not cite the round_closed of its last counted round"
        )


def _check_baseline_refresh_completed(context: _Evidence, record: Envelope) -> None:
    """F0 R43: the refresh closes on its round's complete refresh baseline.

    Over the reduced prefix the round has an open refresh, and the record
    names the round's current baseline, whose base id is the refresh
    measurement (`m-rNN-refresh`, or its `@e<k>` rebind after a `continue`).
    Its one prefix is exactly the one cited by that baseline's
    `baseline_complete`, which therefore precedes it.
    """

    payload = record.payload
    index = context.records.index(record)
    snapshot = context.snapshot_before(index)
    number = int(payload["round"])
    entry = refresh_for(snapshot, number)
    state = snapshot.rounds.get(number)
    if entry is None or entry["completed"] is not None or state is None:
        raise _PreconditionFailed("the round has no open refresh at its prefix")
    current = state.baseline_measurement_id
    if (
        current is None
        or payload["measurement_id"] != current
        or base_measurement_id(current) != entry["measurement_id"]
    ):
        raise _PreconditionFailed(
            "the refresh completion names another measurement than the round's refresh baseline"
        )
    complete = next(
        (
            earlier
            for earlier in reversed(context.records[:index])
            if earlier.event == "baseline_complete"
            and earlier.payload["round"] == number
            and earlier.payload["measurement_id"] == current
        ),
        None,
    )
    if complete is None:
        raise _PreconditionFailed("the refresh completion precedes its round's baseline_complete")
    if _only_reference(record) != _only_reference(complete):
        raise _PreconditionFailed(
            "the refresh completion does not cite its round's baseline_complete prefix"
        )


def _refresh_findings(context: _Evidence) -> list[dict[str, Any]]:
    """F0 R42-R43, ADR-0032 (amended 2026-10-09): the refresh count after every round.

    Families 14 and 15. For every round with a `baseline_bound`, the count is
    re-derived over the reduced prefix before the round's first
    baseline-start record (its `baseline_refresh_bound`, else its
    `baseline_bound`), with the policy in force there:

    - N = 0 journals no refresh record;
    - a refresh is due exactly when a `baseline_refresh_bound` for the round
      precedes its `baseline_bound` (at the round a `rebaseline` starts the
      derived count is 0, so a refresh is not due there);
    - a completed refresh precedes the round's grouped point: its first
      `grouping_started` or `focus_portion_selected`, whichever comes first
      (the no-failing path selects a `reason="none"` portion without
      grouping; the lifecycle's `_check_refresh_completed` gates both);
    - a refresh is left uncompleted only when a `rebaseline` superseded it
      (the round is the `rebaseline` round of its `baseline_bound`'s prefix),
      or the round reached no grouped point and did not close with an
      outcome other than `condition_changed`: it closed `condition_changed`,
      or the journal ends or finishes before that point.

    Over the reduced prefix before the same `baseline_bound` it also checks,
    after a family-14 cohort selection in the previous round, which
    measurement the round binds (`_family14_next_baseline_finding`). That
    finding is reported on the selection, located at its first evidence
    reference, as its own precondition check reported it before (H7 Task 6
    fix round 1). This costs one extra full replay per family-14 audit: the
    pass restarts from prefix 0 after the per-record loop. Within the pass
    the replay only moves forward, and there is no replay per selection.

    The per-record checks own each record's values and citations. The
    public count is H12's.
    """

    if not context.records:
        return []
    bound: dict[int, int] = {}
    refreshed: dict[int, int] = {}
    completed: dict[int, int] = {}
    grouped: dict[int, int] = {}
    selections: dict[int, int] = {}
    closed: dict[int, str] = {}
    for at, record in enumerate(context.records):
        table = {
            "baseline_bound": bound,
            "baseline_refresh_bound": refreshed,
            "baseline_refresh_completed": completed,
            "grouping_started": grouped,
            "focus_portion_selected": grouped,
            "selection_decided": selections,
        }.get(record.event)
        if table is not None:
            table.setdefault(int(record.payload["round"]), at)
        elif record.event == "round_closed":
            closed.setdefault(int(record.payload["round"]), str(record.payload["outcome"]))
    findings: list[dict[str, Any]] = []

    def report(at: int, detail: str) -> None:
        findings.append(_campaign_finding(context, at, "precondition_failed", detail))

    for number, at in sorted(bound.items()):
        refresh = refreshed.get(number)
        start = at if refresh is None else min(at, refresh)
        snapshot = context.snapshot_before(start)
        try:
            policy = _policy_in_force(snapshot)
        except _Unverifiable:  # pragma: no cover - a created log has one
            continue
        if refresh is not None and not getattr(policy, "baseline_refresh_after_rounds", None):
            report(
                refresh, f"the policy refreshes no baseline, yet round {number} journals a refresh"
            )
            continue
        due = refresh_due_for(snapshot, policy, number)
        # The previous round is closed at `start`; its cohort and selection
        # cannot change before `at`. The prefix before `at` is reduced only
        # when it is read, and always after `start`, so the replay moves forward.
        previous = snapshot.rounds.get(number - 1)
        selected = None if previous is None or previous.cohort is None else previous.selection
        if selected is not None and number - 1 in selections:
            rule = _family14_next_baseline_finding(
                context.snapshot_before(at),
                number,
                str(selected["measurement_id"]),
                str(context.records[at].payload["measurement_id"]),
            )
            if rule is not None:
                index = selections[number - 1]
                finding = _campaign_finding(context, index, "precondition_failed", rule)
                evidence = context.records[index].payload.get("evidence")
                if evidence:
                    finding["locator"] = evidence[0]["locator"]
                findings.append(finding)
        if due is not None and (refresh is None or refresh > at):
            report(at, f"round {number} owes a refresh before its baseline")
            continue
        if refresh is None:
            continue
        if due is None:
            report(refresh, f"round {number}'s refresh is not due")
            continue
        done = completed.get(number)
        group = grouped.get(number)
        if done is not None:
            if group is not None and done > group:
                report(
                    done,
                    f"round {number}'s refresh completes after its round grouped "
                    "or selected its portion",
                )
            continue
        if context.snapshot_before(at).rebaseline_from_round == number:
            continue  # Superseded: the round bound the rebaseline's fresh id.
        if group is not None:
            report(
                refresh,
                f"round {number}'s refresh is left uncompleted while its round grouped "
                "or selected its portion",
            )
        elif closed.get(number, "condition_changed") != "condition_changed":
            report(refresh, f"round {number}'s refresh is left uncompleted at its round's close")
    return findings


def _check_round_closed(context: _Evidence, record: Envelope) -> None:
    """`round_closed(promoted)`: S2 committed this champion for this round (§8).

    Family 9's `unplanned` close is re-derived instead: it cites the round's
    focus portion and its settled reflection terminal, and its reason is
    `editor_input_reason` of that terminal (spec §14, ADR-0022).
    """

    payload = record.payload
    if payload["outcome"] == "unplanned":
        _check_unplanned_close(context, record)
        return
    if payload["outcome"] != "promoted":
        return
    reference = _only_reference(record)
    if reference.get("kind") != "ledger_record":
        raise _PreconditionFailed("the evidence is not a ledger record")
    committed = context.one_record(reference, ARTIFACT_FAMILY)
    if committed.event != "promotion_committed":
        raise _PreconditionFailed("the evidence is not S2's promotion commit")
    if int(committed.payload["round_id"]) != int(payload["round"]):
        raise _PreconditionFailed("the commit belongs to another round")
    if (
        committed.payload["version_id"] != payload["champion_version"]
        or committed.payload["candidate_digest"] != payload["champion_digest"]
    ):
        raise _PreconditionFailed("the commit names another champion")


def _check_unplanned_close(context: _Evidence, record: Envelope) -> None:
    """`round_closed(unplanned)`: the portion and the reflection it read, and its reason."""

    payload = record.payload
    evidence = payload["evidence"]
    if len(evidence) != 2 or any(
        reference.get("kind") != "journal_record" or context.file_of(reference) != CAMPAIGN_FILE
        for reference in evidence
    ):
        raise _PreconditionFailed("the unplanned close does not cite two campaign records")
    family = campaign_family_for(context.family_version)
    portion = context.one_record(evidence[0], family)
    terminal = context.one_record(evidence[1], family)
    index = context.records.index(record)
    snapshot = reduce(context.records[:index], selector=None, family_version=context.family_version)
    state = snapshot.rounds.get(int(payload["round"]))
    diagnosis = None if state is None else state.diagnosis(True)
    if (
        state is None
        or state.focus_portion is None
        or portion.event != "focus_portion_selected"
        or portion.to_payload()["sequence"] != state.focus_portion["seq"]
        or portion.payload["round"] != payload["round"]
    ):
        raise _PreconditionFailed("the unplanned close does not cite the round's focus portion")
    if (
        diagnosis is None
        or terminal.event not in ("reflection_finished", "reflection_failed")
        or terminal.to_payload()["sequence"] != diagnosis["seq"]
        or terminal.payload["round"] != payload["round"]
        or terminal.payload["reflection_id"] != diagnosis["reflection_id"]
    ):
        raise _PreconditionFailed("the unplanned close does not cite the round's diagnosis")
    # The reason from the cited bytes alone, the reduced entry's own shape.
    entry = {
        "status": terminal.payload.get("status", "failed"),
        "groups": terminal.payload.get("groups", []),
    }
    if editor_input_reason(entry) != payload["reason"]:
        raise _PreconditionFailed("the unplanned reason is not the cited reflection's")
    if terminal.event == "reflection_failed" and _named_reflection_class(terminal) != "content":
        raise _PreconditionFailed("only a content failure closes a round")
    threshold = snapshot.max_consecutive_failures
    due = threshold is not None and skip_due(snapshot, int(payload["round"]), threshold)
    if ("visitation" in payload) != due:
        raise _PreconditionFailed("the skip is not the one the prefix justifies")


def _check_campaign_finished(context: _Evidence, record: Envelope) -> None:
    """`campaign_finished`: the deciding record is of the kind the branch names (§5).

    A materialization refusal is not re-checkable — a target's lie is nowhere in
    the bytes — but a refusal at *promotion admission* is: the selector says
    which winner it refused, and that winner's durable identities say whether the
    verifier could have refused it.
    """

    payload = record.payload
    _check_linked_continuation(payload)
    # The prefix form is genuinely multi-reference (`selection_evidence` cites
    # the round's measurements); every other shape names one deciding record and
    # is held to exactly that one, like every other single-evidence row.
    prefix_form = all(item.get("kind") == "journal_prefix" for item in payload["evidence"])
    reference = payload["evidence"][0] if prefix_form else _only_reference(record)
    if (
        (payload["reason"] == "infrastructure_failed")
        and (reference.get("kind") == "journal_record")
        and (context.file_of(reference) == CAMPAIGN_FILE)
    ):
        checked = context.one_record(reference, campaign_family_for(context.family_version))
        index = context.records.index(record)
        prefix = context.records[:index]
        snapshot = reduce(prefix, selector=None, family_version=context.family_version)
        pending_checks = tuple(
            state for state in snapshot.rounds.values() if state.probe_check is not None
        )
        if checked.event != "probe_checked" or not pending_checks:
            raise _PreconditionFailed("the evidence is not a pending probe check")
        state = pending_checks[-1]
        check = state.probe_check
        assert check is not None
        if (
            snapshot.identity["reflector"]["probe"] is None
            or check["status"] != "ready"
            or check["reflection_id"]
            != f"r{checked.payload['round']:02d}-x{len(state.reflections) + 1:02d}"
            or state.closed is not None
            or state.diagnosis(True) is not None
            or state.focus_portion is None
            # A round that opens no attempt runs no reflection, so it can hold
            # no pending probe check: `none` and (family 6) `unmeasurable`.
            or state.focus_portion["reason"] in ("none", "unmeasurable")
            or checked.to_payload()["sequence"] != check["seq"]
            or hashlib.sha256(checked.encode()).hexdigest() != check["record_digest"]
        ):
            raise _PreconditionFailed("the evidence is not the current pending probe check")
        return
    if payload["reason"] == "probe_unavailable":
        if reference.get("kind") != "journal_record" or context.file_of(reference) != CAMPAIGN_FILE:
            raise _PreconditionFailed("the evidence is not a campaign probe check")
        checked = context.one_record(reference, campaign_family_for(context.family_version))
        if checked.event != "probe_checked" or checked.payload["status"] != "unavailable":
            raise _PreconditionFailed("the evidence is not an unavailable probe check")
        index = context.records.index(record)
        snapshot = reduce(
            context.records[:index], selector=None, family_version=context.family_version
        )
        retained = tuple(
            state.probe_check
            for state in snapshot.rounds.values()
            if state.probe_check is not None and state.probe_check["status"] == "unavailable"
        )
        if not retained:
            raise _PreconditionFailed("the prefix retains no unavailable probe check")
        latest = retained[-1]
        if (
            checked.to_payload()["sequence"] != latest["seq"]
            or hashlib.sha256(checked.encode()).hexdigest() != latest["record_digest"]
        ):
            raise _PreconditionFailed("the evidence names another probe check")
        return
    if payload["reason"] == "attestation_mismatch":
        raise _PreconditionFailed("a family-13 campaign suspends on an attestation refusal")
    if payload["reason"] == "baseline_incomplete":
        # §7: a baseline or winner-completion stage completed with an
        # indeterminate sample, and the continuation it offers is the linked run.
        if payload["continuation"]["kind"] != "linked_run":
            raise _PreconditionFailed("baseline_incomplete offers no linked run")
        measurement_id = _measurement_of_reference(context, reference)
        terminal, records = _cited_terminal(context, reference, measurement_id)
        final = _final_disposition(
            records,
            str(terminal.payload["case_id"]),
            str(terminal.payload["sample_id"]),
        )
        if final is None or final[0] != "indeterminate":
            raise _PreconditionFailed("the cited sample is not indeterminate")
        if payload["continuation"]["reason"] != final[2]:
            raise _PreconditionFailed("the continuation names another sample reason")
        return
    if payload["reason"] in ("scorer_metric_missing", "infrastructure_failed"):
        # §7: a campaign-level fault sample, cited as its own terminal record —
        # or, at the round barrier, the closed attempts' measurement prefixes
        # (`selection_evidence`), and for a wedged measurement its own prefix
        # (amendment 6).
        # Family 6 continues every terminal as a linked run (continuable-terminals
        # spec §5.1); the lifecycle already proved the continuation's own reason.
        continues = "linked_run"
        if payload["continuation"]["kind"] != continues:
            raise _PreconditionFailed("a campaign-level fault opens no continuation")
        if reference.get("kind") == "journal_prefix":
            _check_fault_prefixes(context, record)
            return
        measurement_id = _measurement_of_reference(context, reference)
        terminal, records = _cited_terminal(context, reference, measurement_id)
        if payload["reason"] == "scorer_metric_missing":
            if terminal.event != "scoring_finished":
                raise _PreconditionFailed("the evidence is not a scoring terminal")
            if context.policy_for(measurement_id).metric_primary in terminal.payload["metrics"]:
                raise _PreconditionFailed("the cited scoring carries the primary metric")
            return
        final = _final_disposition(
            records,
            str(terminal.payload["case_id"]),
            str(terminal.payload["sample_id"]),
        )
        if final is None or (final[0], final[2]) != ("exclude", "infrastructure_failed"):
            raise _PreconditionFailed("the cited sample is not a campaign-level fault")


def _check_linked_continuation(payload: Mapping[str, Any]) -> None:
    """Family 6's continuation rule for every terminal, mirroring the lifecycle (spec §5.1).

    Every `campaign_finished` continues as `linked_run` naming its own reason,
    and `baseline_incomplete` is not a family-6 terminal (§4.4).
    """

    continuation = payload["continuation"]
    if continuation["kind"] != "linked_run":
        raise _PreconditionFailed("a family-6 terminal continues as a linked_run")
    if continuation["reason"] != payload["reason"]:
        raise _PreconditionFailed("the linked_run does not name the terminal's reason")
    if payload["reason"] == "baseline_incomplete":
        raise _PreconditionFailed("baseline_incomplete is not a family-6 terminal")


def _measurement_of_reference(context: _Evidence, reference: Mapping[str, Any]) -> str:
    """The bound measurement whose journal a reference points into."""

    name = context.file_of(reference)
    parts = name.split("/")
    if len(parts) != 3 or parts[0] != MEASUREMENTS_DIR or parts[2] != EVENTS_FILE:
        raise _PreconditionFailed("the evidence is not in a measurement journal")
    return parts[1]


def _is_campaign_fault(
    records: Sequence[Envelope], row: Mapping[str, Any], reason: str, primary: str
) -> bool:
    """Is this scheduled sample the campaign-level fault the branch claims?

    The executor's own rule (`Measurement.fault_rows`): `infrastructure_failed`
    is the sample whose final disposition is that registered `exclude`, and
    `scorer_metric_missing` is a sample that *scored* without the primary metric.
    Both are read from the sample's final disposition and its own record, so a
    measurement merely being incomplete supports neither.
    """

    case_id, sample_id = str(row["case_id"]), str(row["sample_id"])
    final = _final_disposition(records, case_id, sample_id)
    if reason == "infrastructure_failed":
        return final == ("exclude", "infrastructure", "infrastructure_failed")
    if final is None or final[0] != "score":
        return False
    for entry in records:
        if entry.event != "scoring_finished":
            continue
        if (entry.payload.get("case_id"), entry.payload.get("sample_id")) != (case_id, sample_id):
            continue
        if primary not in entry.payload["metrics"]:
            return True
    return False


def _check_fault_prefixes(context: _Evidence, record: Envelope) -> None:
    """The prefix form of a campaign-level fault terminal (§7).

    The round barrier cites the closed attempts' measurement prefixes beside the
    baseline's (`selection_evidence`) rather than the one deciding record, so the
    fault sample has to be found rather than pointed at. It is found the same way
    the executor finds it — the registry over each scheduled sample's final
    disposition — and it must be a fault of *this branch's* reason: a measurement
    that is merely incomplete supports no campaign-level fault at all.
    """

    payload = record.payload
    for reference in payload["evidence"]:
        if reference.get("kind") != "journal_prefix":
            raise _PreconditionFailed("the evidence mixes prefixes and records")
    reason = str(payload["reason"])
    for reference in payload["evidence"]:
        measurement_id = _measurement_of_reference(context, reference)
        primary = context.policy_for(measurement_id).metric_primary
        records = context.measurement_journal(measurement_id)
        schedule = records[0].payload["identity"]["schedule"]
        if any(_is_campaign_fault(records, row, reason, primary) for row in schedule):
            return
    raise _PreconditionFailed("no cited measurement holds a sample of that campaign-level fault")


# -- family 7: suspensions, windows and repair tries (I1 spec §5.2-§5.4) ----


#: The campaign records that create a measurement (and so name its epoch): a
#: family-12 fresh Parent's `parent_measurement_bound` among them (G2b3a), a
#: family-12 completion's `completion_bound` (G2b4), and a family-13 cohort
#: member's `cohort_measurement_bound` (design §6.8).
_MEASUREMENT_CREATIONS = frozenset(
    {
        "measurement_bound",
        "parent_measurement_bound",
        "measurement_rebound",
        *(campaign_vocabulary_for(family).measurement_bound for family in (14,)),
    }
)

#: The campaign records that bind a measurement (and so name its epoch).
_BINDING_EVENTS = frozenset({"baseline_bound", *_MEASUREMENT_CREATIONS})


def _created_measurements(record: Envelope, family_version: int | None) -> tuple[str, ...]:
    """Measurement declarations, with explicit family15 nested final summaries."""
    p = record.payload
    if family_version == 15:
        if record.event == "final_measurement_bound":
            return (str(p["candidate"]["measurement_id"]),) if p["verification"] == "fresh" else ()
        if record.event in {"candidate_completion_bound", "champion_final_bound"}:
            return (str(p["measurement_id"]),)
    return (str(p["measurement_id"]),) if record.event in _MEASUREMENT_CREATIONS else ()


def _window_records(
    records: Sequence[Envelope], window_start: Any, owner: str
) -> tuple[Envelope, ...]:
    """The prefix records `owner`'s threshold window covers (I1 spec §5.3).

    Everything after the `campaign_suspended` `window_start` names, or the
    whole prefix before the first suspension, where the window *is* the
    campaign's totals. I2 spec §6.5: a window an in-place change of the
    owner's field restarted starts at that change's `identity_rebased`, and
    covers only what the new condition measured -- an accounted try of a
    measurement bound before the change (frozen by it) is not the window's.
    Which start is the latest is the lifecycle's to check; here it has to
    name a suspension or a change that restarts this owner.
    """

    if window_start is None:
        return tuple(records)
    seq = int(window_start)
    if 1 <= seq <= len(records):
        named = records[seq - 1]
        if named.to_payload()["sequence"] == seq:
            if named.event == "campaign_suspended":
                return tuple(records[seq:])
            if named.event == "identity_rebased" and owner in restarted_owners(
                named.payload["changed_fields"]
            ):
                after = tuple(records[seq:])
                bound = {
                    str(record.payload["measurement_id"])
                    for record in after
                    if record.event in _BINDING_EVENTS
                }
                return tuple(
                    record
                    for record in after
                    if record.event
                    not in ("indeterminate_observed", "repairs_dispatched", "stage_redispatched")
                    or str(record.payload["measurement_id"]) in bound
                )
            raise _PreconditionFailed(
                "window_start names neither a suspension nor a change of the owner's field"
            )
    raise _PreconditionFailed("window_start names no record of this campaign")


def _window_totals(
    records: Sequence[Envelope], owner: str, *, count_recovered: bool = True
) -> tuple[int, int]:
    """The crossing a window supports: `(count, denominator)` (I1 spec §5.3).

    The lifecycle's own two accounting rules, applied to the window's records
    instead of the campaign's whole prefix: one count per
    `indeterminate_observed` of that owner, and a denominator of every opened
    stage's samples plus the one slot each accounted try reserves when it says
    a further try follows it, plus (#78, spec §3.5) the slots each
    `repairs_dispatched` reserves for its threshold repairs, plus (#109, a28
    spec §3.8) the slots each `stage_redispatched` moves into the window.
    """

    window = OwnerWindow(owner)
    denominator = 0
    opened: set[str] = set()
    for seq, record in enumerate(records, 1):
        if record.event == "stage_opened":
            denominator += int(record.payload["sample_count"])
            opened.add(str(record.payload["measurement_id"]))
        elif record.event == "indeterminate_observed":
            window.observe(record.payload, seq)
            denominator += int(bool(record.payload.get("retry_follows")))
        elif record.event == "repairs_dispatched":
            # #78 (spec §3.5): a threshold repair takes a slot whatever it ends in.
            denominator += int(record.payload["count"])
            opened.add(str(record.payload["measurement_id"]))
        elif record.event == "stage_redispatched":
            # #109 (a28 spec §3.8): a carried stage's unfilled slots move into
            # the window it is dispatched in again, its position with them.
            denominator += int(record.payload["count"])
            opened.add(str(record.payload["measurement_id"]))
        elif (
            record.event == "measurement_rebound"
            and str(record.payload["from_measurement_id"]) in opened
        ):
            # I2: a re-bound source's unstarted
            # slots leave the window that counted its stage.
            denominator -= int(record.payload["released"])
    return window.count(count_recovered), denominator


def _check_suspension_faults(context: _Evidence, evidence: Mapping[str, Any], before: int) -> None:
    """A `fault_rows` suspension: the stage really had exactly those fault rows.

    The lifecycle checks the evidence's shape alone -- a complete list has to
    recompute to its own `rows_digest`, and that is all it can say without the
    measurement journal. Here the journal is readable, so the stage's fault
    rows are recomputed from the prefix as it stood at the suspension
    (`recompute_fault_rows`, the same derivation the executor's owed set reads)
    and must reproduce the evidence's `count` and `rows_digest`. A suspension
    over a stage with no fault at all, or one naming another sample, or a
    truncated one whose claimed total the prefix never held, all fail here.
    """

    named = {str(row["measurement_id"]) for row in evidence["rows"]}
    if len(named) != 1:
        raise _PreconditionFailed("the suspension's fault rows are not one measurement's")
    measurement_id = named.pop()
    records = context.measurement_journal(measurement_id)
    rebuilt = recompute_fault_rows(
        evidence,
        measurement_id,
        context.policy_for(measurement_id).metric_primary,
        records[0].payload["identity"]["schedule"],
        records,
        primary_range=context.primary_range(before, measurement_id),
    )
    if rebuilt is None:
        raise _PreconditionFailed(
            "the stage's fault rows do not recompute to the suspension's count and digest"
        )


def _check_suspension_attestation(
    context: _Evidence, named: Envelope, before: Sequence[Envelope], locator: object
) -> None:
    """A `target_changed` `stage_record`: the named record really fails.

    The lifecycle admits it because the record binds or attests the refused
    measurement; whether the attestation refuses it is a fact about durable
    identities, which R3's own verifier decides here. A refusal at
    materialization leaves no attested identity to contradict -- the target's
    lie is nowhere in the bytes -- but a measurement whose every attested
    identity admits the sealed Candidate was refused by nothing.

    a27 spec §4.3 (#94): a suspension that cites its refusal `prefix` is
    decided over those bytes alone, so neither an earlier session's honest
    attestations nor anything appended later decide it. The cited bytes must
    be the journal's head; a prefix that ends at a refusal -- a refused
    instance's release (`_ends_at_refusal`) or an adapter's own refusal inside
    `materialize` (`_ends_at_refused_materialization`) -- is the refusal's
    evidence: sound, not complete, since the lie is not in the bytes. Any
    other prefix is held to the rule above over the identities attested by
    instances it materialized (and by the frozen measurements a re-bind
    carried); an empty set admits vacuously, so it is a finding too. Only
    attestations journaled before the suspension (`before`, the campaign
    records preceding it) are read, so one appended later cannot support it
    (codex branch review round 1).
    Without `prefix` -- an exempt producer, or a promotion-admission refusal --
    the whole journal decides, as before.
    """

    family15_final = context.family_version == 15 and named.event in {
        "final_measurement_bound",
        "champion_final_bound",
        "candidate_completion_bound",
    }
    if not family15_final and named.event not in (
        "baseline_bound",
        "measurement_bound",
        "parent_measurement_bound",
        "measurement_rebound",
        context.vocabulary.measurement_bound,
        "instance_attested",
    ):
        raise _PreconditionFailed("the stage record neither binds nor attests a measurement")
    measurement_id = str(
        named.payload["candidate"]["measurement_id"]
        if family15_final and named.event == "final_measurement_bound"
        else named.payload["measurement_id"]
    )
    admitted = _PreconditionFailed(
        "the measurement's attested identities admit the sealed Candidate, so nothing refused it"
    )
    if locator is not None:
        records = _born_records(context, measurement_id)
        if records is None:
            raise _PreconditionFailed("the suspension's prefix is not its measurement journal's")
        cited = _cited_prefix(context, measurement_id, records, locator)
        if _ends_at_refusal(context, measurement_id, cited) or _ends_at_refused_materialization(
            context, measurement_id, cited
        ):
            return
        materialized = {
            record.payload.get("instance_id")
            for record in cited
            if record.event == "target_materialize_finished"
        }
        chain = set(context.chain(measurement_id))
        rows = tuple(
            row
            for row in before
            if row.event == "instance_attested"
            and row.payload["measurement_id"] in chain
            and (
                row.payload["measurement_id"] != measurement_id
                or row.payload["instance_id"] in materialized
            )
        )
        if context.verdict_over(measurement_id, rows) is None:
            raise admitted
        return
    if context.attested_rows(measurement_id) and (
        context.attestation_verdict(measurement_id) is None
    ):
        raise admitted


def _drift_reference(
    context: _Evidence,
    attempt_id: str,
    side: str,
    at: Callable[[], tuple[int, CampaignSnapshot]],
) -> DriftReference | None:
    """The drift reference of one side of `attempt_id`, derived once per audit.

    `at()` gives a campaign position and the snapshot reduced over the
    records before it, read only on the first call for that side. The
    callers ask after one of the Attempt's own measurements was bound, so
    its selection -- and with it the inspected map and the round's cited
    baseline -- is already final there: any such position derives the same
    reference. The Candidate's is its inspection's; the Parent's is
    `baseline_parent_reference` over the baseline prefix the round's
    `baseline_complete` cites and its same-run sources, read as
    `_check_affected_cases_selected` reads them. `None` for an Attempt that
    did not select in component mode without a fallback.
    """

    key = (attempt_id, side)
    if key not in context._drift_references:
        index, snapshot = at()
        attempt = next(
            (
                state.attempts[attempt_id]
                for state in snapshot.rounds.values()
                if attempt_id in state.attempts
            ),
            None,
        )
        if attempt is None:
            return None  # not cached: no position before the Attempt's binding decides
        reference: DriftReference | None = None
        baseline_id = snapshot.rounds[attempt.round].baseline_measurement_id
        if side == "candidate":
            reference = candidate_reference(attempt)
        elif checked_selection(attempt) and baseline_id is not None:
            reference = baseline_parent_reference(
                snapshot,
                attempt,
                str(baseline_id),
                _baseline_prefix(context, index, attempt.round, str(baseline_id)),
                lambda source: _same_run_records(context, snapshot, source),
            )
        context._drift_references[key] = reference
    return context._drift_references[key]


def _champion_drift_reference(
    context: _Evidence, round_number: int, at: Callable[[], tuple[int, CampaignSnapshot]]
) -> DriftReference | None:
    """F7 plan choice 10: the reference of a round's Champion cohort member, once per audit.

    `champion_reference` over the baseline prefix the round's
    `baseline_complete` cites and its same-run sources, as `_drift_reference`
    reads a Parent's. The callers ask after one of the round's members was
    bound, so its cohort and complete baseline are final there. `None`, not
    cached, when `at()`'s snapshot has no cohort in that round yet.
    """

    key = (str(round_number), "champion")
    if key not in context._drift_references:
        index, snapshot = at()
        state = snapshot.rounds.get(round_number)
        if (
            state is None
            or state.baseline_measurement_id is None
            or (
                state.cohort is None
                and not (
                    context.family_version == 15
                    and state.family15 is not None
                    and state.family15.champions
                )
            )
        ):
            return None
        baseline_id = str(state.baseline_measurement_id)
        context._drift_references[key] = champion_reference(
            snapshot,
            round_number,
            baseline_id,
            _baseline_prefix(context, index, round_number, baseline_id),
            lambda source: _same_run_records(context, snapshot, source),
        )
    return context._drift_references[key]


def _entry_drift_reference(
    context: _Evidence,
    entry: Mapping[str, Any],
    at: Callable[[], tuple[int, CampaignSnapshot]],
) -> DriftReference | None:
    """The reference a measurement's registry `entry` is checked against, else `None`.

    A family-12 Attempt's Candidate or fresh Parent: its side's; a family-13
    cohort member (F7 plan choice 10): a finalist its Attempt's Candidate
    reference, the Champion its round's. Any other measurement is not checked.
    """

    role = entry.get("role")
    member_role = context.vocabulary.registry_role
    if role == "family15_champion" or (role == member_role and entry["cohort_role"] == "champion"):
        return _champion_drift_reference(context, int(entry["round"]), at)
    if role not in ("candidate", "parent", member_role, "family15_final", "family15_completion"):
        return None
    side = (
        "candidate" if role in (member_role, "family15_final", "family15_completion") else str(role)
    )
    return _drift_reference(context, str(entry["attempt_id"]), side, at)


def _check_suspension_components(
    context: _Evidence, named: Envelope, prefix: Sequence[Envelope], locator: object
) -> None:
    """A `target_components_changed` `stage_record`: the measurement drifted (G2b3b).

    The named record binds or attests a family-12 Attempt's Candidate or fresh
    Parent measurement, as of the suspension; the Attempt selected in
    component mode without a fallback; the side's reference map recomputes
    with the executor's own readers (the Parent's from the baseline prefix the
    round's `baseline_complete` cites and its same-run sources, as
    `_check_affected_cases_selected` reads them); and the measurement's journal
    *as it stood at the refusal* declares a map other than the reference under
    which `drift_suspends` holds. Anything else names no component drift.

    Family 13 (F7 plan choice 10): the named record may also bind or attest a
    cohort member (`cohort_measurement_bound`, or the `measurement_rebound` of
    a member a `continue` re-bound), checked against `_entry_drift_reference`.

    ADR-0019 as amended 2026-10-02 (codex review round 2, maintainer decision
    option B): "as it stood at the refusal" is the suspension's own `prefix`,
    the journal's head through the refused instance's release
    (`_refusal_prefix`). The drift is decided over those bytes alone, so
    nothing appended later -- a declaration, or an instance a later session
    released unused -- can justify the suspension.
    """

    drift = _PreconditionFailed("the suspension names no component drift")
    family15_final = context.family_version == 15 and named.event in {
        "final_measurement_bound",
        "champion_final_bound",
        "candidate_completion_bound",
    }
    if not family15_final and named.event not in (
        "measurement_bound",
        "parent_measurement_bound",
        "measurement_rebound",
        context.vocabulary.measurement_bound,
        "instance_attested",
    ):
        raise drift
    measurement_id = str(
        named.payload["candidate"]["measurement_id"]
        if family15_final and named.event == "final_measurement_bound"
        else named.payload["measurement_id"]
    )
    snapshot = reduce(prefix, selector=None, family_version=context.family_version)
    entry = snapshot.measurements.get(measurement_id)
    if entry is None:
        raise drift
    # `None` for an Attempt not selected in component mode without a fallback
    # (and for its finalist member), and for a Champion member whose round's
    # baseline rows share no one declared map.
    reference = _entry_drift_reference(context, entry, lambda: (len(prefix), snapshot))
    records = _born_records(context, measurement_id)
    if reference is None or records is None:
        raise drift
    cited = _refusal_prefix(context, measurement_id, records, locator)
    declared = {
        str(record.payload["components_digest"])
        for record in cited
        if record.event == "components_declared"
    }
    if not (
        declared - {reference.digest} and drift_suspends(reference, int(entry["epoch"]), declared)
    ):
        raise drift


#: The records that end a release, the last record of a refusal's prefix.
_RELEASE_TERMINALS = frozenset({"target_release_finished", "target_release_failed"})


def _refusal_prefix(
    context: _Evidence, measurement_id: str, records: Sequence[Envelope], locator: object
) -> Sequence[Envelope]:
    """The records a drift suspension's `prefix` cites, held to be its refusal's:
    the journal's head (`_cited_prefix`) ending at a refusal (`_ends_at_refusal`)."""

    cited = _cited_prefix(context, measurement_id, records, locator)
    if not _ends_at_refusal(context, measurement_id, cited):
        raise _PreconditionFailed("the suspension's prefix does not end at a refusal")
    return cited


def _cited_prefix(
    context: _Evidence, measurement_id: str, records: Sequence[Envelope], locator: object
) -> Sequence[Envelope]:
    """The records a refusal suspension's `prefix` cites (drift, or a27's `target_changed`).

    The locator must name the measurement's own journal from its head and end
    on one of its record boundaries, and the cited bytes must be byte for byte
    that journal's (the lifecycle checked the form only).
    """

    match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    foreign = _PreconditionFailed("the suspension's prefix is not its measurement journal's")
    if match is None or match["file"] != name or int(match["start"]) != 0:
        raise foreign
    end, offset, count = int(match["end"]), 0, 0
    while offset < end and count < len(records):
        offset += len(records[count].encode())
        count += 1
    cited = records[:count]
    raw = context.cache.get(name, b"")
    if not cited or offset != end or b"".join(r.encode() for r in cited) != raw[:end]:
        raise foreign
    return cited


def _ends_at_refusal(context: _Evidence, measurement_id: str, cited: Sequence[Envelope]) -> bool:
    """Does a cited prefix end where a refusal at materialization ends?

    The last cited record must be the release of an instance the prefix
    materialized, ran nothing on and the campaign never attested: R1 releases a
    refused instance before any sample starts and the measurement records the
    prefix right after (`Measurement.refusal_prefix`), so a prefix ending
    anywhere else is not the refusal's. The one shape a drift has.
    """

    last = cited[-1]
    instance = last.payload.get("instance_id")
    return (
        last.event in _RELEASE_TERMINALS
        and isinstance(instance, str)
        and any(
            record.event == "target_materialize_finished"
            and record.payload.get("instance_id") == instance
            for record in cited
        )
        and _unused_and_unattested(context, measurement_id, cited, instance)
    )


def _ends_at_refused_materialization(
    context: _Evidence, measurement_id: str, cited: Sequence[Envelope]
) -> bool:
    """Does a cited prefix end where an adapter refused inside `materialize`?

    `target_changed` only (Task 3 fix round 1): an `AttestationError` raised by
    the adapter's own `materialize` (the HTTP adapter's `attestation_mismatch`
    or `strategy_unsupported`) leaves no instance to release. R1 journals it
    as `target_materialize_failed` with that attestation code, and the
    measurement records the prefix right after; the instance ran nothing and
    was never attested.
    """

    last = cited[-1]
    instance = last.payload.get("instance_id")
    return (
        last.event == "target_materialize_failed"
        and last.payload.get("error_code") in ATTESTATION_CODES
        and isinstance(instance, str)
        and _unused_and_unattested(context, measurement_id, cited, instance)
    )


def _unused_and_unattested(
    context: _Evidence, measurement_id: str, cited: Sequence[Envelope], instance: str
) -> bool:
    """`instance` did nothing in `cited` but materialize and release, and the
    campaign never attested it."""

    return not any(
        record.event == "instance_attested"
        and record.payload["measurement_id"] == measurement_id
        and record.payload["instance_id"] == instance
        for record in context.records
    ) and not any(
        record.payload.get("instance_id") == instance
        and not record.event.startswith(("target_materialize_", "target_release_"))
        for record in cited
    )


def _pending_probe_setup(context: _Evidence, prefix: Sequence[Envelope], seq: int) -> bool:
    """Is the `probe_checked` at `seq` the round's still-pending setup check?

    The second half of the lifecycle's own `probe_unavailable` rule
    (`CampaignLifecycle._campaign_suspended`): a *ready* check is a suspension
    cause only while the probe mediator it was checked for never started, which
    the prefix shows as that very check still standing as the round's
    `probe_check`, ready, for the reflection that has not run yet. Once the
    reflection ran, the check is nobody's cause any more.
    """

    snapshot = reduce(prefix, selector=None, family_version=context.family_version)
    return any(
        state.probe_check is not None
        and state.probe_check["seq"] == seq
        and state.probe_check["status"] == "ready"
        and state.probe_check["reflection_id"] == f"r{number:02d}-x{len(state.reflections) + 1:02d}"
        for number, state in snapshot.rounds.items()
    )


def _named_reflection_class(named: Envelope) -> str | None:
    """The family-10 class of a `reflection_failed` record's own bytes (spec
    §4.1); `None` for a record without a `reason` or a pair outside the table."""

    reason = named.payload.get("reason")
    if not isinstance(reason, str):
        return None
    try:
        return reflection_class(str(named.payload["error_class"]), reason)
    except KeyError:
        return None


def _named_editor_class(named: Envelope) -> str | None:
    """The family-10 class of an `editor_failed` record's own bytes (spec
    §5.3); `None` for a record without a `reason` or a pair outside the table."""

    reason = named.payload.get("reason")
    if not isinstance(reason, str):
        return None
    try:
        return editor_class(str(named.payload["error_class"]), reason)
    except KeyError:
        return None


def _second_skip_diagnosis(context: _Evidence, prefix: Sequence[Envelope], seq: int) -> bool:
    """Is the record at `seq` the diagnosis of a skip in a row with the
    previous skip (`skip_chain_breaks` over the prefix)?"""

    snapshot = reduce(prefix, selector=None, family_version=context.family_version)
    for number, state in snapshot.rounds.items():
        diagnosis = state.diagnosis(resilient=True)
        if diagnosis is not None and int(diagnosis["seq"]) == seq:
            close_at = next(
                (
                    at
                    for at, record in enumerate(prefix)
                    if record.event == "round_closed" and record.payload["round"] == number
                ),
                None,
            )
            return close_at is not None and skip_chain_length(snapshot, number) >= skip_chain_limit(
                context.adaptation_identity(close_at).to_payload()
            )
    return False


def _second_full_fallback(context: _Evidence, prefix: Sequence[Envelope], seq: int) -> bool:
    """Is the record at `seq` the second consecutive full fallback?

    Family 14 (ADR-0034, A§7, plan decision 13): the chain the records before
    it derive (`_grouper_chain`) stands at one.
    """

    limit = grouper_chain_limit(context.adaptation_identity(seq).to_payload())
    return any(
        at == seq - 1 and chain >= limit - 1 for at, _, chain in _grouper_chain(prefix[:seq])
    )


def _named_grouper_class(named: Envelope) -> str | None:
    """The class of a family-14 `grouping_failed(role_failed)` record's own
    bytes (`GROUPER_REASONS`); `None` for any other record, or a `reason` and
    `error_class` pair outside the table."""

    if named.event != "grouping_failed" or named.payload.get("code") != "role_failed":
        return None
    try:
        klass = grouper_class(str(named.payload.get("reason")))
    except KeyError:
        return None
    return klass if named.payload.get("error_class") == klass else None


def _grouper_chain(records: Sequence[Envelope]) -> Iterator[tuple[int, Envelope, int]]:
    """`(index, record, chain before it)` over `records`, in order.

    ADR-0034's fallback chain, re-derived from the record bytes alone, never
    from a lifecycle snapshot (plan decisions 6, 7, 13): a `grouping_finished`
    with a scope-`all` fallback adds one, one without a fallback sets it to
    zero, a scope-`uncovered` one and any `grouping_failed` leave it, and every
    `campaign_suspended` sets it to zero. Nothing else -- an
    `identity_rebased` included -- resets it.
    """

    chain = 0
    for index, record in enumerate(records):
        yield index, record, chain
        if record.event == "grouping_finished":
            fallback = record.payload.get("fallback")
            if fallback is None:
                chain = 0
            elif fallback.get("scope") == "all":
                chain += 1
        elif record.event == "campaign_suspended":
            chain = 0


#: What an invocation may journal between a durable `role_failed` and the
#: suspension it owes: the consequence records of its accepted decisions,
#: written before any planning (`_write_consequences`, then
#: `resumed_resilience_cause`) -- an in-place block and a rebase.
_CONSEQUENCE_EVENTS = frozenset(
    {"authorization_applied", "identity_rebased", "measurement_rebound"}
)


def _resume_consequence(record: Envelope) -> bool:
    """A record of a resume's consequence block (`_check_block_order` owns its
    order): see `_CONSEQUENCE_EVENTS`, plus a `rebaseline`'s
    `condition_changed` Attempt and round closures, and bind-check hits
    emitted by a family-15 rebase."""

    if record.event in _CONSEQUENCE_EVENTS:
        return True
    payload = record.payload
    if record.event == "check_shadowed":
        return (
            payload.get("round") is None
            and payload.get("operation_id") is None
            and payload.get("check")
            in {"verbatim_answer_unmatched", "secret_oracle_path_unmatched"}
        )
    if record.event == "attempt_closed":
        return (payload.get("outcome"), payload.get("reason")) == ("abandoned", "condition_changed")
    return record.event == "round_closed" and payload.get("outcome") == "condition_changed"


def _grouper_chain_findings(context: _Evidence) -> list[dict[str, Any]]:
    """ADR-0034, A§7, F0 R50 (plan decision 13): the fallback chain, re-derived.

    Current families. One forward pass over the campaign records
    (`_grouper_chain`) reports:

    - a `grouping_failed(role_failed)` of `error_class: content` before the
      bound full-fallback limit is reached;
    - a `grouping_finished` with a scope-`all` fallback at or beyond that
      limit: the converse;
    - a `role_failed` whose suspension does not follow it: the next record
      that is not a resume's consequence record (`_resume_consequence`; a
      rebase writes `identity_rebased` before the owed suspension) must be a
      `stage_record` `campaign_suspended` naming it, or the `campaign_finished`
      an operator's accepted close owes. A journal ending first is admitted:
      the next invocation owes the suspension.

    The class each Grouper suspension names is `_check_suspension_record`'s.
    """

    findings: list[dict[str, Any]] = []
    records = context.records

    def report(at: int, detail: str) -> None:
        findings.append(_campaign_finding(context, at, "precondition_failed", detail))

    for at, record, chain in _grouper_chain(records):
        payload = record.payload
        limit = grouper_chain_limit(context.adaptation_identity(at).to_payload())
        if record.event == "grouping_finished":
            fallback = payload.get("fallback")
            if fallback is not None and fallback.get("scope") == "all" and chain >= limit - 1:
                report(at, "the second full fallback is a grouping_finished, not a role_failed")
            continue
        if record.event != "grouping_failed" or payload.get("code") != "role_failed":
            continue
        if payload.get("error_class") == "content" and chain < limit - 1:
            report(at, "a content role_failed is not the second full fallback")
        index = next(
            (
                later
                for later in range(at + 1, len(records))
                if not _resume_consequence(records[later])
            ),
            None,
        )
        if index is None:
            continue
        follower = records[index]
        if follower.event == "campaign_suspended":
            evidence = follower.payload.get("evidence")
            held = (
                isinstance(evidence, Mapping)
                and evidence.get("evidence_kind") == "stage_record"
                and evidence.get("seq") == at + 1
            )
        else:
            held = (
                follower.event == "campaign_finished"
                and records[index - 1].event == "authorization_applied"
                and records[index - 1].payload.get("effect") == "close"
            )
        if not held:
            report(at, "the role_failed is not followed by its Grouper suspension")
    return findings


def _check_suspension_record(
    context: _Evidence, payload: Mapping[str, Any], prefix: Sequence[Envelope]
) -> None:
    """A `stage_record` suspension names one failing record of its own kind."""

    evidence = payload["evidence"]
    seq = int(evidence["seq"])
    if not 1 <= seq <= len(prefix) or prefix[seq - 1].to_payload()["sequence"] != seq:
        raise _PreconditionFailed("the stage record is not a record of this prefix")
    named = prefix[seq - 1]
    if named.operation_id != evidence["operation_id"]:
        raise _PreconditionFailed("the stage record names another operation")
    reason = str(payload["reason"])
    if reason in AFFECTED_SUSPENSIONS:
        _check_suspension_components(context, named, prefix, evidence.get("prefix"))
        return
    if reason == "target_changed":
        _check_suspension_attestation(context, named, prefix, evidence.get("prefix"))
        return
    if reason == "grouping_failed":
        held = named.event == "grouping_failed" and named.payload["code"] not in (
            "interrupted",
            "role_failed",
        )
    elif reason == "probe_unavailable":
        # Spec §5.2: the named record carries the failing status. Two records
        # can: an `unavailable` check outright, and -- when the probe mediator
        # never started -- the `ready` check whose setup is still pending for
        # the round's next reflection, which is what the family-6 probe-setup
        # terminal cites. Any other `probe_checked` is nobody's cause.
        held = named.event == "probe_checked" and (
            named.payload["status"] == "unavailable"
            or (named.payload["status"] == "ready" and _pending_probe_setup(context, prefix, seq))
        )
    elif reason == "editor_unresponsive":
        held = named.event == "editor_failed" and named.payload["error_class"] == "unresponsive"
    elif reason == "reflector_unresponsive":
        held = named.event == "reflection_failed" and named.payload["error_class"] == "unresponsive"
    elif reason == "reflector_provider_failed":
        # Family 10 (spec §4.2): a settled failure of class `provider`.
        held = named.event == "reflection_failed" and _named_reflection_class(named) == "provider"
    elif reason == "reflector_failing":
        # Family 10: a settled failure of class `unknown`, or the diagnosis
        # of a skip in a row with the previous one (spec §4.2-§4.3).
        held = named.event == "reflection_failed" and (
            _named_reflection_class(named) == "unknown"
            or _second_skip_diagnosis(context, prefix, seq)
        )
    elif reason == "editor_provider_failed":
        # Family 10 (spec §5.3): a settled Editor failure of class `provider`.
        held = named.event == "editor_failed" and _named_editor_class(named) == "provider"
    elif reason == "editor_failing":
        # Family 10 (spec §5.3): a settled Editor failure of class `unknown`.
        held = named.event == "editor_failed" and _named_editor_class(named) == "unknown"
    elif reason == "grouper_provider_failed":
        # Family 14 (ADR-0034): a Grouper `role_failed` of class `provider`.
        held = _named_grouper_class(named) == "provider"
    elif reason == "grouper_failing":
        # Family 14 (ADR-0034): a Grouper `role_failed` of class `unknown`, or
        # of class `content` where the re-derived chain makes it the second
        # consecutive full fallback.
        klass = _named_grouper_class(named)
        held = klass == "unknown" or (
            klass == "content" and _second_full_fallback(context, prefix, seq)
        )
    elif reason in CONNECTION_SUSPENSIONS:
        # Family 14 (H10, C§4.3, F0 R61): the failed preflight of that status,
        # immediately before the suspension.
        held = (
            named.event == "connection_preflight_failed"
            and named.payload["status"] == reason
            and seq == len(prefix)
        )
    else:
        held = False
    if not held:
        raise _PreconditionFailed("the stage record is not a failing record of that kind")


def _check_campaign_suspended(context: _Evidence, record: Envelope) -> None:
    """`campaign_suspended`: the evidence of its own kind recomputes (spec §5.2).

    One rule per `evidence_kind`, each recomputing what the lifecycle could
    not: a `threshold`'s crossing over the window's own records, a
    `fault_rows`' set over the measurement prefix, and a `stage_record`'s named
    record -- including, for `target_changed`, that its attestation really
    fails.
    """

    payload = record.payload
    evidence = payload["evidence"]
    kind = str(evidence["evidence_kind"])
    prefix = context.records[: context.records.index(record)]
    if kind == "threshold":
        crossing = evidence["crossing"]
        raw_policy = context.snapshot_before(len(prefix)).identity["policy"]
        policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
        count, denominator = _window_totals(
            _window_records(prefix, evidence["window_start"], str(crossing["owner"])),
            str(crossing["owner"]),
            count_recovered=policy.indeterminate[
                cast(Owner, crossing["owner"])
            ].effective_count_recovered,
        )
        if (int(crossing["count"]), int(crossing["denominator"])) != (count, denominator):
            raise _PreconditionFailed("the crossing is not the window's own count and denominator")
        return
    if kind == "fault_rows":
        _check_suspension_faults(context, evidence, len(prefix))
        return
    if kind == context.vocabulary.suspension_evidence_kind:
        _read_faults_reported(_check_cohort_rows)(context, record)
        return
    if kind == CHAMPION_ROWS:
        _read_faults_reported(_check_champion_rows)(context, record)
        return
    _check_suspension_record(context, payload, prefix)


def _check_cohort_rows(context: _Evidence, record: Envelope) -> None:
    """Family-13 design §6.13: the rows are the cohort's incomplete pairs as
    the suspension read them, and the owner arbitrates them (plan choice 21).

    The suspension cites no member prefix, and a resume's repairs change the
    rows after it, so each member's rows are read at the prefix the first
    campaign record after the suspension cites (`measurement_at`), or the
    whole journal when none does. That prefix is the one the suspension read:
    the barrier runs only after a stage owes no try, and the resume writes
    `repairs_dispatched` (or `stage_redispatched`), citing the member's journal
    as it stands, before it dispatches any try on it. The union, in member
    order, must reproduce the evidence's `count`, `rows_digest`, `truncated`
    and listed rows, and the owner must arbitrate it -- one rule whether or
    not the list is truncated. The rows are the gaps `incomplete_rows` reads,
    with each member's declined retries from the snapshot before the
    suspension (`retries_declined`), as the barrier read them. The members are
    the ones the barrier read, the current members still running
    (`running_members`, F7 plan choice 8): after a `continue` a re-bound
    member's `<id>@e<k>` journal, whose carried rows sit at its head and are
    cut at the look like any other.
    """

    name = context.vocabulary.report_key
    payload = record.payload
    evidence = payload["evidence"]
    index = context.records.index(record)
    snapshot = context.snapshot_before(index)
    if context.family_version == 15:
        from evolution.tree.confidence_cohort import cohort_gap, gap_owner

        assert snapshot.family15 is not None
        final_entries = [
            snapshot.measurements[mid]
            for mid in snapshot.family15.current_measurements.values()
            if snapshot.measurements[mid].get("role")
            in {"family15_completion", "family15_final", "family15_champion"}
            and not snapshot.measurements[mid]["complete"]
            and snapshot.rounds[snapshot.measurements[mid]["round"]].closed is None
        ]
        final_entries.sort(
            key=lambda entry: (
                entry["role"] == "family15_champion",
                entry["attempt_id"],
                entry["measurement_id"],
            )
        )
        found15: list[dict[str, Any]] = []
        for entry in final_entries:
            mid = entry["measurement_id"]
            records, state = context.measurement_at(mid, index)
            declined = declined_tries(snapshot.retries_declined, mid)
            found15.extend(
                dict(
                    measurement_id=mid,
                    case_id=row.case_id,
                    sample_id=row.sample_id,
                    retry_ordinal=row.retry_ordinal,
                    owner=gap_owner(row),
                )
                for row in latest_rows(records, state)
                if cohort_gap(row, declined)
            )
        if not found15:
            raise _PreconditionFailed("the final suspension has no actual incomplete slots")
        expected15 = cohort_rows_evidence(found15, family_version=15)
        if any(
            evidence[key] != expected15[key]
            for key in ("count", "rows_digest", "truncated", "rows")
        ) or payload["owner"] != cohort_owner(found15):
            raise _PreconditionFailed(
                "the final suspension differs from its current incomplete slots"
            )
        return
    # The open round's undecided cohort: a `rebaseline` mid-cohort closes its
    # round `condition_changed` with the cohort bound and no selection, and a
    # later round's cohort may suspend after it (the lifecycle's `_open_round`).
    states = [
        s
        for s in snapshot.rounds.values()
        if s.cohort is not None and s.selection is None and s.closed is None
    ]
    if len(states) != 1:
        raise _PreconditionFailed(f"the suspension follows no undecided {name}")
    cohort = states[0].cohort
    assert cohort is not None
    members = list(running_members(states[0]))
    if not members:
        raise _PreconditionFailed(f"the suspension follows no running {name} member")
    stage = max(len(snapshot.measurements[mid]["stages"]) for mid in members)
    if stage < 1:
        raise _PreconditionFailed(f"the suspension follows no {name} stage")
    look = int(cohort["looks"][stage - 1])
    found: list[dict[str, str]] = []
    for mid in members:
        records, state = context.measurement_at(mid, index)
        corpus = [case["case_id"] for case in context.identity_for(mid).corpus_cases]
        role = str(snapshot.measurements[mid]["cohort_role"])
        declined = declined_tries(snapshot.retries_declined, mid)
        # F7 plan choice 13: the producers keep raising for the executor, where
        # a raise is an invariant breach; over a forged journal it is a
        # finding. `incomplete_rows` raises `KeyError` on a row whose case is
        # outside the member's corpus and `_owner`'s `ValueError` on a gap
        # without a terminal or an owner (unreachable: S1 refuses an
        # unregistered outcome); `GateInvariant` is a `ValueError`.
        try:
            rows = latest_rows(records, state, look=look)
            found.extend(incomplete_rows([Member(mid, role, rows, declined)], corpus))
        except (KeyError, ValueError):
            raise _PreconditionFailed(
                f"the suspension's {name} rows cannot be read from its members' journals"
            ) from None
    mismatch = _PreconditionFailed(f"the suspension's {name} rows are not the incomplete pairs")
    if not found:
        raise mismatch
    expected = cohort_rows_evidence(found, family_version=context.family_version)
    if any(evidence[key] != expected[key] for key in ("count", "rows_digest", "truncated", "rows")):
        raise mismatch
    if payload["owner"] != cohort_owner(found):
        raise _PreconditionFailed("the suspension's owner does not arbitrate its rows")


def _check_champion_rows(context: _Evidence, record: Envelope) -> None:
    """#194 spec §3.2, §3.4: the rows are the listed Champion measurement's
    gaps as `close_measurement` read them, and the owner arbitrates them.

    As for `_check_cohort_rows`, the measurement is read at the prefix the
    first campaign record after the suspension cites (`measurement_at`), or
    the whole journal when none does: the listing is written at the terminal
    barrier, after the measurement's last stage, and the resume that spends
    its grants cites the journal (`repairs_dispatched`) before it dispatches
    any try. The gaps are `champion_gap_rows` over the latest try of every
    scheduled pair (plan choice 2: schedule order), with the declined
    retries from the snapshot before the suspension. `count`, `rows_digest`,
    `truncated`, the listed rows and `measurement_id` must reproduce, and the
    owner must arbitrate the whole set. Which measurement may be listed, and
    when, is the lifecycle's (`_champion_listing`).
    """

    payload = record.payload
    evidence = payload["evidence"]
    mid = str(evidence["measurement_id"])
    index = context.records.index(record)
    declined = declined_tries(context.snapshot_before(index).retries_declined, mid)
    mismatch = _PreconditionFailed("the suspension's champion rows are not the measurement's gaps")
    try:
        records, state = context.measurement_at(mid, index)
        if not records:
            raise mismatch
        schedule = records[0].payload["identity"]["schedule"]
        look = max(int(entry["repeat"]) for entry in schedule)
        # As in `_check_cohort_rows` (F7 plan choice 13): a producer's raise
        # over a forged journal is a finding, not a crash.
        rows = champion_gap_rows(mid, schedule, latest_rows(records, state, look=look), declined)
    except (KeyError, ValueError):
        raise _PreconditionFailed(
            "the suspension's champion rows cannot be read from its measurement's journal"
        ) from None
    if not rows:
        raise mismatch
    expected = champion_rows_evidence(mid, rows)
    if any(
        evidence[key] != expected[key]
        for key in ("measurement_id", "count", "rows_digest", "truncated", "rows")
    ):
        raise mismatch
    if payload["owner"] != champion_owner(rows):
        raise _PreconditionFailed("the suspension's owner does not arbitrate its rows")


def _check_cohort_measurement_completed(context: _Evidence, record: Envelope) -> None:
    """Family-13 design §6.9: the completion recomputed from the cited member prefix.

    The payload's `prefix` must be a head of the member's journal -- from byte
    0, ending on a record boundary (`_journal_head`) -- bound to the member's
    derived identity (`_bind_cited`). That it is the journal's end at the
    record is not checked here: the lifecycle holds it no shorter than any
    earlier citation of the member's journal (the #109 floor), which a genuine
    run's `instance_attested` sets at the whole journal. The rows are the
    executor's (`_gate_rows`, the latest try) cut at the cohort's bound R, and
    `member_completion` over them must give the recorded `complete`, `point`,
    `missing` and `critical`. An incomplete completion must satisfy D7: every
    non-scored row is a `rollout`-owned gap (`rollout_owned_gap`), a pair the
    settled stage left without a score -- exhausted, `cancelled`, or a try
    whose retry the cancelled run declined (PDR-0022 / ADR-0027, 2026-10-05).
    A member imports no rows (ADR-0017) -- except the rows a
    `measurement_rebound` carried into it, which `_check_rebound_carry` audits
    (F7 plan choice 9) -- and no scored row it rests on holds a primary
    outside [0, 1] (`_refuse_out_of_range`, ADR-0027 amendment (5)).
    `evidence` must be exactly the `instance_attested` records of the member's
    re-bind chain (`chain`, itself alone without a re-bind) before this one,
    in journal order (plan choice 9).
    """

    payload = record.payload
    mid = str(payload["measurement_id"])
    index = context.records.index(record)
    before = context.snapshot_before(index)
    cohort = before.rounds[int(payload["round"])].cohort
    if cohort is None:
        raise _PreconditionFailed(
            member_diagnostic(
                "the completion follows no bound cohort", family_version=context.family_version
            )
        )
    reference = _journal_head(context, payload["prefix"], mid, "the completion's prefix")
    prefixes, derived = _bind_cited(context, {mid: reference})
    records, state = prefixes[mid]
    carried = before.measurements[mid].get("rebound_from") is not None
    if not carried and (state.imported or any(r.event == "sample_imported" for r in records)):
        raise _PreconditionFailed(
            member_diagnostic(
                "a cohort member imports no rows", family_version=context.family_version
            )
        )
    identity = context.identity_for(mid)
    groups = case_groups(identity.corpus_cases)
    repeats = int(cohort["repeats"])
    rows = _gate_rows(
        context,
        prefixes,
        derived,
        repeats,
        groups=groups,
        seed_mode=str(identity.to_payload()["target"]["seed_mode"]),
    )[mid]
    primary = context.policy_for(mid).metric_primary
    _refuse_out_of_range(context, record, [(mid, rows)], primary)
    result = member_completion(
        rows,
        groups=groups,
        corpus=[str(case["case_id"]) for case in identity.corpus_cases],
        primary=primary,
        repeats=repeats,
    )
    recorded = (payload["complete"], payload["point"], payload["missing"], payload["critical"])
    if isinstance(result, MissingMetric) or (
        (result.complete, result.point, result.missing, result.critical) != recorded
    ):
        raise _PreconditionFailed("the completion is not its member's rows")
    if not result.complete:
        # D6, D7, design §8 (3): only a finalist's rollout-owned gap completes
        # incomplete (the lifecycle already refuses an incomplete Champion).
        # Every pair cut at R that did not score must be one: a `not_started`
        # or still-owed pair, an infrastructure-owned gap or a fault row had
        # to suspend or keep the cohort running instead.
        declined = declined_tries(before.retries_declined, mid)
        if not all(rollout_owned_gap(row, declined) for row in rows if row.disposition != "score"):
            raise _PreconditionFailed("an incomplete finalist's missing pair is not rollout-owned")
    family = campaign_family_for(context.family_version)
    chain = set(context.chain(mid))
    attested = [
        earlier.to_payload()
        for earlier in context.records[:index]
        if earlier.event == "instance_attested" and earlier.payload["measurement_id"] in chain
    ]
    cited = [
        context.one_record(ref, family).to_payload()
        for ref in payload["evidence"]
        if ref.get("kind") == "journal_record" and context.file_of(ref) == CAMPAIGN_FILE
    ]
    if len(cited) != len(payload["evidence"]) or cited != attested:
        raise _PreconditionFailed("the completion does not cite its member's attestations")
    _check_completed_member_tail(context, mid, reference)


def _check_completed_member_tail(
    context: _Evidence, measurement_id: str, reference: Mapping[str, Any]
) -> None:
    """Later bytes can prove a forbidden new row, never alter the cited rows.

    Ignore malformed/torn suffixes here: they are not evidence of another row.
    A well-formed new try, score or import is forbidden even when its S1
    lifecycle would refuse it. Completed members have no repair grant.
    """

    end = context.range_of(reference)[1]
    raw = context.cache[context.file_of(reference)]
    # Only LF delimits journal records; ignore only the final unterminated fragment.
    for line in raw[end:].split(b"\n")[:-1]:
        try:
            appended = Envelope.from_bytes(line + b"\n", family=S1_FAMILY)
        except (ValueError, TypeError):
            continue  # A malformed line cannot hide a later well-formed row.
        if appended.event in {
            "rollout_started",
            "scoring_started",
            *_SAMPLE_TERMINALS,
        }:
            raise _PreconditionFailed(
                f"{measurement_id} gained a try or row after its completion",
                "completed_member_appended",
            )


def _check_global_comparison_completed(context: _Evidence, record: Envelope) -> None:
    """Recompute fresh or stored final comparisons from their immutable citations.

    Family 14 stable evidence is the finalist completion then the round's
    exact stored baseline prefix. Its original measurement identity, full
    exclusion summary, point-set pairing and independently renormalized
    point/bound weights govern reconstruction. Reference checks precede the
    numerical-recompute guard. The stored content digest is seed input two.

    On the fresh path, evidence is the finalist's and then the Champion's
    completion. Each member's rows are read from that
    completion's `prefix` (a head of its journal, bound to its derived
    identity), the executor's way (`_gate_rows`, cut at the bound R), and no
    scored row may hold a primary outside [0, 1]. `global_comparison_record`
    -- the executor's own producer -- over those rows, at `DRAWS`, with the
    cohort's binding digest and both prefixes in the seed, must reproduce
    every field, `pairing` among them: the rows' own provenance proof decides
    it, so a complete comparison claiming `exact` over rows that prove no
    seed (a non-exact `seed_mode`) is a finding. One computation per seed and
    inputs (`context.computed`).
    """

    payload = record.payload
    index = context.records.index(record)
    state = context.snapshot_before(index).rounds[int(payload["round"])]
    cohort = state.cohort
    if cohort is None:
        raise _PreconditionFailed(
            member_diagnostic(
                "the comparison follows no bound cohort", family_version=context.family_version
            )
        )
    finalists = [str(f["attempt_id"]) for f in cohort["finalists"]]
    if str(payload["attempt_id"]) not in finalists:
        raise _PreconditionFailed(
            member_diagnostic(
                "the comparison names no finalist of its cohort",
                family_version=context.family_version,
            )
        )
    position = finalists.index(str(payload["attempt_id"]))
    # F7 plan choice 7: the current members, a re-bound one under `<id>@e<k>`.
    members = list(current_members(state))
    # V11 policy and the actual member binding jointly choose the stored path.
    member = finalist_member(cohort, position, members=members)
    policy = context.policy_for(member)
    stability = policy.stability
    assert stability is not None
    before = context.snapshot_before(index)
    entry = before.measurements[member]
    champion = champion_member(cohort, members=members, stability=stability)
    stored = None
    family = campaign_family_for(context.family_version)
    evidence = payload["evidence"]
    done = {member: state.cohort_completed[member]}
    if stability == "stable":
        if entry.get("stability") != "stable" or entry.get("champion_side") != "stored":
            raise _PreconditionFailed("the stable comparison has no stored member binding")
        stored = entry["stored_champion"]
        champion = str(stored["measurement_id"])
        reference = entry["stored_reference"]
        _stored_final_reference(context, state, stored, reference)
        if len(evidence) != 2 or dict(evidence[1]) != dict(reference):
            raise _PreconditionFailed("the comparison does not cite the stored baseline prefix")
        completions = evidence[:1]
        expected_sequences = [int(done[member]["seq"])]
        references = {champion: reference}
    else:
        if champion is None:
            raise _PreconditionFailed("the comparison has no fresh Champion member")
        done[champion] = state.cohort_completed[champion]
        completions = evidence
        expected_sequences = [int(done[mid]["seq"]) for mid in (member, champion)]
        references = {}
    cited = [
        context.one_record(ref, family).to_payload()["sequence"]
        for ref in completions
        if ref.get("kind") == "journal_record" and context.file_of(ref) == CAMPAIGN_FILE
    ]
    if len(cited) != len(completions) or cited != expected_sequences:
        raise _PreconditionFailed("the comparison does not cite its members' completions")
    if stored is None and not context.recompute:
        return  # Preserve the established fresh-path corpus-mismatch behavior.
    references.update(
        {
            mid: _journal_head(context, completion["prefix"], mid, "a completion's prefix")
            for mid, completion in done.items()
        }
    )
    prefixes, derived = _bind_cited(context, references)
    if not context.recompute:
        return
    identity = context.identity_for(member)
    groups = case_groups(identity.corpus_cases)
    corpus_ids = [str(case["case_id"]) for case in identity.corpus_cases]
    repeats = int(cohort["repeats"])
    rows = _gate_rows(
        context,
        {mid: value for mid, value in prefixes.items() if stored is None or mid != champion},
        derived,
        repeats,
        groups=groups,
        seed_mode=str(identity.to_payload()["target"]["seed_mode"]),
    )
    excluded: frozenset[tuple[str, int]] = frozenset()
    if stored is not None:
        records, lifecycle = prefixes[champion]
        target = derived[champion].to_payload()["target"]
        # Full cited horizon and historical identity, including carried provenance.
        rows[champion] = latest_rows(
            records,
            lifecycle,
            seed_mode=str(target["seed_mode"]),
            dependency_digest=str(target["dependency_digest"]),
        )
        excluded = _stored_final_excluded(context, record, champion, rows[champion], repeats)
    _refuse_out_of_range(
        context, record, [(mid, rows[mid]) for mid in (member, champion)], policy.metric_primary
    )
    seed = derive_seed(
        campaign_seed=policy.random_seed,
        binding=str(cohort["digest"]),
        scope="global",
        prefixes=[
            str(done[member]["prefix"]),
            str(stored["prefix_digest"] if stored is not None else done[champion]["prefix"]),
        ],
        look=None,
        purpose="global",
    )
    inputs = {
        "cohort": cohort["digest"],
        "position": position,
        "completed": {mid: dict(value) for mid, value in done.items()},
        "family_version": context.family_version,
        "stability": stability,
        "stored_champion": None if stored is None else dict(stored),
        "references": {mid: dict(ref) for mid, ref in references.items()},
        "excluded": sorted(excluded),
        "removal_evidence": [dict(ref) for ref in evidence],
        "groups": groups,
        "corpus_ids": corpus_ids,
        "primary": policy.metric_primary,
        "members": members,
        "rows": {mid: _row_facts(rows[mid], policy.metric_primary) for mid in (member, champion)},
    }
    try:
        expected = context.computed(
            "global",
            seed,
            inputs,
            lambda: global_comparison_record(
                cohort,
                state.cohort_completed,
                members=members,
                position=position,
                finalist_rows=rows[member],
                champion_rows=rows[champion],
                groups=groups,
                corpus_ids=corpus_ids,
                primary=policy.metric_primary,
                campaign_seed=policy.random_seed,
                draws=DRAWS,
                family_version=context.vocabulary.family_version,
                stability=stability,
                stored_champion=stored,
                excluded=excluded,
                removal_evidence=evidence,
            ),
        )
    except (GateInvariant, CoverageMissing) as exc:
        raise _PreconditionFailed(str(exc)) from None
    if isinstance(expected, MissingMetric):
        raise _PreconditionFailed(
            f"{expected.case_id}/{expected.sample_id} has no primary metric to compare"
        )
    for name in sorted(expected):
        if payload.get(name) != expected[name]:
            raise _PreconditionFailed(
                f"{name} is {expected[name]!r}, the record says {payload.get(name)!r}"
            )


def _repair_findings(context: _Evidence) -> list[dict[str, Any]]:
    """Family 7: every repair try was granted by a suspension that named its row.

    I1 spec §5.4. The campaign lifecycle's ordinal bound is loose on purpose --
    `sample_retries` plus one ordinal per `fault_rows` suspension, campaign-wide
    -- because it cannot see which rows a truncated suspension named. Here both
    sides are readable: the suspensions in order, each with the complete set of
    rows it named (re-derived from the measurement prefix when the evidence was
    truncated, exactly as `owed_repair_rows` does), and the measurement's own
    repair starts. A repair at ordinal `k` spends the grant of a suspension that
    named its row at ordinal `k - 1`, and a grant is spent once -- so a repair
    of a row no suspension named, and a second repair under one suspension, are
    both reported. `ordinal_limit` is the bound, the one expression S1, the
    campaign lifecycle and this audit share.

    #78 (spec §3.4): a threshold suspension grants the pairs the replayed
    lifecycle names (`threshold_named`) one repair each, read right after it,
    so its grants join the same per-pair pool and are spent the same way.
    A threshold grant's repair also takes a denominator slot (ADR-0019 as
    amended 2026-10-01), so it must follow a `repairs_dispatched` of its
    measurement written after the suspension that named it.
    From a27 (spec §3.1, #116) any suspension whose version owes held repair
    also names an open Attempt's exhausted pairs; `threshold_named` already
    carries those grants, so they join the same pool and need the same
    `repairs_dispatched`.
    Baselines are audited too: their repairs are granted by the same records.

    #194 rule (c): from family 12, a D1 retry (no `repair` key) whose previous
    try on the pair ended `cancelled` is audited as a granted try too. Only a
    `champion_incomplete` listing grants one, so on an Attempt, an unlisted
    pair, a spent grant or any family-12 journal it is named by no suspension.

    A suspension whose rows do not recompute is already reported under its own
    record, so the measurement it names is left alone here rather than reported
    twice under a second name.
    """

    if not context.records:
        return []
    grants = context.threshold_replay().grants
    # Ruling R5: each suspension's rows recompute under the version in force at it.
    suspended = [
        (record.payload["evidence"], index)
        for index, record in enumerate(context.records)
        if record.event == "campaign_suspended"
        and record.payload["evidence"]["evidence_kind"] == "fault_rows"
    ]
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, record, measurement_id in (
        (i, item, mid)
        for i, item in enumerate(context.records, start=1)
        for mid in (
            (str(item.payload["measurement_id"]),)
            if item.event == "baseline_bound"
            else _created_measurements(item, context.family_version)
        )
    ):
        # A measurement re-bound under `continue` runs repair tries of its own,
        # and a baseline's are audited like any other (#78).
        if measurement_id in seen:
            continue
        seen.add(measurement_id)
        name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
        if name not in context.cache and not (context.run_root / name).is_file():
            continue
        try:
            primary = context.policy_for(measurement_id).metric_primary
        except (_PreconditionFailed, _Unverifiable):
            continue
        try:
            records = context.measurement_journal(measurement_id)
        except _PreconditionFailed:
            # An unreadable measurement journal is reported by whichever record
            # cites it; nothing is claimed about repairs it may hold.
            continue
        # Family 13 (pre-F8 spec §2.6): a D1 retry a listing's grant restored
        # (`repair: false`) spends its grant like a repair, in journal order.
        restoring = True
        # #194 rule (c): S1 admits one D1 retry after a closed `cancelled` try
        # on any row-provenance journal (families 12-14), since it sees neither
        # the family nor the grants. Only a Champion listing grant may start
        # one, so every such start must spend a grant here; in family 12, which
        # never lists, every one is a finding.
        after_cancel = True
        named = grants.get(measurement_id, {})
        latest: dict[tuple[str, str], Envelope] = {}
        starts = []
        for entry in records:
            if not entry.event.startswith("rollout_") or "case_id" not in entry.payload:
                continue
            key = (str(entry.payload["case_id"]), str(entry.payload["sample_id"]))
            previous = latest.get(key)
            latest[key] = entry
            if entry.event != "rollout_started":
                continue
            if (
                entry.payload.get("repair") is True
                or (
                    restoring
                    and any(
                        granted == int(entry.payload.get("retry_ordinal", 0)) - 1
                        for granted, _naming in named.get(key, ())
                    )
                )
                or (
                    after_cancel
                    and previous is not None
                    and previous.event == "rollout_finished"
                    and previous.payload.get("outcome") == "cancelled"
                )
            ):
                starts.append(entry)
        if not starts:
            continue
        try:
            reserved = [
                (int(entry.to_payload()["sequence"]), *_dispatched_owed(context, entry))
                for entry in context.records
                if entry.event == "repairs_dispatched"
                and entry.payload["measurement_id"] == measurement_id
            ]
        except (_PreconditionFailed, _Unverifiable):
            # A reservation that does not recompute is reported under its own
            # record (`repairs_dispatched_mismatch`), or the replay ended
            # before it; its coverage is not claimed either way.
            continue
        suspensions = [
            (evidence, context.primary_range(at, measurement_id)) for evidence, at in suspended
        ]
        detail = _repair_detail(
            records,
            starts,
            suspensions,
            grants.get(measurement_id, {}),
            reserved,
            measurement_id,
            primary,
        )
        if detail is None:
            continue
        raw = context.cache[name]
        findings.append(
            dict(
                sequence=index,
                event=record.event,
                locator=f"{name}#bytes=0:{len(raw)}",
                code="repair_try_mismatch",
                detail=detail,
            )
        )
    return findings


def _repair_detail(
    records: Sequence[Envelope],
    starts: Sequence[Envelope],
    suspensions: Sequence[tuple[Mapping[str, Any], PrimaryRange]],
    threshold: Mapping[tuple[str, str], Sequence[tuple[int, int]]],
    reserved: Sequence[tuple[int, int, frozenset[tuple[str, str]]]],
    measurement_id: str,
    primary: str,
) -> str | None:
    """Why one measurement's repair tries are not the ones its suspensions granted.

    `suspensions` are the `fault_rows` evidences, each with the rows a primary
    outside [0, 1] made fault rows under the version in force at it (#97
    spec §5, ruling R5); `threshold` the
    `(ordinal, naming_seq)` threshold suspensions granted each pair (#78, spec
    §3.4), one per naming suspension. Both are grants a repair spends once,
    a `fault_rows` grant first when both cover the same ordinal. A threshold
    grant is spent only under the reservation that covers it (codex branch
    review rounds 1 and 2): one of `reserved` -- each `repairs_dispatched` of
    the measurement as `(seq, cited prefix length, owed pairs)` -- written
    after the naming suspension, whose cited prefix ends before the repair's
    `rollout_started` and whose owed set holds the pair. A reservation covers
    each pair it owes once.
    """

    identity = records[0].payload["identity"]
    schedule = identity["schedule"]
    limit = int(identity.get("sample_retries") or 0)
    # Each grant is `(ordinal, naming_seq)`; a `fault_rows` grant has no naming.
    granted: dict[tuple[str, str], list[tuple[int, int | None]]] = {}
    for evidence, primary_range in suspensions:
        if not any(row["measurement_id"] == measurement_id for row in evidence["rows"]):
            continue
        named = recompute_fault_rows(
            evidence,
            measurement_id,
            primary,
            schedule,
            records,
            primary_range=primary_range,
        )
        if named is None:
            return None
        for key, ordinal in named.items():
            granted.setdefault(key, []).append((ordinal, None))
    for key, grants in threshold.items():
        granted.setdefault(key, []).extend(grants)
    spent: dict[tuple[str, str], int] = {}
    covered: set[tuple[int, tuple[str, str]]] = set()
    position = {id(entry): index for index, entry in enumerate(records)}
    for start in starts:
        key = (str(start.payload["case_id"]), str(start.payload["sample_id"]))
        ordinal = int(start.payload.get("retry_ordinal", 0))
        spent[key] = spent.get(key, 0) + 1
        available = granted.get(key, [])
        matching = sorted(
            (grant for grant in available if grant[0] == ordinal - 1),
            key=lambda grant: grant[1] is not None,
        )
        if not matching:
            return f"the repair try {ordinal} of {key[0]}/{key[1]} was named by no suspension"
        grant = matching[0]
        available.remove(grant)
        if ordinal > ordinal_limit(limit, spent[key]):
            return f"the repair try {ordinal} of {key[0]}/{key[1]} is past the ordinal limit"
        naming = grant[1]
        if naming is None:
            continue
        cover = next(
            (
                seq
                for seq, prefix_end, owed in reserved
                if seq > naming
                and prefix_end <= position[id(start)]
                and key in owed
                and (seq, key) not in covered
            ),
            None,
        )
        if cover is None:
            return (
                f"the repair try {ordinal} of {key[0]}/{key[1]} was granted by a threshold"
                " suspension, but no repairs_dispatched reserved its slot"
            )
        covered.add((cover, key))
    return None


# -- family 7: in-place condition changes (I2 spec §5, ADR-0020) ------------
#
# Four rules over the admitted prefix, each reported under the record it is
# about. The lifecycle already refuses a journal that breaks the block rules
# at admission (its tail is then a `suffix_refused` finding); the audit
# re-proves them over the raw records as defense in depth, and it proves what
# the lifecycle cannot see: identities derived from each epoch, carried rows
# against their frozen source, and the catch-up records of a frozen
# measurement against its own journal.

#: The records a frozen measurement may still be named by after the change that
#: froze it: the accounting of a try it ran before (a catch-up
#: `indeterminate_observed`) and the attestation of an instance it opened
#: (`instance_attested`), both about bytes it already holds (ADR-0020).
_FROZEN_CATCH_UP = frozenset({"indeterminate_observed", "instance_attested"})
#: What a later round may still write about a frozen measurement that was
#: complete before its change: reuse it as a baseline and record that baseline
#: complete. Neither appends to it; a complete measurement stays a valid
#: baseline (ADR-0020). A family-13 `selection_decided` names the cohort
#: member it selects, which a `continue` after its completion froze (F7 plan
#: choice 14); naming it does not append to it either.
#: Family 14 (H7, F0 R43): a `continue` that froze a complete refresh
#: measurement before its `baseline_refresh_completed` leaves that record
#: naming it; like `baseline_complete`, it does not append to it.
_FROZEN_REUSE = frozenset(
    {"baseline_bound", "baseline_complete", "selection_decided", "baseline_refresh_completed"}
)
_COMPLETIONS = frozenset({"baseline_complete", "champion_complete"})
_IN_PLACE_BLOCK_EFFECTS = frozenset({"continue", "rebaseline"})


def _opens_epoch(record: Envelope, *, family_version: int | None = None) -> bool:
    """An `identity_rebased` that moves a comparable field: an in-place change."""

    return record.event == "identity_rebased" and any(
        comparable_field(str(name), family_version=family_version)
        for name in record.payload["changed_fields"]
    )


def _changed_top_fields(record: Envelope, *, family_version: int | None = None) -> list[str]:
    """The comparable top-level fields an in-place `identity_rebased` moved."""

    return sorted(
        {
            str(name).split(".")[1]
            for name in record.payload["changed_fields"]
            if comparable_field(str(name), family_version=family_version)
        }
    )


def _campaign_finding(context: _Evidence, index: int, code: str, detail: str) -> dict[str, Any]:
    """A finding located at `context.records[index]` in the campaign log."""

    start = sum(len(record.encode()) for record in context.records[:index])
    record = context.records[index]
    return dict(
        sequence=index + 1,
        event=record.event,
        locator=f"{CAMPAIGN_FILE}#bytes={start}:{start + len(record.encode())}",
        code=code,
        detail=detail,
    )


def _check_block_order(
    records: Sequence[Envelope], *, family_version: int | None = None
) -> list[tuple[int, str]]:
    """Every in-place block is contiguous and in order (I2 spec §5.4, §5.5).

    `authorization_applied{continue|rebaseline}` is followed at once by the
    `identity_rebased` that names its decision and moves a comparable field;
    such an `identity_rebased` follows only that record. Under `continue` the
    block goes on with `measurement_rebound`s only; under `rebaseline` with the
    open round's `attempt_closed{abandoned, condition_changed}`s, then its
    `round_closed{condition_changed}`. A `measurement_rebound`, such an
    Attempt closure and such a round closure are admitted nowhere else. A
    prefix may end inside a block (the next invocation owes its rest).
    Returns `(index, detail)` per violation.
    """

    problems: list[tuple[int, str]] = []
    phase: str | None = None
    for index, record in enumerate(records):
        previous = records[index - 1] if index else None
        payload = record.payload
        in_place = (
            previous is not None
            and previous.event == "authorization_applied"
            and previous.payload.get("effect") in _IN_PLACE_BLOCK_EFFECTS
        )
        if record.event == "identity_rebased":
            if _opens_epoch(record, family_version=family_version) and not in_place:
                problems.append((index, "a comparable identity_rebased outside its block"))
            if in_place:
                assert previous is not None
                if not _opens_epoch(record, family_version=family_version):
                    problems.append(
                        (index, "the block's identity_rebased moves no comparable field")
                    )
                elif payload["authorization"]["id"] != previous.payload["authorization"]["id"]:
                    problems.append((index, "the block's identity_rebased names another decision"))
                phase = "rebind" if previous.payload["effect"] == "continue" else "close"
                continue
        elif in_place:
            problems.append((index - 1, "an in-place decision is not followed by its identity"))
        if record.event == "measurement_rebound":
            if phase != "rebind":
                problems.append((index, "a measurement_rebound outside a continue block"))
            continue
        closes_attempt = (
            record.event == "attempt_closed"
            and payload.get("outcome") == "abandoned"
            and payload.get("reason") == "condition_changed"
        )
        if closes_attempt:
            if phase != "close":
                problems.append((index, "a condition_changed closure outside a rebaseline block"))
            continue
        if record.event == "round_closed" and payload.get("outcome") == "condition_changed":
            if phase != "close":
                problems.append((index, "a condition_changed round outside a rebaseline block"))
            phase = None
            continue
        phase = None
    return problems


def _epoch_effects(
    records: Sequence[Envelope], *, family_version: int | None = None
) -> list[tuple[int, str]]:
    """`(index, effect)` of each in-place change's `identity_rebased`, in order."""

    return [
        (index, str(records[index - 1].payload.get("effect")))
        for index, record in enumerate(records)
        if _opens_epoch(record, family_version=family_version)
        and index
        and records[index - 1].event == "authorization_applied"
    ]


def _frozen_at(records: Sequence[Envelope], *, family_version: int | None = None) -> dict[str, int]:
    """Each frozen measurement and the index of the change that froze it (ADR-0020).

    The lifecycle freezes the measurements declared before the change. A
    `baseline_bound` alone declares nothing: a run cut before its fresh
    champion's `measurement_bound` (#106) names a baseline that the change
    does not freeze, and that is declared in the new epoch afterwards.
    """

    bound: list[str] = []
    frozen: dict[str, int] = {}
    for index, record in enumerate(records):
        created = _created_measurements(record, family_version)
        if created:
            bound.extend(created)
        elif _opens_epoch(record, family_version=family_version):
            for measurement_id in bound:
                frozen.setdefault(measurement_id, index)
    return frozen


def _check_frozen_untouched(context: _Evidence) -> list[tuple[int, str]]:
    """No frozen measurement is written after the change that froze it (§5.1).

    After its freezing `identity_rebased`, a campaign record may name a frozen
    measurement only to account a try it already ran (`indeterminate_observed`,
    whose own check proves the try), to attest an instance its journal already
    opened (`instance_attested`, proven here), or -- when it was complete
    before the change and the epoch in force was opened by `continue` -- to
    reuse it as a later round's baseline. After a `rebaseline` the next round
    binds a fresh baseline (§3.2), so no reuse of a frozen one is admitted.
    Reading it -- a look's evidence, a re-bind's source -- is not writing it.
    A family-13 cohort member is complete once a `cohort_measurement_completed`
    says `complete: true` (F7 plan choice 14, `recorded_complete`): its
    selection, and its reuse as the next baseline, may follow the change.
    """

    problems: list[tuple[int, str]] = []
    frozen = _frozen_at(context.records, family_version=context.family_version)
    complete_at: dict[str, int] = {}
    for index, record in enumerate(context.records):
        if context.family_version == 15 and record.event in {
            "gate_measurement_completed",
            "candidate_completion_completed",
            "final_measurement_completed",
        }:
            complete_at.setdefault(str(record.payload["source"]["measurement_id"]), index)
            continue
        if record.event in _COMPLETIONS or (
            record.event == context.vocabulary.measurement_completed
            and record.payload.get("complete") is True
        ):
            complete_at.setdefault(str(record.payload["measurement_id"]), index)
    changes = _epoch_effects(context.records, family_version=context.family_version)

    def under_continue(index: int) -> bool:
        effects = [effect for at, effect in changes if at < index]
        return bool(effects) and effects[-1] == "continue"

    for index, record in enumerate(context.records):
        measurement_id = record.payload.get("measurement_id")
        if record.event == "measurement_rebound" or measurement_id not in frozen:
            continue
        freezing = frozen[str(measurement_id)]
        if index <= freezing:
            continue
        if (
            record.event in _FROZEN_REUSE
            and complete_at.get(str(measurement_id), index) < freezing
            and under_continue(index)
        ):
            continue
        if record.event not in _FROZEN_CATCH_UP:
            problems.append((index, f"{measurement_id} is frozen and written again"))
            continue
        if record.event == "instance_attested":
            try:
                journal = context.measurement_journal(str(measurement_id))
            except _PreconditionFailed:
                problems.append((index, f"{measurement_id} is frozen and never opened one"))
                continue
            opened = {
                str(entry.payload["instance_id"])
                for entry in journal
                if "instance_id" in entry.payload
            }
            if str(record.payload["instance_id"]) not in opened:
                problems.append((index, f"{measurement_id} is frozen and never opened one"))
    return problems


def _check_epoch_identities(context: _Evidence) -> list[tuple[int, str]]:
    """Every measurement is the one its epoch derives (ADR-0020, §5.1).

    Each `measurement_bound`, `parent_measurement_bound` and
    `measurement_rebound` is re-derived from the identity of the epoch it was
    written in, on its (origin's) side and Candidate -- and, in family 12, its
    selector (G2 spec §5) -- and must carry that digest; a born journal must
    have been created under it. A family-13 `cohort_measurement_bound` is held
    to the same rule: a full-corpus derivation on its member's side (design §6.8).
    """

    problems: list[tuple[int, str]] = []
    for index, record, measurement_id in (
        (i, item, mid)
        for i, item in enumerate(context.records)
        for mid in _created_measurements(item, context.family_version)
    ):
        try:
            expected = _derive_bound_identity(context, measurement_id)
        except (_PreconditionFailed, KeyError, TypeError, ValueError):
            problems.append((index, f"{measurement_id} cannot be derived from its epoch"))
            continue
        bound_digest = (
            _bound_entry(context, measurement_id)["identity_digest"]
            if context.family_version == 15
            else record.payload["identity_digest"]
        )
        if expected.digest != bound_digest:
            problems.append((index, f"{measurement_id} is not its epoch's derived identity"))
            continue
        name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
        if name not in context.cache and not (context.run_root / name).is_file():
            continue  # declared, not born: nothing to hold to the binding yet
        try:
            journal = context.measurement_journal(measurement_id)
        except _PreconditionFailed:
            continue  # reported by whichever record cites it
        if journal[0].payload.get("identity_digest") != expected.digest:
            problems.append((index, f"{measurement_id}'s journal is not its bound identity"))
    return problems


def _born_records(context: _Evidence, measurement_id: str) -> tuple[Envelope, ...] | None:
    """The valid prefix of a born measurement journal, or `None` when it is not born."""

    # #95: the scan is a function of the bytes, which the audit reads once and
    # keeps in `cache`, so it is kept beside them; an absent journal is not.
    if measurement_id in context._born:
        return context._born[measurement_id]
    name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    raw = context.cache.get(name)
    if raw is None:
        path = context.run_root / name
        if context.family_version == 15:
            from evolution.tree.family15_audit import EvidenceMismatch, _read_owned

            try:
                raw = _read_owned(path)
            except FileNotFoundError:
                return None  # A missing owned destination is still owed birth.
            except (OSError, JournalFault, EvidenceMismatch):
                raise _PreconditionFailed("measurement evidence is unsafe or corrupt") from None
        else:
            if not path.is_file():
                return None
            raw = context.read_file(path)
        context.cache[name] = raw
    snapshot, _state = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
    records = snapshot.records or None
    context._born[measurement_id] = records
    return records


def _check_rebound_carry(context: _Evidence) -> list[tuple[int, str]]:
    """Each `measurement_rebound`'s declared carry is its frozen source's (§5.3).

    The declaration (`imported`, `pairs_digest`) is recomputed from the frozen
    source -- ADR-0017's `admit_rows` over the same in-run source, against the
    re-bound measurement's own derived identity, with the change's comparable
    fields -- and, once the measurement is born, held against its
    `sample_imported` records. The source cannot move, so any difference is
    corruption. Its `released` is recomputed the same way: the slots the
    source's latest stage and its reserved retries declared that no try of
    the frozen journal ever filled (codex branch review round 1).
    """

    from evolution.tree.baseline_import import (  # circular at module scope
        FrozenSourceCorrupt,
        admit_rows,
        import_pairs_digest,
        read_frozen_source,
    )
    from evolution.tree.campaign_measurement import (  # circular at module scope
        Measurement,
        MeasurementError,
    )

    problems: list[tuple[int, str]] = []
    fields: list[str] = []
    run_id = str(context.records[0].to_payload()["run_id"])
    latest_look: dict[str, int] = {}
    reserved: dict[str, set[tuple[str, str, int]]] = {}
    for index, record in enumerate(context.records):
        if _opens_epoch(record, family_version=context.family_version):
            fields = _changed_top_fields(record, family_version=context.family_version)
        if record.event == "stage_opened":
            latest_look[str(record.payload["measurement_id"])] = int(record.payload["look"])
        elif record.event == "indeterminate_observed" and record.payload.get("retry_follows"):
            reserved.setdefault(str(record.payload["measurement_id"]), set()).add(
                (
                    str(record.payload["case_id"]),
                    str(record.payload["sample_id"]),
                    int(record.payload["retry_ordinal"]),
                )
            )
        if record.event != "measurement_rebound":
            continue
        payload = record.payload
        measurement_id = str(payload["measurement_id"])
        source_id = str(payload["from_measurement_id"])
        declared = (int(payload["imported"]), str(payload["pairs_digest"]))
        try:
            source_binding = _measurement_binding(context, source_id)
            target = _derive_bound_identity(context, measurement_id)
            released = 0
            if _born_records(context, source_id) is None:
                carried: tuple[int, str] = (0, import_pairs_digest(()))
            else:
                source = read_frozen_source(
                    context.run_root,
                    run_id,
                    source_id,
                    expected_identity_digest=str(source_binding["identity_digest"]),
                )
                admitted = admit_rows(
                    source,
                    target,
                    primary=context.policy_for(measurement_id).metric_primary,
                    condition_changed=tuple(fields),
                    # ADR-0019 (amended 2026-10-03): the version in force at
                    # the `measurement_rebound`.
                    primary_range=primary_range_required(
                        context.family_version, context.version_before(index)
                    ),
                )
                carried = (admitted.imported, admitted.pairs_digest)
                if source_id in latest_look:
                    frozen = Measurement.open_frozen(context.run_root, source_id)
                    look = latest_look[source_id]
                    declined: frozenset[tuple[str, str, int]] = frozenset()
                    look = stage_horizon(
                        context.policy_for(source_id).looks, look, frozen.max_repeat
                    )
                    before = context.snapshot_before(index)
                    declined = declined_tries(before.retries_declined, source_id) | restored_tries(
                        before.listing_restored, source_id
                    )
                    if context.family_version == 15 and any(
                        stage.get("scope") for stage in before.measurements[source_id]["stages"]
                    ):
                        from evolution.tree.campaign_measurement import unfilled_slots
                        from evolution.tree.campaign_view import owed_rows

                        released = unfilled_slots(
                            owed_rows(before, source_id, opened_only=True),
                            _PrefixReads(context.journal_state(source_id)),
                            look,
                            reserved.get(source_id, set()),
                            declined,
                        )
                    else:
                        released = frozen.unfilled_slots(
                            look, reserved.get(source_id, set()), declined
                        )
                    released += _unspent_reservation(
                        context.threshold_point(record), source_id, frozen
                    )
        except _Unverifiable:
            continue
        except (
            _PreconditionFailed,
            FrozenSourceCorrupt,
            MeasurementError,
            KeyError,
            TypeError,
            ValueError,
        ):
            problems.append((index, f"the frozen source {source_id} cannot be read back"))
            continue
        if carried != declared:
            problems.append((index, f"{measurement_id}'s pairs_digest is not its frozen source's"))
            continue
        if released != int(payload["released"]):
            problems.append((index, f"{measurement_id}'s released is not its frozen source's"))
            continue
        try:
            born = _born_records(context, measurement_id)
        except _PreconditionFailed:
            problems.append(
                (index, f"the rebound destination {measurement_id} is unsafe or corrupt")
            )
            continue
        if born is None:
            continue  # an owed copy: declared, not yet born
        imported = [entry.payload for entry in born if entry.event == "sample_imported"]
        if (len(imported), import_pairs_digest(imported)) != declared:
            problems.append((index, f"{measurement_id}'s carried rows are not its declaration"))
    return problems


def _unspent_reservation(point: _ThresholdPoint, source_id: str, frozen: Any) -> int:
    """#78 (spec §3.5): the current window's threshold slots whose repairs never ran.

    The executor's `CampaignRun._unspent_reservation`, over the lifecycle as it
    stood before the `measurement_rebound`: a reservation made since the
    latest suspension holds one slot per granted pair whose repair has not
    started, or started with no terminal; one of an earlier window was never
    counted in this one.
    """

    reservation = point.repairs_reserved.get(source_id)
    if reservation is None or point.window_start is None or reservation[0] < point.window_start:
        return 0
    return min(reservation[1], int(frozen.unfilled_threshold_repairs(point.named(source_id))))


def _condition_findings(context: _Evidence) -> list[dict[str, Any]]:
    """I2 spec §5: the audit of in-place condition changes, family 7 only.

    Epoch-derived identities, block order and contiguity (Attempt closures and
    `round_closed{condition_changed}` inside `rebaseline` blocks only), each
    re-bound measurement's carry against its frozen source and its born
    journal, and no write to a frozen measurement after its change. A run
    without a change has no block, no re-bind and nothing frozen; its
    measurements are re-derived from epoch `0`, the creation identity.
    """

    if not context.records:
        return []
    findings: list[dict[str, Any]] = []
    for code, rule in (
        ("precondition_failed", _check_epoch_identities),
        (
            "precondition_failed",
            lambda context: _check_block_order(
                context.records, family_version=context.family_version
            ),
        ),
        ("baseline_import_mismatch", _check_rebound_carry),
        ("precondition_failed", _check_frozen_untouched),
    ):
        for index, detail in rule(context):
            findings.append(_campaign_finding(context, index, code, detail))
    return findings


def _attempt_at(context: _Evidence, record: Envelope) -> tuple[int, CampaignSnapshot, AttemptState]:
    """`(index, snapshot, attempt)`: the record's Attempt, reduced over the prefix before it."""

    index = context.records.index(record)
    snapshot = reduce(context.records[:index], selector=None, family_version=context.family_version)
    state = snapshot.rounds.get(int(record.payload["round"]))
    attempt = None if state is None else state.attempts.get(str(record.payload["attempt"]))
    if attempt is None or attempt.candidate_digest is None:
        raise _PreconditionFailed("the record names no sealed Attempt of its round")
    return index, snapshot, attempt


def _check_candidate_inspected(context: _Evidence, record: Envelope) -> None:
    """Family 12 (G2 spec §4.5): the inspection journal declares exactly the recorded map.

    `inspections/<attempt>-i<k>/events.jsonl` exists and is born, its identity
    is the Attempt's derived family-12 measurement identity in the epoch in
    force at the record (so it names the Attempt's Candidate), and it holds a `components_declared` of the
    recorded map's digest -- or, for a `null` map, no declaration at all.
    """

    payload = record.payload
    _index, reduced, attempt = _attempt_at(context, record)
    path = (
        inspection_path(context.run_root, str(payload["attempt"]), int(payload["inspection"]))
        / EVENTS_FILE
    )
    if not path.is_file():
        raise _PreconditionFailed("the inspection journal is missing")
    try:
        raw = context.read_file(path)
    except OSError:
        raise _PreconditionFailed("the inspection journal is unreadable") from None
    snapshot, _state = _scan(raw, MAX_RECORD_BYTES, Lifecycle, S1_FAMILY)
    if snapshot.journal_integrity != "complete" or not snapshot.records:
        raise _PreconditionFailed("the inspection journal is not a valid journal")
    records = snapshot.records
    if records[0].payload["identity"]["candidate_digest"] != attempt.candidate_digest:
        raise _PreconditionFailed("the inspection journal did not load the Attempt's Candidate")
    # The campaign inspects under `derive(...)` of the identity in force at the
    # selection: the family-12 measurement identity of the Attempt's Candidate
    # in the epoch the reduced prefix holds.
    try:
        expected = derived_identity(
            CampaignIdentityV1.from_payload(dict(reduced.identity)),
            str(attempt.candidate_digest),
            "candidate",
        )
    except (KeyError, TypeError, ValueError):
        raise _PreconditionFailed("the Attempt's inspection identity cannot be derived") from None
    if records[0].payload.get("identity_digest") != expected.digest:
        raise _PreconditionFailed("the inspection journal is not the Attempt's derived identity")
    declared = {
        str(entry.payload["components_digest"])
        for entry in records
        if entry.event == "components_declared"
    }
    components = payload["components"]
    if components is None:
        if declared:
            raise _PreconditionFailed("the inspection declared a map the record says is absent")
    elif components_digest(components) not in declared:
        raise _PreconditionFailed("the inspection journal never declared the recorded map")


def _same_run_records(
    context: _Evidence, snapshot: CampaignSnapshot, measurement_id: str
) -> tuple[Envelope, ...] | None:
    """`_Campaign.same_run_records` over the audit's bytes.

    A measurement `snapshot` bound whose journal is born yields its valid
    prefix (what `inspect_journal(...).records` gives the campaign: a torn
    tail is the ordinary crash artefact); anything else is `None`.
    """

    if measurement_id not in snapshot.measurements:
        return None
    return _born_records(context, measurement_id)


def _baseline_prefix(
    context: _Evidence, index: int, round_number: int, measurement_id: str
) -> tuple[Envelope, ...]:
    """The round's baseline as its `baseline_complete` cited it (§4.4).

    Read through `cited_baseline_prefix`, the same bound the campaign's
    selection reads, so a later append to the baseline journal moves neither.
    """

    def read_file(expected: str) -> bytes:
        raw = context.cache.get(expected)
        if raw is None:
            path = context.run_root / expected
            if not path.is_file():
                raise _PreconditionFailed("the measurement journal is missing")
            raw = context.read_file(path)
            context.cache[expected] = raw
        return raw

    try:
        return cited_baseline_prefix(
            context.records[:index], round_number, measurement_id, read_file
        )
    except BaselineUnavailable as exc:
        raise _PreconditionFailed(str(exc)) from exc


def _check_affected_cases_selected(context: _Evidence, record: Envelope) -> None:
    """Family 12 (G2 spec §4.4): the record is `select_affected` over durable inputs.

    The inputs are rebuilt as of the record's position with the executor's own
    readers (`_Campaign.select_attempt_cases`): the identity in force from the
    reduced prefix, the round's baseline prefix its `baseline_complete` cites
    and the valid prefixes of the same-run journals its imported rows name, the two published copies under the store's own
    policy, the adaptation identity in force, and the Attempt's latest
    inspected map. A Target failure leaves nothing durable, so an
    `inspection_failed` claim is accepted only when nothing contradicts it:
    no `candidate_inspected` precedes it, and the rebuilt inputs would have
    inspected (`reported`, component mode, no earlier fallback).
    """

    payload = record.payload
    index, snapshot, attempt = _attempt_at(context, record)
    attempt_id = str(payload["attempt"])
    state = snapshot.rounds[int(payload["round"])]
    if state.baseline_measurement_id is None:
        raise _PreconditionFailed("the selection's round has no baseline")
    kind = str(snapshot.identity["policy"]["gate"]["provenance"]["kind"])
    corpus_ids = tuple(str(case["case_id"]) for case in snapshot.identity["corpus_cases"])

    baseline, known = baseline_inputs(
        _baseline_prefix(context, index, int(payload["round"]), str(state.baseline_measurement_id)),
        snapshot.run_id,
        lambda measurement_id: _same_run_records(context, snapshot, measurement_id),
    )
    artifacts = context.run_root / ARTIFACTS_DIR
    parent_root = artifacts / CHAMPIONS / str(attempt.parent_version)
    child_root = artifacts / CANDIDATES / attempt_id
    if not parent_root.is_dir() or not child_root.is_dir():
        raise _PreconditionFailed("a published copy the selection compares is missing")
    try:
        changes = tree_changes(parent_root, child_root, context.store_policy())
    except OSError:
        raise _PreconditionFailed("a published copy the selection compares is unreadable") from None
    claimed = payload["fallback_reason"] == "inspection_failed" and attempt.inspections == 0
    if claimed:
        mode, _digest, fallback = parent_mode(baseline, known)
        if kind != "reported" or mode != "component" or fallback is not None:
            raise _PreconditionFailed("the selection claims an inspection that was never due")
    expected = select_affected(
        kind=kind,
        corpus_ids=corpus_ids,
        baseline=baseline,
        known=known,
        changes=changes,
        labels=context.adaptation_identity(before=index).provenance_labels,
        candidate_map=attempt.inspected_components,
        inspection_failed=claimed,
    )
    if expected.payload(int(payload["round"]), attempt_id, kind, corpus_ids) != dict(payload):
        raise _PreconditionFailed("the affected-case selection differs from its re-derivation")


def _read_faults_reported(
    check: Callable[[_Evidence, Envelope], None],
) -> Callable[[_Evidence, Envelope], None]:
    """`check`, with a durable read that fails reported rather than raised.

    The family-12 checks read published copies (`scan_tree` refuses a symlink
    or special file with `ArtifactUnsafeError`) and journals outside the cited
    evidence; `verify_evidence` reports and never raises, so a tampered copy or
    an unreadable journal is a finding against the record that relied on it.
    """

    def checked(context: _Evidence, record: Envelope) -> None:
        try:
            check(context, record)
        except ArtifactError as exc:
            raise _PreconditionFailed(f"an artifact cannot be read back ({exc.code})") from None
        except JournalFault as exc:
            raise _PreconditionFailed(f"a journal cannot be read back ({exc})") from None

    return checked


# --- family 14: the connection preflight (H10; F0 R60, C§6.1, ADR-0029 as amended) -----

#: C§3.2's Target-field codes: the rollout completed and the Scorer never ran.
_CONNECTION_FIELD_DETAILS = frozenset(
    {
        "connection_event_missing",
        "connection_field_missing",
        "connection_field_empty",
        "connection_field_type",
        "connection_field_nonfinite",
    }
)
#: The incompatibilities a failed Scorer call yields: its output codes (R55).
_CONNECTION_SCORER_DETAILS = frozenset(
    {
        "connection_scorer_output_invalid",
        "connection_primary_type",
        "connection_primary_nonfinite",
    }
)
#: The `unavailable` causes a failed Scorer call yields: its operational codes,
#: or `scorer_failed` when it reported none (`connection_probe.classify`).
_CONNECTION_SCORER_CAUSES = _SCORER_OPERATIONAL | {SCORER_FAILED_CAUSE}
#: The S1 starts of a try's session operations. A start with none of them did
#: not execute locally, such as the synthetic pair `refuse_rows` writes.
_SESSION_EVENTS = frozenset(
    {
        "target_open_started",
        "environment_next_started",
        "target_send_started",
        "target_close_started",
    }
)
_UNAVAILABLE = "connection_preflight_unavailable"
_INCONCLUSIVE = "connection_preflight_inconclusive"
_INCOMPATIBLE = "connection_incompatible"


def _check_connection_started(context: _Evidence, record: Envelope) -> None:
    """F0 R60, C§4.1: a preflight's binding is its condition epoch's.

    The start's `target`, `environment`, `scorer`, `metric_contract`,
    `primary_metric`, `case_id` and `case_index` must equal the fields
    `connection_probe.epoch_binding` derives from the epoch identity in force
    at the start. The gate derives `D` the same way, so the case must be the
    first train case. `binding_digest` must then re-derive from those fields
    and the epoch's train corpus digest, which the start does not carry. The
    lifecycle checks the epoch number, the contract, the primary metric and
    the seed; only the audit re-derives the digest.
    """

    payload = record.payload
    epochs = context.snapshot_before(context.records.index(record)).epochs
    epoch = int(payload["epoch"])
    if not 0 <= epoch < len(epochs):
        raise _PreconditionFailed("the preflight names no condition epoch of its prefix")
    fields = epoch_binding(epoch, epochs[epoch])
    for name in (
        "target",
        "environment",
        "scorer",
        "metric_contract",
        "primary_metric",
        "case_id",
        "case_index",
    ):
        if payload[name] != fields[name]:
            raise _PreconditionFailed(f"the preflight's {name} is not its condition epoch's")
    if payload["binding_digest"] != binding_digest(**fields):
        raise _PreconditionFailed(
            "the binding digest does not re-derive from the preflight's fields"
        )


def _probe_reached(records: Sequence[Envelope], primary: str) -> str:
    """How far the probe journal shows the probe got, read from record kinds and codes.

    The probe schedules one row, optionally retried; its last try decides.
    The result is `materialized` (a
    finished materialize and a finished release, no rollout: an identity or
    attestation failure, or a description that held a run secret, #287),
    `no_rollout` (any other probe that never started its rollout, such as a
    failed materialize), the rollout's own scoreless outcome,
    `fields` (completed, never scored: a Target field failed), `scorer_failed`,
    `primary_missing`, `scored`, or `unfinished` (no outcome: stopped,
    interrupted or open). The only Scorer output it reads is whether the
    finished metrics name the primary. It never reads a reply, an event value
    or a score.
    """

    last = {record.event: record for record in records}
    if "rollout_started" not in last:
        if "target_materialize_finished" in last and "target_release_finished" in last:
            return "materialized"
        return "no_rollout"
    # A declared retry's last start owns the classification, including a
    # failed last try after an earlier completed one.
    start = max(i for i, r in enumerate(records) if r.event == "rollout_started")
    last = {record.event: record for record in records[start:]}
    terminal = last.get("rollout_finished") or last.get("rollout_failed")
    if terminal is None:
        return "unfinished"
    outcome = str(terminal.payload["outcome"])
    if outcome != "completed":
        scoreless = _UNAVAILABLE_TERMINALS | _INCONCLUSIVE_TERMINALS
        return outcome if outcome in scoreless else "unfinished"
    if "scoring_started" not in last:
        return "fields"
    if "scoring_failed" in last:
        return "scorer_failed"
    finished = last.get("scoring_finished")
    if finished is None:
        return "unfinished"
    return "scored" if primary in finished.payload["metrics"] else "primary_missing"


def _check_connection_terminal(context: _Evidence, record: Envelope) -> None:
    """F0 R60, C§4.4: a preflight terminal agrees with the probe journal it cites.

    The reference check has already proven the cited prefix's bytes. Over
    that prefix, S1's own records must give:

    - the terminal's `cleanup`, by the runner's own rule (`_cleanup_of`);
    - for `_finished`: a completed, scored rollout whose metrics name the
      primary, and a finished release;
    - for `_failed`, unless its cleanup failed (that class wins whatever the
      probe reached, `classify` step 1): the `status` of what the probe
      reached; the rollout terminal as the `cause_code` of an `unavailable`
      or `inconclusive` rollout; an operational Scorer cause or a Scorer
      output detail after a failed Scorer call; a Target-field detail for a
      completed rollout that was never scored; `connection_primary_missing`
      for a score without the primary; `unavailable`, or
      `connection_description_secret`, for a probe that materialized and
      released its instance and started no rollout (#287). The description
      is in no journal, so the audit admits that code from the journal's
      shape and cannot re-derive the match.

    Before any of that, a `_failed` terminal's `cause_code` must be in its
    status's closed family-14 set (`connection_probe.CAUSE_CODES`, maintainer
    ruling 2026-10-12), even where no probe-journal fact could contradict it.

    `_interrupted` is checked for its cleanup only. Under a stop, a probe that
    computed a failure is written `interrupted`, and its trace keeps that
    failure (controller rulings 14, 19, 21). The audit never re-scores the
    probe. A terminal that cites no probe journal leaves nothing to compare.
    """

    payload = record.payload
    if record.event == CONNECTION_FAILED_EVENT:
        status, cause = payload["status"], payload["cause_code"]
        known = CAUSE_CODES.get(status, frozenset()) if isinstance(status, str) else frozenset()
        if cause not in known and not (status == _INCOMPATIBLE and cause is None):
            raise _PreconditionFailed("the terminal's cause is not in the family-14 vocabulary")
    if not payload["evidence"]:
        return
    records, _state = context.measurement_state(payload["evidence"][0])
    if payload["cleanup"] != _cleanup_of(records):
        raise _PreconditionFailed("the terminal's cleanup is not the probe journal's")
    if record.event == CONNECTION_INTERRUPTED_EVENT:
        return
    start = next(
        (
            earlier
            for earlier in context.records[: context.records.index(record)]
            if earlier.event == CONNECTION_START_EVENT
            and earlier.operation_id == record.operation_id
        ),
        None,
    )
    if start is None:
        raise _PreconditionFailed("the terminal closes no connection preflight")
    reached = _probe_reached(records, str(start.payload["primary_metric"]))
    if record.event == CONNECTION_FINISHED_EVENT:
        if reached != "scored" or not any(r.event == "target_release_finished" for r in records):
            raise _PreconditionFailed(
                "a compatible terminal needs a released instance and a scored rollout "
                "naming the primary metric"
            )
        return
    if payload["cleanup"] == "failed":
        return
    if reached == "unfinished":
        raise _PreconditionFailed("the probe journal records no outcome")
    status = payload["status"]
    if reached == "materialized":
        expected = status if status in (_UNAVAILABLE, _INCOMPATIBLE) else _UNAVAILABLE
    elif reached in ("no_rollout", *_UNAVAILABLE_TERMINALS):
        expected = _UNAVAILABLE
    elif reached in _INCONCLUSIVE_TERMINALS:
        expected = _INCONCLUSIVE
    elif reached == "scorer_failed":
        expected = status if status in (_UNAVAILABLE, _INCOMPATIBLE) else _UNAVAILABLE
    elif reached in ("fields", "primary_missing"):
        expected = _INCOMPATIBLE
    else:
        expected = "compatible"
    if status != expected:
        raise _PreconditionFailed("the terminal's status is not the probe journal's")
    cause = payload["cause_code"]
    if reached in _UNAVAILABLE_TERMINALS | _INCONCLUSIVE_TERMINALS and cause != reached:
        raise _PreconditionFailed("the terminal's cause is not the probe journal's")
    if reached == "scorer_failed" and status == _UNAVAILABLE:
        if cause not in _CONNECTION_SCORER_CAUSES:
            raise _PreconditionFailed("the terminal's cause is not the probe journal's")
        return
    details = {
        "materialized": frozenset({"connection_description_secret"}),
        "fields": _CONNECTION_FIELD_DETAILS,
        "scorer_failed": _CONNECTION_SCORER_DETAILS,
        "primary_missing": frozenset({"connection_primary_missing"}),
    }
    if status == _INCOMPATIBLE and payload["detail_code"] not in details[reached]:
        raise _PreconditionFailed("the terminal's detail is not the probe journal's")


def _start_finding(
    context: _Evidence,
    measurement_id: str,
    records: Sequence[Envelope],
    position: int,
    detail: str,
) -> dict[str, Any]:
    """A connection finding at one measurement journal's `rollout_started`.

    As in `_repair_findings`, `sequence` and `event` name the campaign record
    that binds the measurement. The locator names the start's own bytes.
    """

    name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
    start = sum(len(record.encode()) for record in records[:position])
    end = start + len(records[position].encode())
    index = next(
        (
            at
            for at, record in enumerate(context.records)
            if record.payload.get("measurement_id") == measurement_id
        ),
        None,
    )
    return dict(
        sequence=None if index is None else index + 1,
        event=None if index is None else context.records[index].event,
        locator=f"{name}#bytes={start}:{end}",
        code="precondition_failed",
        detail=detail,
    )


#: A located `rollout_started`: its measurement, that journal's records, its position.
_ConnectionStart = tuple[str, Sequence[Envelope], int]


def _try_key(payload: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return payload["case_id"], payload["sample_id"], payload.get("retry_ordinal")


def _connection_findings(context: _Evidence) -> list[dict[str, Any]]:
    """F0 R60, C§6.1, ADR-0029 (amended 2026-10-09): every binding's admission, by naming.

    Current families. A binding is keyed by its condition epoch: `D` is
    `connection_binding/1` of the epoch identity (`epoch_binding`). A
    preflight belongs to its start's epoch. A measurement journal's starts
    belong to the epoch its measurement was bound in. `_check_connection_started`
    proves that each start's recorded digest is that `D`. So a forged digest is
    reported once, there, and not again for every admission that names the
    preflight.

    Over every born measurement journal, for each binding `D`:

    - every `rollout_started` names `D` (`connection_binding_digest`);
    - exactly one start carries an admission. Each further one is reported;
    - in the admitted start's own journal, no earlier start under `D`
      executed a session. A start with no session operation, such as the
      synthetic starts `refuse_rows` writes, did not execute locally;
    - the admission names a preflight of `D` that closed
      `connection_preflight_finished`, whose `invocation_id` is the
      admission's `controller_invocation_id`;
    - that preflight is the last preflight of `D` in campaign-journal order.
      An earlier failed or interrupted one is the admitted recovery path;
    - a binding whose starts executed a session with no admission is
      reported.

    Imported rows write no `rollout_started` and are never read. The audit
    cannot order a start in one journal against another journal's start, or
    against a campaign record written after the compatible terminal (F0 R60).
    So "first" is checked within one journal, and "no later preflight" in
    campaign order only. Family 15 exempts admission-less starts when their
    measurement had an off or settled-shadow window while open. A settlement
    of any failed preflight of the binding counts, not only its last probe.

    A born measurement journal that cannot be read hides its starts. For its
    binding, of the binding-level conclusions, only "the binding's executed
    starts carry no admission" is withheld, since the admission may sit in that
    journal; the unreadable journal's own starts are not judged. Every
    admission in a readable journal of the binding is still judged. Where that conclusion is withheld,
    each unreadable journal of the binding is reported in its place, at the
    record that binds it, so a withheld conclusion is never silent. Otherwise
    an unreadable journal is left to the records that cite it: an uncited torn
    tail stays ignored (H3).
    """

    if not context.records:
        return []
    snapshot = context.snapshot_before(len(context.records))
    bindings: dict[int, str | None] = {}

    def binding(epoch: int) -> str | None:
        if epoch not in bindings:
            try:
                bindings[epoch] = binding_digest(**epoch_binding(epoch, snapshot.epochs[epoch]))
            except (IndexError, KeyError, TypeError, ValueError):
                bindings[epoch] = None
        return bindings[epoch]

    preflights: dict[str, tuple[int, str | None, Mapping[str, Any]]] = {}
    terminals: dict[str, str] = {}
    order: dict[str | None, list[str]] = {}
    for index, record in enumerate(context.records):
        operation = str(record.operation_id)
        if record.event == CONNECTION_START_EVENT:
            key = binding(int(record.payload["epoch"]))
            preflights[operation] = (index, key, record.payload)
            order.setdefault(key, []).append(operation)
        elif record.event in CONNECTION_TERMINALS:
            terminals[operation] = record.event
    # F0 R60: no exact ordering joins an S1 start to a campaign rebase.
    # Admit the missing token only if this measurement had a permissive
    # window while open; a later mode change cannot excuse a closed one.
    admission_optional: set[str] = set()
    if context.family_version == 15:
        settled_bindings: set[str | None] = set()
        for index, record in enumerate(context.records):
            hit = (
                record.event == "check_shadowed"
                and record.payload["check"] == "connection_preflight"
            )
            if hit:
                operation = str(record.payload["operation_id"])
                if (
                    operation in preflights
                    and terminals.get(operation) == "connection_preflight_failed"
                ):
                    settled_bindings.add(preflights[operation][1])
            if (
                not record.event.endswith("_bound")
                and record.event not in {"identity_rebased", "measurement_rebound"}
                and not hit
            ):
                continue
            state = context.snapshot_before(index + 1)
            assert state.family15 is not None
            mode = state.family15.check_modes.get("connection_preflight", "enforce")
            if mode == "enforce":
                continue
            for mid, entry in state.measurements.items():
                if entry["complete"] or entry.get("frozen", False):
                    continue
                if mode == "off" or binding(int(entry["epoch"])) in settled_bindings:
                    admission_optional.add(mid)
    findings: list[dict[str, Any]] = []
    admitted: dict[str | None, list[tuple[_ConnectionStart, bool]]] = {}
    executed: dict[str | None, _ConnectionStart] = {}
    unreadable: dict[str | None, list[dict[str, Any]]] = {}
    for measurement_id, entry in snapshot.measurements.items():
        name = f"{MEASUREMENTS_DIR}/{measurement_id}/{EVENTS_FILE}"
        if name not in context.cache and not (context.run_root / name).is_file():
            continue
        key = binding(int(entry["epoch"]))
        try:
            records = context.measurement_journal(measurement_id)
        except _PreconditionFailed:
            # H10 limitation (b): its starts are unknown, so a missing admission
            # of its binding could be misattributed; only that one is withheld,
            # and this finding stands in its place.
            bound = next(
                (
                    at
                    for at, record in enumerate(context.records)
                    if record.payload.get("measurement_id") == measurement_id
                ),
                None,
            )
            unreadable.setdefault(key, []).append(
                dict(
                    sequence=None if bound is None else bound + 1,
                    event=None if bound is None else context.records[bound].event,
                    locator=f"{name}#bytes=0:{len(context.cache.get(name, b''))}",
                    code="precondition_failed",
                    detail="a measurement journal under the binding cannot be read",
                )
            )
            continue
        sessions = {_try_key(row.payload) for row in records if row.event in _SESSION_EVENTS}
        earlier = False
        for position, row in enumerate(records):
            if row.event != "rollout_started":
                continue
            if key is None or row.payload.get("connection_binding_digest") != key:
                findings.append(
                    _start_finding(
                        context,
                        measurement_id,
                        records,
                        position,
                        "the start names another connection binding than its measurement's epoch",
                    )
                )
                continue
            located: _ConnectionStart = (measurement_id, records, position)
            if row.payload.get("connection_admission") is None and (
                measurement_id in admission_optional
            ):
                continue
            if row.payload.get("connection_admission") is not None:
                admitted.setdefault(key, []).append((located, earlier))
            if _try_key(row.payload) in sessions:
                earlier = True
                executed.setdefault(key, located)
    for key in dict.fromkeys([*admitted, *executed]):
        found = admitted.get(key)
        if not found:
            if key in unreadable:
                findings.extend(unreadable[key])
                continue
            findings.append(
                _start_finding(
                    context, *executed[key], "the binding's executed starts carry no admission"
                )
            )
            continue
        (start, after_executed), *extra = found
        for other, _after in extra:
            findings.append(
                _start_finding(
                    context, *other, "a second start under the binding carries an admission"
                )
            )
        if after_executed:
            findings.append(
                _start_finding(
                    context,
                    *start,
                    "an earlier start under the binding executed a session in the admitted "
                    "start's journal",
                )
            )
        _measurement, journal, position = start
        admission = journal[position].payload["connection_admission"]
        operation = str(admission["operation_id"])
        named = preflights.get(operation)
        if named is None:
            detail = "the admission names no connection preflight"
        elif named[1] != key:
            detail = "the admission names a preflight of another binding"
        elif terminals.get(operation) != CONNECTION_FINISHED_EVENT:
            detail = "the admission names a preflight that is not compatible"
        elif named[2]["invocation_id"] != admission["controller_invocation_id"]:
            detail = "the admission names another invocation than its preflight's"
        else:
            later = order[key][order[key].index(operation) + 1 :]
            if later:
                findings.append(
                    _campaign_finding(
                        context,
                        preflights[later[0]][0],
                        "precondition_failed",
                        "a later preflight of the binding follows its admitted preflight",
                    )
                )
            continue
        findings.append(_start_finding(context, *start, detail))
    return findings


#: Family 12: records re-checked from the campaign log itself. They cite no
#: `evidence` -- what they claim is re-derived from other durable bytes -- so,
#: like `campaign_suspended`, they get no reference-integrity pass and locate
#: themselves; they are not E records (an E record must carry `evidence`).
#: Family 13's calibration records cite their prefix in the payload
#: (`prefix`, design §6.3-§6.4), not as `evidence`, so they locate
#: themselves too; `journal_citations` already counts that prefix (ADR-0019).
_SELF_LOCATED = frozenset(
    {
        "campaign_suspended",
        "candidate_inspected",
        "affected_cases_selected",
        "noise_calibration_bound",
        "noise_calibration_completed",
        # H10: a preflight start cites nothing; its binding re-derives from the log.
        CONNECTION_START_EVENT,
    }
)


#: One re-check per E record (ADR-0008). A record whose kind is absent here — or
#: whose check raises `_Unverifiable` — is verified for reference integrity only.
_PRECONDITIONS: Mapping[str, Any] = MappingProxyType(
    {
        "baseline_complete": _check_measurement_complete,
        "champion_complete": _check_measurement_complete,
        "attempt_sealed": _check_attempt_sealed,
        "instance_attested": _check_instance_attested,
        "indeterminate_observed": _check_indeterminate_observed,
        "look_not_started": _check_look_not_started,
        "gate_scope_selected": _check_gate_scope_selected,
        "threshold_reached": _check_threshold_reached,
        "selection_decided": _check_selection_decided,
        "attempt_input_frozen": _check_attempt_input_frozen,
        "finalists_bound": _read_faults_reported(_check_finalists_bound),
        "final_measurement_bound": _read_faults_reported(_check_final_measurement_bound),
        "round_closed": _check_round_closed,
        "campaign_finished": _check_campaign_finished,
        "pairs_excluded": _check_pairs_excluded,
        "repairs_dispatched": _check_repairs_dispatched,
        "stage_redispatched": _check_stage_redispatched,
        "campaign_suspended": _check_campaign_suspended,
        "candidate_inspected": _read_faults_reported(_check_candidate_inspected),
        "affected_cases_selected": _read_faults_reported(_check_affected_cases_selected),
        "comparison_bound": _read_faults_reported(_check_comparison_bound),
        "noise_calibration_bound": _read_faults_reported(_check_noise_calibration_bound),
        "noise_calibration_completed": _read_faults_reported(_check_noise_calibration_completed),
        "comparison_look_evaluated": _read_faults_reported(_check_comparison_look_evaluated),
        **{
            campaign_vocabulary_for(family).measurement_completed: _read_faults_reported(
                _check_cohort_measurement_completed
            )
            for family in (14,)
        },
        "global_comparison_completed": _read_faults_reported(_check_global_comparison_completed),
        "baseline_refresh_bound": _read_faults_reported(_check_baseline_refresh_bound),
        "baseline_refresh_completed": _read_faults_reported(_check_baseline_refresh_completed),
        CONNECTION_START_EVENT: _check_connection_started,
        **dict.fromkeys(CONNECTION_TERMINALS, _read_faults_reported(_check_connection_terminal)),
    }
)


def _precondition_finding(context: _Evidence, record: Envelope) -> tuple[str, str] | None:
    """Re-evaluate one E record's precondition: `(code, detail)`, or `None` when it holds."""

    check = _PRECONDITIONS.get(record.event)
    if check is None:  # pragma: no cover - every E record has a check
        return None
    try:
        check(context, record)
    except _PreconditionFailed as failed:
        return failed.code, failed.detail
    except _Unverifiable:
        return None
    except (KeyError, IndexError, TypeError, ValueError):
        # A payload the campaign family admitted but this check cannot read is a
        # forgery too: the record claims a fact its own evidence cannot express.
        code = "pairs_excluded_mismatch" if record.event == "pairs_excluded" else None
        return (
            code or "precondition_failed",
            "the evidence does not have the shape this record's precondition needs",
        )
    return None


def _refused_suffix(run_root: Path, log: Any) -> list[dict[str, Any]]:
    """Report a tail the campaign lifecycle refused as a finding of its own.

    `inspect_log` stops at the first record the lifecycle will not admit and
    hands back the prefix; a caller reading `verification` alone would otherwise
    get a clean bill for a journal whose tail was thrown away. The refusal's own
    message is recovered by replaying the admitted prefix through a fresh
    lifecycle and offering it the first refused record.
    """

    if log.journal_integrity == "complete":
        return []
    raw = (run_root / CAMPAIGN_FILE).read_bytes()[log.valid_bytes :]
    code = log.journal_integrity
    detail = log.journal_integrity
    if code == "corrupt_suffix":
        # A record the lifecycle refused: recover its own message by replaying
        # the admitted prefix through a fresh lifecycle and offering it the
        # record. A *torn* last line is a different thing entirely -- the
        # ordinary crash artefact the append log's tail rule repairs and resume
        # handles -- so it keeps the scan's own label and is never called a
        # refusal (round-4 re-run, TT-1).
        code = "suffix_refused"
        try:
            family_version = _reader_family_version(run_root)
            state = CampaignLifecycle(selector=None, family_version=family_version)
            for admitted in log.records:
                state.accept(admitted)
            state.prepare(
                Envelope.from_bytes(
                    raw.split(b"\n", 1)[0] + b"\n",
                    family=campaign_family_for(family_version),
                )
            )
        except SchemaError as refusal:
            detail = str(refusal)
        except (ValueError, TypeError, IndexError):
            detail = "the refused record could not be read"
    return [
        dict(
            sequence=log.sequence + 1,
            event=None,
            locator=f"{CAMPAIGN_FILE}#bytes={log.valid_bytes}:{len(raw) + log.valid_bytes}",
            code=code,
            detail=detail,
        )
    ]


def _verify_reference(
    run_root: Path, reference: Mapping[str, Any], cache: dict[str, bytes], *, nofollow: bool = False
) -> str | None:
    match = _LOCATOR.fullmatch(str(reference["locator"]))
    if match is None:
        return "malformed_locator"
    name = match["file"]
    # `execution.durable_reference._validate_locator` bars a locator that escapes the run at
    # append time, but this function reads any run root -- including one another
    # writer produced -- so the safety half of the rule is re-applied before the
    # locator becomes a path, and reported like any other mismatch.
    if name.startswith("/") or ".." in name.split("/"):
        return "malformed_locator"
    raw = cache.get(name)
    if raw is None:
        path = run_root / name
        if not path.is_file():
            return "evidence_missing"
        if nofollow:
            from evolution.tree.family15_audit import EvidenceMismatch, _read_owned

            try:
                raw = _read_owned(path)
            except (OSError, JournalFault, EvidenceMismatch):
                return "evidence_unsafe"
        else:
            raw = path.read_bytes()
        cache[name] = raw
    start, end = int(match["start"]), int(match["end"])
    if end > len(raw):
        return "evidence_missing"
    if hashlib.sha256(raw[start:end]).hexdigest() != reference["content_digest"]:
        return "evidence_mismatch"
    return None


#: Unambiguous aliases for `evolution.tree`'s flat namespace, where S1 already
#: exports `project_result` / `project_status` for a measurement.
project_campaign_result = project_result
project_campaign_status = project_status

__all__ = [
    "CampaignProjection",
    "champion_measurement",
    "inspect_view",
    "project_campaign_result",
    "project_campaign_status",
    "project_result",
    "project_status",
    "read_run_id",
    "verify_evidence",
]
