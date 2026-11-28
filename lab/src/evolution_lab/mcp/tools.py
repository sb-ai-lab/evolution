"""The published MCP tools: names, descriptions, input and output schemas, results (PDR-0061, ADR-0070).

Only ``evolution_lab.mcp.server`` imports this module; it is the one place that builds ``mcp.types`` values.
"""
from __future__ import annotations

from dataclasses import dataclass
import inspect
import io
from typing import Any, Literal

from mcp import types as mcp_types

from evolution_lab.cli.output import write_json
from evolution_lab.commands import Effect, REGISTRY
from evolution_lab.commands.errors import CommandFailure
from evolution_lab.service.codecs import BUNDLE_REQUEST_SCHEMAS, PREPARE_REQUEST_SCHEMAS

Result = Literal["object", "value", "markdown", "text"]


@dataclass(frozen=True)
class ToolSpec:
    """One published tool: the command it runs and the shape of its success value."""

    name: str
    command: str
    description: str
    input_schema: dict
    result: Result
    effect: Effect
    asks: bool
    secrets: bool


ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "stage": {"type": "string"}, "message": {"type": "string"}},
            "required": ["code", "stage", "message"],
        },
        "hint": {"type": "string"},
    },
    "required": ["error"],
    "additionalProperties": False,
}

def annotations(spec: ToolSpec) -> mcp_types.ToolAnnotations:
    if spec.effect is Effect.READ:
        return mcp_types.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
    return mcp_types.ToolAnnotations(read_only_hint=False, destructive_hint=spec.asks,
                                     idempotent_hint=False, open_world_hint=False)

#: An object value whose command names no response schema: any object with a member, never an error envelope
#: (no such command's value has a top-level ``error``).
_UNPINNED_OBJECT = {"type": "object", "minProperties": 1, "not": {"required": ["error"]}}

_SUCCESS: dict[str, dict] = {
    "value": {"type": "object", "properties": {"value": {}}, "required": ["value"], "additionalProperties": False},
    "markdown": {"type": "object", "properties": {"markdown": {"type": "string"}}, "required": ["markdown"],
                 "additionalProperties": False},
    "text": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
             "additionalProperties": False},
}

