"""Exact gate regressions; analytic expectations never use the kernel to set ties."""

from fractions import Fraction as F
from math import prod

import pytest

from evolution.tree.case_statistics import METHOD, exact_e, lower_enclosure, passes_bound


def equal_e(values, threshold=F(0)):
    """AM-GM gives C_eta=(1+eta*t)^K for equal masses."""
    return sum(
        prod(1 + F(j, 16) * z for z in values)
        / (1 + F(j, 16) * threshold) ** len(values)
        / (2 if j == 16 else 30)
        for j in range(1, 17)
    )


def test_method_and_single_unit_exact_equality():
    assert METHOD == "bounded-weighted-mean/1"
    # Mean mixture coefficient is 3/4, so E(0)=1+3*z/4.
    assert exact_e([F(4, 9)], [F(1)], F(0)) == F(4, 3)
    assert not passes_bound([F(4, 9)], [F(1)], F(0), F(0.25), strict=True)
    assert passes_bound([F(4, 9)], [F(1)], F(0), F(0.25), strict=False)


@pytest.mark.parametrize("n", [2, 7])
def test_exact_tie_rejects_strict_focus_global_but_accepts_inclusive_causal(n):
    z, w = [F(1)] * n, [F(1, n)] * n
    expected = equal_e(z)
    c = 1 - 1 / expected
    assert exact_e(z, w, F(0)) == expected
    assert not passes_bound(z, w, F(0), c, strict=True)
    assert passes_bound(z, w, F(0), c, strict=False)
    assert passes_bound(z, w, F(0), c - F(1, 10000), strict=True)
    assert not passes_bound(z, w, F(0), c + F(1, 10000), strict=False)


def test_binary64_confidence_changes_actual_inclusive_and_strict_decisions():
    parsed, decimal = F(0.95), F(19, 20)
    assert parsed < decimal
    cutoff, decimal_cutoff = 1 / (1 - parsed), 1 / (1 - decimal)
    e0 = equal_e([F(1)] * 4 + [F(0)])
    slope = equal_e([F(1)] * 5) - e0
    w = [F(1, 5)] * 5
    tie = [F(1)] * 4 + [(cutoff - e0) / slope]
    middle = [F(1)] * 4 + [((cutoff + decimal_cutoff) / 2 - e0) / slope]
    assert -1 <= tie[-1] < middle[-1] <= 1
    assert exact_e(tie, w, F(0)) == cutoff
    assert passes_bound(tie, w, F(0), parsed, strict=False)
    assert not passes_bound(tie, w, F(0), decimal, strict=False)
    assert not passes_bound(tie, w, F(0), parsed, strict=True)
    assert passes_bound(middle, w, F(0), parsed, strict=True)
    assert not passes_bound(middle, w, F(0), decimal, strict=True)


def test_binary64_score_threshold_allowance_are_not_decimal_text():
    parsed = F(0.3) - F(0.4)
    decimal = F("0.3") - F("0.4")
    assert parsed < -F(0.1)
    assert decimal == -F("0.1")
    # Algebraic one-unit reference sets a true equality at the parsed threshold.
    t, z = -F(0.1) + F(0.02), [F(1)]
    c = 1 - 1 / equal_e(z, t)
    assert passes_bound(z, [F(1)], t, c, strict=False)
    assert not passes_bound(z, [F(1)], t, c, strict=True)
    assert t < -F("0.1") + F("0.02")
    assert not passes_bound(z, [F(1)], -F("0.1") + F("0.02"), c, strict=False)


@pytest.mark.parametrize("c", [F(1, 10**30), F(19, 20), 1 - F(1, 10**30)])
@pytest.mark.parametrize("strict", [False, True])
def test_support_exterior_and_single_unit(c, strict):
    assert passes_bound([F(-1)], [F(1)], F(-2), c, strict=strict)
    assert not passes_bound([F(1)], [F(1)], F(1), c, strict=strict)
    assert not passes_bound([F(1)], [F(1)], F(2), c, strict=strict)
    assert passes_bound([F(-1)], [F(1)], F(-1), c, strict=strict) is (not strict)
    assert passes_bound([F(0)], [F(1)], F(-1), c, strict=strict)


def test_unequal_support_infinite_light_loss_and_finite_heavy_loss():
    assert passes_bound([F(1), F(-1)], [F(9, 10), F(1, 10)], F(-1), F(0.95), strict=True)
    z, w = [F(-1)] + [F(1)] * 20, [F(1, 2)] + [F(1, 40)] * 20
    limit = sum(((40 + 21 * F(j, 16)) / (40 - 21 * F(j, 16))) ** 20 / 30 for j in range(1, 16))
    c = 1 - 1 / limit
    assert not passes_bound(z, w, F(-1), c, strict=True)
    assert passes_bound(z, w, F(-1), c, strict=False)
    assert passes_bound(z, w, F(-1), c - F(1, 10000), strict=True)
    assert not passes_bound(z, w, F(-1), (1 + c) / 2, strict=True)


