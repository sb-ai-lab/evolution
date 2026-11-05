"""Genuine stable/all_attempts execution, before H12 public result assembly.

Earlier stored and stored-audit tests own the mode/outcome, removal and recovery
matrices. These runs close the remaining execution gaps: another final comparison
on the retained/promoted baseline, joint full measurements with reduced contrasts,
and the actual gate boundary when every stored case lacks points or bounds.
"""

from __future__ import annotations

import json

import pytest

from tests.campaign_drivers import (
    COUNTING_SCORER,
    EDITED,
    LENIENT,
    MANIFEST_SEED,
    OBSERVE,
    _dispatch_order,
    _events,
    _flag_on_finalists_bound,
    _measurement_events,
    _records,
    _RepeatTagged,
    _statistical,
)
from tests.test_tree_campaign_family14_gate_audit import scorer_tables
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_stored import _ranking_adapter, _ranking_scorer

pytestmark = pytest.mark.slow
STABLE = {"stability": "stable"}


def _assert_stored_finals(run, snapshot, repeats, corpus=("c1", "c2")):
    policy = snapshot.identity["policy"]
    assert policy["gate"]["stability"] == "stable"
    assert policy["round_stop"] == "all_attempts"
    assert policy["baseline_refresh_after_rounds"] == 0
    assert not payloads(run, "baseline_refresh_bound")
    bound = payloads(run, "final_measurement_bound")
    assert all(row["role"] == "finalist" and row["champion_side"] == "stored" for row in bound)
    members = [row["measurement_id"] for row in bound]
    assert members == [
        member for binding in payloads(run, "finalists_bound") for member in binding["members"]
    ]
    assert not list((run / "measurements").glob("*-final-*-champion*"))
    for member in members:
        events = _records(_measurement_events(run, member))
        assert not any(row["event"] == "sample_imported" for row in events)
        scored = [row["payload"] for row in events if row["event"] == "scoring_finished"]
        assert len(scored) == len(corpus) * repeats
        assert {(row["case_id"], int(row["sample_id"])) for row in scored} == {
            (case, repeat) for case in corpus for repeat in range(1, repeats + 1)
        }
    for binding in payloads(run, "finalists_bound"):
        round_number = binding["round"]
        completed = [
            row
            for row in _events(run, "final_measurement_completed")
            if row["payload"]["round"] == round_number
        ]
        compared = [
            row
            for row in _events(run, "global_comparison_completed")
            if row["payload"]["round"] == round_number
        ]
        assert len(completed) == len(binding["members"])
        assert max(row["sequence"] for row in completed) < min(row["sequence"] for row in compared)
    assert_clean_run(run, snapshot)


def _two_round_scorer(monkeypatch, flag, retained):
    """Keep c1 diagnostically failing while each edit improves its scalar score.

    The fixed subprocess program sees a marker only during the first final
    stage. Its critical veto supplies an authoritative retained observe round.
    The hook changes fixture inputs; it never changes an engine decision.
    """
    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook

    def phase(self, name):
        if name == "after_finalists_bound":
            state = self.snapshot()
            if retained and len(state.rounds) == 1:
                flag.touch()
        if name == "after_round_closed":
            flag.unlink(missing_ok=True)
        return original(self, name)

    monkeypatch.setattr(_Campaign, "hook", phase)
    return (
        COUNTING_SCORER.replace(
            '"metrics": {"score": 1.0 if passed else 0.0}',
            '"metrics": {"score": 0.2 * skill.count("edited") '
            'if payload["case_id"] == "c1" else 0.8}',
        )
        .replace('"passed": passed', '"passed": payload["case_id"] != "c1"')
        .replace(
            '"critical_pass": True',
            f'"critical_pass": not (payload["case_id"] == "c2" '
            f"and pathlib.Path({str(flag)!r}).exists())",
        )
    )


@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("retained", [False, True], ids=["promoted", "retained"])
async def test_next_round_compares_against_the_actual_selected_stored_measurement(
    tmp_path, monkeypatch, mode, fresh_parent, retained
):
    config = OBSERVE if mode == "observe" else _statistical(mode)
    config = {
        **config,
        "gate": {**config["gate"], "fresh_parent": fresh_parent},
        "scorer": _two_round_scorer(monkeypatch, tmp_path / "veto.flag", retained),
    }
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, family14_settings=STABLE, campaign={"rounds": 2}
    )
    first, second = payloads(run, "selection_decided")
    baselines = payloads(run, "baseline_complete")
    first_comparison, second_comparison = payloads(run, "global_comparison_completed")
    first_final, second_final = payloads(run, "final_measurement_completed")
    expected_baseline = "m-r00-champion" if retained else first_final["measurement_id"]
    assert first["measurement_id"] == expected_baseline
    assert baselines[1]["measurement_id"] == expected_baseline
    assert second_comparison["champion_measurement_id"] == expected_baseline
    assert second_comparison["evidence"][1] == baselines[1]["evidence"][0]
    second_bound = payloads(run, "final_measurement_bound")[1]
    assert second_bound["stored_champion"] == {
        "measurement_id": expected_baseline,
        "prefix_digest": baselines[1]["evidence"][0]["content_digest"],
    }
    if retained:
        assert first_comparison["reason"] == "critical_failure"
        assert baselines[1]["evidence"] == baselines[0]["evidence"]
    else:
        assert first_comparison["accepted"] is True
        assert second_comparison["point"] == pytest.approx(
            second_final["point"] - first_final["point"]
        )
    assert second_comparison["accepted"] is True
    assert second["measurement_id"] == second_final["measurement_id"]
    assert [row["outcome"] for row in payloads(run, "round_closed")] == [
        "retained" if retained else "promoted",
        "promoted",
    ]
    for selection, completion in zip((first, second), (first_final, second_final), strict=True):
        assert selection["ranking"][0]["score"] == completion["point"]
    ledger = _records(run / "artifacts.jsonl")
    promotions = 1 if retained else 2
    assert sum(row["event"] == "promotion_pending" for row in ledger) == promotions
    assert sum(row["event"] == "promotion_committed" for row in ledger) == promotions
    assert [
        row["measurement_id"]
        for row in payloads(run, "measurement_bound")
        if row["attempt_id"] == "champion"
    ] == ["m-r00-champion"]
    _assert_stored_finals(run, snapshot, config["repeats"])


