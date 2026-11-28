"""Safe public failures and HTTP status mapping for the local service."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evolution_lab.owner import OwnerError
from evolution_lab.preparation import PreparationError
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.endpoint import too_long_hint

_SOCKET_TOO_LONG = too_long_hint()

_MESSAGES = {
    "invalid_request": "The service request is invalid.",
    "invalid_argument": "A service argument is invalid.",
    "unsupported_schema": "The service request schema is not supported.",
    "not_found": "The requested service resource was not found.",
    "request_too_large": "The service request body is too large.",
    "unsupported_media_type": "The service request media type is unsupported.",
    "service_stopping": "The service is stopping.",
    "history_unavailable": "The requested service history is unavailable.",
    "socket_path_exists": "The service socket path already exists.",
    "socket_path_too_long": _SOCKET_TOO_LONG[:1].upper() + _SOCKET_TOO_LONG[1:] + '.',
    "service_environment_unconfigured": "The service execution environment is not configured.",
    "request_conflict": "The request key was already used for a different mutation.",
    "request_unknown_generation": "The request key names another service generation.",
    "request_table_full": "The service request key table is full.",
    "io_error": "The service storage operation failed.",
    "lab_error": "The service operation failed.",
    "report_unavailable": "The operation report is unavailable.",
}
_STATUS = {
    "reserved_environment_name": 422,
    'authorization_reason_required': 422, 'authorization_unknown_finding': 409,
    'authorization_effect_invalid': 422, 'linked_predecessor_unknown': 404,
    'linked_predecessor_deleted': 409,
    "baseline_source_unknown": 404, "baseline_source_deleted": 409, "baseline_import_unsupported": 422,
    "policy_unconfirmed": 409, "gateway_port_busy": 409, "gateway_unavailable": 503,
    "policy_continuation_mismatch": 409, "policy_gateway_changed": 409,
    "policy_route_mismatch": 409, "manifest_provider_pin": 409,
    "secret_conflict": 409, "token_name_conflict": 409,
    "invalid_request": 400,
    "invalid_argument": 400,
    "socket_path_too_long": 400,
    "unsupported_schema": 400,
    "not_found": 404,
    "tree_unavailable": 404,
    "busy": 409,
    "resource_busy": 409,
    "inputs_changed": 409,
    "state_changed": 409,
    "digest_mismatch": 409,
    "binding_mismatch": 409,
    "target_slot_held": 409,
    "target_not_running": 409,
    "target_unstable_fresh_required": 409,
    "cleanup_required": 409,
    "history_unavailable": 409,
    "service_environment_unconfigured": 409,
    "request_conflict": 409,
    "request_unknown_generation": 409,
    "results_deleted": 409,
    "target_mismatch": 409,
    "path_conflict": 409,
    "dispatch_refused": 409,
    "export_unavailable": 409,
    "private_disclosure_required": 403,
    "trace_not_a_campaign": 409,
    "report_unavailable": 409,
    "cleanup_unconfirmed": 409,
    "probe_sessions_open": 409,
    "bundle_probe_only": 409,
    "manifest_invalid": 422,
    "artifacts_invalid": 422,
    "environment_invalid": 422,
    "environment_unavailable": 422,
    "unsupported_input": 422,
    "conformance_unavailable": 422,
    "reserved_public_input": 422,
    "linked_run_unsupported": 422,
    "grouper_kind_unsupported": 422,
    "corpus_inside_role_workspace": 422,
    "service_stopping": 503,
    "target_unavailable": 503,
    "request_table_full": 503,
    "io_error": 500,
    "lab_error": 500,
    "spawn_failed": 500,
    "lifecycle_unavailable": 500,
    "campaign_unavailable": 500,
    "callback_failed": 500,
    "request_too_large": 413,
    "unsupported_media_type": 415,
    "socket_path_exists": 409,
    # Project failures (stage "project"); digest_mismatch, inputs_changed and
    # invalid_request share the rows above.
    "tree_not_found": 404,
    "tree_excluded": 422,
    "project_not_found": 404,
    "implementation_not_found": 404,
    "id_taken": 409,
    "invalid_id": 400,
    "archived": 409,
    "active_operation": 409,
    "conflict": 409,
    "reserved_claim": 400,
    "not_acceptable": 409,
    "ambiguous_implementation": 400,
    "working_copy_unavailable": 422,
    "package_unavailable": 422,
    "package_mismatch": 422,
    "project_not_ready": 409,
    "not_linked": 409,
    "clone_failed": 422,
    "not_a_clone": 422, "sync_fetch_failed": 422, "revision_not_found": 422,
    "history_diverged": 409, "branch_mismatch": 409, "working_copy_dirty": 409, "merge_conflict": 409,
    "merge_failed": 409, "upstream_changed": 409, "sync_not_recorded": 500,
    "implementation_archived": 409,
    "git_unavailable": 422,
    "port_destination_exists": 409,
    "port_into_working_copy": 400,
    "port_into_lab_home": 400,
    "template_not_git": 422,
    "template_ref_not_found": 422,
    "template_fetch_failed": 422,
    "port_failed": 500,
    "home_invalid": 500,
    "home_busy": 409,
    "link_failed": 500,
    "registry_newer": 409,
    "registry_invalid": 500,
    "target_entry_invalid": 422,
    "target_undeclared": 422,
    "probe_session_closed": 409,
    "probe_session_not_found": 404,
    "probe_ceiling_reached": 409,
    "probe_ceiling_unverifiable": 409,
    "probe_limit_reached": 409,
    "target_not_ready": 409,
}


@dataclass(eq=False)
class ServiceError(ValueError):
    """A closed transport failure that never contains request values or exception data.

    ``detail`` may only name keys of Lab's own closed schemas (W52), never request input."""

    code: str
    stage: str = "transport"
    detail: str | None = None

    # Not frozen=True: its __setattr__ also refuses __traceback__, which contextlib assigns.
    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("code", "stage", "detail") and name in self.__dict__:
            raise AttributeError(f"ServiceError.{name} is read-only")
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        if self.code not in _MESSAGES or self.stage != "transport":
            raise ValueError("invalid service failure")
        if self.detail is not None and (not isinstance(self.detail, str) or len(self.detail) > 512):
            raise ValueError("invalid service failure detail")

    @property
    def message(self) -> str:
        return _MESSAGES[self.code] if self.detail is None else f"{_MESSAGES[self.code]} {self.detail}"

    def to_payload(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage, "message": self.message}


def translate_exception(error: BaseException) -> ServiceError | Any:
    """Keep typed public failures; collapse every other exception safely."""
    if isinstance(error, ServiceError):
        return error
    if isinstance(error, (OwnerError, PreparationError)):
        return error.failure
    if isinstance(error, ProjectError):
        return error
    if isinstance(error, OSError):
        return ServiceError("io_error")
    return ServiceError("lab_error")


def error_response(error: BaseException | Any) -> dict[str, Any]:
    """Return the framework-neutral status and fixed error envelope."""
    failure = translate_exception(error) if isinstance(error, BaseException) else error
    payload = failure.to_payload()
    return {"status": _STATUS.get(payload["code"], 500), "body": {"error": payload}}
