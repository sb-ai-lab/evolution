"""LLM-driven trace reflection and skill rewriting.

This module is the compatibility entrypoint for the core lifecycle APIs:
reflection over failed traces, rewrite generation, and audited candidate
promotion. Sibling modules keep evidence handling, sanitization, guard checks,
and preservation scoring explicit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evolution.core.models import Skill, SkillFrontmatter
from evolution.core.parser import parse_skill_md, write_skill_md
from evolution.eval.evidence import sanitize_feedback
from evolution.evolution.meta_skill import build_meta_skill, build_rejected_edits_context
from evolution.evolution.preservation import score_preservation, split_skill_body_by_heading
from evolution.evolution.reflection import (
    REFLECT_PROMPT,
    REFLECT_SUCCESS_PROMPT,
    REFLECT_SYSTEM,
    _backend_label,
    collect_reflections,
    load_reflection,
    reflect_failing_traces,
    reflect_trace,
    save_reflection,
)
from evolution.evolution.rewrite_guards import (
    actionable_reflections,
    actionable_success_reflections,
    baseline_body_for_size,
    hard_size_violations,
    leakage_provenance_corpus,
    rewrite_size_limits,
    scan_leakage,
    size_violations,
    validate_candidate_isolation,
)
from evolution.evolution.rewrite_outcome import (
    CANDIDATE_EDIT_BUDGET_EXCEEDED,
    CANDIDATE_INVALID_FRONTMATTER_CORRUPT,
    CANDIDATE_INVALID_NEW_HEADING,
    LEAK_REJECTED,
    LOW_CONFIDENCE_REFLECTION,
    NO_OP_FILE_EDIT,
    NO_OP_TEXT,
    PROMOTED_ELIGIBLE,
    REWRITE_ERROR_BACKEND,
    REWRITE_ERROR_CODEX,
    SANITIZE_ERROR,
    SIZE_VIOLATION,
    RewriteOutcome,
)
from evolution.evolution.rewrite_sanitize import sanitize_rewritten_body
from evolution.evolution.session_workspace import (
    SessionWorkspaceError,
    build_session_workspace,
    cleanup_session_workspace,
)
from evolution.lineage.traces import TraceStore
from evolution.llm.agent_backend import AgentBackend, AgentBackendError
from evolution.llm.backend import LLMBackend
from evolution.splits import SplitManifest

if TYPE_CHECKING:
    from evolution.eval.task import Task
    from evolution.evolution.session_workspace import SessionWorkspace

logger = logging.getLogger(__name__)

__all__ = [
    "REFLECT_PROMPT",
    "REFLECT_SUCCESS_PROMPT",
    "REFLECT_SYSTEM",
    "collect_reflections",
    "load_reflection",
    "reflect_failing_traces",
    "reflect_trace",
    "rewrite_skill",
    "rewrite_skill_with_audit",
    "save_reflection",
]


REWRITE_SYSTEM = """You are an expert at writing procedural skill instructions for LLM agents.
A skill is a focused, reusable guide for a CLASS of tasks, not the answer to
one instance. Revise the skill so the underlying procedure becomes correct,
general, concise, and transferable.

Hard rules:
- Keep the skill a GENERAL, transferable procedure for this task type.
- NEVER embed the task's test harness, verifier, pytest assertions, expected
  answer, hardcoded dataset/table/column names, file paths, or literal expected
  values.
- Fix root causes by revising existing guidance, not by appending a transcript.
- Preserve baseline working procedure unless a reflection clearly contradicts it.

Output ONLY the new skill body (markdown, no frontmatter)."""

FILE_EDIT_SYSTEM = """You are editing a single file on disk: SKILL.md, in your current
working directory.  Use your file-edit tools to MODIFY THE FILE IN PLACE.
Do NOT print or return the file body in chat — the harness reads SKILL.md
from disk after you exit and ignores stdout entirely.

You MUST make at least one substantive, targeted edit informed by the
highest-severity actionable reflection below.  Stay scoped:

- Edit ONLY the section(s) that the reflection is about.
- Do NOT rewrite the whole body.
- Do NOT touch the YAML frontmatter (name, description, license, metadata).
- Do NOT embed test names, verifier code, expected literal answers, or
  task-specific identifiers (file paths, dataset names, hardcoded values).
  Keep the skill a GENERAL, transferable procedure.
- Prefer a small, surgical change over a sweeping rewrite.

If after reading the reflection you genuinely conclude that the parent
SKILL.md already addresses the failure correctly, you may leave the file
unchanged.  But your default disposition should be: the failing trace
exposed a gap in the skill; find the smallest edit that closes that gap."""

FILE_EDIT_PROMPT = """The file you are editing is `SKILL.md` in your current working directory.
Its current contents are reproduced below for your reference; the source
of truth is the file on disk.

## Current SKILL.md body (parent)
{skill_body}

## Task this skill must solve
{task_instruction}
{scripts_section}
## Highest-severity actionable reflection
This is the most important reflection to address with your edit:

{primary_reflection}

## Other actionable reflections ({n_other} total)
{other_reflections}

## Failure pattern summary
{pattern_summary}

## Size envelope (informational)
Keep the body within roughly {max_chars} characters and {max_lines}
non-empty lines.  The frontmatter is locked and will be reattached if you
modify it.

## Your task
Apply ONE targeted edit (or a small cluster of related edits in the same
section) that addresses the highest-severity reflection above.  Use your
file-edit tools to modify SKILL.md.  Do not output the body in chat."""


# Codex-only variants (t34v10 quality-spike prescription).  Used by the
# file-edit path because it requires :class:`AgentBackend` — the agent CLI
# has native file-edit tools + `git diff` self-review in the candidate
# workspace.  Other (future) file-edit backends can keep the simpler
# FILE_EDIT_SYSTEM / FILE_EDIT_PROMPT above without inheriting these
# codex-specific guards.
CODEX_FILE_EDIT_SYSTEM = """You are editing a single file on disk: SKILL.md, in your current
working directory.  Use your file-edit tools to MODIFY THE FILE IN PLACE.
Do NOT print or return the file body in chat — the harness reads SKILL.md
from disk after you exit and ignores stdout entirely.

The candidate workspace is a tiny git repo at `baseline` state. You may
use `git diff -- SKILL.md` to self-review your edits.

You MUST make at least one substantive, targeted edit informed by the
consensus failure pattern below.  Stay scoped:

- Edit ONLY one existing section that the consensus failure is about.
- Do NOT add new top-level (## or ###) headings.  The harness rejects any
  candidate that introduces a new section.
- Do NOT delete or wholesale-replace any existing section.
- Do NOT touch the YAML frontmatter (name, description, license, metadata).
- Do NOT embed test names, verifier code, expected literal answers, or
  task-specific identifiers (file paths, dataset names, hardcoded values).
  Keep the skill a GENERAL, transferable procedure.
- Prefer a small, surgical change over a sweeping rewrite.  Edit churn is
  capped; broad rewrites are rejected pre-gate.

If after reading the reflections you genuinely conclude that the parent
SKILL.md already addresses the failure correctly, you may leave the file
unchanged.  But your default disposition should be: the failing traces
exposed a gap in the skill; find the smallest edit that closes that gap."""

CODEX_FILE_EDIT_PROMPT = """The file you are editing is `SKILL.md` in your current working directory.
Its current contents are reproduced below for your reference; the source
of truth is the file on disk.

## Current SKILL.md body (parent)
{skill_body}

## Preserve these workflows unless the reflections directly contradict them
The parent SKILL.md has these top-level sections.  Do NOT add new top-level
sections (no new `## Heading` lines), and do NOT delete or rewrite an
entire existing section — targeted edits within ONE existing section only:

{preserve_headings}

## Training context
- Task: {task_instruction}
- Source model(s): {source_models}
- Failing traces analysed: {n_traces}
{scripts_section}
## Consensus failure
{consensus_summary}

## All actionable reflections ({n_reflections})
{all_reflections}

## Failure pattern summary
{pattern_summary}
{success_preserve_section}
## Size envelope (HARD)
Keep the body within {max_chars} characters and {max_lines} non-empty
lines.  Edit churn is also capped: if you change more than the configured
fraction of non-empty lines, the harness rejects the candidate.

## Your task
Apply ONE targeted edit (or a small cluster of related edits in ONE
existing section) that addresses the consensus failure above.  Use your
file-edit tools to modify SKILL.md.  Do not output the body in chat."""


# ── agentic-session rewrite (EVO_REWRITE_MODE=agentic-session) ──────────────
# One genuine multi-turn coding-agent session per evolve round replaces the
# two-step reflect→single-shot-edit path. The agent explores raw train traces,
# diagnoses the recurring gap, edits SKILL.md in place, and self-validates by
# writing+running a candidate solve and reading the aggregate score. Bounded by
# --max-budget-usd + wall-clock timeout (NOT a turn counter on this host's
# claude binary). The leak scanner on the lifted SKILL.md stays the final gate.

_SESSION_SYSTEM_COMMON = """You are evolving a reusable PROCEDURAL skill (SKILL.md) — a general
guide for a CLASS of tasks, NOT the answer to one task instance. You work in a
real workspace with file and shell tools; the harness reads SKILL.md from disk
after you exit and ignores everything you print in chat.

Off-limits — never open, list, or read, for any reason:
- ../_verifier, any `tests/` directory, any `*ground_truth*` file, `output/`.
You have no business there; the score you need is exposed via `measure.sh`.

You MAY:
- Read every `traces/<id>/` (the agent's own `solve.py`, `result.json`,
  `evidence.md`) to diagnose the recurring procedural gap.
- Read the task inputs and `SKILL.md`.
- Write `solution.py`, run `bash solve.sh`, then `bash measure.sh` to read an
  aggregate `passed/total` self-validation score (and ONLY that).

Hard rules for the SKILL.md you leave on disk:
- Keep it GENERAL and transferable. NEVER embed test/verifier code, expected
  values, dataset/table/column names, file paths, or literal answers — a leak
  scanner discards any rewrite that does, and the round is wasted.
- Restructure however best closes the gap — revise existing sections, add new
  ones, or reorganize. Keep working guidance the traces did not implicate; fix
  root causes rather than regenerate the skill from scratch. Stay within the
  size and churn budget (broad rewrites are rejected before scoring).
- Do NOT touch the YAML frontmatter.
- Do not append a transcript or a changelog."""

SESSION_SYSTEM_BLIND = (
    _SESSION_SYSTEM_COMMON
    + """

You are in BLIND mode: the trace `evidence.md` files contain only sanitized,
execution-observable signals (no verifier source, no expected values). Diagnose
from the agent's own code + that evidence. This is the paper-faithful setting."""
)

