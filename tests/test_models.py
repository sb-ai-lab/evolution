"""Tests for core data models."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from evolution.core.models import (
    EvalCase,
    EvalSuite,
    LineageEntry,
    Skill,
    SkillFrontmatter,
    SkillLineage,
)


class TestSkillFrontmatter:
    def test_valid_minimal(self):
        fm = SkillFrontmatter(name="my-skill", description="Does something.")
        assert fm.name == "my-skill"
        assert fm.description == "Does something."

    def test_valid_full(self):
        fm = SkillFrontmatter(
            name="pdf-processing",
            description="Extracts text from PDFs.",
            license="MIT",
            compatibility="Requires python3",
            metadata={"author": "test", "version": "1.0"},
            allowed_tools="Bash Read",
        )
        assert fm.metadata == {"author": "test", "version": "1.0"}

    def test_name_uppercase_rejected(self):
        with pytest.raises(ValidationError, match="lowercase"):
            SkillFrontmatter(name="My-Skill", description="x")

    def test_name_leading_hyphen_rejected(self):
        with pytest.raises(ValidationError, match="start/end with hyphen"):
            SkillFrontmatter(name="-bad", description="x")

    def test_name_trailing_hyphen_rejected(self):
        with pytest.raises(ValidationError, match="start/end with hyphen"):
            SkillFrontmatter(name="bad-", description="x")

    def test_name_consecutive_hyphens_rejected(self):
        with pytest.raises(ValidationError, match="consecutive"):
            SkillFrontmatter(name="bad--name", description="x")

    def test_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SkillFrontmatter(name="a" * 65, description="x")

    def test_name_single_char(self):
        fm = SkillFrontmatter(name="x", description="ok")
        assert fm.name == "x"

    def test_description_empty_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            SkillFrontmatter(name="ok", description="")

    def test_description_whitespace_only_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            SkillFrontmatter(name="ok", description="   ")

    def test_description_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SkillFrontmatter(name="ok", description="x" * 1025)

    def test_compatibility_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SkillFrontmatter(name="ok", description="ok", compatibility="x" * 501)


class TestSkill:
    def _make_skill(self, **meta: str) -> Skill:
        return Skill(
            frontmatter=SkillFrontmatter(
                name="test",
                description="test skill",
                metadata=meta or None,
            ),
            body="# Test",
            path=Path("/fake/test"),
            skill_md_path=Path("/fake/test/SKILL.md"),
        )

    def test_fitness_from_metadata(self):
        s = self._make_skill(**{"evolution-fitness": "0.83"})
        assert s.fitness == pytest.approx(0.83)

    def test_fitness_none_when_absent(self):
        s = self._make_skill()
        assert s.fitness is None

    def test_generation_from_metadata(self):
        s = self._make_skill(**{"evolution-generation": "12"})
        assert s.generation == 12

    def test_generation_none_when_absent(self):
        s = self._make_skill()
        assert s.generation is None

    def test_name_delegates(self):
        s = self._make_skill()
        assert s.name == "test"

    def test_description_delegates(self):
        s = self._make_skill()
        assert s.description == "test skill"


class TestSkillLineage:
    def _make_lineage(self) -> SkillLineage:
        now = datetime.now(timezone.utc)
        return SkillLineage(
            skill_id="test",
            current_version="v3",
            created_at=now,
            lineage=[
                LineageEntry(version="v1", timestamp=now, origin="manual", status="retired"),
                LineageEntry(
                    version="v2",
                    timestamp=now,
                    origin="mutation",
                    parent="v1",
                    status="retired",
                ),
                LineageEntry(
                    version="v3",
                    timestamp=now,
                    origin="mutation",
                    parent="v2",
                    status="active",
                ),
            ],
        )

    def test_get_entry(self):
        lin = self._make_lineage()
        assert lin.get_entry("v2").origin == "mutation"

    def test_get_entry_missing(self):
        lin = self._make_lineage()
        with pytest.raises(KeyError):
            lin.get_entry("v99")

    def test_get_active(self):
        lin = self._make_lineage()
        assert lin.get_active().version == "v3"

    def test_ancestry(self):
        lin = self._make_lineage()
        chain = lin.ancestry("v3")
        assert [e.version for e in chain] == ["v1", "v2", "v3"]

    def test_ancestry_root(self):
        lin = self._make_lineage()
        chain = lin.ancestry("v1")
        assert [e.version for e in chain] == ["v1"]


class TestEvalSuite:
    def test_round_trip(self):
        suite = EvalSuite(
            skill_name="test",
            evals=[
                EvalCase(
                    id=1,
                    prompt="Do something",
                    expected_output="Something done",
                    assertions=["Output is non-empty"],
                ),
            ],
        )
        data = suite.model_dump()
        restored = EvalSuite.model_validate(data)
        assert restored.evals[0].prompt == "Do something"
