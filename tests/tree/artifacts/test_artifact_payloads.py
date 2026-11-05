"""Auxiliary publication uses the real artifact owner and ledger, never a tree copy."""

import hashlib
import json
import os
from dataclasses import replace

import pytest

from evolution.tree.artifacts import artifact_payloads as data
from evolution.tree.artifacts import artifact_projections
from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCorruptionError,
)
from evolution.tree.artifacts.artifact_schema import ARTIFACT_FAMILY
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.storage.envelope import SchemaError
from tests.tree.artifacts.test_artifact_store import create, events


def prepared(tmp_path, store, *, purpose="comparison", value=None):
    directory = tmp_path / "prepared"
    directory.mkdir()
    raw = canonical_json({"weights": [[1, 2], [1, 2]]} if value is None else value)
    (directory / "payload.json").write_bytes(raw)
    source = data.AuxiliarySourceReference(
        1, "journal_record", "campaign-run", "campaign.jsonl#bytes=0:123", "a" * 64
    )
    return data.describe_auxiliary(directory, purpose=purpose, declaration_ref=source), directory


def test_publication_has_its_own_identity_and_does_not_move_champion(tmp_path):
    with create(tmp_path) as store:
        declaration, directory = prepared(tmp_path, store)
        champion = store.champion()
        reference = store.publish_auxiliary(declaration, directory)
        assert reference.declaration == declaration
        assert reference.locator == "artifacts/auxiliary/" + declaration.content_digest
        assert reference.publication_ref.kind == "ledger_record"
        assert reference.publication_ref.run_id == store.run_id
        assert store.champion() == champion
        assert events(store)[-2:] == ["auxiliary_publish_started", "auxiliary_publish_finished"]
        reference.verify(tmp_path / "run")
        assert store.publish_auxiliary(declaration, directory) == reference
        assert events(store).count("auxiliary_publish_started") == 1
        assert artifact_projections.inspect_auxiliary(tmp_path / "run", verify=True) == (reference,)
        assert not (tmp_path / "run" / reference.locator / "payload.json").stat().st_mode & 0o222


def test_declaration_is_immutable_and_bounded_independently_of_payload(tmp_path):
    with create(tmp_path) as store:
        declaration, directory = prepared(tmp_path, store, value=list(range(10000)))
        assert len(canonical_json(declaration.to_payload())) < 2048
        assert declaration.byte_count == (directory / "payload.json").stat().st_size
        with pytest.raises((AttributeError, TypeError)):
            declaration.files[0].byte_count = 0
        changed = declaration.to_payload()
        changed["files"][0]["byte_count"] += 1
        assert changed != declaration.to_payload()
        store.publish_auxiliary(declaration, directory)
        other = replace(
            declaration,
            declaration_ref=replace(declaration.declaration_ref, content_digest="b" * 64),
        )
        before = store.ledger.snapshot().sequence
        with pytest.raises(ArtifactAdmissionRefused, match="auxiliary_declaration_conflict"):
            store.publish_auxiliary(other, directory)
        assert store.ledger.snapshot().sequence == before
        assert store.champion().artifact_id == "v0"


@pytest.mark.parametrize(
    "mutation", ["family", "purpose", "unknown", "null", "traversal", "size", "digest"]
)
def test_closed_start_schema(tmp_path, mutation):
    with create(tmp_path) as store:
        declaration, _ = prepared(tmp_path, store)
        payload = declaration.to_payload()
        if mutation == "family":
            payload["family_version"] = 14
        elif mutation == "purpose":
            payload["purpose"] = "tree"
        elif mutation == "unknown":
            payload["extra"] = 1
        elif mutation == "null":
            payload["declaration_ref"] = None
        elif mutation == "traversal":
            payload["files"][0]["name"] = "../payload.json"
        elif mutation == "size":
            payload["files"][0]["byte_count"] = True
        else:
            payload["content_digest"] = "f" * 64
        with pytest.raises((SchemaError, ValueError)):
            ARTIFACT_FAMILY.validate_payload("auxiliary_publish_started", payload)


