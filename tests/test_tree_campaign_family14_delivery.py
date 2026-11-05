"""H3 integration evidence for unstable/all_attempts, margin 0 and refresh 0.

These runs stop before H12 public assembly. They exercise the existing
observe gate or nondegenerate statistical gate with numeric tolerance;
exact gate decisions, margins and zero-noise calibration remain H4.
"""

from __future__ import annotations

import re

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.campaign_schema import SchemaError, campaign_family_for
from evolution.tree.execution.corpus import load_reflection_corpus
from tests.campaign_drivers import (
    DECLINING_EDITOR,
    OBSERVE,
    TWO_ATTEMPTS,
    VARYING_SCORER,
    _cohort_scorer,
    _events,
    _flag_on_finalists_bound,
    _measurement_events,
    _records,
    _statistical,
    observe_barrier,  # noqa: F401
)
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811


def _config(mode, fresh_parent):
    config = OBSERVE if mode == "observe" else _statistical(mode)
    return {**config, "gate": {**config["gate"], "fresh_parent": fresh_parent}}


def _assert_boundary(run, snapshot):
    policy = snapshot.identity["policy"]
    assert policy["gate"]["stability"] == "unstable"
    assert policy["round_stop"] == "all_attempts"
    assert policy["baseline_refresh_after_rounds"] == 0
    if policy["gate"]["mode"] == "statistical":
        assert policy["gate"]["focus"]["margin"] == 0
        assert policy["gate"]["causal"]["tolerance"] == 0.05
    assert snapshot.family_version == 14
    assert snapshot.finished["outcome"] == "completed"
    assert not payloads(run, "cohort_measurement_bound")
    assert not payloads(run, "cohort_measurement_completed")
    assert all(p["outcome"] != "accepted" for p in payloads(run, "attempt_closed"))
    result = verify_evidence(run, load_reflection_corpus(run.parent / "corpus.jsonl").cases)
    assert result["corpus"] == "proven"
    assert result["mismatches"] == []


@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("finalists", [1, 2])
async def test_h3_final_grammar_and_promotion_audit_across_supported_choices(
    tmp_path, monkeypatch, mode, fresh_parent, finalists
):
    """A bounded H3 cell, with observe restricted to its required global off."""
    campaign = {"rounds": 2, **(TWO_ATTEMPTS if finalists == 2 else {})}
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, _config(mode, fresh_parent), campaign=campaign
    )
    _assert_boundary(run, snapshot)
    comparisons = payloads(run, "comparison_bound")
    assert len(comparisons) == finalists
    for comparison in comparisons:
        assert comparison["parent_source"] == ("fresh_parent" if fresh_parent else "baseline")
        assert (comparison["parent_measurement_id"] is not None) is fresh_parent
    assert len(payloads(run, "parent_measurement_bound")) == (finalists if fresh_parent else 0)

    (binding,) = payloads(run, "finalists_bound")
    members = ["m-r01-final-01-champion"] + [
        f"m-r01-final-01-a{index:02d}" for index in range(1, finalists + 1)
    ]
    assert binding["members"] == members
    (final,) = snapshot.rounds[1].finals
    assert list(final["members"]) == members
    assert snapshot.rounds[2].finals == ()
    declarations = payloads(run, "final_measurement_bound")
    assert [p["measurement_id"] for p in declarations] == members
    for declaration in declarations:
        assert (
            declaration["stability"],
            declaration["round_stop"],
            declaration["champion_side"],
        ) == ("unstable", "all_attempts", "fresh")
        assert "stored_champion" not in declaration
    completions = _events(run, "final_measurement_completed")
    globals_ = _events(run, "global_comparison_completed")
    assert len(completions) == len(members)
    assert len(globals_) == finalists
    assert all(r["payload"]["complete"] for r in completions)
    assert max(r["sequence"] for r in completions) < min(r["sequence"] for r in globals_)
    expected_reason = {
        "observe": "global_off",
        "off": "global_off",
        "point": "global_point_improved",
        "bound": "global_bound_positive",
    }[mode]
    for record in globals_:
        comparison = record["payload"]
        assert "eligible" not in comparison
        assert (comparison["accepted"], comparison["reason"]) == (True, expected_reason)
        assert comparison["exact"] is (None if mode == "observe" else False)
        assert comparison["champion_side"] == "fresh"
        assert comparison["removed_pairs"] == comparison["removed_cases"] == []
        assert (comparison["lower"] is None) is (mode == "observe")

    selected = payloads(run, "selection_decided")[0]
    assert selected["reason"] == "finalist_selected"
    assert selected["measurement_id"] == members[1]
    assert len(selected["ranking"]) == finalists
    assert all(r["accepted"] and "eligible" not in r for r in selected["ranking"])
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == members[1]
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["promoted", "clean"]
    assert [p["attempts_opened"] for p in payloads(run, "round_closed")] == [finalists, 0]
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_pending" for r in ledger) == 1
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    for member in members:
        assert snapshot.measurements[member]["role"] == "final"
        rows = _records(_measurement_events(run, member))
        assert not any(r["event"] == "sample_imported" for r in rows)
        assert sum(r["event"] == "scoring_finished" for r in rows) == (
            2 if mode == "observe" else 8
        )


