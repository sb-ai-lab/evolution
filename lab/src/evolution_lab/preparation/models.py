"""Closed immutable models for durable preparation objects."""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from evolution_lab._manifest_schema import (
    BASELINE_IMPORT_UNSUPPORTED_MESSAGE, CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE, DIAGNOSIS_LIMIT, GROUPER_KIND_UNSUPPORTED_MESSAGE)


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DIST_SEPARATORS = re.compile(r"[-_.]+")
_ERROR_MESSAGES = {
    "invalid_request": "The preparation request is invalid.",
    "unsupported_schema": "The preparation schema is not supported.",
    "unsupported_input": "A declared preparation input is not supported.",
    "manifest_invalid": "The campaign manifest is invalid.",
    "artifacts_invalid": "The saved preparation artifacts are invalid.",
    "inputs_changed": "A declared input changed during preparation.",
    "digest_mismatch": "The saved preparation digest does not match its content.",
    "environment_invalid": "A declared environment does not match its identity.",
    "environment_unavailable": "A declared environment is unavailable.",
    "reserved_public_input": "The campaign contains a reserved public input.",
    "reserved_environment_name": "The environment name EVOLUTION_LAB_SCOPE is reserved by Lab.",
    "baseline_import_unsupported": BASELINE_IMPORT_UNSUPPORTED_MESSAGE,
    "linked_run_unsupported": "Lab does not continue a campaign from a linked run.",
    "grouper_kind_unsupported": GROUPER_KIND_UNSUPPORTED_MESSAGE,
    "corpus_inside_role_workspace": CORPUS_INSIDE_ROLE_WORKSPACE_MESSAGE,
    "not_found": "The requested preparation object was not found.",
    "io_error": "The preparation storage operation failed.",
}
_STAGES = frozenset({"bundle", "prepare", "read"})
#: Codes whose message may carry a bounded diagnosis after the fixed text.
DETAILED_CODES = frozenset({"manifest_invalid", "artifacts_invalid", "environment_invalid", "invalid_request",
                          "unsupported_input"})


@dataclass(frozen=True, slots=True)
class Failure:
    code: str
    stage: str
    message: str

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage, "message": self.message}


class PreparationError(ValueError):
    """Typed failure with a fixed public message and no exception/source details.

    ``detail`` is a bounded core diagnosis or a host-path-free input/environment
    diagnosis, appended only for ``DETAILED_CODES``.
    """

    def __init__(self, code: str, stage: str, detail: str | None = None):
        if code not in _ERROR_MESSAGES or stage not in _STAGES:
            raise ValueError("invalid preparation failure")
        if detail is not None and (code not in DETAILED_CODES or not isinstance(detail, str) or not detail
                                   or len(detail) > DIAGNOSIS_LIMIT or not detail.isprintable()):
            raise ValueError("invalid preparation failure detail")
        message = _ERROR_MESSAGES[code] if detail is None else f"{_ERROR_MESSAGES[code]} {detail}"
        self.failure = Failure(code=code, stage=stage, message=message)
        super().__init__(self.failure.message)

    @property
    def code(self) -> str:
        return self.failure.code

    @property
    def stage(self) -> str:
        return self.failure.stage


def _invalid(stage: str, code: str = "invalid_request") -> None:
    raise PreparationError(code, stage)


