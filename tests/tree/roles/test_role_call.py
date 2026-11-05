import asyncio
import ssl

import httpx
import openai
import pytest

from evolution.tree.roles.role_call import (
    RoleCallExhausted,
    RoleCallRetry,
    call_with_retry,
    classify_provider_error,
)

POLICY = {
    "version": "role_call_retry/1",
    "max_attempts": 3,
    "total_budget_seconds": 90,
    "backoff": {"initial_seconds": 1, "multiplier": 2, "max_seconds": 15, "jitter": "full"},
}


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    cls = {429: openai.RateLimitError, 500: openai.InternalServerError}.get(
        status, openai.APIStatusError
    )
    return cls("boom", response=response, body=None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_policy_round_trips_and_is_closed():
    policy = RoleCallRetry.from_payload(POLICY)
    assert policy.to_payload() == POLICY
    with pytest.raises(ValueError, match="backoff.jitter"):
        RoleCallRetry.from_payload({**POLICY, "backoff": {**POLICY["backoff"], "jitter": "equal"}})
    with pytest.raises(ValueError, match="max_attempts"):
        RoleCallRetry.from_payload({**POLICY, "max_attempts": 0})
    with pytest.raises(ValueError, match="unknown"):
        RoleCallRetry.from_payload({**POLICY, "extra": 1})


def test_ceiling_is_capped_exponential():
    policy = RoleCallRetry.from_payload(POLICY)
    assert [policy.ceiling(n) for n in (1, 2, 3, 4, 5)] == [1, 2, 4, 8, 15]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(429), ("transient", "http_429")),
        (_status_error(503), ("transient", "http_5xx")),
        (_status_error(408), ("transient", "http_408")),
        (_status_error(400), ("terminal", "http_4xx")),
        (_status_error(401), ("terminal", "credential")),
        (
            openai.APIConnectionError(request=httpx.Request("POST", "https://x.test")),
            ("transient", "connection"),
        ),
        (
            openai.APITimeoutError(request=httpx.Request("POST", "https://x.test")),
            ("transient", "timeout"),
        ),
        (httpx.RemoteProtocolError("closed"), ("transient", "connection")),
        (TimeoutError(), ("transient", "timeout")),
        (RuntimeError("?"), ("terminal", "unknown")),
    ],
)
def test_classifier_is_closed(exc, expected):
    assert classify_provider_error(exc) == expected


def test_classifier_detects_tls_in_exception_chain():
    """TLS errors in __cause__ or __context__ chain are classified as terminal/tls."""
    # Case (a): APIConnectionError → httpx.ConnectError → ssl.SSLCertVerificationError
    cert_error = ssl.SSLCertVerificationError("cert failed")
    connect_error = httpx.ConnectError("connection failed")
    connect_error.__cause__ = cert_error
    api_error = openai.APIConnectionError(request=httpx.Request("POST", "https://x.test"))
    api_error.__cause__ = connect_error
    assert classify_provider_error(api_error) == ("terminal", "tls")

    # Case (b): httpx.ConnectError → ssl.SSLError
    ssl_error = ssl.SSLError("ssl failed")
    http_connect = httpx.ConnectError("connection failed")
    http_connect.__cause__ = ssl_error
    assert classify_provider_error(http_connect) == ("terminal", "tls")

    # Case (c): APIConnectionError with non-TLS cause stays transient/connection
    non_tls_error = ValueError("some error")
    api_error_non_tls = openai.APIConnectionError(request=httpx.Request("POST", "https://x.test"))
    api_error_non_tls.__cause__ = non_tls_error
    assert classify_provider_error(api_error_non_tls) == ("transient", "connection")


