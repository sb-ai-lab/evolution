"""Private diagnostic output, independent of campaign evidence and subprocesses."""

from __future__ import annotations

import errno
import fcntl
import io
import os
import platform
import subprocess
import sys
import threading
import time
import warnings
from datetime import UTC, datetime

import pytest
import typer

try:
    from typer import _click as click
except ImportError:
    import click

try:
    import click as upstream_click
except ImportError:
    upstream_click = click

from evolution import __version__
from evolution.tree import process_log
from evolution.tree.execution.authorizations import hygiene_instance_digest


@pytest.fixture
def sink(monkeypatch):
    monkeypatch.setattr(process_log, "_now", lambda: datetime(2026, 10, 14, 15, 30, 12, tzinfo=UTC))
    monkeypatch.setattr(process_log, "_getpid", lambda: 48211)
    stderr = io.StringIO()
    log = process_log.ProcessLog("evolve", prefix="evo tree evolve", stderr=stderr)
    yield log, stderr
    log.close()


def log_path(root):
    return root / "logs" / "evolve-20261014T153012Z-48211.log"


def messages(path):
    return [line.split(" ", 1)[1] for line in path.read_text().splitlines()]


def test_materialize_creates_a_private_file_and_flushes_in_original_time_order(
    tmp_path, sink, monkeypatch
):
    log, stderr = sink
    log.record("stderr", "buffered\n")
    monkeypatch.setattr(
        process_log, "_now", lambda: datetime(2026, 10, 14, 15, 31, 13, 123000, tzinfo=UTC)
    )
    log.materialize(tmp_path)
    log.record("stdout", "live\n")
    path = log_path(tmp_path)
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    lines = path.read_text().splitlines()
    assert f"evolution {__version__}" in lines[0] and "evolve" in lines[0] and "48211" in lines[0]
    assert "python " in lines[1] and "tty stdin/stdout/stderr" in lines[1]
    assert "argv " in lines[2]
    assert lines[3:] == [
        "2026-10-14T15:30:12.000Z stderr buffered",
        "2026-10-14T15:31:13.123Z stdout live",
    ]
    assert stderr.getvalue() == ""


def test_building_the_header_starts_no_subprocess(monkeypatch):
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        raise OSError("no subprocess while building the header")

    monkeypatch.setattr(subprocess, "Popen", spy)
    monkeypatch.setattr(platform, "_uname_cache", None)
    monkeypatch.setattr(platform, "_platform_cache", {})
    log = process_log.ProcessLog("evolve", prefix="evo tree evolve", stderr=io.StringIO())
    try:
        assert calls == []
        assert f"python {platform.python_version()} · {platform.system()}" in log._header[1]
    finally:
        log.close()


def test_each_piece_is_written_immediately_and_empty_lines_survive(tmp_path, sink):
    log, _ = sink
    log.materialize(tmp_path)
    log.record("stderr", "a\nb")
    assert messages(log_path(tmp_path))[3:] == ["stderr a", "stderr+ b"]
    log.record("stderr", "c\n")
    log.record("stderr", "")
    log.record("stdout", "\n\n")
    assert messages(log_path(tmp_path))[3:] == [
        "stderr a",
        "stderr+ b",
        "stderr c",
        "stdout ",
        "stdout ",
    ]


def test_redaction_happens_on_whole_writes_including_buffered_text(tmp_path, sink, monkeypatch):
    _, stderr = sink
    monkeypatch.setattr(process_log.sys, "argv", ["evo", "s3cr3t-line1\nline2"])
    log = process_log.ProcessLog("evolve", prefix="evo tree evolve", stderr=stderr)
    log.record("stderr", "before s3cr3t-line1\nline2\n")
    log.set_redactions(["s3cr3t", "s3cr3t-line1\nline2"])
    log.materialize(tmp_path)
    log.record("stdout", '{"value": "s3cr3t"}\n')
    log.record("stderr", "s3cr3t-line1\nline2\n")
    text = log_path(tmp_path).read_text()
    assert "s3cr3t" not in text and "line2" not in text
    assert messages(log_path(tmp_path))[3:] == [
        "stderr before [REDACTED]",
        'stdout {"value": "[REDACTED]"}',
        "stderr [REDACTED]",
    ]
    log.record("stdout", "s3")
    log.record("stdout", "cr3t\n")
    assert messages(log_path(tmp_path))[-2:] == ["stdout+ s3", "stdout cr3t"]
    log.close()


