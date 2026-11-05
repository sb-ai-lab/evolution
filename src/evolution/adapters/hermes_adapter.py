"""Hermes-agent adapter — drive native AIAgent + skill_manage; harvest train state.

Thin wrapper that drives Hermes's native train-time learning loop: each
train task is solved by ``AIAgent.run_conversation`` in-process, the
agent's own ``skill_manage`` tool decides what to persist as skills, and
we wrap that tool with a JSONL event log so harvest knows which on-disk
skills were created during which task. After train, the orchestrator
optionally calls ``run_curator_review`` (synchronous) for the consolidation
pass. Test runtime lives in the orchestrator's ``solver_native`` module.

The adapter is deliberately minimal:
  * No baseline blending / curated injection (orchestrator owns that).
  * No body-sha dedup (Hermes handles its own naming).
  * No cell-aware test-time mapping (orchestrator owns split-aware mapping).
  * Frontmatter compatibility layer is *minimal*: only ensures `name:`
    matches the destination directory and `description:` is non-empty;
    preserves the original SKILL.md alongside under ``SKILL.md.original``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._envelopes import (
    ANCHOR_EDIT_RULE,
    GENERIC_PROCEDURE_RULE,
    TASK_INSTRUCTION_BLOCK,
    TRACE_EVIDENCE_BLOCK,
    WORKSPACE_CWD_RULE,
    WORKSPACE_ROOT_WRITE_RULE,
)
from .common import REPO_ROOT, SKILLS_ROOT, PilotManifest, minimal_frontmatter_compat
from .common import slug as _slug
from .workspace_guard import configure_hermes_terminal_env, install_framework_tool_guards

logger = logging.getLogger(__name__)

FRAMEWORK_TAG = "hermes"

# Trace-fidelity cap for the train instruction; env-gated, generous default.
_INSTR_CHARS = int(os.environ.get("EVO_HERMES_INSTR_CHARS", "60000"))

HERMES_REPO = Path(os.environ.get("HERMES_REPO", ""))
HERMES_HOME = Path.home() / ".hermes_sbbench"

BUILTIN_SKILLS_DIR = HERMES_REPO / "skills"


def ensure_experiments_importable() -> None:
    """Put ``experiments/`` on ``sys.path`` so ``_shared.*`` bridges resolve."""
    experiments = REPO_ROOT / "experiments"
    if str(experiments) not in sys.path:
        sys.path.insert(0, str(experiments))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _yaml_sq(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _redact_config_api_key(hermes_home: Path) -> None:
    cfg_path = hermes_home / "config.yaml"
    if not cfg_path.is_file():
        return
    lines = []
    for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("api_key:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f"{indent}api_key: '<redacted>'")
        else:
            lines.append(line)
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def should_disable_streaming_for_solver(*, solver_base: str, solver_model: str) -> bool:
    """Return True for endpoints with unreliable streaming tool-call deltas."""
    override = os.environ.get("EVO_HERMES_DISABLE_STREAMING")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    base = (solver_base or "").lower()
    model = (solver_model or "").lower()
    is_local = "localhost" in base or "127.0.0.1" in base or "0.0.0.0" in base
    is_openrouter = "openrouter.ai" in base
    return (is_local or is_openrouter) and "gpt-oss" in model


# ---------------------------------------------------------------------------
# skill_manage event-log instrumentation
# ---------------------------------------------------------------------------
_skill_manage_current_task_id: str | None = None
_skill_manage_events_path: Path | None = None
_skill_manage_allowed_skill_id: str | None = None
_skill_manage_orig: Any = None


@dataclass
class HermesProducePlan:
    framework: str = FRAMEWORK_TAG
    per_task_skills: dict[str, list[str]] = field(default_factory=dict)
    all_produced_skill_names: list[str] = field(default_factory=list)
    all_produced_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    snapshot_dir: str = ""
    runtime: str = "in_process"
    skill_manage_events_count: int = 0
    skill_manage_events_path: str = ""
    skill_manage_event_names: list[str] = field(default_factory=list)
    skill_manage_events_parse_errors: int = 0
    evolved_skill_provenance: str = "skill_manage_event_log"
    normalised_fields: list[dict[str, Any]] = field(default_factory=list)
    runtime_seconds: float = 0.0
    home_dir: str = ""
    curator_result: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "per_task_skills": self.per_task_skills,
            "all_produced_skill_names": self.all_produced_skill_names,
            "all_produced_files": self.all_produced_files,
            "notes": self.notes,
            "attempts": self.attempts,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "snapshot_dir": self.snapshot_dir,
            "runtime": self.runtime,
            "skill_manage_events_count": self.skill_manage_events_count,
            "skill_manage_events_path": self.skill_manage_events_path,
            "skill_manage_event_names": self.skill_manage_event_names,
            "skill_manage_events_parse_errors": self.skill_manage_events_parse_errors,
            "evolved_skill_provenance": self.evolved_skill_provenance,
            "normalised_fields": self.normalised_fields,
            "runtime_seconds": self.runtime_seconds,
            "home_dir": self.home_dir,
            "curator_result": self.curator_result,
        }


# ---------------------------------------------------------------------------
# Hermes runtime config
# ---------------------------------------------------------------------------


def _ensure_hermes_path() -> None:
    if str(HERMES_REPO) not in sys.path:
        sys.path.insert(0, str(HERMES_REPO))


def _install_skill_manage_logger(
    events_path: Path,
    *,
    allowed_skill_id: str | None = None,
    home: Path | None = None,
) -> None:
    """Wrap ``tools.skill_manager_tool.skill_manage`` to emit JSONL events."""
    global _skill_manage_events_path, _skill_manage_orig, _skill_manage_allowed_skill_id
    _skill_manage_events_path = events_path
    _skill_manage_allowed_skill_id = allowed_skill_id
    events_path.parent.mkdir(parents=True, exist_ok=True)
    if not events_path.exists():
        events_path.touch()

    _ensure_hermes_path()
    target_home = Path(home) if home is not None else events_path.parent
    os.environ["HERMES_HOME"] = str(target_home)
    from tools import skill_manager_tool  # pyright: ignore[reportAttributeAccessIssue]

    skill_manager_tool.HERMES_HOME = target_home  # pyright: ignore[reportAttributeAccessIssue]
    skill_manager_tool.SKILLS_DIR = target_home / "skills"  # pyright: ignore[reportAttributeAccessIssue]
    (target_home / "skills").mkdir(parents=True, exist_ok=True)

    if _skill_manage_orig is None:
        _skill_manage_orig = skill_manager_tool.skill_manage

    orig = _skill_manage_orig

    def _wrapped(action, name, **kwargs):
        allowed = _skill_manage_allowed_skill_id
        blocked = False
        if allowed is not None:
            if action == "create":
                blocked = True
                result_text = json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"action 'create' blocked in iterative edit mode; "
                            f"patch or edit {allowed!r} only"
                        ),
                    }
                )
            elif name and name != allowed:
                blocked = True
                result_text = json.dumps(
                    {
                        "success": False,
                        "error": (f"skill name {name!r} not allowed; use {allowed!r} only"),
                    }
                )
            else:
                result_text = orig(action=action, name=name, **kwargs)
        else:
            result_text = orig(action=action, name=name, **kwargs)
        try:
            ok = False
            try:
                parsed = (
                    json.loads(result_text) if isinstance(result_text, str) else (result_text or {})
                )
                ok = bool(parsed.get("success"))
            except Exception:
                ok = False
            event = {
                "task_id": _skill_manage_current_task_id,
                "action": action,
                "name": name,
                "ok": ok,
                "blocked": blocked,
                "ts": time.time(),
            }
            assert _skill_manage_events_path is not None
            with _skill_manage_events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as exc:
            logger.warning("skill_manage event log failed: %s", exc)
        return result_text

    skill_manager_tool.skill_manage = _wrapped  # pyright: ignore[reportAttributeAccessIssue]


def _read_skill_manage_events(events_path: Path) -> list[dict[str, Any]]:
    """Back-compat: parse the JSONL event log, dropping malformed lines.

    Use ``_read_skill_manage_events_with_stats`` instead when you need
    visibility into how many lines failed to parse — concurrent appends
    from ProcessPoolExecutor workers can corrupt a line at the page
    boundary, and silently dropping that line reduces the harvested
    skill count without surfacing the cause.
    """
    events, _ = _read_skill_manage_events_with_stats(events_path)
    return events


def _read_skill_manage_events_with_stats(events_path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse the JSONL event log; return ``(events, parse_errors)``.

    ``parse_errors`` counts non-empty lines that failed ``json.loads``.
    Callers should surface this in their produce plan so a low harvest
    count can be distinguished from a corrupt event log.
    """
    if not events_path.is_file():
        return [], 0
    out: list[dict[str, Any]] = []
    parse_errors = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            parse_errors += 1
    return out, parse_errors


