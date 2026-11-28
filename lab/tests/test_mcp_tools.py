"""Published MCP tools: the pinned list, annotations, input and output schemas, results (PDR-0061, ADR-0070)."""
from __future__ import annotations

import inspect
import io
import json
import subprocess
import sys

import jsonschema
import pytest
from mcp import Client, types as mcp_types

from evolution_lab.cli.output import write_json
from evolution_lab.commands import REGISTRY, Effect
from evolution_lab.commands.errors import LocalFailure, ServiceFailure
from evolution_lab.mcp.server import build_server
from evolution_lab.mcp.tools import ERROR_SCHEMA, PUBLISHED, QUERY_ARGUMENTS, error_result, output_schema, success_result

PINNED = (
    ("health", "health"),
    ("operations_list", "operations_list"),
    ("operations_summary", "operations_summary"),
    ("operation_status", "operation_status"),
    ("operation_failure", "operation_failure"),
    ("operation_refusal", "operation_refusal"),
    ("operation_inputs", "operation_inputs"),
    ("operation_report", "operation_report"),
    ("operation_report_markdown", "operation_report_markdown"),
    ("operation_campaign", "operation_campaign"),
    ("operation_routing", "operation_routing"),
    ("operation_suspensions", "operation_suspensions"),
    ("operation_measurements", "operation_measurements"),
    ("operation_measurement", "operation_measurement"),
    ("operation_sample", "operation_sample"),
    ("operation_attempt", "operation_attempt"),
    ("operation_reflection", "operation_reflection"),
    ("operation_diff", "operation_diff"),
    ("projects_list", "projects_list"),
    ("project_show", "project_show"),
    ("project_status", "project_status"),
    ("project_facts", "project_facts"),
    ("project_confirmations", "project_confirmations"),
    ("project_tree", "project_tree"),
    ("project_operations", "project_operations"),
    ("project_operations_summary", "project_operations_summary"),
    ("bundle_show", "bundle_show"),
    ("bundle_measurements", "bundle_measurements"),
    ("prepared_show", "prepared_show"),
    ("execution_policies_list", "execution_policies_list"),
    ("execution_policy_show", "execution_policy_show"),
    ("targets_list", "targets_list"),
    ("target_status", "target_status"),
    ("task_status", "task_status"),
    ("scenarios_list", "service_scenarios_list"),
    ("scenario_show", "service_scenario_show"),
    ("scenario_schema", "service_scenario_schema"),
    ("requirements", "service_requirements"),
    ("guides_list", "guides_list"),
    ("guide_read", "guide_read"),
)

MUTATIONS = (
    "bundle_create", "prepare", "connect", "target_start", "target_stop", "check", "conformance",
    "task_stop", "operation_stop", "project_bundle", "project_prepare", "project_target",
    "project_sync_check", "project_sync", "operation_start", "project_start", "operation_resume",
    "policy_confirm", "delete_results", "release", "cleanup", "project_confirm", "project_accept_preview",
    "project_accept_commit", "project_revoke_confirmation", "project_revoke_acceptance",
)
ASKING = frozenset({
    "project_sync", "operation_start", "project_start", "operation_resume", "policy_confirm", "delete_results",
    "release", "cleanup", "project_confirm", "project_accept_commit", "project_revoke_confirmation",
    "project_revoke_acceptance",
})
APPROVAL_READS = ("approvals_list", "project_approvals")

SPECS = {spec.name: spec for spec in PUBLISHED}
SERVICE_ENVELOPE = {"error": {"code": "unknown_operation", "stage": "owner", "message": "The operation is unknown."}}
CANNED = {
    "object": {"schema": "lab-example/1", "id": "op-1", "items": [1, 2], "nested": {"b": 1, "a": None}},
    "value": [{"name": "budget", "description": "Route model calls.", "path": "budget/SKILL.md"}],
    "markdown": "# Report\n\nNo improvement.\n",
    "text": '{"type": "object"}\n',
}