def test_materialize_is_idempotent_and_ignores_a_second_root(tmp_path, sink):
    log, _ = sink
    log.materialize(tmp_path / "first")
    before = log_path(tmp_path / "first").read_bytes()
    log.materialize(tmp_path / "second")
    log.materialize(tmp_path / "first")
    assert log_path(tmp_path / "first").read_bytes() == before
    assert not (tmp_path / "second").exists()


@pytest.mark.parametrize("exhausted", [False, True])
def test_name_collisions_try_nine_suffixes_then_disable(tmp_path, sink, exhausted):
    log, stderr = sink
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    base = log_path(tmp_path)
    names = [base, *[base.with_stem(base.stem + f"-{n}") for n in range(1, 10)]]
    for name in names if exhausted else names[:-1]:
        name.write_text("do not touch")
    log.materialize(tmp_path)
    if exhausted:
        assert stderr.getvalue() == "evo tree evolve: note: process log disabled: name_exists\n"
        assert all(name.read_text() == "do not touch" for name in names)
    else:
        assert "evolution " in names[-1].read_text()
        assert all(name.read_text() == "do not touch" for name in names[:-1])


def test_a_symlink_at_the_filename_is_a_collision(tmp_path, sink):
    log, _ = sink
    (tmp_path / "logs").mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("untouched")
    base = log_path(tmp_path)
    base.symlink_to(target)
    log.materialize(tmp_path)
    assert target.read_text() == "untouched"
    assert "evolution " in base.with_stem(base.stem + "-1").read_text()


@pytest.mark.parametrize("entry", ["file", "symlink", "unsafe", "accepted"])
def test_an_unsafe_directory_disables_the_log_unless_explicitly_accepted(tmp_path, sink, entry):
    log, stderr = sink
    logs = tmp_path / "logs"
    admitted = ()
    if entry == "file":
        logs.write_text("unchanged")
    elif entry == "symlink":
        other = tmp_path / "other"
        other.mkdir(mode=0o700)
        logs.symlink_to(other, target_is_directory=True)
    else:
        logs.mkdir(mode=0o750)
        logs.chmod(0o750)
        if entry == "accepted":
            admitted = (hygiene_instance_digest("logs", "not owner-only", "0o750"),)
    log.materialize(tmp_path, admitted)
    if entry == "accepted":
        assert log_path(tmp_path).is_file()
        assert stderr.getvalue() == ""
    else:
        assert stderr.getvalue() == "evo tree evolve: note: process log disabled: unsafe\n"
        assert not log_path(tmp_path).exists()
        if entry == "unsafe":
            assert logs.stat().st_mode & 0o777 == 0o750


def test_write_failure_disables_once_and_retains_the_descriptor_until_close(
    tmp_path, sink, monkeypatch
):
    log, stderr = sink
    log.materialize(tmp_path)
    attempts = []

    def full(fd, data):
        attempts.append((fd, bytes(data)))
        raise OSError(errno.ENOSPC, "secret filesystem detail")

    monkeypatch.setattr(process_log, "_write", full)
    log.record("stderr", "lost\n")
    log.record("stdout", "ignored\n")
    log.materialize(tmp_path / "ignored")
    assert len(attempts) == 2
    assert b"log log disabled: ENOSPC" in attempts[1][1]
    os.fstat(attempts[0][0])
    assert stderr.getvalue() == "evo tree evolve: note: process log disabled: ENOSPC\n"
    log.close()
    with pytest.raises(OSError):
        os.fstat(attempts[0][0])
    assert len(attempts) == 2


def test_buffer_bound_counts_unfinished_bytes_and_reports_omissions(tmp_path, sink, monkeypatch):
    log, _ = sink
    monkeypatch.setattr(process_log, "BUFFER_BOUND", 128)
    log.record("stderr", "kept\n")
    log.record("stderr", "x" * 129)
    log.record("stdout", "y" * 129)
    log.record("stdout", "z\n")
    log.materialize(tmp_path)
    text = log_path(tmp_path).read_text()
    assert "stderr kept" in text
    assert "x" * 129 not in text and "y" * 129 not in text
    assert "stdout z" not in text
    assert "omitted 3 records" in text


