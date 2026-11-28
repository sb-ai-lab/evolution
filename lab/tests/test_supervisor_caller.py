"""Legacy supervisor kills its tree without SIGTERM when its caller is gone (T8)."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import psutil
from ownership_modes import ownership_mode  # noqa: F401
from evolution_lab import _supervisor


def _dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_dead_caller_before_start_never_launches_core(tmp_path, ownership_mode):
    status, started = tmp_path / "status.json", tmp_path / "started"
    code = subprocess.run([sys.executable, "-I", _supervisor.__file__, "--ownership", ownership_mode, "--caller-pid", str(_dead_pid()),
                           str(status), sys.executable, "-c", f"open({str(started)!r}, 'w')"], timeout=20).returncode
    saved = json.loads(status.read_text())
    assert saved["caller_lost"] is True and saved["core_exit_code"] is None
    assert saved["cleanup_complete"] is True and not started.exists()
    assert code == 1


def test_caller_loss_after_start_kills_core_without_sigterm(tmp_path, ownership_mode):
    status, pid_file, term = tmp_path / "status.json", tmp_path / "core.pid", tmp_path / "got-term"
    core = (f"import os,signal,time\nfrom pathlib import Path\n"
            f"signal.signal(signal.SIGTERM, lambda *_: Path({str(term)!r}).touch())\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n")
    middle = (f"import os,subprocess,sys,time\n"
              f"subprocess.Popen([sys.executable,'-I',{_supervisor.__file__!r},'--ownership',{ownership_mode!r},'--caller-pid',str(os.getpid()),"
              f"{str(status)!r},sys.executable,'-c',{core!r}])\ntime.sleep(60)\n")
    caller = subprocess.Popen([sys.executable, "-c", middle])
    try:
        assert _wait(lambda: pid_file.exists() and pid_file.read_text())
        caller.kill(); caller.wait()
        assert _wait(status.exists)
        saved = json.loads(status.read_text())
        assert saved["caller_lost"] is True and saved["cleanup_complete"] is True
        try:
            os.kill(int(pid_file.read_text()), 0)
            raise AssertionError("core survived caller loss")
        except ProcessLookupError:
            pass
        assert not term.exists()
    finally:
        if caller.poll() is None:
            caller.kill()


def test_without_caller_pid_status_reports_caller_not_lost(tmp_path, ownership_mode):
    status = tmp_path / "status.json"
    code = subprocess.run([sys.executable, "-I", _supervisor.__file__, "--ownership", ownership_mode, str(status),
                           sys.executable, "-c", "raise SystemExit(0)"], timeout=20).returncode
    saved = json.loads(status.read_text())
    assert code == 0 and saved["caller_lost"] is False and saved["core_exit_code"] == 0
