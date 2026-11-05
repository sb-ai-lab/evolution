"""H12c Task 6: the public family-14 matrix (§15.1), #160's reports (§15.2)
and the refresh reports (§15.12).

Every run here goes through H12b's public driver
(`tests.family14_public.family14_public_run`, H12c plan choice 9), so the
assertions read the documents an operator receives -- `CampaignResult.result`
and `CampaignResult.status` -- and each run's evidence audit. The cell
recipes are imported, never copied: the H6 acceptance cells' configurations
(`_genuine_config` in the H12b report module builds them from
`WORSE_SCORER`, `_cohort_scorer`, `_flag_on_finalists_bound` and
`_statistical`), H6's `scenario` for #160, and H7's `NO_FINALISTS` for the
refresh. Admission is the product's own (`/15` is open); the driver's
`family14_admitted` only marks the connection probe.
"""

from __future__ import annotations

from typing import Any

import pytest

from evolution.tree.campaign import inspect_campaign
from tests.campaign_drivers import audit_mismatches
from tests.family14_public import family14_public_run
from tests.test_tree_campaign_family14_reports import _documents, _genuine_config
from tests.test_tree_campaign_family14_run import payloads

pytestmark = pytest.mark.slow

_ROUND_OUTCOME = {
    "winner": "promoted",
    "no-accepted-finalist": "retained",
    "no-finalist": "stalled",
}


def _kinds(document: dict[str, Any]) -> list[str]:
    return [item["kind"] for item in document["caveats"]]


# -- §15.1: stability x round_stop x fresh_parent x mode x outcome (48 cells) -------------


@pytest.mark.parametrize("outcome", ["winner", "no-accepted-finalist", "no-finalist"])
@pytest.mark.parametrize("mode", ["observe", "statistical"])
@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("round_stop", ["all_attempts", "first_accepted"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_the_public_family14_matrix(
    tmp_path, monkeypatch, stability, round_stop, fresh_parent, mode, outcome
):
    """§15.1: each cell's public documents say its settings, outcome, sides and caveats.

    `statistical` is `_statistical("point")`; the outcome recipes are H6's
    acceptance cells (`attempts_per_round: 3` for a `first_accepted` winner,
    else 1; one Attempt at a time). `baseline_refresh_after_rounds` is 0.
    """

    config = _genuine_config(
        tmp_path, monkeypatch, "observe" if mode == "observe" else "point", outcome
    )
    config["gate"] = {**config["gate"], "fresh_parent": fresh_parent}
    run, result = await family14_public_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"stability": stability, "round_stop": round_stop},
        campaign={
            "attempts_per_round": (
                3 if round_stop == "first_accepted" and outcome == "winner" else 1
            ),
            "parallel_attempts": 1,
        },
    )
    (created,) = payloads(run, "campaign_created")
    assert created["identity"]["policy"]["gate"]["fresh_parent"] is fresh_parent
    for payload in _documents(result):
        assert payload["settings"]["stability"] == stability
        assert payload["settings"]["round_stop"] == round_stop
        assert payload["settings"]["baseline_refresh"] == "off"  # refresh 0 in these cells
        assert set(payload["roles"]) == {"grouper", "reflector", "editor"}
        (round1,) = payload["rounds"]
        assert round1["outcome"] == _ROUND_OUTCOME[outcome]
        kinds = _kinds(payload)
        assert ("champion_not_contemporaneous" in kinds) == (stability == "stable")
        assert ("observe_gate_not_enforced" in kinds) == (mode == "observe")
        if outcome == "no-finalist":
            assert round1["final"] == []
            continue
        assert all(entry["stability"] == stability for entry in round1["final"])
        sides = {entry["champion_side"] for entry in round1["final"]}
        assert sides == {"stored" if stability == "stable" else "fresh"}
        if outcome == "no-accepted-finalist":
            # ADR-0032 "Round barrier": `retained` in both stabilities; the
            # next baseline is the stored measurement under `stable` and the
            # latest fresh Champion measurement under `unstable`.
            last = round1["final"][-1]
            if stability == "stable":
                expected = last["stored_measurement_id"]
                assert expected == "m-r00-champion"
            else:
                (champion,) = [m for m in last["members"] if m["role"] == "champion"]
                expected = champion["measurement_id"]
            assert last["next_baseline_measurement_id"] == expected
    assert not payloads(run, "baseline_refresh_bound")
    assert audit_mismatches(run) == []