def _write_model_block_yaml(
    *,
    hermes_home: Path,
    provider: str,
    model_id: str,
    base_url: str,
    api_key: str,
    api_mode: str = "chat_completions",
    context_length: int | None = None,
    request_timeout_s: float | None = None,
    stale_timeout_s: float | None = None,
) -> None:
    """Write Hermes's ``model:`` block as runtime_provider expects.

    Verified against ``hermes_cli/runtime_provider.py:_get_model_config``
    which reads ``config["model"]`` for keys: ``provider``, ``default``
    (or alias ``model``), ``base_url``, ``api_key``, ``api_mode``. Top-level
    ``default_provider`` / ``default_model`` keys are NOT read by the
    provider resolver — they are ignored by curator's aux-client lookup.
    """
    hermes_home.mkdir(parents=True, exist_ok=True)
    cfg_path = hermes_home / "config.yaml"
    cfg_lines = [
        "model:",
        f"  provider: {_yaml_sq(provider)}",
        f"  default: {_yaml_sq(model_id)}",
        f"  base_url: {_yaml_sq(base_url)}",
        f"  api_key: {_yaml_sq(api_key)}",
        f"  api_mode: {_yaml_sq(api_mode)}",
    ]
    if context_length:
        cfg_lines.append(f"  context_length: {int(context_length)}")
        cfg_lines.extend(
            [
                "auxiliary:",
                "  compression:",
                f"    context_length: {int(context_length)}",
            ]
        )
    if request_timeout_s or stale_timeout_s:
        cfg_lines.extend(["providers:", f"  {provider}:"])
        if request_timeout_s:
            cfg_lines.append(f"    request_timeout_seconds: {float(request_timeout_s):g}")
        if stale_timeout_s:
            cfg_lines.append(f"    stale_timeout_seconds: {float(stale_timeout_s):g}")
        cfg_lines.extend(["    models:", f"      {_yaml_sq(model_id)}:"])
        if request_timeout_s:
            cfg_lines.append(f"        timeout_seconds: {float(request_timeout_s):g}")
        if stale_timeout_s:
            cfg_lines.append(f"        stale_timeout_seconds: {float(stale_timeout_s):g}")
    cfg_lines.extend(
        [
            "skills:",
            "  guard_agent_created: false",
        ]
    )
    cfg_path.write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")


