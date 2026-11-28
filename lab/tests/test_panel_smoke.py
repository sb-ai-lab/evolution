"""Headless rendering over native dev57/b9 snapshots and retained dev55/b8 default responses.

The `suspension-fault` fixture retains historical dev52/b6 provenance.
"""
import asyncio
from html import unescape
import json
from pathlib import Path
import re
import secrets
import shutil

import pytest
import uvicorn

import evolution_lab.panel.app as panel_app
from evolution_lab.panel.app import PanelConfig, create_panel_app
from panel_browser import RequestLog, dump_dom, find_browser, with_requests
from panel_fixtures import canonical, load_snapshot, mock_upstream
from ports import free_port

BROWSER = find_browser()
pytestmark = [pytest.mark.slow, pytest.mark.skipif(BROWSER is None, reason="no headless Chromium: set LAB_PANEL_CHROMIUM, install one, or pull the playwright image")]
HOST = "p0123456789abcdef.localhost"


async def test_result_notice_details_remain_accessible_without_explanation(tmp_path):
    import httpx
    op = _ids("family14")["campaign_final"]
    inner = mock_upstream("family14").handler

    def handler(request):
        response = inner(request)
        if request.url.path != f"/v1/operations/{op}/campaign":
            return response
        payload = response.json()
        payload.update(gate_mode="statistical", caveats=[], lab_notices=[
            {"code": "no_candidate_measured", "rounds": 4,
             "by_reason": {"clean": 3, "editor_failed": 1}, "reflection_errors": {"timeout": 1}},
        ])
        return httpx.Response(200, json=payload)

    script = """
      const timer = setInterval(() => {
        const card = document.querySelector('.status-card');
        if (!card?.textContent.includes('No candidate measured')) return;
        clearInterval(timer);
        const details = card.querySelector('.status-card-more');
        if (details) details.querySelector('summary').click();
        setTimeout(() => {
          const text = card.innerText;
          document.body.dataset.noncleanNotice = [!!details, text.includes('rounds 4'),
            text.includes('clean 3'), text.includes('editor_failed 1'), text.includes('timeout 1')].join(':');
        }, 100);
      }, 25);
    """
    dom = await _render(tmp_path, "family14", f"/operations/{op}", upstream=httpx.MockTransport(handler), smoke_script=script)
    assert 'data-nonclean-notice="true:true:true:true:true"' in dom, re.findall(r'data-nonclean-notice="[^"]*"', dom)


async def test_result_entries_are_independent_of_toggle_and_lineage_receives_focus(tmp_path):
    op = _ids("family14")["campaign_final"]
    script = """
      const timer = setInterval(() => {
        const card = document.querySelector('.status-card');
        const details = card?.querySelector('.status-card-more');
        const entry = card?.querySelector('.status-card-entry');
        const target = document.getElementById('champion-lineage');
        const nav = [...(card?.querySelectorAll('a,button') || [])].find(e => e.textContent.includes('champions recorded'));
        if (!details || !entry || !target || !nav) return;
        clearInterval(timer);
        const summaryOnlyControl = !details.querySelector('summary .status-card-entry');
        entry.click();
        const entryDidNotToggle = !details.open;
        const isButton = nav.tagName === 'BUTTON';
        const hash = location.hash;
        nav.focus(); nav.click();
        const routeAndFocus = location.hash === hash && document.activeElement === target;
        details.querySelector('summary').click();
        const expandedLabel = details.querySelector('summary').innerText.trim() === 'Show less';
        document.body.dataset.cardInteraction = [summaryOnlyControl, entryDidNotToggle, isButton, routeAndFocus, expandedLabel].join(':');
      }, 25);
    """
    dom = await _render(tmp_path, "family14", f"/operations/{op}", smoke_script=script)
    assert 'data-card-interaction="true:true:true:true:true"' in dom, re.findall(r'data-card-interaction="[^"]*"', dom)


@pytest.mark.asyncio
async def test_status_card_result_entry_stays_on_one_line_and_scrolls(tmp_path):
    op = _ids("family14")["campaign_final"]
    script = """
      const timer = setInterval(() => {
        const row = document.querySelector('.status-card-entry');
        if (!row || !row.querySelector('code')) return;
        clearInterval(timer);
        const text = row.querySelector('span').getBoundingClientRect();
        const code = row.querySelector('code').getBoundingClientRect();
        const css = getComputedStyle(row);
        document.body.dataset.rowCheck = [Math.abs(text.top - code.top) < 3,
          css.whiteSpace === 'nowrap', css.overflowX === 'auto', row.tabIndex === 0,
          row.title.includes(row.querySelector('code').textContent)].join(':');
      }, 25);
    """
    dom = await _render(tmp_path, "family14", f"/operations/{op}", smoke_script=script)
    _ok(dom, "campaign")
    assert 'data-row-check="true:true:true:true:true"' in dom


@pytest.mark.parametrize("snapshot,key,screen", [
    ("family14", "campaign_final", "campaign"),
    ("suspended-target", "campaign_suspended_target", "suspended"),
])
async def test_champion_link_scrolls_without_changing_operation_route(tmp_path, snapshot, key, screen):
    op = _ids(snapshot)[key]
    script = """
      const timer = setInterval(() => {
        const link = document.querySelector('.status-card button.status-card-link');
        const target = document.getElementById('champion-lineage');
        if (!link || !target) return;
        clearInterval(timer);
        const before = location.hash;
        window.scrollTo(0, 0);
        link.click();
        setTimeout(() => {
          document.body.dataset.lineageClick = [location.hash === before,
            !!document.querySelector('[data-screen="%s"]'), window.scrollY > 0,
            document.getElementById('champion-lineage')?.getBoundingClientRect().top < innerHeight].join(':');
        }, 100);
      }, 25);
    """ % screen
    dom = await _render(tmp_path, snapshot, f"/operations/{op}", smoke_script=script)
    assert 'data-lineage-click="true:true:true:true"' in dom, re.findall(r'data-lineage-click="[^"]*"', dom)
    _ok(dom, screen)


async def test_status_card_expansion_shows_each_result_once(tmp_path):
    op = _ids("family14")["campaign_final"]
    script = """
      const timer = setInterval(() => {
        const details = document.querySelector('.status-card-more');
        if (!details || !details.querySelector('summary')) return;
        clearInterval(timer);
        const codes = root => [...root.querySelectorAll('.status-card-entry code')].map(node => node.textContent);
        const before = codes(details.parentElement.querySelector('.status-card-visible'));
        details.querySelector('summary').click();
        const after = codes(details.parentElement);
        document.body.dataset.resultCounts = `${before.length}:${after.length}:${new Set(after).size}`;
        const expected = ['observe_gate_not_enforced', 'uncalibrated_point_estimates',
          'champion_not_contemporaneous', 'baseline_provenance_unavailable'];
        document.body.dataset.resultEntries = [before.length === 2,
          JSON.stringify(after) === JSON.stringify(expected), new Set(after).size === 4,
          before.every(code => after.includes(code))].join(':');
      }, 25);
    """
    dom = await _render(tmp_path, "family14", f"/operations/{op}", smoke_script=script)
    assert 'data-result-entries="true:true:true:true"' in dom, re.findall(r'data-result-(?:entries|counts)="[^"]*"', dom)


