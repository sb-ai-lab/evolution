"""Scheduled scenario slots are conditional; paid journal work is separate."""

import pytest

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("repeats,reuse,fresh", [(1, 133, 266), (3, 399, 798)])
def test_133_case_examples_keep_preflight_separate(repeats, reuse, fresh):
    from evolution.tree.family15_report import scenario_cost

    cost = scenario_cost(133, 133, repeats, baseline_imports=133 * repeats, verification="reuse")
    assert cost["baseline_slots"] == 0
    assert cost["candidate_gate_slots"] + cost["candidate_final_slots"] == reuse
    assert cost["connection_preflight_slots"] == 1
    if repeats == 1:
        assert cost["simplest_passing_path_slots"] == 134
    assert scenario_cost(133, 133, repeats, verification="fresh")["candidate_total_slots"] == fresh


def test_failed_gate_has_no_final_cost_and_all_attempts_shares_champion():
    from evolution.tree.family15_report import scenario_cost

    failed = scenario_cost(133, 5, 3, gate_passed=False, fresh_parent=True)
    assert failed["candidate_final_slots"] == 0
    assert failed["parent_slots"] == 15
    assert failed["noise_diagnostic_slots"] == 0
    assert set(failed) == {
        "basis",
        "baseline_slots",
        "candidate_gate_slots",
        "candidate_final_slots",
        "candidate_total_slots",
        "parent_slots",
        "champion_slots",
        "champion_basis",
        "slots_per_refresh",
        "connection_preflight_slots",
        "probes_and_retries",
        "noise_diagnostic_slots",
        "simplest_passing_path_slots",
    }
    shared = scenario_cost(133, 5, 3, stability="unstable", finalists=4, round_stop="all_attempts")
    serial = scenario_cost(
        133, 5, 3, stability="unstable", finalists=4, round_stop="first_accepted"
    )
    assert shared["champion_slots"] == 399
    assert serial["champion_slots"] == 1596
    assert "judge" not in str(shared)


@pytest.mark.parametrize("verification,total,imports", [("reuse", 8, 4), ("fresh", 12, 0)])
async def test_committed_work_counts_actual_shared_champion_and_excludes_imports(
    tmp_path, monkeypatch, verification, total, imports
):
    from evolution.tree.campaign import inspect_campaign
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(
        tmp_path,
        monkeypatch,
        stability="unstable",
        verification=verification,
        stop="result",
        campaign_options={"attempts_per_round": 2, "round_stop": "all_attempts"},
    )
    report = inspect_campaign(run).to_payload()["independent"]
    work = report["committed_work"]
    assert work["measurement"]["scenario_slots"] == total
    assert work["measurement"]["imported_slots"] == imports
    assert work["measurement"]["scorer_executions"] == total
    assert work["connection_preflight"]["scenario_slots"] == 1
    assert work["connection_preflight"]["scorer_executions"] == 1
    assert report["planned_work"]["champion_scope"] == "frozen_nonempty_finalist_set"
    from evolution.tree.family15_report import report_lines

    rendered = report_lines(report)
    assert any(f"committed measurement · {total} scenario slots" in line for line in rendered)
    assert any("separate committed preflight · 1 scenario slots" in line for line in rendered)


async def test_captured_prefix_never_counts_later_campaign_or_measurement_work(
    tmp_path, monkeypatch
):
    from evolution.tree.evolve import evolve
    from evolution.tree.family15_report import campaign15_projection
    from evolution.tree.run_reader import open_run
    from evolution.tree.run_watch import WatchSession
    from tests.campaign_drivers import _Resettable
    from tests.test_tree_campaign_family15_condition_change import _interrupted

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_baseline_complete")
    handle = open_run(run)
    watch = WatchSession(run)
    history = dict(watch._measurement_history)
    watch.close()
    before = campaign15_projection(
        handle.snapshot, run_root=run, campaign_records=handle.records, measurement_records=history
    )
    await evolve(tmp_path / "manifest.json", run, adapter_factory=lambda *a: adapter)
    assert len(open_run(run).snapshot.measurements) > len(handle.snapshot.measurements)
    after = campaign15_projection(
        handle.snapshot, run_root=run, campaign_records=handle.records, measurement_records=history
    )
    assert after == before
    # Without an explicit earlier prefix the report refuses current work counts.
    assert campaign15_projection(handle.snapshot, run_root=run)["evidence"] == "unavailable"


