"""Native chat conversations preserve the provider's assistant continuations."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletionMessage

from evolution.tree.roles.chat_history import assistant_message, message_units, tool_exchange
from evolution.tree.roles.editor.chat_editor import EditFailure, _compact_messages
from evolution.tree.roles.reflector_contracts import ProbeAccess, ReflectRequest
from evolution.tree.storage.redaction import safe_record
from tests.tree.roles.editor.test_chat_editor import (
    SECRET,
    _edit_flow,
    _editor,
    _parallel_response,
    _request,
    _response,
    _ws,
)
from tests.tree.roles.reflector.test_chat_reflector import _reflector
from tests.tree.roles.reflector.test_chat_reflector import reflect_request as reflect_request


@dataclass
class _NativeRun:
    result: Any
    client: Any
    log: Path
    workspace: Path
    requests: list[dict[str, Any]]


def _snapshot_requests(client: Any) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    create = client.chat.completions.create

    async def capture(**arguments: Any) -> Any:
        snapshots.append(copy.deepcopy(arguments))
        return await create(**arguments)

    client.chat.completions.create = capture
    return snapshots


@pytest.fixture(params=["editor", "reflector"])
def native_role(request: pytest.FixtureRequest, tmp_path: Path, reflect_request: ReflectRequest):
    async def run(responses: list[Any], **options: Any) -> _NativeRun:
        if request.param == "editor":
            editor, client = _editor(
                tmp_path, [_response("list_files"), *responses, *_edit_flow()[1:]], **options
            )
            snapshots = _snapshot_requests(client)
            workspace = _ws(tmp_path)
            result = await editor.edit(_request(workspace))
            return _NativeRun(result, client, editor.log_root / "a1.jsonl", workspace, snapshots)
        reflector, client = _reflector(
            tmp_path,
            [
                _response("list_files"),
                *responses,
                _response("finish_reflection", '{"groups":[],"declined":true}'),
            ],
            **options,
        )
        snapshots = _snapshot_requests(client)
        result = await reflector.reflect(reflect_request)
        return _NativeRun(
            result,
            client,
            reflector.log_root / f"{reflect_request.reflection_id}.jsonl",
            reflect_request.workspace,
            snapshots,
        )

    return run


def _continued_response() -> SimpleNamespace:
    message = ChatCompletionMessage(
        role="assistant",
        content="Inspect the current skill.",
        tool_calls=[
            {
                "id": "read-1",
                "type": "function",
                "index": 4,
                "function": {"name": "read_file", "arguments": '{"path":"skills/SKILL.md"}'},
            }
        ],
        reasoning="synthetic-continuation-sentinel",
        reasoning_content=None,
        reasoning_details=[
            {"type": "reasoning.encrypted", "data": "synthetic-opaque-sentinel", "index": 0},
            {"type": "reasoning.text", "text": "synthetic-second-block", "index": 1},
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


async def test_native_roles_preserve_the_original_assistant_continuation(native_role) -> None:
    response = _continued_response()
    original = copy.deepcopy(response.choices[0].message.model_dump(exclude_unset=True))
    run = await native_role([response])

    assert run.result.status in {"edited", "declined"}
    assistant = next(
        message
        for message in run.client.calls[-1]["messages"]
        if message.get("tool_calls", [{}])[0].get("id") == "read-1"
    )
    expected = copy.deepcopy(original)
    expected["tool_calls"][0].pop("index")
    assert assistant == expected
    assert response.choices[0].message.model_dump(exclude_unset=True) == original
    log = run.log.read_text()
    assert "synthetic-continuation-sentinel" not in log
    assert "synthetic-opaque-sentinel" not in log
    assert "synthetic-second-block" not in log
    assert any(
        record.get("continuation_fields") == ["reasoning", "reasoning_content", "reasoning_details"]
        for record in map(json.loads, log.splitlines())
    )


async def test_native_roles_close_every_call_without_executing_later_terminals(native_role) -> None:
    response = _parallel_response(
        ("read_file", '{"path":"skills/SKILL.md"}'),
        ("finish_edit", '{"summary":"must not finish"}'),
        ("finish_reflection", '{"groups":[],"declined":true}'),
        ("write_file", '{"path":"skills/SKILL.md","content":"must not write"}'),
    )

    run = await native_role([response])

    assert run.result.status in {"edited", "declined"}
    units = message_units(run.client.calls[-1]["messages"][2:])
    exchange = next(unit for unit in units if len(unit) == 5)
    assert [result["tool_call_id"] for result in exchange[1:]] == [
        "call-0",
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [json.loads(result["content"]) for result in exchange[2:]] == [
        {"error": "not_executed", "reason": "one_tool_per_turn"}
    ] * 3
    assert (run.workspace / "skills/SKILL.md").read_text() != "must not write"
    tool_calls = [
        record
        for record in map(json.loads, run.log.read_text().splitlines())
        if record.get("phase") == "tool_call"
    ]
    assert sum(record["tool"] in {"finish_edit", "finish_reflection"} for record in tool_calls) == 1


@pytest.mark.parametrize(
    "first_call",
    [("unknown_tool", "{}"), ("read_file", "not-json"), ("read_file", "{}")],
)
async def test_native_roles_close_rejected_calls_before_the_protocol_correction(
    native_role, first_call
) -> None:
    run = await native_role(
        [_parallel_response(first_call, ("finish_reflection", '{"groups":[],"declined":true}'))]
    )

    assert run.result.status in {"edited", "declined"}
    messages = run.client.calls[-1]["messages"]
    index = next(
        index
        for index, message in enumerate(messages)
        if message.get("tool_calls", [{}])[0].get("id") == "call-0"
    )
    assert [message["role"] for message in messages[index : index + 4]] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert "error" in json.loads(messages[index + 1]["content"])
    assert json.loads(messages[index + 2]["content"])["error"] == "not_executed"


async def test_native_roles_keep_prose_continuations_before_a_repair(native_role) -> None:
    message = ChatCompletionMessage(
        role="assistant",
        content="A repair is needed.",
        reasoning_content="synthetic-prose-continuation",
    )
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])

    run = await native_role([response])

    assert run.result.status in {"edited", "declined"}
    assert run.client.calls[-1]["messages"][2] == message.model_dump(exclude_unset=True)
    assert run.client.calls[-1]["messages"][3]["role"] == "user"
    assert "synthetic-prose-continuation" not in run.log.read_text()


@pytest.mark.parametrize("invalid_id", [None, "", " ", 3, "duplicate"])
async def test_native_roles_reject_invalid_ids_before_running_tools(
    native_role, invalid_id
) -> None:
    response = _parallel_response(
        ("read_file", '{"path":"skills/SKILL.md"}'),
        ("write_file", '{"path":"skills/SKILL.md","content":"must not write"}'),
    )
    calls = response.choices[0].message.tool_calls
    calls[1].id = calls[0].id if invalid_id == "duplicate" else invalid_id

    run = await native_role([response])

    assert run.result.status == "failed"
    assert run.result.note.startswith("protocol ")
    assert not any(
        record.get("phase") == "tool_call"
        for record in map(json.loads, run.log.read_text().splitlines())
    )


async def test_skipped_probe_does_not_spend_a_probe_call(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    class ProbeSession:
        calls = 0

        async def request(self, value):
            self.calls += 1
            return {"status": 200}

    session = ProbeSession()
    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 2, ("GET",), None),
        probe_session=session,
    )
    reflector, _ = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _parallel_response(
                ("read_file", '{"path":"skills/SKILL.md"}'),
                ("probe_request", '{"method":"GET","path":"/x","body":null}'),
            ),
            _response("finish_reflection", '{"groups":[],"declined":true}'),
        ],
    )

    result = await reflector.reflect(request)

    assert result.status == "declined"
    assert session.calls == 0


def test_compaction_keeps_a_large_latest_exchange_whole_before_a_correction() -> None:
    response = _parallel_response(
        *[("read_file", json.dumps({"path": "x" * 4000})) for _ in range(20)]
    )
    response.choices[0].message.reasoning_details = [
        {"type": "reasoning.encrypted", "data": "opaque" * 2000}
    ]
    latest = tool_exchange(
        assistant_message(response.choices[0].message), {"content": "result" * 3000}
    )
    original = copy.deepcopy(latest)
    previous = tool_exchange(
        assistant_message(_continued_response().choices[0].message), {"content": "old" * 1000}
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "opening"},
        *previous,
        *latest,
        {"role": "user", "content": "correct the last call"},
    ]

    _compact_messages(messages, trigger_chars=2000, message_max_chars=100)

    units = message_units(messages[2:])
    assert original in units
    assert "context_compacted" not in json.dumps(messages)


@pytest.mark.parametrize("defect", ["missing", "wrong_id", "duplicate", "orphan"])
def test_compaction_rejects_incomplete_exchanges_even_below_the_trigger(defect: str) -> None:
    exchange = tool_exchange(
        assistant_message(_continued_response().choices[0].message), {"content": "result"}
    )
    if defect == "missing":
        exchange.pop()
    elif defect == "wrong_id":
        exchange[1]["tool_call_id"] = "other"
    elif defect == "duplicate":
        exchange.append(copy.deepcopy(exchange[-1]))
    else:
        exchange.pop(0)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "opening"},
        *exchange,
    ]

    with pytest.raises(EditFailure, match="protocol"):
        _compact_messages(messages, trigger_chars=128_000, message_max_chars=12_000)


async def test_editor_preserves_original_arguments_when_evidence_progress_inserts_eof(
    tmp_path: Path,
) -> None:
    arguments = '{"evidence_id": "summary"}'
    editor, client = _editor(
        tmp_path,
        [
            _response("list_files"),
            _response("read_evidence", arguments),
            _response("read_evidence", arguments),
            *_edit_flow()[1:],
        ],
    )

    result = await editor.edit(_request(_ws(tmp_path)))

    assert result.status == "edited"
    exchanges = [
        unit
        for unit in message_units(client.calls[-1]["messages"][2:])
        if unit[0].get("tool_calls", [{}])[0].get("function", {}).get("name") == "read_evidence"
    ]
    assert [unit[0]["tool_calls"][0]["function"]["arguments"] for unit in exchanges] == [
        arguments,
        arguments,
    ]
    assert (
        "offset must point to an evidence record boundary"
        in json.loads(exchanges[1][1]["content"])["error"]
    )


async def test_native_request_deltas_reconstruct_the_redacted_visible_conversation(
    native_role,
) -> None:
    response = _continued_response()
    response.choices[
        0
    ].message.content = f"Visible {SECRET} <think>synthetic-hidden-sentinel</think> tail"

    run = await native_role([response])

    records = [json.loads(line) for line in run.log.read_text().splitlines()]
    requests = [record for record in records if record.get("phase") == "model_request"]
    conversation: list[dict[str, Any]] = []
    assert len(requests) == len(run.requests)
    for record, call in zip(requests, run.requests, strict=True):
        conversation = conversation[: record["messages_after"]] + record["messages"]
        assert conversation == safe_record(call["messages"], (SECRET,))
        assert len(conversation) == record["message_count"]
    assert any(record["messages_after"] > 0 for record in requests)
    persisted = run.log.read_text()
    assert all(
        value not in persisted
        for value in (SECRET, "synthetic-hidden-sentinel", "synthetic-opaque-sentinel")
    )


def test_compaction_summarizes_only_visible_content_and_folds_complete_exchanges() -> None:
    response = _continued_response()
    response.choices[
        0
    ].message.content = (
        "visible before <analysis>synthetic-hidden-sentinel</analysis> visible after"
    )
    first = tool_exchange(
        assistant_message(response.choices[0].message), {"content": "first result"}
    )
    later = [
        tool_exchange(
            assistant_message(_response("read_file", "{}").choices[0].message),
            {"content": f"result {index}"},
        )
        for index in range(10)
    ]
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "opening"},
        *first,
        *(message for exchange in later for message in exchange),
    ]

    _compact_messages(messages, trigger_chars=2000, message_max_chars=100)

    summary = messages[2]["content"]
    assert "visible before [REDACTED] visible after" in summary
    assert "first result" in summary
    assert all(
        value not in summary
        for value in (
            "synthetic-hidden-sentinel",
            "synthetic-opaque-sentinel",
            "synthetic-continuation-sentinel",
        )
    )
    assert all(
        unit[0].get("tool_calls", [{}])[0].get("id") != "read-1"
        for unit in message_units(messages[2:])
    )


@pytest.mark.parametrize("defect", ["unsupported_type", "arguments_object", "opaque_object"])
async def test_native_roles_reject_an_invalid_envelope_before_any_tool_side_effect(
    native_role, defect
) -> None:
    response = _parallel_response(("read_file", '{"path":"skills/SKILL.md"}'))
    message = response.choices[0].message
    if defect == "unsupported_type":
        message.tool_calls[0].type = "custom"
    elif defect == "arguments_object":
        message.tool_calls[0].function.arguments = {"path": "skills/SKILL.md"}
    else:
        message.reasoning_details = [object()]

    run = await native_role([response])

    assert run.result.status == "failed"
    assert run.result.note.startswith("protocol ")
    assert not any(
        record.get("phase") == "tool_call"
        for record in map(json.loads, run.log.read_text().splitlines())
    )


async def test_native_roles_send_complete_exchanges_after_compaction(native_role) -> None:
    response = _parallel_response(
        ("read_file", '{"path":"skills/SKILL.md"}'),
        ("write_file", '{"path":"skills/SKILL.md","content":"must not write"}'),
    )
    response.choices[0].message.reasoning_details = [
        {"type": "reasoning.encrypted", "data": "opaque" * 2000}
    ]
    expected = assistant_message(response.choices[0].message)

    run = await native_role([response], context_compact_chars=2000, context_message_max_chars=100)

    assert run.result.status in {"edited", "declined"}
    following = run.requests[2]["messages"]
    assert following[2]["content"].startswith("Earlier editor context was compacted")
    assert next(unit for unit in message_units(following[2:]) if len(unit) == 3)[0] == expected
    for request in run.requests[1:]:
        message_units(request["messages"][2:])


def test_compaction_keeps_older_assistants_unchanged_while_shortening_their_results() -> None:
    response = _response("read_file", json.dumps({"path": "x" * 5000}))
    response.choices[0].message.reasoning_details = [
        {"type": "reasoning.encrypted", "data": "opaque" * 400}
    ]
    previous = tool_exchange(
        assistant_message(response.choices[0].message), {"content": "x" * 50_000}
    )
    original = copy.deepcopy(previous[0])
    latest = tool_exchange(
        assistant_message(_response("list_files").choices[0].message), {"files": []}
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "opening"},
        *previous,
        *latest,
    ]

    _compact_messages(messages, trigger_chars=30_000, message_max_chars=100)

    units = message_units(messages[3:])
    assert units[0][0] == original
    assert units[0][1]["content"].endswith("[context truncated; re-read the needed range]")
    assert units[1] == latest


@pytest.mark.parametrize("role", ["editor", "reflector"])
async def test_a_valid_first_terminal_does_not_request_another_provider_turn(
    tmp_path: Path, reflect_request: ReflectRequest, role: str
) -> None:
    later = ("write_file", '{"path":"skills/SKILL.md","content":"must not write"}')
    if role == "editor":
        terminal = _parallel_response(("finish_edit", '{"summary":"done"}'), later)
        actor, client = _editor(tmp_path, [*_edit_flow()[:-1], terminal])
        result = await actor.edit(_request(_ws(tmp_path)))
        expected_calls = 4
        log = actor.log_root / "a1.jsonl"
    else:
        terminal = _parallel_response(("finish_reflection", '{"groups":[],"declined":true}'), later)
        actor, client = _reflector(tmp_path, [_response("list_files"), terminal])
        result = await actor.reflect(reflect_request)
        expected_calls = 2
        log = actor.log_root / f"{reflect_request.reflection_id}.jsonl"

    assert result.status in {"edited", "declined"}
    assert len(client.calls) == expected_calls
    dropped = next(
        record
        for record in map(json.loads, log.read_text().splitlines())
        if record.get("phase") == "parallel_calls_dropped"
    )
    assert dropped["calls"][0]["id"] == "call-1"


@pytest.mark.parametrize("later_call", [("unknown_tool", "{}"), ("read_file", "{broken")])
async def test_skipped_call_semantics_do_not_block_first_call(native_role, later_call) -> None:
    response = _parallel_response(("read_file", '{"path":"skills/SKILL.md"}'), later_call)

    run = await native_role([response])

    exchange = next(
        unit for unit in message_units(run.client.calls[-1]["messages"][2:]) if len(unit) == 3
    )
    assert run.result.status in {"edited", "declined"}
    assert "error" not in json.loads(exchange[1]["content"])
    assert json.loads(exchange[2]["content"]) == {
        "error": "not_executed",
        "reason": "one_tool_per_turn",
    }


@pytest.mark.parametrize("calls", [None, []])
async def test_no_call_repair_omits_empty_tool_calls(native_role, calls) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=ChatCompletionMessage(
                    role="assistant",
                    content="Please continue.",
                    tool_calls=calls,
                    reasoning_content="opaque-no-call",
                )
            )
        ]
    )
    run = await native_role([response])
    assert run.result.status in {"edited", "declined"}
    assistant = run.requests[2]["messages"][-2]
    assert assistant == {
        "role": "assistant",
        "content": "Please continue.",
        "reasoning_content": "opaque-no-call",
    }


@pytest.mark.parametrize("content", [None, ""])
async def test_empty_assistant_is_omitted_before_bounded_repair(native_role, content) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=ChatCompletionMessage(
                    role="assistant",
                    content=content,
                    tool_calls=[],
                )
            )
        ]
    )
    run = await native_role([response])
    assert run.result.status in {"edited", "declined"}
    messages = run.requests[2]["messages"]
    assert messages[-1]["role"] == "user"
    assert not any(message.get("role") == "assistant" for message in messages[2:])


async def test_reasoning_only_assistant_is_not_silently_discarded(native_role) -> None:
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=ChatCompletionMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[],
                    reasoning_content="opaque-only",
                )
            )
        ]
    )
    run = await native_role([response])
    assert run.result.status in {"edited", "declined"}
    assert run.requests[2]["messages"][-2] == {
        "role": "assistant",
        "content": None,
        "reasoning_content": "opaque-only",
    }