async def test_joint_final_stage_ranks_full_fresh_scores_and_audits_reduced_contrasts(
    tmp_path, monkeypatch
):
    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    dispatched = _dispatch_order(monkeypatch)
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _ranking_scorer(flag, "reduced")},
        adapter=_ranking_adapter(exclude_c2=True),
        extra_cases={"c3": {"critical": True, "pass": True}},
        family14_settings=STABLE,
        campaign={"attempts_per_round": 2, "indeterminate": LENIENT},
    )
    (binding,) = payloads(run, "finalists_bound")
    assert dispatched[-1] == [
        (member, case, 1) for case in ("c1", "c2", "c3") for member in binding["members"]
    ]
    first, second = payloads(run, "global_comparison_completed")
    (selection,) = payloads(run, "selection_decided")
    assert first["point"] < second["point"]
    assert selection["ranking"][0]["score"] > selection["ranking"][1]["score"]
    assert selection["attempt_id"] == first["attempt_id"]
    assert all(row["removed_cases"] == ["c2"] for row in (first, second))
    assert all(row["removed_pairs"] == [["c2", 1]] for row in (first, second))
    assert (
        sum(row["event"] == "promotion_committed" for row in _records(run / "artifacts.jsonl")) == 1
    )
    _assert_stored_finals(run, snapshot, 1, ("c1", "c2", "c3"))


class _StoredHoles(_RepeatTagged):
    """Exhaust every initial-baseline pair above `keep`, never a fresh side."""

    def __init__(self, *, keep, **kwargs):
        super().__init__(**kwargs)
        self.keep = keep
        self.champions = 0

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion":
            self.champions += 1
            if self.champions == 1:
                instance._lookup = lambda session: (
                    "target_failed"
                    if session.requested_seed - MANIFEST_SEED > self.keep
                    else "completed"
                )
        return instance


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("keep", [0, 1], ids=["no-point-cases", "no-bound-cases"])
async def test_same_horizon_empty_stored_sets_stop_before_finalists(
    tmp_path, monkeypatch, fresh_parent, keep
):
    config = {
        **_statistical("bound"),
        "gate": {
            "fresh_parent": fresh_parent,
            "global": "bound",
            "causal": {"confidence": 0.9, "tolerance": 0.05},
        },
        "scorer": scorer_tables([0] * 4, [0.5] * 4, [0.8] * 4, [0.8] * 4),
    }
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        adapter=_StoredHoles(keep=keep, used=EDITED["used"]),
        family14_settings=STABLE,
        campaign={"indeterminate": LENIENT},
    )
    excluded = payloads(run, "pairs_excluded")
    assert sum(row["count"] for row in excluded) == 2 * (4 - keep)
    assert not payloads(run, "finalists_bound")
    assert not payloads(run, "final_measurement_bound")
    assert not payloads(run, "global_comparison_completed")
    if keep:
        (closed,) = payloads(run, "attempt_closed")
        assert closed["reason"] == "insufficient_repeats"
    assert not any(
        row["event"].startswith("promotion_") for row in _records(run / "artifacts.jsonl")
    )
    _assert_stored_finals(run, snapshot, 4)


async def test_stable_admission_is_public(tmp_path):
    from evolution.tree.evolve import admit_manifest
    from evolution.tree.manifest.manifest_v2 import SCHEMA_V15
    from tests.tree.manifest.test_manifest_v15 import v15_from
    from tests.tree_evolve_support import write_workspace

    path = write_workspace(tmp_path)
    document = v15_from(json.loads(path.read_text()), round_stop="all_attempts", refresh=0)
    assert document["campaign"]["gate"]["stability"] == "stable"
    path.write_text(json.dumps(document))
    manifest = admit_manifest(path)
    assert manifest.schema == SCHEMA_V15
    assert manifest.workflow is not None
    assert manifest.workflow.campaign.stability == "stable"