def test_stderr_bound_keeps_stdout_traceback_and_closing_line(tmp_path, sink, monkeypatch):
    log, _ = sink
    monkeypatch.setattr(process_log, "STDERR_BOUND", 6)
    log.materialize(tmp_path)
    log.record("stderr", "éé")
    log.record("stderr", "dropped\n")
    log.record("stderr", "no-newline")
    log.record("stderr", "z")
    log.record("stdout", "document\n")
    log.close(exit_code=1, exception=RuntimeError("failure"))
    rows = messages(log_path(tmp_path))
    assert "stderr+ éé" in rows and "stdout document" in rows
    assert not any("dropped" in row or "no-newline" in row for row in rows)
    assert any("traceback RuntimeError: failure" == row for row in rows)
    assert "stderr+ z" not in rows
    assert "omitted 3 records" in rows[-2]
    assert rows[-1] == "log exit 1 (unhandled RuntimeError)"


def test_the_designed_bounds():
    assert process_log.BUFFER_BOUND == 1 << 20
    assert process_log.STDERR_BOUND == 64 << 20


@pytest.mark.parametrize("exception", [None, RuntimeError("s3cr3t")])
def test_close_writes_redacted_traceback_exit_and_fsyncs_once(
    tmp_path, sink, monkeypatch, exception
):
    log, _ = sink
    log.set_redactions(["s3cr3t"])
    log.materialize(tmp_path)
    synced = []
    fsync = os.fsync

    def sync(fd):
        synced.append(fd)
        fsync(fd)

    monkeypatch.setattr(process_log.os, "fsync", sync)
    log.close(exit_code=3 if exception is None else 1, exception=exception)
    log.close()
    assert len(synced) == 1
    rows = messages(log_path(tmp_path))
    assert rows[-1] == (
        "log exit 3" if exception is None else "log exit 1 (unhandled RuntimeError)"
    )
    if exception is not None:
        assert "traceback RuntimeError: [REDACTED]" in rows


def test_short_writes_are_completed_before_the_next_record(tmp_path, sink, monkeypatch):
    log, _ = sink
    write = os.write
    monkeypatch.setattr(process_log, "_write", lambda fd, data: write(fd, data[:3]))
    log.materialize(tmp_path)
    log.record("stdout", "whole record\n")
    assert messages(log_path(tmp_path))[-1] == "stdout whole record"


def test_capture_forwards_all_python_output_unchanged(tmp_path, monkeypatch):
    def terminal_warning(message, category, filename, lineno, file=None, line=None):
        # pytest captures warnings before stderr; route the real warn() through
        # the standard formatter to exercise the normal terminal destination.
        sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))

    monkeypatch.setattr(warnings, "showwarning", terminal_warning)

    def output():
        print("stdout print")
        sys.stderr.write("stderr write\n")
        typer.echo("typer", err=True)
        click.echo("click")
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn("warning", UserWarning, stacklevel=1)

        def fail():
            raise RuntimeError("thread failed")

        thread = threading.Thread(target=fail, name="process-log-worker")
        thread.start()
        thread.join()

    monkeypatch.setattr(threading, "excepthook", threading.__excepthook__)
    baseline = (io.StringIO(), io.StringIO())
    monkeypatch.setattr(sys, "stdout", baseline[0])
    monkeypatch.setattr(sys, "stderr", baseline[1])
    output()
    originals = (io.StringIO(), io.StringIO())
    monkeypatch.setattr(sys, "stdout", originals[0])
    monkeypatch.setattr(sys, "stderr", originals[1])
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        log.materialize(tmp_path)
        output()
    assert [s.getvalue() for s in originals] == [s.getvalue() for s in baseline]
    text = next((tmp_path / "logs").glob("*.log")).read_text()
    for value in ("stdout print", "stderr write", "typer", "click", "warning", "thread failed"):
        assert value in text


def test_click_keeps_the_text_tee_and_properties(tmp_path, monkeypatch):
    with (tmp_path / "terminal").open("w", encoding="utf-8", errors="replace") as original:
        monkeypatch.setattr(sys, "stdout", original)
        monkeypatch.setattr(sys, "stderr", original)
        with process_log.capture("evolve", prefix="evo tree evolve") as log:
            log.materialize(tmp_path)
            assert click.utils._default_text_stderr() is sys.stderr
            assert click.utils._default_text_stdout() is sys.stdout
            for stream in (sys.stdout, sys.stderr):
                assert not hasattr(stream, "buffer")
                assert stream.isatty() == original.isatty()
                assert stream.fileno() == original.fileno()
                assert (stream.encoding, stream.errors) == ("utf-8", "replace")
                assert stream.writable()
                with pytest.raises(TypeError):
                    stream.write(b"")
            print("kept")
        assert next((tmp_path / "logs").glob("*.log")).read_text().count("stdout+ kept") == 1


