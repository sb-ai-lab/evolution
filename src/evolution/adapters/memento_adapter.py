"""Memento-Skills adapter — drive ``memento agent`` per train task.

Thin wrapper that runs Memento's full agent loop (Intent → Plan → Execute
→ Reflect → Finalize) per train task as a subprocess with an isolated
``HOME``. Memento's reflection autonomously decides whether to invoke
``skill-creator``, so the envelope here is a plain task envelope (no
forced skill-creator usage). After train, the orchestrator merges the
per-task ``~/memento_s/skills/`` trees into a unified ``test_home`` and
synchronises the registry via ``memento agent --message "list skills"``
(or ``SkillRegistry.sync_from_disk`` directly).

The DreamLoop daemon is intentionally NOT exercised here — the public
surface is ``DreamLoop.run_timer()`` (infinite poll) with no one-shot
trigger. The report row is labeled "Memento agent loop only — daemon not
exercised".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ._envelopes import (
    ANCHOR_EDIT_RULE,
    FILE_OUTPUT_EXAMPLE_FLAT,
    FILE_OUTPUT_EXAMPLE_NESTED,
    GENERIC_PROCEDURE_RULE,
    NO_PACKAGE_INSTALL_RULE,
    PROC_MEMORY_SKILL_CREATOR,
    TASK_INSTRUCTION_BLOCK,
    TRACE_EVIDENCE_BLOCK,
    WORKSPACE_CWD_RULE,
    WORKSPACE_LISTING_HEADER,
)
from ._paths import memento_bin
from .common import PilotManifest, minimal_frontmatter_compat
from .workspace_guard import build_guarded_env

logger = logging.getLogger(__name__)

FRAMEWORK_TAG = "memento"
# Trace-fidelity cap for the train instruction; env-gated, generous default.
_INSTR_CHARS = int(os.environ.get("EVO_MEMENTO_INSTR_CHARS", "60000"))
MEMENTO_RUNS_DIR = Path.home() / "memento_sbbench"
MEMENTO_REPO = Path(os.environ.get("MEMENTO_REPO", ""))

_STOP_SENTINELS = (
    "stopped: repeated identical tool call",
    "stopped: max",
    "stopped due to",
    "timeout after",
)


def _cfg_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _clip_middle(text: str, *, max_chars: int, label: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = (
        f"\n\n[... omitted {len(text) - max_chars} chars from the middle of "
        f"{label}; preserved beginning and end ...]\n\n"
    )
    if len(marker) >= max_chars:
        return text[:max_chars]
    keep = max_chars - len(marker)
    head = max(1, int(keep * 0.65))
    tail = max(1, keep - head)
    return text[:head] + marker + text[-tail:]


def _with_memento_pythonpath(env: dict[str, str]) -> dict[str, str]:
    current = env.get("PYTHONPATH", "")
    paths = [str(MEMENTO_REPO)]
    if current:
        paths.append(current)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["MEMENTO_REPO"] = str(MEMENTO_REPO)
    return env


def _install_memento_allowed_roots_override(
    home: Path,
    allowed_roots: list[Path],
    denied_roots: list[Path] | None = None,
) -> Path:
    """Patch Memento's upstream path boundary for isolated benchmark roots.

    Memento-Skills currently treats ``/home`` as a system directory on Linux.
    In this workspace every isolated experiment home and session workspace
    lives under the user's ``$HOME``, so unmodified Memento blocks ordinary
    relative tool paths such as ``.`` and ``notes.md``. The override is scoped
    to the subprocess via ``sitecustomize`` and only exempts explicit
    benchmark roots created for this run.
    """
    override_dir = Path(home) / "_memento_path_override"
    override_dir.mkdir(parents=True, exist_ok=True)
    sitecustomize = override_dir / "sitecustomize.py"
    sitecustomize.write_text(
        r"""
from pathlib import Path
import os

_raw = os.environ.get("MEMENTO_BENCHMARK_ALLOWED_ROOTS", "")
_allowed = []
for _part in _raw.split(os.pathsep):
    if not _part:
        continue
    try:
        _allowed.append(Path(_part).resolve(strict=False))
    except Exception:
        pass

_raw_denied = os.environ.get("MEMENTO_BENCHMARK_DENIED_ROOTS", "")
_denied = []
for _part in _raw_denied.split(os.pathsep):
    if not _part:
        continue
    try:
        _denied.append(Path(_part).resolve(strict=False))
    except Exception:
        pass

try:
    from shared.tools.path_boundary import PathBoundary

    _orig_is_system_path = PathBoundary.is_system_path

    def _is_system_path_with_benchmark_roots(self, path):
        try:
            resolved = Path(path).resolve(strict=False)
            for root in _denied:
                if resolved == root or resolved.is_relative_to(root):
                    return True
            for root in _allowed:
                if resolved == root or resolved.is_relative_to(root):
                    return False
        except Exception:
            pass
        return _orig_is_system_path(self, path)

    PathBoundary.is_system_path = _is_system_path_with_benchmark_roots
except Exception:
    pass

try:
    from core.memento_s.agent import MementoSAgent

    _orig_build_agent_config = MementoSAgent._build_agent_config

    def _build_agent_config_with_benchmark_overrides(self):
        cfg = _orig_build_agent_config(self)
        _max_react = os.environ.get("EVO_MEMENTO_MAX_REACT_PER_STEP", "")
        if _max_react:
            try:
                cfg.max_react_per_step = max(1, int(_max_react))
            except Exception:
                pass
        _max_replans = os.environ.get("EVO_MEMENTO_MAX_REPLANS", "")
        if _max_replans:
            try:
                cfg.max_replans = max(0, int(_max_replans))
            except Exception:
                pass
        return cfg

    MementoSAgent._build_agent_config = _build_agent_config_with_benchmark_overrides
except Exception:
    pass
