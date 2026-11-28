"""HTTP fixed-evaluation settings preserve Python preparation semantics without execution."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from types import SimpleNamespace

import httpx
import pytest

from evolution_lab.preparation import PreparationStore
from evolution_lab.service.codecs import decode_prepare_request
from evolution_lab.service.errors import ServiceError
from test_preparation_evaluation import _campaign_bundle, _evaluation_bundle, _evaluation_request, _set_session_limit
from test_service_codec import EVALUATION_REQUEST, EVALUATION_REQUEST_2


SETTINGS = {"sample_retries": 2, "min_coverage": 0.9, "concurrency": 2, "scorer_concurrency": 3}
INVALID_SETTINGS = [
    ("sample_retries", -1), ("sample_retries", 9), ("sample_retries", True),
    ("sample_retries", 1.5), ("sample_retries", "2"),
    ("min_coverage", 0), ("min_coverage", -0.1), ("min_coverage", 1.1),
    ("min_coverage", True), ("min_coverage", "0.9"),
    ("concurrency", 0), ("concurrency", -1), ("concurrency", True),
    ("concurrency", 1.5), ("concurrency", "2"),
    ("scorer_concurrency", 0), ("scorer_concurrency", -1), ("scorer_concurrency", True),
    ("scorer_concurrency", 1.5), ("scorer_concurrency", "3"),
]


def body(digest="a" * 64, **settings):
    return {**EVALUATION_REQUEST, "schema": "lab-service-prepare-request/4", "bundle_digest": digest,
            **dict.fromkeys(SETTINGS), **settings}


def test_codec_v4_preserves_settings_and_nulls():
    assert {key: decode_prepare_request(body(**SETTINGS)).kwargs[key] for key in SETTINGS} == SETTINGS
    assert all(decode_prepare_request(body()).kwargs[key] is None for key in SETTINGS)


@pytest.mark.parametrize(("name", "value"), INVALID_SETTINGS + [
    ("min_coverage", float("nan")), ("min_coverage", float("inf")),
    ("min_coverage", 10 ** 500), ("sample_retries", 10 ** 500),
])
def test_codec_v4_invalid_settings_are_invalid_request(name, value):
    with pytest.raises(ServiceError) as caught:
        decode_prepare_request(body(**{name: value}))
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("settings", [
    {"sample_retries": 0, "min_coverage": 1, "concurrency": 1, "scorer_concurrency": 1},
    {"sample_retries": 8, "min_coverage": 0.01, "concurrency": 100, "scorer_concurrency": 100},
])
def test_codec_v4_accepts_python_api_boundaries(settings):
    decoded = decode_prepare_request(body(**settings))
    assert {key: decoded.kwargs[key] for key in settings} == settings


@pytest.mark.parametrize("name", [*SETTINGS, "split"])
def test_codec_v4_requires_every_nullable_setting(name):
    payload = body()
    del payload[name]
    with pytest.raises(ServiceError) as caught:
        decode_prepare_request(payload)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("name", SETTINGS)
def test_codec_older_versions_refuse_new_keys(version, name):
    payload = EVALUATION_REQUEST_2 if version == 2 else EVALUATION_REQUEST
    with pytest.raises(ServiceError) as caught:
        decode_prepare_request({**payload, name: SETTINGS[name]})
    assert caught.value.code == "invalid_request"


def test_codec_v4_campaign_keeps_the_old_shape():
    payload = {"schema": "lab-service-prepare-request/4", "kind": "campaign", "bundle_digest": "a" * 64,
               "stop_grace_seconds": None, "cleanup_wait_seconds": None}
    assert decode_prepare_request(payload).kwargs == {"stop_grace_seconds": None, "cleanup_wait_seconds": None}
    for name in SETTINGS:
        with pytest.raises(ServiceError) as caught:
            decode_prepare_request({**payload, name: None})
        assert caught.value.code == "invalid_request"


@pytest.fixture
async def preparation_http(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime

    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state"))
    runtime.owner = SimpleNamespace(owner_generation="gen")
    runtime.preparation = PreparationStore(tmp_path / "state")
    runtime._executor = ThreadPoolExecutor(max_workers=1)
    runtime._admission_open = True
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://lab") as client:
            yield runtime.preparation, client
    finally:
        runtime._executor.shutdown(wait=True)


async def test_http_v4_campaign_bundle_matches_direct_plan(preparation_http, tmp_path, monkeypatch):
    store, client = preparation_http
    bundle, _ = _campaign_bundle(tmp_path, monkeypatch)
    explicit = {"selection": {"count": 1}, "repeat_seeds": [17], "evaluation_set_id": "fixed-set",
                "max_turns": 2, "timeout_seconds": 30, "metric_plan": {"primary": "score"},
                "max_failed_turns": 1, "call_retries": "off", "runtime_policy": None, "split": None}
    direct = store.prepare_evaluation(bundle.digest, **explicit, **SETTINGS)
    response = await client.post("/v1/prepared-executions", json={**body(bundle.digest, **SETTINGS), **explicit})
    assert response.status_code == 200, response.text
    assert response.json() == direct.to_payload()
    plan = response.json()["evaluation"]
    assert {key: plan[key] for key in SETTINGS} == SETTINGS
    assert store.get_prepared(response.json()["digest"]).evaluation == direct.evaluation
    assert not list(tmp_path.rglob("*_EXECUTED"))


@pytest.mark.parametrize("use_defaults", [False, True])
async def test_http_v4_nulls_inherit_input_or_lab_defaults(preparation_http, tmp_path, monkeypatch, use_defaults):
    store, client = preparation_http
    defaults = SETTINGS if use_defaults else {}
    request = _evaluation_request(tmp_path, monkeypatch, schema="lab-evaluation-input/2", extra_defaults=defaults)
    _set_session_limit(request, 2)
    bundle = store.create_bundle(**request)
    direct = store.prepare_evaluation(bundle.digest, selection={"count": 1})
    payload = body(bundle.digest)
    for key in payload:
        if key not in {"schema", "kind", "bundle_digest", "selection"}:
            payload[key] = None
    response = await client.post("/v1/prepared-executions", json=payload)
    assert response.status_code == 200, response.text
    assert response.json() == direct.to_payload()
    plan = store.get_prepared(response.json()["digest"]).evaluation
    assert (plan.sample_retries, plan.min_coverage, plan.concurrency, plan.scorer_concurrency) == (
        (2, 0.9, 2, 3) if use_defaults else (3, 0.95, 1, 1))


async def test_http_v4_overrides_input_settings(preparation_http, tmp_path, monkeypatch):
    store, client = preparation_http
    request = _evaluation_request(tmp_path, monkeypatch, schema="lab-evaluation-input/2", extra_defaults=SETTINGS)
    _set_session_limit(request, 2)
    bundle = store.create_bundle(**request)
    overrides = {"sample_retries": 0, "min_coverage": 1, "concurrency": 1, "scorer_concurrency": 1}
    direct = store.prepare_evaluation(bundle.digest, selection={"count": 1}, repeat_seeds=[17],
                                     runtime_policy={"stop_grace_seconds": 1, "cleanup_wait_seconds": 2}, **overrides)
    response = await client.post("/v1/prepared-executions", json={**body(bundle.digest, **overrides), "repeat_seeds": [17]})
    assert response.status_code == 200, response.text
    assert response.json() == direct.to_payload()
    assert {key: response.json()["evaluation"].get(key, 1) for key in overrides} == overrides


@pytest.mark.parametrize(("name", "value"), INVALID_SETTINGS)
async def test_http_v4_invalid_settings_refuse_before_preparation(preparation_http, name, value):
    store, client = preparation_http
    response = await client.post("/v1/prepared-executions", json=body(**{name: value}))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert list((store._storage.state_directory / "prepared").iterdir()) == []


@pytest.mark.parametrize("version", [2, 3])
async def test_http_older_versions_still_prepare_and_refuse_new_settings(preparation_http, tmp_path, monkeypatch, version):
    store, client = preparation_http
    bundle, _ = _evaluation_bundle(tmp_path, monkeypatch)
    payload = dict(EVALUATION_REQUEST_2 if version == 2 else EVALUATION_REQUEST,
                   bundle_digest=bundle.digest, repeat_seeds=[17])
    response = await client.post("/v1/prepared-executions", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["evaluation"]["min_coverage"] == 0.95
    for name, value in SETTINGS.items():
        response = await client.post("/v1/prepared-executions", json={**payload, name: value})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


async def test_http_v4_respects_target_session_capacity(preparation_http, tmp_path, monkeypatch):
    store, client = preparation_http
    bundle, _ = _evaluation_bundle(tmp_path, monkeypatch)
    response = await client.post("/v1/prepared-executions", json=body(bundle.digest, concurrency=2))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "concurrency" in response.json()["error"]["message"]


@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("null_setting", [False, True])
@pytest.mark.parametrize("explicit_split", [False, True])
def test_cli_real_client_transmits_v4_and_fills_nullable_keys(tmp_path, monkeypatch, setting, null_setting, explicit_split):
    from evolution_lab.cli import main
    from evolution_lab.cli.client import ServiceClient

    bundle, state = _evaluation_bundle(tmp_path, monkeypatch)
    summary = PreparationStore(state).prepare_evaluation(bundle.digest, selection={"count": 1}).to_payload()
    sent = []

    def handler(request):
        sent.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=summary)

    payload = {key: value for key, value in EVALUATION_REQUEST.items() if key not in {"schema", "split"}}
    payload[setting] = None if null_setting else SETTINGS[setting]
    if explicit_split:
        payload["split"] = "test"
    path = tmp_path / "prepare.json"
    path.write_text(json.dumps(payload))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(["--socket", "/tmp/lab.sock", "--no-request-id", "prepare", "--request", str(path)],
                stdout=stdout, stderr=stderr,
                client_factory=lambda **kw: ServiceClient(**kw, transport=httpx.MockTransport(handler)))
    assert code == 0, stderr.getvalue()
    assert sent == [("POST", "/v1/prepared-executions", {
        **dict.fromkeys(SETTINGS), "split": None, **payload, "schema": "lab-service-prepare-request/4",
    })]
