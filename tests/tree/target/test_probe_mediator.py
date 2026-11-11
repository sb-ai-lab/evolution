"""Contracts for the operation-scoped external probe mediator and client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.target import probe_session as probe_session_module
from evolution.tree.target.config import ProbeSection
from evolution.tree.target.probe_client import ProbeClientError, call
from evolution.tree.target.probe_mediator import (
    MAX_ACTIVE_HANDLERS,
    MAX_REQUEST_BYTES,
    ProbeMediator,
)
from evolution.tree.target.probe_session import ProbeSession
from tests.probe_fakes import ProbeHTTPServer

pytestmark = pytest.mark.slow


def config(base_url: str, **changes: Any) -> ProbeSection:
    values: dict[str, Any] = {
        "base_url": base_url,
        "health_path": "/health",
        "methods": ("GET", "POST"),
        "budget": 4,
        "request_timeout_seconds": 2.0,
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
        "deadline_at": asyncio.get_running_loop().time() + 5,
        "append": events.append,
    }
    values.update(changes)
    return ProbeSession(probe, **values)


async def raw_request(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), timeout=3)
    writer.close()
    await writer.wait_closed()
    return response


def http_request(path: str, body: bytes, **headers: str) -> bytes:
    selected = {
        "Host": "127.0.0.1",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
        **headers,
    }
    lines = [f"POST {path} HTTP/1.1", *(f"{key}: {value}" for key, value in selected.items())]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def response_json(response: bytes) -> object:
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


async def control_character_target() -> tuple[asyncio.Server, str]:
    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in header.split(b"\r\n")[1:]:
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length:
                await reader.readexactly(length)
            body = b"\0" * 4000
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 4000\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_listener_prepares_before_session_then_binds_exactly_once(tmp_path: Path) -> None:
    with ProbeHTTPServer() as target:
        events: list[dict[str, object]] = []
        mediator = ProbeMediator(capability="c" * 43)
        url = await mediator.start()
        assert url == mediator.url
        assert await mediator.start() == url
        assert mediator.capability == "c" * 43
        assert target.base_url not in url

        # The unexposed, unbound listener cannot dispatch or consume accounting.
        unbound = await raw_request(
            mediator.port,
            http_request(mediator.path, b'{"method":"GET","path":"/ok","body":null}'),
        )
        assert unbound == b""

        owner = session(tmp_path, config(target.base_url), events)
        mediator.bind_session(owner)
        with pytest.raises(RuntimeError, match="already bound"):
            mediator.bind_session(owner)

        result = await asyncio.to_thread(
            call,
            url,
            {"method": "GET", "path": "/ok", "body": None},
        )
        assert result == {"seq": 1, "status": 200, "body": "ok", "truncated": False}
        assert [event["seq"] for event in events] == [1]
        assert await mediator.close()


@pytest.mark.asyncio
async def test_bind_is_owner_loop_only_and_refused_after_abort(tmp_path: Path) -> None:
    with ProbeHTTPServer() as target:
        owner = session(tmp_path, config(target.base_url), [])
        mediator = ProbeMediator(capability="b" * 43)
        await mediator.start()
        with pytest.raises(RuntimeError, match="non-owning event loop"):
            await asyncio.to_thread(mediator.bind_session, owner)
        mediator.abort()
        with pytest.raises(RuntimeError, match="closed"):
            mediator.bind_session(owner)
        with pytest.raises(RuntimeError, match="closed"):
            await mediator.start()
        assert await mediator.close()


@pytest.mark.asyncio
async def test_failed_start_expires_capability_and_cannot_reuse_the_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fail_start(*_args: object, **_kwargs: object) -> None:
        raise OSError("listener unavailable")

    monkeypatch.setattr(asyncio, "start_server", fail_start)
    owner = session(tmp_path, config("http://127.0.0.1:1"), [])
    mediator = ProbeMediator(owner, capability="z" * 43)
    with pytest.raises(OSError, match="listener unavailable"):
        await mediator.start()
    assert mediator.closed
    assert owner.closed
    with pytest.raises(RuntimeError, match="closed"):
        _ = mediator.capability
    with pytest.raises(RuntimeError, match="closed"):
        await mediator.start()


@pytest.mark.asyncio
async def test_native_http_and_real_client_subprocess_have_one_session_contract(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as target:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(target.base_url), events)
        mediator = ProbeMediator(owner, capability="d" * 43)
        url = await mediator.start()

        native = await owner.request({"method": "GET", "path": "/status/418", "body": None})
        over_http = await asyncio.to_thread(
            call,
            url,
            {"method": "GET", "path": "/status/418", "body": None},
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "evolution.tree.target.probe_client",
            "--url",
            url,
            json.dumps({"method": "POST", "path": "/echo", "body": {"n": 1}}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=5)

        assert native == {"seq": 1, "status": 418, "body": "status-418", "truncated": False}
        assert over_http == {
            "seq": 2,
            "status": 418,
            "body": "status-418",
            "truncated": False,
        }
        assert child.returncode == 0, stderr.decode("utf-8", "replace")
        assert stderr == b""
        assert json.loads(stdout) == {
            "seq": 3,
            "status": 200,
            "body": '{"n":1}',
            "truncated": False,
        }
        assert [event["seq"] for event in events] == [1, 2, 3]
        assert await mediator.close()


@pytest.mark.asyncio
async def test_control_character_limit_has_native_http_and_subprocess_parity(
    tmp_path: Path,
) -> None:
    target, base_url = await control_character_target()
    events: list[dict[str, object]] = []
    owner = session(tmp_path, config(base_url, budget=3), events)
    mediator = ProbeMediator(owner, capability="n" * 43)
    url = await mediator.start()
    try:
        native = await owner.request({"method": "GET", "path": "/controls", "body": None})
        try:
            over_http: object = await asyncio.to_thread(
                call,
                url,
                {"method": "GET", "path": "/controls", "body": None},
            )
        except ProbeClientError as exc:
            over_http = exc
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "evolution.tree.target.probe_client",
            "--url",
            url,
            json.dumps({"method": "GET", "path": "/controls", "body": None}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=5)

        expected = {"seq": 1, "status": 200, "body": "\0" * 4000, "truncated": False}
        assert native == expected
        assert over_http == {**expected, "seq": 2}
        assert child.returncode == 0, stderr.decode("utf-8", "replace")
        assert stderr == b""
        assert json.loads(stdout) == {**expected, "seq": 3}
        assert [event["seq"] for event in events] == [1, 2, 3]
    finally:
        await mediator.close()
        target.close()
        await target.wait_closed()


@pytest.mark.asyncio
async def test_wrong_and_expired_capabilities_close_without_a_json_reply(tmp_path: Path) -> None:
    with ProbeHTTPServer() as target:
        owner = session(tmp_path, config(target.base_url), [])
        mediator = ProbeMediator(owner, capability="e" * 43)
        await mediator.start()
        wrong = await raw_request(
            mediator.port,
            http_request("/" + "x" * 43 + "/probe", b"{}"),
        )
        port = mediator.port
        assert wrong == b""
        assert owner.budget_remaining == 4

        assert await mediator.close()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [
        b"POST /cap/probe HTTP/1.1\r\nTransfer-Encoding: chunked\r\n"
        b"Content-Type: application/json\r\n\r\n0\r\n\r\n",
        b"POST /cap/probe HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 0\r\n"
        b"Content-Type: application/json\r\n\r\n",
        b"POST /cap/probe HTTP/1.1\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\n{}",
        (
            f"POST /cap/probe HTTP/1.1\r\nContent-Length: {MAX_REQUEST_BYTES + 1}\r\n"
            "Content-Type: application/json\r\n\r\n"
        ).encode(),
        http_request(
            "/cap/probe",
            b'{"method":"GET","method":"POST","path":"/ok","body":null}',
        ),
        b"POST /cap/probe HTTP/1.1\r\nX-Fill: "
        + b"x" * (16 * 1024)
        + b"\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}",
        http_request("/cap/probe", b"[" * 2000 + b"0" + b"]" * 2000),
        b"POST /cap/probe HTTP/1.1\r\nContent-Length: "
        + b"9" * 5000
        + b"\r\nContent-Type: application/json\r\n\r\n",
        http_request("/cap/probe", b"[" * 60_000 + b"0" + b"]" * 60_000),
    ],
)
async def test_hostile_framing_is_refused_before_session_accounting(
    tmp_path: Path, wire: bytes
) -> None:
    with ProbeHTTPServer() as target:
        owner = session(tmp_path, config(target.base_url), [])
        mediator = ProbeMediator(owner, capability="k" * 43)
        await mediator.start()
        wire = wire.replace(b"/cap/probe", mediator.path.encode("ascii"))
        response = await raw_request(mediator.port, wire)
        assert response_json(response) == {
            "error": "rejected",
            "detail": "probe request rejected",
        }
        assert owner.budget_remaining == 4
        assert not target.calls
        assert await mediator.close()


@pytest.mark.asyncio
async def test_handler_preallocation_limit_refuses_excess_slow_readers(tmp_path: Path) -> None:
    with ProbeHTTPServer() as target:
        owner = session(tmp_path, config(target.base_url), [])
        mediator = ProbeMediator(owner, capability="f" * 43)
        await mediator.start()
        peers = [await asyncio.open_connection("127.0.0.1", mediator.port) for _ in range(9)]
        try:
            for _reader, writer in peers:
                writer.write(b"POST ")
                await writer.drain()
            refused = await asyncio.wait_for(peers[-1][0].read(), timeout=1)
            assert refused == b""
            assert mediator.active_handlers == MAX_ACTIVE_HANDLERS
            assert owner.budget_remaining == 4
        finally:
            for _reader, writer in peers:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in peers))
        assert await mediator.close()


@pytest.mark.asyncio
async def test_concurrent_clients_share_dispatch_budget_and_contiguous_events(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as target:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(target.base_url, budget=2), events)
        mediator = ProbeMediator(owner, capability="g" * 43)
        url = await mediator.start()
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    call,
                    url,
                    {"method": "GET", "path": "/slow", "body": None},
                )
                for _ in range(5)
            )
        )
        assert sum("status" in result for result in results) == 2
        assert sum(result.get("error") == "budget_exhausted" for result in results) == 3
        assert [event["seq"] for event in events] == [1, 2]
        assert len(target.calls) == 2
        assert await mediator.close()


@pytest.mark.asyncio
async def test_client_disconnect_after_dispatch_does_not_undo_accounting(tmp_path: Path) -> None:
    with ProbeHTTPServer() as target:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(target.base_url), events)
        mediator = ProbeMediator(owner, capability="h" * 43)
        await mediator.start()
        _reader, writer = await asyncio.open_connection("127.0.0.1", mediator.port)
        writer.write(
            http_request(
                mediator.path,
                b'{"method":"GET","path":"/slow","body":null}',
            )
        )
        await writer.drain()
        assert await asyncio.to_thread(target.called.wait, 1), "must disconnect after dispatch"
        writer.close()
        await writer.wait_closed()

        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.01)
        assert [event["seq"] for event in events] == [1]
        assert owner.budget_remaining == 3
        assert len(target.calls) == 1
        assert await mediator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", ["close", "half_close", "trailing_data"])
async def test_abandoned_queued_client_never_dispatches(tmp_path: Path, abandon: str) -> None:
    with ProbeHTTPServer() as target:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(target.base_url), events)
        mediator = ProbeMediator(owner)
        await mediator.start()
        # Hold dispatch without spending a budget unit; the peer must leave
        # the queue before the lock becomes available.
        await owner._serial.acquire()
        reader, writer = await asyncio.open_connection("127.0.0.1", mediator.port)
        try:
            writer.write(http_request(mediator.path, b'{"method":"POST","path":"/echo","body":1}'))
            await writer.drain()
            async with asyncio.timeout(1):
                while not owner._serial._waiters:
                    await asyncio.sleep(0.001)
            if abandon == "close":
                writer.close()
                await writer.wait_closed()
            elif abandon == "half_close":
                writer.write_eof()
            else:
                writer.write(b"unexpected second request")
                await writer.drain()
            async with asyncio.timeout(0.3):
                while mediator.active_handlers:
                    await asyncio.sleep(0.001)
            if abandon != "close":
                assert await reader.read() == b""
            owner._serial.release()
            # A new live caller proves the abandoned slot did not take seq/budget.
            result = await asyncio.to_thread(
                call, mediator.url, {"method": "GET", "path": "/ok", "body": None}
            )
            assert result["seq"] == 1
            assert owner.budget_remaining == 3
            assert [event["seq"] for event in events] == [1]
            assert [entry.path for entry in target.calls] == ["/ok"]
            assert (
                len(list((tmp_path / "reflector-logs" / "probes" / "r02-x01").glob("*.json"))) == 1
            )
        finally:
            if owner._serial.locked() and not owner.active:
                owner._serial.release()
            writer.close()
            await writer.wait_closed()
            assert await mediator.close()


@pytest.mark.asyncio
async def test_sealed_helpers_wait_for_long_and_queued_calls_under_operation_deadline(
    tmp_path: Path,
) -> None:
    import time

    from evolution.tree.roles.reflection_role_helpers import install_probe_helper
    from evolution.tree.roles.reflector_contracts import ProbeAccess

    with ProbeHTTPServer(delay=1.0) as target:
        events: list[dict[str, object]] = []
        owner = session(
            tmp_path,
            config(target.base_url, request_timeout_seconds=120),
            events,
            deadline_at=asyncio.get_running_loop().time() + 1200,
        )
        mediator = ProbeMediator(owner)
        await mediator.start()
        control = tmp_path / "helper"
        helpers = install_probe_helper(
            control,
            ProbeAccess(mediator.url, 4, ("GET",), None),
            deadline_at=time.monotonic() + 1200,
        )
        # Accelerate only the real client's socket timeout: its old 60-second
        # cutoff becomes 0.5 s, still below the 1 s active call and queued completion.
        # The 1200-second campaign budget becomes 10 s. Actual HTTP/queueing and
        # sealed launcher execution remain real, without a 90-second test.
        runner = tmp_path / "accelerated_socket.py"
        runner.write_text("""import runpy, sys, urllib.request