SESSION_SYSTEM_ORACLE = (
    _SESSION_SYSTEM_COMMON
    + """

The trace `evidence.md` files may include verifier feedback (failed-test names,
tracebacks) for diagnosis ONLY. Use them to localize the exact skill gap, then
describe the fix as a GENERAL procedure. NEVER copy any literal, expected value,
test name, or assertion text into SKILL.md — the leak scanner discards it."""
)

SESSION_PROMPT = """## Skill evolution session{round_banner}

You are improving the procedural skill `SKILL.md` in your working directory so
that future agents solve this CLASS of task more reliably.
{round_context}{rejected_context}
## The task class this skill must serve
{task_instruction}

## Sections in the current SKILL.md (preserve these; edit within them)
{preserve_headings}

## Train traces to diagnose
There are {n_traces} train trace(s) under `traces/`, from source model(s):
{source_models}. Each `traces/<id>/` has `solve.py` (the code that model
produced under the parent skill), `result.json` (scalar pass/total), and
`evidence.md` (sanitized failure signal). These are the procedural-memory
traces you are learning from.

## Round protocol — follow exactly
Step 1 — Diagnose. Read EVERY `traces/<id>/` (all three files). Form a single
  CONSENSUS diagnosis of the recurring procedural gap across the traces — not a
  per-trace patch. A gap that appears in one idiosyncratic trace is weaker
  signal than one repeated across traces/models.
Step 2 — Edit. Improve SKILL.md so the gap is closed as a GENERAL procedure —
  revise, add, or reorganize sections however fits best. Keep working guidance
  the traces did not implicate; fix root causes rather than regenerate from
  scratch. Edit the file in place. If the parent SKILL.md already handles the
  gap, leave it unchanged.
Step 3 — Record your decision NOW. Write `DIAGNOSIS.json` (keys below) to the
  working directory IMMEDIATELY, before any self-validation. SKILL.md and
  DIAGNOSIS.json are your PRIMARY deliverables — the harness lifts them from
  disk, so they must already exist if you later run out of time.
  `rewrite_recommended` is coupled to your EDIT, not to your opinion: set it
  `true` if you changed SKILL.md in any way, and `false` ONLY when you left
  SKILL.md byte-for-byte identical to the parent (because the parent already
  closes the gap). Editing SKILL.md AND setting `rewrite_recommended:false` is a
  contradiction — the harness then discards your edit and keeps the buggy
  parent, wasting the session. If your diagnosis is `severity:critical`/`major`
  with `evidence_sufficiency:sufficient`, you found a real gap: edit the file and
  set `rewrite_recommended:true`.
Step 4 — Self-validate ONLY if budget remains (AT MOST 2 short cycles). Write
  `solution.py` from ONLY your edited SKILL.md + the task instruction (you are
  blind to the tests), run `bash solve.sh`, then `bash measure.sh`, and read
  ONLY the aggregate `passed/total`. The deliverable is a better GENERAL skill,
  NOT a passing solution — do NOT keep iterating to make one task instance pass.
  A higher self-score earned by specializing to THESE traces will be REJECTED:
  the harness re-scores the parent vs. your skill on held-back cases you never
  see, and keeps the parent unless your skill wins THERE. If a cycle reveals a
  clear, general refinement, make the smallest edit and UPDATE `DIAGNOSIS.json`
  (`best_self_score`, `rounds_self_run`). Stop as soon as you are out of
  evidence-grounded hypotheses or budget.

## DIAGNOSIS.json keys (EXACTLY these)
  {{"consensus_pattern": str, "root_cause": str, "skill_gap": str,
    "suggested_fix": str, "severity": "critical|major|minor|info",
    "evidence_sufficiency": "sufficient|insufficient",
    "rewrite_recommended": bool,  // true iff you changed SKILL.md
    "rounds_self_run": int, "best_self_score": "passed/total"}}
Do NOT print SKILL.md or its body in chat — only the files on disk matter.

## Size envelope (HARD)
Keep the body within {max_chars} characters and {max_lines} non-empty lines.
Edit churn is capped; broad rewrites are rejected before scoring."""


_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def _format_primary_reflection(r: dict) -> str:
    """Render a single reflection (used by tests / back-compat)."""
    return (
        f"- severity: {r.get('severity', '?')}\n"
        f"- pattern: {r.get('pattern', '?')}\n"
        f"- root_cause: {r.get('root_cause', '?')}\n"
        f"- skill_gap: {r.get('skill_gap', '?')}\n"
        f"- suggested_fix: {r.get('suggested_fix', '?')}"
    )


def _highest_severity_reflection(reflections: list[dict]) -> dict:
    """Pick the most severe actionable reflection.  Stable tiebreak by list order.

    Retained for back-compat with tests; the file-edit path now uses
    :func:`_consensus_reflection` instead (memory feedback_evolve_signal_per_family).
    """
    if not reflections:
        return {}

    def _key(r: dict) -> tuple[int, int]:
        sev = str(r.get("severity") or "minor").lower()
        return (_SEVERITY_RANK.get(sev, 99), 0)

    return sorted(reflections, key=_key)[0]


def _consensus_reflection(reflections: list[dict]) -> dict:
    """Pick the reflection representing the most common (pattern) cluster.

    Ranking: frequency of same `pattern` > severity > original trace order.
    Memory feedback_evolve_signal_per_family — one idiosyncratic critical
    reflection should not dominate the rewrite when 3/3 traces share a
    different repeated pattern.
    """
    if not reflections:
        return {}
    pattern_counts: Counter[str] = Counter(str(r.get("pattern") or "unknown") for r in reflections)

    def _key(idx_r: tuple[int, dict]) -> tuple[int, int, int]:
        idx, r = idx_r
        pat = str(r.get("pattern") or "unknown")
        sev = str(r.get("severity") or "minor").lower()
        return (-pattern_counts[pat], _SEVERITY_RANK.get(sev, 99), idx)

    return sorted(enumerate(reflections), key=_key)[0][1]


