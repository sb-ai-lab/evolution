"""Task 7: family-10 chat Reflector and chat Editor on `role_call_retry/1`.

The doubles are the existing chat-role test files' own (`_ws`, `_brief`,
`_edit_flow`, `_response`/`_plan`, `reflect_request`, ...), imported rather
than copied, following the convention `test_tree_chat_reflector.py` already
uses for `test_tree_reflect_brief.py`/`test_tree_reflect_evidence.py`. The one
new fixture here is `_ScriptedClient`, which -- unlike the existing `_Client`
doubles -- can raise a scripted provider exception so `role_call.call_with_retry`
has something to classify.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from evolution.prompts import TREE_EDITOR_SYSTEM, TREE_REFLECTOR_SYSTEM
from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.editor.chat_editor import (
    ChatCredentials,
    ChatEditor,
    _new_client,
)
from evolution.tree.roles.reflector.chat_reflector import ChatReflector
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.seals import seal_prompt
from tests.tree.roles.editor.test_chat_editor import SECRET as _EDITOR_SECRET
from tests.tree.roles.editor.test_chat_editor import _edit_flow, _plan, _ws
from tests.tree.roles.editor.test_chat_editor import _log_lines as _editor_log_lines
from tests.tree.roles.editor.test_chat_editor import _request as _edit_request
from tests.tree.roles.editor.test_chat_editor import _response as _edit_response
from tests.tree.roles.editor.test_chat_editor import _section as _editor_section
from tests.tree.roles.reflector.test_chat_reflector import _response as _reflect_response
from tests.tree.roles.reflector.test_chat_reflector import (
    reflect_request,  # noqa: F401 - re-used fixture
)

RETRY = RoleCallRetry(
    max_attempts=3,
    total_budget_seconds=30,
    initial_seconds=0,
    multiplier=1,
    max_seconds=0,
)


# --------------------------------------------------------------------------
# A scripted client that can raise a provider exception
# --------------------------------------------------------------------------


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://gw.example.test/v1/chat/completions")
    return cls("provider", response=httpx.Response(status, request=request), body=None)


def _server_error() -> openai.APIStatusError:
    return _status_error(openai.InternalServerError, 503)


def _rate_limited() -> openai.APIStatusError:
    return _status_error(openai.RateLimitError, 429)


def _unauthorized() -> openai.APIStatusError:
    return _status_error(openai.AuthenticationError, 401)


class _ScriptedClient:
    """`create(**kw)` pops the next scripted item: a response, or an exception to raise."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        item.model = kwargs["model"]
        return item


def _resilient_section(**kw: Any) -> ChatEditorSection:
    kw.setdefault("retry", RETRY)
    return _editor_section(**kw)


def _resilient_editor(tmp_path, client: Any, **kw: Any) -> ChatEditor:
    return ChatEditor(
        section=_resilient_section(**kw),
        credentials=ChatCredentials(_EDITOR_SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(_EDITOR_SECRET,),
        client_factory=lambda section, credentials: client,
        role_resilience=True,
    )


def _resilient_reflector(tmp_path, client: Any, **kw: Any) -> ChatReflector:
    return ChatReflector(
        section=_resilient_section(**kw),
        credentials=ChatCredentials(_EDITOR_SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM),
        log_root=tmp_path / "run" / "reflector-logs",
        secrets=(_EDITOR_SECRET,),
        client_factory=lambda section, credentials: client,
        role_resilience=True,
    )


# --------------------------------------------------------------------------
# `_new_client`: `max_retries` passed only when given
# --------------------------------------------------------------------------


def test_new_client_passes_max_retries_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeAsyncOpenAI)
    section = _editor_section()
    credentials = ChatCredentials(_EDITOR_SECRET, None)

    _new_client(section, credentials)
    _new_client(section, credentials, max_retries=0)

    assert "max_retries" not in captured[0]
    assert captured[1]["max_retries"] == 0


# --------------------------------------------------------------------------
# `role_resilience`: the constructor invariant and the client it builds
# --------------------------------------------------------------------------


def test_role_resilience_requires_a_retry_policy(tmp_path) -> None:
    with pytest.raises(AssertionError):
        ChatEditor(
            section=_editor_section(),
            credentials=ChatCredentials(_EDITOR_SECRET, None),
            prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
            log_root=tmp_path / "run" / "editor-logs",
            secrets=(_EDITOR_SECRET,),
            role_resilience=True,
        )