async def test_expanded_result_keeps_details_for_first_two_notices(tmp_path):
    import httpx
    op = _ids("family14")["campaign_final"]
    inner = mock_upstream("family14").handler

    def handler(request):
        response = inner(request)
        if request.url.path != f"/v1/operations/{op}/campaign":
            return response
        payload = response.json()
        payload.update(gate_mode="statistical", caveats=[], lab_notices=[
            {"code": "no_candidate_measured", "rounds": 3, "by_reason": {"clean": 3}, "reflection_errors": {}},
            {"code": "no_candidate_measured", "rounds": 4, "by_reason": {"clean": 4}, "reflection_errors": {}},
        ])
        return httpx.Response(200, json=payload)

    script = """
      const timer = setInterval(() => {
        const details = document.querySelector('.status-card-more');
        const extras = details?.parentElement.querySelectorAll('.status-card-visible .status-card-entry-extra');
        if (!extras || extras.length < 4) return;
        clearInterval(timer);
        const before = [...extras].every(e => getComputedStyle(e).display === 'none');
        details.querySelector('summary').click();
        setTimeout(() => {
          const after = [...extras].every(e => getComputedStyle(e).display !== 'none');
          const text = details.parentElement.textContent;
          document.body.dataset.noticeDetails = [before, after,
            text.includes('rounds 3'), text.includes('rounds 4'),
            (text.match(/All rounds were clean/g) || []).length === 2].join(':');
        }, 100);
      }, 25);
    """
    dom = await _render(tmp_path, "family14", f"/operations/{op}", upstream=httpx.MockTransport(handler), smoke_script=script)
    assert 'data-notice-details="true:true:true:true:true"' in dom, re.findall(r'data-notice-details="[^"]*"', dom)


STARTUP_S, SHUTDOWN_S = 10, 5


async def _serve(server):
    # uvicorn ends a failed startup (a taken port) with sys.exit, which asyncio would re-raise out of the
    # event loop itself; keep it inside the task as an ordinary error.
    try:
        await server.serve()
    except SystemExit as error:
        raise RuntimeError(f"uvicorn exited with code {error.code}") from None


async def _start(server):
    """serve() as a task, once it listens. A server that dies first (a port taken after free_port, a
    startup error) or never starts fails the test with its cause instead of spinning forever."""
    task = asyncio.create_task(_serve(server))
    deadline = asyncio.get_running_loop().time() + STARTUP_S
    while not server.started:
        if task.done():
            try:
                await task
            except Exception as error:
                pytest.fail(f"panel server failed to start: {error!r}")
            pytest.fail("panel server exited before it started")
        if asyncio.get_running_loop().time() > deadline:
            server.should_exit = True
            task.cancel()
            pytest.fail(f"panel server did not start within {STARTUP_S} s")
        await asyncio.sleep(0.02)
    return task


async def _stop(server, task):
    """Bounded shutdown: uvicorn gives open connections (a browser's live stream) timeout_graceful_shutdown,
    then the test forces the exit and, as a last resort, cancels the task."""
    server.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_S)
    except TimeoutError:
        server.force_exit = True
        try:
            await asyncio.wait_for(task, SHUTDOWN_S)
        except TimeoutError:
            pass  # wait_for has cancelled the task


async def _render(tmp_path, snapshot, route, *, upstream=None, budget_ms=5000, open_history=False, smoke_script=None, capture_api=False):
    port, token = free_port(), secrets.token_urlsafe(32)
    config = PanelConfig(token=token, host=HOST, port=port, started_at="2026-09-30T00:00:00Z",
                         home=tmp_path / "home", socket_path=tmp_path / "none.sock")
    static = None
    if open_history or smoke_script is not None or capture_api:
        # Drive the real components with the existing dump-dom browser. Scripts and API capture
        # exist only in this test's copied static tree; shipped assets stay byte-identical.
        static = tmp_path / "static"
        shutil.copytree(Path(panel_app.__file__).parent / "static", static)
        index = static / "index.html"
        index.write_text(index.read_text() + '<script src="/static/smoke-history.js" defer></script>')
        (static / "smoke-history.js").write_text(smoke_script if smoke_script is not None else """
          const observer = new MutationObserver(() => {
            const title = [...document.querySelectorAll('.disclosure-title')]
              .find(node => node.textContent === 'Suspension history');
            if (title) { observer.disconnect(); title.closest('details').open = true; }
          });
          observer.observe(document, {childList: true, subtree: true});
        """)
    if capture_api:
        app_js = static / "app.js"
        app_js.write_text(app_js.read_text().replace('from "./lib/api.js"', 'from "./smoke-api.js"'))
        (static / "smoke-api.js").write_text("""
          export * from "./lib/api.js";
          import { createApi as realCreateApi } from "./lib/api.js";
          export function createApi(options) {
            const api = realCreateApi(options);
            window.smokeApi = api;
            return api;
          }
        """)
    app = RequestLog(create_panel_app(config, upstream=upstream or mock_upstream(snapshot), static_dir=static))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="critical",
                                           access_log=False, ws="none", timeout_graceful_shutdown=2))
    task = await _start(server)
    try:
        url = f"http://{HOST}:{port}/?token={token}#{route}"
        dom = await asyncio.to_thread(dump_dom, BROWSER, url, budget_ms=budget_ms, workdir=tmp_path)
        return with_requests(dom, app)
    finally:
        await _stop(server, task)


def _ok(dom, screen):
    assert f'data-screen="{screen}"' in dom, dom[-2000:]
    assert "data-screen-error" not in dom, re.findall(r"data-screen-error[^<]*<[^<]*", dom)
    assert 'class="boot"' not in dom  # the static placeholder is gone once the app renders


def _ids(name):
    return load_snapshot(name)["meta"]["ids"]


def _short_ids(name):
    """Every operation of a snapshot, as the panel shows its id (8 characters); from meta.json, so a
    re-capture with new ids does not break the smoke."""
    return [op[:8] for op in _ids(name).values()]


def _completed_champion(name):
    """The champion version shown for the completed campaign of a snapshot, read from the capture (it
    depends on what the captured run promoted, not on a constant)."""
    snap = load_snapshot(name)
    for op in snap["routes"]["/v1/operations"]["body"]["operations"]:
        core = (op.get("result") or {}).get("core") or {}
        if (core.get("campaign_outcome") or {}).get("outcome") == "completed":
            return f"Completed · champion {core['final_champion']['version']}"
    raise AssertionError(f"no completed campaign in snapshot {name}")


CASES = [
    ("final", "/projects", "projects", ["demo"]),
    ("empty", "/projects", "projects", ["No projects yet"]),
    ("final", "/projects/demo", "project", ["connect", "evolve"]),
    ("suspended-target", "/projects/demo-target", "project", ["lab resume"]),
    ("final", "/projects/demo/facts", "facts", ["tester"]),
    ("final", "/experiments", "experiments", [_completed_champion("final"), "Suspended", *_short_ids("final")]),
    ("running", "/experiments", "experiments", ["round", *_short_ids("running")]),
    ("empty", "/experiments", "experiments", ["No experiments yet"]),
    ("final", "/states", "states", ["Not better", "Infrastructure", "Ready to continue", "cleanup_first", "Not started"]),
    ("final", "/nowhere", "not_found", []),
    ("family14", f"/operations/{_ids('family14')['campaign_final']}", "campaign",
     ["Final · entry 1",
      "Campaign status", "rounds_exhausted", "Observe gate not enforced",
      "Uncalibrated point estimates", "champions recorded"]),
    ("content", f"/operations/{_ids('content')['campaign_subprocess']}", "campaign",
     ["Campaign status", "Observe gate not enforced", "Uncalibrated point estimates",
      "uncalibrated_point_estimates", "Campaign",
      # attempt and round limits: a count chip in their rows, the limits in its title (spec family14 §4.2, R7)
      ">2 limits</button>", 'title="Observe gate not enforced · observe_gate_not_enforced',
      # the round's synthesised observe mark reads as the caveat label (final review item 2): one code, one text
      ">1 limit</button>", 'title="Observe gate not enforced · observe_gate_not_enforced"']),
]


