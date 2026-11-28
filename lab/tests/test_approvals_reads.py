"""Read approvals through the routes, command layer and CLI."""
import asyncio
from copy import deepcopy
import json

import pytest

from evolution_lab.cli.responses import validate
from evolution_lab.commands import resources
from evolution_lab.owner import ResourceRef
from test_projects_approvals import CONFIRM, PROJECT, post
from test_projects_approvals import runtime, service as project_service, work  # noqa: F401
from test_service_approvals_admission import (
    ZERO_DIGEST, assert_approval, confirm_request, headers, start_request,
)
from test_service_approvals_admission import offline_target, service  # noqa: F401
from test_service_approvals_irreversible import _assert_completed, _send
from tests.test_service_cli import FakeClient, invoke


SCHEMA = "lab-approval-list/1"
APPROVAL = {
    "schema": "lab-approval/1", "id": "record-one", "request_id": "request-one",
    "tool": "operation_start", "via": "form", "autonomy": "ask",
    "resource": {"kind": "operation", "id": "op-one"}, "start": 1,
    "outcome": "admitted", "created_at": "2026-11-23T08:00:00.000001Z",
}
POLICY_APPROVAL = {
    **APPROVAL, "id": "record-two", "request_id": "request-two", "tool": "policy_confirm",
    "via": "none", "autonomy": "yolo", "resource": {"kind": "execution_policy", "id": ZERO_DIGEST},
    "start": None, "outcome": "applied", "created_at": "2026-11-23T08:00:00.000002Z",
}


async def read_approvals(client, path, **kwargs):
    response = await client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    value = response.json()
    assert validate(value, SCHEMA) == value
    return value["approvals"]


async def test_owner_reads_operation_and_policy_approvals_from_admission(service, offline_target):
    owner, client = service
    started = await client.post("/v1/operations", headers=headers(owner, "start"), json=start_request(offline_target))
    assert started.status_code == 202, started.text
    operation_id = started.json()["id"]
    await owner._operations[operation_id].done
    await asyncio.gather(*tuple(owner._owned))

    operation = ResourceRef("operation", operation_id)
    cleanup = await _send(client, "cleanup", operation, key=headers(owner, "cleanup"))
    await _assert_completed(owner, cleanup, "cleanup", operation)

    policy = confirm_request()
    digest = policy["expected_digest"]
    confirmed = await client.post("/v1/execution-policies", headers=headers(owner, "confirm"), json=policy)
    assert confirmed.status_code == 200, confirmed.text

    started_row, cleanup_row = await read_approvals(client, "/v1/approvals", params={"operation_id": operation_id})
    assert_approval(started_row, request_id="start", tool="operation_start", resource_kind="operation",
                    resource_id=operation_id, start=1, outcome="admitted")
    assert_approval(cleanup_row, request_id="cleanup", tool="cleanup", resource_kind="task",
                    resource_id=cleanup.json()["id"], start=1, outcome="admitted")
    policy_row, = await read_approvals(client, "/v1/approvals", params={"policy_digest": digest})
    assert_approval(policy_row, request_id="confirm", tool="policy_confirm", resource_kind="execution_policy",
                    resource_id=digest, start=None, outcome="applied")
    assert await read_approvals(client, "/v1/approvals", params={"operation_id": "missing-operation"}) == []
    assert await read_approvals(client, "/v1/approvals", params={"policy_digest": ZERO_DIGEST}) == []


async def test_project_reads_approval_from_registry_admission(project_service, runtime, work, tmp_path):
    _, client = project_service
    issued = await post(project_service, "/confirmations", CONFIRM, "confirm")
    assert issued.status_code == 200, issued.text
    confirmation_id = issued.json()["id"]

    other = tmp_path / "other"
    other.mkdir()
    (other / "agent").mkdir()
    (other / "lab").mkdir()
    runtime.projects.create(id="other", name="Other", path=str(other), materials_root="lab",
                            implementations=[{"id": "agent", "place": "agent"}])

    record, = await read_approvals(client, PROJECT + "/approvals")
    assert_approval(record, request_id="confirm", tool="project_confirm", resource_kind="confirmation",
                    resource_id=confirmation_id, start=None, outcome="applied")
    assert await read_approvals(client, "/v1/projects/other/approvals") == []
    missing = await client.get("/v1/projects/missing/approvals")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "project_not_found"


