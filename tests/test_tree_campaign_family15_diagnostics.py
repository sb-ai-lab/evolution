"""Committed diagnostic evidence remains local and scalar-independent."""

import dataclasses
import json
import shutil

import pytest

from evolution.tree import baseline_diagnostics as diagnostics
from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError
from evolution.tree.artifacts.artifact_payloads import describe_auxiliary_files
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.execution.journal_schema import S1_FAMILY
from tests.campaign_fakes import LifecycleFixture
from tests.family15_fakes import family15_created, v16_document
from tests.test_tree_baseline_diagnostics import fixture_slot
from tests.tree.artifacts.test_artifact_store import create
from tests.tree.manifest.test_manifest_v15 import load

pytestmark = pytest.mark.slow


def committed(tmp_path, *, alternating_origins=False):
    source = tmp_path / "source"
    source.mkdir()
    trace, slot = fixture_slot(source)
    slots = [slot]
    if alternating_origins:
        from evolution.tree.baseline_import import record_digest
        from evolution.tree.storage.envelope import Envelope
        from tests.tree.roles.editor.test_editor_evidence import _Trace

        trace.journal.close()
        trace = _Trace(source / "multiple", turns=0, samples=3)
        records = trace.journal.snapshot().records
        slots = []
        for repeat, origin in enumerate(("origin-a", "origin-b", "origin-a"), 1):
            envelopes = {
                r.event: Envelope.from_payload(
                    {**r.to_payload(), "run_id": origin}, family=S1_FAMILY
                )
                for r in records
                if r.payload.get("sample_id") == f"s{repeat}"
                and r.event in {"rollout_finished", "scoring_started", "scoring_finished"}
            }
            admitted = json.loads(canonical_json(slot.admitted))
            admitted.update(repeat=repeat, sample_id=f"destination-{repeat}")
            admitted["source"].update(
                measurement_id=origin,
                rollout_digest=record_digest(envelopes["rollout_finished"]),
                scoring_digest=record_digest(envelopes["scoring_finished"]),
            )
            slots.append(
                dataclasses.replace(
                    slot,
                    admitted=admitted,
                    rollout=envelopes["rollout_finished"],
                    started=envelopes["scoring_started"],
                    finished=envelopes["scoring_finished"],
                )
            )
    prepared = diagnostics.prepare_diagnostics(
        slots, source_root=trace.run_root, destination=tmp_path / "bundle"
    )
    policy = load(tmp_path, v16_document()).workflow.campaign
    with create(tmp_path) as store:
        life = LifecycleFixture.__new__(LifecycleFixture)
        life._initialize(
            [
                dict(
                    schema_version=1,
                    run_id="campaign-run",
                    sequence=1,
                    timestamp="2026-10-18T00:00:00Z",
                    event="campaign_created",
                    operation_id=None,
                    payload=family15_created(policy),
                )
            ],
            15,
        )
        declaration = dict(
            family_version=15,
            purpose="baseline_diagnostics",
            format=diagnostics.FORMAT,
            content_digest=prepared.digest,
            files=[
                f.to_payload()
                for f in describe_auxiliary_files(
                    prepared.directory, purpose="baseline_diagnostics"
                )
            ],
        )
        seq = life.accept("auxiliary_payload_declared", **declaration)
        (tmp_path / "run" / "campaign.jsonl").write_bytes(
            b"".join(r.encode() for r in life.envelopes)
        )
        bound = {**declaration, "declaration_ref": life.reference(seq)}
        publication = diagnostics.commit_diagnostics(
            prepared, run_root=tmp_path / "run", declaration=bound, store=store
        )
        assert (
            diagnostics.commit_diagnostics(
                prepared, run_root=tmp_path / "run", declaration=bound, store=store
            )
            == publication
        )
    return trace, slot, prepared, publication


def test_committed_slot_and_existing_projection_trace_survive_source_loss(tmp_path):
    trace, slot, prepared, publication = committed(tmp_path)
    wanted = (trace.directory / "stdout").read_bytes()
    trace.journal.close()
    shutil.rmtree(tmp_path / "source")
    record = diagnostics.resolve_diagnostic_slot(
        tmp_path / "run", bundle_digest=prepared.digest, case_id="c", repeat=1
    )
    assert record.stdout == wanted
    assert record.envelope("scoring_started") == slot.started
    assert record.entry["validation"]["stdout"] == "captured_at_import"
    assert (
        diagnostics.resolve_diagnostic_slot(
            tmp_path / "run", bundle_digest=prepared.digest, case_id="absent", repeat=1
        )
        is None
    )
    with diagnostics.DiagnosticTraceReader(tmp_path / "run") as reader:
        projected = reader.read(
            trace.request_id,
            measurement_id=slot.started.to_payload()["run_id"],
            sections=("request", "stdout"),
        )
        assert projected.request == record.request and projected.stdout == record.stdout
        assert projected.stderr is None
        assert projected.location == publication.locator


