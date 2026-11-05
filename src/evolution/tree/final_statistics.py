"""Pure exact-comparison and final-contrast statistics for family 14.

The kernel preserves deterministic seeds, streams, and `METHOD_VERSION`, and
never writes `degenerate_interval`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from evolution.tree.confidence_statistics import (
    BOUND_NOT_REQUESTED_IN_OBSERVE,
    DEGENERATE_INTERVAL,
    DRAWS,
    INSUFFICIENT_CALIBRATION_REPEATS,
    INSUFFICIENT_REPEATS,
    NOISE_ALLOWANCE_UNAVAILABLE,
    NOT_REQUESTED_IN_OBSERVE,
    CalibrationVerdict,
    CaseEvidence,
    Decision,
    Estimate,
    Pairing,
    PublishedValue,
    _case_map,
    _ordered,
    _proves,
    _values,
    cohort_contrast,
    confidence_fraction,
    final_statistical,
    interim_focus,
    observe_points,
    pairing_of,
    point_contrast,
    publish,
    published_fraction,
)
from evolution.tree.statistics import ScoreSample, _validate_sample

#: M2, R28: the reason and the `unavailable` value of a finalist with no case left.
FINAL_INCOMPLETE = "final_incomplete"
#: M6: the one focus rejection reason, at the interim and the final look.
FOCUS_BELOW_MARGIN = "focus_below_margin"
#: R27: family-14 look caveat names; the caveat sets themselves are H4's.
EXACT_CONTRAST = "exact_contrast"
NOISE_NOT_OBSERVED = "noise_not_observed"

#: R22: every `unavailable` a family-14 estimate may carry. No `degenerate_interval`.
UNAVAILABLE14: frozenset[str] = frozenset(
    {
        INSUFFICIENT_REPEATS,
        NOT_REQUESTED_IN_OBSERVE,
        BOUND_NOT_REQUESTED_IN_OBSERVE,
        FINAL_INCOMPLETE,
    }
)
_ESTIMATE14_KEYS = frozenset({"point", "lower", "upper", "level", "unavailable", "exact"})


def _equal(left: PublishedValue | None, right: PublishedValue | None) -> bool:
    return (
        left is not None
        and right is not None
        and published_fraction(left) == published_fraction(right)
    )


@dataclass(frozen=True)
class Estimate14:
    """R22: family 13's estimate object plus `exact`.

    `exact` is null when no bound is requested (`observe`), otherwise a
    boolean: true only when the bootstrap's resampled quantity shows no
    within-case variation (R21). An exact estimate is available and its bounds
    equal its point: `lower == point`, and `upper == point` when it is
    two-sided (R21's consistency rule, checked here so the lifecycle can reuse
    it through `from_record`). Deliberately not a subclass of `Estimate`, so a
    family-14 estimate is never accepted where family 13's is typed.
    """

    point: PublishedValue | None
    lower: PublishedValue | None
    upper: PublishedValue | None
    level: PublishedValue | None
    unavailable: str | None
    exact: bool | None

    def __post_init__(self) -> None:
        if self.exact is not None and type(self.exact) is not bool:
            raise ValueError("exact is a boolean or null")
        if self.unavailable is not None and self.unavailable not in UNAVAILABLE14:
            raise ValueError("a family-14 estimate is never degenerate_interval")
        if (self.level is None) != (self.exact is None):
            raise ValueError("exact is null if and only if an estimate has no level")
        if self.exact:
            if self.unavailable is not None:
                raise ValueError("an exact estimate is available")
            if not _equal(self.lower, self.point):
                raise ValueError("an exact estimate's lower bound is its point")
            if self.upper is not None and not _equal(self.upper, self.point):
                raise ValueError("an exact estimate's upper bound is its point")

    def record(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "lower": self.lower,
            "upper": self.upper,
            "level": self.level,
            "unavailable": self.unavailable,
            "exact": self.exact,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Estimate14:
        """A published estimate read back, with its six keys and R21's consistency checked."""

        if set(record) != _ESTIMATE14_KEYS:
            raise ValueError("a family-14 estimate has exactly its six keys")
        return cls(
            record["point"],
            record["lower"],
            record["upper"],
            record["level"],
            record["unavailable"],
            record["exact"],
        )


