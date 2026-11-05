"""Real family-14 final-stage recovery, without H12 report assembly."""

from __future__ import annotations

import pytest

from evolution.tree.execution.authorizations import Authorization
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    SERIAL,
    STATISTICAL,
    TWO_ATTEMPTS,
    _arm_on_finalists_bound,
    _cancel_when,
    _CohortFaults,
    _cut_after_cohort_stage,
    _cut_hook,
    _facts,
    _measurement_events,
    _records,
    _ResettableTagged,
    _starts,
)
from tests.campaign_fakes import Crash, crash_after_the_joint_stage_once
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow
CHAMPION = "m-r01-final-01-champion"
FINALIST = "m-r01-final-01-a01"


@pytest.mark.parametrize(
    "kind,outcome,owner",
    [
        ("candidate", "target_failed", "target"),
        ("champion", "target_failed", "target"),
        ("champion", "time_budget", "rollout"),
    ],
)
async def test_final_gap_lists_and_repairs_exactly_one_try(
    tmp_path, monkeypatch, kind, outcome, owner
):
    adapter = _CohortFaults(faults={(kind, "c2", 1): outcome}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {"indeterminate": LENIENT, "sample_retries": 0}}
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspensions[-1]["reason"] == "final_incomplete"
    (suspended,) = payloads(run, "campaign_suspended")
    mid = FINALIST if kind == "candidate" else CHAMPION
    assert suspended["owner"] == owner
    assert suspended["evidence"]["evidence_kind"] == "final_rows"
    assert suspended["evidence"]["rows"] == [
        {
            "measurement_id": mid,
            "case_id": "c2",
            "sample_id": "01",
            "retry_ordinal": 0,
            "owner": owner,
        }
    ]
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _starts(run, mid, "c2") == [(0, False), (1, True)]
    assert len(payloads(run, "final_measurement_bound")) == 2
    assert len(payloads(run, "final_measurement_completed")) == 2
    assert len(payloads(run, "campaign_suspended")) == 1
    assert sum(p["count"] for p in payloads(run, "repairs_dispatched")) == 1
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("declined", [False, True])
async def test_cancelled_or_declined_champion_grant_survives_dispatch_crash(
    tmp_path, monkeypatch, declined
):
    run = tmp_path / "run"
    adapter = _CohortFaults(
        faults={("champion", "c2", 1): "target_failed"} if declined else {},
        used=EDITED["used"],
    )
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {**SERIAL, "indeterminate": LENIENT}}
    needle = b'"outcome":"target_failed"' if declined else b'"rollout_finished"'
    stop = _cancel_when(run, CHAMPION, needle)
    with monkeypatch.context() as patch:
        crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, cancelled=stop, **options)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspensions[-1]["reason"] == "final_incomplete"
    assert payloads(run, "campaign_suspended")[0]["evidence"]["rows"][0]["retry_ordinal"] == 0
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_repairs_dispatched")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _starts(run, CHAMPION, "c2") == [(0, False), (1, not declined)]
    assert (
        len([p for p in payloads(run, "repairs_dispatched") if p["measurement_id"] == CHAMPION])
        == 1
    )
    assert len(payloads(run, "final_measurement_completed")) == 2
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "hook,call",
    [
        ("after_finalists_bound", 1),
        ("after_cohort_measurement_bound", 1),
        ("after_cohort_measurement_bound", 2),
        ("after_cohort_measurement_bound", 3),
        ("after_cohort_measurement_completed", 1),
        ("after_cohort_measurement_completed", 2),
        ("after_cohort_measurement_completed", 3),
        ("before_global_comparison", 1),
        ("after_global_comparison_completed", 1),
        ("after_global_comparison_completed", 2),
        ("before_selection_decided", 1),
        ("after_selection_decided", 1),
    ],
)
async def test_crash_at_final_fact_resumes_without_duplicate_or_repeated_member(
    tmp_path, monkeypatch, hook, call
):
    with monkeypatch.context() as patch:
        _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, campaign=TWO_ATTEMPTS)
    run = tmp_path / "run"
    frozen = {
        p["measurement_id"]: _measurement_events(run, p["measurement_id"]).read_bytes()
        for p in payloads(run, "final_measurement_completed")
    }
    run, snapshot = await family14_run(tmp_path, monkeypatch, campaign=TWO_ATTEMPTS)
    assert snapshot.finished["outcome"] == "completed"
    for mid, before in frozen.items():
        assert _measurement_events(run, mid).read_bytes() == before
    for event, count in [
        ("finalists_bound", 1),
        ("final_measurement_bound", 3),
        ("final_measurement_completed", 3),
        ("global_comparison_completed", 2),
        ("selection_decided", 1),
    ]:
        assert len(payloads(run, event)) == count
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_pending" for r in ledger) == 1
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    assert audit(run)["mismatches"] == []
    control = tmp_path / "uncut"
    control.mkdir()
    plain, _ = await family14_run(control, monkeypatch, campaign=TWO_ATTEMPTS)
    assert _facts(run) == _facts(plain)