def _canned(spec):
    """A canned success value for the tool; an object carries its command's response schema id when it has one."""
    value = CANNED[spec.result]
    schema_id = REGISTRY[spec.command].schema
    if spec.result == "object" and schema_id is not None:
        return {**value, "schema": schema_id}
    return value


def _function(spec):
    command = REGISTRY[spec.command]
    return command.build or command.run


def _cli_json(value) -> str:
    buffer = io.StringIO()
    write_json(buffer, value)
    return buffer.getvalue()


def _failures():
    return (ServiceFailure(SERVICE_ENVELOPE),
            LocalFailure("service_unavailable", hint="Start the service with lab serve (socket /run/lab.sock)."),
            LocalFailure("usage_error", "The operation id is invalid."))


def test_published_list_is_pinned():
    added_reads = tuple((name, name) for name in APPROVAL_READS)
    mutations = tuple((name, "project_accept" if name.startswith("project_accept_") else name)
                      for name in MUTATIONS)
    assert tuple((spec.name, spec.command) for spec in PUBLISHED) == PINNED + added_reads + mutations


def test_published_commands_exclude_disclosure_variants():
    for spec in PUBLISHED:
        assert not spec.command.endswith("_disclosed")
    offline_forms = {"requirements", "scenarios_list", "scenario_show", "scenario_schema"}
    assert offline_forms.isdisjoint(spec.command for spec in PUBLISHED)


def test_no_published_tool_accepts_disclosure():
    for spec in PUBLISHED:
        names = set(spec.input_schema.get("properties", {}))
        assert not any("disclose" in name for name in names), spec.name


@pytest.mark.parametrize("autonomy", ["ask", "yolo"])
async def test_annotations_and_approval_markers_on_every_tool(tmp_path, autonomy):
    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, autonomy=autonomy)) as client:
        tools = (await client.list_tools()).tools
    assert set(tool.name for tool in tools) == {name for name, _ in PINNED} | set(APPROVAL_READS) | set(MUTATIONS)
    for tool in tools:
        annotations = tool.annotations.model_dump(by_alias=True, exclude_none=True)
        if tool.name in MUTATIONS:
            assert annotations == {"readOnlyHint": False, "destructiveHint": tool.name in ASKING,
                                   "idempotentHint": False, "openWorldHint": False}
        else:
            assert annotations == {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
        assert tool.meta == ({"anthropic/requiresUserInteraction": True}
                             if autonomy == "ask" and tool.name in ASKING else None)


@pytest.mark.parametrize("name", MUTATIONS)
def test_mutation_tools_require_the_caller_key_and_hide_service_only_arguments(name):
    schema = SPECS[name].input_schema
    properties = schema["properties"]
    assert {"secrets", "approval", "observed_start", "dry_run"}.isdisjoint(properties)
    assert "request_id" in schema["required"] and properties["request_id"]["type"] == "string"
    assert "owner_generation" in schema["required"] and properties["owner_generation"]["type"] == "string"
    if name == "project_accept_preview":
        assert "expected" not in properties
    if name == "project_accept_commit":
        assert "expected" in schema["required"]
        validator = jsonschema.Draft202012Validator(properties["expected"])
        assert validator.is_valid([{"id": "baseline", "from": {"kind": "operation", "id": "o"}, "to": {}}])
        assert not validator.is_valid([{"id": "baseline", "from": {}}])


@pytest.mark.parametrize("kind", ["relative", "invalid_json", "invalid_value", "valid"])
async def test_target_start_uses_an_absolute_secret_file(tmp_path, kind):
    secret = "fixture-value-not-for-display"
    path = tmp_path / "credentials.json"
    path.write_text({"invalid_json": secret, "invalid_value": json.dumps([secret])}.get(
        kind, json.dumps({"KEY": secret})), encoding="utf-8")
    sent = []

    class Transport:
        async def call(self, request):
            sent.append(request)
            return {"schema": "lab-task-ref/1", "id": "task", "owner_generation": "generation"}

    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=Transport())) as client:
        result = await client.call_tool("target_start", {
            "connection_id": "connection", "request_id": "request", "owner_generation": "generation",
            "secrets_file": path.name if kind == "relative" else str(path),
        })
    assert secret not in result.model_dump_json()
    if kind == "valid":
        assert not result.is_error, result.structured_content
        request, = sent
        assert request.path == "/v1/connections/connection/target"
        assert request.mutation_key == ("request", "generation")
        assert request.payload["secrets"] == {"KEY": secret}
    else:
        assert result.structured_content["error"]["code"] == "usage_error"
        assert sent == []


