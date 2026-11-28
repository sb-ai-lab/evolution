import pytest

from evolution_lab.panel import allowlist
from evolution_lab.panel.app import create_panel_app
from panel_support import HOST, RecordingUpstream, make_config, request

@pytest.fixture
def upstream():
    return RecordingUpstream()


@pytest.fixture
def app(tmp_path, upstream):
    return create_panel_app(make_config(tmp_path), upstream=upstream)


def _security_headers(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "default-src 'self'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in response.headers


async def test_token_sets_a_strict_cookie_and_redirects(app):
    response = await request(app, "GET", "/?token=" + "k" * 43, cookie=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("lab_panel_4321=" + "k" * 43)
    for part in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert part in cookie
    assert "domain" not in cookie.lower()  # host-only: returned to this launch host name alone
    _security_headers(response)


@pytest.mark.parametrize("path,cookie", [("/", False), ("/?token=wrong", False),
                                         ("/v1/health", False), ("/api/health", False)])
async def test_missing_or_wrong_token_is_forbidden(app, upstream, path, cookie):
    response = await request(app, "GET", path, cookie=cookie)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert upstream.requests == []
    _security_headers(response)


async def test_wrong_cookie_is_forbidden(app, upstream):
    response = await request(app, "GET", "/v1/health", cookie=False, headers={"cookie": "lab_panel_4321=nope"})
    assert response.status_code == 403 and upstream.requests == []


async def test_cookie_of_another_port_is_forbidden(app, upstream):
    response = await request(app, "GET", "/v1/health", cookie=False,
                             headers={"cookie": "lab_panel_9999=" + "k" * 43})
    assert response.status_code == 403 and upstream.requests == []


@pytest.mark.parametrize("host", ["evil.example:4321", "127.0.0.1:9999", "localhost", "", "127.0.0.1.nip.io:4321",
                                  "127.0.0.1:4321", "localhost:4321", HOST, f"{HOST}:9999",
                                  "pffffffffffffffff.localhost:4321", f"x.{HOST}:4321"])
async def test_foreign_host_is_421_before_anything(app, upstream, host):
    response = await request(app, "GET", "/v1/health", host=host)
    assert response.status_code == 421
    assert response.json()["error"]["code"] == "misdirected_request"
    assert upstream.requests == []


async def test_only_the_launch_host_passes(app, upstream):
    response = await request(app, "GET", "/v1/health", host=f"{HOST}:4321")
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_mutating_methods_are_405_even_without_a_cookie(app, upstream, method):
    response = await request(app, method, "/v1/operations", cookie=False)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["/v1/bundles/" + "a" * 64, "/v1/tasks/t1",
                                  "/v1/operations/op1/exports/" + "b" * 64 + "/content",
                                  "/v1/connections/c1/target", "/v2/health", "/v1/operations/op1/unknown",
                                  "/v1/projects/demo/probes", "/v1/probes/" + "c" * 32])
async def test_paths_outside_the_allowlist_are_404(app, upstream, path):
    response = await request(app, "GET", path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert upstream.requests == []


async def test_project_tree_is_explicitly_excluded_from_panel(app, upstream):
    assert "/v1/projects/{project_id}/tree" in allowlist.EXCLUDED
    response = await request(app, "GET", "/v1/projects/demo/tree?implementation=agent&path=.")
    assert response.status_code == 404
    assert upstream.requests == []


@pytest.mark.parametrize(("template", "path"), [
    ("/v1/approvals", "/v1/approvals?operation_id=op1"),
    ("/v1/projects/{project_id}/approvals", "/v1/projects/demo/approvals"),
])
async def test_approval_reads_are_explicitly_excluded_from_panel(app, upstream, template, path):
    assert template in allowlist.EXCLUDED
    response = await request(app, "GET", path)
    assert response.status_code == 404
    assert upstream.requests == []


@pytest.mark.parametrize("path", ["/v1/operations/x%2F..%2Fexports", "/v1/operations/../bundles/x",
                                  "/v1/operations/./campaign", "/v1/operations//campaign",
                                  "/v1/operations/op1/campaign/", "/v1/operations/%6Fp1"])
async def test_encoded_or_dotted_paths_never_reach_the_service(app, upstream, path):
    response = await request(app, "GET", path)
    assert response.status_code == 404
    assert upstream.requests == []


def test_every_service_get_route_is_classified():
    from evolution_lab.service.app import create_app
    service = create_app(object(), event_handler=lambda *_: None)
    routes = getattr(service, "_app", service).routes
    templates = {route.path for route in routes if "GET" in getattr(route, "methods", set())}
    assert templates == set(allowlist.ALLOWED) | set(allowlist.EXCLUDED)
    assert not set(allowlist.ALLOWED) & set(allowlist.EXCLUDED)
    assert "/v1/operations/{operation_id}/exports/{export_digest}/content" in allowlist.EXCLUDED
    assert "/v1/operations/{operation_id}/routing" in allowlist.ALLOWED
    assert "/v1/execution-policies" in allowlist.EXCLUDED
    assert "/v1/execution-policies/{digest}" in allowlist.EXCLUDED


@pytest.mark.parametrize("path", ["/v1/health", "/v1/operations?view=summary", "/v1/operations/op-1:a.b_c/events",
                                  "/v1/projects/demo/operations?view=summary", "/v1/scenarios/connect",
                                  "/v1/operations/op1/measurements/m-r01-a01", "/v1/targets?bundle=x&ready=1",
                                  "/v1/operations/op1/report.md", "/v1/operations/op-1:a.b_c/routing"])
def test_allowed_examples(path):
    assert allowlist.allowed(path.split("?", 1)[0])


@pytest.mark.parametrize("path", [
    "/v1/operations/op1/measurements/m-r01-a01/cases/case-1/samples/00",
    "/v1/operations/op1/measurements/m-r01-a01@e1/cases/%D0%BA%D0%B5%D0%B9%D1%81%2B1%40a/samples/a-00",
    "/v1/operations/op1/attempts/r01-a02",
    "/v1/operations/op1/reflections/r01-x01",
])
def test_content_paths_allowed(path):
    assert allowlist.allowed(path)


@pytest.mark.parametrize("path", [
    "/v1/operations/op1/measurements/m/cases/a%2Fb/samples/00",
    "/v1/operations/op1/measurements/m/cases/../samples/00",
    "/v1/operations/op1/measurements/m/cases/%zz/samples/00",
    "/v1/operations/op1/attempts/r01-x01",
    "/v1/operations/op1/reflections/../../health",
    "/v1/operations/op1/reportXmd",
    "/v1/operations/op1/report.md/x",
])
def test_content_paths_refused(path):
    assert not allowlist.allowed(path)


@pytest.mark.parametrize("path,ok", [
    ("/v1/operations/op1/measurements/m-r01-champion@e1", True),
    ("/v1/operations/op1/measurements/m-r01-champion%40e1", True),
    ("/v1/operations/op1/measurements/m-r01-champion@e12", True),
    ("/v1/operations/op1/measurements/m-r01-champion@e0", False),
    ("/v1/operations/op1/measurements/m-r01-champion@e01", False),
    ("/v1/operations/op1/measurements/m@e1@e2", False),
    ("/v1/operations/op1/measurements/m%40e1%40e2", False),
    ("/v1/operations/op1/measurements/m%2F..%2Fx", False),
    ("/v1/operations/op1/measurements/m%41", False),
    ("/v1/operations/op1%40e1/measurements/m1", False),
    ("/v1/operations/op1@e1/report", False),
    ("/v1/operations/op1@e1/measurements", False),
    ("/v1/projects/p@e1", False),
])
def test_allowlist_epoch_only_in_measurement_segment(path, ok):
    assert allowlist.allowed(path) is ok
