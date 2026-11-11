"""The strict history fake itself: which arguments it calls broken."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import openai
import pytest

from tests.fake_chat_server import completion
from tests.strict_history import (
    REFUSAL_MESSAGE,
    StrictScriptedClient,
    broken_history_calls,
    strict_history,
)

BROKEN = {
    "dsml": '<tool_call name="list_files"><arg>x</arg></tool_call>',
    "truncated": '{"path": "a',
    "empty": "",
    "array": "[]",
    "string": '"x"',
    "null": "null",
    "nan": '{"a": NaN}',
    "infinity": '{"a": Infinity}',
    "trailing_comma": '{"a": 1,}',
}
VALID = ["{}", '{"a": 1}', '{ "a" : [1, 2] }']


def _body(*arguments: str) -> dict:
    calls = [
        {"id": f"call-{i}", "type": "function", "function": {"name": "t", "arguments": text}}
        for i, text in enumerate(arguments)
    ]
    return {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "tool_calls": calls}],
    }


@pytest.mark.parametrize("text", BROKEN.values(), ids=BROKEN.keys())
def test_each_broken_kind_is_broken(text: str) -> None:
    assert broken_history_calls(_body(text)) == ["call-0"]


@pytest.mark.parametrize("text", VALID)
def test_valid_objects_are_not_broken(text: str) -> None:
    assert broken_history_calls(_body(text)) == []


def test_only_assistant_calls_count_and_ids_are_listed() -> None:
    body = _body("{}", "[]", "{}", "null")
    body["messages"].append({"role": "tool", "tool_call_id": "call-1", "content": "[]"})
    assert broken_history_calls(body) == ["call-1", "call-3"]
    assert broken_history_calls({"messages": []}) == []


class _Inner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return "ok"


def test_scripted_client_http_400() -> None:
    inner = _Inner()
    client = StrictScriptedClient(inner, "http_400")
    assert asyncio.run(client.create(**_body("{}"))) == "ok"
    with pytest.raises(openai.BadRequestError) as info:
        asyncio.run(client.create(**_body("{}", "[]")))
    assert info.value.status_code == 400
    assert len(inner.calls) == 1
    assert client.refusals == [["call-1"]]


def test_scripted_client_error_body() -> None:
    client = StrictScriptedClient(_Inner(), "error_body")
    response = asyncio.run(client.create(**_body("null")))
    assert response.choices == []
    assert response.error == {"code": 400, "message": REFUSAL_MESSAGE}


def test_server_adapter_passes_a_clean_history_and_refuses_a_broken_one() -> None:
    normal = completion("fine")
    for mode in ("http_400", "error_body"):
        respond = strict_history(normal, mode=mode)
        assert respond(_body("{}")) is normal
        assert respond(None) is normal
    assert strict_history(normal, mode="http_400")(_body("[]")) == {
        "status": 400,
        "message": REFUSAL_MESSAGE,
    }
    assert strict_history(normal, mode="error_body")(_body("[]")) == {
        "error": {"code": 400, "message": REFUSAL_MESSAGE}
    }
    assert strict_history(lambda request: "called", mode="http_400")(_body("{}")) == "called"


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        strict_history(None, mode="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        StrictScriptedClient(_Inner(), "nope")  # type: ignore[arg-type]
