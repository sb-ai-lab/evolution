"""H6 recovery: later finals preserve settled evidence and open only owed work.

Uncut controls compare event/naming order, ledger transactions and row identities;
each run's numerical evidence is audited independently, never compared cross-run.
"""

from __future__ import annotations

import dataclasses

import pytest

from evolution.tree.campaign import _Campaign
from evolution.tree.evolve import EvolveRefused
from tests.campaign_drivers import (
    EDITED,
    LENIENT,
    SERIAL,
    _CohortFaults,
    _continue,
    _cut_hook,
    _CutOnSend,
    _facts,
    _measurement_events,
    _PowerCut,
    _records,
    _Resettable,
    _starts,
)
from tests.campaign_fakes import Crash, crash_after_the_joint_stage_once
from tests.test_tree_campaign_family14_audit import audit
from tests.test_tree_campaign_family14_first_accepted_run import assert_unopened_absent, scenario
from tests.test_tree_campaign_family14_run import family14_run, payloads
from tests.test_tree_campaign_family14_stored_recovery import (
    BASELINE,
    _assert_stored,
    _bytes,
    _stored_reference,
)

pytestmark = pytest.mark.slow
LATER = "m-r01-final-02-a02"
LATER_CHAMPION = "m-r01-final-02-champion"


def _at_second_final(monkeypatch, callback):
    original = _Campaign.hook

    def hook(self, name):
        original(self, name)
        if name == "after_finalists_bound" and len(self.snapshot().rounds[1].finals) == 2:
            callback()

    monkeypatch.setattr(_Campaign, "hook", hook)


