"""A provider that refuses a request whose history holds a broken tool call.

Real routers (and the providers behind them) parse the `arguments` of every
assistant `tool_calls` entry in the history they are sent, and refuse the whole
request when one is not a JSON object. The ordinary fakes accept anything, so a
role that retains a broken call in its history passes against them and fails in
production. These adapters make a fake strict.

The parser here is deliberately its own small RFC 8259 reader, independent of
`evolution`: it must not share a predicate with the code it checks. Python's
`json.loads` is lenient about `NaN`, `Infinity` and `-Infinity`; they are
refused through `parse_constant`.

* `broken_history_calls(body)` lists the IDs of the broken calls in a request.
* `measures(text)` is a replaced call's `{characters, sha256}` (ADR-0061).
* `holds(value, text)` is whether `text` appears in `value` in any JSON-escaped
  form, so a privacy check on dumped records or bodies can fail.
* `StrictScriptedClient(inner, mode)` wraps a scripted unit client.
* `strict_history(normal, *, mode)` wraps a `tests/fake_chat_server.py` handler.

`mode="http_400"` is an HTTP 400 with `{"error": {"message": ...}}`;
`mode="error_body"` is an HTTP 200 body `{"error": {"code": 400, "message": ...}}`
without `choices`, which `/16` classifies as a router refusal by its inner code
(ADR-0056).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import openai

__all__ = [
    "REFUSAL_MESSAGE",
    "StrictScriptedClient",
    "broken_history_calls",
    "holds",
    "measures",
    "strict_history",
]

REFUSAL_MESSAGE = "function arguments could not be parsed"
Mode = Literal["http_400", "error_body"]


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _is_json_object(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    try:
        value = json.loads(text, parse_constant=_refuse_constant)
    except ValueError:
        return False
    return isinstance(value, dict)


def broken_history_calls(body: Mapping[str, Any]) -> list[str]:
    """IDs of assistant tool calls whose `function.arguments` is not one JSON object."""

    broken: list[str] = []
    for message in body.get("messages") or []:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, Mapping) else None
            arguments = function.get("arguments") if isinstance(function, Mapping) else None
            if not _is_json_object(arguments):
                broken.append(str(call.get("id", "")) if isinstance(call, Mapping) else "")
    return broken


def measures(text: str) -> dict[str, Any]:
    """`{characters, sha256}` of `text`, as a replaced call's record gives them."""

    return {"characters": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _escaped_forms(text: str, depth: int = 3) -> set[str]:
    """`text` and its JSON string escapes, nested up to `depth` times."""

    forms = {text}
    for _ in range(depth):
        forms |= {
            json.dumps(form, ensure_ascii=ascii_only)[1:-1]
            for form in forms
            for ascii_only in (False, True)
        }
    return forms


def holds(value: Any, text: str) -> bool:
    """Whether `text` appears in `value` (a string or a JSON-like value).

    A JSON dump escapes quotes, backslashes and (with `ensure_ascii`) non-ASCII
    characters, and a correction embeds JSON inside a string; a plain
    `text in json.dumps(value)` therefore misses a text with a quote. Every
    escape of `text` up to three levels deep is searched in the value dumped
    both ways.
    """

    dumps = (
        [value]
        if isinstance(value, str)
        else [json.dumps(value, ensure_ascii=a, default=repr) for a in (False, True)]
    )
    return any(form in dump for form in _escaped_forms(text) for dump in dumps)


def _http_400() -> openai.BadRequestError:
    request = httpx.Request("POST", "https://gw.example.test/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"message": REFUSAL_MESSAGE}})
    return openai.BadRequestError(
        REFUSAL_MESSAGE, response=response, body={"error": {"message": REFUSAL_MESSAGE}}
    )


class StrictScriptedClient:
    """Wrap a scripted client: refuse a request whose history has a broken call.

    A refused request never reaches the inner client, so its script is not
    consumed. `refusals` holds the broken IDs of every refused request.
    """

    def __init__(self, inner: Any, mode: Mode) -> None:
        if mode not in ("http_400", "error_body"):
            raise ValueError(f"unknown mode: {mode}")
        self.inner = inner
        self.mode = mode
        self.refusals: list[list[str]] = []
        self.chat = SimpleNamespace(completions=self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def create(self, **kwargs: Any) -> Any:
        broken = broken_history_calls(kwargs)
        if broken:
            self.refusals.append(broken)
            if self.mode == "http_400":
                raise _http_400()
            return SimpleNamespace(
                choices=[], error={"code": 400, "message": REFUSAL_MESSAGE}, model=kwargs["model"]
            )
        return await self.inner.chat.completions.create(**kwargs)


def strict_history(normal: Any, *, mode: Mode) -> Callable[[Any], Any]:
    """Adapt a `fake_chat_server` script item (or callable) to refuse a broken history.

    The result is a callable taking the request JSON, as the server accepts in a
    script. `http_400` answers `{"status": 400, "message": ...}`, which the server
    serves as HTTP 400 with an `error` object; `error_body` answers an HTTP-200
    body with only `error`.
    """

    if mode not in ("http_400", "error_body"):
        raise ValueError(f"unknown mode: {mode}")

    def respond(request: Any) -> Any:
        if isinstance(request, Mapping) and broken_history_calls(request):
            if mode == "http_400":
                return {"status": 400, "message": REFUSAL_MESSAGE}
            return {"error": {"code": 400, "message": REFUSAL_MESSAGE}}
        return normal(request) if callable(normal) else normal

    return respond
