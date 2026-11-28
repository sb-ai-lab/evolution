"""Pin the request builders to the existing CLI and Part A service contracts."""
from __future__ import annotations

import json

import pytest

from evolution_lab.commands import Effect, REGISTRY
from evolution_lab.commands._core import command
from evolution_lab.service.codecs import decode_operation_request
from evolution_lab.service.errors import ServiceError


KEY = ("r-fixed", "owner")
TARGET = {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "target-owner"}
SECRETS = {"TOKEN": "fixture"}
DIGEST = "a" * 64
PROJECT = "project/%"
PROJECT_KWARGS = {"project_id": PROJECT, "implementation": "impl"}
REF = {"kind": "operation", "id": "op"}
FINDINGS = [{"code": "finding", "digest": DIGEST, "effect": None}]
EXPECTED = [{"id": "change", "from": "old", "to": "new"}]

BUNDLE_BODY = {"schema": "lab-service-bundle-request/2", "source": {"path": "/a"}}
PREPARE_BODY = {"schema": "lab-service-prepare-request/5", "kind": "evaluation", "bundle_digest": DIGEST}
PROJECT_PREPARE = {"kind": "campaign", "bundle_digest": DIGEST,
                   "stop_grace_seconds": None, "cleanup_wait_seconds": None}

# (builder, keyword arguments, request method, path, response schema, body)
LEGACY = [
    ("bundle_create", {"body": BUNDLE_BODY}, "POST", "/v1/bundles", "lab-bundle-summary/3", BUNDLE_BODY),
    ("prepare", {"body": PREPARE_BODY}, "POST", "/v1/prepared-executions", "lab-prepared-summary/1", PREPARE_BODY),
    ("connect", {"bundle_digest": DIGEST}, "POST", "/v1/connections/from-bundle", "lab-owner-connection/1",
     {"schema": "lab-service-bind-request/1", "bundle_digest": DIGEST}),
    ("target_start", {"connection_id": "c/%", "secrets": SECRETS}, "POST", "/v1/connections/c%2F%25/target",
     "lab-task-ref/1", {"schema": "lab-service-target-start-request/1", "secrets": SECRETS}),
    ("target_start", {"connection_id": "c", "secrets": SECRETS, "execution_policy": DIGEST}, "POST",
     "/v1/connections/c/target", "lab-task-ref/1",
     {"schema": "lab-service-target-start-request/2", "secrets": SECRETS, "execution_policy": DIGEST}),
    ("target_stop", {"target_id": "target", "target_generation": "owner"}, "DELETE", "/v1/targets/target?owner_generation=owner",
     "lab-task-ref/1", None),
    ("check", {"connection_id": "c", "target_ref": TARGET}, "POST", "/v1/connections/c/check", "lab-task-ref/1",
     {"schema": "lab-service-check-request/1", "target_ref": TARGET}),
    ("conformance", {"connection_id": "c", "target_ref": TARGET}, "POST", "/v1/connections/c/conformance",
     "lab-task-ref/1", {"schema": "lab-service-conformance-request/1", "target_ref": TARGET, "runtime_policy": None}),
    ("conformance", {"connection_id": "c", "target_ref": TARGET,
                     "runtime_policy": {"schema": "lab-runtime-policy/1"}}, "POST", "/v1/connections/c/conformance",
     "lab-task-ref/1", {"schema": "lab-service-conformance-request/1", "target_ref": TARGET,
                       "runtime_policy": {"schema": "lab-runtime-policy/1"}}),
    ("task_stop", {"task_id": "task"}, "POST", "/v1/tasks/task/stop", "lab-owner-stop/1", {}),
    ("operation_stop", {"operation_id": "op/%"}, "POST", "/v1/operations/op%2F%25/stop", "lab-owner-stop/1", {}),
    ("operation_start", {"prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS}, "POST",
     "/v1/operations", "lab-operation-status/8",
     {"schema": "lab-service-start-request/1", "prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS}),
    ("operation_start", {"prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS,
                         "linked_from": "previous", "baseline": "fresh", "seed": "project"}, "POST",
     "/v1/operations", "lab-operation-status/8",
     {"schema": "lab-service-start-request/2", "prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS,
      "linked_from": "previous", "baseline": "fresh", "seed": "project"}),
    ("operation_start", {"prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS,
                         "baseline_from": "evaluation"}, "POST", "/v1/operations", "lab-operation-status/8",
     {"schema": "lab-service-start-request/3", "prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS,
      "linked_from": None, "baseline": "inherit", "seed": "champion", "baseline_from": "evaluation"}),
    ("operation_resume", {"operation_id": "op", "target_ref": TARGET, "secrets": SECRETS}, "POST",
     "/v1/operations/op/resume", "lab-operation-status/8",
     {"schema": "lab-service-resume-request/1", "target_ref": TARGET, "secrets": SECRETS}),
    ("operation_resume", {"operation_id": "op", "target_ref": TARGET, "secrets": SECRETS,
                          "accept": FINDINGS, "reason": "continue", "prepared_digest": DIGEST}, "POST",
     "/v1/operations/op/resume", "lab-operation-status/8",
     {"schema": "lab-service-resume-request/2", "target_ref": TARGET, "secrets": SECRETS,
      "accept": FINDINGS, "reason": "continue", "prepared_digest": DIGEST}),
    ("operation_resume", {"operation_id": "op", "target_ref": TARGET, "secrets": SECRETS, "accept": None, "reason": ""}, "POST",
     "/v1/operations/op/resume", "lab-operation-status/8",
     {"schema": "lab-service-resume-request/2", "target_ref": TARGET, "secrets": SECRETS,
      "accept": [], "reason": "", "prepared_digest": None}),
    ("policy_confirm", {"policy": {"routing": {}}, "expected_digest": DIGEST, "author": "author", "channel": "cli",
                        "note": None}, "POST", "/v1/execution-policies", "lab-execution-policy-record/1",
     {"schema": "lab-service-policy-confirm-request/1", "policy": {"routing": {}}, "expected_digest": DIGEST,
      "author": "author", "channel": "cli", "note": None}),
    ("delete_results", {"operation_id": "op"}, "DELETE", "/v1/operations/op/results", "lab-task-ref/1", None),
    ("release", {"ref": REF, "reason": "done"}, "POST", "/v1/releases", "lab-owner-release/1",
     {"schema": "lab-service-release-request/1", "ref": REF, "reason": "done"}),
    ("cleanup", {"ref": REF, "wait_seconds": None}, "POST", "/v1/cleanup", "lab-task-ref/1",
     {"schema": "lab-service-cleanup-request/1", "ref": REF, "wait_seconds": None}),
    ("project_bundle", PROJECT_KWARGS, "POST", "/v1/projects/project%2F%25/bundles", "lab-project-bundle-result/1",
     {"schema": "lab-service-project-bundle-request/1", "implementation": "impl"}),
    ("project_bundle", {"project_id": ""}, "POST", "/v1/projects/bundles", "lab-project-bundle-result/1",
     {"schema": "lab-service-project-bundle-request/1", "implementation": None}),
    ("project_prepare", {**PROJECT_KWARGS, "body": PROJECT_PREPARE}, "POST", "/v1/projects/project%2F%25/prepared-executions",
     "lab-project-prepared-result/1",
     {"schema": "lab-service-project-prepare-request/1", "implementation": "impl", **PROJECT_PREPARE}),
    ("project_prepare", {**PROJECT_KWARGS, "body": {"kind": "evaluation", "split": None}}, "POST",
     "/v1/projects/project%2F%25/prepared-executions", "lab-project-prepared-result/1",
     {"schema": "lab-service-project-prepare-request/2", "implementation": "impl", "kind": "evaluation", "split": None}),
    ("project_target", {**PROJECT_KWARGS, "bundle_digest": DIGEST, "secrets": SECRETS}, "POST",
     "/v1/projects/project%2F%25/targets", "lab-project-target-result/1",
     {"schema": "lab-service-project-target-request/1", "implementation": "impl", "bundle_digest": DIGEST,
      "secrets": SECRETS}),
    ("project_sync_check", {"project_id": PROJECT, "rev": None}, "POST", "/v1/projects/project%2F%25/sync/check",
     "lab-project-sync-check/1", {"schema": "lab-service-project-sync-check-request/1", "rev": None}),
    ("project_sync", {"project_id": PROJECT, "upstream": "012345"}, "POST",
     "/v1/projects/project%2F%25/sync", "lab-project-sync-result/1",
     {"schema": "lab-service-project-sync-request/1", "upstream": "012345"}),
    ("project_start", {**PROJECT_KWARGS, "prepared_digest": DIGEST, "target_ref": TARGET, "secrets": SECRETS},
     "POST", "/v1/projects/project%2F%25/operations", "lab-project-start-result/5",
     {"schema": "lab-service-project-start-request/1", "implementation": "impl", "prepared_digest": DIGEST,
      "target_ref": TARGET, "secrets": SECRETS}),
    ("project_confirm", {**PROJECT_KWARGS, "claim": "claim", "attests": {"path": "a"},
                         "linked": [{"operation": "op"}], "channel": "cli", "author": "author", "note": None},
     "POST", "/v1/projects/project%2F%25/confirmations", "lab-project-confirmation/1",
     {"schema": "lab-service-confirm-request/1", "implementation": "impl", "claim": "claim",
      "attests": {"path": "a"}, "linked": [{"operation": "op"}], "channel": "cli", "author": "author", "note": None}),
    ("project_accept", {**PROJECT_KWARGS, "only": None, "across_pairs": False, "reason": "ok", "channel": "cli",
                        "author": "author", "dry_run": True, "expected": None},
     "POST", "/v1/projects/project%2F%25/acceptances", "lab-project-acceptance/1",
     {"schema": "lab-service-accept-request/2", "implementation": "impl", "only": None, "across_pairs": False,
      "reason": "ok", "channel": "cli", "author": "author", "dry_run": True, "expected": None}),
    ("project_accept", {**PROJECT_KWARGS, "only": ["change"], "across_pairs": True, "reason": "ok",
                        "channel": "cli", "author": "author", "dry_run": False, "expected": EXPECTED},
     "POST", "/v1/projects/project%2F%25/acceptances", "lab-project-acceptance/1",
     {"schema": "lab-service-accept-request/2", "implementation": "impl", "only": ["change"], "across_pairs": True,
      "reason": "ok", "channel": "cli", "author": "author", "dry_run": False, "expected": EXPECTED}),
    ("project_revoke_confirmation", {"project_id": PROJECT, "confirmation_id": "record/%", "reason": "undo"},
     "POST", "/v1/projects/project%2F%25/confirmations/record%2F%25/revoke", "lab-project-confirmation/1",
     {"schema": "lab-service-revoke-request/1", "reason": "undo"}),
    ("project_revoke_acceptance", {"project_id": PROJECT, "acceptance_id": "record/%", "reason": "undo"},
     "POST", "/v1/projects/project%2F%25/acceptances/record%2F%25/revoke", "lab-project-acceptance/1",
     {"schema": "lab-service-revoke-request/1", "reason": "undo"}),
]

# Versioned bodies keep the legacy field order and add only their newly admitted fields.
APPROVED = {
    "operation_start": ("lab-service-start-request/4", {}),
    "operation_resume": ("lab-service-resume-request/3", {"observed_start": 2}),
    "policy_confirm": ("lab-service-policy-confirm-request/2", {}),
    "delete_results": ("lab-service-delete-results-request/1", {"observed_start": 2}),
    "release": ("lab-service-release-request/2", {"observed_start": 2}),
    "cleanup": ("lab-service-cleanup-request/2", {"observed_start": 2}),
    "project_start": ("lab-service-project-start-request/2", {}),
    "project_confirm": ("lab-service-confirm-request/2", {}),
    "project_accept": ("lab-service-accept-request/3", {}),
    "project_revoke_confirmation": ("lab-service-revoke-request/2", {}),
    "project_revoke_acceptance": ("lab-service-revoke-request/2", {}),
    "project_sync": ("lab-service-project-sync-request/2", {}),
}
IRREVERSIBLE = {"delete_results", "release", "cleanup", "project_revoke_confirmation", "project_revoke_acceptance"}


def _build(name, *args, **kwargs):
    assert name in REGISTRY
    return REGISTRY[name].build(*args, **kwargs)


def _wire_body(payload):
    return None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("name,kwargs,method,path,schema,payload", LEGACY, ids=[case[0] for case in LEGACY])
def test_legacy_request(name, kwargs, method, path, schema, payload):
    for key in (KEY, (None, None)):
        request = _build(name, *key, **kwargs)
        assert (request.method, request.path, request.schema) == (method, path, schema)
        assert _wire_body(request.payload) == _wire_body(payload)
        assert request.mutation_key == (KEY if key == KEY else None)
        assert REGISTRY[name].schema == schema
        assert request.effect == (Effect.IRREVERSIBLE if name == "operation_resume" and kwargs.get("accept") else None)


APPROVED_CASES = [case for case in LEGACY if case[0] in APPROVED and not case[1].get("dry_run")]


@pytest.mark.parametrize("name,kwargs,method,path,schema,legacy", APPROVED_CASES, ids=[case[0] for case in APPROVED_CASES])
def test_approved_request(name, kwargs, method, path, schema, legacy):
    version, extra = APPROVED[name]
    approval = {"via": "form", "autonomy": "ask", "tool": "project_accept_commit" if name == "project_accept" else name}
    fields = dict(legacy or {})
    fields = {"schema": version, **{k: v for k, v in fields.items() if k != "schema"}}
    if name == "operation_start" and "baseline_from" not in fields:
        fields.update(linked_from=kwargs.get("linked_from"), baseline=kwargs.get("baseline", "inherit"),
                      seed=kwargs.get("seed", "champion"), baseline_from=None)
    if name == "operation_resume" and "accept" not in fields:
        fields.update(accept=[], reason=None, prepared_digest=None)
    request = _build(name, *KEY, **kwargs, approval=approval, **extra)
    assert (request.method, request.path, request.schema, request.mutation_key) == (method, path, schema, KEY)
    assert _wire_body(request.payload) == _wire_body({**fields, "approval": approval, **extra})
    assert request.effect == (Effect.IRREVERSIBLE if name == "operation_resume" and kwargs.get("accept") else None)


START_APPROVAL = {"via": "form", "autonomy": "ask", "tool": "operation_start"}


@pytest.mark.parametrize("kwargs", [
    {"linked_from": "previous", "baseline_from": "evaluation"},
    {"linked_from": "previous", "baseline": ""},
    {"seed": ""},
])
def test_approved_start_leaves_invalid_links_and_options_for_service_to_refuse(kwargs):
    request = _build("operation_start", *KEY, DIGEST, TARGET, SECRETS, approval=START_APPROVAL, **kwargs)
    with pytest.raises(ServiceError) as error:
        decode_operation_request(request.payload)
    assert error.value.code == "invalid_request"


def test_approved_start_defaults_decode_with_no_links():
    request = _build("operation_start", *KEY, DIGEST, TARGET, SECRETS, approval=START_APPROVAL)
    decoded = decode_operation_request(request.payload)
    assert (decoded.linked_from, decoded.baseline, decoded.seed, decoded.baseline_from) == (
        None, "inherit", "champion", None,
    )


def test_target_stop_defaults_to_the_key_generation():
    request = _build("target_stop", *KEY, "target")
    assert request.path == "/v1/targets/target?owner_generation=owner"


def test_target_stop_keeps_explicit_query_generation_independent_of_key():
    for key, expected_key in [(KEY, KEY), ((None, None), None)]:
        request = _build("target_stop", *key, "target", target_generation="target/owner")
        assert request.path == "/v1/targets/target?owner_generation=target%2Fowner"
        assert request.mutation_key == expected_key


def test_project_prepare_extended_version_fills_nullable_settings():
    request = _build("project_prepare", *KEY, PROJECT, "impl", {"kind": "evaluation", "sample_retries": 1})
    assert request.payload == {
        "schema": "lab-service-project-prepare-request/3", "implementation": "impl", "kind": "evaluation",
        "sample_retries": 1, "min_coverage": None, "concurrency": None, "scorer_concurrency": None,
        "max_consecutive_unmeasured": None, "split": None,
    }


def test_approved_release_and_cleanup_carry_null_start_for_non_operation_ref():
    for name, args in [("release", {"reason": "done"}), ("cleanup", {"wait_seconds": None})]:
        approval = {"via": "form", "autonomy": "ask", "tool": name}
        request = _build(name, *KEY, ref={"kind": "target", "id": "target"}, **args, approval=approval)
        assert "observed_start" in request.payload
        assert request.payload["observed_start"] is None


def test_registry_declares_effect_and_asks_for_each_mutation():
    mutations = {name for name, entry in REGISTRY.items() if entry.effect is not Effect.READ}
    assert mutations == {case[0] for case in LEGACY}
    assert {name for name, entry in REGISTRY.items() if entry.asks} == set(APPROVED)
    assert {name for name in mutations if REGISTRY[name].effect is Effect.IRREVERSIBLE} == IRREVERSIBLE


def test_read_command_cannot_ask():
    with pytest.raises(ValueError, match="read"):
        command("invalid", effect=Effect.READ, schema=None, asks=True)(lambda: None)
