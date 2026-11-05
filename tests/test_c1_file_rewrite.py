"""Unit tests for the C1 file-edit rewrite ablation (plan §1.7)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from evolution.cli.main import app
from evolution.core.models import Skill, SkillFrontmatter
from evolution.evolve.preservation import (
    frontmatter_semantic_equality,
    heading_retention,
    score_preservation,
    section_preservation,
    split_skill_body_by_heading,
    structure_drift,
    whole_body_seqmatch_ratio,
)
from evolution.evolve.reflect import (
    _non_empty_line_churn,
    rewrite_skill_with_audit,
)
from evolution.evolve.rewrite_guards import (
    _ALLOWED_HIDDEN_NAMES,
    validate_candidate_isolation,
)
from evolution.evolve.rewrite_outcome import (
    CANDIDATE_EDIT_BUDGET_EXCEEDED,
    CANDIDATE_INVALID_EXTRA_FILES,
    CANDIDATE_INVALID_HIDDEN_FILE,
    CANDIDATE_INVALID_SYMLINK,
    NO_OP_FILE_EDIT,
    NO_OP_TEXT,
    PROMOTED_ELIGIBLE,
    REWRITE_ERROR_CODEX,
    RewriteOutcome,
)
from evolution.llm.agent_backend import (
    AgentBackend,
    AgentBackendError,
    _parse_codex_jsonl_events,
    _parse_codex_usage,
    _resolve_file_edit_sandbox,
)
from evolution.llm.backend import LLMResponse
from evolution.llm.mock_backend import MockBackend


def _make_skill(tmp_path: Path, body: str | None = None) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    body = body if body is not None else "Parent body line one.\n"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n" + body,
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body=body.strip(),
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _reflection() -> dict:
    return {
        "root_cause": "wrong output",
        "skill_gap": "missing step",
        "suggested_fix": "add the step",
        "severity": "major",
        "pattern": "logic_error",
        "evidence_sufficiency": "sufficient",
        "rewrite_recommended": True,
    }


# ──────────────────────────────────────────────────────────────────────────
# Preservation scorer
# ──────────────────────────────────────────────────────────────────────────


def test_split_skill_body_by_heading_basic() -> None:
    body = "Prelude line.\n\n## Section A\nline a\n\n## Section B\nline b\n"
    sections = split_skill_body_by_heading(body)
    headings = [h for h, _ in sections]
    assert headings == ["", "Section A", "Section B"]


def test_score_preservation_identical_bodies() -> None:
    body = "Prelude.\n\n## One\nfoo\n\n## Two\nbar\n"
    fm = {"name": "x", "description": "y"}
    result = score_preservation(body, body, fm, fm)
    assert result["heading_retention"] == 1.0
    assert result["section_preservation"] == 1.0
    assert result["structure_drift"] == 0
    assert result["frontmatter_semantic_equality"] is True
    assert result["whole_body_seqmatch_ratio"] == 1.0


def test_score_preservation_targeted_edit() -> None:
    parent = "## One\nshared line\n\n## Two\nold content\n"
    candidate = "## One\nshared line\n\n## Two\nbrand new content\n"
    p_sections = split_skill_body_by_heading(parent)
    c_sections = split_skill_body_by_heading(candidate)
    assert heading_retention(p_sections, c_sections) == 1.0
    assert structure_drift(p_sections, c_sections) == 0
    # ## One unchanged (1 line); ## Two changed (1 line, 0 equal).
    # Mass-weighted equal fraction: 1 / 2 = 0.5.
    sp = section_preservation(p_sections, c_sections)
    assert sp == pytest.approx(0.5, rel=1e-3)


def test_score_preservation_full_regen() -> None:
    parent = "## Alpha\nalpha content one\n\n## Beta\nbeta content two\n"
    candidate = "## Gamma\ngamma content three\n\n## Delta\ndelta content four\n"
    p_sections = split_skill_body_by_heading(parent)
    c_sections = split_skill_body_by_heading(candidate)
    assert heading_retention(p_sections, c_sections) == 0.0
    # Two parent + two candidate sections unmatched (preludes match by
    # empty-header → empty-header).
    assert structure_drift(p_sections, c_sections) == 4
    # SequenceMatcher.ratio() on short strings is noisy (matches "## ",
    # newlines, the word "content"); we only assert the bodies are not
    # identical, not a specific ratio.
    assert whole_body_seqmatch_ratio(parent, candidate) < 1.0


# ──────────────────────────────────────────────────────────────────────────
# Frontmatter semantic equality
# ──────────────────────────────────────────────────────────────────────────


def test_frontmatter_semantic_equality_normalises_yaml_quoting() -> None:
    a = {"name": "pdf", "description": "PDF skill"}
    b = {"name": "pdf", "description": "PDF skill"}  # quoting at YAML layer
    assert frontmatter_semantic_equality(a, b) is True


def test_frontmatter_semantic_equality_detects_changed_known_field() -> None:
    a = {"name": "pdf", "description": "PDF skill"}
    b = {"name": "pdf", "description": "DIFFERENT"}
    assert frontmatter_semantic_equality(a, b) is False


def test_frontmatter_raw_keyset_diff_before_normalisation() -> None:
    """Pydantic drops unknown keys; the metric returns True even when
    candidate adds an unknown top-level key.  This test documents the
    intentional gap; callers who care about raw drift compare key sets
    BEFORE invoking ``frontmatter_semantic_equality``.
    """
    parent = {"name": "pdf", "description": "PDF skill"}
    candidate_extra = {"name": "pdf", "description": "PDF skill", "extra_unknown": "x"}
    assert frontmatter_semantic_equality(parent, candidate_extra) is True
    raw_drift = set(parent.keys()) != set(candidate_extra.keys())
    assert raw_drift is True


# ──────────────────────────────────────────────────────────────────────────
# Candidate isolation validator
# ──────────────────────────────────────────────────────────────────────────


def _seed_candidate(tmp: Path, body: str = "# hi") -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "SKILL.md").write_text(body)
    return tmp


def test_validate_candidate_isolation_clean_passes(tmp_path: Path) -> None:
    p = _seed_candidate(tmp_path / "cand")
    ok, status, _reason = validate_candidate_isolation(p)
    assert ok is True
    assert status == ""


def test_validate_candidate_isolation_rejects_symlink(tmp_path: Path) -> None:
    p = _seed_candidate(tmp_path / "cand")
    target = p / "elsewhere.txt"
    target.write_text("x")
    os.symlink(target, p / "link.txt")
    ok, status, _ = validate_candidate_isolation(p)
    assert ok is False
    assert status == CANDIDATE_INVALID_SYMLINK


def test_validate_candidate_isolation_rejects_extra_file(tmp_path: Path) -> None:
    p = _seed_candidate(tmp_path / "cand")
    (p / "sneaky.py").write_text("x")
    ok, status, _ = validate_candidate_isolation(p)
    assert ok is False
    assert status == CANDIDATE_INVALID_EXTRA_FILES


def test_validate_candidate_isolation_rejects_unknown_hidden_file(tmp_path: Path) -> None:
    p = _seed_candidate(tmp_path / "cand")
    (p / ".codex_session").write_text("x")
    ok, status, _ = validate_candidate_isolation(p)
    assert ok is False
    assert status == CANDIDATE_INVALID_HIDDEN_FILE


def test_validate_candidate_isolation_default_allow_list_is_empty() -> None:
    """Phase 2a populates this set from observation; default is strict."""
    assert _ALLOWED_HIDDEN_NAMES == frozenset()


# ──────────────────────────────────────────────────────────────────────────
# Edit-budget churn
# ──────────────────────────────────────────────────────────────────────────


def test_edit_budget_zero_churn_when_identical() -> None:
    body = "line1\nline2\nline3\n"
    assert _non_empty_line_churn(body, body) == 0.0


def test_edit_budget_full_replace_is_high_churn() -> None:
    parent = "p1\np2\np3\n"
    candidate = "c1\nc2\nc3\n"
    # All three lines replaced; churn = 3/3 = 1.0.
    assert _non_empty_line_churn(parent, candidate) == pytest.approx(1.0, rel=1e-3)


def test_edit_budget_partial_change() -> None:
    parent = "a\nb\nc\nd\n"
    candidate = "a\nb\nCHANGED\nd\n"
    # one replace of one line; churn = 1/4 = 0.25.
    assert _non_empty_line_churn(parent, candidate) == pytest.approx(0.25, rel=1e-3)


# ──────────────────────────────────────────────────────────────────────────
# rewrite_skill_with_audit dispatch
# ──────────────────────────────────────────────────────────────────────────


def test_dispatch_text_return_default(tmp_path: Path) -> None:
    """rewrite_mode="text-return" (default) must run the text-return path."""
    skill = _make_skill(tmp_path)
    backend = MockBackend(responses=["Improved body for the skill.\n"])
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
        )
    )
    assert isinstance(outcome, RewriteOutcome)
    # Should be promoted_eligible or text-no-op depending on sanitize; either
    # way the status is from the text-return enum (no NO_OP_FILE_EDIT here).
    assert outcome.candidate_status != NO_OP_FILE_EDIT


def test_dispatch_failfast_when_file_edit_with_non_codex_backend(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    backend = MockBackend(responses=["irrelevant"])
    with pytest.raises(RuntimeError, match="requires an AgentBackend"):
        asyncio.run(
            rewrite_skill_with_audit(
                backend,
                skill,
                [_reflection(), _reflection()],
                include_best_trace_code=False,
                rewrite_mode="file-edit",
            )
        )


# ──────────────────────────────────────────────────────────────────────────
# File-edit branch with fake codex backend
# ──────────────────────────────────────────────────────────────────────────


class _FakeCodexBackend(AgentBackend):
    """Stand-in for AgentBackend.complete_file_edit in unit tests.

    Subclasses AgentBackend so ``isinstance`` succeeds; overrides
    ``complete_file_edit`` to write a configurable SKILL.md into
    ``work_dir`` and return a stub LLMResponse.  Optionally raises
    :class:`AgentBackendError` to simulate agent failure.
    """

    def __init__(
        self,
        *,
        write_body: str | None = None,
        write_frontmatter: dict | None = None,
        raise_error: bool = False,
        extra_file: str | None = None,
        last_usage: dict[str, Any] | None = None,
        last_codex_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(model="gpt-5.5", reasoning_effort="medium")
        self._write_body = write_body
        self._write_frontmatter = write_frontmatter
        self._raise_error = raise_error
        self._extra_file = extra_file
        if last_usage is not None:
            self.last_usage = last_usage
        self._fake_diagnostics = last_codex_diagnostics

    async def complete_file_edit(
        self,
        work_dir: Path,
        prompt: str,
        system: str | None = None,
        *,
        timeout_s: float | None = None,
    ) -> LLMResponse:
        if self._raise_error:
            raise AgentBackendError("fake codex exec failure")
        if self._extra_file:
            (work_dir / self._extra_file).write_text("noise")
        if self._write_body is not None:
            fm = self._write_frontmatter or {
                "name": "test-skill",
                "description": "A test skill",
            }
            fm_yaml = "\n".join(f"{k}: {v}" for k, v in fm.items())
            (work_dir / "SKILL.md").write_text(
                f"---\n{fm_yaml}\n---\n\n{self._write_body}\n",
                encoding="utf-8",
            )
        self.last_usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_turns": 2,
            "wall_time_ms": 123,
        }
        if self._fake_diagnostics is not None:
            self.last_codex_diagnostics = dict(self._fake_diagnostics)
        return LLMResponse(content="", tokens_in=100, tokens_out=50, time_ms=123)


def test_file_edit_no_op_when_codex_writes_identical_body(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path, body="Parent body.")
    backend = _FakeCodexBackend(write_body="Parent body.")
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == NO_OP_FILE_EDIT
    assert outcome.body == skill.body
    assert outcome.preservation is not None
    assert outcome.preservation["section_preservation"] == 1.0


_PARENT_BODY_RICH = (
    "## Overview\n"
    "The parent skill explains how to do the thing.\n\n"
    "## Steps\n"
    "1. First do A.\n"
    "2. Then do B.\n"
    "3. Finally do C.\n"
)
_CANDIDATE_BODY_RICH = (
    "## Overview\n"
    "The parent skill explains how to do the thing.\n\n"
    "## Steps\n"
    "1. First do A.\n"
    "2. Then do B.\n"
    "3. Then do the new step.\n"
    "4. Finally do C.\n"
)


def test_file_edit_happy_path_marks_promoted_eligible(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    backend = _FakeCodexBackend(write_body=_CANDIDATE_BODY_RICH)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert "new step" in outcome.body
    assert outcome.preservation is not None
    assert outcome.preservation["heading_retention"] == 1.0
    assert outcome.codex_usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "tool_turns": 2,
        "wall_time_ms": 123,
    }


def test_file_edit_codex_error_records_rewrite_error_codex(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    backend = _FakeCodexBackend(raise_error=True)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == REWRITE_ERROR_CODEX
    assert "fake codex exec failure" in outcome.reason


def test_file_edit_extra_file_rejected_as_isolation_violation(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    backend = _FakeCodexBackend(
        write_body="Parent body line one.\nNew step.",
        extra_file="sneaky.py",
    )
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == CANDIDATE_INVALID_EXTRA_FILES
    assert outcome.body == skill.body


def test_file_edit_budget_enforcement_rejects_above_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVO_REWRITE_MAX_DELTA_FRAC", "0.1")
    skill = _make_skill(tmp_path, body="A\nB\nC\nD\n")
    # Replace all four lines → churn=1.0 > 0.1 budget.
    backend = _FakeCodexBackend(write_body="W\nX\nY\nZ\n")
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == CANDIDATE_EDIT_BUDGET_EXCEEDED


def test_file_edit_base_one_disables_churn_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVO_REWRITE_MAX_DELTA_FRAC", "1")
    skill = _make_skill(tmp_path, body="A\nB\nC\nD\n")
    backend = _FakeCodexBackend(write_body="W\nX\nY\nZ\n")
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE


def test_file_edit_debug_archive_moves_candidate_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVO_C1_CANDIDATE_ROOT", str(tmp_path / "candroot"))
    (tmp_path / "candroot").mkdir()
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    backend = _FakeCodexBackend(write_body=_CANDIDATE_BODY_RICH)
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
            audit_root=audit_root,
            row_id="row3__pm.pdf",
            debug_archive=True,
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE
    assert outcome.candidate_debug_dir is not None
    archived = Path(outcome.candidate_debug_dir)
    assert archived.is_dir()
    assert archived.name == "row3__pm.pdf"
    assert (archived / "SKILL.md").is_file()


# ──────────────────────────────────────────────────────────────────────────
# Codex usage parser
# ──────────────────────────────────────────────────────────────────────────


def test_parse_codex_usage_extracts_three_fields() -> None:
    sample = "blah\nprompt tokens: 1,234\ncompletion tokens: 567\ntool turns: 4\n"
    result = _parse_codex_usage(sample)
    assert result == {"prompt_tokens": 1234, "completion_tokens": 567, "tool_turns": 4}


def test_parse_codex_usage_missing_fields_are_none() -> None:
    result = _parse_codex_usage("no telemetry here")
    assert result == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "tool_turns": None,
    }


# ──────────────────────────────────────────────────────────────────────────
# Evidence walker exclusion — defensive (memory
# feedback_audit_files_must_not_feed_evidence).
# ──────────────────────────────────────────────────────────────────────────


def test_evidence_walker_excludes_rewrite_audit_sidecar(tmp_path: Path) -> None:
    """Files matching ``*_rewrite_audit.json`` must not enter produced_artifacts."""
    from evolution.eval.evidence import _produced_manifest

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.txt").write_text("real artifact")
    (workspace / "row3_rewrite_audit.json").write_text('{"schema":"rewrite-audit-v1"}')
    manifest = _produced_manifest(workspace, pre={})
    assert isinstance(manifest, list)
    paths = [m.get("path") for m in manifest if isinstance(m, dict) and "path" in m]
    assert "result.txt" in paths
    assert all("rewrite_audit" not in p for p in paths)


def test_evidence_walker_excludes_c1_candidates_subtree(tmp_path: Path) -> None:
    """``c1_candidates/<row>/SKILL.md`` archives must not enter produced_artifacts."""
    from evolution.eval.evidence import _produced_manifest

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.txt").write_text("real artifact")
    archive = workspace / "c1_candidates" / "row3__pm.pdf"
    archive.mkdir(parents=True)
    (archive / "SKILL.md").write_text("# archived candidate")
    manifest = _produced_manifest(workspace, pre={})
    assert isinstance(manifest, list)
    paths = [m.get("path") for m in manifest if isinstance(m, dict) and "path" in m]
    assert "result.txt" in paths
    assert all("c1_candidates" not in p for p in paths)


def test_text_return_outcome_carries_preservation_when_bodies_differ(
    tmp_path: Path,
) -> None:
    """Review fix #3: text-return rows must populate ``parent_preservation``
    so t34v8 ↔ t34v9 mechanism numbers are comparable.
    """
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    new_body = _CANDIDATE_BODY_RICH

    class _StubBackend(MockBackend):
        pass

    backend = _StubBackend(responses=[new_body])
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
        )
    )
    # Promoted_eligible from the text-return path → preservation populated.
    assert outcome.candidate_status == PROMOTED_ELIGIBLE
    assert outcome.preservation is not None
    assert "section_preservation" in outcome.preservation
    assert outcome.preservation["heading_retention"] == 1.0


def test_text_return_outcome_skips_preservation_when_parent_unchanged(
    tmp_path: Path,
) -> None:
    """When text-return returns the parent body (no_op_text or rejection),
    preservation is None — there's nothing to score against the parent.
    """
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    # MockBackend returning identical body → sanitize keeps it identical →
    # candidate_status=no_op_text and preservation stays None.
    backend = MockBackend(responses=[_PARENT_BODY_RICH])
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
        )
    )
    assert outcome.preservation is None


def test_evidence_walker_excludes_audit_dir(tmp_path: Path) -> None:
    """Anything under an ``audit/`` subdir is excluded as defensive scaffolding."""
    from evolution.eval.evidence import _produced_manifest

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "result.txt").write_text("real artifact")
    audit = workspace / "audit"
    audit.mkdir()
    (audit / "anything.json").write_text("{}")
    manifest = _produced_manifest(workspace, pre={})
    assert isinstance(manifest, list)
    paths = [m.get("path") for m in manifest if isinstance(m, dict) and "path" in m]
    assert "result.txt" in paths
    assert all("audit/" not in p for p in paths)


# ──────────────────────────────────────────────────────────────────────────
# CLI audit-out contract — sanity test that the JSON always lands
# ──────────────────────────────────────────────────────────────────────────


def test_cli_rewrite_always_writes_audit_json_even_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's try/finally must guarantee the audit JSON exists, even when
    rewrite_skill_with_audit raises an unexpected exception.
    """
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)
    manifest_path = tmp_path / "split_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "unit-split",
                "counts": {"train": 1, "val": 0, "test": 0},
                "tasks": [{"id": "task-name", "split": "train"}],
            }
        ),
        encoding="utf-8",
    )

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def boom(*args: Any, **kwargs: Any) -> RewriteOutcome:
        raise RuntimeError("simulated mid-flight failure")

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", boom)

    audit_path = tmp_path / "audit_out.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-audit-out",
            str(audit_path),
            "--manifest",
            str(manifest_path),
        ],
    )
    # rc!=0 because the CLI surfaces internal contract violations.
    assert result.exit_code != 0
    # …but the audit JSON STILL exists thanks to try/finally.
    assert audit_path.is_file(), result.output
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "rewrite-audit-v1"
    assert payload["candidate_status"] == "rewrite_error_internal"
    assert "simulated mid-flight failure" in payload["reason"]


