"""Owner mutation requests shared by the CLI and MCP (ADR-0070, ADR-0071).

Adapters resolve input files and legacy CLI checks before dispatch. The builders
preserve the admitted request versions, leaving service validation to the service.
"""
from __future__ import annotations

from evolution_lab.commands._core import Effect, Request, command, mutation_key, segment_path, with_query


@command("bundle_create", effect=Effect.MUTATION, schema="lab-bundle-summary/3")
def bundle_create(request_id: str | None, owner_generation: str | None, body: dict) -> Request:
    return Request("POST", "/v1/bundles", "lab-bundle-summary/3", body,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("prepare", effect=Effect.MUTATION, schema="lab-prepared-summary/1")
def prepare(request_id: str | None, owner_generation: str | None, body: dict) -> Request:
    return Request("POST", "/v1/prepared-executions", "lab-prepared-summary/1", body,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("connect", effect=Effect.MUTATION, schema="lab-owner-connection/1")
def connect(request_id: str | None, owner_generation: str | None, bundle_digest: str) -> Request:
    payload = {"schema": "lab-service-bind-request/1", "bundle_digest": bundle_digest}
    return Request("POST", "/v1/connections/from-bundle", "lab-owner-connection/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("target_start", effect=Effect.MUTATION, schema="lab-task-ref/1")
def target_start(
    request_id: str | None, owner_generation: str | None, connection_id: str, secrets: dict,
    execution_policy: str | None = None,
) -> Request:
    payload = {"schema": "lab-service-target-start-request/1", "secrets": secrets}
    if execution_policy is not None:
        payload.update(schema="lab-service-target-start-request/2", execution_policy=execution_policy)
    return Request("POST", segment_path("v1", "connections", connection_id, "target"), "lab-task-ref/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("target_stop", effect=Effect.MUTATION, schema="lab-task-ref/1")
def target_stop(
    request_id: str | None, owner_generation: str | None, target_id: str, target_generation: str | None = None,
) -> Request:
    generation = owner_generation if target_generation is None else target_generation
    path = with_query(segment_path("v1", "targets", target_id), owner_generation=generation)
    return Request("DELETE", path, "lab-task-ref/1", mutation_key=mutation_key(request_id, owner_generation))


@command("check", effect=Effect.MUTATION, schema="lab-task-ref/1")
def check(request_id: str | None, owner_generation: str | None, connection_id: str, target_ref: dict) -> Request:
    payload = {"schema": "lab-service-check-request/1", "target_ref": target_ref}
    return Request("POST", segment_path("v1", "connections", connection_id, "check"), "lab-task-ref/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("conformance", effect=Effect.MUTATION, schema="lab-task-ref/1")
def conformance(
    request_id: str | None, owner_generation: str | None, connection_id: str, target_ref: dict,
    runtime_policy: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-conformance-request/1", "target_ref": target_ref,
               "runtime_policy": runtime_policy}
    return Request("POST", segment_path("v1", "connections", connection_id, "conformance"), "lab-task-ref/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("task_stop", effect=Effect.MUTATION, schema="lab-owner-stop/1")
def task_stop(request_id: str | None, owner_generation: str | None, task_id: str) -> Request:
    return Request("POST", segment_path("v1", "tasks", task_id, "stop"), "lab-owner-stop/1", {},
                   mutation_key=mutation_key(request_id, owner_generation))


@command("operation_stop", effect=Effect.MUTATION, schema="lab-owner-stop/1")
def operation_stop(request_id: str | None, owner_generation: str | None, operation_id: str) -> Request:
    return Request("POST", segment_path("v1", "operations", operation_id, "stop"), "lab-owner-stop/1", {},
                   mutation_key=mutation_key(request_id, owner_generation))


@command("operation_start", effect=Effect.MUTATION, schema="lab-operation-status/8", asks=True)
def operation_start(
    request_id: str | None, owner_generation: str | None, prepared_digest: str, target_ref: dict, secrets: dict,
    linked_from: str | None = None, baseline: str | None = None, seed: str | None = None,
    baseline_from: str | None = None, approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-start-request/1", "prepared_digest": prepared_digest,
               "target_ref": target_ref, "secrets": secrets}
    if approval is not None:
        payload.update(schema="lab-service-start-request/4", linked_from=linked_from,
                       baseline="inherit" if baseline is None else baseline,
                       seed="champion" if seed is None else seed, baseline_from=baseline_from, approval=approval)
    elif baseline_from is not None:
        payload.update(schema="lab-service-start-request/3", linked_from=None, baseline="inherit",
                       seed="champion", baseline_from=baseline_from)
    elif linked_from is not None:
        payload.update(schema="lab-service-start-request/2", linked_from=linked_from,
                       baseline=baseline or "inherit", seed=seed or "champion")
    return Request("POST", "/v1/operations", "lab-operation-status/8", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("operation_resume", effect=Effect.MUTATION, schema="lab-operation-status/8", asks=True)
def operation_resume(
    request_id: str | None, owner_generation: str | None, operation_id: str, target_ref: dict, secrets: dict,
    accept: list[dict] | None = None, reason: str | None = None, prepared_digest: str | None = None,
    approval: dict | None = None, observed_start: int | None = None,
) -> Request:
    payload = {"schema": "lab-service-resume-request/1", "target_ref": target_ref, "secrets": secrets}
    if accept or reason is not None or prepared_digest is not None or approval is not None:
        payload.update(schema="lab-service-resume-request/2", accept=list(accept or ()),
                       reason=reason, prepared_digest=prepared_digest)
    if approval is not None:
        payload.update(schema="lab-service-resume-request/3", approval=approval, observed_start=observed_start)
    return Request("POST", segment_path("v1", "operations", operation_id, "resume"),
                   "lab-operation-status/8", payload, mutation_key=mutation_key(request_id, owner_generation),
                   effect=Effect.IRREVERSIBLE if accept else None)


@command("policy_confirm", effect=Effect.MUTATION, schema="lab-execution-policy-record/1", asks=True)
def policy_confirm(
    request_id: str | None, owner_generation: str | None, policy: dict, expected_digest: str, author: str,
    channel: str, note: str | None, approval: dict | None = None,
) -> Request:
    payload = {"schema": "lab-service-policy-confirm-request/1", "policy": policy, "expected_digest": expected_digest,
               "author": author, "channel": channel, "note": note}
    if approval is not None:
        payload.update(schema="lab-service-policy-confirm-request/2", approval=approval)
    return Request("POST", "/v1/execution-policies", "lab-execution-policy-record/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("delete_results", effect=Effect.IRREVERSIBLE, schema="lab-task-ref/1", asks=True)
def delete_results(
    request_id: str | None, owner_generation: str | None, operation_id: str,
    approval: dict | None = None, observed_start: int | None = None,
) -> Request:
    payload = None
    if approval is not None:
        payload = {"schema": "lab-service-delete-results-request/1", "approval": approval,
                   "observed_start": observed_start}
    return Request("DELETE", segment_path("v1", "operations", operation_id, "results"), "lab-task-ref/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("release", effect=Effect.IRREVERSIBLE, schema="lab-owner-release/1", asks=True)
def release(
    request_id: str | None, owner_generation: str | None, ref: dict, reason: str,
    approval: dict | None = None, observed_start: int | None = None,
) -> Request:
    payload = {"schema": "lab-service-release-request/1", "ref": ref, "reason": reason}
    if approval is not None:
        payload.update(schema="lab-service-release-request/2", approval=approval, observed_start=observed_start)
    return Request("POST", "/v1/releases", "lab-owner-release/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))


@command("cleanup", effect=Effect.IRREVERSIBLE, schema="lab-task-ref/1", asks=True)
def cleanup(
    request_id: str | None, owner_generation: str | None, ref: dict, wait_seconds: int | float | None,
    approval: dict | None = None, observed_start: int | None = None,
) -> Request:
    payload = {"schema": "lab-service-cleanup-request/1", "ref": ref, "wait_seconds": wait_seconds}
    if approval is not None:
        payload.update(schema="lab-service-cleanup-request/2", approval=approval, observed_start=observed_start)
    return Request("POST", "/v1/cleanup", "lab-task-ref/1", payload,
                   mutation_key=mutation_key(request_id, owner_generation))