@pytest.mark.parametrize("cut", ["after_stage", "after_completion"])
@pytest.mark.parametrize("effect", ["continue", "rebaseline", "close"])
async def test_changed_condition_preserves_final_history_and_rebinds_only_unfinished(
    tmp_path, monkeypatch, cut, effect
):
    from evolution.tree.evolve import EvolveRefused

    adapter = _ResettableTagged(used=EDITED["used"])
    options = {"adapter": adapter, "campaign": {"rounds": 2}}
    with monkeypatch.context() as patch:
        if cut == "after_stage":
            _cut_after_cohort_stage(patch, 1)
        else:
            _cut_hook(patch, "after_cohort_measurement_completed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    run = tmp_path / "run"
    binding = payloads(run, "finalists_bound")[0]
    before = {mid: _measurement_events(run, mid).read_bytes() for mid in binding["members"]}
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    assert effect in refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-10T00:00:00Z",
        effect=effect,
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, STATISTICAL, authorizations=[decision], **options
    )
    assert snapshot.finished is not None
    assert payloads(run, "finalists_bound")[0] == binding
    for mid, content in before.items():
        assert _measurement_events(run, mid).read_bytes() == content
    rebound = payloads(run, "measurement_rebound")
    if effect == "continue":
        expected = [CHAMPION, FINALIST] if cut == "after_stage" else [FINALIST]
        assert [p["from_measurement_id"] for p in rebound] == expected
        assert [p["measurement_id"] for p in rebound] == [mid + "@e1" for mid in expected]
        assert all(p["imported"] == (6 if cut == "after_stage" else 8) for p in rebound)
        assert list(snapshot.rounds[1].finals[0]["members"]) == binding["members"]
        assert snapshot.rounds[1].selection is not None
    elif effect == "rebaseline":
        assert rebound == []
        assert snapshot.rounds[1].closed["outcome"] == "condition_changed"
        assert snapshot.rounds[1].selection is None
    else:
        assert rebound == []
        assert snapshot.finished["reason"] == "condition_changed"
    assert audit(run)["mismatches"] == []


async def test_final_threshold_suspension_repairs_and_resumes_same_barrier(tmp_path, monkeypatch):
    adapter = _CohortFaults(faults={("candidate", "c2", 1): "target_failed"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {
        "adapter": adapter,
        "campaign": {
            "sample_retries": 0,
            "indeterminate": {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}},
        },
    }
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspensions[-1]["evidence_kind"] == "threshold"
    assert payloads(run, "campaign_suspended")[0]["reason"] == "target_unstable"
    adapter.faults.clear()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _starts(run, FINALIST, "c2") == [(0, False), (1, True)]
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("position", ["settled_stage", "pending", "committed"])
async def test_crash_at_barrier_or_promotion_preserves_one_transaction(
    tmp_path, monkeypatch, position
):
    from tests.campaign_drivers import _cut_before_round_closed, _cut_store_hook

    with monkeypatch.context() as patch:
        if position == "settled_stage":
            crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final")
        elif position == "pending":
            _cut_store_hook(patch, "after_pending")
        else:
            _cut_before_round_closed(patch)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch)
    run, snapshot = await family14_run(tmp_path, monkeypatch)
    assert snapshot.finished["outcome"] == "completed"
    ledger = _records(run / "artifacts.jsonl")
    assert sum(r["event"] == "promotion_pending" for r in ledger) == 1
    assert sum(r["event"] == "promotion_committed" for r in ledger) == 1
    assert len(payloads(run, "selection_decided")) == 1
    assert len(payloads(run, "final_measurement_completed")) == 2
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("kind", ["candidate", "champion"])
async def test_final_member_component_drift_suspends_before_rows_and_resumes(
    tmp_path, monkeypatch, kind
):
    from tests.campaign_drivers import _drift_on_finalists_bound, _Drifts, _map_of_the_tree

    adapter = _Drifts(components=_map_of_the_tree, used=EDITED["used"])
    adapter.kind = kind
    with monkeypatch.context() as patch:
        _drift_on_finalists_bound(patch, adapter)
        run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.suspensions[-1]["reason"] == "target_components_changed"
    mid = FINALIST if kind == "candidate" else CHAMPION
    assert mid not in {p["measurement_id"] for p in payloads(run, "final_measurement_completed")}
    assert audit(run)["mismatches"] == []
    adapter.on = False
    run, snapshot = await family14_run(tmp_path, monkeypatch, adapter=adapter)
    assert snapshot.finished["outcome"] == "completed"
    assert len(payloads(run, "final_measurement_bound")) == 2
    assert len(payloads(run, "final_measurement_completed")) == 2
    assert audit(run)["mismatches"] == []


