"""Train-oracle provenance reporting.

With train-oracle evidence (v5), the reflector may see training verifier source
and expected values. ``leakage_provenance_corpus`` still harvests those literals
so ``scan_leakage`` can report copied train-provenance text for audit/debugging.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from evolution.core.models import Skill, SkillFrontmatter, SkillTrace
from evolution.evolve.rewrite_guards import leakage_provenance_corpus, scan_leakage
from evolution.lineage.traces import TraceStore

_ORACLE_TESTS_SOURCE = (
    "def test_value():\n    assert row['kind'] == 'DriversLicense'\n    assert total == 123456789\n"
)


def _make_skill(tmp_path: Path) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nUse Python.\n",
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body="Use Python.",
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _save_oracle_trace(skill: Skill) -> None:
    trace = SkillTrace(
        id="oracle-trace",
        skill_name=skill.name,
        skill_version="v1",
        model="solver-model",
        task="task-name",
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
    )
    TraceStore(skill).save(
        trace,
        prompt="p",
        response="r",
        code="print('wrong')",
        evidence={
            "schema": "evolution-trace-evidence-v5",
            "agent_solve": {"exit_code": 1, "timeout": False, "diagnostic": "nonzero_exit"},
            "produced_artifacts": [],
            "failure_evidence": {
                "failure_categories": ["produced_but_failed_verification"],
                "produced_outputs": [],
                "verifier_result": {
                    "passed": 0,
                    "total": 1,
                    "verification_category": "produced_but_failed_verification",
                },
                "solve_exception": None,
                "code_generation_status": "ok",
                "oracle_evidence": {
                    "tests_source": _ORACLE_TESTS_SOURCE,
                    "source_artifacts": [],
                    "failed_tests": [
                        {"nodeid": "tests/test_outputs.py::test_value", "message_tail": "assert"}
                    ],
                    "pytest_traceback": "E   assert 0 == 123456789",
                },
            },
            "trace_context": {"trace_spec_idx": 0, "attempt_seed": 0},
        },
    )


def test_oracle_corpus_harvests_test_literals(tmp_path):
    skill = _make_skill(tmp_path)
    _save_oracle_trace(skill)
    corpus = leakage_provenance_corpus(skill, "task-name")
    assert "DriversLicense" in corpus
    assert "123456789" in corpus


def test_oracle_literal_copied_into_skill_is_reported(tmp_path):
    skill = _make_skill(tmp_path)
    _save_oracle_trace(skill)
    corpus = leakage_provenance_corpus(skill, "task-name")

    leaked_body = (
        "## Extraction\n"
        "Always emit the DriversLicense field verbatim.\n"
        "The grand total must equal exactly 123456789 for this dataset.\n"
    )
    findings = scan_leakage(leaked_body, corpus)
    assert findings, "copying train-oracle literals into the skill body should be reported"
    assert any("DriversLicense" in f for f in findings)
    assert any("123456789" in f for f in findings)


def test_general_rewrite_with_oracle_corpus_is_clean(tmp_path):
    skill = _make_skill(tmp_path)
    _save_oracle_trace(skill)
    corpus = leakage_provenance_corpus(skill, "task-name")

    general_body = (
        "## Extraction\n"
        "Identify each labelled field from the document and validate it against "
        "the task's stated schema before writing to output/.\n"
        "Cross-check aggregate totals against the per-row sum you computed.\n"
    )
    assert scan_leakage(general_body, corpus) == []