@pytest.mark.parametrize("n", range(1, 7))
def test_maximal_equal_profile_first_passes_parsed_95_at_five_units(n):
    z, w = [F(1)] * n, [F(1, n)] * n
    assert exact_e(z, w, F(0)) == equal_e(z)
    assert passes_bound(z, w, F(0), F(0.95), strict=True) is (n >= 5)


@pytest.mark.parametrize(
    "wins,losses,zeros,want",
    [
        (20, 5, 108, True),
        (1, 0, 132, False),
        (3, 0, 130, False),
        (4, 0, 129, False),
        (2, 0, 0, False),
        (5, 0, 128, True),
    ],
)
def test_sparse_utility_and_stored_reference_ceiling(wins, losses, zeros, want):
    z = [F(1)] * wins + [F(-1)] * losses + [F(0)] * zeros
    w = [F(1, len(z))] * len(z)
    assert exact_e(z, w, F(0)) == equal_e(z)
    assert passes_bound(z, w, F(0), F(19, 20), strict=True) is want
    assert passes_bound(z, w, F(0), F(0.95), strict=True) is want


def test_collapsed_small_positive_gain_remains_conservative():
    z, w = [F(1, 50)] * 133, [F(1, 133)] * 133
    assert exact_e(z, w, F(0)) == equal_e(z)
    assert not passes_bound(z, w, F(0), F(19, 20), strict=True)
    assert lower_enclosure(z, w, F(19, 20)) < 0


@pytest.mark.parametrize("steps", [0, 1, 63, 64, 70])
def test_display_enclosure_has_at_least_64_certified_bisections(steps):
    # L=0 exactly. The independent formula certifies the adjacent upper endpoint.
    z, w = [F(4, 9)], [F(1)]
    lower = lower_enclosure(z, w, F(1, 4), steps=steps)
    width = F(2, 2 ** max(steps, 64))
    assert lower <= 0 <= lower + width
    assert equal_e(z, lower) >= F(4, 3)
    assert equal_e(z, lower + width) <= F(4, 3)


def test_display_enclosure_empty_support_and_nonzero_root():
    assert lower_enclosure([F(-1)], [F(1)], F(0.95)) == -1
    z, w, c = [F(1)] * 5, [F(1, 5)] * 5, F(0.95)
    lower = lower_enclosure(z, w, c)
    assert equal_e(z, lower) >= 1 / (1 - c)
    assert equal_e(z, lower + F(1, 2**63)) <= 1 / (1 - c)
    assert lower < 1


@pytest.mark.parametrize(
    "z,w",
    [
        ([], []),
        ([F(0)], []),
        ([F(0)], [F(0)]),
        ([F(0)], [F(-1)]),
        ([F(0)], [F(1, 2)]),
        ([F(2)], [F(1)]),
        ([F(-2)], [F(1)]),
        ([0.0], [F(1)]),
        ([F(0)], [1.0]),
        ([True], [F(1)]),
    ],
)
def test_malformed_profiles_rejected_even_at_support_shortcuts(z, w):
    with pytest.raises((TypeError, ValueError)):
        exact_e(z, w, F(0))
    with pytest.raises((TypeError, ValueError)):
        passes_bound(z, w, F(-2), F(1, 2), strict=False)
    with pytest.raises((TypeError, ValueError)):
        lower_enclosure(z, w, F(1, 2))


@pytest.mark.parametrize("c", [F(0), F(1), F(-1), F(2), 0.95, True])
def test_confidence_validation(c):
    with pytest.raises((TypeError, ValueError)):
        passes_bound([F(0)], [F(1)], F(-2), c, strict=False)
    with pytest.raises((TypeError, ValueError)):
        lower_enclosure([F(0)], [F(1)], c)


@pytest.mark.parametrize("t", [F(-1), F(1), F(2), 0.0, True])
def test_exact_e_requires_rational_interior_threshold(t):
    with pytest.raises((TypeError, ValueError)):
        exact_e([F(0)], [F(1)], t)


@pytest.mark.parametrize("steps", [-1, 1.5, True])
def test_invalid_display_steps(steps):
    with pytest.raises((TypeError, ValueError)):
        lower_enclosure([F(0)], [F(1)], F(1, 2), steps=steps)


def test_gate_rejects_nonrational_threshold_and_nonboolean_strictness():
    with pytest.raises(TypeError):
        passes_bound([F(0)], [F(1)], 0.0, F(1, 2), strict=True)
    with pytest.raises(TypeError):
        passes_bound([F(0)], [F(1)], F(0), F(1, 2), strict=1)


@pytest.mark.parametrize("c,want", [(F(1, 10**30), True), (1 - F(1, 10**30), False)])
def test_single_unit_interior_extreme_confidence_has_no_minimum_count_guard(c, want):
    assert passes_bound([F(1)], [F(1)], F(0), c, strict=True) is want