@pytest.mark.parametrize("tamper", ["bytes", "missing", "extra", "symlink", "hardlink"])
def test_committed_tampering_is_corruption(tmp_path, tamper):
    with create(tmp_path) as store:
        declaration, directory = prepared(tmp_path, store)
        reference = store.publish_auxiliary(declaration, directory)
        root = tmp_path / "run" / reference.locator
        root.chmod(0o700)
        path = root / "payload.json"
        if tamper == "bytes":
            path.chmod(0o600)
            path.write_bytes(b"changed")
        elif tamper == "missing":
            path.unlink()
        elif tamper == "extra":
            (root / "extra").write_bytes(b"x")
        elif tamper == "symlink":
            path.unlink()
            path.symlink_to(directory / "payload.json")
        else:
            os.link(path, tmp_path / "alias")
        with pytest.raises(ArtifactCorruptionError):
            artifact_projections.inspect_auxiliary(tmp_path / "run", verify=True)
        with pytest.raises(ArtifactCorruptionError):
            store.publish_auxiliary(declaration, directory)
        assert store.stopped == "artifact_corrupted"


def test_diagnostic_directory_keeps_format_digest_and_separate_gap_bound(tmp_path):
    with create(tmp_path) as store:
        directory = tmp_path / "diagnostics"
        directory.mkdir()
        index = canonical_json({"entries": [], "format": "baseline-diagnostic-bundle/1"})
        (directory / "index.json").write_bytes(index)
        (directory / "payload").write_bytes(b"baseline-diagnostic-bundle/1\n\nend\n")
        (directory / "gaps.json").write_bytes(canonical_json({"included": 0}))
        source = data.AuxiliarySourceReference(
            1, "journal_record", "campaign-run", "campaign.jsonl#bytes=0:123", "a" * 64
        )
        declaration = data.describe_auxiliary(
            directory, purpose="baseline_diagnostics", declaration_ref=source
        )
        assert declaration.content_digest == hashlib.sha256(index).hexdigest()
        assert declaration.byte_count == len(index) + (directory / "payload").stat().st_size
        reference = store.publish_auxiliary(declaration, directory)
        assert reference.locator == "diagnostic-imports/" + declaration.content_digest
        assert (
            json.loads((tmp_path / "run" / reference.locator / "index.json").read_bytes())[
                "entries"
            ]
            == []
        )
        reference.verify(tmp_path / "run")


def test_publication_reference_round_trips_closed_shape(tmp_path):
    with create(tmp_path) as store:
        declaration, directory = prepared(tmp_path, store)
        reference = store.publish_auxiliary(declaration, directory)
        payload = reference.to_payload()
        assert artifact_projections.AuxiliaryPublication.from_payload(payload) == reference
        payload["extra"] = "refuse"
        with pytest.raises(ValueError):
            artifact_projections.AuxiliaryPublication.from_payload(payload)


def test_diagnostic_cap_is_distinct_from_comparison_size():
    source = data.AuxiliarySourceReference(
        1, "journal_record", "campaign-run", "campaign.jsonl#bytes=0:1", "a" * 64
    )
    # Descriptor validation has no arbitrary comparison-size cap. This does not
    # allocate a multi-gigabyte test file or assert that absent content is published.
    big = data.AuxiliaryFile("payload.json", 10**12, "b" * 64)
    assert (
        data.AuxiliaryDeclaration(
            15, "comparison", "comparison-data/1", big.digest, (big,), source
        ).byte_count
        == 10**12
    )
    files = (
        data.AuxiliaryFile("index.json", 2, "b" * 64),
        data.AuxiliaryFile("payload", 256 * 1024 * 1024 - 2, "c" * 64),
        data.AuxiliaryFile("gaps.json", 64 * 1024, "d" * 64),
    )
    valid = data.AuxiliaryDeclaration(
        15, "baseline_diagnostics", "baseline-diagnostic-bundle/1", "b" * 64, files, source
    )
    assert valid.byte_count == 256 * 1024 * 1024
    for index in (1, 2):
        changed = list(files)
        changed[index] = replace(changed[index], byte_count=changed[index].byte_count + 1)
        with pytest.raises(ValueError):
            replace(valid, files=tuple(changed))


