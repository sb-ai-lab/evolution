"""Explicit framework routes for the closed local service API.

The optional ``event_handler`` seam is deliberately narrow: Task4 supplies a
handler for one already-validated operation id and Request, returning a Response.
It owns sampled SSE; this module does not fabricate an event stream.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from starlette.responses import Response

from evolution_lab.owner import BaselineImportChoice, OwnerError, ReportMarkdownResult, ResourceRef

from .codecs import (
    BODY_LIMIT,
    decode_bind_request,
    decode_bundle_request,
    decode_cleanup_request,
    decode_conformance_request,
    decode_delete_results_request,
    decode_digest,
    decode_empty_request,
    decode_http_json_object,
    decode_identifier,
    decode_measurement_id, decode_core_id, decode_attempt_id, decode_reflection_id, decode_disclose,
    decode_operation_request,
    decode_policy_confirm_request,
    decode_prepare_request,
    decode_release_request,
    decode_resume_request,
    decode_trace_request,
    decode_target_query,
    decode_target_start_request,
    decode_task_query,
    serialize_public,
)
from .environment import expand_service_environment, service_health_payload
from .errors import ServiceError
from .mutations import _approval_kwargs, reject_mutation_key


EventHandler = Callable[[str, Request], Response | Awaitable[Response]]


async def _body(request: Request) -> bytes:
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_LIMIT:
            raise ServiceError("request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _json(request: Request) -> dict[str, Any]:
    _no_query(request)
    return decode_http_json_object(
        await _body(request), content_type=request.headers.get("content-type")
    )


async def _json_or_empty(request: Request) -> dict[str, Any] | None:
    _no_query(request)
    raw = await _body(request)
    if not raw:
        return None
    try:
        return decode_http_json_object(raw, content_type=request.headers.get("content-type"))
    except ServiceError as error:
        if error.code != "unsupported_media_type":
            raise
        # The empty-body workspace route already rejects non-JSON bodies as invalid requests.
        raise ServiceError("invalid_request") from None


async def _empty(request: Request) -> None:
    _no_query(request)
    if await _body(request):
        raise ServiceError("invalid_request")


def _no_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ServiceError("invalid_request")


def _optional_query(request: Request, allowed) -> dict[str, str]:
    """Only listed parameters, each at most once and non-empty; absent ones are omitted."""
    result: dict[str, str] = {}
    for name, value in request.query_params.multi_items():
        if name not in allowed or name in result or not value:
            raise ServiceError("invalid_request")
        result[name] = value
    return result


async def _reader(request: Request, allowed=frozenset()) -> dict[str, str]:
    """A read with optional listed query parameters and no body."""
    params = _optional_query(request, allowed)
    if await _body(request):
        raise ServiceError("invalid_request")
    return params


async def _query(request: Request, *names: str) -> tuple[str, ...]:
    items = request.query_params.multi_items()
    if len(items) != len(names) or {key for key, _ in items} != set(names):
        raise ServiceError("invalid_request")
    values = tuple(request.query_params[name] for name in names)
    if await _body(request):
        raise ServiceError("invalid_request")
    return values


def _public(value: Any) -> dict[str, Any]:
    return serialize_public(value)


async def _event(handler: EventHandler | None, operation_id: str, request: Request) -> Response:
    if handler is None:
        raise ServiceError("lab_error")
    response = handler(operation_id, request)
    if inspect.isawaitable(response):
        response = await response
    if not isinstance(response, Response):
        raise ServiceError("lab_error")
    return response


def install_routes(app, runtime, *, event_handler: EventHandler | None = None) -> None:
    """Attach every versioned route with one static public-method mapping."""
    from fastapi.responses import JSONResponse

    def json(value: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(_public(value) if not isinstance(value, dict) else value,
            status_code=status)

    @app.post("/v1/execution-policies")
    async def confirm_execution_policy(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_policy_confirm_request(body)
        return json(await runtime.call_mutation(key, "confirm_execution_policy", {"body": body},
            "owner", "confirm_execution_policy", value.policy, expected_digest=value.expected_digest,
            author=value.author, channel=value.channel, note=value.note, **_approval_kwargs(key, value.approval)))

    @app.get("/v1/execution-policies")
    async def execution_policies(request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        records = await runtime.call_owner("execution_policies")
        return json({"policies": [_public(record) for record in records]})

    @app.get("/v1/execution-policies/{digest}")
    async def execution_policy(digest: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("execution_policy", decode_digest(digest)))

    @app.get("/v1/approvals")
    async def approvals(request: Request):
        reject_mutation_key(request.headers)
        filters = await _reader(request, {"operation_id", "policy_digest"})
        if len(filters) != 1:
            raise ServiceError("invalid_argument")
        if "operation_id" in filters:
            records = await runtime.call_owner("approvals", operation_id=decode_identifier(filters["operation_id"]))
        else:
            records = await runtime.call_owner("approvals", policy_digest=decode_digest(filters["policy_digest"]))
        return json({"schema": "lab-approval-list/1", "approvals": [_public(record) for record in records]})

    @app.post("/v1/bundles")
    async def create_bundle(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_bundle_request(body)
        environments, expected = expand_service_environment(value.environments, runtime.service_environment)
        extra = {} if expected is None else {"expected_execution_environment": expected}
        return json(await runtime.call_mutation(key, "create_bundle", {"body": body},
            "preparation", "create_bundle", source=value.source, environments=environments, env=value.env,
            secret_names=value.secret_names, roots=value.roots, path_bindings=value.path_bindings, **extra))

    @app.get("/v1/bundles/{bundle_digest}")
    async def get_bundle(bundle_digest: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_preparation("get_bundle", decode_digest(bundle_digest)))

    @app.get("/v1/bundles/{bundle_digest}/measurements")
    async def bundle_measurements(bundle_digest: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("bundle_measurements", decode_digest(bundle_digest)))

    @app.post("/v1/prepared-executions")
    async def prepare(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_prepare_request(body)
        method = "prepare_campaign" if value.kind == "campaign" else "prepare_evaluation"
        return json(await runtime.call_mutation(key, "prepare", {"body": body},
            "preparation", method, value.bundle_digest, **value.kwargs))

    @app.get("/v1/prepared-executions/{prepared_digest}")
    async def get_prepared(prepared_digest: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_preparation("get_prepared", decode_digest(prepared_digest)))

    @app.post("/v1/connections/from-bundle", status_code=201)
    async def bind(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        digest = decode_bind_request(body)
        return json(await runtime.call_mutation(key, "bind", {"body": body},
            "owner", "bind", digest), 201)

    @app.post("/v1/connections/{connection_id}/target", status_code=202)
    async def start_target(connection_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_target_start_request(body)
        identifier = decode_identifier(connection_id)
        connection = runtime.connection(identifier)
        return json(await runtime.call_mutation(key, "start_target",
            {"path": {"connection_id": identifier}, "body": body},
            "owner", "start_target", connection, secrets=value.secrets, **({"execution_policy": value.execution_policy} if value.execution_policy is not None else {})), 202)

    @app.get("/v1/targets")
    async def list_targets(request: Request):
        reject_mutation_key(request.headers)
        params = await _reader(request, {"bundle", "state"})
        if params.get("state", "ready") != "ready":
            raise ServiceError("invalid_request")
        values = await runtime.call_owner("list_targets", bundle=params.get("bundle"), state=params.get("state"))
        return json({"schema": "lab-owner-target-list/1", "targets": [_public(value) for value in values]})

    @app.get("/v1/targets/{target_id}")
    async def target(target_id: str, request: Request):
        reject_mutation_key(request.headers)
        generation, = await _query(request, "owner_generation")
        return json(await runtime.call_owner("target", decode_target_query(target_id, generation)))

    @app.delete("/v1/targets/{target_id}", status_code=202)
    async def stop_target(target_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        generation, = await _query(request, "owner_generation")
        ref = decode_target_query(target_id, generation)
        return json(await runtime.call_mutation(key, "stop_target",
            {"path": {"target_id": target_id}, "query": {"owner_generation": generation}},
            "owner", "stop_target", ref), 202)

    @app.post("/v1/connections/{connection_id}/check", status_code=202)
    async def check(connection_id: str, request: Request):
        from .codecs import decode_check_request
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_check_request(body)
        identifier = decode_identifier(connection_id)
        connection = runtime.connection(identifier)
        return json(await runtime.call_mutation(key, "check",
            {"path": {"connection_id": identifier}, "body": body},
            "owner", "check", connection, target_ref=value.target_ref), 202)

    @app.post("/v1/connections/{connection_id}/conformance", status_code=202)
    async def conformance(connection_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_conformance_request(body)
        identifier = decode_identifier(connection_id)
        connection = runtime.connection(identifier)
        return json(await runtime.call_mutation(key, "conformance",
            {"path": {"connection_id": identifier}, "body": body},
            "owner", "conformance", connection, target_ref=value.target_ref,
            runtime_policy=value.runtime_policy), 202)

    @app.get("/v1/tasks/{task_id}")
    async def task(task_id: str, request: Request):
        reject_mutation_key(request.headers)
        generation, = await _query(request, "owner_generation")
        return json(await runtime.call_owner("task", decode_task_query(task_id, generation)))

    @app.post("/v1/tasks/{task_id}/stop")
    async def stop_task(task_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        decode_empty_request(body)
        identifier = decode_identifier(task_id)
        return json(await runtime.call_mutation(key, "stop_task",
            {"path": {"task_id": identifier}, "body": body},
            "owner", "stop", ResourceRef("task", identifier)))

    @app.post("/v1/operations", status_code=202)
    async def start(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_operation_request(body)
        conditions = ({} if body['schema'] == 'lab-service-start-request/1' else
                      dict(linked_from=value.linked_from, baseline=value.baseline, seed=value.seed))
        conditions.update(_approval_kwargs(key, value.approval))
        if value.baseline_from is not None:
            state = runtime.config.state_directory
            source = state / "artifacts" / value.baseline_from / value.baseline_from / "measurement"
            conditions["baseline_import"] = BaselineImportChoice(value.baseline_from, state, source)
        return json(await runtime.call_mutation(key, "start", {"body": body},
            "owner", "start", value.prepared_digest, target_ref=value.target_ref, secrets=value.secrets, **conditions), 202)

    @app.get("/v1/operations")
    async def list_operations(request: Request):
        reject_mutation_key(request.headers)
        params = await _reader(request, {"project", "implementation", "view"})
        view = params.pop("view", None)
        if view not in {None, "summary"}:
            raise ServiceError("invalid_request")
        if "implementation" in params and "project" not in params:
            raise ServiceError("invalid_request")
        ids = None
        if "project" in params:
            # Attribution comes from Owner's client_ref, not the registry projection.
            ids = set(await runtime.call_projects("operation_ids", params["project"], params.get("implementation")))
        if view == "summary":
            rows = await runtime.call_owner("list_summaries", ids)
            return json({"schema": "lab-operation-summary-list/1", "operations": list(rows)})
        values = await runtime.call_owner("list_operations")
        if ids is not None:
            values = [value for value in values if value.id in ids]
        return json({"schema": "lab-operation-list/8", "operations": [_public(value) for value in values]})

    @app.get("/v1/operations/{operation_id}")
    async def status(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("status", ResourceRef("operation", decode_identifier(operation_id))))

    @app.post("/v1/operations/{operation_id}/stop")
    async def stop_operation(operation_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        decode_empty_request(body)
        identifier = decode_identifier(operation_id)
        return json(await runtime.call_mutation(key, "stop_operation",
            {"path": {"operation_id": identifier}, "body": body},
            "owner", "stop", ResourceRef("operation", identifier)))

    @app.post("/v1/operations/{operation_id}/resume", status_code=202)
    async def resume_operation(operation_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_resume_request(body)
        conditions = ({} if body['schema'] == 'lab-service-resume-request/1' else
                      dict(accept=value.accept, reason=value.reason, prepared=value.prepared_digest))
        conditions.update(_approval_kwargs(key, value.approval))
        if value.observed_start is not None:
            conditions["observed_start"] = value.observed_start
        identifier = decode_identifier(operation_id)
        return json(await runtime.call_mutation(key, "resume",
            {"path": {"operation_id": identifier}, "body": body},
            "owner", "resume", ResourceRef("operation", identifier),
            target_ref=value.target_ref, secrets=value.secrets, **conditions), 202)

    @app.post('/v1/operations/{operation_id}/trace')
    async def trace(operation_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_trace_request(body)
        identifier = decode_identifier(operation_id)
        return json(await runtime.call_mutation(key, 'trace',
            {'path': {'operation_id': identifier}, 'body': body},
            'owner', 'trace', ResourceRef('operation', identifier), out=value.out, secrets=value.secrets,
            disclose_private=value.disclose_private, measurements=value.measurements,
            max_field_bytes=value.max_field_bytes))

    @app.get('/v1/operations/{operation_id}/refusal')
    async def refusal(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _reader(request)
        value = await runtime.call_owner('refusal', ResourceRef('operation', decode_identifier(operation_id)))
        if value is None:
            raise ServiceError('not_found')
        return json(value)

    @app.get('/v1/operations/{operation_id}/failure')
    async def failure(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _reader(request)
        value = await runtime.call_owner('failure', ResourceRef('operation', decode_identifier(operation_id)))
        if value is None:
            raise ServiceError('not_found')
        return json(value)

    @app.get('/v1/operations/{operation_id}/inputs')
    async def inputs(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _reader(request)
        return json(await runtime.call_owner('inputs', ResourceRef('operation', decode_identifier(operation_id))))

    @app.get("/v1/operations/{operation_id}/routing")
    async def routing(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _reader(request)
        return json(await runtime.call_owner("operation_routing", ResourceRef("operation", decode_identifier(operation_id))))

    @app.get("/v1/operations/{operation_id}/report")
    async def report(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("report", ResourceRef("operation", decode_identifier(operation_id))))

    @app.get("/v1/operations/{operation_id}/report.md")
    async def report_markdown(operation_id: str, request: Request):
        """report.md (spec 2026-10-14 report-markdown §3, ADR-0022 p. 2): the text, or the JSON error envelope."""
        reject_mutation_key(request.headers)
        await _empty(request)
        identifier = decode_identifier(operation_id)
        value = await runtime.call_owner("report_markdown", ResourceRef("operation", identifier))
        if not isinstance(value, ReportMarkdownResult):
            raise ServiceError("lab_error")
        if value.state != "available":
            if value.reason == "results_deleted":
                raise OwnerError("results_deleted", "read")
            raise ServiceError("report_unavailable", detail=f"Owner reason: {value.reason}.")
        return Response(value.markdown.encode("utf-8"), media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="report-{identifier}.md"'})

    @app.get("/v1/operations/{operation_id}/campaign")
    async def campaign(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("campaign", ResourceRef("operation", decode_identifier(operation_id))))

    @app.get("/v1/operations/{operation_id}/suspensions")
    async def suspensions(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("suspensions", ResourceRef("operation", decode_identifier(operation_id))))

    @app.get("/v1/operations/{operation_id}/measurements")
    async def measurements(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("measurements", ResourceRef("operation", decode_identifier(operation_id))))

    @app.get("/v1/operations/{operation_id}/measurements/{measurement_id}")
    async def measurement(operation_id: str, measurement_id: str, request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(await runtime.call_owner("measurement", ResourceRef("operation", decode_identifier(operation_id)),
                                             decode_measurement_id(measurement_id)))

    # Path converters let the codec refuse decoded slashes with invalid_request.
    @app.get("/v1/operations/{operation_id}/measurements/{measurement_id}/cases/{case_id:path}/samples/{sample_id:path}")
    async def sample(operation_id: str, measurement_id: str, case_id: str, sample_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await _reader(request, {"disclose"})
        return json(await runtime.call_owner(
            "sample", ResourceRef("operation", decode_identifier(operation_id)), decode_measurement_id(measurement_id),
            decode_core_id(case_id, "case_id"), decode_core_id(sample_id, "sample_id"), disclose=decode_disclose(params)))

    @app.get("/v1/operations/{operation_id}/attempts/{attempt_id}")
    async def attempt(operation_id: str, attempt_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await _reader(request, {"disclose"})
        return json(await runtime.call_owner("attempt", ResourceRef("operation", decode_identifier(operation_id)),
                                             decode_attempt_id(attempt_id), disclose=decode_disclose(params)))

    @app.get("/v1/operations/{operation_id}/reflections/{reflection_id}")
    async def reflection(operation_id: str, reflection_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await _reader(request, {"disclose"})
        return json(await runtime.call_owner("reflection", ResourceRef("operation", decode_identifier(operation_id)),
                                             decode_reflection_id(reflection_id), disclose=decode_disclose(params)))

    @app.get("/v1/operations/{operation_id}/diff")
    async def diff(operation_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await _reader(request, {"from", "to"})
        return json(await runtime.call_owner("diff", ResourceRef("operation", decode_identifier(operation_id)),
                                             source=params.get("from", "initial"), selected=params.get("to")))

    @app.post("/v1/operations/{operation_id}/export", status_code=202)
    async def export(operation_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        decode_empty_request(body)
        identifier = decode_identifier(operation_id)
        return json(await runtime.call_mutation(key, "export",
            {"path": {"operation_id": identifier}, "body": body},
            "owner", "export", ResourceRef("operation", identifier)), 202)

    @app.get("/v1/operations/{operation_id}/exports/{export_digest}/content")
    async def export_content(operation_id: str, export_digest: str, request: Request):
        from starlette.responses import Response
        reject_mutation_key(request.headers)
        await _empty(request)
        digest = decode_digest(export_digest)
        data = await runtime.call_owner("read_export", ResourceRef("operation", decode_identifier(operation_id)), digest)
        if not isinstance(data, bytes):
            raise ServiceError("lab_error")
        return Response(data, media_type="application/zip", headers={
            "Content-Length": str(len(data)), "ETag": f'"{digest}"',
            "Content-Disposition": f'attachment; filename="{digest}.zip"',
        })

    @app.delete("/v1/operations/{operation_id}/results", status_code=202)
    async def delete_results(operation_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        _no_query(request)
        raw = await _body(request)
        identifier = decode_identifier(operation_id)
        parameters = {"path": {"operation_id": identifier}}
        kwargs = {}
        if raw:
            body = decode_http_json_object(raw, content_type=request.headers.get("content-type"))
            value = decode_delete_results_request(body)
            parameters["body"] = body
            kwargs = _approval_kwargs(key, value.approval, observed_start=value.observed_start)
        return json(await runtime.call_mutation(key, "delete_results", parameters,
            "owner", "delete_results", ResourceRef("operation", identifier), **kwargs), 202)

    @app.post("/v1/cleanup", status_code=202)
    async def cleanup(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_cleanup_request(body)
        return json(await runtime.call_mutation(key, "cleanup", {"body": body},
            "owner", "cleanup", value.ref, wait_seconds=value.wait_seconds,
            **_approval_kwargs(key, value.approval, observed_start=value.observed_start)), 202)

    @app.post("/v1/releases")
    async def release(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await _json(request)
        value = decode_release_request(body)
        return json(await runtime.call_mutation(key, "release", {"body": body},
            "owner", "release_after_manual_cleanup", value.ref, reason=value.reason,
            **_approval_kwargs(key, value.approval, observed_start=value.observed_start)))

    @app.get("/v1/health")
    async def health(request: Request):
        reject_mutation_key(request.headers)
        await _empty(request)
        return json(service_health_payload(await runtime.call_owner("health"), runtime.service_environment))

    if event_handler is not None:
        @app.get("/v1/operations/{operation_id}/events")
        async def events(operation_id: str, request: Request):
            reject_mutation_key(request.headers)
            await _empty(request)
            return await _event(event_handler, decode_identifier(operation_id), request)

    from .project_routes import install_project_routes
    install_project_routes(app, runtime, json=json, read_json=_json, json_or_empty=_json_or_empty, empty=_empty, query=_reader)
