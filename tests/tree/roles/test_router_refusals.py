"""Router error bodies are private refusals before completion identity checks."""

import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import openai
import pytest

from evolution.tree.roles.editor.chat_editor import EditFailure, provider_request
from evolution.tree.roles.grouping.model_grouping_runtime import _Session
from evolution.tree.roles.role_call import RoleCallExhausted
from evolution.tree.roles.workspace import open_attempt_log
from tests.test_tree_chat_role_retry import (
    RETRY,
    _resilient_editor,
    _resilient_reflector,
    _resilient_section,
    _ScriptedClient,
)
from tests.tree.roles.editor.test_chat_editor import _log_lines, _request, _response, _ws
from tests.tree.roles.grouping.test_portion_session import Harness, basic_rows
from tests.tree.roles.grouping.test_portion_session import section as grouping_section
from tests.tree.roles.reflector.test_chat_reflector import reflect_request  # noqa: F401

MESSAGE = "PRIVATE-ROUTER-MESSAGE no endpoints found for the requested parameters"


def refusal(code=503, message=MESSAGE, provider="CoreWeave"):
    return SimpleNamespace(
        error={"code": code, "message": message, "metadata": {"provider_name": provider}},
        choices=[],
    )


def _preflight():
    result = _response("list_files")
    result.provider = "CoreWeave"
    return result


@pytest.mark.parametrize(
    "code,attempts,result_code,classification",
    [
        (503, 3, "provider_exhausted", "http_5xx"),
        (400, 1, "provider_rejected", "http_4xx"),
        (None, 1, "provider_rejected", "unknown"),
        ("no_endpoints", 1, "provider_rejected", "unknown"),
    ],
)
async def test_v16_error_body_is_refusal_not_identity_mismatch(
    tmp_path, code, attempts, result_code, classification
):
    client = _ScriptedClient([_preflight(), *[refusal(code) for _ in range(attempts)]])
    editor = replace(
        _resilient_editor(tmp_path, client, provider_only=("CoreWeave",)), family15=True
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.status == "failed" and result.code == result_code
    assert f"after {attempts} attempts ({classification})" in result.note
    assert "router refused the request (status 200, code " in result.note
    assert "provider CoreWeave); its message is in the role log" in result.note
    records = _log_lines(editor)
    refused = [r for r in records if r.get("phase") == "provider_refused"]
    assert len(refused) == attempts
    assert [r["attempt"] for r in refused] == list(range(1, attempts + 1))
    assert all(r["message"] == MESSAGE and r["http_status"] == 200 for r in refused)
    assert not any(r.get("phase") == "identity_mismatch" for r in records)


async def test_v16_http_401_with_body_stays_credential(tmp_path):
    request = httpx.Request("POST", "https://router.test/v1")
    error = openai.AuthenticationError(
        MESSAGE,
        response=httpx.Response(401, request=request),
        body={"code": "invalid_api_key", "message": MESSAGE},
    )
    client = _ScriptedClient([_preflight(), error])
    editor = replace(
        _resilient_editor(tmp_path, client, provider_only=("CoreWeave",)), family15=True
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.code == "provider_rejected"
    assert "after 1 attempts (credential)" in result.note
    refused = [r for r in _log_lines(editor) if r.get("phase") == "provider_refused"]
    assert len(refused) == 1
    assert (refused[0]["http_status"], refused[0]["error_code"], refused[0]["message"]) == (
        401,
        "invalid_api_key",
        MESSAGE,
    )
    assert len(client.calls) == 2
    assert MESSAGE not in result.note


@pytest.fixture(
    params=[
        (401, "invalid_api_key", 1, "credential"),
        (401, 503, 1, "credential"),
        (400, 503, 1, "http_4xx"),
        (429, 401, 3, "http_429"),
        (503, 403, 3, "http_5xx"),
    ]
)
async def sdk_router(request):
    status, code, attempts, classification = request.param
    calls = []
    message = MESSAGE + "x" * 10000 + "RUN-SECRET"

    def respond(http_request):
        calls.append(http_request)
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": code,
                    "message": message,
                    "metadata": {"provider_name": "CoreWeave"},
                }
            },
        )

    async with openai.AsyncOpenAI(
        api_key="synthetic-test-key",
        base_url="https://router.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        yield SimpleNamespace(
            client=client,
            calls=calls,
            status=status,
            code=code,
            attempts=attempts,
            classification=classification,
            redacted_message=message.replace("RUN-SECRET", "[REDACTED]"),
        )