@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
async def test_h3_complete_critical_finalist_retains_fresh_champion_and_audits(
    tmp_path, monkeypatch, mode
):
    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    config = {**_config(mode, False), "scorer": _cohort_scorer(flag, critical=True)}
    run, snapshot = await family14_run(tmp_path, monkeypatch, config, campaign={"rounds": 2})
    _assert_boundary(run, snapshot)
    comparison = payloads(run, "global_comparison_completed")[0]
    assert (comparison["accepted"], comparison["reason"]) == (False, "critical_failure")
    assert comparison["point"] is not None
    assert all(p["complete"] for p in payloads(run, "final_measurement_completed"))
    selection = payloads(run, "selection_decided")[0]
    assert (selection["reason"], selection["measurement_id"]) == (
        "no_accepted_finalist",
        "m-r01-final-01-champion",
    )
    assert selection["ranking"][0]["accepted"] is False
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == selection["measurement_id"]
    assert not any(r["event"].startswith("promotion_") for r in _records(run / "artifacts.jsonl"))


@pytest.mark.parametrize("fresh_parent", [False, True])
async def test_h3_no_finalists_closes_without_final_binding_or_promotion(
    tmp_path, monkeypatch, fresh_parent
):
    scorer = VARYING_SCORER.replace(
        '("c1", True): [0.6, 0.9, 0.8, 1.0]', '("c1", True): [0.01, 0.02, 0.03, 0.04]'
    )
    assert scorer != VARYING_SCORER
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**_config("bound", fresh_parent), "scorer": scorer},
        campaign=TWO_ATTEMPTS,
    )
    _assert_boundary(run, snapshot)
    assert not payloads(run, "finalists_bound")
    assert not payloads(run, "final_measurement_bound")
    assert snapshot.rounds[1].finals == ()
    assert [p["outcome"] for p in payloads(run, "attempt_closed")] == ["rejected", "rejected"]
    (selection,) = payloads(run, "selection_decided")
    assert (selection["reason"], selection["measurement_id"], selection["ranking"]) == (
        "no_finalists",
        None,
        [],
    )
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == ("stalled", 2)
    assert not any(r["event"].startswith("promotion_") for r in _records(run / "artifacts.jsonl"))


async def test_h3_unassessed_observe_round_keeps_existing_baseline(tmp_path, monkeypatch):
    from functools import partial

    from tests import campaign_drivers as fixtures
    from tests.tree_evolve_support import write_workspace

    monkeypatch.setattr(
        fixtures, "write_workspace", partial(write_workspace, editor_script=DECLINING_EDITOR)
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, campaign={"rounds": 2})
    _assert_boundary(run, snapshot)
    assert not payloads(run, "comparison_look_evaluated")
    assert not payloads(run, "finalists_bound")
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["unscored", "unscored"]
    assert all(p["reason"] == "no_finalists" for p in payloads(run, "selection_decided"))
    assert [p["measurement_id"] for p in payloads(run, "baseline_bound")] == [
        "m-r00-champion",
        "m-r00-champion",
    ]


async def test_the_real_gates_admit_15(tmp_path):
    from evolution.tree import campaign, evolve
    from evolution.tree.campaign_schema import FINAL_CAMPAIGN_FAMILY_VERSION
    from evolution.tree.manifest.manifest_v2 import SCHEMA_V15, SCHEMA_V16

    assert evolve.CAMPAIGN_SCHEMAS == (SCHEMA_V15, SCHEMA_V16)
    assert campaign._ACTIVE_ADAPTIVE_CAMPAIGN_FAMILY_VERSION == 15
    assert FINAL_CAMPAIGN_FAMILY_VERSION == 14


