"""Tests for SKILL.md parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.core.parser import load_eval_suite, load_skill, parse_skill_md


class TestParseSkillMd:
    def test_valid_skill(self, fixtures_dir: Path):
        fm, body = parse_skill_md(fixtures_dir / "valid-skill" / "SKILL.md")
        assert fm["name"] == "valid-skill"
        assert "parser" in fm["description"].lower()
        assert "# Valid Skill" in body

    def test_minimal_skill(self, fixtures_dir: Path):
        fm, body = parse_skill_md(fixtures_dir / "minimal-skill" / "SKILL.md")
        assert fm["name"] == "minimal-skill"
        assert "Minimal instructions" in body
        assert "license" not in fm

    def test_malformed_colon_in_description(self, fixtures_dir: Path):
        fm, body = parse_skill_md(fixtures_dir / "malformed-skill" / "SKILL.md")
        assert fm["name"] == "malformed-skill"
        # Should have parsed despite the colon
        assert "description" in fm

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            parse_skill_md(tmp_path / "nonexistent" / "SKILL.md")

    def test_no_frontmatter(self, tmp_path: Path):
        f = tmp_path / "SKILL.md"
        f.write_text("Just some markdown with no frontmatter.\n")
        with pytest.raises(ValueError, match="No YAML frontmatter"):
            parse_skill_md(f)


class TestLoadSkill:
    def test_from_directory(self, fixtures_dir: Path):
        skill = load_skill(fixtures_dir / "valid-skill")
        assert skill.name == "valid-skill"
        assert skill.fitness == pytest.approx(0.83)
        assert skill.generation == 5
        assert "# Valid Skill" in skill.body

    def test_from_file(self, fixtures_dir: Path):
        skill = load_skill(fixtures_dir / "valid-skill" / "SKILL.md")
        assert skill.name == "valid-skill"

    def test_scripts_listed(self, fixtures_dir: Path):
        skill = load_skill(fixtures_dir / "valid-skill")
        assert "scripts/helper.sh" in skill.scripts

    def test_name_mismatch_warns(self, tmp_path: Path):
        d = tmp_path / "wrong-dir-name"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: actual-name\ndescription: test\n---\nbody\n")
        with pytest.warns(match="does not match"):
            skill = load_skill(d)
        assert skill.name == "actual-name"

    def test_missing_description_raises(self, tmp_path: Path):
        d = tmp_path / "bad"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: bad\n---\nbody\n")
        with pytest.raises(Exception):
            load_skill(d)

    def test_minimal_skill(self, fixtures_dir: Path):
        skill = load_skill(fixtures_dir / "minimal-skill")
        assert skill.name == "minimal-skill"
        assert skill.fitness is None
        assert skill.generation is None
        assert skill.scripts == []


class TestLoadEvalSuite:
    def test_loads_existing(self, fixtures_dir: Path):
        suite = load_eval_suite(fixtures_dir / "valid-skill")
        assert suite is not None
        assert suite.skill_name == "valid-skill"
        assert len(suite.evals) == 1
        assert suite.evals[0].id == 1

    def test_returns_none_when_absent(self, fixtures_dir: Path):
        suite = load_eval_suite(fixtures_dir / "minimal-skill")
        assert suite is None
