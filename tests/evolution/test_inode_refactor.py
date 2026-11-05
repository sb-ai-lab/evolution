"""Core inode-refactor units: workspace/leaf purge, debug-text archive,
parametrized materializer, and the role-env venv-disable branch."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from evolution.eval.trace_cleanup import (
    archive_cell_debug_text,
    purge_leaf_bulk,
    purge_workspace_bulk,
)
from evolution.eval.workspace_guard import build_guarded_env
from evolution.eval.workspace_materializer import materialize_workspace

# ── purge_workspace_bulk (core solve workspace: keep-set-preserving) ──────────


def _make_core_workspace(root: Path) -> Path:
    ws = root / "latest_t1"
    (ws / "environment" / "data").mkdir(parents=True)
    (ws / "environment" / "data" / "big.csv").write_text("x" * 100, encoding="utf-8")
    (ws / ".venv" / "lib").mkdir(parents=True)
    (ws / "_solve_home").mkdir()
    (ws / "_workspace_guard_bin").mkdir()
    (ws / "output").mkdir()
    (ws / "output" / "result.json").write_text("{}", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_outputs.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "m.pyc").write_text("", encoding="utf-8")
    for name in ("_prompt.md", "_response.md", "_solve.py", "pytest_output.txt"):
        (ws / name).write_text("debug", encoding="utf-8")
    (ws / ".workspace_guard.jsonl").write_text("{}", encoding="utf-8")
    (ws / "solve.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    return ws


def test_purge_workspace_bulk_removes_corpus_keeps_debug(tmp_path: Path) -> None:
    ws = _make_core_workspace(tmp_path)
    hidden = ws.parent / f"_hidden_tests__{ws.name}"
    hidden.mkdir()
    (hidden / "t.py").write_text("x", encoding="utf-8")

    removed = purge_workspace_bulk(ws)

    assert removed == 4  # environment, .venv, _solve_home, _workspace_guard_bin
    for gone in ("environment", ".venv", "_solve_home", "_workspace_guard_bin", "__pycache__"):
        assert not (ws / gone).exists()
    assert not hidden.exists()
    for kept in ("_prompt.md", "_response.md", "_solve.py", "pytest_output.txt", "solve.sh"):
        assert (ws / kept).read_text() in ("debug", "#!/bin/bash\n")
    assert (ws / "output" / "result.json").exists()
    assert (ws / "tests" / "test_outputs.py").exists()


def test_purge_workspace_bulk_optout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _make_core_workspace(tmp_path)
    monkeypatch.setenv("EVO_KEEP_CELL_TRACES", "1")
    assert purge_workspace_bulk(ws) == 0
    assert (ws / "environment").exists()


def test_purge_workspace_bulk_missing_noop(tmp_path: Path) -> None:
    assert purge_workspace_bulk(None) == 0
    assert purge_workspace_bulk(tmp_path / "nope") == 0


# ── purge_leaf_bulk (AFTER cache leaf: path-scoped) ───────────────────────────


def test_purge_leaf_bulk_is_path_scoped(tmp_path: Path) -> None:
    leaf = tmp_path / "solver_cache" / "round0" / "task"
    (leaf / "workspace" / "environment").mkdir(parents=True)
    (leaf / "_hermes_home").mkdir(parents=True)
    (leaf / "bundle.json").write_text("{}", encoding="utf-8")
    # Memento's evolved skill lives under native_state/.../workspace and MUST survive.
    skill = tmp_path / "native_state" / "memento_s" / "workspace" / "d" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("evolved", encoding="utf-8")

    removed = purge_leaf_bulk(leaf)

    assert removed == 2
    assert not (leaf / "workspace").exists()
    assert not (leaf / "_hermes_home").exists()
    assert (leaf / "bundle.json").exists()
    assert skill.read_text() == "evolved"


# ── archive_cell_debug_text ───────────────────────────────────────────────────


def test_archive_collapses_debug_keeps_machine_read(tmp_path: Path) -> None:
    cell = tmp_path / "cell"
    leaf = cell / "solver_cache" / "round0" / "task"
    leaf.mkdir(parents=True)
    for name in ("prompt.md", "response.md", "solve.py", "pytest.log", "solve_run.log"):
        (leaf / name).write_text(name, encoding="utf-8")
    (leaf / "bundle.json").write_text('{"keep":1}', encoding="utf-8")
    (cell / "cell_metrics.json").write_text("{}", encoding="utf-8")
    ns = cell / "native_state" / "memento_s"
    ns.mkdir(parents=True)
    (ns / "response.md").write_text("EVOLVED", encoding="utf-8")  # name-collides but protected

    archive = archive_cell_debug_text(cell)

    assert archive is not None and archive.name in {"cell_traces.tar.zst", "cell_traces.tar.gz"}
    for gone in ("prompt.md", "response.md", "solve.py", "pytest.log", "solve_run.log"):
        assert not (leaf / gone).exists()
    assert (leaf / "bundle.json").exists()
    assert (cell / "cell_metrics.json").exists()
    assert (ns / "response.md").read_text() == "EVOLVED"

    if archive.suffix == ".zst":
        import zstandard

        with open(archive, "rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as r:
            with tarfile.open(fileobj=r, mode="r|") as tar:
                names = sorted(m.name for m in tar)
    else:
        with tarfile.open(archive, mode="r:gz") as tar:
            names = sorted(tar.getnames())
    assert names == [
        "solver_cache/round0/task/prompt.md",
        "solver_cache/round0/task/pytest.log",
        "solver_cache/round0/task/response.md",
        "solver_cache/round0/task/solve.py",
        "solver_cache/round0/task/solve_run.log",
    ]


def test_archive_optout_and_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cell = tmp_path / "cell"
    (cell / "leaf").mkdir(parents=True)
    (cell / "leaf" / "prompt.md").write_text("p", encoding="utf-8")
    monkeypatch.setenv("EVO_KEEP_CELL_TRACES", "1")
    assert archive_cell_debug_text(cell) is None
    assert (cell / "leaf" / "prompt.md").exists()
    monkeypatch.delenv("EVO_KEEP_CELL_TRACES")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert archive_cell_debug_text(empty) is None


# ── parametrized materializer: core vs agent contract ─────────────────────────


def _write_sbbench_task(task_dir: Path) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        '[task]\nname = "tiny"\ndifficulty = "easy"\n', encoding="utf-8"
    )
    (task_dir / "instruction.md").write_text("do it", encoding="utf-8")
    env_data = task_dir / "environment" / "data"
    env_data.mkdir(parents=True)
    (env_data / "input.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (task_dir / "solve.sh").write_text(
        "#!/usr/bin/env bash\npython3 solution.py\n", encoding="utf-8"
    )
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test_outputs.py").write_text(
        "def test_x():\n    pass\n", encoding="utf-8"
    )


def test_materialize_core_contract_keeps_tree(tmp_path: Path) -> None:
    task = tmp_path / "task"
    _write_sbbench_task(task)
    ws = tmp_path / "ws_core"
    materialize_workspace(task, ws, contract="core")
    # core keeps environment/data nested and the original solve.sh
    assert (ws / "environment" / "data" / "input.csv").read_text() == "a,b\n1,2\n"
    assert "python3 solution.py" in (ws / "solve.sh").read_text()
    assert not (ws / "input.csv").exists()  # NOT flattened
    assert (ws / "output").is_dir()


def test_materialize_agent_contract_flattens(tmp_path: Path) -> None:
    task = tmp_path / "task"
    _write_sbbench_task(task)
    ws = tmp_path / "ws_agent"
    materialize_workspace(task, ws, contract="agent")
    # agent flattens environment/data to the root and rewrites solve.sh to run from output/
    assert (ws / "input.csv").read_text() == "a,b\n1,2\n"
    sh = (ws / "solve.sh").read_text()
    assert 'cd "$WS/output"' in sh
    # tests dir is skipped in the agent corpus copy
    assert not (ws / "tests").exists()


def test_materialize_unknown_contract_raises(tmp_path: Path) -> None:
    task = tmp_path / "task"
    _write_sbbench_task(task)
    with pytest.raises(ValueError, match="unknown materialize contract"):
        materialize_workspace(task, tmp_path / "ws", contract="bogus")


# ── venv-disable / role-env PATH hardening ────────────────────────────────────


def test_make_task_venv_defaults_to_role_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.delenv("EVO_HERMES_TASK_VENV", raising=False)
    # Point role "de" at the current interpreter so its bin is a real dir.
    role_bin = str(Path(sys.executable).parent)
    monkeypatch.setenv("EVO_ROLE_PYTHON_DE", sys.executable)
    ws = tmp_path / "ws"
    ws.mkdir()
    env = build_guarded_env(
        framework="hermes",
        workspace_dir=ws,
        run_home=ws / "_solve_home",
        audit_log_path=ws / ".workspace_guard.jsonl",
        base_env={"PATH": "/usr/bin"},
        set_home=True,
        make_task_venv=True,
        task_role="de",
    )
    assert not (ws / ".venv").exists()
    assert env["PATH"].split(":")[0] == role_bin
    assert env.get("EVOLUTION_TASK_ROLE_PYTHON") == sys.executable
    assert "VIRTUAL_ENV" not in env


def test_make_task_venv_flag_restores_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setenv("EVO_HERMES_TASK_VENV", "1")
    monkeypatch.setenv("EVO_ROLE_PYTHON_DE", sys.executable)
    ws = tmp_path / "ws"
    ws.mkdir()
    build_guarded_env(
        framework="hermes",
        workspace_dir=ws,
        run_home=ws / "_solve_home",
        audit_log_path=ws / ".workspace_guard.jsonl",
        base_env={"PATH": "/usr/bin"},
        set_home=True,
        make_task_venv=True,
        task_role="de",
    )
    assert (ws / ".venv" / "bin" / "python").exists()
