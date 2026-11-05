"""Raw baseline episode pages retain authentication and audience boundaries."""

import hashlib
import json

import pytest

from evolution.tree.baseline_diagnostics import DiagnosticTraceReader
from evolution.tree.baseline_traces import BaselineTraces
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.reflection_reader import ReaderArgumentError, ReaderIntegrityError
from tests.test_tree_evidence_projection import inputs, pin
from tests.tree.execution.test_journal_identity import cases
from tests.tree.roles.editor.test_editor_evidence import _Trace


def reader(trace, **changes):
    return BaselineTraces(
        traces=DiagnosticTraceReader(trace.run_root),
        measurement_records=changes.pop("measurement_records", trace.journal.snapshot().records),
        pin=changes.pop("pin", pin(trace_page_chars=256)),
        secrets=changes.pop("secrets", ()),
        corpus=cases(),
        baseline=trace.baseline,
        **changes,
    )


def test_trace_paged_three_times_counts_once(tmp_path):
    from evolution.tree.execution.check_modes import CheckModes, HitSink
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin

    hits = HitSink()
    trace = _Trace(tmp_path, turns=1, events=({"type": "tool_result", "result": "SECRET"},))
    try:
        source = reader(
            trace,
            secrets=("SECRET",),
            hits=hits,
            pin=EvidencePin(
                DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": "shadow"}).input_modes()
            ),
        )
        for _ in range(3):
            source.page("obs:c:1", 0)
        assert hits.entries() == [
            {"check": "input_run_secret_redaction", "code": "redacted", "count": 1}
        ]
    finally:
        trace.close()


def test_compact_projection_and_trace_pages_share_field_identity(tmp_path):
    from evolution.tree.execution.check_modes import CheckModes, HitSink
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin

    hits = HitSink()
    trace = _Trace(tmp_path, turns=1, events=({"type": "tool_result", "result": "SECRET"},))
    evidence_pin = EvidencePin(
        DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": "shadow"}).input_modes()
    )
    try:
        trace.project(pin=evidence_pin, secrets=("SECRET",), hits=hits)
        source = reader(trace, secrets=("SECRET",), hits=hits, pin=evidence_pin)
        source.page("obs:c:1", 0)
        assert hits.entries() == [
            {"check": "input_run_secret_redaction", "code": "redacted", "count": 1}
        ]
    finally:
        trace.close()


def test_read_trace_pages_the_episode_and_never_returns_oracle_or_scorer_output(tmp_path):
    trace = _Trace(tmp_path, turns=2, events=({"type": "tool_result", "result": "SECRET"},))
    try:
        source = reader(trace, secrets=("SECRET",))
        assert source.ids() == ("obs:c:1",)
        pages = []
        offset = 0
        while offset is not None:
            page = source.page("obs:c:1", offset)
            pages.append(page)
            offset = page["next_offset"]
        text = "".join(p["content"] for p in pages)
        assert text == canonical_json(trace.input["view"]).decode().replace("SECRET", "[REDACTED]")
        assert len(pages) > 1 and all(len(p["content"]) <= 256 for p in pages)
        assert all(p["content_digest"] == hashlib.sha256(text.encode()).hexdigest() for p in pages)
        for forbidden in ('"oracle"', "DETAILS-CANARY", "STDERR-CANARY", "SECRET"):
            assert forbidden not in text
    finally:
        trace.close()


def test_read_trace_fails_source_changed_on_a_changed_trace(tmp_path):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = reader(trace)
        source.page("obs:c:1")
        path = trace.directory / "request.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with pytest.raises(ReaderIntegrityError, match="source_changed"):
            source.page("obs:c:1")
        path.write_bytes(original)
        with pytest.raises(ReaderIntegrityError, match="source_changed"):
            source.page("obs:c:1")
    finally:
        trace.close()


def test_read_trace_paging_edges_are_tool_errors(tmp_path):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = reader(trace)
        total = len(source.whole("obs:c:1"))
        page = source.page("obs:c:1", total)
        assert page["content"] == "" and page["next_offset"] is None
        for offset in (-1, total + 1, True, 1.5):
            with pytest.raises(ReaderArgumentError, match="bad_offset"):
                source.page("obs:c:1", offset)
        with pytest.raises(ReaderArgumentError, match="unknown_evidence_id"):
            source.page("obs:missing:1")
        assert source.page("obs:c:1")["content"]
    finally:
        trace.close()


