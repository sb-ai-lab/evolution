"""New fixed evaluations resolve retry defaults; stored plans retain their meaning."""
import importlib.util
import json
from pathlib import Path

import pytest

from evolution_lab.preparation import PreparationError, PreparationStore
from evolution_lab.preparation.models import PreparedExecutionDescriptor
from evolution_lab.preparation.storage import canonical_json_bytes
from test_preparation_evaluation import _evaluation_bundle
# The actual service route/runtime runs only in the parent's installed selection.
from test_service_prepare_v4 import preparation_http


RETRY_KEYS = ("sample_retries", "min_coverage", "max_consecutive_unmeasured", "call_retries", "max_failed_turns")


def _reference(kind):
    directory = Path(__file__).parent / "data/dev66-reference"
    spec = importlib.util.spec_from_file_location("dev66_retry_reference", directory / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return json.loads((directory / f"{kind}.json").read_text()), module.restore_bundle


def _assert_retry(plan):
    assert plan.sample_retries == 3
    assert plan.min_coverage == 0.95
    assert plan.max_consecutive_unmeasured == 3
    assert plan.call_retries == {"max_attempts": 3, "turn_max_attempts": 2}
    assert plan.max_failed_turns == plan.max_turns
    payload = plan.to_payload()
    assert payload["sample_retries"] == 3
    assert payload["min_coverage"] == 0.95
    # Default threshold retains Task 4's canonical omission.
    assert "max_consecutive_unmeasured" not in payload


@pytest.mark.parametrize("kind", ["evaluation", "campaign"])
@pytest.mark.parametrize("nulls", [False, True])
def test_restored_bundle_omitted_and_null_api_settings_resolve_retry(tmp_path, kind, nulls):
    record, restore = _reference(kind)
    store = PreparationStore(tmp_path)
    digest = restore(record, tmp_path)
    bundle = store._storage.get_bundle(digest)
    manifest = bundle.path / bundle.descriptor.entrypoint
    original_manifest = manifest.read_bytes()
    arguments = record["requests"]["omitted"]["arguments"]
    prepared = store.prepare_evaluation(digest, **arguments, **(dict.fromkeys(RETRY_KEYS) if nulls else {}))
    _assert_retry(prepared.evaluation)
    reopened = store.get_prepared(prepared.digest)
    _assert_retry(reopened.evaluation)
    assert reopened == prepared
    descriptor = store._storage.get_prepared(prepared.digest).descriptor
    assert PreparedExecutionDescriptor.from_payload(descriptor.to_payload()).digest == prepared.digest
    assert manifest.read_bytes() == original_manifest
    if kind == "evaluation":
        assert prepared.digest != record["requests"]["omitted"]["prepared_digest"]


@pytest.mark.parametrize("nulls", [False, True])
def test_document3_omitted_and_null_settings_resolve_retry(tmp_path, monkeypatch, nulls):
    bundle, state = _evaluation_bundle(tmp_path, monkeypatch, schema="lab-evaluation-input/3",
                                      extra_defaults=dict.fromkeys(RETRY_KEYS) if nulls else {})
    prepared = PreparationStore(state).prepare_evaluation(bundle.digest, selection={"all": True})
    _assert_retry(prepared.evaluation)
    _assert_retry(PreparationStore(state).get_prepared(prepared.digest).evaluation)


@pytest.mark.parametrize("kind", ["evaluation", "campaign"])
def test_all_explicit_requests_keep_task0_complete_bytes(tmp_path, kind):
    record, restore = _reference(kind)
    store = PreparationStore(tmp_path)
    digest = restore(record, tmp_path)
    reference = record["requests"]["explicit"]
    prepared = store.prepare_evaluation(digest, **reference["arguments"])
    descriptor = store._storage.get_prepared(prepared.digest).descriptor
    assert prepared.to_payload() == reference["prepared"]
    assert prepared.digest == reference["prepared_digest"]
    assert canonical_json_bytes(descriptor.to_payload()).decode() == reference["canonical_descriptor"]
    assert canonical_json_bytes(prepared.evaluation.to_payload()).decode() == reference["canonical_plan"]


def test_dev65_stored_omitted_plan_keeps_actual_old_values_and_digest(tmp_path):
    record, restore = _reference("evaluation")
    restore(record, tmp_path)
    reference = record["requests"]["omitted"]
    descriptor = PreparedExecutionDescriptor.from_payload(reference["descriptor"])
    plan = descriptor.evaluation
    assert (plan.sample_retries, plan.call_retries, plan.max_failed_turns, plan.min_coverage) == (None, None, 1, 0.95)
    assert descriptor.digest == reference["prepared_digest"]
    assert canonical_json_bytes(descriptor.to_payload()).decode() == reference["canonical_descriptor"]
    store = PreparationStore(tmp_path)
    stored = store._storage.publish_prepared(descriptor).summary()
    assert store.get_prepared(stored.digest).evaluation == plan
    assert store.get_prepared(stored.digest).digest == reference["prepared_digest"]


@pytest.mark.parametrize("required", ["repeat_seeds", "evaluation_set_id", "max_turns", "timeout_seconds", "metric_plan"])
def test_campaign_still_requires_non_tolerance_plan_inputs(tmp_path, required):
    record, restore = _reference("campaign")
    digest = restore(record, tmp_path)
    arguments = dict(record["requests"]["omitted"]["arguments"])
    arguments.pop(required)
    with pytest.raises(PreparationError) as caught:
        PreparationStore(tmp_path).prepare_evaluation(digest, **arguments)
    assert (caught.value.code, caught.value.stage) == ("invalid_request", "prepare")


@pytest.mark.parametrize("kind", ["evaluation", "campaign"])
async def test_request5_null_settings_prepare_retry_through_http(preparation_http, tmp_path, kind):
    from test_service_codec import EVALUATION_REQUEST

    store, client = preparation_http
    record, restore = _reference(kind)
    digest = restore(record, store._storage.state_directory)
    arguments = record["requests"]["omitted"]["arguments"]
    request = {**EVALUATION_REQUEST, **arguments, "bundle_digest": digest,
               "schema": "lab-service-prepare-request/5", **dict.fromkeys(RETRY_KEYS),
               "concurrency": None, "scorer_concurrency": None}
    response = await client.post("/v1/prepared-executions", json=request)
    assert response.status_code == 200, response.text
    prepared = store.get_prepared(response.json()["digest"])
    _assert_retry(prepared.evaluation)
    assert response.json() == prepared.to_payload()
