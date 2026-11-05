"""H7 genuine family-14 baseline refresh: the planner and executor write it.

Every run here is a real `family14_run` engine invocation (creation, lifecycle
admission, adapters, scoring and journals are real), stopped before H12's
public report assembly. Each run replays through the lifecycle
(`journal_consistent`) and, with the audit's refresh support (Task 6), audits
clean (`audit(run)["mismatches"] == []`).

Task 7 adds the §15.12 evidence: a refresh in every `stability` x
`round_stop` x mode cell, the resets (Promotion, `unstable` `retained`,
refresh; `continue` keeps the count), every baseline-keeping closure, the
replacement (the stored Champion of a later `stable` final, its rows,
exhausted pairs, the renewed error pool), N = 0 against the H6 event order
and the live-tree bound. The `rebaseline` reset lives with the authorization
cuts (Task 8); the public `baseline_refresh: off` and a linked run's reset
are H12's.
"""

from __future__ import annotations

import pytest

from evolution.tree.campaign_identity import measurement_id_for, refresh_measurement_id_for
from evolution.tree.campaign_view import (
    BindBaseline,
    BindBaselineRefresh,
    CampaignView,
    CompleteBaselineRefresh,
    RunGrouping,
    measurement_side,
    next_actions,
    refresh_due_for,
    refresh_for,
)
from tests.campaign_drivers import STATISTICAL, WORSE_SCORER, _measurement_events, _records
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import journal_consistent
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

#: No finalists: every edit scores c1 worse, so each round closes `stalled`
#: on the same stored Champion measurement and the count grows.
NO_FINALISTS = {**STATISTICAL, "scorer": WORSE_SCORER}
CHAMPION0 = measurement_id_for(None)
REPEATS = STATISTICAL["repeats"]


def _refresh_veto_scorer(monkeypatch, flag):
    """c1 fails everywhere; c2 fails (critical veto, same score) only while a
    refresh measures the Champion.

    The flag exists between the production hooks `after_baseline_refresh_bound`
    and `after_baseline_refresh_completed`, so only the refresh measurements
    observe c2's failure. The hook changes fixture inputs; it never changes an
    engine decision (the H6 `_two_round_scorer` pattern).
    """
    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook

    def phase(self, name):
        if name == "after_baseline_refresh_bound":
            flag.touch()
        if name == "after_baseline_refresh_completed":
            flag.unlink(missing_ok=True)
        return original(self, name)

    monkeypatch.setattr(_Campaign, "hook", phase)
    return WORSE_SCORER.replace(
        '"critical_pass": True',
        f'"critical_pass": not (payload["case_id"] == "c2" and pathlib.Path({str(flag)!r}).exists())',
    )


def _resolve(run, reference):
    """The campaign record a `journal_record` reference names, by its bytes."""
    import hashlib
    import json

    assert reference["kind"] == "journal_record"
    locator = reference["locator"]
    assert locator.startswith("campaign.jsonl#bytes=")
    start, end = (int(part) for part in locator.split("=", 1)[1].split(":"))
    data = (run / "campaign.jsonl").read_bytes()[start:end]
    assert hashlib.sha256(data).hexdigest() == reference["content_digest"]
    return json.loads(data)


def _complete_reference(run, number):
    (row,) = [p for p in payloads(run, "baseline_complete") if p["round"] == number]
    return row["evidence"][0]


async def _refresh_run(tmp_path, monkeypatch, refresh, rounds=3, config=NO_FINALISTS):
    return await family14_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"refresh": refresh},
        campaign={"rounds": rounds},
    )


async def test_n1_refreshes_rounds_2_and_3_in_the_recorded_order(tmp_path, monkeypatch):
    run, snapshot = await _refresh_run(tmp_path, monkeypatch, 1)
    assert snapshot.finished["outcome"] == "completed"
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["stalled"] * 3
    assert journal_consistent(run).finished == snapshot.finished

    # Round 2: refresh bound -> baseline bound -> champion measurement bound
    # -> its stages -> baseline complete -> refresh completed -> grouping.
    events = [row["event"] for row in _records(run / "campaign.jsonl")]
    start = events.index("baseline_refresh_bound")
    end = events.index("grouping_started", start)
    window = events[start : end + 1]
    assert window[:3] == ["baseline_refresh_bound", "baseline_bound", "measurement_bound"]
    assert window[-3:] == ["baseline_complete", "baseline_refresh_completed", "grouping_started"]
    # The refresh's own stages (and their instance attestations).
    assert set(window[3:-3]) == {"stage_opened", "instance_attested"}
    assert {
        row["payload"]["measurement_id"]
        for row in _records(run / "campaign.jsonl")[start + 3 : end - 2]
    } == {"m-r02-refresh"}
    records = _records(run / "campaign.jsonl")
    assert records[start + 1]["payload"]["measurement_id"] == "m-r02-refresh"
    assert records[start + 2]["payload"]["attempt_id"] == "champion"
    assert records[start + 2]["payload"]["measurement_id"] == "m-r02-refresh"
    assert all(
        records[i]["payload"]["round"] == 2 for i in (start, start + 1, end - 2, end - 1, end)
    )

    bound = payloads(run, "baseline_refresh_bound")
    assert [(p["round"], p["measurement_id"]) for p in bound] == [
        (2, "m-r02-refresh"),
        (3, "m-r03-refresh"),
    ]
    for number, row, replaced in ((2, bound[0], CHAMPION0), (3, bound[1], "m-r02-refresh")):
        previous = _complete_reference(run, number - 1)
        assert row["baseline_refresh_after_rounds"] == 1
        assert row["counted_rounds"] == {"first": number - 1, "last": number - 1}
        assert row["replaced"] == {
            "measurement_id": replaced,
            "prefix_digest": previous["content_digest"],
        }
        assert row["evidence"][0] == previous
        closure = _resolve(run, row["evidence"][1])
        assert closure["event"] == "round_closed"
        assert closure["payload"]["round"] == number - 1
    completed = payloads(run, "baseline_refresh_completed")
    assert [(p["round"], p["measurement_id"]) for p in completed] == [
        (2, "m-r02-refresh"),
        (3, "m-r03-refresh"),
    ]
    for row in completed:
        assert row["evidence"] == [_complete_reference(run, row["round"])]

    baselines = [p["measurement_id"] for p in payloads(run, "baseline_bound")]
    assert baselines == [CHAMPION0, "m-r02-refresh", "m-r03-refresh"]
    grouping = payloads(run, "grouping_started")
    assert [row["round"] for row in grouping] == [1, 2, 3]
    assert len({row["baseline_binding"] for row in grouping}) == 3

    # The snapshot keeps one completed entry per refresh, in journal order.
    assert [
        (entry["round"], entry["completed"]["measurement_id"]) for entry in snapshot.refreshes
    ] == [(2, "m-r02-refresh"), (3, "m-r03-refresh")]
    assert audit(run)["mismatches"] == []


