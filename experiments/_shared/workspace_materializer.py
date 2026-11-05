"""Promotes §5.1's inline workspace setup into a reusable helper.

The function copies a sb-bench task tree into a workspace directory and
runs ``data_generator.py`` so file inputs the agent expects exist before
solve. Mirrors §5.1's ``_setup_workspace`` in framework_produce.py.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import tomllib

_NEW_CORPUS_SKIP = {
    "solution.py",
    "source_artifacts",
    "_test_adapted.py",
    ".git",
    "output",
    "tests",
    "__pycache__",
}


class WorkspaceMaterializationError(RuntimeError):
    """Raised when a task workspace cannot be prepared faithfully."""


def _task_role(task_dir: Path) -> str | None:
    task_toml = task_dir / "task.toml"
    if not task_toml.is_file():
        return None
    try:
        data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except Exception:
        return None
    role = ((data.get("task") or {}).get("role") or "").strip()
    return role or None


def resolve_experiment_python(role: str | None = None) -> str:
    """Return the Python executable all generated solve.sh files should use."""
    override = os.environ.get("EVO_PYTHON")
    if override:
        return override
    if os.environ.get("EVO_DISABLE_ROLE_PYTHON", "0") != "1" and role:
        try:
            from evolution.eval.python_env import resolve_python_for_role

            return resolve_python_for_role(role)
        except Exception:
            pass
    return sys.executable


def _canonical_solution_args(solve_sh_text: str) -> str:
    """Trailing CLI args a task's own solve.sh passes to ``solution.py`` (relative
    to ``output/``, the wrapper's cwd, so they transplant directly). Empty for the
    common no-arg contract. Fixes tasks whose instruction mandates a CLI signature."""
    joined = re.sub(r"\\\s*\n", " ", solve_sh_text)
    for line in joined.splitlines():
        s = line.strip()
        if s.startswith("#") or "solution.py" not in s:
            continue
        # require a real interpreter invocation (not a comment mentioning solution.py)
        m = re.search(r"\bpython[0-9.]*\b[^\n]*?solution\.py[\"']?\s+(.+)", s)
        if not m:
            continue
        tail = re.split(r"\s+(?:#|2?>|\||;|&&)", m.group(1).strip())[0].strip()
        if tail:
            return tail
    return ""


def write_agent_solve_sh(path: Path, *, python: str | None = None, solution_args: str = "") -> None:
    """Write the canonical sb-bench solve.sh wrapper.

    The agent code lives at ``<workspace>/solution.py``. The wrapper executes
    it from ``<workspace>/output`` so verifier paths like ``output/foo`` map to
    cwd-relative writes of ``foo`` inside the script.
    """
    python = shlex.quote(python or resolve_experiment_python())
    args = (" " + solution_args.strip()) if solution_args.strip() else ""
    body = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'WS="$(cd "$(dirname "$0")" && pwd)"\n'
        'mkdir -p "$WS/output"\n'
        'cd "$WS/output"\n'
        'if [ -f "$WS/solution.py" ]; then\n'
        f'  exec {python} "$WS/solution.py"{args}\n'
        "fi\n"
        "exit 0\n"
    )
    path.write_text(body, encoding="utf-8")
    try:
        path.chmod(0o755)
    except OSError:
        pass


def _remove_existing_workspace(ws: Path) -> None:
    """Move stale workspace aside before deletion so retries get a clean path."""
    if not ws.exists():
        return
    stale = ws.with_name(f"{ws.name}.stale-{os.getpid()}-{time.time_ns()}")
    try:
        ws.rename(stale)
    except FileNotFoundError:
        return
    except OSError:
        for attempt, delay_s in enumerate((0.05, 0.1, 0.2, 0.5, 1.0), start=1):
            try:
                shutil.rmtree(ws)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(delay_s)
        return

    for delay_s in (0.05, 0.1, 0.2):
        try:
            shutil.rmtree(stale)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay_s)


def materialize_workspace(task_dir: Path, ws: Path, *, agent_solve_sh: bool = True) -> None:
    """Mirror ``task_dir`` into ``ws`` and pre-run any data_generator.py.

    Supports both legacy SkillsBench (``inputs/``) and the new sb-bench
    layout (whole-tree copy with environment/data staging). For agent
    workspaces, ``solve.sh`` is rewritten to execute ``solution.py`` from
    ``<workspace>/output`` so tests measure agent output rather than the oracle.
    """
    _remove_existing_workspace(ws)
    ws.mkdir(parents=True, exist_ok=True)
    role = _task_role(task_dir)
    role_python = resolve_experiment_python(role)

    inputs = task_dir / "inputs"
    if inputs.is_dir():
        for item in inputs.iterdir():
            if item.name.startswith("."):
                continue
            target = ws / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        if agent_solve_sh and (ws / "solve.sh").is_file():
            sh = ws / "solve.sh"
            write_agent_solve_sh(
                sh,
                python=role_python,
                solution_args=_canonical_solution_args(sh.read_text(errors="replace")),
            )
        return

    for item in task_dir.iterdir():
        if item.name in _NEW_CORPUS_SKIP or item.name.startswith("."):
            continue
        target = ws / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=False)
        else:
            shutil.copy2(item, target)
    (ws / "output").mkdir(exist_ok=True)

    dg = ws / "data_generator.py"
    if dg.is_file():
        (ws / "tests").mkdir(exist_ok=True)
        # data_generators may stage visible inputs from source_artifacts (skipped
        # above for oracle hygiene); stage it transiently then remove before agent runs.
        staged_sa = None
        src_artifacts = task_dir / "source_artifacts"
        if src_artifacts.is_dir() and not (ws / "source_artifacts").exists():
            staged_sa = ws / "source_artifacts"
            shutil.copytree(src_artifacts, staged_sa, symlinks=False)
        try:
            proc = subprocess.run(
                [role_python, "data_generator.py"],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode != 0:
                raise WorkspaceMaterializationError(
                    "data_generator.py failed for "
                    f"{task_dir}: rc={proc.returncode}\n"
                    f"stdout:\n{(proc.stdout or '')[-4000:]}\n"
                    f"stderr:\n{(proc.stderr or '')[-4000:]}"
                )
        except WorkspaceMaterializationError:
            raise
        except Exception as exc:
            raise WorkspaceMaterializationError(
                f"data_generator.py failed for {task_dir}: {type(exc).__name__}: {exc}"
            ) from exc
        if staged_sa is not None and staged_sa.is_dir():
            shutil.rmtree(staged_sa, ignore_errors=True)

    env_data = ws / "environment" / "data"
    if env_data.is_dir():
        for item in env_data.iterdir():
            if item.name.startswith("_"):
                continue
            target = ws / item.name
            if target.exists():
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            except Exception:
                continue

    if agent_solve_sh:
        sh = ws / "solve.sh"
        canon_args = (
            _canonical_solution_args(sh.read_text(errors="replace")) if sh.is_file() else ""
        )
        write_agent_solve_sh(sh, python=role_python, solution_args=canon_args)


__all__ = [
    "WorkspaceMaterializationError",
    "materialize_workspace",
    "resolve_experiment_python",
    "write_agent_solve_sh",
]