@pytest.mark.parametrize("snapshot,route,screen,texts", CASES)
async def test_screen_renders(tmp_path, snapshot, route, screen, texts):
    dom = await _render(tmp_path, snapshot, route)
    _ok(dom, screen)
    for text in texts:
        assert text in dom


async def test_family14_campaign_renders_recorded_final_and_lineage(tmp_path):
    op = _ids("family14")["campaign_final"]
    view = load_snapshot("family14")["routes"][f"/v1/operations/{op}/campaign"]["body"]
    dom = await _render(tmp_path, "family14", f"/operations/{op}")
    _ok(dom, "campaign")
    finals = [final for round_ in view["rounds"] for final in round_["final"] or []]
    # one row per final[] record, nested under its attempt (spec family14 §3.3)
    assert dom.count("Final · entry ") == len(finals)
    assert dom.count('<div class="final-entry') == len(finals)
    for final in finals:
        assert f"Final · entry {final['ordinal']}" in dom
        for member in final["members"]:
            assert member["measurement_id"] in dom
            assert f'/measurements/{member["measurement_id"]}' in dom
        for contrast in final["contrasts"]:
            assert contrast["reason"] in dom
    for text in ("Campaign setup", "manifest /15 · family 14", "Core selection", "Champion side", "Members",
                 "Contrasts", "Next baseline", "vs v0 · stored", ">Champion<"):
        assert text in dom, text
    # → vN only in the attempt row's Champion column, never in a final row (§4.4)
    for champion in view["champions"]:
        if champion["origin"] == "promoted_attempt":
            assert f"→ {champion['version']}" in unescape(dom)
    # the observe mark stays in each row as a collapsed limits chip (count, title), not a block (R7)
    chips = re.findall(r'<button type="button" class="chip-warn limits-chip"[^>]*>[^<]*</button>', dom)
    assert len(chips) == 2 and all('aria-expanded="false"' in c and "observe_gate_not_enforced" in c for c in chips), chips
    assert "row-limits" not in dom and 'aria-label="Campaign status"' in dom
    final_rows = re.findall(r'<div class="final-entry.*?</details>', dom, re.S)
    assert len(final_rows) == len(finals) and all("→" not in unescape(row) for row in final_rows)
    assert "see lab campaign" not in dom and "unsupported_schema" not in dom


async def test_campaign_setup_disclosure_expands(tmp_path):
    op = _ids("family14")["campaign_final"]
    click = """
      let groupOpened = false;
      const observer = new MutationObserver(() => {
        const group = [...document.querySelectorAll('.disclosure-title')].find(node => node.textContent === 'Campaign setup & routing');
        if (group && !groupOpened) { groupOpened = true; group.closest('details').open = true; }
        const title = [...document.querySelectorAll('.disclosure-title')].find(node => node.textContent === 'Campaign setup');
        if (title) { observer.disconnect(); title.closest('summary').click(); }
      });
      observer.observe(document, {childList: true, subtree: true});
    """
    closed = await _render(tmp_path / "closed", "family14", f"/operations/{op}")
    dom = await _render(tmp_path / "open", "family14", f"/operations/{op}", smoke_script=click)
    _ok(dom, "campaign")
    setup = re.search(r'<details class="disclosure"[^>]*>\s*<summary[^>]*>(?:(?!</summary>).)*?Campaign setup</span>', dom, re.S)
    assert setup and " open" in setup.group(0).split(">")[0], "the setup block did not expand on click"
    assert not re.search(r'<details class="disclosure" open[^>]*>\s*<summary[^>]*>(?:(?!</summary>).)*Campaign setup</span>', closed, re.S)
    assert "Baseline refresh" in dom and ("No refresh records" in dom or "setup-refresh" in dom)


async def test_final_row_expands_to_members_and_contrasts(tmp_path):
    op = _ids("family14")["campaign_final"]
    final = load_snapshot("family14")["routes"][f"/v1/operations/{op}/campaign"]["body"]["rounds"][0]["final"][0]
    click = """
      const observer = new MutationObserver(() => {
        const summary = document.querySelector('.final-entry > details > summary');
        if (summary) { observer.disconnect(); summary.click(); }
      });
      observer.observe(document, {childList: true, subtree: true});
    """
    closed = await _render(tmp_path / "closed", "family14", f"/operations/{op}")
    dom = await _render(tmp_path / "open", "family14", f"/operations/{op}", smoke_script=click)
    _ok(dom, "campaign")
    assert re.search(r'<div class="final-entry"><details class="disclosure"[^>]*>', closed)
    assert not re.search(r'<div class="final-entry"><details class="disclosure" open', closed)
    row = re.search(r'<div class="final-entry"><details class="disclosure" open[^>]*>.*?</details>', dom, re.S)
    assert row, "the final row did not expand on click"
    body = row.group(0).split("</summary>", 1)[1]
    for text in ("Members", "Role", "Complete", "Critical", "Contrasts", "Bound · lower / level / exact",
                 "Removed pairs · cases", "Core selection", "Listed", "Next baseline"):
        assert text in body, text
    for member in final["members"]:
        assert f'/measurements/{member["measurement_id"]}' in body
    for contrast in final["contrasts"]:
        assert contrast["unavailable"] in body and contrast["pairing"] in body
    assert final["selection"]["reason"] in body


def _historical_view7_as_current(name):
    """The fixture handler of ``name`` serving its recorded family-14 ``lab-campaign-view/7`` campaign answers as
    ``/15`` with explicit unavailable Owner probe and baseline notice facts. The capture lacks these records;
    the ``suspension-fault`` snapshot stays dev52/b6 by the user's decision (2026-10-19)."""
    import httpx
    inner = mock_upstream(name).handler

    def handler(request):
        response = inner(request)
        if request.url.path.endswith("/campaign") and response.status_code == 200:
            body = json.loads(response.content)
            if body.get("schema") == "lab-campaign-view/7" and body.get("family") == 14:
                return httpx.Response(200, json={**body, "schema": "lab-campaign-view/15",
                    "lab_notices_state": "unavailable", "reflector_probe": None, "reflector_probe_state": "unavailable",
                    "reflector_probe_reason": "unreadable"})
        return response

    return handler


async def test_scorer_fault_renders_actual_evidence_rows(tmp_path):
    import httpx
    op = _ids("suspension-fault")["campaign_scorer_fault"]
    evidence = load_snapshot("suspension-fault")["routes"][f"/v1/operations/{op}/suspensions"]["body"]["suspensions"][-1]
    dom = await _render(tmp_path, "suspension-fault", f"/operations/{op}",
                        upstream=httpx.MockTransport(_historical_view7_as_current("suspension-fault")))
    _ok(dom, "suspended")
    for text in ("Current pause", "fault_rows", "scorer_metric_missing", "null",
                 "Rows that caused this pause; this is not the full remaining sample list."):
        assert text in dom
    # the evidence block's own markup, not loose words (final review item 11): count · digest · complete, Retry header
    meta, digest = evidence["evidence"], evidence["evidence"]["rows_digest"]
    rows = f"{meta['count']} {'row' if meta['count'] == 1 else 'rows'}"
    assert (f'<span class="ev-count muted">{rows} · digest <span class="mono" title="{digest}">{digest[:8]}</span>'
            " · complete</span>") in dom
    assert "<th>Retry</th>" in dom
    row = evidence["rows"][0]
    assert f'/measurements/{row["measurement_id"]}' in dom
    assert f'<code>{row["case_id"]}</code>' in dom
    assert f'<code>{row["sample_id"]}</code>' in dom
    assert f'<td>{row["retry_ordinal"]}</td>' in dom


