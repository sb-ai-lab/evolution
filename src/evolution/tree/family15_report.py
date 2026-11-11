"""Shared pre-spend facts for family 15; exact private profiles stay internal."""

from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evolution.tree.campaign_schema import CampaignSnapshot
    from evolution.tree.case_comparison import ComparisonResult

from evolution.tree.campaign_identity import CampaignIdentityV1, derived_identity
from evolution.tree.campaign_statistics import SampleRow
from evolution.tree.case_comparison import PreflightResult, build_profile, preflight
from evolution.tree.case_statistics import passes_bound


def global_preflight(
    snapshot: Any,
    policy: Any,
    round_number: int,
    reference: Mapping[str, Any],
    rows: Sequence[SampleRow],
) -> PreflightResult:
    """Stable uses the actual citation; unstable always uses a fresh full schedule."""
    epoch = CampaignIdentityV1.from_payload(dict(snapshot.epochs[-1]))
    fresh = policy.stability == "unstable"
    if fresh:
        identity = derived_identity(epoch, snapshot.champion[1], "champion")
        reference = dict(
            measurement_id=f"m-r{round_number:02d}-preflight-champion",
            identity=identity.to_payload(),
            identity_digest=identity.digest,
            prefix=None,
        )
    profile = build_profile(
        policy=policy,
        corpus=epoch.corpus_cases,
        scopes={"global": [c["case_id"] for c in epoch.corpus_cases]},
        candidate=None,
        reference=reference,
        reference_rows=() if fresh else rows,
        epoch=len(snapshot.epochs) - 1,
        reference_kind="fresh" if fresh else "stored",
    )
    return preflight(profile, None if fresh else rows)


def local_guidance(confidence: float, *, strict: bool = True) -> dict[str, Any]:
    """Conditional equal-weight zero-threshold illustration, never a veto."""
    count = 1
    while not passes_bound(
        [Fraction(1)] * count,
        [Fraction(1, count)] * count,
        Fraction(0),
        Fraction(confidence),
        strict=strict,
    ):
        count += 1
    return dict(
        status="conditional_guidance",
        minimum_units=count,
        assumptions=[
            "equal_independent_unit_weights",
            "zero_threshold",
            "all_ones_contrast_envelope",
        ],
        interpretation="not_an_admission_or_power_claim",
    )


def explain_preflight(result: PreflightResult, *, policy: Any = None) -> dict[str, Any]:
    """Safe aggregate explanation; never expose case IDs, slots or reference rows."""
    body = result.to_payload()
    explanation: dict[str, Any] = dict(
        status="provisional_or_disabled"
        if body is None
        else "impossible"
        if result.impossible
        else "not_ruled_out",
        basis="stored_reference"
        if result.basis == "stored"
        else "unknown_fresh_reference_envelope",
        profile_digest=result.profile_digest,
        interpretation="score_range_ceiling_not_a_success_or_power_prediction",
        scopes=[],
    )
    if policy is not None and policy.bound_fallback is not None:
        explanation["bound_fallback"] = policy.bound_fallback
    if body is not None:
        for scope, maximum in zip(body["scopes"], body["results"], strict=True):
            row = dict(name=scope["name"], status=maximum["status"])
            if maximum["status"] not in {"skipped", "uncalibrated"}:
                row.update(
                    rule=scope["rule"],
                    threshold=scope["threshold"],
                    strict=scope["strict"],
                    unit_count=len(scope.get("units", scope["cases"])),
                    passes=maximum["passes"],
                )
                for field in ("delta_max", "e_max", "c_max"):
                    if field in maximum:
                        row[field] = maximum[field]
                if "confidence" in scope:
                    row["confidence"] = scope["confidence"]
                if "fallback" in scope:
                    row["fallback"] = scope["fallback"]
            explanation["scopes"].append(row)
    return explanation


def remaining_refresh_cost(snapshot: Any, policy: Any, round_number: int) -> dict[str, Any]:
    """Declared costs under continued retention, after this round's baseline settles.

    Future changes of Champion or an authorized transition can change this
    scenario. Statistical rejection itself never changes the round-based rule.
    Connection probes and retry ceilings are separate from measured slots.
    """
    from evolution.tree.campaign_epochs import refresh_count

    state = snapshot.rounds[round_number]
    if not state.baseline_complete or state.baseline_measurement_id is None:
        raise ValueError("refresh explanation requires a settled current baseline")
    interval = policy.baseline_refresh_after_rounds
    last = snapshot.first_round + snapshot.total_rounds - 1
    count = refresh_count(
        snapshot.rounds,
        first_round=snapshot.first_round,
        round_number=round_number,
        next_baseline=state.baseline_measurement_id,
    )
    remaining = 0 if not interval else (count + last - round_number) // interval
    slots = len(snapshot.identity["corpus_cases"]) * policy.repeats
    return dict(
        scenario="retain_champion_through_remaining_round_budget",
        remaining_refreshes=remaining,
        slots_per_refresh=slots,
        scenario_slots=remaining * slots,
        retry_ceiling_separate=True,
        connection_preflight_credit="separate",
    )


def scenario_cost(
    train_cases: int,
    affected_cases: int,
    repeats: int,
    *,
    baseline_imports: int = 0,
    verification: str = "reuse",
    fresh_parent: bool = False,
    stability: str = "stable",
    round_stop: str = "first_accepted",
    finalists: int = 1,
    gate_passed: bool = True,
    stages: str = "single",
    focus_cases: int | None = None,
    focus_passed: bool | None = None,
    focus_repeats: int | None = None,
) -> dict[str, Any]:
    """No-retry, complete-scoring scenario, not a forecast of reaching final.

    Candidate costs describe one Attempt. Champion cost describes the stated
    finalist set; do not multiply the shared all-attempts cost by its members.
    """
    total, gate = train_cases * repeats, affected_cases * repeats
    # b18: focus cases measured at `focus_repeats` on the Candidate and fresh Parent.
    if focus_repeats is not None:
        if focus_cases is None or not 0 <= focus_cases <= affected_cases:
            raise ValueError("focus repeats require the declared focus size")
        gate += focus_cases * (focus_repeats - repeats)
    reused = affected_cases * repeats
    if stages == "focus_first" and not (gate_passed if focus_passed is None else focus_passed):
        if focus_cases is None or not 0 <= focus_cases <= affected_cases:
            raise ValueError("focus-first rejection requires its declared focus size")
        gate = focus_cases * (repeats if focus_repeats is None else focus_repeats)
    final = (total if verification == "fresh" else total - reused) if gate_passed else 0
    champion = (
        total * (int(finalists > 0) if round_stop == "all_attempts" else finalists)
        if stability == "unstable" and gate_passed
        else 0
    )
    return dict(
        basis="full_scenario_complete_scoring_without_retries",
        baseline_slots=max(0, total - baseline_imports),
        candidate_gate_slots=gate,
        candidate_final_slots=final,
        candidate_total_slots=gate + final,
        parent_slots=gate if fresh_parent else 0,
        champion_slots=champion,
        champion_basis="once_per_frozen_nonempty_finalist_set"
        if round_stop == "all_attempts"
        else "per_reached_finalist",
        slots_per_refresh=total,
        connection_preflight_slots=1,
        probes_and_retries="separate",
        noise_diagnostic_slots=0,
        simplest_passing_path_slots=max(0, total - baseline_imports) + gate + final + 1,
    )


