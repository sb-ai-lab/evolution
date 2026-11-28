"""Bundle, prepared-execution, policy, target, task and service-catalogue reads as commands (ADR-0070)."""
from __future__ import annotations

from evolution_lab.commands._core import Effect, Request, command, segment_path, with_query
from evolution_lab.service.codecs import decode_digest, decode_identifier, decode_target_query, decode_task_query


@command("bundle_show", effect=Effect.READ, schema="lab-bundle-summary/3")
def bundle_show(digest: str) -> Request:
    return Request("GET", segment_path("v1", "bundles", decode_digest(digest)), "lab-bundle-summary/3")


@command("bundle_measurements", effect=Effect.READ, schema="lab-bundle-measurements/1")
def bundle_measurements(digest: str) -> Request:
    return Request("GET", segment_path("v1", "bundles", decode_digest(digest), "measurements"),
                   "lab-bundle-measurements/1")


@command("prepared_show", effect=Effect.READ, schema="lab-prepared-summary/1")
def prepared_show(digest: str) -> Request:
    return Request("GET", segment_path("v1", "prepared-executions", decode_digest(digest)),
                   "lab-prepared-summary/1")


@command("execution_policies_list", effect=Effect.READ, schema=None)
def execution_policies_list() -> Request:
    return Request("GET", "/v1/execution-policies", None)


@command("execution_policy_show", effect=Effect.READ, schema="lab-execution-policy-record/1")
def execution_policy_show(digest: str) -> Request:
    return Request("GET", segment_path("v1", "execution-policies", decode_digest(digest)),
                   "lab-execution-policy-record/1")


@command("approvals_list", effect=Effect.READ, schema="lab-approval-list/1")
def approvals_list(operation_id: str | None = None, policy_digest: str | None = None) -> Request:
    if (operation_id is None) == (policy_digest is None):
        raise ValueError("exactly one approval filter is required")
    if operation_id is not None:
        path = with_query("/v1/approvals", operation_id=decode_identifier(operation_id))
    else:
        path = with_query("/v1/approvals", policy_digest=decode_digest(policy_digest))
    return Request("GET", path, "lab-approval-list/1")


@command("targets_list", effect=Effect.READ, schema="lab-owner-target-list/1")
def targets_list(bundle: str | None = None, ready: bool = False) -> Request:
    return Request("GET", with_query("/v1/targets", bundle=bundle, state="ready" if ready else None),
                   "lab-owner-target-list/1")


@command("target_status", effect=Effect.READ, schema="lab-owner-target/2")
def target_status(target: str, owner_generation: str) -> Request:
    decode_identifier(target)
    ref = decode_target_query(target, owner_generation)
    return Request("GET", with_query(segment_path("v1", "targets", target), owner_generation=ref.owner_generation),
                   "lab-owner-target/2")


@command("task_status", effect=Effect.READ, schema="lab-task-status/1")
def task_status(task: str, owner_generation: str) -> Request:
    decode_identifier(task)
    ref = decode_task_query(task, owner_generation)
    return Request("GET", with_query(segment_path("v1", "tasks", task), owner_generation=ref.owner_generation),
                   "lab-task-status/1")


@command("service_scenarios_list", effect=Effect.READ, schema=None)
def service_scenarios_list() -> Request:
    return Request("GET", "/v1/scenarios", None)


def _route_segment(value: object, what: str) -> str:
    """A catalogue path segment that cannot change the route (no dot segments, no ``/``)."""
    if not isinstance(value, str) or not value or value in (".", "..") or "/" in value:
        raise ValueError(f"invalid {what}")
    return value


@command("service_scenario_show", effect=Effect.READ, schema=None)
def service_scenario_show(scenario: str) -> Request:
    return Request("GET", segment_path("v1", "scenarios", _route_segment(scenario, "scenario id")), None)


@command("service_scenario_schema", effect=Effect.READ, schema=None)
def service_scenario_schema(scenario: str, schema: str) -> Request:
    if not isinstance(schema, str):
        raise ValueError("schema must be NAME/VERSION")
    name, separator, version = schema.partition("/")
    if not separator or not name or not version or "/" in version:
        raise ValueError("schema must be NAME/VERSION")
    scenario = _route_segment(scenario, "scenario id")
    name, version = _route_segment(name, "schema name"), _route_segment(version, "schema version")
    return Request("GET", segment_path("v1", "scenarios", scenario, "schemas", name, version), None,
                   kind="bytes", content_type="application/schema+json")


@command("service_requirements", effect=Effect.READ, schema=None)
def service_requirements() -> Request:
    return Request("GET", "/v1/guides/requirements", None)