async def test_resumed_campaign_reads_history_only_when_opened(tmp_path):
    """Closed history reads nothing; disclosure reads exactly once after campaign settlement."""
    import httpx
    op = _ids("details")["campaign_resumed"]
    path = f"/v1/operations/{op}/suspensions"
    inner, seen = mock_upstream("details").handler, []

    def handler(request):
        seen.append(request.url.path)
        return inner(request)

    upstream = httpx.MockTransport(handler)
    dom = await _render(tmp_path, "details", f"/operations/{op}", upstream=upstream)
    _ok(dom, "campaign")
    assert "Suspension history" in dom and path not in seen
    count = len(load_snapshot("details")["routes"][f"/v1/operations/{op}/campaign"]["body"]["suspensions"])
    recorded = f"{count} {'pause' if count == 1 else 'pauses'} recorded"
    assert f"{recorded} · loaded when opened" in dom
    dom = await _render(tmp_path, "details", f"/operations/{op}", upstream=upstream, open_history=True)
    _ok(dom, "campaign")
    assert seen.count(path) == 1
    # once open the summary keeps the count and drops "loaded when opened" (final review item 6)
    assert recorded in dom and "loaded when opened" not in dom
    for text in (">Historical<", "target_unstable", "threshold", "Observed", "Limit", "max_rate", "0.500000000000"):
        assert text in dom
    assert "Current pause" not in dom


async def test_live_campaign_row_and_operation_card(tmp_path):
    op = _ids("running")["campaign_running"]
    dom = await _render(tmp_path, "running", f"/operations/{op}")
    _ok(dom, "campaign")
    assert op in dom


@pytest.mark.parametrize("snapshot,key,texts", [
    ("suspended-target", "campaign_suspended_target", ["target_unstable", "Latest pause evidence", "lab resume", "Baseline breakdown by owner"]),
    ("suspended-role", "campaign_suspended_role", ["editor_failing", "lab resume"]),
])
async def test_suspended_screen_renders(tmp_path, snapshot, key, texts):
    dom = await _render(tmp_path, snapshot, f"/operations/{_ids(snapshot)[key]}")
    _ok(dom, "suspended")
    for text in texts:
        assert text in dom
    # Spec §4.1: status, continuation, pause evidence, rounds, starts, baseline, download, folded setup.
    marks = ['aria-label="Campaign status"', "lab resume", "Latest pause evidence", "Rounds &amp; attempts",
             'id="champion-lineage"', ">Starts<", "Baseline breakdown by owner", "Download report (.md)", "Campaign setup &amp; routing"]
    at = [dom.find(m) for m in marks]
    assert all(i >= 0 for i in at), dict(zip(marks, at))
    assert at == sorted(at), dict(zip(marks, at))
    assert 'class="cols2"' not in dom
    assert "Suspended by" not in dom
    assert '<button type="button" class="link status-card-link">' in dom
    assert "Champion lineage" in dom
    assert re.search(r'<details class="disclosure"[^>]*>\s*<summary>.*?Campaign setup &amp; routing', dom)


async def test_suspended_card_shows_pause_and_last_start_failure_together(tmp_path):
    import httpx

    name = "suspended-role"
    op = _ids(name)["campaign_suspended_role"]
    operation_path = f"/v1/operations/{op}"
    inner = mock_upstream(name).handler

    def handler(request):
        response = inner(request)
        if request.url.path == operation_path:
            payload = response.json()
            payload["failure"] = {"code": "worker_failed", "stage": "worker"}
            return httpx.Response(200, json=payload)
        return response

    dom = await _render(tmp_path, name, f"/operations/{op}", upstream=httpx.MockTransport(handler))
    _ok(dom, "suspended")
    assert "the last start recorded worker_failed" in dom
    assert "editor_failing" in dom
    assert "did not run" not in dom


async def test_failed_campaign_read_is_shown_in_the_baseline_section(tmp_path):
    op = _ids("suspended-target")["campaign_suspended_target"]
    upstream = _failing("suspended-target", {f"/v1/operations/{op}/campaign": "campaign_refused"})
    dom = await _render(tmp_path, "suspended-target", f"/operations/{op}", upstream=upstream)
    _ok(dom, "suspended")
    assert "Unavailable (<code>campaign_refused</code>)" in dom
    assert "Loading the campaign" not in dom
    lineage = dom.split('id="champion-lineage"', 1)[1].split('</section>', 1)[0]
    assert "Unavailable (<code>campaign_refused</code>)" in lineage


async def test_service_down_shows_the_banner(tmp_path):
    import httpx
    def down(request):
        raise httpx.ConnectError("down", request=request)
    dom = await _render(tmp_path, "final", "/projects", upstream=httpx.MockTransport(down))
    assert "lab serve --home" in dom
    _ok(dom, "projects")


def _failing(name, failures):
    """The fixture upstream, except that each path of `failures` answers {path: code} with a 409 error — the
    shape Lab gives, e.g., ambiguous_implementation to an unqualified status of a project with several
    active implementations."""
    import httpx
    inner = mock_upstream(name).handler

    def handler(request):
        code = failures.get(canonical(request.url.raw_path.decode("latin-1")))
        if code is not None:
            return httpx.Response(409, json={"error": {"code": code, "stage": "fixture", "message": "smoke"}})
        return inner(request)

    return httpx.MockTransport(handler)


# Tenet inspectable-experiments: a joined read that fails is shown with its code, never as absence.
async def test_failed_joined_reads_are_shown_not_emptied(tmp_path):
    upstream = _failing("final", {"/v1/projects/demo/status": "ambiguous_implementation",
                                  canonical("/v1/projects/demo/operations?view=summary"): "summary_refused"})
    dom = await _render(tmp_path, "final", "/projects", upstream=upstream)
    _ok(dom, "projects")
    assert "Unavailable (<code>ambiguous_implementation</code>)" in dom   # the demo row's status cells
    assert "Unavailable (<code>summary_refused</code>)" in dom            # the demo sidebar group
    assert "none yet" not in dom


async def test_failed_operation_read_keeps_the_resume_card(tmp_path):
    op = _ids("suspended-role")["campaign_suspended_role"]
    upstream = _failing("suspended-role", {f"/v1/operations/{op}": "operation_refused"})
    dom = await _render(tmp_path, "suspended-role", "/projects/demo", upstream=upstream)
    _ok(dom, "project")
    card = re.search(r'<section class="panel resume-card[^"]*" data-resume="[^"]*">.*?</section>', dom, re.S)
    assert card, dom[-3000:]
    assert "Unavailable (<code>operation_refused</code>)" in card.group(0)


async def test_failed_operation_read_of_an_active_cycle_is_shown(tmp_path):
    op = _ids("running")["campaign_running"]
    upstream = _failing("running", {f"/v1/operations/{op}": "operation_refused"})
    dom = await _render(tmp_path, "running", "/projects/demo", upstream=upstream)
    _ok(dom, "project")
    heads = [h for h in re.findall(r'<div class="pipeline-head">(.*?)</div><div class="steps">', dom, re.S) if op[:8] in h]
    assert len(heads) == 1, dom[-3000:]
    assert "Unavailable (<code>operation_refused</code>)" in heads[0]


# Codex r2 residual: summaries and aggregates show a missing read, not a guess.
async def test_active_bar_and_project_count_show_failed_reads(tmp_path):
    op = _ids("running")["campaign_running"]
    upstream = _failing("running", {f"/v1/operations/{op}": "operation_refused"})
    dom = await _render(tmp_path, "running", "/projects", upstream=upstream)
    _ok(dom, "projects")
    bar = re.search(r'<a class="active-bar[^\"]*".*?</a>', dom, re.S)
    assert bar and "Unavailable (<code>operation_refused</code>)" in bar.group(0), dom[-3000:]

    upstream = _failing("final", {canonical("/v1/projects/demo/operations?view=summary"): "summary_refused"})
    dom = await _render(tmp_path, "final", "/experiments", upstream=upstream)
    _ok(dom, "experiments")
    assert "? projects (Unavailable (<code>summary_refused</code>))" in dom
    assert "across 0 projects" not in dom


