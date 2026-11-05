"""Agent-session ("full info + tools") editor for codex/claude on the TRAIN set.

Instead of handing the agent a redacted text digest (file-edit mode), this seeds a runnable
workspace with the actual TRAIN tasks (inputs + instruction + verifier `tests/` + `solve.sh`) and
gives the agent full tools (Bash/Read/Write/Edit/Glob). The agent can solve a train task itself,
run its verifier to validate a correct procedure, and distill that into `SKILL.md` — iterating with
real feedback. Only `SKILL.md` is lifted back. TEST tasks are never seeded; the tool-call FS audit
(`_audit_agent_events`) flags any read outside the workspace (dataset / held-out / other skills).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

_SESSION_SYSTEM = (
    "You improve one reusable Agent Skill (SKILL.md) so that a separate, weaker solver that follows "
    "it can reliably solve tasks of this kind. You have a runnable workspace with real train tasks."
)

_SESSION_PROMPT = """Improve the reusable skill in `./SKILL.md` for skill {skill_id!r}.

A separate (weaker) solver will be given ONLY your edited `SKILL.md` — not these tasks — and must
solve held-out tasks of the same kind. Your job: make `SKILL.md` a correct, general, reusable
procedure.

Your workspace `./tasks/` contains real TRAIN tasks (inputs, `instruction.md`, verifier `tests/`,
and `solve.sh`). You may, for any task:
- read `instruction.md` and the input files to understand the task,
- write `solution.py` and run `bash solve.sh` then run the task's `tests/` (pytest) to VALIDATE that
  a procedure actually works,
- iterate until you understand the correct, general procedure.

Then distill that procedure into `./SKILL.md`.

Rules:
- Edit ONLY `./SKILL.md`. Do not modify anything under `./tasks/`.
- Keep `SKILL.md` a valid Agent Skill: YAML frontmatter (name, description) then a markdown body.
  Do not change the `name`.
- Write GENERAL procedure, not task-specific answers: do NOT copy exact train filenames, schema keys,
  expected values, or test assertions into the skill — it must generalize to unseen tasks.
- Stay inside this workspace; do not read the broader dataset.
"""


def _task_dir(corpus_root: Path, role: str, ref: str) -> tuple[Path, str]:
    ref = str(ref)
    r, plain = ref.split("/", 1) if "/" in ref else (role, ref)
    return corpus_root / "tasks" / r / plain, ref.replace("/", "_")


def run_session_edit(
    *,
    framework: str,
    agent_model: str,
    skill_id: str,
    skill_body: str,
    run_dir: Path,
    editor_cfg: dict[str, Any],
    round_idx: int,
    role: str,
) -> tuple[str, dict[str, Any]]:
    from _shared.workspace_materializer import materialize_workspace

    from evolution.adapters.agent_cli_adapter import _audit_agent_events
    from evolution.llm.backend import get_backend

    corpus_root = Path(editor_cfg["corpus_root"]).resolve()
    train_ids = list(editor_cfg.get("train_ids") or [])
    work = (run_dir / f"{framework}_session_workspace" / f"round{round_idx}").resolve()
    work.mkdir(parents=True, exist_ok=True)
    skill_md = work / "SKILL.md"
    skill_md.write_text(skill_body, encoding="utf-8")

    materialized: list[str] = []
    for ref in train_ids:
        tdir, safe = _task_dir(corpus_root, role, ref)
        if not tdir.is_dir():
            continue
        try:
            materialize_workspace(tdir, work / "tasks" / safe, agent_solve_sh=True)
            materialized.append(safe)
        except Exception:  # noqa: BLE001 - a bad task shouldn't abort the edit
            continue

    audit_dir = run_dir / f"{framework}_session_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    events_out = audit_dir / f"round{round_idx}.events"

    timeout_s = editor_cfg.get("agent_timeout_s") or editor_cfg.get("timeout_s")
    budget_usd = editor_cfg.get("agent_budget_usd")
    prompt = _SESSION_PROMPT.format(skill_id=skill_id)
    backend = get_backend(agent_model)
    error = ""
    try:
        asyncio.run(
            backend.complete_agent_session(
                work,
                prompt,
                system=_SESSION_SYSTEM,
                budget_usd=float(budget_usd) if budget_usd else None,
                timeout_s=float(timeout_s) if timeout_s else None,
                events_out=events_out,
            )
        )
    except Exception as exc:  # noqa: BLE001 - degrade to no-op like the file-edit path
        error = str(exc)[:400]

    new_body = (
        skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.is_file() else skill_body
    )
    usage = getattr(backend, "last_usage", {}) or {}
    diag = getattr(backend, "last_codex_diagnostics", {}) or {}
    changed = new_body != skill_body
    telemetry = {
        "editor": framework,
        "edit_mode": "session",
        "agent_model": getattr(backend, "model_label", agent_model),
        "ok": changed and not error,
        "changed": changed,
        "n_train_tasks_seeded": len(materialized),
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "tool_turns": usage.get("tool_turns"),
        "wall_time_ms": usage.get("wall_time_ms"),
        "returncode": diag.get("returncode"),
        "stderr_tail": diag.get("stderr_tail"),
        "workspace": str(work),
        "fs_audit": _audit_agent_events(events_out, work),
    }
    if error:
        telemetry["error"] = error
    return (new_body if changed else skill_body), telemetry


__all__ = ["run_session_edit"]
