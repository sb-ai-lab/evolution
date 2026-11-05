"""H4 row proof over genuine journals; lifecycle consistency is a separate check.

The test admission and pre-public-result seam are owned by `family14_run`.
No estimator, lifecycle or row-audit result is replaced in these tests.
"""

from __future__ import annotations

import json

import pytest

from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
from evolution.tree.storage.envelope import Envelope
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    MANIFEST_SEED,
    OBSERVE,
    STATISTICAL,
    VARYING,
    VARYING_SCORER,
    _measurement_events,
    _records,
    _RepeatTagged,
    _UnseededRepeatTagged,
)
from tests.test_tree_campaign_family14_audit import audit, cut_run, forge
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

CONSTANT = {"gate": {}, "repeats": 3, **EDITED}
MEASURED = {"gate": {}, "repeats": 4, **VARYING}


def journal_consistent(run):
    """Replay the campaign alone; sibling rows are deliberately unread here."""
    life = CampaignLifecycle(selector=None, family_version=14)
    for row in _records(run / "campaign.jsonl"):
        life.accept(Envelope.from_payload(row, family=campaign_family_for(14)))
    return life.snapshot


def row_finding(run, event, detail):
    journal_consistent(run)
    result = audit(run)
    assert result["corpus"] == "proven"
    assert not any(row["code"] == "suffix_refused" for row in result["mismatches"]), result
    assert any(
        row["event"] == event and row["code"] == "precondition_failed" and detail in row["detail"]
        for row in result["mismatches"]
    ), result["mismatches"]


def scorer_tables(focus_parent, focus_candidate, causal_parent, causal_candidate):
    """Use real subprocess scores with repeat tags, including sub-ulp variation."""
    table = {
        ("c1", False): focus_parent,
        ("c1", True): focus_candidate,
        ("c2", False): causal_parent,
        ("c2", True): causal_candidate,
    }
    before, rest = VARYING_SCORER.split("table = ", 1)
    _, after = rest.split("\nscore = ", 1)
    return (before + "table = " + repr(table) + "\nscore = " + after).replace(
        '"passed": score >= 0.5', '"passed": payload["case_id"] == "c2" or score >= 0.5'
    )


@pytest.mark.parametrize("fresh", [False, True])
async def test_degenerate_parent_rows_prove_zero_allowance_and_exact_look(
    tmp_path, monkeypatch, fresh
):
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**CONSTANT, "gate": {"fresh_parent": fresh}},
        family14_settings={"margin": 0.25},
    )
    comparison = payloads(run, "comparison_bound")[0]
    calibration = payloads(run, "noise_calibration_completed")[0]
    look = payloads(run, "comparison_look_evaluated")[0]
    assert comparison["margin"] == 0.25
    assert (calibration["allowance"], calibration["degenerate"], calibration["unavailable"]) == (
        0,
        True,
        None,
    )
    assert look["tolerance"]["value"] == 0
    assert (look["focus"]["exact"], look["causal"]["exact"]) == (True, True)
    assert look["focus"]["lower"] == look["focus"]["point"] == 1
    assert look["focus"]["upper"] is None
    assert "exact" not in look["overall"]
    assert set(look["caveats"]) == {
        "exact_contrast",
        "noise_not_observed",
        "intervals_empirical",
        *([] if fresh else ["causal_parent_not_contemporaneous"]),
    }
    assert (look["decision"], look["reason"]) == ("finalist", "gate_passed")
    journal_consistent(run)
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("scope", ["focus", "causal"])
async def test_well_shaped_false_exact_flag_needs_row_recomputation(tmp_path, monkeypatch, scope):
    run = await cut_run(tmp_path, monkeypatch, "after_comparison_look_evaluated", CONSTANT)
    assert payloads(run, "comparison_look_evaluated")[0][scope]["exact"] is True

    def edit(event, payload):
        if event == "comparison_look_evaluated":
            payload[scope]["exact"] = False
            # The other exact scope still justifies the existing exact_contrast caveat.

    forge(run, edit)
    row_finding(run, "comparison_look_evaluated", f"{scope} exact is True")