@pytest.mark.parametrize("params", [{}, {"operation_id": "op", "policy_digest": ZERO_DIGEST}])
async def test_owner_read_requires_exactly_one_filter(service, params):
    _, client = service
    response = await client.get("/v1/approvals", params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("params", [
    {"operation_id": "op", "unknown": "value"},
    [("operation_id", "op"), ("operation_id", "other")],
    {"operation_id": "../op"}, {"policy_digest": "not-a-digest"},
])
async def test_owner_read_rejects_malformed_filters(service, params):
    _, client = service
    response = await client.get("/v1/approvals", params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("path", ["/v1/approvals?operation_id=op", PROJECT + "/approvals"])
@pytest.mark.parametrize("extra", [{"headers": {"Lab-Request-Id": "read"}}, {"content": b"{}"}])
async def test_approval_reads_refuse_mutation_headers_and_bodies(service, path, extra):
    _, client = service
    response = await client.request("GET", path, **extra)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("filters", [{}, {"operation_id": "op", "policy_digest": ZERO_DIGEST}])
def test_owner_read_builder_requires_exactly_one_filter(filters):
    with pytest.raises(ValueError):
        resources.approvals_list(**filters)


def test_approval_response_validators_keep_wire_values():
    approved = {"schema": SCHEMA, "approvals": [APPROVAL, POLICY_APPROVAL]}
    assert validate(deepcopy(APPROVAL), "lab-approval/1") == APPROVAL
    assert validate(deepcopy(approved), SCHEMA) == approved


@pytest.mark.parametrize("change", [
    {"extra": "field"}, {"id": ""}, {"request_id": None}, {"tool": "unregistered_tool"},
    {"via": "dialog"}, {"autonomy": "maybe"},
    {"resource": {"kind": "operation", "id": "op", "extra": "field"}},
    {"resource": {"kind": "unsupported", "id": "op"}}, {"start": True}, {"start": 0},
    {"outcome": "pending"}, {"created_at": "yesterday"}, {"schema": "lab-approval/2"},
])
def test_approval_response_validators_refuse_invalid_rows(change):
    malformed = {**APPROVAL, **change}
    with pytest.raises(ValueError):
        validate(malformed, "lab-approval/1")
    with pytest.raises(ValueError):
        validate({"schema": SCHEMA, "approvals": [malformed]}, SCHEMA)


@pytest.mark.parametrize("change", [
    {"outcome": {"invalid": "value"}},
    {"resource": {"kind": ["operation"], "id": "op"}},
])
def test_approval_validator_rejects_objects_and_arrays_as_enum_values(change):
    with pytest.raises(ValueError) as error:
        validate({**APPROVAL, **change}, "lab-approval/1")
    assert isinstance(error.value.__cause__, ValueError)


@pytest.mark.parametrize("payload", [
    {"schema": SCHEMA, "approvals": {}},
    {"schema": SCHEMA, "approvals": [], "extra": "field"},
    {"schema": SCHEMA, "approvals": [POLICY_APPROVAL, APPROVAL]},
])
def test_approval_list_validator_checks_envelope_and_order(payload):
    with pytest.raises(ValueError):
        validate(payload, SCHEMA)


@pytest.mark.parametrize(("args", "path"), [
    (["approvals", "--operation", "op-one"], "/v1/approvals?operation_id=op-one"),
    (["approvals", "--policy", ZERO_DIGEST], f"/v1/approvals?policy_digest={ZERO_DIGEST}"),
    (["project", "approvals", "acq"], PROJECT + "/approvals"),
])
def test_cli_approval_reads_print_text_and_json(args, path):
    value = {"schema": SCHEMA, "approvals": [APPROVAL, POLICY_APPROVAL]}
    socket = ["--socket", "/tmp/lab.sock"]
    client = FakeClient([deepcopy(value) for _ in range(3)])
    status, stdout, stderr = invoke([*socket, *args], client)
    assert (status, stderr) == (0, "")
    assert stdout == (
        "2026-11-23T08:00:00.000001Z operation_start via=form autonomy=ask operation:op-one start=1 admitted\n"
        f"2026-11-23T08:00:00.000002Z policy_confirm via=none autonomy=yolo execution_policy:{ZERO_DIGEST} applied\n"
    )
    for argv in ([*socket, "--json", *args], [*socket, *args, "--json"]):
        status, stdout, stderr = invoke(argv, client)
        assert (status, stderr) == (0, "")
        assert json.loads(stdout) == value
    assert client.calls == [("GET", path, None, False, SCHEMA)] * 3
    assert client.keys == [None] * 3


@pytest.mark.parametrize(("flags", "message"), [
    ([], "one of the arguments --operation --policy is required"),
    (["--operation", "op", "--policy", ZERO_DIGEST], "not allowed with argument --operation"),
])
def test_cli_approval_filters_are_mutually_exclusive_and_required(flags, message):
    client = FakeClient()
    status, stdout, stderr = invoke(["--socket", "/tmp/lab.sock", "approvals", *flags], client)
    assert (status, stdout, client.calls) == (2, "", [])
    assert message in stderr


@pytest.mark.parametrize(("argv", "option"), [
    (["approvals", "--help"], "--operation"),
    (["project", "approvals", "--help"], "project"),
])
def test_new_approval_commands_have_help(argv, option, capsys):
    status, stdout, stderr = invoke(argv, None)
    assert (status, stderr) == (0, "")
    assert option in (stdout or capsys.readouterr().out)
