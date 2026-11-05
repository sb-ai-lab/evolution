"""Real declared-birth and paid-slot recovery of pre-Promotion completion."""

import pytest

from evolution.tree.campaign_projections import verify_evidence
from tests.campaign_drivers import _records
from tests.test_tree_campaign_family15_completion import completion_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize(
    "verification,cut",
    [
        ("reuse", "after_attempt_closed"),
        ("reuse", "after_candidate_gate_sealed"),
        ("reuse", "after_candidate_completion_bound"),
        ("reuse", "before_candidate_completion_birth"),
        ("reuse", "after_candidate_completion_birth"),
        ("reuse", "after_independent_final_bound"),
        ("fresh", "after_independent_final_bound"),
        ("fresh", "after_candidate_final_birth"),
        ("fresh", "before_candidate_completion_dispatch"),
    ],
)
async def test_real_cut_replays_only_owed_candidate_work(tmp_path, monkeypatch, verification, cut):
    await completion_run(tmp_path, monkeypatch, verification=verification, stop=cut, hard=True)
    run = await completion_run(tmp_path, monkeypatch, verification=verification)
    events = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "candidate_gate_sealed" for r in events) == 1
    assert sum(r["event"] == "final_measurement_bound" for r in events) == 1
    bindings = [r["payload"] for r in events if r["event"] == "final_measurement_bound"]
    mid = bindings[0]["candidate"]["measurement_id"]
    rows = _records(run / "measurements" / mid / "events.jsonl")
    assert sum(r["event"] == "sample_imported" for r in rows) == (
        2 if verification == "reuse" else 0
    )
    assert sum(r["event"] == "rollout_started" for r in rows) == (
        0 if verification == "reuse" else 2
    )
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize(
    "verification,cut",
    [
        ("reuse", "after_candidate_gate_sealed"),
        ("reuse", "after_candidate_completion_bound"),
        ("reuse", "after_candidate_completion_birth"),
        ("reuse", "before_candidate_completion_dispatch"),
        ("fresh", "before_candidate_completion_dispatch"),
    ],
)
async def test_authorized_continue_preserves_gate_and_rebinds_unfinished_completion(
    tmp_path, monkeypatch, verification, cut
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _only_c1, _Resettable

    adapter = _Resettable(used=_only_c1)
    options = dict(adapter=adapter, used=_only_c1, verification=verification)
    run = await completion_run(tmp_path, monkeypatch, stop=cut, hard=True, **options)
    events = _records(run / "campaign.jsonl")
    seal = next(r["payload"] for r in events if r["event"] == "candidate_gate_sealed")
    source_path = run / "measurements" / seal["source"]["measurement_id"] / "events.jsonl"
    frozen_bytes = source_path.read_bytes()
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, **options)
    run = await completion_run(
        tmp_path, monkeypatch, authorizations=[_continue(refused.value)], **options
    )
    assert source_path.read_bytes() == frozen_bytes
    events = _records(run / "campaign.jsonl")
    completions = [
        r["payload"]
        for r in events
        if r["event"] in {"candidate_completion_completed", "final_measurement_completed"}
    ]
    assert len(completions) == 1
    mid = completions[0]["source"]["measurement_id"]
    rows = _records(run / "measurements" / mid / "events.jsonl")
    if verification == "reuse":
        imported = [r["payload"] for r in rows if r["event"] == "sample_imported"]
        assert imported[0]["source"]["measurement_id"] == seal["source"]["measurement_id"]
        assert imported[0]["source"]["condition_fields"] == ["target"]
    if cut != "after_candidate_gate_sealed":
        assert mid.endswith("@e1")
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("tamper", ["missing", "torn", "changed", "symlink"])
async def test_sealed_gate_corruption_never_repairs_or_remeasures(tmp_path, monkeypatch, tamper):
    from evolution.tree.evolve import EvolveRefused

    run = await completion_run(tmp_path, monkeypatch, stop="after_candidate_gate_sealed", hard=True)
    events = _records(run / "campaign.jsonl")
    seal = next(r["payload"] for r in events if r["event"] == "candidate_gate_sealed")
    path = run / "measurements" / seal["source"]["measurement_id"] / "events.jsonl"
    original = path.read_bytes()
    if tamper == "missing":
        path.unlink()
    elif tamper == "torn":
        path.write_bytes(original + b'{"torn":')
    elif tamper == "changed":
        path.write_bytes(original.replace(b'"score":1.0', b'"score":0.0'))
    else:
        copy = tmp_path / "source-copy"
        copy.write_bytes(original)
        path.unlink()
        path.symlink_to(copy)
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused):
        await completion_run(tmp_path, monkeypatch)
    assert (run / "campaign.jsonl").read_bytes() == before
    assert not (run / "measurements" / "m-r01-a01-completion").exists()