@pytest.mark.parametrize("part", ["index.json", "payload", "gaps.json"])
@pytest.mark.parametrize("change", ["missing", "changed", "symlink"])
def test_committed_corruption_never_becomes_absent_slot(tmp_path, part, change):
    trace, _, prepared, publication = committed(tmp_path)
    trace.journal.close()
    directory = tmp_path / "run" / publication.locator
    directory.chmod(0o700)
    path = directory / part
    if change == "changed":
        path.chmod(0o600)
        path.write_bytes(b"changed")
    else:
        path.unlink()
        if change == "symlink":
            path.symlink_to(prepared.directory / part)
    with pytest.raises(ArtifactCorruptionError):
        diagnostics.resolve_diagnostic_slot(
            tmp_path / "run", bundle_digest=prepared.digest, case_id="absent", repeat=1
        )


def test_missing_original_descriptors_keep_all_admitted_slots_and_bounded_gaps(tmp_path):
    trace, slot = fixture_slot(tmp_path)
    slots = [
        diagnostics.UnavailableDiagnosticSlot({**slot.admitted, "case_id": f"c{i:04d}"}, "missing")
        for i in range(100)
    ]
    before = [canonical_json(s.admitted) for s in slots]
    bundle = diagnostics.prepare_diagnostics(
        slots, source_root=trace.run_root, destination=tmp_path / "all-missing"
    )
    assert bundle.gaps.scheduled == 100 and bundle.included == 0
    assert bundle.gaps.counts["missing"] == 100 and len(bundle.gaps.samples) == 64
    assert json.loads(bundle.index_path.read_bytes())["entries"] == []
    assert before == [canonical_json(s.admitted) for s in slots]
    mixed = diagnostics.prepare_diagnostics(
        [slot, *slots], source_root=trace.run_root, destination=tmp_path / "mixed"
    )
    assert mixed.gaps.scheduled == 101 and mixed.included == 1
    assert mixed.gaps.counts["missing"] == 100
    trace.journal.close()


async def linked_run(tmp_path, monkeypatch, source, *, stop="after_grouping_finished", **options):
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family15_gate import gate_run

    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.5')
    return await gate_run(
        tmp_path,
        monkeypatch,
        stop=stop,
        scorer=options.pop("scorer", scorer),
        campaign_options={"attempts_per_round": 1},
        linked_from=(source, options.pop("seed", source.parent / "seed")),
        **options,
    )


async def failing_source(tmp_path, monkeypatch):
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family15_gate import gate_run

    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.5')
    return await gate_run(
        tmp_path,
        monkeypatch,
        stop="result",
        scorer=scorer,
        campaign_options={"attempts_per_round": 1},
    )


