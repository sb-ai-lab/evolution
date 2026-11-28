"""The panel on the installed wheel over a live demo campaign (spec 2026-09-30 §4, "installed wheel, end of
branch 2"). Runs under pt_inst / pt_work / the full suite: a real `lab serve`, the demo project held in its
first Editor call, a real `lab panel --background`, and a headless browser on the campaign, measurement and
diff screens. Skips without the installed pair or a browser.

Known shortfall against spec §4: a campaign that is still running is NOT rendered in the browser. The panel's
live stream stays open while the operation runs and the browser's --dump-dom does not return until it closes
(no CDP or playwright dependency is added); that the attempt is held in progress is asserted on the service's
/campaign read, and "In progress" rendering is covered by the `running` fixture smoke (test_panel_smoke)."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

import pytest

from panel_browser import dump_dom, find_browser

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "panel"))
import capture  # noqa: E402  (the fixture script's helpers; it never imports the test suite)

LAB_WHEEL = os.environ.get("EVOLUTION_LAB_WHEEL")
CORE_WHEEL = os.environ.get("LAB_TEST_CORE_WHEEL")
CONSOLE = Path(sys.executable).with_name("lab")
BROWSER = find_browser()
pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    not (LAB_WHEEL and CORE_WHEEL and CONSOLE.is_file() and BROWSER),
    reason="needs the installed pair (EVOLUTION_LAB_WHEEL, LAB_TEST_CORE_WHEEL, the lab console) and a headless browser")]


def _screen(dom, name):
    assert f'data-screen="{name}"' in dom and "data-screen-error" not in dom, dom[-3000:]


def test_live_campaign_screens(tmp_path):
    short = Path(tempfile.mkdtemp(prefix="pli-", dir="/tmp"))  # the socket path must fit 107 bytes
    short.chmod(0o700)
    home = short / "home"
    log = (tmp_path / "commands.jsonl").open("x", encoding="utf-8")
    try:
        with capture._service(CONSOLE, state=tmp_path / "state", home=home,
                              wheels=(Path(LAB_WHEEL), Path(CORE_WHEEL)), log=tmp_path / "service.jsonl",
                              env_home=tmp_path / "env-home", command_log=log) as cli:
            project = cli("project", "demo", "demo")[-1]
            wc = Path(project["working_copy"]["path"])
            capture._wrap_editor(wc / "lab", wc)
            digest, campaign_digest = capture._connect(cli, tmp_path, "demo")
            request = json.loads((wc / "lab" / "evaluation-request.json").read_text())
            request["bundle_digest"] = digest
            (tmp_path / "evaluation.json").write_text(json.dumps(request))
            prepared = cli("project", "prepare", "demo", "--request", str(tmp_path / "evaluation.json"))[-1]
            baseline_target, _ = capture._project_target(cli, digest, "demo")
            evaluation = capture._wait_operation(cli, cli("project", "start", "demo", "--prepared",
                                                          prepared["prepared"]["digest"], "--target",
                                                          baseline_target)[-1]["operation"])
            assert evaluation["control_state"] == "finished", evaluation
            hold = wc.with_name(wc.name + ".hold")
            hold.write_text("")
            target, _ = capture._project_target(cli, digest, "demo")
            op = cli("project", "start", "demo", "--prepared", campaign_digest, "--target", target)[-1]["operation"]["id"]
            _, get = capture._getter(home / "service.sock")
            deadline = time.monotonic() + 120
            while True:
                status, view = get(f"/v1/operations/{op}/campaign")
                if status == 200 and any(a.get("group") == "in_progress" for r in view.get("rounds") or [] for a in r.get("attempts") or []):
                    break
                assert time.monotonic() < deadline, view
                time.sleep(0.2)
            panel = cli("panel", "--port", "0", "--background")[-1]  # cli already passes --home
            try:
                base = panel["url"]
                # A browser keeps the live stream of a running operation open, and --dump-dom (virtual time) does
                # not return until it closes (probed: --timeout with or without --virtual-time-budget either
                # still waits for the stream or dumps before the first reads finish), so the hold is released
                # first and the campaign screen is dumped over the ending campaign.
                hold.unlink()
                dom = dump_dom(BROWSER, f"{base}#/operations/{op}", budget_ms=8000, workdir=tmp_path)
                _screen(dom, "campaign")
                assert op[:8] in dom and "Champion lineage" in dom
                baseline = next(m["measurement_id"] for m in view["measurements"] if m["role"] == "baseline")
                finished = capture._wait_operation(cli, cli("status", op)[-1], timeout=180)
                assert finished["control_state"] == "finished", finished
                # Reuse this campaign and evaluation: a live public route/console check adds
                # no second scenario, capture or independent campaign launch.
                for operation_id, reason in ((op, None), (evaluation["id"], "not_campaign")):
                    status, evidence = get(f"/v1/operations/{operation_id}/suspensions")
                    assert status == 200, evidence
                    assert evidence == cli("suspensions", operation_id)[-1]
                    assert evidence == {
                        "schema": "lab-suspension-evidence/1", "operation_id": operation_id,
                        "state": "available" if reason is None else "unavailable",
                        "source": "core_raw", "reason": reason,
                        "suspensions": [] if reason is None else None,
                    }
                dom = dump_dom(BROWSER, f"{base}#/operations/{op}/measurements/{baseline}", budget_ms=8000, workdir=tmp_path)
                _screen(dom, "measurement")
                assert baseline in dom and "Measurement" in dom
                dom = dump_dom(BROWSER, f"{base}#/operations/{op}/diff", budget_ms=8000, workdir=tmp_path)
                _screen(dom, "diff")
                assert "against" in dom or "skills/SKILL.md" in dom
            finally:
                cli("panel", "stop", check=False)
    finally:
        log.close()
        shutil.rmtree(short, ignore_errors=True)
