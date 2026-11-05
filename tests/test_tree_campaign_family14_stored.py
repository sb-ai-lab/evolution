"""Stored Champion bindings cite the round's immutable complete baseline."""

from __future__ import annotations

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811
import copy

import pytest

from evolution.tree.campaign_schema import SchemaError, champion_member, member_identity
from tests.campaign_drivers import _open_round, observe_barrier  # noqa: F401
from tests.test_tree_campaign_family14 import _before_look, _life
from tests.test_tree_campaign_family14_final import _finalists, _member


def _stable_life(records):
    life = _life(_before_look(records), stability="stable")
    start = len(life.envelopes)
    for row in records[start:]:
        event, payload = row["event"], copy.deepcopy(row["payload"])
        if event == "comparison_look_evaluated":
            payload = life.look_payload(payload["attempt_id"], payload["look"])
        elif event == "attempt_closed":
            payload.update(outcome="finalist", reason="observe_finalist")
        elif event == "comparison_bound":
            payload["margin"] = 0
        life.accept(event, op=row["operation_id"], **payload)
    return life


def _baseline_reference(life):
    return copy.deepcopy(
        next(
            row.payload["evidence"][0] for row in life.envelopes if row.event == "baseline_complete"
        )
    )


def _stable_member(life, position=0):
    payload = _member(life, position)
    reference = _baseline_reference(life)
    payload.update(
        stored_champion=dict(
            measurement_id=_open_round(life)[1].baseline_measurement_id,
            prefix_digest=reference["content_digest"],
        ),
        evidence=[reference],
    )
    return payload


def test_reduced_round_retains_immutable_complete_baseline_reference(observe_barrier):
    life = _stable_life(observe_barrier)
    reference = _baseline_reference(life)
    state = _open_round(life)[1]
    assert state.baseline_prefix == reference["locator"]
    assert state.baseline_prefix_digest == reference["content_digest"]
    assert dict(state.baseline_reference) == reference
    with pytest.raises(TypeError):
        state.baseline_reference["content_digest"] = "f" * 64


def test_stable_binding_registers_only_finalists_and_preserves_reference(observe_barrier):
    life = _stable_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life, stability="stable"))
    payload = _stable_member(life)
    life.accept("final_measurement_bound", **payload)
    state = _open_round(life)[1]
    assert champion_member(state.cohort) is None
    assert member_identity(state.cohort, 0) == ("finalist", life.sealed)
    entry = life.snapshot.measurements[payload["measurement_id"]]
    assert entry["cohort_role"] == "finalist"
    assert dict(entry["stored_champion"]) == payload["stored_champion"]
    assert dict(entry["stored_reference"]) == payload["evidence"][0]
    with pytest.raises(TypeError):
        entry["stored_champion"]["prefix_digest"] = "f" * 64
    # No helper-only facts become part of the persisted finalist-list shape.
    persisted = next(row.payload for row in life.envelopes if row.event == "finalists_bound")
    assert "stored_champion" not in persisted and "stability" not in persisted


@pytest.mark.parametrize("change", ["measurement", "digest", "locator", "run_id"])
def test_stable_member_refuses_a_self_consistent_reference_other_than_round_baseline(
    observe_barrier, change
):
    life = _stable_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life, stability="stable"))
    payload = _stable_member(life)
    reference = payload["evidence"][0]
    if change == "measurement":
        other = life.attempt(life.sealed).measurement_id
        payload["stored_champion"]["measurement_id"] = other
        reference["locator"] = f"measurements/{other}/events.jsonl#bytes=0:100"
    elif change == "digest":
        payload["stored_champion"]["prefix_digest"] = reference["content_digest"] = "f" * 64
    elif change == "locator":
        reference["locator"] = reference["locator"].split("#bytes=")[0] + "#bytes=0:100"
    else:
        reference["run_id"] = "another-run"
    with pytest.raises(SchemaError, match="round's complete baseline"):
        life.accept("final_measurement_bound", **payload)


def test_stable_binding_refuses_a_fresh_champion_member(observe_barrier):
    life = _stable_life(observe_barrier)
    with pytest.raises(SchemaError):
        life.accept("finalists_bound", **_finalists(life, stability="unstable"))


async def test_all_stable_finalists_cite_one_reference(tmp_path):
    from tests.campaign_drivers import EDITED, _live_prefix

    records = await _live_prefix(
        tmp_path,
        gate={"mode": "observe"},
        repeats=1,
        campaign={"attempts_per_round": 2, "attempt_feedback": "none"},
        **EDITED,
    )
    life = _stable_life(records)
    life.accept("finalists_bound", **_finalists(life, stability="stable"))
    first, second = _stable_member(life, 0), _stable_member(life, 1)
    life.accept("final_measurement_bound", **first)
    second["stored_champion"]["prefix_digest"] = "f" * 64
    second["evidence"][0]["content_digest"] = "f" * 64
    with pytest.raises(SchemaError, match="round's complete baseline"):
        life.accept("final_measurement_bound", **second)
    life.accept("final_measurement_bound", **_stable_member(life, 1))