def test_every_tool_has_a_description():
    for spec in PUBLISHED:
        assert spec.description.strip(), spec.name


def test_result_kinds():
    kinds = {spec.name: spec.result for spec in PUBLISHED}
    assert kinds.pop("guides_list") == "value"
    assert kinds.pop("operation_report_markdown") == "markdown"
    assert kinds.pop("scenario_schema") == "text"
    assert set(kinds.values()) == {"object"}


def test_schemas_are_valid_json_schemas():
    jsonschema.Draft202012Validator.check_schema(ERROR_SCHEMA)
    for spec in PUBLISHED:
        jsonschema.Draft202012Validator.check_schema(spec.input_schema)
        jsonschema.Draft202012Validator.check_schema(output_schema(spec))


@pytest.mark.parametrize("spec", PUBLISHED, ids=lambda spec: spec.name)
def test_every_result_matches_its_output_schema(spec):
    schema = output_schema(spec)
    success = success_result(spec, _canned(spec))
    assert success.is_error is False
    jsonschema.validate(success.structured_content, schema)
    for failure in _failures():
        result = error_result(failure)
        assert result.is_error is True
        jsonschema.validate(result.structured_content, schema)


def test_output_schema_admits_exactly_two_shapes():
    for spec in PUBLISHED:
        schema = output_schema(spec)
        assert schema["type"] == "object"
        assert set(schema) == {"type", "anyOf"}
        assert len(schema["anyOf"]) == 2 and schema["anyOf"][1] == ERROR_SCHEMA
    wrapped = output_schema(SPECS["guides_list"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"value": [], "extra": 1}, wrapped)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"error": {"code": "x", "stage": "client"}}, wrapped)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"text": 1}, output_schema(SPECS["scenario_schema"]))


def test_guides_list_is_wrapped_in_value():
    value = CANNED["value"]
    result = success_result(SPECS["guides_list"], value)
    assert result.structured_content == {"value": value}
    assert result.content[0].text == _cli_json(value)


def test_object_value_is_unchanged():
    value = CANNED["object"]
    result = success_result(SPECS["operation_status"], value)
    assert result.structured_content == value
    assert list(result.structured_content) == list(value)
    assert list(result.structured_content["nested"]) == ["b", "a"]


def test_markdown_and_text_results_carry_the_raw_text():
    markdown = success_result(SPECS["operation_report_markdown"], CANNED["markdown"])
    assert markdown.structured_content == {"markdown": CANNED["markdown"]}
    assert markdown.content[0].text == CANNED["markdown"]
    text = success_result(SPECS["scenario_schema"], CANNED["text"])
    assert text.structured_content == {"text": CANNED["text"]}
    assert text.content[0].text == CANNED["text"]


@pytest.mark.parametrize("name", ["operation_status", "guides_list", "guide_read"])
def test_text_content_equals_cli_json(name):
    spec = SPECS[name]
    value = CANNED[spec.result]
    result = success_result(spec, value)
    assert len(result.content) == 1
    assert isinstance(result.content[0], mcp_types.TextContent)
    assert result.content[0].text == _cli_json(value)


def test_error_result_carries_the_envelope_and_hint():
    service, unavailable, usage = _failures()
    passed = error_result(service)
    assert passed.structured_content == SERVICE_ENVELOPE
    assert json.loads(passed.content[0].text) == SERVICE_ENVELOPE
    hinted = error_result(unavailable)
    assert hinted.structured_content == {**unavailable.envelope, "hint": unavailable.hint}
    assert hinted.structured_content["error"]["stage"] == "client"
    assert len(hinted.content) == 1 and json.loads(hinted.content[0].text) == hinted.structured_content
    assert "hint" not in error_result(usage).structured_content


