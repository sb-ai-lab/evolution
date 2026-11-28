import asyncio

import httpx
import pytest

from evolution_lab.panel.app import cookie_name, create_panel_app
from panel_support import fake_service, make_config, panel_server, request, uds_service


def _cookies(config):
    return {cookie_name(config.port): config.token}


async def test_forwards_path_query_and_accept_only(tmp_path):
    service = fake_service()
    async with uds_service(service) as sock, panel_server(lambda port: make_config(tmp_path, port=port, socket_path=sock)) as (base, config):
        async with httpx.AsyncClient(base_url=base, cookies=_cookies(config)) as browser:
            response = await browser.get("/v1/operations?view=summary&x=%2F", headers={"accept": "application/json", "last-event-id": "s:1"})
    assert response.status_code == 200
    echoed = response.json()
    assert echoed["query"] == "view=summary&x=%2F"
    assert echoed["accept"] == "application/json"
    assert echoed["cookie"] is None and echoed["host"] == "lab"
    assert echoed["last_event_id"] is None
    assert response.headers["content-security-policy"] == "default-src 'self'"


async def test_sse_streams_frames_without_buffering(tmp_path):
    service = fake_service()
    async with uds_service(service) as sock, panel_server(lambda port: make_config(tmp_path, port=port, socket_path=sock)) as (base, config):
        async with httpx.AsyncClient(base_url=base, cookies=_cookies(config), timeout=10) as browser:
            async with browser.stream("GET", "/v1/operations/op1/events") as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["cache-control"] == "no-store"
                first = await asyncio.wait_for(anext(response.aiter_text()), timeout=1.0)
    assert first.startswith("event: status")


async def test_client_disconnect_closes_the_upstream_stream(tmp_path):
    service = fake_service()
    async with uds_service(service) as sock, panel_server(lambda port: make_config(tmp_path, port=port, socket_path=sock)) as (base, config):
        async with httpx.AsyncClient(base_url=base, cookies=_cookies(config), timeout=10) as browser:
            async with browser.stream("GET", "/v1/operations/op1/events") as response:
                await anext(response.aiter_text())
        await asyncio.wait_for(service.state.disconnected.wait(), timeout=5)


async def test_service_status_codes_and_bodies_pass_through(tmp_path):
    service = fake_service()  # /v1/operations/missing -> 404 {"error": {"code": "not_found", ...}}
    async with uds_service(service) as sock, panel_server(lambda port: make_config(tmp_path, port=port, socket_path=sock)) as (base, config):
        async with httpx.AsyncClient(base_url=base, cookies=_cookies(config)) as browser:
            response = await browser.get("/v1/operations/missing")
    assert response.status_code == 404 and response.json()["error"]["stage"] != "panel"


async def test_service_down_then_up(tmp_path):
    socket_dir_holder = {}
    service = fake_service()
    async with uds_service(service) as sock:
        socket_dir_holder["sock"] = sock
    # the socket path is gone now: the service is down
    sock = socket_dir_holder["sock"]
    async with panel_server(lambda port: make_config(tmp_path, port=port, socket_path=sock)) as (base, config):
        async with httpx.AsyncClient(base_url=base, cookies=_cookies(config)) as browser:
            proxied = await browser.get("/v1/health")
            health = await browser.get("/api/health")
            assert proxied.status_code == 503
            assert proxied.json()["error"]["code"] == "service_unavailable"
            assert proxied.json()["hint"] == f"lab serve --home {config.home}"
            assert health.status_code == 200
            assert health.json() == {"schema": "lab-panel-health/1", "started_at": config.started_at,
                                     "home": str(config.home),
                                     "service": {"state": "service_unavailable", "hint": f"lab serve --home {config.home}"}}
            async with uds_service(fake_service(), socket_path=sock):
                assert (await browser.get("/v1/health")).status_code == 200
                assert (await browser.get("/api/health")).json()["service"] == {"state": "ok"}


class _FailingUpstream(httpx.AsyncBaseTransport):
    def __init__(self, error):
        self.error = error

    async def handle_async_request(self, request):
        raise self.error("upstream failed", request=request)


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout, httpx.PoolTimeout])
async def test_transport_errors_before_headers_are_service_unavailable(tmp_path, error):
    config = make_config(tmp_path)
    app = create_panel_app(config, upstream=_FailingUpstream(error))
    for path in ("/v1/operations", "/v1/operations/op1/events"):
        response = await request(app, "GET", path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert response.json()["hint"] == f"lab serve --home {config.home}"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"] == "default-src 'self'"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    health = await request(app, "GET", "/api/health")
    assert health.status_code == 200
    assert health.json()["service"] == {"state": "service_unavailable", "hint": f"lab serve --home {config.home}"}


async def test_suspension_route_is_allowlisted_read_only(tmp_path):
    from evolution_lab.panel.allowlist import ALLOWED, allowed
    path = "/v1/operations/op1/suspensions"
    assert "/v1/operations/{operation_id}/suspensions" in ALLOWED
    assert allowed(path)
    assert not allowed("/v1/operations/%2E%2E/suspensions")
    app = create_panel_app(make_config(tmp_path), upstream=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"forwarded": request.url.path})))
    assert (await request(app, "GET", path)).json() == {"forwarded": path}
    assert (await request(app, "POST", path)).status_code == 405


async def test_markdown_passes_with_its_type_and_without_the_disposition(tmp_path):
    service = fake_service()
    async with uds_service(service) as sock, panel_server(lambda port: make_config(tmp_path, port=port, socket_path=sock)) as (base, config):
        async with httpx.AsyncClient(base_url=base, cookies=_cookies(config)) as browser:
            response = await browser.get("/v1/operations/op1/report.md")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "content-disposition" not in response.headers
    assert response.content.decode("utf-8") == "# Кампания\n"
