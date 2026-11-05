"""Tests for skill diff."""

from __future__ import annotations

from pathlib import Path

from evolution.core.models import Skill, SkillFrontmatter
from evolution.lineage.diff import skill_diff


def _make_skill(body: str, name: str = "test", **fm_kwargs) -> Skill:
    return Skill(
        frontmatter=SkillFrontmatter(name=name, description="test", **fm_kwargs),
        body=body,
        path=Path("/fake/test"),
        skill_md_path=Path("/fake/test/SKILL.md"),
    )


class TestSkillDiff:
    def test_identical_no_changes(self):
        body = "# Title\n\nSome instructions."
        d = skill_diff(_make_skill(body), _make_skill(body))
        assert d.sections_added == []
        assert d.sections_removed == []
        assert d.sections_modified == []
        assert "no changes" in d.summary

    def test_section_added(self):
        old = _make_skill("# Title\n\nOriginal.")
        new = _make_skill("# Title\n\nOriginal.\n\n## New Section\n\nNew content.")
        d = skill_diff(old, new)
        assert "New Section" in d.sections_added

    def test_section_removed(self):
        old = _make_skill("# Title\n\nOriginal.\n\n## Old Section\n\nOld content.")
        new = _make_skill("# Title\n\nOriginal.")
        d = skill_diff(old, new)
        assert "Old Section" in d.sections_removed

    def test_section_modified(self):
        old = _make_skill("# Title\n\nVersion 1 content.")
        new = _make_skill("# Title\n\nVersion 2 content.")
        d = skill_diff(old, new)
        assert "Title" in d.sections_modified

    def test_frontmatter_change(self):
        old = _make_skill("body", license="MIT")
        new = _make_skill("body", license="Apache-2.0")
        d = skill_diff(old, new)
        assert "license" in d.frontmatter_changes
        assert d.frontmatter_changes["license"] == ("MIT", "Apache-2.0")

    def test_text_diff_present(self):
        old = _make_skill("# Title\n\nLine A.")
        new = _make_skill("# Title\n\nLine B.")
        d = skill_diff(old, new)
        assert "Line A" in d.text_diff
        assert "Line B" in d.text_diff

    def test_summary_describes_changes(self):
        old = _make_skill("# Title\n\nOld.")
        new = _make_skill("# Title\n\nNew.\n\n## Added\n\nStuff.")
        d = skill_diff(old, new)
        assert "added sections" in d.summary
        assert "modified sections" in d.summary