def write_hermes_config(
    *,
    hermes_home: Path,
    solver_model: str,
    solver_base: str,
    solver_key: str,
    aux_model: str = "",
    aux_base: str = "",
    aux_key: str = "",
    context_length: int | None = None,
    request_timeout_s: float | None = None,
    stale_timeout_s: float | None = None,
) -> None:
    """Write $HERMES_HOME/config.yaml so that the active provider is the solver.

    Uses Hermes's ``model:`` schema (``provider`` / ``default`` /
    ``base_url`` / ``api_key``). The aux model parameters are accepted
    for API symmetry but only the solver block is written; to route the
    curator pass through aux, the orchestrator calls
    :func:`write_hermes_aux_default_config` to swap the model block, then
    restores via this function.
    """
    # Determine provider tag from base_url. OpenRouter base_urls are
    # routed via litellm with the "openrouter" provider; everything else
    # we treat as openai-compatible (the local sglang shape, OpenAI proper,
    # etc).
    provider = "openrouter" if "openrouter.ai" in (solver_base or "") else "openai"
    _ = aux_model, aux_base, aux_key  # symmetry; not written here
    if request_timeout_s is None:
        request_timeout_s = _env_float("EVO_HERMES_REQUEST_TIMEOUT_S", 240.0)
    if stale_timeout_s is None:
        stale_timeout_s = _env_float("EVO_HERMES_STALE_TIMEOUT_S", 180.0)
    _write_model_block_yaml(
        hermes_home=hermes_home,
        provider=provider,
        model_id=solver_model,
        base_url=solver_base,
        api_key=solver_key or "EMPTY",
        context_length=context_length,
        request_timeout_s=request_timeout_s,
        stale_timeout_s=stale_timeout_s,
    )


def write_hermes_aux_default_config(
    *,
    hermes_home: Path,
    solver_model: str,
    solver_base: str,
    solver_key: str,
    aux_model: str,
    aux_base: str,
    aux_key: str,
    context_length: int | None = None,
    request_timeout_s: float | None = None,
    stale_timeout_s: float | None = None,
) -> None:
    """Write a config.yaml whose ``model:`` block points at the aux provider.

    Used right before ``run_curator_review`` so the curator's auxiliary
    client resolves to the aux LLM (e.g. Gemini). Restore via
    :func:`write_hermes_config` afterward.
    """
    _ = solver_model, solver_base, solver_key  # symmetry; not written here
    provider = "openrouter" if "openrouter.ai" in (aux_base or "") else "openai"
    if request_timeout_s is None:
        request_timeout_s = _env_float("EVO_HERMES_REQUEST_TIMEOUT_S", 240.0)
    if stale_timeout_s is None:
        stale_timeout_s = _env_float("EVO_HERMES_STALE_TIMEOUT_S", 180.0)
    _write_model_block_yaml(
        hermes_home=hermes_home,
        provider=provider,
        model_id=aux_model,
        base_url=aux_base,
        api_key=aux_key or "EMPTY",
        context_length=context_length,
        request_timeout_s=request_timeout_s,
        stale_timeout_s=stale_timeout_s,
    )


# ---------------------------------------------------------------------------
# Per-train-task agent invocation
# ---------------------------------------------------------------------------


_HERMES_EDIT_MECHANISM = (
    "Persist updates via ``skill_manage(action='edit', name='{skill_id}', "
    "content=<full updated SKILL.md>)`` (or ``action='patch'``) ONLY."
)

_TRAIN_ENVELOPE = (
    "You are solving a Python coding task. You have access to your full tool "
    "set (file ops, terminal, ``skill_manage``). " + WORKSPACE_CWD_RULE + " "
    "Do NOT try to fetch files from anywhere else on disk.\n"
    "\n"
    "After you've solved the task, if the techniques you used would be reusable "
    "on similar future tasks, USE ``skill_manage(action='create', name=<slug>, "
    "content=<full SKILL.md text including frontmatter>)`` to capture a "
    "reusable skill (or ``action='patch'`` to extend an existing one). Skills "
    "should be GENERIC procedures, not the answer to this specific task.\n"
    "\n" + TASK_INSTRUCTION_BLOCK + "\n"
    "When done, summarise (a) what you tried, (b) whether you wrote a skill, "
    "(c) the skill name if you did.\n"
)

_EDIT_ENVELOPE = (
    ANCHOR_EDIT_RULE.format(skill_id="{skill_id}", edit_mechanism=_HERMES_EDIT_MECHANISM) + "\n\n"
    "1. Call ``skill_view(name='{skill_id}')`` to read the current SKILL.md.\n"
    "2. Apply the update through ``skill_manage`` as described above.\n"
    "3. Do NOT use the generic file ``patch`` tool on absolute paths. If you "
    "need to inspect a local copy, use ``skills/{skill_id}/SKILL.md`` relative "
    "to the current workspace; the persisted update must still be made through "
    "``skill_manage``.\n"
    "\n" + TRACE_EVIDENCE_BLOCK + "\n"
    "When done, summarise what you changed in the skill procedure. " + GENERIC_PROCEDURE_RULE + "\n"
)


def _role_of(task) -> str | None:
    cid = getattr(task, "cell_id", "") or ""
    if "." in cid:
        head = cid.split(".", 1)[0].strip().lower()
        if head:
            return head
    return None


