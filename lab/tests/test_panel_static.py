import hashlib
from pathlib import Path

import pytest

import evolution_lab.panel as panel_pkg
from evolution_lab.panel.app import create_panel_app
from panel_support import RecordingUpstream, make_config, request

STATIC = Path(panel_pkg.__file__).parent / "static"


def test_campaign_hierarchy_and_readable_codes():
    screen = (STATIC / "screens/campaign.js").read_text()
    parts = ['<${StatusCard}', '<${SuspensionHistory}', 'Rounds & attempts',
             'Champion lineage', 'Independent acceptance', '<h2>Samples</h2>',
             'data-section="target-tokens"', '<${ReportDownload}', 'Campaign setup & routing',
             'Checks & reflector probe']
    positions = [screen.index(part) for part in parts]
    assert positions == sorted(positions)
    css = (STATIC / "panel.css").read_text()
    assert "color:var(--text-3)" in css[css.index('.status-card-codes'):].split('}')[0]


@pytest.fixture
def app(tmp_path):
    return create_panel_app(make_config(tmp_path), upstream=RecordingUpstream())


async def test_index_is_served_after_the_cookie(app):
    response = await request(app, "GET", "/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert '<script type="module" src="/static/app.js"></script>' in response.text
    assert response.headers["content-security-policy"] == "default-src 'self'"


@pytest.mark.parametrize("path,kind", [("/static/app.js", "text/javascript; charset=utf-8"),
                                       ("/static/vendor/htm-preact.js", "text/javascript; charset=utf-8"),
                                       ("/static/panel.css", "text/css; charset=utf-8"),
                                       ("/static/vendor/LICENSE-htm", "text/plain; charset=utf-8")])
async def test_static_types(app, path, kind):
    response = await request(app, "GET", path)
    assert response.status_code == 200 and response.headers["content-type"] == kind


@pytest.mark.parametrize("path", ["/static/../app.py", "/static/%2e%2e/app.py", "/static/missing.js", "/static/"])
async def test_static_outside_the_map_is_404(app, path):
    assert (await request(app, "GET", path)).status_code == 404


async def test_static_needs_the_cookie(app):
    assert (await request(app, "GET", "/static/app.js", cookie=False)).status_code == 403


def test_no_inline_script_or_style_under_csp():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<style" not in html and "style=" not in html
    assert html.count("<script") == 1 and 'src="/static/app.js"' in html


def test_vendor_is_pinned():
    digest = hashlib.sha256((STATIC / "vendor" / "htm-preact.js").read_bytes()).hexdigest()
    assert digest == "72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7"
    assert digest in (STATIC / "vendor" / "README.md").read_text(encoding="utf-8")


def _sources():
    return {p.relative_to(STATIC).as_posix(): p.read_text(encoding="utf-8")
            for p in STATIC.rglob("*.js") if "vendor" not in p.parts}


def test_app_rechecks_health_after_load():
    # A cheap guard that the outage wiring stays in place; lib/ behaviour is covered by node:test
    # (tests/panel_js), the live browser check covers the rest.
    src = _sources()
    assert "HEALTH_POLL_MS = 5000" in src["lib/api.js"]
    assert "createPoller(() => poll.current(), HEALTH_POLL_MS)" in src["ui/hooks.js"]
    assert "onServiceLost: () => setLost(true)" in src["app.js"] and "openLive(" in src["app.js"]
    api = src["lib/api.js"]
    assert api.index("if (!response.ok)") < api.index("const value = await response.json()")


def test_colon_operation_id_streams_without_percent_encoding():
    from evolution_lab.panel import allowlist
    assert allowlist.allowed("/v1/operations/op:1/events")
    for name, text in _sources().items():
        if name == "lib/api.js":
            # Only core case/sample ids are percent-encoded (spec 2026-10-13 §3.1), in one place.
            assert text.count("encodeURIComponent") == 1 and "const coreSeg" in text
        else:
            assert "encodeURIComponent" not in text, name
        if name != "lib/api.js":
            assert '"/v1/' not in text and "`/v1/" not in text, name


async def test_fonts_are_served_as_woff2(app):
    response = await request(app, "GET", "/static/fonts/Geist-Regular.woff2")
    assert response.status_code == 200 and response.headers["content-type"] == "font/woff2"


async def test_index_loads_tokens_styles_and_module(app):
    text = (await request(app, "GET", "/")).text
    assert '<link rel="stylesheet" href="/static/tokens.css">' in text
    assert '<link rel="stylesheet" href="/static/panel.css">' in text
    assert '<script type="module" src="/static/app.js"></script>' in text
    assert "<style" not in text and "style=" not in text


def test_font_vendoring_is_recorded():
    readme = (STATIC / "fonts" / "README.md").read_text(encoding="utf-8")
    for name in ("Geist-Regular.woff2", "GeistMono-SemiBold.woff2"):
        digest = hashlib.sha256((STATIC / "fonts" / name).read_bytes()).hexdigest()
        assert digest in readme
    assert "SIL Open Font License" in (STATIC / "fonts" / "LICENSE-Geist.txt").read_text(encoding="utf-8")
