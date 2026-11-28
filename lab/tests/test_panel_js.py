"""Runs the frontend's pure-module tests with node's built-in runner (ADR-0018 p. 5, amended
2026-09-30: node is an optional, package-free test runner; no node -> skip)."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import evolution_lab.panel as panel_pkg
from panel_fixtures import FIXTURES

pytestmark = pytest.mark.slow

NODE = shutil.which("node")
TESTS = sorted((Path(__file__).parent / "panel_js").glob("*.test.mjs"))


@pytest.mark.skipif(NODE is None, reason="node is not installed; it is an optional test runner (ADR-0018 p. 5)")
def test_panel_js_modules(tmp_path):
    assert TESTS, "no tests/panel_js/*.test.mjs found"  # an empty list would make node auto-discover in cwd
    env = {**os.environ, "LAB_PANEL_STATIC": str(Path(panel_pkg.__file__).parent / "static"),
           "LAB_PANEL_FIXTURES": str(FIXTURES)}
    # Stage fields must come from today's Owner reader, not an adaptation of a
    # producing dev63 capture that predates the closed /14 stage form.
    from dev63_default_outputs import SCENARIOS, read_outputs
    live = tmp_path / "focus-first.json"
    live.write_text(json.dumps(read_outputs(FIXTURES / "b15/focus-first/run", include_panel=False)["campaign.json"]))
    env["LAB_FOCUS_FIRST_CAMPAIGN"] = str(live)
    defaults = tmp_path / "default-campaigns.json"
    defaults.write_text(json.dumps({name: read_outputs(FIXTURES / SCENARIOS[name] / "run",
                                                   include_panel=False)["campaign.json"]
                                    for name in ("agent-failed", "single-stage")}))
    env["LAB_DEFAULT_CAMPAIGNS"] = str(defaults)
    result = subprocess.run([NODE, "--test", *map(str, TESTS)], capture_output=True, text=True,
                            timeout=300, env=env)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]
