from __future__ import annotations

import os
from pathlib import Path

import pytest

from evolution.adapters import memp_adapter
from evolution.adapters._memp_env import StepResult
from evolution.eval.evidence import snapshot_workspace
from evolution.eval.layouts.base import collect_outputs


def test_collect_outputs_copies_root_product_into_output(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    (ws / "input.csv").write_text("col\n1\n", encoding="utf-8")
    pre = snapshot_workspace(ws)
    # Agent writes a product at the root after the snapshot.
    (ws / "result.txt").write_text("answer", encoding="utf-8")

    res = collect_outputs(ws, pre)

    assert res.error is None
    assert res.copied == 1
    assert (ws / "output" / "result.txt").read_text(encoding="utf-8") == "answer"
    # Pre-existing input is not re-copied.
    assert not (ws / "output" / "input.csv").exists()


def test_collect_outputs_no_clobber_existing_output(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    pre = snapshot_workspace(ws)
    (ws / "result.txt").write_text("root", encoding="utf-8")
    (ws / "output" / "result.txt").write_text("existing", encoding="utf-8")

    res = collect_outputs(ws, pre)

    assert res.error is None
    assert (ws / "output" / "result.txt").read_text(encoding="utf-8") == "existing"


def test_collect_outputs_flattens_nested_output(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    nested = ws / "output" / "output"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("nested", encoding="utf-8")
    pre = snapshot_workspace(ws)

    res = collect_outputs(ws, pre)

    assert res.error is None
    assert res.flattened == 1
    assert (ws / "output" / "deep.txt").read_text(encoding="utf-8") == "nested"
    assert not (ws / "output" / "output").exists()


def test_collect_outputs_fail_loud_on_copy_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("read-only dir does not block root")
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    pre = snapshot_workspace(ws)
    (ws / "result.txt").write_text("answer", encoding="utf-8")
    (ws / "output").chmod(0o500)
    try:
        res = collect_outputs(ws, pre)
    finally:
        (ws / "output").chmod(0o700)

    # A genuine placement failure surfaces as error, never a silent copied==0.
    assert res.error is not None
    assert res.copied == 0


def test_memp_clean_solver_action_strips_provider_special_tokens() -> None:
    assert memp_adapter.clean_solver_action("list_files<|message|>") == "list_files"
    assert memp_adapter.clean_solver_action("Action: run_tests") == "run_tests"
    assert (
        memp_adapter.clean_solver_action("- read_file instruction.md") == "read_file instruction.md"
    )
    assert (
        memp_adapter.clean_solver_action(
            "<|start|>assistant<|channel|>commentary to=container.exec code\nls\n"
        )
        == "list_files"
    )
    assert memp_adapter.clean_solver_action("cat instruction.md") == "read_file instruction.md"
    assert (
        memp_adapter.clean_solver_action(
            "We need to inspect the solution. Let's read it.read_file solution.py"
        )
        == "read_file solution.py"
    )
    assert (
        memp_adapter.clean_solver_action("read_file solution.pyWe need to inspect it")
        == "read_file solution.py"
    )
    assert (
        memp_adapter.clean_solver_action(
            'We need to list files.{"cmd":["bash","-lc","ls -R"]}'
        )
        == "list_files"
    )
    assert (
        memp_adapter.clean_solver_action(
            "Inspect the file.{\"cmd\":[\"bash\",\"-lc\",\"sed -n '1,120p' instruction.md\"]}"
        )
        == "read_file instruction.md"
    )
    assert memp_adapter.clean_solver_action("I should inspect the workspace first.") == ""


def test_memp_clean_solver_action_preserves_write_solution_body() -> None:
    raw = "Action: write_solution import pathlib\npathlib.Path('x').write_text('y')"
    cleaned = memp_adapter.clean_solver_action(raw)
    assert cleaned.startswith("write_solution import pathlib")
    assert "write_text('y')" in cleaned


def test_memp_clean_solver_action_strips_python_code_wrapper() -> None:
    raw = "<python codeimport json\nprint(json.dumps({'ok': True}))"
    cleaned = memp_adapter.clean_solver_action(raw)
    assert cleaned.startswith("write_solution import json")
    assert "json.dumps" in cleaned


def test_memp_run_episode_rejects_premature_done(monkeypatch, tmp_path: Path) -> None:
    calls = iter(
        [
            ("done Unable to complete.", {"tokens_in": 1, "tokens_out": 1, "latency_ms": 1}),
            ("list_files<|message|>", {"tokens_in": 1, "tokens_out": 1, "latency_ms": 1}),
        ]
    )

    def fake_solver_chat(*args, **kwargs):
        return next(calls)

    class FakeEnv:
        task_id = "fake"
        total_tests = 1
        last_pass_rate = 0.0
        final_pass_rate = 0.0
        last_verifier_trace = {}
        workspace = tmp_path

        def reset(self) -> str:
            return "ready"

        def step(self, action: str) -> StepResult:
            assert action == "list_files"
            return StepResult("listing", done=False)

    monkeypatch.setattr(memp_adapter, "_solver_chat", fake_solver_chat)

    traj = memp_adapter.run_episode(
        FakeEnv(),
        task_instruction="do it",
        retrieved_workflows=[],
        solver_base="http://unused",
        solver_key="EMPTY",
        solver_model_id="unused",
        max_steps=2,
    )

    assert traj["steps"][0]["rejected_premature_done"] is True
    assert traj["steps"][0]["raw_action"].startswith("done")
    assert traj["steps"][1]["action"] == "list_files"


def test_memp_run_episode_stops_after_empty_action_streak(monkeypatch, tmp_path: Path) -> None:
    def fake_solver_chat(*args, **kwargs):
        return "", {
            "tokens_in": 1,
            "tokens_out": 1,
            "latency_ms": 1,
            "empty_content": True,
        }

    class FakeEnv:
        task_id = "fake"
        total_tests = 1
        last_pass_rate = 0.0
        final_pass_rate = 0.0
        last_verifier_trace = {}
        workspace = tmp_path

        def reset(self) -> str:
            return "ready"

        def step(self, action: str) -> StepResult:
            return StepResult("should not reach env after limit", done=False)

    monkeypatch.setattr(memp_adapter, "_solver_chat", fake_solver_chat)
    monkeypatch.setenv("EVO_MEMP_EMPTY_ACTION_LIMIT", "2")

    traj = memp_adapter.run_episode(
        FakeEnv(),
        task_instruction="do it",
        retrieved_workflows=[],
        solver_base="http://unused",
        solver_key="EMPTY",
        solver_model_id="unused",
        max_steps=10,
    )

    assert traj["solver_error"] == "empty_action_streak:2"
    assert traj["steps"][-1]["empty_action_limit_reached"] is True
    assert len(traj["steps"]) == 2
