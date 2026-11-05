"""Unified skill editor contract for the iterative evolution loop."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EVO_SRC = REPO_ROOT / "src"
if str(EVO_SRC) not in sys.path:
    sys.path.insert(0, str(EVO_SRC))
if str(REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "experiments"))

from evolution.core.parser import load_skill, write_skill_md  # noqa: E402
from evolution.evolution.rewrite_sanitize import sanitize_rewritten_body  # noqa: E402

from .trace_bundle import (  # noqa: E402
    _collect_editor_redactions,
    bundles_to_reflections,
    render_traces_digest,
)
from .verification import scan_body_leakage  # noqa: E402


@dataclass
class EditResult:
    new_body: str
    changed: bool
    telemetry: dict[str, Any] = field(default_factory=dict)
    leak_findings: int = 0


def _body_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cfg_bool(value: Any, *, default: bool) -> bool:
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


def _split_skill_md(md: str) -> tuple[str, str]:
    import re

    m = re.match(r"^(---\n.*?\n---\n)(.*)$", md, flags=re.DOTALL)
    if not m:
        return "", md
    return m.group(1), m.group(2)


# output/, pytest, and <workspace> are legitimate portable runtime-contract terms; keep them.
_HARNESS_LITERAL_RE = re.compile(
    r"(?i)(?:"
    r"environment/data|"
    r"solve\.sh|solution\.py|_solve\.py|task\.toml|"
    r"tests/_test|"
    r"\bverifier\b|"
    r"prompt\.md|response\.md"
    r")"
)


def _literal_patterns_from_bundles(bundles: list[dict[str, Any]] | None) -> set[str]:
    literals: set[str] = set()
    for b in bundles or []:
        literals.update(_collect_editor_redactions(b))
        instruction = str(b.get("instruction") or "")
        for line in instruction.splitlines():
            m = re.match(r"^\s*#\s+(.+?)\s*$", line)
            if m and len(m.group(1).strip()) >= 8:
                literals.add(m.group(1).strip())
                break
        for key in re.findall(r'"([A-Za-z][A-Za-z0-9_-]{3,})"\s*:', instruction):
            key_l = key.lower()
            if (
                "_" in key
                or "-" in key
                or "duration" in key_l
                or "speed" in key_l
                or "dependency" in key_l
            ):
                literals.add(key)
    return {
        lit
        for lit in literals
        if lit and lit not in {"json", "yaml", "yml", "csv", "txt", "md"} and len(lit) >= 4
    }


def _classify_nonportable_line(line: str, literals: set[str]) -> str:
    low = line.lower()
    if _HARNESS_LITERAL_RE.search(line):
        return "runtime_contract"
    if re.search(r"(?i)\b(?:training example|training case|training trace|train case)\b", line):
        return "exact_train_literal"
    if re.search(
        r"(?i)\b(?:exact field names|must be used in the json report|must be used in json report)\b",
        line,
    ):
        return "exact_train_schema"
    if re.search(r"\b(?:original_duration|optimized_duration|speedup_percent)\b", line):
        return "exact_train_schema"
    if re.match(r"^\s*---\s*$", line):
        return ""
    for lit in literals:
        # trailing "." is a sentence boundary, not part of the literal (e.g. "...foo.yml.")
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(lit)}(?![A-Za-z0-9_-])", line):
            if "." in lit:
                return "exact_train_file"
            if "duration" in lit.lower() or "minute" in lit.lower():
                return "exact_train_schema"
            return "exact_train_literal"
    if "avg_duration_minutes" in low or "p95_duration_minutes" in low:
        return "exact_train_schema"
    return ""


_PORTABLE_REPLACEMENTS = {
    "runtime_contract": (
        "- Follow the fixed solver/runtime contract for where inputs are staged "
        "and where requested artifacts must be written; do not hard-code "
        "benchmark paths in the skill."
    ),
    "exact_train_file": (
        "- Refer to task inputs and requested artifacts by their role, not by "
        "train-task filenames; inspect the current task instruction and file "
        "listing at solve time."
    ),
    "exact_train_schema": (
        "- When aggregating metrics from structured JSON/YAML, inspect whether "
        "values are nested records and extract the relevant numeric measure "
        "rather than summing entire objects."
    ),
    "exact_train_literal": (
        "- Generalize from the observed train case: infer true data dependencies "
        "from producers, consumers, and required artifacts instead of copying "
        "train-only names."
    ),
}


def portable_sanitize_skill_body(
    body: str,
    *,
    bundles: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Rewrite non-portable skill lines into generic procedural guidance."""
    literals = _literal_patterns_from_bundles(bundles)
    findings: list[dict[str, str]] = []
    out: list[str] = []
    inserted: set[str] = set()
    fence: list[str] | None = None

    def emit_replacement(kind: str) -> None:
        replacement = _PORTABLE_REPLACEMENTS[kind]
        if replacement not in inserted:
            out.append(replacement)
            inserted.add(replacement)

    def classify_fence(block: list[str]) -> list[str]:
        kinds: list[str] = []
        for line in block[1:-1]:
            kind = _classify_nonportable_line(line, literals)
            if kind:
                findings.append({"kind": kind, "line": line.strip()[:240]})
                if kind not in kinds:
                    kinds.append(kind)
        return kinds

    for raw in body.splitlines():
        stripped = raw.strip()
        if fence is not None:
            fence.append(raw)
            if stripped.startswith("```"):
                kinds = classify_fence(fence)
                if kinds:
                    for kind in kinds:
                        emit_replacement(kind)
                else:
                    out.extend(fence)
                fence = None
            continue
        if stripped.startswith("```"):
            fence = [raw]
            continue
        kind = _classify_nonportable_line(raw, literals)
        if not kind:
            out.append(raw)
            continue
        findings.append({"kind": kind, "line": stripped[:240]})
        emit_replacement(kind)
    if fence is not None:
        kinds = classify_fence(fence + ["```"])
        if kinds:
            for kind in kinds:
                emit_replacement(kind)
        else:
            out.extend(fence)
    return "\n".join(out).strip() + "\n", findings


