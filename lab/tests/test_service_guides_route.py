import httpx

from evolution_lab.service.app import create_app
from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
from test_service_projects import key, service  # noqa: F401  (fixture reuse)


async def test_requirements_route_returns_the_cli_object(service):
    from evolution_lab.guides import requirements
    runtime, client = service
    response = await client.get("/v1/guides/requirements")
    assert response.status_code == 200 and response.json() == requirements()
    assert response.json()["catalogue_version"] == 13
    assert any(item["id"] == "run.failures" for item in response.json()["items"])
    assert runtime.project_session.calls == []


async def test_requirements_route_refuses_a_mutation_key(service):
    runtime, client = service
    scenarios = await client.get("/v1/scenarios", headers=key("r1"))
    response = await client.get("/v1/guides/requirements", headers=key("r2"))
    assert response.status_code == scenarios.status_code != 200
    assert response.json()["error"]["code"] == scenarios.json()["error"]["code"]


async def test_requirements_route_rejects_a_query(service):
    runtime, client = service
    assert (await client.get("/v1/guides/requirements?x=1")).status_code == 400


async def test_requirements_route_works_without_a_home(tmp_path):
    tmp_path.chmod(0o700)
    runtime = ServiceRuntime(ServiceConfig(tmp_path / "s.sock", tmp_path / "state", 0.2))
    runtime._admission_open = True
    transport = httpx.ASGITransport(app=create_app(runtime))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        assert (await client.get("/v1/guides/requirements")).status_code == 200