def _assert_sdk_refusal_records(log, router, family15):
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    refused = [r for r in records if r.get("phase") == "provider_refused"]
    assert len(router.calls) == router.attempts
    assert len(refused) == (router.attempts if family15 else 0)
    for attempt, record in enumerate(refused, 1):
        assert record["attempt"] == attempt
        assert record["http_status"] == router.status
        assert record["error_code"] == router.code
        assert record["provider_name"] == "CoreWeave"
        assert record["message"] == router.redacted_message
    assert "RUN-SECRET" not in log.path.read_text()
    assert all(
        MESSAGE not in json.dumps(r) for r in records if r.get("phase") != "provider_refused"
    )


@pytest.mark.parametrize("family15", [True, False], ids=["v16", "v15"])
async def test_sdk_status_error_on_tool_path(tmp_path, sdk_router, family15):
    with open_attempt_log(tmp_path / "logs", "tool", secrets=("RUN-SECRET",)) as log:
        with pytest.raises(EditFailure) as caught:
            await provider_request(
                client=sdk_router.client,
                section=_resilient_section(),
                log=log,
                messages=[],
                tools=[],
                request_style="modern",
                turn=1,
                remaining=lambda: 10,
                retry=RETRY,
                deadline=time.monotonic() + 10,
                family15=family15,
            )
    failure = caught.value
    assert failure.code == (
        "provider_rejected" if sdk_router.attempts == 1 else "provider_exhausted"
    )
    assert f"after {sdk_router.attempts} attempts ({sdk_router.classification})" in failure.note
    assert isinstance(failure.__cause__.__cause__, openai.APIStatusError)
    assert MESSAGE not in failure.note
    _assert_sdk_refusal_records(log, sdk_router, family15)


@pytest.mark.parametrize("family15", [True, False], ids=["v16", "v15"])
async def test_sdk_status_error_on_consolidation_path(tmp_path, sdk_router, family15):
    async def no_sleep(_seconds):
        return None

    with open_attempt_log(tmp_path / "logs", "batch", secrets=("RUN-SECRET",)) as log:
        session = _Session(
            client=sdk_router.client,
            section=grouping_section(retry=RETRY),
            log=log,
            deadline=time.monotonic() + 10,
            cancelled=lambda: False,
            forbidden_secret=lambda _: False,
            clock=time.monotonic,
            sleep=no_sleep,
            rng=lambda: 0,
            family15=family15,
        )
        with pytest.raises(RoleCallExhausted) as caught:
            await session.request([], "batch-1")
    assert caught.value.code == sdk_router.classification
    assert caught.value.attempts == sdk_router.attempts
    assert isinstance(caught.value.__cause__, openai.APIStatusError)
    assert MESSAGE not in str(caught.value)
    assert MESSAGE not in json.dumps(asdict(session.recorder.report()))
    _assert_sdk_refusal_records(log, sdk_router, family15)


async def test_v15_error_body_keeps_provider_changed(tmp_path):
    client = _ScriptedClient([_preflight(), refusal()])
    editor = _resilient_editor(tmp_path, client, provider_only=("CoreWeave",))
    result = await editor.edit(_request(_ws(tmp_path)))
    assert result.code == "provider_changed"
    records = _log_lines(editor)
    assert any(r.get("phase") == "identity_mismatch" for r in records)
    assert not any(r.get("phase") == "provider_refused" for r in records)


async def test_refusal_message_only_in_provider_refused(tmp_path):
    client = _ScriptedClient([_preflight(), *[refusal() for _ in range(3)]])
    editor = replace(
        _resilient_editor(tmp_path, client, provider_only=("CoreWeave",)), family15=True
    )
    result = await editor.edit(_request(_ws(tmp_path)))
    records = _log_lines(editor)
    assert any(MESSAGE in json.dumps(r) for r in records)
    assert all(
        MESSAGE not in json.dumps(r) for r in records if r.get("phase") != "provider_refused"
    )
    assert MESSAGE not in result.note


async def test_v16_reflector_detects_refusals(tmp_path, reflect_request):  # noqa: F811
    client = _ScriptedClient([_preflight(), *[refusal() for _ in range(3)]])
    reflector = replace(
        _resilient_reflector(tmp_path, client, provider_only=("CoreWeave",)), family15=True
    )
    result = await reflector.reflect(reflect_request)
    assert result.code == "provider_exhausted"
    records = [
        json.loads(line) for line in (reflector.log_root / "r01-x01.jsonl").read_text().splitlines()
    ]
    assert len([r for r in records if r.get("phase") == "provider_refused"]) == 3
    assert not any(r.get("phase") == "identity_mismatch" for r in records)


