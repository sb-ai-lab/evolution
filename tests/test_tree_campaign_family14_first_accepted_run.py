"""Genuine H6 finalist prefixes, acceptance cells and live loaded-tree bounds."""

from __future__ import annotations

import pytest

from evolution.tree.campaign import _Campaign
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_projections import inspect_view
from evolution.tree.campaign_schema import SchemaError, current_members
from evolution.tree.campaign_view import DecideRound, next_actions
from tests.campaign_drivers import EDITED, OBSERVE, _cohort_scorer, _cut_hook, _events, _records
from tests.campaign_fakes import Crash, FakeAdapter, Family14Life
from tests.test_tree_campaign_family14_gate_run import assert_clean_run
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


def assert_unopened_absent(run, *, opened, k):
    """Absence covers durable births and work, not only Attempt counters."""
    journal = _records(run / "campaign.jsonl")
    ledger = _records(run / "artifacts.jsonl")
    for index in range(opened + 1, k + 1):
        attempt = f"r01-a{index:02d}"
        assert not any(attempt in str(row["payload"]) for row in journal + ledger)
        assert not list(run.rglob(f"*{attempt}*"))


@pytest.mark.parametrize("capacity,k,losers", [(1, 1, 0), (1, 4, 3), (4, 4, 3)])
async def test_issue160_samples_every_live_tree_transition(
    tmp_path, monkeypatch, capacity, k, losers
):
    options = scenario(tmp_path, monkeypatch, stability="stable", losers=losers)
    options["config"]["gate"] = {"mode": "observe", "fresh_parent": False}
    options["adapter"] = adapter = FakeAdapter(
        max_instances=capacity, max_concurrent_sessions=1, used=EDITED["used"]
    )
    options["campaign"].update(
        attempts_per_round=k, max_concurrency=capacity, attempt_feedback="sequential"
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert len(payloads(run, "attempt_opened")) == k
    assert len({row["parent_digest"] for row in payloads(run, "attempt_opened")}) == 1
    assert all(row["role"] == "finalist" for row in payloads(run, "final_measurement_bound"))
    assert not list((run / "measurements").glob("*-final-*-champion*"))
    samples = adapter.live_tree_samples
    assert len(samples) == len(adapter.materialized) + len(adapter.released)
    assert max(len({digest for _, digest in live}) for _, _, live in samples) == 1
    assert max(len(live) for _, _, live in samples) == adapter.peak_live == capacity
    assert any(len(live) > 1 for _, _, live in samples) == (capacity > 1)
    assert not adapter.live and samples[-1][2] == ()
    # Sampled digests come from the same identity attested by the rollout.
    assert all(
        digest == adapter.created[iid]._identity.instance.loaded_candidate_digest
        for _, _, live in samples
        for iid, digest in live
    )
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
@pytest.mark.parametrize("outcome", ["winner", "no-finalist", "no-accepted-finalist"])
async def test_genuine_acceptance_cells(
    tmp_path, monkeypatch, stability, fresh_parent, mode, outcome
):
    from tests.campaign_drivers import WORSE_SCORER, _flag_on_finalists_bound, _statistical

    config = dict(OBSERVE if mode == "observe" else _statistical(mode))
    config["gate"] = {**config["gate"], "fresh_parent": fresh_parent}
    flag = tmp_path / "final.flag"
    if outcome == "no-accepted-finalist":
        _flag_on_finalists_bound(monkeypatch, flag)
        config["scorer"] = _cohort_scorer(
            flag, flip=mode in ("point", "bound"), critical=mode in ("observe", "off")
        )
    elif outcome == "no-finalist":
        if mode == "observe":
            flag.touch()
            config["scorer"] = _cohort_scorer(flag, critical=True)
        else:
            config["scorer"] = WORSE_SCORER
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"stability": stability, "round_stop": "first_accepted"},
        campaign={"attempts_per_round": 3 if outcome == "winner" else 1, "parallel_attempts": 1},
    )
    (selection,) = payloads(run, "selection_decided")
    (closed,) = payloads(run, "round_closed")
    assert closed["attempts_opened"] == 1
    assert snapshot.identity["policy"]["baseline_refresh_after_rounds"] == 0
    assert not payloads(run, "baseline_refresh_bound")
    if outcome == "no-finalist":
        assert not payloads(run, "finalists_bound")
        assert not payloads(run, "final_measurement_bound")
        assert not payloads(run, "global_comparison_completed")
        assert (selection["reason"], selection["ranking"], selection["measurement_id"]) == (
            "no_finalists",
            [],
            None,
        )
        assert closed["outcome"] == "stalled"
    else:
        (comparison,) = payloads(run, "global_comparison_completed")
        assert comparison["accepted"] == (outcome == "winner")
        assert closed["outcome"] == ("promoted" if outcome == "winner" else "retained")
        assert comparison["champion_side"] == ("stored" if stability == "stable" else "fresh")
        if outcome == "no-accepted-finalist":
            assert (
                comparison["reason"]
                == {
                    "observe": "critical_failure",
                    "off": "critical_failure",
                    "point": "global_point_not_improved",
                    "bound": "global_bound_not_positive",
                }[mode]
            )
            if mode in ("point", "bound"):
                assert comparison["point"] == 0  # Strict equality loses honestly.
            else:
                assert comparison["point"] > 0  # The guard vetoes an improved score.
        else:
            assert_unopened_absent(run, opened=1, k=3)
            assert len(payloads(run, "editor_started")) == 1
            assert len(payloads(run, "attempt_input_frozen")) == 1
    assert_clean_run(run, snapshot)