#: Argument descriptions by name; a ``(tool, argument)`` key overrides the shared one.
_ARGUMENTS: dict[str | tuple[str, str], str] = {
    "operation": "Operation id, from operations_list or project_operations.",
    "measurement": "Measurement id, from operation_measurements.",
    "case": "Case id, from the rows of operation_measurement.",
    "sample": "Sample id within the case, from the rows of operation_measurement.",
    "attempt": "Attempt id (r<NN>-a<MM>), from the rounds of operation_report.",
    "reflection": "Reflection id (r<NN>-x<MM>), from the rounds of operation_report.",
    "tree_from": ("Tree ref to diff from: initial, champion:vN or attempt:<attempt id> from operation_report; "
                  "omitted, initial."),
    "tree_to": ("Tree ref to diff to: initial, champion:vN or attempt:<attempt id> from operation_report; "
                "omitted, the final champion."),
    "project": "Project id, from projects_list.",
    "implementation": ("Implementation id of the project, from project_show; omitted, the project's only active "
                       "implementation (refused when it has several, or none: implementation_not_found)."),
    ("project_tree", "implementation"): "Implementation id of the project, from project_show.",
    ("project_operations", "implementation"): ("Only operations of this implementation, from project_show; "
                                               "omitted, every implementation."),
    ("project_operations_summary", "implementation"): ("Only operations of this implementation, from "
                                                       "project_show; omitted, every implementation."),
    "scenario": "Scenario id, from scenarios_list.",
    ("project_status", "scenario"): "Only this scenario, from scenarios_list; omitted, every scenario.",
    "path": "Path inside the implementation's tree, relative; `.` is its root.",
    "text": "true to return the text of the file at path.",
    "archived": "true to include archived projects.",
    "revoked": "true to include revoked confirmations.",
    "digest": "Bundle digest, from operation_inputs or targets_list.",
    ("prepared_show", "digest"): "Prepared execution digest, from operation_inputs.",
    ("execution_policy_show", "digest"): "Execution policy digest, from execution_policies_list.",
    "bundle": "Only targets of this Bundle digest, from operation_inputs; omitted, targets of every Bundle.",
    "ready": "true to list only ready targets.",
    "target": "Target id, from targets_list.",
    "owner_generation": "Owner generation of the target, from targets_list.",
    ("task_status", "owner_generation"): "Owner generation from the task reference that started the task.",
    "task": "Task id, from the task reference (lab-task-ref/1) a Lab command returned.",
    "schema": "Schema as NAME/VERSION, from scenario_show.",
    "name": "Guide name or directory from guides_list, or scenarios/<scenario id> for a scenario's guide.",
    "file": "A file inside the guide's directory, as a relative path; SKILL.md when omitted.",
    "operation_id": "Operation id from operations_list.",
    "project_id": "Project id from projects_list.",
    "confirmation_id": "Confirmation id from project_confirmations.",
    "acceptance_id": "Acceptance id from project_accept_commit or project_facts.",
    "policy_digest": "Policy digest from execution_policies_list.",
    "request_id": "Caller-chosen mutation key. Repeat it with the original arguments on retry.",
    "bundle_digest": "Bundle digest from bundle_create, project_bundle or bundle_show.",
    "prepared_digest": "Prepared execution digest from prepare, project_prepare or operation_inputs.",
    "connection_id": "Connection id from connect.",
    "target_id": "Target id from targets_list.",
    "target_generation": "Target's Owner generation from targets_list. Defaults to owner_generation.",
    "task_id": "Task id from the task reference a Lab tool returned.",
    "target_ref": "Exact target reference from the service, with id and owner_generation.",
    "ref": "The kind and id of the resource to release or clean up.",
    "secrets_file": "Absolute path to a JSON file of secret values. Omit when no secrets are needed.",
    "body": "The service request document. Use absolute paths and keep secret values in a secrets file.",
    ("project_prepare", "body"): "The prepare document without schema or implementation.",
    "runtime_policy": "Optional target stop and cleanup time limits in seconds.",
    "execution_policy": "Confirmed execution policy digest from execution_policies_list.",
    "linked_from": "Operation id to use as a campaign predecessor.",
    "baseline": "Linked campaign baseline choice.",
    "seed": "Linked campaign seed choice.",
    "baseline_from": "Import a baseline from this evaluation operation id.",
    "accept": "Core refusal findings to accept, with code, digest and effect from operation_refusal.",
    "reason": "Human reason for the action.",
    "wait_seconds": "Seconds to wait for cleanup, or null for the service default.",
    "policy": "Execution policy document, with secret names for provider keys.",
    "expected_digest": "Exact policy digest to confirm.",
    "author": "Human author. When a form is shown, its author field supplies the recorded name.",
    "channel": "The channel for this action is mcp.",
    "note": "Optional author note.",
    "claim": "Claim being confirmed.",
    "attests": "The digests and facts this confirmation attests to.",
    "linked": "Related fact references.",
    "only": "Accept only these fact ids. Null selects all eligible facts.",
    "across_pairs": "Allow eligible facts from a different version pair.",
    "expected": "Exact id, from and to values for each entry from project_accept_preview.",
    "rev": "Source revision to inspect. Omit to check the upstream branch.",
    "upstream": "Exact upstream commit SHA returned by project_sync_check.",
}

#: The string arguments that are not a URL path segment, by tool: a query parameter or an offline file name.
#: Only an id that becomes a path segment must be non-empty; for these the shared builder (and the service behind
#: it) decides what an empty string means, exactly as for the CLI. ``test_mcp_tools`` checks every service-backed
#: entry, and its absence for every other string argument, against the request each builder produces.
QUERY_ARGUMENTS: dict[str, frozenset[str]] = {
    "targets_list": frozenset({"bundle"}),
    "target_status": frozenset({"owner_generation"}),
    "task_status": frozenset({"owner_generation"}),
    "project_status": frozenset({"implementation", "scenario"}),
    "project_facts": frozenset({"implementation"}),
    "project_tree": frozenset({"implementation", "path"}),
    "project_operations": frozenset({"implementation"}),
    "project_operations_summary": frozenset({"implementation"}),
    "operation_diff": frozenset({"tree_from", "tree_to"}),
    # Offline: ``file`` is joined under the guide's directory; ``guide_read`` refuses "" itself (not a file).
    "guide_read": frozenset({"file"}),
}


def _closed_object(properties: dict, required: tuple[str, ...] | None = None) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "required": list(properties if required is None else required)}


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


_STRING = {"type": "string"}
_OBJECT = {"type": "object"}
_NUMBER_OR_NULL = {"type": ["number", "null"]}
_INTEGER_OR_NULL = {"type": ["integer", "null"]}
_STRING_OR_NULL = {"type": ["string", "null"]}
_RUNTIME_POLICY = _closed_object({"stop_grace_seconds": {"type": "number"},
                                 "cleanup_wait_seconds": {"type": "number"}})