def _write_native_trace(
    *,
    result: dict[str, Any],
    path: Path,
    task_id: str,
    events_path: Path | None,
) -> dict[str, Any]:
    messages = result.get("messages") if isinstance(result, dict) else []
    if not isinstance(messages, list):
        messages = []
    tool_events: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            tool_events.append(
                {
                    "message_idx": idx,
                    "kind": "tool_calls",
                    "tool_calls": msg.get("tool_calls"),
                }
            )
        elif role == "tool":
            tool_events.append(
                {
                    "message_idx": idx,
                    "kind": "tool_result",
                    "name": msg.get("name"),
                    "content_tail": str(msg.get("content", ""))[-4000:],
                }
            )
    trace = {
        "framework": FRAMEWORK_TAG,
        "task_id": task_id,
        "native_contract": {
            "conversation_messages": len(messages),
            "tool_events": len(tool_events),
            "skill_manage_events_path": str(events_path or ""),
        },
        "messages": messages,
        "tool_events": tool_events,
        "run_result": {
            k: v for k, v in result.items() if k not in {"messages"} and isinstance(k, str)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "native_trace_path": str(path),
        "native_contract": trace["native_contract"],
    }


def _cleanup_hermes_terminal_runtime() -> None:
    """Drain Hermes terminal environments before restoring parent env vars.

    Hermes registers an ``atexit`` cleanup hook in ``tools.terminal_tool``.
    If active environments remain after we restore the parent environment, that
    hook can resolve sandbox paths from host defaults instead of this isolated
    run home. Cleaning here keeps all sandbox state under the adapter run dir.
    """
    try:
        _ensure_hermes_path()
        from tools import terminal_tool  # pyright: ignore[reportAttributeAccessIssue]

        cleanup = getattr(terminal_tool, "cleanup_all_environments", None)
        if callable(cleanup):
            cleanup()
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes terminal runtime cleanup skipped/failed: %s", exc)


def _run_one_train_task(
    *,
    task_id: str,
    task_instruction: str,
    workspace_dir: Path,
    hermes_home: Path,
    solver_model_served: str,
    solver_base: str,
    solver_key: str,
    max_iterations: int = 90,
    events_path: Path | None = None,
    task_role: str | None = None,
    allowed_skill_id: str | None = None,
    raw_envelope: bool = False,
    make_task_venv: bool = True,
    disable_terminal: bool = False,
) -> dict[str, Any]:
    """Run AIAgent on one train task with cwd pinned to ``workspace_dir``.

    ``events_path`` is the JSONL location for skill_manage telemetry. When
    ``produce()`` runs train tasks via ``ProcessPoolExecutor`` (parallel),
    each worker process has fresh module-global state, so the parent's
    one-time ``_install_skill_manage_logger`` call doesn't reach the
    workers — re-install per worker here. Idempotent in the serial case
    because the wrapper preserves ``_skill_manage_orig`` on first install.
    """
    global _skill_manage_current_task_id
    workspace_dir = Path(workspace_dir).resolve()
    hermes_home = Path(hermes_home).resolve()
    if events_path is not None:
        events_path = Path(events_path).resolve()
    _ensure_hermes_path()
    saved_env = dict(os.environ)
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ.setdefault("OPENAI_API_KEY", solver_key or "EMPTY")
    home_override_token = None
    guard_log_path = (
        events_path.with_name("workspace_guard.jsonl")
        if events_path is not None
        else hermes_home / "workspace_guard.jsonl"
    )
    configure_hermes_terminal_env(
        workspace_dir=workspace_dir,
        run_home=hermes_home,
        terminal_timeout_s=45,
        audit_log_path=guard_log_path,
        make_task_venv=make_task_venv,
        task_role=task_role,
    )
    home_override_token = _pin_hermes_skill_runtime(hermes_home)
    if events_path is not None:
        _install_skill_manage_logger(
            events_path, allowed_skill_id=allowed_skill_id, home=hermes_home
        )
    install_framework_tool_guards(
        framework="hermes",
        workspace_dir=workspace_dir,
        run_home=hermes_home / "home",
        audit_log_path=guard_log_path,
        disable_terminal=disable_terminal,
    )
    from run_agent import AIAgent  # pyright: ignore[reportMissingImports]

    envelope = (
        task_instruction[:_INSTR_CHARS]
        if raw_envelope
        else _TRAIN_ENVELOPE.format(task_instruction=task_instruction[:_INSTR_CHARS])
    )

    saved_cwd = os.getcwd()
    _skill_manage_current_task_id = task_id
    started = time.time()
    telemetry_recorder = None
    telemetry_patched = False
    uninstall_openai_class_patch_fn = None
    try:
        if not workspace_dir.is_dir():
            return {
                "task_id": task_id,
                "ok": False,
                "elapsed_s": 0.0,
                "error": f"workspace_dir missing: {workspace_dir}",
            }
        if should_disable_streaming_for_solver(
            solver_base=solver_base,
            solver_model=solver_model_served,
        ):
            ensure_experiments_importable()
            from _shared.llm_telemetry import (  # pyright: ignore[reportMissingImports]
                JsonlRecorder,
                patch_openai_class,
                uninstall_openai_class_patch,
            )

            uninstall_openai_class_patch_fn = uninstall_openai_class_patch

            telemetry_path = (
                events_path.with_name(f"{task_id}__hermes_llm_telemetry.jsonl")
                if events_path is not None
                else hermes_home / f"{task_id}__hermes_llm_telemetry.jsonl"
            )
            telemetry_recorder = JsonlRecorder(telemetry_path)
            telemetry_recorder.open()
            telemetry_extra_body = None
            if "gpt-oss" in (solver_model_served or "").lower():
                reasoning_effort = os.environ.get("EVO_LLM_REASONING_EFFORT", "low")
                telemetry_extra_body = {"reasoning_effort": reasoning_effort}
            patch_openai_class(
                telemetry_recorder,
                source="hermes",
                extra_body=telemetry_extra_body,
            )
            telemetry_patched = True
        os.chdir(str(workspace_dir))
        agent = AIAgent(
            base_url=solver_base,
            api_key=solver_key or "EMPTY",
            model=solver_model_served,
            provider="openrouter" if "openrouter.ai" in solver_base else "openai",
            max_iterations=max_iterations,
            max_tokens=int(os.environ.get("HERMES_MAX_OUTPUT_TOKENS", "32768")),
        )
        if should_disable_streaming_for_solver(
            solver_base=solver_base,
            solver_model=solver_model_served,
        ):
            agent._disable_streaming = True
        result = agent.run_conversation(envelope)
        native_trace_path = (
            events_path.with_name(f"{task_id}__hermes_native_trace.json")
            if events_path is not None
            else hermes_home / f"{task_id}__hermes_native_trace.json"
        )
        native_trace = _write_native_trace(
            result=result if isinstance(result, dict) else {"final_response": str(result)},
            path=native_trace_path,
            task_id=task_id,
            events_path=events_path,
        )
        final = (
            (result.get("final_response") or "")[-4000:]
            if isinstance(result, dict)
            else str(result)[-4000:]
        )
        return {
            "task_id": task_id,
            "ok": True,
            "elapsed_s": round(time.time() - started, 1),
            "final_tail": final,
            "workspace_dir": str(workspace_dir),
            **native_trace,
        }
    except Exception as exc:
        return {
            "task_id": task_id,
            "ok": False,
            "elapsed_s": round(time.time() - started, 1),
            "error": repr(exc)[:500],
            "traceback_tail": traceback.format_exc()[-3000:],
            "workspace_dir": str(workspace_dir),
        }
    finally:
        os.chdir(saved_cwd)
        if telemetry_patched and uninstall_openai_class_patch_fn is not None:
            uninstall_openai_class_patch_fn()
        if telemetry_recorder is not None:
            telemetry_recorder.close()
        _cleanup_hermes_terminal_runtime()
        _reset_hermes_skill_runtime(home_override_token)
        os.environ.clear()
        os.environ.update(saved_env)
        _skill_manage_current_task_id = None


# ---------------------------------------------------------------------------
# Snapshot + harvest
# ---------------------------------------------------------------------------


def _snapshot_skills(hermes_home: Path) -> Path:
    src = hermes_home / "skills"
    if not src.is_dir():
        return src
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = hermes_home / f"skills_snapshot_{ts}"
    if dst.exists():
        raise RuntimeError(f"Snapshot dir already exists: {dst}")
    shutil.copytree(src, dst, symlinks=False)
    return dst


def _is_agent_created(skill_dir: Path) -> bool:
    if not (skill_dir / "SKILL.md").is_file():
        return False
    name = skill_dir.name
    if name.startswith("."):
        return False
    bundled = (
        {p.name for p in BUILTIN_SKILLS_DIR.iterdir() if p.is_dir()}
        if BUILTIN_SKILLS_DIR.is_dir()
        else set()
    )
    return name not in bundled


def _harvest_skills(
    snapshot: Path,
    plan: HermesProducePlan,
    *,
    allowed_skill_names: set[str] | None,
) -> list[Path]:
    """Walk snapshot/, copy each agent-created skill into Evolution's
    SkillStore as ``hermes__<slug>``, applying the minimal frontmatter
    compat layer and preserving the original SKILL.md as ``SKILL.md.original``.
    """
    imported: list[Path] = []
    if not snapshot.is_dir():
        return imported
    for skill_dir in sorted(snapshot.iterdir()):
        if not skill_dir.is_dir() or not _is_agent_created(skill_dir):
            continue
        if allowed_skill_names is not None and skill_dir.name not in allowed_skill_names:
            plan.notes.append(f"hermes: skipping {skill_dir.name} (not in skill_manage event log)")
            continue
        src_md = skill_dir / "SKILL.md"
        original_text = src_md.read_text(encoding="utf-8", errors="replace")
        slug = _slug(skill_dir.name)
        dest_name = f"hermes__{slug}"
        dest_dir = SKILLS_ROOT / dest_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in skill_dir.iterdir():
            target = dest_dir / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        # Preserve the original SKILL.md alongside; rewrite the
        # canonical SKILL.md only when the compat layer changed something.
        modified, fields = minimal_frontmatter_compat(original_text, dest_name)
        if fields:
            (dest_dir / "SKILL.md.original").write_text(original_text, encoding="utf-8")
            (dest_dir / "SKILL.md").write_text(modified, encoding="utf-8")
            plan.normalised_fields.append(
                {"skill": dest_name, "fields": fields, "source": skill_dir.name}
            )
        plan.all_produced_skill_names.append(dest_name)
        plan.all_produced_files.append(str(dest_dir / "SKILL.md"))
        imported.append(dest_dir)
        logger.info("hermes: harvested %s → %s", skill_dir.name, dest_name)
    return imported


# ---------------------------------------------------------------------------
# Curator pass — synchronous run_curator_review
# ---------------------------------------------------------------------------


def run_curator(hermes_home: Path) -> dict[str, Any]:
    """Trigger one synchronous curator pass via ``run_curator_review``.

    The curator's auxiliary client resolves via Hermes's runtime provider
    (``hermes_cli.runtime_provider.resolve_runtime_provider``), so the
    caller is responsible for pointing ``$HERMES_HOME/config.yaml`` at the
    desired aux provider before invoking this function.
    """
    _ensure_hermes_path()
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    try:
        from agent.curator import run_curator_review  # pyright: ignore[reportMissingImports]

        result = run_curator_review(on_summary=lambda s: None, synchronous=True)
        return {"ok": result is not None, "result": result}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)[:600]}