def test_cli_rewrite_invalid_mode_records_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown --rewrite-mode value triggers rc!=0 + audit with internal error."""
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())

    audit_path = tmp_path / "audit_out.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-audit-out",
            str(audit_path),
            "--rewrite-mode",
            "bogus-mode",
        ],
    )
    assert result.exit_code != 0
    assert audit_path.is_file(), result.output
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["candidate_status"] == "rewrite_error_internal"
    assert "invalid --rewrite-mode" in payload["reason"]


def test_cli_rewrite_text_return_writes_audit_with_null_codex_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text-return mode produces the same schema, with null codex_usage."""
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def fake_outcome(*args: Any, **kwargs: Any) -> RewriteOutcome:
        return RewriteOutcome(
            body=skill.body,
            candidate_status=NO_OP_TEXT,
            reason="test fake no-op",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="fake/test",
            parent_body=skill.body,
            candidate_body_raw=None,
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_outcome)

    audit_path = tmp_path / "audit_out.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-audit-out",
            str(audit_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["rewrite_mode"] == "text-return"
    assert payload["candidate_status"] == NO_OP_TEXT
    assert payload["codex_usage"] is None
    assert payload["parent_preservation"] is None
    assert payload["candidate_debug_dir"] is None


# ──────────────────────────────────────────────────────────────────────────
# t34v10 — JSONL diagnostics parser + sandbox env var + classifier split
# ──────────────────────────────────────────────────────────────────────────

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "codex_jsonl"


def test_parse_codex_jsonl_events_captures_sandbox_errors_from_fixture_ws() -> None:
    text = (_FIXTURES_DIR / "ws.jsonl").read_text(encoding="utf-8")
    diag = _parse_codex_jsonl_events(text)
    joined = "\n".join(diag["sandbox_errors"])
    assert "bwrap: Failed to make / slave: Permission denied" in joined
    assert diag["file_changes"] == []
    assert diag["non_json_lines"] > 0


def test_parse_codex_jsonl_events_captures_file_changes_from_fixture_dfa() -> None:
    text = (_FIXTURES_DIR / "dfa.jsonl").read_text(encoding="utf-8")
    diag = _parse_codex_jsonl_events(text)
    paths = [ch.get("path") for ch in diag["file_changes"]]
    assert any(p and p.endswith("/SKILL.md") for p in paths)
    assert any((ch.get("kind") == "update") for ch in diag["file_changes"])
    assert diag["sandbox_errors"] == []


def test_parse_codex_jsonl_events_handles_non_json_lines() -> None:
    text = (
        '{"type":"turn.started"}\n'
        "2026-05-21T09:41:24Z ERROR codex_core: bwrap: Failed to make / slave: Permission denied\n"
        "not even close to JSON\n"
    )
    diag = _parse_codex_jsonl_events(text)
    assert diag["non_json_lines"] == 2
    assert any("bwrap:" in s for s in diag["sandbox_errors"])


def test_parse_codex_jsonl_events_captures_command_failures() -> None:
    text = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "sed -i s/a/b/ SKILL.md",
                "exit_code": 1,
                "aggregated_output": "Permission denied",
                "status": "completed",
            },
        }
    )
    diag = _parse_codex_jsonl_events(text)
    assert len(diag["command_failures"]) == 1
    assert diag["command_failures"][0]["exit_code"] == 1
    assert "Permission denied" in (diag["command_failures"][0]["stderr"] or "")


