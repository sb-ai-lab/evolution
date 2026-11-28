"""A command's value is the CLI's validated value (ADR-0070 §4: each value is compared with its authority).

For every command-layer read with a schema, one valid response from the existing fixtures goes through both
paths: the layer's ``check_response`` and the CLI over a real ``ServiceClient``, whose ``request_json`` applies
the CLI's own response validation. The value comes back unchanged from the layer and is printed unchanged by
``--json``; the same value under another schema id is refused by both.
"""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import httpx
import pytest

from evolution_lab.cli.client import ClientError, ServiceClient
from evolution_lab.cli.output import write_json
from evolution_lab.commands import REGISTRY, Effect
from evolution_lab.commands._core import check_response
from tests.panel_fixtures import load_snapshot
from tests.test_approvals_reads import APPROVAL, SCHEMA as APPROVAL_LIST_SCHEMA
from tests.test_commands_cli_parity import CASES, FAILURE, INPUTS, REFUSAL
from tests.test_commands_coverage import CLI_READS
from tests.test_service_cli import DIGEST, HEALTH, _full_status, invoke

DATA = Path(__file__).parent / "data"
OPERATION = "/v1/operations/8e93a4b6fa04495b8a90179c331dd0f5"
PROJECT = "/v1/projects/demo-connection"


def _snapshot(name: str, route: str) -> dict:
    """A recorded service answer from a panel snapshot, in its current form."""
    return copy.deepcopy(load_snapshot(name)["routes"][route]["body"])


def _data(path: str, *keys: str) -> dict:
    value = json.loads((DATA / path).read_text(encoding="utf-8"))
    for key in keys:
        value = value[key]
    return value


def _target() -> dict:
    # As tests/test_owner_target_gateway.py: the dev65 reference target, read under its current schema id.
    return {**_data("dev65-phase-a-reference/target.json"), "schema": "lab-owner-target/2"}


def _measurements(detail: bool) -> dict:
    from evolution_lab.owner import measurements
    from tests.test_focus_signal_notice import core

    rows = measurements.measurement_rows(core(), "evaluation", {}, None)
    if not detail:
        return {"schema": "lab-measurement-list/4", "operation_id": "op1", "measurements": rows}
    row, = measurements.measurement_rows(core(), "evaluation", {}, None, detail=rows[0]["measurement_id"])
    return {"schema": "lab-measurement/4", "operation_id": "op1", "measurement": row}


def _routing() -> dict:
    from evolution_lab.owner.routing import build_routing
    return build_routing("op", [dict(start=1, policy_digest=None, lab_version="v")], {})


#: Layer command -> a valid response for its schema, built from existing fixtures.
def _policy_record() -> dict:
    from evolution_lab.execution_policy import policy_digest
    from evolution_lab.owner.models import PolicyRecord
    from tests.test_execution_policy import policy_payload
    policy = policy_payload()
    return PolicyRecord(policy_digest(policy), policy, "operator", "cli", None, 1_700_000_000.5).to_payload()


VALUES = {
    "bundle_show": lambda: {  # the stored summary of tests/test_preparation_storage.py, with test digests
        "schema": "lab-bundle-summary/3", "digest": DIGEST, "source_kind": "campaign",
        "entrypoint": "inputs/project/manifest.json", "measurement_basis": None, "launch_digest": DIGEST,
        "environment_digests": [DIGEST], "secret_names": ["API_KEY", "TOKEN"], "entry_count": 4,
        "limitations": ["wheel archives remain external"], "warnings": []},
    "bundle_measurements": lambda: {  # tests/test_bundle_measurements.py
        "schema": "lab-bundle-measurements/1", "bundle_digest": DIGEST, "measurement_basis": None, "items": []},
    "prepared_show": lambda: _data("dev66-reference/evaluation.json", "requests", "explicit", "prepared"),
    "health": lambda: dict(HEALTH),
    "operation_attempt": lambda: _snapshot("b13/checks", f"{OPERATION}/attempts/r01-a01"),
    "operation_attempt_disclosed": lambda: _snapshot("b13/checks", f"{OPERATION}/attempts/r01-a01"),
    "operation_reflection": lambda: _snapshot("b13/checks", f"{OPERATION}/reflections/r01-x01"),
    "operation_reflection_disclosed": lambda: _snapshot("b13/checks", f"{OPERATION}/reflections/r01-x01"),
    "operation_sample": lambda: _snapshot(
        "b13/checks", f"{OPERATION}/measurements/m-r00-champion/cases/q1/samples/01"),
    "operation_sample_disclosed": lambda: _snapshot(
        "b13/checks", f"{OPERATION}/measurements/m-r00-champion/cases/q1/samples/01"),
    "operation_suspensions": lambda: _snapshot("b13/checks", f"{OPERATION}/suspensions"),
    "operation_campaign": lambda: _snapshot("content", "/v1/operations/c312a20fe33643e1a33f39c008a6729e/campaign"),
    "operation_diff": lambda: _snapshot("b13/family14", "/v1/operations/184ed063aa8d40a9af9ef0be6ac68f08/diff"),
    "operation_report": lambda: _snapshot("details", "/v1/operations/fe7e7bbc7f4c410abc3864e3eb87b145/report"),
    "operation_routing": _routing,
    "operation_measurements": lambda: _measurements(detail=False),
    "operation_measurement": lambda: _measurements(detail=True),
    "operation_status": lambda: _full_status(None),
    "operation_failure": lambda: copy.deepcopy(FAILURE),
    "operation_refusal": lambda: copy.deepcopy(REFUSAL),
    "operation_inputs": lambda: copy.deepcopy(INPUTS),
    "operations_list": lambda: {"schema": "lab-operation-list/8", "operations": [_full_status(None)]},
    "operations_summary": lambda: _snapshot("b13/checks", "/v1/operations?view=summary"),
    "target_status": _target,
    "targets_list": lambda: {"schema": "lab-owner-target-list/1", "targets": [_target()]},
    "task_status": lambda: {  # tests/test_service_cli.py, the succeeded deletion
        "schema": "lab-task-status/1", "id": "task", "owner_generation": "generation", "action": "delete",
        "ref": None, "state": "succeeded", "result": None, "failure": None,
        "persistence": {"state": "saved", "failure": None}},
    "projects_list": lambda: _snapshot("b13/checks", "/v1/projects"),
    "project_show": lambda: _snapshot("b13/family14", PROJECT),
    "project_status": lambda: _snapshot("b13/family14", f"{PROJECT}/status"),
    "project_facts": lambda: _data("dev67-reference/facts.json"),
    "project_confirmations": lambda: _snapshot("b13/family14", f"{PROJECT}/confirmations"),
    "project_operations": lambda: _snapshot("b13/family14", f"{PROJECT}/operations"),
    "project_operations_summary": lambda: _snapshot("b13/family14", f"{PROJECT}/operations?view=summary"),
    "approvals_list": lambda: {
        "schema": APPROVAL_LIST_SCHEMA,
        "approvals": [{**APPROVAL, "resource": {"kind": "operation", "id": "op"}}],
    },
    "project_approvals": lambda: {
        "schema": APPROVAL_LIST_SCHEMA,
        "approvals": [{**APPROVAL, "tool": "project_confirm", "start": None, "outcome": "applied",
                       "resource": {"kind": "confirmation", "id": "confirmation-one"}}],
    },
    "execution_policy_show": lambda: _policy_record(),
    "project_tree": lambda: {  # the absent-path answer of tests/test_projects_tree_read.py
        "schema": "lab-project-tree/1", "project": "p", "implementation": "i", "path": "a/b", "exists": False,
        "digest": None, "bundle_covers": None, "bundle_reason": None, "files": []},
}

