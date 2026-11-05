"""Strict wire parsing prevents ambiguous or unsafe durable evidence."""

import importlib
import importlib.util
import json

import pytest

from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.storage.envelope import Envelope, SchemaError


def schema():
    assert importlib.util.find_spec("evolution.tree.execution.journal_schema") is not None, (
        "native journal schema missing"
    )
    return importlib.import_module("evolution.tree.execution.journal_schema")


def record(event="rollout_started", **payload):
    return dict(
        schema_version=1,
        run_id="run-1",
        sequence=2,
        timestamp="2026-09-06T12:00:00Z",
        event=event,
        operation_id="op-1",
        payload=dict(
            case_id="case-1", sample_id="sample-1", candidate_digest="candidate", **payload
        ),
    )


# `_SCORING_STARTED_EXTRA` carries `scoring_started`'s other required keys
# (`score_request_id`, `scorer`, `input_digest`; `record()` already supplies
# the sample fields), copied from `SCORING_START` below rather than guessed.
_SCORING_STARTED_EXTRA = dict(
    score_request_id="score-1",
    input_digest="a" * 64,
    scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
)


@pytest.mark.parametrize("version", [1, 2])
def test_scoring_started_accepts_both_input_schema_versions(version):
    # Adapted from the brief: there is no `schema.validate(...)` free function
    # and no `schema` fixture — `schema()` is a plain helper returning the
    # module, and the equivalent single-record check is
    # `Envelope.from_payload`, which runs `_validate_payload` and raises the
    # (ValueError-subclassing) `SchemaError` on a bad payload.
    Envelope.from_payload(
        record("scoring_started", input_schema_version=version, **_SCORING_STARTED_EXTRA),
        family=S1_FAMILY,
    )


@pytest.mark.parametrize("version", [0, 3, "2"])
def test_scoring_started_refuses_any_other_input_schema_version(version):
    with pytest.raises(SchemaError):
        Envelope.from_payload(
            record("scoring_started", input_schema_version=version, **_SCORING_STARTED_EXTRA),
            family=S1_FAMILY,
        )


@pytest.mark.parametrize(
    "event,outcome",
    [
        ("rollout_finished", "scorer_failed"),
        ("rollout_failed", "completed"),
        ("rollout_interrupted", "cancelled"),
    ],
)
def test_wrong_execution_event_cannot_fabricate_outcome(event, outcome):
    with pytest.raises(SchemaError):
        Envelope.from_payload(record(event, outcome=outcome), family=S1_FAMILY)


@pytest.mark.parametrize(
    "event,outcome",
    [
        ("rollout_finished", "completed"),
        ("rollout_finished", "turn_budget"),
        ("rollout_finished", "candidate_failed"),
        ("rollout_failed", "environment_failed"),
        ("rollout_failed", "infrastructure_failed"),
        ("rollout_failed", "target_failed"),
        ("rollout_failed", "time_budget"),
        ("rollout_interrupted", "interrupted"),
    ],
)
def test_supported_execution_outcomes_preserved(event, outcome):
    assert (
        Envelope.from_payload(record(event, outcome=outcome), family=S1_FAMILY).to_payload()[
            "payload"
        ]["outcome"]
        == outcome
    )


def test_rollout_finished_refuses_an_unknown_terminal():
    with pytest.raises(SchemaError):
        Envelope.from_payload(record("rollout_finished", outcome="nope"), family=S1_FAMILY)


def test_unknown_nested_payload_and_secret_are_not_echoed():
    raw = record()
    raw["payload"]["oracle"] = "SECRET-CANARY"
    with pytest.raises(SchemaError) as exc:
        Envelope.from_payload(raw, family=S1_FAMILY)
    assert "SECRET-CANARY" not in str(exc.value)
    with pytest.raises(SchemaError):
        Envelope.from_payload(record(), secrets=("sample",), family=S1_FAMILY)


def test_scoring_summary_rejects_raw_details_and_nonfinite_metrics():
    raw = record(
        "scoring_finished",
        score_request_id="score-1",
        metrics={"accuracy": 1.0},
        critical_pass=None,
    )
    assert Envelope.from_payload(raw, family=S1_FAMILY).event == "scoring_finished"
    for key, value in [
        ("details", {"private": "SECRET"}),
        ("metrics", {"accuracy": float("inf")}),
        ("critical_pass", 1),
    ]:
        bad = json.loads(json.dumps(raw))
        bad["payload"][key] = value
        with pytest.raises(SchemaError):
            Envelope.from_payload(bad, family=S1_FAMILY)


def test_external_terminal_requires_safe_category_and_duration():
    raw = record(
        "target_send_failed",
        instance_id="instance-1",
        request_id="request-1",
        turn=1,
        retry_ordinal=0,
        latency_ms=0,
        failure="target_error",
    )
    assert Envelope.from_payload(raw, family=S1_FAMILY).event == "target_send_failed"
    raw["payload"]["failure"] = "SECRET exception body"
    with pytest.raises(SchemaError):
        Envelope.from_payload(raw, family=S1_FAMILY)


def ledger(identity_extra=None):
    m = schema()
    assert hasattr(m, "Lifecycle"), "cross-record lifecycle missing"
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from tests.tree.execution.test_journal_identity import identity_payload as payload

    ident = RunIdentityV1.from_payload({**payload(), **(identity_extra or {})})
    raw = dict(
        schema_version=1,
        run_id="run-1",
        sequence=1,
        timestamp="2026-09-06T12:00:00Z",
        event="run_created",
        operation_id=None,
        payload=dict(
            identity=ident.to_payload(), identity_digest=ident.digest, producing_version="0.2.0rc2"
        ),
    )
    state = m.Lifecycle()
    state.accept(Envelope.from_payload(raw, family=S1_FAMILY))
    return state


def _identity_target():
    from tests.tree.execution.test_journal_identity import identity_payload

    return identity_payload()["target"]


