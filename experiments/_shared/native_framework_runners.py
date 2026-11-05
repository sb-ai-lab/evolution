"""Phase 2 — solve every (framework, held-out task, condition, trial) cell
through the framework's NATIVE test runtime.

Cache layout per cell:

  artifacts/solver_cache/<fw>/<task_id>/<condition>/trial_<n>/
      transcript.txt   (Hermes/Memento/MemP — full conversation/trajectory)
      prompt.md        (Evolution only — single uniform prompt)
      workspace/       (final workspace state)
      pytest.log       (last 4KB)
      result.json      (pass_rate, total_tests, elapsed_s, max_iter_used,
                        ts, llm_calls, tokens_in_total, tokens_out_total,
                        latency_ms_total, per_turn=[...], native_metrics)

Conditions:
  description_only  — runtime sees no skills/memory.
  default_skill     — runtime sees the task's bundled
                      `environment/skills/*/SKILL.md` files.
  post_train        — runtime sees Phase-1's harvested learned state.

Concurrency: thread pool with --workers 16 against the OpenRouter
solver budget. Each framework's solver entrypoint is independent.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from typing import Any

import tomllib

# Hermes test trials previously needed a process-level lock because the
# in-process AIAgent mutates os.chdir() + HERMES_HOME globally. We now
# dispatch Hermes through ``_hermes_subprocess_runner.py`` — each trial
# is a fresh Python process with its own globals and cwd, so the lock is
# unnecessary and the thread pool can run Hermes trials in parallel.

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))
sys.path.insert(0, str(Path(__file__).parent))

_SKILL_BODY_CHAR_LIMIT = int(os.environ.get("SKILL_BODY_CHAR_LIMIT", "262144"))
_RESULT_PREVIEW_CHARS = int(os.environ.get("EVO_RESULT_PREVIEW_CHARS", "40000"))
_WORKSPACE_LISTING_MAX = int(os.environ.get("EVO_WORKSPACE_LISTING_MAX", "2000"))

logging.basicConfig(
    level=os.environ.get("PILOT_LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("solver_native")

os.environ.setdefault("LITELLM_LOG", "ERROR")
os.environ.setdefault("OPENAI_LOG", "warning")

from _shared.llm_telemetry import read_telemetry_jsonl as _read_telemetry_jsonl  # noqa: E402
from _shared.preflight_common import load_env_silent as _load_env_silent  # noqa: E402
from _shared.pytest_runner import (  # noqa: E402
    run_pytest_against_workspace as _run_pytest_shared,
)
from _shared.workspace_materializer import (  # noqa: E402
    WorkspaceMaterializationError,
    materialize_workspace,
    write_agent_solve_sh,
)
from evolution.adapters._envelopes import (  # noqa: E402
    FILE_OUTPUT_EXAMPLE_FLAT,
    FILE_OUTPUT_EXAMPLE_NESTED,
    NO_PACKAGE_INSTALL_RULE,
    TASK_INSTRUCTION_BLOCK,
    WORKSPACE_LISTING_HEADER,
)
from evolution.adapters.workspace_guard import build_guarded_env, make_workspace_guard  # noqa: E402
from evolution.eval.evidence import snapshot_workspace  # noqa: E402
from evolution.eval.layouts.base import collect_outputs  # noqa: E402
from evolution.eval.python_env import resolve_python_for_role  # noqa: E402
from evolution.eval.solver import CODE_EXTRACTION_STUB, _extract_python_code  # noqa: E402


def _clip_middle_text(text: str, limit: int, *, label: str = "text") -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = f"\n\n[... {label} clipped: {len(text) - limit} chars omitted from middle ...]\n\n"
    if len(marker) >= limit:
        return text[:limit], True
    head = max(1, (limit - len(marker)) // 2)
    tail = max(1, limit - len(marker) - head)
    return text[:head] + marker + text[-tail:], True


def _preview_text(text: str, limit: int = _RESULT_PREVIEW_CHARS, *, label: str = "text") -> str:
    preview, _truncated = _clip_middle_text(text or "", limit, label=label)
    return preview


def _extract_memento_skill_mentions(text: str) -> list[str]:
    mentions: set[str] = set()
    for pattern in (
        r"\bexecute_skill\b\s+with\s+name\s+['\"]?([A-Za-z0-9_.-]+)",
        r"\bexecute_skill(?:<\|[^>]*\|>)?[^A-Za-z0-9_.-]{0,40}([A-Za-z0-9_.-]+)",
        r"\b(?:Executing|Invoking|Selected|Using)\s+skill\s+['\"]?([A-Za-z0-9_.-]+)",
    ):
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            name = match.group(1).strip("`'\".,:;()[]{}")
            if name and name.lower() not in {
                "skill",
                "tool",
                "installed",
                "unknown",
                "with",
                "name",
            }:
                mentions.add(name)
    return sorted(mentions)[:20]


def _extract_evoskill_final_answer_from_result(result: object) -> tuple[str, str]:
    def _from_obj(obj: object) -> str:
        if isinstance(obj, dict):
            value = obj.get("final_answer") or obj.get("answer")
            return str(value) if value else ""
        return ""

    direct = _from_obj(result)
    if direct:
        return direct, "trace.result"
    if not isinstance(result, str):
        return "", ""

    text = result.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:])
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3].strip()

    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        extracted = _from_obj(parsed)
        if extracted:
            return extracted, "trace.result"

    # Some OpenAI-compatible agents return a JSON-looking object where a later
    # field is malformed, while the final_answer string itself is valid. Recover
    # that native output instead of discarding the generated program.
    match = re.search(
        r'"(?:final_answer|answer)"\s*:\s*("(?:(?:\\.)|[^"\\])*")',
        text,
        flags=re.DOTALL,
    )
    if match:
        try:
            extracted = json.loads(match.group(1))
        except json.JSONDecodeError:
            extracted = ""
        if extracted:
            return str(extracted), "trace.result.final_answer"
    return "", ""


def _coerce_evoskill_final_answer(trace: object) -> tuple[str, str, str]:
    output = getattr(trace, "output", None)
    if output is not None:
        final_answer = str(getattr(output, "final_answer", "") or "")
        if final_answer:
            return final_answer, "trace.output", ""
    final_answer, source = _extract_evoskill_final_answer_from_result(getattr(trace, "result", ""))
    if final_answer:
        return final_answer, source, ""
    return "", "", "missing structured output"


def _optional_positive_float_env(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _subprocess_captured_text(value: str | bytes | None) -> str:
    """Normalize TimeoutExpired output/stderr to str for text-mode subprocesses."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_process_group(
    cmd: list[str],
    *,
    timeout_s: int | float,
    capture_output: bool = True,
    text: bool = True,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess in its own process group and kill descendants on timeout."""

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                # bound this drain: an escaped grandchild (own session) can hold
                # the captured pipe open after the child dies → unbounded hang.
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = None, None
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout_s,
            output=stdout,
            stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _rmtree_retry(path: Path, *, attempts: int = 5, delay_s: float = 0.1) -> None:
    """Remove a tree that may still have subprocess/logging handles closing."""
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(delay_s * attempt)


def _rmtree_best_effort(path: Path) -> None:
    with suppress(Exception):
        _rmtree_retry(path)


def _kill_processes_matching_needles(needles: list[str], *, grace_s: float = 2.0) -> list[int]:
    """Best-effort cleanup for native framework children that escaped a wrapper PGID."""
    needles = [n for n in needles if n and len(n) >= 8]
    if not needles:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []

    current = os.getpid()
    matched: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == current:
            continue
        if "_memento_subprocess_wrapper.py" in cmd:
            continue
        text_parts = [cmd]
        with suppress(OSError):
            text_parts.append(os.readlink(f"/proc/{pid}/cwd"))
        with suppress(OSError):
            env_bytes = Path(f"/proc/{pid}/environ").read_bytes()
            text_parts.append(env_bytes.decode("utf-8", errors="replace"))
        haystack = "\n".join(text_parts)
        if any(needle in haystack for needle in needles):
            matched.append(pid)

    for pid in matched:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    if matched:
        time.sleep(max(0.0, grace_s))
    for pid in matched:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
    return matched


def _task_role(task_dir: Path) -> str | None:
    task_toml = Path(task_dir) / "task.toml"
    if not task_toml.is_file():
        return None
    try:
        data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except Exception:
        return None
    role = ((data.get("task") or {}).get("role") or "").strip()
    return role or None


def _python_for_task(task_dir: Path) -> str:
    if os.environ.get("EVO_DISABLE_ROLE_PYTHON", "0") == "1":
        return sys.executable
    role = _task_role(task_dir)
    return resolve_python_for_role(role) if role else sys.executable


def _all_llm_calls_failed(per_turn: list[dict]) -> bool:
    """Return True when telemetry shows calls happened, but none succeeded."""
    return bool(per_turn) and not any(bool(turn.get("ok", True)) for turn in per_turn)


def _llm_failure_result(
    *,
    framework: str,
    error: str,
    elapsed_s: float,
    per_turn: list[dict],
    totals: dict,
) -> dict:
    return {
        "outcome": "hard_fail",
        "error": error,
        "elapsed_s": round(elapsed_s, 1),
        "llm_calls": totals["llm_calls"],
        "tokens_in_total": totals["tokens_in_total"],
        "tokens_out_total": totals["tokens_out_total"],
        "latency_ms_total": totals["latency_ms_total"],
        "per_turn": per_turn,
        "native_metrics": {
            "llm_all_calls_failed": True,
            "framework": framework,
            "telemetry_parse_errors": totals["telemetry_parse_errors"],
        },
    }


# ---------------------------------------------------------------------------
# Workspace setup + pytest
# ---------------------------------------------------------------------------


def _setup_workspace(task_dir: Path, ws: Path) -> dict[str, str]:
    """Mirror task_dir into ws and snapshot the pre-agent state.

    Supports both the legacy SkillsBench layout (``task_dir/inputs/``) and the
    new sb-bench layout (whole task tree with ``environment/``, ``output/``,
    ``data_generator.py``, ``solve.sh``, ``tests/``).

    Returns a relpath->sha256 snapshot of the staged workspace (inputs +
    generated data) so a caller can later identify agent-produced files and
    place them with ``collect_outputs``.
    """
    try:
        materialize_workspace(task_dir, ws, agent_solve_sh=True)
    except WorkspaceMaterializationError as exc:
        # A corpus-level setup defect should be scored as an attempted task
        # failure, not crash the whole framework cell. Keep the marker inside
        # the pre-agent snapshot so it is visible in cached artifacts but not
        # treated as an agent-produced output.
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "output").mkdir(exist_ok=True)
        write_agent_solve_sh(ws / "solve.sh", python=_python_for_task(task_dir))
        (ws / "_workspace_materialization_error.json").write_text(
            json.dumps(
                {
                    "task_dir": str(task_dir),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log.warning("workspace materialization failed for %s: %s", task_dir, exc)
    return snapshot_workspace(ws)


def _run_pytest(task_dir: Path, ws: Path, *, python: str, timeout_s: float = 240.0) -> dict:
    """Execute the task's pytest in the workspace.

    Thin adapter over ``_shared.pytest_runner.run_pytest_against_workspace``.
    """
    return _run_pytest_shared(task_dir, ws, python=python, timeout_s=timeout_s)


def _ensure_solution_py(ws: Path, fallback_code: str = "") -> None:
    """Ensure ``ws/solution.py`` exists for the new sb-bench solve.sh contract.

    Frameworks that write files via tools (Hermes/Memento/MemP) typically
    leave ``ws/solution.py`` empty; the test fixture only requires that
    ``bash solve.sh`` exits 0 — verification is by output file inspection.
    """
    sol = ws / "solution.py"
    if sol.is_file() and sol.stat().st_size > 0:
        return
    code = _extract_python_code(fallback_code or "")
    if not code:
        # No-op stub so solve.sh exits 0; tests verify file artifacts.
        code = '"""auto-generated stub: solution code lived in tool calls."""\n'
    sol.write_text(code, encoding="utf-8")


def _run_agent_solution(
    ws: Path, cache_dir: Path, *, timeout_s: float, python: str | None = None
) -> None:
    """Execute the agent's solution.py at cwd=ws (its envelope's workspace-root contract); collect_outputs relocates any root artifact into output/."""
    sol = ws / "solution.py"
    if not sol.is_file() or sol.stat().st_size == 0:
        (cache_dir / "solve_run.log").write_text(
            f"solution.py missing/empty at {sol}", encoding="utf-8"
        )
        return
    solve_timeout_s = max(60.0, float(timeout_s) - 30.0)
    try:
        sproc = _run_process_group(
            [python or sys.executable, str(sol)],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout_s=solve_timeout_s,
        )
        solve_log = (sproc.stdout or "") + "\n--- stderr ---\n" + (sproc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        solve_log = f"solution.py TIMEOUT after {exc.timeout}s"
    except Exception as exc:  # noqa: BLE001
        solve_log = f"solution.py exec error: {exc!r}"
    (cache_dir / "solve_run.log").write_text(solve_log, encoding="utf-8")


def _persist_native_debug_artifacts(cache_dir: Path, ws: Path, pytest_res: dict) -> None:
    """Keep native-run evidence in the same shape as the neutral solver cache."""

    (cache_dir / "pytest.log").write_text(str(pytest_res.get("log_tail") or ""), encoding="utf-8")
    (cache_dir / "pytest_full.log").write_text(
        str(pytest_res.get("log_full") or pytest_res.get("log_tail") or ""),
        encoding="utf-8",
    )
    for name in ("_solve.py", "solution.py"):
        src = ws / name
        if src.is_file():
            target = cache_dir / ("solve.py" if name == "_solve.py" else name)
            shutil.copy2(src, target)
            if name == "solution.py" and not (cache_dir / "solve.py").is_file():
                shutil.copy2(src, cache_dir / "solve.py")
            break


# ---------------------------------------------------------------------------
# Default-skill loading from raw sb-bench paths
# ---------------------------------------------------------------------------


def _load_default_skill_texts(raw_sbbench_path: Path) -> list[tuple[str, str]]:
    """Return (skill_name, body) tuples for every SKILL.md under environment/skills/.

    Supports two on-disk layouts:
      * Legacy:  ``skills/<skill_name>/SKILL.md``
      * New sb-bench: ``skills/SKILL_HANDCRAFT.md`` when present, otherwise
        ``skills/SKILL.md`` — name is parsed from the YAML frontmatter
        ``name:`` field, falling back to the parent task's role-derived name.
    """
    out: list[tuple[str, str]] = []
    skills_dir = raw_sbbench_path / "environment" / "skills"
    if not skills_dir.is_dir():
        return out

    def _parse_name(body: str, fallback: str) -> str:
        # Look for `name: foo` in the first 30 lines (YAML frontmatter).
        for line in body.splitlines()[:30]:
            line = line.strip()
            if line.lower().startswith("name:"):
                v = line.split(":", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
        return fallback

    # New layout: preferred skill file directly under skills/
    direct = (
        skills_dir / "SKILL_HANDCRAFT.md"
        if (skills_dir / "SKILL_HANDCRAFT.md").is_file()
        else skills_dir / "SKILL.md"
    )
    if direct.is_file():
        body = direct.read_text(encoding="utf-8", errors="replace")
        name = _parse_name(body, fallback=skills_dir.parent.parent.name)
        out.append((name, body))

    # Legacy layout: subdirectories
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        sm = child / "SKILL.md"
        if sm.is_file():
            body = sm.read_text(encoding="utf-8", errors="replace")
            out.append((child.name, body))
    return out


# ---------------------------------------------------------------------------
# Evolution test runtime
# ---------------------------------------------------------------------------

EVOLUTION_SYSTEM = """\
You are a skilled Python programmer solving a task. Write a single
self-contained Python script that solves the task using the workspace
files present at the time the script runs. Follow the execution contract
in the user prompt exactly. Respond with ONLY a Python code block:

