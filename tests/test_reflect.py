"""Tests for reflection metadata and strict failure behavior."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from evolution.core.models import Skill, SkillFrontmatter, SkillTrace
from evolution.evolution.reflect import (
    reflect_failing_traces,
    reflect_trace,
    rewrite_skill,
)
from evolution.lineage.traces import TraceStore
from evolution.llm.mock_backend import MockBackend
from evolution.splits import SplitManifest


def _reflection_json() -> str:
    return json.dumps(
        {
            "root_cause": "wrong output",
            "skill_gap": "missing validation",
            "suggested_fix": "use a general validation step",
            "severity": "major",
            "pattern": "logic_error",
        }
    )


def _make_skill(tmp_path: Path) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nUse Python carefully.\n",
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body="Use Python carefully.",
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _write_split_manifest(tmp_path: Path, *, task_id: str = "task-name") -> SplitManifest:
    path = tmp_path / "split_manifest.json"
    path.write_text(
        json.dumps(
            {
                "name": "unit-split",
                "tasks": [{"id": task_id, "split": "train"}],
            }
        ),
        encoding="utf-8",
    )
    return SplitManifest.load(path)


def _v3_evidence() -> dict:
    return {
        "schema": "evolution-trace-evidence-v3",
        "agent_solve": {
            "exit_code": 1,
            "timeout": False,
            "diagnostic": "nonzero_exit",
        },
        "produced_artifacts": [],
        "failure_evidence": {
            "failure_categories": ["no_artifact"],
            "produced_outputs": [],
            "verifier_result": {"passed": 0, "total": 1, "verification_category": "no_artifact"},
            "solve_exception": None,
            "code_generation_status": "ok",
            "required_output_count": 0,
            "required_output_suffixes": [],
        },
        "trace_context": {"trace_spec_idx": 0, "attempt_seed": 0},
    }


def _save_failed_trace(skill: Skill) -> Path:
    trace = SkillTrace(
        id="trace-1",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
    )
    return TraceStore(skill).save(
        trace,
        prompt="prompt",
        response="response",
        code="print('wrong')",
        evidence=_v3_evidence(),
    )


def _save_success_trace(skill: Skill, *, code: str) -> Path:
    trace = SkillTrace(
        id="success-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=1,
        total=1,
        pass_rate=1.0,
        success=True,
        errors=[],
    )
    return TraceStore(skill).save(
        trace,
        prompt="prompt",
        response="response",
        code=code,
        evidence=_v3_evidence(),
    )


def test_reflect_trace_records_reflector_model(tmp_path: Path):
    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    backend = MockBackend(
        responses=[
            json.dumps(
                {
                    "root_cause": "wrong output",
                    "skill_gap": "missing validation",
                    "suggested_fix": "check the expected file",
                    "severity": "major",
                    "pattern": "logic_error",
                }
            )
        ]
    )
    backend.model_label = "mock/reflector"  # type: ignore[attr-defined]

    reflection = asyncio.run(reflect_trace(backend, trace_dir, skill, "task"))

    assert reflection["model"] == "solver-model"
    assert reflection["reflector_model"] == "mock/reflector"
    assert reflection["tokens_used"] == 150


def test_reflect_failing_traces_strict_rejects_parse_error(tmp_path: Path):
    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    backend = MockBackend(responses=["not json"])

    with pytest.raises(ValueError, match="reflection JSON parse failed"):
        reflect_failing_traces(
            backend,
            skill,
            "task",
            task_filter="task-name",
            strict=True,
        )

    assert not (trace_dir / "reflection.json").exists()


def test_rewrite_skill_compacts_oversized_output(monkeypatch, tmp_path: Path):
    skill = _make_skill(tmp_path)
    monkeypatch.setenv("EVO_REWRITE_MIN_CHARS", "100")
    monkeypatch.setenv("EVO_REWRITE_MAX_CHARS", "10000")
    monkeypatch.setenv("EVO_REWRITE_MAX_GROWTH", "1")
    monkeypatch.setenv("EVO_REWRITE_MIN_LINES", "3")
    monkeypatch.setenv("EVO_REWRITE_MAX_LINES", "3")

    oversized = "\n".join(f"Operational line {i}" for i in range(10))
    compact = "Use Python carefully.\nValidate outputs.\nAvoid speculative steps."
    backend = MockBackend(responses=[oversized, compact])

    body = asyncio.run(
        rewrite_skill(
            backend,
            skill,
            [
                {
                    "root_cause": "wrong output",
                    "skill_gap": "missing validation",
                    "suggested_fix": "validate output files",
                    "pattern": "logic_error",
                    "pass_rate": 0.0,
                    "model": "solver",
                }
            ],
            task_instruction="task",
            task_filter="task-name",
        )
    )

    assert body == compact
    assert len(backend.calls) == 2


def test_rewrite_accepts_compacted_body_over_soft_cap_under_absolute(monkeypatch, tmp_path: Path):
    """A post-compaction body slightly over the derived cap but within the
    absolute ceiling must be ACCEPTED, not raise (regression: a 3% near-miss
    aborted the sql Tables-3/4 pilot)."""
    skill = _make_skill(tmp_path)
    monkeypatch.setenv("EVO_REWRITE_MIN_CHARS", "10")
    monkeypatch.setenv("EVO_REWRITE_MAX_CHARS", "100000")
    monkeypatch.setenv("EVO_REWRITE_MAX_GROWTH", "1")
    monkeypatch.setenv("EVO_REWRITE_MIN_LINES", "3")
    monkeypatch.setenv("EVO_REWRITE_MAX_LINES", "50")  # absolute ceiling

    oversized = "\n".join(f"Operational line {i}" for i in range(40))
    # 8 non-empty lines: over the derived soft cap (3) but well under the
    # absolute ceiling (50) — must be accepted.
    compact = "\n".join(f"Concise rule {i}" for i in range(8))
    backend = MockBackend(responses=[oversized, compact])

    body = asyncio.run(
        rewrite_skill(
            backend,
            skill,
            [
                {
                    "root_cause": "wrong output",
                    "skill_gap": "missing validation",
                    "suggested_fix": "validate output files",
                    "pattern": "logic_error",
                    "pass_rate": 0.0,
                    "model": "solver",
                }
            ],
            task_instruction="task",
            task_filter="task-name",
        )
    )

    assert body == compact
    assert len(backend.calls) == 2


def test_rewrite_raises_when_compacted_body_breaches_absolute_ceiling(monkeypatch, tmp_path: Path):
    """Genuine runaway bloat (post-compaction body over the ABSOLUTE ceiling)
    must still hard-fail."""
    skill = _make_skill(tmp_path)
    monkeypatch.setenv("EVO_REWRITE_MIN_CHARS", "10")
    monkeypatch.setenv("EVO_REWRITE_MAX_CHARS", "100000")
    monkeypatch.setenv("EVO_REWRITE_MAX_GROWTH", "1")
    monkeypatch.setenv("EVO_REWRITE_MIN_LINES", "3")
    monkeypatch.setenv("EVO_REWRITE_MAX_LINES", "10")  # absolute ceiling

    oversized = "\n".join(f"Operational line {i}" for i in range(40))
    # 25 non-empty lines: over the absolute ceiling (10) even after compaction.
    still_huge = "\n".join(f"Still verbose line {i}" for i in range(25))
    backend = MockBackend(responses=[oversized, still_huge])

    with pytest.raises(ValueError, match="absolute size ceiling"):
        asyncio.run(
            rewrite_skill(
                backend,
                skill,
                [
                    {
                        "root_cause": "wrong output",
                        "skill_gap": "missing validation",
                        "suggested_fix": "validate output files",
                        "pattern": "logic_error",
                        "pass_rate": 0.0,
                        "model": "solver",
                    }
                ],
                task_instruction="task",
                task_filter="task-name",
            )
        )


def _save_evidence(trace_dir: Path, evidence: dict) -> None:
    (trace_dir / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_reflect_prompt_and_system_have_observable_directives():
    from evolution.evolution.reflect import REFLECT_PROMPT, REFLECT_SYSTEM

    # v3 metadata-only blocks
    assert "## Agent Solve Diagnostics" in REFLECT_PROMPT
    assert "## Produced Artifact Metadata" in REFLECT_PROMPT
    assert "{solve_exit_code}" in REFLECT_PROMPT
    assert "{solve_timeout}" in REFLECT_PROMPT
    assert "{solve_diagnostic}" in REFLECT_PROMPT
    assert "{produced_artifacts}" in REFLECT_PROMPT
    # verifier/text channels must be GONE
    assert "{pytest_output}" not in REFLECT_PROMPT
    assert "{failed_tests}" not in REFLECT_PROMPT
    assert "{solve_output}" not in REFLECT_PROMPT
    assert "{errors}" not in REFLECT_PROMPT
    assert "pytest output" not in REFLECT_PROMPT

    # split-controlled policy: sanitized source feedback allowed, oracle blocked
    assert "sanitized execution" in REFLECT_SYSTEM
    assert "NEVER given verifier source" in REFLECT_SYSTEM
    assert "pytest node ids" in REFLECT_SYSTEM
    assert "transfer across related tasks" in REFLECT_SYSTEM
    assert "NEVER recommend copying test code" in REFLECT_SYSTEM


def test_reflect_trace_v3_metadata_only_and_ignores_stale_text(tmp_path: Path):
    """v3 metadata appears; stale text keys (pytest_output/solve_output/
    failed_tests/preview) must NOT leak into the prompt even if present."""
    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    _save_evidence(
        trace_dir,
        {
            "schema": "evolution-trace-evidence-v3",
            "agent_solve": {
                "exit_code": 1,
                "timeout": False,
                "diagnostic": "nonzero_exit",
            },
            "produced_artifacts": [
                {
                    "path": "out.csv",
                    "status": "created",
                    "size": 12,
                    "sha256": "deadbeefcafebabe",
                    "suffix": ".csv",
                }
            ],
            "failure_evidence": {
                "failure_categories": ["produced_but_failed_verification"],
                "produced_outputs": [{"path": "out.csv", "suffix": ".csv", "shape": {"n_cols": 2}}],
                "verifier_result": {
                    "passed": 0,
                    "total": 1,
                    "verification_category": "produced_but_failed_verification",
                },
                "code_generation_status": "ok",
                "solve_exception": None,
                "required_output_count": 1,
                "required_output_suffixes": [".csv"],
            },
            "trace_context": {"trace_spec_idx": 0, "attempt_seed": 1},
            # Stale/unsafe v1 fields that MUST be ignored:
            "pytest_output": "PYTEST_SENTINEL_XYZ assertion 7 != 42",
            "failed_tests": ["tests/test_outputs.py::test_value"],
            "solve_output": "SOLVE_SENTINEL_ABC traceback",
        },
    )
    backend = MockBackend(responses=[_reflection_json()])

    reflection = asyncio.run(reflect_trace(backend, trace_dir, skill, "task"))

    assert "parse_error" not in reflection
    prompt = backend.calls[0]["messages"][0]["content"]
    # sanitized metadata present
    assert "nonzero_exit" in prompt
    assert ".csv" in prompt  # suffix allowed
    assert "produced_but_failed_verification" in prompt  # failure category
    # canonical leak rule: no literal paths or content hashes in the prompt
    assert "out.csv" not in prompt
    assert "deadbeefcafe" not in prompt
    # stale unsafe text channels absent
    assert "PYTEST_SENTINEL_XYZ" not in prompt
    assert "SOLVE_SENTINEL_ABC" not in prompt
    assert "tests/test_outputs.py::test_value" not in prompt


def test_reflect_trace_withholds_invalid_artifact_strings_and_notes(tmp_path: Path):
    """Stale malformed artifact fields must not become a text side-channel."""
    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    _save_evidence(
        trace_dir,
        {
            "schema": "evolution-trace-evidence-v3",
            "agent_solve": {"exit_code": 1, "timeout": False, "diagnostic": "nonzero_exit"},
            "produced_artifacts": "STALE_PREVIEW_SENTINEL expected=42",
        },
    )
    backend = MockBackend(responses=[_reflection_json()])

    asyncio.run(reflect_trace(backend, trace_dir, skill, "task"))

    prompt = backend.calls[0]["messages"][0]["content"]
    assert "STALE_PREVIEW_SENTINEL" not in prompt
    assert "artifact metadata withheld" in prompt

    backend = MockBackend(responses=[_reflection_json()])
    _save_evidence(
        trace_dir,
        {
            "schema": "evolution-trace-evidence-v3",
            "agent_solve": {"exit_code": 1, "timeout": False, "diagnostic": "nonzero_exit"},
            "produced_artifacts": [{"note": "NOTE_SENTINEL expected=42"}],
        },
    )

    asyncio.run(reflect_trace(backend, trace_dir, skill, "task"))

    prompt = backend.calls[0]["messages"][0]["content"]
    assert "NOTE_SENTINEL" not in prompt
    assert "artifact note withheld" in prompt


def test_reflect_trace_no_evidence_renders_marker_and_succeeds(tmp_path: Path):
    # Per-trace reflect_trace is fail-soft on a missing evidence packet (the
    # round-level provenance gate lives in reflect_failing_traces, not here).
    # Save WITHOUT evidence so the marker path is exercised.
    skill = _make_skill(tmp_path)
    trace = SkillTrace(
        id="trace-1",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
    )
    trace_dir = TraceStore(skill).save(trace, prompt="p", response="r", code="x")
    assert not (trace_dir / "evidence.json").exists()
    backend = MockBackend(responses=[_reflection_json()])

    reflection = asyncio.run(reflect_trace(backend, trace_dir, skill, "task"))

    assert "parse_error" not in reflection
    assert reflection["model"] == "solver-model"
    prompt = backend.calls[0]["messages"][0]["content"]
    assert "(unavailable" in prompt


def test_reflect_trace_does_not_read_task_oracle_files(tmp_path: Path):
    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    # Plant oracle-shaped files next to the trace and in a sibling task dir.
    task_dir = tmp_path / "tasks" / "task-name"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "tests" / "test_outputs.py").write_text(
        "ORACLE_TEST_SENTINEL = 'do_not_leak'\n", encoding="utf-8"
    )
    (task_dir / "expected_output.json").write_text(
        '{"answer": "EXPECTED_SENTINEL_LEAK"}', encoding="utf-8"
    )
    (trace_dir / "expected_output.json").write_text(
        '{"answer": "TRACE_EXPECTED_SENTINEL"}', encoding="utf-8"
    )
    backend = MockBackend(responses=[_reflection_json()])

    asyncio.run(reflect_trace(backend, trace_dir, skill, "task"))

    prompt = backend.calls[0]["messages"][0]["content"]
    assert "ORACLE_TEST_SENTINEL" not in prompt
    assert "EXPECTED_SENTINEL_LEAK" not in prompt
    assert "TRACE_EXPECTED_SENTINEL" not in prompt


def test_rewrite_prompt_is_transfer_safe_with_size_budget():
    from evolution.evolution.reflect import (
        COMPACT_PROMPT,
        REWRITE_PROMPT,
        REWRITE_SYSTEM,
    )

    # generalization + anti-leakage + in-prompt size budget
    assert "GENERAL, transferable procedure" in REWRITE_PROMPT
    assert "DO NOT embed or paraphrase the task's tests/verifier/assertions" in REWRITE_PROMPT
    assert "## Size Budget (HARD)" in REWRITE_PROMPT
    assert "{max_chars}" in REWRITE_PROMPT and "{max_lines}" in REWRITE_PROMPT
    assert "NEVER embed the task's test harness, verifier" in REWRITE_SYSTEM
    # old overfitting framing removed
    assert 'Add a "Do NOT" section listing the specific wrong approaches' not in REWRITE_PROMPT
    assert "Include complete, working code patterns — not pseudocode" not in REWRITE_PROMPT
    # compaction also strips leakage
    assert "Strip any embedded test/verifier/assertion text" in COMPACT_PROMPT


def test_rewrite_prompt_size_budget_is_populated(monkeypatch, tmp_path: Path):
    """The numeric budget must actually reach the rewrite prompt (guards the
    move of _rewrite_size_limits before REWRITE_PROMPT.format)."""
    skill = _make_skill(tmp_path)
    monkeypatch.setenv("EVO_REWRITE_MIN_CHARS", "777")
    monkeypatch.setenv("EVO_REWRITE_MAX_CHARS", "777")
    monkeypatch.setenv("EVO_REWRITE_MIN_LINES", "55")
    monkeypatch.setenv("EVO_REWRITE_MAX_LINES", "55")
    monkeypatch.setenv("EVO_REWRITE_MAX_GROWTH", "1")
    backend = MockBackend(responses=["A general, parameterized procedure.\nStep one.\nStep two."])

    asyncio.run(
        rewrite_skill(
            backend,
            skill,
            [
                {
                    "root_cause": "wrong output",
                    "skill_gap": "missing validation",
                    "suggested_fix": "use a general validation step",
                    "pattern": "logic_error",
                    "pass_rate": 0.0,
                    "model": "solver",
                }
            ],
            task_instruction="task",
            task_filter="task-name",
        )
    )

    rewrite_prompt = backend.calls[0]["messages"][0]["content"]
    assert "Maximum characters: 777" in rewrite_prompt
    assert "Maximum non-empty lines: 55" in rewrite_prompt
    assert "Size Budget (HARD)" in rewrite_prompt


def test_rewrite_best_trace_code_is_optional(tmp_path: Path):
    skill = _make_skill(tmp_path)
    _save_success_trace(skill, code="print('BEST_TRACE_SENTINEL')")
    reflections = [
        {
            "root_cause": "wrong output",
            "skill_gap": "missing validation",
            "suggested_fix": "use a general validation step",
            "pattern": "logic_error",
            "pass_rate": 0.0,
            "model": "solver",
        }
    ]

    backend = MockBackend(responses=["A general, parameterized procedure.\nStep one.\nStep two."])
    asyncio.run(
        rewrite_skill(
            backend,
            skill,
            reflections,
            task_instruction="task",
            task_filter="task-name",
            include_best_trace_code=False,
        )
    )
    assert "BEST_TRACE_SENTINEL" not in backend.calls[0]["messages"][0]["content"]

    backend = MockBackend(responses=["A general, parameterized procedure.\nStep one.\nStep two."])
    asyncio.run(
        rewrite_skill(
            backend,
            skill,
            reflections,
            task_instruction="task",
            task_filter="task-name",
            include_best_trace_code=True,
        )
    )
    assert "BEST_TRACE_SENTINEL" in backend.calls[0]["messages"][0]["content"]


def test_rewrite_cli_manifest_defaults_no_best_trace_code(monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from evolution.cli import main as cli

    skill = _make_skill(tmp_path)
    manifest_path = tmp_path / "split_manifest.json"
    manifest_path.write_text(
        json.dumps({"name": "unit-split", "tasks": [{"id": "task-name", "split": "train"}]}),
        encoding="utf-8",
    )
    captured: list[bool] = []

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    from evolution.evolution.rewrite_outcome import NO_OP_TEXT, RewriteOutcome

    async def fake_rewrite_with_audit(*args, **kwargs):
        captured.append(kwargs["include_best_trace_code"])
        return RewriteOutcome(
            body=skill.body,
            candidate_status=NO_OP_TEXT,
            reason="test fake",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="fake/test",
            parent_body=skill.body,
            candidate_body_raw=None,
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection_json()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_rewrite_with_audit)

    runner = CliRunner()
    no_manifest = runner.invoke(cli.app, ["rewrite", skill.name])
    assert no_manifest.exit_code == 0, no_manifest.output
    with_manifest = runner.invoke(
        cli.app, ["rewrite", skill.name, "--manifest", str(manifest_path)]
    )
    assert with_manifest.exit_code == 0, with_manifest.output

    assert captured == [True, False]


def test_scan_leakage_is_provenance_aware(monkeypatch):
    from evolution.evolution.rewrite_guards import scan_leakage as _scan_leakage

    monkeypatch.setenv("EVO_LEAK_HARNESS_NAMES", "assert_real_optimization")
    corpus = (
        "FAILED tests/test_outputs.py::test_daily_totals\n"
        "assert df['daily_total'].sum() == 123456789\n"
        "AssertionError: expected the exact OEWN sense join shape\n"
    )
    # Reproduced-from-evidence span + verifier identifier + harness name → flagged
    leaked = (
        "Use a general approach.\n"
        "assert df['daily_total'].sum() == 123456789\n"  # verbatim from corpus
        "Call assert_real_optimization(conn, a, b) to be safe.\n"  # harness name
    )
    f = _scan_leakage(leaked, corpus)
    assert any("known train-harness identifier" in x for x in f)
    # the expected constant 123456789 has trace provenance and is reproduced
    assert any("123456789" in x for x in f)

    # Same-length generic guidance with NO evidence provenance → NOT flagged
    clean = (
        "Always validate the output schema against the task's stated columns.\n"
        "Prefer set-based SQL over row-by-row Python loops for large joins.\n"
    )
    assert _scan_leakage(clean, corpus) == []
    # Empty corpus: only configured harness names can fire without provenance.
    assert _scan_leakage("plain general guidance, no harness", "") == []


def test_scan_leakage_allows_legitimate_api_names_in_rewrite():
    """Library API names appearing in failure stderr must not be flagged.

    Regression for the t34v7 over-rejection: codex rewrites teaching
    `Page.search_for` for PyMuPDF were being killed because the stderr
    happened to mention the API. The big_tokens rule (8+ char identifier
    catch-all) is removed; rules 1-4 still catch verifier names, expected
    constants, harness names, and verbatim-span leaks.
    """
    from evolution.evolution.rewrite_guards import scan_leakage

    corpus = (
        "TypeError: search_for() got unexpected keyword argument 'hit_max'\n"
        "Processing template index 2 raised KeyError\n"
    )
    rewritten = (
        "## Searching\nUse Page.search_for(text) to locate matches.\n"
        "Processing flow: parse, anonymise, write to output/.\n"
    )
    # No quoted literals, no test_/assert_ prefix, no 50+ char verbatim span
    # ⇒ legitimate teaching content, no findings.
    assert scan_leakage(rewritten, corpus) == []


def test_scan_leakage_still_flags_quoted_expected_constants():
    """Rule 4 (quoted-literal) catches actual expected-value leaks."""
    from evolution.evolution.rewrite_guards import scan_leakage

    corpus = "Expected 'DriversLicense' field, got 'CANDIDATE_NAME'\n"
    rewritten = "Always handle the DriversLicense field on the form.\n"
    findings = scan_leakage(rewritten, corpus)
    assert any("DriversLicense" in f for f in findings)


def test_rewrite_keeps_parent_on_leak_without_cleanup_retry(monkeypatch, tmp_path: Path):
    """A leaked rewrite is rejected without sending leaked text back to a model."""
    skill = _make_skill(tmp_path)
    _save_failed_trace(skill)
    monkeypatch.setenv("EVO_LEAK_HARNESS_NAMES", "assert_real_optimization")
    # v3 evidence is metadata-only (no text provenance). The deterministic
    # leak signal is a known train-harness identifier, which the scanner
    # flags provenance-independently.
    leaked_draft = (
        "General intro line that is sufficiently long to be a real body.\n"
        "Call assert_real_optimization(conn, original, candidate) here.\n"
        "More general guidance that is also sufficiently long here.\n"
    )
    backend = MockBackend(responses=[leaked_draft])

    body = asyncio.run(
        rewrite_skill(
            backend,
            skill,
            [
                {
                    "root_cause": "wrong output",
                    "skill_gap": "missing validation",
                    "suggested_fix": "use a general validation step",
                    "pattern": "logic_error",
                    "pass_rate": 0.0,
                    "model": "solver-model",
                }
            ],
            task_instruction="task",
            task_filter="task-name",
        )
    )

    assert body == skill.body
    assert "assert_real_optimization" not in body
    assert len(backend.calls) == 1


def test_reflect_failing_traces_provenance_gate_rejects_non_v3_trace(tmp_path: Path):
    """v3 gate is decoupled from ``strict``: it raises ONLY under
    ``require_v3_evidence=True`` (paper/produce path). Ad-hoc CLI
    (``strict=True``, no require flag) on an old/sparse trace must NOT raise."""
    skill = _make_skill(tmp_path)
    trace = SkillTrace(
        id="stale-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
    )
    # Saved WITHOUT evidence (mimics an imported/preloaded trace).
    TraceStore(skill).save(trace, prompt="p", response="r", code="x")
    backend = MockBackend(responses=[_reflection_json(), _reflection_json()])

    # strict=True but require_v3_evidence not set: must NOT raise (decoupled).
    out = reflect_failing_traces(backend, skill, "task", task_filter="task-name", strict=True)
    assert isinstance(out, list)

    # Paper path: require_v3_evidence=True rejects a trace with no evidence.
    skill2 = _make_skill(tmp_path / "s2")
    TraceStore(skill2).save(trace, prompt="p", response="r", code="x")
    with pytest.raises(ValueError, match="provenance gate"):
        reflect_failing_traces(
            backend,
            skill2,
            "task",
            task_filter="task-name",
            strict=True,
            require_v3_evidence=True,
        )

    # Wrong-schema (v1) evidence is also rejected under require_v3_evidence.
    trace_dir = TraceStore(skill2).traces_dir / "stale-trace"
    (trace_dir / "evidence.json").write_text(
        json.dumps({"schema": "evolution-trace-evidence-v1"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="provenance gate"):
        reflect_failing_traces(
            backend,
            skill2,
            "task",
            task_filter="task-name",
            strict=True,
            require_v3_evidence=True,
        )


def test_reflect_failing_traces_requires_active_manifest_provenance(tmp_path: Path):
    """With a split manifest, v3 evidence alone is not enough: the trace must
    also be stamped with the same manifest SHA and evolution scope."""
    manifest = _write_split_manifest(tmp_path)
    skill = _make_skill(tmp_path)
    trace = SkillTrace(
        id="unscoped-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
    )
    TraceStore(skill).save(trace, prompt="p", response="r", code="x", evidence=_v3_evidence())
    backend = MockBackend(responses=[_reflection_json()])

    with pytest.raises(ValueError, match="active reflection manifest"):
        reflect_failing_traces(
            backend,
            skill,
            "task",
            task_filter="task-name",
            manifest=manifest,
            strict=True,
        )


def test_reflect_failing_traces_accepts_matching_manifest_provenance(tmp_path: Path):
    manifest = _write_split_manifest(tmp_path)
    skill = _make_skill(tmp_path)
    trace = SkillTrace(
        id="fresh-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
        manifest_name=manifest.name,
        manifest_sha=manifest.sha256,
        manifest_scope="skill_evolution",
    )
    TraceStore(skill).save(trace, prompt="p", response="r", code="x", evidence=_v3_evidence())
    backend = MockBackend(responses=[_reflection_json()])

    reflections = reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="task-name",
        manifest=manifest,
        strict=True,
    )

    assert len(reflections) == 1
    assert (TraceStore(skill).traces_dir / "fresh-trace" / "reflection.json").is_file()


def test_reflect_failing_traces_rejects_wrong_manifest_sha(tmp_path: Path):
    manifest = _write_split_manifest(tmp_path)
    skill = _make_skill(tmp_path)
    trace = SkillTrace(
        id="wrong-manifest-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
        manifest_name=manifest.name,
        manifest_sha="0" * 64,
        manifest_scope="skill_evolution",
    )
    TraceStore(skill).save(trace, prompt="p", response="r", code="x", evidence=_v3_evidence())
    backend = MockBackend(responses=[_reflection_json()])

    with pytest.raises(ValueError, match="active reflection manifest"):
        reflect_failing_traces(
            backend,
            skill,
            "task",
            task_filter="task-name",
            manifest=manifest,
            strict=True,
        )


# ── reflector parity additions (cross-round memory, success branch, ──────────
#    sibling digests, agent-visible workspace view) ──────────────────────────


def _success_reflection_json() -> str:
    return json.dumps(
        {
            "reinforcement_pattern": "step_sequencing",
            "helpful_sections": ["## Validate Inputs", "## Use the Right API"],
            "transferability": "broad",
            "evidence_sufficiency": "sufficient",
            "preserve_recommended": True,
        }
    )


def test_meta_skill_blob_distills_prior_rounds(tmp_path: Path):
    from evolution.evolution.meta_skill import build_meta_skill, write_round_record

    skill = _make_skill(tmp_path)
    write_round_record(
        skill.path,
        {
            "timestamp": "20260601_120000_000001",
            "skill_name": skill.name,
            "parent_version": "v1",
            "candidate_version": "v2",
            "gate_status": "rolled_back",
            "delta": -0.05,
            "n_failure_reflections": 3,
            "n_success_reflections": 0,
            "dominant_failure_pattern": "wrong_api",
            "reason": "candidate underperformed parent on validation",
        },
    )
    write_round_record(
        skill.path,
        {
            "timestamp": "20260601_120001_000002",
            "skill_name": skill.name,
            "parent_version": "v1",
            "candidate_version": "v3",
            "gate_status": "promoted",
            "delta": 0.041,
            "n_failure_reflections": 2,
            "n_success_reflections": 1,
            "dominant_failure_pattern": "missing_step",
            "dominant_success_pattern": "step_sequencing",
            "reason": "candidate > parent",
        },
    )
    blob = build_meta_skill(skill.path, char_cap=2000)

    assert blob, "meta-skill blob should be non-empty when prior rounds exist"
    assert "Prior rounds: 2 total" in blob
    assert "1 PROMOTE" in blob and "1 ROLLBACK" in blob
    assert "R1: rolled_back" in blob and "R2: promoted" in blob
    assert "wrong_api" in blob and "step_sequencing" in blob


def test_extract_workspace_view_strips_instruction_prefix(tmp_path: Path):
    from evolution.evolution.reflection import extract_workspace_view

    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    instruction = "Solve the task carefully."
    workspace_block = "Available files:\n- inputs/data.csv (12 rows)"
    (trace_dir / "prompt.md").write_text(f"{instruction}\n\n{workspace_block}", encoding="utf-8")
    out = extract_workspace_view(trace_dir, instruction)
    assert out == workspace_block
    (trace_dir / "prompt.md").unlink()
    assert extract_workspace_view(trace_dir, instruction) == ""


def test_actionable_reflections_partitions_by_kind():
    from evolution.evolution.rewrite_guards import (
        actionable_reflections,
        actionable_success_reflections,
    )

    mixed = [
        {
            "kind": "failure",
            "pattern": "wrong_api",
            "evidence_sufficiency": "sufficient",
            "rewrite_recommended": True,
        },
        {
            "kind": "success",
            "reinforcement_pattern": "step_sequencing",
            "evidence_sufficiency": "sufficient",
            "preserve_recommended": True,
        },
        {
            "pattern": "logic_error",
            "evidence_sufficiency": "sufficient",
            "rewrite_recommended": True,
        },
    ]
    failures = actionable_reflections(mixed)
    successes = actionable_success_reflections(mixed)
    assert failures is not None and len(failures) == 2
    assert all(str(r.get("kind") or "failure") == "failure" for r in failures)
    assert len(successes) == 1
    assert successes[0]["reinforcement_pattern"] == "step_sequencing"


def test_reflect_trace_renders_new_sections(tmp_path: Path):
    from evolution.evolution.reflection import reflect_trace

    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    backend = MockBackend(responses=[_reflection_json()])
    siblings = {"other-trace": "task=other pass=0/1 verifier=produced_but_failed_verification"}

    asyncio.run(
        reflect_trace(
            backend,
            trace_dir,
            skill,
            "task",
            siblings=siblings,
            meta_skill_blob="R1: rolled_back v1->v2 (Δ=-0.050)",
            workspace_view="Available inputs:\n- inputs/foo.csv",
        )
    )
    prompt_text = backend.calls[-1]["messages"][0]["content"]
    assert "## Cross-Round Learning" in prompt_text
    assert "R1: rolled_back" in prompt_text
    assert "## Other Train Traces This Round" in prompt_text
    assert "task=other" in prompt_text
    assert "## Agent Workspace View" in prompt_text
    assert "inputs/foo.csv" in prompt_text


def test_reflect_failing_traces_branches_on_success(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EVO_REFLECT_SUCCESS_K", "3")
    import importlib

    from evolution.evolution import reflection as _reflection

    importlib.reload(_reflection)

    skill = _make_skill(tmp_path)
    fail_dir = _save_failed_trace(skill)
    succ_dir = _save_success_trace(skill, code="print('ok')")
    backend = MockBackend(responses=[_reflection_json(), _success_reflection_json()])

    out = _reflection.reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="task-name",
    )

    assert len(out) == 2
    kinds = {r.get("kind") for r in out}
    assert kinds == {"failure", "success"}

    fail_refl = json.loads((fail_dir / "reflection.json").read_text())
    succ_refl = json.loads((succ_dir / "reflection.json").read_text())
    assert fail_refl["kind"] == "failure"
    assert succ_refl["kind"] == "success"
    assert succ_refl["preserve_recommended"] is True
    succ_prompt = backend.calls[-1]["messages"][0]["content"]
    assert "## Execution Evidence" in succ_prompt


def test_summarize_reflection_audit_counts_by_kind():
    from evolution.evolution.meta_skill import summarize_reflection_audit

    audit = [
        {"kind": "failure", "pattern": "wrong_api"},
        {"kind": "failure", "pattern": "wrong_api"},
        {"kind": "failure", "pattern": "missing_step"},
        {"kind": "success", "pattern": "step_sequencing"},
        {"pattern": "logic_error"},
    ]
    out = summarize_reflection_audit(audit)
    assert out["n_failure_reflections"] == 4
    assert out["n_success_reflections"] == 1
    assert out["dominant_failure_pattern"] == "wrong_api"
    assert out["dominant_success_pattern"] == "step_sequencing"


def test_reflect_prompt_includes_success_preserve_placeholder():
    from evolution.evolution.reflect import (
        CODEX_FILE_EDIT_PROMPT,
        REFLECT_SUCCESS_PROMPT,
        REWRITE_PROMPT,
    )

    assert "{success_preserve_section}" in REWRITE_PROMPT
    assert "{success_preserve_section}" in CODEX_FILE_EDIT_PROMPT
    assert "## Execution Evidence" in REFLECT_SUCCESS_PROMPT
    assert "preserve_recommended" in REFLECT_SUCCESS_PROMPT


def test_reflect_system_selects_blind_or_oracle(monkeypatch):
    """The reflector system prompt flips with EVO_REFLECT_BLIND: oracle-visible
    is the new default; blind is the publication opt-out."""
    from evolution.evolution.reflection import (
        REFLECT_SYSTEM_BLIND,
        REFLECT_SYSTEM_ORACLE,
        _reflect_system,
    )

    monkeypatch.setenv("EVO_REFLECT_BLIND", "1")
    assert _reflect_system() is REFLECT_SYSTEM_BLIND
    assert "NEVER given verifier source" in REFLECT_SYSTEM_BLIND

    monkeypatch.setenv("EVO_REFLECT_BLIND", "0")
    assert _reflect_system() is REFLECT_SYSTEM_ORACLE
    assert "full diagnostic evidence" in REFLECT_SYSTEM_ORACLE
    assert "leak scanner" in REFLECT_SYSTEM_ORACLE


def test_reflect_trace_renders_rejected_edit_buffer(tmp_path: Path):
    skill = _make_skill(tmp_path)
    trace_dir = _save_failed_trace(skill)
    backend = MockBackend(responses=[_reflection_json()])

    asyncio.run(
        reflect_trace(
            backend,
            trace_dir,
            skill,
            "task",
            rejected_edits_blob=(
                "Prior-round rewrite rejections (most recent last):\n"
                "- R1: status=leak_rejected; churn=0.40; new_headings=[(none)]; "
                "counts(actionable=2, low_conf=0, leak=1)"
            ),
        )
    )

    prompt = backend.calls[0]["messages"][0]["content"]
    assert "## Rejected-Edit Buffer" in prompt
    assert "leak_rejected" in prompt


def test_session_has_progress_detects_salvageable_output(tmp_path: Path):
    from types import SimpleNamespace

    from evolution.evolution.reflect import _session_has_progress

    parent_body = "Use Python carefully."
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    skill_md = session_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: test-skill\ndescription: A test skill\n---\n\n{parent_body}\n",
        encoding="utf-8",
    )
    ws = SimpleNamespace(session_dir=session_dir, skill_path=skill_md)

    # Unedited body, no DIAGNOSIS.json -> nothing to salvage.
    assert _session_has_progress(ws, parent_body) is False

    # An edited SKILL.md alone counts as progress.
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nEdited body.\n",
        encoding="utf-8",
    )
    assert _session_has_progress(ws, parent_body) is True

    # A written DIAGNOSIS.json counts even when the body is unchanged.
    skill_md.write_text(
        f"---\nname: test-skill\ndescription: A test skill\n---\n\n{parent_body}\n",
        encoding="utf-8",
    )
    assert _session_has_progress(ws, parent_body) is False
    (session_dir / "DIAGNOSIS.json").write_text("{}", encoding="utf-8")
    assert _session_has_progress(ws, parent_body) is True
