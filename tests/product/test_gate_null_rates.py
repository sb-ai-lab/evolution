"""The promotion gate's true-null behavior, locked as arithmetic."""

from __future__ import annotations

from types import ModuleType

import pytest

from tests.product import gate_null_rates


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    return gate_null_rates


def test_the_rejection_floor_is_one_minus_alpha(tool: ModuleType) -> None:
    """The bound that decides which quantity a published figure is."""
    assert tool.rejection_floor(0.10) == pytest.approx(0.90)
    assert tool.rejection_floor(0.05) == pytest.approx(0.95)


def test_the_plans_three_task_figure_is_below_the_floor(tool: ModuleType) -> None:
    """63.9% cannot be a total rejection rate, and this is why.

    Stated as a test rather than as prose because the misreading it prevents
    already happened once, in this repository, in writing.
    """
    published_at_three_tasks = 0.639

    assert published_at_three_tasks < tool.rejection_floor(tool.GATE_ALPHA)


def test_the_tie_free_closed_form_matches_the_hand_derivation(tool: ModuleType) -> None:
    """``1 - 2**-T``: each task is a coin flip between exchangeable arms."""
    assert tool.any_task_regression(1) == pytest.approx(0.5)
    assert tool.any_task_regression(3) == pytest.approx(0.875)
    assert tool.any_task_regression(6) == pytest.approx(0.984375)
    assert tool.any_task_regression(24) == pytest.approx(1.0, abs=1e-6)


def test_ties_lower_the_regression_rate_rather_than_raising_it(tool: ModuleType) -> None:
    """A tie is not a regression, so ties can only move the rate down."""
    tie_free = tool.any_task_regression(6)
    heavy_ties = tool.any_task_regression(6, tie_probability=0.42)

    assert heavy_ties < tie_free
    assert tool.any_task_regression(6, tie_probability=1.0) == pytest.approx(0.0)


def test_the_regression_rate_rises_with_task_count(tool: ModuleType) -> None:
    """The finding itself: more validation data makes this gate stricter.

    Not better informed -- stricter. The gate rejects when *any* task's candidate
    mean falls below its parent's, and under a null each added task is another
    chance for that to happen by luck alone.
    """
    rates = [tool.any_task_regression(tasks) for tasks in (3, 6, 12, 24)]

    assert rates == sorted(rates)
    assert rates[0] < rates[-1]


def test_the_simulation_agrees_with_the_closed_form(tool: ModuleType) -> None:
    """A small run of the real gate, checked against the algebra.

    The simulation drives ``conservative_paired_gate`` itself, so this is the
    one test here that would notice the gate's rule changing underneath the
    derivation. Ten checks per trial keeps ties rare enough that the measured
    component sits just under the tie-free ceiling.
    """
    row = tool.simulate(
        tasks=6,
        trials=3,
        checks=10,
        rate=0.5,
        replicates=300,
        seed=20260814,
        diagnostic_replicates=0,
    )

    assert row["rejection_rate"] >= tool.rejection_floor(tool.GATE_ALPHA)
    assert row["task_regression_rate"] <= tool.any_task_regression(6)
    assert row["task_regression_rate"] > 0.85


def test_the_diagnostic_can_be_switched_off_without_losing_the_verdict(
    tool: ModuleType,
) -> None:
    """The permutation diagnostic is exact and exponential; it is sampled.

    Running it on every replicate made the 24-task row take longer than the rest
    of the script together, for a number the verdict does not use.
    """
    row = tool.simulate(
        tasks=3,
        trials=3,
        checks=10,
        rate=0.5,
        replicates=50,
        seed=1,
        diagnostic_replicates=0,
    )

    assert row["underpowered_rate"] is None
    assert row["underpowered_sample"] == 0
    assert row["rejection_rate"] > 0
