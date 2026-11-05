"""`TargetCallLedger` (ADR-0014).

The first half drives the state machine through its transport hooks with no
socket at all, so every row of the table and both event orders are pinned
deterministically. The second half drives `HttpTransport` against
scripted loopback sockets.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from evolution.tree.execution.interaction import RolloutInfrastructureError
from evolution.tree.execution.wire_v2 import PREFIX, parse_session
from evolution.tree.target.config import TargetBinding, Timeouts
from evolution.tree.target.http_adapter import HttpTransport, _fail, _json_call
from evolution.tree.target.target_calls import (
    TargetCallLedger,
    TargetCallRecord,
    network_outcome,
)
from evolution.tree.target.target_server import _error_payload
from tests.tree.target.test_http_adapter import unused_port

NS = "run-a"
IID = "run-a-0123456789abcdef0123456789abcdef"


def admit(ledger, method="POST", path=f"{PREFIX}/instances/{IID}/sessions", body=None):
    call = ledger._admit(method, path, body)
    assert call is not None
    return call


def invariant(snap):
    assert snap.admitted == snap.completed + snap.not_sent + snap.uncertain + snap.pending
    assert snap.in_flight <= snap.pending
    assert len(snap.records) == snap.pending + snap.uncertain
    assert snap.uncertain_any is (snap.uncertain > 0)
    assert snap.quiescent is (snap.admission_closed and snap.in_flight == 0 and snap.pending == 0)


def test_a_fresh_ledger_reports_nothing():
    snap = TargetCallLedger().report()
    assert snap.namespace is None
    assert (snap.admitted, snap.in_flight, snap.pending, snap.refused) == (0, 0, 0, 0)
    assert snap.bound_transports == 0
    assert snap.coverage == "bound_transports"
    assert snap.records == ()
    assert snap.quiescent is False
    invariant(snap)


def test_get_is_neither_counted_nor_refused():
    ledger = TargetCallLedger()
    assert ledger._admit("GET", f"{PREFIX}/health", None) is None
    ledger.close()
    assert ledger._admit("GET", f"{PREFIX}/health", None) is None
    snap = ledger.report()
    assert (snap.admitted, snap.refused) == (0, 0)
    assert snap.quiescent is True


def test_close_refuses_post_and_delete_with_admission_closed():
    ledger = TargetCallLedger()
    ledger.close()
    ledger.close()  # idempotent
    for method in ("POST", "DELETE"):
        with pytest.raises(RolloutInfrastructureError) as caught:
            ledger._admit(method, f"{PREFIX}/instances/{IID}", None)
        assert caught.value.code == "admission_closed"
        assert IID not in str(caught.value)
    snap = ledger.report()
    assert (snap.refused, snap.admitted) == (2, 0)
    invariant(snap)


@pytest.mark.parametrize(
    ("method", "path", "body", "operation"),
    [
        ("POST", f"{PREFIX}/instances", {"instance_id": IID}, "create_instance"),
        ("DELETE", f"{PREFIX}/instances/{IID}", None, "release_instance"),
        ("POST", f"{PREFIX}/instances/{IID}/sessions", {}, "open_session"),
        ("DELETE", f"{PREFIX}/instances/{IID}/sessions/s1", None, "close_session"),
        ("POST", f"{PREFIX}/instances/{IID}/sessions/s1/turns", {}, "turn"),
        ("POST", f"{PREFIX}/instances/{IID}/other", {}, "unknown"),
        ("DELETE", "/elsewhere", None, "unknown"),
    ],
)
def test_operation_and_instance_id_come_from_the_path(method, path, body, operation):
    ledger = TargetCallLedger()
    ledger.bind_namespace(NS)
    call = admit(ledger, method, path, body)
    (record,) = ledger.report().records
    assert record == TargetCallRecord(
        operation=operation,
        instance_id=None if operation == "unknown" else IID,
        state="pending",
        abandoned=False,
        reason=None,
    )
    assert ledger._start(call) is True


def test_instance_id_outside_the_namespace_is_hidden():
    ledger = TargetCallLedger()
    ledger.bind_namespace("run-b")
    admit(ledger, "DELETE", f"{PREFIX}/instances/{IID}")
    admit(ledger, "POST", f"{PREFIX}/instances", {"instance_id": "session-secret"})
    assert [r.instance_id for r in ledger.report().records] == [None, None]
    unbound = TargetCallLedger()
    admit(unbound, "DELETE", f"{PREFIX}/instances/{IID}")
    assert unbound.report().records[0].instance_id is None


def test_one_namespace_per_ledger():
    ledger = TargetCallLedger()
    ledger.bind_namespace(NS)
    ledger.bind_namespace(NS)
    with pytest.raises(ValueError):
        ledger.bind_namespace("run-b")
    assert ledger.report().namespace == NS


# (delivery, io outcome, io reason) -> (class counter, record reason)
TABLE = [
    (False, "response", None, "completed", None),
    (False, "not_sent", None, "not_sent", None),
    (False, "uncertain", "callee_timeout", "uncertain", "callee_timeout"),
    (True, "response", None, "uncertain", "abandoned"),
    (True, "not_sent", None, "not_sent", None),
    (True, "uncertain", "transport_error", "uncertain", "transport_error"),
]


@pytest.mark.parametrize("io_first", [True, False])
@pytest.mark.parametrize(("abandoned", "outcome", "reason", "klass", "record_reason"), TABLE)
def test_the_class_is_assigned_once_both_events_happened_in_either_order(
    io_first, abandoned, outcome, reason, klass, record_reason
):
    ledger = TargetCallLedger()
    call = admit(ledger)
    assert ledger._start(call) is True
    first, second = (
        (
            lambda: ledger._io_end(call, outcome, reason),
            lambda: ledger._deliver(call, abandoned=abandoned),
        )
        if io_first
        else (
            lambda: ledger._deliver(call, abandoned=abandoned),
            lambda: ledger._io_end(call, outcome, reason),
        )
    )
    first()
    midway = ledger.report()
    assert midway.pending == 1
    assert midway.in_flight == (0 if io_first else 1)
    assert midway.records[0].abandoned is (abandoned and not io_first)
    invariant(midway)
    second()
    snap = ledger.report()
    assert getattr(snap, klass) == 1
    assert (snap.pending, snap.in_flight) == (0, 0)
    records = snap.records
    if klass == "uncertain":
        assert records == (
            TargetCallRecord("open_session", None, "uncertain", abandoned, record_reason),
        )
    else:
        assert records == ()
    invariant(snap)


def test_abandoned_before_start_is_withdrawn_and_not_sent():
    ledger = TargetCallLedger()
    call = admit(ledger)
    ledger._deliver(call, abandoned=True)
    snap = ledger.report()
    assert (snap.not_sent, snap.pending, snap.in_flight) == (1, 0, 0)
    assert ledger._start(call) is False  # the late worker must not open a socket
    invariant(ledger.report())


def test_an_admitted_call_not_yet_started_is_in_flight():
    ledger = TargetCallLedger()
    admit(ledger)
    snap = ledger.report()
    assert (snap.in_flight, snap.pending) == (1, 1)


def test_uncertainty_is_sticky():
    ledger = TargetCallLedger()
    bad = admit(ledger)
    ledger._start(bad)
    ledger._io_end(bad, "uncertain", "callee_timeout")
    ledger._deliver(bad, abandoned=False)
    good = admit(ledger)
    ledger._start(good)
    ledger._io_end(good, "response", None)
    ledger._deliver(good, abandoned=False)
    snap = ledger.report()
    assert snap.uncertain_any is True
    assert (snap.completed, snap.uncertain) == (1, 1)


def test_bound_transports_counts_bindings():
    ledger = TargetCallLedger()
    ledger._bind_transport()
    ledger._bind_transport()
    assert ledger.report().bound_transports == 2


async def test_settle_waits_for_io_end_only():
    ledger = TargetCallLedger()
    call = admit(ledger)
    ledger._start(call)
    timer = threading.Timer(0.1, lambda: ledger._io_end(call, "response", None))
    timer.start()
    started = time.monotonic()
    snap = await ledger.settle()
    assert time.monotonic() - started < 2.0
    assert (snap.in_flight, snap.pending) == (0, 1)  # delivery has not happened
    assert snap.quiescent is False


async def test_settle_with_timeout_returns_a_snapshot_without_raising():
    ledger = TargetCallLedger()
    admit(ledger)
    snap = await ledger.settle(timeout=0.05)
    assert snap.in_flight == 1


async def test_settle_does_not_need_the_default_executor():
    await asyncio.get_running_loop().shutdown_default_executor()
    snap = await TargetCallLedger().settle(timeout=0.05)
    assert snap.in_flight == 0


def _settle_thread_alive() -> bool:
    return any(t.name == "target-call-settle" and t.is_alive() for t in threading.enumerate())


def test_cancelling_settle_ends_the_waiter_thread_promptly():
    ledger = TargetCallLedger()
    admit(ledger)  # admitted, never started, I/O never ends

    async def cancelled_settle():
        await asyncio.wait_for(ledger.settle(), 0.05)

    with pytest.raises(TimeoutError):
        asyncio.run(cancelled_settle())

    deadline = time.monotonic() + 2.0
    while _settle_thread_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _settle_thread_alive()


def test_io_end_after_a_cancelled_settles_loop_closed_raises_nothing():
    ledger = TargetCallLedger()
    call = admit(ledger)
    ledger._start(call)
    errors = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: errors.append(args)
    try:

        async def cancelled_settle():
            await asyncio.wait_for(ledger.settle(), 0.05)

        with pytest.raises(TimeoutError):
            asyncio.run(cancelled_settle())  # the loop is closed by the time this returns

        deadline = time.monotonic() + 2.0
        while _settle_thread_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _settle_thread_alive()  # must not linger past the closed loop

        ledger._io_end(call, "response", None)  # must not raise in any thread
    finally:
        threading.excepthook = previous_hook
    assert errors == []
    assert ledger.report().in_flight == 0


def test_close_and_report_from_other_threads_keep_the_invariant():
    ledger = TargetCallLedger()
    stop = threading.Event()
    seen = []

    def reader():
        while not stop.is_set():
            seen.append(ledger.report())

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in readers:
        thread.start()
    for index in range(500):
        try:
            call = admit(ledger)
        except RolloutInfrastructureError:
            break
        ledger._start(call)
        ledger._io_end(call, "response", None)
        ledger._deliver(call, abandoned=index % 7 == 0)
        if index == 250:
            threading.Thread(target=ledger.close).start()
    stop.set()
    for thread in readers:
        thread.join()
    for snap in seen:
        invariant(snap)


@pytest.mark.parametrize(
    ("sent", "failure", "complete", "expected"),
    [
        (False, _fail("x", "transport_error"), False, ("not_sent", None)),
        (False, _fail("x", "invalid_request"), False, ("not_sent", None)),
        (True, None, True, ("response", None)),
        (True, None, False, ("uncertain", "incomplete_response")),
        (True, _fail("x", "callee_timeout"), False, ("uncertain", "callee_timeout")),
        (True, _fail("x", "response_too_large"), False, ("uncertain", "response_too_large")),
        (True, _fail("x", "transport_error"), False, ("uncertain", "transport_error")),
        (True, ValueError("chunk"), False, ("uncertain", "incomplete_response")),
    ],
)
def test_network_outcome(sent, failure, complete, expected):
    assert network_outcome(sent, failure, complete) == expected


def test_network_outcome_names_a_truncated_chunked_body_incomplete():
    import http.client

    failure = _fail("x", "transport_error")
    failure.__cause__ = http.client.IncompleteRead(b"")
    assert network_outcome(True, failure, False) == ("uncertain", "incomplete_response")


# -- HttpTransport over scripted loopback sockets ---------------------

OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
ERR = (
    b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n"
    b"Content-Length: 2\r\n\r\n{}"
)
CREATE = (f"{PREFIX}/instances", {"instance_id": IID})


class ScriptedServer:
    """A loopback listener that reads one request per connection and then runs
    `action(conn, server)`. `connections` counts accepted connections."""

    def __init__(self, action):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.action = action
        self.connections = 0
        self.release = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            while len(rest) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                rest += chunk
            try:
                self.action(conn, self)
            except OSError:
                pass

    def close(self):
        self.release.set()
        self.sock.close()


def respond(raw):
    def action(conn, server):
        conn.sendall(raw)
        # A half-close (rather than letting `with conn:` jump straight to a full
        # close) sends a clean FIN instead of an RST. Kept as hygiene: a full
        # close right after `sendall` was suspected of being able to race the
        # client's next `recv` and abort the connection before it observed a
        # graceful EOF for a close-delimited body, whose framing depends on that
        # EOF — the race was never actually reproduced on this host.
        conn.shutdown(socket.SHUT_WR)

    return action


def hang_then(raw):
    def action(conn, server):
        server.release.wait(10)
        conn.sendall(raw)
        conn.shutdown(socket.SHUT_WR)

    return action


def reset(conn, server):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


async def _await_worker_started(server: ScriptedServer, timeout: float = 5.0) -> None:
    """Block until the worker's connect() has been accepted.

    An accepted connection proves the executor actually ran the worker (opened
    the socket, called `_start`), which a fixed `sleep` before `task.cancel()`
    only assumes: under load the thread pool can take longer than the sleep to
    pick the job up, and a cancel that lands first withdraws the call instead of
    abandoning it — a different, untested path. Polling `server.connections`
    removes the assumption.
    """
    deadline = time.monotonic() + timeout
    while server.connections < 1:
        if time.monotonic() >= deadline:
            raise AssertionError("the worker never opened its connection")
        await asyncio.sleep(0.01)


@pytest.fixture
def scripted():
    made = []

    def _make(action):
        server = ScriptedServer(action)
        made.append(server)
        return server

    yield _make
    for server in made:
        server.close()


def transport_for(port, ledger, *, timeouts=Timeouts(2.0, 5.0), recorder=None, **kwargs):
    binding = TargetBinding(
        base_url=f"http://127.0.0.1:{port}",
        seed_mode="exact",
        contract_digest=None,
        settings_hash=None,
        timeouts=timeouts,
        **kwargs,
    )
    return HttpTransport(binding, recorder=recorder, ledger=ledger)


def only(snap, **expected):
    for name, value in expected.items():
        assert getattr(snap, name) == value, (name, snap)
    invariant(snap)


def test_a_transport_without_a_ledger_is_unchanged(scripted):
    server = scripted(respond(OK))
    transport = transport_for(server.port, None)
    assert transport.ledger is None
    assert asyncio.run(transport.request("POST", *CREATE))[0] == 200


@pytest.mark.parametrize("raw", [OK, ERR])
def test_a_full_response_is_completed_whatever_the_status(scripted, raw):
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)
    asyncio.run(transport.request("POST", *CREATE))
    only(ledger.report(), completed=1, bound_transports=1, pending=0, in_flight=0)


def test_get_through_a_ledger_is_not_counted(scripted):
    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    asyncio.run(transport_for(server.port, ledger).request("GET", f"{PREFIX}/health"))
    only(ledger.report(), admitted=0)


def test_a_refused_connection_is_not_sent():
    ledger = TargetCallLedger()
    transport = transport_for(unused_port(), ledger)
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("POST", *CREATE))
    assert caught.value.code == "transport_error"
    only(ledger.report(), not_sent=1)


def test_an_unserializable_body_is_not_sent():
    ledger = TargetCallLedger()
    transport = transport_for(unused_port(), ledger)
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("POST", f"{PREFIX}/instances", {"x": object()}))
    assert caught.value.code == "invalid_request"
    only(ledger.report(), not_sent=1)


def test_an_unserializable_body_on_a_closed_ledger_is_refused_first():
    ledger = TargetCallLedger()
    ledger.close()
    transport = transport_for(unused_port(), ledger)
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("POST", f"{PREFIX}/instances", {"x": object()}))
    assert caught.value.code == "admission_closed"
    only(ledger.report(), admitted=0, refused=1)


def test_the_body_is_serialized_before_the_call_is_admitted(scripted, monkeypatch):
    from evolution.tree.target import http_adapter

    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)
    seen = []
    original = http_adapter.canonical_body

    def observing(value):
        snap = ledger.report()
        seen.append((snap.admitted, snap.in_flight))
        return original(value)

    monkeypatch.setattr(http_adapter, "canonical_body", observing)
    asyncio.run(transport.request("POST", *CREATE))
    assert seen == [(0, 0)]
    only(ledger.report(), admitted=1, completed=1)


def test_unledgered_serialization_never_runs_on_the_loop_thread(scripted, monkeypatch):
    """Without a ledger, `request` must do no serialization on the loop
    (ADR-0014, base revision 679f5714): `canonical_body` should only ever run
    inside the `asyncio.to_thread` worker, never on the thread that owns the
    event loop."""
    from evolution.tree.target import http_adapter

    server = scripted(respond(OK))
    threads: dict[str, int] = {}
    original = http_adapter.canonical_body

    def observing(value):
        threads["serialize"] = threading.get_ident()
        return original(value)

    monkeypatch.setattr(http_adapter, "canonical_body", observing)
    transport = transport_for(server.port, None)

    async def run() -> None:
        threads["loop"] = threading.get_ident()
        await transport.request("POST", *CREATE)

    asyncio.run(run())
    assert threads["serialize"] != threads["loop"]


def test_ledgered_serialization_runs_on_the_loop_thread(scripted, monkeypatch):
    """With a ledger bound, today's behaviour is unchanged: the body is
    serialized in the coroutine, before admission, so `in_flight`/`settle()`
    cover socket I/O only (ADR-0014)."""
    from evolution.tree.target import http_adapter

    server = scripted(respond(OK))
    threads: dict[str, int] = {}
    original = http_adapter.canonical_body

    def observing(value):
        threads["serialize"] = threading.get_ident()
        return original(value)

    monkeypatch.setattr(http_adapter, "canonical_body", observing)
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)

    async def run() -> None:
        threads["loop"] = threading.get_ident()
        await transport.request("POST", *CREATE)

    asyncio.run(run())
    assert threads["serialize"] == threads["loop"]


def test_the_transport_ledger_cannot_be_rebound():
    ledger = TargetCallLedger()
    transport = transport_for(unused_port(), ledger)
    with pytest.raises(AttributeError):
        transport.ledger = TargetCallLedger()
    assert transport.ledger is ledger


def test_a_silent_target_is_uncertain_callee_timeout(scripted):
    server = scripted(hang_then(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger, timeouts=Timeouts(0.5, 0.2))
    with pytest.raises(RolloutInfrastructureError):
        asyncio.run(transport.request("POST", *CREATE))
    snap = ledger.report()
    only(snap, uncertain=1)
    assert snap.records[0].reason == "callee_timeout"


def test_a_reset_after_the_request_is_uncertain_transport_error(scripted):
    server = scripted(reset)
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError):
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    snap = ledger.report()
    only(snap, uncertain=1)
    assert snap.records[0].reason == "transport_error"


def test_an_oversize_reply_is_uncertain_response_too_large(scripted):
    body = b"x" * 4096
    raw = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4096\r\n\r\n" + body
    )
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger, max_response_bytes=1024)
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("POST", *CREATE))
    assert caught.value.code == "response_too_large"
    assert ledger.report().records[0].reason == "response_too_large"


def test_a_short_content_length_is_incomplete_and_still_returns_its_bytes(scripted):
    raw = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"a":1}'
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    status, received, _media = asyncio.run(
        transport_for(server.port, ledger).request("POST", *CREATE)
    )
    assert (status, received) == (200, b'{"a":1}')  # the transport's return is unchanged
    snap = ledger.report()
    only(snap, uncertain=1)
    assert snap.records[0].reason == "incomplete_response"


def test_a_truncated_chunked_body_is_incomplete(scripted):
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab"
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError):
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert ledger.report().records[0].reason == "incomplete_response"


_CHUNKED_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
)


@pytest.mark.parametrize(
    "tail",
    [b"0\n\n", b"0\r\nX-A: b\r\n c\r\n\r\n"],
    ids=["bare_lf_terminator", "obs_fold_trailer"],
)
def test_a_chunked_body_with_a_lenient_terminator_is_completed(scripted, tail):
    server = scripted(respond(_CHUNKED_HEAD + b"2\r\n{}\r\n" + tail))
    ledger = TargetCallLedger()
    asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    only(ledger.report(), completed=1)


@pytest.mark.parametrize(
    "tail",
    [b"0\n", b"0\r\nX-A: b\r\n c\r\n"],
    ids=["bare_lf_last_chunk_then_eof", "eof_after_fold_line"],
)
def test_a_chunked_body_cut_after_a_lf_line_is_incomplete(scripted, tail):
    server = scripted(respond(_CHUNKED_HEAD + b"2\r\n{}\r\n" + tail))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError):
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    snap = ledger.report()
    only(snap, uncertain=1)
    assert snap.records[0].reason == "incomplete_response"


def test_a_close_delimited_body_is_completed(scripted):
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}"
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    only(ledger.report(), completed=1)


def test_close_refuses_before_the_socket(scripted):
    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)
    ledger.close()
    for method, path, body in (("POST", *CREATE), ("DELETE", f"{PREFIX}/instances/{IID}", None)):
        with pytest.raises(RolloutInfrastructureError) as caught:
            asyncio.run(transport.request(method, path, body))
        assert caught.value.code == "admission_closed"
    time.sleep(0.1)
    assert server.connections == 0
    only(ledger.report(), refused=2, admitted=0)


def test_an_admitted_call_finishes_after_close(scripted):
    server = scripted(hang_then(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)

    async def main():
        task = asyncio.create_task(transport.request("POST", *CREATE))
        await asyncio.sleep(0.2)
        ledger.close()
        server.release.set()
        await task

    asyncio.run(main())
    only(ledger.report(), completed=1, quiescent=True)


def test_cancel_after_start_is_abandoned_and_a_late_reply_does_not_clear_it(scripted):
    server = scripted(hang_then(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)

    async def main():
        task = asyncio.create_task(transport.request("POST", *CREATE))
        await _await_worker_started(server)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        midway = ledger.report()
        only(midway, pending=1, in_flight=1)
        assert midway.records[0].abandoned is True
        server.release.set()
        return await ledger.settle()

    snap = asyncio.run(main())
    only(snap, uncertain=1, in_flight=0, pending=0)
    assert snap.records[0].reason == "abandoned"


def test_cancel_after_start_then_a_timeout_keeps_the_network_reason(scripted):
    server = scripted(hang_then(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger, timeouts=Timeouts(0.5, 0.3))

    async def main():
        task = asyncio.create_task(transport.request("POST", *CREATE))
        await _await_worker_started(server)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await ledger.settle()

    snap = asyncio.run(main())
    only(snap, uncertain=1)
    assert (snap.records[0].reason, snap.records[0].abandoned) == ("callee_timeout", True)


def test_cancel_before_the_worker_starts_is_withdrawn(scripted):
    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        gate = threading.Event()
        blocker = loop.run_in_executor(None, gate.wait)
        task = asyncio.create_task(transport.request("POST", *CREATE))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        snap = ledger.report()
        gate.set()
        await blocker
        await asyncio.sleep(0.2)
        return snap

    snap = asyncio.run(main())
    only(snap, not_sent=1, in_flight=0, pending=0)
    assert server.connections == 0


def test_an_executor_that_never_runs_the_worker_is_not_sent(scripted):
    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger)

    async def main():
        await asyncio.get_running_loop().shutdown_default_executor()
        with pytest.raises(RuntimeError):
            await transport.request("POST", *CREATE)
        return await ledger.settle(timeout=1.0)

    snap = asyncio.run(main())
    only(snap, not_sent=1, in_flight=0, pending=0)
    assert server.connections == 0


def blocking_recorder():
    entered, gate = threading.Event(), threading.Event()

    def record(exchange):
        entered.set()
        gate.wait(10)

    return record, entered, gate


def test_a_blocked_recorder_is_pending_not_in_flight(scripted):
    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    record, entered, gate = blocking_recorder()
    transport = transport_for(server.port, ledger, recorder=record)

    async def main():
        task = asyncio.create_task(transport.request("POST", *CREATE))
        await asyncio.to_thread(entered.wait, 5)
        settled = await ledger.settle(timeout=2.0)
        gate.set()
        await task
        return settled

    settled = asyncio.run(main())
    only(settled, in_flight=0, pending=1)
    assert settled.quiescent is False
    only(ledger.report(), completed=1)


def test_a_full_reply_whose_delivery_was_cancelled_is_abandoned(scripted):
    server = scripted(respond(OK))
    ledger = TargetCallLedger()
    record, entered, gate = blocking_recorder()
    transport = transport_for(server.port, ledger, recorder=record)

    async def main():
        task = asyncio.create_task(transport.request("POST", *CREATE))
        await asyncio.to_thread(entered.wait, 5)  # I/O has ended; delivery has not
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        return await ledger.settle()

    snap = asyncio.run(main())
    only(snap, uncertain=1)
    assert snap.records[0].reason == "abandoned"


# -- an answer sent before the body is written ---------------------------

# Larger than any loopback socket buffer pair, so the client is still writing
# when the server closes with the body unread.
BIG = {"padding": "x" * (32 << 20)}
_REFUSAL = _error_payload("invalid_request", "content_length")
TOO_LARGE = (
    b"HTTP/1.1 413 Content Too Large\r\nContent-Type: application/json\r\n"
    b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(_REFUSAL)
) + _REFUSAL


class EarlyServer:
    """A loopback listener that reads only the request head and then runs
    `action(conn)`: the body is never read, as `0.3.0a28`'s reference target
    refused an oversized request."""

    def __init__(self, action):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.action = action
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            try:
                self.action(conn)
            except OSError:
                pass
            # Leaving `with conn:` closes with the body unread: the kernel resets
            # the connection, and the client's blocked write fails.

    def close(self):
        self.sock.close()


def answer_then_reset(raw):
    def action(conn):
        conn.sendall(raw)
        conn.shutdown(socket.SHUT_WR)
        time.sleep(0.2)  # the answer reaches the client before the reset

    return action


def reset_without_answer(conn):
    time.sleep(0.2)


@pytest.fixture
def early():
    made = []

    def _make(action):
        server = EarlyServer(action)
        made.append(server)
        return server

    yield _make
    for server in made:
        server.close()


def test_an_answer_before_the_body_is_written_is_the_targets_answer(early):
    server = early(answer_then_reset(TOO_LARGE))
    ledger = TargetCallLedger()
    status, raw, media = asyncio.run(
        transport_for(server.port, ledger).request("POST", CREATE[0], BIG)
    )
    assert (status, raw, media) == (413, _REFUSAL, "application/json")
    only(ledger.report(), completed=1, uncertain=0, uncertain_any=False, in_flight=0)


def test_an_answer_before_the_body_is_written_without_a_ledger(early):
    server = early(answer_then_reset(TOO_LARGE))
    status, _, _ = asyncio.run(transport_for(server.port, None).request("POST", CREATE[0], BIG))
    assert status == 413


def test_an_early_refusal_keeps_its_wire_code(early):
    server = early(answer_then_reset(TOO_LARGE))
    transport = transport_for(server.port, TargetCallLedger())
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(_json_call(transport, "POST", CREATE[0], BIG, parse_session))
    assert caught.value.code == "invalid_request"
    assert caught.value.retryable is False


def test_a_broken_write_with_no_answer_stays_a_transport_error(early):
    server = early(reset_without_answer)
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error" and caught.value.retryable is True
    assert isinstance(caught.value.__cause__, ConnectionError)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0)
    assert [record.reason for record in snap.records] == ["transport_error"]


def test_a_broken_write_with_a_partial_answer_stays_a_transport_error(early):
    server = early(answer_then_reset(TOO_LARGE[: len(TOO_LARGE) - 10]))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert [record.reason for record in ledger.report().records] == ["transport_error"]


CLOSE_DELIMITED = (
    b"HTTP/1.1 413 Content Too Large\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"
) + _REFUSAL


def test_a_close_delimited_answer_after_a_broken_write_is_a_transport_error(early):
    server = early(answer_then_reset(CLOSE_DELIMITED))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, ConnectionError)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0, uncertain_any=True)
    assert [record.reason for record in snap.records] == ["transport_error"]


def _chunked_413(terminator):
    head = b"HTTP/1.1 413 Content Too Large\r\nContent-Type: application/json\r\n"
    head += b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    return head + b"%x\r\n" % len(_REFUSAL) + _REFUSAL + terminator


@pytest.mark.parametrize("terminator", [b"\r\n0", b"\r\n0\r\n\r\n"], ids=["truncated", "complete"])
def test_a_chunked_answer_after_a_broken_write_is_a_transport_error(early, terminator):
    """Only `Content-Length` frames the answer after a broken write: `http.client`
    tolerates an EOF at the terminal chunk, so a chunked body cannot be told from
    a reset (maintainer rulingADR-0014)."""
    server = early(answer_then_reset(_chunked_413(terminator)))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, ConnectionError)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0, uncertain_any=True)
    assert [record.reason for record in snap.records] == ["transport_error"]


def test_an_oversize_answer_after_a_broken_write_is_a_transport_error(early):
    server = early(answer_then_reset(TOO_LARGE))
    ledger = TargetCallLedger()
    transport = transport_for(server.port, ledger, max_response_bytes=len(_REFUSAL) - 1)
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport.request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert [record.reason for record in ledger.report().records] == ["transport_error"]


@pytest.mark.parametrize(
    "raw",
    [
        b"HTTP/1.1 413 Content Too Large\r\nContent-Length: 0",
        b"HTTP/1.1 204 No Content\r\n\r\n",
        b"HTTP/1.1 413 Content Too Large\r\nContent-Length: 0\r\n\r\n",
    ],
    ids=["unterminated_header", "synthesized_length", "explicit_zero_length"],
)
def test_an_answer_without_a_non_empty_content_length_body_after_a_broken_write_is_a_transport_error(
    early, raw
):
    """After a broken write the answer counts only with an explicit `Content-Length`
    above zero and that many body bytes read: a non-empty body read in full proves
    the header block ended, while `http.client` tolerates an EOF before the header
    terminator and synthesizes a length of 0 for a 204."""
    server = early(answer_then_reset(raw))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, ConnectionError)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0, uncertain_any=True)
    assert [record.reason for record in snap.records] == ["transport_error"]


# -- A chunked reply on the normal path ---------------------------------
#
# The request is read in full (`ScriptedServer`), so the write never breaks and
# `_read_response` decides completeness by the reply's framing alone. A chunked
# body ends with the last chunk, an optional trailer section and a final CRLF
# (RFC 9112 §7.1); `http.client` accepts an EOF inside the last-chunk line and
# in place of the trailer's final CRLF, so the transport has to check it.

CHUNKED_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
)


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(b"2\r\n{}\r\n0", id="eof_in_last_chunk_line"),
        pytest.param(b"2\r\n{}\r\n0\r", id="eof_after_last_chunk_cr"),
        pytest.param(b"2\r\n{}\r\n0;x=y", id="eof_in_last_chunk_extension"),
        pytest.param(b"2\r\n{}\r\n0\r\n", id="eof_before_final_crlf"),
        pytest.param(b"2\r\n{}\r\n0\r\n\r", id="eof_inside_final_crlf"),
        pytest.param(b"2\r\n{}\r\n0\r\nX-Trailer: a\r\n", id="eof_after_trailer_field"),
        pytest.param(b"2\r\n{}\r\n0\r\nX-Trailer: a", id="eof_in_trailer_field"),
        pytest.param(b"5\r\n{}", id="eof_in_chunk_data"),
        pytest.param(b"2\r\n{}\r\n", id="eof_before_last_chunk"),
    ],
)
def test_a_chunked_reply_cut_before_its_terminator_is_uncertain(scripted, tail):
    """A chunked body that ends before `0\\r\\n[trailer]\\r\\n` is a truncated
    body: `transport_error` with the parser's `IncompleteRead` as its cause, and
    the ledger keeps the call `uncertain` (ADR-0014)."""
    server = scripted(respond(CHUNKED_HEAD + tail))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert caught.value.code == "transport_error" and caught.value.retryable is True
    assert isinstance(caught.value.__cause__, http.client.IncompleteRead)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0, uncertain_any=True, in_flight=0)
    assert [record.reason for record in snap.records] == ["incomplete_response"]


def test_a_chunked_reply_cut_before_its_terminator_fails_without_a_ledger(scripted):
    server = scripted(respond(CHUNKED_HEAD + b"2\r\n{}\r\n0"))
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, None).request("POST", *CREATE))
    assert caught.value.code == "transport_error"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(CHUNKED_HEAD + b"2\r\n{}\r\n0\r\n\r\n", id="chunked_terminated"),
        pytest.param(CHUNKED_HEAD + b"1\r\n{\r\n1\r\n}\r\n0\r\n\r\n", id="chunked_two_chunks"),
        pytest.param(CHUNKED_HEAD + b"2;x=y\r\n{}\r\n0;x=y\r\n\r\n", id="chunked_extensions"),
        pytest.param(
            CHUNKED_HEAD + b"2\r\n{}\r\n0\r\nX-Trailer: a\r\nX-Other: b\r\n\r\n",
            id="chunked_trailer",
        ),
        pytest.param(OK, id="content_length"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
            id="close_delimited",
        ),
    ],
)
def test_a_reply_that_reaches_the_end_of_its_framing_is_completed(scripted, raw):
    """A close-delimited body ends at EOF by its own
    framing (RFC 9112 §6.3), so it stays `completed`."""
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    status, received, _media = asyncio.run(
        transport_for(server.port, ledger).request("POST", *CREATE)
    )
    assert (status, received) == (200, b"{}")
    only(ledger.report(), completed=1, uncertain=0, uncertain_any=False, in_flight=0)


def test_a_content_length_short_by_one_is_incomplete(scripted):
    raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 3\r\n\r\n{}"
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    status, received, _media = asyncio.run(
        transport_for(server.port, ledger).request("POST", *CREATE)
    )
    assert (status, received) == (200, b"{}")
    snap = ledger.report()
    only(snap, uncertain=1, completed=0)
    assert [record.reason for record in snap.records] == ["incomplete_response"]


def test_an_overlong_trailer_line_stays_line_too_long(scripted):
    """The line bound keeps its own classification: a trailer line over
    `http.client`'s limit is `LineTooLong`, not a truncation."""
    tail = b"2\r\n{}\r\n0\r\nX-Trailer: " + b"a" * 70000 + b"\r\n\r\n"
    server = scripted(respond(CHUNKED_HEAD + tail))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, http.client.LineTooLong)
    assert [record.reason for record in ledger.report().records] == ["transport_error"]


