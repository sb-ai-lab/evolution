"""CLI-only diagnostic output (PDR-0030, ADR-0036, process-log design L1–L17).

The terminal copy is buffered until the engine's last refusal point, then
written privately, redacted per whole write, bounded and never read as evidence.
Sink failures disable diagnostics without changing command outcomes. The command
wrapper owns tees, exception reporting and any fatal-signal handlers; library
campaign calls install nothing. See the 2026-10-14 process-log design in docs.
"""

from __future__ import annotations

import errno
import faulthandler
import fcntl
import io
import os
import platform
import re
import shlex
import signal
import stat
import sys
import threading
import traceback
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO, cast

import typer

from evolution import __version__
from evolution.lineage.state_io import ensure_private_directory
from evolution.tree.execution.output_admission import (
    PROCESS_LOGS_DIR,
    EvolveRefused,
    admit_output_root,
)
from evolution.tree.execution.private_files import WorkspaceError, open_private_file

try:  # Typer 0.27 vendors Click; older supported releases use upstream Click.
    from typer import _click as _typer_click
except ImportError:
    import click as _typer_click  # type: ignore[no-redef]

_EXIT_TYPES: tuple[type[Exception], ...] = (typer.Exit,)
_CLICK_ERROR_TYPES: tuple[type[Exception], ...] = (_typer_click.ClickException,)
try:
    import click as _upstream_click
except ImportError:  # Current Typer does not require standalone Click.
    pass
else:
    _EXIT_TYPES += (_upstream_click.exceptions.Exit,)
    _CLICK_ERROR_TYPES += (_upstream_click.ClickException,)

#: An ANSI SGR (colour/style) sequence (#295): the terminal copy keeps it, the
#: durable log does not, so a styled `WARNING:` label reads as plain text there.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

__all__ = [
    "PROCESS_LOGS_DIR",
    "LOG_FILE_UNAVAILABLE",
    "LOG_FILE_UNSAFE",
    "LogFileRefused",
    "ProcessLog",
    "capture",
    "open_log_file",
]

LOG_FILE_UNAVAILABLE = "log_file_unavailable"
LOG_FILE_UNSAFE = "log_file_unsafe"

BUFFER_BOUND = 1 << 20
STDERR_BOUND = 64 << 20

_getpid = os.getpid
_write = os.write


class _ExitSignal(Protocol):
    exit_code: int


class _ClickError(_ExitSignal, Protocol):
    def format_message(self) -> str: ...


class LogFileRefused(ValueError):
    """The operator's requested diagnostic path cannot be used safely."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def open_log_file(path: Path) -> int:
    """Append privately; never truncate or change an existing file's mode."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = None
    try:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, flags | os.O_NONBLOCK)
            created = False
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_mode & 0o077
        ):
            raise LogFileRefused(LOG_FILE_UNSAFE)
        if created:
            os.fchmod(fd, 0o600)  # Only the file this invocation exclusively created.
        status = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, status & ~os.O_NONBLOCK)
        result, fd = fd, None
        return result
    except OSError as exc:
        code = LOG_FILE_UNSAFE if exc.errno in (errno.ELOOP, errno.ENXIO) else LOG_FILE_UNAVAILABLE
        raise LogFileRefused(code) from exc
    finally:
        if fd is not None:
            os.close(fd)


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _encode(text: str) -> bytes:
    # POSIX argv and filenames may contain surrogateescaped bytes. Match
    # stderr's safe representation rather than losing diagnostics to them.
    return text.encode(errors="backslashreplace")


def _reason(exc: Exception) -> str:
    if isinstance(exc, (WorkspaceError, EvolveRefused)):
        return "unsafe"
    if isinstance(exc, OSError):
        return errno.errorcode.get(exc.errno or 0, "unavailable")
    return "unsafe"


def _tty(stream: TextIO | None) -> str:
    try:
        return "yes" if stream is not None and stream.isatty() else "no"
    except Exception:
        return "no"


