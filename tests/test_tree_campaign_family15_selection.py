"""Full-score ranking and retained evidence are automatic selection authority."""

import pytest

from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.family15_audit import authenticate_auxiliary
from tests.campaign_drivers import TWO_CASE_SCORER, _only_c1, _records
from tests.test_tree_campaign_family15_completion import completion_run

pytestmark = pytest.mark.slow


def data(run, payload):
    return authenticate_auxiliary(run, AuxiliaryPublication.from_payload(payload["data"]))


@pytest.mark.parametrize("stability", ["stable", "unstable"])
async def test_global_rejection_retains_last_reference_and_opens_next(
    tmp_path, monkeypatch, stability
):
    # Gate sees c1 improve; c2 outside the affected set regresses, tying the full mean.
    scorer = TWO_CASE_SCORER.replace(
        '"score": 1.0 if passed else 0.0',
        '"score": (0.0 if payload["oracle"].get("pass") and "edited" in skill else 1.0 if passed else 0.0)',
    )
    run = await completion_run(
        tmp_path,
        monkeypatch,
        stability=stability,
        used=_only_c1,
        scorer=scorer,
        stop="result",
        campaign_options={"attempts_per_round": 2, "round_stop": "first_accepted", "rounds": 2},
    )
    records = _records(run / "campaign.jsonl")
    results = [r["payload"] for r in records if r["event"] == "global_comparison_completed"]
    assert len(results) == 4
    assert all(not p["accepted"] for p in results)
    selections = [r["payload"] for r in records if r["event"] == "selection_decided"]
    assert all(s["selected_candidate"] is None for s in selections)
    assert all(
        r["payload"]["outcome"] == "retained" for r in records if r["event"] == "round_closed"
    )
    assert sum(r["event"] == "champion_final_bound" for r in records) == (
        4 if stability == "unstable" else 0
    )
    baseline2 = next(
        r["payload"]
        for r in records
        if r["event"] == "baseline_bound" and r["payload"]["round"] == 2
    )
    assert baseline2["measurement_id"] == selections[0]["next_champion"]["measurement_id"]
    assert selections[0]["next_champion"] == results[1]["reference"]
    assert verify_evidence(run)["mismatches"] == []


async def test_full_score_tie_prefers_smaller_attempt_without_new_baseline(tmp_path, monkeypatch):
    run = await completion_run(
        tmp_path,
        monkeypatch,
        stop="result",
        campaign_options={"attempts_per_round": 2, "rounds": 2},
    )
    records = _records(run / "campaign.jsonl")
    selected = next(r["payload"] for r in records if r["event"] == "selection_decided")
    ranking = data(run, selected)
    assert ranking["proposed_attempt"] == "r01-a01"
    assert [entry["point"] for entry in ranking["entries"]] == [
        {"format": "rational-hex/1", "numerator": "1", "denominator": "1"}
    ] * 2
    baseline = next(
        r["payload"]
        for r in records
        if r["event"] == "baseline_bound" and r["payload"]["round"] == 2
    )
    assert baseline["measurement_id"] == selected["selected_evidence"]["measurement_id"]
    assert verify_evidence(run)["mismatches"] == []


