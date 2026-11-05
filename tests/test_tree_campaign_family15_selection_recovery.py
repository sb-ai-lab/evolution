"""Final/selection cuts preserve paid rows, frozen results and one publication."""

from dataclasses import replace

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import EvolveRefused
from tests.campaign_drivers import _continue, _only_c1, _records, _Resettable
from tests.test_tree_campaign_family15_completion import completion_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("attempts", [1, 2])
async def test_last_first_accepted_attempt_resumes_owed_comparison_work(
    tmp_path, monkeypatch, verification, stability, attempts
):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family15_gate import GatePowerCut
    from tests.test_tree_campaign_family15_selection import data

    # Two Attempts both improve c1 but regress c2, so the first global rejection
    # opens the final permitted Attempt before the same pre-measurement cut.
    scorer = TWO_CASE_SCORER
    if attempts == 2:
        scorer = scorer.replace(
            '"score": 1.0 if passed else 0.0',
            '"score": (0.0 if payload["oracle"].get("pass") and "edited" in skill else 1.0 if passed else 0.0)',
        )
    options = dict(
        verification=verification,
        stability=stability,
        used=_only_c1,
        scorer=scorer,
        stop="result",
        campaign_options={"attempts_per_round": attempts, "round_stop": "first_accepted"},
    )
    hook = _Campaign.hook

    def cut(self, name):
        hook(self, name)
        if name == "after_comparison_bound" and len(self.snapshot().rounds[1].attempts) == attempts:
            raise GatePowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", cut)
        with pytest.raises(GatePowerCut):
            await completion_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    frozen = (run / "campaign.jsonl").read_bytes()
    before = _records(run / "campaign.jsonl")
    assert before[-1]["event"] == "comparison_bound"
    assert sum(r["event"] == "global_comparison_completed" for r in before) == attempts - 1
    if attempts == 2:
        assert not next(
            r["payload"]["accepted"] for r in before if r["event"] == "global_comparison_completed"
        )
    run = await completion_run(tmp_path, monkeypatch, **options)
    assert (run / "campaign.jsonl").read_bytes().startswith(frozen)
    records = _records(run / "campaign.jsonl")
    globals = [r["payload"] for r in records if r["event"] == "global_comparison_completed"]
    assert len(globals) == attempts
    assert [r["accepted"] for r in globals] == ([True] if attempts == 1 else [False, False])
    selection = next(r["payload"] for r in records if r["event"] == "selection_decided")
    assert [e["point"] for e in data(run, selection)["entries"]] == [
        {"format": "rational-hex/1", "numerator": "1", "denominator": "1" if attempts == 1 else "2"}
    ] * attempts
    assert [r["payload"]["outcome"] for r in records if r["event"] == "round_closed"] == [
        "promoted" if attempts == 1 else "retained"
    ]
    paid = [
        r
        for path in (run / "measurements").glob("*/events.jsonl")
        for r in _records(path)
        if r["event"] == "rollout_started"
    ]
    assert len(paid) == 2 + attempts * (
        1 + (1 if verification == "reuse" else 2) + (2 if stability == "unstable" else 0)
    )
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "cut",
    [
        "after_independent_final_bound",
        "after_global_comparison_completed",
        "after_selection_decided",
        "after_round_closed",
    ],
)
async def test_final_selection_cut_is_missing_only(
    tmp_path, monkeypatch, verification, stability, cut
):
    options = dict(
        verification=verification, stability=stability, campaign_options={"attempts_per_round": 2}
    )
    run = await completion_run(tmp_path, monkeypatch, stop=cut, hard=True, **options)
    before = (run / "campaign.jsonl").read_bytes()
    globals_before = [
        r for r in _records(run / "campaign.jsonl") if r["event"] == "global_comparison_completed"
    ]
    run = await completion_run(tmp_path, monkeypatch, stop="result", **options)
    assert (run / "campaign.jsonl").read_bytes().startswith(before)
    records = _records(run / "campaign.jsonl")
    assert [r for r in records if r["event"] == "global_comparison_completed"][
        : len(globals_before)
    ] == globals_before
    assert sum(r["event"] == "selection_decided" for r in records) == 1
    assert sum(r["event"] == "round_closed" for r in records) == 1
    assert len([r for r in records if r["event"] == "global_comparison_completed"]) == 2
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    for binding in [r["payload"] for r in records if r["event"] == "final_measurement_bound"]:
        rows = _records(
            run / "measurements" / binding["candidate"]["measurement_id"] / "events.jsonl"
        )
        assert sum(r["event"] == "rollout_started" for r in rows) == (
            0 if verification == "reuse" else 2
        )
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_after_bound_continue_uses_current_slots_without_rewriting_profile(
    tmp_path, monkeypatch, verification, stability
):
    adapter = _Resettable(used=_only_c1)
    options = dict(adapter=adapter, used=_only_c1, verification=verification, stability=stability)
    run = await completion_run(
        tmp_path, monkeypatch, stop="after_independent_final_bound", hard=True, **options
    )
    before = _records(run / "campaign.jsonl")
    binding = next(r for r in before if r["event"] == "final_measurement_bound")
    old = {p: p.read_bytes() for p in (run / "measurements").glob("*/events.jsonl")}
    adapter.seed_mode = "unpaired"
    adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, stop="result", **options)
    run = await completion_run(
        tmp_path, monkeypatch, stop="result", authorizations=[_continue(refused.value)], **options
    )
    assert all(p.read_bytes() == content for p, content in old.items())
    records = _records(run / "campaign.jsonl")
    assert [r for r in records if r["event"] == "final_measurement_bound"] == [binding]
    result = next(r["payload"] for r in records if r["event"] == "global_comparison_completed")
    assert result["accepted"]
    assert result["candidate"]["measurement_id"].endswith("@e1")
    rows = _records(run / "measurements" / result["candidate"]["measurement_id"] / "events.jsonl")
    assert sum(r["event"] == "rollout_started" for r in rows) == 2
    assert not any(r["event"] == "sample_imported" for r in rows)
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_continue_between_candidate_completions_preserves_mixed_epochs(
    tmp_path, monkeypatch, verification
):
    adapter = _Resettable(used=_only_c1)
    options = dict(
        adapter=adapter,
        used=_only_c1,
        stability="unstable",
        verification=verification,
        campaign_options={"attempts_per_round": 2},
    )
    cut = (
        "after_candidate_completion_completed"
        if verification == "reuse"
        else "after_final_candidate_completed"
    )
    run = await completion_run(tmp_path, monkeypatch, stop=cut, hard=True, **options)
    events = _records(run / "campaign.jsonl")
    completed = next(
        r["payload"]["source"]
        for r in events
        if r["event"] in {"candidate_completion_completed", "final_measurement_completed"}
    )
    path = run / "measurements" / completed["measurement_id"] / "events.jsonl"
    frozen = path.read_bytes()
    adapter.seed_mode = "unpaired"
    adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, stop="result", **options)
    run = await completion_run(
        tmp_path, monkeypatch, stop="result", authorizations=[_continue(refused.value)], **options
    )
    assert path.read_bytes() == frozen
    results = [
        r["payload"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] == "global_comparison_completed"
    ]
    assert results[0]["candidate"] == completed
    assert results[0]["reference"]["measurement_id"].endswith("@e1")
    assert results[1]["candidate"]["measurement_id"].endswith("@e1")
    assert verify_evidence(run)["mismatches"] == []


