"""H6 immediate Attempt decisions, independent of sibling feedback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evolution.tree import campaign_policy
from evolution.tree.campaign_projections import inspect_view
from evolution.tree.campaign_schema import SchemaError
from evolution.tree.campaign_view import EvaluateLook, OpenAttempt, next_actions
from tests import test_tree_campaign_family14_run as driver
from tests.campaign_drivers import EDITED, LENIENT, OBSERVE, _cut_hook, _events, _records
from tests.campaign_fakes import (
    Crash,
    FakeAdapter,
    Family14Life,
    crash_after_the_stage_once,
)


@pytest.mark.parametrize(
    "feedback,stop,expected",
    [
        ("none", "all_attempts", False),
        ("sequential", "all_attempts", True),
        ("none", "first_accepted", True),
        ("sequential", "first_accepted", True),
    ],
)
def test_immediate_decision_predicate_follows_current_round_stop(feedback, stop, expected):
    policy = SimpleNamespace(attempt_feedback=feedback, round_stop=stop)
    assert campaign_policy.decides_at_once(policy) is expected


class _ExhaustCandidates(FakeAdapter):
    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind != "champion":
            instance._lookup = lambda session: "target_failed"
        return instance


async def _run(tmp_path, monkeypatch, *, stability, feedback, stop="first_accepted", **kwargs):
    """Use the genuine engine seam; H6's shared driver extension is Task 3."""
    original = driver.family14_document

    def document(base, **settings):
        return original(base, **settings, round_stop=stop)

    with monkeypatch.context() as patch:
        patch.setattr(driver, "family14_document", document)
        return await driver.family14_run(
            tmp_path,
            patch,
            {**OBSERVE, "gate": {"mode": "observe", "fresh_parent": False}},
            family14_settings={"stability": stability},
            campaign={
                "attempts_per_round": 2,
                "parallel_attempts": 1,
                "attempt_feedback": feedback,
                "indeterminate": kwargs.pop("indeterminate", LENIENT),
            },
            **kwargs,
        )


