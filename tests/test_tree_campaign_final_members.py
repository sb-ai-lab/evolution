"""Shared final-member addressing and immutable binding history."""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
from dataclasses import replace

import pytest

from evolution.tree import campaign_schema as schema
from evolution.tree.confidence_report import _member
from tests.campaign_drivers import _completed
from tests.campaign_drivers import observe_barrier as observe_barrier
from tests.test_tree_campaign_family14 import _closed_life
from tests.test_tree_campaign_family14_final import _bound, _comparison, _done, _finalists


def _binding(stability, ordinal=1):
    prefix = f"m-r01-final-{ordinal:02}"
    return {
        "stability": stability,
        "finalists": [{"attempt_id": "r01-a01"}, {"attempt_id": "r01-a02"}],
        "members": ([f"{prefix}-champion"] if stability == "unstable" else [])
        + [f"{prefix}-a01", f"{prefix}-a02"],
        "digest": str(ordinal) * 64,
        "seq": ordinal,
    }


@pytest.mark.parametrize("stability", ["stable", "unstable"])
def test_optional_champion_and_finalists_resolve_current_epoch_ids(stability):
    binding = _binding(stability)
    members = tuple(f"{name}@e2" for name in binding["members"])
    assert schema.champion_member(binding, members=members) == (
        members[0] if stability == "unstable" else None
    )
    assert schema.finalist_member(binding, 0, members=members).endswith("a01@e2")
    assert schema.finalist_member(binding, 1, members=members).endswith("a02@e2")
    state = schema.RoundState(1, cohort=binding, cohort_current=members)
    assert schema.current_members(state) == members
    first = _member(state, 0, members[0])
    assert (first["role"], first["attempt_id"]) == (
        ("champion", None) if stability == "unstable" else ("finalist", "r01-a01")
    )


@pytest.mark.parametrize("bad", [[], [None, "x", "y"], "abc", ["x", "x", "y"]])
def test_member_accessors_refuse_malformed_bindings(bad):
    binding = _binding("unstable")
    binding["members"] = bad
    with pytest.raises(schema.SchemaError):
        schema.champion_member(binding)


def test_current_members_refuse_unrelated_or_missing_ids():
    binding = _binding("stable")
    for members in [("unrelated", "m-r01-final-01-a02"), (), None]:
        state = schema.RoundState(1, cohort=binding, cohort_current=members)
        with pytest.raises(schema.SchemaError):
            schema.current_members(state)


@pytest.mark.parametrize("position", [-1, 2, True])
def test_finalist_position_is_an_exact_in_range_integer(position):
    with pytest.raises(schema.SchemaError):
        schema.finalist_member(_binding("stable"), position)


def test_binding_append_preserves_prior_snapshot_and_digest(observe_barrier):
    life = _closed_life(observe_barrier)
    before = life.snapshot
    payload = _finalists(life)
    life.accept("finalists_bound", **payload)
    after = life.snapshot
    assert before.rounds[1].finals == ()
    assert len(after.rounds[1].finals) == 1
    binding = after.rounds[1].finals[0]
    assert binding is after.rounds[1].cohort
    assert binding["digest"] == schema.binding_digest(payload)
    with pytest.raises(TypeError):
        binding["champion"]["digest"] = "b" * 64
    with pytest.raises(TypeError):
        binding["members"][0] = "changed"
    payload["members"].clear()
    assert len(binding["members"]) == 2
    with pytest.raises(schema.SchemaError, match="already bound"):
        life.accept("finalists_bound", **_finalists(life))
    assert life.snapshot.rounds[1].finals == after.rounds[1].finals


def test_independently_constructed_binding_history_is_immutable():
    first, second = _binding("unstable"), _binding("stable", 2)
    before = schema.RoundState(
        1,
        finals=(first,),
        finals_current=(tuple(first["members"]),),
        cohort=first,
        cohort_current=tuple(first["members"]),
    )
    after = replace(
        before,
        finals=(*before.finals, second),
        finals_current=(*before.finals_current, tuple(second["members"])),
        cohort=second,
        cohort_current=tuple(second["members"]),
    )
    first["members"].clear()
    second["finalists"][0]["attempt_id"] = "changed"
    assert len(before.finals) == 1
    assert len(after.finals) == 2
    assert before.cohort["digest"] == "1" * 64
    assert after.cohort["digest"] == "2" * 64
    assert schema.finalist_member(after.cohort, 0).endswith("final-02-a01")
    assert after.cohort["finalists"][0]["attempt_id"] == "r01-a01"


def test_real_continue_preserves_bound_history_and_old_current_view(observe_barrier):
    life = _closed_life(observe_barrier)
    members = _bound(life)
    for mid in members:
        life.stage(mid, 1, 1)
    before = life.snapshot
    moved = life.continue_block(list(members))
    after = life.snapshot
    assert schema.current_members(before.rounds[1]) == tuple(members)
    assert schema.current_members(after.rounds[1]) == tuple(target for _, target in moved)
    assert before.rounds[1].finals == after.rounds[1].finals
    for source, target in moved:
        assert after.measurements[target]["role"] == "final"
        assert before.measurements[source]["frozen"] is False