@pytest.mark.parametrize("scope", ["focus", "causal"])
async def test_well_shaped_true_exact_flag_cannot_follow_rounded_equal_bounds(
    tmp_path, monkeypatch, scope
):
    # Fraction-level variation survives exact_scope even though float publication
    # rounds its point and lower bound to .5. Equal endpoints cannot prove exactness.
    varied, constant = [0, 1e-18, 0], [0.5] * 3
    scorer = scorer_tables(
        varied if scope == "focus" else [0] * 3,
        constant,
        varied if scope == "causal" else [0] * 3,
        constant,
    )
    config = {**CONSTANT, "adapter_class": _RepeatTagged, "scorer": scorer}
    run = await cut_run(tmp_path, monkeypatch, "after_comparison_look_evaluated", config)
    estimate = payloads(run, "comparison_look_evaluated")[0][scope]
    assert (estimate["point"], estimate["lower"], estimate["upper"], estimate["exact"]) == (
        0.5,
        0.5,
        None,
        False,
    )

    def edit(event, payload):
        if event == "comparison_look_evaluated":
            payload[scope]["exact"] = True

    forge(run, edit)
    row_finding(run, "comparison_look_evaluated", f"{scope} exact is False")


@pytest.mark.parametrize("field,value", [("degenerate", False), ("allowance", 0.125)])
async def test_calibration_claims_require_parent_rows(tmp_path, monkeypatch, field, value):
    run = await cut_run(tmp_path, monkeypatch, "after_noise_calibration_completed", CONSTANT)

    def edit(event, payload):
        if event == "noise_calibration_completed":
            assert payload[field] != value
            payload[field] = value

    forge(run, edit)
    row_finding(run, "noise_calibration_completed", field)


async def test_forged_no_variation_on_varying_parent_requires_row_recomputation(
    tmp_path, monkeypatch
):
    run = await cut_run(tmp_path, monkeypatch, "after_noise_calibration_completed", MEASURED)
    assert payloads(run, "noise_calibration_completed")[0]["degenerate"] is False

    def edit(event, payload):
        if event == "noise_calibration_completed":
            payload["degenerate"] = True

    forge(run, edit)
    row_finding(run, "noise_calibration_completed", "degenerate is False")


@pytest.mark.parametrize(
    "corruption",
    [
        "margin",
        "comparison_binding",
        "tolerance",
        "numeric_tolerance",
        "point_pass",
        "missing_exact_caveat",
        "extra_exact_caveat",
        "extra_empirical_caveat",
        "missing_empirical_caveat",
        "extra_noise_caveat",
        "missing_noise_caveat",
    ],
)
async def test_journal_consistency_refuses_h4_contract_corruptions(
    tmp_path, monkeypatch, corruption
):
    config = {**OBSERVE, "repeats": 3} if corruption == "point_pass" else CONSTANT
    if corruption in {"extra_exact_caveat", "missing_empirical_caveat", "extra_noise_caveat"}:
        config = {**MEASURED, "repeats": 3}
    elif corruption == "numeric_tolerance":
        config = {**CONSTANT, "gate": {"causal": {"confidence": 0.9, "tolerance": 0.05}}}
    run = await cut_run(
        tmp_path,
        monkeypatch,
        "after_comparison_look_evaluated",
        config,
        family14_settings={"margin": 0.25},
    )

    def edit(event, payload):
        if event == "comparison_bound" and corruption == "margin":
            payload["margin"] = 0
        if event != "comparison_look_evaluated":
            return
        if corruption == "comparison_binding":
            payload["comparison"] += 1
        elif corruption in {"tolerance", "numeric_tolerance"}:
            payload["tolerance"]["value"] = 0.125
        elif corruption == "point_pass":
            assert payload["would_pass_point"] is True
            payload["would_pass_point"] = False
        elif corruption == "missing_exact_caveat":
            payload["caveats"].remove("exact_contrast")
        elif corruption == "extra_exact_caveat":
            payload["caveats"].append("exact_contrast")
            payload["caveats"].sort()
        elif corruption == "extra_empirical_caveat":
            payload["caveats"].append("noise_allowance_empirical")
            payload["caveats"].sort()
        elif corruption == "missing_noise_caveat":
            payload["caveats"].remove("noise_not_observed")
        elif corruption == "missing_empirical_caveat":
            payload["caveats"].remove("noise_allowance_empirical")
        elif corruption == "extra_noise_caveat":
            payload["caveats"].append("noise_not_observed")
            payload["caveats"].sort()

    forge(run, edit)
    result = audit(run)["mismatches"]
    assert any(row["code"] == "suffix_refused" for row in result), result
    # A lifecycle refusal is not claimed as an independent numerical row finding.
    assert not [row for row in result if row["event"] == "comparison_look_evaluated"]


