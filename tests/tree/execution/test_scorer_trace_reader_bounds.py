"""Opt-in per-section limits preserve layout semantics and pin admitted payloads."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from evolution.tree import ScorerTraceReader, ScorerTraceUnreadable, read_scorer_trace
from evolution.tree.execution import scorer_traces as traces
from evolution.tree.execution.scorer_traces import PACK_FORMAT, encode_pack_record
from tests.scorer_trace_packs import (
    ID_A,
    output_record,
    pack_record,
    request_record,
    write_directory_trace,
    write_pack,
    write_private,
)


@pytest.fixture(params=["directory", "pack"])
def layout(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=["reader", "function"])
def read(request: pytest.FixtureRequest) -> Callable:
    if request.param == "function":
        return read_scorer_trace

    def read_with_reader(run: Path, trace_id: str, **kwargs):  # type: ignore[no-untyped-def]
        with ScorerTraceReader(run) as reader:
            return reader.read(trace_id, **kwargs)

    return read_with_reader


def _trace(
    run: Path,
    layout: str,
    *,
    request: bytes | None = b"{}",
    stdout: bytes | None = b"ok",
    stderr: bytes | None = b"",
) -> Path:
    run.mkdir(mode=0o700)
    if layout == "directory":
        return write_directory_trace(
            run / "scorer-traces", ID_A, request=request, stdout=stdout, stderr=stderr
        )
    records = [pack_record("m")]
    if request is not None:
        records.append(request_record(ID_A, request))
    if stdout is not None or stderr is not None:
        records.append(output_record(ID_A, stdout or b"", stderr or b""))
    return write_pack(run / "scorer-traces", "m.pack", records)


def _refuses(code: str, call: Callable[[], object]) -> None:
    with pytest.raises(ScorerTraceUnreadable) as error:
        call()
    assert error.value.code == code
    assert str(error.value) == code  # No payload or private path in diagnostics.


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_limit_is_inclusive_per_section(
    tmp_path: Path, read: Callable, layout: str, limit: int
) -> None:
    run = tmp_path / "run"
    _trace(run, layout, request=b"{}", stdout=b"ok")
    if limit < 2:
        _refuses("too_large", lambda: read(run, ID_A, max_section_bytes=limit))
    else:
        record = read(run, ID_A, max_section_bytes=limit)
        assert record is not None
        assert (record.request, record.stdout, record.stderr) == (b"{}", b"ok", b"")
        assert dict(record.sizes) == {"request": 2, "stdout": 2, "stderr": 0}


def test_zero_allows_empty_selected_sections(tmp_path: Path, read: Callable, layout: str) -> None:
    run = tmp_path / "run"
    _trace(run, layout, request=b"", stdout=b"", stderr=b"")
    record = read(run, ID_A, max_section_bytes=0)
    assert record is not None
    assert (record.request, record.stdout, record.stderr) == (b"", b"", b"")


@pytest.mark.parametrize("limit", [True, False, -1, 1.0, "1", b"1", [], {}])
def test_invalid_limits_raise_value_error_before_identity_or_lookup(
    tmp_path: Path, read: Callable, limit: object
) -> None:
    with pytest.raises(ValueError):
        read(tmp_path / "absent", "../invalid", max_section_bytes=limit)


def test_function_constructs_reader_before_validating_read_arguments() -> None:
    class BrokenPath:
        def __fspath__(self) -> str:
            raise RuntimeError("constructor path error")

    with pytest.raises(RuntimeError, match="constructor path error"):
        read_scorer_trace(BrokenPath(), ID_A, max_section_bytes=-1)


def test_unselected_large_stderr_is_not_a_size_refusal(
    tmp_path: Path, read: Callable, layout: str
) -> None:
    run = tmp_path / "run"
    _trace(run, layout, stderr=b"large stderr")
    record = read(run, ID_A, sections=("request", "stdout"), max_section_bytes=2)
    assert record is not None and record.stderr is None
    assert dict(record.sizes) == {"request": 2, "stdout": 2, "stderr": 12}


def test_metadata_only_reads_no_payload_and_has_no_size_refusal(
    tmp_path: Path, layout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    _trace(run, layout, request=b"large request")
    with ScorerTraceReader(run) as reader:
        reader.read(ID_A, sections=())  # Warm only the pack's header index.

        def no_payload(*args):  # type: ignore[no-untyped-def]
            pytest.fail("metadata lookup attempted a payload read")

        monkeypatch.setattr(os, "read", no_payload)
        monkeypatch.setattr(os, "pread", no_payload)
        record = reader.read(ID_A, sections=(), max_section_bytes=0)
    assert record is not None and record.request is record.stdout is record.stderr is None
    assert dict(record.sizes) == {"request": 13, "stdout": 2, "stderr": 0}


def test_missing_directory_request_ignores_oversized_semantically_absent_streams(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    _trace(run, "directory", request=None, stdout=b"oversized")
    record = read_scorer_trace(run, ID_A, max_section_bytes=0)
    assert record is not None
    assert (record.request, record.stdout, record.stderr, dict(record.sizes)) == (
        None,
        None,
        None,
        {},
    )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_missing_directory_stream_stays_empty(tmp_path: Path, stream: str) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    (directory / stream).unlink()
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None and getattr(record, stream) == b"" and record.sizes[stream] == 0


def test_unrequested_unsafe_stderr_remains_unsized(tmp_path: Path) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    (directory / "stderr").unlink()
    (directory / "stderr").symlink_to(tmp_path / "absent")
    record = read_scorer_trace(run, ID_A, sections=("request", "stdout"), max_section_bytes=2)
    assert record is not None and record.stderr is None and "stderr" not in record.sizes


@pytest.mark.parametrize("missing_request", [False, True])
def test_selected_unsafe_entry_precedes_size_refusal_or_missing_request(
    tmp_path: Path, missing_request: bool
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory", request=None if missing_request else b"oversized")
    (directory / "stderr").unlink()
    (directory / "stderr").symlink_to(tmp_path / "absent")
    _refuses("unsafe_entry", lambda: read_scorer_trace(run, ID_A, max_section_bytes=0))


def test_ambiguity_precedes_oversize(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _trace(run, "directory", request=b"oversized")
    write_pack(run / "scorer-traces", "m.pack", [pack_record("m"), request_record(ID_A, b"{}")])
    _refuses("ambiguous", lambda: read_scorer_trace(run, ID_A, max_section_bytes=0))


def test_directory_hit_beside_corrupt_pack_is_still_readable(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _trace(run, "directory")
    write_private(run / "scorer-traces" / "m.pack", b"invalid\n")
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None and record.request == b"{}"


def test_known_sparse_oversize_is_refused_without_reading_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    with (directory / "request.json").open("wb") as file:
        file.truncate(32 * 2**20 + 1)
    reads: list[int] = []
    original = os.read

    def counting(fd: int, size: int) -> bytes:
        reads.append(size)
        return original(fd, size)

    monkeypatch.setattr(os, "read", counting)
    _refuses("too_large", lambda: read_scorer_trace(run, ID_A, max_section_bytes=32 * 2**20))
    assert reads == []


def _on_first_payload_read(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    original = os.read
    changed = False

    def reading(fd: int, size: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            change()
        return original(fd, size)

    monkeypatch.setattr(os, "read", reading)


@pytest.mark.parametrize("change", ["unlink", "replace", "directory"])
def test_admitted_payloads_survive_unlink_or_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")

    def mutate() -> None:
        if change == "directory":
            directory.rename(directory.with_name("old"))
            write_directory_trace(run / "scorer-traces", ID_A, request=b"other", stdout=b"bad")
        else:
            (directory / "stdout").unlink()
            if change == "replace":
                write_private(directory / "stdout", b"bad")
        (directory.with_name("old") if change == "directory" else directory).joinpath(
            "request.json"
        ).unlink()

    _on_first_payload_read(monkeypatch, mutate)
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None
    assert (record.request, record.stdout, record.stderr) == (b"{}", b"ok", b"")
    assert dict(record.sizes) == {"request": 2, "stdout": 2, "stderr": 0}


def test_admission_time_absent_stream_stays_empty_if_created_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory", stdout=None)
    _on_first_payload_read(monkeypatch, lambda: write_private(directory / "stdout", b"new"))
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None and record.stdout == b"" and record.sizes["stdout"] == 0


@pytest.mark.parametrize("present", [False, True])
def test_unselected_request_presence_is_observed_once_at_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory", request=b"{}" if present else None)
    original = traces._file

    def opening(fd: int, name: str, flags: int) -> int:
        if name == "stdout":
            if present:
                (directory / "request.json").unlink()
            else:
                write_private(directory / "request.json", b"{}")
        return original(fd, name, flags)

    monkeypatch.setattr(traces, "_file", opening)
    record = read_scorer_trace(run, ID_A, sections=("stdout",), max_section_bytes=2)
    assert record is not None and record.request is None
    assert record.stdout == (b"ok" if present else None)
    assert dict(record.sizes) == ({"request": 2, "stdout": 2, "stderr": 0} if present else {})


@pytest.mark.parametrize("mutation", ["mtime", "grow", "shrink"])
def test_in_limit_content_mutation_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    path = directory / "request.json"

    def mutate() -> None:
        before = path.stat()
        path.write_bytes({"mtime": b"[]", "grow": b"{} ", "shrink": b"{"}[mutation])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000))

    _on_first_payload_read(monkeypatch, mutate)
    _refuses("corrupt", lambda: read_scorer_trace(run, ID_A, max_section_bytes=4))


def test_mode_and_link_changes_alone_are_not_content_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")

    def mutate() -> None:
        (directory / "stdout").chmod(0o640)
        os.link(directory / "stdout", tmp_path / "alias")

    _on_first_payload_read(monkeypatch, mutate)
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None and record.stdout == b"ok"


def test_growth_reads_at_most_limit_plus_one_and_oversize_precedes_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    original = os.read
    consumed = 0

    def reading(fd: int, size: int) -> bytes:
        nonlocal consumed
        if not consumed:
            (directory / "request.json").write_bytes(b"x" * 100)
        data = original(fd, size)
        consumed += len(data)
        return data

    monkeypatch.setattr(os, "read", reading)
    _refuses("too_large", lambda: read_scorer_trace(run, ID_A, max_section_bytes=2))
    assert consumed == 3


def test_stable_metadata_with_short_read_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    _trace(run, "directory")
    original = os.read
    shortened = False

    def reading(fd: int, size: int) -> bytes:
        nonlocal shortened
        data = original(fd, size)
        if not shortened:
            shortened = True
            return data[:-1]
        return data

    monkeypatch.setattr(os, "read", reading)
    _refuses("corrupt", lambda: read_scorer_trace(run, ID_A, max_section_bytes=2))


@pytest.mark.parametrize("damage", ["digest", "short"])
def test_pack_still_checks_digest_and_short_payload_after_indexing(
    tmp_path: Path, damage: str
) -> None:
    run = tmp_path / "run"
    pack = _trace(run, "pack")
    with ScorerTraceReader(run) as reader:
        reader.read(ID_A, sections=())
        data = pack.read_bytes()
        if damage == "digest":
            pack.write_bytes(data.replace(b"\n{}\n", b"\n[]\n"))
        else:
            pack.write_bytes(data[: data.index(b"\n{}\n") + 2])
        _refuses("corrupt", lambda: reader.read(ID_A, sections=("request",), max_section_bytes=2))


def test_pack_does_not_read_a_growth_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    _trace(run, "pack")
    original = os.pread
    lengths: list[int] = []
    with ScorerTraceReader(run) as reader:
        reader.read(ID_A, sections=())

        def counting(fd: int, size: int, offset: int) -> bytes:
            lengths.append(size)
            return original(fd, size, offset)

        monkeypatch.setattr(os, "pread", counting)
        record = reader.read(ID_A, max_section_bytes=2)
    assert record is not None and record.stdout == b"ok"
    assert lengths == [2, 2]


@pytest.mark.parametrize("outcome", ["success", "oversize", "unsafe", "ambiguous", "corrupt"])
def test_call_releases_every_descriptor_on_success_and_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, layout: str
) -> None:
    run = tmp_path / "run"
    path = _trace(run, layout)
    if outcome == "unsafe":
        unsafe = path / "stderr" if layout == "directory" else path
        unsafe.unlink()
        unsafe.symlink_to(tmp_path / "absent")
    if outcome == "ambiguous":
        if layout == "directory":
            write_pack(
                run / "scorer-traces", "m.pack", [pack_record("m"), request_record(ID_A, b"{}")]
            )
        else:
            write_directory_trace(run / "scorer-traces", ID_A, request=b"{}")
    if outcome == "corrupt":
        if layout == "directory":
            _on_first_payload_read(monkeypatch, lambda: (path / "request.json").write_bytes(b"{"))
        else:
            path.write_bytes(path.read_bytes().replace(b"\n{}\n", b"\n[]\n"))
    opened: set[int] = set()
    original_open, original_close = os.open, os.close

    def opening(*args, **kwargs):  # type: ignore[no-untyped-def]
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def closing(fd: int) -> None:
        original_close(fd)
        opened.remove(fd)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(os, "close", closing)
    if outcome == "success":
        assert read_scorer_trace(run, ID_A, max_section_bytes=2) is not None
    else:
        _refuses(
            {
                "oversize": "too_large",
                "unsafe": "unsafe_entry",
                "ambiguous": "ambiguous",
                "corrupt": "corrupt",
            }[outcome],
            lambda: read_scorer_trace(
                run, ID_A, max_section_bytes=0 if outcome == "oversize" else 2
            ),
        )
    assert opened == set()


def test_none_keeps_unlimited_opened_size_behavior_under_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    original = os.pread
    changed = False

    def reading(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            (directory / "request.json").write_bytes(b"{}more")
        return original(fd, size, offset)

    monkeypatch.setattr(os, "pread", reading)
    record = read_scorer_trace(run, ID_A, max_section_bytes=None)
    assert record is not None and record.request == b"{}" and record.sizes["request"] == 2
    assert (directory / "request.json").read_bytes() == b"{}more"


def test_finite_and_unlimited_agree_on_unchanging_traces(tmp_path: Path, layout: str) -> None:
    run = tmp_path / "run"
    _trace(run, layout)
    assert read_scorer_trace(run, ID_A, max_section_bytes=2) == read_scorer_trace(run, ID_A)


def test_absent_trace_stays_none_with_finite_limit(tmp_path: Path) -> None:
    assert read_scorer_trace(tmp_path / "absent", ID_A, max_section_bytes=0) is None


def test_sparse_pack_oversize_is_refused_without_reading_its_selected_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    size = 32 * 2**20 + 1
    # Only the indexed length matters for oversize: its unverified digest and
    # sparse contents must never be consulted to diagnose the refused payload.
    header = encode_pack_record(
        {
            "format": PACK_FORMAT,
            "kind": "request",
            "score_request_id": ID_A,
            "sections": [["request", size, "0" * 64]],
        }
    )
    pack = write_private(run / "scorer-traces" / "m.pack", pack_record("m"))
    with pack.open("ab") as file:
        file.write(header[:-1])
    with pack.open("r+b") as file:
        file.seek(0, os.SEEK_END)
        file.seek(size, os.SEEK_CUR)
        file.write(b"\n")
    with ScorerTraceReader(run) as reader:
        head = reader.read(ID_A, sections=())
        assert head is not None and head.sizes["request"] == 33_554_433

        def no_payload(*args):  # type: ignore[no-untyped-def]
            pytest.fail("known oversized pack payload was read")

        monkeypatch.setattr(os, "pread", no_payload)
        _refuses("too_large", lambda: reader.read(ID_A, max_section_bytes=32 * 2**20))


def test_cached_oversized_pack_is_safely_admitted_before_size_refusal(tmp_path: Path) -> None:
    run = tmp_path / "run"
    pack = _trace(run, "pack", request=b"oversized")
    with ScorerTraceReader(run) as reader:
        reader.read(ID_A, sections=())
        pack.unlink()
        pack.symlink_to(tmp_path / "absent")
        _refuses("unsafe_entry", lambda: reader.read(ID_A, max_section_bytes=0))


def test_final_size_oversize_refuses_even_when_no_extra_byte_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    original = os.read
    consumed = 0

    def reading(fd: int, size: int) -> bytes:
        nonlocal consumed
        data = original(fd, size)
        consumed += len(data)
        if not data:
            (directory / "request.json").write_bytes(b"big")
        return data

    monkeypatch.setattr(os, "read", reading)
    _refuses("too_large", lambda: read_scorer_trace(run, ID_A, max_section_bytes=2))
    assert consumed == 2


def test_reads_continue_after_partial_system_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    _trace(run, "directory")
    original = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: original(fd, min(1, size)))
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None and (record.request, record.stdout) == (b"{}", b"ok")


def test_cached_request_only_pack_stays_request_only_after_output_append(tmp_path: Path) -> None:
    run = tmp_path / "run"
    pack = _trace(run, "pack", stdout=None, stderr=None)
    with ScorerTraceReader(run) as reader:
        before = reader.read(ID_A, max_section_bytes=2)
        with pack.open("ab") as file:
            file.write(output_record(ID_A, b"ok"))
        after = reader.read(ID_A, max_section_bytes=2)
    fresh = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert before is not None and before == after
    assert before.stdout is before.stderr is before.status is before.returncode is None
    assert dict(before.sizes) == {"request": 2}
    assert fresh is not None and fresh.stdout == b"ok" and fresh.status == "completed"


def test_bounded_pack_ignores_torn_output_tail(tmp_path: Path) -> None:
    run = tmp_path / "run"
    pack = _trace(run, "pack", stdout=None, stderr=None)
    with pack.open("ab") as file:
        file.write(output_record(ID_A, b"oversized output")[:-1])
    record = read_scorer_trace(run, ID_A, max_section_bytes=2)
    assert record is not None and record.request == b"{}" and record.stdout is None


def test_bounded_preflight_addressing_keeps_both_layouts(tmp_path: Path, layout: str) -> None:
    run = tmp_path / "run"
    path = _trace(run, layout)
    operation_root = run / "connection-logs" / "m"
    operation_root.parent.mkdir()
    (run / "scorer-traces").rename(operation_root)
    record = read_scorer_trace(run, ID_A, operation_id="m", max_section_bytes=2)
    assert record is not None and record.request == b"{}" and record.stdout == b"ok"
    assert record.location == (
        "connection-logs/m/scorer_traces/" + ID_A
        if layout == "directory"
        else "connection-logs/m/" + path.name
    )
    assert read_scorer_trace(run, ID_A, max_section_bytes=2) is None


def test_unselected_unsafe_request_still_establishes_presence(tmp_path: Path) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    (directory / "request.json").unlink()
    (directory / "request.json").symlink_to(tmp_path / "absent")
    record = read_scorer_trace(run, ID_A, sections=("stdout",), max_section_bytes=2)
    assert record is not None and record.stdout == b"ok" and record.request is None
    assert dict(record.sizes) == {"stdout": 2, "stderr": 0}


def test_prior_metadata_size_does_not_bound_a_later_directory_read(tmp_path: Path) -> None:
    run = tmp_path / "run"
    directory = _trace(run, "directory")
    with ScorerTraceReader(run) as reader:
        head = reader.read(ID_A, sections=(), max_section_bytes=2)
        assert head is not None and head.sizes["stdout"] == 2
        (directory / "stdout").write_bytes(b"new")
        _refuses("too_large", lambda: reader.read(ID_A, max_section_bytes=2))