async def test_retries_transient_then_succeeds_with_full_jitter():
    clock = FakeClock()
    calls: list[float] = []
    seen = []

    async def send(timeout: float) -> str:
        calls.append(timeout)
        if len(calls) < 3:
            raise _status_error(429)
        return "ok"

    result = await call_with_retry(
        send,
        policy=RoleCallRetry.from_payload(POLICY),
        role="grouping",
        request_id="r1",
        deadline=1000.0,
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 0.5,
        on_attempt=seen.append,
    )
    assert result == "ok"
    assert clock.sleeps == [0.5, 1.0]  # 0.5 * ceiling(1), 0.5 * ceiling(2)
    assert [t.outcome for t in seen] == ["http_429", "http_429", "ok"]
    assert [t.attempt for t in seen] == [1, 2, 3]


async def test_exhaustion_starts_no_extra_request():
    clock = FakeClock()
    calls = 0

    async def send(timeout: float) -> str:
        nonlocal calls
        calls += 1
        raise _status_error(500)

    with pytest.raises(RoleCallExhausted) as info:
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload(POLICY),
            role="grouping",
            request_id="r1",
            deadline=1000.0,
            clock=clock,
            sleep=clock.sleep,
            rng=lambda: 1.0,
        )
    assert calls == 3
    assert info.value.reason == "provider_unavailable"
    assert info.value.attempts == 3


async def test_total_budget_stops_before_a_wait_that_would_overrun_it():
    clock = FakeClock()
    calls = 0

    async def send(timeout: float) -> str:
        nonlocal calls
        calls += 1
        clock.now += 40  # each attempt burns 40 s
        raise _status_error(429)

    policy = RoleCallRetry.from_payload({**POLICY, "max_attempts": 10})
    with pytest.raises(RoleCallExhausted) as info:
        await call_with_retry(
            send,
            policy=policy,
            role="grouping",
            request_id="r1",
            deadline=1000.0,
            clock=clock,
            sleep=clock.sleep,
            rng=lambda: 1.0,
        )
    # Attempt 1 ends at 40, waits 1; attempt 2 ends at 81, waits 2 (9 s left);
    # attempt 3 starts at 83 with 7 s left and ends at 123; the next wait (4)
    # no longer fits the budget, so no fourth request starts.
    assert calls == 3
    assert clock.sleeps == [1.0, 2.0]
    assert info.value.reason == "provider_unavailable"


async def test_no_attempt_starts_after_the_deadline():
    clock = FakeClock()
    clock.now = 50.0
    calls = 0

    async def send(timeout: float) -> str:
        nonlocal calls
        calls += 1
        return "ok"

    with pytest.raises(RoleCallExhausted) as info:
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload(POLICY),
            role="grouping",
            request_id="r1",
            deadline=50.0,
            clock=clock,
            sleep=clock.sleep,
        )
    assert calls == 0
    assert info.value.reason == "deadline_exhausted"


async def test_deadline_passing_during_an_attempt_is_deadline_exhausted():
    """The enclosing operation deadline elapsing inside a provider call is
    `deadline_exhausted`, not `provider_unavailable`."""

    clock = FakeClock()
    calls = 0

    async def send(timeout: float) -> str:
        nonlocal calls
        calls += 1
        clock.now += timeout
        raise TimeoutError

    with pytest.raises(RoleCallExhausted) as info:
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload(POLICY),
            role="grouping",
            request_id="r1",
            deadline=30.0,
            clock=clock,
            sleep=clock.sleep,
            rng=lambda: 0.0,
        )
    assert calls == 1
    assert info.value.reason == "deadline_exhausted"
    assert info.value.code == "timeout"


async def test_per_attempt_timeout_is_the_smaller_remaining_budget():
    clock = FakeClock()
    timeouts: list[float] = []

    async def send(timeout: float) -> str:
        timeouts.append(timeout)
        return "ok"

    await call_with_retry(
        send,
        policy=RoleCallRetry.from_payload(POLICY),
        role="grouping",
        request_id="r1",
        deadline=30.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert timeouts == [30.0]


async def test_terminal_error_is_not_retried():
    clock = FakeClock()
    calls = 0

    async def send(timeout: float) -> str:
        nonlocal calls
        calls += 1
        raise _status_error(400)

    with pytest.raises(RoleCallExhausted) as info:
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload(POLICY),
            role="grouping",
            request_id="r1",
            deadline=1000.0,
            clock=clock,
            sleep=clock.sleep,
        )
    assert calls == 1
    assert info.value.reason == "provider_rejected"


