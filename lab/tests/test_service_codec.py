from pathlib import Path

import pytest

from evolution_lab.owner import (
    CleanupFacts,
    ExecutorIdentity,
    ExportResult,
    OperationStatus,
    ProcessFacts,
    TargetRef,
    TaskResult,
    TaskStatus,
)
from evolution_lab.service.codecs import (
    decode_bundle_request,
    decode_bind_request,
    decode_check_request,
    decode_cleanup_request,
    decode_conformance_request,
    decode_digest,
    decode_empty_request,
    decode_http_json_object,
    decode_identifier,
    decode_measurement_id,
    decode_json_object,
    decode_operation_request,
    decode_prepare_request,
    decode_resource_ref,
    decode_release_request,
    decode_runtime_policy,
    decode_secret_map,
    decode_target_ref,
    decode_target_query,
    decode_task_ref,
    decode_task_query,
    decode_target_start_request,
    serialize_public,
)
from evolution_lab.service.errors import ServiceError, error_response, translate_exception


DIGEST = "a" * 64

EVALUATION_REQUEST = {
    "schema": "lab-service-prepare-request/3",
    "kind": "evaluation",
    "bundle_digest": DIGEST,
    "selection": {"count": 1},
    "repeat_seeds": [1, None],
    "evaluation_set_id": None,
    "max_turns": None,
    "timeout_seconds": None,
    "metric_plan": None,
    "runtime_policy": {"stop_grace_seconds": 1, "cleanup_wait_seconds": 2},
    "max_failed_turns": None,
    "call_retries": None,
    "split": None,
}

EVALUATION_REQUEST_2 = {**{k: v for k, v in EVALUATION_REQUEST.items() if k != "split"},
                        "schema": "lab-service-prepare-request/2"}


def bundle_payload():
    return {
        "schema": "lab-service-bundle-request/1",
        "source": {
            "kind": "campaign",
            "manifest_path": "/srv/input/manifest.json",
            "launch_path": "/srv/input/launch.json",
        },
        "environments": [{
            "executable": "/srv/venv/bin/python",
            "wheel_paths": ["/srv/wheels/core.whl"],
            "runtime_files": [],
        }],
        "env": {"LANG": "C"},
        "secret_names": ["TOKEN"],
        "roots": [{"alias": "input", "source_path": "/srv/input"}],
        "path_bindings": [{
            "document": "/srv/input/manifest.json",
            "pointer": "/target/policy",
            "root_alias": "input",
            "relative_path": "policy.json",
        }],
    }


def test_json_body_rejects_duplicates_nonfinite_and_non_objects():
    for raw in (b'{"a": 1, "a": 2}', b'{"a": NaN}', b'[]'):
        with pytest.raises(ServiceError) as error:
            decode_json_object(raw)
        assert error.value.code == "invalid_request"
        assert error.value.stage == "transport"


def test_http_json_body_requires_media_and_enforces_size_limits_before_decode():
    with pytest.raises(ServiceError) as media:
        decode_http_json_object(b"{}", content_type="text/plain")
    assert media.value.code == "unsupported_media_type"
    with pytest.raises(ServiceError) as missing_media:
        decode_http_json_object(b"{}", content_type=None)
    assert missing_media.value.code == "unsupported_media_type"
    assert decode_json_object(b"{}") == {}

    with pytest.raises(ServiceError) as oversized:
        decode_http_json_object(b"{" + b" " * (1024 * 1024) + b"}", content_type="application/json")
    assert oversized.value.code == "request_too_large"


def test_bundle_request_is_closed_and_constructs_public_local_path_inputs():
    request = decode_bundle_request(bundle_payload())

    assert request.source.manifest_path == Path("/srv/input/manifest.json")
    assert request.environments[0].wheel_paths == (Path("/srv/wheels/core.whl"),)
    assert request.path_bindings[0].relative_path == "policy.json"

    payload = bundle_payload()
    payload["source"]["extra"] = True
    with pytest.raises(ServiceError) as error:
        decode_bundle_request(payload)
    assert error.value.code == "invalid_request"


