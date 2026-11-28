"""The ``lab-probe-turn/1`` record, the ``agent_outcome`` mapping and the ceiling check (spec 2026-10-02 §4.1-§4.4)."""
from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..target import AGENT_OUTCOME_CATEGORY, AGENT_OUTCOMES

if TYPE_CHECKING:
    from evolution.tree import TurnResult

PROBE_TURN_SCHEMA = "lab-probe-turn/1"
CEILING_REACHED = "probe_ceiling_reached"
CEILING_UNVERIFIABLE = "probe_ceiling_unverifiable"


def agent_outcome(outcome: str, reason: Mapping[str, Any] | None, failure: str | None) -> str:
    """Spec §4.2: the closed ``agent_outcome`` vocabulary for one turn."""
    if failure is not None or outcome == "target_failed":
        return "infrastructure"
    if outcome == "candidate_failed":
        return "error"
    if outcome == "ok" and reason and reason.get("category") == AGENT_OUTCOME_CATEGORY and reason.get("code") in AGENT_OUTCOMES:
        return str(reason["code"])
    return "unspecified"


def plain_json(value: Any) -> Any:
    """Thaw nested boundary containers without coercing JSON object keys."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return value


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, Mapping):
        return plain_json(value)
    return plain_json({"category": value.category, "code": value.code})


def _usage(usage: Any) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return plain_json({key: getattr(usage, key) for key in ("prompt", "completion", "cached", "total")})


def _step(index: int, event: Any) -> dict[str, Any]:
    body = dict(event) if isinstance(event, Mapping) else dict(vars(event))
    return {"index": index, "type": body.pop("type"), "data": plain_json(body)}


def turn_record(*, session_id: str, n: int, started_at: str, finished_at: str, content: str,
                result: TurnResult | None, failure: str | None, cost: dict | None) -> dict[str, Any]:
    """The ``lab-probe-turn/1`` body without ``ceiling`` (the session layer adds it)."""
    try:
        record = _turn_record(session_id=session_id, n=n, started_at=started_at, finished_at=finished_at,
                              content=content, result=result, failure=failure, cost=cost)
        # json.dumps accepts numeric keys by coercion, so check keys explicitly as well.
        record = plain_json(record)
        json.dumps(record, allow_nan=False)
        return record
    except (TypeError, ValueError):
        return _turn_record(session_id=session_id, n=n, started_at=started_at, finished_at=finished_at,
                            content=content, result=None, failure="protocol_error", cost=cost)


def _turn_record(*, session_id: str, n: int, started_at: str, finished_at: str, content: str,
                 result: TurnResult | None, failure: str | None, cost: dict | None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    answer: str | None = None
    if result is not None:
        steps = [_step(i, event) for i, event in enumerate(result.events)]
        for step in steps:
            if step["type"] == "assistant_message":
                answer = step["data"].get("content")
    outcome = result.outcome if result is not None else "target_failed"
    reason = _plain(result.reason) if result is not None else None
    return {
        "schema": PROBE_TURN_SCHEMA,
        "session_id": session_id,
        "turn_id": f"{session_id}/{n}",
        "started_at": started_at,
        "finished_at": finished_at,
        "latency_ms": result.latency_ms if result is not None else None,
        "request": {"content": content},
        "answer": answer,
        "steps": steps,
        "outcome": outcome,
        "reason": reason,
        "agent_outcome": agent_outcome(outcome, reason, failure),
        "usage": _usage(result.usage) if result is not None else None,
        "cost": cost,
        "truncated": any(step["type"] == "output_truncated" for step in steps),
        "failure": {"code": failure} if failure is not None else None,
    }


def check_ceiling(ceiling: dict, used: dict) -> str | None:
    """Before a turn: ``None`` to proceed, else the refusal code. Order: turns, tokens, cost (spec §4.4)."""
    if used["turns"] >= ceiling["max_turns"]:
        return CEILING_REACHED
    if ceiling.get("max_tokens") is not None:
        if used["tokens_incomplete"]:
            return CEILING_UNVERIFIABLE
        if used["tokens"] >= ceiling["max_tokens"]:
            return CEILING_REACHED
    if ceiling.get("max_cost") is not None:
        if used["cost_unavailable"]:
            return CEILING_UNVERIFIABLE
        if Decimal(used["cost_units"]) / 10**9 >= Decimal(ceiling["max_cost"]):
            return CEILING_REACHED
    return None