def _closed(payload: Any, keys: set[str], stage: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != keys:
        _invalid(stage)
    return payload


def _schema(payload: Mapping[str, Any], expected: str, stage: str) -> None:
    if payload.get("schema") != expected:
        _invalid(stage, "unsupported_schema")


def _string(value: Any, stage: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        _invalid(stage)
    return value


def _digest(value: Any, stage: str) -> str:
    value = _string(value, stage)
    if _DIGEST.fullmatch(value) is None:
        _invalid(stage)
    return value


def _integer(value: Any, stage: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _invalid(stage)
    if maximum is not None and value > maximum:
        _invalid(stage)
    return value


def _positive_number(value: Any, stage: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(stage)
    if not math.isfinite(value) or value <= 0:
        _invalid(stage)
    return value


def logical_path(value: Any, stage: str) -> str:
    value = _string(value, stage)
    path = PurePosixPath(value)
    if (
        value.startswith("/")
        or "\\" in value
        or value != path.as_posix()
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _invalid(stage)
    return value


def _strings(values: Any, stage: str, *, ordered_set: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        _invalid(stage)
    result = tuple(_string(value, stage) for value in values)
    if len(set(result)) != len(result):
        _invalid(stage)
    return tuple(sorted(result)) if ordered_set else result


def _optional_digest(value: Any, stage: str) -> str | None:
    return None if value is None else _digest(value, stage)


def _normalized_distribution_name(value: Any, stage: str) -> str:
    name = _DIST_SEPARATORS.sub("-", _string(value, stage)).lower()
    if not name:
        _invalid(stage)
    return name


def _freeze_json(value: Any, stage: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid(stage)
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            _invalid(stage)
        return MappingProxyType({key: _freeze_json(item, stage) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, stage) for item in value)
    _invalid(stage)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


PREPARED_EXECUTION_SCHEMA = "lab-prepared-execution/2"
_CALL_RETRY_KEYS = {"max_attempts", "turn_max_attempts"}


def parse_call_retries(value: Any, stage: str) -> dict[str, int] | None:
    """JSON form of core's CallRetries (bounds as core a24 `rollout.py`, unchanged since a17)."""
    if value is None:
        return None
    row = _closed(value, _CALL_RETRY_KEYS, stage)
    return {key: _integer(row[key], stage, minimum=1, maximum=10) for key in sorted(_CALL_RETRY_KEYS)}


@dataclass(frozen=True, slots=True)
class CampaignSource:
    manifest_path: Path
    launch_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        object.__setattr__(self, "launch_path", Path(self.launch_path))


@dataclass(frozen=True, slots=True)
class EvaluationSource:
    input_path: Path
    launch_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_path", Path(self.input_path))
        object.__setattr__(self, "launch_path", Path(self.launch_path))


@dataclass(frozen=True, slots=True)
class ProbeSource:
    """A target entry file and the ``lab-target-launch/2`` synthesized from it.

    The launch carries no ``environment_ref`` and ``identity.runtime_ref_digest``
    is ``None``; preparation fills both from the captured target environment.
    """

    entry_path: Path
    launch: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_path", Path(self.entry_path))
        object.__setattr__(self, "launch", copy.deepcopy(dict(self.launch)))


@dataclass(frozen=True, slots=True)
class EnvironmentInput:
    executable: Path
    wheel_paths: tuple[Path, ...]
    runtime_files: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable", Path(self.executable))
        object.__setattr__(self, "wheel_paths", tuple(Path(path) for path in self.wheel_paths))
        object.__setattr__(self, "runtime_files", tuple(Path(path) for path in self.runtime_files))


@dataclass(frozen=True, slots=True)
class SourceRoot:
    alias: str
    source_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not self.alias or "/" in self.alias or "\\" in self.alias:
            _invalid("bundle")
        object.__setattr__(self, "source_path", Path(self.source_path))


@dataclass(frozen=True, slots=True)
class PathBinding:
    document: Path
    pointer: str
    root_alias: str
    relative_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", Path(self.document))
        _string(self.pointer, "bundle")
        _string(self.root_alias, "bundle")
        logical_path(self.relative_path, "bundle")


@dataclass(frozen=True, slots=True)
class DistributionPin:
    name: str
    version: str
    wheel_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalized_distribution_name(self.name, "bundle"))
        _string(self.version, "bundle")
        _digest(self.wheel_sha256, "bundle")

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "bundle") -> "DistributionPin":
        row = _closed(payload, {"name", "version", "wheel_sha256"}, stage)
        try:
            return cls(
                _normalized_distribution_name(row["name"], stage),
                _string(row["version"], stage),
                _digest(row["wheel_sha256"], stage),
            )
        except PreparationError as error:
            if error.stage != stage:
                raise PreparationError(error.code, stage) from None
            raise

    def to_payload(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "wheel_sha256": self.wheel_sha256}


@dataclass(frozen=True, slots=True)
class RuntimeFile:
    path: str
    sha256: str

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "bundle") -> "RuntimeFile":
        row = _closed(payload, {"path", "sha256"}, stage)
        return cls(_string(row["path"], stage), _digest(row["sha256"], stage))

    def to_payload(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class PlatformIdentity:
    system: str
    machine: str

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "bundle") -> "PlatformIdentity":
        row = _closed(payload, {"system", "machine"}, stage)
        return cls(_string(row["system"], stage), _string(row["machine"], stage))

    def to_payload(self) -> dict[str, str]:
        return {"system": self.system, "machine": self.machine}


@dataclass(frozen=True, slots=True)
class EnvironmentDescriptor:
    executable: str
    executable_sha256: str
    python_version: str | None
    distributions: tuple[DistributionPin, ...]
    runtime_files: tuple[RuntimeFile, ...]
    platform: PlatformIdentity
    proof_profile: str
    limitations: tuple[str, ...]
    schema: str = "lab-environment/1"

    def __post_init__(self) -> None:
        _string(self.executable, "bundle")
        _digest(self.executable_sha256, "bundle")
        if self.python_version is not None:
            _string(self.python_version, "bundle")
        pins = tuple(sorted(self.distributions, key=lambda pin: pin.name))
        if len({pin.name for pin in pins}) != len(pins):
            _invalid("bundle")
        object.__setattr__(self, "distributions", pins)
        object.__setattr__(self, "runtime_files", tuple(self.runtime_files))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        if self.proof_profile != "wheel-members/1" or self.schema != "lab-environment/1":
            _invalid("bundle", "unsupported_schema")

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "bundle") -> "EnvironmentDescriptor":
        keys = {
            "schema", "executable", "executable_sha256", "python_version", "distributions",
            "runtime_files", "platform", "proof_profile", "limitations",
        }
        if not isinstance(payload, Mapping):
            _invalid(stage)
        _schema(payload, "lab-environment/1", stage)
        row = _closed(payload, keys, stage)
        if row["python_version"] is not None:
            _string(row["python_version"], stage)
        if not isinstance(row["distributions"], list) or not isinstance(row["runtime_files"], list):
            _invalid(stage)
        try:
            return cls(
                executable=_string(row["executable"], stage),
                executable_sha256=_digest(row["executable_sha256"], stage),
                python_version=row["python_version"],
                distributions=tuple(DistributionPin.from_payload(pin, stage) for pin in row["distributions"]),
                runtime_files=tuple(RuntimeFile.from_payload(item, stage) for item in row["runtime_files"]),
                platform=PlatformIdentity.from_payload(row["platform"], stage),
                proof_profile=_string(row["proof_profile"], stage),
                limitations=_strings(row["limitations"], stage),
            )
        except PreparationError as error:
            if error.stage != stage:
                raise PreparationError(error.code, stage) from None
            raise

    @property
    def digest(self) -> str:
        from .storage import descriptor_digest
        return descriptor_digest(self)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "executable": self.executable,
            "executable_sha256": self.executable_sha256,
            "python_version": self.python_version,
            "distributions": [pin.to_payload() for pin in self.distributions],
            "runtime_files": [item.to_payload() for item in self.runtime_files],
            "platform": self.platform.to_payload(),
            "proof_profile": self.proof_profile,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class BundleEntry:
    path: str
    type: str
    executable: int
    size: int | None
    sha256: str | None

    def __post_init__(self) -> None:
        logical_path(self.path, "bundle")
        if self.path == "descriptor.json" or self.type not in {"file", "directory"}:
            _invalid("bundle")
        executable = _integer(self.executable, "bundle", maximum=0o111)
        if executable & ~0o111:
            _invalid("bundle")
        if self.type == "directory":
            if self.size is not None or self.sha256 is not None:
                _invalid("bundle")
        else:
            _integer(self.size, "bundle")
            _digest(self.sha256, "bundle")

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "bundle") -> "BundleEntry":
        row = _closed(payload, {"path", "type", "executable", "size", "sha256"}, stage)
        path = logical_path(row["path"], stage)
        if path == "descriptor.json":
            _invalid(stage)
        entry_type = row["type"]
        if entry_type not in {"file", "directory"}:
            _invalid(stage)
        executable = _integer(row["executable"], stage, maximum=0o111)
        if executable & ~0o111:
            _invalid(stage)
        if entry_type == "directory":
            if row["size"] is not None or row["sha256"] is not None:
                _invalid(stage)
            return cls(path, entry_type, executable, None, None)
        return cls(
            path,
            entry_type,
            executable,
            _integer(row["size"], stage),
            _digest(row["sha256"], stage),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path, "type": self.type, "executable": self.executable,
            "size": self.size, "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class BundleDescriptor:
    entrypoint: str
    conformance_entrypoint: str
    launch: str
    environments: tuple[EnvironmentDescriptor, ...]
    env: Mapping[str, str]
    secret_names: tuple[str, ...]
    entries: tuple[BundleEntry, ...]
    source_kind: str = "campaign"
    schema: str = "lab-bundle/1"

    def __post_init__(self) -> None:
        if self.schema != "lab-bundle/1":
            _invalid("bundle", "unsupported_schema")
        if self.source_kind not in {"campaign", "evaluation", "probe"}:
            _invalid("bundle", "unsupported_schema")
        for path in (self.entrypoint, self.conformance_entrypoint, self.launch):
            logical_path(path, "bundle")
        if (self.source_kind == "campaign") != (self.conformance_entrypoint == self.entrypoint):
            _invalid("bundle")
        environments = tuple(self.environments)
        if not environments or len({environment.digest for environment in environments}) != len(environments):
            _invalid("bundle")
        env = dict(self.env)
        if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in env.items()):
            _invalid("bundle")
        secret_names = tuple(sorted(self.secret_names))
        if len(set(secret_names)) != len(secret_names) or set(secret_names) & set(env):
            _invalid("bundle")
        entries = tuple(sorted(self.entries, key=lambda entry: entry.path))
        if not entries or len({entry.path for entry in entries}) != len(entries):
            _invalid("bundle")
        entry_types = {entry.path: entry.type for entry in entries}
        if any(entry_types.get(path) != "file" for path in (self.entrypoint, self.conformance_entrypoint, self.launch)):
            _invalid("bundle")
        for entry in entries:
            parents = PurePosixPath(entry.path).parents
            if any(str(parent) != "." and entry_types.get(str(parent)) != "directory" for parent in parents):
                _invalid("bundle")
        object.__setattr__(self, "environments", environments)
        object.__setattr__(self, "env", MappingProxyType(env))
        object.__setattr__(self, "secret_names", secret_names)
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "bundle") -> "BundleDescriptor":
        keys = {
            "schema", "source_kind", "entrypoint", "conformance_entrypoint", "launch",
            "environments", "env", "secret_names", "entries",
        }
        if not isinstance(payload, Mapping):
            _invalid(stage)
        _schema(payload, "lab-bundle/1", stage)
        row = _closed(payload, keys, stage)
        if row["source_kind"] not in {"campaign", "evaluation", "probe"}:
            _invalid(stage, "unsupported_schema")
        if not isinstance(row["environments"], list) or not isinstance(row["entries"], list):
            _invalid(stage)
        if not isinstance(row["env"], Mapping):
            _invalid(stage)
        try:
            return cls(
                source_kind=row["source_kind"],
                entrypoint=logical_path(row["entrypoint"], stage),
                conformance_entrypoint=logical_path(row["conformance_entrypoint"], stage),
                launch=logical_path(row["launch"], stage),
                environments=tuple(EnvironmentDescriptor.from_payload(value, stage) for value in row["environments"]),
                env=MappingProxyType({
                    _string(key, stage): _string(value, stage, empty=True)
                    for key, value in row["env"].items()
                }),
                secret_names=_strings(row["secret_names"], stage, ordered_set=True),
                entries=tuple(BundleEntry.from_payload(value, stage) for value in row["entries"]),
            )
        except PreparationError as error:
            if error.stage != stage:
                raise PreparationError(error.code, stage) from None
            raise

    @property
    def digest(self) -> str:
        from .storage import descriptor_digest
        return descriptor_digest(self)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_kind": self.source_kind,
            "entrypoint": self.entrypoint,
            "conformance_entrypoint": self.conformance_entrypoint,
            "launch": self.launch,
            "environments": [value.to_payload() for value in self.environments],
            "env": dict(self.env),
            "secret_names": list(self.secret_names),
            "entries": [value.to_payload() for value in self.entries],
        }


