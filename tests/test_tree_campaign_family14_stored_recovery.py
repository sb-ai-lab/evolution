"""Stable final evidence survives genuine crash, repair and condition boundaries.

Control comparisons use H3's naming facts, ledger event sequence and row identity
multisets. They do not assert numerical equality across independently seeded runs.
Historical bytes and settled payloads are compared exactly within the resumed run;
both control and resumed numerical evidence receive independent row audits.
"""

from __future__ import annotations

import dataclasses

import pytest

from evolution.tree.evolve import EvolveRefused
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    SERIAL,
    STATISTICAL,
    TWO_ATTEMPTS,
    _arm_on_finalists_bound,
    _CohortFaults,
    _continue,
    _cut_after_cohort_stage,
    _cut_before_round_closed,
    _cut_hook,
    _cut_store_hook,
    _facts,
    _measurement_events,
    _records,
    _Resettable,
    _ResettableTagged,
    _starts,
)
from tests.campaign_fakes import Crash, crash_after_the_joint_stage_once
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_gate_audit import CONSTANT
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow
STABLE = {"stability": "stable"}
BASELINE = "m-r00-champion"
FINALIST = "m-r01-final-01-a01"
SECOND = "m-r01-final-01-a02"


def _bytes(run):
    return {
        path.parent.name: path.read_bytes()
        for path in (run / "measurements").glob("*/events.jsonl")
    }


def _stored_reference(run):
    (baseline,) = payloads(run, "baseline_complete")
    return {
        "measurement_id": baseline["measurement_id"],
        "prefix_digest": baseline["evidence"][0]["content_digest"],
    }, baseline["evidence"][0]


def _assert_stored(run, stored, reference):
    bindings = payloads(run, "final_measurement_bound")
    assert bindings
    assert all(row["stored_champion"] == stored for row in bindings)
    assert all(row["evidence"] == [reference] for row in bindings)
    assert all(row["role"] == "finalist" for row in bindings)
    assert not list((run / "measurements").glob("*-final-*-champion*"))
    for row in payloads(run, "global_comparison_completed"):
        assert row["champion_measurement_id"] == stored["measurement_id"]
        assert row["evidence"][1] == reference


@pytest.mark.parametrize(
    "hook,call",
    [
        ("after_finalists_bound", 1),
        ("after_cohort_measurement_bound", 1),
        ("after_cohort_measurement_bound", 2),
        ("after_cohort_measurement_completed", 1),
        ("after_cohort_measurement_completed", 2),
        ("before_global_comparison", 1),
        ("after_global_comparison_completed", 1),
        ("after_global_comparison_completed", 2),
        ("before_selection_decided", 1),
        ("after_selection_decided", 1),
        ("settled_stage", 1),
        ("promotion_pending", 1),
        ("promotion_committed", 1),
    ],
)
async def test_stable_crash_resumes_missing_facts_and_matches_uncut_structure(
    tmp_path, monkeypatch, hook, call
):
    options = dict(family14_settings=STABLE, campaign=TWO_ATTEMPTS)
    with monkeypatch.context() as patch:
        if hook == "settled_stage":
            crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final")
        elif hook == "promotion_pending":
            _cut_store_hook(patch, "after_pending")
        elif hook == "promotion_committed":
            _cut_before_round_closed(patch)
        else:
            _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    stored, reference = _stored_reference(run)
    history = (run / "campaign.jsonl").read_bytes()
    rows = _bytes(run)
    complete = {BASELINE} | {
        row["measurement_id"] for row in payloads(run, "final_measurement_completed")
    }
    settled = payloads(run, "global_comparison_completed")
    if hook == "after_cohort_measurement_bound":
        assert not _measurement_events(run, [FINALIST, SECOND][call - 1]).exists()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    for mid, before in rows.items():
        after = _measurement_events(run, mid).read_bytes()
        assert after == before if mid in complete else after.startswith(before)
    assert payloads(run, "global_comparison_completed")[: len(settled)] == settled
    _assert_stored(run, stored, reference)
    for event, count in [
        ("finalists_bound", 1),
        ("final_measurement_bound", 2),
        ("final_measurement_completed", 2),
        ("global_comparison_completed", 2),
        ("selection_decided", 1),
    ]:
        assert len(payloads(run, event)) == count
    ledger = _records(run / "artifacts.jsonl")
    assert sum(row["event"] == "promotion_pending" for row in ledger) == 1
    assert sum(row["event"] == "promotion_committed" for row in ledger) == 1
    assert audit(run)["mismatches"] == []
    control = tmp_path / "uncut"
    control.mkdir()
    plain, _ = await family14_run(control, monkeypatch, **options)
    assert audit(plain)["mismatches"] == []
    assert _facts(run) == _facts(plain)


