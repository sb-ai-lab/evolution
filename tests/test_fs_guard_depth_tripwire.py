"""The fs-guard depth tripwire records (and optionally refuses) pathological
directory nesting, so a resolve_path-style recursion can never silently bleed
inodes. Exercises the real guard script as a subprocess ``sitecustomize``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD_SCRIPT = ROOT / "src" / "evolution" / "eval" / "_guard_scripts" / "fs_guard_sitecustomize.py"


def _run_makedirs(tmp_path: Path, *, depth: int, max_depth: int, raise_on_depth: bool):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        GUARD_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    run_home = tmp_path / "run_home"
    run_home.mkdir()
    log = tmp_path / "guard.jsonl"
    target = run_home.joinpath(*[f"d{i}" for i in range(depth)])

    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    env["EVOLUTION_WORKSPACE_GUARD_RUN_HOME"] = str(run_home)
    env["EVOLUTION_WORKSPACE_GUARD_WORKSPACE"] = str(run_home)
    env["EVOLUTION_WORKSPACE_GUARD_LOG"] = str(log)
    env["EVO_GUARD_MAX_DEPTH"] = str(max_depth)
    if raise_on_depth:
        env["EVO_GUARD_MAX_DEPTH_RAISE"] = "1"
    else:
        env.pop("EVO_GUARD_MAX_DEPTH_RAISE", None)

    proc = subprocess.run(
        [sys.executable, "-c", f"import os; os.makedirs(r'{target}', exist_ok=True)"],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc, log, target


def _depth_events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == "python_fs_depth"
    ]


def test_deep_makedirs_refused_when_raise_enabled(tmp_path: Path):
    proc, log, target = _run_makedirs(tmp_path, depth=8, max_depth=3, raise_on_depth=True)
    assert proc.returncode != 0
    assert "nesting depth" in proc.stderr
    assert not target.exists()


def test_deep_makedirs_records_only_by_default(tmp_path: Path):
    proc, log, target = _run_makedirs(tmp_path, depth=8, max_depth=3, raise_on_depth=False)
    assert proc.returncode == 0, proc.stderr
    assert target.exists()
    assert _depth_events(log)


def test_shallow_makedirs_not_tripped(tmp_path: Path):
    proc, log, target = _run_makedirs(tmp_path, depth=2, max_depth=10, raise_on_depth=True)
    assert proc.returncode == 0, proc.stderr
    assert target.exists()
    assert not _depth_events(log)
