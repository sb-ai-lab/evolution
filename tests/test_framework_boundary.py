"""Core framework imports must not load paper/baseline adapter modules."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


def test_framework_api_import_does_not_import_baseline_adapters() -> None:
    script = """
import sys
import evolution.workflow
import evolution.cli.main

blocked_names = (
    "adapters.evolution_adapter",
    "adapters.evoskill_adapter",
    "adapters.hermes_adapter",
    "adapters.memento_adapter",
    "adapters.memp_adapter",
    "adapters.skillx_adapter",
)
loaded = [name for name in blocked_names if name in sys.modules]
if loaded:
    raise SystemExit("baseline adapters imported: " + ", ".join(loaded))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_framework_api_import_does_not_import_research_adapters() -> None:
    script = """
import sys
import evolution.workflow
import evolution.cli.main

blocked_names = (
    "research.adapters",
    "research.adapters.compose",
    "research.adapters.context",
)
loaded = [name for name in blocked_names if name in sys.modules]
if loaded:
    raise SystemExit("research adapters imported: " + ", ".join(loaded))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