def test_prepare_variants_and_refs_have_exact_closed_shapes():
    campaign = decode_prepare_request({
        "schema": "lab-service-prepare-request/3",
        "kind": "campaign",
        "bundle_digest": DIGEST,
        "stop_grace_seconds": None,
        "cleanup_wait_seconds": 2.5,
    })
    assert campaign.kind == "campaign"
    assert campaign.kwargs == {"stop_grace_seconds": None, "cleanup_wait_seconds": 2.5}

    evaluation = decode_prepare_request(EVALUATION_REQUEST)
    assert evaluation.kind == "evaluation"
    assert evaluation.kwargs["runtime_policy"].to_payload()["cleanup_wait_seconds"] == 2
    assert evaluation.kwargs["max_failed_turns"] is None
    assert evaluation.kwargs["call_retries"] is None

    target = decode_target_ref({"schema": "lab-target-ref/1", "id": "target-1", "owner_generation": "g1"})
    task = decode_task_ref({"schema": "lab-task-ref/1", "id": "task-1", "owner_generation": "g1"})
    resource = decode_resource_ref({"kind": "operation", "id": "op-1"})
    assert (target.id, task.id, resource.kind) == ("target-1", "task-1", "operation")

    with pytest.raises(ServiceError):
        decode_target_ref({"schema": "lab-target-ref/1", "id": "target-1", "owner_generation": "g1", "extra": True})
    with pytest.raises(ServiceError) as invalid_id:
        decode_resource_ref({"kind": "operation", "id": "../private"})
    assert invalid_id.value.code == "invalid_request"


@pytest.mark.parametrize("budget", [
    {"max_failed_turns": 3, "call_retries": {"max_attempts": 4, "turn_max_attempts": 2}},
    {"max_failed_turns": None, "call_retries": "off"},
])
def test_evaluation_prepare_request_carries_the_budget(budget):
    from evolution_lab.service.codecs import decode_prepare_request
    value = decode_prepare_request({**EVALUATION_REQUEST, **budget})
    assert value.kwargs["max_failed_turns"] == budget["max_failed_turns"]
    assert value.kwargs["call_retries"] == budget["call_retries"]


@pytest.mark.parametrize("budget", [
    {"max_failed_turns": 0}, {"max_failed_turns": True},
    {"call_retries": "on"}, {"call_retries": {"max_attempts": 0, "turn_max_attempts": 1}},
])
def test_evaluation_prepare_request_rejects_an_invalid_budget(budget):
    from evolution_lab.service.codecs import decode_prepare_request
    with pytest.raises(ServiceError) as error:
        decode_prepare_request({**EVALUATION_REQUEST, **budget})
    assert error.value.code == "invalid_request"


def test_prepare_request_v1_is_unsupported():
    from evolution_lab.service.codecs import decode_prepare_request
    with pytest.raises(ServiceError) as error:
        decode_prepare_request({**EVALUATION_REQUEST, "schema": "lab-service-prepare-request/1"})
    assert error.value.code == "unsupported_schema"
    for schema in (["lab-service-prepare-request/3"], {"v": 3}, None):  # unhashable or absent: a refusal, not a crash
        with pytest.raises(ServiceError) as error:
            decode_prepare_request({**EVALUATION_REQUEST, "schema": schema})
        assert error.value.code == "unsupported_schema"


def test_prepare_request_2_is_still_accepted_and_means_split_null():
    """User decision 2026-10-09: /2 stays accepted; it is /3 with split null (train)."""
    from evolution_lab.service.codecs import decode_prepare_request
    old, new = decode_prepare_request(EVALUATION_REQUEST_2), decode_prepare_request(EVALUATION_REQUEST)
    assert old.kind == new.kind == "evaluation" and old.kwargs["split"] is None
    assert {k: v for k, v in old.kwargs.items() if k != "runtime_policy"} == \
        {k: v for k, v in new.kwargs.items() if k != "runtime_policy"}
    assert old.kwargs["runtime_policy"].to_payload() == new.kwargs["runtime_policy"].to_payload()
    with pytest.raises(ServiceError):
        decode_prepare_request({**EVALUATION_REQUEST_2, "split": "test"})  # /2 has no split key
    campaign = {"schema": "lab-service-prepare-request/2", "kind": "campaign", "bundle_digest": DIGEST,
                "stop_grace_seconds": None, "cleanup_wait_seconds": None}
    assert decode_prepare_request(campaign).kwargs == decode_prepare_request(
        dict(campaign, schema="lab-service-prepare-request/3")).kwargs