async def test_the_refresh_measures_the_full_corpus_with_the_baseline_schedule(
    tmp_path, monkeypatch
):
    run, snapshot = await _refresh_run(tmp_path, monkeypatch, 1, rounds=2)
    refresh = refresh_measurement_id_for(2)
    seed = _records(_measurement_events(run, CHAMPION0))
    own = _records(_measurement_events(run, refresh))
    assert own[0]["payload"]["identity"]["schedule"] == seed[0]["payload"]["identity"]["schedule"]
    assert not any(row["event"] == "sample_imported" for row in own)
    scored = [row["payload"] for row in own if row["event"] == "scoring_finished"]
    assert {(row["case_id"], int(row["sample_id"])) for row in scored} == {
        (case, repeat) for case in ("c1", "c2") for repeat in range(1, REPEATS + 1)
    }
    assert len(scored) == 2 * REPEATS
    # Declared on the Champion side (`side_of_entry`); never a Candidate.
    assert snapshot.measurements[refresh]["attempt_id"] == "champion"
    assert measurement_side(snapshot, refresh) == "champion"
    assert audit(run)["mismatches"] == []


async def test_n2_refreshes_only_at_round_3(tmp_path, monkeypatch):
    run, snapshot = await _refresh_run(tmp_path, monkeypatch, 2)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    (bound,) = payloads(run, "baseline_refresh_bound")
    assert (bound["round"], bound["measurement_id"]) == (3, "m-r03-refresh")
    assert bound["counted_rounds"] == {"first": 1, "last": 2}
    assert bound["replaced"]["measurement_id"] == CHAMPION0
    assert bound["evidence"][0] == _complete_reference(run, 2)
    assert [p["round"] for p in payloads(run, "baseline_refresh_completed")] == [3]
    baselines = [p["measurement_id"] for p in payloads(run, "baseline_bound")]
    assert baselines == [CHAMPION0, CHAMPION0, "m-r03-refresh"]
    assert audit(run)["mismatches"] == []


async def test_n0_writes_no_refresh_record(tmp_path, monkeypatch):
    run, snapshot = await _refresh_run(tmp_path, monkeypatch, 0)
    assert snapshot.identity["policy"]["baseline_refresh_after_rounds"] == 0
    assert not payloads(run, "baseline_refresh_bound")
    assert not payloads(run, "baseline_refresh_completed")
    assert snapshot.refreshes == ()
    assert [p["measurement_id"] for p in payloads(run, "baseline_bound")] == [CHAMPION0] * 3
    assert audit(run)["mismatches"] == []


async def test_the_refresh_carries_visitation_within_the_cycle(tmp_path, monkeypatch):
    """Round 1's baseline fails c1 only, so it visits c1. Round 2's refresh
    (a new baseline binding, its own grouping) also fails c2: the cycle goes
    on, c1 stays visited, and the portion selects c2 in cycle 1. Round 3's
    refresh has both cases visited, so its cycle advances to 2."""

    scorer = _refresh_veto_scorer(monkeypatch, tmp_path / "veto.flag")
    run, snapshot = await _refresh_run(
        tmp_path, monkeypatch, 1, config={**STATISTICAL, "scorer": scorer}
    )
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["stalled"] * 3
    assert [p["round"] for p in payloads(run, "baseline_refresh_bound")] == [2, 3]
    portions = payloads(run, "focus_portion_selected")
    assert [row["case_ids"] for row in portions][:2] == [["c1"], ["c2"]]
    assert [row["cycle"] for row in portions] == [1, 1, 2]
    assert not set(portions[1]["case_ids"]) & set(portions[0]["case_ids"])
    grouping = payloads(run, "grouping_started")
    assert len({row["baseline_binding"] for row in grouping}) == 3
    assert [row["baseline_binding"] for row in portions] == [
        row["baseline_binding"] for row in grouping
    ]
    assert audit(run)["mismatches"] == []


def _view(snapshot):
    return CampaignView(snapshot, measurements={}, lineage=None, siblings={})


async def test_the_planner_is_a_pure_function_of_each_refresh_cut(tmp_path, monkeypatch):
    """Replay the genuine N = 1 journal up to each refresh boundary and ask
    the planner what comes next: it names exactly the record the engine
    wrote next."""
    from tests.campaign_fakes import Family14Life

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1, rounds=2)
    records = _records(run / "campaign.jsonl")
    events = [row["event"] for row in records]
    policy_holder = Family14Life(records[:1])
    policy = policy_holder.state.policy

    def planned(end):
        snapshot = Family14Life(records[:end]).snapshot
        return snapshot, next_actions(_view(snapshot), policy)

    closed = next(i for i, row in enumerate(records) if row["event"] == "round_closed")
    snapshot, actions = planned(closed + 1)
    assert actions == (BindBaselineRefresh(2),)
    due = refresh_due_for(snapshot, policy, 2)
    assert due is not None and (due.first, due.last, due.replaced) == (1, 1, CHAMPION0)
    assert refresh_for(snapshot, 2) is None

    bound = events.index("baseline_refresh_bound")
    snapshot, actions = planned(bound + 1)
    assert actions == (BindBaseline(2, "m-r02-refresh"),)
    assert refresh_for(snapshot, 2)["completed"] is None
    # Undeclared, the refresh id still resolves to the Champion side.
    assert "m-r02-refresh" not in snapshot.measurements
    assert measurement_side(snapshot, "m-r02-refresh") == "champion"

    # Cut between `baseline_bound` and the Champion's `measurement_bound`.
    snapshot, actions = planned(bound + 2)
    assert actions == (BindBaseline(2, "m-r02-refresh"),)

    complete = events.index("baseline_complete", bound)
    snapshot, actions = planned(complete + 1)
    assert actions == (CompleteBaselineRefresh(2, "m-r02-refresh"),)

    snapshot, actions = planned(complete + 2)
    assert events[complete + 1] == "baseline_refresh_completed"
    assert refresh_for(snapshot, 2)["completed"]["measurement_id"] == "m-r02-refresh"
    assert actions == (RunGrouping(2),)


