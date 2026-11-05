"""The `chat` Candidate editor: an OpenAI-compatible tool loop over a projection.

`ChatEditor` is the v2 re-seating of the dormant `file_editor.py` tool loop on
the `CandidateEditor` protocol (`campaign_editor`). The mechanics are the
dormant editor's and are deliberately unchanged in substance -- preflight
dialect negotiation, one native function call executed per turn, the twelve tools, `record_plan`
before the first mutating tool and checked against the final diff, removal of a
tool after five consecutive failures, byte caps per payload and cumulative,
metadata-only reads of a large range, context compaction, catalog pagination,
and a pinned provider verified on every response.

What is new is the boundary. The editor never writes `request.workspace`: it
works in the private projection `editor_workspace.project` hands it and reaches
the real Candidate only through `admit` and `publish`, which are the one
authority on what a change may contain. In family 14 the request carries a
role projection (`EditRequest.projection`), and the projection is sparse: the
tools list, inspect and read only the visible set. The operator's
`writable_paths` are the tools' first line and admission's last. The class is frozen and holds no
per-attempt state; every `edit` builds its own `_Session`, its own client and
its own attempt log, so concurrent attempts share nothing mutable.

The dormant editor's live-tree transaction is gone with the live tree: no
`_isolated_tree_state`, no per-call anchor assertion, no restore-on-failure, no
`FileValidator`, no `EndpointConfig` and no environment-variable endpoint
resolution. Configuration is the manifest's `ChatEditorSection`, sealed into an
`EditorIdentity` before the run starts; the transcript is
`<log_root>/<attempt_id>.jsonl`, owner-only, carrying request deltas, visible
responses and tool results with secrets and hidden reasoning redacted.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import urlsplit

from evolution.prompts import TREE_EDITOR_CONTINUATION
from evolution.tree.artifacts.artifact_policy import matches as policy_matches
from evolution.tree.execution.private_files import WorkspaceError
from evolution.tree.roles.chat_history import (
    CONTINUATION_FIELDS,
    ChatHistoryError,
    assistant_message,
    message_units,
    tool_exchange,
    with_dropped_note,
)
from evolution.tree.roles.config import (
    ChatEditorSection,
    effective_nearest_paths,
    effective_protocol_repairs,
    effective_require_parameters,
    effective_tool_failures,
)
from evolution.tree.roles.editor.editor_brief import EvidenceRecord, render_catalog, render_opening
from evolution.tree.roles.editor_contracts import (
    PROJECTION_REFUSED_NOTE,
    PUBLICATION_REFUSED_NOTE,
    EditRequest,
    EditResult,
    outcome_class,
)
from evolution.tree.roles.identities import EditorIdentity
from evolution.tree.roles.role_call import (
    AttemptRecorder,
    AttemptTelemetry,
    RoleCallExhausted,
    RoleCallRetry,
    RouterRefusal,
    call_with_retry,
    classify_provider_error,
    error_body,
    provider_error_detail,
    router_refusal,
    status_error_body,
)
from evolution.tree.roles.seals import FileSeal, SealChanged, chat_identity
from evolution.tree.roles.workspace import (
    ActorViolation,
    AttemptLog,
    ControlCollision,
    Projection,
    WorkspaceChanged,
    admit,
    bounded_note,
    changed_file_paths,
    discard,
    open_attempt_log,
    project,
    publish,
    relative_candidate_path,
    self_check,
)
from evolution.tree.storage.redaction import safe_record

if TYPE_CHECKING:
    import httpx

__all__ = [
    "CREDENTIAL_HEADERS",
    "EVIDENCE_CHUNK_CHARS",
    "EVIDENCE_PAGE_BYTES",
    "EVIDENCE_PAGE_RECORDS",
    "MAX_CONSECUTIVE_TOOL_FAILURES",
    "MAX_PROTOCOL_REPAIRS",
    "MAX_READ_FILE_CHARS",
    "PROTOCOL_HEADERS",
    "TOOLS",
    "ChatCredentials",
    "ChatEditor",
    "chat_http_client",
    "credential_free_transport",
    "environment_header_names",
    "limited",
    "negotiate_style",
    "provider_request",
    "provider_response",
    "read_file_tool",
    "readable_files",
    "readable_path",
    "record_request",
    "request_options",
    "response_telemetry",
    "sdk_credentials",
]

# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

MAX_PROTOCOL_REPAIRS = 2
MAX_CONSECUTIVE_TOOL_FAILURES = 5
PROVIDER_ATTEMPTS = 3
PROTOCOL_PREVIEW_CHARS = 4_000
MAX_READ_FILE_CHARS = 16_000
FILE_SUMMARY_MAX_CHARS = 800
EVIDENCE_PAGE_RECORDS = 64
EVIDENCE_PAGE_BYTES = 10_000
EVIDENCE_CHUNK_CHARS = 64_000
CONTEXT_TAIL_MESSAGES = 4
CLIENT_CLOSE_GRACE_SECONDS = 1.0
#: The preflight's one user message for the editor and the Reflector
#: (`negotiate_style`'s default `probe_message`).
PREFLIGHT_MESSAGE = "Call list_files now; do not edit anything."


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


Outcome = tuple[Literal["edited", "declined", "failed"], str, "str | None", bool]
"""What one session decides: the `EditResult` status, its unbounded note, its code, and
whether a family-10 continuation turn was taken (`EditResult.continued`; `False` for
every editor without `role_resilience`)."""


class ToolError(RuntimeError):
    """One tool call failed; the model is told and the attempt continues."""


class PlanCorrection(Exception):
    """A family-15 plan problem corrected at the cost of a turn."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


class EditFailure(RuntimeError):
    """A terminal attempt failure, carrying the stable note class."""

    def __init__(
        self,
        klass: str,
        detail: str = "",
        *,
        separator: str = " ",
        code: str | None = None,
    ) -> None:
        self.klass = klass
        self.detail = detail
        self.separator = separator
        self.code = code or klass
        self.note = f"{klass}{separator}{detail}" if detail else klass
        super().__init__(self.note)


class ProviderIdentityError(RuntimeError):
    """The provider returned a different editor model or provider than was pinned."""

    def __init__(self, mismatch: str, details: dict[str, Any]) -> None:
        super().__init__(f"provider returned a different editor {mismatch}")
        self.mismatch = mismatch
        self.details = details


# --------------------------------------------------------------------------
# Declared configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatCredentials:
    """The resolved secret values a `chat` binding carries, never its names.

    `api_key` is `None` for a role that declares no `api_key_env`: it calls its
    endpoint without credentials.
    """

    api_key: str | None
    proxy_url: str | None


#: The request headers an unauthenticated chat role never sends:
#: the SDK builds `Authorization` from its key and the organization and
#: project headers from its organization and project, which it reads from
#: `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` when it is given none; `api-key` is
#: the key header of Azure-style endpoints.
CREDENTIAL_HEADERS: Final = (
    "authorization",
    "api-key",
    "openai-organization",
    "openai-project",
)

#: The headers HTTP itself needs, which `credential_free_transport` keeps even
#: when `OPENAI_CUSTOM_HEADERS` names them: removing
#: `Content-Type` or `Content-Length` would break every request.
PROTOCOL_HEADERS: Final = (
    "content-type",
    "content-length",
    "accept",
    "accept-encoding",
    "user-agent",
    "host",
    "connection",
    "transfer-encoding",
)

#: The key an unauthenticated role hands the SDK, because the SDK would
#: otherwise read `OPENAI_API_KEY` or (3.x) refuse construction. It is not a
#: secret and never leaves the process: `credential_free_transport` removes
#: the `Authorization` header the SDK builds from it.
UNAUTHENTICATED_API_KEY: Final = "evolution-unauthenticated-placeholder"


