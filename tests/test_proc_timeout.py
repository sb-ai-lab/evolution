"""run_capture must enforce the timeout even when a grandchild leaks the pipe.

Regression guard for the t34v7 xlsx deadlock: subprocess.run(capture_output=
True, timeout=) hung forever because a surviving grandchild held the stdout
pipe, so TimeoutExpired was never raised and the produce-pool thread wedged.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from evolution.eval._proc import run_capture


def test_timeout_enforced_despite_leaked_grandchild() -> None:
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        # backgrounded grandchild keeps the inherited stdout pipe open while a
        # foreground child sleeps; the old subprocess.run would block forever.
        run_capture(
            ["bash", "-c", 'sleep 600 & python3 -c "import time; time.sleep(600)"'],
            timeout=3,
        )
    assert time.time() - t0 < 60, "timeout was not actually enforced"


def test_normal_command_returns_completedprocess() -> None:
    proc = run_capture(["bash", "-c", "echo hi; exit 0"], timeout=10)
    assert proc.returncode == 0
    assert "hi" in proc.stdout


def test_nonzero_exit_captured() -> None:
    proc = run_capture(["bash", "-c", "echo boom >&2; exit 7"], timeout=10)
    assert proc.returncode == 7
    assert "boom" in proc.stderr
