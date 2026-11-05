"""Actual producer preflight, budget/recovery and declared refresh schedule."""

import json
from collections import Counter

import pytest

from tests.campaign_drivers import TWO_CASE_SCORER, _records
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import family14_document
from tests.family15_fakes import family15_admitted, v16_document

pytestmark = pytest.mark.slow


class Boundary(BaseException):
    pass


def _assert_preflight_prefix(run, cut):
    from evolution.tree.artifacts.artifact_projections import inspect_auxiliary

    last_event, opened, failed, closed = {
        "after_baseline_complete": ("baseline_complete", 0, 0, 0),
        "after_attempt_opened": ("attempt_opened", 1, 0, 0),
        "after_comparison_data_declared": ("auxiliary_payload_declared", 1, 0, 0),
        "after_comparison_data_published": ("auxiliary_payload_declared", 1, 0, 0),
        "after_comparison_preflight_failed": ("comparison_preflight_failed", 1, 1, 0),
        "after_attempt_closed": ("attempt_closed", 1, 1, 1),
        "after_finalists_bound": ("finalists_bound", 2, 2, 2),
        "after_selection_decided": ("selection_decided", 2, 2, 2),
        "after_round_closed": ("round_closed", 2, 2, 2),
        "after_baseline_refresh_bound": ("baseline_refresh_bound", 2, 2, 2),
        "after_baseline_refresh_completed": ("baseline_refresh_completed", 2, 2, 2),
    }[cut]
    records = _records(run / "campaign.jsonl")
    counts = Counter(r["event"] for r in records)
    assert records[-1]["event"] == last_event
    assert (
        counts["attempt_opened"],
        counts["comparison_preflight_failed"],
        counts["attempt_closed"],
    ) == (opened, failed, closed)
    assert counts["campaign_finished"] == counts["campaign_suspended"] == 0

    if cut in {"after_comparison_data_declared", "after_comparison_data_published"}:
        digest = records[-1]["payload"]["content_digest"]
        published = {p.declaration.content_digest for p in inspect_auxiliary(run, verify=True)}
        assert (digest in published) == (cut == "after_comparison_data_published")

    if cut in {"after_baseline_refresh_bound", "after_baseline_refresh_completed"}:
        completed = cut == "after_baseline_refresh_completed"
        assert counts["round_closed"] == counts["baseline_refresh_bound"] == 1
        assert counts["baseline_refresh_completed"] == int(completed)
        assert records[-1]["payload"]["measurement_id"] == "m-r02-refresh"
        measurement = run / "measurements" / "m-r02-refresh" / "events.jsonl"
        assert measurement.exists() == completed
        if completed:
            assert sum(r["event"] == "scoring_finished" for r in _records(measurement)) == 2