async def test_mixed_campaign_shows_attempts_failure_and_strip(tmp_path):
    op = _ids("details")["campaign_mixed"]
    dom = await _render(tmp_path, "details", f"/operations/{op}")
    _ok(dom, "campaign")
    view = load_snapshot("details")["routes"][f"/v1/operations/{op}/campaign"]["body"]
    attempt_ids = [a["attempt_id"] for r in view["rounds"] for a in r["attempts"]]
    for text in ("Round 1", "baseline v0", *attempt_ids, "No candidate", "Champion lineage", "baseline r1", "2/2 scored",
                 "no final", "Final · entry 1"):
        assert text in dom, text
    assert re.search(r"editor · editor", dom)
    assert 'href="#/operations/%s/diff?from=initial&amp;to=champion:v1"' % op in dom or "to=champion:v1" in dom


async def test_unavailable_champions_and_details_are_shown(tmp_path):
    import copy
    import httpx
    op = _ids("details")["campaign_mixed"]
    body = copy.deepcopy(load_snapshot("details")["routes"][f"/v1/operations/{op}/campaign"]["body"])
    body.update(champions=None, champions_state="unavailable", details_state="unavailable", details_reason="core_raw_unsupported")
    for r in body["rounds"]:
        r["baseline_version"] = None  # null next to details_state, as owner/campaign_views.py serves it
        for a in r["attempts"]:
            a["failure"] = None
    inner = mock_upstream("details").handler

    def handler(request):
        if request.url.raw_path.decode("latin-1") == f"/v1/operations/{op}/campaign":
            return httpx.Response(200, json=body)
        return inner(request)

    dom = await _render(tmp_path, "details", f"/operations/{op}", upstream=httpx.MockTransport(handler))
    _ok(dom, "campaign")
    assert "No champions yet" not in dom
    assert dom.count("core_raw_unsupported") >= 3
    assert "baseline Unavailable" in dom      # codex r1: the round title does not say "baseline —"
    assert "baseline —" not in dom
    # lineage unavailable: every attempt's Champion cell says so, never "—" (spec family14 §3.3)
    cells = re.findall(r'<span class="mono champion-cell[^"]*"[^>]*>([^<]*)</span>', dom)
    attempts = sum(len(r["attempts"]) for r in body["rounds"])
    assert len(cells) == attempts and set(cells) == {"unavailable"}, cells


def _attempt_by_group(name, role, group):
    view = load_snapshot(name)["routes"][f"/v1/operations/{_op(name, role)}/campaign"]["body"]
    return next(a for r in view["rounds"] for a in r["attempts"] if a["group"] == group and a["measurement_id"])


async def test_measurement_screen_shows_attempt_diff_reflector_and_cases(tmp_path):
    op = _ids("details")["campaign_mixed"]
    dom = await _render(tmp_path, "details", f"/operations/{op}/measurements/{_attempt_by_group('details', 'campaign_mixed', 'accepted')['measurement_id']}")
    _ok(dom, "measurement")
    for text in ("Attempt", "Promoted → v1", "Changes against round baseline v0", "skills/SKILL.md", "Open full diff",
                 "Reflector · round 1", "ADR-0019", "the skill&#39;s first line does not carry", "Which hypothesis this attempt used",
                 "Editor tokens", "Baseline v0", "Comparison", "2/2 scored"):
        assert text.replace("&#39;", "'") in dom.replace("&#39;", "'"), text


async def test_family14_observe_attempt_measurement_shows_confidence(tmp_path):
    op = _ids("family14")["campaign_final"]
    view = load_snapshot("family14")["routes"][f"/v1/operations/{op}/campaign"]["body"]
    attempt = next(a for r in view["rounds"] for a in r["attempts"]
                   if (a["confidence"] or {}).get("latest") and a["measurement_id"])
    dom = await _render(tmp_path, "family14", f"/operations/{op}/measurements/{attempt['measurement_id']}")
    _ok(dom, "measurement")
    assert "Confidence" in dom and "observe · bounds not computed" in dom
    # Observe: the point with "—" bounds and no drawing (spec family14 §3.5; mock 5 · OBSERVE).
    focus = re.search(r'<div class="ci-row" data-interval="focus">(.*?)</div>', dom, re.S)
    assert focus and "[—, —]" in focus.group(1) and "<svg" not in focus.group(1), dom[-3000:]
    assert "Uncalibrated point estimates" in dom and "uncalibrated_point_estimates" in dom
    assert f"Attempt {attempt['attempt_id']} · round 1" in dom


async def test_measurement_screen_states_of_failed_reads(tmp_path):
    import httpx
    op = _ids("details")["campaign_mixed"]
    inner = mock_upstream("details").handler

    def handler(request):
        if request.url.raw_path.decode("latin-1").startswith(f"/v1/operations/{op}/diff"):
            return httpx.Response(409, json={"error": {"code": "diff_refused"}})
        return inner(request)

    measured = _attempt_by_group("details", "campaign_mixed", "accepted")
    dom = await _render(tmp_path, "details", f"/operations/{op}/measurements/{measured['measurement_id']}", upstream=httpx.MockTransport(handler))
    _ok(dom, "measurement")
    assert "diff_refused" in dom
    assert "No candidate tree" not in dom


async def test_measurement_and_diff_screens_with_unavailable_champions_say_so(tmp_path):
    import copy
    import httpx
    op = _ids("details")["campaign_mixed"]
    body = copy.deepcopy(load_snapshot("details")["routes"][f"/v1/operations/{op}/campaign"]["body"])
    body.update(champions=None, champions_state="unavailable")
    for r in body["rounds"]:
        for a in r["attempts"]:
            a["has_tree"] = None
    inner = mock_upstream("details").handler

    def handler(request):
        if request.url.raw_path.decode("latin-1") == f"/v1/operations/{op}/campaign":
            return httpx.Response(200, json=body)
        return inner(request)

    mid = _attempt_by_group("details", "campaign_mixed", "accepted")["measurement_id"]
    dom = await _render(tmp_path, "details", f"/operations/{op}/measurements/{mid}", upstream=httpx.MockTransport(handler))
    _ok(dom, "measurement")
    assert "No candidate tree" not in dom
    assert "Unavailable (<code>unavailable</code>)" in dom
    dom = await _render(tmp_path, "details", f"/operations/{op}/diff", upstream=httpx.MockTransport(handler))
    _ok(dom, "diff")
    assert "Unavailable (<code>unavailable</code>)" in dom


async def test_report_download_follows_the_results_state(tmp_path):
    """Spec 2026-10-14 report-markdown §5: the button while results are present, on campaign, suspended and
    evaluation screens; none for deleted results."""
    ids = _ids("details")
    for route in (f"/operations/{ids['campaign_mixed']}", f"/operations/{ids['evaluation']}"):
        assert "Download report (.md)" in await _render(tmp_path, "details", route), route
    suspended = _ids("suspended-target")["campaign_suspended_target"]
    assert "Download report (.md)" in await _render(tmp_path, "suspended-target", f"/operations/{suspended}")
    assert "Download report (.md)" not in await _render(tmp_path, "details", f"/operations/{ids['campaign_finished']}")