async def test_refresh_due_for_is_family_14_only(tmp_path, monkeypatch):

    from tests.campaign_fakes import Family14Life

    run, _ = await _refresh_run(tmp_path, monkeypatch, 1, rounds=2)
    records = _records(run / "campaign.jsonl")
    closed = next(i for i, row in enumerate(records) if row["event"] == "round_closed")
    life = Family14Life(records[: closed + 1])
    snapshot = life.snapshot
    assert refresh_due_for(snapshot, life.state.policy, 2) is not None


# -- Task 7: genuine §15.12 evidence in every cell ------------------------------
#
# Fixture phases change only what the fixed scorer subprocess sees, through
# production hooks (the H6 `_two_round_scorer` pattern); every engine decision
# (gate, final, selection, close, count, refresh) stays real.


def _round_now(campaign):
    """The latest round the campaign has begun (the closed one in `after_round_closed`)."""
    return max(campaign.snapshot().rounds)


def _phase_hooks(
    monkeypatch, *, veto=None, veto_rounds=(), stall=None, stall_through=0, during_refresh=None
):
    """`veto` exists from a `veto_rounds` round's `finalists_bound` until it
    closes; `stall` exists from the start until round `stall_through` closes;
    `during_refresh` exists between `after_baseline_refresh_bound` and
    `after_baseline_refresh_completed`, so only refresh measurements see it."""
    from evolution.tree.campaign import _Campaign

    original = _Campaign.hook
    if stall is not None and stall_through:
        stall.touch()

    def hook(self, name):
        if during_refresh is not None:
            if name == "after_baseline_refresh_bound":
                during_refresh.touch()
            if name == "after_baseline_refresh_completed":
                during_refresh.unlink(missing_ok=True)
        if name == "after_finalists_bound" and veto is not None:
            if _round_now(self) in veto_rounds:
                veto.touch()
        if name == "after_round_closed":
            if veto is not None:
                veto.unlink(missing_ok=True)
            if stall is not None and _round_now(self) >= stall_through:
                stall.unlink(missing_ok=True)
        return original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)


def _veto_scorer(flag, *, refresh_score=None, refresh_fails=None):
    """c1 fails diagnostically on every tree while each edit raises its score by
    0.2 (so every Candidate is a finalist); c2 scores 0.8 and fails its critical
    check on every tree while `flag` exists, which vetoes a final measured then
    (`retained`). While `refresh_score` exists c2 scores 0.6 instead (a refresh
    reads apart from `m-r00-champion`); while `refresh_fails` exists c2 also
    fails its verdict (it joins the refresh's error pool)."""
    from tests.campaign_drivers import COUNTING_SCORER

    c2 = "0.8"
    if refresh_score is not None:
        c2 = f"(0.6 if pathlib.Path({str(refresh_score)!r}).exists() else 0.8)"
    passed = 'payload["case_id"] != "c1"'
    if refresh_fails is not None:
        passed += f" and not pathlib.Path({str(refresh_fails)!r}).exists()"
    return (
        COUNTING_SCORER.replace(
            '"metrics": {"score": 1.0 if passed else 0.0}',
            '"metrics": {"score": 0.2 * skill.count("edited") '
            f'if payload["case_id"] == "c1" else {c2}}}',
        )
        .replace('"passed": passed', f'"passed": {passed}')
        .replace(
            '"critical_pass": True',
            f'"critical_pass": not (payload["case_id"] == "c2" '
            f"and pathlib.Path({str(flag)!r}).exists())",
        )
    )


def _stall_scorer(flag, mode):
    """`VARYING_SCORER`; while `flag` exists an edited tree has no finalist:
    under observe it fails c2's critical check, under statistical it scores
    c1 worse than the unedited tree. Unedited trees are never affected."""
    from tests.campaign_drivers import VARYING_SCORER, _cohort_scorer

    if mode == "observe":
        return _cohort_scorer(flag, critical=True)
    line = 'score = table[(payload["case_id"], edited)][(repeat - 1) % 4]'
    text = VARYING_SCORER.replace(
        line,
        f"{line}\n"
        f'if edited and payload["case_id"] == "c1" and pathlib.Path({str(flag)!r}).exists():\n'
        "    score = [0.0, 0.05, 0.1, 0.0][(repeat - 1) % 4]",
    )
    assert text != VARYING_SCORER
    return text


def _cell_config(mode, fresh_parent, scorer):
    from tests.campaign_drivers import OBSERVE, _statistical

    config = dict(OBSERVE if mode == "observe" else _statistical("bound"))
    config["gate"] = {**config["gate"], "fresh_parent": fresh_parent}
    config["scorer"] = scorer
    return config


def _adapter(mode, cls=None):
    from tests.campaign_drivers import EDITED, _RepeatTagged
    from tests.campaign_fakes import FakeAdapter

    if cls is None:
        cls = FakeAdapter if mode == "observe" else _RepeatTagged
    return cls(used=EDITED["used"])


def _by_round(run, event, key="measurement_id"):
    return {row["round"]: row[key] for row in payloads(run, event)}


#: `stability` x `round_stop` x mode; N and `fresh_parent` spread across the
#: cells (each value four times; `fresh_parent` is orthogonal to the refresh).
CELLS = [
    ("stable", "all_attempts", "statistical", 1, False),
    ("stable", "all_attempts", "observe", 2, True),
    ("stable", "first_accepted", "statistical", 2, True),
    ("stable", "first_accepted", "observe", 1, False),
    ("unstable", "all_attempts", "statistical", 2, False),
    ("unstable", "all_attempts", "observe", 1, True),
    ("unstable", "first_accepted", "statistical", 1, True),
    ("unstable", "first_accepted", "observe", 2, False),
]