@pytest.mark.parametrize("incomplete", [False, True])
def test_family14_selection_and_completed_member_guards(observe_barrier, incomplete):
    life = _closed_life(observe_barrier)
    champion, finalist = _done(life, incomplete=incomplete)
    state = life.snapshot.rounds[1]
    assert schema.recorded_complete(life.snapshot.measurements[champion], life.snapshot.rounds)
    assert schema.recorded_complete(life.snapshot.measurements[finalist], life.snapshot.rounds) is (
        not incomplete
    )
    assert schema.running_members(state) == ()
    for mid in (champion, finalist):
        with pytest.raises(schema.SchemaError, match="completed member takes no further try"):
            life.accept(
                "repairs_dispatched", measurement_id=mid, count=1, evidence=[life.reference(1)]
            )
        assert not life.state._champion_measurement(life.snapshot.measurements[mid])
    life.accept("global_comparison_completed", **_comparison(life, incomplete=incomplete))
    selection = schema.cohort_selection(life.snapshot.rounds[1], "off", family_version=14)
    assert selection["measurement_id"] == (champion if incomplete else finalist)
    assert selection["reason"] == ("no_accepted_finalist" if incomplete else "finalist_selected")
    assert "accepted" in selection["ranking"][0]
    state = life.snapshot.rounds[1]
    evidence = [
        life.reference(state.cohort_completed[mid]["seq"]) for mid in (champion, finalist)
    ] + [life.reference(state.global_comparisons[life.sealed]["seq"])]
    life.accept("selection_decided", **selection, evidence=evidence)


@pytest.mark.parametrize("suffix", ["@e0", "@e01", "@e-1", "@e1@e2", "@eunknown"])
def test_current_member_epoch_suffix_is_canonical(suffix):
    binding = _binding("stable")
    members = (binding["members"][0] + suffix, binding["members"][1])
    with pytest.raises(schema.SchemaError):
        schema.finalist_member(binding, 0, members=members)


def test_stability_override_cannot_change_a_reduced_binding():
    with pytest.raises(schema.SchemaError):
        schema.champion_member(_binding("stable"), stability="unstable")


def test_unbound_finalists_cannot_bypass_final_selection(observe_barrier):
    life = _closed_life(observe_barrier)
    with pytest.raises(schema.SchemaError, match="round with finalists"):
        life.accept(
            "selection_decided",
            round=1,
            attempt_id=None,
            candidate_digest=None,
            dependency_digest=None,
            measurement_id=None,
            **{"global": "off"},
            reason="no_finalists",
            ranking=[],
            evidence=[life.reference(1)],
        )


def test_selection_resolves_rebound_finalists_and_preserves_digest(observe_barrier):
    life = _closed_life(observe_barrier)
    original = _bound(life)
    for mid in original:
        life.stage(mid, 1, 1)
    before = life.snapshot
    moved = life.continue_block(list(original))
    members = tuple(target for _, target in moved)
    for mid in members:
        life.stage(mid, 1, 1)
    for mid in members:
        life.accept("final_measurement_completed", **_completed(life, mid))
    payload = dict(
        round=1,
        attempt_id=life.sealed,
        champion_measurement_id=members[0],
        finalist_measurement_id=members[1],
        point=0.25,
        lower=None,
        level=None,
        unavailable="bound_not_requested_in_observe_mode",
        pairing="unpaired",
        accepted=True,
        reason="global_off",
        exact=None,
        champion_side="fresh",
        removed_pairs=[],
        removed_cases=[],
        evidence=[
            life.reference(life.snapshot.rounds[1].cohort_completed[mid]["seq"])
            for mid in (members[1], members[0])
        ],
    )
    life.accept("global_comparison_completed", **payload)
    selection = schema.cohort_selection(life.snapshot.rounds[1], "off", family_version=14)
    assert selection["measurement_id"] == members[1]
    assert life.snapshot.rounds[1].cohort["digest"] == before.rounds[1].cohort["digest"]
    state = life.snapshot.rounds[1]
    evidence = [life.reference(state.cohort_completed[mid]["seq"]) for mid in members] + [
        life.reference(state.global_comparisons[life.sealed]["seq"])
    ]
    life.accept("selection_decided", **selection, evidence=evidence)


def test_stable_drift_lookup_skips_unborn_members_without_offset_loss(observe_barrier, tmp_path):
    from evolution.tree.affected_report import read_cohort_drift_items
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
    assert read_cohort_drift_items(tmp_path, life.snapshot, {}) == {}


def test_drift_side_discovery_recognizes_final_role(observe_barrier):
    from evolution.tree.affected_report import drift_caveats
    from evolution.tree.component_drift import DriftReference

    life = _closed_life(observe_barrier)
    champion, finalist = _bound(life)
    snapshot = life.snapshot
    attempt = replace(
        snapshot.rounds[1].attempts[life.sealed],
        selection={"mode": "component", "fallback_reason": None},
    )
    state = replace(snapshot.rounds[1], attempts={life.sealed: attempt})
    snapshot = replace(snapshot, rounds={1: state})
    reference = DriftReference("candidate", "c" * 64, "a" * 64, 0)
    items = drift_caveats(
        snapshot,
        {champion: [{"components_digest": "b" * 64}], finalist: [{"components_digest": "b" * 64}]},
        {(life.sealed, "cohort_finalist"): reference},
        {1: reference},
    )
    assert [(item["attempt_id"], item["sides"]) for item in items] == [
        (life.sealed, ["cohort_finalist"]),
        (None, ["cohort_champion"]),
    ]
