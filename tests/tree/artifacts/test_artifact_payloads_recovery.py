"""The original auxiliary declaration survives every durable publication cut."""

import errno
import hashlib
import multiprocessing as mp
import os
import signal
from dataclasses import replace

import pytest

from evolution.tree.artifacts import artifact_payloads as data
from evolution.tree.artifacts import artifact_projections
from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCapacityError,
    ArtifactCorruptionError,
    ArtifactUnsafeError,
)
from evolution.tree.artifacts.artifact_ledger import inspect_ledger
from evolution.tree.artifacts.artifacts import ArtifactStore
from tests.tree.artifacts.test_artifact_payloads import prepared
from tests.tree.artifacts.test_artifact_store import POLICY, VR, create, events


def resume(tmp_path):
    return ArtifactStore.resume(tmp_path / "run", POLICY, version_resolver=VR)


def child_publish(tmp_path, declaration, source, cut):
    with resume(tmp_path) as store:
        store._hooks = {cut: lambda: os.kill(os.getpid(), signal.SIGKILL)}
        store.publish_auxiliary(declaration, source)


@pytest.mark.parametrize(
    "cut", ["after_auxiliary_started", "after_auxiliary_replace", "after_auxiliary_finished"]
)
def test_killed_publication_recovers_exactly_once(tmp_path, cut):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
    process = mp.get_context("spawn").Process(
        target=child_publish, args=(tmp_path, declaration, source, cut)
    )
    process.start()
    process.join(20)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("publication child did not reach its cut")
    assert process.exitcode == -signal.SIGKILL
    with resume(tmp_path) as store:
        if cut == "after_auxiliary_started":
            assert store.owed_auxiliary == (declaration,)
            assert any(
                a.startswith("auxiliary_source_required:") for a in store.last_recovery.actions
            )
            assert artifact_projections.inspect_auxiliary(tmp_path / "run") == ()
            result = store.publish_auxiliary(declaration, source)
        else:
            assert store.owed_auxiliary == ()
            result = artifact_projections.inspect_auxiliary(tmp_path / "run", verify=True)[0]
        assert result.declaration == declaration
        assert events(store).count("auxiliary_publish_finished") == 1
        sequence = store.ledger.snapshot().sequence
        assert store.publish_auxiliary(declaration, source) == result
        assert store.ledger.snapshot().sequence == sequence
    source.rename(tmp_path / "source_moved")
    with resume(tmp_path) as store:
        assert store.owed_auxiliary == ()
        assert store.ledger.snapshot().sequence == sequence
        assert artifact_projections.inspect_auxiliary(tmp_path / "run", verify=True) == (result,)


def test_retry_refuses_different_bytes_and_keeps_original_debt(tmp_path):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        store._hooks = {
            "after_auxiliary_started": lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        }
        with pytest.raises(KeyboardInterrupt):
            store.publish_auxiliary(declaration, source)
    (source / "payload.json").write_bytes(b"different")
    with resume(tmp_path) as store:
        before = store.ledger.snapshot().sequence
        with pytest.raises(ArtifactUnsafeError):
            store.publish_auxiliary(declaration, source)
        assert store.ledger.snapshot().sequence == before
        assert store.owed_auxiliary == (declaration,)
        assert store.stopped is None


def test_committed_missing_content_stops_store_without_republishing(tmp_path):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        result = store.publish_auxiliary(declaration, source)
    directory = tmp_path / "run" / result.locator
    directory.chmod(0o700)
    (directory / "payload.json").unlink()
    directory.rmdir()
    with pytest.raises(ArtifactCorruptionError, match="artifact_missing"):
        resume(tmp_path)
    records = inspect_ledger(tmp_path / "run").records
    assert records[-1].event == "store_stopped"
    assert sum(r.event == "auxiliary_publish_finished" for r in records) == 1


def test_foreign_reference_is_refused_without_reading_external_files(tmp_path):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        result = store.publish_auxiliary(declaration, source)
        foreign = replace(result, publication_ref=replace(result.publication_ref, run_id="foreign"))
        with pytest.raises(ArtifactCorruptionError):
            foreign.verify(tmp_path / "run")
        declaration2 = replace(
            declaration,
            declaration_ref=replace(declaration.declaration_ref, run_id="other-campaign"),
        )
        before = store.ledger.snapshot().sequence
        with pytest.raises(ArtifactAdmissionRefused, match="foreign_auxiliary_declaration"):
            store.publish_auxiliary(declaration2, source)
        assert store.ledger.snapshot().sequence == before


