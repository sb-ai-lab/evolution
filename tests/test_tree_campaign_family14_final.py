"""Family 14 H3 final-stage closed grammar and lifecycle, without runtime mocks."""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
import copy

import pytest

from evolution.tree.campaign_identity import final_measurement_id_for
from evolution.tree.campaign_schema import SchemaError, campaign_family_for
from tests.campaign_drivers import _completed, _open_round
from tests.campaign_drivers import observe_barrier as observe_barrier
from tests.test_tree_campaign_family14 import _closed_life


def _finalists(life, *, stability="unstable"):
    number, state = _open_round(life)
    attempts = sorted(
        (a for a in state.attempts.values() if a.outcome == "finalist"), key=lambda a: a.index
    )
    policy = life.state.policy
    version, digest = life.snapshot.champion
    members = [final_measurement_id_for(number, 1, a.index) for a in attempts]
    if stability == "unstable":
        members.insert(0, final_measurement_id_for(number, 1, None))
    return dict(
        round=number,
        champion={"version": version, "digest": digest},
        finalists=[
            dict(attempt_id=a.attempt_id, candidate_digest=a.candidate_digest, level=None)
            for a in attempts
        ],
        members=members,
        **{"global": policy.global_mode},
        mode=policy.gate_mode,
        repeats=policy.repeats,
        looks=list(policy.looks),
        seed_mode=life.snapshot.identity["target"]["seed_mode"],
        epoch=0,
        order="case_repeat_member",
        evidence=[life.reference(a.closed_seq) for a in attempts],
    )


def _member(life, index):
    number, state = _open_round(life)
    final = state.cohort
    champion = index == 0 and life.state.policy.stability == "unstable"
    finalist = (
        None
        if champion
        else final["finalists"][index - int(life.state.policy.stability == "unstable")]
    )
    payload = dict(
        round=number,
        measurement_id=final["members"][index],
        role="champion" if champion else "finalist",
        attempt_id=None if champion else finalist["attempt_id"],
        candidate_digest=final["champion"]["digest"] if champion else finalist["candidate_digest"],
        identity_digest="a" * 64,
        stability=life.state.policy.stability,
        round_stop=life.state.policy.round_stop,
        champion_side="fresh" if life.state.policy.stability == "unstable" else "stored",
    )
    if life.state.policy.stability == "stable":
        reference = dict(state.baseline_reference)
        payload["stored_champion"] = dict(
            measurement_id=state.baseline_measurement_id, prefix_digest=reference["content_digest"]
        )
        payload["evidence"] = [reference]
    return payload


def _bound(life):
    life.accept("finalists_bound", **_finalists(life))
    members = _open_round(life)[1].cohort["members"]
    for index in range(len(members)):
        life.accept("final_measurement_bound", **_member(life, index))
    return members


def _done(life, *, incomplete=False, critical=False):
    members = _bound(life)
    for stage, look in enumerate(life.state.policy.looks, start=1):
        for mid in members:
            life.stage(mid, stage, look)
    for index, mid in enumerate(members):
        extra = {"critical": critical} if index else {}
        if index and incomplete:
            extra.update(complete=False, point=None, missing=1)
        life.accept("final_measurement_completed", **_completed(life, mid, **extra))
    return members


@pytest.fixture(
    params=[(14, "final_measurement_completed")],
    ids=["family14"],
)
def completed_member(observe_barrier, request):
    """Admit a real member completion in each family's own vocabulary."""
    _family, event = request.param
    life = _closed_life(observe_barrier)
    members = _bound(life)
    for stage, look in enumerate(life.state.policy.looks, start=1):
        for mid in members:
            life.stage(mid, stage, look)
    # Earlier stages cite campaign records, leaving the completion as the
    # member's first journal-prefix citation.
    mid = members[0]
    assert mid not in life.state._cited_ends
    life.accept(event, **_completed(life, mid))
    return life, life.envelopes[-1]


def test_member_completion_cites_its_prefix_and_raises_the_lifecycle_floor(completed_member):
    """Omitting either completion spelling loses its durable prefix and floor."""
    from evolution.tree.campaign_schema import _cited_journal_ends, journal_citations

    life, completion = completed_member
    payload = completion.payload
    mid = payload["measurement_id"]
    assert journal_citations(completion.event, payload) == [
        ("journal_record", payload["evidence"][0]["locator"]),
        ("journal_prefix", f"measurements/{mid}/events.jsonl#bytes=0:100"),
    ]
    assert _cited_journal_ends(completion.event, payload) == {mid: 100}
    assert life.state._cited_ends[mid] == 100