def test_input_schema_requires_command_arguments():
    for name, _ in PINNED:
        spec = SPECS[name]
        schema = spec.input_schema
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        parameters = inspect.signature(_function(spec)).parameters
        required = [name for name, p in parameters.items() if p.default is inspect.Parameter.empty]
        assert list(schema.get("properties", {})) == list(parameters), spec.name
        assert schema.get("required", []) == required, spec.name
        for name, prop in schema.get("properties", {}).items():
            annotation = parameters[name].annotation
            assert prop["type"] == ("boolean" if annotation in ("bool", bool) else "string"), (spec.name, name)
            assert prop.get("description", "").strip(), (spec.name, name)


#: Two valid, different values per argument name, so a builder accepts either and the request shows where it goes.
VALID_PAIRS = {
    "operation": ("op-a", "op-b"), "measurement": ("m-a", "m-b"), "case": ("c-a", "c-b"), "sample": ("s-a", "s-b"),
    "attempt": ("r01-a01", "r02-a02"), "reflection": ("r01-x01", "r02-x02"), "project": ("p-a", "p-b"),
    "digest": ("a" * 64, "b" * 64), "target": ("t-a", "t-b"), "owner_generation": ("g-a", "g-b"),
    "task": ("k-a", "k-b"), "scenario": ("sc-a", "sc-b"), "schema": ("n/1", "m/2"),
    "implementation": ("i-a", "i-b"), "path": ("a/b", "c/d"), "bundle": ("bu-a", "bu-b"),
    "tree_from": ("initial", "champion:v1"), "tree_to": ("initial", "champion:v1"),
}


def _request_parts(spec, arguments):
    request = _function(spec)(**arguments)
    path, _, query = request.path.partition("?")
    return path, query


def _where_it_goes(spec, name):
    """``path`` or ``query``: where the builder puts argument ``name``, found by changing only that argument."""
    base = {arg: VALID_PAIRS[arg][0] for arg in spec.input_schema["properties"]
            if spec.input_schema["properties"][arg]["type"] == "string"}
    other = {**base, name: VALID_PAIRS[name][1]}
    first, second = _request_parts(spec, base), _request_parts(spec, other)
    assert first != second, (spec.name, name)
    return "path" if first[0] != second[0] else "query"


SERVICE_SPECS = [SPECS[name] for name, command in PINNED if REGISTRY[command].needs_service]


def test_only_path_segment_ids_refuse_empty():
    """``minLength: 1`` exactly on the string arguments that become a path segment; query arguments keep CLI semantics."""
    for spec in SERVICE_SPECS:
        for name, prop in spec.input_schema.get("properties", {}).items():
            if prop["type"] != "string":
                continue
            location = _where_it_goes(spec, name)
            assert (location == "path") == (prop.get("minLength") == 1), (spec.name, name, location)
            assert (location == "query") == (name in QUERY_ARGUMENTS.get(spec.name, ())), (spec.name, name)
            assert "minLength" in prop or location == "query"


def test_offline_tools_refuse_empty_only_for_the_guide_name():
    assert SPECS["guide_read"].input_schema["properties"]["name"]["minLength"] == 1
    assert "minLength" not in SPECS["guide_read"].input_schema["properties"]["file"]
    assert QUERY_ARGUMENTS["guide_read"] == {"file"}
    # The runner itself refuses "": the directory is not a file (a usage error through the command layer).
    with pytest.raises(ValueError, match="unknown guide file"):
        REGISTRY["guide_read"].run(name="budget", file="")


def test_every_query_argument_entry_names_a_string_argument():
    for tool, names in QUERY_ARGUMENTS.items():
        properties = SPECS[tool].input_schema["properties"]
        assert names <= set(properties) and all(properties[name]["type"] == "string" for name in names), tool