async def test_unstable_final_samples_both_loaded_sides(tmp_path, monkeypatch):
    adapter = FakeAdapter(max_instances=2, used=EDITED["used"])
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        adapter=adapter,
        family14_settings={"stability": "unstable", "round_stop": "first_accepted"},
    )
    members = payloads(run, "final_measurement_bound")
    assert {row["role"] for row in members} == {"champion", "finalist"}
    assert max(len({digest for _, digest in live}) for _, _, live in adapter.live_tree_samples) == 2
    assert adapter.peak_live == 2 and not adapter.live
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("mode", ["point", "bound"])
async def test_critical_guard_precedes_a_failing_global_numeric_rule(
    tmp_path, monkeypatch, stability, mode
):
    from tests.campaign_drivers import _flag_on_finalists_bound, _statistical

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    scorer = _cohort_scorer(flag, flip=True).replace(
        '"critical_pass": True',
        f'"critical_pass": not (payload["case_id"] == "c2" '
        f"and pathlib.Path({str(flag)!r}).exists())",
    )
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**_statistical(mode), "scorer": scorer},
        family14_settings={"stability": stability, "round_stop": "first_accepted"},
    )
    (comparison,) = payloads(run, "global_comparison_completed")
    assert (comparison["point"], comparison["lower"]) == (0, 0)
    assert (comparison["accepted"], comparison["reason"]) == (False, "critical_failure")
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("retained", [False, True])
async def test_next_round_baseline_grouping_and_visitation_are_real(
    tmp_path, monkeypatch, stability, retained
):
    from tests.test_tree_campaign_family14_stored_run import _two_round_scorer

    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _two_round_scorer(monkeypatch, tmp_path / "veto.flag", retained)},
        family14_settings={"stability": stability, "round_stop": "first_accepted"},
        campaign={"rounds": 2},
    )
    first, second = payloads(run, "selection_decided")
    baseline = payloads(run, "baseline_bound")[1]
    expected = (
        "m-r00-champion"
        if retained and stability == "stable"
        else "m-r01-final-01-champion"
        if retained
        else "m-r01-final-01-a01"
    )
    assert first["measurement_id"] == baseline["measurement_id"] == expected
    assert second["measurement_id"] == "m-r02-final-01-a01"
    comparisons = payloads(run, "global_comparison_completed")
    if stability == "stable":
        assert comparisons[1]["champion_measurement_id"] == expected
    else:
        assert comparisons[1]["champion_measurement_id"] == "m-r02-final-01-champion"
    champion = payloads(run, "round_closed")[0]["champion_digest"]
    assert payloads(run, "attempt_opened")[1]["parent_digest"] == champion
    assert [row["outcome"] for row in payloads(run, "round_closed")] == [
        "retained" if retained else "promoted",
        "promoted",
    ]
    portions = payloads(run, "focus_portion_selected")
    # The fresh retained Champion also observed the critical failure on c2.
    # Its unvisited c2 is selected without forgetting c1's earlier visit.
    fresh_retained = retained and stability == "unstable"
    assert [row["case_ids"] for row in portions] == [["c1"], ["c2"] if fresh_retained else ["c1"]]
    assert [row["cycle"] for row in portions] == (
        [1, 2] if retained and stability == "stable" else [1, 1]
    )
    grouping = payloads(run, "grouping_started")
    assert len(grouping) == (1 if retained and stability == "stable" else 2)
    assert len({row["baseline_binding"] for row in grouping}) == len(grouping)
    assert len(payloads(run, "reflection_finished")) == 2
    for number in (1, 2):
        inputs = [row for row in payloads(run, "attempt_input_frozen") if row["round"] == number]
        assert len({row["common_input_digest"] for row in inputs}) == 1
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("path", ["clean", "declined", "invalid"])
async def test_zero_attempt_paths_own_no_final_or_selection_artifacts(
    tmp_path, monkeypatch, stability, path
):
    import json

    from tests.campaign_drivers import COUNTING_SCORER, DECLINING_REFLECTOR

    kwargs = {}
    config = dict(OBSERVE)
    if path == "clean":
        config["scorer"] = COUNTING_SCORER.replace('"passed": passed', '"passed": True')
    elif path == "declined":
        kwargs["reflector"] = DECLINING_REFLECTOR
    else:
        invalid = {
            "root_cause": "r",
            "suggested_fix": "f",
            "evidence_ids": ["obs:unknown:1"],
            "affected_cases": ["c1"],
            "target_paths": ["skills/SKILL.md"],
            "counterevidence": "c",
            "regression_risks": "r",
            "why_this_layer": "w",
        }
        kwargs["reflector"] = (
            "print("
            + repr(json.dumps({"status": "reflected", "note": "n", "groups": [invalid]}))
            + ")\n"
        )
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        **kwargs,
        family14_settings={"stability": stability, "round_stop": "first_accepted"},
        campaign={"attempts_per_round": 3},
    )
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == (
        "clean" if path == "clean" else "unplanned",
        0,
    )
    for event in (
        "attempt_opened",
        "attempt_input_frozen",
        "editor_started",
        "finalists_bound",
        "final_measurement_bound",
        "final_measurement_completed",
        "selection_decided",
    ):
        assert not payloads(run, event)
    assert_unopened_absent(run, opened=0, k=3)
    assert not any(
        row["event"].startswith("promotion_") for row in _records(run / "artifacts.jsonl")
    )
    assert_clean_run(run, snapshot)


