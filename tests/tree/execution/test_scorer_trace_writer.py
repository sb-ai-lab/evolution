"""Production packed writer: framing, session ownership and durable recovery."""

from __future__ import annotations

import errno
import hashlib
import importlib
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from evolution.tree import read_scorer_trace
from evolution.tree.execution.interaction import ScoringError
from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault
from tests.scorer_trace_packs import (
    EXAMPLE_SHA256,
    ID_A,
    ID_B,
    STDERR_B,
    STDOUT_A,
    STDOUT_B,
    example_pack,
    pack_record,
    request_bytes,
    request_record,
    write_private,
)


def writer(root: Path, pack_id: str = "m"):
    module = importlib.import_module("evolution.tree.execution.scorer_trace_writer")
    return module.PackedTraceWriter(root, pack_id)


def test_collected_writer_can_release_while_another_writer_registers(tmp_path: Path) -> None:
    # A subprocess bounds a regression that would otherwise deadlock pytest itself.
    script = textwrap.dedent(
        """
        import faulthandler
        import gc
        import sys
        import weakref
        from pathlib import Path

        from evolution.tree.execution import scorer_trace_writer as module

        faulthandler.dump_traceback_later(5)
        gc.disable()
        root = Path(sys.argv[1])
        survivor = module.PackedTraceWriter(root, "pack")
        victim = module.PackedTraceWriter(root, "pack")
        victim.cycle = victim
        reference = weakref.ref(victim)
        del victim
        session_type = module._Session
        collected = []

        def collect_during_allocation():
            # This allocation runs while registration holds the registry lock.
            gc.collect()
            collected.append(reference() is None)
            return session_type()

        module._Session = collect_during_allocation
        replacement = module.PackedTraceWriter(root, "pack")
        module._Session = session_type
        assert collected == [True]
        assert replacement._session is survivor._session
        assert replacement._session.owners == 2
        replacement.append_request("request", b"{}")
        survivor.close()
        assert replacement._session.owners == 1
        replacement.close()
        assert replacement._session.owners == 0
        assert root / "pack.pack" not in module._sessions
        faulthandler.cancel_dump_traceback_later()
        print("collected and released")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "scorer-traces")],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_writer_matches_the_published_example(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    with writer(root, "m-r01-a02") as pack:
        assert not root.exists()
        pack.append_request(ID_A, request_bytes(ID_A, "c1"))
        pack.append_request(ID_B, request_bytes(ID_B, "c2"))
        for trace_id, out, err in [(ID_A, STDOUT_A, b""), (ID_B, STDOUT_B, STDERR_B)]:
            with pack.streams() as (stdout, stderr):
                stdout.write(out)
                stderr.write(err)
                record = pack.append_output(
                    trace_id, stdout, stderr, status="completed", returncode=0
                )
                with record.path.open("rb") as handle:
                    handle.seek(record.sections["stdout"].offset)
                    assert handle.read(record.sections["stdout"].length) == out
            assert stdout.closed and stderr.closed
    data = (root / "m-r01-a02.pack").read_bytes()
    assert data == example_pack()
    assert hashlib.sha256(data).hexdigest() == EXAMPLE_SHA256
    assert (root / "m-r01-a02.pack").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in root.iterdir()) == ["m-r01-a02.pack"]
    result = read_scorer_trace(tmp_path, ID_B, measurement_id="m-r01-a02")
    assert result is not None and result.stdout == STDOUT_B and result.stderr == STDERR_B


def test_request_only_then_empty_start_failed_output(tmp_path: Path) -> None:
    with writer(tmp_path / "scorer-traces") as pack:
        pack.append_request("try", b"{}\n")
        result = read_scorer_trace(tmp_path, "try", measurement_id="m")
        assert result is not None and result.request == b"{}\n" and result.stdout is None
        with pack.streams() as (stdout, stderr):
            assert os.fstat(stdout.fileno()).st_nlink == 0
            assert os.fstat(stdout.fileno()).st_mode & 0o777 == 0o600
            pack.append_output("try", stdout, stderr, status="start_failed", returncode=None)
        result = read_scorer_trace(tmp_path, "try", measurement_id="m")
        assert result is not None and result.stdout == result.stderr == b""
        assert result.status == "start_failed" and result.returncode is None


def test_independent_instances_serialize_and_share_ids(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    with writer(root) as first, writer(root) as second:

        def append(number: int) -> None:
            pack = first if number % 2 else second
            trace_id = f"t{number}"
            pack.append_request(trace_id, trace_id.encode())
            with pack.streams() as (stdout, stderr):
                stdout.write(trace_id.encode() * 100)
                pack.append_output(trace_id, stdout, stderr, status="completed", returncode=0)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(24)))
        with pytest.raises(ScoringError, match="already in use"):
            second.append_request("t0", b"replacement")
    for number in range(24):
        record = read_scorer_trace(tmp_path, f"t{number}", measurement_id="m")
        assert record is not None and record.stdout == f"t{number}".encode() * 100


@pytest.mark.parametrize(
    "tail",
    [b'{"kind":', request_record("torn", b"body")[:-1], request_record("torn", b"body")[:-3]],
)
def test_only_torn_tail_is_removed(tmp_path: Path, tail: bytes) -> None:
    prefix = pack_record("m") + request_record("old", b"old")
    path = write_private(tmp_path / "scorer-traces" / "m.pack", prefix + tail)
    with writer(path.parent) as pack:
        pack.append_request("new", b"new")
    assert path.read_bytes() == prefix + request_record("new", b"new")


@pytest.mark.parametrize(
    "tail",
    [
        b"x" * 4096,
        b"{}\n",
        request_record("bad", b"bad")[:-1] + b"!",
        request_record("bad", b"bad").replace(b'"request",3,', b'"request",9,'),
    ],
)
def test_corrupt_suffix_is_preserved_and_warns_safely(tmp_path: Path, capsys, tail: bytes) -> None:
    prefix = pack_record("m") + request_record("old", b"secret")
    path = write_private(tmp_path / "scorer-traces" / "m.pack", prefix + tail)
    with writer(path.parent) as pack:
        pack.append_request("new", b"new")
        with pytest.raises(ScoringError, match="already in use"):
            pack.append_request("old", b"overwrite")
    assert path.read_bytes() == prefix + tail
    assert (path.parent / "m.s1.pack").read_bytes() == pack_record("m", 1) + request_record(
        "new", b"new"
    )
    warning = capsys.readouterr().err
    assert "scorer-traces/m.pack" in warning and str(len(prefix)) in warning
    assert str(tmp_path) not in warning and "secret" not in warning
    assert len(warning.splitlines()) == 1


@pytest.mark.parametrize("problem", ["symlink", "hardlink", "mode", "directory", "fifo"])
def test_unsafe_pack_faults_without_rotation(tmp_path: Path, problem: str) -> None:
    root = tmp_path / "scorer-traces"
    root.mkdir(mode=0o700)
    path = root / "m.pack"
    if problem == "symlink":
        target = write_private(tmp_path / "target", pack_record("m"))
        path.symlink_to(target)
    elif problem == "directory":
        path.mkdir()
    elif problem == "fifo":
        os.mkfifo(path)
    else:
        write_private(path, pack_record("m"))
        if problem == "hardlink":
            os.link(path, tmp_path / "other")
        else:
            path.chmod(0o640)
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            pack.append_request("try", b"{}")
    assert caught.value.code == "journal_unavailable"
    assert not (root / "m.s1.pack").exists()


@pytest.mark.parametrize("number", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
def test_failed_partial_append_rolls_back_and_faults_every_owner(
    tmp_path: Path, monkeypatch, number: int
) -> None:
    module = importlib.import_module("evolution.tree.execution.scorer_trace_writer")
    root = tmp_path / "scorer-traces"
    with writer(root) as first, writer(root) as second:
        first.append_request("old", b"old")
        before = (root / "m.pack").read_bytes()
        original = module._write
        count = 0

        def fail(fd, data):
            nonlocal count
            count += 1
            if count == 1:
                return original(fd, data[:10])
            raise OSError(number, "secret path /sensitive")

        with monkeypatch.context() as patch:
            patch.setattr(module, "_write", fail)
            with pytest.raises(ScorerTraceStorageFault) as caught:
                first.append_request("new", b"new")
        assert (root / "m.pack").read_bytes() == before
        first.close()
        with pytest.raises(ScorerTraceStorageFault) as again:
            second.append_request("later", b"later")
        assert again.value is caught.value
        assert "sensitive" not in caught.value.detail
        assert caught.value.code == (
            "storage_capacity_exhausted" if number != errno.EIO else "journal_unavailable"
        )
    with writer(root) as recovered:
        recovered.append_request("new", b"new")
    assert read_scorer_trace(tmp_path, "new", measurement_id="m") is not None


def test_large_stream_is_copied_and_hashed_in_bounded_chunks(tmp_path: Path, monkeypatch) -> None:
    import tracemalloc

    import evolution.tree.execution.scorer_trace_writer as module

    with writer(tmp_path / "scorer-traces") as pack:
        pack.append_request("large", b"{}")
        with pack.streams() as (stdout, stderr):
            chunk = bytes(range(256)) * 4096
            for _ in range(24):
                stdout.write(chunk)
            original = os.pread
            reads = []

            def bounded(fd, length, offset):
                reads.append(length)
                assert length <= 1024 * 1024
                return original(fd, length, offset)

            with monkeypatch.context() as patch:
                patch.setattr(module.os, "pread", bounded)
                tracemalloc.start()
                try:
                    record = pack.append_output(
                        "large", stdout, stderr, status="completed", returncode=0
                    )
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            assert max(reads) == 1024 * 1024 and peak < 6 * 1024 * 1024
            assert record.sections["stdout"].length == 24 * 1024 * 1024
    result = read_scorer_trace(tmp_path, "large", measurement_id="m")
    assert result is not None and result.stdout == chunk * 24


def test_adoption_scans_once_and_only_repairs_highest_segment(tmp_path: Path, monkeypatch) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    lower = write_private(root / "m.pack", pack_record("m") + request_record("old", b"x") + b"torn")
    write_private(root / "m.s1.pack", pack_record("m", 1))
    higher = write_private(
        root / "m.s2.pack", pack_record("m", 2) + request_record("newer", b"y") + b"torn"
    )
    before = lower.read_bytes()
    with writer(root) as first, writer(root) as second:
        first.append_request("a", b"a")
        with monkeypatch.context() as patch:
            patch.setattr(
                module, "_scan", lambda *args: pytest.fail("scanned twice in one session")
            )
            second.append_request("b", b"b")
        for trace_id in ("old", "newer"):
            with pytest.raises(ScoringError):
                second.append_request(trace_id, b"overwrite")
    assert lower.read_bytes() == before
    assert higher.read_bytes() == pack_record("m", 2) + request_record(
        "newer", b"y"
    ) + request_record("a", b"a") + request_record("b", b"b")


@pytest.mark.parametrize("data", [b"", b'{"format":', pack_record("m")[:-1]])
def test_incomplete_first_header_is_rebuilt(tmp_path: Path, data: bytes) -> None:
    path = write_private(tmp_path / "scorer-traces" / "m.pack", data)
    with writer(path.parent) as pack:
        pack.append_request("new", b"new")
    assert path.read_bytes() == pack_record("m") + request_record("new", b"new")


@pytest.mark.parametrize(
    "data", [pack_record("other"), request_record("bad-first", b"payload"), pack_record("m", 1)]
)
def test_bad_pack_identity_is_preserved_and_rotated(tmp_path: Path, data: bytes) -> None:
    path = write_private(tmp_path / "scorer-traces" / "m.pack", data)
    with writer(path.parent) as pack:
        pack.append_request("new", b"new")
    assert path.read_bytes() == data
    assert read_scorer_trace(tmp_path, "new", measurement_id="m") is not None


def test_valid_other_filename_interpretation_faults_without_hiding_new_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "scorer-traces"
    path = write_private(root / "m.s1.pack", pack_record("m.s1"))
    before = path.read_bytes()
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault):
            pack.append_request("new", b"new")
    assert path.read_bytes() == before
    assert not (root / "m.s2.pack").exists()


def test_adoption_does_not_hash_section_payloads(tmp_path: Path) -> None:
    prefix = pack_record("m") + request_record("old", b"secret")
    path = write_private(
        tmp_path / "scorer-traces" / "m.pack", prefix.replace(b"secret\n", b"damage\n")
    )
    with writer(path.parent) as pack:
        pack.append_request("new", b"new")
    assert path.read_bytes().endswith(request_record("new", b"new"))
    assert not (path.parent / "m.s1.pack").exists()


@pytest.mark.parametrize("failure", ["zero", "fsync", "rollback"])
def test_failed_append_preserves_first_fault_even_when_rollback_fails(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("old", b"old")
        before = (root / "m.pack").read_bytes()
        with monkeypatch.context() as patch:
            if failure == "zero":
                patch.setattr(module, "_write", lambda *_args: 0)
            elif failure == "fsync":

                def sync_failed(_fd):
                    raise OSError(errno.ENOSPC, "full")

                patch.setattr(module, "_fsync", sync_failed)
            else:
                real_write = module._write

                def write_failed(fd, data):
                    real_write(fd, data[:5])
                    raise OSError(errno.EDQUOT, "quota")

                def rollback_failed(*_args):
                    raise OSError(errno.EIO, "rollback")

                patch.setattr(module, "_write", write_failed)
                patch.setattr(module, "_truncate", rollback_failed)
            with pytest.raises(ScorerTraceStorageFault) as caught:
                pack.append_request("new", b"new")
        assert caught.value.code == (
            "journal_unavailable" if failure == "zero" else "storage_capacity_exhausted"
        )
        with pytest.raises(ScorerTraceStorageFault) as again:
            pack.append_request("new", b"new")
        assert again.value is caught.value
        if failure != "rollback":
            assert (root / "m.pack").read_bytes() == before
    with writer(root) as resumed:
        resumed.append_request("new", b"new")
    assert (root / "m.pack").read_bytes() == before + request_record("new", b"new")


def test_short_pack_and_buffer_writes_complete(tmp_path: Path, monkeypatch) -> None:
    import evolution.tree.execution.scorer_trace_writer as module
    import evolution.tree.execution.scorer_traces as shared

    write = module._write
    raw_write = shared._raw_write
    monkeypatch.setattr(module, "_write", lambda fd, data: write(fd, data[:7]))
    monkeypatch.setattr(shared, "_raw_write", lambda stream, data: raw_write(stream, data[:3]))
    with writer(tmp_path / "scorer-traces") as pack:
        pack.append_request("try", b"{}")
        with pack.streams() as (stdout, stderr):
            stdout.write(b"long stdout" * 10)
            pack.append_output("try", stdout, stderr, status="failed", returncode=3)
    result = read_scorer_trace(tmp_path, "try", measurement_id="m")
    assert result is not None and result.stdout == b"long stdout" * 10 and result.returncode == 3


def test_external_oserror_in_stream_context_is_not_a_storage_failure(tmp_path: Path) -> None:
    with writer(tmp_path / "scorer-traces") as pack:
        original = OSError(errno.ENOENT, "scorer missing")
        with pytest.raises(OSError) as caught:
            with pack.streams() as (stdout, stderr):
                raise original
        assert caught.value is original
        assert stdout.closed and stderr.closed
        pack.append_request("still-usable", b"{}")


def test_buffer_storage_failure_faults_session_and_closes_both_streams(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution.tree.execution.scorer_traces as shared

    root = tmp_path / "scorer-traces"
    with writer(root) as pack, writer(root) as other:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            with pack.streams() as (stdout, stderr):
                with monkeypatch.context() as patch:
                    patch.setattr(shared, "_raw_write", lambda *_args: 0)
                    stdout.write(b"output")
        assert stdout.closed and stderr.closed
        with pytest.raises(ScorerTraceStorageFault) as again:
            other.append_request("try", b"{}")
        assert again.value is caught.value


def test_stream_creation_failure_closes_first_buffer(tmp_path: Path, monkeypatch) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    original = module.tempfile.TemporaryFile
    created = []

    def temporary(*args, **kwargs):
        if created:
            raise OSError(errno.ENOSPC, "full")
        result = original(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(module.tempfile, "TemporaryFile", temporary)
    with writer(tmp_path / "scorer-traces") as pack:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            with pack.streams():
                pytest.fail("should not reach scoring")
        assert caught.value.code == "storage_capacity_exhausted"
    assert created[0].closed
    assert list((tmp_path / "scorer-traces").iterdir()) == []


def test_root_is_readmitted_before_each_request(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("old", b"old")
        before = (root / "m.pack").read_bytes()
        root.chmod(0o755)
        with pytest.raises(ScorerTraceStorageFault):
            pack.append_request("new", b"new")
        assert (root / "m.pack").read_bytes() == before


def test_replaced_pack_faults_instead_of_appending_to_unindexed_file(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("old", b"old")
        path = root / "m.pack"
        path.rename(root / "saved")
        replacement = write_private(path, pack_record("m"))
        with pytest.raises(ScorerTraceStorageFault):
            pack.append_request("new", b"new")
        assert replacement.read_bytes() == pack_record("m")


def test_gap_in_segments_faults_before_writing_unreachable_records(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    paths = [
        write_private(root / "m.pack", pack_record("m")),
        write_private(root / "m.s2.pack", pack_record("m", 2)),
    ]
    before = [path.read_bytes() for path in paths]
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault):
            pack.append_request("new", b"new")
    assert [path.read_bytes() for path in paths] == before


def test_unsafe_later_segment_fault_names_that_segment(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    write_private(root / "m.pack", pack_record("m"))
    path = write_private(root / "m.s1.pack", pack_record("m", 1))
    path.chmod(0o644)
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            pack.append_request("new", b"new")
    assert "scorer-traces/m.s1.pack" in caught.value.detail


def test_pack_close_failure_preserves_original_write_fault(tmp_path: Path, monkeypatch) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("old", b"old")
        real_close = os.close

        def close_failed(fd):
            info = os.fstat(fd)
            real_close(fd)
            if info.st_ino == (root / "m.pack").stat().st_ino:
                raise OSError(errno.EIO, "close")

        def write_failed(*_args):
            raise OSError(errno.ENOSPC, "full")

        with monkeypatch.context() as patch:
            patch.setattr(module.os, "close", close_failed)
            patch.setattr(module, "_write", write_failed)
            with pytest.raises(ScorerTraceStorageFault) as caught:
                pack.append_request("new", b"new")
        assert caught.value.code == "storage_capacity_exhausted"
        assert "ENOSPC" in caught.value.detail


def test_owner_mismatch_is_a_fault(tmp_path: Path, monkeypatch) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    path = write_private(root / "m.pack", pack_record("m"))
    inode = path.stat().st_ino
    real_stat = os.fstat

    def foreign_owner(fd):
        info = real_stat(fd)
        if info.st_ino != inode:
            return info
        values = list(info)
        values[4] += 1
        return os.stat_result(values)

    monkeypatch.setattr(module.os, "fstat", foreign_owner)
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault):
            pack.append_request("new", b"new")
    assert path.read_bytes() == pack_record("m")


def test_buffer_close_failure_closes_both_and_faults_the_session(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution.tree.execution.scorer_traces as shared

    original = shared._raw_close
    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            with pack.streams() as (stdout, stderr):

                def close_failed(stream):
                    original(stream)
                    raise OSError(errno.EDQUOT, "close")

                monkeypatch.setattr(shared, "_raw_close", close_failed)
        assert stdout.closed and stderr.closed
        with pytest.raises(ScorerTraceStorageFault) as again:
            pack.append_request("try", b"{}")
        assert again.value is caught.value


def test_no_append_lock_is_held_while_the_scorer_writes_streams(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    with writer(root) as first, writer(root) as second:
        first.append_request("a", b"a")
        with first.streams() as (stdout, _stderr):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(second.append_request, "b", b"b")
                assert future.result(timeout=2).path == root / "m.pack"
            stdout.write(b"a")


def test_duplicate_output_does_not_overwrite_evidence(tmp_path: Path) -> None:
    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("try", b"{}")
        with pack.streams() as (stdout, stderr):
            pack.append_output("try", stdout, stderr, status="completed", returncode=0)
            before = (root / "m.pack").read_bytes()
            with pytest.raises(ScoringError):
                pack.append_output("try", stdout, stderr, status="failed", returncode=1)
            assert (root / "m.pack").read_bytes() == before


def test_finalization_releases_a_faulted_session(tmp_path: Path, monkeypatch) -> None:
    import gc

    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    pack = writer(root)
    pack.append_request("old", b"old")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_write", lambda *_args: 0)
        try:
            pack.append_request("new", b"new")
        except ScorerTraceStorageFault:
            pass
    del pack
    gc.collect()
    with writer(root) as resumed:
        resumed.append_request("new", b"new")


def test_directory_close_failure_is_typed_and_faults_session(tmp_path: Path, monkeypatch) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("old", b"old")
        real_close = os.close
        directory_inode = root.stat().st_ino

        def close_failed(fd):
            info = os.fstat(fd)
            real_close(fd)
            if info.st_ino == directory_inode:
                raise OSError(errno.EDQUOT, "directory close")

        with monkeypatch.context() as patch:
            patch.setattr(module.os, "close", close_failed)
            with pytest.raises(ScorerTraceStorageFault) as caught:
                pack.append_request("new", b"new")
        assert caught.value.code == "storage_capacity_exhausted"
        with pytest.raises(ScorerTraceStorageFault) as again:
            pack.append_request("later", b"later")
        assert again.value is caught.value


def test_temporary_wrapper_close_failure_cleans_duplicated_stream(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    original_temporary = module.tempfile.TemporaryFile
    original_stream = module.TraceStream
    streams = []

    class FailedClose:
        def __enter__(self):
            self.temporary = original_temporary(dir=tmp_path / "scorer-traces", buffering=0)
            return self.temporary

        def __exit__(self, *_args):
            self.temporary.close()
            raise OSError(errno.ENOSPC, "close")

    def stream(*args, **kwargs):
        opened = original_stream(*args, **kwargs)
        streams.append(opened)
        return opened

    monkeypatch.setattr(module.tempfile, "TemporaryFile", lambda **_kwargs: FailedClose())
    monkeypatch.setattr(module, "TraceStream", stream)
    with writer(tmp_path / "scorer-traces") as pack:
        with pytest.raises(ScorerTraceStorageFault):
            with pack.streams():
                pytest.fail("should not reach scorer")
    assert streams and all(stream.closed for stream in streams)


@pytest.mark.parametrize("failed_sync", [1, 2, 3])
def test_first_pack_failure_is_typed_and_recoverable(
    tmp_path: Path, monkeypatch, failed_sync: int
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    original = module._fsync
    count = 0

    def fail(fd):
        nonlocal count
        if not stat.S_ISDIR(os.fstat(fd).st_mode) or os.fstat(fd).st_ino == root.stat().st_ino:
            count += 1
        if count == failed_sync:
            raise OSError(errno.ENOSPC, "full")
        original(fd)

    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        with monkeypatch.context() as patch:
            patch.setattr(module, "_fsync", fail)
            with pytest.raises(ScorerTraceStorageFault):
                pack.append_request("try", b"{}")
        result = read_scorer_trace(tmp_path, "try", measurement_id="m")
        assert result is None
    synced = []
    directory_inode = root.stat().st_ino

    def observe_recovery_sync(fd):
        if os.fstat(fd).st_ino == directory_inode:
            # The name is durable before a new request can be published.
            assert (root / "m.pack").read_bytes() == pack_record("m")
            synced.append("directory")
        elif stat.S_ISDIR(os.fstat(fd).st_mode):
            pass  # Ancestor syncs are covered separately from pack adoption.
        elif os.pread(fd, os.fstat(fd).st_size, 0) == pack_record("m"):
            synced.append("header")
        else:
            synced.append("request")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_fsync", observe_recovery_sync)
        with writer(root) as pack:
            pack.append_request("try", b"{}")
    expected = ["header", "directory", "request"] if failed_sync == 1 else ["directory", "request"]
    assert synced == expected
    assert (root / "m.pack").read_bytes() == pack_record("m") + request_record("try", b"{}")


def test_stream_constructor_failure_closes_the_duplicate_descriptor(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    duplicated = []

    def failed_stream(fd, *_args):
        duplicated.append(fd)
        raise OSError(errno.EMFILE, "descriptor limit")

    monkeypatch.setattr(module, "TraceStream", failed_stream)
    with writer(tmp_path / "scorer-traces") as pack:
        with pytest.raises(ScorerTraceStorageFault):
            with pack.streams():
                pytest.fail("should not reach scoring")
    for fd in duplicated:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "data", [pack_record("m"), pack_record("m")[:-1]], ids=["complete", "torn"]
)
def test_adoption_directory_sync_failure_prevents_new_request(
    tmp_path: Path, monkeypatch, data: bytes
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "scorer-traces"
    path = write_private(root / "m.pack", data)
    original = module._fsync
    directory_inode = root.stat().st_ino

    def fail_directory_sync(fd):
        if os.fstat(fd).st_ino == directory_inode:
            raise OSError(errno.ENOSPC, "directory sync")
        original(fd)

    with writer(root) as pack:
        with monkeypatch.context() as patch:
            patch.setattr(module, "_fsync", fail_directory_sync)
            with pytest.raises(ScorerTraceStorageFault) as caught:
                pack.append_request("try", b"{}")
        assert caught.value.code == "storage_capacity_exhausted"
        assert path.read_bytes() == pack_record("m")
        with pytest.raises(ScorerTraceStorageFault) as again:
            pack.append_request("later", b"{}")
        assert again.value is caught.value
    with writer(root) as recovered:
        recovered.append_request("try", b"{}")
    assert path.read_bytes() == pack_record("m") + request_record("try", b"{}")


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_root_parent_entries_are_synced_before_request_and_once_per_session(
    tmp_path: Path, monkeypatch, nested: bool, existing: bool
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "outer" / "run" / "scorer-traces" if nested else tmp_path / "scorer-traces"
    if existing:
        root.mkdir(parents=True, mode=0o700)
    synced = []
    original_sync = module._fsync
    original_write = module._write

    def sync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        original_sync(fd)

    def write(fd, data):
        if b'"kind":"request"' in bytes(data):
            for parent in root.parents:
                info = parent.stat()
                assert (info.st_dev, info.st_ino) in synced
        return original_write(fd, data)

    monkeypatch.setattr(module, "_fsync", sync)
    monkeypatch.setattr(module, "_write", write)
    with writer(root) as first, writer(root) as second:
        first.append_request("first", b"first")
        initial = list(synced)
        second.append_request("second", b"second")
        with second.streams() as (stdout, stderr):
            second.append_output("second", stdout, stderr, status="completed", returncode=0)
        assert synced == initial
    assert (
        (root / "m.pack")
        .read_bytes()
        .startswith(pack_record("m") + request_record("first", b"first"))
    )


@pytest.mark.parametrize("failed_parent", [".", "outer", "outer/run"])
def test_failed_root_parent_sync_is_sticky_and_retried_with_retained_directories(
    tmp_path: Path, monkeypatch, failed_parent: str
) -> None:
    import evolution.tree.execution.scorer_trace_writer as module

    root = tmp_path / "outer" / "run" / "scorer-traces"
    target = tmp_path / failed_parent
    original_sync = module._fsync
    original_open = os.open
    original_close = os.close
    opened = set()
    synced = []
    failed_fd = None

    def opened_fd(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def closed_fd(fd):
        original_close(fd)
        opened.remove(fd)
        if fd == failed_fd:
            raise OSError(errno.EIO, "secondary close error")

    def fail_parent(fd):
        nonlocal failed_fd
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (target.stat().st_dev, target.stat().st_ino):
            failed_fd = fd
            raise OSError(errno.ENOSPC, "parent sync failed /sensitive")
        original_sync(fd)

    with writer(root) as first, writer(root) as second:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "open", opened_fd)
            patch.setattr(module.os, "close", closed_fd)
            patch.setattr(module, "_fsync", fail_parent)
            with pytest.raises(ScorerTraceStorageFault) as caught:
                first.append_request("first", b"first")
        assert not opened
        assert root.is_dir()
        assert caught.value.code == "storage_capacity_exhausted"
        assert "ENOSPC" in caught.value.detail and "sensitive" not in caught.value.detail
        with pytest.raises(ScorerTraceStorageFault) as again:
            second.append_request("second", b"second")
        assert again.value is caught.value

    def sync(fd):
        info = os.fstat(fd)
        synced.append((info.st_dev, info.st_ino))
        original_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_fsync", sync)
        with writer(root) as recovered:
            recovered.append_request("recovered", b"recovered")
    for parent in (tmp_path, tmp_path / "outer", tmp_path / "outer" / "run"):
        info = parent.stat()
        assert (info.st_dev, info.st_ino) in synced
    assert (root / "m.pack").read_bytes() == pack_record("m") + request_record(
        "recovered", b"recovered"
    )


@pytest.mark.parametrize("link_root", [False, True])
def test_root_parent_sync_never_follows_symlinked_components(
    tmp_path: Path, link_root: bool
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    root = link if link_root else link / "scorer-traces"
    with writer(root) as pack:
        with pytest.raises(ScorerTraceStorageFault):
            pack.append_request("try", b"{}")
    assert list(target.iterdir()) == []


def _spy_temporary_file(monkeypatch, *, force_dir=None, error=None):
    import evolution.tree.execution.scorer_trace_writer as module

    original = module.tempfile.TemporaryFile
    seen: list[object] = []

    def temporary(*args, **kwargs):
        seen.append(kwargs.get("dir"))
        if error is not None:
            raise error
        if force_dir is not None:
            kwargs["dir"] = force_dir
        return original(*args, **kwargs)

    monkeypatch.setattr(module.tempfile, "TemporaryFile", temporary)
    return seen


def test_stream_buffers_use_default_temporary_directory(tmp_path: Path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    seen = _spy_temporary_file(monkeypatch)
    root = tmp_path / "scorer-traces"
    with writer(root) as pack:
        pack.append_request("try", b"{}")
        with pack.streams() as (stdout, stderr):
            assert seen == [None, None]
            for stream in (stdout, stderr):
                assert os.fstat(stream.fileno()).st_mode & 0o777 == 0o600
            assert sorted(os.listdir(root)) == ["m.pack"]


def test_pack_bytes_do_not_depend_on_buffer_location(tmp_path: Path, monkeypatch) -> None:
    packs = []
    for label in ("b17", "default"):
        root = tmp_path / label / "scorer-traces"
        with monkeypatch.context() as patch:
            _spy_temporary_file(patch, force_dir=root if label == "b17" else None)
            with writer(root) as pack:
                pack.append_request(ID_A, request_bytes(ID_A, "c1"))
                with pack.streams() as (stdout, stderr):
                    stdout.write(STDOUT_A)
                    stderr.write(STDERR_B)
                    pack.append_output(ID_A, stdout, stderr, status="completed", returncode=0)
        packs.append((root / "m.pack").read_bytes())
    assert packs[0] == packs[1]


@pytest.mark.parametrize(
    ("number", "code", "remedy"),
    [
        (errno.ENOSPC, "storage_capacity_exhausted", "free space or inodes in the temporary"),
        (errno.EIO, "journal_unavailable", "check the temporary directory"),
    ],
)
def test_buffer_fault_names_temporary_directory(
    tmp_path: Path, monkeypatch, number: int, code: str, remedy: str
) -> None:
    _spy_temporary_file(monkeypatch, error=OSError(number, "x"))
    with writer(tmp_path / "scorer-traces") as pack:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            with pack.streams():
                pytest.fail("should not reach scoring")
    assert caught.value.code == code
    assert "(buffer)" in caught.value.detail
    assert remedy in caught.value.detail
    assert "temporary directory (TMPDIR, else /tmp)" in caught.value.detail


def test_non_buffer_fault_text_unchanged(tmp_path: Path, monkeypatch) -> None:
    module = importlib.import_module("evolution.tree.execution.scorer_trace_writer")

    def fail(fd, data):
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(module, "_write", fail)
    with writer(tmp_path / "scorer-traces") as pack:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            pack.append_request("try", b"{}")
    assert "free space or inodes on the run directory's filesystem" in caught.value.detail
    assert "temporary directory" not in caught.value.detail


@pytest.mark.parametrize(
    ("number", "code", "remedy"),
    [
        (
            errno.ENOSPC,
            "storage_capacity_exhausted",
            "free space or inodes in the temporary directory (TMPDIR, else /tmp)",
        ),
        (errno.EIO, "journal_unavailable", "check the temporary directory (TMPDIR, else /tmp)"),
    ],
)
def test_buffer_write_fault_names_temporary_directory(
    tmp_path: Path, monkeypatch, number: int, code: str, remedy: str
) -> None:
    import evolution.tree.execution.scorer_traces as shared

    def fail(*_args):
        raise OSError(number, "x")

    with writer(tmp_path / "scorer-traces") as pack:
        pack.append_request("try", b"{}")
        with pytest.raises(ScorerTraceStorageFault) as caught:
            with pack.streams() as (stdout, _stderr):
                monkeypatch.setattr(shared, "_raw_write", fail)
                stdout.write(b"output")
    assert caught.value.code == code
    assert "(write)" in caught.value.detail
    assert remedy in caught.value.detail
    assert "run directory" not in caught.value.detail


def test_directory_layout_stream_write_fault_names_run_directory(
    tmp_path: Path, monkeypatch
) -> None:
    import evolution.tree.execution.scorer_traces as shared

    def fail(*_args):
        raise OSError(errno.ENOSPC, "full")

    stream = shared.open_trace_stream(tmp_path / "stdout", "scorer-traces/try/stdout")
    monkeypatch.setattr(shared, "_raw_write", fail)
    try:
        with pytest.raises(ScorerTraceStorageFault) as caught:
            stream.write(b"output")
    finally:
        monkeypatch.undo()
        stream.close()
    assert caught.value.code == "storage_capacity_exhausted"
    assert caught.value.detail == (
        "scorer trace scorer-traces/try/stdout: ENOSPC (write); "
        "free space or inodes on the run directory's filesystem and run the same command again"
    )


@pytest.mark.parametrize("failure", ["eio", "ended_early"])
def test_buffer_read_fault_during_append_names_temporary_directory(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    module = importlib.import_module("evolution.tree.execution.scorer_trace_writer")
    root = tmp_path / "scorer-traces"
    original = os.pread
    calls = 0

    def flaky(fd, length, offset):
        nonlocal calls
        calls += 1
        if calls < 3:  # hashing both buffers succeeds; the copy into the pack fails
            return original(fd, length, offset)
        if failure == "eio":
            raise OSError(errno.EIO, "secret path /sensitive")
        return b""

    with writer(root) as pack:
        pack.append_request("try", b"{}")
        before = (root / "m.pack").read_bytes()
        with pack.streams() as (stdout, stderr):
            stdout.write(b"output")
            stderr.write(b"errors")
            with monkeypatch.context() as patch:
                patch.setattr(module.os, "pread", flaky)
                with pytest.raises(ScorerTraceStorageFault) as caught:
                    pack.append_output("try", stdout, stderr, status="completed", returncode=0)
        assert (root / "m.pack").read_bytes() == before
        assert caught.value.code == "journal_unavailable"
        assert "(buffer)" in caught.value.detail
        assert "check the temporary directory (TMPDIR, else /tmp)" in caught.value.detail
        assert "run directory" not in caught.value.detail
        assert "sensitive" not in caught.value.detail
        with pytest.raises(ScorerTraceStorageFault) as again:
            pack.append_request("later", b"{}")
        assert again.value is caught.value


@pytest.mark.parametrize("failing", ["fstat", "pread"])
def test_buffer_describe_fault_names_temporary_directory(
    tmp_path: Path, monkeypatch, failing: str
) -> None:
    module = importlib.import_module("evolution.tree.execution.scorer_trace_writer")
    root = tmp_path / "scorer-traces"

    def fail(*_args):
        raise OSError(errno.EIO, "secret path /sensitive")

    with writer(root) as pack:
        pack.append_request("try", b"{}")
        before = (root / "m.pack").read_bytes()
        with pack.streams() as (stdout, stderr):
            stdout.write(b"output")
            with monkeypatch.context() as patch:
                if failing == "fstat":
                    original = os.fstat
                    scratch = stdout.fileno()

                    def fstat(fd):
                        if fd == scratch:
                            raise OSError(errno.EIO, "secret path /sensitive")
                        return original(fd)

                    patch.setattr(module.os, "fstat", fstat)
                else:
                    patch.setattr(module.os, "pread", fail)
                with pytest.raises(ScorerTraceStorageFault) as caught:
                    pack.append_output("try", stdout, stderr, status="completed", returncode=0)
        assert (root / "m.pack").read_bytes() == before
        assert caught.value.code == "journal_unavailable"
        assert "(buffer)" in caught.value.detail
        assert "check the temporary directory (TMPDIR, else /tmp)" in caught.value.detail
        assert "run directory" not in caught.value.detail
        assert "sensitive" not in caught.value.detail
        with pytest.raises(ScorerTraceStorageFault) as again:
            pack.append_request("later", b"{}")
        assert again.value is caught.value


@pytest.mark.parametrize(
    ("number", "remedy"),
    [
        (errno.ENOSPC, "free space or inodes on the run directory's filesystem"),
        (errno.EIO, "check the filesystem"),
    ],
)
def test_pack_write_fault_during_buffered_append_stays_append(
    tmp_path: Path, monkeypatch, number: int, remedy: str
) -> None:
    module = importlib.import_module("evolution.tree.execution.scorer_trace_writer")
    root = tmp_path / "scorer-traces"
    original = module._write
    count = 0

    def flaky(fd, data):
        nonlocal count
        count += 1
        if count == 1:  # the header is written; the first section copy then fails
            return original(fd, data)
        raise OSError(number, "secret path /sensitive")

    with writer(root) as pack:
        pack.append_request("try", b"{}")
        before = (root / "m.pack").read_bytes()
        with pack.streams() as (stdout, stderr):
            stdout.write(b"output")
            stderr.write(b"errors")
            with monkeypatch.context() as patch:
                patch.setattr(module, "_write", flaky)
                with pytest.raises(ScorerTraceStorageFault) as caught:
                    pack.append_output("try", stdout, stderr, status="completed", returncode=0)
        assert (root / "m.pack").read_bytes() == before
    assert "(append)" in caught.value.detail
    assert remedy in caught.value.detail
    assert "temporary directory" not in caught.value.detail
    assert "sensitive" not in caught.value.detail