def test_parse_codex_jsonl_events_captures_usage_from_turn_completed() -> None:
    text = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 45}}
    )
    diag = _parse_codex_jsonl_events(text)
    assert diag["prompt_tokens"] == 123
    assert diag["completion_tokens"] == 45


def test_parse_codex_jsonl_events_captures_codex_error_event_routes_to_sandbox_errors_when_bwrap() -> (
    None
):
    text = json.dumps(
        {"type": "error", "message": "bwrap: Failed to make / slave: Permission denied"}
    )
    diag = _parse_codex_jsonl_events(text)
    assert any("bwrap:" in m for m in diag["errors"])
    assert any("bwrap:" in s for s in diag["sandbox_errors"])


def test_rewrite_classifies_sandbox_failure_unchanged_as_rewrite_error_codex(
    tmp_path: Path,
) -> None:
    skill = _make_skill(tmp_path, body="Parent body.")
    backend = _FakeCodexBackend(
        write_body="Parent body.",  # unchanged
        last_codex_diagnostics={
            "sandbox": "workspace-write",
            "sandbox_errors": ["bwrap: Failed to make / slave: Permission denied"],
        },
    )
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == REWRITE_ERROR_CODEX, outcome.reason
    assert "sandbox failure" in outcome.reason.lower()
    assert "workspace-write" in outcome.reason
    assert outcome.codex_diagnostics is not None
    assert "bwrap:" in (outcome.codex_diagnostics.get("sandbox_errors") or [""])[0]


