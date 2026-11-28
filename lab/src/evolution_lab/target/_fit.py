"""Fit a reply into core's `max_output_bytes`, measured the way the server measures.

The server builds the body from the backend's events, core-measured
`latency_ms`, the outcome, `truncated` and -- only when the backend set one --
the turn `reason`, encodes it with core's `canonical_body` and answers
`500 internal` when it is larger than the limit (`target_server.py`, turn
path). The layer measures the same body with the same `canonical_body`, the
widest latency and outcome, the turn's own reason and `truncated=False`. This
is conservative: JSON-encoded `false` (5 bytes) is wider than `true` (4
bytes); an ordinary reply is sent with `truncated=False`, so measuring with
False ensures a reply that fits will not exceed the server's limit.

The reason belongs to the mandatory minimum (spec W19 §3.2): it is never cut,
so when only the minimum fits the reason is still sent.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from evolution.tree import Usage
from evolution.tree.execution.wire_v2 import canonical_body

from evolution_lab.target._contract import OUTCOMES

VISIBLE = frozenset({"assistant_message", "tool_call", "tool_result", "skill_read", "output_truncated"})
_WIDEST_LATENCY = 99999999999999999999
_WIDEST_OUTCOME = max(OUTCOMES, key=len)


class OutputTooLarge(Exception):
    """Even the mandatory minimum with empty content exceeds the limit."""


def _plain(value: Any) -> Any:
    """Fold nested mappings and tuples into `dict`/`list`, as the server's
    `_json_safe` does before `canonical_body` (which only encodes those)."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _size(events: Sequence[Mapping[str, Any]], usage: Usage | None,
          reason: Mapping[str, str] | None = None,
          provenance: Mapping[str, Any] | None = None) -> int:
    body: dict[str, Any] = {"events": list(events), "usage": usage.to_payload() if usage is not None else None,
                            "latency_ms": _WIDEST_LATENCY, "outcome": _WIDEST_OUTCOME, "truncated": False}
    if reason is not None:  # omit-not-null, as the server writes it
        body["reason"] = dict(reason)
    if provenance is not None:
        body["provenance"] = provenance
    return len(canonical_body(_plain(body)))


def fit(events: Sequence[Mapping[str, Any]], text: str, usage: Usage | None, limit: int, *,
        reason: Mapping[str, str] | None = None,
        provenance: Mapping[str, Any] | None = None) -> tuple[tuple[dict[str, Any], ...], bool, bool]:
    agent = [dict(event) for event in events]
    final = {"type": "assistant_message", "content": text}

    def size() -> int:
        return _size([*agent, final], usage, reason, provenance)

    # Provenance must never displace answer text. Only after deciding whether
    # it fits with the complete final answer do the legacy truncation rules run.
    minimum = [{"type": "assistant_message", "content": ""}]
    if provenance is not None and _size([final], usage, reason, provenance) > limit:
        provenance = None
    if _size(minimum, usage, reason, provenance) > limit:
        raise OutputTooLarge("mandatory reply minimum exceeds max_output_bytes")
    cut = False
    for visible in (False, True):
        index = len(agent) - 1
        while index >= 0 and size() > limit:
            if (agent[index]["type"] in VISIBLE) == visible:
                del agent[index]
                cut = True
            index -= 1
    if size() > limit:
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            final["content"] = text[:middle]
            if size() <= limit:
                low = middle
            else:
                high = middle - 1
        final["content"] = text[:low]
        cut = True
    return (*agent, final), cut, provenance is not None
