"""Trace reflection and reflection-store helpers for skill evolution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from evolution.core.models import Skill
from evolution.evolution.meta_skill import build_meta_skill, build_rejected_edits_context
from evolution.evolution.reflection_evidence import (
    render_artifacts,
    render_failure_evidence,
    render_oracle_sections,
    render_solve_diag,
    validate_trace_provenance,
)
from evolution.lineage.traces import TraceStore, load_trace_evidence
from evolution.llm.backend import LLMBackend
from evolution.splits import SplitManifest, assert_no_leakage

REFLECTION_SCHEMA = "evolution-reflection-v2"

SUCCESS_TRACE_SAMPLE_K = int(os.environ.get("EVO_REFLECT_SUCCESS_K", "3") or "3")
SIBLING_DIGEST_MAX_LINES = int(os.environ.get("EVO_REFLECT_SIBLING_MAX", "20") or "20")
META_SKILL_CHAR_CAP = int(os.environ.get("EVO_REFLECT_META_SKILL_CHARS", "4000") or "4000")

logger = logging.getLogger(__name__)


def _backend_label(backend: LLMBackend) -> str:
    for attr in ("model_label", "_model", "model"):
        value = getattr(backend, attr, None)
        if value:
            return str(value)
    return backend.__class__.__name__


def _enforce_scope(
    traces: list[dict],
    manifest: SplitManifest | None,
    scope: str,
    caller: str,
) -> list[dict]:
    """Filter traces to the requested manifest scope and fail on leakage."""
    if manifest is None:
        return traces
    allowed = manifest.ids(scope)
    task_ids = {t.get("task", "") for t in traces if t.get("task")}
    known = manifest.all_ids()
    to_check = [tid for tid in task_ids if tid in known]
    assert_no_leakage(
        manifest,
        [tid for tid in to_check if tid not in allowed],
        scope=scope,
        context=caller,
    )
    filtered = [t for t in traces if t.get("task", "") in allowed]
    if len(filtered) != len(traces):
        logger.info(
            "%s: filtered %d/%d traces by manifest %s scope=%s",
            caller,
            len(traces) - len(filtered),
            len(traces),
            manifest.name,
            scope,
        )
    return filtered


REFLECT_SYSTEM_BLIND = """You are an expert at analyzing LLM-generated code traces.
You receive the task instruction, current skill, generated code, pass/fail
count, sanitized solve diagnostics, metadata-only artifact status, and
value-free failure evidence.

For source/local training traces you may also receive sanitized execution
feedback: the agent's own stdout/stderr tail, exception class, exit/timeout,
and a sanitized failure-message tail. You are NEVER given verifier source,
full test files, expected/golden values, pytest node ids, failed-test names,
assertion expressions, or expected constants — these are stripped. Diagnose
the procedural failure from the code, pass/fail signal, solve diagnostics,
sanitized feedback (when present), and produced artifact shape.

Be specific and actionable. Focus on the skill instructions, not the model's
capabilities. Propose fixes that transfer across related tasks and models.
NEVER recommend copying test code, assertion names, expected constants,
train-only filenames, dataset literals, or path hacks into the skill. If the
evidence is insufficient, say so and avoid a task-specific workaround."""

REFLECT_SYSTEM_ORACLE = """You are an expert at analyzing LLM-generated code traces.
You receive the task instruction, current skill, generated code, pass/fail
count, solve diagnostics, produced artifact shape, and — for source/local
training traces — full diagnostic evidence: the test source, failed test
names, assertion expressions, expected values, pytest tracebacks, and source
artifacts. Use all of it to localize the procedural failure precisely.

