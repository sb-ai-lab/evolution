"""Offline fakes for the grouping-model role (model error grouping spec §7).

Shared by the runtime tests and the campaign wiring tests, so both drive the
same `client_factory` seam `run_model_grouping` exposes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, TypeVar

import httpx
import openai

__all__ = [
    "CHAT_GROUPER_KEY",
    "CHAT_GROUPER_KEY_ENV",
    "PortionGrouper",
    "ScriptedClient",
    "ScriptedReply",
    "ScriptedToolClient",
    "answer_group",
    "bad_request",
    "chat_grouper_section",
    "failing_members",
    "group_everything",
    "grouping_answer",
    "is_repair",
    "map_group",
    "rate_limited",
    "server_error",
    "use_chat_grouper",
]


class ScriptedReply:
    """A scripted answer that also sets the response's `finish_reason` and `usage`."""

    def __init__(
        self, text: str, *, finish_reason: str | None = None, usage: Mapping[str, Any] | None = None
    ) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.usage = usage


class ScriptedClient:
    """`client.chat.completions.create(**kw)` pops the next scripted item.

    An item is an exception instance (raised), a text (returned as the
    message content), or a callable taking the request keywords and returning
    either -- possibly as an awaitable, so a test can delay one response.
    """

    def __init__(self, script: Iterable[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw: Any) -> Any:
        self.requests.append(kw)
        item = self.script.pop(0)
        if callable(item) and not isinstance(item, BaseException):
            item = item(kw)
            if inspect.isawaitable(item):
                item = await item
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ScriptedReply):
            choice = SimpleNamespace(
                message=SimpleNamespace(content=item.text), finish_reason=item.finish_reason
            )
            extra = {"usage": dict(item.usage)} if item.usage is not None else {}
            return SimpleNamespace(choices=[choice], **extra)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))])

    async def close(self) -> None:
        self.closed = True

    def factory(self) -> Callable[..., ScriptedClient]:
        return lambda _section, _credentials: self


class ScriptedToolClient:
    """A tool-calling client: each response is `{content, tool_calls, finish_reason, usage, delay}`.

    `tool_calls` is a list of `(name, arguments)`; `arguments` is a string sent
    as is (so a test can truncate the JSON) or a mapping encoded as JSON. Each
    request is kept, copied through JSON, in `bodies`.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw: Any) -> Any:
        self.bodies.append(json.loads(json.dumps(kw, ensure_ascii=False, default=repr)))
        item = self.responses.pop(0)
        if item.get("delay"):
            await asyncio.sleep(item["delay"])
        calls = [
            SimpleNamespace(
                id=f"call-{len(self.bodies)}-{index}",
                type="function",
                function=SimpleNamespace(
                    name=name, arguments=args if isinstance(args, str) else json.dumps(args)
                ),
            )
            for index, (name, args) in enumerate(item.get("tool_calls", ()))
        ]
        message = SimpleNamespace(content=item.get("content"), tool_calls=calls or None)
        if item.get("legacy") and calls:
            # A legacy `function_call`: no `tool_calls`, and a call without `.function`.
            message = SimpleNamespace(
                content=item.get("content"),
                tool_calls=None,
                function_call=SimpleNamespace(
                    name=calls[0].function.name, arguments=calls[0].function.arguments
                ),
            )
        extra = {"usage": dict(item["usage"])} if item.get("usage") is not None else {}
        choice = SimpleNamespace(message=message, finish_reason=item.get("finish_reason"))
        return SimpleNamespace(choices=[choice], model=kw["model"], **extra)


def _status_error(
    cls: type[openai.APIStatusError], status: int, message: str = "provider"
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")
    return cls(message, response=httpx.Response(status, request=request), body=None)


def rate_limited(message: str = "provider") -> openai.APIStatusError:
    return _status_error(openai.RateLimitError, 429, message)


def bad_request(message: str = "provider") -> openai.APIStatusError:
    return _status_error(openai.BadRequestError, 400, message)


def server_error(message: str = "provider") -> openai.APIStatusError:
    return _status_error(openai.InternalServerError, 503, message)


def map_group(
    local: str,
    members: Sequence[tuple[str, str]],
    *,
    title: str = "t",
    **extra: Any,
) -> dict[str, Any]:
    """One map group in the Task 6 schema; cases derive from `case#repeat` IDs."""

    body: dict[str, Any] = {
        "id": local,
        "title": title,
        "failure_mode": "fm",
        "expected_behaviour": "eb",
        "question": "q?",
        "members": [{"evidence_id": m, "variant": v} for m, v in members],
        "affected_cases": sorted({m.split("#")[0] for m, _ in members}),
        "citations": ["verdict:c1"],
        "priority": "p",
    }
    body.update(extra)
    return body


def grouping_answer(*groups: dict[str, Any]) -> str:
    return json.dumps({"groups": list(groups)})


def failing_members(request: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`(evidence_id, case_id)` of every failing row a map request lists, in order.

    Real campaign evidence IDs are run-derived, so a campaign-level script
    answers from the request it is sent rather than from IDs fixed in advance.
    `request` is the create keywords (or the chat server's request JSON).
    """

    from evolution.tree.roles.grouping.model_grouping_runtime import MAP_LEAD

    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    assert user.startswith(MAP_LEAD), "not a map request"
    members: list[tuple[str, str]] = []
    for line in user[len(MAP_LEAD) :].split("\n"):
        bundle = json.loads(line)
        members.extend(
            (row["evidence_id"], bundle["case_id"])
            for row in bundle["rows"]
            if row["state"] == "failing"
        )
    return members


