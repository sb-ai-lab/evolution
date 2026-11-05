"""Real family-15 gate producer; stop before later-task final orchestration."""

import json

import pytest

from tests.campaign_drivers import (
    _records,
    observe_barrier,  # noqa: F401
)
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import family14_document
from tests.family15_fakes import family15_admitted, v16_document

pytestmark = pytest.mark.slow


class GateBoundary(Exception):
    pass


class GatePowerCut(BaseException):
    pass


async def gate_run(
    tmp_path,
    monkeypatch,
    *,
    mode="point",
    passing=True,
    fresh=False,
    stop="after_attempt_closed",
    hard=False,
    repeats=1,
    noise="negligible",
    campaign_options=None,
    gate_options=None,
    **run_options,
):
    from evolution.tree.campaign import _Campaign
    from tests import campaign_drivers as fixtures

    family15_admitted(monkeypatch)
    real = fixtures.evolve

    async def evolve(path, *args, **kwargs):
        document = family14_document(json.loads(path.read_text()), repeats=repeats)
        document["schema"] = "evolution-v2-manifest/16"
        document["campaign"]["gate"] = v16_document()["campaign"]["gate"]
        if campaign_options and "round_stop" in campaign_options:
            document["campaign"]["round_stop"] = campaign_options["round_stop"]
        gate = document["campaign"]["gate"]
        gate.update(mode=mode, fresh_parent=fresh, execution_noise={"kind": noise})
        if mode == "unfiltered":
            gate["global"] = "off"
        gate["focus"]["margin"] = -0.9 if mode == "confidence" else 0 if passing else 0.5
        if mode == "confidence":
            gate["focus"]["confidence"] = 0.1
            gate["causal"]["confidence"] = 0.1
            gate["causal"]["tolerance"] = 1
            gate["statistics"] = {"method": "bounded-weighted-mean/1", "unit": "case"}
        if mode == "statistical":
            gate["focus"]["confidence"] = gate["causal"]["confidence"] = 0.75
        gate.update(gate_options or {})
        path.write_text(json.dumps(document))
        return await real(path, *args, **kwargs)

    original = _Campaign.hook

    def hook(self, name):
        original(self, name)
        if name == stop and (
            stop != "after_attempt_opened" or len(self.snapshot().rounds[1].attempts) == 2
        ):
            raise GatePowerCut() if hard else GateBoundary()

    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", evolve)
        patch.setattr(_Campaign, "hook", hook)
        if stop == "result":

            def result(self):
                raise GateBoundary()

            patch.setattr(_Campaign, "result", result)
        with pytest.raises(GatePowerCut if hard else GateBoundary):
            await _two_case_run(
                tmp_path,
                terminal=None,
                campaign={"attempts_per_round": 2, **(campaign_options or {})},
                **run_options,
            )
    return tmp_path / "run"


@pytest.mark.parametrize("mode", ["point", "confidence"])
async def test_real_r1_comparison_reaches_finalist_boundary(tmp_path, monkeypatch, mode):
    run = await gate_run(tmp_path, monkeypatch, mode=mode)
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    assert result["outcome"] == "passed"
    assert records[-1]["event"] == "attempt_closed"
    assert records[-1]["payload"]["outcome"] == "finalist"
    assert not any(
        r["event"] in {"noise_calibration_bound", "comparison_look_evaluated", "campaign_suspended"}
        for r in records
    )
    assert len([r for r in records if r["event"] == "gate_measurement_completed"]) == 1