def exact_scope(cases: Sequence[CaseEvidence], scope: Collection[str], pairing: Pairing) -> bool:
    """R21: does no retained case of `scope` vary in what the bootstrap resamples?

    Under `exact` pairing the resampled quantity is the paired difference,
    matched by sample id as `joint_replicates` matches it; otherwise it is each
    side's values. Compared as exact `Fraction`s. Callers run it after the
    `insufficient_repeats` check, which keeps precedence (design §6.3).
    """

    if pairing not in ("exact", "unpaired"):
        raise ValueError("pairing is 'exact' or 'unpaired'")
    by_id = _case_map(cases)
    if not scope or not set(scope) <= set(by_id):
        raise ValueError("a scope names given cases and is not empty")
    if pairing == "exact" and not all(_proves(by_id[case_id]) for case_id in scope):
        raise ValueError("exact pairing requires the provenance proof")
    for case_id in scope:
        case = by_id[case_id]
        if pairing == "exact":
            parent = {sample.sample_id: Fraction(sample.score) for sample in case.parent}
            differences = {
                Fraction(sample.score) - parent[sample.sample_id] for sample in case.candidate
            }
            if len(differences) != 1:
                return False
        elif len(set(_values(case.candidate))) != 1 or len(set(_values(case.parent))) != 1:
            return False
    return True


@dataclass(frozen=True)
class LookEstimates14:
    """A family-14 final look: focus and causal carry `exact`; overall stays a five-key `Estimate`."""

    focus: Estimate14
    causal: Estimate14 | None
    overall: Estimate


def _gate14(estimate: Estimate, exact: bool) -> Estimate14:
    """A family-13 gate estimate as family 14 publishes it: never `degenerate_interval`."""

    if estimate.unavailable == INSUFFICIENT_REPEATS:
        return Estimate14(estimate.point, None, None, estimate.level, INSUFFICIENT_REPEATS, False)
    if estimate.unavailable not in (None, DEGENERATE_INTERVAL):
        raise ValueError("a gate estimate is available, degenerate or insufficient")
    return Estimate14(estimate.point, estimate.lower, estimate.upper, estimate.level, None, exact)


def interim_focus14(
    cases: Sequence[CaseEvidence],
    weights: Mapping[str, Fraction],
    *,
    confidence: PublishedValue,
    pairing: Pairing,
    seed: int,
    draws: int = DRAWS,
) -> Estimate14:
    """R22: family 13's interim focus interval, same draws, with `exact` instead of degeneracy."""

    estimate = interim_focus(
        cases, weights, confidence=confidence, pairing=pairing, seed=seed, draws=draws
    )
    if estimate.unavailable == INSUFFICIENT_REPEATS:
        return _gate14(estimate, False)
    return _gate14(estimate, exact_scope(cases, weights, pairing))


def final_statistical14(
    cases: Sequence[CaseEvidence],
    *,
    focus: Mapping[str, Fraction],
    causal: Mapping[str, Fraction] | None,
    overall: Mapping[str, Fraction],
    focus_confidence: PublishedValue,
    causal_confidence: PublishedValue,
    pairing: Pairing,
    seed: int,
    draws: int = DRAWS,
) -> LookEstimates14:
    """R22: family 13's final look from the same joint stream; focus and causal carry
    `exact`, decided per scope (one may be exact while the other is drawn), and the
    overall diagnostic keeps five keys and drops `degenerate_interval`."""

    built = final_statistical(
        cases,
        focus=focus,
        causal=causal,
        overall=overall,
        focus_confidence=focus_confidence,
        causal_confidence=causal_confidence,
        pairing=pairing,
        seed=seed,
        draws=draws,
    )

    def gate(estimate: Estimate, weights: Mapping[str, Fraction]) -> Estimate14:
        if estimate.unavailable == INSUFFICIENT_REPEATS:
            return _gate14(estimate, False)
        return _gate14(estimate, exact_scope(cases, weights, pairing))

    overall_estimate = built.overall
    if overall_estimate.unavailable == DEGENERATE_INTERVAL:
        overall_estimate = Estimate(
            overall_estimate.point,
            overall_estimate.lower,
            overall_estimate.upper,
            overall_estimate.level,
            None,
        )
    return LookEstimates14(
        gate(built.focus, focus),
        None if built.causal is None or causal is None else gate(built.causal, causal),
        overall_estimate,
    )


