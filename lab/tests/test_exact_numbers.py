"""Family-15 statistics: Core's exact rational kept next to a display value (display only, never a gate)."""
import math
import sys
from fractions import Fraction

import pytest
from evolution.tree.family15_schema import encode_rational

from evolution_lab.owner.exact_numbers import statistical


def test_statistical_quantity_keeps_cores_object_and_rounds_to_6():
    exact = encode_rational(Fraction(123456789, 10**9))
    assert statistical(exact) == {"value": 0.123457, "exact": exact}


def test_spec_example_values():                       # 0.12, 0.03, 0.9 round-trip
    assert [statistical(encode_rational(Fraction(n, 100)))["value"] for n in (12, 3, 90)] == [0.12, 0.03, 0.9]


def test_null_stays_null():
    assert statistical(None) is None


@pytest.mark.parametrize("sign", [1, -1])
def test_outside_binary64_is_value_null_with_exact_kept(sign):
    big = sign * (Fraction(10**308) + Fraction(10**308))          # Core's 1e308 + 1e308 case
    exact = encode_rational(big)
    assert statistical(exact) == {"value": None, "exact": exact}


@pytest.mark.parametrize("bad", [{"format": "rational-hex/2", "numerator": "1", "denominator": "1"},
                                 {"format": "rational-hex/1", "numerator": "1", "denominator": "0"},
                                 {"format": "rational-hex/1", "numerator": "1", "denominator": "1", "x": 1}])
def test_malformed_rational_is_refused(bad):
    with pytest.raises(ValueError):
        statistical(bad)


def test_binary64_max_is_displayable_with_exact_kept():
    exact = encode_rational(Fraction(sys.float_info.max))
    assert statistical(exact) == {"value": sys.float_info.max, "exact": exact}


def test_negative_tiny_value_normalizes_display_negative_zero():
    exact = encode_rational(Fraction(-1, 10**9))
    result = statistical(exact)
    assert result == {"value": 0.0, "exact": exact}
    assert math.copysign(1, result["value"]) == 1
