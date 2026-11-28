"""Agent-neutral preparation and explicit target check for native campaigns."""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evolution.tree import ManifestError, load_manifest_v2
from evolution.tree.artifacts.artifact_policy import scan_tree
from evolution.tree.target.http_adapter import HttpSessionAdapter
from evolution.tree.roles.config import AgentCliEditorSection, ChatEditorSection
from evolution.tree.execution.config import CommandSection
from evolution.tree.campaign_policy import PolicyError

from evolution_lab._client_models import (
    CheckResult,
    ClientError,
    Connection,
    Failure,
    PreparedCampaign,
    RunResult,
    StatusSnapshot,
    StopReceipt,
    make_prepared_campaign,
    prepared_environment,
    prepared_inputs,
)
from evolution_lab._manifest_schema import (
    CORPUS_INSIDE_ROLE_WORKSPACE, GROUPER_KIND_UNSUPPORTED, LINKED_RUN_UNSUPPORTED, core_diagnosis,
    corpus_inside_role_workspace, has_unsupported_grouper,
    LEGACY_CAMPAIGN_MANIFEST_SCHEMAS, admits_campaign_manifest, is_linked_run,
)
from evolution_lab._target_preflight import preflight_failure_code, run_target_preflight
from evolution_lab.provenance import (
    FilePin,
    InputChanged,
    IsolatedEnvironmentError,
    verify_isolated,
)
from evolution_lab.recipe import (
    PreparationArtifactsInvalid,
    PreparationEnvironmentInvalid,
    prepare_inputs,
)


_REQUIRED_DISTRIBUTIONS = frozenset({"evolution-lab", "evo-skills"})


def _error(code: str, stage: str, detail: str | None = None) -> ClientError:
    return ClientError(Failure(code, stage), detail)


def _connection(value: Any, stage: str) -> Connection:
    if not isinstance(value, Connection):
        raise _error("invalid_argument", stage)
    return value


def _editor_kind(manifest) -> str:
    assert manifest.workflow is not None
    editor = manifest.workflow.editor
    if isinstance(editor, CommandSection):
        return "subprocess"
    if isinstance(editor, ChatEditorSection):
        return "chat"
    if isinstance(editor, AgentCliEditorSection):
        return "agent_cli"
    raise _error("manifest_invalid", "connect")


def connect(manifest_path: os.PathLike[str] | str) -> Connection:
    """Load one native manifest and pin its byte identity (H1: /15 only)."""
    return _connect(manifest_path, LEGACY_CAMPAIGN_MANIFEST_SCHEMAS)


def _connect(manifest_path: os.PathLike[str] | str, schemas: frozenset[str]) -> Connection:
    """``connect`` with the admitted schema set explicit; the Owner passes ``CAMPAIGN_MANIFEST_SCHEMAS``."""
    if isinstance(manifest_path, (str, os.PathLike)):
        path = Path(manifest_path)
    else:
        raise _error("invalid_argument", "connect")
    try:
        pin = FilePin.capture(path)
        manifest = load_manifest_v2(pin.path)
        if not admits_campaign_manifest(manifest, schemas):
            raise _error("manifest_invalid", "connect")
        if is_linked_run(manifest):
            raise _error(LINKED_RUN_UNSUPPORTED, "connect")
        if has_unsupported_grouper(manifest):
            raise _error(GROUPER_KIND_UNSUPPORTED, "connect")
        if corpus_inside_role_workspace(manifest):
            raise _error(CORPUS_INSIDE_ROLE_WORKSPACE, "connect")
        kind = _editor_kind(manifest)
    except ClientError:
        raise
    except OSError as exc:
        raise _error("io_error", "connect") from exc
    except (ManifestError, PolicyError, ValueError) as exc:
        raise _error("manifest_invalid", "connect", core_diagnosis(exc)) from exc
    return Connection(pin.sha256, kind, pin, manifest)


def _verify_connection(connection: Connection, stage: str) -> None:
    try:
        connection._manifest_pin.verify()
    except InputChanged as exc:
        raise _error("inputs_changed", stage) from exc


def _check_failure(exc: BaseException) -> Failure:
    return Failure(preflight_failure_code(exc), "check")


async def check(connection: Connection) -> CheckResult:
    """Run the explicit pinned-core health preflight without creating a campaign."""
    connection = _connection(connection, "check")
    try:
        _verify_connection(connection, "check")
        manifest = connection._manifest
        await run_target_preflight(
            manifest, scan=scan_tree, adapter_type=HttpSessionAdapter
        )
    except ClientError as exc:
        if exc.failure.code == "invalid_argument":
            raise
        return CheckResult("failed", exc.failure)
    except Exception as exc:
        return CheckResult("failed", _check_failure(exc))
    return CheckResult("passed", None)