async def test_v16_portion_session_detects_refusals(tmp_path):
    harness = Harness(tmp_path, basic_rows(), retry=RETRY)
    client = _ScriptedClient([refusal() for _ in range(3)])
    with open_attempt_log(tmp_path / "portion-logs", "portion", secrets=()) as log:
        from evolution.tree.roles.grouping.portion_session import run_portion

        with pytest.raises(RoleCallExhausted) as caught:
            await run_portion(harness.context(client, log), harness.plan.portions[0])
    assert caught.value.reason == "provider_unavailable"
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert len([r for r in records if r.get("phase") == "provider_refused"]) == 3
    assert MESSAGE not in str(caught.value)


async def test_v16_refusal_message_redacted_but_not_cut(tmp_path):
    message = "x" * 10000 + "RUN-SECRET" + "y" * 10000
    client = _ScriptedClient([refusal(400, message)])
    with open_attempt_log(tmp_path / "logs", "private", secrets=("RUN-SECRET",)) as log:
        with pytest.raises(EditFailure):
            await provider_request(
                client=client,
                section=_resilient_section(),
                log=log,
                messages=[],
                tools=[],
                request_style="modern",
                turn=1,
                remaining=lambda: 10,
                retry=RETRY,
                deadline=time.monotonic() + 10,
                family15=True,
            )
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    record = next(r for r in records if r.get("phase") == "provider_refused")
    assert record["message"] == "x" * 10000 + "[REDACTED]" + "y" * 10000


async def _negotiate(tmp_path, client, *, family15=True, **section_keys):
    from itertools import count

    from evolution.tree.roles.editor.chat_editor import TOOLS, negotiate_style
    from evolution.tree.roles.role_call import AttemptRecorder

    section_keys.setdefault("provider_only", ("CoreWeave",))
    section = _resilient_section(**section_keys)
    recorder = AttemptRecorder()
    styles = []
    with open_attempt_log(tmp_path / "negotiation", "preflight", secrets=()) as log:
        style = await negotiate_style(
            client=client,
            section=section,
            log=log,
            tools=TOOLS,
            next_turn=count().__next__,
            remaining=lambda: 10,
            record=lambda messages, style, turn, tools: styles.append(style),
            retry=RETRY,
            deadline=time.monotonic() + 10,
            attempts=recorder,
            family15=family15,
        )
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    return style, records, recorder, styles


class ShapeRouter:
    def __init__(self, code=503):
        self.code = code
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = refusal(self.code) if "parallel_tool_calls" in kwargs else _preflight()
        result.model = kwargs["model"]
        return result


@pytest.mark.parametrize("code,attempts", [(503, 3), (404, 1), (None, 1)])
async def test_383_body_retried_in_modern_before_modern_single(tmp_path, code, attempts):
    client = ShapeRouter(code)
    style, records, recorder, styles = await _negotiate(tmp_path, client)
    assert style == "modern_single"
    assert styles == ["modern", "modern_single"]
    assert len(client.calls) == attempts + 1
    assert all(r["parallel_tool_calls"] is False for r in client.calls[:-1])
    assert "parallel_tool_calls" not in client.calls[-1]
    assert client.calls[-1]["tool_choice"] == "required"
    report = recorder.report()
    assert report.failure is None
    assert len(report.summary.failed) == attempts
    assert len([r for r in records if r.get("phase") == "provider_refused"]) == attempts
    failed = [r for r in records if r.get("phase") == "model_request_failed"]
    assert len(failed) == 1 and failed[0]["http_status"] == 200
    assert MESSAGE not in json.dumps(failed)


@pytest.mark.parametrize("code", [401, 403])
async def test_inner_credential_never_changes_style(tmp_path, code):
    client = _ScriptedClient([refusal(code)])
    with pytest.raises(EditFailure) as caught:
        await _negotiate(tmp_path, client)
    assert caught.value.code == "provider_rejected"
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_http_credential_never_changes_style(tmp_path, status):
    request = httpx.Request("POST", "https://router.test/v1")
    error = openai.APIStatusError(
        MESSAGE,
        response=httpx.Response(status, request=request),
        body={"error": {"code": 503, "message": MESSAGE}},
    )
    client = _ScriptedClient([error])
    with pytest.raises(EditFailure) as caught:
        await _negotiate(tmp_path, client)
    assert caught.value.code == "provider_rejected"
    assert len(client.calls) == 1