def _system() -> str:
    # platform.platform() unpacks uname() and runs `uname -p` in a subprocess on Linux.
    return f"{platform.system()}-{platform.release()}-{platform.machine()}"


class ProcessLog:
    """One invocation's best-effort sink; all descriptor writes share one lock."""

    def __init__(self, command: str, *, prefix: str, stderr: TextIO | None = None) -> None:
        self.command = command
        self.prefix = prefix
        self.started = _now()
        self.pid = _getpid()
        self._stderr = stderr
        self._lock = threading.RLock()
        self._fd: int | None = None
        self._attempted = False
        self._disabled = False
        self._disabled_reason = ""
        self._note_written = False
        self._closed = False
        self._faults: _FaultHandlerOwnership | None = None
        self._secrets: tuple[str, ...] = ()
        self._buffer: list[tuple[datetime, str, str]] = []
        self._buffer_bytes = 0
        self._buffer_omitted = 0
        self._buffer_full = False
        self._stderr_bytes = 0
        self._stderr_omitted = 0
        self._stderr_full = False
        self._header = (
            f"evolution {__version__} · {command} · pid {self.pid} · started {_stamp(self.started)}",
            f"python {platform.python_version()} · {_system()} · "
            f"tty stdin/stdout/stderr {_tty(sys.stdin)}/{_tty(sys.stdout)}/{_tty(stderr)}",
            f"argv {shlex.join(sys.argv)}",
        )

    def set_redactions(self, secrets: Sequence[str]) -> None:
        with self._lock:
            self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _write_all(self, data: bytes) -> None:
        assert self._fd is not None
        remaining = memoryview(data)
        while remaining:
            count = _write(self._fd, remaining)
            if count <= 0:
                raise OSError(errno.EIO, "process log write made no progress")
            remaining = remaining[count:]

    def _emit(self, when: datetime, stream: str, text: str) -> None:
        # Redact before splitting, including secrets containing a newline.
        pieces = self._redact(text).split("\n")
        for index, piece in enumerate(pieces):
            unfinished = index == len(pieces) - 1
            if unfinished and not piece:
                continue
            tag = stream + ("+" if unfinished else "")
            self._write_all(_encode(f"{_stamp(when)} {tag} {piece}\n"))

    def _disable(self, exc: Exception | str) -> None:
        if self._disabled:
            return
        self._disabled = True
        self._buffer.clear()
        reason = exc if isinstance(exc, str) else _reason(exc)
        self._disabled_reason = reason
        if self._fd is not None:
            try:
                self._emit(_now(), "log", f"log disabled: {reason}\n")
            except Exception:
                pass
        if self._attempted:
            self._notify_disabled()

    def _notify_disabled(self) -> None:
        if self._note_written:
            return
        self._note_written = True
        # Before materialization even a failed buffer must leave refused
        # stderr unchanged. Once due, bypass the tee to avoid recursion.
        try:
            if self._stderr is not None:
                self._stderr.write(
                    f"{self.prefix}: note: process log disabled: {self._disabled_reason}\n"
                )
                self._stderr.flush()
        except Exception:
            pass

    def materialize(self, root: Path, admitted_hygiene: Collection[str] = ()) -> None:
        """Re-admit the output root and create one private, exclusive file."""
        with self._lock:
            if self._attempted or self._closed:
                return
            self._attempted = True
            if self._disabled:
                self._notify_disabled()
                return
            try:
                path = root / PROCESS_LOGS_DIR
                admit_output_root(path, admitted=admitted_hygiene)
                directory = ensure_private_directory(path)
                stem = f"{self.command}-{self.started.astimezone(UTC):%Y%m%dT%H%M%SZ}-{self.pid}"
                for suffix in ["", *[f"-{n}" for n in range(1, 10)]]:
                    try:
                        self._fd = open_private_file(
                            directory / f"{stem}{suffix}.log",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC,
                            refusal="process log is not a private regular file",
                        )
                        break
                    except FileExistsError:
                        continue
                if self._fd is None:
                    self._disable("name_exists")
                    return
                self._activate_fd(self._fd)
            except Exception as exc:
                self._disable(exc)

    def _activate_fd(self, fd: int) -> None:
        with self._lock:
            self._attempted = True
            self._fd = fd
            try:
                if self._faults is not None:
                    self._faults.point(fd)
                for line in self._header:
                    self._emit(self.started, "log", line + "\n")
                for when, stream, text in self._buffer:
                    self._record_open(when, stream, text)
                self._buffer.clear()
                if self._buffer_omitted:
                    self._emit(
                        _now(), "log", f"omitted {self._buffer_omitted} records over buffer bound\n"
                    )
            except Exception as exc:
                self._disable(exc)

    def _record_open(self, when: datetime, stream: str, text: str) -> None:
        if stream == "stderr":
            size = len(_encode(text))
            if self._stderr_full or self._stderr_bytes + size > STDERR_BOUND:
                self._stderr_full = True
                self._stderr_omitted += 1
                return
            self._stderr_bytes += size
        self._emit(when, stream, text)

    def record(self, stream: str, text: str) -> None:
        """Take one complete original write; never wait for its final newline.

        Terminal colour codes are removed from the copy kept here, at the one
        point every stream reaches; the terminal itself still receives them.
        """
        text = _SGR.sub("", text)
        with self._lock:
            if not text or self._disabled or self._closed:
                return
            try:
                when = _now()
                if self._fd is not None:
                    self._record_open(when, stream, text)
                else:
                    # Charge framing too, rather than allowing unlimited tiny writes.
                    size = len(_encode(text)) + len(stream) + 32
                    if self._buffer_full or self._buffer_bytes + size > BUFFER_BOUND:
                        self._buffer_full = True
                        self._buffer_omitted += 1
                        return
                    self._buffer.append((when, stream, text))
                    self._buffer_bytes += size
            except Exception as exc:
                self._disable(exc)

    def close(self, *, exit_code: int = 0, exception: BaseException | None = None) -> None:
        """Finish diagnostics once, then sync and close even a disabled sink."""
        with self._lock:
            if self._closed:
                return
            try:
                if self._fd is not None and not self._disabled:
                    if exception is not None:
                        self._emit(
                            _now(), "traceback", "".join(traceback.format_exception(exception))
                        )
                    if self._stderr_omitted:
                        self._emit(
                            _now(),
                            "log",
                            f"omitted {self._stderr_omitted} records over stderr bound\n",
                        )
                    detail = "" if exception is None else f" (unhandled {type(exception).__name__})"
                    self._emit(_now(), "log", f"exit {exit_code}{detail}\n")
            except Exception as exc:
                self._disable(exc)
            finally:
                self._closed = True
                self._buffer.clear()
                if self._fd is not None:
                    try:
                        os.fsync(self._fd)
                    except Exception as exc:
                        self._disable(exc)
                    try:
                        os.close(self._fd)
                    except Exception as exc:
                        self._disable(exc)
                    self._fd = None


