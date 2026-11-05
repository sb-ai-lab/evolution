"""Family-15 pure ordering uses admitted journal prefixes, never seeded state."""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
import copy
import hashlib

import pytest

from evolution.tree.campaign_schema import SchemaError
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import observe_barrier  # noqa: F401
from tests.family15_fakes import v16_document
from tests.tree.manifest.test_manifest_v15 import load


def life15(records, *, early=False, **settings):
    from tests.family15_fakes import Family15Life

    return Family15Life(records, early=early, **settings)


def test_early_global_has_budgeted_attempt_without_candidate(observe_barrier):
    life = life15(observe_barrier, early=True)
    life.early_attempt()
    assert len(life.snapshot.rounds[1].attempts) == 1
    attempt = life.attempt("r1-a1")
    assert attempt.candidate_digest is None
    assert attempt.measurement_id is None
    assert life.snapshot.alpha_rounds == {}
    assert life.snapshot.rounds[1].gate_scope is None
    with pytest.raises(SchemaError):
        life.early_attempt(index=2)


def test_preflight_reference_must_exist_and_rejected_prepare_is_atomic(observe_barrier):
    life = life15(observe_barrier, early=True)
    life.early_attempt()
    payload = life.preflight_payload()
    before = life.snapshot
    broken = copy.deepcopy(payload)
    broken["data"]["declaration"]["declaration_ref"]["content_digest"] = "f" * 64
    with pytest.raises(SchemaError):
        life.accept("comparison_preflight_failed", **broken)
    assert life.snapshot is before
    seq = life.accept("comparison_preflight_failed", **payload)
    life.accept(
        "attempt_closed",
        attempt_id="r1-a1",
        outcome="rejected",
        reason="structurally_unattainable",
        path="early_global",
        evidence=life.reference(seq),
    )
    assert life.attempt("r1-a1").outcome == "rejected"


def statistical_life(records, *, early=False, focus_first=False):
    records = copy.deepcopy(records)
    for row in records:
        if row["event"] == "stage_opened":
            row["payload"]["look"] = 2
    gate = v16_document()["campaign"]["gate"]
    gate.update(mode="statistical")
    if focus_first:
        gate["stages"] = "focus_first"
    gate["focus"]["confidence"] = gate["causal"]["confidence"] = 0.95
    evaluation = dict(
        records[0]["payload"]["identity"]["policy"]["evaluation"], repeats_per_question=2
    )
    return life15(records, early=early, gate=gate, evaluation=evaluation)


def test_statistical_lifecycle_admits_early_global_but_not_local_preflight(observe_barrier):
    life = statistical_life(observe_barrier, early=True)
    life.early_attempt()
    payload = life.preflight_payload() | {"mode": "statistical"}
    failed = life.accept("comparison_preflight_failed", **payload)
    life.accept(
        "attempt_closed",
        attempt_id="r1-a1",
        path="early_global",
        outcome="rejected",
        reason="structurally_unattainable",
        evidence=life.reference(failed),
    )
    assert life.attempt("r1-a1").reason == "structurally_unattainable"
    local = statistical_life(observe_barrier)
    payload = dict(payload, attempt_id=local.sealed, phase="attempt", data=local.data())
    before = local.snapshot
    with pytest.raises(SchemaError):
        local.accept("comparison_preflight_failed", **payload)
    assert local.snapshot is before


@pytest.mark.parametrize("reason", ["insufficient_repeats", "insufficient_calibration"])
def test_new_unavailability_retains_champion_and_closes_its_own_comparison(observe_barrier, reason):
    life = life15(observe_barrier)
    incumbent = life.snapshot.champion
    bound = life.bind_gate()
    life.accept(
        "stage_opened",
        measurement_id=life.attempt(life.sealed).measurement_id,
        stage=1,
        look=1,
        sample_count=2,
    )
    life.settle_gate_sides(bound)
    payload = life.gate_result_payload(bound) | {
        "outcome": "comparison_unavailable",
        "reason": reason,
    }
    result = life.accept("comparison_completed", **payload)
    life.accept(
        "attempt_closed",
        attempt_id=life.sealed,
        path="candidate",
        outcome="rejected",
        reason=reason,
        evidence=life.reference(result),
    )
    assert life.attempt(life.sealed).reason == reason
    assert life.snapshot.champion == incumbent


def test_projection_grammar_admits_new_numerical_shapes_only(observe_barrier):
    from evolution.tree.case_comparison import ComparisonResult
    from evolution.tree.family15_report import (
        campaign15_projection,
        comparison_projection,
        validate_campaign15_projection,
    )
    from tests.test_tree_campaign_family15_schema import (
        bootstrap_binding,
        bootstrap_result,
        calibration,
        q,
    )

    life = life15(observe_barrier)
    report = campaign15_projection(life.snapshot)
    before = canonical_json(report)
    validate_campaign15_projection(report)
    assert canonical_json(report) == before
    report["settings"].update(
        mode="statistical",
        method="f13-bootstrap/1",
        repeats=2,
        train_slots=2 * report["settings"]["train_cases"],
    )
    body = bootstrap_result()
    projected = comparison_projection(
        ComparisonResult(canonical_json(body)), profile=bootstrap_binding()
    )
    projected["caveats"] = body["caveats"]
    projected["scopes"][0].update({k: body["scopes"][0][k] for k in ("lower", "exact", "pairing")})
    report["attempts"] = [
        dict(
            round=1,
            attempt_id="a",
            outcome="rejected",
            reason="threshold_not_passed",
            comparison=None,
            gate_comparison=projected,
            global_comparison=None,
            preflight=None,
            conditional_cost=None,
            eligible_final_score=None,
        )
    ]
    validate_campaign15_projection(report)
    bad = copy.deepcopy(report)
    bad["attempts"][0]["gate_comparison"]["scopes"][0]["pairing"] = "unknown"
    with pytest.raises(SchemaError):
        validate_campaign15_projection(bad)
    report["settings"].update(mode="confidence", method="bounded-weighted-mean/1")
    report["settings"]["bound_fallback"] = "point"
    projected.update(mode="confidence", method="bounded-weighted-mean/1", caveats=[])
    scope = projected["scopes"][0]
    for key in ("lower", "exact", "pairing"):
        scope.pop(key)
    scope.update(
        rule="point",
        fallback={"reason": "focus_unattainable"},
        confidence=None,
        enclosure=None,
        threshold=q(),
    )
    validate_campaign15_projection(report)
    report["settings"].pop("bound_fallback")
    with pytest.raises(SchemaError):
        validate_campaign15_projection(report)
    report["settings"]["bound_fallback"] = "point"
    projected.update(calibration=calibration(), caveats=["noise_allowance_empirical"])
    validate_campaign15_projection(report)
    for reason in ("insufficient_repeats", "insufficient_calibration"):
        unavailable = copy.deepcopy(report)
        result = unavailable["attempts"][0]["gate_comparison"]
        result.update(outcome="comparison_unavailable", reason=reason, eligible=False)
        result["scopes"][0].update(status="unavailable", unavailable=reason, point=None)
        if reason == "insufficient_calibration":
            result["scopes"][0].pop("fallback")
            result.pop("calibration")
        validate_campaign15_projection(unavailable)
    for mutate in (
        lambda p: p["calibration"].update(result_digest="f" * 64),
        lambda p: p.update(caveats=["unknown"]),
        lambda p: p["scopes"][0]["fallback"].update(reason="causal_unconfirmable"),
    ):
        bad = copy.deepcopy(report)
        mutate(bad["attempts"][0]["gate_comparison"])
        with pytest.raises(SchemaError):
            validate_campaign15_projection(bad)