def event(state, kind, op="op-1", **extra):
    raw = record(kind, **extra)
    raw["payload"].update(case_id="c", sample_id="s1")
    raw["sequence"] = state.sequence + 1
    raw["operation_id"] = op
    return Envelope.from_payload(raw, family=S1_FAMILY)


def test_lifecycle_rejects_duplicate_and_mismatched_terminal():
    state = ledger()
    state.accept(event(state, "rollout_started"))
    for bad in [
        event(state, "rollout_started", op="op-2"),
        event(state, "rollout_finished", op="other", outcome="completed"),
    ]:
        with pytest.raises(SchemaError):
            state.accept(bad)
    state.accept(event(state, "rollout_finished", outcome="completed"))
    with pytest.raises(SchemaError):
        state.accept(event(state, "rollout_finished", outcome="completed"))


def test_scoring_requires_completed_scoreable_execution_and_matching_request():
    state = ledger()

    def start():
        return event(
            state,
            "scoring_started",
            op="score-op",
            score_request_id="score-1",
            input_schema_version=1,
            input_digest="a" * 64,
            scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
        )

    with pytest.raises(SchemaError):
        state.accept(start())
    state.accept(event(state, "rollout_started"))
    state.accept(event(state, "rollout_finished", outcome="completed"))
    state.accept(start())
    with pytest.raises(SchemaError):
        state.accept(
            event(
                state,
                "scoring_finished",
                op="score-op",
                score_request_id="different",
                metrics={"accuracy": 1},
                critical_pass=None,
            )
        )
    state.accept(
        event(
            state,
            "scoring_finished",
            op="score-op",
            score_request_id="score-1",
            metrics={"accuracy": 1},
            critical_pass=None,
        )
    )
    assert not state.pending


@pytest.mark.parametrize("outcome", ["time_budget", "target_failed", "cancelled", "interrupted"])
def test_nonscoreable_execution_never_admits_scoring(outcome):
    state = ledger()
    state.accept(event(state, "rollout_started"))
    state.accept(event(state, "rollout_finished", outcome=outcome))
    with pytest.raises(SchemaError):
        state.accept(
            event(
                state,
                "scoring_started",
                op="score-op",
                score_request_id="score-1",
                input_schema_version=1,
                input_digest="a" * 64,
                scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
            )
        )


def test_gap_or_cross_run_record_leaves_sequence_unchanged():
    state = ledger()
    for key, value in [("sequence", 3), ("run_id", "another")]:
        raw = event(state, "rollout_started").to_payload()
        raw[key] = value
        with pytest.raises(SchemaError):
            state.accept(Envelope.from_payload(raw, family=S1_FAMILY))
    assert state.sequence == 1


def external(state, kind, op, **extra):
    raw = event(state, kind, op=op, instance_id="i", retry_ordinal=0, **extra)
    return raw


def test_external_closure_must_precede_rollout_terminal_and_match_instance():
    state = ledger()
    state.accept(event(state, "rollout_started"))
    state.accept(external(state, "target_identity_started", "external-1"))
    with pytest.raises(SchemaError):
        state.accept(event(state, "rollout_finished", outcome="completed"))
    raw = external(state, "target_identity_finished", "external-1", latency_ms=1).to_payload()
    raw["payload"]["instance_id"] = "other"
    with pytest.raises(SchemaError):
        state.accept(Envelope.from_payload(raw, family=S1_FAMILY))
    state.accept(external(state, "target_identity_finished", "external-1", latency_ms=1))
    raw = external(state, "target_identity_started", "external-2").to_payload()
    raw["payload"]["instance_id"] = "other"
    with pytest.raises(SchemaError):
        state.accept(Envelope.from_payload(raw, family=S1_FAMILY))


def test_materialization_reserves_one_release_and_recovery_consumes_terminal():
    state = ledger()

    def owned(kind, op, **extra):
        raw = record()
        raw.update(
            event=kind,
            operation_id=op,
            sequence=state.sequence + 1,
            payload=dict(
                instance_id="i",
                candidate_digest="candidate",
                strategy="filesystem_overlay",
                retry_ordinal=0,
                **extra,
            ),
        )
        return Envelope.from_payload(raw, family=S1_FAMILY)

    state.accept(owned("target_materialize_started", "mat"))
    assert state.reserved_records == 4
    state.accept(owned("target_materialize_finished", "mat", latency_ms=0))
    assert state.reserved_records == 3
    state.accept(owned("target_release_started", "rel"))
    assert state.reserved_records == 2
    raw = record()
    raw.update(
        event="operation_interrupted",
        operation_id="rel",
        sequence=state.sequence + 1,
        payload={"operation": "target_release"},
    )
    state.accept(Envelope.from_payload(raw, family=S1_FAMILY))
    assert state.reserved_records == 1 and "i" in state.instances
    with pytest.raises(SchemaError):
        state.accept(owned("target_materialize_started", "mat2"))


@pytest.mark.parametrize("value", [None, True, -1])
def test_external_required_latency_is_integer(value):
    with pytest.raises(SchemaError):
        Envelope.from_payload(
            record("target_identity_finished", instance_id="i", retry_ordinal=0, latency_ms=value),
            family=S1_FAMILY,
        )


def test_prepared_transition_does_not_publish_until_committed():
    state = ledger()
    before = dict(state.__dict__)
    transition = state.prepare(event(state, "rollout_started"))
    assert state.__dict__ == before and state.sequence == 1
    assert transition.reserved_records == state.reserved_records + 1
    transition.commit()
    assert state.sequence == 2 and len(state.pending) == 1
    with pytest.raises(SchemaError):
        transition.commit()


def test_validation_never_decodes_old_creation_or_walks_old_indexes(monkeypatch):
    state = ledger()
    created = state.created
    original = Envelope.to_payload

    def read(record):
        assert record is not created, "redecoded frozen run schedule"
        return original(record)

    class NoScan(dict):
        def values(self):
            pytest.fail("scanned accumulated history")

        def __deepcopy__(self, memo):
            pytest.fail("copied accumulated history")

    monkeypatch.setattr(Envelope, "to_payload", read)
    state.scoring = NoScan(state.scoring)
    state.pending = NoScan(state.pending)
    state.accept(event(state, "rollout_started"))
    state.accept(event(state, "rollout_finished", outcome="completed"))
    state.accept(
        event(
            state,
            "scoring_started",
            op="score-op",
            score_request_id="score-1",
            input_schema_version=1,
            input_digest="a" * 64,
            scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
        )
    )