def scenario(tmp_path, monkeypatch, *, stability, skip_first=False, losers=2):
    """Change actual scorer verdicts by phase; all lifecycle/decision code runs."""
    flag = tmp_path / "critical.flag"
    original = _Campaign.hook

    def hook(self, name):
        if name in ("after_attempt_input_frozen", "before_finalists_bound"):
            state = self.snapshot().rounds[1]
            critical = (
                skip_first and len(state.attempts) == 1
                if name == "after_attempt_input_frozen"
                else len(state.finals) + 1 <= losers
            )
            if critical:
                flag.touch()
            else:
                flag.unlink(missing_ok=True)
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)
    return dict(
        adapter=FakeAdapter(used=EDITED["used"]),
        config={**OBSERVE, "scorer": _cohort_scorer(flag, critical=True)},
        family14_settings={"stability": stability, "round_stop": "first_accepted"},
        campaign={"attempts_per_round": 5, "parallel_attempts": 1, "attempt_feedback": "none"},
    )


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("skip_first", [False, True])
async def test_each_finalist_finishes_before_next_attempt_and_ordinals_count_finals(
    tmp_path, monkeypatch, stability, skip_first
):
    options = scenario(tmp_path, monkeypatch, stability=stability, skip_first=skip_first)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_global_comparison_completed", call=3)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    bindings = _events(run, "finalists_bound")
    comparisons = _events(run, "global_comparison_completed")
    assert [row["payload"]["accepted"] for row in comparisons] == [False, False, True]
    assert not payloads(run, "selection_decided")
    assert not payloads(run, "round_closed")
    assert not [r for r in _records(run / "artifacts.jsonl") if r["event"].startswith("promotion_")]
    closes = _events(run, "attempt_closed")
    opens = _events(run, "attempt_opened")
    assert len(opens) == 3 + skip_first
    inputs = payloads(run, "attempt_input_frozen")
    assert len({row["common_input_digest"] for row in inputs}) == 1
    assert all(row["feedback"]["attempts"] == [] for row in inputs)
    assert len({row["payload"]["parent_digest"] for row in opens}) == 1
    life = Family14Life(_records(run / "campaign.jsonl"))
    state = life.snapshot.rounds[1]
    assert len(state.finals) == 3
    baseline = payloads(run, "baseline_complete")[0]
    for ordinal, (bound, compared) in enumerate(zip(bindings, comparisons, strict=True), 1):
        index = ordinal + skip_first
        expected = [f"m-r01-final-{ordinal:02d}-a{index:02d}"]
        if stability == "unstable":
            expected.insert(0, f"m-r01-final-{ordinal:02d}-champion")
        assert bound["payload"]["members"] == expected
        assert bound["payload"]["finalists"][0]["attempt_id"] == f"r01-a{index:02d}"
        assert current_members(state, final=state.finals[ordinal - 1]) == tuple(expected)
        assert closes[index - 1]["sequence"] < bound["sequence"] < compared["sequence"]
        if ordinal < 3:
            assert compared["sequence"] < opens[index]["sequence"]
        for member in expected:
            assert member in state.cohort_completed
            if stability == "stable":
                entry = life.snapshot.measurements[member]
                assert dict(entry["stored_reference"]) == baseline["evidence"][0]
                assert (
                    entry["stored_champion"]["prefix_digest"]
                    == baseline["evidence"][0]["content_digest"]
                )
    view = inspect_view(run)
    policy = CampaignPolicyV11.from_payload(view.snapshot.identity["policy"])
    assert next_actions(view, policy) == (DecideRound(1),)

    # Replay genuine journal prefixes to exercise the lifecycle fences, with
    # no fabricated successful look, completion or comparison.
    records = _records(run / "campaign.jsonl")
    for ordinal, bound in enumerate(bindings):
        before = Family14Life(records[: bound["sequence"] - 1])
        payload = bound["payload"]
        wrong_ordinal = {
            **payload,
            "members": [
                mid.replace(f"final-{ordinal + 1:02d}", "final-09") for mid in payload["members"]
            ],
        }
        with pytest.raises(SchemaError, match="members are the Champion"):
            before.accept("finalists_bound", **wrong_ordinal)
        wrong_citation = {**payload, "evidence": [baseline["evidence"][0]]}
        with pytest.raises(SchemaError, match="cites each finalist"):
            before.accept("finalists_bound", **wrong_citation)
        if ordinal:
            with pytest.raises(SchemaError):
                before.accept("finalists_bound", **bindings[ordinal - 1]["payload"])
            multiple = {
                **payload,
                "finalists": bindings[ordinal - 1]["payload"]["finalists"] + payload["finalists"],
            }
            with pytest.raises(SchemaError):
                before.accept("finalists_bound", **multiple)
        during = Family14Life(records[: bound["sequence"]])
        with pytest.raises(SchemaError, match="earlier final bindings settle"):
            during.accept("finalists_bound", **payload)
        version, digest = during.snapshot.champion
        index = ordinal + 2 + skip_first
        with pytest.raises(SchemaError, match="finalist is compared"):
            during.accept(
                "attempt_opened",
                round=1,
                attempt_id=f"r01-a{index:02d}",
                attempt_index=index,
                parent_version=version,
                parent_digest=digest,
            )
        after_comparison = Family14Life(records[: comparisons[ordinal]["sequence"]])
        with pytest.raises(SchemaError, match="latest closed unbound|accepted finalist"):
            after_comparison.accept("finalists_bound", **payload)
    with pytest.raises(SchemaError, match="accepted finalist"):
        life.accept(
            "attempt_opened",
            round=1,
            attempt_id="r01-a05",
            attempt_index=5,
            parent_version=version,
            parent_digest=digest,
        )


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "cut",
    [
        "after_finalists_bound",
        "after_cohort_measurement_bound",
        "after_cohort_measurement_completed",
        "after_global_comparison_completed",
    ],
)
async def test_losing_final_prefix_resumes_into_next_binding(tmp_path, monkeypatch, stability, cut):
    options = scenario(tmp_path, monkeypatch, stability=stability)
    with monkeypatch.context() as patch:
        _cut_hook(patch, cut)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    before_records = _records(run / "campaign.jsonl")
    before = Family14Life(before_records).snapshot.rounds[1]
    assert len(before.finals) == 1
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_finalists_bound")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    after = Family14Life(_records(run / "campaign.jsonl")).snapshot.rounds[1]
    assert len(after.finals) == 2
    assert before.finals[0] == after.finals[0]
    assert len(before.finals) == 1
    assert current_members(before) == current_members(after, final=after.finals[0])
    assert len(after.global_comparisons) == 1
    assert after.global_comparisons["r01-a01"]["accepted"] is False
    assert all(mid in after.cohort_completed for mid in current_members(before))
    assert not payloads(run, "selection_decided")
    assert _records(run / "campaign.jsonl")[: len(before_records)] == before_records