@pytest.mark.parametrize("fresh", [False, True])
def test_projection_keeps_focus_fallback_when_causal_calibration_is_unavailable(
    tmp_path,
    observe_barrier,
    fresh,
):
    from evolution.tree.case_comparison import ComparisonResult
    from evolution.tree.family15_report import (
        campaign15_projection,
        comparison_projection,
        validate_campaign15_projection,
    )
    from tests.test_tree_campaign_family15_schema import (
        focus_fallback_calibration_failure,
        gate_policy,
    )

    policy = gate_policy(tmp_path, fresh=fresh, tolerance="measured_noise")
    bound, value = focus_fallback_calibration_failure(policy)
    projected = comparison_projection(ComparisonResult(canonical_json(value)), profile=bound)
    # Task18 will emit this optional marker; this regression tests only the grammar.
    projected["scopes"][0]["fallback"] = copy.deepcopy(value["scopes"][0]["fallback"])
    report = campaign15_projection(life15(observe_barrier).snapshot)
    report["settings"].update(
        mode="confidence",
        bound_fallback="point",
        fresh_parent=fresh,
        method="bounded-weighted-mean/1",
        unit="case",
        repeats=2,
        train_slots=2 * report["settings"]["train_cases"],
    )
    report["attempts"] = [
        dict(
            round=1,
            attempt_id="a",
            outcome="rejected",
            reason="insufficient_calibration",
            comparison=projected,
            gate_comparison=projected,
            global_comparison=None,
            preflight=None,
            conditional_cost=None,
            eligible_final_score=None,
        )
    ]
    validate_campaign15_projection(report)
    assert projected["scopes"][0]["fallback"] == {"reason": "focus_unattainable"}
    assert projected["scopes"][1]["unavailable"] == "insufficient_calibration"
    # Unknown q still forbids deciding C by fallback, even with the valid focus marker.
    causal = projected["scopes"][1]
    causal.update(rule="point", fallback={"reason": "causal_unconfirmable"}, confidence=None)
    with pytest.raises(SchemaError):
        validate_campaign15_projection(report)


def test_local_gate_requires_binding_and_its_exact_result(observe_barrier):
    life = life15(observe_barrier)
    bound = life.bind_gate()
    result = life.gate_result_payload(bound)
    bad = copy.deepcopy(result)
    bad["binding"]["content_digest"] = "f" * 64
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **bad)
    assert life.snapshot is before
    life.accept(
        "stage_opened",
        measurement_id=life.attempt(life.sealed).measurement_id,
        stage=1,
        look=1,
        sample_count=2,
    )
    life.settle_gate_sides(bound)
    seq = life.accept("comparison_completed", **result)
    life.close_gate(seq)
    assert life.attempt(life.sealed).outcome == "finalist"
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **result)


@pytest.mark.parametrize("reason", ["no_evaluable_focus", "no_evaluable_pairs"])
async def test_inherited_no_look_close_keeps_native_family15_replay_and_audit(
    tmp_path, monkeypatch, reason
):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for, reduce
    from evolution.tree.storage.envelope import Envelope
    from tests.campaign_drivers import _records
    from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

    # A genuine /16 writer prefix, cut before any comparison or Candidate work.
    # Its inherited no-path close is not a family-14 confidence comparison close.
    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, cut="after_affected_cases_selected")
    run = tmp_path / "run"
    journal = run / "campaign.jsonl"
    prefix = journal.read_bytes()
    rows = _records(journal)
    assert rows[0]["payload"]["family_version"] == 15
    assert rows[-1]["event"] == "affected_cases_selected"
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []

    family = campaign_family_for(15)
    envelopes = [Envelope.from_payload(row, family=family) for row in rows]
    state = CampaignLifecycle(selector=None, family_version=15)
    for envelope in envelopes:
        state.accept(envelope)
    before = state.snapshot
    attempt = next(iter(before.rounds[1].attempts.values()))
    assert attempt.comparison is None
    assert attempt.family15 is not None and attempt.family15.comparison_binding is None
    assert attempt.outcome is None and attempt.looks == {}
    payload = dict(
        attempt_id=attempt.attempt_id,
        outcome="insufficient_evidence",
        reason=reason,
        look=None,
        code=None,
    )
    close = Envelope.from_payload(
        {
            **rows[-1],
            "sequence": len(rows) + 1,
            "event": "attempt_closed",
            "operation_id": None,
            "payload": payload,
        },
        family=family,
    )
    assert "path" not in close.payload
    transition = state.prepare(close)
    assert state.snapshot is before
    transition.commit()
    closed = state.snapshot.rounds[1].attempts[attempt.attempt_id]
    assert (closed.outcome, closed.reason, closed.look) == ("insufficient_evidence", reason, None)
    assert closed.comparison is None and closed.looks == {}

    replayed = reduce([*envelopes, close], selector=None, family_version=15)
    assert replayed.rounds[1].attempts[attempt.attempt_id] == closed
    with journal.open("ab") as stream:
        stream.write(close.encode())
    assert journal.read_bytes() == prefix + close.encode()
    assert verify_evidence(run)["mismatches"] == []
    report = inspect_campaign(run, verify=True).to_payload()
    assert report["verification"]["mismatches"] == []
    projected = next(
        a for a in report["independent"]["attempts"] if a["attempt_id"] == attempt.attempt_id
    )
    assert (projected["outcome"], projected["reason"]) == ("insufficient_evidence", reason)