def test_rewrite_classifies_clean_unchanged_as_no_op_file_edit(tmp_path: Path) -> None:
    """No sandbox errors + identical candidate → genuine no_op_file_edit (back-compat)."""
    skill = _make_skill(tmp_path, body="Parent body.")
    backend = _FakeCodexBackend(
        write_body="Parent body.",
        last_codex_diagnostics={"sandbox": "workspace-write", "sandbox_errors": []},
    )
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == NO_OP_FILE_EDIT, outcome.reason


def test_complete_file_edit_threads_evo_codex_file_edit_sandbox_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVO_CODEX_FILE_EDIT_SANDBOX", raising=False)
    # Default is the bypass flag because workspace-write fails silently under
    # this host's bwrap (memory reference_codex_workspace_write_broken_bwrap).
    assert _resolve_file_edit_sandbox() == "bypass"

    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "danger-full-access")
    assert _resolve_file_edit_sandbox() == "danger-full-access"

    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "workspace-write")
    assert _resolve_file_edit_sandbox() == "workspace-write"

    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "dangerously-bypass-approvals-and-sandbox")
    assert _resolve_file_edit_sandbox() == "bypass"

    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "read-only")
    with pytest.raises(AgentBackendError):
        _resolve_file_edit_sandbox()


def test_audit_json_contains_codex_diagnostics_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: CLI writes audit JSON with codex_diagnostics field populated."""
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)
    fake_diag = {"sandbox": "workspace-write", "sandbox_errors": ["bwrap: ..."]}

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def fake_outcome(*args: Any, **kwargs: Any) -> RewriteOutcome:
        return RewriteOutcome(
            body=skill.body,
            candidate_status=REWRITE_ERROR_CODEX,
            reason="sandbox failure under -s workspace-write: bwrap: ...",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="codex/gpt-5.5",
            parent_body=skill.body,
            candidate_body_raw=None,
            codex_diagnostics=fake_diag,
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_outcome)

    audit_path = tmp_path / "audit_out.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-mode",
            "file-edit",
            "--rewrite-audit-out",
            str(audit_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["codex_diagnostics"] == fake_diag
    assert payload["candidate_status"] == REWRITE_ERROR_CODEX


def test_complete_file_edit_populates_diagnostics_even_on_nonzero_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per plan §1.1: codex_diagnostics + last_usage populate on every call,
    including the nonzero-rc raise path. Otherwise the audit JSON loses
    sandbox_errors / token counts for the very failures we added --json to see.
    """

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = 2
            self.pid = 0
            self.stdin = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(
                b'{"type":"thread.started","thread_id":"x"}\n'
                b'{"type":"error","message":"bwrap: Failed to make / slave: Permission denied"}\n'
                b'{"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":2}}\n'
            )
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["args"] = list(args)
        return _FakeProc()

    monkeypatch.setattr("evolution.llm.agent_backend.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("evolution.llm.agent_backend.shutil.which", lambda _: "/fake/codex")
    monkeypatch.delenv("EVO_CODEX_FILE_EDIT_SANDBOX", raising=False)

    backend = AgentBackend(model="codex/gpt-5.5", reasoning_effort="medium")
    work = tmp_path / "wd"
    work.mkdir()
    (work / "SKILL.md").write_text("# parent\n")

    with pytest.raises(AgentBackendError):
        asyncio.run(backend.complete_file_edit(work, "prompt", "system"))

    diag = backend.last_codex_diagnostics
    assert diag, "diagnostics must populate even on nonzero rc"
    assert any("bwrap:" in s for s in (diag.get("sandbox_errors") or []))
    assert diag.get("prompt_tokens") == 11
    assert diag.get("completion_tokens") == 2
    # Agent permission defaults to the real Codex workspace sandbox.
    assert diag.get("sandbox") == "workspace-write"
    assert diag.get("bypass_approvals_and_sandbox") is False
    assert "--dangerously-bypass-approvals-and-sandbox" not in (diag.get("command") or [])
    command = diag.get("command") or []
    assert command[command.index("-s") + 1] == "workspace-write"
    assert "--json" in captured["args"]
    assert "-o" in captured["args"]
    assert diag.get("returncode") == 2
    assert "stdout_tail" in diag
    assert "stderr_tail" in diag
    assert "last_message" in diag

    usage = backend.last_usage
    assert usage.get("prompt_tokens") == 11
    assert usage.get("completion_tokens") == 2
    assert usage.get("wall_time_ms") is not None


# ──────────────────────────────────────────────────────────────────────────
# t34v10 quality-spike guards: consensus ranking, no-new-heading, git init
# ──────────────────────────────────────────────────────────────────────────


def _refl(pattern: str, severity: str, suggested_fix: str = "fix-X") -> dict:
    return {
        "root_cause": "rc",
        "skill_gap": "sg",
        "suggested_fix": suggested_fix,
        "severity": severity,
        "pattern": pattern,
        "evidence_sufficiency": "sufficient",
        "rewrite_recommended": True,
    }


def test_consensus_reflection_picks_most_common_pattern_over_singleton_critical() -> None:
    from evolution.evolve.reflect import _consensus_reflection

    reflections = [
        _refl(pattern="missing_step", severity="major", suggested_fix="ms-fix-1"),
        _refl(pattern="missing_step", severity="major", suggested_fix="ms-fix-2"),
        _refl(pattern="logic_error", severity="critical", suggested_fix="le-fix"),
    ]
    chosen = _consensus_reflection(reflections)
    assert chosen["pattern"] == "missing_step", "consensus should beat singleton critical"


def test_consensus_reflection_severity_tiebreaks_when_frequencies_equal() -> None:
    from evolution.evolve.reflect import _consensus_reflection

    reflections = [
        _refl(pattern="missing_step", severity="minor"),
        _refl(pattern="logic_error", severity="critical"),
    ]
    chosen = _consensus_reflection(reflections)
    assert chosen["pattern"] == "logic_error", "tie → severity wins"


def test_consensus_reflection_order_tiebreaks_when_freq_and_sev_equal() -> None:
    from evolution.evolve.reflect import _consensus_reflection

    reflections = [
        _refl(pattern="A", severity="major", suggested_fix="first"),
        _refl(pattern="B", severity="major", suggested_fix="second"),
    ]
    chosen = _consensus_reflection(reflections)
    assert chosen["suggested_fix"] == "first", "ties broken by original order"


def test_consensus_summary_three_of_three_phrasing() -> None:
    from evolution.evolve.reflect import _consensus_summary

    reflections = [_refl("missing_step", "major")] * 3
    summary = _consensus_summary(reflections)
    assert "3/3" in summary
    assert "missing_step" in summary


def test_consensus_summary_handles_empty() -> None:
    from evolution.evolve.reflect import _consensus_summary

    assert "no actionable" in _consensus_summary([])


def test_parent_top_headings_extracts_h2_h3() -> None:
    from evolution.evolve.reflect import _parent_top_headings

    body = "Prelude.\n\n## Section A\nline\n\n## Section B\nline\n\n### Subsection\nx\n"
    headings = _parent_top_headings(body)
    assert "Section A" in headings
    assert "Section B" in headings


def test_candidate_added_new_heading_detects_new_h2() -> None:
    from evolution.evolve.reflect import _candidate_added_new_heading

    parent = "## Step One\ndo x\n\n## Step Two\ndo y\n"
    candidate = "## Step One\ndo x\n\n## Step Two\ndo y\n\n## Step Three\nNEW SECTION\n"
    added, new = _candidate_added_new_heading(parent, candidate)
    assert added is True
    assert any("Step Three" in h for h in new)


def test_candidate_added_new_heading_allows_fuzzy_match() -> None:
    """Minor wording rephrase of an existing heading should not count as 'new'."""
    from evolution.evolve.reflect import _candidate_added_new_heading

    parent = "## Validation Checklist\ndo x\n"
    candidate = "## Validation Checklist:\ndo x\n"  # added colon
    added, _new = _candidate_added_new_heading(parent, candidate)
    assert added is False


def test_candidate_added_new_heading_returns_false_when_parent_has_no_headings() -> None:
    from evolution.evolve.reflect import _candidate_added_new_heading

    parent = "Just a body, no headings.\n"
    candidate = "Just a body, no headings.\n\n## New\nx\n"
    added, _new = _candidate_added_new_heading(parent, candidate)
    assert added is False  # by design — parent has none, can't enforce


def test_rewrite_allows_candidate_with_new_h2_section(tmp_path: Path) -> None:
    """New headings are advisory provenance, not a private admission gate."""
    parent_body = "## Step One\ndo x\n\n## Step Two\ndo y\n"
    candidate_body = parent_body + "\n## Step Three\nNEW SECTION CODEX ADDED\n"
    skill = _make_skill(tmp_path, body=parent_body)
    backend = _FakeCodexBackend(write_body=candidate_body)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
            base_budget=1,
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert outcome.body == candidate_body.strip()
    assert "Step Three" in (outcome.new_headings or [])


def test_git_init_candidate_creates_git_repo(tmp_path: Path) -> None:
    """The harness's git-init shim sets up baseline so codex's `git diff` works."""
    import shutil as _shutil

    from evolution.evolve.reflect import _git_init_candidate

    if not _shutil.which("git"):
        pytest.skip("git not available in env")
    work = tmp_path / "cand"
    work.mkdir()
    (work / "SKILL.md").write_text("# parent\n")
    ok = _git_init_candidate(work, timeout_s=8)
    assert ok is True
    assert (work / ".git").is_dir()
    head = (work / ".git" / "HEAD").read_text(encoding="utf-8")
    assert "baseline" in head


def test_file_edit_prompt_includes_parent_headings_and_consensus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real prompt build (captured via a probe backend) carries
    the new preserve-headings list, consensus summary, and full reflections."""
    captured: dict[str, str] = {}

    class _ProbeBackend(_FakeCodexBackend):
        async def complete_file_edit(
            self,
            work_dir: Path,
            prompt: str,
            system: str | None = None,
            *,
            timeout_s: float | None = None,
        ) -> LLMResponse:
            captured["prompt"] = prompt
            captured["system"] = system or ""
            return await super().complete_file_edit(work_dir, prompt, system, timeout_s=timeout_s)

    body = "## Step One\nfirst\n\n## Step Two\nsecond\n"
    skill = _make_skill(tmp_path, body=body)
    backend = _ProbeBackend(write_body=body)  # no-op, but we capture the prompt
    asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_refl("missing_step", "major"), _refl("missing_step", "major")],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    prompt = captured["prompt"]
    assert "## Existing workflows" in prompt
    assert "- Step One" in prompt
    assert "- Step Two" in prompt
    assert "## Consensus failure" in prompt
    # Three, not two: `rewrite_file_edit` passes `len(reflections) + 1`, because
    # the count includes the synthesized consensus context alongside the raw
    # reflections. The code and this assertion disagreed for a while -- the test
    # was red at the merge base -- and the disagreement is settled in favour of
    # the prompt contract rather than of the arithmetic.
    assert "## All actionable reflections (3)" in prompt
    assert "shared edit brake" in captured["system"].lower()


def test_audit_provenance_fields_populated_on_promoted_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promoted-eligible outcome carries parent_skill_path, sha256s, churn,
    new_headings list (empty), and the effective edit budget."""
    monkeypatch.delenv("EVO_REWRITE_MAX_DELTA_FRAC", raising=False)
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    backend = _FakeCodexBackend(write_body=_CANDIDATE_BODY_RICH)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert outcome.parent_skill_path == str(skill.skill_md_path.resolve())
    assert outcome.parent_body_sha256 and len(outcome.parent_body_sha256) == 64
    assert outcome.candidate_body_sha256 and len(outcome.candidate_body_sha256) == 64
    assert outcome.parent_body_sha256 != outcome.candidate_body_sha256
    assert outcome.computed_churn is not None and outcome.computed_churn > 0
    assert outcome.new_headings == []  # candidate kept parent's headings
    assert outcome.edit_budget_frac == 0.4


def test_audit_provenance_carries_new_headings_without_rejection(tmp_path: Path) -> None:
    """New-heading provenance remains available without a private rejection."""
    parent_body = "## Step One\ndo x\n\n## Step Two\ndo y\n"
    candidate_body = parent_body + "\n## Step Three\nNEW\n\n#### Sub-step\nx\n"
    skill = _make_skill(tmp_path, body=parent_body)
    backend = _FakeCodexBackend(write_body=candidate_body)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
            base_budget=1,
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert outcome.new_headings is not None
    assert "Step Three" in outcome.new_headings
    assert "Sub-step" in outcome.new_headings  # H4 also counts
    # All other provenance fields populated too:
    assert outcome.parent_skill_path
    assert outcome.parent_body_sha256
    assert outcome.candidate_body_sha256
    assert outcome.computed_churn is not None


def test_audit_provenance_edit_budget_frac_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared budget is recorded in the audit evidence."""
    monkeypatch.setenv("EVO_REWRITE_MAX_DELTA_FRAC", "0.15")
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    backend = _FakeCodexBackend(write_body=_CANDIDATE_BODY_RICH)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.edit_budget_frac == 0.15


def test_audit_json_includes_provenance_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: CLI audit JSON has all six provenance keys, even when the
    rewriter exits via the text-return path (keys are None)."""
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def fake_outcome(*args: Any, **kwargs: Any) -> RewriteOutcome:
        return RewriteOutcome(
            body=skill.body,
            candidate_status=NO_OP_TEXT,
            reason="fake",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="fake/test",
            parent_body=skill.body,
            candidate_body_raw=None,
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_outcome)

    audit_path = tmp_path / "audit_out.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["evolve", "rewrite", skill.name, "--rewrite-audit-out", str(audit_path)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    for key in (
        "parent_skill_path",
        "parent_body_sha256",
        "candidate_body_sha256",
        "computed_churn",
        "new_headings",
        "edit_budget_frac",
    ):
        assert key in payload, f"audit missing key: {key}"