def test_member_completion_places_a_dispatch_before_the_next_suspension(completed_member):
    """Without the completion's upper citation this dispatch has no known window."""
    from evolution.tree.campaign_projections import _Dispatch, _redispatch_windows
    from tests.campaign_drivers import _redispatched

    life, completion = completed_member
    mid = completion.payload["measurement_id"]
    # These shape-validated records isolate the audit's citation-only rule:
    # there is no instance attestation or accounted try to place the dispatch.
    lower = life._envelope("stage_redispatched", _redispatched(life, mid, 50), None)
    suspended = life._envelope(
        "campaign_suspended",
        {
            "reason": "grouping_failed",
            "owner": "campaign",
            "evidence": {"evidence_kind": "stage_record", "seq": 1, "operation_id": "op-1"},
        },
        None,
    )
    windows = _redispatch_windows([lower, completion, suspended])
    dispatch = _Dispatch(
        instance_id="unattested-instance",
        start=50,
        end=100,
        tries=frozenset({("c1", "s1", 0)}),
        rows=frozenset({("c1", "s1")}),
    )
    assert windows.cited[mid] == ((0, 50), (1, 100))
    assert windows.place(mid, dispatch) == 0
    # Losing the upper citation makes the closing suspension ambiguous.
    assert _redispatch_windows([lower, suspended]).place(mid, dispatch) is None


def _comparison(life, *, incomplete=False):
    _, state = _open_round(life)
    final = state.cohort
    champion, finalist = final["members"]
    return dict(
        round=1,
        attempt_id=final["finalists"][0]["attempt_id"],
        finalist_measurement_id=finalist,
        champion_measurement_id=champion,
        point=None if incomplete else 0.25,
        lower=None,
        level=None,
        unavailable="final_incomplete" if incomplete else "bound_not_requested_in_observe_mode",
        pairing=("exact" if incomplete and final["seed_mode"] == "exact" else "unpaired"),
        accepted=not incomplete,
        reason="final_incomplete" if incomplete else "global_off",
        exact=None,
        champion_side="fresh",
        removed_pairs=[],
        removed_cases=[],
        evidence=[
            life.reference(state.cohort_completed[mid]["seq"]) for mid in (finalist, champion)
        ],
    )


def test_final_records_reduce_with_registry_role_distinct_from_member_role(observe_barrier):
    life = _closed_life(observe_barrier)
    champion, finalist = _done(life)
    assert life.snapshot.measurements[champion]["role"] == "final"
    assert life.snapshot.measurements[finalist]["role"] == "final"
    assert life.snapshot.measurements[champion]["cohort_role"] == "champion"
    assert life.snapshot.measurements[finalist]["cohort_role"] == "finalist"
    life.accept("global_comparison_completed", **_comparison(life))
    assert _open_round(life)[1].global_comparisons[life.sealed]["accepted"] is True


@pytest.mark.parametrize("incomplete,critical", [(True, False), (False, True)])
def test_fixed_incomplete_state_and_critical_veto_reduce(observe_barrier, incomplete, critical):
    life = _closed_life(observe_barrier)
    _done(life, incomplete=incomplete, critical=critical)
    payload = _comparison(life, incomplete=incomplete)
    if critical:
        payload.update(accepted=False, reason="critical_failure")
    life.accept("global_comparison_completed", **payload)
    assert _open_round(life)[1].global_comparisons[life.sealed]["accepted"] is False


@pytest.mark.parametrize(
    "field,value",
    [("stability", "stable"), ("round_stop", "first_accepted"), ("champion_side", "stored")],
)
def test_member_settings_follow_v11_policy(observe_barrier, field, value):
    life = _closed_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life))
    payload = _member(life, 0)
    payload[field] = value
    with pytest.raises(SchemaError):
        life.accept("final_measurement_bound", **payload)


