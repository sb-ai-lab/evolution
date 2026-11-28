"""Project routes (spec §4.2): thin closures over ``runtime.project_session``.

Each handler follows the order of the general routes: mutation key first (syntax and
generation before any request state is read), then the closed body, then one call
through ``runtime.call_mutation(..., "projects", ...)`` or ``runtime.call_projects``.
Session method names come only from this static table, never from a request.
"""
from __future__ import annotations

import asyncio

from fastapi import Request, Response

from evolution_lab import guides

from .codecs import decode_http_json_object
from .errors import ServiceError
from .mutations import _approval_kwargs, reject_mutation_key
from .project_codecs import (
    decode_accept,
    decode_confirm,
    decode_project_bundle,
    decode_project_create,
    decode_project_demo,
    PROBE_TURN_BODY_LIMIT,
    decode_probe_turn,
    decode_project_port,
    decode_project_probe,
    decode_project_prepare,
    decode_project_start,
    decode_project_sync,
    decode_project_sync_check,
    decode_project_target,
    decode_project_update,
    decode_project_workspace,
    decode_revoke,
)


def install_project_routes(app, runtime, *, json, read_json, json_or_empty, empty, query) -> None:
    """``query(request, allowed)`` returns the listed query parameters and rejects a body."""

    def _flag(value):
        if value not in (None, "1"):
            raise ServiceError("invalid_request")
        return value == "1"

    async def _mutate(key, route, parts, method, /, *args, **kwargs):
        return await runtime.call_mutation(key, route, parts, "projects", method, *args, **kwargs)

    @app.post("/v1/projects")
    async def create_project(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        return json(await _mutate(key, "project_create", {"body": body}, "create", **decode_project_create(body)))

    @app.get("/v1/projects")
    async def list_projects(request: Request):
        reject_mutation_key(request.headers)
        archived = _flag((await query(request, {"archived"})).get("archived"))
        return json(await runtime.call_projects("list_projects", archived=archived))

    # Registered before every "/v1/projects/{project_id}..." route below: a literal
    # "demo" segment must never be swallowed as a project id (route order, Task 3).
    @app.post("/v1/projects/demo")
    async def create_demo(request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        if runtime.service_environment is None:
            raise ServiceError("service_environment_unconfigured")
        return json(await _mutate(key, "project_demo", {"body": body}, "demo", decode_project_demo(body)))

    @app.post("/v1/projects/{project_id}/workspace")
    async def open_workspace(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await json_or_empty(request)
        autonomy = "ask" if body is None else decode_project_workspace(body)
        parts = {"path": {"project_id": project_id}}
        if body is not None:
            parts["body"] = body
        return json(await _mutate(key, "project_workspace", parts, "open_workspace", project_id,
                                  autonomy=autonomy))

    @app.post("/v1/projects/{project_id}/ports")
    async def project_port(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        fields = decode_project_port(body)
        return json(await _mutate(key, "project_port", {"path": {"project_id": project_id}, "body": body},
                                  "port", project_id, **fields))

    @app.post("/v1/projects/{project_id}/sync/check")
    async def project_sync_check(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        fields = decode_project_sync_check(body)
        return json(await _mutate(key, "project_sync_check", {"path": {"project_id": project_id}, "body": body},
                                  "sync_check", project_id, **fields))

    @app.post("/v1/projects/{project_id}/sync")
    async def project_sync(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        fields = decode_project_sync(body)
        fields.update(_approval_kwargs(key, fields.pop("approval")))
        return json(await _mutate(key, "project_sync", {"path": {"project_id": project_id}, "body": body},
                                  "sync_apply", project_id, **fields))

    @app.get("/v1/projects/{project_id}")
    async def get_project(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        await empty(request)
        return json(await runtime.call_projects("get_project", project_id))

    @app.patch("/v1/projects/{project_id}")
    async def update_project(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        mutation, expected = decode_project_update(body)
        return json(await _mutate(key, "project_update", {"path": {"project_id": project_id}, "body": body},
                                  "update", project_id, mutation, expected_record_version=expected))

    @app.post("/v1/projects/{project_id}/bundles")
    async def project_bundle(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        implementation = decode_project_bundle(body)
        summary, link = await _mutate(key, "project_bundle", {"path": {"project_id": project_id}, "body": body},
                                      "bundle", project_id, implementation)
        return json({"schema": "lab-project-bundle-result/1", "bundle": summary.to_payload(),
                     "link": link.to_payload()})

    @app.post("/v1/projects/{project_id}/prepared-executions")
    async def project_prepare(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        implementation, value = decode_project_prepare(body)
        summary, link = await _mutate(key, "project_prepare", {"path": {"project_id": project_id}, "body": body},
                                      "prepare", project_id, implementation, bundle_digest=value.bundle_digest,
                                      kind=value.kind, params=value.kwargs)
        return json({"schema": "lab-project-prepared-result/1", "prepared": summary.to_payload(),
                     "link": link.to_payload()})

    @app.post("/v1/projects/{project_id}/targets", status_code=202)
    async def project_target(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        implementation, bundle_digest, secrets = decode_project_target(body)
        task, link = await _mutate(key, "project_target", {"path": {"project_id": project_id}, "body": body},
                                   "start_target", project_id, implementation, bundle_digest=bundle_digest,
                                   secrets=secrets)
        return json({"schema": "lab-project-target-result/1", "task": task.to_payload(),
                     "link": None if link is None else link.to_payload()}, 202)

    @app.post("/v1/projects/{project_id}/probes", status_code=201)
    async def project_probe_start(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        args = decode_project_probe(body)
        return json(await _mutate(key, "project_probe_start", {"path": {"project_id": project_id}, "body": body},
                                  "probe_start", project_id, args["implementation"], ceiling=args["ceiling"],
                                  budget=args["budget"], turn_timeout_seconds=args["turn_timeout_seconds"],
                                  seed=args["seed"]), 201)

    @app.get("/v1/projects/{project_id}/probes")
    async def project_probes(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        implementation = (await query(request, {"implementation"})).get("implementation")
        return json(await runtime.call_projects("probe_list", project_id, implementation))

    @app.get("/v1/probes/{session_id}")
    async def get_probe(session_id: str, request: Request):
        reject_mutation_key(request.headers)
        await empty(request)
        return json(await runtime.call_projects("probe_session", session_id))

    @app.post("/v1/probes/{session_id}/turns")
    async def probe_turn(session_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        if request.query_params.multi_items():
            raise ServiceError("invalid_request")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > PROBE_TURN_BODY_LIMIT:
                raise ServiceError("request_too_large")
        body = decode_http_json_object(raw, content_type=request.headers.get("content-type"),
                                       max_bytes=PROBE_TURN_BODY_LIMIT)
        content = decode_probe_turn(body)
        return json(await _mutate(key, "probe_turn", {"path": {"session_id": session_id}, "body": body},
                                  "probe_turn", session_id, content))

    @app.post("/v1/probes/{session_id}/reset", status_code=201)
    async def probe_reset(session_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        if request.query_params.multi_items():
            raise ServiceError("invalid_request")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 1024:  # the body is empty or "{}"
                raise ServiceError("request_too_large")
        if raw and decode_http_json_object(raw, content_type=request.headers.get("content-type")) != {}:
            raise ServiceError("invalid_request")
        return json(await _mutate(key, "probe_reset", {"path": {"session_id": session_id}},
                                  "probe_reset", session_id), 201)

    @app.delete("/v1/probes/{session_id}")
    async def probe_close(session_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        await empty(request)
        return json(await _mutate(key, "probe_close", {"path": {"session_id": session_id}},
                                  "probe_close", session_id))

    @app.post("/v1/projects/{project_id}/operations", status_code=202)
    async def project_start(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        implementation, prepared, target_ref, secrets, approval = decode_project_start(body)
        status, link = await _mutate(key, "project_start", {"path": {"project_id": project_id}, "body": body},
                                     "start", project_id, implementation, prepared_digest=prepared,
                                     target_ref=target_ref, secrets=secrets, **_approval_kwargs(key, approval))
        return json({"schema": "lab-project-start-result/5", "operation": status.to_payload(),
                     "link": None if link is None else link.to_payload()}, 202)

    @app.get("/v1/projects/{project_id}/operations")
    async def project_operations(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await query(request, {"implementation", "view"})
        implementation, view = params.get("implementation"), params.get("view")
        if view not in {None, "summary"}:
            raise ServiceError("invalid_request")
        if view == "summary":  # lab-project-operation-list/2; the plain list stays /1
            return json(await runtime.call_projects("operations_summary", project_id, implementation))
        links = await runtime.call_projects("operations", project_id, implementation)
        return json({"schema": "lab-project-operation-list/1", "links": [link.to_payload() for link in links]})

    @app.get("/v1/projects/{project_id}/tree")
    async def project_tree(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await query(request, {"implementation", "path", "text"})
        if not params.get("implementation") or not params.get("path"):
            raise ServiceError("invalid_request")
        return json(await runtime.call_projects("tree", project_id, params["implementation"], params["path"],
                                                text=_flag(params.get("text"))))

    @app.get("/v1/projects/{project_id}/facts")
    async def project_facts(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        implementation = (await query(request, {"implementation"})).get("implementation")
        return json(await runtime.call_projects("facts_payload", project_id, implementation))

    @app.get("/v1/projects/{project_id}/status")
    async def project_status(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        params = await query(request, {"implementation", "scenario"})
        return json(await runtime.call_projects("status_payload", project_id, params.get("implementation"),
                                                params.get("scenario")))

    # The scenario catalogue is wheel data: no project session and no Lab home needed
    # (ADR-0017 p. 8 partially replaces ADR-0012's "no service route for guides").
    @app.get("/v1/scenarios")
    async def scenario_list(request: Request):
        reject_mutation_key(request.headers)
        await query(request, set())
        catalogue = await asyncio.to_thread(guides.scenarios)
        return json(catalogue.list_payload())

    @app.get("/v1/scenarios/{scenario_id}")
    async def scenario_entry(scenario_id: str, request: Request):
        reject_mutation_key(request.headers)
        await query(request, set())
        catalogue = await asyncio.to_thread(guides.scenarios)
        if catalogue.descriptor(scenario_id) is None:
            raise ServiceError("not_found")
        return json(catalogue.entry_payload(scenario_id))

    @app.get("/v1/scenarios/{scenario_id}/schemas/{name}/{version}")
    async def scenario_schema(scenario_id: str, name: str, version: str, request: Request):
        """A scenario's artifact schema, the file as shipped (spec 2026-10-01 §2.3)."""
        reject_mutation_key(request.headers)
        await query(request, set())
        catalogue = await asyncio.to_thread(guides.scenarios)
        path = catalogue.schema_path(scenario_id, f"{name}/{version}")
        if path is None:
            raise ServiceError("not_found")
        return Response(content=await asyncio.to_thread(path.read_bytes), media_type="application/schema+json")

    # PDR-0020: the requirements catalogue, the same object as `lab guides requirements --json`;
    # wheel data like the scenarios, so no project session and no Lab home.
    @app.get("/v1/guides/requirements")
    async def guides_requirements(request: Request):
        reject_mutation_key(request.headers)
        await query(request, set())
        return json(await asyncio.to_thread(guides.requirements))

    @app.post("/v1/projects/{project_id}/confirmations")
    async def project_confirm(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        fields = decode_confirm(body)
        fields.update(_approval_kwargs(key, fields.pop("approval")))
        implementation = fields.pop("implementation_id")
        return json(await _mutate(key, "project_confirm", {"path": {"project_id": project_id}, "body": body},
                                  "confirm", project_id, implementation, **fields))

    @app.get("/v1/projects/{project_id}/approvals")
    async def project_approvals(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        await empty(request)
        approvals = await runtime.call_projects("project_approvals", project_id)
        return json({"schema": "lab-approval-list/1", "approvals": approvals})

    @app.get("/v1/projects/{project_id}/confirmations")
    async def project_confirmations(project_id: str, request: Request):
        reject_mutation_key(request.headers)
        revoked = _flag((await query(request, {"revoked"})).get("revoked"))
        values = await runtime.call_projects("confirmations", project_id, revoked=revoked)
        return json({"schema": "lab-project-confirmation-list/1",
                     "confirmations": [value.to_payload() for value in values]})

    @app.post("/v1/projects/{project_id}/confirmations/{confirmation_id}/revoke")
    async def project_revoke_confirmation(project_id: str, confirmation_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        reason, approval = decode_revoke(body)
        return json(await _mutate(key, "project_revoke",
                                  {"path": {"project_id": project_id, "confirmation_id": confirmation_id},
                                   "body": body},
                                  "revoke_confirmation", project_id, confirmation_id, reason,
                                  **_approval_kwargs(key, approval)))

    @app.post("/v1/projects/{project_id}/acceptances")
    async def project_accept(project_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        fields = decode_accept(body)
        fields.update(_approval_kwargs(key, fields.pop("approval")))
        implementation = fields.pop("implementation_id")
        return json(await _mutate(key, "project_accept", {"path": {"project_id": project_id}, "body": body},
                                  "accept", project_id, implementation, **fields))

    @app.post("/v1/projects/{project_id}/acceptances/{acceptance_id}/revoke")
    async def project_revoke_acceptance(project_id: str, acceptance_id: str, request: Request):
        key = runtime.mutation_key(request.headers)
        body = await read_json(request)
        reason, approval = decode_revoke(body)
        return json(await _mutate(key, "project_revoke",
                                  {"path": {"project_id": project_id, "acceptance_id": acceptance_id},
                                   "body": body},
                                  "revoke_acceptance", project_id, acceptance_id, reason,
                                  **_approval_kwargs(key, approval)))
