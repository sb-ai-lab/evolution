"""V-S5-3: sampled public snapshots through controlled ASGI consumers."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import uuid

import pytest

from evolution_lab.owner import (
    CleanupFacts, Coverage, ExecutorIdentity, Failure, OperationResult,
    OperationStatus, OwnerError, Persistence, ProcessFacts, ResourceRef, StartFacts, TargetRef,
)
from evolution_lab.owner.progress import CampaignPlan, campaign_progress, evaluation_progress
from evolution_lab.service.app import create_app

PLAN = CampaignPlan("entry.py", 2, 1, "score")
TIMES = {"first_event_at": "2026-09-29T00:00:00Z", "last_event_at": "2026-09-29T00:01:00Z"}


def snapshot(*, kind="campaign", state="running", core=None, coverage=None,
             results="present", result=True, failure=None, starts=(),
             progress=None, progress_state="not_started", times=None, times_state="not_started",
             times_reason=None):
    # ADR-0019 marking as Owner's ``_views`` gives it: the source of present times, the
    # reason of unavailable ones ("unreadable" unless a test names another).
    times_source = None if times is None else {"campaign": "core_raw", "evaluation": "core_public"}[kind]
    if times_state == "unavailable" and times_reason is None:
        times_reason = "unreadable"
    return OperationStatus(
        id="operation", owner_generation="producing-generation", prepared_digest="a" * 64,
        target_ref=TargetRef("target", "producing-generation"), control_state=state,
        process=ProcessFacts(), target_cleanup=CleanupFacts.pending(),
        result=OperationResult("a" * 64, ProcessFacts(), core=core, coverage=coverage,
            persistence=Persistence("saved"), kind=kind) if result else None,
        failure=failure, results_state=results,
        executor=ExecutorIdentity("a" * 64, "b" * 64, "c" * 64, {},
            kind=f"lab-{kind}-worker/1"), kind=kind,
        retained_summary={"campaign_outcome": None, "dispositions": []},
        starts=starts, progress=progress, progress_state=progress_state,
        times=times, times_state=times_state, times_source=times_source, times_reason=times_reason,
    )


def viewed(*, core, **kwargs):
    """A campaign status as Owner's ``_views`` gives it after the journal was written."""
    return snapshot(core=core, progress=campaign_progress(core, PLAN, TIMES["last_event_at"]),
                    progress_state="present", times=TIMES, times_state="present", **kwargs)


def start_facts(number, *, scope_id=None):
    """A valid StartFacts for ``number``; OperationStatus overlays the *last*
    one's owner_generation/target_ref/process/target_cleanup/failure onto the
    snapshot's own top-level values, so those fields are placeholders here."""
    return StartFacts(
        number, scope_id or f"scope-{number}", "producing-generation",
        TargetRef("target", "producing-generation"), ProcessFacts(),
        CleanupFacts.pending(), None, "positive",
    )


class Reader:
    """Controlled public calls with the same retained/shielded lifetime as runtime."""

    def __init__(self, *values):
        self.values = list(values)
        self.stopping = asyncio.Event()
        self.calls = []
        self.completions = []
        self.owned = []
        self.active = 0

    async def call_owner(self, method, ref):
        assert method == "status"
        assert ref == ResourceRef("operation", "operation")
        self.calls.append(asyncio.get_running_loop().time())

        async def read():
            self.active += 1
            try:
                value = self.values.pop(0)
                if callable(value):
                    value = await value()
                if isinstance(value, Exception):
                    raise value
                return value
            finally:
                self.active -= 1
                self.completions.append(asyncio.get_running_loop().time())

        task = asyncio.create_task(read())
        self.owned.append(task)
        return await asyncio.shield(task)


class Consumer:
    def __init__(self, runtime, *, headers=(), send_hook=None, app=None):
        self.messages = []
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self.send_hook = send_hook
        self.scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "GET", "scheme": "http", "path":
            "/v1/operations/operation/events", "raw_path": b"/v1/operations/operation/events",
            "query_string": b"", "headers": list(headers), "server": ("lab", 0),
            "client": ("local", 0), "root_path": ""}
        self.task = asyncio.create_task((app or create_app(runtime))(
            self.scope, self.incoming.get, self.send))

    async def send(self, message):
        if self.send_hook is not None:
            await self.send_hook(message)
        self.messages.append(message)

    async def done(self):
        await asyncio.wait_for(self.task, 2)

    def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})

    def frames(self):
        frames = []
        for message in self.messages:
            body = message.get("body", b"")
            if body.startswith(b"event:"):
                fields = dict(line.split(": ", 1) for line in body.decode().strip().splitlines())
                fields["data"] = json.loads(fields["data"])
                frames.append(fields)
        return frames