async def test_role_resilience_builds_its_client_with_max_retries_zero(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_new_client(section: Any, credentials: Any, *, max_retries: int | None = None) -> Any:
        captured["max_retries"] = max_retries
        return _ScriptedClient(_edit_flow())

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor._new_client", fake_new_client)
    editor = ChatEditor(
        section=_resilient_section(),
        credentials=ChatCredentials(_EDITOR_SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
        log_root=tmp_path / "run" / "editor-logs",
        secrets=(_EDITOR_SECRET,),
        role_resilience=True,
    )

    result = await editor.edit(_edit_request(_ws(tmp_path)))

    assert captured["max_retries"] == 0
    assert result.status == "edited"


# --------------------------------------------------------------------------
# The family-10 chat Editor over `role_call_retry/1`
# --------------------------------------------------------------------------


async def test_family10_chat_editor_retries_one_request(tmp_path) -> None:
    workspace = _ws(tmp_path)
    client = _ScriptedClient([_server_error(), _rate_limited(), *_edit_flow()])
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert (result.status, result.note) == ("edited", "done")
    first_three = client.calls[:3]
    assert first_three[0]["messages"] == first_three[1]["messages"] == first_three[2]["messages"]


async def test_family10_exhaustion_is_provider_exhausted(tmp_path) -> None:
    workspace = _ws(tmp_path)
    client = _ScriptedClient([_server_error(), _server_error(), _server_error()])
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert (result.status, result.code) == ("failed", "provider_exhausted")
    assert len(client.calls) == 3


async def test_family10_401_is_provider_rejected_without_retry(tmp_path) -> None:
    workspace = _ws(tmp_path)
    client = _ScriptedClient([_unauthorized()])
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert (result.status, result.code) == ("failed", "provider_rejected")
    assert len(client.calls) == 1


async def test_protocol_errors_do_not_consume_retry_attempts(tmp_path) -> None:
    """Two malformed `record_plan` calls trigger protocol repair, not a retry:
    each turn is its own logical request, transport-successful on its first
    (and only) attempt, so every `provider_attempt` entry reads `attempt: 1`.
    """

    workspace = _ws(tmp_path)
    path = "skills/SKILL.md"
    script = [
        _edit_response("list_files"),  # preflight
        _edit_response("record_plan", "not-json"),  # turn 1: repair
        _edit_response("record_plan", "still not json"),  # turn 2: repair
        _plan(path),  # turn 3: a valid plan
        _edit_response("write_file", json.dumps({"path": path, "content": "second\n"})),
        _edit_response("finish_edit", '{"summary":"done"}'),
    ]
    client = _ScriptedClient(script)
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert result.status == "edited"
    attempts = [
        entry["attempt"]
        for entry in _editor_log_lines(editor)
        if entry.get("phase") == "provider_attempt"
    ]
    assert attempts, "no provider_attempt records were written"
    assert all(attempt == 1 for attempt in attempts)


# --------------------------------------------------------------------------
# The family-10 chat Reflector: the same contract, over the same function
# --------------------------------------------------------------------------


async def test_family10_chat_reflector_retries_one_request(
    tmp_path,
    reflect_request,  # noqa: F811 - the imported fixture, used by name
) -> None:
    client = _ScriptedClient(
        [
            _server_error(),
            _reflect_response("list_files"),
            _reflect_response("finish_reflection", {"groups": [], "declined": True}),
        ]
    )
    reflector = _resilient_reflector(tmp_path, client)

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    first_two = client.calls[:2]
    assert first_two[0]["messages"] == first_two[1]["messages"]


async def test_family10_chat_reflector_exhaustion_is_provider_exhausted(
    tmp_path,
    reflect_request,  # noqa: F811 - the imported fixture, used by name
) -> None:
    client = _ScriptedClient([_server_error(), _server_error(), _server_error()])
    reflector = _resilient_reflector(tmp_path, client)

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.code) == ("failed", "provider_exhausted")
    assert len(client.calls) == 3


# --------------------------------------------------------------------------
# Final review, finding 2: preflight dialect negotiation survives a 4xx
# --------------------------------------------------------------------------


def _bad_request() -> openai.APIStatusError:
    return _status_error(openai.BadRequestError, 400)


class _ModernRejecting400Client(_ScriptedClient):
    """Answers a tools/tool_choice request with a terminal HTTP 400 and
    anything else (the legacy `functions` dialect) from the script, or with
    `rejection` when one is given for every request."""

    def __init__(self, script: list[Any], rejection: BaseException | None = None) -> None:
        super().__init__(script)
        self.rejection = rejection

    async def create(self, **kwargs: Any) -> Any:
        if self.rejection is not None:
            self.calls.append(kwargs)
            raise self.rejection
        if "tools" in kwargs:
            self.calls.append(kwargs)
            raise _bad_request()
        return await super().create(**kwargs)


async def test_family10_chat_editor_preflight_falls_back_to_legacy_on_400(tmp_path) -> None:
    from tests.tree.roles.editor.test_chat_editor import _legacy_response

    workspace = _ws(tmp_path)
    path = "skills/SKILL.md"
    client = _ModernRejecting400Client(
        [
            _legacy_response("list_files"),
            _plan(path, legacy=True),
            _legacy_response("write_file", json.dumps({"path": path, "content": "second\n"})),
            _legacy_response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert result.status == "edited"
    # Each modern dialect is rejected once, with no retry (a 400 is
    # terminal), then the legacy dialect carries the whole conversation.
    assert ["tools" in call for call in client.calls[:2]] == [True, True]
    assert all("functions" in call for call in client.calls[2:])


async def test_family10_chat_editor_preflight_401_stays_provider_rejected(tmp_path) -> None:
    workspace = _ws(tmp_path)
    client = _ModernRejecting400Client([], rejection=_unauthorized())
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert (result.status, result.code) == ("failed", "provider_rejected")
    assert len(client.calls) == 1


async def test_family10_chat_editor_400_after_preflight_is_provider_rejected(tmp_path) -> None:
    workspace = _ws(tmp_path)
    client = _ScriptedClient([_edit_response("list_files"), _bad_request()])
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(workspace))

    assert (result.status, result.code) == ("failed", "provider_rejected")
    assert len(client.calls) == 2


async def test_family10_chat_reflector_preflight_falls_back_to_legacy_on_400(
    tmp_path,
    reflect_request,  # noqa: F811 - the imported fixture, used by name
) -> None:
    from tests.tree.roles.editor.test_chat_editor import _legacy_response

    client = _ModernRejecting400Client(
        [
            _legacy_response("list_files"),
            _legacy_response("finish_reflection", json.dumps({"groups": [], "declined": True})),
        ]
    )
    reflector = _resilient_reflector(tmp_path, client)

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert all("functions" in call for call in client.calls[2:])


async def test_family10_chat_reflector_preflight_401_stays_provider_rejected(
    tmp_path,
    reflect_request,  # noqa: F811 - the imported fixture, used by name
) -> None:
    client = _ModernRejecting400Client([], rejection=_unauthorized())
    reflector = _resilient_reflector(tmp_path, client)

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.code) == ("failed", "provider_rejected")
    assert len(client.calls) == 1


# --------------------------------------------------------------------------
# Final review, finding 10: a family-10 identity mismatch is logged
# --------------------------------------------------------------------------


async def test_family10_identity_mismatch_is_logged(tmp_path) -> None:
    workspace = _ws(tmp_path)
    response = _edit_response("list_files")
    response.provider = "B"
    client = _ScriptedClient([response])
    editor = _resilient_editor(tmp_path, client, provider_only=("A",))

    result = await editor.edit(_edit_request(workspace))

    assert (result.status, result.note.split(" ")[0]) == ("failed", "provider_changed")
    entries = _editor_log_lines(editor)
    failed = [
        entry
        for entry in entries
        if entry.get("phase") == "provider_attempt" and entry.get("outcome") == "identity_mismatch"
    ]
    assert len(failed) == 1 and failed[0]["attempt"] == 1
    assert (failed[0]["http_status"], failed[0]["provider_code"]) == (None, None)
    mismatch = [entry for entry in entries if entry.get("phase") == "identity_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["requested_model"] == "m"
    assert mismatch[0]["returned_provider"] == "B"
    assert mismatch[0]["provider_verified"] is False


@pytest.mark.parametrize("status", [400, 413])
async def test_editor_context_rejection_preserves_latest_exchange_and_stops(
    tmp_path, status
) -> None:
    response = _edit_response("list_files")
    continuation = "opaque-context-sentinel" * 8000
    response.choices[0].message.reasoning_content = continuation
    client = _ScriptedClient(
        [_edit_response("list_files"), response, _status_error(openai.APIStatusError, status)]
    )
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(_ws(tmp_path)))

    assert (result.status, result.code) == ("failed", "provider_rejected")
    assert len(client.calls) == 3
    assert any(
        message.get("reasoning_content") == continuation for message in client.calls[-1]["messages"]
    )


@pytest.mark.parametrize("status", [400, 413])
async def test_reflector_context_rejection_preserves_latest_exchange_and_stops(
    tmp_path,
    reflect_request,  # noqa: F811 - imported fixture
    status,
) -> None:
    response = _reflect_response("list_files")
    continuation = "opaque-context-sentinel" * 8000
    response.choices[0].message.reasoning_content = continuation
    client = _ScriptedClient(
        [_reflect_response("list_files"), response, _status_error(openai.APIStatusError, status)]
    )
    reflector = _resilient_reflector(tmp_path, client)

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.code) == ("failed", "provider_rejected")
    assert len(client.calls) == 3
    assert any(
        message.get("reasoning_content") == continuation for message in client.calls[-1]["messages"]
    )


