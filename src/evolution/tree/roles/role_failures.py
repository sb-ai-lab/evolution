"""Closed failure reasons of family-10 improvement roles (ADR-0023)."""

from __future__ import annotations

import re
from typing import Final, Literal, get_args

EDITOR_PROTOCOL_CATEGORIES: Final = (
    "no_tool_call",
    "unknown_tool",
    "invalid_arguments",
    "plan_path_not_writable",
    "plan_path_unsafe",
    "plan_repeated_path",
    "plan_delete_missing",
    "mutation_before_plan",
    "finish_before_plan",
    "plan_mismatch",
)

# Shared schema names stay in this light module; journal validators import it too.
ADAPTIVE_GROUP_KEY_ORDER: Final = (
    "root_cause",
    "suggested_fix",
    "evidence_ids",
    "affected_cases",
    "target_paths",
    "counterevidence",
    "regression_risks",
    "why_this_layer",
    "probe_refs",
    "document_ids",
)

REFLECTION_DETAIL_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "unknown_key",
        "missing_key",
        "not_a_string_list",
        "missing_evidence_id",
        "unknown_evidence_id",
        "not_a_case_id",
        "uncited_affected_case",
        "unwritable_target_path",
        "unknown_document_id",
        "not_a_probe_list",
        "unknown_probe",
        "bounds",
        "run_secret",
        "protocol",
    }
)
REFLECTION_DETAIL_PATH: Final = re.compile(
    r"groups\[(0|[1-9]\d{0,3})\](\.(?:"
    + "|".join(ADAPTIVE_GROUP_KEY_ORDER)
    + r")(\[(0|[1-9]\d{0,3})\])?)?"
)

FailureClass = Literal["provider", "content", "unknown"]
FAILURE_CLASSES: Final = frozenset(get_args(FailureClass))

LIFECYCLE_ERRORS: Final = frozenset({"interrupted", "cancelled", "unresponsive"})

REFLECTION_REASONS: Final[dict[tuple[str, str], FailureClass]] = {
    ("reported", "provider_exhausted"): "provider",
    ("reported", "provider_rejected"): "provider",
    ("reported", "preflight_failed"): "unknown",
    ("reported", "protocol_exhausted"): "content",
    ("reported", "turns_exhausted"): "content",
    ("reported", "self_check_failed"): "content",
    ("reported", "invalid_result"): "content",
    ("invalid_result", "invalid_result"): "content",
    ("oracle_leak", "oracle_leak"): "content",
    # Family 14 (H11, F0 R67): the Reflector changed its frozen role projection.
    ("projection_changed", "projection_changed"): "content",
    # Family 14 (ADR-0039 D5, ADR-0023): a minimum first request refused
    # before probe setup repeats until repaired, so it suspends (PDR-0019).
    # The Grouper's same-named failure stays content and falls back.
    ("input_too_large", "input_too_large"): "unknown",
    ("reported", "agent_error"): "unknown",
    ("reported", "unclassified"): "unknown",
    ("timeout", "timeout"): "unknown",
    ("control_collision", "control_collision"): "unknown",
    ("reader_integrity", "reader_integrity"): "unknown",
    ("exception", "exception"): "unknown",  # error_class "exception:<Name>" normalised
}

EDITOR_REASONS: Final[dict[tuple[str, str], FailureClass]] = {
    ("reported", "provider_exhausted"): "provider",
    ("reported", "provider_rejected"): "provider",
    ("reported", "preflight_failed"): "unknown",
    ("reported", "protocol_exhausted"): "content",
    ("reported", "turns_exhausted"): "content",
    ("reported", "self_check_failed"): "content",
    ("reported", "actor_violation"): "content",
    ("reported", "changed_file_budget"): "content",
    ("reported", "written_byte_budget"): "content",
    ("reported", "plan_mismatch"): "content",
    ("reported", "agent_error"): "unknown",
    ("reported", "program_failed"): "unknown",
    ("reported", "environment_changed"): "unknown",
    ("reported", "unclassified"): "unknown",
    ("timeout", "timeout"): "unknown",
    ("exception", "exception"): "unknown",
}

_REPORTED_REFLECTOR: Final = {
    "provider_exhausted": "provider_exhausted",
    "provider_rejected": "provider_rejected",
    "preflight_failed": "preflight_failed",
    "protocol": "protocol_exhausted",
    "turns_exhausted": "turns_exhausted",
    "budget": "turns_exhausted",
    "self_check_failed": "self_check_failed",
    "invalid_result": "invalid_result",
    "agent_error": "agent_error",
}