def test_late_instance_validation_rejection_preserves_all_indexes():
    from copy import deepcopy

    state = ledger()
    state.accept(event(state, "rollout_started"))
    state.accept(external(state, "target_identity_started", "first"))
    state.accept(external(state, "target_identity_finished", "first", latency_ms=0))
    before = deepcopy(state.__dict__)
    bad = external(state, "target_identity_started", "second").to_payload()
    bad["payload"]["instance_id"] = "wrong-instance"
    with pytest.raises(SchemaError):
        state.accept(Envelope.from_payload(bad, family=S1_FAMILY))
    assert state.__dict__ == before
    state.accept(external(state, "target_identity_started", "second"))
    state.accept(external(state, "target_identity_finished", "second", latency_ms=0))
    state.accept(event(state, "rollout_finished", outcome="completed"))
    assert not state.pending


def retried_ledger(limit=1):
    """A ledger whose identity admits `limit` further tries of each sample."""
    return ledger({"sample_retries": limit})


def accept(state, kind, op="op-1", **extra):
    record = event(state, kind, op=op, **extra)
    state.accept(record)
    return record


SCORING_START = dict(
    score_request_id="score-1",
    input_schema_version=1,
    input_digest="a" * 64,
    scorer=dict(kind="judge", digest="scorer", recovery="non_replayable"),
)


def test_a_second_try_starts_after_an_indeterminate_terminal():
    state = retried_ledger()
    first = accept(state, "rollout_started", "r0", retry_ordinal=0)
    closed = accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    assert state.ordinal[("c", "s1")] == 1
    assert state.tries[("c", "s1")] == ((closed, None),)
    assert state.executions[("c", "s1")].event == "rollout_started"
    assert first is not state.executions[("c", "s1")]


def test_no_try_after_a_score_or_beyond_the_limit_or_out_of_order():
    state = retried_ledger()
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r2", retry_ordinal=2)
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r0b", retry_ordinal=0)
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    with pytest.raises(SchemaError):  # the try is still open
        accept(state, "rollout_started", "r2", retry_ordinal=2)
    accept(state, "rollout_finished", "r1", retry_ordinal=1, outcome="time_budget")
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r2", retry_ordinal=2)

    scored = retried_ledger(3)
    accept(scored, "rollout_started", "r0", retry_ordinal=0)
    accept(scored, "rollout_finished", "r0", retry_ordinal=0, outcome="completed")
    accept(scored, "scoring_started", "s0", retry_ordinal=0, **SCORING_START)
    with pytest.raises(SchemaError):  # scoring in flight
        accept(scored, "rollout_started", "r1", retry_ordinal=1)
    accept(
        scored,
        "scoring_finished",
        "s0",
        retry_ordinal=0,
        score_request_id="score-1",
        metrics={"accuracy": 1},
        critical_pass=None,
    )
    with pytest.raises(SchemaError):
        accept(scored, "rollout_started", "r1", retry_ordinal=1)


def test_a_scoring_terminal_without_a_score_admits_the_next_try():
    state = retried_ledger()
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="completed")
    accept(state, "scoring_started", "s0", retry_ordinal=0, **SCORING_START)
    failed = accept(
        state,
        "scoring_failed",
        "s0",
        retry_ordinal=0,
        score_request_id="score-1",
        failure="scorer_error",
    )
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    assert state.tries[("c", "s1")][0][1] == failed
    assert ("c", "s1") not in state.scoring
    accept(state, "rollout_finished", "r1", retry_ordinal=1, outcome="completed")
    with pytest.raises(SchemaError):  # a score request id stays unique across tries
        accept(state, "scoring_started", "s1", retry_ordinal=1, **SCORING_START)
    second = {**SCORING_START, "score_request_id": "score-2"}
    accept(state, "scoring_started", "s1", retry_ordinal=1, **second)