@pytest.mark.parametrize("fresh_parent", [False, True])
async def test_real_stable_final_binding_and_dispatch_omit_champion(
    tmp_path, monkeypatch, fresh_parent
):
    from tests.campaign_drivers import OBSERVE, _cut_hook, _measurement_events, _records
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    config = {**OBSERVE, "gate": {"mode": "observe", "fresh_parent": fresh_parent}}
    _cut_hook(monkeypatch, "after_cohort_measurement_completed")
    with pytest.raises(Crash):
        await family14_run(
            tmp_path,
            monkeypatch,
            config,
            family14_settings={"stability": "stable"},
            campaign={"attempts_per_round": 2},
        )
    run = tmp_path / "run"
    (binding,) = payloads(run, "finalists_bound")
    members = binding["members"]
    assert members == ["m-r01-final-01-a01", "m-r01-final-01-a02"]
    baseline = payloads(run, "baseline_complete")[0]
    bound = payloads(run, "final_measurement_bound")
    assert [row["measurement_id"] for row in bound] == members
    assert all(row["role"] == "finalist" for row in bound)
    assert all(row["evidence"] == baseline["evidence"] for row in bound)
    assert all(
        row["stored_champion"]
        == dict(
            measurement_id=baseline["measurement_id"],
            prefix_digest=baseline["evidence"][0]["content_digest"],
        )
        for row in bound
    )
    assert not (run / "measurements" / "m-r01-final-01-champion").exists()
    for member in members:
        events = _records(_measurement_events(run, member))
        assert sum(row["event"] == "scoring_finished" for row in events) == 2
        assert not any(row["event"] == "sample_imported" for row in events)


@pytest.mark.parametrize("change", ["later_valid_bytes", "changed_prefix", "missing"])
async def test_runtime_reads_exact_stored_prefix_and_classifies_unavailable_bytes(
    tmp_path, monkeypatch, change
):
    from evolution.tree.campaign import _Campaign, _Fault
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    original = _Campaign.hook
    observed = []

    def inspect_reference(self, name):
        if name == "after_finalists_bound":
            state = self.snapshot().rounds[1]
            mid = state.baseline_measurement_id
            reference = self.prefix(mid, round_number=1)
            before = self.cited_baseline_records(1, mid)
            path = self.run_root / reference["locator"].split("#bytes=")[0]
            if change == "later_valid_bytes":
                self.measurements[mid]._require_journal().append(
                    "journal_dispatch_stopped", {"code": "journal_limit_exceeded"}
                )
                assert self.prefix(mid) != reference
                assert self.prefix(mid, round_number=1) == reference
                assert self.cited_baseline_records(1, mid) == before
                observed.append(reference)
            else:
                if change == "missing":
                    path.unlink()
                else:
                    raw = path.read_bytes()
                    path.write_bytes(raw.replace(b'"timestamp":"2', b'"timestamp":"1', 1))
                    assert path.read_bytes() != raw
                with pytest.raises(_Fault) as failure:
                    self.cited_baseline_records(1, mid)
                assert failure.value.terminal == "persistence_failed"
                assert failure.value.owner == "measurement_journal"
                observed.append(reference)
                raise Crash("unavailable stored prefix checked")
        elif name == "after_cohort_measurement_bound":
            raise Crash("stored reference bound before journal birth")
        original(self, name)

    monkeypatch.setattr(_Campaign, "hook", inspect_reference)
    with pytest.raises(Crash):
        await family14_run(tmp_path, monkeypatch, family14_settings={"stability": "stable"})
    assert len(observed) == 1
    run = tmp_path / "run"
    bindings = payloads(run, "final_measurement_bound")
    assert len(bindings) == int(change == "later_valid_bytes")
    if bindings:
        assert bindings[0]["evidence"] == observed


async def test_declared_stable_member_is_born_on_resume_without_rebinding(tmp_path, monkeypatch):
    from tests.campaign_drivers import _cut_hook, _measurement_events
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_bound")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, family14_settings={"stability": "stable"})
    run = tmp_path / "run"
    (binding,) = payloads(run, "final_measurement_bound")
    assert not _measurement_events(run, binding["measurement_id"]).exists()
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_completed")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, family14_settings={"stability": "stable"})
    assert payloads(run, "final_measurement_bound") == [binding]
    assert _measurement_events(run, binding["measurement_id"]).exists()
    assert len(payloads(run, "final_measurement_completed")) == 1


