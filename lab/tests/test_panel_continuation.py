"""The panel prints the same continuation line as the CLI (spec 2026-09-30 §3.3, PDR-0015)."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import evolution_lab.panel as panel_pkg
from evolution_lab.cli.output import continuation_line
from panel_fixtures import FIXTURES, SNAPSHOTS, load_snapshot

NODE = shutil.which("node")
SCRIPT = Path(__file__).parent / "panel_js" / "print-continuation.mjs"
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed; it is an optional test runner (ADR-0018 p. 5)")


def _js(status, ready):
    env = {**os.environ, "LAB_PANEL_STATIC": str(Path(panel_pkg.__file__).parent / "static"),
           "LAB_PANEL_FIXTURES": str(FIXTURES)}
    out = subprocess.run([NODE, str(SCRIPT), json.dumps([status, ready])], capture_output=True, text=True,
                         timeout=60, env=env, check=True)
    return json.loads(out.stdout)


def _ready(report_body):
    # lab-owner-report/6: the report dict sits under the envelope's `report` (owner/readers.py `report`);
    # its `owner_resume_target` exists only when the status's resume.target is set.
    report = (report_body or {}).get("report") or {}
    return (report.get("owner_resume_target") or {}).get("ready_target")


def _cases():
    for name in SNAPSHOTS:
        routes = load_snapshot(name)["routes"]
        for key, entry in routes.items():
            body = entry["body"]
            if key.count("/") == 3 and key.startswith("/v1/operations/") and "?" not in key \
                    and isinstance(body, dict) and body.get("resume") is not None:
                ready = _ready(routes.get(f"{key}/report", {}).get("body"))
                yield pytest.param(body, ready, id=f"{name}:{body['id']}")


CASES = list(_cases())


@needs_node
@pytest.mark.parametrize("status,ready", CASES)
def test_panel_line_equals_cli_line(status, ready):
    assert _js(status, ready) == continuation_line(status, ready)


# Reason codes are the real ones of owner/resume_rules.py `durable_verdict` / `target_requirement`.
_REF1 = {"schema": "lab-target-ref/1", "id": "t1", "owner_generation": "g"}
_FRESH = {"requirement": "fresh_instance", "reason": "target_unstable_fresh_required", "excluded": [_REF1]}
_BASE = {"id": "o", "target_ref": _REF1}
_VARIANTS = [
    ({**_BASE, "resume": {"state": "ready", "reason": None, "target": None}}, None),
    ({**_BASE, "resume": {"state": "ready", "reason": None, "target": _FRESH}}, None),
    ({**_BASE, "resume": {"state": "ready", "reason": None, "target": _FRESH}}, {"id": "t2", "owner_generation": "g"}),
    # No target_ref in the status: the last excluded target is the one to stop.
    ({"id": "o", "resume": {"state": "ready", "reason": None,
                            "target": {**_FRESH, "excluded": [_REF1, {"id": "t3", "owner_generation": "h"}]}}}, None),
    ({**_BASE, "resume": {"state": "cleanup_first", "reason": "cleanup_required", "target": None}}, None),
    ({**_BASE, "resume": {"state": "cleanup_first", "reason": "cleanup_required", "target": _FRESH}},
     {"id": "t2", "owner_generation": "g"}),
    ({**_BASE, "resume": {"state": "cleanup_first", "reason": "cleanup_unconfirmed", "target": None}}, None),
    ({**_BASE, "resume": {"state": "refused", "reason": "cleanup_unconfirmed", "target": None}}, None),
    ({**_BASE, "resume": {"state": "refused", "reason": "results_deleted", "target": None}}, None),
    ({**_BASE, "resume": {"state": "busy", "reason": "resource_busy", "target": _FRESH}}, None),
    ({**_BASE, "resume": {"state": "not_applicable", "reason": "campaign_not_suspended", "target": None}}, None),
    ({**_BASE, "resume": None}, None),
    ({"resume": {"state": "ready", "reason": None, "target": None}}, None),  # no id: the CLI prints ""
]


@needs_node
@pytest.mark.parametrize("status,ready", _VARIANTS)
def test_synthetic_branches_match(status, ready):
    assert _js(status, ready) == continuation_line(status, ready)


def test_fixtures_cover_role_and_target_suspensions():
    kinds = {("target" if c.values[0]["resume"]["target"] else "role") for c in CASES}
    assert kinds >= {"target", "role"}


def _suspended_js(name, op_id):
    env = {**os.environ, "LAB_PANEL_STATIC": str(Path(panel_pkg.__file__).parent / "static"),
           "LAB_PANEL_FIXTURES": str(FIXTURES)}
    out = subprocess.run([NODE, str(SCRIPT), "suspended", name, op_id], capture_output=True, text=True,
                         timeout=60, env=env, check=True)
    return json.loads(out.stdout)


def _suspended_cases():
    for name in ("suspended-target", "suspended-role"):
        ids = load_snapshot(name)["meta"]["ids"]
        op_id = ids["campaign_" + name.replace("-", "_")]
        routes = load_snapshot(name)["routes"]
        status = routes[f"/v1/operations/{op_id}"]["body"]
        ready = _ready(routes.get(f"/v1/operations/{op_id}/report", {}).get("body"))
        yield pytest.param(name, op_id, status, ready, id=name)


@needs_node
@pytest.mark.parametrize("name,op_id,status,ready", list(_suspended_cases()))
def test_suspended_screen_line_equals_cli_line(name, op_id, status, ready):
    assert _suspended_js(name, op_id) == continuation_line(status, ready)
