"""rev H1: promotion_validation / held_out / cross_role / genm traces NEVER
carry oracle_evidence, even with EVO_REFLECT_BLIND=0 (oracle-visible default)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.eval.evidence import _NEVER_ORACLE_PHASES, build_evidence


def _task_dir_with_tests(tmp_path: Path) -> Path:
    tests_dir = tmp_path / "task" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_outputs.py").write_text(
        "def test_x():\n    assert compute() == 42424242\n", encoding="utf-8"
    )
    return tmp_path / "task"


def _build(tmp_path: Path, phase: str | None):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "out.txt").write_text("x", encoding="utf-8")
    return build_evidence(
        workspace=ws,
        pre_snapshot={},
        solve_exit_code=1,
        solve_timeout=False,
        task_dir=_task_dir_with_tests(tmp_path),
        test_file="tests/test_outputs.py",
        trace_context={"phase": phase} if phase is not None else {},
        manifest_scope="skill_evolution",
    )


@pytest.mark.parametrize("phase", sorted(_NEVER_ORACLE_PHASES))
def test_never_oracle_phase_has_no_oracle_evidence(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("EVO_REFLECT_BLIND", "0")
    ev = _build(tmp_path, phase)
    assert "oracle_evidence" not in ev["failure_evidence"], phase


def test_collect_phase_control_does_get_oracle_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_REFLECT_BLIND", "0")
    ev = _build(tmp_path, "collect")
    assert "oracle_evidence" in ev["failure_evidence"]
    assert "42424242" in ev["failure_evidence"]["oracle_evidence"]["tests_source"]