async def test_nonpassing_comparison_opens_next_attempt_without_suspension(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER

    scorer = TWO_CASE_SCORER.replace(
        '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
    )
    run = await gate_run(
        tmp_path, monkeypatch, passing=False, scorer=scorer, stop="after_attempt_opened"
    )
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "attempt_opened"
    assert records[-1]["payload"]["attempt_index"] == 2
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    assert result["outcome"] == "comparison_not_passed"
    assert not any(r["event"] == "campaign_suspended" for r in records)


async def test_fresh_parent_is_measured_once_and_settled_independently(tmp_path, monkeypatch):
    run = await gate_run(tmp_path, monkeypatch, fresh=True)
    records = _records(run / "campaign.jsonl")
    settled = [r["payload"] for r in records if r["event"] == "gate_measurement_completed"]
    assert [r["side"] for r in settled] == ["candidate", "parent"]
    assert all(r["required"] == r["admissible"] == 2 for r in settled)
    for entry in settled:
        events = _records(run / "measurements" / entry["source"]["measurement_id"] / "events.jsonl")
        assert sum(r["event"] == "scoring_finished" for r in events) == 2


async def test_actual_gate_numeric_evidence_audits_without_pending_hooks(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence

    run = await gate_run(tmp_path, monkeypatch)
    audit = verify_evidence(run)
    assert not audit["mismatches"], audit


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("tolerance", [0, "measured_noise"])
async def test_real_fallback_binding_result_projection_and_full_audit(
    tmp_path, monkeypatch, fresh, tolerance
):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.family15_audit import authenticate_auxiliary
    from tests.campaign_drivers import EDITED

    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode="confidence",
        repeats=2,
        fresh=fresh,
        gate_options={
            "bound_fallback": "point",
            "focus": {"margin": 0, "confidence": 0.9},
            "causal": {"tolerance": tolerance, "confidence": 0.9},
        },
        stop="after_global_comparison_completed",
        **EDITED,
    )
    records = _records(run / "campaign.jsonl")
    bodies = {
        event: authenticate_auxiliary(
            run,
            AuxiliaryPublication.from_payload(
                next(r["payload"]["data"] for r in records if r["event"] == event)
            ),
        )
        for event in ("comparison_bound", "comparison_completed")
    }
    bound, result = bodies["comparison_bound"], bodies["comparison_completed"]
    assert bound["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}
    assert bound["scopes"][1]["rule"] == (
        "bound" if fresh and tolerance == "measured_noise" else "point"
    )
    assert result["scopes"][1]["fallback"] == {"reason": "causal_unconfirmable"}
    assert result["outcome"] == "passed"
    projection = inspect_campaign(run, verify=True).to_payload()
    assert not projection["verification"]["mismatches"], projection["verification"]
    assert (
        projection["independent"]["attempts"][0]["gate_comparison"]["scopes"][1]["fallback"]
        == result["scopes"][1]["fallback"]
    )


@pytest.mark.parametrize("stop", ["after_comparison_bound", "after_comparison_data_published"])
async def test_fallback_pending_profile_is_readable_and_resumes_without_remeasurement(
    tmp_path, monkeypatch, stop
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import EDITED

    options = dict(
        mode="confidence",
        repeats=2,
        fresh=True,
        gate_options={
            "bound_fallback": "point",
            "focus": {"margin": 0, "confidence": 0.9},
            "causal": {"tolerance": "measured_noise", "confidence": 0.9},
        },
        **EDITED,
    )
    run = await gate_run(tmp_path, monkeypatch, stop=stop, hard=True, **options)
    before = {p: p.read_bytes() for p in (run / "measurements").glob("*/events.jsonl")}
    projection = inspect_campaign(run).to_payload()
    assert projection["independent"]["evidence"] == "authenticated_publications"
    run = await gate_run(tmp_path, monkeypatch, stop="after_attempt_closed", **options)
    # Already paid prefixes stay unchanged (the early publication cut may
    # precede Candidate measurement entirely).
    assert all(p.read_bytes().startswith(raw) for p, raw in before.items())
    assert sum(r["event"] == "comparison_bound" for r in _records(run / "campaign.jsonl")) == 1
    assert not verify_evidence(run)["mismatches"]


async def test_fallback_history_uses_bound_epoch_policy_after_authorized_change(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evolve import EvolveRefused, evolve
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import EDITED, _Resettable

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
    result = await evolve(
        path, run, adapter_factory=lambda *a: adapter, authorizations=[authorization]
    )
    assert result.terminal == "finished"
    projection = inspect_campaign(run, verify=True).to_payload()
    assert not projection["verification"]["mismatches"], projection["verification"]
    assert "bound_fallback" not in projection["independent"]["settings"]
    comparison = projection["independent"]["attempts"][0]["gate_comparison"]
    assert comparison["bound_fallback"] == "point"
    assert comparison["scopes"][1]["fallback"] == {"reason": "causal_unconfirmable"}


@pytest.mark.parametrize("mode", ["point", "statistical", "confidence"])
@pytest.mark.parametrize("fresh", [False, True])
async def test_measured_noise_real_binding_decision_and_audit(tmp_path, monkeypatch, mode, fresh):
    from fractions import Fraction

    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary
    from evolution.tree.family15_schema import decode_rational as dq
    from evolution.tree.family15_statistics import calibration_seed_binding
    from tests.campaign_drivers import VARYING

    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode=mode,
        fresh=fresh,
        repeats=4,
        gate_options={
            "focus": {
                "margin": -0.9 if mode == "confidence" else 0,
                **({"confidence": 0.1} if mode != "point" else {}),
            },
            "causal": {
                "tolerance": "measured_noise",
                **({"confidence": 0.1} if mode != "point" else {}),
            },
        },
        **VARYING,
    )
    records = _records(run / "campaign.jsonl")
    bound = next(r for r in records if r["event"] == "comparison_bound")
    completed = next(r for r in records if r["event"] == "comparison_completed")
    binding = authenticate_auxiliary(
        run, AuxiliaryPublication.from_payload(bound["payload"]["data"])
    )
    result = authenticate_auxiliary(
        run, AuxiliaryPublication.from_payload(completed["payload"]["data"])
    )
    assert ("calibration" in binding) != fresh
    assert ("calibration" in result) == fresh
    calibration = result["calibration"] if fresh else binding["calibration"]
    assert calibration["source"] == ("fresh" if fresh else "stored")
    assert calibration["prefix"] == completed["payload"]["reference"]["prefix"]
    assert calibration["measurement_id"] == completed["payload"]["reference"]["measurement_id"]
    assert dq(calibration["allowance"]) > 0
    assert calibration["degenerate"] is False
    assert "noise_allowance_empirical" in result["caveats"]
    assert result["scopes"][1]["threshold"] is not None
    assert dq(result["scopes"][1]["threshold"]) == -dq(calibration["allowance"])
    expected_seed = (
        result["binding_digest"]
        if fresh
        else calibration_seed_binding(
            binding["policy_digest"],
            binding["reference"]["measurement_id"],
            binding["reference"]["prefix"],
            [(c["case_id"], dq(c["weight"])) for c in binding["scopes"][1]["cases"]],
        )
    )
    assert calibration["seed_input"] == expected_seed
    assert calibration["retained"] == 1
    assert dq(calibration["level"]) == Fraction(995, 1000)
    assert bound["sequence"] < completed["sequence"]
    assert sum(r["event"] == "measurement_bound" for r in records) == 2
    assert sum(r["event"] == "parent_measurement_bound" for r in records) == int(fresh)
    assert not verify_evidence(run)["mismatches"]
    projection = inspect_campaign(run, verify=True).to_payload()
    assert not projection["verification"]["mismatches"]


@pytest.mark.parametrize("fresh", [False, True])
async def test_measured_noise_zero_variation_is_decided_not_closed(tmp_path, monkeypatch, fresh):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary
    from evolution.tree.family15_schema import decode_rational
    from tests.campaign_drivers import EDITED

    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=fresh,
        repeats=2,
        gate_options={"causal": {"tolerance": "measured_noise"}},
        **EDITED,
    )
    records = _records(run / "campaign.jsonl")
    data = {
        event: authenticate_auxiliary(
            run,
            AuxiliaryPublication.from_payload(
                next(r["payload"]["data"] for r in records if r["event"] == event)
            ),
        )
        for event in ("comparison_bound", "comparison_completed")
    }
    calibration = data["comparison_completed" if fresh else "comparison_bound"]["calibration"]
    assert decode_rational(calibration["allowance"]) == 0 and calibration["degenerate"]
    assert data["comparison_completed"]["caveats"] == ["noise_not_observed"]
    assert data["comparison_completed"]["outcome"] == "passed"
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_measured_noise_resume_after_calculation_reuses_paid_rows(
    tmp_path, monkeypatch, fresh, fallback
):
    import copy

    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary
    from tests.campaign_drivers import VARYING

    options = dict(
        fresh=fresh,
        repeats=4,
        mode="confidence" if fallback else "point",
        gate_options={
            **({"bound_fallback": "point"} if fallback else {}),
            "causal": {"tolerance": "measured_noise", **({"confidence": 0.9} if fallback else {})},
        },
        **VARYING,
    )
    original = _Campaign.publish_comparison_data
    calculated = None

    def cut(self, body):
        nonlocal calculated
        publication = original(self, body)
        if body["kind"] == "result":
            calculated = copy.deepcopy(body)
            raise GatePowerCut()
        return publication

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "publish_comparison_data", cut)
        run = await gate_run(tmp_path, patch, stop="unused", hard=True, **options)
    assert calculated is not None
    paid = {p: p.read_bytes() for p in (run / "measurements").glob("*/events.jsonl")}
    before = _records(run / "campaign.jsonl")
    run = await gate_run(tmp_path, monkeypatch, **options)
    assert all(p.read_bytes() == data for p, data in paid.items())
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_bound" for r in records) == 1
    assert sum(r["event"] == "gate_measurement_completed" for r in before) == (2 if fresh else 1)
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    assert (
        authenticate_auxiliary(run, AuxiliaryPublication.from_payload(result["data"])) == calculated
    )
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("fresh", [False, True])
async def test_measured_noise_audit_rebuilds_self_consistent_forged_metadata(
    tmp_path, monkeypatch, fresh
):
    import copy
    import hashlib

    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.journal_identity import canonical_json
    from tests.campaign_drivers import VARYING

    original = _Campaign.publish_comparison_data

    def forged(self, body):
        body = copy.deepcopy(body)
        if "calibration" in body:
            calibration = body["calibration"]
            calibration["seed_input"] = "f" * 64
            calibration["result_digest"] = hashlib.sha256(
                canonical_json({k: v for k, v in calibration.items() if k != "result_digest"})
            ).hexdigest()
        return original(self, body)

    monkeypatch.setattr(_Campaign, "publish_comparison_data", forged)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=fresh,
        repeats=4,
        gate_options={"causal": {"tolerance": "measured_noise"}},
        **VARYING,
    )
    audit = verify_evidence(run)
    assert any(m["code"] == "family15_evidence_mismatch" for m in audit["mismatches"]), audit


@pytest.mark.parametrize("fallback", [False, True])
async def test_measured_noise_insufficient_stored_parent_continues_without_a_calibration(
    tmp_path, monkeypatch, fallback
):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary
    from tests.campaign_drivers import LENIENT, MANIFEST_SEED, VARYING, _RepeatTagged

    class OneCausalRepeat(_RepeatTagged):
        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                instance._lookup = lambda session: (
                    "target_failed"
                    if session.case_id == "c2" and session.requested_seed != MANIFEST_SEED + 1
                    else "completed"
                )
            return instance

    run = await gate_run(
        tmp_path,
        monkeypatch,
        repeats=2,
        mode="confidence" if fallback else "point",
        gate_options={
            **(
                {"bound_fallback": "point", "focus": {"margin": 0, "confidence": 0.9}}
                if fallback
                else {}
            ),
            "causal": {"tolerance": "measured_noise", **({"confidence": 0.9} if fallback else {})},
        },
        campaign_options={"indeterminate": LENIENT},
        **{**VARYING, "adapter_class": OneCausalRepeat},
    )
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    assert (result["outcome"], result["reason"], result["deciding_scope"]) == (
        "comparison_unavailable",
        "insufficient_calibration",
        "causal",
    )
    bound = next(r["payload"] for r in records if r["event"] == "comparison_bound")
    body = authenticate_auxiliary(run, AuxiliaryPublication.from_payload(bound["data"]))
    assert (
        "calibration" not in body and body["calibration_unavailable"] == "insufficient_calibration"
    )
    assert body["scopes"][1]["threshold"] is None
    assert not verify_evidence(run)["mismatches"]
    if fallback:
        from evolution.tree.campaign import inspect_campaign

        projection = inspect_campaign(run, verify=True).to_payload()
        assert not projection["verification"]["mismatches"]
        scopes = projection["independent"]["attempts"][0]["gate_comparison"]["scopes"]
        assert scopes[0]["fallback"] == {"reason": "focus_unattainable"}
        assert scopes[1]["rule"] == "bound" and "fallback" not in scopes[1]