_PREPARE_FIELDS = {
    "kind": {"enum": ["campaign", "evaluation"]}, "bundle_digest": _STRING,
    "stop_grace_seconds": _NUMBER_OR_NULL, "cleanup_wait_seconds": _NUMBER_OR_NULL,
    "selection": {}, "repeat_seeds": {**_array(_INTEGER_OR_NULL), "type": ["array", "null"]},
    "evaluation_set_id": _STRING_OR_NULL, "max_turns": _INTEGER_OR_NULL, "timeout_seconds": _NUMBER_OR_NULL,
    "metric_plan": {"type": ["object", "null"]}, "runtime_policy": {**_RUNTIME_POLICY, "type": ["object", "null"]},
    "max_failed_turns": _INTEGER_OR_NULL, "call_retries": {"type": ["object", "null"]},
    "split": {"enum": ["train", "test", None]}, "sample_retries": _INTEGER_OR_NULL,
    "min_coverage": _NUMBER_OR_NULL, "concurrency": _INTEGER_OR_NULL, "scorer_concurrency": _INTEGER_OR_NULL,
    "max_consecutive_unmeasured": _INTEGER_OR_NULL,
}
_SCHEMAS = {
    "accept": _array(_closed_object({"code": _STRING, "digest": _STRING, "effect": _STRING_OR_NULL})),
    "target_ref": _closed_object({"schema": {"const": "lab-target-ref/1"}, "id": _STRING,
                                  "owner_generation": _STRING}),
    "ref": _closed_object({"kind": {"enum": ["operation", "target", "task"]}, "id": _STRING}),
    "expected": {**_array(_closed_object({"id": _STRING, "from": _OBJECT, "to": _OBJECT})), "minItems": 1},
    "runtime_policy": _RUNTIME_POLICY,
    "policy": _closed_object({
        "schema": {"enum": ["lab-execution-policy/1", "lab-execution-policy/2"]},
        "gateway": _closed_object({"host": _STRING, "port": {"type": "integer"}, "ledger_path": _STRING,
                                   "ledger_identity": _STRING, "currency": {"enum": ["RUB", "USD"]},
                                   "limit_units": {"type": "integer"}}),
        "routes": {"type": "object", "additionalProperties": _OBJECT},
        "auto_resume": _OBJECT, "target_token_name": _STRING_OR_NULL,
        "scorer_token_names": {"type": "object", "additionalProperties": _STRING},
        "role_groups": {"type": "object", "additionalProperties": _array(_STRING)},
    }, ("schema",)),
    ("bundle_create", "body"): _closed_object({
        "schema": {"enum": list(BUNDLE_REQUEST_SCHEMAS)}, "source": _OBJECT, "environments": _array(_OBJECT),
        "env": {"type": "object", "additionalProperties": _STRING}, "secret_names": _array(_STRING),
        "roots": _array(_OBJECT), "path_bindings": _array(_OBJECT),
    }),
    ("prepare", "body"): _closed_object({
        "schema": {"enum": sorted(PREPARE_REQUEST_SCHEMAS)}, **_PREPARE_FIELDS,
    }, ("schema", "kind", "bundle_digest")),
    ("project_prepare", "body"): _closed_object(_PREPARE_FIELDS, ("kind", "bundle_digest")),
    "channel": {"type": "string", "const": "mcp", "default": "mcp"},
}


def _parameter_schema(tool: str, name: str, annotation) -> dict:
    declared = _SCHEMAS.get((tool, name)) or _SCHEMAS.get(name)
    if declared is not None:
        return dict(declared)
    if annotation in ("bool", bool):
        return {"type": "boolean"}
    if annotation in ("str", "str | None", str):
        prop = {"type": "string"}
        if (name not in QUERY_ARGUMENTS.get(tool, ()) and name != "note"
                and (tool, name) != ("operation_resume", "reason")):
            prop["minLength"] = 1
        return prop
    if annotation in ("int", int):
        return {"type": "integer"}
    if annotation == "int | float | None":
        return {"type": "number"}
    if annotation in ("dict", "dict | None"):
        return {"type": "object"}
    if annotation in ("list[dict]", "list[dict] | None"):
        return _array(_OBJECT)
    if annotation == "list[str] | None":
        return _array(_STRING)
    raise TypeError(f"tool {tool!r} argument {name!r} has no MCP type")


