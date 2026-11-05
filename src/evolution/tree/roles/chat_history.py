"""Assistant continuations and complete native tool exchanges (ADR-0025)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

CONTINUATION_FIELDS = ("reasoning", "reasoning_content", "reasoning_details")
_ASSISTANT_FIELDS = ("role", "content", "refusal", *CONTINUATION_FIELDS)


class ChatHistoryError(ValueError):
    """A provider message or retained tool exchange cannot be continued truthfully."""


def with_dropped_note(output: Mapping[str, Any], dropped: Sequence[str]) -> dict[str, Any]:
    """Tell the first call which other calls were skipped, preserving their order."""
    result = dict(output)
    if dropped:
        result["parallel_calls_not_run"] = {
            "tools": list(dropped),
            "note": "Only the first tool call of a response runs. These calls did not run; "
            "send each one again in its own response.",
        }
    return result


def _fields(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_unset=True)
    return vars(value)


def _tool_call(value: Any) -> dict[str, Any]:
    call = _fields(value)
    function = _fields(call.get("function"))
    if call.get("type") != "function":
        raise ChatHistoryError("unsupported tool call type")
    if not all(isinstance(function.get(key), str) for key in ("name", "arguments")):
        raise ChatHistoryError("tool function name and arguments must be strings")
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {key: function[key] for key in ("name", "arguments")},
    }


def _call_ids(calls: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for call in calls:
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ChatHistoryError("tool call IDs must be non-empty strings")
        ids.append(call_id)
    if len(set(ids)) != len(ids):
        raise ChatHistoryError("tool call IDs must be unique within a response")
    return ids


def assistant_message(message: Any) -> dict[str, Any]:
    """Detach the request-compatible fields without rewriting opaque continuations."""
    try:
        fields = _fields(message)
        assistant = {key: fields[key] for key in _ASSISTANT_FIELDS if key in fields}
        assistant.setdefault("role", "assistant")
        if assistant["role"] != "assistant":
            raise ChatHistoryError("provider message must have the assistant role")
        if "tool_calls" in fields:
            calls = fields["tool_calls"]
            if calls is not None:
                if not isinstance(calls, list):
                    raise ChatHistoryError("tool_calls must be a list")
                calls = [_tool_call(call) for call in calls]
                _call_ids(calls)
            if calls:
                assistant["tool_calls"] = calls
        return json.loads(json.dumps(assistant, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        if isinstance(exc, ChatHistoryError):
            raise
        raise ChatHistoryError("assistant message must have a JSON-compatible envelope") from exc


def tool_exchange(assistant: dict[str, Any], output: dict[str, Any]) -> list[dict[str, Any]]:
    """Close every returned ID while reporting only the first call's real result."""
    ids = _call_ids(assistant.get("tool_calls") or [])
    if not ids:
        raise ChatHistoryError("a tool exchange requires at least one call")
    skipped = {"error": "not_executed", "reason": "one_tool_per_turn"}
    return [assistant] + [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                output if index == 0 else skipped, ensure_ascii=False, separators=(",", ":")
            ),
        }
        for index, call_id in enumerate(ids)
    ]


def message_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group complete, ordered ID-correlated exchanges without discarding orphans."""
    units: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "tool":
            raise ChatHistoryError("orphan tool result in conversation")
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        ids = _call_ids(calls) if calls else []
        results = messages[index + 1 : index + 1 + len(ids)]
        if [
            result.get("tool_call_id") for result in results if result.get("role") == "tool"
        ] != ids:
            raise ChatHistoryError("tool results must match every assistant call ID in order")
        units.append([message, *results])
        index += 1 + len(ids)
    return units