@pytest.mark.parametrize("side", ["before", "after"])
async def test_atomic_import_rename_has_no_partial_admission(tmp_path, monkeypatch, side):
    import os

    from tests.test_tree_campaign_family15_gate import GatePowerCut

    original = os.replace
    fired = False

    def replace(source, destination, **kwargs):
        nonlocal fired
        directory = (
            os.readlink(f"/proc/self/fd/{kwargs['src_dir_fd']}")
            if kwargs.get("src_dir_fd") is not None
            else ""
        )
        target = directory.endswith("m-r01-a01-completion") and str(destination) == "events.jsonl"
        if target and not fired:
            fired = True
            if side == "before":
                raise GatePowerCut()
            original(source, destination, **kwargs)
            raise GatePowerCut()
        return original(source, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        await completion_run(tmp_path, patch, hard=True)
    assert fired
    run = await completion_run(tmp_path, monkeypatch)
    rows = _records(run / "measurements" / "m-r01-a01-completion" / "events.jsonl")
    assert [r["event"] for r in rows] == ["run_created", "sample_imported", "sample_imported"]


async def test_missing_stage_cut_does_not_repeat_paid_slots(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_drivers import _only_c1
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    original = _Campaign.run_stage

    async def cut(self, mid, stage):
        result = await original(self, mid, stage)
        if mid.endswith("-completion"):
            raise GatePowerCut()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "run_stage", cut)
        await completion_run(tmp_path, patch, used=_only_c1, hard=True)
    original_thresholds = _Campaign.thresholds
    seen = []

    def thresholds(self, number, barrier, measurement_id=None):
        if measurement_id and measurement_id.endswith("-completion"):
            seen.append(barrier)
        return original_thresholds(self, number, barrier, measurement_id)

    monkeypatch.setattr(_Campaign, "thresholds", thresholds)
    run = await completion_run(tmp_path, monkeypatch, used=_only_c1)
    rows = _records(run / "measurements" / "m-r01-a01-completion" / "events.jsonl")
    assert [r["payload"]["case_id"] for r in rows if r["event"] == "rollout_started"] == ["c2"]
    assert seen == ["final_stage"]


async def test_completed_candidate_evidence_stands_after_continue(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable
    from tests.test_tree_campaign_family15_gate import GateBoundary

    adapter = _Resettable()
    run = await completion_run(tmp_path, monkeypatch, adapter=adapter, hard=True)
    path = run / "measurements" / "m-r01-a01-completion" / "events.jsonl"
    before = path.read_bytes()
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, adapter=adapter)

    async def final_boundary(self, number):
        assert self.snapshot().rounds[number].attempts["r01-a01"].family15.completion is not None
        raise GateBoundary()

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "decide_round", final_boundary)
        await completion_run(
            tmp_path, patch, adapter=adapter, authorizations=[_continue(refused.value)]
        )
    assert path.read_bytes() == before
    events = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "candidate_completion_completed" for r in events) == 1
    assert not any(r["event"] == "measurement_rebound" for r in events)