@pytest.mark.parametrize(
    "cut", ["after_baseline_bound", "after_finalists_bound", "after_cohort_measurement_bound"]
)
async def test_stored_binding_uses_complete_baseline_identity_across_continue(
    tmp_path, monkeypatch, cut
):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import EDITED, _continue, _cut_hook, _records, _Resettable
    from tests.campaign_fakes import Crash, Family14Life
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    adapter = _Resettable(used=EDITED["used"])
    options = dict(adapter=adapter, family14_settings={"stability": "stable"})
    with monkeypatch.context() as patch:
        _cut_hook(patch, cut)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, **options)
    run = tmp_path / "run"
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, **options)
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_cohort_measurement_completed")
        with pytest.raises(Crash):
            await family14_run(
                tmp_path, monkeypatch, authorizations=[_continue(refusal.value)], **options
            )
    (baseline,) = payloads(run, "baseline_complete")
    (binding,) = payloads(run, "final_measurement_bound")
    assert binding["evidence"] == baseline["evidence"]
    assert binding["stored_champion"]["measurement_id"] == baseline["measurement_id"]
    rebound = payloads(run, "measurement_rebound")
    if cut == "after_baseline_bound":
        assert baseline["measurement_id"] == "m-r00-champion@e1"
        assert [row["from_measurement_id"] for row in rebound] == ["m-r00-champion"]
    else:
        assert baseline["measurement_id"] == "m-r00-champion"
        assert [row["from_measurement_id"] for row in rebound] == (
            [binding["measurement_id"]] if cut == "after_cohort_measurement_bound" else []
        )
    life = Family14Life(_records(run / "campaign.jsonl"))
    baseline_entry = life.snapshot.measurements[baseline["measurement_id"]]
    final_entry = life.snapshot.measurements[life.snapshot.rounds[1].cohort_current[0]]
    assert final_entry["epoch"] == 1
    assert baseline_entry["epoch"] == int(cut == "after_baseline_bound")
    assert dict(final_entry["stored_reference"]) == baseline["evidence"][0]
    assert dict(final_entry["stored_champion"]) == binding["stored_champion"]
    assert final_entry["stability"] == "stable"
    assert final_entry["round_stop"] == "all_attempts"
    assert final_entry["champion_side"] == "stored"
    assert (
        life.snapshot.rounds[1].baseline_prefix_digest
        == binding["stored_champion"]["prefix_digest"]
    )


def _stored_comparison_payload():
    from tests.campaign_fakes import prefix_reference

    return dict(
        round=1,
        attempt_id="r01-a01",
        finalist_measurement_id="m-r01-final-01-a01",
        champion_measurement_id="m-r00-champion",
        point=0.25,
        lower=0.25,
        level=0.9,
        unavailable=None,
        exact=True,
        pairing="exact",
        accepted=True,
        reason="global_bound_positive",
        champion_side="stored",
        removed_pairs=[],
        removed_cases=[],
        evidence=[prefix_reference("run", "campaign.jsonl#bytes=0:100")],
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(
            point=None,
            lower=None,
            level=None,
            exact=None,
            unavailable="final_incomplete",
            accepted=False,
            reason="final_incomplete",
        ),
        dict(
            point=None,
            lower=None,
            exact=False,
            unavailable="final_incomplete",
            accepted=False,
            reason="final_incomplete",
        ),
        dict(
            lower=None,
            exact=False,
            unavailable="insufficient_repeats",
            accepted=False,
            reason="final_incomplete",
        ),
        dict(
            lower=None,
            exact=False,
            unavailable="insufficient_repeats",
            accepted=True,
            reason="global_point_improved",
        ),
        dict(
            lower=None,
            exact=False,
            unavailable="insufficient_repeats",
            accepted=True,
            reason="global_off",
        ),
        dict(
            lower=None,
            level=None,
            exact=None,
            unavailable="bound_not_requested_in_observe_mode",
            accepted=True,
            reason="global_off",
        ),
    ],
)
def test_stored_global_shape_preserves_every_m2_estimate_state(changes):
    from evolution.tree.campaign_schema import campaign_family_for

    payload = {**_stored_comparison_payload(), **changes}
    campaign_family_for(14).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize(
    "mode,global_mode",
    [
        ("statistical", "off"),
        ("statistical", "point"),
        ("statistical", "bound"),
        ("observe", "off"),
    ],
)
@pytest.mark.parametrize("availability", ["incomplete", "empty_point", "empty_bound", "full"])
def test_real_producer_m2_states_round_trip_through_closed_grammar(mode, global_mode, availability):
    from evolution.tree.campaign_schema import campaign_family_for
    from tests.test_tree_final_stored import compare, rows

    finalist, champion = rows({"z": [0.75] * 3}), rows({"z": [0.5] * 3})
    removed = {"empty_point": (1, 2, 3), "empty_bound": (2, 3)}.get(availability, ())
    result = compare(
        finalist,
        champion,
        groups={"z": ("g", "s")},
        mode=mode,
        global_mode=global_mode,
        complete=availability != "incomplete",
        excluded=frozenset(("z", repeat) for repeat in removed),
    )
    expected_accepted = availability == "full" or (
        availability == "empty_bound" and global_mode != "bound"
    )
    assert result["accepted"] is expected_accepted
    assert result["point"] == (None if availability in ("incomplete", "empty_point") else 0.25)
    payload = {
        **_stored_comparison_payload(),
        **result,
        "finalist_measurement_id": "m-r01-final-01-a01",
        "champion_measurement_id": "m-r00-champion",
    }
    campaign_family_for(14).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize(
    "changes",
    [
        dict(lower=None),
        dict(point=None),
        dict(level=None, exact=None),
        dict(lower=None, unavailable="insufficient_repeats"),
        dict(lower=None, exact=False, unavailable="final_incomplete"),
        dict(point=None, lower=None, exact=False, unavailable="insufficient_repeats"),
        dict(
            point=None,
            lower=None,
            exact=False,
            unavailable="final_incomplete",
            accepted=True,
            reason="global_off",
        ),
        dict(reason="final_incomplete", accepted=False),
    ],
)
def test_stored_global_shape_refuses_inconsistent_null_exact_and_reason_states(changes):
    from evolution.tree.campaign_schema import campaign_family_for

    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload(
            "global_comparison_completed", {**_stored_comparison_payload(), **changes}
        )


