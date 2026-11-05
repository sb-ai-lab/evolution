"""Tests for skill validator."""

from __future__ import annotations

from pathlib import Path

from evolution.core.validator import validate_skill


class TestValidator:
    def test_valid_skill_passes(self, fixtures_dir: Path):
        r = validate_skill(fixtures_dir / "valid-skill")
        assert r.valid
        assert r.errors == []

    def test_minimal_skill_passes(self, fixtures_dir: Path):
        r = validate_skill(fixtures_dir / "minimal-skill")
        assert r.valid

    def test_missing_skill_md(self, tmp_path: Path):
        r = validate_skill(tmp_path)
        assert not r.valid
        assert any("not found" in e for e in r.errors)

    def test_missing_description(self, tmp_path: Path):
        d = tmp_path / "bad-skill"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: bad-skill\n---\nbody\n")
        r = validate_skill(d)
        assert not r.valid
        assert any("description" in e.lower() for e in r.errors)

    def test_name_mismatch_warns(self, tmp_path: Path):
        d = tmp_path / "dir-name"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: different-name\ndescription: ok\n---\nbody\n")
        r = validate_skill(d)
        assert r.valid  # warning, not error
        assert any("does not match" in w for w in r.warnings)

    def test_empty_body_warns(self, tmp_path: Path):
        d = tmp_path / "empty-body"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: empty-body\ndescription: ok\n---\n")
        r = validate_skill(d)
        assert r.valid
        assert any("empty" in w for w in r.warnings)

    def test_long_body_warns(self, tmp_path: Path):
        d = tmp_path / "long-body"
        d.mkdir()
        body = "\n".join(f"Line {i}" for i in range(600))
        (d / "SKILL.md").write_text(f"---\nname: long-body\ndescription: ok\n---\n{body}\n")
        r = validate_skill(d)
        assert r.valid
        assert any("500" in w for w in r.warnings)

    def test_invalid_yaml(self, tmp_path: Path):
        d = tmp_path / "broken"
        d.mkdir()
        (d / "SKILL.md").write_text("---\n: : : garbage\n---\nbody\n")
        r = validate_skill(d)
        assert not r.valid

    def test_eval_suite_name_mismatch_warns(self, tmp_path: Path):
        d = tmp_path / "mismatch-eval"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: mismatch-eval\ndescription: ok\n---\nbody\n")
        evals_dir = d / "evals"
        evals_dir.mkdir()
        (evals_dir / "evals.json").write_text('{"skill_name": "wrong-name", "evals": []}')
        r = validate_skill(d)
        assert r.valid
        assert any("does not match" in w for w in r.warnings)
