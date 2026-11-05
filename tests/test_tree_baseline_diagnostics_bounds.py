"""Exact limits and greedy slot admission without corpus-sized trace buffers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import tracemalloc

import pytest

from evolution.tree import baseline_diagnostics as diagnostics
from evolution.tree.execution.journal_identity import canonical_json
from tests.test_tree_baseline_diagnostics import fixture_slot, index


def _repeat(slot, repeat, *, sample_id=None):
    admitted = json.loads(canonical_json(slot.admitted))
    admitted.update(repeat=repeat, sample_id=sample_id or f"s{repeat:05}")
    return dataclasses.replace(slot, admitted=admitted)


@pytest.mark.parametrize("layout", ["directory", "pack"])
@pytest.mark.parametrize("section", ["request", "stdout"])
@pytest.mark.parametrize("extra", [0, 1])
def test_eight_mib_section_limit_is_inclusive_without_truncation(tmp_path, section, extra, layout):
    trace, slot = fixture_slot(tmp_path)
    path = trace.directory / ("request.json" if section == "request" else "stdout")
    raw = path.read_bytes()
    size = 8 * 1024 * 1024 + extra
    with path.open("ab") as stream:
        stream.write(b" " * (size - len(raw)))
    if layout == "pack":
        from tests.scorer_trace_packs import output_record, pack_record, request_record, write_pack

        write_pack(
            trace.run_root / "scorer-traces",
            "original-measurement.pack",
            [
                pack_record("original-measurement"),
                request_record(trace.request_id, (trace.directory / "request.json").read_bytes()),
                output_record(trace.request_id, (trace.directory / "stdout").read_bytes()),
            ],
        )
        shutil.rmtree(trace.directory)
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.included == (0 if extra else 1)
    if extra:
        assert result.gaps.counts["too_large"] == 1
        assert index(result)["entries"] == []
    else:
        assert index(result)["entries"][0]["sections"][section]["length"] == size


def test_exact_total_budget_includes_index_comma_header_footer_and_both_sections(
    tmp_path, monkeypatch
):
    trace, slot = fixture_slot(tmp_path)
    slots = [_repeat(slot, 1), _repeat(slot, 2)]
    full = diagnostics.prepare_diagnostics(
        slots, source_root=trace.run_root, destination=tmp_path / "full"
    )
    exact = full.index_path.stat().st_size + full.payload_path.stat().st_size
    assert exact == full.byte_count
    assert full.index_path.read_bytes() == canonical_json(index(full))
    monkeypatch.setattr(diagnostics, "MAX_BUNDLE_BYTES", exact)
    fits = diagnostics.prepare_diagnostics(
        list(reversed(slots)), source_root=trace.run_root, destination=tmp_path / "fits"
    )
    assert fits.included == 2
    assert fits.digest == full.digest
    monkeypatch.setattr(diagnostics, "MAX_BUNDLE_BYTES", exact - 1)
    short = diagnostics.prepare_diagnostics(
        slots, source_root=trace.run_root, destination=tmp_path / "short"
    )
    assert short.included == 1
    assert short.gaps.counts["bundle_budget"] == 1
    assert short.byte_count < exact - 1
    assert short.payload_path.read_bytes().count(b'"trace_id"') == 1


def test_large_early_slot_skipped_while_later_small_slot_fits(tmp_path, monkeypatch):
    trace, slot = fixture_slot(tmp_path)
    small = _repeat(slot, 2)
    large = _repeat(slot, 1, sample_id="large" * 2000)
    reference = diagnostics.prepare_diagnostics(
        [small], source_root=trace.run_root, destination=tmp_path / "reference"
    )
    monkeypatch.setattr(diagnostics, "MAX_BUNDLE_BYTES", reference.byte_count)
    result = diagnostics.prepare_diagnostics(
        [small, large], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.included == 1
    assert result.digest == reference.digest
    assert result.payload_path.read_bytes() == reference.payload_path.read_bytes()
    assert result.gaps.counts["bundle_budget"] == 1
    assert (
        result.gaps.samples[0]["slot_sha256"]
        == hashlib.sha256(
            canonical_json({"case_id": "c", "repeat": 1, "sample_id": "large" * 2000})
        ).hexdigest()
    )


def test_large_all_missing_schedule_has_constant_empty_index_and_bounded_gaps(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    shutil.rmtree(trace.directory)
    slots = [_repeat(slot, n, sample_id="x" * 1200 + str(n)) for n in range(1, 3001)]
    result = diagnostics.prepare_diagnostics(
        list(reversed(slots)), source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.included == 0
    assert result.gaps.counts["missing"] == 3000
    assert result.gaps.scheduled == 3000
    assert len(result.gaps.samples) == 64
    assert len(result.gaps.encoded) <= 64 * 1024
    assert (
        result.index_path.read_bytes() == b'{"entries":[],"format":"baseline-diagnostic-bundle/1"}'
    )
    assert result.gaps.digest == hashlib.sha256(result.gaps_path.read_bytes()).hexdigest()
    expected_first = {"case_id": "c", "repeat": 1, "sample_id": "x" * 1200 + "1"}
    assert (
        result.gaps.samples[0]["slot_sha256"]
        == hashlib.sha256(canonical_json(expected_first)).hexdigest()
    )
    assert sum(result.gaps.counts.values()) == 3000


def test_trace_bodies_are_not_retained_across_slots(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    stdout = trace.directory / "stdout"
    with stdout.open("ab") as stream:
        stream.write(b" " * (128 * 1024))
    slots = [_repeat(slot, n) for n in range(1, 129)]
    tracemalloc.start()
    try:
        result = diagnostics.prepare_diagnostics(
            slots, source_root=trace.run_root, destination=tmp_path / "prepared"
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.included == 128
    assert result.byte_count > 16 * 1024 * 1024
    assert peak < 8 * 1024 * 1024


def test_duplicate_destination_is_rejected_before_writing(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    destination = tmp_path / "prepared"
    with pytest.raises(ValueError, match="duplicate"):
        diagnostics.prepare_diagnostics(
            [slot, slot], source_root=trace.run_root, destination=destination
        )
    assert not destination.exists()


def test_missing_stdout_has_missing_reason_not_invalid_output(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    (trace.directory / "stdout").unlink()
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.gaps.counts["missing"] == 1


def test_packed_source_index_is_reused_for_consecutive_slots(tmp_path, monkeypatch):
    from evolution.tree.execution import scorer_traces

    trace, slot = fixture_slot(tmp_path, layout="pack")
    original = scorer_traces._index_file
    scanned = []

    def counted(directory, name):
        scanned.append(name)
        return original(directory, name)

    monkeypatch.setattr(scorer_traces, "_index_file", counted)
    result = diagnostics.prepare_diagnostics(
        [_repeat(slot, n) for n in range(1, 17)],
        source_root=trace.run_root,
        destination=tmp_path / "prepared",
    )
    assert result.included == 16
    assert scanned == ["original-measurement.pack", "original-measurement.s1.pack"]