def test_prepare_request_3_carries_the_split():
    from evolution_lab.service.codecs import decode_prepare_request
    assert decode_prepare_request({**EVALUATION_REQUEST, "split": "test"}).kwargs["split"] == "test"
    assert decode_prepare_request(EVALUATION_REQUEST).kwargs["split"] is None
    for bad in ("holdout", 1, True):
        with pytest.raises(ServiceError):
            decode_prepare_request({**EVALUATION_REQUEST, "split": bad})
    with pytest.raises(ServiceError):
        decode_prepare_request({k: v for k, v in EVALUATION_REQUEST.items() if k != "split"})
    campaign = {"schema": "lab-service-prepare-request/3", "kind": "campaign", "bundle_digest": DIGEST,
                "stop_grace_seconds": None, "cleanup_wait_seconds": None}
    with pytest.raises(ServiceError):
        decode_prepare_request({**campaign, "split": "train"})  # a campaign runs on train: core decides


def test_error_mapping_is_fixed_and_preserves_public_owner_failure():
    busy = translate_exception(__import__("evolution_lab.owner", fromlist=["OwnerError"]).OwnerError("resource_busy", "before_start"))
    assert error_response(busy) == {
        "status": 409,
        "body": {"error": {"code": "resource_busy", "stage": "before_start", "message": "The requested owner resource is busy."}},
    }
    assert error_response(ServiceError("request_too_large"))["status"] == 413
    assert error_response(ServiceError("unsupported_media_type"))["status"] == 415
    assert error_response(ServiceError("service_stopping"))["status"] == 503
    assert error_response(ServiceError("history_unavailable"))["status"] == 409
    assert error_response(ServiceError("socket_path_exists"))["status"] == 409
    assert error_response(translate_exception(OSError("private path")))["body"] == {
        "error": {"code": "io_error", "stage": "transport", "message": "The service storage operation failed."}
    }


def test_public_serialization_uses_to_payload_without_private_fields():
    executor = ExecutorIdentity(DIGEST, DIGEST, DIGEST, {"worker": DIGEST})
    status = OperationStatus(
        id="op-1", owner_generation="g1", prepared_digest=DIGEST,
        target_ref=TargetRef("target-1", "g1"), control_state="finished",
        process=ProcessFacts(True, 0, True, False),
        target_cleanup=CleanupFacts("confirmed", True), result=None, failure=None,
        results_state="deleted", executor=executor,
        retained_summary={
            "campaign_outcome": {"outcome": "", "reason": "", "continuation": {"kind": None, "reason": None}},
            "dispositions": [{"scope_id": "scope-1", "disposition": "normal", "reason": None}],
        },
    )
    task = TaskStatus(
        "task-1", "g1", "conformance", None, "succeeded",
        TaskResult("conformance", {"core_report": None, "cleanup": {"state": "confirmed", "complete": True}}),
        None,
    )

    assert serialize_public(status) == status.to_payload()
    assert "dispatch_possible" not in serialize_public(status)
    assert serialize_public(status)["retained_summary"] == status.to_payload()["retained_summary"]
    assert serialize_public(task)["result"]["payload"]["core_report"] is None
    assert serialize_public(ExportResult("op-1", "no_change", None, None, None)) == {
        "operation_id": "op-1", "state": "no_change", "export_id": None,
        "sha256": None, "manifest": None,
    }
    with pytest.raises(ServiceError):
        serialize_public({"private": "value"})


def test_bind_and_cli_value_helpers_keep_action_schema_and_closed_values_public():
    assert decode_bind_request({"schema": "lab-service-bind-request/1", "bundle_digest": DIGEST}) == DIGEST
    assert decode_secret_map({"TOKEN": "value"}) == {"TOKEN": "value"}
    assert decode_runtime_policy({"stop_grace_seconds": 1, "cleanup_wait_seconds": 2}).to_payload() == {
        "stop_grace_seconds": 1, "cleanup_wait_seconds": 2,
    }
    assert decode_empty_request({}) is None
    with pytest.raises(ServiceError):
        decode_bind_request({"schema": "lab-service-bind-request/1", "bundle_digest": DIGEST, "extra": True})
    with pytest.raises(ServiceError):
        decode_empty_request({"schema": "lab-service-stop-request/1"})