async def test_evaluation_screen_via_operation_and_measurement_routes(tmp_path):
    op = _ids("details")["evaluation"]
    for route in (f"/operations/{op}", f"/operations/{op}/measurements/c-{op}"):
        dom = await _render(tmp_path, "details", route)
        _ok(dom, "evaluation")
        for text in ("Evaluation", "Mean score", "Critical pass", "1 / 1", "Coverage", "Samples", "0 pass · 1 fail"):
            assert text in dom, (route, text)


async def test_diff_screen_renders_files_and_pairs(tmp_path):
    op = _ids("details")["campaign_mixed"]
    accepted = _attempt_by_group("details", "campaign_mixed", "accepted")["attempt_id"]
    routes = load_snapshot("details")["routes"]
    for route in (f"/operations/{op}/diff", f"/operations/{op}/diff?from=champion:v0&to=attempt:{accepted}"):
        dom = await _render(tmp_path, "details", route)
        _ok(dom, "diff")
        lines = routes[f"/v1{route}"]["body"]["text"].splitlines()
        for line in lines:
            if line.startswith("+++ b/"):
                assert line[6:] in dom, (route, line)
            elif line.startswith("@@"):
                assert line in dom, (route, line)
        expected_changes = [("add", line) if line.startswith("+") else ("remove", "−" + line[1:])
                            for line in lines if line.startswith(("+", "-"))
                            and not line.startswith(("+++ ", "--- "))]
        rendered_changes = [(kind, unescape(text)) for kind, text in
                            re.findall(r'<div class="diff-line diff-(add|remove)">([^<]*)</div>', dom)]
        assert expected_changes and rendered_changes == expected_changes, (route, rendered_changes)
        assert ("diff-line diff-remove" in dom) == any(kind == "remove" for kind, _ in expected_changes), route
        assert "the original agent" in dom and f"attempt {accepted} against v0" in dom, route


async def test_diff_screen_shows_the_code_of_a_refused_read(tmp_path):
    op = _ids("final")["campaign_finished"]
    dom = await _render(tmp_path, "final", f"/operations/{op}/diff")
    _ok(dom, "diff")
    assert "results_deleted" in dom


# Branch 2, Task 8: what the earlier screen tests above do not already assert.
def _op(name, role):
    return _ids(name)[role]


def _first_attempt(name, role):
    """The first attempt of the operation with a measurement, from the fixture (ids change on a re-capture)."""
    view = load_snapshot(name)["routes"][f"/v1/operations/{_op(name, role)}/campaign"]["body"]
    return next(a for r in view["rounds"] for a in r["attempts"] if a["measurement_id"])


EXPERIMENT_CASES = [
    ("running", lambda: f"/operations/{_op('running', 'campaign_running')}", "campaign", ["In progress"]),
    ("details", lambda: f"/operations/{_op('details', 'campaign_mixed')}/measurements/{_first_attempt('details', 'campaign_mixed')['measurement_id']}", "measurement",
     ["Core does not record this", "Not shown in panel v1", "ADR-0019", "Undetermined and infrastructure cells: reason not read by Lab"]),
    ("suspended-target", lambda: f"/operations/{_op('suspended-target', 'campaign_suspended_target')}", "suspended",
     ["lab agent stop", "on a fresh target"]),
    ("details", lambda: f"/operations/{_op('details', 'campaign_mixed')}/diff", "diff", ["against"]),
]


@pytest.mark.parametrize("snapshot,route,screen,texts", EXPERIMENT_CASES)
async def test_experiment_screen_renders(tmp_path, snapshot, route, screen, texts):
    dom = await _render(tmp_path, snapshot, route())
    _ok(dom, screen)
    for text in texts:
        assert text in dom, text


async def _diff_with(tmp_path, body_patch):
    """The details snapshot, except that the mixed campaign's default diff answers 200 with `body_patch` merged in."""
    import copy
    import httpx
    op = _ids("details")["campaign_mixed"]
    body = copy.deepcopy(load_snapshot("details")["routes"][f"/v1/operations/{op}/diff"]["body"])
    body.update(body_patch)
    inner = mock_upstream("details").handler

    def handler(request):
        if request.url.raw_path.decode("latin-1").startswith(f"/v1/operations/{op}/diff"):
            return httpx.Response(200, json=body)
        return inner(request)

    return await _render(tmp_path, "details", f"/operations/{op}/diff", upstream=httpx.MockTransport(handler))


async def test_diff_screen_no_change(tmp_path):
    dom = await _diff_with(tmp_path, {"state": "no_change", "text": "", "nontext_changes": []})
    _ok(dom, "diff")
    assert "No changes between these versions" in dom
    assert "diff-add" not in dom


async def test_diff_screen_unparsed_text_is_shown_by_its_code(tmp_path):
    dom = await _diff_with(tmp_path, {"state": "changed", "text": "this is not a unified diff\n"})
    _ok(dom, "diff")
    assert "diff_unparsed" in dom
    assert "diff-add" not in dom


# Codex r1 (tenet inspectable-experiments): values derived from an unavailable section keep its state on screen.
def _campaign_patched(op, patch):
    """The details snapshot, except that the campaign view of `op` answers with `patch(body)`."""
    import copy
    import httpx
    body = patch(copy.deepcopy(load_snapshot("details")["routes"][f"/v1/operations/{op}/campaign"]["body"]))
    inner = mock_upstream("details").handler

    def handler(request):
        if request.url.raw_path.decode("latin-1") == f"/v1/operations/{op}/campaign":
            return httpx.Response(200, json=body)
        return inner(request)

    return httpx.MockTransport(handler)


def _details_unavailable(body, champions=True):
    body.update(details_state="unavailable", details_reason="core_raw_unsupported")
    if not champions:
        body.update(champions=None, champions_state="unavailable")
    for r in body["rounds"]:
        r.update(baseline_version=None, reflection=None)
        for a in r["attempts"]:
            a["failure"] = None
            if not champions:
                a["has_tree"] = None
    return body


async def test_measurement_with_unavailable_sections_says_so_in_editor_and_header(tmp_path):
    op = _ids("details")["campaign_mixed"]
    upstream = _campaign_patched(op, lambda b: _details_unavailable(b, champions=False))
    mid = _attempt_by_group("details", "campaign_mixed", "accepted")["measurement_id"]
    dom = await _render(tmp_path, "details", f"/operations/{op}/measurements/{mid}", upstream=upstream)
    _ok(dom, "measurement")
    assert "No failure recorded" not in dom
    editor = re.search(r'<div class="strong">Editor</div>.*?Editor tokens', dom, re.S)
    assert editor and "Unavailable (<code>core_raw_unsupported</code>)" in editor.group(0), dom[-3000:]
    transition = re.search(r'<div data-fact="champion"><div class="lbl">Champion transition</div>(.*?)</div></div>', dom, re.S)
    assert transition and unescape(re.sub(r"<[^>]+>", " ", transition.group(1))).split() == ["Lineage", "unavailable", "unavailable"], dom[-3000:]
    assert "→ v" not in dom
    assert "baseline Unavailable" in dom


async def test_diff_pairs_with_unavailable_details_name_the_attempt_versions(tmp_path):
    op = _ids("details")["campaign_mixed"]
    dom = await _render(tmp_path, "details", f"/operations/{op}/diff", upstream=_campaign_patched(op, _details_unavailable))
    _ok(dom, "diff")
    pairs = re.search(r'<div class="diff-pairs">(.*?)</div></div>', dom, re.S)
    assert pairs and "Attempt versions" in pairs.group(1), dom[-3000:]
    assert "Unavailable (<code>core_raw_unsupported</code>)" in pairs.group(1)
    assert "to=attempt:" not in pairs.group(1)