# --------------------------------------------------------------------------
# #290 §4.3: every provider attempt records its HTTP status and provider code
# --------------------------------------------------------------------------

#: A provider error body whose message echoes the request: never logged (ADR-0039).
ECHO = "ECHO-OF-REQUEST-TEXT"
BUSY = {"status": 503, "code": "server_busy", "message": ECHO}


def _attempts(entries: list[dict[str, Any]]) -> list[tuple[Any, Any, Any]]:
    return [
        (entry["outcome"], entry["http_status"], entry["provider_code"])
        for entry in entries
        if entry.get("phase") == "provider_attempt"
    ]


def _served(script: list[Any]) -> tuple[str, Any]:
    import tests.fake_chat_server as fake_chat_server

    return fake_chat_server.start(script)


def _stop(server: Any) -> None:
    server.shutdown()
    server.server_close()


def _real_client(section: Any, credentials: Any) -> Any:
    return _new_client(section, credentials, max_retries=0)


async def test_a_chat_editor_attempt_records_the_parsed_status_and_code(tmp_path) -> None:
    """A preflight 503 then 200, through the SDK's own parse of the body."""

    import tests.fake_chat_server as fake

    path = "skills/SKILL.md"
    base_url, server = _served(
        [
            BUSY,
            fake.tool_call("list_files"),
            fake.tool_call(
                "record_plan",
                {
                    "summary": "s",
                    "changes": [{"path": path, "action": "update", "purpose": "p"}],
                },
            ),
            fake.tool_call("write_file", {"path": path, "content": "second\n"}),
            fake.tool_call("finish_edit", {"summary": "done"}),
        ]
    )
    try:
        editor = ChatEditor(
            section=_resilient_section(base_url=base_url),
            credentials=ChatCredentials(_EDITOR_SECRET, None),
            prompt=seal_prompt(None, packaged=TREE_EDITOR_SYSTEM),
            log_root=tmp_path / "run" / "editor-logs",
            secrets=(_EDITOR_SECRET,),
            client_factory=_real_client,
            role_resilience=True,
        )
        result = await editor.edit(_edit_request(_ws(tmp_path)))
    finally:
        _stop(server)

    assert result.status == "edited"
    entries = _editor_log_lines(editor)
    attempts = _attempts(entries)
    assert attempts[:2] == [("http_5xx", 503, "server_busy"), ("ok", None, None)]
    assert all(attempt == ("ok", None, None) for attempt in attempts[1:])
    assert ECHO not in json.dumps(entries)