async def test_mixed_final_gaps_list_in_member_case_order_and_arbitrate_owner(
    tmp_path, monkeypatch
):
    import hashlib
    import json

    adapter = _CohortFaults(
        faults={
            ("champion", "c2", 1): "time_budget",
            ("candidate", "c1", 1): "target_failed",
            ("candidate", "c2", 1): "target_failed",
        },
        used=EDITED["used"],
    )
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {"sample_retries": 0, "indeterminate": LENIENT}}
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    (suspension,) = payloads(run, "campaign_suspended")
    rows = suspension["evidence"]["rows"]
    assert [(r["measurement_id"], r["case_id"], r["owner"], r["retry_ordinal"]) for r in rows] == [
        (CHAMPION, "c2", "rollout", 0),
        (FINALIST, "c1", "target", 0),
        (FINALIST, "c2", "target", 0),
    ]
    assert suspension["owner"] == "target"
    expected = [
        [CHAMPION, "c2", "01", 0, "rollout"],
        [FINALIST, "c1", "01", 0, "target"],
        [FINALIST, "c2", "01", 0, "target"],
    ]
    assert (
        suspension["evidence"]["rows_digest"]
        == hashlib.sha256(json.dumps(sorted(expected), separators=(",", ":")).encode()).hexdigest()
    )
    assert (suspension["evidence"]["count"], suspension["evidence"]["truncated"]) == (3, False)
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert sum(p["count"] for p in payloads(run, "repairs_dispatched")) == 3
    assert _starts(run, CHAMPION, "c1") == [(0, False)]
    for mid, case in [(CHAMPION, "c2"), (FINALIST, "c1"), (FINALIST, "c2")]:
        assert _starts(run, mid, case) == [(0, False), (1, True)]
    assert audit(run)["mismatches"] == []


async def test_continue_after_gap_listing_carries_scored_rows_without_repairing_frozen_member(
    tmp_path, monkeypatch
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _Resettable

    class ResettableFaults(_Resettable, _CohortFaults):
        pass

    adapter = ResettableFaults(
        faults={("candidate", "c2", 1): "target_failed"}, used=EDITED["used"]
    )
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {"indeterminate": LENIENT, "sample_retries": 0}}
    run, _ = await family14_run(tmp_path, monkeypatch, **options)
    old_rows = {mid: _measurement_events(run, mid).read_bytes() for mid in (CHAMPION, FINALIST)}
    adapter.settings = "settings-v2"
    adapter.faults.clear()
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, **options)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept target repair",
        now="2026-10-10T00:00:00Z",
        effect="continue",
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, authorizations=[decision], **options)
    assert snapshot.finished["outcome"] == "completed"
    rebound = payloads(run, "measurement_rebound")
    assert [(p["from_measurement_id"], p["measurement_id"], p["imported"]) for p in rebound] == [
        (CHAMPION, CHAMPION + "@e1", 2),
        (FINALIST, FINALIST + "@e1", 1),
    ]
    for mid, content in old_rows.items():
        assert _measurement_events(run, mid).read_bytes() == content
    assert not payloads(run, "repairs_dispatched")
    assert _starts(run, FINALIST + "@e1", "c1") == []
    assert _starts(run, FINALIST + "@e1", "c2") == [(0, False)]
    assert audit(run)["mismatches"] == []


