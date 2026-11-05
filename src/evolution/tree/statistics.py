"""Small exact-paired and honest-unpaired promotion gates."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

BOOTSTRAP_DRAWS = 10_000
SeedMode = Literal["exact", "unpaired", "none"]
_TWO_53 = 9007199254740992.0  # 2**53: the width of `random()`'s mantissa


@dataclass(frozen=True)
class GateResult:
    decision: Literal["promote", "reject", "continue"]
    lower: float
    upper: float
    repeats: int
    alpha: float = 0.05
    look: int = 0
    reason: str = ""


@dataclass(frozen=True)
class ScoreSample:
    case_id: str
    sample_id: str
    score: float
    requested_seed: int | None = None
    effective_seed: int | None = None
    boot_id: str = ""
    dependency_digest: str = ""
    group_id: str = ""
    stratum: str = ""

    @property
    def identity(self) -> tuple[str, str]:
        return self.case_id, self.sample_id


@dataclass(frozen=True)
class GatePlan:
    seed_mode: SeedMode
    random_seed: int
    alpha_spend: Mapping[int, Fraction]
    alpha_budget: Fraction = Fraction(1, 20)
    draws: int = BOOTSTRAP_DRAWS

    def __post_init__(self) -> None:
        if self.draws < 100:
            raise ValueError("draws must be at least 100")
        values = [*self.alpha_spend.values(), self.alpha_budget]
        if any(type(value) is not Fraction for value in values):
            raise ValueError("alpha must be exact")
        if not 0 < self.alpha_budget < 1:
            raise ValueError("alpha_budget must be in (0, 1)")
        if not self.alpha_spend or any(
            type(look) is not int or look < 1 for look in self.alpha_spend
        ):
            raise ValueError("alpha_spend looks must be positive integers")
        if any(not 0 < alpha < 1 for alpha in self.alpha_spend.values()):
            raise ValueError("alpha spend must be in (0, 1)")
        if sum(self.alpha_spend.values(), Fraction(0)) > self.alpha_budget:
            raise ValueError("alpha spend exceeds caller-supplied budget")


def gate_at_look(
    parent: Sequence[ScoreSample],
    candidate: Sequence[ScoreSample],
    plan: GatePlan,
    look: int,
    *,
    excluded_repeats: bool = False,
) -> GateResult:
    """Evaluate one declared look after proving its sample provenance.

    `excluded_repeats` (campaign family 6, continuable-terminals spec §4.5) says
    the caller removed excluded pairs from both sides, so a case may hold any
    `1 <= n_c <= look` repeats; both sides must then hold the same count per
    case. Without it every case must hold `look` repeats, as before. On
    inputs where every case holds the same count the kernel consumes the same
    draws in the same order and returns the same result either way.
    """

    if look not in plan.alpha_spend:
        raise ValueError("look has no caller-supplied alpha spend")
    parent_by_case = _select_samples(parent, look, partial=excluded_repeats)
    candidate_by_case = _select_samples(candidate, look, partial=excluded_repeats)
    if parent_by_case.keys() != candidate_by_case.keys():
        raise ValueError("gate requires matching case IDs")
    if any(
        len(parent_by_case[case_id]) != len(candidate_by_case[case_id])
        for case_id in parent_by_case
    ):
        raise ValueError("gate requires matching repeat counts per case")
    if plan.seed_mode == "exact":
        values = _paired_deltas(parent_by_case, candidate_by_case)
        samples = _bootstrap_groups(values, None, plan.draws, plan.random_seed + look)
    else:
        # "unpaired" and "none" both take the group-aware unpaired path: it
        # never assumes a shared seed, so a seedless (`none`) condition needs
        # nothing else here.
        parent_values = _group_values(parent_by_case)
        candidate_values = _group_values(candidate_by_case)
        if parent_values.keys() != candidate_values.keys():
            raise ValueError("unpaired gate requires matching group IDs")
        if any(
            parent_values[group].keys() != candidate_values[group].keys() for group in parent_values
        ):
            raise ValueError("unpaired gate requires matching case IDs within each group")
        samples = _bootstrap_groups(
            candidate_values, parent_values, plan.draws, plan.random_seed + look
        )
    lower, upper = _interval(samples, plan.alpha_spend[look])
    alpha = float(plan.alpha_spend[look])
    if lower > 0:
        return GateResult(
            "promote", float(lower), float(upper), look, alpha, look, "lower_bound_positive"
        )
    if upper <= 0:
        return GateResult(
            "reject", float(lower), float(upper), look, alpha, look, "upper_bound_nonpositive"
        )
    return GateResult(
        "continue", float(lower), float(upper), look, alpha, look, "interval_crosses_zero"
    )


def repeat_scores(samples: Sequence[ScoreSample], look: int) -> list[float]:
    """Score each repeat on its own, so a caller can read the run's own spread.

    ``group_weighted_score`` averages the repeats away, which hides how far
    one draw of the same tree lands from the next. The per-repeat series keeps
    that spread; its mean is still the group-weighted score.

    Every case must hold `look` repeats: a per-repeat series has no meaning for
    a case some of whose repeats were excluded, and no caller feeds it one.
    """

    selected = _select_samples(samples, look)
    return [
        group_weighted_score([case_samples[index] for case_samples in selected.values()], 1)
        for index in range(look)
    ]


def group_weighted_score(
    samples: Sequence[ScoreSample], look: int, *, excluded_repeats: bool = False
) -> float:
    """Average repeats within cases, cases within groups, then groups equally.

    A case's mean is over its own repeats, so with `excluded_repeats` a case
    holding fewer than `look` of them is averaged over the ones it holds.
    """

    groups = _group_values(_select_samples(samples, look, partial=excluded_repeats))
    group_means = [
        math.fsum(math.fsum(values) / len(values) for values in cases.values()) / len(cases)
        for cases in groups.values()
    ]
    return math.fsum(group_means) / len(group_means)


def _select_samples(
    samples: Sequence[ScoreSample], look: int, *, partial: bool = False
) -> dict[str, tuple[ScoreSample, ...]]:
    grouped: dict[str, list[ScoreSample]] = defaultdict(list)
    identities: set[tuple[str, str]] = set()
    for sample in samples:
        _validate_sample(sample)
        if sample.identity in identities:
            raise ValueError("duplicate sample identity")
        identities.add(sample.identity)
        grouped[sample.case_id].append(sample)
    if not grouped:
        raise ValueError("gate needs samples")
    selected: dict[str, tuple[ScoreSample, ...]] = {}
    for case_id, values in grouped.items():
        ordered = tuple(sorted(values, key=lambda item: _sample_order(item.sample_id)))
        if len(ordered) < look and not partial:
            raise ValueError(f"case {case_id!r} lacks samples for look {look}")
        selected[case_id] = ordered[:look]
    return selected


def _validate_sample(sample: ScoreSample) -> None:
    if not sample.case_id or not sample.sample_id or not 0 <= sample.score <= 1:
        raise ValueError("samples need non-empty identities and scores in [0, 1]")
    if not sample.group_id or not sample.stratum:
        raise ValueError("samples need corpus group_id and stratum")


def _sample_order(sample_id: str) -> tuple[int, int | str]:
    return (0, int(sample_id)) if sample_id.isdecimal() else (1, sample_id)


def _paired_deltas(
    parent: Mapping[str, Sequence[ScoreSample]], candidate: Mapping[str, Sequence[ScoreSample]]
) -> dict[str, dict[str, tuple[Fraction, ...]]]:
    if parent.keys() != candidate.keys():
        raise ValueError("exact paired gate requires matching case IDs")
    grouped: dict[str, dict[str, tuple[Fraction, ...]]] = defaultdict(dict)
    for case_id, parent_samples in parent.items():
        candidate_samples = {sample.sample_id: sample for sample in candidate[case_id]}
        if {sample.sample_id for sample in parent_samples} != set(candidate_samples):
            raise ValueError("exact paired gate requires matching sample identities")
        _validate_exact_proof(parent_samples, candidate_samples)
        grouped[parent_samples[0].group_id][case_id] = tuple(
            Fraction(candidate_samples[sample.sample_id].score) - Fraction(sample.score)
            for sample in parent_samples
        )
    return dict(grouped)


def _validate_exact_proof(
    parent: Sequence[ScoreSample], candidate_by_id: Mapping[str, ScoreSample]
) -> None:
    """Prove v2's pairing rule (`rollout.are_samples_paired`, restated here to
    keep this module free of a `rollout` import): matching group/stratum,
    `requested_seed == effective_seed` on both sides and equal across sides,
    and an equal `dependency_digest` across sides (part of `condition_key`).
    `boot_id` is not part of the proof: in v2 the parent and Candidate run in
    different service instances, and a measurement may use several."""

    for left in parent:
        right = candidate_by_id[left.sample_id]
        if (left.group_id, left.stratum) != (right.group_id, right.stratum):
            raise ValueError("exact paired gate requires matching group and stratum")
        if (
            left.requested_seed is None
            or left.effective_seed is None
            or right.requested_seed is None
            or right.effective_seed is None
            or left.requested_seed != left.effective_seed
            or right.requested_seed != right.effective_seed
            or left.requested_seed != right.requested_seed
        ):
            raise ValueError("exact paired gate requires matching effective seeds")
        if not left.dependency_digest or left.dependency_digest != right.dependency_digest:
            raise ValueError("exact paired gate requires matching dependency digest")


def _group_values(
    samples_by_case: Mapping[str, Sequence[ScoreSample]],
) -> dict[str, dict[str, tuple[Fraction, ...]]]:
    grouped: dict[str, dict[str, tuple[Fraction, ...]]] = defaultdict(dict)
    for case_id, samples in samples_by_case.items():
        group_id, stratum = samples[0].group_id, samples[0].stratum
        if any((sample.group_id, sample.stratum) != (group_id, stratum) for sample in samples):
            raise ValueError("case samples must retain one group and stratum")
        grouped[group_id][case_id] = tuple(Fraction(sample.score) for sample in samples)
    return dict(grouped)


def _draw(randomizer: random.Random, n: int) -> int:
    """An index in `range(n)` from the `random()` stream, by integer arithmetic.

    `random()` is the one stream CPython documents as reproducible across
    versions; multiplying it by 2**53 is exact on every IEEE platform, so the
    index is a function of the seed alone (spec §5).
    """

    return (int(randomizer.random() * _TWO_53) * n) >> 53


def _scaled(
    groups: Mapping[str, Mapping[str, Sequence[Fraction]]],
) -> tuple[dict[str, dict[str, tuple[int, ...]]], int]:
    """Integer numerators over one common power-of-two denominator, and its exponent."""

    exponent = 0
    for cases in groups.values():
        for values in cases.values():
            for value in values:
                denominator = value.denominator
                if denominator & (denominator - 1):
                    raise ValueError("scores must be dyadic rationals")
                exponent = max(exponent, denominator.bit_length() - 1)
    scaled = {
        group_id: {
            case_id: tuple(
                value.numerator << (exponent - (value.denominator.bit_length() - 1))
                for value in values
            )
            for case_id, values in cases.items()
        }
        for group_id, cases in groups.items()
    }
    return scaled, exponent


def _bootstrap_groups(
    candidate: Mapping[str, Mapping[str, Sequence[Fraction]]],
    parent: Mapping[str, Mapping[str, Sequence[Fraction]]] | None,
    draws: int,
    seed: int,
) -> list[Fraction]:
    """Resample correlation groups, then cases, then repeated observations — exactly.

    Each case's repeats are resampled with replacement from its own count `n_c`
    and its mean is its sum over `n_c` (continuable-terminals spec §4.5). The
    arithmetic stays integer: every case sum is scaled by `common // n_c` over
    one unit `common << exponent`, `common` being the least common multiple of
    the case counts. With one count for every case `common` is that count and
    every factor is `1`, so the draws, their order and every total are exactly
    those of the uniform kernel.
    """

    randomizer = random.Random(seed)
    group_ids = tuple(candidate)
    if not group_ids:
        raise ValueError("gate needs groups")
    counts = {len(values) for cases in candidate.values() for values in cases.values()}
    if parent is not None:
        counts |= {len(values) for cases in parent.values() for values in cases.values()}
    if not counts or min(counts) < 1:
        raise ValueError("gate needs at least one repeat per case")
    count = math.lcm(*counts)
    merged = dict(candidate)
    if parent is not None:
        merged = {
            **{f"c:{g}": c for g, c in candidate.items()},
            **{f"p:{g}": c for g, c in parent.items()},
        }
    scaled, exponent = _scaled(merged)
    if parent is None:
        candidate_int = {g: scaled[g] for g in candidate}
        parent_int = None
    else:
        candidate_int = {g: scaled[f"c:{g}"] for g in candidate}
        parent_int = {g: scaled[f"p:{g}"] for g in parent}
    unit = count << exponent  # every case mean is `scaled_case_sum / unit`
    samples: list[Fraction] = []
    for _ in range(draws):
        group_means: list[Fraction] = []
        for _ in group_ids:
            group_id = group_ids[_draw(randomizer, len(group_ids))]
            cases = candidate_int[group_id]
            case_ids = tuple(cases)
            total = 0
            for _ in case_ids:
                case_id = case_ids[_draw(randomizer, len(case_ids))]
                values = cases[case_id]
                total += _resampled_sum(values, randomizer) * (count // len(values))
                if parent_int is not None:
                    parent_values = parent_int[group_id][case_id]
                    total -= _resampled_sum(parent_values, randomizer) * (
                        count // len(parent_values)
                    )
            group_means.append(Fraction(total, len(case_ids) * unit))
        samples.append(sum(group_means, Fraction(0)) / len(group_means))
    return samples


def _resampled_sum(values: Sequence[int], randomizer: random.Random) -> int:
    return sum(values[_draw(randomizer, len(values))] for _ in values)


def _interval(samples: list[Fraction], alpha: Fraction) -> tuple[Fraction, Fraction]:
    samples.sort()
    lower_index = math.floor(alpha / 2 * len(samples))
    upper_index = math.ceil((1 - alpha / 2) * len(samples)) - 1
    return samples[lower_index], samples[upper_index]