@pytest.mark.parametrize("fresh", [False, True])
async def test_statistical_r10_real_comparison_and_projection_audit(tmp_path, monkeypatch, fresh):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary

    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode="statistical",
        repeats=10,
        fresh=fresh,
        stop="after_global_comparison_completed",
    )
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    body = authenticate_auxiliary(run, AuxiliaryPublication.from_payload(result["data"]))
    assert body["outcome"] == "passed"
    assert body["scopes"][0]["rule"] == "bootstrap"
    assert body["caveats"] == ["exact_contrast", "intervals_empirical"]
    policy = records[0]["payload"]["identity"]["policy"]
    assert body["bootstrap"]["look"] == policy["evaluation"]["repeats_per_question"] == 10
    assert body["bootstrap"]["campaign_seed"] == policy["random_seed"]
    global_result = next(
        r["payload"] for r in records if r["event"] == "global_comparison_completed"
    )
    global_body = authenticate_auxiliary(
        run, AuxiliaryPublication.from_payload(global_result["data"])
    )
    assert global_body["scopes"][0]["rule"] == "point"
    assert global_body["bootstrap"]["look"] == policy["evaluation"]["repeats_per_question"]
    assert global_body["caveats"] == ["intervals_empirical"]
    assert not any(r["event"] == "comparison_preflight_failed" for r in records)
    assert not verify_evidence(run)["mismatches"]
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]


@pytest.mark.parametrize("tamper", [False, True])
async def test_statistical_varying_repeats_audit_draws_and_detect_forged_lower(
    tmp_path, monkeypatch, tamper
):
    import copy
    from fractions import Fraction

    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_audit import authenticate_auxiliary
    from evolution.tree.family15_schema import decode_rational as dq
    from evolution.tree.family15_schema import encode_rational as q
    from tests.campaign_drivers import VARYING

    if tamper:
        original = _Campaign.publish_comparison_data

        def forged(self, body):
            body = copy.deepcopy(body)
            if body["kind"] == "result":
                focus = body["scopes"][0]
                assert focus["rule"] == "bootstrap" and focus["exact"] is False
                focus["lower"] = q(dq(focus["lower"]) + Fraction(1, 100))
            return original(self, body)

        monkeypatch.setattr(_Campaign, "publish_comparison_data", forged)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode="statistical",
        repeats=10,
        gate_options={
            "focus": {"margin": 0, "confidence": 0.9},
            "causal": {"tolerance": 0.05, "confidence": 0.95},
        },
        **VARYING,
    )
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    body = authenticate_auxiliary(run, AuxiliaryPublication.from_payload(result["data"]))
    assert body["outcome"] == "passed"
    assert body["caveats"] == ["intervals_empirical"]
    focus, causal = body["scopes"]
    assert focus["exact"] is causal["exact"] is False
    assert dq(focus["lower"]) < dq(focus["point"])
    assert dq(focus["confidence"]) == Fraction(9, 10)
    assert dq(causal["confidence"]) == Fraction(19, 20)
    audit = verify_evidence(run)
    if tamper:
        assert any(
            m["code"] == "family15_evidence_mismatch"
            and m["detail"] == "comparison result differs from exact authenticated-row computation"
            for m in audit["mismatches"]
        )
    else:
        assert not audit["mismatches"], audit
        assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]


async def test_statistical_skips_impossible_local_preflight(tmp_path, monkeypatch):
    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode="statistical",
        repeats=10,
        gate_options={"focus": {"margin": 2, "confidence": 0.75}},
    )
    records = _records(run / "campaign.jsonl")
    assert any(r["event"] == "comparison_completed" for r in records)
    assert not any(r["event"] == "comparison_preflight_failed" for r in records)


async def test_statistical_keeps_early_global_point_impossibility(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import TWO_CASE_SCORER

    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 1.0')
    run = await gate_run(tmp_path, monkeypatch, mode="statistical", repeats=10, scorer=scorer)
    records = _records(run / "campaign.jsonl")
    failed = next(r["payload"] for r in records if r["event"] == "comparison_preflight_failed")
    assert failed["phase"] == "early_global"
    assert not any(r["event"] in {"editor_started", "comparison_bound"} for r in records)
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("tamper", ["seed", "exact"])
async def test_statistical_audit_rederives_seed_and_exactness(tmp_path, monkeypatch, tamper):
    import copy

    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.confidence_statistics import derive_seed

    original = _Campaign.publish_comparison_data

    def forged(self, body):
        body = copy.deepcopy(body)
        if body["kind"] == "result":
            if tamper == "seed":
                b = body["bootstrap"]
                b["campaign_seed"] += 1
                b["seed"] = derive_seed(
                    **{k: v for k, v in b.items() if k not in {"method", "seed"}}
                )
            else:
                for scope in body["scopes"]:
                    if scope.get("exact"):
                        scope["exact"] = False
                body["caveats"] = ["intervals_empirical"]
        return original(self, body)

    monkeypatch.setattr(_Campaign, "publish_comparison_data", forged)
    run = await gate_run(tmp_path, monkeypatch, mode="statistical", repeats=10)
    assert any(
        m["code"] == "family15_evidence_mismatch" for m in verify_evidence(run)["mismatches"]
    )


@pytest.mark.parametrize(
    "cut",
    [
        "after_comparison_data_declared",
        "after_comparison_data_published",
        "after_comparison_bound",
        "after_gate_measurement_completed",
        "after_comparison_completed",
    ],
)
async def test_gate_resume_reuses_publication_and_paid_slots(tmp_path, monkeypatch, cut):
    await gate_run(tmp_path, monkeypatch, stop=cut)
    run = await gate_run(tmp_path, monkeypatch)
    records = _records(run / "campaign.jsonl")
    assert sum(r["event"] == "comparison_bound" for r in records) == 1
    assert sum(r["event"] == "comparison_completed" for r in records) == 1
    assert sum(r["event"] == "auxiliary_payload_declared" for r in records) == 2
    candidate = next(
        r["payload"]["source"]["measurement_id"]
        for r in records
        if r["event"] == "gate_measurement_completed"
    )
    events = _records(run / "measurements" / candidate / "events.jsonl")
    assert sum(r["event"] == "scoring_finished" for r in events) == 2


async def test_completed_candidate_survives_real_parent_only_continue(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable

    adapter = _Resettable()
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        adapter=adapter,
        stop="after_gate_measurement_completed",
        hard=True,
    )
    first = _records(run / "campaign.jsonl")
    completed = [r["payload"] for r in first if r["event"] == "gate_measurement_completed"]
    assert [r["side"] for r in completed] == ["candidate"]
    original_source = completed[0]["source"]
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await gate_run(tmp_path, monkeypatch, fresh=True, adapter=adapter)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        adapter=adapter,
        authorizations=[_continue(refused.value)],
    )
    records = _records(run / "campaign.jsonl")
    rebound = [r["payload"] for r in records if r["event"] == "measurement_rebound"]
    assert len(rebound) == 1
    assert "parent" in str(rebound[0])
    comparisons = [r["payload"] for r in records if r["event"] == "comparison_bound"]
    assert len(comparisons) == 2
    assert comparisons[-1]["candidate"] == original_source
    assert sum(r["event"] == "comparison_completed" for r in records) == 1
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("tamper", ["threshold", "point", "variance"])
async def test_audit_rederives_authenticated_gate_numbers(tmp_path, monkeypatch, tamper):
    import copy
    from fractions import Fraction

    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.family15_schema import encode_rational

    original = _Campaign.publish_comparison_data

    def forged(self, body):
        body = copy.deepcopy(body)
        if tamper == "threshold" and body["kind"] == "binding":
            body["scopes"][0]["threshold"] = encode_rational(Fraction(-1, 4))
        elif tamper == "point" and body["kind"] == "result":
            body["scopes"][0]["point"] = encode_rational(Fraction(0))
        elif tamper == "variance" and body["kind"] == "result":
            body["repeat_diagnostics"][0].update(
                variation=encode_rational(Fraction(0)), unavailable=None
            )
        return original(self, body)

    monkeypatch.setattr(_Campaign, "publish_comparison_data", forged)
    run = await gate_run(tmp_path, monkeypatch)
    audit = verify_evidence(run)
    assert any(m["code"] == "family15_evidence_mismatch" for m in audit["mismatches"])


