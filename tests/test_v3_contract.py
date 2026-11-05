"""Regression guards for the v3 evidence + reflect-over-K + low-confidence-no-op contract."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))


def test_artifact_shape_counts_only_no_labels_failsoft():
    from evolution.eval import artifact_shape as S

    d = Path(tempfile.mkdtemp())
    (d / "o.json").write_text('{"alpha": 1, "beta": 2}')
    (d / "r.csv").write_text("colA,colB\n1,2\n3,4\n")
    (d / "bad.json").write_text("{not json")
    js = S.shape_for(d / "o.json")
    assert js == {"top_type": "object", "n_keys": 2, "depth_ok": True}
    cs = S.shape_for(d / "r.csv")
    assert cs == {"n_cols": 2, "n_rows": 2, "has_header": True}
    bad = S.shape_for(d / "bad.json")
    assert bad["parse_ok"] is False and "error" in bad
    assert S.shape_for(d / "x.txt") is None
    # no literal labels/values anywhere
    blob = f"{js}{cs}{bad}"
    for leak in ("alpha", "beta", "colA", "colB"):
        assert leak not in blob


def test_v3_evidence_all_exit_paths_and_two_phase():
    from evolution.eval import evidence as E

    d = Path(tempfile.mkdtemp())
    (d / "out.csv").write_text("a,b\n1,2\n")
    (d / "bad.json").write_text("{not json")
    # normal path
    ev = E.build_evidence(
        workspace=d,
        pre_snapshot={},
        solve_exit_code=1,
        solve_timeout=False,
        required_outputs=["output/out.csv", "r.json"],
        trace_context={"trace_spec_idx": 3, "attempt_seed": 9, "source_model_id": "m"},
    )
    assert ev["schema"] == "evolution-trace-evidence-v5"
    fe = ev["failure_evidence"]
    assert fe["verifier_result"]["verification_category"] == "not_run"
    assert "invalid_output_shape" in fe["failure_categories"]
    assert fe["required_output_count"] == 2 and ".csv" in fe["required_output_suffixes"]
    assert ev["trace_context"]["attempt_seed"] == 9
    # no literal required path leaked into the packet
    assert "output/out.csv" not in str(fe.get("required_output_suffixes"))
    # Phase B
    E.attach_verifier_result(ev, passed=1, total=3)
    assert fe["verifier_result"]["verification_category"] == "produced_but_failed_verification"
    # timeout/exception still emit full v3 (gate must not abort them)
    evt = E.build_evidence(workspace=d, pre_snapshot={}, solve_exit_code=None, solve_timeout=True)
    assert evt["schema"] == "evolution-trace-evidence-v5"
    assert "timeout" in evt["failure_evidence"]["failure_categories"]
    assert evt["failure_evidence"]["verifier_result"]["verification_category"] == "not_run"
    eve = E.build_evidence(
        workspace=d,
        pre_snapshot={},
        solve_exit_code=1,
        solve_timeout=False,
        solve_diagnostic="exception",
        solve_exception="RuntimeError",
        solve_exception_frame="solve.py:12",
    )
    assert "runtime_exception" in eve["failure_evidence"]["failure_categories"]
    assert eve["failure_evidence"]["solve_exception"] == {
        "type": "RuntimeError",
        "frame": "solve.py:12",
    }


def test_required_outputs_instruction_only_count_suffix():
    from evolution.eval.task import load_task

    d = Path(tempfile.mkdtemp())
    (d / "task.toml").write_text('name = "t"\n')
    (d / "instruction.md").write_text("Produce `output/report.json` and `sum.csv`.")
    t = load_task(d)
    assert sorted(t.required_outputs) == ["output/report.json", "sum.csv"]
    # explicit task.toml overrides
    (d / "task.toml").write_text('name = "t"\nrequired_outputs = ["x/y.parquet"]\n')
    assert load_task(d).required_outputs == ["x/y.parquet"]


def test_trace_id_backcompat_and_collision():
    spec = importlib.util.spec_from_file_location(
        "solver_v3t", REPO_ROOT / "src" / "evolution" / "eval" / "solver.py"
    )
    import inspect

    from evolution.eval import solver as S

    src = inspect.getsource(S._save_trace)
    # attempt_seed=None keeps the exact old format; a seed adds µs + a{seed}
    assert "if attempt_seed is None:" in src
    assert "now.microsecond:06d" in src and "_a{attempt_seed:04d}_" in src
    assert spec is not None


def test_k_attempts_spec_multiplicity():
    fpv = importlib.util.spec_from_file_location(
        "preflight_v3t",
        REPO_ROOT
        / "experiments"
        / "table3-4-minimal-sbbench-v2"
        / "runner"
        / "preflight_offline.py",
    )
    mod = importlib.util.module_from_spec(fpv)  # type: ignore[arg-type]
    sys.modules["preflight_v3t"] = mod
    fpv.loader.exec_module(mod)  # type: ignore[union-attr]
    row = {"task_diversity": "T1", "model_diversity": "Single"}
    manifest_k3 = {
        "models": {"source_models_single": [{"id": "m1"}]},
        "attempts_per_source_model": 3,
    }
    manifest_k1 = {"models": {"source_models_single": [{"id": "m1"}]}}
    s3 = mod._trace_specs_for_row(
        row=row, source_train=["t"], support_train=[], t1_source="t", manifest=manifest_k3
    )
    s1 = mod._trace_specs_for_row(
        row=row, source_train=["t"], support_train=[], t1_source="t", manifest=manifest_k1
    )
    assert len(s3) == 3 and len(s1) == 1
    assert sorted(x["attempt_seed"] for x in s3) == [0, 1, 2]


def test_low_confidence_noop_keeps_parent():
    import asyncio

    from evolution.core.models import Skill, SkillFrontmatter
    from evolution.evolution.reflect import rewrite_skill
    from evolution.llm.mock_backend import MockBackend

    d = Path(tempfile.mkdtemp())
    sd = d / ".agents" / "skills" / "s"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nPARENT_BODY")
    sk = Skill(
        frontmatter=SkillFrontmatter(name="s", description="d"),
        body="PARENT_BODY",
        path=sd,
        skill_md_path=sd / "SKILL.md",
    )
    refl_bad = [
        {
            "root_cause": "?",
            "skill_gap": "?",
            "suggested_fix": "?",
            "severity": "minor",
            "pattern": "other",
            "evidence_sufficiency": "insufficient",
            "rewrite_recommended": False,
        }
    ]
    out = asyncio.run(rewrite_skill(MockBackend(default_response="NEW"), sk, refl_bad))
    assert out == "PARENT_BODY"  # kept parent (low-confidence no-op), no rewrite


def test_reflection_audit_counts_traces_not_tasks():
    from evolution.adapters import evolution_adapter as A

    src = inspect.getsource(A.evolve_skills_one_round)
    assert '"n_failing_tasks": len(failing_tasks)' in src
    assert '"n_failing_traces": len(audit)' in src
    assert '"n_reflections_used": len(audit)' in src
