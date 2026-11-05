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
    "evolution.adapters.evolution_adapter",
    "evolution.adapters.evoskill_adapter",
    "evolution.adapters.hermes_adapter",
    "evolution.adapters.memento_adapter",
    "evolution.adapters.memp_adapter",
    "evolution.adapters.skillx_adapter",
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
    "evolution.research.adapters",
    "evolution.research.adapters.compose",
    "evolution.research.adapters.context",
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


def test_research_adapter_namespace_is_importable() -> None:
    script = """
from evolution.research.adapters.compose import compose_skill_with_adapter
from evolution.research.adapters.context import AdapterRecord, AdapterStore, lookup_adapter

assert compose_skill_with_adapter
assert AdapterRecord
assert AdapterStore
assert lookup_adapter
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