def _consensus_summary(reflections: list[dict]) -> str:
    """One-paragraph 'N/M traces failed because <pattern>' summary."""
    if not reflections:
        return "(no actionable reflections)"
    n = len(reflections)
    patterns: Counter[str] = Counter(str(r.get("pattern") or "unknown") for r in reflections)
    top_pattern, top_count = patterns.most_common(1)[0]
    rep = _consensus_reflection(reflections)
    root_cause = str(rep.get("root_cause") or "(no root_cause)")
    skill_gap = str(rep.get("skill_gap") or "(no skill_gap)")
    suggested_fix = str(rep.get("suggested_fix") or "(no suggested_fix)")
    if top_count == n and n > 1:
        head = f"All {n}/{n} traces share pattern `{top_pattern}`."
    elif top_count > 1:
        head = f"{top_count}/{n} traces share pattern `{top_pattern}`; the rest differ."
    else:
        head = f"Each of {n} traces failed differently; representative pattern `{top_pattern}`."
    return (
        f"{head}\n"
        f"- Representative root_cause: {root_cause}\n"
        f"- Skill gap to close: {skill_gap}\n"
        f"- Suggested fix: {suggested_fix}"
    )


def _format_all_actionable_reflections(reflections: list[dict]) -> str:
    """Render every actionable reflection in full (root_cause + skill_gap + suggested_fix)."""
    if not reflections:
        return "(none)"
    lines: list[str] = []
    for i, r in enumerate(reflections, 1):
        sev = r.get("severity", "?")
        pat = r.get("pattern", "?")
        lines.append(f"### Reflection {i}  (severity: {sev}, pattern: `{pat}`)")
        lines.append(f"- root_cause: {r.get('root_cause', '?')}")
        lines.append(f"- skill_gap: {r.get('skill_gap', '?')}")
        lines.append(f"- suggested_fix: {r.get('suggested_fix', '?')}")
    return "\n".join(lines)


def _source_models_summary(reflections: list[dict]) -> str:
    """Dedup'd source_model_id list pulled from each reflection's trace_context."""
    models: list[str] = []
    for r in reflections:
        ctx = r.get("trace_context") or {}
        mid = ctx.get("source_model_id") or r.get("model")
        if mid and mid not in models:
            models.append(str(mid))
    return ", ".join(models) if models else "(unknown)"


def _parent_top_headings(body: str) -> list[str]:
    """Return parent's H2+H3 heading lines (verbatim, stripped) for the preserve list."""
    out: list[str] = []
    for heading, _section_body in split_skill_body_by_heading(body):
        h = heading.strip()
        if not h or h.startswith("name:"):
            continue
        out.append(h)
    return out


def _candidate_added_new_heading(parent_body: str, candidate_body: str) -> tuple[bool, list[str]]:
    """Detect any candidate heading (``##``/``###``/``####``/…) that does not
    fuzzy-match a parent heading.

    Fuzzy match: SequenceMatcher.ratio() >= 0.85 on lowercased+stripped
    heading text (same threshold as preservation.heading_retention).  Returns
    ``(added, list_of_new_headings)``.  Matches every heading level ≥ ``##``
    (i.e., ``^##+ ``) — adding a new ``#### Sub-step`` under an existing
    section still counts as a new heading.  An empty parent heading list
    permits anything (back-compat: skills without any ``##+`` heading are
    unconstrained).
    """
    parent_headings = _parent_top_headings(parent_body)
    if not parent_headings:
        return (False, [])
    candidate_headings = _parent_top_headings(candidate_body)
    parent_norm = [h.strip().lower() for h in parent_headings]
    added: list[str] = []
    for ch in candidate_headings:
        ch_norm = ch.strip().lower()
        if any(SequenceMatcher(None, ch_norm, ph_norm).ratio() >= 0.85 for ph_norm in parent_norm):
            continue
        added.append(ch)
    return (bool(added), added)


