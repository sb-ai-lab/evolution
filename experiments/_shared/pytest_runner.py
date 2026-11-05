"""Run a task's pytest tests against a workspace and parse pass/fail/total.

Per memory ``feedback_pytest_primary_judge_secondary.md``: for sb-bench
runs, pytest pass-rate is the primary metric. The judge column is
secondary and never gates row inclusion.

Adapter for the sb-bench task layout (``task_dir/tests/test_outputs.py``
or ``task_dir/tests/test_*.py``). Tests are read, ``/root/`` and
``/app/`` paths are rewritten to the workspace path, then pytest runs
with ``-p no:cacheprovider`` and ``--no-header`` for clean output. The
returned dict matches the legacy contract used by
``experiments/_shared/native_framework_runners.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

from evolution.adapters.workspace_guard import build_guarded_env
from evolution.eval.python_env import resolve_python_for_role


def _parse_collected_count(pytest_output: str) -> int:
    """Best-effort count for pytest runs that time out after collection."""

    patterns = (
        r"collected\s+(\d+)\s+items?",
        r"=+\s*(\d+)\s+tests?\s+collected",
    )
    for pattern in patterns:
        m = re.search(pattern, pytest_output)
        if m:
            return int(m.group(1))
    return 0


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


def _python_for_task(task_dir: Path, requested: str | None) -> str:
    if os.environ.get("EVO_DISABLE_ROLE_PYTHON", "0") == "1":
        return requested or sys.executable
    role = _task_role(task_dir)
    if role:
        return resolve_python_for_role(role)
    return requested or sys.executable


def run_pytest_against_workspace(
    task_dir: Path,
    workspace: Path,
    *,
    python: str | None = None,
    timeout_s: float = 240.0,
) -> dict:
    """Execute ``task_dir/tests/test_*.py`` against ``workspace``.

    Returns:

        {
          "pass_rate": float,           # passed / total, 0.0 when total=0
          "total_tests": int,
          "passed": int,
          "failed": int,
          "errors": int,
          "log_tail": str,              # last 4KB of stdout+stderr
          "log_full": str,              # full stdout+stderr
          "rc": int,                    # subprocess return code, -1 on timeout
        }
    """
    task_dir = Path(task_dir).resolve()
    workspace = Path(workspace).resolve()
    py = _python_for_task(task_dir, python)
    test_dir = task_dir / "tests"
    test_path = test_dir / "test_outputs.py"
    if not test_path.is_file():
        cands = list(test_dir.glob("test_*.py")) if test_dir.is_dir() else []
        if not cands:
            return {
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "log_tail": "no test file found",
                "log_full": "no test file found",
                "rc": -1,
            }
        test_path = cands[0]

    test_text = test_path.read_text(encoding="utf-8", errors="replace")
    test_text = test_text.replace("/root/", f"{workspace}/").replace("/app/", f"{workspace}/")
    # Preserve the original tests/ layout so tests that derive the task root
    # from ``Path(__file__).parents[1]`` still resolve to ``workspace``.
    adapted_dir = workspace / "tests"
    adapted_dir.mkdir(exist_ok=True)
    adapted = adapted_dir / "_test_adapted.py"
    adapted.write_text(test_text, encoding="utf-8")
    if test_dir.is_dir():
        for f in test_dir.iterdir():
            if not f.is_file() or f.name == test_path.name:
                continue
            if f.suffix == ".py":
                support = f.read_text(encoding="utf-8", errors="replace")
                support = support.replace("/root/", f"{workspace}/").replace(
                    "/app/", f"{workspace}/"
                )
                (adapted_dir / f.name).write_text(support, encoding="utf-8")
            else:
                shutil.copy2(f, adapted_dir / f.name)

    env = build_guarded_env(
        framework="pytest",
        workspace_dir=workspace,
        audit_log_path=workspace.parent / "workspace_guard.jsonl",
        base_env=os.environ,
        command_wrappers=True,
    )
    # sb-bench tests were authored against pandas<3 legacy string
    # inference, where read_csv text columns have dtype object.
    env["PANDAS_FUTURE_INFER_STRING"] = "0"
    env.pop("PYTHONHOME", None)
    venv_dir = str(Path(py).parent)
    env["PATH"] = venv_dir + os.pathsep + env.get("PATH", "")

    cmd = [
        py,
        "-m",
        "pytest",
        str(adapted),
        "-v",
        "--tb=short",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(workspace),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        out = (stdout or "") + (stderr or "")
        collected = _parse_collected_count(out)
        if collected > 0:
            return {
                "pass_rate": 0.0,
                "total_tests": collected,
                "passed": 0,
                "failed": collected,
                "errors": 0,
                "log_tail": (out + f"\npytest timed out after {timeout_s}s")[-4000:],
                "log_full": out + f"\npytest timed out after {timeout_s}s",
                "rc": -1,
                "pytest_status": "timeout",
            }
        return {
            "pass_rate": 0.0,
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "log_tail": f"pytest timed out after {timeout_s}s",
            "log_full": f"pytest timed out after {timeout_s}s",
            "rc": -1,
            "pytest_status": "timeout",
        }
    out = (stdout or "") + (stderr or "")
    m_pass = re.search(r"(\d+) passed", out)
    m_fail = re.search(r"(\d+) failed", out)
    m_err = re.search(r"(\d+) error", out)
    passed = int(m_pass.group(1)) if m_pass else 0
    failed = int(m_fail.group(1)) if m_fail else 0
    errors = int(m_err.group(1)) if m_err else 0
    total = passed + failed + errors
    rate = (passed / total) if total else 0.0
    return {
        "pass_rate": rate,
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "log_tail": out[-4000:],
        "log_full": out,
        "rc": proc.returncode,
        "pytest_status": "completed",
    }


__all__ = ["run_pytest_against_workspace"]
