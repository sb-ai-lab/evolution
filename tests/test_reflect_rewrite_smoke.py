"""Small smoke checks for the two reflect -> rewrite transports.

The first test is deterministic and runs in the normal unit suite. The
second is an opt-in live Codex smoke because it spends real Codex tokens and
depends on local Codex auth.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from evolution.core.models import Skill, SkillFrontmatter
from evolution.evolution.reflect import rewrite_skill_with_audit
from evolution.evolution.rewrite_outcome import PROMOTED_ELIGIBLE
from evolution.llm.agent_backend import AgentBackend
from evolution.llm.mock_backend import MockBackend


def _make_skill(tmp_path: Path, body: str) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "rewrite-smoke"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: rewrite-smoke\ndescription: Smoke test skill\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(
            name="rewrite-smoke",
            description="Smoke test skill",
        ),
        body=body.strip(),
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _reflections() -> list[dict]:
    return [
        {
            "root_cause": "the skill omits the validation step",
            "skill_gap": "missing validation step",
            "suggested_fix": "add one short validation reminder",
            "severity": "major",
            "pattern": "missing_step",
            "evidence_sufficiency": "sufficient",
            "rewrite_recommended": True,
        },
        {
            "root_cause": "the skill does not tell the solver to verify output",
            "skill_gap": "missing validation step",
            "suggested_fix": "mention checking the final output",
            "severity": "major",
            "pattern": "missing_step",
            "evidence_sufficiency": "sufficient",
            "rewrite_recommended": True,
        },
    ]


def test_text_return_reflect_rewrite_smoke(tmp_path: Path) -> None:
    """A normal LLM text-return rewrite can produce a promoted candidate."""
    parent = "## Steps\n- Follow the parent procedure.\n"
    candidate = parent + "- Validate the final output before stopping.\n"
    skill = _make_skill(tmp_path, parent)
    backend = MockBackend(responses=[candidate])

    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            _reflections(),
            task_instruction="Produce the expected output and verify it.",
            include_best_trace_code=False,
        )
    )

    assert outcome.candidate_status == PROMOTED_ELIGIBLE
    assert "Validate the final output" in outcome.body
    assert outcome.codex_usage is None
    assert outcome.tokens_used == 150
    assert backend.calls, "the rewrite prompt should have called the LLM backend"


def test_codex_file_edit_reflect_rewrite_smoke_deterministic(tmp_path: Path) -> None:
    """Codex file-edit path produces a promoted candidate end-to-end.

    Deterministic mirror of :func:`test_live_codex_file_edit_reflect_rewrite_smoke`
    — uses an AgentBackend subclass that stubs ``complete_file_edit`` so the
    rewrite flow is exercised without spending Codex tokens.
    """
    from evolution.llm.backend import LLMResponse

    parent = "## Steps\n- Follow the parent procedure.\n"
    candidate_body = parent + "- Validate the final output before stopping.\n"
    skill = _make_skill(tmp_path, parent)

    class _Stub(AgentBackend):
        async def complete_file_edit(
            self,
            work_dir: Path,
            prompt: str,
            system: str | None = None,
            *,
            timeout_s: float | None = None,
        ) -> LLMResponse:
            (work_dir / "SKILL.md").write_text(
                f"---\nname: rewrite-smoke\ndescription: Smoke test skill\n---\n\n{candidate_body}",
                encoding="utf-8",
            )
            self.last_usage = {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "tool_turns": 2,
                "wall_time_ms": 250,
            }
            self.last_codex_diagnostics = {
                "sandbox": "bypass",
                "bypass_approvals_and_sandbox": True,
                "file_changes": [{"path": str(work_dir / "SKILL.md"), "kind": "update"}],
                "last_message": "Updated SKILL.md to add the validation step.",
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
            }
            return LLMResponse(content="", tokens_in=12, tokens_out=8, time_ms=250)

    backend = _Stub(model="gpt-5.5", reasoning_effort="medium")

    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            _reflections(),
            task_instruction="Produce the expected output and verify it.",
            include_best_trace_code=False,
            file_edit=True,
        )
    )

    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert "Validate the final output" in outcome.body
    diag = outcome.codex_diagnostics or {}
    assert diag.get("sandbox") == "bypass"
    assert isinstance(diag.get("last_message"), str) and diag["last_message"]
    usage = outcome.codex_usage or {}
    assert usage.get("wall_time_ms") == 250


@pytest.mark.live
def test_live_codex_file_edit_reflect_rewrite_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in smoke for Codex's native file-edit rewrite path.

    Run with:

        EVO_RUN_LIVE_CODEX_SMOKE=1 pytest -q tests/test_reflect_rewrite_smoke.py -m live
    """
    if os.environ.get("EVO_RUN_LIVE_CODEX_SMOKE") != "1":
        pytest.skip("set EVO_RUN_LIVE_CODEX_SMOKE=1 to spend Codex tokens")

    monkeypatch.setenv("EVO_CODEX_FILE_EDIT_SANDBOX", "bypass")
    monkeypatch.setenv("EVO_C1_EDIT_BUDGET_FRAC", "1.0")

    skill = _make_skill(
        tmp_path,
        "## Steps\n- When asked for the marker output, write alpha.\n",
    )
    reflections = [
        {
            "root_cause": "the skill says to write the wrong marker alpha",
            "skill_gap": "the marker instruction should say beta",
            "suggested_fix": "replace alpha with beta in the existing Steps section",
            "severity": "critical",
            "pattern": "logic_error",
            "evidence_sufficiency": "sufficient",
            "rewrite_recommended": True,
        },
        {
            "root_cause": "the existing instruction points to alpha instead of beta",
            "skill_gap": "wrong marker value",
            "suggested_fix": "change only the marker word from alpha to beta",
            "severity": "major",
            "pattern": "logic_error",
            "evidence_sufficiency": "sufficient",
            "rewrite_recommended": True,
        },
    ]
    backend = AgentBackend(model="codex/gpt-5.5", reasoning_effort="medium", timeout_s=300)

    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            reflections,
            task_instruction="Use marker beta, not alpha.",
            include_best_trace_code=False,
            file_edit=True,
            audit_root=tmp_path / "audit",
            row_id="live-codex-smoke",
            debug_archive=True,
        )
    )

    assert outcome.candidate_status == PROMOTED_ELIGIBLE, outcome.reason
    assert "beta" in outcome.body.lower()
    assert outcome.candidate_debug_dir is not None
    archived_entries = sorted(
        str(path.relative_to(outcome.candidate_debug_dir))
        for path in Path(outcome.candidate_debug_dir).rglob("*")
    )
    assert archived_entries == ["SKILL.md"]

    diag = outcome.codex_diagnostics or {}
    assert diag.get("sandbox") == "bypass"
    assert diag.get("bypass_approvals_and_sandbox") is True
    assert any(
        str(change.get("path", "")).endswith("/SKILL.md") for change in diag.get("file_changes", [])
    )
    # Diagnostic surface added in t34v10: last_message captured, returncode +
    # stdout_tail/stderr_tail populated, wall_time_ms measured Python-side.
    assert isinstance(diag.get("last_message"), str)
    assert diag.get("returncode") == 0
    assert "stdout_tail" in diag and "stderr_tail" in diag
    usage = outcome.codex_usage or {}
    assert isinstance(usage.get("wall_time_ms"), int) and usage["wall_time_ms"] > 0