async def test_mixed_timeout_and_refusal_does_not_move(tmp_path):
    client = _ScriptedClient([refusal(), TimeoutError(), refusal()])
    with pytest.raises(EditFailure) as caught:
        await _negotiate(tmp_path, client)
    assert caught.value.code == "provider_exhausted"
    assert len(client.calls) == 3
    assert all(r["parallel_tool_calls"] is False for r in client.calls)


@pytest.mark.parametrize(
    "code,per_style,result_code", [(503, 3, "provider_exhausted"), (400, 1, "provider_rejected")]
)
async def test_every_style_refused_never_enters_legacy(tmp_path, code, per_style, result_code):
    client = _ScriptedClient([refusal(code) for _ in range(per_style * 3)])
    with pytest.raises(EditFailure) as caught:
        await _negotiate(tmp_path, client)
    assert caught.value.code == result_code
    assert len(client.calls) == per_style * 3
    assert all("tools" in r and "functions" not in r for r in client.calls)
    assert [r["tool_choice"] for r in client.calls] == ["required"] * (2 * per_style) + [
        "auto"
    ] * per_style


async def test_dialect_4xx_still_moves_at_once_and_withdraws(tmp_path):
    request = httpx.Request("POST", "https://router.test/v1")
    error = openai.BadRequestError(
        "unsupported parameter", response=httpx.Response(400, request=request), body=None
    )
    client = _ScriptedClient([error, _preflight()])
    style, _, recorder, styles = await _negotiate(tmp_path, client)
    assert style == "modern_single" and styles == ["modern", "modern_single"]
    assert len(client.calls) == 2
    assert recorder.report() is None


async def test_openrouter_skips_modern_single(tmp_path):
    client = _ScriptedClient([refusal(404), _preflight()])
    style, _, recorder, styles = await _negotiate(
        tmp_path, client, base_url="https://openrouter.ai/api/v1"
    )
    assert style == "modern_auto" and styles == ["modern", "modern_auto"]
    assert all("parallel_tool_calls" not in request for request in client.calls)
    assert len(recorder.report().summary.failed) == 1


async def test_z_ai_styles_unchanged(tmp_path):
    response = _preflight()
    response.provider = "z-ai"
    client = _ScriptedClient([response])
    style, _, _, styles = await _negotiate(tmp_path, client, provider_only=("z-ai",))
    assert style == "modern_auto" and styles == ["modern_auto"]
    assert client.calls[0]["tool_choice"] == "auto"


async def test_v15_styles_unchanged(tmp_path):
    client = ShapeRouter()
    with pytest.raises(EditFailure) as caught:
        await _negotiate(tmp_path, client, family15=False)
    assert caught.value.klass == "provider_changed"
    assert len(client.calls) == 1


@pytest.mark.parametrize("base_url", ["https://router.test/v1", "https://openrouter.ai/api/v1"])
def test_modern_single_request_options(base_url):
    from evolution.tree.roles.editor.chat_editor import TOOLS, _protocol_options

    options = _protocol_options(_resilient_section(base_url=base_url), TOOLS, "modern_single")
    assert options == {"tools": TOOLS, "tool_choice": "required"}


@pytest.mark.parametrize("pin", ["provider_only", "provider_order"])
@pytest.mark.parametrize(
    "returned", [{}, {"model": "another-model", "provider": "another-provider"}]
)
@pytest.mark.parametrize(
    "role,family15",
    [("grouping", True), ("grouping", False), ("editor", True), ("reflector", True)],
)
async def test_only_v16_grouper_skips_completion_identity_verification(
    tmp_path, pin, returned, role, family15
):
    from evolution.tree.roles.editor.chat_editor import ProviderIdentityError, provider_response

    async def create(**kwargs):
        assert kwargs["extra_body"]["provider"]["only" if pin == "provider_only" else "order"] == [
            "CoreWeave"
        ]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], **returned
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    section = _resilient_section(**{pin: ("CoreWeave",)})
    arguments = dict(
        client=client,
        section=section,
        messages=[],
        protocol_options={},
        remaining=lambda: 5,
        role=role,
        family15=family15,
    )
    if role == "grouping" and family15:
        response = await provider_response(**arguments)
        assert response.choices[0].message.content == "ok"
    else:
        with pytest.raises(ProviderIdentityError):
            await provider_response(**arguments)
