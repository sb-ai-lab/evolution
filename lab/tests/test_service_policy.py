"""Real Owner policy API through closed service codecs and keyed mutations."""
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
import pytest

from evolution_lab.execution_policy import policy_digest
from evolution_lab.owner import Owner
from test_execution_policy import policy_payload


@pytest.fixture
async def service(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
    async with Owner.open(tmp_path / "state") as owner:
        runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state"))
        runtime.owner = owner
        runtime._executor = ThreadPoolExecutor(max_workers=1)
        runtime._admission_open = True
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
                                        base_url="http://lab") as client:
                yield owner, client
        finally:
            runtime._executor.shutdown(wait=True)


def request():
    policy = policy_payload()
    return {"schema": "lab-service-policy-confirm-request/1", "policy": policy,
            "expected_digest": policy_digest(policy), "author": "operator", "channel": "api", "note": None}


async def test_round_trip_and_mutation_keys(service):
    owner, client = service
    body = request()
    headers = {"Lab-Request-Id": "confirm", "Lab-Owner-Generation": owner.owner_generation}
    first = await client.post("/v1/execution-policies", json=body, headers=headers)
    assert first.status_code == 200, first.text
    record = first.json()
    assert record["policy"] == body["policy"]
    assert record["schema"] == "lab-execution-policy-record/1"
    assert (await client.post("/v1/execution-policies", json=body, headers=headers)).json() == record
    changed = {**body, "note": "different"}
    assert (await client.post("/v1/execution-policies", json=changed, headers=headers)).status_code == 409
    assert (await client.get("/v1/execution-policies/" + body["expected_digest"])).json() == record
    from evolution_lab.cli.responses import validate
    assert validate(record, "lab-execution-policy-record/1").to_payload() == record
    assert (await client.get("/v1/execution-policies")).json() == {
        "policies": [record]}
    assert (await client.get("/v1/execution-policies/" + "0" * 64)).status_code == 404


@pytest.mark.parametrize("change", ["extra", "missing", "schema", "policy", "digest", "author", "note",
                                    "author_surrogate", "channel_surrogate", "note_surrogate"])
async def test_closed_form_refusals_are_4xx_and_write_nothing(service, change):
    owner, client = service
    body = request()
    if change == "extra": body["extra"] = True
    elif change == "missing": del body["channel"]
    elif change == "schema": body["schema"] = "lab-service-policy-confirm-request/99"
    elif change == "policy": body["policy"]["extra"] = True
    elif change == "digest": body["expected_digest"] = "0" * 64
    elif change == "author": body["author"] = []
    elif change == "note": body["note"] = {}
    elif change.endswith("_surrogate"): body[change.split("_")[0]] = "\ud800"
    response = await client.post("/v1/execution-policies", content=json.dumps(body),
                                 headers={"Content-Type": "application/json"})
    assert 400 <= response.status_code < 500, response.text
    assert await owner.execution_policies() == ()


@pytest.mark.parametrize("suffix", ["?unknown=1", "/bad"])
async def test_read_forms_refused(service, suffix):
    _, client = service
    assert (await client.get("/v1/execution-policies" + suffix)).status_code == 400


async def test_keyed_digest_refusal_is_replayed_without_creating_policy(service):
    owner, client = service
    body = {**request(), "expected_digest": "0" * 64}
    headers = {"Lab-Request-Id": "bad-digest", "Lab-Owner-Generation": owner.owner_generation}
    first = await client.post("/v1/execution-policies", json=body, headers=headers)
    second = await client.post("/v1/execution-policies", json=body, headers=headers)
    assert first.status_code == second.status_code == 409
    assert first.json() == second.json()
    assert first.json()["error"]["code"] == "digest_mismatch"
    assert await owner.execution_policies() == ()


async def test_v2_confirmation_and_invalid_reason_pointer(service):
    owner, client = service
    policy = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 1, 'delay_seconds': 0}}
    body = {**request(), 'policy': policy, 'expected_digest': policy_digest(policy)}
    response = await client.post('/v1/execution-policies', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['policy'] == policy
    policy['auto_resume']['reasons'] = ['misspelled']
    body['expected_digest'] = policy_digest(policy)
    response = await client.post('/v1/execution-policies', json=body)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'invalid_request'
    assert '/auto_resume/reasons/0' in response.json()['error']['message']


@pytest.mark.parametrize('field', ['delay_seconds', 'busy_recheck_seconds'])
@pytest.mark.parametrize('value', [31_536_001, 2**72])
async def test_v2_confirmation_rejects_interval_with_safe_pointer(service, field, value):
    owner, client = service
    policy = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 0, 'busy_recheck_seconds': 1}}
    policy['auto_resume'][field] = value
    body = {**request(), 'policy': policy, 'expected_digest': policy_digest(policy)}
    response = await client.post('/v1/execution-policies', json=body)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'invalid_request'
    assert '/auto_resume/' + field in response.json()['error']['message']
    assert await owner.execution_policies() == ()


async def test_v2_confirmation_accepts_interval_max(service):
    owner, client = service
    policy = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 2**70,
        'delay_seconds': 31_536_000, 'busy_recheck_seconds': 31_536_000}}
    body = {**request(), 'policy': policy, 'expected_digest': policy_digest(policy)}
    response = await client.post('/v1/execution-policies', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['policy'] == policy
    assert len(await owner.execution_policies()) == 1