async def test_cancellation_propagates_untouched():
    clock = FakeClock()

    async def send(timeout: float) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload(POLICY),
            role="grouping",
            request_id="r1",
            deadline=1000.0,
            clock=clock,
            sleep=clock.sleep,
        )


def test_telemetry_carries_status_and_code_but_no_message():
    """The status and the provider code join the five fields; no
    body or message field ever does (ADR-0039)."""

    import dataclasses

    from evolution.tree.roles.role_call import AttemptTelemetry

    assert {f.name for f in dataclasses.fields(AttemptTelemetry)} == {
        "role",
        "request_id",
        "attempt",
        "duration_seconds",
        "outcome",
        "http_status",
        "provider_code",
    }


def _bodied_error(status: int, body: object) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    cls = {500: openai.InternalServerError, 503: openai.InternalServerError}.get(
        status, openai.APIStatusError
    )
    return cls("ECHO-OF-REQUEST-TEXT", response=httpx.Response(status, request=request), body=body)


async def test_a_failed_attempt_records_its_status_and_code_but_no_text():
    import json
    from dataclasses import asdict

    clock = FakeClock()
    seen = []
    script: list[object] = [
        _bodied_error(503, {"error": {"code": "server_busy", "message": "ECHO-OF-REQUEST-TEXT"}}),
        "ok",
    ]

    async def send(timeout: float) -> object:
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    result = await call_with_retry(
        send,
        policy=RoleCallRetry.from_payload(POLICY),
        role="grouping",
        request_id="r1",
        deadline=1000.0,
        clock=clock,
        sleep=clock.sleep,
        on_attempt=seen.append,
    )
    assert result == "ok"
    assert [(t.outcome, t.http_status, t.provider_code) for t in seen] == [
        ("http_5xx", 503, "server_busy"),
        ("ok", None, None),
    ]
    assert "ECHO-OF-REQUEST-TEXT" not in json.dumps([asdict(t) for t in seen])


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (openai.APITimeoutError(request=httpx.Request("POST", "https://x.test")), "timeout"),
        (TimeoutError(), "timeout"),
        (openai.APIConnectionError(request=httpx.Request("POST", "https://x.test")), "connection"),
        (httpx.ConnectError("refused"), "connection"),
    ],
)
async def test_a_failure_without_a_response_has_no_status(exc, outcome):
    clock = FakeClock()
    seen = []

    async def send(timeout: float) -> str:
        raise exc

    with pytest.raises(RoleCallExhausted):
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload({**POLICY, "max_attempts": 1}),
            role="grouping",
            request_id="r1",
            deadline=1000.0,
            clock=clock,
            sleep=clock.sleep,
            on_attempt=seen.append,
        )
    assert [(t.outcome, t.http_status, t.provider_code) for t in seen] == [(outcome, None, None)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": {"code": "server_busy", "message": "text"}}, (503, "server_busy")),
        ({"code": "server_busy", "message": "text"}, (503, "server_busy")),
        ({"error": {"code": "unsafe code!", "type": "safe_type"}}, (503, "safe_type")),
        ({"error": {"code": "x" * 65}, "code": "safe_fallback"}, (503, "safe_fallback")),
        ({"error": {"code": 503, "message": "text"}}, (503, None)),
        ({"code": "unsafe code!", "type": "bad type!"}, (503, None)),
        (None, (503, None)),
    ],
)
def test_provider_error_detail_keeps_the_reflector_rules(body, expected):
    from evolution.tree.roles.role_call import provider_error_detail

    error = _bodied_error(503, body)
    assert provider_error_detail(error) == expected
    wrapped = RoleCallExhausted("provider_unavailable", 3, "http_5xx")
    wrapped.__cause__ = error
    outer = RuntimeError("outer")
    outer.__cause__ = wrapped
    assert provider_error_detail(outer) == expected
    assert provider_error_detail(RuntimeError("no response")) == (None, None)


