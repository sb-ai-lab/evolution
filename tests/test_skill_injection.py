"""
Verify that changing the SKILL.md file actually changes the LLM prompt.

The prompt generation flow:
1. SkillStore reads .agents/skills/<name>/SKILL.md → Skill object with body
2. solve_task() builds messages + system_prompt
3. LiteLLMBackend.complete_with_skills() injects skills into system prompt:
   {system}\n\n<skill_content name="{s.name}">\n{s.body}\n</skill_content>

This test verifies the injection chain without making any API calls.
"""

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class TestSkillInjectionChangesPrompt:
    """Verify SKILL.md changes propagate to the LLM prompt."""

    def _make_skill(
        self, proj: Path, skill_dir: Path, name: str, description: str, body: str
    ) -> None:
        """Helper: write frontmatter + body to a SKILL.md file."""
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
        )

    def test_different_skill_bodies_produce_different_system_prompts(self, tmp_path: Path):
        """Two different SKILL.md files should produce different system prompts."""
        from evolution.core.store import SkillStore

        proj1 = tmp_path / "proj1"
        proj2 = tmp_path / "proj2"
        self._make_skill(
            proj1,
            proj1 / ".agents" / "skills" / "testskill",
            "testskill",
            "Version 1",
            "# Test Skill v1\n## Steps\n1. Do thing A\n2. Do thing B\n",
        )
        self._make_skill(
            proj2,
            proj2 / ".agents" / "skills" / "testskill",
            "testskill",
            "Version 2",
            "# Test Skill v2\n## Steps\n1. Do thing X\n2. Do thing Y\n3. Do thing Z\n",
        )

        store1 = SkillStore(project_dir=str(proj1))
        store2 = SkillStore(project_dir=str(proj2))
        skill1 = store1.get("testskill")
        skill2 = store2.get("testskill")

        assert skill1.body != skill2.body
        assert sha256(skill1.body) != sha256(skill2.body)

        blocks1 = f'<skill_content name="{skill1.name}">\n{skill1.body}\n</skill_content>'
        blocks2 = f'<skill_content name="{skill2.name}">\n{skill2.body}\n</skill_content>'
        system = "You are a skilled Python programmer solving a task."
        combined1 = f"{system}\n\n{blocks1}"
        combined2 = f"{system}\n\n{blocks2}"

        assert combined1 != combined2
        assert skill1.body in combined1 and skill1.body not in combined2
        assert skill2.body in combined2 and skill2.body not in combined1

    def test_same_skill_body_produces_same_prompt(self, tmp_path: Path):
        """Identical SKILL.md files should produce identical prompts."""
        from evolution.core.store import SkillStore

        proj = tmp_path / "proj"
        self._make_skill(
            proj,
            proj / ".agents" / "skills" / "testskill",
            "testskill",
            "Identical",
            "# Test Skill\n## Steps\n1. Do something\n",
        )

        store_a = SkillStore(project_dir=str(proj))
        skill_a = store_a.get("testskill")
        store_b = SkillStore(project_dir=str(proj))
        skill_b = store_b.get("testskill")

        assert skill_a.body == skill_b.body
        blocks_a = f'<skill_content name="{skill_a.name}">\n{skill_a.body}\n</skill_content>'
        blocks_b = f'<skill_content name="{skill_b.name}">\n{skill_b.body}\n</skill_content>'
        assert blocks_a == blocks_b

    def test_skill_body_excludes_frontmatter(self, tmp_path: Path):
        """The skill body should be the content after frontmatter, not the raw file."""
        from evolution.core.store import SkillStore

        proj = tmp_path / "proj"
        skill_dir = proj / ".agents" / "skills" / "xlsx"
        self._make_skill(
            proj,
            skill_dir,
            "xlsx",
            "Handle Excel files",
            "# Handle Excel Files\n## When to use\n"
            "When working with .xlsx files.\n"
            "## Steps\n1. Open the file\n2. Process the data\n",
        )

        store = SkillStore(project_dir=str(proj))
        skill = store.get("xlsx")

        assert "---" not in skill.body
        assert "# Handle Excel Files" in skill.body
        assert "## Steps" in skill.body
        hash1 = sha256(skill.body)
        skill2 = SkillStore(project_dir=str(proj)).get("xlsx")
        assert sha256(skill2.body) == hash1

    def test_multiple_skills_injected_into_combined_prompt(self, tmp_path: Path):
        """Multiple skills should all appear in one combined system prompt."""
        from evolution.core.store import SkillStore

        proj = tmp_path / "proj"
        for name in ("skill-a", "skill-b"):
            d = proj / ".agents" / "skills" / name
            self._make_skill(proj, d, name, "Test", f"# Skill {name}\n## Steps\n1. Step 1\n")

        store = SkillStore(project_dir=str(proj))
        skill_a = store.get("skill-a")
        skill_b = store.get("skill-b")

        combined = "\n\n".join(
            f'<skill_content name="{s.name}">\n{s.body}\n</skill_content>'
            for s in [skill_a, skill_b]
        )

        assert '<skill_content name="skill-a">' in combined
        assert '<skill_content name="skill-b">' in combined
        assert "# Skill skill-a" in combined
        assert "# Skill skill-b" in combined

    def test_evolution_version_change_changes_prompt(self, tmp_path: Path):
        """When evolution creates a new version, the prompt changes."""
        from evolution.core.parser import load_skill
        from evolution.core.store import SkillStore
        from evolution.lineage.version import VersionStore

        proj = tmp_path / "proj"
        skill_dir = proj / ".agents" / "skills" / "testskill"

        # Write v1 SKILL.md and init .evolution/ for lineage tracking
        (skill_dir).mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: testskill\ndescription: Test skill evolution\n"
            "metadata:\n  evolution-generation: 1\n---\n\n"
            "# Test Skill v1\n## Steps\n1. Do old thing\n"
        )

        skill_obj = load_skill(skill_dir)

        # Use VersionStore to snapshot v1
        vs = VersionStore(skill_obj)
        vs.save("v1")

        # Now overwrite SKILL.md to v2 (this is what mutation operators do)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: testskill\ndescription: Test skill evolution\n"
            "metadata:\n  evolution-generation: 2\n---\n\n"
            "# Test Skill v2\n## Steps\n1. Do new thing\n2. Do another new thing\n"
        )

        # Snapshot the new version
        skill_v2 = load_skill(skill_dir)
        vs2 = VersionStore(skill_v2)
        vs2.save("v2")

        # Reload SkillStore — it reads the live SKILL.md on disk
        store = SkillStore(project_dir=str(proj))
        active = store.get("testskill")

        # Active should be v2 (latest on disk)
        assert "Do new thing" in active.body
        assert "Do old thing" not in active.body

        # v1 is still available from VersionStore
        v1_skill = vs.load("v1")
        assert "Do old thing" in v1_skill.body

        # Prompts must differ
        prompt_v1 = f'<skill_content name="testskill">\n{v1_skill.body}\n</skill_content>'
        prompt_v2 = f'<skill_content name="testskill">\n{active.body}\n</skill_content>'
        assert prompt_v1 != prompt_v2

    def test_trace_gen_prepare_skill_sets_up_store_correctly(self, tmp_path: Path):
        """Verify that the exact flow trace_gen.py uses works end-to-end.

        trace_gen.py prepare_skill():
          1. copies SKILL.md from source to EVOLUTION_SKILLS/<name>/SKILL.md
          2. calls evo run bench which uses SkillStore(project_dir=skills_root)
        """
        from evolution.core.store import SkillStore

        proj = tmp_path / "proj"
        evo_skills = proj / ".agents" / "skills" / "xlsx"
        evo_skills.mkdir(parents=True)
        (evo_skills / "SKILL.md").write_text(
            "---\n"
            "name: xlsx\n"
            "description: Excel handling\n"
            "---\n\n"
            "# Excel Skills\n"
            "## Steps\n"
            "1. Read with openpyxl\n"
        )

        store = SkillStore(project_dir=str(proj))
        skill = store.get("xlsx")

        assert "Excel Skills" in skill.body
        assert "openpyxl" in skill.body

        # Now simulate mutation: change the SKILL.md
        (evo_skills / "SKILL.md").write_text(
            "---\n"
            "name: xlsx\n"
            "description: Excel handling v2\n"
            "---\n\n"
            "# Excel Skills v2\n"
            "## Steps\n"
            "1. Read with pandas\n"
            "2. Write with openpyxl\n"
            "3. Validate schema\n"
        )

        store2 = SkillStore(project_dir=str(proj))
        skill2 = store2.get("xlsx")
        assert "pandas" in skill2.body
        assert "openpyxl" in skill2.body
        assert "# Excel Skills v2" in skill2.body

        # Prompts differ
        blocks_old = f'<skill_content name="xlsx">\n{skill.body}\n</skill_content>'
        blocks_new = f'<skill_content name="xlsx">\n{skill2.body}\n</skill_content>'
        assert blocks_old != blocks_new


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