def _cell_id(cell):
    stability, round_stop, mode, refresh, fresh_parent = cell
    return f"{stability}-{round_stop}-{mode}-N{refresh}-fp{int(fresh_parent)}"


async def _cell_run(tmp_path, monkeypatch, cell, *, refresh=None, adapter=None):
    """N losing rounds, then round N + 1. Under `stable` each losing round has a
    finalist the final vetoes (`retained`, the stored baseline kept); under
    `unstable` each has no finalist (`stalled`). Round N + 1 promotes."""
    stability, round_stop, mode, cell_refresh, fresh_parent = cell
    losing = cell_refresh
    if stability == "stable":
        flag, during = tmp_path / "veto.flag", tmp_path / "refresh.flag"
        _phase_hooks(
            monkeypatch, veto=flag, veto_rounds=range(1, losing + 1), during_refresh=during
        )
        scorer = _veto_scorer(flag, refresh_score=during)
    else:
        flag = tmp_path / "stall.flag"
        _phase_hooks(monkeypatch, stall=flag, stall_through=losing)
        scorer = _stall_scorer(flag, mode)
    return await family14_run(
        tmp_path,
        monkeypatch,
        _cell_config(mode, fresh_parent, scorer),
        adapter=adapter if adapter is not None else _adapter(mode),
        family14_settings={
            "stability": stability,
            "round_stop": round_stop,
            "refresh": cell_refresh if refresh is None else refresh,
        },
        campaign={"rounds": losing + 1},
    )


@pytest.mark.parametrize("cell", CELLS, ids=_cell_id)
async def test_every_cell_refreshes_at_the_derived_round_and_audits_clean(
    tmp_path, monkeypatch, cell
):
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    stability, round_stop, mode, refresh, fresh_parent = cell
    adapter = _adapter(mode)
    run, snapshot = await _cell_run(tmp_path, monkeypatch, cell, adapter=adapter)
    assert_clean_run(run, snapshot)
    policy = snapshot.identity["policy"]
    assert policy["gate"]["stability"] == stability
    assert policy["gate"]["fresh_parent"] is fresh_parent
    assert policy["round_stop"] == round_stop
    assert policy["baseline_refresh_after_rounds"] == refresh

    last = refresh + 1
    refresh_id = refresh_measurement_id_for(last)
    losing = "retained" if stability == "stable" else "stalled"
    assert [row["outcome"] for row in payloads(run, "round_closed")] == [losing] * refresh + [
        "promoted"
    ]
    finals = _by_round(run, "finalists_bound", "members")
    if stability == "stable":
        # Losers: every losing round had a finalist, the final vetoed it, and
        # the selection kept the stored baseline -- so the round counts.
        assert set(finals) == set(range(1, last + 1))
        assert all(
            row["measurement_id"] == CHAMPION0
            for row in payloads(run, "selection_decided")
            if row["round"] <= refresh
        )
    else:
        # Only after N rounds with no finalist.
        assert set(finals) == {last}

    (bound,) = payloads(run, "baseline_refresh_bound")
    assert (bound["round"], bound["measurement_id"], bound["baseline_refresh_after_rounds"]) == (
        last,
        refresh_id,
        refresh,
    )
    assert bound["counted_rounds"] == {"first": 1, "last": refresh}
    assert bound["replaced"]["measurement_id"] == CHAMPION0
    (completed,) = payloads(run, "baseline_refresh_completed")
    assert (completed["round"], completed["measurement_id"]) == (last, refresh_id)
    baselines = _by_round(run, "baseline_bound")
    assert list(baselines.values()) == [CHAMPION0] * refresh + [refresh_id]

    # Grouping ran on the refresh binding: its baseline prefix.
    refresh_prefix = _complete_reference(run, last)
    grouping = _by_round(run, "grouping_started", "baseline_binding")
    assert grouping[last] == refresh_prefix["content_digest"]
    assert grouping[1] == _complete_reference(run, 1)["content_digest"] != grouping[last]

    comparisons = _by_round(run, "global_comparison_completed", "champion_measurement_id")
    (comparison,) = [p for p in payloads(run, "global_comparison_completed") if p["round"] == last]
    if stability == "stable":
        # The later stable final is compared with the refresh (§4.9).
        assert all(comparisons[number] == CHAMPION0 for number in range(1, last))
        assert comparison["champion_side"] == "stored"
        assert comparison["champion_measurement_id"] == refresh_id
        assert comparison["evidence"][1] == refresh_prefix
        # Its rows, not `m-r00-champion`'s: the refresh scored c2 0.6, so the
        # Champion point is 0.3 (0.4 on `m-r00-champion`); the finalist is 0.5.
        (final,) = [
            p
            for p in payloads(run, "final_measurement_completed")
            if p["round"] == last and p["measurement_id"] == comparison["finalist_measurement_id"]
        ]
        assert final["point"] == pytest.approx(0.5)
        assert comparison["point"] == pytest.approx(0.2)
        assert all(
            p["point"] == pytest.approx(0.1)
            for p in payloads(run, "global_comparison_completed")
            if p["round"] < last
        )
        for row in payloads(run, "final_measurement_bound"):
            if row["round"] == last:
                assert row["stored_champion"] == {
                    "measurement_id": refresh_id,
                    "prefix_digest": refresh_prefix["content_digest"],
                }
    else:
        assert comparison["champion_side"] == "fresh"
        assert comparison["champion_measurement_id"] == f"m-r{last:02d}-final-01-champion"
    assert comparison["accepted"] is True

    if (stability, round_stop, fresh_parent) == ("stable", "first_accepted", False):
        # H6's live-tree sampler: across the refresh round one loaded tree at
        # a time; the refresh materialized the Champion once more, alone.
        samples = adapter.live_tree_samples
        assert max(len({digest for _, digest in live}) for _, _, live in samples) == 1
        champion = payloads(run, "round_closed")[0]["champion_digest"]
        champion_loads = [
            iid
            for operation, iid, live in samples
            if operation == "materialize" and dict(live)[iid] == champion
        ]
        assert len(champion_loads) == 2
        assert not adapter.live