def _git_init_candidate(work_dir: Path, *, timeout_s: float = 10.0) -> bool:
    """Best-effort: initialise candidate dir as a tiny git repo so codex's
    in-loop ``git diff`` self-review works.  Failures are non-fatal
    (returns False); codex's git diff just exits non-zero as before.
    """
    if not shutil.which("git"):
        return False
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    cmds = (
        ["git", "init", "-q", "-b", "baseline"],
        ["git", "-c", "user.email=evo@local", "-c", "user.name=evo", "add", "SKILL.md"],
        [
            "git",
            "-c",
            "user.email=evo@local",
            "-c",
            "user.name=evo",
            "commit",
            "-q",
            "-m",
            "baseline",
        ],
    )
    try:
        for cmd in cmds:
            r = subprocess.run(
                cmd,
                cwd=str(work_dir),
                env=env,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
            if r.returncode != 0:
                logger.debug(
                    "git init candidate failed at %s: rc=%s stderr=%s",
                    cmd[1] if len(cmd) > 1 else cmd[0],
                    r.returncode,
                    (r.stderr or b"")[:200],
                )
                return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git init candidate exception: %s", exc)
        return False
    return True


REWRITE_PROMPT = """## Current Skill
{skill_body}

## Task This Skill Must Solve
{task_instruction}
{scripts_section}
## Failure Reflections (from {n_reflections} failing traces)
{reflections}

## Failure Pattern Summary
{pattern_summary}
{success_preserve_section}{best_trace_section}
## Size Budget (HARD)
- Maximum characters: {max_chars}
- Maximum non-empty lines: {max_lines}
Stay within this budget. Be concise: revise, do not accrete.

## Rewrite Instructions
Rewrite the skill body. Key rules:
- Keep the skill a GENERAL, transferable procedure for this TYPE of task. The
  instruction above is ONE example; the skill must work on unseen instances.
- Fix ROOT CAUSES by correcting libraries/APIs, steps, and general guardrails.
- Show exact imports and API calls as GENERAL patterns parameterized over inputs.
- DO NOT embed or paraphrase the task's tests/verifier/assertions, the expected
  answer, specific dataset/table/column names, or input file paths.
- You MAY include short general pitfalls from the reflections.
- Output ONLY the markdown body (no YAML frontmatter, no code fences around the whole output)"""

COMPACT_SYSTEM = """You are an expert at editing procedural skill instructions.
Consolidate an oversized skill draft into a concise, non-contradictory skill body.
Output ONLY the compacted skill body (markdown, no frontmatter)."""

COMPACT_PROMPT = """## Baseline Skill Body
{baseline_body}

## Oversized Draft Rewrite
{draft_body}

## Failure Fixes That Must Remain Covered
{reflection_summary}

## Size Limits
- Maximum characters: {max_chars}
- Maximum non-empty lines: {max_lines}

## Compacting Rules
- Preserve the baseline's core successful procedure unless a reflection explicitly contradicts it
- Fold repeated warnings into one short checklist
- Remove redundant examples and keep only the most general working code pattern
- Remove speculative, task-specific, or contradictory advice
- Strip any embedded test/verifier/assertion text, hardcoded dataset/table/column
  names, private file paths, literal expected answers, or one-off constants
- Keep enough exact library/API details for an LLM to execute the procedure
- Output ONLY the compacted markdown body"""


def _collect_scripts(skill: Skill, max_chars: int = 15000) -> str:
    """Collect scripts/ content from the skill directory."""
    scripts_dir = skill.path / "scripts"
    if not scripts_dir.is_dir():
        return ""

    parts = []
    total = 0
    for f in sorted(scripts_dir.iterdir()):
        if not f.is_file() or f.suffix not in (".py", ".sh", ".md", ".txt"):
            continue
        content = f.read_text(encoding="utf-8", errors="replace")
        if total + len(content) > max_chars:
            break
        parts.append(f"### {f.name}\n```{f.suffix.lstrip('.')}\n{content}\n```")
        total += len(content)

    if not parts:
        return ""
    return (
        "\n\n## Skill Scripts (working reference code from the skill directory)\n\n"
        + "\n\n".join(parts)
    )


def _find_best_trace(skill: Skill, task_filter: str | None = None) -> tuple[dict, str] | None:
    """Find the best-performing trace and return (result_dict, code)."""
    ts = TraceStore(skill)
    traces = ts.list(task=task_filter)
    if not traces:
        return None

    best = max(traces, key=lambda t: t.get("pass_rate", 0))
    if best.get("pass_rate", 0) == 0:
        return None

    code_path = ts.traces_dir / best["id"] / "solve.py"
    if not code_path.exists():
        return None
    return best, code_path.read_text(encoding="utf-8")


def _sanitize_reflections_for_rewriter(reflections: list[dict]) -> list[dict]:
    """Strip oracle literals from reflection free-text before the rewriter sees it.

    Only active in oracle-visible mode (``EVO_REFLECT_BLIND != "1"``): the
    reflector may have quoted an expected value / test literal in its prose, and
    the rewriter must never copy that into the skill body (rev J1). Blind-mode
    reflections were generated without oracle access, so they pass through
    untouched. On-disk reflections stay verbatim — only the in-memory copy fed to
    the rewrite prompt is sanitized.
    """
    if os.environ.get("EVO_REFLECT_BLIND") == "1":
        return reflections
    out: list[dict] = []
    for r in reflections:
        if not isinstance(r, dict):
            out.append(r)
            continue
        rr = dict(r)
        for key in ("root_cause", "skill_gap", "suggested_fix"):
            if rr.get(key):
                rr[key] = sanitize_feedback(str(rr[key]))
        out.append(rr)
    return out


def _format_reflections(reflections: list[dict]) -> str:
    text = ""
    for i, r in enumerate(reflections, 1):
        if "error" in r:
            continue
        text += (
            f"\n### Failure {i} "
            f"(model: {r.get('model', '?')}, pass_rate: {r.get('pass_rate', 0):.0%})\n"
        )
        text += f"- **Root cause**: {r.get('root_cause', '?')}\n"
        text += f"- **Skill gap**: {r.get('skill_gap', '?')}\n"
        text += f"- **Suggested fix**: {r.get('suggested_fix', '?')}\n"
        text += f"- **Pattern**: {r.get('pattern', '?')}\n"
    return text


def _format_success_preserve(reflections: list[dict]) -> str:
    """Render success-kind reflections as a 'preserve these sections' block.

    Returns ``""`` when no actionable successes; otherwise a
    ``## Success Patterns to Preserve`` block listing helpful sections + the
    reinforcement pattern from each success reflection.
    """
    successes = actionable_success_reflections(reflections)
    if not successes:
        return ""
    lines: list[str] = ["", "## Success Patterns to Preserve"]
    for i, r in enumerate(successes, 1):
        helpful = r.get("helpful_sections") or []
        if isinstance(helpful, list):
            sections = ", ".join(str(h) for h in helpful if h) or "(unspecified)"
        else:
            sections = str(helpful)
        pat = r.get("reinforcement_pattern", "?")
        transfer = r.get("transferability", "?")
        lines.append(
            f"- Success {i} (pattern={pat}, transferability={transfer}): "
            f"keep `{sections}` substantially intact."
        )
    lines.append("")
    return "\n".join(lines)


def _pattern_summary(reflections: list[dict]) -> str:
    patterns = Counter(r.get("pattern", "unknown") for r in reflections if "error" not in r)
    summary = "| Pattern | Count |\n|---------|-------|\n"
    for pat, cnt in patterns.most_common():
        summary += f"| {pat} | {cnt} |\n"
    return summary


def _best_trace_section(skill: Skill, task_filter: str | None, include_code: bool) -> str:
    if not include_code:
        return ""
    best = _find_best_trace(skill, task_filter=task_filter)
    if not best:
        return ""
    result, code = best
    return (
        f"\n## Best Trace (for reference - {result.get('passed', 0)}/{result.get('total', 0)} "
        f"passed, model: {result.get('model', '?')})\n"
        "This code partially worked. Use it as a starting point for the approach.\n"
        f"```python\n{code}\n```\n"
    )


def _reject_leaked_rewrite(skill: Skill, body: str, corpus: str) -> bool:
    if os.environ.get("EVO_DISABLE_LEAK_SCANNER", "").strip().lower() in {"1", "true", "yes"}:
        return False
    findings = scan_leakage(body, corpus)
    if not findings:
        return False
    logger.warning(
        "rewrite for %s leaked train-specific content (%s); keeping parent version",
        skill.name,
        "; ".join(findings[:5]),
    )
    return True


async def _compact_oversized_rewrite(
    backend: LLMBackend,
    skill: Skill,
    draft_body: str,
    reflections: list[dict],
    limits: dict[str, int],
) -> str:
    reflection_summary = ""
    for i, r in enumerate(reflections, 1):
        if "error" in r:
            continue
        reflection_summary += (
            f"\n{i}. Pattern={r.get('pattern', '?')}; "
            f"gap={r.get('skill_gap', '?')}; fix={r.get('suggested_fix', '?')}"
        )
    prompt = COMPACT_PROMPT.format(
        baseline_body=baseline_body_for_size(skill, skill.body),
        draft_body=draft_body,
        reflection_summary=reflection_summary.strip() or "(none)",
        max_chars=limits["max_chars"],
        max_lines=limits["max_lines"],
    )
    resp = await backend.complete([{"role": "user", "content": prompt}], system=COMPACT_SYSTEM)
    return sanitize_rewritten_body(resp.content, skill.body)


async def _rewrite_skill_text_return_impl(
    backend: LLMBackend,
    skill: Skill,
    reflections: list[dict],
    task_instruction: str,
    task_filter: str | None,
    manifest: SplitManifest | None,
    include_best_trace_code: bool,
) -> tuple[str, str, str, int]:
    """Run the historical text-return rewrite path and classify the outcome.

    Returns ``(body, candidate_status, reason, tokens_used)``.  May raise on backend
    failure; :func:`rewrite_skill_with_audit` is responsible for catching
    those and mapping them to ``rewrite_error_backend``.
    """
    parent_body = skill.body

    if manifest is not None and task_filter:
        manifest.assert_no_leakage(
            [task_filter],
            scope="skill_evolution",
            context="rewrite_skill(task_filter)",
        )

    success_preserve = _format_success_preserve(reflections)
    selected = actionable_reflections(reflections)
    if selected is None:
        return (
            parent_body,
            LOW_CONFIDENCE_REFLECTION,
            "fewer than half of reflections were actionable",
            0,
        )
    reflections = _sanitize_reflections_for_rewriter(selected)

    reflection_text = _format_reflections(reflections)
    if not reflection_text.strip():
        return (
            parent_body,
            LOW_CONFIDENCE_REFLECTION,
            "no valid reflections to use for rewriting",
            0,
        )

    limits = rewrite_size_limits(skill, skill.body)
    prompt = REWRITE_PROMPT.format(
        skill_body=skill.body,
        task_instruction=task_instruction or "(not provided)",
        scripts_section=_collect_scripts(skill),
        n_reflections=len(reflections),
        reflections=reflection_text,
        pattern_summary=_pattern_summary(reflections),
        success_preserve_section=success_preserve,
        best_trace_section=_best_trace_section(
            skill,
            task_filter=task_filter,
            include_code=include_best_trace_code,
        ),
        max_chars=limits["max_chars"],
        max_lines=limits["max_lines"],
    )

    resp = await backend.complete([{"role": "user", "content": prompt}], system=REWRITE_SYSTEM)
    tokens_used = int(
        getattr(resp, "total_tokens", 0)
        or ((getattr(resp, "tokens_in", 0) or 0) + (getattr(resp, "tokens_out", 0) or 0))
        or 0
    )
    try:
        new_body = sanitize_rewritten_body(resp.content, skill.body)
    except ValueError as exc:
        return parent_body, SANITIZE_ERROR, f"sanitize_rewritten_body: {exc!s}"[:300], tokens_used

    corpus = leakage_provenance_corpus(skill, task_filter)
    if _reject_leaked_rewrite(skill, new_body, corpus):
        return (
            parent_body,
            LEAK_REJECTED,
            "leakage scanner flagged train-specific content",
            tokens_used,
        )

    issues = size_violations(new_body, limits)
    if issues:
        logger.warning(
            "rewrite for %s exceeded size cap (%s); requesting one compaction pass",
            skill.name,
            "; ".join(issues),
        )
        try:
            new_body = await _compact_oversized_rewrite(
                backend, skill, new_body, reflections, limits
            )
        except ValueError as exc:
            return parent_body, SANITIZE_ERROR, f"compaction sanitize: {exc!s}"[:300], tokens_used
        issues = size_violations(new_body, limits)
        if issues:
            hard = hard_size_violations(new_body, limits)
            if hard:
                return (
                    parent_body,
                    SIZE_VIOLATION,
                    "exceeds absolute size ceiling after compaction: " + "; ".join(hard),
                    tokens_used,
                )
            logger.warning(
                "rewrite for %s still over derived cap after compaction (%s) "
                "but within absolute ceiling (%s chars/%s lines); accepting compacted body",
                skill.name,
                "; ".join(issues),
                limits["hard_max_chars"],
                limits["hard_max_lines"],
            )

    if _reject_leaked_rewrite(skill, new_body, corpus):
        return (
            parent_body,
            LEAK_REJECTED,
            "leakage scanner flagged content after compaction",
            tokens_used,
        )

    if new_body == parent_body:
        return parent_body, NO_OP_TEXT, "rewrite returned a body byte-equal to parent", tokens_used

    return new_body, PROMOTED_ELIGIBLE, "text-return rewrite produced a candidate", tokens_used


def _candidate_root() -> Path:
    """Resolve the temp root under which per-row candidate dirs live."""
    return Path(os.environ.get("EVO_C1_CANDIDATE_ROOT") or "/tmp")


def _non_empty_line_churn(parent_body: str, candidate_body: str) -> float:
    """Edit-budget churn as a fraction of parent non-empty lines."""
    p_lines = [ln for ln in parent_body.splitlines() if ln.strip()]
    c_lines = [ln for ln in candidate_body.splitlines() if ln.strip()]
    if not p_lines:
        return 1.0 if c_lines else 0.0
    opcodes = SequenceMatcher(None, p_lines, c_lines).get_opcodes()
    churn = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "insert":
            churn += j2 - j1
        elif tag == "delete":
            churn += i2 - i1
        elif tag == "replace":
            churn += max(i2 - i1, j2 - j1)
    return churn / len(p_lines)


_FENCE_RE = re.compile(r"```[\w+-]*[ \t]*\n(.*?)\n```", re.DOTALL)


def _extract_fenced_candidate(last_message: str, parent_body: str) -> str | None:
    """Recover a candidate body when codex answered in chat-mode.

    Mirrors the ``fenced_candidate.SKILL.md`` workflow (docs/codex_usage.md):
    when codex returns a fenced markdown block instead
    of editing the workspace file, the largest block usually IS the intended
    candidate. Strips an optional YAML frontmatter so the returned string is
    a pure body. Returns ``None`` when no usable candidate is recoverable.
    """
    if not last_message:
        return None
    blocks = _FENCE_RE.findall(last_message)
    if not blocks:
        return None
    payload = max(blocks, key=len).strip("\n")
    if payload.startswith("---\n") or payload.startswith("---\r\n"):
        end = payload.find("\n---", 4)
        if end != -1:
            payload = payload[end + 4 :].lstrip("\n")
    payload = payload.strip()
    if not payload or payload == parent_body.strip():
        return None
    return payload


async def _rewrite_skill_file_edit_impl(
    backend: LLMBackend,
    skill: Skill,
    reflections: list[dict],
    task_instruction: str,
    task_filter: str | None,
    manifest: SplitManifest | None,
    include_best_trace_code: bool,
    *,
    audit_root: Path | None,
    row_id: str,
    debug_archive: bool,
    model_id: str,
) -> RewriteOutcome:
    """Run the C1 file-edit rewrite path and return a full RewriteOutcome.

    Per plan §1.5 — flow numbered to match the plan steps in order.
    """

    parent_body = skill.body

    # Provenance fields populated regardless of which branch we hit so the
    # audit JSON tells postmortem analysis exactly which parent the guards
    # ran against. Avoids the "compared to wrong _roots version" hazard.
    _budget_env = os.environ.get("EVO_C1_EDIT_BUDGET_FRAC")
    try:
        _budget_frac = float(_budget_env) if _budget_env else None
    except ValueError:
        _budget_frac = None
    base_prov: dict[str, Any] = {
        "parent_skill_path": str(skill.skill_md_path.resolve()),
        "parent_body_sha256": hashlib.sha256(parent_body.encode("utf-8")).hexdigest(),
        "edit_budget_frac": _budget_frac,
    }

    # 0. Fail-fast if backend is not an agent CLI.  Memory feedback_no_silent_fallbacks:
    #    file-edit mode requires an AgentBackend; we do not silently degrade.
    if not isinstance(backend, AgentBackend):
        raise RuntimeError(
            f"EVO_REWRITE_MODE=file-edit requires an AgentBackend; got {type(backend).__name__}"
        )

    # 1. low-confidence check (same threshold as text-return).
    if manifest is not None and task_filter:
        manifest.assert_no_leakage(
            [task_filter],
            scope="skill_evolution",
            context="rewrite_skill_with_audit(file_edit,task_filter)",
        )
    success_preserve = _format_success_preserve(reflections)
    selected = actionable_reflections(reflections)
    if selected is None:
        return RewriteOutcome(
            body=parent_body,
            candidate_status=LOW_CONFIDENCE_REFLECTION,
            reason="fewer than half of reflections were actionable",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            **base_prov,
        )
    reflections = _sanitize_reflections_for_rewriter(selected)

    reflection_text = _format_reflections(reflections)
    if not reflection_text.strip():
        return RewriteOutcome(
            body=parent_body,
            candidate_status=LOW_CONFIDENCE_REFLECTION,
            reason="no valid reflections to use for rewriting",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            **base_prov,
        )

    # 2. Candidate workspace seeded with parent SKILL.md.
    candidate_dir = _candidate_root() / f"evo_c1_{uuid.uuid4().hex}"
    candidate_dir.mkdir(parents=True, exist_ok=False)
    candidate_skill_path = candidate_dir / "SKILL.md"
    shutil.copy2(skill.skill_md_path, candidate_skill_path)

    # 2b. Best-effort git init so codex can use `git diff` to self-review.
    _git_init_candidate(candidate_dir)

    # 3. Build prompt — consensus-first ranking + full reflections + parent
    #    headings as preserve list (memories feedback_evolve_signal_per_family,
    #    project_evolution_regenerates_not_refines).
    limits = rewrite_size_limits(skill, skill.body)
    parent_headings = _parent_top_headings(skill.body)
    preserve_text = "\n".join(f"- {h}" for h in parent_headings) if parent_headings else "(none)"
    prompt = CODEX_FILE_EDIT_PROMPT.format(
        skill_body=skill.body,
        preserve_headings=preserve_text,
        task_instruction=task_instruction or "(not provided)",
        source_models=_source_models_summary(reflections),
        n_traces=len(reflections),
        scripts_section=_collect_scripts(skill),
        consensus_summary=_consensus_summary(reflections),
        n_reflections=len(reflections),
        all_reflections=_format_all_actionable_reflections(reflections),
        pattern_summary=_pattern_summary(reflections),
        success_preserve_section=success_preserve,
        max_chars=limits["max_chars"],
        max_lines=limits["max_lines"],
    )
    # _best_trace_section() / include_best_trace_code is intentionally unused
    # here — file-edit mode hides raw best-trace code from the prompt to keep
    # the leak surface minimal.
    if include_best_trace_code:
        logger.debug("file-edit mode ignores include_best_trace_code=True (leak surface)")

    # 4. codex exec in a caller-seeded candidate dir.  The backend chooses
    #    the Codex sandbox/bypass mode; Evolution's safety boundary is still
    #    the temp workspace plus post-exit validators below.
    codex_usage: dict | None = None
    codex_diag: dict | None = None
    try:
        await backend.complete_file_edit(candidate_dir, prompt, CODEX_FILE_EDIT_SYSTEM)
        codex_usage = dict(getattr(backend, "last_usage", {}) or {})
        codex_diag = dict(getattr(backend, "last_codex_diagnostics", {}) or {})
    except AgentBackendError as exc:
        codex_usage = dict(getattr(backend, "last_usage", {}) or {})
        codex_diag = dict(getattr(backend, "last_codex_diagnostics", {}) or {})
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=REWRITE_ERROR_CODEX,
            reason=f"codex file-edit failed: {exc!s}"[:500],
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            codex_diagnostics=codex_diag or None,
            **base_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 4b. Strip the harness-seeded .git dir before isolation check — git was
    #     only for codex's `git diff` self-review and is not part of the
    #     candidate. Keeps `_ALLOWED_HIDDEN_NAMES` strict.
    git_dir = candidate_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)

    # 5. Hard isolation validators (post-exit only).
    ok, iso_status, iso_reason = validate_candidate_isolation(candidate_dir)
    if not ok:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=iso_status,
            reason=iso_reason,
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            codex_diagnostics=codex_diag or None,
            **base_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 6. Parse the candidate, capture frontmatter BEFORE reattach.
    try:
        candidate_fm_raw, candidate_body_raw = parse_skill_md(candidate_skill_path)
    except Exception as exc:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=CANDIDATE_INVALID_FRONTMATTER_CORRUPT,
            reason=f"parse_skill_md: {exc!s}"[:300],
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            codex_diagnostics=codex_diag or None,
            **base_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 7. Frontmatter reattach (runtime safety net, independent of metric).
    parent_fm_raw, _ = parse_skill_md(skill.skill_md_path)
    parent_fm_validated = SkillFrontmatter.model_validate(parent_fm_raw)
    write_skill_md(candidate_skill_path, parent_fm_validated, candidate_body_raw)
    _re_fm, candidate_body = parse_skill_md(candidate_skill_path)

    # 7a. Compute candidate-derived provenance eagerly so every post-parse
    #     outcome (including silent paths like no-op and sandbox-fail) lands
    #     a self-describing audit record. Memory
    #     feedback_diagnose_runtime_contracts_not_just_prompt.
    _new_added, _new_headings_list = _candidate_added_new_heading(parent_body, candidate_body)
    cand_prov: dict[str, Any] = {
        **base_prov,
        "candidate_body_sha256": hashlib.sha256(candidate_body.encode("utf-8")).hexdigest(),
        "computed_churn": _non_empty_line_churn(parent_body, candidate_body),
        "new_headings": list(_new_headings_list),
    }

    # 7b. Sandbox-failure classifier (t34v10): codex exits 0 even when
    #     `bwrap` aborts inside `-s workspace-write` on this host (memory
    #     reference_codex_workspace_write_broken_bwrap). With `--json`
    #     diagnostics, the bwrap line lands in `sandbox_errors`. Unchanged
    #     candidate + sandbox failure → rewrite_error_codex, not a silent
    #     no_op_file_edit.
    sandbox_errors = (codex_diag or {}).get("sandbox_errors") or []
    cmd_failures = (codex_diag or {}).get("command_failures") or []
    sandbox_failed = bool(sandbox_errors) or any(
        "permission denied" in (cf.get("stderr") or "").lower() for cf in cmd_failures
    )
    if sandbox_failed and candidate_body == parent_body:
        reason = (
            "sandbox failure under -s "
            + ((codex_diag or {}).get("sandbox") or "?")
            + ": "
            + "; ".join(sandbox_errors[:2] or ["see codex_diagnostics"])
        )[:300]
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=REWRITE_ERROR_CODEX,
            reason=reason,
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 7c. Fenced-candidate fallback (see docs/codex_usage.md):
    #     when the file is unchanged but codex returned a chat-mode reply with
    #     a fenced markdown block, treat the block as the candidate body. The
    #     reattach + parse cycle is re-run so downstream guards see the same
    #     shape they would for a file-edit candidate.
    if candidate_body == parent_body and not sandbox_failed:
        fenced_body = _extract_fenced_candidate(
            (codex_diag or {}).get("last_message") or "", parent_body
        )
        if fenced_body is not None:
            write_skill_md(candidate_skill_path, parent_fm_validated, fenced_body)
            _re_fm, candidate_body = parse_skill_md(candidate_skill_path)
            candidate_body_raw = fenced_body
            _new_added, _new_headings_list = _candidate_added_new_heading(
                parent_body, candidate_body
            )
            cand_prov = {
                **base_prov,
                "candidate_body_sha256": hashlib.sha256(candidate_body.encode("utf-8")).hexdigest(),
                "computed_churn": _non_empty_line_churn(parent_body, candidate_body),
                "new_headings": list(_new_headings_list),
            }
            if codex_diag is None:
                codex_diag = {}
            codex_diag["fenced_fallback"] = True

    # 8. no-op file edit (codex chose not to change the body).
    if candidate_body == parent_body:
        preservation = score_preservation(
            parent_body, candidate_body, parent_fm_raw, candidate_fm_raw
        )
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=NO_OP_FILE_EDIT,
            reason="candidate SKILL.md body byte-equal to parent",
            preservation=preservation,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 8b. No-new-heading guard (item 4b of t34v10 quality-spike prescription):
    #     reject candidates that add a new ##, ###, ####, … heading not in
    #     parent. Targeted edits should land inside an existing section.
    if _new_added:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=CANDIDATE_INVALID_NEW_HEADING,
            reason=(
                "candidate added new headings not in parent: " + "; ".join(_new_headings_list[:5])
            )[:300],
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 9. Sanitize + leak guard + size cap (reuse text-return validators).
    try:
        candidate_body = sanitize_rewritten_body(candidate_body, parent_body)
    except ValueError as exc:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=SANITIZE_ERROR,
            reason=f"sanitize_rewritten_body: {exc!s}"[:300],
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome
    corpus = leakage_provenance_corpus(skill, task_filter)
    if _reject_leaked_rewrite(skill, candidate_body, corpus):
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=LEAK_REJECTED,
            reason="leakage scanner flagged train-specific content",
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome
    hard = hard_size_violations(candidate_body, limits)
    if hard:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=SIZE_VIOLATION,
            reason="exceeds absolute size ceiling: " + "; ".join(hard),
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 10. Optional edit budget (set only when EVO_C1_EDIT_BUDGET_FRAC defined).
    if _budget_frac is not None:
        churn_val = float(cand_prov.get("computed_churn") or 0.0)
        if churn_val > _budget_frac:
            outcome = RewriteOutcome(
                body=parent_body,
                candidate_status=CANDIDATE_EDIT_BUDGET_EXCEEDED,
                reason=f"non-empty-line churn {churn_val:.2f} > budget {_budget_frac:.2f}",
                preservation=None,
                codex_usage=codex_usage or None,
                candidate_debug_dir=None,
                model_id=model_id,
                parent_body=parent_body,
                candidate_body_raw=candidate_body_raw,
                codex_diagnostics=codex_diag or None,
                **cand_prov,
            )
            _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
            return outcome

    # 11. Preservation score (measured against candidate_fm BEFORE reattach).
    preservation = score_preservation(parent_body, candidate_body, parent_fm_raw, candidate_fm_raw)

    # 12. Build final outcome before cleanup; let cleanup attach debug_dir.
    outcome = RewriteOutcome(
        body=candidate_body,
        candidate_status=PROMOTED_ELIGIBLE,
        reason="file-edit rewrite produced a candidate",
        preservation=preservation,
        codex_usage=codex_usage or None,
        candidate_debug_dir=None,
        model_id=model_id,
        parent_body=parent_body,
        candidate_body_raw=candidate_body_raw,
        codex_diagnostics=codex_diag or None,
        **cand_prov,
    )
    _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
    return outcome


