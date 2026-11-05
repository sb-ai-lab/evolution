"""Characterization tests pinning rewrite_skill_with_audit dispatch behaviour.

These lock the candidate_status outcomes of the session path (previously
untested) and the text-return router exception/no-op paths, so the reflect.py
dispatch/symmetry refactor can be verified as behaviour-preserving.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import evolution.evolve.reflect as reflect_mod
import evolution.evolve.rewrite_session as session_mod
from evolution.core.models import Skill, SkillFrontmatter
from evolution.evolve.reflect import rewrite_skill_with_audit
from evolution.evolve.rewrite_outcome import (
    LOW_CONFIDENCE_REFLECTION,
    NO_OP_FILE_EDIT,
    NO_OP_TEXT,
    PROMOTED_ELIGIBLE,
    REWRITE_ERROR_BACKEND,
)
from evolution.evolve.session_workspace import SessionWorkspace
from evolution.llm.agent_backend import AgentBackend
from evolution.llm.backend import LLMResponse
from evolution.llm.mock_backend import MockBackend

_PARENT_BODY = (
    "## Overview\n"
    "The parent skill explains how to do the thing.\n\n"
    "## Steps\n"
    "1. First do A.\n"
    "2. Then do B.\n"
    "3. Finally do C.\n"
)
_CANDIDATE_BODY = (
    "## Overview\n"
    "The parent skill explains how to do the thing.\n\n"
    "## Steps\n"
    "1. First do A.\n"
    "2. Then do B.\n"
    "3. Then do the new step.\n"
    "4. Finally do C.\n"
)
_SKILL_FM = "---\nname: test-skill\ndescription: A test skill\n---\n\n"


def _make_skill(tmp_path: Path, body: str = _PARENT_BODY) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(_SKILL_FM + body, encoding="utf-8")
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


class _FakeSessionBackend(AgentBackend):
    """Drives _rewrite_skill_agentic_session_impl: writes the agent's on-disk
    SKILL.md edit + DIAGNOSIS.json into ``work_dir`` (the session dir)."""

    def __init__(
        self,
        *,
        write_body: str | None,
        write_diagnosis: dict | None,
    ) -> None:
        super().__init__(model="gpt-5.5", reasoning_effort="medium")
        self._write_body = write_body
        self._write_diagnosis = write_diagnosis

    async def complete_agent_session(
        self,
        work_dir: Path,
        prompt: str,
        system: str | None = None,
        *,
        budget_usd: float | None = None,
        timeout_s: float | None = None,
    ) -> LLMResponse:
        if self._write_body is not None:
            (work_dir / "SKILL.md").write_text(_SKILL_FM + self._write_body, encoding="utf-8")
        if self._write_diagnosis is not None:
            (work_dir / "DIAGNOSIS.json").write_text(
                json.dumps(self._write_diagnosis), encoding="utf-8"
            )
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
        self.last_codex_diagnostics = {}
        return LLMResponse(content="", tokens_in=100, tokens_out=50, time_ms=100)


def _install_fake_workspace(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("EVO_C1_CANDIDATE_ROOT", str(tmp_path / "cand"))
    (tmp_path / "cand").mkdir(parents=True, exist_ok=True)

    def _fake_build(skill: Skill, task: Any, **_kwargs: Any) -> SessionWorkspace:
        root = tmp_path / "sess_root"
        session_dir = root / "session"
        verifier_dir = root / "_verifier"
        session_dir.mkdir(parents=True)
        verifier_dir.mkdir(parents=True)
        skill_path = session_dir / "SKILL.md"
        skill_path.write_text(_SKILL_FM + _PARENT_BODY, encoding="utf-8")
        solve_sh = session_dir / "solve.sh"
        solve_sh.write_text("#!/bin/sh\n")
        measure_sh = session_dir / "measure.sh"
        measure_sh.write_text("#!/bin/sh\n")
        return SessionWorkspace(
            root=root,
            session_dir=session_dir,
            verifier_dir=verifier_dir,
            skill_path=skill_path,
            measure_sh=measure_sh,
            solve_sh=solve_sh,
            trace_ids=["t1"],
            role=None,
            source_models=["m"],
            blind=True,
        )

    monkeypatch.setattr(session_mod, "build_session_workspace", _fake_build)


def _run_session(skill: Skill, backend: AgentBackend) -> Any:
    return asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            [_reflection()],
            rewrite_mode="agentic-session",
            task=object(),
        )
    )


def test_session_task_none_returns_backend_error(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    backend = _FakeSessionBackend(write_body=None, write_diagnosis=None)
    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend, skill, [_reflection()], rewrite_mode="agentic-session", task=None
        )
    )
    assert outcome.candidate_status == REWRITE_ERROR_BACKEND
    assert outcome.body == skill.body


def test_session_promoted_eligible(tmp_path: Path, monkeypatch: Any) -> None:
    _install_fake_workspace(monkeypatch, tmp_path)
    skill = _make_skill(tmp_path)
    backend = _FakeSessionBackend(
        write_body=_CANDIDATE_BODY,
        write_diagnosis={"rewrite_recommended": True, "evidence_sufficiency": "sufficient"},
    )
    outcome = _run_session(skill, backend)
    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert "new step" in outcome.body


def test_session_no_op_when_body_unchanged(tmp_path: Path, monkeypatch: Any) -> None:
    _install_fake_workspace(monkeypatch, tmp_path)
    skill = _make_skill(tmp_path)
    backend = _FakeSessionBackend(
        write_body=_PARENT_BODY,
        write_diagnosis={"rewrite_recommended": True, "evidence_sufficiency": "sufficient"},
    )
    outcome = _run_session(skill, backend)
    assert outcome.candidate_status == NO_OP_FILE_EDIT
    assert outcome.body == skill.body


def test_session_low_confidence_when_diagnosis_missing(tmp_path: Path, monkeypatch: Any) -> None:
    _install_fake_workspace(monkeypatch, tmp_path)
    skill = _make_skill(tmp_path)
    backend = _FakeSessionBackend(write_body=_CANDIDATE_BODY, write_diagnosis=None)
    outcome = _run_session(skill, backend)
    assert outcome.candidate_status == LOW_CONFIDENCE_REFLECTION
    assert outcome.body == skill.body


def test_session_low_confidence_when_not_recommended(tmp_path: Path, monkeypatch: Any) -> None:
    _install_fake_workspace(monkeypatch, tmp_path)
    skill = _make_skill(tmp_path)
    backend = _FakeSessionBackend(
        write_body=_CANDIDATE_BODY,
        write_diagnosis={"rewrite_recommended": False, "evidence_sufficiency": "sufficient"},
    )
    outcome = _run_session(skill, backend)
    assert outcome.candidate_status == LOW_CONFIDENCE_REFLECTION
    assert outcome.body == skill.body


def test_text_return_no_op_keeps_parent(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    backend = MockBackend(responses=[_PARENT_BODY])
    outcome = asyncio.run(
        rewrite_skill_with_audit(backend, skill, [_reflection()], include_best_trace_code=False)
    )
    assert outcome.candidate_status == NO_OP_TEXT
    assert outcome.body == skill.body


def test_text_return_backend_exception_maps_to_backend_error(
    tmp_path: Path, monkeypatch: Any
) -> None:
    skill = _make_skill(tmp_path)

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(reflect_mod, "_rewrite_skill_text_return_impl", _boom)
    backend = MockBackend(responses=["unused"])
    outcome = asyncio.run(
        rewrite_skill_with_audit(backend, skill, [_reflection()], include_best_trace_code=False)
    )
    assert outcome.candidate_status == REWRITE_ERROR_BACKEND
    assert outcome.body == skill.body