def observe_points14(
    cases: Sequence[CaseEvidence],
    *,
    focus: Mapping[str, Fraction],
    causal: Mapping[str, Fraction] | None,
    overall: Mapping[str, Fraction],
) -> LookEstimates14:
    """R22: observe's points; `exact` is null because no bound is requested."""

    built = observe_points(cases, focus=focus, causal=causal, overall=overall)

    def point(estimate: Estimate) -> Estimate14:
        return Estimate14(estimate.point, None, None, None, estimate.unavailable, None)

    return LookEstimates14(
        point(built.focus),
        None if built.causal is None else point(built.causal),
        built.overall,
    )


_CRITICAL = Decision("rejected", "critical_failure")

#: R24, M6: every family-14 Attempt decision and its admitted reasons. Family 13's
#: `ATTEMPT_DECISIONS` is not widened; `decide_binding`'s closes are in both.
ATTEMPT_DECISIONS14: Mapping[str, frozenset[str | None]] = MappingProxyType(
    {
        "continue": frozenset({None, "interval_unavailable"}),
        "rejected": frozenset({FOCUS_BELOW_MARGIN, "causal_below_tolerance", "critical_failure"}),
        "finalist": frozenset({"gate_passed", "observe_finalist"}),
        "insufficient_evidence": frozenset(
            {
                INSUFFICIENT_REPEATS,
                "insufficient_calibration",
                "no_evaluable_focus",
                "no_evaluable_pairs",
            }
        ),
    }
)


def _margin(margin: PublishedValue) -> Fraction:
    """`gate.focus.margin` as bound: any finite published number (negative admitted, D8)."""

    return published_fraction(margin)


def _tolerance(tolerance: PublishedValue | None) -> Fraction | None:
    if tolerance is None:
        return None
    value = published_fraction(tolerance)
    if value < 0:
        raise ValueError("a tolerance is at least 0")
    return value


def _decided(estimate: Estimate14, bound: PublishedValue | None) -> Fraction:
    """The value a family-14 look compares: the point when exact, else the bound (§6.2)."""

    value = estimate.point if estimate.exact else bound
    if value is None:
        raise ValueError("an available estimate has the bound its look reads")
    return published_fraction(value)


def decide_interim14(focus: Estimate14, *, margin: PublishedValue, critical: bool) -> Decision:
    """M6: reject when `U_F <= m`, or `d_F <= m` when exact; an unavailable interval continues."""

    m = _margin(margin)
    if critical:
        return _CRITICAL
    if focus.unavailable is not None:
        return Decision("continue", "interval_unavailable")
    if focus.lower is None or focus.upper is None:
        raise ValueError("an available interim estimate has both bounds")
    if _decided(focus, focus.upper) <= m:
        return Decision("rejected", FOCUS_BELOW_MARGIN)
    return Decision("continue", None)


def decide_final_statistical14(
    focus: Estimate14,
    causal: Estimate14 | None,
    *,
    margin: PublishedValue,
    tolerance: PublishedValue | None,
    critical: bool,
) -> Decision:
    """§6.1 to §6.3: critical, `insufficient_repeats`, then `L_F > m` (or `d_F > m` when
    exact), then `L_C >= -tolerance` (or `d_C`); no degenerate branch (R24)."""

    m = _margin(margin)
    if critical:
        return _CRITICAL
    required = [focus] if causal is None else [focus, causal]
    if any(estimate.unavailable == INSUFFICIENT_REPEATS for estimate in required):
        return Decision("insufficient_evidence", INSUFFICIENT_REPEATS)
    if any(estimate.unavailable is not None for estimate in required):
        raise ValueError("a final statistical estimate is available or insufficient")
    if causal is not None and tolerance is None:
        raise ValueError("a non-empty causal scope needs its tolerance")
    allowance = _tolerance(tolerance)
    if _decided(focus, focus.lower) <= m:
        return Decision("rejected", FOCUS_BELOW_MARGIN)
    if causal is not None:
        assert allowance is not None
        if _decided(causal, causal.lower) < -allowance:
            return Decision("rejected", "causal_below_tolerance")
    return Decision("finalist", "gate_passed")


