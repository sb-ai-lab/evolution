"""The service-owned execution environment, captured once at startup (ADR-0009)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from evolution_lab.preparation import EnvironmentInput
from evolution_lab.preparation.environment import capture_environment
from evolution_lab.preparation.models import EnvironmentDescriptor


HEALTH_SCHEMA = "lab-service-health/2"
ENVIRONMENT_SCHEMA = "lab-service-environment/2"


@dataclass(frozen=True, slots=True)
class ServiceEnvironment:
    input: EnvironmentInput
    descriptor: EnvironmentDescriptor

    @property
    def digest(self) -> str:
        return self.descriptor.digest

    def to_payload(self) -> dict[str, Any]:
        """Digest, versions and interpreter path; no wheel or runtime-file paths (ADR-0011)."""
        descriptor = self.descriptor
        return {
            "schema": ENVIRONMENT_SCHEMA, "digest": self.digest,
            "executable": descriptor.executable,
            "python_version": descriptor.python_version,
            "executable_sha256": descriptor.executable_sha256,
            "platform": descriptor.platform.to_payload(),
            "distributions": [pin.to_payload() for pin in descriptor.distributions],
            "runtime_files": [{"sha256": item.sha256} for item in descriptor.runtime_files],
            "proof_profile": descriptor.proof_profile,
        }


def capture_service_environment(wheels, runtime_files) -> ServiceEnvironment:
    """Capture the execution pair of this interpreter; the executable is never a flag."""
    value = EnvironmentInput(Path(sys.executable), tuple(wheels), tuple(runtime_files))
    return ServiceEnvironment(value, capture_environment(value, execution=True).descriptor)


def expand_service_environment(environments, service: ServiceEnvironment | None):
    """Replace a leading marker; the Bundle stores an ordinary descriptor (ADR-0009)."""
    from .codecs import SERVICE_ENVIRONMENT
    from .errors import ServiceError

    environments = tuple(environments)
    if not environments or environments[0] is not SERVICE_ENVIRONMENT:
        return environments, None
    if service is None:
        raise ServiceError("service_environment_unconfigured")
    return (service.input, *environments[1:]), service.digest


def service_health_payload(health, environment: ServiceEnvironment | None) -> dict[str, Any]:
    payload = health.to_payload()
    payload["schema"] = HEALTH_SCHEMA
    payload["service_environment"] = None if environment is None else environment.to_payload()
    return payload