@pytest.mark.parametrize(
    ("exception", "closing"),
    [
        (None, "exit 0"),
        (typer.Exit(3), "exit 3"),
        (getattr(upstream_click.exceptions, "Exit", typer.Exit)(5), "exit 5"),
        (RuntimeError("secret-value"), "exit 1 (unhandled RuntimeError)"),
        (click.exceptions.UsageError("secret-value"), "exit 2"),
        (upstream_click.exceptions.UsageError("secret-value"), "exit 2"),
        (SystemExit(), "exit 0"),
        (SystemExit("secret-value"), "exit 1"),
        (SystemExit(7), "exit 7"),
        (KeyboardInterrupt(), "exit 1 (unhandled KeyboardInterrupt)"),
    ],
)
def test_capture_restores_streams_and_preserves_exceptions(
    tmp_path, monkeypatch, exception, closing
):
    originals = (io.StringIO(), io.StringIO())
    monkeypatch.setattr(sys, "stdout", originals[0])
    monkeypatch.setattr(sys, "stderr", originals[1])
    fd = os.open(tmp_path / "explicit.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def invoke():
        with process_log.capture("evolve", prefix="evo tree evolve", log_fd=fd) as log:
            log.set_redactions(["secret-value"])
            if exception is not None:
                raise exception

    if exception is None:
        invoke()
    else:
        with pytest.raises(type(exception)) as caught:
            invoke()
        assert caught.value is exception
    assert (sys.stdout, sys.stderr) == originals
    text = (tmp_path / "explicit.log").read_text()
    assert messages(tmp_path / "explicit.log")[-1] == "log " + closing
    assert "secret-value" not in text
    if isinstance(exception, (click.ClickException, upstream_click.ClickException)):
        assert "[REDACTED]" in text
    elif "unhandled " in closing:
        assert "traceback " in text
    with pytest.raises(OSError):
        os.fstat(fd)


class BrokenTerminal(io.StringIO):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.attempts = 0
        if failure == "closed":
            self.close()

    def write(self, text):
        if self.failure == "EPIPE":
            self.attempts += 1
            raise BrokenPipeError(errno.EPIPE, "pipe closed")
        if self.failure == "closed":
            self.attempts += 1
            raise ValueError("closed file")
        return super().write(text)

    def flush(self):
        if self.failure == "EIO":
            self.attempts += 1
            raise OSError(errno.EIO, "terminal gone")
        return super().flush()


@pytest.mark.parametrize("side", ["stdout", "stderr"])
@pytest.mark.parametrize("failure", ["EPIPE", "EIO", "closed", "absent"])
def test_a_dead_stream_is_marked_once_and_every_write_reaches_log(
    tmp_path, monkeypatch, side, failure
):
    original = None if failure == "absent" else BrokenTerminal(failure)
    alive = io.StringIO()
    other = "stderr" if side == "stdout" else "stdout"
    monkeypatch.setattr(sys, side, original)
    monkeypatch.setattr(sys, other, alive)
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        log.materialize(tmp_path)
        stream = getattr(sys, side)
        stream.write("first\n")
        stream.flush()
        stream.write("second\n")
        stream.flush()
    text = next((tmp_path / "logs").glob("*.log")).read_text()
    assert text.count(f"terminal {side} unavailable: {failure}") == 1
    assert f"{side} first" in text and f"{side} second" in text
    if original is not None:
        assert original.attempts == 1
    assert getattr(sys, side) is not original
    assert getattr(sys, other) is alive
    getattr(sys, side).write("discarded")
    getattr(sys, side).flush()


@pytest.mark.parametrize("dead", [False, True])
def test_a_disabled_note_bypasses_the_tee_and_does_not_retry_dead_stderr(
    tmp_path, monkeypatch, dead
):
    original = BrokenTerminal("EPIPE") if dead else io.StringIO()
    monkeypatch.setattr(sys, "stderr", original)
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        if dead:
            sys.stderr.write("first\n")
        (tmp_path / "logs").write_text("not a directory")
        log.materialize(tmp_path)
    if dead:
        assert original.attempts == 1
    else:
        assert original.getvalue() == "evo tree evolve: note: process log disabled: unsafe\n"


def test_concurrent_writes_remain_whole_under_short_os_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    write = os.write
    monkeypatch.setattr(process_log, "_write", lambda fd, data: write(fd, data[:3]))
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        log.materialize(tmp_path)
        threads = [
            threading.Thread(
                target=lambda tag=tag: [sys.stdout.write(f"{tag}-{i}\n") for i in range(1000)]
            )
            for tag in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    rows = messages(next((tmp_path / "logs").glob("*.log")))
    assert sorted(row for row in rows if row.startswith("stdout ")) == sorted(
        f"stdout {tag}-{i}" for tag in ("a", "b") for i in range(1000)
    )


def test_concurrent_terminal_failures_do_not_retry_the_dead_stream(tmp_path, monkeypatch):
    class SlowBrokenTerminal(BrokenTerminal):
        def write(self, text):
            time.sleep(0.02)  # The failed OS write can release the GIL.
            return super().write(text)

    original = SlowBrokenTerminal("EPIPE")
    monkeypatch.setattr(sys, "stdout", original)
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        log.materialize(tmp_path)
        start = threading.Barrier(3)

        def output():
            start.wait()
            sys.stdout.write("kept\n")

        threads = [threading.Thread(target=output) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()
    assert original.attempts == 1
    text = next((tmp_path / "logs").glob("*.log")).read_text()
    assert text.count("terminal stdout unavailable: EPIPE") == 1
    assert text.count("stdout kept") == 2


def test_surrogates_in_argv_buffer_and_traceback_are_escaped(tmp_path, sink, monkeypatch):
    _, stderr = sink
    monkeypatch.setattr(sys, "argv", ["evo", "path-\udcff"])
    log = process_log.ProcessLog("evolve", prefix="evo tree evolve", stderr=stderr)
    log.record("stderr", "buffer-\ud800\n")
    log.materialize(tmp_path)
    log.close(exit_code=1, exception=RuntimeError("exception-\udfff"))
    text = log_path(tmp_path).read_text()
    for value in (r"path-\udcff", r"buffer-\ud800", r"exception-\udfff"):
        assert value in text
    assert text.endswith("log exit 1 (unhandled RuntimeError)\n")
    assert stderr.getvalue() == ""


def test_a_live_terminal_encoding_error_propagates_after_the_log_copy(tmp_path, monkeypatch):
    original = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", original)
    with pytest.raises(UnicodeEncodeError):
        with process_log.capture("evolve", prefix="evo tree evolve") as log:
            log.materialize(tmp_path)
            sys.stdout.write("é")
    assert sys.stdout is original and not original.closed
    original.write("still alive")
    text = next((tmp_path / "logs").glob("*.log")).read_text()
    assert "stdout+ é" in text
    assert "terminal stdout unavailable" not in text
    assert text.endswith("log exit 1 (unhandled UnicodeEncodeError)\n")


def test_an_unrelated_value_error_from_a_live_terminal_is_preserved(tmp_path, monkeypatch):
    error = ValueError("invalid stream configuration")

    class LiveTerminal(io.StringIO):
        def write(self, text):
            raise error

    original = LiveTerminal()
    monkeypatch.setattr(sys, "stdout", original)
    with pytest.raises(ValueError) as caught:
        with process_log.capture("evolve", prefix="evo tree evolve") as log:
            log.materialize(tmp_path)
            sys.stdout.write("copied first\n")
    assert caught.value is error and sys.stdout is original


@pytest.mark.parametrize("materialize", [False, True])
def test_pre_materialization_failure_never_changes_refused_stderr(
    tmp_path, monkeypatch, materialize
):
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        with monkeypatch.context() as patch:

            def broken_clock():
                raise RuntimeError("clock unavailable")

            patch.setattr(process_log, "_now", broken_clock)
            log.record("stderr", "buffered\n")
        sys.stderr.write("refused\n")
        if materialize:
            log.materialize(tmp_path)
    note = "evo tree evolve: note: process log disabled: unsafe\n" if materialize else ""
    assert stderr.getvalue() == "refused\n" + note
    assert not list(tmp_path.iterdir())


def test_capture_without_materialize_never_touches_the_run_directory(tmp_path, monkeypatch):
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with process_log.capture("evolve", prefix="evo tree evolve"):
        sys.stderr.write("refused: path-\udcff\n")
    assert stderr.getvalue() == "refused: path-\udcff\n"
    assert not list(tmp_path.iterdir())


def test_a_cached_tee_forwards_after_capture_without_extending_the_log(tmp_path, monkeypatch):
    original = io.StringIO()
    monkeypatch.setattr(sys, "stderr", original)
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        log.materialize(tmp_path)
        cached = sys.stderr
        cached.write("inside\n")
    path = next((tmp_path / "logs").glob("*.log"))
    before = path.read_bytes()
    cached.write("after\n")
    cached.flush()
    assert original.getvalue() == "inside\nafter\n"
    assert path.read_bytes() == before


def test_terminal_backpressure_does_not_block_materialize_or_the_other_stream(
    tmp_path, monkeypatch
):
    entered, release, finished = (threading.Event() for _ in range(3))

    class StalledTerminal(io.StringIO):
        def write(self, text):
            entered.set()
            assert release.wait(5), "test did not release the stalled stream"
            return super().write(text)

    monkeypatch.setattr(sys, "stderr", StalledTerminal())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with process_log.capture("evolve", prefix="evo tree evolve") as log:
        writer = threading.Thread(target=lambda: sys.stderr.write("pending\n"))
        writer.start()
        assert entered.wait(5)

        def engine():
            log.materialize(tmp_path)
            sys.stdout.write("independent\n")
            finished.set()

        worker = threading.Thread(target=engine)
        worker.start()
        try:
            assert finished.wait(1), "terminal backpressure blocked the engine"
        finally:
            release.set()
            writer.join(timeout=5)
            worker.join(timeout=5)
    text = next((tmp_path / "logs").glob("*.log")).read_text()
    assert "stderr pending" in text and "stdout independent" in text


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        ("absent", None),
        ("private", None),
        ("wide", "log_file_unsafe"),
        ("symlink", "log_file_unsafe"),
        ("fifo", "log_file_unsafe"),
        ("hardlink", "log_file_unsafe"),
        ("foreign", "log_file_unsafe"),
        ("directory", "log_file_unavailable"),
        ("missing_parent", "log_file_unavailable"),
    ],
)
def test_explicit_file_is_private_append_only_and_never_changes_existing_modes(
    tmp_path, monkeypatch, entry, code
):
    path = tmp_path / "output.log"
    if entry in ("private", "wide", "hardlink", "foreign"):
        path.write_text("previous\n")
        path.chmod(0o644 if entry == "wide" else 0o600)
        if entry == "hardlink":
            os.link(path, tmp_path / "another-link")
        if entry == "foreign":
            uid = os.geteuid()
            monkeypatch.setattr(process_log.os, "geteuid", lambda: uid + 1)
    elif entry == "symlink":
        target = tmp_path / "target"
        target.write_text("untouched")
        path.symlink_to(target)
    elif entry == "fifo":
        os.mkfifo(path, 0o600)
    elif entry == "directory":
        path.mkdir(mode=0o700)
    elif entry == "missing_parent":
        path = tmp_path / "absent" / "output.log"
    before = path.lstat() if path.exists() or path.is_symlink() else None
    if before is not None:

        def forbidden_chmod(*args):
            raise AssertionError("must not fchmod an existing file")

        monkeypatch.setattr(process_log.os, "fchmod", forbidden_chmod)
    if code is not None:
        with pytest.raises(process_log.LogFileRefused) as caught:
            process_log.open_log_file(path)
        assert caught.value.code == code
        if before is not None:
            assert path.lstat().st_mode == before.st_mode
        if entry in ("wide", "hardlink", "foreign"):
            assert path.read_text() == "previous\n"
        if entry == "symlink":
            assert target.read_text() == "untouched"
    else:
        fd = process_log.open_log_file(path)
        try:
            assert os.fstat(fd).st_mode & 0o777 == 0o600
            assert not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
            os.write(fd, b"appended\n")
        finally:
            os.close(fd)
        assert path.read_text() == ("previous\n" if entry == "private" else "") + "appended\n"


def test_a_new_explicit_file_has_mode_600_even_with_a_restrictive_umask(tmp_path):
    previous = os.umask(0o777)
    try:
        fd = process_log.open_log_file(tmp_path / "output.log")
    finally:
        os.umask(previous)
    try:
        assert os.fstat(fd).st_mode & 0o777 == 0o600
    finally:
        os.close(fd)