def test_mutation_requests_have_exact_actions_and_only_public_values():
    target_ref = {"schema": "lab-target-ref/1", "id": "target-1", "owner_generation": "g1"}
    assert decode_target_start_request({
        "schema": "lab-service-target-start-request/1", "secrets": {},
    }).secrets == {}
    assert decode_check_request({
        "schema": "lab-service-check-request/1", "target_ref": target_ref,
    }).target_ref.id == "target-1"
    assert decode_conformance_request({
        "schema": "lab-service-conformance-request/1", "target_ref": target_ref, "runtime_policy": None,
    }).runtime_policy is None
    assert decode_operation_request({
        "schema": "lab-service-start-request/1", "prepared_digest": DIGEST,
        "target_ref": target_ref, "secrets": {"TOKEN": "value"},
    }).prepared_digest == DIGEST
    assert decode_cleanup_request({
        "schema": "lab-service-cleanup-request/1", "ref": {"kind": "task", "id": "task-1"},
        "wait_seconds": None,
    }).ref.kind == "task"
    assert decode_release_request({
        "schema": "lab-service-release-request/1", "ref": {"kind": "target", "id": "target-1"},
        "reason": "operator-cleaned",
    }).reason == "operator-cleaned"
    with pytest.raises(ServiceError):
        decode_operation_request({
            "schema": "lab-service-start-request/1", "prepared_digest": DIGEST,
            "target_ref": target_ref, "secrets": {}, "extra": True,
        })


def test_resume_request_is_closed():
    from evolution_lab.service.codecs import decode_resume_request
    ref = {"schema": "lab-target-ref/1", "id": "a" * 32, "owner_generation": "b" * 32}
    value = decode_resume_request({"schema": "lab-service-resume-request/1",
                                   "target_ref": ref, "secrets": {}})
    assert value.target_ref.id == "a" * 32 and value.secrets == {}
    for bad in ({"schema": "lab-service-resume-request/1", "target_ref": ref},
                {"schema": "lab-service-resume-request/1", "target_ref": ref, "secrets": {}, "prepared_digest": "c" * 64},
                {"schema": "lab-service-start-request/1", "target_ref": ref, "secrets": {}}):
        with pytest.raises(Exception):
            decode_resume_request(bad)


def test_route_query_helpers_keep_generation_and_ids_out_of_private_codecs():
    assert decode_digest(DIGEST) == DIGEST
    assert decode_identifier("op-1") == "op-1"
    assert decode_target_query("target-1", "g1").to_payload() == {
        "schema": "lab-target-ref/1", "id": "target-1", "owner_generation": "g1",
    }
    assert decode_task_query("task-1", "g1").to_payload() == {
        "schema": "lab-task-ref/1", "id": "task-1", "owner_generation": "g1",
    }
    with pytest.raises(ServiceError):
        decode_task_query("task/1", "g1")


def test_large_integer_time_inputs_are_fixed_transport_refusals():
    huge = 10**10000
    raw_json = decode_json_object(b'{"number": ' + b"9" * 1000 + b"}")
    assert raw_json["number"] == int(b"9" * 1000)
    with pytest.raises(ServiceError) as policy:
        decode_runtime_policy({"stop_grace_seconds": huge, "cleanup_wait_seconds": 1})
    assert policy.value.code == "invalid_request"
    with pytest.raises(ServiceError) as campaign:
        decode_prepare_request({
            "schema": "lab-service-prepare-request/3", "kind": "campaign",
            "bundle_digest": DIGEST, "stop_grace_seconds": huge, "cleanup_wait_seconds": None,
        })
    assert campaign.value.code == "invalid_request"
    with pytest.raises(ServiceError) as cleanup:
        decode_cleanup_request({
            "schema": "lab-service-cleanup-request/1", "ref": {"kind": "task", "id": "task-1"},
            "wait_seconds": huge,
        })
    assert cleanup.value.code == "invalid_request"


def _bundle(schema, environments):
    return {"schema": schema, "source": {"kind": "evaluation", "input_path": "/abs/i.json",
            "launch_path": "/abs/l.json"}, "environments": environments, "env": {},
            "secret_names": [], "roots": [], "path_bindings": []}


EXPLICIT = {"executable": "/abs/python", "wheel_paths": ["/abs/a.whl"], "runtime_files": []}


def test_bundle_request_2_accepts_service_marker_only_first():
    from evolution_lab.service.codecs import SERVICE_ENVIRONMENT, decode_bundle_request
    value = decode_bundle_request(_bundle("lab-service-bundle-request/2", [{"kind": "service"}, EXPLICIT]))
    assert value.environments[0] is SERVICE_ENVIRONMENT
    assert value.environments[1].executable.as_posix() == "/abs/python"


@pytest.mark.parametrize(("schema", "environments", "code"), [
    ("lab-service-bundle-request/1", [{"kind": "service"}], "invalid_request"),
    ("lab-service-bundle-request/2", [EXPLICIT, {"kind": "service"}], "invalid_request"),
    ("lab-service-bundle-request/2", [{"kind": "service"}, {"kind": "service"}], "invalid_request"),
    ("lab-service-bundle-request/2", [{"kind": "service", "executable": "/abs/python"}], "invalid_request"),
    ("lab-service-bundle-request/3", [EXPLICIT], "unsupported_schema"),
])
def test_bundle_request_marker_refusals(schema, environments, code):
    from evolution_lab.service.codecs import decode_bundle_request
    from evolution_lab.service.errors import ServiceError
    with pytest.raises(ServiceError) as caught:
        decode_bundle_request(_bundle(schema, environments))
    assert caught.value.code == code


