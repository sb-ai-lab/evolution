"""Task solver — send task + skill to LLM, execute response, evaluate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from evolution.adapters.workspace_guard import build_guarded_env
from evolution.core.models import Skill, SkillTrace
from evolution.eval.evidence import (
    attach_verifier_result,
    build_evidence,
    snapshot_workspace,
)
from evolution.eval.python_env import resolve_python_for_role
from evolution.eval.runner import run_task
from evolution.eval.task import Task, TaskResult
from evolution.eval.workspace import setup_workspace
from evolution.lineage.traces import TraceStore
from evolution.lineage.tracker import LineageTracker
from evolution.llm.backend import LLMBackend, LLMResponse

if TYPE_CHECKING:
    from evolution.splits import SplitManifest

logger = logging.getLogger(__name__)

BASE_SOLVER_SYSTEM_PROMPT = """\
You are a skilled Python programmer solving a task.
You will be given a task instruction and optionally a skill document with guidance.

Your job: write a single Python script that solves the task.
{execution_contract}
- Use only standard library and common packages (openpyxl, pandas, pdfplumber, python-docx, python-pptx, numpy, scipy, etc.).
- Print progress to stdout so we can see what's happening.

Respond with ONLY a Python code block. No explanation before or after.

```python
# your solution here
```"""

ROOT_CWD_CONTRACT = """\
- The script runs in a workspace directory containing the input files.
- Read inputs from the current directory (e.g., `sc100-blank.pdf`).
- Write outputs to the paths the task instruction names, relative to the
  current directory (e.g., `sc100-filled.pdf` or `redacted/paper1.pdf`).
- Do NOT add an `output/` prefix; tests resolve required paths from the
  workspace root, not from an `output/` subdirectory."""

OUTPUT_CWD_CONTRACT = """\
- The harness runs your script from `<workspace>/output`.
- Read input files from the workspace root with parent-relative paths
  (for example `../input.pdf` or `../environment/data/file.csv`).
