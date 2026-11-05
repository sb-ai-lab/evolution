"""Runtime-contract regression tests for the t34v7 follow-up fixes.

Covers:
  B1+B4: ``sanitize_task`` overwrites solve.sh to use ``sys.executable`` and
         run ``solution.py`` from ``<workspace>/output``.
  B2:    Output-location guidance is appended to ``instruction.md`` only when
         ``tests/test_outputs.py`` actually reads from ``output/``.
  B5:    ``solve_task`` writes ``prompt_audit.json`` with the structured
         system/user/skill metadata expected by the audit pipeline.

These tests do not exercise live LLMs; B5 uses a tiny stub backend so it
runs in the default (``not live``) selection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "experiments"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from _shared.task_freezer import sanitize_task  # noqa: E402
from _shared.workspace_materializer import materialize_workspace  # noqa: E402


def _write_raw_task(raw_dir: Path, *, tests_use_output: bool) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "task.toml").write_text(
        '[task]\nname = "tiny"\ndifficulty = "easy"\n', encoding="utf-8"
    )
    (raw_dir / "instruction.md").write_text("# Tiny task\n\nDo it.\n", encoding="utf-8")
    (raw_dir / "solve.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'TASK_ROOT="$(cd "$(dirname "$0")" && pwd)"\n'
        'cd "$TASK_ROOT/output"\n'
        'python3 "$TASK_ROOT/solution.py"\n',
        encoding="utf-8",
    )
    tests = raw_dir / "tests"
    tests.mkdir()
    if tests_use_output:
        body = 'from pathlib import Path\nOUTPUT = Path("output/result.json")\n'
    else:
        body = "def test_noop():\n    assert True\n"
    (tests / "test_outputs.py").write_text(body, encoding="utf-8")
    env = raw_dir / "environment" / "data"
    env.mkdir(parents=True)
    (env / "input.txt").write_text("x\n", encoding="utf-8")


def test_sanitize_task_overwrites_solve_sh_to_use_experiment_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_PYTHON", sys.executable)
    raw = tmp_path / "raw"
    _write_raw_task(raw, tests_use_output=True)
    clean = tmp_path / "clean"
    private = tmp_path / "private"

    sanitize_task(raw_task=raw, clean_task=clean, private_root=private)

    solve_sh = (clean / "solve.sh").read_text(encoding="utf-8")
    assert sys.executable in solve_sh, "solve.sh must invoke the experiment interpreter"
    assert "python3 " not in solve_sh, "bare python3 must not survive sanitisation"
    assert 'cd "$WS/output"' in solve_sh
    assert 'mkdir -p "$WS/output"' in solve_sh


def test_sanitize_task_appends_output_guidance_when_tests_use_output(tmp_path):
    raw = tmp_path / "raw"
    _write_raw_task(raw, tests_use_output=True)
    clean = tmp_path / "clean"
    private = tmp_path / "private"

    sanitize_task(raw_task=raw, clean_task=clean, private_root=private)

    instruction = (clean / "instruction.md").read_text(encoding="utf-8")
    assert "runs your solution from `<workspace>/output`" in instruction
    assert "write `redacted/paper1.pdf` from your script" in instruction
    assert "write `output/redacted/paper1.pdf`" not in instruction


def test_sanitize_task_skips_output_guidance_when_tests_dont_use_output(tmp_path):
    raw = tmp_path / "raw"
    _write_raw_task(raw, tests_use_output=False)
    clean = tmp_path / "clean"
    private = tmp_path / "private"

    sanitize_task(raw_task=raw, clean_task=clean, private_root=private)

    instruction = (clean / "instruction.md").read_text(encoding="utf-8")
    assert "Output Location" not in instruction


def test_sanitize_task_skips_output_guidance_when_no_solve_sh(tmp_path):
    raw = tmp_path / "raw"
    _write_raw_task(raw, tests_use_output=True)
    (raw / "solve.sh").unlink()
    clean = tmp_path / "clean"
    private = tmp_path / "private"

    sanitize_task(raw_task=raw, clean_task=clean, private_root=private)

    instruction = (clean / "instruction.md").read_text(encoding="utf-8")
    assert "Output Location" not in instruction
    assert "runs your solution from `<workspace>/output`" not in instruction


def test_root_cwd_contract_does_not_instruct_writing_to_output_subdir():
    from evolution.eval.solver import ROOT_CWD_CONTRACT

    assert "Write all output files into the `output/`" not in ROOT_CWD_CONTRACT
    assert "directory already exists" not in ROOT_CWD_CONTRACT
    assert "current directory" in ROOT_CWD_CONTRACT


def test_materialize_workspace_uses_same_output_cwd_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_PYTHON", sys.executable)
    raw = tmp_path / "raw"
    _write_raw_task(raw, tests_use_output=True)
    ws = tmp_path / "ws"

    materialize_workspace(raw, ws, agent_solve_sh=True)

    solve_sh = (ws / "solve.sh").read_text(encoding="utf-8")
    assert sys.executable in solve_sh
    assert 'cd "$WS/output"' in solve_sh
    assert "[ -f solution.py ]" not in solve_sh


@pytest.mark.parametrize("persist_audit_env", [None, "1"])
def test_prompt_audit_sidecar_is_written(tmp_path, monkeypatch, persist_audit_env):
    """B5: solver writes prompt_audit.json with structured fields."""
    from evolution.eval.solver import solve_task
    from evolution.eval.task import Task
    from evolution.llm.mock_backend import MockBackend

    if persist_audit_env is None:
        monkeypatch.delenv("EVO_PERSIST_PROMPT_AUDIT", raising=False)
    else:
        monkeypatch.setenv("EVO_PERSIST_PROMPT_AUDIT", persist_audit_env)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "output").mkdir()
    task = Task(
        name="audit_smoke",
        instruction="echo OK",
        difficulty="easy",
        category="test",
        skills_required=[],
        path=tmp_path,
    )
    backend = MockBackend(default_response="```python\nprint('OK')\n```")
    solve_task(task=task, backend=backend, workspace=ws, evaluate=False, model_name="mock/test")

    audit_path = ws / "prompt_audit.json"
    assert audit_path.is_file(), "prompt_audit.json must be written"
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["model_id"] == "mock/test"
    assert payload["task"] == "audit_smoke"
    assert "echo OK" in payload["user"]
    assert "execution_contract" in payload
    assert isinstance(payload["skills"], list)


def test_prompt_audit_sidecar_is_skipped_when_disabled(tmp_path, monkeypatch):
    from evolution.eval.solver import solve_task
    from evolution.eval.task import Task
    from evolution.llm.mock_backend import MockBackend

    monkeypatch.setenv("EVO_PERSIST_PROMPT_AUDIT", "0")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "output").mkdir()
    task = Task(
        name="audit_off",
        instruction="x",
        difficulty="easy",
        category="test",
        skills_required=[],
        path=tmp_path,
    )
    backend = MockBackend(default_response="```python\npass\n```")
    solve_task(task=task, backend=backend, workspace=ws, evaluate=False, model_name="mock/test")
    assert not (ws / "prompt_audit.json").exists()
