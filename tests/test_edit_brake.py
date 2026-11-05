"""Public edit-brake policy tests."""

from __future__ import annotations

import math

import pytest

from evolution.core.models import Skill, SkillFrontmatter
from evolution.evolve.edit_brake import (
    analyze_edit,
    effective_budget,
    format_effective_budget,
    resolve_base_budget,
)
from evolution.evolve.reflect import rewrite_skill_with_audit
from evolution.evolve.rewrite_outcome import PROMOTED_ELIGIBLE
from evolution.llm.mock_backend import MockBackend


def test_edit_brake_uses_normalized_nonempty_line_mass() -> None:
    evidence = analyze_edit("one\ntwo\n", "one\nthree\n", base_budget=0.40, promotion_count=0)

    assert evidence.normalized_churn == pytest.approx(0.5)
    assert evidence.changed_line_mass == 2
    assert evidence.allowed is False


def test_edit_brake_ignores_trailing_whitespace_but_not_leading_whitespace() -> None:
    trailing = analyze_edit("  item  \n", "  item\n", base_budget=0, promotion_count=0)
    leading = analyze_edit("item\n", "  item\n", base_budget=0, promotion_count=0)

    assert trailing.normalized_churn == 0
    assert leading.normalized_churn == 1


@pytest.mark.parametrize("value", ["-0.1", "1.1", "nan", "inf"])
def test_edit_brake_rejects_invalid_base_budget(value: str) -> None:
    with pytest.raises(ValueError):
        resolve_base_budget({"EVO_REWRITE_MAX_DELTA_FRAC": value})


def test_edit_brake_decays_without_raising_a_small_base() -> None:
    assert effective_budget(0.40, 0, version_decay=True) == pytest.approx(0.40)
    assert effective_budget(0.40, 3, version_decay=True) == pytest.approx(0.20)
    assert effective_budget(0.05, 100, version_decay=True) == pytest.approx(0.05)
    assert effective_budget(0.40, 100, version_decay=False) == pytest.approx(0.40)


def test_edit_brake_formats_the_precise_decayed_budget() -> None:
    assert format_effective_budget(effective_budget(0.40, 2)) == "0.230940107676 (23.09%)"


def test_edit_brake_uses_a_constant_budget_when_decay_is_disabled() -> None:
    evidence = analyze_edit(
        "one\ntwo",
        "one\nthree",
        base_budget=0.50,
        promotion_count=99,
        version_decay=False,
    )

    assert evidence.effective_budget_frac == 0.50
    assert evidence.schedule == "constant-v1"


def test_edit_brake_is_bounded_for_empty_and_large_lines() -> None:
    assert analyze_edit("", "", base_budget=0, promotion_count=0).normalized_churn == 0
    evidence = analyze_edit("x" * 1000, "y" * 1000, base_budget=1, promotion_count=0)
    assert math.isclose(evidence.normalized_churn, 1)
    assert evidence.max_changed_line_length == 1000


@pytest.mark.asyncio
async def test_rewrite_retries_only_after_the_shared_brake(tmp_path) -> None:
    skill_path = tmp_path / "SKILL.md"
    parent = "one\ntwo\nthree\nfour"
    skill_path.write_text(f"---\nname: brake\ndescription: test\n---\n\n{parent}\n")
    skill = Skill(
        frontmatter=SkillFrontmatter(name="brake", description="test"),
        body=parent,
        path=tmp_path,
        skill_md_path=skill_path,
    )
    backend = MockBackend(responses=["a\nb\nc\nd", parent + "\nfive"])
    outcome = await rewrite_skill_with_audit(
        backend,
        skill,
        [{"root_cause": "x", "skill_gap": "x", "suggested_fix": "x", "pattern": "x"}],
        max_rewrite_attempts=2,
        promotion_count=2,
    )

    assert outcome.candidate_status == PROMOTED_ELIGIBLE
    assert len(outcome.rewrite_attempts or []) == 2
    assert outcome.rewrite_attempts[0]["candidate_status"] == "candidate_edit_budget_exceeded"
    first_prompt = backend.calls[0]["messages"][0]["content"]
    second_prompt = backend.calls[1]["messages"][0]["content"]
    assert "exact effective budget of 0.230940107676 (23.09%)" in first_prompt
    assert "Attempt 1 exceeded the edit brake" in second_prompt


@pytest.mark.asyncio
async def test_resume_does_not_repeat_a_finalized_rewrite_attempt(tmp_path) -> None:
    skill_path = tmp_path / "skill" / "SKILL.md"
    skill_path.parent.mkdir()
    parent = "one\ntwo\nthree\nfour"
    skill_path.write_text(f"---\nname: brake\ndescription: test\n---\n\n{parent}\n")
    skill = Skill(
        frontmatter=SkillFrontmatter(name="brake", description="test"),
        body=parent,
        path=skill_path.parent,
        skill_md_path=skill_path,
    )
    reflections = [{"root_cause": "x", "skill_gap": "x", "suggested_fix": "x", "pattern": "x"}]
    audit_root = tmp_path / "audit"
    first_backend = MockBackend(responses=["a\nb\nc\nd"])

    first = await rewrite_skill_with_audit(
        first_backend,
        skill,
        reflections,
        max_rewrite_attempts=1,
        promotion_count=2,
        audit_root=audit_root,
    )
    resumed_backend = MockBackend(responses=[parent + "\nfive"])
    resumed = await rewrite_skill_with_audit(
        resumed_backend,
        skill,
        reflections,
        max_rewrite_attempts=2,
        promotion_count=2,
        audit_root=audit_root,
    )

    assert first.candidate_status == "candidate_edit_budget_exceeded"
    assert len(first_backend.calls) == 1
    assert len(resumed_backend.calls) == 1
    assert resumed.candidate_status == PROMOTED_ELIGIBLE
    assert [attempt["attempt"] for attempt in resumed.rewrite_attempts or []] == [1, 2]