@pytest.mark.parametrize("field", ["removed_pairs", "removed_cases"])
def test_stored_removal_summary_is_admitted_without_claiming_row_proofs(field):
    from evolution.tree.campaign_schema import campaign_family_for

    payload = _stored_comparison_payload()
    payload[field] = {"count": 65, "digest": "b" * 64}
    campaign_family_for(14).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize("field", ["removed_pairs", "removed_cases"])
@pytest.mark.parametrize(
    "summary",
    [
        {"count": True, "digest": "b" * 64},
        {"count": 0, "digest": "b" * 64},
        {"count": -1, "digest": "b" * 64},
        {"count": 1.0, "digest": "b" * 64},
        {"count": 65},
        {"digest": "b" * 64},
        {"count": 65, "digest": "b" * 64, "extra": None},
        {"count": 65, "digest": "B" * 64},
        {"count": 65, "digest": "b" * 63},
    ],
)
def test_stored_removal_summary_has_closed_positive_count_and_digest(field, summary):
    from evolution.tree.campaign_schema import campaign_family_for

    payload = _stored_comparison_payload()
    payload[field] = summary
    with pytest.raises(SchemaError):
        campaign_family_for(14).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize("field", ["removed_pairs", "removed_cases"])
def test_fresh_and_old_families_refuse_removal_summaries(field):
    from evolution.tree.campaign_schema import campaign_family_for

    payload = _stored_comparison_payload()
    payload[field] = {"count": 1, "digest": "b" * 64}
    payload["champion_side"] = "fresh"
    payload["champion_measurement_id"] = "m-r01-final-01-champion"
    for family in (13, 14):
        with pytest.raises(SchemaError):
            campaign_family_for(family).validate_payload("global_comparison_completed", payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"removed_pairs": [["z", 0]]},
        {"removed_pairs": [["z", True]]},
        {"removed_pairs": [["z", 4]]},
        {"removed_pairs": [["z", 1], ["z", 1]]},
        {"removed_pairs": [["z", 2], ["z", 1]]},
        {"removed_pairs": [["a", 1], ["z", 1]]},
        {"removed_pairs": [["missing", 1]]},
        {"removed_cases": ["a", "z"]},
        {"removed_cases": ["z", "z"]},
        {"removed_cases": ["missing"]},
    ],
)
def test_stored_removal_lists_validate_corpus_order_duplicates_and_bound_repeats(changes):
    from evolution.tree.campaign_schema import _validate_final_removals

    payload = dict(removed_pairs=[["z", 1], ["a", 3]], removed_cases=["z", "a"])
    corpus = [{"case_id": "z"}, {"case_id": "a"}]
    _validate_final_removals(payload, corpus, repeats=3)
    with pytest.raises(SchemaError):
        _validate_final_removals({**payload, **changes}, corpus, repeats=3)


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
async def test_stable_final_selection_promotes_full_fresh_measurement_and_reuses_it(
    tmp_path, monkeypatch, fresh_parent, mode
):
    from tests.campaign_drivers import OBSERVE, _statistical
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    config = copy.deepcopy(OBSERVE if mode == "observe" else _statistical(mode))
    config["gate"]["fresh_parent"] = fresh_parent
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"stability": "stable"},
        campaign={"rounds": 2},
    )
    assert snapshot.finished["outcome"] == "completed"
    completion = payloads(run, "final_measurement_completed")[0]
    comparison = payloads(run, "global_comparison_completed")[0]
    selection = payloads(run, "selection_decided")[0]
    baseline = payloads(run, "baseline_complete")[0]
    assert comparison["accepted"] is True
    assert comparison["champion_measurement_id"] == baseline["measurement_id"]
    assert comparison["evidence"][1] == baseline["evidence"][0]
    assert comparison["evidence"][0]["kind"] == "journal_record"
    assert selection["measurement_id"] == completion["measurement_id"]
    assert snapshot.champion[1] == selection["candidate_digest"]
    assert selection["ranking"][0]["score"] == completion["point"]
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == selection["measurement_id"]
    assert payloads(run, "round_closed")[0]["outcome"] == "promoted"
    assert not (run / "measurements" / "m-r01-final-01-champion").exists()