# ---------------------------------------------------------------------------
# Public produce API
# ---------------------------------------------------------------------------


def produce(
    *,
    pilot: PilotManifest,
    solver_model: str,
    solver_base: str,
    solver_key: str,
    aux_model: str,
    aux_base: str,
    aux_key: str,
    task_instructions: dict[str, str],
    task_workspaces: dict[str, Path],
    hermes_home: Path = HERMES_HOME,
    max_iterations: int = 90,
    run_curator_pass: bool = True,
) -> HermesProducePlan:
    """Produce-phase entrypoint."""
    plan = HermesProducePlan()
    started_at = time.time()

    if hermes_home.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive = hermes_home.with_name(f"{hermes_home.name}_{ts}")
        if archive.exists():
            plan.notes.append(f"hermes: refusing to overwrite archive {archive}")
            plan.skipped = True
            plan.skip_reason = "archive collision"
            return plan
        hermes_home.rename(archive)
        plan.notes.append(f"hermes: archived prior HERMES_HOME → {archive.name}")
    hermes_home.mkdir(parents=True, exist_ok=True)
    plan.home_dir = str(hermes_home)

    served_id = solver_model.split("/", 1)[-1] if "/" in solver_model else solver_model
    write_hermes_config(
        hermes_home=hermes_home,
        solver_model=solver_model,
        solver_base=solver_base,
        solver_key=solver_key,
        aux_model=aux_model,
        aux_base=aux_base,
        aux_key=aux_key,
    )
    plan.notes.append(f"hermes: wrote {hermes_home}/config.yaml (default=solver={served_id})")

    events_path = hermes_home / ".skill_manage_events.jsonl"
    _install_skill_manage_logger(events_path, home=hermes_home)
    plan.skill_manage_events_path = str(events_path)
    plan.notes.append(f"hermes: skill_manage event log at {events_path}")

    parallel = max(1, int(os.environ.get("HERMES_TRAIN_PARALLEL", "1")))
    runnable: list[tuple[Any, Path]] = []
    for t in pilot.train:
        ws = task_workspaces.get(t.id)
        if ws is None or not Path(ws).is_dir():
            plan.attempts.append(
                {"task_id": t.id, "ok": False, "error": f"missing task workspace: {ws}"}
            )
            plan.notes.append(f"hermes: train task {t.id} skipped (no workspace)")
            continue
        runnable.append((t, Path(ws)))

    def _run(task_id: str, ws_path: Path, task_role: str | None) -> dict[str, Any]:
        return _run_one_train_task(
            task_id=task_id,
            task_instruction=task_instructions.get(task_id, ""),
            workspace_dir=ws_path,
            hermes_home=hermes_home,
            solver_model_served=served_id,
            solver_base=solver_base,
            solver_key=solver_key,
            max_iterations=max_iterations,
            events_path=events_path,
            task_role=task_role,
        )

    if parallel == 1:
        for t, ws_path in runnable:
            attempt = _run(t.id, ws_path, _role_of(t))
            plan.attempts.append(attempt)
            if not attempt["ok"]:
                plan.notes.append(f"hermes: train task {t.id} failed: {attempt.get('error')}")
            else:
                logger.info("hermes: train %s ok in %.1fs", t.id, attempt["elapsed_s"])
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        logger.info("hermes: running %d train tasks with %d workers", len(runnable), parallel)
        with ProcessPoolExecutor(max_workers=parallel) as pool:
            future_to_tid: dict[Any, str] = {}
            for t, ws_path in runnable:
                fut = pool.submit(
                    _run_one_train_task,
                    task_id=t.id,
                    task_instruction=task_instructions.get(t.id, ""),
                    workspace_dir=ws_path,
                    hermes_home=hermes_home,
                    solver_model_served=served_id,
                    solver_base=solver_base,
                    solver_key=solver_key,
                    max_iterations=max_iterations,
                    events_path=events_path,
                    task_role=_role_of(t),
                )
                future_to_tid[fut] = t.id
            for fut in as_completed(future_to_tid):
                tid = future_to_tid[fut]
                try:
                    attempt = fut.result()
                except Exception as exc:
                    attempt = {
                        "task_id": tid,
                        "ok": False,
                        "elapsed_s": 0.0,
                        "error": f"worker exception: {exc!r}",
                    }
                plan.attempts.append(attempt)
                if not attempt.get("ok"):
                    plan.notes.append(f"hermes: train task {tid} failed: {attempt.get('error')}")
                else:
                    logger.info("hermes: train %s ok in %.1fs", tid, attempt["elapsed_s"])

    if run_curator_pass and aux_base:
        # Flip default to aux for the curator pass, then restore.
        write_hermes_aux_default_config(
            hermes_home=hermes_home,
            solver_model=solver_model,
            solver_base=solver_base,
            solver_key=solver_key,
            aux_model=aux_model,
            aux_base=aux_base,
            aux_key=aux_key,
        )
        cur = run_curator(hermes_home)
        plan.curator_result = cur
        plan.notes.append(f"hermes: curator pass ok={cur.get('ok')}")
        # Restore solver default for any downstream in-process AIAgent uses.
        write_hermes_config(
            hermes_home=hermes_home,
            solver_model=solver_model,
            solver_base=solver_base,
            solver_key=solver_key,
            aux_model=aux_model,
            aux_base=aux_base,
            aux_key=aux_key,
        )

    events, parse_errors = _read_skill_manage_events_with_stats(events_path)
    plan.skill_manage_events_count = len(events)
    plan.skill_manage_events_parse_errors = parse_errors
    allowed: set[str] = set()
    for e in events:
        if not e.get("ok"):
            continue
        name = e.get("name")
        if isinstance(name, str) and name:
            allowed.add(name)
    plan.skill_manage_event_names = sorted(allowed)
    plan.notes.append(
        f"hermes: {len(events)} skill_manage events, {len(allowed)} unique successful "
        f"skill names ({parse_errors} malformed JSONL lines dropped)"
    )

    snapshot = _snapshot_skills(hermes_home)
    plan.snapshot_dir = str(snapshot)
    _harvest_skills(snapshot, plan, allowed_skill_names=allowed if events else set())

    # Per-task tagging from event log (orchestrator may use this for analysis).
    produced_set = set(plan.all_produced_skill_names)
    per_train_skills: dict[str, list[str]] = {t.id: [] for t in pilot.train}
    for e in events:
        tid = e.get("task_id") or ""
        raw_nm = e.get("name") or ""
        if not (e.get("ok") and tid and raw_nm):
            continue
        dest_nm = f"hermes__{_slug(raw_nm)}"
        if dest_nm not in produced_set:
            continue
        if tid in per_train_skills and dest_nm not in per_train_skills[tid]:
            per_train_skills[tid].append(dest_nm)
    for t in pilot.train:
        plan.per_task_skills[t.id] = list(per_train_skills.get(t.id, []))

    if not plan.all_produced_skill_names:
        # Native runtime ran but produced no skills — orchestrator labels
        # this as ``no_artifact_produced`` for post_train, NOT a hard fail.
        plan.notes.append(
            "hermes: no agent-created skills harvested; "
            "post_train test rows will run with empty $HERMES_HOME/skills/."
        )

    plan.runtime_seconds = round(time.time() - started_at, 1)
    return plan