def test_all_attempts_barrier_precedes_completion(observe_barrier):
    life = life15(observe_barrier)
    life.finish_gate()
    with pytest.raises(SchemaError):
        life.bind_completion()
    life.bind_finalists()
    life.bind_completion()
    assert life.attempt(life.sealed).family15.completion_binding is not None


def test_unstable_champion_binding_precedes_completion(observe_barrier):
    life = life15(observe_barrier, stability="unstable")
    life.finish_gate()
    life.bind_finalists()
    with pytest.raises(SchemaError):
        life.bind_completion()
    life.bind_champion()
    life.bind_completion()


def test_policy_fixture_is_point_single_repeat(tmp_path):
    # This fixture is explicit new-family admission, not old-family look semantics.
    policy = load(tmp_path, v16_document()).workflow.campaign
    assert policy.repeats == 1
    assert policy.gate_mode == "point"


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
@pytest.mark.parametrize("stability", ["stable", "unstable"])
@pytest.mark.parametrize("round_stop", ["first_accepted", "all_attempts"])
def test_final_combinations_complete_owned_evidence(
    observe_barrier, verification, stability, round_stop
):
    life = life15(
        observe_barrier, verification=verification, stability=stability, round_stop=round_stop
    )
    life.finish_final()
    life.select()
    selected = life.snapshot.rounds[1].selection
    assert selected["measurement_id"] == (
        "completion" if verification == "reuse" else "fresh-candidate"
    )
    assert life.snapshot.measurements[selected["measurement_id"]]["complete"]


@pytest.mark.parametrize("accepted", [True, False])
def test_closure_keeps_exact_selection_and_next_evidence(observe_barrier, accepted):
    life = life15(observe_barrier, stability="unstable")
    life.finish_final(accepted=accepted)
    life.select()
    selected = life.snapshot.rounds[1].selection
    assert selected["measurement_id"] == ("completion" if accepted else "fresh-champion")
    life.close_round()
    assert life.snapshot.rounds[1].closed["outcome"] == ("promoted" if accepted else "retained")


@pytest.mark.parametrize("effect", ["rebaseline", "close"])
def test_settled_eligible_evidence_does_not_block_an_authorized_transition(observe_barrier, effect):
    # The ordinary pre-selection rule: settled evidence owes no selection first.
    life = life15(observe_barrier, rounds=2)
    life.finish_final()
    assert life.snapshot.rounds[1].selection is None
    if effect == "rebaseline":
        life.continue_block(effect=effect)
        assert life.snapshot.pending_change["phase"] == "close_round"
    else:
        # The fixture's later rebase is refused: an applied close owes only its terminal.
        with pytest.raises(SchemaError, match="only the owed terminal"):
            life.continue_block(effect=effect)
        assert life.envelopes[-1].event == "authorization_applied"
        assert life.envelopes[-1].payload["effect"] == "close"
    assert life.snapshot.rounds[1].selection is None


def test_partial_final_evidence_allows_applied_transition_recovery(observe_barrier):
    life = life15(observe_barrier, rounds=2)
    life.finish_gate()
    life.bind_finalists()
    life.bind_completion()
    life.continue_block(effect="rebaseline")
    assert life.snapshot.pending_change["phase"] == "close_round"
    assert life.snapshot.epochs[-1]["target"]["settings_hash"] == "settings-2"


def test_continue_rebinds_unfinished_completion_and_keeps_completed_gate(observe_barrier):
    life = life15(observe_barrier)
    life.finish_gate()
    life.bind_finalists()
    binding = life.bind_completion()
    gate = life.attempt(life.sealed).family15.gate_seal
    moved = life.continue_block(["completion"], imported=2)
    assert moved == [("completion", "completion@e1")]
    assert life.attempt(life.sealed).family15.gate_seal == gate
    assert life.snapshot.measurements[gate.payload["source"]["measurement_id"]]["complete"]
    life.accept(
        "candidate_completion_completed",
        round=1,
        attempt_id=life.sealed,
        binding=life.reference(binding),
        source=life.source("completion@e1"),
        required=2,
        admissible=2,
    )


def test_no_eligible_candidate_retains(observe_barrier):
    life = life15(observe_barrier)
    life.finish_final(accepted=False)
    life.select()
    assert life.snapshot.rounds[1].family15.selection.payload["reason"] == ("no_eligible_candidate")
    life.close_round()
    assert life.snapshot.rounds[1].closed["outcome"] == "retained"


def test_prepare_has_no_authority_until_commit_and_stale_commit_refuses(observe_barrier):
    life = life15(observe_barrier, early=True)
    life.early_attempt()
    payload = life.preflight_payload()
    envelope = life._envelope("comparison_preflight_failed", payload, None)
    before = life.snapshot
    transition = life.state.prepare(envelope)
    assert life.snapshot is before
    assert life.attempt("r1-a1").family15.preflight is None
    transition.commit()
    assert life.attempt("r1-a1").family15.preflight is not None
    with pytest.raises(SchemaError, match="stale"):
        transition.commit()