def comparison_projection(
    result: "ComparisonResult",
    *,
    profile: Mapping[str, Any] | None = None,
    verification: str | None = None,
    policy: Any = None,
) -> dict[str, Any]:
    """Redact exact numerical facts; profiles supply frozen threshold metadata.

    The result alone cannot reconstruct its binding. Missing profile fields
    remain unknown; no policy or reference facts are guessed from an outcome.
    No raw slots, per-case variation, provenance locators or user text escape.
    """
    from evolution.tree.family15_schema import encode_rational

    body = result.to_payload()
    bound = profile or {}
    noise = bound.get("execution_noise", {})
    kind = noise.get("kind")
    allowance = noise.get("allowance", encode_rational(Fraction())) if kind else None
    scopes = []
    profiles = {s["name"]: s for s in bound.get("scopes", ())}
    for scope in body["scopes"]:
        frozen = profiles.get(scope["name"], {})
        scopes.append(
            dict(
                **{
                    k: scope[k]
                    for k in (
                        "name",
                        "rule",
                        "status",
                        "declared_count",
                        "unit_count",
                        "mass",
                        "excluded_mass",
                        "unavailable",
                    )
                },
                point=scope["point"] if body["reason"] != "candidate_incomplete" else None,
                threshold=scope.get("threshold", frozen.get("threshold")),
                strict=frozen.get("strict"),
                confidence=scope.get("confidence"),
                enclosure=scope.get("enclosure"),
            )
        )
        if scope["rule"] == "bootstrap":
            scopes[-1].update({k: scope[k] for k in ("lower", "exact", "pairing")})
        if "fallback" in scope:
            scopes[-1]["fallback"] = scope["fallback"]
    reference = bound.get("reference_kind")
    bias = []
    if any(s["name"] == "focus" for s in scopes):
        bias.append("focus_selection")
    if reference == "stored":
        bias.append("stored_reference")
    if verification == "reuse":
        bias.append("reuse")
    if verification in {"reuse", "fresh"}:
        bias.append("candidate_selection")
    return dict(
        **(
            {"bound_fallback": policy.bound_fallback}
            if policy is not None and policy.bound_fallback is not None
            else {}
        ),
        **(
            {"calibration": body.get("calibration", bound.get("calibration"))}
            if "calibration" in body or "calibration" in bound
            else {}
        ),
        **({"caveats": body["caveats"]} if "caveats" in body else {}),
        outcome=body["outcome"],
        reason=body["reason"],
        deciding_layer=body["deciding_scope"],
        eligible=body["outcome"] == "passed",
        mode=body["mode"],
        scopes=scopes,
        coverage=body["coverage"],
        excluded_reference_slots=len(body["excluded_reference"]),
        method=bound.get("statistics", {}).get("method"),
        unit=bound.get("statistics", {}).get("unit"),
        confidence_scope="conditional_fixed_scope_independent_bounded_units"
        if any(s["rule"] == "bound" for s in scopes)
        else None,
        reference="stored_non_contemporaneous"
        if reference == "stored"
        else "fresh"
        if reference == "fresh"
        else None,
        reference_measurement_id=body["reference"]["measurement_id"],
        verification="reused_evidence"
        if verification == "reuse"
        else "fresh_global_evidence"
        if verification == "fresh"
        else None,
        selection_bias_possible=bias,
        noise=dict(
            kind=kind,
            allowance=allowance,
            source=noise.get("source") if kind == "bounded" else None,
            source_kind="operator_supplied"
            if kind == "bounded"
            else "assumed_negligible"
            if kind == "negligible"
            else "diagnostic_only"
            if kind == "observed"
            else None,
            variation=body["variation"],
            diagnostic="candidate_primary_score_repeat_variance",
            units="score_squared",
            observed_cases=sum(d["variation"] is not None for d in body["repeat_diagnostics"]),
            extra_measurements=0,
        ),
    )


def campaign15_projection(
    snapshot: "CampaignSnapshot",
    *,
    run_root: Any = None,
    preflights: Mapping[int, PreflightResult] | None = None,
    unavailable: bool = False,
    campaign_records: Any = None,
    measurement_records: Any = None,
) -> dict[str, Any]:
    """One shared public projection, with explicit optional external evidence.

    A snapshot owns policy, outcomes and pending requests. Authenticated S2
    payloads supply numerical binding/result details; S1 owns committed work.
    Neither presentation nor reading this report changes campaign decisions.
    """
    from evolution.tree.campaign_policy import policy_class_for_payload
    from evolution.tree.family15_schema import BOOTSTRAP_METHOD, encode_rational

    raw_policy = snapshot.identity["policy"]
    policy: Any = policy_class_for_payload(raw_policy).from_payload(raw_policy)
    n = len(snapshot.identity["corpus_cases"])
    linked = snapshot.identity.get("linked_from") or {}
    imported = (linked.get("baseline") or {}).get("imported", 0)
    if snapshot.baseline_abandoned is not None:
        imported = 0
    report: dict[str, Any] = dict(
        next_action="finished"
        if snapshot.finished is not None
        else "repair_evidence"
        if unavailable
        else "repair_and_resume"
        if snapshot.suspended
        else "continue_evolve",
        settings=dict(
            mode=policy.gate_mode,
            verification=policy.verification,
            stability=policy.stability,
            fresh_parent=policy.fresh_parent,
            global_rule=policy.global_mode,
            train_cases=n,
            repeats=policy.repeats,
            train_slots=n * policy.repeats,
            execution_noise=policy.execution_noise["kind"],
            allowance=encode_rational(Fraction(policy.execution_noise.get("allowance", 0))),
            allowance_source=policy.execution_noise.get("source"),
            allowance_source_kind="operator_supplied"
            if policy.execution_noise["kind"] == "bounded"
            else "assumed_negligible"
            if policy.execution_noise["kind"] == "negligible"
            else "diagnostic_only",
            method=BOOTSTRAP_METHOD
            if policy.gate_mode == "statistical"
            else None
            if policy.statistics is None
            else policy.statistics["method"],
            unit=None if policy.statistics is None else policy.statistics["unit"],
        ),
        planned_work=dict(
            basis="full_initial_baseline_and_conditional_future_stage_scenarios",
            baseline_slots=max(0, n * policy.repeats - imported),
            admitted_baseline_imports=imported,
            affected_cases=None,
            candidate_final_slots_if_gate_passes=n * policy.repeats
            if policy.verification == "fresh"
            else None,
            parent_gate_slots=None if policy.fresh_parent else 0,
            champion_slots_per_scope=n * policy.repeats if policy.stability == "unstable" else 0,
            champion_scope="frozen_nonempty_finalist_set"
            if policy.round_stop == "all_attempts"
            else "reached_finalist",
            slots_per_refresh=n * policy.repeats,
            connection_preflight="separate_no_measurement_credit",
            probes_and_retries="separate",
            noise_diagnostic_slots=0,
        ),
        local_guidance=local_guidance(policy.focus_confidence)
        if policy.gate_mode == "confidence"
        else None,
        preflight=[],
        attempts=[],
        diagnostics=[],
        committed_work=None,
        remaining_work=None,
        evidence="unavailable" if unavailable else "snapshot_only",
    )
    if policy.focus_first:
        report["settings"]["stages"] = "focus_first"
    if policy.bound_fallback is not None:
        report["settings"]["bound_fallback"] = policy.bound_fallback
    availability = [
        {"round": number, **reflection["probe_availability"]}
        for number, state in sorted(snapshot.rounds.items())
        for reflection in state.reflections
        if "probe_availability" in reflection
    ]
    if availability:
        report["probe_availability"] = availability
    for number, result in sorted((preflights or {}).items()):
        entry = dict(
            round=number,
            explanation=explain_preflight(result),
            refresh=remaining_refresh_cost(snapshot, policy, number),
        )
        report["preflight"].append(entry)
    if snapshot.family15 is not None:
        for record in snapshot.family15.diagnostic_imports:
            p = record.payload
            report["diagnostics"].append(
                dict(
                    scheduled=p["scheduled"],
                    included=p["included"],
                    sufficiency=p["sufficiency"],
                    score="journal_bound",
                    request="capture_time_producer_attestation",
                    request_coverage=p["authentication"]["request"],
                    independent_request_replay="requires_matching_corpus",
                    stdout="captured_at_import",
                )
            )
    if run_root is not None and not unavailable:
        _add_external_facts(
            report, run_root, snapshot, policy, campaign_records, measurement_records
        )
    if snapshot.family15 is not None:
        for name in ("check_modes", "shadow_hits"):
            value = getattr(snapshot.family15, name)
            if value:
                report[name] = dict(value)
    return report