async def test_unfiltered_still_respects_reported_component_provenance(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence

    run = await gate_run(
        tmp_path,
        monkeypatch,
        mode="unfiltered",
        used=lambda case, turn: [] if case == "c1" else ["skills/SKILL.md"],
    )
    records = _records(run / "campaign.jsonl")
    assert records[-1]["payload"]["reason"] == "no_exam_impact"
    assert not any(
        r["event"] == "measurement_bound" and r["payload"]["attempt_id"] != "champion"
        for r in records
    )
    assert not verify_evidence(run)["mismatches"]


async def test_scorer_failure_suspends_under_fault_owner_before_comparison(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER

    script = TWO_CASE_SCORER.replace(
        "passed = ", 'if "edited" in view or "edited" in skill:\n    sys.exit(1)\npassed = '
    )
    run = await gate_run(tmp_path, monkeypatch, scorer=script, stop="result")
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "campaign_suspended"
    assert not any(r["event"] == "comparison_completed" for r in records)


async def test_corpus_mismatch_suppresses_numeric_recompute_but_checks_sources(
    tmp_path, monkeypatch
):
    import evolution.tree.case_comparison as arithmetic
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.interaction import EvalCase

    run = await gate_run(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("numeric recompute after corpus mismatch")

    monkeypatch.setattr(arithmetic, "evaluate_comparison", forbidden)
    monkeypatch.setattr(arithmetic, "build_profile", forbidden)
    corpus = [EvalCase("different", "g", "s", (), {}, {}, {})]
    audit = verify_evidence(run, corpus=corpus)
    assert [m["code"] for m in audit["mismatches"]] == ["corpus_mismatch"]
    measurement = next(p for p in (run / "measurements").iterdir() if "a01" in p.name)
    path = measurement / "events.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"score":1.0', b'"score":0.0'))
    audit = verify_evidence(run, corpus=corpus)
    assert sum(m["code"] == "corpus_mismatch" for m in audit["mismatches"]) == 1
    assert any(m["code"] != "corpus_mismatch" for m in audit["mismatches"])


async def test_unavailable_stored_support_continues_after_complete_candidate(tmp_path, monkeypatch):
    from tests.campaign_drivers import LENIENT
    from tests.campaign_fakes import FakeAdapter

    class BaselineExcludesC2(FakeAdapter):
        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                instance._lookup = lambda session: (
                    "target_failed" if session.case_id == "c2" else "completed"
                )
            return instance

    from evolution.tree.campaign_projections import verify_evidence

    run = await gate_run(
        tmp_path,
        monkeypatch,
        adapter_class=BaselineExcludesC2,
        used=lambda case, turn: ["skills/SKILL.md"],
        campaign_options={"indeterminate": LENIENT},
        stop="after_attempt_opened",
    )
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    assert (result["outcome"], result["reason"], result["deciding_scope"]) == (
        "comparison_unavailable",
        "empty_support",
        "causal",
    )
    assert records[-1]["payload"]["attempt_index"] == 2
    complete = next(r["payload"] for r in records if r["event"] == "gate_measurement_completed")
    assert complete["required"] == complete["admissible"] == 2
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("repeats", [1, 3])
async def test_observed_noise_never_buys_extra_calls(tmp_path, monkeypatch, repeats):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.family15_audit import authenticate_auxiliary

    run = await gate_run(tmp_path, monkeypatch, fresh=True, repeats=repeats, noise="observed")
    records = _records(run / "campaign.jsonl")
    result = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    body = authenticate_auxiliary(run, AuxiliaryPublication.from_payload(result["data"]))
    assert body["variation"] == ("unavailable" if repeats == 1 else "observed")
    for event in (r for r in records if r["event"] == "gate_measurement_completed"):
        events = _records(
            run / "measurements" / event["payload"]["source"]["measurement_id"] / "events.jsonl"
        )
        assert sum(r["event"] == "scoring_finished" for r in events) == 2 * repeats
    assert sum(r["event"] == "comparison_completed" for r in records) == 1


async def test_internal_v16_preparation_partitions_before_gate_scheduling(tmp_path, monkeypatch):
    from tests import campaign_drivers as fixtures

    original = fixtures.two_case_manifest

    def partitioned(*args, rows, **kwargs):
        rows = [
            {
                **row,
                "group_id": row["case_id"],
                "split": "test" if row["case_id"] == "c2" else "train",
            }
            for row in rows
        ]
        return original(*args, rows=rows, **kwargs)

    monkeypatch.setattr(fixtures, "two_case_manifest", partitioned)
    run = await gate_run(tmp_path, monkeypatch)
    for directory in (run / "measurements").iterdir():
        identity = _records(directory / "events.jsonl")[0]["payload"]["identity"]
        assert {s["case_id"] for s in identity["schedule"]} == {"c1"}
    created = _records(run / "campaign.jsonl")[0]["payload"]
    assert created["adaptation"]["version"] == "campaign_adaptation/7"


async def test_exhausted_parent_gap_keeps_operational_close_separate(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_drivers import LENIENT, _ParentExhaustsOnceOn

    adapter = _ParentExhaustsOnceOn()
    original = _Campaign.hook

    def arm(self, name):
        if name == "after_comparison_bound":
            adapter.on = True
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", arm)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        adapter=adapter,
        campaign_options={"indeterminate": LENIENT, "attempt_feedback": "sequential"},
    )
    records = _records(run / "campaign.jsonl")
    assert records[-1]["payload"]["reason"] == "incomplete_sample"
    from evolution.tree.campaign_projections import verify_evidence

    assert not verify_evidence(run)["mismatches"]
    assert not any(r["event"] in {"comparison_completed", "look_not_started"} for r in records)
    from evolution.tree.campaign import inspect_campaign

    attempts = inspect_campaign(run).to_payload()["independent"]["attempts"]
    assert any(a["outcome"] == "insufficient_evidence" for a in attempts)


@pytest.mark.parametrize("drain", ["active", "pre_stage_and_unused_slot"])
async def test_parallel_crossing_defers_scored_side_to_threshold_barrier(
    tmp_path, monkeypatch, drain
):
    import asyncio

    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.campaign_statistics import thresholds_reached
    from tests.campaign_fakes import FakeAdapter

    crossed = asyncio.Event()
    second_started = asyncio.Event()
    original_stage = _Campaign.run_stage
    original_editor = _Campaign.run_editor
    original_account = _Campaign.account

    class FirstAttemptFails(FakeAdapter):
        failing = True

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if (
                self.failing
                and candidate.kind == "candidate"
                and candidate.artifact_id == "r01-a01"
            ):
                instance._lookup = lambda session: "target_failed"
            return instance

    def account(self, mid, rows):
        original_account(self, mid, rows)
        if thresholds_reached(self.snapshot(), self.policy):
            crossed.set()

    async def stage(self, mid, number):
        if mid == "m-r01-a01" and drain == "active":
            await asyncio.wait_for(second_started.wait(), 5)
        if mid == "m-r01-a02":
            second_started.set()
            await asyncio.wait_for(crossed.wait(), 5)
        return await original_stage(self, mid, number)

    async def editor(self, attempt_id):
        if attempt_id == "r01-a02" and drain != "active":
            await asyncio.wait_for(crossed.wait(), 5)
        return await original_editor(self, attempt_id)

    monkeypatch.setattr(_Campaign, "run_editor", editor)
    monkeypatch.setattr(_Campaign, "account", account)
    monkeypatch.setattr(_Campaign, "run_stage", stage)
    run = await asyncio.wait_for(
        gate_run(
            tmp_path,
            monkeypatch,
            stop="result",
            adapter_class=FirstAttemptFails,
            campaign_options={
                "parallel_attempts": 2,
                "attempt_feedback": "none",
                "attempts_per_round": 2 if drain == "active" else 3,
            },
        ),
        15,
    )
    records = _records(run / "campaign.jsonl")
    assert crossed.is_set()
    assert sum(r["event"] == "attempt_opened" for r in records) == (2 if drain == "active" else 3)
    assert sum(r["event"] == "campaign_suspended" for r in records) == 1
    assert records[-1]["event"] == "campaign_suspended"
    assert records[-1]["payload"]["reason"] == "target_unstable"
    assert not any(
        r["event"] in {"gate_measurement_completed", "comparison_completed"} for r in records
    )
    second = _records(run / "measurements" / "m-r01-a02" / "events.jsonl")
    assert sum(r["event"] == "scoring_finished" for r in second) == 2
    assert not verify_evidence(run)["mismatches"]
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]

    # The parked exemption is earned from real complete bytes only at a crossing.
    from dataclasses import replace

    from evolution.tree import campaign_projections as projections

    checked = []
    original_parked = projections._parked_scored_at
    original_rows = projections.latest_rows

    def check_parked(context, index, attempt, point):
        result = original_parked(context, index, attempt, point)
        if result:
            assert not original_parked(context, index, attempt, replace(point, crossed=False))
            with monkeypatch.context() as patch:
                patch.setattr(
                    projections, "latest_rows", lambda *a, **kw: original_rows(*a, **kw)[:-1]
                )
                assert not original_parked(context, index, attempt, point)
            checked.append(index)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(projections, "_parked_scored_at", check_parked)
        assert not verify_evidence(run)["mismatches"]
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]
    assert checked

    # A real repair resume publishes the parked side without replaying its paid slots.
    FirstAttemptFails.failing = False
    run = await gate_run(
        tmp_path,
        monkeypatch,
        adapter_class=FirstAttemptFails,
        campaign_options={
            "parallel_attempts": 2,
            "attempt_feedback": "none",
            "attempts_per_round": 2 if drain == "active" else 3,
        },
    )
    assert not verify_evidence(run)["mismatches"]
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]
    assert sum(r["event"] == "campaign_suspended" for r in _records(run / "campaign.jsonl")) == 1
    second = _records(run / "measurements" / "m-r01-a02" / "events.jsonl")
    assert sum(r["event"] == "scoring_finished" for r in second) == 2
    assert any(
        r["event"] == "gate_measurement_completed"
        and r["payload"]["source"]["measurement_id"] == "m-r01-a02"
        for r in _records(run / "campaign.jsonl")
    )