_BLOCK_PROVIDER_SDKS = """
import sys


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("httpx", "openai"):
            raise ImportError(f"blocked {name}")
        return None


sys.meta_path.insert(0, _Block())
import evolution.tree.manifest.manifest_v2
import evolution.tree.roles.role_call
import evolution.tree.roles.grouping.grouping_runtime
import evolution.tree.roles.grouping.model_grouping_runtime
import evolution.cli.main
print("imported")
"""


def test_the_manifest_loader_imports_without_the_provider_sdks() -> None:
    """A core install has no `tree` extra: `manifest_v2` reaches this module
    for `RoleCallRetry`, so `httpx`/`openai` must load only on classification."""

    import os
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    result = subprocess.run(
        [sys.executable, "-c", _BLOCK_PROVIDER_SDKS],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


async def test_a_programming_error_inside_send_propagates():
    """Only provider/transport exceptions are classified; a `TypeError` in
    `send` is a defect, never `provider_rejected` and a silent fallback."""

    from evolution.tree.roles.role_call import RoleCallRetry, call_with_retry

    seen = []

    async def send(_timeout: float) -> None:
        raise TypeError("bug")

    with pytest.raises(TypeError, match="bug"):
        await call_with_retry(
            send,
            policy=RoleCallRetry(3, 90, 1, 2, 15),
            role="grouping",
            request_id="r",
            deadline=1000.0,
            clock=lambda: 0.0,
            on_attempt=seen.append,
        )
    assert seen == []


@pytest.mark.parametrize(
    "code,want",
    [
        (408, ("transient", "http_408")),
        (429, ("transient", "http_429")),
        (503, ("transient", "http_5xx")),
        (401, ("terminal", "credential")),
        (403, ("terminal", "credential")),
        (400, ("terminal", "http_4xx")),
        (404, ("terminal", "http_4xx")),
        ("no_endpoints", ("terminal", "unknown")),
        (None, ("terminal", "unknown")),
        (True, ("terminal", "unknown")),
        ("503", ("terminal", "unknown")),
    ],
)
def test_router_refusal_classified_by_inner_code(code, want):
    from evolution.tree.roles import role_call

    refusal = role_call.router_refusal({"code": code, "message": "private"}, http_status=200)
    assert classify_provider_error(refusal) == want


@pytest.mark.parametrize(
    "status,want",
    [
        (408, ("transient", "http_408")),
        (429, ("transient", "http_429")),
        (503, ("transient", "http_5xx")),
        (401, ("terminal", "credential")),
        (403, ("terminal", "credential")),
        (400, ("terminal", "http_4xx")),
        (404, ("terminal", "http_4xx")),
    ],
)
def test_status_errors_classify_as_before(status, want):
    assert (
        classify_provider_error(_bodied_error(status, {"error": {"code": "invalid_api_key"}}))
        == want
    )


def test_router_refusal_str_has_no_message():
    from evolution.tree.roles import role_call

    refusal = role_call.router_refusal(
        {"code": 503, "message": "PRIVATE-SENTINEL"}, http_status=200
    )
    assert str(refusal) == "router_refusal"
    assert "PRIVATE-SENTINEL" not in repr(refusal)
    assert "PRIVATE-SENTINEL" not in str(refusal.args)
    assert refusal.message == "PRIVATE-SENTINEL"


@pytest.mark.parametrize(
    "code,want",
    [
        (503, "503"),
        ("no_endpoints", "no_endpoints"),
        ("unsafe code!", None),
        (None, None),
        (True, None),
    ],
)
def test_provider_error_detail_reads_router_refusal(code, want):
    from evolution.tree.roles import role_call

    refusal = role_call.router_refusal({"code": code}, http_status=200)
    assert role_call.provider_error_detail(refusal) == (200, want)
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = refusal
    assert role_call.provider_error_detail(wrapped) == (200, want)


@pytest.mark.parametrize(
    "provider,want",
    [
        ("CoreWeave", "CoreWeave"),
        ("provider:route/1 - v2", "provider:route/1 - v2"),
        ("provider\nmessage", None),
        ("x" * 65, None),
        (None, None),
    ],
)
def test_router_refusal_provider_note_is_safe(provider, want):
    from evolution.tree.roles import role_call

    refusal = role_call.router_refusal({"metadata": {"provider_name": provider}}, http_status=200)
    assert refusal.safe_provider == want
    assert refusal.provider_name == provider


@pytest.mark.parametrize("kind", ["sdk", "namespace", "mapping", "extra"])
@pytest.mark.parametrize("choices", [None, [], [object()]])
def test_error_body_detection(kind, choices):
    from types import SimpleNamespace

    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    from evolution.tree.roles import role_call

    error = {"code": 503, "message": "No endpoints found that can handle the requested parameters."}
    document = {"error": error}
    if choices is not None:
        document["choices"] = choices
    if kind == "sdk":
        response = construct_type(value=document, type_=ChatCompletion)
    elif kind == "namespace":
        response = SimpleNamespace(**document)
    elif kind == "extra":
        response = SimpleNamespace(choices=choices, model_extra={"error": error})
    else:
        response = document
    assert role_call.error_body(response) == (None if choices else error)
    assert role_call.error_body(SimpleNamespace(choices=[])) is None
    assert role_call.error_body({"error": "not an object"}) is None


@pytest.mark.parametrize("wrapped", [True, False])
def test_status_error_body_accepts_wrapped_and_sdk_normalized_objects(wrapped):
    from evolution.tree.roles import role_call

    error = {"code": "invalid_api_key", "message": "private"}
    body = {"error": error} if wrapped else error
    assert role_call.status_error_body(body) == error


@pytest.mark.parametrize("body", [None, "not JSON", ["not an object"]])
def test_status_error_body_ignores_non_objects(body):
    from evolution.tree.roles import role_call

    assert role_call.status_error_body(body) is None


@pytest.mark.parametrize("choices", [None, [], [object()]])
def test_response_error_detection_requires_top_level_error(choices):
    from types import SimpleNamespace

    from evolution.tree.roles import role_call

    fields = {"code": 503, "message": "completion metadata", "choices": choices}
    assert role_call.error_body(fields) is None
    assert role_call.error_body(SimpleNamespace(**fields)) is None


@pytest.mark.parametrize(
    "code,attempts,reason",
    [
        (503, 3, "provider_unavailable"),
        (401, 1, "provider_rejected"),
        ("503", 1, "provider_rejected"),
    ],
)
async def test_call_with_retry_retries_transient_refusals_and_rejects_terminal(
    code, attempts, reason
):
    from evolution.tree.roles import role_call

    clock = FakeClock()
    seen = []
    calls = 0

    async def send(timeout):
        nonlocal calls
        calls += 1
        raise role_call.router_refusal(
            {"code": code, "message": "PRIVATE-SENTINEL"}, http_status=200
        )

    with pytest.raises(RoleCallExhausted) as caught:
        await call_with_retry(
            send,
            policy=RoleCallRetry.from_payload(POLICY),
            role="editor",
            request_id="t1",
            deadline=1000,
            clock=clock,
            sleep=clock.sleep,
            rng=lambda: 0,
            on_attempt=seen.append,
        )
    assert caught.value.reason == reason
    assert calls == attempts == len(seen)
    assert all(t.http_status == 200 for t in seen)
    assert "PRIVATE-SENTINEL" not in repr(seen)


@pytest.mark.parametrize(
    "status,want",
    [
        (401, ("terminal", "credential")),
        (400, ("terminal", "http_4xx")),
        (503, ("transient", "http_5xx")),
    ],
)
def test_router_refusal_non_2xx_http_status_has_precedence(status, want):
    from evolution.tree.roles import role_call

    refusal = role_call.router_refusal({"code": 429}, http_status=status)
    assert classify_provider_error(refusal) == want