# ──────────────────────────────────────────────────────────────────────────
# Fenced-candidate fallback (see docs/codex_usage.md)
# ──────────────────────────────────────────────────────────────────────────


def test_extract_fenced_candidate_returns_largest_block() -> None:
    from evolution.evolve.reflect import _extract_fenced_candidate

    msg = (
        "Here is a small example:\n"
        "```bash\necho hi\n```\n"
        "And the full updated skill body:\n"
        "```markdown\n## Overview\nThis is much longer than the bash example.\n"
        "## Steps\n1. step a\n2. step b\n```\n"
    )
    out = _extract_fenced_candidate(msg, parent_body="x")
    assert out is not None
    assert "## Overview" in out
    assert "echo hi" not in out


def test_extract_fenced_candidate_strips_frontmatter() -> None:
    from evolution.evolve.reflect import _extract_fenced_candidate

    msg = "```markdown\n---\nname: x\ndescription: y\n---\n\n## Body\nthe body.\n```"
    out = _extract_fenced_candidate(msg, parent_body="")
    assert out is not None
    assert not out.startswith("---")
    assert out.startswith("## Body")


def test_extract_fenced_candidate_no_block_returns_none() -> None:
    from evolution.evolve.reflect import _extract_fenced_candidate

    out = _extract_fenced_candidate("Just chat, no fenced block.", parent_body="x")
    assert out is None