def _strip_nested_skill_frontmatter(body: str) -> tuple[str, list[dict[str, str]]]:
    """Remove Agent-Skill YAML frontmatter blocks accidentally pasted into the body."""
    findings: list[dict[str, str]] = []
    lines = body.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "---":
            j = i + 1
            block: list[str] = []
            while j < len(lines) and lines[j].strip() != "---":
                block.append(lines[j])
                j += 1
            if j < len(lines) and any(
                re.match(r"^\s*(name|description|version|author|tags|models|capabilities):", b)
                for b in block
            ):
                findings.append({"kind": "nested_frontmatter", "line": "---"})
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip() + "\n", findings


def _sanitize_rewritten_body_additive(
    candidate_body: str,
    original_body: str,
) -> tuple[str, dict[str, Any]]:
    """Sanitize a rewrite, allowing short additive bridges before section restore.

    Native frameworks can export a compact bridge artifact while their real
    runtime state lives elsewhere. The canonical SKILL.md still gets the
    original sections restored below, so the short-body guard should not reject
    those compact-but-real additions before restoration happens.
    """
    try:
        return sanitize_rewritten_body(candidate_body, original_body), {
            "short_rewrite_guard_relaxed": False,
        }
    except ValueError as exc:
        msg = str(exc)
        if "Rewritten skill body is too short" not in msg:
            raise
        prior = os.environ.get("EVO_REWRITE_MIN_BODY_FRAC")
        os.environ["EVO_REWRITE_MIN_BODY_FRAC"] = os.environ.get(
            "EVO_ADDITIVE_REWRITE_MIN_BODY_FRAC",
            "0.02",
        )
        try:
            sanitized = sanitize_rewritten_body(candidate_body, original_body)
        except ValueError as relaxed_exc:
            if "Rewritten skill body is too short" not in str(relaxed_exc):
                raise
            sanitized = original_body
            return sanitized, {
                "short_rewrite_guard_relaxed": True,
                "short_rewrite_fallback_unchanged": True,
                "short_rewrite_original_error": msg,
                "short_rewrite_relaxed_error": str(relaxed_exc),
            }
        finally:
            if prior is None:
                os.environ.pop("EVO_REWRITE_MIN_BODY_FRAC", None)
            else:
                os.environ["EVO_REWRITE_MIN_BODY_FRAC"] = prior
        return sanitized, {
            "short_rewrite_guard_relaxed": True,
            "short_rewrite_original_error": msg,
        }