async def test_exact_full_score_beats_attempt_index(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.campaign_fakes import FakeAdapter

    class Tagged(FakeAdapter):
        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            send = instance.send

            async def tagged(session, message, request_id):
                result = await send(session, message, request_id)
                event = {
                    **result.events[0],
                    "text": result.events[0]["text"] + " #" + candidate.artifact_id,
                }
                return replace(result, events=(event, *result.events[1:]))

            instance.send = tagged
            return instance

    scorer = TWO_CASE_SCORER.replace(
        '"score": 1.0 if passed else 0.0',
        '"score": (0.9999999999999998 if "#r01-a01" in view else 1.0) if passed else 0.0',
    )
    run = await completion_run(
        tmp_path,
        monkeypatch,
        adapter=Tagged(used=_only_c1),
        used=_only_c1,
        scorer=scorer,
        stop="result",
        campaign_options={"attempts_per_round": 2},
    )
    selected = next(
        r["payload"] for r in _records(run / "campaign.jsonl") if r["event"] == "selection_decided"
    )
    ranking = data(run, selected)
    assert ranking["proposed_attempt"] == "r01-a02"
    assert ranking["entries"][0]["point"] != ranking["entries"][1]["point"]
    assert verify_evidence(run)["mismatches"] == []


async def test_eligible_settled_evidence_is_selected_and_promoted_automatically(
    tmp_path, monkeypatch
):
    run = await completion_run(tmp_path, monkeypatch, stop="result")
    records = _records(run / "campaign.jsonl")
    assert any(
        r["event"] == "global_comparison_completed" and r["payload"]["accepted"] for r in records
    )
    selected = next(r["payload"] for r in records if r["event"] == "selection_decided")
    assert set(selected) == {
        "round",
        "data",
        "reason",
        "selected_candidate",
        "selected_evidence",
        "next_champion",
    }
    assert selected["reason"] == "automatic" and selected["selected_candidate"] is not None
    closed = next(r["payload"] for r in records if r["event"] == "round_closed")
    assert closed["outcome"] == "promoted"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("tamper", ["final_profile", "global", "ranking"])
async def test_real_producer_semantic_forgery_is_rejected_by_e(tmp_path, monkeypatch, tamper):
    import copy
    from fractions import Fraction

    from evolution.tree.campaign import _Campaign
    from evolution.tree.family15_schema import encode_rational

    original = _Campaign.publish_comparison_data

    def publish(self, body):
        body = copy.deepcopy(body)
        if tamper == "final_profile" and body["kind"] == "final":
            body["comparison"]["scopes"][0]["threshold"] = encode_rational(Fraction(1, 8))
        if (
            tamper == "global"
            and body["kind"] == "result"
            and body["scopes"][0]["name"] == "global"
        ):
            body["scopes"][0]["point"] = encode_rational(Fraction(1, 4))
        if tamper == "ranking" and body["kind"] == "ranking":
            for entry in body["entries"]:
                entry["point"] = encode_rational(Fraction(1, 2))
        return original(self, body)

    monkeypatch.setattr(_Campaign, "publish_comparison_data", publish)
    run = await completion_run(tmp_path, monkeypatch, stop="result")
    findings = verify_evidence(run)["mismatches"]
    assert any(m["code"] == "family15_evidence_mismatch" for m in findings), findings
    assert not any(m["code"] == "family15_audit_incomplete" for m in findings)


@pytest.mark.parametrize("side", ["gate", "candidate", "champion"])
async def test_real_final_audit_rejects_corrupt_attestation_chain(tmp_path, monkeypatch, side):
    from evolution.tree.execution.journal_identity import canonical_json

    run = await completion_run(
        tmp_path, monkeypatch, verification="fresh", stability="unstable", stop="result"
    )
    path = run / "campaign.jsonl"
    records = _records(path)
    mid = {"gate": "m-r01-a01", "candidate": "m-r01-a01-final", "champion": "m-r01-final-champion"}[
        side
    ]
    record = next(
        r
        for r in records
        if r["event"] == "instance_attested" and r["payload"]["measurement_id"] == mid
    )
    record["payload"]["identity"]["semantic"]["dependency_digest"] = "f" * 64
    record["payload"]["dependency_digest"] = "f" * 64
    path.write_bytes(b"".join(canonical_json(r) + b"\n" for r in records))
    findings = verify_evidence(run)["mismatches"]
    assert any(
        m["event"] == "global_comparison_completed"
        and m["code"] == "family15_evidence_mismatch"
        and "attestation" in m["detail"]
        for m in findings
    ), findings


def test_ranking_full_weighting_and_missing_repeat():
    from evolution.tree.case_comparison import full_primary_score
    from tests.test_tree_case_comparison import row

    rows = [row(1, case="a"), row(1, case="a", repeat=2), row(0, case="b"), row(0, case="c")]
    schedule = [
        dict(
            case_id=r.case_id,
            repeat=r.repeat,
            sample_id=r.sample_id,
            requested_seed=r.requested_seed,
        )
        for r in rows
    ]
    corpus = [
        dict(case_id="a", group_id="small"),
        dict(case_id="b", group_id="large"),
        dict(case_id="c", group_id="large"),
    ]
    assert full_primary_score(rows, schedule, corpus=corpus, primary="score") == {
        "format": "rational-hex/1",
        "numerator": "1",
        "denominator": "2",
    }
    assert full_primary_score(rows[:-1], schedule, corpus=corpus, primary="score") is None