# Resets ------------------------------------------------------------------------


def _portions(run):
    return {
        row["round"]: (row["case_ids"], row["cycle"])
        for row in payloads(run, "focus_portion_selected")
    }


@pytest.mark.parametrize(
    "refresh,veto_rounds,expected",
    [(1, {1, 3}, [2, 4]), (2, {1, 3, 4}, [5])],
    ids=["N1-promotion-after-a-refresh", "N2-promotion-mid-chain"],
)
async def test_a_promotion_resets_the_count_and_starts_a_new_cycle(
    tmp_path, monkeypatch, refresh, veto_rounds, expected
):
    """`stable` observe; the final of each `veto_rounds` round is vetoed
    (`retained`, counted), every other round promotes. Round 2 promotes:
    no refresh until N rounds have closed on its finalist's measurement.

    N = 2: round 1 counted 1 of 2, so without the Promotion round 3 would
    refresh (the uncut control, `test_n2_refreshes_only_at_round_3`); it
    does not, and the refresh comes at round 5 over rounds 3 and 4.
    N = 1: round 2 refreshes and then promotes; round 3 does not refresh,
    round 4 does. Round 3 starts a new visitation chain (ADR-0012: a
    Promotion begins a new cycle, a refresh does not): its cycle restarts at
    1 and revisits c1, while round 4's refresh carries c1 and goes on."""
    from tests.campaign_drivers import OBSERVE
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    flag = tmp_path / "veto.flag"
    _phase_hooks(monkeypatch, veto=flag, veto_rounds=veto_rounds)
    rounds = 5 if refresh == 2 else 4
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _veto_scorer(flag)},
        family14_settings={"stability": "stable", "refresh": refresh},
        campaign={"rounds": rounds},
    )
    assert_clean_run(run, snapshot)
    outcomes = ["retained" if n in veto_rounds else "promoted" for n in range(1, rounds + 1)]
    assert [row["outcome"] for row in payloads(run, "round_closed")] == outcomes
    promoted = "m-r02-final-01-a01"
    bound = payloads(run, "baseline_refresh_bound")
    assert [row["round"] for row in bound] == expected
    baselines = _by_round(run, "baseline_bound")
    if refresh == 2:
        assert list(baselines.values()) == [
            CHAMPION0,
            CHAMPION0,
            promoted,
            promoted,
            "m-r05-refresh",
        ]
        (row,) = bound
        assert row["counted_rounds"] == {"first": 3, "last": 4}
        assert row["replaced"]["measurement_id"] == promoted
    else:
        assert list(baselines.values()) == [CHAMPION0, "m-r02-refresh", promoted, "m-r04-refresh"]
        assert [row["counted_rounds"] for row in bound] == [
            {"first": 1, "last": 1},
            {"first": 3, "last": 3},
        ]
        assert [row["replaced"]["measurement_id"] for row in bound] == [CHAMPION0, promoted]
        portions = _portions(run)
        assert portions == {
            1: (["c1"], 1),
            2: (["c1"], 2),  # the refresh carries c1: the cycle goes on
            3: (["c1"], 1),  # the Promotion restarts the chain: c1 again, cycle 1
            4: (["c1"], 2),  # the next refresh carries round 3's c1
        }


async def test_an_unstable_retained_round_resets_the_count(tmp_path, monkeypatch):
    """`unstable` observe, N = 1; round 1's final is vetoed and closes
    `retained` on the fresh final Champion, which becomes round 2's
    baseline: the count restarts and round 2 does not refresh. The control
    is the matrix cell `stable-first_accepted-observe-N1-fp0` (the same
    `_veto_scorer`, observe and N = 1): there the retained round under
    `stable` keeps `m-r00-champion` and round 2 refreshes it."""
    from tests.campaign_drivers import OBSERVE
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    flag = tmp_path / "veto.flag"
    _phase_hooks(monkeypatch, veto=flag, veto_rounds={1})
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _veto_scorer(flag)},
        family14_settings={"stability": "unstable", "refresh": 1},
        campaign={"rounds": 2},
    )
    assert_clean_run(run, snapshot)
    assert snapshot.identity["policy"]["baseline_refresh_after_rounds"] == 1
    assert [row["outcome"] for row in payloads(run, "round_closed")] == ["retained", "promoted"]
    assert _by_round(run, "selection_decided")[1] == "m-r01-final-01-champion"
    assert _by_round(run, "baseline_bound") == {1: CHAMPION0, 2: "m-r01-final-01-champion"}
    assert not payloads(run, "baseline_refresh_bound")
    assert not payloads(run, "baseline_refresh_completed")
    assert snapshot.refreshes == ()


async def test_a_refresh_resets_the_count(tmp_path, monkeypatch):
    """N = 2, five `stalled` rounds: round 3 refreshes over rounds 1-2, and
    the count restarts on `m-r03-refresh`, so round 4 does not refresh
    (it would on a count that kept rounds 1-3) and round 5 does, over 3-4."""
    run, snapshot = await _refresh_run(tmp_path, monkeypatch, 2, rounds=5)
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["stalled"] * 5
    bound = payloads(run, "baseline_refresh_bound")
    assert [(row["round"], row["counted_rounds"]) for row in bound] == [
        (3, {"first": 1, "last": 2}),
        (5, {"first": 3, "last": 4}),
    ]
    assert [row["replaced"]["measurement_id"] for row in bound] == [CHAMPION0, "m-r03-refresh"]
    assert list(_by_round(run, "baseline_bound").values()) == [
        CHAMPION0,
        CHAMPION0,
        "m-r03-refresh",
        "m-r03-refresh",
        "m-r05-refresh",
    ]
    assert audit(run)["mismatches"] == []