class _Discard(io.TextIOBase):
    """A dead terminal's shutdown-safe replacement."""

    encoding = "utf-8"
    errors = "strict"

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() requires str")
        return len(text)

    def flush(self) -> None:
        pass

    def writable(self) -> bool:
        return True


class _Tee(_Discard):
    """Text-only so Click cannot bypass the copy by rewrapping `.buffer`."""

    def __init__(self, original: TextIO | None, stream: str, log: ProcessLog) -> None:
        self.original = original
        self.stream = stream
        self.log = log
        self.dead = False
        self.active = True
        self._forward_lock = threading.RLock()
        if original is None:
            self._mark_dead("absent")

    @property
    def encoding(self) -> str:  # type: ignore[override]  # Read-only view of the original.
        return getattr(self.original, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]  # Read-only view of the original.
        return getattr(self.original, "errors", None) or "strict"

    def isatty(self) -> bool:
        return _tty(self.original) == "yes"

    def fileno(self) -> int:
        if self.original is None:
            raise OSError(errno.EBADF, "absent terminal")
        return self.original.fileno()

    def _mark_dead(self, reason: str) -> None:
        if self.dead:
            return
        self.dead = True
        if self.stream == "stderr":
            self.log._stderr = None
        self.log.record("log", f"terminal {self.stream} unavailable: {reason}\n")

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() requires str")
        with self._forward_lock:
            if self.active:
                self.log.record(self.stream, text)
            if not self.dead and self.original is not None:
                try:
                    self.original.write(text)
                except (OSError, ValueError) as exc:
                    if isinstance(exc, ValueError) and not self.original.closed:
                        raise
                    self._mark_dead("closed" if isinstance(exc, ValueError) else _reason(exc))
            return len(text)

    def flush(self) -> None:
        with self._forward_lock:
            if not self.dead and self.original is not None:
                try:
                    self.original.flush()
                except (OSError, ValueError) as exc:
                    if isinstance(exc, ValueError) and not self.original.closed:
                        raise
                    self._mark_dead("closed" if isinstance(exc, ValueError) else _reason(exc))


