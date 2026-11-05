"""The family-14 Reflector's first-request budget and size records.

For a `chat`
Reflector, with R = `context_compact_chars` and M = `context_message_max_chars`,
H = min(2M, ⌊R/4⌋); the opening's history is elided until the first request
(messages plus the offered tool list, in the characters `_compact_messages`
counts) is within ⌊R/2⌋ − H, and refused only above R − H. These numbers are
used only to build the opening; compaction and later turns are unchanged
(ADR-0025). Other kinds get the history cap only. The size
record holds numbers and closed names, never text.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.private_files import open_private_file
from evolution.tree.roles.chat_loop import chat_request_size
from evolution.tree.roles.reflection_role_helpers import probe_opening
from evolution.tree.roles.reflector.reflect_aggregate import serialized_chars
from evolution.tree.roles.reflector.reflect_brief import (
    AdaptiveReflectBrief,
    reflect_brief_payload,
    render_reflect_opening,
)
from evolution.tree.roles.reflector.reflect_history import HistorySource, Layout, bound_history
from evolution.tree.roles.reflector_contracts import HISTORY_MAX_CHARS, ProbeAccess

COMPONENTS: Final = (
    "system_prompt",
    "instructions",
    "focus",
    "evidence_catalog",
    "documents",
    "history",
    "probe",
    "tools",
    "headroom",
)
#: The longest URL a probe mediator can have: loopback, a 5-digit port and the
#: 32-hex capability path (`ProbeMediator`, `uuid4().hex`).
PROBE_URL_PLACEHOLDER: Final = "http://127.0.0.1:65535/" + "0" * 32 + "/probe"


@dataclass(frozen=True)
class RequestBudget:
    compact_chars: int
    message_max_chars: int
    max_output_tokens: int | None = None

    @property
    def headroom(self) -> int:
        return min(2 * self.message_max_chars, self.compact_chars // 4)

    @property
    def fold_target(self) -> int:
        return self.compact_chars // 2 - self.headroom

    @property
    def refusal_limit(self) -> int:
        return self.compact_chars - self.headroom


@dataclass(frozen=True)
class FirstRequest:
    total_chars: int
    components: Mapping[str, int]


def _payload_parts(brief: AdaptiveReflectBrief) -> dict[str, int]:
    payload = reflect_brief_payload(brief)
    whole = serialized_chars(payload)
    history = serialized_chars(payload["history"])
    catalog = serialized_chars(payload["evidence_catalog"])
    documents = serialized_chars(payload["documents"]) if "documents" in payload else 0
    return {
        "whole": whole,
        "focus": whole - history - catalog - documents,
        "evidence_catalog": catalog,
        "documents": documents,
        "history": history,
    }


def chat_first_request(
    prompt_text: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    brief: AdaptiveReflectBrief,
    *,
    probe: ProbeAccess | None,
    budget: RequestBudget,
) -> FirstRequest:
    opening = str(messages[1]["content"])
    probe_text = probe_opening(probe, typed_body=brief.version == "reflect_brief/6")
    parts = _payload_parts(brief)
    components = {
        "system_prompt": len(prompt_text),
        "instructions": len(opening) - parts["whole"] - len(probe_text),
        "focus": parts["focus"],
        "evidence_catalog": parts["evidence_catalog"],
        "documents": parts["documents"],
        "history": parts["history"],
        "probe": len(probe_text),
        "tools": len(json.dumps(list(tools), ensure_ascii=False)),
        "headroom": budget.headroom,
    }
    return FirstRequest(chat_request_size(messages, tools), components)


def document_first_request(
    brief: AdaptiveReflectBrief,
    *,
    editable_paths: Sequence[str],
    probe: ProbeAccess | None,
    editor_max_changed_files: int | None,
    system_prompt: str | None,
) -> FirstRequest:
    """`agent_cli` and `subprocess`: the opening plus the evidence document; the
    payload travels in both, so each payload component is counted twice."""

    probe_text = probe_opening(probe, typed_body=brief.version == "reflect_brief/6")
    opening = (
        render_reflect_opening(
            brief, (), editable_paths, editor_max_changed_files=editor_max_changed_files
        )
        + probe_text
    )
    parts = _payload_parts(brief)
    system = system_prompt or ""
    components = {
        "system_prompt": len(system),
        "instructions": len(opening) - parts["whole"] - len(probe_text),
        "focus": 2 * parts["focus"],
        "evidence_catalog": 2 * parts["evidence_catalog"],
        "documents": 2 * parts["documents"],
        "history": 2 * parts["history"],
        "probe": len(probe_text),
        "tools": 0,
        "headroom": 0,
    }
    return FirstRequest(len(system) + len(opening) + parts["whole"], components)


def request_budget_of(reflector: object) -> RequestBudget | None:
    method = getattr(reflector, "request_budget", None)
    return None if method is None else method()


def measure_first_request(
    reflector: object,
    brief: AdaptiveReflectBrief,
    *,
    editable_paths: Sequence[str],
    probe: ProbeAccess | None,
    editor_max_changed_files: int | None,
) -> FirstRequest:
    """The bound kind's own measure; a caller-supplied Reflector without one is
    measured as a document kind with no system prompt (history cap only)."""

    method = getattr(reflector, "measure_first_request", None)
    if method is None:
        return document_first_request(
            brief,
            editable_paths=editable_paths,
            probe=probe,
            editor_max_changed_files=editor_max_changed_files,
            system_prompt=None,
        )
    result: FirstRequest = method(
        brief,
        editable_paths=editable_paths,
        probe=probe,
        editor_max_changed_files=editor_max_changed_files,
    )
    return result


@dataclass(frozen=True)
class BoundHistory:
    history: Mapping[str, Any]
    layout: Layout
    request: FirstRequest
    decision: Literal["sent", "refused"]
    below_fold_target: bool | None


def bound_reflection_history(
    source: HistorySource,
    *,
    budget: RequestBudget | None,
    measure: Callable[[Mapping[str, Any]], FirstRequest],
) -> BoundHistory:
    """The first layout within the history cap
    and, for `chat`, the fold target; the minimum otherwise, refused only above
    R − H."""

    def fits(history: Mapping[str, Any]) -> bool:
        if serialized_chars(history) > HISTORY_MAX_CHARS:
            return False
        return budget is None or measure(history).total_chars <= budget.fold_target

    history, layout = bound_history(source, fits=fits)
    request = measure(history)
    if budget is None:
        return BoundHistory(history, layout, request, "sent", None)
    if request.total_chars > budget.refusal_limit:
        return BoundHistory(history, layout, request, "refused", False)
    return BoundHistory(history, layout, request, "sent", request.total_chars <= budget.fold_target)


def budget_record(bound: BoundHistory, *, budget: RequestBudget | None) -> dict[str, Any]:
    components = {name: int(bound.request.components.get(name, 0)) for name in COMPONENTS}
    record: dict[str, Any] = {
        "budget_chars": None if budget is None else budget.compact_chars,
        "headroom_chars": None if budget is None else budget.headroom,
        "history_cap_chars": HISTORY_MAX_CHARS,
        "fold_target_chars": None if budget is None else budget.fold_target,
        "components": components,
        "total_chars": bound.request.total_chars,
        "below_fold_target": bound.below_fold_target,
        "detail_rounds": bound.layout.detail,
        "summary_rounds": bound.layout.summary,
        "aggregated_rounds": bound.layout.aggregated,
        "aggregate_chars": serialized_chars(bound.history["aggregate"]),
        "aggregate_form": bound.layout.form,
        "decision": bound.decision,
    }
    if budget is not None and budget.max_output_tokens is not None:
        record["max_output_tokens"] = budget.max_output_tokens
    if bound.decision == "refused":
        assert budget is not None
        record["over_by"] = bound.request.total_chars - budget.refusal_limit
        record["largest_component"] = max(
            (name for name in COMPONENTS if name != "headroom"),
            key=lambda name: (components[name], name),
        )
    return record


def write_budget_record(run_root: Path, reflection_id: str, record: Mapping[str, Any]) -> None:
    """`<run_root>/reflector-logs/budget/<reflection_id>.json`, `0600` in `0700`
    directories, fsynced; the probe diagnostics' private-file rules."""

    root = ensure_private_directory(Path(run_root) / "reflector-logs")
    directory = ensure_private_directory(root / "budget")
    target = directory / f"{reflection_id}.json"
    descriptor = open_private_file(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        refusal=f"budget record is not a private regular file: {target.name}",
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_json(dict(record)) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
