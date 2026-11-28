"""The stdio MCP server over the Lab command layer: one operator-chosen service (PDR-0061, ADR-0070).

Only ``lab mcp`` imports this module; it needs the ``mcp`` extra.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import dataclasses
import functools
from hashlib import sha256
from importlib import metadata
from pathlib import Path
import sys
from typing import Any, AsyncIterator
from urllib.parse import unquote

import anyio
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from mcp import types as mcp_types
from mcp.server import Server
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity, compact_json
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from evolution_lab.commands import Effect, REGISTRY, Request
from evolution_lab.commands.errors import CommandFailure, LocalFailure, ServiceFailure
from evolution_lab.mcp import approval, facts
from evolution_lab.mcp.tools import PUBLISHED, ToolSpec, annotations, error_result, output_schema, success_result
from evolution_lab.mcp.transport import INVALID_HINT, AsyncServiceTransport, log_reason, transport_failure
from evolution_lab.service.codecs import decode_json_object, decode_secret_map
from evolution_lab.service.errors import ServiceError

_BY_NAME = {spec.name: spec for spec in PUBLISHED}
_VALIDATORS = {spec.name: Draft202012Validator(spec.input_schema) for spec in PUBLISHED}
_GUIDE_TOOLS = frozenset({"guides_list", "guide_read"})
_GUIDES = "lab://guides/"
_MEDIA = {".md": "text/markdown", ".json": "application/json"}
#: The startup ``health`` read never holds up the server longer than this.
STARTUP_DEADLINE = 5.0
_STATE_HINT = "Read the current state before asking again. Use a new request ID."
_ACCEPT_STATE_CODES = frozenset({"digest_mismatch", "inputs_changed"})
_ToolOutcome = mcp_types.CallToolResult | mcp_types.InputRequiredResult
_START_BINDINGS = frozenset({"operation_resume", "delete_results", "release", "cleanup"})


@dataclasses.dataclass(frozen=True)
class SentRequest:
    fingerprint: bytes
    request: Request = dataclasses.field(repr=False)


@dataclasses.dataclass
class MutationSession:
    sent: dict[str, SentRequest] = dataclasses.field(default_factory=dict)
    preparing: dict[str, anyio.Event] = dataclasses.field(default_factory=dict)


def _installed_version() -> str:
    return metadata.version("evolution-lab")


def _notice(installed: str, service: str) -> str | None:
    if service == installed:
        return None
    return f"Guides come from evolution-lab {installed} installed with lab mcp; the service runs {service}."


def _instructions(socket_path: Path, version: str, notice: str | None, autonomy: str) -> str:
    approval = (
        "Autonomy is ask. Lab asks the human before actions requiring approval, through a form when "
        "supported and through the client's tool approval otherwise. When using the CLI for one of "
        "those actions, ask the human in the conversation and run the command after a yes."
        if autonomy == "ask" else
        "Autonomy is yolo. Lab does not ask for approval. The client's own settings apply."
    )
    return (
        f"Evolution Lab {version} over MCP. This server uses the Lab service at the socket {socket_path}, "
        f"chosen when lab mcp started. {approval} "
        "Ids come from the list tools (operations_list, projects_list, targets_list, "
        "scenarios_list, execution_policies_list, guides_list); each argument's description names the tool that "
        "returns it. Call health first to check that the service answers. Disclosed reads (private case data) and "
        "probe dialogs are CLI-only: run lab for them. Guides (guides_list, guide_read and lab://guides/ "
        f"resources) come from evolution-lab {version} installed with lab mcp; a guide result says so when the "
        "service runs another version." + (f" {notice}" if notice else "")
    )


def _version_request():
    """The ``health`` request without its schema check: the advisory version read takes ``lab_version`` from
    a service of any health schema; the public ``health`` tool keeps the strict check."""
    return dataclasses.replace(REGISTRY["health"].build(), schema=None)


def _lab_version(health: Any) -> str | None:
    version = health.get("lab_version") if isinstance(health, dict) else None
    return version if isinstance(version, str) and version else None


def _argument_problem(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """Which argument breaks the tool's input schema, named without echoing its value; ``None`` when none does."""
    error = best_match(_VALIDATORS[spec.name].iter_errors(arguments))
    if error is None:
        return None
    properties = spec.input_schema["properties"]
    if error.validator == "additionalProperties":
        unknown = sorted(name for name in arguments if name not in properties)
        return f"unknown argument {', '.join(unknown)}; the tool takes {', '.join(properties) or 'no arguments'}"
    if error.validator == "required":
        missing = [name for name in spec.input_schema.get("required", ()) if name not in arguments]
        return f"missing argument {', '.join(missing)}"
    name = error.path[0] if error.path else "?"
    if error.validator == "type":
        return f"argument {name} must be a {error.validator_value}"
    if error.validator == "minLength":
        return f"argument {name} must not be empty"
    return f"argument {name} is invalid"