def answer_group(
    members: Sequence[tuple[str, str]],
    *,
    local: str = "g1",
    title: str = "t",
    failure_mode: str = "fm",
    variant: str = "v",
    **extra: Any,
) -> dict[str, Any]:
    """One map group over `(evidence_id, case_id)` members, cases taken as given;
    `extra` overrides any other key (`question`, `priority`, `citations`)."""

    body = {
        "id": local,
        "title": title,
        "failure_mode": failure_mode,
        "expected_behaviour": "eb",
        "question": "q?",
        "members": [{"evidence_id": m, "variant": variant} for m, _ in members],
        "affected_cases": sorted({case for _, case in members}),
        "citations": ["verdict:c1"],
        "priority": "p",
    }
    body.update(extra)
    return body


def group_everything(
    *, limit: int | None = None, **header: Any
) -> Callable[[Mapping[str, Any]], str]:
    """A script item answering a map request with one group over its failing rows.

    `limit` keeps only the first `limit` members (a partial answer that leaves
    the rest uncovered); `header` overrides `answer_group`'s header fields.
    """

    def answer(request: Mapping[str, Any]) -> str:
        members = failing_members(request)
        return grouping_answer(answer_group(members[:limit], **header))

    return answer


#: The variable a test `/15` `chat` Grouper section names for its key, and the
#: key a test sets in it. Both are test values; no provider ever sees them.
CHAT_GROUPER_KEY_ENV = "H8_CHAT_GROUPER_KEY"
CHAT_GROUPER_KEY = "sk-h8-chat-grouper-0123456789"


def chat_grouper_section(**overrides: Any) -> dict[str, Any]:
    """A complete `/15` `grouper.kind: chat` section with an instant retry.

    Every key a `/15` `chat` Grouper needs (family-14 design R12):
    `retry.version: role_call_retry/1`, `backend`, `concurrency`,
    `max_input_chars` and `max_output_tokens`, plus the key variable. No
    `fallback` is declared, so a fallback groups as singletons. `overrides`
    replaces top-level keys.
    """

    section: dict[str, Any] = {
        "kind": "chat",
        "backend": "openai_chat",
        "model": "scripted-grouping-model",
        "base_url": "https://provider.example/v1",
        "api_key_env": CHAT_GROUPER_KEY_ENV,
        "timeout_seconds": 60,
        "concurrency": 1,
        "max_input_chars": 200000,
        "max_output_tokens": 8192,
        "retry": {
            "version": "role_call_retry/1",
            "max_attempts": 3,
            "total_budget_seconds": 30,
            "backoff": {"initial_seconds": 0, "multiplier": 1, "max_seconds": 0, "jitter": "full"},
        },
    }
    section.update(overrides)
    return section


