import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from evolution_lab.panel import runtime as rt

pytestmark = pytest.mark.slow

PANEL_FILES = {"panel.json", "panel.lock", "logs"}
SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def home():
    base = Path(tempfile.mkdtemp(prefix="pnlh-", dir="/tmp")).resolve()
    base.chmod(0o700)
    yield base / "home"
    shutil.rmtree(base, ignore_errors=True)


def lab(home, *args, check=True, timeout=30):
    result = subprocess.run([sys.executable, "-m", "evolution_lab.cli", "--json", "--home", str(home), *args],
                            capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "PYTHONPATH": SRC})
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _cleanup(home):
    lab(home, "panel", "stop", check=False)


def test_background_start_status_stop(home):
    try:
        started = json.loads(lab(home, "panel", "--background").stdout)
        assert started["schema"] == "lab-panel-runtime/1" and started["home"] == str(home)
        assert (home / "panel.json").stat().st_mode & 0o777 == 0o600
        assert (home / "logs" / "panel.log").exists()
        assert {p.name for p in home.iterdir()} == PANEL_FILES  # no clones/, workspaces/ (ADR-0018 p. 1)
        port, host = started["port"], urlsplit(started["url"]).hostname
        assert host.startswith("p") and host.endswith(".localhost")
        with httpx.Client() as browser:
            first = browser.get(started["url"], follow_redirects=False)
            assert first.status_code == 303
            health = browser.get(f"http://{host}:{port}/api/health")  # the jar keeps the 303's cookie
            assert health.json()["started_at"] == started["started_at"]
            assert health.json()["service"]["state"] == "service_unavailable"
        # The right token on a loopback address is refused: only the launch host name answers.
        token = parse_qs(urlsplit(started["url"]).query)["token"][0]
        for other in (f"127.0.0.1:{port}", f"localhost:{port}"):
            misdirected = httpx.get(f"http://127.0.0.1:{port}/api/health",
                                    headers={"host": other, "cookie": f"lab_panel_{port}={token}"})
            assert misdirected.status_code == 421
        with httpx.Client() as stranger:  # an empty cookie jar
            assert stranger.get(f"http://{host}:{port}/api/health").status_code == 403
        status = json.loads(lab(home, "panel", "status").stdout)
        assert status == {"schema": "lab-panel-status/1", "state": "running", "panel": started}
        stopped = json.loads(lab(home, "panel", "stop").stdout)
        assert stopped == {"schema": "lab-panel-stop/1", "state": "stopped", "pid": started["pid"]}
        assert not (home / "panel.json").exists()
        assert json.loads(lab(home, "panel", "status").stdout)["state"] == "not_running"
    finally:
        _cleanup(home)


def test_second_copy_refuses_and_prints_the_first_link(home):
    try:
        started = json.loads(lab(home, "panel", "--background").stdout)
        again = lab(home, "panel", "--background", check=False)
        assert again.returncode == 1
        error = json.loads(again.stderr)
        assert error["error"]["code"] == "panel_already_running" and error["url"] == started["url"]
        foreground = lab(home, "panel", check=False)  # the foreground path refuses the same way
        assert foreground.returncode == 1 and json.loads(foreground.stderr)["url"] == started["url"]
    finally:
        _cleanup(home)


def test_stop_refuses_a_foreign_pid(home):
    home.mkdir(mode=0o700)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    fd = rt.acquire_lock(home)  # something holds the lock, but it is not a panel
    try:
        rt.write_runtime(home, rt.runtime_record(pid=stranger.pid, port=1, host="p0000000000000000.localhost",
                                                 token="x" * 43,
                                                 started_at="2026-09-29T00:00:00.000000+00:00", home=home))
        result = lab(home, "panel", "stop", check=False)
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"]["code"] == "panel_identity_unconfirmed"
        assert stranger.poll() is None
    finally:
        rt.release_lock(fd)
        stranger.kill()
        stranger.wait()