async def test_continue_between_rounds_keeps_the_count(tmp_path, monkeypatch):
    """N = 2, three `stalled` rounds, twice with one `_Resettable` Target
    configuration each: uncut (the control), and cut after round 1's close,
    the Target's settings changed and the comparable change accepted with
    `continue`. `continue` keeps every complete measurement (PDR-0016), so the
    count is kept: round 2 rebinds nothing, `m-r00-champion` keeps its plain id,
    and round 3 refreshes exactly as in the control."""
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import EDITED, _continue, _cut_hook, _Resettable
    from tests.campaign_fakes import Crash

    def refreshes(run):
        return [
            (row["round"], row["counted_rounds"], row["replaced"]["measurement_id"])
            for row in payloads(run, "baseline_refresh_bound")
        ]

    options = {"family14_settings": {"refresh": 2}, "campaign": {"rounds": 3}}
    control_root = tmp_path / "control"
    control_root.mkdir()
    control, _ = await family14_run(
        control_root,
        monkeypatch,
        NO_FINALISTS,
        adapter=_Resettable(used=EDITED["used"]),
        **options,
    )
    assert refreshes(control) == [(3, {"first": 1, "last": 2}, CHAMPION0)]

    root = tmp_path / "cut"
    root.mkdir()
    adapter = _Resettable(used=EDITED["used"])
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_round_closed")
        with pytest.raises(Crash):
            await family14_run(root, patch, NO_FINALISTS, adapter=adapter, **options)
    run = root / "run"
    assert [(p["round"], p["outcome"]) for p in payloads(run, "round_closed")] == [(1, "stalled")]
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(root, monkeypatch, NO_FINALISTS, adapter=adapter, **options)
    run, snapshot = await family14_run(
        root,
        monkeypatch,
        NO_FINALISTS,
        adapter=adapter,
        authorizations=[_continue(refused.value)],
        **options,
    )
    assert snapshot.finished["outcome"] == "completed"
    assert journal_consistent(run).finished == snapshot.finished
    (applied,) = payloads(run, "authorization_applied")
    assert applied["effect"] == "continue"
    assert len(snapshot.epochs) == 2
    assert not payloads(run, "measurement_rebound")
    assert [p["outcome"] for p in payloads(run, "round_closed")] == ["stalled"] * 3
    assert list(_by_round(run, "baseline_bound").values()) == [
        CHAMPION0,
        CHAMPION0,
        "m-r03-refresh",
    ]
    assert refreshes(run) == refreshes(control)
    events = [row["event"] for row in _records(run / "campaign.jsonl")]
    assert events.index("authorization_applied") < events.index("baseline_refresh_bound")
    assert audit(run)["mismatches"] == []


# Counted closures -----------------------------------------------------------------
# `stalled`: the runs above and the `unstable` cells; `stable` `retained`: the
# `stable` cells; `clean`: `test_genuine_clean_refresh_run_audits_clean` (the
# refresh audit module). The rest here, each genuine.


class _ExhaustFirstChampionC1:
    """Mixin: the first Champion instance (round 1's baseline) exhausts every
    c1 sample with `target_failed`; every later instance is unaffected."""

    champions = 0

    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion":
            self.champions += 1
            if self.champions == 1:
                instance._lookup = lambda session: (
                    "target_failed" if session.case_id == "c1" else "completed"
                )
        return instance


def _invalid_reflector():
    import json

    group = {
        "root_cause": "r",
        "suggested_fix": "f",
        "evidence_ids": ["obs:unknown:1"],
        "affected_cases": ["c1"],
        "target_paths": ["skills/SKILL.md"],
        "counterevidence": "c",
        "regression_risks": "r",
        "why_this_layer": "w",
    }
    return (
        "print(" + repr(json.dumps({"status": "reflected", "note": "n", "groups": [group]})) + ")\n"
    )


@pytest.mark.parametrize(
    "closure", ["unplanned-declined", "unplanned-invalid", "unscored", "unmeasurable"]
)
async def test_each_baseline_keeping_closure_counts(tmp_path, monkeypatch, closure):
    """N = 1, two rounds: round 1 closes `closure` on `m-r00-champion`, which
    counts, so round 2 refreshes it.

    `unplanned`: the Reflector declines, or returns a group citing unknown
    evidence. `unscored`: the editor declines, so the round's Attempt has no
    look. `unmeasurable`: every baseline sample of the failing c1 is exhausted
    below the (lenient) thresholds, so the excluded pairs empty the only failing
    cohort; the refresh's own instance measures c1 normally."""
    from functools import partial

    from tests import campaign_drivers as fixtures
    from tests.campaign_drivers import (
        DECLINING_EDITOR,
        DECLINING_REFLECTOR,
        EDITED,
        LENIENT,
        OBSERVE,
    )
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run
    from tests.tree_evolve_support import write_workspace

    kwargs = {}
    campaign = {"rounds": 2}
    if closure == "unplanned-declined":
        kwargs["reflector"] = DECLINING_REFLECTOR
    elif closure == "unplanned-invalid":
        kwargs["reflector"] = _invalid_reflector()
    elif closure == "unscored":
        monkeypatch.setattr(
            fixtures, "write_workspace", partial(write_workspace, editor_script=DECLINING_EDITOR)
        )
    else:
        adapter_class = type("Exhausting", (_ExhaustFirstChampionC1, FakeAdapter), {})
        kwargs["adapter"] = adapter_class(used=EDITED["used"])
        campaign["indeterminate"] = LENIENT
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        OBSERVE,
        family14_settings={"refresh": 1},
        campaign=campaign,
        **kwargs,
    )
    assert_clean_run(run, snapshot)
    outcome = closure.split("-")[0]
    closed = payloads(run, "round_closed")
    assert closed[0]["outcome"] == outcome
    assert _by_round(run, "baseline_bound") == {1: CHAMPION0, 2: "m-r02-refresh"}
    (bound,) = payloads(run, "baseline_refresh_bound")
    assert (bound["round"], bound["counted_rounds"], bound["replaced"]["measurement_id"]) == (
        2,
        {"first": 1, "last": 1},
        CHAMPION0,
    )
    assert [row["round"] for row in payloads(run, "baseline_refresh_completed")] == [2]
    if outcome == "unmeasurable":
        portion = _by_round(run, "focus_portion_selected", "reason")
        assert portion[1] == "unmeasurable"
        excluded = {row["measurement_id"]: row["count"] for row in payloads(run, "pairs_excluded")}
        assert excluded == {CHAMPION0: 1}
        # The refresh's instance measured c1: its round focuses on it.
        assert portion[2] == "selected"


