from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

SAMPLE = resources.files("evolution.tree.reference_sample")
NAMES = ("score.py", "edit.py", "reflect.py", "corpus.jsonl")


def test_sample_files_resolve_through_importlib_resources():
    for name in NAMES:
        assert (SAMPLE / name).is_file(), name


@pytest.mark.parametrize("module", ["score", "edit", "reflect"])
def test_programs_are_import_safe(module):
    # Import in a child with stdin closed: an import that reads stdin or writes would fail or print.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import evolution.tree.reference_sample.{module} as m; assert callable(m.main)",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "" and completed.stderr == ""


def _run(name: str, stdin: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SAMPLE / name)],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


@pytest.mark.parametrize(
    "view, expected",
    [({"history": [{"text": "IMPROVED: x"}]}, 1.0), ({"history": [{"text": "plain"}]}, 0.0)],
)
def test_score_rewards_the_improved_line(tmp_path, view, expected):
    doc = {
        "case_id": "q1",
        "public": {"text": "question one"},
        "oracle": {"critical": True},
        "view": view,
    }
    completed = _run("score.py", json.dumps(doc), tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "metrics": {"score": expected},
        "critical_pass": True,
        "verdicts": [{"criterion": "improved", "passed": expected == 1.0}],
    }


@pytest.mark.parametrize("brief_version", ["edit_brief/5", "edit_brief/7"])
def test_edit_rewrites_the_first_line_once(tmp_path, brief_version):
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    skill = workspace / "skills" / "SKILL.md"
    skill.write_text("Reference skill: answer with this line.\n\nbody\n", encoding="utf-8")
    request = json.dumps(
        {
            "version": "edit_brief/1",
            "workspace": str(workspace),
            "brief": {"version": brief_version},
        }
    )
    first = _run("edit.py", request, tmp_path)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"status": "edited", "note": "first line improved"}
    assert skill.read_text(encoding="utf-8") == "IMPROVED: reference skill\n\nbody\n"
    before = skill.read_bytes()
    second = _run("edit.py", request, tmp_path)
    assert second.returncode == 0 and skill.read_bytes() == before


def test_edit_refuses_a_brief_it_was_not_written_for(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    skill = workspace / "skills" / "SKILL.md"
    skill.write_text("Reference skill: answer with this line.\n\nbody\n", encoding="utf-8")
    before = skill.read_bytes()
    request = json.dumps(
        {
            "version": "edit_brief/1",
            "workspace": str(workspace),
            "brief": {"version": "edit_brief/1"},
        }
    )
    completed = _run("edit.py", request, tmp_path)
    assert completed.returncode != 0
    assert skill.read_bytes() == before


def _reflect_request(workspace: Path, *, evidence: dict) -> str:
    """One adaptive `reflect_request/2` document."""

    return json.dumps(
        {
            "version": "reflect_request/2",
            "reflection_id": "r01-x01",
            "round": 1,
            "workspace": str(workspace),
            "editable_paths": ["skills/**"],
            "deadline_seconds": 30.0,
            "environment_docs": None,
            "probe": None,
            "evidence": evidence,
            "readers": _helper_readers(workspace),
        }
    )


def _helper_readers(workspace: Path) -> dict[str, list[str]]:
    helper = workspace / "reader_helper.py"
    calls = workspace / "reader_calls.jsonl"
    helper.write_text(
        """import json
import pathlib
import sys

calls = pathlib.Path(sys.argv[1])
arguments = json.loads(sys.argv[2])
operation = pathlib.Path(sys.argv[0]).stem.removesuffix("_helper")
calls.open("a", encoding="utf-8").write(json.dumps(arguments) + "\\n")
if "evidence_id" in arguments:
    print(json.dumps({"content": "authenticated", "next_offset": None, "total_characters": 13}))
else:
    print(json.dumps({"entries": [{"case_handle": "case-01"}], "next_offset": None, "total": 1}))
""",
        encoding="utf-8",
    )
    return {
        "list_evidence": [sys.executable, str(helper), str(calls)],
        "read_evidence": [sys.executable, str(helper), str(calls)],
    }


def _brief_payload(*, empty: bool = False) -> dict:
    return {
        "version": "reflect_brief/2",
        "portion": []
        if empty
        else [
            {
                "cohort_id": "cohort-01",
                "group_id": "group-01",
                "case_id": "case-01",
                "failure_count": 1,
                "exemplars": ["obs:case-01:1"],
                "same_case_success": None,
                "omitted_variant_count": 0,
            }
        ],
    }


def test_reflect_returns_one_group_per_portion_cohort(tmp_path):
    evidence = _brief_payload()
    (cohort,) = evidence["portion"]
    completed = _run("reflect.py", _reflect_request(tmp_path, evidence=evidence), tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "status": "reflected",
        "groups": [
            {
                "root_cause": (
                    "the skill's first line does not carry the improvement the scorer looks for"
                ),
                "suggested_fix": "rewrite the first line of skills/SKILL.md",
                "evidence_ids": [
                    cohort["exemplars"][0],
                ],
                "affected_cases": [cohort["case_id"]],
                "target_paths": ["skills/SKILL.md"],
                "counterevidence": "No passing observation was selected for this cohort.",
                "regression_risks": "Keep the existing response format while changing the first line.",
                "why_this_layer": "The observed failure is caused by the editable skill text.",
            }
        ],
        "note": "sample reflector",
    }
    calls = [
        json.loads(line) for line in (tmp_path / "reader_calls.jsonl").read_text().splitlines()
    ]
    assert calls == [{}, {"evidence_id": cohort["exemplars"][0], "offset": 0}]


def test_reflect_declines_an_empty_portion(tmp_path):
    evidence = _brief_payload(empty=True)
    assert evidence["portion"] == []
    completed = _run("reflect.py", _reflect_request(tmp_path, evidence=evidence), tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "status": "declined",
        "groups": [],
        "note": "sample reflector",
    }


def test_reflect_refuses_a_request_it_was_not_written_for(tmp_path):
    document = json.loads(_reflect_request(tmp_path, evidence=_brief_payload()))
    document["version"] = "reflect_request/1"
    completed = _run("reflect.py", json.dumps(document), tmp_path)
    assert completed.returncode == 2
    assert completed.stdout == ""
    # Not merely a non-zero exit: the refusal must name the version it wanted,
    # so a missing file (which also exits 2) cannot pass for one.
    assert "reflect_request/2" in completed.stderr


def test_reflect_groups_pass_the_role_validator(tmp_path):
    from evolution.tree.roles.reflector_contracts import validate_groups

    evidence = _brief_payload()
    completed = _run("reflect.py", _reflect_request(tmp_path, evidence=evidence), tmp_path)
    assert completed.returncode == 0, completed.stderr
    groups = json.loads(completed.stdout)["groups"]
    parsed = validate_groups(
        groups,
        catalog_ids={"obs:case-01:1": "case-01"},
        case_ids={"case-01"},
        editable_paths=["skills/**"],
        probe_seqs=None,
        needles=(),
        observation_ids={"obs:case-01:1": "case-01"},
        failing_evidence_ids={"obs:case-01:1"},
    )
    assert len(parsed) == len(groups)


def test_corpus_has_two_critical_rows_in_one_group():
    rows = [
        json.loads(line)
        for line in (SAMPLE / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["case_id"] for r in rows] == ["q1", "q2"]
    assert {r["group_id"] for r in rows} == {"g"} and {r["stratum"] for r in rows} == {"s"}
    assert all(r["oracle"] == {"critical": True} for r in rows)
    assert all(set(r) == {"case_id", "group_id", "stratum", "public", "oracle"} for r in rows)
