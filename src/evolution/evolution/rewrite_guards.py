"""Rewrite selection, size, leakage, and candidate-isolation guards.

These checks are framework safeguards for promoting skill rewrites. They are
kept separate from prompt orchestration so CLI, workflow, and research runners
can share the same rejection rules.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from evolution.core.models import Skill
from evolution.core.parser import parse_skill_md
from evolution.evolution.rewrite_outcome import (
    CANDIDATE_INVALID_EXTRA_FILES,
    CANDIDATE_INVALID_HIDDEN_FILE,
    CANDIDATE_INVALID_PATH_ESCAPE,
    CANDIDATE_INVALID_SYMLINK,
)
from evolution.lineage.traces import TraceStore, load_trace_evidence

LEAK_NGRAM_MIN_CHARS = int(os.environ.get("EVO_LEAK_NGRAM_MIN_CHARS", "50") or "50")
LEAK_CORPUS_MAX_CHARS = int(os.environ.get("EVO_LEAK_CORPUS_MAX_CHARS", "200000") or "200000")

# C1 file-edit candidate isolation.  Default empty = strict: every
# ``.``-prefixed entry is rejected unless explicitly allow-listed after
# Phase 2a observation (plan §1.6).
_ALLOWED_HIDDEN_NAMES: frozenset[str] = frozenset()

# Catch a significant numeric constant in any common surface form so the leak
# scanner can match 5000 ↔ 5e3 ↔ 5_000 ↔ "5,000" after normalization (rev D1).
_NUM_TOKEN = re.compile(
    r"""(?<![\w.])[+-]?
        (?:\d{1,3}(?:[,_]\d{3})+(?:\.\d+)?      # grouped: 5,000 / 5_000 / 1,234.5
          |\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)      # plain/sci/float: 5000 / 5e3 / 5.0
        (?![\w.])""",
    re.VERBOSE,
)
_LEAK_NUM_MIN_ABS = 1000.0


def _normalized_numeric_values(text: str) -> set[float]:
    """Significant (|v|≥1000) numeric constants in ``text``, representation-free."""
    vals: set[float] = set()
    for m in _NUM_TOKEN.finditer(text):
        tok = m.group(0).replace(",", "").replace("_", "")
        try:
            v = float(tok)
        except ValueError:
            continue
        if abs(v) >= _LEAK_NUM_MIN_ABS:
            vals.add(v)
    return vals


def _ast_expected_literals(src: str) -> list[str]:
    """``repr()`` of literal comparison operands (the verifier's expected
    answers); the computed side, where path/filename tokens live, is skipped."""
    out: list[str] = []
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return out

    def _emit(v: object) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, str):
            if len(v) >= 6 and not any(ch.isspace() for ch in v):
                out.append(repr(v))
        elif isinstance(v, (int, float)) and abs(v) >= _LEAK_NUM_MIN_ABS:
            out.append(repr(v))

    def _harvest_operand(node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            _emit(node.value)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for el in node.elts:
                _harvest_operand(el)
        elif isinstance(node, ast.Dict):
            for el in node.values:
                _harvest_operand(el)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            _harvest_operand(operand)
    return out


def actionable_reflections(reflections: list[dict]) -> list[dict] | None:
    failures = [
        r for r in reflections if "error" not in r and str(r.get("kind") or "failure") == "failure"
    ]
    actionable = [
        r
        for r in failures
        if r.get("evidence_sufficiency", "sufficient") != "insufficient"
        and r.get("rewrite_recommended", True) is not False
    ]
    if failures and (not actionable or len(actionable) / len(failures) < 0.5):
        if os.environ.get("EVO_ALLOW_LOWCONF_REWRITE", "").lower() in ("1", "true", "yes"):
            return failures
        return None
    return actionable or failures or None


def actionable_success_reflections(reflections: list[dict]) -> list[dict]:
    """Filter to success-kind reflections that recommend preservation."""
    return [
        r
        for r in reflections
        if "error" not in r
        and str(r.get("kind") or "") == "success"
        and r.get("evidence_sufficiency", "sufficient") != "insufficient"
        and r.get("preserve_recommended", True) is not False
    ]


def baseline_body_for_size(skill: Skill, fallback_body: str) -> str:
    candidates = [
        skill.path / ".evolution" / "versions" / "v1" / "SKILL.md",
        skill.skill_md_path,
    ]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            _fm, body = parse_skill_md(p)
        except Exception:
            body = p.read_text(encoding="utf-8", errors="replace")
        if str(body or "").strip():
            return str(body)
    return fallback_body


def nonempty_line_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def rewrite_size_limits(skill: Skill, original_body: str) -> dict[str, int]:
    baseline = baseline_body_for_size(skill, original_body)
    baseline_chars = max(1, len(baseline))
    baseline_lines = max(1, nonempty_line_count(baseline))
    growth = float(os.environ.get("EVO_REWRITE_MAX_GROWTH", "3.0") or "3.0")
    min_chars = int(os.environ.get("EVO_REWRITE_MIN_CHARS", "12000") or "12000")
    max_chars_abs = int(os.environ.get("EVO_REWRITE_MAX_CHARS", "30000") or "30000")
    min_lines = int(os.environ.get("EVO_REWRITE_MIN_LINES", "300") or "300")
    max_lines_abs = int(os.environ.get("EVO_REWRITE_MAX_LINES", "1200") or "1200")
    return {
        "baseline_chars": baseline_chars,
        "baseline_lines": baseline_lines,
        "max_chars": min(max_chars_abs, max(min_chars, int(baseline_chars * growth))),
        "max_lines": min(max_lines_abs, max(min_lines, int(baseline_lines * growth))),
        "hard_max_chars": max_chars_abs,
        "hard_max_lines": max_lines_abs,
    }


def size_violations(body: str, limits: dict[str, int]) -> list[str]:
    issues: list[str] = []
    if len(body) > limits["max_chars"]:
        issues.append(f"{len(body)} chars > cap {limits['max_chars']}")
    lines = nonempty_line_count(body)
    if lines > limits["max_lines"]:
        issues.append(f"{lines} non-empty lines > cap {limits['max_lines']}")
    return issues


def hard_size_violations(body: str, limits: dict[str, int]) -> list[str]:
    issues: list[str] = []
    if len(body) > limits["hard_max_chars"]:
        issues.append(f"{len(body)} chars > absolute ceiling {limits['hard_max_chars']}")
    lines = nonempty_line_count(body)
    if lines > limits["hard_max_lines"]:
        issues.append(f"{lines} non-empty lines > absolute ceiling {limits['hard_max_lines']}")
    return issues


def leakage_provenance_corpus(skill: Skill, task_filter: str | None) -> str:
    parts: list[str] = []
    try:
        store = TraceStore(skill)
        traces = store.list(task=task_filter) if task_filter else store.list()
        for t in traces:
            tdir = store.traces_dir / t.get("id", "")
            ev = load_trace_evidence(tdir)
            if not ev:
                continue
            # Only reflection-eligible traces (exclude promotion_validation),
            # mirroring reflect_failing_traces' phase filter.
            if (ev.get("trace_context") or {}).get("phase") == "promotion_validation":
                continue
            arts = ev.get("produced_artifacts")
            if isinstance(arts, list):
                for a in arts:
                    if isinstance(a, dict):
                        parts.append(str(a.get("path", "")))
                        parts.append(str(a.get("suffix", "")))
            fe = ev.get("failure_evidence") or {}
            sf = fe.get("source_feedback")
            if isinstance(sf, dict):
                for _k in ("stderr_tail", "pytest_error_tail"):
                    if sf.get(_k):
                        parts.append(str(sf[_k]))
            # Oracle (v5): harvest only asserted expected values, not raw test
            # scaffolding, so structural tokens aren't mistaken for leaks (D3).
            oe = fe.get("oracle_evidence")
            if isinstance(oe, dict):
                src = oe.get("tests_source")
                if src:
                    parts.extend(_ast_expected_literals(str(src)))
                for ft in oe.get("failed_tests") or []:
                    if isinstance(ft, dict) and ft.get("nodeid"):
                        parts.append(str(ft["nodeid"]))
                for a in oe.get("source_artifacts") or []:
                    if isinstance(a, dict) and a.get("content"):
                        parts.append(str(a["content"]))
            if sum(len(p) for p in parts) >= LEAK_CORPUS_MAX_CHARS:
                break
    except Exception:
        return "\n".join(parts)[:LEAK_CORPUS_MAX_CHARS]
    return "\n".join(parts)[:LEAK_CORPUS_MAX_CHARS]


def scan_leakage(
    body: str,
    corpus: str,
    harness_names: set[str] | None = None,
) -> list[str]:
    findings: list[str] = []
    if not body:
        return findings

    env_names = {s.strip() for s in os.environ.get("EVO_LEAK_HARNESS_NAMES", "").split(",")}
    harness = {s for s in (harness_names or set()) | env_names if s}
    for name in sorted(harness):
        if name in body:
            findings.append(f"known train-harness identifier: {name}")

    if corpus:
        corpus_lower = corpus.lower()
        body_idents = set(re.findall(r"\b(?:test_|assert_)\w+", body))
        for ident in sorted(body_idents):
            # Case-insensitive match so Test_Foo↔test_foo is still caught (rev D1).
            if (
                ident.lower() in corpus_lower
                and f"known train-harness identifier: {ident}" not in findings
            ):
                findings.append(f"verifier identifier with trace provenance: {ident}")

        for raw in body.splitlines():
            line = raw.strip()
            if len(line) < LEAK_NGRAM_MIN_CHARS:
                continue
            if line.startswith(("#", "```", "<!--", "|")):
                continue
            if line in corpus:
                snip = line if len(line) <= 80 else line[:80] + "..."
                findings.append(f"verbatim span copied from trace evidence: {snip!r}")

        quoted = set(re.findall(r"""['"]([^'"\n]{6,})['"]""", corpus))
        for lit in sorted(quoted):
            if lit and lit in body:
                findings.append(f"expected-constant literal with trace provenance: {lit!r}")

        corpus_nums = _normalized_numeric_values(corpus)
        if corpus_nums:
            for v in sorted(corpus_nums & _normalized_numeric_values(body)):
                shown = int(v) if v.is_integer() else v
                findings.append(f"expected-constant numeric with trace provenance: {shown!r}")

    seen: set[str] = set()
    uniq: list[str] = []
    for finding in findings:
        if finding not in seen:
            seen.add(finding)
            uniq.append(finding)
    return uniq[:25]


def validate_candidate_isolation(
    candidate_dir: Path,
    *,
    allowed_extra: set[str] | None = None,
) -> tuple[bool, str, str]:
    """Walk ``candidate_dir`` and reject anything outside the isolation contract.

    Returns ``(ok, candidate_status, reason)``.  On success, ``ok=True``
    and the status / reason strings are empty.  On rejection, ``ok=False``
    and ``candidate_status`` is one of the ``CANDIDATE_INVALID_*``
    constants from :mod:`evolution.evolution.rewrite_outcome`.

    Rules (plan §1.6):

    * No symlinks (``Path.is_symlink()``).
    * No hidden files (``name.startswith(".")``) unless
      ``name in _ALLOWED_HIDDEN_NAMES``.
    * No files outside ``{"SKILL.md"} ∪ allowed_extra``.
    * No path that resolves outside ``candidate_dir.resolve()``.
    """
    allowed = {"SKILL.md"} | (allowed_extra or set())
    candidate_root = candidate_dir.resolve()
    if not candidate_root.is_dir():
        return (
            False,
            CANDIDATE_INVALID_PATH_ESCAPE,
            f"candidate_dir is not a directory: {candidate_dir}",
        )

    for entry in candidate_dir.rglob("*"):
        if entry.is_symlink():
            return (
                False,
                CANDIDATE_INVALID_SYMLINK,
                f"symlink in candidate: {entry.relative_to(candidate_dir)}",
            )
        try:
            resolved = entry.resolve()
        except (OSError, RuntimeError) as exc:
            return (
                False,
                CANDIDATE_INVALID_PATH_ESCAPE,
                f"failed to resolve {entry.relative_to(candidate_dir)}: {exc!s}",
            )
        # ``Path.is_relative_to`` is the Python 3.9+ equivalent; use string
        # prefix only as a fallback for older runtimes (project supports 3.10+).
        if candidate_root != resolved and not resolved.is_relative_to(candidate_root):
            return (
                False,
                CANDIDATE_INVALID_PATH_ESCAPE,
                f"path escapes candidate root: {entry.relative_to(candidate_dir)}",
            )
        name = entry.name
        if name.startswith("."):
            if name not in _ALLOWED_HIDDEN_NAMES:
                return (
                    False,
                    CANDIDATE_INVALID_HIDDEN_FILE,
                    f"unknown hidden entry: {entry.relative_to(candidate_dir)}",
                )
            continue
        if entry.is_file() and name not in allowed:
            return (
                False,
                CANDIDATE_INVALID_EXTRA_FILES,
                f"unexpected file in candidate: {entry.relative_to(candidate_dir)}",
            )
    return True, "", ""