async def test_a_chat_reflector_attempt_records_the_parsed_status_and_code(
    tmp_path,
    reflect_request,  # noqa: F811 - the imported fixture, used by name
) -> None:
    import tests.fake_chat_server as fake

    base_url, server = _served(
        [
            BUSY,
            fake.tool_call("list_files"),
            fake.tool_call("finish_reflection", {"groups": [], "declined": True}),
        ]
    )
    try:
        reflector = ChatReflector(
            section=_resilient_section(base_url=base_url),
            credentials=ChatCredentials(_EDITOR_SECRET, None),
            prompt=seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM),
            log_root=tmp_path / "run" / "reflector-logs",
            secrets=(_EDITOR_SECRET,),
            client_factory=_real_client,
            role_resilience=True,
        )
        result = await reflector.reflect(reflect_request)
    finally:
        _stop(server)

    assert result.status == "declined"
    from tests.tree.roles.reflector.test_chat_reflector import _log_lines as _reflector_log_lines

    entries = _reflector_log_lines(reflector)
    assert _attempts(entries) == [
        ("http_5xx", 503, "server_busy"),
        ("ok", None, None),
        ("ok", None, None),
    ]
    assert ECHO not in json.dumps(entries)


async def test_a_chat_grouper_attempt_records_the_parsed_status_and_code(tmp_path) -> None:
    from evolution.tree.roles.grouping.model_grouping_runtime import run_model_grouping
    from evolution.tree.roles.workspace import open_attempt_log
    from tests.test_tree_chat_credentials import _grouping_answer
    from tests.tree.roles.grouping.test_model_grouping_runtime import (
        NEEDLE,
        PROMPT,
        REG,
        SECRET,
        _no_sleep,
        log_records,
        section,
    )

    base_url, server = _served([BUSY, _grouping_answer])
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=(SECRET,))
    try:
        outcome = await run_model_grouping(
            REG,
            section=section(base_url=base_url),
            prompt=PROMPT,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials("key", None),
            log=log,
            deadline=float("inf"),
            cancelled=lambda: False,
            forbidden_secret=lambda text: SECRET in text,
            oracle_needle=lambda text: NEEDLE in text,
            sleep=_no_sleep,
            family14=True,
        )
    finally:
        log.close()
        _stop(server)

    assert outcome.fallback is None
    entries = log_records(tmp_path)
    assert _attempts(entries) == [("http_5xx", 503, "server_busy"), ("ok", None, None)]
    assert ECHO not in json.dumps(entries)


