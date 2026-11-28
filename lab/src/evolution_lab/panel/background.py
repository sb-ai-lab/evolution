"""Background panel start and stop over ``H/panel.json`` and ``H/panel.lock`` (ADR-0018 p. 4).

``start`` launches a detached ``lab panel`` child and returns its runtime record once the child
has written it. ``stop`` signals a panel only after it proves its identity: it must answer
``/api/health`` with the ``started_at`` recorded in ``panel.json``, so a reused PID is never hit.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

from .runtime import (LOG_PATH, RUNTIME_NAME, PanelError, existing_panel_home, lock_is_held, open_log,
                      prepare_panel_home, read_runtime, runtime_status)

STOP_SCHEMA = "lab-panel-stop/1"
_POLL = 0.1
_HEALTH_TIMEOUT = 3.0


def start(*, home: Path, argv_tail: list[str], timeout: float = 15.0) -> dict:
    # Validated here too, so nothing (not even the log) is written into an invalid home.
    home = prepare_panel_home(Path(home))
    if lock_is_held(home):
        record = read_runtime(home)
        raise PanelError("panel_already_running", url=None if record is None else record["url"])
    log = open_log(home)
    try:
        child = subprocess.Popen([sys.executable, "-m", "evolution_lab.cli", "--home", str(home), *argv_tail],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    finally:
        os.close(log)
    deadline = time.monotonic() + timeout
    while True:
        record = read_runtime(home)
        if record is not None and record["pid"] == child.pid:
            return record
        if child.poll() is not None:
            raise PanelError("panel_start_failed", log=str(home / LOG_PATH))
        if time.monotonic() >= deadline:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise PanelError("panel_start_timeout", pid=child.pid, log=str(home / LOG_PATH))
        time.sleep(_POLL)


def _confirmed(record: dict) -> bool:
    import httpx
    from .app import cookie_name

    port = record["port"]
    url = urlsplit(record["url"])
    token = parse_qs(url.query).get("token", [""])[0]
    try:
        # The panel listens on 127.0.0.1 only and answers only to its launch host name.
        with httpx.Client(trust_env=False, timeout=_HEALTH_TIMEOUT) as client:
            response = client.get(f"http://127.0.0.1:{port}/api/health",
                                  headers={"Host": f"{url.hostname}:{port}", "Cookie": f"{cookie_name(port)}={token}"})
        return response.status_code == 200 and response.json().get("started_at") == record["started_at"]
    except (httpx.HTTPError, OSError, ValueError, AttributeError):
        return False


def stop(home: Path, *, wait_seconds: float = 10.0) -> dict:
    home = existing_panel_home(Path(home))  # same home rule as start; nothing is created
    if home is None:
        return {"schema": STOP_SCHEMA, "state": "not_running", "pid": None}
    status = runtime_status(home)
    if status["state"] == "not_running":
        return {"schema": STOP_SCHEMA, "state": "not_running", "pid": None}
    record = status["panel"]
    if record is None:
        # The lock is held but panel.json is absent or unreadable: nothing names the holder.
        raise PanelError("panel_identity_unconfirmed", pid=None)
    pid = record["pid"]
    if not _confirmed(record):
        raise PanelError("panel_identity_unconfirmed", pid=pid)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_seconds
    while lock_is_held(home):
        if time.monotonic() >= deadline:
            raise PanelError("panel_stop_timeout", pid=pid)
        time.sleep(_POLL)
    # Normally the panel removes its own file; after the lock is free a leftover is stale.
    current = read_runtime(home)
    if current is not None and current["pid"] == pid:
        (home / RUNTIME_NAME).unlink(missing_ok=True)
    return {"schema": STOP_SCHEMA, "state": "stopped", "pid": pid}
