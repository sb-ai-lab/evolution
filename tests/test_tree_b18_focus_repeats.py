"""b18: `/16` `evaluation.focus_repeats_per_question` measures focus cases at R_focus."""

from __future__ import annotations

import json

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import exit_code_for
from tests.campaign_drivers import _records
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


def _focus_repeats(document, value=3):
    document["campaign"]["evaluation"]["focus_repeats_per_question"] = value


def _schedules(run):
    """Measurement id -> {case_id: max repeat} from each journal's identity."""
    out = {}
    for path in (run / "measurements").glob("*/events.jsonl"):
        first = _records(path)[0]
        schedule = first["payload"]["identity"]["schedule"]
        counts: dict[str, int] = {}
        for row in schedule:
            counts[row["case_id"]] = max(counts.get(row["case_id"], 0), row["repeat"])
        out[path.parent.name] = counts
    return out


async def test_focus_cases_measured_at_focus_repeats_on_both_sides(tmp_path, monkeypatch):
    run, result = await public_run(
        tmp_path, monkeypatch, gate={"fresh_parent": True}, configure=_focus_repeats
    )
    assert result.terminal == "finished" and exit_code_for(result) == 0, (result.code, result.owner)
    assert verify_evidence(run)["mismatches"] == []
    schedules = _schedules(run)
    # c1 is the claimed focus case, c2 the causal one (two_case_run).
    assert schedules["m-r01-a01"] == {"c1": 3, "c2": 1}
    assert schedules["m-r01-a01-parent"] == {"c1": 3, "c2": 1}
    assert schedules["m-r01-a01-completion"] == {"c1": 1, "c2": 1}
    assert schedules["m-r00-champion"] == {"c1": 1, "c2": 1}
    bodies = [
        json.loads(path.read_bytes())
        for path in (run / "artifacts/auxiliary").glob("*/payload.json")
    ]
    profiles = [body for body in bodies if body.get("kind") == "binding"]
    assert profiles
    for profile in profiles:
        by_name = {scope["name"]: scope for scope in profile["scopes"]}
        assert [c["repeats"] for c in by_name["focus"]["cases"]] == [[1, 2, 3]]
        assert [c["repeats"] for c in by_name["causal"]["cases"]] == [[1]]


async def test_without_the_key_schedules_keep_one_repeat(tmp_path, monkeypatch):
    run, result = await public_run(tmp_path, monkeypatch, gate={"fresh_parent": True})
    assert result.terminal == "finished"
    assert all(set(c.values()) == {1} for c in _schedules(run).values())


async def test_focus_first_stages_keep_focus_repeats(tmp_path, monkeypatch):
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        gate={"fresh_parent": True, "stages": "focus_first"},
        configure=_focus_repeats,
    )
    assert result.terminal == "finished", (result.code, result.owner)
    assert verify_evidence(run)["mismatches"] == []
    schedules = _schedules(run)
    assert schedules["m-r01-a01"] == {"c1": 3, "c2": 1}
    assert schedules["m-r01-a01-parent"] == {"c1": 3, "c2": 1}


@pytest.mark.parametrize(
    "cut",
    [
        "after_measurement_bound",
        "after_parent_measurement_bound",
        "after_comparison_bound",
        "after_candidate_gate_sealed",
        "after_candidate_completion_bound",
        "after_candidate_completion_completed",
    ],
)
async def test_focus_repeats_cut_restart_matches_uncut(tmp_path, monkeypatch, cut):
    from tests.test_tree_campaign_family15_delivery import PowerCut, durable_facts

    gate = {"fresh_parent": True}
    control, _ = await public_run(
        tmp_path / "control", monkeypatch, gate=gate, configure=_focus_repeats
    )
    with pytest.raises(PowerCut):
        await public_run(
            tmp_path / "cut", monkeypatch, gate=gate, cut=cut, configure=_focus_repeats
        )
    run, result = await public_run(
        tmp_path / "cut", monkeypatch, gate=gate, configure=_focus_repeats
    )
    assert result.terminal == "finished", (result.code, result.owner)
    assert durable_facts(run) == durable_facts(control)
    assert verify_evidence(run)["mismatches"] == []


def _repeats(r, rf):
    def configure(document):
        document["campaign"]["evaluation"]["repeats_per_question"] = r
        document["campaign"]["evaluation"]["focus_repeats_per_question"] = rf

    return configure


async def test_focus_repeats_above_a_larger_r(tmp_path, monkeypatch):
    run, result = await public_run(
        tmp_path, monkeypatch, gate={"fresh_parent": True}, configure=_repeats(2, 3)
    )
    assert result.terminal == "finished", (result.code, result.owner)
    assert verify_evidence(run)["mismatches"] == []
    assert _schedules(run)["m-r01-a01"] == {"c1": 3, "c2": 2}


async def test_attempt_with_only_focus_cases_measures_them_at_focus_repeats(tmp_path, monkeypatch):
    # Only c1 reads the edited file, so the Attempt has no causal case.
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        gate={"fresh_parent": True},
        configure=_focus_repeats,
        used=lambda case_id, turn: ["skills/SKILL.md"] if case_id == "c1" else [],
    )
    assert result.terminal == "finished", (result.code, result.owner)
    assert verify_evidence(run)["mismatches"] == []
    schedules = _schedules(run)
    assert schedules["m-r01-a01"] == {"c1": 3}
    assert schedules["m-r01-a01-parent"] == {"c1": 3}