@pytest.mark.parametrize(
    "stability,cut",
    [
        (stability, cut)
        for stability in ("stable", "unstable")
        for cut in (
            "attempt_close",
            "binding",
            "declaration",
            "rows",
            "finalist_complete",
            "loss",
            "accepted",
        )
    ]
    + [("unstable", "champion_complete")],
)
async def test_later_final_cuts_resume_owed_work_with_immutable_prefixes(
    tmp_path, monkeypatch, stability, cut
):
    # Repeating a final or opening before a settled verdict changes these facts.
    options = scenario(tmp_path, monkeypatch, stability=stability)
    width = 1 if stability == "stable" else 2
    with monkeypatch.context() as patch:
        if cut == "rows":
            crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final-02")
        else:
            hook, call = {
                "attempt_close": ("after_attempt_closed", 2),
                "binding": ("after_finalists_bound", 2),
                "declaration": ("after_cohort_measurement_bound", width * 2),
                "champion_complete": ("after_cohort_measurement_completed", 3),
                "finalist_complete": ("after_cohort_measurement_completed", width * 2),
                "loss": ("after_global_comparison_completed", 2),
                "accepted": ("after_global_comparison_completed", 3),
            }[cut]
            _cut_hook(patch, hook, call=call)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    history = (run / "campaign.jsonl").read_bytes()
    before = _bytes(run)
    settled = payloads(run, "global_comparison_completed")
    complete = {BASELINE} | {
        row["measurement_id"] for row in payloads(run, "final_measurement_completed")
    }
    assert len(payloads(run, "attempt_opened")) == (3 if cut == "accepted" else 2)
    assert settled[0]["accepted"] is False
    if cut == "declaration":
        assert not _measurement_events(run, LATER).exists()
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert (run / "campaign.jsonl").read_bytes().startswith(history)
    for mid, content in before.items():
        after = _measurement_events(run, mid).read_bytes()
        assert after == content if mid in complete else after.startswith(content)
    assert payloads(run, "global_comparison_completed")[: len(settled)] == settled
    assert [p["accepted"] for p in payloads(run, "global_comparison_completed")] == [
        False,
        False,
        True,
    ]
    assert len(payloads(run, "finalists_bound")) == 3
    assert len(payloads(run, "final_measurement_completed")) == width * 3
    assert len(payloads(run, "selection_decided")) == 1
    assert len(payloads(run, "grouping_started")) == len(payloads(run, "reflection_finished")) == 1
    assert_unopened_absent(run, opened=3, k=5)
    assert audit(run)["mismatches"] == []
    control = tmp_path / "uncut"
    control.mkdir()
    plain_options = scenario(control, monkeypatch, stability=stability)
    plain, _ = await family14_run(control, monkeypatch, **plain_options)
    assert audit(plain)["mismatches"] == []
    assert _facts(run) == _facts(plain)


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("retries", [0, 1])
async def test_later_final_power_loss_keeps_real_attestations_and_retry_ownership(
    tmp_path, monkeypatch, stability, retries
):
    # Losing the prepared instance's identity must not make a settled D7 gap unauditable.
    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1)
    adapter = _CutOnSend(faults={}, used=EDITED["used"])
    options["adapter"] = adapter
    options["campaign"].update(**SERIAL, indeterminate=LENIENT, sample_retries=retries)
    with monkeypatch.context() as patch:
        _at_second_final(patch, lambda: setattr(adapter, "cut", ("candidate", "c2")))
        with pytest.raises(_PowerCut):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    baseline = _measurement_events(run, BASELINE).read_bytes()
    earlier = payloads(run, "global_comparison_completed")
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _measurement_events(run, BASELINE).read_bytes() == baseline
    assert payloads(run, "global_comparison_completed")[:1] == earlier
    assert _starts(run, LATER, "c1") == [(0, False)]
    assert _starts(run, LATER, "c2") == ([(0, False), (1, False)] if retries else [(0, False)])
    assert not payloads(run, "repairs_dispatched")
    later = payloads(run, "global_comparison_completed")[1]
    assert (later["accepted"], later["reason"]) == (
        bool(retries),
        "global_off" if retries else "final_incomplete",
    )
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize(
    "failure", ["threshold", "below_threshold", "target_fault", "scorer_fault"]
)
async def test_feedback_none_attempt_repair_never_opens_past_unsettled_work(
    tmp_path, monkeypatch, stability, failure
):
    options = scenario(tmp_path, monkeypatch, stability=stability, losers=0)
    options["config"]["gate"] = {"mode": "observe", "fresh_parent": False}
    fault = "invalid_request" if failure == "target_fault" else "target_failed"
    adapter = _CohortFaults(faults={("candidate", "c2", 1): fault}, used=EDITED["used"])
    options["adapter"] = adapter
    options["campaign"].update(sample_retries=0, indeterminate=LENIENT)
    if failure in ("threshold", "target_fault"):
        options["campaign"]["indeterminate"] = {
            **LENIENT,
            "target": {"max_count": 0, "max_rate": "1"},
        }
    marker = tmp_path / "scorer-broken.flag"
    if failure == "scorer_fault":
        adapter.faults.clear()
        options["config"]["scorer"] = options["config"]["scorer"].replace(
            '"metrics": {"score": score}',
            f'"metrics": {{"score": 2 if edited and payload["case_id"] == "c2" '
            f"and pathlib.Path({str(marker)!r}).exists() else score}}",
        )
    original = _Campaign.hook

    def arm(self, name):
        original(self, name)
        if name == "after_attempt_input_frozen" and len(self.snapshot().rounds[1].attempts) == 1:
            adapter.armed = True
            marker.touch()

    monkeypatch.setattr(_Campaign, "hook", arm)
    if failure == "below_threshold":
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_attempt_closed")
            with pytest.raises(Crash):
                await family14_run(tmp_path, patch, **options)
        run = tmp_path / "run"
        assert not payloads(run, "campaign_suspended")
        assert payloads(run, "attempt_closed")[0]["reason"] == "incomplete_sample"
    else:
        run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
        assert snapshot.suspended and not snapshot.finished
        (suspended,) = payloads(run, "campaign_suspended")
        assert suspended["owner"] == ("scorer" if failure == "scorer_fault" else "target")
        assert suspended["evidence"]["evidence_kind"] == (
            "threshold" if failure == "threshold" else "fault_rows"
        )
        assert not payloads(run, "attempt_closed")
        assert not payloads(run, "selection_decided")
    assert len(payloads(run, "attempt_opened")) == 1
    assert_unopened_absent(run, opened=1, k=5)
    baseline = _measurement_events(run, BASELINE).read_bytes()
    scored = [
        row
        for row in _records(_measurement_events(run, "m-r01-a01"))
        if row["event"] == "scoring_finished"
    ]
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    marker.unlink(missing_ok=True)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert _measurement_events(run, BASELINE).read_bytes() == baseline
    assert [
        row
        for row in _records(_measurement_events(run, "m-r01-a01"))
        if row["event"] == "scoring_finished"
    ][: len(scored)] == scored
    assert _starts(run, "m-r01-a01", "c1") == [(0, False)]
    assert _starts(run, "m-r01-a01", "c2") == (
        [(0, False)] if failure == "below_threshold" else [(0, False), (1, True)]
    )
    assert len(payloads(run, "attempt_opened")) == (2 if failure == "below_threshold" else 1)
    assert all(p["feedback"]["attempts"] == [] for p in payloads(run, "attempt_input_frozen"))
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_later_final_gap_preserves_membership_and_repairs_only_named_pair(
    tmp_path, monkeypatch, stability
):
    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1)
    adapter = _CohortFaults(faults={("candidate", "c2", 1): "target_failed"}, used=EDITED["used"])
    options["adapter"] = adapter
    options["campaign"].update(sample_retries=0, indeterminate=LENIENT)
    _at_second_final(monkeypatch, lambda: setattr(adapter, "armed", True))
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.suspended
    assert payloads(run, "campaign_suspended")[0]["reason"] == "final_incomplete"
    before, bindings = _bytes(run), payloads(run, "finalists_bound")
    settled = payloads(run, "global_comparison_completed")
    assert len(bindings) == 2 and len(settled) == 1
    assert_unopened_absent(run, opened=2, k=5)
    assert not payloads(run, "selection_decided")
    assert audit(run)["mismatches"] == []
    adapter.faults.clear()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_repairs_dispatched")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "finalists_bound") == bindings
    assert payloads(run, "global_comparison_completed")[:1] == settled
    for mid, content in before.items():
        after = _measurement_events(run, mid).read_bytes()
        assert after.startswith(content) if mid == LATER else after == content
    assert _starts(run, LATER, "c1") == [(0, False)]
    assert _starts(run, LATER, "c2") == [(0, False), (1, True)]
    assert sum(p["count"] for p in payloads(run, "repairs_dispatched")) == 1
    assert len(payloads(run, "attempt_opened")) == 2
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "stability,cut,unseeded",
    [
        ("stable", "rows", False),
        ("stable", "rows", True),
        ("unstable", "rows", False),
        ("unstable", "champion_complete", False),
        ("unstable", "champion_complete", True),
    ],
)
async def test_continue_inside_later_final_rebinds_only_unfinished_provenance(
    tmp_path, monkeypatch, stability, cut, unseeded
):
    from evolution.tree.campaign_schema import current_members

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1)
    options["adapter"] = adapter = _Resettable(used=EDITED["used"])
    with monkeypatch.context() as patch:
        if cut == "rows":
            crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final-02")
        else:
            _cut_hook(patch, "after_cohort_measurement_completed", call=3)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    before, bindings = _bytes(run), payloads(run, "finalists_bound")
    settled = payloads(run, "global_comparison_completed")
    stored, reference = _stored_reference(run)
    adapter.settings = "settings-v2"
    if unseeded:
        adapter.seed_mode = "none"
        adapter.capabilities = dataclasses.replace(adapter.capabilities, seed_mode="none")
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, **options)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, authorizations=[_continue(refusal.value)], **options
    )
    assert snapshot.finished["outcome"] == "completed"
    assert payloads(run, "finalists_bound") == bindings
    assert payloads(run, "global_comparison_completed")[:1] == settled
    for mid, content in before.items():
        assert _measurement_events(run, mid).read_bytes() == content
    rebound = payloads(run, "measurement_rebound")
    expected = [LATER_CHAMPION, LATER] if stability == "unstable" and cut == "rows" else [LATER]
    assert [p["from_measurement_id"] for p in rebound] == expected
    assert [p["measurement_id"] for p in rebound] == [mid + "@e1" for mid in expected]
    assert [p["imported"] for p in rebound] == [0 if unseeded else 2] * len(expected)
    state = snapshot.rounds[1]
    assert current_members(state, final=state.finals[0]) == tuple(bindings[0]["members"])
    assert current_members(state, final=state.finals[1]) == tuple(
        mid + "@e1" if mid in expected else mid for mid in bindings[1]["members"]
    )
    later = payloads(run, "global_comparison_completed")[1]
    assert later["pairing"] == ("unpaired" if unseeded else "exact")
    assert payloads(run, "selection_decided")[0]["measurement_id"] == LATER + "@e1"
    if stability == "stable":
        _assert_stored(run, stored, reference)
    for mid in expected:
        imported = [
            r["payload"]
            for r in _records(_measurement_events(run, mid + "@e1"))
            if r["event"] == "sample_imported"
        ]
        assert all(
            r["source"]["measurement_id"] == mid and r["source"]["condition_fields"] == ["target"]
            for r in imported
        )
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_cancellation_during_later_final_stops_without_selecting_or_opening(
    tmp_path, monkeypatch, stability
):
    from tests.campaign_drivers import _cancel_when

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1)
    options["campaign"].update(SERIAL)
    run = tmp_path / "run"
    stop = _cancel_when(run, LATER, b'"scoring_finished"')
    run, snapshot = await family14_run(tmp_path, monkeypatch, cancelled=stop, **options)
    assert (snapshot.finished["outcome"], snapshot.finished["reason"]) == ("stopped", "cancelled")
    assert len(payloads(run, "finalists_bound")) == 2
    assert len(payloads(run, "global_comparison_completed")) == 1
    assert not payloads(run, "selection_decided")
    assert_unopened_absent(run, opened=2, k=5)
    assert not any(r["event"].startswith("promotion_") for r in _records(run / "artifacts.jsonl"))
    assert audit(run)["mismatches"] == []
    history = (run / "campaign.jsonl").read_bytes()
    before = _bytes(run)
    # A finished cancellation verifies on the same invocation; it cannot resume work.
    run, again = await family14_run(tmp_path, monkeypatch, **options)
    assert again.finished == snapshot.finished
    assert (run / "campaign.jsonl").read_bytes() == history
    assert _bytes(run) == before