def test_stable_shape_requires_stored_reference_and_no_champion_member(observe_barrier):
    from tests.test_tree_campaign_family14 import _before_look, _life

    life = _life(_before_look(observe_barrier), stability="stable")
    life.accept("comparison_look_evaluated", **life.look_payload(life.sealed, 1))
    life.accept(
        "attempt_closed",
        attempt_id=life.sealed,
        outcome="finalist",
        reason="observe_finalist",
        look=1,
        code=None,
    )
    life.accept("finalists_bound", **_finalists(life, stability="stable"))
    payload = _member(life, 0)
    campaign_family_for(14).validate_payload("final_measurement_bound", payload)
    assert payload["role"] == "finalist"
    del payload["stored_champion"]
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("final_measurement_bound", payload)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(members=p["members"] * 2),
        lambda p: p.update(finalists=p["finalists"] * 2),
        lambda p: p.update(members=[s.replace("final-01", "final-00") for s in p["members"]]),
        lambda p: p.update(members=list(reversed(p["members"]))),
        lambda p: p.update(stability="unstable"),
    ],
)
def test_finalists_have_closed_keys_unique_positive_ordinal_ids_and_order(observe_barrier, change):
    life = _closed_life(observe_barrier)
    payload = _finalists(life)
    change(payload)
    with pytest.raises(SchemaError):
        life.accept("finalists_bound", **payload)


@pytest.mark.parametrize(
    "change",
    [
        {"eligible": True},
        {"exact": True},
        {"exact": 0},
        {"exact": False},
        {"unavailable": "degenerate_interval"},
        {"point": float("inf")},
        {"removed_pairs": [["c1", 0]]},
        {"removed_cases": ["c1"]},
        {"reason": "cohort_incomplete", "accepted": False},
        {"champion_side": "stored"},
    ],
)
def test_observe_global_metadata_is_closed_and_fresh(observe_barrier, change):
    life = _closed_life(observe_barrier)
    _done(life)
    payload = _comparison(life)
    payload.update(change)
    with pytest.raises(SchemaError):
        life.accept("global_comparison_completed", **payload)


@pytest.mark.parametrize("exact,lower", [(True, 0.25), (False, 0.05)])
def test_statistical_global_exactness_is_boolean_and_consistent(observe_barrier, exact, lower):
    life = _closed_life(observe_barrier)
    _done(life)
    payload = _comparison(life)
    payload.update(level=0.9, lower=lower, unavailable=None, exact=exact)
    campaign_family_for(14).validate_payload("global_comparison_completed", payload)
    payload["exact"] = True
    payload["lower"] = 0.1
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize("kind", ["list", "string", "payload"])
def test_family14_final_records_are_bounded(observe_barrier, kind):
    life = _closed_life(observe_barrier)
    _done(life)
    payload = _comparison(life)
    if kind == "list":
        payload["champion_side"] = "stored"
        payload["removed_pairs"] = [["c1", index] for index in range(65)]
    elif kind == "string":
        payload["attempt_id"] = "a" * 2049
    else:
        ref = copy.deepcopy(payload["evidence"][0])
        ref["run_id"] = "a" * 2000
        payload["evidence"] = [copy.deepcopy(ref) for _ in range(12)]
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("global_comparison_completed", payload)


def test_finalist_order_is_numerical_and_checked_at_the_envelope(observe_barrier):
    life = _closed_life(observe_barrier)
    payload = _finalists(life)
    seed = payload["finalists"][0]
    payload["finalists"] = [dict(seed, attempt_id=f"r01-a{i:02d}") for i in (99, 100)]
    payload["members"] = [final_measurement_id_for(1, 1, None)] + [
        final_measurement_id_for(1, 1, i) for i in (99, 100)
    ]
    campaign_family_for(14).validate_payload("finalists_bound", payload)
    payload["finalists"].reverse()
    payload["members"][1:] = reversed(payload["members"][1:])
    with pytest.raises(SchemaError, match="finalists are in Attempt index order"):
        campaign_family_for(14).validate_payload("finalists_bound", payload)


def test_inherited_finalist_count_is_not_capped_at_64(observe_barrier):
    life = _closed_life(observe_barrier)
    payload = _finalists(life)
    seed = payload["finalists"][0]
    payload["finalists"] = [dict(seed, attempt_id=f"r01-a{i:02d}") for i in range(1, 71)]
    payload["members"] = [final_measurement_id_for(1, 1, None)] + [
        final_measurement_id_for(1, 1, i) for i in range(1, 71)
    ]
    payload["evidence"] *= 70
    campaign_family_for(14).validate_payload("finalists_bound", payload)


