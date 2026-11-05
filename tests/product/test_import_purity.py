"""Importing the framework must not load foreign packages or touch process state.

This is the one failure mode that corrupts measurements rather than breaking a
build. ``eval/child_env.py`` forwards ``PYTHONPATH`` to untrusted solution and
verifier children, so a module-scope ``os.environ`` write anywhere in the import
graph silently re-points those children at another checkout. The A/B arms still
run, still write results, and still look healthy.

The check has to happen in a subprocess: by the time pytest collects this file
the parent has already imported half the tree, which masks exactly what we want
to observe.

What this does **not** cover, so nobody reads it as more than it is:

* it compares terminal state, so a module that writes a variable and restores it
  before the import completes passes;
* ``FOREIGN_ROOTS`` is an explicit list, so an unrelated third-party root is not
  flagged -- this catches the packages the merge is about, not every import;
* pollution on first *call* rather than at import is out of scope here; the
  child-process contract in ``test_child_env_contract.py`` is what bounds that;
* the child has no ``.env`` file, so an import-time ``load_dotenv`` would pass
  here and still write variables for an operator who has one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

# Package roots that must never be pulled in by a core framework import.
FOREIGN_ROOTS = ("agentsamo", "skill_evolution", "langchain", "gigachat", "torch")

# ``src`` is injected via sys.path rather than PYTHONPATH on purpose. Handing the
# child a PYTHONPATH would mask an import-time
# ``os.environ.setdefault("PYTHONPATH", ...)`` -- it would find the name already
# set and do nothing here, while polluting a normal process where it is absent.
# That is precisely the defect this file exists to catch.
_PROBE = """
import json, os, sys

sys.path.insert(0, {src!r})

before_env = dict(os.environ)
before_path = list(sys.path)
before_modules = set(sys.modules)

import evolution.workflow
import evolution.cli.main

after_env = dict(os.environ)
foreign_roots = {foreign_roots!r}

print(json.dumps({{
    "added": {{k: after_env[k] for k in after_env if k not in before_env}},
    "changed": sorted(k for k in before_env if after_env.get(k) != before_env[k]),
    "removed": sorted(k for k in before_env if k not in after_env),
    "path_reordered": before_path != sys.path,
    "path_added": [p for p in sys.path if p not in before_path],
    "path_removed": [p for p in before_path if p not in sys.path],
    "foreign": sorted({{
        name.split(".")[0]
        for name in set(sys.modules) - before_modules
        if name.split(".")[0] in foreign_roots
    }}),
}}))
"""


@pytest.fixture(scope="module")
def import_effects() -> dict[str, object]:
    """Run the framework import in a clean interpreter and report what it changed.

    The child's environment is stripped to the minimum an interpreter needs. A
    name the framework might write must be *absent* here, or a write to it looks
    like a no-op.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(src=str(SRC), foreign_roots=FOREIGN_ROOTS)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_import_loads_no_foreign_package(import_effects: dict[str, object]) -> None:
    assert import_effects["foreign"] == []


def test_import_adds_no_environment_variable(import_effects: dict[str, object]) -> None:
    assert import_effects["added"] == {}


def test_import_rewrites_no_environment_variable(import_effects: dict[str, object]) -> None:
    assert import_effects["changed"] == []


def test_import_removes_no_environment_variable(import_effects: dict[str, object]) -> None:
    assert import_effects["removed"] == []


def test_import_leaves_sys_path_unchanged(import_effects: dict[str, object]) -> None:
    """Order matters as much as membership: a reordering changes what wins."""
    assert import_effects["path_added"] == []
    assert import_effects["path_removed"] == []
    assert import_effects["path_reordered"] is False