@pytest.mark.parametrize("tool, arguments, same_as", [
    ("project_operations", {"project": "p", "implementation": ""}, {"project": "p"}),
    ("project_operations_summary", {"project": "p", "implementation": ""}, {"project": "p"}),
    ("project_status", {"project": "p", "implementation": "", "scenario": ""}, {"project": "p"}),
    ("project_facts", {"project": "p", "implementation": ""}, {"project": "p"}),
])
def test_empty_query_arguments_build_the_cli_request(tool, arguments, same_as):
    """An empty filter is accepted by the schema and built exactly as ``lab project ... ''`` builds it."""
    spec = SPECS[tool]
    jsonschema.Draft202012Validator(spec.input_schema).validate(arguments)
    assert _function(spec)(**arguments) == _function(spec)(**same_as)


def test_empty_tree_path_reaches_the_service():
    spec = SPECS["project_tree"]
    arguments = {"project": "p", "implementation": "i", "path": ""}
    jsonschema.Draft202012Validator(spec.input_schema).validate(arguments)
    assert _function(spec)(**arguments).path == "/v1/projects/p/tree?implementation=i&path="


@pytest.mark.parametrize("tool, required", [
    ("project_tree", ["project", "implementation", "path"]),
    ("target_status", ["target", "owner_generation"]),
    ("guide_read", ["name"]),
    ("operation_diff", ["operation"]),
    ("operation_sample", ["operation", "measurement", "case", "sample"]),
    ("health", []),
])
def test_required_arguments_are_pinned(tool, required):
    assert SPECS[tool].input_schema.get("required", []) == required


@pytest.mark.parametrize("tool, arguments", [
    ("project_show", {"project": ""}),
    ("project_status", {"project": ""}),
    ("scenario_show", {"scenario": ""}),
    ("scenario_schema", {"scenario": "", "schema": "s/1"}),
    ("operation_status", {"operation": ""}),
    ("project_tree", {"project": "", "implementation": "i", "path": "."}),
    ("operation_diff", {"operation": ""}),
    ("target_status", {"target": "", "owner_generation": "g"}),
    ("project_show", {"project": 123}),
    ("project_show", {"project": None}),
    ("scenario_show", {"scenario": 123}),
    ("operation_status", {"operation": None}),
    ("targets_list", {"ready": "yes"}),
    ("project_show", {}),
    ("project_show", {"project": "p", "disclose": True}),
])
def test_input_schemas_refuse_bad_arguments(tool, arguments):
    validator = jsonschema.Draft202012Validator(SPECS[tool].input_schema)
    assert not validator.is_valid(arguments)


def test_input_schemas_admit_good_arguments():
    for tool, arguments in [
        ("health", {}),
        ("project_status", {"project": "p", "scenario": "connect"}),
        ("targets_list", {"bundle": "sha256:" + "0" * 64, "ready": True}),
        ("guide_read", {"name": "budget"}),
        ("project_tree", {"project": "p", "implementation": "i", "path": ".", "text": True}),
        ("operation_diff", {"operation": "op", "tree_from": "champion:v1", "tree_to": "attempt:r01-a01"}),
    ]:
        jsonschema.Draft202012Validator(SPECS[tool].input_schema).validate(arguments)


def test_cli_and_commands_do_not_import_the_tools():
    program = ("import sys, evolution_lab.commands, evolution_lab.cli\n"
               "from evolution_lab.cli import parser, output\n"
               "assert 'evolution_lab.mcp.tools' not in sys.modules\n"
               "assert 'mcp.types' not in sys.modules\n")
    subprocess.run([sys.executable, "-c", program], check=True)


OBJECT_SPECS = [spec for spec in PUBLISHED if spec.result == "object"]
MALFORMED_ERRORS = (
    {},
    {"error": {"code": 1}},
    {"error": {"code": "x", "stage": "client"}},
    {"error": {"code": "x", "stage": "client", "message": None}},
    {"error": "service_unavailable"},
    {"error": {"code": "x", "stage": "client", "message": "m"}, "hint": 1},
    {"error": {"code": "x", "stage": "client", "message": "m"}, "extra": True},
)