def _paths(value: Any, stage: str) -> tuple[Path, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error("invalid_argument", stage)
    try:
        return tuple(Path(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise _error("invalid_argument", stage) from exc


def _environment(value: Any) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise _error("invalid_argument", "prepare")
    return value


def _distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _raise_prepare_failure(
    connection: Connection, code: str, cause: BaseException
) -> None:
    _verify_connection(connection, "prepare")
    detail = core_diagnosis(cause) if code in {"manifest_invalid", "artifacts_invalid"} else None
    raise _error(code, "prepare", detail) from cause


def prepare(
    connection: Connection,
    *,
    wheel_paths: Sequence[os.PathLike[str] | str],
    extra_paths: Sequence[os.PathLike[str] | str] = (),
    env: Mapping[str, str],
) -> PreparedCampaign:
    """Seal a runnable preparation under one explicit copied environment (H1: /15 only)."""
    return _prepare(connection, LEGACY_CAMPAIGN_MANIFEST_SCHEMAS, wheel_paths=wheel_paths, extra_paths=extra_paths, env=env)


def _prepare(
    connection: Connection,
    schemas: frozenset[str],
    *,
    wheel_paths: Sequence[os.PathLike[str] | str],
    extra_paths: Sequence[os.PathLike[str] | str] = (),
    env: Mapping[str, str],
) -> PreparedCampaign:
    """``prepare`` with the admitted schema set explicit; the Owner passes ``CAMPAIGN_MANIFEST_SCHEMAS``."""
    connection = _connection(connection, "prepare")
    wheels = _paths(wheel_paths, "prepare")
    extras = _paths(extra_paths, "prepare")
    environment = _environment(env)
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise _error("environment_invalid", "prepare")
    _verify_connection(connection, "prepare")
    from evolution_lab._ownership import MARKER_NAME
    from evolution_lab.owner.secrets import declared_environment_names
    names = declared_environment_names(workflow=connection._manifest.workflow)
    if MARKER_NAME in names.execution:
        raise _error('reserved_environment_name', 'prepare')
    try:
        inputs = prepare_inputs(
            connection._manifest_pin.path,
            wheel_paths=wheels,
            extra_paths=extras,
            env=environment,
            schemas=schemas,
        )
        sealed_environment = inputs._environment
        if sealed_environment is None:
            raise PreparationEnvironmentInvalid()
    except InputChanged as exc:
        raise _error("inputs_changed", "prepare") from exc
    except PreparationArtifactsInvalid as exc:
        _raise_prepare_failure(connection, "artifacts_invalid", exc)
    except PreparationEnvironmentInvalid as exc:
        _raise_prepare_failure(connection, "environment_invalid", exc)
    except OSError as exc:
        _raise_prepare_failure(connection, "io_error", exc)
    except ManifestError as exc:
        _raise_prepare_failure(connection, "manifest_invalid", exc)
    except (PolicyError, ValueError) as exc:
        _raise_prepare_failure(connection, "artifacts_invalid", exc)
    try:
        manifest_pin = next(
            (pin for pin in inputs.files if pin.path == connection._manifest_pin.path),
            None,
        )
        if manifest_pin is None or manifest_pin.sha256 != connection.manifest_digest:
            raise _error("inputs_changed", "prepare")
        _verify_connection(connection, "prepare")
        names = frozenset(_distribution_name(pin.name) for pin in inputs.wheels)
        if not _REQUIRED_DISTRIBUTIONS.issubset(names):
            raise _error("environment_invalid", "prepare")
    except ClientError:
        raise
    except InputChanged as exc:
        raise _error("inputs_changed", "prepare") from exc
    try:
        verify_isolated(inputs.wheels, env=sealed_environment)
    except IsolatedEnvironmentError as exc:
        raise _error("environment_invalid", "prepare") from exc
    except OSError as exc:
        raise _error("io_error", "prepare") from exc
    except ValueError as exc:
        raise _error("environment_invalid", "prepare") from exc
    return make_prepared_campaign(connection.manifest_digest, inputs, sealed_environment)


def _prepared_inputs(value: PreparedCampaign):
    if not isinstance(value, PreparedCampaign):
        raise _error("invalid_argument", "prepare")
    return prepared_inputs(value)


def _prepared_environment(value: PreparedCampaign) -> Mapping[str, str]:
    if not isinstance(value, PreparedCampaign):
        raise _error("invalid_argument", "prepare")
    return prepared_environment(value)


from evolution_lab._client_operation import RunOperation, create_run, stop
from evolution_lab._client_readers import ReportUnavailable, report, status
from evolution_lab.report import Report


__all__ = [
    "CheckResult", "ClientError", "Connection", "Failure", "PreparedCampaign",
    "Report", "ReportUnavailable", "RunResult", "StatusSnapshot", "StopReceipt",
    "check", "connect", "prepare", "RunOperation", "create_run", "report", "status",
    "stop",
]