@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("effect", ["rebaseline", "close"])
async def test_later_final_ordered_condition_stop_keeps_prior_finals_frozen(
    tmp_path, monkeypatch, stability, effect
):
    from evolution.tree.execution.authorizations import Authorization

    options = scenario(tmp_path, monkeypatch, stability=stability, losers=1)
    options["adapter"] = adapter = _Resettable(used=EDITED["used"])
    options["campaign"]["rounds"] = 2
    with monkeypatch.context() as patch:
        crash_after_the_joint_stage_once(patch, measurement_prefix="m-r01-final-02")
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    before = _bytes(run)
    bindings, settled = (
        payloads(run, "finalists_bound"),
        payloads(run, "global_comparison_completed"),
    )
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, **options)
    assert effect in refusal.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        str(refusal.value.instance_digest),
        "accept changed target",
        now="2026-10-11T00:00:00Z",
        effect=effect,
    )
    if effect == "rebaseline":
        # The durable authorization owns the remainder of the transition.
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_condition_applied")
            with pytest.raises(Crash):
                await family14_run(tmp_path, patch, authorizations=[decision], **options)
    # A close completes in this invocation. Its early owed-terminal recovery
    # bypasses the driver's _Campaign.result seam and belongs to H12 admission.
    run, snapshot = await family14_run(tmp_path, monkeypatch, authorizations=[decision], **options)
    assert snapshot.finished is not None
    for mid, content in before.items():
        assert _measurement_events(run, mid).read_bytes() == content
    assert [p for p in payloads(run, "finalists_bound") if p["round"] == 1] == bindings
    assert [p for p in payloads(run, "global_comparison_completed") if p["round"] == 1] == settled
    assert not payloads(run, "measurement_rebound")
    assert not [p for p in payloads(run, "selection_decided") if p["round"] == 1]
    assert len(payloads(run, "authorization_applied")) == 1
    if effect == "close":
        assert snapshot.finished["reason"] == "condition_changed"
    else:
        assert snapshot.rounds[1].closed["outcome"] == "condition_changed"
        assert snapshot.rounds[2].baseline_measurement_id == "m-r02-champion@e1"
    assert_unopened_absent(run, opened=2, k=5)
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_later_stored_baseline_read_failure_restores_same_run(tmp_path, monkeypatch, damage):
    options = scenario(tmp_path, monkeypatch, stability="stable", losers=1)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_bound", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, patch, **options)
    run = tmp_path / "run"
    source = _measurement_events(run, BASELINE)
    original = source.read_bytes()
    bindings, settled = (
        payloads(run, "finalists_bound"),
        payloads(run, "global_comparison_completed"),
    )
    stored, reference = _stored_reference(run)
    if damage == "missing":
        source.unlink()
    else:
        source.write_bytes(original.replace(b'"timestamp":"2', b'"timestamp":"1', 1))
        assert source.read_bytes() != original
    _, fault = await family14_run(
        tmp_path,
        monkeypatch,
        expected_fault=("persistence_failed", "measurement_journal"),
        **options,
    )
    assert fault.terminal == "persistence_failed"
    assert payloads(run, "global_comparison_completed") == settled
    assert payloads(run, "finalists_bound") == bindings
    assert not payloads(run, "selection_decided")
    assert_unopened_absent(run, opened=2, k=5)
    source.write_bytes(original)
    run, snapshot = await family14_run(tmp_path, monkeypatch, **options)
    assert snapshot.finished["outcome"] == "completed"
    assert source.read_bytes() == original
    assert payloads(run, "global_comparison_completed")[:1] == settled
    assert len(payloads(run, "baseline_bound")) == 1
    assert len(payloads(run, "attempt_opened")) == 2
    _assert_stored(run, stored, reference)
    assert audit(run)["mismatches"] == []