@dataclass(frozen=True, slots=True)
class TargetBinding:
    launch_digest: str
    environment_digest: str
    base_url: str
    policy_digest: str
    target_id: str
    contract_digest: str | None
    settings_hash: str | None
    runtime_ref_digest: str | None
    seed_mode: str

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "prepare") -> "TargetBinding":
        keys = {
            "launch_digest", "environment_digest", "base_url", "policy_digest", "target_id",
            "contract_digest", "settings_hash", "runtime_ref_digest", "seed_mode",
        }
        row = _closed(payload, keys, stage)
        return cls(
            launch_digest=_digest(row["launch_digest"], stage),
            environment_digest=_digest(row["environment_digest"], stage),
            base_url=_string(row["base_url"], stage),
            policy_digest=_digest(row["policy_digest"], stage),
            target_id=_string(row["target_id"], stage),
            contract_digest=_optional_digest(row["contract_digest"], stage),
            settings_hash=_optional_digest(row["settings_hash"], stage),
            runtime_ref_digest=_optional_digest(row["runtime_ref_digest"], stage),
            seed_mode=_string(row["seed_mode"], stage),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "launch_digest": self.launch_digest,
            "environment_digest": self.environment_digest,
            "base_url": self.base_url,
            "policy_digest": self.policy_digest,
            "target_id": self.target_id,
            "contract_digest": self.contract_digest,
            "settings_hash": self.settings_hash,
            "runtime_ref_digest": self.runtime_ref_digest,
            "seed_mode": self.seed_mode,
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    stop_grace_seconds: int | float
    cleanup_wait_seconds: int | float

    def __post_init__(self) -> None:
        _positive_number(self.stop_grace_seconds, "prepare")
        _positive_number(self.cleanup_wait_seconds, "prepare")

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "prepare") -> "RuntimePolicy":
        row = _closed(payload, {"stop_grace_seconds", "cleanup_wait_seconds"}, stage)
        return cls(
            _positive_number(row["stop_grace_seconds"], stage),
            _positive_number(row["cleanup_wait_seconds"], stage),
        )

    def to_payload(self) -> dict[str, int | float]:
        return {
            "stop_grace_seconds": self.stop_grace_seconds,
            "cleanup_wait_seconds": self.cleanup_wait_seconds,
        }


