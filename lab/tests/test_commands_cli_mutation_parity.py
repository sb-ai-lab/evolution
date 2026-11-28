"""Byte-for-byte CLI mutation fixtures captured on origin/main after Part A."""
from dataclasses import dataclass, field
import io
import json
import shlex

import pytest

from evolution_lab.cli import main
from evolution_lab.cli.client import AcceptanceUnknown, ApiError, RequestTimeout
from evolution_lab.cli import parser as cli_parser
from test_execution_policy import policy_payload
from test_service_cli import CONNECTION, DIGEST, EVALUATION_REQUEST, FakeClient, HEALTH, SLOT_HELD, TASK_REF
from test_service_cli_projects import SYNC_CHECK, SYNC_RESULT


KEY = ("k" * 32, "key-generation")
DEFAULT_TARGET_ARGS = "--target target --generation target-generation"
TARGET_REF = {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "target-generation"}
TASK_STATUS = {
    "schema": "lab-task-status/1", "id": "task", "owner_generation": "generation", "action": "cleanup",
    "ref": None, "state": "succeeded", "result": None, "failure": None,
    "persistence": {"state": "saved", "failure": None},
}
OPERATION = {"schema": "lab-operation-status/8", "id": "op", "control_state": "reserved",
             "process": {"launched": False}, "target_cleanup": {"state": "not_needed"}}
STOPPED = {"schema": "lab-owner-stop/1", "id": "op", "state": "stopping"}
RELEASED = {"schema": "lab-owner-release/1", "id": "op", "state": "released"}
PREPARED = {"schema": "lab-prepared-summary/1", "digest": DIGEST}
BUNDLE = {"schema": "lab-bundle-summary/3", "digest": DIGEST,
          "warnings": [{"code": "lab_version_mismatch", "detail": {"expected": "a", "found": None}}]}
POLICY = policy_payload()
POLICY_RECORD = {"schema": "lab-execution-policy-record/1", "digest": DIGEST}
BUNDLE_SOURCE = {"source": {"kind": "evaluation", "input_path": "/abs/i.json", "launch_path": "/abs/l.json"},
                 "environments": [{"kind": "service"}], "env": {}, "secret_names": [], "roots": [],
                 "path_bindings": []}
EVALUATION = {k: v for k, v in EVALUATION_REQUEST.items() if k not in {"schema", "split"}}
CAMPAIGN = {"kind": "campaign", "bundle_digest": DIGEST, "stop_grace_seconds": None, "cleanup_wait_seconds": None}
RUNTIME_POLICY = {"stop_grace_seconds": 1, "cleanup_wait_seconds": 2}
EVALUATION_SETTINGS = {"sample_retries", "min_coverage", "concurrency", "scorer_concurrency"}
PREPARE_4_SETTINGS = dict.fromkeys(EVALUATION_SETTINGS | {"split"})
PREPARE_5_SETTINGS = dict.fromkeys(EVALUATION_SETTINGS | {"split", "max_consecutive_unmeasured"})
PROJECT_SETTINGS = {"sample_retries", "min_coverage", "concurrency", "scorer_concurrency",
                    "max_consecutive_unmeasured"}
PROJECT_PREPARE_3_SETTINGS = dict.fromkeys(PROJECT_SETTINGS | {"split"})
# These fields are emitted from Python sets on main. Pin their contents and positions,
# and compare bytes for fixed-order fields. The within-group order varies by hash seed
# and can even differ between imported and pytest-rewritten modules.
HASH_ORDERED_FIELDS = {
    "lab-service-prepare-request/4": PREPARE_4_SETTINGS.keys(),
    "lab-service-prepare-request/5": PREPARE_5_SETTINGS.keys(),
    "lab-service-project-prepare-request/3": PROJECT_PREPARE_3_SETTINGS.keys(),
}
PROJECT_BUNDLE = {"schema": "lab-project-bundle-result/1", "bundle": BUNDLE,
                  "link": {"kind": "bundle", "object_id": DIGEST}}
