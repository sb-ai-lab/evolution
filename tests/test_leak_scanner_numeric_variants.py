"""rev D2: a leaked expected constant is caught across surface forms — the
scanner normalizes 5000 / 5_000 / "5,000" / 5e3 / 5000.0 to one value before
matching the trace-evidence corpus."""

from __future__ import annotations

import pytest

from evolution.evolve.rewrite_guards import scan_leakage

_CORPUS = "assert df['daily_total'].sum() == 5000\n"


@pytest.mark.parametrize("variant", ["5000", "5_000", '"5,000"', "5e3", "5000.0"])
def test_numeric_expected_constant_flagged_in_any_form(variant):
    body = f"Always cap the running batch total at {variant} rows for this task.\n"
    findings = scan_leakage(body, _CORPUS)
    assert any("numeric" in f and "5000" in f for f in findings), (variant, findings)


def test_unrelated_number_not_flagged():
    # A number absent from the corpus must not trip the numeric pass.
    body = "Process inputs in chunks of 4096 records at a time.\n"
    assert scan_leakage(body, _CORPUS) == []