@pytest.mark.parametrize("retries", [0, 1])
async def test_power_loss_inside_final_rollout_preserves_attestation_and_retry_policy(
    tmp_path, monkeypatch, retries
):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_drivers import _CutOnSend, _PowerCut

    adapter = _CutOnSend(faults={}, used=EDITED["used"])
    options = dict(
        adapter=adapter,
        family14_settings=STABLE,
        campaign={**SERIAL, "indeterminate": LENIENT, "sample_retries": retries},
    )
    real_hook = _Campaign.hook

    def arm(self, name):
        if name == "after_finalists_bound":
            adapter.cut = ("candidate", "c2")
        return real_hook(self, name)

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", arm)
        with pytest.raises(_PowerCut):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    stored, reference = _stored_reference(run)
    before = _bytes(run)
    completed = [
        r for r in _records(_measurement_events(run, FINALIST)) if r["event"] == "scoring_finished"
    ]
    assert len(completed) == 1 and completed[0]["payload"]["case_id"] == "c1"
    attestations = [
        row for row in payloads(run, "instance_attested") if row["measurement_id"] == FINALIST
    ]
    assert len(attestations) == 1
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert not snapshot.suspensions
    assert snapshot.finished["outcome"] == "completed"
    assert _measurement_events(run, BASELINE).read_bytes() == before[BASELINE]
    assert _measurement_events(run, FINALIST).read_bytes().startswith(before[FINALIST])
    assert _starts(run, FINALIST, "c1") == [(0, False)]
    assert _starts(run, FINALIST, "c2") == ([(0, False), (1, False)] if retries else [(0, False)])
    scored = [
        r for r in _records(_measurement_events(run, FINALIST)) if r["event"] == "scoring_finished"
    ]
    assert scored[0] == completed[0] and len(scored) == 1 + retries
    assert not payloads(run, "repairs_dispatched")
    (comparison,) = payloads(run, "global_comparison_completed")
    assert comparison["reason"] == ("global_off" if retries else "final_incomplete")
    (completion,) = payloads(run, "final_measurement_completed")
    assert completion["complete"] == bool(retries)
    assert completion["evidence"]
    assert [row for row in payloads(run, "instance_attested") if row["measurement_id"] == FINALIST][
        :1
    ] == attestations
    assert payloads(run, "round_closed")[0]["outcome"] == ("promoted" if retries else "retained")
    interrupted = [
        row for row in payloads(run, "indeterminate_observed") if row["measurement_id"] == FINALIST
    ]
    assert [(row["owner"], row["reason"], row["retry_follows"]) for row in interrupted] == [
        ("rollout", "rollout_interrupted", bool(retries))
    ]
    _assert_stored(run, stored, reference)
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("failure", ["gap", "threshold", "fault", "mixed_owners"])
async def test_stable_final_barrier_repairs_gaps_faults_and_thresholds(
    tmp_path, monkeypatch, failure
):
    faults = {("candidate", "c2", 1): "invalid_request" if failure == "fault" else "target_failed"}
    if failure == "mixed_owners":
        faults[("candidate", "c1", 1)] = "time_budget"
    adapter = _CohortFaults(faults=faults, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    thresholds = (
        {**LENIENT, "target": {"max_count": 0, "max_rate": "1"}}
        if failure == "threshold"
        else LENIENT
    )
    options = dict(
        adapter=adapter,
        family14_settings=STABLE,
        campaign={"sample_retries": 0, "indeterminate": thresholds},
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspensions and not snapshot.finished
    (suspended,) = payloads(run, "campaign_suspended")
    assert suspended["owner"] == "target"
    assert (
        suspended["evidence"]["evidence_kind"]
        == {
            "gap": "final_rows",
            "threshold": "threshold",
            "fault": "fault_rows",
            "mixed_owners": "final_rows",
        }[failure]
    )
    stored, reference = _stored_reference(run)
    before = _bytes(run)
    binding = payloads(run, "finalists_bound")
    assert not payloads(run, "selection_decided")
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    # A crash after granting the repair cannot grant or execute it twice.
    if failure != "fault":
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_repairs_dispatched")
            with pytest.raises(Crash):
                await family14_run(tmp_path, monkeypatch, **options)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "finalists_bound") == binding
    assert _measurement_events(run, BASELINE).read_bytes() == before[BASELINE]
    assert _measurement_events(run, FINALIST).read_bytes().startswith(before[FINALIST])
    for case in ("c1", "c2"):
        expected = [(0, False), (1, True)] if ("candidate", case, 1) in faults else [(0, False)]
        assert _starts(run, FINALIST, case) == expected
    assert sum(row["count"] for row in payloads(run, "repairs_dispatched")) == (
        0 if failure == "fault" else len(faults)
    )
    assert len(payloads(run, "final_measurement_completed")) == 1
    _assert_stored(run, stored, reference)
    assert audit(run)["mismatches"] == []


async def test_stored_exclusion_does_not_excuse_the_same_fresh_finalist_gap(tmp_path, monkeypatch):
    from tests.campaign_drivers import MANIFEST_SEED, _RepeatTagged
    from tests.test_tree_campaign_family14_gate_audit import scorer_tables

    class ExcludedAndMissing(_RepeatTagged):
        armed = False
        repaired = False
        champions = 0

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                self.champions += 1
            baseline = candidate.kind == "champion" and self.champions == 1
            instance._lookup = lambda session: (
                "target_failed"
                if session.case_id == "c1"
                and session.requested_seed == MANIFEST_SEED + 4
                and (baseline or (self.armed and not self.repaired))
                else "completed"
            )
            return instance

    adapter = ExcludedAndMissing(used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    config = {
        **CONSTANT,
        "repeats": 4,
        "scorer": scorer_tables([0] * 4, [0.5] * 4, [1] * 4, [1] * 4),
    }
    options = dict(
        adapter=adapter,
        family14_settings=STABLE,
        campaign={"indeterminate": LENIENT, "sample_retries": 0},
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, config, **options)
    assert snapshot.suspensions[-1]["reason"] == "final_incomplete"
    stored, reference = _stored_reference(run)
    before = _measurement_events(run, BASELINE).read_bytes()
    (suspended,) = payloads(run, "campaign_suspended")
    assert [
        (row["measurement_id"], row["case_id"], row["sample_id"])
        for row in suspended["evidence"]["rows"]
    ] == [(FINALIST, "c1", "04")]
    assert not payloads(run, "global_comparison_completed")
    assert audit(run)["mismatches"] == []
    adapter.repaired = True
    run, snapshot = await family14_run(tmp_path, monkeypatch, config, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _measurement_events(run, BASELINE).read_bytes() == before
    (comparison,) = payloads(run, "global_comparison_completed")
    assert comparison["removed_pairs"] == [["c1", 4]]
    assert comparison["accepted"] is True
    assert payloads(run, "final_measurement_completed")[0]["complete"] is True
    _assert_stored(run, stored, reference)
    assert audit(run)["mismatches"] == []


async def test_stable_continue_seed_capability_change_has_no_carry_and_unpaired_contrast(
    tmp_path, monkeypatch
):
    adapter = _Resettable(used=EDITED["used"])
    config = {**CONSTANT, "repeats": 4}
    options = dict(adapter=adapter, family14_settings=STABLE)
    with monkeypatch.context() as patch:
        _cut_after_cohort_stage(patch, 1)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    run = tmp_path / "run"
    before = _bytes(run)
    stored, reference = _stored_reference(run)
    adapter.settings = "settings-v2"
    adapter.seed_mode = "none"
    adapter.capabilities = dataclasses.replace(adapter.capabilities, seed_mode="none")
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, config, **options)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[_continue(refusal.value)], **options
    )
    assert snapshot.finished["outcome"] == "completed"
    for mid, content in before.items():
        assert _measurement_events(run, mid).read_bytes() == content
    (rebound,) = payloads(run, "measurement_rebound")
    assert (rebound["from_measurement_id"], rebound["measurement_id"], rebound["imported"]) == (
        FINALIST,
        FINALIST + "@e1",
        0,
    )
    rows = _records(_measurement_events(run, FINALIST + "@e1"))
    assert not [row for row in rows if row["event"] == "sample_imported"]
    assert sum(row["event"] == "scoring_finished" for row in rows) == 8
    original_rows = _records(_measurement_events(run, FINALIST))

    def scores(records, case):
        return {
            row["payload"]["metrics"]["score"]
            for row in records
            if row["event"] == "scoring_finished" and row["payload"]["case_id"] == case
        }

    for case in ("c1", "c2"):
        assert scores(rows, case) == scores(original_rows, case)
    (comparison,) = payloads(run, "global_comparison_completed")
    # Constant identical scores across the change do not supply a seed proof.
    # Zero variation can still make the *unpaired* estimate exact.
    assert (comparison["pairing"], comparison["exact"]) == ("unpaired", True)
    assert payloads(run, "selection_decided")[0]["measurement_id"] == FINALIST + "@e1"
    _assert_stored(run, stored, reference)
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_stored_evidence_loss_after_binding_fails_persistence_and_restores_original(
    tmp_path, monkeypatch, damage
):
    options = dict(family14_settings=STABLE)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_bound")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    source = _measurement_events(run, BASELINE)
    original = source.read_bytes()
    history = (run / "campaign.jsonl").read_bytes()
    stored, reference = _stored_reference(run)
    if damage == "missing":
        source.unlink()
    else:
        source.write_bytes(original.replace(b'"timestamp":"2', b'"timestamp":"1', 1))
        assert source.read_bytes() != original
    _, failure = await family14_run(
        tmp_path,
        monkeypatch,
        expected_fault=("persistence_failed", "measurement_journal"),
        **options,
    )
    assert failure.terminal == "persistence_failed"
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    assert [row["measurement_id"] for row in payloads(run, "baseline_bound")] == [BASELINE]
    assert not payloads(run, "selection_decided")
    assert not any(
        row["event"].startswith("promotion_") for row in _records(run / "artifacts.jsonl")
    )
    source.write_bytes(original)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert source.read_bytes() == original
    _assert_stored(run, stored, reference)
    assert len(payloads(run, "final_measurement_bound")) == 1
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "cut",
    ["before_finalists", "before_binding", "partial_stage", "first_complete", "first_comparison"],
)
async def test_stable_continue_preserves_stored_prefix_and_rebinds_only_unfinished(
    tmp_path, monkeypatch, cut
):
    from evolution.tree import confidence_cohort
    from evolution.tree.baseline_import import fields_between
    from evolution.tree.campaign_epochs import epoch_of
    from evolution.tree.campaign_view import current_members

    adapter = _ResettableTagged(used=EDITED["used"])
    options = dict(adapter=adapter, family14_settings=STABLE, campaign=TWO_ATTEMPTS)
    with monkeypatch.context() as patch:
        if cut == "partial_stage":
            _cut_after_cohort_stage(patch, 1)
        else:
            _cut_hook(
                patch,
                {
                    "before_finalists": "before_finalists_bound",
                    "before_binding": "after_finalists_bound",
                    "first_complete": "after_cohort_measurement_completed",
                    "first_comparison": "after_global_comparison_completed",
                }[cut],
            )
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    run = tmp_path / "run"
    stored, reference = _stored_reference(run)
    before = _bytes(run)
    settled = payloads(run, "global_comparison_completed")
    binding = payloads(run, "finalists_bound")
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    seed_inputs = []
    real_seed = confidence_cohort.derive_seed

    def record_seed(**kwargs):
        seed_inputs.append(kwargs)
        return real_seed(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(confidence_cohort, "derive_seed", record_seed)
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, STATISTICAL, authorizations=[_continue(refusal.value)], **options
        )
    assert snapshot.finished["outcome"] == "completed"
    for mid, content in before.items():
        assert _measurement_events(run, mid).read_bytes() == content
    if cut == "before_finalists":
        assert not binding
        assert payloads(run, "finalists_bound")[0]["epoch"] == 1
    else:
        assert payloads(run, "finalists_bound") == binding
    expected = {
        "before_finalists": [],
        "before_binding": [],
        "partial_stage": [FINALIST, SECOND],
        "first_complete": [SECOND],
        "first_comparison": [],
    }[cut]
    rebound = payloads(run, "measurement_rebound")
    assert [row["from_measurement_id"] for row in rebound] == expected
    assert [row["imported"] for row in rebound] == [6 if cut == "partial_stage" else 8] * len(
        expected
    )
    members = [mid + "@e1" if mid in expected else mid for mid in (FINALIST, SECOND)]
    assert list(current_members(snapshot.rounds[1])) == members
    assert payloads(run, "selection_decided")[0]["measurement_id"] in members
    comparisons = payloads(run, "global_comparison_completed")
    assert comparisons[: len(settled)] == settled
    assert len(comparisons) == 2
    assert all(row["pairing"] == "exact" for row in comparisons)
    pending = comparisons[len(settled) :]
    assert len(seed_inputs) == len(pending)
    for inputs, compared in zip(seed_inputs, pending, strict=True):
        member = compared["finalist_measurement_id"]
        assert inputs["scope"] == inputs["purpose"] == "global"
        assert inputs["prefixes"] == [
            snapshot.rounds[1].cohort_completed[member]["prefix"],
            stored["prefix_digest"],
        ]
    for row in rebound:
        imported = [
            r["payload"]
            for r in _records(_measurement_events(run, row["measurement_id"]))
            if r["event"] == "sample_imported"
        ]
        assert len(imported) == row["imported"]
        assert all(item["effective_seed"] is not None for item in imported)
        assert all(
            item["source"]["measurement_id"] == row["from_measurement_id"] for item in imported
        )
        assert all(item["source"]["condition_fields"] == ["target"] for item in imported)
    # H12 will render these durable facts: the stored measurement's epoch,
    # rather than the round/final binding epoch, owns its condition relation.
    assert epoch_of(snapshot, BASELINE) == 0
    assert len(snapshot.epochs) == 2
    assert fields_between(snapshot, BASELINE) == ("target",)
    _assert_stored(run, stored, reference)
    assert audit(run)["mismatches"] == []


