"""Narrow seams for worker ledger failure-path tests (spec S6 §4).

The workers under test keep their real core `TargetCallLedger`, `HttpSessionAdapter`
and `HttpTransport`. The seams below act only on the in-process reference target
(the test double on the other side of the socket) or observe the worker's own
ledger; none replaces core transport code.

- `LedgerProbe` rebinds the worker module's `TargetCallLedger` name to a subclass
  that remembers every ledger the worker creates and the snapshot its `settle`
  returned, so a test can see the per-call records the published `/2` omits.
- `HeldCall` wraps `TargetServer.dispatch` so the first matching request waits
  until the test releases it: the client's worker thread is inside socket I/O.
- `drop_first_reply` patches the reference target's request handler so the first
  matching request is read and its connection closed without any reply.
"""
from __future__ import annotations

import threading
import time

from evolution.tree import TargetCallLedger


def is_turn(method: str, path: str) -> bool:
    return method == "POST" and path.endswith("/turns")


class LedgerProbe:
    def __init__(self, monkeypatch, worker_module):
        probe = self
        self.ledgers: list[TargetCallLedger] = []
        self.settled = []

        class RecordingLedger(TargetCallLedger):
            def __init__(self):
                super().__init__()
                probe.ledgers.append(self)

            async def settle(self, timeout=None):
                snapshot = await super().settle(timeout)
                probe.settled.append(snapshot)
                return snapshot

        monkeypatch.setattr(worker_module, "TargetCallLedger", RecordingLedger)

    def wait_for(self, predicate, *, seconds: float = 10) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(predicate(ledger.report()) for ledger in list(self.ledgers)):
                return True
            time.sleep(.01)
        return False


def abandoned_pending_turn(snapshot) -> bool:
    return any(
        record.operation == "turn" and record.abandoned and record.state == "pending"
        for record in snapshot.records
    )


class HeldCall:
    """Hold the first request matching `predicate` until `release` is set."""

    def __init__(self, server, predicate, *, seconds: float = 30):
        self.entered = threading.Event()
        self.release = threading.Event()
        real = server.dispatch
        lock = threading.Lock()
        taken = []

        def dispatch(method, path, raw_body=b""):
            if predicate(method, path):
                with lock:
                    first = not taken
                    taken.append(path)
                if first:
                    self.entered.set()
                    self.release.wait(seconds)
            return real(method, path, raw_body)

        server.dispatch = dispatch


def drop_first_reply(monkeypatch, predicate) -> list[str]:
    """Close the connection of the first matching request without replying."""
    from evolution.tree.target import reference_target_http

    handler = reference_target_http._Handler
    real = handler._handle
    lock = threading.Lock()
    dropped: list[str] = []

    def handle(self, method):
        if predicate(method, self.path):
            with lock:
                first = not dropped
                if first:
                    dropped.append(self.path)
            if first:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.close_connection = True
                return
        real(self, method)

    monkeypatch.setattr(handler, "_handle", handle)
    return dropped
