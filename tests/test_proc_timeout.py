"""Subprocess timeout and bounded-capture regressions."""

from __future__ import annotations

import subprocess
import sys
import time
from os import kill
from pathlib import Path
from signal import SIGKILL

import pytest

from evolution.eval.proc import run_capture

pytestmark = pytest.mark.slow


def test_timeout_enforced_despite_leaked_grandchild() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture(
            ["bash", "-c", 'sleep 600 & python3 -c "import time; time.sleep(600)"'],
            timeout=0.1,
        )
    assert time.monotonic() - started < 2


def test_timeout_enforced_when_detached_grandchild_holds_output(tmp_path: Path) -> None:
    pid_file = tmp_path / "detached.pid"
    code = (
        "import os, pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    path.write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "while not path.exists():\n"
        "    time.sleep(0.001)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_capture([sys.executable, "-c", code, str(pid_file)], timeout=0.2)
        assert time.monotonic() - started < 2
    finally:
        if pid_file.exists():
            try:
                kill(int(pid_file.read_text()), SIGKILL)
            except ProcessLookupError:
                pass


def test_normal_command_returns_completedprocess() -> None:
    proc = run_capture(["bash", "-c", "echo hi; exit 0"], timeout=10)
    assert proc.returncode == 0
    assert "hi" in proc.stdout


def test_nonzero_exit_captured() -> None:
    proc = run_capture(["bash", "-c", "echo boom >&2; exit 7"], timeout=10)
    assert proc.returncode == 7
    assert "boom" in proc.stderr


def test_large_output_has_a_bounded_view_and_complete_sink(tmp_path: Path) -> None:
    trace = tmp_path / "stdout"
    with trace.open("xb", buffering=0) as stdout_sink:
        proc = run_capture(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'A' * 200_000 + b'TAIL')",
            ],
            timeout=10,
            max_output_bytes=1024,
            stdout_sink=stdout_sink,
        )

    assert proc.returncode == 0
    assert "output truncated" in proc.stdout
    assert proc.stdout.endswith("TAIL")
    assert len(proc.stdout.encode("utf-8")) <= 1024
    assert trace.read_bytes() == b"A" * 200_000 + b"TAIL"


def test_timeout_applies_when_child_never_reads_stdin() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        run_capture(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            input="x" * 1_000_000,
            timeout=0.1,
        )


def test_timeout_sink_keeps_bytes_acknowledged_before_kill(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    counter.write_bytes(b"\0" * 8)
    trace = tmp_path / "stdout"
    code = (
        "import os, sys\n"
        "counter = os.open(sys.argv[1], os.O_WRONLY)\n"
        "total = 0\n"
        "chunk = b'X' * 65536\n"
        "while True:\n"
        "    total += os.write(1, chunk)\n"
        "    os.pwrite(counter, total.to_bytes(8, 'little'), 0)\n"
    )

    with trace.open("xb", buffering=0) as stdout_sink:
        with pytest.raises(subprocess.TimeoutExpired):
            run_capture(
                [sys.executable, "-c", code, str(counter)],
                timeout=1,
                stdout_sink=stdout_sink,
            )

    acknowledged = int.from_bytes(counter.read_bytes(), "little")
    assert len(trace.read_bytes()) >= acknowledged > 0


def test_timeout_does_not_drain_an_out_of_group_writer_forever(tmp_path: Path) -> None:
    pid_file = tmp_path / "detached-writer.pid"
    trace = tmp_path / "stdout"
    code = (
        "import os, pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    path.write_text(str(os.getpid()))\n"
        "    while True:\n"
        "        os.write(1, b'X' * 65536)\n"
        "while not path.exists():\n"
        "    time.sleep(0.001)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()

    try:
        with trace.open("xb", buffering=0) as stdout_sink:
            with pytest.raises(subprocess.TimeoutExpired):
                run_capture(
                    [sys.executable, "-c", code, str(pid_file)],
                    timeout=1,
                    stdout_sink=stdout_sink,
                )
        assert time.monotonic() - started < 2
        assert trace.stat().st_size > 0
    finally:
        if pid_file.exists():
            try:
                kill(int(pid_file.read_text()), SIGKILL)
            except ProcessLookupError:
                pass


def test_input_does_not_hang_when_grandchild_holds_stdin_after_parent_exits(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import os, sys, time; "
        "pid = os.fork(); "
        "(open(sys.argv[1], 'w').write(str(os.getpid())), os.close(1), os.close(2), time.sleep(60)) "
        "if pid == 0 else os._exit(0)"
    )
    started = time.monotonic()

    try:
        completed = run_capture(
            [sys.executable, "-c", code, str(pid_file)], input="x" * 1_000_000, timeout=1
        )

        assert completed.returncode == 0
        assert pid_file.exists()
        assert time.monotonic() - started < 10
    finally:
        if pid_file.exists():
            try:
                kill(int(pid_file.read_text()), SIGKILL)
            except ProcessLookupError:
                pass