def _add_external_facts(
    report: dict[str, Any],
    root: Any,
    snapshot: Any,
    policy: Any,
    campaign_records: Any,
    measurement_records: Any,
) -> None:
    """Authenticate publications once per report; no full numerical E replay."""
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_policy import policy_class_for_payload
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.campaign_view import owed_rows
    from evolution.tree.case_comparison import (
        ComparisonProfile,
        ComparisonResult,
        PreflightResult,
        preflight,
    )
    from evolution.tree.execution.candidate_integrity import ArtifactError
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from evolution.tree.run_reader import committed_campaign_work, remaining_measurement_slots
    from evolution.tree.storage.journal_errors import JournalBoundaryError

    try:
        context, _ = _evidence_of(root)
        if campaign_records is None:
            if context.snapshot_before(len(context.records)) != snapshot:
                raise EvidenceMismatch("report snapshot changed while reading")
            campaign_records = context.records
        elif tuple(context.records[: len(campaign_records)]) != tuple(campaign_records):
            raise EvidenceMismatch("report prefix changed while reading")
        audit = Family15Audit(context)

        def data(record: Any) -> dict[str, Any]:
            body = audit.publication(AuxiliaryPublication.from_payload(record.payload["data"]))
            assert body is not None
            return body

        unscored, scheduled = remaining_measurement_slots(
            root, snapshot, measurement_records=measurement_records
        )
        attempts = []
        for number, state in sorted(snapshot.rounds.items()):
            ranking = {}
            extra_round = state.family15
            ranked = None if extra_round is None else extra_round.selection
            if ranked is not None:
                ranking = {e["attempt_id"]: e for e in data(ranked)["entries"]}
            for attempt in sorted(state.attempts.values(), key=lambda a: a.index):
                extra = attempt.family15
                if extra is None:
                    continue
                gate_comparison = None
                if extra.comparison is not None and extra.comparison_binding is not None:
                    gate_comparison = comparison_projection(
                        ComparisonResult(canonical_json(data(extra.comparison))),
                        profile=data(extra.comparison_binding),
                        verification=None,
                        policy=context.policy_for(
                            data(extra.comparison_binding)["candidate"]["measurement_id"]
                        ),
                    )
                global_comparison = None
                if extra.global_result is not None and extra.final_binding is not None:
                    final_binding = data(extra.final_binding)
                    global_comparison = comparison_projection(
                        ComparisonResult(canonical_json(data(extra.global_result))),
                        profile=final_binding["comparison"],
                        verification=final_binding["verification"],
                        policy=context.policy_for(
                            final_binding["comparison"]["candidate"]["measurement_id"]
                        ),
                    )
                comparison = global_comparison or gate_comparison
                check = None
                profile = None
                if extra.preflight is not None:
                    body = data(extra.preflight)
                    check = explain_preflight(
                        PreflightResult("", "", body["reference_kind"], canonical_json(body), True),
                        policy=policy_class_for_payload(
                            snapshot.epochs[body["epoch"]]["policy"]
                        ).from_payload(snapshot.epochs[body["epoch"]]["policy"]),
                    )
                    check["profile_digest"] = (
                        None  # Candidate identity is absent from the published preflight.
                    )
                elif extra.comparison_binding is not None:
                    profile = data(extra.comparison_binding)
                    for side in ("candidate", "reference"):
                        declared = profile[side]
                        scheduled.setdefault(
                            declared["measurement_id"],
                            len(owed_rows(snapshot, declared["measurement_id"])),
                        )
                    groups = {
                        c["case_id"]: c["group_id"] for c in snapshot.identity["corpus_cases"]
                    }
                    if extra.comparison is None:
                        bound_policy = context.policy_for(profile["candidate"]["measurement_id"])
                        check = explain_preflight(
                            preflight(
                                ComparisonProfile.from_payload(
                                    profile,
                                    primary=bound_policy.metric_primary,
                                    groups=groups,
                                    policy=bound_policy,
                                ),
                                None,
                            ),
                            policy=bound_policy,
                        )
                affected = (
                    None
                    if profile is None
                    else len(
                        {row["case_id"] for row in profile["candidate"]["identity"]["schedule"]}
                    )
                )
                focus_repeats = getattr(policy, "focus_repeats_per_question", None)
                scoped = profile is not None and getattr(
                    context.policy_for(profile["candidate"]["measurement_id"]), "focus_first", False
                )
                focus_passed = (
                    extra.focus_decision is None
                    or extra.focus_decision.payload["outcome"] == "passed"
                )
                costs = (
                    None
                    if affected is None
                    else scenario_cost(
                        len(snapshot.identity["corpus_cases"]),
                        affected,
                        policy.repeats,
                        verification=policy.verification,
                        fresh_parent=policy.fresh_parent,
                        stability=policy.stability,
                        round_stop=policy.round_stop,
                        gate_passed=(
                            focus_passed
                            and (
                                extra.comparison is None
                                or extra.comparison.payload["outcome"] == "passed"
                            )
                        )
                        if scoped
                        else extra.comparison is None
                        or extra.comparison.payload["outcome"] == "passed",
                        focus_passed=focus_passed if scoped else None,
                        stages="focus_first" if scoped else "single",
                        focus_cases=len(profile["scopes"][0]["declared_cases"])
                        if profile is not None and (scoped or focus_repeats is not None)
                        else None,
                        focus_repeats=focus_repeats,
                        finalists=sum(a.outcome == "finalist" for a in state.attempts.values()),
                    )
                )
                slot_counts: dict[str, Any] = {}
                if scoped:
                    assert profile is not None
                    sides = {"candidate": profile["candidate"]["measurement_id"]}
                    if context.policy_for(sides["candidate"]).fresh_parent:
                        sides["parent"] = profile["reference"]["measurement_id"]
                    slot_counts["slots"] = {}
                    for side, mid in sides.items():
                        case_slots = len(
                            profile["candidate" if side == "candidate" else "reference"][
                                "identity"
                            ]["schedule"]
                        )
                        dispatched = set()
                        for owned, entry in snapshot.measurements.items():
                            if (
                                entry.get("role") != side
                                or entry.get("attempt_id") != attempt.attempt_id
                            ):
                                continue
                            records = (
                                context.measurement_journal(owned)
                                if measurement_records is None
                                else measurement_records.get(owned, ())
                            )
                            slots_by_id = (
                                {}
                                if not records
                                else {
                                    (slot["case_id"], slot["sample_id"]): slot["repeat"]
                                    for slot in records[0].payload["identity"]["schedule"]
                                }
                            )
                            for record in records:
                                if record.event == "rollout_started":
                                    dispatched.add(
                                        (
                                            record.payload["case_id"],
                                            slots_by_id[
                                                (
                                                    record.payload["case_id"],
                                                    record.payload["sample_id"],
                                                )
                                            ],
                                        )
                                    )
                        slot_counts["slots"][side] = dict(
                            scheduled=case_slots,
                            dispatched=len(dispatched),
                            not_owed=case_slots - len(owed_rows(snapshot, mid)),
                        )
                ranked_row = ranking.get(attempt.attempt_id)
                attempts.append(
                    dict(
                        **slot_counts,
                        round=number,
                        attempt_id=attempt.attempt_id,
                        outcome=attempt.outcome,
                        reason=attempt.reason,
                        comparison=comparison,
                        gate_comparison=gate_comparison,
                        global_comparison=global_comparison,
                        preflight=check,
                        conditional_cost=costs,
                        eligible_final_score=ranked_row["point"]
                        if ranked_row is not None
                        and ranked_row["accepted"]
                        and comparison is not None
                        and comparison["eligible"]
                        else None,
                    )
                )
        report["attempts"] = attempts
        report["remaining_work"] = remaining_cost(snapshot, policy, unscored, scheduled)
        report["committed_work"] = committed_campaign_work(
            root, snapshot, tuple(campaign_records), measurement_records=measurement_records
        )
        report["evidence"] = "authenticated_publications"
    except (EvidenceMismatch, ArtifactError, JournalBoundaryError, OSError):
        report["evidence"] = "unavailable"
        report["committed_work"] = None
        report["remaining_work"] = None
        if snapshot.finished is None:
            report["next_action"] = "repair_evidence"


