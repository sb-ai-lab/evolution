"""Foreground UDS entry; optional HTTP dependencies load only when serving."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import socket
import stat
import sys

from evolution_lab.durability import Durability

from .errors import ServiceError, error_response
from .runtime import ServiceConfig, ServiceRuntime


_DEPENDENCY_FAILURE = {'error': {
    'code': 'lab_error', 'stage': 'transport',
    'message': 'The service requires the optional service dependencies.',
}}


class PrivateListener:
    """A socket and the exact filesystem inode this invocation created."""

    def __init__(self, path, listener, identity):
        self.path, self.socket, self.identity = path, listener, identity

    @classmethod
    def bind(cls, path):
        if os.path.lexists(path):
            raise ServiceError('socket_path_exists')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        owned = None
        try:
            old_umask = os.umask(0o077)
            try:
                listener.bind(str(path))
            finally:
                os.umask(old_umask)
            info = path.lstat()
            owned = cls(path, listener, (info.st_dev, info.st_ino))
            os.chmod(path, 0o600)
            listener.listen(128)
            listener.setblocking(False)
            return owned
        except BaseException as error:
            if owned is not None:
                owned.close()
            else:
                listener.close()
            if isinstance(error, OSError) and error.errno in {errno.EADDRINUSE, errno.EEXIST}:
                raise ServiceError('socket_path_exists') from None
            raise

    def close(self):
        self.socket.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.identity:
            self.path.unlink()


def _load_http():
    # Import FastAPI too: a partial installation must fail before creating Owner.
    import fastapi  # noqa: F401
    import uvicorn

    class RuntimeSignalServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield

    return uvicorn.Config, RuntimeSignalServer


def _default_app_factory(runtime):
    from .app import create_app
    return create_app(runtime)


def _write(stderr, payload):
    print(json.dumps(payload, separators=(',', ':'), allow_nan=False), file=stderr, flush=True)


async def serve(config: ServiceConfig, *, app_factory=None, stderr=None, on_ready=None) -> int:
    """Run until service shutdown, returning CLI exit code and safe stderr JSON.

    app_factory(runtime) is the explicit ASGI construction hook. Production uses
    service.app.create_app; bounded neutral lifecycle tests supply a fixture.
    on_ready(socket_path), if given, is called once the socket is listening and the HTTP
    task has started (success path only); the library itself never prints.
    """
    stderr = sys.stderr if stderr is None else stderr
    try:
        Config, Server = _load_http()
    except ImportError:
        _write(stderr, _DEPENDENCY_FAILURE)
        return 1
    except Exception as error:
        _write(stderr, error_response(error)['body'])
        return 1
    runtime = ServiceRuntime(config)
    failure = None
    try:
        with runtime.signal_handlers():
            try:
                if await runtime.start():
                    listener = PrivateListener.bind(config.socket_path)
                    # Transfer the bound inode immediately, including app/config
                    # construction failures before an HTTP task exists.
                    runtime.attach_http(None, None, listener)
                    app = (app_factory or _default_app_factory)(runtime)
                    server = Server(Config(app, lifespan='off', access_log=False,
                        log_config=None, log_level='critical', timeout_graceful_shutdown=0))
                    task = asyncio.create_task(server.serve(sockets=[listener.socket]))
                    runtime.attach_http(server, task, listener)
                    if on_ready is not None:
                        on_ready(config.socket_path)
                    await asyncio.shield(task)
            except Exception as error:
                failure = error_response(error)['body']
                # An operator must know the old scratch root to reopen and clean
                # it. Only startup stderr carries this diagnostic; Owner's fixed
                # Failure and the HTTP error translator remain unchanged.
                from evolution_lab.owner.scratch import ScratchBindingError
                if isinstance(error, ScratchBindingError):
                    failure['error']['message'] = str(error)
            finally:
                await runtime.shutdown()
    except Exception as error:
        failure = error_response(error)['body']
    if failure is None:
        failure = runtime.http_failure
    if failure is not None:
        _write(stderr, failure)
    for diagnostic in runtime.diagnostics:
        _write(stderr, diagnostic)
    if runtime.shutdown_result is not None:
        _write(stderr, runtime.shutdown_result.to_payload())
    return 0 if (failure is None and runtime.shutdown_result is not None
                 and runtime.shutdown_result.cleanup.complete is True) else 1


def run_service(*, socket_path, state_directory, shutdown_wait_seconds=30,
        execution_wheels=(), execution_runtime_files=(), home_directory=None,
        durability: Durability | None = None, scratch_directory: Path | None = None, stderr=None) -> int:
    """Synchronous CLI entry; owns its event loop, no reload/worker/TCP options.

    Without ``home_directory`` the Lab home is ``default_home()`` (``LAB_HOME``, else the
    XDG data directory); a home another process writes refuses startup with ``home_busy``.
    Without ``socket_path`` the socket is ``<home>/service.sock`` (W49)."""
    stderr = sys.stderr if stderr is None else stderr
    try:
        if home_directory is None:
            from evolution_lab.projects.home import default_home
            home_directory = default_home()
        if socket_path is None:
            from evolution_lab.projects.endpoint import default_socket
            socket_path = default_socket(Path(home_directory))
        config = ServiceConfig(socket_path, state_directory, shutdown_wait_seconds,
            execution_wheels=execution_wheels, execution_runtime_files=execution_runtime_files,
            home_directory=home_directory, durability=durability, scratch_directory=scratch_directory)
    except Exception as error:
        _write(stderr, error_response(error)['body'])
        return 1
    def announce(path):
        print(f"lab serve: listening on {path}", flush=True)
    return asyncio.run(serve(config, stderr=stderr, on_ready=announce))