original = urllib.request.OpenerDirector.open
def accelerated(self, request, data=None, timeout=None):
    return original(self, request, data=data, timeout=timeout / 120)
urllib.request.OpenerDirector.open = accelerated
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
""")
        processes = []
        try:
            for _ in range(2):
                processes.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-I",
                        str(runner),
                        helpers.descriptors["probe_request"][-1],
                        json.dumps({"method": "GET", "path": "/slow", "body": None}),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                )
            replies = await asyncio.wait_for(
                asyncio.gather(*(p.communicate() for p in processes)), 15
            )
            results = [json.loads(stdout) for stdout, _stderr in replies]
            assert all(result.get("status") == 200 for result in results), results
            assert sorted(result["seq"] for result in results) == [1, 2]
            assert all(stderr == b"" for _stdout, stderr in replies)
            assert [event["seq"] for event in events] == [1, 2]
            assert owner.budget_remaining == 2
            helpers.verify()
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            assert await mediator.close()


@pytest.mark.asyncio
async def test_persistence_failure_closes_wire_and_invalidates_later_clients(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as target:
        fault = OSError("disk unavailable")

        def fail_write(_path: Path, _document: object) -> None:
            raise fault

        owner = session(
            tmp_path,
            config(target.base_url),
            [],
            diagnostic_writer=fail_write,
        )
        mediator = ProbeMediator(owner, capability="i" * 43)
        await mediator.start()
        first = await raw_request(
            mediator.port,
            http_request(
                mediator.path,
                b'{"method":"GET","path":"/ok","body":null}',
            ),
        )
        assert first == b""
        assert mediator.closed
        assert owner.fault is fault
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", mediator.port)
        with pytest.raises(OSError, match="disk unavailable"):
            owner.raise_if_failed()
        assert await mediator.close()


@pytest.mark.asyncio
async def test_close_cuts_transport_but_drains_reserved_call_before_return(
    tmp_path: Path,
) -> None:
    with ProbeHTTPServer() as target:
        events: list[dict[str, object]] = []
        owner = session(tmp_path, config(target.base_url), events)
        mediator = ProbeMediator(owner, capability="j" * 43)
        url = await mediator.start()
        client = asyncio.create_task(
            asyncio.to_thread(
                call,
                url,
                {"method": "GET", "path": "/slow", "body": None},
            )
        )
        for _ in range(100):
            if owner.active:
                break
            await asyncio.sleep(0.01)
        assert owner.active
        assert await mediator.close()
        with pytest.raises(ProbeClientError, match="unavailable"):
            await client
        assert len(events) == 1
        assert events[0]["error"] == "context_exhausted"
        settled = list(events)
        await asyncio.sleep(0.2)
        assert events == settled


@pytest.mark.asyncio
async def test_close_bounds_stalled_listener_and_revokes_late_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport_release = asyncio.Event()
    transport_started = asyncio.Event()
    listener_release = asyncio.Event()

    async def stubborn_transport(*_args: object, **_kwargs: object) -> object:
        transport_started.set()
        while not transport_release.is_set():
            try:
                await transport_release.wait()
            except asyncio.CancelledError:
                pass
        return probe_session_module._Response(200, b"late", False)

    monkeypatch.setattr(probe_session_module, "_http_exchange", stubborn_transport)
    events: list[dict[str, object]] = []
    owner = session(tmp_path, config("http://127.0.0.1:9"), events)
    mediator = ProbeMediator(owner, capability="q" * 43)
    url = await mediator.start()
    real_server = mediator._server
    assert real_server is not None

    class StalledServer:
        def close(self) -> None:
            real_server.close()

        async def wait_closed(self) -> None:
            await listener_release.wait()

    mediator._server = StalledServer()  # type: ignore[assignment]
    client = asyncio.create_task(
        asyncio.to_thread(call, url, {"method": "GET", "path": "/ok", "body": None})
    )
    await transport_started.wait()
    started_at = asyncio.get_running_loop().time()
    assert await asyncio.wait_for(mediator.close(grace_seconds=0.03), timeout=0.2) is False
    elapsed = asyncio.get_running_loop().time() - started_at
    assert 0.02 <= elapsed < 0.15
    assert mediator.closed and owner.closed
    with pytest.raises(RuntimeError, match="closed"):
        _ = mediator.capability
    assert await owner.request({"method": "GET", "path": "/later", "body": None}) == {
        "error": "context_exhausted",
        "detail": "probe request context exhausted",
    }

    transport_release.set()
    with pytest.raises(ProbeClientError, match="unavailable"):
        await client
    await asyncio.sleep(0)
    assert events == []
    listener_release.set()
    assert await mediator.close(grace_seconds=0.2)


@pytest.mark.asyncio
async def test_close_revokes_authority_before_false_when_session_close_is_delayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport_release = asyncio.Event()
    transport_started = asyncio.Event()

    async def stubborn_transport(*_args: object, **_kwargs: object) -> object:
        transport_started.set()
        while not transport_release.is_set():
            try:
                await transport_release.wait()
            except asyncio.CancelledError:
                pass
        return probe_session_module._Response(200, b"late", False)

    monkeypatch.setattr(probe_session_module, "_http_exchange", stubborn_transport)
    events: list[dict[str, object]] = []
    owner = session(tmp_path, config("http://127.0.0.1:9"), events)
    mediator = ProbeMediator(owner, capability="t" * 43)
    url = await mediator.start()
    client = asyncio.create_task(
        asyncio.to_thread(call, url, {"method": "GET", "path": "/ok", "body": None})
    )
    await transport_started.wait()

    original_close = owner.close

    async def delayed_close(*, grace_seconds: float = 5.0) -> bool:
        await asyncio.sleep(0.05)
        return await original_close(grace_seconds=grace_seconds)

    monkeypatch.setattr(owner, "close", delayed_close)
    try:
        assert await mediator.close(grace_seconds=0.01) is False
        assert owner._append_revoked is True
    finally:
        transport_release.set()
        await asyncio.gather(client, return_exceptions=True)
        monkeypatch.setattr(owner, "close", original_close)
        await mediator.close(grace_seconds=0.1)
    assert isinstance(client.exception(), ProbeClientError)
    assert str(client.exception()) == "unavailable"
    await asyncio.sleep(0)
    assert events == []


@pytest.mark.asyncio
async def test_unexpected_handler_exception_is_retrieved_and_closes_mediator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ProbeHTTPServer() as target:
        owner = session(tmp_path, config(target.base_url), [])
        mediator = ProbeMediator(owner, capability="r" * 43)
        await mediator.start()
        contexts: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        prior = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))

        async def fail_read(_reader: asyncio.StreamReader) -> object:
            raise RuntimeError("internal parser fault")

        monkeypatch.setattr(mediator, "_read_request", fail_read)
        try:
            try:
                response = await raw_request(mediator.port, b"anything")
            except ConnectionResetError:
                response = b""
            await asyncio.sleep(0)
            assert response == b""
            assert mediator.closed
            assert contexts == []
        finally:
            loop.set_exception_handler(prior)
            assert await mediator.close() is False


def test_client_refuses_non_loopback_or_malformed_access_without_connecting() -> None:
    request = {"method": "GET", "path": "/ok", "body": None}
    for url in (
        "https://example.invalid/cap/probe",
        "http://localhost:8123/cap/probe",
        "http://127.0.0.1:8123/probe",
        "http://user@127.0.0.1:8123/cap/probe",
        "http://127.0.0.1:8123/cap/probe?q=1",
    ):
        with pytest.raises(ProbeClientError, match="invalid_probe_url"):
            call(url, request)


# The deepest JSON array a single command-line argument can carry: Linux caps
# one argument at 128 KiB including its terminator, and each level costs two
# bytes.
_MAX_ARGUMENT_DEPTH = (128 * 1024 - 2) // 2

# Run in a fresh interpreter, as the CLI is: the smallest depth whose text the
# CLI cannot decode and re-encode, or "none" up to the given bound.
_FIRST_CLI_RECURSION_FAILURE = """
import json, sys