# Replacement ------------------------------------------------------------------
# The `stable` cells above: a later final cites the refresh as its stored
# Champion, its comparison reads the refresh's rows (and the audit recomputes
# it), and grouping ran on the refresh binding.


class _RefreshExhaustsC2R1:
    """Mixin: a Champion instance materialized while `armed` exists (between the
    refresh hooks) exhausts the c2 sample of repeat 1 (`target_failed` on every
    retry); every other sample and instance is unaffected."""

    armed = None

    def instance_for(self, candidate, instance_id, boot_id):
        from tests.campaign_drivers import MANIFEST_SEED

        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind == "champion" and self.armed.exists():
            instance._lookup = lambda session: (
                "target_failed"
                if session.case_id == "c2" and session.requested_seed - MANIFEST_SEED == 1
                else "completed"
            )
        return instance


async def test_a_refresh_excludes_its_exhausted_pair_and_a_later_final_removes_it(
    tmp_path, monkeypatch
):
    """`stable` statistical, N = 1, `fresh_parent: false`, four repeats: round 1
    is `retained`. Round 2's refresh exhausts one pair, (c2, 1), below the
    lenient thresholds: it writes `pairs_excluded` before `baseline_complete`
    and completes (ADR-0016). Round 2's stable final then removes that pair on
    both sides (`removed_pairs`, H5's stored-side contrast); round 1's final,
    on `m-r00-champion`, removed nothing."""
    from tests.campaign_drivers import EDITED, LENIENT, _RepeatTagged
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    flag, during = tmp_path / "veto.flag", tmp_path / "refresh.flag"
    _phase_hooks(monkeypatch, veto=flag, veto_rounds={1}, during_refresh=during)
    adapter = type("Exhausting", (_RefreshExhaustsC2R1, _RepeatTagged), {"armed": during})(
        used=EDITED["used"]
    )
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        _cell_config("statistical", False, _veto_scorer(flag)),
        adapter=adapter,
        family14_settings={"stability": "stable", "refresh": 1},
        campaign={"rounds": 2, "indeterminate": LENIENT},
    )
    assert_clean_run(run, snapshot)
    assert [row["outcome"] for row in payloads(run, "round_closed")] == ["retained", "promoted"]
    (excluded,) = payloads(run, "pairs_excluded")
    assert (excluded["measurement_id"], excluded["count"]) == ("m-r02-refresh", 1)
    assert [(row["case_id"], int(row["sample_id"])) for row in excluded["pairs"]] == [("c2", 1)]
    order = [
        (row["event"], row["payload"].get("measurement_id"))
        for row in _records(run / "campaign.jsonl")
        if row["event"] in ("pairs_excluded", "baseline_complete", "baseline_refresh_completed")
    ]
    assert order == [
        ("baseline_complete", CHAMPION0),
        ("pairs_excluded", "m-r02-refresh"),
        ("baseline_complete", "m-r02-refresh"),
        ("baseline_refresh_completed", "m-r02-refresh"),
    ]
    first, second = payloads(run, "global_comparison_completed")
    assert (first["champion_measurement_id"], first["removed_pairs"]) == (CHAMPION0, [])
    assert second["champion_measurement_id"] == "m-r02-refresh"
    assert (second["removed_pairs"], second["removed_cases"]) == ([["c2", 1]], [])
    assert second["accepted"] is True


async def test_under_observe_the_refresh_renews_the_error_pool(tmp_path, monkeypatch):
    """`stable` observe, N = 1: round 1 is `retained`; `m-r00-champion` fails
    only c1. Round 2's refresh also fails c2 (the scorer sees it only during the
    refresh), and round 2 groups on the refresh: its error pool holds c2, and
    with c1 visited in the cycle the portion is c2 (cycle 1). Without the
    refresh the round reuses round 1's grouping and revisits c1 in cycle 2
    (H6's `test_next_round_baseline_grouping_and_visitation_are_real`)."""
    from tests.campaign_drivers import OBSERVE
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    flag, during = tmp_path / "veto.flag", tmp_path / "refresh.flag"
    _phase_hooks(monkeypatch, veto=flag, veto_rounds={1}, during_refresh=during)
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _veto_scorer(flag, refresh_fails=during)},
        family14_settings={"stability": "stable", "refresh": 1},
        campaign={"rounds": 2},
    )
    assert_clean_run(run, snapshot)
    assert snapshot.identity["policy"]["gate"]["mode"] == "observe"
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"
    grouping = _by_round(run, "grouping_started", "baseline_binding")
    assert grouping == {
        1: _complete_reference(run, 1)["content_digest"],
        2: _complete_reference(run, 2)["content_digest"],
    }
    portions = payloads(run, "focus_portion_selected")
    assert [(row["case_ids"], row["cycle"]) for row in portions] == [(["c1"], 1), (["c2"], 1)]
    assert [row["baseline_binding"] for row in portions] == [grouping[1], grouping[2]]


# N = 0 ------------------------------------------------------------------------
# `baseline_refresh: off` in the public report and status is H12's (maintainer
# ruling M1, F0 §13); here N = 0 is shown on the journal.

