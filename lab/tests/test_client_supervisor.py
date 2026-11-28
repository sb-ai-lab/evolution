"""Synthetic V2 coverage for the private H1 supervisor wire protocol."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import sys
from types import SimpleNamespace

import pytest
import psutil
from ownership_modes import ownership_mode  # noqa: F401
from evolution_lab._ownership import select_mode

TRUE = shutil.which("true") or "/usr/bin/true"


async def _start_h1(tmp_path, core_argv, *, supervisor_env=None, stdout=None, supervisor_path=None):
    from evolution_lab import _supervisor
    from evolution_lab._client_protocol import (
        CommandChannel,
        open_event_channel,
        supervisor_invocation,
    )

    command_read, command_write = os.pipe()
    event_read, event_write = os.pipe()
    argv = supervisor_invocation(
        Path(supervisor_path or _supervisor.__file__), command_read, event_write, "operation-1", core_argv,
        ownership=select_mode()
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            pass_fds=(command_read, event_write),
            env=supervisor_env,
            stdout=stdout,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    finally:
        os.close(command_read)
        os.close(event_write)
    commands = CommandChannel(command_write)
    events = await open_event_channel(event_read, "operation-1")
    return process, commands, events


async def _event(events, kind):
    while True:
        item = await asyncio.wait_for(events.read(), timeout=3)
        assert item is not None, f"event stream ended before {kind}"
        assert set(item) == {"schema", "operation_id", "kind", "data"}
        assert item["schema"] == "lab-supervisor-event/1"
        assert item["operation_id"] == "operation-1"
        if item["kind"] == kind:
            return item["data"]


async def _finish(process, commands, events):
    commands.close()
    events.close()
    if process.returncode is None:
        os.kill(process.pid, signal.SIGKILL)
    # Process.wait() can finish before asyncio has delivered EOF on stderr.
    # Drain it so no subprocess transport callbacks survive the test loop.
    await process.communicate()


@pytest.mark.usefixtures("ownership_mode")
async def test_config_can_exceed_pipe_buf_and_cancel_before_go_prevents_spawn(tmp_path):
    marker = tmp_path / "spawned"
    core = (sys.executable, "-I", "-c", f"open({str(marker)!r}, 'w').close()")
    process, commands, events = await _start_h1(tmp_path, core)
    try:
        environment = {"PATH": os.environ["PATH"], "LARGE": "x" * 100_000}
        await asyncio.wait_for(
            commands.send_config("operation-1", environment, grace_seconds=0.1),
            timeout=3,
        )
        assert await _event(events, "ready") == {}
        assert not marker.exists()

        commands.send_cancel("operation-1")
        assert await _event(events, "spawn_skipped") == {
            "stop_effect": "prevented_start"
        }
        assert await _event(events, "cleanup_complete") == {"value": True}
        assert await asyncio.wait_for(process.wait(), timeout=3) == 0
        assert not marker.exists()
        assert await events.read() is None
    finally:
        await _finish(process, commands, events)


@pytest.mark.usefixtures("ownership_mode")
async def test_command_eof_before_go_prevents_spawn_and_reaps_supervisor(tmp_path):
    marker = tmp_path / "spawned"
    core = (sys.executable, "-I", "-c", f"open({str(marker)!r}, 'w').close()")
    process, commands, events = await _start_h1(tmp_path, core)
    try:
        await commands.send_config(
            "operation-1", {"PATH": os.environ["PATH"]}, grace_seconds=0.1
        )
        await _event(events, "ready")
        commands.close()

        assert await _event(events, "spawn_skipped") == {
            "stop_effect": "prevented_start"
        }
        assert await _event(events, "cleanup_complete") == {"value": True}
        assert await asyncio.wait_for(process.wait(), timeout=3) == 0
        assert not marker.exists()
    finally:
        await _finish(process, commands, events)


@pytest.mark.usefixtures("ownership_mode")
async def test_only_go_spawns_and_core_receives_exact_config_environment(tmp_path):
    configured = {"PRIVATE_CANARY": "value-never-placed-in-argv"}
    ambient = dict(os.environ)
    ambient["SUPERVISOR_ONLY"] = "must-not-reach-core"
    process, commands, events = await _start_h1(
        tmp_path, ("/usr/bin/env", "-0"), supervisor_env=ambient,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        await commands.send_config("operation-1", configured, grace_seconds=0.1)
        await _event(events, "ready")
        commands.send_go("operation-1")
        assert await _event(events, "core_started") == {}
        assert await _event(events, "core_exited") == {"exit_code": 0}
        assert await _event(events, "cleanup_complete") == {"value": True}
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)

        assert process.returncode == 0
        assert stderr == b""
        received = dict(item.split(b"=", 1) for item in stdout.split(b"\0") if item)
        token = received.pop(b"EVOLUTION_LAB_SCOPE", None)
        assert (token is not None) == (select_mode() == "group_tree_marker")
        assert received == {b"PRIVATE_CANARY": b"value-never-placed-in-argv"}
        assert not (tmp_path / "supervisor-result.json").exists()
    finally:
        await _finish(process, commands, events)


@pytest.mark.usefixtures("ownership_mode")
async def test_single_argument_core_command_is_a_valid_fast_exit(tmp_path):
    process, commands, events = await _start_h1(tmp_path, (TRUE,))
    try:
        await commands.send_config("operation-1", {}, grace_seconds=0.1)
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "core_started")
        assert await _event(events, "core_exited") == {"exit_code": 0}
        assert await _event(events, "cleanup_complete") == {"value": True}
        assert await asyncio.wait_for(process.wait(), timeout=3) == 0
    finally:
        await _finish(process, commands, events)


@pytest.mark.usefixtures("ownership_mode")
async def test_stop_then_force_reports_distinct_core_and_descendant_facts(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    script = tmp_path / "core.py"
    script.write_text(
        "import os,signal,subprocess,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *args: None)\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    process, commands, events = await _start_h1(
        tmp_path, (sys.executable, "-I", str(script))
    )
    descendant = None
    try:
        await commands.send_config(
            "operation-1", {"PATH": os.environ["PATH"]}, grace_seconds=0.05
        )
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "core_started")
        for _ in range(300):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        descendant = int(pidfile.read_text())

        commands.send_stop("operation-1")
        assert await _event(events, "stop_effect") == {"value": "delivered"}
        commands.send_force("operation-1")
        assert await _event(events, "core_force_requested") == {"value": True}
        assert (await _event(events, "core_exited"))["exit_code"] < 0
        assert await _event(events, "cleanup_complete") == {"value": True}
        await asyncio.wait_for(process.wait(), timeout=3)
        _assert_dead(descendant)
    finally:
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await _finish(process, commands, events)


@pytest.mark.usefixtures("ownership_mode")
async def test_event_channel_loss_after_go_cleans_owned_descendants(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    script = tmp_path / "fast-core.py"
    script.write_text(
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(child.pid))\n"
    )
    process, commands, events = await _start_h1(
        tmp_path, (sys.executable, "-I", str(script))
    )
    descendant = None
    try:
        await commands.send_config(
            "operation-1", {"PATH": os.environ["PATH"]}, grace_seconds=0.05
        )
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "core_started")
        events.close()
        await asyncio.sleep(0)

        for _ in range(300):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        descendant = int(pidfile.read_text())
        assert await asyncio.wait_for(process.wait(), timeout=3) != 0
        _assert_dead(descendant)
    finally:
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await _finish(process, commands, events)


def test_emergency_cleanup_never_signals_a_known_reaped_core_pid(
    tmp_path, monkeypatch, ownership_mode
):
    from evolution_lab import _supervisor

    children = tmp_path / "children"
    children.write_text("")
    core = SimpleNamespace(pid=424_242, returncode=0)
    signals = []
    monkeypatch.setattr(
        _supervisor.os, "kill", lambda pid, sig: signals.append((pid, sig))
    )
    monkeypatch.setattr(_supervisor, "_reap", lambda pid: (None, True))

    _supervisor._cleanup_without_events(core, children, 0.01, ownership=ownership_mode, mechanism=None)

    assert signals == []


def test_incremental_reader_rejects_bad_header_before_draining_readable_source(
    monkeypatch,
):
    from evolution_lab import _supervisor

    reader = _supervisor.IncrementalFrameReader(max_payload=64)
    calls = 0

    def continuously_readable(fd, size):
        nonlocal calls
        calls += 1
        if calls > 20:
            raise AssertionError("reader did not parse while draining")
        if calls == 1:
            return _supervisor._HEADER.pack(65) + b"payload"
        return b"x" * min(size, 64)

    monkeypatch.setattr(_supervisor.os, "read", continuously_readable)

    with pytest.raises(_supervisor.ProtocolError):
        reader.read_available(424_242)
    assert calls == 1


def test_incremental_reader_returns_a_bounded_batch_from_readable_source(monkeypatch):
    from evolution_lab import _supervisor

    frame = _supervisor.encode_frame(
        _supervisor.make_command("operation-1", "STOP")
    )
    reader = _supervisor.IncrementalFrameReader(max_payload=len(frame))
    calls = 0

    def continuously_readable(fd, size):
        nonlocal calls
        calls += 1
        if calls > 100:
            raise AssertionError("reader did not return lifecycle control")
        return frame

    monkeypatch.setattr(_supervisor.os, "read", continuously_readable)

    messages, eof = reader.read_available(424_242)

    assert eof is False
    assert 0 < len(messages) < 100
    assert calls < 100


@pytest.mark.usefixtures("ownership_mode")
async def test_stop_accepted_after_core_reap_reports_too_late_before_cleanup(tmp_path, ownership_mode):
    pidfile = tmp_path / "descendant.pid"
    script = tmp_path / "exited-core.py"
    script.write_text(
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(child.pid))\n"
    )
    wrapper, paused, release = _pause_on_zombie(tmp_path) if ownership_mode == "group_tree_marker" else (None, None, None)
    process, commands, events = await _start_h1(
        tmp_path, (sys.executable, "-I", str(script)), supervisor_path=wrapper
    )
    descendant = None
    try:
        await commands.send_config(
            "operation-1", {"PATH": os.environ["PATH"]}, grace_seconds=0.05
        )
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "core_started")
        if ownership_mode == "subreaper":
            assert await _event(events, "core_exited") == {"exit_code": 0}
        else:
            for _ in range(300):
                if paused.exists():
                    break
                await asyncio.sleep(.01)
            assert paused.exists()
        descendant = int(pidfile.read_text())

        commands.send_stop("operation-1")
        if release is not None:
            release.touch()
        assert await _event(events, "stop_effect") == {"value": "too_late"}
        if ownership_mode == "group_tree_marker":
            assert await _event(events, "core_exited") == {"exit_code": 0}
        assert await _event(events, "cleanup_complete") == {"value": True}
        await asyncio.wait_for(process.wait(), timeout=3)
    finally:
        if release is not None:
            release.touch()
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await _finish(process, commands, events)


@pytest.mark.parametrize(
    "message",
    [
        {"schema": "lab-supervisor-event/1", "operation_id": "operation-1",
         "kind": [], "data": {}},
        {"schema": "lab-supervisor-event/1", "operation_id": "operation-1",
         "kind": "stop_effect", "data": {"value": []}},
    ],
)
def test_unhashable_event_values_are_protocol_errors(message):
    from evolution_lab import _supervisor

    with pytest.raises(_supervisor.ProtocolError):
        _supervisor.validate_event(message)


@pytest.mark.usefixtures("ownership_mode")
async def test_unhashable_command_after_go_still_cleans_owned_tree(tmp_path):
    from evolution_lab import _supervisor

    pidfile = tmp_path / "owned.pid"
    script = tmp_path / "owned-core.py"
    script.write_text(
        "import os,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
        "time.sleep(60)\n"
    )
    process, commands, events = await _start_h1(
        tmp_path, (sys.executable, "-I", str(script))
    )
    owned = []
    try:
        await commands.send_config(
            "operation-1", {"PATH": os.environ["PATH"]}, grace_seconds=0.05
        )
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "core_started")
        for _ in range(300):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        owned = [int(item) for item in pidfile.read_text().split()]

        _supervisor.write_atomic_frame(
            commands._open_fd(),
            {"schema": "lab-supervisor-command/1", "operation_id": "operation-1",
             "kind": [], "data": {}},
        )
        assert await asyncio.wait_for(process.wait(), timeout=3) != 0
        for pid in owned:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await _finish(process, commands, events)


@pytest.mark.usefixtures("ownership_mode")
async def test_partial_post_go_command_does_not_block_force_or_cleanup(tmp_path):
    pidfile = tmp_path / "owned.pid"
    script = tmp_path / "blocked-core.py"
    script.write_text(
        "import os,signal,subprocess,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *args: None)\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
        "time.sleep(60)\n"
    )
    process, commands, events = await _start_h1(
        tmp_path, (sys.executable, "-I", str(script))
    )
    owned = []
    try:
        await commands.send_config(
            "operation-1", {"PATH": os.environ["PATH"]}, grace_seconds=0.05
        )
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "core_started")
        for _ in range(300):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        owned = [int(item) for item in pidfile.read_text().split()]

        os.write(commands._open_fd(), b"\x00\x00")
        os.kill(process.pid, signal.SIGUSR1)
        assert await asyncio.wait_for(_event(events, "core_force_requested"), 1) == {
            "value": True
        }
        assert (await _event(events, "core_exited"))["exit_code"] < 0
        assert await _event(events, "cleanup_complete") == {"value": True}
        await asyncio.wait_for(process.wait(), timeout=3)
        for pid in owned:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await _finish(process, commands, events)


async def test_async_config_cancellation_and_partial_event_frame_do_not_hang():
    from evolution_lab._client_protocol import (
        CommandChannel,
        ProtocolError,
        open_event_channel,
    )

    command_read, command_write = os.pipe()
    commands = CommandChannel(command_write)
    send = asyncio.create_task(
        commands.send_config("operation-1", {"X": "x" * 1_000_000}, grace_seconds=1)
    )
    try:
        await asyncio.sleep(0)
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(send, timeout=1)
    finally:
        commands.close()
        os.close(command_read)

    event_read, event_write = os.pipe()
    events = await open_event_channel(event_read, "operation-1")
    try:
        os.write(event_write, b"\x00\x00\x00\x10partial")
        os.close(event_write)
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(events.read(), timeout=1)
    finally:
        events.close()


@pytest.mark.usefixtures("ownership_mode")
async def test_legacy_invocation_still_writes_result_and_reaps_descendants(tmp_path):
    from evolution_lab import _supervisor

    pidfile = tmp_path / "pid"
    script = tmp_path / "legacy-core.py"
    script.write_text(
        "import subprocess,sys,time,signal\n"
        "signal.signal(signal.SIGTERM, lambda *args: None)\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    status = tmp_path / "supervisor-result.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", _supervisor.__file__, "--ownership", select_mode(), str(status),
        sys.executable, "-I", str(script), start_new_session=True,
    )
    descendant = None
    try:
        for _ in range(300):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.01)
        descendant = int(pidfile.read_text())
        os.kill(process.pid, signal.SIGUSR1)
        await asyncio.wait_for(process.wait(), timeout=3)
        record = json.loads(status.read_text())
        assert record["forced_stop"] is True
        assert record["cleanup_complete"] is True
        _assert_dead(descendant)
    finally:
        if process.returncode is None:
            os.kill(process.pid, signal.SIGUSR1)
            await process.wait()
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _pause_on_zombie(tmp_path):
    wrapper = tmp_path / "paused-supervisor.py"
    paused, release = tmp_path / "zombie-paused", tmp_path / "zombie-release"
    wrapper.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        "from evolution_lab import _supervisor as s\n"
        "original = s.GroupTreeMarker.scope_empty\n"
        "def empty(self):\n"
        f"    paused, release = Path({str(paused)!r}), Path({str(release)!r})\n"
        "    if self.leader_exited() and not paused.exists():\n"
        "        paused.touch()\n"
        "        while not release.exists(): time.sleep(.001)\n"
        "        return False\n"
        "    return original(self)\n"
        "s.GroupTreeMarker.scope_empty = empty\n"
        "raise SystemExit(s.main())\n"
    )
    return wrapper, paused, release


@pytest.mark.parametrize("argv", [[], ["--h1", "3", "4", "op", "--", "/bin/true"],
    ["--ownership", "other", "unused", "/bin/true"], ["--ownership"],
    ["--ownership", "group_tree_marker"], ["--ownership", "subreaper", "--caller-pid"]])
def test_supervisor_requires_ownership_argument(argv, monkeypatch):
    from evolution_lab import _supervisor
    monkeypatch.setattr(_supervisor.subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected spawn"))
    assert _supervisor.main(argv) == 2


async def test_h1_core_exited_precedes_cleanup_complete_in_macos_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOLUTION_LAB_TEST_OWNERSHIP", "group_tree_marker")
    process, commands, events = await _start_h1(tmp_path, (TRUE,))
    try:
        await commands.send_config("operation-1", {}, grace_seconds=.1)
        await _event(events, "ready")
        commands.send_go("operation-1")
        kinds = []
        while (event := await asyncio.wait_for(events.read(), 3)) is not None:
            kinds.append(event["kind"])
        assert kinds == ["core_started", "core_exited", "cleanup_complete"]
        assert await asyncio.wait_for(process.wait(), 3) == 0
    finally:
        await _finish(process, commands, events)


async def test_supervisor_drops_inherited_marker(tmp_path, ownership_mode):
    ambient = dict(os.environ, EVOLUTION_LAB_SCOPE="outer")
    process, commands, events = await _start_h1(tmp_path, ("/usr/bin/env", "-0"),
        supervisor_env=ambient, stdout=asyncio.subprocess.PIPE)
    try:
        await commands.send_config("operation-1", {"EVOLUTION_LAB_SCOPE": "outer"}, grace_seconds=.1)
        await _event(events, "ready")
        commands.send_go("operation-1")
        await _event(events, "cleanup_complete")
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert b"outer" not in stdout and stderr == b""
        assert (b"EVOLUTION_LAB_SCOPE=" in stdout) == (ownership_mode == "group_tree_marker")
    finally:
        await _finish(process, commands, events)


def _assert_dead(pid):
    if select_mode() == "subreaper":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    else:
        # The process may be reaped between any two psutil calls; gone counts as dead.
        try:
            assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass


async def test_legacy_status_written_after_final_reap(tmp_path, monkeypatch):
    from evolution_lab import _supervisor
    monkeypatch.setenv("EVOLUTION_LAB_TEST_OWNERSHIP", "group_tree_marker")
    wrapper, paused, release = _pause_on_zombie(tmp_path)
    status, pids = tmp_path / "status.json", tmp_path / "pids"
    source = (
        "import os,subprocess,sys\n"
        "child = subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],start_new_session=True)\n"
        f"open({str(pids)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
    )
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(wrapper),
        "--ownership", "group_tree_marker", str(status), sys.executable, "-I", "-c", source)
    descendant = None
    try:
        for _ in range(300):
            if paused.exists():
                break
            await asyncio.sleep(.01)
        assert paused.exists()
        leader, descendant = map(int, pids.read_text().split())
        assert psutil.Process(leader).status() == psutil.STATUS_ZOMBIE
        assert not status.exists()
        release.touch()
        for _ in range(300):
            if status.exists():
                break
            await asyncio.sleep(.01)
        assert status.exists()
        assert not psutil.pid_exists(leader)
        _assert_dead(descendant)
        assert json.loads(status.read_text()) == {
            "core_exit_code": 0, "stop_requested": False, "forced_stop": True,
            "cleanup_complete": True, "caller_lost": False,
        }
        assert await asyncio.wait_for(process.wait(), 3) == 0
    finally:
        release.touch()
        if process.returncode is None:
            os.kill(process.pid, signal.SIGUSR1)
            await asyncio.wait_for(process.wait(), 3)
        if descendant is not None:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_legacy_supervisor_drops_inherited_marker(tmp_path, ownership_mode):
    from evolution_lab import _supervisor
    status = tmp_path / "status.json"
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", _supervisor.__file__,
        "--ownership", ownership_mode, str(status), "/usr/bin/env", "-0",
        env=dict(os.environ, EVOLUTION_LAB_SCOPE="outer"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
    assert process.returncode == 0 and stderr == b""
    received = dict(item.split(b"=", 1) for item in stdout.split(b"\0") if item)
    token = received.get(b"EVOLUTION_LAB_SCOPE")
    assert token != b"outer"
    assert (token is not None) == (ownership_mode == "group_tree_marker")
    assert b"EVOLUTION_LAB_TEST_OWNERSHIP" not in received


@pytest.mark.parametrize('protocol', ['h1', 'legacy'])
async def test_final_reap_newcomer_is_cleaned_before_success(tmp_path, monkeypatch, protocol):
    from test_owner_scopes_ownership import _late_marker_patch
    monkeypatch.setenv('EVOLUTION_LAB_TEST_OWNERSHIP', 'group_tree_marker')
    pidfile = tmp_path / 'newcomer.pid'
    wrapper = tmp_path / 'late-marker-supervisor.py'
    wrapper.write_text("import sys\nfrom pathlib import Path\nfrom evolution_lab import _supervisor as s\n"
                       + _late_marker_patch(pidfile) + "\nraise SystemExit(s.main())\n")
    core = (sys.executable, '-I', '-c', 'raise SystemExit(0)')
    commands = events = None
    status = tmp_path / 'status.json'
    if protocol == 'h1':
        process, commands, events = await _start_h1(tmp_path, core, supervisor_path=wrapper)
    else:
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', str(wrapper),
            '--ownership', 'group_tree_marker', str(status), *core, stderr=asyncio.subprocess.PIPE)
    try:
        if commands is not None:
            await commands.send_config('operation-1', {}, grace_seconds=.1)
            await _event(events, 'ready')
            commands.send_go('operation-1')
            assert await _event(events, 'core_exited') == {'exit_code': 0}
            assert await _event(events, 'cleanup_complete') == {'value': True}
        assert await asyncio.wait_for(process.wait(), 5) == 0
        _assert_dead(int(pidfile.read_text()))
        if protocol == 'legacy':
            record = json.loads(status.read_text())
            assert record['cleanup_complete'] and record['forced_stop']
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if commands is not None:
            await _finish(process, commands, events)
        else:
            if process.returncode is None:
                process.kill()
            await process.communicate()


@pytest.mark.parametrize('error_type', ['RuntimeError', 'psutil.Error'])
async def test_h1_transient_mechanism_failure_still_cleans_descendants(tmp_path, monkeypatch, error_type):
    from test_owner_scopes_ownership import _cleanup
    monkeypatch.setenv('EVOLUTION_LAB_TEST_OWNERSHIP', 'group_tree_marker')
    pidfile = tmp_path / 'pids'
    wrapper = tmp_path / 'fault-supervisor.py'
    wrapper.write_text(f"""import sys,time,psutil