def family15_attempt_vocabulary() -> Mapping[str, frozenset[str]]:
    """Keep public outcomes aligned with feedback and no-path close admission."""
    from evolution.tree.campaign_schema import _FINAL_ATTEMPT_OUTCOMES, feedback_outcomes
    from evolution.tree.family15_schema import REASONS

    vocabulary = dict(feedback_outcomes(15))
    excluded = REASONS | {"insufficient_calibration", "insufficient_repeats"}
    for outcome in ("abandoned", "insufficient_evidence"):
        vocabulary[outcome] |= _FINAL_ATTEMPT_OUTCOMES[outcome] - excluded
    return vocabulary


def remaining_cost(
    snapshot: Any, policy: Any, unscored: Mapping[str, int], scheduled: Mapping[str, int]
) -> dict[str, Any]:
    """Conditional remaining work for the current baseline/opened Attempts.

    No work for unopened future Attempts is forecast. Declared imports/scored
    slots receive credit, failed gates buy no final and all-attempts shares one
    Champion. Refresh scenarios retain Task5's existing schedule owner.
    """
    from evolution.tree.campaign_view import _scheduled_rows, owed_rows

    total = len(snapshot.identity["corpus_cases"]) * policy.repeats
    current = snapshot.rounds.get(max(snapshot.rounds)) if snapshot.rounds else None

    def missing(mid: str | None, default: int) -> int:
        if mid is not None and snapshot.family15 is not None:
            mid = snapshot.family15.current_measurements.get(mid, mid)
        if mid is not None and snapshot.measurements.get(mid, {}).get("complete"):
            return 0
        return default if mid is None else unscored.get(mid, scheduled.get(mid, default))

    baseline = 0
    gate: int | None = 0
    parent: int | None = 0
    final: int | None = 0
    champion = 0
    refreshes = refresh_slots = 0 if snapshot.finished is not None else None
    if current is not None and snapshot.finished is None:
        if not current.baseline_complete:
            inherited = 0
            if current.round == snapshot.first_round and snapshot.baseline_abandoned is None:
                inherited = (
                    (snapshot.identity.get("linked_from") or {}).get("baseline") or {}
                ).get("imported", 0)
            baseline = missing(current.baseline_measurement_id, max(0, total - inherited))
        if current.baseline_complete and current.baseline_measurement_id is not None:
            refresh = remaining_refresh_cost(snapshot, policy, current.round)
            refreshes, refresh_slots = refresh["remaining_refreshes"], refresh["scenario_slots"]
        if current.closed is None:
            pending_finalists = []
            for attempt in current.attempts.values():
                extra = attempt.family15
                if (
                    extra is None
                    or extra.preflight is not None
                    or attempt.outcome
                    in {"rejected", "declined", "failed", "abandoned", "insufficient_evidence"}
                ):
                    continue
                if extra.comparison is not None and extra.comparison.payload["outcome"] != "passed":
                    continue
                if (
                    getattr(policy, "focus_first", False)
                    and extra.focus_decision is not None
                    and extra.focus_decision.payload["outcome"] != "passed"
                ):
                    continue
                if extra.comparison_binding is None:
                    gate = None
                    parent = None if policy.fresh_parent else parent
                    if policy.verification == "reuse":
                        final = None
                    elif final is not None:
                        final += total
                else:
                    # Conditional work assumes pending focus passes. Owed-now
                    # maps exclude C until then; add those unopened rows once.
                    def gate_missing(mid: str | None) -> int:
                        if not getattr(policy, "focus_first", False) or mid is None:
                            return missing(mid, total)
                        if snapshot.family15 is not None:
                            mid = snapshot.family15.current_measurements.get(mid, mid)
                        full = len(_scheduled_rows(snapshot, mid))
                        remaining = missing(mid, full)
                        if not snapshot.measurements[mid].get("complete") and (
                            mid in unscored or mid in scheduled
                        ):
                            remaining += full - len(owed_rows(snapshot, mid))
                        return remaining

                    if extra.comparison is None:
                        if gate is not None:
                            gate += gate_missing(attempt.measurement_id)
                        if policy.fresh_parent and parent is not None:
                            parent += gate_missing(attempt.parent_measurement_id)
                    if final is not None:
                        if (
                            extra.global_result is not None
                            or extra.completion is not None
                            or extra.final_completion is not None
                        ):
                            pass
                        elif extra.final_binding is not None:
                            mid = extra.final_binding.payload["candidate"]["measurement_id"]
                            imported = (
                                0
                                if extra.completion_binding is None
                                else extra.completion_binding.payload["imported"]
                            )
                            final += missing(mid, total - imported)
                        elif extra.comparison is not None:
                            # S1 schedule size is supplied separately below.
                            gate_size = (
                                len(_scheduled_rows(snapshot, attempt.measurement_id))
                                if getattr(policy, "focus_first", False)
                                and attempt.measurement_id is not None
                                else scheduled.get(str(attempt.measurement_id))
                            )
                            if policy.verification == "reuse" and gate_size is None:
                                final = None
                            else:
                                final += (
                                    total
                                    if policy.verification == "fresh"
                                    else max(0, total - gate_size)
                                )
                        else:
                            gate_size = (
                                len(_scheduled_rows(snapshot, attempt.measurement_id))
                                if getattr(policy, "focus_first", False)
                                and attempt.measurement_id is not None
                                else scheduled.get(str(attempt.measurement_id))
                            )
                            if policy.verification == "reuse" and gate_size is None:
                                final = None
                            else:
                                final += (
                                    total if policy.verification == "fresh" else total - gate_size
                                )
                if (
                    extra.comparison is not None
                    and extra.comparison.payload["outcome"] == "passed"
                    and extra.global_result is None
                ):
                    pending_finalists.append(attempt.attempt_id)
            if policy.stability == "unstable" and pending_finalists:
                extra_round = current.family15
                declarations = () if extra_round is None else extra_round.champions
                if policy.round_stop == "all_attempts":
                    champion = (
                        missing(declarations[-1].payload["measurement_id"], total)
                        if declarations
                        else total
                    )
                else:
                    by_attempt = {
                        r.payload["attempt_id"]: r.payload["measurement_id"] for r in declarations
                    }
                    champion = sum(missing(by_attempt.get(a), total) for a in pending_finalists)
    return dict(
        basis="conditional_same_bound_conditions_complete_scoring_without_retries",
        scope="current_baseline_and_opened_attempts_only",
        baseline_slots=baseline,
        candidate_gate_slots=gate,
        parent_gate_slots=parent,
        candidate_final_slots_if_gates_pass=final,
        champion_slots_for_reached_finalists=champion,
        remaining_refreshes=refreshes,
        refresh_scenario_slots=refresh_slots,
        probes_and_retries="separate",
        noise_diagnostic_slots=0,
    )


