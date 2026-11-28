"""S7 W26: service environment capture, health projection and serve flags."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from evolution_lab.preparation.models import (
    DistributionPin, EnvironmentDescriptor, EnvironmentInput, PlatformIdentity, RuntimeFile,
)


def _descriptor():
    return EnvironmentDescriptor(
        executable="/home/someone/venv/bin/python", executable_sha256="1" * 64,
        python_version="3.14.7",
        distributions=(DistributionPin("evolution-lab", "0.0.3.dev23", "2" * 64),
                       DistributionPin("evo-skills", "0.3.0a16", "3" * 64)),
        runtime_files=(RuntimeFile("/home/someone/private/runtime.txt", "4" * 64),),
        platform=PlatformIdentity("Linux", "x86_64"), proof_profile="wheel-members/1",
        limitations=("a limitation",),
    )


def _environment():
    from evolution_lab.service.environment import ServiceEnvironment
    return ServiceEnvironment(EnvironmentInput(Path("/home/someone/venv/bin/python"),
        (Path("/home/someone/w/lab.whl"),), (Path("/home/someone/private/runtime.txt"),)), _descriptor())


def test_environment_projection_has_executable_and_no_wheel_or_runtime_paths():
    environment = _environment()
    payload = environment.to_payload()
    assert payload == {
        "schema": "lab-service-environment/2", "digest": _descriptor().digest,
        "executable": "/home/someone/venv/bin/python",
        "python_version": "3.14.7", "executable_sha256": "1" * 64,
        "platform": {"system": "Linux", "machine": "x86_64"},
        "distributions": [
            {"name": "evo-skills", "version": "0.3.0a16", "wheel_sha256": "3" * 64},
            {"name": "evolution-lab", "version": "0.0.3.dev23", "wheel_sha256": "2" * 64},
        ],
        "runtime_files": [{"sha256": "4" * 64}], "proof_profile": "wheel-members/1",
    }
    assert "/home/someone/private/runtime.txt" not in json.dumps(payload)
    assert "/home/someone/w/lab.whl" not in json.dumps(payload)


def test_service_health_payload_extends_owner_health_without_changing_fields():
    from evolution_lab.owner import Health
    from evolution_lab.service.environment import service_health_payload

    health = Health("0.0.3.dev23", "0.3.0a16", (), "generation", None, ())
    owner = health.to_payload()
    for environment in (None, _environment()):
        payload = service_health_payload(health, environment)
        assert payload["schema"] == "lab-service-health/2"
        assert {k: v for k, v in payload.items() if k not in {"schema", "service_environment"}} == \
            {k: v for k, v in owner.items() if k != "schema"}
        assert payload["service_environment"] == (None if environment is None else environment.to_payload())


@pytest.mark.parametrize("kwargs", [
    {"execution_runtime_files": ("/abs/runtime.txt",)},
    {"execution_wheels": ("relative.whl",)},
])
def test_service_config_rejects_runtime_files_without_wheels_and_relative_paths(tmp_path, kwargs):
    from evolution_lab.service.errors import ServiceError
    from evolution_lab.service.runtime import ServiceConfig

    with pytest.raises(ServiceError) as caught:
        ServiceConfig(tmp_path / "s.sock", tmp_path / "state", **kwargs)
    assert caught.value.code == "invalid_argument"


def test_failed_capture_exits_before_state_and_socket(tmp_path, monkeypatch):
    from evolution_lab.preparation import PreparationError
    from evolution_lab.service import runtime as runtime_module
    from evolution_lab.service.runtime import ServiceConfig
    from evolution_lab.service.server import serve

    tmp_path.chmod(0o700)
    seen = []

    def refuse(wheels, runtime_files):
        seen.append((state.exists(), socket_path.exists()))
        raise PreparationError("environment_invalid", "bundle")

    monkeypatch.setattr(runtime_module, "capture_service_environment", refuse)
    state, socket_path = tmp_path / "state", tmp_path / "s.sock"
    stderr = io.StringIO()
    config = ServiceConfig(socket_path, state, 5, execution_wheels=(tmp_path / "lab.whl",),
                           home_directory=tmp_path / "home")
    assert asyncio.run(serve(config, stderr=stderr)) == 1
    assert seen == [(False, False)]
    assert not state.exists() and not socket_path.exists()
    assert not (tmp_path / "home").exists()  # the refused capture precedes the Lab home too
    assert json.loads(stderr.getvalue().splitlines()[0])["error"]["code"] == "environment_invalid"


def test_serve_flags_reach_run_service_and_runtime_file_needs_wheel(monkeypatch):
    from evolution_lab.cli import main
    from evolution_lab.service import server

    calls = []
    monkeypatch.setattr(server, "run_service", lambda **kwargs: calls.append(kwargs) or 0)
    base = ["serve", "--socket", "/tmp/s.sock", "--state", "/tmp/state"]
    stderr = io.StringIO()
    assert main([*base, "--execution-runtime-file", "/abs/r.txt"], stdout=io.StringIO(), stderr=stderr) == 2
    assert calls == []
    assert main([*base, "--execution-wheel", "/abs/a.whl", "--execution-wheel", "/abs/b.whl",
                 "--execution-runtime-file", "/abs/r.txt"], stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert calls[0]["execution_wheels"] == ("/abs/a.whl", "/abs/b.whl")
    assert calls[0]["execution_runtime_files"] == ("/abs/r.txt",)


def test_cli_validates_service_health_and_rejects_wheel_or_runtime_paths_in_environment():
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner import Health
    from evolution_lab.service.environment import service_health_payload

    payload = service_health_payload(Health("l", "c", (), "generation", None, ()), _environment())
    validate(payload, "lab-service-health/2")
    broken = json.loads(json.dumps(payload))
    broken["service_environment"]["runtime_files"][0]["path"] = "/home/someone/x"
    with pytest.raises(ValueError):
        validate(broken, "lab-service-health/2")


def test_cli_rejects_noncanonical_owner_and_environment_field_types():
    """A key-shape-only check can miss wrong-typed values; the round-trip must catch them."""
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner import Health
    from evolution_lab.service.environment import service_health_payload

    payload = service_health_payload(Health("l", "c", (), "generation", None, ()), _environment())
    validate(payload, "lab-service-health/2")  # the well-formed payload still validates

    owner_broken = json.loads(json.dumps(payload))
    owner_broken["distributions"] = "abc"
    with pytest.raises(ValueError):
        validate(owner_broken, "lab-service-health/2")

    platform_broken = json.loads(json.dumps(payload))
    platform_broken["service_environment"]["platform"]["system"] = 1
    with pytest.raises(ValueError):
        validate(platform_broken, "lab-service-health/2")

    pin_broken = json.loads(json.dumps(payload))
    pin_broken["service_environment"]["distributions"][0]["name"] = 7
    with pytest.raises(ValueError):
        validate(pin_broken, "lab-service-health/2")


class _Public:
    def to_payload(self):
        return {"schema": "test-public/1"}


class _BundleRuntime:
    def __init__(self, environment):
        self.service_environment = environment
        self.preparation_calls = []

    def mutation_key(self, headers):  # Task 5 seam; no key headers in this test
        return None

    async def call_mutation(self, key, route, parts, target, method, /, *args, **kwargs):
        self.preparation_calls.append((method, kwargs))
        return _Public()

    async def call_preparation(self, method, *args, **kwargs):
        self.preparation_calls.append((method, kwargs))
        return _Public()


async def _post_bundle(runtime, environments, schema="lab-service-bundle-request/2"):
    import httpx
    from evolution_lab.service.app import create_app
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        return await client.post("/v1/bundles", json={"schema": schema, "source": {
            "kind": "evaluation", "input_path": "/abs/i.json", "launch_path": "/abs/l.json"},
            "environments": environments, "env": {}, "secret_names": [], "roots": [], "path_bindings": []})


@pytest.mark.asyncio
async def test_marker_without_configured_environment_is_409_without_call():
    runtime = _BundleRuntime(None)
    response = await _post_bundle(runtime, [{"kind": "service"}])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "service_environment_unconfigured"
    assert runtime.preparation_calls == []


@pytest.mark.asyncio
async def test_marker_expands_to_saved_input_and_expected_digest():
    environment = _environment()
    runtime = _BundleRuntime(environment)
    response = await _post_bundle(runtime, [{"kind": "service"}])
    assert response.status_code == 200
    (method, kwargs), = runtime.preparation_calls
    assert method == "create_bundle"
    assert kwargs["environments"] == (environment.input,)
    assert kwargs["expected_execution_environment"] == environment.digest


@pytest.mark.asyncio
async def test_explicit_form_passes_no_expected_digest():
    runtime = _BundleRuntime(_environment())
    await _post_bundle(runtime, [{"executable": "/abs/python", "wheel_paths": ["/abs/a.whl"], "runtime_files": []}])
    (_, kwargs), = runtime.preparation_calls
    assert "expected_execution_environment" not in kwargs


def test_marker_bundle_equals_explicit_and_changed_wheel_is_refused(tmp_path):
    import shutil
    from evolution_lab.preparation import PreparationError, PreparationStore
    from evolution_lab.service.codecs import SERVICE_ENVIRONMENT
    from evolution_lab.service.environment import capture_service_environment, expand_service_environment
    from test_owner_binding import CORE_WHEEL, _installed_lab_wheel
    from test_preparation_expected_environment import _inputs

    bundle, kwargs = _inputs(tmp_path)
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    copies = tuple(Path(shutil.copy2(path, wheels)) for path in (_installed_lab_wheel(), CORE_WHEEL))
    service = capture_service_environment(copies, ())
    environments, expected = expand_service_environment((SERVICE_ENVIRONMENT,), service)
    store = PreparationStore(tmp_path / "service-state")
    marker = store.create_bundle(**{**kwargs, "environments": environments},
                                 expected_execution_environment=expected)
    assert marker.digest == bundle.digest
    assert expected == bundle.environment_digests[0]

    before = sorted(p.name for p in (tmp_path / "service-state" / "bundles").iterdir())
    with copies[1].open("ab") as stream:
        stream.write(b"\0")
    with pytest.raises(PreparationError) as caught:
        store.create_bundle(**{**kwargs, "environments": environments}, expected_execution_environment=expected)
    assert caught.value.code == "environment_invalid"
    assert sorted(p.name for p in (tmp_path / "service-state" / "bundles").iterdir()) == before


@pytest.mark.parametrize("mutate", [
    lambda env: env.pop("executable"),
    lambda env: env.__setitem__("executable", 7),
    lambda env: env.__setitem__("executable", ""),
    lambda env: env.__setitem__("executable", "venv/bin/python"),
    lambda env: env.__setitem__("extra", 1),
    lambda env: env.__setitem__("schema", "lab-service-environment/1"),
])
def test_cli_rejects_invalid_environment_v2(mutate):
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner import Health
    from evolution_lab.service.environment import service_health_payload

    payload = service_health_payload(Health("l", "c", (), "generation", None, ()), _environment())
    validate(payload, "lab-service-health/2")
    mutate(payload["service_environment"])
    with pytest.raises(ValueError):
        validate(payload, "lab-service-health/2")


def test_cli_rejects_health_v1():
    from evolution_lab.cli.responses import validate
    from evolution_lab.owner import Health
    from evolution_lab.service.environment import service_health_payload

    payload = service_health_payload(Health("l", "c", (), "generation", None, ()), None)
    payload["schema"] = "lab-service-health/1"
    for name in ("lab-service-health/1", "lab-service-health/2"):
        with pytest.raises(ValueError):
            validate(payload, name)


def test_service_config_rejects_socket_path_longer_than_af_unix_limit(tmp_path):
    from evolution_lab.service.errors import ServiceError
    from evolution_lab.service.runtime import ServiceConfig

    socket_path = tmp_path / ("s" * 120 + ".sock")
    with pytest.raises(ServiceError) as caught:
        ServiceConfig(socket_path, tmp_path / "state")
    assert caught.value.code == "socket_path_too_long"