@pytest.mark.parametrize(
    "immediate",
    [
        {"attempt_feedback": "sequential"},
        {"attempt_feedback": "none", "round_stop": "first_accepted"},
    ],
    ids=["sequential", "first_accepted"],
)
async def test_immediate_threshold_resume_settles_owner_before_next_attempt(
    tmp_path, monkeypatch, immediate
):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.campaign_statistics import thresholds_reached
    from tests.campaign_fakes import FakeAdapter

    class CandidateFails(FakeAdapter):
        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "candidate":
                instance._lookup = lambda session: "target_failed"
            return instance

    original = _Campaign.finish_stage
    cut = False

    def finish_stage(self, mid, *args, **kwargs):
        nonlocal cut
        outcome = original(self, mid, *args, **kwargs)
        if mid == "m-r01-a01" and not cut:
            assert thresholds_reached(self.snapshot(), self.policy)
            cut = True
            raise GatePowerCut()
        return outcome

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "finish_stage", finish_stage)
        run = await gate_run(
            tmp_path,
            patch,
            adapter_class=CandidateFails,
            campaign_options=immediate,
            stop="after_durable_accounting",
            hard=True,
        )
    before = _records(run / "campaign.jsonl")
    assert cut
    bound_policy = before[0]["payload"]["identity"]["policy"]
    assert all(bound_policy[key] == value for key, value in immediate.items())
    assert not any(r["event"] == "campaign_suspended" for r in before)
    measurement = run / "measurements" / "m-r01-a01" / "events.jsonl"
    before_bytes = measurement.read_bytes()
    role_starts = [r for r in before if r["event"] in {"attempt_opened", "editor_started"}]

    run = await gate_run(
        tmp_path,
        monkeypatch,
        adapter_class=CandidateFails,
        campaign_options=immediate,
        stop="result",
    )
    after = _records(run / "campaign.jsonl")
    suspensions = [r for r in after if r["event"] == "campaign_suspended"]
    assert len(suspensions) == 1
    assert suspensions[0]["payload"]["reason"] == "target_unstable"
    assert [r for r in after if r["event"] in {"attempt_opened", "editor_started"}] == role_starts
    assert measurement.read_bytes() == before_bytes
    assert not any(
        r["event"] in {"gate_measurement_completed", "comparison_completed"} for r in after
    )
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]


@pytest.mark.parametrize(
    "immediate",
    [
        {"attempt_feedback": "sequential"},
        {"attempt_feedback": "none", "round_stop": "first_accepted"},
    ],
    ids=["sequential", "first_accepted"],
)
async def test_recovered_target_retry_suspends_before_independent_comparison(
    tmp_path, monkeypatch, immediate
):
    """A threshold counts a failed try even when its sample retry later scores."""

    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import STRICT_TARGET
    from tests.campaign_fakes import FakeAdapter

    class CandidateFailsOnce(FakeAdapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failed = set()

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "candidate":

                def lookup(session):
                    key = (session.case_id, session.requested_seed)
                    if key not in self.failed:
                        self.failed.add(key)
                        return "target_failed"
                    return "completed"

                instance._lookup = lookup
            return instance

    run = await gate_run(
        tmp_path,
        monkeypatch,
        adapter_class=CandidateFailsOnce,
        campaign_options={**immediate, **STRICT_TARGET},
        stop="result",
    )
    records = _records(run / "campaign.jsonl")
    assert records[-1]["event"] == "campaign_suspended"
    assert records[-1]["payload"]["reason"] == "target_unstable"
    assert not any(record["event"] == "comparison_completed" for record in records)
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("repeats", [1, 3])
async def test_focus_first_stage_one_dispatches_only_focus_rows(tmp_path, monkeypatch, repeats):
    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.campaign_projections import inspect_view
    from evolution.tree.campaign_view import DecideFocus, next_actions

    run = await gate_run(
        tmp_path,
        monkeypatch,
        repeats=repeats,
        gate_options={"stages": "focus_first"},
        stop="after_focus_stage",
    )
    records = _records(run / "campaign.jsonl")
    attempt = next(r["payload"] for r in records if r["event"] == "comparison_bound")
    mid = attempt["candidate"]["measurement_id"]
    dispatched = [
        r["payload"]
        for r in _records(run / "measurements" / mid / "events.jsonl")
        if r["event"] == "rollout_started"
    ]
    assert {r["case_id"] for r in dispatched} == {"c1"}
    assert len(dispatched) == repeats
    opened = [
        r["payload"]
        for r in records
        if r["event"] == "stage_opened" and r["payload"]["measurement_id"] == mid
    ]
    assert len(opened) == 1 and opened[0]["scope"] == "focus"
    assert opened[0]["look"] == repeats and opened[0]["sample_count"] == repeats
    view = inspect_view(run)
    policy = CampaignPolicyV12.from_payload(view.snapshot.identity["policy"])
    assert any(isinstance(action, DecideFocus) for action in next_actions(view, policy))

    from evolution.tree.campaign_projections import verify_evidence

    assert verify_evidence(run)["mismatches"] == []


async def test_fresh_parent_stage_one_mirrors_candidate(tmp_path, monkeypatch):
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        gate_options={"stages": "focus_first"},
        stop="after_focus_stage",
    )
    records = _records(run / "campaign.jsonl")
    bound = next(r["payload"] for r in records if r["event"] == "comparison_bound")
    sides = [bound[side]["measurement_id"] for side in ("candidate", "reference")]
    opened = [
        r["payload"]
        for r in records
        if r["event"] == "stage_opened" and r["payload"]["measurement_id"] in sides
    ]
    assert [s["measurement_id"] for s in opened] == sides
    assert all(
        (s["stage"], s["look"], s["scope"], s["sample_count"]) == (1, 1, "focus", 1) for s in opened
    )
    for mid in sides:
        calls = [
            r["payload"]
            for r in _records(run / "measurements" / mid / "events.jsonl")
            if r["event"] == "rollout_started"
        ]
        assert [r["case_id"] for r in calls] == ["c1"]

    from evolution.tree.campaign_projections import verify_evidence

    assert verify_evidence(run)["mismatches"] == []