def test_reuse_final_freezes_before_missing_work_and_waits_for_coverage(observe_barrier):
    life = life15(observe_barrier, stability="unstable")
    life.finish_gate()
    life.bind_finalists()
    life.bind_champion()
    # Three total slots, two reused. Only the missing slot is dispatchable.
    payload = life.completion_payload()
    payload.update(required=3, imported=2, missing_count=1)
    binding = life.accept("candidate_completion_bound", **payload)
    with pytest.raises(SchemaError):
        life.accept("stage_opened", measurement_id="completion", stage=1, look=1, sample_count=1)
    final = life.bind_reuse_final(binding)
    with pytest.raises(SchemaError):
        life.accept("stage_opened", measurement_id="completion", stage=1, look=1, sample_count=3)
    life.accept("stage_opened", measurement_id="completion", stage=1, look=1, sample_count=1)
    result = dict(
        round=1,
        attempt_id=life.sealed,
        binding=life.reference(final),
        data=life.data(),
        outcome="passed",
        reason="passed",
        deciding_scope="global",
        accepted=True,
        candidate=life.source("completion"),
        reference=life.source("fresh-champion"),
    )
    with pytest.raises(SchemaError):
        life.accept("global_comparison_completed", **result)
    life.accept(
        "candidate_completion_completed",
        round=1,
        attempt_id=life.sealed,
        binding=life.reference(binding),
        source=life.source("completion"),
        required=3,
        admissible=3,
    )
    with pytest.raises(SchemaError):
        life.accept("global_comparison_completed", **result)


def test_statistical_rejection_continues_and_cannot_suspend(observe_barrier):
    life = life15(observe_barrier, attempts_per_round=2, round_stop="first_accepted")
    binding = life.bind_gate()
    life.accept(
        "stage_opened",
        measurement_id=life.attempt(life.sealed).measurement_id,
        stage=1,
        look=1,
        sample_count=2,
    )
    payload = life.gate_result_payload(binding)
    payload.update(
        outcome="comparison_not_passed", reason="threshold_not_passed", deciding_scope="focus"
    )
    life.settle_gate_sides(binding)
    result = life.accept("comparison_completed", **payload)
    life.accept(
        "attempt_closed",
        attempt_id=life.sealed,
        outcome="rejected",
        reason="threshold_not_passed",
        path="candidate",
        evidence=life.reference(result),
    )
    with pytest.raises(SchemaError):
        life.accept(
            "campaign_suspended", owner="statistics", reason="comparison_not_passed", evidence=None
        )
    life.accept(
        "attempt_opened",
        round=1,
        attempt_id="r01-a02",
        attempt_index=2,
        parent_version=life.snapshot.champion[0],
        parent_digest=life.snapshot.champion[1],
        path="candidate",
    )
    earlier = life.attempt(life.sealed)
    frozen = copy.deepcopy(earlier.frozen_input)
    frozen["attempt_id"] = "r01-a02"
    frozen["feedback"]["attempts"] = [
        dict(
            attempt_id=earlier.attempt_id,
            index=1,
            assessed=True,
            outcome="rejected",
            reason="threshold_not_passed",
            changes_status="withheld",
            changed_paths=[],
            changed_paths_truncated=False,
            diff_excerpt="",
            diff_excerpt_truncated=False,
            cases=[],
            omitted_exam_cases=0,
            omitted_rest_cases=0,
        )
    ]
    frozen["source_refs"] = [life.reference(earlier.closed_seq)]
    frozen["input_digest"] = hashlib.sha256(
        canonical_json(
            {
                k: frozen[k]
                for k in ("common_input_digest", "feedback", "feedback_mode", "source_refs")
            }
        )
    ).hexdigest()
    life.accept("attempt_input_frozen", **frozen)
    assert life.attempt("r01-a02").outcome is None
    assert life.snapshot.alpha_rounds == {}


def test_early_global_cannot_open_role_work(observe_barrier):
    life = life15(observe_barrier, early=True)
    life.early_attempt()
    grouping = next(copy.deepcopy(r) for r in observe_barrier if r["event"] == "grouping_started")
    grouping["payload"]["grouper_kind"] = "fields"
    with pytest.raises(SchemaError):
        life.accept("grouping_started", **grouping["payload"])


def test_operational_final_gap_preserves_owner_and_repair_grant(observe_barrier):
    from evolution.tree.campaign_schema import cohort_rows_digest

    life = life15(observe_barrier, stability="unstable")
    life.finish_gate()
    life.bind_finalists()
    life.bind_champion()
    binding = life.bind_completion()
    life.bind_reuse_final(binding)
    life.accept("stage_opened", measurement_id="fresh-champion", stage=1, look=1, sample_count=2)
    rows = [
        dict(
            measurement_id="fresh-champion",
            case_id="c1",
            sample_id="01",
            retry_ordinal=0,
            owner="target",
        )
    ]
    payload = dict(
        owner="target",
        reason="final_incomplete",
        evidence=dict(
            evidence_kind="final_rows",
            count=1,
            rows_digest=cohort_rows_digest(rows),
            truncated=False,
            rows=rows,
        ),
    )
    life.accept("campaign_suspended", **payload)
    assert life.snapshot.suspended
    assert life.snapshot.suspensions[-1]["owner"] == "target"
    assert ("fresh-champion", "c1", "01") in life.state._cohort_listed


def test_early_global_exhausted_round_retains_without_roles(observe_barrier):
    life = life15(observe_barrier, early=True)
    life.early_attempt()
    failed = life.accept("comparison_preflight_failed", **life.preflight_payload())
    life.accept(
        "attempt_closed",
        attempt_id="r1-a1",
        path="early_global",
        outcome="rejected",
        reason="structurally_unattainable",
        evidence=life.reference(failed),
    )
    incumbent = life.source(life.snapshot.rounds[1].baseline_measurement_id)
    life.accept(
        "selection_decided",
        round=1,
        data=life.data(),
        reason="no_eligible_candidate",
        selected_candidate=None,
        selected_evidence=None,
        next_champion=incumbent,
    )
    life.accept(
        "round_closed",
        round=1,
        outcome="retained",
        reason=None,
        champion_version=life.snapshot.champion[0],
        champion_digest=life.snapshot.champion[1],
        attempts_opened=1,
        coverage=dict(
            selected_cohort_visits=[],
            claimed_cases=0,
            examined_cases=0,
            measured_cases=0,
            had_assessed_candidate=False,
            cycle=1,
            closure_refs=[],
            omitted_closure_refs=0,
        ),
        evidence=None,
    )
    assert life.snapshot.rounds[1].closed["outcome"] == "retained"


