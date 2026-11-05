"""Subprocess-isolated Hermes test runtime with token telemetry.

Why this exists
---------------
Hermes's ``AIAgent`` is in-process and uses ``os.chdir()`` plus the
``HERMES_HOME`` env var globally — both process-wide. The previous
``solver_native.run_hermes_test`` had to acquire a process-level lock
around every Hermes test trial, serialising all Hermes runs. This
runner pushes Hermes into its own subprocess so the orchestrator's
threadpool can dispatch Hermes trials in parallel; each subprocess has
its own globals/cwd and there is no cross-contamination.

The runner also patches ``openai.OpenAI.chat.completions.create`` to log
``{prompt_tokens, completion_tokens, latency_ms, model}`` per call into
a JSONL file. ``solver_native`` reads that file after the subprocess
exits and aggregates it into the cached ``result.json``.

Inputs and outputs
------------------
``--config <path>`` — JSON with these fields:
    workspace_dir       (str)    chdir target
    hermes_home         (str)    HERMES_HOME env value
    solver_model        (str)    e.g. "qwen/qwen3.6-plus"
    solver_base         (str)    OpenAI-compatible base URL
    solver_key          (str)    api key (or "" for keyless)
    provider            (str)    "openrouter" or "openai"
    max_iterations      (int)
    instruction         (str)    task instruction (truncated upstream)

``--out <path>`` — JSON written on exit with:
    outcome             ("ok" | "hard_fail")
    iterations_used     (int)
    final_response      (str, truncated)
    error               (optional, hard_fail only)
    traceback_tail      (optional, hard_fail only)

``--telemetry-jsonl <path>`` — per-LLM-call lines with the four fields
above (prompt_tokens / completion_tokens / latency_ms / model).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "experiments"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from _shared.llm_telemetry import JsonlRecorder, patch_openai_class  # noqa: E402
from _shared.local_paths import hermes_repo  # noqa: E402
from evolution.adapters.workspace_guard import (  # noqa: E402
    configure_hermes_terminal_env,
    install_framework_tool_guards,
)
from evolution.adapters.hermes_adapter import should_disable_streaming_for_solver  # noqa: E402

HERMES_REPO = hermes_repo()


def _install_openai_telemetry(jsonl_path: Path, *, extra_body: dict | None = None) -> JsonlRecorder:
    """Patch ``openai.OpenAI`` so every chat completion writes a JSONL line.

    Thin adapter over ``_shared.llm_telemetry.patch_openai_class``: opens a
    ``JsonlRecorder`` for the trial, then patches the OpenAI class so any
    client built afterwards (including by Hermes's ``AIAgent._create_openai_client``)
    is instrumented. Returns the recorder so the caller can keep it alive
    for the subprocess lifetime; ``patch_openai_class`` is idempotent.
    """
    recorder = JsonlRecorder(jsonl_path)
    recorder.open()
    patch_openai_class(recorder, source="hermes", extra_body=extra_body)
    return recorder


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--telemetry-jsonl", type=Path, required=True)
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if cfg.get("request_timeout_s"):
        os.environ["HERMES_API_TIMEOUT"] = str(cfg["request_timeout_s"])
        os.environ.setdefault("HERMES_STREAM_READ_TIMEOUT", str(cfg["request_timeout_s"]))
    if cfg.get("stale_timeout_s"):
        os.environ["HERMES_API_CALL_STALE_TIMEOUT"] = str(cfg["stale_timeout_s"])
        os.environ.setdefault("HERMES_STREAM_STALE_TIMEOUT", str(cfg["stale_timeout_s"]))

    # Telemetry must be installed BEFORE Hermes imports openai/litellm so
    # the patched __init__ catches the AIAgent's client construction.
    _install_openai_telemetry(args.telemetry_jsonl, extra_body=cfg.get("extra_body") or None)

    if str(HERMES_REPO) not in sys.path:
        sys.path.insert(0, str(HERMES_REPO))

    workspace_dir = Path(cfg["workspace_dir"])
    hermes_home = Path(cfg["hermes_home"])
    guard_log_path = Path(cfg.get("guard_log_path") or (hermes_home / "workspace_guard.jsonl"))
    os.environ["HERMES_HOME"] = str(hermes_home)
    if cfg.get("solver_key"):
        os.environ.setdefault("OPENAI_API_KEY", cfg["solver_key"])
    else:
        os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    configure_hermes_terminal_env(
        workspace_dir=workspace_dir,
        run_home=hermes_home,
        terminal_timeout_s=int(cfg.get("terminal_timeout_s", 45)),
        audit_log_path=guard_log_path,
    )

    started = time.time()
    out: dict = {"outcome": "hard_fail", "elapsed_s": 0.0}

    try:
        os.chdir(str(workspace_dir))
        install_framework_tool_guards(
            framework="hermes",
            workspace_dir=workspace_dir,
            run_home=hermes_home / "home",
            audit_log_path=guard_log_path,
        )
        from run_agent import AIAgent  # type: ignore

        agent = AIAgent(
            base_url=cfg["solver_base"],
            api_key=cfg.get("solver_key") or "EMPTY",
            model=cfg["solver_model"],
            provider=cfg.get("provider", "openrouter"),
            max_iterations=int(cfg.get("max_iterations", 90)),
            max_tokens=int(cfg.get("max_tokens") or 32768),
            quiet_mode=True,
        )
        if should_disable_streaming_for_solver(
            solver_base=cfg["solver_base"],
            solver_model=cfg["solver_model"],
        ):
            agent._disable_streaming = True
        envelope = (
            "Solve the Python task whose instruction follows. Your CWD is the "
            "task workspace. Use file ops to discover inputs, then write a "
            "_solve.py that implements the solution.\n\n"
            f"Task instruction:\n---\n{cfg['instruction']}\n---\n"
        )
        result = agent.run_conversation(envelope)
        if isinstance(result, dict):
            iterations_used = int(result.get("iterations_used", 0) or 0)
            final_response = str(result.get("final_response", ""))
        else:
            iterations_used = 0
            final_response = str(result)
        out = {
            "outcome": "ok",
            "iterations_used": iterations_used,
            "final_response": final_response,
            "elapsed_s": round(time.time() - started, 1),
        }
    except Exception as exc:
        out = {
            "outcome": "hard_fail",
            "error": f"hermes-runtime: {exc!r}"[:500],
            "traceback_tail": traceback.format_exc()[-600:],
            "elapsed_s": round(time.time() - started, 1),
        }
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