def _cleanup_candidate(
    candidate_dir: Path,
    audit_root: Path | None,
    row_id: str,
    debug_archive: bool,
    outcome: RewriteOutcome,
) -> None:
    """Either archive the candidate dir for postmortem or delete it.

    Mutates ``outcome.candidate_debug_dir`` when archiving succeeds.
    """
    if not candidate_dir.exists():
        return
    if debug_archive and audit_root is not None and row_id:
        try:
            dest = audit_root / "c1_candidates" / row_id
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(candidate_dir), str(dest))
            outcome.candidate_debug_dir = str(dest)
            return
        except Exception as exc:
            logger.warning("EVO_DEBUG_C1 archive failed (%s); deleting candidate dir", exc)
    shutil.rmtree(candidate_dir, ignore_errors=True)


def _parse_diagnosis_json(session_dir: Path) -> dict | None:
    """Read the agent-written ``DIAGNOSIS.json``; ``None`` if missing/malformed.

    Missing or malformed is treated by the caller as ``insufficient`` →
    conservative no-op (paper-safe default).
    """
    p = session_dir / "DIAGNOSIS.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def _session_has_progress(ws: SessionWorkspace, parent_body: str) -> bool:
    """True if an errored/timed-out session left salvageable on-disk output.

    Either a written ``DIAGNOSIS.json`` or an edited ``SKILL.md`` counts; the
    downstream lift->gate path re-validates whatever is salvaged, so an
    unparseable edit is still progress (the gate rejects it cleanly).
    """
    if (ws.session_dir / "DIAGNOSIS.json").is_file():
        return True
    try:
        _fm, body = parse_skill_md(ws.skill_path)
    except Exception:
        return ws.skill_path.exists()
    return body != parent_body


