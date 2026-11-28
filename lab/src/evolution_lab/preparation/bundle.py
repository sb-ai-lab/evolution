"""Compose durable campaign bundles without preparing or executing commands."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from evolution.tree import ManifestError, load_manifest_v2

from evolution_lab._document_inputs import DocumentInputsError, capture_document_inputs
from evolution_lab._fileset import entry_fields, excluded_name, excluded_path, excluded_source
from evolution_lab._manifest_schema import (
    BASELINE_IMPORT_UNSUPPORTED,
    DIAGNOSIS_LIMIT,
    EVALUATION_MANIFEST_SCHEMA,
    CORPUS_INSIDE_ROLE_WORKSPACE,
    GROUPER_KIND_UNSUPPORTED,
    LINKED_RUN_UNSUPPORTED,
    core_diagnosis,
    corpus_inside_role_workspace,
    has_baseline_import,
    is_campaign_manifest,
    is_campaign_schema,
    is_linked_run,
    unsupported_grouper_kind,
)

from .environment import capture_environment
from .evaluation import bind_evaluation_input, conformance_payload, load_evaluation_input
from .inventory_audit import audit_manifest_relocation
from .internal_manifest import load_internal_manifest
from .models import BundleDescriptor, BundleEntry, CampaignSource, EnvironmentInput, EvaluationSource, PathBinding, PreparationError, ProbeSource, SourceRoot, StoredBundle
from .relocation import Relocator, local_path, unsupported
from .storage import PreparationStorage, canonical_json_bytes


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _refuse_reserved_environment_names(*, workflow=None, evaluation_input=None, launch=None,
                                       kind='campaign'):
    # Import at this runtime boundary: Owner binding also consumes preparation.
    from evolution_lab._ownership import MARKER_NAME
    from evolution_lab.owner import secrets

    names = secrets.declared_environment_names(workflow=workflow, evaluation_input=evaluation_input,
        launch=launch, kind=kind)
    if MARKER_NAME in names.execution | names.target_secrets | names.target_static:
        raise PreparationError('reserved_environment_name', 'bundle')


def _expected_execution(value: object) -> str | None:
    if value is not None and (not isinstance(value, str) or _DIGEST.fullmatch(value) is None):
        raise PreparationError("invalid_request", "bundle")
    return value


def _check_execution(descriptors, expected: str | None) -> None:
    # Compared after this call's own capture and before staging/publication:
    # the published descriptor is exactly the one compared (no TOCTOU window).
    if expected is not None and descriptors[0].digest != expected:
        raise PreparationError("environment_invalid", "bundle")


def _safe_info(path: Path, *, logical: str | None = None) -> os.stat_result:
    for ancestor in reversed((path, *path.parents)):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode):
            detail = None
            if logical is not None:
                detail = (f"declared root {logical[:-2]!a} path passes through a symbolic link"
                          if logical.endswith("/.") else f"symlink ancestor: {logical!a}")[:DIAGNOSIS_LIMIT]
            raise PreparationError("unsupported_input", "bundle", detail)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            unsupported()
    return info


def _read_file(path: Path, *, logical: str) -> bytes:
    before = _safe_info(path, logical=logical)
    if not stat.S_ISREG(before.st_mode):
        unsupported()
    with path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise PreparationError("inputs_changed", "bundle")
        data = stream.read()
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(before) != _identity(path.lstat()):
        raise PreparationError("inputs_changed", "bundle")
    return data


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(roots: Mapping[str, Path]) -> dict[Path, tuple[tuple[int, ...], str | None]]:
    snapshot = {}
    for alias, root in roots.items():
        stack = [root]
        while stack:
            path = stack.pop()
            logical = f"{alias}/{path.relative_to(root).as_posix()}"
            info = _safe_info(path, logical=logical)
            if stat.S_ISDIR(info.st_mode):
                snapshot[path] = (_identity(info), None)
                stack.extend(
                    child for child in sorted(path.iterdir())
                    if not excluded_name(child.name, directory=stat.S_ISDIR(child.lstat().st_mode))
                )
            else:
                snapshot[path] = (_identity(info), hashlib.sha256(_read_file(path, logical=logical)).hexdigest())
    return snapshot


def _verify_source(roots: Mapping[str, Path], before: dict) -> None:
    try:
        after = _snapshot(roots)
    except (OSError, PreparationError):
        raise PreparationError("inputs_changed", "bundle") from None
    if before != after:
        raise PreparationError("inputs_changed", "bundle")


def _document(data: bytes, *, manifest: bool) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def reject(_):
        raise ValueError("non-finite")
    try:
        result = json.loads(data, object_pairs_hook=unique, parse_constant=reject)
        if not isinstance(result, dict):
            raise ValueError("object required")
        return result
    except (ValueError, UnicodeError):
        raise PreparationError("manifest_invalid" if manifest else "invalid_request", "bundle") from None


def _inventory(stage: Path) -> tuple[BundleEntry, ...]:
    entries = []
    for path in sorted(stage.rglob("*")):
        info = path.lstat()
        digest = None if stat.S_ISDIR(info.st_mode) else hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(BundleEntry(**entry_fields(path.relative_to(stage).as_posix(), info, sha256=digest)))
    return tuple(entries)


def _copy_snapshot(stage: Path, relocator: Relocator, before: Mapping[Path, tuple[tuple[int, ...], str | None]]) -> None:
    (stage / "inputs").mkdir(mode=0o755, exist_ok=True)
    for path, (identity, digest) in sorted(before.items(), key=lambda item: (len(item[0].parts), str(item[0]))):
        destination = stage / relocator.logical(path)
        if digest is None:
            destination.mkdir(mode=0o700 | (identity[2] & 0o111), exist_ok=True)
            os.chmod(destination, 0o600 | (identity[2] & 0o111))
        else:
            data = _read_file(path, logical=relocator.logical(path))
            if hashlib.sha256(data).hexdigest() != digest:
                raise PreparationError("inputs_changed", "bundle")
            destination.write_bytes(data)
            os.chmod(destination, 0o600 | (identity[2] & 0o111))


def _refuse_excluded(
    relocator: Relocator, documents: Sequence[Path], path_bindings: Sequence[PathBinding],
) -> None:
    for document in documents:
        if excluded_path(relocator.logical(document)):
            raise PreparationError("unsupported_input", "bundle")
    for binding in path_bindings:
        if excluded_path(binding.relative_path) or excluded_path(relocator.logical(binding.document)):
            raise PreparationError("unsupported_input", "bundle")


class _Composition:
    """One Bundle composition: source snapshot, lazily created staging, receipt and publication."""

    def __init__(self, storage: PreparationStorage, relocator: Relocator, path_bindings: Sequence[PathBinding]):
        self.storage = storage
        self.relocator = relocator
        self.path_bindings = tuple(path_bindings)
        self.before: dict[Path, tuple[tuple[int, ...], str | None]] | None = None
        self.stage: Path | None = None
        self.composing = True

    def snapshot(self) -> dict[Path, tuple[tuple[int, ...], str | None]]:
        self.before = _snapshot(self.relocator.roots)
        return self.before

    def staging(self) -> Path:
        self.stage = self.storage.create_bundle_staging()
        return self.stage

    def publish(self, descriptor: BundleDescriptor, receipt: Mapping[str, Any], captures) -> StoredBundle:
        """Complete the source-specific receipt fields with the shared provenance, then publish."""
        receipt = {
            "schema": "lab-bundle-source-receipt/1",
            **receipt,
            "roots": [{"alias": alias, "source_path": str(path)} for alias, path in self.relocator.roots.items()],
            "path_bindings": [{"document": str(binding.document.absolute()), "pointer": binding.pointer, "root_alias": binding.root_alias, "relative_path": binding.relative_path} for binding in self.path_bindings],
            "files": [{"path": str(path), "sha256": digest} for path, (_, digest) in self.before.items() if digest is not None],
            "environments": [dict(capture.source_receipt) for capture in captures],
        }
        publishing, self.stage = self.stage, None  # publication owns cleanup after its claim
        self.composing = False
        return self.storage.publish_bundle(publishing, descriptor, source_receipt=receipt)


@contextmanager
def _composition(
    storage: PreparationStorage, relocator: Relocator, path_bindings: Sequence[PathBinding],
) -> Iterator[_Composition]:
    composition = _Composition(storage, relocator, path_bindings)
    try:
        yield composition
    except (PreparationError, OSError) as error:
        # A known source becoming unavailable/invalid during composition is drift,
        # even when it first surfaced as a filesystem or loader error.
        if composition.before is not None and composition.composing:
            _verify_source(relocator.roots, composition.before)
        if isinstance(error, PreparationError):
            raise
        raise PreparationError("io_error", "bundle") from None
    finally:
        if composition.stage is not None:
            storage.abort_bundle_staging(composition.stage)


def _create_evaluation_bundle(
    storage: PreparationStorage, *, source: EvaluationSource,
    environments: tuple[EnvironmentInput, ...], env: dict[str, str],
    secret_names: tuple[str, ...], relocator: Relocator,
    path_bindings: Sequence[PathBinding],
    expected_execution_environment: str | None = None,
) -> StoredBundle:
    input_path = local_path(str(source.input_path), Path.cwd())
    launch_path = local_path(str(source.launch_path), Path.cwd())
    if input_path == launch_path:
        unsupported()
    entrypoint = relocator.logical(input_path)
    launch_entry = relocator.logical(launch_path)
    _refuse_excluded(relocator, (input_path, launch_path), path_bindings)
    conformance_entrypoint = ".lab/conformance.json"
    with _composition(storage, relocator, path_bindings) as composition:
        before = composition.snapshot()
        original_input = _read_file(input_path, logical=entrypoint)
        original_launch = _read_file(launch_path, logical=launch_entry)
        value = load_evaluation_input(input_path, "bundle")
        raw_launch = _document(original_launch, manifest=False)
        if raw_launch.get("schema") == "lab-target-launch/3" and raw_launch.get("reflector_probe") is not None:
            raise PreparationError("invalid_request", "bundle")
        if local_path(raw_launch.get("policy"), launch_path.parent) != input_path:
            raise PreparationError("unsupported_input", "bundle", "launch.policy must point to the evaluation input.")
        captures = tuple(
            capture_environment(value, execution=value.executable.absolute() == Path(sys.executable).absolute())
            for value in environments
        )
        descriptors = tuple(capture.descriptor for capture in captures)
        _check_execution(descriptors, expected_execution_environment)
        stage = composition.staging()
        internal = stage / conformance_entrypoint
        internal.parent.mkdir(mode=0o700)
        seed_root = local_path(value["seed_root"], input_path.parent)
        internal.write_bytes(canonical_json_bytes(conformance_payload(value, seed_root=str(seed_root))))
        try:
            load_internal_manifest(internal)
        except ManifestError:
            raise PreparationError("manifest_invalid", "bundle") from None
        normalized_input = relocator.evaluation(input_path, value, descriptors)
        normalized_launch = relocator.launch(
            launch_path, raw_launch, descriptors, env, secret_names,
            policy_entry=conformance_entrypoint,
        )
        _refuse_reserved_environment_names(evaluation_input=normalized_input,
            launch=normalized_launch, kind='evaluation')
        relocator.finish()
        _copy_snapshot(stage, relocator, before)
        (stage / entrypoint).write_bytes(canonical_json_bytes(normalized_input))
        conformance_seed = os.path.relpath(
            relocator.logical(seed_root), Path(conformance_entrypoint).parent,
        ).replace(os.sep, "/")
        internal.write_bytes(canonical_json_bytes(
            conformance_payload(normalized_input, seed_root=conformance_seed)
        ))
        (stage / launch_entry).write_bytes(canonical_json_bytes(normalized_launch))
        try:
            manifest = load_internal_manifest(internal)
        except ManifestError:
            raise PreparationError("manifest_invalid", "bundle") from None
        bind_evaluation_input(
            normalized_input, document=stage / entrypoint, manifest=manifest,
            env=env, secret_names=secret_names, stage="bundle",
        )
        _verify_source(relocator.roots, before)
        descriptor = BundleDescriptor(
            entrypoint, conformance_entrypoint, launch_entry, descriptors, env,
            secret_names, _inventory(stage), source_kind="evaluation",
        )
        return composition.publish(descriptor, {
            "source_kind": "evaluation",
            "input_path": str(input_path),
            "input_sha256": hashlib.sha256(original_input).hexdigest(),
            "launch_path": str(launch_path),
            "launch_sha256": hashlib.sha256(original_launch).hexdigest(),
        }, captures)


def _probe_manifest(raw: Mapping[str, Any], *, seed_root: str) -> dict[str, Any]:
    identity = raw["identity"]
    return {
        "schema": EVALUATION_MANIFEST_SCHEMA,
        "seed_root": seed_root,
        "target": {
            "base_url": raw["endpoint"].get("base_url"),
            "seed_mode": identity.get("seed_mode"),
            "contract_digest": identity.get("contract_digest"),
            "settings_hash": identity.get("settings_hash"),
        },
        "artifacts": {"excludes": [], "dependency_paths": [], "writable_paths": []},
    }


def _check_probe_entry(data: bytes, identity: Mapping[str, Any]) -> None:
    """The launch was synthesized from the entry file before the snapshot; the Bundle holds the snapshot's entry.

    Both must be the same declaration (ADR-0021): the snapshot's entry has the canonical JSON digest that
    ``projects/entry.py`` gave the launch as ``identity.contract_digest``; otherwise the file changed in between.
    """
    try:
        digest = hashlib.sha256(canonical_json_bytes(json.loads(data.decode("utf-8")))).hexdigest()
    except (ValueError, TypeError):
        raise PreparationError("inputs_changed", "bundle") from None
    if digest != identity.get("contract_digest"):
        raise PreparationError("inputs_changed", "bundle")


def _create_probe_bundle(
    storage: PreparationStorage, *, source: ProbeSource,
    environments: tuple[EnvironmentInput, ...], env: dict[str, str],
    secret_names: tuple[str, ...], relocator: Relocator,
    path_bindings: Sequence[PathBinding],
    expected_execution_environment: str | None = None,
) -> StoredBundle:
    """A live target from an entry file: no package, a synthesized policy entry (spec 2026-10-02 §2.2)."""
    raw = dict(source.launch)
    identity, endpoint = raw.get("identity"), raw.get("endpoint")
    if (raw.get("schema") not in ("lab-target-launch/2", "lab-target-launch/3") or "environment_ref" in raw
            or not isinstance(identity, dict) or identity.get("runtime_ref_digest") is not None
            or not isinstance(endpoint, dict) or raw.get("reflector_probe") is not None):
        raise PreparationError("invalid_request", "bundle")
    entry_path = local_path(str(source.entry_path), Path.cwd())
    place_root = entry_path.parent
    if list(relocator.roots.values()) != [place_root]:  # the place is the only root
        raise PreparationError("invalid_request", "bundle")
    entrypoint = relocator.logical(entry_path)
    _refuse_excluded(relocator, (entry_path,), path_bindings)
    conformance_entrypoint = ".lab/probe-policy.json"
    launch_entry = ".lab/launch.json"
    with _composition(storage, relocator, path_bindings) as composition:
        before = composition.snapshot()
        original_entry = _read_file(entry_path, logical=entrypoint)
        captures = tuple(
            capture_environment(value, execution=value.executable.absolute() == Path(sys.executable).absolute())
            for value in environments
        )
        descriptors = tuple(capture.descriptor for capture in captures)
        _check_execution(descriptors, expected_execution_environment)
        target_environment = descriptors[-1]  # the service environment when it is the only one
        raw["environment_ref"] = target_environment.digest
        raw["identity"] = {**identity, "runtime_ref_digest": target_environment.digest}
        stage = composition.staging()
        internal = stage / conformance_entrypoint
        internal.parent.mkdir(mode=0o700)
        internal.write_bytes(canonical_json_bytes(_probe_manifest(raw, seed_root=str(place_root))))
        try:
            load_internal_manifest(internal)
        except ManifestError:
            raise PreparationError("manifest_invalid", "bundle") from None
        normalized_launch = relocator.launch(
            entry_path, raw, descriptors, env, secret_names,
            policy_entry=conformance_entrypoint,
        )
        _refuse_reserved_environment_names(launch=normalized_launch, kind='probe')
        relocator.finish()
        _copy_snapshot(stage, relocator, before)
        _check_probe_entry((stage / entrypoint).read_bytes(), identity)
        conformance_seed = os.path.relpath(
            relocator.logical(place_root), Path(conformance_entrypoint).parent,
        ).replace(os.sep, "/")
        internal.write_bytes(canonical_json_bytes(_probe_manifest(raw, seed_root=conformance_seed)))
        (stage / launch_entry).write_bytes(canonical_json_bytes(normalized_launch))
        try:
            load_internal_manifest(internal)
        except ManifestError:
            raise PreparationError("manifest_invalid", "bundle") from None
        _verify_source(relocator.roots, before)
        descriptor = BundleDescriptor(
            entrypoint, conformance_entrypoint, launch_entry, descriptors, env,
            secret_names, _inventory(stage), source_kind="probe",
        )
        return composition.publish(descriptor, {
            "source_kind": "probe",
            "entry_path": str(entry_path),
            "entry_sha256": hashlib.sha256(original_entry).hexdigest(),
        }, captures)


def create_bundle(
    storage: PreparationStorage, *, source: CampaignSource | EvaluationSource | ProbeSource,
    environments: Sequence[EnvironmentInput], env: Mapping[str, str],
    secret_names: Sequence[str], roots: Sequence[SourceRoot],
    path_bindings: Sequence[PathBinding] = (),
    expected_execution_environment: str | None = None,
) -> StoredBundle:
    """Copy exactly declared roots, normalize known references, then publish.

    Roots are explicit file/tree copy declarations (including any extra inputs).
    Native /15 has no ``extra_paths`` field. Source metadata is private provenance;
    published inventory always records the actual normalized/copied bytes.
    """
    if not isinstance(source, (CampaignSource, EvaluationSource, ProbeSource)) or not isinstance(env, Mapping):
        raise PreparationError("invalid_request", "bundle")
    expected = _expected_execution(expected_execution_environment)
    env = dict(env)
    secret_names = tuple(secret_names)
    if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in env.items()) or any(not isinstance(name, str) or not name for name in secret_names) or len(set(secret_names)) != len(secret_names) or set(env) & set(secret_names):
        raise PreparationError("invalid_request", "bundle")
    environments = tuple(environments)
    if not environments or any(not isinstance(value, EnvironmentInput) for value in environments):
        raise PreparationError("invalid_request", "bundle")
    execution = Path(sys.executable).absolute()
    if environments[0].executable.absolute() != execution or sum(value.executable.absolute() == execution for value in environments) != 1:
        raise PreparationError("environment_invalid", "bundle")
    relocator = Relocator(roots, path_bindings)
    forbidden = [storage.state_directory.absolute()]
    for environment in environments:
        forbidden.extend(path.absolute() for path in (environment.executable, *environment.wheel_paths, *environment.runtime_files))
    for root in relocator.roots.values():
        if any(root == path or root in path.parents or path in root.parents for path in forbidden):
            unsupported()
        # ADR-0017 p. 6 at the top-level boundary: relocation hides the root's own physical
        # name, so a root that is (or lies inside) an excluded entry is refused, not copied.
        if excluded_source(root, directory=root.is_dir()):
            raise PreparationError("unsupported_input", "bundle")
    if isinstance(source, EvaluationSource):
        return _create_evaluation_bundle(
            storage, source=source, environments=environments, env=env,
            secret_names=secret_names, relocator=relocator, path_bindings=path_bindings,
            expected_execution_environment=expected,
        )
    if isinstance(source, ProbeSource):
        return _create_probe_bundle(
            storage, source=source, environments=environments, env=env,
            secret_names=secret_names, relocator=relocator, path_bindings=path_bindings,
            expected_execution_environment=expected,
        )
    manifest_path = local_path(str(source.manifest_path), Path.cwd())
    launch_path = local_path(str(source.launch_path), Path.cwd())
    if manifest_path == launch_path:
        unsupported()
    entrypoint, launch_entry = relocator.logical(manifest_path), relocator.logical(launch_path)
    _refuse_excluded(relocator, (manifest_path, launch_path), path_bindings)
    with _composition(storage, relocator, path_bindings) as composition:
        before = composition.snapshot()
        original_manifest = _read_file(manifest_path, logical=entrypoint)
        original_launch = _read_file(launch_path, logical=launch_entry)
        raw_manifest = _document(original_manifest, manifest=True)
        raw_launch = _document(original_launch, manifest=False)
        if not is_campaign_schema(raw_manifest.get("schema")):
            raise PreparationError("unsupported_schema", "bundle")
        try:
            manifest = load_manifest_v2(manifest_path)
        except ManifestError as error:
            raise PreparationError("manifest_invalid", "bundle", core_diagnosis(error)) from None
        if manifest.workflow is None:
            raise PreparationError("manifest_invalid", "bundle")
        if is_linked_run(manifest):
            raise PreparationError(LINKED_RUN_UNSUPPORTED, "bundle")
        if has_baseline_import(manifest):
            raise PreparationError(BASELINE_IMPORT_UNSUPPORTED, "bundle")
        if unsupported_grouper_kind(raw_manifest):
            raise PreparationError(GROUPER_KIND_UNSUPPORTED, "bundle")
        try:
            misplaced_corpus = corpus_inside_role_workspace(manifest)
        except ValueError:
            raise PreparationError("manifest_invalid", "bundle") from None
        if misplaced_corpus:
            raise PreparationError(CORPUS_INSIDE_ROLE_WORKSPACE, "bundle")
        try:
            if manifest.workflow.reflector.environment_docs is not None:
                capture_document_inputs(manifest.workflow.reflector.environment_docs)
        except DocumentInputsError:
            unsupported()
        captures = tuple(capture_environment(value, execution=value.executable.absolute() == execution) for value in environments)
        descriptors = tuple(capture.descriptor for capture in captures)
        _check_execution(descriptors, expected)
        normalized_manifest = relocator.manifest(manifest_path, raw_manifest, descriptors)
        audit_manifest_relocation(
            manifest, relocator=relocator, document=manifest_path, raw=raw_manifest,
            pinned={descriptor.executable for descriptor in descriptors},
        )
        normalized_launch = relocator.launch(launch_path, raw_launch, descriptors, env, secret_names)
        _refuse_reserved_environment_names(workflow=manifest.workflow, launch=normalized_launch)
        if normalized_launch.get("reflector_probe") is not None and manifest.workflow.reflector.probe is None:
            raise PreparationError("manifest_invalid", "bundle",
                                   "Launch reflector_probe is declared but the manifest has no reflector.probe.")
        relocator.finish()
        stage = composition.staging()
        _copy_snapshot(stage, relocator, before)
        (stage / entrypoint).write_bytes(canonical_json_bytes(normalized_manifest))
        (stage / launch_entry).write_bytes(canonical_json_bytes(normalized_launch))
        _verify_source(relocator.roots, before)
        try:
            relocated = load_manifest_v2(stage / entrypoint)
        except ManifestError as error:
            raise PreparationError("manifest_invalid", "bundle", core_diagnosis(error)) from None
        if not is_campaign_manifest(relocated):
            raise PreparationError("manifest_invalid", "bundle")
        descriptor = BundleDescriptor(entrypoint, entrypoint, launch_entry, descriptors, env, secret_names, _inventory(stage))
        return composition.publish(descriptor, {
            "manifest_path": str(manifest_path), "manifest_sha256": hashlib.sha256(original_manifest).hexdigest(),
            "launch_path": str(launch_path), "launch_sha256": hashlib.sha256(original_launch).hexdigest(),
        }, captures)