def test_try_is_retryable_classifies_closed_tries():
    m = schema()
    state = retried_ledger()
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    completed = event(state, "rollout_finished", "r0", retry_ordinal=0, outcome="completed")
    failed = event(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    cancelled = event(state, "rollout_finished", "r0", retry_ordinal=0, outcome="cancelled")
    score = dict(retry_ordinal=0, score_request_id="score-1")
    finished = event(state, "scoring_finished", "s", metrics={"a": 1}, critical_pass=None, **score)
    interrupted = event(state, "scoring_interrupted", "s", **score)
    assert m.try_is_retryable(failed, None)
    assert m.try_is_retryable(completed, None)
    assert m.try_is_retryable(completed, interrupted)
    assert not m.try_is_retryable(completed, finished)
    assert not m.try_is_retryable(cancelled, None)


def test_sample_operations_carry_the_current_try():
    state = retried_ledger()
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    accept(state, "target_identity_started", "t0", instance_id="i0", retry_ordinal=0)
    accept(state, "target_identity_finished", "t0", instance_id="i0", retry_ordinal=0, latency_ms=0)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    with pytest.raises(SchemaError):
        accept(state, "target_identity_started", "t1", instance_id="i1", retry_ordinal=0)
    accept(state, "target_identity_started", "t1", instance_id="i1", retry_ordinal=1)
    with pytest.raises(SchemaError):  # the instance is fixed within one try
        accept(state, "target_identity_started", "t2", instance_id="i0", retry_ordinal=1)


def test_sample_records_require_the_ordinal_iff_the_identity_retries():
    with pytest.raises(SchemaError):
        accept(retried_ledger(), "rollout_started")  # missing retry_ordinal
    with pytest.raises(SchemaError):
        accept(ledger(), "rollout_started", retry_ordinal=0)  # identity without sample_retries
    with pytest.raises(SchemaError):  # operations stay at ordinal 0 without sample_retries
        state = ledger()
        accept(state, "rollout_started")
        accept(state, "target_identity_started", "t", instance_id="i", retry_ordinal=1)
    raw = record("rollout_started", retry_ordinal=True)
    with pytest.raises(SchemaError):
        Envelope.from_payload(raw, family=S1_FAMILY)


def _fault(state, op, ordinal):
    """Close try `ordinal` of `("c", "s1")` as a fault row: `rollout_failed(infrastructure_failed)`."""
    accept(state, "rollout_failed", op, retry_ordinal=ordinal, outcome="infrastructure_failed")


def test_a_repair_try_follows_a_closed_fault_past_the_retry_limit():
    """One repair try, the next ordinal, outside `sample_retries`."""
    state = retried_ledger(1)
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    _fault(state, "r1", 1)
    with pytest.raises(SchemaError):  # a fault is not retryable: no sample try follows it
        accept(state, "rollout_started", "r2", retry_ordinal=2)
    with pytest.raises(SchemaError):  # a repair is the next ordinal, never a later one
        accept(state, "rollout_started", "r3", retry_ordinal=3, repair=True)
    accept(state, "rollout_started", "r2", retry_ordinal=2, repair=True)
    assert state.repairs[("c", "s1")] == 1
    # The repair's own records carry its ordinal, past `sample_retries`.
    accept(state, "target_identity_started", "t2", instance_id="i2", retry_ordinal=2)
    accept(state, "target_identity_finished", "t2", instance_id="i2", retry_ordinal=2, latency_ms=0)
    with pytest.raises(SchemaError):  # still bounded: one ordinal per admitted repair
        accept(state, "target_identity_started", "t3", instance_id="i2", retry_ordinal=3)
    # A repair ending in a fault again admits the next repair (the campaign
    # dispatches it on the next resume; the audit checks the suspension named it).
    _fault(state, "r2", 2)
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r3", retry_ordinal=3)
    accept(state, "rollout_started", "r3", retry_ordinal=3, repair=True)
    assert state.repairs[("c", "s1")] == 2
    # A repair ending indeterminate admits no sample retry; another repair is
    # admitted (#78): a threshold suspension may grant one.
    accept(state, "rollout_finished", "r3", retry_ordinal=3, outcome="target_failed")
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r4", retry_ordinal=4)
    accept(state, "rollout_started", "r4", retry_ordinal=4, repair=True)
    assert state.repairs[("c", "s1")] == 3


def test_a_repair_may_follow_an_indeterminate_try_whose_retries_are_spent():
    """#78: a threshold suspension grants a repair to an exhausted
    pair. S1 admits it after a retryable try at the retry limit; which pairs
    were granted is the campaign's and the audit's."""
    state = retried_ledger(0)
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    with pytest.raises(SchemaError):  # no sample retry: sample_retries is 0
        accept(state, "rollout_started", "r1", retry_ordinal=1)
    with pytest.raises(SchemaError):  # a repair is the next ordinal, never a later one
        accept(state, "rollout_started", "r2", retry_ordinal=2, repair=True)
    accept(state, "rollout_started", "r1", retry_ordinal=1, repair=True)
    assert state.repairs[("c", "s1")] == 1
    with pytest.raises(SchemaError):  # one try at a time: ordinal 1 is still open
        accept(state, "rollout_started", "r2", retry_ordinal=2, repair=True)
    accept(state, "rollout_finished", "r1", retry_ordinal=1, outcome="target_failed")
    accept(state, "rollout_started", "r2", retry_ordinal=2, repair=True)
    assert state.repairs[("c", "s1")] == 2


def test_no_d1_retry_follows_a_repair_even_below_the_retry_limit():
    state = retried_ledger(3)
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    _fault(state, "r0", 0)
    accept(state, "rollout_started", "r1", retry_ordinal=1, repair=True)
    accept(state, "rollout_finished", "r1", retry_ordinal=1, outcome="target_failed")
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r2", retry_ordinal=2)


def test_a_repair_needs_a_closed_non_retryable_try_and_the_literal_true():
    fresh = retried_ledger(1)
    with pytest.raises(SchemaError):  # the first try is never a repair
        accept(fresh, "rollout_started", "r0", retry_ordinal=0, repair=True)
    state = retried_ledger(1)
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    with pytest.raises(SchemaError):  # the try is still open
        accept(state, "rollout_started", "r1", retry_ordinal=1, repair=True)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="target_failed")
    with pytest.raises(SchemaError):  # a retryable try is retried, never repaired
        accept(state, "rollout_started", "r1", retry_ordinal=1, repair=True)
    with pytest.raises(SchemaError):
        Envelope.from_payload(
            record("rollout_started", retry_ordinal=1, repair=False), family=S1_FAMILY
        )
    with pytest.raises(SchemaError):  # only the start says it
        Envelope.from_payload(
            record("rollout_finished", retry_ordinal=1, outcome="completed", repair=True),
            family=S1_FAMILY,
        )
    unnumbered = ledger()
    accept(unnumbered, "rollout_started", "r0")
    _unnumbered_fault = event(unnumbered, "rollout_failed", "r0", outcome="infrastructure_failed")
    unnumbered.accept(_unnumbered_fault)
    with pytest.raises(SchemaError):  # an identity without sample_retries numbers no try
        accept(unnumbered, "rollout_started", "r1", repair=True)


def test_ordinal_limit_is_the_retry_limit_plus_the_repairs():
    m = schema()
    assert m.ordinal_limit(None, 0) == 0
    assert m.ordinal_limit(2, 0) == 2
    assert m.ordinal_limit(2, 3) == 5


IMPORT_SOURCE = dict(
    run_id="pred-1",
    measurement_id="m-r00-champion",
    identity_digest="d" * 64,
    candidate_digest="c" * 64,
    retry_ordinal=0,
    rollout_digest="1" * 64,
    scoring_digest="2" * 64,
    condition_fields=["identity.target.runtime_ref_digest"],
)


