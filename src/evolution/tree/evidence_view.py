"""Pure compact rendering of full episodes, with counted middle cuts."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, Literal, TypeVar

from evolution.tree.execution.evidence_settings import EvidenceSettings
from evolution.tree.execution.input_secrets import redact_run_secrets
from evolution.tree.execution.journal_identity import canonical_json

T = TypeVar("T")


def text_of(value: object) -> str:
    return value if isinstance(value, str) else canonical_json(value).decode()


def cut_text(text: str, limit: int) -> tuple[str, int]:
    removed = max(0, len(text) - limit)
    if not removed:
        return text, 0
    head = limit // 2
    tail = limit - head
    return text[:head] + f"\n…[{removed} characters cut]…\n" + (
        text[-tail:] if tail else ""
    ), removed


def cut_items(
    items: Sequence[T],
    limit: int,
    *,
    keep: Collection[int] = (),
    unit: Literal["events", "turns"],
) -> list[T | dict[str, Any]]:
    if len(items) <= limit:
        return list(items)
    head = limit // 2
    tail = limit - head
    indices = set(range(head)) | set(range(len(items) - tail, len(items))) | set(keep)
    result: list[T | dict[str, Any]] = []
    removed = 0
    for index, item in enumerate(items):
        if index not in indices:
            removed += 1
            continue
        if removed:
            result.append({"cut": {unit: removed}})
            removed = 0
        result.append(item)
    if removed:
        result.append({"cut": {unit: removed}})
    return result


def _text(value: object, limit: int, redact: Callable[[Any], Any]) -> str:
    return cut_text(redact(text_of(value)), limit)[0]


def render_event(
    event: Mapping[str, Any],
    settings: EvidenceSettings,
    secrets: Sequence[str],
    *,
    redact: Callable[[Any], Any] | None = None,
) -> dict[str, str]:
    transform = redact if redact is not None else lambda value: redact_run_secrets(value, secrets)
    return {
        transform(key): _text(value, settings.text_chars, transform) for key, value in event.items()
    }


def render_turn(
    turn: Mapping[str, Any],
    settings: EvidenceSettings,
    secrets: Sequence[str],
    *,
    redact: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    transform = redact if redact is not None else lambda value: redact_run_secrets(value, secrets)
    events = turn.get("events", ())
    assistant_indices = [
        i for i, event in enumerate(events) if event.get("type") == "assistant_message"
    ]
    keep = (assistant_indices[0], assistant_indices[-1]) if assistant_indices else ()
    shown = [render_event(event, settings, secrets, redact=transform) for event in events]
    message = turn.get("message", {})
    reason = turn.get("reason")
    return {
        "index": turn["index"],
        "outcome": transform(turn.get("outcome")),
        "reason": render_event(reason, settings, secrets, redact=transform)
        if reason is not None
        else None,
        "input": {
            "content": _text(message.get("content", ""), settings.input_chars, transform),
            "fields": _text(message["fields"], settings.input_chars, transform)
            if message.get("fields")
            else None,
        },
        "events": cut_items(shown, settings.events_per_turn, keep=keep, unit="events"),
    }


def render_turns(
    turns: Sequence[Mapping[str, Any]],
    settings: EvidenceSettings,
    secrets: Sequence[str],
    *,
    redact: Callable[[Any], Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    shown = [render_turn(turn, settings, secrets, redact=redact) for turn in turns]
    return cut_items(shown, settings.turns, unit="turns"), max(0, len(turns) - settings.turns)


def is_marker(item: object) -> bool:
    return isinstance(item, dict) and set(item) == {"cut"}