def parse_sample_retries(value: Any, stage: str) -> int | None:
    return None if value is None else _integer(value, stage, minimum=0, maximum=8)


def parse_min_coverage(value: Any, stage: str) -> float | None:
    if value is None:
        return None
    # Compare numeric bounds before math.isfinite converts an integer to float.
    if isinstance(value, (int, float)) and not 0 < value <= 1:
        _invalid(stage)
    return _positive_number(value, stage)


UNMEASURED_LIMIT_DETAIL = "max_consecutive_unmeasured must be an integer >= 1 or off."


def parse_max_consecutive_unmeasured(value: Any, stage: str) -> int | str:
    if value is None:
        return 3
    if value == "off" or type(value) is int and value >= 1:
        return value
    raise PreparationError("invalid_request", stage, UNMEASURED_LIMIT_DETAIL)


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    case_ids: tuple[str, ...]
    repeat_seeds: tuple[int | None, ...]
    evaluation_set_id: str
    max_turns: int
    timeout_seconds: int | float
    metric_plan: Mapping[str, Any]
    max_failed_turns: int = 1
    call_retries: Mapping[str, int] | None = None
    concurrency: int = 1
    sample_retries: int | None = None
    min_coverage: float | None = None
    scorer_concurrency: int = 1
    max_consecutive_unmeasured: int | str | None = 3
    _legacy_min_coverage: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        case_ids = _strings(self.case_ids, "prepare")
        if not case_ids:
            _invalid("prepare")
        repeat_seeds = tuple(self.repeat_seeds)
        if not repeat_seeds or any(
            seed is not None and (isinstance(seed, bool) or not isinstance(seed, int))
            for seed in repeat_seeds
        ):
            _invalid("prepare")
        _string(self.evaluation_set_id, "prepare")
        _integer(self.max_turns, "prepare", minimum=1)
        _positive_number(self.timeout_seconds, "prepare")
        if not isinstance(self.metric_plan, Mapping) or not self.metric_plan:
            _invalid("prepare")
        _integer(self.max_failed_turns, "prepare", minimum=1)
        retries = parse_call_retries(self.call_retries, "prepare")
        object.__setattr__(self, "call_retries", None if retries is None else _freeze_json(retries, "prepare"))
        _integer(self.concurrency, "prepare", minimum=1)
        _integer(self.scorer_concurrency, "prepare", minimum=1)
        parse_sample_retries(self.sample_retries, "prepare")
        object.__setattr__(self, "max_consecutive_unmeasured",
                           parse_max_consecutive_unmeasured(self.max_consecutive_unmeasured, "prepare"))
        coverage = parse_min_coverage(self.min_coverage, "prepare")
        object.__setattr__(self, "_legacy_min_coverage", coverage is None)
        object.__setattr__(self, "min_coverage", 1.0 if coverage is None else coverage)
        object.__setattr__(self, "case_ids", case_ids)
        object.__setattr__(self, "repeat_seeds", repeat_seeds)
        object.__setattr__(self, "metric_plan", _freeze_json(self.metric_plan, "prepare"))

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "prepare") -> "EvaluationPlan":
        keys = {
            "case_ids", "repeat_seeds", "evaluation_set_id", "max_turns",
            "timeout_seconds", "metric_plan", "max_failed_turns", "call_retries", "concurrency",
        }
        optional = {"sample_retries", "min_coverage", "scorer_concurrency", "max_consecutive_unmeasured"}
        if not isinstance(payload, Mapping) or not keys <= set(payload) <= keys | optional:
            _invalid(stage)
        row = payload
        # Missing coverage is exclusively the pre-dev60 shape. Explicit null
        # cannot turn a newly written plan into a historical descriptor.
        if ("min_coverage" in row and row["min_coverage"] is None
                or "min_coverage" not in row and set(row) - {"max_consecutive_unmeasured"} != keys):
            _invalid(stage)
        if not isinstance(row["repeat_seeds"], (list, tuple)):
            _invalid(stage)
        try:
            return cls(
                case_ids=_strings(row["case_ids"], stage),
                repeat_seeds=tuple(row["repeat_seeds"]),
                evaluation_set_id=_string(row["evaluation_set_id"], stage),
                max_turns=_integer(row["max_turns"], stage, minimum=1),
                timeout_seconds=_positive_number(row["timeout_seconds"], stage),
                metric_plan=row["metric_plan"],
                max_failed_turns=_integer(row["max_failed_turns"], stage, minimum=1),
                call_retries=parse_call_retries(row["call_retries"], stage),
                concurrency=_integer(row["concurrency"], stage, minimum=1),
                sample_retries=parse_sample_retries(row.get("sample_retries"), stage),
                min_coverage=parse_min_coverage(row.get("min_coverage"), stage),
                scorer_concurrency=_integer(row.get("scorer_concurrency", 1), stage, minimum=1),
                max_consecutive_unmeasured=parse_max_consecutive_unmeasured(row.get("max_consecutive_unmeasured"), stage),
            )
        except PreparationError as error:
            if error.stage != stage:
                raise PreparationError(error.code, stage) from None
            raise

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "case_ids": list(self.case_ids),
            "repeat_seeds": list(self.repeat_seeds),
            "evaluation_set_id": self.evaluation_set_id,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "metric_plan": _thaw_json(self.metric_plan),
            "max_failed_turns": self.max_failed_turns,
            "call_retries": None if self.call_retries is None else _thaw_json(self.call_retries),
            "concurrency": self.concurrency,
        }

        if not self._legacy_min_coverage:
            payload["min_coverage"] = self.min_coverage
        if self.sample_retries is not None:
            payload["sample_retries"] = self.sample_retries
        if self.scorer_concurrency != 1:
            payload["scorer_concurrency"] = self.scorer_concurrency
        # Historical plans used the implicit threshold 3. Preserve their bytes
        # and digest, as for the implicit scorer concurrency above.
        if self.max_consecutive_unmeasured != 3:
            payload["max_consecutive_unmeasured"] = self.max_consecutive_unmeasured
        return payload