@pytest.fixture
def fast_clock(monkeypatch):
    # Preserve scheduling semantics, shortening only the three specified intervals.
    try:
        from evolution_lab.service import events
    except ImportError:
        return
    monkeypatch.setattr(events, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.04)
    monkeypatch.setattr(events, "SEND_SECONDS", 0.08)


@pytest.mark.asyncio
async def test_initial_public_summary_and_terminal_old_generation():
    core = {"rounds": [{"attempts": [{"private": "secret-a"}, {}]}, {"attempts": [{}]}], "private": "secret-b"}
    runtime = Reader(viewed(state="orphaned", core=core))
    consumer = Consumer(runtime)
    await consumer.done()
    start = consumer.messages[0]
    assert start["status"] == 200
    assert dict(start["headers"])[b"cache-control"] == b"no-store"
    assert dict(start["headers"])[b"content-type"].startswith(b"text/event-stream")
    assert b"content-length" not in dict(start["headers"])
    frame, = consumer.frames()
    data = frame["data"]
    assert set(data) == {"schema", "operation_id", "owner_generation", "stream_id", "sequence", "kind", "payload"}
    assert data["schema"] == "lab-operation-event/3"
    assert data["operation_id"] == "operation"
    assert data["owner_generation"] == "producing-generation"
    assert uuid.UUID(hex=data["stream_id"]).hex == data["stream_id"]
    assert data["sequence"] == 1 and data["kind"] == "status"
    assert frame["event"] == "status" and frame["id"] == data["stream_id"] + ":1"
    # The CLI's closed decoder accepts exactly what the service emits.
    from evolution_lab.cli.responses import operation_event
    assert operation_event(data) == data
    assert data["payload"] == {
        "kind": "campaign", "control_state": "orphaned",
        "process": {"launched": None, "exit_code": None, "cleanup_complete": None, "forced": None},
        "target_cleanup": {"state": "pending", "complete": None}, "results_state": "present",
        "failure": None, "result_available": True, "persistence": {"state": "saved", "failure": None},
        "progress": campaign_progress(core, PLAN, TIMES["last_event_at"]), "progress_state": "present",
        "times": TIMES, "times_state": "present", "times_source": "core_raw", "times_reason": None,
        "start": 1}
    assert b"secret" not in b"".join(m.get("body", b"") for m in consumer.messages)
    assert len(runtime.calls) == 1
    assert consumer.messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_summary_start_is_the_latest_start_not_the_first():
    consumer = Consumer(Reader(snapshot(starts=(start_facts(1), start_facts(2)))))
    await consumer.done()
    assert consumer.frames()[0]["data"]["payload"]["start"] == 2


