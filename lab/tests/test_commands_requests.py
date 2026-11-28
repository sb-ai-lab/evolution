"""Operation and service reads as commands: each builds the request the CLI sends today (ADR-0070)."""
from __future__ import annotations

import pytest

from evolution_lab.commands import REGISTRY, Effect, Request
from tests.test_service_cli import DIGEST

READS = [
    ("health", {}, ("GET", "/v1/health", "lab-service-health/2")),
    ("operations_list", {}, ("GET", "/v1/operations", "lab-operation-list/8")),
    ("operations_summary", {}, ("GET", "/v1/operations?view=summary", "lab-operation-summary-list/1")),
    ("operation_status", {"operation": "op"}, ("GET", "/v1/operations/op", "lab-operation-status/8")),
    ("operation_failure", {"operation": "op"}, ("GET", "/v1/operations/op/failure", "lab-operation-failure/1")),
    ("operation_refusal", {"operation": "op"}, ("GET", "/v1/operations/op/refusal", "lab-operation-refusal/1")),
    ("operation_inputs", {"operation": "op"}, ("GET", "/v1/operations/op/inputs", "lab-operation-inputs/1")),
    ("operation_report", {"operation": "op"}, ("GET", "/v1/operations/op/report", "lab-owner-report/15")),
    ("operation_report_markdown", {"operation": "op"}, ("GET", "/v1/operations/op/report.md", None)),
    ("operation_campaign", {"operation": "op"}, ("GET", "/v1/operations/op/campaign", "lab-campaign-view/15")),
    ("operation_routing", {"operation": "op"}, ("GET", "/v1/operations/op/routing", "lab-operation-routing/1")),
    ("operation_suspensions", {"operation": "op"},
     ("GET", "/v1/operations/op/suspensions", "lab-suspension-evidence/1")),
    ("operation_measurements", {"operation": "op"},
     ("GET", "/v1/operations/op/measurements", "lab-measurement-list/4")),
    ("operation_measurement", {"operation": "op", "measurement": "m"},
     ("GET", "/v1/operations/op/measurements/m", "lab-measurement/4")),
    ("operation_sample", {"operation": "op", "measurement": "m@e2", "case": "case@1", "sample": "00"},
     ("GET", "/v1/operations/op/measurements/m%40e2/cases/case%401/samples/00", "lab-sample/1")),
    ("operation_sample_disclosed", {"operation": "op", "measurement": "m", "case": "case", "sample": "00"},
     ("GET", "/v1/operations/op/measurements/m/cases/case/samples/00?disclose=private", "lab-sample/1")),
    ("operation_attempt", {"operation": "op", "attempt": "r01-a01"},
     ("GET", "/v1/operations/op/attempts/r01-a01", "lab-attempt/1")),
    ("operation_attempt_disclosed", {"operation": "op", "attempt": "r01-a01"},
     ("GET", "/v1/operations/op/attempts/r01-a01?disclose=private", "lab-attempt/1")),
    ("operation_reflection", {"operation": "op", "reflection": "r01-x01"},
     ("GET", "/v1/operations/op/reflections/r01-x01", "lab-reflection/1")),
    ("operation_reflection_disclosed", {"operation": "op", "reflection": "r01-x01"},
     ("GET", "/v1/operations/op/reflections/r01-x01?disclose=private", "lab-reflection/1")),
    ("operation_diff", {"operation": "op"}, ("GET", "/v1/operations/op/diff", "lab-operation-diff/2")),
    ("operation_diff", {"operation": "op", "tree_from": "seed", "tree_to": "r01-a01"},
     ("GET", "/v1/operations/op/diff?from=seed&to=r01-a01", "lab-operation-diff/2")),
    ("operation_diff", {"operation": "op", "tree_to": "a b"},
     ("GET", "/v1/operations/op/diff?to=a+b", "lab-operation-diff/2")),
    ("bundle_show", {"digest": DIGEST}, ("GET", f"/v1/bundles/{DIGEST}", "lab-bundle-summary/3")),
    ("bundle_measurements", {"digest": DIGEST},
     ("GET", f"/v1/bundles/{DIGEST}/measurements", "lab-bundle-measurements/1")),
    ("prepared_show", {"digest": DIGEST},
     ("GET", f"/v1/prepared-executions/{DIGEST}", "lab-prepared-summary/1")),
    ("approvals_list", {"operation_id": "op"}, ("GET", "/v1/approvals?operation_id=op", "lab-approval-list/1")),
    ("approvals_list", {"policy_digest": DIGEST},
     ("GET", f"/v1/approvals?policy_digest={DIGEST}", "lab-approval-list/1")),
    ("execution_policies_list", {}, ("GET", "/v1/execution-policies", None)),
    ("execution_policy_show", {"digest": DIGEST},
     ("GET", f"/v1/execution-policies/{DIGEST}", "lab-execution-policy-record/1")),
    ("targets_list", {}, ("GET", "/v1/targets", "lab-owner-target-list/1")),
    ("targets_list", {"bundle": DIGEST, "ready": True},
     ("GET", f"/v1/targets?bundle={DIGEST}&state=ready", "lab-owner-target-list/1")),
    ("targets_list", {"ready": True}, ("GET", "/v1/targets?state=ready", "lab-owner-target-list/1")),
    ("target_status", {"target": "target", "owner_generation": "generation"},
     ("GET", "/v1/targets/target?owner_generation=generation", "lab-owner-target/2")),
    ("task_status", {"task": "task", "owner_generation": "generation"},
     ("GET", "/v1/tasks/task?owner_generation=generation", "lab-task-status/1")),
    ("service_scenarios_list", {}, ("GET", "/v1/scenarios", None)),
    ("service_scenario_show", {"scenario": "connect"}, ("GET", "/v1/scenarios/connect", None)),
    ("service_scenario_schema", {"scenario": "restructure", "schema": "behavior-map/1"},
     ("GET", "/v1/scenarios/restructure/schemas/behavior-map/1", None)),
    ("service_requirements", {}, ("GET", "/v1/guides/requirements", None)),
]