def fails(depth):
    text = "[" * depth + "0" + "]" * depth
    try:
        json.dumps(json.loads(text), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except RecursionError:
        return True
    return False

low, high = 0, int(sys.argv[1])
if not fails(high):
    print("none")
else:
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if fails(middle) else (middle, high)
    print(high)
"""


def _first_encoding_failure(high: int) -> int | None:
    """The smallest nesting depth the client's encoder rejects here, if any up to `high`.

    From Python 3.14 the limit follows the C stack and the build rather than a
    fixed counter: at an 8 MiB stack a GCC build stops near 52 000 levels and a
    Clang build near 74 500, so a fixed depth is not a premise a test can hold.
    """

    def fails(depth: int) -> bool:
        value: object = 0
        for _ in range(depth):
            value = [value]
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except RecursionError:
            return True
        return False

    if not fails(high):
        return None
    low = 0
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if fails(middle) else (middle, high)
    return high


def test_client_normalizes_recursive_json_to_invalid_request() -> None:
    """An object too deep to encode is invalid_request before any connection."""

    failing = _first_encoding_failure(400_000)
    if failing is None:
        pytest.skip("this interpreter encodes 400 000 nested arrays, so no object is too deep")
    deep: object = 0
    for _ in range(failing + 1_000):
        deep = [deep]
    url = f"http://127.0.0.1:1/{'s' * 43}/probe"
    with pytest.raises(ProbeClientError, match="invalid_request") as caught:
        call(url, deep)
    assert isinstance(caught.value.__cause__, RecursionError)


@pytest.mark.asyncio
async def test_cli_normalizes_recursive_json_to_invalid_request() -> None:
    """A request the CLI cannot decode or re-encode is invalid_request, not a traceback."""

    measure = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _FIRST_CLI_RECURSION_FAILURE,
        str(_MAX_ARGUMENT_DEPTH),
        stdout=asyncio.subprocess.PIPE,
    )
    measured, _ = await asyncio.wait_for(measure.communicate(), timeout=60)
    assert measure.returncode == 0
    if measured.strip() == b"none":
        pytest.skip(
            "this interpreter decodes and re-encodes the deepest array one "
            f"command-line argument can carry ({_MAX_ARGUMENT_DEPTH} levels)"
        )
    # The CLI's own frames sit above the measurement's, so it fails no later.
    depth = min(int(measured) + 500, _MAX_ARGUMENT_DEPTH)
    encoded = "[" * depth + "0" + "]" * depth
    url = f"http://127.0.0.1:1/{'s' * 43}/probe"
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "evolution.tree.target.probe_client",
        "--url",
        url,
        encoded,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
    )
    stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=5)
    assert child.returncode == 2
    assert stdout == b""
    assert stderr == b"invalid_request\n"