def would_pass_point14(
    focus: PublishedValue,
    causal: PublishedValue | None,
    tolerance: PublishedValue | None,
    *,
    margin: PublishedValue,
) -> bool | None:
    """§6.1: `d_F > m` and, with C, `d_C >= -tolerance`; unavailable without a tolerance."""

    m = _margin(margin)
    allowance = _tolerance(tolerance)
    if causal is None:
        return published_fraction(focus) > m
    if allowance is None:
        return None
    return published_fraction(focus) > m and published_fraction(causal) >= -allowance


def decide_final_observe14(*, critical: bool) -> Decision:
    """Observe enforces no number; family 14 names a spared Attempt `finalist` (D10)."""

    return _CRITICAL if critical else Decision("finalist", "observe_finalist")


def decide_calibration14(
    mode: str, *, degenerate: bool, unavailable: str | None
) -> CalibrationVerdict:
    """R25, R26: no Parent variation is tolerance 0 with `noise_not_observed` in both modes;
    an unavailable calibration still closes a statistical Attempt `insufficient_calibration`."""

    if unavailable not in (None, INSUFFICIENT_CALIBRATION_REPEATS):
        raise ValueError("unknown calibration unavailability")
    if mode == "statistical":
        if unavailable is not None:
            return CalibrationVerdict(
                Decision("insufficient_evidence", "insufficient_calibration"), None
            )
        return CalibrationVerdict(None, NOISE_NOT_OBSERVED if degenerate else None)
    if mode == "observe":
        if unavailable is not None:
            return CalibrationVerdict(None, NOISE_ALLOWANCE_UNAVAILABLE)
        return CalibrationVerdict(None, NOISE_NOT_OBSERVED if degenerate else None)
    raise ValueError("mode must be 'statistical' or 'observe'")


def measured_tolerance14(
    allowance: PublishedValue | None, *, degenerate: bool, unavailable: str | None
) -> PublishedValue | None:
    """R25: the tolerance a family-14 look uses from a completed calibration.

    Unavailable: none. Degenerate: exactly 0, whatever the allowance (on the
    measure-zero set of varying rows whose draws coincide, 0 is the conservative
    choice). Otherwise the published allowance.
    """

    if unavailable is not None:
        if allowance is not None:
            raise ValueError("an unavailable calibration has no allowance")
        return None
    if allowance is None:
        raise ValueError("a completed calibration has an allowance")
    _tolerance(allowance)
    return publish(Fraction(0)) if degenerate else allowance


def _repeat(sample: ScoreSample) -> int:
    if not sample.sample_id.isdecimal():
        raise ValueError("a final-contrast sample id is its repeat number")
    return int(sample.sample_id)


def _final_side(case_id: str, samples: Sequence[ScoreSample]) -> tuple[ScoreSample, ...]:
    if len({sample.sample_id for sample in samples}) != len(samples):
        raise ValueError("a side names each sample once")
    for sample in samples:
        _validate_sample(sample)
        _repeat(sample)
        if sample.case_id != case_id:
            raise ValueError("an observation belongs to its case")
    return _ordered(samples)


@dataclass(frozen=True)
class FinalCase:
    """One corpus case's scored observations on both sides of a final contrast.

    `finalist` is the finalist's final-measurement side, `champion` the
    Champion side (the stored measurement under `stable`, the fresh member
    under `unstable`). Unlike `CaseEvidence`, a side may be empty: an
    unobserved case leaves the contrast, it is not an invariant failure (R28).
    Sample ids are repeat numbers, as `campaign_statistics.score_samples_of`
    writes them, so the stored exclusions can be matched by `(case_id, repeat)`.
    """

    case_id: str
    finalist: tuple[ScoreSample, ...]
    champion: tuple[ScoreSample, ...]

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("a case needs an id")
        object.__setattr__(self, "finalist", _final_side(self.case_id, self.finalist))
        object.__setattr__(self, "champion", _final_side(self.case_id, self.champion))