@dataclass(frozen=True, slots=True)
class PreparedExecutionDescriptor:
    bundle_digest: str
    entrypoint: str
    target_binding: TargetBinding
    execution_environment: str
    secret_names: tuple[str, ...]
    runtime_policy: RuntimePolicy
    evaluation: EvaluationPlan | None = None
    kind: str = "campaign"
    schema: str = PREPARED_EXECUTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PREPARED_EXECUTION_SCHEMA or self.kind not in {"campaign", "evaluation"}:
            _invalid("prepare", "unsupported_schema")
        if (self.kind == "campaign") != (self.evaluation is None):
            _invalid("prepare", "unsupported_schema")
        _digest(self.bundle_digest, "prepare")
        logical_path(self.entrypoint, "prepare")
        _digest(self.execution_environment, "prepare")
        names = tuple(sorted(self.secret_names))
        if len(names) != len(set(names)):
            _invalid("prepare")
        object.__setattr__(self, "secret_names", names)

    @classmethod
    def from_payload(cls, payload: Any, stage: str = "prepare") -> "PreparedExecutionDescriptor":
        keys = {
            "schema", "bundle_digest", "kind", "entrypoint", "target_binding",
            "execution_environment", "secret_names", "runtime_policy", "evaluation",
        }
        if not isinstance(payload, Mapping):
            _invalid(stage)
        _schema(payload, PREPARED_EXECUTION_SCHEMA, stage)
        row = _closed(payload, keys, stage)
        if row["kind"] not in {"campaign", "evaluation"}:
            _invalid(stage, "unsupported_schema")
        if (row["kind"] == "campaign") != (row["evaluation"] is None):
            _invalid(stage, "unsupported_schema")
        try:
            return cls(
                bundle_digest=_digest(row["bundle_digest"], stage),
                kind=row["kind"],
                entrypoint=logical_path(row["entrypoint"], stage),
                target_binding=TargetBinding.from_payload(row["target_binding"], stage),
                execution_environment=_digest(row["execution_environment"], stage),
                secret_names=_strings(row["secret_names"], stage, ordered_set=True),
                runtime_policy=RuntimePolicy.from_payload(row["runtime_policy"], stage),
                evaluation=(
                    None
                    if row["evaluation"] is None
                    else EvaluationPlan.from_payload(row["evaluation"], stage)
                ),
            )
        except PreparationError as error:
            if error.stage != stage:
                raise PreparationError(error.code, stage) from None
            raise

    @property
    def digest(self) -> str:
        from .storage import descriptor_digest
        return descriptor_digest(self)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bundle_digest": self.bundle_digest,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "target_binding": self.target_binding.to_payload(),
            "execution_environment": self.execution_environment,
            "secret_names": list(self.secret_names),
            "runtime_policy": self.runtime_policy.to_payload(),
            "evaluation": None if self.evaluation is None else self.evaluation.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class BundleWarning:
    """A non-refusing preparation warning; never blocks Bundle creation."""

    code: str
    detail: Mapping[str, str | None]

    def __post_init__(self) -> None:
        _string(self.code, "bundle")
        if not isinstance(self.detail, Mapping) or any(
            not isinstance(key, str) or not key or not (value is None or isinstance(value, str))
            for key, value in self.detail.items()
        ):
            _invalid("bundle")
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class BundleSummary:
    digest: str
    source_kind: str
    entrypoint: str
    launch_digest: str
    environment_digests: tuple[str, ...]
    secret_names: tuple[str, ...]
    entry_count: int
    limitations: tuple[str, ...]
    warnings: tuple[BundleWarning, ...] = ()
    measurement_basis: str | None = None
    schema: str = "lab-bundle-summary/3"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "digest": self.digest,
            "source_kind": self.source_kind,
            "entrypoint": self.entrypoint,
            "launch_digest": self.launch_digest,
            "measurement_basis": self.measurement_basis,
            "environment_digests": list(self.environment_digests),
            "secret_names": list(self.secret_names),
            "entry_count": self.entry_count,
            "limitations": list(self.limitations),
            "warnings": [warning.to_payload() for warning in self.warnings],
        }


