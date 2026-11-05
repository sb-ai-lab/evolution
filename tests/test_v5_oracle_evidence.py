"""v5 oracle_evidence capture gate (rev H1): oracle-visible by default,
opted out via EVO_REFLECT_BLIND=1; dynamic fields filled post-verifier."""

from __future__ import annotations

from pathlib import Path

from evolution.eval.evidence import attach_verifier_result, build_evidence


def _task_dir_with_tests(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_outputs.py").write_text(
        "def test_daily_total():\n    assert compute() == 123456789  # ORACLE_SENTINEL\n",
        encoding="utf-8",
    )
    return task_dir


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return ws


def _build(tmp_path: Path, *, phase: str | None = "collect", scope: str | None = "skill_evolution"):
    return build_evidence(
        workspace=_workspace(tmp_path),
        pre_snapshot={},
        solve_exit_code=1,
        solve_timeout=False,
        task_dir=_task_dir_with_tests(tmp_path),
        test_file="tests/test_outputs.py",
        trace_context={"phase": phase} if phase is not None else {},
        manifest_scope=scope,
    )


def test_v5_emits_oracle_evidence_when_visible(tmp_path, monkeypatch):
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    ev = _build(tmp_path)
    assert ev["schema"] == "evolution-trace-evidence-v5"
    oe = ev["failure_evidence"]["oracle_evidence"]
    assert "ORACLE_SENTINEL" in oe["tests_source"]
    assert "123456789" in oe["tests_source"]
    # Dynamic fields are seeded empty until attach_verifier_result runs.
    assert oe["failed_tests"] == []
    assert oe["pytest_traceback"] == ""


def test_v5_omits_oracle_evidence_when_blind(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_REFLECT_BLIND", "1")
    ev = _build(tmp_path)
    assert "oracle_evidence" not in ev["failure_evidence"]


def test_attach_verifier_result_fills_oracle_dynamic_fields(tmp_path, monkeypatch):
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    ev = _build(tmp_path)
    attach_verifier_result(
        ev,
        passed=0,
        total=1,
        pytest_stdout=(
            "FAILED tests/test_outputs.py::test_daily_total - assert 0 == 123456789\n"
            "E   assert 0 == 123456789\n"
        ),
        nodeids=["tests/test_outputs.py::test_daily_total"],
    )
    oe = ev["failure_evidence"]["oracle_evidence"]
    assert oe["failed_tests"][0]["nodeid"] == "tests/test_outputs.py::test_daily_total"
    assert "123456789" in oe["pytest_traceback"]


def test_v5_omits_oracle_evidence_for_non_evolution_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("EVO_REFLECT_BLIND", raising=False)
    ev = _build(tmp_path, scope="held_out_eval")
    assert "oracle_evidence" not in ev["failure_evidence"]