@pytest.mark.parametrize("fresh_parent", [False, True])
@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
async def test_stable_retention_keeps_stored_baseline_into_next_round(
    tmp_path, monkeypatch, fresh_parent, mode
):
    from tests.campaign_drivers import (
        OBSERVE,
        _cohort_scorer,
        _flag_on_finalists_bound,
        _statistical,
    )
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    config = copy.deepcopy(OBSERVE if mode == "observe" else _statistical(mode))
    config["gate"]["fresh_parent"] = fresh_parent
    config["scorer"] = _cohort_scorer(
        flag, flip=mode in ("point", "bound"), critical=mode in ("observe", "off")
    )
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        family14_settings={"stability": "stable"},
        campaign={"rounds": 2},
    )
    assert snapshot.finished["outcome"] == "completed"
    selection = payloads(run, "selection_decided")[0]
    assert (selection["attempt_id"], selection["measurement_id"], selection["reason"]) == (
        None,
        "m-r00-champion",
        "no_accepted_finalist",
    )
    assert payloads(run, "round_closed")[0]["outcome"] == "retained"
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == "m-r00-champion"
    assert all(row["role"] == "finalist" for row in payloads(run, "final_measurement_bound"))


@pytest.mark.parametrize("fresh_parent", [False, True])
async def test_stable_no_finalists_creates_no_final_measurement(
    tmp_path, monkeypatch, fresh_parent
):
    from tests.campaign_drivers import STATISTICAL, WORSE_SCORER
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    config = copy.deepcopy({**STATISTICAL, "scorer": WORSE_SCORER})
    config["gate"]["fresh_parent"] = fresh_parent
    run, _ = await family14_run(
        tmp_path, monkeypatch, config, family14_settings={"stability": "stable"}
    )
    assert not payloads(run, "finalists_bound")
    assert not payloads(run, "final_measurement_bound")
    assert not payloads(run, "final_measurement_completed")
    (selection,) = payloads(run, "selection_decided")
    assert (selection["measurement_id"], selection["ranking"], selection["reason"]) == (
        None,
        [],
        "no_finalists",
    )


@pytest.mark.parametrize("mode", ["observe", "off", "point", "bound"])
@pytest.mark.parametrize("availability", ["incomplete", "empty_point", "empty_bound", "full"])
async def test_m2_producer_states_pass_real_stable_lifecycle(
    tmp_path, monkeypatch, mode, availability
):
    """Synthetic authoritative rows exercise M2; replay and estimator are real.

    The whole engine provides the policy, bindings, stages and prefix references.
    Synthetic contrast rows isolate empty sets that a normal baseline may prevent
    from reaching final selection. This is lifecycle coverage, not a row audit.
    """
    from evolution.tree.campaign_schema import cohort_selection
    from evolution.tree.confidence_cohort import global_comparison_record
    from tests.campaign_drivers import OBSERVE, _cut_hook, _records, _statistical
    from tests.campaign_fakes import Crash, Family14Life
    from tests.test_tree_campaign_family14_run import family14_run
    from tests.test_tree_final_stored import rows

    config = copy.deepcopy(OBSERVE if mode == "observe" else _statistical(mode))
    # Observe has enough repeats to distinguish point-only and bound cases too.
    config["repeats"] = 4
    with monkeypatch.context() as patch:
        _cut_hook(patch, "before_global_comparison")
        with pytest.raises(Crash):
            await family14_run(
                tmp_path, monkeypatch, config, family14_settings={"stability": "stable"}
            )
    records = _records(tmp_path / "run" / "campaign.jsonl")
    if availability == "incomplete":
        records[-1]["payload"].update(complete=False, point=None, missing=1)
        assert records[-1]["event"] == "final_measurement_completed"
    life = Family14Life(records)
    state = life.snapshot.rounds[1]
    cohort = state.cohort
    (member,) = state.cohort_current
    stored = life.snapshot.measurements[member]["stored_champion"]
    excluded_repeats = {"empty_point": (1, 2, 3, 4), "empty_bound": (2, 3, 4)}.get(availability, ())
    evidence = [
        life.reference(state.cohort_completed[member]["seq"]),
        dict(state.baseline_reference),
    ]
    result = global_comparison_record(
        cohort,
        state.cohort_completed,
        members=[member],
        position=0,
        finalist_rows=rows({"c1": [0.75] * 4, "c2": [0.75] * 4}),
        champion_rows=rows({"c1": [0.5] * 4, "c2": [0.5] * 4}),
        groups={"c1": ("g", "s"), "c2": ("g", "s")},
        corpus_ids=["c1", "c2"],
        primary="score",
        campaign_seed=life.state.policy.random_seed,
        family_version=14,
        stability="stable",
        stored_champion=stored,
        excluded=frozenset((case, repeat) for case in ("c1", "c2") for repeat in excluded_repeats),
        removal_evidence=evidence,
    )
    life.accept("global_comparison_completed", **result, evidence=evidence)
    state = life.snapshot.rounds[1]
    selection = cohort_selection(state, life.state.policy.global_mode, family_version=14)
    life.accept(
        "selection_decided",
        **selection,
        evidence=[
            life.reference(state.cohort_completed[member]["seq"]),
            life.reference(state.global_comparisons[life.sealed]["seq"]),
        ],
    )
    accepted = availability == "full" or (availability == "empty_bound" and mode != "bound")
    assert result["accepted"] is accepted
    assert (selection["attempt_id"] is not None) is accepted
    if not accepted:
        assert selection["measurement_id"] == state.baseline_measurement_id
    if availability == "empty_bound" and mode != "observe":
        assert (result["point"], result["lower"], result["unavailable"], result["exact"]) == (
            0.25,
            None,
            "insufficient_repeats",
            False,
        )
    if availability == "empty_point":
        assert result["point"] is None and result["reason"] == "final_incomplete"