async def test_real_inherited_verdict_only_failure_reaches_grouping_without_new_baseline_calls(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import _records

    source = await failing_source(tmp_path / "source", monkeypatch)
    run = await linked_run(tmp_path / "successor", monkeypatch, source)
    records = _records(run / "campaign.jsonl")
    imported = next(r["payload"] for r in records if r["event"] == "diagnostic_import_declared")
    assert imported["scheduled"] == imported["included"] == 2
    baseline = next(
        r["payload"]["measurement_id"] for r in records if r["event"] == "baseline_bound"
    )
    rows = _records(run / "measurements" / baseline / "events.jsonl")
    assert sum(r["event"] == "sample_imported" for r in rows) == 2
    assert not any(r["event"] in {"rollout_started", "scoring_started"} for r in rows)
    assert any(r["event"] == "grouping_finished" for r in records)
    from evolution.tree.campaign_projections import verify_evidence

    assert verify_evidence(run)["mismatches"] == []
    from evolution.tree.execution.corpus import load_reflection_corpus

    proven = verify_evidence(run, load_reflection_corpus(tmp_path / "successor/corpus.jsonl").cases)
    assert proven["mismatches"] == []
    assert proven["corpus"] == "proven"


async def test_missing_diagnostics_preserve_scalar_import_and_refresh_zero_stall(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import _records

    source = await failing_source(tmp_path / "source", monkeypatch)
    shutil.rmtree(source / "scorer-traces")
    run = await linked_run(tmp_path / "successor", monkeypatch, source, stop="result")
    records = _records(run / "campaign.jsonl")
    diagnostic = next(r["payload"] for r in records if r["event"] == "diagnostic_import_declared")
    assert diagnostic["scheduled"] == 2 and diagnostic["included"] == 0
    assert diagnostic["sufficiency"] == "unavailable"
    assert records[0]["payload"]["identity"]["policy"]["baseline_refresh_after_rounds"] == 0
    samples = [r for p in (run / "measurements").glob("*/events.jsonl") for r in _records(p)]
    assert sum(r["event"] == "sample_imported" for r in samples) == 2
    assert not any(r["event"] in {"scoring_started", "rollout_started"} for r in samples)
    assert not any(r["event"] == "attempt_opened" for r in records)
    assert verify_evidence(run)["mismatches"] == []


async def test_same_run_completion_then_two_local_transitive_links(tmp_path, monkeypatch):
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import TWO_CASE_SCORER, _records
    from tests.test_tree_campaign_family15_gate import gate_run

    scorer = TWO_CASE_SCORER.replace(
        '"score": 1.0 if passed else 0.0', '"score": 0.75 if "edited" in skill else 0.5'
    ).replace('"passed": passed', '"passed": bool(payload["oracle"].get("pass"))')
    source = await gate_run(
        tmp_path / "origin",
        monkeypatch,
        stop="result",
        scorer=scorer,
        campaign_options={"attempts_per_round": 1},
    )
    records = _records(source / "campaign.jsonl")
    assert any(r["event"] == "candidate_completion_completed" for r in records)
    seed = tmp_path / "selected-seed"
    shutil.copytree(source / "artifacts/champions/v1", seed)
    carrier = await linked_run(
        tmp_path / "carrier", monkeypatch, source, stop="result", scorer=scorer, seed=seed
    )
    declaration = next(
        r["payload"]
        for r in _records(carrier / "campaign.jsonl")
        if r["event"] == "diagnostic_import_declared"
    )
    assert declaration["included"] == 2
    source.rename(source.with_name("origin-moved"))
    destination = await linked_run(
        tmp_path / "destination",
        monkeypatch,
        carrier,
        stop="after_baseline_bound",
        hard=True,
        scorer=scorer,
        seed=seed,
    )
    carrier.rename(carrier.with_name("carrier-moved"))
    await linked_run(tmp_path / "destination", monkeypatch, carrier, scorer=scorer, seed=seed)
    assert verify_evidence(destination)["mismatches"] == []
    own = next(
        r["payload"]
        for r in _records(destination / "campaign.jsonl")
        if r["event"] == "diagnostic_import_declared"
    )
    assert own["included"] == 2
    samples = _records(destination / "measurements" / own["measurement_id"] / "events.jsonl")
    assert sum(r["event"] == "sample_imported" for r in samples) == 2
    assert not any(r["event"] in {"rollout_started", "scoring_started"} for r in samples)
    for bundle in (destination / "diagnostic-imports").glob("*/index.json"):
        entries = json.loads(bundle.read_bytes())["entries"]
        assert all(e["source"]["measurement_id"].startswith("m-r01-a") for e in entries)


@pytest.mark.parametrize(
    "change", ["origin", "labels", "offset", "request", "input", "scorer", "header", "footer"]
)
async def test_audit_rejects_forged_semantics_even_with_valid_publication_bytes(
    tmp_path, monkeypatch, change
):
    import hashlib

    from evolution.tree.campaign_projections import verify_evidence

    source = await failing_source(tmp_path / "source", monkeypatch)
    original = diagnostics.prepare_import_diagnostics

    def forged(*args, **kwargs):
        prepared = original(*args, **kwargs)
        document = json.loads(prepared.index_path.read_bytes())
        entry = document["entries"][0]
        if change == "origin":
            entry["source"]["run_id"] = "forged-original-campaign"
        elif change == "labels":
            entry["validation"]["stdout"] = "historically_authenticated"
        elif change == "offset":
            entry["sections"]["request"]["offset"] += 1
        elif change == "request":
            entry["records"]["scoring_started"]["payload"]["score_request_id"] = "forged-request"
        elif change == "scorer":
            entry["records"]["scoring_started"]["payload"]["scorer"]["digest"] = "a" * 64
        elif change in {"header", "footer"}:
            payload = prepared.payload_path.read_bytes()
            if change == "header":
                payload = (
                    b"!" * len(diagnostics._PAYLOAD_HEADER)
                    + payload[len(diagnostics._PAYLOAD_HEADER) :]
                )
            else:
                payload = payload[: -len(diagnostics._PAYLOAD_FOOTER)] + b"!" * len(
                    diagnostics._PAYLOAD_FOOTER
                )
            prepared.payload_path.write_bytes(payload)
        else:
            payload = prepared.payload_path.read_bytes()
            rebuilt = diagnostics._PAYLOAD_HEADER
            for index, item in enumerate(document["entries"]):
                for name in ("request", "stdout"):
                    section = item["sections"][name]
                    raw = payload[section["offset"] : section["offset"] + section["length"]]
                    if index == 0 and name == "request":
                        request = json.loads(raw)
                        request["input"]["view"]["tampered"] = True
                        request["input_sha256"] = hashlib.sha256(
                            canonical_json(request["input"])
                        ).hexdigest()
                        raw = canonical_json(request)
                    item["sections"][name] = dict(
                        offset=len(rebuilt), length=len(raw), sha256=hashlib.sha256(raw).hexdigest()
                    )
                    rebuilt += raw
            prepared.payload_path.write_bytes(rebuilt + diagnostics._PAYLOAD_FOOTER)
        raw = canonical_json(document)
        prepared.index_path.write_bytes(raw)
        return dataclasses.replace(
            prepared,
            digest=hashlib.sha256(raw).hexdigest(),
            byte_count=len(raw) + prepared.payload_path.stat().st_size,
        )

    monkeypatch.setattr(diagnostics, "prepare_import_diagnostics", forged)
    run = await linked_run(
        tmp_path / "successor", monkeypatch, source, stop="after_baseline_bound", hard=True
    )
    from evolution.tree.execution.corpus import load_reflection_corpus

    corpus = load_reflection_corpus(tmp_path / "successor/corpus.jsonl").cases
    if change == "input":
        # Without full corpus tags/private, E can authenticate only the committed
        # preparation attestation and self-hash, not replay scoring_input_digest.
        assert verify_evidence(run)["mismatches"] == []
    findings = verify_evidence(run, corpus)["mismatches"]
    assert any(
        f["event"] == "diagnostic_import_declared" and f["code"] != "family15_audit_incomplete"
        for f in findings
    ), findings


def test_private_bulk_resolution_authenticates_once_and_faults_after_reader_creation(
    tmp_path, monkeypatch
):
    trace, slot, prepared, publication = committed(tmp_path)
    original = diagnostics._committed
    checked = []

    def counted(*args, **kwargs):
        checked.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(diagnostics, "_committed", counted)
    with diagnostics.DiagnosticTraceReader(tmp_path / "run") as reader:
        for _ in range(133):
            assert (
                reader.read(
                    trace.request_id,
                    measurement_id=slot.started.to_payload()["run_id"],
                    sections=("request", "stdout"),
                )
                is not None
            )
        assert len(checked) == 1
        directory = tmp_path / "run" / publication.locator
        directory.chmod(0o700)
        (directory / "payload").unlink()
        from evolution.tree.roles.editor.editor_evidence import read_trace

        with pytest.raises(ArtifactCorruptionError):
            read_trace(reader, slot.started, ("request",))
    trace.journal.close()


def test_import_preparation_authenticates_once_across_alternating_original_measurements(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    trace, slot, prepared, _ = committed(tmp_path, alternating_origins=True)
    entries = json.loads(prepared.index_path.read_bytes())["entries"]
    assert len(entries) == 3
    rows = [
        dict(
            **entry["destination"],
            source=entry["source"],
            requested_seed=42,
            metrics=entry["scalar"]["metrics"],
            critical_pass=entry["scalar"]["critical_pass"],
        )
        for entry in entries
    ]
    identity = dict(
        candidate_digest=slot.candidate.digest,
        schedule=[entry["destination"] for entry in entries],
        **entries[0]["projection"],
    )
    trace.journal.close()
    shutil.rmtree(trace.run_root)
    original = diagnostics._committed
    checked = []

    def counted(*args, **kwargs):
        checked.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(diagnostics, "_committed", counted)
    imported = diagnostics.prepare_import_diagnostics(
        SimpleNamespace(run_id="carrier", measurement_id="carrier-measurement"),
        rows,
        target=SimpleNamespace(to_payload=lambda: identity),
        source_root=tmp_path / "run",
        destination=tmp_path / "reimported",
        corpus=[slot.case],
        candidate=slot.candidate,
    )
    assert imported.included == 3
    assert imported.payload_path.read_bytes() == prepared.payload_path.read_bytes()
    assert [
        entry["source"]["measurement_id"]
        for entry in json.loads(imported.index_path.read_bytes())["entries"]
    ] == ["origin-a", "origin-b", "origin-a"]
    assert len(checked) == 1


def test_family15_link_support_preserves_old_directions_with_public_writer():
    from evolution.tree.campaign import _ADMITTED_CAMPAIGN_FAMILIES, _linkable_families
    from evolution.tree.evolve import CAMPAIGN_SCHEMAS

    # ADR-0046 / ADR-0032's 2026-10-20 amendment excludes family 13 from new links.
    assert _linkable_families(15) == (14, 15)
    assert _linkable_families(14) == (14,)
    assert 15 in _ADMITTED_CAMPAIGN_FAMILIES
    assert "evolution-v2-manifest/16" in CAMPAIGN_SCHEMAS


def test_request_only_local_lookup_does_not_read_stdout_and_cap_precedes_payload_read(
    tmp_path, monkeypatch
):
    from evolution.tree.execution.scorer_traces import ScorerTraceUnreadable

    trace, slot, prepared, publication = committed(tmp_path)
    with diagnostics.DiagnosticTraceReader(tmp_path / "run") as reader:
        index = json.loads(prepared.index_path.read_bytes())["entries"][0]
        path = tmp_path / "run" / publication.locator / "payload"
        path.chmod(0o600)
        # The committed owner was checked on construction. Later section reads
        # authenticate only their requested bytes, preserving request-first order.
        with path.open("r+b") as stream:
            stream.seek(index["sections"]["stdout"]["offset"])
            stream.write(b"!")
        found = reader.read(
            trace.request_id,
            measurement_id=slot.started.to_payload()["run_id"],
            sections=("request",),
        )
        assert found.request and found.stdout is None
        with pytest.raises(ArtifactCorruptionError):
            reader.read(
                trace.request_id,
                measurement_id=slot.started.to_payload()["run_id"],
                sections=("stdout",),
            )
        with pytest.raises(ScorerTraceUnreadable, match="too_large"):
            reader.read(
                trace.request_id,
                measurement_id=slot.started.to_payload()["run_id"],
                sections=("stdout",),
                max_section_bytes=1,
            )
    trace.journal.close()


async def test_legacy_carrier_with_vanished_original_keeps_scalars_and_explicit_gaps(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import TWO_CASE_SCORER, _records
    from tests.test_tree_campaign_family14_run import family14_run

    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.5')
    source, _ = await family14_run(
        tmp_path / "legacy-origin",
        monkeypatch,
        scorer=scorer,
        family14_settings={"stability": "stable"},
    )
    carrier, _ = await family14_run(
        tmp_path / "legacy-carrier",
        monkeypatch,
        scorer=scorer,
        family14_settings={"stability": "stable"},
        linked_from=(source, source.parent / "seed"),
    )
    assert any(
        r["event"] == "sample_imported"
        for p in (carrier / "measurements").glob("*/events.jsonl")
        for r in _records(p)
    )
    source.rename(source.with_name("original-gone"))
    run = await linked_run(tmp_path / "successor", monkeypatch, carrier, stop="result")
    declaration = next(
        r["payload"]
        for r in _records(run / "campaign.jsonl")
        if r["event"] == "diagnostic_import_declared"
    )
    assert declaration["scheduled"] == 2 and declaration["included"] == 0
    samples = [r for p in (run / "measurements").glob("*/events.jsonl") for r in _records(p)]
    assert sum(r["event"] == "sample_imported" for r in samples) == 2
    assert not any(r["event"] == "scoring_started" for r in samples)
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("part", ["index.json", "payload"])
@pytest.mark.parametrize("replacement", ["fifo", "hardlink"])
def test_post_index_unsafe_substitution_is_a_prompt_persistence_fault(tmp_path, part, replacement):
    import os

    trace, slot, _, publication = committed(tmp_path)
    with diagnostics.DiagnosticTraceReader(tmp_path / "run") as reader:
        directory = tmp_path / "run" / publication.locator
        directory.chmod(0o700)
        path = directory / part
        if replacement == "fifo":
            path.unlink()
            os.mkfifo(path)
        else:
            os.link(path, tmp_path / "second-name")
        with pytest.raises(ArtifactCorruptionError):
            reader.read(
                trace.request_id,
                measurement_id=slot.started.to_payload()["run_id"],
                sections=("request",),
            )
    trace.journal.close()


async def test_unfinished_family15_carrier_is_refused_before_successor_creation(
    tmp_path, monkeypatch
):
    from evolution.tree.evolve import EvolveRefused
    from tests.test_tree_campaign_family15_gate import gate_run

    source = await gate_run(
        tmp_path / "source", monkeypatch, stop="after_baseline_bound", hard=True
    )
    with pytest.raises(EvolveRefused, match="lineage_not_finished"):
        await linked_run(tmp_path / "successor", monkeypatch, source)
    assert not (tmp_path / "successor/run/campaign.jsonl").exists()