PROJECT_PREPARED = {"schema": "lab-project-prepared-result/1", "prepared": PREPARED, "link": None}
PROJECT_TARGET = {"schema": "lab-project-target-result/1", "task": TASK_REF, "link": None}
PROJECT_START = {"schema": "lab-project-start-result/5", "operation": OPERATION, "link": None}
CONFIRMATION = {"schema": "lab-project-confirmation/1", "id": "c-1"}
ACCEPTANCE = {
    "schema": "lab-project-acceptance/1", "id": "a-1",
    "facts": [{"id": "spec.confirmed", "from": {"kind": "confirmation", "id": "c-1"},
               "to": {"attests": "sha256:" + DIGEST}, "codes": ["digest_changed"], "changes": None}],
    "dry_run": False,
}
PREVIEW = {**ACCEPTANCE, "id": None, "dry_run": True}
ACCEPTANCE_LINE = "fact spec.confirmed from=confirmation:c-1 codes=digest_changed changes=none\n"
ACCEPTANCE_BODY = {"schema": "lab-service-accept-request/2", "implementation": None, "only": None,
                   "across_pairs": False, "reason": "typo", "channel": "cli", "author": "cli",
                   "dry_run": True, "expected": None}
ACCEPTANCE_COMMIT = {**ACCEPTANCE_BODY, "dry_run": False, "expected": [
    {"id": "spec.confirmed", "from": {"kind": "confirmation", "id": "c-1"}, "to": {"attests": "sha256:" + DIGEST}},
]}
REVOCATION_BODY = {"schema": "lab-service-revoke-request/1", "reason": "typo"}
READY_TARGETS = {"schema": "lab-owner-target-list/1", "targets": [
    {"id": "t-old", "owner_generation": "g1", "state": "ready", "bundle_digest": "b" * 64},
]}
RETURN_TO_TARGET = {"schema": "lab-operation-refusal/1", "findings": [
    {"code": "run_identity_changed", "instance_digest": DIGEST, "effects": ["restart_round"]},
]}
DIGEST_MISMATCH = {"error": {"code": "digest_mismatch", "stage": "before_start", "message": "Facts changed."}}
WAIT_TIMEOUT = {"error": {"code": "wait_timeout", "stage": "transport",
                          "message": "The accepted task did not finish before the timeout."},
                "acceptance": "accepted", "task_ref": TASK_REF}
SHA = "a" * 40


