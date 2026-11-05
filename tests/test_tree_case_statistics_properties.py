"""Independent active-set optimization and exact finite null distributions."""

from collections import Counter
from fractions import Fraction as F
from functools import cache
from itertools import product
from math import comb, prod

import pytest

from evolution.tree.case_statistics import exact_e, passes_bound
from tests.test_tree_case_statistics import equal_e


@cache
def reference_normalizer(weights, threshold, eta):
    """Enumerate lower/free/upper KKT faces, not water-level breakpoints.

    Equal-mass coordinates share an optimum by strict concavity. Enumerating
    faces of these distinct mass classes keeps the unequal 1/100 fixture small.
    """
    counts = Counter(weights)
    masses = tuple(counts)
    lambdas = [eta * (1 + a / max(weights)) / 2 for a in masses]
    best = F(0)
    for face in product((-1, 0, 1), repeat=len(masses)):
        free = [i for i, state in enumerate(face) if state == 0]
        remaining = threshold - sum(
            counts[a] * a * state for a, state in zip(masses, face, strict=True)
        )
        if free:
            # On a free face lambda/(1+lambda*u) = multiplier*a.
            reciprocal = (
                remaining + sum(counts[masses[i]] * masses[i] / lambdas[i] for i in free)
            ) / sum(counts[masses[i]] for i in free)
            us = [
                reciprocal / a - 1 / lam if state == 0 else F(state)
                for a, lam, state in zip(masses, lambdas, face, strict=True)
            ]
        else:
            us = list(map(F, face))
        if any(abs(u) > 1 for u in us):
            continue
        if sum(counts[a] * a * u for a, u in zip(masses, us, strict=True)) > threshold:
            continue
        value = prod(
            (1 + lam * u) ** counts[a] for a, lam, u in zip(masses, lambdas, us, strict=True)
        )
        best = max(best, value)
    assert best > 0
    return best


def reference_e(z, weights, threshold=F(0)):
    return sum(
        prod(
            (1 + F(j, 16) * (1 + a / max(weights)) / 2 * value) ** n
            for (a, value), n in Counter(zip(weights, z, strict=True)).items()
        )
        / reference_normalizer(tuple(weights), threshold, F(j, 16))
        / (2 if j == 16 else 30)
        for j in range(1, 17)
    )


@pytest.mark.parametrize("weights", [(F(1, 2), F(1, 2)), (F(1, 4), F(3, 4))])
@pytest.mark.parametrize("threshold", [F(-1, 2), F(0), F(1, 2)])
def test_independent_optimizer_grid_and_exact_product(weights, threshold):
    # All 12 predeclared grid comparisons; independently solved face maxima
    # then check the public mixture at all 289 possible observed pairs.
    grid = tuple(F(k, 8) for k in range(-8, 9))
    for eta in (F(1, 2), F(1)):
        lam = [eta * (1 + a / max(weights)) / 2 for a in weights]
        candidates = [
            prod(1 + coefficient * u for coefficient, u in zip(lam, us, strict=True))
            for us in product(grid, repeat=2)
            if sum(a * u for a, u in zip(weights, us, strict=True)) <= threshold
        ]
        optimum = reference_normalizer(weights, threshold, eta)
        assert max(candidates) <= optimum
        if weights[0] == weights[1]:
            assert max(candidates) == optimum
    for z in product(grid, repeat=2):
        assert exact_e(z, weights, threshold) == reference_e(z, weights, threshold)


FAIR = ((F(-1), F(1, 2)), (F(1), F(1, 2)))
SKEW = ((F(-1), F(1, 10)), (F(1, 9), F(9, 10)))
NULLS = [
    ([F(1, 8)] * 8, [FAIR] * 8, F(0)),
    ([F(1, 8)] * 8, [SKEW] * 8, F(0)),
    ([F(1, 2), F(1, 4), F(1, 8), F(1, 8)], [FAIR] * 4, F(0)),
    (
        [F(1, 2), F(1, 4), F(1, 4)],
        [
            ((F(-1), F(1, 4)), (F(1), F(3, 4))),
            ((F(-1), F(3, 4)), (F(1), F(1, 4))),
            ((F(-1), F(3, 4)), (F(1), F(1, 4))),
        ],
        F(0),
    ),
    ([F(1, 4)] * 4, [((F(-1), F(3, 4)), (F(1), F(1, 4)))] * 4, F(0)),
    ([F(1, 4)] * 4, [((F(-1), F(5, 8)), (F(1), F(3, 8)))] * 4, F(-1, 4)),
    ([F(1, 4)] * 4, [((F(-1), F(3, 8)), (F(1), F(5, 8)))] * 4, F(1, 4)),
]


@pytest.mark.parametrize(
    "weights,supports,threshold",
    NULLS,
    ids=[
        "iid-sign-8",
        "sparse-loss-8",
        "unequal",
        "heterogeneous-mean",
        "negative-null",
        "negative-margin",
        "positive-margin",
    ],
)
def test_exact_independent_null_enumeration(weights, supports, threshold):
    mean = sum(
        a * sum(z * p for z, p in support) for a, support in zip(weights, supports, strict=True)
    )
    assert mean <= threshold
    total, expected = F(0), F(0)
    tails = {F(1, 10): F(0), F(1, 20): F(0), F(1, 100): F(0)}
    for atoms in product(*supports):
        z = [v for v, _ in atoms]
        probability = prod(p for _, p in atoms)
        e = exact_e(z, weights, threshold)
        assert e == reference_e(z, weights, threshold)
        total += probability
        expected += probability * e
        for alpha in tails:
            if e >= 1 / alpha:
                tails[alpha] += probability
    assert total == 1
    assert expected <= 1
    assert all(tail <= alpha for alpha, tail in tails.items())