async def test_suspended_resume_command_from_a_stale_report_is_marked_stale(tmp_path):
    """The first health probe fails (an outage), the next succeeds (recovery: every resource is read again).
    On that re-read the report fails: the command stays, from the last answer, marked stale — the operation
    and campaign reads are fresh, so only the report can put the mark there."""
    import httpx
    op = _ids("suspended-target")["campaign_suspended_target"]
    inner = mock_upstream("suspended-target").handler
    seen = {"health": 0, "report": 0}

    def handler(request):
        path = request.url.raw_path.decode("latin-1")
        if path == "/v1/health":
            seen["health"] += 1
            if seen["health"] == 1:
                raise httpx.ConnectError("down", request=request)
        if path == f"/v1/operations/{op}/report":
            seen["report"] += 1
            if seen["report"] > 1:
                return httpx.Response(409, json={"error": {"code": "report_refused"}})
        return inner(request)

    dom = await _render(tmp_path, "suspended-target", f"/operations/{op}", upstream=httpx.MockTransport(handler), budget_ms=9000)
    _ok(dom, "suspended")
    assert seen["report"] >= 2, seen
    card = re.search(r'<section class="panel resume-card[^"]*" data-resume="[^"]*">.*?</section>', dom, re.S)
    assert card, dom[-3000:]
    assert "lab resume" in card.group(0)
    assert "Ready target from the last report read" in card.group(0)
    assert '<span class="small stale-mark">stale</span>' in card.group(0)


# Codex r1 #3: the schema of the campaign view is checked before any field is read, on every screen that reads it.
# A newer view whose fields changed shape renders Unavailable (unsupported_schema) — never a screen error — and no
# measurement listed in it is fetched.
_V10 = {"schema": "lab-campaign-view/10", "family": 14, "measurements": {}, "rounds": {}, "champions": {}, "lab_notices": {},
       "caveats": {}, "suspensions": {}, "outcome": "completed", "progress": 5}


@pytest.mark.parametrize("snapshot,role,suffix,screen", [
    ("details", "campaign_mixed", "", "campaign"),
    ("suspended-target", "campaign_suspended_target", "", "suspended"),
    ("details", "campaign_mixed", "/diff", "diff"),
    ("details", "campaign_mixed", "/measurements/{m}", "measurement"),
])
async def test_campaign_view_of_another_version_is_unavailable_on_every_screen(tmp_path, snapshot, role, suffix, screen):
    import httpx
    op = _op(snapshot, role)
    m = _first_attempt("details", "campaign_mixed")["measurement_id"] if "{m}" in suffix else None
    inner, seen = mock_upstream(snapshot).handler, []

    def handler(request):
        path = request.url.raw_path.decode("latin-1")
        seen.append(path)
        if path == f"/v1/operations/{op}/campaign":
            return httpx.Response(200, json=_V10)
        return inner(request)

    dom = await _render(tmp_path, snapshot, f"/operations/{op}{suffix.format(m=m)}",
                        upstream=httpx.MockTransport(handler))
    _ok(dom, screen)
    assert "Unavailable (<code>unsupported_schema</code>)" in dom
    assert "Core outcome" not in dom
    if screen == "suspended":
        for marker in ('aria-label="Campaign status"', '<h2>Rounds &amp; attempts</h2>',
                       'id="champion-lineage"', 'Campaign setup &amp; routing'):
            start = dom.index(marker)
            # Baseline and folded setup have their own nested blocks; only require their read indication
            # before the next outer section/disclosure, not an unrelated error elsewhere on the page.
            end = dom.find('</details>' if marker == 'Campaign setup &amp; routing' else '</section>', start)
            assert end > start, marker
            assert "unsupported_schema" in dom[start:end], marker
        baseline_end = dom.index('Baseline breakdown by owner')
        baseline_start = dom.rfind('<section class="block">', 0, baseline_end)
        assert "unsupported_schema" in dom[baseline_start:baseline_end]
    fetched = [p for p in seen if "/measurements/" in p and (m is None or not p.endswith(f"/measurements/{m}"))]
    assert fetched == [], fetched


async def test_content_attempt_measurement_shows_role_cards(tmp_path):
    op = _ids("content")["campaign_chat"]
    view = load_snapshot("content")["routes"][f"/v1/operations/{op}/campaign"]["body"]
    attempt = next(a for r in view["rounds"] for a in r["attempts"] if a["measurement_id"])
    dom = await _render(tmp_path, "content", f"/operations/{op}/measurements/{attempt['measurement_id']}")
    _ok(dom, "measurement")
    assert "Editor · chat" in dom and "Tool call" in dom
    assert "Reflector · chat" in dom and "(private)" in dom
    assert "Show private content" in dom.split("Reflector · chat", 1)[1].split("</section>", 1)[0]
    assert ">Not read by Lab yet</div>" not in dom.split("Editor · chat", 1)[1].split("</section>", 1)[0]


async def test_case_browser_selects_and_steps(tmp_path):
    """Spec family14 §3.7: the first case is selected above the table; private content is read only on the
    button; ArrowDown steps to the next case and a click on a row selects it, each starting withheld again."""
    import httpx
    op = _ids("content")["campaign_chat"]
    view = load_snapshot("content")["routes"][f"/v1/operations/{op}/campaign"]["body"]
    mid = next(a for r in view["rounds"] for a in r["attempts"] if a["measurement_id"])["measurement_id"]
    samples = load_snapshot("content")["routes"][f"/v1/operations/{op}/measurements/{mid}"]["body"]["measurement"]["samples"]
    first, second = (f'{s["case_id"]} · {s["sample_id"]}' for s in samples[:2])
    inner, seen = mock_upstream("content").handler, []

    def handler(request):
        seen.append(request.url.raw_path.decode("latin-1"))
        return inner(request)

    steps = """
      const log = (t) => { let el = document.getElementById('smoke-log');
        if (!el) { el = document.createElement('div'); el.id = 'smoke-log'; document.body.appendChild(el); }
        el.textContent += t + '|'; };
      const head = () => document.querySelector('.case-detail .case-id')?.textContent;
      const reveal = () => [...document.querySelectorAll('.case-detail button')].find(b => b.textContent === 'Show private content');
      // Shown = the private answer is on screen; while it loads the public trace stays (Task 10 item 3).
      const shown = () => { const t = document.querySelector('.case-detail')?.textContent ?? '';
        return t.includes('Showing private content') && !t.includes('Loading private content'); };
      let step = 0, start = null;
      const timer = setInterval(() => {
        if (step === 0 && reveal()) { start = head(); log('first:' + start); reveal().click(); step = 1; }
        else if (step === 1 && shown()) { log('private:' + head());
          document.querySelector('.cases-row.selected').dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
          step = 2; }
        else if (step === 2 && head() !== start) {
          log('down:' + head() + ':' + (shown() ? 'shown' : 'withheld'));
          document.querySelectorAll('.cases-row')[0].click(); step = 3; }
        else if (step === 3 && reveal()) { log('click:' + head() + ':' + (shown() ? 'shown' : 'withheld')); clearInterval(timer); }
      }, 50);
    """
    dom = await _render(tmp_path, "content", f"/operations/{op}/measurements/{mid}", upstream=httpx.MockTransport(handler),
                        smoke_script=steps)
    _ok(dom, "measurement")
    log = unescape(re.search(r'<div id="smoke-log">(.*?)</div>', dom, re.S).group(1)).split("|")
    assert log[:4] == [f"first:{first}", f"private:{first}", f"down:{second}:withheld", f"click:{first}:withheld"], log
    assert re.search(r'<button type="button" class="cases-grid cols-\d+ cases-row selected" aria-current="true">', dom)
    private = [p for p in seen if "/cases/" in p and "disclose=private" in p]
    # One private read per side of the first case, on the button only; none for the stepped-to case.
    assert len(private) == 2 and all(f'/cases/{samples[0]["case_id"]}/' in p for p in private), private