Be specific and actionable. Focus on the skill instructions, not the model's
capabilities. Propose fixes that transfer across related tasks and models.
CRITICAL: do not paraphrase or copy literals, expected values, dataset/table/
column names, or test/assertion text into the rewritten skill — a leak scanner
rejects candidates that do, and the rewrite is discarded. Your structured
fields are sanitized before any downstream rewriter sees them, so you may state
expected values plainly to the analyst while keeping the skill itself general.
If the evidence is insufficient, say so and avoid a task-specific workaround."""

# Back-compat alias: legacy import surface defaults to the blind wording.
REFLECT_SYSTEM = REFLECT_SYSTEM_BLIND


def _reflect_system() -> str:
    """Select the system prompt from the oracle-visibility flag (compose time)."""
    return (
        REFLECT_SYSTEM_BLIND
        if os.environ.get("EVO_REFLECT_BLIND") == "1"
        else REFLECT_SYSTEM_ORACLE
    )


REFLECT_PROMPT = """## Task Instruction
{instruction}
{workspace_section}
## Skill Content
{skill_body}
{cross_round_section}{rejected_edits_section}
## Generated Code
```python
{code}
```
{siblings_section}
## Result
Passed: {passed}/{total}

## Agent Solve Diagnostics
exit_code: {solve_exit_code}
timeout: {solve_timeout}
diagnostic: {solve_diagnostic}

## Failure Evidence
{failure_evidence}

## Produced Artifact Metadata
{produced_artifacts}
{tests_section}
## Analysis Request
1. **Root cause**: What specifically went wrong in the generated code?
2. **Skill gap**: What is the skill missing or saying incorrectly that led to this failure?
3. **Fix**: What specific change to the skill would prevent this failure?

Respond as JSON:
```json
{{
  "root_cause": "...",
  "skill_gap": "...",
  "suggested_fix": "...",
  "severity": "critical|major|minor",
  "pattern": "wrong_library|wrong_api|missing_step|logic_error|other",
  "evidence_sufficiency": "sufficient|insufficient",
  "rewrite_recommended": true
}}
```
Set `evidence_sufficiency` to `insufficient` and `rewrite_recommended` to
false when the evidence does not localize a concrete procedural skill gap."""


REFLECT_SUCCESS_PROMPT = """## Task Instruction
{instruction}
{workspace_section}
## Skill Content
{skill_body}
{cross_round_section}{rejected_edits_section}
## Generated Code
```python
{code}
```
{siblings_section}
## Result
Passed: {passed}/{total}

## Agent Solve Diagnostics
exit_code: {solve_exit_code}
timeout: {solve_timeout}
diagnostic: {solve_diagnostic}

## Execution Evidence
{failure_evidence}

## Produced Artifact Metadata
{produced_artifacts}
{tests_section}
## Analysis Request
1. **Reinforcement pattern**: What concrete behaviour in the skill enabled this pass?
2. **Helpful sections**: Which top-level skill sections (verbatim headings) carried the load?
3. **Transferability**: Will this approach generalise to sibling tasks of the same class?

