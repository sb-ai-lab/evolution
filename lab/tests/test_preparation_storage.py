"""V-S1-1: canonical preparation objects and immutable publication."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier

import pytest
from platforms import fd_path


HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _environment(*, limitations=("wheel archives remain external",)):
    return {
        "schema": "lab-environment/1",
        "executable": "/opt/python/bin/python",
        "executable_sha256": HEX_A,
        "python_version": "3.12.11",
        "distributions": [
            {"name": "Evo_Skills", "version": "0.3.0a5", "wheel_sha256": HEX_B},
            {"name": "evolution-lab", "version": "0.0.3.dev4", "wheel_sha256": HEX_C},
        ],
        "runtime_files": [{"path": "/opt/lib/runtime.so", "sha256": HEX_A}],
        "platform": {"system": "Linux", "machine": "x86_64"},
        "proof_profile": "wheel-members/1",
        "limitations": list(limitations),
    }


def _bundle_payload(files: dict[str, bytes], *, env=None, secret_names=("TOKEN", "API_KEY")):
    from evolution_lab.preparation.models import EnvironmentDescriptor

    environment = env or _environment()
    manifest = "inputs/project/manifest.json"
    launch = "inputs/project/launch.json"
    entries = [
        {"path": "inputs", "type": "directory", "executable": 0o111, "size": None, "sha256": None},
        {"path": "inputs/project", "type": "directory", "executable": 0o111, "size": None, "sha256": None},
    ]
    entries.extend(
        {"path": path, "type": "file", "executable": 0, "size": len(data), "sha256": _sha(data)}
        for path, data in files.items()
    )
    return {
        "schema": "lab-bundle/1",
        "source_kind": "campaign",
        "entrypoint": manifest,
        "conformance_entrypoint": manifest,
        "launch": launch,
        "environments": [environment],
        "env": {"MODE": "synthetic", "LANG": "C.UTF-8"},
        "secret_names": list(secret_names),
        "entries": list(reversed(entries)),
    }, EnvironmentDescriptor.from_payload(environment).digest


def _prepared_payload(bundle):
    return {
        "schema": "lab-prepared-execution/2",
        "bundle_digest": bundle.digest,
        "kind": "campaign",
        "entrypoint": bundle.descriptor.entrypoint,
        "target_binding": {
            "launch_digest": bundle.summary().launch_digest,
            "environment_digest": bundle.descriptor.environments[0].digest,
            "base_url": "http://127.0.0.1:8123",
            "policy_digest": HEX_A,
            "target_id": "synthetic-target",
            "contract_digest": None,
            "settings_hash": None,
            "runtime_ref_digest": None,
            "seed_mode": "required",
        },
        "execution_environment": bundle.descriptor.environments[0].digest,
        "secret_names": ["TOKEN", "API_KEY"],
        "runtime_policy": {"stop_grace_seconds": 45, "cleanup_wait_seconds": 30.0},
        "evaluation": None,
    }


def _stage_bundle(storage, files):
    staging = storage.create_bundle_staging()
    for relative, data in files.items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return staging


def _published_bundle(tmp_path, *, source_receipt=None):
    from evolution_lab.preparation.models import BundleDescriptor
    from evolution_lab.preparation.storage import PreparationStorage

    files = {
        "inputs/project/manifest.json": b'{"schema":"evolution-campaign/3"}\n',
        "inputs/project/launch.json": b'{"schema":"lab-target-launch/1"}\n',
    }
    storage = PreparationStorage(tmp_path / "state")
    payload, _ = _bundle_payload(files)
    descriptor = BundleDescriptor.from_payload(payload)
    return storage, storage.publish_bundle(_stage_bundle(storage, files), descriptor, source_receipt=source_receipt)


@pytest.fixture
def synced_paths(monkeypatch):
    import evolution_lab.preparation.storage as storage_module

    paths = []
    real_fsync = storage_module.os.fsync

    def record_fsync(descriptor):
        paths.append(Path(fd_path(descriptor)))
        return real_fsync(descriptor)

    monkeypatch.setattr(storage_module.os, "fsync", record_fsync)
    return paths


def test_first_bundle_publication_syncs_all_original_paths(tmp_path, synced_paths):
    from evolution_lab.preparation.models import BundleDescriptor
    from evolution_lab.preparation.storage import PreparationStorage

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    stage = _stage_bundle(storage, files)

    storage.publish_bundle(stage, descriptor, source_receipt={"source": "first"})

    receipt_directory = storage.state_directory / "receipts" / "bundles"
    receipt_pending = [path for path in synced_paths if path.suffix == ".pending"]
    assert len(receipt_pending) == 1
    assert receipt_pending[0].parent == receipt_directory
    assert set(synced_paths) == {
        stage / "descriptor.json",
        stage / "inputs/project/manifest.json",
        stage / "inputs/project/launch.json",
        stage / "inputs/project",
        stage / "inputs",
        stage,
        stage.parent,
        receipt_pending[0],
        receipt_directory,
    }
    assert len(synced_paths) == 9


def test_first_prepared_publication_syncs_all_original_paths(tmp_path, synced_paths):
    from evolution_lab.preparation.models import PreparedExecutionDescriptor

    storage, bundle = _published_bundle(tmp_path)
    synced_paths.clear()

    prepared = storage.publish_prepared(PreparedExecutionDescriptor.from_payload(_prepared_payload(bundle)))

    stage_descriptors = [path for path in synced_paths if path.name == "descriptor.json"]
    assert len(stage_descriptors) == 2
    stage = stage_descriptors[0].parent
    assert stage.name.endswith(".staging")
    assert set(synced_paths) == {stage / "descriptor.json", stage, prepared.path.parent}
    assert len(synced_paths) == 4


def test_repeated_bundle_publication_with_receipt_does_not_fsync(tmp_path, synced_paths):
    storage, bundle = _published_bundle(tmp_path, source_receipt={"source": "first"})
    files = {entry.path: (bundle.path / entry.path).read_bytes() for entry in bundle.descriptor.entries if entry.type == "file"}
    receipt = storage.state_directory / "receipts" / "bundles" / f"{bundle.digest}.json"
    first_receipt = receipt.read_bytes()
    stage = _stage_bundle(storage, files)
    synced_paths.clear()

    reused = storage.publish_bundle(stage, bundle.descriptor, source_receipt={"source": "second"})

    assert synced_paths == []
    assert reused == bundle
    assert not stage.exists()
    assert receipt.read_bytes() == first_receipt


def test_repeated_prepared_publication_does_not_fsync_or_create_stage(tmp_path, monkeypatch, synced_paths):
    from evolution_lab.preparation.models import PreparedExecutionDescriptor

    storage, bundle = _published_bundle(tmp_path)
    descriptor = PreparedExecutionDescriptor.from_payload(_prepared_payload(bundle))
    first = storage.publish_prepared(descriptor)
    created_stages = []
    real_mkdir = Path.mkdir

    def record_mkdir(path, *args, **kwargs):
        if path.name.endswith(".staging"):
            created_stages.append(path)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    synced_paths.clear()

    assert storage.publish_prepared(descriptor) == first

    assert synced_paths == []
    assert created_stages == []


def test_repeated_bundle_restores_missing_receipt_durably(tmp_path, synced_paths):
    receipt_data = {"source": "first"}
    storage, bundle = _published_bundle(tmp_path, source_receipt=receipt_data)
    files = {entry.path: (bundle.path / entry.path).read_bytes() for entry in bundle.descriptor.entries if entry.type == "file"}
    receipt = storage.state_directory / "receipts" / "bundles" / f"{bundle.digest}.json"
    first_receipt = receipt.read_bytes()
    receipt.unlink()
    stage = _stage_bundle(storage, files)
    synced_paths.clear()

    assert storage.publish_bundle(stage, bundle.descriptor, source_receipt=receipt_data) == bundle

    assert receipt.read_bytes() == first_receipt
    assert len(synced_paths) == 2
    assert synced_paths[0].parent == receipt.parent
    assert synced_paths[0].suffix == ".pending"
    assert synced_paths[1] == receipt.parent
    assert not stage.exists()


@pytest.mark.parametrize("corruption", ["stage", "published"])
def test_repeated_bundle_still_verifies_both_inventories(tmp_path, synced_paths, corruption):
    from evolution_lab.preparation.models import PreparationError

    storage, bundle = _published_bundle(tmp_path)
    files = {entry.path: (bundle.path / entry.path).read_bytes() for entry in bundle.descriptor.entries if entry.type == "file"}
    stage = _stage_bundle(storage, files)
    damaged = (stage if corruption == "stage" else bundle.path) / bundle.descriptor.entrypoint
    damaged.chmod(0o600)
    damaged.write_bytes(b"x" * len(files[bundle.descriptor.entrypoint]))
    synced_paths.clear()

    with pytest.raises(PreparationError) as error:
        storage.publish_bundle(stage, bundle.descriptor)

    assert error.value.code == "artifacts_invalid"
    assert error.value.stage == "bundle"
    assert synced_paths == []
    assert not stage.exists()


@pytest.mark.parametrize("kind", ["bundle", "prepared"])
@pytest.mark.parametrize("leaf", ["file", "symlink", "dangling_symlink"])
def test_publish_refuses_invalid_destination_before_fsync(tmp_path, synced_paths, kind, leaf):
    from evolution_lab.preparation.models import PreparedExecutionDescriptor, PreparationError

    storage, bundle = _published_bundle(tmp_path)
    files = {entry.path: (bundle.path / entry.path).read_bytes() for entry in bundle.descriptor.entries if entry.type == "file"}
    if kind == "bundle":
        # Keep the valid linked Bundle for Prepared tests; use a new digest here.
        from evolution_lab.preparation.models import BundleDescriptor

        files[bundle.descriptor.entrypoint] = b"other manifest"
        descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
        destination = storage.bundle_path(descriptor.digest)
    else:
        descriptor = PreparedExecutionDescriptor.from_payload(_prepared_payload(bundle))
        destination = storage.prepared_path(descriptor.digest)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"keep")
    if leaf == "file":
        destination.write_bytes(b"keep")
    else:
        destination.symlink_to(external if leaf == "symlink" else tmp_path / "absent", target_is_directory=True)
    original = destination.lstat()
    synced_paths.clear()

    with pytest.raises(PreparationError) as error:
        if kind == "bundle":
            stage = _stage_bundle(storage, files)
            storage.publish_bundle(stage, descriptor)
        else:
            storage.publish_prepared(descriptor)

    assert error.value.code == "artifacts_invalid"
    assert error.value.stage == ("bundle" if kind == "bundle" else "prepare")
    assert synced_paths == []
    assert destination.lstat() == original
    assert sentinel.read_bytes() == b"keep"
    if kind == "bundle":
        assert not stage.exists()


@pytest.mark.parametrize("leaf", ["file", "directory", "symlink", "dangling_symlink"])
def test_existing_receipt_leaf_is_preserved_without_fsync(tmp_path, synced_paths, leaf):
    storage, bundle = _published_bundle(tmp_path)
    files = {entry.path: (bundle.path / entry.path).read_bytes() for entry in bundle.descriptor.entries if entry.type == "file"}
    receipt = storage.state_directory / "receipts" / "bundles" / f"{bundle.digest}.json"
    external = tmp_path / "external"
    external.write_bytes(b"keep")
    if leaf == "file":
        receipt.write_bytes(b"keep")
    elif leaf == "directory":
        receipt.mkdir()
        (receipt / "sentinel").write_bytes(b"keep")
    else:
        receipt.symlink_to(external if leaf == "symlink" else tmp_path / "absent")
    original = receipt.lstat()
    synced_paths.clear()

    assert storage.publish_bundle(
        _stage_bundle(storage, files), bundle.descriptor, source_receipt={"source": "later"},
    ) == bundle

    assert synced_paths == []
    assert receipt.lstat() == original
    assert external.read_bytes() == b"keep"
    if leaf == "file":
        assert receipt.read_bytes() == b"keep"
    elif leaf == "directory":
        assert (receipt / "sentinel").read_bytes() == b"keep"
    assert not list(receipt.parent.glob("*.pending"))


def test_receipt_link_still_settles_concurrent_first_writers(tmp_path, monkeypatch):
    import evolution_lab.preparation.storage as storage_module

    storage, bundle = _published_bundle(tmp_path)
    other = storage_module.PreparationStorage(storage.state_directory)
    ready_to_link = Barrier(2)
    real_link = storage_module.os.link

    def racing_link(source, destination):
        ready_to_link.wait(timeout=5)
        return real_link(source, destination)

    monkeypatch.setattr(storage_module.os, "link", racing_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer._publish_receipt, bundle.digest, {"source": name}) for writer, name in ((storage, "first"), (other, "second"))]
        for future in futures:
            future.result(timeout=5)

    directory = storage.state_directory / "receipts" / "bundles"
    assert json.loads((directory / f"{bundle.digest}.json").read_bytes()) in (
        {"source": "first"}, {"source": "second"},
    )
    assert len(list(directory.iterdir())) == 1


def test_prepared_digest_appearing_after_early_check_reuses_first_publication(tmp_path, monkeypatch):
    from evolution_lab.preparation.models import PreparedExecutionDescriptor
    from evolution_lab.preparation.storage import PreparationStorage

    storage, bundle = _published_bundle(tmp_path)
    other = PreparationStorage(storage.state_directory)
    descriptor = PreparedExecutionDescriptor.from_payload(_prepared_payload(bundle))
    real_write = storage._write_descriptor
    competing = []

    def publish_competitor_before_write(path, value, *, stage):
        competing.append(other.publish_prepared(descriptor))
        return real_write(path, value, stage=stage)

    monkeypatch.setattr(storage, "_write_descriptor", publish_competitor_before_write)

    assert storage.publish_prepared(descriptor) == competing[0]
    assert list((storage.state_directory / "prepared").iterdir()) == [competing[0].path]


def test_models_are_closed_canonical_and_deeply_immutable():
    from evolution_lab.preparation.models import (
        BundleDescriptor,
        BundleEntry,
        PreparationError,
        RuntimePolicy,
    )
    from evolution_lab.preparation.storage import descriptor_digest

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    left, _ = _bundle_payload(files)
    right = json.loads(json.dumps(left))
    right["env"] = {"LANG": "C.UTF-8", "MODE": "synthetic"}
    right["secret_names"] = list(reversed(right["secret_names"]))
    right["entries"] = list(reversed(right["entries"]))
    right["environments"][0]["distributions"].reverse()

    first = BundleDescriptor.from_payload(left)
    second = BundleDescriptor.from_payload(right)

    assert descriptor_digest(first) == descriptor_digest(second)
    assert [row.path for row in first.entries] == sorted(row.path for row in first.entries)
    assert first.secret_names == ("API_KEY", "TOKEN")
    assert [pin.name for pin in first.environments[0].distributions] == ["evo-skills", "evolution-lab"]
    with pytest.raises(FrozenInstanceError):
        first.source_kind = "evaluation"
    copied = first.to_payload()
    copied["env"]["MODE"] = "changed"
    assert first.env["MODE"] == "synthetic"

    for mutation in (
        lambda value: value.update(extra="unknown"),
        lambda value: value["entries"][0].update(executable=True),
        lambda value: value["entries"][0].update(size=True),
    ):
        invalid = json.loads(json.dumps(left))
        mutation(invalid)
        with pytest.raises(PreparationError) as error:
            BundleDescriptor.from_payload(invalid)
        assert error.value.failure.to_payload()["code"] == "invalid_request"

    with pytest.raises(PreparationError):
        BundleEntry("input.txt", "file", True, 1, HEX_A)
    with pytest.raises(PreparationError):
        RuntimePolicy(True, 30)


def test_campaign_conformance_entrypoint_must_equal_entrypoint():
    from evolution_lab.preparation.models import BundleDescriptor, PreparationError

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/conformance.json": b"other manifest",
        "inputs/project/launch.json": b"launch",
    }
    payload, _ = _bundle_payload(files)
    payload["conformance_entrypoint"] = "inputs/project/conformance.json"

    with pytest.raises(PreparationError) as error:
        BundleDescriptor.from_payload(payload)

    assert error.value.code == "invalid_request"
    assert error.value.stage == "bundle"


def test_bundle_and_prepared_publish_reopen_and_expose_safe_summaries(tmp_path):
    from evolution_lab.preparation.models import PreparedExecutionDescriptor

    storage, bundle = _published_bundle(tmp_path)
    prepared = storage.publish_prepared(PreparedExecutionDescriptor.from_payload(_prepared_payload(bundle)))

    reopened_bundle = storage.get_bundle(bundle.digest)
    reopened_prepared = storage.get_prepared(prepared.digest)

    assert reopened_bundle.descriptor == bundle.descriptor
    assert (reopened_bundle.path / bundle.descriptor.entrypoint).read_bytes().startswith(b'{"schema"')
    assert reopened_prepared.descriptor == prepared.descriptor
    assert reopened_bundle.summary().to_payload() == {
        "schema": "lab-bundle-summary/3",
        "digest": bundle.digest,
        "source_kind": "campaign",
        "entrypoint": "inputs/project/manifest.json",
        "measurement_basis": None,
        "launch_digest": _sha(b'{"schema":"lab-target-launch/1"}\n'),
        "environment_digests": [bundle.descriptor.environments[0].digest],
        "secret_names": ["API_KEY", "TOKEN"],
        "entry_count": 4,
        "limitations": ["wheel archives remain external"],
        "warnings": [],
    }
    prepared_payload = reopened_prepared.summary().to_payload()
    assert set(prepared_payload) == {
        "schema", "digest", "bundle_digest", "kind", "target_binding",
        "execution_environment", "secret_names", "runtime_policy", "evaluation",
    }
    serialized = json.dumps({"bundle": reopened_bundle.summary().to_payload(), "prepared": prepared_payload})
    for private_value in ('"MODE"', "manifest.json\"}", "/opt/lib/runtime.so", "receipt", "argv"):
        assert private_value not in serialized


def test_reopen_never_reads_source_project(tmp_path):
    from evolution_lab.preparation.models import BundleDescriptor
    from evolution_lab.preparation.storage import PreparationStorage

    source = tmp_path / "source"
    source.mkdir()
    files = {
        "inputs/project/manifest.json": b"original manifest",
        "inputs/project/launch.json": b"original launch",
    }
    for logical, data in files.items():
        (source / Path(logical).name).write_bytes(data)
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    staging = storage.create_bundle_staging()
    for logical in files:
        destination = staging / logical
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / Path(logical).name).read_bytes())
    published = storage.publish_bundle(staging, descriptor, source_receipt={"root": str(source)})

    for path in source.iterdir():
        path.unlink()
    source.rmdir()
    reopened = PreparationStorage(tmp_path / "state").get_bundle(published.digest)

    assert (reopened.path / "inputs/project/manifest.json").read_bytes() == b"original manifest"
    assert str(source) not in json.dumps(reopened.summary().to_payload())


def test_interrupted_bundle_publication_has_no_visible_object(tmp_path, monkeypatch):
    from evolution_lab.preparation.models import BundleDescriptor, PreparationError
    from evolution_lab.preparation.storage import PreparationStorage
    import evolution_lab.preparation.storage as storage_module

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    digest = descriptor.digest
    real_rename = storage_module.os.rename

    def fail_final_rename(source, destination):
        if Path(destination) == storage.bundle_path(digest):
            raise OSError("synthetic interruption")
        return real_rename(source, destination)

    monkeypatch.setattr(storage_module.os, "rename", fail_final_rename)
    with pytest.raises(PreparationError) as error:
        storage.publish_bundle(_stage_bundle(storage, files), descriptor)

    assert error.value.code == "io_error"
    assert not storage.bundle_path(digest).exists()


def test_corrupt_existing_destination_is_never_overwritten(tmp_path):
    from evolution_lab.preparation.models import BundleDescriptor, PreparationError
    from evolution_lab.preparation.storage import PreparationStorage

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    destination = storage.bundle_path(descriptor.digest)
    destination.mkdir(parents=True)
    sentinel = destination / "descriptor.json"
    sentinel.write_text("{broken", encoding="utf-8")

    with pytest.raises(PreparationError):
        storage.publish_bundle(_stage_bundle(storage, files), descriptor)

    assert sentinel.read_text(encoding="utf-8") == "{broken"


def test_rejected_external_staging_path_is_never_cleaned(tmp_path):
    from evolution_lab.preparation.models import BundleDescriptor, PreparationError
    from evolution_lab.preparation.storage import PreparationStorage

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(PreparationError) as error:
        storage.publish_bundle(unrelated, descriptor)

    assert error.value.code == "invalid_request"
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_published_bundle_cannot_be_reused_as_cleanup_staging(tmp_path):
    from evolution_lab.preparation.models import PreparationError

    storage, bundle = _published_bundle(tmp_path)

    with pytest.raises(PreparationError) as error:
        storage.publish_bundle(bundle.path, bundle.descriptor)

    assert error.value.code == "invalid_request"
    assert storage.get_bundle(bundle.digest).descriptor == bundle.descriptor


def test_rejected_staging_symlink_cleanup_never_chmods_target(tmp_path):
    from evolution_lab.preparation.models import BundleDescriptor, PreparationError
    from evolution_lab.preparation.storage import PreparationStorage

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    staging = _stage_bundle(storage, files)
    external = tmp_path / "external"
    external.write_text("keep", encoding="utf-8")
    external.chmod(0o400)
    (staging / "unexpected-link").symlink_to(external)

    with pytest.raises(PreparationError) as error:
        storage.publish_bundle(staging, descriptor)

    assert error.value.code == "artifacts_invalid"
    assert external.read_text(encoding="utf-8") == "keep"
    assert external.stat().st_mode & 0o777 == 0o400
    assert not staging.exists()


def test_two_writers_of_same_digest_reuse_one_verified_bundle(tmp_path):
    from evolution_lab.preparation.models import BundleDescriptor
    from evolution_lab.preparation.storage import PreparationStorage

    files = {
        "inputs/project/manifest.json": b"manifest",
        "inputs/project/launch.json": b"launch",
    }
    storage = PreparationStorage(tmp_path / "state")
    descriptor = BundleDescriptor.from_payload(_bundle_payload(files)[0])
    first_stage = _stage_bundle(storage, files)
    second_stage = _stage_bundle(storage, files)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda stage: storage.publish_bundle(stage, descriptor), (first_stage, second_stage)))

    assert {result.digest for result in results} == {descriptor.digest}
    assert storage.get_bundle(descriptor.digest).descriptor == descriptor
    assert len(list((tmp_path / "state" / "bundles").iterdir())) == 1


@pytest.mark.parametrize(
    ("descriptor_bytes", "directory_name", "expected_code"),
    [
        (b'{"schema":"lab-bundle/2"}', HEX_A, "unsupported_schema"),
        (b'{"schema":"lab-bundle/1","schema":"lab-bundle/1"}', HEX_A, "artifacts_invalid"),
        (b"{broken", HEX_A, "artifacts_invalid"),
    ],
)
def test_read_errors_are_typed_and_safe(tmp_path, descriptor_bytes, directory_name, expected_code):
    from evolution_lab.preparation.models import PreparationError
    from evolution_lab.preparation.storage import PreparationStorage

    storage = PreparationStorage(tmp_path / "state")
    destination = storage.bundle_path(directory_name)
    destination.mkdir(parents=True)
    (destination / "descriptor.json").write_bytes(descriptor_bytes)

    with pytest.raises(PreparationError) as error:
        storage.get_bundle(directory_name)

    assert error.value.code == expected_code
    assert error.value.stage == "read"
    assert "broken" not in str(error.value)


def test_missing_invalid_and_modified_objects_fail_without_source_details(tmp_path):
    from evolution_lab.preparation.models import PreparationError

    storage, bundle = _published_bundle(tmp_path)
    (bundle.path / bundle.descriptor.entrypoint).chmod(0o600)
    (bundle.path / bundle.descriptor.entrypoint).write_bytes(b"changed")

    for digest, expected in (("not-a-digest", "invalid_request"), (HEX_A, "not_found"), (bundle.digest, "artifacts_invalid")):
        with pytest.raises(PreparationError) as error:
            storage.get_bundle(digest)
        assert error.value.code == expected
        assert error.value.stage == "read"
        assert str(tmp_path) not in str(error.value)


def test_noncanonical_descriptor_bytes_are_rejected(tmp_path):
    from evolution_lab.preparation.models import PreparationError

    storage, bundle = _published_bundle(tmp_path)
    descriptor_path = bundle.path / "descriptor.json"
    descriptor_path.chmod(0o600)
    descriptor_path.write_text(
        json.dumps(bundle.descriptor.to_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(PreparationError) as error:
        storage.get_bundle(bundle.digest)

    assert error.value.code == "artifacts_invalid"
    assert error.value.stage == "read"


def test_prepared_reader_revalidates_linked_bundle(tmp_path):
    from evolution_lab.preparation.models import PreparedExecutionDescriptor, PreparationError

    storage, bundle = _published_bundle(tmp_path)
    prepared = storage.publish_prepared(PreparedExecutionDescriptor.from_payload(_prepared_payload(bundle)))
    (bundle.path / bundle.descriptor.entrypoint).chmod(0o600)
    (bundle.path / bundle.descriptor.entrypoint).write_bytes(b"changed")

    with pytest.raises(PreparationError) as error:
        storage.get_prepared(prepared.digest)

    assert error.value.code == "artifacts_invalid"
    assert error.value.stage == "read"


def test_prepared_descriptor_v1_is_refused(tmp_path):
    from evolution_lab.preparation.models import PreparationError, PreparedExecutionDescriptor

    _storage, bundle = _published_bundle(tmp_path)
    payload = _prepared_payload(bundle)
    payload["schema"] = "lab-prepared-execution/1"

    with pytest.raises(PreparationError) as error:
        PreparedExecutionDescriptor.from_payload(payload, stage="prepare")

    assert error.value.code == "unsupported_schema"


pytestmark = pytest.mark.usefixtures("strict_durability")
