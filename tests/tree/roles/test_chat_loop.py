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
from tests.strict_history import holds
from tests.strict_history import measures as _measures

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
    client: Any = None,
    limits: LoopLimits = LIMITS,
) -> tuple[Any, Any, Path]:
    client = client if client is not None else ScriptedToolClient(responses)

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
                limits=limits,
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


# --------------------------------------------------------------------------
# `/16`: a broken tool call is retained as `{}` (ADR-0061, b18 Part A)
# --------------------------------------------------------------------------

#: The `/16` correction's fixed sentence (b18 plan, Builder decision 3).
REPLACED_SENTENCE = (
    "The arguments of each call listed without a preview were not one JSON object "
    "and were removed from the conversation history."
)
_KIND_ERRORS = {
    "dsml": "tool arguments are not JSON",
    "truncated": "tool arguments are not JSON",
    "empty": "tool arguments are not JSON",
    "array": "tool arguments must be an object",
    "string": "tool arguments must be an object",
    "null": "tool arguments must be an object",
    "nan": "tool arguments are not JSON",
    "unknown_tool_broken": "unknown tool: no_such_tool",
}
_DSML = (
    '<｜DSML｜function_calls><｜DSML｜invoke name="list_files">'
    "</｜DSML｜invoke></｜DSML｜function_calls>"
)


