"""Durable rollout boundaries preserve ownership and never publish raw inputs."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from evolution.tree.execution.interaction import (
    CallRetries,
    EnvironmentIdentity,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutSample,
    RolloutTargetUnstable,
    RolloutTimeBudgetExceeded,
    ScorerIdentity,
    SemanticIdentity,
)
from evolution.tree.execution.journal_identity import RunIdentityV1
from evolution.tree.execution.journal_rollout import JournalRolloutAdapter, durable_rollout
from evolution.tree.execution.journal_schema import S1_FAMILY
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalFault,
    JournalRecoveryRequired,
)
from tests.tree.execution.test_journal_identity import cases, identity_payload
from tests.tree.execution.test_rollout_core import _InMemoryInstance


class Sink:
    def __init__(self, fail=None, exception=JournalFault, delay=None):
        self.records = []
        self.fail = fail
        self.exception = exception
        self.delay = delay
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True

    def append(self, event, payload, *, operation_id=None):
        if event == self.fail:
            raise self.exception()
        if event == self.delay:
            time.sleep(0.025)
        record = Envelope.from_payload(
            dict(
                schema_version=1,
                run_id="r",
                sequence=len(self.records) + 1,
                timestamp="2026-09-06T00:00:00Z",
                event=event,
                operation_id=operation_id,
                payload=payload,
            ),
            family=S1_FAMILY,
        )
        self.records.append(record.to_payload())
        return len(self.records)


def setup(sink, *, env_failure=None, timeout=1.5, identity_extra=None):
    data = {**identity_payload(), **(identity_extra or {})}
    data["budget"]["timeout_seconds"] = timeout
    identity = RunIdentityV1(data)
    sink.identity = identity
    sink.corpus = cases()
    instance = _InMemoryInstance()
    instance.target_identity = instance.target_identity.__class__(
        SemanticIdentity(**data["target"]),
        instance.target_identity.instance,
        instance.target_identity.attested,
    )

    async def next_message(case, history):
        if env_failure:
            raise env_failure
        return None

    kwargs = dict(
        journal=sink,
        identity=identity,
        corpus=cases(),
        case=cases()[0],
        sample=RolloutSample("s1", "candidate", 42),
        instance=instance,
        environment=SimpleNamespace(
            identity=EnvironmentIdentity(**data["environment"]), next=next_message
        ),
        scorer=SimpleNamespace(identity=ScorerIdentity("judge", "scorer")),
        budget=RolloutBudget(2, timeout),
        expected_target=instance.target_identity,
    )
    return instance, kwargs


def test_native_rollout_envelopes_correlate_and_redact():
    sink = Sink()
    instance, kwargs = setup(sink)
    result = asyncio.run(durable_rollout(**kwargs))
    assert result.terminal == "completed"
    starts = {
        r["operation_id"]: r["event"] for r in sink.records if r["event"].endswith("_started")
    }
    assert len(starts) == len([r for r in sink.records if r["event"].endswith("_started")])
    for record in sink.records:
        assert record["operation_id"] in starts
        assert "session_id" not in record["payload"]
        assert "error_class" not in record["payload"]
    assert sink.records[0]["operation_id"] == sink.records[-1]["operation_id"]
    assert "secret" not in str(sink.records)


@pytest.mark.parametrize(
    "event",
    [
        "rollout_started",
        "target_identity_started",
        "target_identity_finished",
        "target_open_started",
        "target_open_finished",
        "environment_next_started",
        "environment_next_finished",
        "target_close_started",
        "target_close_finished",
        "rollout_finished",
    ],
)
def test_journal_fault_is_never_component_failure_or_fictional_terminal(event):
    sink = Sink(fail=event)
    instance, kwargs = setup(sink)
    with pytest.raises(JournalFault):
        asyncio.run(durable_rollout(**kwargs))
    assert not any(
        r["event"] in ("rollout_finished", "rollout_failed", "rollout_interrupted")
        for r in sink.records
    )
    if event in ("target_open_finished", "environment_next_started", "environment_next_finished"):
        assert not instance.closed


@pytest.mark.parametrize(
    "event", ["rollout_started", "target_identity_started", "environment_next_started"]
)
def test_admission_refusal_closes_only_started_rollout(event):
    sink = Sink(fail=event, exception=JournalAdmissionRefused)
    _, kwargs = setup(sink)
    with pytest.raises(JournalAdmissionRefused):
        asyncio.run(durable_rollout(**kwargs))
    terminals = [r for r in sink.records if r["event"] == "rollout_interrupted"]
    assert len(terminals) == (event != "rollout_started")


def test_known_environment_failure_keeps_owner_and_safe_category():
    sink = Sink()
    _, kwargs = setup(sink, env_failure=RuntimeError("SECRET INPUT"))
    with pytest.raises(RolloutEnvironmentError):
        asyncio.run(durable_rollout(**kwargs))
    assert sink.records[-1]["payload"]["outcome"] == "environment_failed"
    assert "SECRET INPUT" not in str(sink.records)
    assert (
        next(r for r in sink.records if r["event"] == "environment_next_failed")["payload"][
            "failure"
        ]
        == "environment_error"
    )


@pytest.mark.parametrize("event", ["rollout_started", "target_identity_started"])
def test_start_durability_time_is_in_operational_deadline(event):
    sink = Sink(delay=event)
    instance, kwargs = setup(sink, timeout=0.01)
    with pytest.raises(RolloutTimeBudgetExceeded):
        asyncio.run(durable_rollout(**kwargs))
    assert instance.opened_public is None
    assert sink.records[-1]["payload"]["outcome"] == "time_budget"


def test_live_target_mismatch_is_not_allowed_to_open():
    sink = Sink()
    instance, kwargs = setup(sink)
    instance.target_identity = instance.target_identity.__class__(
        SemanticIdentity("changed", "dep", "settings", "exact", "1", "runtime"),
        instance.target_identity.instance,
        frozenset(),
    )
    with pytest.raises(RolloutInfrastructureError):
        asyncio.run(durable_rollout(**kwargs))
    assert instance.opened_public is None
    assert sink.records[-1]["payload"]["outcome"] == "infrastructure_failed"


def test_an_exhausted_transient_call_closes_target_failed():
    """Under `/8`'s `call_retries`, a transient target-call failure that exhausts
    its attempts is `RolloutTargetUnstable`, and `durable_rollout` closes the
    sample `rollout_failed(target_failed)` for it
    -- not `infrastructure_failed`."""

    from evolution.tree.execution.interaction import Message
    from tests.tree.execution.test_rollout_core import _FlakyInstance

    sink = Sink()
    _, base_kwargs = setup(sink)
    instance = _FlakyInstance(1)
    instance.target_identity = base_kwargs["instance"].target_identity

    async def next_message(case, history):
        return Message("PROMPT") if not history.turns else None

    base_kwargs["environment"].next = next_message
    kwargs = {
        **base_kwargs,
        "instance": instance,
        "budget": RolloutBudget(
            base_kwargs["budget"].max_turns,
            base_kwargs["budget"].timeout_seconds,
            call_retries=CallRetries(2, 1),
        ),
    }
    with pytest.raises(RolloutTargetUnstable):
        asyncio.run(durable_rollout(**kwargs))
    assert sink.records[-1]["payload"]["outcome"] == "target_failed"


@pytest.mark.parametrize("event", ["target_send_started", "target_send_finished"])
def test_send_journal_fault_skips_close(event):
    from evolution.tree.execution.interaction import Message

    sink = Sink(fail=event)
    instance, kwargs = setup(sink)

    async def next_message(case, history):
        return Message("PRIVATE PROMPT") if not history.turns else None

    kwargs["environment"].next = next_message
    with pytest.raises(JournalFault):
        asyncio.run(durable_rollout(**kwargs))
    assert not instance.closed
    assert not any(r["event"] == "rollout_failed" for r in sink.records)
    assert "PRIVATE PROMPT" not in str(sink.records)


def test_failure_terminal_write_fault_takes_precedence_over_component_error():
    sink = Sink(fail="environment_next_failed")
    instance, kwargs = setup(sink, env_failure=ValueError("private"))
    with pytest.raises(JournalFault):
        asyncio.run(durable_rollout(**kwargs))
    assert not instance.closed
    assert not any(r["event"] == "rollout_failed" for r in sink.records)


def test_cleanup_write_fault_takes_precedence_over_primary_error():
    sink = Sink(fail="target_close_started")
    instance, kwargs = setup(sink, env_failure=ValueError("private"))
    with pytest.raises(JournalFault):
        asyncio.run(durable_rollout(**kwargs))
    assert not instance.closed
    assert not any(r["event"] == "rollout_failed" for r in sink.records)


@pytest.mark.parametrize(
    "stage",
    [
        "target_materialize_started",
        "target_materialize_finished",
        "target_release_started",
        "target_release_finished",
    ],
)
def test_owned_instance_boundary_fault_passthrough(stage):
    from evolution.tree.execution.rollout import materialize_target, release_target
    from tests.tree.execution.test_rollout_core import _InMemoryAdapter

    sink = Sink(fail=stage)
    adapter = _InMemoryAdapter()
    journal = JournalRolloutAdapter(sink)

    async def run():
        if stage.startswith("target_materialize"):
            await materialize_target(
                adapter,
                SimpleNamespace(digest="candidate", verify=lambda: None),
                "artifact_root_injection",
                "instance-1",
                journal,
            )
        else:
            await release_target(
                adapter, "instance-1", "candidate", "artifact_root_injection", journal
            )

    with pytest.raises(JournalFault):
        asyncio.run(run())
    assert not any(r["event"].endswith("_failed") for r in sink.records)
    if stage == "target_materialize_started":
        assert not adapter.instances
    if stage == "target_release_started":
        assert not adapter.released


def test_task_cancellation_closes_execution_as_interrupted():
    sink = Sink()
    _, kwargs = setup(sink, env_failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(durable_rollout(**kwargs))
    assert sink.records[-1]["event"] == "rollout_interrupted"
    assert sink.records[-1]["payload"]["outcome"] == "interrupted"


def test_requested_identity_cannot_override_persisted_budget():
    from evolution.tree.execution.journal_identity import IdentityError

    sink = Sink()
    _, kwargs = setup(sink)
    data = kwargs["identity"].to_payload()
    data["budget"]["timeout_seconds"] = 99
    kwargs["identity"] = RunIdentityV1(data)
    kwargs["budget"] = RolloutBudget(2, 99)
    with pytest.raises(IdentityError):
        asyncio.run(durable_rollout(**kwargs))
    assert not sink.records


def test_pending_recovery_prevents_new_execution_start():
    sink = Sink()
    _, kwargs = setup(sink)
    sink.recovery_required = True
    with pytest.raises(JournalRecoveryRequired, match="journal_busy"):
        asyncio.run(durable_rollout(**kwargs))
    assert not sink.records


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"event": None, "operation_id": "op"},
        {"event": "unknown_started", "operation_id": "op"},
        {"event": "target_identity_started", "operation_id": "op"},
    ],
)
def test_adapter_translation_failure_invalidates_native_owner(tmp_path, bad):
    from tests.tree.execution.test_journal_durability import create

    with create(tmp_path / "run") as journal:
        adapter = JournalRolloutAdapter(journal)
        with pytest.raises(JournalFault, match="journal_unavailable"):
            adapter.append(bad)
        with pytest.raises(JournalFault):
            journal.snapshot()


def test_translation_fault_inside_environment_boundary_keeps_journal_ownership(monkeypatch):
    sink = Sink()
    instance, kwargs = setup(sink)
    original = JournalRolloutAdapter._append

    def broken_translation(adapter, event):
        if event["event"] == "environment_next_finished":
            event = dict(event)
            del event["operation_id"]
        return original(adapter, event)

    monkeypatch.setattr(JournalRolloutAdapter, "_append", broken_translation)
    with pytest.raises(JournalFault, match="journal_unavailable"):
        asyncio.run(durable_rollout(**kwargs))
    assert sink.invalidated and not instance.closed
    assert not any(
        r["event"] in ("environment_next_failed", "rollout_failed", "rollout_finished")
        for r in sink.records
    )


def test_durable_rollout_writes_the_ordinal_on_every_sample_record():
    sink = Sink()
    _, kwargs = setup(sink, identity_extra={"sample_retries": 1})
    asyncio.run(durable_rollout(**kwargs, retry_ordinal=1))
    sample_events = [r for r in sink.records if "case_id" in r["payload"]]
    assert sample_events and all(r["payload"]["retry_ordinal"] == 1 for r in sample_events)
    owned = [r for r in sink.records if "case_id" not in r["payload"]]
    assert all(r["payload"]["retry_ordinal"] == 0 for r in owned)


def test_durable_rollout_without_an_ordinal_keeps_rollout_records_unnumbered():
    sink = Sink()
    _, kwargs = setup(sink)
    asyncio.run(durable_rollout(**kwargs))
    rollouts = [r for r in sink.records if r["event"].startswith("rollout_")]
    assert rollouts and all("retry_ordinal" not in r["payload"] for r in rollouts)
    operations = [r for r in sink.records if not r["event"].startswith("rollout_")]
    assert operations and all(r["payload"]["retry_ordinal"] == 0 for r in operations)


def test_an_interrupted_try_closes_with_its_ordinal():
    sink = Sink()
    _, kwargs = setup(
        sink, env_failure=RolloutEnvironmentError("boom"), identity_extra={"sample_retries": 2}
    )
    with pytest.raises(RolloutEnvironmentError):
        asyncio.run(durable_rollout(**kwargs, retry_ordinal=2))
    assert sink.records[-1]["event"] == "rollout_failed"
    assert sink.records[-1]["payload"]["retry_ordinal"] == 2


@pytest.mark.parametrize(
    ("identity_extra", "retry_ordinal"),
    [
        (None, 0),
        ({"sample_retries": 1}, None),
        ({"sample_retries": 1}, 2),
        ({"sample_retries": 1}, -1),
    ],
)
def test_durable_rollout_refuses_an_ordinal_the_identity_does_not_admit(
    identity_extra, retry_ordinal
):
    sink = Sink()
    _, kwargs = setup(sink, identity_extra=identity_extra)
    with pytest.raises(ValueError, match="^retry_ordinal_inconsistent$"):
        asyncio.run(durable_rollout(**kwargs, retry_ordinal=retry_ordinal))
    assert sink.records == []


def test_the_adapter_forwards_call_attempt_evidence():
    sink = Sink()
    adapter = JournalRolloutAdapter(sink)
    base = {
        "operation_id": "op-send",
        "case_id": "c",
        "sample_id": "s1",
        "candidate_digest": "candidate",
        "instance_id": "i",
        "request_id": "req",
        "turn": 0,
        "call_attempt": 2,
    }
    adapter.append({"event": "target_send_started", **base})
    adapter.append(
        {
            "event": "target_send_failed",
            **base,
            "latency_ms": 1,
            "error_class": "RolloutInfrastructureError",
            "error_code": "internal",
        }
    )
    started, failed = (r["payload"] for r in sink.records)
    assert started["call_attempt"] == 2 and "error_code" not in started
    assert failed["call_attempt"] == 2 and failed["error_code"] == "internal"
    assert failed["failure"] == "target_error"


def test_a_durable_rollout_journals_every_attempt(monkeypatch):
    import evolution.tree.execution.rollout as rollout_core
    from evolution.tree.execution.interaction import CallRetries

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(rollout_core, "_SLEEP", no_sleep)
    sink = Sink()
    instance, kwargs = setup(sink)
    opens = {"calls": 0}
    real_open = instance.open

    async def flaky_open(case_public, seed, request_id):
        opens["calls"] += 1
        if opens["calls"] == 1:
            raise RolloutInfrastructureError("x", "transport_error", retryable=True)
        return await real_open(case_public, seed, request_id)

    instance.open = flaky_open
    kwargs["budget"] = RolloutBudget(2, 1.5, call_retries=CallRetries(2, 1))
    result = asyncio.run(durable_rollout(**kwargs))
    assert result.terminal == "completed"
    opened = [
        (r["event"], r["payload"].get("call_attempt"), r["payload"].get("error_code"))
        for r in sink.records
        if r["event"].startswith("target_open_")
    ]
    assert opened == [
        ("target_open_started", 1, None),
        ("target_open_failed", 1, "transport_error"),
        ("target_open_started", 2, None),
        ("target_open_finished", 2, None),
    ]


# -- the connection admission token --------------------

BINDING = "b" * 64


def token(**overrides):
    from evolution.tree.execution.journal_rollout import ConnectionAdmission

    fields = dict(binding_digest=BINDING, operation_id="pre-1", invocation_id="inv-1")
    return ConnectionAdmission(**{**fields, **overrides})


def test_a_token_admits_exactly_once():
    admission = token()
    assert (admission.binding_digest, admission.operation_id, admission.invocation_id) == (
        BINDING,
        "pre-1",
        "inv-1",
    )
    assert admission.claim() == {"operation_id": "pre-1", "controller_invocation_id": "inv-1"}
    assert admission.claim() is None
    assert admission.claim() is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"binding_digest": "B" * 64},
        {"binding_digest": "b" * 63},
        {"binding_digest": None},
        {"operation_id": ""},
        {"operation_id": "a b"},
        {"operation_id": 1},
        {"invocation_id": None},
        {"invocation_id": "x" * 300},
    ],
)
def test_a_malformed_token_is_refused_before_any_write(overrides):
    with pytest.raises(ValueError, match="^invalid_connection_admission$"):
        token(**overrides)


def _connected_kwargs(sink, connection, *, marked=True):
    extra = {"connection_binding": 1} if marked else None
    _, kwargs = setup(sink, identity_extra=extra)
    return {**kwargs, "connection": connection}


def test_the_first_start_carries_the_admission_and_later_starts_carry_null():
    admission = token()
    first, second = Sink(), Sink()
    asyncio.run(durable_rollout(**_connected_kwargs(first, admission)))
    asyncio.run(durable_rollout(**_connected_kwargs(second, admission)))
    starts = [
        next(r for r in sink.records if r["event"] == "rollout_started") for sink in (first, second)
    ]
    assert [s["payload"]["connection_binding_digest"] for s in starts] == [BINDING, BINDING]
    assert starts[0]["payload"]["connection_admission"] == {
        "operation_id": "pre-1",
        "controller_invocation_id": "inv-1",
    }
    assert starts[1]["payload"]["connection_admission"] is None


def test_only_the_written_start_holds_the_connection_keys():
    sink = Sink()
    adapter = JournalRolloutAdapter(sink, connection=token())
    sample = {"case_id": "c", "sample_id": "s1", "candidate_digest": "candidate"}
    adapter.append({"event": "rollout_started", "operation_id": "op", **sample})
    assert adapter.sample_payload == sample
    assert set(sink.records[0]["payload"]) == {
        *sample,
        "connection_binding_digest",
        "connection_admission",
    }
    adapter.close_execution("rollout_interrupted", "interrupted")
    assert sink.records[-1]["payload"] == {**sample, "outcome": "interrupted"}


def test_every_terminal_and_operation_of_a_connected_rollout_omits_the_keys():
    sink = Sink()
    asyncio.run(durable_rollout(**_connected_kwargs(sink, token())))
    assert sink.records[-1]["event"] == "rollout_finished"
    for record in sink.records:
        if record["event"] != "rollout_started":
            assert "connection_binding_digest" not in record["payload"]
            assert "connection_admission" not in record["payload"]


def test_an_unadmitting_writer_names_the_binding_and_never_claims():
    """`refuse_rows`: a synthetic start names `D` but is no boundary."""

    admission = token()
    sink = Sink()
    adapter = JournalRolloutAdapter(sink, connection=admission, admit=False)
    sample = {"case_id": "c", "sample_id": "s1", "candidate_digest": "candidate"}
    adapter.append({"event": "rollout_started", "operation_id": "op", **sample})
    adapter.close_execution("rollout_finished", "target_failed")
    assert sink.records[0]["payload"]["connection_binding_digest"] == BINDING
    assert sink.records[0]["payload"]["connection_admission"] is None
    assert sink.records[-1]["payload"] == {**sample, "outcome": "target_failed"}
    assert admission.claim() is not None


def test_without_a_token_nothing_changes():
    sink = Sink()
    _, kwargs = setup(sink)
    asyncio.run(durable_rollout(**kwargs, connection=None))
    start = next(r for r in sink.records if r["event"] == "rollout_started")
    assert set(start["payload"]) == {"case_id", "sample_id", "candidate_digest"}


def test_pre_write_checks_refuse_before_the_token_is_claimed():
    from evolution.tree.execution.journal_identity import IdentityError

    admission = token()
    sink = Sink()
    kwargs = _connected_kwargs(sink, admission)
    data = kwargs["identity"].to_payload()
    data["budget"]["timeout_seconds"] = 99
    kwargs["identity"] = RunIdentityV1(data)
    kwargs["budget"] = RolloutBudget(2, 99)
    with pytest.raises(IdentityError):
        asyncio.run(durable_rollout(**kwargs))
    sink.recovery_required = True
    with pytest.raises(JournalRecoveryRequired):
        asyncio.run(durable_rollout(**_connected_kwargs(sink, admission)))
    assert sink.records == []
    assert admission.claim() is not None


def test_a_refused_start_returns_the_admission():
    """A start the journal refused before writing it leaves the admission for
    the next start; a fault after which the bytes may exist keeps it spent."""

    admission = token()
    refused = Sink(fail="rollout_started", exception=JournalAdmissionRefused)
    with pytest.raises(JournalAdmissionRefused):
        asyncio.run(durable_rollout(**_connected_kwargs(refused, admission)))
    assert refused.records == []
    next_start = Sink()
    asyncio.run(durable_rollout(**_connected_kwargs(next_start, admission)))
    assert next_start.records[0]["payload"]["connection_admission"] == admission_payload()

    spent = token()
    faulted = Sink(fail="rollout_started", exception=JournalFault)
    with pytest.raises(JournalFault):
        asyncio.run(durable_rollout(**_connected_kwargs(faulted, spent)))
    assert spent.claim() is None


def test_a_real_marked_journal_admits_the_connected_start(tmp_path):
    from evolution import __version__
    from evolution.tree.execution.journal import Journal

    admission = token()
    sink = Sink()
    kwargs = _connected_kwargs(sink, admission)
    with Journal.create(
        tmp_path / "run",
        "run",
        kwargs["identity"],
        cases(),
        version_resolver=lambda _: __version__,
    ) as journal:
        asyncio.run(durable_rollout(**{**kwargs, "journal": journal}))
        records = journal.snapshot().records
    start = next(r for r in records if r.event == "rollout_started")
    assert start.payload["connection_admission"] == admission_payload()
    assert records[-1].event == "rollout_finished"


def admission_payload():
    return {"operation_id": "pre-1", "controller_invocation_id": "inv-1"}


def test_concurrent_tries_under_one_token_write_exactly_one_admission():
    """`rollout()` appends `rollout_started` synchronously on entry, so the
    claim needs no lock: both tries are parked inside the Target's identity call
    (after their starts) before either goes on, and exactly one start holds
    the admission."""

    admission = token()
    sinks = [Sink(), Sink()]
    gate = asyncio.Event()
    parked = []

    async def run():
        kwargs = [_connected_kwargs(sink, admission) for sink in sinks]
        for item in kwargs:
            instance = item["instance"]
            original = instance.identity

            async def identity(original=original, instance=instance):
                parked.append(instance)
                await gate.wait()
                return await original()

            instance.identity = identity
        tasks = [asyncio.create_task(durable_rollout(**item)) for item in kwargs]
        while len(parked) < 2:
            await asyncio.sleep(0)
        starts = [[r for r in sink.records if r["event"] == "rollout_started"] for sink in sinks]
        assert [len(s) for s in starts] == [1, 1]
        gate.set()
        await asyncio.gather(*tasks)
        return [s[0]["payload"]["connection_admission"] for s in starts]

    admissions = asyncio.run(run())
    assert sorted(admissions, key=lambda a: a is None) == [admission_payload(), None]
