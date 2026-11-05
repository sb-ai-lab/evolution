"""Guards for the LLM text-return rewrite path: refuse truncated generations,
drop the length-ratio floor, and catch numeric expected-value leakage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from evolution.core.models import Skill, SkillFrontmatter
from evolution.evolve.reflect import rewrite_skill
from evolution.evolve.rewrite_guards import scan_leakage
from evolution.llm.backend import LLMResponse


class _FixedBackend:
    """Returns one fixed body + finish_reason; records calls."""

    seed_mode = "forwarded"

    def __init__(self, content: str, finish_reason: str = "") -> None:
        self._content = content
        self._finish = finish_reason
        self.calls: list = []

    async def complete(self, messages, system=None, seed=None) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(
            content=self._content, tokens_in=10, tokens_out=20, finish_reason=self._finish
        )


def _make_skill(tmp_path: Path, body: str) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: test-skill\ndescription: A test skill\n---\n\n{body}\n", encoding="utf-8"
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body=body,
        path=skill_dir,
        skill_md_path=skill_md,
    )


_REFLECTIONS = [
    {
        "root_cause": "wrong output",
        "skill_gap": "missing validation",
        "suggested_fix": "validate output files generally",
        "pattern": "logic_error",
        "pass_rate": 0.0,
        "model": "solver",
    }
]


def test_truncated_rewrite_keeps_parent(tmp_path: Path):
    """finish_reason=='length' must NOT commit the (gutted) body — keep parent."""
    parent = "## Overview\n" + "\n".join(f"Real procedural step {i}." for i in range(30))
    skill = _make_skill(tmp_path, parent)
    # A plausible-but-truncated regeneration; would pass size/leak checks.
    truncated = "## Overview\nStep 1. Do the thing.\nStep 2. Validate"
    backend = _FixedBackend(truncated, finish_reason="length")

    body = asyncio.run(
        rewrite_skill(backend, skill, _REFLECTIONS, task_instruction="t", task_filter=None)
    )
    assert body == parent  # parent kept, not the fragment


def test_short_complete_rewrite_is_accepted_when_the_brake_is_disabled(tmp_path: Path, monkeypatch):
    """A complete short body is accepted when the caller disables churn rejection."""
    parent = "## Overview\n" + "\n".join(f"Verbose legacy line {i}." for i in range(40))
    skill = _make_skill(tmp_path, parent)
    concise = "## Overview\nUse the right library.\nValidate every output file before exit."
    assert len(concise) < 0.4 * len(parent)  # would have tripped the old floor
    backend = _FixedBackend(concise, finish_reason="stop")
    monkeypatch.setenv("EVO_REWRITE_MAX_DELTA_FRAC", "1")

    body = asyncio.run(
        rewrite_skill(backend, skill, _REFLECTIONS, task_instruction="t", task_filter=None)
    )
    assert body == concise  # accepted, not rejected as "too short"


def test_numeric_expected_values_are_flagged_but_literals_kept():
    body = "Process up to 5000 rows per batch and name the column total_value."
    # Expected numeric values and quoted literals are both provenance-bearing leaks.
    assert any("numeric" in f for f in scan_leakage(body, "assert total == 5000"))
    assert any("total_value" in f for f in scan_leakage(body, "x = 'total_value'"))
    assert scan_leakage("Process inputs in chunks of 4096 rows.", "assert total == 5000") == []
