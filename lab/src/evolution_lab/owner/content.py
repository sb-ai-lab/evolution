"""Pure disclosure, masking and size policies for Lab's content forms.

No files or core imports: ``core_raw`` reads and ``readers`` assembles (ADR-0019).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

FIELD_MAX = 16384  # Characters kept across head and tail, excluding the marker.
LIST_MAX = 200
RESPONSE_MAX = 1024 * 1024
TIERS = ((FIELD_MAX, LIST_MAX), (4096, 100), (1024, 40))
WITHHELD = {"withheld": "private"}
REDACTED = "[REDACTED]"

# Oracle and private_context are governed by disclosure, not credential masking.
_CREDENTIAL_KEY = re.compile(r"authorization|token(?!s)|api.?key|credential|password|secret|reasoning|thought", re.I)
_HIDDEN_TAG = re.compile(r"<(?:(think|analysis|reasoning)\b[^>]*>|/(think|analysis|reasoning)\s*>)", re.I)
_TOO_LARGE = {"state": "unavailable", "reason": "too_large"}
_SECTION_PATHS = {
    "lab-sample/1": (("input",), ("turns",), ("terminal",), ("scoring",), ("private",)),
    "lab-attempt/1": (
        ("confidence",), ("editor", "events"), ("editor", "changed"),
        ("editor", "failure"), ("editor", "stderr_tail"),
    ),
    "lab-reflection/1": (("events",), ("stderr_tail",)),
}


def mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and _CREDENTIAL_KEY.search(key) else mask(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask(item) for item in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _mask_text(text: str) -> str:
    if text.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            masked = mask(parsed)
            return text if masked == parsed else json.dumps(masked, ensure_ascii=False, separators=(",", ":"))
    parts = []
    stack = []
    cursor = 0
    for tag in _HIDDEN_TAG.finditer(text):
        opening, closing = tag.groups()
        if opening:
            if not stack:
                parts.extend((text[cursor:tag.start()], REDACTED))
            stack.append(opening.lower())
        elif stack and closing.lower() == stack[-1]:
            stack.pop()
            if not stack:
                cursor = tag.end()
    # An unclosed hidden block hides everything through the end of the text.
    if not stack:
        parts.append(text[cursor:])
    return "".join(parts)


def clip_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    tail = text[-half:] if half else ""
    return f"{text[:half]}\n… [{len(text) - 2 * half} characters omitted] …\n{tail}", True


def clip_value(value: Any, limit: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        return clip_text(value, limit)
    if isinstance(value, list):
        pairs = [clip_value(item, limit) for item in value]
        return [item for item, _ in pairs], any(cut for _, cut in pairs)
    if isinstance(value, dict):
        pairs = {key: clip_value(item, limit) for key, item in value.items()}
        return {key: item for key, (item, _) in pairs.items()}, any(cut for _, cut in pairs.values())
    return value, False


def clip_fields(value: Any, limit: int, *, tail_paths=(), preserve_paths=()) -> tuple[Any, list[dict]]:
    """Clip a disclosed, masked form and record pointers to emitted text only."""
    truncation = []

    def walk(item, path):
        if path in preserve_paths:
            return item
        if isinstance(item, str):
            if path in tail_paths and len(item) > limit:
                clipped, cut = item[-limit:], True
            else:
                clipped, cut = clip_text(item, limit)
            if cut:
                truncation.append({"path": path, "size": len(item)})
            return clipped
        if isinstance(item, list):
            return [walk(child, f"{path}/{index}") for index, child in enumerate(item)]
        if isinstance(item, dict):
            return {key: walk(child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
                    for key, child in item.items()}
        return item

    return walk(value, ""), truncation


def clip_list(items: list, limit: int) -> tuple[list, int]:
    if len(items) <= limit:
        return list(items), 0
    half = limit // 2
    tail = items[-half:] if half else []
    return list(items[:half]) + list(tail), len(items) - 2 * half


def private(value: Any, disclose: bool) -> Any:
    return value if disclose else dict(WITHHELD)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def fit(build: Callable[[int, int], dict]) -> dict:
    """Build each tier, then mark the largest permitted sections unavailable.

    Fixed metadata and public withheld sentinels survive the hard size cap. An
    unsupported builder whose envelope alone exceeds it is a programming error.
    """
    for field_max, list_max in TIERS:
        body = build(field_max, list_max)
        if _json_size(body) <= RESPONSE_MAX:
            return body

    # Only these dictionaries can be edited; leave the builder's values intact.
    body = dict(body)
    if body.get("schema") == "lab-attempt/1" and isinstance(body.get("editor"), dict):
        body["editor"] = dict(body["editor"])
    body["truncated"] = True
    sentinel_size = _json_size(_TOO_LARGE)
    while _json_size(body) > RESPONSE_MAX:
        candidates = []
        for path in _SECTION_PATHS.get(body.get("schema"), ()):
            parent = body
            for key in path[:-1]:
                parent = parent.get(key)
                if not isinstance(parent, dict):
                    break
            if not isinstance(parent, dict) or path[-1] not in parent:
                continue
            value = parent[path[-1]]
            if value == WITHHELD:
                continue
            size = _json_size(value)
            if size > sentinel_size:
                pointer = "/" + "/".join(path)
                metadata_size = sum(_json_size(entry) for entry in body.get("truncation", ())
                                    if entry["path"] == pointer or entry["path"].startswith(pointer + "/"))
                candidates.append((size + metadata_size, parent, path[-1], pointer))
        if not candidates:
            raise ValueError("Content envelope exceeds RESPONSE_MAX after section fallback")
        _, parent, key, pointer = max(candidates, key=lambda candidate: candidate[0])
        parent[key] = dict(_TOO_LARGE)
        if "truncation" in body:
            body["truncation"] = [entry for entry in body["truncation"]
                                  if entry["path"] != pointer and not entry["path"].startswith(pointer + "/")]
    return body


# Core records Lab knows and does not give (§2.6): usage, provider attempts, repairs, service records.
_SERVICE_PHASES = frozenset({
    "provider_attempt", "provider_refused", "protocol_repair_requested", "parallel_calls_dropped", "model_request_failed", "preflight",
    "preflight_failure", "identity_mismatch", "self_check_repair_requested", "continuation", "evidence_usage",
    "exception", "self_check", "usage",
})
_PRIVATE = "private"
_PAGE_TOOLS = frozenset({"read_trace", "read_evidence", "read_document", "read_history"})
_PAGE_MARKERS = frozenset({"offset", "next_offset", "total_characters", "total_chars"})
_PAGE_IDS = ("evidence_id", "document_id", "id")
# The reflector's visibility per event (§2.6, codex r2): None — the whole event is private; a dict — per field,
# fields not listed are public. The editor's events are public throughout.
_REFLECTOR = {
    "input": None, "model_message": None,
    "tool_call": {"arguments": _PRIVATE},
    "tool_result": {"text": _PRIVATE},
    "rejected": {"reason": _PRIVATE, "text": _PRIVATE},
    "failed": {"text": _PRIVATE},
    "agent_summary": {"file_changes": _PRIVATE, "command_failures": _PRIVATE, "last_message": _PRIVATE,
                      "stderr_tail": _PRIVATE},
}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # OpenAI content parts
        return "".join(part.get("text", "") for part in value if isinstance(part, dict))
    return json.dumps(value, ensure_ascii=False)


def _page_metadata(result: Any) -> dict:
    if not isinstance(result, dict):
        return {}
    metadata = {key: result[key] for key in _PAGE_IDS if isinstance(result.get(key), str)}
    for key in ("offset", "total_characters", "total_chars", "next_offset"):
        value = result.get(key)
        if (type(value) is int and value >= 0) or (key == "next_offset" and key in result and value is None):
            metadata[key] = value
    digest = result.get("content_digest")
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        metadata["content_digest"] = digest
    return metadata


def _opening(conversation: list) -> str | None:
    for message in conversation:
        if isinstance(message, dict) and message.get("role") == "user":
            text = _text(message.get("content"))
            if not text.startswith("Call list_files now"):
                return text
    return None


def project_role(kind: str, records: list[dict]) -> dict:
    """Project the pinned role records, keeping replayed sessions and no usage."""
    events, skipped = [], 0
    conversation: list = []
    opened = False
    session_start = 0
    calls: dict[Any, str | None] = {}
    for record in records:
        phase = record.get("phase")
        if phase == "model_request":
            # Delta-encoded (chat_editor.py:2358): the conversation cut at messages_after, plus messages. A request
            # that offers only list_files is core's preflight: its user message is not the opening.
            after = record.get("messages_after") or 0
            if after == 0:
                # A replay appends a new session: neither opening nor call ids carry over.
                if opened:
                    session_start = len(events)
                conversation, opened = [], False
                calls.clear()
            conversation = conversation[:after] + list(record.get("messages") or [])
            if not opened and record.get("tool_names") != ["list_files"]:
                text = _opening(conversation)
                if text is not None:
                    # Reconstruct the opening first, even if preflight events were already logged.
                    events.insert(session_start, {"type": "input", "text": text})
                    opened = True
        elif phase == "model_response":
            turn = record.get("turn")
            if record.get("content"):
                events.append({"type": "model_message", "turn": turn, "text": _text(record["content"])})
            for call in record.get("calls") or ():
                calls[turn] = call.get("id")
                events.append({"type": "tool_call", "turn": turn, "id": call.get("id"), "name": call.get("name"),
                               "arguments": call.get("arguments")})
        elif phase == "tool_call":
            result = record.get("result")
            if record.get("tool") in _PAGE_TOOLS or (
                isinstance(result, dict) and "content" in result and _PAGE_MARKERS.intersection(result)
            ):
                # A page can split a credential key or hidden-reasoning tag.
                # Allow only typed metadata before any disclosure. Known readers
                # also fail closed when their result is malformed or incomplete.
                result = _page_metadata(result)
            events.append({"type": "tool_result", "turn": record.get("turn"), "id": calls.get(record.get("turn")),
                           "name": record.get("tool"), "text": _text(result)})
        elif phase == "tool_error":
            error = record.get("error")
            if record.get("tool") in _PAGE_TOOLS:
                # b13 wraps ReaderArgumentError.code as ToolError(str).
                # Unexpected exception text carries no safely extractable code.
                error = error if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]*", error) else ""
            events.append({"type": "tool_result", "turn": record.get("turn"), "id": calls.get(record.get("turn")),
                           "name": record.get("tool"), "text": _text(error), "error": True})
        elif phase == "finish_reflection_rejected":
            text = _text(record.get("error"))
            if record.get("correction") is not None:
                text += "; " + _text(record["correction"])
            events.append({"type": "rejected", "turn": record.get("turn"), "reason": "finish_reflection_rejected",
                           "text": text})
        elif phase == "plan_correction_requested":
            events.append({"type": "rejected", "turn": record.get("turn"), "reason": phase,
                           "text": _text(record.get("error"))})
        elif phase == "plan_normalized":
            # This labels Core's recorded normalization separately from its actual tool result.
            normalized = {key: record.get(key) for key in ("path", "declared", "recorded")}
            events.append({"type": "tool_result", "turn": record.get("turn"), "id": calls.get(record.get("turn")),
                           "name": "record_plan", "text": "plan_normalized: " + _text(normalized)})
        elif phase == "refused":
            events.append({"type": "rejected", "turn": record.get("call"), "reason": "refused",
                           "error_class": record.get("error_class"), "text": _text(record.get("error"))})
        elif phase == "failed":
            events.append({"type": "failed", "error_class": record.get("error_class"), "text": _text(record.get("note"))})
        elif phase == "request":
            events.append({"type": "input", "turn": record.get("call"), "text": _text(record.get("opening"))})
        elif phase == "response":
            diagnostics = record.get("diagnostics") if isinstance(record.get("diagnostics"), dict) else {}
            events.append({"type": "model_message", "turn": record.get("call"), "text": _text(record.get("content"))})
            events.append({"type": "agent_summary", "turn": record.get("call"), "tool_turns": record.get("tool_turns"),
                           **{key: diagnostics.get(key) for key in
                              ("file_changes", "command_failures", "last_message", "stderr_tail")}})
        elif phase == "published":
            events.append({"type": "published", "changed": record.get("changed")})
        elif phase == "reflected":
            events.append({"type": "reflected", "status": record.get("status"), "groups": record.get("groups")})
        elif phase not in _SERVICE_PHASES:
            skipped += 1
    return {"events": events, "skipped": skipped, "tool_calls": "recorded" if kind == "chat" else "not_recorded"}


def disclose_events(role: str, events: list[dict], disclose: bool) -> list[dict]:
    """Apply the reflector's event/field disclosure before credential masking."""
    if role == "editor" or disclose:
        return events
    out = []
    for event in events:
        rule = _REFLECTOR.get(event["type"], {})
        if rule is None:
            out.append({"type": event["type"], **WITHHELD})
        else:
            out.append({key: dict(WITHHELD) if rule.get(key) == _PRIVATE else value for key, value in event.items()})
    return out


def failure_text(kind: str, records: list[dict]) -> tuple[str, str] | None:
    """Read a terminal failure without crossing a later request or publication.

    A replay appends records. An earlier session's failure stays in the event
    history, but cannot explain a terminal session whose failure text is absent.
    Service exceptions remain out of the projected events.
    """
    for record in reversed(records):
        phase = record.get("phase")
        if phase in ("model_request", "request", "published"):
            return None
        if phase == "failed":
            return _text(record.get("note")), "editor_log"
        if phase in ("refused", "exception"):
            return _text(record.get("error")), "editor_log"
    return None