async def test_repeated_stable_finals_keep_exclusions_and_measure_full_corpus(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import LENIENT, _measurement_events
    from tests.test_tree_campaign_family14_stored import _ranking_adapter

    options = scenario(tmp_path, monkeypatch, stability="stable")
    options["adapter"] = _ranking_adapter(exclude_c2=True)
    options["campaign"]["indeterminate"] = LENIENT
    options["extra_cases"] = {"c3": {"critical": True, "pass": True}}
    options["config"]["scorer"] = options["config"]["scorer"].replace(
        'table[(payload["case_id"], edited)]',
        'table[("c2" if payload["case_id"] == "c3" else payload["case_id"], edited)]',
    )
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_global_comparison_completed", call=3)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    comparisons = payloads(run, "global_comparison_completed")
    assert [row["accepted"] for row in comparisons] == [False, False, True]
    assert all(row["removed_pairs"] == [["c2", 1]] for row in comparisons)
    assert all(row["removed_cases"] == ["c2"] for row in comparisons)
    baseline = payloads(run, "baseline_complete")[0]
    bindings = payloads(run, "final_measurement_bound")
    assert len(bindings) == 3
    assert all(row["evidence"] == baseline["evidence"] for row in bindings)
    assert all(
        row["stored_champion"]["prefix_digest"] == baseline["evidence"][0]["content_digest"]
        for row in bindings
    )
    for completion in payloads(run, "final_measurement_completed"):
        assert completion["complete"] is True and completion["missing"] == 0
        rows = _records(_measurement_events(run, completion["measurement_id"]))
        assert not any(row["event"] == "sample_imported" for row in rows)
        assert sum(row["event"] == "scoring_finished" for row in rows) == 3


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_historical_binding_retains_its_rebound_member_ids(tmp_path, monkeypatch, stability):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable

    options = scenario(tmp_path, monkeypatch, stability=stability)
    options["adapter"] = adapter = _Resettable(used=EDITED["used"])
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_bound")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    before = Family14Life(_records(run / "campaign.jsonl")).snapshot.rounds[1]
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, **options)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_finalists_bound")
        with pytest.raises(Crash):
            await family14_run(
                tmp_path, patch, authorizations=[_continue(refusal.value)], **options
            )
    after = Family14Life(_records(run / "campaign.jsonl")).snapshot.rounds[1]
    assert len(after.finals) == 2
    historical = current_members(after, final=after.finals[0])
    assert historical[0] == current_members(before)[0] + "@e1"
    assert all(mid in after.cohort_completed for mid in historical)
    assert after.finals[0] == before.finals[0]
    assert (
        after.global_comparisons["r01-a01"][
            "finalist_measurement_id" if stability == "stable" else "champion_measurement_id"
        ]
        == historical[0]
    )
    assert current_members(after) == tuple(after.finals[1]["members"])
    assert not payloads(run, "selection_decided")