@dataclass(frozen=True, slots=True)
class PreparedSummary:
    digest: str
    bundle_digest: str
    kind: str
    target_binding: TargetBinding
    execution_environment: str
    secret_names: tuple[str, ...]
    runtime_policy: RuntimePolicy
    evaluation: EvaluationPlan | None = None
    schema: str = "lab-prepared-summary/1"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "digest": self.digest,
            "bundle_digest": self.bundle_digest,
            "kind": self.kind,
            "target_binding": self.target_binding.to_payload(),
            "execution_environment": self.execution_environment,
            "secret_names": list(self.secret_names),
            "runtime_policy": self.runtime_policy.to_payload(),
            "evaluation": None if self.evaluation is None else self.evaluation.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class StoredBundle:
    digest: str
    path: Path
    descriptor: BundleDescriptor

    def summary(self, warnings: tuple[BundleWarning, ...] = ()) -> BundleSummary:
        from .basis import measurement_basis

        launch_entry = next(entry for entry in self.descriptor.entries if entry.path == self.descriptor.launch)
        limitations = tuple(
            limitation
            for environment in self.descriptor.environments
            for limitation in environment.limitations
        )
        return BundleSummary(
            digest=self.digest,
            source_kind=self.descriptor.source_kind,
            entrypoint=self.descriptor.entrypoint,
            launch_digest=launch_entry.sha256 or "",
            environment_digests=tuple(environment.digest for environment in self.descriptor.environments),
            secret_names=self.descriptor.secret_names,
            entry_count=len(self.descriptor.entries),
            limitations=limitations,
            warnings=warnings,
            measurement_basis=measurement_basis(self),
        )


@dataclass(frozen=True, slots=True)
class StoredPrepared:
    digest: str
    path: Path
    descriptor: PreparedExecutionDescriptor

    def summary(self) -> PreparedSummary:
        return PreparedSummary(
            digest=self.digest,
            bundle_digest=self.descriptor.bundle_digest,
            kind=self.descriptor.kind,
            target_binding=self.descriptor.target_binding,
            execution_environment=self.descriptor.execution_environment,
            secret_names=self.descriptor.secret_names,
            runtime_policy=self.descriptor.runtime_policy,
            evaluation=self.descriptor.evaluation,
        )