async def test_stable_final_does_not_select_successful_subset_of_fixed_membership(
    tmp_path, monkeypatch
):
    class FirstMemberFault(_CohortFaults):
        final_instances = 0

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if self.armed:
                self.final_instances += 1
                if self.final_instances == 2:
                    instance._lookup = lambda session: "completed"
            return instance

    adapter = FirstMemberFault(
        faults={("candidate", "c2", 1): "target_failed"}, used=EDITED["used"]
    )
    _arm_on_finalists_bound(monkeypatch, adapter)
    options = dict(
        adapter=adapter,
        family14_settings=STABLE,
        campaign={**TWO_ATTEMPTS, "sample_retries": 0, "indeterminate": LENIENT},
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspensions[-1]["reason"] == "final_incomplete"
    binding = payloads(run, "finalists_bound")
    assert binding[0]["members"] == [FINALIST, SECOND]
    successful = [
        r for r in _records(_measurement_events(run, SECOND)) if r["event"] == "scoring_finished"
    ]
    assert len(successful) == 2
    assert not payloads(run, "global_comparison_completed")
    assert not payloads(run, "selection_decided")
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "finalists_bound") == binding
    assert [
        r for r in _records(_measurement_events(run, SECOND)) if r["event"] == "scoring_finished"
    ] == successful
    assert len(payloads(run, "global_comparison_completed")) == 2
    assert _starts(run, FINALIST, "c2") == [(0, False), (1, True)]
    assert _starts(run, SECOND, "c2") == [(0, False)]
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("effect", ["rebaseline", "close"])
async def test_stable_authorized_other_effects_keep_h3_round_boundaries(
    tmp_path, monkeypatch, effect
):
    adapter = _ResettableTagged(used=EDITED["used"])
    options = dict(adapter=adapter, family14_settings=STABLE, campaign={"rounds": 2})
    with monkeypatch.context() as patch:
        _cut_after_cohort_stage(patch, 1)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    run = tmp_path / "run"
    before = _bytes(run)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, STATISTICAL, **options)
    from evolution.tree.execution.authorizations import Authorization

    assert effect in refusal.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refusal.value.instance_digest),
        "accept changed target",
        now="2026-10-11T00:00:00Z",
        effect=effect,
    )
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, STATISTICAL, authorizations=[decision], **options
    )
    assert snapshot.finished is not None
    for mid, content in before.items():
        assert _measurement_events(run, mid).read_bytes() == content
    assert not payloads(run, "measurement_rebound")
    assert snapshot.rounds[1].selection is None
    if effect == "rebaseline":
        assert snapshot.rounds[1].closed["outcome"] == "condition_changed"
        assert snapshot.rounds[2].baseline_measurement_id == "m-r02-champion@e1"
    else:
        assert snapshot.finished["reason"] == "condition_changed"
    assert audit(run)["mismatches"] == []