""".lstrip(),
        encoding="utf-8",
    )
    return override_dir


def _per_task_home(task_id: str) -> Path:
    return MEMENTO_RUNS_DIR / task_id


def _per_task_paths(task_id: str) -> tuple[Path, Path, Path, Path]:
    home = _per_task_home(task_id)
    cfg = home / "memento_s"
    return home, cfg, cfg / "skills", cfg / "workspace"


def _has_stop_sentinel(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    for s in _STOP_SENTINELS:
        if s in lower:
            return s
    return None


def _memento_session_workspace(workspace_dir: Path, session_id: str) -> Path:
    date_str = datetime.now().strftime("%Y-%m-%d")
    short_id = hashlib.md5(session_id.encode()).hexdigest()[:8]
    return Path(workspace_dir).resolve() / date_str / short_id


def _copy_workspace_inputs_to_session(workspace_dir: Path, session_workspace: Path) -> int:
    workspace_dir = Path(workspace_dir).resolve()
    session_workspace = Path(session_workspace).resolve()
    session_workspace.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in sorted(workspace_dir.iterdir()):
        if item.name in {"sessions", datetime.now().strftime("%Y-%m-%d")}:
            continue
        if item.resolve(strict=False) == session_workspace:
            continue
        target = session_workspace / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target, symlinks=False)
        else:
            shutil.copy2(item, target)
        copied += 1
    return copied


def _create_memento_cli_session(env: dict[str, str]) -> str:
    code = r"""
import asyncio
from bootstrap import bootstrap_sync
bootstrap_sync()
from shared.chat import ChatManager

async def main():
    session = await ChatManager.create_session(
        title="SB-Bench task",
        metadata={"channel": "sbbench-table3"},
    )
    print(f"SB_SESSION_ID={session.id}")

