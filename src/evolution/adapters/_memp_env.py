"""ALFWorld-style step env wrapping a Python-coding task.

Per user spec: this is a **non-native environment wrapper** for MemP
(MemP was designed for ALFWorld step/action/observation loops; we expose
file ops + pytest as the analog so MemP's own
``Memory.process_trajectory_item_reflect`` and ``memory_adjust`` can run
unchanged).

Five actions are exposed to the agent (text-action interface mirroring
ALFWorld):

  list_files             → returns workspace directory listing
  read_file <path>       → returns first 16KB of file content
  write_solution <code>  → writes _solve.py to workspace
  write_file <path>\n<content>
                          → writes a durable workspace file
  run_tests              → runs pytest, returns pass/fail summary
  done <answer>          → ends episode

Each step the env returns a (observation, done, reward) triple. Reward
is 1.0 only on a successful done after a passing run_tests; partial
pytest pass rates show up via the ``run_tests`` observation but reward
isn't counted until the agent says ``done``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from evolution.eval._proc import run_capture
from evolution.eval.evidence import snapshot_workspace
from evolution.eval.layouts.base import collect_outputs

from .feedback_policy import (
    FeedbackPolicy,
    parse_pytest_counts,
    render_pytest_observation,
    resolve_feedback_policy,
)
from .workspace_guard import build_guarded_env, make_workspace_guard

logger = logging.getLogger(__name__)

# Trace-fidelity caps for MemP's evolve signal; env-gated, generous defaults.
_READ_FILE_CHARS = int(os.environ.get("EVO_MEMP_READ_FILE_CHARS", "32000"))
_SOLVER_TAIL_CHARS = int(os.environ.get("EVO_MEMP_SOLVER_TAIL_CHARS", "8000"))


@dataclass
class StepResult:
    observation: str
    done: bool = False
    reward: float = 0.0
    info: dict = field(default_factory=dict)


@dataclass
class TaskEnv:
    """Per-task isolated environment.

    Holds the workspace path, the task's test file, and the python
    interpreter (from a per-task venv). Stateless across actions — each
    action is a fresh subprocess.
    """

    task_id: str
    workspace: Path
    test_path: Path
    task_path: Path
    python: str = field(default_factory=lambda: sys.executable)
    timeout_per_action: float = 120.0
    guard_log_path: Path | None = None
    output_cwd: bool = False
    feedback_policy: FeedbackPolicy | str | None = None
    last_pass_rate: float = 0.0
    total_tests: int = 0
    final_pass_rate: float = 0.0  # set on done
    last_verifier_trace: dict | None = None

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self.test_path = Path(self.test_path).resolve()
        self.task_path = Path(self.task_path).resolve()
        if self.guard_log_path is not None:
            self.guard_log_path = Path(self.guard_log_path).resolve()

    def _guard(self):
        return make_workspace_guard(
            framework="memp",
            workspace_dir=self.workspace,
            audit_log_path=self.guard_log_path,
        )

    # ------------------------------------------------------------------
    # Initial observation
    # ------------------------------------------------------------------

    def reset(self) -> str:
        """Return the initial observation: task instruction not included
        here (the adapter passes it as the system+user prompt). The env
        observation is just the workspace state.
        """
        self._guard().log(
            event="setup",
            decision="allow",
            action="memp_task_env",
            subject=str(self.workspace),
        )
        return f"Task {self.task_id} workspace ready at {self.workspace}.\n" + self._listing()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def step(self, action_text: str) -> StepResult:
        action_text = action_text.strip()
        if not action_text:
            return StepResult("Empty action.", done=False)

        verb, _, rest = action_text.partition(" ")
        verb = verb.strip().lower()
        rest = rest.strip()

        if verb == "list_files":
            return StepResult(self._listing())

        if verb == "read_file":
            return StepResult(self._read_file(rest))

        if verb == "write_solution":
            return StepResult(self._write_solution(rest))

        if verb == "write_file":
            return StepResult(self._write_file(rest))

        if verb == "run_tests":
            if os.environ.get("EVO_MEMP_EVAL_DISABLE_RUN_TESTS", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }:
                return StepResult("run_tests is unavailable during evaluation.")
            return StepResult(self._run_tests())

        if verb == "done":
            # Agent declares completion. Final reward is current
            # last_pass_rate (set by most recent run_tests). We accept
            # rate==1.0 as success; anything less is partial.
            self.final_pass_rate = self.last_pass_rate
            obs = (
                f"Episode ended. Final pass rate: {self.last_pass_rate:.2f} "
                f"of {self.total_tests} tests."
            )
            return StepResult(obs, done=True, reward=self.last_pass_rate, info={"answer": rest})

        return StepResult(
            f"Unknown action {verb!r}. Valid actions: list_files | read_file <path> | "
            "write_solution <code> | write_file <path>\\n<content> | run_tests | done <answer>",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _listing(self) -> str:
        if not self.workspace.is_dir():
            return f"Workspace missing: {self.workspace}"
        entries = []
        for p in sorted(self.workspace.iterdir()):
            if p.is_dir():
                entries.append(f"  {p.name}/")
            else:
                size = p.stat().st_size
                entries.append(f"  {p.name} ({size}B)")
        return "Workspace listing:\n" + "\n".join(entries[:50])

    def _read_file(self, rel: str) -> str:
        if len(rel) > 4096:
            preview = rel[:240].replace("\n", "\\n")
            return (
                "read_file: path is too long; provide one relative workspace path. "
                f"Received prefix: {preview!r}"
            )
        reason = self._guard().path_block_reason(rel, label="memp read_file")
        if reason:
            return f"read_file: {reason}"
        try:
            target = Path(rel).expanduser()
            if not target.is_absolute():
                target = self.workspace / target
            target = target.resolve(strict=False)
        except OSError as exc:
            return f"read_file: invalid path {exc!r}"
        try:
            is_file = target.is_file()
        except OSError as exc:
            return f"read_file: invalid path {exc!r}"
        if not is_file:
            return f"read_file: '{rel}' not found in workspace"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")[:_READ_FILE_CHARS]
        except OSError as exc:
            return f"read_file: {exc!r}"
        return f"=== {rel} (truncated to {_READ_FILE_CHARS} chars) ===\n{text}"

    def _write_solution(self, code: str) -> str:
        # Strip leading/trailing markdown fences if present.
        code = re.sub(r"^```[a-zA-Z]*\n", "", code.strip())
        code = re.sub(r"\n```\s*$", "", code)
        target = self.workspace / "_solve.py"
        target.write_text(code, encoding="utf-8")
        return f"write_solution: wrote {len(code)} bytes to _solve.py"

    def _write_file(self, payload: str) -> str:
        payload = payload.strip()
        if not payload:
            return "write_file: missing path and content"
        first, sep, content = payload.partition("\n")
        path = first.strip().strip("`'\"")
        if not sep:
            return (
                "write_file: expected `write_file <path>` followed by file content on the next line"
            )
        if len(path) > 4096:
            return "write_file: path is too long"
        reason = self._guard().path_block_reason(path, label="memp write_file")
        if reason:
            return f"write_file: {reason}"
        try:
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = self.workspace / target
            target = target.resolve(strict=False)
        except OSError as exc:
            return f"write_file: invalid path {exc!r}"
        try:
            target.relative_to(self.workspace)
        except ValueError:
            return f"write_file: path escapes workspace: {path!r}"
        content = re.sub(r"^```[a-zA-Z0-9_.+-]*\n", "", content.strip())
        content = re.sub(r"\n```\s*$", "", content)
        lines = content.splitlines()
        while lines and lines[-1].strip().lower() in {"done", "done."}:
            lines.pop()
        content = "\n".join(lines).rstrip() + ("\n" if lines else "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"write_file: wrote {len(content)} bytes to {target.relative_to(self.workspace)}"

    def _run_tests(self) -> str:
        """Run pytest against the task's tests, regardless of whether the
        agent's ``_solve.py`` ran cleanly.

        The previous behavior gated pytest on ``_solve.py`` returncode,
        so a single import-time crash collapsed the trajectory to
        ``total_tests=0`` (which then made every reward 0 and starved
        MemP's reflector). Pytest itself reports ``N failed`` even when
        the import crashes — that signal is much more informative for
        the procedure-memory reflective loop, so we let pytest run no
        matter what.

        Path rewrites for ``/root`` and ``/app`` to the actual workspace
        happen inline (mirroring the skillstorage runner's behavior
        without taking its lock).
        """
        solve_path = self.workspace / "_solve.py"
        if not solve_path.is_file():
            return "run_tests: no _solve.py written; use write_solution first"

        # In ``output_cwd`` mode the workspace IS the task's ``output/`` dir, so
        # the verifier (and ``output/...`` literals) resolve from its parent and
        # the agent's writes land in ``output/`` natively (no bridge needed).
        verify_cwd = self.workspace.parent if self.output_cwd else self.workspace

        env = build_guarded_env(
            framework="memp",
            workspace_dir=self.workspace,
            audit_log_path=self.guard_log_path,
            base_env=os.environ,
            command_wrappers=True,
        )
        env.pop("PYTHONHOME", None)
        venv_dir = str(Path(self.python).parent)
        env["PATH"] = venv_dir + os.pathsep + env.get("PATH", "")

        pre_ws_snapshot = snapshot_workspace(verify_cwd)

        # 1. Run the agent's solution. Capture its outcome but continue
        #    to pytest no matter what.
        solver_rc: int | None = None
        solver_tail = ""
        try:
            proc = run_capture(
                [self.python, str(solve_path)],
                cwd=str(self.workspace),
                env=env,
                timeout=self.timeout_per_action,
            )
            solver_rc = proc.returncode
            solver_tail = (proc.stdout + proc.stderr)[-_SOLVER_TAIL_CHARS:]
        except subprocess.TimeoutExpired:
            solver_rc = -1
            solver_tail = f"solver script timed out after {self.timeout_per_action}s"

        # Place the script's root products under ``output/`` for the verifier;
        # a no-op in ``output_cwd`` mode (the agent already writes inside it).
        collect = collect_outputs(verify_cwd, pre_ws_snapshot)

        # 2. Adapt test file: rewrite /root/ and /app/ to workspace.
        # The adapted test goes into a HIDDEN dir adjacent to the
        # workspace (NOT inside it), so the agent's ``read_file`` /
        # ``list_files`` actions can't see the test source. Previously
        # this wrote ``_test_adapted.py`` directly into the workspace,
        # leaking every assertion to the agent on the next turn.
        test_text = self.test_path.read_text(encoding="utf-8", errors="replace")
        test_text = test_text.replace("/root/", f"{verify_cwd}/").replace("/app/", f"{verify_cwd}/")
        hidden_tests = verify_cwd.parent / f"_hidden_tests__{self.task_id}"
        hidden_tests.mkdir(parents=True, exist_ok=True)
        adapted = hidden_tests / "_test_adapted.py"
        adapted.write_text(test_text, encoding="utf-8")
        for f in self.test_path.parent.iterdir():
            if f.name == self.test_path.name:
                continue
            if f.is_file() and f.suffix == ".py":
                support = f.read_text(encoding="utf-8", errors="replace")
                support = support.replace("/root/", f"{verify_cwd}/").replace(
                    "/app/", f"{verify_cwd}/"
                )
                (hidden_tests / f.name).write_text(support, encoding="utf-8")
            elif f.is_file():
                import shutil as _sh

                _sh.copy2(f, hidden_tests / f.name)
            elif f.is_dir():
                # Copy auxiliary fixture/data dirs verbatim.
                import shutil as _sh

                target = hidden_tests / f.name
                if target.exists():
                    _sh.rmtree(target)
                _sh.copytree(f, target)

        # 3. Run pytest unconditionally. cwd stays at the workspace so
        # the agent's relative paths resolve, but the test file lives
        # outside the workspace so it isn't readable by the agent.
        try:
            tproc = run_capture(
                [
                    self.python,
                    "-m",
                    "pytest",
                    str(adapted),
                    "-v",
                    "--tb=short",
                    "--no-header",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=str(verify_cwd),
                env=env,
                timeout=self.timeout_per_action,
            )
        except subprocess.TimeoutExpired:
            self.last_pass_rate = 0.0
            self.total_tests = 0
            return (
                f"run_tests: pytest timed out after {self.timeout_per_action}s "
                f"(solver rc={solver_rc}, tail: {solver_tail})"
            )

        out = tproc.stdout + tproc.stderr
        counts = parse_pytest_counts(out)
        passed = int(counts["passed"])
        failed = int(counts["failed"])
        errors = int(counts["errors"])
        total = int(counts["total_tests"])
        self.last_pass_rate = (passed / total) if total else 0.0
        self.total_tests = total
        self.last_verifier_trace = {
            "solver_rc": solver_rc,
            "solver_tail": solver_tail,
            "pytest_rc": tproc.returncode,
            "pytest_output_tail": out[-12000:],
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "total_tests": total,
            "pass_rate": self.last_pass_rate,
            "hidden_tests_dir": str(hidden_tests),
            "adapted_test": str(adapted),
        }

        norm_note = ""
        if collect.error:
            norm_note = f" Output collection error: {collect.error}."
        elif collect.copied:
            norm_note = f" Collected {collect.copied} output file(s)."
        return render_pytest_observation(
            policy=resolve_feedback_policy(
                str(self.feedback_policy) if self.feedback_policy else None
            ),
            passed=passed,
            total=total,
            pass_rate=self.last_pass_rate,
            solver_rc=solver_rc,
            solver_tail=solver_tail,
            pytest_output=out,
            collect_note=norm_note,
        )