async def test_single_plans_unchanged(tmp_path, monkeypatch):
    from evolution.tree.execution.journal_identity import canonical_json

    stages = []
    digests = []
    for name, options in (("absent", {}), ("single", {"stages": "single"})):
        root = tmp_path / name
        root.mkdir()
        run = await gate_run(root, monkeypatch, gate_options=options)
        records = _records(run / "campaign.jsonl")
        stage_payloads = [r["payload"] for r in records if r["event"] == "stage_opened"]
        assert all("scope" not in stage for stage in stage_payloads)
        stages.append(canonical_json(stage_payloads))
        digests.append(records[0]["payload"]["identity"]["policy_digest"])
    assert stages[0] == stages[1]
    assert digests[0] == digests[1]


def test_partition_by_scope_preserves_all_repeats_and_frozen_order():
    from evolution.tree.campaign_measurement import _partition_by_scope

    rows = [
        dict(case_id=case, sample_id=f"s{i}", repeat=i, requested_seed=42)
        for i in (1, 2, 3)
        for case in ("c1", "c2")
    ]
    focus, causal = _partition_by_scope(3, rows, frozenset({"c1"}), frozenset({"c2"}))
    assert (focus.stage, focus.look, focus.scope) == (1, 3, "focus")
    assert (causal.stage, causal.look, causal.scope) == (2, 3, "causal")
    assert focus.rows == tuple(rows[::2])
    assert causal.rows == tuple(rows[1::2])


@pytest.mark.parametrize("outcome", ["passed", "comparison_not_passed"])
def test_focus_decision_selects_the_next_scope_action(observe_barrier, outcome):  # noqa: F811
    from evolution.tree.campaign_view import (
        CampaignView,
        DecideFocus,
        EvaluateLook,
        RunAttemptStage,
        _independent_action,
    )
    from tests.family15_fakes import Family15Life
    from tests.test_tree_campaign_family15_lifecycle import focus_stage

    life = Family15Life.focus_first(observe_barrier)
    life.bind_gate()
    focus_stage(life)
    life.decide_focus(outcome)
    attempt = life.attempt(life.sealed)
    mid = attempt.measurement_id
    disposition = dict(kind="score", outcome="completed", owner=None, reason=None)
    samples = [
        dict(
            case_id=c,
            candidate_digest=attempt.candidate_digest,
            sample_id="01",
            execution=dict(disposition) if c == "c1" else None,
            scoring_state="finished" if c == "c1" else "not_started",
            critical_pass=True if c == "c1" else None,
            execution_state="finished" if c == "c1" else "not_started",
            tries=1 if c == "c1" else 0,
            evaluation=dict(disposition) if c == "c1" else None,
            metrics={"score": 1} if c == "c1" else None,
        )
        for c in ("c1", "c2")
    ]

    def view():
        return CampaignView(life.snapshot, {mid: {"samples": samples}}, None, {})

    action = _independent_action(view(), life.state.policy, attempt)
    if outcome != "passed":
        assert action == DecideFocus(attempt.attempt_id)
        return
    assert action == RunAttemptStage(attempt.attempt_id, 2)
    life.accept("stage_opened", measurement_id=mid, stage=2, look=1, sample_count=1, scope="causal")
    samples[1].update(
        execution_state="finished",
        execution=dict(disposition),
        tries=1,
        scoring_state="finished",
        evaluation=dict(disposition),
        metrics={"score": 1},
        critical_pass=True,
    )
    assert _independent_action(
        view(), life.state.policy, life.attempt(life.sealed)
    ) == EvaluateLook(attempt.attempt_id, 1)


def comparison_body(run, event="comparison_completed"):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.family15_audit import authenticate_auxiliary

    payload = next(r["payload"] for r in _records(run / "campaign.jsonl") if r["event"] == event)
    return authenticate_auxiliary(run, AuxiliaryPublication.from_payload(payload["data"]))


@pytest.mark.parametrize("fresh", [False, True])
async def test_focus_rejection_dispatches_no_causal_row(tmp_path, monkeypatch, fresh):
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import TWO_CASE_SCORER

    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=fresh,
        passing=False,
        gate_options={"stages": "focus_first"},
        scorer=TWO_CASE_SCORER.replace(
            '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
        ),
    )
    records = _records(run / "campaign.jsonl")
    bound = next(r["payload"] for r in records if r["event"] == "comparison_bound")
    for side in ("candidate", "reference") if fresh else ("candidate",):
        rows = _records(run / "measurements" / bound[side]["measurement_id"] / "events.jsonl")
        assert {r["payload"]["case_id"] for r in rows if r["event"] == "rollout_started"} == {"c1"}
    events = [r["event"] for r in records]
    assert (
        events.index("focus_decided")
        < events.index("comparison_completed")
        < events.index("attempt_closed")
    )
    result = comparison_body(run)
    assert result["scopes"][1]["status"] == "not_measured"
    assert result["deciding_scope"] == "focus"
    assert records[-1]["payload"]["outcome"] == "rejected"
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_focus_pass_measures_causal_and_reaches_finalist(tmp_path, monkeypatch, verification):
    from evolution.tree.campaign_projections import verify_evidence

    digests = []
    for stages in ("single", "focus_first"):
        run = await gate_run(
            tmp_path / stages,
            monkeypatch,
            gate_options={"stages": stages, "verification": verification},
            stop="after_candidate_gate_sealed",
        )
        records = _records(run / "campaign.jsonl")
        assert (
            next(r["payload"] for r in records if r["event"] == "attempt_closed")["outcome"]
            == "finalist"
        )
        seal = next(r["payload"] for r in records if r["event"] == "candidate_gate_sealed")
        digests.append(seal["slots_digest"])
        assert not verify_evidence(run)["mismatches"]
    assert digests[0] == digests[1]


@pytest.mark.parametrize("mode", ["point", "confidence", "unfiltered", "statistical"])
async def test_focus_first_equivalence_numbers_and_outcome_match_single(
    tmp_path, monkeypatch, mode
):
    from evolution.tree.campaign_projections import verify_evidence

    outcomes = []
    results = []
    for stages in ("single", "focus_first"):
        run = await gate_run(
            tmp_path / stages, monkeypatch, mode=mode, repeats=2, gate_options={"stages": stages}
        )
        outcomes.append(_records(run / "campaign.jsonl")[-1]["payload"]["outcome"])
        result = comparison_body(run)
        results.append(
            {
                key: result[key]
                for key in (
                    "scopes",
                    "coverage",
                    "retained_pairs",
                    "repeat_diagnostics",
                    "variation",
                    "outcome",
                    "reason",
                    "deciding_scope",
                )
            }
        )
        assert not verify_evidence(run)["mismatches"]
    assert outcomes == ["finalist", "finalist"]
    assert results[0] == results[1]