@pytest.mark.parametrize(
    "change",
    [
        {"stored_champion": {"measurement_id": "m-r00-champion", "prefix_digest": "b" * 64}},
        {"role": "final"},
        {"measurement_id": "m-r01-final-00-champion"},
        {"measurement_id": "m-r01-final-1-champion"},
    ],
)
def test_unstable_member_rejects_stored_reference_registry_role_and_invalid_ids(
    observe_barrier, change
):
    life = _closed_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life))
    payload = _member(life, 0)
    payload.update(change)
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("final_measurement_bound", payload)


def test_round_close_requires_actual_opened_count(observe_barrier):
    life = _closed_life(observe_barrier)
    version, digest = life.snapshot.champion
    payload = dict(
        round=1,
        outcome="stalled",
        champion_version=version,
        champion_digest=digest,
        reason=None,
        evidence=None,
        attempts_opened=2,
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
    )
    campaign_family_for(14).validate_payload("round_closed", payload)
    with pytest.raises(SchemaError, match="attempts_opened matches the round's opened Attempts"):
        life.accept("round_closed", **payload)
    for invalid in (None, True, -1, 1.5):
        payload["attempts_opened"] = invalid
        with pytest.raises(SchemaError):
            campaign_family_for(14).validate_payload("round_closed", payload)


def test_optional_champion_accessor_validates_cardinality(observe_barrier):
    from evolution.tree.campaign_schema import champion_member

    life = _closed_life(observe_barrier)
    unstable = _finalists(life)
    stable = _finalists(life, stability="stable")
    assert champion_member(unstable, stability="unstable") == unstable["members"][0]
    assert champion_member(stable, stability="stable") is None
    with pytest.raises(SchemaError):
        champion_member(unstable, stability="stable")
    with pytest.raises(SchemaError):
        champion_member(stable, stability="unstable")


def test_selection_ranking_and_retention_names_are_closed(observe_barrier):
    life = _closed_life(observe_barrier)
    payload = dict(
        round=1,
        attempt_id=None,
        candidate_digest=None,
        dependency_digest=None,
        measurement_id="m-r01-final-01-champion",
        **{"global": "off"},
        reason="no_accepted_finalist",
        ranking=[
            dict(attempt_id=life.sealed, score=0.5, accepted=False, reason="critical_failure")
        ],
        evidence=[life.reference(1)],
    )
    campaign_family_for(14).validate_payload("selection_decided", payload)
    payload["ranking"][0]["eligible"] = payload["ranking"][0].pop("accepted")
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("selection_decided", payload)
    payload.update(reason="no_finalists", ranking=[], measurement_id=None)
    campaign_family_for(14).validate_payload("selection_decided", payload)


@pytest.mark.parametrize("global_mode", ["off", "point", "bound"])
def test_statistical_final_lifecycle_uses_its_own_level(tmp_path, global_mode):
    import asyncio

    from evolution.tree.campaign_schema import finalist_level
    from tests.campaign_drivers import VARYING, _live_prefix
    from tests.test_tree_campaign_family14 import _before_look, _life

    records = asyncio.run(
        _live_prefix(
            tmp_path,
            gate={"global": global_mode, "causal": {"confidence": 0.9, "tolerance": 0.05}},
            repeats=4,
            **VARYING,
        )
    )
    life = _life(_before_look(records))
    for stage, look in enumerate(life.state.policy.looks, start=1):
        if stage > 1:
            life.stages(life.sealed, stage, look)
        life.accept("comparison_look_evaluated", **life.look_payload(life.sealed, look))
    life.accept(
        "attempt_closed",
        attempt_id=life.sealed,
        outcome="finalist",
        reason="gate_passed",
        look=4,
        code=None,
    )
    payload = _finalists(life)
    level = finalist_level(life.state.policy, life.attempt(life.sealed))
    payload["finalists"][0]["level"] = level
    life.accept("finalists_bound", **payload)
    members = _open_round(life)[1].cohort["members"]
    for index in range(len(members)):
        life.accept("final_measurement_bound", **_member(life, index))
    for stage, look in enumerate(life.state.policy.looks, start=1):
        for mid in members:
            life.stage(mid, stage, look)
    for mid in members:
        life.accept("final_measurement_completed", **_completed(life, mid))
    payload = _comparison(life)
    payload.update(
        level=level,
        lower=0.05,
        unavailable=None,
        exact=False,
        reason={
            "off": "global_off",
            "point": "global_point_improved",
            "bound": "global_bound_positive",
        }[global_mode],
    )
    life.accept("global_comparison_completed", **payload)
    assert _open_round(life)[1].global_comparisons[life.sealed]["level"] == level


