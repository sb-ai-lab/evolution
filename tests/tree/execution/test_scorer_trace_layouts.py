"""ADR-0037 I1 through ADR-0040: every internal reader projects a packed try byte for byte
as it projects the same try in the directory layout, and a missing stream file as an empty one."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evolution.tree.execution.scorer_traces import TRACE_ROOT, TRACE_SUBDIR, ScorerTraceReader
from evolution.tree.roles.reflector.reflect_evidence import project_repeats
from evolution.tree.run_trace import TraceDirReader
from tests.campaign_fakes import PRIMARY
from tests.scorer_trace_packs import write_production_pack
from tests.tree.execution.test_journal_identity import cases
from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace
from tests.tree.roles.reflector.test_reflection_inputs import _inputs

ROWS = tuple(_reflect_row(f"s{index}", index) for index in (1, 2, 3))


def _pack(trace: _Trace, *, keep: int = 0) -> None:
    """Move every directory-layout try of the trace's journal into its pack."""

    started = [r for r in trace.journal.snapshot().records if r.event == "scoring_started"]
    run_id = str(started[0].to_payload()["run_id"])
    traces = trace.run_root / TRACE_ROOT
    traces.chmod(0o700)
    with ScorerTraceReader(trace.run_root) as reader:
        records = [reader.read(str(r.payload["score_request_id"])) for r in started[keep:]]
    assert all(record is not None for record in records)
    write_production_pack(traces, run_id, records)  # type: ignore[arg-type]
    for record in records:
        assert record is not None
        shutil.rmtree(traces / TRACE_SUBDIR / record.score_request_id)
    if not keep:
        (traces / TRACE_SUBDIR).rmdir()


def _projections(trace: _Trace) -> dict[str, object]:
    records = trace.journal.snapshot().records
    source = _inputs(trace, rows=ROWS)
    handlers = source.handlers()
    case_handle = handlers["list_evidence"]({})["entries"][0]["case_handle"]
    evidence = handlers["list_evidence"]({"case_id": case_handle})["entries"]
    with source.reader_view().open_operation("round-1", {"read_evidence"}) as reader:
        bodies = [
            reader.dispatch("read_evidence", {"evidence_id": entry["evidence_id"]})
            for entry in evidence
        ]
    with TraceDirReader(trace.run_root, records) as traces:
        exported = [
            traces.trace(
                str(record.payload["score_request_id"]),
                measurement_id=str(record.to_payload()["run_id"]),
            )
            for record in records
            if record.event == "scoring_started"
        ]
    return {
        "observations": trace.project(),
        "repeats": project_repeats(
            rows=ROWS,
            records=records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        ),
        "headers": [
            (h.available, h.failing, h.failed_criteria, h.terminal) for h in source._headers
        ],
        "evidence": bodies,
        "trace": exported,
    }


@pytest.mark.parametrize("keep", [0, 1])
def test_a_packed_try_projects_byte_for_byte_as_its_directory(tmp_path: Path, keep: int) -> None:
    trace = _Trace(tmp_path, turns=10, samples=3)
    try:
        before = _projections(trace)
        _pack(trace, keep=keep)
        after = _projections(trace)
    finally:
        trace.close()
    assert before == after
    assert all(o.status == "present" for o in before["observations"].values())  # type: ignore[attr-defined]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_a_missing_stream_file_projects_as_an_empty_one(tmp_path: Path, stream: str) -> None:
    empty = _Trace(tmp_path / "empty", turns=2, samples=3)
    missing = _Trace(tmp_path / "missing", turns=2, samples=3)
    try:
        for request_id in empty.request_ids:
            (empty.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / stream).write_bytes(b"")
        for request_id in missing.request_ids:
            (missing.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id / stream).unlink()
        assert _projections(empty) == _projections(missing)
    finally:
        empty.close()
        missing.close()


def test_request_allocation_failure_projects_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = _Trace(tmp_path)
    original = ScorerTraceReader.read

    def fail_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("sections") == ("request",):
            raise MemoryError
        return original(self, *args, **kwargs)

    try:
        monkeypatch.setattr(ScorerTraceReader, "read", fail_request)
        observation = trace.project()[("c", "s1")]
    finally:
        trace.close()
    assert observation.status == "unavailable"


def test_exporter_stdout_allocation_failure_keeps_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = _Trace(tmp_path)
    started = next(r for r in trace.journal.snapshot().records if r.event == "scoring_started")
    request_id = str(started.payload["score_request_id"])
    measurement_id = str(started.to_payload()["run_id"])
    original = ScorerTraceReader.read

    def fail_stdout(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("sections") == ("stdout",):
            raise MemoryError
        return original(self, *args, **kwargs)

    try:
        monkeypatch.setattr(ScorerTraceReader, "read", fail_stdout)
        with TraceDirReader(trace.run_root, (started,)) as reader:
            exported = reader.trace(request_id, measurement_id=measurement_id)
    finally:
        trace.close()
    assert exported is not None
    assert exported.request["trace_id"] == request_id
    assert exported.stdout is None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_internal_projection_does_not_swallow_process_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
) -> None:
    trace = _Trace(tmp_path)

    def stop(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise interrupt

    try:
        monkeypatch.setattr(ScorerTraceReader, "read", stop)
        with pytest.raises(interrupt):
            trace.project()
    finally:
        trace.close()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_exporter_does_not_swallow_process_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: type[BaseException]
) -> None:
    trace = _Trace(tmp_path)
    started = next(r for r in trace.journal.snapshot().records if r.event == "scoring_started")
    request_id = str(started.payload["score_request_id"])
    measurement_id = str(started.to_payload()["run_id"])
    original = ScorerTraceReader.read

    def stop_stdout(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("sections") == ("stdout",):
            raise interrupt
        return original(self, *args, **kwargs)

    try:
        monkeypatch.setattr(ScorerTraceReader, "read", stop_stdout)
        with TraceDirReader(trace.run_root, (started,)) as reader:
            with pytest.raises(interrupt):
                reader.trace(request_id, measurement_id=measurement_id)
    finally:
        trace.close()