@pytest.mark.parametrize(
    "n,gain,loss",
    [
        (133, F(1, 50), F(1, 50)),
        (133, F(1, 10), F(1, 10)),
        (133, F(1, 100), F(1, 50)),
        (133, F(1, 200), F(1, 200)),
        (20, F(1, 4), F(1, 4)),
    ],
)
def test_sparse_multinomial_and_fresh_reference_null(n, gain, loss):
    # Enumerate every count vector with exact probabilities and exact analytic
    # evidence. Compare product kernel at each rejection frontier (both sides).
    tails = {F(1, 10): F(0), F(1, 20): F(0), F(1, 100): F(0)}
    total, expected = F(0), F(0)
    for losses in range(n + 1):
        previous = None
        checked = set()
        for wins in range(n - losses + 1):
            zeros = n - wins - losses
            probability = (
                F(comb(n, wins) * comb(n - wins, losses))
                * gain**wins
                * loss**losses
                * (1 - gain - loss) ** zeros
            )
            e = sum(
                (1 + F(j, 16)) ** wins * (1 - F(j, 16)) ** losses / (2 if j == 16 else 30)
                for j in range(1, 17)
            )
            total += probability
            expected += probability * e
            for alpha in tails:
                if e >= 1 / alpha:
                    tails[alpha] += probability
                    if alpha not in checked:
                        for k, want in [(wins, e), (wins - 1, previous)]:
                            if k >= 0:
                                z = [F(1)] * k + [F(-1)] * losses + [F(0)] * (n - k - losses)
                                assert exact_e(z, [F(1, n)] * n, F(0)) == want
                        checked.add(alpha)
            previous = e
    assert total == 1 and expected <= 1
    assert all(tail <= alpha for alpha, tail in tails.items())


@pytest.mark.parametrize(
    "sizes,want_light,want_heavy",
    [([1, 20], False, False), ([1, 100], True, False), ([3, 60], True, True)],
)
def test_unequal_weights_retain_outcome_association(sizes, want_light, want_heavy):
    w = [F(1, len(sizes) * size) for size in sizes for _ in range(size)]
    light, heavy = [F(1)] * (sum(sizes) - 1) + [F(-1)], [F(-1)] + [F(1)] * (sum(sizes) - 1)
    light_e, heavy_e = reference_e(light, w), reference_e(heavy, w)
    assert exact_e(light, w, F(0)) == light_e > heavy_e == exact_e(heavy, w, F(0))
    assert passes_bound(light, w, F(0), F(19, 20), strict=True) is want_light
    assert passes_bound(heavy, w, F(0), F(19, 20), strict=True) is want_heavy
    assert exact_e(list(reversed(light)), list(reversed(w)), F(0)) == light_e


@pytest.mark.parametrize("n", [20, 100])
def test_unequal_maximal_passes_moderate_gains_fail(n):
    w = [F(1, 2)] + [F(1, 2 * n)] * n
    for gain, want in [(F(1), True), (F(1, 5), False)]:
        z = [gain] * (n + 1)
        assert exact_e(z, w, F(0)) == reference_e(z, w)
        assert passes_bound(z, w, F(0), F(19, 20), strict=True) is want


def test_block_duplicates_do_not_create_units_and_dependence_is_not_calibrated():
    blocks = [F(1), F(1), F(-1)] + [F(0)] * 7
    w = [F(1, 10)] * 10
    reconstructed = [sum([v] * 10) / 10 for v in blocks]
    assert exact_e(reconstructed, w, F(0)) == equal_e(blocks)
    # A shared random sign falsely treated as 20 independent units rejects
    # with probability 1/2, documenting the independence assumption.
    tail = sum(
        p
        for sign, p in FAIR
        if passes_bound([sign] * 20, [F(1, 20)] * 20, F(0), F(19, 20), strict=True)
    )
    assert tail == F(1, 2)
    assert not passes_bound([F(1)], [F(1)], F(0), F(19, 20), strict=True)


def test_selected_zero_baseline_characterization():
    # A no-op with fresh Bernoulli(1/2) scores against selected stored zeroes
    # has a positive conditional contrast; this is not a campaign null.
    probability = sum(
        F(comb(20, k), 2**20)
        for k in range(21)
        if passes_bound(
            [F(1)] * k + [F(0)] * (20 - k), [F(1, 20)] * 20, F(0), F(19, 20), strict=True
        )
    )
    assert probability == sum(F(comb(20, k), 2**20) for k in range(5, 21))
    assert probability > F(99, 100)


def test_evidence_strictly_decreases_in_threshold():
    z, w = [F(1), F(0), F(-1)], [F(1, 2), F(1, 4), F(1, 4)]
    es = [exact_e(z, w, F(k, 10)) for k in range(-9, 10)]
    assert all(a > b for a, b in zip(es, es[1:], strict=False))


@pytest.mark.parametrize("threshold", [F(-999, 1000), F(-1, 3), F(2, 3), F(999, 1000)])
@pytest.mark.parametrize("w", [(F(1, 1000), F(999, 1000)), (F(1, 2), F(1, 3), F(1, 6))])
def test_clipped_optimizer_near_support_and_with_distinct_weights(w, threshold):
    z = [F(1, 4), F(-1, 3), F(3, 4)][: len(w)]
    assert exact_e(z, w, threshold) == reference_e(z, w, threshold)
