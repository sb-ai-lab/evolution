"""Private validation of closed service response DTOs for the CLI client."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from pathlib import PurePosixPath
import re
from typing import Any

from evolution_lab.owner import (
    CleanupFacts, CoreTerminal, Connection, DiffResult, ExecutorIdentity, Failure, Health,
    OperationResult, OperationStatus, Persistence, ProcessFacts, ReleaseReceipt, ResumeVerdict,
    ReportResult, SuspensionEvidenceResult,
    ResourceRef, StartFacts, StopReceipt, TargetRef, TargetStatus, TaskRef, TaskResult,
    TaskStatus,
)
from evolution_lab.owner.models import Approval, PolicyRecord, Refusal, RefusalRecord, WorkerFailureRecord
from evolution_lab.preparation import PreparationError
from evolution_lab.preparation.models import (
    DETAILED_CODES, BundleSummary, BundleWarning, DistributionPin, EvaluationPlan, PlatformIdentity, PreparedSummary,
    RuntimePolicy, TargetBinding, UNMEASURED_LIMIT_DETAIL, logical_path,
)
from evolution_lab.projects.errors import ProjectError
from evolution_lab.projects.models import Acceptance, Confirmation, Link, project_from_wire, project_wire
from evolution_lab.service.codecs import decode_digest, decode_identifier
from evolution_lab.service.errors import ServiceError


_DETAILED_PROJECT_CODES = frozenset({"tree_excluded", "port_destination_exists", "working_copy_dirty", "merge_conflict", "merge_failed",
                                     "revision_not_found"})

_DETAILED_SERVICE_CODES = frozenset({"report_unavailable"})  # "<code text> <detail>", spec 2026-10-14 report-markdown §3


def _failure(value: Any) -> Failure | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"code", "stage", "message"}:
        raise ValueError("invalid failure")
    try:
        return Failure(**value)
    except (TypeError, ValueError):
        pass
    try:
        failure = ServiceError(value["code"], value["stage"])
        if failure.to_payload() == value:
            return None
        if value == ServiceError("invalid_request", detail=UNMEASURED_LIMIT_DETAIL).to_payload():
            return None
        prefix = failure.message + " "
        if value["code"] in _DETAILED_SERVICE_CODES and isinstance(value["message"], str) \
                and value["message"].startswith(prefix):
            detailed = ServiceError(value["code"], value["stage"], value["message"][len(prefix):])
            if detailed.to_payload() == value:
                return None
    except (KeyError, TypeError, ValueError):
        pass
    try:
        failure = PreparationError(value["code"], value["stage"]).failure
        if failure.to_payload() == value:
            return None
        # core's diagnosis appended to a manifest/artifacts refusal (spec 2026-10-09 §3)
        prefix = failure.message + " "
        if value["code"] in DETAILED_CODES and isinstance(value["message"], str) \
                and value["message"].startswith(prefix):
            detailed = PreparationError(value["code"], value["stage"], value["message"][len(prefix):]).failure
            if detailed.to_payload() == value:
                return None
    except (KeyError, TypeError, ValueError):
        pass
    try:
        failure = ProjectError(value["code"])
        if failure.to_payload() == value:
            return None
        # the detailed messages: the default port destination and the sync failures' paths or fixed texts, appended
        # to the code text exactly as ProjectError.message builds them
        if value["code"] in _DETAILED_PROJECT_CODES:
            prefix = failure.message + " "
            message = value["message"]
            detail = message[len(prefix):] if isinstance(message, str) and message.startswith(prefix) else ""
            if detail and ProjectError(value["code"], detail).to_payload() == value:
                return None
    except (KeyError, TypeError, ValueError):
        pass
    raise ValueError("invalid failure")


def _owner_failure(value: Any) -> Failure | None:
    failure = _failure(value)
    if value is not None and failure is None:
        raise ValueError("non-owner failure in owner DTO")
    return failure


def _process(value: Any) -> ProcessFacts:
    return ProcessFacts(**value)


def _cleanup(value: Any) -> CleanupFacts:
    return CleanupFacts(**value)


def _persistence(value: Any) -> Persistence:
    return Persistence(value["state"], _owner_failure(value["failure"]))


def _resource(value: Any) -> ResourceRef:
    return ResourceRef.from_payload(value)


def _task_result(value: Any) -> TaskResult | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "payload"}:
        raise ValueError("invalid task result")
    return TaskResult(value["kind"], value["payload"])


def _task_status(value: dict[str, Any]) -> TaskStatus:
    row = _closed(value, {"schema", "id", "owner_generation", "action", "ref", "state", "result", "failure", "persistence"})
    if row["schema"] != "lab-task-status/1":
        raise ValueError("invalid schema")
    return TaskStatus(
        row["id"], row["owner_generation"], row["action"],
        None if row["ref"] is None else _resource(row["ref"]), row["state"],
        _task_result(row["result"]), _owner_failure(row["failure"]),
        _persistence(_closed(row["persistence"], {"state", "failure"})),
    )


def _start(value: Any) -> StartFacts:
    row = _closed(value, {"start", "scope_id", "owner_generation", "target_ref", "process", "target_cleanup", "failure", "settlement", "refusal", "core_terminal", "initiated_by", "auto_resume_attempt"})
    return StartFacts(
        row["start"], row["scope_id"], row["owner_generation"],
        TargetRef.from_payload(row["target_ref"]),
        _process(_closed(row["process"], {"launched", "exit_code", "cleanup_complete", "forced"})),
        _cleanup(_closed(row["target_cleanup"], {"state", "complete"})),
        _owner_failure(row["failure"]), row["settlement"],
        None if row["refusal"] is None else Refusal.from_payload(row["refusal"]),
        None if row["core_terminal"] is None else CoreTerminal.from_payload(row["core_terminal"]),
        row['initiated_by'], row['auto_resume_attempt'],
    )


def _operation_status(value: dict[str, Any]) -> OperationStatus:
    row = _closed(value, {"schema", "id", "owner_generation", "prepared_digest", "kind", "target_ref", "control_state", "process", "target_cleanup", "result", "failure", "refusal", "core_terminal", "results_state", "executor", "retained_summary", "starts", "resume", "entrypoint", "progress", "progress_state", "times", "times_state", "times_source", "times_reason", "auto_resume"})
    if row["schema"] != "lab-operation-status/8" or not isinstance(row["starts"], list):
        raise ValueError("invalid schema")
    return OperationStatus(
        row["id"], row["owner_generation"], row["prepared_digest"],
        TargetRef.from_payload(row["target_ref"]), row["control_state"],
        _process(_closed(row["process"], {"launched", "exit_code", "cleanup_complete", "forced"})),
        _cleanup(_closed(row["target_cleanup"], {"state", "complete"})),
        None if row["result"] is None else OperationResult.from_payload(row["result"]),
        _owner_failure(row["failure"]), row["results_state"],
        ExecutorIdentity.from_payload(row["executor"]), kind=row["kind"],
        retained_summary=row["retained_summary"],
        starts=tuple(_start(item) for item in row["starts"]),
        resume=None if row["resume"] is None else ResumeVerdict.from_payload(row["resume"]),
        entrypoint=row["entrypoint"], progress=row["progress"], progress_state=row["progress_state"],
        times=row["times"], times_state=row["times_state"],
        times_source=row["times_source"], times_reason=row["times_reason"],
        refusal=None if row["refusal"] is None else Refusal.from_payload(row["refusal"]),
        core_terminal=None if row["core_terminal"] is None else CoreTerminal.from_payload(row["core_terminal"]),
        auto_resume=row['auto_resume'],
    )


def _bundle_warning(value: Any) -> BundleWarning:
    row = _closed(value, {"code", "detail"})
    if not isinstance(row["code"], str) or not row["code"]:
        raise ValueError("invalid bundle warning code")
    detail = row["detail"]
    if not isinstance(detail, dict) or any(
        not isinstance(key, str) or not key or not (item is None or isinstance(item, str))
        for key, item in detail.items()
    ):
        raise ValueError("invalid bundle warning detail")
    warning = BundleWarning(row["code"], detail)
    if warning.to_payload() != value:
        raise ValueError("invalid bundle warning")
    return warning


def _bundle_summary(value: dict[str, Any]) -> BundleSummary:
    row = _closed(value, {"schema", "digest", "source_kind", "entrypoint", "launch_digest", "environment_digests", "secret_names", "entry_count", "limitations", "warnings", "measurement_basis"})
    if row["schema"] != "lab-bundle-summary/3":
        raise ValueError("invalid schema")
    decode_digest(row["digest"])
    decode_digest(row["launch_digest"])
    if row["measurement_basis"] is not None:
        decode_digest(row["measurement_basis"])
    if row["source_kind"] not in {"campaign", "evaluation", "probe"}:
        raise ValueError("invalid source kind")
    logical_path(row["entrypoint"], "read")
    _digest_list(row["environment_digests"])
    _string_list(row["secret_names"])
    _string_list(row["limitations"])
    if isinstance(row["entry_count"], bool) or not isinstance(row["entry_count"], int) or row["entry_count"] < 0:
        raise ValueError("invalid entry count")
    if not isinstance(row["warnings"], list):
        raise ValueError("invalid warnings")
    warnings = tuple(_bundle_warning(item) for item in row["warnings"])
    result = BundleSummary(**{
        key: (warnings if key == "warnings" else row[key]) for key in row if key != "schema"
    })
    if result.to_payload() != value:
        raise ValueError("invalid bundle summary")
    return result


def _prepared_summary(value: dict[str, Any]) -> PreparedSummary:
    row = _closed(value, {"schema", "digest", "bundle_digest", "kind", "target_binding", "execution_environment", "secret_names", "runtime_policy", "evaluation"})
    if row["schema"] != "lab-prepared-summary/1":
        raise ValueError("invalid schema")
    decode_digest(row["digest"])
    decode_digest(row["bundle_digest"])
    decode_digest(row["execution_environment"])
    if row["kind"] not in {"campaign", "evaluation"}:
        raise ValueError("invalid prepared kind")
    _string_list(row["secret_names"])
    result = PreparedSummary(
        row["digest"], row["bundle_digest"], row["kind"],
        TargetBinding.from_payload(row["target_binding"]), row["execution_environment"],
        tuple(row["secret_names"]), RuntimePolicy.from_payload(row["runtime_policy"]),
        None if row["evaluation"] is None else EvaluationPlan.from_payload(row["evaluation"]),
    )
    if result.to_payload() != value:
        raise ValueError("invalid prepared summary")
    return result


def _connection(value: dict[str, Any]) -> Connection:
    row = _closed(value, {"schema", "id", "source_kind", "bundle_digest", "launch_digest", "owner_generation"})
    if row["schema"] != "lab-owner-connection/1":
        raise ValueError("invalid schema")
    return Connection(row["id"], row["source_kind"], row["bundle_digest"], row["launch_digest"], row["owner_generation"])


def _stop(value: dict[str, Any]) -> StopReceipt:
    row = _closed(value, {"schema", "ref", "accepted", "effect", "process"})
    if row["schema"] != "lab-owner-stop/1":
        raise ValueError("invalid schema")
    return StopReceipt(_resource(row["ref"]), row["accepted"], row["effect"], _process(_closed(row["process"], {"launched", "exit_code", "cleanup_complete", "forced"})))


def _release(value: dict[str, Any]) -> ReleaseReceipt:
    row = _closed(value, {"schema", "ref", "disposition", "reason", "cleanup", "released"})
    if row["schema"] != "lab-owner-release/1":
        raise ValueError("invalid schema")
    result = ReleaseReceipt(_resource(row["ref"]), row["reason"], _cleanup(_closed(row["cleanup"], {"state", "complete"})), row["released"])
    if result.to_payload() != value:
        raise ValueError("invalid release receipt")
    return result


def _target(value: dict[str, Any]) -> TargetStatus:
    row = _closed(value, {"schema", "id", "owner_generation", "launch_digest", "bundle_digest", "state", "base_url", "boot_id", "health", "cleanup", "failure"} | ({"execution_policy"} if "execution_policy" in value else set()))
    if row["schema"] != "lab-owner-target/2":
        raise ValueError("invalid schema")
    if "execution_policy" in row and row["execution_policy"] is None:
        raise ValueError("policy must be absent or a digest")
    return TargetStatus(row["id"], row["owner_generation"], row["launch_digest"], row["bundle_digest"],
        row["state"], row["base_url"], row["boot_id"], row["health"],
        _cleanup(_closed(row["cleanup"], {"state", "complete"})), _owner_failure(row["failure"]), row.get("execution_policy"))


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    """Owner Health plus its already-validated environment projection.

    ``to_payload`` reconstructs the full ``lab-service-health/2`` envelope from the
    canonical ``health`` (never from the raw input), so ``validate``'s generic
    round-trip check applies to this schema exactly as it does to every other one:
    a noncanonical owner field (e.g. ``distributions`` not a list) makes
    ``health.to_payload()`` diverge from the input and the round-trip check catches
    it, with no schema-specific bypass required.
    """

    health: Health
    service_environment: dict[str, Any] | None
    schema: str = field(default="lab-service-health/2", init=False)

    def to_payload(self) -> dict[str, Any]:
        payload = self.health.to_payload()
        payload["schema"] = self.schema
        payload["service_environment"] = self.service_environment
        return payload


def _service_environment_row(env: dict[str, Any]) -> None:
    """Validate lab-service-environment/2 by value, not just by key: every field is
    either digest-decoded, type-checked, or rebuilt through its canonical model and
    required to round-trip exactly, so a wrong-typed field (not just an extra key)
    is rejected."""
    if env["schema"] != "lab-service-environment/2" or not isinstance(env["proof_profile"], str):
        raise ValueError("invalid service environment")
    executable = env["executable"]
    if not isinstance(executable, str) or not executable or not PurePosixPath(executable).is_absolute():
        raise ValueError("invalid service environment")
    decode_digest(env["digest"]); decode_digest(env["executable_sha256"])
    if env["python_version"] is not None and not isinstance(env["python_version"], str):
        raise ValueError("invalid service environment")
    platform_row = env["platform"]
    if PlatformIdentity.from_payload(platform_row).to_payload() != platform_row:
        raise ValueError("invalid service environment")
    if not isinstance(env["distributions"], list) or not isinstance(env["runtime_files"], list):
        raise ValueError("invalid service environment")
    for pin in env["distributions"]:
        if DistributionPin.from_payload(pin).to_payload() != pin:
            raise ValueError("invalid service environment")
    for item in env["runtime_files"]:
        decode_digest(_closed(item, {"sha256"})["sha256"])


def _service_health(value: dict[str, Any]) -> ServiceHealth:
    row = _closed(value, {"schema", "lab_version", "core_version", "distributions", "owner_generation",
                          "active_operation_id", "blockers", "service_state", "service_environment"})
    if row["schema"] != "lab-service-health/2":
        raise ValueError("invalid schema")
    owner = {key: row[key] for key in row if key != "service_environment"}
    canonical_owner = {**owner, "schema": "lab-owner-health/1"}
    health = _health(canonical_owner)
    if health.to_payload() != canonical_owner:
        raise ValueError("noncanonical service health")
    environment = row["service_environment"]
    if environment is not None:
        _service_environment_row(_closed(environment, {"schema", "digest", "executable", "python_version",
            "executable_sha256", "platform", "distributions", "runtime_files", "proof_profile"}))
    return ServiceHealth(health, environment)


def _health(value: dict[str, Any]) -> Health:
    row = _closed(value, {"schema", "lab_version", "core_version", "distributions", "owner_generation", "active_operation_id", "blockers", "service_state"})
    if row["schema"] != "lab-owner-health/1":
        raise ValueError("invalid schema")
    return Health(row["lab_version"], row["core_version"], tuple(row["distributions"]), row["owner_generation"],
        row["active_operation_id"], tuple(_resource(item) for item in row["blockers"]), row["service_state"])


def _routing(value):
    from evolution_lab.owner.routing import validate_routing
    return validate_routing(value)


def _report(value: dict[str, Any]) -> ReportResult:
    row = _closed(value, {"schema", "operation_id", "state", "report", "reason", "starts", "usage", "core_terminal", "reflector_probe", "reflector_probe_state", "reflector_probe_reason"} | ({"routing"} if "routing" in value else set()))
    if row["schema"] != "lab-owner-report/15" or not isinstance(row["starts"], list):
        raise ValueError("invalid schema")
    if row["usage"] is not None:
        if not isinstance(row["usage"], list):
            raise ValueError("invalid usage")
        for item in row["usage"]:
            _usage_pair(_closed(item, {"measurement_id", "usage", "usage_state"}))
    if isinstance(row["report"], dict):
        _report_independent(row["report"])
        _report_checks(row["report"])
    result = ReportResult(row["operation_id"], row["state"], row["report"], row["reason"],
        starts=tuple(_start(item) for item in row["starts"]), usage=row["usage"],
        **({"routing": row["routing"]} if "routing" in row else {}),
        **{key: row[key] for key in ("reflector_probe", "reflector_probe_state", "reflector_probe_reason")})
    if result.to_payload()["core_terminal"] != row["core_terminal"]:
        raise ValueError("inconsistent core terminal")
    return result


_REPORT_INDEPENDENT = ("lab_independent", "lab_independent_state", "lab_independent_reason")


def _report_independent(report: dict[str, Any]) -> None:
    if report.get("operation") == "evaluation":
        _focus_notices(report)
    elif report.get("operation") == "campaign":
        _campaign_notices(report)
    """The family-15 ``lab_independent`` of a ``/7`` report (``report._lab_independent``): absent for family 14 (no
    key at all); otherwise the block next to ``lab_independent_state`` ``present`` and a null reason, or null next to
    ``unavailable`` and ``unreadable`` (ADR-0019 p.3, the view's ``details_state``/``details_reason``). The rest of
    the report stays opaque."""
    present = [key for key in _REPORT_INDEPENDENT if key in report]
    if not present:
        return
    if len(present) != len(_REPORT_INDEPENDENT):
        raise ValueError("invalid lab_independent")
    block, state, reason = (report[key] for key in _REPORT_INDEPENDENT)
    if state == "present" and reason is None and block is not None:
        _independent(block)
    elif not (state == "unavailable" and reason == "unreadable" and block is None):
        raise ValueError("invalid lab_independent")


def _checks(block: Any, state: Any, reason: Any) -> None:
    if state == "unavailable" and reason == "unreadable" and block is None:
        return
    if state != "present" or reason is not None:
        raise ValueError("invalid checks")
    if block is None:
        return
    row = _closed(block, {"modes", "shadow_hits"})
    if not isinstance(row["modes"], dict) or not isinstance(row["shadow_hits"], dict):
        raise ValueError("invalid checks")
    for name, mode in row["modes"].items():
        _str(name)
        if mode not in {"shadow", "off"}:
            raise ValueError("invalid check mode")
    for name, count in row["shadow_hits"].items():
        _str(name)
        if not _count(count) or count < 1:
            raise ValueError("invalid shadow count")


def _report_checks(report: dict[str, Any]) -> None:
    keys = ("lab_checks", "lab_checks_state", "lab_checks_reason")
    present = [key for key in keys if key in report]
    if not present and not any(key in report for key in _REPORT_INDEPENDENT):
        return
    if len(present) != len(keys):
        raise ValueError("invalid lab_checks")
    _checks(*(report[key] for key in keys))


_MEASUREMENT_ROW = {"measurement_id", "role", "round", "attempt_id", "candidate_digest", "coverage",
                    "metrics", "baseline", "times", "usage", "usage_state", "retries",
                    "call_failures_by_code", "classification", "repairs"}
_MEASUREMENT_SAMPLE = {"case_id", "sample_id", "execution_state", "scoring_state", "metrics",
                       "critical_pass", "tries", "baseline", "comparison"}


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _repairs(value: Any) -> None:
    """``null`` or core's repair counters (spec 2026-10-02 §3.1): ``pairs_scored <= tries``."""
    if value is None:
        return
    row = _closed(value, {"tries", "pairs_scored"})
    if not (_count(row["tries"]) and _count(row["pairs_scored"])) or row["pairs_scored"] > row["tries"]:
        raise ValueError("invalid repairs")


def _usage_pair(row: dict[str, Any]) -> None:
    """``usage`` is null exactly when ``usage_state`` is not ``present``."""
    if row["usage_state"] not in {"present", "unavailable"}:
        raise ValueError("invalid usage state")
    if (row["usage"] is None) != (row["usage_state"] == "unavailable"):
        raise ValueError("invalid usage")
    if row["usage"] is not None:
        _closed(row["usage"], {"input_tokens", "output_tokens", "unknown_calls", "coverage"})


def _focus_notices(row, *, evaluation=True):
    if row.get("lab_notices_state") not in {"present", "unavailable"}:
        raise ValueError("invalid notice state")
    notices = row.get("lab_notices")
    if not isinstance(notices, list) or len(notices) > 1:
        raise ValueError("invalid notices")
    if not evaluation and (notices or row["lab_notices_state"] != "present"):
        raise ValueError("invalid campaign notices")
    if notices and row["lab_notices_state"] != "present":
        raise ValueError("unavailable notices")
    for notice in notices:
        value = _closed(notice, {"code", "admitted", "not_completed"})
        if value["code"] != "scorer_without_focus_signal" or not _count(value["admitted"]) \
                or value["admitted"] == 0 or not _count(value["not_completed"]):
            raise ValueError("invalid focus notice")


def _measurement_row(value: Any, *, detail: bool, notices: bool = True) -> None:
    # /14 embeds the original row form; only measurement/list /3 adds notices.
    row = _closed(value, _MEASUREMENT_ROW | ({"samples"} if detail else set()) |
                  ({"lab_notices", "lab_notices_state", "unanswered", "unanswered_state", "answered_reported"}
                   if notices else set()))
    if row["role"] not in {"baseline", "attempt", "evaluation", "other"}:
        raise ValueError("invalid role")
    _usage_pair(row)
    _closed(row["coverage"], {"expected", "scored", "excluded_pairs"})
    if not _count(row["coverage"]["scored"]):
        raise ValueError("invalid scored coverage")
    if notices:
        _focus_notices(row, evaluation=row["role"] == "evaluation")
        state, count, reported = row["unanswered_state"], row["unanswered"], row["answered_reported"]
        if state in {"present", "partial"}:
            if not _count(count) or not _count(reported) or not 0 <= count <= reported <= row["coverage"]["scored"]:
                raise ValueError("invalid unanswered count")
            if not reported or (state == "present") != (reported == row["coverage"]["scored"]):
                raise ValueError("invalid answered coverage")
        elif state == "not_reported":
            if count is not None or not _count(reported) or reported != 0:
                raise ValueError("invalid unreported count")
        elif state != "unavailable" or count is not None or reported is not None:
            raise ValueError("invalid unanswered state")
    _repairs(row["repairs"])
    if not isinstance(row["metrics"], dict):
        raise ValueError("invalid metrics")
    if row["times"] is not None:
        _closed(row["times"], {"started_at", "last_event_at"})
    baseline = row["baseline"]
    if baseline is not None:
        _closed(baseline, {"measurement_id", "state", "delta", "cases"})
        if baseline["state"] not in {"present", "exclusions_truncated", "unavailable"}:
            raise ValueError("invalid baseline state")
        if (baseline["state"] == "present") != (baseline["cases"] is not None):
            raise ValueError("invalid baseline")
        if baseline["cases"] is not None:
            _closed(baseline["cases"], {"better", "same", "worse", "unpaired"})
    if detail:
        if not isinstance(row["samples"], list):
            raise ValueError("invalid samples")
        for sample in row["samples"]:
            item = _closed(sample, _MEASUREMENT_SAMPLE)
            if item["comparison"] not in {"better", "same", "worse", "unpaired", "unknown"}:
                raise ValueError("invalid comparison")
            if item["baseline"] is not None:
                _closed(item["baseline"], {"metrics", "critical_pass"})


_CAMPAIGN_ATTEMPT = {"attempt_id", "outcome", "reason", "group", "look", "measurement_id",
                     "has_tree", "failure", "confidence", "final_decision"}
_CAMPAIGN_ROUND = {"round", "outcome", "baseline_measurement_id", "baseline_version", "reflection", "attempts",
                   "final"}
_CONFIDENCE = {"mode", "repeats", "focus_cases", "causal_cases", "calibration", "latest", "margin"}
_LOOK = {"look", "final", "decision", "reason", "focus", "causal"}
_INTERVAL = {"point", "lower", "upper", "exact"}
_FINAL_ENTRY = {"ordinal", "stability", "champion_side", "stored_measurement_id", "mode", "global", "members",
                "contrasts", "selection", "listed", "next_baseline_measurement_id", "condition_changed_in_place"}
_FINAL_MEMBER = {"measurement_id", "role", "attempt_id", "complete", "point", "missing", "critical"}
_FINAL_CONTRAST = {"attempt_id", "point", "lower", "level", "unavailable", "pairing", "accepted", "reason", "exact",
                   "removed_pairs", "removed_cases", "condition_changed_in_place"}
_FINAL_DECISIONS = {"accepted", "rejected", "pending", "unavailable"}
_ROLES = {"editor", "reflector", "grouper"}
_SETTINGS = {"stability", "round_stop", "baseline_refresh"}
_CONNECTION = {"state", "outcome", "attempts", "binding_digest", "operation_id", "detail"}
_CAVEAT = {"kind", "round", "attempt_id"}
_GATE_MODES = {"statistical", "observe"}
_CAMPAIGN_HYPOTHESIS = {"index", "root_cause", "suggested_fix", "affected_cases", "target_paths",
                        "counterevidence", "regression_risks", "why_this_layer"}
_REFLECTION_STATES = {"present", "declined", "failed", "unsettled", "in_progress", "absent"}
_FAILURE_STAGES = {"editor", "seal", "measurement", "lifecycle", "unknown"}


def _campaign_reflection(value: dict[str, Any]) -> None:
    row = _closed(value, {"state", "reflection_id", "error_class", "reason", "hypotheses"})
    if row["state"] not in _REFLECTION_STATES or not isinstance(row["hypotheses"], list):
        raise ValueError("invalid reflection")
    if row["state"] not in {"failed", "unsettled"} and (row["error_class"] is not None or row["reason"] is not None):
        raise ValueError("invalid reflection")
    if row["state"] != "present" and row["hypotheses"]:
        raise ValueError("invalid reflection")
    for index, hypothesis in enumerate(row["hypotheses"], start=1):
        item = _closed(hypothesis, _CAMPAIGN_HYPOTHESIS)
        if item["index"] != index or not isinstance(item["root_cause"], str) \
                or not isinstance(item["suggested_fix"], str):
            raise ValueError("invalid hypothesis")


def _campaign_failure(value: dict[str, Any]) -> None:
    row = _closed(value, {"stage", "role", "error_class", "role_reason", "code"})
    if row["stage"] not in _FAILURE_STAGES or row["role"] not in {"editor", None}:
        raise ValueError("invalid failure")


def _confidence(value: dict[str, Any]) -> None:
    row = _closed(value, _CONFIDENCE)
    if row["mode"] not in _GATE_MODES or not all(_count(row[k]) for k in ("repeats", "focus_cases", "causal_cases")):
        raise ValueError("invalid confidence")
    if row["latest"] is not None:
        look = _closed(row["latest"], _LOOK)
        if not isinstance(look["final"], bool):
            raise ValueError("invalid look")
        for name in ("focus", "causal"):
            if look[name] is not None:
                _closed(look[name], _INTERVAL)


def _final(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("invalid final")
    for entry in value:
        row = _closed(entry, _FINAL_ENTRY)
        if not isinstance(row["members"], list) or not isinstance(row["contrasts"], list) or not _count(row["listed"]):
            raise ValueError("invalid final")
        for member in row["members"]:
            _closed(member, _FINAL_MEMBER)
        for contrast in row["contrasts"]:
            _closed(contrast, _FINAL_CONTRAST)
        if row["selection"] is not None:
            _closed(row["selection"], {"reason", "attempt_id", "measurement_id"})


def _family_14_blocks(row: dict[str, Any]) -> None:
    roles = _closed(row["roles"], _ROLES)
    for role in roles.values():
        _closed(role, {"kind"})
    _closed(row["settings"], _SETTINGS)
    connection = _closed(row["connection"], _CONNECTION)
    if not _count(connection["attempts"]):
        raise ValueError("invalid connection")


_CAMPAIGN_VIEW_14 = {"schema", "operation_id", "policy", "progress", "outcome", "champions", "champions_state",
                     "rounds", "measurements", "suspensions", "details_state", "details_reason", "details_source",
                     "family", "gate_mode", "caveats", "lab_notices", "lab_notices_state", "roles", "settings", "connection",
                     "reflector_probe", "reflector_probe_state", "reflector_probe_reason"}
#: Family 15 (spec 2026-10-19 D4): no gate mode, the ``independent`` block instead.
_CAMPAIGN_VIEW_15 = (_CAMPAIGN_VIEW_14 - {"gate_mode"}) | {"independent", "checks", "checks_state", "checks_reason"}
_CAMPAIGN_ROUND_15 = _CAMPAIGN_ROUND - {"final"}
_CAMPAIGN_ATTEMPT_15 = _CAMPAIGN_ATTEMPT - {"look", "confidence", "final_decision"}


def _campaign_view(value: dict[str, Any]) -> dict[str, Any]:
    """``lab-campaign-view/15``: one closed form per family, chosen by ``family`` (a mixed key set is refused)."""
    family = value.get("family") if isinstance(value, dict) else None
    if family == 15 and not isinstance(family, bool):
        return _campaign_view_15(value)
    return _campaign_view_14(value)


def _campaign_notices(row):
    notices = row.get("lab_notices")
    if not isinstance(notices, list):
        raise ValueError("invalid closed response")
    if row.get("lab_notices_state") not in {"present", "unavailable"}:
        raise ValueError("invalid notices state")
    for notice in notices:
        if not isinstance(notice, dict):
            raise ValueError("invalid closed response")
        if notice.get("code") == "baseline_provenance_unknown":
            _closed(notice, {"code", "round", "measurement_id", "samples"})
            if not _count(notice["round"]) or not _count(notice["samples"]) or notice["samples"] == 0 or not isinstance(notice["measurement_id"], str) or not notice["measurement_id"]:
                raise ValueError("invalid baseline notice")
        elif notice.get("code") != "no_candidate_measured" or not set(notice) <= {"code", "rounds", "by_reason", "reflection_errors"} or not {"code", "rounds", "by_reason"} <= set(notice):
            raise ValueError("invalid closed response")


def _campaign_shared(row: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Checks both families share; ``(lineage_read, details_read, unreadable)``."""
    from evolution_lab.owner.progress import validate_progress
    from evolution_lab._reflector_probe import validate_probe
    validate_probe(row["reflector_probe"], row["reflector_probe_state"], row["reflector_probe_reason"])
    if row["champions_state"] not in {"present", "unavailable"}:
        raise ValueError("invalid champions_state")
    lineage_read = row["champions_state"] == "present"
    details_read = row["details_state"] == "present"
    if row["details_state"] not in {"present", "unavailable"} or row["details_source"] != "core_raw" or (
            row["details_reason"] is not None if details_read
            else row["details_reason"] not in {"core_raw_unsupported", "unreadable"}):
        raise ValueError("invalid details")
    unreadable = row["details_state"] == "unavailable" and row["details_reason"] == "unreadable"
    if not isinstance(row["caveats"], list):
        raise ValueError("invalid caveats")
    for caveat in row["caveats"]:
        _closed(caveat, _CAVEAT)
    # Family-14 blocks may all be null only next to unreadable details.
    blocks = (row["roles"], row["settings"], row["connection"])
    if not unreadable or blocks != (None, None, None):
        _family_14_blocks(row)
    ResourceRef("operation", row["operation_id"])
    _closed(row["policy"], {"rounds", "attempts_per_round", "metric_primary"})
    if row["progress"] is not None:
        validate_progress(row["progress"], "campaign")
    if row["outcome"] is not None:
        _closed(row["outcome"], {"outcome", "reason"})
    for name in ("rounds", "measurements", "suspensions"):
        if not isinstance(row[name], list):
            raise ValueError(f"invalid {name}")
    # An unread lineage is null next to its state, never an empty chain (Global Constraints).
    if not (isinstance(row["champions"], list) if lineage_read else row["champions"] is None):
        raise ValueError("invalid champions")
    for champion in row["champions"] or ():
        _closed(champion, {"version", "digest", "origin", "attempt_id", "measurement_id"})
    for item in row["measurements"]:
        _measurement_row(item, detail=False, notices=False)
    # Lab's notices beside core's outcome (PDR-0025): codes and counts, never texts.
    _campaign_notices(row)
    return lineage_read, details_read, unreadable


def _campaign_round(item: dict[str, Any], details_read: bool) -> None:
    if details_read:
        _campaign_reflection(item["reflection"])
    elif item["reflection"] is not None or item["baseline_version"] is not None:
        raise ValueError("invalid round")
    if not isinstance(item["attempts"], list):
        raise ValueError("invalid attempts")


def _campaign_attempt(attempt: dict[str, Any], lineage_read: bool, details_read: bool) -> None:
    from evolution_lab.owner.campaign_views import GROUPS
    if attempt["failure"] is not None:
        if not details_read:
            raise ValueError("invalid attempt")
        _campaign_failure(attempt["failure"])
    has_tree = attempt["has_tree"]
    if attempt["group"] not in GROUPS or (
            not isinstance(has_tree, bool) if lineage_read else has_tree is not None):
        raise ValueError("invalid attempt")


def _campaign_view_15(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, _CAMPAIGN_VIEW_15)
    if row["family"] != 15:
        raise ValueError("invalid family")
    lineage_read, details_read, unreadable = _campaign_shared(row)
    _checks(row["checks"], row["checks_state"], row["checks_reason"])
    for item in row["rounds"]:
        _closed(item, _CAMPAIGN_ROUND_15)
        # independent can fail while the separately admitted round facts stay
        # readable. Only family15's explicit unreadable state permits this
        # partial result; validate every retained fact, not arbitrary objects.
        round_details_read = details_read or (
            unreadable and row["independent"] is None and item["reflection"] is not None)
        if round_details_read:
            _str(item["baseline_version"], nullable=True)
        _campaign_round(item, round_details_read)
        for attempt in item["attempts"]:
            _closed(attempt, _CAMPAIGN_ATTEMPT_15)
            _str(attempt["outcome"], nullable=True)
            _str(attempt["reason"], nullable=True)
            _campaign_attempt(attempt, lineage_read, round_details_read)
    # The block is null only next to unreadable details (owner/campaign_views.py::_view_15).
    if row["independent"] is None:
        if not unreadable:
            raise ValueError("invalid independent")
    else:
        _independent(row["independent"])
    return value


def _campaign_view_14(value: dict[str, Any]) -> dict[str, Any]:
    """Family 14: ``/7``'s closed form and checks (spec D4: family 14 Core content unchanged inside ``/12``)."""
    row = _closed(value, _CAMPAIGN_VIEW_14)
    lineage_read, details_read, unreadable = _campaign_shared(row)
    # Family 14's gate mode is null only next to unreadable details (its caveats could not be read,
    # owner/campaign_views.py); unreadable details with readable caveats keep the gate mode.
    if row["family"] not in {14} or isinstance(row["family"], bool) or (
            row["gate_mode"] not in _GATE_MODES | ({None} if unreadable else set())):
        raise ValueError("invalid family")
    for item in row["rounds"]:
        _closed(item, _CAMPAIGN_ROUND)
        if item["final"] is not None:
            _final(item["final"])
        _campaign_round(item, details_read)
        for attempt in item["attempts"]:
            _closed(attempt, _CAMPAIGN_ATTEMPT)
            if attempt["confidence"] is not None:
                _confidence(attempt["confidence"])
            if attempt["final_decision"] is not None:
                decision = _closed(attempt["final_decision"], {"state", "reason"})
                if decision["state"] not in _FINAL_DECISIONS:
                    raise ValueError("invalid final_decision")
            _campaign_attempt(attempt, lineage_read, details_read)
    return value


def _keys_of(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return _closed(value, set(keys))


def _statistic(value: Any) -> None:
    """A family-15 statistical quantity: null, or ``{value, exact}`` -- ``value`` a finite number or null (outside
    the display range), ``exact`` Core's ``rational-hex/1`` object (``owner/exact_numbers.py``)."""
    if value is None:
        return
    row = _closed(value, {"value", "exact"})
    number = row["value"]
    if number is not None and (not isinstance(number, (int, float)) or isinstance(number, bool)
                               or not math.isfinite(number)):
        raise ValueError("invalid statistic")
    exact = _closed(row["exact"], {"format", "numerator", "denominator"})
    if exact["format"] != "rational-hex/1" or not all(
            isinstance(exact[name], str) and exact[name] for name in ("numerator", "denominator")):
        raise ValueError("invalid statistic")


def _plain_block(value: Any, keys: tuple[str, ...]) -> None:
    """A copied block of counts and codes: closed keys, JSON scalars or lists of strings only."""
    for item in _keys_of(value, keys).values():
        if isinstance(item, list):
            _string_list(item)
        elif item is not None and not isinstance(item, (str, bool)) and not _count(item):
            raise ValueError("invalid block value")


def _count_or_null(value: Any) -> bool:
    return value is None or _count(value)


def _independent_fallback(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv
    if value is not None:
        row = _keys_of(value, iv.FALLBACK_KEYS)
        if row["reason"] not in iv.FALLBACK_REASONS:
            raise ValueError("invalid fallback")


def _independent_calibration(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv
    if value is None:
        return
    row = _keys_of(value, iv.CALIBRATION_KEYS)
    prefix = _keys_of(row["prefix"], iv.PREFIX_KEYS)
    if row["source"] not in {"stored", "fresh"} or row["method"] != "f13-bootstrap/1" \
            or not _count(row["retained"]) or not isinstance(row["degenerate"], bool) \
            or prefix["version"] != 1 or prefix["kind"] != "journal_prefix":
        raise ValueError("invalid calibration")
    for key in ("measurement_id", "seed_input", "result_digest"):
        _str(row[key])
    for key in ("run_id", "locator", "content_digest"):
        _str(prefix[key])
    for key in ("level", "allowance"):
        _statistic(row[key])


def _independent_scope(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv
    row = _keys_of(value, iv.SCOPE_KEYS)
    if row["display"] not in iv.DISPLAY or not all(isinstance(row[k], str) for k in ("name", "rule", "status")) \
            or not (_count(row["declared_count"]) and _count(row["unit_count"])) \
            or row["strict"] not in (None, True, False):
        raise ValueError("invalid scope")
    if row["name"] not in {"focus", "causal", "global"} or row["rule"] not in {"point", "bound", "off", "bootstrap"} \
            or row["status"] not in {"available", "unavailable", "skipped", "not_measured"} \
            or row["pairing"] not in {None, "exact", "unpaired"} \
            or not (row["exact"] is None or type(row["exact"]) is bool):
        raise ValueError("invalid scope")
    _independent_fallback(row["fallback"])
    for name in iv.SCOPE_QUANTITIES:
        _statistic(row[name])
    if row["enclosure"] is not None:
        enclosure = _keys_of(row["enclosure"], iv.ENCLOSURE_KEYS)
        _statistic(enclosure["lower"])
        _statistic(enclosure["upper"])
        if not _count(enclosure["steps"]):
            raise ValueError("invalid enclosure")


def _independent_comparison(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv
    row = _keys_of(value, iv.COMPARISON_KEYS)
    _str(row["reason"], nullable=True)
    _str(row["reference_measurement_id"], nullable=True)
    if row["mode"] not in {"point", "confidence", "unfiltered", "statistical"} \
            or row["bound_fallback"] not in {None, "point"} \
            or row["method"] not in {None, "bounded-weighted-mean/1", "f13-bootstrap/1"} \
            or row["unit"] not in {None, "case", "group"}:
        raise ValueError("invalid comparison")
    _independent_calibration(row["calibration"])
    if row["caveats"] is not None:
        _string_list(row["caveats"])
        if any(code not in iv.COMPARISON_CAVEATS for code in row["caveats"]):
            raise ValueError("invalid comparison caveats")
    # The derived words are Lab's, next to Core's recorded values (Q1, Q2 as corrected).
    if row["layer"] not in iv.LAYERS or row["layer"] != ("global" if row["deciding_layer"] == "global" else "gate") \
            or row["deciding_layer"] not in ("focus", "causal", "global"):
        raise ValueError("invalid layer")
    if row["reference"] not in iv.REFERENCES or row["reference"] != {
            "stored_non_contemporaneous": "stored", "fresh": "fresh", None: "unknown"}.get(row["reference_kind"]):
        raise ValueError("invalid reference")
    if not isinstance(row["eligible"], bool) or not _count(row["excluded_reference_slots"]):
        raise ValueError("invalid comparison")
    _string_list(row["selection_bias_possible"])
    coverage = _closed(row["coverage"], {"candidate", "reference"})
    for side in coverage.values():
        if not all(_count(v) for v in _keys_of(side, iv.COVERAGE_SIDE_KEYS).values()):
            raise ValueError("invalid coverage")
    noise = _keys_of(row["noise"], iv.NOISE_KEYS + (("repeat_variance",) if "repeat_variance" in row["noise"] else ()))
    _statistic(noise["allowance"])
    if not (_count(noise["observed_cases"]) and _count(noise["extra_measurements"])):
        raise ValueError("invalid noise")
    if "repeat_variance" in noise:
        variance = _keys_of(noise["repeat_variance"], iv.REPEAT_VARIANCE_KEYS)
        missing = variance["unavailable_counts"]
        if noise["kind"] != "observed" or noise["units"] != "score_squared" \
                or noise["variation"] not in {"observed", "unavailable"} \
                or not isinstance(missing, dict) or not set(missing) <= set(iv.VARIANCE_UNAVAILABLE) \
                or any(not _count(total) or total < 1 for total in missing.values()) \
                or bool(missing) != (noise["variation"] == "unavailable"):
            raise ValueError("invalid repeat variance")
        for name in ("minimum", "maximum"):
            _statistic(variance[name])
            if (variance[name] is not None) != bool(noise["observed_cases"]):
                raise ValueError("invalid repeat variance")
    if not isinstance(row["scopes"], list):
        raise ValueError("invalid scopes")
    for scope in row["scopes"]:
        _independent_scope(scope)


def _independent_explanation(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv
    row = _keys_of(value, iv.EXPLANATION_KEYS)
    if row["bound_fallback"] not in {None, "point"}:
        raise ValueError("invalid preflight")
    if not isinstance(row["scopes"], list):
        raise ValueError("invalid preflight")
    for scope in row["scopes"]:
        item = _keys_of(scope, iv.PREFLIGHT_SCOPE_KEYS)
        if item["status"] not in {"skipped", "calculated", "uncalibrated"} or not _count_or_null(item["unit_count"]) \
                or item["strict"] not in (None, True, False) or item["passes"] not in (None, True, False):
            raise ValueError("invalid preflight scope")
        _independent_fallback(item["fallback"])
        for name in iv.PREFLIGHT_QUANTITIES:
            _statistic(item[name])
        if item["e_max"] is not None:
            e_max = _keys_of(item["e_max"], iv.E_MAX_KEYS)
            if e_max["kind"] not in {"finite", "positive_infinity"}:
                raise ValueError("invalid preflight scope")
            _statistic(e_max["value"])


def _public_code(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise ValueError("invalid report code")


def _independent_role_failure(item: dict[str, Any]) -> None:
    if item["category"] not in {"content", "provider", "unknown", "lifecycle"}:
        raise ValueError("invalid role failure")
    _public_code(item["reason"])


def _independent_round_progress(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv

    if not isinstance(value, list):
        raise ValueError("invalid round progress")
    for entry in value:
        row = _keys_of(entry, iv.ROUND_PROGRESS_KEYS)
        counts = [row[k] for k in ("attempts_measured", "candidates_sealed", "attempts_opened")]
        if not _count(row["round"]) or row["round"] == 0 or not all(_count(c) for c in counts) \
                or counts != sorted(counts) or not isinstance(row["reflections"], list):
            raise ValueError("invalid round progress")
        for name in ("outcome", "reason"):
            if row[name] is not None:
                _public_code(row[name])
        for reflection in row["reflections"]:
            item = _keys_of(reflection, iv.REFLECTION_PROGRESS_KEYS)
            if not isinstance(item["suspended"], bool):
                raise ValueError("invalid Reflection progress")
            if item["status"] == "failed":
                _independent_role_failure(item)
            elif item["status"] not in {"started", "reflected", "declined"} or item["category"] is not None \
                    or item["reason"] is not None or item["suspended"]:
                raise ValueError("invalid Reflection progress")


def _independent_attempt_progress(value: Any) -> None:
    from evolution_lab.owner import independent_view as iv

    parent = ("parent_measurement",) if isinstance(value, dict) and "parent_measurement" in value else ()
    row = _keys_of(value, iv.ATTEMPT_PROGRESS_KEYS + parent)
    states = {"bound", "started", "scored"}
    if row["editor"] not in {"not_started", "started", "edited", "declined", "failed"} \
            or row["measurement"] not in states | {"not_bound"} \
            or not isinstance(row["candidate_sealed"], bool) or not isinstance(row["editor_failures"], list) \
            or (not row["candidate_sealed"] and row["measurement"] != "not_bound") \
            or (parent and row["parent_measurement"] not in states):
        raise ValueError("invalid Attempt progress")
    for entry in row["editor_failures"]:
        _independent_role_failure(_keys_of(entry, iv.ROLE_FAILURE_KEYS))


def _independent(value: Any) -> None:
    """The family-15 ``independent`` block of ``lab-campaign-view/15`` (``owner/independent_view.py``)."""
    from evolution_lab.owner import independent_view as iv
    from evolution_lab.owner.campaign_views import GROUPS
    optional = tuple(name for name in ("round_progress", "suspension") if isinstance(value, dict) and name in value)
    row = _keys_of(value, iv.BLOCK_KEYS + optional)
    if row["evidence"] not in iv.EVIDENCE or not isinstance(row["next_action"], str):
        raise ValueError("invalid independent")
    if "suspension" in row:
        suspension = _keys_of(row["suspension"], iv.SUSPENSION_KEYS)
        for code in suspension.values():
            _public_code(code)
    if "round_progress" in row:
        _independent_round_progress(row["round_progress"])
    settings = _keys_of(row["settings"], iv.SETTINGS_KEYS + (("stages",) if "stages" in row["settings"] else ()))
    if "stages" in settings and settings["stages"] != "focus_first":
        raise ValueError("invalid independent stages")
    if settings["mode"] not in {"point", "confidence", "unfiltered", "statistical"} \
            or settings["bound_fallback"] not in {None, "point"}:
        raise ValueError("invalid independent settings")
    noise = _keys_of(settings["execution_noise"], iv.EXECUTION_NOISE_KEYS)
    _statistic(noise["allowance"])
    _keys_of(settings["statistics"], iv.STATISTICS_KEYS)
    if not isinstance(settings["fresh_parent"], bool) or not all(
            _count(settings[k]) for k in ("repeats", "train_cases", "train_slots")):
        raise ValueError("invalid independent settings")
    if not isinstance(row["attempts"], list):
        raise ValueError("invalid independent attempts")
    for attempt in row["attempts"]:
        has_stages = settings.get("stages") == "focus_first" and "slots" in attempt
        item = _keys_of(attempt, iv.ATTEMPT_KEYS + (("slots",) if "slots" in attempt else ())
                        + (("stages",) if has_stages else ()) + (("progress",) if "progress" in attempt else ()))
        if has_stages:
            iv.stages_row(item["stages"])
        if "slots" in item:
            iv.slots_row(item["slots"])
        if "progress" in item:
            _independent_attempt_progress(item["progress"])
        _str(item["outcome"], nullable=True)
        _str(item["reason"], nullable=True)
        if item["group"] not in GROUPS or not _count(item["round"]) or not isinstance(item["attempt_id"], str):
            raise ValueError("invalid independent attempt")
        _statistic(item["eligible_final_score"])
        for name in ("gate_binding", "global_binding"):
            iv.binding_row(item[name])
        for name in ("comparison", "gate_comparison", "global_comparison"):
            if item[name] is not None:
                _independent_comparison(item[name])
        if item["preflight"] is not None:
            _independent_explanation(item["preflight"])
        if item["conditional_cost"] is not None:
            _plain_block(item["conditional_cost"], iv.COST_KEYS)
    attainability = _keys_of(row["attainability"], iv.ATTAINABILITY_KEYS)
    if not isinstance(attainability["preflight"], list):
        raise ValueError("invalid attainability")
    for entry in attainability["preflight"]:
        item = _keys_of(entry, iv.ROUND_PREFLIGHT_KEYS)
        if not _count(item["round"]):
            raise ValueError("invalid attainability")
        _independent_explanation(item["explanation"])
        _plain_block(item["refresh"], iv.REFRESH_KEYS)
    if attainability["local_guidance"] is not None:
        _plain_block(attainability["local_guidance"], iv.GUIDANCE_KEYS)
    work = _keys_of(row["work"], iv.WORK_KEYS)
    _plain_block(work["planned"], iv.PLANNED_KEYS)
    if work["remaining"] is not None:
        _plain_block(work["remaining"], iv.REMAINING_KEYS)
    if work["committed"] is not None:
        committed = _keys_of(work["committed"], iv.COMMITTED_KEYS)
        for name, item in committed.items():
            if name in {"measurement", "connection_preflight"}:
                if item is not None:
                    _plain_block(item, iv.COMMITTED_WORK_KEYS)
            elif item is not None and not isinstance(item, str) and not _count(item):
                raise ValueError("invalid committed work")


def _target_list(value: dict[str, Any]) -> tuple[TargetStatus, ...]:
    row = _closed(value, {"schema", "targets"})
    if row["schema"] != "lab-owner-target-list/1" or not isinstance(row["targets"], list):
        raise ValueError("invalid schema")
    return tuple(_target(item) for item in row["targets"])


def _measurement_list(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "operation_id", "measurements"})
    if not isinstance(row["measurements"], list):
        raise ValueError("invalid measurements")
    ResourceRef("operation", row["operation_id"])
    for item in row["measurements"]:
        _measurement_row(item, detail=False)
    return value


def _measurement(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "operation_id", "measurement"})
    ResourceRef("operation", row["operation_id"])
    _measurement_row(row["measurement"], detail=True)
    return value


def _operation_list(value: dict[str, Any]) -> tuple[OperationStatus, ...]:
    row = _closed(value, {"schema", "operations"})
    if row["schema"] != "lab-operation-list/8" or not isinstance(row["operations"], list):
        raise ValueError("invalid schema")
    return tuple(_operation_status(item) for item in row["operations"])


def _summary_row(item: Any) -> None:
    from evolution_lab.owner.progress import validate_progress
    from evolution_lab.owner.summary import SUMMARY_KEYS
    _closed(item, set(SUMMARY_KEYS))
    ResourceRef("operation", item["id"])
    if item["kind"] not in {"campaign", "evaluation"}:
        raise ValueError("invalid kind")
    _event_views(item)
    if item["outcome"] is not None:
        _closed(item["outcome"], {"outcome", "reason", "champion"})


def _summary_list(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "operations"})
    if not isinstance(row["operations"], list):
        raise ValueError("invalid operations")
    for item in row["operations"]:
        _summary_row(item)
    return value


def _project_operation_summary(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "project", "operations"})
    if not isinstance(row["project"], str) or not row["project"] or not isinstance(row["operations"], list):
        raise ValueError("invalid project operation summary")
    for item in row["operations"]:
        entry = _closed(item, {"link", "label", "operation_state", "operation", "outcome"})
        link = Link.from_payload(entry["link"])
        if link.to_payload() != entry["link"] or not isinstance(entry["label"], str):
            raise ValueError("invalid project operation row")
        if entry["operation_state"] not in {"present", "other_pair", "unavailable"} \
                or (entry["operation"] is None) == (entry["operation_state"] == "present"):
            raise ValueError("invalid operation state")
        if entry["operation"] is not None:
            _summary_row(entry["operation"])
        if entry["outcome"] is not None:
            _closed(entry["outcome"], {"outcome", "reason", "champion"})
    return value


def _project_2(value: Any) -> dict[str, Any]:
    project, ports = project_from_wire(value)
    if project_wire(project, ports) != value:
        raise ValueError("noncanonical project")
    return value


def _project_list(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "projects"})
    if row["schema"] != "lab-project-list/2" or not isinstance(row["projects"], list):
        raise ValueError("invalid schema")
    for item in row["projects"]:
        _project_2(item)
    return value


def _link_or_none(value: Any) -> Link | None:
    if value is None:
        return None
    link = Link.from_payload(value)
    if link.to_payload() != value:
        raise ValueError("invalid link")
    return link


def _project_operation_list(value: dict[str, Any]) -> tuple[Link, ...]:
    row = _closed(value, {"schema", "links"})
    if row["schema"] != "lab-project-operation-list/1" or not isinstance(row["links"], list):
        raise ValueError("invalid schema")
    links = tuple(Link.from_payload(item) for item in row["links"])
    if [item.to_payload() for item in links] != row["links"]:
        raise ValueError("invalid link list")
    return links


def _approval(value: Any) -> dict[str, Any]:
    row = _closed(value, {"schema", "id", "request_id", "tool", "via", "autonomy",
                          "resource", "start", "outcome", "created_at"})
    if row["schema"] != "lab-approval/1" or row["outcome"] not in ("admitted", "applied", "failed"):
        raise ValueError("invalid approval record")
    Approval.decode({key: row[key] for key in ("via", "autonomy", "tool")})
    decode_identifier(row["id"])
    _str(row["request_id"])
    if not row["request_id"]:
        raise ValueError("missing approval request id")
    resource = _closed(row["resource"], {"kind", "id"})
    if resource["kind"] not in ("operation", "target", "task", "execution_policy", "confirmation",
                               "acceptance", "sync"):
        raise ValueError("invalid approval resource")
    decode_identifier(resource["id"])
    _int(row["start"], nullable=True)
    if row["start"] is not None and row["start"] < 1:
        raise ValueError("invalid approval start")
    created_at = row["created_at"]
    _str(created_at)
    if created_at[10:11] != "T" or datetime.fromisoformat(created_at).tzinfo is None:
        raise ValueError("invalid approval timestamp")
    return row


def _approval_list(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "approvals"})
    if row["schema"] != "lab-approval-list/1" or not isinstance(row["approvals"], list):
        raise ValueError("invalid approval list")
    order = []
    for item in row["approvals"]:
        approval = _approval(item)
        order.append((approval["created_at"], approval["id"]))
    if order != sorted(order):
        raise ValueError("unordered approvals")
    return row


def _project_confirmation_list(value: dict[str, Any]) -> tuple[Confirmation, ...]:
    row = _closed(value, {"schema", "confirmations"})
    if row["schema"] != "lab-project-confirmation-list/1" or not isinstance(row["confirmations"], list):
        raise ValueError("invalid schema")
    confirmations = tuple(Confirmation.from_payload(item) for item in row["confirmations"])
    if [item.to_payload() for item in confirmations] != row["confirmations"]:
        raise ValueError("invalid confirmation list")
    return confirmations


def _stale_reason(value: Any) -> None:
    if value is None:
        return
    row = _closed(value, {"codes", "message", "changes"})
    if not isinstance(row["codes"], list) or any(not isinstance(code, str) or not code for code in row["codes"]):
        raise ValueError("invalid stale reason codes")
    if not isinstance(row["message"], str):
        raise ValueError("invalid stale reason message")


def _fact(value: Any) -> dict[str, Any]:
    row = _closed(value, {"id", "subject", "holds", "value", "source", "stale_reason", "carried_over"})
    if not isinstance(row["id"], str) or not row["id"]:
        raise ValueError("invalid fact id")
    if not isinstance(row["holds"], bool):
        raise ValueError("invalid fact holds")
    if not isinstance(row["carried_over"], list):
        raise ValueError("invalid fact carried_over")
    _stale_reason(row["stale_reason"])
    return row


def _project_facts(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "project", "implementation", "pair", "revision", "facts"})
    if row["schema"] != "lab-project-facts/5":
        raise ValueError("invalid schema")
    if not isinstance(row["project"], str) or not row["project"]:
        raise ValueError("invalid project id")
    if row["implementation"] is not None and not isinstance(row["implementation"], str):
        raise ValueError("invalid implementation id")
    pair = _closed(row["pair"], {"lab", "core"})
    if not isinstance(pair["lab"], str) or not isinstance(pair["core"], str):
        raise ValueError("invalid pair")
    revision = _closed(row["revision"], {"schema", "digest", "places", "files", "git"})
    if revision["schema"] != "lab-materials-revision/1":
        raise ValueError("invalid schema")
    # An inventory digest is "sha256:<64 hex>" (spec §3.4), unlike a bare Bundle digest.
    digest = revision["digest"]
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("invalid revision digest")
    decode_digest(digest.removeprefix("sha256:"))
    if not isinstance(revision["places"], list):
        raise ValueError("invalid revision places")
    if isinstance(revision["files"], bool) or not isinstance(revision["files"], int) or revision["files"] < 0:
        raise ValueError("invalid revision file count")
    if not isinstance(row["facts"], list):
        raise ValueError("invalid facts")
    for item in row["facts"]:
        _fact(item)
    return row


_STEP_KEYS = {"id", "title", "state", "optional", "human_only", "missing", "blocked_reason", "invalid",
              "stale_reason", "carried_over", "sources", "done_at"}
_BLOCKED_CODES = frozenset({"results_deleted", "artifact_invalid", "schema_unusable", "campaign_suspended",
                            "campaign_state_unknown"})


def _artifact_digest(value: Any) -> None:
    """A verified-bytes digest (status/6): "sha256:<64 hex>" or null."""
    if value is None:
        return
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("invalid artifact digest")
    decode_digest(value.removeprefix("sha256:"))


def _step_source(value: Any) -> None:
    """The source shapes that carry more than one key; the others stay one-key objects."""
    if not isinstance(value, dict) or len(value) == 0:
        raise ValueError("invalid step source")
    if "export" in value:
        row = _closed(value, {"export", "results_state"})
        if row["results_state"] not in {"present", "deleted"}:
            raise ValueError("invalid export results_state")
    elif "fact" in value:
        row = _closed(value, {"fact", "confirmations"})
        if not isinstance(row["confirmations"], list):
            raise ValueError("invalid confirmations")
        for item in row["confirmations"]:
            entry = _closed(item, {"id", "author", "channel", "created_at"})
            if not all(isinstance(entry[key], str) for key in entry):
                raise ValueError("invalid confirmation")
    elif "artifact" in value:
        manifest = value.get("artifact") == "@manifest"
        row = _closed(value, {"artifact"} if manifest else {"artifact", "digest"})
        if not isinstance(row["artifact"], str) or not row["artifact"]:
            raise ValueError("invalid step source")
        if not manifest:
            _artifact_digest(row["digest"])
    elif len(value) != 1 or next(iter(value)) not in {"bundle", "prepared", "operation"}:
        raise ValueError("invalid step source")


def _status_steps(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("invalid steps")
    for item in value:
        row = _closed(item, _STEP_KEYS)
        if row["state"] not in {"done", "stale", "ready", "blocked"}:
            raise ValueError("invalid step state")
        if not all(isinstance(row[key], list) for key in ("missing", "carried_over", "sources", "invalid")):
            raise ValueError("invalid step lists")
        if not isinstance(row["optional"], bool) or not isinstance(row["human_only"], bool):
            raise ValueError("invalid step flags")
        if not isinstance(row["title"], str) or not (row["done_at"] is None or isinstance(row["done_at"], str)):
            raise ValueError("invalid step")
        reason = row["blocked_reason"]
        if reason is not None:
            unusable = isinstance(reason, dict) and reason.get("code") == "schema_unusable"
            reason = _closed(reason, {"code", "schema"} if unusable else {"code"})
            if reason["code"] not in _BLOCKED_CODES or row["state"] != "blocked":
                raise ValueError("invalid blocked_reason")
            if unusable and (not isinstance(reason["schema"], str) or not reason["schema"]):
                raise ValueError("invalid blocked_reason schema")
        if row["invalid"]:
            if reason is None or reason["code"] != "artifact_invalid":
                raise ValueError("invalid step invalid")
            per_file: dict[str, list] = {}
            for entry in row["invalid"]:
                entry = _closed(entry, {"artifact", "digest", "path", "message"})
                if not all(isinstance(entry[key], str) for key in ("artifact", "path", "message")) or not entry["artifact"]:
                    raise ValueError("invalid step invalid entry")
                _artifact_digest(entry["digest"])
                if entry["digest"] is None and entry["path"] != "":
                    raise ValueError("invalid step invalid entry")
                per_file.setdefault(entry["artifact"], []).append(entry["digest"])
            if any(len(digests) > 5 or len(set(digests)) != 1 for digests in per_file.values()):
                raise ValueError("invalid step invalid")
        for source in row["sources"]:
            _step_source(source)


def _project_status(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "project", "implementation", "pair", "catalogue", "facts", "scenarios"})
    if row["schema"] != "lab-project-status/6":
        raise ValueError("invalid schema")
    if not isinstance(row["project"], str) or not row["project"]:
        raise ValueError("invalid project id")
    _closed(row["pair"], {"lab", "core"})
    _closed(row["catalogue"], {"version", "digest"})
    if not isinstance(row["facts"], list) or not isinstance(row["scenarios"], list):
        raise ValueError("invalid status lists")
    for item in row["scenarios"]:
        repeatable = item.get("repeatable") if isinstance(item, dict) else None
        keys = {"id", "version", "repeatable", "state", "missing"} | (
            {"next_cycle", "cycles"} if repeatable is True else {"steps"})
        scenario = _closed(item, keys)
        if repeatable is True:
            if scenario["state"] not in {"unavailable", "available"}:
                raise ValueError("invalid scenario state")
            _status_steps(_closed(scenario["next_cycle"], {"steps"})["steps"])
            for entry in scenario["cycles"]:
                cycle = _closed(entry, {"operation_id", "order", "pair", "current", "detail", "control_state",
                                        "resume", "outcome", "complete", "steps"})
                if isinstance(cycle["order"], bool) or not isinstance(cycle["order"], int) or cycle["order"] < 0:
                    raise ValueError("invalid cycle order")
                if not isinstance(cycle["complete"], bool):
                    raise ValueError("invalid cycle complete")
                if cycle["outcome"] is not None:
                    _closed(cycle["outcome"], {"outcome", "reason", "champion"})
                _status_steps(cycle["steps"])
        else:
            if scenario["state"] not in {"unavailable", "available", "produced"}:
                raise ValueError("invalid scenario state")
            _status_steps(scenario["steps"])
    return value


def _project_bundle_result(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "bundle", "link"})
    if row["schema"] != "lab-project-bundle-result/1":
        raise ValueError("invalid schema")
    _bundle_summary(row["bundle"])
    link = Link.from_payload(row["link"])
    if link.to_payload() != row["link"]:
        raise ValueError("invalid link")
    return row


def _project_prepared_result(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "prepared", "link"})
    if row["schema"] != "lab-project-prepared-result/1":
        raise ValueError("invalid schema")
    _prepared_summary(row["prepared"])
    link = Link.from_payload(row["link"])
    if link.to_payload() != row["link"]:
        raise ValueError("invalid link")
    return row


def _project_target_result(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "task", "link"})
    if row["schema"] != "lab-project-target-result/1":
        raise ValueError("invalid schema")
    task = TaskRef.from_payload(row["task"])
    if task.to_payload() != row["task"]:
        raise ValueError("invalid task ref")
    _link_or_none(row["link"])
    return row


_PROBE_OUTCOMES = frozenset({"answered", "off_topic", "handoff", "gave_up", "error", "infrastructure", "unspecified"})


def _int(value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not int:
        raise ValueError("invalid integer")


def _str(value: Any, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise ValueError("invalid string")


def _probe_ceiling(value: Any) -> None:
    """The one ceiling shape of the session and the turn (spec §4.1): ``turns`` always, ``tokens``/``cost`` or null."""
    row = _closed(value, {"turns", "tokens", "cost"})
    for name in ("turns", "tokens"):
        if name == "tokens" and row[name] is None:
            continue
        part = _closed(row[name], {"used", "max"})
        _int(part["used"]), _int(part["max"])
    if row["cost"] is not None:
        cost = _closed(row["cost"], {"used", "max", "currency"})
        for item in cost.values():
            _str(item)


def _probe_turn(value: Any) -> dict[str, Any]:
    row = _closed(value, {"schema", "session_id", "turn_id", "started_at", "finished_at", "latency_ms", "request",
                          "answer", "steps", "outcome", "reason", "agent_outcome", "usage", "cost", "truncated",
                          "failure", "ceiling"})
    if row["schema"] != "lab-probe-turn/1":
        raise ValueError("invalid schema")
    for name in ("session_id", "turn_id", "started_at", "finished_at", "outcome"):
        _str(row[name])
    _int(row["latency_ms"], nullable=True)
    _str(_closed(row["request"], {"content"})["content"])
    _str(row["answer"], nullable=True)
    if not isinstance(row["steps"], list):
        raise ValueError("invalid steps")
    for step in row["steps"]:
        step = _closed(step, {"index", "type", "data"})
        _int(step["index"]), _str(step["type"])
        if not isinstance(step["data"], dict):
            raise ValueError("invalid step data")
    if row["reason"] is not None:
        reason = _closed(row["reason"], {"category", "code"})
        _str(reason["category"]), _str(reason["code"])
    if row["agent_outcome"] not in _PROBE_OUTCOMES:
        raise ValueError("invalid agent outcome")
    if row["usage"] is not None:
        for item in _closed(row["usage"], {"prompt", "completion", "cached", "total"}).values():
            _int(item, nullable=True)
    if row["cost"] is not None:
        cost = _closed(row["cost"], {"amount", "currency", "attribution", "upper_bound"})
        _str(cost["amount"]), _str(cost["currency"]), _str(cost["attribution"])
        if not isinstance(cost["upper_bound"], bool):
            raise ValueError("invalid cost")
    if not isinstance(row["truncated"], bool):
        raise ValueError("invalid truncated")
    if row["failure"] is not None:
        _str(_closed(row["failure"], {"code"})["code"])
    _probe_ceiling(row["ceiling"])
    return row


_PROBE_SESSION_KEYS = {"schema", "id", "project", "implementation", "state", "closed_reason", "reset_of",
                       "created_at", "closed_at", "target", "seed", "ceiling", "budget", "tokens_incomplete",
                       "cost_unavailable", "turn_timeout_seconds"}


def _probe_session_row(value: Any, *, turns: bool) -> dict[str, Any]:
    row = _closed(value, _PROBE_SESSION_KEYS | ({"turns"} if turns else set()))
    for name in ("id", "project", "implementation", "state", "created_at"):
        _str(row[name])
    for name in ("closed_reason", "reset_of", "closed_at"):
        _str(row[name], nullable=True)
    if row["state"] not in {"open", "closed"}:
        raise ValueError("invalid state")
    for item in _closed(row["target"], {"id", "owner_generation", "boot_id", "bundle_digest", "launch_digest",
                                        "source", "seed_mode"}).values():
        _str(item)
    for item in _closed(row["seed"], {"requested", "effective"}).values():
        _int(item, nullable=True)
    _probe_ceiling(row["ceiling"])
    if row["budget"] is not None:
        budget = _closed(row["budget"], {"ledger_path", "roles", "currency"})
        _str(budget["ledger_path"]), _str(budget["currency"])
        if not isinstance(budget["roles"], list):
            raise ValueError("invalid roles")
        for role in budget["roles"]:
            _str(role)
    for name in ("tokens_incomplete", "cost_unavailable"):
        if not isinstance(row[name], bool):
            raise ValueError("invalid flag")
    if isinstance(row["turn_timeout_seconds"], bool) or not isinstance(row["turn_timeout_seconds"], (int, float)):
        raise ValueError("invalid timeout")
    if turns:
        if not isinstance(row["turns"], list):
            raise ValueError("invalid turns")
        for turn in row["turns"]:
            _probe_turn(turn)
    return row


def _probe_session(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != "lab-probe-session/1":
        raise ValueError("invalid schema")
    return _probe_session_row(value, turns=True)


def _probe_session_list(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "sessions"})
    if row["schema"] != "lab-probe-session-list/1" or not isinstance(row["sessions"], list):
        raise ValueError("invalid schema")
    for item in row["sessions"]:
        if not isinstance(item, dict) or item.get("schema") != "lab-probe-session/1":
            raise ValueError("invalid session")
        _probe_session_row(item, turns=False)
    return row


def _project_start_result(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "operation", "link"})
    if row["schema"] != "lab-project-start-result/5":
        raise ValueError("invalid schema")
    operation = _operation_status(row["operation"])
    if operation.to_payload() != row["operation"]:
        raise ValueError("invalid operation")
    _link_or_none(row["link"])
    return row



def _project_workspace(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "path"})
    if row["schema"] != "lab-project-workspace/1":
        raise ValueError("invalid schema")
    for key in ("path",):
        if not isinstance(row[key], str) or not row[key]:
            raise ValueError(f"invalid {key}")
    return row

_SYNC_MODES = frozenset({"up_to_date", "fast_forward", "merge"})
_SYNC_STATUSES = frozenset({"A", "M", "D", "R"})
_SYNC_DIRTY_STATES = frozenset({"merge_in_progress", "rebase_in_progress"})


def _sync_str(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid string")
    return value


def _sync_check(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "project", "base_revision", "upstream", "mode", "commits", "commits_truncated",
                          "files", "files_truncated", "dirty"})
    if row["schema"] != "lab-project-sync-check/1" or row["mode"] not in _SYNC_MODES:
        raise ValueError("invalid schema")
    _sync_str(row["project"]); _sync_str(row["base_revision"])
    upstream = _closed(row["upstream"], {"sha", "ref"})
    _sync_str(upstream["sha"])
    if upstream["ref"] is not None:  # null when the upstream is a commit (--rev <sha>), spec 2.1
        _sync_str(upstream["ref"])
    for flag in ("commits_truncated", "files_truncated"):
        if not isinstance(row[flag], bool):
            raise ValueError(f"invalid {flag}")
    if not isinstance(row["commits"], list) or not isinstance(row["files"], list) or not isinstance(row["dirty"], list):
        raise ValueError("invalid lists")
    for item in row["commits"]:
        commit = _closed(item, {"sha", "author", "date", "subject"})
        for key in commit:
            if not isinstance(commit[key], str):
                raise ValueError("invalid commit")
        _sync_str(commit["sha"])
    for item in row["files"]:
        if not isinstance(item, dict) or item.get("status") not in _SYNC_STATUSES:
            raise ValueError("invalid file")
        file = _closed(item, {"path", "status", "old_path"} if item["status"] == "R" else {"path", "status"})
        _sync_str(file["path"])
        if "old_path" in file:
            _sync_str(file["old_path"])
    for item in row["dirty"]:
        if not isinstance(item, dict):
            raise ValueError("invalid dirty")
        if "state" in item:
            entry = _closed(item, {"path", "state"})
            if entry["path"] is not None or entry["state"] not in _SYNC_DIRTY_STATES:
                raise ValueError("invalid dirty")
        else:
            _sync_str(_closed(item, {"path"})["path"])
    return row


def _sync_result(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "mode", "merged", "head", "project"})
    if row["schema"] != "lab-project-sync-result/1" or row["mode"] not in _SYNC_MODES:
        raise ValueError("invalid schema")
    _sync_str(row["merged"]); _sync_str(row["head"])
    _project_2(row["project"])
    return row