def test_bundle_request_1_and_2_decode_explicit_form_identically():
    from evolution_lab.service.codecs import decode_bundle_request
    assert decode_bundle_request(_bundle("lab-service-bundle-request/1", [EXPLICIT])) == \
        decode_bundle_request(_bundle("lab-service-bundle-request/2", [EXPLICIT]))


def test_missing_fields_are_named():
    from evolution_lab.service.errors import ServiceError
    base = {"schema": "lab-service-prepare-request/3", "kind": "campaign", "bundle_digest": "a" * 64}
    with pytest.raises(ServiceError) as caught:
        decode_prepare_request(base)
    payload = caught.value.to_payload()
    assert set(payload) == {"code", "stage", "message"} and payload["code"] == "invalid_request"
    assert "missing: cleanup_wait_seconds, stop_grace_seconds" in payload["message"]


def test_extra_fields_are_counted_not_echoed():
    from evolution_lab.service.errors import ServiceError
    base = {"schema": "lab-service-prepare-request/3", "kind": "campaign", "bundle_digest": "a" * 64}
    with pytest.raises(ServiceError) as caught:
        decode_prepare_request(dict(base, stop_grace_seconds=None, cleanup_wait_seconds=None, secret_key_x=1))
    message = caught.value.to_payload()["message"]
    assert "unexpected: 1 field(s)" in message and "secret_key_x" not in message


@pytest.mark.parametrize("value", ["m-r01-champion", "m-r01-champion@e1", "m-r02-a01@e12"])
def test_measurement_id_accepts_epoch(value):
    assert decode_measurement_id(value) == value


@pytest.mark.parametrize("value", ["m@e0", "m@e01", "m@x1", "@e1", "m@e1@e2", "m-r01@e1/../x", "m@e1%2F", "m%40e1",
                                   "", "..", None, 1])
def test_measurement_id_rejects_malformed_epoch(value):
    with pytest.raises(ServiceError):
        decode_measurement_id(value)


@pytest.mark.parametrize("value", ["op@e1", "op-1@e1", "@e1"])
def test_other_identifiers_still_reject_at_sign(value):
    with pytest.raises(ServiceError):
        decode_identifier(value)


def test_target_slot_held_is_a_fixed_409():
    from evolution_lab.owner.models import Failure, OwnerError
    from evolution_lab.service.errors import _STATUS, error_response, translate_exception

    assert Failure.make("target_slot_held", "before_start").message == (
        "Another target of this Owner is ready; stop it before starting a target of a different Bundle.")
    assert _STATUS["target_slot_held"] == 409
    answer = error_response(translate_exception(OwnerError("target_slot_held", "before_start")))
    assert answer["status"] == 409 and answer["body"]["error"]["code"] == "target_slot_held"


def test_suspension_result_serializes_without_changing_existing_forms():
    from evolution_lab.owner import SuspensionEvidenceResult
    result = SuspensionEvidenceResult("op1", "available", [], None)
    assert serialize_public(result) == {
        "schema": "lab-suspension-evidence/1", "operation_id": "op1", "state": "available",
        "source": "core_raw", "reason": None, "suspensions": []}


def test_v3_start_request_names_an_evaluation():
    value = {
        "schema": "lab-service-start-request/3", "prepared_digest": DIGEST,
        "target_ref": TargetRef("target", "generation").to_payload(), "secrets": {},
        "linked_from": None, "baseline": "inherit", "seed": "champion",
        "baseline_from": "evaluation",
    }
    request = decode_operation_request(value)
    assert request.baseline_from == "evaluation"
    assert request.prepared_digest == DIGEST
    assert request.linked_from is None and request.target_ref.id == "target"
    assert decode_operation_request({**value, "baseline_from": None}).baseline_from is None
    for invalid in ({**value, "linked_from": "predecessor"}, {**value, "baseline_from": "../evaluation"},
                    {**value, "extra": True}, {**value, "schema": "lab-service-start-request/2"},
                    {**value, "baseline": "fresh"}, {**value, "seed": "project"}):
        with pytest.raises(ServiceError):
            decode_operation_request(invalid)
