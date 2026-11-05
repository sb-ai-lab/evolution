"""The fault-injecting reference target used by the conformance, adapter and
target suites.

This is an ordinary test *module* (imported as `tests.reference_target_faults`),
not a fixture file. The product `ReferenceTarget` cannot be made to violate
the wire (R3-ref); this subclass is where the `Misbehave` hooks live so the
suites can prove the *adapter* rejects a non-conforming target.

I1 adds one hook of its own, `fail_turns`: an unstable *backend* rather than a
non-conforming one, which answers `internal` (500, retryable) to the turn
requests a selector picks by ordinal. It is a `dispatch` override here and not
a `Misbehave` field because nothing in `src` may gain a way to emit faults
(PDR-0004), and because the fault must precede the turn's record so a resent
`request_id` executes the turn rather than replaying a stored 500.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from evolution.tree.execution.wire_v2 import canonical_body
from evolution.tree.target.reference_target import Misbehave, ReferenceTarget

#: The deterministic 27 % pattern of I1's live-shaped test: 3 of every 11 turn
#: requests fail, and no two consecutive ordinals do, so one resend always
#: clears a fault when the turns are serialized (`campaign.max_concurrency: 1`).
TURN_FAULT_CYCLE = 11
TURN_FAULT_OFFSETS = (3, 6, 10)


class FaultInjectingReferenceTarget(ReferenceTarget):
    """A reference target whose hooks a test can flip on a live server."""

    def __init__(self, **kwargs: Any) -> None:
        self._faults = Misbehave()
        self._turn_fault: Callable[[int], bool] | None = None
        self._turn_requests = 0
        self._turn_lock = threading.Lock()
        super().__init__(**kwargs)

    @property
    def misbehave(self) -> Misbehave:
        return self._faults

    def fault(self, **changes: Any) -> Misbehave:
        """Replace the current hooks with `changes` applied; returns the new value."""
        self._faults = replace(self._faults, **changes)
        return self._faults

    def fail_turns(self, selector: Callable[[int], bool] | None) -> None:
        """Answer `internal` to the turn requests `selector` picks; `None` serves them all."""
        self._turn_fault = selector

    @property
    def turn_requests(self) -> int:
        """How many turn requests have been dispatched, faulted ones included."""
        return self._turn_requests

    def reset(self) -> None:
        self._faults = Misbehave()
        self._turn_fault = None

    def dispatch(self, method: str, path: str, raw_body: bytes = b"") -> tuple[int, bytes]:
        if _is_turn(method, path):
            with self._turn_lock:
                ordinal = self._turn_requests
                self._turn_requests += 1
            if self._turn_fault is not None and self._turn_fault(ordinal):
                return 500, canonical_body(
                    {"error": {"code": "internal", "message": "internal", "retryable": True}}
                )
        return super().dispatch(method, path, raw_body)


def _is_turn(method: str, path: str) -> bool:
    return method == "POST" and path.split("?", 1)[0].rstrip("/").endswith("/turns")