def _ranking_adapter(*, exclude_c2=False):
    from dataclasses import replace

    from tests.campaign_drivers import EDITED, _RepeatTagged

    class Tagged(_RepeatTagged):
        champions = 0

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                self.champions += 1
                if exclude_c2 and self.champions == 1:
                    instance._lookup = lambda session: (
                        "target_failed" if session.case_id == "c2" else "completed"
                    )
            send = instance.send

            async def tagged(session, message, request_id):
                result = await send(session, message, request_id)
                first = {
                    **result.events[0],
                    "text": f"{result.events[0]['text']} #attempt-{candidate.artifact_id}",
                }
                return replace(result, events=(first, *result.events[1:]))

            instance.send = tagged
            return instance

    return Tagged(used=EDITED["used"])


def _ranking_scorer(flag, scenario):
    from tests.campaign_drivers import VARYING_SCORER

    if scenario == "reduced":
        scores = {
            "r01-a01": {"c1": 0.2, "c2": 1.0, "c3": 0.7},
            "r01-a02": {"c1": 0.4, "c2": 0.5, "c3": 0.7},
        }
    else:
        scores = {
            name: {"c1": score, "c2": score}
            for name, score in (("r01-a01", 0.95), ("r01-a02", 0.7), ("r01-a03", 0.7))
        }
    insert = (
        f"\nfinal = pathlib.Path({str(flag)!r}).exists()\n"
        'attempt = re.search(r"#attempt-(r[0-9]+-a[0-9]+)", view)\n'
        f"scores = {scores!r}\n"
        'if final and attempt:\n    score = scores[attempt.group(1)][payload["case_id"]]\n'
        'critical = not (final and attempt and attempt.group(1) == "r01-a01" '
        f'and {scenario == "reject_tie"!r} and payload["case_id"] == "c2")\n'
    )
    return (
        VARYING_SCORER.replace("json.dump(\n", insert + "json.dump(\n")
        .replace('"critical_pass": True', '"critical_pass": critical')
        .replace(
            'table[(payload["case_id"], edited)]',
            'table[("c2" if payload["case_id"] == "c3" else payload["case_id"], edited)]',
        )
    )


@pytest.mark.parametrize("scenario", ["reduced", "reject_tie"])
async def test_stable_ranking_uses_full_scores_rejects_high_veto_and_breaks_exact_ties(
    tmp_path, monkeypatch, scenario
):
    from tests.campaign_drivers import LENIENT, OBSERVE, _dispatch_order, _flag_on_finalists_bound
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    dispatched = _dispatch_order(monkeypatch)
    count = 2 if scenario == "reduced" else 3
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        {**OBSERVE, "scorer": _ranking_scorer(flag, scenario)},
        adapter=_ranking_adapter(exclude_c2=scenario == "reduced"),
        extra_cases={"c3": {"critical": True, "pass": True}} if scenario == "reduced" else {},
        family14_settings={"stability": "stable"},
        campaign={"attempts_per_round": count, "indeterminate": LENIENT},
    )
    (selection,) = payloads(run, "selection_decided")
    completions = payloads(run, "final_measurement_completed")
    comparisons = payloads(run, "global_comparison_completed")
    assert len(selection["ranking"]) == len(completions) == count
    members = [row["measurement_id"] for row in completions]
    assert dispatched[-1] == [
        (member, case, 1)
        for case in (("c1", "c2", "c3") if scenario == "reduced" else ("c1", "c2"))
        for member in members
    ]
    full_scores = {row["measurement_id"]: row["point"] for row in completions}
    for row in selection["ranking"]:
        assert row["score"] == full_scores[f"m-r01-final-01-{row['attempt_id'].split('-')[1]}"]
    if scenario == "reduced":
        assert selection["ranking"][0]["score"] > selection["ranking"][1]["score"]
        assert comparisons[0]["point"] < comparisons[1]["point"]
        assert all(row["removed_cases"] == ["c2"] for row in comparisons)
        assert all(row["removed_pairs"] == [["c2", 1]] for row in comparisons)
        assert all(row["missing"] == 0 and row["complete"] for row in completions)
        assert selection["attempt_id"] == "r01-a01"
    else:
        assert [row["score"] for row in selection["ranking"]] == [0.7, 0.7, 0.95]
        assert [row["accepted"] for row in selection["ranking"]] == [True, True, False]
        assert selection["ranking"][-1]["reason"] == "critical_failure"
        assert selection["attempt_id"] == "r01-a02"
    assert [row["attempt_id"] for row in selection["ranking"]] == (
        ["r01-a01", "r01-a02"] if scenario == "reduced" else ["r01-a02", "r01-a03", "r01-a01"]
    )
    assert selection["measurement_id"] == f"m-r01-final-01-{selection['attempt_id'].split('-')[1]}"


