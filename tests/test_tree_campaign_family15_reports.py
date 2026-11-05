"""Family15 shares exact safe explanations across public readers."""

import json
from fractions import Fraction

import pytest

from evolution.tree.case_comparison import evaluate_comparison
from tests.test_tree_case_comparison import built, row

pytestmark = pytest.mark.slow


def report_for(policy):
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.family15_report import campaign15_projection
    from evolution.tree.storage.envelope import Envelope
    from tests.family15_fakes import family15_created

    created = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=1,
            timestamp="2026-10-19T00:00:00Z",
            event="campaign_created",
            operation_id=None,
            payload=family15_created(policy),
        ),
        family=campaign_family_for(15),
    )
    return campaign15_projection(reduce((created,), selector=None, family_version=15))


@pytest.mark.parametrize(
    "health,mediator,u,t,want",
    [
        (
            "unavailable",
            "started",
            4,
            1,
            "round 3: probe unavailable at health check; 4 calls unreachable, 1 transport failure",
        ),
        (
            "unavailable",
            "started",
            1,
            0,
            "round 3: probe unavailable at health check; 1 call unreachable",
        ),
        ("unavailable", "started", 0, 0, "round 3: probe unavailable at health check"),
        ("ready", "failed", 0, 0, "round 3: probe mediator failed to start; no probe calls"),
        (
            "unavailable",
            "failed",
            1,
            0,
            "round 3: probe mediator failed to start; 1 call unreachable",
        ),
        ("ready", "failed", 3, 0, "round 3: probe mediator failed to start; 3 calls unreachable"),
        (
            "ready",
            "started",
            1,
            2,
            "round 3: probe calls failed: 1 unreachable, 2 transport failures",
        ),
        ("ready", "started", 0, 1, "round 3: probe calls failed: 1 transport failure"),
        ("ready", "started", 0, 0, None),
    ],
)
def test_report_probe_degraded_lines(health, mediator, u, t, want):
    from evolution.tree.family15_report import report_lines, validate_campaign15_projection

    profile, _ = built(mode="point", repeats=1)
    report = report_for(profile.policy)
    report["probe_availability"] = [
        {
            "round": 3,
            "health": health,
            "mediator": mediator,
            "unreachable": u,
            "transport_failures": t,
        }
    ]
    validate_campaign15_projection(report)
    lines = [line for line in report_lines(report) if line.startswith("round 3: probe")]
    assert lines == ([] if want is None else [want])


def projected_report(profile, candidate, reference):
    from evolution.tree.family15_report import comparison_projection

    result = evaluate_comparison(profile, candidate, reference, campaign_seed=123)
    comparison = comparison_projection(result, profile=profile.to_payload(), policy=profile.policy)
    report = report_for(profile.policy)
    report["attempts"] = [dict(attempt_id="a", comparison=comparison)]
    return report, comparison