def imported(state, case_id="c", sample_id="s1", **overrides):
    identity = state._identity
    entry = next(
        s for s in identity["schedule"] if (s["case_id"], s["sample_id"]) == (case_id, sample_id)
    )
    payload = dict(
        case_id=case_id,
        sample_id=sample_id,
        candidate_digest=identity["candidate_digest"],
        outcome="completed",
        metrics={"score": 1.0},
        critical_pass=None,
        effective_seed=entry["requested_seed"],
        dependency_digest=identity["target"]["dependency_digest"],
        source=dict(IMPORT_SOURCE),
    )
    payload.update(overrides)
    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run-1",
            sequence=state.sequence + 1,
            timestamp="2026-09-18T12:00:00Z",
            event="sample_imported",
            operation_id=None,
            payload=payload,
        ),
        family=S1_FAMILY,
    )


def test_an_imported_sample_follows_run_created_and_is_recorded():
    state = ledger()
    record = imported(state)
    state.accept(record)
    assert state.imported[("c", "s1")] is record
    assert ("c", "s1") not in state.executions


def test_an_import_after_any_other_record_is_refused():
    from tests.tree.execution.test_journal_identity import identity_payload

    schedule = [
        *identity_payload()["schedule"],
        dict(case_id="c", sample_id="s2", repeat=2, requested_seed=42),
    ]
    state = ledger({"schedule": schedule})
    accept(state, "rollout_started", "r0")
    with pytest.raises(SchemaError):
        state.accept(imported(state, sample_id="s2"))


def test_a_pair_is_imported_at_most_once_and_never_dispatched():
    state = ledger()
    state.accept(imported(state))
    with pytest.raises(SchemaError):
        state.accept(imported(state))
    with pytest.raises(SchemaError):
        state.accept(event(state, "rollout_started"))


@pytest.mark.parametrize("outcome", ["completed", "turn_budget", "candidate_failed"])
def test_every_scored_terminal_is_importable(outcome):
    # PDR-0013/ADR-0017 admit a row because it is *scored*, never by a list of
    # terminals; `candidate_failed` is a scoring terminal, so a carried
    # row may hold it.
    state = ledger()
    record = imported(state, outcome=outcome)
    state.accept(record)
    assert state.imported[("c", "s1")] is record


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": "time_budget"},
        {"metrics": {}},
        {"candidate_digest": "e" * 64},
        {"source": {**IMPORT_SOURCE, "condition_fields": ["b", "a"]}},
        {"source": {**IMPORT_SOURCE, "extra": 1}},
    ],
)
def test_an_invalid_import_is_refused(overrides):
    state = ledger()
    with pytest.raises(SchemaError):
        state.accept(imported(state, **overrides))


def test_an_exact_import_needs_the_targets_dependency_digest():
    state = ledger({"target": {**_identity_target(), "seed_mode": "exact"}})
    with pytest.raises(SchemaError):
        state.accept(imported(state, dependency_digest="f" * 64))


# --- call-attempt evidence ----------------------------------------


def _send_failed(**extra):
    return record(
        "target_send_failed",
        instance_id="instance-1",
        request_id="request-1",
        turn=1,
        retry_ordinal=0,
        latency_ms=0,
        failure="target_error",
        **extra,
    )


def test_a_failed_attempt_carries_its_number_and_code():
    m = schema()
    raw = _send_failed(call_attempt=2, error_code="internal")
    assert Envelope.from_payload(raw, family=S1_FAMILY).payload["call_attempt"] == 2
    started = record(
        "target_send_started",
        instance_id="instance-1",
        request_id="request-1",
        turn=1,
        retry_ordinal=0,
        call_attempt=1,
    )
    assert Envelope.from_payload(started, family=S1_FAMILY).payload["call_attempt"] == 1
    for operation in m.EXTERNAL_OPERATIONS - {"target_materialize", "target_release"}:
        raw = record(f"{operation}_finished", instance_id="i", retry_ordinal=0, latency_ms=0)
        if operation in ("target_open", "target_send"):
            raw["payload"]["request_id"] = "r"
        if operation in ("target_send", "environment_next"):
            raw["payload"]["turn"] = 0
        raw["payload"]["call_attempt"] = 3
        Envelope.from_payload(raw, family=S1_FAMILY)


@pytest.mark.parametrize(
    "extra",
    [
        {"call_attempt": 0},
        {"call_attempt": "2"},
        {"call_attempt": True},
        {"error_code": "has space"},
        {"error_code": ""},
        {"error_code": "x" * 65},
        {"error_code": 7},
    ],
)
def test_malformed_attempt_evidence_is_refused(extra):
    with pytest.raises(SchemaError):
        Envelope.from_payload(_send_failed(**extra), family=S1_FAMILY)


def test_error_code_is_only_admitted_on_a_failed_record():
    raw = record(
        "target_send_started",
        instance_id="instance-1",
        request_id="request-1",
        turn=1,
        retry_ordinal=0,
        error_code="internal",
    )
    with pytest.raises(SchemaError):
        Envelope.from_payload(raw, family=S1_FAMILY)


def test_a_terminal_must_repeat_its_starts_attempt_number():
    state = ledger()
    state.accept(event(state, "rollout_started"))
    state.accept(external(state, "target_identity_started", "external-1", call_attempt=1))
    with pytest.raises(SchemaError):
        state.accept(
            external(
                state,
                "target_identity_failed",
                "external-1",
                latency_ms=0,
                failure="target_error",
                call_attempt=2,
                error_code="internal",
            )
        )
    state.accept(
        external(
            state,
            "target_identity_failed",
            "external-1",
            latency_ms=0,
            failure="target_error",
            call_attempt=1,
            error_code="internal",
        )
    )