def seed_canonical_skill(*, hermes_home: Path, skill_id: str, body: str) -> Path:
    """Write anchor skill to ``$HERMES_HOME/skills/<skill_id>/SKILL.md``."""
    hermes_home = Path(hermes_home).resolve()
    skill_dir = hermes_home / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    normalized, _ = minimal_frontmatter_compat(body, skill_id)
    skill_md.write_text(normalized, encoding="utf-8")
    snap = hermes_home / ".skills_prompt_snapshot.json"
    if snap.is_file():
        snap.unlink()
    return skill_md


def _pin_hermes_skill_runtime(hermes_home: Path):
    """Point Hermes module-level skill paths at this run's isolated home."""
    token = None
    try:
        _ensure_hermes_path()
        import hermes_constants  # pyright: ignore[reportMissingImports]

        token = hermes_constants.set_hermes_home_override(hermes_home)
    except Exception:
        token = None
    for mod_name in ("tools.skills_tool", "tools.skill_manager_tool"):
        try:
            mod = __import__(mod_name, fromlist=["SKILLS_DIR"])
            mod.HERMES_HOME = hermes_home
            mod.SKILLS_DIR = hermes_home / "skills"
        except Exception:
            continue
    return token


def _reset_hermes_skill_runtime(token) -> None:
    if token is None:
        return
    try:
        _ensure_hermes_path()
        import hermes_constants  # pyright: ignore[reportMissingImports]

        hermes_constants.reset_hermes_home_override(token)
    except Exception:
        pass