def test_final_binding_keeps_the_all_attempts_barrier(tmp_path):
    import asyncio

    from tests.campaign_drivers import EDITED, _live_prefix

    records = asyncio.run(
        _live_prefix(
            tmp_path,
            gate={"mode": "observe"},
            repeats=1,
            campaign={"attempts_per_round": 2},
            **EDITED,
        )
    )
    life = _closed_life(records)
    assert len(_open_round(life)[1].attempts) == 1
    with pytest.raises(SchemaError, match="every declared attempt of the round is closed"):
        life.accept("finalists_bound", **_finalists(life))


@pytest.mark.parametrize(
    "field,value",
    [
        ("removed_pairs", [["c1", True]]),
        ("removed_pairs", [["c1", -1]]),
        ("removed_pairs", [["c1", 0], ["c1", 0]]),
        ("removed_cases", ["c1", "c1"]),
        ("removed_pairs", [["c1", "s0"]]),
    ],
)
def test_stored_removal_metadata_has_closed_types_and_unique_entries(observe_barrier, field, value):
    life = _closed_life(observe_barrier)
    _done(life)
    payload = _comparison(life)
    payload.update(champion_side="stored")
    payload[field] = value
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize(
    "stored",
    [
        {"measurement_id": "m-r00-champion", "prefix_digest": "bad"},
        {"measurement_id": "m-r00-champion", "prefix_digest": "b" * 64, "prefix": "extra"},
        {"measurement_id": "m-r00-champion"},
    ],
)
def test_stable_reference_is_closed_and_digest_bound(observe_barrier, stored):
    life = _closed_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life))
    payload = _member(life, 1)
    payload.update(stability="stable", champion_side="stored", stored_champion=stored)
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("final_measurement_bound", payload)


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"removed_cases": ["a", "z"]},
        {"removed_pairs": [["a", 0], ["z", 1]]},
        {"removed_pairs": [["z", 2], ["z", 1]]},
        {"removed_cases": ["missing"]},
        {"removed_pairs": [["missing", 0]]},
    ],
)
def test_removals_follow_corpus_order_not_identifier_order(change):
    from evolution.tree import campaign_schema

    assert hasattr(campaign_schema, "_validate_final_removals"), (
        "corpus-order validation is not implemented"
    )
    payload = dict(removed_cases=["z", "a"], removed_pairs=[["z", 1], ["z", 2], ["a", 1]])
    corpus_cases = [{"case_id": "z"}, {"case_id": "a"}]
    if change is None:
        campaign_schema._validate_final_removals(payload, corpus_cases)
    else:
        payload.update(change)
        with pytest.raises(SchemaError):
            campaign_schema._validate_final_removals(payload, corpus_cases)


def test_stable_binding_requires_and_checks_stored_prefix_evidence(observe_barrier):
    from evolution.tree.campaign_schema import validate_payload
    from tests.campaign_fakes import prefix_reference

    life = _closed_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life))
    payload = _member(life, 1)
    stored_id = _open_round(life)[1].baseline_measurement_id
    reference = prefix_reference(life.run_id, f"measurements/{stored_id}/events.jsonl#bytes=0:100")
    payload.update(
        stability="stable",
        champion_side="stored",
        stored_champion=dict(measurement_id=stored_id, prefix_digest=reference["content_digest"]),
    )
    with pytest.raises(SchemaError):
        validate_payload("final_measurement_bound", payload, final=True)
    payload["evidence"] = [reference]
    validate_payload("final_measurement_bound", payload, final=True)
    for change in (
        {"content_digest": "a" * 64},
        {"kind": "journal_record"},
        {"locator": "measurements/other/events.jsonl#bytes=0:100"},
    ):
        payload["evidence"] = [{**reference, **change}]
        with pytest.raises(SchemaError):
            validate_payload("final_measurement_bound", payload, final=True)
    payload["evidence"] = [{**reference, "version": 99}]
    with pytest.raises(SchemaError):
        validate_payload("final_measurement_bound", payload, final=True)