def renormalized_weights(
    weights: Mapping[str, Fraction], kept: Collection[str]
) -> dict[str, Fraction]:
    """M2: the fixed weights of the `kept` cases, rescaled to sum to 1, as family 13's
    amendment E1 rescales the observed cases' weights (`confidence_gate.renormalized`)."""

    if not kept or not set(kept) <= set(weights):
        raise ValueError("the kept cases are weighed and not empty")
    chosen = {case_id: weights[case_id] for case_id in weights if case_id in kept}
    if any(type(weight) is not Fraction or weight <= 0 for weight in chosen.values()):
        raise ValueError("a weight is a positive exact fraction")
    total = sum(chosen.values(), Fraction(0))
    return {case_id: weight / total for case_id, weight in chosen.items()}


@dataclass(frozen=True)
class FinalContrast:
    """R28: one finalist minus the Champion over the full corpus, after the stored exclusions.

    `point_cases` hold at least one usable observation on each side and carry
    the point; `bound_cases` (a subset) hold at least two and carry the bound;
    `removed_cases` are the corpus cases outside the point set; `removed_pairs`
    are the exclusions applied, `(case_id, repeat)` in corpus then repeat order.
    All case lists keep corpus order.
    """

    estimate: Estimate14
    point_cases: tuple[str, ...]
    bound_cases: tuple[str, ...]
    removed_cases: tuple[str, ...]
    removed_pairs: tuple[tuple[str, int], ...]


def _mask(
    cases: Sequence[FinalCase], excluded: Collection[tuple[str, int]]
) -> tuple[dict[str, int], frozenset[tuple[str, int]]]:
    position = {case.case_id: index for index, case in enumerate(cases)}
    if not cases or len(position) != len(cases):
        raise ValueError("cases are not empty and each is named once")
    for case_id, repeat in excluded:
        if case_id not in position or type(repeat) is not int or repeat < 1:
            raise ValueError("an excluded pair names a corpus case and a positive repeat")
    return position, frozenset(excluded)


def _point_and_bound(
    cases: Sequence[FinalCase], mask: frozenset[tuple[str, int]]
) -> tuple[list[CaseEvidence], list[CaseEvidence], list[str]]:
    """M2: the point set (one usable observation per side), the bound set (two) and the rest."""

    point: list[CaseEvidence] = []
    bound: list[CaseEvidence] = []
    removed: list[str] = []
    for case in cases:
        sides = [
            tuple(sample for sample in side if (case.case_id, _repeat(sample)) not in mask)
            for side in (case.finalist, case.champion)
        ]
        if not sides[0] or not sides[1]:
            removed.append(case.case_id)
            continue
        evidence = CaseEvidence(case.case_id, candidate=sides[0], parent=sides[1])
        point.append(evidence)
        if len(sides[0]) >= 2 and len(sides[1]) >= 2:
            bound.append(evidence)
    return point, bound, removed


def final_pairing(cases: Sequence[FinalCase], *, excluded: Collection[tuple[str, int]]) -> Pairing:
    """R28: the contrast's pairing, `exact` only when the provenance proof holds for every
    pair left in the point set after the stored exclusions (`pairing_of`)."""

    _position, mask = _mask(cases, excluded)
    point, _bound, _removed = _point_and_bound(cases, mask)
    return pairing_of(point)