class _FaultHandlerOwnership:
    """Own only handlers absent at entry; release them before the fd closes."""

    def __init__(self, stderr: TextIO | None) -> None:
        self.owned = not faulthandler.is_enabled()
        self._hup = False
        if self.owned and stderr is not None:
            try:
                faulthandler.enable(file=stderr.fileno(), all_threads=True)
            except Exception:
                # A missing/closed/non-fd terminal cannot receive a dump. The
                # private descriptor can still become its destination later.
                pass

    def point(self, fd: int) -> None:
        if not self.owned:
            return
        faulthandler.enable(file=fd, all_threads=True)
        if signal.getsignal(signal.SIGHUP) == signal.SIG_DFL:
            faulthandler.register(signal.SIGHUP, file=fd, all_threads=True, chain=True)
            self._hup = True

    def close(self) -> None:
        if not self.owned:
            return
        try:
            if self._hup:
                faulthandler.unregister(signal.SIGHUP)
        finally:
            faulthandler.disable()


@contextmanager
def capture(command: str, *, prefix: str, log_fd: int | None = None) -> Iterator[ProcessLog]:
    """Copy this CLI invocation's output and preserve its exception and exit code."""
    original_stdout, original_stderr = sys.stdout, sys.stderr
    log = ProcessLog(command, prefix=prefix, stderr=original_stderr)
    faults = _FaultHandlerOwnership(original_stderr)
    log._faults = faults
    stdout = _Tee(original_stdout, "stdout", log)
    stderr = _Tee(original_stderr, "stderr", log)
    sys.stdout, sys.stderr = stdout, stderr
    exit_code = 0
    unhandled = None
    try:
        if log_fd is not None:
            log._activate_fd(log_fd)
        yield log
    except BaseException as exc:
        if isinstance(exc, _EXIT_TYPES):
            exit_code = cast(_ExitSignal, exc).exit_code
        elif isinstance(exc, SystemExit):
            exit_code = (
                int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            )
        elif isinstance(exc, _CLICK_ERROR_TYPES):
            error = cast(_ClickError, exc)
            exit_code = error.exit_code
            log.record("log", error.format_message() + "\n")
        else:
            exit_code = 1
            unhandled = exc
        raise
    finally:
        try:
            # Buffered terminal pipes often report EPIPE only on flush.
            stdout.flush()
            stderr.flush()
        finally:
            try:
                faults.close()
            finally:
                log.close(exit_code=exit_code, exception=unhandled)
                stdout.active = stderr.active = False
                sys.stdout = _Discard() if stdout.dead else original_stdout
                sys.stderr = _Discard() if stderr.dead else original_stderr