Respond as JSON:
```json
{{
  "reinforcement_pattern": "import_strategy|step_sequencing|edge_case_handling|api_choice|other",
  "helpful_sections": ["## Heading A", "## Heading B"],
  "transferability": "task_specific|partial|broad",
  "evidence_sufficiency": "sufficient|insufficient",
  "preserve_recommended": true
}}
```
Set `evidence_sufficiency` to `insufficient` and `preserve_recommended` to
false when the trace does not localise concrete skill text worth preserving."""


def _render_section(title: str, blob: str) -> str:
    if not blob:
        return ""
    return f"\n## {title}\n{blob}\n"


def _render_oracle_block(blob: str) -> str:
    """Slot the already-headed oracle sections as their own block, or empty."""
    if not blob:
        return ""
    return f"\n{blob}\n"


def _digest_for_trace(trace: dict, evidence: dict | None) -> str:
    task = str(trace.get("task") or "?")
    fe = (evidence or {}).get("failure_evidence") or {}
    vr = fe.get("verifier_result") or {}
    cat = str(vr.get("verification_category") or "?")
    fcats = fe.get("failure_categories") or []
    top_fc = str(fcats[0]) if fcats else "-"
    arts = (evidence or {}).get("produced_artifacts")
    n_arts = (
        sum(1 for a in arts if isinstance(a, dict) and a.get("path"))
        if isinstance(arts, list)
        else 0
    )
    passed = trace.get("passed", 0)
    total = trace.get("total", 0)
    return (
        f"task={task} pass={passed}/{total} verifier={cat} top_failure={top_fc} artifacts={n_arts}"
    )


def build_sibling_digests(
    traces: list[dict],
    evidences: dict[str, dict | None],
) -> dict[str, str]:
    """Map trace_id -> one-line digest for every supplied trace."""
    out: dict[str, str] = {}
    for t in traces:
        tid = t.get("id") or ""
        if not tid:
            continue
        out[tid] = _digest_for_trace(t, evidences.get(tid))
    return out


def _render_siblings_blob(self_id: str, digests: dict[str, str]) -> str:
    items = [d for tid, d in digests.items() if tid != self_id]
    if not items:
        return ""
    if len(items) > SIBLING_DIGEST_MAX_LINES:
        items = items[:SIBLING_DIGEST_MAX_LINES]
        items.append(f"(capped at {SIBLING_DIGEST_MAX_LINES} sibling lines)")
    return "\n".join(f"- {line}" for line in items)


def extract_workspace_view(trace_dir: Path, task_instruction: str) -> str:
    """Return the agent-visible workspace block from ``prompt.md``.

    Solver prompts are saved as ``{task.instruction}\\n\\n{workspace_context}``;
    strip the leading instruction prefix and return the remainder. Empty string
    on missing files or instruction-only prompts (legacy traces).
    """
    p = trace_dir / "prompt.md"
    if not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    stripped = text
    if task_instruction and stripped.startswith(task_instruction):
        stripped = stripped[len(task_instruction) :]
    return stripped.strip()


async def reflect_trace(
    backend: LLMBackend,
    trace_dir: Path,
    skill: Skill,
    task_instruction: str,
    require_v3_evidence: bool = False,
    *,
    kind: str = "failure",
    siblings: dict[str, str] | None = None,
    meta_skill_blob: str = "",
    rejected_edits_blob: str = "",
    workspace_view: str | None = None,
) -> dict:
    """Analyze a single trace and return structured reflection.

    ``kind="failure"`` uses ``REFLECT_PROMPT`` (root-cause framing); ``"success"``
    uses ``REFLECT_SUCCESS_PROMPT`` (reinforcement-pattern framing).
    """
    code = (trace_dir / "solve.py").read_text(encoding="utf-8")
    result = json.loads((trace_dir / "result.json").read_text(encoding="utf-8"))
    evidence = load_trace_evidence(trace_dir)
    solve_exit, solve_timeout, solve_diag = render_solve_diag(evidence)
    self_id = result.get("id", trace_dir.name)
    if workspace_view is None:
        workspace_view = extract_workspace_view(trace_dir, task_instruction)

    template = REFLECT_SUCCESS_PROMPT if kind == "success" else REFLECT_PROMPT
    prompt = template.format(
        instruction=task_instruction,
        workspace_section=_render_section("Agent Workspace View", workspace_view or ""),
        skill_body=skill.body,
        cross_round_section=_render_section("Cross-Round Learning", meta_skill_blob or ""),
        rejected_edits_section=_render_section("Rejected-Edit Buffer", rejected_edits_blob or ""),
        siblings_section=_render_section(
            "Other Train Traces This Round",
            _render_siblings_blob(self_id, siblings or {}),
        ),
        code=code,
        passed=result.get("passed", 0),
        total=result.get("total", 0),
        solve_exit_code=solve_exit,
        solve_timeout=solve_timeout,
        solve_diagnostic=solve_diag,
        failure_evidence=render_failure_evidence(evidence),
        produced_artifacts=render_artifacts(evidence),
        tests_section=_render_oracle_block(render_oracle_sections(evidence)),
    )

    resp = await backend.complete(
        [{"role": "user", "content": prompt}],
        system=_reflect_system(),
    )

    reflection = _extract_json(resp.content)
    reflection["trace_id"] = self_id
    reflection["model"] = result.get("model", "")
    reflection["reflector_model"] = _backend_label(backend)
    reflection["pass_rate"] = result.get("pass_rate", 0.0)
    reflection["tokens_used"] = resp.total_tokens
    reflection["kind"] = kind
    reflection.setdefault("schema", REFLECTION_SCHEMA)

    if kind == "success":
        if require_v3_evidence:
            if reflection.get("evidence_sufficiency") not in ("sufficient", "insufficient"):
                reflection["evidence_sufficiency"] = "insufficient"
            if not isinstance(reflection.get("preserve_recommended"), bool):
                reflection["preserve_recommended"] = False
        else:
            reflection.setdefault("evidence_sufficiency", "sufficient")
            reflection.setdefault("preserve_recommended", True)
    else:
        if require_v3_evidence:
            if reflection.get("evidence_sufficiency") not in ("sufficient", "insufficient"):
                reflection["evidence_sufficiency"] = "insufficient"
            if not isinstance(reflection.get("rewrite_recommended"), bool):
                reflection["rewrite_recommended"] = False
        else:
            reflection.setdefault("evidence_sufficiency", "sufficient")
            reflection.setdefault("rewrite_recommended", True)

    return reflection


def save_reflection(trace_dir: Path, reflection: dict) -> Path:
    """Save reflection.json into the trace folder."""
    path = trace_dir / "reflection.json"
    path.write_text(json.dumps(reflection, indent=2), encoding="utf-8")
    return path


def load_reflection(trace_dir: Path) -> dict | None:
    """Load reflection.json from a trace folder, or None."""
    path = trace_dir / "reflection.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_success_traces(
    success_traces: list[tuple[dict, Path]], k: int
) -> list[tuple[dict, Path]]:
    """Pick up to ``k`` passing traces with task diversity preference."""
    if k <= 0 or not success_traces:
        return []
    seen_tasks: set[str] = set()
    primary: list[tuple[dict, Path]] = []
    spillover: list[tuple[dict, Path]] = []
    for t, d in success_traces:
        task = str(t.get("task") or "")
        if task and task not in seen_tasks:
            primary.append((t, d))
            seen_tasks.add(task)
        else:
            spillover.append((t, d))
    picked = primary[:k]
    if len(picked) < k:
        picked.extend(spillover[: k - len(picked)])
    return picked


def reflect_failing_traces(
    backend: LLMBackend,
    skill: Skill,
    task_instruction: str,
    task_filter: str | None = None,
    trace_model: str | None = None,
    limit: int | None = None,
    manifest: SplitManifest | None = None,
    scope: str = "skill_evolution",
    strict: bool = False,
    require_v3_evidence: bool = False,
) -> list[dict]:
    """Analyze train traces (failure + sampled success) and save reflections."""
    ts = TraceStore(skill)
    traces = ts.list(task=task_filter)
    if trace_model:
        traces = [t for t in traces if trace_model.lower() in t.get("model", "").lower()]
    traces = _enforce_scope(traces, manifest, scope=scope, caller="reflect_failing_traces")

    failing: list[tuple[dict, Path]] = []
    passing: list[tuple[dict, Path]] = []
    for t in traces:
        trace_dir = ts.traces_dir / t["id"]
        if not trace_dir.exists() or (trace_dir / "reflection.json").exists():
            continue
        _ev = load_trace_evidence(trace_dir)
        if (_ev or {}).get("trace_context", {}).get("phase") == "promotion_validation":
            continue
        if t.get("success"):
            passing.append((t, trace_dir))
        else:
            failing.append((t, trace_dir))

    if not failing and not passing:
        return []
    if limit is not None:
        failing = failing[:limit]

    successes = _sample_success_traces(passing, SUCCESS_TRACE_SAMPLE_K)
    targets: list[tuple[dict, Path, str]] = [
        *((t, d, "failure") for t, d in failing),
        *((t, d, "success") for t, d in successes),
    ]

    require_v3 = require_v3_evidence or manifest is not None
    evidences: dict[str, dict | None] = {}
    for trace, trace_dir, _kind in targets:
        ev = load_trace_evidence(trace_dir)
        evidences[trace.get("id", "")] = ev
        validate_trace_provenance(
            trace=trace,
            trace_dir=trace_dir,
            evidence=ev,
            manifest=manifest,
            scope=scope,
            require_v3_evidence=require_v3,
        )

    sibling_digests = build_sibling_digests([t for t, _d, _k in targets], evidences)
    meta_skill_blob = build_meta_skill(skill.path, char_cap=META_SKILL_CHAR_CAP)
    rejected_edits_blob = build_rejected_edits_context(skill.path)

    reflections: list[dict] = []
    for t, trace_dir, kind in targets:
        try:
            reflection = asyncio.run(
                reflect_trace(
                    backend,
                    trace_dir,
                    skill,
                    task_instruction,
                    require_v3_evidence=require_v3,
                    kind=kind,
                    siblings=sibling_digests,
                    meta_skill_blob=meta_skill_blob,
                    rejected_edits_blob=rejected_edits_blob,
                )
            )
            if "parse_error" in reflection:
                raise ValueError(
                    f"reflection JSON parse failed for trace {t['id']}: "
                    f"{reflection.get('parse_error')}"
                )
            save_reflection(trace_dir, reflection)
            reflections.append(reflection)
        except Exception as exc:
            if strict:
                raise
            reflections.append({"trace_id": t["id"], "error": str(exc), "kind": kind})

    return reflections


def collect_reflections(
    skill: Skill,
    task_filter: str | None = None,
    trace_model: str | None = None,
    skill_version: str | None = None,
    manifest: SplitManifest | None = None,
    scope: str = "skill_evolution",
) -> list[dict]:
    """Collect all existing reflections from trace folders."""
    ts = TraceStore(skill)
    traces = ts.list(task=task_filter)

    if trace_model:
        traces = [t for t in traces if trace_model.lower() in t.get("model", "").lower()]
    if skill_version:
        traces = [t for t in traces if t.get("skill_version") == skill_version]

    traces = _enforce_scope(traces, manifest, scope=scope, caller="collect_reflections")

    reflections = []
    for t in traces:
        r = load_reflection(ts.traces_dir / t["id"])
        if r:
            reflections.append(r)
    return reflections


def scoped_trace_dirs(
    skill: Skill,
    task_filter: str | None = None,
    trace_model: str | None = None,
    skill_version: str | None = None,
    manifest: SplitManifest | None = None,
    scope: str = "skill_evolution",
) -> list[tuple[dict, Path]]:
    """Return ``(trace_dict, trace_dir)`` pairs in the same train scope as
    :func:`collect_reflections`.

    Used by the agentic-session rewrite mode to seed raw traces (``solve.py`` +
    evidence) the agent explores. Applies the identical model/version/manifest
    filtering and drops ``promotion_validation``-phase traces (held-back δ-gate
    pool) so the session never sees validation data. Missing trace dirs are
    skipped.
    """
    ts = TraceStore(skill)
    traces = ts.list(task=task_filter)
    if trace_model:
        traces = [t for t in traces if trace_model.lower() in t.get("model", "").lower()]
    if skill_version:
        traces = [t for t in traces if t.get("skill_version") == skill_version]
    traces = _enforce_scope(traces, manifest, scope=scope, caller="scoped_trace_dirs")

    pairs: list[tuple[dict, Path]] = []
    for t in traces:
        trace_dir = ts.traces_dir / t["id"]
        if not trace_dir.is_dir():
            continue
        ev = load_trace_evidence(trace_dir)
        if (ev or {}).get("trace_context", {}).get("phase") == "promotion_validation":
            continue
        pairs.append((t, trace_dir))
    return pairs


def _first_balanced_object(text: str) -> str | None:
    """Return the first brace-balanced ``{...}`` span (string-aware)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def _extract_json(text: str) -> dict:
    """Extract JSON object from an LLM response."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    span = _first_balanced_object(text)
    if span:
        try:
            return json.loads(span)
        except json.JSONDecodeError:
            pass

    return {"raw_response": text, "parse_error": "Could not extract JSON"}