@pytest.mark.slow
@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("feedback", ["none", "sequential"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_completed_stage_keeps_its_slot_until_immediate_decision(
    tmp_path, monkeypatch, stability, feedback, exhausted
):
    adapter = (_ExhaustCandidates if exhausted else FakeAdapter)(used=EDITED["used"])
    with monkeypatch.context() as patch:
        cut = crash_after_the_stage_once(patch, measurement_prefix="m-r01-a01")
        with pytest.raises(Crash):
            await _run(tmp_path, patch, stability=stability, feedback=feedback, adapter=adapter)
    assert cut == ["m-r01-a01"]
    run = tmp_path / "run"
    view = inspect_view(run)
    policy = campaign_policy.CampaignPolicyV11.from_payload(view.snapshot.identity["policy"])
    assert next_actions(view, policy) == (EvaluateLook("r01-a01", 1),)
    assert view.lacking == (frozenset({"r01-a01"}) if exhausted else frozenset())
    life = Family14Life(_records(run / "campaign.jsonl"))
    parent_version, parent_digest = life.snapshot.champion
    with pytest.raises(SchemaError, match="parallel attempts bound"):
        life.accept(
            "attempt_opened",
            round=1,
            attempt_id="r01-a02",
            attempt_index=2,
            parent_version=parent_version,
            parent_digest=parent_digest,
        )


@pytest.mark.slow
@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("feedback", ["none", "sequential"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("crossing", [False, True])
async def test_exhausted_attempt_is_decided_threshold_first_before_next_input(
    tmp_path, monkeypatch, stability, feedback, resume, crossing
):
    adapter = _ExhaustCandidates(used=EDITED["used"])
    options = dict(stability=stability, feedback=feedback, adapter=adapter)
    if crossing:
        options["indeterminate"] = {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}}
    if resume:
        with monkeypatch.context() as patch:
            crash_after_the_stage_once(patch, measurement_prefix="m-r01-a01")
            with pytest.raises(Crash):
                await _run(tmp_path, patch, **options)
    if crossing:
        run, snapshot = await _run(tmp_path, monkeypatch, **options)
        assert snapshot.suspended is True
        assert snapshot.suspensions[-1]["reason"] == "target_unstable"
        assert not _events(run, "attempt_closed")
        assert not _events(run, "look_not_started")
        assert not _events(run, "selection_decided")
        assert [r["payload"]["attempt_id"] for r in _events(run, "attempt_opened")] == ["r01-a01"]
        assert [r["payload"]["attempt_id"] for r in _events(run, "attempt_input_frozen")] == [
            "r01-a01"
        ]
    else:
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_attempt_input_frozen", call=1 if resume else 2)
            with pytest.raises(Crash):
                await _run(tmp_path, patch, **options)
        run = tmp_path / "run"
        (closed,) = _events(run, "attempt_closed")
        first_input, second_input = _events(run, "attempt_input_frozen")
        (later,) = [r for r in _events(run, "attempt_opened") if r["payload"]["attempt_index"] == 2]
        assert (closed["payload"]["outcome"], closed["payload"]["reason"]) == (
            "insufficient_evidence",
            "incomplete_sample",
        )
        assert closed["sequence"] < later["sequence"] < second_input["sequence"]
        assert (
            first_input["payload"]["common_input_digest"]
            == second_input["payload"]["common_input_digest"]
        )
        if feedback == "none":
            assert second_input["payload"]["feedback"] == {"attempts": [], "omitted_attempts": 0}
            assert second_input["payload"]["source_refs"] == []
        else:
            assert second_input["payload"]["feedback"]["attempts"][0]["attempt_id"] == "r01-a01"


@pytest.mark.slow
@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_all_attempts_none_keeps_exhausted_attempts_held(tmp_path, monkeypatch, stability):
    with monkeypatch.context() as patch:
        crash_after_the_stage_once(patch, measurement_prefix="m-r01-a01")
        with pytest.raises(Crash):
            await _run(
                tmp_path,
                patch,
                stability=stability,
                feedback="none",
                stop="all_attempts",
                adapter=_ExhaustCandidates(used=EDITED["used"]),
            )
    view = inspect_view(tmp_path / "run")
    assert next_actions(
        view, campaign_policy.CampaignPolicyV11.from_payload(view.snapshot.identity["policy"])
    ) == (OpenAttempt(1, 2, "r01-a02"),)
    life = Family14Life(_records(tmp_path / "run" / "campaign.jsonl"))
    version, digest = life.snapshot.champion
    life.accept(
        "attempt_opened",
        round=1,
        attempt_id="r01-a02",
        attempt_index=2,
        parent_version=version,
        parent_digest=digest,
    )


class _FaultAndExhaustion(FakeAdapter):
    def instance_for(self, candidate, instance_id, boot_id):
        instance = super().instance_for(candidate, instance_id, boot_id)
        if candidate.kind != "champion":
            instance._lookup = lambda session: (
                "target_failed" if session.case_id == "c1" else "invalid_request"
            )
        return instance


@pytest.mark.slow
@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("feedback", ["none", "sequential"])
@pytest.mark.parametrize("resume", [False, True])
async def test_stage_fault_precedes_threshold_and_leaves_attempt_open(
    tmp_path, monkeypatch, stability, feedback, resume
):
    options = dict(
        stability=stability,
        feedback=feedback,
        adapter=_FaultAndExhaustion(used=EDITED["used"]),
        indeterminate={**LENIENT, "target": {"max_count": 0, "max_rate": "1"}},
    )
    if resume:
        with monkeypatch.context() as patch:
            crash_after_the_stage_once(patch, measurement_prefix="m-r01-a01")
            with pytest.raises(Crash):
                await _run(tmp_path, patch, **options)
    run, snapshot = await _run(tmp_path, monkeypatch, **options)
    assert snapshot.suspended is True
    (suspension,) = _events(run, "campaign_suspended")
    assert suspension["payload"]["evidence"]["evidence_kind"] == "fault_rows"
    assert suspension["payload"]["reason"] == "target_contract_violated"
    assert _events(run, "indeterminate_observed")
    assert not _events(run, "attempt_closed")
    assert not _events(run, "look_not_started")
    assert not _events(run, "selection_decided")
    assert len(_events(run, "attempt_opened")) == 1
    assert len(_events(run, "attempt_input_frozen")) == 1