def test_background_start_after_a_crashed_panel(home):
    try:
        started = json.loads(lab(home, "panel", "--background").stdout)
        os.kill(started["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while rt.lock_is_held(home):
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert (home / "panel.json").exists()  # stale
        again = json.loads(lab(home, "panel", "--background").stdout)
        assert again["pid"] != started["pid"] and again["url"] != started["url"]
    finally:
        _cleanup(home)


def test_port_in_use(home):
    import socket
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    try:
        result = lab(home, "panel", "--port", str(taken.getsockname()[1]), check=False)
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"]["code"] == "panel_port_in_use"
        assert not (home / "panel.json").exists()
    finally:
        taken.close()


def test_text_output(home):
    try:
        started = json.loads(lab(home, "panel", "--background").stdout)
        text = subprocess.run([sys.executable, "-m", "evolution_lab.cli", "--home", str(home), "panel", "status"],
                              capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC}, timeout=30)
        assert text.stdout.startswith(f"panel: running {started['url']} (pid {started['pid']}")
    finally:
        _cleanup(home)


def test_start_timeout_reports_pid_and_leaves_no_child(home, monkeypatch):
    from evolution_lab.panel import background
    real_popen = subprocess.Popen

    def sleeper(argv, **kwargs):  # a child that never writes panel.json
        return real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    monkeypatch.setattr(background.subprocess, "Popen", sleeper)
    with pytest.raises(rt.PanelError) as error:
        background.start(home=home, argv_tail=["panel"], timeout=0.5)
    assert error.value.code == "panel_start_timeout"
    pid = error.value.fields["pid"]
    assert error.value.fields["log"].endswith("panel.log")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # already reaped: terminated and waited for


def test_open_does_not_block_serving(home, monkeypatch):
    import argparse
    import threading
    import webbrowser
    from evolution_lab.cli import panel as cli_panel

    blocked, release = threading.Event(), threading.Event()
    monkeypatch.setattr(webbrowser, "open", lambda url: (blocked.set(), release.wait(30)))
    result = {}

    def probe():
        record = None
        try:
            deadline = time.monotonic() + 20
            while record is None and time.monotonic() < deadline:
                record = rt.read_runtime(home)
                time.sleep(0.05)
            assert record is not None and blocked.wait(10)  # the browser call is stuck
            host = urlsplit(record["url"]).hostname
            with httpx.Client() as browser:
                browser.get(record["url"], follow_redirects=False)
                result["health"] = browser.get(f"http://{host}:{record['port']}/api/health", timeout=5).status_code
            result["files"] = {p.name for p in home.iterdir()}
        finally:
            release.set()
            # SIGTERM reaches the panel's own handler only while it serves, i.e. while its record
            # is on disk; otherwise the default handler would kill pytest itself.
            current = rt.read_runtime(home)
            if record is not None and current is not None and current["pid"] == os.getpid():
                os.kill(os.getpid(), signal.SIGTERM)

    args = argparse.Namespace(home=str(home), socket=None, port=0, open_browser=True, background=False,
                              panel_command=None, machine=True)
    import io
    thread = threading.Thread(target=probe)
    thread.start()
    try:
        code = cli_panel.run_panel_command(args, stdout=io.StringIO(), stderr=io.StringIO())
    finally:
        release.set()
        thread.join(30)
    assert code == 0 and result["health"] == 200
    assert result["files"] == {"panel.json", "panel.lock"}  # the foreground panel writes no log


def test_background_open_calls_the_browser_before_the_cli_returns(home, monkeypatch):
    import argparse
    import io
    import threading
    import webbrowser
    from evolution_lab.cli import panel as cli_panel

    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: calls.append((url, threading.current_thread().name)))
    monkeypatch.setenv("PYTHONPATH", SRC)
    args = argparse.Namespace(home=str(home), socket=None, port=0, open_browser=True, background=True,
                              panel_command=None, machine=True)
    stdout = io.StringIO()
    try:
        code = cli_panel.run_panel_command(args, stdout=stdout, stderr=io.StringIO())
        assert code == 0
        record = json.loads(stdout.getvalue())
        assert calls == [(record["url"], threading.current_thread().name)]  # synchronous, not a daemon
    finally:
        _cleanup(home)


def test_logs_symlink_is_home_invalid_and_its_target_untouched(home):
    home.mkdir(mode=0o700)
    target = home.parent / "elsewhere"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    (home / "logs").symlink_to(target)
    result = lab(home, "panel", "--background", check=False)
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "home_invalid"
    assert target.stat().st_mode & 0o777 == 0o755 and list(target.iterdir()) == []
    assert not (home / "panel.json").exists()


def test_status_and_stop_refuse_a_symlinked_home(home):
    home.mkdir(mode=0o700)
    link = home.parent / "link"
    link.symlink_to(home)
    try:
        started = json.loads(lab(home, "panel", "--background").stdout)
        for command in ("status", "stop"):
            result = lab(link, "panel", command, check=False)
            assert result.returncode == 1
            assert json.loads(result.stderr)["error"]["code"] == "home_invalid"
        assert json.loads(lab(home, "panel", "status").stdout)["panel"] == started  # still running
    finally:
        _cleanup(home)