#: Layer command -> why no valid value is compared.
VALUE_PARITY_EXCLUDED: dict[str, str] = {}

#: Reads the CLI makes only as follow-ups of text ``lab status``; it sends them through ``parser._read``.
FOLLOW_UPS = {"operation_failure", "operation_inputs", "operation_refusal"}

#: Captured from ``feat/lab-mcp`` (before the layer): a response the CLI's validation refuses.
CLIENT_ERROR_STDERR = ('{"error":{"code":"client_error","stage":"transport",'
                       '"message":"The local service client failed."}}\n')

SCHEMA_READS = sorted(name for name, command in REGISTRY.items()
                      if command.effect is Effect.READ and command.needs_service and command.schema is not None)
COVERED = sorted(VALUES)

#: Layer command -> (argv, layer kwargs): the first CLI case of its variant in the parity test.
ARGV: dict[str, tuple[list[str], dict]] = {}
for _variant, _argv, _kwargs in CASES:
    ARGV.setdefault(CLI_READS[_variant], (_argv, _kwargs))
FOLLOW_UP_KWARGS = {"operation": "op-1"}


def test_every_schema_read_is_covered_or_excluded():
    assert set(VALUES).isdisjoint(VALUE_PARITY_EXCLUDED)
    assert set(VALUES) | set(VALUE_PARITY_EXCLUDED) == set(SCHEMA_READS)
    assert FOLLOW_UPS == set(SCHEMA_READS) - {CLI_READS[variant] for variant in CLI_READS}


def _client(value, seen):
    """A real ``ServiceClient``: its ``request_json`` decodes and validates exactly as against the service."""
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode("ascii")))
        return httpx.Response(200, json=value)
    return ServiceClient(socket_path="/tmp/lab.sock", transport=httpx.MockTransport(answer))


def _request(name):
    kwargs = FOLLOW_UP_KWARGS if name in FOLLOW_UPS else ARGV[name][1]
    return REGISTRY[name].build(**kwargs)


def _json(value) -> str:
    stream = io.StringIO()
    write_json(stream, value)
    return stream.getvalue()


@pytest.mark.parametrize("name", COVERED)
def test_valid_value_is_the_layer_value_and_the_cli_output(name):
    value = VALUES[name]()
    request = _request(name)
    assert value["schema"] == request.schema == REGISTRY[name].schema
    original = copy.deepcopy(value)
    assert check_response(request, value) is value
    assert value == original
    seen = []
    if name in FOLLOW_UPS:
        from evolution_lab.cli.parser import _read
        assert _read(_client(value, seen), request) == original
    else:
        code, out, err = invoke(ARGV[name][0], _client(value, seen))
        assert (code, err) == (0, ""), err
        assert out == _json(original)
    assert seen == [(request.method, request.path)]


@pytest.mark.parametrize("name", COVERED)
def test_another_schema_id_is_refused_by_both(name):
    value = {**VALUES[name](), "schema": "lab-not-this/1"}
    request = _request(name)
    with pytest.raises(ValueError):
        check_response(request, value)
    if name in FOLLOW_UPS:
        from evolution_lab.cli.parser import _read
        with pytest.raises(ClientError):
            _read(_client(value, []), request)
    else:
        assert invoke(ARGV[name][0], _client(value, [])) == (1, "", CLIENT_ERROR_STDERR)
