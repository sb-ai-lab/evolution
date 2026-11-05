"""The read-only campaign view and the pure next-action planner (spec §2, §6, §11).

The executor keeps no campaign state of its own. Everything it decides it decides
over a `CampaignView`: the campaign snapshot (the reduction of `campaign.jsonl`),
the S1 status projection of every bound measurement, the S2 lineage projection and
the absent/unborn/born classification of every declared sibling (spec §4).
`next_actions(view, policy)` is a pure function of that view and names what the
executor should do next; `assemble` builds the view from the live objects.

The view invariants in this module amend the
task brief:

1. `CampaignView(snapshot, measurements, lineage, siblings)` — `measurements`
   maps a measurement id to its S1 status *payload* (a plain mapping, so the view
   is inert data), `lineage` is the S2 `LineageProjection` or `None` while the
   store is unborn, and `siblings` classifies `"artifact_store"` plus every
   declared measurement id.
2. `assemble` takes a keyword-only `siblings` override. Classifying a sibling
   reads its whole log, and the executor re-assembles the view after every
   durable step; the executor therefore classifies once (at creation and at
   resume) and passes the cached mapping. `siblings=None` classifies from disk.
3. The planner's action set adds `BindBaseline`, `CompleteBaseline` and
   `CompleteWinner` to the kinds the brief listed. The brief folded a barrier's
   *closing* record into the stage action before it; splitting them out is what
   lets a resumed campaign that crashed after its last stage write the closing
   record without re-running a stage.
4. A `CloseAttempt` action names only the outcomes the planner can derive from
   the campaign prefix alone — an editor terminal and a decided look. The
   consequences that need sibling facts (a campaign-level fault sample, an
   indeterminate sample, the critical veto, an unevaluable look) are the
   executor's, decided when the stage that proves them completes (spec §6).

5. `SelectFocus`, `RunReflection` and `SelectGateScope` are the round's
   reflector prelude (spec §3.3) and are campaign-level, not attempt-level:
   they are outside `ATTEMPT_ACTIONS`, so `drive()` performs them itself, one
   at a time, before any attempt of the round may open.

6. Family 14's baseline refresh (H7, F0 R43) adds `BindBaselineRefresh`,
   planned before a due round's baseline, and `CompleteBaselineRefresh`,
   planned once that baseline is complete and before grouping. Both are
   campaign-level; `refresh_due_for` is the lifecycle's own derivation.

The planner never reads a sibling log: `measurements` and `lineage` are carried
for the projections (§12) and for the executor, not for the plan.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Container, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from evolution.tree.artifacts.artifact_ledger import (
    LEDGER_FILE,
    MAX_LEDGER_BYTES,
    MAX_LEDGER_RECORD_BYTES,
)
from evolution.tree.artifacts.artifact_projections import LineageProjection, project_lineage
from evolution.tree.artifacts.artifact_schema import ARTIFACT_FAMILY, ArtifactLifecycle
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.campaign_epochs import (
    RefreshDue,
    base_measurement_id,
    family14_next_baseline,
    refresh_due,
)
from evolution.tree.campaign_identity import measurement_id_for, probe_continues
from evolution.tree.campaign_measurement import Measurement
from evolution.tree.campaign_policy import (
    CampaignPolicyV11,
    CampaignPolicyV12,
    decides_at_once,
    policy_class_for_payload,
)
from evolution.tree.campaign_schema import (
    AttemptState,
    CampaignSnapshot,
    RoundState,
    affected_layer_one,
    calibration_owed,
    confidence_scopes,
    current_members,
    editor_input_reason,
    editor_replayable,
    held_repair_required,
    owed_close,
    running_members,
    side_of_entry,
    superseding_comparison_owed,
)
from evolution.tree.campaign_statistics import (
    MeasurementTries,
    PrimaryRange,
    SampleRow,
    declined_tries,
    owed_repair_rows,
    owed_threshold_rows,
    primary_range_for,
    rows_held,
    stage_floor,
    stage_horizon,
    thresholds_reached,
)
from evolution.tree.roles.role_failures import grouper_suspension_for
from evolution.tree.storage.append_log import inspect_log
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalBoundaryError

Birth = Literal["absent", "unborn", "born"]
InspectionBirth = Literal["absent", "unborn", "born", "unreadable"]

#: The sibling key of the S2 store in `CampaignView.siblings`.
STORE_SIBLING = "artifact_store"

# Spec §7's threshold terminals, per owner; the planner turns a recorded
# `threshold_reached` into exactly this `campaign_finished`.
THRESHOLD_TERMINAL: Mapping[str, tuple[str, str]] = {
    "target": ("infrastructure_failed", "target_unstable"),
    "environment": ("infrastructure_failed", "environment_unstable"),
    "scorer": ("infrastructure_failed", "scorer_unstable"),
    "rollout": ("budget_exhausted", "rollout_time_budget_exhausted"),
}


def attempt_id_for(round_number: int, index: int) -> str:
    """The frozen attempt id of one round's slot; it is also the S2 attempt id."""

    return f"r{round_number:02d}-a{index:02d}"


def adaptive_grouping_for(
    snapshot: CampaignSnapshot,
    round_number: int,
    *,
    baseline_binding: str | None = None,
    policy_digest: str | None = None,
) -> Mapping[str, Any] | None:
    """Return this round's settled grouping or its same-input prior reuse.

    The reducer never reconstructs registry membership. The executor verifies
    source-derived facts before admission; this helper only makes the durable
    reuse key available on resume.
    """

    state = snapshot.rounds.get(round_number)
    if state is not None and state.grouping is not None:
        grouping = state.grouping
        if grouping["status"] == "finished":
            return grouping
        # b16 §11.1: only an open grouping keys the lookup. A settled failure
        # (an interruption) leaves the round unbound, and an unbound round
        # looks under the key the caller passes: the current identity's.
        if grouping["status"] == "started":
            baseline_binding = grouping["baseline_binding"]
            policy_digest = grouping["policy_digest"]
    if baseline_binding is None or policy_digest is None:
        return None
    return _finished_grouping(snapshot, round_number, baseline_binding, policy_digest)


def _finished_grouping(
    snapshot: CampaignSnapshot,
    round_number: int,
    baseline_binding: str,
    policy_digest: str,
    result_digest: str | None = None,
) -> Mapping[str, Any] | None:
    """The latest earlier round's finished grouping under this key, if any."""

    for number in sorted(snapshot.rounds, reverse=True):
        if number >= round_number:
            continue
        prior = snapshot.rounds[number].grouping
        if (
            prior is not None
            and prior["status"] == "finished"
            and prior["baseline_binding"] == baseline_binding
            and prior["policy_digest"] == policy_digest
            and (result_digest is None or prior["result_digest"] == result_digest)
        ):
            return prior
    return None


def bound_grouping(snapshot: CampaignSnapshot, round_number: int) -> Mapping[str, Any] | None:
    """The grouping a round is bound to for its life, or `None` while it is unbound.

    b16 spec §11.1 (ADR-0055, after ADR-0051's rule that an open round keeps
    its inputs across a rebase): once a round has a finished grouping of its
    own, or a selected portion, every resolution of the round's grouping uses
    the grouping that produced it -- the round's own finished grouping, else
    the one its `focus_portion_selected` names by `baseline_binding`,
    `policy_digest` and `grouping_digest`. A caller re-admits it under its
    own `policy_digest` and recorded `grouper_kind`, never the current
    identity's, so its group and cohort ids stay those the round used. A
    portion without a grouping (`none`) binds nothing.
    """

    state = snapshot.rounds.get(round_number)
    if state is None:
        return None
    own = state.grouping
    if own is not None and own["status"] == "finished":
        return own
    portion = state.focus_portion
    if portion is None or portion.get("grouping_digest") is None:
        return None
    return _finished_grouping(
        snapshot,
        round_number,
        portion["baseline_binding"],
        portion["policy_digest"],
        portion["grouping_digest"],
    )