def test_pinned_reader_exposes_raw_episode_and_unpinned_reader_keeps_methods(tmp_path):
    trace = _Trace(tmp_path, turns=1)
    try:
        assert "read_trace" not in inputs(trace).handlers()
        source = inputs(trace, pin())
        page = source.handlers()["read_trace"]({"evidence_id": "obs:c:1"})
        assert json.loads(page["content"]) == trace.input["view"]
    finally:
        trace.close()


def test_read_trace_reauthenticates_even_an_initially_modified_request(tmp_path):
    trace = _Trace(tmp_path, turns=1)
    try:
        document = dict(trace.input)
        document["view"] = {**document["view"], "terminal": "forged"}
        trace.write_request(document)
        with pytest.raises(ReaderArgumentError, match="trace_unavailable"):
            reader(trace).whole("obs:c:1")
    finally:
        trace.close()


@pytest.mark.parametrize("unavailable", ["unscored", "missing", "unbound", "invalid_json"])
def test_unavailable_trace_is_a_tool_error_and_does_not_poison_other_ids(tmp_path, unavailable):
    trace = _Trace(tmp_path, turns=1, samples=2)
    try:
        records = trace.journal.snapshot().records
        if unavailable == "unscored":
            records = tuple(
                row
                for row in records
                if not (
                    row.event in {"scoring_started", "scoring_finished"}
                    and row.payload.get("sample_id") == "s1"
                )
            )
        elif unavailable == "missing":
            (trace.directory / "request.json").unlink()
        elif unavailable == "unbound":
            trace.write_request({**trace.input, "case_id": "wrong-case"})
        else:
            (trace.directory / "request.json").write_text("{")
        source = reader(trace, measurement_records=records)
        for _ in range(2):
            with pytest.raises(ReaderArgumentError, match="^trace_unavailable$"):
                source.read({"evidence_id": "obs:c:1"})
            assert json.loads(source.whole("obs:c:2")) == trace.input["view"]
    finally:
        trace.close()


@pytest.mark.parametrize("fault", ["baseline", "trace_reader"])
def test_storage_corruption_propagates_without_poisoning_reader(tmp_path, monkeypatch, fault):
    from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
    from evolution.tree.artifacts.artifacts import ArtifactRef

    trace = _Trace(tmp_path, turns=1)
    try:
        source = reader(trace)
        error = ArtifactCorruptionError("artifact_digest_mismatch")

        def corrupt(*args, **kwargs):
            raise error

        with monkeypatch.context() as patch:
            if fault == "baseline":
                patch.setattr(ArtifactRef, "verify", corrupt)
            else:
                patch.setattr(DiagnosticTraceReader, "read", corrupt)
            with pytest.raises(ArtifactCorruptionError) as caught:
                source.read({"evidence_id": "obs:c:1"})
            assert caught.value is error

        assert source.read({"evidence_id": "obs:c:1"})["content"]
        assert json.loads(source.whole("obs:c:1")) == trace.input["view"]
    finally:
        trace.close()


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
def test_raw_trace_input_modes(tmp_path, mode):
    from evolution.tree.execution.check_modes import CheckModes, HitSink
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin

    trace = _Trace(tmp_path, turns=1, events=({"type": "tool_result", "result": "SECRET"},))
    try:
        hits = HitSink()
        source = BaselineTraces(
            traces=DiagnosticTraceReader(trace.run_root),
            measurement_records=trace.journal.snapshot().records,
            pin=EvidencePin(
                DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": mode}).input_modes()
            ),
            secrets=("SECRET",),
            corpus=cases(),
            baseline=trace.baseline,
            hits=hits,
        )
        text = source.whole("obs:c:1")
        assert ("SECRET" in text) is (mode != "enforce")
        assert bool(hits.entries()) is (mode == "shadow")
    finally:
        trace.close()


@pytest.mark.parametrize("offset", [-1, "bad", 10**9])
def test_trace_bad_offset_detail_names_range(tmp_path, offset):
    trace = _Trace(tmp_path, turns=1)
    try:
        source = reader(trace)
        total = len(source.whole("obs:c:1"))
        with pytest.raises(ReaderArgumentError) as caught:
            source.page("obs:c:1", offset)
        assert str(caught.value) == "bad_offset"
        assert caught.value.detail == f"offset must be between 0 and {total}"
    finally:
        trace.close()