def _wire(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _wire_body(body):
    if body is None:
        return None
    fields = HASH_ORDERED_FIELDS.get(body.get("schema"))
    if fields is None:
        return _wire(body)
    ordered_fields = iter(sorted((name, value) for name, value in body.items() if name in fields))
    return _wire(dict(
        next(ordered_fields) if name in fields else (name, value) for name, value in body.items()
    ))


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: dict | None
    mutation: bool
    schema: str
    key: tuple | None
    timeout: float | None = None


def _post(path, schema, body, key=KEY):
    return Call("POST", path, body, True, schema, key)


def _get(path, schema, *, timeout=None):
    return Call("GET", path, None, False, schema, None, timeout)


HEALTH_CALL = _get("/v1/health", "lab-service-health/2")
START_AGENT = _post("/v1/connections/conn/target", "lab-task-ref/1",
                    {"schema": "lab-service-target-start-request/1", "secrets": {}})
ACCEPT_PATH = "/v1/projects/acq/acceptances"
ACCEPT_SCHEMA = "lab-project-acceptance/1"


@dataclass(frozen=True)
class Case:
    name: str
    command: str
    calls: tuple
    replies: tuple
    text_stdout: str
    json_stdout: tuple
    exit_code: int = 0
    stderr: str = ""
    options: tuple = ("--request-id", KEY[0], "--generation", KEY[1])
    stdin: str = ""
    files: dict = field(default_factory=dict)
    json_calls: tuple | None = None
    json_replies: tuple | None = None


def _one(name, command, call, reply, text, **kwargs):
    return Case(name, command, (call,), (reply,), text, (reply,), **kwargs)


CASES = [
    _one("bundle-create", "bundle create --request -",
         _post("/v1/bundles", "lab-bundle-summary/3", {"schema": "lab-service-bundle-request/2", **BUNDLE_SOURCE}),
         BUNDLE, f"lab-bundle-summary/3 {DIGEST}\nwarning: lab_version_mismatch expected=a found=-\n",
         stdin=json.dumps(BUNDLE_SOURCE)),
    _one("prepare-v2", "prepare --request -",
         _post("/v1/prepared-executions", "lab-prepared-summary/1", {"schema": "lab-service-prepare-request/2", **CAMPAIGN}),
         PREPARED, f"lab-prepared-summary/1 {DIGEST}\n", stdin=json.dumps(CAMPAIGN)),
    _one("prepare-v3", "prepare --request -",
         _post("/v1/prepared-executions", "lab-prepared-summary/1",
               {"schema": "lab-service-prepare-request/3", **EVALUATION, "split": "test"}),
         PREPARED, f"lab-prepared-summary/1 {DIGEST}\n", stdin=json.dumps({**EVALUATION, "split": "test"})),
    _one("prepare-v4", "prepare --request -",
         _post("/v1/prepared-executions", "lab-prepared-summary/1",
               {"schema": "lab-service-prepare-request/4", **PREPARE_4_SETTINGS, **EVALUATION, "concurrency": 2}),
         PREPARED, f"lab-prepared-summary/1 {DIGEST}\n", stdin=json.dumps({**EVALUATION, "concurrency": 2})),
    _one("prepare-v5", "prepare --request -",
         _post("/v1/prepared-executions", "lab-prepared-summary/1",
               {"schema": "lab-service-prepare-request/5", **PREPARE_5_SETTINGS, **EVALUATION, "max_consecutive_unmeasured": 2}),
         PREPARED, f"lab-prepared-summary/1 {DIGEST}\n",
         stdin=json.dumps({**EVALUATION, "max_consecutive_unmeasured": 2})),
    _one("connect", f"connect {DIGEST}",
         _post("/v1/connections/from-bundle", "lab-owner-connection/1",
               {"schema": "lab-service-bind-request/1", "bundle_digest": DIGEST}),
         CONNECTION, "lab-owner-connection/1 connection\n"),
    _one("agent-start", "agent start conn", START_AGENT, TASK_REF, "lab-task-ref/1 task\n"),
    _one("agent-start-policy", f"target start conn --execution-policy {DIGEST} --secrets-file @secrets.json",
         _post("/v1/connections/conn/target", "lab-task-ref/1",
               {"schema": "lab-service-target-start-request/2", "secrets": {"TEST_KEY": "fixture"},
                "execution_policy": DIGEST}),
         TASK_REF, "lab-task-ref/1 task\n", files={"secrets.json": {"TEST_KEY": "fixture"}}),
    Case("agent-invalid-secrets", "agent start conn --secrets-file @invalid.json",
         (), (), "", (), exit_code=1, options=(), files={"invalid.json": {"TEST_KEY": None}},
         stderr='{"error":{"code":"client_error","stage":"transport","message":"The local service client failed."}}\n'),
    Case("agent-acceptance-unknown", "agent start conn", (START_AGENT,), (AcceptanceUnknown("fixture"),),
         "", (), exit_code=125, stderr=_wire({
             "error": {"code": "acceptance_unknown", "stage": "transport", "message": "The mutation acceptance is unknown."},
             "acceptance": "unknown", "action": "target start", "cause": "fixture", "request_id": KEY[0],
             "owner_generation": KEY[1],
         }).decode() + "\n"),
    _one("agent-stop-keyed", "agent stop target --generation target-generation",
         Call("DELETE", "/v1/targets/target?owner_generation=target-generation", None, True, "lab-task-ref/1", KEY),
         TASK_REF, "lab-task-ref/1 task\n"),
    _one("agent-stop-unkeyed", "target stop target --generation target-generation",
         Call("DELETE", "/v1/targets/target?owner_generation=target-generation", None, True, "lab-task-ref/1", None),
         TASK_REF, "lab-task-ref/1 task\n", options=("--no-request-id",)),
    Case("agent-slot-held", "agent start conn", (START_AGENT, _get("/v1/targets?state=ready", "lab-owner-target-list/1")),
         (ApiError(SLOT_HELD), READY_TARGETS),
         "held by: t-old (generation g1) — lab agent stop t-old --generation g1 --wait\n", (),
         exit_code=1, stderr=_wire(SLOT_HELD).decode() + "\n", json_calls=(START_AGENT,),
         json_replies=(ApiError(SLOT_HELD),)),
    _one("check", f"check conn {DEFAULT_TARGET_ARGS}",
         _post("/v1/connections/conn/check", "lab-task-ref/1",
               {"schema": "lab-service-check-request/1", "target_ref": TARGET_REF}),
         TASK_REF, "lab-task-ref/1 task\n"),
    _one("conformance", f"conformance conn {DEFAULT_TARGET_ARGS}",
         _post("/v1/connections/conn/conformance", "lab-task-ref/1",
               {"schema": "lab-service-conformance-request/1", "target_ref": TARGET_REF, "runtime_policy": None}),
         TASK_REF, "lab-task-ref/1 task\n"),
    _one("conformance-policy", f"conformance conn {DEFAULT_TARGET_ARGS} --runtime-policy @runtime.json",
         _post("/v1/connections/conn/conformance", "lab-task-ref/1",
               {"schema": "lab-service-conformance-request/1", "target_ref": TARGET_REF,
                "runtime_policy": {"stop_grace_seconds": 1, "cleanup_wait_seconds": 2}}),
         TASK_REF, "lab-task-ref/1 task\n", files={"runtime.json": RUNTIME_POLICY}),
    _one("job-stop", "job stop task", _post("/v1/tasks/task/stop", "lab-owner-stop/1", {}),
         STOPPED, "lab-owner-stop/1 op stopping\n"),
    _one("operation-stop", "stop op", _post("/v1/operations/op/stop", "lab-owner-stop/1", {}),
         STOPPED, "lab-owner-stop/1 op stopping\n"),
]

START_BODY = {"schema": "lab-service-start-request/1", "prepared_digest": DIGEST,
              "target_ref": TARGET_REF, "secrets": {}}
RESUME_BODY = {"schema": "lab-service-resume-request/1", "target_ref": TARGET_REF, "secrets": {}}
OPERATION_TEXT = "lab-operation-status/8 op reserved process=not_launched cleanup=not_needed\n"
REFUSAL_READ = _get("/v1/operations/op/refusal", "lab-operation-refusal/1")
RESUME = f"resume op {DEFAULT_TARGET_ARGS}"
START = f"start {DIGEST} {DEFAULT_TARGET_ARGS}"
CLEANUP_BODY = {"schema": "lab-service-cleanup-request/1", "ref": {"kind": "operation", "id": "op"},
                "wait_seconds": None}
CLEANUP = _post("/v1/cleanup", "lab-task-ref/1", CLEANUP_BODY)
RELEASE = _post("/v1/releases", "lab-owner-release/1",
                {"schema": "lab-service-release-request/1", "ref": {"kind": "operation", "id": "op"}, "reason": "done"})

CASES += [
    _one("start-v1", START, _post("/v1/operations", "lab-operation-status/8", START_BODY),
         OPERATION, OPERATION_TEXT),
    _one("start-unlinked-options-ignored", f"{START} --baseline fresh --seed project",
         _post("/v1/operations", "lab-operation-status/8", START_BODY), OPERATION, OPERATION_TEXT),
    _one("start-linked-defaults", f"{START} --linked-from predecessor",
         _post("/v1/operations", "lab-operation-status/8", {**START_BODY, "schema": "lab-service-start-request/2",
               "linked_from": "predecessor", "baseline": "inherit", "seed": "champion"}),
         OPERATION, OPERATION_TEXT),
    _one("start-linked-options", f"{START} --linked-from predecessor --baseline fresh --seed project",
         _post("/v1/operations", "lab-operation-status/8", {**START_BODY, "schema": "lab-service-start-request/2",
               "linked_from": "predecessor", "baseline": "fresh", "seed": "project"}),
         OPERATION, OPERATION_TEXT),
    _one("start-import", f"{START} --baseline-from evaluation",
         _post("/v1/operations", "lab-operation-status/8", {**START_BODY, "schema": "lab-service-start-request/3",
               "linked_from": None, "baseline": "inherit", "seed": "champion", "baseline_from": "evaluation"}),
         OPERATION, OPERATION_TEXT),
    _one("resume-v1", RESUME, _post("/v1/operations/op/resume", "lab-operation-status/8", RESUME_BODY),
         OPERATION, OPERATION_TEXT),
    _one("resume-reason", f"{RESUME} --reason ''",
         _post("/v1/operations/op/resume", "lab-operation-status/8", {
             **RESUME_BODY, "schema": "lab-service-resume-request/2", "accept": [], "reason": "", "prepared_digest": None}),
         OPERATION, OPERATION_TEXT),
    _one("resume-prepared", f"{RESUME} --prepared {DIGEST}",
         _post("/v1/operations/op/resume", "lab-operation-status/8", {
             **RESUME_BODY, "schema": "lab-service-resume-request/2", "accept": [], "reason": None, "prepared_digest": DIGEST}),
         OPERATION, OPERATION_TEXT),
    _one("resume-explicit-effect", f"{RESUME} --accept run_identity_changed:{DIGEST} --reason checked --effect restart_round",
         _post("/v1/operations/op/resume", "lab-operation-status/8", {
             **RESUME_BODY, "schema": "lab-service-resume-request/2",
             "accept": [{"code": "run_identity_changed", "digest": DIGEST, "effect": "restart_round"}],
             "reason": "checked", "prepared_digest": None}),
         OPERATION, OPERATION_TEXT),
    Case("resume-default-effect", f"{RESUME} --accept run_identity_changed:{DIGEST} --reason checked",
         (REFUSAL_READ, HEALTH_CALL, _post("/v1/operations/op/resume", "lab-operation-status/8", {
             **RESUME_BODY, "schema": "lab-service-resume-request/2",
             "accept": [{"code": "run_identity_changed", "digest": DIGEST, "effect": None}],
             "reason": "checked", "prepared_digest": None}, ("r-fixed1", "gen-1"))),
         (RETURN_TO_TARGET, HEALTH, OPERATION), OPERATION_TEXT, (OPERATION,), options=(),
         stderr=f"authorization run_identity_changed:{DIGEST}: default means restart_round\n"),
    Case("resume-reason-missing", f"{RESUME} --accept run_identity_changed:{DIGEST}",
         (), (), "", (), exit_code=2, stderr="usage: --accept requires --reason\n", options=()),
    _one("exec-policy", f"exec-policy confirm - --expect {DIGEST} --author human --channel cli",
         _post("/v1/execution-policies", "lab-execution-policy-record/1",
               {"schema": "lab-service-policy-confirm-request/1", "policy": POLICY, "expected_digest": DIGEST,
                "author": "human", "channel": "cli", "note": None}),
         POLICY_RECORD, _wire(POLICY_RECORD).decode() + "\n", stdin=json.dumps(POLICY)),
    _one("policy-alias", f"policy confirm - --expect {DIGEST} --author human --channel local --note reviewed",
         _post("/v1/execution-policies", "lab-execution-policy-record/1",
               {"schema": "lab-service-policy-confirm-request/1", "policy": POLICY, "expected_digest": DIGEST,
                "author": "human", "channel": "local", "note": "reviewed"}),
         POLICY_RECORD, _wire(POLICY_RECORD).decode() + "\n", stdin=json.dumps(POLICY)),
    _one("delete-results", "delete-results op",
         Call("DELETE", "/v1/operations/op/results", None, True, "lab-task-ref/1", KEY), TASK_REF, "lab-task-ref/1 task\n"),
    _one("release", "release operation op --reason done", RELEASE, RELEASED, "lab-owner-release/1 op released\n"),
    _one("release-alias", "release-operation op --reason done", RELEASE, RELEASED, "lab-owner-release/1 op released\n"),
    _one("cleanup", "cleanup operation op", CLEANUP, TASK_REF, "lab-task-ref/1 task\n"),
    Case("cleanup-wait", "cleanup operation op --wait-seconds 9 --wait",
         (_post("/v1/cleanup", "lab-task-ref/1", {**CLEANUP_BODY, "wait_seconds": 9.0}),
          _get("/v1/tasks/task?owner_generation=generation", "lab-task-status/1")),
         (TASK_REF, TASK_STATUS), "lab-task-ref/1 task\nlab-task-status/1 task succeeded\n", (TASK_REF, TASK_STATUS)),
    Case("cleanup-wait-timeout", "cleanup operation op --wait",
         (CLEANUP, _get("/v1/tasks/task?owner_generation=generation", "lab-task-status/1", timeout=30.0)),
         (TASK_REF, RequestTimeout()), "lab-task-ref/1 task\n", (TASK_REF,), exit_code=124,
         stderr=_wire(WAIT_TIMEOUT).decode() + "\n", options=("--request-id", KEY[0], "--generation", KEY[1], "--timeout", "30")),
]

PROJECT_PREPARE_PATH = "/v1/projects/acq/prepared-executions"
PROJECT_PREPARE_SCHEMA = "lab-project-prepared-result/1"
PROJECT_PREPARE_TEXT = "lab-project-prepared-result/1\nlink=none\n"
PROJECT_SYNC_TEXT = (
    "lab-project-sync-check/1\nmerge, 1 commits, 2 files\n"
    "  commit aaaaaaaaaaaa new\n  A x.py\n  R o.py -> n.py\n"
    f"next: lab project sync acq --apply {SHA}\n"
)
PROJECT_START_COMMAND = f"project start acq --prepared {DIGEST} --target target"
PROJECT_START_BODY = {
    "schema": "lab-service-project-start-request/1", "implementation": None, "prepared_digest": DIGEST,
    "target_ref": {"schema": "lab-target-ref/1", "id": "target", "owner_generation": "target-generation"}, "secrets": {},
}
PROJECT_START_TEXT = "lab-project-start-result/5\nlink=none\n"
PREVIEW_TEXT = "lab-project-acceptance/1 None\n" + ACCEPTANCE_LINE
APPLIED_TEXT = "lab-project-acceptance/1 a-1\n" + ACCEPTANCE_LINE
ACCEPT_COMMAND = "project accept acq --reason typo"
PREVIEW_CALL = _post(ACCEPT_PATH, ACCEPT_SCHEMA, ACCEPTANCE_BODY, ("r-fixed1", "gen-1"))
ACCEPT_CALL = _post(ACCEPT_PATH, ACCEPT_SCHEMA, ACCEPTANCE_COMMIT, ("r-fixed2", "key-generation"))

CASES += [
    _one("project-bundle", "project bundle 'acq/%' --implementation agent",
         _post("/v1/projects/acq%2F%25/bundles", "lab-project-bundle-result/1",
               {"schema": "lab-service-project-bundle-request/1", "implementation": "agent"}),
         PROJECT_BUNDLE, f"lab-project-bundle-result/1\nlink=bundle:{DIGEST}\n"),
    _one("project-bundle-empty-project", "project bundle ''",
         _post("/v1/projects/bundles", "lab-project-bundle-result/1",
               {"schema": "lab-service-project-bundle-request/1", "implementation": None}),
         PROJECT_BUNDLE, f"lab-project-bundle-result/1\nlink=bundle:{DIGEST}\n"),
    _one("project-prepare-v1", "project prepare acq --request -",
         _post(PROJECT_PREPARE_PATH, PROJECT_PREPARE_SCHEMA, {
             "schema": "lab-service-project-prepare-request/1", "implementation": None, **CAMPAIGN}),
         PROJECT_PREPARED, PROJECT_PREPARE_TEXT, stdin=json.dumps(CAMPAIGN)),
    _one("project-prepare-v2", "project prepare acq --request -",
         _post(PROJECT_PREPARE_PATH, PROJECT_PREPARE_SCHEMA, {
             "schema": "lab-service-project-prepare-request/2", "implementation": None, **EVALUATION, "split": None}),
         PROJECT_PREPARED, PROJECT_PREPARE_TEXT, stdin=json.dumps({**EVALUATION, "split": None})),
    _one("project-prepare-v3", "project prepare acq --request - --implementation agent",
         _post(PROJECT_PREPARE_PATH, PROJECT_PREPARE_SCHEMA, {
             "schema": "lab-service-project-prepare-request/3", "implementation": "agent",
             **PROJECT_PREPARE_3_SETTINGS, **EVALUATION, "sample_retries": 1}),
         PROJECT_PREPARED, PROJECT_PREPARE_TEXT, stdin=json.dumps({**EVALUATION, "sample_retries": 1})),
    Case("project-prepare-invalid-file", "project prepare acq --request -",
         (), (), "", (), exit_code=2, stderr="usage: request file must not contain schema\n",
         options=(), stdin='{"schema":"provided"}'),
    _one("project-target", f"project target acq --bundle {DIGEST} --secrets-file -",
         _post("/v1/projects/acq/targets", "lab-project-target-result/1", {
             "schema": "lab-service-project-target-request/1", "implementation": None, "bundle_digest": DIGEST,
             "secrets": {"TEST_KEY": "fixture"}}),
         PROJECT_TARGET, "lab-project-target-result/1\nlink=none\n", stdin='{"TEST_KEY":"fixture"}'),
    Case("project-start-two-generations", PROJECT_START_COMMAND + " --secrets-file - --implementation agent",
         (HEALTH_CALL, HEALTH_CALL, _post("/v1/projects/acq/operations", "lab-project-start-result/5",
                                         {**PROJECT_START_BODY, "implementation": "agent", "secrets": {"TEST_KEY": "fixture"}},
                                         ("r-fixed1", "key-generation"))),
         ({**HEALTH, "owner_generation": "target-generation"}, {**HEALTH, "owner_generation": "key-generation"}, PROJECT_START),
         PROJECT_START_TEXT, (PROJECT_START,), options=(), stdin='{"TEST_KEY":"fixture"}'),
    Case("project-start-unkeyed", PROJECT_START_COMMAND,
         (HEALTH_CALL, _post("/v1/projects/acq/operations", "lab-project-start-result/5", PROJECT_START_BODY, None)),
         ({**HEALTH, "owner_generation": "target-generation"}, PROJECT_START),
         PROJECT_START_TEXT, (PROJECT_START,), options=("--no-request-id",)),
    Case("project-start-invalid-secrets", PROJECT_START_COMMAND + " --secrets-file -",
         (HEALTH_CALL,), (HEALTH,), "", (), exit_code=1, options=(), stdin="not JSON",
         stderr='{"error":{"code":"client_error","stage":"transport","message":"The local service client failed."}}\n'),
    _one("project-confirm", f"project confirm acq --claim spec.confirmed --path 'a/b' --digest sha256:{DIGEST} "
         "--link task:k1 --link operation:op --author human --note reviewed",
         _post("/v1/projects/acq/confirmations", "lab-project-confirmation/1", {
             "schema": "lab-service-confirm-request/1", "implementation": None, "claim": "spec.confirmed",
             "attests": {"path": "a/b", "digest": "sha256:" + DIGEST},
             "linked": [{"kind": "task", "ref": "k1"}, {"kind": "operation", "ref": "op"}],
             "channel": "cli", "author": "human", "note": "reviewed"}),
         CONFIRMATION, "lab-project-confirmation/1 c-1\n"),
    _one("project-revoke-confirmation", "project revoke-confirmation acq 'c/%' --reason typo",
         _post("/v1/projects/acq/confirmations/c%2F%25/revoke", "lab-project-confirmation/1", REVOCATION_BODY),
         CONFIRMATION, "lab-project-confirmation/1 c-1\n"),
    _one("project-revoke-acceptance", "project revoke-acceptance acq 'a/%' --reason typo",
         _post("/v1/projects/acq/acceptances/a%2F%25/revoke", "lab-project-acceptance/1", REVOCATION_BODY),
         ACCEPTANCE, APPLIED_TEXT),
    _one("project-accept-yes", f"{ACCEPT_COMMAND} --yes --across-pairs --only spec.confirmed --implementation agent",
         _post(ACCEPT_PATH, ACCEPT_SCHEMA, {**ACCEPTANCE_BODY, "implementation": "agent",
               "across_pairs": True, "only": ["spec.confirmed"], "dry_run": False}), ACCEPTANCE, APPLIED_TEXT),
    Case("project-accept-interactive-yes", ACCEPT_COMMAND, (HEALTH_CALL, PREVIEW_CALL, HEALTH_CALL, ACCEPT_CALL),
         (HEALTH, PREVIEW, {**HEALTH, "owner_generation": "key-generation"}, ACCEPTANCE),
         PREVIEW_TEXT + APPLIED_TEXT, (PREVIEW, ACCEPTANCE), stderr="apply? [y/N] ", options=(), stdin="y\n"),
    Case("project-accept-interactive-no", ACCEPT_COMMAND, (HEALTH_CALL, PREVIEW_CALL), (HEALTH, PREVIEW),
         PREVIEW_TEXT, (PREVIEW,), stderr="apply? [y/N] ", options=(), stdin="n\n"),
    Case("project-accept-explicit-key-without-yes", ACCEPT_COMMAND, (), (), "", (), exit_code=2,
         stderr="usage: an explicit --request-id needs --yes: the preview and the write are two different requests\n"),
    Case("project-accept-facts-changed", ACCEPT_COMMAND, (HEALTH_CALL, PREVIEW_CALL, HEALTH_CALL, ACCEPT_CALL),
         (HEALTH, PREVIEW, {**HEALTH, "owner_generation": "key-generation"}, ApiError(DIGEST_MISMATCH)),
         PREVIEW_TEXT, (PREVIEW,), exit_code=1, options=(), stdin="y\n", stderr=(
             "apply? [y/N] " + _wire(DIGEST_MISMATCH).decode()
             + "\nhint: the facts changed since the preview; run lab project accept again\n")),
    _one("project-sync-check-default", "project sync acq",
         _post("/v1/projects/acq/sync/check", "lab-project-sync-check/1",
               {"schema": "lab-service-project-sync-check-request/1", "rev": None}),
         SYNC_CHECK, PROJECT_SYNC_TEXT),
    _one("project-sync-check-revision", "project sync acq --rev develop",
         _post("/v1/projects/acq/sync/check", "lab-project-sync-check/1",
               {"schema": "lab-service-project-sync-check-request/1", "rev": "develop"}),
         SYNC_CHECK, PROJECT_SYNC_TEXT),
    _one("project-sync-apply", f"project sync acq --apply {SHA}",
         _post("/v1/projects/acq/sync", "lab-project-sync-result/1",
               {"schema": "lab-service-project-sync-request/1", "upstream": SHA}),
         SYNC_RESULT, "lab-project-sync-result/1\nfast_forward aaaaaaaaaaaa → head aaaaaaaaaaaa\n"),
]


class RecordingClient(FakeClient):
    def __init__(self, replies):
        super().__init__(replies)
        self.timeouts = []

    def request_json(self, *args, timeout=None, **kwargs):
        self.timeouts.append(timeout)
        return super().request_json(*args, **kwargs)


@pytest.mark.parametrize("machine", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_mutation_matches_the_cli_on_main(case, machine, monkeypatch, tmp_path):
    nonces = iter(("fixed1", "fixed2"))
    monkeypatch.setattr(cli_parser.secrets, "token_urlsafe", lambda _: next(nonces))
    monkeypatch.setattr(cli_parser.time, "monotonic", lambda: 100.0)
    argv = ["--socket", "/tmp/lab.sock", *case.options]
    if machine:
        argv.append("--json")
    for arg in shlex.split(case.command):
        if arg.startswith("@"):
            path = tmp_path / arg[1:]
            path.write_text(json.dumps(case.files[path.name]), encoding="utf-8")
            arg = str(path)
        argv.append(arg)
    client = RecordingClient(case.json_replies if machine and case.json_replies is not None else case.replies)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout=stdout, stderr=stderr, stdin=io.StringIO(case.stdin), client_factory=lambda **_: client)

    if machine:
        expected_stdout = b"".join(_wire(reply) + b"\n" for reply in case.json_stdout)
    else:
        expected_stdout = case.text_stdout.encode("utf-8")
    assert (code, stdout.getvalue().encode("utf-8"), stderr.getvalue().encode("utf-8")) == (
        case.exit_code, expected_stdout, case.stderr.encode("utf-8"),
    )
    expected = case.json_calls if machine and case.json_calls is not None else case.calls
    actual = [Call(*call, key, timeout) for call, key, timeout in zip(client.calls, client.keys, client.timeouts, strict=True)]
    assert actual == list(expected)
    assert [_wire_body(call.body) for call in actual] == [_wire_body(call.body) for call in expected]