def environment_header_names(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The lower-cased header names `OPENAI_CUSTOM_HEADERS` declares, in order.

    `openai` 2.38 and later read that variable when a client is built and merge
    its headers into every request, a credential among them as easily as
    anything else. The parse is the SDK's own: newline-separated
    lines, each split at its first colon, the name stripped; a line without a
    colon is skipped, and an empty name is no name. `environ` defaults to the
    process environment, read now.
    """

    text = (os.environ if environ is None else environ).get("OPENAI_CUSTOM_HEADERS")
    names: list[str] = []
    for line in [] if text is None else text.split("\n"):
        colon = line.find(":")
        name = line[:colon].strip().lower() if colon >= 0 else ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


def credential_free_transport(
    inner: httpx.AsyncBaseTransport | None = None, *, also: Collection[str] = ()
) -> httpx.AsyncBaseTransport:
    """A transport that removes `CREDENTIAL_HEADERS`, and every header named in
    `also` except `PROTOCOL_HEADERS`, from every request, then sends it through
    `inner` (a default `httpx.AsyncHTTPTransport` if `None`).

    It sits below the SDK, so it holds whatever credential a given `openai`
    version derives from the process environment. Names
    match case-insensitively. A credential header is removed even when `also`
    does not name it; a protocol header is kept even when it does. Closing it
    closes `inner`.
    """

    import httpx

    named = (name.lower() for name in also)
    stripped = tuple(
        dict.fromkeys(
            (*CREDENTIAL_HEADERS, *(name for name in named if name not in PROTOCOL_HEADERS))
        )
    )

    class _CredentialFree(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self._inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            for name in stripped:
                if name in request.headers:
                    del request.headers[name]
            return await self._inner.handle_async_request(request)

        async def aclose(self) -> None:
            await self._inner.aclose()

    return _CredentialFree(httpx.AsyncHTTPTransport() if inner is None else inner)


def chat_http_client(verify: bool, credentials: ChatCredentials) -> Any:
    """The env-blind `httpx.AsyncClient` a chat role's SDK client sends through.

    A declared key keeps today's client argument for argument. Without one
    The client sends through `credential_free_transport`, which also
    drops every header `OPENAI_CUSTOM_HEADERS` names now (a protocol header
    such as `Content-Type` excepted), over an inner
    transport that carries the same `verify` and proxy: `httpx` routes a
    client-level `proxy` through its own mount, which would bypass a
    `transport=`, so the proxy moves into the inner transport instead.
    """

    import httpx

    if credentials.api_key is not None:
        return httpx.AsyncClient(
            verify=verify,
            trust_env=False,
            proxy=credentials.proxy_url,
            follow_redirects=False,
        )
    return httpx.AsyncClient(
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        transport=credential_free_transport(
            httpx.AsyncHTTPTransport(verify=verify, trust_env=False, proxy=credentials.proxy_url),
            also=environment_header_names(),
        ),
    )


def sdk_credentials(credentials: ChatCredentials, sdk: Callable[..., Any]) -> dict[str, Any]:
    """What a chat role hands `sdk` (`openai.AsyncOpenAI`) about credentials.

    A declared key is passed as today. Without one, the SDK gets
    `UNAUTHENTICATED_API_KEY` and an empty organization and project, so it
    reads none of `OPENAI_API_KEY`, `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID`;
    whatever it derives anyway (`OPENAI_ADMIN_KEY` in 3.x, say) is removed by
    `credential_free_transport` before a request leaves. `organization` and
    `project` are passed only where `sdk`'s signature has them: `openai>=1.0`
    admits an SDK without `project`, which then reads no `OPENAI_PROJECT_ID`
    either.
    """

    if credentials.api_key is not None:
        return {"api_key": credentials.api_key}
    try:
        parameters = inspect.signature(sdk).parameters
    except (TypeError, ValueError):
        parameters = None
    given: dict[str, Any] = {"api_key": UNAUTHENTICATED_API_KEY}
    for name in ("organization", "project"):
        if parameters is not None and name in parameters:
            given[name] = ""
    return given


def request_options(section: ChatEditorSection) -> dict[str, Any]:
    """The `extra_body` the section's routing knobs ask for, or nothing at all.

    Moved from the dormant `EndpointConfig.request_options`: the provider pin,
    its quantizations, the reasoning effort, and the `Qwen/` family's
    `chat_template_kwargs`.
    """

    extra_body: dict[str, Any] = {}
    if section.model.startswith("Qwen/"):
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}
    if section.provider_only or section.provider_order:
        provider: dict[str, Any] = {
            **(
                {"only": list(section.provider_only)}
                if section.provider_only
                else {"order": list(section.provider_order or ())}
            ),
            "allow_fallbacks": bool(section.allow_fallbacks),
            "require_parameters": effective_require_parameters(section),
        }
        if section.quantizations:
            provider["quantizations"] = list(section.quantizations)
        extra_body["provider"] = provider
    if section.reasoning_effort:
        extra_body["reasoning"] = {"effort": section.reasoning_effort}
    return {"extra_body": extra_body} if extra_body else {}


# --------------------------------------------------------------------------
# Tool schema
# --------------------------------------------------------------------------

_TOOL_NAMES = (
    "list_files",
    "inspect_file",
    "read_file",
    "write_file",
    "replace_text",
    "edit_yaml",
    "delete_file",
    "apply_patch",
    "list_evidence",
    "read_evidence",
    "record_plan",
    "finish_edit",
)

_MUTATING_TOOLS = frozenset(
    {"write_file", "edit_yaml", "delete_file", "apply_patch", "replace_text"}
)


def _tool(
    name: str, properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name.replace("_", " "),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool("list_files", {}),
    _tool("inspect_file", {"path": {"type": "string"}}, ["path"]),
    _tool(
        "read_file",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        ["path"],
    ),
    _tool(
        "write_file",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _tool(
        "replace_text",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        ["path", "old_text", "new_text"],
    ),
    _tool(
        "edit_yaml",
        {
            "path": {"type": "string"},
            "key_path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        ["path", "key_path", "new_text"],
    ),
    _tool("delete_file", {"path": {"type": "string"}}, ["path"]),
    _tool("apply_patch", {"patch": {"type": "string"}}, ["patch"]),
    _tool("list_evidence", {"offset": {"type": "integer", "minimum": 0}}),
    _tool(
        "read_evidence",
        {"evidence_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
        ["evidence_id"],
    ),
    _tool(
        "record_plan",
        {
            "summary": {"type": "string"},
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "action": {"type": "string", "enum": ["create", "update", "delete"]},
                        "purpose": {"type": "string"},
                    },
                    "required": ["path", "action", "purpose"],
                    "additionalProperties": False,
                },
            },
        },
        ["summary", "changes"],
    ),
    _tool("finish_edit", {"summary": {"type": "string"}}),
]


_TRACE_TOOL = _tool(
    "read_trace",
    {"evidence_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
    ["evidence_id"],
)
_TRACE_POSITION = (
    next(i for i, tool in enumerate(TOOLS) if tool["function"]["name"] == "read_evidence") + 1
)
TOOLS_V16 = [*TOOLS[:_TRACE_POSITION], _TRACE_TOOL, *TOOLS[_TRACE_POSITION:]]


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


AUTO_PURPOSE: Final = "added: changed outside the plan"


@dataclass(frozen=True)
class PlanChange:
    path: str
    action: str
    purpose: str


@dataclass(frozen=True)
class Plan:
    summary: str
    changes: tuple[PlanChange, ...]

    @property
    def actions(self) -> dict[str, str]:
        return {change.path: change.action for change in self.changes}


def _covers(planned: dict[str, str], actual: dict[str, str]) -> bool:
    """Return whether every actual change was declared."""

    return all(planned.get(path) == action for path, action in actual.items())


#: ADR-0037 D2/D3: at most this many paths are named in one budget refusal.
_BUDGET_PATH_LIMIT = 32


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def _path_list(paths: Sequence[str]) -> str:
    shown = list(paths[:_BUDGET_PATH_LIMIT])
    rest = len(paths) - len(shown)
    return ", ".join(shown) + (f", and {rest} more" if rest else "")


def _final_planned_actions(before: dict[str, bytes], plans: list[Plan]) -> dict[str, str]:
    actions: dict[str, str] = {}
    for plan in plans:
        for change in plan.changes:
            originally_existed = change.path in before
            if change.action == "delete":
                if originally_existed:
                    actions[change.path] = "delete"
                else:
                    actions.pop(change.path, None)
            elif originally_existed:
                actions[change.path] = "update"
            else:
                actions[change.path] = "create"
    return actions


def _changed_actions(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, str]:
    return {
        path: "create" if path not in before else "delete" if path not in after else "update"
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    }


# --------------------------------------------------------------------------
# Wire helpers: calls, messages, compaction
# --------------------------------------------------------------------------


def _tool_calls(response: Any) -> list[Any]:
    message = response.choices[0].message
    calls = list(getattr(message, "tool_calls", None) or [])
    legacy_call = getattr(message, "function_call", None)
    return calls or ([legacy_call] if legacy_call is not None else [])


def _call_name(call: Any) -> str:
    return str(getattr(getattr(call, "function", call), "name", ""))


def _raw_call_arguments(call: Any) -> Any:
    return getattr(getattr(call, "function", call), "arguments", "{}")


def _call_arguments(call: Any) -> dict[str, Any]:
    raw = _raw_call_arguments(call)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ToolError("tool arguments are not JSON") from exc
    if not isinstance(value, dict):
        raise ToolError("tool arguments must be an object")
    return value


def _call_id(call: Any) -> str:
    return str(getattr(call, "id", "call"))


def _encoded_call_arguments(call: Any) -> str:
    arguments = _raw_call_arguments(call)
    return arguments if isinstance(arguments, str) else json.dumps(arguments)


def _response_message(response: Any, request_style: str) -> dict[str, Any] | None:
    message = response.choices[0].message
    if not getattr(message, "tool_calls", None) and (
        getattr(message, "function_call", None) is not None or request_style.startswith("legacy")
    ):
        return None
    try:
        assistant = assistant_message(message)
        if not any(value for key, value in assistant.items() if key != "role"):
            return None
        return assistant
    except ChatHistoryError as exc:
        raise EditFailure("protocol", str(exc)) from exc


def _tool_result_messages(
    call: Any, output: dict[str, Any], assistant: dict[str, Any] | None
) -> list[dict[str, Any]]:
    content = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    if not hasattr(call, "function"):
        return [
            {
                "role": "user",
                "content": (
                    f"Local function {_call_name(call)} returned {content}. "
                    "Continue with exactly one native function call and no prose."
                ),
            }
        ]
    if assistant is None:
        raise EditFailure("protocol", "missing modern assistant message")
    return tool_exchange(assistant, output)


def _text_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return {
        "characters": len(text),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "preview": text[:PROTOCOL_PREVIEW_CHARS],
        "truncated": len(text) > PROTOCOL_PREVIEW_CHARS,
    }


def _protocol_correction(
    error: str, calls: list[Any], tools: Sequence[dict[str, Any]] = TOOLS
) -> str:
    call_details = [
        {"name": _call_name(call), "arguments": _text_summary(_encoded_call_arguments(call))}
        for call in calls
    ]
    name = _call_name(calls[0]) if len(calls) == 1 else ""
    schema = next(
        (tool["function"] for tool in tools if tool["function"]["name"] == name),
        None,
    )
    return (
        "Your previous response was rejected locally. Return exactly one native "
        "function call and no prose. Correction details: "
        + json.dumps(
            {"error": error, "received_calls": call_details, "required_schema": schema},
            sort_keys=True,
        )
    )


def _compact_message(message: dict[str, Any], max_chars: int) -> dict[str, Any]:
    if message.get("role") == "assistant":
        return message
    compacted = dict(message)
    content = compacted.get("content")
    if isinstance(content, str) and len(content) > max_chars:
        compacted["content"] = (
            content[:max_chars] + "\n[context truncated; re-read the needed range]"
        )
    return compacted


def _compact_messages(
    messages: list[dict[str, Any]], *, trigger_chars: int, message_max_chars: int
) -> None:
    """Fold the middle of a long conversation into one summary, in place.

    Above `trigger_chars`, aim at half that size. Retained assistant messages
    and the latest tool exchange stay whole; older results may be truncated
    to `message_max_chars` (the role's `context_*` manifest keys).
    """

    try:
        units = message_units(messages[2:])
    except ChatHistoryError as exc:
        raise EditFailure("protocol", str(exc)) from exc
    size = sum(len(json.dumps(message, ensure_ascii=False, default=str)) for message in messages)
    if size <= trigger_chars or not units:
        return
    target_chars = trigger_chars // 2
    prefix = messages[:2]
    latest_exchange = next(
        (index for index in range(len(units) - 1, -1, -1) if units[index][0].get("tool_calls")),
        len(units) - 1,
    )
    protected_unit = units[latest_exchange]
    tail_units: list[list[dict[str, Any]]] = []
    tail_size = 0
    for index in range(len(units) - 1, -1, -1):
        unit = units[index]
        if index < latest_exchange and tail_size + len(unit) > CONTEXT_TAIL_MESSAGES:
            break
        tail_units.insert(0, unit)
        tail_size += len(unit)
    previous_units = units[: len(units) - len(tail_units)]
    previous = [message for unit in previous_units for message in unit]
    summary_parts = [
        "Earlier editor context was compacted. "
        "Re-read any details you need with line-bounded tools.",
    ]
    for message in previous:
        text = safe_record(message.get("content"))
        if text:
            summary_parts.append(f"{message.get('role')}: {str(text)[:800]}")
    summary = {"role": "user", "content": "\n".join(summary_parts)[:8_000]}
    tail_units = [
        unit
        if unit is protected_unit
        else [_compact_message(message, message_max_chars) for message in unit]
        for unit in tail_units
    ]
    tail = [message for unit in tail_units for message in unit]
    compacted = prefix + [summary] + tail
    while (
        len(json.dumps(compacted, ensure_ascii=False, default=str)) > target_chars
        and len(tail_units) > 1
        and tail_units[0] is not protected_unit
    ):
        tail_units.pop(0)
        tail = [message for unit in tail_units for message in unit]
        compacted = prefix + [summary] + tail
    messages[:] = compacted


# --------------------------------------------------------------------------
# Provider identity
# --------------------------------------------------------------------------


def _provider_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _returned_model(response: Any) -> str | None:
    value = getattr(response, "model", None)
    return value if isinstance(value, str) else None


def _returned_provider(response: Any) -> str | None:
    value = getattr(response, "provider", None)
    extra = getattr(response, "model_extra", None)
    if value is None and isinstance(extra, dict):
        value = extra.get("provider")
    return value if isinstance(value, str) else None


def _requested_provider(section: ChatEditorSection) -> str | None:
    providers = section.provider_only or section.provider_order
    return ",".join(providers) if providers else None


def _provider_matches(returned: str | None, section: ChatEditorSection) -> bool:
    if section.allow_fallbacks:
        return True
    providers = section.provider_only or section.provider_order
    if returned is None or not providers:
        return False
    return any(_provider_name(returned) == _provider_name(name) for name in providers)


# --------------------------------------------------------------------------
# File inspection (pure, over a root)
# --------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"non-UTF-8 file: {path.name}") from exc
    except OSError as exc:
        raise ToolError(f"cannot read file: {path.name}") from exc


def _read_file_range(path: Path, arguments: dict[str, Any]) -> tuple[str, int, int, int, str]:
    raw = _read_text(path)
    lines = raw.splitlines(keepends=True)
    total = len(lines)
    start = arguments.get("start_line", 1)
    end = arguments.get("end_line", total or 1)
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        raise ToolError("line range must satisfy 1 <= start_line <= end_line")
    if start > total and total:
        raise ToolError("start_line is past end of file")
    end = min(end, total or 1)
    return (
        "".join(lines[start - 1 : end]),
        start,
        end,
        total,
        hashlib.sha256(raw.encode()).hexdigest(),
    )


def _markdown_sections(lines: list[str]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    start_line = 1
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index, line in enumerate(lines[1:], 2) if line.strip() in {"---", "..."}),
            None,
        )
        if closing is not None:
            sections.append(
                {
                    "line": 1,
                    "end_line": closing,
                    "path": "frontmatter",
                    "title": "YAML front matter",
                    "level": 0,
                }
            )
            start_line = closing + 1
    stack: list[tuple[int, str]] = []
    for index in range(start_line, len(lines) + 1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[index - 1])
        if match is None:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and level <= stack[-1][0]:
            stack.pop()
        sections.append(
            {
                "line": index,
                "end_line": len(lines),
                "path": ".".join([item[1] for item in stack] + [title]),
                "title": title,
                "level": level,
            }
        )
        stack.append((level, title))
    for index, section in enumerate(sections):
        if section["path"] == "frontmatter":
            continue
        next_section = next(
            (
                candidate
                for candidate in sections[index + 1 :]
                if candidate["path"] != "frontmatter" and candidate["level"] <= section["level"]
            ),
            None,
        )
        section["end_line"] = next_section["line"] - 1 if next_section is not None else len(lines)
    return sections


def _yaml_sections(lines: list[str]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    stack: list[tuple[int, str, int]] = []
    block_indent: int | None = None
    for index, line in enumerate(lines, 1):
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        match = re.match(r"^( *)([^\s#][^:]*):(.*)$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        title = match.group(2).strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        path_parts = [item[1] for item in stack] + [title]
        sections.append(
            {
                "line": index,
                "end_line": len(lines),
                "path": ".".join(path_parts),
                "title": title,
                "level": len(stack),
                "_indent": indent,
            }
        )
        stack.append((indent, title, len(sections) - 1))
        value = match.group(3).strip()
        if value.startswith(("|", ">")):
            block_indent = indent
        elif value[:1] in {"'", '"'} and not value.endswith(value[:1]):
            block_indent = indent
    for index, section in enumerate(sections):
        next_section = next(
            (
                candidate
                for candidate in sections[index + 1 :]
                if candidate["_indent"] <= section["_indent"]
            ),
            None,
        )
        section["end_line"] = next_section["line"] - 1 if next_section is not None else len(lines)
        del section["_indent"]
    return sections


def _inspect_file(path: Path, root: Path) -> dict[str, Any]:
    lines = _read_text(path).splitlines()
    suffix = path.suffix.lower()
    if suffix == ".md":
        sections = _markdown_sections(lines)
    elif suffix in {".yaml", ".yml"}:
        sections = _yaml_sections(lines)
    else:
        sections = []
    return {
        "path": path.relative_to(root).as_posix(),
        "line_count": len(lines),
        "first_lines": lines[:5],
        "sections": sections,
    }


def _file_summary(path: Path, root: Path) -> dict[str, Any]:
    info = _inspect_file(path, root)
    sections = [
        {key: section[key] for key in ("line", "end_line", "path", "title", "level")}
        for section in info["sections"]
    ]
    summary = json.dumps(
        {
            "path": info["path"],
            "line_count": info["line_count"],
            "size_bytes": path.stat().st_size,
            "first_lines": info["first_lines"],
            "sections": sections,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {"path": info["path"], "summary": summary[:FILE_SUMMARY_MAX_CHARS]}


def readable_path(root: Path, value: str) -> Path:
    """An actor-supplied path resolved under `root`, or a `ToolError`.

    The Candidate-path grammar (not a rule's): `relative_rule` vetoes every
    dot-prefixed segment, which is right for an operator's policy rule and
    wrong for a file the policy admits -- with `writable_paths=(".github/**",)`,
    `.github/workflows/ci.yml` is exactly the file the actor is asked to edit.
    A symlink anywhere on the way, or a target that resolves outside `root`, is
    refused: both the editor's tools and the Reflector's read-only ones reach a
    projection only through here.
    """

    try:
        relative = PurePosixPath(relative_candidate_path(value))
    except ValueError as exc:
        raise ToolError(f"unsafe path: {value!r}") from exc
    target = root.joinpath(*relative.parts)
    if target.is_symlink() or any(
        item.is_symlink() for item in _parents_to_root(target.parent, root)
    ):
        raise ToolError(f"symlinks are not allowed: {value}")
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ToolError(f"path escapes workspace: {value}") from exc
    return target


def readable_files(root: Path) -> list[str]:
    """Every readable regular file under `root`, as relative POSIX paths.

    Dot-prefixed names are listed like any other. A chat actor's projection is
    made without a control prefix, so it holds Candidate content and nothing
    else, and a `.github/` the artifact policy admitted is a file the actor
    must be able to see before it can edit it.
    """

    paths: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        paths.append(relative)
    return sorted(paths)


def levenshtein(a: str, b: str) -> int:
    """Unit-cost edit distance over Unicode code points with one retained row."""
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        following = [i]
        for j, right in enumerate(b, 1):
            following.append(min(following[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = following
    return row[-1]


def nearest_paths(requested: str, listed: Sequence[str], count: int) -> list[str]:
    if count <= 0:
        return []
    name = PurePosixPath(requested).name
    return sorted(
        listed,
        key=lambda path: (
            levenshtein(name, PurePosixPath(path).name),
            levenshtein(requested, path),
            path,
        ),
    )[:count]


def missing_path_text(requested: str, root: Path, count: int) -> str:
    message = f"path does not exist: {requested}"
    paths = nearest_paths(requested, readable_files(root), count) if count else []
    return message + ("; nearest existing paths: " + ", ".join(paths) if paths else "")


def limited(content: str, max_payload_bytes: int) -> str:
    if len(content.encode()) > max_payload_bytes:
        raise ToolError("text payload exceeds limit")
    return content


def read_file_tool(
    root: Path,
    arguments: dict[str, Any],
    *,
    max_payload_bytes: int,
    nearest_paths: int | None = None,
) -> tuple[dict[str, Any], int]:
    """The `read_file` tool, over any projection root: the editor's and the Reflector's."""

    _expect(arguments, {"path"}, optional={"start_line", "end_line"})
    path = readable_path(root, arguments["path"])
    if nearest_paths is not None and not path.exists():
        raise ToolError(missing_path_text(arguments["path"], root, nearest_paths))
    try:
        content, start_line, end_line, total_lines, file_hash = _read_file_range(path, arguments)
    except ToolError as exc:
        if "past end of file" not in str(exc):
            raise
        info = _inspect_file(path, root)
        return {
            "path": path.relative_to(root).as_posix(),
            "error": str(exc),
            "total_lines": info["line_count"],
            "first_lines": info["first_lines"],
            "sections": info["sections"][:64],
        }, 0
    numbered = "".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(content.splitlines(keepends=True), start=start_line)
    )
    relative = path.relative_to(root).as_posix()
    if len(content) > MAX_READ_FILE_CHARS:
        info = _inspect_file(path, root)
        return {
            "path": relative,
            "large_file": True,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "size_bytes": path.stat().st_size,
            "sha256": file_hash,
            "first_lines": info["first_lines"],
            "sections": info["sections"][:64],
            "sections_truncated": len(info["sections"]) > 64,
            "message": (
                "File range is too large to return. Choose a smaller "
                "start_line/end_line range and call read_file again."
            ),
        }, 0
    return {
        "path": relative,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "sha256": file_hash,
        "content": limited(content, max_payload_bytes),
        "line_numbered_content": limited(numbered, max_payload_bytes),
    }, 0


# --------------------------------------------------------------------------
# Catalog pagination
# --------------------------------------------------------------------------


def _evidence_page(catalog: Sequence[EvidenceRecord], offset: int) -> list[dict[str, Any]]:
    page: list[dict[str, Any]] = []
    for record in catalog[offset : offset + EVIDENCE_PAGE_RECORDS]:
        candidate = [*page, {"id": record.evidence_id, "kind": record.kind}]
        next_offset = offset + len(candidate)
        response = {
            "records": candidate,
            "next_offset": next_offset if next_offset < len(catalog) else None,
            "total": len(catalog),
        }
        if page and len(json.dumps(response, separators=(",", ":")).encode()) > EVIDENCE_PAGE_BYTES:
            break
        page = candidate
    return page


def _evidence_chunk(content: str, offset: int, *, family15: bool = False) -> tuple[str, int | None]:
    if offset > len(content):
        detail = f"; offset must be between 0 and {len(content)}" if family15 else ""
        raise ToolError("offset is past end of evidence" + detail)
    if offset and content[offset - 1] != "\n":
        raise ToolError("offset must point to an evidence record boundary")
    if offset == len(content):
        return "", None
    limit = min(offset + EVIDENCE_CHUNK_CHARS, len(content))
    if limit == len(content):
        return content[offset:], None
    end = content.rfind("\n", offset, limit + 1) + 1
    if end <= offset:
        newline = content.find("\n", limit)
        end = len(content) if newline < 0 else newline + 1
    return content[offset:end], None if end == len(content) else end


# --------------------------------------------------------------------------
# Unified diff
# --------------------------------------------------------------------------


def _parse_patch(patch: str) -> dict[str, list[tuple[int, int, list[str]]]]:
    """Parse a small, strict unified diff; all files are staged before writing."""

    lines = patch.splitlines(keepends=True)
    changes: dict[str, list[tuple[int, int, list[str]]]] = {}
    index = 0
    current: str | None = None
    while index < len(lines):
        if lines[index].startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise ToolError("malformed unified diff headers")
            target = lines[index + 1][4:].strip()
            if not target.startswith("b/"):
                raise ToolError("patch target must use b/ path")
            current = target[2:]
            changes.setdefault(current, [])
            index += 2
            continue
        if lines[index].startswith("@@"):
            if current is None:
                raise ToolError("hunk without file header")
            header = lines[index].split("@@", 2)[1].strip().split()
            try:
                old = header[0][1:]
                hunk: tuple[int, int, list[str]]
                if old:
                    start, _, length = old.partition(",")
                    hunk = (int(start), int(length or 1), [])
                else:
                    hunk = (0, 0, [])
                changes[current].append(hunk)
            except (IndexError, ValueError) as exc:
                raise ToolError("malformed hunk header") from exc
            index += 1
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- ")):
                if not lines[index].startswith((" ", "+", "-")):
                    raise ToolError("malformed hunk line")
                changes[current][-1][2].append(lines[index])
                index += 1
            continue
        if lines[index].strip():
            raise ToolError("malformed unified diff")
        index += 1
    if not changes:
        raise ToolError("empty patch")
    return changes


def _hunk_position(lines: list[str], old: list[str], start: int, length: int, offset: int) -> int:
    if start == 0 and length == 0:
        return 0
    if start:
        return max(0, start - 1 + offset)
    if not old:
        return len(lines)
    try:
        return lines.index(old[0])
    except ValueError as exc:
        raise ToolError("patch hunk does not match file") from exc


def _apply_hunks(original: str, hunks: list[tuple[int, int, list[str]]]) -> str:
    lines = original.splitlines(keepends=True)
    offset = 0
    for start, length, hunk_lines in hunks:
        old = [line[1:] for line in hunk_lines if line.startswith((" ", "-"))]
        new = [line[1:] for line in hunk_lines if line.startswith((" ", "+"))]
        position = _hunk_position(lines, old, start, length, offset)
        if lines[position : position + len(old)] != old:
            raise ToolError("patch hunk does not match file")
        lines[position : position + len(old)] = new
        offset += len(new) - len(old)
    return "".join(lines)


# --------------------------------------------------------------------------
# The editor
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatEditor:
    """A `CandidateEditor` that is one OpenAI-compatible tool loop per attempt."""

    section: ChatEditorSection
    credentials: ChatCredentials
    prompt: FileSeal
    log_root: Path
    secrets: tuple[str, ...]
    client_factory: Callable[[ChatEditorSection, ChatCredentials], Any] | None = None
    #: Instance digests of the run-directory hygiene findings the operator
    #: has accepted (PDR-0010), handed to `open_attempt_log` with the
    #: `log_root` it re-admits; empty by default.
    admitted_hygiene: frozenset[str] = field(default=frozenset(), kw_only=True)
    #: `/11` (PDR-0019, ADR-0023): true for a family-10 run. The client is
    #: then built with `max_retries=0` and every provider request runs
    #: through `role_call.call_with_retry` under `section.retry`, which must
    #: not be `None`. False (the default) keeps every earlier family's client
    #: and `provider_request` loop byte for byte.
    role_resilience: bool = field(default=False, kw_only=True)
    plan_correction: bool = field(default=False, kw_only=True)
    family15: bool = field(default=False, kw_only=True)
    identity: EditorIdentity = field(init=False)

    @property
    def max_changed_files(self) -> int | None:
        """ADR-0037 D10: the file budget the campaign states to the family-14 Reflector."""

        return self.section.budgets.max_changed_files

    def __post_init__(self) -> None:
        object.__setattr__(self, "log_root", Path(self.log_root))
        continuation_sha256 = (
            hashlib.sha256(TREE_EDITOR_CONTINUATION.encode()).hexdigest()
            if self.role_resilience
            else None
        )
        object.__setattr__(
            self,
            "identity",
            chat_identity(
                self.section,
                self.prompt,
                continuation_sha256=continuation_sha256,
                plan_correction=self.plan_correction,
                family15=self.family15,
            ),
        )
        if self.role_resilience:
            assert self.section.retry is not None, "family-10 chat editor requires retry"

    async def edit(self, request: EditRequest) -> EditResult:
        """Run one attempt: project, edit in the projection, admit and publish.

        Its provider attempts feed `request.attempts`, the executor's recorder
        for the operator's line, or a private one without it.
        """

        attempts = request.attempts if request.attempts is not None else AttemptRecorder()
        attempts.unauthenticated = self.credentials.api_key is None
        return await self._edit(request, attempts)

    async def _edit(self, request: EditRequest, attempts: AttemptRecorder) -> EditResult:
        if request.attempt_id != Path(request.attempt_id).name or request.attempt_id in {
            "",
            ".",
            "..",
        }:
            raise ValueError("attempt_id must be one path segment")
        try:
            self.prompt.verify()
        except SealChanged:
            return EditResult("failed", "prompt_changed", code="prompt_changed")
        # The attempt log is opened before the projection, so its own refusal
        # -- an entry at the attempt pathname that is not a private regular
        # file -- has no log to be recorded in and is classified here, as the
        # workspace refusal it is.
        try:
            attempt_log = open_attempt_log(
                self.log_root,
                request.attempt_id,
                secrets=self.secrets,
                admitted=self.admitted_hygiene,
            )
        except WorkspaceError as exc:
            return EditResult(
                "failed", bounded_note(f"workspace_changed {exc}"), code="workspace_changed"
            )
        role = request.projection
        with attempt_log as log:
            try:
                projection = project(
                    request.workspace,
                    visible=None if role is None else role.visible,
                    expected_digest=None if role is None else role.expected_digest,
                )
            except ControlCollision as exc:
                log.record(_refusal("control_collision", str(exc)))
                return EditResult(
                    "failed", bounded_note(f"control_collision {exc}"), code="control_collision"
                )
            except WorkspaceError as exc:
                log.record(_refusal("workspace_changed", str(exc)))
                return EditResult(
                    "failed",
                    bounded_note(f"workspace_changed {exc}")
                    if role is None
                    # Family 14: a refusal of the real workspace can name a
                    # hidden entry; the transcript keeps the detail.
                    else PROJECTION_REFUSED_NOTE,
                    code="workspace_changed",
                )
            try:
                outcome = await self._session(request, projection, log, attempts)
                result = self._after_run(
                    EditResult(outcome[0], bounded_note(outcome[1]), outcome[2], outcome[3]), log
                )
                if result.status == "edited":
                    changes = admit(
                        projection,
                        writable_paths=request.writable_paths,
                        budgets=self.section.budgets,
                    )
                    self.prompt.verify()
                    publish(changes, projection)
                return result
            except ActorViolation as exc:
                log.record(_refusal("actor_violation", str(exc)))
                return self._after_run(
                    EditResult("failed", bounded_note(f"actor_violation {exc}"), code=exc.code),
                    log,
                )
            except WorkspaceChanged as exc:
                log.record(_refusal("workspace_changed", str(exc)))
                return self._after_run(
                    EditResult(
                        "failed",
                        bounded_note(f"workspace_changed {exc}")
                        if role is None
                        else PUBLICATION_REFUSED_NOTE,
                        code="workspace_changed",
                    ),
                    log,
                )
            except SealChanged:
                # The publication check itself: the seal is already known to
                # have changed, so there is nothing left for `_after_run`.
                log.record(_refusal("prompt_changed"))
                return EditResult("failed", "prompt_changed", code="prompt_changed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.record(
                    {
                        "phase": "exception",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                return self._after_run(
                    EditResult(
                        "failed",
                        bounded_note(f"exception:{type(exc).__name__}"),
                        code="exception",
                    ),
                    log,
                )
            finally:
                discard(projection)

    def _after_run(self, result: EditResult, log: AttemptLog) -> EditResult:
        """Verify the prompt seal after the attempt, whatever its outcome.

        The seal is verified "before and after every `edit`" (PDR-0005), not
        only before a publication: a prompt rewritten under a run that declines
        or fails is a tampering the result must carry rather than mask. An
        `edited` outcome is not re-checked here because its own verification
        stands immediately before `publish`, which keeps publication strictly
        after the check; every other way an attempt ends passes through here.
        The replaced result keeps the outcome it overrode in its detail.
        """

        if result.status == "edited":
            return result
        try:
            self.prompt.verify()
        except SealChanged:
            replaced = outcome_class(result)
            if replaced == "prompt_changed":
                return result
            log.record(_refusal("prompt_changed", f"after {replaced}"))
            return EditResult(
                "failed",
                bounded_note(f"prompt_changed (after {replaced})"),
                code="prompt_changed",
            )
        return result

    async def _session(
        self,
        request: EditRequest,
        projection: Projection,
        log: AttemptLog,
        attempts: AttemptRecorder,
    ) -> Outcome:
        """One tool loop, over its own client, closed whatever the outcome.

        The client is built inside the `try`: a construction that fails --
        the packaging guard's `provider_error`, or a `client_factory` that
        raises -- is then the attempt's own failure rather than an
        unhandled escape, and `finally` closes whatever was built.
        """

        client: Any = None
        session: _Session | None = None
        try:
            if self.client_factory is not None:
                client = self.client_factory(self.section, self.credentials)
            elif self.role_resilience:
                client = _new_client(self.section, self.credentials, max_retries=0)
            else:
                client = _new_client(self.section, self.credentials)
            session = _Session(self, request, projection, log, client, attempts)
            return await session.run()
        except EditFailure as exc:
            log.record({"phase": "failed", "error_class": exc.klass, "note": exc.note})
            attempts.end(exc)
            # `continued` is only meaningful on a finished (`edited`/`declined`)
            # result; `editor_failed` never journals it (campaign.py only reads
            # `outcome.continued` off the `finished` terminal). Carried through
            # here anyway so `Outcome`'s shape stays uniform across returns.
            return ("failed", exc.note, exc.code, session._continued if session else False)
        finally:
            if client is not None:
                await _close_client(client)


def _refusal(error_class: str, detail: str = "") -> dict[str, Any]:
    return {"phase": "refused", "error_class": error_class, "error": detail}


def _new_client(
    section: ChatEditorSection, credentials: ChatCredentials, *, max_retries: int | None = None
) -> Any:
    """The default client: `openai.AsyncOpenAI` over an env-blind `httpx` client.

    `max_retries` is `None` for every family but 10, which keeps the SDK's own
    default byte for byte; family 10 passes `0`, so `role_call.call_with_retry`
    is the sole retry owner and its `max_attempts` is the real attempt count.
    """

    try:
        import httpx  # noqa: F401 - the packaging guard covers `chat_http_client`'s import
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise EditFailure(
            "provider_error", "install evo-skills[tree] to use the chat editor"
        ) from exc
    kwargs: dict[str, Any] = {}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    return AsyncOpenAI(
        **sdk_credentials(credentials, AsyncOpenAI),
        base_url=section.base_url,
        http_client=cast(Any, chat_http_client(section.verify_ssl_certs, credentials)),
        **kwargs,
    )


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        value = close()
        if not inspect.isawaitable(value):
            return
        close_task = asyncio.ensure_future(value)
        try:
            async with asyncio.timeout(CLIENT_CLOSE_GRACE_SECONDS):
                await asyncio.shield(close_task)
        except TimeoutError:
            close_task.cancel()
            close_task.add_done_callback(_consume_late_task)
        except asyncio.CancelledError:
            owner_task = asyncio.current_task()
            if owner_task is not None and owner_task.cancelling():
                raise
    except Exception:
        return


def _consume_late_task(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


# --------------------------------------------------------------------------
# The provider protocol, shared by the editor's loop and the Reflector's
# --------------------------------------------------------------------------
#
# These are the editor session's own provider methods, lifted unchanged to
# module level so `chat_reflector`'s session (and `roles/chat_loop`, which
# runs its turns) uses the same dialect negotiation, the same retries, the
# same identity pin and the same delta request audit rather than a second
# copy of them. Each takes the state the method read off `self`: the client,
# the section, the attempt log, and the session's own
# `remaining`/`next_turn`/`record_request` callables. `role` appears only in
# the notes a failure carries, so an operator reading a reflection's
# `provider_error` is not told an editor timed out.


def record_request(
    log: AttemptLog,
    sent: Sequence[str],
    messages: list[dict[str, Any]],
    request_style: str,
    turn: int,
    tools: list[dict[str, Any]],
) -> list[str]:
    """Record a provider request as a delta over the previous one, and return its keys.

    Every request carries the whole conversation, so recording it in full made
    a session of `n` turns log about `n²/2` messages. The record keeps the
    full-record property at linear cost: `messages_after` is the number of
    leading messages identical to the previous record's, and `messages` holds
    only the rest, so the conversation at any request is the previous one cut
    at `messages_after` plus this record's messages. Compaction rewrites the
    middle of the list; the shared prefix then shrinks and the rewritten
    messages are recorded again, as they should be.
    """

    encoded = [_message_key(message) for message in messages]
    after = _shared_prefix(sent, encoded)
    log.record(_request_audit(messages, request_style, turn, tools, after=after))
    return encoded


def _protocol_options(
    section: ChatEditorSection, tools: list[dict[str, Any]], request_style: str
) -> dict[str, Any]:
    if request_style.startswith("legacy"):
        return {
            "functions": [tool["function"] for tool in tools],
            "function_call": (
                {"name": tools[0]["function"]["name"]}
                if request_style == "legacy_preflight"
                else "auto"
            ),
        }
    if request_style == "modern_auto":
        return {"tools": tools, "tool_choice": "auto"}
    options: dict[str, Any] = {"tools": tools, "tool_choice": "required"}
    if request_style == "modern" and urlsplit(section.base_url).hostname != "openrouter.ai":
        options["parallel_tool_calls"] = False
    return options


def _verify_identity(
    response: Any, section: ChatEditorSection, log: AttemptLog | None = None
) -> None:
    returned_model = _returned_model(response)
    returned_provider = _returned_provider(response)
    model_verified = returned_model == section.model
    provider_verified = _provider_matches(returned_provider, section)
    if model_verified and provider_verified:
        if section.allow_fallbacks and log is not None:
            assert section.provider_order
            log.record(
                {
                    "phase": "provider_route",
                    "returned_provider": returned_provider,
                    "fallback": returned_provider is None
                    or _provider_name(returned_provider)
                    != _provider_name(section.provider_order[0]),
                }
            )
        return
    raise ProviderIdentityError(
        "model" if not model_verified else "provider",
        {
            "requested_model": section.model,
            "requested_provider": _requested_provider(section),
            "returned_model": returned_model,
            "returned_provider": returned_provider,
            "model_verified": model_verified,
            "provider_verified": provider_verified,
        },
    )


def _provider_attempt_record(
    log: AttemptLog,
    request_style: str,
    turn: int | None,
    attempt: int,
    outcome: str,
    error_type: str | None = None,
    *,
    note: str | None = None,
) -> None:
    """`error_type` is the failure's own class, never a stand-in for it: an
    expired deadline is an `EditFailure("provider_error",...)` and is recorded
    as `provider_error` with its note, not as a `TimeoutError`.
    """

    log.record(
        {
            "phase": "provider_attempt",
            "turn": turn,
            "request_style": request_style,
            "attempt": attempt,
            "outcome": outcome,
            "error_type": error_type,
            "note": note,
        }
    )


async def provider_response(
    *,
    client: Any,
    section: ChatEditorSection,
    messages: list[dict[str, Any]],
    protocol_options: dict[str, Any],
    remaining: Callable[[], float],
    role: str = "editor",
    raw_errors: bool = False,
    log: AttemptLog | None = None,
    family15: bool = False,
) -> Any:
    """One completion call, bounded by the session's remaining time and pinned.

    `raw_errors` is set by the family-10 path in `provider_request`: a timeout
    is let through as `TimeoutError` instead of being converted to
    `EditFailure`, so `role_call.call_with_retry`'s classifier sees it. The
    empty-`choices` case stays a protocol matter, not a transport one, and is
    unaffected by the flag.
    """

    task = asyncio.ensure_future(
        client.chat.completions.create(
            model=section.model,
            messages=messages,
            max_tokens=section.max_output_tokens,
            **protocol_options,
            **request_options(section),
        )
    )
    try:
        async with asyncio.timeout(remaining()):
            response = await asyncio.shield(task)
        if family15 and (body := error_body(response)) is not None:
            raise router_refusal(body, http_status=200)
        if (section.provider_only or section.provider_order) and not (
            family15 and role == "grouping"
        ):
            _verify_identity(response, section, log)
        if not getattr(response, "choices", None):
            raise RuntimeError("provider returned a completion with no choices")
        return response
    except TimeoutError as exc:
        task.cancel()
        task.add_done_callback(_consume_late_task)
        if raw_errors:
            raise
        raise EditFailure("provider_error", f"{role} timed out") from exc
    except BaseException:
        task.cancel()
        task.add_done_callback(_consume_late_task)
        raise


@dataclass
class RefusalTally:
    refused: int = 0
    credential: bool = False
    other: int = 0


async def provider_request(
    *,
    client: Any,
    section: ChatEditorSection,
    log: AttemptLog,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    request_style: str,
    turn: int | None,
    remaining: Callable[[], float],
    role: str = "editor",
    retry: RoleCallRetry | None = None,
    deadline: float | None = None,
    attempts: AttemptRecorder | None = None,
    family15: bool = False,
    refusals: RefusalTally | None = None,
) -> Any:
    """One provider turn in the named dialect.

    `retry is None` (every family but 10) is today's loop, retried
    `PROVIDER_ATTEMPTS` times with the SDK's own defaults, untouched. Family
    10 passes the declared `retry` policy and the session's absolute
    `deadline`; the request then runs once per `role_call.call_with_retry`
    attempt, over a client built with `max_retries=0`, so `retry.max_attempts`
    is the real count. Only the closed transient classes are retried;
    `RoleCallExhausted` becomes an `EditFailure("provider_error",...,
    code="provider_exhausted" | "provider_rejected")`, and a pinned-provider
    mismatch stays `provider_changed`.

    `attempts`, when given, sees the same attempt telemetry the log records,
    and each exhaustion, for the invocation's operator line.
    """

    if family15:
        assert retry is not None, "family-15 requests require the shared retry policy"
    protocol_options = _protocol_options(section, tools, request_style)
    if retry is not None:
        assert deadline is not None

        request_id = f"{role}-t{turn}"
        sent = 0

        async def send(timeout: float) -> Any:
            nonlocal sent
            sent += 1
            started = time.monotonic()
            try:
                return await provider_response(
                    client=client,
                    section=section,
                    messages=messages,
                    protocol_options=protocol_options,
                    remaining=lambda: timeout,
                    role=role,
                    raw_errors=True,
                    log=log,
                    family15=family15,
                )
            except ProviderIdentityError:
                if refusals is not None:
                    refusals.other += 1
                # Not a provider exception, so `call_with_retry` writes no
                # telemetry for it: record the failed attempt here, in the
                # same shape, as the family-9 loop records its own.
                on_attempt(
                    AttemptTelemetry(
                        role, request_id, sent, time.monotonic() - started, "identity_mismatch"
                    )
                )
                raise

            except Exception as exc:
                if refusals is not None:
                    if isinstance(exc, RouterRefusal):
                        if classify_provider_error(exc)[1] == "credential":
                            refusals.credential = True
                        else:
                            refusals.refused += 1
                    else:
                        refusals.other += 1
                if family15:
                    import openai

                    refused = exc if isinstance(exc, RouterRefusal) else None
                    if isinstance(exc, openai.APIStatusError):
                        body = status_error_body(exc.body)
                        if body is not None:
                            refused = router_refusal(body, http_status=exc.status_code)
                    if refused is not None:
                        log.record(
                            {
                                "phase": "provider_refused",
                                "request_style": request_style,
                                "turn": turn,
                                "attempt": sent,
                                "http_status": refused.http_status,
                                "error_code": refused.inner_code,
                                "provider_name": refused.provider_name,
                                "message": refused.message,
                            }
                        )
                raise

        def on_attempt(telemetry: Any) -> None:
            log.record({"phase": "provider_attempt", **asdict(telemetry)})
            if attempts is not None:
                attempts.observe(telemetry)

        try:
            return await call_with_retry(
                send,
                policy=retry,
                role=role,
                request_id=request_id,
                deadline=deadline,
                on_attempt=on_attempt,
            )
        except RoleCallExhausted as exc:
            if attempts is not None:
                attempts.exhausted(exc)
            note = f"{role} request {exc.reason} after {exc.attempts} attempts ({exc.code})"
            if isinstance(exc.__cause__, RouterRefusal):
                refused = exc.__cause__
                provider = f", provider {refused.safe_provider}" if refused.safe_provider else ""
                note += (
                    f"; router refused the request (status {refused.http_status}, "
                    f"code {refused.code_text or 'unknown'}{provider}); "
                    "its message is in the role log"
                )
            raise EditFailure(
                "provider_error",
                note,
                code="provider_rejected"
                if exc.reason == "provider_rejected"
                else "provider_exhausted",
            ) from exc
        except ProviderIdentityError as exc:
            log.record(
                {
                    "phase": "identity_mismatch",
                    "turn": turn,
                    "request_style": request_style,
                    **exc.details,
                }
            )
            raise EditFailure("provider_changed", str(exc)) from exc

    errors: list[str] = []
    for attempt in range(PROVIDER_ATTEMPTS):
        _provider_attempt_record(log, request_style, turn, attempt + 1, "started")
        try:
            response = await provider_response(
                client=client,
                section=section,
                messages=messages,
                protocol_options=protocol_options,
                remaining=remaining,
                role=role,
                log=log,
            )
        except EditFailure as exc:
            _provider_attempt_record(
                log, request_style, turn, attempt + 1, "failed", exc.klass, note=exc.note
            )
            raise
        except ProviderIdentityError as exc:
            _provider_attempt_record(
                log, request_style, turn, attempt + 1, "failed", type(exc).__name__
            )
            log.record(
                {
                    "phase": "identity_mismatch",
                    "turn": turn,
                    "request_style": request_style,
                    **exc.details,
                }
            )
            raise EditFailure("provider_changed", str(exc)) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            errors.append(error_type)
            _provider_attempt_record(log, request_style, turn, attempt + 1, "failed", error_type)
            if attempt == PROVIDER_ATTEMPTS - 1:
                raise EditFailure(
                    "provider_error",
                    f"{role} request failed after {PROVIDER_ATTEMPTS} attempts: "
                    + ", ".join(errors),
                ) from exc
            await asyncio.sleep(min(2**attempt, remaining()))
            continue
        remaining()
        _provider_attempt_record(log, request_style, turn, attempt + 1, "succeeded")
        return response
    raise AssertionError("unreachable")  # pragma: no cover


def _request_failure_detail(exc: BaseException) -> dict[str, int | str | None]:
    """A `model_request_failed` record's `http_status` and `provider_code`
    the same fields, under the same names, as the attempt
    telemetry's, both `None` for a failure without a response. Never the
    provider's message text (ADR-0039)."""

    status, code = provider_error_detail(exc)
    return {"http_status": status, "provider_code": code}


def _dialect_rejection(exc: EditFailure) -> bool:
    """A family-10 `provider_rejected` whose classifier code is `http_4xx`:
    the provider refused this request shape, not the caller's credential."""

    cause = exc.__cause__
    return (
        exc.code == "provider_rejected"
        and isinstance(cause, RoleCallExhausted)
        and cause.reason == "provider_rejected"
        and cause.code == "http_4xx"
    )


async def negotiate_style(
    *,
    client: Any,
    section: ChatEditorSection,
    log: AttemptLog,
    tools: list[dict[str, Any]],
    next_turn: Callable[[], int],
    remaining: Callable[[], float],
    record: Callable[[list[dict[str, Any]], str, int, list[dict[str, Any]]], None],
    role: str = "editor",
    retry: RoleCallRetry | None = None,
    deadline: float | None = None,
    attempts: AttemptRecorder | None = None,
    probe_tool: str = "list_files",
    probe_message: str = PREFLIGHT_MESSAGE,
    family15: bool = False,
) -> str:
    """The preflight: the dialect the provider actually speaks, or `protocol`.

    `tools[0]` must be `probe_tool` -- the one tool the preflight offers, and
    the one call it accepts: `list_files` for both the editor's tool set and
    the Reflector's. `probe_message` is the preflight's one user message. Its
    attempts are `attempts`' preflight phase, so the operator's line can say
    the failures were the preflight's.
    """

    recorder = attempts if attempts is not None else AttemptRecorder()
    with recorder.preflight():
        return await _negotiate_style(
            client=client,
            section=section,
            log=log,
            tools=tools,
            next_turn=next_turn,
            remaining=remaining,
            record=record,
            role=role,
            retry=retry,
            deadline=deadline,
            attempts=attempts,
            probe_tool=probe_tool,
            probe_message=probe_message,
            family15=family15,
        )


async def _negotiate_style(
    *,
    client: Any,
    section: ChatEditorSection,
    log: AttemptLog,
    tools: list[dict[str, Any]],
    next_turn: Callable[[], int],
    remaining: Callable[[], float],
    record: Callable[[list[dict[str, Any]], str, int, list[dict[str, Any]]], None],
    role: str,
    retry: RoleCallRetry | None,
    deadline: float | None,
    attempts: AttemptRecorder | None,
    probe_tool: str = "list_files",
    probe_message: str = PREFLIGHT_MESSAGE,
    family15: bool = False,
) -> str:

    preflight_tools = tools[:1]
    assert preflight_tools[0]["function"]["name"] == probe_tool, "tools[0] is the probe tool"
    messages: list[dict[str, Any]] = [{"role": "user", "content": probe_message}]
    styles: tuple[str, ...] = (
        ("modern", "modern_single", "modern_auto") if family15 else ("modern", "modern_auto")
    )
    if section.provider_only and "z-ai" in section.provider_only:
        styles = ("modern_auto",)
    elif urlsplit(section.base_url).hostname == "openrouter.ai":
        styles = ("modern", "modern_auto")
    turn = next_turn()
    calls: list[Any] = []
    modern_error: str | None = None
    for style in styles:
        tally = RefusalTally() if family15 else None
        record(messages, style, turn, preflight_tools)
        try:
            response = await provider_request(
                client=client,
                section=section,
                log=log,
                messages=messages,
                tools=preflight_tools,
                request_style=style,
                turn=turn,
                remaining=remaining,
                role=role,
                retry=retry,
                deadline=deadline,
                attempts=attempts,
                family15=family15,
                refusals=tally,
            )
        except EditFailure as exc:
            # A family-10 exhaustion or rejection is not a dialect mismatch:
            # it must reach the caller as-is (`run()`'s own check re-codes
            # everything else `preflight_failed`), never be swallowed into a
            # style fallback or the legacy protocol-repair loop below
            # (transient/terminal provider errors never use protocol repair).
            # The one exception is a plain HTTP 4xx: a provider that refuses
            # the `tools`/`tool_choice` request shape is exactly the dialect
            # mismatch family 9 falls back on, so it falls through to the next
            # style here too. A credential or TLS rejection and a transient
            # exhaustion still raise at once.
            dialect = _dialect_rejection(exc)
            if (
                family15
                and isinstance(exc.__cause__, RoleCallExhausted)
                and isinstance(exc.__cause__.__cause__, RouterRefusal)
            ):
                dialect = False
            refused_style = (
                tally is not None
                and style != styles[-1]
                and tally.refused > 0
                and tally.other == 0
                and not tally.credential
            )
            if exc.klass != "provider_error" or (
                exc.code in ("provider_exhausted", "provider_rejected")
                and not dialect
                and not refused_style
            ):
                raise
            modern_error = exc.note
            if attempts is not None and dialect:
                # The next style answers this refusal; it is negotiation,
                # not a failed attempt for the operator's line. The role log
                # keeps it, with its status, just below.
                attempts.withdraw(exc.__cause__)
            log.record(
                {
                    "phase": "model_request_failed",
                    "turn": turn,
                    "request_style": style,
                    "error": exc.note,
                    **_request_failure_detail(exc),
                }
            )
            continue
        log.record(_response_audit(response, section, style, turn))
        calls = _tool_calls(response)
        if calls and _call_name(calls[0]) == probe_tool:
            resolved = style if hasattr(calls[0], "function") else "legacy"
            log.record({"phase": "preflight", "tool": probe_tool, "request_style": resolved})
            return resolved
        log.record(_preflight_failure(response, calls, style))
    messages.append(
        {
            "role": "user",
            "content": _protocol_correction(
                modern_error or f"preflight requires {probe_tool}", calls, tools
            ),
        }
    )
    for _repair in range(effective_protocol_repairs(section) + 1):
        remaining()
        turn = next_turn()
        record(messages, "legacy", turn, preflight_tools)
        response = await provider_request(
            client=client,
            section=section,
            log=log,
            messages=messages,
            tools=preflight_tools,
            request_style="legacy_preflight",
            turn=turn,
            remaining=remaining,
            role=role,
            retry=retry,
            deadline=deadline,
            attempts=attempts,
            family15=family15,
        )
        log.record(_response_audit(response, section, "legacy", turn))
        calls = _tool_calls(response)
        if calls and _call_name(calls[0]) == probe_tool:
            log.record({"phase": "preflight", "tool": probe_tool, "request_style": "legacy"})
            return "legacy"
        log.record(_preflight_failure(response, calls, "legacy"))
        messages.append(
            {
                "role": "user",
                "content": _protocol_correction(f"preflight requires {probe_tool}", calls, tools),
            }
        )
    raise EditFailure("protocol", f"{role} preflight must make exactly one {probe_tool} call")


# --------------------------------------------------------------------------
# One attempt
# --------------------------------------------------------------------------


class _Session:
    """The per-attempt state of one tool loop; nothing here outlives `edit`."""

    def __init__(
        self,
        editor: ChatEditor,
        request: EditRequest,
        projection: Projection,
        log: AttemptLog,
        client: Any,
        attempts: AttemptRecorder | None = None,
    ) -> None:
        self.section = editor.section
        self.prompt = editor.prompt
        self.request = request
        self.projection = projection
        self.root = projection.root
        self.log = log
        self.client = client
        self.attempts = attempts
        self.role_resilience = editor.role_resilience
        self.plan_correction = editor.plan_correction
        self.family15 = editor.family15
        self.deadline = time.monotonic() + request.deadline_seconds
        self.catalog = render_catalog(request.brief)
        self.tools = TOOLS if request.traces is None else TOOLS_V16
        self._turns = 0
        #: `/11` (PDR-0019, ADR-0023): true once this attempt has taken its one
        #: family-10 continuation turn after an empty-diff decline.
        self._continued = False
        # The messages of the last `model_request` record, serialized one by one,
        # so the next record can carry only what changed (W2 transcript growth).
        self._sent: list[str] = []

    # -- transcript -------------------------------------------------------

    def _record(self, entry: dict[str, Any]) -> None:
        """Append one event; `AttemptLog.record` redacts it with the run's secrets."""

        self.log.record(entry)

    def _record_request(
        self,
        messages: list[dict[str, Any]],
        request_style: str,
        turn: int,
        tools: list[dict[str, Any]],
    ) -> None:
        self._sent = record_request(self.log, self._sent, messages, request_style, turn, tools)

    # -- budgets ----------------------------------------------------------

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EditFailure("provider_error", "editor deadline reached")
        return remaining

    def _next_turn(self) -> int:
        if self._turns >= self.section.max_turns:
            raise EditFailure("turns_exhausted", "editor turn budget exceeded")
        turn = self._turns
        self._turns += 1
        return turn

    # -- paths ------------------------------------------------------------

    def _readable_path(self, value: str) -> Path:
        return readable_path(self.root, value)

    def _is_writable(self, relative: str) -> bool:
        """The tools' first line, using admission's own matcher (controller R5)."""

        candidate = PurePosixPath(relative)
        return any(policy_matches(rule, candidate, False) for rule in self.request.writable_paths)

    def _writable_path(self, value: str) -> Path:
        target = self._readable_path(value)
        relative = target.relative_to(self.root).as_posix()
        if not self._is_writable(relative):
            raise ToolError(f"path is not writable: {value}")
        return target

    def _plan_path(self, value: str) -> Path:
        try:
            target = self._readable_path(value)
        except ToolError as exc:
            raise PlanCorrection("plan_path_unsafe", str(exc)) from exc
        if not self._is_writable(target.relative_to(self.root).as_posix()):
            raise PlanCorrection("plan_path_not_writable", f"path is not writable: {value}")
        return target

    def _all_files(self) -> list[str]:
        return readable_files(self.root)

    def _require_existing(self, path: Path) -> None:
        if self.family15 and not path.exists():
            raise ToolError(
                missing_path_text(
                    path.relative_to(self.root).as_posix(),
                    self.root,
                    effective_nearest_paths(self.section),
                )
            )

    def _file_bytes(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if not path.is_symlink() and path.is_file()
        }

    def _limited(self, content: str) -> str:
        return limited(content, self.section.budgets.max_payload_bytes)

    # -- the run ----------------------------------------------------------

    async def run(self) -> Outcome:
        try:
            request_style = await self._preflight()
        except EditFailure as exc:
            if exc.code in ("provider_exhausted", "provider_rejected"):
                raise
            raise EditFailure(
                exc.klass, exc.detail, separator=exc.separator, code="preflight_failed"
            ) from exc
        return await self._conversation(request_style)

    # -- preflight --------------------------------------------------------

    async def _preflight(self) -> str:
        return await negotiate_style(
            client=self.client,
            section=self.section,
            log=self.log,
            tools=self.tools,
            next_turn=self._next_turn,
            remaining=self._remaining,
            record=self._record_request,
            retry=self.section.retry if self.role_resilience else None,
            deadline=self.deadline if self.role_resilience else None,
            attempts=self.attempts,
            family15=self.family15,
        )

    # -- the tool loop ----------------------------------------------------

    async def _conversation(self, request_style: str) -> Outcome:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt.text()},
            {
                "role": "user",
                "content": render_opening(
                    self.request.brief,
                    self.catalog,
                    self.request.writable_paths,
                    max_changed_files=self.section.budgets.max_changed_files,
                ),
            },
        ]
        written = 0
        if self.family15:
            messages[1]["content"] += (
                f"\n\nSession limits: at most {self.section.max_turns} model turns in this session, "
                f"the preflight included, and {effective_protocol_repairs(self.section)} protocol repairs."
            )
        plan: Plan | None = None
        plans: list[Plan] = []
        plan_before: dict[str, bytes] | None = None
        plan_nudged = False
        self_check_repaired = False
        evidence_progress: dict[str, int] = {}
        completed_evidence_ids: set[str] = set()
        listed_evidence = False
        protocol_repairs = 0
        blocked_tools: set[str] = set()
        failing_tool: str | None = None
        consecutive_failures = 0
        assistant: dict[str, Any] | None = None
        dropped: list[str] = []

        def noted(output: dict[str, Any]) -> dict[str, Any]:
            return with_dropped_note(output, dropped) if self.family15 else output

        def results(call: Any, output: dict[str, Any]) -> list[dict[str, Any]]:
            return _tool_result_messages(call, noted(output), assistant)

        def request_repair(error: str, calls: list[Any], category: str | None = None) -> None:
            nonlocal protocol_repairs
            if protocol_repairs >= effective_protocol_repairs(self.section):
                raise EditFailure("protocol", f"editor protocol repair limit exceeded: {error}")
            protocol_repairs += 1
            if assistant is not None:
                messages.extend(
                    tool_exchange(assistant, noted({"error": error})) if calls else [assistant]
                )
            correction = _protocol_correction(error, calls, self.tools)
            messages.append({"role": "user", "content": correction})
            self._record(
                {
                    "phase": "protocol_repair_requested",
                    "repair": protocol_repairs,
                    "error": error,
                    **({"category": category} if self.plan_correction else {}),
                }
            )

        def request_correction(category: str, message: str, call: Any) -> None:
            messages.extend(results(call, {"error": message}))
            self._record(
                {
                    "phase": "plan_correction_requested",
                    "turn": turn,
                    "category": category,
                    "error": message,
                }
            )

        while True:
            # A `record_plan` that passed validation, recorded only once its
            # budget check (ADR-0037 D2) and the tool call succeed.
            proposed: Plan | None = None
            normalizations: list[tuple[str, str | None, str]] = []
            rewrites: dict[str, str] = {}
            self._remaining()
            turn = self._next_turn()
            _compact_messages(
                messages,
                trigger_chars=self.section.context_compact_chars,
                message_max_chars=self.section.context_message_max_chars,
            )
            offered = self._offered_tools(blocked_tools)
            self._record_request(messages, request_style, turn, offered)
            try:
                response = await self._request(messages, offered, request_style, turn)
            except EditFailure as exc:
                self._record(
                    {
                        "phase": "model_request_failed",
                        "turn": turn,
                        "request_style": request_style,
                        "error": exc.note,
                        **_request_failure_detail(exc),
                    }
                )
                raise
            assistant = _response_message(response, request_style)
            self._record(_response_audit(response, self.section, request_style, turn))
            calls = _tool_calls(response)
            dropped = [_call_name(item) for item in calls[1:]] if self.family15 else []
            if not calls:
                request_repair(
                    "response must contain exactly one native function call", calls, "no_tool_call"
                )
                continue
            if len(calls) > 1:
                self._record(
                    {
                        "phase": "parallel_calls_dropped",
                        "turn": turn,
                        "tool": _call_name(calls[0]),
                        "calls": [
                            {"id": _call_id(item), "name": _call_name(item), "arguments": ""}
                            for item in calls[1:]
                        ],
                    }
                )
            call = calls[0]
            name = _call_name(call)
            if name not in {tool["function"]["name"] for tool in self.tools}:
                request_repair(f"unknown tool: {name}", calls, "unknown_tool")
                continue
            try:
                arguments = _call_arguments(call)
            except ToolError as exc:
                request_repair(str(exc), calls, "invalid_arguments")
                continue
            if self.plan_correction and name in _MUTATING_TOOLS | {"finish_edit"}:
                # Shape errors remain broken calls even before the first plan.
                parameters = next(
                    tool["function"]["parameters"]
                    for tool in TOOLS
                    if tool["function"]["name"] == name
                )
                try:
                    # finish_edit's runtime contract requires summary even though
                    # the historical offered schema leaves it optional.
                    required = {"summary"} if name == "finish_edit" else set(parameters["required"])
                    _expect(arguments, required, set(parameters["properties"]))
                    if any(not isinstance(value, str) for value in arguments.values()):
                        raise ToolError("invalid tool arguments")
                except ToolError as exc:
                    request_repair(str(exc), calls, "invalid_arguments")
                    continue
            if name == "read_evidence" and "offset" not in arguments:
                evidence_id = arguments.get("evidence_id")
                if isinstance(evidence_id, str):
                    arguments = {**arguments, "offset": evidence_progress.get(evidence_id, 0)}
            if name == "record_plan":
                try:
                    proposed, normalized = self._validate_plan(
                        arguments, normalize=self.plan_correction
                    )
                    normalizations.extend(normalized)
                except PlanCorrection as exc:
                    request_correction(exc.category, exc.message, call)
                    continue
                except ToolError as exc:
                    request_repair(str(exc), calls, "invalid_arguments")
                    continue
            elif name == "finish_edit":
                if plan is None or plan_before is None:
                    if self.plan_correction:
                        request_correction(
                            "finish_before_plan", "record_plan is required before finish_edit", call
                        )
                    else:
                        request_repair("record_plan is required before finish_edit", calls)
                    continue
                planned = _final_planned_actions(
                    plan_before, [plan] if self.plan_correction else plans
                )
                actual = _changed_actions(plan_before, self._file_bytes())
                mismatch = None if actual == planned else f"expected {planned}, got {actual}"
                if self.plan_correction and mismatch:
                    request_correction(
                        "plan_mismatch",
                        "record_plan does not exactly match final changes: " + mismatch,
                        call,
                    )
                    continue
                if mismatch and not (plan_nudged and _covers(planned, actual)):
                    if plan_nudged:
                        raise EditFailure("plan_mismatch", mismatch)
                    plan_nudged = True
                    request_repair(
                        "record_plan does not exactly match final changes: " + mismatch, calls
                    )
                    continue
            elif name in _MUTATING_TOOLS:
                if plan is None:
                    if self.plan_correction:
                        request_correction(
                            "mutation_before_plan",
                            "record_plan is required before a file change",
                            call,
                        )
                    else:
                        request_repair("record_plan is required before a file change", calls)
                    continue
                try:
                    if self.plan_correction:
                        rewrites, normalizations = self._planned_mutation_paths(
                            name, arguments, plan
                        )
                    else:
                        self._validate_planned_mutation(name, arguments, plan)
                except PlanCorrection as exc:
                    request_correction(exc.category, exc.message, call)
                    continue
                except ToolError as exc:
                    request_repair(str(exc), calls, "invalid_arguments")
                    continue
            try:
                # ADR-0037 D2/D3: a budget refusal is an ordinary tool error,
                # never a protocol repair, and a refused plan is never recorded.
                if proposed is not None:
                    self._check_plan_budget(proposed)
                elif name in _MUTATING_TOOLS:
                    self._check_edit_budget(name, arguments)
                output, bytes_written = self._run_tool(name, arguments)
            except (ToolError, OSError) as exc:
                if isinstance(exc, ToolError) and str(exc) == "invalid tool arguments":
                    request_repair(str(exc), calls, "invalid_arguments")
                    continue
                consecutive_failures = consecutive_failures + 1 if failing_tool == name else 1
                failing_tool = name
                guidance = ""
                if name == "apply_patch":
                    guidance = (
                        " Use replace_text with an exact unique old_text instead; "
                        "this file is too large to rewrite with write_file."
                        if self._patch_targets_large(str(arguments.get("patch", "")))
                        else " Correct it, or use replace_text with an exact unique old_text."
                    )
                if consecutive_failures >= effective_tool_failures(self.section):
                    blocked_tools.add(name)
                    guidance += (
                        f" {name} has failed {consecutive_failures} times in a row and is "
                        "no longer available; use another tool."
                    )
                self._record({"phase": "tool_error", "turn": turn, "tool": name, "error": str(exc)})
                messages.extend(results(call, {"error": f"{exc}.{guidance}".rstrip()}))
                continue
            failing_tool, consecutive_failures = None, 0
            if proposed is not None:
                if plan_before is None:
                    plan_before = self._file_bytes()
                if self.plan_correction:
                    plan = self._with_changed_paths(proposed, plan_before)
                    normalizations.extend(
                        (change.path, None, change.action)
                        for change in plan.changes
                        if change.path not in proposed.actions
                    )
                    plans = [plan]
                else:
                    plan = proposed
                    plans.append(plan)
            elif self.plan_correction and plan is not None and normalizations:
                changes = [
                    replace(change, action=rewrites.get(change.path, change.action))
                    for change in plan.changes
                ]
                changes.extend(
                    PlanChange(path, action, AUTO_PURPOSE)
                    for path, declared, action in normalizations
                    if declared is None
                )
                plan = Plan(plan.summary, tuple(changes))
                plans = [plan]
            if self.plan_correction:
                for path, declared, recorded in normalizations:
                    self._record(
                        {
                            "phase": "plan_normalized",
                            "turn": turn,
                            "path": path,
                            "declared": declared,
                            "recorded": recorded,
                        }
                    )
            if name == "list_evidence":
                listed_evidence = True
            elif name == "read_evidence":
                evidence_id = str(arguments["evidence_id"])
                offset = arguments.get("offset", 0)
                if offset == evidence_progress.get(evidence_id, 0):
                    next_offset = output["next_offset"]
                    evidence_progress[evidence_id] = (
                        output["total_chars"] if next_offset is None else next_offset
                    )
                    if next_offset is None:
                        completed_evidence_ids.add(evidence_id)
            written += bytes_written
            if written > self.section.budgets.max_written_bytes:
                raise EditFailure(
                    "actor_violation", "written-byte budget exceeded", code="written_byte_budget"
                )
            self._record(
                {
                    "phase": "tool_call",
                    "turn": turn,
                    "tool": name,
                    "arguments": arguments,
                    "result": output,
                }
            )
            if name == "finish_edit":
                summary = str(arguments["summary"]).strip()
                issues = self._self_check()
                if issues and not self_check_repaired:
                    self_check_repaired = True
                    messages.extend(results(call, output))
                    messages.append({"role": "user", "content": "self_check: " + "; ".join(issues)})
                    self._record(
                        {"phase": "self_check_repair_requested", "turn": turn, "error": issues[0]}
                    )
                    continue
                if issues:
                    raise EditFailure("self_check_failed", issues[0], separator=": ")
                self._record(self._evidence_usage(listed_evidence, completed_evidence_ids))
                # The *final diff*, not the history of tool calls, decides.
                # A model that writes a file and then restores its original
                # bytes -- the plan-mismatch nudge and a second `finish_edit`
                # make that a reachable script -- has proposed nothing, and an
                # empty change set must neither be admitted nor published: the
                # Candidate root would be replaced for no change at all.
                if self.projection.changes():
                    return ("edited", summary, None, self._continued)
                if (
                    self.role_resilience
                    and not self._continued
                    and self._turns < self.section.max_turns
                    and time.monotonic() < self.deadline
                ):
                    self._continued = True
                    messages.extend(results(call, output))
                    messages.append({"role": "user", "content": TREE_EDITOR_CONTINUATION})
                    self._record({"phase": "continuation"})
                    continue
                return ("declined", summary or "no change proposed", None, self._continued)
            messages.extend(results(call, output))

    def _evidence_usage(self, listed: bool, read_ids: set[str]) -> dict[str, Any]:
        cases = {record.evidence_id for record in self.catalog if record.kind == "case"}
        unread = sorted(cases - read_ids)
        return {
            "phase": "evidence_usage",
            "severity": "warning" if unread else "info",
            "catalog_listed": listed,
            "read_ids": sorted(read_ids),
            "unread_cases": unread,
        }

    def _self_check(self) -> tuple[str, ...]:
        changed = [change.path for change in self.projection.changes() if change.action != "delete"]
        return self_check(self.root, changed)

    def _offered_tools(self, blocked: Collection[str] = ()) -> list[dict[str, Any]]:
        """Tools the projection supports and that are not blocked this attempt."""

        writable = [name for name in self._all_files() if self._is_writable(name)]
        dropped = set(blocked)
        if not any(name.endswith((".yaml", ".yml")) for name in writable):
            dropped.add("edit_yaml")
        return [tool for tool in self.tools if tool["function"]["name"] not in dropped]

    # -- plan discipline --------------------------------------------------

    def _validate_plan(
        self, arguments: dict[str, Any], *, normalize: bool = False
    ) -> tuple[Plan, list[tuple[str, str, str]]]:
        _expect(arguments, {"summary", "changes"})
        if not isinstance(arguments["summary"], str) or not arguments["summary"].strip():
            raise ToolError("record_plan summary must be a non-empty string")
        changes = arguments["changes"]
        if not isinstance(changes, list):
            raise ToolError("record_plan changes must be an array")
        changes_by_path: dict[str, PlanChange] = {}
        normalizations: list[tuple[str, str, str]] = []
        for change in changes:
            if not isinstance(change, dict):
                raise ToolError("record_plan changes must contain objects")
            _expect(change, {"path", "action", "purpose"})
            path, action, purpose = change["path"], change["action"], change["purpose"]
            if not all(
                isinstance(value, str) and value.strip() for value in (path, action, purpose)
            ):
                raise ToolError("record_plan fields must be non-empty strings")
            if action not in {"create", "update", "delete"}:
                raise ToolError("record_plan action must be create, update, or delete")
            safe_path = self._plan_path(path) if normalize else self._writable_path(path)
            relative = safe_path.relative_to(self.root).as_posix()
            if relative in changes_by_path:
                if normalize:
                    raise PlanCorrection(
                        "plan_repeated_path", f"record_plan repeats path: {relative}"
                    )
                raise ToolError(f"record_plan repeats path: {relative}")
            if normalize and action in {"create", "update"}:
                recorded = "update" if safe_path.is_file() else "create"
                if recorded != action:
                    normalizations.append((relative, action, recorded))
                    action = recorded
            else:
                if action == "create" and safe_path.exists():
                    raise ToolError(f"record_plan create path already exists: {relative}")
                if action in {"update", "delete"} and not safe_path.is_file():
                    if normalize:
                        raise PlanCorrection(
                            "plan_delete_missing",
                            (
                                missing_path_text(
                                    relative, self.root, effective_nearest_paths(self.section)
                                )
                                if self.family15
                                else f"record_plan delete path does not exist: {relative}"
                            )
                            + "; "
                            + "if this edit already deleted it, leave it out: "
                            "the plan keeps every changed path",
                        )
                    raise ToolError(f"record_plan {action} path does not exist: {relative}")
            changes_by_path[relative] = PlanChange(relative, action, purpose)
        return Plan(arguments["summary"].strip(), tuple(changes_by_path.values())), normalizations

    def _validate_planned_mutation(self, name: str, arguments: dict[str, Any], plan: Plan) -> None:
        if name == "apply_patch":
            patch = arguments.get("patch")
            if not isinstance(patch, str):
                raise ToolError("patch must be a string")
            paths = set(_parse_patch(patch))
        else:
            path = arguments.get("path")
            if not isinstance(path, str):
                raise ToolError("file change path must be a string")
            paths = {self._writable_path(path).relative_to(self.root).as_posix()}
        actions = plan.actions
        unplanned = paths - set(actions)
        if unplanned:
            raise ToolError("file change is outside record_plan: " + ", ".join(sorted(unplanned)))
        for relative in paths:
            planned_action = actions[relative]
            exists = (self.root / relative).is_file()
            actual_action = "delete" if name == "delete_file" else "update" if exists else "create"
            if planned_action != actual_action:
                raise ToolError(
                    f"planned {planned_action} but requested {actual_action}: {relative}"
                )

    def _with_changed_paths(self, proposed: Plan, before: dict[str, bytes]) -> Plan:
        changes = list(proposed.changes)
        for relative, action in _changed_actions(before, self._file_bytes()).items():
            if relative not in proposed.actions:
                try:
                    self._writable_path(relative)
                except ToolError:
                    continue  # The final mismatch/admission still refuses any outside change.
                changes.append(PlanChange(relative, action, AUTO_PURPOSE))
        return Plan(proposed.summary, tuple(changes))

    def _planned_mutation_paths(
        self, name: str, arguments: dict[str, Any], plan: Plan
    ) -> tuple[dict[str, str], list[tuple[str, str | None, str]]]:
        if name == "apply_patch":
            patch = arguments.get("patch")
            if not isinstance(patch, str):
                raise ToolError("patch must be a string")
            paths = set(_parse_patch(patch))
        else:
            path = arguments.get("path")
            if not isinstance(path, str):
                raise ToolError("file change path must be a string")
            paths = {path}
        rewrites: dict[str, str] = {}
        normalizations: list[tuple[str, str | None, str]] = []
        actions = plan.actions
        for path in sorted(paths):
            target = self._plan_path(path)
            relative = target.relative_to(self.root).as_posix()
            actual = (
                "delete" if name == "delete_file" else "update" if target.is_file() else "create"
            )
            declared = actions.get(relative)
            if declared is None:
                normalizations.append((relative, None, actual))
            elif (
                declared in {"create", "update"}
                and actual in {"create", "update"}
                and declared != actual
            ):
                rewrites[relative] = actual
                normalizations.append((relative, declared, actual))
        return rewrites, normalizations

    # -- the file budget (ADR-0037 D2, D3) --------------------------------

    def _check_plan_budget(self, plan: Plan) -> None:
        """Refuse a plan that would leave more files changed than the budget (D2).

        Counts the files that differ from the Candidate now -- the set admission
        counts -- together with the files this plan names. An earlier plan is
        not counted again: its effect is in the live tree, so an updated file
        written back to its original bytes frees its slot. Raised inside the ordinary tool-error path, so it never
        spends a protocol repair (spec D2a).
        """

        limit = self.section.budgets.max_changed_files
        if limit is None:
            return
        current = changed_file_paths(self.projection)
        planned = set(plan.actions)
        total = set(current) | planned
        if len(total) <= limit:
            return
        raise ToolError(
            f"record_plan refused: it would leave {_files(len(total))} changed "
            f"({len(current)} changed now, {len(planned - set(current))} more planned), "
            f"and this Attempt may change at most {_files(limit)}. "
            f"Choose at most {limit} of these paths, starting with the diagnosis's "
            "highest-mass hypothesis, and record the plan again; "
            "a planned update counts toward this plan even if the file later returns to "
            "its original bytes"
        )

    def _check_edit_budget(self, name: str, arguments: dict[str, Any]) -> None:
        """Refuse a mutation that would change one file more than the budget (D3)."""

        limit = self.section.budgets.max_changed_files
        if limit is None:
            return
        if name == "apply_patch":
            paths = set(_parse_patch(str(arguments.get("patch", ""))))
        else:
            paths = {self._writable_path(str(arguments["path"])).relative_to(self.root).as_posix()}
        current = changed_file_paths(self.projection)
        if len(set(current) | paths) <= limit:
            return
        raise ToolError(
            f"{name} refused: this Attempt may change at most {_files(limit)}, and "
            f"{_files(len(current))} already differ from the Candidate it started from: "
            f"{_path_list(current)}. Change only these files, or write an updated file's "
            "original bytes back to free its slot; a deleted file cannot be restored this way"
        )

    # -- tools ------------------------------------------------------------

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if name == "list_files":
            _expect(arguments, set())
            files = self._all_files()
            return {
                "files": files,
                "file_summaries": [
                    _file_summary(self.root / relative, self.root) for relative in files
                ],
            }, 0
        if name == "inspect_file":
            _expect(arguments, {"path"})
            path = self._readable_path(arguments["path"])
            self._require_existing(path)
            return _inspect_file(path, self.root), 0
        if name == "read_file":
            return self._read_file(arguments)
        if name == "write_file":
            return self._write_file(arguments)
        if name == "replace_text":
            return self._replace_text(arguments)
        if name == "edit_yaml":
            _expect(arguments, {"path", "key_path", "new_text"}, {"old_text"})
            return self._edit_yaml(arguments)
        if name == "delete_file":
            _expect(arguments, {"path"})
            path = self._writable_path(arguments["path"])
            self._require_existing(path)
            if not path.exists():
                raise ToolError("cannot delete missing file")
            size = path.stat().st_size
            path.unlink()
            return {"deleted": path.relative_to(self.root).as_posix()}, size
        if name == "apply_patch":
            _expect(arguments, {"patch"})
            patch = arguments["patch"]
            if not isinstance(patch, str):
                raise ToolError("patch must be a string")
            self._limited(patch)
            return self._apply_patch(patch), len(patch.encode())
        if name == "list_evidence":
            _expect(arguments, set(), optional={"offset"})
            offset = _offset(arguments)
            records = _evidence_page(self.catalog, offset)
            next_offset = offset + len(records)
            return {
                "records": records,
                "next_offset": next_offset if next_offset < len(self.catalog) else None,
                "total": len(self.catalog),
            }, 0
        if name == "read_trace":
            from evolution.tree.roles.reflection_reader import ReaderArgumentError

            assert self.request.traces is not None
            try:
                return self.request.traces.read(arguments), 0
            except ReaderArgumentError as exc:
                detail = f"{exc.code}: {exc.detail}" if self.family15 and exc.detail else exc.code
                raise ToolError(detail) from exc
        if name == "read_evidence":
            _expect(arguments, {"evidence_id"}, optional={"offset"})
            offset = _offset(arguments)
            record = next(
                (item for item in self.catalog if item.evidence_id == arguments["evidence_id"]),
                None,
            )
            if record is None:
                raise ToolError("unknown evidence ID")
            content, following_offset = _evidence_chunk(
                record.content, offset, family15=self.family15
            )
            return {
                "id": record.evidence_id,
                "content": content,
                "next_offset": following_offset,
                "total_chars": len(record.content),
            }, 0
        if name == "record_plan":
            return {"recorded": arguments}, 0
        if name == "finish_edit":
            _expect(arguments, {"summary"})
            if not isinstance(arguments["summary"], str):
                raise ToolError("finish_edit summary must be a string")
            return {"finished": True}, 0
        raise AssertionError(name)  # pragma: no cover - guarded by `_TOOL_NAMES`

    def _read_file(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        return read_file_tool(
            self.root,
            arguments,
            max_payload_bytes=self.section.budgets.max_payload_bytes,
            nearest_paths=effective_nearest_paths(self.section) if self.family15 else None,
        )

    def _write_file(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        _expect(arguments, {"path", "content"})
        path = self._writable_path(arguments["path"])
        content = arguments["content"]
        if not isinstance(content, str):
            raise ToolError("write_file content must be a string")
        if path.exists() and path.stat().st_size > MAX_READ_FILE_CHARS:
            raise ToolError(
                "large existing files require a targeted apply_patch; "
                "do not rewrite the complete file"
            )
        self._limited(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"written": path.relative_to(self.root).as_posix()}, len(content.encode())

    def _replace_text(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        _expect(arguments, {"path", "old_text", "new_text"})
        path = self._writable_path(arguments["path"])
        old_text, new_text = arguments["old_text"], arguments["new_text"]
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolError("replace_text arguments must be strings")
        if not old_text:
            raise ToolError("replace_text old_text must not be empty")
        self._require_existing(path)
        content = _read_text(path)
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ToolError(
                f"old_text occurs {occurrences} times in "
                f"{path.relative_to(self.root).as_posix()}; it must occur exactly once"
            )
        self._limited(new_text)
        path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return {"replaced": path.relative_to(self.root).as_posix()}, len(new_text.encode())

    def _edit_yaml(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        import yaml

        path = self._writable_path(arguments["path"])
        key_path = arguments["key_path"]
        old_text = arguments.get("old_text")
        new_text = arguments["new_text"]
        if not isinstance(key_path, str) or not isinstance(new_text, str):
            raise ToolError("edit_yaml arguments must be strings")
        self._require_existing(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ToolError(f"cannot parse YAML: {exc}") from exc
        target: Any = data
        for key in key_path.split("."):
            if not isinstance(target, dict) or key not in target:
                raise ToolError(f"YAML key path not found: {key_path}")
            target = target[key]
        if not isinstance(target, str):
            raise ToolError("edit_yaml target must be a string scalar")
        if old_text is None or old_text == "__WHOLE_VALUE__":
            updated = new_text
        else:
            if not old_text or target.count(old_text) != 1:
                raise ToolError("old_text must occur exactly once in the YAML value")
            updated = target.replace(old_text, new_text, 1)
        cursor: Any = data
        keys = key_path.split(".")
        for key in keys[:-1]:
            cursor = cursor[key]
        cursor[keys[-1]] = updated
        rendered = yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096
        )
        path.write_text(rendered, encoding="utf-8")
        try:
            yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            raise ToolError(f"generated invalid YAML: {exc}") from exc
        return {
            "edited": path.relative_to(self.root).as_posix(),
            "key_path": key_path,
        }, len(rendered.encode("utf-8"))

    def _patch_targets_large(self, patch: str) -> bool:
        for line in patch.splitlines():
            if not line.startswith("+++ b/"):
                continue
            relative = line[6:].strip()
            try:
                path = self._writable_path(relative)
            except ToolError:
                continue
            if path.exists() and path.stat().st_size > MAX_READ_FILE_CHARS:
                return True
        return False

    def _apply_patch(self, patch: str) -> dict[str, Any]:
        changes = _parse_patch(patch)
        staged: dict[Path, str] = {}
        for relative, hunks in changes.items():
            path = self._writable_path(relative)
            if any(
                line.startswith((" ", "-")) for _start, _removed, lines in hunks for line in lines
            ):
                self._require_existing(path)
            old = _read_text(path) if path.exists() else ""
            try:
                staged[path] = _apply_hunks(old, hunks)
            except ToolError as exc:
                digest = hashlib.sha256(old.encode()).hexdigest()
                raise ToolError(f"{exc} (path={relative}, current_sha256={digest})") from exc
        for path, content in staged.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return {"patched": sorted(path.relative_to(self.root).as_posix() for path in staged)}

    # -- provider ---------------------------------------------------------

    async def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        request_style: str,
        turn: int | None,
    ) -> Any:
        return await provider_request(
            client=self.client,
            section=self.section,
            log=self.log,
            messages=messages,
            tools=tools or TOOLS,
            request_style=request_style,
            turn=turn,
            remaining=self._remaining,
            retry=self.section.retry if self.role_resilience else None,
            deadline=self.deadline if self.role_resilience else None,
            attempts=self.attempts,
            family15=self.family15,
        )


def _parents_to_root(path: Path, root: Path) -> list[Path]:
    parents: list[Path] = []
    while path != root:
        parents.append(path)
        path = path.parent
    return parents


def _expect(
    arguments: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    if (
        not isinstance(arguments, dict)
        or set(arguments) - (required | (optional or set()))
        or not required <= set(arguments)
    ):
        raise ToolError("invalid tool arguments")


def _offset(arguments: dict[str, Any]) -> int:
    offset = arguments.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ToolError("offset must be a non-negative integer")
    return offset


def _request_audit(
    messages: list[dict[str, Any]],
    request_style: str,
    turn: int,
    tools: list[dict[str, Any]],
    *,
    after: int = 0,
) -> dict[str, Any]:
    return {
        "phase": "model_request",
        "turn": turn,
        "request_style": request_style,
        "message_count": len(messages),
        "messages_after": after,
        "messages": messages[after:],
        "tool_names": [tool["function"]["name"] for tool in tools],
    }


def _message_key(message: Mapping[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)


def _shared_prefix(previous: Sequence[str], current: Sequence[str]) -> int:
    """How many leading messages two serialized conversations have in common."""

    shared = 0
    for left, right in zip(previous, current, strict=False):
        if left != right:
            break
        shared += 1
    return shared


def response_telemetry(response: Any) -> tuple[str | None, dict[str, Any] | None]:
    """`(finish_reason, usage)` of the first choice; `None` where the provider omits one."""

    choice = response.choices[0]
    usage = getattr(response, "usage", None)
    if usage is not None and hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    return getattr(choice, "finish_reason", None), usage if isinstance(usage, dict) else None


def _response_audit(
    response: Any, section: ChatEditorSection, request_style: str, turn: int
) -> dict[str, Any]:
    choice = response.choices[0]
    message = choice.message
    calls = _tool_calls(response)
    finish_reason, usage = response_telemetry(response)
    returned_model = _returned_model(response)
    returned_provider = _returned_provider(response)
    pinned = bool(section.provider_only or section.provider_order)
    return {
        "phase": "model_response",
        "turn": turn,
        "request_style": request_style,
        "response_id": getattr(response, "id", None),
        "requested_model": section.model,
        "requested_provider": _requested_provider(section),
        "returned_model": returned_model,
        "returned_provider": returned_provider,
        "model_verified": pinned and returned_model == section.model,
        "provider_verified": pinned and _provider_matches(returned_provider, section),
        "finish_reason": finish_reason,
        "usage": usage,
        "content": getattr(message, "content", None),
        "continuation_fields": [name for name in CONTINUATION_FIELDS if hasattr(message, name)],
        "calls": [
            {
                "id": _call_id(call),
                "name": _call_name(call),
                "arguments": _encoded_call_arguments(call),
                "format": "modern" if hasattr(call, "function") else "legacy",
            }
            for call in calls
        ],
    }


def _preflight_failure(response: Any, calls: list[Any], request_style: str) -> dict[str, Any]:
    message = response.choices[0].message
    return {
        "phase": "preflight_failure",
        "request_style": request_style,
        "content": getattr(message, "content", None),
        "calls": [
            {"name": _call_name(call), "arguments": _encoded_call_arguments(call)} for call in calls
        ],
    }
