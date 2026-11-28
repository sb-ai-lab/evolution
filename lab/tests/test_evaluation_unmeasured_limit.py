"""PDR-0031: Lab's stop threshold is configurable without changing old plans."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

from evolution_lab.preparation import PreparationError, PreparationStore
from evolution_lab.preparation.models import EvaluationPlan, PreparedExecutionDescriptor
from evolution_lab.preparation.storage import canonical_json_bytes
from evolution_lab.service.codecs import decode_prepare_request
from evolution_lab.service.errors import ServiceError
from test_preparation_evaluation import _evaluation_bundle
from test_service_codec import EVALUATION_REQUEST


FIELD = "max_consecutive_unmeasured"
SETTINGS = dict(sample_retries=None, min_coverage=None, concurrency=None, scorer_concurrency=None)


@pytest.mark.parametrize("value, effective", [(None, 3), (1, 1), (3, 3), (7, 7), ("off", "off")])
def test_api_plan_roundtrip_and_digest(tmp_path, monkeypatch, value, effective):
    bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
    store = PreparationStore(state)
    original = store.prepare_evaluation(bundle.digest, selection={"all": True})
    prepared = store.prepare_evaluation(bundle.digest, selection={"all": True}, **{FIELD: value})
    assert prepared.evaluation.max_consecutive_unmeasured == effective
    assert store.get_prepared(prepared.digest).evaluation.max_consecutive_unmeasured == effective
    assert (prepared.digest == original.digest) is (effective == 3)
    payload = prepared.evaluation.to_payload()
    if effective == 3:
        assert FIELD not in payload
    else:
        assert payload[FIELD] == effective


@pytest.mark.parametrize("value", [0, -1, 1.0, True, False, "3", "PRIVATE-USER-VALUE", [], {}])
def test_invalid_api_value_names_only_the_field(tmp_path, monkeypatch, value):
    bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
    with pytest.raises(PreparationError) as caught:
        PreparationStore(state).prepare_evaluation(bundle.digest, selection={"all": True}, **{FIELD: value})
    assert caught.value.code == "invalid_request"
    assert FIELD in str(caught.value) and "PRIVATE-USER-VALUE" not in str(caught.value)


@pytest.mark.parametrize("schema", ["lab-evaluation-input/1", "lab-evaluation-input/2", "lab-evaluation-input/3"])
def test_input_versions_keep_historical_threshold(tmp_path, monkeypatch, schema):
    bundle, state = _evaluation_bundle(tmp_path, monkeypatch, schema=schema)
    plan = PreparationStore(state).prepare_evaluation(bundle.digest, selection={"all": True}).evaluation
    assert plan.max_consecutive_unmeasured == 3


@pytest.mark.parametrize("value, effective", [(None, 3), (2, 2), ("off", "off")])
def test_input3_defaults_and_api_override(tmp_path, monkeypatch, value, effective):
    bundle, state = _evaluation_bundle(tmp_path, monkeypatch, schema="lab-evaluation-input/3",
                                      extra_defaults={FIELD: value})
    store = PreparationStore(state)
    assert store.prepare_evaluation(bundle.digest, selection={"all": True}).evaluation.max_consecutive_unmeasured == effective
    assert store.prepare_evaluation(bundle.digest, selection={"all": True},
                                    **{FIELD: None}).evaluation.max_consecutive_unmeasured == effective
    assert store.prepare_evaluation(bundle.digest, selection={"all": True},
                                    **{FIELD: 5}).evaluation.max_consecutive_unmeasured == 5


@pytest.mark.parametrize("value", [0, -1, 2.0, True, "PRIVATE-USER-VALUE"])
def test_input3_invalid_default_is_safe(tmp_path, monkeypatch, value):
    with pytest.raises(PreparationError) as caught:
        _evaluation_bundle(tmp_path, monkeypatch, schema="lab-evaluation-input/3", extra_defaults={FIELD: value})
    assert FIELD in str(caught.value) and "PRIVATE-USER-VALUE" not in str(caught.value)


@pytest.mark.parametrize("schema", ["lab-evaluation-input/1", "lab-evaluation-input/2"])
def test_old_input_versions_refuse_new_key(tmp_path, monkeypatch, schema):
    with pytest.raises(PreparationError):
        _evaluation_bundle(tmp_path, monkeypatch, schema=schema, extra_defaults={FIELD: 2})


@pytest.mark.parametrize("value", [None, 1, 4, "off"])
def test_request5_carries_the_new_setting(value):
    request = {**EVALUATION_REQUEST, **SETTINGS, "schema": "lab-service-prepare-request/5", FIELD: value}
    assert decode_prepare_request(request).kwargs[FIELD] == value


@pytest.mark.parametrize("value", [0, -1, 1.0, True, "PRIVATE-USER-VALUE", [], {}])
def test_request5_invalid_value_is_safe_and_cli_decodes_failure(value):
    from evolution_lab.cli.responses import error
    with pytest.raises(ServiceError) as caught:
        decode_prepare_request({**EVALUATION_REQUEST, **SETTINGS,
                                "schema": "lab-service-prepare-request/5", FIELD: value})
    assert caught.value.code == "invalid_request"
    assert FIELD in caught.value.message and "PRIVATE-USER-VALUE" not in caught.value.message
    assert error({"error": caught.value.to_payload()})
    assert not error({"error": ServiceError("invalid_request", detail="PRIVATE-USER-VALUE").to_payload()})


@pytest.mark.parametrize("version", [2, 3, 4])
def test_historical_requests_stay_closed_and_keep_values(version):
    request = {**EVALUATION_REQUEST, "schema": f"lab-service-prepare-request/{version}"}
    if version == 2:
        request.pop("split")
    if version == 4:
        request.update(sample_retries=0, min_coverage=1, concurrency=1, scorer_concurrency=2)
    parsed = decode_prepare_request(request)
    assert FIELD not in parsed.kwargs
    if version == 4:
        assert parsed.kwargs["sample_retries"] == 0 and parsed.kwargs["min_coverage"] == 1
    with pytest.raises(ServiceError):
        decode_prepare_request({**request, FIELD: 2})


def test_cli_selects_request5_and_fills_nullable_settings():
    from evolution_lab.cli.parser import _request_file, PREPARE_REQUEST_PREFIX
    request = {key: value for key, value in EVALUATION_REQUEST.items() if key != "schema"}
    decoded = _request_file("-", PREPARE_REQUEST_PREFIX, io.StringIO(json.dumps({**request, FIELD: "off"})))
    assert decoded["schema"] == "lab-service-prepare-request/5"
    assert decode_prepare_request(decoded).kwargs[FIELD] == "off"
    assert all(decoded[name] is None for name in SETTINGS)


def test_historical_plan_retains_default_and_exact_bytes():
    fixture = json.loads((Path(__file__).parent / "fixtures/preparation/dev59-evaluation-descriptor.json").read_text())
    descriptor = PreparedExecutionDescriptor.from_payload(fixture["descriptor"])
    assert descriptor.evaluation.max_consecutive_unmeasured == 3
    assert descriptor.digest == fixture["prepared_digest"]
    assert canonical_json_bytes(descriptor.to_payload()).decode() == fixture["canonical_descriptor"]


@pytest.mark.parametrize("value", [0, -1, 1.0, True, "PRIVATE-USER-VALUE"])
def test_stored_plan_invalid_value_names_field_at_read_stage(value):
    fixture = json.loads((Path(__file__).parent / "fixtures/preparation/dev59-evaluation-descriptor.json").read_text())
    plan = fixture["descriptor"]["evaluation"]
    with pytest.raises(PreparationError) as caught:
        EvaluationPlan.from_payload({**plan, FIELD: value}, "read")
    assert caught.value.stage == "read"
    assert FIELD in str(caught.value) and "PRIVATE-USER-VALUE" not in str(caught.value)


@pytest.mark.parametrize("kind", ["evaluation", "campaign"])
def test_restored_task0_explicit_reference_stays_byte_equal(tmp_path, kind):
    directory = Path(__file__).parent / "data/dev66-reference"
    spec = importlib.util.spec_from_file_location("dev66_reference_restore", directory / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = json.loads((directory / f"{kind}.json").read_text())
    digest = module.restore_bundle(record, tmp_path)
    store = PreparationStore(tmp_path)
    reference = record["requests"]["explicit"]
    for extra in ({}, {FIELD: 3}, {FIELD: None}):
        prepared = store.prepare_evaluation(digest, **reference["arguments"], **extra)
        descriptor = store._storage.get_prepared(prepared.digest).descriptor
        assert prepared.evaluation.max_consecutive_unmeasured == 3
        assert prepared.digest == reference["prepared_digest"]
        assert canonical_json_bytes(descriptor.to_payload()).decode() == reference["canonical_descriptor"]
        assert canonical_json_bytes(prepared.evaluation.to_payload()).decode() == reference["canonical_plan"]


@pytest.mark.parametrize("value", [2, "off"])
def test_measurements_report_saved_effective_threshold(tmp_path, monkeypatch, value):
    from dataclasses import replace
    from test_bundle_measurements import _campaign, _operation, _read
    store, _, _, bundle, original = _campaign(tmp_path, monkeypatch)
    stored = store._storage.get_prepared(original.digest)
    plan = replace(original.evaluation, **{FIELD: value})
    # Store the actual descriptor, then let the public read resolve it normally.
    descriptor = replace(stored.descriptor, evaluation=plan)
    prepared = store._storage.publish_prepared(descriptor).summary()
    item, = _read(store, bundle.digest, [_operation(prepared)])["items"]
    assert item["plan"][FIELD] == value
