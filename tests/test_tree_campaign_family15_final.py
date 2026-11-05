"""Real final producers: full coverage, shared Champion cost and automatic closure."""

import pytest

from evolution.tree.campaign_projections import verify_evidence
from tests.campaign_drivers import _records
from tests.test_tree_campaign_family15_completion import completion_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("round_stop", ["first_accepted", "all_attempts"])
@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_automatic_final_completes_and_promotes(
    tmp_path, monkeypatch, stability, round_stop, verification
):
    run = await completion_run(
        tmp_path,
        monkeypatch,
        stability=stability,
        verification=verification,
        stop="result",
        campaign_options={"attempts_per_round": 2, "round_stop": round_stop},
    )
    records = _records(run / "campaign.jsonl")

    def payloads(event):
        return [r["payload"] for r in records if r["event"] == event]

    expected = 1 if round_stop == "first_accepted" else 2
    assert len(payloads("global_comparison_completed")) == expected
    assert all(p["accepted"] for p in payloads("global_comparison_completed"))
    assert len(payloads("selection_decided")) == 1
    assert payloads("round_closed")[0]["outcome"] == "promoted"
    assert payloads("campaign_finished")[0]["outcome"] == "completed"
    assert len(payloads("champion_final_bound")) == (1 if stability == "unstable" else 0)
    for final in payloads("final_measurement_bound"):
        rows = _records(
            run / "measurements" / final["candidate"]["measurement_id"] / "events.jsonl"
        )
        assert sum(r["event"] == "sample_imported" for r in rows) == (
            2 if verification == "reuse" else 0
        )
        assert sum(r["event"] == "rollout_started" for r in rows) == (
            0 if verification == "reuse" else 2
        )
    if round_stop == "all_attempts":
        barrier = next(r["sequence"] for r in records if r["event"] == "finalists_bound")
        assert all(r["sequence"] < barrier for r in records if r["event"] == "attempt_closed")
        assert all(
            r["sequence"] > barrier for r in records if r["event"] == "final_measurement_bound"
        )
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_shared_champion_interleaves_missing_work_once(tmp_path, monkeypatch, verification):
    from tests.campaign_drivers import _dispatch_order, _only_c1

    seen = _dispatch_order(monkeypatch)
    run = await completion_run(
        tmp_path,
        monkeypatch,
        stability="unstable",
        verification=verification,
        stop="result",
        used=_only_c1,
        campaign_options={"attempts_per_round": 2, "max_concurrency": 1},
    )
    suffix = "completion" if verification == "reuse" else "final"
    champion = "m-r01-final-champion"
    expected = (
        [(champion, "c1", 1)]
        if verification == "reuse"
        else [("m-r01-a01-final", "c1", 1), ("m-r01-a02-final", "c1", 1), (champion, "c1", 1)]
    )
    expected += [
        (f"m-r01-a01-{suffix}", "c2", 1),
        (f"m-r01-a02-{suffix}", "c2", 1),
        (champion, "c2", 1),
    ]
    assert seen == [expected]
    rows = _records(run / "measurements" / champion / "events.jsonl")
    assert sum(r["event"] == "rollout_started" for r in rows) == 2
    assert verify_evidence(run)["mismatches"] == []


async def test_fresh_champion_global_admission_refusal_prevents_selection(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign

    original = _Campaign.refused_in

    def refusal(self, mid):
        if "final-champion" in mid:
            return self.attested_chain(mid)[0]["instance_id"]
        return original(self, mid)

    monkeypatch.setattr(_Campaign, "refused_in", refusal)
    run = await completion_run(tmp_path, monkeypatch, stability="unstable", stop="result")
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "campaign_suspended"
    assert not any(
        r["event"] in {"global_comparison_completed", "selection_decided"} for r in records
    )


@pytest.mark.parametrize("mode", ["point", "unfiltered"])
@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_full_candidate_critical_guard_precedes_global_mode(
    tmp_path, monkeypatch, mode, verification
):
    from tests.campaign_drivers import TWO_CASE_SCORER, _only_c1

    scorer = TWO_CASE_SCORER.replace(
        '"critical_pass": True',
        '"critical_pass": not (payload["oracle"].get("pass") and "edited" in skill)',
    )
    run = await completion_run(
        tmp_path,
        monkeypatch,
        scorer=scorer,
        used=_only_c1,
        mode=mode,
        verification=verification,
        stop="result",
    )
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "global_comparison_completed")
    assert not result["accepted"] and result["reason"] == "critical_failed"
    assert not any(r["event"] == "campaign_suspended" for r in records)
    assert (
        next(r["payload"] for r in records if r["event"] == "round_closed")["outcome"] == "retained"
    )
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("side", ["candidate", "champion"])
async def test_final_component_drift_uses_existing_suspension_and_repair(
    tmp_path, monkeypatch, side
):
    from tests.campaign_drivers import _drift_after, _Drifts, _map_of_the_tree, _only_c1

    adapter = _Drifts(components=_map_of_the_tree, used=_only_c1)
    adapter.kind = side
    options = dict(
        adapter=adapter, used=_only_c1, verification="fresh", stability="unstable", stop="result"
    )
    with monkeypatch.context() as patch:
        _drift_after(patch, adapter, seam="after_independent_final_bound")
        run = await completion_run(tmp_path, patch, **options)
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "campaign_suspended"
    assert records[-1]["payload"]["reason"] == "target_components_changed"
    assert not any(r["event"] == "global_comparison_completed" for r in records)
    assert verify_evidence(run)["mismatches"] == []
    adapter.on = False
    run = await completion_run(tmp_path, monkeypatch, **options)
    assert _records(run / "campaign.jsonl")[-1]["event"] == "campaign_finished"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("global_mode", ["off", "point", "bound"])
async def test_real_global_modes_use_shared_exact_result(tmp_path, monkeypatch, global_mode):
    import json

    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family15_selection import data

    original = fixtures.evolve

    async def configure(path, *args, **kwargs):
        document = json.loads(path.read_text())
        document["campaign"]["gate"]["global"] = global_mode
        path.write_text(json.dumps(document))
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(fixtures, "evolve", configure)
    run = await completion_run(tmp_path, monkeypatch, mode="confidence", stop="result")
    result = next(
        r["payload"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] == "global_comparison_completed"
    )
    body = data(run, result)
    assert body["scopes"][0]["rule"] == global_mode
    assert body["coverage"]["candidate"] == {"required": 2, "settled": 2, "admissible": 2}
    assert result["accepted"]
    assert verify_evidence(run)["mismatches"] == []