def public_source(value: str) -> str:
    """Public policy provenance is bounded and single-line in human output."""
    import json

    escaped = json.dumps(value, ensure_ascii=True)[1:-1]
    return escaped[:160] + ("… (truncated)" if len(escaped) > 160 else "")


def readable_rational(value: Any) -> str:
    """Bounded approximation only; exact values remain the decision authority."""
    import math

    from evolution.tree.family15_schema import decode_rational

    if value is None:
        return "unavailable"
    exact = decode_rational(value)
    try:
        approximate = float(exact)
    except OverflowError:
        return "finite value outside display range (exact rational retained)"
    if not math.isfinite(approximate):
        return "finite value outside display range (exact rational retained)"
    return f"{approximate:.6g} (approx)"


def preflight_lines(explanation: Mapping[str, Any], *, label: str) -> list[str]:
    """Shared bounded presentation of an exact aggregate ceiling explanation."""
    lines = [
        f"{label} preflight · {explanation['status']} · {explanation['basis']} · score-range ceiling, not a power prediction"
    ]
    for scope in explanation["scopes"]:
        if scope["status"] == "skipped":
            continue
        if scope["status"] == "uncalibrated":
            lines.append(
                "causal · calibration will be computed from fresh Parent at decision"
                if explanation["basis"] == "unknown_fresh_reference_envelope"
                else "causal · Parent calibration unavailable"
            )
            continue
        name = _scope_label(scope)
        threshold = readable_rational(scope["threshold"])
        if scope["rule"] == "bound":
            ceiling = readable_rational(scope.get("c_max"))
            requested = readable_rational(scope["confidence"])
            lines.append(
                f"{name} · delta threshold {threshold} · maximal confidence {ceiling} · requested confidence {requested} · {scope['unit_count']} units"
            )
        else:
            value = readable_rational(scope.get("delta_max"))
            lines.append(
                f"{name} · threshold {threshold} · maximal point {value} · {scope['unit_count']} units"
            )
    return lines


def _scope_label(scope: Mapping[str, Any]) -> str:
    name: str = scope["name"]
    if "fallback" in scope:
        return f"{name} · {scope['rule']} (fallback: {scope['fallback']['reason']})"
    if scope["rule"] == "bootstrap":
        return f"{name} · bootstrap"
    return name


def _comparison_lines(comparison: Mapping[str, Any], *, label: str) -> list[str]:
    """Keep historical wording unless this comparison carries new gate facts."""
    readable = readable_rational
    lines = [
        f"{label} · {comparison['outcome']} · deciding {comparison['deciding_layer']} · {comparison['reference']}"
    ]
    calibration = comparison.get("calibration")
    noise = comparison["noise"]
    for scope in comparison["scopes"]:
        threshold = readable(scope["threshold"])
        if scope["name"] == "causal" and calibration is not None and scope["threshold"] is not None:
            from evolution.tree.family15_schema import decode_rational

            expression = (
                "-q"
                if noise["allowance"] is not None and not decode_rational(noise["allowance"])
                else "e - q"
            )
            threshold += f" ({expression})"
        lines.append(
            f"{_scope_label(scope)} · point {readable(scope['point'])} · threshold {threshold} · {scope['declared_count']} cases / {scope['unit_count']} units"
        )
        if scope["enclosure"] is not None:
            lines.append(
                f"{scope['name']} · conditional fixed-scope confidence {readable(scope['confidence'])} · lower endpoint enclosure [{readable(scope['enclosure']['lower'])}, {readable(scope['enclosure']['upper'])}]"
            )
        if scope["rule"] == "bootstrap":
            exact = "unavailable" if scope["exact"] is None else "yes" if scope["exact"] else "no"
            lines.append(
                f"{scope['name']} · repeat-bootstrap confidence {readable(scope['confidence'])} · lower {readable(scope['lower'])} · exact contrast: {exact} · pairing {scope['pairing'] or 'unavailable'}"
            )
    if calibration is not None:
        lines.append(
            f"calibration · {calibration['source']} Parent · q {readable(calibration['allowance'])} · level {readable(calibration['level'])} · {calibration['method']}"
        )
    elif comparison["reason"] == "insufficient_calibration":
        lines.append("causal · Parent calibration unavailable")
    caveats = {
        "intervals_empirical": "empirical repeat intervals, not guaranteed coverage",
        "exact_contrast": "decided on the point; no within-case contrast variation",
        "noise_allowance_empirical": "empirical Parent repeat calibration",
        "noise_not_observed": "no Parent repeat noise observed; q = 0",
    }
    for caveat in comparison.get("caveats", ()):
        lines.append(f"{caveat} · {caveats[caveat]}")
    lines.append(
        f"noise · {noise['source_kind']} · allowance {readable(noise['allowance'])} · Candidate repeat variance {noise['variation']} (score squared)"
    )
    if noise["source"] is not None:
        lines.append("declared allowance source · " + public_source(noise["source"]))
    lines.append(
        f"coverage · {comparison['coverage']['candidate']['admissible']}/{comparison['coverage']['candidate']['required']} Candidate slots · {comparison['excluded_reference_slots']} reference exclusions · {comparison['method']} / {comparison['unit']} · {comparison['verification']}"
    )
    if comparison["selection_bias_possible"]:
        lines.append(
            "selection_bias_possible · " + ", ".join(comparison["selection_bias_possible"])
        )
    return lines