@pytest.mark.asyncio
async def test_changes_only_sequence_and_poll_after_read_completion(fast_clock):
    async def delayed_same():
        await asyncio.sleep(0.055)
        return snapshot(core={"rounds": [], "timestamp": "changed"})

    runtime = Reader(snapshot(core={"rounds": [], "timestamp": "initial"}), delayed_same,
        viewed(core={"rounds": [{"attempts": [{}]}]}),
        snapshot(state="finished", result=False, progress_state="unavailable", times_state="unavailable"))
    consumer = Consumer(runtime)
    await consumer.done()
    frames = consumer.frames()
    assert [f["data"]["sequence"] for f in frames] == [1, 2, 3]
    # Campaign without a journal -> not_started; after a record -> present with a phase.
    assert [f["data"]["payload"]["progress_state"] for f in frames] == ["not_started", "present", "unavailable"]
    assert [(f["data"]["payload"]["progress"] or {}).get("phase") for f in frames] == [None, "waiting", None]
    assert len({f["data"]["stream_id"] for f in frames}) == 1
    assert all(start - end >= 0.018 for start, end in zip(runtime.calls[1:], runtime.completions))
    assert any(m.get("body", b"").startswith(b":") for m in consumer.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("core,expected", [
    (None, (None, None)), ({}, (None, None)), ({"rounds": {}}, (None, None)),
    ({"rounds": [{"attempts": []}, {}]}, (None, None)),
    ({"rounds": [{"attempts": "bad"}]}, (None, None)),
    ({"rounds": [None]}, (None, None)), ({"rounds": []}, (0, 0)),
])
async def test_campaign_unavailable_shape_does_not_invent_counters(core, expected):
    # The event carries the status' own progress; it derives nothing from ``core``.
    consumer = Consumer(Reader(snapshot(state="finished", core=core, progress_state="unavailable",
                                        times_state="unavailable")))
    await consumer.done()
    summary = consumer.frames()[0]["data"]["payload"]
    assert summary["progress"] is None and summary["progress_state"] == "unavailable"
    assert summary["times"] is None and summary["times_state"] == "unavailable"
    assert summary["times_source"] is None and summary["times_reason"] == "unreadable"
    assert summary["result_available"] is (core is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["campaign", "evaluation"])
@pytest.mark.parametrize("results", ["present", "deleting", "delete_failed", "deleted"])
async def test_delete_clears_public_counters_without_exposing_retained_payload(kind, results):
    available = results == "present"
    core = {"rounds": [{"attempts": [{}]}]}
    # As Owner's ``_views``: removed results -> both sections unavailable.
    progress = (evaluation_progress(Coverage(8, 3), None) if kind == "evaluation"
                else campaign_progress(core, PLAN, None)) if available else None
    consumer = Consumer(Reader(snapshot(kind=kind, state="released", results=results,
        core=core, coverage=Coverage(8, 3), progress=progress,
        progress_state="present" if available else "unavailable",
        times_state="not_started" if available else "unavailable")))
    await consumer.done()
    summary = consumer.frames()[0]["data"]["payload"]
    assert summary["result_available"] is available
    assert summary["progress"] == progress
    assert summary["progress_state"] == ("present" if available else "unavailable")
    assert "retained_summary" not in summary and "core" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["finished", "orphaned", "cleanup_required", "released"])
async def test_terminal_states_close_after_one_snapshot_and_fresh_connection(state):
    runtime = Reader(snapshot(kind="evaluation", state=state, result=False), snapshot(kind="evaluation", state=state, result=False))
    first, second = Consumer(runtime), Consumer(runtime)
    await asyncio.gather(first.done(), second.done())
    left, right = first.frames()[0]["data"], second.frames()[0]["data"]
    assert left["stream_id"] != right["stream_id"]
    assert left["sequence"] == right["sequence"] == 1
    assert left["payload"]["progress"] is None and left["payload"]["progress_state"] == "not_started"
    assert left["payload"]["persistence"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [b"", b"old-stream:45"])