def _diagnosis_to_reflection(diag: dict) -> dict:
    """Map a session ``DIAGNOSIS.json`` into a reflection-shaped audit record."""
    sev = str(diag.get("severity") or "info").lower()
    if sev not in _SEVERITY_RANK:
        sev = "info"
    suff = str(diag.get("evidence_sufficiency") or "insufficient").lower()
    if suff not in ("sufficient", "insufficient"):
        suff = "insufficient"
    return {
        "pattern": str(diag.get("consensus_pattern") or ""),
        "root_cause": str(diag.get("root_cause") or ""),
        "skill_gap": str(diag.get("skill_gap") or ""),
        "suggested_fix": str(diag.get("suggested_fix") or ""),
        "severity": sev,
        "evidence_sufficiency": suff,
        "rewrite_recommended": bool(diag.get("rewrite_recommended", False)),
    }


def _session_round_banner(row_id: str) -> str:
    """Cosmetic ` (round N)` suffix parsed from ``row{N}__…`` row ids."""
    m = re.match(r"row(\d+)", row_id or "")
    return f" — round {m.group(1)}" if m else ""


async def _rewrite_skill_agentic_session_impl(
    backend: LLMBackend,
    skill: Skill,
    task: Task | None,
    task_instruction: str,
    task_filter: str | None,
    manifest: SplitManifest | None,
    *,
    audit_root: Path | None,
    row_id: str,
    debug_archive: bool,
    model_id: str,
) -> RewriteOutcome:
    """Run ONE multi-turn agentic session per evolve round (EVO_REWRITE_MODE=agentic-session).

    The agent explores raw train traces in a rich scratch workspace, diagnoses
    the recurring procedural gap, edits SKILL.md in place, and self-validates by
    writing+running a candidate solve. We lift ONLY the edited SKILL.md, then run
    the SAME post-exit validators as the file-edit path. Selection authority stays
    with the downstream δ-gate; the session's self-score is diagnostic only.
    """
    parent_body = skill.body

    _budget_env = os.environ.get("EVO_C1_EDIT_BUDGET_FRAC")
    try:
        _budget_frac = float(_budget_env) if _budget_env else None
    except ValueError:
        _budget_frac = None
    base_prov: dict[str, Any] = {
        "parent_skill_path": str(skill.skill_md_path.resolve()),
        "parent_body_sha256": hashlib.sha256(parent_body.encode("utf-8")).hexdigest(),
        "edit_budget_frac": _budget_frac,
    }

    # 0. Fail-fast on misconfiguration (no silent degrade).
    if not isinstance(backend, AgentBackend):
        raise RuntimeError(
            "EVO_REWRITE_MODE=agentic-session requires an AgentBackend; "
            f"got {type(backend).__name__}"
        )
    if task is None:
        return RewriteOutcome(
            body=parent_body,
            candidate_status=REWRITE_ERROR_BACKEND,
            reason="agentic-session requires a Task to build the workspace; got None",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            **base_prov,
        )

    # 1. Same leakage assertion as file-edit; the train/test split is the boundary.
    if manifest is not None and task_filter:
        manifest.assert_no_leakage(
            [task_filter],
            scope="skill_evolution",
            context="rewrite_skill_with_audit(agentic_session,task_filter)",
        )

    limits = rewrite_size_limits(skill, skill.body)
    parent_headings = _parent_top_headings(skill.body)
    preserve_text = "\n".join(f"- {h}" for h in parent_headings) if parent_headings else "(none)"

    # 2. Build the rich, oracle-pruned scratch workspace.
    try:
        ws = build_session_workspace(skill, task, task_filter=task_filter, manifest=manifest)
    except SessionWorkspaceError as exc:
        return RewriteOutcome(
            body=parent_body,
            candidate_status=REWRITE_ERROR_BACKEND,
            reason=f"session workspace build failed: {exc!s}"[:500],
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            **base_prov,
        )

    # 3. Compose the round-protocol prompt; blind/oracle system per EVO_REFLECT_BLIND.
    blind = os.environ.get("EVO_REFLECT_BLIND") == "1"
    system = SESSION_SYSTEM_BLIND if blind else SESSION_SYSTEM_ORACLE
    meta = build_meta_skill(skill.path)
    rejected = build_rejected_edits_context(skill.path, row_id=row_id or None)
    round_context = f"\n## Cross-round learning (prior promote/rollback)\n{meta}\n" if meta else ""
    rejected_context = f"\n## Rejected-edit buffer\n{rejected}\n" if rejected else ""
    prompt = SESSION_PROMPT.format(
        round_banner=_session_round_banner(row_id),
        round_context=round_context,
        rejected_context=rejected_context,
        task_instruction=task_instruction or "(not provided)",
        preserve_headings=preserve_text,
        n_traces=len(ws.trace_ids),
        source_models=", ".join(ws.source_models) if ws.source_models else "(unknown)",
        max_chars=limits["max_chars"],
        max_lines=limits["max_lines"],
    )
    try:
        (ws.session_dir / "TASK.md").write_text(prompt, encoding="utf-8")
    except OSError:
        pass

    # 3b. Read-only fallback: EVO_SESSION_SELF_VALIDATE=0 strips the run scripts.
    self_validate = os.environ.get("EVO_SESSION_SELF_VALIDATE", "1") != "0"
    if not self_validate:
        for f in (ws.solve_sh, ws.measure_sh):
            try:
                Path(f).unlink()
            except OSError:
                pass

    def _env_float(name: str) -> float | None:
        v = os.environ.get(name)
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    budget = _env_float("EVO_SESSION_BUDGET_USD")
    timeout = _env_float("EVO_SESSION_TIMEOUT_S")

    # 4. Run the session. Non-zero rc is captured, not raised — the agent may
    #    have made useful partial progress; we decide from the SKILL.md on disk.
    codex_usage: dict | None = None
    codex_diag: dict | None = None
    try:
        await backend.complete_agent_session(
            ws.session_dir, prompt, system, budget_usd=budget, timeout_s=timeout
        )
        codex_usage = dict(getattr(backend, "last_usage", {}) or {})
        codex_diag = dict(getattr(backend, "last_codex_diagnostics", {}) or {})
    except AgentBackendError as exc:
        codex_usage = dict(getattr(backend, "last_usage", {}) or {})
        codex_diag = dict(getattr(backend, "last_codex_diagnostics", {}) or {})
        # Salvage on-disk progress: a timeout/error mid-session may still leave an
        # edited SKILL.md or a written DIAGNOSIS.json. The lift->gate path below
        # (isolation/leak/size/churn/delta-gate) protects integrity, so we only
        # bail outright when the agent left nothing to gate.
        codex_diag["session_error"] = str(exc)[:300]
        codex_diag["session_timed_out"] = "timed out" in str(exc).lower()
        if not _session_has_progress(ws, parent_body):
            cleanup_session_workspace(ws)
            return RewriteOutcome(
                body=parent_body,
                candidate_status=REWRITE_ERROR_CODEX,
                reason=f"agentic session failed: {exc!s}"[:500],
                preservation=None,
                codex_usage=codex_usage or None,
                candidate_debug_dir=None,
                model_id=model_id,
                parent_body=parent_body,
                candidate_body_raw=None,
                codex_diagnostics=codex_diag or None,
                **base_prov,
            )

    # 5. Parse the agent's DIAGNOSIS.json (the reflection-contract bridge) and
    #    attach it to the audit diagnostics so every outcome below carries it.
    diag = _parse_diagnosis_json(ws.session_dir)
    reflection_record = _diagnosis_to_reflection(diag) if diag else None
    if codex_diag is None:
        codex_diag = {}
    codex_diag["session_diagnosis"] = diag
    codex_diag["session_reflection_record"] = reflection_record
    codex_diag["session_trace_ids"] = list(ws.trace_ids)
    codex_diag["session_source_models"] = list(ws.source_models)
    codex_diag["session_self_validate"] = self_validate
    if diag is not None:
        codex_diag["session_best_self_score"] = diag.get("best_self_score")
        codex_diag["session_rounds_self_run"] = diag.get("rounds_self_run")

    # 6. Lift ONLY the edited SKILL.md into a clean candidate dir, drop the rest.
    candidate_dir = _candidate_root() / f"evo_sess_c_{uuid.uuid4().hex}"
    candidate_dir.mkdir(parents=True, exist_ok=False)
    candidate_skill_path = candidate_dir / "SKILL.md"
    try:
        shutil.copy2(ws.skill_path, candidate_skill_path)
    finally:
        cleanup_session_workspace(ws)

    # 7. Hard isolation validator (candidate dir holds exactly SKILL.md).
    ok, iso_status, iso_reason = validate_candidate_isolation(candidate_dir)
    if not ok:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=iso_status,
            reason=iso_reason,
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            codex_diagnostics=codex_diag or None,
            **base_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 8. Parse candidate, reattach parent frontmatter (runtime safety net).
    try:
        candidate_fm_raw, candidate_body_raw = parse_skill_md(candidate_skill_path)
    except Exception as exc:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=CANDIDATE_INVALID_FRONTMATTER_CORRUPT,
            reason=f"parse_skill_md: {exc!s}"[:300],
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
            codex_diagnostics=codex_diag or None,
            **base_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    parent_fm_raw, _ = parse_skill_md(skill.skill_md_path)
    parent_fm_validated = SkillFrontmatter.model_validate(parent_fm_raw)
    write_skill_md(candidate_skill_path, parent_fm_validated, candidate_body_raw)
    _re_fm, candidate_body = parse_skill_md(candidate_skill_path)

    _new_added, _new_headings_list = _candidate_added_new_heading(parent_body, candidate_body)
    cand_prov: dict[str, Any] = {
        **base_prov,
        "candidate_body_sha256": hashlib.sha256(candidate_body.encode("utf-8")).hexdigest(),
        "computed_churn": _non_empty_line_churn(parent_body, candidate_body),
        "new_headings": list(_new_headings_list),
    }

    # 9. No-op: the agent concluded the parent already handles the gap.
    if candidate_body == parent_body:
        preservation = score_preservation(
            parent_body, candidate_body, parent_fm_raw, candidate_fm_raw
        )
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=NO_OP_FILE_EDIT,
            reason="candidate SKILL.md body byte-equal to parent (session made no edit)",
            preservation=preservation,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 10. Low-confidence gate — DIAGNOSIS.json is the authoritative signal for
    #     this mode (the agent diagnoses from raw traces, not pre-digested
    #     reflections). Missing/insufficient/not-recommended ⇒ conservative no-op
    #     restoring the parent (paper-safe default).
    rr = reflection_record or {}
    if (
        diag is None
        or not rr.get("rewrite_recommended")
        or rr.get("evidence_sufficiency") == "insufficient"
    ):
        if diag is None:
            reason = "no/malformed DIAGNOSIS.json from session ⇒ treated as insufficient"
        elif not rr.get("rewrite_recommended"):
            # Section 9 already returned on byte-equal, so a real edit exists here:
            # the agent edited SKILL.md yet set rewrite_recommended=false. Flag the
            # contradiction for the audit (outcome stays the paper-safe no-op).
            codex_diag["session_recommend_edit_contradiction"] = True
            reason = "session edited SKILL.md but set rewrite_recommended=false; keeping parent"
        else:
            reason = "session reported evidence_sufficiency=insufficient; keeping parent"
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=LOW_CONFIDENCE_REFLECTION,
            reason=reason,
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 11. No-new-heading guard skipped in session mode: the agent's focused
    #     sub-section for a diagnosed gap is a valid minimal fix; leak + size cap
    #     + δ-gate (held-back train_b) are the integrity boundary. _new_headings
    #     stays in cand_prov for audit telemetry.

    # 12. Sanitize + leak guard + size cap (reuse text-return validators).
    try:
        candidate_body = sanitize_rewritten_body(candidate_body, parent_body)
    except ValueError as exc:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=SANITIZE_ERROR,
            reason=f"sanitize_rewritten_body: {exc!s}"[:300],
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome
    corpus = leakage_provenance_corpus(skill, task_filter)
    if _reject_leaked_rewrite(skill, candidate_body, corpus):
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=LEAK_REJECTED,
            reason="leakage scanner flagged train-specific content",
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome
    hard = hard_size_violations(candidate_body, limits)
    if hard:
        outcome = RewriteOutcome(
            body=parent_body,
            candidate_status=SIZE_VIOLATION,
            reason="exceeds absolute size ceiling: " + "; ".join(hard),
            preservation=None,
            codex_usage=codex_usage or None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=candidate_body_raw,
            codex_diagnostics=codex_diag or None,
            **cand_prov,
        )
        _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
        return outcome

    # 13. Optional edit budget.
    if _budget_frac is not None:
        churn_val = float(cand_prov.get("computed_churn") or 0.0)
        if churn_val > _budget_frac:
            outcome = RewriteOutcome(
                body=parent_body,
                candidate_status=CANDIDATE_EDIT_BUDGET_EXCEEDED,
                reason=f"non-empty-line churn {churn_val:.2f} > budget {_budget_frac:.2f}",
                preservation=None,
                codex_usage=codex_usage or None,
                candidate_debug_dir=None,
                model_id=model_id,
                parent_body=parent_body,
                candidate_body_raw=candidate_body_raw,
                codex_diagnostics=codex_diag or None,
                **cand_prov,
            )
            _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
            return outcome

    # 14. Promote-eligible candidate; downstream δ-gate is the selection authority.
    preservation = score_preservation(parent_body, candidate_body, parent_fm_raw, candidate_fm_raw)
    outcome = RewriteOutcome(
        body=candidate_body,
        candidate_status=PROMOTED_ELIGIBLE,
        reason="agentic-session rewrite produced a candidate",
        preservation=preservation,
        codex_usage=codex_usage or None,
        candidate_debug_dir=None,
        model_id=model_id,
        parent_body=parent_body,
        candidate_body_raw=candidate_body_raw,
        codex_diagnostics=codex_diag or None,
        **cand_prov,
    )
    _cleanup_candidate(candidate_dir, audit_root, row_id, debug_archive, outcome)
    return outcome