# -- A reply cut before the end of its header block ---------------------
#
# A body that ends at EOF is complete only if the header section was received
# intact (RFC 9112 §8). `http.client` reads the status line and each header
# line with `readline` and takes an EOF for the empty line that ends the header
# block, so a reply with an empty or close-delimited body cut anywhere in its
# header section looked complete on the normal path.

_HEAD_CUTS = [
    pytest.param(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n", id="eof_after_header_line"
    ),
    pytest.param(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Le",
        id="eof_in_header_name",
    ),
    pytest.param(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 0\r\n",
        id="eof_after_zero_content_length",
    ),
    pytest.param(b"HTTP/1.1 204 No Content\r\n", id="eof_after_204_status_line"),
    pytest.param(b"HTTP/1.1 200", id="eof_in_status_line"),
    pytest.param(b"HTTP/1.1 200 OK\r", id="eof_after_status_line_cr"),
    pytest.param(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r", id="eof_in_blank_line"
    ),
    pytest.param(
        b"HTTP/1.1 200 OK\nContent-Type: application/json\n", id="bare_lf_eof_after_header"
    ),
    pytest.param(b"HTTP/1.1 200 OK\r\nX-Folded: a\r\n b\r\n", id="eof_after_obs_fold_line"),
    pytest.param(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\n", id="eof_after_interim_100"),
    pytest.param(b"HTTP/1.1 100 Continue\r\n\r\n", id="eof_after_interim_100_block"),
]


@pytest.mark.parametrize("raw", _HEAD_CUTS)
def test_a_reply_cut_before_its_header_terminator_is_uncertain(scripted, raw):
    """A reply cut before the empty line that ends its header block is truncated:
    `transport_error` with `IncompleteRead` as its cause, and the ledger keeps the
    call `uncertain/incomplete_response` (ADR-0014), as for a chunked body
    cut before its terminator."""
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert caught.value.code == "transport_error" and caught.value.retryable is True
    assert isinstance(caught.value.__cause__, http.client.IncompleteRead)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0, uncertain_any=True, in_flight=0)
    assert [record.reason for record in snap.records] == ["incomplete_response"]