def _busy() -> openai.APIStatusError:
    request = httpx.Request("POST", "https://gw.example.test/v1/chat/completions")
    return openai.InternalServerError(
        ECHO,
        response=httpx.Response(503, request=request),
        body={"code": "server_busy", "message": ECHO},
    )


async def test_the_editors_model_request_failed_records_status_and_code(tmp_path) -> None:
    client = _ScriptedClient([_edit_response("list_files"), _busy(), _busy(), _busy()])
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(_ws(tmp_path)))

    assert (result.status, result.code) == ("failed", "provider_exhausted")
    entries = _editor_log_lines(editor)
    failed = [entry for entry in entries if entry.get("phase") == "model_request_failed"]
    assert len(failed) == 1
    assert (failed[0]["http_status"], failed[0]["provider_code"]) == (503, "server_busy")
    assert _attempts(entries)[1:] == [("http_5xx", 503, "server_busy")] * 3
    assert ECHO not in json.dumps(entries)


class _ModernRejectingWithBody(_ModernRejecting400Client):
    """The 400 a provider gives a `tools` request it cannot take, with a code."""

    async def create(self, **kwargs: Any) -> Any:
        if "tools" in kwargs:
            self.calls.append(kwargs)
            request = httpx.Request("POST", "https://gw.example.test/v1/chat/completions")
            raise openai.BadRequestError(
                ECHO,
                response=httpx.Response(400, request=request),
                body={"error": {"code": "unsupported_parameter", "message": ECHO}},
            )
        return await super().create(**kwargs)


async def test_the_preflights_model_request_failed_records_status_and_code(tmp_path) -> None:
    from tests.tree.roles.editor.test_chat_editor import _legacy_response

    path = "skills/SKILL.md"
    client = _ModernRejectingWithBody(
        [
            _legacy_response("list_files"),
            _plan(path, legacy=True),
            _legacy_response("write_file", json.dumps({"path": path, "content": "second\n"})),
            _legacy_response("finish_edit", '{"summary":"done"}'),
        ]
    )
    editor = _resilient_editor(tmp_path, client)

    result = await editor.edit(_edit_request(_ws(tmp_path)))

    assert result.status == "edited"
    entries = _editor_log_lines(editor)
    failed = [entry for entry in entries if entry.get("phase") == "model_request_failed"]
    assert [(row["http_status"], row["provider_code"]) for row in failed] == [
        (400, "unsupported_parameter"),
        (400, "unsupported_parameter"),
    ]
    assert _attempts(entries)[:2] == [("http_4xx", 400, "unsupported_parameter")] * 2
    assert ECHO not in json.dumps(entries)


async def test_the_reflectors_preflight_model_request_failed_records_status_and_code(
    tmp_path,
    reflect_request,  # noqa: F811 - the imported fixture, used by name
) -> None:
    from tests.tree.roles.editor.test_chat_editor import _legacy_response
    from tests.tree.roles.reflector.test_chat_reflector import _log_lines as _reflector_log_lines

    client = _ModernRejectingWithBody(
        [
            _legacy_response("list_files"),
            _legacy_response("finish_reflection", json.dumps({"groups": [], "declined": True})),
        ]
    )
    reflector = _resilient_reflector(tmp_path, client)

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    entries = _reflector_log_lines(reflector)
    failed = [entry for entry in entries if entry.get("phase") == "model_request_failed"]
    assert [(row["http_status"], row["provider_code"]) for row in failed] == [
        (400, "unsupported_parameter"),
        (400, "unsupported_parameter"),
    ]
    assert ECHO not in json.dumps(entries)