def _input_schema(tool: str, command: str) -> dict:
    """Expose builder parameters with MCP's file inputs and action-specific fields."""
    entry = REGISTRY[command]
    parameters = inspect.signature(entry.build or entry.run).parameters
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, parameter in parameters.items():
        if name in {"approval", "observed_start", "dry_run"} or (
            name == "expected" and tool == "project_accept_preview"
        ):
            continue
        if name == "secrets":
            properties["secrets_file"] = {"type": "string", "minLength": 1,
                                          "description": _ARGUMENTS["secrets_file"]}
            continue
        prop = _parameter_schema(tool, name, parameter.annotation)
        default = parameter.default
        if entry.effect is not Effect.READ and "None" in str(parameter.annotation) and name not in {
            "request_id", "owner_generation", "expected",
        }:
            prop["type"] = [prop["type"], "null"]
        prop["description"] = _ARGUMENTS.get((tool, name)) or _ARGUMENTS[name]
        if default is inspect.Parameter.empty and name != "channel":
            required.append(name)
        elif default is not None and default is not inspect.Parameter.empty:
            prop["default"] = default
        properties[name] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _tool(name: str, description: str, *, command: str | None = None, result: Result = "object") -> ToolSpec:
    command = command or name
    entry = REGISTRY[command]
    schema = _input_schema(name, command)
    return ToolSpec(name, command, description, schema, result, entry.effect,
                    entry.asks and name != "project_accept_preview", "secrets_file" in schema["properties"])


PUBLISHED: tuple[ToolSpec, ...] = (
    _tool("health", "The Lab service's health and its service environment. Call it first to check that the "
                    "service answers."),
    _tool("operations_list", "Every operation (evaluation or campaign) of the service with its state. Use it to "
                             "find an operation id."),
    _tool("operations_summary", "A short summary of every operation: id, kind, state and headline result."),
    _tool("operation_status", "One operation's current status: control and results state, progress, and what "
                              "to do next."),
    _tool("operation_failure", "Why an operation failed: the saved failure record."),
    _tool("operation_refusal", "Why Core refused to start or continue an operation: the saved refusal and its "
                               "findings."),
    _tool("operation_inputs", "Each start of an operation with its prepared execution digest and Bundle digest."),
    _tool("operation_report", "An operation's owner report as JSON: rounds, attempts, reflections, measurements "
                              "and the champion. Attempt and reflection ids come from here."),
    _tool("operation_report_markdown", "An operation's owner report as Markdown, as "
                                       "`lab report OPERATION --markdown` prints it.",
          result="markdown"),
    _tool("operation_campaign", "A campaign operation's campaign view: rounds, attempts, decisions and the "
                                "champion."),
    _tool("operation_routing", "How an operation routes model calls: providers, routes and cost caps."),
    _tool("operation_suspensions", "The suspensions of an operation and their saved evidence."),
    _tool("operation_measurements", "The measurements of an operation. Use it to find a measurement id."),
    _tool("operation_measurement", "One measurement of an operation: its plan, coverage and rows. Case and "
                                   "sample ids come from here."),
    _tool("operation_sample", "One sample of a case in a measurement, without private case data."),
    _tool("operation_attempt", "One attempt of a campaign, without private case data."),
    _tool("operation_reflection", "One reflection of a campaign round, without private case data."),
    _tool("operation_diff", "The diff between two trees of a campaign, for example the champion and an attempt."),
    _tool("projects_list", "Every registered project. Use it to find a project id."),
    _tool("project_show", "One project's record: working copy, materials root and implementations. "
                          "Implementation ids come from here."),
    _tool("project_status", "A project's readiness: which steps are done and what to do next, optionally for "
                            "one implementation and one scenario."),
    _tool("project_facts", "The facts the service computes for a project, optionally for one implementation."),
    _tool("project_confirmations", "The confirmations recorded for a project."),
    _tool("project_tree", "A directory listing or a file's text from a project implementation's tree."),
    _tool("project_operations", "The operations of a project, optionally for one implementation."),
    _tool("project_operations_summary", "A short summary of a project's operations, optionally for one "
                                        "implementation."),
    _tool("bundle_show", "A Bundle's summary: its frozen inputs and identity."),
    _tool("bundle_measurements", "The measurements taken on a Bundle across operations."),
    _tool("prepared_show", "A prepared execution's summary: the plan an operation starts from."),
    _tool("execution_policies_list", "The confirmed execution policies. Use it to find a policy digest."),
    _tool("execution_policy_show", "One confirmed execution policy record."),
    _tool("targets_list", "The service's targets (running agents), optionally of one Bundle or only ready ones. "
                          "Target ids and owner generations come from here."),
    _tool("target_status", "One target's status for an owner generation."),
    _tool("task_status", "One service task's status for an owner generation."),
    _tool("scenarios_list", "The scenarios in the service's catalogue. Use it to find a scenario id.",
          command="service_scenarios_list"),
    _tool("scenario_show", "One scenario of the service's catalogue: its steps and schemas.",
          command="service_scenario_show"),
    _tool("scenario_schema", "One schema file of a scenario in the service's catalogue, as text.",
          command="service_scenario_schema", result="text"),
    _tool("requirements", "The service's requirements catalogue for checking a package.",
          command="service_requirements"),
    _tool("guides_list", "The Lab guides (skills) installed with this MCP server: name, description and path. "
                         "Use it to find a guide name.", result="value"),
    _tool("guide_read", "One file of a Lab guide (skill) installed with this MCP server; SKILL.md by default."),
    _tool("approvals_list", "Recorded approvals for an operation or execution policy. Supply exactly one filter."),
    _tool("project_approvals", "Recorded approvals for a project."),
    _tool("bundle_create", "Freeze agent and execution inputs as a Bundle."),
    _tool("prepare", "Prepare a campaign or evaluation from a Bundle."),
    _tool("connect", "Bind a Bundle to an Owner connection."),
    _tool("target_start", "Start the target for an Owner connection."),
    _tool("target_stop", "Stop a target."),
    _tool("check", "Run the connection's offline checks."),
    _tool("conformance", "Run offline target contract checks on a connection."),
    _tool("task_stop", "Request stop for an Owner task."),
    _tool("operation_stop", "Request stop for an evaluation or campaign."),
    _tool("project_bundle", "Freeze inputs for a registered project implementation."),
    _tool("project_prepare", "Prepare a registered project's campaign or evaluation."),
    _tool("project_target", "Start a registered project's target."),
    _tool("project_sync_check", "Show incoming commits without merging. Use the returned upstream SHA for project_sync."),
    _tool("project_sync", "Merge exactly the upstream SHA from project_sync_check into the project working copy."),
    _tool("operation_start", "Start a prepared campaign or evaluation on the named target."),
    _tool("project_start", "Start a registered project's prepared campaign or evaluation."),
    _tool("operation_resume", "Resume an operation, optionally accepting explicitly named refusal findings."),
    _tool("policy_confirm", "Confirm an execution policy with the given digest."),
    _tool("delete_results", "Delete the stored results of an operation."),
    _tool("release", "Release an Owner resource with a reason."),
    _tool("cleanup", "Clean up an Owner resource."),
    _tool("project_confirm", "Record an attestation for a registered project."),
    _tool("project_accept_preview", "Preview which stale project facts are eligible for acceptance.",
          command="project_accept"),
    _tool("project_accept_commit", "Accept exactly the stale facts shown in project_accept_preview.",
          command="project_accept"),
    _tool("project_revoke_confirmation", "Revoke a recorded project confirmation."),
    _tool("project_revoke_acceptance", "Revoke a recorded project acceptance."),
)