async def test_presentation_failure_is_observational(tmp_path, monkeypatch):
    from evolution.tree import family15_report
    from evolution.tree.campaign import inspect_campaign
    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family15_completion import completion_run

    original = fixtures.evolve

    async def configured(*args, **kwargs):
        kwargs["notes"] = lambda line: None
        return await original(*args, **kwargs)

    def broken(report):
        raise RuntimeError("private rendering failure")

    monkeypatch.setattr(fixtures, "evolve", configured)
    monkeypatch.setattr(family15_report, "report_lines", broken)
    run = await completion_run(tmp_path, monkeypatch, stop="result")
    assert inspect_campaign(run).to_payload()["campaign_outcome"]["outcome"] == "completed"


@pytest.mark.parametrize("verification,remaining", [("reuse", 1), ("fresh", 2)])
async def test_remaining_after_gate_credits_completed_gate_and_baseline(
    tmp_path, monkeypatch, verification, remaining
):
    from evolution.tree.campaign import inspect_campaign
    from tests.campaign_drivers import _only_c1
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(
        tmp_path,
        monkeypatch,
        verification=verification,
        used=_only_c1,
        fresh=True,
        stop="after_comparison_completed",
    )
    report = inspect_campaign(run).to_payload()["independent"]
    left = report["remaining_work"]
    assert left["baseline_slots"] == left["candidate_gate_slots"] == left["parent_gate_slots"] == 0
    assert left["candidate_final_slots_if_gates_pass"] == remaining
    assert report["attempts"][0]["conditional_cost"]["candidate_gate_slots"] == 1
    assert report["attempts"][0]["conditional_cost"]["parent_slots"] == 1
    assert report["committed_work"]["measurement"]["scenario_slots"] == 4


