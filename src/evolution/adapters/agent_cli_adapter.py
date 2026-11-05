"""Agent-CLI skill editor (codex/claude) for the iterative evolution loop.

Mirrors the hermes-adapter role: an autonomous coding-agent CLI edits the anchor
SKILL.md from trace evidence, and the evolved text is scored by the shared
neutral solver (canonical-export lane). Phase 1 uses AgentBackend file-edit mode:
seed ``work_dir/SKILL.md`` -> the agent edits it in place -> read it back.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from evolution.llm.agent_backend import AgentBackendError
from evolution.llm.backend import get_backend

_SYSTEM = "You edit one reusable procedural skill file (SKILL.md) in place from execution traces."

_EDIT_ENVELOPE = (
    "You are improving a reusable Agent Skill stored as `SKILL.md` in your current "
    "working directory.\n\n"
    "Goal: edit `SKILL.md` in place so the next solver of skill {skill_id!r} avoids "
    "the failures shown in the trace evidence below and follows a more correct, "
    "reusable procedure.\n\n"
    "Rules:\n"
    "- Edit ONLY the file `SKILL.md`; do not create other files.\n"
    "- Keep it a valid Agent Skill: YAML frontmatter (name, description) then a "
    "markdown body. Do not change the `name`.\n"
    "- Fix root causes by revising guidance; preserve working procedure unless the "
    "evidence contradicts it.\n"
    "- Do NOT copy test code, exact train filenames, schema keys, benchmark paths, "
    "or task answers into the skill.\n\n"
    "## Trace Evidence\n"
    "{trace_digest}\n"
)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


# The editor agent should stay inside its seeded workspace (SKILL.md + any seeded train tasks).
# Audit is LOCATION-based: a tool-call path is flagged iff it resolves OUTSIDE work_dir — that's the
# only way to reach the dataset/verifier/held-out test/other skills. Benign = the agent's own
# runtime/config/library reads; system prefixes are ignored.
_AUDIT_BENIGN = re.compile(
    r"(?i)/\.(codex|claude|config|cache)/|/node_modules/|/site-packages/|/\.nvm/|/\.cache/|"
    r"/\.mlspace/|/(mini)?conda|/envs/[^/]+/bin/|/\.?venv/"  # the agent running the role interpreter/pip
)
_AUDIT_SYS_PREFIXES = (
    "/usr/",
    "/lib",
    "/bin/",
    "/sbin/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/etc/",
    "/run/",
)
_AUDIT_ACCESS_KEYS = {
    "command",
    "cmd",
    "file_path",
    "filepath",
    "path",
    "paths",
    "file",
    "files",
    "pattern",
    "glob",
    "dir",
}


def _collect_access_strings(obj: Any, out: list[str]) -> None:
    """Recursively pull tool-call file/command fields (not free text or written content)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _AUDIT_ACCESS_KEYS:
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    out.append(" ".join(str(x) for x in v))
            _collect_access_strings(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _collect_access_strings(x, out)


def _path_outside_workspace(token: str, wd: str) -> bool:
    tok = token.rstrip(".,;:)\"'")
    if _AUDIT_BENIGN.search(tok):
        return False
    if tok.startswith("/"):
        rp = os.path.normpath(tok)
        if any(rp.startswith(pref) for pref in _AUDIT_SYS_PREFIXES):
            return False
        return not (rp == wd or rp.startswith(wd + "/"))
    rp = os.path.normpath(os.path.join(wd, tok))  # relative -> resolve against workspace
    return not (rp == wd or rp.startswith(wd + "/"))


def _audit_agent_events(events_path: Path, work_dir: Path) -> dict[str, Any]:
    """Scan the agent's tool-call stream for file/command access OUTSIDE its workspace.

    `clean=True` means every tool-call path stayed inside ``work_dir`` (or was a benign agent-config /
    system read) — i.e. the agent never reached the dataset, held-out test tasks, the verifier, or
    other skills. Seeded train tasks under ``work_dir`` are in-workspace and allowed.
    """
    p = Path(events_path)
    if not p.is_file():
        return {"events_captured": False, "clean": None}
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return {"events_captured": False, "clean": None}
    wd = str(Path(work_dir).resolve())
    access: list[str] = []
    n_events = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        n_events += 1
        _collect_access_strings(ev, access)
    hits: set[str] = set()
    for s in access:
        for raw in s.split():
            tok = raw.strip("'\"`,;:()<>")
            if "/" in tok and _path_outside_workspace(tok, wd):
                hits.add(tok[:200])
    return {
        "events_captured": True,
        "events_path": str(p),
        "n_events": n_events,
        "n_access_strings": len(access),
        "suspicious_accesses": sorted(hits),
        "clean": not hits,
    }


def edit_skill_from_traces(
    *,
    framework: str,
    agent_model: str,
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
    """File-edit editor: an agent CLI rewrites the anchor SKILL.md in place."""
    _ = (bundles, api_key, role)
    workspace = run_dir / f"{framework}_edit_workspace" / f"round{round_idx}"
    workspace.mkdir(parents=True, exist_ok=True)
    skill_md = workspace / "SKILL.md"
    skill_md.write_text(skill_body, encoding="utf-8")

    # Agent tool-call stream is written OUTSIDE the agent's cwd (so it can't see/edit it) for the
    # out-of-workspace read audit.
    audit_dir = run_dir / f"{framework}_edit_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    events_out = audit_dir / f"round{round_idx}.events"

    trace_cap = _env_int("EVO_AGENT_EDIT_TRACE_CHARS", 60000)
    prompt = _EDIT_ENVELOPE.format(skill_id=skill_id, trace_digest=traces_digest[:trace_cap])

    timeout_s = editor_cfg.get("agent_timeout_s")
    backend = get_backend(agent_model)
    error = ""
    try:
        asyncio.run(
            backend.complete_file_edit(
                workspace,
                prompt,
                system=_SYSTEM,
                timeout_s=float(timeout_s) if timeout_s else None,
            )
        )
    except AgentBackendError as exc:
        # Agent failure (rc!=0/timeout) degrades to a no-op (parent kept), like hermes.
        error = str(exc)[:400]

    fs_audit = _audit_agent_events(events_out, workspace)

    new_body = (
        skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.is_file() else skill_body
    )
    usage = getattr(backend, "last_usage", {}) or {}
    diag = getattr(backend, "last_codex_diagnostics", {}) or {}
    changed = new_body != skill_body
    telemetry = {
        "editor": framework,
        "agent_model": getattr(backend, "model_label", agent_model),
        "ok": changed and not error,
        "changed": changed,
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "tool_turns": usage.get("tool_turns"),
        "wall_time_ms": usage.get("wall_time_ms"),
        "returncode": diag.get("returncode"),
        "stderr_tail": diag.get("stderr_tail"),
        "workspace": str(workspace),
        "fs_audit": fs_audit,
    }
    if error:
        telemetry["error"] = error
    return (new_body if changed else skill_body), telemetry


__all__ = ["edit_skill_from_traces"]