async def test_missing_slot_scorer_fault_suspends_before_completion(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER, _only_c1

    script = TWO_CASE_SCORER.replace(
        "passed = ",
        'if payload["case_id"] == "c2" and ("edited" in view or "edited" in skill):\n    sys.exit(1)\npassed = ',
    )
    run = await completion_run(tmp_path, monkeypatch, used=_only_c1, scorer=script, stop="result")
    events = _records(run / "campaign.jsonl")
    assert events[-1]["event"] == "campaign_suspended"
    assert not any(r["event"] == "candidate_completion_completed" for r in events)


async def test_completion_uses_final_barrier(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_drivers import _only_c1

    original = _Campaign.thresholds
    seen = []

    def thresholds(self, number, barrier, measurement_id=None):
        if measurement_id and measurement_id.endswith("-completion"):
            seen.append(barrier)
        return original(self, number, barrier, measurement_id)

    monkeypatch.setattr(_Campaign, "thresholds", thresholds)
    await completion_run(tmp_path, monkeypatch, used=_only_c1)
    assert seen == ["final_stage"]


@pytest.mark.parametrize(
    "cut",
    [
        "after_condition_applied",
        "before_owed_candidate_completion_birth",
        "after_owed_candidate_completion_birth",
        "after_identity_rebased",
    ],
)
async def test_owed_birth_and_condition_transition_cut_preserves_exact_carry(
    tmp_path, monkeypatch, cut
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _only_c1, _Resettable

    adapter = _Resettable(used=_only_c1)
    await completion_run(
        tmp_path, monkeypatch, adapter=adapter, stop="after_candidate_completion_bound", hard=True
    )
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, adapter=adapter)
    authorization = _continue(refused.value)
    await completion_run(
        tmp_path, monkeypatch, adapter=adapter, authorizations=[authorization], stop=cut, hard=True
    )
    run = await completion_run(
        tmp_path, monkeypatch, adapter=adapter, authorizations=[authorization]
    )
    old = _records(run / "measurements" / "m-r01-a01-completion" / "events.jsonl")
    new = _records(run / "measurements" / "m-r01-a01-completion@e1" / "events.jsonl")
    old_rows = [r["payload"] for r in old if r["event"] == "sample_imported"]
    new_rows = [r["payload"] for r in new if r["event"] == "sample_imported"]
    assert len(old_rows) == len(new_rows) == 1
    assert old_rows[0]["source"]["condition_fields"] == []
    assert new_rows[0]["source"] == {**old_rows[0]["source"], "condition_fields": ["target"]}
    assert sum(r["event"] == "rollout_started" for r in old) == 0
    assert [r["payload"]["case_id"] for r in new if r["event"] == "rollout_started"] == ["c2"]
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("cut", ["after_candidate_gate_sealed", "after_candidate_completion_bound"])
@pytest.mark.parametrize("change", ["dependency", "seed_mode"])
async def test_continue_drops_incompatible_rows_without_condemning_sealed_source(
    tmp_path, monkeypatch, cut, change
):
    from dataclasses import replace

    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _only_c1, _Resettable

    class ChangingTarget(_Resettable):
        changed = False

        def _semantic_for(self, dependency_digest):
            semantic = super()._semantic_for(dependency_digest)
            if self.changed and change == "dependency":
                return replace(semantic, dependency_digest="new-deps")
            return semantic

    adapter = ChangingTarget(used=_only_c1)
    options = dict(adapter=adapter, used=_only_c1)
    run = await completion_run(tmp_path, monkeypatch, stop=cut, hard=True, **options)
    events = _records(run / "campaign.jsonl")
    seal = next(r["payload"] for r in events if r["event"] == "candidate_gate_sealed")
    gate_path = run / "measurements" / seal["source"]["measurement_id"] / "events.jsonl"
    gate_bytes = gate_path.read_bytes()
    declared = cut == "after_candidate_completion_bound"
    original = run / "measurements" / "m-r01-a01-completion" / "events.jsonl"
    assert not original.exists()
    adapter.changed = True
    if change == "seed_mode":
        adapter.seed_mode = "unpaired"
        adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, **options)
    authorization = _continue(refused.value)
    # Stop after binding to inspect the genuinely executed epoch transition;
    # dependency attestation remains owned by the ordinary later probe/stage.
    run = await completion_run(
        tmp_path,
        monkeypatch,
        authorizations=[authorization],
        stop="after_independent_final_bound",
        hard=True,
        **options,
    )
    assert gate_path.read_bytes() == gate_bytes
    old_bytes = original.read_bytes()
    old = _records(original)
    assert sum(r["event"] == "sample_imported" for r in old) == (1 if declared else 0)
    assert not any(r["event"] == "rollout_started" for r in old)
    if declared:
        imported = next(r["payload"] for r in old if r["event"] == "sample_imported")
        assert imported["source"]["measurement_id"] == seal["source"]["measurement_id"]
        assert imported["source"]["condition_fields"] == []
    events = _records(run / "campaign.jsonl")
    final = next(r["payload"] for r in events if r["event"] == "final_measurement_bound")
    mid = final["candidate"]["measurement_id"]
    assert mid.endswith("@e1") == declared
    current = run / "measurements" / mid / "events.jsonl"
    assert not any(r["event"] == "sample_imported" for r in _records(current))
    audit = verify_evidence(run)
    assert audit["mismatches"] == [], audit
    if change == "seed_mode":
        run = await completion_run(tmp_path, monkeypatch, authorizations=[authorization], **options)
        rows = _records(current)
        assert [r["payload"]["case_id"] for r in rows if r["event"] == "rollout_started"] == [
            "c1",
            "c2",
        ]
        assert not any(r["event"] == "sample_imported" for r in rows)
        assert gate_path.read_bytes() == gate_bytes
        if declared:
            assert original.read_bytes() == old_bytes
        audit = verify_evidence(run)
        assert audit["mismatches"] == [], audit
    else:
        await completion_run(
            tmp_path, monkeypatch, authorizations=[authorization], stop="result", **options
        )
        events = _records(run / "campaign.jsonl")
        assert events[-1]["event"] == "campaign_suspended"
        assert events[-1]["payload"]["owner"] == "connection"
        assert not any(r["event"] == "candidate_completion_completed" for r in events)
        assert gate_path.read_bytes() == gate_bytes
        audit = verify_evidence(run)
        assert audit["mismatches"] == [], audit
        if declared:
            assert original.read_bytes() == old_bytes


async def test_unborn_planner_does_not_restore_rows_omitted_by_original_completion(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _only_c1, _Resettable

    adapter = _Resettable(used=_only_c1)
    options = dict(adapter=adapter, used=_only_c1)
    run = await completion_run(
        tmp_path, monkeypatch, stop="after_candidate_gate_sealed", hard=True, **options
    )
    adapter.seed_mode = "unpaired"
    adapter.capabilities = replace(adapter.capabilities, seed_mode="unpaired")
    with pytest.raises(EvolveRefused) as first:
        await completion_run(tmp_path, monkeypatch, **options)
    await completion_run(
        tmp_path,
        monkeypatch,
        authorizations=[_continue(first.value)],
        stop="after_candidate_completion_bound",
        hard=True,
        **options,
    )
    original = run / "measurements" / "m-r01-a01-completion" / "events.jsonl"
    assert not original.exists()
    # The gate's exact seeds match again, but its rows never entered this
    # declared original completion and cannot appear in that completion's carry.
    adapter.seed_mode = "exact"
    adapter.capabilities = replace(adapter.capabilities, seed_mode="exact")
    with pytest.raises(EvolveRefused) as second:
        await completion_run(tmp_path, monkeypatch, **options)
    run = await completion_run(
        tmp_path, monkeypatch, authorizations=[_continue(second.value)], **options
    )
    old = _records(original)
    current = _records(run / "measurements" / "m-r01-a01-completion@e2" / "events.jsonl")
    assert [r["event"] for r in old] == ["run_created"]
    assert not any(r["event"] == "sample_imported" for r in current)
    assert [r["payload"]["case_id"] for r in current if r["event"] == "rollout_started"] == [
        "c1",
        "c2",
    ]
    audit = verify_evidence(run)
    assert audit["mismatches"] == [], audit
