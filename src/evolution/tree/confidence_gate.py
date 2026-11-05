"""Family-13 and family-14 gate payloads, built from rows (design §6.2-§6.5).

This is the shared producer of gate payloads (plan choice 1): the
executor passes it live rows and the bound values, and the audit passes it the
rows of the prefix a record cites, so the two can never compute a payload two
ways. The module is pure: it reads what it is given, journals nothing, writes
no file and imports nothing from the executor. A caller makes a record durable
(`_Campaign.append`) or compares it with the journaled one (the audit).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_schema import (
    AttemptState,
    comparison_confidence,
    comparison_tolerance,
    confidence_scopes,
    look_caveats,
    look_tolerance,
    retained_cases,
)
from evolution.tree.campaign_statistics import (
    MissingMetric,
    PrimaryRange,
    SampleRow,
    case_set_coverage,
    critical_vetoed,
    exclude_pairs,
    row_fault,
    score_samples_of,
    stage_exhausted,
    stage_horizon,
    stage_lacking,
)
from evolution.tree.confidence_statistics import (
    DRAWS,
    INSUFFICIENT_REPEATS,
    CaseEvidence,
    Decision,
    Estimate,
    LookEstimates,
    NoiseCase,
    corpus_weights,
    decide_final_observe,
    decide_final_statistical,
    decide_interim,
    derive_seed,
    final_statistical,
    interim_focus,
    method_object,
    noise_calibration,
    noise_result_digest,
    observe_points,
    pairing_of,
    point_contrast,
    publish,
    scope_weights,
    would_pass_point,
)
from evolution.tree.final_statistics import (
    Estimate14,
    LookEstimates14,
    decide_final_observe14,
    decide_final_statistical14,
    decide_interim14,
    final_statistical14,
    interim_focus14,
    observe_points14,
    would_pass_point14,
)


class GateInvariant(ValueError):
    """A gate input this module cannot have been given by a correct caller (plan F10).

    The module cannot import the executor's `CampaignInvariantViolated`. The
    executor's `calibrate` and `evaluate_confidence` both re-raise this as one
    with its message, and any other `ValueError` the statistics kernel raises
    as one reading "the gate refused its own inputs", as family 12 does. The
    audit reports this as a failed precondition.
    """


@dataclass(frozen=True)
class GateInputs:
    """What one look reads of its governing comparison (F7, plan choice 1)."""

    looks: tuple[int, ...]
    final: bool
    horizon: int
    retained: Mapping[str, tuple[int, ...]]
    mask: frozenset[tuple[str, int]]


def gate_inputs(
    comparison: Mapping[str, Any],
    *,
    max_repeat: int,
    look: int,
    excluded: Collection[tuple[str, int]],
) -> GateInputs:
    """The one source of a family-13 look's looks, finality, horizon and mask.

    `comparison` is the Attempt's governing `comparison_bound` payload (the
    executor passes `attempt.comparison`; the audit, the binding the look
    record cites), never the policy, so a `continue` that changes the policy
    cannot move a bound look. `max_repeat` is the largest last scheduled
    repeat of the measurements the stage reads, `excluded` the baseline's
    `(case_id, repeat)` exclusions; the mask keeps those at repeats `<= look`
    (plan choice 1). Pure: it reads only what it is given.
    """

    looks = tuple(int(entry) for entry in comparison["looks"])
    if look not in looks:
        raise GateInvariant(f"look {look} is not one of the comparison's looks {list(looks)}")
    return GateInputs(
        looks=looks,
        final=look == max(looks),
        horizon=stage_horizon(looks, look, max_repeat),
        retained={
            name: tuple(int(index) for index in comparison["retained"][name])
            for name in ("focus", "causal")
        },
        mask=frozenset(pair for pair in excluded if pair[1] <= look),
    )


def comparison_record(
    policy: CampaignPolicyV11,
    *,
    attempt: AttemptState,
    baseline_measurement_id: str,
    corpus_cases: Sequence[Mapping[str, Any]],
    exam: Sequence[str],
    epoch: int,
    baseline_schedule: Sequence[tuple[str, int]],
    excluded: Collection[tuple[str, int]],
    exclusions: Mapping[str, Any] | None,
    evidence: Sequence[Mapping[str, Any]],
    family_version: int = 13,
) -> dict[str, Any]:
    """Design §6.2: the `comparison_bound` payload, with `retained` fixed by G6.

    F and C come from the Attempt's selection over the round's exam; every
    bound value is the policy's in force (PDR-0005). `retained` keeps each
    case with at least one scheduled baseline pair at R outside `excluded`,
    the baseline's exclusions as `(case_id, repeat)` pairs (plan choice 10).
    Explicit family 14 context also freezes the policy's normalized focus margin.
    """

    assert attempt.selection is not None and attempt.measurement_id is not None
    focus, causal = confidence_scopes(attempt.selection, corpus_cases, exam)
    corpus_ids = [str(case["case_id"]) for case in corpus_cases]
    retained = {
        name: retained_cases(cases, corpus_ids, baseline_schedule, excluded, policy.repeats)
        for name, cases in (("focus", focus), ("causal", causal))
    }
    payload = {
        "round": attempt.round,
        "attempt_id": attempt.attempt_id,
        "supersedes": None,
        "mode": policy.gate_mode,
        "epoch": epoch,
        "candidate_measurement_id": attempt.measurement_id,
        "parent_measurement_id": attempt.parent_measurement_id,
        "baseline_measurement_id": baseline_measurement_id,
        "parent_source": "fresh_parent" if policy.fresh_parent else "baseline",
        "focus": focus,
        "causal": causal,
        "retained": retained,
        "exclusions": None if exclusions is None else dict(exclusions),
        "repeats": policy.repeats,
        "looks": list(policy.looks),
        "confidence": comparison_confidence(policy),
        "tolerance": comparison_tolerance(policy),
        "method": method_object(),
        "evidence": list(evidence),
    }
    if family_version == 14:
        payload["margin"] = policy.focus_margin
    return payload


def calibration_record(
    comparison: Mapping[str, Any],
    calibration: Mapping[str, Any],
    *,
    parent_rows: Sequence[SampleRow],
    excluded: Collection[tuple[str, int]],
    groups: Mapping[str, tuple[str, str]],
    corpus_ids: Sequence[str],
    primary: str,
    horizon: int,
    prefix: str,
    campaign_seed: int,
    draws: int = DRAWS,
) -> dict[str, Any] | MissingMetric:
    """Design §6.4: the `noise_calibration_completed` payload (plan choice 11).

    The inputs are the retained C cases in corpus order. Each case's pool is
    its usable source observations -- every scored row outside `excluded` up
    to `horizon` (two for an observe R=1 `case_repeats` Parent) -- and both
    virtual sizes are its repeats up to R outside `excluded`. One `NoiseCase`
    per retained case; the kernel decides availability
    (`insufficient_calibration_repeats`, before any draw) and degeneracy.
    The seed binds to the calibration binding's own digest (plan choices 7, 8).
    """

    cases = _ids(corpus_ids, comparison["retained"]["causal"])  # corpus order, as `look_record`
    usable = [
        row
        for row in parent_rows
        if row.case_id in cases and (row.case_id, row.repeat) not in excluded
    ]
    samples = score_samples_of(usable, groups, primary, horizon)
    if isinstance(samples, MissingMetric):
        return samples
    repeats = int(comparison["repeats"])
    noise = []
    for case_id in cases:
        observations = tuple(sample for sample in samples if sample.case_id == case_id)
        # A retained case has an unexcluded pair and its stage is not barred
        # (F15), so a correct caller never gets here; the audit reports it.
        if not observations:
            raise GateInvariant(f"retained case {case_id} has no usable Parent observation")
        size = sum(1 for repeat in range(1, repeats + 1) if (case_id, repeat) not in excluded)
        noise.append(NoiseCase(case_id, observations, first=size, second=size))
    seed = derive_seed(
        campaign_seed=campaign_seed,
        binding=str(calibration["digest"]),
        scope="noise",
        prefixes=[prefix],
        look=None,
        purpose="noise",
    )
    result = noise_calibration(noise, scope_weights(cases, groups), seed=seed, draws=draws)
    return {
        "round": calibration["round"],
        "attempt_id": calibration["attempt_id"],
        "binding": calibration["seq"],
        "prefix": prefix,
        "allowance": None if result.allowance is None else publish(result.allowance),
        "degenerate": result.degenerate,
        "unavailable": result.unavailable,
        "result_digest": None if result.draws is None else noise_result_digest(result.draws),
    }


@dataclass(frozen=True)
class StageAdmission:
    """What a stage's rows admit before a look or a calibration reads them.

    `faults` are `(side, row, reason)` for every row `row_fault` classifies:
    the stage barrier suspends on them (#97 spec §4.1, ADR-0027 "missing or
    invalid primary metrics remain Scorer faults"). `held` are `(side, row)`
    for every row of the stage that holds the Attempt (rulings R13, R16;
    #120): a repair may still fill it, so nothing is computed over it. `side`
    indexes the caller's `sides`.
    """

    faults: tuple[tuple[int, SampleRow, str], ...]
    held: tuple[tuple[int, SampleRow], ...]

    @property
    def admitted(self) -> bool:
        return not self.faults and not self.held


def stage_admission(
    sides: Sequence[tuple[Sequence[SampleRow], PrimaryRange]],
    *,
    horizon: int,
    floor: int,
    primary: str,
    lacking: bool,
) -> StageAdmission:
    """The one stage-admission check the executor and the audit share (codex
    branch round 1, maintainer ruling 2026-10-04; ADR-0027).

    `sides` holds each measurement the Attempt schedules -- the Candidate and,
    when one is bound, the fresh Parent -- as its latest-try rows over every
    case it schedules, with the range rule that judges them. The fault rows
    are read over all of them, as the executor's `attempt_fault_rows` reads
    each measurement whole: at a look's prelude no try has started past the
    stage, so they are the stage's own fault rows. The held rows are the
    stage's, repeats after `floor` up to `horizon` (`stage_floor`,
    `stage_horizon`), by the executor's `lacking_at` rule: under `lacking`
    (`held_repair_required`) any row that did not score (`stage_lacking`),
    else an exhausted one (`stage_exhausted`).
    """

    faults: list[tuple[int, SampleRow, str]] = []
    held: list[tuple[int, SampleRow]] = []
    rule = stage_lacking if lacking else stage_exhausted
    for side, (rows, primary_range) in enumerate(sides):
        for row in rows:
            reason = row_fault(row, primary, primary_range=primary_range)
            if reason is not None:
                faults.append((side, row, reason))
            if rule([[row]], horizon, floor):
                held.append((side, row))
    return StageAdmission(faults=tuple(faults), held=tuple(held))


@dataclass(frozen=True)
class Unobserved:
    """Retained cases with no usable observation on a side at this look (E1)."""

    case_ids: tuple[str, ...]


def renormalized(weights: Mapping[str, Fraction]) -> dict[str, Fraction]:
    """The fixed G6 weights of the observed cases, rescaled to sum to 1 (E1 (a))."""

    total = sum(weights.values(), Fraction(0))
    return {case_id: weight / total for case_id, weight in weights.items()}


def observed_point(cases: Sequence[CaseEvidence], weights: Mapping[str, Fraction]) -> float | None:
    """E1 (a), maintainer ruling 2026-10-04: the scope's point over its observed
    retained cases with renormalized weights; `None` when none is observed."""

    observed = {case.case_id: weights[case.case_id] for case in cases if case.case_id in weights}
    if not observed:
        return None
    return publish(point_contrast(cases, renormalized(observed)))


def case_evidence(
    candidate: Sequence[SampleRow],
    parent: Sequence[SampleRow],
    look: int,
    *,
    cases: Sequence[str],
    groups: Mapping[str, tuple[str, str]],
    primary: str,
    excluded: frozenset[tuple[str, int]],
) -> tuple[tuple[CaseEvidence, ...], Unobserved | None] | MissingMetric:
    """The observed cases' evidence at `look`, in the given (corpus) order, and
    the unobserved ones (design §6.5, G6; plan choices 9, 10).

    `case_set_coverage` keeps a case that owes a pair at `look` and raises
    `CoverageMissing` on any owed pair a side did not score. Each side's
    samples are its rows up to `look` outside `excluded` (the baseline's
    exclusions leave both sides). A case that owes nothing at this look, or is
    left with no sample on a side, is unobserved: it gets no `CaseEvidence`.
    """

    kept, _dropped = case_set_coverage(parent, candidate, look, cases=cases, excluded=excluded)
    read = frozenset(kept)
    sides: list[tuple[Any, ...]] = []
    for rows in (candidate, parent):
        chosen = tuple(row for row in exclude_pairs(rows, excluded) if row.case_id in read)
        samples = score_samples_of(chosen, groups, primary, look)
        if isinstance(samples, MissingMetric):
            return samples
        sides.append(samples)
    evidence: list[CaseEvidence] = []
    unobserved: list[str] = []
    for case_id in cases:
        own = [tuple(sample for sample in side if sample.case_id == case_id) for side in sides]
        if case_id not in read or not own[0] or not own[1]:
            unobserved.append(case_id)
            continue
        evidence.append(CaseEvidence(case_id, candidate=own[0], parent=own[1]))
    return tuple(evidence), (Unobserved(tuple(unobserved)) if unobserved else None)


def _ids(corpus_ids: Sequence[str], indices: Sequence[Any]) -> list[str]:
    return [corpus_ids[int(index)] for index in sorted(int(index) for index in indices)]


def look_missing(
    comparison: Mapping[str, Any],
    sides: Mapping[str, Sequence[SampleRow]],
    *,
    corpus_ids: Sequence[str],
    excluded: Collection[tuple[str, int]],
    horizon: int,
) -> list[dict[str, Any]]:
    """The `look_not_started.missing` entries of a family-13 look; `[]` when evaluable.

    `sides` maps each measurement the look owes rows on -- the Candidate,
    then the fresh Parent when one is bound (a baseline source is settled) --
    to its latest-try rows. A row is named when its repeat is at most
    `horizon` (the stage's, so a `case_repeats` Parent's last repeat counts
    on the last look), its case is among the comparison's F ∪ C, its
    `(case_id, repeat)` pair is not in the baseline's `excluded` mask, and it
    did not score. Entries keep `_missing_rows`' shape and each side's row
    order (PDR-0015: a missing row is named, never scored).
    """

    scope = set(_ids(corpus_ids, [*comparison["focus"], *comparison["causal"]]))
    return [
        {
            "measurement_id": measurement_id,
            "case_id": row.case_id,
            "sample_id": row.sample_id,
            "state": row.disposition,
        }
        for measurement_id, rows in sides.items()
        for row in rows
        if row.repeat <= horizon
        and row.case_id in scope
        and (row.case_id, row.repeat) not in excluded
        and row.disposition != "score"
    ]


def look_record(
    comparison: Mapping[str, Any],
    calibration_result: Mapping[str, Any] | None,
    *,
    look: int,
    candidate_rows: Sequence[SampleRow],
    parent_rows: Sequence[SampleRow],
    excluded: frozenset[tuple[str, int]],
    groups: Mapping[str, tuple[str, str]],
    corpus_ids: Sequence[str],
    primary: str,
    prefixes: Mapping[str, str],
    campaign_seed: int,
    draws: int = DRAWS,
    family_version: int = 13,
) -> dict[str, Any] | MissingMetric:
    """Design §6.5: the `comparison_look_evaluated` payload without `evidence`.

    An interim look reads F's retained cases and draws the two-sided focus
    interval (`scope: focus`); the final look reads F ∪ C's retained cases in
    corpus order and draws one joint stream (`scope: joint`, §6.1), or, in
    observe, publishes points only. Weights are fixed over each scope's
    retained cases (G6, G9; plan F11). The seed binds to the comparison's
    digest and the cited prefixes, `[candidate, parent]` (plan choice 8);
    pairing is `pairing_of` (choice 9); the mask leaves both sides (choice 10).

    Explicit family context selects family 14's exact estimates and margin
    decisions, retaining the shared rows, weights, seeds and draw streams.

    The critical veto reads the Candidate's rows up to `look`, unexcluded, and
    rejects first (plan choice 4, R6); the estimates are still published.

    E1 (a), maintainer ruling 2026-10-04: at an interim look, a scope with an
    unobserved retained case is not drawn; its bounds are null, `unavailable`
    is `insufficient_repeats` and its point is the contrast over the observed
    retained cases with renormalized weights, null when none is observed. A
    final look is never barred over an unobserved retained case, so one here
    is a `GateInvariant`.
    """

    # `excluded` is the baseline's exclusions; the horizon is not read here.
    bound = gate_inputs(comparison, max_repeat=look, look=look, excluded=excluded)
    final = bound.final
    excluded = bound.mask
    focus_ids = _ids(corpus_ids, bound.retained["focus"])
    causal_ids = _ids(corpus_ids, bound.retained["causal"])
    read = (
        _ids(corpus_ids, [*bound.retained["focus"], *bound.retained["causal"]])
        if final
        else focus_ids
    )
    built = case_evidence(
        candidate_rows,
        parent_rows,
        look,
        cases=read,
        groups=groups,
        primary=primary,
        excluded=excluded,
    )
    if isinstance(built, MissingMetric):
        return built
    evidence, unobserved = built
    if unobserved is not None and final:
        raise GateInvariant("a final look has an unobserved retained case")
    critical = critical_vetoed([row for row in candidate_rows if row.repeat <= look])
    ordered = [str(prefixes["candidate"]), str(prefixes["parent"])]

    def seed(scope: str) -> int:
        return derive_seed(
            campaign_seed=campaign_seed,
            binding=str(comparison["digest"]),
            scope=scope,
            prefixes=ordered,
            look=look,
            purpose="gate",
        )

    focus_weights = scope_weights(focus_ids, groups)
    causal_weights = scope_weights(causal_ids, groups) if causal_ids else None
    tolerance = look_tolerance(comparison, calibration_result, family_version=family_version)
    observe = comparison["mode"] == "observe"
    estimates: LookEstimates | LookEstimates14 | None = None
    focus: Estimate | Estimate14
    decision: Decision
    if not final:
        level = comparison["confidence"]["focus"]
        if unobserved is not None:
            point = observed_point(evidence, focus_weights)
            focus = (
                Estimate14(point, None, None, level, INSUFFICIENT_REPEATS, False)
                if family_version == 14
                else Estimate(point, None, None, level, INSUFFICIENT_REPEATS)
            )
        else:
            interim = interim_focus14 if family_version == 14 else interim_focus
            focus = interim(
                evidence,
                focus_weights,
                confidence=level,
                pairing=pairing_of(evidence),
                seed=seed("focus"),
                draws=draws,
            )
        if family_version == 14:
            assert isinstance(focus, Estimate14)
            decision = decide_interim14(focus, margin=comparison["margin"], critical=critical)
        else:
            assert isinstance(focus, Estimate)
            decision = decide_interim(focus, critical=critical)
    else:
        overall = corpus_weights(read, groups, corpus_ids)
        if observe:
            observe_estimates = observe_points14 if family_version == 14 else observe_points
            estimates = observe_estimates(
                evidence, focus=focus_weights, causal=causal_weights, overall=overall
            )
            observe_decision = (
                decide_final_observe14 if family_version == 14 else decide_final_observe
            )
            decision = observe_decision(critical=critical)
        else:
            confidence = comparison["confidence"]
            statistical = final_statistical14 if family_version == 14 else final_statistical
            estimates = statistical(
                evidence,
                focus=focus_weights,
                causal=causal_weights,
                overall=overall,
                focus_confidence=confidence["focus"],
                causal_confidence=confidence["causal"],
                pairing=pairing_of(evidence),
                seed=seed("joint"),
                draws=draws,
            )
            if family_version == 14:
                assert isinstance(estimates, LookEstimates14)
                decision = decide_final_statistical14(
                    estimates.focus,
                    estimates.causal,
                    margin=comparison["margin"],
                    tolerance=tolerance["value"],
                    critical=critical,
                )
            else:
                assert isinstance(estimates, LookEstimates)
                decision = decide_final_statistical(
                    estimates.focus,
                    estimates.causal,
                    tolerance=tolerance["value"],
                    critical=critical,
                )
        focus = estimates.focus
    passed: bool | None = None
    if observe:
        assert estimates is not None
        # Observe has only a final look, whose points are never null (E1 (a)).
        causal_point = None if estimates.causal is None else estimates.causal.point
        if focus.point is None or (estimates.causal is not None and causal_point is None):
            raise GateInvariant("an observe look has a null point")
        passed = (
            would_pass_point14(
                focus.point, causal_point, tolerance["value"], margin=comparison["margin"]
            )
            if family_version == 14
            else would_pass_point(focus.point, causal_point, tolerance["value"])
        )
    return {
        "round": comparison["round"],
        "attempt_id": comparison["attempt_id"],
        "comparison": comparison["seq"],
        "look": look,
        "final": final,
        "prefixes": {"candidate": ordered[0], "parent": ordered[1]},
        "focus": focus.record(),
        "causal": None
        if estimates is None or estimates.causal is None
        else estimates.causal.record(),
        "overall": None if estimates is None else estimates.overall.record(),
        "tolerance": tolerance,
        "decision": decision.decision,
        "reason": decision.reason,
        "would_pass_point": passed,
        "caveats": look_caveats(
            comparison,
            tolerance,
            calibration_result,
            family_version=family_version,
            focus=focus.record(),
            causal=None
            if estimates is None or estimates.causal is None
            else estimates.causal.record(),
        ),
    }
