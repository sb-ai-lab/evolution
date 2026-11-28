"""The justfile takes the Lab and Core versions from pyproject.toml, so a repin edits one file."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("just") is None, reason="just is not installed")


def _evaluate(name: str) -> str:
    return subprocess.run(
        ["just", "--justfile", str(ROOT / "justfile"), "--evaluate", name],
        check=True, capture_output=True, text=True,
    ).stdout


def test_justfile_versions_follow_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = [dep.split("==", 1)[1] for dep in project["dependencies"] if dep.startswith("evo-skills==")]
    assert len(pins) == 1, project["dependencies"]
    assert _evaluate("lab_version") == project["version"]
    assert _evaluate("core_version") == pins[0]
    lab_dev = re.fullmatch(r".*\.(dev\d+)", project["version"]).group(1)
    core_beta = re.fullmatch(r"[0-9.]+(\w+)", pins[0]).group(1)
    assert _evaluate("pair") == f"{lab_dev}-{core_beta}"


def _dry_run(*recipe: str) -> str:
    done = subprocess.run(
        ["just", "--justfile", str(ROOT / "justfile"), "--dry-run", *recipe],
        check=True, capture_output=True, text=True,
    )
    return done.stdout + done.stderr


def test_demo_builds_the_project_from_the_skeleton_with_both_wheels():
    script = _dry_run("demo")
    assert '/demo/evaluation"' in script
    assert ' new-project "$ws/project"' in script
    evaluate = next(line for line in script.splitlines() if " evaluate " in line)
    assert '"$ws/project"' in evaluate
    assert evaluate.count("--wheel") == 2
    assert "evolution_lab-" in evaluate and "evo_skills-" in evaluate
    assert "examples/owner/evaluation.py" not in script


def test_evaluate_recipe_passes_both_wheels_and_extra_arguments():
    script = _dry_run("evaluate", "my-agent", "--json")
    line = next(line for line in script.splitlines() if " evaluate " in line)
    assert '"my-agent"' in line and line.count("--wheel") == 2 and line.rstrip().endswith("--json")


def test_new_project_recipe_calls_lab_new_project():
    assert ' new-project "my-agent"' in _dry_run("new-project", "my-agent")


def test_demo_campaign_keeps_the_example_path():
    script = _dry_run("demo-campaign")
    assert "examples/owner/campaign.py" in script and "demo_readout.py" in script


def test_demo_panel_reads_the_project_state_of_this_pair():
    # --dry-run prints the backtick expressions of the version variables instead of running them.
    assert 'state="$ws/project/.lab/state-' in _dry_run("demo-panel")
    assert '"$ws/state"' in _dry_run("demo-panel", "campaign")


def test_test_recipes_rebuild_then_run_installed_mode_with_both_wheels():
    extra = "tests/test_report.py"
    script = _dry_run("test", extra)
    assert "-o pythonpath=" in script and "--basetemp" in script
    for name in ("LAB_TEST_CORE_WHEEL", "EVOLUTION_CORE_WHEEL", "EVOLUTION_LAB_WHEEL"):
        assert f"{name}=" in script
    assert "--deselect tests/test_service_runtime.py::test_shutdown_drains" in script
    assert script.rstrip().endswith(extra) or f" {extra}" in script
    assert "test_service_installed" in script


def test_test_fast_runs_the_offline_suite_without_slow_modules():
    assert "-m 'not slow'" not in _dry_run("test")
    marked = _dry_run("test_marks=not slow", "test", "-x")
    assert "-m 'not slow' -x" in marked
    fast = _dry_run("test-fast", "-x")
    assert 'test_marks="not slow" test -x' in fast
