"""V2 statistics adaptation: evidence extraction, shared looks and thresholds.

Spec §7 (thresholds) and §8 (evidence, looks, alpha ledger, selector). This
module is the C1-owned bridge between the campaign journal's frozen record
vocabulary (`campaign_schema.py`) and the transferred bootstrap gate
(`statistics.py`): it turns durable per-sample facts into `ScoreSample`
evidence, decides whether a look is evaluable, wraps `gate_at_look`, arbitrates
crossed indeterminate thresholds. Current final selection belongs to the
final and independent kernels, not the shared look helpers here.

This module implements two statistics invariants:

1. `score_samples_of` and `look_evaluable` do not take the S1 status
   projection (it carries no provenance). They take `SampleRow`, a small
   frozen carrier of exactly the durable facts Task 5 assembles from the
   frozen schedule, the measurement identity's target and the campaign's
   `instance_attested` records.
2. `thresholds_reached` and `arbitrate_owner` implement spec §7's Barriers
   paragraph exactly: the owner order `target, environment, scorer, rollout`
   is fixed, a count threshold is `count > max_count`, a rate threshold is
   `count >= 1` and `Fraction(count, denominator) > Fraction(max_rate)` in
   exact arithmetic, and an owner crossing both is reported as `"count"`.

"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from evolution.tree.campaign_policy import CampaignPolicyV11, crosses
from evolution.tree.campaign_schema import (
    OWNERS,
    CampaignSnapshot,
    primary_range_required,
)
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.execution.journal_schema import Lifecycle
from evolution.tree.execution.sample_outcomes import SampleOutcome, disposition_for
from evolution.tree.statistics import GatePlan, GateResult, ScoreSample, gate_at_look
from evolution.tree.storage.envelope import Envelope

# Spec §7 Barriers: "the campaign reason is the first in the fixed order
# target, environment, scorer, rollout" — used by both `thresholds_reached`
# (iteration order) and `arbitrate_owner` (first match). It is the lifecycle's
# `OWNERS`, which arbitrates a `cohort_rows` suspension's owner by the same
# order, so the two cannot drift apart.
_OWNER_ORDER: tuple[str, ...] = OWNERS


@dataclass(frozen=True)
class SampleRow:
    """One durable per-sample fact, as Task 5 assembles it from the frozen
    schedule, the measurement identity's target and `instance_attested`.

    `outcome` is the sample's R2a-registry terminal outcome -- from its
    execution record, or from its scoring record when scoring reached a
    terminal of its own (`scorer_failed`, `interrupted`) -- and `None` for a
    scheduled sample with no durable terminal yet.

    `disposition` is the R2a disposition C1 maps (`score | indeterminate |
    exclude`) plus two states Task 5 adds for a row that has no durable
    evaluation (ruling 1: a caller's union of rows equals the schedule):
    `not_started` for a sample with no execution terminal, and `unscored` for
    one whose execution terminal is scoreable but whose scoring has none of its
    own. Neither is evidence: `score_samples_of` and `look_evaluable` key their
    reasoning on `disposition` alone, and both states block a look.

    A row is the sample's **latest try**: `retry_ordinal` numbers it (`0` for a
    measurement whose identity declares no `sample_retries`), and `exhausted`
    says that try left the sample without a score and no further try is
    admitted (`retry_ordinal >= sample_retries`, or `0` retries when undeclared,
    or -- I1 spec §5.4 -- the try is a repair try, which no D1 retry follows).
    A settled row is one that scored or is exhausted.

    `imported` marks a row carried in by `sample_imported` (baseline
    inheritance, spec §3.3): a scored row with its own provenance -- the
    source's effective seed and dependency digest, never the target's -- that no
    try produced, so it is neither a try nor exhausted.
    """

    case_id: str
    sample_id: str
    repeat: int
    requested_seed: int | None
    effective_seed: int | None
    boot_id: str
    dependency_digest: str
    outcome: str | None
    disposition: str
    metrics: Mapping[str, float] | None
    critical_pass: bool | None
    retry_ordinal: int = 0
    exhausted: bool = False
    imported: bool = False

    def __post_init__(self) -> None:
        if self.metrics is not None:
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True)
class MissingMetric:
    """The first `(case_id, sample_id)` row whose `metrics` lacks `primary`."""

    case_id: str
    sample_id: str


def sample_row(
    entry: Mapping[str, Any],
    execution: Envelope | None,
    scoring: Envelope | None,
    *,
    seed_mode: str,
    dependency_digest: str,
    boot_id: str,
    retry_ordinal: int = 0,
    limit: int = 0,
    imported: Envelope | None = None,
    repaired: bool = False,
) -> SampleRow:
    """One durable per-sample fact from its schedule entry and its two records.

    `execution` and `scoring` are the sample's latest try, numbered
    `retry_ordinal`; `limit` is the identity's `sample_retries` (`0` when it
    declares none), the last ordinal a D1 try may carry. `repaired` says the
    sample started a repair try (I1 spec §5.4): only a repair follows a repair,
    so its latest try is one, and an indeterminate repair exhausts the sample
    whatever its ordinal.

    The one reduction both the executor (`CampaignMeasurement.sample_rows`)
    and the audit (`_check_look_evaluated`) use, so neither can disagree with
    the other about a row's disposition or metrics (spec §5 step 1).

    `imported` is the pair's `sample_imported` record, when it was carried in:
    the lifecycle admits no try on such a pair, so the row is that record's
    score, with the seed and dependency digest the record carries.
    """

    if imported is not None:
        carried = imported.payload
        return SampleRow(
            case_id=str(entry["case_id"]),
            sample_id=str(entry["sample_id"]),
            repeat=int(entry["repeat"]),
            requested_seed=entry["requested_seed"],
            effective_seed=carried["effective_seed"],
            boot_id="",
            dependency_digest=str(carried["dependency_digest"]),
            outcome=str(carried["outcome"]),
            disposition="score",
            metrics=dict(carried["metrics"]),
            critical_pass=carried["critical_pass"],
            retry_ordinal=0,
            exhausted=False,
            imported=True,
        )
    outcome: str | None = None
    disposition = "not_started"
    metrics: Mapping[str, float] | None = None
    critical_pass: bool | None = None
    effective_seed: int | None = None
    requested_seed = entry["requested_seed"]
    if execution is not None and execution.event != "rollout_started":
        outcome = str(execution.payload["outcome"])
        # R1 refuses a session that does not confirm the requested seed, so
        # under `exact` a durable `rollout_finished` is the proof of it.
        if seed_mode == "exact" and execution.event == "rollout_finished":
            effective_seed = requested_seed
        kind = disposition_for(cast(SampleOutcome, outcome)).kind
        scoring_event = scoring.event if scoring is not None else None
        if kind != "score":
            disposition = kind
        elif scoring_event == "scoring_finished":
            disposition = "score"
            metrics = dict(scoring.payload["metrics"]) if scoring is not None else None
            critical_pass = scoring.payload["critical_pass"] if scoring is not None else None
        elif scoring_event == "scoring_failed":
            outcome, disposition = "scorer_failed", "indeterminate"
        elif scoring_event == "scoring_interrupted":
            outcome, disposition = "interrupted", "indeterminate"
        else:
            # A scoreable execution terminal whose scoring has none of its
            # own: terminal for the stage, not evidence for a look.
            disposition = "unscored"
    return SampleRow(
        case_id=str(entry["case_id"]),
        sample_id=str(entry["sample_id"]),
        repeat=int(entry["repeat"]),
        requested_seed=requested_seed,
        effective_seed=effective_seed,
        boot_id=boot_id,
        dependency_digest=dependency_digest,
        outcome=outcome,
        disposition=disposition,
        metrics=metrics,
        critical_pass=critical_pass,
        retry_ordinal=retry_ordinal,
        exhausted=disposition in ("indeterminate", "unscored")
        and (retry_ordinal >= limit or repaired),
    )


#: #97 spec §4.1: a scored row whose `metric.primary` lies outside [0, 1].
PRIMARY_OUT_OF_RANGE = "scorer_metric_out_of_range"


@dataclass(frozen=True)
class PrimaryRange:
    """#97 spec §4.1, ADR-0019 (amended 2026-10-03): which scored rows of one
    measurement a primary outside [0, 1] makes a fault row.

    `live` covers the rows a try of the measurement scored: the producing
    version in force decides (`primary_range_required`). `imported` covers the
    rows carried in by `sample_imported`: these are judged by the version
    their carry was admitted under as well (`import_versions`), so a row an
    earlier producer carried in is not a fault the current one cannot repair.
    """

    live: bool = False
    imported: bool = False

    def covers(self, row: SampleRow) -> bool:
        return self.imported if row.imported else self.live


#: No range rule: families 3-6 and every producer before `0.3.0a26`.
NO_PRIMARY_RANGE = PrimaryRange()


def primary_range_for(
    family_version: int, version: str, import_version: str | None
) -> PrimaryRange:
    """The one `PrimaryRange` the executor, the read-only view and the audit build.

    `version` is the producing version in force where the rows are judged;
    `import_version` the version the measurement's carried rows were admitted
    under (`None` when it carries none, which then follows `version`).
    """

    live = primary_range_required(family_version, version)
    imported = live and (
        import_version is None or primary_range_required(family_version, import_version)
    )
    return PrimaryRange(live=live, imported=imported)


def row_fault(
    row: SampleRow, primary: str, *, primary_range: PrimaryRange = NO_PRIMARY_RANGE
) -> str | None:
    """The campaign-level fault a latest-try row is, if any (`fault_rows`).

    `infrastructure_failed` -- `exclude` with reason `rollout_cancelled` is the
    caller's token, never a fault -- or a scored row missing the primary metric.
    Where `primary_range` covers the row (families 7-12 from `0.3.0a26`, #97
    spec §4.1) a scored primary outside [0, 1] is `scorer_metric_out_of_range`;
    the gate's kernel cannot use it.
    """

    if row.disposition == "exclude" and row.outcome == "infrastructure_failed":
        return "infrastructure_failed"
    if row.disposition == "score" and (row.metrics is None or primary not in row.metrics):
        return "scorer_metric_missing"
    if primary_range.covers(row) and row.disposition == "score":
        assert row.metrics is not None
        if not 0.0 <= float(row.metrics[primary]) <= 1.0:
            return PRIMARY_OUT_OF_RANGE
    return None


def owed_repair_rows(
    evidence: Mapping[str, Any],
    measurement_id: str,
    primary: str,
    *,
    schedule: Sequence[Mapping[str, Any]],
    latest_ordinal: Callable[[tuple[str, str]], int | None],
    records: Callable[[], Sequence[Envelope]],
    primary_range: PrimaryRange = NO_PRIMARY_RANGE,
) -> frozenset[tuple[str, str]]:
    """The rows of one measurement a `fault_rows` suspension still owes a repair try.

    I1 spec §5.4: a named row owes exactly one repair try while its latest try
    (`latest_ordinal`, `None` for a sample never started) is still the one the
    suspension named; once a try past it has started -- the repair, whatever it
    ended in -- it owes nothing more under this suspension.

    The named rows are the listed ones unless the evidence was truncated at
    `MAX_FAULT_ROWS`: then the complete set is re-derived from the measurement
    prefix as it stood at the suspension (`recompute_fault_rows`), and the
    listed rows are the fallback when it cannot be -- a suspension spanning
    another measurement's rows, whose digest one journal cannot reproduce.

    The one derivation the executor dispatches from and the planner plans from
    (through `campaign_view.owed_repairs`), live journal or read-only prefix.
    """

    named = {
        (str(row["case_id"]), str(row["sample_id"])): int(row["retry_ordinal"])
        for row in evidence["rows"]
        if row["measurement_id"] == measurement_id
    }
    if evidence["truncated"]:
        rebuilt = recompute_fault_rows(
            evidence, measurement_id, primary, schedule, records(), primary_range=primary_range
        )
        if rebuilt is not None:
            named = rebuilt
    return frozenset(key for key, ordinal in named.items() if latest_ordinal(key) == ordinal)


def stage_floor(looks: Sequence[int], look: int) -> int:
    """The last repeat before the stage of `look`: the declared look before it, or `0`."""

    return max((int(earlier) for earlier in looks if int(earlier) < look), default=0)


def stage_horizon(looks: Sequence[int], look: int, max_repeat: int) -> int:
    """The largest repeat a look's stage reads: the look, or at the last look
    the larger of the look and the measurement's last scheduled repeat (design
    §5, a `case_repeats` Parent). It is never less than the look.

    Invariant: for every measurement without `case_repeats`, `max_repeat ==
    max(looks)`, so the horizon equals the look on every stage. That is why the
    readers that call this without a family fence (`_held_at`, `inspect_view`,
    `lacking_at`, `_sides_held`, `_unfilled_slots`) read families 3-12 byte for
    byte as before (ADR-0008).
    """

    if look == max(int(item) for item in looks):
        return max(look, max_repeat)
    return look


def stage_exhausted(sides: Sequence[Sequence[SampleRow]], look: int, floor: int) -> bool:
    """#91 spec §3.1, rulings R13 and R16: does the latest stage hold an exhausted row?

    Only the rows of the stage that decides `look` -- repeats after `floor`
    (`stage_floor`) up to `look`, on any side -- count. An earlier look was
    decided over its own rows, so an exhausted row there (a run resumed from
    an earlier producer) does not hold the Attempt now.
    """

    return any(row.exhausted and floor < row.repeat <= look for rows in sides for row in rows)


def stage_lacking(sides: Sequence[Sequence[SampleRow]], look: int, floor: int) -> bool:
    """#120 (a27): does the latest stage hold a row that did not score?

    The stage's rows as `stage_exhausted` reads them (repeats after `floor` up
    to `look`, on any side). Once the stage is settled, a row that did not
    score is one no try will fill: an exhausted row (R13), a row `cancelled`
    by the caller's token, or a try whose retry the cancelled run declined.
    Either way the look lacks a pair and cannot be evaluated.
    """

    return any(
        row.disposition != "score" and floor < row.repeat <= look for rows in sides for row in rows
    )


def row_owes_a_try(row: SampleRow, declined: Container[tuple[str, str, int]] = ()) -> bool:
    """Is `row` still owed a try, so that its stage is not settled?

    A row not started, or one whose latest try ended without a score while a
    D1 try is still admitted: not exhausted, and not a try whose retry the
    cancelled run declined (`declined`, `(case_id, sample_id, retry_ordinal)`
    from the campaign's `retries_declined`; #120). A scored row, an `exclude`
    row (a `cancelled` try is the token's, never retried), an exhausted row and
    a declined try owe none. `rows_held` and the family-13 cohort's gaps
    (`confidence_cohort.cohort_gap`) both read this one rule.
    """

    return row.disposition == "not_started" or (
        row.disposition in ("indeterminate", "unscored")
        and not row.exhausted
        and (row.case_id, row.sample_id, row.retry_ordinal) not in declined
    )


def rows_held(
    sides: Sequence[tuple[Sequence[SampleRow], PrimaryRange]],
    look: int,
    floor: int,
    primary: str,
    *,
    lacking: bool = False,
    declined: Sequence[Container[tuple[str, str, int]]] = (),
) -> bool:
    """#91 spec §3.1, ruling R13: the one definition of a held Attempt's rows.

    `sides` holds each of the Attempt's measurements -- the Candidate and, in
    family 12, the fresh Parent -- as its latest-try rows up to `look` and
    which of them a primary outside [0, 1] makes a fault row (`PrimaryRange`). Held when no row is
    a fault row, every row has started and scored or exhausted its tries (no
    D1 try still owed), and the latest stage holds an exhausted row
    (`stage_exhausted`). No row past the latest stage's look has a try, so
    the rows up to it are the measurement's whole fault set. The executor's
    `lacking_attempts` and the audit's held-Attempt checks both read this.

    `lacking` (#120, from `0.3.0a27`, `held_repair_required`) widens the rule
    to a settled stage whose look lacks pairs for any reason
    (`stage_lacking`): a forced stop's `cancelled` rows hold the Attempt too.
    A `cancelled` row was always settled -- the token's `exclude`, never
    retried. Under `lacking` a try whose retry the cancelled run declined
    (`declined`, one container of `(case_id, sample_id, retry_ordinal)` per
    side, from the campaign's `retries_declined`) owes no D1 try either, so it
    is settled as well.
    """

    if lacking and len(declined) != len(sides):
        raise ValueError(
            f"rows_held: declined has {len(declined)} containers for {len(sides)} sides"
        )
    for index, (rows, primary_range) in enumerate(sides):
        skipped = declined[index] if lacking else ()
        for row in rows:
            if row_fault(row, primary, primary_range=primary_range) is not None:
                return False
            if row_owes_a_try(row, skipped):
                return False
    stage = [rows for rows, _range in sides]
    if lacking:
        return stage_lacking(stage, look, floor)
    return stage_exhausted(stage, look, floor)


def declined_tries(
    retries_declined: Iterable[tuple[Any, ...]], measurement_id: str
) -> frozenset[tuple[str, str, int]]:
    """One measurement's declined retries, as `rows_held(declined=...)` reads them.

    `retries_declined` is the campaign's `CampaignSnapshot.retries_declined`:
    `(measurement_id, case_id, sample_id, retry_ordinal)` for each accounted
    try whose `indeterminate_observed` says no retry follows it.
    """

    return frozenset(
        (str(key[1]), str(key[2]), int(key[3]))
        for key in retries_declined
        if len(key) == 4 and key[0] == measurement_id
    )


def restored_tries(
    listing_restored: Iterable[tuple[Any, ...]], measurement_id: str
) -> frozenset[tuple[str, str, int]]:
    """One measurement's restored tries, keyed as `declined_tries` keys them.

    `listing_restored` is the campaign's `CampaignSnapshot.listing_restored`:
    `(measurement_id, case_id, sample_id, retry_ordinal)` of each try a
    `cohort_rows` or (#194, plan choice 7) `champion_rows` listing restored, so
    its successor is a D1 retry, not a repair. Empty for a run that never lists.
    """

    return frozenset(
        (str(key[1]), str(key[2]), int(key[3]))
        for key in listing_restored
        if len(key) == 4 and key[0] == measurement_id
    )


def pre_fix_out_of_range(
    rows: Sequence[SampleRow],
    primary: str,
    *,
    completed_before_fix: bool,
    imported_before_fix: bool,
) -> bool:
    """#97 spec §4.4, ADR-0019 (amended 2026-10-03): does a gate input row carry a
    primary outside [0, 1] that was written before the fix?

    `rows` are one measurement's gate inputs (`affected_gate.gate_input_rows`).
    A row qualifies when its measurement was completed under an exempt
    version (`completed_before_fix`), or when it was carried in by
    `sample_imported` and the carry was admitted under one
    (`imported_before_fix`). The executor's historical closure and the audit
    share it.
    """

    return any(
        (completed_before_fix or (row.imported and imported_before_fix))
        and row.metrics is not None
        and primary in row.metrics
        and not 0.0 <= float(row.metrics[primary]) <= 1.0
        for row in rows
    )


def owed_threshold_rows(
    named: Mapping[tuple[str, str], tuple[int, int]],
    latest_ordinal: Callable[[tuple[str, str]], int | None],
) -> frozenset[tuple[str, str]]:
    """#78 (spec §3.2, condition 5): the named pairs whose repair has not started.

    `named` is the lifecycle's grant for one measurement (`threshold_named`);
    a pair owes its repair while its latest started try is still the named
    one. Once a try past it has started -- the repair, whatever it ended in --
    the grant is spent.
    """

    return frozenset(
        key for key, (ordinal, _naming) in named.items() if latest_ordinal(key) == ordinal
    )


def repair_cuts(records: Sequence[Envelope]) -> tuple[int | None, ...]:
    """The prefixes a suspension may have read, latest first (I1 spec §5.4).

    The whole journal (`None`), then each position just before a `repair: true`
    `rollout_started`, latest first: until the owed rows settle, the only tries
    started after a suspension are repair tries. A position `p` names the
    prefix `records[:p]`; `recompute_fault_rows` searches them latest first.
    """

    cuts = [
        position
        for position, record in enumerate(records)
        if record.event == "rollout_started"
        and "case_id" in record.payload
        and "sample_id" in record.payload
        and record.payload.get("repair") is True
    ]
    return (None, *reversed(cuts))


def recompute_fault_rows(
    evidence: Mapping[str, Any],
    measurement_id: str,
    primary: str,
    schedule: Sequence[Mapping[str, Any]],
    records: Sequence[Envelope],
    *,
    primary_range: PrimaryRange = NO_PRIMARY_RANGE,
) -> dict[tuple[str, str], int] | None:
    """A measurement's fault rows as the suspension saw them, or `None`.

    The one recomputation the executor's owed set and the audit both read
    (I1 spec §5.2, §5.4): the audit refuses a `fault_rows` suspension this
    returns `None` for, because no cut of the measurement's own prefix
    reproduces the count and digest its evidence claims.

    A suspension follows a stage every opened sample of which ran to its
    terminal, and until the owed rows settle the only tries started after it
    are repair tries (ADR-0008, I1 spec §5.4). So the prefix the suspension
    read is this one cut just before one of those repair starts, or the whole
    of it when none has started yet. Each repair changes the one row it
    repairs, so the *latest* cut whose fault rows reproduce the evidence's
    `count` and `rows_digest` is the suspension's own.
    """

    from evolution.tree.campaign_schema import fault_rows_digest

    # Per sample: every try as [start position, execution terminal, scoring terminal].
    tries: dict[tuple[str, str], list[list[Any]]] = {}
    imported: dict[tuple[str, str], Envelope] = {}
    for position, record in enumerate(records):
        payload = record.payload
        if "case_id" not in payload or "sample_id" not in payload:
            continue
        key = (str(payload["case_id"]), str(payload["sample_id"]))
        if record.event == "sample_imported":
            imported[key] = record
        elif record.event == "rollout_started":
            tries.setdefault(key, []).append([position, None, None])
        elif record.event.startswith("rollout_") and key in tries:
            tries[key][-1][1] = record
        elif record.event.startswith("scoring_") and key in tries:
            tries[key][-1][2] = None if record.event == "scoring_started" else record
    for cut in repair_cuts(records):
        rows: dict[tuple[str, str], tuple[int, str]] = {}
        for entry in schedule:
            key = (str(entry["case_id"]), str(entry["sample_id"]))
            started = [t for t in tries.get(key, ()) if cut is None or t[0] < cut]
            if not started and key not in imported:
                continue
            execution, scoring = (started[-1][1], started[-1][2]) if started else (None, None)
            ordinal = 0 if execution is None else int(execution.payload.get("retry_ordinal", 0))
            row = sample_row(
                entry,
                execution,
                scoring,
                seed_mode="none",
                dependency_digest="",
                boot_id="",
                retry_ordinal=ordinal,
                imported=None if started else imported.get(key),
            )
            reason = row_fault(row, primary, primary_range=primary_range)
            if reason is not None:
                rows[key] = (ordinal, reason)
        keys = [
            (measurement_id, case_id, sample_id, ordinal, reason)
            for (case_id, sample_id), (ordinal, reason) in rows.items()
        ]
        if (
            len(keys) == int(evidence["count"])
            and fault_rows_digest(keys) == evidence["rows_digest"]
        ):
            return {key: ordinal for key, (ordinal, _reason) in rows.items()}
    return None


def latest_rows(
    records: Sequence[Envelope],
    state: Lifecycle,
    *,
    schedule: Sequence[Mapping[str, Any]] | None = None,
    look: int | None = None,
    seed_mode: str = "none",
    dependency_digest: str = "",
    boot_id: Callable[[tuple[str, str]], str] | None = None,
) -> tuple[SampleRow, ...]:
    """Each scheduled sample's latest-try row over a measurement prefix.

    `CampaignMeasurement.sample_rows` over bytes instead of a live journal: S1's
    `executions`/`scoring` hold the latest try, the row is numbered by its
    execution's `retry_ordinal` and bounded by the identity's `sample_retries`
    (`0` when undeclared), so `exhausted` means what it means to the executor.
    `schedule` defaults to the prefix's own; the seed, dependency and boot facts
    matter only to a caller building gate evidence.
    """

    if not records:
        # A born sibling whose journal holds no admitted record yet has no rows.
        return ()
    identity = records[0].payload["identity"]
    limit = int(identity.get("sample_retries") or 0)
    rows = []
    for entry in identity["schedule"] if schedule is None else schedule:
        if look is not None and int(entry["repeat"]) > look:
            continue
        key = (str(entry["case_id"]), str(entry["sample_id"]))
        execution = state.executions.get(key)
        rows.append(
            sample_row(
                entry,
                execution,
                state.scoring.get(key),
                seed_mode=seed_mode,
                dependency_digest=dependency_digest,
                boot_id="" if boot_id is None else boot_id(key),
                retry_ordinal=(
                    0 if execution is None else int(execution.payload.get("retry_ordinal", 0))
                ),
                limit=limit,
                imported=state.imported.get(key),
                repaired=key in state.repairs,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class MeasurementTries:
    """The per-measurement try facts the family-6 projections report (spec §8).

    `tries` counts every started try of every sample; `exhausted_repeats` is the
    schedule repeat of each exhausted pair, in schedule order, so the exclusion
    in force at a look `L` is the number of entries `<= L`. `imported` counts the
    pairs carried in by `sample_imported`, which are neither tries nor exhausted.

    Family 7 (I1 spec §3.4) adds what the call retries cost this measurement:
    `call_retries` counts the target calls that ran a second or later attempt,
    and `call_failures_by_code` counts each attempt that failed, by the code
    its record carries. Both inform; neither changes a statistic, a gate
    decision or a terminal. They are `0` and empty below family 7, whose
    journals carry neither key.

    #78 adds what a resume's repairs did: `repair_tries` counts the repair
    tries (fault-row or threshold) started with `repair: true`, and
    `repairs_scored` counts the repaired pairs whose latest row is scored. Both
    inform and change no statistic.
    """

    tries: int
    exhausted_repeats: tuple[int, ...]
    imported: int = 0
    call_retries: int = 0
    call_failures_by_code: Mapping[str, int] = field(default_factory=dict)
    repair_tries: int = 0
    repairs_scored: int = 0


def measurement_tries(records: Sequence[Envelope]) -> MeasurementTries:
    """Reduce a measurement prefix to its `MeasurementTries`."""

    state = Lifecycle()
    for record in records:
        state.accept(record)
    rows = latest_rows(records, state)
    failures: dict[str, int] = {}
    for record in records:
        code = record.payload.get("error_code")
        if code is not None:
            failures[str(code)] = failures.get(str(code), 0) + 1
    repair_starts = [
        record
        for record in records
        if record.event == "rollout_started" and record.payload.get("repair") is True
    ]
    repaired = {(str(r.payload["case_id"]), str(r.payload["sample_id"])) for r in repair_starts}
    return MeasurementTries(
        tries=sum(1 for record in records if record.event == "rollout_started"),
        exhausted_repeats=tuple(row.repeat for row in rows if row.exhausted),
        imported=len(state.imported),
        # One count per *attempt* past the first, so a retried call contributes
        # once however many records it writes: the start is the attempt.
        call_retries=sum(
            1
            for record in records
            if record.event.endswith("_started") and int(record.payload.get("call_attempt", 1)) > 1
        ),
        call_failures_by_code=dict(sorted(failures.items())),
        repair_tries=len(repair_starts),
        repairs_scored=sum(
            1
            for row in rows
            if (row.case_id, row.sample_id) in repaired and row.disposition == "score"
        ),
    )


def case_groups(
    cases: Sequence[EvalCase] | Sequence[Mapping[str, str]],
) -> dict[str, tuple[str, str]]:
    """`case_id -> (group_id, stratum)` from `EvalCase`s or from the identity's `corpus_cases`."""

    groups: dict[str, tuple[str, str]] = {}
    for case in cases:
        if isinstance(case, EvalCase):
            groups[case.case_id] = (case.group_id, case.stratum)
        else:
            groups[str(case["case_id"])] = (str(case["group_id"]), str(case["stratum"]))
    return groups


def score_samples_of(
    rows: Sequence[SampleRow],
    cases: Mapping[str, tuple[str, str]] | Sequence[EvalCase],
    primary: str,
    look: int,
) -> tuple[ScoreSample, ...] | MissingMetric:
    """Build `look`'s `ScoreSample` evidence from `score`-disposition rows.

    A `scoring_finished` without `metric.primary` is spec §8's
    `infrastructure_failed(scorer_metric_missing)`: this function does not
    raise for it — the campaign fault is the caller's decision — it names the
    first offending row as `MissingMetric` instead. `ScoreSample.sample_id`
    carries the repeat number (not the row's durable `sample_id`), because
    that is what `statistics._validate_exact_proof` pairs across sides.

    `cases` is either the corpus itself or the `case_id -> (group_id,
    stratum)` mapping `case_groups` builds from it — the audit already has
    the mapping (from the identity's durable `corpus_cases`) and building a
    fresh `EvalCase` sequence just to re-derive it would be wasted work.
    """

    groups = cases if isinstance(cases, Mapping) else case_groups(cases)
    result: list[ScoreSample] = []
    for row in rows:
        if row.disposition != "score" or row.repeat > look:
            continue
        group = groups.get(row.case_id)
        if group is None:
            raise ValueError("score_samples_of requires every row's case in the corpus")
        if row.metrics is None or primary not in row.metrics:
            return MissingMetric(row.case_id, row.sample_id)
        group_id, stratum = group
        result.append(
            ScoreSample(
                case_id=row.case_id,
                sample_id=str(row.repeat),
                score=row.metrics[primary],
                requested_seed=row.requested_seed,
                effective_seed=row.effective_seed,
                boot_id=row.boot_id,
                dependency_digest=row.dependency_digest,
                group_id=group_id,
                stratum=stratum,
            )
        )
    return tuple(result)


def exclude_pairs(
    rows: Sequence[SampleRow], excluded: frozenset[tuple[str, int]]
) -> tuple[SampleRow, ...]:
    """The rows whose `(case_id, repeat)` pair is not excluded.

    Continuable-terminals spec §4.3: an excluded baseline pair leaves the
    comparison on *both* sides, so the same set is applied to the Parent's
    rows and to the Candidate's before any evidence is built from them. The
    pair is keyed by case and repeat, not sample id (ruling R13): under
    `unpaired`/`none` seeds the two sides' sample ids differ.
    """

    return tuple(row for row in rows if (row.case_id, row.repeat) not in excluded)


def look_evaluable(
    baseline_rows: Sequence[SampleRow],
    attempt_rows: Sequence[SampleRow],
    look: int,
    *,
    excluded: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[bool, tuple[dict[str, Any], ...]]:
    """Spec §8: look `L` is evaluable when both sides have a `score`
    disposition for every case at every repeat `<= L`.

    Each missing entry is `{case_id, sample_id, state}`; `state` is the
    blocking row's own disposition when one exists, `"not_started"` when the
    repeat has not reached a terminal row at all on that side.

    `excluded` (family 6, continuable-terminals spec §4.3) names the
    `(case_id, repeat)` pairs the baseline excluded: such a pair is neither
    required nor reported missing on either side, so `missing` names only the
    remaining pairs.
    """

    skipped = excluded
    expected_cases = sorted(
        {
            row.case_id
            for row in (*baseline_rows, *attempt_rows)
            if (row.case_id, row.repeat) not in excluded
        }
    )
    missing: list[dict[str, Any]] = []
    for rows in (baseline_rows, attempt_rows):
        by_case_repeat = {(row.case_id, row.repeat): row for row in rows}
        for case_id in expected_cases:
            for repeat in range(1, look + 1):
                if (case_id, repeat) in skipped:
                    continue
                row = by_case_repeat.get((case_id, repeat))
                if row is None:
                    missing.append(
                        {"case_id": case_id, "sample_id": str(repeat), "state": "not_started"}
                    )
                elif row.disposition != "score":
                    missing.append(
                        {
                            "case_id": row.case_id,
                            "sample_id": row.sample_id,
                            "state": row.disposition,
                        }
                    )
    return (not missing, tuple(missing))


def evaluate_look(
    baseline: Sequence[ScoreSample], candidate: Sequence[ScoreSample], plan: GatePlan, look: int
) -> GateResult:
    """A thin wrapper over `gate_at_look`. `gate_at_look` itself already
    refuses an undeclared look as its first line, so this does not repeat
    that check."""

    return gate_at_look(baseline, candidate, plan, look)


class CoverageMissing(Exception):
    """A named case lacks a scored row on one side at a pair the comparison owes (G2 spec §3.4).

    Missing coverage is a defect in the caller or the journal, never narrowed
    away: `missing` names every `(side, case_id, repeat)` that lacks one,
    `side` being `parent` or `candidate`.

    This is deliberately not a `ValueError`: the executor and the audit turn a
    `ValueError` from a gate call into an infrastructure close, and a coverage
    defect must not be closed that way. Argument errors stay `ValueError`.
    """

    def __init__(self, missing: tuple[tuple[str, str, int], ...]) -> None:
        super().__init__("case set lacks coverage")
        self.missing = missing


def case_set_coverage(
    parent: Sequence[SampleRow],
    candidate: Sequence[SampleRow],
    look: int,
    *,
    cases: Sequence[str],
    excluded: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The named `cases` split into `(kept, dropped)` at `look` (G2 spec §3.4).

    A case owes a scored row on both sides for each repeat `1..look` that
    `excluded` does not name; one that owes nothing is dropped. Any other gap
    raises `CoverageMissing`, naming every `(side, case_id, repeat)` that
    lacks one. Both keep the caller's order. Family 12's `compare_on_cases`
    and family 13's `confidence_gate.case_evidence` share this rule.
    """

    scored: dict[tuple[str, str], set[int]] = {}
    for side, rows in (("parent", parent), ("candidate", candidate)):
        for row in rows:
            if row.disposition == "score":
                scored.setdefault((side, row.case_id), set()).add(row.repeat)

    kept: list[str] = []
    dropped: list[str] = []
    missing: list[tuple[str, str, int]] = []
    for case_id in cases:
        owed = [repeat for repeat in range(1, look + 1) if (case_id, repeat) not in excluded]
        if not owed:
            dropped.append(case_id)
            continue
        kept.append(case_id)
        for side in ("parent", "candidate"):
            have = scored.get((side, case_id), set())
            missing.extend((side, case_id, repeat) for repeat in owed if repeat not in have)
    if missing:
        raise CoverageMissing(tuple(missing))
    return tuple(kept), tuple(dropped)


@dataclass(frozen=True)
class CaseSetComparison:
    """One comparison on a named case set (G2 spec §3.4).

    `cases` are the named cases that entered, in the caller's order;
    `dropped` those whose every pair up to the look was excluded, as the
    layered gate's `remaining` narrowing drops them today. `result` is `None`
    when every named case dropped out -- the caller decides what that closes.
    """

    result: GateResult | None
    cases: tuple[str, ...]
    dropped: tuple[str, ...]


def compare_on_cases(
    parent: Sequence[SampleRow],
    candidate: Sequence[SampleRow],
    plan: GatePlan,
    look: int,
    *,
    cases: Sequence[str],
    groups: Mapping[str, tuple[str, str]],
    primary: str,
    excluded: frozenset[tuple[str, int]] = frozenset(),
) -> CaseSetComparison | MissingMetric:
    """Compare two measurement sides on the named `cases` at `look` (G2 spec §3.4).

    Only the named cases are read on either side; rows of any other case, or
    beyond `look`, are ignored. Every named case owes a scored row on both
    sides for each repeat `1..look` that `excluded` does not name; a case that
    owes nothing drops out, and any other gap raises `CoverageMissing`. The
    interval is `gate_at_look`'s, unchanged: its local random stream, its
    `exact` pairing proof and its partial counts after exclusion.
    """

    named = tuple(cases)
    if not named or len(set(named)) != len(named):
        raise ValueError("a case set names each case once and is not empty")
    if any(case_id not in groups for case_id in named):
        raise ValueError("a case set names only corpus cases")
    if look not in plan.alpha_spend:
        raise ValueError("gate look is not declared")

    kept, dropped = case_set_coverage(parent, candidate, look, cases=named, excluded=excluded)
    if not kept:
        return CaseSetComparison(None, (), dropped)

    keep = frozenset(kept)
    sides: list[tuple[ScoreSample, ...]] = []
    for rows in (parent, candidate):
        chosen = tuple(row for row in exclude_pairs(rows, excluded) if row.case_id in keep)
        samples = score_samples_of(chosen, groups, primary, look)
        if isinstance(samples, MissingMetric):
            return samples
        sides.append(samples)
    partial = any(
        (case_id, repeat) in excluded for case_id in kept for repeat in range(1, look + 1)
    )
    result = gate_at_look(sides[0], sides[1], plan, look, excluded_repeats=partial)
    return CaseSetComparison(result, kept, dropped)


def critical_vetoed(rows: Sequence[SampleRow]) -> bool:
    """Spec §8: a Candidate sample with `critical_pass is False` is a veto."""

    return any(row.critical_pass is False for row in rows)


def thresholds_reached(
    snapshot: CampaignSnapshot, policy: CampaignPolicyV11
) -> tuple[dict[str, Any], ...]:
    """Spec §7 Barriers, over each owner's own window (`snapshot.window_count`,
    `.window_denominator_for`; I1 spec §5.3): the tries since the last
    `campaign_suspended`, or since `campaign_created` before a first
    suspension -- or, for an owner whose field an in-place condition change
    moved after that, since the change (I2 spec §6.5). Families <= 6 never set
    a window base nor restart a window, so their window equals the campaign's
    totals and no family branch is needed here.

    Returned in the fixed owner order; an owner crossing both count and rate
    is reported once, as `"count"`.
    """

    crossed: list[dict[str, Any]] = []
    for owner in _OWNER_ORDER:
        threshold = policy.indeterminate[owner]  # type: ignore[index]
        count = snapshot.window_count(owner, count_recovered=threshold.effective_count_recovered)
        denominator = snapshot.window_denominator_for(owner)
        count_crossed = crosses(count, denominator, threshold, "count")
        rate_crossed = crosses(count, denominator, threshold, "rate")
        if count_crossed or rate_crossed:
            crossed.append(
                {
                    "owner": owner,
                    "kind": "count" if count_crossed else "rate",
                    "count": count,
                    "denominator": denominator,
                    "max_count": threshold.max_count,
                    "max_rate": threshold.max_rate,
                }
            )
    return tuple(crossed)


def arbitrate_owner(crossed: Sequence[str]) -> str:
    """Spec §7 Barriers: "the campaign reason is the first in the fixed
    order"."""

    crossed_set = set(crossed)
    for owner in _OWNER_ORDER:
        if owner in crossed_set:
            return owner
    raise ValueError("arbitrate_owner requires at least one crossed owner")
