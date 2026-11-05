"""Paper §4 alignment guards: δ-gate spec design + split-controlled feedback.

These are deterministic/offline. The full δ-gate lifecycle (reflect→rewrite→
commit→validate→PROMOTE/ROLLBACK) is integration-tested by the pilot; here we
guard the pure, paper-critical contracts: diagnosis/validation stream split,
disjointness, validation seed band, the feedback sanitizer, the v4 feedback
flag gate, and the lineage rollback primitive the gate relies on.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))


def _preflight_mod():
    spec = importlib.util.spec_from_file_location(
        "preflight_pa",
        REPO_ROOT
        / "experiments"
        / "table3-4-minimal-sbbench-v2"
        / "runner"
        / "preflight_offline.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["preflight_pa"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_MANIFEST = {"models": {"source_models_single": [{"id": "m1"}]}}


def test_diagnosis_uses_train_a_validation_uses_train_b_disjoint(monkeypatch):
    monkeypatch.setenv("EVO_VAL_MAX_TASKS", "6")
    monkeypatch.setenv("EVO_VAL_ATTEMPTS", "1")
    mod = _preflight_mod()
    src_a, src_b = ["sa1", "sa2"], ["sb1", "sb2"]
    sup_a, sup_b = ["pa1"], ["pb1"]
    t1_src = "sa1"
    t1_val = sorted((set(src_a) | set(src_b)) - {t1_src})

    for diversity in ("T1", "T2", "T3"):
        row = {"task_diversity": diversity, "model_diversity": "Single"}
        diag = mod._trace_specs_for_row(
            row=row,
            source_train=sorted(src_a),
            support_train=sorted(sup_a),
            t1_source=t1_src,
            manifest=_MANIFEST,
        )
        val = mod._promotion_validation_specs_for_row(
            row=row,
            val_source=sorted(src_b),
            val_support=sorted(sup_b),
            t1_validation=t1_val,
            manifest=_MANIFEST,
        )
        diag_tasks = {s["task_id"] for s in diag}
        val_tasks = {s["task_id"] for s in val}
        # Disjoint by construction — the reflector never sees a validation task.
        assert diag_tasks.isdisjoint(val_tasks), (diversity, diag_tasks, val_tasks)
        # Validation seeds live in a band disjoint from collect seeds.
        assert all(s["attempt_seed"] >= 100000 for s in val)
        assert all(s.get("phase") == "promotion_validation" for s in val)
        if diversity == "T1":
            assert diag_tasks == {t1_src}
            assert val_tasks == set(t1_val)
        elif diversity == "T2":
            assert diag_tasks == set(src_a)
            assert val_tasks == set(src_b)
        else:
            assert diag_tasks == set(src_a) | set(sup_a)
            assert val_tasks == set(src_b) | set(sup_b)


def test_validation_max_tasks_cap_and_thin_pool(monkeypatch):
    monkeypatch.setenv("EVO_VAL_MAX_TASKS", "3")
    monkeypatch.setenv("EVO_VAL_ATTEMPTS", "1")
    mod = _preflight_mod()
    row = {"task_diversity": "T2", "model_diversity": "Single"}
    many = [f"vb{i}" for i in range(9)]
    capped = mod._promotion_validation_specs_for_row(
        row=row,
        val_source=many,
        val_support=[],
        t1_validation=[],
        manifest=_MANIFEST,
    )
    assert len({s["task_id"] for s in capped}) == 3
    # Thin pool (no disjoint validation task) -> empty spec, gate handles it.
    thin = mod._promotion_validation_specs_for_row(
        row=row,
        val_source=[],
        val_support=[],
        t1_validation=[],
        manifest=_MANIFEST,
    )
    assert thin == []


def test_sanitize_feedback_strips_oracle_signal():
    from evolution.eval import evidence as E

    raw = "\n".join(
        [
            "tests/test_outputs.py::test_total FAILED",
            "FAILED tests/test_outputs.py::test_total - AssertionError",
            "E       assert 0.1234 == 0.5678",
            "E       where 0.1234 = round(x)",
            "-  expected_value = 987654",
            "+  got_value = 12",
            "ran solve at /home/user/secret/workspace/out.csv",
            "model produced 'a_very_secret_constant' as the answer",
            "ok normal progress line",
        ]
    )
    out = E.sanitize_feedback(raw)
    assert "::test_" not in out
    assert "FAILED" not in out and "AssertionError" not in out
    assert "assert 0.1234" not in out and "where 0.1234" not in out
    assert "expected_value" not in out  # diff line dropped
    assert "987654" not in out and "1234" not in out and "5678" not in out
    assert "/home/user/secret/workspace" not in out
    assert "a_very_secret_constant" not in out
    assert "ok normal progress line" in out
    assert "<redacted>" in out
    # tail-bounded
    assert len(E.sanitize_feedback("x" * 5000, limit=100)) == 100


def test_build_evidence_source_feedback_gated_by_flag():
    from evolution.eval import evidence as E

    d = Path(tempfile.mkdtemp())
    stderr = "FAILED tests/test_x.py::test_a\nE assert 1234 == 5\nok tail"

    def _ev(feedback_enabled: bool) -> dict:
        return E.build_evidence(
            workspace=d,
            pre_snapshot={},
            solve_exit_code=1,
            solve_timeout=False,
            solve_exception="RuntimeError",
            solve_stderr_tail=stderr,
            feedback_enabled=feedback_enabled,
        )

    off = _ev(False)
    fe_off = off["failure_evidence"]
    assert off["schema"] == "evolution-trace-evidence-v5"
    assert fe_off["source_feedback_included"] is False
    assert "source_feedback" not in fe_off

    fe_on = _ev(True)["failure_evidence"]
    assert fe_on["source_feedback_included"] is True
    sf = fe_on["source_feedback"]
    assert sf["exception_class"] == "RuntimeError"
    assert "::test_" not in sf["stderr_tail"]
    assert "FAILED" not in sf["stderr_tail"]
    assert "1234" not in sf["stderr_tail"]
    assert "ok tail" in sf["stderr_tail"]


def test_lineage_reject_rolls_back_to_parent():
    from datetime import datetime

    from evolution.core.models import LineageEntry, Skill, SkillFrontmatter
    from evolution.lineage.tracker import LineageTracker
    from evolution.lineage.version import VersionStore

    def _entry(version: str, parent: str | None, origin: str) -> LineageEntry:
        return LineageEntry(version=version, parent=parent, timestamp=datetime.now(), origin=origin)

    d = Path(tempfile.mkdtemp())
    sd = d / ".agents" / "skills" / "s"
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nPARENT")
    sk = Skill(
        frontmatter=SkillFrontmatter(name="s", description="d"),
        body="PARENT",
        path=sd,
        skill_md_path=sd / "SKILL.md",
    )
    tr = LineageTracker(sk)
    vs = VersionStore(sk)
    tr.record(_entry("v1", None, "manual"))
    vs.save("v1")
    tr.promote("v1")
    # candidate body committed but NOT promoted (δ-gate --no-promote)
    (sd / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\nCANDIDATE")
    tr.record(_entry("v2", "v1", "mutation"))
    vs.save("v2")
    # ROLLBACK: keep parent active, mark candidate rejected (stays on disk).
    tr.promote("v1")
    tr.reject("v2")
    assert tr.current_version == "v1"
    assert tr.get_entry("v2").status == "rejected"
    assert (sd / "SKILL.md").read_text().endswith("PARENT")
    assert vs.exists("v2")  # rejected branch retained, never deleted


# --- Review blockers (verified real, fixed): contract + gating regressions ---

_HEX = "a" * 64


def _valid_plan(*, evolve_summary: dict, train_solve_summary: list) -> dict:
    """A produce plan that passes every other validate_produce_plan check, so
    a test isolates exactly the evolve_summary/gate-outcome branch."""
    return {
        "schema": "table3-framework-produce-v2",
        "framework": "evolution",
        "status": "completed",
        "valid_for_eval": True,
        "trace_spec_hash": _HEX,
        "n_traces_used": 1,
        "trace_budget": 1,
        "source_model_ids": ["m1"],
        "used_openrouter": False,
        "base_seed": {
            "policy": "source_trace_task_skill_union_v1",
            "seed_source_task_ids": ["t1"],
            "base_skill_names": ["sql"],
            "base_skill_hashes": {"sql": _HEX},
        },
        "records": [
            {
                "task_id": "t1",
                "source_model_id": "m1",
                "solve_status": "completed",
                "pytest_status": "completed",
            }
        ],
        "health": {
            "persistence_ok": True,
            "base_seed_ok": True,
            "eval_writes_blocked": True,
            "skipped_or_noop_labelled": True,
        },
        "state": {
            "evolution_plan": {
                "evolve_summary": evolve_summary,
                "train_solve_summary": train_solve_summary,
            }
        },
    }


_FAIL_TRACE = [{"task_id": "t1", "success": False, "total_tests": 3}]
_NO_EVOLVED = (
    "evolution has verifier-covered failed source traces but produced no evolved skill version"
)


def _issues(summary, train=_FAIL_TRACE):
    from _shared.table3_adapter_contract import validate_produce_plan

    return validate_produce_plan(
        _valid_plan(evolve_summary=summary, train_solve_summary=train),
        trace_spec_hash=_HEX,
        trace_budget=1,
        heldout_model_id="held",
    )


def test_gate_outcomes_valid_with_verifier_failures():
    # PROMOTE / ROLLBACK / ROLLBACK_NO_VALIDATION are honest mechanism
    # outcomes — must NOT be flagged "produced no evolved skill version".
    base = {"evolved": 0, "no_op": 0, "error": 0, "no_failure_signal": 0}
    for key in ("promoted", "rolled_back", "rolled_back_no_validation"):
        s = dict(base)
        s[key] = 1
        assert _NO_EVOLVED not in _issues(s), key
    # legacy evolved>0 and no_op still exempt
    assert _NO_EVOLVED not in _issues({**base, "evolved": 1})
    assert _NO_EVOLVED not in _issues({**base, "no_op": 1})


def test_no_failure_signal_still_invalid_with_verifier_failures():
    s = {"evolved": 0, "no_op": 0, "error": 0, "no_failure_signal": 1}
    assert _NO_EVOLVED in _issues(s)


def test_evolve_summary_error_is_invalid():
    s = {"evolved": 0, "no_op": 0, "error": 1, "promoted": 1, "no_failure_signal": 0}
    assert any("evolve_summary has errors" in i for i in _issues(s))


def test_promotion_validation_solve_is_not_source_feedback_gated_by_flag_alone():
    # Blocker 2: solver must gate source_feedback on phase, not EVO_SOURCE_FEEDBACK alone.
    import inspect

    from evolution.eval import solver as S

    src = inspect.getsource(S.solve_task)
    assert "feedback_enabled" in src
    assert 'os.environ.get("EVO_SOURCE_FEEDBACK") == "1"' in src
    assert "phase in (" in src and '"collect"' in src


def test_promotion_validation_raises_on_wiring_and_crash():
    # Blocker 3: _run_promotion_validation must raise (mechanism failure),
    # never silently score None/0.0, for missing task/backend/solve crash.
    import inspect

    from evolution.adapters import evolution_adapter as A

    src = inspect.getsource(A.produce_with_trace_spec)
    assert "promotion-validation task dir missing" in src
    assert "promotion-validation backend missing" in src
    assert "promotion-validation solve crashed" in src
    assert "assert_no_leakage" in src
    # the swallow paths are gone
    assert (
        "return None"
        not in src.split("_run_promotion_validation", 1)[1].split("# Cap solver concurrency", 1)[0]
    )


def test_candidate_detection_uses_pre_commit_max_not_active():
    # Blocker 4: a rejected candidate survives in lineage; candidate detection
    # must compare against the pre-commit max, not the active/before version.
    import inspect

    from evolution.adapters import evolution_adapter as A

    src = inspect.getsource(A.evolve_skills_one_round)
    assert "pre_commit_max = _max_committed_version(skill) if gated else None" in src
    assert "candidate_version == pre_commit_max" in src
    assert "candidate_version == before_version" not in src


def test_promotion_validation_workspace_is_per_call_unique():
    # Regression: parent-pass and candidate-pass reused promotion_validation/
    # <skill>/v_{j} → rmtree/mkdir race (OSError 39) killed whole anchors.
    import inspect

    from evolution.adapters import evolution_adapter as A

    src = inspect.getsource(A.produce_with_trace_spec)
    assert "call_token = _uuid.uuid4().hex" in src
    assert '"promotion_validation" / skill_name / call_token / f"v_{j:03d}"' in src
    assert "shutil.rmtree(ws, ignore_errors=True)" in src