def test_lifecycle_requires_original_declaration_terminal_and_single_publication(tmp_path):
    from evolution.tree.artifacts.artifact_schema import ArtifactLifecycle
    from evolution.tree.storage.envelope import Envelope

    with create(tmp_path) as store:
        declaration, _ = prepared(tmp_path, store)
        life = ArtifactLifecycle()
        for record in store.ledger.snapshot().records:
            life.accept(record)

        def record(event, payload, operation="aux"):
            return Envelope.from_payload(
                dict(
                    schema_version=1,
                    run_id=store.run_id,
                    sequence=life.sequence + 1,
                    timestamp="2026-10-18T00:00:00Z",
                    event=event,
                    operation_id=operation,
                    payload=payload,
                ),
                family=ARTIFACT_FAMILY,
            )

        reserved = life.reserved_records
        start = record("auxiliary_publish_started", declaration.to_payload())
        transition = life.prepare(start)
        assert not life.auxiliary_declared
        transition.commit()
        assert life.reserved_records == reserved + 1
        for wrong in ("0" * 64, "f" * 64):
            with pytest.raises(ValueError):
                life.accept(record("auxiliary_publish_finished", {"declaration_digest": wrong}))
        with pytest.raises(ValueError):
            life.accept(record("auxiliary_publish_started", declaration.to_payload(), "other"))
        life.accept(record("operation_interrupted", {"operation": "auxiliary_publish"}))
        assert life.reserved_records == reserved
        altered = replace(
            declaration,
            declaration_ref=replace(declaration.declaration_ref, content_digest="c" * 64),
        )
        with pytest.raises(ValueError):
            life.accept(record("auxiliary_publish_started", altered.to_payload(), "retry"))
        life.accept(record("auxiliary_publish_started", declaration.to_payload(), "retry"))
        life.accept(
            record(
                "auxiliary_publish_finished", {"declaration_digest": declaration.digest}, "retry"
            )
        )
        assert life.auxiliary_published[declaration.locator].payload == declaration.to_payload()
        with pytest.raises(ValueError):
            life.accept(
                record(
                    "auxiliary_publish_finished",
                    {"declaration_digest": declaration.digest},
                    "retry",
                )
            )
        with pytest.raises(ValueError):
            life.accept(record("auxiliary_publish_started", declaration.to_payload(), "third"))
        assert life.champion_id == "v0"


def test_content_can_be_described_before_caller_durable_declaration(tmp_path):
    directory = tmp_path / "prepared"
    directory.mkdir()
    (directory / "payload.json").write_bytes(canonical_json({"slots": []}))
    files = data.describe_auxiliary_files(directory, purpose="comparison")
    # The campaign binds these content facts first. Its later durable reference
    # then enters AuxiliaryDeclaration; no self-reference or placeholder is needed.
    assert files[0].digest == hashlib.sha256((directory / "payload.json").read_bytes()).hexdigest()
    source = data.AuxiliarySourceReference(
        1, "journal_record", "campaign-run", "campaign.jsonl#bytes=0:123", "a" * 64
    )
    declaration = data.AuxiliaryDeclaration(
        15, "comparison", "comparison-data/1", files[0].digest, files, source
    )
    assert (
        data.describe_auxiliary(directory, purpose="comparison", declaration_ref=source)
        == declaration
    )


def test_publication_rejects_invalid_values_with_the_schema_error():
    from evolution.tree.storage.envelope import SchemaError

    with pytest.raises(SchemaError):
        artifact_projections.AuxiliaryPublication(None, None)
