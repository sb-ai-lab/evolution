"""The panel process in the foreground (ADR-0018 p. 1, 4).

Order: dependencies -> home (panel files only) -> lock -> bind+listen -> panel.json -> ready -> serve. The runtime
file appears only once the socket listens, so a URL read from it is already reachable; on the
way out the file is removed (only if it is still ours) before the lock is released.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import errno
import os
from pathlib import Path
import secrets
import signal
import socket
from typing import Callable

from .runtime import (PanelError, acquire_lock, prepare_panel_home, release_lock, remove_runtime,
                      runtime_record, write_runtime)


def launch_host() -> str:
    # A fresh *.localhost name per launch (ADR-0018 p. 3, amended): browsers do not isolate
    # cookies by port, so a cookie for 127.0.0.1 would reach any HTTP server on loopback.
    return "p" + secrets.token_hex(8) + ".localhost"


def _bind(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as error:
        listener.close()
        if error.errno == errno.EADDRINUSE:
            raise PanelError("panel_port_in_use") from None
        raise
    listener.listen(64)
    return listener


def run_panel(*, home: Path, socket_path: Path, port: int, ready: Callable[[dict], None]) -> int:
    try:
        import uvicorn
        from .app import PanelConfig, create_panel_app
    except ImportError:
        raise PanelError("panel_dependencies_missing") from None
    home = prepare_panel_home(Path(home))
    lock = acquire_lock(home)
    try:
        listener = _bind(port)
        try:
            port = listener.getsockname()[1]
            host = launch_host()
            token = secrets.token_urlsafe(32)
            started_at = datetime.now(UTC).isoformat(timespec="microseconds")
            app = create_panel_app(PanelConfig(token=token, host=host, port=port, started_at=started_at,
                                               home=home, socket_path=Path(socket_path)))
            record = runtime_record(pid=os.getpid(), port=port, host=host, token=token,
                                    started_at=started_at, home=home)
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning",
                                                   timeout_graceful_shutdown=2))
            asyncio.run(_serve(server, app, listener, home, record, ready))
        finally:
            listener.close()
    finally:
        release_lock(lock)
    return 0


async def _serve(server, app, listener, home: Path, record: dict, ready) -> None:
    def stop(signum, frame):
        server.should_exit = True

    # uvicorn installs its own SIGINT/SIGTERM handlers for the duration of serve() and, on the
    # way out, restores the previous ones and re-raises the captured signal. With the default
    # handlers that re-raise would kill the process before the cleanup below; these handlers
    # absorb it, and also cover a signal that arrives between writing panel.json and serve().
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        write_runtime(home, record)
        ready(record)
        await server.serve(sockets=[listener])
    finally:
        await app.client.aclose()
        remove_runtime(home, os.getpid())
        for sig, handler in previous.items():
            signal.signal(sig, handler)