async def test_forged_final_gap_listing_is_rederived_from_real_member_rows(tmp_path, monkeypatch):
    from evolution.tree.campaign_schema import cohort_rows_digest
    from tests.test_tree_campaign_family14_audit import forge

    adapter = _CohortFaults(faults={("candidate", "c2", 1): "target_failed"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        adapter=adapter,
        campaign={"indeterminate": LENIENT, "sample_retries": 0},
    )

    def edit(event, payload):
        if event == "campaign_suspended":
            rows = [{**payload["evidence"]["rows"][0], "case_id": "c1"}]
            payload["evidence"].update(rows=rows, rows_digest=cohort_rows_digest(rows))

    forge(run, edit)
    found = audit(run)["mismatches"]
    assert [(r["event"], r["detail"]) for r in found] == [
        ("campaign_suspended", "the suspension's final rows are not the incomplete pairs")
    ]


async def test_rollout_owned_incomplete_finalist_stays_completed_on_resume(tmp_path, monkeypatch):
    adapter = _CohortFaults(faults={("candidate", "c2", 1): "time_budget"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = {"adapter": adapter, "campaign": {"indeterminate": LENIENT, "sample_retries": 0}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_completed", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    before = _measurement_events(run, FINALIST).read_bytes()
    adapter.faults.clear()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _measurement_events(run, FINALIST).read_bytes() == before
    assert not payloads(run, "campaign_suspended")
    assert not payloads(run, "repairs_dispatched")
    (comparison,) = payloads(run, "global_comparison_completed")
    assert (
        comparison["accepted"],
        comparison["reason"],
        comparison["exact"],
        comparison["point"],
    ) == (False, "final_incomplete", None, None)
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize(
    "hook,call,settled_looks",
    [
        ("after_comparison_bound", 1, 0),
        ("after_noise_calibration_completed", 1, 1),
        ("after_comparison_look_evaluated", 1, 1),
        ("after_comparison_look_evaluated", 2, 2),
    ],
    ids=["comparison", "calibration", "interim", "deciding-final"],
)
async def test_h4_gate_fact_crash_executes_only_missing_work(
    tmp_path, monkeypatch, fresh_parent, hook, call, settled_looks
):
    from tests.test_tree_campaign_family14_gate_run import (
        CONSTANT,
        assert_clean_run,
        assert_fresh_final,
    )

    # Replaying any settled gate or sample would duplicate facts versus control.
    config = {**CONSTANT, "gate": {"fresh_parent": fresh_parent}}
    options = {"family14_settings": {"margin": 0.25}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    run = tmp_path / "run"
    before = (run / "campaign.jsonl").read_bytes()
    looks = payloads(run, "comparison_look_evaluated")
    # Fresh calibration completes between looks 3 and 4; baseline precedes 3.
    if hook == "after_noise_calibration_completed":
        settled_looks = 1 if fresh_parent else 0
    assert len(looks) == settled_looks
    assert not payloads(run, "attempt_closed")
    rows_before = {
        path.parent.name: path.read_bytes()
        for path in (run / "measurements").glob("*/events.jsonl")
    }
    run, snapshot = await family14_run(tmp_path, monkeypatch, config, **options)
    assert (run / "campaign.jsonl").read_bytes().startswith(before)
    for member, content in rows_before.items():
        assert _measurement_events(run, member).read_bytes().startswith(content)
    assert payloads(run, "comparison_look_evaluated")[:settled_looks] == looks
    for event, count in [
        ("comparison_bound", 1),
        ("noise_calibration_bound", 1),
        ("noise_calibration_completed", 1),
        ("comparison_look_evaluated", 2),
        ("attempt_closed", 1),
        ("final_measurement_bound", 2),
        ("final_measurement_completed", 2),
    ]:
        assert len(payloads(run, event)) == count
    assert_fresh_final(run, snapshot, 4)
    assert_clean_run(run, snapshot)
    control = tmp_path / "uncut"
    control.mkdir()
    plain, plain_snapshot = await family14_run(control, monkeypatch, config, **options)
    assert_clean_run(plain, plain_snapshot)
    resumed_facts, plain_facts = _facts(run), _facts(plain)
    # A live stage may need a new target instance after process loss. Its
    # attestation is fresh metadata; scheduled/scored work and gate facts stand.
    assert [fact for fact in resumed_facts[0] if fact[0] != "instance_attested"] == [
        fact for fact in plain_facts[0] if fact[0] != "instance_attested"
    ]
    assert resumed_facts[1:] == plain_facts[1:]


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize(
    "hook,call,decided",
    [
        ("after_comparison_bound", 1, False),
        ("after_comparison_look_evaluated", 1, False),
        ("after_comparison_look_evaluated", 2, True),
    ],
    ids=["before-first-look", "after-interim", "after-deciding-look"],
)
async def test_h4_continue_keeps_margin_and_historical_looks(
    tmp_path, monkeypatch, fresh_parent, hook, call, decided
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _events
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run, assert_fresh_final

    # Reinterpreting a settled look or reusing changed provenance breaks recovery.
    adapter = _ResettableTagged(used=EDITED["used"])
    config = {**STATISTICAL, "gate": {**STATISTICAL["gate"], "fresh_parent": fresh_parent}}
    options = {"adapter": adapter, "family14_settings": {"margin": 0.2}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    run = tmp_path / "run"
    historical = _events(run, "comparison_look_evaluated")
    original_comparison = _events(run, "comparison_bound")[0]
    old_rows = {
        path.parent.name: path.read_bytes()
        for path in (run / "measurements").glob("*/events.jsonl")
    }
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, config, **options)
    assert "continue" in refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target with the frozen focus margin",
        now="2026-10-11T00:00:00Z",
        effect="continue",
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[decision], **options
    )
    for member, content in old_rows.items():
        assert _measurement_events(run, member).read_bytes() == content
    looks = _events(run, "comparison_look_evaluated")
    assert looks[: len(historical)] == historical
    comparisons = _events(run, "comparison_bound")
    assert all(record["payload"]["margin"] == 0.2 for record in comparisons)
    rebound = payloads(run, "measurement_rebound")
    expected = ["m-r01-a01", "m-r01-a01-parent"] if fresh_parent else ["m-r01-a01"]
    assert [record["from_measurement_id"] for record in rebound] == expected
    assert [record["measurement_id"] for record in rebound] == [mid + "@e1" for mid in expected]
    if decided:
        assert comparisons == [original_comparison]
        assert looks == historical
        assert len(looks) == 2
        (closed,) = payloads(run, "attempt_closed")
        assert (closed["outcome"], closed["reason"], closed["look"]) == (
            "finalist",
            "gate_passed",
            4,
        )
    else:
        assert len(comparisons) == 2
        assert comparisons[1]["payload"]["supersedes"] == original_comparison["sequence"]
        assert looks[-1]["payload"]["comparison"] == comparisons[1]["sequence"]
    assert len(payloads(run, "authorization_applied")) == 1
    assert len(payloads(run, "attempt_closed")) == 1
    assert_fresh_final(run, snapshot, 4)
    assert_clean_run(run, snapshot)


async def test_h4_continue_with_changed_seed_provenance_falls_back_to_unpaired(
    tmp_path, monkeypatch
):
    import dataclasses

    from evolution.tree import confidence_gate
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_campaign_family14_gate_audit import scorer_tables
    from tests.test_tree_campaign_family14_gate_run import assert_clean_run, assert_fresh_final

    class ChangedSeedTarget(_ResettableTagged):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.unseeded_counts = {}

        def instance_for(self, candidate, instance_id, boot_id):
            if self.seed_mode != "none":
                return super().instance_for(candidate, instance_id, boot_id)
            instance = FakeAdapter.instance_for(self, candidate, instance_id, boot_id)
            send, tags = instance.send, {}

            async def tagged(session, message, request_id):
                # The test scorer sees varying replies without seed claims.
                # Count sessions, as the existing unseeded H3 adapter does.
                if session not in tags:
                    key = (candidate.digest, session.case_id)
                    self.unseeded_counts[key] = self.unseeded_counts.get(key, 0) + 1
                    tags[session] = self.unseeded_counts[key]
                result = await send(session, message, request_id)
                first = {
                    **result.events[0],
                    "text": f"{result.events[0]['text']} #r{tags[session]}",
                }
                return dataclasses.replace(result, events=(first, *result.events[1:]))

            instance.send = tagged
            return instance

    scorer = scorer_tables(
        [0, 0.125, 0.25, 0.375],
        [0.5, 0.625, 0.75, 0.875],
        [0.5, 0.625, 0.75, 0.875],
        [0.5, 0.625, 0.75, 0.875],
    )
    adapter = ChangedSeedTarget(used=EDITED["used"])
    config = {
        **STATISTICAL,
        "scorer": scorer,
        "gate": {"causal": {"confidence": 0.9, "tolerance": 0.5}},
    }
    options = {"adapter": adapter, "family14_settings": {"margin": 0.2}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_comparison_look_evaluated")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    run = tmp_path / "run"
    historical = payloads(run, "comparison_look_evaluated")[0]
    original_rows = {
        member: _measurement_events(run, member).read_bytes()
        for member in ("m-r01-a01", "m-r01-a01-parent")
    }
    assert historical["focus"]["exact"] is True
    adapter.settings = "settings-v2"
    adapter.seed_mode = "none"
    adapter.capabilities = dataclasses.replace(adapter.capabilities, seed_mode="none")
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, config, **options)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept unseeded target",
        now="2026-10-11T00:00:00Z",
        effect="continue",
    )
    real_pairing, pairings = confidence_gate.pairing_of, []

    def record_real_pairing(evidence):
        result = real_pairing(evidence)
        pairings.append(result)
        return result

    # Trace the unchanged real proof, never replace its inputs or result. A
    # clean independent row audit below also verifies the resulting intervals.
    with monkeypatch.context() as patch:
        patch.setattr(confidence_gate, "pairing_of", record_real_pairing)
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, config, authorizations=[decision], **options
        )
    assert pairings == ["unpaired"]
    first, final = payloads(run, "comparison_look_evaluated")
    assert first == historical
    for member, content in original_rows.items():
        assert _measurement_events(run, member).read_bytes() == content
    assert (final["focus"]["exact"], final["causal"]["exact"]) == (False, False)
    assert final["decision"] == "finalist"
    assert len(payloads(run, "comparison_bound")) == 2
    assert all(p["margin"] == 0.2 for p in payloads(run, "comparison_bound"))
    assert [p["from_measurement_id"] for p in payloads(run, "measurement_rebound")] == [
        "m-r01-a01",
        "m-r01-a01-parent",
    ]
    for member in ("m-r01-a01@e1", "m-r01-a01-parent@e1"):
        rows = _records(_measurement_events(run, member))
        imported = [r["payload"] for r in rows if r["event"] == "sample_imported"]
        # Seed-mode changes the sample-id namespace. None of the old exact
        # samples matches the new unseeded schedule, so there is no carry.
        assert imported == []
        scored = [r["payload"] for r in rows if r["event"] == "scoring_finished"]
        assert len(scored) == 8
        assert {r["metrics"]["score"] for r in scored if r["case_id"] == "c1"} == (
            {0.5, 0.625, 0.75, 0.875} if "parent" not in member else {0, 0.125, 0.25, 0.375}
        )
    assert_fresh_final(run, snapshot, 4)
    assert_clean_run(run, snapshot)


@pytest.mark.parametrize("fresh_parent", [False, True])
async def test_h4_continue_keeps_completed_zero_calibration_without_redraw(
    tmp_path, monkeypatch, fresh_parent
):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family14_gate_run import (
        CONSTANT,
        assert_clean_run,
        assert_fresh_final,
    )

    # A superseding comparison must keep a settled Parent allowance, including 0.
    adapter = _ResettableTagged(used=EDITED["used"])
    config = {**CONSTANT, "gate": {"fresh_parent": fresh_parent}}
    options = {"adapter": adapter, "family14_settings": {"margin": 0.25}}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_noise_calibration_completed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    run = tmp_path / "run"
    (calibration,) = payloads(run, "noise_calibration_completed")
    assert (calibration["degenerate"], calibration["allowance"]) == (True, 0)
    historical = payloads(run, "comparison_look_evaluated")
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await family14_run(tmp_path, monkeypatch, config, **options)
    decision = Authorization.new(
        "run_identity_changed",
        str(refused.value.instance_digest),
        "accept changed target",
        now="2026-10-11T00:00:00Z",
        effect="continue",
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[decision], **options
    )
    assert len(payloads(run, "comparison_bound")) == 2
    assert all(p["margin"] == 0.25 for p in payloads(run, "comparison_bound"))
    assert len(payloads(run, "noise_calibration_bound")) == 1
    assert payloads(run, "noise_calibration_completed") == [calibration]
    looks = payloads(run, "comparison_look_evaluated")
    assert looks[: len(historical)] == historical
    assert (looks[-1]["decision"], looks[-1]["tolerance"]["value"]) == ("finalist", 0)
    assert "noise_not_observed" in looks[-1]["caveats"]
    assert "noise_allowance_empirical" not in looks[-1]["caveats"]
    assert_fresh_final(run, snapshot, 4)
    assert_clean_run(run, snapshot)