asyncio.run(main())
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(MEMENTO_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(f"memento session bootstrap failed rc={proc.returncode}: {text[-1000:]}")
    match = re.search(r"SB_SESSION_ID=([a-z0-9_-]+)", text)
    if not match:
        raise RuntimeError(f"memento session bootstrap did not print a session id: {text[-1000:]}")
    return match.group(1)


@dataclass
class MementoProducePlan:
    framework: str = FRAMEWORK_TAG
    per_task_skills: dict[str, list[str]] = field(default_factory=dict)
    all_produced_skill_names: list[str] = field(default_factory=list)
    all_produced_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    test_home_dir: str = ""
    runs_root: str = ""
    runtime_seconds: float = 0.0
    normalised_fields: list[dict] = field(default_factory=list)
    daemon_exercised: bool = False
    runtime_label: str = "memento agent loop only — daemon not exercised"

    def to_jsonable(self) -> dict:
        return {
            "framework": self.framework,
            "per_task_skills": self.per_task_skills,
            "all_produced_skill_names": self.all_produced_skill_names,
            "all_produced_files": self.all_produced_files,
            "notes": self.notes,
            "attempts": self.attempts,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "test_home_dir": self.test_home_dir,
            "runs_root": self.runs_root,
            "runtime_seconds": self.runtime_seconds,
            "normalised_fields": self.normalised_fields,
            "daemon_exercised": self.daemon_exercised,
            "runtime_label": self.runtime_label,
        }


def write_llm_config(
    *,
    solver_model: str,
    solver_base: str,
    solver_key: str,
    embedding_model: str,
    embedding_base: str,
    embedding_key: str,
    config_dir: Path,
    workspace_dir: Path | None = None,
    max_iterations: int | None = None,
    llm_timeout_s: float | None = None,
    served_model_id: str | None = None,
) -> None:
    """Write Memento's per-process ``config.json``.

    The agent's own LLM is the solver (qwen3.6-plus). Embeddings are
    swapped to OpenRouter qwen3-embedding-8b. Reranker disabled.

    ``workspace_dir``, when provided, pins ``paths.workspace_dir`` so the
    agent's session sandbox is rooted under the caller-controlled
    workspace instead of the default ``~/memento_s/workspace``.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = config_dir / "config.json"
    # litellm needs a provider hint when the model id is not its own slash
    # convention. For OpenRouter base_urls, route via "openrouter".
    litellm_provider = "openrouter" if "openrouter.ai" in (solver_base or "") else ""
    if litellm_provider == "openrouter":
        # Memento joins ``litellm_provider`` and ``model`` internally. The
        # experiment CLI may pass Evolution/LiteLLM ids such as
        # ``openai/openai/gpt-oss-20b``; without this collapse Memento calls
        # ``openrouter/openai/openai/gpt-oss-20b`` and all native calls fail.
        if solver_model.startswith("openrouter/"):
            solver_model = solver_model.removeprefix("openrouter/")
        if solver_model.startswith("openai/openai/"):
            solver_model = solver_model.removeprefix("openai/")
    elif served_model_id and solver_base:
        # Local OpenAI-compatible endpoint: litellm strips ONE leading "openai/"
        # provider segment before hitting base_url, so the model must be
        # "openai/<served-id>" to deliver the EXACT served id. When the served id
        # itself begins with "openai/" (vLLM may serve e.g. "openai/gpt-oss-120b"),
        # a single prefix is stripped to the wrong name and 404s — hence doubling.
        served = served_model_id.strip()
        solver_model = served if served.startswith("openai/openai/") else f"openai/{served}"
    if max_iterations is None:
        max_iterations = int(os.environ.get("EVO_MEMENTO_MAX_ITERATIONS", "24") or "24")
    if llm_timeout_s is None:
        llm_timeout_s = float(os.environ.get("EVO_MEMENTO_LLM_TIMEOUT_S", "240") or "240")

    pilot_profile: dict[str, object] = {
        "model": solver_model,
        "api_key": solver_key or "not-needed",
        "base_url": solver_base,
        "litellm_provider": litellm_provider,
        "max_tokens": 32000,
        "temperature": 0.2,
        "timeout": llm_timeout_s,
    }
    solver_model_l = solver_model.lower()
    if "qwen3" in solver_model_l:
        pilot_profile["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": False},
        }
    elif "gpt-oss" in solver_model_l:
        pilot_profile["extra_body"] = {
            "reasoning_effort": os.environ.get("EVO_LLM_REASONING_EFFORT", "low")
        }
    raw_extra_body = (
        os.environ.get("EVO_MEMENTO_EXTRA_BODY_JSON")
        or os.environ.get("EVO_OPENAI_COMPAT_EXTRA_BODY_JSON")
        or ""
    ).strip()
    if raw_extra_body:
        try:
            parsed_extra_body = json.loads(raw_extra_body)
        except json.JSONDecodeError:
            parsed_extra_body = None
        if isinstance(parsed_extra_body, dict):
            existing_extra_body = pilot_profile.get("extra_body")
            merged_extra_body = (
                dict(existing_extra_body) if isinstance(existing_extra_body, dict) else {}
            )
            merged_extra_body.update(parsed_extra_body)
            pilot_profile["extra_body"] = merged_extra_body
    cfg: dict[str, object] = {
        "llm": {
            "active_profile": "pilot",
            "profiles": {"pilot": pilot_profile},
        },
        "agent": {
            "max_iterations": max(1, int(max_iterations)),
            "max_react_per_step": max(
                1,
                int(os.environ.get("EVO_MEMENTO_MAX_REACT_PER_STEP", "12") or "12"),
            ),
            "max_replans": max(
                0,
                int(os.environ.get("EVO_MEMENTO_MAX_REPLANS", "4") or "4"),
            ),
        },
        "gateway": {
            "enabled": False,
        },
        "dream": {
            "enabled": False,
        },
        "memory": {
            "enabled": False,
        },
        "logging": {
            "level": "WARNING",
            "verbose": False,
        },
        "skills": {
            "retrieval": {
                "embedding_base_url": embedding_base,
                "embedding_api_key": embedding_key,
                "embedding_model": embedding_model.removeprefix("openrouter/"),
                "embedding_dimension": int(os.environ.get("EVO_EMBEDDING_DIMENSION", "1536")),
                "reranker_enabled": False,
            },
            "execution": {
                "pip_install_timeout_sec": 1,
            },
        },
    }
    if workspace_dir is not None:
        cfg["paths"] = {"workspace_dir": str(workspace_dir.resolve())}
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def redact_memento_configs(root: Path) -> int:
    """Redact provider keys from persisted Memento config artifacts."""
    root = Path(root)
    if not root.exists():
        return 0
    redacted = 0
    for cfg_path in root.rglob("config.json"):
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        changed = False
        profiles = (data.get("llm") or {}).get("profiles") or {}
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if isinstance(profile, dict) and profile.get("api_key"):
                    profile["api_key"] = "<redacted>"
                    changed = True
        retrieval = (data.get("skills") or {}).get("retrieval") or {}
        if isinstance(retrieval, dict):
            emb_key = str(retrieval.get("embedding_api_key") or "")
            if emb_key and emb_key.upper() not in {"EMPTY", "NONE", "NOT-NEEDED"}:
                retrieval["embedding_api_key"] = "<redacted>"
                changed = True
        if changed:
            cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            redacted += 1
    return redacted


def memento_phase_max_iterations(phase: str, default: int) -> int:
    key = f"EVO_MEMENTO_{phase.upper()}_MAX_ITERATIONS"
    raw = os.environ.get(key, os.environ.get("EVO_MEMENTO_MAX_ITERATIONS", str(default)))
    try:
        return max(1, int(raw or default))
    except ValueError:
        return max(1, int(default))


def memento_phase_timeout_s(phase: str, requested: float, default_cap: float) -> float:
    key = f"EVO_MEMENTO_{phase.upper()}_TIMEOUT_S"
    raw = os.environ.get(key, os.environ.get("EVO_MEMENTO_TIMEOUT_S", str(default_cap)))
    try:
        cap = max(1.0, float(raw or default_cap))
    except ValueError:
        cap = max(1.0, float(default_cap))
    return min(float(requested), cap)


def _workspace_listing(workspace_dir: Path, *, cap: int = 200) -> str:
    """Input files relative to the workspace root, so the agent need not blind-probe."""
    root = Path(workspace_dir)
    if not root.is_dir():
        return "(empty)"
    rels = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and not str(p.relative_to(root)).startswith("tests/")
        and p.name != "data_generator.py"
    )
    return "\n".join(rels[:cap]) or "(empty)"


_TRAIN_ENVELOPE = (
    "You are solving the Python coding task below. "
    + WORKSPACE_CWD_RULE
    + " "
    + PROC_MEMORY_SKILL_CREATOR
    + "\n"
    "\n" + NO_PACKAGE_INSTALL_RULE + "\n"
    "\n"
    "Output contract: pytest verifies files under the task workspace's "
    "``output/`` directory. " + FILE_OUTPUT_EXAMPLE_FLAT + " " + FILE_OUTPUT_EXAMPLE_NESTED + "\n"
    "\n" + WORKSPACE_LISTING_HEADER + "\n"
    "{workspace_listing}\n"
    "\n" + TASK_INSTRUCTION_BLOCK
)


def memento_native_services_profile(value: str | None = None) -> str:
    raw = (value or os.environ.get("EVO_MEMENTO_NATIVE_SERVICES_PROFILE") or "").strip().lower()
    if not raw:
        return "benchmark_minimal"
    aliases = {
        "minimal": "benchmark_minimal",
        "benchmark_minimal": "benchmark_minimal",
        "native_reflection": "native_reflection",
        "reflection": "native_reflection",
        "native_full": "native_full",
        "full": "native_full",
    }
    if raw not in aliases:
        allowed = ", ".join(sorted(aliases))
        raise ValueError(f"Unknown EVO_MEMENTO_NATIVE_SERVICES_PROFILE={raw!r}; allowed: {allowed}")
    return aliases[raw]


def _apply_native_services_profile(env: dict[str, str], profile: str) -> None:
    """Configure Memento lifecycle services for an isolated benchmark HOME."""
    env["MEMENTO_DISABLE_GATEWAY"] = "1"
    if profile == "benchmark_minimal":
        env["MEMENTO_DISABLE_DREAM_DAEMON"] = "1"
        env["MEMENTO_DISABLE_AGENT_PROFILE_EVOLVER"] = "1"
        env["MEMENTO_DISABLE_MEMORY_CONSOLIDATION"] = os.environ.get(
            "EVO_MEMENTO_DISABLE_CONSOLIDATION", "1"
        )
    elif profile == "native_reflection":
        env["MEMENTO_DISABLE_DREAM_DAEMON"] = "1"
        env["MEMENTO_DISABLE_AGENT_PROFILE_EVOLVER"] = "0"
        env["MEMENTO_DISABLE_MEMORY_CONSOLIDATION"] = os.environ.get(
            "EVO_MEMENTO_DISABLE_CONSOLIDATION", "0"
        )
    elif profile == "native_full":
        env["MEMENTO_DISABLE_DREAM_DAEMON"] = os.environ.get(
            "EVO_MEMENTO_DISABLE_DREAM_DAEMON", "0"
        )
        env["MEMENTO_DISABLE_AGENT_PROFILE_EVOLVER"] = "0"
        env["MEMENTO_DISABLE_MEMORY_CONSOLIDATION"] = "0"
    else:  # pragma: no cover - protected by resolver
        raise ValueError(profile)


def write_memento_native_trace(
    *,
    path: Path,
    task_id: str,
    session_workspace: Path | None,
    stdout: str,
    stderr: str,
    changed_skills: list[str] | None = None,
    native_services_profile: str | None = None,
) -> dict:
    profile = memento_native_services_profile(native_services_profile)
    session_files: list[dict] = []
    if session_workspace and Path(session_workspace).is_dir():
        for p in sorted(Path(session_workspace).rglob("*"))[:200]:
            if p.is_file():
                try:
                    session_files.append(
                        {
                            "path": str(p.relative_to(session_workspace)),
                            "size": p.stat().st_size,
                            "suffix": p.suffix,
                        }
                    )
                except OSError:
                    continue
    trace = {
        "framework": FRAMEWORK_TAG,
        "task_id": task_id,
        "native_services_profile": profile,
        "native_contract": {
            "session_workspace": str(session_workspace or ""),
            "session_file_count": len(session_files),
            "changed_skills": list(changed_skills or []),
            "stdout_tail_chars": min(len(stdout or ""), 8000),
            "stderr_tail_chars": min(len(stderr or ""), 4000),
        },
        "session_files": session_files,
        "stdout_tail": (stdout or "")[-8000:],
        "stderr_tail": (stderr or "")[-4000:],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"native_trace_path": str(path), "native_contract": trace["native_contract"]}


def _invoke_memento(
    envelope: str,
    *,
    home: Path,
    workspace_dir: Path,
    skills_dir: Path,
    timeout_s: float = 900.0,
) -> tuple[int, str, str, Path | None]:
    """Run ``memento agent --message`` with isolated HOME and CWD."""
    home = Path(home).resolve()
    workspace_dir = Path(workspace_dir).resolve()
    skills_dir = Path(skills_dir).resolve()
    env = build_guarded_env(
        framework="memento",
        workspace_dir=workspace_dir,
        run_home=home,
        audit_log_path=home / "workspace_guard.jsonl",
        base_env=os.environ.copy(),
        set_home=True,
        command_wrappers=True,
        python_fs_guard_on_pythonpath=False,
    )
    env["MEMENTO_ENV"] = "dev"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("LITELLM_LOG", "ERROR")
    env["MEMENTO_USE_CURRENT_PYTHON_SANDBOX"] = "1"
    env["MEMENTO_DISABLE_PYTHON_DEP_INSTALL"] = "1"
    env["MEMENTO_DISABLE_BUILTIN_SKILL_SYNC"] = "1"
    _apply_native_services_profile(env, memento_native_services_profile())
    env["MEMENTO_SANDBOX_PYTHON"] = sys.executable
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    allowed_roots = [
        workspace_dir,
        home,
        skills_dir,
    ]
    env["MEMENTO_BENCHMARK_ALLOWED_ROOTS"] = os.pathsep.join(
        str(p.resolve(strict=False)) for p in allowed_roots
    )
    denied_roots = [
        workspace_dir / "tests",
        workspace_dir / "data_generator.py",
    ]
    override_dir = _install_memento_allowed_roots_override(
        home,
        allowed_roots,
        denied_roots,
    )
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(override_dir)
        if not current_pythonpath
        else str(override_dir) + os.pathsep + current_pythonpath
    )
    _with_memento_pythonpath(env)
    home.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    tele_path = os.environ.get("EVO_PRODUCE_TELEMETRY_JSONL")
    tele_wrapper = os.environ.get("EVO_MEMENTO_TELEMETRY_WRAPPER")
    use_wrapper = bool(tele_path and tele_wrapper and Path(tele_wrapper).is_file())
    try:
        if use_wrapper:
            # The wrapper runs the agent via in-process ``app()``, which cannot
            # load a session pre-created by a separate subprocess. Mirror the
            # eval path: pin the id via env and let the agent create it in-process.
            import uuid

            session_id = "produce-" + uuid.uuid4().hex[:12]
            env["MEMENTO_PILOT_SESSION_ID"] = session_id
        else:
            session_id = _create_memento_cli_session(env)
        session_workspace = _memento_session_workspace(workspace_dir, session_id)
        _copy_workspace_inputs_to_session(workspace_dir, session_workspace)
        denied_roots.extend(
            [
                session_workspace / "tests",
                session_workspace / "data_generator.py",
            ]
        )
        env["MEMENTO_BENCHMARK_DENIED_ROOTS"] = os.pathsep.join(
            str(p.resolve(strict=False)) for p in denied_roots
        )
    except Exception as exc:
        return -1, "", f"session bootstrap failed: {exc}", None
    if use_wrapper:
        cmd = [
            sys.executable,
            tele_wrapper,
            "--telemetry-jsonl",
            tele_path,
            "agent",
            "--message",
            envelope,
            "--no-markdown",
        ]
    else:
        cmd = [
            memento_bin(),
            "agent",
            "--message",
            envelope,
            "--session",
            session_id,
            "--no-markdown",
        ]
    try:
        # killpg-safe: a leaked-pipe grandchild can hang subprocess.run(timeout) forever.
        from evolution.eval._proc import run_capture

        proc = run_capture(cmd, timeout=timeout_s, env=env, cwd=str(workspace_dir))
        return proc.returncode, proc.stdout, proc.stderr, session_workspace
    except subprocess.TimeoutExpired as exc:
        return -1, "", f"timeout after {timeout_s}s: {exc}", session_workspace


def _harvest_one_task_skills(
    *,
    task_id: str,
    skills_dir: Path,
    test_home_skills: Path,
    plan: MementoProducePlan,
    anchor_ts: float,
    exclude: set[str] | None = None,
) -> list[str]:
    """Copy every fresh agent-created skill directory from one task's
    ``skills_dir`` into the unified ``test_home_skills`` tree.

    A directory is considered fresh if (a) it has a SKILL.md whose mtime
    is after ``anchor_ts`` (i.e. produced by THIS pilot run) and (b) it
    is not the built-in ``skill-creator`` directory.

    Returns the list of names successfully copied for ``task_id``.
    """
    out: list[str] = []
    if not skills_dir.is_dir():
        return out
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "skill-creator" or child.name in (exclude or set()):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            mtime = skill_md.stat().st_mtime
        except OSError:
            continue
        if mtime <= anchor_ts:
            continue
        # Copy to the unified test_home tree, applying minimal
        # frontmatter compat.
        dest_name = child.name
        dest_dir = test_home_skills / dest_name
        if dest_dir.exists():
            # Cross-task collision: keep the first-seen copy; record a note.
            plan.notes.append(
                f"memento: collision on skill {dest_name!r} from task {task_id} "
                "(already in unified test_home from earlier task); kept first copy."
            )
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in child.iterdir():
            target = dest_dir / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        original = (dest_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        modified, fields = minimal_frontmatter_compat(original, dest_name)
        if fields:
            (dest_dir / "SKILL.md.original").write_text(original, encoding="utf-8")
            (dest_dir / "SKILL.md").write_text(modified, encoding="utf-8")
            plan.normalised_fields.append(
                {"skill": dest_name, "fields": fields, "source_task": task_id}
            )
        out.append(dest_name)
    return out


def _memento_registry_aliases(skill_dir_name: str, skill_md: Path) -> list[str]:
    aliases = {skill_dir_name}
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\n#]+)", text)
    if match:
        frontmatter_name = match.group(1).strip()
        if frontmatter_name:
            aliases.add(frontmatter_name)
            aliases.add(frontmatter_name.replace("-", "_"))
            aliases.add(frontmatter_name.replace("_", "-"))
            slug = re.sub(r"[^A-Za-z0-9]+", "_", frontmatter_name).strip("_")
            if slug:
                aliases.add(slug)
                aliases.add(slug.replace("_", "-"))
    return sorted(a for a in aliases if a)


def _sync_test_home_registry(test_home: Path) -> dict:
    """Make Memento's SkillRegistry index the unified test_home/skills/.

    Writes Memento's ``skill.json`` directly instead of launching
    ``memento agent --message "list skills"``. The CLI path is expensive
    and can make an unrelated LLM call; for the Table 3 artifact all we
    need is a registry that points at the isolated skill directories.

    Returns a dict including ``registered_skills``: the list of skill
    names found in ``skill.json`` after sync. Empty list means the
    sync produced no registry entries (callers should treat this as a
    hard failure when files were copied to disk).
    """
    test_home.mkdir(parents=True, exist_ok=True)
    skills_dir = test_home / "memento_s" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entries: dict[str, dict[str, object]] = {}
    collisions: list[dict[str, str]] = []
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        entry = {
            "location": str(child.resolve()),
            "source": "local",
            "version": 1,
            "installed_at": now,
            "status": "active",
            "tags": [],
        }
        for alias in _memento_registry_aliases(child.name, skill_md):
            existing = entries.get(alias)
            if existing and existing.get("location") != entry["location"]:
                collisions.append(
                    {
                        "alias": alias,
                        "kept": str(existing.get("location") or ""),
                        "skipped": str(entry["location"]),
                    }
                )
                continue
            entries[alias] = dict(entry)

    payload = {
        "$schema": "skill_config_schema.json",
        "version": 1,
        "skills": entries,
        "index": {
            "last_sync": now,
            "sync_errors": collisions,
        },
    }
    sj = test_home / "memento_s" / "skill.json"
    sj.parent.mkdir(parents=True, exist_ok=True)
    sj.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "method": "direct skill.json write",
        "registered_skills": sorted(entries),
        "alias_collisions": collisions,
        "skill_json_exists": sj.is_file(),
        "rc": 0,
    }


def produce(
    *,
    pilot: PilotManifest,
    solver_model: str,
    solver_base: str,
    solver_key: str,
    embedding_model: str,
    embedding_base: str,
    embedding_key: str,
    task_instructions: dict[str, str],
    task_workspaces: dict[str, Path] | None = None,
    runs_root: Path = MEMENTO_RUNS_DIR,
    test_home_dirname: str = "test_home",
    timeout_per_task_s: float = 900.0,
    extra_skills: list[Path] | None = None,
    solver_served_id: str | None = None,
) -> MementoProducePlan:
    """Drive native ``memento agent`` per train task; produce a unified test_home.

    ``task_workspaces`` (optional) maps ``task_id -> Path`` to a workspace
    directory pre-populated with that task's input files. Contents are
    copied into ``<home>/memento_s/workspace/`` before each invocation so
    the Memento agent operates on real task inputs (not an empty dir).
    Pass ``None`` only for unit tests that intentionally exercise the
    no-inputs case.

    ``extra_skills`` (optional): a list of SKILL.md *file* paths whose
    parent directories will be copied verbatim into each per-task
    ``skills_dir`` BEFORE the Memento agent runs. Memento's auto-sync
    picks them up via ``SkillRegistry`` so the agent's native discovery
    surfaces them. Use this to register orchestrator-provided skills
    (e.g. a black-box verifier) without touching the train envelope.
    """
    plan = MementoProducePlan()
    started_at = time.time()

    try:
        memento_path = memento_bin()
    except RuntimeError as exc:
        plan.notes.append(f"memento: {exc}")
        plan.skipped = True
        plan.skip_reason = "memento CLI missing"
        return plan
    if not Path(memento_path).exists():
        plan.notes.append(f"memento: CLI not installed at {memento_path}; adapter cannot run.")
        plan.skipped = True
        plan.skip_reason = "memento CLI missing"
        return plan

    if runs_root.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive = runs_root.with_name(f"{runs_root.name}_{ts}")
        if archive.exists():
            plan.notes.append(f"memento: refusing to overwrite archive {archive}")
            plan.skipped = True
            plan.skip_reason = "archive collision"
            return plan
        runs_root.rename(archive)
        plan.notes.append(f"memento: archived prior runs root → {archive.name}")
    runs_root.mkdir(parents=True, exist_ok=True)
    plan.runs_root = str(runs_root)

    # Concurrency: opt-in via env
    parallel = max(
        1,
        min(
            int(
                os.environ.get(
                    "MEMENTO_TRAIN_PARALLEL",
                    os.environ.get("SMOKE4WAY_MEMENTO_PARALLEL", "1"),
                )
            ),
            16,
        ),
    )

    anchor_ts = time.time()

    def _drive_one(t) -> dict:
        home, config_dir, skills_dir, workspace_dir = _per_task_paths(t.id)
        # Plant orchestrator-provided extra skills before the agent runs;
        # Memento's registry sync picks them up at next ``list skills``.
        if extra_skills:
            for src_md in extra_skills:
                src_md = Path(src_md)
                if not src_md.is_file():
                    continue
                # Each SKILL.md sits in its own directory; copy that dir
                # to <skills_dir>/<dir_name>/ verbatim.
                src_dir = src_md.parent
                dst_dir = skills_dir / src_dir.name
                dst_dir.mkdir(parents=True, exist_ok=True)
                for entry in src_dir.iterdir():
                    if entry.is_file():
                        shutil.copy2(entry, dst_dir / entry.name)
                    elif entry.is_dir():
                        target = dst_dir / entry.name
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(entry, target)
        write_llm_config(
            solver_model=solver_model,
            solver_base=solver_base,
            solver_key=solver_key,
            embedding_model=embedding_model,
            embedding_base=embedding_base,
            embedding_key=embedding_key,
            config_dir=config_dir,
            max_iterations=memento_phase_max_iterations("train", 24),
            served_model_id=solver_served_id,
        )
        # Populate workspace_dir with the task's real inputs so the agent
        # can interact with files (not an empty cwd). Without this the
        # Memento subprocess sees only the instruction text and has
        # nothing to operate on — which is what the user flagged.
        workspace_dir.mkdir(parents=True, exist_ok=True)
        copied_files = 0
        if task_workspaces is not None:
            src_ws = task_workspaces.get(t.id)
            if src_ws is not None and Path(src_ws).is_dir():
                for item in Path(src_ws).iterdir():
                    if item.name.startswith("."):
                        continue
                    target = workspace_dir / item.name
                    if item.is_dir():
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.copytree(item, target)
                    else:
                        shutil.copy2(item, target)
                    copied_files += 1
        envelope = _TRAIN_ENVELOPE.format(
            task_instruction=(task_instructions.get(t.id, "") or "")[:_INSTR_CHARS],
            workspace_listing=_workspace_listing(workspace_dir),
        )
        logger.info("memento: invoking agent for %s (workspace files=%d)", t.id, copied_files)
        rc, out, err, session_workspace = _invoke_memento(
            envelope,
            home=home,
            workspace_dir=workspace_dir,
            skills_dir=skills_dir,
            timeout_s=memento_phase_timeout_s("train", timeout_per_task_s, 1800.0),
        )
        redacted_configs = redact_memento_configs(home)
        sentinel = _has_stop_sentinel((out or "") + "\n" + (err or ""))
        ok = rc == 0 and not sentinel
        if not ok and rc == 0 and sentinel:
            rc = 2  # downgrade so harvest skips this task
        return {
            "task_id": t.id,
            "rc": rc,
            "ok": ok,
            "sentinel": sentinel or "",
            "stdout_tail": (out or "")[-800:],
            "stderr_tail": (err or "")[-800:],
            "home": str(home),
            "skills_dir": str(skills_dir),
            "workspace_dir": str(workspace_dir),
            "session_workspace": str(session_workspace) if session_workspace else "",
            "redacted_configs": redacted_configs,
        }

    if parallel == 1:
        attempt_results = [_drive_one(t) for t in pilot.train]
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        logger.info(
            "memento: parallel produce %d workers over %d tasks",
            parallel,
            len(pilot.train),
        )
        attempt_results = [None] * len(pilot.train)  # type: ignore
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_drive_one, t): i for i, t in enumerate(pilot.train)}
            for fut in as_completed(futures):
                idx = futures[fut]
                attempt_results[idx] = fut.result()
    plan.attempts = [a for a in attempt_results if a is not None]

    # Build the unified test_home by merging accepted attempts' skills/.
    test_home = runs_root / test_home_dirname
    test_home.mkdir(parents=True, exist_ok=True)
    test_skills = test_home / "memento_s" / "skills"
    test_skills.mkdir(parents=True, exist_ok=True)
    plan.test_home_dir = str(test_home)

    extra_skill_names = {Path(s).parent.name for s in (extra_skills or [])}
    seen_names: set[str] = set()
    for attempt in plan.attempts:
        if not attempt.get("ok"):
            continue
        names = _harvest_one_task_skills(
            task_id=attempt["task_id"],
            skills_dir=Path(attempt["skills_dir"]),
            test_home_skills=test_skills,
            plan=plan,
            anchor_ts=anchor_ts,
            exclude=extra_skill_names,
        )
        plan.per_task_skills.setdefault(attempt["task_id"], []).extend(names)
        for n in names:
            if n not in seen_names:
                plan.all_produced_skill_names.append(n)
                plan.all_produced_files.append(str(test_skills / n / "SKILL.md"))
                seen_names.add(n)

    if plan.all_produced_skill_names:
        # Trigger registry sync so SkillGateway/LocalRecall index the
        # newly-merged on-disk skills.
        sync = _sync_test_home_registry(test_home)
        plan.notes.append(f"memento: registry sync via {sync.get('method')}: {sync}")
        # Gate: every harvested skill must appear in skill.json after
        # sync. Files-on-disk-only is NOT acceptable — the runtime
        # retrieval (LocalRecall/BM25) reads from the registry, not the
        # filesystem. A registry miss means post_train can't actually
        # retrieve that skill at test time.
        registered = set(sync.get("registered_skills") or [])
        harvested = set(plan.all_produced_skill_names)
        unregistered = sorted(harvested - registered)
        registered_in_harvest = len(harvested) - len(unregistered)
        plan.notes.append(
            f"memento: registry verification — {registered_in_harvest}/{len(harvested)} "
            f"harvested skills registered; unregistered={unregistered}"
        )
        if not registered:
            # Total registry failure — no skill is retrievable at test
            # time. Hard-skip so downstream solver_native rows get an
            # honest hard_fail rather than silently running with files
            # the runtime cannot see.
            plan.skipped = True
            plan.skip_reason = (
                f"memento registry sync produced 0 registered skills "
                f"despite {len(harvested)} harvested files (skill_json_exists="
                f"{sync.get('skill_json_exists')}); LocalRecall cannot retrieve them."
            )
        elif unregistered:
            plan.notes.append(
                f"memento: WARN {len(unregistered)} harvested skills are NOT in "
                f"skill.json and will not be retrievable: {unregistered[:10]}"
            )
        redacted = redact_memento_configs(test_home)
        if redacted:
            plan.notes.append(f"memento: redacted {redacted} persisted config(s)")
    else:
        plan.notes.append(
            "memento: no agent-created skills harvested across all train tasks; "
            "post_train test rows will run with empty test_home/memento_s/skills/."
        )

    plan.runtime_seconds = round(time.time() - started_at, 1)
    return plan


_MEMENTO_EDIT_MECHANISM = (
    "The skill file is at ~/memento_s/skills/{skill_id}/SKILL.md — "
    "read it, improve it based on the trace evidence, and write the updated "
    "version back to that exact path. Do not create unrelated skill directories."
)

_MEMENTO_EDIT_ENVELOPE = (
    ANCHOR_EDIT_RULE.format(skill_id="{skill_id}", edit_mechanism=_MEMENTO_EDIT_MECHANISM)
    + "\n\n"
    + TRACE_EVIDENCE_BLOCK
    + "\n"
    "Improve the skill procedure based on the evidence. " + GENERIC_PROCEDURE_RULE + "\n"
)


def _extract_skill_md_candidate(text: str, *, skill_id: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    fenced: list[str] = []
    for match in re.finditer(
        r"```(?:markdown|md)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE
    ):
        block = match.group(1).strip()
        if block.startswith("---"):
            fenced.append(block)
    if fenced:
        return fenced[-1].strip() + "\n"
    start = raw.find("---\n")
    if start >= 0:
        block = raw[start:].strip()
        block = re.sub(r"\n```\s*$", "", block).strip()
        if block.startswith("---"):
            return block.strip() + "\n"
    return ""


async def _direct_edit_skill_md(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    editor_cfg: dict,
    api_key: str,
    timeout_s: float | None = None,
) -> tuple[str, dict]:
    from evolution.llm.litellm_backend import LiteLLMBackend

    model = (
        editor_cfg.get("litellm_id")
        or editor_cfg.get("id")
        or editor_cfg.get(
            "served_id_hint",
            "",
        )
    )
    kwargs: dict[str, object] = {
        "api_base": editor_cfg.get("api_base", ""),
        "api_key": api_key or "EMPTY",
    }
    if isinstance(editor_cfg.get("extra_body"), dict):
        kwargs["extra_body"] = dict(editor_cfg["extra_body"])
    if editor_cfg.get("max_output_tokens"):
        kwargs["max_tokens"] = int(editor_cfg["max_output_tokens"])
    if editor_cfg.get("temperature") is not None:
        kwargs["temperature"] = float(editor_cfg["temperature"])
    if timeout_s is not None:
        kwargs["timeout"] = float(timeout_s)
    if model.split("/", 1)[0] in ("codex", "claude-code", "hermes"):
        # Coding-agent CLI reflector for the in-process direct-edit fallback;
        # the main Memento-S agent runs in a subprocess (HTTP) and is unaffected.
        from evolution.llm.backend import get_backend

        backend = get_backend(model)
    else:
        backend = LiteLLMBackend(model=model, **kwargs)
    single_task_trace_fidelity = (
        str(editor_cfg.get("single_task_trace_fidelity", "")).strip().lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
        or editor_cfg.get("single_task_trace_fidelity") is True
    )
    specificity_rule = (
        "This is a single-task trace-fidelity diagnostic. Preserve concrete input "
        "filenames, output filenames, schema keys, exception messages, and small "
        "code idioms that appear in the trace when they are needed to avoid the "
        "observed failure. Do not copy pytest source or hidden expected answers."
        if single_task_trace_fidelity
        else "Keep the guidance portable: do not include benchmark paths, exact task "
        "answers, or test code."
    )
    prompt = (
        f"Rewrite reusable skill {skill_id!r} as a complete Agent Skill "
        "SKILL.md file. Return only the full SKILL.md text, starting with YAML "
        "frontmatter. Do not wrap it in Markdown fences. Preserve useful "
        "existing sections, but add concrete procedural guidance from the trace "
        "evidence.\n\n"
        "Rules:\n"
        f"- {specificity_rule}\n"
        "- For every observed traceback, add a direct prevention rule and, when "
        "useful, a short safe code pattern.\n"
        "- If the trace shows structured JSON/YAML values, state exactly how to "
        "extract the scalar value before arithmetic or comparisons.\n"
        "- Keep runtime path guidance consistent with the trace's execution "
        "contract; do not introduce parent directories or absolute paths that "
        "the trace did not authorize.\n\n"
        "- For Memento's native skill runtime, make file-tool guidance explicit: "
        "tool arguments must be evaluated path strings, never source-code "
        "expressions such as os.path.join(...), and the skill should verify that "
        "requested artifacts contain complete YAML/JSON rather than placeholders.\n\n"
        "# Current SKILL.md\n"
        f"{skill_body}\n\n"
        "# Trace Evidence\n"
        f"{traces_digest}"
    )
    resp = await backend.complete(
        [{"role": "user", "content": prompt}],
        system="You edit reusable procedural skills. Output only SKILL.md.",
    )
    content = resp.content.strip()
    extracted = _extract_skill_md_candidate(content, skill_id=skill_id)
    return extracted or content.strip() + "\n", {
        "fallback_model": model,
        "fallback_tokens_in": resp.tokens_in,
        "fallback_tokens_out": resp.tokens_out,
        "fallback_time_ms": resp.time_ms,
    }


def edit_skill_from_traces(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    bundles: list[dict],
    run_dir: Path,
    editor_cfg: dict,
    api_key: str,
    round_idx: int = 0,
) -> tuple[str, dict]:
    """Dual-output editor: native ``test_home`` + canonical anchor harvest."""
    _ = bundles
    test_home = run_dir / "native_state" / "memento"
    skills_dir = test_home / "memento_s" / "skills"
    anchor_dir = skills_dir / skill_id
    anchor_dir.mkdir(parents=True, exist_ok=True)
    (anchor_dir / "SKILL.md").write_text(skill_body, encoding="utf-8")
    anchor_ts = time.time()
    workspace = run_dir / "memento_edit_workspace" / f"round{round_idx}"
    workspace.mkdir(parents=True, exist_ok=True)
    write_llm_config(
        solver_model=editor_cfg.get("litellm_id")
        or editor_cfg.get("id")
        or editor_cfg.get("served_id_hint", ""),
        solver_base=editor_cfg.get("api_base", ""),
        solver_key=api_key,
        embedding_model=(
            editor_cfg.get("embedding_id")
            or editor_cfg.get("embedding_model")
            or editor_cfg.get("id", "")
        ),
        embedding_base=(
            editor_cfg.get("embedding_api_base")
            or editor_cfg.get("embedding_base")
            or editor_cfg.get("api_base", "")
        ),
        embedding_key=editor_cfg.get("embedding_api_key") or api_key,
        config_dir=test_home / "memento_s",
        workspace_dir=workspace,
        max_iterations=memento_phase_max_iterations("edit", 8),
        served_model_id=editor_cfg.get("served_id_hint"),
    )
    trace_cap = int(os.environ.get("EVO_MEMENTO_EDIT_TRACE_CHARS", "60000") or "60000")
    envelope = _MEMENTO_EDIT_ENVELOPE.format(
        skill_id=skill_id,
        trace_digest=_clip_middle(
            traces_digest,
            max_chars=trace_cap,
            label="memento edit trace digest",
        ),
    )
    edit_timeout_s = float(os.environ.get("EVO_MEMENTO_EDIT_TIMEOUT_S", "1800"))
    rc, stdout, stderr, session_ws = _invoke_memento(
        envelope,
        home=test_home,
        workspace_dir=workspace,
        skills_dir=skills_dir,
        timeout_s=edit_timeout_s,
    )
    skill_md = anchor_dir / "SKILL.md"
    if not skill_md.is_file():
        return skill_body, {
            "editor": "memento",
            "ok": False,
            "rc": rc,
            "stderr_tail": stderr[-2000:],
        }
    new_body = skill_md.read_text(encoding="utf-8", errors="replace")
    # Harvest the agent's real native output: it may edit the anchor in place, or (via
    # skill-creator) write new skill dirs into skills_dir or its session workspace. Fold
    # those genuine siblings into the anchor body so the round produces a scorable version.
    from evolution.adapters.skill_artifact_export import (
        collect_session_skill_dirs,
        fold_evolved_skills,
        list_evolved_skill_dirs,
    )

    harvested = list_evolved_skill_dirs(skills_dir, skill_id, since_ts=anchor_ts)
    harvested_names = {d.name for d in harvested}
    for d in collect_session_skill_dirs(session_ws):
        if d.name != skill_id and d.name not in harvested_names:
            harvested.append(d)
            harvested_names.add(d.name)
    # Memento's native evolution is the skill library its agent builds while
    # SOLVING (procedural memory), not an edit-agent rewrite. Harvest skills the
    # agent created during this round's train solves, persist them into the skill
    # library so later rounds solve with them, and fold the new ones into the body.
    solve_round = run_dir / "native_solver_cache" / f"round{round_idx}"
    for home_skills in sorted(solve_round.glob("**/memento_s/skills")):
        for d in list_evolved_skill_dirs(home_skills, skill_id):
            if d.name in harvested_names:
                continue
            harvested_names.add(d.name)
            dest = skills_dir / d.name
            if not dest.exists():
                shutil.copytree(d, dest)
            if f"## Evolved sub-skill: {d.name}" not in new_body:
                harvested.append(dest)
    new_body = fold_evolved_skills(new_body, harvested)
    native_changed = (
        hashlib.sha256(new_body.encode("utf-8")).hexdigest()
        != hashlib.sha256(skill_body.encode("utf-8")).hexdigest()
    )
    fallback_used = False
    fallback_meta: dict = {}
    if not native_changed or rc != 0:
        env_allow = os.environ.get("EVO_MEMENTO_ALLOW_DIRECT_EDIT_FALLBACK")
        allow_fallback = _cfg_bool(
            env_allow,
            default=_cfg_bool(
                editor_cfg.get("single_task_trace_fidelity"),
                default=False,
            ),
        )
        extracted = _extract_skill_md_candidate(stdout + "\n" + stderr, skill_id=skill_id)
        allow_stdout_fallback = _cfg_bool(
            os.environ.get("EVO_MEMENTO_ALLOW_STDOUT_SKILL_FALLBACK"),
            default=False,
        )
        if extracted and allow_stdout_fallback:
            new_body = extracted
            fallback_used = True
            fallback_meta = {"fallback": "memento_stdout_markdown"}
        elif allow_fallback:
            try:
                direct_timeout_s = float(
                    os.environ.get("EVO_MEMENTO_DIRECT_EDIT_TIMEOUT_S", "1800") or "1800"
                )

                async def _bounded_direct_edit() -> tuple[str, dict]:
                    return await asyncio.wait_for(
                        _direct_edit_skill_md(
                            skill_id=skill_id,
                            skill_body=skill_body,
                            traces_digest=traces_digest,
                            editor_cfg=editor_cfg,
                            api_key=api_key,
                            timeout_s=direct_timeout_s,
                        ),
                        timeout=max(1.0, direct_timeout_s) + 5.0,
                    )

                new_body, fallback_meta = asyncio.run(_bounded_direct_edit())
                fallback_used = True
                fallback_meta = {
                    "fallback": "direct_skill_md_rewrite",
                    "fallback_timeout_s": direct_timeout_s,
                    **fallback_meta,
                }
            except Exception as exc:  # noqa: BLE001
                fallback_meta = {
                    "fallback": "direct_skill_md_rewrite",
                    "fallback_error": repr(exc)[:500],
                }
        else:
            fallback_meta = {
                "fallback_suppressed": (
                    "memento_stdout_markdown" if extracted else "direct_skill_md_rewrite"
                )
            }
        if fallback_used and new_body.strip():
            skill_md.write_text(new_body, encoding="utf-8")
    _sync_test_home_registry(test_home)
    return new_body, {
        "editor": "memento",
        "ok": rc == 0 or fallback_used,
        "rc": rc,
        "native_changed": native_changed,
        "fallback_used": fallback_used,
        "harvested_skills": sorted(harvested_names),
        "harvested_count": len(harvested_names),
        **fallback_meta,
        "test_home": str(test_home),
        "session_workspace": str(session_ws or ""),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


__all__ = [
    "FRAMEWORK_TAG",
    "MEMENTO_RUNS_DIR",
    "MementoProducePlan",
    "memento_native_services_profile",
    "write_memento_native_trace",
    "edit_skill_from_traces",
    "produce",
    "write_llm_config",
    "_sync_test_home_registry",
]