async def test_content_campaign_shows_failure_text(tmp_path):
    op = _ids("content")["campaign_chat"]
    dom = await _render(tmp_path, "content", f"/operations/{op}")
    _ok(dom, "campaign")
    assert "editor_log" in dom


@pytest.mark.parametrize("state,reason,failure_text", [
    ("unavailable", "log_missing", None),
    ("available", "unreadable", None),
    ("available", "unreadable", "readable terminal failure excerpt"),
])
async def test_content_campaign_failure_shows_role_read_reason(tmp_path, state, reason, failure_text):
    import copy
    import httpx
    op = _ids("content")["campaign_chat"]
    routes = load_snapshot("content")["routes"]
    view = routes[f"/v1/operations/{op}/campaign"]["body"]
    attempt = next(a for r in view["rounds"] for a in r["attempts"] if a["failure"])
    path = f"/v1/operations/{op}/attempts/{attempt['attempt_id']}"
    body = copy.deepcopy(routes[path]["body"])
    body["editor"].update(state=state, reason=reason, events=[])
    body["editor"]["failure"].pop("text", None)
    body["editor"]["failure"].pop("text_source", None)
    if failure_text is not None:
        body["editor"]["failure"].update(text=failure_text, text_source="editor_log")
    inner = mock_upstream("content").handler

    def handler(request):
        if canonical(request.url.raw_path.decode("latin-1")) == path:
            return httpx.Response(200, json=body)
        return inner(request)

    dom = await _render(tmp_path, "content", f"/operations/{op}", upstream=httpx.MockTransport(handler))
    _ok(dom, "campaign")
    rows = re.findall(r'<div class="attempt">.*?(?=<div class="attempt">|<div class="round"|$)', dom, re.S)
    row = next(row for row in rows if f">{attempt['attempt_id']}</" in row)
    assert reason in row, row
    if state == "unavailable":
        assert "Unavailable" in row
    if failure_text is not None:
        assert failure_text in row
    else:
        assert '<pre class="content-text"' not in row


@pytest.mark.parametrize("snapshot,key,screen", [
    ("suspension-fault", "campaign_scorer_fault", "suspended"),
    ("details", "campaign_resumed", "campaign"),
])
@pytest.mark.parametrize("campaign_fails", [False, True])
async def test_history_waits_for_campaign_settlement(tmp_path, snapshot, key, screen, campaign_fails):
    """Real screen props/hooks wait even when ordinary disclosure opens during loading."""
    import httpx
    op = _ids(snapshot)[key]
    campaign_path, history_path = f"/v1/operations/{op}/campaign", f"/v1/operations/{op}/suspensions"
    inner = (_historical_view7_as_current(snapshot) if snapshot == "suspension-fault" else mock_upstream(snapshot).handler)
    settled, history_before_settlement, history_reads = False, [], []

    async def handler(request):
        nonlocal settled
        if request.url.path == campaign_path:
            await asyncio.sleep(0.2)
            settled = True
            if campaign_fails:
                return httpx.Response(409, json={"error": {"code": "campaign_refused"}})
        if request.url.path == history_path:
            history_reads.append(request.url.path)
            history_before_settlement.append(not settled)
        return inner(request)

    dom = await _render(tmp_path, snapshot, f"/operations/{op}", upstream=httpx.MockTransport(handler),
                        open_history=screen == "campaign")
    _ok(dom, screen)
    assert history_before_settlement == [False]
    assert history_reads == [history_path]
    assert "Loading suspension evidence" not in dom
    assert (">Historical<" if screen == "campaign" or campaign_fails else "Current pause") in dom
    if campaign_fails:
        assert "campaign_refused" in dom


async def test_navigation_caches_first_answer_and_keeps_old_pending_read_evicted(tmp_path):
    import httpx
    op = _ids("family14")["campaign_final"]
    inner, reads = mock_upstream("family14").handler, []

    def handler(request):
        reads.append(request.url.path)
        return inner(request)

    script = """
      (async () => {
        const until = (ready) => ready() ? Promise.resolve() : new Promise(resolve => {
          const observer = new MutationObserver(() => {
            if (ready()) { observer.disconnect(); resolve(); }
          });
          observer.observe(document, {childList: true, subtree: true});
        });
        await until(() => window.smokeApi?.last('/v1/projects') !== undefined && window.smokeApi.last('/v1/health') !== undefined
          && window.smokeApi.last('/v1/projects/demo-connection/status') !== undefined
          && window.smokeApi.last('/v1/projects/demo-final/status') !== undefined
          && document.querySelector('[data-screen="projects"]'));
        const api = window.smokeApi, originalFetch = window.fetch;
        let answerOld;
        window.fetch = (path, ...args) => path === '/cache-old'
          ? new Promise(resolve => { answerOld = () => resolve(new Response('{"old":true}')); })
          : originalFetch(path, ...args);
        const old = api.get('/cache-old');
        const evictedAtNavigation = new Promise(resolve => window.addEventListener('hashchange', () => {
          answerOld();
          old.then(() => resolve(api.last('/cache-old') === undefined));
        }, { once: true }));
        location.hash = '/operations/OP_ID';
        const oldEvicted = await evictedAtNavigation;
        await until(() => document.querySelector('[data-screen="campaign"] .lineage'));
        const path = '/v1/operations/OP_ID';
        const cached = api.last(path);
        const report = document.createElement('div');
        report.id = 'cache-report';
        report.textContent = `first-cached:${cached !== undefined};old-evicted:${oldEvicted}`;
        document.body.append(report);
        window.fetch = (url, ...args) => url === path ? Promise.reject(new TypeError('offline')) : originalFetch(url, ...args);
        const { html, render } = await import('/static/vendor/htm-preact.js');
        const { useResource } = await import('/static/ui/hooks.js');
        function Fallback() {
          const entry = useResource(api, path);
          return html`<div id="fallback-report">fallback:${String(entry.data?.id === 'OP_ID')};stale:${String(entry.stale)};code:${entry.code}</div>`;
        }
        const root = document.createElement('div'); document.body.append(root);
        render(html`<${Fallback} />`, root);
      })();
    """.replace("OP_ID", op)
    dom = await _render(tmp_path, "family14", "/projects", upstream=httpx.MockTransport(handler),
                        smoke_script=script, capture_api=True)
    _ok(dom, "campaign")
    assert 'first-cached:true;old-evicted:true' in dom, dom[-3000:]
    assert 'fallback:true;stale:true;code:network_error' in dom
    assert reads.count(f"/v1/operations/{op}") == 1  # a retry cannot hide loss of the first answer

async def test_home_now_is_always_present_and_health_failure_is_not_idle(tmp_path):
    running = await _render(tmp_path / "run", "running", "/projects")
    _ok(running, "projects")
    assert 'data-section="now"' in running
    assert "Now running" in running
    assert f'#/operations/{_ids("running")["campaign_running"]}' in running
    assert "Generating candidate" in running
    idle = await _render(tmp_path / "idle", "final", "/projects")
    _ok(idle, "projects")
    assert 'data-section="now"' in idle
    assert "Nothing is running" in idle
    upstream = _failing("running", {"/v1/health": "health_refused"})
    failed = await _render(tmp_path / "fail", "running", "/projects", upstream=upstream)
    _ok(failed, "projects")
    now = re.search(r'<section[^>]*data-section="now".*?</section>', failed, re.S)
    assert now and "Unavailable" in now.group(0) and "service_unavailable" in now.group(0)
    assert "Nothing is running" not in now.group(0) and "Now running" not in now.group(0)
