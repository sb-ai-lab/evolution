"""Timeout-safe subprocess capture.

``subprocess.run(capture_output=True, timeout=...)`` does not reliably enforce
the timeout: on expiry CPython kills only the direct child, then blocks in
``communicate()`` on the stdout/stderr pipe whose write end is still held by
surviving grandchildren — so ``TimeoutExpired`` is never raised and the calling
thread wedges forever (this deadlocked a whole anchor in t34v7). Running the
child in its own session and killing the entire process group on timeout closes
those pipes and makes the timeout real.
"""

from __future__ import annotations

import os
import signal
import subprocess


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def run_capture(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: dict | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Like ``subprocess.run(capture_output=True, timeout=...)`` but the
    timeout is actually enforced: the whole process group is killed on expiry.

    Raises ``subprocess.TimeoutExpired`` (with whatever output was captured) so
    existing callers' ``except subprocess.TimeoutExpired`` paths are unchanged.
    """

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        cwd=cwd,
        env=env,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                out, err = ("" if text else b""), ("" if text else b"")
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