def _top_level_sections(body: str) -> list[tuple[str, str]]:
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^##\s+\S", line)]
    sections: list[tuple[str, str]] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        heading = lines[start].strip()
        sections.append((heading, "\n".join(lines[start:end]).strip()))
    return sections


def _restore_missing_original_sections(
    *,
    original_body: str,
    candidate_body: str,
) -> tuple[str, list[str]]:
    """Keep framework edits additive unless they intentionally retain section headings."""
    restored: list[str] = []
    out = candidate_body.rstrip()
    candidate_headings = {
        line.strip() for line in candidate_body.splitlines() if re.match(r"^##\s+\S", line)
    }
    for heading, section in _top_level_sections(original_body):
        if heading in candidate_headings:
            continue
        out += "\n\n" + section
        restored.append(heading)
    return out.strip() + "\n", restored


def finalize_skill_md(
    *, original_full: str, candidate_full: str, corpus: str = ""
) -> tuple[str, int]:
    """Sanitize body and scan for leakage; preserve YAML frontmatter."""
    fm_block, orig_body = _split_skill_md(original_full)
    _, cand_body = _split_skill_md(candidate_full)
    if not cand_body.strip():
        cand_body = candidate_full
    sanitized_body = sanitize_rewritten_body(cand_body, orig_body or original_full)
    sanitized_body, _frontmatter_findings = _strip_nested_skill_frontmatter(sanitized_body)
    leak = scan_body_leakage(sanitized_body, corpus=corpus)
    if fm_block:
        return fm_block + sanitized_body.strip() + "\n", leak
    return sanitized_body.strip() + "\n", leak


def finalize_skill_md_with_portability(
    *,
    original_full: str,
    candidate_full: str,
    corpus: str = "",
    bundles: list[dict[str, Any]] | None = None,
) -> tuple[str, int, dict[str, Any]]:
    fm_block, orig_body = _split_skill_md(original_full)
    _, cand_body = _split_skill_md(candidate_full)
    if not cand_body.strip():
        cand_body = candidate_full
    sanitized_body, short_guard_telemetry = _sanitize_rewritten_body_additive(
        cand_body,
        orig_body or original_full,
    )
    sanitized_body, frontmatter_findings = _strip_nested_skill_frontmatter(sanitized_body)
    portable_body, findings = portable_sanitize_skill_body(
        sanitized_body,
        bundles=bundles,
    )
    portable_body, restored_sections = _restore_missing_original_sections(
        original_body=orig_body or original_full,
        candidate_body=portable_body,
    )
    leak = scan_body_leakage(portable_body, corpus=corpus)
    result = fm_block + portable_body.strip() + "\n" if fm_block else portable_body.strip() + "\n"
    telemetry = {
        **short_guard_telemetry,
        "portable_findings": frontmatter_findings + findings,
        "portable_findings_count": len(frontmatter_findings) + len(findings),
        "restored_original_sections": restored_sections,
        "restored_original_sections_count": len(restored_sections),
    }
    return result, leak + len(frontmatter_findings) + len(findings), telemetry


_AGENT_MODEL_PREFIXES = ("codex", "claude-code", "hermes")


def _make_backend(editor_cfg: dict[str, Any], api_key: str):
    model = (
        editor_cfg.get("litellm_id") or editor_cfg.get("id") or editor_cfg.get("served_id_hint", "")
    )
    if model.split("/", 1)[0] in _AGENT_MODEL_PREFIXES:
        # Coding-agent CLI editor (codex/claude-code/hermes): AgentBackend reads
        # model/effort/timeout from the id + env ladder; litellm api_base/key/
        # token kwargs do not apply.
        from evolution.llm.backend import get_backend

        return get_backend(model)

    from evolution.llm.litellm_backend import LiteLLMBackend

    kwargs: dict[str, Any] = {
        "api_base": editor_cfg.get("api_base", ""),
        "api_key": api_key or "EMPTY",
    }
    if isinstance(editor_cfg.get("extra_body"), dict):
        kwargs["extra_body"] = dict(editor_cfg["extra_body"])
    if editor_cfg.get("max_output_tokens"):
        kwargs["max_tokens"] = int(editor_cfg["max_output_tokens"])
    if editor_cfg.get("temperature") is not None:
        kwargs["temperature"] = float(editor_cfg["temperature"])
    return LiteLLMBackend(model=model, **kwargs)