# -- §15.2: #160's reports -----------------------------------------------------------------


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_issue160_reports_a_stored_final_without_a_champion_member(
    tmp_path, monkeypatch, stability
):
    """#160 verbatim: `stable`, `first_accepted`, `fresh_parent: false`, one
    Attempt at a time on a one-instance target, two rounds.

    H6's `scenario(losers=1)` makes round 1's final lose (`retained`) and
    round 2's win (`promoted`), so each round has exactly one final entry.
    Under `stable` it shows the stored side and no Champion member, and the
    campaign carries `champion_not_contemporaneous`; the `unstable` twin
    measures a Champion member and carries no such caveat. The twin needs a
    second instance (its final loads the Champion beside the finalist, so a
    one-instance target is refused `capabilities_insufficient` at admission).
    """

    from tests.campaign_drivers import EDITED
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_campaign_family14_first_accepted_run import scenario

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1)
    options["config"]["gate"] = {"mode": "observe", "fresh_parent": False}
    capacity = 1 if stability == "stable" else 2
    options["adapter"] = adapter = FakeAdapter(
        max_instances=capacity, max_concurrent_sessions=1, used=EDITED["used"]
    )
    options["campaign"].update(
        attempts_per_round=1,
        max_concurrency=1,
        attempt_feedback="sequential",
        rounds=2,
    )
    run, result = await family14_public_run(tmp_path, monkeypatch, **options)
    assert adapter.peak_live == capacity
    stable = stability == "stable"
    for payload in _documents(result):
        assert (payload["settings"]["stability"], payload["settings"]["round_stop"]) == (
            stability,
            "first_accepted",
        )
        assert ("champion_not_contemporaneous" in _kinds(payload)) is stable
        assert [entry["outcome"] for entry in payload["rounds"]] == ["retained", "promoted"]
        for entry in payload["rounds"]:
            (final,) = entry["final"]
            roles = [member["role"] for member in final["members"]]
            if stable:
                assert final["champion_side"] == "stored"
                assert final["stored_measurement_id"] == "m-r00-champion"
                assert "champion" not in roles
            else:
                assert final["champion_side"] == "fresh"
                assert roles.count("champion") == 1
    # The status validates (its projection round-trips in `_documents`) and
    # matches the verified inspection.
    from evolution.tree.final_report import validate_final_entries

    status = result.status.to_payload()
    for entry in status["rounds"]:
        validate_final_entries(entry["final"])
    inspected = inspect_campaign(run, verify=True).to_payload()
    for name in ("settings", "rounds", "caveats"):
        assert inspected[name] == status[name]
    assert audit_mismatches(run) == []


# -- §15.12: the refresh reports -----------------------------------------------------------


async def test_a_refresh_after_two_stalled_rounds_is_reported(tmp_path, monkeypatch):
    """`refresh=2`, `stable`, `first_accepted`, three rounds with no Promotion.

    Rounds 1 and 2 close on `m-r00-champion`, so round 3 starts with a
    refresh; once round 3 closes, one round has closed on the refresh
    measurement (H12b choice 10).
    """

    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    run, result = await family14_public_run(
        tmp_path,
        monkeypatch,
        NO_FINALISTS,
        family14_settings={"refresh": 2, "stability": "stable", "round_stop": "first_accepted"},
        campaign={"rounds": 3},
    )
    (bound,) = payloads(run, "baseline_refresh_bound")
    refresh_id = bound["measurement_id"]
    expected = {
        "every_rounds": 2,
        "count": 1,
        "performed": [
            {
                "round": 3,
                "measurement_id": refresh_id,
                "replaced_measurement_id": "m-r00-champion",
                "completed": True,
            }
        ],
    }
    for payload in _documents(result):
        assert [entry["outcome"] for entry in payload["rounds"]] == ["stalled"] * 3
        assert payload["settings"]["baseline_refresh"] == expected
    inspected = inspect_campaign(run, verify=True).to_payload()
    assert inspected["settings"]["baseline_refresh"] == expected
    assert inspected["verification"]["mismatches"] == []


async def test_a_run_without_refresh_reports_off_and_binds_none(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family14_refresh_run import NO_FINALISTS

    run, result = await family14_public_run(
        tmp_path,
        monkeypatch,
        NO_FINALISTS,
        family14_settings={"refresh": 0, "stability": "stable", "round_stop": "first_accepted"},
        campaign={"rounds": 3},
    )
    for payload in _documents(result):
        assert payload["settings"]["baseline_refresh"] == "off"
    assert not payloads(run, "baseline_refresh_bound")
    assert audit_mismatches(run) == []


# -- `stored_pairs_excluded` from a genuine `stable` run (H12b left it unreached) ----------


async def test_a_genuine_stable_run_reports_its_stored_pairs_excluded(tmp_path, monkeypatch):
    """H6's repeated-stable-finals recipe, run to its end through the public driver.

    The stored Champion measurement misses `c2` (its first Champion instance
    fails `c2`), so each of the three stored global contrasts removes the
    pair `(c2, 1)`; the public documents carry one `stored_pairs_excluded`
    item per contrast, citing the stored measurement.
    """

    from tests.campaign_drivers import LENIENT
    from tests.test_tree_campaign_family14_first_accepted_run import scenario
    from tests.test_tree_campaign_family14_stored import _ranking_adapter

    options = scenario(tmp_path, monkeypatch, stability="stable")
    options["adapter"] = _ranking_adapter(exclude_c2=True)
    options["campaign"]["indeterminate"] = LENIENT
    options["extra_cases"] = {"c3": {"critical": True, "pass": True}}
    options["config"]["scorer"] = options["config"]["scorer"].replace(
        'table[(payload["case_id"], edited)]',
        'table[("c2" if payload["case_id"] == "c3" else payload["case_id"], edited)]',
    )
    run, result = await family14_public_run(tmp_path, monkeypatch, **options)
    comparisons = payloads(run, "global_comparison_completed")
    assert [row["accepted"] for row in comparisons] == [False, False, True]
    assert all(row["removed_pairs"] == [["c2", 1]] for row in comparisons)
    for payload in _documents(result):
        items = [item for item in payload["caveats"] if item["kind"] == "stored_pairs_excluded"]
        assert [(item["round"], item["attempt_id"]) for item in items] == [
            (1, "r01-a01"),
            (1, "r01-a02"),
            (1, "r01-a03"),
        ]
        assert {item["measurement_id"] for item in items} == {"m-r00-champion"}
        contrasts = [
            contrast for entry in payload["rounds"][0]["final"] for contrast in entry["contrasts"]
        ]
        # One removed pair per stored contrast, counted from the journal rows.
        assert [item["count"] for item in items] == [
            len(row["removed_pairs"]) for row in comparisons
        ]
        assert [item["count"] for item in items] == [1, 1, 1]
        assert [c["removed_pairs"] for c in contrasts] == [1, 1, 1]
        assert "champion_not_contemporaneous" in _kinds(payload)
    assert audit_mismatches(run) == []
