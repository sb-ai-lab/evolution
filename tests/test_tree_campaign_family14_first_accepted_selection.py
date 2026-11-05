"""H6 terminal selection across historical final bindings, with real scoring."""

from __future__ import annotations

import hashlib

import pytest

from evolution.tree.campaign_schema import SchemaError, current_members
from tests.campaign_drivers import _events, _records
from tests.campaign_fakes import Family14Life
from tests.test_tree_campaign_family14_first_accepted_run import scenario
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "skip_first,losers,k",
    [(False, 0, 5), (False, 2, 5), (True, 1, 3), (False, 2, 3), (False, 3, 3), (True, 3, 3)],
)
async def test_selection_covers_every_final_and_stops_at_winner_or_k(
    tmp_path, monkeypatch, stability, skip_first, losers, k
):
    options = scenario(
        tmp_path, monkeypatch, stability=stability, skip_first=skip_first, losers=losers
    )
    options["campaign"].update(attempts_per_round=k, rounds=2)
    # A critical global loser has a higher fresh score than the later winner.
    flag = tmp_path / "critical.flag"
    options["config"]["scorer"] = options["config"]["scorer"].replace(
        '"score": score',
        f'"score": (0.95 if pathlib.Path({str(flag)!r}).exists() else 0.7) if edited else score',
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    state = snapshot.rounds[1]
    selection = payloads(run, "selection_decided")[0]
    opened = min(k, losers + 1 + skip_first)
    accepted = losers < k - skip_first
    assert len(state.attempts) == opened
    assert payloads(run, "round_closed")[0]["attempts_opened"] == opened
    assert len(selection["ranking"]) == len(state.finals) == opened - skip_first
    assert sum(row["accepted"] for row in selection["ranking"]) == accepted
    expected_mid = (
        f"m-r01-final-{len(state.finals):02d}-a{opened:02d}"
        if accepted
        else "m-r00-champion"
        if stability == "stable"
        else f"m-r01-final-{len(state.finals):02d}-champion"
    )
    assert selection["measurement_id"] == expected_mid
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == expected_mid
    assert selection["attempt_id"] == (f"r01-a{opened:02d}" if accepted else None)
    assert payloads(run, "round_closed")[0]["outcome"] == ("promoted" if accepted else "retained")
    if accepted and losers:
        assert selection["ranking"][0]["score"] < selection["ranking"][1]["score"]
    expected_sequences = [
        state.cohort_completed[mid]["seq"]
        for final in state.finals
        for mid in current_members(state, final=final)
    ] + [state.global_comparisons[a]["seq"] for a in sorted(state.global_comparisons)]
    campaign = _records(run / "campaign.jsonl")
    cited = {ref["content_digest"] for ref in selection["evidence"]}
    lines = (run / "campaign.jsonl").read_bytes().splitlines(keepends=True)

    assert len(cited) == len(expected_sequences) == len(selection["evidence"])
    assert [ref["content_digest"] for ref in selection["evidence"]] == [
        hashlib.sha256(lines[seq - 1]).hexdigest() for seq in expected_sequences
    ]

    record = _events(run, "selection_decided")[0]
    before = Family14Life(campaign[: record["sequence"] - 1])
    for field, value in (
        ("ranking", selection["ranking"][:-1]),
        ("ranking", selection["ranking"] + selection["ranking"][:1]),
        ("measurement_id", "m-r01-final-09-a01"),
        ("evidence", selection["evidence"][:-1] + selection["evidence"][:1]),
        ("evidence", list(reversed(selection["evidence"]))),
    ):
        with pytest.raises(SchemaError):
            before.accept("selection_decided", **{**selection, field: value})
    # A later payload cannot bypass missing/open work in a genuine prefix.
    for event in ("attempt_opened", "final_measurement_completed", "global_comparison_completed"):
        last = [r for r in _events(run, event) if r["payload"]["round"] == 1][-1]
        unfinished = Family14Life(campaign[: last["sequence"] - (event != "attempt_opened")])
        with pytest.raises(SchemaError):
            unfinished.accept("selection_decided", **selection)
    if accepted and losers:
        old = state.attempts[f"r01-a{1 + skip_first:02d}"]
        with pytest.raises(SchemaError, match="another winner"):
            before.accept(
                "selection_decided",
                **{
                    **selection,
                    "attempt_id": old.attempt_id,
                    "candidate_digest": old.candidate_digest,
                    "dependency_digest": old.dependency_digest,
                    "measurement_id": f"m-r01-final-01-a{1 + skip_first:02d}",
                },
            )
    before.accept("selection_decided", **selection)
    with pytest.raises(SchemaError, match="already decided"):
        before.accept("selection_decided", **selection)
    close = _events(run, "round_closed")[0]
    closing = Family14Life(campaign[: close["sequence"] - 1])
    with pytest.raises(SchemaError, match="attempts_opened"):
        closing.accept("round_closed", **{**close["payload"], "attempts_opened": opened - 1})


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("stop", ["first_accepted", "all_attempts"])
async def test_k_gate_losers_and_zero_attempt_closures(tmp_path, monkeypatch, stability, stop):
    from tests.campaign_drivers import DECLINING_REFLECTOR, OBSERVE, _cohort_scorer

    flag = tmp_path / "critical.flag"
    flag.touch()
    settings = {"stability": stability, "round_stop": stop}
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _cohort_scorer(flag, critical=True)},
        family14_settings=settings,
        campaign={"attempts_per_round": 3, "parallel_attempts": 1},
    )
    assert len(payloads(run, "attempt_closed")) == 3
    assert not payloads(run, "finalists_bound")
    (selection,) = payloads(run, "selection_decided")
    assert (selection["reason"], selection["ranking"], selection["measurement_id"]) == (
        "no_finalists",
        [],
        None,
    )
    assert payloads(run, "round_closed")[0]["attempts_opened"] == 3

    root = tmp_path / "unplanned"
    root.mkdir()
    run, _ = await family14_run(
        root, monkeypatch, family14_settings=settings, reflector=DECLINING_REFLECTOR
    )
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == ("unplanned", 0)
    assert not payloads(run, "selection_decided")
    root = tmp_path / "clean"
    root.mkdir()
    run, _ = await family14_run(
        root, monkeypatch, family14_settings=settings, campaign={"rounds": 2}
    )
    assert [(p["outcome"], p["attempts_opened"]) for p in payloads(run, "round_closed")] == [
        ("promoted", 1),
        ("clean", 0),
    ]


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "cut", ["after_selection_decided", "after_pending", "round_closed", "after_round_closed"]
)
async def test_multifinal_selection_resumes_one_promotion(tmp_path, monkeypatch, stability, cut):
    from tests.campaign_drivers import _cut_before_round_closed, _cut_hook, _cut_store_hook
    from tests.campaign_fakes import Crash

    options = scenario(tmp_path, monkeypatch, stability=stability)
    with monkeypatch.context() as patch:
        if cut == "after_pending":
            _cut_store_hook(patch, cut)
        elif cut == "round_closed":
            _cut_before_round_closed(patch)
        else:
            _cut_hook(patch, cut)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    prefix = _records(run / "campaign.jsonl")
    selection = payloads(run, "selection_decided")[0]
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _records(run / "campaign.jsonl")[: len(prefix)] == prefix
    assert payloads(run, "selection_decided") == [selection]
    assert len(payloads(run, "attempt_opened")) == 3
    assert len(payloads(run, "global_comparison_completed")) == 3
    ledger = _records(run / "artifacts.jsonl")
    assert sum(row["event"] == "promotion_pending" for row in ledger) == 1
    assert sum(row["event"] == "promotion_committed" for row in ledger) == 1
    assert len(payloads(run, "round_closed")) == 1


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("rebound_final", [1, 3])
@pytest.mark.parametrize("winner", [False, True])
async def test_selection_preserves_historical_and_latest_rebound_ids(
    tmp_path, monkeypatch, stability, rebound_final, winner
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _cut_hook, _Resettable
    from tests.campaign_fakes import Crash

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=2 if winner else 3)
    options["campaign"].update(attempts_per_round=3, rounds=2)
    options["adapter"] = adapter = _Resettable(used=options["adapter"].used)
    with monkeypatch.context() as patch:
        _cut_hook(
            patch,
            "after_cohort_measurement_bound",
            call=rebound_final * (1 if stability == "stable" else 2),
        )
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, **options)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, authorizations=[_continue(refusal.value)], **options
    )
    state = snapshot.rounds[1]
    members = current_members(state, final=state.finals[rebound_final - 1])
    assert members[0].endswith("@e1")
    (selection,) = [p for p in payloads(run, "selection_decided") if p["round"] == 1]
    assert len(selection["ranking"]) == 3
    expected = (
        "m-r01-final-03-a03" + ("@e1" if rebound_final == 3 else "")
        if winner
        else "m-r00-champion"
        if stability == "stable"
        else "m-r01-final-03-champion" + ("@e1" if rebound_final == 3 else "")
    )
    assert selection["measurement_id"] == expected
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == expected
    source = state.cohort_completed[members[0]]
    assert source["record_digest"] in {ref["content_digest"] for ref in selection["evidence"]}


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("winner", [False, True])
async def test_cancellation_after_selection_finishes_the_committed_round(
    tmp_path, monkeypatch, stability, winner
):
    from evolution.tree.campaign import _Campaign

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1 if winner else 2)
    options["campaign"]["attempts_per_round"] = 2
    original = _Campaign.hook
    cancelled = []

    def hook(self, name):
        if name == "after_selection_decided":
            cancelled.append(True)
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", hook)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, cancelled=lambda: bool(cancelled), **options
    )
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == (
        "promoted" if winner else "retained",
        2,
    )
    assert snapshot.finished["reason"] == "cancelled"
    events = [row["event"] for row in _records(run / "campaign.jsonl")]
    assert (
        events.index("selection_decided")
        < events.index("round_closed")
        < events.index("campaign_finished")
    )
    assert (
        sum(row["event"] == "promotion_committed" for row in _records(run / "artifacts.jsonl"))
        == winner
    )


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_last_gate_loser_still_selects_prior_finals(tmp_path, monkeypatch, stability):
    from evolution.tree.campaign import _Campaign

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=3)
    options["campaign"]["attempts_per_round"] = 3
    original = _Campaign.hook
    flag = tmp_path / "critical.flag"

    def hook(self, name):
        original(self, name)
        if name == "after_attempt_input_frozen" and len(self.snapshot().rounds[1].attempts) == 3:
            flag.touch()

    monkeypatch.setattr(_Campaign, "hook", hook)
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    (selection,) = payloads(run, "selection_decided")
    assert selection["attempt_id"] is None
    assert [row["attempt_id"] for row in selection["ranking"]] == ["r01-a01", "r01-a02"]
    (closed,) = payloads(run, "round_closed")
    assert (closed["outcome"], closed["attempts_opened"]) == ("retained", 3)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("mode", ["point", "bound"])
async def test_k_numeric_global_losses_retain_with_all_rankings(
    tmp_path, monkeypatch, stability, mode
):
    from tests.campaign_drivers import _cohort_scorer, _statistical

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=2)
    options.pop("adapter")
    options["campaign"]["attempts_per_round"] = 2
    options["config"] = {
        **_statistical(mode),
        "scorer": _cohort_scorer(tmp_path / "critical.flag", flip=True),
    }
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    comparisons = payloads(run, "global_comparison_completed")
    assert len(comparisons) == 2
    assert [row["accepted"] for row in comparisons] == [False, False]
    assert {row["reason"] for row in comparisons} == {
        "global_point_not_improved" if mode == "point" else "global_bound_not_positive"
    }
    (selection,) = payloads(run, "selection_decided")
    assert [row["attempt_id"] for row in selection["ranking"]] == ["r01-a01", "r01-a02"]
    assert selection["measurement_id"] == (
        "m-r00-champion" if stability == "stable" else "m-r01-final-02-champion"
    )
    assert payloads(run, "round_closed")[0]["attempts_opened"] == 2
