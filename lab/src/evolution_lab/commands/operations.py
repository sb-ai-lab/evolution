"""Operation reads and service health as commands; each builds the request the CLI sends (ADR-0070)."""
from __future__ import annotations

from urllib.parse import urlencode

from evolution_lab.commands._core import Effect, Request, command, segment_path, with_query
from evolution_lab.service.codecs import (
    decode_attempt_id, decode_core_id, decode_identifier, decode_measurement_id, decode_reflection_id,
)


def _operation(operation: str, *segments: str) -> str:
    return segment_path("v1", "operations", decode_identifier(operation), *segments)


def _get(path: str, schema: str | None, **extra) -> Request:
    return Request("GET", path, schema, **extra)


@command("health", effect=Effect.READ, schema="lab-service-health/2")
def health() -> Request:
    return _get("/v1/health", "lab-service-health/2")


@command("operations_list", effect=Effect.READ, schema="lab-operation-list/8")
def operations_list() -> Request:
    return _get("/v1/operations", "lab-operation-list/8")


@command("operations_summary", effect=Effect.READ, schema="lab-operation-summary-list/1")
def operations_summary() -> Request:
    return _get("/v1/operations?view=summary", "lab-operation-summary-list/1")


@command("operation_status", effect=Effect.READ, schema="lab-operation-status/8")
def operation_status(operation: str) -> Request:
    return _get(_operation(operation), "lab-operation-status/8")


@command("operation_failure", effect=Effect.READ, schema="lab-operation-failure/1")
def operation_failure(operation: str) -> Request:
    return _get(_operation(operation, "failure"), "lab-operation-failure/1")


@command("operation_refusal", effect=Effect.READ, schema="lab-operation-refusal/1")
def operation_refusal(operation: str) -> Request:
    return _get(_operation(operation, "refusal"), "lab-operation-refusal/1")


@command("operation_inputs", effect=Effect.READ, schema="lab-operation-inputs/1")
def operation_inputs(operation: str) -> Request:
    return _get(_operation(operation, "inputs"), "lab-operation-inputs/1")


@command("operation_report", effect=Effect.READ, schema="lab-owner-report/15")
def operation_report(operation: str) -> Request:
    return _get(_operation(operation, "report"), "lab-owner-report/15")


@command("operation_report_markdown", effect=Effect.READ, schema=None)
def operation_report_markdown(operation: str) -> Request:
    return _get(_operation(operation, "report.md"), None, kind="text", content_type="text/markdown")


@command("operation_campaign", effect=Effect.READ, schema="lab-campaign-view/15")
def operation_campaign(operation: str) -> Request:
    return _get(_operation(operation, "campaign"), "lab-campaign-view/15")


@command("operation_routing", effect=Effect.READ, schema="lab-operation-routing/1")
def operation_routing(operation: str) -> Request:
    return _get(_operation(operation, "routing"), "lab-operation-routing/1")


@command("operation_suspensions", effect=Effect.READ, schema="lab-suspension-evidence/1")
def operation_suspensions(operation: str) -> Request:
    return _get(_operation(operation, "suspensions"), "lab-suspension-evidence/1")


@command("operation_measurements", effect=Effect.READ, schema="lab-measurement-list/4")
def operation_measurements(operation: str) -> Request:
    return _get(_operation(operation, "measurements"), "lab-measurement-list/4")


@command("operation_measurement", effect=Effect.READ, schema="lab-measurement/4")
def operation_measurement(operation: str, measurement: str) -> Request:
    return _get(_operation(operation, "measurements", decode_measurement_id(measurement)), "lab-measurement/4")


def _sample(operation: str, measurement: str, case: str, sample: str, disclose: bool) -> Request:
    path = _operation(operation, "measurements", decode_measurement_id(measurement),
                      "cases", decode_core_id(case, "case_id"), "samples", decode_core_id(sample, "sample_id"))
    return _get(with_query(path, disclose="private" if disclose else None), "lab-sample/1")


@command("operation_sample", effect=Effect.READ, schema="lab-sample/1")
def operation_sample(operation: str, measurement: str, case: str, sample: str) -> Request:
    return _sample(operation, measurement, case, sample, False)


@command("operation_sample_disclosed", effect=Effect.READ, schema="lab-sample/1")
def operation_sample_disclosed(operation: str, measurement: str, case: str, sample: str) -> Request:
    return _sample(operation, measurement, case, sample, True)


@command("operation_attempt", effect=Effect.READ, schema="lab-attempt/1")
def operation_attempt(operation: str, attempt: str) -> Request:
    return _get(_operation(operation, "attempts", decode_attempt_id(attempt)), "lab-attempt/1")


@command("operation_attempt_disclosed", effect=Effect.READ, schema="lab-attempt/1")
def operation_attempt_disclosed(operation: str, attempt: str) -> Request:
    path = _operation(operation, "attempts", decode_attempt_id(attempt))
    return _get(with_query(path, disclose="private"), "lab-attempt/1")


@command("operation_reflection", effect=Effect.READ, schema="lab-reflection/1")
def operation_reflection(operation: str, reflection: str) -> Request:
    return _get(_operation(operation, "reflections", decode_reflection_id(reflection)), "lab-reflection/1")


@command("operation_reflection_disclosed", effect=Effect.READ, schema="lab-reflection/1")
def operation_reflection_disclosed(operation: str, reflection: str) -> Request:
    path = _operation(operation, "reflections", decode_reflection_id(reflection))
    return _get(with_query(path, disclose="private"), "lab-reflection/1")


@command("operation_diff", effect=Effect.READ, schema="lab-operation-diff/2")
def operation_diff(operation: str, tree_from: str | None = None, tree_to: str | None = None) -> Request:
    # The CLI builds this query with urlencode (spaces become "+"), unlike its other queries.
    refs = {key: ref for key, ref in (("from", tree_from), ("to", tree_to)) if ref is not None}
    path = _operation(operation, "diff")
    return _get(f"{path}?{urlencode(refs)}" if refs else path, "lab-operation-diff/2")