def report_lines(report: Mapping[str, Any]) -> list[str]:
    """Presentation of vetted aggregates only; no arbitrary source/reason text."""
    settings = report["settings"]
    lines = [
        f"independent · {settings['mode']} · {settings['train_cases']} train cases × {settings['repeats']} repeats · {settings['verification']} verification"
    ]
    if report.get("check_modes"):
        lines.insert(
            0,
            "checks: "
            + ", ".join(f"{name}={mode}" for name, mode in report["check_modes"].items()),
        )
    for name, count in report.get("shadow_hits", {}).items():
        lines.append(f"shadow hits: {name} · count {count}")
    for availability in report.get("probe_availability", ()):
        u, t = availability["unreachable"], availability["transport_failures"]
        label = f"round {availability['round']}: probe "
        counts = []
        if u:
            counts.append(f"{u} call{'s' if u != 1 else ''} unreachable")
        if t:
            counts.append(f"{t} transport failure{'s' if t != 1 else ''}")
        if availability["mediator"] == "failed":
            lines.append(
                label + "mediator failed to start; " + (counts[0] if u else "no probe calls")
            )
        elif availability["health"] == "unavailable":
            lines.append(
                label + "unavailable at health check" + ("; " + ", ".join(counts) if counts else "")
            )
        elif counts:
            if u:
                counts[0] = f"{u} unreachable"
            lines.append(label + "calls failed: " + ", ".join(counts))
    noise_allowance = report["settings"]["allowance"]

    lines.append(
        f"noise · {settings['allowance_source_kind']} · allowance {readable_rational(noise_allowance)} · {settings['method']} / {settings['unit']}"
    )
    planned = report["planned_work"]
    lines.append(
        f"initial baseline scenario: {planned['baseline_slots']} scenario slots · {planned['admitted_baseline_imports']} admitted imports · preflight/probes/retries separate"
    )
    lines.append(
        f"conditional Champion: {planned['champion_slots_per_scope']} slots per {planned['champion_scope']} · noise extra measurements: 0"
    )
    if settings["allowance_source"] is not None:
        lines.append("declared allowance source · " + public_source(settings["allowance_source"]))
    if report["local_guidance"] is not None:
        lines.append(
            f"conditional equal-weight local guidance: {report['local_guidance']['minimum_units']} units · not an admission or power claim"
        )
    for diagnostic in report["diagnostics"]:
        lines.append(
            f"diagnostic imports · {diagnostic['included']}/{diagnostic['scheduled']} · {diagnostic['sufficiency']} · score {diagnostic['score']}"
        )
        lines.append(
            f"diagnostic provenance · request {diagnostic['request']} ({diagnostic['request_coverage']}) · independent replay {diagnostic['independent_request_replay']} · stdout {diagnostic['stdout']}"
        )
    for item in report["preflight"]:
        lines.extend(preflight_lines(item["explanation"], label="global"))
        refresh = item["refresh"]
        lines.append(
            f"conditional remaining refreshes: {refresh['remaining_refreshes']} · {refresh['scenario_slots']} scenario slots"
        )
    work = report.get("committed_work")
    if work is not None:
        measured = work["measurement"]
        lines.append(
            f"committed measurement · {measured['scenario_slots']} scenario slots · {measured['target_turns']} Target turns · {measured['transport_retries']} transport retries · {measured['scorer_executions']} Scorer executions · {work['role_requests']} role requests · {measured['imported_slots']} imports"
        )
        probe = work["connection_preflight"]
        if probe is not None:
            lines.append(
                f"separate committed preflight · {probe['scenario_slots']} scenario slots · {probe['target_turns']} Target turns · {probe['scorer_executions']} Scorer executions"
            )
    remaining = report.get("remaining_work")
    if remaining is not None:
        lines.append(
            f"conditional remaining · baseline {remaining['baseline_slots']} · Candidate gate {remaining['candidate_gate_slots']} · Parent gate {remaining['parent_gate_slots']} · Candidate final if gates pass {remaining['candidate_final_slots_if_gates_pass']} · Champion {remaining['champion_slots_for_reached_finalists']} scenario slots"
        )
    for attempt in report["attempts"]:
        not_owed = sum(side["not_owed"] for side in attempt.get("slots", {}).values())
        if not_owed and attempt.get("outcome") == "rejected":
            lines.append(
                f"{attempt['attempt_id']} · causal not_measured · {not_owed} slots not owed after focus rejection"
            )
        comparison = attempt["comparison"]
        gate = attempt.get("gate_comparison")
        if (
            gate is not None
            and attempt.get("global_comparison") is not None
            and (
                gate["mode"] == "statistical"
                or "calibration" in gate
                or any("fallback" in scope for scope in gate["scopes"])
            )
        ):
            # A completed global comparison must not hide the local rule that
            # admitted the finalist. Old-key reports keep their original bytes.
            lines.extend(_comparison_lines(gate, label=f"{attempt['attempt_id']} · gate"))
        if comparison is not None:
            lines.extend(_comparison_lines(comparison, label=attempt["attempt_id"]))
    return lines


