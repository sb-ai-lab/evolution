"""The opt-in no-follow reader pins its root and owns a terminal lifecycle."""

from __future__ import annotations

import errno
import gc
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution.tree import ScorerTraceReader, ScorerTraceUnreadable, read_scorer_trace
from evolution.tree.execution import scorer_traces as traces
from tests.scorer_trace_packs import (
    ID_A,
    ID_B,
    output_record,
    pack_record,
    request_record,
    write_directory_trace,
    write_pack,
)


def _patch_open(monkeypatch: pytest.MonkeyPatch, replacement) -> None:
    supported = os.supports_dir_fd - {os.open}
    monkeypatch.setattr(os, "open", replacement)
    monkeypatch.setattr(os, "supports_dir_fd", supported | {replacement})


@pytest.fixture
def run(tmp_path: Path) -> Path:
    # pytest's own temporary root can have system aliases on some platforms.
    root = tmp_path.resolve() / "run"
    root.mkdir(mode=0o700)
    return root


def test_default_resolves_alias_and_close_remains_reusable(run: Path) -> None:
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    alias = run.with_name("alias")
    alias.symlink_to(run, target_is_directory=True)
    reader = ScorerTraceReader(alias)
    reader.close()
    assert reader.read(ID_A).request == b"{}"
    with reader:
        assert reader.read(ID_A, sections=()).sizes == {"request": 2, "stdout": 0, "stderr": 0}


@pytest.mark.parametrize("boundary", [None, True, 1, "", "NOFOLLOW", [], {}])
def test_invalid_boundary_is_value_error(run: Path, boundary: object) -> None:
    with pytest.raises(ValueError):
        ScorerTraceReader(run, run_boundary=boundary)


@pytest.mark.parametrize("suffix", ["", "/run"])
def test_strict_rejects_supplied_root_or_ancestor_alias(run: Path, suffix: str) -> None:
    alias = run.with_name("system-alias")
    alias.symlink_to(run if not suffix else run.parent, target_is_directory=True)
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(str(alias) + suffix, run_boundary="nofollow")


def test_strict_rejects_dotdot_before_normalization(run: Path) -> None:
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(str(run / ".." / "run"), run_boundary="nofollow")