_NATIVE_EDIT_ENVELOPE = (
    ANCHOR_EDIT_RULE.format(skill_id="{skill_id}", edit_mechanism=_HERMES_EDIT_MECHANISM) + "\n"
    "This is iteration round {round_idx}; the skill's recent train pass-rate was {prior_pass_rate}.\n"
    "\n"
    "1. Solve the Python task below with your full tool set (terminal, file ops). "
    + WORKSPACE_CWD_RULE
    + " Then "
    + WORKSPACE_ROOT_WRITE_RULE
    + ".\n"
    "2. After solving, fold what worked into ``{skill_id}`` IN PLACE as described above.\n"
    "3. " + GENERIC_PROCEDURE_RULE + "\n"
    "\n" + TASK_INSTRUCTION_BLOCK
)


def _split_task_ref(default_role: str, task_id: str) -> tuple[str, str]:
    text = str(task_id)
    if "/" in text:
        role_, plain = text.split("/", 1)
        return role_, plain
    return default_role, text


def _native_solve_edit(
    *,
    skill_id: str,
    skill_body: str,
    bundles: list[dict[str, Any]],
    run_dir: Path,
    editor_cfg: dict[str, Any],
    api_key: str,
    round_idx: int,
    hermes_home: Path,
    events_path: Path,
    served: str,
    base: str,
) -> tuple[str, dict[str, Any]] | None:
    """Solve train tasks with Hermes's full agent so edits are grounded in its own runs."""
    corpus_root = editor_cfg.get("corpus_root")
    train_ids = [str(t) for t in (editor_cfg.get("train_ids") or [])]
    if not corpus_root or not train_ids:
        return None
    ensure_experiments_importable()
    import _shared.workspace_materializer as workspace_materializer  # pyright: ignore[reportMissingImports]

    materialize_workspace = workspace_materializer.materialize_workspace

    events_path.parent.mkdir(parents=True, exist_ok=True)
    cell_role = str(editor_cfg.get("role") or "")
    rates = [float(b.get("pass_rate", 0.0) or 0.0) for b in bundles]
    prior_pass = f"{sum(rates) / len(rates):.2f}" if rates else "n/a"
    attempts: list[dict[str, Any]] = []
    for tid in train_ids:
        role_, plain = _split_task_ref(cell_role, tid)
        task_dir = Path(corpus_root) / "tasks" / role_ / plain
        instr_path = task_dir / "instruction.md"
        instruction = (
            instr_path.read_text(encoding="utf-8", errors="replace") if instr_path.is_file() else ""
        )
        ws = run_dir / "hermes_edit_workspace" / f"round{round_idx}" / plain
        try:
            materialize_workspace(task_dir, ws)
        except Exception as exc:  # workspace prep is the only non-agent failure surface
            attempts.append({"task_id": tid, "ok": False, "error": f"materialize: {exc!r}"[:300]})
            continue
        res = _run_one_train_task(
            task_id=f"edit-r{round_idx}-{plain}",
            task_instruction=_NATIVE_EDIT_ENVELOPE.format(
                skill_id=skill_id,
                round_idx=round_idx,
                prior_pass_rate=prior_pass,
                task_instruction=instruction,
            ),
            workspace_dir=ws,
            hermes_home=hermes_home,
            solver_model_served=served,
            solver_base=base,
            solver_key=api_key,
            max_iterations=int(os.environ.get("EVO_HERMES_EDIT_MAX_ITER", "40")),
            events_path=events_path,
            task_role=role_,
            allowed_skill_id=skill_id,
            raw_envelope=True,
            make_task_venv=True,
            disable_terminal=False,
        )
        attempts.append(
            {"task_id": tid, "ok": bool(res.get("ok", True)), "elapsed_s": res.get("elapsed_s")}
        )
    skill_md = hermes_home / "skills" / skill_id / "SKILL.md"
    events, parse_errors = _read_skill_manage_events_with_stats(events_path)
    violations = [
        e
        for e in events
        if e.get("action") == "create" or (e.get("name") and e.get("name") != skill_id)
    ]
    if not skill_md.is_file():
        return skill_body, {
            "editor": "hermes_native_solve",
            "ok": False,
            "error": "anchor SKILL.md missing after native solve edit",
            "attempts": attempts,
            "events_path": str(events_path),
            "events_count": len(events),
        }
    return skill_md.read_text(encoding="utf-8", errors="replace"), {
        "editor": "hermes_native_solve",
        "ok": True,
        "attempts": attempts,
        "events_path": str(events_path),
        "events_count": len(events),
        "parse_errors": parse_errors,
        "violations": violations,
    }