def validate_campaign15_projection(report: Any) -> None:
    """Closed public report grammar, independent of private artifact grammar."""
    from evolution.tree.campaign_projections import _identifier, _keys, _require, _sha
    from evolution.tree.family15_schema import (
        BOOTSTRAP_METHOD,
        OUTCOMES,
        REASONS,
        _calibration,
        _caveats,
        _fallback,
        decode_rational,
    )

    def keys(value: Any, names: str) -> None:
        _keys(value, set(names.split()))

    def choice(value: Any, values: str) -> None:
        _require(value in values.split())

    def count(value: Any) -> None:
        _require(type(value) is int and value >= 0)

    def rational(value: Any) -> None:
        if value is not None:
            decode_rational(value)

    def fallback(scope: Any, mode: str, declaration: Any) -> None:
        if "fallback" in scope:
            _fallback(scope["fallback"], scope["name"])
            _require(mode == "confidence" and scope["rule"] == "point" and declaration == "point")

    def cost(value: Any) -> None:
        if value is None:
            return
        keys(
            value,
            "basis baseline_slots candidate_gate_slots candidate_final_slots candidate_total_slots parent_slots champion_slots champion_basis slots_per_refresh connection_preflight_slots probes_and_retries noise_diagnostic_slots simplest_passing_path_slots",
        )
        _require(value["basis"] == "full_scenario_complete_scoring_without_retries")
        _require(value["probes_and_retries"] == "separate")
        choice(
            value["champion_basis"], "once_per_frozen_nonempty_finalist_set per_reached_finalist"
        )
        for k, v in value.items():
            if k not in {"basis", "probes_and_retries", "champion_basis"}:
                count(v)

    def preflight_report(value: Any) -> None:
        if value is None:
            return
        keys(
            value,
            "status basis profile_digest interpretation scopes"
            + (" bound_fallback" if "bound_fallback" in value else ""),
        )
        if "bound_fallback" in value:
            _require(value["bound_fallback"] == "point")
        choice(value["status"], "provisional_or_disabled impossible not_ruled_out")
        choice(value["basis"], "stored_reference unknown_fresh_reference_envelope")
        if value["profile_digest"] is not None:
            _sha(value["profile_digest"])
        _require(value["interpretation"] == "score_range_ceiling_not_a_success_or_power_prediction")
        _require(type(value["scopes"]) is list)
        for scope in value["scopes"]:
            names = {"name", "status"}
            if scope["status"] not in {"skipped", "uncalibrated"}:
                names |= {"rule", "threshold", "strict", "unit_count", "passes"}
                names |= {
                    k
                    for k in ("confidence", "delta_max", "e_max", "c_max", "fallback")
                    if k in scope
                }
            _keys(scope, names)
            choice(scope["name"], "focus causal global")
            choice(scope["status"], "skipped calculated uncalibrated")
            if scope.get("e_max") is not None:
                evidence = scope["e_max"]
                choice(evidence.get("kind"), "finite positive_infinity")
                _keys(evidence, {"kind", "value"} if evidence["kind"] == "finite" else {"kind"})
                if "value" in evidence:
                    rational(evidence["value"])
            for name in ("threshold", "confidence", "delta_max", "c_max"):
                if name in scope:
                    rational(scope[name])
            if "rule" in scope:
                choice(scope["rule"], "point bound off")
                fallback(
                    scope,
                    "confidence" if "bound_fallback" in value else settings["mode"],
                    value.get("bound_fallback", settings.get("bound_fallback")),
                )
                count(scope["unit_count"])
                if "fallback" in scope:
                    _require(scope["unit_count"] > 0)
                _require(type(scope["strict"]) is bool and type(scope["passes"]) is bool)

    def comparison(value: Any, *, scoped: bool = False) -> None:
        if value is None:
            return
        keys(
            value,
            "outcome reason deciding_layer eligible mode scopes coverage excluded_reference_slots method unit confidence_scope reference reference_measurement_id verification selection_bias_possible noise"
            + (" calibration" if "calibration" in value else "")
            + (" caveats" if "caveats" in value else "")
            + (" bound_fallback" if "bound_fallback" in value else ""),
        )
        if "bound_fallback" in value:
            _require(value["bound_fallback"] == "point" and value["mode"] == "confidence")
        _require(value["outcome"] in OUTCOMES and value["reason"] in REASONS)
        choice(value["deciding_layer"], "focus causal global")
        choice(value["mode"], "point confidence unfiltered statistical")
        if "calibration" in value:
            _calibration(value["calibration"])
        if "caveats" in value:
            _caveats(value["caveats"])
        _require(
            type(value["eligible"]) is bool and value["eligible"] == (value["outcome"] == "passed")
        )
        _require(value["method"] in (None, "bounded-weighted-mean/1", BOOTSTRAP_METHOD))
        if value["method"] == BOOTSTRAP_METHOD:
            _require(value["mode"] == "statistical")
        _require(value["unit"] in (None, "case", "group"))
        _require(
            value["confidence_scope"] in (None, "conditional_fixed_scope_independent_bounded_units")
        )
        _require(value["reference"] in (None, "stored_non_contemporaneous", "fresh"))
        _require(
            value["reference_measurement_id"] is None
            or type(value["reference_measurement_id"]) is str
        )
        _require(value["verification"] in (None, "reused_evidence", "fresh_global_evidence"))
        _require(type(value["selection_bias_possible"]) is list)
        for source in value["selection_bias_possible"]:
            choice(source, "focus_selection candidate_selection stored_reference reuse")
        count(value["excluded_reference_slots"])
        keys(value["coverage"], "candidate reference")
        for side in value["coverage"].values():
            keys(side, "required settled admissible")
            for v in side.values():
                count(v)
            _require(side["admissible"] <= side["settled"] <= side["required"])
        _require(type(value["scopes"]) is list)
        for scope in value["scopes"]:
            keys(
                scope,
                "name rule status declared_count unit_count mass excluded_mass unavailable point threshold strict confidence enclosure"
                + (" fallback" if "fallback" in scope else "")
                + (" lower exact pairing" if scope.get("rule") == "bootstrap" else ""),
            )
            choice(scope["name"], "focus causal global")
            choice(scope["rule"], "point bound off bootstrap")
            fallback(
                scope, value["mode"], value.get("bound_fallback", settings.get("bound_fallback"))
            )
            if "fallback" in scope:
                _require(
                    scope["declared_count"] > 0
                    and scope["status"] != "skipped"
                    and (scope["name"] != "causal" or value["reason"] != "insufficient_calibration")
                )
            if scope["rule"] == "bootstrap":
                _require(value["mode"] == "statistical" and scope["name"] != "global")
                _require("intervals_empirical" in value.get("caveats", []))
                rational(scope["lower"])
                _require(scope["enclosure"] is None)
                if scope["status"] == "available":
                    _require(scope["lower"] is not None and type(scope["exact"]) is bool)
                    choice(scope["pairing"], "exact unpaired")
                    if scope["exact"]:
                        _require(
                            scope["lower"] == scope["point"]
                            and "exact_contrast" in value.get("caveats", [])
                        )
                else:
                    _require(scope["lower"] is None and scope["exact"] is None)
                    _require(scope["pairing"] in (None, "exact", "unpaired"))
            choice(scope["status"], "skipped available unavailable not_measured")
            if scope["status"] == "not_measured":
                _require(
                    scoped
                    and scope["name"] == "causal"
                    and scope["point"] is None
                    and scope["unavailable"] is None
                    and value["outcome"] != "passed"
                    and value["deciding_layer"] == "focus"
                )
            for name in ("declared_count", "unit_count"):
                count(scope[name])
            for name in ("mass", "excluded_mass", "point", "threshold", "confidence"):
                rational(scope[name])
            _require(scope["strict"] is None or type(scope["strict"]) is bool)
            _require(
                scope["unavailable"] is None
                or scope["unavailable"] in REASONS
                or scope["unavailable"] == "empty_support"
            )
            if scope["enclosure"] is not None:
                keys(scope["enclosure"], "lower upper steps")
                rational(scope["enclosure"]["lower"])
                rational(scope["enclosure"]["upper"])
                _require(scope["enclosure"]["steps"] == 64)
        noise = value["noise"]
        keys(
            noise,
            "kind allowance source source_kind variation diagnostic units observed_cases extra_measurements",
        )
        _require(noise["kind"] in (None, "negligible", "bounded", "observed"))
        _require(
            noise["source_kind"]
            in (None, "operator_supplied", "assumed_negligible", "diagnostic_only")
        )
        _require(
            noise["source"] is None
            or type(noise["source"]) is str
            and 0 < len(noise["source"].strip()) <= 1024
        )
        rational(noise["allowance"])
        choice(noise["variation"], "not_requested unavailable observed")
        _require(
            noise["diagnostic"] == "candidate_primary_score_repeat_variance"
            and noise["units"] == "score_squared"
            and noise["extra_measurements"] == 0
        )
        count(noise["observed_cases"])

    keys(
        report,
        "next_action settings planned_work local_guidance preflight attempts diagnostics committed_work remaining_work evidence"
        + (" check_modes" if "check_modes" in report else "")
        + (" shadow_hits" if "shadow_hits" in report else "")
        + (" probe_availability" if "probe_availability" in report else ""),
    )
    if "probe_availability" in report:
        _require(type(report["probe_availability"]) is list and bool(report["probe_availability"]))
        for item in report["probe_availability"]:
            keys(item, "round health mediator unreachable transport_failures")
            _require(type(item["round"]) is int and item["round"] >= 1)
            choice(item["health"], "ready unavailable")
            choice(item["mediator"], "started failed")
            count(item["unreachable"])
            count(item["transport_failures"])
    choice(
        report["next_action"],
        "finished repair_and_resume repair_evidence continue_evolve",
    )
    choice(report["evidence"], "snapshot_only unavailable authenticated_publications")
    from evolution.tree.execution.check_modes import CHECK_NAMES

    for block in ("check_modes", "shadow_hits"):
        if block in report:
            _require(type(report[block]) is dict and bool(report[block]))
    for name, mode in report.get("check_modes", {}).items():
        _require(name in CHECK_NAMES and mode in ("shadow", "off"))
    for name, total in report.get("shadow_hits", {}).items():
        _require(name in CHECK_NAMES and type(total) is int and total >= 1)
    settings = report["settings"]
    keys(
        settings,
        "mode verification stability fresh_parent global_rule train_cases repeats train_slots execution_noise allowance allowance_source allowance_source_kind method unit"
        + (" bound_fallback" if "bound_fallback" in settings else "")
        + (" stages" if "stages" in settings else ""),
    )
    if "stages" in settings:
        _require(settings["stages"] == "focus_first")
    choice(settings["mode"], "point confidence unfiltered statistical")
    if "bound_fallback" in settings:
        _require(settings["mode"] == "confidence" and settings["bound_fallback"] == "point")
    choice(settings["verification"], "reuse fresh")
    choice(settings["stability"], "stable unstable")
    choice(settings["global_rule"], "point bound off")
    choice(settings["execution_noise"], "negligible bounded observed")
    choice(
        settings["allowance_source_kind"], "operator_supplied assumed_negligible diagnostic_only"
    )
    _require(
        settings["allowance_source"] is None
        or type(settings["allowance_source"]) is str
        and 0 < len(settings["allowance_source"].strip()) <= 1024
    )
    rational(settings["allowance"])
    _require(
        settings["method"] in (None, "bounded-weighted-mean/1", BOOTSTRAP_METHOD)
        and settings["unit"] in (None, "case", "group")
    )
    _require(type(settings["fresh_parent"]) is bool)
    if settings["mode"] == "statistical":
        _require(settings["repeats"] >= 2 and settings["global_rule"] != "bound")
    if settings["method"] == BOOTSTRAP_METHOD:
        _require(settings["mode"] == "statistical")
    for name in ("train_cases", "repeats", "train_slots"):
        count(settings[name])
    _require(
        settings["repeats"] > 0
        and settings["train_slots"] == settings["train_cases"] * settings["repeats"]
    )
    planned = report["planned_work"]
    keys(
        planned,
        "basis baseline_slots admitted_baseline_imports affected_cases candidate_final_slots_if_gate_passes parent_gate_slots champion_slots_per_scope champion_scope slots_per_refresh connection_preflight probes_and_retries noise_diagnostic_slots",
    )
    _require(planned["basis"] == "full_initial_baseline_and_conditional_future_stage_scenarios")
    _require(planned["affected_cases"] is None)
    choice(planned["champion_scope"], "frozen_nonempty_finalist_set reached_finalist")
    _require(
        planned["connection_preflight"] == "separate_no_measurement_credit"
        and planned["probes_and_retries"] == "separate"
    )
    for name in (
        "baseline_slots",
        "admitted_baseline_imports",
        "champion_slots_per_scope",
        "slots_per_refresh",
        "noise_diagnostic_slots",
    ):
        count(planned[name])
    for name in ("candidate_final_slots_if_gate_passes", "parent_gate_slots"):
        if planned[name] is not None:
            count(planned[name])
    if report["local_guidance"] is not None:
        value = report["local_guidance"]
        keys(value, "status minimum_units assumptions interpretation")
        _require(
            value["status"] == "conditional_guidance"
            and value["interpretation"] == "not_an_admission_or_power_claim"
        )
        _require(
            value["assumptions"]
            == ["equal_independent_unit_weights", "zero_threshold", "all_ones_contrast_envelope"]
        )
        count(value["minimum_units"])
    for name in ("preflight", "attempts", "diagnostics"):
        _require(type(report[name]) is list)
    for value in report["preflight"]:
        keys(value, "round explanation refresh")
        count(value["round"])
        preflight_report(value["explanation"])
        refresh = value["refresh"]
        keys(
            refresh,
            "scenario remaining_refreshes slots_per_refresh scenario_slots retry_ceiling_separate connection_preflight_credit",
        )
        _require(
            refresh["scenario"] == "retain_champion_through_remaining_round_budget"
            and refresh["retry_ceiling_separate"] is True
            and refresh["connection_preflight_credit"] == "separate"
        )
        for name in ("remaining_refreshes", "slots_per_refresh", "scenario_slots"):
            count(refresh[name])
    for value in report["attempts"]:
        keys(
            value,
            "round attempt_id outcome reason comparison gate_comparison global_comparison preflight conditional_cost eligible_final_score"
            + (" slots" if "slots" in value else ""),
        )
        count(value["round"])
        _identifier(value["attempt_id"])
        vocabulary = family15_attempt_vocabulary()
        legacy = {"declined", "failed", "promoted", "retained"}
        outcome = value["outcome"]
        _require(outcome is None or outcome in vocabulary or outcome in legacy)
        if value["reason"] is not None:
            _identifier(value["reason"])
        if outcome in vocabulary:
            _require(value["reason"] in vocabulary[outcome])
        if "slots" in value:
            keys(value["slots"], "candidate" + (" parent" if settings["fresh_parent"] else ""))
            for side in value["slots"].values():
                keys(side, "scheduled dispatched not_owed")
                for total in side.values():
                    count(total)
                _require(side["dispatched"] + side["not_owed"] <= side["scheduled"])
        comparison(value["comparison"], scoped="slots" in value)
        comparison(value["gate_comparison"], scoped="slots" in value)
        comparison(value["global_comparison"])
        preflight_report(value["preflight"])
        cost(value["conditional_cost"])
        rational(value["eligible_final_score"])
        _require(
            value["eligible_final_score"] is None
            or value["comparison"] is not None
            and value["comparison"]["eligible"]
        )
    for value in report["diagnostics"]:
        keys(
            value,
            "scheduled included sufficiency score request request_coverage independent_request_replay stdout",
        )
        count(value["scheduled"])
        count(value["included"])
        choice(value["sufficiency"], "complete partial unavailable")
        choice(value["request_coverage"], "authenticated partial unavailable")
        _require(
            value["score"] == "journal_bound"
            and value["request"] == "capture_time_producer_attestation"
            and value["independent_request_replay"] == "requires_matching_corpus"
            and value["stdout"] == "captured_at_import"
        )
    remaining = report["remaining_work"]
    if remaining is not None:
        keys(
            remaining,
            "basis scope baseline_slots candidate_gate_slots parent_gate_slots candidate_final_slots_if_gates_pass champion_slots_for_reached_finalists remaining_refreshes refresh_scenario_slots probes_and_retries noise_diagnostic_slots",
        )
        _require(
            remaining["basis"]
            == "conditional_same_bound_conditions_complete_scoring_without_retries"
            and remaining["scope"] == "current_baseline_and_opened_attempts_only"
            and remaining["probes_and_retries"] == "separate"
        )
        for name in (
            "baseline_slots",
            "candidate_gate_slots",
            "parent_gate_slots",
            "candidate_final_slots_if_gates_pass",
            "champion_slots_for_reached_finalists",
            "remaining_refreshes",
            "refresh_scenario_slots",
            "noise_diagnostic_slots",
        ):
            if remaining[name] is not None:
                count(remaining[name])
    if report["committed_work"] is not None:
        work = report["committed_work"]
        keys(
            work,
            "measurement connection_preflight role_requests reflector_probe_requests interpretation",
        )
        count(work["role_requests"])
        count(work["reflector_probe_requests"])
        _require(
            work["interpretation"]
            == "journaled_work_not_tokens_currency_or_provider_call_estimates"
        )
        for name in ("measurement", "connection_preflight"):
            if work[name] is None:
                continue
            keys(
                work[name],
                "scenario_slots imported_slots target_turns transport_retries scorer_executions",
            )
            for v in work[name].values():
                count(v)