@pytest.mark.parametrize("fresh", [False, True])
def test_fallback_report_names_the_substituted_rule_only_where_present(fresh):
    from evolution.tree.family15_report import report_lines, validate_campaign15_projection

    p, refs = built(
        mode="confidence",
        repeats=2,
        fresh=fresh,
        fallback=True,
        confidence=(0.95, 0.95),
    )
    report, comparison = projected_report(p, [row(1, repeat=i) for i in (1, 2)], refs)
    assert comparison["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}
    assert "fallback" not in comparison["scopes"][1]
    # A historical comparison remains self-describing after policy authorization.
    report["settings"].pop("bound_fallback")
    report["attempts"][0].update(
        round=1,
        outcome="rejected",
        reason="threshold_not_passed",
        gate_comparison=comparison,
        global_comparison=None,
        preflight=None,
        conditional_cost=None,
        eligible_final_score=None,
    )
    validate_campaign15_projection(report)
    text = "\n".join(report_lines(report))
    assert "focus · point (fallback: focus_unattainable) · point 1 (approx)" in text
    assert "causal · point (fallback:" not in text


@pytest.mark.parametrize("exact", [False, True])
def test_statistical_report_shows_bootstrap_lower_exactness_and_empirical_caveat(exact):
    from evolution.tree.family15_report import report_lines

    p, refs = built(mode="statistical", repeats=2)
    candidate = [row(1 if exact else i / 4, repeat=i) for i in (1, 2)]
    report, comparison = projected_report(p, candidate, refs)
    scope = comparison["scopes"][0]
    assert report["settings"]["mode"] == "statistical"
    assert report["settings"]["method"] == comparison["method"] == "f13-bootstrap/1"
    assert scope["exact"] is exact and scope["lower"] is not None
    assert scope["pairing"] == "exact"
    recorded = evaluate_comparison(p, candidate, refs, campaign_seed=123).to_payload()["scopes"][0]
    for field in ("point", "lower", "exact", "pairing"):
        assert scope[field] == recorded[field]
    assert (scope["lower"] == scope["point"]) is exact
    text = "\n".join(report_lines(report))
    assert "independent · statistical" in text
    assert "focus · bootstrap · point" in text
    assert "repeat-bootstrap confidence 0.1 (approx) · lower" in text
    assert f"exact contrast: {'yes' if exact else 'no'}" in text
    assert "intervals_empirical · empirical repeat intervals, not guaranteed coverage" in text
    assert ("exact_contrast · decided on the point" in text) is exact


def test_unavailable_bootstrap_report_does_not_present_a_lower_or_exactness_claim():
    from evolution.tree.family15_report import report_lines

    p, refs = built(mode="statistical", repeats=2, exclusions=(("c", 2),))
    report, comparison = projected_report(p, [row(1, repeat=i) for i in (1, 2)], refs)
    assert comparison["reason"] == "insufficient_repeats"
    assert comparison["scopes"][0]["lower"] is None
    assert comparison["scopes"][0]["exact"] is None
    text = "\n".join(report_lines(report))
    assert "lower unavailable · exact contrast: unavailable" in text
    assert "intervals_empirical · empirical repeat intervals, not guaranteed coverage" in text


@pytest.mark.parametrize("mode", ["point", "statistical", "confidence"])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("allowance", [None, 0.125])
def test_measured_noise_report_uses_actual_threshold_and_parent_source(mode, fresh, allowance):
    from evolution.tree.family15_report import readable_rational, report_lines
    from evolution.tree.family15_schema import decode_rational, encode_rational

    p, refs = built(
        mode=mode,
        repeats=2,
        fresh=fresh,
        allowance=allowance,
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        parent_values={"a": [0, 0], "b": [0, 1]},
        tolerance="measured_noise",
        fallback=mode == "confidence",
    )
    candidate = [row(1, case=r.case_id, repeat=r.repeat) for r in refs]
    report, comparison = projected_report(p, candidate, refs)
    calibration = comparison["calibration"]
    q = decode_rational(calibration["allowance"])
    threshold = encode_rational(Fraction(allowance or 0) - q)
    assert q > 0 and comparison["scopes"][1]["threshold"] == threshold
    assert calibration["source"] == ("fresh" if fresh else "stored")
    assert calibration == (
        evaluate_comparison(p, candidate, refs, campaign_seed=123).to_payload()["calibration"]
        if fresh
        else p.to_payload()["calibration"]
    )
    text = "\n".join(report_lines(report))
    expression = "e - q" if allowance else "-q"
    assert f"threshold {readable_rational(threshold)} ({expression})" in text
    assert (
        f"calibration · {'fresh' if fresh else 'stored'} Parent · q {readable_rational(calibration['allowance'])}"
        in text
    )
    assert "noise_allowance_empirical · empirical Parent repeat calibration" in text


@pytest.mark.parametrize("fresh", [False, True])
def test_unknown_q_report_does_not_invent_calibration_or_a_numeric_threshold(fresh):
    from evolution.tree.case_comparison import ComparisonProfile
    from evolution.tree.family15_report import report_lines

    p, refs = built(
        mode="confidence",
        repeats=2,
        fresh=fresh,
        fallback=True,
        confidence=(0.95, 0.95),
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
        exclusions=() if fresh else (("b", 2),),
    )
    if fresh:
        body = p.to_payload()
        body["scopes"][1]["cases"][0]["repeats"] = [1]
        p = ComparisonProfile.from_payload(
            body, primary="score", groups=dict(p.groups), policy=p.policy
        )
    candidate = [row(1, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)]
    report, comparison = projected_report(p, candidate, refs)
    assert comparison["reason"] == "insufficient_calibration"
    assert "calibration" not in comparison
    assert comparison["scopes"][1]["threshold"] is None
    assert "fallback" not in comparison["scopes"][1]
    text = "\n".join(report_lines(report))
    assert "causal · Parent calibration unavailable" in text
    assert "threshold unavailable" in text
    assert "q = 0" not in text and "Parent · q 0" not in text


def test_preflight_report_labels_fallback_without_changing_ordinary_scope_text():
    from evolution.tree.case_comparison import preflight
    from evolution.tree.family15_report import explain_preflight, preflight_lines

    for fallback in (False, True):
        p, refs = built(mode="confidence", confidence=(0.95, 0.95), fallback=fallback)
        check = explain_preflight(preflight(p, refs), policy=p.policy)
        text = "\n".join(preflight_lines(check, label="local"))
        assert ("focus · point (fallback: focus_unattainable) · threshold" in text) is fallback
        assert ("bound_fallback" in check) is fallback
        assert ("fallback" in check["scopes"][0]) is fallback


def test_old_key_report_and_projection_bytes_stay_pinned():
    from hashlib import sha256

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_report import report_lines

    p, refs = built()
    report, comparison = projected_report(p, [row(1)], refs)
    assert not {"bound_fallback", "calibration", "caveats"} & comparison.keys()
    assert all("fallback" not in s for s in comparison["scopes"])
    assert "bound_fallback" not in report["settings"]
    assert sha256(canonical_json(report)).hexdigest() == (
        "ea4886a19a0393b92ae4a2727e5c968b5dd0822a6b4a99efcb336805910228a2"
    )
    assert sha256("\n".join(report_lines(report)).encode()).hexdigest() == (
        "f18d03b91522669b7e67a7ccf65430e4ca4612ed8b9716d868fb9f350c324a1d"
    )


@pytest.mark.parametrize("fresh", [False, True])
def test_zero_q_is_observed_no_noise_not_missing_calibration(fresh):
    from evolution.tree.family15_report import report_lines

    p, refs = built(
        repeats=2,
        fresh=fresh,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    report, _ = projected_report(p, [row(1, case=r.case_id, repeat=r.repeat) for r in refs], refs)
    text = "\n".join(report_lines(report))
    assert f"calibration · {'fresh' if fresh else 'stored'} Parent · q 0 (approx)" in text
    assert "noise_not_observed · no Parent repeat noise observed; q = 0" in text
    assert "calibration unavailable" not in text


def test_fresh_calibration_without_profile_keeps_execution_allowance_unknown():
    from evolution.tree.family15_report import comparison_projection, report_lines

    p, refs = built(
        repeats=2,
        fresh=True,
        allowance=0.125,
        tolerance="measured_noise",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    result = evaluate_comparison(
        p, [row(1, case=r.case_id, repeat=r.repeat) for r in refs], refs, campaign_seed=123
    )
    comparison = comparison_projection(result)
    assert comparison["noise"]["allowance"] is None
    report = report_for(p.policy)
    report["attempts"] = [dict(attempt_id="a", comparison=comparison)]
    text = "\n".join(report_lines(report))
    assert "threshold 0.125 (approx) (e - q)" in text
    assert "noise · None · allowance unavailable" in text


async def test_report_keeps_gate_fallback_visible_after_global_and_policy_authorization(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evolve import EvolveRefused, evolve
    from evolution.tree.execution.authorizations import Authorization
    from evolution.tree.family15_report import report_lines
    from tests.campaign_drivers import EDITED, _Resettable
    from tests.test_tree_campaign_family15_gate import gate_run

    adapter = _Resettable(**EDITED)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        adapter=adapter,
        mode="confidence",
        repeats=2,
        fresh=True,
        campaign_options={"rounds": 2},
        stop="after_round_closed",
        hard=True,
        gate_options={
            "bound_fallback": "point",
            "focus": {"margin": 0, "confidence": 0.9},
            "causal": {"tolerance": "measured_noise", "confidence": 0.9},
        },
    )
    path = tmp_path / "manifest.json"
    document = json.loads(path.read_text())
    document["campaign"]["gate"].pop("bound_fallback")
    path.write_text(json.dumps(document))
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *a: adapter)
    authorization = Authorization.new(
        "run_identity_changed",
        refused.value.instance_digest,
        "Remove fallback next epoch",
        now="2026-10-22T10:00:00Z",
        effect="rebaseline",
    )
    await evolve(path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization])
    status = inspect_campaign(run, verify=True).to_payload()
    assert status["verification"]["mismatches"] == []
    report = status["independent"]
    assert "bound_fallback" not in report["settings"]
    attempt = report["attempts"][0]
    assert attempt["comparison"] == attempt["global_comparison"]
    assert attempt["gate_comparison"]["bound_fallback"] == "point"
    text = "\n".join(report_lines(report))
    assert f"{attempt['attempt_id']} · gate · passed" in text
    assert "focus · point (fallback: focus_unattainable)" in text
    assert "causal · point (fallback: causal_unconfirmable)" in text
    assert "calibration · fresh Parent · q 0 (approx)" in text


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_a_finalist_carries_both_layers_and_comparison_is_the_global_one(
    tmp_path, monkeypatch, verification
):
    import importlib

    from evolution.tree.campaign import inspect_campaign
    from tests.campaign_drivers import _records
    from tests.family15_fakes import independent_run
    from tests.test_tree_campaign_family15_selection import data

    module = importlib.import_module("evolution.tree.evolve")
    original = module.evolve

    async def configured(path, *args, **kwargs):
        document = json.loads(path.read_text())
        document["campaign"]["gate"]["verification"] = verification
        path.write_text(json.dumps(document))
        return await original(path, *args, **kwargs)

    monkeypatch.setattr(module, "evolve", configured)
    run, finished = await independent_run(tmp_path, monkeypatch)
    report = inspect_campaign(run, verify=True).to_payload()["independent"]
    assert report == finished.result.to_payload()["independent"]
    events = _records(run / "campaign.jsonl")
    attempts = {a["attempt_id"]: a for a in report["attempts"]}
    finals = [r["payload"] for r in events if r["event"] == "global_comparison_completed"]
    assert finals
    for final in finals:
        attempt = attempts[final["attempt_id"]]
        gate = next(
            r["payload"]
            for r in events
            if r["event"] == "comparison_completed"
            and r["payload"]["attempt_id"] == final["attempt_id"]
        )
        assert attempt["gate_comparison"]["verification"] is None
        assert attempt["global_comparison"]["verification"] == (
            "reused_evidence" if verification == "reuse" else "fresh_global_evidence"
        )
        assert attempt["comparison"] == attempt["global_comparison"]
        for name, payload in (("gate_comparison", gate), ("global_comparison", final)):
            assert (
                attempt[name]["reference_measurement_id"]
                == data(run, payload)["reference"]["measurement_id"]
            )
        assert {s["name"] for s in attempt["gate_comparison"]["scopes"]} == {"focus", "causal"}
        assert {s["name"] for s in attempt["global_comparison"]["scopes"]} == {"global"}


async def test_a_gate_rejected_attempt_has_only_the_gate_layer(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family15_gate import gate_run

    scorer = TWO_CASE_SCORER.replace(
        '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
    )
    run = await gate_run(tmp_path, monkeypatch, passing=False, scorer=scorer)
    attempt = inspect_campaign(run, verify=True).to_payload()["independent"]["attempts"][0]
    assert attempt["outcome"] == "rejected"
    assert attempt["gate_comparison"]["outcome"] == "comparison_not_passed"
    assert attempt["gate_comparison"]["verification"] is None
    assert attempt["global_comparison"] is None
    assert attempt["comparison"] == attempt["gate_comparison"]


async def test_an_attempt_without_a_comparison_has_neither_layer(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from tests.test_tree_campaign_family15_preflight_recovery import run_preflight

    run = await run_preflight(tmp_path, monkeypatch)
    attempts = inspect_campaign(run, verify=True).to_payload()["independent"]["attempts"]
    assert attempts
    for attempt in attempts:
        assert attempt["preflight"] is not None
        assert attempt["gate_comparison"] is None
        assert attempt["global_comparison"] is None
        assert attempt["comparison"] is None


async def test_a_b8_shaped_projection_is_refused(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection
    from evolution.tree.storage.envelope import SchemaError
    from tests.family15_fakes import independent_run

    run, _ = await independent_run(tmp_path, monkeypatch)
    payload = inspect_campaign(run).to_payload()
    for attempt in payload["independent"]["attempts"]:
        assert {"gate_comparison", "global_comparison"} <= attempt.keys()
        del attempt["gate_comparison"], attempt["global_comparison"]
        if attempt["comparison"] is not None:
            del attempt["comparison"]["reference_measurement_id"]
    with pytest.raises(SchemaError):
        CampaignProjection.from_bytes(json.dumps(payload).encode())


def test_comparison_explains_single_allowance_and_complete_coverage():
    from evolution.tree.family15_report import comparison_projection
    from evolution.tree.family15_schema import encode_rational as q

    profile, reference = built(allowance=0.125)
    result = evaluate_comparison(profile, [row(0.75)], reference)
    report = comparison_projection(result, profile=profile.to_payload(), verification="reuse")
    assert report["deciding_layer"] == "focus"
    assert report["scopes"][0]["point"] == q(Fraction(3, 4))
    assert report["scopes"][0]["threshold"] == q(Fraction(1, 8))
    assert report["noise"]["allowance"] == q(Fraction(1, 8))
    assert report["noise"]["source"] == "declared reserve"
    assert report["noise"]["source_kind"] == "operator_supplied"
    assert report["reference"] == "stored_non_contemporaneous"
    assert report["verification"] == "reused_evidence"
    assert report["selection_bias_possible"] == [
        "focus_selection",
        "stored_reference",
        "reuse",
        "candidate_selection",
    ]
    assert "case_id" not in json.dumps(report)


def test_confidence_endpoint_and_incomplete_candidate_have_distinct_claims():
    from evolution.tree.family15_report import comparison_projection

    profile, reference = built(mode="confidence", groups={f"c{i}": "g" for i in range(5)})
    rows = [row(1, case=f"c{i}") for i in range(5)]
    complete = comparison_projection(
        evaluate_comparison(profile, rows, reference), profile=profile.to_payload()
    )
    assert complete["method"] == "bounded-weighted-mean/1"
    assert complete["unit"] == "case"
    assert complete["scopes"][0]["unit_count"] == 5
    assert complete["scopes"][0]["enclosure"] is not None
    assert complete["confidence_scope"] == "conditional_fixed_scope_independent_bounded_units"
    incomplete = comparison_projection(
        evaluate_comparison(profile, rows[:-1], reference), profile=profile.to_payload()
    )
    assert incomplete["outcome"] == "comparison_unavailable"
    assert incomplete["eligible"] is False
    assert all(s["point"] is None for s in incomplete["scopes"])


@pytest.mark.parametrize("repeats,state", [(1, "unavailable"), (3, "observed")])
def test_repeat_variation_is_only_candidate_score_diagnostic(repeats, state):
    from evolution.tree.family15_report import comparison_projection
    from tests.test_tree_case_comparison import profile

    p = profile(noise="observed", repeats=repeats)
    report = comparison_projection(
        evaluate_comparison(
            p,
            [row(i / 4, repeat=i) for i in range(1, repeats + 1)],
            [row(0.5, repeat=i) for i in range(1, repeats + 1)],
        ),
        profile=p.to_payload(),
    )
    assert report["noise"]["variation"] == state
    assert report["noise"]["diagnostic"] == "candidate_primary_score_repeat_variance"
    assert report["noise"]["units"] == "score_squared"
    assert report["noise"]["extra_measurements"] == 0


async def test_public_status_result_and_watch_agree_on_an_unfinished_run(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection
    from evolution.tree.evolve import evolve
    from evolution.tree.family15_report import campaign15_projection
    from evolution.tree.run_reader import open_run
    from evolution.tree.run_watch import WatchSession, follow
    from tests.campaign_drivers import _Resettable
    from tests.test_tree_campaign_family15_condition_change import _interrupted

    adapter = _Resettable()
    run = await _interrupted(tmp_path, monkeypatch, adapter, "after_round_closed")
    status = inspect_campaign(run, verify=True).to_payload()
    report = status["independent"]
    assert not {"boundary", "manual"} & set(report)
    assert "promotion" not in report["settings"]
    assert report["next_action"] == "continue_evolve"
    assert report == campaign15_projection(open_run(run).snapshot, run_root=run)
    assert (
        CampaignProjection.from_bytes(inspect_campaign(run).encode()).to_payload()["independent"]
        == report
    )
    emitted = []
    assert follow(run, emit=emitted.append, once=True) == 0
    # One full batch spanning the rest of the run and its terminal must return terminal 3.
    batch_session = WatchSession(run)
    session = WatchSession(run)
    try:
        stopped = await evolve(
            tmp_path / "manifest.json",
            run,
            cancelled=lambda: True,
            adapter_factory=lambda *a: adapter,
        )
        assert stopped.finished["outcome"] == "stopped"
        assert session.poll()[1] == 3
        assert batch_session.poll()[1] == 3
        assert follow(run, emit=emitted.append) == 3
        assert (
            stopped.status.to_payload()["independent"] == stopped.result.to_payload()["independent"]
        )
    finally:
        batch_session.close()
        session.close()


async def test_public_preflight_is_visible_before_actual_role_and_candidate_entry(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family15_completion import completion_run

    notes = []
    entered = []
    original_evolve = fixtures.evolve
    original_group = _Campaign.run_grouping
    original_stage = _Campaign.attempt_stage

    async def evolve(*args, **kwargs):
        kwargs["notes"] = notes.append
        return await original_evolve(*args, **kwargs)

    async def grouping(self, number):
        assert any("global preflight" in n for n in notes)
        public = inspect_campaign(self.run_root).to_payload()["independent"]
        assert public["preflight"][0]["explanation"]["status"] == "not_ruled_out"
        entered.append("grouping")
        return await original_group(self, number)

    async def stage(self, attempt_id, number):
        assert any("local preflight" in n for n in notes)
        report = inspect_campaign(self.run_root).to_payload()["independent"]
        assert report["attempts"][0]["preflight"]["status"] == "not_ruled_out"
        entered.append("candidate")
        return await original_stage(self, attempt_id, number)

    monkeypatch.setattr(fixtures, "evolve", evolve)
    monkeypatch.setattr(_Campaign, "run_grouping", grouping)
    monkeypatch.setattr(_Campaign, "attempt_stage", stage)
    run = await completion_run(tmp_path, monkeypatch, stop="result")
    assert entered == ["grouping", "candidate"]
    assert (
        inspect_campaign(run).to_payload()["independent"]["evidence"]
        == "authenticated_publications"
    )


async def test_broken_prespent_observer_cannot_change_campaign_outcome(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family15_completion import completion_run

    original = fixtures.evolve
    calls = []

    def broken(line):
        calls.append(line)
        if line.startswith(
            (
                "independent",
                "global preflight",
                "global ·",
                "local preflight",
                "focus ·",
                "causal ·",
                "conditional",
            )
        ):
            raise RuntimeError("private observer failure")

    async def evolve(*args, **kwargs):
        kwargs["notes"] = broken
        return await original(*args, **kwargs)

    monkeypatch.setattr(fixtures, "evolve", evolve)
    run = await completion_run(tmp_path, monkeypatch, stop="result")
    assert calls
    assert inspect_campaign(run).to_payload()["campaign_outcome"]["outcome"] == "completed"


async def test_unreadable_sibling_keeps_closed_report_and_no_actions(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection
    from tests.test_tree_campaign_family15_preflight_recovery import run_preflight

    run = await run_preflight(tmp_path, monkeypatch, cut="after_baseline_complete")
    journal = run / "measurements" / "m-r00-champion" / "events.jsonl"
    original = journal.read_bytes()
    journal.unlink()
    target = tmp_path / "unsafe"
    target.write_bytes(original)
    journal.symlink_to(target)
    projection = inspect_campaign(run, verify=True)
    status = projection.to_payload()
    assert status["actions"] == []
    assert status["independent"]["evidence"] == "unavailable"
    assert status["independent"]["committed_work"] is None
    assert CampaignProjection.from_bytes(projection.encode()).to_payload() == status


async def test_cli_result_prints_the_same_shared_facts(tmp_path, monkeypatch):
    import asyncio
    import importlib
    from contextlib import nullcontext

    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app
    from tests.family15_fakes import independent_run

    run, finished = await independent_run(tmp_path, monkeypatch)
    module = importlib.import_module("evolution.tree.evolve")

    async def frozen(*args, **kwargs):
        return finished

    monkeypatch.setattr(module, "evolve", frozen)
    cli_module = importlib.import_module("evolution.tree.cli")
    monkeypatch.setattr(cli_module, "_stop_flag", lambda: nullcontext(lambda: False))
    output = await asyncio.to_thread(
        CliRunner().invoke,
        tree_app,
        ["evolve", "--manifest", str(tmp_path / "manifest.json"), "--run-dir", str(run)],
    )
    assert output.exit_code == 0, output.output
    assert "independent · point" in output.stderr
    assert "decide" not in output.stderr and "approv" not in output.stderr
    assert (
        json.loads(output.stdout)["status"]["independent"]
        == finished.status.to_payload()["independent"]
    )


def test_human_rational_rendering_is_bounded_even_for_huge_exact_inputs():
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.case_comparison import ComparisonResult
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_report import (
        campaign15_projection,
        comparison_projection,
        report_lines,
    )
    from evolution.tree.family15_schema import encode_rational
    from evolution.tree.storage.envelope import Envelope
    from tests.family15_fakes import family15_created, v16_document
    from tests.test_tree_campaign_family15_schema import result_data

    policy = CampaignPolicyV12.from_payload(v16_document()["campaign"])
    created = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=1,
            timestamp="2026-10-19T00:00:00Z",
            event="campaign_created",
            operation_id=None,
            payload=family15_created(policy),
        ),
        family=campaign_family_for(15),
    )
    snapshot = reduce((created,), selector=None, family_version=15)
    report = campaign15_projection(snapshot)
    body = result_data()
    body["scopes"][0]["point"] = encode_rational(Fraction(2**20000 - 1, 2**20000))
    comparison = comparison_projection(ComparisonResult(canonical_json(body)))
    report["attempts"] = [dict(attempt_id="a", comparison=comparison)]
    lines = report_lines(report)
    assert all(len(line) < 350 for line in lines)
    assert any("approx" in line for line in lines)


def test_bound_preflight_human_line_names_confidence_ceiling_and_request():
    from evolution.tree.case_comparison import preflight
    from evolution.tree.family15_report import explain_preflight, preflight_lines

    p, reference = built(mode="confidence", groups={f"c{i}": "g" for i in range(5)})
    check = explain_preflight(preflight(p, reference))
    text = "\n".join(preflight_lines(check, label="local"))
    assert "maximal confidence" in text
    assert "requested confidence" in text
    assert "0.1 (approx)" in text


def test_derived_threshold_outside_binary64_display_range_is_bounded():
    from evolution.tree.family15_report import comparison_projection, readable_rational
    from evolution.tree.family15_schema import encode_rational
    from tests.test_tree_case_comparison import profile

    # Both operands are admitted finite binary64 values; their exact sum is larger.
    threshold = Fraction(1e308) + Fraction(1e308)
    assert "outside display range" in readable_rational(encode_rational(threshold))
    p = profile(threshold=threshold)
    report = comparison_projection(
        evaluate_comparison(p, [row(1)], [row(0.5)]), profile=p.to_payload()
    )
    assert report["outcome"] == "comparison_not_passed"
    # Every human numerical path uses the same bounded formatter.
    assert report["scopes"][0]["threshold"] == encode_rational(threshold)


async def test_actual_diagnostic_import_note_precedes_successor_grouping(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.family15_report import report_lines
    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family15_diagnostics import failing_source, linked_run

    source = await failing_source(tmp_path / "source", monkeypatch)
    notes = []
    original = fixtures.evolve
    grouping = _Campaign.run_grouping

    async def configured(*args, **kwargs):
        kwargs["notes"] = notes.append
        return await original(*args, **kwargs)

    async def entry(self, number):
        assert any("diagnostic imports · 2/2 · complete" in line for line in notes)
        assert any(
            "capture_time_producer_attestation" in line and "captured_at_import" in line
            for line in notes
        )
        report = inspect_campaign(self.run_root).to_payload()["independent"]
        assert any("diagnostic imports · 2/2 · complete" in line for line in report_lines(report))
        return await grouping(self, number)

    monkeypatch.setattr(fixtures, "evolve", configured)
    monkeypatch.setattr(_Campaign, "run_grouping", entry)
    run = await linked_run(tmp_path / "successor", monkeypatch, source)
    assert inspect_campaign(run).to_payload()["independent"]["diagnostics"][0]["included"] == 2


async def test_fresh_final_conditional_cost_is_known_before_editor_dispatch(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from tests.test_tree_campaign_family15_completion import completion_run

    editor = _Campaign.run_editor
    seen = []

    async def entry(self, attempt_id):
        report = inspect_campaign(self.run_root).to_payload()["independent"]
        assert report["remaining_work"]["candidate_final_slots_if_gates_pass"] == 2
        assert report["remaining_work"]["candidate_gate_slots"] is None
        seen.append(attempt_id)
        return await editor(self, attempt_id)

    monkeypatch.setattr(_Campaign, "run_editor", entry)
    await completion_run(tmp_path, monkeypatch, verification="fresh", stop="result")
    assert seen == ["r01-a01"]


def test_final_human_enclosures_include_each_scopes_distinct_confidence():
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.family15_report import (
        campaign15_projection,
        comparison_projection,
        report_lines,
    )
    from evolution.tree.storage.envelope import Envelope
    from tests.family15_fakes import family15_created, v16_document

    profile, reference = built(
        mode="confidence",
        scopes={"focus": ["a"], "causal": ["b"]},
        groups={"a": "g", "b": "g"},
        confidence=(0.25, 0.75),
    )
    result = evaluate_comparison(profile, [row(1, case="a"), row(1, case="b")], reference)
    comparison = comparison_projection(result, profile=profile.to_payload())
    policy = CampaignPolicyV12.from_payload(v16_document()["campaign"])
    created = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=1,
            timestamp="2026-10-19T00:00:00Z",
            event="campaign_created",
            operation_id=None,
            payload=family15_created(policy),
        ),
        family=campaign_family_for(15),
    )
    report = campaign15_projection(reduce((created,), selector=None, family_version=15))
    report["attempts"] = [dict(attempt_id="a", comparison=comparison)]
    text = "\n".join(report_lines(report))
    assert (
        "focus · conditional fixed-scope confidence 0.25 (approx) · lower endpoint enclosure"
        in text
    )
    assert (
        "causal · conditional fixed-scope confidence 0.75 (approx) · lower endpoint enclosure"
        in text
    )


def test_the_report_counts_hits_per_check_and_omits_empty_blocks():
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.family15_report import campaign15_projection, validate_campaign15_projection
    from evolution.tree.storage.envelope import Envelope
    from tests.family15_fakes import family15_created

    profile, _ = built(mode="point", repeats=2)
    payload = family15_created(profile.policy)
    hits = [
        {
            "check": "verbatim_answer_unmatched",
            "code": "verbatim_answer_unmatched",
            "count": n,
            "round": None,
            "operation_id": None,
        }
        for n in (2, 3)
    ]
    records = []
    for event, data in [("campaign_created", payload), *(("check_shadowed", hit) for hit in hits)]:
        records.append(
            Envelope.from_payload(
                dict(
                    schema_version=1,
                    run_id="run",
                    sequence=len(records) + 1,
                    timestamp="2026-10-26T00:00:00Z",
                    event=event,
                    operation_id=None,
                    payload=data,
                ),
                family=campaign_family_for(15),
            )
        )
    empty = campaign15_projection(reduce(records[:1], selector=None, family_version=15))
    assert "check_modes" not in empty and "shadow_hits" not in empty
    report = campaign15_projection(reduce(records, selector=None, family_version=15))
    assert report["shadow_hits"] == {"verbatim_answer_unmatched": 5}
    validate_campaign15_projection(report)


@pytest.mark.parametrize(
    "field,value",
    [
        ("check_modes", []),
        ("shadow_hits", None),
        ("check_modes", {"seal_oracle_leak": "silent"}),
        ("shadow_hits", {"seal_oracle_leak": True}),
        ("shadow_hits", {"unknown_check": 1}),
    ],
)
def test_shadow_report_blocks_refuse_malformed_data(field, value):
    from evolution.tree.family15_report import validate_campaign15_projection
    from evolution.tree.storage.envelope import SchemaError

    profile, _ = built(mode="point", repeats=2)
    report = report_for(profile.policy)
    report[field] = value
    with pytest.raises(SchemaError):
        validate_campaign15_projection(report)


def _attempt_projection(outcome, reason):
    profile, _ = built(mode="point", repeats=1)
    report = report_for(profile.policy)
    report["attempts"] = [
        dict(
            round=1,
            attempt_id="a",
            outcome=outcome,
            reason=reason,
            comparison=None,
            gate_comparison=None,
            global_comparison=None,
            preflight=None,
            conditional_cost=None,
            eligible_final_score=None,
        )
    ]
    return report


def test_projection_accepts_every_family15_attempt_close():
    from evolution.tree.campaign_schema import _FINAL_ATTEMPT_OUTCOMES
    from evolution.tree.family15_report import (
        family15_attempt_vocabulary,
        validate_campaign15_projection,
    )
    from evolution.tree.family15_schema import REASONS, validate_family15_payload
    from evolution.tree.storage.envelope import SchemaError
    from tests.family15_fakes import reference15

    admitted = set()
    for outcome, reasons in _FINAL_ATTEMPT_OUTCOMES.items():
        for reason in reasons:
            payload = dict(
                attempt_id="a",
                outcome=outcome,
                reason=reason,
                look=None,
                code="seal_refused" if reason == "seal_refused" else None,
            )
            try:
                validate_family15_payload("attempt_closed", payload)
            except SchemaError:
                continue
            admitted.add((outcome, reason))
    for outcome in ("rejected", "finalist"):
        for reason in REASONS | {"structurally_unattainable"}:
            payload = dict(
                attempt_id="a",
                outcome=outcome,
                reason=reason,
                path="candidate",
                evidence=reference15(),
            )
            try:
                validate_family15_payload("attempt_closed", payload)
            except SchemaError:
                continue
            admitted.add((outcome, reason))
    vocabulary = family15_attempt_vocabulary()
    assert {
        ("insufficient_evidence", "incomplete_sample"),
        ("finalist", "passed"),
        ("rejected", "threshold_not_passed"),
    } <= admitted
    assert admitted <= {(o, r) for o, reasons in vocabulary.items() for r in reasons}
    for outcome, reasons in vocabulary.items():
        for reason in reasons:
            validate_campaign15_projection(_attempt_projection(outcome, reason))


def test_projection_refuses_unknown_attempt_outcome():
    from evolution.tree.family15_report import validate_campaign15_projection
    from evolution.tree.storage.envelope import SchemaError

    with pytest.raises(SchemaError):
        validate_campaign15_projection(_attempt_projection("bogus", "incomplete_sample"))


def test_projection_refuses_reason_from_another_attempt_outcome():
    from evolution.tree.family15_report import validate_campaign15_projection
    from evolution.tree.storage.envelope import SchemaError

    with pytest.raises(SchemaError):
        validate_campaign15_projection(_attempt_projection("rejected", "editor_declined"))


def test_remaining_cost_skips_closed_insufficient_evidence():
    from types import SimpleNamespace as State

    from evolution.tree.family15_report import remaining_cost

    policy = State(repeats=1, fresh_parent=True, verification="reuse", stability="stable")

    def attempt(name, outcome):
        return State(
            attempt_id=name,
            outcome=outcome,
            measurement_id=name,
            parent_measurement_id=name + "-parent",
            family15=State(
                preflight=None,
                comparison=None,
                comparison_binding=object(),
                global_result=None,
                completion=None,
                final_completion=None,
                final_binding=None,
            ),
        )

    closed, active = attempt("a1", "insufficient_evidence"), attempt("a2", None)
    snapshot = State(
        identity={"corpus_cases": [1, 2, 3]},
        finished=None,
        first_round=1,
        family15=None,
        measurements={},
        rounds={
            1: State(
                round=1,
                closed=None,
                baseline_complete=True,
                baseline_measurement_id=None,
                attempts={"a1": closed, "a2": active},
            )
        },
    )
    remaining = remaining_cost(
        snapshot, policy, {}, {"a1": 2, "a1-parent": 2, "a2": 2, "a2-parent": 2}
    )
    assert remaining["candidate_gate_slots"] == 2
    assert remaining["parent_gate_slots"] == 2
    assert remaining["candidate_final_slots_if_gates_pass"] == 1


@pytest.mark.parametrize("mode", ["point", "statistical"])
@pytest.mark.parametrize("fresh", [False, True])
async def test_projection_validates_focus_rejected_attempt(tmp_path, monkeypatch, mode, fresh):
    from dataclasses import replace

    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_measurement import FrozenMeasurement
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.family15_report import report_lines, validate_campaign15_projection
    from evolution.tree.roles.editor.editor_evidence import _result
    from evolution.tree.run_watch import WatchSession
    from tests.campaign_drivers import TWO_CASE_SCORER, _records
    from tests.campaign_fakes import corpus
    from tests.test_tree_campaign_family15_gate import gate_run

    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode=mode,
        fresh=fresh,
        repeats=2,
        passing=False,
        stop="result",
        gate_options={"stages": "focus_first"},
        campaign_options={"attempt_feedback": "sequential"},
        scorer=TWO_CASE_SCORER.replace(
            '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
        ),
    )
    status = inspect_campaign(run, verify=True).to_payload()
    assert not status["verification"]["mismatches"]
    report = status["independent"]
    validate_campaign15_projection(report)
    attempt = report["attempts"][0]
    assert attempt["comparison"]["scopes"][1]["status"] == "not_measured"
    expected_slots = dict(scheduled=4, dispatched=2, not_owed=2)
    assert attempt["slots"] == {
        "candidate": expected_slots,
        **({"parent": expected_slots} if fresh else {}),
    }
    assert attempt["conditional_cost"]["candidate_gate_slots"] == 2
    saving = 4 if fresh else 2
    assert any(
        f"causal not_measured · {saving} slots not owed" in line for line in report_lines(report)
    )
    records = _records(run / "campaign.jsonl")
    feedback = [r["payload"]["feedback"] for r in records if r["event"] == "attempt_input_frozen"][
        1
    ]
    prior = feedback["attempts"][0]
    assert prior["reason"] == "threshold_not_passed"
    causal = next(c for c in prior["cases"] if c["case_id"] == "c2")
    assert causal == dict(
        case_id="c2",
        status="not_measured",
        delta=None,
        paired_repeats=0,
        expected_repeats=2,
        incomplete=False,
    )
    # A valid shape cannot relabel an owed focus case as unmeasured.
    import copy
    import hashlib

    from evolution.tree.campaign_schema import CampaignLifecycle
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_schema import FAMILY15_CONTRACT
    from evolution.tree.storage.envelope import Envelope, SchemaError

    frozen = next(
        r
        for r in records
        if r["event"] == "attempt_input_frozen" and r["payload"]["feedback"]["attempts"]
    )
    state_machine = CampaignLifecycle(selector=None, family_version=15)
    for prior_record in records[: frozen["sequence"] - 1]:
        state_machine.accept(Envelope.from_payload(prior_record, family=FAMILY15_CONTRACT))
    bad = copy.deepcopy(frozen)
    unmeasured = next(
        c for c in bad["payload"]["feedback"]["attempts"][0]["cases"] if "status" in c
    )
    unmeasured["case_id"] = "c1"
    bad["payload"]["input_digest"] = hashlib.sha256(
        canonical_json(
            {
                key: bad["payload"][key]
                for key in ("common_input_digest", "feedback", "feedback_mode", "source_refs")
            }
        )
    ).hexdigest()
    with pytest.raises(SchemaError, match="unmeasured feedback cases differ"):
        state_machine.accept(Envelope.from_payload(bad, family=FAMILY15_CONTRACT))
    view = inspect_view(run)
    state = view.snapshot.rounds[1].attempts["r01-a01"]
    policy = CampaignPolicyV12.from_payload(view.snapshot.identity["policy"])
    cases = tuple(replace(case, case_id=f"c{i}") for i, case in enumerate(corpus(2), 1))
    result = _result(
        state, {state.measurement_id: FrozenMeasurement(run, state.measurement_id)}, cases, policy
    )
    assert all(
        r.terminal == "not_measured" for c in result.cases if c.case_id == "c2" for r in c.repeats
    )
    session = WatchSession(run)
    try:
        assert "not_measured" in "\n".join(session.snapshot.lines())
    finally:
        session.close()


@pytest.mark.parametrize(
    "cut,scope", [("after_focus_stage", "focus"), ("after_gate_measurement_completed", "causal")]
)
async def test_watch_shows_stage_scope(tmp_path, monkeypatch, cut, scope):
    from evolution.tree.run_watch import WatchSession
    from tests.test_tree_campaign_family15_gate import gate_run

    run = await gate_run(tmp_path, monkeypatch, stop=cut, gate_options={"stages": "focus_first"})
    session = WatchSession(run)
    try:
        assert f"measurement ({scope})" in "\n".join(session.snapshot.lines())
        assert f"measurement ({scope})" in session.snapshot.snapshot_line()
    finally:
        session.close()


@pytest.mark.parametrize("side", ["candidate", "parent"])
def test_projection_refuses_per_side_slot_overcommit(side):
    from evolution.tree.family15_report import validate_campaign15_projection
    from evolution.tree.storage.envelope import SchemaError

    report = _attempt_projection("rejected", "threshold_not_passed")
    report["settings"].update(stages="focus_first", fresh_parent=True)
    report["attempts"][0]["slots"] = {
        name: dict(scheduled=4, dispatched=2, not_owed=2) for name in ("candidate", "parent")
    }
    validate_campaign15_projection(report)
    report["attempts"][0]["slots"][side]["dispatched"] = 3
    with pytest.raises(SchemaError):
        validate_campaign15_projection(report)


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered", "statistical"])
@pytest.mark.parametrize("fresh", [False, True])
def test_focus_projection_vocabulary_matches_kernel(mode, fresh):
    import copy

    from evolution.tree.case_comparison import evaluate_focus, focus_rejection
    from evolution.tree.family15_report import (
        comparison_projection,
        validate_campaign15_projection,
    )
    from evolution.tree.family15_schema import encode_rational
    from evolution.tree.storage.envelope import SchemaError

    profile, refs = built(
        mode=mode,
        fresh=fresh,
        repeats=2,
        stages="focus_first",
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    rows = [
        row(0, case=case, repeat=repeat, critical=False) for case in ("a", "b") for repeat in (1, 2)
    ]
    focus = evaluate_focus(profile, rows, refs, campaign_seed=profile.policy.random_seed)
    result = focus_rejection(profile, focus)
    comparison = comparison_projection(result, profile=profile.to_payload(), policy=profile.policy)
    report = report_for(profile.policy)
    attempt = _attempt_projection("rejected", result.reason)["attempts"][0]
    attempt.update(
        comparison=comparison,
        gate_comparison=copy.deepcopy(comparison),
        slots={
            name: dict(scheduled=4, dispatched=2, not_owed=2)
            for name in (("candidate", "parent") if fresh else ("candidate",))
        },
    )
    report["attempts"] = [attempt]
    assert report["settings"]["stages"] == "focus_first"
    assert comparison["scopes"][1]["status"] == "not_measured"
    validate_campaign15_projection(report)

    def invalid(change):
        broken = copy.deepcopy(report)
        change(broken)
        with pytest.raises(SchemaError):
            validate_campaign15_projection(broken)

    invalid(lambda r: r["settings"].update(stages="unknown"))
    invalid(lambda r: r["attempts"][0]["slots"]["candidate"].update(not_owed=-1))
    invalid(lambda r: r["attempts"][0]["slots"]["candidate"].update(dispatched=3))
    invalid(
        lambda r: r["attempts"][0]["slots"].update(
            baseline=dict(scheduled=4, dispatched=2, not_owed=2)
        )
    )
    invalid(
        lambda r: r["attempts"][0]["comparison"]["scopes"][1].update(
            point=encode_rational(Fraction(1))
        )
    )
    invalid(lambda r: r["attempts"][0]["comparison"]["scopes"][0].update(status="not_measured"))
    invalid(
        lambda r: r["attempts"][0]["comparison"].update(
            outcome="passed", reason="passed", eligible=True
        )
    )