def adaptive_scope_cases(state: RoundState) -> tuple[str, ...]:
    """The private focus fallback scope, deliberately never all group members."""

    return state.adaptive_scope_cases()


# -- the view -------------------------------------------------------------


@dataclass(frozen=True)
class CampaignView:
    """The campaign prefix plus the sibling facts, as inert data (spec §2)."""

    snapshot: CampaignSnapshot
    measurements: Mapping[str, Mapping[str, Any]]
    lineage: LineageProjection | None
    siblings: Mapping[str, InspectionBirth]
    #: Family 6: each read measurement's try facts, for the projections only
    #: (continuable-terminals spec §8). The planner never reads them, so
    #: `assemble` leaves them empty; `inspect_view` and the executor's result
    #: fill them.
    tries: Mapping[str, MeasurementTries] = field(default_factory=dict)
    #: Family 7: each read measurement's rows the latest `fault_rows`
    #: suspension still owes a repair try (I1 spec §5.4), from `owed_repairs`
    #: -- the one function the executor dispatches from too. Empty otherwise.
    repairs: Mapping[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    #: Family 12: each round's report-only caveat items -- its closed
    #: completion's checks (G2 spec §4.7) and its Attempts'
    #: `components_changed_by_condition` (§4.6) -- from `read_affected_items`,
    #: computed once per report, so `project_status` and `project_result`
    #: share one bootstrap per round. The planner never reads them;
    #: `inspect_view` and the executor's result fill them, as they fill `tries`.
    affected_items: Mapping[int, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    #: Families 7-12: the ids of the open Attempts whose latest stage ended
    #: with exhausted rows (#91 spec §3.1, ruling R13: every row settled, no
    #: fault row, nothing still owed a D1 try) -- from `0.3.0a27` (#120) with
    #: any row that did not score, a forced stop's `cancelled` ones included --
    #: from `held_attempts` -- the executor's verdict over the shared
    #: `campaign_statistics.rows_held`, since the planner reads no sample rows.
    #: In independent mode such an Attempt is held: it has no step and no slot
    #: until a repair it owes reactivates it (§3.2) or the round barrier
    #: decides it. Empty below family 7; the read-only `inspect_view` names
    #: the same set over the measurement prefixes (#114).
    lacking: frozenset[str] = frozenset()
    #: Families 7-12: the ids of the open Attempts whose measurement a
    #: `continue` re-bound and which have opened no stage on it yet, whose
    #: frozen source was held (ruling R17: the same `rows_held` over the
    #: source's rows), from `rebound_held_attempts`. The planner admits each
    #: like a held Attempt owing a repair: into a free `parallel_attempts`
    #: slot, in attempt-index order (§3.2). Empty below family 7; the
    #: read-only `inspect_view` names the same set (#114).
    rebound_held: frozenset[str] = frozenset()
    global_preflights: Mapping[int, Any] = field(default_factory=dict)


#: A measurement's latest-try rows up to a repeat: the executor's live
#: `Measurement.sample_rows`, or `latest_rows` over a read-only prefix.
RowsAt = Callable[[str, int], Sequence[SampleRow]]

#: A measurement's largest scheduled repeat: R, or a `case_repeats` count
#: (family-13 design §5).
MaxRepeat = Callable[[str], int]


def _scheduled_rows(
    snapshot: CampaignSnapshot, measurement_id: str
) -> tuple[Mapping[str, Any], ...]:
    """Rebuild the bound schedule from its own epoch and selector, including unborn sides."""
    from evolution.tree.campaign_epochs import epoch_identity
    from evolution.tree.campaign_identity import derived_identity, fresh_final_identity

    entry = snapshot.measurements[measurement_id]
    epoch = epoch_identity(snapshot, measurement_id)
    identity = derived_identity(
        epoch,
        entry["candidate_digest"],
        "champion" if side_of_entry(entry) == "champion" else "candidate",
        case_subset=entry.get("case_subset"),
        case_repeats=entry.get("case_repeats"),
    )
    if snapshot.family_version == 15 and entry.get("role") in {
        "family15_final",
        "family15_champion",
    }:
        raw_policy = epoch.to_payload()["policy"]
        policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
        identity = fresh_final_identity(identity, measurement_id, policy.repeats)
    return tuple(identity.to_payload()["schedule"])


def owed_rows(
    snapshot: CampaignSnapshot, measurement_id: str, *, opened_only: bool = False
) -> tuple[Mapping[str, Any], ...]:
    """Frozen rows required by the durable path, optionally only opened stages.

    A passing focus makes C owed before its stage reserves any slots.
    Release accounting requests only rows whose stage has actually opened.
    """
    from evolution.tree.campaign_epochs import epoch_identity

    schedule = _scheduled_rows(snapshot, measurement_id)
    entry = snapshot.measurements[measurement_id]
    raw_policy = epoch_identity(snapshot, measurement_id).to_payload()["policy"]
    policy = policy_class_for_payload(raw_policy).from_payload(raw_policy)
    if (
        snapshot.family_version != 15
        or not isinstance(policy, CampaignPolicyV12)
        or not policy.focus_first
        or entry.get("role") not in {"candidate", "parent"}
    ):
        return schedule
    attempt = snapshot.rounds[entry["round"]].attempts[entry["attempt_id"]]
    extra = attempt.family15
    if (
        extra is not None
        and extra.focus_decision is not None
        and extra.focus_decision.payload["outcome"] == "passed"
        and (not opened_only or any(s.get("scope") == "causal" for s in entry["stages"]))
    ):
        return schedule
    state = snapshot.rounds[attempt.round]
    assert attempt.selection is not None and state.gate_scope is not None
    corpus = epoch_identity(snapshot, measurement_id).to_payload()["corpus_cases"]
    focus, _ = confidence_scopes(attempt.selection, corpus, state.gate_scope["cases"])
    cases = {corpus[i]["case_id"] for i in focus}
    return tuple(row for row in schedule if row["case_id"] in cases)


def _owed_sample_rows(
    snapshot: CampaignSnapshot, mid: str, rows: Sequence[SampleRow]
) -> Sequence[SampleRow]:
    """Filter only scoped gate readers; keep legacy full-schedule reads unchanged."""
    entry = snapshot.measurements[mid]
    raw = snapshot.epochs[entry.get("epoch", 0)]["policy"]
    if (
        snapshot.family_version != 15
        or raw.get("gate", {}).get("stages") != "focus_first"
        or entry.get("role") not in {"candidate", "parent"}
    ):
        return rows
    keys = {(slot["case_id"], slot["sample_id"]) for slot in owed_rows(snapshot, mid)}
    return tuple(row for row in rows if (row.case_id, row.sample_id) in keys)


def _sides_held(
    snapshot: CampaignSnapshot,
    rows: RowsAt,
    ranges: Callable[[str], PrimaryRange],
    max_repeat: MaxRepeat,
    sides: Sequence[str],
    look: int,
    looks: Sequence[int],
    primary: str,
) -> bool:
    """`rows_held` over an Attempt's `sides` up to the stage of `look`, under
    the rule the producing version in force owes: from `0.3.0a27` (#120,
    `held_repair_required`) a settled stage whose look lacks pairs for any
    reason, a declined retry settled; before it R13's exhausted rows only.

    The stage is read up to its horizon (design §5, `stage_horizon`): the
    look, or on the last look a `case_repeats` Parent's last repeat. Without
    `case_repeats` the horizon is the look.
    """

    lacking = held_repair_required(snapshot.producing_version)
    horizon = stage_horizon(looks, look, max(max_repeat(mid) for mid in sides))
    return rows_held(
        [(_owed_sample_rows(snapshot, mid, rows(mid, horizon)), ranges(mid)) for mid in sides],
        horizon,
        stage_floor(looks, look),
        primary,
        lacking=lacking,
        declined=(
            [declined_tries(snapshot.retries_declined, mid) for mid in sides] if lacking else ()
        ),
    )


def held_attempts(
    snapshot: CampaignSnapshot,
    rows: RowsAt,
    ranges: Callable[[str], PrimaryRange],
    max_repeat: MaxRepeat,
    readable: Container[str],
    looks: Sequence[int],
    primary: str,
) -> frozenset[str]:
    """Attempts whose settled latest stage holds an unscored row (held): `CampaignView.lacking`.

    Exhausted rows (R13) and, from `0.3.0a27` (#120), any other unscored row
    (cancelled, declined retry), even on a pair the look does not need.

    #91 spec §3.1, ruling R13: the planner holds such an Attempt. It reads no
    sample rows, so the executor (`_Campaign.lacking_attempts`) and the
    read-only `inspect_view` both name them here, with the one definition the
    audit re-proves (`campaign_statistics.rows_held`): every row up to the
    latest stage's look, on each of the Attempt's sides, has started and
    scored or exhausted its tries, a row of that stage on either side is
    exhausted, and no side has a fault row. Whether the look lacks pairs does
    not matter: an exhausted row on a baseline-excluded pair holds the
    Attempt too, and the round barrier closes it `incomplete_sample`. An
    Attempt with a row not yet dispatched or still owed a D1 try is active,
    not held. An owed repair does not leave the set: its row stays exhausted
    until the repair try starts, so a held Attempt owing one is what the
    planner reactivates (§3.2). An Attempt whose latest look is decided or
    barred is left out, and so is one with a side not in `readable` (a
    measurement the caller has not opened).

    #120: from `0.3.0a27` a settled stage holds whenever its look lacks
    pairs, not only through an exhausted row -- a forced stop's `cancelled`
    rows, and a try whose retry the cancelled run declined, hold the Attempt
    until the round barrier as well (`_sides_held`).
    """

    lacking: set[str] = set()
    for state in snapshot.rounds.values():
        for attempt in state.attempts.values():
            if attempt.outcome is not None or attempt.measurement_id is None:
                continue
            stages = snapshot.measurements[attempt.measurement_id]["stages"]
            if not stages:
                continue
            look = int(stages[-1]["look"])
            if look in attempt.looks or look in attempt.not_started:
                continue
            sides = [
                mid
                for mid in (attempt.measurement_id, attempt.parent_measurement_id)
                if mid is not None
            ]
            if any(mid not in readable for mid in sides):
                continue
            if _sides_held(snapshot, rows, ranges, max_repeat, sides, look, looks, primary):
                lacking.add(attempt.attempt_id)
    return frozenset(lacking)


def rebound_held_attempts(
    snapshot: CampaignSnapshot,
    rows: RowsAt,
    ranges: Callable[[str], PrimaryRange],
    max_repeat: MaxRepeat,
    readable: Container[str],
    looks: Sequence[int],
    primary: str,
) -> frozenset[str]:
    """Re-bound, still stageless Attempts whose frozen source was held (R17):
    `CampaignView.rebound_held`.

    A `continue` re-binds every open Attempt's measurement (I2 spec §5.2),
    held ones included -- after a round-barrier threshold suspension, every
    open Attempt is held. A re-bound measurement opens no stage until the
    planner runs one, so `held_attempts` leaves it out, and without this set
    every such Attempt would read as active and run at once, past
    `parallel_attempts`. An Attempt is named when its measurement has no stage
    and was re-bound, and the frozen source it was re-bound from -- followed
    back past any source that itself opened no stage -- was held by the same
    `rows_held` `held_attempts` applies, over the source's rows (and, in
    family 12, its fresh Parent's source's) up to the source's latest stage's
    look, with that look still undecided. The planner admits each through the
    bounded, index-ordered slot logic of a reactivating Attempt. A source not
    in `readable` (never born, or not opened by the caller) names nothing.
    The executor (`_Campaign.rebound_held_attempts`) and the read-only
    `inspect_view` both name them here.
    """

    measurements = snapshot.measurements

    def source_of(measurement_id: str | None) -> str | None:
        if measurement_id is None:
            return None
        entry = measurements[measurement_id]
        if entry["stages"] or entry.get("rebound_from") is None:
            return None
        current = str(entry["rebound_from"])
        while not measurements[current]["stages"]:
            earlier = measurements[current].get("rebound_from")
            if earlier is None:
                return None
            current = str(earlier)
        return current

    named: set[str] = set()
    for state in snapshot.rounds.values():
        for attempt in state.attempts.values():
            if attempt.outcome is not None:
                continue
            source = source_of(attempt.measurement_id)
            if source is None:
                continue
            look = int(measurements[source]["stages"][-1]["look"])
            if look in attempt.looks or look in attempt.not_started:
                continue
            parent = source_of(attempt.parent_measurement_id)
            if attempt.parent_measurement_id is not None and parent is None:
                continue
            sides = [mid for mid in (source, parent) if mid is not None]
            if any(mid not in readable for mid in sides):
                continue
            if _sides_held(snapshot, rows, ranges, max_repeat, sides, look, looks, primary):
                named.add(attempt.attempt_id)
    return frozenset(named)


def classify_store(run_root: Path) -> Birth:
    """Classify the S2 store absent / unborn / born without taking its lease (§4)."""

    run_root = Path(run_root)
    if not (run_root / LEDGER_FILE).exists():
        return "absent"
    snapshot = inspect_log(
        run_root,
        file_name=LEDGER_FILE,
        max_record_bytes=MAX_LEDGER_RECORD_BYTES,
        max_log_bytes=MAX_LEDGER_BYTES,
        state_factory=ArtifactLifecycle,
        envelope_family=ARTIFACT_FAMILY,
    )
    return "born" if snapshot.records else "unborn"


def classify_siblings(run_root: Path, snapshot: CampaignSnapshot) -> dict[str, Birth]:
    """Classify every sibling the campaign journal declared (spec §4 step 3)."""

    births: dict[str, Birth] = {STORE_SIBLING: classify_store(run_root)}
    for measurement_id in snapshot.measurements:
        births[measurement_id] = Measurement.classify(run_root, measurement_id)
    return births


def assemble(
    run_root: Path,
    snapshot: CampaignSnapshot,
    measurements: Mapping[str, Measurement],
    store: ArtifactStore | None,
    *,
    siblings: Mapping[str, Birth] | None = None,
    primary: str | None = None,
    lacking: frozenset[str] = frozenset(),
    rebound_held: frozenset[str] = frozenset(),
    global_preflights: Mapping[int, Any] | None = None,
) -> CampaignView:
    """Build the read-only view from the campaign prefix and the live siblings.

    `primary` is the policy's primary metric; given, the view carries each
    measurement's owed repair rows (`owed_repairs`), which the planner reads.
    `lacking` is the executor's set of held open Attempts
    (`CampaignView.lacking`), `rebound_held` its set of re-bound, stageless
    Attempts whose frozen source was held (`CampaignView.rebound_held`).
    """

    statuses: dict[str, Mapping[str, Any]] = {}
    repairs: dict[str, frozenset[tuple[str, str]]] = {}
    for measurement_id in snapshot.measurements:
        measurement = measurements.get(measurement_id)
        if measurement is None:
            continue
        try:
            statuses[measurement_id] = measurement.status()
            if primary is not None:
                owed = owed_repairs(snapshot, measurement, primary)
                if owed:
                    repairs[measurement_id] = owed
        except (ValueError, JournalBoundaryError):
            # A declared measurement whose journal this process has not opened —
            # or whose writer a §7 fault poisoned — contributes no status; its
            # classification still names it, and the campaign's own terminal
            # already names the owner and the code. The view is not only the §12
            # report, though: `next_actions` plans from it too, and a measurement
            # with no status reads there as a stage with no shortfall
            # (`_stage_state` returns `(len(stages), False)`), so a planner would
            # move past it rather than finish it. That is safe because a
            # measurement-journal fault is classified at the executor's own
            # boundary before any view is assembled from it — the campaign is
            # already stopping and `drive` takes no further step — while a
            # read-only `inspect_view` plans nothing at all. The report must
            # survive the fault it is reporting.
            continue
    lineage = None if store is None else project_lineage(store.ledger.snapshot())
    return CampaignView(
        snapshot=snapshot,
        measurements=statuses,
        lineage=lineage,
        siblings=dict(siblings) if siblings is not None else classify_siblings(run_root, snapshot),
        repairs=repairs,
        lacking=lacking,
        rebound_held=rebound_held,
        global_preflights=global_preflights or {},
    )


def owed_repairs(
    snapshot: CampaignSnapshot, measurement: Measurement, primary: str
) -> frozenset[tuple[str, str]]:
    """Family 7: the `(case_id, sample_id)` rows of `measurement` owed a repair try.

    I1 spec §5.4: when the latest suspension is a `fault_rows` one, each row it
    named -- the listed rows, or the prefix reconstruction when its evidence
    was truncated (`Measurement.owed_repairs`) -- owes one repair try until a
    try past the named one has started. The executor dispatches exactly this
    set and the planner keeps the measurement's stage open while it is not
    empty, so the two cannot disagree. Empty below family 7 and after any other
    kind of suspension.

    #78: the union of the latest `fault_rows` suspension's rows and the pairs
    threshold suspensions grant (`threshold_owed`).
    """

    evidence = repair_evidence(snapshot)
    fault = (
        frozenset()
        if evidence is None
        else measurement.owed_repairs(
            evidence, primary, repair_range(snapshot, measurement.measurement_id)
        )
    )
    return fault | threshold_owed(snapshot, measurement)


def threshold_owed(
    snapshot: CampaignSnapshot, measurement: Measurement
) -> frozenset[tuple[str, str]]:
    """#78 (spec §3.2): the pairs of `measurement` owed a threshold repair try.

    Empty below family 7, for a frozen measurement (its pairs run on the
    re-bound successor), and for an Attempt measurement unless its Attempt is
    open: an open Attempt's measurement is named from 0.3.0a26 (#91 spec §3.2).
    """

    entry = snapshot.measurements.get(measurement.measurement_id)
    if entry is None or entry.get("frozen", False):
        return frozenset()
    named = snapshot.threshold_named.get(measurement.measurement_id)
    return frozenset() if not named else measurement.owed_threshold_repairs(named)


def owed_repairs_in(
    snapshot: CampaignSnapshot,
    measurement_id: str,
    records: Sequence[Envelope],
    primary: str,
) -> frozenset[tuple[str, str]]:
    """`owed_repairs` over a read-only measurement prefix (`inspect_view`)."""

    latest: dict[tuple[str, str], int] = {}
    for record in records:
        if record.event == "rollout_started":
            key = (str(record.payload["case_id"]), str(record.payload["sample_id"]))
            latest[key] = int(record.payload.get("retry_ordinal", 0))
    named = snapshot.threshold_named.get(measurement_id) or {}
    frozen = bool(snapshot.measurements.get(measurement_id, {}).get("frozen", False))
    threshold = frozenset() if frozen or not records else owed_threshold_rows(named, latest.get)
    evidence = repair_evidence(snapshot)
    if evidence is None or not records:
        return threshold
    return threshold | owed_repair_rows(
        evidence,
        measurement_id,
        primary,
        schedule=records[0].payload["identity"]["schedule"],
        latest_ordinal=latest.get,
        records=lambda: records,
        primary_range=repair_range(snapshot, measurement_id),
    )


def repair_range(snapshot: CampaignSnapshot, measurement_id: str) -> PrimaryRange:
    """The range rule a `fault_rows` suspension counted `measurement_id`'s rows under.

    The producing version in force at the latest suspension -- not the
    current one (codex branch review round 1; the audit's ruling R5) -- and
    the version the measurement's carried rows were admitted under, so a
    truncated named set is rebuilt exactly as the suspension counted it.
    """

    suspensions = snapshot.suspensions
    version = (
        str(suspensions[-1].get("version", snapshot.producing_version))
        if suspensions
        else snapshot.producing_version
    )
    return primary_range_for(
        snapshot.family_version, version, snapshot.import_versions.get(measurement_id)
    )


def repair_evidence(snapshot: CampaignSnapshot) -> Mapping[str, Any] | None:
    """The latest suspension's `fault_rows` evidence in family 7, else `None`."""

    suspensions = snapshot.suspensions
    if not suspensions or suspensions[-1]["evidence_kind"] != "fault_rows":
        return None
    return suspensions[-1]["evidence"]


# -- actions --------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One step `next_actions` names; the executor performs it."""


@dataclass(frozen=True)
class BindBaseline(Action):
    round: int
    measurement_id: str


@dataclass(frozen=True)
class MeasureBaselineStage(Action):
    round: int
    measurement_id: str
    stage: int


@dataclass(frozen=True)
class CompleteBaseline(Action):
    round: int
    measurement_id: str


@dataclass(frozen=True)
class BindBaselineRefresh(Action):
    """Family 14 (F0 R43): journal round `round`'s due `baseline_refresh_bound`."""

    round: int


@dataclass(frozen=True)
class CompleteBaselineRefresh(Action):
    """Family 14 (F0 R43): journal `baseline_refresh_completed` for round
    `round`, whose complete baseline `measurement_id` is its refresh."""

    round: int
    measurement_id: str


@dataclass(frozen=True)
class OpenAttempt(Action):
    round: int
    index: int
    attempt_id: str
    path: str = "candidate"


@dataclass(frozen=True)
class FailPreflight(Action):
    attempt_id: str


@dataclass(frozen=True)
class RunEditor(Action):
    attempt_id: str


@dataclass(frozen=True)
class SealAttempt(Action):
    attempt_id: str


@dataclass(frozen=True)
class SelectAffected(Action):
    """Family 12 (G2 spec §4.4, §4.5): inspect the Candidate when component
    mode needs its map, then journal the Attempt's affected cases."""

    attempt_id: str


@dataclass(frozen=True)
class BindMeasurement(Action):
    attempt_id: str


@dataclass(frozen=True)
class BindParent(Action):
    """Family 12 (G2 spec §4.6): bind the Attempt's fresh Parent over its
    layer 1, after the Candidate's bind and before its first stage."""

    attempt_id: str


@dataclass(frozen=True)
class BindComparison(Action):
    """Family 13 (design §6.2, §6.3, G10): bind the Attempt's comparison and,
    when it owes one, its noise calibration -- after its measurements and
    before their first stage; on a resume, only the missing half."""

    attempt_id: str


@dataclass(frozen=True)
class CalibrateNoise(Action):
    """Family 13 (design §6.4, G10, D4): complete the bound noise calibration."""

    attempt_id: str


@dataclass(frozen=True)
class RunAttemptStage(Action):
    attempt_id: str
    stage: int


@dataclass(frozen=True)
class DecideFocus(Action):
    attempt_id: str


@dataclass(frozen=True)
class EvaluateLook(Action):
    attempt_id: str
    look: int


@dataclass(frozen=True)
class CloseAttempt(Action):
    attempt_id: str
    outcome: str
    reason: str
    look: int | None = None


@dataclass(frozen=True)
class SelectFocus(Action):
    round: int


@dataclass(frozen=True)
class RunGrouping(Action):
    round: int


@dataclass(frozen=True)
class SelectFocusPortion(Action):
    round: int


@dataclass(frozen=True)
class RunReflection(Action):
    round: int


@dataclass(frozen=True)
class SelectGateScope(Action):
    round: int


@dataclass(frozen=True)
class DecideRound(Action):
    round: int


@dataclass(frozen=True)
class AdvanceCandidateCompletion(Action):
    attempt_id: str


@dataclass(frozen=True)
class BindFinalists(Action):
    """Family-13 design §6.7: the round barrier, then `finalists_bound`.

    Performed by `decide_round`, which owns the barrier prelude (plan choice 2).
    """

    round: int


@dataclass(frozen=True)
class BindCohortMember(Action):
    """Design §6.8: one member, declared before its journal is created."""

    round: int
    measurement_id: str


@dataclass(frozen=True)
class RunCohortStage(Action):
    """Design §7: one joint stage of every member, then the cohort barrier (§8)."""

    round: int
    stage: int


@dataclass(frozen=True)
class CompleteCohortMember(Action):
    """Design §6.9: one member's `cohort_measurement_completed`."""

    round: int
    measurement_id: str


@dataclass(frozen=True)
class FinishCampaign(Action):
    outcome: str
    reason: str
    continuation: str


#: The action kinds that belong to one attempt; the executor runs each in that
#: attempt's own task (spec §10).
ATTEMPT_ACTIONS = (
    OpenAttempt,
    RunEditor,
    SealAttempt,
    SelectAffected,
    BindMeasurement,
    BindParent,
    RunAttemptStage,
    DecideFocus,
    EvaluateLook,
    CloseAttempt,
    BindComparison,
    CalibrateNoise,
    FailPreflight,
)


def action_attempt_id(action: Action) -> str | None:
    """The attempt an action belongs to, or `None` for a campaign-level one."""

    return getattr(action, "attempt_id", None) if isinstance(action, ATTEMPT_ACTIONS) else None


# -- the planner ----------------------------------------------------------


def stage_progress(
    view: CampaignView, measurement_id: str, retries: int | None = None
) -> tuple[int, bool]:
    """How many stages a measurement opened, and whether the last one is unfinished.

    Spec §11: `stage_opened` is durable before any of its samples runs, so an
    interruption can leave an opened stage only partly dispatched. Stages open in
    order and a stage opens only once every sample of the previous one is
    terminal, so any shortfall between the opened stages' declared `sample_count`
    and the samples S1 has actually started belongs to the last opened stage —
    and the planner must finish that stage rather than open the next.

    `retries` is the policy's `sample_retries` (continuable-terminals spec §3.4):
    a started sample whose latest try ended without a score while fewer than
    `retries + 1` tries exist still owes a try, so its stage is unfinished too.
    Only a measurement whose status counts `tries` is read this way. In family
    7 a try whose `indeterminate_observed` says no retry follows it owes none:
    that is how the planner, which reads no sibling log, learns that an
    indeterminate repair try exhausted its sample below `retries` (I1 spec
    §5.4).

    Baseline inheritance §3.3: a carried (`imported`) row is not dispatched by
    any stage. `stage_opened.sample_count` declares only the rows a stage
    dispatches, so `started` counts only dispatched rows too; a carried row of a
    stage not yet opened must not stand in for a missing row of the open one.
    """

    entry = view.snapshot.measurements.get(measurement_id)
    stages = () if entry is None else entry["stages"]
    if not stages:
        return 0, False
    status = view.measurements.get(measurement_id)
    if status is None:
        return len(stages), False
    samples = status["samples"]
    if any(stage.get("scope") for stage in stages):
        keys = {
            (row["case_id"], row["sample_id"]) for row in owed_rows(view.snapshot, measurement_id)
        }
        samples = [row for row in samples if (row["case_id"], row["sample_id"]) in keys]
    expected = sum(int(stage["sample_count"]) for stage in stages)
    started = sum(1 for row in samples if row["execution_state"] not in ("not_started", "imported"))
    if started < expected:
        return len(stages), True
    if _repair_owed(view, measurement_id):
        return len(stages), True
    declined: frozenset[tuple[Any, ...]] = frozenset()
    declined = view.snapshot.retries_declined
    owed = retries is not None and any(
        _owes_a_try(row, retries)
        and (measurement_id, str(row["case_id"]), str(row["sample_id"]), int(row["tries"]) - 1)
        not in declined
        for row in samples
        if "tries" in row
    )
    return len(stages), owed


def attempt_stage_progress(
    view: CampaignView, attempt: AttemptState, retries: int | None
) -> tuple[int, bool]:
    """`stage_progress` over an Attempt's Candidate and, if bound, its fresh Parent.

    G2 spec §4.6: one stage spans both measurements. The Candidate's count is
    the Attempt's; the stage is unfinished while either side is, or while the
    Parent has opened fewer stages (a crash between the two `stage_opened`
    records, or a refusal while opening the Parent's).
    """

    assert attempt.measurement_id is not None
    opened, unfinished = stage_progress(view, attempt.measurement_id, retries)
    if attempt.parent_measurement_id is None:
        return opened, unfinished
    parent_opened, parent_unfinished = stage_progress(view, attempt.parent_measurement_id, retries)
    return opened, unfinished or parent_unfinished or parent_opened < opened


def _repair_owed(view: CampaignView, measurement_id: str) -> bool:
    """Family 7: does the latest `fault_rows` suspension still owe this measurement a repair?

    I1 spec §5.4: a stage suspended by a fault row is unsettled, so a resume
    returns to it rather than open the next stage (or judge an Attempt) over a
    faulted sample. The owed set is `view.repairs`, computed by `owed_repairs`
    -- the executor's own derivation, truncated evidence included -- so the
    stage the planner returns to always dispatches it, wherever the owed row's
    own stage is.
    """

    return bool(view.repairs.get(measurement_id))


def _owes_a_try(sample: Mapping[str, Any], retries: int) -> bool:
    """A finished latest try without a score, below the try limit (spec §3.4)."""

    if sample["execution_state"] != "finished" or int(sample["tries"]) > retries:
        return False
    evaluation = sample["evaluation"]
    return evaluation is None or evaluation["kind"] == "indeterminate"


def refresh_for(snapshot: CampaignSnapshot, round_number: int) -> Mapping[str, Any] | None:
    """Round `round_number`'s `CampaignSnapshot.refreshes` entry, or `None`.

    Only family 14 journals a refresh, so every older snapshot has none.
    """

    return next((entry for entry in snapshot.refreshes if entry["round"] == round_number), None)


def refresh_due_for(
    snapshot: CampaignSnapshot, policy: CampaignPolicyV11, round_number: int
) -> RefreshDue | None:
    """The refresh round `round_number` owes before its baseline (F0 R42), or `None`.

    Families 14 and 15. It is the lifecycle's own derivation (`refresh_due` over
    the same rounds, rebaseline round and latest epoch), so the planner never
    proposes a binding the lifecycle refuses: `None` under N = 0 or below N
    counted closures, and at the round a `rebaseline` starts.
    """

    return refresh_due(
        snapshot.rounds,
        first_round=snapshot.first_round,
        rebaseline_from_round=snapshot.rebaseline_from_round,
        epoch=len(snapshot.epochs) - 1,
        round_number=round_number,
        after_rounds=getattr(policy, "baseline_refresh_after_rounds", None),
    )


def _baseline_measurement(snapshot: CampaignSnapshot, round_number: int) -> str:
    """The measurement round `round_number`'s baseline binds (spec §6).

    The first round of the campaign measures the seed champion; a later round
    continues the previous round's promoted winner, or — after a stall — the very
    measurement the previous round already used. The round a `rebaseline`
    starts (I2 spec §3.2, §5.2) measures its champion afresh, under the epoch id
    `m-r<NN>-champion@e<k>` of the latest epoch. In family 13 a round that
    promoted or retained continues its selected cohort measurement (design
    §6.12). Family 14 uses the shared `family14_next_baseline` rule, except
    that a round whose `baseline_refresh_bound` is journaled binds its refresh
    measurement (F0 R43); the `rebaseline` round still binds its fresh id
    (plan decision 7: the rebaseline supersedes the refresh).
    """

    if round_number != snapshot.rebaseline_from_round:
        refresh = refresh_for(snapshot, round_number)
        if refresh is not None:
            return str(refresh["measurement_id"])
    return family14_next_baseline(
        snapshot.rounds,
        first_round=snapshot.first_round,
        rebaseline_from_round=snapshot.rebaseline_from_round,
        epoch=len(snapshot.epochs) - 1,
        round_number=round_number,
    )


def attempt_action(
    view: CampaignView, policy: CampaignPolicyV11, attempt: AttemptState
) -> Action | None:
    """The next step of one attempt (spec §6, §8).

    Everything but the unfinished-stage question is decided from the campaign
    prefix; that one reads the measurement's S1 status, which is the only place
    a partly dispatched stage is visible.
    """

    snapshot = view.snapshot
    if attempt.outcome is not None:
        return None
    attempt_id = attempt.attempt_id
    if snapshot.family_version == 15 and attempt.family15 is not None:
        if attempt.family15.preflight is not None:
            return CloseAttempt(attempt_id, "rejected", "structurally_unattainable")
        if attempt.family15.path == "early_global":
            return FailPreflight(attempt_id)
    if attempt.editor in (None, "started"):
        return RunEditor(attempt_id)
    if attempt.editor == "declined":
        return CloseAttempt(attempt_id, "abandoned", "editor_declined")
    if attempt.editor == "failed":
        if editor_replayable(
            attempt,
            adaptive=True,
            resilient=True,
        ):
            # Family 10 (role resilience spec §5.3): a `provider`/`unknown`
            # failure re-runs only once its suspension is written; until then
            # the Attempt has no step (a resume writes the owed suspension
            # before planning, `_Campaign.resumed_resilience_cause`).
            if attempt.editor_error == "interrupted" or attempt.editor_suspended:
                return RunEditor(attempt_id)
            return None
        reason = (
            "editor_unresponsive" if attempt.editor_error == "unresponsive" else "editor_failed"
        )
        return CloseAttempt(attempt_id, "abandoned", reason)
    if attempt.candidate_digest is None:
        return SealAttempt(attempt_id)
    if attempt.measurement_id is None:
        if attempt.selection is None:
            return SelectAffected(attempt_id)
        if not selection_layer_one(snapshot, attempt):
            return CloseAttempt(attempt_id, "abandoned", "no_exam_impact")
        return BindMeasurement(attempt_id)
    if (policy.fresh_parent) and (attempt.parent_measurement_id is None):
        # G2 spec §4.6: the fresh Parent is bound after the Candidate and
        # before the Attempt's first stage -- on a resume between the two
        # binds too. The policy in force decides, as the lifecycle's does.
        return BindParent(attempt_id)
    if snapshot.family_version == 15:
        return _independent_action(view, policy, attempt)
    return _confidence_action(view, policy, attempt)


def _independent_action(
    view: CampaignView, policy: CampaignPolicyV11, attempt: AttemptState
) -> Action | None:
    """Family 15 has one complete deciding look and no calibration."""
    extra = attempt.family15
    assert extra is not None
    if extra.comparison is not None:
        result = extra.comparison.payload
        return CloseAttempt(
            attempt.attempt_id,
            "finalist" if result["outcome"] == "passed" else "rejected",
            str(result["reason"]),
        )
    bound = extra.comparison_binding
    if (
        bound is None
        or bound.payload["candidate"]["measurement_id"] != attempt.measurement_id
        or (
            policy.fresh_parent
            and bound.payload["reference"]["measurement_id"] != attempt.parent_measurement_id
        )
    ):
        return BindComparison(attempt.attempt_id)
    if isinstance(policy, CampaignPolicyV12) and policy.focus_first:
        decision = extra.focus_decision
        if decision is not None and decision.payload["outcome"] != "passed":
            return DecideFocus(attempt.attempt_id)
        round_state = view.snapshot.rounds[attempt.round]
        assert attempt.selection is not None and round_state.gate_scope is not None
        focus, causal = confidence_scopes(
            attempt.selection,
            view.snapshot.identity["corpus_cases"],
            round_state.gate_scope["cases"],
        )
        stage = 1 if decision is None else 2 if causal else None
        sides = tuple(
            mid
            for mid in (attempt.measurement_id, attempt.parent_measurement_id)
            if mid is not None
        )
        if stage is not None:
            for scope_mid in sides:
                if view.snapshot.measurements[scope_mid]["complete"]:
                    continue
                opened, unfinished = stage_progress(
                    view, scope_mid, getattr(policy, "sample_retries", None)
                )
                if opened == 0 and stage == 2:
                    return RunAttemptStage(attempt.attempt_id, 1)
                if opened < stage or unfinished:
                    return RunAttemptStage(attempt.attempt_id, stage)
        if thresholds_reached(view.snapshot, policy):
            return None
        if decision is None:
            focus_ids = {view.snapshot.identity["corpus_cases"][i]["case_id"] for i in focus}
            from evolution.tree.case_comparison import admissible

            if any(
                not admissible(
                    SimpleNamespace(
                        disposition=(row.get("evaluation") or {}).get("kind"),
                        metrics=row.get("metrics"),
                    ),
                    policy.metric_primary,
                )
                for mid in sides
                for row in view.measurements.get(mid, {}).get("samples", ())
                if row["case_id"] in focus_ids
            ):
                return None
            return DecideFocus(attempt.attempt_id)
        if _held(view, policy, attempt):
            return None
        return EvaluateLook(attempt.attempt_id, policy.repeats)
    for mid in (attempt.measurement_id, attempt.parent_measurement_id):
        if mid is None or view.snapshot.measurements[mid]["complete"]:
            continue
        opened, unfinished = stage_progress(view, mid, getattr(policy, "sample_retries", None))
        if opened == 0 or unfinished:
            return RunAttemptStage(attempt.attempt_id, 1)
    if _held(view, policy, attempt) or thresholds_reached(view.snapshot, policy):
        return None
    return EvaluateLook(attempt.attempt_id, policy.repeats)


def _confidence_action(
    view: CampaignView, policy: CampaignPolicyV11, attempt: AttemptState
) -> Action | None:
    """Family 13 (design §6.14 rows 1-3): bind, calibrate, stage, look, close.

    It follows F3's lifecycle exactly: the close is always `owed_close`, each
    stage and look the next of the comparison's frozen `looks`, and a held
    Attempt -- whose fresh Parent's final stage included (plan choice 6) --
    takes no step until a repair or the round barrier decides it.

    G11 (plan choices 2-4): after a `continue` re-bound either side of an
    undecided comparison, the next step re-binds the comparison (and a fresh
    calibration it sets aside) before any other work. An owed close is
    written first: a decided comparison is never re-bound.
    """

    attempt_id = attempt.attempt_id
    comparison = attempt.comparison
    if comparison is None or (calibration_owed(comparison) and attempt.calibration is None):
        return BindComparison(attempt_id)
    owed = owed_close(attempt, family_version=view.snapshot.family_version)
    if owed is not None:
        decision, look = owed
        return CloseAttempt(attempt_id, decision.decision, str(decision.reason), look)
    if superseding_comparison_owed(attempt, family_version=view.snapshot.family_version):
        return BindComparison(attempt_id)
    calibration = attempt.calibration
    if (
        calibration is not None
        and attempt.calibration_result is None
        and calibration["source"] == "baseline"
    ):
        return CalibrateNoise(attempt_id)
    opened, unfinished = attempt_stage_progress(
        view, attempt, getattr(policy, "sample_retries", None)
    )
    if opened == 0 and not attempt.looks and not attempt.not_started:
        return RunAttemptStage(attempt_id, 1)
    if unfinished:
        return RunAttemptStage(attempt_id, opened)
    assert attempt.measurement_id is not None
    stages = view.snapshot.measurements[attempt.measurement_id]["stages"]
    look = int(stages[-1]["look"]) if stages else max((*attempt.looks, *attempt.not_started))
    if look in attempt.not_started:
        return CloseAttempt(attempt_id, "insufficient_evidence", "incomplete_sample")
    if look not in attempt.looks:
        if _held(view, policy, attempt):
            return None
        if (
            look == max(comparison["looks"])
            and calibration is not None
            and attempt.calibration_result is None
        ):
            return CalibrateNoise(attempt_id)
        return EvaluateLook(attempt_id, look)
    return RunAttemptStage(attempt_id, len(stages) + 1)


def selection_layer_one(snapshot: CampaignSnapshot, attempt: AttemptState) -> tuple[str, ...]:
    """Layer 1 of a selected family-12 Attempt: its affected exam cases.

    The same `affected_layer_one` the lifecycle's bind and `no_exam_impact`
    rules apply, over the identity in force and the round's gate scope.
    """

    assert attempt.selection is not None
    scope = snapshot.rounds[attempt.round].gate_scope
    assert scope is not None
    return affected_layer_one(attempt.selection, snapshot.identity["corpus_cases"], scope["cases"])


def _held(view: CampaignView, policy: CampaignPolicyV11, attempt: AttemptState) -> bool:
    """Families 7-12, independent mode: does this open Attempt's settled latest
    stage hold an unscored row (`CampaignView.lacking`)? That is an exhausted
    row (ruling R13) or, gated from `0.3.0a27`, any other unscored row such as
    a `cancelled` or declined-retry one (#120), even on a pair the look does
    not need.

    #91 spec §3.1: such an Attempt is held -- it is not evaluated, and it
    takes no `parallel_attempts` slot unless a repair it owes reactivates it
    (§3.2, `_round_actions`). Under sequential feedback or
    `round_stop: first_accepted`, the one active Attempt is decided at once
    instead, so nothing is held.
    """

    return (attempt.attempt_id in view.lacking) and (not decides_at_once(policy))


def _round_actions(
    view: CampaignView, policy: CampaignPolicyV11, state: RoundState
) -> tuple[Action, ...]:
    """Every action the round's open attempts and its free slots ask for.

    #91 spec §3.2: a held Attempt takes no slot; one that owes repairs
    reactivates into a free slot, in attempt-index order, never more at once
    than `parallel_attempts` minus the Attempts already active. The others stay
    held until a slot frees, and no new Attempt opens while they wait.
    Ruling R17: a held Attempt that a `continue` re-bound, and which has opened
    no stage on its new measurement yet (`CampaignView.rebound_held`), is
    reactivating in the same way, so the bound holds after an in-place change.
    """

    retries = getattr(policy, "sample_retries", None)

    def rebound(attempt: AttemptState) -> bool:
        return attempt.outcome is None and attempt.attempt_id in view.rebound_held

    def held(attempt: AttemptState) -> bool:
        parked = (
            view.snapshot.family_version == 15
            and not decides_at_once(policy)
            and bool(thresholds_reached(view.snapshot, policy))
            and attempt.measurement_id is not None
            and attempt_action(view, policy, attempt) is None
        )
        return attempt.outcome is None and (
            _held(view, policy, attempt) or rebound(attempt) or parked
        )

    attempts = sorted(state.attempts.values(), key=lambda item: item.index)
    if view.snapshot.family_version == 15:
        finalists = [a for a in attempts if a.outcome == "finalist" and a.family15 is not None]
        unsealed = next(
            (a for a in finalists if a.family15 is not None and a.family15.gate_seal is None), None
        )
        if unsealed is not None:
            return (AdvanceCandidateCompletion(unsealed.attempt_id),)
        ready = policy.round_stop == "first_accepted" or (
            len(attempts) == policy.attempts_per_round
            and all(a.outcome is not None for a in attempts)
        )
        pending = next(
            (a for a in finalists if a.family15 is not None and a.family15.global_result is None),
            None,
        )
        if ready and pending is not None:
            return (AdvanceCandidateCompletion(pending.attempt_id),)
        if pending is None and (
            any(
                a.family15 is not None
                and a.family15.global_result is not None
                and a.family15.global_result.payload["accepted"]
                for a in finalists
            )
            or (
                len(attempts) == policy.attempts_per_round
                and all(a.outcome is not None for a in attempts)
            )
        ):
            return (DecideRound(state.round),)
    if view.snapshot.family_version == 14 and policy.round_stop == "first_accepted":
        if any(c["accepted"] for c in state.global_comparisons.values()):
            return (DecideRound(state.round),)
        pending = next(
            (
                a
                for a in attempts
                if a.outcome == "finalist" and a.attempt_id not in state.global_comparisons
            ),
            None,
        )
        if pending is not None:
            if state.cohort is None or not any(
                f["attempt_id"] == pending.attempt_id for f in state.cohort["finalists"]
            ):
                return (BindFinalists(state.round),)
            return _cohort_actions(view, policy, state)
    actions: list[Action] = []
    running = sum(1 for attempt in attempts if attempt.outcome is None and not held(attempt))
    for attempt in attempts:
        if held(attempt) and (
            rebound(attempt) or attempt_stage_progress(view, attempt, retries)[1]
        ):
            # A held Attempt owing repairs, or re-bound by `continue`,
            # reactivates when a slot is free.
            if running >= policy.parallel_attempts:
                continue
            running += 1
        action = attempt_action(view, policy, attempt)
        if action is not None:
            actions.append(action)
    used = {attempt.index for attempt in state.attempts.values()}
    opened = len(state.attempts)
    for index in range(1, policy.attempts_per_round + 1):
        if opened >= policy.attempts_per_round or running >= policy.parallel_attempts:
            break
        if index in used:
            continue
        actions.append(OpenAttempt(state.round, index, attempt_id_for(state.round, index)))
        opened += 1
        running += 1
    return tuple(actions)


def stages_owed(snapshot: CampaignSnapshot, measurement_id: str, policy: CampaignPolicyV11) -> int:
    """How many stages a measurement opens in all: one per look, or fewer when re-bound.

    I2 spec §5.2: a measurement re-bound under `continue` for an Attempt that
    already decided looks opens a stage only for each later look
    (`rebound_after_look`, which the executor's `plans_for` reads too).
    """

    entry = snapshot.measurements.get(measurement_id)
    after = None if entry is None else entry.get("rebound_after_look")
    if after is None:
        return len(policy.looks)
    return sum(1 for look in policy.looks if look > after)


def _cohort_actions(view: CampaignView, policy: Any, state: Any) -> tuple[Action, ...]:
    """Family-13 design §7 and R37: drive the current final binding.

    Without a bound cohort the round barrier is next (`BindFinalists`); with
    one, every member binds, the cohort stages run, every member completes,
    and then `DecideRound` compares and, at the round barrier, selects.
    A first-accepted loser returns to search before that barrier. The members are the
    current ones (`RoundState.cohort_current`), and no stage opens on a
    completed member (F7 plan choice 8; F5 plan choice 13 said "once any
    member has completed no cohort stage runs again", the same rule without
    a re-bind). A member not bound when a `continue` re-bound the others is
    bound now under its plain id, in the new epoch (plan choice 6).
    """

    number = state.round
    if state.cohort is None:
        return (BindFinalists(number),) if state.attempts else (DecideRound(number),)
    members = list(current_members(state))
    for mid in members:
        if mid not in view.snapshot.measurements:
            return (BindCohortMember(number, mid),)
    running = list(running_members(state))
    if running:
        stage = _cohort_stage_owed(view, running, policy)
        if stage is not None:
            return (RunCohortStage(number, stage),)
        return (CompleteCohortMember(number, running[0]),)
    return (DecideRound(number),)


def _cohort_stage_owed(view: CampaignView, members: list[str], policy: Any) -> int | None:
    """The cohort stage to run over the running `members`: the latest one
    while any member's is unfinished or unopened, else the next look, else
    none. A member a `continue` re-bound has no stage, so the cohort runs
    again from stage 1 (F7 plan choice 8)."""

    retries = getattr(policy, "sample_retries", None)
    progress = [stage_progress(view, mid, retries) for mid in members]
    opened = [count for count, _ in progress]
    latest = max(opened)
    if latest and (min(opened) < latest or any(unfinished for _, unfinished in progress)):
        return latest
    if latest < len(policy.looks):
        return latest + 1
    return None


def next_actions(view: CampaignView, policy: CampaignPolicyV11) -> tuple[Action, ...]:
    """What the campaign does next, as a pure function of its view (spec §6, §11)."""

    snapshot = view.snapshot
    if "unreadable" in view.siblings.values():
        # Inspection retains the campaign prefix when a sibling cannot be read.
        # Missing live facts cannot authorize any executor action.
        return ()
    if snapshot.finished is not None:
        return ()
    if snapshot.owed_terminal is not None:
        # Comparable-links spec §5.3: an applied close or terminal is followed
        # by its terminal and nothing else.
        outcome, reason = snapshot.owed_terminal
        return (FinishCampaign(outcome, reason, "none"),)
    if snapshot.threshold is not None:
        outcome, reason = THRESHOLD_TERMINAL[str(snapshot.threshold["owner"])]
        return (FinishCampaign(outcome, reason, "none"),)
    last_round = snapshot.first_round + snapshot.total_rounds - 1
    for number in range(snapshot.first_round, last_round + 1):
        state = snapshot.rounds.get(number)
        if state is None or state.baseline_measurement_id is None:
            # Family 14 (F0 R43): a due refresh is journaled before the
            # round's baseline, which then binds the refresh measurement.
            if (
                refresh_for(snapshot, number) is None
                and refresh_due_for(snapshot, policy, number) is not None
            ):
                return (BindBaselineRefresh(number),)
            return (BindBaseline(number, _baseline_measurement(snapshot, number)),)
        baseline = state.baseline_measurement_id
        if baseline not in snapshot.measurements and state.closed is None:
            # #106, ADR-0008: a run cut between `baseline_bound` and the fresh
            # champion's `measurement_bound` names a baseline nothing declares
            # yet; the same command binds it again, and `bind_baseline` writes
            # only the missing half.
            return (BindBaseline(number, baseline),)
        # I2 spec §3.2: a round a `rebaseline` closed keeps its incomplete
        # baseline frozen; nothing appends to it again.
        condition_changed = state.closed is not None and state.closed["outcome"] == (
            "condition_changed"
        )
        if not state.baseline_complete and not condition_changed:
            if snapshot.family_version == 15 and snapshot.measurements[baseline]["complete"]:
                return (CompleteBaseline(number, baseline),)
            opened, unfinished = stage_progress(
                view, baseline, getattr(policy, "sample_retries", None)
            )
            if unfinished:
                return (MeasureBaselineStage(number, baseline, opened),)
            if opened < stages_owed(snapshot, baseline, policy):
                return (MeasureBaselineStage(number, baseline, opened + 1),)
            return (CompleteBaseline(number, baseline),)
        if state.closed is None:
            # Family 14 (F0 R43): a refresh round's complete baseline closes
            # its refresh before anything reads it. A refresh a `rebaseline`
            # superseded (the round bound its fresh id) is never completed.
            refresh = refresh_for(snapshot, number)
            if (
                refresh is not None
                and refresh["completed"] is None
                and base_measurement_id(baseline) == refresh["measurement_id"]
            ):
                return (CompleteBaselineRefresh(number, baseline),)
            global_check = view.global_preflights.get(number)
            if snapshot.family_version == 15 and (
                (global_check is not None and global_check.impossible)
                or any(
                    a.family15 is not None and a.family15.path == "early_global"
                    for a in state.attempts.values()
                )
            ):
                actions = _round_actions(view, policy, state)
                return tuple(
                    OpenAttempt(a.round, a.index, a.attempt_id, "early_global")
                    if isinstance(a, OpenAttempt)
                    else a
                    for a in actions
                ) or (DecideRound(number),)
            grouping = state.grouping
            if grouping is not None and grouping["status"] == "started":
                return (RunGrouping(number),)
            if grouping is not None and grouping["status"] == "failed":
                if grouping["code"] == "role_failed":
                    # Family 14 (F0 R46, plan decision 11): a Grouper role
                    # failure owes its own class's suspension.
                    cause = grouper_suspension_for(grouping["error_class"])
                    return (FinishCampaign("infrastructure_failed", cause, "none"),)
                if grouping["code"] != "interrupted":
                    return (FinishCampaign("infrastructure_failed", "grouping_failed", "none"),)
                return (RunGrouping(number),)
            if state.focus_portion is None:
                if grouping is None:
                    return (RunGrouping(number),)
                return (SelectFocusPortion(number),)
            if state.focus_portion["reason"] in ("none", "unmeasurable"):
                # Nothing to chase: either nothing failed, or (family 6)
                # every failing cohort was empty once the excluded baseline
                # pairs left it. Both close the round with no reflection and
                # no attempt (spec §3.3, §4.3, §6).
                return (DecideRound(number),)
            if (
                state.probe_check is not None
                and state.probe_check["status"] == "unavailable"
                and not probe_continues(snapshot.identity, snapshot.family_version)
            ):
                return (FinishCampaign("infrastructure_failed", "probe_unavailable", "none"),)
            diagnosis = state.diagnosis(True)
            if diagnosis is None:
                return (RunReflection(number),)
            if state.gate_scope is None:
                if editor_input_reason(diagnosis) is not None:
                    # Spec §14, ADR-0022: a settled reflection that is no
                    # valid Editor input closes the round `unplanned`,
                    # with no gate scope and no Attempt.
                    return (DecideRound(number),)
                return (SelectGateScope(number),)
            if state.selection is not None:
                return (DecideRound(number),)
            actions = _round_actions(view, policy, state)
            if actions:
                return actions
            return _cohort_actions(view, policy, state)
    return (FinishCampaign("completed", "rounds_exhausted", "none"),)


#: `campaign_identity.refresh_measurement_id_for`'s shape.
_REFRESH_ID = re.compile(r"m-r[0-9]{2,}-refresh")


def measurement_side(snapshot: CampaignSnapshot, measurement_id: str) -> str:
    """`champion` for a baseline's measurement and a family-12 fresh Parent,
    `candidate` for an attempt's own (`side_of_entry`). An undeclared id is
    the Champion's only for the seed measurement and, in family 14 only, a
    refresh (no older family mints that shape)."""

    entry = snapshot.measurements.get(measurement_id)
    if entry is None:
        return (
            "champion"
            if measurement_id == measurement_id_for(None)
            or (snapshot.family_version == 14 and _is_refresh_id(measurement_id))
            else "candidate"
        )
    return side_of_entry(entry)


def _is_refresh_id(measurement_id: str) -> bool:
    """A family-14 refresh's id, `m-r<NN>-refresh` or its `@e<k>` rebind (F0 R43)."""

    return _REFRESH_ID.fullmatch(base_measurement_id(measurement_id)) is not None


def declared_measurements(snapshot: CampaignSnapshot) -> Sequence[str]:
    return tuple(snapshot.measurements)
