"""dump_dom's diagnostics (#475): a page whose app never booted comes back with the browser's log."""

import functools
import http.server
import sys
import threading

import pytest

from panel_browser import dump_dom, find_browser

BROWSER = find_browser()
pytestmark = pytest.mark.slow
needs_browser = pytest.mark.skipif(BROWSER is None, reason="no headless Chromium")


@pytest.fixture
def serve(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def page(body: str) -> str:
        (site / "index.html").write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
        return f"http://127.0.0.1:{server.server_address[1]}/index.html"

    yield page
    server.shutdown()
    server.server_close()


@needs_browser
def test_a_page_that_never_booted_carries_the_browser_console(serve, tmp_path):
    url = serve('<p class="boot">Loading…</p><script>console.error("boot-probe-message")</script>')
    dom = dump_dom(BROWSER, url, budget_ms=2000, workdir=tmp_path)
    log = dom[dom.index("<!-- browser log"):]
    assert "boot-probe-message" in log and "wall time" in log
    assert "dbus" not in log


@needs_browser
def test_a_booted_page_comes_back_unchanged(serve, tmp_path):
    url = serve('<p class="boot">Loading…</p><script>console.error("boot-probe-message");'
                'document.querySelector(".boot").remove()</script>')
    dom = dump_dom(BROWSER, url, budget_ms=2000, workdir=tmp_path)
    assert "browser log" not in dom and "boot-probe-message" in dom  # only the script's own text


def test_native_browser_profile_is_created_under_a_new_workdir(tmp_path):
    workdir = tmp_path / "closed"
    browser = (
        "from pathlib import Path\n"
        "import sys\n"
        "profile = Path(next(arg.split('=', 1)[1] for arg in sys.argv[2:] "
        "if arg.startswith('--user-data-dir=')))\n"
        "assert profile.parent == Path(sys.argv[1]) and profile.is_dir()\n"
        "print('<p>rendered</p>')\n"
    )

    dom = dump_dom([sys.executable, "-c", browser, str(workdir)], "http://fixture", workdir=workdir)

    assert dom.strip() == "<p>rendered</p>"
    assert list(workdir.iterdir()) == []


async def test_request_log_records_path_status_and_order_without_the_query():
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from panel_browser import RequestLog

    async def ok(request):
        return PlainTextResponse("x")

    log = RequestLog(Starlette(routes=[Route("/static/app.js", ok)]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=log), base_url="http://panel") as client:
        await client.get("/static/app.js?token=secret-token")
        await client.get("/static/missing.js")
    lines = log.text().splitlines()
    assert lines[0].startswith("GET /static/app.js 200 +") and lines[1].startswith("GET /static/missing.js 404 +")
    assert "secret-token" not in log.text()


async def test_request_log_marks_a_request_without_a_response():
    from panel_browser import RequestLog

    async def broken(scope, receive, send):
        raise RuntimeError("upstream gone")

    log = RequestLog(broken)
    with pytest.raises(RuntimeError):
        await log({"type": "http", "method": "GET", "path": "/v1/health"}, None, None)
    assert log.text().startswith("GET /v1/health RuntimeError +")


def test_the_request_log_is_appended_only_when_the_app_did_not_boot():
    from panel_browser import RequestLog, with_requests

    log = RequestLog(None)
    log.entries.append({"line": "GET /static/app.js", "status": None, "at": 0.0})
    assert with_requests("<p>rendered</p>", log) == "<p>rendered</p>"
    assert "GET /static/app.js no response +0 ms" in with_requests('<p class="boot">Loading…</p>', log)
