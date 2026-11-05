"""One chat tool loop for the roles that read through tools.

`run_tool_loop` is the conversation the chat Reflector ran on its own until
`2.0.0b16`, lifted out unchanged so the family-15 Grouper's portion sessions
run the same turns rather than a second copy. One turn: the deadline, the turn
budget, compaction, the offered tools (those failing too often are withheld),
the delta request record, the retried provider request, the response audit,
then exactly one native call. A response without a call, an unknown tool or
arguments that are not a JSON object get a protocol repair, and more than one
call keeps the first. A terminal call goes to the caller's `terminal`
callback, which ends the loop (`Done`), repairs the protocol (`Repair`) or
returns a semantic refusal in the same conversation without spending the
repair budget (`Correct`). Every other call goes to `run_tool`. A
`ToolError`/`OSError` becomes the tool's error result, and after
`tool_failures` consecutive failures of one tool it is no longer offered.

The provider protocol (`provider_request`, `record_request`, compaction, the
call readers) stays `chat_editor`'s; the preflight stays with the caller,
which passes the negotiated `request_style`. Nothing here knows a role's
tools, its terminal's meaning or the campaign: the Reflector's leak screen,
bounds repair, admission and evidence progress live in its callbacks
(ADR-0045: `roles/` must not import `campaign*`). The editor keeps its own
loop in this release.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evolution.tree.roles.chat_history import tool_exchange, with_dropped_note
from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.editor.chat_editor import (  # noqa: PLC2701 - one tool loop, not two
    EditFailure,
    ToolError,
    _call_arguments,
    _call_id,
    _call_name,
    _compact_messages,
    _protocol_correction,
    _response_audit,
    _response_message,
    _tool_calls,
    _tool_result_messages,
    provider_request,
    record_request,
)
from evolution.tree.roles.role_call import AttemptRecorder, RoleCallRetry, provider_error_detail
from evolution.tree.roles.workspace import AttemptLog

__all__ = [
    "Correct",
    "Done",
    "LoopHooks",
    "LoopLimits",
    "LoopStop",
    "Repair",
    "SessionTimeout",
    "ToolOutput",
    "ToolReads",
    "chat_request_size",
    "cut_answer",
    "elide_to_fit",
    "run_tool_loop",
]

Messages = list[dict[str, Any]]
Tools = list[dict[str, Any]]


@dataclass(frozen=True)
class LoopLimits:
    """The session's limits, built by the caller from its section.

    `max_turns` counts every provider turn the session takes, the caller's
    preflight included (see `first_turn`); `compact_chars` and
    `message_max_chars` are the compaction trigger and the per-message cut.
    """

    max_turns: int
    protocol_repairs: int
    tool_failures: int
    compact_chars: int
    message_max_chars: int


@dataclass(frozen=True)
class Done:
    """The terminal call ends the loop; `run_tool_loop` returns `value`."""

    value: Any


@dataclass(frozen=True)
class Repair:
    """The terminal call is a protocol error: one repair, from the budget."""

    error: str


@dataclass(frozen=True)
class Correct:
    """The terminal call is refused on its meaning: `message` is returned in the
    conversation and the model asked to call the terminal again. It spends a
    turn, never a protocol repair."""

    message: str


@dataclass(frozen=True)
class ToolOutput:
    """A tool result with what it does to the session.

    `block` withdraws the tool from the offered set; `follow_up` is a user
    message appended after the result. `run_tool` may return a plain mapping
    for neither.
    """

    result: dict[str, Any]
    block: bool = False
    follow_up: str | None = None


class LoopStop(Exception):
    """A hook ended the session; `reason` names why. Raised out of the loop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SessionTimeout(Exception):
    """A session's own deadline passed before a request; nothing was sent.

    Raised in place of `EditFailure("provider_error", ...)` when the caller
    opts in with `LoopHooks.session_timeout`, so a bounded session's timeout is
    told apart from a provider failure (which carries `provider_exhausted`).
    """