def test_rejected_completion_reference_cannot_borrow_other_attempt(observe_barrier):
    life = life15(observe_barrier)
    life.finish_gate()
    life.bind_finalists()
    p = life.completion_payload()
    p["finalist_closure"] = life.reference(life.sequence_of("comparison_completed"))
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("candidate_completion_bound", **p)
    assert life.snapshot is before


def test_partial_completion_rebind_dispatches_only_remaining_slots(observe_barrier):
    life = life15(observe_barrier, stability="unstable")
    life.finish_gate()
    life.bind_finalists()
    life.bind_champion()
    payload = life.completion_payload()
    payload.update(required=4, imported=2, missing_count=2)
    binding = life.accept("candidate_completion_bound", **payload)
    life.bind_reuse_final(binding)
    life.accept("stage_opened", measurement_id="completion", stage=1, look=1, sample_count=2)
    life.continue_block(["completion"], imported=3)
    entry = life.snapshot.measurements["completion@e1"]
    assert entry["missing_count"] == 1
    life.accept("stage_opened", measurement_id="completion@e1", stage=1, look=1, sample_count=1)
    with pytest.raises(SchemaError):
        life.accept(
            "candidate_completion_completed",
            round=1,
            attempt_id=life.sealed,
            binding=life.reference(binding),
            source=life.source("completion"),
            required=4,
            admissible=4,
        )
    life.accept(
        "candidate_completion_completed",
        round=1,
        attempt_id=life.sealed,
        binding=life.reference(binding),
        source=life.source("completion@e1"),
        required=4,
        admissible=4,
    )


def test_settled_gate_measurement_is_not_rebound(observe_barrier):
    life = life15(observe_barrier)
    binding = life.bind_gate()
    mid = life.attempt(life.sealed).measurement_id
    life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    life.settle_gate_sides(binding)
    life.accept("comparison_completed", **life.gate_result_payload(binding))
    life.continue_block()
    assert life.snapshot.measurements[mid]["complete"]
    with pytest.raises(SchemaError):
        life.accept("measurement_rebound", **life.rebound_payload(mid))


def test_null_required_reference_is_rejected_as_schema_error(observe_barrier):
    life = life15(observe_barrier)
    binding = life.bind_gate()
    payload = life.gate_result_payload(binding)
    payload["binding"] = None
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **payload)
    assert life.snapshot is before


def test_snapshot_exposes_immutable_family15_measurement_carry(observe_barrier):
    life = life15(observe_barrier)
    life.finish_gate()
    life.bind_finalists()
    life.bind_completion()
    before = life.snapshot
    assert before.family15 is not None
    life.continue_block(["completion"])
    current = life.snapshot.family15.current_measurements
    assert current["completion"] == "completion@e1"
    assert before.family15.current_measurements["completion"] == "completion"
    with pytest.raises(TypeError):
        current["bad"] = "bad"


def test_rebound_completion_with_all_slots_imported_needs_no_dispatch(observe_barrier):
    life = life15(observe_barrier)
    life.finish_gate()
    life.bind_finalists()
    payload = life.completion_payload()
    payload.update(required=4, imported=2, missing_count=2)
    binding = life.accept("candidate_completion_bound", **payload)
    life.bind_reuse_final(binding)
    life.continue_block(["completion"], imported=4)
    life.accept(
        "candidate_completion_completed",
        round=1,
        attempt_id=life.sealed,
        binding=life.reference(binding),
        source=life.source("completion@e1"),
        required=4,
        admissible=4,
    )
    assert not life.snapshot.measurements["completion@e1"]["stages"]


def test_round_closure_requires_exact_assessed_comparison_reference(observe_barrier):
    life = life15(observe_barrier)
    life.finish_final(accepted=False)
    life.select()
    ref = life.attempt(life.sealed).family15.comparison.reference.to_payload()
    ref["locator"] = "campaign.jsonl#bytes=0:1"
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.close_round(closure_ref=ref)
    with pytest.raises(SchemaError):
        life.close_round(outcome="unscored")
    assert life.snapshot is before
    life.close_round()


def test_fresh_parent_covers_focus_and_causal_without_legacy_repeat_selector(observe_barrier):
    from evolution.tree.campaign_schema import confidence_scopes

    life = life15(observe_barrier, fresh_parent=True)
    a = life.attempt(life.sealed)
    focus, causal = confidence_scopes(
        a.selection,
        life.snapshot.identity["corpus_cases"],
        life.snapshot.rounds[1].gate_scope["cases"],
    )
    assert focus and causal
    parent = life.snapshot.measurements[a.parent_measurement_id]
    assert tuple(parent["case_subset"]) == tuple(sorted(focus + causal))
    assert "case_repeats" not in parent
    binding = life.bind_gate()
    for mid in (a.measurement_id, a.parent_measurement_id):
        life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    life.settle_gate_sides(binding)
    result = life.accept("comparison_completed", **life.gate_result_payload(binding))
    life.close_gate(result)
    assert life.snapshot.measurements[a.parent_measurement_id]["complete"]
    assert life.snapshot.measurements[a.measurement_id]["complete"]


