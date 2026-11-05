"""Upstream sb-bench task layout.

Reads tasks in the sb-bench native format directly (no conversion to
the evo layout). Mirrors the runtime behavior of
``test_bench_scripts/qwen35_9B_run/bench.py`` while keeping skill
management on the evo side.

Layout assumptions (sb-bench native):
    task_dir/
        task.toml          — sb-bench flavor: [task], [verification]
        instruction.md     — references paths under environment/data/
        environment/data/  — input files (NOT staged separately;
                             the safe-copy preserves them in place)
        solve.sh           — execution wrapper; cd's to output/ then runs python3 solution.py
        tests/test_outputs.py
        [.ground_truth_*]  — kept inside the safe-copy
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from evolution.eval.layouts.base import (
    CollectResult,
    RunResult,
    TaskLayout,
    TaskMetadata,
    TestResult,
    collect_outputs,
    rewrite_tests_if_needed,
)
from evolution.eval.layouts.evo import _parse_pytest_summary
from evolution.eval.python_env import resolve_python_for_role

# Filenames never carried into the safe-copy workspace.
_COPY_IGNORE = shutil.ignore_patterns(
    "output",
    "solution.py",
    "_solve.py",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    "_solve_output.txt",
    "_pytest_output.txt",
)

# data_generator.py uses Path("environment/data") relative to cwd; cap it so a
# misbehaving generator can't wedge the whole pipeline.
_DATA_GEN_TIMEOUT_S = float(os.environ.get("EVO_SB_DATA_GEN_TIMEOUT_S", "120") or "120")


class SbBenchTaskLayout(TaskLayout):
    """The sb-bench source format (no conversion to evo)."""

    name = "sb_bench"

    # ── metadata ────────────────────────────────────────────────────────

    def parse_metadata(self, task_dir: Path) -> TaskMetadata:
        toml_path = task_dir / "task.toml"
        instruction_path = task_dir / "instruction.md"
        if not toml_path.exists():
            raise FileNotFoundError(f"task.toml not found in {task_dir}")

        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        task_block = data.get("task", {})
        verify_block = data.get("verification", {})
        instruction = (
            instruction_path.read_text(encoding="utf-8").strip()
            if instruction_path.exists()
            else ""
        )

        return TaskMetadata(
            # Identity is the directory name (== sb-bench task id), NOT the
            # human-readable [task].name title — every other part of the
            # system addresses tasks by their dir id (role/task_id splits,
            # the .txt task lists, evo bench --tasks). Mirrors the SkillStore
            # convention where a skill's identity is its folder, not its
            # frontmatter name. The descriptive title is intentionally
            # dropped; the raw id is preserved in source_task below.
            name=task_dir.name,
            instruction=instruction,
            role=task_block.get("role"),
            difficulty=task_block.get("difficulty", "medium"),
            category=task_block.get("role") or "",  # sb-bench has no separate category
            skills_required=list(task_block.get("skills") or []),
            tags=list(task_block.get("tags") or []),
            timeout_sec=float(
                verify_block.get("timeout_seconds") or task_block.get("timeout_seconds") or 300
            ),
            test_file=verify_block.get("verifier_entrypoint", "tests/test_outputs.py"),
            required_outputs=[],
            source="sb-bench",
            source_task=task_block.get("id", task_dir.name),
            raw_toml=toml_path.read_text(encoding="utf-8"),
        )

    # ── staging ────────────────────────────────────────────────────────

    def stage_workspace(self, task_dir: Path, workspace: Path) -> None:
        """Copy the entire task tree (sans output/, caches) into the workspace.

        sb-bench tasks reference paths like ``environment/data/foo.xlsx``
        from instruction.md and solve.sh, so the layout cannot afford to
        flatten files into the workspace root. The workspace mirrors the
        task tree.
        """
        if workspace.exists() and workspace.is_dir() and any(workspace.iterdir()):
            # Fresh-start an existing workspace dir.
            for child in workspace.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                else:
                    shutil.rmtree(child)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if not workspace.exists():
            shutil.copytree(task_dir, workspace, ignore=_COPY_IGNORE)
        else:
            # ``copytree`` insists the target not exist; we manually copy.
            for item in task_dir.iterdir():
                if item.name in {"output", "__pycache__", ".pytest_cache"}:
                    continue
                dest = workspace / item.name
                if item.is_file():
                    shutil.copy2(item, dest)
                elif item.is_dir():
                    shutil.copytree(item, dest)
        (workspace / "output").mkdir(exist_ok=True)
        self._regenerate_environment_data(workspace, self.parse_metadata(task_dir).role)

    @staticmethod
    def _regenerate_environment_data(workspace: Path, role: str | None) -> None:
        """Run ``data_generator.py`` from the workspace if present.

        Tasks like the AFTER benchmark ship raw inputs in ``source_artifacts/``
        and rebuild ``environment/data/`` at solve time. Pre-populated
        ``environment/data/`` tasks ship without a generator — that's fine; we
        only fire when the script exists. The generator runs under the role
        interpreter (same one ``run_solution``/``run_tests`` use), so generators
        needing pandas/openpyxl/duckdb stop silently no-op'ing under bare python3.
        """
        gen = workspace / "data_generator.py"
        if not gen.is_file():
            return
        if (workspace / "environment" / "data").is_dir() and any(
            (workspace / "environment" / "data").iterdir()
        ):
            return
        try:
            subprocess.run(
                [resolve_python_for_role(role), "data_generator.py"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=_DATA_GEN_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return

    # ── solution placement + run ───────────────────────────────────────

    def place_solution(self, workspace: Path, code: str) -> Path:
        """Write ``solution.py`` at the workspace root (where solve.sh expects
        it). Also writes ``_solve.py`` as a debug duplicate so other
        callers can reuse a single name."""
        sol = workspace / "solution.py"
        sol.write_text(code, encoding="utf-8")
        (workspace / "_solve.py").write_text(code, encoding="utf-8")
        return sol

    def run_solution(
        self,
        workspace: Path,
        python_exe: str,
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        """``bash solve.sh`` with ``python3`` patched to ``python_exe``.

        Mirrors ``test_bench_scripts/.../bench.py:run_solve_sh``: the
        original solve.sh is read, its ``python3`` invocations are
        replaced with the role-specific python so the patched script
        runs in the desired conda env, and then we execute the patched
        text via ``bash -c`` so the underlying file is never mutated.
        """
        solve_sh = workspace / "solve.sh"
        if not solve_sh.is_file():
            return RunResult(ok=False, error=f"solve.sh not found in {workspace}")

        patched = solve_sh.read_text(encoding="utf-8").replace("python3", python_exe)
        start = time.time()
        try:
            proc = subprocess.run(
                ["bash", "-c", patched],
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
                error=f"solve.sh timed out after {timeout}s",
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
        """Run pytest from the workspace with TASK_WORKSPACE set.

        Handles the historical sb-bench convention: tests bake in
        ``/root/`` and ``/app/`` prefixes for ground-truth files. If any
        such reference is present, we copy the tests directory into a
        sibling ``_hidden_tests__<workspace_name>/`` and rewrite paths
        there before invoking pytest. The originals are never touched.
        """
        tf = test_file or "tests/test_outputs.py"
        test_path = workspace / tf
        if not test_path.exists():
            # Fall back to the source task dir for the test source. This
            # mirrors ``runner.run_task`` which reads from ``task.test_path``.
            test_path = task_dir / tf
        if not test_path.exists():
            return TestResult(error=f"Test file not found at {tf}")

        # Shared /root|/app rewrite (same helper EvoTaskLayout uses).
        run_test_path, run_cwd = rewrite_tests_if_needed(test_path, workspace)

        run_env = {**(env or os.environ), "TASK_WORKSPACE": str(workspace)}
        # Prepend the role python's bin dir so bare ``python3`` resolves there.
        py_bin_dir = os.path.dirname(python_exe)
        if py_bin_dir:
            run_env["PATH"] = py_bin_dir + os.pathsep + run_env.get("PATH", "")

        cmd = [
            python_exe,
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
                env=run_env,
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
        """Copy produced root files into ``output/`` (the verifier's read dir)."""
        return collect_outputs(workspace, pre_snapshot)

    # ── cleanup ────────────────────────────────────────────────────────

    def cleanup_between_attempts(self, workspace: Path) -> None:
        for name in ("solution.py", "_solve.py"):
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
