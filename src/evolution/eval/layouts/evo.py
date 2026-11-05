"""Evo native task layout.

Mirrors the behavior that ``eval/workspace.py``, ``eval/runner.py``, and
``eval/solver.py`` currently implement inline. Pulled into a layout so
the eval pipeline can dispatch through ``TaskLayout`` once Phase 3
lands. Until then this class is unused outside the smoke tests.

Layout assumptions (evo native):
    task_dir/
        task.toml          — evo flavor: [metadata], [skills], [evaluation]
        instruction.md     — paths already rewritten if imported from a bench
        inputs/            — staged into the workspace before solving
        tests/test_outputs.py
        [solve.sh]         — optional; if present, used as the run command
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from evolution.eval.layouts.base import (
    CollectResult,
    RunResult,
    TaskLayout,
    TaskMetadata,
    TestResult,
    rewrite_tests_if_needed,
)


class EvoTaskLayout(TaskLayout):
    """The current evo task format."""

    name = "evo"

    # ── metadata ────────────────────────────────────────────────────────

    def parse_metadata(self, task_dir: Path) -> TaskMetadata:
        toml_path = task_dir / "task.toml"
        instruction_path = task_dir / "instruction.md"
        if not toml_path.exists():
            raise FileNotFoundError(f"task.toml not found in {task_dir}")
        toml_text = toml_path.read_text(encoding="utf-8")
        instruction = (
            instruction_path.read_text(encoding="utf-8").strip()
            if instruction_path.exists()
            else ""
        )

        return TaskMetadata(
            name=_toml_value(toml_text, "name") or task_dir.name,
            instruction=instruction,
            difficulty=_toml_value(toml_text, "difficulty") or "medium",
            category=_toml_value(toml_text, "category") or "",
            skills_required=_toml_list(toml_text, "required"),
            tags=_toml_list(toml_text, "tags"),
            timeout_sec=float(_toml_value(toml_text, "timeout_sec") or "600"),
            test_file=_toml_value(toml_text, "test_file") or "tests/test_outputs.py",
            required_outputs=_required_outputs(toml_text, instruction),
            source=_toml_value(toml_text, "origin") or "",
            source_task=_toml_value(toml_text, "task_id") or "",
            raw_toml=toml_text,
        )

    # ── staging ────────────────────────────────────────────────────────

    def stage_workspace(self, task_dir: Path, workspace: Path) -> None:
        """Copy ``task_dir/inputs/*`` into the workspace root.

        Identical to ``eval/workspace.setup_workspace`` so the smoke
        regression diff comes out empty.
        """
        workspace.mkdir(parents=True, exist_ok=True)
        inputs_dir = task_dir / "inputs"
        if not inputs_dir.is_dir():
            return
        for item in inputs_dir.iterdir():
            dest = workspace / item.name
            if item.is_file():
                shutil.copy2(item, dest)
            elif item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)

    # ── solution placement + run ───────────────────────────────────────

    def place_solution(self, workspace: Path, code: str) -> Path:
        """Mirror the solver's current contract: write both ``_solve.py``
        (canonical debug copy) and ``solution.py`` (the name solve.sh
        expects, when a downstream layout has one). Returns ``_solve.py``.
        """
        script = workspace / "_solve.py"
        script.write_text(code, encoding="utf-8")
        (workspace / "solution.py").write_text(code, encoding="utf-8")
        return script

    def run_solution(
        self,
        workspace: Path,
        python_exe: str,
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        """Run the staged solution. Honors a workspace ``solve.sh`` when present
        (``bash -c`` with ``python3``→``python_exe``, like ``SbBenchTaskLayout``)
        so the execution cwd matches the OUTPUT_CWD solver contract and outputs
        land in ``output/`` natively; otherwise runs ``python _solve.py``. Both
        with cwd=workspace and the caller's env.
        """
        py = python_exe or sys.executable
        solve_sh = workspace / "solve.sh"
        if solve_sh.is_file():
            patched = solve_sh.read_text(encoding="utf-8").replace("python3", py)
            cmd = ["bash", "-c", patched]
        else:
            script = workspace / "_solve.py"
            if not script.is_file():
                return RunResult(ok=False, error=f"{script} missing — call place_solution first")
            cmd = [py, str(script)]
        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return RunResult(
                ok=proc.returncode == 0,
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
                elapsed_sec=time.time() - start,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                ok=False,
                returncode=-1,
                elapsed_sec=time.time() - start,
                timed_out=True,
                error=f"timed out after {timeout}s",
            )
        except OSError as exc:
            return RunResult(
                ok=False,
                returncode=-1,
                elapsed_sec=time.time() - start,
                error=str(exc),
            )

    # ── testing ────────────────────────────────────────────────────────

    def run_tests(
        self,
        task_dir: Path,
        workspace: Path,
        python_exe: str,
        *,
        timeout: float,
        env: dict[str, str] | None = None,
        test_file: str | None = None,
    ) -> TestResult:
        """Run pytest with cwd=workspace.

        The caller is expected to have already set ``WORKSPACE_DIR`` (via
        ``build_guarded_env``) in ``env``. We do not add it here because
        the production code path in ``runner.run_task`` builds a guarded
        env at the call site.
        """
        tf = test_file or "tests/test_outputs.py"
        test_path = task_dir / tf
        if not test_path.exists():
            return TestResult(error=f"Test file not found: {test_path}")

        # Mirror legacy run_task: rewrite /root|/app test paths to the
        # workspace when present (no-op for the common case).
        run_test_path, run_cwd = rewrite_tests_if_needed(test_path, workspace)

        cmd = [
            python_exe or sys.executable,
            "-m",
            "pytest",
            str(run_test_path),
            "-v",
            "--tb=short",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ]
        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(run_cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env or {**os.environ, "WORKSPACE_DIR": str(workspace)},
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                elapsed_sec=time.time() - start,
                timed_out=True,
                error=f"pytest timed out after {timeout}s",
            )

        return _parse_pytest_summary(proc.stdout + proc.stderr, time.time() - start)

    # ── output collection ──────────────────────────────────────────────

    def collect_outputs(self, workspace: Path, pre_snapshot: dict[str, str]) -> CollectResult:
        """Strict no-op: evo verifiers read produced files from the workspace
        root via ``TASK_WORKSPACE``, never from ``output/``."""
        return CollectResult()

    # ── cleanup ────────────────────────────────────────────────────────

    def cleanup_between_attempts(self, workspace: Path) -> None:
        for name in ("solution.py", "_solve.py", "_response.md", "_prompt.md"):
            p = workspace / name
            if p.is_file():
                p.unlink()
        out_dir = workspace / "output"
        if out_dir.is_dir():
            for child in out_dir.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                else:
                    shutil.rmtree(child)
        for cache in workspace.rglob("__pycache__"):
            if cache.is_dir():
                shutil.rmtree(cache, ignore_errors=True)


# ── helpers (toml + pytest parsing; mirror eval/task.py + eval/runner.py) ─

_OUT_SUFFIXES = "json|csv|tsv|xlsx|xls|txt|parquet|db|sqlite|pdf|png|md|html|yaml|yml|xml"
_OUT_RE = re.compile(r"`([A-Za-z0-9_./-]+\.(?:" + _OUT_SUFFIXES + r"))`")


def _toml_value(text: str, key: str) -> str:
    m = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def _toml_list(text: str, key: str) -> list[str]:
    m = re.search(rf"^{key}\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return re.findall(r'"([^"]*)"', m.group(1))


def _required_outputs(toml_text: str, instruction: str) -> list[str]:
    explicit = _toml_list(toml_text, "required_outputs")
    if explicit:
        return explicit
    seen: set[str] = set()
    out: list[str] = []
    for m in _OUT_RE.finditer(instruction or ""):
        tok = m.group(1)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out[:20]


def _parse_pytest_summary(output: str, elapsed: float) -> TestResult:
    """Same parsing logic as ``runner._parse_pytest_output``."""
    passed = failed = skipped = errored = 0
    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) skipped", output)
    if m:
        skipped = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        errored = int(m.group(1))
    collected = passed + failed + skipped + errored
    return TestResult(
        collected=collected,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errored=errored,
        stdout=output,
        elapsed_sec=elapsed,
        error=None if collected else "No tests collected",
    )
