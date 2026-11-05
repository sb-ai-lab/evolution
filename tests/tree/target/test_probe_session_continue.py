"""Connection establishment determines continue-mode probe accounting."""

import asyncio
import json

import pytest

from evolution.tree.target import probe_session as module
from tests.probe_fakes import ProbeHTTPServer, RefusingOrigin
from tests.tree.target.test_probe_session import config, session

REQUEST = {"method": "GET", "path": "/ok", "body": None}
UNAVAILABLE = {
    "error": "unavailable",
    "detail": "probe unavailable: continue without it or retry later",
}


@pytest.mark.parametrize(
    "kind", ["malformed", "oversized", "many", "symlink", "missing", "unreadable"]
)
def test_helper_call_log_is_bounded_and_best_effort(tmp_path, monkeypatch, kind):
    from evolution.tree.roles import reflection_role_helpers as helpers

    directory = tmp_path / ".evolution/probe"
    directory.mkdir(parents=True)
    path = directory / helpers.PROBE_CALL_LOG_NAME
    line = json.dumps(REQUEST).encode() + b"\n"
    expected = ()
    if kind == "malformed":
        path.write_bytes(b"{bad}\n{}\n" + line + b"\xff\n")
        expected = (REQUEST,)
    elif kind == "oversized":
        path.write_bytes(line + b" " * helpers.MAX_PROBE_CALL_LOG_BYTES)
    elif kind == "many":
        path.write_bytes(line * (helpers.MAX_PROBE_CALL_LOG_ENTRIES + 1))
        expected = (REQUEST,) * helpers.MAX_PROBE_CALL_LOG_ENTRIES
    elif kind == "symlink":
        other = tmp_path / "other"
        other.write_bytes(line)
        path.symlink_to(other)
    elif kind == "unreadable":
        path.write_bytes(line)
        original = helpers.os.open

        def unreadable(name, *args, **kwargs):
            if name == helpers.PROBE_CALL_LOG_NAME:
                raise PermissionError("unreadable log")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(helpers.os, "open", unreadable)
    assert helpers.read_probe_call_log(tmp_path, ".evolution") == expected


async def test_disabled_session_records_prior_helper_calls_after_role_deadline(tmp_path):
    events = []
    deadline = asyncio.get_running_loop().time() - 1
    owner = session(
        tmp_path,
        config("http://localhost:8099"),
        events,
        on_unavailable="continue",
        disabled=True,
        deadline_at=deadline,
    )
    try:
        await owner.record_unavailable_calls([REQUEST, {**REQUEST, "method": "PATCH"}, REQUEST])
        assert [event["error"] for event in events] == ["unreachable", "unreachable"]
        assert [event["seq"] for event in events] == [1, 2]
        assert owner.budget_remaining == 4
        assert owner.deadline_at == deadline
    finally:
        await owner.close()


async def test_suspend_session_bytes_unchanged(tmp_path):
    origin = RefusingOrigin()
    events = []
    try:
        owner = session(tmp_path, config(origin.base_url), events)
        results = [await owner.request(REQUEST) for _ in range(4)]
        assert [r["error"] for r in results] == [
            "transport",
            "transport",
            "instance_died",
            "instance_died",
        ]
        assert [e["error"] for e in events] == ["transport", "transport", "instance_died"]
        assert owner.budget_remaining == 1
        await owner.close()
    finally:
        origin.close()


async def test_continue_unreachable_is_free_and_recovers(tmp_path):
    origin = RefusingOrigin()
    port = origin.socket.getsockname()[1]
    events = []
    owner = session(tmp_path, config(origin.base_url), events, on_unavailable="continue")
    try:
        for _ in range(5):
            assert await owner.request(REQUEST) == UNAVAILABLE
        assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]
        assert [e["error"] for e in events] == ["unreachable"] * 5
        assert owner.budget_remaining == 4 and not owner.dead
        origin.close()

        async def answer(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(answer, "127.0.0.1", port):
            assert (await owner.request(REQUEST))["status"] == 200
        assert owner.budget_remaining == 3
        assert owner.availability() == {"unreachable": 5, "transport_failures": 0}
    finally:
        origin.close()
        await owner.close()


async def test_continue_reset_after_connect_is_charged_without_lockout(tmp_path):
    with ProbeHTTPServer() as server:
        events = []
        owner = session(
            tmp_path, config(server.base_url, budget=5), events, on_unavailable="continue"
        )
        try:
            for _ in range(4):
                assert await owner.request({**REQUEST, "path": "/drop"}) == UNAVAILABLE
            assert [e["error"] for e in events] == ["transport"] * 4
            assert owner.budget_remaining == 1 and not owner.dead
            assert (await owner.request(REQUEST))["status"] == 200
            assert owner.availability() == {"unreachable": 0, "transport_failures": 4}
        finally:
            await owner.close()


@pytest.mark.parametrize("context", [False, True])
async def test_continue_timeout_before_connect(tmp_path, monkeypatch, context):
    async def never_connect(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "open_connection", never_connect)
    events = []
    owner = session(
        tmp_path,
        config("http://localhost:8099", request_timeout_seconds=1 if context else 0.02),
        events,
        on_unavailable="continue",
        deadline_at=asyncio.get_running_loop().time() + (0.02 if context else 3),
    )
    try:
        result = await owner.request(REQUEST)
        assert result == (
            {"error": "context_exhausted", "detail": "probe request context exhausted"}
            if context
            else UNAVAILABLE
        )
        assert events[0]["error"] == ("context_exhausted" if context else "unreachable")
        assert owner.budget_remaining == (3 if context else 4)
    finally:
        await owner.close()


async def test_continue_timeout_after_connect_is_charged(tmp_path):
    with ProbeHTTPServer(delay=0.1) as server:
        events = []
        owner = session(
            tmp_path,
            config(server.base_url, request_timeout_seconds=0.02),
            events,
            on_unavailable="continue",
        )
        try:
            assert await owner.request({**REQUEST, "path": "/slow"}) == UNAVAILABLE
            assert events[0]["error"] == "transport"
            assert owner.budget_remaining == 3
        finally:
            await owner.close()


async def test_disabled_session_never_dispatches(tmp_path):
    with ProbeHTTPServer() as server:
        events = []
        owner = session(
            tmp_path, config(server.base_url), events, on_unavailable="continue", disabled=True
        )
        try:
            for _ in range(5):
                assert await owner.request(REQUEST) == UNAVAILABLE
            assert server.calls == []
            assert owner.budget_remaining == 4
            assert [e["error"] for e in events] == ["unreachable"] * 5
            assert owner.availability() == {"unreachable": 5, "transport_failures": 0}
            assert module.UNAVAILABLE_RESULT == UNAVAILABLE
        finally:
            await owner.close()