def _read_secrets(filename: str | None) -> dict[str, str]:
    if filename is None:
        return {}
    path = Path(filename)
    if not path.is_absolute():
        raise LocalFailure("usage_error", hint="secrets_file must be an absolute path")
    try:
        return decode_secret_map(decode_json_object(path.read_bytes()))
    except (OSError, ValueError, ServiceError):
        raise LocalFailure("usage_error", hint="secrets_file must be a readable JSON object of secret names and strings") from None


def _uri_guide(uri: str) -> tuple[str, str]:
    """``lab://guides/<name>/<file>`` as (name, file); a scenario guide's name is ``scenarios/<id>``."""
    if not uri.startswith(_GUIDES):
        raise ValueError("unknown resource")
    segments = uri[len(_GUIDES):].split("/")
    take = 2 if segments[0] == "scenarios" else 1
    name = "/".join(unquote(segment) for segment in segments[:take])
    file = "/".join(unquote(segment) for segment in segments[take:]) or "SKILL.md"
    return name, file


def _guide_resources() -> list[mcp_types.Resource]:
    """Each guide's SKILL.md, then each catalogue scenario's guide, from the guides ``guide_read`` serves."""
    from evolution_lab import guides
    resources = [mcp_types.Resource(name=guide["name"], uri=f"{_GUIDES}{guide['name']}/SKILL.md",
                                    description=guide["description"], mime_type="text/markdown")
                 for guide in REGISTRY["guides_list"].run()]
    for scenario in guides.scenarios().descriptors:
        name = f"scenarios/{scenario['id']}"
        resources.append(mcp_types.Resource(name=name, uri=f"{_GUIDES}{name}/{scenario['skill']}",
                                            title=scenario.get("title"), mime_type="text/markdown",
                                            description=f"The guide of the {scenario['id']} scenario."))
    return resources