def test_a_reply_cut_before_its_header_terminator_is_not_blamed_on_the_target(scripted):
    """The adapter sees the transport's retryable `transport_error`, never the
    `invalid_response` an empty body would otherwise earn the target."""
    server = scripted(respond(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"))
    transport = transport_for(server.port, TargetCallLedger())
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(_json_call(transport, "POST", *CREATE, parse_session))
    assert caught.value.code == "transport_error" and caught.value.retryable is True


def test_a_reply_cut_before_its_header_terminator_fails_without_a_ledger(scripted):
    server = scripted(respond(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Le"))
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, None).request("POST", *CREATE))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, http.client.IncompleteRead)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(OK, (200, b"{}"), id="crlf_content_length"),
        pytest.param(
            b"HTTP/1.1 200 OK\nContent-Type: application/json\nContent-Length: 2\n\n{}",
            (200, b"{}"),
            id="bare_lf_content_length",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Folded: a\r\n b\r\n"
            b"Content-Length: 2\r\n\r\n{}",
            (200, b"{}"),
            id="obs_fold_header",
        ),
        pytest.param(
            b"HTTP/1.1 200\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
            (200, b"{}"),
            id="status_line_without_reason",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 0\r\n\r\n",
            (200, b""),
            id="zero_content_length",
        ),
        pytest.param(CHUNKED_HEAD + b"2\r\n{}\r\n0\r\n\r\n", (200, b"{}"), id="chunked"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
            (200, b"{}"),
            id="close_delimited",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n",
            (200, b""),
            id="close_delimited_empty",
        ),
        pytest.param(b"HTTP/1.1 204 No Content\r\n\r\n", (204, b""), id="no_content"),
        pytest.param(b"HTTP/1.1 204 No Content\n\n", (204, b""), id="no_content_bare_lf"),
        pytest.param(
            b"HTTP/1.1 100 Continue\r\n\r\n" + OK, (200, b"{}"), id="interim_100_then_reply"
        ),
    ],
)
def test_a_reply_with_an_intact_header_block_is_completed(scripted, raw, expected):
    """Valid line endings, folding, framing and empty
    bodies are read as before once the header block has ended."""
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    status, received, _media = asyncio.run(
        transport_for(server.port, ledger).request("POST", *CREATE)
    )
    assert (status, received) == expected
    only(ledger.report(), completed=1, uncertain=0, uncertain_any=False, in_flight=0)