def final_contrast(
    cases: Sequence[FinalCase],
    weights: Mapping[str, Fraction],
    *,
    excluded: Collection[tuple[str, int]],
    level: PublishedValue | None,
    pairing: Pairing,
    seed: int,
    draws: int = DRAWS,
) -> FinalContrast:
    """R28 with M2: the global contrast of family 14, either Champion side.

    `cases` are every corpus case in corpus order; `weights` are the corpus's
    fixed weights (`scope_weights(corpus, groups)`, as family 13's cohort
    contrast weighs). `excluded` are the stored measurement's `pairs_excluded`,
    keyed `(case_id, repeat)` (ruling R13); they leave both sides. Empty under
    `unstable`. `level` is the finalist's level, or None in observe; `pairing`
    is `final_pairing(cases, excluded=excluded)`.

    The point is the contrast over the point set and the bound the one-sided
    lower bound over the bound set, each with the fixed weights renormalized over
    its own set (M2). The bound reuses family 13's `cohort_contrast`, so with no
    exclusion and every case observed twice it is family 13's number exactly.
    An empty point set is `final_incomplete`; an empty bound set under a level
    publishes `lower: null` with `insufficient_repeats`. `exact` holds only when
    the bound set equals the point set and `exact_scope` holds over it.
    """

    position, mask = _mask(cases, excluded)
    if set(weights) != set(position):
        raise ValueError("a final contrast weighs every corpus case")
    if level is not None:
        confidence_fraction(level)
    point_evidence, bound_evidence, removed = _point_and_bound(cases, mask)
    point_cases = tuple(case.case_id for case in point_evidence)
    bound_cases = tuple(case.case_id for case in bound_evidence)
    removed_pairs = tuple(sorted(mask, key=lambda pair: (position[pair[0]], pair[1])))

    def done(estimate: Estimate14) -> FinalContrast:
        return FinalContrast(estimate, point_cases, bound_cases, tuple(removed), removed_pairs)

    if not point_evidence:
        return done(
            Estimate14(None, None, None, level, FINAL_INCOMPLETE, None if level is None else False)
        )
    point = publish(point_contrast(point_evidence, renormalized_weights(weights, point_cases)))
    if level is None:
        return done(Estimate14(point, None, None, None, BOUND_NOT_REQUESTED_IN_OBSERVE, None))
    if not bound_evidence:
        return done(Estimate14(point, None, None, level, INSUFFICIENT_REPEATS, False))
    bound_weights = renormalized_weights(weights, bound_cases)
    bound = cohort_contrast(
        bound_evidence, bound_weights, level=level, pairing=pairing, seed=seed, draws=draws
    )
    exact = bound_cases == point_cases and exact_scope(bound_evidence, bound_weights, pairing)
    return done(Estimate14(point, bound.lower, None, level, None, exact))


#: D10, M2: `accepted` -> its admitted reasons. Family 13's `GLOBAL_REASONS` is not widened.
GLOBAL_REASONS14: Mapping[bool, frozenset[str]] = MappingProxyType(
    {
        True: frozenset({"global_off", "global_point_improved", "global_bound_positive"}),
        False: frozenset(
            {
                "critical_failure",
                FINAL_INCOMPLETE,
                "global_point_not_improved",
                "global_bound_not_positive",
            }
        ),
    }
)


@dataclass(frozen=True)
class GlobalVerdict14:
    """`global_comparison_completed.accepted` and its reason (D10: family 13 says `eligible`)."""

    accepted: bool
    reason: str


def decide_global14(
    mode: str,
    *,
    complete: bool,
    critical: bool,
    point: PublishedValue | None,
    lower: PublishedValue | None,
) -> GlobalVerdict14:
    """M2 on §6.10: `final_incomplete` first (an incomplete finalist, an empty point set, or
    under `bound` an empty bound set), then the critical guard, then the strict mode rule."""

    if mode not in ("off", "point", "bound"):
        raise ValueError("global is 'off', 'point' or 'bound'")
    if not complete or point is None or (mode == "bound" and lower is None):
        return GlobalVerdict14(False, FINAL_INCOMPLETE)
    if critical:
        return GlobalVerdict14(False, "critical_failure")
    if mode == "off":
        return GlobalVerdict14(True, "global_off")
    if mode == "point":
        improved = published_fraction(point) > 0
        return GlobalVerdict14(
            improved, "global_point_improved" if improved else "global_point_not_improved"
        )
    assert lower is not None
    positive = published_fraction(lower) > 0
    return GlobalVerdict14(
        positive, "global_bound_positive" if positive else "global_bound_not_positive"
    )