async def run_preflight(
    tmp_path,
    monkeypatch,
    *,
    stable=True,
    local=False,
    refresh=0,
    rounds=1,
    cut=None,
    stop="all_attempts",
    changing_scores=False,
    noise_allowance=0.5,
    fresh=False,
    local_margin=1,
    local_global="off",
    global_point=False,
    **run_options,
):
    from evolution.tree.campaign import _Campaign
    from tests import campaign_drivers as fixtures

    family15_admitted(monkeypatch)
    real = fixtures.evolve

    async def evolve(path, *args, **kwargs):
        doc = family14_document(
            json.loads(path.read_text()), repeats=1, refresh=refresh, round_stop=stop
        )
        doc["schema"] = "evolution-v2-manifest/16"
        doc["campaign"]["gate"] = v16_document()["campaign"]["gate"]
        gate = doc["campaign"]["gate"]
        gate.update(stability="stable" if stable else "unstable", fresh_parent=fresh)
        if changing_scores:
            gate["execution_noise"] = {
                "kind": "bounded",
                "allowance": noise_allowance,
                "source": "declared",
            }
        if local:
            gate["global"] = local_global
            gate["focus"]["margin"] = local_margin
        elif not stable and not global_point:
            gate.update(
                mode="confidence",
                **{"global": "bound"},
                statistics={"method": "bounded-weighted-mean/1", "unit": "case"},
            )
            gate["focus"]["confidence"] = gate["causal"]["confidence"] = 0.95
        path.write_text(json.dumps(doc))
        return await real(path, *args, **kwargs)

    def hook(self, name):
        if name == cut:
            raise Boundary(name)

    def result(self):
        raise Boundary()

    async def forbidden(*args, **kwargs):
        pytest.fail("early global dispatched a role")

    scorer = TWO_CASE_SCORER.replace(
        'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
        "passed = True",
    )
    if changing_scores:
        scorer = scorer.replace(
            "passed = True",
            f"import fcntl\ncounter = pathlib.Path({str(tmp_path / 'score-count')!r})\nwith counter.open('a+') as f:\n    fcntl.flock(f, fcntl.LOCK_EX)\n    f.seek(0)\n    n = int(f.read() or 0)\n    f.seek(0)\n    f.truncate()\n    f.write(str(n + 1))\npassed = True",
        )
        scorer = scorer.replace(
            '"score": 1.0 if passed else 0.0', '"score": 1.0 if n < 3 else 0.75'
        )
    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", evolve)
        patch.setattr(_Campaign, "hook", hook)
        patch.setattr(_Campaign, "result", result)
        if not local:
            for name in ("run_grouping", "run_reflection", "run_editor"):
                patch.setattr(_Campaign, name, forbidden)
        with pytest.raises(Boundary) as boundary:
            await _two_case_run(
                tmp_path,
                terminal=None,
                campaign={"rounds": rounds, "attempts_per_round": 2},
                **({} if local else {"scorer": scorer}),
                **run_options,
            )
    assert boundary.value.args == (() if cut is None else (cut,)), (
        f"requested cut {cut!r} was not reached: {boundary.value.args!r}"
    )
    run = tmp_path / "run"
    if cut is not None:
        _assert_preflight_prefix(run, cut)
    return run


@pytest.mark.parametrize("stable", [True, False])
@pytest.mark.parametrize("stop", ["first_accepted", "all_attempts"])
async def test_early_global_spends_only_attempt_budgets(tmp_path, monkeypatch, stable, stop):
    from evolution.tree.campaign_projections import verify_evidence

    run = await run_preflight(tmp_path, monkeypatch, stable=stable, stop=stop)
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_preflight_failed" for r in records) == 2
    assert [r["payload"]["path"] for r in records if r["event"] == "attempt_opened"] == [
        "early_global"
    ] * 2
    assert all(
        r["payload"]["outcome"] == "retained" for r in records if r["event"] == "round_closed"
    )
    assert records[-1]["event"] == "campaign_finished"
    assert not any(
        r["event"]
        in {
            "grouping_started",
            "reflection_started",
            "editor_started",
            "attempt_sealed",
            "comparison_completed",
        }
        for r in records
    )
    assert not verify_evidence(run)["mismatches"]