async def test_last_event_id_presence_refuses_before_read(header):
    runtime = Reader()
    consumer = Consumer(runtime, headers=[(b"last-event-id", header)])
    await consumer.done()
    assert consumer.messages[0]["status"] == 409
    assert json.loads(consumer.messages[-1]["body"])["error"]["code"] == "history_unavailable"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_initial_failure_is_http_error_and_later_failure_one_safe_frame(fast_clock):
    initial = Consumer(Reader(OwnerError("not_found", "read")))
    await initial.done()
    assert initial.messages[0]["status"] == 404
    assert initial.frames() == []
    later = Consumer(Reader(snapshot(), RuntimeError("private exception")))
    await later.done()
    assert later.messages[0]["status"] == 200
    frames = later.frames()
    assert [f["event"] for f in frames] == ["status", "error"]
    assert frames[1] == {"event": "error", "data": {"error": {
        "code": "lab_error", "stage": "transport", "message": "The service operation failed."}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("during_factory", [False, True])
async def test_request_arriving_after_shutdown_gets_fixed_503_before_stream(during_factory):
    runtime = Reader()
    if during_factory:
        from evolution_lab.service.events import event_response

        def handler(operation_id, request):
            response = event_response(runtime, operation_id, request)
            runtime.stopping.set()
            return response

        app = create_app(runtime, event_handler=handler)
    else:
        runtime.stopping.set()
        app = create_app(runtime)
    consumer = Consumer(runtime, app=app)
    await consumer.done()
    assert consumer.messages[0]["status"] == 503
    assert json.loads(consumer.messages[-1]["body"]) == {"error": {
        "code": "service_stopping", "stage": "transport", "message": "The service is stopping."}}
    assert consumer.frames() == []
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_summary_retains_fixed_owner_failure_and_persistence():
    failure = Failure.make("io_error", "read")
    value = snapshot(state="cleanup_required", failure=failure)
    value = replace(value, result=replace(value.result, persistence=Persistence("failed", failure)))
    consumer = Consumer(Reader(value))
    await consumer.done()
    summary = consumer.frames()[0]["data"]["payload"]
    assert summary["failure"] == failure.to_payload()
    assert summary["persistence"] == {"state": "failed", "failure": failure.to_payload()}


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("initial", [False, True])
async def test_disconnect_or_shutdown_detaches_pending_read_without_canceling_owner(fast_clock, shutdown, initial):
    entered, release, heartbeat = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pending():
        entered.set()
        await release.wait()
        return snapshot()

    runtime = Reader(*([pending] if initial else [snapshot(), pending]))

    async def sent(message):
        if message.get("body", b"").startswith(b":"):
            heartbeat.set()

    consumer = Consumer(runtime, send_hook=sent)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if initial:
            assert consumer.messages == []
        else:
            await asyncio.wait_for(heartbeat.wait(), 1)
            assert runtime.active == 1
        runtime.stopping.set() if shutdown else consumer.disconnect()
        await consumer.done()
        assert not runtime.owned[-1].done()
        assert runtime.active == 1
    finally:
        release.set()
        await asyncio.gather(*runtime.owned)
    assert len(runtime.calls) == (1 if initial else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["deadline", "disconnect", "shutdown"])
async def test_actual_asgi_slow_send_is_bounded_and_does_not_prefetch(fast_clock, ending):
    entered, canceled = asyncio.Event(), asyncio.Event()
    runtime = Reader(snapshot())

    async def block(message):
        if message.get("body", b"").startswith(b"event:"):
            assert runtime.active == 0  # Reader lease released before network send.
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

    consumer = Consumer(runtime, send_hook=block)
    await asyncio.wait_for(entered.wait(), 1)
    if ending == "disconnect":
        consumer.disconnect()
    elif ending == "shutdown":
        runtime.stopping.set()
    await consumer.done()
    assert canceled.is_set()
    assert len(runtime.calls) == 1
    assert consumer.frames() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deadline", "disconnected"])
async def test_failed_heartbeat_send_closes_without_a_status_error_or_new_read(fast_clock, failure):
    release = asyncio.Event()

    async def pending():
        await release.wait()
        return snapshot()

    runtime = Reader(snapshot(), pending)
    attempted = []

    async def block_heartbeat(message):
        body = message.get("body", b"")
        if body:
            attempted.append(body)
        if body.startswith(b":"):
            if failure == "disconnected":
                raise OSError("peer closed")
            await asyncio.Event().wait()

    consumer = Consumer(runtime, send_hook=block_heartbeat)
    try:
        await consumer.done()
        assert len(attempted) == 2
        assert attempted[0].startswith(b"event: status")
        assert attempted[1].startswith(b":")
        assert not runtime.owned[-1].done()
    finally:
        release.set()
        await asyncio.gather(*runtime.owned)
    assert len(runtime.calls) == 2


@pytest.mark.asyncio
async def test_delete_transition_replaces_progress_and_later_owner_failure_stays_fixed(fast_clock):
    runtime = Reader(viewed(core={"rounds": [{"attempts": [{}]}]}),
        snapshot(core={"rounds": [{"attempts": [{}]}]}, results="deleting",
                 progress_state="unavailable", times_state="unavailable"),
        OwnerError("resource_busy", "read"))
    consumer = Consumer(runtime)
    await consumer.done()
    first, deletion, error = consumer.frames()
    assert first["data"]["payload"]["result_available"] is True
    assert deletion["data"]["sequence"] == 2
    assert deletion["data"]["payload"]["result_available"] is False
    assert deletion["data"]["payload"]["progress"] is None
    assert deletion["data"]["payload"]["progress_state"] == "unavailable"
    assert error == {"event": "error", "data": {"error": Failure.make("resource_busy", "read").to_payload()}}


@pytest.mark.asyncio
async def test_explicit_event_handler_injection_stays_available():
    from starlette.responses import Response

    async def handler(operation_id, request):
        assert operation_id == "operation"
        return Response(b"injected")

    consumer = Consumer(object(), app=create_app(object(), event_handler=handler))
    await consumer.done()
    assert consumer.messages[0]["status"] == 200
    assert consumer.messages[-1]["body"] == b"injected"
