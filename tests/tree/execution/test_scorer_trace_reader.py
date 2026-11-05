"""The shared scorer-trace reader (ADR-0040 D5, D6; PDR-0032): both layouts, both roots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from evolution.tree import (
    ScorerTraceReader,
    ScorerTraceRecord,
    ScorerTraceUnreadable,
    read_scorer_trace,
)
from evolution.tree.execution.scorer_traces import PACK_FORMAT, encode_pack_record
from tests.scorer_trace_packs import (
    EXAMPLE_SHA256,
    ID_A,
    ID_B,
    STDERR_B,
    STDOUT_A,
    STDOUT_B,
    example_pack,
    output_record,
    pack_record,
    request_bytes,
    request_record,
    write_directory_trace,
    write_pack,
    write_private,
)

ID_C = "0123456789abcdef0123456789abcdef"


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    return run


def _example(run: Path) -> Path:
    return write_private(run / "scorer-traces" / "m-r01-a02.pack", example_pack())


def _code(excinfo: pytest.ExceptionInfo[ScorerTraceUnreadable]) -> str:
    return excinfo.value.code


# -- the format ------------------------------------------------------------------


def test_the_encoder_reproduces_the_designs_worked_example_byte_for_byte() -> None:
    data = example_pack()
    assert len(data) == 2476
    assert hashlib.sha256(data).hexdigest() == EXAMPLE_SHA256
    first = data.split(b"\n", 1)[0]
    assert json.loads(first)["hsha"] == (
        "d1b9988b5d84758ea1d19c7f58daed0ef2b5c50e258760684074976eb55c12ac"
    )


def test_the_encoder_refuses_a_header_its_reader_would_not_accept() -> None:
    with pytest.raises(ValueError):
        encode_pack_record({"format": PACK_FORMAT, "kind": "pack", "pack_id": "a:b", "segment": 0})
    with pytest.raises(ValueError):
        encode_pack_record(
            {"format": PACK_FORMAT, "kind": "request", "score_request_id": "x" * 5000},
            [("request", b"{}")],
        )


# -- packs -------------------------------------------------------------------------


def test_a_pack_try_reads_every_section_status_and_size(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _example(run)

    record = read_scorer_trace(run, ID_B, measurement_id="m-r01-a02")

    assert record == ScorerTraceRecord(
        ID_B,
        "pack",
        "scorer-traces/m-r01-a02.pack",
        request_bytes(ID_B, "c2"),
        STDOUT_B,
        STDERR_B,
        "completed",
        0,
        {"request": 440, "stdout": 48, "stderr": 18},
    )
    assert record.request_document()["trace_id"] == ID_B


def test_an_empty_pack_stream_is_empty_bytes_not_none(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _example(run)

    record = read_scorer_trace(run, ID_A, measurement_id="m-r01-a02")

    assert record is not None
    assert record.stderr == b"" and record.sizes["stderr"] == 0


def test_no_keyword_finds_a_pack_by_scanning_every_pack(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _example(run)

    record = read_scorer_trace(run, ID_A)

    assert record is not None and record.stdout == STDOUT_A


def test_an_evaluation_is_addressed_by_its_run_directory_and_measurement_id(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    write_pack(
        run / "scorer-traces",
        "evaluation.pack",
        (pack_record("evaluation"), request_record(ID_C, b"{}\n"), output_record(ID_C, b"{}")),
    )

    with_id = read_scorer_trace(run, ID_C, measurement_id="evaluation")
    without = read_scorer_trace(run, ID_C)

    assert with_id is not None and with_id == without
    assert with_id.location == "scorer-traces/evaluation.pack"


def test_a_preflight_trace_is_read_by_operation_id_in_both_layouts(tmp_path: Path) -> None:
    run = _run(tmp_path)
    operation = "5a2a4ac3f07242de92bed5edd781927e"
    root = run / "connection-logs" / operation
    write_pack(
        root,
        f"{operation}.pack",
        (pack_record(operation), request_record(ID_A, b"{}\n"), output_record(ID_A, b"{}")),
    )
    write_directory_trace(root, ID_B, request=b"{}\n", stdout=b"{}")

    packed = read_scorer_trace(run, ID_A, operation_id=operation)
    directory = read_scorer_trace(run, ID_B, operation_id=operation)

    assert packed is not None and packed.layout == "pack"
    assert packed.location == f"connection-logs/{operation}/{operation}.pack"
    assert directory is not None and directory.layout == "directory"
    assert read_scorer_trace(run, ID_A) is None  # preflights only through operation_id


def test_both_keywords_are_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_scorer_trace(_run(tmp_path), ID_A, measurement_id="m", operation_id="o")


def test_an_unknown_section_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_scorer_trace(_run(tmp_path), ID_A, sections=("stdin",))


@pytest.mark.parametrize("request_id", ["../x", ".hidden", "", "a/b", "x" * 129])
def test_an_id_outside_the_trace_grammar_is_invalid_id(tmp_path: Path, request_id: str) -> None:
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(_run(tmp_path), request_id)
    assert _code(excinfo) == "invalid_id"


def test_an_operation_id_outside_the_pack_grammar_is_invalid_id(tmp_path: Path) -> None:
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(_run(tmp_path), ID_A, operation_id="../x")
    assert _code(excinfo) == "invalid_id"


def test_sections_none_fills_sizes_and_reads_no_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path)
    _example(run)
    bodies: list[object] = []
    original = ScorerTraceReader._read_sections

    def counting(self, root, index, reads):  # type: ignore[no-untyped-def]
        bodies.append(dict(reads))
        return original(self, root, index, reads)

    monkeypatch.setattr(ScorerTraceReader, "_read_sections", counting)
    with ScorerTraceReader(run) as reader:
        head = reader.read(ID_B, measurement_id="m-r01-a02", sections=())
        assert head is not None and bodies == []
        assert dict(head.sizes) == {"request": 440, "stdout": 48, "stderr": 18}
        assert head.request is head.stdout is head.stderr is None
        body = reader.read(ID_B, measurement_id="m-r01-a02", sections=("request", "stdout"))
    assert body is not None and body.stderr is None
    assert len(body.request or b"") == head.sizes["request"]
    assert len(body.stdout or b"") == head.sizes["stdout"]


def test_a_try_with_only_its_request_record_has_no_output(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_pack(
        run / "scorer-traces",
        "m-1.pack",
        (pack_record("m-1"), request_record(ID_A, b"{}\n")),
    )

    record = read_scorer_trace(run, ID_A, measurement_id="m-1")

    assert record is not None
    assert (record.stdout, record.stderr, record.status, record.returncode) == (
        None,
        None,
        None,
        None,
    )
    assert dict(record.sizes) == {"request": 3}


def test_an_id_absent_from_a_sound_pack_is_none(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _example(run)
    assert read_scorer_trace(run, ID_C, measurement_id="m-r01-a02") is None


# -- the torn and corrupt rule (ADR-0040 D4, D5) --------------------------------


def test_a_torn_tail_is_ignored_and_the_prefix_stays_readable(tmp_path: Path) -> None:
    run = _run(tmp_path)
    data = example_pack()
    for cut in (len(data) - 1, len(data) - 30, 2021 + 10):  # body, body, header of the last record
        write_private(run / "scorer-traces" / "m-r01-a02.pack", data[:cut])
        with ScorerTraceReader(run) as reader:
            assert reader.read(ID_A, measurement_id="m-r01-a02") is not None
            partial = reader.read(ID_B, measurement_id="m-r01-a02")
        # ID_B's request is complete; its output record is the torn one.
        assert partial is not None and partial.stdout is None


def test_a_flipped_body_byte_makes_only_that_try_corrupt(tmp_path: Path) -> None:
    run = _run(tmp_path)
    data = bytearray(example_pack())
    data[2021 + 388] ^= 0x01  # first stdout byte of ID_B's output
    write_private(run / "scorer-traces" / "m-r01-a02.pack", bytes(data))

    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_B, measurement_id="m-r01-a02")
    assert _code(excinfo) == "corrupt"
    assert read_scorer_trace(run, ID_B, measurement_id="m-r01-a02", sections=("request",))
    assert read_scorer_trace(run, ID_A, measurement_id="m-r01-a02") is not None


@pytest.mark.parametrize("record_start", [1586, 2021], ids=["earlier", "last"])
def test_a_flipped_length_digit_is_corrupt_not_torn(tmp_path: Path, record_start: int) -> None:
    run = _run(tmp_path)
    data = example_pack()
    header_end = data.index(b"\n", record_start)
    header = data[record_start:header_end].replace(b'["stdout",4', b'["stdout",9', 1)
    damaged = data[:record_start] + header + data[header_end:]
    write_private(run / "scorer-traces" / "m-r01-a02.pack", damaged)

    # Everything before the damaged header stays readable; the damaged try, and
    # any id the pack does not show, is `corrupt`: it may lie beyond.
    assert read_scorer_trace(run, ID_A, measurement_id="m-r01-a02", sections=("request",))
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_C, measurement_id="m-r01-a02")
    assert _code(excinfo) == "corrupt"


def _malformed_header(*, kind: object = "request", status: object = None) -> bytes:
    if status is None:
        fields = {"format": PACK_FORMAT, "kind": kind, "score_request_id": ID_C}
        fields["sections"] = [["request", 2, hashlib.sha256(b"{}\n").hexdigest()]]
    else:
        fields = {
            "format": PACK_FORMAT,
            "kind": "output",
            "score_request_id": ID_C,
            "status": status,
            "returncode": 0,
            "sections": [
                ["stdout", 0, hashlib.sha256(b"").hexdigest()],
                ["stderr", 0, hashlib.sha256(b"").hexdigest()],
            ],
        }
    if isinstance(kind, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in kind):
        # The escaped JSON parses to a lone surrogate, which cannot be encoded
        # by the format's UTF-8 canonicalizer. A bad hsha must still be corrupt.
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return encoded.encode("ascii") + b"\n"
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    fields["hsha"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return encoded.encode("utf-8") + b"\n"


@pytest.mark.parametrize(
    "header",
    [
        _malformed_header(kind=[]),
        _malformed_header(kind={}),
        _malformed_header(status=[]),
        _malformed_header(status={}),
        _malformed_header(kind="\ud800"),
    ],
    ids=["kind-list", "kind-object", "status-list", "status-object", "escaped-surrogate"],
)
def test_malformed_headers_preserve_the_prefix_and_mark_misses_corrupt(
    tmp_path: Path, header: bytes
) -> None:
    run = _run(tmp_path)
    write_pack(
        run / "scorer-traces",
        "m-r01-a02.pack",
        (pack_record("m-r01-a02"), request_record(ID_A, b"{}\n"), header),
    )

    prefix = read_scorer_trace(run, ID_A, measurement_id="m-r01-a02")
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_C, measurement_id="m-r01-a02")

    assert prefix is not None and prefix.request == b"{}\n"
    assert _code(excinfo) == "corrupt"


def test_a_wrong_terminator_is_corrupt_even_on_the_last_record(tmp_path: Path) -> None:
    run = _run(tmp_path)
    data = bytearray(example_pack())
    data[-1] = ord("x")
    write_private(run / "scorer-traces" / "m-r01-a02.pack", bytes(data))

    assert read_scorer_trace(run, ID_A, measurement_id="m-r01-a02") is not None
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_C, measurement_id="m-r01-a02")
    assert _code(excinfo) == "corrupt"


def test_no_newline_within_the_header_bound_is_corrupt(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_private(run / "scorer-traces" / "m-r01-a02.pack", pack_record("m-r01-a02") + b"x" * 5000)
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A, measurement_id="m-r01-a02")
    assert _code(excinfo) == "corrupt"


def test_a_renamed_pack_is_corrupt(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_private(run / "scorer-traces" / "m-r09-a01.pack", example_pack())
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A, measurement_id="m-r09-a01")
    assert _code(excinfo) == "corrupt"


def test_a_duplicate_id_is_ambiguous_in_one_pack_and_across_segments(tmp_path: Path) -> None:
    run = _run(tmp_path)
    traces = run / "scorer-traces"
    write_pack(
        traces,
        "m-1.pack",
        (pack_record("m-1"), request_record(ID_A, b"{}\n"), request_record(ID_A, b"{}\n")),
    )
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A, measurement_id="m-1")
    assert _code(excinfo) == "ambiguous"

    write_pack(traces, "m-2.pack", (pack_record("m-2"), request_record(ID_B, b"{}\n")))
    write_pack(traces, "m-2.s1.pack", (pack_record("m-2", 1), request_record(ID_B, b"{}\n")))
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_B, measurement_id="m-2")
    assert _code(excinfo) == "ambiguous"


def test_segments_are_read_in_order_after_a_corrupt_one(tmp_path: Path) -> None:
    run = _run(tmp_path)
    traces = run / "scorer-traces"
    write_pack(
        traces,
        "m-1.pack",
        (pack_record("m-1"), request_record(ID_A, b"{}\n"), b"garbage\n" * 600),
    )
    write_pack(traces, "m-1.s1.pack", (pack_record("m-1", 1), request_record(ID_B, b"{}\n")))

    first = read_scorer_trace(run, ID_A, measurement_id="m-1")
    later = read_scorer_trace(run, ID_B, measurement_id="m-1")

    assert first is not None and first.location == "scorer-traces/m-1.pack"
    assert later is not None and later.location == "scorer-traces/m-1.s1.pack"
    with pytest.raises(ScorerTraceUnreadable):
        read_scorer_trace(run, ID_C, measurement_id="m-1")


def test_a_live_pack_is_read_up_to_its_size_at_open(tmp_path: Path) -> None:
    run = _run(tmp_path)
    data = example_pack()
    path = write_private(run / "scorer-traces" / "m-r01-a02.pack", data[: 2021 + 100])
    with ScorerTraceReader(run) as reader:
        assert reader.read(ID_B, measurement_id="m-r01-a02").stdout is None  # type: ignore[union-attr]
        path.write_bytes(data)
        # The index was taken at the first lookup; a new reader sees the rest.
        assert reader.read(ID_B, measurement_id="m-r01-a02").stdout is None  # type: ignore[union-attr]
    assert read_scorer_trace(run, ID_B, measurement_id="m-r01-a02").stdout == STDOUT_B  # type: ignore[union-attr]


def test_an_unsafe_pack_is_unsafe_entry(tmp_path: Path) -> None:
    run = _run(tmp_path)
    real = write_private(tmp_path / "elsewhere.pack", example_pack())
    (run / "scorer-traces").mkdir(mode=0o700)
    (run / "scorer-traces" / "m-r01-a02.pack").symlink_to(real)
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A, measurement_id="m-r01-a02")
    assert _code(excinfo) == "unsafe_entry"


# -- the directory layout ----------------------------------------------------------------


def test_a_directory_try_reads_its_three_files(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_directory_trace(
        run / "scorer-traces", ID_A, request=b"{}\n", stdout=STDOUT_A, stderr=b"warn\n"
    )

    record = read_scorer_trace(run, ID_A, measurement_id="m-r01-a01")

    assert record == ScorerTraceRecord(
        ID_A,
        "directory",
        f"scorer-traces/scorer_traces/{ID_A}",
        b"{}\n",
        STDOUT_A,
        b"warn\n",
        None,
        None,
        {"request": 3, "stdout": len(STDOUT_A), "stderr": 5},
    )


def test_a_missing_stream_file_next_to_the_request_is_empty(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}\n", stdout=STDOUT_A)

    record = read_scorer_trace(run, ID_A)

    assert record is not None and record.stderr == b"" and record.sizes["stderr"] == 0


def test_without_the_request_every_section_is_none(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_directory_trace(run / "scorer-traces", ID_A, request=None, stdout=STDOUT_A)

    record = read_scorer_trace(run, ID_A)

    assert record is not None
    assert (record.request, record.stdout, record.stderr, dict(record.sizes)) == (
        None,
        None,
        None,
        {},
    )


def test_an_absent_try_is_none(tmp_path: Path) -> None:
    assert read_scorer_trace(_run(tmp_path), ID_A) is None


def test_an_unsafe_requested_file_is_unsafe_entry_and_an_unrequested_one_is_unsized(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    directory = write_directory_trace(run / "scorer-traces", ID_A, request=b"{}\n")
    elsewhere = write_private(tmp_path / "elsewhere", STDOUT_A)
    (directory / "stdout").symlink_to(elsewhere)

    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A)
    assert _code(excinfo) == "unsafe_entry"
    head = read_scorer_trace(run, ID_A, sections=("request",))
    assert head is not None and "stdout" not in head.sizes


def test_a_second_hard_link_is_unsafe_entry(tmp_path: Path) -> None:
    run = _run(tmp_path)
    directory = write_directory_trace(run / "scorer-traces", ID_A, request=b"{}\n")
    os.link(directory / "request.json", tmp_path / "alias")
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A)
    assert _code(excinfo) == "unsafe_entry"


def test_a_try_in_both_layouts_is_ambiguous(tmp_path: Path) -> None:
    run = _run(tmp_path)
    _example(run)
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}\n")
    with pytest.raises(ScorerTraceUnreadable) as excinfo:
        read_scorer_trace(run, ID_A, measurement_id="m-r01-a02")
    assert _code(excinfo) == "ambiguous"


def test_a_mixed_run_reads_old_tries_from_directories_and_new_ones_from_the_pack(
    tmp_path: Path,
) -> None:
    run = _run(tmp_path)
    _example(run)
    write_directory_trace(run / "scorer-traces", ID_C, request=b"{}\n", stdout=b"{}")
    with ScorerTraceReader(run) as reader:
        old = reader.read(ID_C, measurement_id="m-r01-a02")
        new = reader.read(ID_A, measurement_id="m-r01-a02")
    assert old is not None and old.layout == "directory"
    assert new is not None and new.layout == "pack"


def test_a_symlinked_run_directory_is_resolved_once(tmp_path: Path) -> None:
    run = _run(tmp_path)
    write_directory_trace(run / "scorer-traces", ID_A, request=b"{}\n")
    link = tmp_path / "link"
    link.symlink_to(run, target_is_directory=True)
    assert read_scorer_trace(link, ID_A) is not None


def test_request_document_refuses_an_absent_or_unparseable_request() -> None:
    absent = ScorerTraceRecord(ID_A, "directory", "x", None, None, None, None, None)
    broken = ScorerTraceRecord(ID_A, "directory", "x", b"{", None, None, None, None)
    with pytest.raises(ValueError):
        absent.request_document()
    with pytest.raises(ValueError):
        broken.request_document()


@pytest.mark.parametrize("preflight", [False, True])
def test_reader_consumes_production_writer_bytes_in_both_roots(
    tmp_path: Path, preflight: bool
) -> None:
    from evolution.tree.execution.scorer_trace_writer import PackedTraceWriter

    run = _run(tmp_path)
    pack_id = "operation" if preflight else "evaluation"
    root = run / "connection-logs" / pack_id if preflight else run / "scorer-traces"
    with PackedTraceWriter(root, pack_id) as writer:
        writer.append_request(ID_A, request_bytes(ID_A, "c1"))
        with writer.streams() as (stdout, stderr):
            stdout.write(STDOUT_A)
            stderr.write(b"\x00\xff\r\n")
            writer.append_output(ID_A, stdout, stderr, status="timeout", returncode=None)
    kwargs = {"operation_id": pack_id} if preflight else {"measurement_id": pack_id}
    record = read_scorer_trace(run, ID_A, **kwargs)
    assert record is not None
    assert record.request == request_bytes(ID_A, "c1")
    assert record.stdout == STDOUT_A and record.stderr == b"\x00\xff\r\n"
    assert record.status == "timeout" and record.returncode is None
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        metadata = reader.read(ID_A, sections=(), max_section_bytes=0, **kwargs)
        assert metadata is not None and metadata.sizes == record.sizes
