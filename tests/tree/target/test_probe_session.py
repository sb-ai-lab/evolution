"""Contracts for serialized outbound probes and their private diagnostics."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.target import probe_session as probe_session_module
from evolution.tree.target.config import ProbeSection
from evolution.tree.target.probe_session import (
    ProbeSession,
    ProbeSessionAborted,
    check_probe_health,
)
from tests.probe_fakes import ProbeHTTPServer, RefusingOrigin, decode_body

pytestmark = pytest.mark.slow


def config(base_url: str, **changes: Any) -> ProbeSection:
    values: dict[str, Any] = {
        "base_url": base_url,
        "health_path": "/health",
        "methods": ("GET", "POST"),
        "budget": 4,
        "request_timeout_seconds": 1.0,
        "description": None,
    }
    values.update(changes)
    return ProbeSection(**values)


def session(
    tmp_path: Path,
    probe: ProbeSection,
    events: list[dict[str, object]],
    **changes: Any,
) -> ProbeSession:
    values: dict[str, Any] = {
        "round": 2,
        "reflection_id": "r02-x01",
        "diagnostic_root": tmp_path / "reflector-logs",
        "deadline_at": asyncio.get_running_loop().time() + 3,
        "append": lambda payload: events.append(payload),
    }
    values.update(changes)
    return ProbeSession(probe, **values)


@pytest.mark.asyncio
async def test_http_status_body_unicode_path_and_request_framing(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)
        result = await owner.request({"method": "POST", "path": "/café?q=ü", "body": {"n": 1}})

        assert result == {"seq": 1, "status": 200, "body": '{"n":1}', "truncated": False}
        assert events == [
            {
                "round": 2,
                "reflection_id": "r02-x01",
                "seq": 1,
                "method": "POST",
                "path": "/café?q=ü",
                "status": 200,
                "error": None,
            }
        ]
        assert server.calls[0].path == "/caf%C3%A9?q=%C3%BC"
        assert decode_body(server.calls[0]) == {"n": 1}
        assert server.calls[0].headers["Content-Type"] == "application/json"
        assert server.calls[0].headers["Connection"] == "close"
        await owner.close()


@pytest.mark.asyncio
async def test_redirect_and_http_error_are_results_and_never_retried(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)

        redirected = await owner.request({"method": "GET", "path": "/redirect", "body": None})
        failed = await owner.request({"method": "GET", "path": "/status/500", "body": None})

        assert (redirected["status"], redirected["body"]) == (302, "redirect")
        assert (failed["status"], failed["body"]) == (500, "status-500")
        assert [call.path for call in server.calls] == ["/redirect", "/status/500"]
        assert [event["status"] for event in events] == [302, 500]


@pytest.mark.asyncio
async def test_chunked_response_is_incrementally_decoded(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        owner = session(tmp_path, config(server.base_url), [])
        result = await owner.request({"method": "GET", "path": "/chunked", "body": None})
        assert result == {"seq": 1, "status": 200, "body": "abcde", "truncated": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample",
    [
        {},
        {"method": "GET", "path": "/ok"},
        {"method": "GET", "path": "/ok", "body": None, "headers": {}},
        {"method": "PATCH", "path": "/ok", "body": None},
        {"method": "GET", "path": "relative", "body": None},
        {"method": "GET", "path": "//authority", "body": None},
        {"method": "GET", "path": "/a\\b", "body": None},
        {"method": "GET", "path": "/a#fragment", "body": None},
        {"method": "GET", "path": "/a/../b", "body": None},
        {"method": "GET", "path": "/a/%2e%2e/b", "body": None},
        {"method": "GET", "path": "/a/%2fb", "body": None},
        {"method": "GET", "path": "/a/%252f/b", "body": None},
        {"method": "GET", "path": "/a/%zz", "body": None},
        {"method": "GET", "path": "/a\x00b", "body": None},
        {"method": "GET", "path": "/" + "x" * 2048, "body": None},
        {"method": "GET", "path": "/ok", "body": float("nan")},
        {"method": "GET", "path": "/ok", "body": {1: "not a JSON object key"}},
    ],
)
async def test_rejected_requests_have_no_budget_event_or_dispatch(
    tmp_path: Path, sample: object
) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)

        assert await owner.request(sample) == {
            "error": "rejected",
            "detail": "probe request rejected",
        }
        assert owner.budget_remaining == 4
        assert events == []
        assert server.calls == []


@pytest.mark.asyncio
async def test_secret_in_journal_fields_is_refused_before_dispatch(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(server.base_url),
            events,
            secrets=("top-secret-value",),
        )
        result = await owner.request({"method": "GET", "path": "/top-secret-value", "body": None})
        assert result["error"] == "rejected"
        assert owner.budget_remaining == 4
        assert not server.calls


@pytest.mark.asyncio
async def test_body_depth_and_pre_and_post_redaction_limits_are_rejected(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(server.base_url),
            events,
            secrets=("s",),
        )
        deep: object = 0
        for _ in range(33):
            deep = [deep]
        requests = [
            {"method": "POST", "path": "/ok", "body": deep},
            {"method": "POST", "path": "/ok", "body": "x" * 65536},
            {"method": "POST", "path": "/ok", "body": "s" * 9000},
        ]
        for request in requests:
            result = await owner.request(request)
            assert result["error"] == "rejected"
        assert owner.budget_remaining == 4
        assert not server.calls


@pytest.mark.asyncio
async def test_response_is_bounded_decoded_and_redacted_in_reply_and_private_file(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(server.base_url),
            events,
            secrets=("top-secret-value",),
        )
        invalid = await owner.request({"method": "GET", "path": "/invalid-utf8", "body": None})
        secret = await owner.request({"method": "GET", "path": "/secret-response", "body": None})
        large = await owner.request({"method": "GET", "path": "/large", "body": None})

        assert invalid["body"] == "ok�tail"
        assert secret["body"] == "response has [REDACTED]"
        assert len(large["body"]) == 4000
        assert large["truncated"] is True
        private = json.loads((tmp_path / "reflector-logs/probes/r02-x01/2.json").read_text())
        assert private["outcome"]["body"] == "response has [REDACTED]"
        assert "top-secret-value" not in json.dumps(private)


@pytest.mark.asyncio
async def test_request_secret_is_sent_but_redacted_from_reply_and_diagnostic(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            secrets=("top-secret-value",),
        )
        result = await owner.request(
            {
                "method": "POST",
                "path": "/echo",
                "body": {"value": "top-secret-value"},
            }
        )
        assert decode_body(server.calls[0]) == {"value": "top-secret-value"}
        assert "top-secret-value" not in result["body"]
        private_raw = (tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text()
        assert "top-secret-value" not in private_raw


@pytest.mark.asyncio
async def test_private_clipping_cannot_retain_a_secret_prefix_at_64k_boundary(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            secrets=("top-secret-value",),
        )
        result = await owner.request({"method": "GET", "path": "/secret-boundary", "body": None})
        private_raw = (tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text()
        assert result["truncated"] is True
        assert "top-secret-value" not in private_raw
        assert not json.loads(private_raw)["outcome"]["body"].endswith("top-")


@pytest.mark.asyncio
async def test_transport_clipping_scrubs_secret_prefix_moved_forward_by_redaction(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            secrets=("top-secret-value",),
        )
        result = await owner.request(
            {"method": "GET", "path": "/secret-transport-boundary", "body": None}
        )
        private = json.loads((tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text())
        assert result == {
            "seq": 1,
            "status": 200,
            "body": "[REDACTED]",
            "truncated": True,
        }
        assert private["outcome"]["body"] == "[REDACTED]"
        assert private["outcome"]["response_bytes_truncated"] is True
        assert "top-" not in json.dumps(private)


@pytest.mark.asyncio
async def test_transport_clipping_redacts_complete_self_overlapping_secret_before_scrub(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            secrets=("abab",),
        )
        result = await owner.request(
            {"method": "GET", "path": "/secret-self-overlap-boundary", "body": None}
        )
        private = json.loads((tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text())
        assert result["body"] == "[REDACTED][REDACTED]"
        assert private["outcome"]["body"] == "[REDACTED][REDACTED]"
        assert "ab" not in json.dumps(private)


@pytest.mark.asyncio
async def test_transport_clipping_redacts_complete_cross_overlapping_secret_before_scrub(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            secrets=("tokenXYZ", "XYZabc"),
        )
        result = await owner.request(
            {"method": "GET", "path": "/secret-cross-overlap-boundary", "body": None}
        )
        private = json.loads((tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text())
        assert result["body"] == "[REDACTED][REDACTED]"
        assert private["outcome"]["body"] == "[REDACTED][REDACTED]"
        assert "token" not in json.dumps(private)


@pytest.mark.asyncio
async def test_transport_clipping_uses_raw_overlap_when_utf8_codepoint_is_partial(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            secrets=("top-€done",),
        )
        result = await owner.request(
            {"method": "GET", "path": "/secret-utf8-boundary", "body": None}
        )
        private = json.loads((tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text())
        assert result["body"] == "[REDACTED]"
        assert private["outcome"]["body"] == "[REDACTED]"
        assert "top-" not in json.dumps(private)


@pytest.mark.asyncio
async def test_private_default_is_owner_only_and_records_full_path_clipping_fact(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)
        path = "/" + "p" * 300
        await owner.request({"method": "GET", "path": path, "body": None})

        target = tmp_path / "reflector-logs/probes/r02-x01/1.json"
        document = json.loads(target.read_text())
        assert events[0]["path"] == path[:255] + "…"
        assert len(events[0]["path"]) == 256
        assert document["request"]["path"] == path
        assert document["path_clipped"] is True
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_requests_are_serialized_and_budget_is_reserved_at_dispatch(tmp_path: Path) -> None:
    with ProbeHTTPServer(delay=0.08) as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url, budget=2), events)
        requests = [
            asyncio.create_task(
                owner.request({"method": "GET", "path": f"/slow?i={index}", "body": None})
            )
            for index in range(3)
        ]
        results = await asyncio.gather(*requests)

        assert [result.get("seq") for result in results] == [1, 2, None]
        assert results[2] == {"error": "budget_exhausted", "detail": "probe budget exhausted"}
        assert server.max_active == 1
        assert len(server.calls) == len(events) == 2
        assert owner.budget_remaining == 0


@pytest.mark.asyncio
async def test_third_transport_failure_kills_session_without_fourth_dispatch(
    tmp_path: Path,
) -> None:
    origin = RefusingOrigin()
    try:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(origin.base_url, budget=5), events)
        results = [
            await owner.request({"method": "GET", "path": "/ok", "body": None}) for _ in range(4)
        ]
        assert [result["error"] for result in results] == [
            "transport",
            "transport",
            "instance_died",
            "instance_died",
        ]
        assert [event["error"] for event in events] == [
            "transport",
            "transport",
            "instance_died",
        ]
        assert owner.dead is True
        assert owner.budget_remaining == 2
    finally:
        origin.close()


@pytest.mark.asyncio
async def test_http_response_resets_transport_failure_streak(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url, budget=5), events)
        assert (await owner.request({"method": "GET", "path": "/drop", "body": None}))[
            "error"
        ] == "transport"
        assert (await owner.request({"method": "GET", "path": "/ok", "body": None}))[
            "status"
        ] == 200
        # A response reset means the next broken exchange is transport, not instance_died.
        assert (await owner.request({"method": "GET", "path": "/drop", "body": None}))[
            "error"
        ] == "transport"
        assert owner.dead is False


@pytest.mark.asyncio
async def test_endpoint_timeout_is_one_attempt_and_counts_toward_death(tmp_path: Path) -> None:
    with ProbeHTTPServer(delay=0.15) as server:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(server.base_url, budget=5, request_timeout_seconds=0.03),
            events,
        )
        results = [
            await owner.request({"method": "GET", "path": "/slow", "body": None}) for _ in range(4)
        ]
        assert [result["error"] for result in results] == [
            "transport",
            "transport",
            "instance_died",
            "instance_died",
        ]
        assert len(server.calls) == 3


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_or_resend_dispatched_call(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer(delay=0.08) as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)
        caller = asyncio.create_task(
            owner.request({"method": "GET", "path": "/slow", "body": None})
        )
        assert await asyncio.to_thread(server.called.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        while not events:
            await asyncio.sleep(0.01)
        assert await owner.close() is True
        assert len(server.calls) == 1
        assert len(events) == 1
        assert events[0]["status"] == 200


@pytest.mark.asyncio
async def test_abort_records_context_exhausted_for_dispatched_call(tmp_path: Path) -> None:
    with ProbeHTTPServer(delay=0.2) as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)
        caller = asyncio.create_task(
            owner.request({"method": "GET", "path": "/slow", "body": None})
        )
        assert await asyncio.to_thread(server.called.wait, 1)
        owner.abort()
        assert await caller == {
            "error": "context_exhausted",
            "detail": "probe request context exhausted",
        }
        assert await owner.close() is True
        assert events[0]["error"] == "context_exhausted"
        assert owner.closed is True
        assert owner.active is False


@pytest.mark.asyncio
async def test_abort_after_reservation_before_owned_task_starts_keeps_accounting(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)
        caller = asyncio.create_task(owner.request({"method": "GET", "path": "/ok", "body": None}))
        while owner.budget_remaining == 4:
            await asyncio.sleep(0)
        assert owner.active is True
        assert owner._transport_task is None
        owner.abort()
        assert (await caller)["error"] == "context_exhausted"
        assert owner.budget_remaining == 3
        assert [event["error"] for event in events] == ["context_exhausted"]
        assert await owner.close() is True


@pytest.mark.asyncio
async def test_stop_admission_wakes_queued_request_before_active_io_finishes(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer(delay=0.2) as server:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(server.base_url), events)
        active = asyncio.create_task(
            owner.request({"method": "GET", "path": "/slow", "body": None})
        )
        assert await asyncio.to_thread(server.called.wait, 1)
        queued = asyncio.create_task(
            owner.request({"method": "GET", "path": "/later", "body": None})
        )
        await asyncio.sleep(0)
        owner.stop_admission()
        assert await asyncio.wait_for(queued, 0.05) == {
            "error": "context_exhausted",
            "detail": "probe request context exhausted",
        }
        assert not active.done()
        assert (await active)["status"] == 200
        assert len(events) == 1


@pytest.mark.asyncio
async def test_close_grace_reports_live_work_and_revokes_late_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def stubborn_transport(*_args: object, **_kwargs: object) -> object:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return probe_session_module._Response(200, b"late", False)

    monkeypatch.setattr(probe_session_module, "_http_exchange", stubborn_transport)
    events: list[dict[str, object]] = []
    owner = session(tmp_path, config("http://127.0.0.1:9"), events)
    caller = asyncio.create_task(owner.request({"method": "GET", "path": "/ok", "body": None}))
    await started.wait()
    assert await owner.close(grace_seconds=0.01) is False
    assert owner.active is True
    release.set()
    with pytest.raises(ProbeSessionAborted):
        await caller
    await asyncio.sleep(0)
    assert events == []
    assert await owner.close(grace_seconds=0.1) is True


@pytest.mark.asyncio
async def test_explicit_event_authority_revocation_blocks_late_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def stubborn_transport(*_args: object, **_kwargs: object) -> object:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return probe_session_module._Response(200, b"late", False)

    monkeypatch.setattr(probe_session_module, "_http_exchange", stubborn_transport)
    events: list[dict[str, object]] = []
    owner = session(tmp_path, config("http://127.0.0.1:9"), events)
    caller = asyncio.create_task(owner.request({"method": "GET", "path": "/ok", "body": None}))
    await started.wait()

    try:
        owner.revoke_event_authority()
        assert owner.closed
        assert owner._append_revoked is True
    except BaseException:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        raise
    release.set()
    with pytest.raises(ProbeSessionAborted):
        await caller
    await asyncio.sleep(0)
    assert events == []
    assert await owner.close(grace_seconds=0.1) is True


@pytest.mark.asyncio
async def test_shared_deadline_before_dispatch_has_no_event_or_budget(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(server.base_url),
            events,
            deadline_at=asyncio.get_running_loop().time() - 1,
        )
        result = await owner.request({"method": "GET", "path": "/ok", "body": None})
        assert result["error"] == "context_exhausted"
        assert owner.budget_remaining == 4
        assert not events
        assert not server.calls


@pytest.mark.asyncio
async def test_shared_deadline_after_dispatch_records_context_without_changing_streak(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer(delay=0.15) as server:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(server.base_url, budget=4, request_timeout_seconds=1),
            events,
            deadline_at=asyncio.get_running_loop().time() + 0.03,
        )
        result = await owner.request({"method": "GET", "path": "/slow", "body": None})
        assert result["error"] == "context_exhausted"
        assert events[0]["error"] == "context_exhausted"
        assert owner.dead is False
        assert owner.budget_remaining == 3


@pytest.mark.asyncio
async def test_file_then_event_then_reply_order_runs_on_owner_loop(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        order: list[str] = []
        loop = asyncio.get_running_loop()

        def write(_path: Path, _document: dict[str, object]) -> None:
            assert asyncio.get_running_loop() is loop
            order.append("file")

        def append(_payload: dict[str, object]) -> None:
            assert asyncio.get_running_loop() is loop
            order.append("event")

        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            append=append,
            diagnostic_writer=write,
        )
        result = await owner.request({"method": "GET", "path": "/ok", "body": None})
        order.append("reply")
        assert result["status"] == 200
        assert order == ["file", "event", "reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["write", "append"])
async def test_persistence_failure_is_sticky_aborts_reply_and_prevents_later_dispatch(
    tmp_path: Path, failure_stage: str
) -> None:
    with ProbeHTTPServer() as server:
        original = OSError(28, f"{failure_stage} failed")
        writes: list[dict[str, object]] = []
        appends: list[dict[str, object]] = []

        def write(_path: Path, document: dict[str, object]) -> None:
            writes.append(document)
            if failure_stage == "write":
                raise original

        def append(payload: dict[str, object]) -> None:
            appends.append(payload)
            if failure_stage == "append":
                raise original

        owner = session(
            tmp_path,
            config(server.base_url),
            [],
            append=append,
            diagnostic_writer=write,
        )
        with pytest.raises(ProbeSessionAborted):
            await owner.request({"method": "GET", "path": "/ok", "body": None})
        with pytest.raises(ProbeSessionAborted):
            await owner.request({"method": "GET", "path": "/later", "body": None})
        assert len(server.calls) == 1
        assert len(writes) == 1
        assert len(appends) == (1 if failure_stage == "append" else 0)
        assert owner.fault is original
        with pytest.raises(OSError) as caught:
            owner.raise_if_failed()
        assert caught.value is original


@pytest.mark.asyncio
async def test_health_is_bounded_cancellable_and_has_no_session_side_effects() -> None:
    with ProbeHTTPServer(delay=0.15) as server:
        assert await check_probe_health(config(server.base_url)) is True
        assert await check_probe_health(config(server.base_url, health_path="/health-bad")) is False
        assert (
            await check_probe_health(
                config(server.base_url, health_path="/slow", request_timeout_seconds=0.02)
            )
            is False
        )
        cancelled = False

        def stop() -> bool:
            return cancelled

        task = asyncio.create_task(
            check_probe_health(config(server.base_url, health_path="/slow"), cancelled=stop)
        )
        await asyncio.sleep(0.01)
        cancelled = True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [call.method for call in server.calls] == ["GET", "GET", "GET", "GET"]
        assert all(not call.body for call in server.calls)


@pytest.mark.asyncio
async def test_session_is_bound_to_creating_loop(tmp_path: Path) -> None:
    with ProbeHTTPServer() as server:
        owner = session(tmp_path, config(server.base_url), [])

        def use_another_loop() -> type[BaseException] | None:
            async def invoke() -> None:
                await owner.request({"method": "GET", "path": "/ok", "body": None})

            try:
                asyncio.run(invoke())
            except BaseException as exc:
                return type(exc)
            return None

        assert await asyncio.to_thread(use_another_loop) is RuntimeError
        assert not server.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:8080",
        "https://127.0.0.1:8080",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:8080",
        "http://127.0.0.1:8080/path",
    ],
)
async def test_direct_dataclass_invalid_origins_are_refused_without_io(
    tmp_path: Path, base_url: str
) -> None:
    with pytest.raises(ValueError, match="probe base_url"):
        session(tmp_path, config(base_url), [])
    assert not (tmp_path / "reflector-logs").exists()
    with pytest.raises(ValueError, match="probe base_url"):
        await check_probe_health(config(base_url))


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_component", ["probes", "reflection"])
async def test_default_writer_refuses_symlinked_descendants(
    tmp_path: Path, unsafe_component: str
) -> None:
    admitted = tmp_path / "reflector-logs"
    admitted.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    if unsafe_component == "probes":
        (admitted / "probes").symlink_to(outside, target_is_directory=True)
    else:
        probes = admitted / "probes"
        probes.mkdir(mode=0o700)
        (probes / "r02-x01").symlink_to(outside, target_is_directory=True)
    with ProbeHTTPServer() as server:
        owner = ProbeSession(
            config(server.base_url),
            round=2,
            reflection_id="r02-x01",
            diagnostic_root=admitted,
            deadline_at=asyncio.get_running_loop().time() + 2,
            append=lambda _payload: None,
        )
        with pytest.raises(ProbeSessionAborted):
            await owner.request({"method": "GET", "path": "/ok", "body": None})
        assert not list(outside.iterdir())
        with pytest.raises(ValueError):
            owner.raise_if_failed()


def test_public_state_is_read_only() -> None:
    names = ("config", "deadline_at", "closed", "dead", "fault", "budget_remaining", "active")
    for name in names:
        assert isinstance(getattr(ProbeSession, name), property)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "body", "expected", "parsed"),
    [
        (None, '{"a": 1}', '{"a": 1}', False),
        ("send", '{"a": 1}', '{"a": 1}', False),
        ("parse", '{"a": 1}', {"a": 1}, True),
        ("parse", "not json", "not json", False),
        ("parse", {"a": 1}, {"a": 1}, False),
        ("parse", "[1,true]", [1, True], True),
    ],
)
async def test_string_body_transmission_and_private_parse_record(
    tmp_path, mode, body, expected, parsed
):
    with ProbeHTTPServer() as server:
        events = []
        owner = session(tmp_path, config(server.base_url, string_body=mode), events)
        result = await owner.request({"method": "POST", "path": "/echo", "body": body})
        assert result["status"] == 200
        assert decode_body(server.calls[0]) == expected
        document = json.loads((tmp_path / "reflector-logs/probes/r02-x01/1.json").read_text())
        assert document.get("body_parsed", False) is parsed
        assert "body_parsed" not in events[0]
        await owner.close()
