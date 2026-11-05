"""H10 (F0 §11, R62, choices 7, 16, 17): the family-14 connection-preflight grammar.

The four `connection_preflight_*` records and the `connection` suspension, over
`Family14Life`: closed key sets and bounds, the one-terminal lifecycle, the owed
suspension after a failed terminal, the capacity reservation and the
`CampaignSnapshot.connection` view the dispatch gate (Task 7) reads.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from evolution.tree.campaign_schema import (
    CONNECTION_SUSPENSIONS,
    SUSPENSION_REASONS,
    CampaignLifecycle,
    ConnectionView,
    SchemaError,
    campaign_family_for,
    validate_payload,
)
from evolution.tree.storage.append_log import AppendLog
from evolution.tree.storage.journal_errors import JournalAdmissionRefused
from evolution.tree.target.connection_probe import DETAIL_CODES, OUTCOME_STATUSES, probe_seed
from tests.campaign_fakes import Family14Life
from tests.test_tree_campaign_family14_visibility_change import _change, _created14, _row

STARTED = "connection_preflight_started"
FINISHED = "connection_preflight_finished"
FAILED = "connection_preflight_failed"
INTERRUPTED = "connection_preflight_interrupted"
RECORDS = (STARTED, FINISHED, FAILED, INTERRUPTED)
OP = "cp-0001"
INVOCATION = "0123456789abcdef0123456789abcdef"
BINDING = "b" * 64
TRACE = "e" * 64
#: `Envelope.from_payload` flattens a payload refusal to this text.
ENVELOPE = "^invalid journal record$"


def _life() -> Family14Life:
    return Family14Life([_row(_created14())])


def _reference(op: str = OP, end: int = 321) -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "journal_prefix",
        "run_id": op,
        "locator": f"connection/{op}/events.jsonl#bytes=0:{end}",
        "content_digest": "d" * 64,
    }


def _started(life: Family14Life | None = None, op: str = OP, **overrides: Any) -> dict[str, Any]:
    life = life or _life()
    snapshot = life.snapshot
    identity = snapshot.epochs[-1]
    candidate = snapshot.champion[1]
    binding = overrides.pop("binding_digest", BINDING)
    payload = {
        "operation_id": op,
        "invocation_id": INVOCATION,
        "binding_digest": binding,
        "epoch": len(snapshot.epochs) - 1,
        "case_id": "c1",
        "case_index": 0,
        "target": "1" * 64,
        "environment": "2" * 64,
        "scorer": "3" * 64,
        "metric_contract": identity["metric_contract"],
        "primary_metric": identity["policy"]["metric"]["primary"],
        "candidate_digest": candidate,
        "dependency_digest": snapshot.champion_dependency_digest,
        "probe_seed": probe_seed(binding, candidate),
    }
    payload.update(overrides)
    return payload


def _finished(op: str = OP, **overrides: Any) -> dict[str, Any]:
    payload = {
        "status": "compatible",
        "trace_digest": TRACE,
        "cleanup": "released",
        "evidence": [_reference(op)],
    }
    payload.update(overrides)
    return payload


def _failed(op: str = OP, **overrides: Any) -> dict[str, Any]:
    payload = {
        "status": "connection_incompatible",
        "detail_code": "connection_field_missing",
        "manifest_path": "scorer.metric_contract.target_fields[0]",
        "observed_path": "events[event=assistant_message,last]/content",
        "cause_code": None,
        "trace_digest": TRACE,
        "cleanup": "released",
        "evidence": [_reference(op)],
    }
    payload.update(overrides)
    return payload


def _unavailable(op: str = OP, **overrides: Any) -> dict[str, Any]:
    payload = _failed(
        op,
        status="connection_preflight_unavailable",
        detail_code=None,
        manifest_path=None,
        observed_path=None,
        cause_code="target_failed",
    )
    payload.update(overrides)
    return payload


def _interrupted(op: str = OP, **overrides: Any) -> dict[str, Any]:
    payload = {
        "reason": "cancelled",
        "trace_digest": None,
        "cleanup": "not_needed",
        "evidence": [],
    }
    payload.update(overrides)
    return payload


def _suspension(seq: int, reason: str = "connection_incompatible", op: str = OP) -> dict[str, Any]:
    return {
        "reason": reason,
        "owner": "connection",
        "evidence": {"evidence_kind": "stage_record", "seq": seq, "operation_id": op},
    }


def _payloads() -> dict[str, dict[str, Any]]:
    return {
        STARTED: _started(),
        FINISHED: _finished(),
        FAILED: _failed(),
        INTERRUPTED: _interrupted(),
    }


def _final(event: str, payload: dict[str, Any]) -> None:
    campaign_family_for(14).validate_payload(event, payload)


# --------------------------------------------------------------------------
# Grammar: closed key sets, closed vocabularies, bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event", RECORDS)
def test_family14_admits_each_connection_record(event):
    _final(event, _payloads()[event])


@pytest.mark.parametrize("event", RECORDS)
def test_connection_records_have_closed_key_sets(event):
    payload = _payloads()[event]
    with pytest.raises(SchemaError):
        _final(event, {**payload, "extra": None})
    for key in payload:
        reduced = {name: value for name, value in payload.items() if name != key}
        with pytest.raises(SchemaError):
            _final(event, reduced)


def test_the_key_sets_are_choice_7s():
    from evolution.tree.campaign_schema import _FINAL_KEYS

    assert _FINAL_KEYS[STARTED] == {
        "operation_id",
        "invocation_id",
        "binding_digest",
        "epoch",
        "case_id",
        "case_index",
        "target",
        "environment",
        "scorer",
        "metric_contract",
        "primary_metric",
        "candidate_digest",
        "dependency_digest",
        "probe_seed",
    }
    assert _FINAL_KEYS[FINISHED] == {"status", "trace_digest", "cleanup", "evidence"}
    assert _FINAL_KEYS[FAILED] == {
        "status",
        "detail_code",
        "manifest_path",
        "observed_path",
        "cause_code",
        "trace_digest",
        "cleanup",
        "evidence",
    }
    assert _FINAL_KEYS[INTERRUPTED] == {"reason", "trace_digest", "cleanup", "evidence"}


@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("binding_digest", "B" * 64, "binding_digest is not a sha-256 digest"),
        ("target", "x", "target is not a sha-256 digest"),
        ("environment", None, "environment is not a sha-256 digest"),
        ("scorer", 3, "scorer is not a sha-256 digest"),
        ("metric_contract", "", "metric_contract is not a sha-256 digest"),
        ("candidate_digest", "c" * 63, "candidate_digest is not a sha-256 digest"),
        ("dependency_digest", "c" * 65, "dependency_digest is not a sha-256 digest"),
        ("epoch", -1, "epoch is not a non-negative integer"),
        ("epoch", True, "epoch is not a non-negative integer"),
        ("case_index", 1, "the preflight probes the first train case"),
        ("probe_seed", -1, "probe_seed is not a 31-bit seed"),
        ("probe_seed", 0x80000000, "probe_seed is not a 31-bit seed"),
        ("probe_seed", 1.0, "probe_seed is not a 31-bit seed"),
    ],
)
def test_the_start_payload_is_bounded(field, value, rule):
    with pytest.raises(SchemaError, match=f"^{rule}$"):
        validate_payload(STARTED, {**_started(), field: value}, final=True)


@pytest.mark.parametrize("field", ["operation_id", "invocation_id", "case_id", "primary_metric"])
def test_the_start_identifiers_follow_the_identifier_grammar(field):
    with pytest.raises(SchemaError):
        validate_payload(STARTED, {**_started(), field: "../x"}, final=True)


def test_finished_is_compatible_released_and_traced():
    for overrides, rule in (
        ({"status": "connection_incompatible"}, "a finished preflight is compatible"),
        ({"cleanup": "not_needed"}, "a compatible preflight released its instance"),
        ({"cleanup": "failed"}, "a compatible preflight released its instance"),
        ({"trace_digest": None}, "trace_digest is not a sha-256 digest"),
        ({"evidence": []}, "connection evidence cites the probe journal once"),
    ):
        with pytest.raises(SchemaError, match=f"^{rule}$"):
            validate_payload(FINISHED, _finished(**overrides), final=True)


@pytest.mark.parametrize("status", ["compatible", "connection_unknown", None])
def test_failed_status_is_one_of_the_three_failure_classes(status):
    with pytest.raises(SchemaError, match="^status is not a connection failure class$"):
        validate_payload(FAILED, _failed(status=status), final=True)


def test_the_failure_classes_are_the_probe_outcomes_and_the_suspension_reasons():
    assert set(CONNECTION_SUSPENSIONS) == set(OUTCOME_STATUSES) - {"compatible"}
    assert dict(CONNECTION_SUSPENSIONS) == dict.fromkeys(
        CONNECTION_SUSPENSIONS, ("connection", "stage_record")
    )
    assert list(SUSPENSION_REASONS)[-3:] == list(CONNECTION_SUSPENSIONS)


@pytest.mark.parametrize("code", DETAIL_CODES)
def test_every_closed_detail_code_is_admitted_for_an_incompatibility(code):
    cause = "output_invalid" if code == "connection_scorer_output_invalid" else None
    validate_payload(FAILED, _failed(detail_code=code, cause_code=cause), final=True)


def test_detail_code_is_non_null_exactly_for_an_incompatibility():
    rule = "^detail_code is present exactly for connection_incompatible$"
    with pytest.raises(SchemaError, match=rule):
        validate_payload(FAILED, _failed(detail_code=None), final=True)
    with pytest.raises(SchemaError, match=rule):
        validate_payload(FAILED, _unavailable(detail_code="connection_field_missing"), final=True)
    inconclusive = _unavailable(
        status="connection_preflight_inconclusive", cause_code="turn_budget"
    )
    validate_payload(FAILED, inconclusive, final=True)
    with pytest.raises(SchemaError, match=rule):
        validate_payload(
            FAILED, {**inconclusive, "detail_code": "connection_field_type"}, final=True
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"detail_code": "connection_field_wrong"},
        {"manifest_path": None},
        {"observed_path": ""},
        {"manifest_path": "x" * 2049},
        {"observed_path": "é" * 1025},
        {"cause_code": "output_invalid"},
        {"detail_code": "connection_scorer_output_invalid", "cause_code": None},
        {"detail_code": "connection_scorer_output_invalid", "cause_code": "timeout"},
    ],
)
def test_an_incompatibility_carries_a_closed_bounded_diagnostic(overrides):
    with pytest.raises(SchemaError):
        validate_payload(FAILED, _failed(**overrides), final=True)


def test_a_path_of_exactly_2048_bytes_is_admitted():
    validate_payload(FAILED, _failed(manifest_path="x" * 2048), final=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"cause_code": None},
        {"cause_code": "bad code!"},
        {"cause_code": "x" * 65},
        {"manifest_path": "scorer"},
        {"observed_path": "scorer.output"},
    ],
)
def test_unavailable_and_inconclusive_carry_only_a_bounded_cause(overrides):
    with pytest.raises(SchemaError):
        validate_payload(FAILED, _unavailable(**overrides), final=True)


def test_a_failed_cleanup_is_exactly_the_cleanup_failed_cause():
    unsettled = _unavailable(cause_code="cleanup_failed", cleanup="failed")
    validate_payload(FAILED, unsettled, final=True)
    rule = "^a failed cleanup is the cleanup_failed cause$"
    with pytest.raises(SchemaError, match=rule):
        validate_payload(FAILED, {**unsettled, "cleanup": "released"}, final=True)
    with pytest.raises(SchemaError, match=rule):
        validate_payload(FAILED, _unavailable(cleanup="failed"), final=True)
    # `cleanup_failed` is outside the inconclusive class's closed cause set
    # (H10 review r1 item 5), so the detail itself is refused first.
    with pytest.raises(SchemaError, match="^the connection failure detail is not well formed$"):
        validate_payload(
            FAILED,
            _unavailable(
                status="connection_preflight_inconclusive",
                cause_code="cleanup_failed",
                cleanup="failed",
            ),
            final=True,
        )
    with pytest.raises(SchemaError, match="^cleanup is not in the registry$"):
        validate_payload(FAILED, _failed(cleanup="leaked"), final=True)


def test_only_an_unsettled_recovery_may_lack_the_trace():
    """Task 5 concern 4: a crash before the summary leaves no trace to digest."""

    validate_payload(
        FAILED,
        _unavailable(cause_code="cleanup_failed", cleanup="failed", trace_digest=None),
        final=True,
    )
    with pytest.raises(SchemaError, match="^trace_digest is not a sha-256 digest$"):
        validate_payload(FAILED, _failed(trace_digest=None), final=True)
    with pytest.raises(SchemaError, match="^trace_digest is not a sha-256 digest$"):
        validate_payload(FAILED, _unavailable(trace_digest=None), final=True)


def test_a_failed_terminal_cites_at_most_the_one_probe_journal():
    validate_payload(
        FAILED,
        _unavailable(cause_code="cleanup_failed", cleanup="failed", trace_digest=None, evidence=[]),
        final=True,
    )
    with pytest.raises(SchemaError, match="^connection evidence cites the probe journal once$"):
        validate_payload(FAILED, _failed(evidence=[_reference(), _reference()]), final=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "cancelled", "cleanup": "released", "trace_digest": TRACE},
        {"reason": "interrupted", "cleanup": "failed", "evidence": [_reference()]},
        {"reason": "interrupted", "cleanup": "not_needed", "evidence": []},
    ],
)
def test_interrupted_admits_a_null_trace_failed_cleanup_and_no_journal(overrides):
    validate_payload(INTERRUPTED, _interrupted(**overrides), final=True)


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"reason": "stopped"}, "interruption reason is not in the registry"),
        ({"cleanup": "leaked"}, "cleanup is not in the registry"),
        ({"trace_digest": "x"}, "trace_digest is not a sha-256 digest"),
        ({"evidence": None}, "connection evidence cites the probe journal once"),
        (
            {"evidence": [_reference(), _reference()]},
            "connection evidence cites the probe journal once",
        ),
    ],
)
def test_interrupted_is_closed(overrides, rule):
    with pytest.raises(SchemaError, match=f"^{rule}$"):
        validate_payload(INTERRUPTED, _interrupted(**overrides), final=True)


@pytest.mark.parametrize(
    "reference",
    [
        {**_reference(), "kind": "journal_record"},
        {**_reference(), "locator": f"measurements/{OP}/events.jsonl#bytes=0:9"},
        {**_reference(), "locator": "connection/other/events.jsonl#bytes=0:9"},
        {**_reference(), "locator": f"connection/{OP}/events.jsonl#bytes=1:9"},
        {**_reference(), "locator": f"connection/{OP}/events.jsonl#bytes=0:0"},
        {**_reference(), "locator": f"connection/{OP}/summary.json#bytes=0:9"},
    ],
)
def test_evidence_is_a_prefix_of_the_named_probe_journal(reference):
    with pytest.raises(
        SchemaError, match="^connection evidence is not the probe journal's prefix$"
    ):
        validate_payload(FINISHED, _finished(evidence=[reference]), final=True)


def test_a_malformed_reference_is_refused():
    with pytest.raises(SchemaError):
        validate_payload(FINISHED, _finished(evidence=[{**_reference(), "version": 2}]), final=True)


def test_connection_records_are_bounded_at_16_kib():
    from evolution.tree.campaign_schema import _FINAL_BOUNDED_RECORDS

    assert set(RECORDS) <= _FINAL_BOUNDED_RECORDS


@pytest.mark.parametrize("reason", sorted(CONNECTION_SUSPENSIONS))
def test_family14_admits_the_connection_suspension_shape(reason):
    _final("campaign_suspended", _suspension(5, reason))
    with pytest.raises(SchemaError):
        _final("campaign_suspended", {**_suspension(5, reason), "owner": "target"})
    threshold = {
        **_suspension(5, reason),
        "evidence": {"evidence_kind": "threshold", "crossing": {}, "window_start": None},
    }
    with pytest.raises(SchemaError):
        _final("campaign_suspended", threshold)


def test_the_start_is_a_start_only_in_family14():
    assert campaign_family_for(14).is_start(STARTED)
    for event in (FINISHED, FAILED, INTERRUPTED):
        assert not campaign_family_for(14).is_start(event)
    for event in ("editor_started", "reflection_started", "grouping_started"):
        assert campaign_family_for(14).is_start(event)


# --------------------------------------------------------------------------
# Lifecycle: one start, one terminal, the owed suspension
# --------------------------------------------------------------------------


def test_a_compatible_preflight_closes_once():
    life = _life()
    seq = life.accept(STARTED, op=OP, **_started(life))
    view = life.snapshot.connection
    assert view.attempts == 1
    assert view.open is not None and view.open["operation_id"] == OP and view.open["seq"] == seq
    assert view.open["outcome"] is None and view.open["terminal"] is None
    closed = life.accept(FINISHED, op=OP, **_finished())
    view = life.snapshot.connection
    assert view.open is None and view.owes_suspension is None
    assert view.latest[BINDING]["outcome"] == "compatible"
    assert view.latest[BINDING]["terminal"]["seq"] == closed
    assert "evidence" not in view.latest[BINDING]["terminal"]
    assert view.latest[BINDING]["operation_id"] == OP
    assert view.latest[BINDING]["invocation_id"] == INVOCATION
    assert "a" * 64 not in view.latest
    with pytest.raises(SchemaError, match="^a terminal needs its start$"):
        life.accept(FINISHED, op=OP, **_finished())
    with pytest.raises(SchemaError, match="^operation id is used once$"):
        life.accept(STARTED, op=OP, **_started(life))


def test_the_payload_operation_id_is_the_envelopes():
    life = _life()
    with pytest.raises(SchemaError, match="^the payload operation id must match the envelope$"):
        life.accept(STARTED, op="cp-other", **_started(life))
    assert life.snapshot.connection == ConnectionView()


def test_a_second_start_while_one_is_open_is_refused():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    with pytest.raises(SchemaError, match="^a connection preflight is already open$"):
        life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    life.accept(INTERRUPTED, op=OP, **_interrupted())
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    assert life.snapshot.connection.attempts == 2


def test_a_terminal_needs_a_connection_start():
    life = _life()
    with pytest.raises(SchemaError, match="^a terminal needs its start$"):
        life.accept(FINISHED, op=OP, **_finished())


def test_a_terminal_cites_its_own_probe_journal():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    with pytest.raises(SchemaError, match="^the preflight evidence cites another probe journal$"):
        life.accept(FINISHED, op=OP, **_finished(evidence=[_reference("cp-other")]))


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"epoch": 1}, "the preflight names another condition epoch"),
        ({"metric_contract": "f" * 64}, "the preflight names another metric contract"),
        ({"primary_metric": "other"}, "the preflight names another primary metric"),
        ({"candidate_digest": "f" * 64}, "the preflight probes another Champion"),
        ({"dependency_digest": "f" * 64}, "the preflight probes another Champion"),
        ({"probe_seed": 7}, "the probe seed is not the binding's"),
    ],
)
def test_the_start_binds_the_current_epoch_champion_and_seed(overrides, rule):
    life = _life()
    payload = _started(life)
    if "candidate_digest" in overrides:
        # Keep the seed consistent so only the Champion rule can refuse it.
        overrides["probe_seed"] = probe_seed(BINDING, overrides["candidate_digest"])
    payload.update(overrides)
    with pytest.raises(SchemaError, match=f"^{rule}$"):
        life.accept(STARTED, op=OP, **payload)


def test_the_start_names_the_epoch_a_comparable_change_opened():
    life = _life()
    applied, rebased = _change(life.envelopes[0].payload, target=True, effect="continue")
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **rebased)
    assert len(life.snapshot.epochs) == 2
    with pytest.raises(SchemaError, match="^the preflight names another condition epoch$"):
        life.accept(STARTED, op=OP, **_started(life, epoch=0))
    life.accept(STARTED, op=OP, **_started(life))
    assert life.snapshot.connection.open["epoch"] == 1


def _failed_life(payload: dict[str, Any] | None = None) -> tuple[Family14Life, int]:
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    seq = life.accept(FAILED, op=OP, **(payload or _failed()))
    return life, seq


def test_a_failed_terminal_owes_its_suspension():
    life, seq = _failed_life()
    view = life.snapshot.connection
    assert view.open is None
    assert view.owes_suspension == {
        "seq": seq,
        "operation_id": OP,
        "reason": "connection_incompatible",
    }
    entry = view.latest[BINDING]
    assert entry["outcome"] == "connection_incompatible"
    assert entry["terminal"]["detail_code"] == "connection_field_missing"
    suspended = life.accept("campaign_suspended", **_suspension(seq))
    view = life.snapshot.connection
    assert view.owes_suspension is None
    assert life.snapshot.suspended
    assert life.snapshot.suspensions[-1]["seq"] == suspended
    assert life.snapshot.suspensions[-1]["owner"] == "connection"
    # A resume probes again under a fresh operation id.
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    life.accept(FINISHED, op="cp-0002", **_finished("cp-0002"))
    assert life.snapshot.connection.latest[BINDING]["operation_id"] == "cp-0002"
    assert [entry["outcome"] for entry in life.snapshot.connection.operations] == [
        "connection_incompatible",
        "compatible",
    ]


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        ("connection_preflight_unavailable", "target_failed"),
        ("connection_preflight_inconclusive", "turn_budget"),
    ],
)
def test_each_failure_class_is_suspended_under_its_own_reason(status, cause):
    life, seq = _failed_life(_unavailable(status=status, cause_code=cause))
    with pytest.raises(
        SchemaError, match="^the owed connection suspension names the failed status$"
    ):
        life.accept("campaign_suspended", **_suspension(seq, "connection_incompatible"))
    life.accept("campaign_suspended", **_suspension(seq, status))


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        ("connection_preflight_unavailable", "made_up_code"),
        ("connection_preflight_unavailable", "target_unavailable"),
        ("connection_preflight_unavailable", "turn_budget"),
        ("connection_preflight_inconclusive", "target_failed"),
        ("connection_preflight_inconclusive", "made_up_code"),
    ],
)
def test_a_failed_terminal_with_a_cause_outside_its_status_is_refused(status, cause):
    """The cause vocabulary is closed per failure class in family 14."""

    with pytest.raises(SchemaError, match="^the connection failure detail is not well formed$"):
        validate_payload(FAILED, _unavailable(status=status, cause_code=cause), final=True)
    known = "infrastructure" if status == "connection_preflight_unavailable" else "turn_budget"
    validate_payload(FAILED, _unavailable(status=status, cause_code=known), final=True)
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    with pytest.raises(SchemaError):
        life.accept(FAILED, op=OP, **_unavailable(status=status, cause_code=cause))
    life.accept(FAILED, op=OP, **_unavailable(status=status, cause_code=known))


def test_an_unsettled_recovery_is_suspended_as_unavailable():
    payload = _unavailable(cause_code="cleanup_failed", cleanup="failed", trace_digest=None)
    life, seq = _failed_life(payload)
    life.accept("campaign_suspended", **_suspension(seq, "connection_preflight_unavailable"))


def test_only_the_matching_suspension_follows_a_failed_terminal():
    life, seq = _failed_life()
    created = life.envelopes[0].payload
    applied, _ = _change(created, target=True, effect="continue")
    _, rebased = _change(created, reflector=True)
    owed = "^only the owed connection suspension follows a failed preflight$"
    with pytest.raises(SchemaError, match=owed):
        life.accept("authorization_applied", **applied)
    with pytest.raises(SchemaError, match=owed):
        life.accept("identity_rebased", **rebased)
    with pytest.raises(SchemaError, match=owed):
        life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    with pytest.raises(SchemaError, match=owed):
        life.accept(
            "campaign_finished",
            outcome="stopped",
            reason="cancelled",
            continuation={"kind": "linked_run", "reason": "cancelled"},
            evidence=None,
        )
    with pytest.raises(
        SchemaError, match="^the owed connection suspension names the failed preflight$"
    ):
        life.accept("campaign_suspended", **_suspension(seq - 1))
    with pytest.raises(
        SchemaError, match="^the owed connection suspension names the failed preflight$"
    ):
        life.accept("campaign_suspended", **_suspension(seq, op="cp-other"))
    with pytest.raises(
        SchemaError, match="^the owed connection suspension names the failed status$"
    ):
        life.accept("campaign_suspended", **_suspension(seq, "connection_preflight_unavailable"))
    with pytest.raises(SchemaError):
        life.accept("campaign_suspended", **{**_suspension(seq), "owner": "target"})
    assert len(life.envelopes) == seq
    life.accept("campaign_suspended", **_suspension(seq))
    # The suspension discharged the debt: an operator's consequence records follow.
    life.accept("authorization_applied", **applied)
    life.accept("identity_rebased", **_change(created, target=True, effect="continue")[1])


def test_a_connection_suspension_names_the_immediately_preceding_failed_terminal():
    life = _life()
    with pytest.raises(SchemaError, match="^a connection suspension follows its failed preflight$"):
        life.accept("campaign_suspended", **_suspension(7))
    start = life.accept(STARTED, op=OP, **_started(life))
    with pytest.raises(SchemaError, match="^a connection suspension follows its failed preflight$"):
        life.accept("campaign_suspended", **_suspension(start - 1))
    with pytest.raises(SchemaError, match="^stage_record names no failing record$"):
        life.accept("campaign_suspended", **_suspension(start))
    life.accept(FINISHED, op=OP, **_finished())
    with pytest.raises(SchemaError, match="^stage_record names no failing record$"):
        life.accept("campaign_suspended", **_suspension(start + 1))


def test_an_interrupted_preflight_owes_nothing():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    life.accept(INTERRUPTED, op=OP, **_interrupted(reason="interrupted", cleanup="failed"))
    view = life.snapshot.connection
    assert view.owes_suspension is None and view.open is None
    assert view.latest[BINDING]["outcome"] == "interrupted"
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))


def test_campaign_finished_is_refused_while_a_preflight_is_open():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    with pytest.raises(
        SchemaError, match="^campaign_finished waits for the open connection preflight$"
    ):
        life.accept(
            "campaign_finished",
            outcome="stopped",
            reason="cancelled",
            continuation={"kind": "linked_run", "reason": "cancelled"},
            evidence=None,
        )


def test_the_view_keeps_the_latest_operation_per_binding():
    life = _life()
    other = "a" * 64
    life.accept(STARTED, op=OP, **_started(life))
    life.accept(FINISHED, op=OP, **_finished())
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002", binding_digest=other))
    view = life.snapshot.connection
    assert view.open["operation_id"] == "cp-0002"
    assert view.latest[BINDING]["operation_id"] == OP
    assert view.latest[other]["operation_id"] == "cp-0002"
    assert view.latest[other]["outcome"] is None
    assert view.latest[BINDING]["outcome"] == "compatible"
    assert view.attempts == 2
    assert [entry["binding_digest"] for entry in view.operations] == [BINDING, other]


def test_the_view_is_immutable():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    view = life.snapshot.connection
    assert isinstance(view.latest, MappingProxyType)
    assert isinstance(view.open, MappingProxyType)
    with pytest.raises(TypeError):
        view.open["outcome"] = "compatible"  # type: ignore[index]
    with pytest.raises(AttributeError):
        view.open = None  # type: ignore[misc]


def test_the_view_carries_no_admission_query():
    """H10 review r1 item 4: the campaign reads `latest` itself; the view has
    no unused `admitted()` of its own to drift from that reading."""

    assert not hasattr(ConnectionView, "admitted")
    assert _life().snapshot.connection == ConnectionView()


# --------------------------------------------------------------------------
# Capacity (R62, choice 16)
# --------------------------------------------------------------------------


def test_reserved_records_cover_the_terminal_and_a_possible_suspension():
    life = _life()
    state = life.state
    assert state.reserved_records == 1  # campaign_finished
    life.accept(STARTED, op=OP, **_started(life))
    assert state.reserved_records == 3  # + the terminal + a possible suspension
    seq = life.accept(FAILED, op=OP, **_failed())
    assert state.reserved_records == 2  # + the owed suspension
    life.accept("campaign_suspended", **_suspension(seq))
    assert state.reserved_records == 1
    life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    assert state.reserved_records == 3
    life.accept(FINISHED, op="cp-0002", **_finished("cp-0002"))
    assert state.reserved_records == 1


def test_a_transition_reserves_what_its_record_leaves_owed():
    life = _life()
    envelope = life._envelope(STARTED, _started(life), OP)
    assert life.state.prepare(envelope).reserved_records == 3
    life.accept(STARTED, op=OP, **_started(life))
    failed = life._envelope(FAILED, _failed(), OP)
    assert life.state.prepare(failed).reserved_records == 2
    finished = life._envelope(FINISHED, _finished(), OP)
    assert life.state.prepare(finished).reserved_records == 1


def _open_log(tmp_path, life: Family14Life, limit: int) -> AppendLog:
    prefix = b"".join(envelope.encode() for envelope in life.envelopes)
    log = AppendLog.open(
        tmp_path,
        file_name="campaign.jsonl",
        lock_name="campaign.lock",
        create=True,
        state_factory=lambda: CampaignLifecycle(selector=None, adaptive=True, family_version=14),
        max_record_bytes=RECORD,
        max_log_bytes=len(prefix) + limit,
        secrets=(),
        envelope_family=campaign_family_for(14),
        require_empty=True,
        initial=prefix,
    )
    log.scan()
    return log


RECORD = 64 * 1024


def _sized(life: Family14Life) -> tuple[Any, Any, Any]:
    """The start, failed terminal and suspension envelopes, with fixed bytes."""

    shadow = Family14Life([envelope.to_payload() for envelope in life.envelopes])
    start = shadow._envelope(STARTED, _started(shadow), OP)
    shadow.state.accept(start)
    shadow.envelopes.append(start)
    failed = shadow._envelope(FAILED, _failed(), OP)
    shadow.state.accept(failed)
    shadow.envelopes.append(failed)
    suspended = shadow._envelope("campaign_suspended", _suspension(len(shadow.envelopes)), None)
    return start, failed, suspended


@pytest.mark.parametrize("room", [-1, 0])
def test_a_start_near_the_limit_is_refused_before_any_external_work(tmp_path, room):
    """The start is refused (`JournalAdmissionRefused`, C§6.1) unless its
    terminal and a possible suspension still fit behind it; then they do."""

    life = _life()
    start, failed, suspended = _sized(life)
    # After the start: its terminal, a possible suspension and campaign_finished.
    limit = len(start.encode()) + 3 * RECORD + room
    log = _open_log(tmp_path, life, limit)
    try:
        if room < 0:
            with pytest.raises(JournalAdmissionRefused):
                log.append_envelope(start)
            assert log.healthy
            return
        log.append_envelope(start)
        log.append_envelope(failed)
        log.append_envelope(suspended)
        assert log.healthy
    finally:
        log.close()


# --------------------------------------------------------------------------
# A cancellation, an applied close, evidence of a completed probe
# --------------------------------------------------------------------------

CANCELLED_FINISH = {
    "outcome": "stopped",
    "reason": "cancelled",
    "continuation": {"kind": "linked_run", "reason": "cancelled"},
    "evidence": None,
}


def _cancelled(life: Family14Life) -> None:
    """Stand in for a durable `editor_failed(cancelled)` (family 10's rule):
    the lifecycle state that record leaves, without building a whole Attempt."""

    life.state._cancelled_role_seq = len(life.envelopes)
    life.state._snapshot = None


def test_no_preflight_starts_after_a_cancelled_role_terminal():
    life = _life()
    _cancelled(life)
    with pytest.raises(
        SchemaError, match="^a cancelled role terminal admits no connection preflight$"
    ):
        life.accept(STARTED, op=OP, **_started(life))


def test_an_open_preflight_closes_interrupted_under_a_cancellation():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    _cancelled(life)
    with pytest.raises(
        SchemaError, match="^a cancelled role terminal admits no connection preflight$"
    ):
        life.accept(FAILED, op=OP, **_failed())
    with pytest.raises(
        SchemaError, match="^a cancelled role terminal admits no connection preflight$"
    ):
        life.accept(FAILED, op=OP, **_unavailable(cause_code="cleanup_failed", cleanup="failed"))
    life.accept(INTERRUPTED, op=OP, **_interrupted(reason="cancelled", cleanup="failed"))
    life.accept("campaign_finished", **CANCELLED_FINISH)
    assert life.snapshot.finished is not None
    assert life.snapshot.connection.latest[BINDING]["outcome"] == "interrupted"


def _closed(life: Family14Life) -> None:
    applied, _ = _change(life.envelopes[0].payload, visibility=True, effect="close")
    life.accept("authorization_applied", **applied)
    assert life.snapshot.owed_terminal is not None


def test_an_applied_close_admits_the_open_preflights_interrupted_terminal():
    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    _closed(life)
    owed = "^only the owed terminal follows an applied close$"
    with pytest.raises(SchemaError, match=owed):
        life.accept(FAILED, op=OP, **_failed())
    with pytest.raises(SchemaError, match=owed):
        life.accept(FINISHED, op=OP, **_finished())
    with pytest.raises(
        SchemaError, match="^campaign_finished waits for the open connection preflight$"
    ):
        life.accept("campaign_finished", **_close_terminal(life))
    life.accept(INTERRUPTED, op=OP, **_interrupted(reason="interrupted", cleanup="released"))
    with pytest.raises(SchemaError, match=owed):
        life.accept(STARTED, op="cp-0002", **_started(life, op="cp-0002"))
    life.accept("campaign_finished", **_close_terminal(life))
    assert life.snapshot.finished is not None


def test_an_applied_close_without_an_open_preflight_admits_no_connection_record():
    life = _life()
    _closed(life)
    owed = "^only the owed terminal follows an applied close$"
    with pytest.raises(SchemaError, match=owed):
        life.accept(STARTED, op=OP, **_started(life))
    with pytest.raises(SchemaError):
        life.accept(INTERRUPTED, op=OP, **_interrupted())


def _close_terminal(life: Family14Life) -> dict[str, Any]:
    outcome, reason = life.snapshot.owed_terminal
    return {
        "outcome": outcome,
        "reason": reason,
        "continuation": {"kind": "linked_run", "reason": reason},
        "evidence": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        _failed(evidence=[]),
        _unavailable(
            status="connection_preflight_inconclusive", cause_code="turn_budget", evidence=[]
        ),
    ],
)
def test_a_completed_probe_cites_its_journal(payload):
    with pytest.raises(SchemaError, match="^connection evidence cites the probe journal once$"):
        validate_payload(FAILED, payload, final=True)
    validate_payload(FAILED, {**payload, "evidence": [_reference()]}, final=True)


def test_only_an_unavailable_probe_may_cite_no_journal():
    validate_payload(FAILED, _unavailable(evidence=[]), final=True)


def test_a_terminal_whose_operation_is_not_the_open_preflight_is_refused_under_o():
    """The open-index check is a refusal, not an `assert` `-O` would strip."""

    life = _life()
    life.accept(STARTED, op=OP, **_started(life))
    life.state._connection_open = None
    with pytest.raises(SchemaError, match="^the operation is not the open connection preflight$"):
        life.accept(FINISHED, op=OP, **_finished())