_Client = TypeVar("_Client", "ScriptedClient", "PortionGrouper")


def use_chat_grouper(monkeypatch: Any, client: _Client) -> _Client:
    """Make `client` every `chat` grouping's provider and set the Grouper's key.

    Genuine runs bind no `client_factory`, so `run_model_grouping` falls back
    to the module-level `new_grouping_client`, which this replaces for the test.
    """

    from evolution.tree.roles.grouping import model_grouping_runtime

    monkeypatch.setenv(CHAT_GROUPER_KEY_ENV, CHAT_GROUPER_KEY)
    monkeypatch.setattr(model_grouping_runtime, "new_grouping_client", client.factory())
    return client


class PortionGrouper:
    """A scripted `model_grouping/2` chat Grouper for campaign tests (b16).

    It answers by request kind: the preflight (only `list_case` offered) with
    a `list_case` call, a portion session turn with `finish_portion`, and a
    consolidation batch (no tools) with no merge. A portion's answer is one
    group over every unit of the portion (``limit`` keeps the first ``limit``
    units, leaving the rest uncovered), with ``header`` overriding the group
    texts; ``invalid`` answers every portion with an unknown reference, a
    defect no protocol repair fixes. Requests are kept, as `ScriptedClient`
    keeps them, in `requests`. ``budget`` bounds them as a finite
    `ScriptedClient` script does: a request past it is a provider failure, so
    an unexpected extra grouping suspends the run instead of being answered.
    """

    def __init__(
        self,
        *,
        invalid: bool = False,
        limit: int | None = None,
        budget: int | None = None,
        **header: Any,
    ) -> None:
        self.invalid = invalid
        self.limit = limit
        self.budget = budget
        self.header = header
        self.requests: list[dict[str, Any]] = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def factory(self) -> Callable[..., PortionGrouper]:
        return lambda _section, _credentials: self

    async def close(self) -> None:
        self.closed = True

    def _answer(self, kw: Mapping[str, Any]) -> tuple[str | None, list[tuple[str, Any]]]:
        from evolution.tree.roles.grouping.portion_session import PORTION_LEAD

        tools = [tool["function"]["name"] for tool in kw.get("tools") or ()]
        tools += [function["name"] for function in kw.get("functions") or ()]
        if not tools:
            return json.dumps({"merges": []}), []
        if tools == ["list_case"]:
            return None, [("list_case", {"case": "c1"})]
        user = next(m["content"] for m in kw["messages"] if m["role"] == "user")
        portion = json.loads(user[len(PORTION_LEAD) :])
        refs = [unit["ref"] for case in portion["cases"] for unit in case["units"]]
        if self.invalid:
            refs = ["o0"]
        group = {
            "id": "g1",
            "title": "t",
            "failure_mode": "fm",
            "expected_behaviour": "eb",
            "question": "q?",
            "members": [{"ref": ref, "variant": "v"} for ref in refs[: self.limit]],
            "citations": [],
            "priority": "p",
        }
        group.update(self.header)
        return None, [("finish_portion", {"groups": [group]})]

    async def _create(self, **kw: Any) -> Any:
        self.requests.append(kw)
        if self.budget is not None and len(self.requests) > self.budget:
            # A provider failure, so the run suspends `grouper_provider_failed`
            # rather than ending as a content failure an assertion might expect.
            raise server_error("PortionGrouper: request past its budget")
        content, calls = self._answer(kw)
        tool_calls = [
            SimpleNamespace(
                id=f"call-{len(self.requests)}-{index}",
                type="function",
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
            )
            for index, (name, arguments) in enumerate(calls)
        ]
        message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], model=kw["model"])


def is_repair(request: Mapping[str, Any]) -> bool:
    """Recognize either bounded protocol-repair request shape."""

    messages = request["messages"]
    return messages[-1]["content"].startswith("Your answer") or any(
        "An earlier answer to this request was rejected." in message["content"]
        for message in messages
        if message["role"] == "user"
    )
