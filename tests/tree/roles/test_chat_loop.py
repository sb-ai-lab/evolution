"""The shared chat runner's hooks for a bounded session (b16 task B2)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.tree.roles import chat_loop
from evolution.tree.roles.chat_loop import (
    Done,
    LoopHooks,
    LoopLimits,
    LoopStop,
    SessionTimeout,
    ToolOutput,
    ToolReads,
    chat_request_size,
    cut_answer,
    elide_to_fit,
    run_tool_loop,
)
from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.editor.chat_editor import EditFailure
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.workspace import open_attempt_log
from tests.model_grouping_fakes import ScriptedToolClient

SECRET = "sk-very-secret-value"
TOOLS = [
    {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object"}},
    }
    for name in ("read", "finish")
]
LIMITS = LoopLimits(
    max_turns=10, protocol_repairs=2, tool_failures=3, compact_chars=10**9, message_max_chars=10**9
)


def _read(text: str = "") -> dict[str, Any]:
    return {"tool_calls": [("read", {"what": text})]}


def _finish() -> dict[str, Any]:
    return {"tool_calls": [("finish", {})]}


async def _run(
    tmp_path: Path,
    responses: list[dict[str, Any]],
    *,
    hooks: LoopHooks = LoopHooks(),  # noqa: B008
    deadline: float | None = None,
    run_tool: Any = None,
    secrets: tuple[str, ...] = (),
    retry: Any = None,
    request_style: str = "modern_auto",
    family15: bool = False,
    terminal: Any = None,
) -> tuple[Any, ScriptedToolClient, Path]:
    client = ScriptedToolClient(responses)

    async def default_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"text": "x" * 3000}

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    with open_attempt_log(tmp_path / "logs", "a", secrets=secrets) as log:
        result: Any = None
        error: BaseException | None = None
        try:
            result = await run_tool_loop(
                client=client,
                section=ChatEditorSection(model="m", api_key_env="K", base_url="https://gw/v1"),
                log=log,
                recorder=None,
                deadline=deadline if deadline is not None else time.monotonic() + 60,
                request_style=request_style,
                retry=retry
                if retry is not None
                else (RoleCallRetry(3, 30, 0, 1, 0) if family15 else None),
                family15=family15,
                limits=LIMITS,
                messages=messages,
                tools=TOOLS,
                terminal_names={"finish"},
                run_tool=run_tool or default_tool,
                terminal=terminal or (lambda name, arguments, turn: Done("finished")),
                hooks=hooks,
                role="grouper",
            )
        except (LoopStop, SessionTimeout) as exc:
            error = exc
    return (result if error is None else error), client, log.path


@pytest.mark.asyncio
async def test_cut_answer_stops_before_repair(tmp_path: Path) -> None:
    cut = {"tool_calls": [("read", '{"what": "tru')], "finish_reason": "length"}
    outcome, client, _ = await _run(tmp_path, [cut], hooks=LoopHooks(inspect=cut_answer))
    assert isinstance(outcome, LoopStop) and outcome.reason == "cut"
    assert len(client.bodies) == 1  # no repair request followed
    # without the hook the same answer goes through the repair path
    outcome, client, _ = await _run(tmp_path / "b", [cut, _finish()])
    assert outcome == "finished" and len(client.bodies) == 2


@pytest.mark.asyncio
async def test_empty_answer_without_length_is_repaired(tmp_path: Path) -> None:
    empty = {"content": "", "finish_reason": "stop"}
    outcome, client, _ = await _run(
        tmp_path, [empty, _finish()], hooks=LoopHooks(inspect=cut_answer)
    )
    assert outcome == "finished"
    assert "rejected locally" in client.bodies[1]["messages"][-1]["content"]


def test_cut_answer_reads_the_first_choice() -> None:
    cut = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")])
    stop = cut_answer(cut)
    assert stop is not None and stop.reason == "cut"
    assert cut_answer(SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])) is None


@pytest.mark.asyncio
async def test_elision_keeps_request_within_limit(tmp_path: Path) -> None:
    limit = 9000
    hooks = LoopHooks(fit=lambda messages, tools: elide_to_fit(messages, tools, limit=limit))
    responses = [_read(f"part-{i}") for i in range(5)] + [_finish()]
    outcome, client, _ = await _run(tmp_path, responses, hooks=hooks)
    assert outcome == "finished"
    for body in client.bodies:
        assert chat_request_size(body["messages"], body["tools"]) <= limit
    last = [m for m in client.bodies[-1]["messages"] if m["role"] == "tool"]
    markers = [json.loads(m["content"]) for m in last]
    elided = [m for m in markers if m.get("elided")]
    assert elided and all(m["tool"] == "read" and "what" in m["read"] for m in elided)
    assert elided[0]["read"] == {"what": "part-0"}
    assert not markers[-1].get("elided")  # the newest result is read whole


def test_elide_to_fit_returns_none_when_nothing_is_left_to_elide() -> None:
    messages = [{"role": "user", "content": "y" * 500}]
    assert elide_to_fit(messages, TOOLS, limit=100) is None
    assert elide_to_fit(messages, TOOLS, limit=10**6) == messages


@pytest.mark.asyncio
async def test_context_exhausted_sends_nothing(tmp_path: Path) -> None:
    bad = {"content": "z" * 20000, "tool_calls": [("nope", {})]}
    hooks = LoopHooks(fit=lambda messages, tools: elide_to_fit(messages, tools, limit=8000))
    outcome, client, _ = await _run(tmp_path, [bad, _finish()], hooks=hooks)
    assert isinstance(outcome, LoopStop) and outcome.reason == "context_exhausted"
    assert len(client.bodies) == 1  # the repair request was never sent
    assert len(client.responses) == 1


@pytest.mark.asyncio
async def test_session_timeout_is_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(chat_loop, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def slow(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        now[0] = 100.0
        return {}

    outcome, client, _ = await _run(
        tmp_path,
        [_read(), _finish()],
        hooks=LoopHooks(session_timeout=True),
        deadline=50.0,
        run_tool=slow,
    )
    assert isinstance(outcome, SessionTimeout)
    assert len(client.bodies) == 1  # nothing sent once the deadline passed


@pytest.mark.asyncio
async def test_deadline_without_opt_in_stays_a_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(chat_loop, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def slow(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        now[0] = 100.0
        return {}

    with pytest.raises(EditFailure, match="grouper deadline reached"):
        await _run(tmp_path, [_read(), _finish()], deadline=50.0, run_tool=slow)


@pytest.mark.asyncio
async def test_screen_runs_before_log(tmp_path: Path) -> None:
    def screen(record: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(record).replace(SECRET, "[screened]"))

    calls = [("read", {"what": f"look {SECRET}"})]
    responses = [{"content": f"thinking {SECRET}", "tool_calls": calls}, _finish()]

    async def tool(name: str, arguments: dict[str, Any]) -> ToolOutput:
        return ToolOutput({"text": f"seen {SECRET}"})

    # The log is opened with no secrets of its own: only `screen` can keep the
    # value out, so the file proves the order, request deltas included.
    outcome, client, path = await _run(
        tmp_path, responses, hooks=LoopHooks(screen=screen), run_tool=tool
    )
    assert outcome == "finished"
    text = path.read_text()
    assert SECRET not in text
    assert "[screened]" in text


RETRY = RoleCallRetry(
    max_attempts=3, total_budget_seconds=30, initial_seconds=0, multiplier=1, max_seconds=0
)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [RETRY, None], ids=["retry", "no_retry"])
async def test_deadline_during_a_request_is_a_session_timeout(tmp_path: Path, retry: Any) -> None:
    slow = {**_read(), "delay": 1.0}
    outcome, client, _ = await _run(
        tmp_path,
        [slow, _finish()],
        hooks=LoopHooks(session_timeout=True),
        deadline=time.monotonic() + 0.2,
        retry=retry,
    )
    assert isinstance(outcome, SessionTimeout)
    assert len(client.bodies) == 1  # no second attempt after the deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [RETRY, None], ids=["retry", "no_retry"])
async def test_deadline_during_a_request_without_opt_in_is_unchanged(
    tmp_path: Path, retry: Any
) -> None:
    slow = {**_read(), "delay": 1.0}
    with pytest.raises(EditFailure) as caught:
        await _run(tmp_path, [slow, _finish()], deadline=time.monotonic() + 0.2, retry=retry)
    assert caught.value.klass == "provider_error"


@pytest.mark.asyncio
async def test_legacy_style_results_are_elided(tmp_path: Path) -> None:
    limit = 9000
    reads = ToolReads()
    hooks = LoopHooks(
        fit=lambda messages, tools: elide_to_fit(messages, tools, limit=limit, reads=reads),
        reads=reads,
    )
    responses = [{**_read(f"part-{i}"), "legacy": True} for i in range(5)]
    responses.append({**_finish(), "legacy": True})
    outcome, client, _ = await _run(tmp_path, responses, hooks=hooks, request_style="legacy_auto")
    assert outcome == "finished"
    for body in client.bodies:
        assert chat_request_size(body["messages"], []) <= limit
    users = [m["content"] for m in client.bodies[-1]["messages"] if m["role"] == "user"]
    markers = [json.loads(c) for c in users if c.startswith('{"elided"')]
    assert markers and markers[0] == {"elided": True, "tool": "read", "read": {"what": "part-0"}}


@pytest.mark.parametrize("family15", [False, True])
@pytest.mark.parametrize("mode", ["normal", "error", "repair", "correction"])
async def test_dropped_calls_named_in_first_result_only_in_v16(tmp_path, family15, mode):
    from evolution.tree.roles.chat_loop import Correct
    from evolution.tree.roles.editor.chat_editor import ToolError

    first = "unknown" if mode == "repair" else "finish" if mode == "correction" else "read"
    responses = [{"tool_calls": [(first, {}), ("finish", {}), ("read", {})]}, _finish()]
    terminal_calls = 0

    def terminal(name, arguments, turn):
        nonlocal terminal_calls
        terminal_calls += 1
        return (
            Correct("use a different answer")
            if mode == "correction" and terminal_calls == 1
            else Done("finished")
        )

    async def run_tool(name, arguments):
        if mode == "error":
            raise ToolError("file missing")
        return {"content": "read result"}

    result, client, _ = await _run(
        tmp_path, responses, family15=family15, terminal=terminal, run_tool=run_tool
    )
    assert result == "finished"
    outputs = [
        json.loads(m["content"]) for m in client.bodies[1]["messages"] if m["role"] == "tool"
    ]
    assert len(outputs) == 3
    assert outputs[1:] == [
        {"error": "not_executed", "reason": "one_tool_per_turn"},
        {"error": "not_executed", "reason": "one_tool_per_turn"},
    ]
    if family15:
        assert outputs[0]["parallel_calls_not_run"] == {
            "tools": ["finish", "read"],
            "note": "Only the first tool call of a response runs. These calls did not run; send each one again in its own response.",
        }
    else:
        assert "parallel_calls_not_run" not in outputs[0]
