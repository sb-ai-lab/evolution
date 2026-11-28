"""Closed, secret-free value objects shared by the H1 client implementation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping

from evolution.tree import ManifestV2

from evolution_lab.provenance import FilePin
from evolution_lab.recipe import PreparedInputs


@dataclass(frozen=True)
class Failure:
    code: str
    stage: str

    def public(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage}


class ClientError(RuntimeError):
    """An expected client refusal carrying only its closed public failure; ``detail`` is core's diagnosis of a
    refused manifest or corpus (spec 2026-10-09 §3), outside ``Failure`` (closed form unchanged)."""

    def __init__(self, failure: Failure, detail: str | None = None) -> None:
        self.failure = failure
        self.detail = detail
        super().__init__(failure.code if detail is None else f"{failure.code}: {detail}")


@dataclass(frozen=True)
class Connection:
    manifest_digest: str
    editor_kind: str
    _manifest_pin: FilePin = field(repr=False, compare=False)
    _manifest: ManifestV2 = field(repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "schema": "lab-connection/1",
            "manifest_digest": self.manifest_digest,
            "editor_kind": self.editor_kind,
        }


@dataclass(frozen=True)
class CheckResult:
    state: str
    failure: Failure | None

    def public(self) -> dict[str, Any]:
        return {
            "schema": "lab-check/1",
            "state": self.state,
            "failure": None if self.failure is None else self.failure.public(),
        }


@dataclass(frozen=True)
class PreparedCampaign:
    manifest_digest: str
    candidate_digest: str
    dependency_digest: str
    case_count: int
    distributions: tuple[tuple[str, str, str], ...]
    _inputs: PreparedInputs = field(repr=False, compare=False)
    _environment: Mapping[str, str] = field(repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "schema": "lab-prepared/1",
            "manifest_digest": self.manifest_digest,
            "seed": {
                "candidate_digest": self.candidate_digest,
                "dependency_digest": self.dependency_digest,
            },
            "case_count": self.case_count,
            "distributions": [
                {"name": name, "version": version, "wheel_sha256": sha256}
                for name, version, sha256 in self.distributions
            ],
        }


def make_prepared_campaign(
    manifest_digest: str, inputs: PreparedInputs, environment: Mapping[str, str]
) -> PreparedCampaign:
    distributions = tuple((pin.name, pin.version, pin.sha256) for pin in inputs.wheels)
    return PreparedCampaign(
        manifest_digest=manifest_digest,
        candidate_digest=inputs.seed.candidate_digest,
        dependency_digest=inputs.seed.dependency_digest,
        case_count=len(inputs.cases),
        distributions=distributions,
        _inputs=inputs,
        _environment=environment,
    )


def prepared_inputs(value: PreparedCampaign) -> PreparedInputs:
    return value._inputs


def prepared_environment(value: PreparedCampaign) -> Mapping[str, str]:
    return value._environment


@dataclass(frozen=True)
class ExecutionSnapshot:
    operation_id: str
    state: str
    stage: str
    launched: bool | None
    exit_code: int | None
    stop_requested: bool
    stop_reason: str | None
    stop_effect: str
    core_force_requested: bool | None
    cleanup_complete: bool | None
    failure: Failure | None
    secondary_failures: tuple[Failure, ...]

    def public(self) -> dict[str, Any]:
        return {
            "schema": "lab-execution-result/1",
            "operation_id": self.operation_id,
            "state": self.state,
            "stage": self.stage,
            "launched": self.launched,
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "stop_reason": self.stop_reason,
            "stop_effect": self.stop_effect,
            "core_force_requested": self.core_force_requested,
            "cleanup_complete": self.cleanup_complete,
            "failure": None if self.failure is None else self.failure.public(),
            "secondary_failures": [item.public() for item in self.secondary_failures],
        }


@dataclass(frozen=True)
class RunResult(ExecutionSnapshot):
    persistence_state: str
    persistence_failure: Failure | None

    def public(self) -> dict[str, Any]:
        result = super().public()
        result["schema"] = "lab-run-result/1"
        result["persistence"] = {
            "state": self.persistence_state,
            "failure": None if self.persistence_failure is None else self.persistence_failure.public(),
        }
        return result


@dataclass(frozen=True)
class StatusSnapshot:
    operation_id: str
    state: str
    stage: str
    launched: bool | None
    exit_code: int | None
    stop_requested: bool
    cleanup_complete: bool | None
    result: RunResult | None
    campaign_availability: str
    campaign_projection: Any
    campaign_failure: Failure | None
    _projection: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_projection", deepcopy(self.campaign_projection))

    def public(self) -> dict[str, Any]:
        return {
            "schema": "lab-status/1",
            "operation_id": self.operation_id,
            "state": self.state,
            "stage": self.stage,
            "launched": self.launched,
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "cleanup_complete": self.cleanup_complete,
            "result": None if self.result is None else self.result.public(),
            "campaign": {
                "availability": self.campaign_availability,
                "projection": deepcopy(self._projection),
                "failure": None if self.campaign_failure is None else self.campaign_failure.public(),
            },
        }


@dataclass(frozen=True)
class StopReceipt:
    operation_id: str
    disposition: str

    def public(self) -> dict[str, str]:
        return {
            "schema": "lab-stop/1",
            "operation_id": self.operation_id,
            "disposition": self.disposition,
        }