@pytest.mark.parametrize("cut", ["after_focus_decided", "after_causal_stage_opened"])
async def test_focus_first_crash_resumes_without_redeciding(tmp_path, monkeypatch, cut):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence

    original = _Campaign._append_stage_opened

    def stage_opened(self, mid, plan):
        original(self, mid, plan)
        if cut == "after_causal_stage_opened" and plan.scope == "causal":
            raise GatePowerCut()

    options = dict(gate_options={"stages": "focus_first"}, fresh=True)
    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "_append_stage_opened", stage_opened)
        await gate_run(tmp_path, patch, stop=cut, hard=True, **options)
    run = tmp_path / "run"
    decision = next(r for r in _records(run / "campaign.jsonl") if r["event"] == "focus_decided")
    await gate_run(tmp_path, monkeypatch, **options)
    records = _records(run / "campaign.jsonl")
    assert [r for r in records if r["event"] == "focus_decided"] == [decision]
    assert records[-1]["payload"]["outcome"] == "finalist"
    assert not verify_evidence(run)["mismatches"]


async def test_fresh_parent_measured_noise_calibrates_after_causal_stage(tmp_path, monkeypatch):
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        repeats=2,
        gate_options={"stages": "focus_first", "causal": {"tolerance": "measured_noise"}},
    )
    focus = comparison_body(run, "focus_decided")
    assert "calibration" not in focus
    assert comparison_body(run)["calibration"]["source"] == "fresh"
    from evolution.tree.campaign_projections import verify_evidence

    assert not verify_evidence(run)["mismatches"]