def _trace_fidelity_enabled(editor_cfg: dict[str, Any]) -> bool:
    return _cfg_bool(editor_cfg.get("single_task_trace_fidelity"), default=False)


def _agent_session_requested(editor_cfg: dict[str, Any]) -> bool:
    """codex/claude full-info+tools mode: a runnable TRAIN workspace instead of a text digest.

    Needs the train materialization info (corpus_root + train_ids) that the loop threads into
    editor_cfg; falls back to file-edit otherwise.
    """
    mode = (
        str(editor_cfg.get("edit_mode") or os.environ.get("EVO_AGENT_EDIT_MODE", ""))
        .strip()
        .lower()
    )
    return (
        mode == "session"
        and bool(editor_cfg.get("train_ids"))
        and bool(editor_cfg.get("corpus_root"))
    )


def _extract_skill_md_candidate(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    candidates: list[str] = []
    for match in re.finditer(
        r"```(?:markdown|md)?\s*(.*?)```",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        block = match.group(1).strip()
        if block.startswith("---"):
            candidates.append(block)
    start = raw.find("---\n")
    if start >= 0:
        block = raw[start:].strip()
        block = re.sub(r"\n```\s*$", "", block).strip()
        if block.startswith("---"):
            candidates.append(block)
    return candidates[-1].strip() + "\n" if candidates else ""


async def _rewrite_via_trace_digest(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    editor_cfg: dict[str, Any],
    api_key: str,
) -> tuple[str, dict[str, Any]]:
    backend = _make_backend(editor_cfg, api_key)
    prompt = (
        f"Rewrite reusable skill {skill_id!r} as a complete Agent Skill "
        "SKILL.md file. The next solver sees only this skill file, not the "
        "train traces or your analysis, so every useful lesson from the trace "
        "must be placed in the skill itself.\n\n"
        "Return only the full SKILL.md text starting with YAML frontmatter. "
        "Do not wrap it in Markdown fences. Do not solve the task directly.\n\n"
        "Trace-fidelity rules:\n"
        "- This is a focused trace-fidelity diagnostic. Preserve concrete input "
        "filenames, output filenames, schema keys, exception messages, and "
        "small code idioms only when they are needed to prevent the observed "
        "failure in the next solve.\n"
        "- For every observed traceback or verifier failure, add a direct "
        "prevention rule and, when useful, a short safe code pattern.\n"
        "- If generated code treated structured JSON/YAML records as scalars, "
        "state exactly how to extract the numeric scalar before arithmetic or "
        "comparisons.\n"
        "- Preserve the runtime path contract from the trace: generated solver "
        "code reads workspace-root inputs through the shown ../ paths and "
        "writes requested artifacts in the current output directory.\n"
        "- Do not copy pytest source, hidden expected answers, benchmark "
        "storage paths, or unrelated harness implementation details.\n\n"
        "# Current SKILL.md\n"
        f"{skill_body}\n\n"
        "# Full Trace Evidence\n"
        f"{traces_digest}"
    )
    resp = await backend.complete(
        [{"role": "user", "content": prompt}],
        system=("You edit reusable procedural skills from execution traces. Output only SKILL.md."),
    )
    content = resp.content.strip()
    candidate = _extract_skill_md_candidate(content) or content.strip() + "\n"
    return candidate, {
        "editor": "evolution_trace_digest_rewrite",
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
        "time_ms": resp.time_ms,
    }


def rewrite_skill_from_trace_digest(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    editor_cfg: dict[str, Any],
    api_key: str,
) -> tuple[str, dict[str, Any]]:
    return asyncio.run(
        _rewrite_via_trace_digest(
            skill_id=skill_id,
            skill_body=skill_body,
            traces_digest=traces_digest,
            editor_cfg=editor_cfg,
            api_key=api_key,
        )
    )


async def _rewrite_via_evolution(
    *,
    skill_id: str,
    skill_body: str,
    bundles: list[dict[str, Any]],
    editor_cfg: dict[str, Any],
    api_key: str,
) -> str:
    import tempfile

    from evolution.evolution.reflect import rewrite_skill

    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / skill_id
        d.mkdir()
        (d / "SKILL.md").write_text(skill_body, encoding="utf-8")
        skill = load_skill(d)
        reflections = bundles_to_reflections(bundles)
        backend = _make_backend(editor_cfg, api_key)
        new_body = await rewrite_skill(
            backend,
            skill,
            reflections,
            task_instruction=bundles[0].get("instruction", "") if bundles else "",
            include_best_trace_code=True,
        )
        write_skill_md(skill.skill_md_path, skill.frontmatter, new_body)
        return skill.skill_md_path.read_text(encoding="utf-8")


def rewrite_skill_from_traces(
    *,
    skill_id: str,
    skill_body: str,
    bundles: list[dict[str, Any]],
    editor_cfg: dict[str, Any],
    api_key: str,
) -> str:
    return asyncio.run(
        _rewrite_via_evolution(
            skill_id=skill_id,
            skill_body=skill_body,
            bundles=bundles,
            editor_cfg=editor_cfg,
            api_key=api_key,
        )
    )


def edit_skill(
    framework: str,
    *,
    skill_id: str,
    skill_body: str,
    bundles: list[dict[str, Any]],
    run_dir: Path,
    editor_cfg: dict[str, Any],
    api_key: str,
    round_idx: int = 0,
    role: str = "",
) -> EditResult:
    """Dispatch to per-framework editor; all return canonical SKILL.md text."""
    fw = framework.strip().lower()
    redact_digest = _cfg_bool(
        editor_cfg.get("redact_trace_digest"),
        default=os.environ.get("EVO_EDITOR_REDACT_TRACE_DIGEST", "1").strip() != "0",
    )
    blind_digest = _cfg_bool(
        editor_cfg.get("blind_trace_digest"),
        default=os.environ.get("EVO_REFLECT_BLIND", "1").strip() != "0",
    )
    digest = render_traces_digest(
        bundles,
        skill_body=skill_body,
        redact=redact_digest,
        blind=blind_digest,
    )
    corpus = "\n".join(b.get("instruction", "") for b in bundles)
    single_task_trace_fidelity = _trace_fidelity_enabled(editor_cfg)

    if fw == "hermes":
        from evolution.adapters.hermes_adapter import edit_skill_from_traces as hermes_edit

        new_body, telemetry = hermes_edit(
            skill_id=skill_id,
            skill_body=skill_body,
            traces_digest=digest,
            bundles=bundles,
            run_dir=run_dir,
            editor_cfg=editor_cfg,
            api_key=api_key,
            round_idx=round_idx,
            role=role,
        )
    elif fw == "evoskill":
        from evolution.adapters.evoskill_adapter import edit_skill_from_traces as evo_edit

        new_body, telemetry = evo_edit(
            skill_id=skill_id,
            skill_body=skill_body,
            traces_digest=digest,
            bundles=bundles,
            run_dir=run_dir,
            editor_cfg=editor_cfg,
            api_key=api_key,
            round_idx=round_idx,
        )
    elif fw == "memento":
        from evolution.adapters.memento_adapter import edit_skill_from_traces as mem_edit

        new_body, telemetry = mem_edit(
            skill_id=skill_id,
            skill_body=skill_body,
            traces_digest=digest,
            bundles=bundles,
            run_dir=run_dir,
            editor_cfg=editor_cfg,
            api_key=api_key,
            round_idx=round_idx,
        )
    elif fw == "memp":
        from evolution.adapters.memp_adapter import edit_skill_from_traces as memp_edit

        new_body, telemetry = memp_edit(
            skill_id=skill_id,
            skill_body=skill_body,
            traces_digest=digest,
            bundles=bundles,
            run_dir=run_dir,
            editor_cfg=editor_cfg,
            api_key=api_key,
            round_idx=round_idx,
        )
    elif fw == "codex":
        if _agent_session_requested(editor_cfg):
            from iterative_evolution.agent_session_edit import run_session_edit

            new_body, telemetry = run_session_edit(
                framework="codex",
                agent_model=os.environ.get("EVO_CODEX_MODEL") or "codex/gpt-5.5",
                skill_id=skill_id,
                skill_body=skill_body,
                run_dir=run_dir,
                editor_cfg=editor_cfg,
                round_idx=round_idx,
                role=role,
            )
        else:
            from evolution.adapters.codex_adapter import edit_skill_from_traces as codex_edit

            new_body, telemetry = codex_edit(
                skill_id=skill_id,
                skill_body=skill_body,
                traces_digest=digest,
                bundles=bundles,
                run_dir=run_dir,
                editor_cfg=editor_cfg,
                api_key=api_key,
                round_idx=round_idx,
                role=role,
            )
    elif fw == "claude":
        if _agent_session_requested(editor_cfg):
            from iterative_evolution.agent_session_edit import run_session_edit

            new_body, telemetry = run_session_edit(
                framework="claude",
                agent_model=os.environ.get("EVO_CLAUDE_MODEL") or "claude-code/claude-sonnet-4-6",
                skill_id=skill_id,
                skill_body=skill_body,
                run_dir=run_dir,
                editor_cfg=editor_cfg,
                round_idx=round_idx,
                role=role,
            )
        else:
            from evolution.adapters.claude_adapter import edit_skill_from_traces as claude_edit

            new_body, telemetry = claude_edit(
                skill_id=skill_id,
                skill_body=skill_body,
                traces_digest=digest,
                bundles=bundles,
                run_dir=run_dir,
                editor_cfg=editor_cfg,
                api_key=api_key,
                round_idx=round_idx,
                role=role,
            )
    elif fw == "evolution":
        if single_task_trace_fidelity:
            new_body, telemetry = rewrite_skill_from_trace_digest(
                skill_id=skill_id,
                skill_body=skill_body,
                traces_digest=digest,
                editor_cfg=editor_cfg,
                api_key=api_key,
            )
        else:
            new_body = rewrite_skill_from_traces(
                skill_id=skill_id,
                skill_body=skill_body,
                bundles=bundles,
                editor_cfg=editor_cfg,
                api_key=api_key,
            )
            telemetry = {"editor": "evolution_rewrite"}
    else:
        raise ValueError(f"unsupported framework for edit_skill: {framework}")

    if _body_fingerprint(new_body) == _body_fingerprint(skill_body):
        # editor no-op: skip finalize so its whitespace normalization can't mint a version
        telemetry = {
            **telemetry,
            "editor_noop": True,
            "trace_digest_redacted": redact_digest,
            "trace_digest_blind": blind_digest,
        }
        return EditResult(new_body=skill_body, changed=False, telemetry=telemetry, leak_findings=0)

    try:
        if single_task_trace_fidelity:
            finalized, leak = finalize_skill_md(
                original_full=skill_body,
                candidate_full=new_body,
                corpus=corpus,
            )
            guard_telemetry = {
                "portable_findings": [],
                "portable_findings_count": 0,
                "restored_original_sections": [],
                "restored_original_sections_count": 0,
                "portability_sanitizer": "disabled_for_single_task_trace_fidelity",
            }
        else:
            finalized, leak, guard_telemetry = finalize_skill_md_with_portability(
                original_full=skill_body,
                candidate_full=new_body,
                corpus=corpus,
                bundles=bundles,
            )
    except ValueError as exc:
        msg = str(exc)
        if "Rewritten skill body is too short" not in msg:
            raise
        finalized = skill_body
        leak = 0
        guard_telemetry = {
            "edit_rejected_by_sanitizer": True,
            "edit_reject_reason": msg,
            "portable_findings": [],
            "portable_findings_count": 0,
            "restored_original_sections": [],
            "restored_original_sections_count": 0,
        }
    telemetry = {
        **telemetry,
        **guard_telemetry,
        "trace_digest_redacted": redact_digest,
        "trace_digest_blind": blind_digest,
    }
    changed = _body_fingerprint(finalized) != _body_fingerprint(skill_body)
    return EditResult(
        new_body=finalized,
        changed=changed,
        telemetry=telemetry,
        leak_findings=leak,
    )


__all__ = [
    "EditResult",
    "edit_skill",
    "finalize_skill_md",
    "finalize_skill_md_with_portability",
    "portable_sanitize_skill_body",
    "rewrite_skill_from_traces",
    "rewrite_skill_from_trace_digest",
]