def _owned(state, kind, op, **extra):
    raw = record()
    raw.update(
        event=kind,
        operation_id=op,
        sequence=state.sequence + 1,
        payload=dict(
            instance_id="i",
            candidate_digest="candidate",
            strategy="filesystem_overlay",
            retry_ordinal=0,
            **extra,
        ),
    )
    return Envelope.from_payload(raw, family=S1_FAMILY)


def test_a_failed_materialize_may_be_retried_under_the_same_instance_id():
    state = ledger()
    state.accept(_owned(state, "target_materialize_started", "mat1", call_attempt=1))
    assert state.reserved_records == 4
    state.accept(
        _owned(
            state,
            "target_materialize_failed",
            "mat1",
            latency_ms=0,
            failure="target_error",
            call_attempt=1,
            error_code="internal",
        )
    )
    assert state.reserved_records == 3
    # The retry reserves nothing new: the one owed release is already reserved.
    state.accept(_owned(state, "target_materialize_started", "mat2", call_attempt=2))
    assert state.reserved_records == 4
    # No third start while the second is still pending.
    with pytest.raises(SchemaError):
        state.accept(_owned(state, "target_materialize_started", "mat3", call_attempt=3))
    state.accept(_owned(state, "target_materialize_finished", "mat2", latency_ms=0, call_attempt=2))
    # A finished materialization is never re-materialized.
    with pytest.raises(SchemaError):
        state.accept(_owned(state, "target_materialize_started", "mat3", call_attempt=3))
    state.accept(_owned(state, "target_release_started", "rel"))
    state.accept(_owned(state, "target_release_finished", "rel", latency_ms=0))
    assert state.reserved_records == 1 and "i" not in state.instances


# -- components_declared (family 12) ------------------------------------

MAP = {"catalog": {"sha256": "a" * 64, "sources": ["skills/a/SKILL.md"]}}


def declared(state, components=MAP, digest=None):
    m = schema()
    payload = dict(
        components_digest=m.components_digest(components) if digest is None else digest,
        components=components,
    )
    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run-1",
            sequence=state.sequence + 1,
            timestamp="2026-10-01T12:00:00Z",
            event="components_declared",
            operation_id=None,
            payload=payload,
        ),
        family=S1_FAMILY,
    )


def test_components_digest_is_the_sha256_of_the_canonical_map():
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json

    expected = hashlib.sha256(canonical_json(MAP)).hexdigest()
    assert schema().components_digest(MAP) == expected


def test_components_declared_needs_a_marked_journal_and_its_own_digest():
    m = schema()
    marked = ledger({"row_provenance": 1})
    record = declared(marked)
    marked.accept(record)
    assert marked.declared_components[m.components_digest(MAP)] is record

    unmarked = ledger()
    with pytest.raises(SchemaError):
        unmarked.accept(declared(unmarked))

    wrong = ledger({"row_provenance": 1})
    with pytest.raises(SchemaError):
        wrong.accept(declared(wrong, digest="b" * 64))


def test_a_map_is_declared_once():
    state = ledger({"row_provenance": 1})
    state.accept(declared(state))
    with pytest.raises(SchemaError):
        state.accept(declared(state))
    other = {"other": {"sha256": "c" * 64, "sources": []}}
    state.accept(declared(state, other))
    assert len(state.declared_components) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"x": {"sha256": "nothex", "sources": []}},
        {"bad id!": {"sha256": "a" * 64, "sources": []}},
        {"x": {"sha256": "a" * 64, "sources": ["/abs"]}},
        {"x": {"sha256": "a" * 64, "sources": ["a", "a"]}},
        {"x": {"sha256": "a" * 64, "sources": [], "extra": 1}},
    ],
)
def test_a_malformed_map_is_refused(bad):
    state = ledger({"row_provenance": 1})
    with pytest.raises(SchemaError):
        state.accept(declared(state, bad, digest="a" * 64))
    with pytest.raises(SchemaError):
        state.accept(declared(state, bad))


def test_a_declaration_closes_the_import_window_and_follows_no_stop():
    state = ledger({"row_provenance": 1})
    state.accept(declared(state))
    with pytest.raises(SchemaError):
        state.accept(imported(state))

    stopped = ledger({"row_provenance": 1})
    stopped.accept(
        Envelope.from_payload(
            dict(
                schema_version=1,
                run_id="run-1",
                sequence=stopped.sequence + 1,
                timestamp="2026-10-01T12:00:00Z",
                event="journal_dispatch_stopped",
                operation_id=None,
                payload=dict(code="journal_limit_exceeded"),
            ),
            family=S1_FAMILY,
        )
    )
    with pytest.raises(SchemaError):
        stopped.accept(declared(stopped))


def test_a_declaration_reserves_nothing_and_carries_no_operation():
    state = ledger({"row_provenance": 1})
    before = state.reserved_records
    state.accept(declared(state))
    assert state.reserved_records == before
    raw = declared(state).to_payload()
    raw["operation_id"] = "op-1"
    with pytest.raises(SchemaError):
        Envelope.from_payload(raw, family=S1_FAMILY)


def test_an_empty_map_is_a_declared_map():
    m = schema()
    state = ledger({"row_provenance": 1})
    state.accept(declared(state, {}))
    assert set(state.declared_components) == {m.components_digest({})}
    with pytest.raises(SchemaError):
        state.accept(declared(state, {}))


# -- provenance and components_digest on rollout_finished --------------


def marked_started(declare=True):
    state = ledger({"row_provenance": 1})
    if declare:
        state.accept(declared(state))
    state.accept(event(state, "rollout_started"))
    return state


def finish(state, **extra):
    state.accept(event(state, "rollout_finished", outcome="completed", **extra))


def test_a_marked_row_admits_provenance_and_a_declared_digest():
    m = schema()
    state = marked_started()
    finish(state, provenance=["a.md", "skills/b#c"], components_digest=m.components_digest(MAP))
    assert state.executions[("c", "s1")].payload["provenance"] == ["a.md", "skills/b#c"]

    empty = marked_started()
    finish(empty, provenance=[])
    overflow = marked_started()
    finish(overflow, provenance_overflow=True)
    exact = marked_started()
    finish(exact, provenance=[f"p/{i:04d}.md" for i in range(1024)])
    at_budget = marked_started()
    finish(at_budget, provenance=AT_BUDGET)


