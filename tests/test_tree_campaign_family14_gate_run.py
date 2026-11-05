"""H4 gates reach real final measurement and Promotion before H12 assembly.

Only the shared driver's test admission and pre-public-result seam are used.
The scorer subprocesses, comparisons, row audit and lifecycle stay real.
"""

from __future__ import annotations

import pytest

from tests.campaign_drivers import EDITED, VARYING, _measurement_events, _records
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent, scorer_tables
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow
CONSTANT = {"gate": {}, "repeats": 4, **EDITED}


def assert_clean_run(run, snapshot):
    """Both journal-only reduction and independent sibling-row audit must pass."""
    replayed = journal_consistent(run)
    assert replayed.finished == snapshot.finished
    assert replayed.finished["outcome"] == "completed"
    result = audit(run)
    assert result["corpus"] == "proven"
    assert result["mismatches"] == []


def assert_fresh_final(run, snapshot, repeats):
    """Final members have their own full-corpus rows and one Promotion."""
    (binding,) = payloads(run, "finalists_bound")
    assert binding["members"] == ["m-r01-final-01-champion", "m-r01-final-01-a01"]
    for member in binding["members"]:
        assert snapshot.measurements[member]["role"] == "final"
        rows = _records(_measurement_events(run, member))
        assert not any(row["event"] == "sample_imported" for row in rows)
        scored = [row["payload"] for row in rows if row["event"] == "scoring_finished"]
        assert {(row["case_id"], int(row["sample_id"].rsplit("-", 1)[-1])) for row in scored} == {
            (case, repeat) for case in ("c1", "c2") for repeat in range(1, repeats + 1)
        }
        assert len(scored) == 2 * repeats
    ledger = _records(run / "artifacts.jsonl")
    assert sum(row["event"] == "promotion_pending" for row in ledger) == 1
    assert sum(row["event"] == "promotion_committed" for row in ledger) == 1
    assert payloads(run, "selection_decided")[0]["measurement_id"] == binding["members"][1]


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("causal", [False, True], ids=["empty-C", "nonempty-C"])
async def test_exact_gate_reaches_independent_full_final_rows_and_promotion(
    tmp_path, monkeypatch, fresh_parent, causal
):
    # Restoring the family-13 degenerate close would prevent final measurement.
    config = {**CONSTANT, "gate": {"fresh_parent": fresh_parent}}
    if not causal:
        config["used"] = lambda case, turn: ["skills/SKILL.md"] if case == "c1" else []
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, family14_settings={"margin": 0.25}
    )
    (comparison,) = payloads(run, "comparison_bound")
    assert (comparison["margin"], comparison["causal"]) == (0.25, [1] if causal else [])
    looks = payloads(run, "comparison_look_evaluated")
    assert [look["look"] for look in looks] == [3, 4]
    assert (looks[0]["focus"]["point"], looks[0]["focus"]["lower"], looks[0]["focus"]["upper"]) == (
        1,
        1,
        1,
    )
    final = looks[-1]
    assert (final["decision"], final["reason"], final["focus"]["exact"]) == (
        "finalist",
        "gate_passed",
        True,
    )
    assert final["focus"]["upper"] is None
    assert "exact" not in final["overall"]
    assert "exact_contrast" in final["caveats"]
    calibration = payloads(run, "noise_calibration_completed")
    if causal:
        assert len(calibration) == 1
        assert (calibration[0]["degenerate"], calibration[0]["allowance"]) == (True, 0)
        assert final["causal"]["exact"] is True
        assert final["tolerance"] == {"source": "measured_noise", "value": 0}
        assert "noise_not_observed" in final["caveats"]
        assert "noise_allowance_empirical" not in final["caveats"]
    else:
        assert calibration == []
        assert final["causal"] is None
        assert "noise_not_observed" not in final["caveats"]
    assert payloads(run, "attempt_closed")[0]["outcome"] == "finalist"
    assert_fresh_final(run, snapshot, 4)
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize(
    "parent,candidate,margin,accepted",
    [
        (0.4, 0.2, -0.3, True),
        (0.4, 0.2, -0.2, False),
        (0.4, 0.4, -0.1, True),
        (0.4, 0.4, 0, False),
        (0.25, 0.5, -0.1, True),
        (0.25, 0.5, 0, True),
        (0.25, 0.5, 0.2, True),
        (0.25, 0.5, 0.25, False),
        (0.25, 0.5, 0.3, False),
    ],
)
async def test_statistical_exact_focus_uses_strict_bound_margin(
    tmp_path, monkeypatch, parent, candidate, margin, accepted
):
    # A zero-only focus decision or >= at the margin changes these outcomes.
    scorer = scorer_tables([parent] * 4, [candidate] * 4, [0.5] * 4, [0.5] * 4)
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**CONSTANT, **VARYING, "scorer": scorer},
        family14_settings={"margin": margin},
    )
    assert payloads(run, "comparison_bound")[0]["margin"] == margin
    looks = payloads(run, "comparison_look_evaluated")
    look = looks[-1]
    assert look["focus"]["point"] == pytest.approx(candidate - parent)
    assert look["focus"]["exact"] is True
    assert (look["decision"], look["reason"]) == (
        ("finalist", "gate_passed") if accepted else ("rejected", "focus_below_margin")
    )
    assert [look["look"] for look in looks] == ([3, 4] if accepted else [3])
    if accepted:
        assert_fresh_final(run, snapshot, 4)
    else:
        assert not payloads(run, "final_measurement_bound")
        assert not any(
            row["event"].startswith("promotion_") for row in _records(run / "artifacts.jsonl")
        )
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize(
    "candidate,exact,accepted",
    [([0.5] * 4, True, True), ([0.4] * 4, True, False), ([0.4, 0.6, 0.4, 0.6], False, False)],
    ids=["constant-equal", "constant-regression", "varying-zero-point"],
)
async def test_zero_parent_noise_requires_nonnegative_candidate_causal_bound(
    tmp_path, monkeypatch, fresh_parent, candidate, exact, accepted
):
    # Treating no Parent variation as causal acceptance admits the noisy loser.
    scorer = scorer_tables([0] * 4, [1] * 4, [0.5] * 4, candidate)
    config = {**CONSTANT, **VARYING, "scorer": scorer, "gate": {"fresh_parent": fresh_parent}}
    run, snapshot = await family14_run(tmp_path, monkeypatch, config)
    (calibration,) = payloads(run, "noise_calibration_completed")
    assert (calibration["degenerate"], calibration["allowance"]) == (True, 0)
    look = payloads(run, "comparison_look_evaluated")[-1]
    assert look["look"] == 4
    assert look["tolerance"] == {"source": "measured_noise", "value": 0}
    assert look["causal"]["exact"] is exact
    assert (look["decision"], look["reason"]) == (
        ("finalist", "gate_passed") if accepted else ("rejected", "causal_below_tolerance")
    )
    if exact:
        assert look["causal"]["lower"] == look["causal"]["point"]
    else:
        assert look["causal"]["point"] == pytest.approx(0)
        assert look["causal"]["lower"] < 0
    assert "noise_not_observed" in look["caveats"]
    assert "noise_allowance_empirical" not in look["caveats"]
    if accepted:
        assert_fresh_final(run, snapshot, 4)
    else:
        assert not payloads(run, "finalists_bound")
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("fresh_parent", [False, True])
async def test_observe_false_margin_diagnostic_still_enters_final_and_promotes(
    tmp_path, monkeypatch, fresh_parent
):
    # Making would_pass_point a veto would lose this diagnostic-only finalist.
    config = {**CONSTANT, "gate": {"mode": "observe", "fresh_parent": fresh_parent}}
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, family14_settings={"margin": 1}
    )
    look = payloads(run, "comparison_look_evaluated")[-1]
    assert payloads(run, "comparison_bound")[0]["margin"] == 1
    assert (look["focus"]["point"], look["focus"]["exact"], look["would_pass_point"]) == (
        1,
        None,
        False,
    )
    assert (look["decision"], look["reason"]) == ("finalist", "observe_finalist")
    assert "noise_not_observed" in look["caveats"]
    assert "exact_contrast" not in look["caveats"]
    assert_fresh_final(run, snapshot, 4)
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("margin,accepted", [(-0.1, True), (0, True), (0.6, False)])
async def test_nondegenerate_gate_rejects_on_final_lower_bound_above_zero_but_below_margin(
    tmp_path, monkeypatch, margin, accepted
):
    from tests.campaign_drivers import STATISTICAL

    # Checking the positive point instead of the lower bound admits margin .6.
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, STATISTICAL, family14_settings={"margin": margin}
    )
    interim, final = payloads(run, "comparison_look_evaluated")
    assert interim["decision"] == "continue"
    assert interim["focus"]["upper"] > margin
    assert final["focus"]["exact"] is False
    assert final["focus"]["point"] == pytest.approx(0.65)
    assert 0 < final["focus"]["lower"] < 0.6
    assert final["focus"]["upper"] is None
    assert (final["decision"], final["reason"]) == (
        ("finalist", "gate_passed") if accepted else ("rejected", "focus_below_margin")
    )
    if accepted:
        assert_fresh_final(run, snapshot, 4)
    else:
        assert not payloads(run, "finalists_bound")
        assert payloads(run, "attempt_closed")[0]["look"] == 4
    assert_clean_run(run, snapshot)