@pytest.mark.parametrize("changed", ["content_digest", "locator"])
def test_stored_gate_binds_exact_round_baseline_prefix(observe_barrier, changed):
    life = life15(observe_barrier)
    a = life.attempt(life.sealed)
    reference = life.source(life.snapshot.rounds[1].baseline_measurement_id)
    payload = dict(
        round=1,
        attempt_id=a.attempt_id,
        epoch=0,
        data=life.data(),
        candidate=life.source(a.measurement_id, False),
        reference=reference,
        reference_kind="stored",
    )
    wrong = copy.deepcopy(payload)
    wrong["reference"]["prefix"][changed] = (
        "f" * 64
        if changed == "content_digest"
        else reference["prefix"]["locator"].split("#bytes=")[0] + "#bytes=0:1"
    )
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("comparison_bound", **wrong)
    assert life.snapshot is before
    life.accept("comparison_bound", **payload)


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
def test_condition_closed_round_cannot_bind_new_final_work(observe_barrier, verification):
    life = life15(observe_barrier, rounds=2, verification=verification)
    life.finish_gate()
    life.bind_finalists()
    if verification == "reuse":
        event, payload = "candidate_completion_bound", life.completion_payload()
    else:
        event = "final_measurement_bound"
        payload = dict(
            round=1,
            attempt_id=life.sealed,
            data=life.data(),
            verification="fresh",
            candidate_source="fresh",
            champion_side="stored",
            finalist_closure=life.reference(life.sequence_of("attempt_closed")),
            source_ref=life.reference(life.sequence_of("candidate_gate_sealed")),
            candidate=dict(
                measurement_id="fresh-candidate",
                identity_digest="e" * 64,
                candidate_digest=life.attempt(life.sealed).candidate_digest,
                prefix=None,
            ),
            champion=life.source(life.snapshot.rounds[1].baseline_measurement_id),
        )
    life.continue_block(effect="rebaseline")
    life.close_round(outcome="condition_changed")
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept(event, **payload)
    assert life.snapshot is before


def test_closed_no_eligible_round_cannot_select(observe_barrier):
    life = life15(observe_barrier, rounds=2)
    life.finish_final(accepted=False)
    life.continue_block(effect="rebaseline")
    life.close_round(outcome="condition_changed")
    with pytest.raises(SchemaError):
        life.select()


def test_completed_candidate_parent_only_carry_can_finish_comparison(observe_barrier):
    life = life15(observe_barrier, fresh_parent=True)
    a = life.attempt(life.sealed)
    candidate, parent = a.measurement_id, a.parent_measurement_id
    binding = life.bind_gate()
    for mid in (candidate, parent):
        life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    candidate_payload = life.gate_side_payload(binding)
    life.accept("gate_measurement_completed", **candidate_payload)
    old_parent_payload = life.gate_side_payload(binding, "parent")
    moved = life.continue_block([parent])
    assert moved == [(parent, parent + "@e1")]
    assert life.snapshot.measurements[candidate]["complete"]
    assert life.attempt(life.sealed).family15.comparison is None
    with pytest.raises(SchemaError):
        life.accept("measurement_rebound", **life.rebound_payload(candidate))
    with pytest.raises(SchemaError):
        life.accept("gate_measurement_completed", **old_parent_payload)
    changed = copy.deepcopy(life.envelopes[binding - 1].payload)
    changed.update(
        epoch=1,
        candidate=copy.deepcopy(candidate_payload["source"]),
        reference=life.source(parent + "@e1", False),
    )
    changed["candidate"]["prefix"]["content_digest"] = "f" * 64
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("comparison_bound", **changed)
    assert life.snapshot is before
    binding = life.bind_gate()
    with pytest.raises(SchemaError):
        life.accept("gate_measurement_completed", **candidate_payload)
    with pytest.raises(SchemaError):
        life.accept("stage_opened", measurement_id=candidate, stage=2, look=1, sample_count=2)
    life.accept("stage_opened", measurement_id=parent + "@e1", stage=1, look=1, sample_count=2)
    life.settle_gate_sides(binding)
    result = life.accept("comparison_completed", **life.gate_result_payload(binding))
    life.close_gate(result)
    assert life.attempt(life.sealed).outcome == "finalist"
    assert (
        life.attempt(life.sealed).family15.gate_candidate.payload["source"]
        == candidate_payload["source"]
    )
    assert len(life.snapshot.measurements[candidate]["stages"]) == 1


def test_gate_side_completion_requires_settled_owned_stage_and_atomic_commit(observe_barrier):
    life = life15(observe_barrier)
    binding = life.bind_gate()
    payload = life.gate_side_payload(binding)
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("gate_measurement_completed", **payload)
    assert life.snapshot is before
    mid = life.attempt(life.sealed).measurement_id
    life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    for bad in (
        dict(payload, side="parent"),
        dict(payload, admissible=1),
        dict(payload, binding={**payload["binding"], "content_digest": "f" * 64}),
    ):
        with pytest.raises(SchemaError):
            life.accept("gate_measurement_completed", **bad)
    result = life.gate_result_payload(binding)
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **result)
    assert life.snapshot is before
    envelope = life._envelope("gate_measurement_completed", payload, None)
    transition = life.state.prepare(envelope)
    assert life.snapshot is before
    assert not life.snapshot.measurements[mid]["complete"]
    life.accept("gate_measurement_completed", **payload)
    with pytest.raises(SchemaError):
        transition.commit()
    with pytest.raises(SchemaError):
        life.accept("gate_measurement_completed", **payload)
    with pytest.raises(SchemaError):
        life.accept(
            "indeterminate_observed",
            owner="target",
            reason="target_unstable",
            measurement_id=mid,
            case_id="c1",
            sample_id="01",
            count=1,
            evidence=[life.reference(len(life.envelopes))],
            retry_ordinal=0,
            retry_follows=False,
        )
    bad = copy.deepcopy(result)
    bad["candidate"]["prefix"]["content_digest"] = "f" * 64
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **bad)
    life.accept("comparison_completed", **result)


@pytest.mark.parametrize("reason", ["candidate_incomplete", "reference_incomplete"])
def test_incomplete_owned_side_never_becomes_durable_statistical_result(observe_barrier, reason):
    life = life15(observe_barrier, fresh_parent=True)
    binding = life.bind_gate()
    a = life.attempt(life.sealed)
    for mid in (a.measurement_id, a.parent_measurement_id):
        life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    life.settle_gate_sides(binding)
    payload = life.gate_result_payload(binding)
    payload.update(outcome="comparison_unavailable", reason=reason)
    before = life.snapshot
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **payload)
    assert life.snapshot is before