def test_a_not_modified_reply_with_an_intact_header_block_is_completed(scripted):
    """A 304 is refused as a redirect after the ledger has counted the response."""
    server = scripted(respond(b"HTTP/1.1 304 Not Modified\r\n\r\n"))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert caught.value.code == "invalid_response"
    only(ledger.report(), completed=1, uncertain=0)


def test_a_close_with_no_reply_stays_remote_disconnected(scripted):
    """No byte at all is `http.client`'s own `RemoteDisconnected`, not a cut reply."""
    server = scripted(respond(b""))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, http.client.RemoteDisconnected)
    assert [record.reason for record in ledger.report().records] == ["transport_error"]


@pytest.mark.parametrize(
    ("raw", "cause"),
    [
        pytest.param(
            b"HTTP/1.1 200 OK\r\nX-Long: " + b"a" * 70000 + b"\r\n\r\n",
            http.client.LineTooLong,
            id="overlong_header_line",
        ),
        pytest.param(
            b"HTTP/1.1 200 " + b"a" * 70000 + b"\r\n\r\n",
            http.client.LineTooLong,
            id="overlong_status_line",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\n" + b"X-A: b\r\n" * 101 + b"\r\n",
            http.client.HTTPException,
            id="too_many_headers",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nX-Long: " + b"a" * 70000,
            http.client.LineTooLong,
            id="overlong_header_line_cut",
        ),
    ],
)
def test_header_limits_keep_their_own_classification(scripted, raw, cause):
    """The line and header-count bounds still raise `http.client`'s own errors,
    and the ledger records them as `transport_error`, not a truncation."""
    server = scripted(respond(raw))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", *CREATE))
    assert caught.value.code == "transport_error"
    assert type(caught.value.__cause__) is cause
    assert [record.reason for record in ledger.report().records] == ["transport_error"]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(
            b"HTTP/1.1 413 Content Too Large\r\nContent-Length: %d\r\n" % len(_REFUSAL),
            id="eof_before_header_terminator",
        ),
        pytest.param(b"HTTP/1.1 413 Content Too Large\r\nContent-Le", id="eof_in_header_name"),
        pytest.param(b"HTTP/1.1 413", id="eof_in_status_line"),
    ],
)
def test_a_header_cut_after_a_broken_write_stays_the_writes_transport_error(early, raw):
    """The broken-write path keeps its contract: a header section cut short
    is the write's own `transport_error`, with the write error as its cause, so
    the ledger records `transport_error`, not `incomplete_response`."""
    server = early(answer_then_reset(raw))
    ledger = TargetCallLedger()
    with pytest.raises(RolloutInfrastructureError) as caught:
        asyncio.run(transport_for(server.port, ledger).request("POST", CREATE[0], BIG))
    assert caught.value.code == "transport_error"
    assert isinstance(caught.value.__cause__, ConnectionError)
    snap = ledger.report()
    only(snap, uncertain=1, completed=0, uncertain_any=True)
    assert [record.reason for record in snap.records] == ["transport_error"]