class LabServer:
    """Handlers and per-server state: the transport, the service's Lab version and the guide notice."""

    def __init__(self, socket_path: Path, *, timeout: float, transport: AsyncServiceTransport | None,
                 service_version: str | None, autonomy: str, approval_timeout: float) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.autonomy = autonomy
        self.approval_timeout = approval_timeout
        self.installed_version = _installed_version()
        self.service_version: str | None = None
        #: The notice guide results carry once the service's Lab version is known to differ.
        self.version_notice: str | None = None
        self._injected = transport
        self._transport: AsyncServiceTransport | None = transport
        self._health_read = False
        self._health_lock = anyio.Lock()
        if service_version is not None:
            self._record({"lab_version": service_version})

    @asynccontextmanager
    async def lifespan(self, _server: Server) -> AsyncIterator[dict[str, Any]]:
        """One owned transport per connection; an injected transport is the caller's to close."""
        context = {"mutations": MutationSession()}
        if self._injected is not None:
            yield context
            return
        self._transport = AsyncServiceTransport(self.socket_path, timeout=self.timeout)
        try:
            yield context
        finally:
            await self._transport.aclose()

    # Tools.

    async def list_tools(self, ctx, params) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=[
            mcp_types.Tool(name=spec.name, description=spec.description, input_schema=spec.input_schema,
                           output_schema=output_schema(spec), annotations=annotations(spec),
                           _meta={"anthropic/requiresUserInteraction": True}
                           if self.autonomy == "ask" and spec.asks else None)
            for spec in PUBLISHED])

    async def call_tool(self, ctx, params: mcp_types.CallToolRequestParams) -> _ToolOutcome:
        """Bound reads, mutation preparation and dispatch, and the human's form wait separately."""
        spec = _BY_NAME.get(params.name)
        if spec is None:
            raise MCPError(code=mcp_types.INVALID_PARAMS, message=f"Unknown tool: {params.name}")
        arguments = dict(params.arguments or {})
        problem = _argument_problem(spec, arguments)
        if problem is not None:
            return error_result(LocalFailure("usage_error", hint=problem))
        if spec.effect is not Effect.READ:
            return await self._outcome(ctx, params, spec, arguments)
        deadline = anyio.current_time() + self.timeout
        try:
            with anyio.fail_after(self.timeout):
                result = await self._outcome(ctx, params, spec, arguments)
                if spec.name in _GUIDE_TOOLS:
                    notice = await self._guide_notice(deadline)
                    if notice is not None:
                        result.content.append(mcp_types.TextContent(type="text", text=notice))
        except TimeoutError:
            result = error_result(LocalFailure("request_timeout", hint="retry"))
        return result

    async def _outcome(self, ctx, params: mcp_types.CallToolRequestParams, spec: ToolSpec,
                       arguments: dict[str, Any]) -> _ToolOutcome:
        try:
            return await self._run(ctx, params, spec, arguments)
        except ServiceFailure as failure:
            code = failure.envelope["error"]["code"]
            if code == "state_changed" or (spec.name == "project_accept_commit" and code in _ACCEPT_STATE_CODES):
                failure.hint = _STATE_HINT
            return error_result(failure)
        except CommandFailure as failure:
            return error_result(failure)
        except TimeoutError:
            return error_result(LocalFailure("request_timeout", hint="retry"))
        except Exception as error:  # noqa: BLE001 - protocol errors are for malformed requests
            log_reason(f"internal_error in {spec.name}", error, trace=True)
            return error_result(LocalFailure("internal_error"))

    async def _build_request(self, ctx, params: mcp_types.CallToolRequestParams, spec: ToolSpec,
                             arguments: dict[str, Any]) -> Request | mcp_types.InputRequiredResult:
        modern_form = spec.asks and self.autonomy == "ask" and ctx.protocol_version >= "2026-07-28" and (
            params.request_state is not None or approval.via_form(ctx.session, self.autonomy)
        )
        shown = approval.shown_form(params.request_state, self.approval_timeout) if modern_form else None
        with anyio.fail_after(self.timeout):
            fields = dict(arguments)
            if spec.secrets:
                fields["secrets"] = await anyio.to_thread.run_sync(
                    _read_secrets, fields.pop("secrets_file", None), abandon_on_cancel=True)
            if "channel" in spec.input_schema["properties"]:
                fields["channel"] = "mcp"
            if spec.command == "project_accept":
                fields["dry_run"] = spec.name == "project_accept_preview"
                fields.setdefault("expected", None)
            observed = await facts.gather(self._transport, spec.name, fields) if spec.asks else None
        if observed is not None:
            if modern_form:
                decision = approval.form_round(observed, params, shown)
                if isinstance(decision, mcp_types.InputRequiredResult):
                    return decision
            else:
                decision = await approval.obtain(ctx.session, observed, autonomy=self.autonomy,
                                                 tool=spec.name, timeout=self.approval_timeout)
            fields.update(decision)
            if spec.name in _START_BINDINGS:
                fields["observed_start"] = observed.observed_start
        return REGISTRY[spec.command].build(**fields)

    async def _mutation_request(self, ctx, params: mcp_types.CallToolRequestParams, spec: ToolSpec,
                                arguments: dict[str, Any]) -> Request | mcp_types.InputRequiredResult:
        session: MutationSession = ctx.lifespan_context["mutations"]
        key = arguments["request_id"]
        fingerprint = sha256(compact_json([spec.name, arguments], sort_keys=True).encode()).digest()
        conflict = LocalFailure("request_conflict", "This request ID already identifies a different mutation.",
                                stage="approval")
        # Serialize preparation for one key. A cancelled or declined call leaves the key available.
        with anyio.fail_after(self.timeout + self.approval_timeout):
            while key in session.preparing:
                await session.preparing[key].wait()
        sent = session.sent.get(key)
        if sent is not None:
            if sent.fingerprint != fingerprint:
                raise conflict
            if params.request_state is None and params.input_responses is None:
                return sent.request
        ready = session.preparing[key] = anyio.Event()
        try:
            request = await self._build_request(ctx, params, spec, arguments)
            if isinstance(request, mcp_types.InputRequiredResult):
                return request
            if sent is not None:
                if request != sent.request:
                    raise conflict
                return sent.request
            # Preparation and all form rounds are complete. Hand the exact request to dispatch.
            session.sent[key] = SentRequest(fingerprint, request)
            return request
        finally:
            del session.preparing[key]
            ready.set()

    async def _run(self, ctx, params: mcp_types.CallToolRequestParams, spec: ToolSpec,
                   arguments: dict[str, Any]) -> _ToolOutcome:
        entry = REGISTRY[spec.command]
        try:
            if entry.build is not None:
                build = self._build_request if spec.effect is Effect.READ else self._mutation_request
                request = await build(ctx, params, spec, arguments)
                if isinstance(request, mcp_types.InputRequiredResult):
                    return request
            else:
                # Abandoned on the call's deadline or cancellation: a blocked file read cannot hold the call.
                value = await anyio.to_thread.run_sync(functools.partial(entry.run, **arguments),
                                                       abandon_on_cancel=True)
        except (UnicodeError, OSError) as error:
            log_reason(f"internal_error in {spec.name}", error)
            raise LocalFailure("internal_error") from None
        except (ValueError, LookupError, ServiceError) as error:
            message = getattr(error, "message", None) or str(error) or None
            raise LocalFailure("usage_error", message, hint=f"check {', '.join(sorted(arguments)) or 'the call'}; "
                                                            "ids come from the list tools") from None
        if entry.build is not None:
            try:
                with anyio.fail_after(self.timeout):
                    value = await self._transport.call(request)
            except TimeoutError:
                raise transport_failure(request, "request_timeout", "retry") from None
            if spec.name == "health":
                self._record(value)
        if spec.result == "text" and isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeError as error:
                log_reason("invalid_service_response (non-UTF-8 text)", error)
                raise LocalFailure("invalid_service_response", hint=INVALID_HINT) from None
        try:
            return success_result(spec, value)
        except TypeError as error:
            log_reason("invalid_service_response (result shape)", error)
            raise LocalFailure("invalid_service_response", hint=INVALID_HINT) from None

    # The version notice.

    def _record(self, health: Any) -> None:
        version = _lab_version(health)
        if version is None:
            return
        self._health_read = True
        self.service_version = version
        self.version_notice = _notice(self.installed_version, version)

    async def _guide_notice(self, deadline: float) -> str | None:
        """Read ``health`` once per server for the service's Lab version; a failed or cut-off read adds no notice.

        Guides work without the service, so this advisory read never costs the guide its result: it (and the
        wait for a concurrent read) is cut off after ``min(STARTUP_DEADLINE, timeout / 2)`` and after half the
        time left before the call's ``deadline``, whichever comes first. A cut-off read is not repeated.
        """
        now = anyio.current_time()
        with anyio.CancelScope(deadline=min(now + min(STARTUP_DEADLINE, self.timeout / 2), now + (deadline - now) / 2)):
            async with self._health_lock:
                if not self._health_read:
                    self._health_read = True
                    try:
                        self._record(await self._transport.call(_version_request()))
                    except Exception:  # noqa: BLE001 - the notice is best effort; the guide result stands
                        pass
        return self.version_notice

    # Guide resources.

    async def list_resources(self, ctx, params) -> mcp_types.ListResourcesResult:
        return mcp_types.ListResourcesResult(resources=await anyio.to_thread.run_sync(_guide_resources))

    async def list_resource_templates(self, ctx, params) -> mcp_types.ListResourceTemplatesResult:
        return mcp_types.ListResourceTemplatesResult(resource_templates=[mcp_types.ResourceTemplate(
            name="lab-guide-file", uri_template=_GUIDES + "{name}/{file}", mime_type="text/markdown",
            description=("A file of a Lab guide installed with lab mcp: name from guides_list (or scenarios/<id>), "
                         "file a relative path inside the guide, SKILL.md for the guide itself."))])

    async def read_resource(self, ctx, params: mcp_types.ReadResourceRequestParams) -> mcp_types.ReadResourceResult:
        uri = str(params.uri)
        try:
            name, file = _uri_guide(uri)
            value = await anyio.to_thread.run_sync(functools.partial(REGISTRY["guide_read"].run, name, file))
        except (ValueError, LookupError, OSError):
            raise MCPError(code=mcp_types.INVALID_PARAMS, message=f"Unknown resource: {uri}") from None
        media = _MEDIA.get(Path(file).suffix.lower(), "text/plain")
        return mcp_types.ReadResourceResult(contents=[
            mcp_types.TextResourceContents(uri=uri, mime_type=media, text=value["text"])])


