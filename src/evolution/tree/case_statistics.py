"""Exact fixed-sample bounds for independent bounded unit contrasts.

Inputs are canonical rational values: callers convert parsed binary64 numbers
with Fraction(value), and derive weights exactly from counts. This method has
no optional-stopping or task-population guarantee. See ADR-0041 and the frozen
independent-acceptance design, section 4.
"""

from collections import Counter
from collections.abc import Sequence
from fractions import Fraction
from math import prod

METHOD = "bounded-weighted-mean/1"

_BETS = tuple((Fraction(j, 16), Fraction(1, 30)) for j in range(1, 16)) + (
    (Fraction(1), Fraction(1, 2)),
)


def _rational(value: Fraction, name: str) -> None:
    if not isinstance(value, Fraction):
        raise TypeError(f"{name} must be a Fraction")


def _validate(z: Sequence[Fraction], weights: Sequence[Fraction]) -> None:
    if not z or len(z) != len(weights):
        raise ValueError("unit outcomes and weights must be nonempty and aligned")
    for value, weight in zip(z, weights, strict=True):
        _rational(value, "unit outcome")
        _rational(weight, "unit weight")
        if not -1 <= value <= 1:
            raise ValueError("unit outcomes must lie in [-1, 1]")
        if weight <= 0:
            raise ValueError("unit weights must be positive")
    if sum(weights) != 1:
        raise ValueError("unit weights must sum exactly to one")


def _confidence(confidence: Fraction) -> Fraction:
    _rational(confidence, "confidence")
    if not 0 < confidence < 1:
        raise ValueError("confidence must lie strictly between zero and one")
    return 1 / (1 - confidence)


def _normalizer(masses: Counter[Fraction], threshold: Fraction, eta: Fraction) -> Fraction:
    """Solve the clipped water-level budget by an exact breakpoint sweep.

    Equal-mass coordinates have equal coefficients and optimal values, so their
    multiplicities can be retained without expanding the product or sweep.
    """
    largest = max(masses)
    bounds = []
    events: Counter[Fraction] = Counter()
    budget = threshold
    mass = Fraction(0)
    for weight, count in masses.items():
        coefficient = eta * (1 + weight / largest) / 2
        cost = weight / coefficient
        low, high = cost * (1 - coefficient), cost * (1 + coefficient)
        bounds.append((cost, low, high, count))
        budget += count * cost
        mass += count * low
        events[low] += count
        events[high] -= count

    previous = min(events)
    slope = 0
    level = previous
    for point in sorted(events):
        next_mass = mass + slope * (point - previous)
        if mass <= budget <= next_mass:
            # For a flat segment the smallest solution is its left endpoint.
            level = previous if slope == 0 else previous + (budget - mass) / slope
            break
        mass = next_mass
        slope += events[point]
        previous = point
    else:
        raise ArithmeticError("no feasible bounded-mean water level")

    return prod(
        ((min(high, max(low, level)) / cost) ** count for cost, low, high, count in bounds),
        start=Fraction(1),
    )


def _e(
    outcomes: Counter[tuple[Fraction, Fraction]],
    masses: Counter[Fraction],
    threshold: Fraction,
) -> Fraction:
    largest = max(masses)
    return sum(
        (
            mixture
            * prod(
                (1 + eta * (1 + weight / largest) / 2 * value) ** count
                for (weight, value), count in outcomes.items()
            )
            / _normalizer(masses, threshold, eta)
            for eta, mixture in _BETS
        ),
        Fraction(0),
    )


def exact_e(z: Sequence[Fraction], weights: Sequence[Fraction], threshold: Fraction) -> Fraction:
    """Return the exact mixture evidence at an interior null threshold."""
    _validate(z, weights)
    _rational(threshold, "threshold")
    if not -1 < threshold < 1:
        raise ValueError("evidence requires a threshold strictly inside (-1, 1)")
    return _e(Counter(zip(weights, z, strict=True)), Counter(weights), threshold)


def _above_lower_support(
    outcomes: Counter[tuple[Fraction, Fraction]],
    largest: Fraction,
    cutoff: Fraction,
) -> bool:
    if not any(weight == largest and value == -1 for weight, value in outcomes):
        return True  # The eta=1 numerator is positive and its limit is infinite.
    limit = sum(
        mixture
        * prod(
            (
                (1 + eta * (1 + weight / largest) / 2 * value)
                / (1 - eta * (1 + weight / largest) / 2)
            )
            ** count
            for (weight, value), count in outcomes.items()
        )
        for eta, mixture in _BETS[:-1]
    )
    return limit > cutoff


def passes_bound(
    z: Sequence[Fraction],
    weights: Sequence[Fraction],
    threshold: Fraction,
    confidence: Fraction,
    *,
    strict: bool,
) -> bool:
    """Decide L > threshold (strict) or L >= threshold with exact arithmetic."""
    _validate(z, weights)
    _rational(threshold, "threshold")
    cutoff = _confidence(confidence)
    if not isinstance(strict, bool):
        raise TypeError("strict must be a bool")
    if threshold < -1:
        return True
    if threshold >= 1:
        return False
    outcomes, masses = Counter(zip(weights, z, strict=True)), Counter(weights)
    if threshold == -1:
        return not strict or _above_lower_support(outcomes, max(masses), cutoff)
    evidence = _e(outcomes, masses, threshold)
    return evidence > cutoff if strict else evidence >= cutoff


def lower_enclosure(
    z: Sequence[Fraction],
    weights: Sequence[Fraction],
    confidence: Fraction,
    *,
    steps: int = 64,
) -> Fraction:
    """Return a certified display lower endpoint, never a gate decision.

    At least 64 exact bisections enclose L in [result, result+2**(1-max(64, steps))].
    With an empty rejection set the endpoint is exactly -1.
    """
    _validate(z, weights)
    cutoff = _confidence(confidence)
    if isinstance(steps, bool) or not isinstance(steps, int):
        raise TypeError("steps must be an integer")
    if steps < 0:
        raise ValueError("steps must be nonnegative")
    outcomes, masses = Counter(zip(weights, z, strict=True)), Counter(weights)
    if not _above_lower_support(outcomes, max(masses), cutoff):
        return Fraction(-1)
    low, high = Fraction(-1), Fraction(1)
    for _ in range(max(64, steps)):
        middle = (low + high) / 2
        if _e(outcomes, masses, middle) >= cutoff:
            low = middle
        else:
            high = middle
    return low