@pytest.mark.parametrize("spec", OBJECT_SPECS, ids=lambda spec: spec.name)
def test_object_output_schemas_refuse_empty_and_malformed_errors(spec):
    validator = jsonschema.Draft202012Validator(output_schema(spec))
    for value in MALFORMED_ERRORS:
        assert not validator.is_valid(value), (spec.name, value)
    assert validator.is_valid(_canned(spec))
    for failure in _failures():
        assert validator.is_valid(error_result(failure).structured_content)


@pytest.mark.parametrize("spec", [spec for spec in OBJECT_SPECS if REGISTRY[spec.command].schema is not None],
                         ids=lambda spec: spec.name)
def test_object_output_schemas_are_pinned_by_the_schema_id(spec):
    """An object tool whose command names a response schema admits only values carrying that id."""
    schema_id = REGISTRY[spec.command].schema
    success = output_schema(spec)["anyOf"][0]
    assert success == {"type": "object", "properties": {"schema": {"const": schema_id}}, "required": ["schema"],
                       "not": {"required": ["error"]}}
    validator = jsonschema.Draft202012Validator(output_schema(spec))
    assert not validator.is_valid({**CANNED["object"], "schema": "lab-other/1"})
    assert not validator.is_valid({key: value for key, value in CANNED["object"].items() if key != "schema"})


def test_unpinned_object_tools_are_the_commands_without_a_schema():
    unpinned = sorted(spec.name for spec in OBJECT_SPECS if REGISTRY[spec.command].schema is None)
    assert unpinned == ["execution_policies_list", "guide_read", "requirements", "scenario_show", "scenarios_list"]
    for name in unpinned:
        assert output_schema(SPECS[name])["anyOf"][0] == {
            "type": "object", "minProperties": 1, "not": {"required": ["error"]}}


@pytest.mark.parametrize("spec", [spec for spec in OBJECT_SPECS if REGISTRY[spec.command].schema is not None],
                         ids=lambda spec: spec.name)
def test_typed_success_branch_refuses_a_malformed_envelope_with_the_expected_schema_id(spec):
    schema_id = REGISTRY[spec.command].schema
    validator = jsonschema.Draft202012Validator(output_schema(spec))
    for error in ({"code": 1}, "service_unavailable", {"code": "x", "stage": "client", "message": "m"}):
        assert not validator.is_valid({"schema": schema_id, "error": error}), (spec.name, error)
    assert validator.is_valid(_canned(spec))


def test_policy_input_admits_routing_options():
    gateway = {"host": "localhost", "port": 4444, "ledger_path": "/tmp/spend.json", "ledger_identity": "eval",
               "limit_units": 100, "currency": "RUB"}
    policy = {
        "schema": "lab-execution-policy/2", "gateway": gateway,
        "routes": {"target": {"api_key_secret": "PROVIDER_TOKEN"}},
        "target_token_name": "TARGET_TOKEN", "scorer_token_names": {"SCORER_TOKEN": "target"},
        "role_groups": {"chooser": ["target"]},
    }
    properties = SPECS["policy_confirm"].input_schema["properties"]
    jsonschema.Draft202012Validator(properties["policy"]).validate(policy)
    assert not jsonschema.Draft202012Validator(properties["channel"]).is_valid("cli")


async def test_accept_preview_builds_the_keyed_dry_run(tmp_path):
    sent = []

    class Transport:
        async def call(self, request):
            sent.append(request)
            return {"schema": "lab-project-acceptance/1", "id": None}

    async with Client(build_server(tmp_path / "none.sock", timeout=1.0, transport=Transport())) as client:
        result = await client.call_tool("project_accept_preview", {
            "request_id": "preview", "owner_generation": "generation", "project_id": "project",
            "implementation": None, "only": None, "across_pairs": False, "reason": "inspect", "author": "Operator",
        })
    assert not result.is_error
    request, = sent
    assert request.path == "/v1/projects/project/acceptances"
    assert request.mutation_key == ("preview", "generation")
    assert request.payload == {
        "schema": "lab-service-accept-request/2", "implementation": None, "only": None, "across_pairs": False,
        "reason": "inspect", "channel": "mcp", "author": "Operator", "dry_run": True, "expected": None,
    }