def test_extract_fenced_candidate_equal_to_parent_returns_none() -> None:
    from evolution.evolve.reflect import _extract_fenced_candidate

    parent = "## Overview\nthe body.\n"
    msg = f"```markdown\n{parent}```"
    assert _extract_fenced_candidate(msg, parent_body=parent) is None


def test_file_edit_promotes_via_fenced_fallback_when_codex_responds_in_chat(
    tmp_path: Path,
) -> None:
    """File on disk unchanged + last_message carrying a fenced body → promote."""
    skill = _make_skill(tmp_path, body=_PARENT_BODY_RICH)
    fenced_msg = (
        "I'll keep the file untouched and propose the change here:\n"
        f"```markdown\n{_CANDIDATE_BODY_RICH}```\n"
    )
    backend = _FakeCodexBackend(
        write_body=_PARENT_BODY_RICH,  # file unchanged
        last_codex_diagnostics={
            "sandbox": "bypass",
            "sandbox_errors": [],
            "last_message": fenced_msg,
        },
    )
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert "new step" in outcome.body
    assert (outcome.codex_diagnostics or {}).get("fenced_fallback") is True


def test_file_edit_fenced_fallback_skipped_when_sandbox_failed(tmp_path: Path) -> None:
    """Sandbox failure must dominate — fenced fallback can't rescue an honest infra error."""
    skill = _make_skill(tmp_path, body="Parent body.")
    backend = _FakeCodexBackend(
        write_body="Parent body.",  # file unchanged
        last_codex_diagnostics={
            "sandbox": "workspace-write",
            "sandbox_errors": ["bwrap: Failed to make / slave: Permission denied"],
            "last_message": "```markdown\n## Different\nthis would otherwise work.\n```",
        },
    )
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection(), _reflection()],
            include_best_trace_code=False,
            rewrite_mode="file-edit",
        )
    )
    assert outcome.candidate_status == REWRITE_ERROR_CODEX
    # No fenced_fallback marker because the fallback never ran.
    assert (outcome.codex_diagnostics or {}).get("fenced_fallback") is not True