#: The campaign journal's `(event, round, measurement_id)` order, as
#: `event@round:measurement_id` tokens, of two N = 0 cells (`_cell_run` with
#: `refresh=0`), recorded on the H6 merge `a31ed2487` -- the engine before H7 --
#: by running the same scenario helpers there (whose driver has no `refresh`
#: setting; its default is 0). Never regenerate it from a later engine.
H6_EVENT_ORDER = {
    "stable-first_accepted-observe-N1-fp0": (
        "campaign_created baseline_bound@1:m-r00-champion measurement_bound@1:m-r00-champion "
        "stage_opened:m-r00-champion instance_attested:m-r00-champion "
        "baseline_complete@1:m-r00-champion grouping_started@1 grouping_finished@1 "
        "focus_portion_selected@1 reflection_started@1 reflection_finished@1 "
        "gate_scope_selected@1 attempt_opened@1 attempt_input_frozen@1 editor_started "
        "editor_finished attempt_sealed affected_cases_selected@1 "
        "measurement_bound@1:m-r01-a01 comparison_bound@1 noise_calibration_bound@1 "
        "noise_calibration_completed@1 stage_opened:m-r01-a01 instance_attested:m-r01-a01 "
        "comparison_look_evaluated@1 attempt_closed finalists_bound@1 "
        "final_measurement_bound@1:m-r01-final-01-a01 stage_opened:m-r01-final-01-a01 "
        "instance_attested:m-r01-final-01-a01 "
        "final_measurement_completed@1:m-r01-final-01-a01 global_comparison_completed@1 "
        "selection_decided@1:m-r00-champion round_closed@1 baseline_bound@2:m-r00-champion "
        "baseline_complete@2:m-r00-champion focus_portion_selected@2 reflection_started@2 "
        "reflection_finished@2 gate_scope_selected@2 attempt_opened@2 attempt_input_frozen@2 "
        "editor_started editor_finished attempt_sealed affected_cases_selected@2 "
        "measurement_bound@2:m-r02-a01 comparison_bound@2 noise_calibration_bound@2 "
        "noise_calibration_completed@2 stage_opened:m-r02-a01 instance_attested:m-r02-a01 "
        "comparison_look_evaluated@2 attempt_closed finalists_bound@2 "
        "final_measurement_bound@2:m-r02-final-01-a01 stage_opened:m-r02-final-01-a01 "
        "instance_attested:m-r02-final-01-a01 "
        "final_measurement_completed@2:m-r02-final-01-a01 global_comparison_completed@2 "
        "selection_decided@2:m-r02-final-01-a01 round_closed@2 campaign_finished "
    ),
    "unstable-all_attempts-statistical-N2-fp0": (
        "campaign_created baseline_bound@1:m-r00-champion measurement_bound@1:m-r00-champion "
        "stage_opened:m-r00-champion instance_attested:m-r00-champion "
        "stage_opened:m-r00-champion baseline_complete@1:m-r00-champion grouping_started@1 "
        "grouping_finished@1 focus_portion_selected@1 reflection_started@1 "
        "reflection_finished@1 gate_scope_selected@1 attempt_opened@1 attempt_input_frozen@1 "
        "editor_started editor_finished attempt_sealed affected_cases_selected@1 "
        "measurement_bound@1:m-r01-a01 comparison_bound@1 stage_opened:m-r01-a01 "
        "instance_attested:m-r01-a01 comparison_look_evaluated@1 attempt_closed "
        "selection_decided@1 round_closed@1 baseline_bound@2:m-r00-champion "
        "baseline_complete@2:m-r00-champion focus_portion_selected@2 reflection_started@2 "
        "reflection_finished@2 gate_scope_selected@2 attempt_opened@2 attempt_input_frozen@2 "
        "editor_started editor_finished attempt_sealed affected_cases_selected@2 "
        "measurement_bound@2:m-r02-a01 comparison_bound@2 stage_opened:m-r02-a01 "
        "instance_attested:m-r02-a01 comparison_look_evaluated@2 attempt_closed "
        "selection_decided@2 round_closed@2 baseline_bound@3:m-r00-champion "
        "baseline_complete@3:m-r00-champion focus_portion_selected@3 reflection_started@3 "
        "reflection_finished@3 gate_scope_selected@3 attempt_opened@3 attempt_input_frozen@3 "
        "editor_started editor_finished attempt_sealed affected_cases_selected@3 "
        "measurement_bound@3:m-r03-a01 comparison_bound@3 stage_opened:m-r03-a01 "
        "instance_attested:m-r03-a01 comparison_look_evaluated@3 stage_opened:m-r03-a01 "
        "comparison_look_evaluated@3 attempt_closed finalists_bound@3 "
        "final_measurement_bound@3:m-r03-final-01-champion "
        "final_measurement_bound@3:m-r03-final-01-a01 stage_opened:m-r03-final-01-champion "
        "instance_attested:m-r03-final-01-champion stage_opened:m-r03-final-01-a01 "
        "instance_attested:m-r03-final-01-a01 stage_opened:m-r03-final-01-champion "
        "stage_opened:m-r03-final-01-a01 "
        "final_measurement_completed@3:m-r03-final-01-champion "
        "final_measurement_completed@3:m-r03-final-01-a01 global_comparison_completed@3 "
        "selection_decided@3:m-r03-final-01-a01 round_closed@3 campaign_finished "
    ),
}


def _with_connection_preflight(order):
    """H10 (F0 R57, plan Task 7): the recorded H6 order plus the run's one
    connection preflight, `connection_preflight_started` and `_finished`, right
    before the baseline's first `stage_opened`. Nothing else moves."""

    tokens = order.split()
    at = tokens.index("stage_opened:m-r00-champion")
    return [
        *tokens[:at],
        "connection_preflight_started",
        "connection_preflight_finished",
        *tokens[at:],
    ]


def _event_order(run):
    tokens = []
    for row in _records(run / "campaign.jsonl"):
        token, payload = row["event"], row["payload"]
        if payload.get("round") is not None:
            token += f"@{payload['round']}"
        if payload.get("measurement_id") is not None:
            token += f":{payload['measurement_id']}"
        tokens.append(token)
    return tokens


@pytest.mark.parametrize("cell", [CELLS[3], CELLS[4]], ids=_cell_id)
async def test_n0_cells_write_no_refresh_and_keep_the_h6_event_order(tmp_path, monkeypatch, cell):
    """The same scenario as the cell, N = 0: the round that refreshes under N > 0
    binds the stored baseline, no refresh record is written, and the whole
    journal's event order equals the H6 engine's."""
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run

    run, snapshot = await _cell_run(tmp_path, monkeypatch, cell, refresh=0)
    assert_clean_run(run, snapshot)
    assert snapshot.identity["policy"]["baseline_refresh_after_rounds"] == 0
    assert not payloads(run, "baseline_refresh_bound")
    assert not payloads(run, "baseline_refresh_completed")
    assert snapshot.refreshes == ()
    assert set(_by_round(run, "baseline_bound").values()) == {CHAMPION0}
    assert _event_order(run) == _with_connection_preflight(H6_EVENT_ORDER[_cell_id(cell)])