@pytest.mark.parametrize("fresh_parent", [False, True])
async def test_stable_incomplete_finalist_is_retained_and_both_finalist_chains_are_admitted(
    tmp_path, monkeypatch, fresh_parent
):
    from evolution.tree.campaign import _Campaign
    from tests.campaign_drivers import (
        EDITED,
        LENIENT,
        OBSERVE,
        _arm_on_finalists_bound,
        _CohortFaults,
    )
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    adapter = _CohortFaults(faults={("candidate", "c2", 1): "time_budget"}, used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    seen = []
    original = _Campaign.admitted_measurement

    def admitted(self, measurement_id):
        seen.append(measurement_id)
        return original(self, measurement_id)

    monkeypatch.setattr(_Campaign, "admitted_measurement", admitted)
    config = {**OBSERVE, "gate": {"mode": "observe", "fresh_parent": fresh_parent}}
    run, _ = await family14_run(
        tmp_path,
        monkeypatch,
        config,
        adapter=adapter,
        family14_settings={"stability": "stable"},
        campaign={"indeterminate": LENIENT},
    )
    (completion,) = payloads(run, "final_measurement_completed")
    assert (completion["complete"], completion["point"], completion["missing"]) == (False, None, 1)
    (comparison,) = payloads(run, "global_comparison_completed")
    assert (comparison["accepted"], comparison["reason"]) == (False, "final_incomplete")
    assert {"m-r01-a01", "m-r01-final-01-a01"} <= set(seen)
    assert "m-r00-champion" not in seen
    (selection,) = payloads(run, "selection_decided")
    assert selection["ranking"][0]["score"] is None
    assert selection["measurement_id"] == "m-r00-champion"


async def test_stable_retained_selection_and_next_baseline_preserve_epoch_id(tmp_path, monkeypatch):
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import (
        EDITED,
        _cohort_scorer,
        _continue,
        _cut_hook,
        _flag_on_finalists_bound,
        _Resettable,
        _statistical,
    )
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    flag = tmp_path / "final.flag"
    _flag_on_finalists_bound(monkeypatch, flag)
    adapter = _Resettable(used=EDITED["used"])
    config = {**_statistical("bound"), "scorer": _cohort_scorer(flag, flip=True)}
    options = dict(
        adapter=adapter, family14_settings={"stability": "stable"}, campaign={"rounds": 2}
    )
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_baseline_bound")
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, config, **options)
    adapter.settings = "settings-v2"
    with pytest.raises(EvolveRefused) as refusal:
        await family14_run(tmp_path, monkeypatch, config, **options)
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, config, authorizations=[_continue(refusal.value)], **options
    )
    assert snapshot.finished["outcome"] == "completed"
    selection = payloads(run, "selection_decided")[0]
    assert selection["reason"] == "no_accepted_finalist"
    assert selection["measurement_id"] == "m-r00-champion@e1"
    assert payloads(run, "baseline_bound")[1]["measurement_id"] == selection["measurement_id"]
    assert (
        payloads(run, "global_comparison_completed")[0]["champion_measurement_id"]
        == selection["measurement_id"]
    )