def edit_skill_from_traces(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    bundles: list[dict[str, Any]],
    run_dir: Path,
    editor_cfg: dict[str, Any],
    api_key: str,
    round_idx: int = 0,
    role: str = "",
) -> tuple[str, dict[str, Any]]:
    """Iterative-loop editor: Hermes solves train tasks and updates the anchor skill in place."""
    _ = role
    hermes_home = run_dir / "hermes_home" / f"round{round_idx}"
    hermes_home.mkdir(parents=True, exist_ok=True)
    write_hermes_config(
        hermes_home=hermes_home,
        solver_model=editor_cfg.get("served_id_hint") or editor_cfg.get("id", ""),
        solver_base=editor_cfg.get("api_base", ""),
        solver_key=api_key,
    )
    seed_canonical_skill(hermes_home=hermes_home, skill_id=skill_id, body=skill_body)
    events_path = run_dir / "traces" / f"round{round_idx}" / "_hermes_edit_events.jsonl"
    native_on = str(os.environ.get("EVO_HERMES_NATIVE_SOLVE_EDIT", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if native_on:
        native = _native_solve_edit(
            skill_id=skill_id,
            skill_body=skill_body,
            bundles=bundles,
            run_dir=run_dir,
            editor_cfg=editor_cfg,
            api_key=api_key,
            round_idx=round_idx,
            hermes_home=hermes_home,
            events_path=events_path,
            served=editor_cfg.get("served_id_hint") or editor_cfg.get("id", ""),
            base=editor_cfg.get("api_base", ""),
        )
        if native is not None:
            _redact_config_api_key(hermes_home)
            return native
    workspace = run_dir / "hermes_edit_workspace" / f"round{round_idx}"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_skill_md = workspace / "skills" / skill_id / "SKILL.md"
    workspace_skill_md.parent.mkdir(parents=True, exist_ok=True)
    workspace_skill_md.write_text(skill_body, encoding="utf-8")
    trace_cap = int(os.environ.get("EVO_HERMES_EDIT_TRACE_CHARS", "60000") or "60000")
    envelope = _EDIT_ENVELOPE.format(skill_id=skill_id, trace_digest=traces_digest[:trace_cap])
    result = _run_one_train_task(
        task_id=f"edit-r{round_idx}",
        task_instruction=envelope,
        workspace_dir=workspace,
        hermes_home=hermes_home,
        solver_model_served=editor_cfg.get("served_id_hint") or editor_cfg.get("id", ""),
        solver_base=editor_cfg.get("api_base", ""),
        solver_key=api_key,
        max_iterations=int(os.environ.get("EVO_HERMES_EDIT_MAX_ITER", "40")),
        events_path=events_path,
        task_role=None,
        allowed_skill_id=skill_id,
        raw_envelope=True,
        make_task_venv=False,
        disable_terminal=True,
    )
    skill_md = hermes_home / "skills" / skill_id / "SKILL.md"
    if not skill_md.is_file():
        _redact_config_api_key(hermes_home)
        return skill_body, {
            "editor": "hermes",
            "ok": False,
            "error": "anchor SKILL.md missing after edit",
            "run_result": result,
            "events_path": str(events_path),
        }
    new_body = skill_md.read_text(encoding="utf-8", errors="replace")
    workspace_body = (
        workspace_skill_md.read_text(encoding="utf-8", errors="replace")
        if workspace_skill_md.is_file()
        else skill_body
    )
    used_workspace_copy = False
    if new_body == skill_body and workspace_body != skill_body:
        new_body = workspace_body
        used_workspace_copy = True
    events, parse_errors = _read_skill_manage_events_with_stats(events_path)
    violations = [
        e
        for e in events
        if e.get("action") == "create" or (e.get("name") and e.get("name") != skill_id)
    ]
    _redact_config_api_key(hermes_home)
    return new_body, {
        "editor": "hermes",
        "ok": bool(result.get("ok", True)),
        "events_path": str(events_path),
        "events_count": len(events),
        "parse_errors": parse_errors,
        "violations": violations,
        "used_workspace_skill_copy": used_workspace_copy,
        "run_result": {k: result.get(k) for k in ("task_id", "ok", "elapsed_s", "error")},
    }


__all__ = [
    "FRAMEWORK_TAG",
    "HERMES_HOME",
    "HERMES_REPO",
    "HermesProducePlan",
    "_EDIT_ENVELOPE",
    "edit_skill_from_traces",
    "produce",
    "run_curator",
    "seed_canonical_skill",
    "write_hermes_config",
    "write_hermes_aux_default_config",
    "_install_skill_manage_logger",
    "_read_skill_manage_events",
    "_read_skill_manage_events_with_stats",
]