_CONTROL_STATES = frozenset({"reserved", "running", "stopping", "finished", "orphaned", "cleanup_required", "released"})
_RESULTS_STATES = frozenset({"present", "not_created", "deleting", "deleted", "delete_failed", "unavailable"})


def _identifier_string(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid identifier")


def _event_views(payload: dict[str, Any]) -> None:
    from evolution_lab.owner.progress import validate_progress
    for state, value in ((payload["progress_state"], payload["progress"]), (payload["times_state"], payload["times"])):
        if state not in {"not_started", "present", "unavailable"} or (value is None) == (state == "present"):
            raise ValueError("invalid state")
    if payload["progress"] is not None:
        validate_progress(payload["progress"], payload["kind"])
    if payload["times"] is not None:
        _closed(payload["times"], {"first_event_at", "last_event_at"})
    from evolution_lab.owner.models import times_marking_valid
    if not times_marking_valid(payload["kind"], payload["times"], payload["times_state"],
                               payload["times_source"], payload["times_reason"]):
        raise ValueError("invalid times marking")


def operation_event(value: Any) -> dict[str, Any]:
    """Closed decoder for one SSE ``status`` frame (``lab-operation-event/3``)."""
    row = _closed(value, {"schema", "operation_id", "owner_generation", "stream_id", "sequence", "kind", "payload"})
    if row["schema"] != "lab-operation-event/3" or row["kind"] != "status":
        raise ValueError("invalid schema")
    for key in ("operation_id", "owner_generation", "stream_id"):
        _identifier_string(row[key])
    sequence = row["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("invalid sequence")
    payload = _closed(row["payload"], {"kind", "control_state", "process", "target_cleanup", "results_state",
                                        "failure", "result_available", "persistence", "progress", "progress_state",
                                        "times", "times_state", "times_source", "times_reason", "start"})
    if payload["kind"] not in {"campaign", "evaluation"}:
        raise ValueError("invalid kind")
    if payload["control_state"] not in _CONTROL_STATES or payload["results_state"] not in _RESULTS_STATES:
        raise ValueError("invalid state")
    _process(_closed(payload["process"], {"launched", "exit_code", "cleanup_complete", "forced"}))
    _cleanup(_closed(payload["target_cleanup"], {"state", "complete"}))
    _owner_failure(payload["failure"])
    if not isinstance(payload["result_available"], bool):
        raise ValueError("invalid result_available")
    if payload["persistence"] is not None:
        _persistence(_closed(payload["persistence"], {"state", "failure"}))
    _event_views(payload)
    start = payload["start"]
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        raise ValueError("invalid start")
    return row


def _closed(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid closed response")
    return value


def _string_list(value: Any) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("invalid string list")


def _digest_list(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("invalid digest list")
    for item in value:
        decode_digest(item)


_CONTENT_BASE = {"schema", "operation_id", "disclosure", "masking", "truncated", "truncation"}
_TOO_LARGE = {"state": "unavailable", "reason": "too_large"}
_WITHHELD = {"withheld": "private"}
_ROLE_KEYS = {"source", "kind", "state", "reason", "events", "events_omitted", "tool_calls", "stderr_tail"}
_ROLE_EVENTS = {
    "input": ({"type", "text"}, {"turn"}),
    "model_message": ({"type", "turn", "text"}, set()),
    "tool_call": ({"type", "turn", "id", "name", "arguments"}, set()),
    "tool_result": ({"type", "turn", "id", "name", "text"}, {"error"}),
    "published": ({"type", "changed"}, set()),
    "reflected": ({"type", "status", "groups"}, set()),
    "rejected": ({"type", "turn", "reason", "text"}, {"error_class"}),
    "failed": ({"type", "error_class", "text"}, set()),
    "agent_summary": ({"type", "turn", "tool_turns", "file_changes", "command_failures", "last_message", "stderr_tail"}, set()),
}
_REFLECTOR_PRIVATE = {
    "tool_call": {"arguments"}, "tool_result": {"text"}, "rejected": {"reason", "text"},
    "failed": {"text"}, "agent_summary": {"file_changes", "command_failures", "last_message", "stderr_tail"},
}


def _content_base(row):
    _identifier_string(row["operation_id"])
    if row["disclosure"] not in {"public", "private"} or row["masking"] != "keys" or not isinstance(row["truncated"], bool):
        raise ValueError("invalid disclosure or truncation")
    if not isinstance(row["truncation"], list):
        raise ValueError("invalid truncation")
    seen = set()
    for entry in row["truncation"]:
        _closed(entry, {"path", "size"})
        pointer = entry["path"]
        if not isinstance(pointer, str) or not pointer.startswith("/") or pointer in seen or not _count(entry["size"]):
            raise ValueError("invalid truncation path or size")
        seen.add(pointer)
        value = row
        for part in pointer[1:].split("/"):
            # JSON Pointer escape sequences are exact; no URI decoding.
            if re.search(r"~(?![01])", part):
                raise ValueError("invalid pointer escape")
            key = part.replace("~1", "/").replace("~0", "~")
            if value == _WITHHELD or value == _TOO_LARGE:
                raise ValueError("metadata under withheld or omitted section")
            if isinstance(value, list):
                if re.fullmatch(r"0|[1-9][0-9]*", key) is None:
                    raise ValueError("invalid pointer index")
                value = value[int(key)]
            else:
                value = value[key]
        if not isinstance(value, str) or not row["truncated"]:
            raise ValueError("invalid clipped text metadata")


def _section(value):
    """Only the exact object selects the extra section union variant."""
    return value == _TOO_LARGE


def _content_source(value):
    if value != "core_raw":
        raise ValueError("invalid content source")


def _content_sample(value):
    row = _closed(value, _CONTENT_BASE | {"measurement_id", "case_id", "sample_id", "source", "execution_state",
                  "trace", "try", "input", "turns", "turns_omitted", "terminal", "scoring", "private"})
    _content_base(row)
    from evolution_lab.service.codecs import decode_measurement_id, decode_core_id
    decode_measurement_id(row["measurement_id"])
    decode_core_id(row["case_id"], "case_id")
    decode_core_id(row["sample_id"], "sample_id")
    _content_source(row["source"])
    trace = row["trace"]
    if not isinstance(trace, dict) or trace.get("state") not in {"available", "unavailable"}:
        raise ValueError("invalid trace")
    _closed(trace, {"state"} if trace["state"] == "available" else {"state", "reason"})
    if trace["state"] == "unavailable" and trace["reason"] not in {
            "not_scored", "imported", "incomplete", "trace_missing", "core_raw_unsupported", "unreadable", "too_large", "missing"}:
        raise ValueError("invalid trace reason")
    if row["try"] is not None:
        trial = _closed(row["try"], {"score_request_id", "retry_ordinal", "tries_total"})
        _str(trial["score_request_id"])
        if ((trial["retry_ordinal"] is not None and not _count(trial["retry_ordinal"]))
                or not _count(trial["tries_total"]) or trial["tries_total"] < 1):
            raise ValueError("invalid scoring try")
    if not _section(row["input"]) and row["input"] is not None:
        _closed(row["input"], {"public"})
    if not _section(row["turns"]) and not isinstance(row["turns"], list):
        raise ValueError("invalid turns")
    if not _count(row["turns_omitted"]):
        raise ValueError("invalid turns omitted")
    if not _section(row["terminal"]):
        _str(row["terminal"], nullable=True)
    scoring = row["scoring"]
    if not _section(scoring):
        _closed(scoring, {"state", "metrics", "critical_pass", "verdicts", "public_feedback"})
        if scoring["state"] not in {"admitted", "failed", "not_scored"}:
            raise ValueError("invalid scoring state")
        if scoring["state"] != "admitted":
            if any(scoring[key] is not None for key in ("metrics", "critical_pass", "verdicts", "public_feedback")):
                raise ValueError("unadmitted scorer output")
        else:
            if (not isinstance(scoring["metrics"], dict)
                    or not (type(scoring["critical_pass"]) is bool or scoring["critical_pass"] is None)
                    or not isinstance(scoring["verdicts"], list)):
                raise ValueError("invalid admitted scoring")
            for verdict in scoring["verdicts"]:
                _closed(verdict, {"criterion", "passed"})
                _str(verdict["criterion"])
                if not isinstance(verdict["passed"], bool):
                    raise ValueError("invalid verdict")
    private = row["private"]
    if row["disclosure"] == "public":
        if private != _WITHHELD:
            raise ValueError("public oracle disclosure")
    elif not _section(private):
        keys = {"oracle", "diagnostics", "details", "verdict_notes"}
        if isinstance(private, dict) and "scorer_output" in private:
            keys.add("scorer_output")
            # Section fallback may remove scoring while keeping private diagnostics.
            if not _section(scoring) and scoring["state"] != "failed":
                raise ValueError("unexpected scorer diagnostics")
            output = _closed(private["scorer_output"], {"stdout", "exit_status"})
            _str(output["stdout"], nullable=True)
            _int(output["exit_status"], nullable=True)
        _closed(private, keys)
        if not isinstance(private["verdict_notes"], list):
            raise ValueError("invalid verdict notes")
        for note in private["verdict_notes"]:
            _closed(note, {"criterion", "note"})
    return row


def _content_events(events, role, disclosure):
    if _section(events):
        return
    if not isinstance(events, list):
        raise ValueError("invalid role events")
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in _ROLE_EVENTS:
            raise ValueError("invalid role event")
        kind = event["type"]
        if role == "reflector" and disclosure == "public" and kind in {"input", "model_message"}:
            if event != {"type": kind, **_WITHHELD}:
                raise ValueError("public reflector text")
            continue
        if (role == "reflector" and kind == "published") or (role == "editor" and kind == "reflected"):
            raise ValueError("event in wrong role")
        required, optional = _ROLE_EVENTS[kind]
        if not required <= set(event) or set(event) - required - optional:
            raise ValueError("invalid closed role event")
        private_fields = _REFLECTOR_PRIVATE.get(kind, set()) if role == "reflector" and disclosure == "public" else set()
        for key in private_fields:
            if event[key] != _WITHHELD:
                raise ValueError("public reflector field")
        for key in ({"text", "arguments", "reason", "last_message", "stderr_tail"} & set(event)) - private_fields:
            # Tool arguments may be a parsed JSON value; summaries may have no text.
            if key != "arguments":
                _str(event[key], nullable=kind == "agent_summary")
        if "error" in event and not isinstance(event["error"], bool):
            raise ValueError("invalid tool error")


def _content_role(part, role, disclosure):
    _content_source(part["source"])
    if part["kind"] not in {None, "chat", "agent_cli", "subprocess"} or part["state"] not in {"available", "unavailable"}:
        raise ValueError("invalid role state")
    if part["reason"] not in {None, "not_recorded", "incomplete", "core_raw_unsupported", "unreadable", "too_large", "log_missing", "missing"}:
        raise ValueError("invalid role reason")
    if part["tool_calls"] not in {"recorded", "not_recorded"} or not _count(part["events_omitted"]):
        raise ValueError("invalid role accounting")
    _content_events(part["events"], role, disclosure)
    stderr = part["stderr_tail"]
    if _section(stderr):
        return
    if role == "reflector" and disclosure == "public":
        if stderr is not None and stderr != _WITHHELD:
            raise ValueError("public reflector stderr")
    else:
        _str(stderr, nullable=True)


def _content_attempt(value):
    row = _closed(value, _CONTENT_BASE | {"attempt_id", "round", "confidence", "editor"})
    _content_base(row)
    from evolution_lab.service.codecs import decode_attempt_id
    decode_attempt_id(row["attempt_id"])
    _int(row["round"])
    confidence = row["confidence"]
    if confidence is not None and not _section(confidence):
        _closed(confidence, {"method", "levels", "tolerance", "retained", "parent_source", "repeats", "focus_cases",
                             "causal_cases", "calibration", "looks"})
        if not isinstance(confidence["looks"], list):
            raise ValueError("invalid confidence looks")
        for look in confidence["looks"]:
            _closed(look, {"look", "final", "decision", "reason", "would_pass_point", "caveats", "focus", "causal", "overall"})
            for name in ("focus", "causal", "overall"):
                if look[name] is not None:
                    _closed(look[name], {"point", "lower", "upper", "level", "unavailable"})
    editor = _closed(row["editor"], _ROLE_KEYS | {"changed", "changed_count", "failure"})
    _content_role(editor, "editor", row["disclosure"])
    if editor["changed"] is not None and not _section(editor["changed"]):
        _string_list(editor["changed"])
    if editor["changed_count"] is not None and not _count(editor["changed_count"]):
        raise ValueError("invalid changed count")
    failure = editor["failure"]
    if failure is not None and not _section(failure):
        keys = {"error_class", "reason"}
        if "text" in failure or "text_source" in failure:
            keys |= {"text", "text_source"}
            _str(failure["text"])
            if failure["text_source"] not in {"editor_log", "stderr"}:
                raise ValueError("invalid failure source")
        _closed(failure, keys)
    return row


def _content_reflection(value):
    row = _closed(value, _CONTENT_BASE | _ROLE_KEYS | {"reflection_id", "round", "skipped"})
    _content_base(row)
    from evolution_lab.service.codecs import decode_reflection_id
    decode_reflection_id(row["reflection_id"])
    _int(row["round"])
    if not _count(row["skipped"]):
        raise ValueError("invalid skipped")
    _content_role(row, "reflector", row["disclosure"])
    return row


def _suspension_evidence(value):
    row = _closed(value, {"schema", "operation_id", "state", "source", "reason", "suspensions"})
    # The public immutable model owns the same closed history validation as Python.
    # validate's canonical round trip also pins schema/source and JSON list shape.
    return SuspensionEvidenceResult(row["operation_id"], row["state"], row["suspensions"], row["reason"])


def _bundle_measurements(value):
    from evolution_lab.owner.models import Coverage, RESULTS_STATES
    from evolution_lab.preparation.models import parse_call_retries, parse_min_coverage, parse_sample_retries
    from evolution_lab._outcomes import validate_unmeasured
    row = _closed(value, {"schema", "bundle_digest", "measurement_basis", "items"})
    decode_digest(row["bundle_digest"])
    if row["measurement_basis"] is not None:
        decode_digest(row["measurement_basis"])
    if not isinstance(row["items"], list) or row["measurement_basis"] is None and row["items"]:
        raise ValueError("invalid measurement items")
    for entry in row["items"]:
        item = _closed(entry, {"operation_id", "prepared_digest", "bundle_digest", "same_bundle",
            "control_state", "results_state", "failure", "times", "outcome", "plan"})
        _identifier_string(item["operation_id"])
        for name in ("prepared_digest", "bundle_digest"):
            decode_digest(item[name])
        if type(item["same_bundle"]) is not bool or item["same_bundle"] != (item["bundle_digest"] == row["bundle_digest"]):
            raise ValueError("invalid Bundle relationship")
        if item["control_state"] not in {"reserved", "running", "stopping", "finished", "orphaned", "cleanup_required", "released"} or item["results_state"] not in RESULTS_STATES:
            raise ValueError("invalid operation state")
        _owner_failure(item["failure"])
        if item["times"] is not None:
            times = _closed(item["times"], {"first_event_at", "last_event_at"})
            if any(not isinstance(time, str) or not time.endswith("Z") for time in times.values()):
                raise ValueError("invalid times")
        if item["outcome"] is not None:
            outcome = _closed(item["outcome"], {"complete", "coverage", "stopped_reason", "unmeasured", "min_coverage"})
            if outcome["complete"] is not None and type(outcome["complete"]) is not bool:
                raise ValueError("invalid completion")
            if outcome["coverage"] is not None:
                Coverage(**_closed(outcome["coverage"], {"expected", "scored"}))
            _str(outcome["stopped_reason"], nullable=True)
            validate_unmeasured(outcome["unmeasured"])
            if parse_min_coverage(outcome["min_coverage"], "read") is None:
                raise ValueError("missing coverage threshold")
        plan = _closed(item["plan"], {"evaluation_set_id", "split", "case_ids_digest", "case_count",
            "repeat_seeds", "max_turns", "timeout_seconds", "metric_plan_digest", "concurrency",
            "scorer_concurrency", "sample_retries", "min_coverage", "max_consecutive_unmeasured",
            "call_retries", "max_failed_turns"})
        if not isinstance(plan["evaluation_set_id"], str) or not plan["evaluation_set_id"] or plan["split"] not in (None, "train", "test"):
            raise ValueError("invalid selection")
        for name in ("case_ids_digest", "metric_plan_digest"):
            decode_digest(plan[name])
        for name in ("case_count", "max_turns", "concurrency", "scorer_concurrency", "max_failed_turns"):
            if type(plan[name]) is not int or plan[name] < 1:
                raise ValueError("invalid plan count")
        stop = plan["max_consecutive_unmeasured"]
        if stop != "off" and (type(stop) is not int or stop < 1):
            raise ValueError("invalid stop count")
        timeout = plan["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < math.inf:
            raise ValueError("invalid timeout")
        seeds = plan["repeat_seeds"]
        if not isinstance(seeds, list) or not seeds or any(seed is not None and type(seed) is not int for seed in seeds):
            raise ValueError("invalid repeat seeds")
        parse_sample_retries(plan["sample_retries"], "read")
        if parse_min_coverage(plan["min_coverage"], "read") is None:
            raise ValueError("missing coverage threshold")
        parse_call_retries(plan["call_retries"], "read")
    return row


def _project_tree(value: dict[str, Any]) -> dict[str, Any]:
    row = _closed(value, {"schema", "project", "implementation", "path", "exists", "digest",
                          "bundle_covers", "bundle_reason", "files"})
    for name in ("project", "implementation", "path"):
        if not isinstance(row[name], str) or not row[name]:
            raise ValueError("invalid tree identity")
    if type(row["exists"]) is not bool or (row["bundle_covers"] is not None and type(row["bundle_covers"]) is not bool):
        raise ValueError("invalid tree status")
    if row["bundle_reason"] is not None and (not isinstance(row["bundle_reason"], str) or not row["bundle_reason"]):
        raise ValueError("invalid bundle reason")
    if row["digest"] is not None and (not isinstance(row["digest"], str)
                                      or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"])):
        raise ValueError("invalid tree digest")
    if not isinstance(row["files"], list):
        raise ValueError("invalid tree files")
    for item in row["files"]:
        item = _closed(item, {"path", "type", "size", "sha256", "executable", "target", "text"})
        if not isinstance(item["path"], str) or not item["path"] or item["type"] not in {"file", "directory", "symlink", "other"}:
            raise ValueError("invalid tree entry")
        if item["size"] is not None and not _count(item["size"]):
            raise ValueError("invalid tree size")
        if not _count(item["executable"]):
            raise ValueError("invalid executable")
        if item["sha256"] is not None and (not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("invalid tree hash")
        if item["target"] is not None and not isinstance(item["target"], str):
            raise ValueError("invalid symlink")
        if item["text"] is not None and type(item["text"]) is not bool:
            raise ValueError("invalid text marker")
    return row


def _inputs(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'operation_id', 'starts'} or value['schema'] != 'lab-operation-inputs/1':
        raise ValueError('invalid operation inputs')
    _identifier_string(value['operation_id'])
    if not isinstance(value['starts'], list) or not value['starts']:
        raise ValueError('invalid input starts')
    for index, row in enumerate(value['starts'], 1):
        if (not isinstance(row, dict) or set(row) != {'start', 'prepared_digest', 'bundle_digest'}
                or type(row['start']) is not int or row['start'] != index):
            raise ValueError('invalid input start')
        decode_digest(row['prepared_digest']); decode_digest(row['bundle_digest'])
    return value


def _trace_summary(value):
    fields = {'tries', 'scored', 'failed', 'unavailable', 'unverified', 'in_flight', 'unset_secrets'}
    if (not isinstance(value, dict) or set(value) != fields | {'schema'}
            or value['schema'] != 'lab-trace-summary/1' or any(not _count(value[key]) for key in fields)):
        raise ValueError('invalid trace summary')
    return value


def validate(value: dict[str, Any], expected_schema: str | None) -> Any:
    """Validate only public DTO envelopes; report/core payload remains opaque."""
    if expected_schema is None:
        return value
    validators = {
        'lab-trace-summary/1': lambda: _trace_summary(value),
        'lab-operation-refusal/1': lambda: RefusalRecord.from_payload(value),
        'lab-operation-failure/1': lambda: WorkerFailureRecord.from_payload(value),
        'lab-operation-inputs/1': lambda: _inputs(value),
        "lab-task-ref/1": lambda: TaskRef.from_payload(value),
        "lab-task-status/1": lambda: _task_status(value),
        "lab-operation-status/8": lambda: _operation_status(value),
        "lab-bundle-summary/3": lambda: _bundle_summary(value),
        "lab-bundle-measurements/1": lambda: _bundle_measurements(value),
        "lab-prepared-summary/1": lambda: _prepared_summary(value),
        "lab-owner-connection/1": lambda: _connection(value),
        "lab-owner-stop/1": lambda: _stop(value),
        "lab-owner-release/1": lambda: _release(value),
        "lab-owner-target/2": lambda: _target(value),
        "lab-owner-target-list/1": lambda: _target_list(value),
        "lab-owner-health/1": lambda: _health(value),
        "lab-approval/1": lambda: _approval(value),
        "lab-approval-list/1": lambda: _approval_list(value),
        "lab-execution-policy-record/1": lambda: PolicyRecord.from_payload(value),
        "lab-service-health/2": lambda: _service_health(value),
        "lab-owner-report/15": lambda: _report(value),
        "lab-operation-routing/1": lambda: _routing(value),
        "lab-campaign-view/15": lambda: _campaign_view(value),
        "lab-suspension-evidence/1": lambda: _suspension_evidence(value),
        "lab-operation-diff/2": lambda: DiffResult.from_payload(value),
        "lab-measurement-list/4": lambda: _measurement_list(value),
        "lab-measurement/4": lambda: _measurement(value),
        "lab-sample/1": lambda: _content_sample(value),
        "lab-attempt/1": lambda: _content_attempt(value),
        "lab-reflection/1": lambda: _content_reflection(value),
        "lab-operation-list/8": lambda: _operation_list(value),
        "lab-operation-summary-list/1": lambda: _summary_list(value),
        "lab-project/2": lambda: _project_2(value),
        "lab-project-list/2": lambda: _project_list(value),
        "lab-project-operation-list/1": lambda: _project_operation_list(value),
        "lab-project-facts/5": lambda: _project_facts(value),
        "lab-project-tree/1": lambda: _project_tree(value),
        "lab-project-operation-list/2": lambda: _project_operation_summary(value),
        "lab-project-status/6": lambda: _project_status(value),
        "lab-project-confirmation/1": lambda: Confirmation.from_payload(value),
        "lab-project-confirmation-list/1": lambda: _project_confirmation_list(value),
        "lab-project-acceptance/1": lambda: Acceptance.from_payload(value),
        "lab-project-bundle-result/1": lambda: _project_bundle_result(value),
        "lab-project-prepared-result/1": lambda: _project_prepared_result(value),
        "lab-project-target-result/1": lambda: _project_target_result(value),
        "lab-project-start-result/5": lambda: _project_start_result(value),
        "lab-project-workspace/1": lambda: _project_workspace(value),
        "lab-project-sync-check/1": lambda: _sync_check(value),
        "lab-project-sync-result/1": lambda: _sync_result(value),
        "lab-probe-session/1": lambda: _probe_session(value),
        "lab-probe-session-list/1": lambda: _probe_session_list(value),
        "lab-probe-turn/1": lambda: _probe_turn(value),
    }
    try:
        validator = validators.get(expected_schema)
        if validator is None or value.get("schema") != expected_schema:
            raise ValueError("unknown or mismatched schema")
        result = validator()
        if hasattr(result, "to_payload") and result.to_payload() != value:
            raise ValueError("noncanonical response")
    except Exception as error:
        raise ValueError("invalid public response") from error
    return result


def error(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"error"}:
        return False
    try:
        return _failure(value["error"]) is not None or isinstance(value["error"], dict)
    except ValueError:
        return False