async def read_service_version(socket_path: Path, *, timeout: float) -> str | None:
    """The service's ``lab_version`` from one ``health`` read bounded by ``min(timeout, STARTUP_DEADLINE)``.

    ``None`` when the read fails for any reason (no socket, a stall, an invalid answer): the server then
    starts without a notice.
    """
    transport = AsyncServiceTransport(Path(socket_path), timeout=min(timeout, STARTUP_DEADLINE))
    try:
        health = await transport.call(_version_request())
    except Exception:  # noqa: BLE001 - the server starts whatever the service does
        return None
    finally:
        await transport.aclose()
    return _lab_version(health)


def build_server(socket_path: Path, *, timeout: float, transport: AsyncServiceTransport | None = None,
                 service_version: str | None = None, autonomy: str = "ask",
                 approval_timeout: float = 600.0) -> Server:
    """The MCP server reading the service at ``socket_path``; every tool call is bounded by ``timeout``.

    ``service_version`` is the service's Lab version read at startup (``read_service_version``); when it
    differs from the installed one, the instructions and every guide result carry the notice, and guide
    calls do not read ``health`` again.
    """
    lab = LabServer(socket_path, timeout=timeout, transport=transport, service_version=service_version,
                    autonomy=autonomy, approval_timeout=approval_timeout)
    server = Server(
        "evolution-lab", version=lab.installed_version,
        instructions=_instructions(lab.socket_path, lab.installed_version, lab.version_notice, autonomy),
        lifespan=lab.lifespan, on_list_tools=lab.list_tools, on_call_tool=lab.call_tool,
        on_list_resources=lab.list_resources, on_list_resource_templates=lab.list_resource_templates,
        on_read_resource=lab.read_resource,
    )
    # The form deadline is enforced inside the handler, so an authentic late reply
    # can return approval_timeout. The SDK key only exists for this stdio process.
    security = RequestStateSecurity.ephemeral(ttl=sys.float_info.max)
    server.middleware.append(RequestStateBoundary(security, default_audience=server.name))
    server.lab = lab
    return server


def serve_stdio(socket_path: Path, *, timeout: float, autonomy: str = "ask", approval_timeout: float = 600.0) -> int:
    """Read the service's version once, then serve MCP on stdin/stdout until the client closes them; 0 on EOF."""

    async def main() -> None:
        version = await read_service_version(Path(socket_path), timeout=timeout)
        server = build_server(Path(socket_path), timeout=timeout, service_version=version, autonomy=autonomy,
                              approval_timeout=approval_timeout)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
    return 0