async def test_focus_first_empty_causal_one_stage(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence
    from tests import campaign_drivers as fixtures

    original = fixtures.two_case_manifest

    def partitioned(*args, rows, **kwargs):
        rows = [
            {
                **row,
                "group_id": row["case_id"],
                "split": "test" if row["case_id"] == "c2" else "train",
            }
            for row in rows
        ]
        return original(*args, rows=rows, **kwargs)

    monkeypatch.setattr(fixtures, "two_case_manifest", partitioned)
    run = await gate_run(tmp_path, monkeypatch, gate_options={"stages": "focus_first"})
    records = _records(run / "campaign.jsonl")
    assert records[-1]["payload"]["outcome"] == "finalist"
    assert comparison_body(run)["scopes"][1]["status"] == "skipped"
    assert not any(
        r["event"] == "stage_opened" and r["payload"].get("scope") == "causal" for r in records
    )
    assert not verify_evidence(run)["mismatches"]


async def test_incomplete_focus_is_insufficient_evidence(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import LENIENT, _ParentExhaustsOnceOn

    adapter = _ParentExhaustsOnceOn()
    original = _Campaign.hook

    def arm(self, name):
        if name == "after_comparison_bound":
            adapter.on = True
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", arm)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        adapter=adapter,
        gate_options={"stages": "focus_first"},
        campaign_options={"indeterminate": LENIENT, "attempt_feedback": "sequential"},
    )
    records = _records(run / "campaign.jsonl")
    assert records[-1]["payload"]["outcome"] == "insufficient_evidence"
    assert records[-1]["payload"]["reason"] == "incomplete_sample"
    assert not any(r["event"] == "focus_decided" for r in records)
    assert not any(
        r["event"] == "stage_opened" and r["payload"].get("scope") == "causal" for r in records
    )
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("mode", ["point", "statistical"])
@pytest.mark.parametrize("cut", ["after_focus_stage", "after_focus_decided"])
async def test_focus_first_continue_preserves_decision_barrier(tmp_path, monkeypatch, mode, cut):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable

    adapter = _Resettable()
    options = dict(
        adapter=adapter, mode=mode, repeats=2, fresh=True, gate_options={"stages": "focus_first"}
    )
    run = await gate_run(tmp_path, monkeypatch, stop=cut, hard=True, **options)
    decisions = [r for r in _records(run / "campaign.jsonl") if r["event"] == "focus_decided"]
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await gate_run(tmp_path, monkeypatch, **options)
    await gate_run(tmp_path, monkeypatch, authorizations=[_continue(refused.value)], **options)
    records = _records(run / "campaign.jsonl")
    after = [r for r in records if r["event"] == "focus_decided"]
    assert len(after) == 1
    if decisions:
        assert after == decisions
        binding = [r for r in records if r["event"] == "comparison_bound"][-1]
        assert binding["payload"]["focus"]["seq"] == decisions[0]["sequence"]
        assert binding["payload"]["focus"]["outcome"] == "passed"
        import copy

        from evolution.tree.campaign_projections import _evidence_of
        from evolution.tree.campaign_schema import CampaignLifecycle
        from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
        from evolution.tree.family15_schema import FAMILY15_CONTRACT
        from evolution.tree.storage.envelope import Envelope, SchemaError

        context, _ = _evidence_of(run)
        for changed in ({"seq": 1}, {"digest": "f" * 64}, {"outcome": "comparison_not_passed"}):
            state = CampaignLifecycle(selector=None, family_version=15)
            for prior in records[: binding["sequence"] - 1]:
                state.accept(Envelope.from_payload(prior, family=FAMILY15_CONTRACT))
            bad = copy.deepcopy(binding)
            bad["payload"]["focus"].update(changed)
            with pytest.raises(SchemaError):
                state.accept(Envelope.from_payload(bad, family=FAMILY15_CONTRACT))
            if "outcome" not in changed:
                with pytest.raises(EvidenceMismatch, match="carried focus"):
                    Family15Audit(context).binding(
                        Envelope.from_payload(bad, family=FAMILY15_CONTRACT)
                    )
        for rebound in (r["payload"] for r in records if r["event"] == "measurement_rebound"):
            events = _records(run / "measurements" / rebound["measurement_id"] / "events.jsonl")
            assert all(
                r["payload"]["case_id"] == "c2" for r in events if r["event"] == "rollout_started"
            )
    assert records[-1]["payload"]["outcome"] == "finalist"
    assert not verify_evidence(run)["mismatches"]


async def test_crash_between_focus_decided_and_replacement_binding(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable

    adapter = _Resettable()
    options = dict(adapter=adapter, fresh=True, gate_options={"stages": "focus_first"})
    run = await gate_run(tmp_path, monkeypatch, stop="after_focus_decided", hard=True, **options)
    decision = next(r for r in _records(run / "campaign.jsonl") if r["event"] == "focus_decided")
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await gate_run(tmp_path, monkeypatch, **options)
    await gate_run(
        tmp_path,
        monkeypatch,
        stop="after_measurement_rebound",
        hard=True,
        authorizations=[_continue(refused.value)],
        **options,
    )
    await gate_run(tmp_path, monkeypatch, **options)
    assert [r for r in _records(run / "campaign.jsonl") if r["event"] == "focus_decided"] == [
        decision
    ]
    assert not verify_evidence(run)["mismatches"]


async def test_crash_before_focus_dispatch_replays_only_opened_reservations(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence

    original = _Campaign._append_stage_opened

    def cut(self, mid, plan):
        original(self, mid, plan)
        if plan.scope == "focus":
            raise GatePowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "_append_stage_opened", cut)
        await gate_run(
            tmp_path, patch, stop="unused", hard=True, gate_options={"stages": "focus_first"}
        )
    run = await gate_run(tmp_path, monkeypatch, gate_options={"stages": "focus_first"})
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("effect", ["continue", "rebaseline", "close"])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("cut", ["after_focus_decided", "after_gate_measurement_completed"])
async def test_rejected_focus_closes_before_authorized_change(
    tmp_path, monkeypatch, fresh, cut, effect
):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.execution.authorizations import Authorization
    from tests.campaign_drivers import TWO_CASE_SCORER, _Resettable

    adapter = _Resettable()
    options = dict(
        adapter=adapter,
        fresh=fresh,
        passing=False,
        gate_options={"stages": "focus_first"},
        campaign_options={"rounds": 2},
        scorer=TWO_CASE_SCORER.replace(
            '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
        ),
    )
    run = await gate_run(tmp_path, monkeypatch, stop=cut, hard=True, **options)
    before = _records(run / "campaign.jsonl")
    focus = next(r for r in before if r["event"] == "focus_decided")
    owned = next(r["payload"] for r in before if r["event"] == "comparison_bound")
    mids = {
        owned[side]["measurement_id"]
        for side in (("candidate", "reference") if fresh else ("candidate",))
    }
    adapter.settings = "settings-v2"
    if effect == "rebaseline":
        options["gate_options"] = {"stages": "single"}  # A policy change offers rebaseline/close.
    with pytest.raises(EvolveRefused) as refused:
        await gate_run(tmp_path, monkeypatch, **options)
    assert effect in refused.value.effects, refused.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        refused.value.instance_digest,
        "Apply the changed target",
        now="2026-10-28T10:00:00Z",
        effect=effect,
    )
    await gate_run(
        tmp_path,
        monkeypatch,
        authorizations=[decision],
        stop="result" if effect == "close" else "after_condition_applied",
        hard=effect != "close",
        **options,
    )
    records = _records(run / "campaign.jsonl")
    closed = next(i for i, r in enumerate(records) if r["event"] == "attempt_closed")
    applied = next(i for i, r in enumerate(records) if r["event"] == "authorization_applied")
    assert closed < applied
    assert records[closed]["payload"]["outcome"] == "rejected"
    assert [r for r in records if r["event"] == "focus_decided"] == [focus]
    assert comparison_body(run)["scopes"][1]["status"] == "not_measured"
    # Finish the interrupted authorized block using only its durable authority.
    if effect == "close":
        adapter.settings = "settings-v1"  # A finished close retains the original identity.
    await gate_run(tmp_path, monkeypatch, stop="result", **options)
    records = _records(run / "campaign.jsonl")
    assert not any(
        r["event"] == "measurement_rebound" and r["payload"]["from_measurement_id"] in mids
        for r in records
    )
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize("fresh", [False, True])
async def test_rejected_focus_continue_preserves_other_parallel_attempt(
    tmp_path, monkeypatch, fresh
):
    from evolution.tree.campaign_projections import inspect_view, verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import TWO_CASE_SCORER, _continue, _Resettable

    adapter = _Resettable()
    options = dict(
        adapter=adapter,
        fresh=fresh,
        passing=False,
        gate_options={"stages": "focus_first"},
        campaign_options={"parallel_attempts": 2, "attempt_feedback": "none"},
        scorer=TWO_CASE_SCORER.replace(
            '"score": 1.0 if passed else 0.0', '"score": 0.25 if passed else 0.0'
        ),
    )
    run = await gate_run(tmp_path, monkeypatch, stop="after_focus_decided", hard=True, **options)
    attempts = inspect_view(run).snapshot.rounds[1].attempts
    assert len(attempts) == 2
    assert any(
        a.measurement_id is not None and a.family15.focus_decision is None
        for a in attempts.values()
    )
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await gate_run(tmp_path, monkeypatch, **options)
    await gate_run(
        tmp_path, monkeypatch, stop="result", authorizations=[_continue(refused.value)], **options
    )
    assert all(
        a.outcome is not None for a in inspect_view(run).snapshot.rounds[1].attempts.values()
    )
    assert not verify_evidence(run)["mismatches"]


async def test_incomplete_closure_real_evolve_finishes_readably(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign, inspect_campaign
    from evolution.tree.evolve import evolve, exit_code_for
    from tests.campaign_drivers import LENIENT, _ParentExhaustsOnceOn

    adapter = _ParentExhaustsOnceOn()
    original = _Campaign.hook

    def arm(self, name):
        if name == "after_comparison_bound":
            adapter.on = True
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", arm)
    run = await gate_run(
        tmp_path,
        monkeypatch,
        fresh=True,
        adapter=adapter,
        campaign_options={"indeterminate": LENIENT, "attempt_feedback": "sequential"},
    )
    assert _records(run / "campaign.jsonl")[-1]["payload"]["reason"] == "incomplete_sample"
    result = await evolve(tmp_path / "manifest.json", run, adapter_factory=lambda *a: adapter)
    assert result.terminal == "finished" and result.finished["outcome"] == "completed"
    assert exit_code_for(result) == 0
    assert result.result is not None
    attempts = result.result.to_payload()["independent"]["attempts"]
    assert ("insufficient_evidence", "incomplete_sample") in {
        (a["outcome"], a["reason"]) for a in attempts
    }
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]


@pytest.mark.parametrize("stages,release", [("single", 2), ("focus_first", 1)])
async def test_rebound_release_audit_is_independent_of_executor(
    tmp_path, monkeypatch, stages, release
):
    from evolution.tree import campaign
    from evolution.tree.campaign_measurement import FrozenMeasurement
    from evolution.tree.campaign_projections import inspect_view, verify_evidence
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import _continue, _Resettable

    original = campaign._Campaign._append_stage_opened

    def cut(self, mid, plan):
        original(self, mid, plan)
        if self.snapshot().measurements[mid].get("role") == "candidate":
            raise GatePowerCut()

    adapter = _Resettable()
    options = dict(adapter=adapter, gate_options={"stages": stages})
    with monkeypatch.context() as patch:
        patch.setattr(campaign._Campaign, "_append_stage_opened", cut)
        run = await gate_run(tmp_path, patch, stop="stage_cut", hard=True, **options)
    snapshot = inspect_view(run).snapshot
    mid = snapshot.rounds[1].attempts["r01-a01"].measurement_id

    def wrong_owner(*args, **kwargs):
        pytest.fail("executor reconstructed focus instead of using owed_rows")

    with monkeypatch.context() as patch:
        patch.setattr(campaign, "confidence_scopes", wrong_owner)
        assert campaign._unfilled_slots(snapshot, mid, FrozenMeasurement(run, mid), [1]) == release
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refused:
        await gate_run(tmp_path, monkeypatch, **options)
    await gate_run(tmp_path, monkeypatch, authorizations=[_continue(refused.value)], **options)
    rebounds = [
        r["payload"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] == "measurement_rebound"
    ]
    assert rebounds and all(r["released"] == release for r in rebounds)

    def writer_forbidden(*args, **kwargs):
        pytest.fail("audit called executor reservation accounting")

    monkeypatch.setattr(campaign, "_unfilled_slots", writer_forbidden)
    assert not verify_evidence(run)["mismatches"]


@pytest.mark.parametrize(
    "disposition,metrics,ready",
    [
        ("score", {"score": 0}, True),
        ("score", {"score": 1}, True),
        ("score", {"score": -0.01}, False),
        ("score", {"score": 1.01}, False),
        ("score", {"score": float("nan")}, False),
        ("score", {"other": 1}, False),
        ("score", None, False),
        ("exclude", {"score": 1}, False),
        ("indeterminate", {"score": 1}, False),
    ],
)
def test_focus_planner_requires_kernel_admissibility(observe_barrier, disposition, metrics, ready):  # noqa: F811
    from evolution.tree.campaign_view import CampaignView, DecideFocus, _independent_action
    from tests.family15_fakes import Family15Life
    from tests.test_tree_campaign_family15_lifecycle import focus_stage

    life = Family15Life.focus_first(observe_barrier)
    life.bind_gate()
    focus_stage(life)
    attempt = life.attempt(life.sealed)
    sample = dict(
        case_id="c1",
        sample_id="01",
        execution_state="finished",
        tries=(life.state.policy.sample_retries or 0) + 1,
        execution=dict(kind="score"),
        evaluation=dict(kind=disposition),
        scoring_state="finished",
        metrics=metrics,
    )
    view = CampaignView(life.snapshot, {attempt.measurement_id: {"samples": [sample]}}, None, {})
    action = _independent_action(view, life.state.policy, attempt)
    assert action == (DecideFocus(attempt.attempt_id) if ready else None)