async def rewrite_skill_with_audit(
    backend: LLMBackend,
    skill: Skill,
    reflections: list[dict],
    task_instruction: str = "",
    task_filter: str | None = None,
    manifest: SplitManifest | None = None,
    include_best_trace_code: bool = True,
    *,
    file_edit: bool = False,
    session: bool = False,
    task: Task | None = None,
    audit_root: Path | None = None,
    row_id: str = "",
    debug_archive: bool = False,
) -> RewriteOutcome:
    """Produce a structured :class:`RewriteOutcome` for one rewrite call.

    ``file_edit=False`` (default) runs the historical text-return path via
    :func:`_rewrite_skill_text_return_impl`.  ``file_edit=True`` runs the
    Codex file-edit branch via :func:`_rewrite_skill_file_edit_impl`.
    ``session=True`` runs the unified agentic-session branch via
    :func:`_rewrite_skill_agentic_session_impl` (requires ``task``).
    No branch calls the public :func:`rewrite_skill` wrapper; that
    would recurse.
    """
    parent_body = skill.body
    model_id = _backend_label(backend)

    if session:
        return await _rewrite_skill_agentic_session_impl(
            backend,
            skill,
            task,
            task_instruction,
            task_filter,
            manifest,
            audit_root=audit_root,
            row_id=row_id,
            debug_archive=debug_archive,
            model_id=model_id,
        )

    if file_edit:
        return await _rewrite_skill_file_edit_impl(
            backend,
            skill,
            reflections,
            task_instruction,
            task_filter,
            manifest,
            include_best_trace_code,
            audit_root=audit_root,
            row_id=row_id,
            debug_archive=debug_archive,
            model_id=model_id,
        )

    try:
        new_body, candidate_status, reason, tokens_used = await _rewrite_skill_text_return_impl(
            backend,
            skill,
            reflections,
            task_instruction,
            task_filter,
            manifest,
            include_best_trace_code,
        )
    except Exception as exc:
        return RewriteOutcome(
            body=parent_body,
            candidate_status=REWRITE_ERROR_BACKEND,
            reason=f"{type(exc).__name__}: {exc!s}"[:500],
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id=model_id,
            parent_body=parent_body,
            candidate_body_raw=None,
        )

    # Compute preservation + codex_usage even for text-return so t34v8 and
    # t34v9 mechanism numbers are comparable (review #3).  Text-return does
    # not touch frontmatter, so candidate_fm == parent_fm.
    preservation = None
    if new_body != parent_body:
        try:
            parent_fm_raw, _ = parse_skill_md(skill.skill_md_path)
            preservation = score_preservation(parent_body, new_body, parent_fm_raw, parent_fm_raw)
        except Exception:
            preservation = None
    codex_usage = None
    if isinstance(backend, AgentBackend):
        last = getattr(backend, "last_usage", None)
        if last:
            codex_usage = dict(last)

    return RewriteOutcome(
        body=new_body,
        candidate_status=candidate_status,
        reason=reason,
        preservation=preservation,
        codex_usage=codex_usage,
        candidate_debug_dir=None,
        model_id=model_id,
        parent_body=parent_body,
        candidate_body_raw=new_body if new_body != parent_body else None,
        tokens_used=tokens_used,
    )


async def rewrite_skill(
    backend: LLMBackend,
    skill: Skill,
    reflections: list[dict],
    task_instruction: str = "",
    task_filter: str | None = None,
    manifest: SplitManifest | None = None,
    include_best_trace_code: bool = True,
) -> str:
    """Use reflections + task context to generate an improved skill body.

    Thin compatibility wrapper around :func:`rewrite_skill_with_audit`.
    Existing callers keep the historical ``str`` return type.
    """
    outcome = await rewrite_skill_with_audit(
        backend,
        skill,
        reflections,
        task_instruction=task_instruction,
        task_filter=task_filter,
        manifest=manifest,
        include_best_trace_code=include_best_trace_code,
        file_edit=False,
    )
    if outcome.candidate_status == SIZE_VIOLATION:
        # Historical behaviour raised here; preserve for callers that don't
        # consume the structured outcome.
        raise ValueError("Rewritten skill body " + outcome.reason)
    return outcome.body
