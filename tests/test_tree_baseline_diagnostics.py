"""Pure diagnostic preparation never changes independently admitted scalar rows."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evolution.tree import baseline_diagnostics as diagnostics
from evolution.tree.baseline_import import record_digest
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.journal_schema import S1_FAMILY
from tests.scorer_trace_packs import output_record, pack_record, request_record, write_pack
from tests.tree.roles.editor.test_editor_evidence import _Trace


def fixture_slot(tmp_path: Path, *, layout: str = "directory"):
    trace = _Trace(tmp_path, turns=0)
    records = trace.journal.snapshot().records
    rollout = next(r for r in records if r.event == "rollout_finished")
    started = next(r for r in records if r.event == "scoring_started")
    finished = next(r for r in records if r.event == "scoring_finished")
    source = dict(
        run_id="original-run",
        measurement_id="original-measurement",
        identity_digest=trace.identity.digest,
        candidate_digest=trace.baseline.digest,
        retry_ordinal=0,
        rollout_digest=record_digest(rollout),
        scoring_digest=record_digest(finished),
        condition_fields=["target"],
    )
    admitted = dict(
        case_id="c",
        repeat=1,
        sample_id="destination-sample",
        requested_seed=42,
        metrics={"accuracy": 1.0},
        critical_pass=None,
        source=source,
    )
    projection = dict(environment=dict(kind="static", digest="envdigest"), scorer=trace.scorer)
    slot = diagnostics.DiagnosticSlot(
        admitted=admitted,
        source_prefix_digest="a" * 64,
        rollout=rollout,
        started=started,
        finished=finished,
        case=trace.case,
        candidate=trace.baseline.candidate_view(),
        source_projection=projection,
        target_projection=projection,
    )
    if layout == "pack":
        request = (trace.directory / "request.json").read_bytes()
        stdout = (trace.directory / "stdout").read_bytes()
        write_pack(
            trace.run_root / "scorer-traces",
            "original-measurement.pack",
            [
                pack_record("original-measurement"),
                request_record(trace.request_id, request),
                output_record(trace.request_id, stdout, b"STDERR-CANARY"),
            ],
        )
        shutil.rmtree(trace.directory)
    return trace, slot


def index(bundle):
    return json.loads(bundle.index_path.read_bytes())


@pytest.mark.parametrize("layout", ["directory", "pack"])
def test_preparation_is_private_deterministic_self_contained_and_scalar_independent(
    tmp_path, layout
):
    trace, slot = fixture_slot(tmp_path, layout=layout)
    before = canonical_json(slot.admitted)
    first = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "one"
    )
    second = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "two"
    )
    assert (
        first.digest == second.digest == hashlib.sha256(first.index_path.read_bytes()).hexdigest()
    )
    assert first.payload_path.read_bytes() == second.payload_path.read_bytes()
    assert first.index_path.read_bytes() == second.index_path.read_bytes()
    assert first.gaps == second.gaps
    assert first.byte_count == first.index_path.stat().st_size + first.payload_path.stat().st_size
    assert first.included == 1
    entry = index(first)["entries"][0]
    assert entry["destination"] == {"case_id": "c", "repeat": 1, "sample_id": "destination-sample"}
    assert entry["source"] == json.loads(canonical_json(slot.admitted["source"]))
    assert entry["validation"] == {
        "score_provenance": "journal_bound",
        "request": "authenticated",
        "stdout": "captured_at_import",
        "scalar": "matched",
        "projection": "matched",
    }
    assert b"STDERR-CANARY" not in first.payload_path.read_bytes()
    for part in (first.index_path, first.payload_path, first.gaps_path):
        assert part.stat().st_mode & 0o777 == 0o600
    assert first.directory.stat().st_mode & 0o777 == 0o700
    shutil.rmtree(trace.run_root)
    payload = first.payload_path.read_bytes()
    for name in ("request", "stdout"):
        section = entry["sections"][name]
        raw = payload[section["offset"] : section["offset"] + section["length"]]
        assert hashlib.sha256(raw).hexdigest() == section["sha256"]
        assert json.loads(raw)
    assert canonical_json(slot.admitted) == before


@pytest.mark.parametrize(
    "change,reason",
    [
        ("request_id", "request_mismatch"),
        ("request_hash", "request_mismatch"),
        ("corpus", "request_mismatch"),
        ("metrics", "scalar_mismatch"),
        ("critical", "scalar_mismatch"),
        ("projection", "projection_mismatch"),
        ("scorer", "projection_mismatch"),
        ("source", "source_mismatch"),
        ("malformed", "stdout_invalid"),
        ("missing", "missing"),
    ],
)
def test_invalid_source_is_a_gap_without_rewriting_scores(tmp_path, change, reason):
    trace, slot = fixture_slot(tmp_path)
    if change.startswith("request") or change == "corpus":
        doc = json.loads((trace.directory / "request.json").read_bytes())
        if change == "request_id":
            doc["trace_id"] = "other"
        elif change == "request_hash":
            doc["input_sha256"] = "0" * 64
        else:
            doc["input"]["oracle"] = {"tampered": True}
            doc["input_sha256"] = hashlib.sha256(canonical_json(doc["input"])).hexdigest()
        (trace.directory / "request.json").write_bytes(canonical_json(doc))
        # Failed authentication must precede reading this unsafe stdout.
        (trace.directory / "stdout").unlink()
        (trace.directory / "stdout").symlink_to(tmp_path / "outside")
    elif change in ("metrics", "critical"):
        doc = json.loads((trace.directory / "stdout").read_bytes())
        doc["metrics" if change == "metrics" else "critical_pass"] = (
            {"accuracy": 0.0} if change == "metrics" else False
        )
        (trace.directory / "stdout").write_bytes(canonical_json(doc))
    elif change in ("projection", "scorer"):
        projection = json.loads(canonical_json(slot.target_projection))
        projection["environment" if change == "projection" else "scorer"]["digest"] = "changed"
        slot = dataclasses.replace(slot, target_projection=projection)
    elif change == "source":
        admitted = json.loads(canonical_json(slot.admitted))
        admitted["source"]["scoring_digest"] = "0" * 64
        slot = dataclasses.replace(slot, admitted=admitted)
    elif change == "malformed":
        (trace.directory / "stdout").write_bytes(b'{"metrics": {}, "unknown": true}')
    else:
        shutil.rmtree(trace.directory)
    before = canonical_json(slot.admitted)
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.included == 0
    assert index(result)["entries"] == []
    assert result.gaps.counts[reason] == 1
    assert canonical_json(slot.admitted) == before


def test_transitive_source_location_preserves_original_execution(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    shutil.move(str(trace.run_root), sources / "original")
    slot = dataclasses.replace(slot, trace_run="original")
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=sources, destination=tmp_path / "prepared"
    )
    assert result.included == 1
    assert index(result)["entries"][0]["source"]["run_id"] == "original-run"
    assert "trace_run" not in index(result)["entries"][0]


@pytest.mark.parametrize("where", ["root", "ancestor", "request", "stdout"])
def test_never_follows_source_symlinks(tmp_path, where):
    trace, slot = fixture_slot(tmp_path)
    root = trace.run_root
    if where == "root":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif where == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias / "run"
    else:
        path = trace.directory / ("request.json" if where == "request" else "stdout")
        moved = tmp_path / "moved"
        path.rename(moved)
        path.symlink_to(moved)
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=root, destination=tmp_path / "prepared"
    )
    assert result.included == 0
    assert result.gaps.counts["unsafe_entry"] == 1


def test_input_mappings_are_frozen_and_caller_aliases_cannot_change_preparation(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    with pytest.raises(TypeError):
        slot.admitted["source"]["run_id"] = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        slot.trace_run = "changed"
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    with pytest.raises(TypeError):
        result.gaps.counts["missing"] = 99


def test_metadata_and_sections_reconstruct_transitive_input_without_original_journals(tmp_path):
    from evolution.tree.storage.envelope import Envelope
    from tests.scorer_trace_packs import write_directory_trace

    trace, slot = fixture_slot(tmp_path)
    first = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "first"
    )
    entry = index(first)["entries"][0]
    retained = {}
    with first.payload_path.open("rb") as stream:
        for name in ("request", "stdout"):
            section = entry["sections"][name]
            stream.seek(section["offset"])
            retained[name] = stream.read(section["length"])
    shutil.rmtree(trace.run_root)
    shutil.rmtree(tmp_path / "journal")
    local = tmp_path / "locally-resolved"
    write_directory_trace(local / "scorer-traces", trace.request_id, **retained)
    # Task11 verifies committed-bundle authority before providing these local
    # sections. This test checks only the pure representation and auth handoff.
    reconstructed = diagnostics.DiagnosticSlot(
        admitted=slot.admitted,
        source_prefix_digest=entry["source_prefix_digest"],
        rollout=Envelope.from_payload(entry["records"]["rollout_finished"], family=S1_FAMILY),
        started=Envelope.from_payload(entry["records"]["scoring_started"], family=S1_FAMILY),
        finished=Envelope.from_payload(entry["records"]["scoring_finished"], family=S1_FAMILY),
        case=trace.case,
        candidate=trace.baseline.candidate_view(),
        source_projection=entry["projection"],
        target_projection=slot.target_projection,
    )
    second = diagnostics.prepare_diagnostics(
        [reconstructed], source_root=local, destination=tmp_path / "second"
    )
    assert second.digest == first.digest
    assert second.payload_path.read_bytes() == first.payload_path.read_bytes()


def test_source_scoring_envelopes_from_different_journals_are_unavailable(tmp_path):
    from evolution.tree.storage.envelope import Envelope

    trace, slot = fixture_slot(tmp_path)
    changed = slot.started.to_payload()
    changed["run_id"] = "foreign-journal"
    slot = dataclasses.replace(slot, started=Envelope.from_payload(changed, family=S1_FAMILY))
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.included == 0
    assert result.gaps.counts["source_mismatch"] == 1


@pytest.mark.parametrize("trace_run", ["../outside", "/absolute", "a/../b", "a//b", "a\\b"])
def test_trace_locations_cannot_escape_source_root(tmp_path, trace_run):
    trace, slot = fixture_slot(tmp_path)
    slot = dataclasses.replace(slot, trace_run=trace_run)
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.gaps.counts["unsafe_entry"] == 1


def test_matching_scalars_do_not_claim_historical_stdout_authentication(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    first = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "first"
    )
    output = json.loads((trace.directory / "stdout").read_bytes())
    output["verdicts"] = [{"criterion": "answer", "passed": False, "note": "PRIVATE-VERDICT"}]
    (trace.directory / "stdout").write_bytes(canonical_json(output))
    second = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "second"
    )
    assert second.included == 1
    assert first.digest != second.digest
    assert index(second)["entries"][0]["validation"]["stdout"] == "captured_at_import"


def test_existing_destination_is_never_overwritten(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    destination = tmp_path / "prepared"
    destination.mkdir()
    sentinel = destination / "index.json"
    sentinel.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        diagnostics.prepare_diagnostics([slot], source_root=trace.run_root, destination=destination)
    assert sentinel.read_bytes() == b"existing"


def test_stderr_is_never_read_even_when_it_is_an_unsafe_entry(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    path = trace.directory / "stderr"
    path.unlink()
    path.symlink_to(tmp_path / "absent-private-stderr")
    result = diagnostics.prepare_diagnostics(
        [slot], source_root=trace.run_root, destination=tmp_path / "prepared"
    )
    assert result.included == 1
    assert set(index(result)["entries"][0]["sections"]) == {"request", "stdout"}


@pytest.mark.parametrize("field", ["public", "oracle"])
def test_malformed_copied_case_content_is_a_gap_before_stdout_and_later_slots_continue(
    tmp_path, field
):
    malformed_trace, malformed = fixture_slot(tmp_path / "malformed")
    valid_trace, valid = fixture_slot(tmp_path / "valid")
    malformed = dataclasses.replace(malformed, trace_run="malformed/run")
    valid_row = json.loads(canonical_json(valid.admitted))
    valid_row.update(repeat=2, sample_id="later-valid-sample")
    valid = dataclasses.replace(valid, admitted=valid_row, trace_run="valid/run")
    rows_before = [canonical_json(slot.admitted) for slot in (malformed, valid)]

    request_input = dict(malformed_trace.input)
    request_input[field] = {"malformed": float("nan")}
    # Ordinary JSON hashing matches the existing request check; canonical
    # corpus authentication must reject this non-finite copied content.
    malformed_trace.write_request(request_input)
    stdout = malformed_trace.directory / "stdout"
    stdout.unlink()
    stdout.symlink_to(tmp_path / "must-not-read-stdout")

    result = diagnostics.prepare_diagnostics(
        [valid, malformed], source_root=tmp_path, destination=tmp_path / "prepared"
    )

    assert result.included == 1
    assert result.gaps.counts["request_mismatch"] == 1
    assert sum(result.gaps.counts.values()) == 1
    entries = index(result)["entries"]
    assert len(entries) == 1
    assert entries[0]["destination"] == {
        "case_id": "c",
        "repeat": 2,
        "sample_id": "later-valid-sample",
    }
    section = entries[0]["sections"]["stdout"]
    with result.payload_path.open("rb") as stream:
        stream.seek(section["offset"])
        assert stream.read(section["length"]) == (valid_trace.directory / "stdout").read_bytes()
    assert [canonical_json(slot.admitted) for slot in (malformed, valid)] == rows_before
