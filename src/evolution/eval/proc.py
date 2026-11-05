"""Timeout-safe subprocess capture with optional lossless stream sinks."""

from __future__ import annotations

import fcntl
import locale
import os
import selectors
import signal
import subprocess
import termios
import time
from array import array
from tempfile import TemporaryFile
from typing import IO

_DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_CAPTURE_CHUNK_BYTES = 64 * 1024
_PROCESS_EXIT_GRACE_SECONDS = 1.0


class _BoundedCapture:
    """Drain a byte stream while retaining bounded head-and-tail evidence."""

    def __init__(self, limit: int, sink: IO[bytes] | None = None) -> None:
        self.limit = max(0, limit)
        self.sink = sink
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        if self.sink is not None:
            self.sink.write(chunk)
        self.total += len(chunk)
        if len(self.head) < self.limit:
            self.head.extend(chunk[: self.limit - len(self.head)])
        if self.limit:
            self.tail.extend(chunk)
            if len(self.tail) > self.limit:
                del self.tail[: len(self.tail) - self.limit]

    def value(self) -> bytes:
        if self.total <= self.limit:
            return bytes(self.head)
        marker = f"\n...[output truncated; {self.total} bytes total]...\n".encode("ascii")
        if len(marker) >= self.limit:
            return marker[: self.limit]
        payload_budget = self.limit - len(marker)
        head_size = payload_budget // 2
        tail_size = payload_budget - head_size
        tail = bytes(self.tail[-tail_size:]) if tail_size else b""
        return bytes(self.head[:head_size]) + marker + tail


def _kill_group(proc: subprocess.Popen, process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _register_pipe(
    selector: selectors.BaseSelector, pipe: IO[bytes], capture: _BoundedCapture
) -> None:
    descriptor = pipe.fileno()
    os.set_blocking(descriptor, False)
    selector.register(descriptor, selectors.EVENT_READ, capture)


def _capture_ready(selector: selectors.BaseSelector, timeout: float) -> bool:
    ready = selector.select(timeout)
    for key, _ in ready:
        try:
            chunk = os.read(key.fd, _CAPTURE_CHUNK_BYTES)
        except BlockingIOError:
            continue
        if chunk:
            key.data.feed(chunk)
        else:
            selector.unregister(key.fd)
    return bool(ready)


def _drain_available(selector: selectors.BaseSelector) -> None:
    for key in tuple(selector.get_map().values()):
        pending = array("i", [0])
        fcntl.ioctl(key.fd, termios.FIONREAD, pending, True)
        remaining = pending[0]
        while remaining:
            try:
                chunk = os.read(key.fd, min(remaining, _CAPTURE_CHUNK_BYTES))
            except BlockingIOError:
                break
            if not chunk:
                selector.unregister(key.fd)
                break
            key.data.feed(chunk)
            remaining -= len(chunk)


def _capture_until_deadline(
    proc: subprocess.Popen[bytes], selector: selectors.BaseSelector, deadline: float
) -> bool:
    while proc.poll() is None or selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if selector.get_map():
            _capture_ready(selector, remaining)
        else:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                return False
    return True


def _stop_process(proc: subprocess.Popen[bytes], process_group: int) -> None:
    _kill_group(proc, process_group)
    try:
        proc.wait(timeout=_PROCESS_EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()


def _render(capture: _BoundedCapture, *, text: bool) -> str | bytes:
    value = capture.value()
    if not text:
        return value
    return value.decode(locale.getpreferredencoding(False), errors="replace")


def run_capture(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: dict | None = None,
    text: bool = True,
    input: str | bytes | None = None,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    stdout_sink: IO[bytes] | None = None,
    stderr_sink: IO[bytes] | None = None,
) -> subprocess.CompletedProcess:
    """Return a bounded view and copy complete output to optional sinks.

    Raises ``subprocess.TimeoutExpired`` (with whatever output was captured) so
    existing callers' ``except subprocess.TimeoutExpired`` paths are unchanged.
    """

    started = time.monotonic()
    stdin = None
    if input is not None:
        stdin = TemporaryFile()
        payload = input.encode() if isinstance(input, str) else input
        stdin.write(payload)
        stdin.seek(0)
    proc: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin,
            text=False,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        stdout_capture = _BoundedCapture(max_output_bytes, stdout_sink)
        stderr_capture = _BoundedCapture(max_output_bytes, stderr_sink)
        assert proc.stdout is not None
        assert proc.stderr is not None
        _register_pipe(selector, proc.stdout, stdout_capture)
        _register_pipe(selector, proc.stderr, stderr_capture)

        completed = _capture_until_deadline(proc, selector, started + timeout)
        if not completed:
            _stop_process(proc, proc.pid)
            _drain_available(selector)

        out = _render(stdout_capture, text=text)
        err = _render(stderr_capture, text=text)
        if not completed:
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        selector.close()
        if proc is not None:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            if proc.poll() is None:
                _stop_process(proc, proc.pid)
        if stdin:
            stdin.close()