@pytest.mark.parametrize("event", ["final_measurement_completed", "global_comparison_completed"])
def test_inherited_evidence_has_no_new_list_cap_but_counts_toward_payload_budget(
    observe_barrier, event
):
    from evolution.tree.execution.journal_identity import canonical_json

    life = _closed_life(observe_barrier)
    members = _done(life)
    payload = (
        _completed(life, members[0])
        if event == "final_measurement_completed"
        else _comparison(life)
    )
    reference = dict(life.reference(1), run_id="r", locator="campaign.jsonl#bytes=0:1")
    payload["evidence"] = [copy.deepcopy(reference) for _ in range(65)]
    assert len(canonical_json(payload)) < 16 * 1024
    campaign_family_for(14).validate_payload(event, payload)
    payload["evidence"] *= 2
    assert len(canonical_json(payload)) > 16 * 1024
    with pytest.raises(SchemaError, match="final record exceeds the payload budget"):
        campaign_family_for(14).validate_payload(event, payload)


@pytest.mark.parametrize("event", ["final_measurement_completed", "global_comparison_completed"])
def test_final_records_refuse_old_family_member_ids_at_the_envelope(observe_barrier, event):
    life = _closed_life(observe_barrier)
    champion, _ = _done(life)
    payload = (
        _completed(life, champion) if event == "final_measurement_completed" else _comparison(life)
    )
    field = (
        "measurement_id" if event == "final_measurement_completed" else "finalist_measurement_id"
    )
    payload[field] = "m-r01-cohort-a01"
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload(event, payload)


@pytest.mark.parametrize("event", ["final_measurement_completed", "global_comparison_completed"])
def test_final_current_ids_keep_condition_epoch_suffixes(observe_barrier, event):
    life = _closed_life(observe_barrier)
    champion, _ = _done(life)
    if event == "final_measurement_completed":
        payload = _completed(life, champion)
        payload["measurement_id"] += "@e1"
        payload["prefix"] = f"measurements/{payload['measurement_id']}/events.jsonl#bytes=0:100"
    else:
        payload = _comparison(life)
        payload["finalist_measurement_id"] += "@e1"
        payload["champion_measurement_id"] += "@e1"
    campaign_family_for(14).validate_payload(event, payload)


@pytest.mark.parametrize("position", [0, 1])
def test_final_members_import_no_gate_rows_or_exclusions(observe_barrier, position):
    life = _closed_life(observe_barrier)
    members = _bound(life)
    mid = members[position]
    entry = life.snapshot.measurements[mid]
    assert entry["stages"] == ()
    assert "imported" not in entry
    for member in members:
        life.stage(member, 1, 1)
    with pytest.raises(SchemaError, match="measurements never exclude pairs"):
        life.accept(
            "pairs_excluded",
            round=1,
            measurement_id=mid,
            stage=1,
            count=1,
            by_owner={"target": 1},
            pairs_digest="a" * 64,
            pairs=[
                dict(
                    case_id="c1", sample_id="01", owner="target", reason="target_unstable", tries=1
                )
            ],
            truncated=False,
            evidence=[life.reference(1)],
        )


def test_final_completion_preserves_earlier_prefix_floor(observe_barrier):
    from tests.campaign_fakes import prefix_reference

    life = _closed_life(observe_barrier)
    champion, finalist = _bound(life)
    for mid in (champion, finalist):
        life.stage(mid, 1, 1)
    digest = life.snapshot.champion[1]
    attested = copy.deepcopy(
        next(
            row["payload"]
            for row in observe_barrier
            if row["event"] == "instance_attested"
            and row["payload"]["loaded_candidate_digest"] == digest
        )
    )
    floor = f"measurements/{champion}/events.jsonl#bytes=0:200"
    attested.update(
        measurement_id=champion,
        instance_id="i-final-champion",
        evidence=[prefix_reference(life.run_id, floor)],
    )
    life.accept("instance_attested", **attested)
    with pytest.raises(SchemaError, match="prefix ends before an earlier citation"):
        life.accept("final_measurement_completed", **_completed(life, champion))
    life.accept("final_measurement_completed", **_completed(life, champion, prefix=floor))