def test_relative_path_uses_physical_cwd_and_accepts_dot_separators(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = run.with_name("alias")
    alias.symlink_to(run.parent, target_is_directory=True)
    monkeypatch.chdir(alias)
    monkeypatch.setenv("PWD", str(alias))
    with ScorerTraceReader("./run//.", run_boundary="nofollow") as reader:
        assert reader.read(ID_A) is None
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(alias / "run", run_boundary="nofollow")


def test_missing_root_is_unsafe_but_missing_trace_is_none(run: Path) -> None:
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(run / "missing", run_boundary="nofollow")
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        assert reader.read(ID_A) is None


@pytest.mark.parametrize("limit", [None, 4])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
@pytest.mark.parametrize("layout", ["directory", "pack", "segment", "preflight"])
@pytest.mark.parametrize("explicit", [False, True])
def test_reads_remain_on_renamed_root_for_every_lookup(
    run: Path, limit: int | None, replacement: str, layout: str, explicit: bool
) -> None:
    preflight = layout == "preflight"
    root = run / "connection-logs" / "op" if preflight else run / "scorer-traces"
    if layout in {"directory", "preflight"}:
        write_directory_trace(root, ID_A, request=b"old")
    else:
        records = (pack_record("m"), request_record(ID_A, b"old"))
        if layout == "segment":
            write_pack(root, "m.pack", (pack_record("m"),))
            records = (pack_record("m", 1), request_record(ID_A, b"old"))
        write_pack(root, "m.s1.pack" if layout == "segment" else "m.pack", records)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        moved = run.with_name("moved")
        run.rename(moved)
        other = run.with_name("other")
        other.mkdir()
        write_directory_trace(other / "scorer-traces", ID_A, request=b"new")
        if replacement == "symlink":
            run.symlink_to(other, target_is_directory=True)
        else:
            other.rename(run)
        kwargs = (
            {"operation_id": "op"} if preflight else ({"measurement_id": "m"} if explicit else {})
        )
        record = reader.read(ID_A, max_section_bytes=limit, **kwargs)
        assert record is not None and record.request == b"old"


@pytest.mark.parametrize("component", ["scorer-traces", "scorer_traces", ID_A])
@pytest.mark.parametrize("limit", [None, 4])
def test_strict_rejects_descendant_symlink(run: Path, component: str, limit: int | None) -> None:
    directory = write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    original = next(path for path in (directory, *directory.parents) if path.name == component)
    target = original.with_name(original.name + "-real")
    original.rename(target)
    original.symlink_to(target, target_is_directory=True)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
            reader.read(ID_A, max_section_bytes=limit)


def test_strict_close_is_terminal_before_validation_or_filesystem_access(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = ScorerTraceReader(run, run_boundary="nofollow")
    reader.close()
    reader.close()

    def forbidden(*args, **kwargs):
        pytest.fail("closed reader touched the filesystem")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "fstat", forbidden)
    monkeypatch.setattr(os, "listdir", forbidden)
    with pytest.raises(ValueError, match="closed"):
        reader.read(ID_A, sections=())
    with pytest.raises(ValueError, match="closed"):
        reader.read("invalid/id", max_section_bytes=-1)
    with pytest.raises(ValueError, match="closed"):
        reader.__enter__()


def test_convenience_constructs_before_validating_read_arguments(run: Path) -> None:
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        read_scorer_trace(run / "missing", ID_A, run_boundary="nofollow", max_section_bytes=-1)
    with pytest.raises(ValueError):
        read_scorer_trace(run, ID_A, run_boundary="nofollow", max_section_bytes=-1)


@pytest.mark.parametrize("scope", ["run", "trace-root", "try"])
def test_owner_admission_is_retained(
    run: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    directory = write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    target = {"run": run, "trace-root": run / "scorer-traces", "try": directory}[scope]
    identity = target.stat().st_ino
    original = os.fstat

    def foreign(fd):
        info = original(fd)
        if info.st_ino == identity:
            return SimpleNamespace(st_uid=os.geteuid() + 1)
        return info

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        with ScorerTraceReader(run, run_boundary="nofollow") as reader:
            reader.read(ID_A)
    if scope == "run":
        # Default admission checks descendants, never the run root itself.
        assert read_scorer_trace(run, ID_A).request == b"{}"


@pytest.mark.parametrize("primitive", ["O_NOFOLLOW", "O_DIRECTORY", "dir_fd", "listdir_fd"])
def test_unsupported_primitives_fail_closed(
    run: Path, monkeypatch: pytest.MonkeyPatch, primitive: str
) -> None:
    if primitive in {"O_NOFOLLOW", "O_DIRECTORY"}:
        monkeypatch.delattr(os, primitive)
    elif primitive == "dir_fd":
        monkeypatch.setattr(os, "supports_dir_fd", set())
    else:
        monkeypatch.setattr(os, "supports_fd", set())
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(run, run_boundary="nofollow")


@pytest.mark.parametrize("failure", [errno.EIO, errno.EMFILE])
def test_constructor_io_failure_closes_partial_walk_and_hides_paths(
    run: Path, monkeypatch: pytest.MonkeyPatch, failure: int
) -> None:
    original = os.open
    opened = []

    def fail(path, flags, *args, **kwargs):
        if path == "run":
            raise OSError(failure, "private path " + str(run))
        fd = original(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    _patch_open(monkeypatch, fail)
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(run, run_boundary="nofollow")
    assert opened
    for fd in opened:
        with pytest.raises(OSError) as exc:
            os.fstat(fd)
        assert exc.value.errno == errno.EBADF


@pytest.mark.parametrize("cleanup", ["close", "context-error", "finalize"])
def test_root_descriptor_is_released(
    run: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    original = os.open
    root_fds = []

    def capture(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if path == "run":
            root_fds.append(fd)
        return fd

    _patch_open(monkeypatch, capture)
    reader = ScorerTraceReader(run, run_boundary="nofollow")
    assert len(root_fds) == 1
    assert os.fstat(root_fds[0]).st_ino == run.stat().st_ino
    if cleanup == "close":
        reader.close()
    elif cleanup == "context-error":
        with pytest.raises(RuntimeError), reader:
            raise RuntimeError("body failed")
    else:
        del reader
        gc.collect()
    with pytest.raises(OSError) as exc:
        os.fstat(root_fds[0])
    assert exc.value.errno == errno.EBADF


def test_close_waits_for_active_read(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    reader = ScorerTraceReader(run, run_boundary="nofollow")
    entered = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    close_finished = threading.Event()
    original = traces._probe_file

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def close():
        closing.set()
        reader.close()
        close_finished.set()

    monkeypatch.setattr(traces, "_probe_file", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(reader.read, ID_A)
        try:
            assert entered.wait(5)
            closed = pool.submit(close)
            assert closing.wait(5)
            assert not close_finished.wait(0.1)
        finally:
            release.set()
        assert reading.result(timeout=5).request == b"{}"
        closed.result(timeout=5)
    with pytest.raises(ValueError, match="closed"):
        reader.read(ID_A)


@pytest.mark.parametrize("explicit", [False, True])
def test_pack_cache_retains_request_only_record_and_misses_until_new_reader(
    run: Path, explicit: bool
) -> None:
    root = run / "scorer-traces"
    path = write_pack(root, "m.pack", (pack_record("m"), request_record(ID_A, b"{}")))
    kwargs = {"measurement_id": "m"} if explicit else {}
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        first = reader.read(ID_A, **kwargs)
        assert first is not None and first.stdout is None
        assert reader.read(ID_B, **kwargs) is None
        with path.open("ab") as stream:
            stream.write(output_record(ID_A, b"later"))
            stream.write(request_record(ID_B, b"new"))
        write_pack(root, "m.s1.pack", (pack_record("m", 1), request_record("new-id", b"new")))
        assert reader.read(ID_A, **kwargs).stdout is None
        assert reader.read(ID_B, **kwargs) is None
        assert reader.read("new-id", **kwargs) is None
    with ScorerTraceReader(run, run_boundary="nofollow") as fresh:
        assert fresh.read(ID_A, **kwargs).stdout == b"later"
        assert fresh.read(ID_B, **kwargs).request == b"new"
        assert fresh.read("new-id", **kwargs).request == b"new"


@pytest.mark.parametrize("primitive", ["dup", "fstat", "pread", "geteuid"])
def test_missing_descriptor_primitive_is_refused_during_construction(
    run: Path, monkeypatch: pytest.MonkeyPatch, primitive: str
) -> None:
    monkeypatch.delattr(os, primitive)
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader(run, run_boundary="nofollow")


def test_intermediate_owner_policy_is_unchanged(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    intermediate = directory.parent.stat().st_ino
    original = os.fstat

    def foreign(fd):
        info = original(fd)
        return SimpleNamespace(st_uid=os.getuid() + 1) if info.st_ino == intermediate else info

    monkeypatch.setattr(os, "fstat", foreign)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        assert reader.read(ID_A).request == b"{}"
    assert read_scorer_trace(run, ID_A).request == b"{}"


@pytest.mark.parametrize("operation", ["cwd", "open"])
def test_runtime_unsupported_or_cwd_failure_is_safe(
    run: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def fail(*args, **kwargs):
        if operation == "cwd":
            raise OSError(errno.EIO, "private cwd")
        raise NotImplementedError("private unsupported operation")

    if operation == "cwd":
        monkeypatch.setattr(os, "getcwd", fail)
    else:
        _patch_open(monkeypatch, fail)
    with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
        ScorerTraceReader("run", run_boundary="nofollow")


def test_strict_directory_lookup_is_uncached_and_uses_no_absolute_fallback(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("strict traversal used a resolved absolute path")

    monkeypatch.setattr(os.path, "realpath", forbidden)
    monkeypatch.setattr(traces, "_directory", forbidden)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        assert reader.read(ID_A) is None
        write_directory_trace(run / "scorer-traces", ID_A, request=b"new")
        assert reader.read(ID_A).request == b"new"


def test_strict_reads_serialize_after_payload_lookup(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    entered = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    original = traces._read_bounded
    active = 0
    overlap = False
    state_lock = threading.Lock()

    def pause(*args, **kwargs):
        nonlocal active, overlap
        with state_lock:
            active += 1
            overlap |= active > 1
            if active > 1:
                second_entered.set()
        entered.set()
        try:
            assert release.wait(5)
            return original(*args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(traces, "_read_bounded", pause)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:

        def second():
            second_started.set()
            return reader.read(ID_A, max_section_bytes=4)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reader.read, ID_A, max_section_bytes=4)
            try:
                assert entered.wait(5)
                later = pool.submit(second)
                assert second_started.wait(5)
                assert not second_entered.wait(0.1)
            finally:
                release.set()
            assert first.result(timeout=5).request == b"{}"
            assert later.result(timeout=5).request == b"{}"
    assert not overlap


def test_failed_bounded_read_closes_temporaries_but_keeps_root(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    original_open = os.open
    original_dup = os.dup
    descriptors = []
    root_fds = []

    def capture(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        descriptors.append(fd)
        if path == "run":
            root_fds.append(fd)
        return fd

    def duplicate(fd):
        child = original_dup(fd)
        descriptors.append(child)
        return child

    def fail(*args, **kwargs):
        raise MemoryError("payload allocation")

    _patch_open(monkeypatch, capture)
    monkeypatch.setattr(os, "dup", duplicate)
    monkeypatch.setattr(traces, "_read_bounded", fail)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        with pytest.raises(MemoryError):
            reader.read(ID_A, max_section_bytes=4)
        assert os.fstat(root_fds[0]).st_ino == run.stat().st_ino
        for fd in set(descriptors) - set(root_fds):
            with pytest.raises(OSError) as exc:
                os.fstat(fd)
            assert exc.value.errno == errno.EBADF


def test_new_pack_id_is_invisible_to_cached_all_id_enumeration(run: Path) -> None:
    root = run / "scorer-traces"
    write_pack(root, "m.pack", (pack_record("m"), request_record(ID_A, b"{}")))
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        assert reader.read(ID_A).request == b"{}"
        assert reader.read(ID_B) is None
        write_pack(root, "other.pack", (pack_record("other"), request_record(ID_B, b"new")))
        assert reader.read(ID_B) is None
    with ScorerTraceReader(run, run_boundary="nofollow") as fresh:
        assert fresh.read(ID_B).request == b"new"


@pytest.mark.parametrize("limit", [None, 2, 1])
@pytest.mark.parametrize("layout", ["directory", "pack"])
def test_strict_convenience_preserves_section_limit_and_unrequested_safety(
    run: Path, limit: int | None, layout: str
) -> None:
    root = run / "scorer-traces"
    if layout == "directory":
        directory = write_directory_trace(root, ID_A, request=b"{}")
        (directory / "stderr").symlink_to(directory / "request.json")
    else:
        write_pack(
            root,
            "m.pack",
            (pack_record("m"), request_record(ID_A, b"{}"), output_record(ID_A, b"", b"large")),
        )
    if limit == 1:
        with pytest.raises(ScorerTraceUnreadable, match="^too_large$"):
            read_scorer_trace(
                run, ID_A, sections=("request",), max_section_bytes=limit, run_boundary="nofollow"
            )
    else:
        record = read_scorer_trace(
            run, ID_A, sections=("request",), max_section_bytes=limit, run_boundary="nofollow"
        )
        assert record is not None and record.request == b"{}" and record.stderr is None


@pytest.mark.parametrize("boundary", ["resolved", "nofollow"])
@pytest.mark.parametrize("layout", ["directory", "pack"])
@pytest.mark.parametrize("limit", [None, 4])
def test_runtime_unsupported_directory_open_preserves_mode_contract_and_closes_descriptors(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    layout: str,
    limit: int | None,
) -> None:
    root = run / "scorer-traces"
    if layout == "directory":
        write_directory_trace(root, ID_A, request=b"{}")
        failing_component = "scorer_traces"
    else:
        write_pack(root, "m.pack", (pack_record("m"), request_record(ID_A, b"{}")))
        failing_component = "scorer-traces"
    original_open = os.open
    original_dup = os.dup
    descriptors = []
    root_fds = []
    constructing = True
    unsupported = NotImplementedError("private directory-open implementation")
    failure_count = 0

    def capture(path, flags, *args, **kwargs):
        nonlocal failure_count
        if path == failing_component and kwargs.get("dir_fd") is not None:
            failure_count += 1
            raise unsupported
        fd = original_open(path, flags, *args, **kwargs)
        descriptors.append(fd)
        if constructing and path == "run":
            root_fds.append(fd)
        return fd

    def duplicate(fd):
        child = original_dup(fd)
        descriptors.append(child)
        return child

    _patch_open(monkeypatch, capture)
    monkeypatch.setattr(os, "dup", duplicate)
    with ScorerTraceReader(run, run_boundary=boundary) as reader:
        constructing = False
        if boundary == "nofollow":
            with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
                reader.read(ID_A, measurement_id="m", max_section_bytes=limit)
            assert os.fstat(root_fds[0]).st_ino == run.stat().st_ino
        else:
            with pytest.raises(NotImplementedError) as exc:
                reader.read(ID_A, measurement_id="m", max_section_bytes=limit)
            assert exc.value is unsupported
        assert failure_count == 1
        for fd in set(descriptors) - set(root_fds):
            with pytest.raises(OSError) as closed:
                os.fstat(fd)
            assert closed.value.errno == errno.EBADF
        # A failed lookup must not cache a miss or invalidate the active root.
        monkeypatch.setattr(os, "open", original_open)
        record = reader.read(ID_A, measurement_id="m", max_section_bytes=limit)
        assert record is not None and record.request == b"{}" and record.layout == layout
    for fd in root_fds:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("boundary", ["resolved", "nofollow"])
@pytest.mark.parametrize("sections", [(), ("request",)])
@pytest.mark.parametrize("limit", [None, 4])
def test_runtime_unsupported_fd_listing_preserves_mode_contract_and_closes_descriptors(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    sections: tuple[str, ...],
    limit: int | None,
) -> None:
    write_pack(run / "scorer-traces", "m.pack", (pack_record("m"), request_record(ID_A, b"{}")))
    original_open = os.open
    original_listdir = os.listdir
    root_fds = []
    listing_fds = []

    def capture(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == "run":
            root_fds.append(fd)
        return fd

    def unsupported(path):
        if isinstance(path, int):
            listing_fds.append(path)
            raise NotImplementedError("private fd-listing implementation")
        return original_listdir(path)

    _patch_open(monkeypatch, capture)
    advertised = os.supports_fd - {original_listdir}
    monkeypatch.setattr(os, "listdir", unsupported)
    monkeypatch.setattr(os, "supports_fd", advertised | {unsupported})
    with ScorerTraceReader(run, run_boundary=boundary) as reader:
        if boundary == "nofollow":
            with pytest.raises(ScorerTraceUnreadable, match="^unsafe_entry$"):
                reader.read(ID_A, sections=sections, max_section_bytes=limit)
            assert os.fstat(root_fds[0]).st_ino == run.stat().st_ino
        else:
            with pytest.raises(NotImplementedError, match="private fd-listing implementation"):
                reader.read(ID_A, sections=sections, max_section_bytes=limit)
        assert listing_fds
        for fd in listing_fds:
            with pytest.raises(OSError) as exc:
                os.fstat(fd)
            assert exc.value.errno == errno.EBADF
        # Failed enumeration must not cache a miss or poison the active reader.
        monkeypatch.setattr(os, "listdir", original_listdir)
        record = reader.read(ID_A, sections=sections, max_section_bytes=limit)
        assert record is not None and record.sizes == {"request": 2}
    for fd in root_fds:
        with pytest.raises(OSError) as exc:
            os.fstat(fd)
        assert exc.value.errno == errno.EBADF