class ToolReads:
    """Which messages are tool results, kept beside the conversation.

    A legacy-style session returns a result as a plain user message, which
    cannot be told from other user text. The loop registers each result it
    appends here (by object identity, holding the message so the identity
    stays valid), with the tool and the arguments of the call it answers;
    `elide_to_fit(..., reads=reads)` uses it. Nothing is added to the wire
    messages.
    """

    def __init__(self) -> None:
        self._entries: dict[int, tuple[dict[str, Any], str, Any]] = {}

    def add(self, message: dict[str, Any], name: str, arguments: Any) -> None:
        self._entries[id(message)] = (message, name, arguments)

    def get(self, message: Mapping[str, Any]) -> tuple[str, Any] | None:
        entry = self._entries.get(id(message))
        return None if entry is None else (entry[1], entry[2])


@dataclass(frozen=True)
class LoopHooks:
    """Optional session controls; `None` keeps the Reflector's behavior.

    - `fit(messages, tools)` returns the messages to send, or `None` when no
      request within the session's size fits, which raises
      `LoopStop("context_exhausted")` before anything is sent.
    - `inspect(response)` sees each response before its audit is logged; a
      returned `LoopStop` is raised after the audit.
    - `screen(record)` is applied to every record the loop itself writes,
      and runs before the first record that holds a response's text: the
      response audit, and the request deltas that later repeat it, pass
      through it too.
    - `session_timeout` raises `SessionTimeout` instead of the Reflector's
      `EditFailure("provider_error", "<role> deadline reached")`. It also
      covers a request in flight when the deadline passes, retried or not.
    - `reads` receives every tool result the loop appends, for
      `elide_to_fit(..., reads=reads)`.
    """

    fit: Callable[[Messages, Tools], Messages | None] | None = None
    inspect: Callable[[Any], LoopStop | None] | None = None
    screen: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    session_timeout: bool = False
    reads: ToolReads | None = None


class _ScreenedLog:
    """`record_request` writes through this so its deltas pass the screen too."""

    def __init__(self, record: Callable[[dict[str, Any]], None]) -> None:
        self.record = record


def chat_request_size(
    messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> int:
    """Messages as `_compact_messages` counts them, plus the tool list's JSON."""

    return sum(
        len(json.dumps(message, ensure_ascii=False, default=str)) for message in messages
    ) + len(json.dumps(list(tools), ensure_ascii=False))


def _is_elided(message: Mapping[str, Any]) -> bool:
    try:
        content = json.loads(message["content"])
    except (KeyError, TypeError, ValueError):
        return False
    return isinstance(content, dict) and content.get("elided") is True


def _call_reads(messages: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, Any]]:
    """Each tool call id with its tool name and arguments, from the assistant messages."""

    reads: dict[str, tuple[str, Any]] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                pass
            reads[str(call.get("id"))] = (str(function.get("name", "")), arguments)
    return reads


def elide_to_fit(
    messages: Messages, tools: Tools, *, limit: int, reads: ToolReads | None = None
) -> Messages | None:
    """Messages within `limit` characters, eliding the oldest tool results.

    A tool result is replaced by `{"elided": true, "tool": <name>, "read":
    <the call's arguments>}`, oldest first, until `chat_request_size` is at
    most `limit`. A modern result is found by its `tool` role; a legacy-style
    one (a user message) only through `reads`. `messages` is not changed. Returns `None` when every tool
    result is already elided and the request is still over the limit.
    """

    fitted = list(messages)
    if chat_request_size(fitted, tools) <= limit:
        return fitted
    calls = _call_reads(fitted)
    for index, message in enumerate(fitted):
        known = reads.get(message) if reads is not None else None
        if (message.get("role") != "tool" and known is None) or _is_elided(message):
            continue
        name, arguments = known or calls.get(str(message.get("tool_call_id")), ("", None))
        marker = {"elided": True, "tool": name, "read": arguments}
        fitted[index] = {
            **message,
            "content": json.dumps(marker, ensure_ascii=False, separators=(",", ":")),
        }
        if chat_request_size(fitted, tools) <= limit:
            return fitted
    return None


def cut_answer(response: Any) -> LoopStop | None:
    """`LoopStop("cut")` when the answer ended at the output limit.

    A cut answer is not a protocol error: its tool JSON is truncated and a
    repair would only be cut again, so it is checked before the repair path.
    """

    choices = getattr(response, "choices", None) or []
    if choices and getattr(choices[0], "finish_reason", None) == "length":
        return LoopStop("cut")
    return None