@pytest.mark.parametrize("fault", ["target_failed", "invalid_request", "threshold"])
async def test_stable_final_barrier_blocks_selection_of_complete_subset_and_repairs_gaps(
    tmp_path, monkeypatch, fault
):
    from tests.campaign_drivers import EDITED, LENIENT, STRICT_TARGET, _arm_on_finalists_bound
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_campaign_family14_run import family14_run, payloads

    class SecondFinalistFault(FakeAdapter):
        armed = False

        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            instance._lookup = lambda session: (
                ("target_failed" if fault == "threshold" else fault)
                if self.armed and candidate.artifact_id == "r01-a02" and session.case_id == "c2"
                else "completed"
            )
            return instance

    adapter = SecondFinalistFault(used=EDITED["used"])
    _arm_on_finalists_bound(monkeypatch, adapter)
    campaign = {
        "attempts_per_round": 2,
        **(STRICT_TARGET if fault == "threshold" else {"indeterminate": LENIENT}),
    }
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        adapter=adapter,
        family14_settings={"stability": "stable"},
        campaign=campaign,
    )
    assert snapshot.finished is None and snapshot.suspensions
    assert not payloads(run, "selection_decided")
    assert not payloads(run, "global_comparison_completed")
    assert not payloads(run, "final_measurement_completed")
    (suspended,) = payloads(run, "campaign_suspended")
    assert (
        suspended["evidence"]["evidence_kind"]
        == {
            "target_failed": "final_rows",
            "invalid_request": "fault_rows",
            "threshold": "threshold",
        }[fault]
    )
    if fault == "target_failed":
        assert {row["measurement_id"] for row in suspended["evidence"]["rows"]} == {
            "m-r01-final-01-a02"
        }
        # Repairing the same target resumes the full final stage and both finalists.
        adapter.armed = False
        run, snapshot = await family14_run(
            tmp_path,
            monkeypatch,
            adapter=adapter,
            family14_settings={"stability": "stable"},
            campaign=campaign,
        )
        assert snapshot.finished["outcome"] == "completed"
        assert len(payloads(run, "final_measurement_completed")) == 2
        (selection,) = payloads(run, "selection_decided")
        assert len(selection["ranking"]) == 2 and selection["attempt_id"] == "r01-a01"


def _stable_done(observe_barrier, *, critical=False):
    from tests.campaign_drivers import _completed

    life = _stable_life(observe_barrier)
    life.accept("finalists_bound", **_finalists(life, stability="stable"))
    (member,) = _open_round(life)[1].cohort_current
    life.accept("final_measurement_bound", **_stable_member(life))
    for stage, look in enumerate(life.state.policy.looks, start=1):
        life.stage(member, stage, look)
    life.accept("final_measurement_completed", **_completed(life, member, critical=critical))
    return life


def _stable_global(life):
    from evolution.tree.confidence_cohort import global_comparison_record
    from tests.test_tree_final_stored import rows

    state = _open_round(life)[1]
    (member,) = state.cohort_current
    evidence = [
        life.reference(state.cohort_completed[member]["seq"]),
        dict(state.baseline_reference),
    ]
    result = global_comparison_record(
        state.cohort,
        state.cohort_completed,
        members=[member],
        position=0,
        finalist_rows=rows({"c1": [0.75], "c2": [0.75]}),
        champion_rows=rows({"c1": [0.5], "c2": [0.5]}),
        groups={"c1": ("g", "s"), "c2": ("g", "s")},
        corpus_ids=["c1", "c2"],
        primary="score",
        campaign_seed=life.state.policy.random_seed,
        family_version=14,
        stability="stable",
        stored_champion=life.snapshot.measurements[member]["stored_champion"],
        removal_evidence=evidence,
    )
    return {**result, "evidence": evidence}


@pytest.mark.parametrize(
    "change", ["champion_id", "completion", "stored_digest", "stored_locator", "stored_run", "swap"]
)
def test_stable_global_requires_own_completion_and_exact_governing_stored_reference(
    observe_barrier, change
):
    life = _stable_done(observe_barrier)
    payload = _stable_global(life)
    if change == "champion_id":
        payload["champion_measurement_id"] = life.attempt(life.sealed).measurement_id
    elif change == "completion":
        payload["evidence"][0] = life.reference(1)
    elif change == "stored_digest":
        payload["evidence"][1]["content_digest"] = "f" * 64
    elif change == "stored_locator":
        payload["evidence"][1]["locator"] = (
            payload["evidence"][1]["locator"].split("#")[0] + "#bytes=0:100"
        )
    elif change == "stored_run":
        payload["evidence"][1]["run_id"] = "another-run"
    else:
        payload["evidence"].reverse()
    with pytest.raises(SchemaError):
        life.accept("global_comparison_completed", **payload)
    assert not _open_round(life)[1].global_comparisons
    life.accept("global_comparison_completed", **_stable_global(life))


def test_stable_retained_selection_refuses_substituting_first_finalist(observe_barrier):
    from evolution.tree.campaign_schema import cohort_selection

    life = _stable_done(observe_barrier, critical=True)
    life.accept("global_comparison_completed", **_stable_global(life))
    state = _open_round(life)[1]
    (member,) = state.cohort_current
    payload = {
        **cohort_selection(state, "off", family_version=14),
        "evidence": [
            life.reference(state.cohort_completed[member]["seq"]),
            life.reference(state.global_comparisons[life.sealed]["seq"]),
        ],
    }
    assert payload["measurement_id"] == state.baseline_measurement_id
    with pytest.raises(SchemaError, match="selection names another winner"):
        life.accept("selection_decided", **{**payload, "measurement_id": member})
    life.accept("selection_decided", **payload)