async def test_all_attempts_continue_between_final_bindings_uses_current_champion(
    tmp_path, monkeypatch
):
    adapter = _Resettable(used=_only_c1)
    options = dict(
        adapter=adapter,
        used=_only_c1,
        stability="unstable",
        campaign_options={"attempts_per_round": 2},
    )
    await completion_run(
        tmp_path, monkeypatch, stop="after_independent_final_bound", hard=True, **options
    )
    adapter.seed_mode = "unpaired"
    adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, stop="result", **options)
    run = await completion_run(
        tmp_path, monkeypatch, stop="result", authorizations=[_continue(refused.value)], **options
    )
    finals = [
        r["payload"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] == "final_measurement_bound"
    ]
    assert len(finals) == 2
    assert finals[1]["champion"]["measurement_id"].endswith("@e1")
    assert verify_evidence(run)["mismatches"] == []


async def test_publication_committed_before_closure_is_recovered_once(tmp_path, monkeypatch):
    from evolution.tree.artifacts.artifacts import ArtifactStore
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    promote = ArtifactStore.promote

    def cut(self, decision):
        promote(self, decision)
        raise GatePowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "promote", cut)
        with pytest.raises(GatePowerCut):
            await completion_run(tmp_path, patch, stop="result")
    run = await completion_run(tmp_path, monkeypatch, stop="result")
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "selection_decided" for r in records) == 1
    assert sum(r["event"] == "round_closed" for r in records) == 1
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_pending" for r in ledger) == 1
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    assert verify_evidence(run)["mismatches"] == []


async def test_joint_final_cut_rechecks_scorer_fault_before_any_completion(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.campaign_fakes import Crash, crash_after_the_joint_stage_once

    scorer = TWO_CASE_SCORER.replace(
        "passed = ",
        'if payload["case_id"] == "c2" and "edited" in skill:\n    sys.exit(1)\npassed = ',
    )
    options = dict(used=_only_c1, scorer=scorer, stability="unstable", stop="result")
    with monkeypatch.context() as patch:
        crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-a01-completion")
        with pytest.raises(Crash):
            await completion_run(tmp_path, patch, **options)
    run = await completion_run(tmp_path, monkeypatch, **options)
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "campaign_suspended"
    assert records[-1]["payload"]["owner"] == "scorer"
    assert not any(
        r["event"]
        in {
            "candidate_completion_completed",
            "final_measurement_completed",
            "global_comparison_completed",
        }
        for r in records
    )
    assert verify_evidence(run)["mismatches"] == []
