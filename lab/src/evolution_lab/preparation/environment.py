"""Capture local, byte-checked environment descriptors without an application."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from evolution_lab._manifest_schema import DIAGNOSIS_LIMIT

from .models import EnvironmentDescriptor, EnvironmentInput, PreparationError


_PROBE = Path(__file__).with_name("_environment_probe.py")
_EXECUTION_DISTRIBUTIONS = frozenset({"evolution-lab", "evo-skills"})
_EXECUTION_MODULES = {
    "evolution-lab": "evolution_lab",
    "evo-skills": "evolution",
}
_LIMITATIONS = (
    "wheel archives, interpreter, and runtime files remain external prerequisites",
    "wheel members are compared as bytes; native system-library attestation is not provided",
    "undeclared or transitive dependencies are not resolved or attested; callers must declare retained wheel pins",
    "future Start rechecks environment prerequisites before execution",
)


@dataclass(frozen=True, slots=True)
class CapturedEnvironment:
    """A verified public descriptor and private JSON paths for later storage."""

    descriptor: EnvironmentDescriptor
    source_receipt_json: str

    @property
    def source_receipt(self) -> Mapping[str, Any]:
        return MappingProxyType(json.loads(self.source_receipt_json))


def _failure(code: str, detail: str | None = None) -> PreparationError:
    return PreparationError(code, "bundle", detail)


def _probe_input(value: EnvironmentInput) -> tuple[Path, dict[str, object]]:
    executable = Path(value.executable).absolute()
    wheel_paths = tuple(Path(path).absolute() for path in value.wheel_paths)
    runtime_files = tuple(Path(path).absolute() for path in value.runtime_files)
    if not wheel_paths:
        raise _failure("environment_unavailable")
    if len(set(wheel_paths)) != len(wheel_paths) or len(set(runtime_files)) != len(runtime_files):
        raise _failure("invalid_request")
    return executable, {
        "executable": str(executable),
        "wheel_paths": [str(path) for path in wheel_paths],
        "runtime_files": [str(path) for path in runtime_files],
    }


def _run_probe(executable: Path, request: dict[str, object]) -> Mapping[str, Any]:
    try:
        result = subprocess.run(
            [str(executable), "-I", "-S", str(_PROBE)],
            input=json.dumps(request, sort_keys=True),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            env={},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _failure("environment_unavailable") from None
    if result.returncode != 0:
        raise _failure("environment_unavailable")
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _failure("environment_unavailable") from None
    if not isinstance(response, dict):
        raise _failure("environment_unavailable")
    if response.get("status") == "environment_invalid":
        detail = response.get("detail")
        if not isinstance(detail, str) or not detail or len(detail) > DIAGNOSIS_LIMIT or not detail.isprintable():
            detail = None
        raise PreparationError("environment_invalid", "bundle", detail)
    if response.get("status") != "ok" or set(response) != {"status", "receipt"} or not isinstance(response["receipt"], dict):
        raise _failure("environment_unavailable")
    return response["receipt"]


def _validate_execution_pair(receipt: Mapping[str, Any]) -> None:
    distributions = receipt.get("archive_wheels")
    if not isinstance(distributions, list) or not all(
        isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("version"), str)
        for item in distributions
    ):
        raise _failure("environment_invalid")
    declared = {item["name"]: item["version"] for item in distributions}
    if len(declared) != len(distributions) or not _EXECUTION_DISTRIBUTIONS <= set(declared):
        raise _failure("environment_invalid", "the execution pair evolution-lab and evo-skills is incomplete")


def _validate_execution_origins(receipt: Mapping[str, Any]) -> None:
    distributions = receipt.get("archive_wheels")
    if not isinstance(distributions, list):
        raise _failure("environment_invalid")
    declared = {
        item.get("name"): item
        for item in distributions
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    try:
        for distribution, module in _EXECUTION_MODULES.items():
            paths = declared.get(distribution, {}).get("module_paths")
            spec = find_spec(module)
            if (
                not isinstance(paths, Mapping)
                or not isinstance(paths.get(module), str)
                or spec is None
                or spec.origin is None
                or not Path(spec.origin).samefile(paths[module])
            ):
                raise _failure("environment_invalid")
    except OSError:
        raise _failure("environment_invalid") from None


def _execution_pair(executable: Path, receipt: Mapping[str, Any]) -> None:
    if executable != Path(sys.executable).absolute():
        raise _failure("environment_invalid", "the declared executable is not this service's interpreter")
    _validate_execution_pair(receipt)
    _validate_execution_origins(receipt)


def _source_receipt(executable: Path, descriptor: EnvironmentDescriptor, receipt: Mapping[str, Any]) -> str:
    locations = {
        "schema": "lab-environment-source-receipt/1",
        "executable": str(executable),
        "archive_wheels": receipt["archive_wheels"],
        "runtime_files": [item["path"] for item in descriptor.to_payload()["runtime_files"]],
    }
    return json.dumps(locations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def capture_environment(value: EnvironmentInput, *, execution: bool) -> CapturedEnvironment:
    """Return verified local conditions; source locations remain in a private receipt.

    ``execution=True`` is reserved for the current Lab interpreter and requires
    the installed Lab/core pair among its declared, byte-checked wheel pins.
    ``False`` describes a future target only through its local files; it does
    not contact or import it.
    """
    if not isinstance(value, EnvironmentInput) or not isinstance(execution, bool):
        raise _failure("invalid_request")
    executable, request = _probe_input(value)
    receipt = _run_probe(executable, request)
    try:
        descriptor = EnvironmentDescriptor.from_payload({
            "schema": "lab-environment/1",
            "executable": receipt["executable"],
            "executable_sha256": receipt["executable_sha256"],
            "python_version": receipt["python_version"],
            "distributions": [
                {key: wheel[key] for key in ("name", "version", "wheel_sha256")}
                for wheel in receipt["archive_wheels"]
            ],
            "runtime_files": receipt["runtime_files"],
            "platform": receipt["platform"],
            "proof_profile": "wheel-members/1",
            "limitations": list(_LIMITATIONS),
        }, stage="bundle")
    except (KeyError, TypeError, PreparationError):
        raise _failure("environment_unavailable") from None
    if execution:
        _execution_pair(executable, receipt)
    return CapturedEnvironment(descriptor, _source_receipt(executable, descriptor, receipt))


def _recapture_environment(
    descriptor: EnvironmentDescriptor,
    source_receipt: Mapping[str, Any],
    *,
    execution: bool,
) -> CapturedEnvironment:
    """Re-run capture from retained prerequisite paths, never source inputs.

    This is an internal S2 seam.  The public descriptor remains unchanged; the
    private source receipt supplies only the executable, retained wheel archives,
    and runtime prerequisites that S1 recorded at publication.
    """
    try:
        if (
            not isinstance(descriptor, EnvironmentDescriptor)
            or not isinstance(source_receipt, Mapping)
            or set(source_receipt) != {
                "schema", "executable", "archive_wheels", "runtime_files"
            }
            or source_receipt["schema"] != "lab-environment-source-receipt/1"
            or source_receipt["executable"] != descriptor.executable
            or not isinstance(source_receipt["archive_wheels"], (list, tuple))
            or not isinstance(source_receipt["runtime_files"], (list, tuple))
        ):
            raise _failure("environment_invalid")
        wheels = source_receipt["archive_wheels"]
        if not all(
            isinstance(item, Mapping)
            and set(item) == {"path", "name", "version", "wheel_sha256", "module_paths"}
            and isinstance(item["path"], str)
            and isinstance(item["name"], str)
            and isinstance(item["version"], str)
            and isinstance(item["wheel_sha256"], str)
            and isinstance(item["module_paths"], Mapping)
            and all(isinstance(key, str) and isinstance(value, str)
                    for key, value in item["module_paths"].items())
            for item in wheels
        ):
            raise _failure("environment_invalid")
        expected_wheels = {
            pin.name: (pin.version, pin.wheel_sha256) for pin in descriptor.distributions
        }
        received_wheels = {
            item["name"]: (item["version"], item["wheel_sha256"]) for item in wheels
        }
        runtime_files = source_receipt["runtime_files"]
        if (
            len(received_wheels) != len(wheels)
            or received_wheels != expected_wheels
            or not all(isinstance(path, str) for path in runtime_files)
            or len(set(runtime_files)) != len(runtime_files)
            or set(runtime_files) != {item.path for item in descriptor.runtime_files}
        ):
            raise _failure("environment_invalid")
        captured = capture_environment(
            EnvironmentInput(
                executable=Path(source_receipt["executable"]),
                wheel_paths=tuple(Path(item["path"]) for item in wheels),
                runtime_files=tuple(Path(path) for path in runtime_files),
            ),
            execution=execution,
        )
    except PreparationError:
        raise
    except (KeyError, TypeError, ValueError):
        raise _failure("environment_invalid") from None
    if captured.descriptor.digest != descriptor.digest:
        raise _failure("environment_invalid")
    return captured