```python
# solution
```
"""


_CWD_CONTRACT = (
    "## Execution contract (read carefully)\n"
    "- The harness runs your script with the working directory set to "
    "`<workspace>/output/`. The workspace listing below is shown relative "
    "to the workspace ROOT, one level above your CWD.\n"
    "- To READ input files (e.g. `model_report.pdf`, `legacy_eval_report.pdf`, "
    "anything in `environment/`), use a parent-relative path like "
    "`../<file>` (e.g. `pdfplumber.open('../model_report.pdf')`).\n"
    "- To WRITE output files, write to the current directory `./<file>` "
    "(e.g. `df.to_csv('top10_features.csv')`). Tests look in "
    "`<workspace>/output/<file>`.\n"
    "- If a reused skill template creates an `output/` subdirectory anyway, "
    "the harness will normalize that accidental `output/output/` nesting "
    "before pytest. Prefer direct current-directory writes for new code.\n"
    "- Do NOT use absolute paths or hardcoded `/root/`/`/app/` prefixes."
)


def _build_evolution_prompt(instruction: str, ws: Path, skill_bodies: list[str]) -> str:
    listing: list[str] = []
    if ws.is_dir():
        for p in sorted(ws.iterdir()):
            if p.is_dir():
                listing.append(f"  {p.name}/")
                try:
                    children = sorted(p.iterdir())
                except OSError:
                    children = []
                for sub in children[:20]:
                    if sub.is_dir():
                        listing.append(f"    {sub.name}/")
                        try:
                            grand = sorted(sub.iterdir())[:10]
                        except OSError:
                            grand = []
                        for g in grand:
                            tag = "/" if g.is_dir() else f" ({g.stat().st_size}B)"
                            listing.append(f"      {g.name}{tag}")
                    else:
                        listing.append(f"    {sub.name} ({sub.stat().st_size}B)")
                if len(children) > 20:
                    listing.append(f"    … ({len(children) - 20} more)")
            else:
                listing.append(f"  {p.name} ({p.stat().st_size}B)")
    parts = [
        instruction,
        "",
        _CWD_CONTRACT,
        "",
        "Workspace listing (paths relative to workspace ROOT):\n"
        + "\n".join(listing[:_WORKSPACE_LISTING_MAX]),
    ]
    for i, body in enumerate(skill_bodies, 1):
        parts.append("")
        parts.append(f"<skill_content #{i}>")
        parts.append(body[:_SKILL_BODY_CHAR_LIMIT])
        parts.append(f"</skill_content #{i}>")
    return "\n".join(parts)


def _evolution_chat(
    prompt: str,
    *,
    base: str,
    key: str,
    model_id: str,
    max_tokens: int = 8000,
    temperature: float = 0.2,
    frequency_penalty: float | None = None,
    extra_body: dict | None = None,
    seed: int | None = None,
) -> tuple[str, dict]:
    from contextlib import nullcontext

    from _shared.local_endpoint_gate import gate_for, is_local_endpoint
    from evolution.llm.litellm_backend import LiteLLMBackend

    kwargs: dict = {
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
    }
    if frequency_penalty is not None:
        kwargs["frequency_penalty"] = float(frequency_penalty)
    if extra_body:
        kwargs["extra_body"] = dict(extra_body)
    litellm_model = model_id
    if "openrouter.ai" in (base or "") and not litellm_model.startswith("openrouter/"):
        # OpenRouter requires the LiteLLM provider prefix; the raw OpenAI-compatible
        # API path used the provider's model slug directly.
        litellm_model = f"openrouter/{litellm_model}"
    elif is_local_endpoint(base) and not litellm_model.startswith("openai/openai/"):
        # LiteLLM strips one provider prefix; wrap the served name so it is sent
        # verbatim (vLLM serves names like "openai/gpt-oss-120b", sglang bare ones).
        litellm_model = f"openai/{litellm_model}"
    backend = LiteLLMBackend(
        model=litellm_model,
        api_base=base,
        api_key=key or "EMPTY",
        **kwargs,
    )

    async def _call_backend():
        return await backend.complete(
            [{"role": "user", "content": prompt}],
            system=EVOLUTION_SYSTEM,
            seed=int(seed) if seed is not None else None,
        )

    def _run_async_sync():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_call_backend())
        result: list[Any] = []
        error: list[BaseException] = []

        def _target() -> None:
            try:
                result.append(asyncio.run(_call_backend()))
            except BaseException as exc:  # noqa: BLE001 - propagate exactly below.
                error.append(exc)

        thread = threading.Thread(target=_target, name="evolution-litellm-call", daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0]

    # Gate only local endpoints (shared cross-process GPU budget); remote
    # provider calls use the provider's own rate limiting.
    _gate = gate_for(base).permit() if is_local_endpoint(base) else nullcontext()
    with _gate:
        t0 = time.time()
        r = _run_async_sync()
        dt_ms = (time.time() - t0) * 1000
    return r.content or "", {
        "tokens_in": int(getattr(r, "tokens_in", 0) or 0),
        "tokens_out": int(getattr(r, "tokens_out", 0) or 0),
        "latency_ms": round(dt_ms, 1),
        "model_id": model_id,
        "litellm_model_id": litellm_model,
        "retry_errors": [],
        "transport": "evolution.llm.litellm_backend",
    }


def _write_prompt_audit(
    path: Path,
    *,
    prompt: str,
    skill_bodies: list[str],
    model_id: str,
    seed: int | None,
) -> None:
    if os.environ.get("EVO_PERSIST_PROMPT_AUDIT", "1") == "0":
        return
    max_chars = 256 * 1024
    payload = {
        "system": EVOLUTION_SYSTEM[:max_chars],
        "user": prompt[:max_chars],
        "execution_contract": _CWD_CONTRACT,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_id": model_id,
        "request_id": None,
        "seed": seed,
        "skills": [
            {
                "index": idx,
                "body_chars": len(body),
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }
            for idx, body in enumerate(skill_bodies, start=1)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_evolution_test(
    *,
    task_dir: Path,
    raw_sbbench_path: Path,
    instruction: str,
    condition: str,
    cache_dir: Path,
    solver_cfg: dict,
    api_key: str,
    post_train_skill_bodies: list[str] | None = None,
    default_skill_bodies: list[str] | None = None,
    timeout_s: int = 300,
) -> dict:
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    ws = cache_dir / "workspace"
    _setup_workspace(task_dir, ws)
    make_workspace_guard(
        framework="evolution",
        workspace_dir=ws,
        audit_log_path=cache_dir / "workspace_guard.jsonl",
    ).log(
        event="setup",
        decision="allow",
        action="evolution_test_runtime",
        subject=str(ws),
    )

    if condition in ("description_only", "no_skill"):
        skill_bodies: list[str] = []
    elif condition == "default_skill":
        # AFTER tasks ship no environment/skills; caller supplies the seed body.
        skill_bodies = (
            list(default_skill_bodies)
            if default_skill_bodies is not None
            else [body for _, body in _load_default_skill_texts(raw_sbbench_path)]
        )
    else:  # post_train
        skill_bodies = list(post_train_skill_bodies or [])

    prompt = _build_evolution_prompt(instruction, ws, skill_bodies)
    (cache_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    started = time.time()
    # Local sglang serves the bare model id (no provider prefix). Prefer
    # ``served_id_hint`` whenever the manifest declares one — for cloud
    # providers the hint is identical to ``id`` so this is a no-op. Without
    # this, sglang 404s on ``openai/<id>``.
    chat_model_id = solver_cfg.get("served_id_hint") or solver_cfg["id"]
    chat_max_tokens = int(solver_cfg.get("max_output_tokens") or 8000)
    chat_temperature = float(solver_cfg.get("temperature", 0.2))
    chat_frequency_penalty = solver_cfg.get("frequency_penalty")
    chat_extra_body = solver_cfg.get("extra_body") or None
    chat_seed = solver_cfg.get("seed")
    _write_prompt_audit(
        cache_dir / "prompt_audit.json",
        prompt=prompt,
        skill_bodies=skill_bodies,
        model_id=chat_model_id,
        seed=chat_seed,
    )
    try:
        response, usage = _evolution_chat(
            prompt,
            base=solver_cfg["api_base"],
            key=api_key,
            model_id=chat_model_id,
            max_tokens=chat_max_tokens,
            temperature=chat_temperature,
            frequency_penalty=chat_frequency_penalty,
            extra_body=chat_extra_body,
            seed=chat_seed,
        )
    except Exception as exc:
        (cache_dir / "eval_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return {
            "outcome": "hard_fail",
            "error": f"llm-fail: {exc!r}"[:300],
            "elapsed_s": round(time.time() - started, 1),
        }
    code = _extract_python_code(response)
    code_extraction_failed = not bool(code)
    if code_extraction_failed:
        code = CODE_EXTRACTION_STUB
    (cache_dir / "response.md").write_text(response, encoding="utf-8")
    (cache_dir / "solve.py").write_text(code, encoding="utf-8")
    (ws / "_solve.py").write_text(code, encoding="utf-8")
    # New sb-bench: solve.sh runs ws/solution.py from ws/output.
    (ws / "solution.py").write_text(code, encoding="utf-8")
    pre_solve_snapshot = snapshot_workspace(ws)
    # CRITICAL: sb-bench tests assert on output files produced by solve.sh.
    # The pytest fixture ``run_solution`` is a no-op stub upstream — the
    # harness is expected to execute solve.sh BEFORE pytest. Without this
    # step every test that checks an output artifact fails with
    # FileNotFoundError, regardless of solver capability, capping pass_rate
    # at the floor of input-only tests.
    solve_sh = ws / "solve.sh"
    solve_log = ""
    if solve_sh.is_file():
        solve_timeout_s = max(60.0, float(timeout_s) - 30.0)
        try:
            sproc = _run_process_group(
                ["bash", str(solve_sh)],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout_s=solve_timeout_s,
            )
            solve_log = (sproc.stdout or "") + "\n--- stderr ---\n" + (sproc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            solve_log = f"solve.sh TIMEOUT after {exc.timeout}s"
            (cache_dir / "solve_run.log").write_text(solve_log, encoding="utf-8")
            return {
                "outcome": "hard_fail",
                "error": solve_log,
                "elapsed_s": round(time.time() - started, 1),
                "pass_rate": 0.0,
                "total_tests": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "pytest_status": "not_run",
                "llm_calls": 1,
                "tokens_in_total": usage["tokens_in"],
                "tokens_out_total": usage["tokens_out"],
                "latency_ms_total": usage["latency_ms"],
                "per_turn": [usage],
                "code_extraction_failed": code_extraction_failed,
                "code_extraction_retried": False,
                "native_metrics": {
                    "n_skill_bodies": len(skill_bodies),
                    "skill_prompt_chars": sum(len(b) for b in skill_bodies),
                    "code_extraction_failed": code_extraction_failed,
                    "code_extraction_retried": False,
                    "solve_rc": -1,
                },
            }
        except Exception as exc:  # noqa: BLE001
            solve_log = f"solve.sh exec error: {exc!r}"
    else:
        solve_log = f"solve.sh missing at {solve_sh}"
    (cache_dir / "solve_run.log").write_text(solve_log, encoding="utf-8")
    collect = collect_outputs(ws, pre_solve_snapshot)
    if collect.error:
        return {
            "outcome": "hard_fail",
            "error": f"output collection failed: {collect.error}"[:300],
            "elapsed_s": round(time.time() - started, 1),
            "pass_rate": 0.0,
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "pytest_status": "not_run",
            "llm_calls": 1,
            "tokens_in_total": usage["tokens_in"],
            "tokens_out_total": usage["tokens_out"],
            "latency_ms_total": usage["latency_ms"],
            "per_turn": [usage],
            "code_extraction_failed": code_extraction_failed,
            "code_extraction_retried": False,
            "native_metrics": {
                "n_skill_bodies": len(skill_bodies),
                "skill_prompt_chars": sum(len(b) for b in skill_bodies),
                "code_extraction_failed": code_extraction_failed,
                "code_extraction_retried": False,
                "collect_copied": collect.copied,
                "collect_flattened": collect.flattened,
                "collect_error": collect.error,
            },
        }
    task_python = _python_for_task(task_dir)
    pytest_res = _run_pytest(task_dir, ws, python=task_python, timeout_s=timeout_s)
    (cache_dir / "pytest.log").write_text(pytest_res["log_tail"], encoding="utf-8")
    (cache_dir / "pytest_full.log").write_text(
        pytest_res.get("log_full", pytest_res["log_tail"]),
        encoding="utf-8",
    )
    return {
        "outcome": "ok",
        "pass_rate": pytest_res["pass_rate"],
        "total_tests": pytest_res["total_tests"],
        "passed": pytest_res["passed"],
        "failed": pytest_res["failed"],
        "errors": pytest_res["errors"],
        "pytest_status": pytest_res.get("pytest_status", "completed"),
        "pytest_full": pytest_res.get("log_full", pytest_res["log_tail"]),
        "elapsed_s": round(time.time() - started, 1),
        "llm_calls": 1,
        "tokens_in_total": usage["tokens_in"],
        "tokens_out_total": usage["tokens_out"],
        "latency_ms_total": usage["latency_ms"],
        "per_turn": [usage],
        "code_extraction_failed": code_extraction_failed,
        "code_extraction_retried": False,
        "native_metrics": {
            "n_skill_bodies": len(skill_bodies),
            "skill_prompt_chars": sum(len(b) for b in skill_bodies),
            "code_extraction_failed": code_extraction_failed,
            "code_extraction_retried": False,
            "collect_copied": collect.copied,
            "collect_flattened": collect.flattened,
        },
    }


# ---------------------------------------------------------------------------
# LLM telemetry helpers (shared by Hermes + Memento subprocess runners)
# ---------------------------------------------------------------------------
# `_read_telemetry_jsonl` is now imported from `_shared.llm_telemetry` at
# module top — same return shape, accepts the legacy schema unchanged.


# ---------------------------------------------------------------------------
# Hermes test runtime — subprocess-isolated for parallel-safety
# ---------------------------------------------------------------------------


def run_hermes_test(
    *,
    task_dir: Path,
    raw_sbbench_path: Path,
    instruction: str,
    condition: str,
    cache_dir: Path,
    solver_cfg: dict,
    aux_cfg: dict,
    api_key: str,
    aux_key: str,
    post_train_skills_dir: Path | None = None,
    max_iterations: int = 90,
    timeout_s: float = 300.0,
) -> dict:
    """Run Hermes's AIAgent in a fresh subprocess per trial.

    The subprocess (``_hermes_subprocess_runner.py``) sets up
    ``HERMES_HOME``, monkey-patches ``openai.OpenAI`` for token telemetry,
    instantiates ``AIAgent`` with cwd pinned to the workspace, and
    writes a result JSON. The parent then reads telemetry JSONL and
    aggregates it into the cached ``result.json``. No process-level lock
    is required — each subprocess has its own globals and cwd, so the
    orchestrator's threadpool can run Hermes trials in parallel.
    """
    hermes_cap = _optional_positive_float_env("EVO_HERMES_NATIVE_TIMEOUT_S")
    if hermes_cap is not None:
        timeout_s = min(float(timeout_s), hermes_cap)
    request_timeout_s = (
        _optional_positive_float_env("EVO_HERMES_REQUEST_TIMEOUT_S")
        or _optional_positive_float_env("HERMES_API_TIMEOUT")
        or 240.0
    )
    stale_timeout_s = (
        _optional_positive_float_env("EVO_HERMES_STALE_TIMEOUT_S")
        or _optional_positive_float_env("HERMES_API_CALL_STALE_TIMEOUT")
        or 180.0
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    ws = cache_dir / "workspace"
    pre_snapshot = _setup_workspace(task_dir, ws)

    short_root = Path(tempfile.mkdtemp(prefix="hermes_native_"))
    short_ws = short_root / "w"
    shutil.copytree(ws, short_ws)
    home_artifact = cache_dir / "_hermes_home"
    home = short_root / "h"
    if home_artifact.exists():
        _rmtree_retry(home_artifact)
    if home.exists():
        _rmtree_retry(home)
    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_bodies_for_audit: list[str] = []

    def _sync_short_paths(*, remove_tmp: bool = False) -> None:
        if short_ws.is_dir():
            if ws.exists():
                _rmtree_retry(ws)
            shutil.copytree(short_ws, ws, symlinks=False)
        if home.is_dir():
            if home_artifact.exists():
                _rmtree_retry(home_artifact)
            shutil.copytree(
                home,
                home_artifact,
                symlinks=False,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            cfg = home_artifact / "config.yaml"
            if cfg.is_file():
                lines = []
                for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip().startswith("api_key:"):
                        indent = line[: len(line) - len(line.lstrip())]
                        lines.append(f"{indent}api_key: '<redacted>'")
                    else:
                        lines.append(line)
                cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if remove_tmp:
            _rmtree_best_effort(short_root)

    if condition == "description_only":
        pass  # empty skills_dir
    elif condition == "default_skill":
        for name, body in _load_default_skill_texts(raw_sbbench_path):
            d = skills_dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(body, encoding="utf-8")
            skill_bodies_for_audit.append(body)
    else:  # post_train
        if post_train_skills_dir is None or not post_train_skills_dir.is_dir():
            _rmtree_best_effort(short_root)
            return {
                "outcome": "hard_fail",
                "error": f"hermes post_train skills_dir missing: {post_train_skills_dir}",
                "elapsed_s": 0.0,
                "native_metrics": {
                    "missing_post_train_state": True,
                    "post_train_skills_dir": str(post_train_skills_dir),
                    "request_timeout_s": request_timeout_s,
                    "stale_timeout_s": stale_timeout_s,
                    "runner_timeout_s": timeout_s,
                },
            }
        for child in post_train_skills_dir.iterdir():
            if child.is_dir() and next(child.rglob("SKILL.md"), None) is not None:
                target = skills_dir / child.name
                if target.exists():
                    _rmtree_retry(target)
                shutil.copytree(child, target)
                skill_md = next(target.rglob("SKILL.md"), None)
                if skill_md is not None:
                    skill_bodies_for_audit.append(
                        skill_md.read_text(encoding="utf-8", errors="replace")
                    )

    from evolution.adapters.hermes_adapter import write_hermes_config

    write_hermes_config(
        hermes_home=home,
        solver_model=solver_cfg["id"],
        solver_base=solver_cfg["api_base"],
        solver_key=api_key,
        aux_model=aux_cfg["id"],
        aux_base=aux_cfg["api_base"],
        aux_key=aux_key,
        request_timeout_s=request_timeout_s,
        stale_timeout_s=stale_timeout_s,
    )

    runner = Path(__file__).parent / "_hermes_subprocess_runner.py"
    config_path = cache_dir / "_runner_config.json"
    runtime_config_path = short_root / "_runner_config.json"
    out_path = cache_dir / "_runner_result.json"
    telemetry_path = cache_dir / "_telemetry.jsonl"
    # Truncate any prior telemetry from a re-run of the same cell.
    if telemetry_path.exists():
        telemetry_path.unlink()
    _write_prompt_audit(
        cache_dir / "prompt_audit.json",
        prompt=instruction,
        skill_bodies=skill_bodies_for_audit,
        model_id=solver_cfg.get("served_id_hint") or solver_cfg["id"],
        seed=solver_cfg.get("seed"),
    )

    runner_cfg = {
        "workspace_dir": str(short_ws),
        "hermes_home": str(home),
        "solver_model": solver_cfg.get("served_id_hint") or solver_cfg["id"],
        "solver_base": solver_cfg["api_base"],
        "solver_key": api_key,
        "provider": "openrouter" if "openrouter.ai" in solver_cfg["api_base"] else "openai",
        "max_iterations": max_iterations,
        "max_tokens": int(
            solver_cfg.get("max_output_tokens")
            or solver_cfg.get("max_tokens")
            or os.environ.get("HERMES_MAX_OUTPUT_TOKENS", "32768")
        ),
        "instruction": _preview_text(
            instruction,
            int(os.environ.get("EVO_TASK_INSTRUCTION_PROMPT_CHARS", "60000") or "60000"),
            label="task instruction",
        ),
        "extra_body": solver_cfg.get("extra_body") or {},
        "guard_log_path": str(cache_dir / "workspace_guard.jsonl"),
        "request_timeout_s": request_timeout_s,
        "stale_timeout_s": stale_timeout_s,
        "runner_timeout_s": timeout_s,
    }
    runtime_config_path.write_text(
        json.dumps(runner_cfg, ensure_ascii=False),
        encoding="utf-8",
    )
    redacted_runner_cfg = dict(runner_cfg)
    redacted_runner_cfg["solver_key"] = "<redacted>"
    config_path.write_text(
        json.dumps(redacted_runner_cfg, ensure_ascii=False),
        encoding="utf-8",
    )

    started = time.time()
    proc_env = os.environ.copy()
    proc_env.setdefault("LITELLM_LOG", "ERROR")
    proc_env.setdefault("OPENAI_LOG", "warning")
    proc_env["HERMES_API_TIMEOUT"] = f"{request_timeout_s:g}"
    proc_env["HERMES_API_CALL_STALE_TIMEOUT"] = f"{stale_timeout_s:g}"
    proc_env.setdefault("HERMES_STREAM_READ_TIMEOUT", f"{request_timeout_s:g}")
    proc_env.setdefault("HERMES_STREAM_STALE_TIMEOUT", f"{stale_timeout_s:g}")
    try:
        proc = _run_process_group(
            [
                sys.executable,
                str(runner),
                "--config",
                str(runtime_config_path),
                "--out",
                str(out_path),
                "--telemetry-jsonl",
                str(telemetry_path.resolve()),
            ],
            timeout_s=timeout_s,
            capture_output=True,
            text=True,
            env=proc_env,
        )
    except subprocess.TimeoutExpired:
        per_turn, totals = _read_telemetry_jsonl(telemetry_path)
        _sync_short_paths(remove_tmp=True)
        return {
            "outcome": "hard_fail",
            "error": f"hermes runner timed out after {timeout_s}s",
            "elapsed_s": round(time.time() - started, 1),
            "llm_calls": totals["llm_calls"],
            "tokens_in_total": totals["tokens_in_total"],
            "tokens_out_total": totals["tokens_out_total"],
            "latency_ms_total": totals["latency_ms_total"],
            "per_turn": per_turn,
            "native_metrics": {
                "runner_timeout": True,
                "request_timeout_s": request_timeout_s,
                "stale_timeout_s": stale_timeout_s,
                "runner_timeout_s": timeout_s,
                "telemetry_parse_errors": totals["telemetry_parse_errors"],
            },
        }

    runner_out: dict = {}
    if out_path.is_file():
        try:
            runner_out = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            runner_out = {
                "outcome": "hard_fail",
                "error": f"runner result unparseable: {exc!r}",
            }
    if proc.returncode != 0 and not runner_out:
        runner_out = {
            "outcome": "hard_fail",
            "error": (
                f"runner rc={proc.returncode}; stderr_preview="
                f"{_preview_text(proc.stderr or '', 400, label='runner stderr')}"
            ),
        }

    transcript_parts = [
        f"[runner stdout]\n{proc.stdout or ''}",
        f"\n--- runner stderr ---\n{proc.stderr or ''}",
    ]
    if runner_out.get("final_response"):
        transcript_parts.append(f"\n[FINAL]\n{runner_out['final_response']}")
    transcript = "".join(transcript_parts)
    (cache_dir / "runner_stdout.txt").write_text(proc.stdout or "", encoding="utf-8")
    (cache_dir / "runner_stderr.txt").write_text(proc.stderr or "", encoding="utf-8")
    (cache_dir / "transcript.txt").write_text(transcript, encoding="utf-8")

    if runner_out.get("outcome") == "hard_fail":
        per_turn, totals = _read_telemetry_jsonl(telemetry_path)
        _sync_short_paths(remove_tmp=True)
        return {
            "outcome": "hard_fail",
            "error": runner_out.get("error", "unknown"),
            "traceback_tail": runner_out.get("traceback_tail", ""),
            "elapsed_s": round(time.time() - started, 1),
            "llm_calls": totals["llm_calls"],
            "tokens_in_total": totals["tokens_in_total"],
            "tokens_out_total": totals["tokens_out_total"],
            "latency_ms_total": totals["latency_ms_total"],
            "per_turn": per_turn,
            "native_metrics": {
                "request_timeout_s": request_timeout_s,
                "stale_timeout_s": stale_timeout_s,
                "runner_timeout_s": timeout_s,
                "telemetry_parse_errors": totals["telemetry_parse_errors"],
            },
        }

    per_turn, totals = _read_telemetry_jsonl(telemetry_path)
    if _all_llm_calls_failed(per_turn):
        _sync_short_paths(remove_tmp=True)
        return _llm_failure_result(
            framework="hermes",
            error="hermes eval had LLM calls but all failed; refusing to score stub workspace",
            elapsed_s=time.time() - started,
            per_turn=per_turn,
            totals=totals,
        )

    _sync_short_paths(remove_tmp=False)
    _ensure_solution_py(ws, runner_out.get("final_response", "") or "")
    task_python = _python_for_task(task_dir)
    _run_agent_solution(ws, cache_dir, timeout_s=timeout_s, python=task_python)
    collect = collect_outputs(ws, pre_snapshot)
    if collect.error:
        log.warning("hermes output collection failed: %s", collect.error)
    pytest_res = _run_pytest(task_dir, ws, python=task_python, timeout_s=timeout_s)
    _persist_native_debug_artifacts(cache_dir, ws, pytest_res)

    iterations_used = int(runner_out.get("iterations_used", 0) or 0)
    skills_dir_count = sum(1 for _ in skills_dir.iterdir()) if skills_dir.is_dir() else 0
    _sync_short_paths(remove_tmp=True)

    return {
        "outcome": "ok",
        "pass_rate": pytest_res["pass_rate"],
        "total_tests": pytest_res["total_tests"],
        "passed": pytest_res["passed"],
        "failed": pytest_res["failed"],
        "errors": pytest_res["errors"],
        "elapsed_s": round(time.time() - started, 1),
        # ``llm_calls`` from telemetry preferred over iterations_used —
        # one iteration can issue more than one LLM call.
        "llm_calls": totals["llm_calls"] or iterations_used,
        "tokens_in_total": totals["tokens_in_total"],
        "tokens_out_total": totals["tokens_out_total"],
        "latency_ms_total": totals["latency_ms_total"],
        "per_turn": per_turn,
        "pytest_log_tail": pytest_res["log_tail"],
        "cache_dir": str(cache_dir),
        "response_tail": _preview_text(
            str(runner_out.get("final_response") or ""),
            label="final response",
        ),
        "native_metrics": {
            "iterations_used": iterations_used,
            "skills_dir_count": skills_dir_count,
            "short_path_runtime": True,
            "request_timeout_s": request_timeout_s,
            "stale_timeout_s": stale_timeout_s,
            "runner_timeout_s": timeout_s,
            "telemetry_parse_errors": totals["telemetry_parse_errors"],
        },
    }


# ---------------------------------------------------------------------------
# Memento test runtime
# ---------------------------------------------------------------------------


def _memento_test_envelope(sandbox_listing: str, instruction: str) -> str:
    # Byte-frozen surface (t34/5p2 consumers): rendered bytes must not change.
    return (
        "Solve the Python task whose instruction follows. You are in a "
        "benchmark workspace sandbox. Read input files from your CWD using "
        "tools, and write required solution artifacts as real files under "
        "`output/` from the workspace root.\n\n"
        + WORKSPACE_LISTING_HEADER
        + "\n"
        + sandbox_listing
        + "\n\n"
        "Memento tool-path contract:\n"
        "- Tool path arguments are literal strings, not Python expressions.\n"
        "- To list the workspace root, call list_dir with path `.`; do not use "
        "an empty path.\n"
        "- Read task inputs from normal input paths such as "
        "`environment/data/`, `workflow.yml`, and `timing_log.json` when "
        "present.\n"
        "- Do not read `tests/` or `data_generator.py`; they are verifier/oracle "
        "implementation files and are intentionally outside the solve surface.\n\n"
        "File-output contract:\n"
        "- Do not answer with YAML, JSON, CSV, markdown, or code in chat "
        "instead of writing files.\n"
        "- " + FILE_OUTPUT_EXAMPLE_FLAT + "\n"
        "- " + FILE_OUTPUT_EXAMPLE_NESTED + "\n"
        "- If the task names `optimized_workflow.yml` or "
        "`optimization_report.json`, create exactly "
        "`output/optimized_workflow.yml` and "
        "`output/optimization_report.json`.\n"
        "- Use file-writing tools such as file_create/write_file or shell/Python "
        "commands to create the artifacts. After writing, read or list the "
        "`output/` paths to verify the files exist and are non-empty.\n"
        "- Only after the required files exist, give a short final answer that "
        "lists the created paths.\n\n"
        + NO_PACKAGE_INSTALL_RULE
        + "\n\n"
        + TASK_INSTRUCTION_BLOCK.format(
            task_instruction=_preview_text(
                instruction,
                int(os.environ.get("EVO_TASK_INSTRUCTION_PROMPT_CHARS", "60000") or "60000"),
                label="task instruction",
            )
        )
    )


def run_memento_test(
    *,
    task_dir: Path,
    raw_sbbench_path: Path,
    instruction: str,
    condition: str,
    cache_dir: Path,
    solver_cfg: dict,
    embedding_cfg: dict,
    api_key: str,
    emb_key: str,
    post_train_test_home: Path | None = None,
    timeout_s: float = 600.0,
) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    ws = cache_dir / "workspace"
    pre_snapshot = _setup_workspace(task_dir, ws)

    home = cache_dir / "_memento_home"
    if home.exists():
        _rmtree_retry(home)
    if condition == "post_train":
        if post_train_test_home is None or not post_train_test_home.is_dir():
            return {
                "outcome": "hard_fail",
                "error": f"memento post_train test_home missing: {post_train_test_home}",
                "elapsed_s": 0.0,
                "native_metrics": {
                    "missing_post_train_state": True,
                    "post_train_test_home": str(post_train_test_home),
                },
            }
        shutil.copytree(
            post_train_test_home,
            home,
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        from evolution.adapters.memento_adapter import redact_memento_configs

        redact_memento_configs(post_train_test_home)
    else:
        home.mkdir(parents=True, exist_ok=True)
    skills_dir = home / "memento_s" / "skills"
    config_dir = home / "memento_s"
    skills_dir.mkdir(parents=True, exist_ok=True)

    if condition == "description_only":
        pass
    elif condition == "default_skill":
        for name, body in _load_default_skill_texts(raw_sbbench_path):
            d = skills_dir / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(body, encoding="utf-8")
    else:  # post_train
        # Native lane: the whole trained home was cloned above so SQLite,
        # session memory, registry state, and skills are all available.
        pass

    from evolution.adapters.memento_adapter import (
        MEMENTO_REPO,
        _apply_native_services_profile,
        _install_memento_allowed_roots_override,
        _sync_test_home_registry,
        memento_native_services_profile,
        memento_phase_max_iterations,
        memento_phase_timeout_s,
        redact_memento_configs,
        write_llm_config,
    )

    if condition == "post_train":
        # skill.json stores absolute locations; re-anchor them to the copied home (fixes LOO).
        _sync_test_home_registry(home)

    timeout_s = memento_phase_timeout_s("eval", timeout_s, 360.0)
    eval_max_iterations = memento_phase_max_iterations("eval", 28)
    native_services_profile = memento_native_services_profile()

    # Memento's config_manager forcibly clobbers paths.workspace_dir with
    # PathManager.get_workspace_dir() (which is HOME/memento_s/workspace
    # in DEV mode), so an in-config override is ignored. We instead:
    #   - point HOME at <cache_dir>/_memento_home (already done above) →
    #     workspace becomes <home>/memento_s/workspace/
    #   - pin the session id via env-var so the wrapper monkey-patches
    #     SessionService._create to use it; the gateway then derives the
    #     sandbox at <home>/memento_s/workspace/<date>/md5(sid)[:8]
    #   - pre-create that exact path and copy the task inputs into it,
    #     so the agent's read_file/glob (which resolve under the sandbox)
    #     can see schema_doc.pdf, sales.csv, etc.
    #   - post-execution: recursively copy any files the agent wrote in
    #     the sandbox back to ws so pytest (which runs in ws) sees them.
    write_llm_config(
        solver_model=solver_cfg.get("litellm_id")
        or solver_cfg.get("id")
        or solver_cfg.get("served_id_hint", ""),
        solver_base=solver_cfg["api_base"],
        solver_key=api_key,
        embedding_model=embedding_cfg["id"],
        embedding_base=embedding_cfg["api_base"],
        embedding_key=emb_key,
        config_dir=config_dir,
        max_iterations=eval_max_iterations,
        served_model_id=solver_cfg.get("served_id_hint"),
    )

    import hashlib
    from datetime import datetime as _dt

    pinned_session_id = (
        f"pilot-{cache_dir.parent.parent.name}-{cache_dir.parent.name}-{cache_dir.name}"
        f"-{_dt.now().strftime('%H%M%S')}"
    )
    short_id = hashlib.md5(pinned_session_id.encode()).hexdigest()[:8]
    date_str = _dt.now().strftime("%Y-%m-%d")
    sandbox = home / "memento_s" / "workspace" / date_str / short_id
    sandbox.mkdir(parents=True, exist_ok=True)
    for item in ws.iterdir():
        target = sandbox / item.name
        if target.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    sandbox_listing = (
        "\n".join(
            sorted(
                str(p.relative_to(sandbox))
                for p in sandbox.rglob("*")
                if p.is_file()
                and not str(p.relative_to(sandbox)).startswith("tests/")
                and p.name != "data_generator.py"
            )[:200]
        )
        or "(empty)"
    )

    envelope = _memento_test_envelope(sandbox_listing, instruction)

    env = build_guarded_env(
        framework="memento",
        workspace_dir=sandbox,
        run_home=home,
        audit_log_path=cache_dir / "workspace_guard.jsonl",
        base_env=os.environ.copy(),
        set_home=True,
        command_wrappers=True,
        python_fs_guard_on_pythonpath=False,
    )
    env["MEMENTO_ENV"] = "dev"
    env["MEMENTO_PILOT_SESSION_ID"] = pinned_session_id
    env["MEMENTO_USE_CURRENT_PYTHON_SANDBOX"] = "1"
    env["MEMENTO_DISABLE_PYTHON_DEP_INSTALL"] = "1"
    env["MEMENTO_DISABLE_BUILTIN_SKILL_SYNC"] = "1"
    _apply_native_services_profile(env, native_services_profile)
    env["MEMENTO_SANDBOX_PYTHON"] = sys.executable
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("LITELLM_LOG", "ERROR")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    allowed_roots = [ws, sandbox, home, skills_dir]
    denied_roots = [
        ws / "tests",
        sandbox / "tests",
        ws / "data_generator.py",
        sandbox / "data_generator.py",
    ]
    env["MEMENTO_BENCHMARK_ALLOWED_ROOTS"] = os.pathsep.join(
        str(p.resolve(strict=False)) for p in allowed_roots
    )
    env["MEMENTO_BENCHMARK_DENIED_ROOTS"] = os.pathsep.join(
        str(p.resolve(strict=False)) for p in denied_roots
    )
    override_dir = _install_memento_allowed_roots_override(
        home,
        allowed_roots,
        denied_roots,
    )
    current_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_parts = [str(override_dir), str(MEMENTO_REPO)]
    if current_pythonpath:
        pythonpath_parts.append(current_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["MEMENTO_REPO"] = str(MEMENTO_REPO)

    # Per-trial telemetry JSONL — the wrapper installs litellm and openai
    # callbacks that append one line per LLM call. Truncate any prior
    # telemetry so cache-hit re-runs start fresh.
    telemetry_path = cache_dir / "_telemetry.jsonl"
    if telemetry_path.exists():
        telemetry_path.unlink()

    wrapper = Path(__file__).parent / "_memento_subprocess_wrapper.py"
    cleanup_needles = [
        str(cache_dir.resolve(strict=False)),
        str(home.resolve(strict=False)),
        str(sandbox.resolve(strict=False)),
        str(telemetry_path.resolve(strict=False)),
        pinned_session_id,
    ]

    def _copy_sandbox_to_workspace() -> int:
        # Copy any files the agent wrote in its sandbox back to ws so pytest
        # (which runs in ws) sees them. Memento rooted the agent at
        # sandbox = home/memento_s/workspace/<date>/<sid>/, so we walk that
        # tree recursively, skipping the inputs we pre-copied (still
        # bit-identical) and overwriting matching paths in ws. Returns the
        # number of agent-authored (not pre-copied) files.
        if not sandbox.is_dir():
            return 0
        copied_new = 0
        for sub in sandbox.rglob("*"):
            if not sub.is_file():
                continue
            rel = sub.relative_to(sandbox)
            dst = ws / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sub, dst)
            except OSError:
                continue
            if str(rel) not in pre_snapshot:
                copied_new += 1
        return copied_new

    def _score_workspace(
        *,
        rc: int | None,
        out: str,
        per_turn: list[dict],
        totals: dict,
        timeout_error: str | None = None,
    ) -> dict:
        redacted_configs = redact_memento_configs(home)
        agent_new_files = _copy_sandbox_to_workspace()
        _ensure_solution_py(ws, out or "")
        task_python = _python_for_task(task_dir)
        score_timeout_s = float(
            os.environ.get(
                "EVO_MEMENTO_SCORE_TIMEOUT_S",
                str(min(120.0, max(30.0, float(timeout_s) / 3.0))),
            )
            or "120"
        )
        pytest_timeout_s = float(
            os.environ.get(
                "EVO_MEMENTO_PYTEST_TIMEOUT_S",
                str(min(120.0, max(30.0, float(timeout_s) / 3.0))),
            )
            or "120"
        )
        _run_agent_solution(ws, cache_dir, timeout_s=score_timeout_s, python=task_python)
        collect = collect_outputs(ws, pre_snapshot)
        if collect.error:
            log.warning("memento output collection failed: %s", collect.error)
        pytest_res = _run_pytest(task_dir, ws, python=task_python, timeout_s=pytest_timeout_s)
        _persist_native_debug_artifacts(cache_dir, ws, pytest_res)

        native_metrics = {
            "memento_rc": rc,
            "skills_dir_count": sum(1 for _ in skills_dir.iterdir()) if skills_dir.is_dir() else 0,
            "telemetry_parse_errors": totals["telemetry_parse_errors"],
            "native_services_profile": native_services_profile,
            "memento_eval_max_iterations": eval_max_iterations,
            "memento_eval_timeout_s": timeout_s,
            "memento_score_timeout_s": score_timeout_s,
            "memento_pytest_timeout_s": pytest_timeout_s,
            "memento_skill_mentions": _extract_memento_skill_mentions(out or ""),
            "agent_new_files": agent_new_files,
            "consumer_model": solver_cfg.get("served_id_hint")
            or solver_cfg.get("id")
            or solver_cfg.get("litellm_id", ""),
            "redacted_configs": redacted_configs,
        }
        if timeout_error is not None:
            native_metrics["memento_timeout_salvaged"] = True
            native_metrics["memento_timeout_s"] = timeout_s
            native_metrics["memento_timeout_error"] = timeout_error

        result = {
            "outcome": "ok",
            "pass_rate": pytest_res["pass_rate"],
            "total_tests": pytest_res["total_tests"],
            "passed": pytest_res["passed"],
            "failed": pytest_res["failed"],
            "errors": pytest_res["errors"],
            "elapsed_s": round(time.time() - started, 1),
            "llm_calls": totals["llm_calls"],
            "tokens_in_total": totals["tokens_in_total"],
            "tokens_out_total": totals["tokens_out_total"],
            "latency_ms_total": totals["latency_ms_total"],
            "per_turn": per_turn,
            "pytest_log_tail": pytest_res["log_tail"],
            "cache_dir": str(cache_dir),
            "response_tail": _preview_text(out or "", label="memento stdout"),
            "output_collected": collect.copied,
            "native_metrics": native_metrics,
        }
        if timeout_error is not None:
            result["error"] = timeout_error
        elif rc == 0 and agent_new_files == 0 and float(pytest_res["pass_rate"] or 0.0) == 0.0:
            # Agent finished cleanly but wrote nothing pytest could score — flag, don't silent-0.
            result["outcome"] = "no_output"
            (cache_dir / "sandbox_listing.txt").write_text(
                "\n".join(sorted(str(p.relative_to(sandbox)) for p in sandbox.rglob("*")))
                if sandbox.is_dir()
                else "(no sandbox)",
                encoding="utf-8",
            )
        return result

    started = time.time()
    try:
        proc = _run_process_group(
            [
                sys.executable,
                str(wrapper),
                "--telemetry-jsonl",
                str(telemetry_path.resolve()),
                "agent",
                "--message",
                envelope,
                "--no-markdown",
            ],
            timeout_s=timeout_s,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ws),
        )
        rc = proc.returncode
        out = proc.stdout
        err = proc.stderr
    except subprocess.TimeoutExpired as exc:
        killed = _kill_processes_matching_needles(cleanup_needles)
        per_turn, totals = _read_telemetry_jsonl(telemetry_path)
        out = _subprocess_captured_text(exc.output)
        err = _subprocess_captured_text(exc.stderr)
        timeout_error = f"memento timed out after {timeout_s}s"
        transcript = (out or "") + "\n--- STDERR ---\n" + (err or "")
        (cache_dir / "memento_stdout.txt").write_text(out or "", encoding="utf-8")
        (cache_dir / "memento_stderr.txt").write_text(err or "", encoding="utf-8")
        (cache_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
        scored = _score_workspace(
            rc=None,
            out=out,
            per_turn=per_turn,
            totals=totals,
            timeout_error=timeout_error,
        )
        scored.setdefault("native_metrics", {})["memento_timeout_cleanup_pids"] = killed
        return scored

    killed_after_exit = _kill_processes_matching_needles(cleanup_needles, grace_s=0.2)

    transcript = (out or "") + "\n--- STDERR ---\n" + (err or "")
    (cache_dir / "memento_stdout.txt").write_text(out or "", encoding="utf-8")
    (cache_dir / "memento_stderr.txt").write_text(err or "", encoding="utf-8")
    (cache_dir / "transcript.txt").write_text(transcript, encoding="utf-8")

    per_turn, totals = _read_telemetry_jsonl(telemetry_path)
    redacted_configs = redact_memento_configs(home)
    if rc != 0:
        return {
            "outcome": "hard_fail",
            "error": (
                f"memento exited rc={rc}; stderr_preview="
                f"{_preview_text(err or '', 500, label='memento stderr')}"
            ),
            "elapsed_s": round(time.time() - started, 1),
            "llm_calls": totals["llm_calls"],
            "tokens_in_total": totals["tokens_in_total"],
            "tokens_out_total": totals["tokens_out_total"],
            "latency_ms_total": totals["latency_ms_total"],
            "per_turn": per_turn,
            "native_metrics": {
                "memento_cleanup_pids": killed_after_exit,
                "redacted_configs": redacted_configs,
            },
        }
    if _all_llm_calls_failed(per_turn) or "RUN_ERROR" in transcript:
        return _llm_failure_result(
            framework="memento",
            error="memento eval reported RUN_ERROR or all LLM calls failed; refusing to score stub workspace",
            elapsed_s=time.time() - started,
            per_turn=per_turn,
            totals=totals,
        )

    scored = _score_workspace(
        rc=rc,
        out=out or "",
        per_turn=per_turn,
        totals=totals,
    )
    scored.setdefault("native_metrics", {})["memento_cleanup_pids"] = killed_after_exit
    return scored


# ---------------------------------------------------------------------------
# MemP test runtime
# ---------------------------------------------------------------------------


def memp_retrieve_workflows(
    *,
    post_train_memory_dir: Path,
    instruction: str,
    cache_dir: Path,
    embedding_cfg: dict,
    reflector_cfg: dict,
    emb_key: str,
    refl_key: str,
    retrieve_num: int = 3,
) -> list[str]:
    """Load a produced MemP memory and return the top-k retrieved workflow texts
    for ``instruction`` — MemP's native artifact rendered as plain text so the
    shared neutral solver can score it on the same axis as the other frameworks."""
    if not post_train_memory_dir.is_dir():
        return []
    from evolution.adapters import memp_adapter

    memp_adapter.install_monkeypatches(
        embedding_model=embedding_cfg["id"],
        embedding_base=embedding_cfg["api_base"],
        embedding_key=emb_key,
        reflector_model=reflector_cfg["id"],
        reflector_base=reflector_cfg["api_base"],
        reflector_key=refl_key,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    mem_dir = cache_dir / "_memp_neutral_memory"
    if mem_dir.exists():
        _rmtree_retry(mem_dir)
    shutil.copytree(post_train_memory_dir, mem_dir)
    mem: Any = memp_adapter._load_memory_from_dir(mem_dir, retrieve_num=retrieve_num)
    if not mem.documents:
        return []
    retrieve_query = _preview_text(
        instruction,
        int(os.environ.get("EVO_MEMP_RETRIEVE_QUERY_CHARS", "4000") or "4000"),
        label="memp retrieval query",
    )
    results = mem.retrieve(retrieve_query) or []
    workflows: list[str] = []
    for r in results:
        doc = r[0] if isinstance(r, tuple) else r
        metadata = getattr(doc, "metadata", {}) or {}
        wf = metadata.get("workflow") or getattr(doc, "page_content", "") or ""
        if wf:
            workflows.append(str(wf))
    return workflows


def run_memp_test(
    *,
    task_dir: Path,
    raw_sbbench_path: Path,
    instruction: str,
    condition: str,
    cache_dir: Path,
    solver_cfg: dict,
    reflector_cfg: dict,
    embedding_cfg: dict,
    api_key: str,
    refl_key: str,
    emb_key: str,
    post_train_memory_dir: Path | None = None,
    max_steps: int = 12,
    timeout_s: int = 600,
) -> dict:
    from evolution.adapters import memp_adapter
    from evolution.adapters._memp_env import TaskEnv

    cache_dir.mkdir(parents=True, exist_ok=True)
    ws = cache_dir / "workspace"
    pre_snapshot = _setup_workspace(task_dir, ws)
    test_dir = task_dir / "tests"
    test_path = test_dir / "test_outputs.py"
    if not test_path.is_file():
        cands = list(test_dir.glob("test_*.py")) if test_dir.is_dir() else []
        if cands:
            test_path = cands[0]

    memp_adapter.install_monkeypatches(
        embedding_model=embedding_cfg["id"],
        embedding_base=embedding_cfg["api_base"],
        embedding_key=emb_key,
        reflector_model=reflector_cfg["id"],
        reflector_base=reflector_cfg["api_base"],
        reflector_key=refl_key,
    )

    # Load Memory according to condition.
    from ProcedureMem.memory import Memory  # type: ignore

    mem_dir = cache_dir / "_memp_memory"
    if mem_dir.exists():
        _rmtree_retry(mem_dir)
    mem_dir.mkdir(parents=True, exist_ok=True)

    workflows: list[str] = []
    n_memory_documents = 0
    if condition == "description_only":
        mem = Memory(
            is_cold_start=False,
            policy={"build": "direct", "retrieve": "facts", "update": "replace"},
            memory_dir=str(mem_dir),
            retrieve_num=3,
            memory_size=1000,
        )
    elif condition == "default_skill":
        mem = Memory(
            is_cold_start=False,
            policy={"build": "direct", "retrieve": "facts", "update": "replace"},
            memory_dir=str(mem_dir),
            retrieve_num=3,
            memory_size=1000,
        )
        # Seed Memory.documents with one Document per bundled SKILL.md.
        # Use langchain_core.documents (the new home); the old
        # langchain.schema re-export is broken in this env's pin.
        from langchain_core.documents import Document  # type: ignore

        for name, body in _load_default_skill_texts(raw_sbbench_path):
            doc = Document(
                page_content=body,
                metadata={"source": name, "workflow": body, "task_id": name},
            )
            mem.documents.append(doc)
        # Build vector_store via _save_documents — it's the only path that
        # constructs FAISS from self.documents (memory.py:83).
        try:
            mem._save_documents()
        except Exception as exc:
            log.warning("memp _save_documents (default_skill seed) failed: %s", exc)
        workflows = [body for _, body in _load_default_skill_texts(raw_sbbench_path)]
        n_memory_documents = len(mem.documents)
    else:  # post_train
        if post_train_memory_dir is None or not post_train_memory_dir.is_dir():
            return {
                "outcome": "hard_fail",
                "error": f"memp post_train memory_dir missing: {post_train_memory_dir}",
                "elapsed_s": 0.0,
                "native_metrics": {
                    "missing_post_train_state": True,
                    "post_train_memory_dir": str(post_train_memory_dir),
                },
            }
        _rmtree_retry(mem_dir)
        shutil.copytree(post_train_memory_dir, mem_dir)
        # Upstream Memory.__init__ does not reload persisted documents unless
        # it is in cold-start mode, which would rebuild from a trajectory file.
        # For post-train eval we need to load the saved memory as-is, then let
        # _save_documents() rebuild FAISS for native retrieve().
        try:
            mem: Any = memp_adapter._load_memory_from_dir(mem_dir, retrieve_num=3)
        except Exception as exc:
            log.warning("memp documents.json load failed: %s", exc)
            return {
                "outcome": "hard_fail",
                "error": f"memp-load-memory: {exc!r}"[:500],
                "elapsed_s": 0.0,
                "native_metrics": {"post_train_memory_dir": str(post_train_memory_dir)},
            }
        n_memory_documents = len(mem.documents)
        if mem.documents:
            try:
                retrieve_query = _preview_text(
                    instruction,
                    int(os.environ.get("EVO_MEMP_RETRIEVE_QUERY_CHARS", "4000") or "4000"),
                    label="memp retrieval query",
                )
                results = mem.retrieve(retrieve_query) or []
            except Exception as exc:
                log.warning("memp retrieve at test failed: %s", exc)
                return {
                    "outcome": "hard_fail",
                    "error": f"memp-retrieve: {exc!r}"[:500],
                    "elapsed_s": 0.0,
                    "native_metrics": {"n_memory_documents": len(mem.documents)},
                }
            for r in results:
                doc = r[0] if isinstance(r, tuple) else r
                metadata = getattr(doc, "metadata", {}) or {}
                wf = metadata.get("workflow") or getattr(doc, "page_content", "") or ""
                if wf:
                    workflows.append(str(wf))

    if os.environ.get("EVO_MEMP_NATIVE_EXECUTOR", "single_shot") != "episode":
        # MemP native lane: natively-retrieved workflows -> shared single-shot solver.
        res = run_evolution_test(
            task_dir=task_dir,
            raw_sbbench_path=raw_sbbench_path,
            instruction=instruction,
            condition="post_train",
            cache_dir=cache_dir,
            solver_cfg=solver_cfg,
            api_key=api_key,
            post_train_skill_bodies=workflows,
            timeout_s=timeout_s,
        )
        res = dict(res)
        res["native_metrics"] = {
            **(res.get("native_metrics") or {}),
            "memp_executor": "single_shot",
            "n_memory_documents": n_memory_documents,
            "n_workflows_injected": len(workflows),
        }
        return res

    env = TaskEnv(
        task_id=task_dir.name,
        workspace=ws,
        test_path=test_path,
        task_path=task_dir,
        python=_python_for_task(task_dir),
        guard_log_path=cache_dir / "workspace_guard.jsonl",
    )
    # OpenAI SDK sends `model` verbatim to the server; sglang exposes the
    # bare id (no ``openai/`` prefix). Strip the prefix here so MemP's
    # ``_solver_chat`` (which uses the openai SDK directly) hits the
    # correct served id.
    served_solver = solver_cfg.get("served_id_hint") or solver_cfg["id"]
    started = time.time()
    try:
        traj = memp_adapter.run_episode(
            env,
            task_instruction=instruction,
            retrieved_workflows=workflows,
            solver_base=solver_cfg["api_base"],
            solver_key=api_key,
            solver_model_id=served_solver,
            max_steps=max_steps,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        return {
            "outcome": "hard_fail",
            "error": f"memp-runtime: {exc!r}"[:500],
            "elapsed_s": round(time.time() - started, 1),
        }
    (cache_dir / "native_trajectory.json").write_text(
        json.dumps(traj, indent=2, default=str),
        encoding="utf-8",
    )
    (cache_dir / "transcript.txt").write_text(
        "\n".join(
            f"step {s.get('step')}: action={s.get('action', '')!r}\n"
            f"  obs={s.get('observation', '')!r}"
            for s in traj.get("steps", [])
        ),
        encoding="utf-8",
    )

    final_step = (traj.get("steps") or [{}])[-1] if traj.get("steps") else {}
    _ensure_solution_py(
        ws,
        (final_step.get("action") or "") + "\n" + (final_step.get("observation") or ""),
    )
    task_python = _python_for_task(task_dir)
    _run_agent_solution(ws, cache_dir, timeout_s=timeout_s, python=task_python)
    collect = collect_outputs(ws, pre_snapshot)
    if collect.error:
        log.warning("memp output collection failed: %s", collect.error)
    pytest_res = _run_pytest(task_dir, ws, python=task_python, timeout_s=240)
    _persist_native_debug_artifacts(cache_dir, ws, pytest_res)

    per_turn = traj.get("per_turn_usage", [])
    total_in = sum(u.get("tokens_in", 0) for u in per_turn)
    total_out = sum(u.get("tokens_out", 0) for u in per_turn)
    total_lat = sum(u.get("latency_ms", 0.0) for u in per_turn)

    solver_error = traj.get("solver_error")
    return {
        "outcome": "ok" if not solver_error else "extraction_failed",
        "error": (
            "memp-solver: " + _preview_text(str(solver_error), 500, label="memp solver error")
            if solver_error
            else ""
        ),
        "pass_rate": pytest_res["pass_rate"],
        "total_tests": pytest_res["total_tests"],
        "passed": pytest_res["passed"],
        "failed": pytest_res["failed"],
        "errors": pytest_res["errors"],
        "elapsed_s": round(time.time() - started, 1),
        "llm_calls": len(per_turn),
        "tokens_in_total": total_in,
        "tokens_out_total": total_out,
        "latency_ms_total": round(total_lat, 1),
        "per_turn": per_turn,
        "pytest_log_tail": pytest_res["log_tail"],
        "cache_dir": str(cache_dir),
        "response_tail": _preview_text(str(final_step.get("action") or ""), label="memp action"),
        "native_metrics": {
            "n_steps": len(traj.get("steps", [])),
            "final_reward": traj.get("final_reward", 0.0),
            "n_memory_documents": n_memory_documents,
            "n_workflows_injected": len(workflows),
            "solver_error": solver_error or "",
        },
    }


# ---------------------------------------------------------------------------
# EvoSkill native test runtime (Lane N)
# ---------------------------------------------------------------------------

_EVO_REPO_LOCKS: dict[str, threading.Lock] = {}
_EVO_REPO_LOCKS_GUARD = threading.Lock()


def _evoskill_repo_lock(repo: Path) -> threading.Lock:
    key = str(repo.resolve())
    with _EVO_REPO_LOCKS_GUARD:
        if key not in _EVO_REPO_LOCKS:
            _EVO_REPO_LOCKS[key] = threading.Lock()
        return _EVO_REPO_LOCKS[key]


def _list_claude_skills(skills_root: Path) -> list[str]:
    if not skills_root.is_dir():
        return []
    return sorted(
        d.name for d in skills_root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()
    )


def _git_porcelain(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "").strip()


def run_evoskill_skill_dir_test(
    *,
    task_dir: Path,
    instruction: str,
    cache_dir: Path,
    skills_dir: Path,
    solver_cfg: dict,
    api_key: str,
    timeout_s: int = 900,
    native_metrics_extra: dict | None = None,
) -> dict:
    """Run a task through EvoSkill's native openai_compat agent using a skill dir.

    This is the EvoSkill-native scoring primitive: skills live under
    ``.claude/skills`` in the agent workspace, the EvoSkill agent sees them via
    its native skill injection, writes task artifacts with tools, and pytest
    scores the resulting workspace. It intentionally does not call
    ``run_evolution_test`` or inject skill bodies into Evolution's neutral
    solver.
    """
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = Path(skills_dir).resolve()
    started = time.time()

    ws = cache_dir / "workspace"
    if ws.exists():
        _rmtree_retry(ws)
    _setup_workspace(task_dir, ws)
    dst_skills = ws / ".claude" / "skills"
    dst_skills.mkdir(parents=True, exist_ok=True)

    skill_names: list[str] = _list_claude_skills(skills_dir)
    if skills_dir.is_dir():
        for live in sorted(skills_dir.iterdir()):
            if live.is_dir():
                dst = dst_skills / live.name
                if dst.exists():
                    _rmtree_retry(dst)
                shutil.copytree(live, dst, symlinks=False)

    pre_snapshot = snapshot_workspace(ws)

    served = solver_cfg.get("served_id_hint") or solver_cfg["id"]
    env_backup = {
        k: os.environ.get(k)
        for k in (
            "EVO_OPENAI_COMPAT_BASE_URL",
            "EVO_OPENAI_COMPAT_API_KEY",
            "EVO_OPENAI_COMPAT_MODEL",
            "EVO_OPENAI_COMPAT_TEMPERATURE",
            "EVO_OPENAI_COMPAT_MAX_TOKENS",
            "EVO_OPENAI_COMPAT_REASONING_EFFORT",
        )
    }
    os.environ["EVO_OPENAI_COMPAT_BASE_URL"] = solver_cfg["api_base"]
    os.environ["EVO_OPENAI_COMPAT_API_KEY"] = api_key or "EMPTY"
    os.environ["EVO_OPENAI_COMPAT_MODEL"] = served
    os.environ["EVO_OPENAI_COMPAT_TEMPERATURE"] = str(solver_cfg.get("temperature", 0.7))
    os.environ["EVO_OPENAI_COMPAT_MAX_TOKENS"] = str(solver_cfg.get("max_output_tokens") or 8192)
    extra = solver_cfg.get("extra_body") or {}
    if isinstance(extra, dict):
        reasoning = extra.get("reasoning") or {}
        if isinstance(reasoning, dict) and reasoning.get("enabled"):
            os.environ.setdefault("EVO_OPENAI_COMPAT_REASONING_EFFORT", "medium")

    query = (
        instruction.strip() + "\n\nProduce all required output artifacts under output/. "
        "When finished, respond with JSON containing final_answer (a single Python script "
        "as a string) and reasoning."
    )

    final_answer = ""
    parse_error = ""
    parse_source = ""
    tokens_in = 0
    tokens_out = 0
    llm_calls = 0

    try:
        evoskill_root = REPO_ROOT.parent / "EvoSkill"
        if str(evoskill_root) not in sys.path:
            sys.path.insert(0, str(evoskill_root))
        from src.agent_profiles.base_agent.base_agent import make_base_agent_options
        from src.harness import Agent, set_sdk
        from src.schemas import AgentResponse

        set_sdk("openai_compat")
        agent_timeout_s = max(
            30,
            min(
                int(timeout_s),
                int(os.environ.get("EVO_EVOSKILL_NATIVE_AGENT_TIMEOUT_S", "300")),
            ),
        )
        agent_retries = max(
            1,
            int(os.environ.get("EVO_EVOSKILL_NATIVE_AGENT_RETRIES", "1")),
        )
        agent = Agent(
            make_base_agent_options(model=served, data_dirs=[], project_root=ws),
            AgentResponse,
            timeout_seconds=agent_timeout_s,
            max_retries=agent_retries,
        )
        trace = asyncio.run(agent.run(query))
        llm_calls = int(getattr(trace, "num_turns", 0) or 0)
        usage = getattr(trace, "usage", {}) or {}
        if isinstance(usage, dict):
            tokens_in = int(usage.get("prompt_tokens") or usage.get("tokens_in") or 0)
            tokens_out = int(usage.get("completion_tokens") or usage.get("tokens_out") or 0)
        final_answer, parse_source, parse_error = _coerce_evoskill_final_answer(trace)
        (cache_dir / "final_answer.txt").write_text(final_answer, encoding="utf-8")
        (cache_dir / "agent_trace.json").write_text(
            json.dumps(
                {
                    "result": getattr(trace, "result", ""),
                    "is_error": getattr(trace, "is_error", False),
                    "duration_ms": getattr(trace, "duration_ms", 0),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        parse_error = f"evoskill-agent: {exc!r}"[:300]
        (cache_dir / "eval_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    code = _extract_python_code(final_answer)
    extracted = bool(code)
    post_snapshot = snapshot_workspace(ws)
    agent_wrote_files = any(
        post_snapshot.get(rel) != pre_snapshot.get(rel) for rel in post_snapshot
    ) or any(rel not in pre_snapshot for rel in post_snapshot)

    if extracted:
        (ws / "solution.py").write_text(code, encoding="utf-8")
        (ws / "_solve.py").write_text(code, encoding="utf-8")
        (cache_dir / "solve.py").write_text(code, encoding="utf-8")
        task_python = _python_for_task(task_dir)
        _run_agent_solution(ws, cache_dir, timeout_s=float(timeout_s), python=task_python)
    else:
        task_python = _python_for_task(task_dir)

    solve_sh = ws / "solve.sh"
    if solve_sh.is_file():
        solve_timeout_s = max(60.0, float(timeout_s) - 30.0)
        try:
            sproc = _run_process_group(
                ["bash", str(solve_sh)],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout_s=solve_timeout_s,
            )
            solve_log = (sproc.stdout or "") + "\n--- stderr ---\n" + (sproc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            solve_log = f"solve.sh TIMEOUT after {exc.timeout}s"
        except Exception as exc:  # noqa: BLE001
            solve_log = f"solve.sh exec error: {exc!r}"
        (cache_dir / "solve_run.log").write_text(solve_log, encoding="utf-8")
        collect = collect_outputs(ws, pre_snapshot)
        if collect.error:
            log.warning("evoskill output collection failed: %s", collect.error)

    pytest_res = _run_pytest(task_dir, ws, python=task_python, timeout_s=240)
    _persist_native_debug_artifacts(cache_dir, ws, pytest_res)

    metrics = {
        "skill_names": skill_names,
        "skills_dir": str(skills_dir),
        "extracted": extracted,
        "agent_wrote_files": agent_wrote_files,
        "parse_error": parse_error,
        "parse_source": parse_source,
        "scorer": "evoskill_native_openai_compat",
    }
    if native_metrics_extra:
        metrics.update(native_metrics_extra)

    base = {
        "elapsed_s": round(time.time() - started, 1),
        "pass_rate": pytest_res["pass_rate"],
        "total_tests": pytest_res["total_tests"],
        "passed": pytest_res["passed"],
        "failed": pytest_res["failed"],
        "errors": pytest_res["errors"],
        "llm_calls": llm_calls,
        "tokens_in_total": tokens_in,
        "tokens_out_total": tokens_out,
        "pytest_log_tail": pytest_res["log_tail"],
        "cache_dir": str(cache_dir),
        "response_tail": _preview_text(final_answer, label="evoskill final answer"),
        "native_metrics": metrics,
    }
    if not extracted and not agent_wrote_files:
        return {
            **base,
            "outcome": "extraction_failed",
            "error": parse_error or "no extracted code and no agent file writes",
        }
    return {
        **base,
        "outcome": "ok",
    }


def run_evoskill_test(
    *,
    task_dir: Path,
    instruction: str,
    condition: str,
    cache_dir: Path,
    repo_dir: Path,
    solver_cfg: dict,
    api_key: str,
    timeout_s: int = 900,
) -> dict:
    """Run a held-out task through EvoSkill's openai_compat agent in a scored workspace.

    ``condition`` is ``seed_program`` (``program/base``) or ``best_program`` (frontier best).
    Skills are copied from the checked-out repo branch into ``ws/.claude/skills/``; the agent
    never runs inside the scaffold git repo.
    """
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo = repo_dir.resolve()
    if not repo.is_dir():
        return {
            "outcome": "hard_fail",
            "error": f"evoskill repo missing: {repo}",
            "elapsed_s": 0.0,
        }

    lock = _evoskill_repo_lock(repo)
    started = time.time()
    branch_name = ""
    head_sha = ""
    skill_names: list[str] = []

    ws = cache_dir / "workspace"
    if ws.exists():
        _rmtree_retry(ws)
    _setup_workspace(task_dir, ws)
    dst_skills = ws / ".claude" / "skills"
    dst_skills.mkdir(parents=True, exist_ok=True)

    with lock:
        dirty = _git_porcelain(repo)
        if dirty:
            return {
                "outcome": "hard_fail",
                "error": "dirty_repo",
                "elapsed_s": round(time.time() - started, 1),
                "native_metrics": {"dirty_repo": True, "porcelain": dirty[:500]},
            }

        evoskill_root = REPO_ROOT.parent / "EvoSkill"
        if str(evoskill_root) not in sys.path:
            sys.path.insert(0, str(evoskill_root))

        from src.registry import ProgramManager

        manager = ProgramManager(cwd=repo)
        if condition == "seed_program":
            program = "base"
        elif condition == "best_program":
            best = manager.get_best_from_frontier()
            if not best:
                return {
                    "outcome": "hard_fail",
                    "error": "empty_frontier",
                    "elapsed_s": round(time.time() - started, 1),
                }
            program = best
        else:
            return {
                "outcome": "hard_fail",
                "error": f"unknown evoskill condition: {condition}",
                "elapsed_s": round(time.time() - started, 1),
            }

        try:
            manager.switch_to(program)
        except Exception as exc:
            return {
                "outcome": "hard_fail",
                "error": f"git-checkout: {exc!r}"[:300],
                "elapsed_s": round(time.time() - started, 1),
            }

        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        head_sha = (head_proc.stdout or "").strip()
        branch_name = f"program/{program}"

        src_skills = repo / ".claude" / "skills"
        skill_names = _list_claude_skills(src_skills)
        if src_skills.is_dir():
            for live in sorted(src_skills.iterdir()):
                if live.is_dir():
                    shutil.copytree(live, dst_skills / live.name, symlinks=False)

    pre_snapshot = snapshot_workspace(ws)

    served = solver_cfg.get("served_id_hint") or solver_cfg["id"]
    env_backup = {
        k: os.environ.get(k)
        for k in (
            "EVO_OPENAI_COMPAT_BASE_URL",
            "EVO_OPENAI_COMPAT_API_KEY",
            "EVO_OPENAI_COMPAT_MODEL",
            "EVO_OPENAI_COMPAT_TEMPERATURE",
            "EVO_OPENAI_COMPAT_MAX_TOKENS",
            "EVO_OPENAI_COMPAT_REASONING_EFFORT",
        )
    }
    os.environ["EVO_OPENAI_COMPAT_BASE_URL"] = solver_cfg["api_base"]
    os.environ["EVO_OPENAI_COMPAT_API_KEY"] = api_key or "EMPTY"
    os.environ["EVO_OPENAI_COMPAT_MODEL"] = served
    os.environ["EVO_OPENAI_COMPAT_TEMPERATURE"] = str(solver_cfg.get("temperature", 0.7))
    os.environ["EVO_OPENAI_COMPAT_MAX_TOKENS"] = str(solver_cfg.get("max_output_tokens") or 8192)
    extra = solver_cfg.get("extra_body") or {}
    if isinstance(extra, dict):
        reasoning = extra.get("reasoning") or {}
        if isinstance(reasoning, dict) and reasoning.get("enabled"):
            os.environ.setdefault("EVO_OPENAI_COMPAT_REASONING_EFFORT", "medium")

    query = (
        instruction.strip() + "\n\nProduce all required output artifacts under output/. "
        "When finished, respond with JSON containing final_answer (a single Python script "
        "as a string) and reasoning."
    )

    final_answer = ""
    parse_error = ""
    parse_source = ""
    tokens_in = 0
    tokens_out = 0
    llm_calls = 0

    try:
        from src.agent_profiles.base_agent.base_agent import make_base_agent_options
        from src.harness import Agent, set_sdk
        from src.schemas import AgentResponse

        set_sdk("openai_compat")
        agent_timeout_s = max(
            30,
            min(
                int(timeout_s),
                int(os.environ.get("EVO_EVOSKILL_NATIVE_AGENT_TIMEOUT_S", "300")),
            ),
        )
        agent_retries = max(
            1,
            int(os.environ.get("EVO_EVOSKILL_NATIVE_AGENT_RETRIES", "1")),
        )
        agent = Agent(
            make_base_agent_options(model=served, data_dirs=[], project_root=ws),
            AgentResponse,
            timeout_seconds=agent_timeout_s,
            max_retries=agent_retries,
        )
        trace = asyncio.run(agent.run(query))
        llm_calls = int(getattr(trace, "num_turns", 0) or 0)
        usage = getattr(trace, "usage", {}) or {}
        if isinstance(usage, dict):
            tokens_in = int(usage.get("prompt_tokens") or usage.get("tokens_in") or 0)
            tokens_out = int(usage.get("completion_tokens") or usage.get("tokens_out") or 0)
        final_answer, parse_source, parse_error = _coerce_evoskill_final_answer(trace)
        (cache_dir / "final_answer.txt").write_text(final_answer, encoding="utf-8")
        (cache_dir / "agent_trace.json").write_text(
            json.dumps(
                {
                    "result": getattr(trace, "result", ""),
                    "is_error": getattr(trace, "is_error", False),
                    "duration_ms": getattr(trace, "duration_ms", 0),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        parse_error = f"evoskill-agent: {exc!r}"[:300]
        (cache_dir / "eval_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    code = _extract_python_code(final_answer)
    extracted = bool(code)
    post_snapshot = snapshot_workspace(ws)
    agent_wrote_files = any(
        post_snapshot.get(rel) != pre_snapshot.get(rel) for rel in post_snapshot
    ) or any(rel not in pre_snapshot for rel in post_snapshot)

    if extracted:
        (ws / "solution.py").write_text(code, encoding="utf-8")
        (ws / "_solve.py").write_text(code, encoding="utf-8")
        (cache_dir / "solve.py").write_text(code, encoding="utf-8")
        task_python = _python_for_task(task_dir)
        _run_agent_solution(ws, cache_dir, timeout_s=float(timeout_s), python=task_python)
    else:
        task_python = _python_for_task(task_dir)

    solve_sh = ws / "solve.sh"
    if solve_sh.is_file():
        solve_timeout_s = max(60.0, float(timeout_s) - 30.0)
        try:
            sproc = _run_process_group(
                ["bash", str(solve_sh)],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout_s=solve_timeout_s,
            )
            solve_log = (sproc.stdout or "") + "\n--- stderr ---\n" + (sproc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            solve_log = f"solve.sh TIMEOUT after {exc.timeout}s"
        except Exception as exc:  # noqa: BLE001
            solve_log = f"solve.sh exec error: {exc!r}"
        (cache_dir / "solve_run.log").write_text(solve_log, encoding="utf-8")
        collect = collect_outputs(ws, pre_snapshot)
        if collect.error:
            log.warning("evoskill output collection failed: %s", collect.error)

    pytest_res = _run_pytest(task_dir, ws, python=task_python, timeout_s=240)
    _persist_native_debug_artifacts(cache_dir, ws, pytest_res)

    if not extracted and not agent_wrote_files:
        return {
            "outcome": "extraction_failed",
            "error": parse_error or "no extracted code and no agent file writes",
            "elapsed_s": round(time.time() - started, 1),
            "pass_rate": pytest_res["pass_rate"],
            "total_tests": pytest_res["total_tests"],
            "passed": pytest_res["passed"],
            "failed": pytest_res["failed"],
            "errors": pytest_res["errors"],
            "llm_calls": llm_calls,
            "tokens_in_total": tokens_in,
            "tokens_out_total": tokens_out,
            "pytest_log_tail": pytest_res["log_tail"],
            "cache_dir": str(cache_dir),
            "response_tail": _preview_text(final_answer, label="evoskill final answer"),
            "native_metrics": {
                "branch": branch_name,
                "head_sha": head_sha,
                "skill_names": skill_names,
                "extracted": False,
                "agent_wrote_files": agent_wrote_files,
                "parse_source": parse_source,
            },
        }

    return {
        "outcome": "ok",
        "pass_rate": pytest_res["pass_rate"],
        "total_tests": pytest_res["total_tests"],
        "passed": pytest_res["passed"],
        "failed": pytest_res["failed"],
        "errors": pytest_res["errors"],
        "elapsed_s": round(time.time() - started, 1),
        "llm_calls": llm_calls,
        "tokens_in_total": tokens_in,
        "tokens_out_total": tokens_out,
        "pytest_log_tail": pytest_res["log_tail"],
        "cache_dir": str(cache_dir),
        "response_tail": _preview_text(final_answer, label="evoskill final answer"),
        "native_metrics": {
            "branch": branch_name,
            "head_sha": head_sha,
            "skill_names": skill_names,
            "extracted": extracted,
            "agent_wrote_files": agent_wrote_files,
            "parse_error": parse_error,
            "parse_source": parse_source,
        },
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _process_cell(
    args, runtime: dict, plans: dict, fw: str, tid: str, condition: str, trial: int
) -> dict:
    # Resolve all paths to absolute up front so concurrent worker threads
    # that os.chdir() (Hermes test runtime in particular) don't accidentally
    # resolve relative cache_dir against another worker's cwd. Without this,
    # cell directories get nested under each other when threads interleave.
    cache_root = Path(args.cache_dir).resolve()
    suffix = getattr(args, "cache_suffix", "") or ""
    if suffix:
        cache_dir = cache_root / fw / tid / f"{condition}{suffix}" / f"trial_{trial}"
    else:
        cache_dir = cache_root / fw / tid / condition / f"trial_{trial}"
    result_path = cache_dir / "result.json"
    if result_path.is_file():
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except (json.JSONDecodeError, OSError):
            pass
    cache_dir.mkdir(parents=True, exist_ok=True)

    pilot_cfg = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    solver_cfg = pilot_cfg["models"]["solver"]
    aux_cfg = pilot_cfg["models"]["reflector"]
    emb_cfg = pilot_cfg["models"]["embedding"]
    api_key = os.environ.get(solver_cfg["api_key_env"], "")
    aux_key = os.environ.get(aux_cfg["api_key_env"], "")
    emb_key = os.environ.get(emb_cfg["api_key_env"], "")

    meta = runtime["tasks"][tid]
    raw_path = Path(meta["raw_sbbench_path"]).resolve()
    task_dir = (Path(args.evolution_tasks_dir) / tid).resolve()
    instruction = (
        (task_dir / "instruction.md").read_text(encoding="utf-8")
        if (task_dir / "instruction.md").is_file()
        else ""
    )

    iso = pilot_cfg.get("isolation", {})
    runtime_budget = pilot_cfg.get("runtime_budget", {})

    # Compute split_class + eval_skill_source for this row.
    own_cell = meta.get("cell_id", "")
    own_split_class = meta.get("split_class", "")
    eval_skill_source = own_cell
    split_class = own_split_class
    if getattr(args, "cross_role_eval", False):
        anchor_pair = list(runtime.get("anchor_pair") or [])
        if own_cell in anchor_pair:
            other = next((c for c in anchor_pair if c != own_cell), None)
            if other:
                eval_skill_source = other
                split_class = "cross-role"

    out: dict = {
        "framework": fw,
        "task_id": tid,
        "condition": condition,
        "trial": trial,
        "cell_id": own_cell,
        "role": meta.get("role", ""),
        "stream": meta.get("stream", ""),
        "split_class": split_class,
        "eval_skill_source": eval_skill_source,
        "served_id": pilot_cfg["models"]["solver"].get("served_id_hint", solver_cfg.get("id", "")),
        "ts": time.time(),
    }
    try:
        if fw == "evolution":
            post_train_bodies: list[str] = []
            if condition == "post_train":
                # Use SkillStore to read evolved bodies for this cell.
                fw_plan = plans.get("evolution", {})
                names = fw_plan.get("per_task_skills", {}).get(tid)
                if names is None:
                    # Fall back to baseline skills declared on the task.
                    names = list(meta.get("skills", []) or [])
                from evolution.adapters.common import REPO_ROOT as ADAPTER_REPO_ROOT
                from evolution.core.store import SkillStore

                store = SkillStore(project_dir=ADAPTER_REPO_ROOT)
                for n in names:
                    try:
                        s = store.get(n)
                        post_train_bodies.append(s.body or "")
                    except Exception:
                        continue
            res = run_evolution_test(
                task_dir=task_dir,
                raw_sbbench_path=raw_path,
                instruction=instruction,
                condition=condition,
                cache_dir=cache_dir,
                solver_cfg=solver_cfg,
                api_key=api_key,
                post_train_skill_bodies=post_train_bodies,
                timeout_s=runtime_budget.get("evolution_solve_timeout_s", 300),
            )
        elif fw == "hermes":
            post_train_skills_dir: Path | None = None
            if condition == "post_train":
                home = (
                    Path(os.path.expanduser(iso["hermes_home"])).resolve()
                    if iso.get("hermes_home")
                    else None
                )
                if home is not None:
                    post_train_skills_dir = home / "skills"
            res = run_hermes_test(
                task_dir=task_dir,
                raw_sbbench_path=raw_path,
                instruction=instruction,
                condition=condition,
                cache_dir=cache_dir,
                solver_cfg=solver_cfg,
                aux_cfg=aux_cfg,
                api_key=api_key,
                aux_key=aux_key,
                post_train_skills_dir=post_train_skills_dir,
                max_iterations=runtime_budget.get("hermes_max_iterations", 90),
                timeout_s=runtime_budget.get("evolution_solve_timeout_s", 300),
            )
        elif fw == "memento":
            post_train_test_home: Path | None = None
            if condition == "post_train":
                runs_root = (
                    Path(os.path.expanduser(iso["memento_runs_root"])).resolve()
                    if iso.get("memento_runs_root")
                    else None
                )
                test_home_name = iso.get("memento_test_home_dirname", "test_home")
                if runs_root is not None:
                    post_train_test_home = runs_root / test_home_name
            res = run_memento_test(
                task_dir=task_dir,
                raw_sbbench_path=raw_path,
                instruction=instruction,
                condition=condition,
                cache_dir=cache_dir,
                solver_cfg=solver_cfg,
                embedding_cfg=emb_cfg,
                api_key=api_key,
                emb_key=emb_key,
                post_train_test_home=post_train_test_home,
                timeout_s=runtime_budget.get("memento_per_task_timeout_s", 900),
            )
        elif fw == "memp":
            post_train_memory_dir: Path | None = None
            if condition == "post_train":
                home = (
                    Path(os.path.expanduser(iso["memp_home"])).resolve()
                    if iso.get("memp_home")
                    else None
                )
                if home is not None:
                    post_train_memory_dir = home / "memory"
            res = run_memp_test(
                task_dir=task_dir,
                raw_sbbench_path=raw_path,
                instruction=instruction,
                condition=condition,
                cache_dir=cache_dir,
                solver_cfg=solver_cfg,
                reflector_cfg=aux_cfg,
                embedding_cfg=emb_cfg,
                api_key=api_key,
                refl_key=aux_key,
                emb_key=emb_key,
                post_train_memory_dir=post_train_memory_dir,
                max_steps=runtime_budget.get("memp_max_steps", 12),
                timeout_s=runtime_budget.get("memento_per_task_timeout_s", 600),
            )
        else:
            res = {"outcome": "hard_fail", "error": f"unknown framework {fw!r}"}
    except Exception as exc:
        res = {
            "outcome": "hard_fail",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback_tail": traceback.format_exc()[-600:],
        }
    out.update(res)
    result_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=Path(__file__).parent / "pilot_manifest.json")
    ap.add_argument(
        "--runtime",
        type=Path,
        default=Path(__file__).parent / "artifacts" / "pilot_manifest_runtime.json",
    )
    ap.add_argument(
        "--evolution-tasks-dir",
        type=Path,
        default=REPO_ROOT.parent / "evolution_input_sbbench_2026-04-30" / "tasks_evo",
    )
    ap.add_argument(
        "--produce-dir",
        type=Path,
        default=Path(__file__).parent / "artifacts" / "produce_plans",
    )
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).parent / "artifacts" / "solver_cache",
    )
    ap.add_argument("--frameworks", default="evolution,hermes,memento,memp")
    ap.add_argument("--conditions", default="description_only,default_skill,post_train")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--only-task", default="")
    ap.add_argument(
        "--streams",
        default="test",
        help=(
            "Comma-separated streams to evaluate. Default 'test' is the "
            "Gen_T held-task pass. Use 'train_a,train_b' for the Spec "
            "(local) metric. Use 'test' alone with --cross-role-eval for "
            "the Gen_R pass."
        ),
    )
    ap.add_argument(
        "--cross-role-eval",
        action="store_true",
        help=(
            "Anchor-pair cross-role evaluation: tasks in the anchor pair's "
            "test stream are solved with skills harvested from the OTHER "
            "anchor cell. Forces post_train condition. Tags rows "
            "split_class=cross-role and eval_skill_source=<other_cell>."
        ),
    )
    ap.add_argument(
        "--freeze-skills-during-eval",
        action="store_true",
        default=True,
        help=(
            "(Default ON.) Eval phases never call back into "
            "skill_manage / process_trajectory_item / memento update. "
            "Adapters honor this by not re-reflecting on eval outcomes."
        ),
    )
    ap.add_argument(
        "--cache-suffix",
        default="",
        help="Optional cache subdirectory suffix to keep streams isolated.",
    )
    args = ap.parse_args()

    _load_env_silent(REPO_ROOT / ".env")
    runtime = json.loads(args.runtime.read_text(encoding="utf-8"))

    plans: dict[str, dict] = {}
    for fw in args.frameworks.split(","):
        fw = fw.strip()
        p = args.produce_dir / f"{fw}_produce.json"
        if p.is_file():
            try:
                plans[fw] = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to load %s: %s", p, exc)
                plans[fw] = {}
        else:
            plans[fw] = {}

    streams_to_eval = [s.strip() for s in args.streams.split(",") if s.strip()]
    only_task_set = (
        {x.strip() for x in args.only_task.split(",") if x.strip()} if args.only_task else None
    )

    # Build cross-role mapping (task → cell_id whose skills to inject) when
    # --cross-role-eval is on. Only applies to anchor-pair cells.
    anchor_pair = list(runtime.get("anchor_pair") or [])
    cross_role_skill_source: dict[str, str] = {}
    if args.cross_role_eval:
        if len(anchor_pair) != 2:
            log.error(
                "--cross-role-eval requires runtime.anchor_pair=[cellA,cellB]; got %r",
                anchor_pair,
            )
            return 1
        cell_lookup = {c["cell_id"]: c for c in runtime["cells"]}
        for cid in anchor_pair:
            other = next(c for c in anchor_pair if c != cid)
            for tid in cell_lookup[cid]["streams"].get("test", []):
                cross_role_skill_source[tid] = other

    eval_ids: list[str] = []
    for cell in runtime["cells"]:
        for stream in streams_to_eval:
            for tid in cell["streams"].get(stream, []):
                if only_task_set is not None and tid not in only_task_set:
                    continue
                if args.cross_role_eval and tid not in cross_role_skill_source:
                    # Cross-role pass only runs on anchor-pair test tasks.
                    continue
                eval_ids.append(tid)
    # Dedupe while preserving order
    seen = set()
    eval_ids = [t for t in eval_ids if not (t in seen or seen.add(t))]

    cells: list[tuple[str, str, str, int]] = []
    for fw in args.frameworks.split(","):
        for condition in args.conditions.split(","):
            for tid in eval_ids:
                for trial in range(args.trials):
                    cells.append((fw.strip(), tid, condition.strip(), trial))

    log.info(
        "Solving %d cells (%d trials × %d frameworks × %d conditions × %d tasks) with %d workers",
        len(cells),
        args.trials,
        len(args.frameworks.split(",")),
        len(args.conditions.split(",")),
        len(eval_ids),
        args.workers,
    )

    counter = {"done": 0, "ok": 0, "no_artifact": 0, "hard_fail": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_process_cell, args, runtime, plans, fw, tid, cond, trial): (
                fw,
                tid,
                cond,
                trial,
            )
            for fw, tid, cond, trial in cells
        }
        for fut in as_completed(futures):
            fw, tid, cond, trial = futures[fut]
            try:
                r = fut.result()
            except Exception as exc:
                r = {
                    "outcome": "hard_fail",
                    "error": repr(exc)[:300],
                }
            counter["done"] += 1
            outcome = r.get("outcome", "hard_fail")
            counter[outcome] = counter.get(outcome, 0) + 1
            if counter["done"] % 5 == 0 or counter["done"] == len(cells):
                log.info(
                    "[%d/%d] last=%s/%s/%s/%d outcome=%s pass_rate=%s",
                    counter["done"],
                    len(cells),
                    fw,
                    tid,
                    cond,
                    trial,
                    outcome,
                    r.get("pass_rate"),
                )
    log.info("Done. counter=%s", counter)
    return 0


if __name__ == "__main__":
    sys.exit(main())