def _raw_turn(*calls: tuple[str, str]) -> Any:
    """A provider response whose calls carry `arguments` exactly as given."""

    tool_calls = [
        SimpleNamespace(
            id="call" if len(calls) == 1 else f"call-{index}",
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for index, (name, arguments) in enumerate(calls)
    ]
    message = SimpleNamespace(
        content="thinking aloud",
        tool_calls=tool_calls,
        reasoning_content="chain",
        reasoning_details=[{"type": "reasoning.encrypted", "data": "opaque=="}],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _raw_finish() -> Any:
    return _raw_turn(("finish", "{}"))


def _loop_turn(kind: str) -> Any:
    """The A0 golden kind on this loop's tools (`read`, `finish`)."""

    from tests.tree.roles.test_chat_loop_broken_calls_golden import BROKEN

    if kind == "skipped_broken":
        return _raw_turn(("read", "{}"), ("read", '{"path": "a'))
    name, arguments = BROKEN[kind]
    return _raw_turn(("no_such_tool" if kind == "unknown_tool_broken" else "read", arguments))


def _capturing(script: list[Any]) -> Any:
    from tests.tree.roles.test_chat_loop_reflector_golden import _CapturingClient

    return _CapturingClient(script)


def _log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _after_response(log: list[dict[str, Any]], phase: str) -> list[tuple[dict, dict]]:
    """Each `phase` record with the `model_response` record it follows."""

    pairs = []
    response: dict[str, Any] | None = None
    for entry in log:
        if entry["phase"] == "model_response":
            response = entry
        elif entry["phase"] == phase:
            assert response is not None
            pairs.append((entry, response))
    return pairs


def _linked(replaced: list[dict[str, Any]], response: dict[str, Any]) -> None:
    by_id = {call["id"]: call for call in response["calls"]}
    for entry in replaced:
        original = by_id[entry["id"]]
        assert entry["name"] == original["name"]
        assert {key: entry[key] for key in ("characters", "sha256")} == _measures(
            original["arguments"]
        )


def test_v16_loop_kinds_are_the_v15_golden_kinds() -> None:
    from tests.tree.roles.test_chat_loop_broken_calls_golden import BROKEN, KINDS

    assert set(_KIND_ERRORS) == set(BROKEN)
    assert [*_KIND_ERRORS, "skipped_broken"] == KINDS


@pytest.mark.parametrize(
    "kind",
    [
        "dsml",
        "truncated",
        "empty",
        "array",
        "string",
        "null",
        "nan",
        "unknown_tool_broken",
        "skipped_broken",
    ],
)
async def test_v16_shared_loop_retains_broken_call_as_empty_object(
    tmp_path: Path, kind: str
) -> None:
    from evolution.tree.roles.chat_history import assistant_message
    from tests.strict_history import broken_history_calls

    broken = _loop_turn(kind)
    provider = broken.choices[0].message
    expected = assistant_message(provider)
    broken_ids = {"call-1"} if kind == "skipped_broken" else {"call"}
    for call in expected["tool_calls"]:
        if call["id"] in broken_ids:
            call["function"]["arguments"] = "{}"
    client = _capturing([broken, _raw_finish()])

    async def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": "read result"}

    result, _, path = await _run(tmp_path, [], family15=True, run_tool=run_tool, client=client)

    assert result == "finished"
    before, after = client.bodies[0]["messages"], client.bodies[1]["messages"]
    assert after[: len(before)] == before
    retained = after[len(before)]
    assert retained == expected
    assert json.dumps(retained, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert [call["id"] for call in retained["tool_calls"]] == [
        call["id"] for call in expected["tool_calls"]
    ]
    for key in ("content", "reasoning_content", "reasoning_details"):
        assert retained[key] == getattr(provider, key)
    rest = after[len(before) + 1 :]
    log = _log(path)
    if kind == "skipped_broken":
        assert [m["tool_call_id"] for m in rest] == ["call-0", "call-1"]
        first = json.loads(rest[0]["content"])
        assert first["content"] == "read result"
        assert first["parallel_calls_not_run"]["tools"] == ["read"]
        assert json.loads(rest[1]["content"]) == {
            "error": "not_executed",
            "reason": "one_tool_per_turn",
        }
        assert not _after_response(log, "protocol_repair_requested")
        carriers = [
            *_after_response(log, "parallel_calls_dropped"),
            *_after_response(log, "tool_call"),
        ]
        assert len(carriers) == 2
        for entry, response in carriers:
            assert entry["replaced_calls"] == [
                {"id": "call-1", "name": "read", **_measures('{"path": "a')}
            ]
            _linked(entry["replaced_calls"], response)
    else:
        assert len(rest) == 2
        assert rest[0]["tool_call_id"] == "call"
        assert json.loads(rest[0]["content"]) == {"error": _KIND_ERRORS[kind]}
        assert rest[1]["role"] == "user"
        correction = rest[1]["content"]
        assert correction.count(REPLACED_SENTENCE) == 1
        details = json.loads(correction.split("Correction details: ", 1)[1])
        name, arguments = (
            provider.tool_calls[0].function.name,
            provider.tool_calls[0].function.arguments,
        )
        assert details["error"] == _KIND_ERRORS[kind]
        assert details["received_calls"] == [{"name": name, "arguments": _measures(arguments)}]
        ((repair, response),) = _after_response(log, "protocol_repair_requested")
        assert repair["replaced_calls"] == [{"id": "call", "name": name, **_measures(arguments)}]
        _linked(repair["replaced_calls"], response)
        assert not _after_response(log, "tool_call")
    for body in client.bodies:
        assert broken_history_calls(body) == []


async def test_v16_shared_loop_nan_and_empty_never_dispatched(tmp_path: Path) -> None:
    nan = '{"what": NaN}'
    script = [
        _raw_turn(("read", nan)),
        _raw_turn(("read", "")),
        _raw_turn(("finish", '{"x": Infinity}')),
        _raw_finish(),
    ]
    dispatched: list[tuple[str, dict[str, Any]]] = []
    terminals: list[dict[str, Any]] = []

    async def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched.append((name, arguments))
        return {"content": "read result"}

    def terminal(name: str, arguments: dict[str, Any], turn: int) -> Any:
        terminals.append(arguments)
        return Done("finished")

    result, client, path = await _run(
        tmp_path,
        [],
        family15=True,
        run_tool=run_tool,
        terminal=terminal,
        client=_capturing(script),
        limits=LoopLimits(
            max_turns=10,
            protocol_repairs=3,
            tool_failures=3,
            compact_chars=10**9,
            message_max_chars=10**9,
        ),
    )

    assert result == "finished"
    assert dispatched == []
    assert terminals == [{}]
    log = _log(path)
    assert [e["error"] for e in log if e["phase"] == "protocol_repair_requested"] == [
        "tool arguments are not JSON"
    ] * 3
    assert not [e for e in log if e["phase"] in ("tool_call", "tool_error")]
    for body in client.bodies:
        assert "NaN" not in json.dumps(body) and "Infinity" not in json.dumps(body)

    # `/15` keeps `json.loads`, which admits `NaN` inside an object.
    dispatched.clear()
    result, _, _ = await _run(
        tmp_path / "v15",
        [],
        run_tool=run_tool,
        client=_capturing([_raw_turn(("read", nan)), _raw_finish()]),
    )
    assert result == "finished"
    assert len(dispatched) == 1 and dispatched[0][0] == "read"


@pytest.mark.parametrize("mode", ["unknown", "repair", "tool_error", "correction"])
async def test_v16_replaced_calls_carriers_with_a_valid_first_call(
    tmp_path: Path, mode: str
) -> None:
    """Open question 3 and ruling R5: a broken skipped call behind a valid first one.

    `parallel_calls_dropped` carries the replacement in every such turn; a
    repair of the same turn carries it too, after it, in today's record order.
    """

    from evolution.tree.roles.chat_loop import Correct, Repair
    from evolution.tree.roles.editor.chat_editor import ToolError

    first = {"unknown": "nope", "repair": "finish", "correction": "finish"}.get(mode, "read")
    script = [_raw_turn((first, '{ "a" : 1 }'), ("read", _DSML)), _raw_finish()]
    verdicts = {"repair": Repair("finish needs more"), "correction": Correct("groups[0]: empty")}
    terminal_calls = 0

    def terminal(name: str, arguments: dict[str, Any], turn: int) -> Any:
        nonlocal terminal_calls
        terminal_calls += 1
        return verdicts[mode] if terminal_calls == 1 and mode in verdicts else Done("finished")

    async def run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ToolError("file missing")

    client = _capturing(script)
    result, _, path = await _run(
        tmp_path, [], family15=True, terminal=terminal, run_tool=run_tool, client=client
    )

    assert result == "finished"
    log = _log(path)
    expected = [{"id": "call-1", "name": "read", **_measures(_DSML)}]
    turn = [e["phase"] for e in log if e["phase"] not in ("model_request", "provider_attempt")]
    start = turn.index("model_response")
    first_turn = turn[start : turn.index("model_response", start + 1)]
    ((dropped, response),) = _after_response(log, "parallel_calls_dropped")
    assert dropped["replaced_calls"] == expected
    _linked(dropped["replaced_calls"], response)
    assert response["calls"][1]["arguments"] == _DSML
    repairs = _after_response(log, "protocol_repair_requested")
    if mode in ("unknown", "repair"):
        assert first_turn == [
            "model_response",
            "parallel_calls_dropped",
            "protocol_repair_requested",
        ]
        ((repair, repaired),) = repairs
        assert repaired is response
        assert repair["replaced_calls"] == expected
        correction = client.bodies[1]["messages"][-1]["content"]
        assert correction.count(REPLACED_SENTENCE) == 1
        received = json.loads(correction.split("Correction details: ", 1)[1])["received_calls"]
        assert received[1] == {"name": "read", "arguments": _measures(_DSML)}
    else:
        assert not repairs
        assert first_turn == [
            "model_response",
            "parallel_calls_dropped",
            *(["tool_error"] if mode == "tool_error" else []),
        ]
        assert all("replaced_calls" not in e for e in log if e["phase"] == "tool_error")
    retained = client.bodies[1]["messages"][len(client.bodies[0]["messages"])]
    assert [call["function"]["arguments"] for call in retained["tool_calls"]] == [
        '{ "a" : 1 }',
        "{}",
    ]
    assert holds(response, _DSML)  # the check below can see the text
    for entry in log:
        if entry["phase"] != "model_response":
            assert not holds(entry, _DSML) and not holds(entry, "DSML"), entry["phase"]
    for body in client.bodies:
        assert not holds(body, _DSML) and not holds(body, "DSML")


async def test_v15_shared_loop_keeps_broken_arguments_and_records_no_replacement(
    tmp_path: Path,
) -> None:
    """`/15` retains the original string, keeps the preview and records nothing new."""

    client = _capturing([_raw_turn(("read", '{"path": "a')), _raw_finish()])
    result, _, path = await _run(tmp_path, [], client=client)
    assert result == "finished"
    retained = client.bodies[1]["messages"][len(client.bodies[0]["messages"])]
    assert retained["tool_calls"][0]["function"]["arguments"] == '{"path": "a'
    correction = client.bodies[1]["messages"][-1]["content"]
    assert REPLACED_SENTENCE not in correction and "preview" in correction
    assert "replaced_calls" not in path.read_text(encoding="utf-8")