@pytest.mark.parametrize("family,noun", [(14, "final")])
@pytest.mark.parametrize("defect", ["oversized", "owner", "retry", "digest"])
def test_h3_invalid_gap_facts_report_the_recorded_family(family, noun, defect):
    from evolution.tree.confidence_cohort import cohort_rows_evidence

    row = dict(
        measurement_id="m-member", case_id="c1", sample_id="01", retry_ordinal=0, owner="target"
    )
    evidence = cohort_rows_evidence([row], family_version=family)
    if defect == "oversized":
        evidence.update(count=257, rows=[row] * 257)
        expected = f"{noun}_rows lists at most 256 rows"
    elif defect == "owner":
        evidence["rows"][0]["owner"] = "invalid"
        expected = f"a {noun} row's owner is not a threshold owner"
    elif defect == "retry":
        evidence["rows"][0]["retry_ordinal"] = -1
        expected = f"a {noun} row's retry_ordinal is a non-negative integer"
    else:
        evidence["rows_digest"] = "f" * 64
        expected = f"the {noun} rows digest covers its rows"
    payload = dict(reason=f"{noun}_incomplete", owner="target", evidence=evidence)
    with pytest.raises(SchemaError, match=f"^{re.escape(expected)}$"):
        campaign_family_for(family).validate_payload("campaign_suspended", payload)


@pytest.mark.parametrize("family,noun", [(14, "final")])
@pytest.mark.parametrize("defect", ["champion", "binding_order"])
def test_h3_invalid_final_binding_reports_the_recorded_family(
    observe_barrier, family, noun, defect
):
    from tests import test_tree_campaign_family14_final as new
    from tests.test_tree_campaign_family14 import _closed_life

    life = _closed_life(observe_barrier)
    payload = new._finalists(life)
    if defect == "champion":
        payload["champion"]["digest"] = "f" * 64
        event, expected = "finalists_bound", f"the {noun} binds the incumbent Champion"
    else:
        life.accept("finalists_bound", **payload)
        payload = new._member(life, 1)
        event, expected = f"{noun}_measurement_bound", f"{noun} members bind in order"
    with pytest.raises(SchemaError, match=f"^{re.escape(expected)}$"):
        life.accept(event, **payload)


@pytest.mark.parametrize("family,noun", [(14, "final")])
async def test_h3_forged_selection_audit_names_the_recorded_family(
    tmp_path, monkeypatch, family, noun
):
    from tests.test_tree_campaign_gate_audit import _rewrite_campaign

    run, _ = await family14_run(tmp_path, monkeypatch, campaign=TWO_ATTEMPTS)

    def edit(event, payload):
        if event == "selection_decided":
            payload["evidence"][-2:] = reversed(payload["evidence"][-2:])

    _rewrite_campaign(run, edit, family=family)
    findings = verify_evidence(run)["mismatches"]
    # H6 authenticates final selection citations during lifecycle replay.
    event, code = None, "suffix_refused"
    expected = "the selection cites every completion and every comparison in order"
    assert any(
        f["event"] == event and f["code"] == code and f["detail"] == expected for f in findings
    ), findings


@pytest.mark.parametrize("family,noun", [(14, "final")])
async def test_h3_executor_refuses_completed_member_with_missing_case_in_its_family(
    tmp_path, monkeypatch, family, noun
):
    from evolution.tree.campaign import CampaignInvariantViolated, _Campaign
    from evolution.tree.campaign_measurement import Measurement
    from tests.campaign_drivers import _selection_run

    hook = _Campaign.hook
    sample_rows = Measurement.sample_rows
    comparing = False

    def before_comparison(self, name):
        nonlocal comparing
        if name == "before_global_comparison":
            comparing = True
        return hook(self, name)

    def missing_case(self, repeats):
        rows = sample_rows(self, repeats)
        if comparing and self.measurement_id == f"m-r01-{noun}-{'01-' if family == 14 else ''}a01":
            return tuple(row for row in rows if row.case_id != "c2")
        return rows

    monkeypatch.setattr(_Campaign, "hook", before_comparison)
    monkeypatch.setattr(Measurement, "sample_rows", missing_case)
    driver = _selection_run if family == 13 else family14_run
    with pytest.raises(
        CampaignInvariantViolated, match=f"^a completed {noun} member lacks coverage$"
    ):
        await driver(tmp_path, monkeypatch)
    run = tmp_path / "run"
    assert len(payloads(run, f"{noun}_measurement_completed")) == 2
    assert not payloads(run, "global_comparison_completed")
    assert not payloads(run, "selection_decided")
