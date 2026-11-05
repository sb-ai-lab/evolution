"""Family 13's statistics kernel: fixed-question resampling and decisions.

Implementation design §4 ("Seams chosen for the gate"), §6.1 (conventions),
§6.4 to §6.6 and §6.10; ADR-0027 ("Fixed-question resampling", "Concrete
empirical method"). The module is pure: it imports no campaign, schema,
manifest or family module, keeps every draw, quantile and bound an exact
`Fraction`, and takes every random index from `statistics._draw`. Records
publish `publish(value)`; decisions read `published_fraction(published)`
(design G4), so the executor, the lifecycle and the audit compare the same
numbers.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Literal

from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.statistics import (
    ScoreSample,
    _draw,
    _sample_order,
    _scaled,
    _validate_exact_proof,
    _validate_sample,
)

#: Design §6.2 `method`: versioned constants, never launch knobs (ADR-0027).
METHOD_VERSION = "f13-bootstrap/1"
DRAWS = 20_000
QUANTILE_RULE = "linear"
NOISE_LEVEL = Fraction(995, 1000)
#: Frozen design §6.4: the rejection-only stages strictly below R.
INTERIM_STAGES: tuple[int, ...] = (3, 5, 10)

PublishedValue = int | float


def method_object() -> dict[str, Any]:
    """`comparison_bound.method` and `noise_calibration_bound.method` (design §6.2)."""

    return {
        "version": METHOD_VERSION,
        "draws": DRAWS,
        "quantile": QUANTILE_RULE,
        "noise_level": publish(NOISE_LEVEL),
    }


def confidence_looks(mode: str, repeats: int) -> tuple[int, ...]:
    """Design G5: statistical looks are the stages of {3, 5, 10} below R, then R; observe has R."""

    if type(repeats) is not int:
        raise ValueError("repeats must be an integer")
    if mode == "statistical":
        if repeats < 2:
            raise ValueError("statistical mode needs at least two repeats")
        return (*(stage for stage in INTERIM_STAGES if stage < repeats), repeats)
    if mode == "observe":
        if repeats < 1:
            raise ValueError("observe mode needs at least one repeat")
        return (repeats,)
    raise ValueError("mode must be 'statistical' or 'observe'")


def publish(value: Fraction) -> float:
    """The JSON number a record carries for an exact value (design G4)."""

    if type(value) is not Fraction:
        raise ValueError("only exact values are published")
    published = float(value)
    if not math.isfinite(published):
        raise ValueError("a published value is finite")
    return published


def published_fraction(value: PublishedValue) -> Fraction:
    """The exact value a decision reads from a published number: its shortest spelling."""

    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("a published value is a finite JSON number")
    return Fraction(str(value))


def confidence_fraction(value: PublishedValue) -> Fraction:
    """A published confidence level, strictly between 0 and 1."""

    level = published_fraction(value)
    if not 0 < level < 1:
        raise ValueError("a confidence is a fraction strictly between 0 and 1")
    return level


@dataclass(frozen=True)
class Draws:
    """One projection's replicate values, sorted: `numerators[i] / denominator`."""

    numerators: tuple[int, ...]
    denominator: int

    def __post_init__(self) -> None:
        if not self.numerators:
            raise ValueError("draws are not empty")
        if type(self.denominator) is not int or self.denominator < 1:
            raise ValueError("draws share one positive integer denominator")
        if any(type(n) is not int for n in self.numerators) or list(self.numerators) != sorted(
            self.numerators
        ):
            raise ValueError("draws are sorted integer numerators")

    @classmethod
    def of(cls, values: Sequence[Fraction]) -> Draws:
        if not values or any(type(value) is not Fraction for value in values):
            raise ValueError("draws are exact fractions")
        denominator = math.lcm(*(value.denominator for value in values))
        return cls(
            tuple(sorted(v.numerator * (denominator // v.denominator) for v in values)),
            denominator,
        )

    @property
    def degenerate(self) -> bool:
        """Design G8: every draw equal, compared exactly."""

        return self.numerators[0] == self.numerators[-1]

    def published(self) -> list[float]:
        return [publish(Fraction(n, self.denominator)) for n in self.numerators]


def linear_quantile(draws: Draws, q: Fraction) -> Fraction:
    """Frozen design §5.3: `h = (B-1) q`, `j = floor(h)`, `x[j] + (h-j)(x[min(j+1,B-1)] - x[j])`."""

    if type(q) is not Fraction or not 0 <= q <= 1:
        raise ValueError("a quantile level is an exact fraction in [0, 1]")
    count = len(draws.numerators)
    h = (count - 1) * q
    j = math.floor(h)
    low = draws.numerators[j]
    high = draws.numerators[min(j + 1, count - 1)]
    return (low + (h - j) * (high - low)) / draws.denominator


def _level(confidence: Fraction) -> Fraction:
    if type(confidence) is not Fraction or not 0 < confidence < 1:
        raise ValueError("a confidence is an exact fraction strictly between 0 and 1")
    return confidence


def one_sided_lower(draws: Draws, confidence: Fraction) -> Fraction:
    """The one-sided lower bound at `confidence`: quantile `1 - c`, not `(1 - c) / 2`."""

    return linear_quantile(draws, 1 - _level(confidence))


def two_sided(draws: Draws, confidence: Fraction) -> tuple[Fraction, Fraction]:
    """The equal-tailed interval at `confidence` (frozen design §6.4)."""

    tail = (1 - _level(confidence)) / 2
    return linear_quantile(draws, tail), linear_quantile(draws, 1 - tail)


#: Design §6.1: each scope draws for exactly one purpose.
_PURPOSE_OF_SCOPE: Mapping[str, str] = MappingProxyType(
    {"focus": "gate", "causal": "gate", "joint": "gate", "noise": "noise", "global": "global"}
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def binding_digest(payload: Mapping[str, Any]) -> str:
    """Design §6.1: SHA-256 of the canonical JSON of a binding payload without `evidence`."""

    return hashlib.sha256(
        canonical_json({key: value for key, value in payload.items() if key != "evidence"})
    ).hexdigest()


def derive_seed(
    *,
    campaign_seed: int,
    binding: str,
    scope: str,
    prefixes: Sequence[str],
    look: int | None,
    purpose: str,
) -> int:
    """Design §6.1: the first 128 bits, big-endian, of the SHA-256 of the canonical JSON
    `{method, campaign_seed, binding, scope, prefixes, look, purpose}`.

    `prefixes` keep the caller's order, which is the record's: `[candidate, parent]`
    for a look, `[source]` for noise, `[finalist, champion]` for a cohort contrast.
    `look` is the look for `purpose: gate` and null otherwise. A confidence level
    never enters the seed (design §6.10).
    """

    if type(campaign_seed) is not int:
        raise ValueError("campaign_seed is an integer")
    if type(binding) is not str or _SHA256_HEX.fullmatch(binding) is None:
        raise ValueError("binding is a lower-case SHA-256 hex digest")
    if _PURPOSE_OF_SCOPE.get(scope) != purpose:
        raise ValueError("scope and purpose disagree")
    if (
        isinstance(prefixes, str)
        or not prefixes
        or any(type(prefix) is not str or not prefix for prefix in prefixes)
    ):
        raise ValueError("prefixes are non-empty journal locators")
    if purpose == "gate":
        if type(look) is not int or look < 1:
            raise ValueError("a gate seed names its look")
    elif look is not None:
        raise ValueError("only a gate seed names a look")
    payload = {
        "method": METHOD_VERSION,
        "campaign_seed": campaign_seed,
        "binding": binding,
        "scope": scope,
        "prefixes": list(prefixes),
        "look": look,
        "purpose": purpose,
    }
    return int.from_bytes(hashlib.sha256(canonical_json(payload)).digest()[:16], "big")


Pairing = Literal["exact", "unpaired"]


def _ordered(samples: Sequence[ScoreSample]) -> tuple[ScoreSample, ...]:
    return tuple(
        sorted(samples, key=lambda sample: (_sample_order(sample.sample_id), sample.sample_id))
    )


def _checked_side(case_id: str, samples: Sequence[ScoreSample]) -> tuple[ScoreSample, ...]:
    if not samples:
        raise ValueError("each side needs an observation")
    if len({sample.sample_id for sample in samples}) != len(samples):
        raise ValueError("a side names each sample once")
    for sample in samples:
        _validate_sample(sample)
        if sample.case_id != case_id:
            raise ValueError("an observation belongs to its case")
    return _ordered(samples)


@dataclass(frozen=True)
class CaseEvidence:
    """One retained case's usable observations on both sides of a contrast.

    `candidate` and `parent` are the Candidate's and the Parent's scored
    observations after the comparison's mask -- for a cohort contrast the
    finalist's and the Champion's. Input order does not matter: the kernel
    keeps them in repeat order (`statistics._sample_order`).
    """

    case_id: str
    candidate: tuple[ScoreSample, ...]
    parent: tuple[ScoreSample, ...]

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("a case needs an id")
        object.__setattr__(self, "candidate", _checked_side(self.case_id, self.candidate))
        object.__setattr__(self, "parent", _checked_side(self.case_id, self.parent))


def _values(samples: Sequence[ScoreSample]) -> tuple[Fraction, ...]:
    return tuple(Fraction(sample.score) for sample in samples)


def _mean(values: Sequence[Fraction]) -> Fraction:
    return sum(values, Fraction(0)) / len(values)


def _members(
    case_ids: Sequence[str], groups: Mapping[str, tuple[str, str]]
) -> dict[str, list[str]]:
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValueError("a scope names each case once and is not empty")
    members: dict[str, list[str]] = {}
    for case_id in case_ids:
        if case_id not in groups:
            raise ValueError("a scope names only corpus cases")
        members.setdefault(groups[case_id][0], []).append(case_id)
    return members


def scope_weights(
    case_ids: Sequence[str], groups: Mapping[str, tuple[str, str]]
) -> dict[str, Fraction]:
    """The existing aggregation over a scope: groups equal, cases equal within a group.

    Computed once over the retained cases (design G6); a case's weight does not
    depend on its repeat count (frozen design §5.3).
    """

    members = _members(case_ids, groups)
    return {
        case_id: Fraction(1, len(members) * len(cases))
        for cases in members.values()
        for case_id in cases
    }


def corpus_weights(
    case_ids: Sequence[str], groups: Mapping[str, tuple[str, str]], corpus: Sequence[str]
) -> dict[str, Fraction]:
    """Design G9: the full corpus's weights restricted to `case_ids`; U adds nothing."""

    sizes = {group: len(cases) for group, cases in _members(corpus, groups).items()}
    _members(case_ids, groups)
    if not set(case_ids) <= set(corpus):
        raise ValueError("a scope names only corpus cases")
    return {case_id: Fraction(1, len(sizes) * sizes[groups[case_id][0]]) for case_id in case_ids}


def _check_case_ids(ids: Sequence[str]) -> None:
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("cases are not empty and each is named once")


def _case_map(cases: Sequence[CaseEvidence]) -> dict[str, CaseEvidence]:
    _check_case_ids([case.case_id for case in cases])
    return {case.case_id: case for case in cases}


def _check_weights(case_ids: Collection[str], weights: Mapping[str, Fraction]) -> None:
    if not weights:
        raise ValueError("a projection weighs at least one case")
    for case_id, weight in weights.items():
        if case_id not in case_ids:
            raise ValueError("a projection weighs only the given cases")
        if type(weight) is not Fraction or weight <= 0:
            raise ValueError("a weight is a positive exact fraction")


def point_contrast(cases: Sequence[CaseEvidence], weights: Mapping[str, Fraction]) -> Fraction:
    """`sum w_i (mean candidate_i - mean parent_i)`: the scope's point difference."""

    by_id = _case_map(cases)
    _check_weights(by_id, weights)
    return sum(
        (
            weight
            * (_mean(_values(by_id[case_id].candidate)) - _mean(_values(by_id[case_id].parent)))
            for case_id, weight in weights.items()
        ),
        Fraction(0),
    )


def _proves(case: CaseEvidence) -> bool:
    candidate = {sample.sample_id: sample for sample in case.candidate}
    if set(candidate) != {sample.sample_id for sample in case.parent}:
        return False
    try:
        _validate_exact_proof(case.parent, candidate)
    except ValueError:
        return False
    return True


def pairing_of(cases: Sequence[CaseEvidence]) -> Pairing:
    """`exact` when the existing provenance proof holds for every pair, else `unpaired`."""

    return "exact" if cases and all(_proves(case) for case in cases) else "unpaired"


@dataclass(frozen=True)
class Replicates:
    """Every projection of one joint stream, in draw order (design §6.1)."""

    names: tuple[str, ...]
    numerators: tuple[tuple[int, ...], ...]
    denominators: tuple[int, ...]

    def values(self, name: str) -> tuple[Fraction, ...]:
        index = self.names.index(name)
        return tuple(Fraction(n, self.denominators[index]) for n in self.numerators[index])

    def draws(self, name: str) -> Draws:
        index = self.names.index(name)
        return Draws(tuple(sorted(self.numerators[index])), self.denominators[index])


@dataclass(frozen=True)
class _Part:
    """One resampling unit: draw `count` indices into `pool`, weighted per projection."""

    pool: tuple[int, ...]
    count: int
    coefficients: tuple[Fraction, ...]


def _resample(
    parts: Sequence[_Part], width: int, exponent: int, *, seed: int, draws: int
) -> tuple[tuple[tuple[int, ...], ...], tuple[int, ...]]:
    """Integer replicate numerators per projection and their denominators.

    A projection's value is `sum coefficient * (sum of drawn scaled values) / 2**exponent`;
    each coefficient is scaled to an integer over one common denominator, so the
    inner loop is integer arithmetic and the result is exact.
    """

    if type(seed) is not int:
        raise ValueError("a seed is an integer")
    if type(draws) is not int or draws < 1:
        raise ValueError("draws is a positive integer")
    denominators = [
        math.lcm(*(part.coefficients[k].denominator for part in parts)) for k in range(width)
    ]
    integer = [
        tuple(int(part.coefficients[k] * denominators[k]) for k in range(width)) for part in parts
    ]
    columns: list[list[int]] = [[] for _ in range(width)]
    stream = random.Random(seed)
    for _ in range(draws):
        totals = [0] * width
        for part, coefficients in zip(parts, integer, strict=True):
            size = len(part.pool)
            drawn = 0
            for _ in range(part.count):
                drawn += part.pool[_draw(stream, size)]
            for k, coefficient in enumerate(coefficients):
                if coefficient:
                    totals[k] += coefficient * drawn
        for k in range(width):
            columns[k].append(totals[k])
    return (
        tuple(tuple(column) for column in columns),
        tuple(denominator << exponent for denominator in denominators),
    )


def joint_replicates(
    cases: Sequence[CaseEvidence],
    projections: Mapping[str, Mapping[str, Fraction]],
    *,
    pairing: Pairing,
    seed: int,
    draws: int = DRAWS,
) -> Replicates:
    """One stream; every projection reads the same resampled cases (design §6.1).

    Draw order (plan R4, method `f13-bootstrap/1`): per replicate, cases in the
    given order (callers pass corpus order); `unpaired` draws the Candidate's
    `n_c` indices then the Parent's `n_p`, each side from its own usable
    observations; `exact` draws `n` indices into the within-case differences
    after the provenance proof. Case and group ids are never resampled; a
    case's weight is fixed by `projections`.
    """

    by_id = _case_map(cases)
    if not projections:
        raise ValueError("at least one projection")
    for weights in projections.values():
        _check_weights(by_id, weights)
    if pairing not in ("exact", "unpaired"):
        raise ValueError("pairing is 'exact' or 'unpaired'")
    scaled, exponent = _scaled(
        {
            "candidate": {case.case_id: _values(case.candidate) for case in cases},
            "parent": {case.case_id: _values(case.parent) for case in cases},
        }
    )
    names = tuple(projections)
    parts: list[_Part] = []
    for case in cases:
        case_weights = [Fraction(projections[name].get(case.case_id, 0)) for name in names]
        candidate = tuple(scaled["candidate"][case.case_id])
        parent = tuple(scaled["parent"][case.case_id])
        if pairing == "exact":
            if not _proves(case):
                raise ValueError("exact pairing requires the provenance proof")
            # Pair by sample id; the sorted order only fixes the draw order.
            parent_by_id = {
                sample.sample_id: value for sample, value in zip(case.parent, parent, strict=True)
            }
            pool = tuple(
                value - parent_by_id[sample.sample_id]
                for sample, value in zip(case.candidate, candidate, strict=True)
            )
            parts.append(_Part(pool, len(pool), tuple(w / len(pool) for w in case_weights)))
        else:
            parts.append(
                _Part(candidate, len(candidate), tuple(w / len(candidate) for w in case_weights))
            )
            parts.append(_Part(parent, len(parent), tuple(-w / len(parent) for w in case_weights)))
    numerators, denominators = _resample(parts, len(names), exponent, seed=seed, draws=draws)
    return Replicates(names, numerators, denominators)


INSUFFICIENT_CALIBRATION_REPEATS = "insufficient_calibration_repeats"


@dataclass(frozen=True)
class NoiseCase:
    """One retained causal case's Parent observations and the two virtual sizes.

    `first` and `second` are design §6.3's `sizes` for this case: the
    Candidate's and the Parent's planned repeats on C, each reduced to the
    retained count on its side by the comparison's mask; 1 and 1 only on the
    observe R=1 path. Candidate values never enter (frozen design §5.1).
    """

    case_id: str
    parent: tuple[ScoreSample, ...]
    first: int
    second: int

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("a case needs an id")
        for size in (self.first, self.second):
            if type(size) is not int or size < 1:
                raise ValueError("a virtual measurement has at least one observation per case")
        object.__setattr__(self, "parent", _checked_side(self.case_id, self.parent))


@dataclass(frozen=True)
class NoiseResult:
    """Design §6.4: `allowance` or null, `degenerate`, `unavailable`, and D0 for the digest."""

    allowance: Fraction | None
    degenerate: bool
    unavailable: str | None
    draws: Draws | None


def noise_allowance(draws: Draws) -> Fraction:
    """`max(0, -q_0.005(D0))` at the fixed noise level (frozen design §5.1)."""

    return max(Fraction(0), -linear_quantile(draws, 1 - NOISE_LEVEL))


def noise_calibration(
    cases: Sequence[NoiseCase],
    weights: Mapping[str, Fraction],
    *,
    seed: int,
    draws: int = DRAWS,
) -> NoiseResult:
    """D0 = Q(first virtual Parent) - Q(second virtual Parent) over the retained C cases.

    Draw order (plan R4): per replicate, cases in the given order; the first
    virtual measurement's `first` indices, then the second's `second`, both
    into the case's own usable Parent observations. A case with fewer than two
    observations makes the estimate unavailable; it is never imputed.

    `weights` are the causal projection's `scope_weights` over the retained C
    cases (not `corpus_weights`): D0 is compared with L_C, which uses them.
    """

    ids = [case.case_id for case in cases]
    _check_case_ids(ids)
    if set(weights) != set(ids):
        raise ValueError("the weights cover exactly the calibrated cases")
    _check_weights(set(ids), weights)
    if any(len(case.parent) < 2 for case in cases):
        return NoiseResult(None, False, INSUFFICIENT_CALIBRATION_REPEATS, None)
    scaled, exponent = _scaled({"parent": {case.case_id: _values(case.parent) for case in cases}})
    parts: list[_Part] = []
    for case in cases:
        pool = tuple(scaled["parent"][case.case_id])
        weight = weights[case.case_id]
        parts.append(_Part(pool, case.first, (weight / case.first,)))
        parts.append(_Part(pool, case.second, (-weight / case.second,)))
    numerators, denominators = _resample(parts, 1, exponent, seed=seed, draws=draws)
    d0 = Draws(tuple(sorted(numerators[0])), denominators[0])
    return NoiseResult(noise_allowance(d0), d0.degenerate, None, d0)


def noise_result_digest(draws: Draws) -> str:
    """Design §6.4 `result_digest` (plan R5): SHA-256 of the canonical JSON list of the
    published draws in ascending exact order, as plain lower-case hex."""

    return hashlib.sha256(canonical_json(draws.published())).hexdigest()


INSUFFICIENT_REPEATS = "insufficient_repeats"
DEGENERATE_INTERVAL = "degenerate_interval"
NOT_REQUESTED_IN_OBSERVE = "not_requested_in_observe_mode"
BOUND_NOT_REQUESTED_IN_OBSERVE = "bound_not_requested_in_observe_mode"


@dataclass(frozen=True)
class Estimate:
    """Design §6.5's `{point, lower, upper, level, unavailable}` object, published.

    Plan R3: a gate estimate with fewer than two usable observations on any
    side of any case has null bounds and `insufficient_repeats`; a zero-width
    one keeps its bounds and says `degenerate_interval`. A cohort contrast
    (`cohort_contrast`) never marks degeneracy (design R2).

    `point` is null only under E1 (a): no retained case of the scope is
    observed (maintainer ruling 2026-10-04; design §6.5 Amendments).
    """

    point: float | None
    lower: float | None
    upper: float | None
    level: PublishedValue | None
    unavailable: str | None

    def record(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "lower": self.lower,
            "upper": self.upper,
            "level": self.level,
            "unavailable": self.unavailable,
        }


@dataclass(frozen=True)
class LookEstimates:
    focus: Estimate
    causal: Estimate | None
    overall: Estimate


def _too_few(cases: Sequence[CaseEvidence], only: Collection[str] | None = None) -> bool:
    """Frozen design §5.4: a bound needs two usable observations per retained case and side.

    `only` restricts the check to those case ids (a scope's keys).
    """

    return any(
        len(case.candidate) < 2 or len(case.parent) < 2
        for case in cases
        if only is None or case.case_id in only
    )


def global_level(
    focus_confidence: PublishedValue,
    causal_confidence: PublishedValue | None,
    *,
    causal_empty: bool,
) -> PublishedValue:
    """`max(focus, causal)` confidence, or the focus confidence when C is empty (ADR-0027)."""

    focus = confidence_fraction(focus_confidence)
    if causal_empty:
        return focus_confidence
    if causal_confidence is None:
        raise ValueError("a non-empty causal scope has a confidence")
    return causal_confidence if confidence_fraction(causal_confidence) > focus else focus_confidence


def interim_focus(
    cases: Sequence[CaseEvidence],
    weights: Mapping[str, Fraction],
    *,
    confidence: PublishedValue,
    pairing: Pairing,
    seed: int,
    draws: int = DRAWS,
) -> Estimate:
    """An interim look's two-sided focus interval at the focus confidence (`scope: focus`)."""

    level = confidence_fraction(confidence)
    if set(weights) != set(_case_map(cases)):
        raise ValueError("an interim look weighs exactly the focus cases")
    point = publish(point_contrast(cases, weights))
    if _too_few(cases):
        return Estimate(point, None, None, confidence, INSUFFICIENT_REPEATS)
    focus = joint_replicates(
        cases, {"focus": weights}, pairing=pairing, seed=seed, draws=draws
    ).draws("focus")
    lower, upper = two_sided(focus, level)
    return Estimate(
        point,
        publish(lower),
        publish(upper),
        confidence,
        DEGENERATE_INTERVAL if focus.degenerate else None,
    )


def _check_scopes(
    cases: Sequence[CaseEvidence],
    focus: Mapping[str, Fraction],
    causal: Mapping[str, Fraction] | None,
    overall: Mapping[str, Fraction],
) -> None:
    ids = set(_case_map(cases))
    if causal is not None and not causal:
        raise ValueError("an empty causal scope is None")
    focus_ids, causal_ids = set(focus), set(causal or {})
    if (
        not focus_ids
        or focus_ids & causal_ids
        or focus_ids | causal_ids != ids
        or set(overall) != ids
    ):
        raise ValueError("focus and causal partition the cases and overall covers them")


def final_statistical(
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
) -> LookEstimates:
    """The final statistical look: L_F, L_C and the overall diagnostic from one joint stream.

    `cases` are F ∪ C in corpus order (design §6.1, `scope: joint`); `overall`
    carries the G9 weights (`corpus_weights`). Each bound is one-sided at its
    level; `upper` is null. Availability is decided per scope: an estimate is
    `insufficient_repeats` iff a case in its own weights has fewer than two
    observations on a side, while one joint stream serves the available scopes.
    """

    _check_scopes(cases, focus, causal, overall)
    confidence_fraction(causal_confidence)
    level = global_level(focus_confidence, causal_confidence, causal_empty=causal is None)
    scopes: dict[str, tuple[Mapping[str, Fraction], PublishedValue]] = {
        "focus": (focus, focus_confidence),
        "overall": (overall, level),
    }
    if causal is not None:
        scopes["causal"] = (causal, causal_confidence)
    points = {name: publish(point_contrast(cases, w)) for name, (w, _published) in scopes.items()}
    short = {name: _too_few(cases, w) for name, (w, _published) in scopes.items()}
    built: dict[str, Estimate] = {
        name: Estimate(points[name], None, None, published, INSUFFICIENT_REPEATS)
        for name, (_w, published) in scopes.items()
        if short[name]
    }
    available = {name: w for name, (w, _p) in scopes.items() if not short[name]}
    if available:
        replicates = joint_replicates(cases, available, pairing=pairing, seed=seed, draws=draws)
        for name in available:
            projection = replicates.draws(name)
            published = scopes[name][1]
            built[name] = Estimate(
                points[name],
                publish(one_sided_lower(projection, confidence_fraction(published))),
                None,
                published,
                DEGENERATE_INTERVAL if projection.degenerate else None,
            )
    return LookEstimates(built["focus"], built.get("causal"), built["overall"])


def observe_points(
    cases: Sequence[CaseEvidence],
    *,
    focus: Mapping[str, Fraction],
    causal: Mapping[str, Fraction] | None,
    overall: Mapping[str, Fraction],
) -> LookEstimates:
    """Observe's final look: point differences only (frozen design §5.5); no draw."""

    _check_scopes(cases, focus, causal, overall)

    def point(weights: Mapping[str, Fraction]) -> Estimate:
        return Estimate(
            publish(point_contrast(cases, weights)), None, None, None, NOT_REQUESTED_IN_OBSERVE
        )

    return LookEstimates(point(focus), None if causal is None else point(causal), point(overall))


def cohort_contrast(
    cases: Sequence[CaseEvidence],
    weights: Mapping[str, Fraction],
    *,
    level: PublishedValue | None,
    pairing: Pairing,
    seed: int,
    draws: int = DRAWS,
) -> Estimate:
    """A finalist minus the Champion over the full corpus (design §6.10, `scope: global`).

    `candidate` is the finalist's member, `parent` the Champion's; `weights` are
    `scope_weights(corpus, groups)`. `level` is the finalist's own level, or
    `None` in observe. A degenerate bound is decided on its value (design R2).
    """

    if set(weights) != set(_case_map(cases)):
        raise ValueError("a cohort contrast weighs every corpus case")
    point = publish(point_contrast(cases, weights))
    if level is None:
        return Estimate(point, None, None, None, BOUND_NOT_REQUESTED_IN_OBSERVE)
    confidence = confidence_fraction(level)
    if _too_few(cases):
        return Estimate(point, None, None, level, INSUFFICIENT_REPEATS)
    contrast = joint_replicates(
        cases, {"global": weights}, pairing=pairing, seed=seed, draws=draws
    ).draws("global")
    return Estimate(point, publish(one_sided_lower(contrast, confidence)), None, level, None)


OBSERVE_CALIBRATION_DEGENERATE = "observe_calibration_degenerate"
NOISE_ALLOWANCE_UNAVAILABLE = "noise_allowance_unavailable"

#: Design §6.6: every Attempt decision and its admitted reasons.
ATTEMPT_DECISIONS: Mapping[str, frozenset[str | None]] = MappingProxyType(
    {
        "continue": frozenset({None, "interval_unavailable"}),
        "rejected": frozenset(
            {
                "focus_upper_nonpositive",
                "focus_lower_not_positive",
                "causal_below_tolerance",
                "critical_failure",
            }
        ),
        "accepted": frozenset({"gate_passed", "observe_finalist"}),
        "insufficient_evidence": frozenset(
            {
                INSUFFICIENT_REPEATS,
                DEGENERATE_INTERVAL,
                "insufficient_calibration",
                "no_evaluable_focus",
                "no_evaluable_pairs",
            }
        ),
    }
)
#: Design §6.10: `eligible` -> its admitted reasons.
GLOBAL_REASONS: Mapping[bool, frozenset[str]] = MappingProxyType(
    {
        True: frozenset({"global_off", "global_point_improved", "global_bound_positive"}),
        False: frozenset(
            {
                "critical_failure",
                "cohort_incomplete",
                "global_point_not_improved",
                "global_bound_not_positive",
            }
        ),
    }
)


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str | None


@dataclass(frozen=True)
class CalibrationVerdict:
    close: Decision | None
    caveat: str | None


@dataclass(frozen=True)
class GlobalVerdict:
    eligible: bool
    reason: str


_CRITICAL = Decision("rejected", "critical_failure")


def decide_binding(
    *, focus_retained: int, causal_cases: int, causal_retained: int
) -> Decision | None:
    """G6, D3, R1: a wholly excluded F, or a non-empty C wholly excluded, closes at binding."""

    if focus_retained == 0:
        return Decision("insufficient_evidence", "no_evaluable_focus")
    if causal_cases > 0 and causal_retained == 0:
        return Decision("insufficient_evidence", "no_evaluable_pairs")
    return None


def decide_interim(focus: Estimate, *, critical: bool) -> Decision:
    """Frozen design §6.4: reject only when `U_F <= 0`; an unavailable interval continues."""

    if critical:
        return _CRITICAL
    if focus.unavailable is not None:
        return Decision("continue", "interval_unavailable")
    if focus.lower is None or focus.upper is None:
        raise ValueError("an available interim estimate has both bounds")
    if published_fraction(focus.upper) <= 0:
        return Decision("rejected", "focus_upper_nonpositive")
    return Decision("continue", None)


def decide_final_statistical(
    focus: Estimate, causal: Estimate | None, *, tolerance: PublishedValue | None, critical: bool
) -> Decision:
    """`L_F > 0` and (C empty or `L_C >= -tolerance`), on published values (plan R6)."""

    if critical:
        return _CRITICAL
    required = [focus] if causal is None else [focus, causal]
    if any(estimate.unavailable == INSUFFICIENT_REPEATS for estimate in required):
        return Decision("insufficient_evidence", INSUFFICIENT_REPEATS)
    if any(estimate.unavailable == DEGENERATE_INTERVAL for estimate in required):
        return Decision("insufficient_evidence", DEGENERATE_INTERVAL)
    if any(estimate.lower is None for estimate in required):
        raise ValueError("an available final estimate has a lower bound")
    assert focus.lower is not None
    if causal is not None:
        if tolerance is None:
            raise ValueError("a non-empty causal scope needs its tolerance")
        if published_fraction(tolerance) < 0:
            raise ValueError("a tolerance is at least 0")
    if published_fraction(focus.lower) <= 0:
        return Decision("rejected", "focus_lower_not_positive")
    if causal is not None:
        assert causal.lower is not None and tolerance is not None
        if published_fraction(causal.lower) < -published_fraction(tolerance):
            return Decision("rejected", "causal_below_tolerance")
    return Decision("accepted", "gate_passed")


def would_pass_point(
    focus: PublishedValue, causal: PublishedValue | None, tolerance: PublishedValue | None
) -> bool | None:
    """Frozen design §5.5: `delta_F > 0` and, with C, `delta_C >= -tolerance`; unavailable without one."""

    if tolerance is not None and published_fraction(tolerance) < 0:
        raise ValueError("a tolerance is at least 0")
    if causal is None:
        return published_fraction(focus) > 0
    if tolerance is None:
        return None
    return published_fraction(focus) > 0 and published_fraction(causal) >= -published_fraction(
        tolerance
    )


def decide_final_observe(*, critical: bool) -> Decision:
    """Observe enforces no number: every Attempt the non-numerical guards spare is a finalist."""

    return _CRITICAL if critical else Decision("accepted", "observe_finalist")


def decide_calibration(
    mode: str, *, degenerate: bool, unavailable: str | None
) -> CalibrationVerdict:
    """Design §6.4 and D4: statistical closes `insufficient_calibration`; observe records a caveat."""

    if unavailable not in (None, INSUFFICIENT_CALIBRATION_REPEATS):
        raise ValueError("unknown calibration unavailability")
    if mode == "statistical":
        if degenerate or unavailable is not None:
            return CalibrationVerdict(
                Decision("insufficient_evidence", "insufficient_calibration"), None
            )
        return CalibrationVerdict(None, None)
    if mode == "observe":
        if unavailable is not None:
            return CalibrationVerdict(None, NOISE_ALLOWANCE_UNAVAILABLE)
        if degenerate:
            return CalibrationVerdict(None, OBSERVE_CALIBRATION_DEGENERATE)
        return CalibrationVerdict(None, None)
    raise ValueError("mode must be 'statistical' or 'observe'")


def decide_global(
    mode: str,
    *,
    complete: bool,
    critical: bool,
    point: PublishedValue | None,
    lower: PublishedValue | None,
) -> GlobalVerdict:
    """Design §6.10: D7's fixed state first, then the critical guard, then the strict mode rule."""

    if mode not in ("off", "point", "bound"):
        raise ValueError("global is 'off', 'point' or 'bound'")
    if not complete:
        return GlobalVerdict(False, "cohort_incomplete")
    if critical:
        return GlobalVerdict(False, "critical_failure")
    if mode == "off":
        return GlobalVerdict(True, "global_off")
    if mode == "point":
        if point is None:
            raise ValueError("a complete finalist has a point contrast")
        improved = published_fraction(point) > 0
        return GlobalVerdict(
            improved, "global_point_improved" if improved else "global_point_not_improved"
        )
    if lower is None:
        raise ValueError("global: bound needs a lower bound")
    positive = published_fraction(lower) > 0
    return GlobalVerdict(
        positive, "global_bound_positive" if positive else "global_bound_not_positive"
    )


#: Design §6.11: the family-13 `selection_decided.reason` vocabulary.
SELECTION_REASONS: frozenset[str] = frozenset(
    {
        "finalist_selected",
        "no_eligible_finalist",
        "global_point_not_improved",
        "global_bound_not_positive",
        "no_finalists",
    }
)
#: Design §6.11: the guards a finalist fails before its `global` mode is read.
GUARD_REASONS: frozenset[str] = frozenset({"critical_failure", "cohort_incomplete"})


@dataclass(frozen=True)
class FinalistVerdict:
    """One finalist as the selector reads it: its cohort score and its comparison's verdict.

    `score` is the finalist's `cohort_measurement_completed.point`, null for
    an incomplete (D7) finalist; `eligible` and `reason` are its
    `global_comparison_completed`'s.
    """

    attempt_id: str
    score: PublishedValue | None
    eligible: bool
    reason: str


@dataclass(frozen=True)
class SelectionVerdict:
    """`winner` is a position in the selector's input, or `None` for a retained Champion."""

    winner: int | None
    reason: str
    ranking: tuple[dict[str, Any], ...]


def decide_selection(mode: str, finalists: Sequence[FinalistVerdict]) -> SelectionVerdict:
    """Design §6.11 on published values (G4): rank, then select or retain.

    `finalists` are in Attempt-index order. Eligible finalists rank by score
    descending, an exact tie going to the smaller index; the ineligible ones
    follow in index order. With no eligible finalist the Champion is
    retained: `no_eligible_finalist` when every finalist failed a guard
    (`GUARD_REASONS`), else the mode's own reason. No finalist at all is
    `no_finalists`.
    """

    if mode not in ("off", "point", "bound"):
        raise ValueError("global is 'off', 'point' or 'bound'")
    if not finalists:
        return SelectionVerdict(None, "no_finalists", ())
    for finalist in finalists:
        if finalist.reason not in GLOBAL_REASONS[finalist.eligible]:
            raise ValueError("a finalist's reason does not match its eligibility")
        if finalist.eligible and finalist.score is None:
            raise ValueError("an eligible finalist has a score")
    eligible = sorted(
        ((position, finalist) for position, finalist in enumerate(finalists) if finalist.eligible),
        key=lambda item: (-published_fraction(item[1].score), item[0]),  # type: ignore[arg-type]
    )
    ineligible = [
        (position, finalist) for position, finalist in enumerate(finalists) if not finalist.eligible
    ]
    ranking = tuple(
        {
            "attempt_id": finalist.attempt_id,
            "score": finalist.score,
            "eligible": finalist.eligible,
            "reason": finalist.reason,
        }
        for _, finalist in (*eligible, *ineligible)
    )
    if eligible:
        return SelectionVerdict(eligible[0][0], "finalist_selected", ranking)
    if all(finalist.reason in GUARD_REASONS for finalist in finalists):
        return SelectionVerdict(None, "no_eligible_finalist", ranking)
    if mode == "off":
        raise ValueError("under global off a finalist past the guards is eligible")
    reason = "global_point_not_improved" if mode == "point" else "global_bound_not_positive"
    return SelectionVerdict(None, reason, ranking)
