"""Verifier-free saved Bundle binding and explicit runtime revalidation."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping
from zipfile import BadZipFile, ZipFile

from evolution.tree import ManifestError, load_manifest_v2

from evolution_lab._manifest_schema import is_campaign_manifest, is_evaluation_manifest
from evolution_lab.preparation.environment import _recapture_environment
from evolution_lab.preparation.internal_manifest import load_internal_manifest
from evolution_lab.preparation.models import (
    EnvironmentDescriptor,
    PreparationError,
    RuntimePolicy,
)
from evolution_lab.preparation.storage import PreparationStorage

from .models import ExecutorIdentity, OwnerError
from .receipts import ReceiptError, read_json


_MAX_SOURCE_RECEIPT_BYTES = 1024 * 1024
_MAX_WHEEL_BYTES = 64 * 1024 * 1024
_MAX_MODULE_BYTES = 4 * 1024 * 1024
_MAX_WHEEL_MEMBERS = 10_000
_LAB_DISTRIBUTION = "evolution-lab"
_CORE_DISTRIBUTION = "evo-skills"
_COMMON_EXECUTOR_MEMBERS = {
    "evolution_lab._ownership": "evolution_lab/_ownership.py",
    "evolution_lab.owner._helper": "evolution_lab/owner/_helper.py",
}
_EXECUTOR_MEMBERS = MappingProxyType({
    "campaign": MappingProxyType({
        **_COMMON_EXECUTOR_MEMBERS,
        "evolution_lab.owner._campaign_worker": "evolution_lab/owner/_campaign_worker.py",
        "evolution_lab.owner.core_entry": "evolution_lab/owner/core_entry.py",
        "evolution_lab.owner.settlement": "evolution_lab/owner/settlement.py",
    }),
    "evaluation": MappingProxyType({
        **_COMMON_EXECUTOR_MEMBERS,
        "evolution_lab.owner._evaluation_worker": "evolution_lab/owner/_evaluation_worker.py",
        "evolution_lab.owner.settlement": "evolution_lab/owner/settlement.py",
    }),
    "conformance": MappingProxyType({
        **_COMMON_EXECUTOR_MEMBERS,
        "evolution_lab.owner._conformance_worker": "evolution_lab/owner/_conformance_worker.py",
    }),
})
_CLOSED_STAGES = frozenset({
    "connect", "target_start", "check", "before_start", "spawning", "running",
    "finalizing", "cleanup", "read",
})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _stage(value: str) -> str:
    if value not in _CLOSED_STAGES:
        raise OwnerError("invalid_argument", "before_start")
    return value


def _from_preparation(error: PreparationError, stage: str) -> OwnerError:
    code = error.code
    if code == "invalid_request":
        code = "invalid_argument"
    if code not in {
        "invalid_argument", "unsupported_schema", "not_found", "inputs_changed",
        "digest_mismatch", "manifest_invalid", "artifacts_invalid",
        "environment_invalid", "environment_unavailable", "unsupported_input",
        "reserved_public_input", "reserved_environment_name", "io_error",
    }:
        code = "artifacts_invalid"
    return OwnerError(code, stage)


@dataclass(frozen=True, slots=True)
class BundleBinding:
    """Immutable, verifier-free view over copied Bundle inputs."""

    bundle_digest: str
    launch_digest: str
    source_kind: str
    bundle_path: Path
    entrypoint_path: Path
    manifest_path: Path
    launch_path: Path
    manifest: Any
    launch: Mapping[str, Any]
    execution_environment: EnvironmentDescriptor
    target_environment: EnvironmentDescriptor
    environment_descriptors: Mapping[str, EnvironmentDescriptor]
    base_environment: Mapping[str, str]
    secret_names: tuple[str, ...]
    evaluation_input: Mapping[str, Any] | None
    _environment_receipts: Mapping[str, Mapping[str, Any]] = field(
        repr=False, compare=False
    )

    def environment_receipt(self, digest: str) -> Mapping[str, Any]:
        try:
            return self._environment_receipts[digest]
        except KeyError:
            raise OwnerError("environment_invalid", "before_start") from None


@dataclass(frozen=True, slots=True)
class PreparedBinding:
    """A saved PreparedExecution linked to its immutable Bundle binding."""

    prepared_digest: str
    bundle: BundleBinding
    runtime_policy: RuntimePolicy
    kind: str
    evaluation: Any = None
    secret_names: tuple[str, ...] = ()


def _validated_environment_receipts(stored, receipt: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    environments = receipt.get("environments")
    if (
        receipt.get("schema") != "lab-bundle-source-receipt/1"
        or not isinstance(environments, (list, tuple))
        or len(environments) != len(stored.descriptor.environments)
    ):
        raise ValueError("invalid environment receipts")
    result: dict[str, Mapping[str, Any]] = {}
    for descriptor, value in zip(stored.descriptor.environments, environments, strict=True):
        if not isinstance(value, Mapping):
            raise ValueError("invalid environment receipt")
        # Detailed closed-shape/pin validation remains centralized in the
        # recapture seam; binding stores a defensive immutable copy only.
        if value.get("executable") != descriptor.executable:
            raise ValueError("environment executable mismatch")
        result[descriptor.digest] = _freeze(value)
    if len(result) != len(environments):
        raise ValueError("duplicate environment receipt")
    return MappingProxyType(result)


def open_bundle(
    storage: PreparationStorage,
    bundle_digest: str,
    *,
    stage: str = "connect",
) -> BundleBinding:
    """Open copied Bundle inputs without probing any external prerequisite."""
    stage = _stage(stage)
    if not isinstance(storage, PreparationStorage):
        raise OwnerError("invalid_argument", stage)
    try:
        stored = storage.get_bundle(bundle_digest)
        receipt = read_json(
            storage.state_directory / "receipts" / "bundles" / f"{stored.digest}.json",
            max_bytes=_MAX_SOURCE_RECEIPT_BYTES,
        )
        receipts = _validated_environment_receipts(stored, receipt)
        entrypoint_path = stored.path / stored.descriptor.entrypoint
        manifest_path = stored.path / stored.descriptor.conformance_entrypoint
        launch_path = stored.path / stored.descriptor.launch
        source_kind = stored.descriptor.source_kind
        manifest = (
            load_internal_manifest(manifest_path)
            if source_kind in {"evaluation", "probe"}
            else load_manifest_v2(manifest_path)
        )
        if (
            source_kind == "campaign"
            and not is_campaign_manifest(manifest)
        ) or (
            # A probe Bundle's synthesized policy entry is an evaluation-shaped manifest (spec 2026-10-02 §2.2).
            source_kind in {"evaluation", "probe"}
            and not is_evaluation_manifest(manifest)
        ):
            raise ValueError("unsupported saved manifest")
        evaluation_input = None
        if source_kind == "evaluation":
            from evolution_lab.preparation.evaluation import (
                bind_evaluation_input,
                load_evaluation_input,
            )

            evaluation_input = load_evaluation_input(entrypoint_path, "read")
            bind_evaluation_input(
                evaluation_input,
                document=entrypoint_path,
                manifest=manifest,
                env=stored.descriptor.env,
                secret_names=stored.descriptor.secret_names,
                stage="read",
            )
        launch = read_json(launch_path, max_bytes=_MAX_SOURCE_RECEIPT_BYTES)
        target_digest = launch.get("environment_ref")
        descriptors = {
            descriptor.digest: descriptor for descriptor in stored.descriptor.environments
        }
        if not isinstance(target_digest, str) or target_digest not in descriptors:
            raise ValueError("unknown target environment")
        from .secrets import reserved_service_names

        reserved = reserved_service_names()
        target_env = launch.get("env")
        target_names = launch.get("secret_names")
        if (
            not isinstance(target_env, Mapping)
            or not isinstance(target_names, (list, tuple))
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in target_env.items())
            or any(not isinstance(name, str) for name in target_names)
            or reserved & (
                set(stored.descriptor.env)
                | set(stored.descriptor.secret_names)
                | set(target_env)
                | set(target_names)
            )
            or not set(target_names) <= set(stored.descriptor.secret_names)
        ):
            raise ValueError("invalid saved environment names")
        return BundleBinding(
            bundle_digest=stored.digest,
            launch_digest=stored.summary().launch_digest,
            source_kind=source_kind,
            bundle_path=stored.path,
            entrypoint_path=entrypoint_path,
            manifest_path=manifest_path,
            launch_path=launch_path,
            manifest=manifest,
            launch=_freeze(launch),
            execution_environment=stored.descriptor.environments[0],
            target_environment=descriptors[target_digest],
            environment_descriptors=MappingProxyType(descriptors),
            base_environment=MappingProxyType(dict(stored.descriptor.env)),
            secret_names=tuple(stored.descriptor.secret_names),
            evaluation_input=None if evaluation_input is None else _freeze(evaluation_input),
            _environment_receipts=receipts,
        )
    except OwnerError:
        raise
    except PreparationError as error:
        raise _from_preparation(error, stage) from None
    except (ReceiptError, ManifestError, OSError, ValueError, TypeError, KeyError):
        raise OwnerError("artifacts_invalid", stage) from None


def open_prepared(
    storage: PreparationStorage,
    prepared_digest: str,
    *,
    stage: str = "before_start",
) -> PreparedBinding:
    """Open a PreparedExecution and its copied Bundle without revalidation."""
    stage = _stage(stage)
    try:
        stored = storage.get_prepared(prepared_digest)
        bundle = open_bundle(storage, stored.descriptor.bundle_digest, stage=stage)
        if (
            stored.descriptor.execution_environment != bundle.execution_environment.digest
            or stored.descriptor.target_binding.environment_digest
            != bundle.target_environment.digest
        ):
            raise OwnerError("digest_mismatch", stage)
        return PreparedBinding(
            stored.digest,
            bundle,
            stored.descriptor.runtime_policy,
            stored.descriptor.kind,
            stored.descriptor.evaluation,
            stored.descriptor.secret_names,
        )
    except OwnerError:
        raise
    except PreparationError as error:
        raise _from_preparation(error, stage) from None


def _recapture(binding: BundleBinding, environment_digest: str, stage: str):
    try:
        descriptor = binding.environment_descriptors[environment_digest]
        receipt = binding.environment_receipt(environment_digest)
        return _recapture_environment(
            descriptor,
            receipt,
            execution=environment_digest == binding.execution_environment.digest,
        )
    except OwnerError:
        raise
    except KeyError:
        raise OwnerError("environment_invalid", stage) from None
    except PreparationError as error:
        raise _from_preparation(error, stage) from None


def revalidate_environment(
    binding: BundleBinding,
    environment_digest: str,
    *,
    stage: str,
) -> EnvironmentDescriptor:
    """Recheck exact retained executable, wheel, and runtime bytes."""
    stage = _stage(stage)
    if not isinstance(binding, BundleBinding) or not isinstance(environment_digest, str):
        raise OwnerError("invalid_argument", stage)
    return _recapture(binding, environment_digest, stage).descriptor


def _distribution_pin(binding: BundleBinding, name: str):
    pins = {pin.name: pin for pin in binding.execution_environment.distributions}
    try:
        return pins[name]
    except KeyError:
        raise OwnerError("environment_invalid", "before_start") from None


def _lab_archive(binding: BundleBinding, stage: str) -> Path:
    receipt = binding.environment_receipt(binding.execution_environment.digest)
    wheels = receipt.get("archive_wheels")
    if not isinstance(wheels, (list, tuple)):
        raise OwnerError("environment_invalid", stage)
    matches = [item for item in wheels if isinstance(item, Mapping) and item.get("name") == _LAB_DISTRIBUTION]
    if len(matches) != 1 or not isinstance(matches[0].get("path"), str):
        raise OwnerError("environment_invalid", stage)
    return Path(matches[0]["path"])


def _declared_module_hashes(
    path: Path, members: Mapping[str, str], stage: str
) -> Mapping[str, str]:
    try:
        info = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > _MAX_WHEEL_BYTES
        ):
            raise ValueError("invalid Lab wheel")
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_WHEEL_MEMBERS or len({item.filename for item in infos}) != len(infos):
                raise ValueError("invalid Lab wheel members")
            by_name = {item.filename: item for item in infos}
            result: dict[str, str] = {}
            for module, member in members.items():
                parts = PurePosixPath(member)
                item = by_name.get(member)
                mode = 0 if item is None else item.external_attr >> 16
                if (
                    item is None
                    or parts.is_absolute()
                    or ".." in parts.parts
                    or "\\" in member
                    or item.is_dir()
                    or item.file_size > _MAX_MODULE_BYTES
                    or stat.S_ISLNK(mode)
                ):
                    raise ValueError("missing or invalid executor module")
                data = archive.read(item)
                if len(data) != item.file_size:
                    raise ValueError("truncated executor module")
                result[module] = hashlib.sha256(data).hexdigest()
            return MappingProxyType(result)
    except OwnerError:
        raise
    except (OSError, BadZipFile, KeyError, RuntimeError, ValueError):
        raise OwnerError("environment_invalid", stage) from None


def declared_executor_identity(
    binding: BundleBinding,
    *,
    kind: str = "campaign",
    stage: str = "before_start",
) -> ExecutorIdentity:
    """Extract immutable expected identity without running the byte verifier."""
    stage = _stage(stage)
    if not isinstance(binding, BundleBinding):
        raise OwnerError("invalid_argument", stage)
    try:
        members = _EXECUTOR_MEMBERS[kind]
    except KeyError:
        raise OwnerError("invalid_argument", stage) from None
    lab = _distribution_pin(binding, _LAB_DISTRIBUTION)
    core = _distribution_pin(binding, _CORE_DISTRIBUTION)
    return ExecutorIdentity(
        environment_digest=binding.execution_environment.digest,
        lab_wheel_sha256=lab.wheel_sha256,
        core_wheel_sha256=core.wheel_sha256,
        module_hashes=_declared_module_hashes(_lab_archive(binding, stage), members, stage),
        kind=f"lab-{kind}-worker/1",
    )


def revalidate_executor_identity(
    binding: BundleBinding,
    declared: ExecutorIdentity,
    *,
    stage: str = "before_start",
) -> ExecutorIdentity:
    """Fully attest the installed executor after durable operation acceptance."""
    stage = _stage(stage)
    if not isinstance(binding, BundleBinding) or not isinstance(declared, ExecutorIdentity):
        raise OwnerError("invalid_argument", stage)
    try:
        kind = declared.kind.removeprefix("lab-").removesuffix("-worker/1")
        members = _EXECUTOR_MEMBERS[kind]
    except KeyError:
        raise OwnerError("invalid_argument", stage) from None
    expected = declared_executor_identity(binding, kind=kind, stage=stage)
    if declared != expected:
        raise OwnerError("environment_invalid", stage)
    captured = _recapture(binding, binding.execution_environment.digest, stage)
    receipt = captured.source_receipt
    try:
        lab_rows = [
            item for item in receipt["archive_wheels"] if item["name"] == _LAB_DISTRIBUTION
        ]
        if len(lab_rows) != 1:
            raise ValueError("missing Lab receipt")
        package_origin = Path(lab_rows[0]["module_paths"]["evolution_lab"])
        site_root = package_origin.parent.parent
        actual: dict[str, str] = {}
        for module, member in members.items():
            spec = find_spec(module)
            if spec is None or spec.origin is None:
                raise ValueError("missing installed executor module")
            origin = Path(spec.origin)
            info = origin.stat(follow_symlinks=False)
            # Same file by inode, as preparation compares origins: the probe and this
            # process may reach one site-packages through different directory aliases
            # (a venv's lib64 -> lib), so path prefixes cannot be compared.
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or not origin.samefile(site_root / member)
            ):
                raise ValueError("executor module origin mismatch")
            digest = hashlib.sha256(origin.read_bytes()).hexdigest()
            if digest != declared.module_hashes[module]:
                raise ValueError("executor module digest mismatch")
            actual[module] = digest
        if actual != dict(declared.module_hashes):
            raise ValueError("executor module key mismatch")
    except (KeyError, OSError, TypeError, ValueError):
        raise OwnerError("environment_invalid", stage) from None
    return declared


def rebind_evaluation_input(binding: BundleBinding, *, stage: str = "before_start"):
    """Rebuild only saved evaluation environment/scorer bindings."""
    stage = _stage(stage)
    if binding.source_kind == "campaign":
        from evolution_lab.preparation.evaluation import bind_campaign_evaluation

        try:
            return bind_campaign_evaluation(
                binding.manifest,
                bundle_root=binding.bundle_path,
                env=binding.base_environment,
                secret_names=binding.secret_names,
                stage="read",
            )
        except PreparationError as error:
            raise _from_preparation(error, stage) from None
    if binding.source_kind != "evaluation" or binding.evaluation_input is None:
        raise OwnerError("invalid_argument", stage)
    from evolution_lab.preparation.evaluation import bind_evaluation_input

    try:
        return bind_evaluation_input(
            _thaw(binding.evaluation_input),
            document=binding.entrypoint_path,
            manifest=binding.manifest,
            env=binding.base_environment,
            secret_names=binding.secret_names,
            stage="read",
        )
    except PreparationError as error:
        raise _from_preparation(error, stage) from None


def revalidate_prepared(
    storage: PreparationStorage,
    prepared_digest: str,
    declared: ExecutorIdentity,
    *,
    stage: str = "before_start",
) -> PreparedBinding:
    """Reopen saved bytes and apply the common environment and worker pin checks."""
    stage = _stage(stage)
    fresh = open_prepared(storage, prepared_digest, stage=stage)
    checked: set[str] = set()
    for environment in (
        fresh.bundle.execution_environment,
        fresh.bundle.target_environment,
    ):
        if environment.digest not in checked:
            revalidate_environment(fresh.bundle, environment.digest, stage=stage)
            checked.add(environment.digest)
    revalidate_executor_identity(fresh.bundle, declared, stage=stage)
    if fresh.kind == "evaluation":
        rebind_evaluation_input(fresh.bundle, stage=stage)
    return fresh