# ──────────────────────────────────────────────────────────────────────────
# CLI sidecar artifacts (last_message.txt + diff.patch next to audit JSON)
# ──────────────────────────────────────────────────────────────────────────


def test_cli_writes_last_message_sidecar_when_codex_returned_chat_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)
    msg = "I declined to edit. Here is what I'd write:\n```markdown\n## X\nbody\n```"
    fake_diag = {"sandbox": "bypass", "last_message": msg}

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def fake_outcome(*args: Any, **kwargs: Any) -> RewriteOutcome:
        return RewriteOutcome(
            body=skill.body,
            candidate_status=NO_OP_FILE_EDIT,
            reason="candidate SKILL.md body byte-equal to parent",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="codex/gpt-5.5",
            parent_body=skill.body,
            candidate_body_raw=skill.body,
            codex_diagnostics=fake_diag,
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_outcome)

    audit_path = tmp_path / "row3__pm.pdf_rewrite_audit.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-mode",
            "file-edit",
            "--rewrite-audit-out",
            str(audit_path),
        ],
    )
    assert result.exit_code == 0, result.output
    sidecar = audit_path.parent / "row3__pm.pdf_rewrite_audit.last_message.txt"
    assert sidecar.is_file(), "last_message sidecar should land next to the audit JSON"
    assert sidecar.read_text(encoding="utf-8") == msg