def test_gate_side_completion_keeps_known_operational_threshold_hold(observe_barrier):
    from evolution.tree.campaign_schema import OWNERS

    life = life15(
        observe_barrier,
        indeterminate={owner: dict(max_count=0, max_rate="0.000000000000") for owner in OWNERS},
    )
    binding = life.bind_gate()
    mid = life.attempt(life.sealed).measurement_id
    life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    life.accept(
        "indeterminate_observed",
        owner="target",
        reason="target_unstable",
        measurement_id=mid,
        case_id="c1",
        sample_id="01",
        count=1,
        evidence=[life.reference(len(life.envelopes))],
        retry_ordinal=0,
        retry_follows=False,
    )
    before = life.snapshot
    with pytest.raises(SchemaError, match="threshold"):
        life.accept("gate_measurement_completed", **life.gate_side_payload(binding))
    assert life.snapshot is before
    assert not life.snapshot.measurements[mid]["complete"]


def test_independent_slot_cannot_be_reused_before_a_stage(observe_barrier):
    life = life15(
        observe_barrier,
        early=True,
        attempts_per_round=2,
        parallel_attempts=1,
        attempt_feedback="none",
    )
    life.early_attempt()
    before = life.snapshot
    with pytest.raises(SchemaError, match="parallel Attempt budget"):
        life.early_attempt(index=2)
    assert life.snapshot is before


def _long_reflection(records, chars, *, limit=None):
    """`records` with round 1's stored `root_cause` at `chars` characters and,
    when `limit` is given, a creation that binds `reflection.group_text_chars`."""
    from evolution.tree.campaign_identity import AdaptationIdentityV1

    rows = copy.deepcopy(records)
    if limit is not None:
        created = rows[0]["payload"]
        created["adaptation_payload"]["reflection"]["group_text_chars"] = limit
        created["adaptation"]["digest"] = AdaptationIdentityV1.from_payload(
            created["adaptation_payload"]
        ).digest
    (finished,) = [row for row in rows if row["event"] == "reflection_finished"]
    finished["payload"]["groups"][0]["root_cause"] = "x" * chars
    return rows


def test_a_family15_round_opens_at_its_creation_limit(observe_barrier):
    """#282 (C6): family-15 rounds open with `baseline_bound` too, so the limit
    the creation binds is round 1's; without the key it is 512 (R2)."""

    with pytest.raises(SchemaError, match="^root_cause exceeds 512 characters$"):
        life15(_long_reflection(observe_barrier, 900))
    assert life15(_long_reflection(observe_barrier, 512)).snapshot.round_group_text_chars(1) == 512
    life = life15(_long_reflection(observe_barrier, 900, limit=4096))
    assert life.snapshot.round_group_text_chars(1) == 4096
    assert len(life.snapshot.rounds[1].reflections[0]["groups"][0]["root_cause"]) == 900
    with pytest.raises(SchemaError, match="^root_cause exceeds 4096 characters$"):
        life15(_long_reflection(observe_barrier, 4097, limit=4096))


def test_a_family15_rebase_mid_round_waits_for_the_next_round(observe_barrier):
    """An authorized 4,096 limit accepted after round 1 opened leaves it at 512."""
    from tests.test_tree_campaign_schema import _limit_rebase

    rows = _long_reflection(observe_barrier, 900)
    start = next(i for i, row in enumerate(rows) if row["event"] == "reflection_started")
    life = life15(rows[:start])
    created = life.envelopes[0].payload
    current = {
        key: copy.deepcopy(created[key])
        for key in (
            "identity",
            "identity_digest",
            "producing_version",
            "adaptation",
            "adaptation_payload",
        )
    }
    life.accept("identity_rebased", **_limit_rebase(current, 4096))
    assert life.snapshot.round_group_text_chars(1) == 512
    for row in rows[start : start + 2]:
        if row["event"] == "reflection_finished":
            with pytest.raises(SchemaError, match="^root_cause exceeds 512 characters$"):
                life.accept(row["event"], op=row["operation_id"], **row["payload"])
        else:
            life.accept(row["event"], op=row["operation_id"], **row["payload"])


def test_a_round_opened_without_evidence_pins_nothing(observe_barrier):
    life = life15(observe_barrier)
    assert life.snapshot.round_evidence(1) is None


def focus_stage(life, *, parent=True):
    attempt = life.attempt(life.sealed)
    for mid in (attempt.measurement_id, attempt.parent_measurement_id if parent else None):
        if mid is not None:
            life.accept(
                "stage_opened",
                measurement_id=mid,
                stage=1,
                look=life.state.policy.repeats,
                sample_count=life.state.policy.repeats,
                scope="focus",
            )


@pytest.mark.parametrize("outcome", ["passed", "comparison_not_passed"])
def test_causal_stage_requires_passing_focus_decision(observe_barrier, outcome):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier)
    life.bind_gate()
    focus_stage(life)
    mid = life.attempt(life.sealed).measurement_id
    with pytest.raises(SchemaError):
        life.accept(
            "stage_opened", measurement_id=mid, stage=2, look=1, sample_count=1, scope="causal"
        )
    life.decide_focus(outcome)
    if outcome == "passed":
        life.accept(
            "stage_opened", measurement_id=mid, stage=2, look=1, sample_count=1, scope="causal"
        )
        assert life.snapshot.measurements[mid]["stages"][-1]["scope"] == "causal"
    else:
        with pytest.raises(SchemaError):
            life.accept(
                "stage_opened", measurement_id=mid, stage=2, look=1, sample_count=1, scope="causal"
            )


@pytest.mark.parametrize("fresh", [False, True])
def test_focus_decision_requires_each_owned_focus_stage(observe_barrier, fresh):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier, fresh_parent=fresh)
    life.bind_gate()
    with pytest.raises(SchemaError):
        life.decide_focus()
    focus_stage(life, parent=False)
    if fresh:
        with pytest.raises(SchemaError):
            life.decide_focus()
        life.accept(
            "stage_opened",
            measurement_id=life.attempt(life.sealed).parent_measurement_id,
            stage=1,
            look=1,
            sample_count=1,
            scope="focus",
        )
    decision = life.decide_focus()
    assert (
        life.attempt(life.sealed).family15.focus_decision.envelope.to_payload()["sequence"]
        == decision
    )
    with pytest.raises(SchemaError):
        life.decide_focus()


