"""An unstable reference target: turns answer a retryable 500 while `failing` is set.

The seam wraps `dispatch` of the in-process target (the test double on the other
side of the socket), as core's own `tests/reference_target_faults.py` does; no
core transport code is replaced.
"""
from __future__ import annotations

import threading
from pathlib import Path

from evolution.tree.execution.wire_v2 import canonical_body

from ledger_faults import is_turn


class FailingTurns:
    def __init__(self, server, *, mode=None):
        self.failing = threading.Event()
        self.failing.set()
        real = server.dispatch
        turn_lock, turns = threading.Lock(), 0

        def dispatch(method, path, raw_body=b""):
            nonlocal turns
            if is_turn(method, path):
                with turn_lock:
                    turns += 1
                    turn = turns
                fail = self.failing.is_set()
                if mode is not None:
                    _, successful_turns, flag = mode.split(":", 2)
                    fail = fail and turn > int(successful_turns) and Path(flag).exists()
                if fail:
                    return 500, canonical_body(
                        {"error": {"code": "internal", "message": "internal", "retryable": True}})
            return real(method, path, raw_body)

        server.dispatch = dispatch
