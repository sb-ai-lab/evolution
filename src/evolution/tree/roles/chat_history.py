"""Assistant continuations and complete native tool exchanges (ADR-0025)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def strict_json(raw: str) -> Any:
    """`json.loads` that refuses `NaN`, `Infinity` and `-Infinity` (ValueError).

    A literal such as `1e400` overflows to `inf` inside `json.loads` without a
    constant callback, so it is accepted as a float, as `json.loads` decides it.
    """
    try:
        return json.loads(raw, parse_constant=_refuse_constant)
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def is_object_arguments(raw: object) -> bool:
    """True when `raw` is a string holding exactly one strict JSON object."""
    if not isinstance(raw, str):
        return False
    try:
        return isinstance(strict_json(raw), dict)
    except ValueError:
        return False


@dataclass(frozen=True)
class ReplacedCall:
    """A retained tool call whose arguments were replaced by `{}`."""

    id: str
    name: str
    characters: int
    sha256: str

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "characters": self.characters,
            "sha256": self.sha256,
        }


def _raw_text(raw: Any) -> str:
    """The measured text: the string itself, else its JSON encoding (`null` if absent)."""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw)
    except (TypeError, ValueError):
        return str(raw)


def retained_message(
    assistant: dict[str, Any],
) -> tuple[dict[str, Any], tuple[ReplacedCall, ...]]:
    """Retain `assistant` with every non-object tool call's arguments set to `{}`.

    Only `function.arguments` of broken calls changes; ids, names, order and all
    other fields (including opaque continuations) are kept. The input is not
    mutated. Without broken calls the input object itself is returned.
    """
    calls = assistant.get("tool_calls")
    if not isinstance(calls, list):
        return assistant, ()
    retained: list[Any] = []
    replaced: list[ReplacedCall] = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        raw = function.get("arguments") if isinstance(function, dict) else None
        if is_object_arguments(raw):
            retained.append(call)
            continue
        # Defensive only: `assistant_message`/`_tool_call` refuse non-string
        # arguments, so they never reach this branch on the modern path.
        text = _raw_text(raw)
        name = function.get("name") if isinstance(function, dict) else None
        call_id = call.get("id") if isinstance(call, dict) else None
        replaced.append(
            ReplacedCall(
                id=call_id if isinstance(call_id, str) else "",
                name=name if isinstance(name, str) else "",
                characters=len(text),
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
        )
        fixed = dict(call) if isinstance(call, dict) else {}
        fixed["function"] = {**(function if isinstance(function, dict) else {}), "arguments": "{}"}
        retained.append(fixed)
    if not replaced:
        return assistant, ()
    return {**assistant, "tool_calls": retained}, tuple(replaced)