Terminal = Callable[[str, dict[str, Any], int], "Done | Repair | Correct"]
RunTool = Callable[[str, dict[str, Any]], Awaitable["Mapping[str, Any] | ToolOutput"]]
Compact = Callable[..., None]


async def run_tool_loop(
    *,
    client: Any,
    section: ChatEditorSection,
    log: AttemptLog,
    recorder: AttemptRecorder | None,
    deadline: float,
    request_style: str,
    limits: LoopLimits,
    messages: Messages,
    tools: Tools,
    terminal_names: Collection[str],
    run_tool: RunTool,
    terminal: Terminal,
    hooks: LoopHooks = LoopHooks(),  # noqa: B008 - frozen and stateless
    role: str,
    retry: RoleCallRetry | None = None,
    first_turn: int = 0,
    compact: Compact | None = _compact_messages,
    family15: bool = False,
) -> object:
    """Run one tool conversation until a terminal call returns `Done`.

    `messages` is the conversation so far (the caller's opening) and is
    extended in place. `deadline` is absolute (`time.monotonic()`); `retry`,
    when set, is the role's `role_call_retry/1` policy and the deadline then
    also bounds its attempts. `first_turn` is the next turn number, after the
    turns the caller's preflight spent. `run_tool` may complete `arguments`
    in place; the `tool_call` record shows them as run. A terminal callback
    writes its own records. Failures raise `EditFailure`
    (`turns_exhausted`, `protocol`, `provider_error`, ...), or a hook's
    `LoopStop`.
    """

    names = frozenset(tool["function"]["name"] for tool in tools)
    turns = first_turn
    sent: list[str] = []
    protocol_repairs = 0
    blocked: set[str] = set()
    failing_tool: str | None = None
    consecutive_failures = 0
    assistant: dict[str, Any] | None = None
    dropped: list[str] = []

    def noted(output: dict[str, Any]) -> dict[str, Any]:
        return with_dropped_note(output, dropped) if family15 else output

    def record(value: dict[str, Any]) -> None:
        log.record(hooks.screen(value) if hooks.screen is not None else value)

    request_log: Any = _ScreenedLog(record) if hooks.screen is not None else log

    def request_remaining() -> float:
        # Inside a request the deadline is always an `EditFailure`: the
        # provider layer would wrap anything else as a provider error. The
        # loop turns it into `SessionTimeout` below.
        left = deadline - time.monotonic()
        if left <= 0:
            raise EditFailure("provider_error", f"{role} deadline reached")
        return left

    def remaining() -> float:
        try:
            return request_remaining()
        except EditFailure:
            if hooks.session_timeout:
                raise SessionTimeout(f"{role} deadline reached") from None
            raise

    def results(call: Any, output: dict[str, Any]) -> Messages:
        appended = _tool_result_messages(call, noted(output), assistant)
        if hooks.reads is not None:
            for message in appended:
                if message is not assistant:
                    hooks.reads.add(message, _call_name(call), _arguments_of(call))
        return appended

    def next_turn() -> int:
        nonlocal turns
        if turns >= limits.max_turns:
            raise EditFailure("turns_exhausted")
        turn = turns
        turns += 1
        return turn

    def request_repair(error: str, calls: list[Any]) -> None:
        nonlocal protocol_repairs
        if protocol_repairs >= limits.protocol_repairs:
            raise EditFailure("protocol", f"{role} protocol repair limit exceeded: {error}")
        protocol_repairs += 1
        if assistant is not None:
            messages.extend(
                tool_exchange(assistant, noted({"error": error})) if calls else [assistant]
            )
        messages.append({"role": "user", "content": _protocol_correction(error, calls, tools)})
        record({"phase": "protocol_repair_requested", "repair": protocol_repairs, "error": error})

    def request_correction(name: str, correction: str, calls: list[Any]) -> None:
        """Return a semantic refusal in the same conversation.

        `correction` is a field path and a bounded category, never a value;
        the protocol-repair budget is untouched, and the model turn that
        produced the call is its only cost.
        """
        if assistant is not None:
            messages.extend(
                tool_exchange(assistant, noted({"error": f"{name} rejected: {correction}"}))
                if calls
                else [assistant]
            )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"{name} was rejected: {correction}. Revise the result and call {name} again."
                ),
            }
        )

    while True:
        remaining()
        turn = next_turn()
        if compact is not None:
            compact(
                messages,
                trigger_chars=limits.compact_chars,
                message_max_chars=limits.message_max_chars,
            )
        offered = [tool for tool in tools if tool["function"]["name"] not in blocked]
        outgoing = messages
        if hooks.fit is not None:
            fitted = hooks.fit(messages, offered)
            if fitted is None:
                raise LoopStop("context_exhausted")
            outgoing = fitted
        sent = record_request(request_log, sent, outgoing, request_style, turn, offered)
        try:
            response = await provider_request(
                client=client,
                section=section,
                log=log,
                messages=outgoing,
                tools=offered,
                request_style=request_style,
                turn=turn,
                remaining=request_remaining,
                role=role,
                retry=retry,
                deadline=deadline if retry is not None else None,
                attempts=recorder,
                family15=family15,
            )
        except EditFailure as exc:
            timed_out = hooks.session_timeout and time.monotonic() >= deadline
            record(
                {
                    "phase": "model_request_failed",
                    "turn": turn,
                    "request_style": request_style,
                    "error": exc.note,
                    **_provider_error_fields(exc),
                }
            )
            if timed_out:
                raise SessionTimeout(f"{role} deadline reached") from exc
            raise
        stop = hooks.inspect(response) if hooks.inspect is not None else None
        if stop is not None:
            record(_response_audit(response, section, request_style, turn))
            raise stop
        assistant = _response_message(response, request_style)
        record(_response_audit(response, section, request_style, turn))
        calls = _tool_calls(response)
        dropped = [_call_name(item) for item in calls[1:]] if family15 else []
        if not calls:
            request_repair("response must contain exactly one native function call", calls)
            continue
        if len(calls) > 1:
            record(
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
        if name not in names:
            request_repair(f"unknown tool: {name}", calls)
            continue
        try:
            # A copy, so a callback completing it never touches the response.
            arguments = dict(_call_arguments(call))
        except ToolError as exc:
            request_repair(str(exc), calls)
            continue
        if name in terminal_names:
            verdict = terminal(name, arguments, turn)
            if isinstance(verdict, Repair):
                request_repair(verdict.error, calls)
                continue
            if isinstance(verdict, Correct):
                request_correction(name, verdict.message, calls)
                continue
            return verdict.value
        try:
            outcome = await run_tool(name, arguments)
        except (ToolError, OSError) as exc:
            if isinstance(exc, ToolError) and str(exc) == "invalid tool arguments":
                request_repair(str(exc), calls)
                continue
            consecutive_failures = consecutive_failures + 1 if failing_tool == name else 1
            failing_tool = name
            guidance = ""
            if consecutive_failures >= limits.tool_failures:
                blocked.add(name)
                guidance = (
                    f" {name} has failed {consecutive_failures} times in a row and is "
                    "no longer available; use another tool."
                )
            record({"phase": "tool_error", "turn": turn, "tool": name, "error": str(exc)})
            messages.extend(results(call, {"error": f"{exc}.{guidance}".rstrip()}))
            continue
        if isinstance(outcome, ToolOutput):
            output, follow_up = outcome.result, outcome.follow_up
            if outcome.block:
                blocked.add(name)
        else:
            output, follow_up = dict(outcome), None
        failing_tool, consecutive_failures = None, 0
        record(
            {
                "phase": "tool_call",
                "turn": turn,
                "tool": name,
                "arguments": arguments,
                "result": output,
            }
        )
        messages.extend(results(call, output))
        if follow_up is not None:
            messages.append({"role": "user", "content": follow_up})


def _arguments_of(call: Any) -> Any:
    try:
        return _call_arguments(call)
    except ToolError:
        return None


def _provider_error_fields(exc: BaseException) -> dict[str, Any]:
    """The conversation-turn record's names for `role_call.provider_error_detail`
    by the provider-detail contract: `http_status` when the failure had a response, and
    `provider_error_code` when that response carried a safe code; never
    message text. The names are this record's own and stay."""

    status, code = provider_error_detail(exc)
    detail: dict[str, Any] = {}
    if status is not None:
        detail["http_status"] = status
        if code is not None:
            detail["provider_error_code"] = code
    return detail