@pytest.mark.parametrize(("name", "kwargs", "expected"), READS)
def test_each_read_builds_the_cli_request(name, kwargs, expected):
    request = REGISTRY[name].build(**kwargs)
    assert isinstance(request, Request)
    assert (request.method, request.path, request.schema) == expected
    assert request.payload is None and request.mutation_key is None
    assert REGISTRY[name].effect is Effect.READ and REGISTRY[name].schema == expected[2]


def test_every_read_in_the_table_is_registered():
    assert {name for name, _, _ in READS} <= set(REGISTRY)


def test_response_kinds():
    assert REGISTRY["operation_report_markdown"].build(operation="op").kind == "text"
    assert REGISTRY["operation_report_markdown"].build(operation="op").content_type == "text/markdown"
    schema = REGISTRY["service_scenario_schema"].build(scenario="s", schema="n/1")
    assert (schema.kind, schema.content_type) == ("bytes", "application/schema+json")
    assert REGISTRY["health"].build().kind == "json"


@pytest.mark.parametrize(("name", "kwargs"), [
    ("bundle_show", {"digest": "nothex"}),
    ("prepared_show", {"digest": DIGEST.upper()}),
    ("execution_policy_show", {"digest": "abc"}),
    ("operation_attempt", {"operation": "op", "attempt": "x"}),
    ("operation_reflection", {"operation": "op", "reflection": "x"}),
    ("operation_measurement", {"operation": "op", "measurement": "bad id"}),
    ("operation_sample", {"operation": "op", "measurement": "m", "case": "bad/case", "sample": "00"}),
    ("operation_status", {"operation": ""}),
    ("operation_status", {"operation": 7}),
    ("target_status", {"target": "t", "owner_generation": ""}),
    ("task_status", {"task": "t", "owner_generation": 3}),
    ("service_scenario_schema", {"scenario": "s", "schema": "no-version"}),
])
def test_invalid_ids_raise_value_error(name, kwargs):
    with pytest.raises(ValueError):
        REGISTRY[name].build(**kwargs)


@pytest.mark.parametrize("scenario", ["", ".", "..", "a/b", 7, None])
def test_scenario_show_refuses_ids_that_change_the_route(scenario):
    with pytest.raises(ValueError):
        REGISTRY["service_scenario_show"].build(scenario=scenario)


@pytest.mark.parametrize(("scenario", "schema"), [
    ("", "n/1"), (".", "n/1"), ("..", "n/1"), ("a/b", "n/1"), (7, "n/1"),
    ("s", "spec-answers/.."), ("s", "./1"), ("s", "../1"), ("s", "n/."), ("s", 7), ("s", None),
])
def test_scenario_schema_refuses_segments_that_change_the_route(scenario, schema):
    with pytest.raises(ValueError):
        REGISTRY["service_scenario_schema"].build(scenario=scenario, schema=schema)