@pytest.mark.parametrize("where", ["source", "ancestor", "destination_parent"])
def test_symlink_paths_fail_before_publication(tmp_path, where):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        if where == "source":
            link = tmp_path / "link"
            link.symlink_to(source, target_is_directory=True)
            source = link
        elif where == "ancestor":
            link = tmp_path / "ancestor"
            link.symlink_to(tmp_path, target_is_directory=True)
            source = link / "prepared"
        else:
            external = tmp_path / "outside"
            external.mkdir()
            (tmp_path / "run/artifacts/auxiliary").symlink_to(external, target_is_directory=True)
        with pytest.raises(ArtifactUnsafeError):
            store.publish_auxiliary(declaration, source)
        assert not artifact_projections.inspect_auxiliary(tmp_path / "run")
        if where == "destination_parent":
            assert list(external.iterdir()) == []


def test_copy_capacity_failure_is_resumable_with_original_declaration(tmp_path, monkeypatch):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        original = data._file_digest

        def full(*args, **kwargs):
            if kwargs.get("output") is not None:
                raise OSError(errno.ENOSPC, "injected")
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(data, "_file_digest", full)
            with pytest.raises(ArtifactCapacityError):
                store.publish_auxiliary(declaration, source)
        assert store.owed_auxiliary == (declaration,)
        assert events(store)[-1] == "auxiliary_publish_failed"
    with resume(tmp_path) as store:
        reference = store.publish_auxiliary(declaration, source)
        reference.verify(tmp_path / "run")
        assert store.owed_auxiliary == ()


def test_terminal_byte_range_and_hash_name_exact_durable_record(tmp_path):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        reference = store.publish_auxiliary(declaration, source)
        start, end = map(int, reference.publication_ref.locator.split("=")[1].split(":"))
        raw = (tmp_path / "run/artifacts.jsonl").read_bytes()[start:end]
        assert hashlib.sha256(raw).hexdigest() == reference.publication_ref.content_digest
        assert raw == store.ledger.snapshot().records[-1].encode()


def test_terminal_append_capacity_cut_recovers_renamed_content(tmp_path, monkeypatch):
    from evolution.tree.storage.journal_errors import JournalFault

    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        append = store.ledger.append

        def fail_terminal(event, *args, **kwargs):
            if event == "auxiliary_publish_finished":
                raise JournalFault("storage_capacity_exhausted")
            return append(event, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(store.ledger, "append", fail_terminal)
            with pytest.raises(ArtifactCapacityError):
                store.publish_auxiliary(declaration, source)
        assert store.owed_auxiliary == (declaration,)
        assert not artifact_projections.inspect_auxiliary(tmp_path / "run")
    source.rename(tmp_path / "source_gone")
    with resume(tmp_path) as store:
        assert not store.owed_auxiliary
        assert (
            artifact_projections.inspect_auxiliary(tmp_path / "run", verify=True)[0].declaration
            == declaration
        )


def test_read_io_failure_is_not_permanent_corruption(tmp_path, monkeypatch):
    from evolution.tree.execution.candidate_integrity import ArtifactError

    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        store.publish_auxiliary(declaration, source)
        with monkeypatch.context() as patch:

            def fail(*args, **kwargs):
                raise OSError(errno.EIO, "injected")

            patch.setattr(data, "_file_digest", fail)
            with pytest.raises(ArtifactError, match="io_failure"):
                store.publish_auxiliary(declaration, source)
        assert store.stopped is None
        assert store.publish_auxiliary(declaration, source).declaration == declaration


def test_resume_refuses_corrupt_renamed_debt_without_source_fallback(tmp_path):
    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        store._hooks = {
            "after_auxiliary_replace": lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        }
        with pytest.raises(KeyboardInterrupt):
            store.publish_auxiliary(declaration, source)
    path = tmp_path / "run" / declaration.locator / "payload.json"
    path.chmod(0o600)
    path.write_bytes(b"forged")
    with pytest.raises(ArtifactCorruptionError):
        resume(tmp_path)
    assert not any(
        r.event == "auxiliary_publish_finished" for r in inspect_ledger(tmp_path / "run").records
    )


def test_corrupt_auxiliary_blocks_owed_promotion_before_tree_publication(tmp_path):
    from tests.tree.artifacts.test_artifact_store import decision

    with create(tmp_path) as store:
        declaration, source = prepared(tmp_path, store)
        reference = store.publish_auxiliary(declaration, source)
        store.materialize_candidate("a1")
        candidate = store.seal("a1")
        store._hooks = {"after_pending": lambda: (_ for _ in ()).throw(KeyboardInterrupt())}
        with pytest.raises(KeyboardInterrupt):
            store.promote(decision(store, "a1", candidate))
    path = tmp_path / "run" / reference.locator / "payload.json"
    path.chmod(0o600)
    path.write_bytes(b"corrupt evidence")
    with pytest.raises(ArtifactCorruptionError):
        resume(tmp_path)
    records = inspect_ledger(tmp_path / "run").records
    assert not any(r.event == "promotion_committed" for r in records)
    assert not (tmp_path / "run/artifacts/champions/v1").exists()