#: 1024 identifiers whose sorted list is exactly 256 KiB of canonical JSON,
#: and the same list one byte over (ADR-0024, 2026-10-01 amendment).
AT_BUDGET = sorted(f"p/{i:04d}" + "x" * (247 if i else 246) for i in range(1024))
OVER_BUDGET = sorted(f"p/{i:04d}" + "x" * 247 for i in range(1024))


@pytest.mark.parametrize(
    "extra",
    [
        {"provenance": ["a.md"]},
        {"provenance_overflow": True},
        {"components_digest": "a" * 64},
    ],
)
def test_an_unmarked_journal_refuses_the_row_keys(extra):
    state = ledger()
    state.accept(event(state, "rollout_started"))
    with pytest.raises(SchemaError):
        finish(state, **extra)
    finish(state)


def test_an_undeclared_components_digest_is_refused():
    m = schema()
    state = marked_started()
    with pytest.raises(SchemaError):
        finish(state, components_digest="b" * 64)
    nothing = marked_started(declare=False)
    with pytest.raises(SchemaError):
        finish(nothing, components_digest=m.components_digest(MAP))
    finish(state, components_digest=m.components_digest(MAP))


@pytest.mark.parametrize(
    "extra",
    [
        {"provenance": ["a.md"], "provenance_overflow": True},
        {"provenance_overflow": False},
        {"provenance_overflow": 1},
        {"provenance": ["b.md", "a.md"]},
        {"provenance": ["a.md", "a.md"]},
        {"provenance": [f"p/{i:04d}.md" for i in range(1025)]},
        {"provenance": OVER_BUDGET},
        {"provenance": ["a b\\c"]},
        {"provenance": [1]},
        {"provenance": "a.md"},
        {"provenance": None},
        {"components_digest": "A" * 64},
        {"components_digest": "a" * 63},
    ],
)
def test_a_malformed_row_provenance_is_refused(extra):
    state = marked_started()
    with pytest.raises(SchemaError):
        finish(state, **extra)


@pytest.mark.parametrize("key", ["provenance", "provenance_overflow", "components_digest"])
@pytest.mark.parametrize("kind", ["rollout_started", "rollout_failed", "rollout_interrupted"])
def test_only_rollout_finished_carries_the_row_keys(kind, key):
    m = schema()
    value = {"provenance": ["a.md"], "provenance_overflow": True}.get(key, m.components_digest(MAP))
    outcome = {"rollout_failed": "target_failed", "rollout_interrupted": "interrupted"}
    extra = {key: value}
    if kind in outcome:
        extra["outcome"] = outcome[kind]
    with pytest.raises(SchemaError):
        Envelope.from_payload(record(kind, **extra), family=S1_FAMILY)


# -- provenance and components_digest on sample_imported ---------------


def test_a_marked_journal_imports_a_row_with_its_provenance_and_digest():
    # The map lives in the source journal: a carried digest names no
    # declaration of the target (b2 owns `components_unavailable`).
    m = schema()
    state = ledger({"row_provenance": 1})
    record = imported(state, provenance=["a.md"], components_digest="b" * 64)
    state.accept(record)
    assert state.imported[("c", "s1")].payload["provenance"] == ["a.md"]
    assert state.imported[("c", "s1")].payload["components_digest"] == "b" * 64

    empty = ledger({"row_provenance": 1})
    empty.accept(imported(empty, provenance=[]))
    at_budget = ledger({"row_provenance": 1})
    at_budget.accept(imported(at_budget, provenance=AT_BUDGET))
    digest_only = ledger({"row_provenance": 1})
    digest_only.accept(imported(digest_only, components_digest=m.components_digest(MAP)))


@pytest.mark.parametrize("extra", [{"provenance": ["a.md"]}, {"components_digest": "a" * 64}])
def test_an_unmarked_journal_refuses_an_import_with_the_row_keys(extra):
    state = ledger()
    with pytest.raises(SchemaError):
        state.accept(imported(state, **extra))
    state.accept(imported(state))


@pytest.mark.parametrize(
    "extra",
    [
        {"provenance_overflow": True},
        {"provenance": ["b.md", "a.md"]},
        {"provenance": ["a.md", "a.md"]},
        {"provenance": [f"p/{i:04d}.md" for i in range(1025)]},
        {"provenance": OVER_BUDGET},
        {"provenance": ["a b\\c"]},
        {"provenance": [1]},
        {"provenance": "a.md"},
        {"provenance": None},
        {"components_digest": "A" * 64},
        {"components_digest": "a" * 63},
    ],
)
def test_a_malformed_imported_provenance_is_refused(extra):
    state = ledger({"row_provenance": 1})
    with pytest.raises(SchemaError):
        state.accept(imported(state, **extra))


# -- connection_binding_digest and connection_admission on rollout_started --


DIGEST = "e" * 64
ADMISSION = {"operation_id": "pre-1", "controller_invocation_id": "0123abcd"}


def connected(admission=ADMISSION, digest=DIGEST):
    return {"connection_binding_digest": digest, "connection_admission": admission}


def test_a_marked_journal_starts_carry_the_binding_and_a_nullable_admission():
    state = ledger({"connection_binding": 1})
    state.accept(event(state, "rollout_started", **connected()))
    start = state.executions[("c", "s1")].payload
    assert start["connection_binding_digest"] == DIGEST
    assert start["connection_admission"] == ADMISSION
    # The terminal echoes neither key: it is the start's alone.
    state.accept(event(state, "rollout_finished", outcome="completed"))
    later = ledger({"connection_binding": 1})
    later.accept(event(later, "rollout_started", **connected(admission=None)))
    assert later.executions[("c", "s1")].payload["connection_admission"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"connection_binding_digest": DIGEST},
        {"connection_admission": None},
        {"connection_admission": ADMISSION},
    ],
)
def test_a_marked_journal_requires_both_keys_on_every_start(extra):
    state = ledger({"connection_binding": 1})
    with pytest.raises(SchemaError):
        state.accept(event(state, "rollout_started", **extra))
    assert state.sequence == 1