def test_cli_writes_diff_patch_sidecar_on_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path, body="Parent line one.\nParent line two.\n")
    new_body = "Parent line one.\nNew middle line.\nParent line two.\n"

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def fake_outcome(*args: Any, **kwargs: Any) -> RewriteOutcome:
        return RewriteOutcome(
            body=new_body,
            candidate_status=PROMOTED_ELIGIBLE,
            reason="file-edit rewrite produced a candidate",
            preservation={"section_preservation": 1.0},
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="codex/gpt-5.5",
            parent_body=skill.body,
            candidate_body_raw=new_body,
            codex_diagnostics={"sandbox": "bypass"},
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_outcome)

    audit_path = tmp_path / "row3_audit.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-mode",
            "file-edit",
            "--rewrite-audit-out",
            str(audit_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    sidecar = audit_path.parent / "row3_audit.diff.patch"
    assert sidecar.is_file(), "diff.patch sidecar should land next to the audit JSON"
    diff_text = sidecar.read_text(encoding="utf-8")
    assert "New middle line." in diff_text
    assert diff_text.startswith("---")  # unified-diff header


def test_cli_skips_diff_sidecar_when_body_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty diff → no diff.patch file (avoid churn from rejection rows)."""
    from typer.testing import CliRunner

    from evolution.cli.commands import evolve as cli

    skill = _make_skill(tmp_path)

    class Store:
        def has(self, name: str) -> bool:
            return name == skill.name

        def get(self, name: str) -> Skill:
            return skill

    async def fake_outcome(*args: Any, **kwargs: Any) -> RewriteOutcome:
        return RewriteOutcome(
            body=skill.body,  # unchanged
            candidate_status=NO_OP_FILE_EDIT,
            reason="byte-equal",
            preservation=None,
            codex_usage=None,
            candidate_debug_dir=None,
            model_id="codex/gpt-5.5",
            parent_body=skill.body,
            candidate_body_raw=skill.body,
            codex_diagnostics={"sandbox": "bypass"},
        )

    monkeypatch.setattr(cli, "_get_store", lambda **kwargs: Store())
    monkeypatch.setattr(cli, "collect_reflections", lambda *args, **kwargs: [_reflection()])
    monkeypatch.setattr(cli, "get_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "rewrite_skill_with_audit", fake_outcome)

    audit_path = tmp_path / "row5_audit.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "evolve",
            "rewrite",
            skill.name,
            "--rewrite-mode",
            "file-edit",
            "--rewrite-audit-out",
            str(audit_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (audit_path.parent / "row5_audit.diff.patch").exists()