from pathlib import Path
from evolution_lab import _supervisor as s
original_poll = s.GroupTreeMarker.poll
faulted = False
def poll(self):
 global faulted
 if faulted:
  return original_poll(self)
 path = Path({str(pidfile)!r})
 deadline = time.monotonic() + 3
 while (not path.exists() or not path.read_text()) and time.monotonic() < deadline:
  time.sleep(.001)
 faulted = True
 raise {error_type}('injected mechanism failure')
s.GroupTreeMarker.poll = poll
raise SystemExit(s.main())
""")
    source = (
        "import os,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],start_new_session=True)\n"
        f"open({str(pidfile)!r},'w').write(str(os.getpid())+' '+str(child.pid))\n"
        "time.sleep(60)\n"
    )
    process, commands, events = await _start_h1(
        tmp_path, (sys.executable, '-I', '-c', source), supervisor_path=wrapper)
    owned, pids = [], []
    try:
        await commands.send_config('operation-1', {}, grace_seconds=.1)
        await _event(events, 'ready')
        commands.send_go('operation-1')
        for _ in range(300):
            if pidfile.exists() and len(pidfile.read_text().split()) == 2:
                break
            await asyncio.sleep(.01)
        pids = list(map(int, pidfile.read_text().split()))
        for pid in pids:
            try:
                owned.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                pass
        assert await asyncio.wait_for(process.wait(), 5) == 2
        for pid in pids:
            _assert_dead(pid)
    finally:
        _cleanup(owned)
        await _finish(process, commands, events)