async def test_remaining_failed_gate_buys_no_final(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from tests.test_tree_campaign_family15_gate import gate_run

    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    run = await gate_run(failed_root, monkeypatch, passing=False)
    left = inspect_campaign(run).to_payload()["independent"]["remaining_work"]
    assert left["candidate_final_slots_if_gates_pass"] == 0
    assert left["noise_diagnostic_slots"] == 0
    assert set(left) == {
        "basis",
        "scope",
        "baseline_slots",
        "candidate_gate_slots",
        "parent_gate_slots",
        "candidate_final_slots_if_gates_pass",
        "champion_slots_for_reached_finalists",
        "remaining_refreshes",
        "refresh_scenario_slots",
        "probes_and_retries",
        "noise_diagnostic_slots",
    }


async def test_remaining_shared_champion_counts_once_for_frozen_finalist_set(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(
        tmp_path,
        monkeypatch,
        stability="unstable",
        stop="after_champion_final_bound",
        campaign_options={"attempts_per_round": 2, "round_stop": "all_attempts"},
    )
    left = inspect_campaign(run).to_payload()["independent"]["remaining_work"]
    assert left["champion_slots_for_reached_finalists"] == 2
    assert left["candidate_gate_slots"] == 0
    # Both full gates are carried, so neither completion buys more Candidate slots.
    assert left["candidate_final_slots_if_gates_pass"] == 0


@pytest.mark.parametrize("round_stop", ["first_accepted", "all_attempts"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_remaining_final_sides_follow_actual_authorized_rebound(
    tmp_path, monkeypatch, round_stop, stability
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.run_reader import open_run
    from tests.campaign_drivers import _continue, _only_c1, _records, _Resettable
    from tests.test_tree_campaign_family15_completion import completion_run

    adapter = _Resettable(used=_only_c1)
    options = dict(
        adapter=adapter,
        used=_only_c1,
        stability=stability,
        campaign_options={"attempts_per_round": 1, "round_stop": round_stop},
    )
    await completion_run(
        tmp_path, monkeypatch, stop="before_candidate_completion_dispatch", hard=True, **options
    )
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, **options)
    run = await completion_run(
        tmp_path, monkeypatch, authorizations=[_continue(refused.value)], **options
    )
    snapshot = open_run(run).snapshot
    extra = snapshot.rounds[1].attempts["r01-a01"].family15
    original = extra.final_binding.payload["candidate"]["measurement_id"]
    current = snapshot.family15.current_measurements[original]
    assert current.endswith("@e1")
    assert extra.completion.payload["source"]["measurement_id"] == current
    if stability == "unstable":
        champion = snapshot.rounds[1].family15.champions[0].payload["measurement_id"]
        rebound = snapshot.family15.current_measurements[champion]
        assert rebound.endswith("@e1")
        rows = _records(run / "measurements" / rebound / "events.jsonl")
        assert sum(r["event"] == "scoring_finished" for r in rows) == 2
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    report = inspect_campaign(run).to_payload()["independent"]
    assert report["remaining_work"]["candidate_final_slots_if_gates_pass"] == 0
    assert report["remaining_work"]["champion_slots_for_reached_finalists"] == 0
    assert {p: p.read_bytes() for p in run.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("damage", ["invalid", "torn", "missing", "empty", "truncated"])
async def test_corrupt_completed_preflight_cost_is_unknown_not_zero(tmp_path, monkeypatch, damage):
    from evolution.tree.campaign import inspect_campaign
    from tests.campaign_drivers import _records
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(tmp_path, monkeypatch, stop="result")
    before = inspect_campaign(run).to_payload()["independent"]
    assert before["committed_work"]["connection_preflight"]["scenario_slots"] == 1
    record = next(
        r for r in _records(run / "campaign.jsonl") if r["event"] == "connection_preflight_started"
    )
    path = run / "connection" / record["operation_id"] / "events.jsonl"
    if damage == "missing":
        path.unlink()
    elif damage == "torn":
        path.write_bytes(path.read_bytes() + b'{"torn":')
    elif damage == "empty":
        path.write_bytes(b"")
    elif damage == "truncated":
        path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
    else:
        path.write_bytes(b"{invalid}\n")
    status = inspect_campaign(run, verify=True).to_payload()
    if damage != "torn":
        assert status["verification"]["mismatches"]
    # A later torn tail need not invalidate the old cited prefix; current
    # complete-probe accounting still refuses the unreadable whole journal.
    assert status["independent"]["evidence"] == "unavailable"
    assert status["independent"]["committed_work"] is None


async def test_corrupt_measurement_is_refused_by_both_cost_readers(tmp_path, monkeypatch):
    from evolution.tree.run_reader import (
        committed_campaign_work,
        open_run,
        remaining_measurement_slots,
    )
    from evolution.tree.storage.journal_errors import JournalFault
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(tmp_path, monkeypatch, stop="result")
    handle = open_run(run)
    path = run / "measurements" / "m-r00-champion" / "events.jsonl"
    path.write_bytes(path.read_bytes() + b"{invalid}\n")
    with pytest.raises(JournalFault):
        committed_campaign_work(run, handle.snapshot, handle.records)
    with pytest.raises(JournalFault):
        remaining_measurement_slots(run, handle.snapshot)


async def test_frozen_unfinished_valid_prefix_stays_reportable_with_torn_tail(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.run_reader import (
        committed_campaign_work,
        open_run,
        remaining_measurement_slots,
    )
    from tests.campaign_drivers import _continue, _only_c1, _Resettable
    from tests.test_tree_campaign_family15_completion import completion_run

    adapter = _Resettable(used=_only_c1)
    options = dict(adapter=adapter, used=_only_c1)
    await completion_run(
        tmp_path, monkeypatch, stop="before_candidate_completion_dispatch", hard=True, **options
    )
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, **options)
    run = await completion_run(
        tmp_path, monkeypatch, authorizations=[_continue(refused.value)], **options
    )
    handle = open_run(run)
    old = "m-r01-a01-completion"
    entry = handle.snapshot.measurements[old]
    assert entry["frozen"] and not entry["complete"]
    before_work = committed_campaign_work(run, handle.snapshot, handle.records)
    before_slots = remaining_measurement_slots(run, handle.snapshot)
    path = run / "measurements" / old / "events.jsonl"
    path.write_bytes(path.read_bytes() + b'{"torn":')
    before = path.read_bytes()
    assert committed_campaign_work(run, handle.snapshot, handle.records) == before_work
    assert remaining_measurement_slots(run, handle.snapshot) == before_slots
    public = inspect_campaign(run).to_payload()
    assert public["siblings"][old] == "born"
    assert public["independent"]["evidence"] == "authenticated_publications"
    assert public["independent"]["committed_work"] == before_work
    assert path.read_bytes() == before


@pytest.mark.parametrize("empty_file", [False, True])
async def test_owed_unborn_measurement_has_zero_new_work(tmp_path, monkeypatch, empty_file):
    from evolution.tree.run_reader import (
        committed_campaign_work,
        open_run,
        remaining_measurement_slots,
    )
    from tests.campaign_drivers import _only_c1
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(
        tmp_path, monkeypatch, used=_only_c1, stop="before_candidate_completion_birth", hard=True
    )
    path = run / "measurements" / "m-r01-a01-completion" / "events.jsonl"
    assert not path.exists()
    if empty_file:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"")
    handle = open_run(run)
    work = committed_campaign_work(run, handle.snapshot, handle.records)
    assert work["measurement"]["scenario_slots"] == 3
    remaining, _ = remaining_measurement_slots(run, handle.snapshot)
    assert "m-r01-a01-completion" not in remaining


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_fresh_rebound_sides_credit_current_scores_before_completion_terminal(
    tmp_path, monkeypatch, stability
):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _only_c1, _Resettable
    from tests.test_tree_campaign_family15_completion import completion_run
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    adapter = _Resettable(used=_only_c1)
    options = dict(
        adapter=adapter,
        used=_only_c1,
        verification="fresh",
        stability=stability,
        campaign_options={"attempts_per_round": 1, "round_stop": "all_attempts"},
    )
    run = await completion_run(
        tmp_path, monkeypatch, stop="before_candidate_completion_dispatch", hard=True, **options
    )
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await completion_run(tmp_path, monkeypatch, **options)
    original_stage = _Campaign.run_stage
    original_joint = _Campaign.run_joint_stage
    seen = []

    def inspect_finished_rows(self):
        snapshot = self.snapshot()
        extra = snapshot.rounds[1].attempts["r01-a01"].family15
        assert extra.completion is None and extra.final_completion is None
        assert all(mid.endswith("@e1") for mid in snapshot.family15.current_measurements.values())
        public = inspect_campaign(run).to_payload()["independent"]
        assert public["remaining_work"]["candidate_final_slots_if_gates_pass"] == 0
        assert public["remaining_work"]["champion_slots_for_reached_finalists"] == 0
        seen.append(public)
        raise GatePowerCut()

    async def stage(self, mid, *args, **kwargs):
        outcome = await original_stage(self, mid, *args, **kwargs)
        if mid.endswith("@e1") and self.snapshot().measurements[mid]["role"] == "family15_final":
            inspect_finished_rows(self)
        return outcome

    async def joint(self, mids, *args, **kwargs):
        outcome = await original_joint(self, mids, *args, **kwargs)
        if any(mid.endswith("@e1") for mid in mids):
            inspect_finished_rows(self)
        return outcome

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "run_stage", stage)
        patch.setattr(_Campaign, "run_joint_stage", joint)
        await completion_run(
            tmp_path, patch, authorizations=[_continue(refused.value)], hard=True, **options
        )
    assert len(seen) == 1


async def test_omitted_committed_measurement_prefix_is_unknown_in_both_cost_readers(
    tmp_path, monkeypatch
):
    from evolution.tree.run_reader import (
        committed_campaign_work,
        open_run,
        remaining_measurement_slots,
    )
    from evolution.tree.storage.journal_errors import JournalFault
    from tests.test_tree_campaign_family15_completion import completion_run

    run = await completion_run(tmp_path, monkeypatch, stop="result")
    handle = open_run(run)
    with pytest.raises(JournalFault):
        committed_campaign_work(run, handle.snapshot, handle.records, measurement_records={})
    with pytest.raises(JournalFault):
        remaining_measurement_slots(run, handle.snapshot, measurement_records={})


@pytest.mark.parametrize(
    "cut,owed_count,remaining_count",
    [
        ("after_focus_stage", 1, 0),
        ("after_focus_decided", 2, 1),
    ],
)
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_owed_slots_wait_for_focus_but_conditional_work_assumes_pass(
    tmp_path, monkeypatch, cut, owed_count, remaining_count, fresh, verification
):
    from evolution.tree.campaign import _unfilled_slots
    from evolution.tree.campaign_measurement import FrozenMeasurement
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.campaign_view import owed_rows, stage_progress
    from evolution.tree.family15_report import remaining_cost
    from evolution.tree.run_reader import remaining_measurement_slots
    from tests.test_tree_campaign_family15_gate import gate_run

    run = await gate_run(
        tmp_path,
        monkeypatch,
        stop=cut,
        fresh=fresh,
        gate_options={"stages": "focus_first", "verification": verification},
    )
    view = inspect_view(run)
    snapshot = view.snapshot
    attempt = snapshot.rounds[1].attempts["r01-a01"]
    mid = attempt.measurement_id
    policy = CampaignPolicyV12.from_payload(snapshot.identity["policy"])
    assert len(owed_rows(snapshot, mid)) == owed_count
    remaining, scheduled = remaining_measurement_slots(run, snapshot)
    assert scheduled[mid] == owed_count
    assert remaining[mid] == remaining_count
    assert remaining_cost(snapshot, policy, remaining, scheduled)["candidate_gate_slots"] == 1
    assert remaining_cost(snapshot, policy, remaining, scheduled)[
        "candidate_final_slots_if_gates_pass"
    ] == (2 if verification == "fresh" else 0)
    assert remaining_cost(snapshot, policy, remaining, scheduled)["parent_gate_slots"] == (
        1 if fresh else 0
    )
    assert stage_progress(view, mid, policy.sample_retries) == (1, False)
    assert _unfilled_slots(snapshot, mid, FrozenMeasurement(run, mid), policy.looks) == 0


async def test_focus_rejected_attempt_has_no_remaining_causal_slots(tmp_path, monkeypatch):
    from evolution.tree.campaign import _unfilled_slots
    from evolution.tree.campaign_measurement import FrozenMeasurement
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.campaign_view import owed_rows, stage_progress
    from evolution.tree.family15_report import remaining_cost
    from evolution.tree.run_reader import remaining_measurement_slots
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family15_gate import gate_run

    run = await gate_run(
        tmp_path,
        monkeypatch,
        passing=False,
        gate_options={"stages": "focus_first"},
        scorer=TWO_CASE_SCORER.replace(
            '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
        ),
    )
    view = inspect_view(run)
    snapshot = view.snapshot
    mid = snapshot.rounds[1].attempts["r01-a01"].measurement_id
    policy = CampaignPolicyV12.from_payload(snapshot.identity["policy"])
    assert {row["case_id"] for row in owed_rows(snapshot, mid)} == {"c1"}
    remaining, scheduled = remaining_measurement_slots(run, snapshot)
    assert (scheduled[mid], remaining[mid]) == (1, 0)
    assert stage_progress(view, mid, policy.sample_retries) == (1, False)
    assert _unfilled_slots(snapshot, mid, FrozenMeasurement(run, mid), policy.looks) == 0
    assert remaining_cost(snapshot, policy, remaining, scheduled)["candidate_gate_slots"] == 0


def test_focus_rejection_scenario_owes_only_focus_on_both_owned_sides():
    from evolution.tree.family15_report import scenario_cost

    result = scenario_cost(
        10, 8, 3, gate_passed=False, fresh_parent=True, stages="focus_first", focus_cases=2
    )
    assert result["candidate_gate_slots"] == result["parent_slots"] == 6
    assert result["candidate_final_slots"] == 0


def test_passing_focus_then_failed_causal_pays_full_gate_and_no_final():
    from evolution.tree.family15_report import scenario_cost

    result = scenario_cost(
        10,
        8,
        3,
        gate_passed=False,
        fresh_parent=True,
        stages="focus_first",
        focus_cases=2,
        focus_passed=True,
    )
    assert result["candidate_gate_slots"] == result["parent_slots"] == 24
    assert result["candidate_final_slots"] == 0


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_pending_focus_conditional_scenario_matches_single(
    tmp_path, monkeypatch, verification, fresh_parent, stability
):
    from evolution.tree.campaign import inspect_campaign
    from tests.test_tree_campaign_family15_gate import gate_run

    reports = []
    for stages in ("single", "focus_first"):
        directory = tmp_path / stages
        directory.mkdir()
        run = await gate_run(
            directory,
            monkeypatch,
            fresh=fresh_parent,
            stop="after_comparison_bound",
            gate_options={"stages": stages, "verification": verification, "stability": stability},
        )
        reports.append(inspect_campaign(run).to_payload()["independent"])
    single, scoped = reports
    assert scoped["attempts"][0]["conditional_cost"] == single["attempts"][0]["conditional_cost"]
    assert scoped["remaining_work"] == single["remaining_work"]
    cost = scoped["attempts"][0]["conditional_cost"]
    assert cost["candidate_gate_slots"] == 2
    assert cost["parent_slots"] == (2 if fresh_parent else 0)
    assert cost["candidate_final_slots"] == (2 if verification == "fresh" else 0)
    assert scoped["remaining_work"]["candidate_gate_slots"] == 2
    assert scoped["remaining_work"]["parent_gate_slots"] == (2 if fresh_parent else 0)