@pytest.mark.parametrize("outcome", ["passed", "comparison_not_passed"])
def test_focus_only_completion_requires_nonpassing_decision(observe_barrier, outcome):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier)
    bound = life.bind_gate()
    focus_stage(life)
    life.decide_focus(outcome)
    payload = life.gate_side_payload(bound) | {"scopes": ["focus"], "required": 1, "admissible": 1}
    if outcome == "passed":
        with pytest.raises(SchemaError):
            life.accept("gate_measurement_completed", **payload)
    else:
        life.accept("gate_measurement_completed", **payload)
        assert life.snapshot.measurements[payload["source"]["measurement_id"]]["complete"]


@pytest.mark.parametrize("fresh", [False, True])
def test_focus_rejection_comparison_preserves_focus_verdict(observe_barrier, fresh):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier, fresh_parent=fresh)
    bound = life.bind_gate()
    focus_stage(life)
    life.decide_focus("comparison_not_passed")
    for side in ["candidate", "parent"] if fresh else ["candidate"]:
        life.accept(
            "gate_measurement_completed",
            **(
                life.gate_side_payload(bound, side)
                | {
                    "scopes": ["focus"],
                    "required": 1,
                    "admissible": 1,
                }
            ),
        )
    payload = life.gate_result_payload(bound)
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **payload)
    payload.update(
        outcome="comparison_not_passed", reason="threshold_not_passed", deciding_scope="focus"
    )
    with pytest.raises(SchemaError):
        life.accept("comparison_completed", **(payload | {"reason": "critical_failed"}))
    life.accept("comparison_completed", **payload)


@pytest.mark.parametrize("fresh", [False, True])
def test_passing_comparison_requires_both_scope_completions(observe_barrier, fresh):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier, fresh_parent=fresh)
    bound = life.bind_gate()
    focus_stage(life)
    life.decide_focus()
    full = life.gate_side_payload(bound) | {"scopes": ["focus", "causal"]}
    with pytest.raises(SchemaError):
        life.accept("gate_measurement_completed", **full)
    attempt = life.attempt(life.sealed)
    for mid in (attempt.measurement_id, attempt.parent_measurement_id):
        if mid is not None:
            life.accept(
                "stage_opened", measurement_id=mid, stage=2, look=1, sample_count=1, scope="causal"
            )
    for side in ["candidate", "parent"] if fresh else ["candidate"]:
        life.accept(
            "gate_measurement_completed",
            **(
                life.gate_side_payload(bound, side)
                | {
                    "scopes": ["focus", "causal"],
                }
            ),
        )
    life.accept("comparison_completed", **life.gate_result_payload(bound))


def test_single_refuses_focus_first_fields_and_record(observe_barrier):
    life = life15(observe_barrier)
    bound = life.bind_gate()
    mid = life.attempt(life.sealed).measurement_id
    with pytest.raises(SchemaError):
        life.accept(
            "stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2, scope="focus"
        )
    life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=2)
    with pytest.raises(SchemaError):
        life.accept(
            "gate_measurement_completed",
            **(
                life.gate_side_payload(bound)
                | {
                    "scopes": ["focus", "causal"],
                }
            ),
        )
    with pytest.raises(SchemaError):
        life.decide_focus()


def test_focus_first_gate_stage_requires_its_scope(observe_barrier):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier)
    life.bind_gate()
    mid = life.attempt(life.sealed).measurement_id
    with pytest.raises(SchemaError):
        life.accept("stage_opened", measurement_id=mid, stage=1, look=1, sample_count=1)
    with pytest.raises(SchemaError):
        life.accept(
            "stage_opened", measurement_id=mid, stage=1, look=1, sample_count=1, scope="causal"
        )


@pytest.mark.parametrize("reason", ["candidate_incomplete", "reference_incomplete"])
def test_incomplete_owned_focus_cannot_write_focus_decided(observe_barrier, reason):
    from tests.family15_fakes import Family15Life

    life = Family15Life.focus_first(observe_barrier, fresh_parent=True)
    bound = life.bind_gate()
    focus_stage(life)
    data = life.data()
    with pytest.raises(SchemaError):
        life.accept(
            "focus_decided",
            round=1,
            attempt_id=life.sealed,
            binding=life.reference(bound),
            outcome="comparison_unavailable",
            reason=reason,
            data=data,
        )


@pytest.mark.parametrize("mode", ["point", "statistical"])
def test_focus_seed_is_required_exactly_in_statistical_mode(observe_barrier, mode):
    from evolution.tree.confidence_statistics import derive_seed
    from tests.family15_fakes import Family15Life

    life = (
        statistical_life(observe_barrier, focus_first=True)
        if mode == "statistical"
        else Family15Life.focus_first(observe_barrier)
    )
    bound = life.bind_gate()
    focus_stage(life)
    data = life.data()
    payload = dict(
        round=1,
        attempt_id=life.sealed,
        binding=life.reference(bound),
        outcome="passed",
        reason="passed",
        data=data,
    )
    seed = dict(
        method="f13-bootstrap/1",
        campaign_seed=life.state.policy.random_seed,
        binding=life.envelopes[bound - 1].payload["data"]["declaration"]["content_digest"],
        scope="focus",
        prefixes=[
            "measurements/candidate/events.jsonl#bytes=0:100",
            "measurements/reference/events.jsonl#bytes=0:100",
        ],
        look=2,
        purpose="gate",
    )
    seed["seed"] = derive_seed(**{k: v for k, v in seed.items() if k != "method"})
    with pytest.raises(SchemaError):
        life.accept(
            "focus_decided", **(payload if mode == "statistical" else payload | {"seed": seed})
        )
    life.accept("focus_decided", **(payload | {"seed": seed} if mode == "statistical" else payload))