def _success_schema(spec: ToolSpec) -> dict:
    """An object value is pinned by its response schema id, which every validated Lab value carries as its
    top-level ``schema``; the other shapes are the wrappers above."""
    if spec.result != "object":
        return _SUCCESS[spec.result]
    schema_id = REGISTRY[spec.command].schema
    if schema_id is None:
        return _UNPINNED_OBJECT
    return {"type": "object", "properties": {"schema": {"const": schema_id}}, "required": ["schema"],
            "not": {"required": ["error"]}}


def output_schema(spec: ToolSpec) -> dict:
    """The tool's success shape or the error envelope, nothing else; the MCP schema needs the root ``type``."""
    return {"type": "object", "anyOf": [_success_schema(spec), ERROR_SCHEMA]}


def _cli_json(value: Any) -> str:
    buffer = io.StringIO()
    write_json(buffer, value)
    return buffer.getvalue()


def _result(structured: dict, text: str, *, is_error: bool) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text=text)],
                                    structured_content=structured, is_error=is_error)


def success_result(spec: ToolSpec, value: Any) -> mcp_types.CallToolResult:
    """The command's value as structured content, and as text exactly as ``lab ... --json`` prints it."""
    if spec.result == "object":
        if not isinstance(value, dict):
            raise TypeError(f"tool {spec.name!r} needs a JSON object value")
        return _result(value, _cli_json(value), is_error=False)
    if spec.result == "value":
        return _result({"value": value}, _cli_json(value), is_error=False)
    if not isinstance(value, str):
        raise TypeError(f"tool {spec.name!r} needs a text value")
    return _result({spec.result: value}, value, is_error=False)


def error_result(failure: CommandFailure) -> mcp_types.CallToolResult:
    """The failure's envelope, with its hint when there is one, as structured content and as JSON text."""
    structured = {**failure.envelope, **({"hint": failure.hint} if failure.hint else {})}
    return _result(structured, _cli_json(structured), is_error=True)