@pytest.mark.parametrize("admission", [ADMISSION, None])
def test_an_unmarked_journal_refuses_both_keys(admission):
    for marker in (None, {"row_provenance": 1}):
        state = ledger(marker)
        with pytest.raises(SchemaError):
            state.accept(event(state, "rollout_started", **connected(admission=admission)))
        state.accept(event(state, "rollout_started"))


@pytest.mark.parametrize(
    "extra",
    [
        connected(digest="E" * 64),
        connected(digest="e" * 63),
        connected(digest="g" * 64),
        connected(digest=None),
        connected(digest=1),
        connected(admission={}),
        connected(admission={"operation_id": "pre-1"}),
        connected(admission={**ADMISSION, "binding_digest": DIGEST}),
        connected(admission={"operation_id": "pre-1", "invocation_id": "0123abcd"}),
        connected(admission={**ADMISSION, "operation_id": ""}),
        connected(admission={**ADMISSION, "operation_id": "a b"}),
        connected(admission={**ADMISSION, "controller_invocation_id": 7}),
        connected(admission={**ADMISSION, "controller_invocation_id": None}),
        connected(admission=["pre-1", "0123abcd"]),
        connected(admission="pre-1"),
        connected(admission=False),
    ],
)
def test_a_malformed_binding_or_admission_is_refused(extra):
    with pytest.raises(SchemaError):
        Envelope.from_payload(record("rollout_started", **extra), family=S1_FAMILY)
    state = ledger({"connection_binding": 1})
    with pytest.raises(SchemaError):
        state.accept(event(state, "rollout_started", **extra))


@pytest.mark.parametrize("key", ["connection_binding_digest", "connection_admission"])
@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("rollout_finished", "completed"),
        ("rollout_failed", "target_failed"),
        ("rollout_interrupted", "interrupted"),
        ("scoring_started", None),
    ],
)
def test_only_rollout_started_carries_the_connection_keys(kind, outcome, key):
    extra = {key: connected()[key]}
    if outcome is not None:
        extra["outcome"] = outcome
    with pytest.raises(SchemaError):
        Envelope.from_payload(record(kind, **extra), family=S1_FAMILY)


def test_a_marked_start_recovers_with_todays_interrupted_terminal():
    """Recovery (`journal_recovery.py:118-122`) closes a marked start with the
    same four keys as an unmarked one, and S1 admits that terminal."""

    from evolution.tree.execution.journal_recovery import recovery_plan

    terminals = []
    for marker, extra in (({"connection_binding": 1}, connected()), (None, {})):
        state = ledger(marker)
        start = event(state, "rollout_started", **extra)
        state.accept(start)
        (action,) = recovery_plan([state.created, start]).actions
        assert action.event == "rollout_interrupted"
        payload = action.payload
        raw = record("rollout_interrupted")
        raw.update(sequence=state.sequence + 1, operation_id=action.operation_id, payload=payload)
        state.accept(Envelope.from_payload(raw, family=S1_FAMILY))
        terminals.append(payload)
    assert terminals[0] == terminals[1]
    assert terminals[0] == {
        "case_id": "c",
        "sample_id": "s1",
        "candidate_digest": "candidate",
        "outcome": "interrupted",
    }


# -- #194: a granted sample retry after a closed `cancelled` try ----------------


def _cancelled_first_try(extra):
    state = ledger({"sample_retries": 2, **extra})
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    accept(state, "rollout_finished", "r0", retry_ordinal=0, outcome="cancelled")
    return state


def test_a_d1_retry_follows_a_cancelled_try_under_row_provenance():
    state = _cancelled_first_try({"row_provenance": 1})
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    assert ("c", "s1") not in state.repairs


def test_a_cancelled_d1_retry_stops_at_the_retry_limit():
    state = _cancelled_first_try({"row_provenance": 1})
    accept(state, "rollout_started", "r1", retry_ordinal=1)
    accept(state, "rollout_finished", "r1", retry_ordinal=1, outcome="cancelled")
    accept(state, "rollout_started", "r2", retry_ordinal=2)
    accept(state, "rollout_finished", "r2", retry_ordinal=2, outcome="cancelled")
    with pytest.raises(SchemaError):  # ordinal 3 is past sample_retries = 2
        accept(state, "rollout_started", "r3", retry_ordinal=3)


def test_no_d1_retry_follows_a_cancelled_try_after_a_repair():
    state = ledger({"sample_retries": 2, "row_provenance": 1})
    accept(state, "rollout_started", "r0", retry_ordinal=0)
    _fault(state, "r0", 0)
    accept(state, "rollout_started", "r1", retry_ordinal=1, repair=True)
    accept(state, "rollout_finished", "r1", retry_ordinal=1, outcome="cancelled")
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r2", retry_ordinal=2)


def test_a_cancelled_try_admits_no_d1_retry_without_row_provenance():
    state = _cancelled_first_try({})
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r1", retry_ordinal=1)


def test_a_cancelled_try_admits_no_d1_retry_under_the_evaluation_profile():
    state = _cancelled_first_try({"row_provenance": 1, "profile": "rollout_evaluation/1"})
    with pytest.raises(SchemaError):
        accept(state, "rollout_started", "r1", retry_ordinal=1)


def test_a_repair_after_a_cancelled_try_is_still_admitted():
    """#144 is unchanged: a repair needs a non-retryable try or spent retries."""
    state = _cancelled_first_try({"row_provenance": 1, "sample_retries": 0})
    accept(state, "rollout_started", "r1", retry_ordinal=1, repair=True)
    assert state.repairs[("c", "s1")] == 1
    plain = _cancelled_first_try({})
    with pytest.raises(SchemaError):
        accept(plain, "rollout_started", "r1", retry_ordinal=1)