async def test_consistent_observe_point_pass_forgery_requires_row_recomputation(
    tmp_path, monkeypatch
):
    run = await cut_run(
        tmp_path,
        monkeypatch,
        "after_comparison_look_evaluated",
        OBSERVE,
        family14_settings={"margin": 0.25},
    )
    look = payloads(run, "comparison_look_evaluated")[0]
    assert (look["focus"]["point"], look["would_pass_point"]) == (1, True)

    def edit(event, payload):
        if event == "comparison_look_evaluated":
            # Both fields agree with the frozen margin in the journal, but the
            # sibling rows prove the focus point was 1 and the diagnostic true.
            payload["focus"]["point"] = 0.125
            payload["would_pass_point"] = False

    forge(run, edit)
    row_finding(run, "comparison_look_evaluated", "focus point is 1.0")


@pytest.mark.parametrize(
    "adapter_class,exact", [(_RepeatTagged, True), (_UnseededRepeatTagged, False)]
)
async def test_constant_paired_differences_need_provenance_not_matching_scores(
    tmp_path, monkeypatch, adapter_class, exact
):
    scorer = scorer_tables(
        [0, 0.125, 0.25], [0.5, 0.625, 0.75], [0.5, 0.625, 0.75], [0.5, 0.625, 0.75]
    )
    run, _ = await family14_run(
        tmp_path, monkeypatch, {**CONSTANT, "adapter_class": adapter_class, "scorer": scorer}
    )
    look = payloads(run, "comparison_look_evaluated")[0]
    assert (look["focus"]["point"], look["causal"]["point"]) == (0.5, 0)
    assert (look["focus"]["exact"], look["causal"]["exact"]) == (exact, exact)
    # In particular C has identical varying score multisets on both sides;
    # only actual seed/provenance proof permits paired constant differences.
    candidate = payloads(run, "comparison_bound")[0]["candidate_measurement_id"]
    created = _records(_measurement_events(run, candidate))[0]["payload"]
    assert created["identity"]["target"]["seed_mode"] == ("exact" if exact else "none")
    journal_consistent(run)
    assert audit(run)["mismatches"] == []


class _ExcludeLastPair(_RepeatTagged):
    """Only the baseline exhausts c1/04; its exclusion must leave both gate sides."""

    champions = 0

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion":
            self.champions += 1
            if self.champions == 1:
                instance._lookup = lambda session: (
                    "target_failed"
                    if session.case_id == "c1" and session.requested_seed - MANIFEST_SEED == 4
                    else "completed"
                )
        return instance


async def test_exclusion_removes_sole_varying_pair_from_both_cited_sides(tmp_path, monkeypatch):
    scorer = scorer_tables([0, 0, 0, 0.25], [0.5, 0.5, 0.5, 1], [1] * 4, [1] * 4)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**CONSTANT, "repeats": 4, "adapter_class": _ExcludeLastPair, "scorer": scorer},
        campaign={"indeterminate": LENIENT},
    )
    excluded = payloads(run, "pairs_excluded")[0]["pairs"]
    assert [(row["case_id"], row["sample_id"]) for row in excluded] == [("c1", "04")]
    final = payloads(run, "comparison_look_evaluated")[-1]
    assert (final["focus"]["point"], final["focus"]["lower"], final["focus"]["exact"]) == (
        0.5,
        0.5,
        True,
    )
    # Removing just one side gives .625 or .4375; keeping both gives .5625.
    # Only excluding this varying pair on both sides gives the exact .5 contrast.
    comparison = payloads(run, "comparison_bound")[0]
    for mid in (comparison["candidate_measurement_id"], comparison["parent_measurement_id"]):
        scores = [
            row["payload"]["metrics"]["score"]
            for row in _records(_measurement_events(run, mid))
            if row["event"] == "scoring_finished" and row["payload"]["case_id"] == "c1"
        ]
        assert len(set(scores)) == 2
    journal_consistent(run)
    assert audit(run)["mismatches"] == []


async def test_valid_later_rows_do_not_change_historical_cited_look(tmp_path, monkeypatch):
    run = await cut_run(tmp_path, monkeypatch, "after_comparison_look_evaluated", STATISTICAL)
    historical = payloads(run, "comparison_look_evaluated")[0]
    assert historical["final"] is False
    assert audit(run)["mismatches"] == []
    heads = {}
    for reference in historical["evidence"]:
        name, extent = reference["locator"].split("#bytes=")
        end = int(extent.split(":")[1])
        heads[name] = (end, (run / name).read_bytes()[:end])
    await family14_run(tmp_path, monkeypatch, STATISTICAL)
    assert payloads(run, "comparison_look_evaluated")[0] == historical
    for name, (end, original) in heads.items():
        raw = (run / name).read_bytes()
        assert raw[:end] == original and len(raw) > end
        tail = [json.loads(line) for line in raw[end:].splitlines()]
        assert any(row["event"] == "scoring_finished" for row in tail)
    journal_consistent(run)
    assert audit(run)["mismatches"] == []