async def test_local_skips_measurement_after_actual_scope_freeze(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence

    run = await run_preflight(tmp_path, monkeypatch, local=True)
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_preflight_failed" for r in records) == 2
    assert sum(r["event"] == "affected_cases_selected" for r in records) == 2
    assert not any(r["event"] == "comparison_completed" for r in records)
    for r in records:
        if r["event"] == "measurement_bound" and r["payload"]["attempt_id"] != "champion":
            path = run / "measurements" / r["payload"]["measurement_id"] / "events.jsonl"
            assert not path.exists() or not any(
                e["event"] == "scoring_finished" for e in _records(path)
            )
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize(
    "cut",
    [
        "after_attempt_opened",
        "after_comparison_data_declared",
        "after_comparison_data_published",
        "after_comparison_preflight_failed",
        "after_attempt_closed",
        "after_finalists_bound",
        "after_selection_decided",
        "after_round_closed",
    ],
)
async def test_actual_early_global_recovery_has_no_duplicate_budget_or_publication(
    tmp_path, monkeypatch, cut
):
    from evolution.tree.campaign_projections import verify_evidence

    await run_preflight(tmp_path, monkeypatch, cut=cut)
    run = await run_preflight(tmp_path, monkeypatch)
    records = _records(run / "campaign.jsonl")
    for event in ("attempt_opened", "attempt_closed", "comparison_preflight_failed"):
        assert sum(r["event"] == event for r in records) == 2
    assert sum(r["event"] == "auxiliary_payload_declared" for r in records) == 2
    assert sum(r["event"] == "round_closed" for r in records) == 1
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("stable", [True, False])
async def test_declared_refreshes_keep_actual_paid_work_and_recompute_reference(
    tmp_path, monkeypatch, stable
):
    from evolution.tree.campaign_projections import verify_evidence

    run = await run_preflight(tmp_path, monkeypatch, stable=stable, refresh=1, rounds=3)
    records = _records(run / "campaign.jsonl")
    refreshes = [r["payload"] for r in records if r["event"] == "baseline_refresh_completed"]
    assert len(refreshes) == 2
    assert sum(r["event"] == "comparison_preflight_failed" for r in records) == 6
    ids = [r["payload"]["measurement_id"] for r in records if r["event"] == "measurement_bound"]
    assert len(ids) == 3
    assert (
        sum(
            sum(
                e["event"] == "scoring_finished"
                for e in _records(run / "measurements" / mid / "events.jsonl")
            )
            for mid in ids
        )
        == 6
    )
    assert not verify_evidence(run)["mismatches"]


async def test_inspection_planner_resumes_before_roles(tmp_path, monkeypatch):
    from evolution.tree.campaign_policy import policy_class_for_payload
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.campaign_view import OpenAttempt, next_actions

    run = await run_preflight(tmp_path, monkeypatch, cut="after_baseline_complete")
    view = inspect_view(run)
    policy = policy_class_for_payload(view.snapshot.identity["policy"]).from_payload(
        view.snapshot.identity["policy"]
    )
    actions = next_actions(view, policy)
    assert actions and all(isinstance(a, OpenAttempt) and a.path == "early_global" for a in actions)


@pytest.mark.parametrize("stable", [True, False])
@pytest.mark.parametrize("citation", ["malformed", "creation_only"])
async def test_inspection_preserves_facts_with_unreadable_baseline_citation(
    tmp_path, monkeypatch, stable, citation
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection, inspect_view

    run = await run_preflight(tmp_path, monkeypatch, stable=stable, cut="after_baseline_complete")
    campaign = run / "campaign.jsonl"
    records = _records(campaign)
    complete = next(r for r in records if r["event"] == "baseline_complete")
    mid = complete["payload"]["measurement_id"]
    reference = complete["payload"]["evidence"][0]
    if citation == "malformed":
        reference["locator"] = reference["locator"].replace("#bytes=0:", "#bytes=1:")
    else:
        name = reference["locator"].split("#", 1)[0]
        creation = (run / name).read_bytes().splitlines(keepends=True)[0]
        reference["locator"] = f"{name}#bytes=0:{len(creation)}"
    campaign.write_text("".join(json.dumps(r) + "\n" for r in records))
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}

    projection = inspect_campaign(run, verify=True)
    status = projection.to_payload()

    assert inspect_view(run).snapshot.rounds[1].baseline_complete
    assert status["siblings"][mid] == "unreadable"
    assert status["actions"] == []
    assert next(m for m in status["measurements"] if m["measurement_id"] == mid)["status"] is None
    assert any(
        m["event"] == "baseline_complete"
        and m["locator"] == reference["locator"]
        and m["code"] == "evidence_mismatch"
        for m in status["verification"]["mismatches"]
    )
    assert CampaignProjection.from_bytes(projection.encode()) == projection
    assert {p: p.read_bytes() for p in run.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("stable", [True, False])
async def test_changed_refresh_values_rebind_only_stored_ceiling(tmp_path, monkeypatch, stable):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.family15_audit import authenticate_auxiliary
    from evolution.tree.family15_schema import decode_rational

    run = await run_preflight(
        tmp_path, monkeypatch, stable=stable, changing_scores=True, refresh=1, rounds=2
    )
    preflights = [
        r["payload"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] == "comparison_preflight_failed"
    ]
    bodies = [
        authenticate_auxiliary(run, AuxiliaryPublication.from_payload(p["data"]))
        for p in preflights
    ]
    assert bodies[0]["reference"]["measurement_id"] != bodies[-1]["reference"]["measurement_id"]
    assert [decode_rational(c["value"]) for c in bodies[0]["results"][0]["ceilings"]] == (
        [0, 0] if stable else [1, 1]
    )
    assert [decode_rational(c["value"]) for c in bodies[-1]["results"][0]["ceilings"]] == (
        [0.25, 0.25] if stable else [1, 1]
    )


@pytest.mark.parametrize(
    "cut",
    ["after_comparison_data_declared", "after_comparison_preflight_failed", "after_attempt_closed"],
)
async def test_local_preflight_recovery_does_not_dispatch_fresh_parent(tmp_path, monkeypatch, cut):
    await run_preflight(tmp_path, monkeypatch, local=True, fresh=True, cut=cut)
    run = await run_preflight(tmp_path, monkeypatch, local=True, fresh=True)
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_preflight_failed" for r in records) == 2
    assert not any(r["event"] == "attempt_stage_completed" for r in records)
    for path in (run / "measurements").glob("*-a*/events.jsonl"):
        assert not any(r["event"] == "scoring_finished" for r in _records(path))


async def test_actual_published_false_maximum_fails_evidence_audit(tmp_path, monkeypatch):
    from fractions import Fraction

    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_schema import encode_rational

    original = _Campaign.publish_comparison_data

    def publish(self, body):
        body = json.loads(json.dumps(body))
        if body["kind"] == "preflight":
            body["results"][0].update(
                e_max={"kind": "finite", "value": encode_rational(Fraction(1))}, c_max=None
            )
        return original(self, body)

    monkeypatch.setattr(_Campaign, "publish_comparison_data", publish)
    run = await run_preflight(tmp_path, monkeypatch, stable=False)
    audit = verify_evidence(run)
    assert audit["mismatches"]
    assert any("preflight exact" in str(m) for m in audit["mismatches"])


async def test_safe_pre_spend_explanation_keeps_refresh_scenario_and_privacy(tmp_path, monkeypatch):
    from evolution.tree.campaign_policy import policy_class_for_payload
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.family15_report import explain_preflight, remaining_refresh_cost

    run = await run_preflight(
        tmp_path, monkeypatch, rounds=5, refresh=2, cut="after_baseline_complete"
    )
    view = inspect_view(run)
    policy = policy_class_for_payload(view.snapshot.identity["policy"]).from_payload(
        view.snapshot.identity["policy"]
    )
    explanation = explain_preflight(view.global_preflights[1])
    assert explanation["status"] == "impossible"
    assert explanation["basis"] == "stored_reference"
    assert "case_id" not in json.dumps(explanation) and "reference_values" not in json.dumps(
        explanation
    )
    cost = remaining_refresh_cost(view.snapshot, policy, 1)
    assert cost["remaining_refreshes"] == 2
    assert cost["scenario_slots"] == 4
    assert cost["connection_preflight_credit"] == "separate"


async def test_no_eligible_round_also_closes_after_measured_nonpass(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence

    scorer = TWO_CASE_SCORER.replace(
        '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
    )
    run = await run_preflight(tmp_path, monkeypatch, local=True, local_margin=0.5, scorer=scorer)
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_completed" for r in records) == 2
    assert not any(r["event"] == "comparison_preflight_failed" for r in records)
    assert records[-1]["event"] == "campaign_finished"
    assert not verify_evidence(run)["mismatches"]


async def test_unstable_global_point_does_not_tighten_from_stored_ones(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.family15_report import explain_preflight

    run = await run_preflight(
        tmp_path, monkeypatch, stable=False, global_point=True, cut="after_baseline_complete"
    )
    explanation = explain_preflight(inspect_view(run).global_preflights[1])
    assert explanation["status"] == "not_ruled_out"
    assert explanation["basis"] == "unknown_fresh_reference_envelope"


async def test_unstable_global_and_stored_local_have_distinct_reference_knowledge(
    tmp_path, monkeypatch
):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary

    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.9')
    run = await run_preflight(
        tmp_path,
        monkeypatch,
        stable=False,
        local=True,
        local_global="point",
        local_margin=0.2,
        scorer=scorer,
    )
    records = _records(run / "campaign.jsonl")
    assert [r["payload"]["path"] for r in records if r["event"] == "attempt_opened"] == [
        "candidate"
    ] * 2
    results = [r["payload"] for r in records if r["event"] == "comparison_preflight_failed"]
    assert len(results) == 2
    assert all(
        authenticate_auxiliary(run, AuxiliaryPublication.from_payload(p["data"]))["reference_kind"]
        == "stored"
        for p in results
    )
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize(
    "cut", ["after_baseline_refresh_bound", "after_baseline_refresh_completed"]
)
async def test_refresh_recovery_preserves_measurement_credit(tmp_path, monkeypatch, cut):
    from evolution.tree.campaign_projections import verify_evidence

    await run_preflight(tmp_path, monkeypatch, refresh=1, rounds=2, cut=cut)
    run = await run_preflight(tmp_path, monkeypatch, refresh=1, rounds=2)
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "baseline_refresh_bound" for r in records) == 1
    assert sum(r["event"] == "baseline_refresh_completed" for r in records) == 1
    assert sum(r["event"] == "comparison_preflight_failed" for r in records) == 4
    events = _records(run / "measurements" / "m-r02-refresh" / "events.jsonl")
    assert sum(r["event"] == "scoring_finished" for r in events) == 2
    assert not verify_evidence(run)["mismatches"]


async def test_early_global_continue_recomputes_policy_epoch_binding(tmp_path, monkeypatch):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.family15_audit import authenticate_auxiliary
    from tests.campaign_drivers import _continue, _Resettable

    adapter = _Resettable()
    await run_preflight(tmp_path, monkeypatch, adapter=adapter, cut="after_attempt_opened")
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await run_preflight(tmp_path, monkeypatch, adapter=adapter)
    run = await run_preflight(
        tmp_path, monkeypatch, adapter=adapter, authorizations=[_continue(refused.value)]
    )
    records = _records(run / "campaign.jsonl")
    failures = [r["payload"] for r in records if r["event"] == "comparison_preflight_failed"]
    assert len(failures) == 2
    assert all(p["epoch"] == 1 for p in failures)
    assert all(
        authenticate_auxiliary(run, AuxiliaryPublication.from_payload(p["data"]))["epoch"] == 1
        for p in failures
    )
    assert not verify_evidence(run)["mismatches"]


async def test_refresh_that_reopens_ceiling_returns_to_roles(tmp_path, monkeypatch):
    from evolution.tree.campaign_policy import policy_class_for_payload
    from evolution.tree.campaign_projections import inspect_view, verify_evidence
    from evolution.tree.campaign_view import RunGrouping, next_actions

    run = await run_preflight(
        tmp_path,
        monkeypatch,
        changing_scores=True,
        noise_allowance=0,
        refresh=1,
        rounds=2,
        cut="after_baseline_refresh_completed",
    )
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_preflight_failed" for r in records) == 2
    view = inspect_view(run)
    policy = policy_class_for_payload(view.snapshot.identity["policy"]).from_payload(
        view.snapshot.identity["policy"]
    )
    assert not view.global_preflights[2].impossible
    assert next_actions(view, policy) == (RunGrouping(2),)
    assert not verify_evidence(run)["mismatches"]