- Write required output artifacts relative to the current directory. If the
  verifier expects `output/result.json`, write `result.json`; do not write
  `output/result.json` from inside `<workspace>/output`."""

CODE_RETRY_SYSTEM_REMINDER = "Respond with ONLY a Python code block."
CODE_EXTRACTION_STUB = 'raise SystemExit("solver: model did not produce a Python code block")\n'


@dataclass
class SolveResult:
    """Result of solving a task with an LLM."""

    task_name: str
    llm_response: LLMResponse | None = None
    script_path: Path | None = None
    script_output: str = ""
    script_exit_code: int = -1
    eval_result: TaskResult | None = None
    errors: list[str] = field(default_factory=list)
    code_extraction_failed: bool = False
    code_extraction_retried: bool = False
    llm_transport_failed: bool = False
    solve_exception: str | None = None
    solve_exception_frame: str | None = None
    # Per-phase timing (ms). llm time lives on ``llm_response.time_ms``;
    # ``exec_ms`` is the solution run (solve.sh/python), ``eval_ms`` the
    # pytest evaluation. ``completion_ms`` is their sum (full task wall time).
    exec_ms: int = 0
    eval_ms: int = 0

    @property
    def completion_ms(self) -> int:
        llm_ms = self.llm_response.time_ms if self.llm_response else 0
        return llm_ms + self.exec_ms + self.eval_ms

    @property
    def success(self) -> bool:
        return self.eval_result is not None and self.eval_result.success

    @property
    def pass_rate(self) -> float:
        if self.eval_result is None:
            return 0.0
        return self.eval_result.pass_rate


def _build_workspace_context(workspace: Path) -> str:
    """Build a description of workspace files for the LLM prompt.

    Lists all files and includes contents of small text files so the model
    knows column names, data formats, etc.
    """
    TEXT_EXTS = {
        ".md",
        ".txt",
        ".csv",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
    }
    MAX_TEXT_SIZE = 8_000  # include text files up to 8KB
    PREVIEW_ROWS = 5  # for spreadsheets, show first N rows

    parts = []
    files = sorted(f for f in workspace.iterdir() if f.is_file() and not f.name.startswith("_"))

    if not files:
        return ""

    parts.append("## Workspace files\n")

    for f in files:
        size = f.stat().st_size
        parts.append(f"- `{f.name}` ({size:,} bytes)")

    # Include contents of small text files
    for f in files:
        if f.suffix.lower() in TEXT_EXTS and f.stat().st_size <= MAX_TEXT_SIZE:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                parts.append(f"\n### {f.name}\n```\n{content.strip()}\n```")
            except Exception:
                pass

    # For spreadsheets, show column names and first few rows
    for f in files:
        if f.suffix.lower() in (".xlsx", ".xls"):
            try:
                import pandas as pd

                xls = pd.ExcelFile(f)
                for sheet in xls.sheet_names:
                    df = pd.read_excel(xls, sheet_name=sheet, nrows=PREVIEW_ROWS)
                    if df.empty and len(df.columns) == 0:
                        parts.append(f'\n### {f.name} — sheet "{sheet}"\n(empty)')
                    else:
                        parts.append(
                            f'\n### {f.name} — sheet "{sheet}" '
                            f"(columns: {list(df.columns)})\n"
                            f"```\n{df.head(PREVIEW_ROWS).to_string()}\n```"
                        )
            except Exception:
                pass

    # For PDFs, list form fields
    for f in files:
        if f.suffix.lower() == ".pdf":
            try:
                import fitz

                doc = fitz.open(str(f))
                fields = []
                for page in doc:
                    for w in page.widgets():
                        fields.append(
                            f"  page {page.number}: {w.field_name} "
                            f"(type={w.field_type_string}, value={w.field_value!r})"
                        )
                if fields:
                    parts.append(
                        f"\n### {f.name} — form fields ({len(fields)} fields)\n"
                        f"```\n" + "\n".join(fields) + "\n```"
                    )
                doc.close()
            except Exception:
                pass

    return "\n".join(parts)


def _system_prompt_for_workspace(workspace: Path) -> tuple[str, str]:
    contract = OUTPUT_CWD_CONTRACT if (workspace / "solve.sh").is_file() else ROOT_CWD_CONTRACT
    return BASE_SOLVER_SYSTEM_PROMPT.format(execution_contract=contract), contract


def _write_prompt_audit(
    workspace: Path,
    *,
    system_prompt: str,
    prompt: str,
    execution_contract: str,
    skills: list[Skill],
    backend: LLMBackend,
    model_name: str,
    phase: str,
    attempt_seed: int | None,
    task_name: str,
) -> None:
    if os.environ.get("EVO_PERSIST_PROMPT_AUDIT", "1") == "0":
        return
    max_chars = 256 * 1024
    audit = {
        "model_id": model_name or getattr(backend, "model", ""),
        "phase": phase,
        "attempt_seed": attempt_seed,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "request_id": None,
        "execution_contract": execution_contract,
        "system": system_prompt[:max_chars],
        "user": prompt[:max_chars],
        "task": task_name,
        "skills": [
            {
                "name": s.frontmatter.name,
                "version": getattr(s.frontmatter, "version", None) or "",
                "body_chars": len(s.body or ""),
                "body_sha256": hashlib.sha256((s.body or "").encode("utf-8")).hexdigest(),
            }
            for s in skills
        ],
    }
    (workspace / "prompt_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def solve_task(
    task: Task,
    backend: LLMBackend,
    workspace: Path,
    skill: Skill | None = None,
    skills: list[Skill] | None = None,
    timeout: float = 300.0,
    evaluate: bool = True,
    model_name: str = "",
    manifest: SplitManifest | None = None,
    manifest_scope: str | None = None,
    attempt_seed: int | None = None,
    trace_spec_idx: int | None = None,
    round_id: str | None = None,
    phase: str = "collect",
) -> SolveResult:
    """Solve a task: LLM generates code, we execute it, then evaluate.

    Args:
        task: The task to solve.
        backend: LLM backend to use.
        workspace: Directory with input files (use setup_workspace first).
        skill: Optional single skill (backward compat, use skills instead).
        skills: Optional list of skills to inject into LLM context.
        timeout: Script execution timeout in seconds.
        evaluate: Whether to run evaluation tests after execution.
        model_name: Model identifier for trace recording (e.g. "claude-sonnet-4").
        manifest: Optional SplitManifest; if provided, the solver verifies
            that `task.name` is in the manifest's `manifest_scope` pool and
            records manifest identity in the saved trace. Fails with
            LeakageError if the task is outside the allowed scope.
        manifest_scope: One of "skill_evolution", "dev", "reporting". Required
            when `manifest` is supplied.

    Returns:
        SolveResult with LLM response, execution output, and eval results.
    """
    if manifest is not None:
        if manifest_scope is None:
            raise ValueError(
                "manifest_scope is required when manifest is provided "
                "(one of 'skill_evolution', 'dev', 'reporting')."
            )
        # Fail fast if this task is out of scope for the current run.
        manifest.assert_no_leakage(
            [task.name],
            scope=manifest_scope,
            context=f"solve_task({task.name})",
        )
    # Normalize: single skill → list
    resolved_skills: list[Skill] = []
    if skills:
        resolved_skills = skills
    elif skill is not None:
        resolved_skills = [skill]

    result = SolveResult(task_name=task.name)

    ws_pre_snapshot = snapshot_workspace(workspace)
    trace_ctx = {
        "trace_spec_idx": trace_spec_idx,
        "attempt_seed": attempt_seed,
        "source_model_id": model_name,
        "round_id": round_id,
        "phase": phase,
    }
    # Split-controlled feedback is collect-phase ONLY. Promotion-validation
    # traces NEVER carry source_feedback (plan §B) — structural, not merely
    # filtered at reflection time.
    feedback_enabled = os.environ.get("EVO_SOURCE_FEEDBACK") == "1" and phase in (
        "collect",
        "",
        None,
    )

    # Step 1: Get solution from LLM
    ws_context = _build_workspace_context(workspace)
    prompt = task.instruction
    if ws_context:
        prompt = f"{task.instruction}\n\n{ws_context}"
    messages = [{"role": "user", "content": prompt}]
    system_prompt, execution_contract = _system_prompt_for_workspace(workspace)

    # Save prompt even if the provider fails before returning a response; the
    # fallback stub below still needs an auditable workspace.
    (workspace / "_prompt.md").write_text(prompt, encoding="utf-8")

    _write_prompt_audit(
        workspace,
        system_prompt=system_prompt,
        prompt=prompt,
        execution_contract=execution_contract,
        skills=resolved_skills,
        backend=backend,
        model_name=model_name,
        phase=phase,
        attempt_seed=attempt_seed,
        task_name=task.name,
    )

    try:
        if resolved_skills:
            llm_resp = asyncio.run(
                backend.complete_with_skills(
                    messages,
                    resolved_skills,
                    system=system_prompt,
                    seed=attempt_seed,
                )
            )
        else:
            llm_resp = asyncio.run(
                backend.complete(messages, system=system_prompt, seed=attempt_seed)
            )
        result.llm_response = llm_resp
    except Exception as exc:
        result.errors.append(f"LLM call failed: {exc}; retrying once")
        try:
            if resolved_skills:
                llm_resp = asyncio.run(
                    backend.complete_with_skills(
                        messages,
                        resolved_skills,
                        system=system_prompt,
                    )
                )
            else:
                llm_resp = asyncio.run(
                    backend.complete(messages, system=system_prompt, seed=attempt_seed)
                )
            result.llm_response = llm_resp
        except Exception as retry_exc:
            result.errors.append(f"LLM call failed after retry: {retry_exc}; using stub _solve.py")
            result.code_extraction_failed = True
            result.llm_transport_failed = True
            llm_resp = LLMResponse(content="")
            result.llm_response = llm_resp

    # Save traces: prompt sent and full LLM response
    (workspace / "_response.md").write_text(llm_resp.content, encoding="utf-8")

    if (
        not result.llm_transport_failed
        and not (llm_resp.content or "").strip()
        and llm_resp.tokens_in == 0
        and llm_resp.tokens_out == 0
    ):
        result.llm_transport_failed = True
        result.code_extraction_failed = True
        result.errors.append("LLM returned no tokens (transport failure); using stub _solve.py")

    # Step 2: Extract Python code from response
    code = (
        CODE_EXTRACTION_STUB
        if result.code_extraction_failed
        else _extract_python_code(llm_resp.content)
    )
    if not code:
        result.code_extraction_retried = True
        result.errors.append("No Python code block found in LLM response; retrying once")
        retry_system = f"{system_prompt}\n\n{CODE_RETRY_SYSTEM_REMINDER}"
        try:
            if resolved_skills:
                retry_resp = asyncio.run(
                    backend.complete_with_skills(
                        messages,
                        resolved_skills,
                        system=retry_system,
                        seed=attempt_seed,
                    )
                )
            else:
                retry_resp = asyncio.run(
                    backend.complete(messages, system=retry_system, seed=attempt_seed)
                )
            result.llm_response = retry_resp
            llm_resp = retry_resp
            (workspace / "_response_retry.md").write_text(llm_resp.content, encoding="utf-8")
            code = _extract_python_code(llm_resp.content)
        except Exception as exc:
            result.errors.append(f"LLM retry after code extraction failure failed: {exc}")

    if not code:
        result.code_extraction_failed = True
        result.errors.append("Code extraction failed after retry; using stub _solve.py")
        code = CODE_EXTRACTION_STUB

    # Step 3: Write and execute the script via the task's layout. The layout
    # writes the solution where its run step expects it (``solution.py`` +
    # ``_solve.py``) and picks the run command — ``bash solve.sh`` when a
    # workspace solve.sh is present (both layouts), else ``python _solve.py``.
    # We keep a ``_solve.py`` debug copy regardless for trace/audit code.
    layout = task.resolve_layout()
    script_path = layout.place_solution(workspace, code)
    if not (workspace / "_solve.py").is_file():
        (workspace / "_solve.py").write_text(code, encoding="utf-8")
    result.script_path = script_path

    try:
        env = build_guarded_env(
            framework="evolution",
            workspace_dir=workspace,
            audit_log_path=workspace / ".workspace_guard.jsonl",
            base_env=None,
            command_wrappers=True,
        )
        run = layout.run_solution(
            workspace, resolve_python_for_role(task.role), timeout=timeout, env=env
        )
        result.script_output = (run.stdout or "") + (run.stderr or "")
        result.script_exit_code = run.returncode
        result.exec_ms = int(run.elapsed_sec * 1000)
        if run.timed_out:
            # Re-raise so the existing TimeoutExpired handler below runs.
            raise subprocess.TimeoutExpired(cmd="<layout>", timeout=timeout)

        if result.script_exit_code != 0:
            result.errors.append(f"Script exited with code {result.script_exit_code}")
            logger.warning(
                "Script failed for %s:\n%s",
                task.name,
                result.script_output[-500:],
            )
    except subprocess.TimeoutExpired:
        result.errors.append(f"Script timed out after {timeout}s")
        evidence = build_evidence(
            workspace=workspace,
            pre_snapshot=ws_pre_snapshot,
            solve_exit_code=result.script_exit_code,
            solve_timeout=True,
            code_extraction_failed=result.code_extraction_failed,
            code_extraction_retried=result.code_extraction_retried,
            required_outputs=getattr(task, "required_outputs", None),
            trace_context=trace_ctx,
            solve_stderr_tail=result.script_output,
            feedback_enabled=feedback_enabled,
            task_dir=task.path,
            test_file=task.test_file,
            manifest_scope=manifest_scope,
        )
        if resolved_skills and model_name:
            for s in resolved_skills:
                try:
                    _save_trace(
                        s,
                        task,
                        result,
                        model_name,
                        prompt,
                        result.llm_response.content if result.llm_response else "",
                        code,
                        manifest=manifest,
                        manifest_scope=manifest_scope,
                        evidence=evidence,
                        attempt_seed=attempt_seed,
                        trace_context=trace_ctx,
                    )
                except Exception as exc:
                    logger.warning("Failed to save timeout trace for %s: %s", s.name, exc)
        return result
    except Exception as exc:
        result.solve_exception = type(exc).__name__
        tb = traceback.extract_tb(exc.__traceback__)
        if tb:
            frame = tb[-1]
            result.solve_exception_frame = f"{Path(frame.filename).name}:{frame.lineno}"
        result.errors.append(f"Solve raised {type(exc).__name__}")
        evidence = build_evidence(
            workspace=workspace,
            pre_snapshot=ws_pre_snapshot,
            solve_exit_code=result.script_exit_code,
            solve_timeout=False,
            solve_diagnostic="exception",
            solve_exception=result.solve_exception,
            solve_exception_frame=result.solve_exception_frame,
            code_extraction_failed=result.code_extraction_failed,
            code_extraction_retried=result.code_extraction_retried,
            required_outputs=getattr(task, "required_outputs", None),
            trace_context=trace_ctx,
            solve_stderr_tail=result.script_output,
            feedback_enabled=feedback_enabled,
            task_dir=task.path,
            test_file=task.test_file,
            manifest_scope=manifest_scope,
        )
        if resolved_skills and model_name:
            for s in resolved_skills:
                try:
                    _save_trace(
                        s,
                        task,
                        result,
                        model_name,
                        prompt,
                        result.llm_response.content if result.llm_response else "",
                        code,
                        manifest=manifest,
                        manifest_scope=manifest_scope,
                        evidence=evidence,
                        attempt_seed=attempt_seed,
                        trace_context=trace_ctx,
                    )
                except Exception as save_exc:
                    logger.warning("Failed to save exception trace for %s: %s", s.name, save_exc)
        return result

    # Build evidence before pytest/verifier execution. The artifact manifest
    # must describe only files produced by the agent solve step, never files a
    # verifier may create while checking outputs.
    solve_timeout_flag = any("timed out" in str(e).lower() for e in (result.errors or []))
    evidence = build_evidence(
        workspace=workspace,
        pre_snapshot=ws_pre_snapshot,
        solve_exit_code=result.script_exit_code,
        solve_timeout=solve_timeout_flag,
        solve_exception=result.solve_exception,
        solve_exception_frame=result.solve_exception_frame,
        code_extraction_failed=result.code_extraction_failed,
        code_extraction_retried=result.code_extraction_retried,
        required_outputs=getattr(task, "required_outputs", None),
        trace_context=trace_ctx,
        solve_stderr_tail=result.script_output,
        feedback_enabled=feedback_enabled,
        task_dir=task.path,
        test_file=task.test_file,
        manifest_scope=manifest_scope,
    )

    # Place produced files where the verifier reads them (sb-bench copies root
    # files into output/; evo is a no-op). Fail loud: a copy IOError must not
    # masquerade as a silent pytest 0, so skip evaluation and surface the error.
    collect = layout.collect_outputs(workspace, ws_pre_snapshot)
    if collect.error:
        result.errors.append(f"output collection failed: {collect.error}")

    # Step 4: Evaluate
    if evaluate and not collect.error:
        eval_t0 = time.monotonic()
        result.eval_result = run_task(task, workspace)
        result.eval_ms = int((time.monotonic() - eval_t0) * 1000)
        if result.eval_result is not None:
            attach_verifier_result(
                evidence,
                passed=result.eval_result.passed,
                total=result.eval_result.total_tests,
                pytest_error_tail="\n".join(result.eval_result.errors or []),
                pytest_stdout=result.eval_result.raw_stdout,
                nodeids=result.eval_result.nodeids,
            )

    # Step 5: Save trace for each skill used
    if resolved_skills and result.eval_result is not None and model_name:
        for s in resolved_skills:
            try:
                _save_trace(
                    s,
                    task,
                    result,
                    model_name,
                    prompt,
                    result.llm_response.content if result.llm_response else "",
                    code,
                    manifest=manifest,
                    manifest_scope=manifest_scope,
                    evidence=evidence,
                    attempt_seed=attempt_seed,
                    trace_context=trace_ctx,
                )
            except Exception as exc:
                logger.warning("Failed to save trace for %s: %s", s.name, exc)

    return result


def _save_trace(
    skill: Skill,
    task: Task,
    result: SolveResult,
    model_name: str,
    prompt: str,
    response: str,
    code: str,
    manifest: SplitManifest | None = None,
    manifest_scope: str | None = None,
    evidence: dict | None = None,
    attempt_seed: int | None = None,
    trace_context: dict | None = None,
) -> None:
    """Save a skill trace after evaluation."""
    tracker = LineageTracker(skill)
    now = datetime.now()
    model_tag = model_name.replace("/", "-")

    if attempt_seed is None:
        trace_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{model_tag}_{task.name}"
    else:
        trace_id = (
            f"{now.strftime('%Y%m%d_%H%M%S')}{now.microsecond:06d}"
            f"_a{attempt_seed:04d}_{model_tag}_{task.name}"
        )

    er = result.eval_result
    trace = SkillTrace(
        id=trace_id,
        skill_name=skill.name,
        skill_version=tracker.current_version,
        model=model_name,
        task=task.name,
        timestamp=now,
        passed=er.passed if er else 0,
        total=er.total_tests if er else 0,
        pass_rate=er.pass_rate if er else 0.0,
        success=er.success if er else False,
        tokens_in=result.llm_response.tokens_in if result.llm_response else 0,
        tokens_out=result.llm_response.tokens_out if result.llm_response else 0,
        time_ms=result.llm_response.time_ms if result.llm_response else 0,
        errors=result.errors,
        manifest_name=manifest.name if manifest is not None else None,
        manifest_sha=manifest.sha256 if manifest is not None else None,
        manifest_scope=manifest_scope,
        trace_context=trace_context or None,
    )

    store = TraceStore(skill)
    store.save(trace, prompt, response, code, evidence=evidence)


def solve_task_pair(
    task: Task,
    backend: LLMBackend,
    workspace_dir: Path,
    skill: Skill | None = None,
    skills: list[Skill] | None = None,
    timeout: float = 300.0,
) -> tuple[SolveResult, SolveResult]:
    """Solve a task twice: once without skill, once with skill(s).

    This measures the skill's effectiveness (fitness = delta).

    Returns:
        (baseline_result, skill_result)
    """
    ws_base = Path(workspace_dir) / f"{task.name}-baseline"
    ws_skill = Path(workspace_dir) / f"{task.name}-with-skill"

    setup_workspace(task, ws_base)
    setup_workspace(task, ws_skill)

    baseline = solve_task(task, backend, ws_base, timeout=timeout)
    with_skill = solve_task(task, backend, ws_skill, skill=skill, skills=skills, timeout=timeout)

    return baseline, with_skill


def _looks_like_python_code(code: str) -> bool:
    """Return True when a response fragment is plausibly a Python script."""
    candidate = code.strip()
    if not candidate:
        return False
    try:
        compile(candidate, "<llm-response>", "exec")
    except SyntaxError:
        return False

    python_starters = (
        "import ",
        "from ",
        "def ",
        "class ",
        "async ",
        "if ",
        "for ",
        "while ",
        "try:",
        "with ",
        "raise ",
        "assert ",
        "return ",
        "print(",
        "print ",
        "@",
        "#",
    )
    for line in (line.strip() for line in candidate.splitlines() if line.strip()):
        if line.startswith(python_starters):
            return True
        if re.match(r"[A-Za-z_]\w*\s*=", line):
            return True
        if re.match(r"[A-Za-z_][\w.]*\(", line):
            return True
    return False


def _extract_python_code(text: str) -> str:
    """Extract Python code from a markdown response.

    Python-labeled fences are trusted as the model's intended script. Generic
    fences and bare responses must parse as plausible Python so prose is not
    accidentally written to ``solution.py``.
    """
    response = text.strip()
    if not response:
        return ""

    # Try ```python ... ``` / ```py ... ``` first. Anchor the CLOSING fence to
    # the start of a line so a literal ``` inside the code (e.g. a regex that
    # matches markdown code fences) cannot terminate the block early.
    m = re.search(
        r"```(?:python|py)[^\n]*\n(.*?)\n[ \t]*```[ \t]*(?:\n|$)",
        response,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:  # no line-anchored closer (closer shares a line with code) — best effort
        m = re.search(r"```(?:python|py)\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Try generic ``` ... ``` blocks, but only accept plausible Python.
    for m in re.finditer(r"```[^\n]*\n(.*?)\n[ \t]*```[ \t]*(?:\n|$)", response, re.DOTALL):
        code = m.group(1).strip()
        if _looks_like_python_code(code):
            return code

    # If the whole response looks like code (no markdown), use it directly.
    if _looks_like_python_code(response):
        return response

    return ""