_REPORTED_EDITOR: Final = {
    "provider_exhausted": "provider_exhausted",
    "provider_rejected": "provider_rejected",
    "preflight_failed": "preflight_failed",
    "protocol": "protocol_exhausted",
    "turns_exhausted": "turns_exhausted",
    "budget": "turns_exhausted",
    "self_check_failed": "self_check_failed",
    "actor_violation": "actor_violation",
    "changed_file_budget": "changed_file_budget",
    "written_byte_budget": "written_byte_budget",
    "plan_mismatch": "plan_mismatch",
    "agent_error": "agent_error",
    "program_failed": "program_failed",
    "exit_status": "program_failed",
    "result_too_large": "program_failed",
    "bad_result": "program_failed",
    "no_result": "program_failed",
    "prompt_changed": "environment_changed",
    "command_changed": "environment_changed",
    "workspace_changed": "environment_changed",
    "control_collision": "environment_changed",
}


def _error_key(error_class: str) -> str:
    return "exception" if error_class.startswith("exception:") else error_class


def reflection_reason(error_class: str, code: str | None) -> str | None:
    """The family-10 `reason` of a reflection terminal, or None for a lifecycle terminal."""
    if error_class in LIFECYCLE_ERRORS:
        return None
    key = _error_key(error_class)
    if key == "reported":
        return _REPORTED_REFLECTOR.get(code or "", "unclassified")
    return key


def editor_reason(error_class: str, code: str | None) -> str | None:
    """The family-10 `reason` of an Editor terminal, or None for a lifecycle terminal."""
    if error_class in LIFECYCLE_ERRORS:
        return None
    key = _error_key(error_class)
    if key == "reported":
        return _REPORTED_EDITOR.get(code or "", "unclassified")
    return key


#: ADR-0037: the Editor budget refusals family 14 names in `editor_failed.reason`.
EDITOR_BUDGET_REASONS: Final = frozenset({"changed_file_budget", "written_byte_budget"})
#: The first family whose schema admits `EDITOR_BUDGET_REASONS`
#: (`campaign_schema.FINAL_CAMPAIGN_FAMILY_VERSION`; not imported, because
#: `campaign_schema` imports this module).
EDITOR_BUDGET_REASON_FAMILY: Final = 14


def journaled_editor_reason(
    error_class: str, code: str | None, *, family_version: int
) -> str | None:
    """`editor_reason` as the campaign journals it (ADR-0037, ADR-0023 amendment):
    the two budget codes exist from family 14; families 10-13 keep `actor_violation`."""

    reason = editor_reason(error_class, code)
    if reason in EDITOR_BUDGET_REASONS and family_version < EDITOR_BUDGET_REASON_FAMILY:
        return "actor_violation"
    return reason


def reflection_class(error_class: str, reason: str) -> FailureClass:
    """Raises KeyError for a pair outside the contract (the reducer maps it to a refusal)."""
    return REFLECTION_REASONS[(_error_key(error_class), reason)]


def editor_class(error_class: str, reason: str) -> FailureClass:
    """Raises KeyError for a pair outside the contract."""
    return EDITOR_REASONS[(_error_key(error_class), reason)]


#: Family 14 (ADR-0034): the closed
#: `reason` of a `chat` or `agent_cli` Grouper's `grouping_failed(role_failed)`
#: and the failure class it carries as `error_class`. Which reasons each kind
#: may write is the lifecycle's rule; the schema validator admits this union.
GROUPER_REASONS: Final[dict[str, FailureClass]] = {
    "provider_exhausted": "provider",
    "provider_rejected": "provider",
    "invalid_output": "content",
    "input_too_large": "content",
    "turns_exhausted": "content",
    "agent_error": "unknown",
    "timeout": "unknown",
    "exception": "unknown",
}

#: The family-14 `campaign_suspended` reason a Grouper failure class decides.
_GROUPER_SUSPENSION_FOR: Final[dict[str, str]] = {
    "provider": "grouper_provider_failed",
    "content": "grouper_failing",
    "unknown": "grouper_failing",
}


def grouper_class(reason: str) -> FailureClass:
    """Raises KeyError for a reason outside `GROUPER_REASONS`."""
    return GROUPER_REASONS[reason]


def grouper_suspension_for(error_class: str) -> str:
    """`grouper_provider_failed` for `provider`, else `grouper_failing`.

    Raises KeyError for a value that is not a failure class."""
    return _GROUPER_SUSPENSION_FOR[error_class]


class GrouperRoleFailure(Exception):
    """A family-14 Grouper role failure (ADR-0034): the runtime raises it and
    the executor journals it as `grouping_failed(role_failed)`.

    The pair is held to `GROUPER_REASONS` at construction, so no producer can
    carry a reason with another reason's class."""

    def __init__(self, reason: str, error_class: str) -> None:
        if GROUPER_REASONS.get(reason) != error_class:
            raise ValueError(f"not a Grouper reason and class: {reason!r}, {error_class!r}")
        super().__init__(f"Grouper {error_class} failure: {reason}")
        self.reason = reason
        self.error_class: FailureClass = GROUPER_REASONS[reason]


class GroupingExecutionError(ValueError):
    """Safe terminal code for a sealed custom grouping invocation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
