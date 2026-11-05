from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evolution.adapters.workspace_guard import (  # noqa: E402
    build_guarded_env,
    make_workspace_guard,
    path_block_reason,
    terminal_command_block_reason,
)


def test_guard_allows_relative_workspace_paths(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    assert path_block_reason("instruction.md", ws) is None
    assert path_block_reason("./outputs/result.json", ws) is None
    assert terminal_command_block_reason("find . -name '*.pdf'", ws) is None


def test_guard_blocks_broad_home_search(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    reason = terminal_command_block_reason(
        f'find {Path.home()} -name "quarterly_report.pdf"',
        ws,
    )

    assert reason is not None
    assert "outside the task workspace" in reason or "host directory" in reason


def test_guard_allows_actual_workspace_absolute_path(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    assert path_block_reason(str(ws / "instruction.md"), ws) is None
    assert terminal_command_block_reason(f"ls {ws}", ws) is None


def test_guard_blocks_file_tool_path_escape(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()

    reason = path_block_reason(str(Path.home() / "quarterly_report.pdf"), ws)

    assert reason is not None
    assert "must stay inside" in reason


def test_guard_writes_audit_log_for_block(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    log_path = tmp_path / "guard.jsonl"
    guard = make_workspace_guard(
        framework="memp",
        workspace_dir=ws,
        audit_log_path=log_path,
    )

    reason = guard.path_block_reason(str(Path.home() / "quarterly_report.pdf"), label="read_file")

    assert reason is not None
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert entries
    assert entries[-1]["framework"] == "memp"
    assert entries[-1]["event"] == "path_check"
    assert entries[-1]["decision"] == "block"


def test_build_guarded_env_does_not_change_home_unless_requested(tmp_path: Path):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    base_env = {"HOME": "/original/home", "PATH": os.environ.get("PATH", "")}

    env = build_guarded_env(
        framework="evolution",
        workspace_dir=ws,
        run_home=home,
        base_env=base_env,
    )
    isolated = build_guarded_env(
        framework="memento",
        workspace_dir=ws,
        run_home=home,
        base_env=base_env,
        set_home=True,
    )

    assert env["HOME"] == "/original/home"
    assert isolated["HOME"] == str(home.resolve())
    assert env["TASK_WORKSPACE"] == str(ws.resolve())
    assert env["EVOLUTION_WORKSPACE_GUARD_FRAMEWORK"] == "evolution"


def test_command_wrappers_block_broad_host_search_but_allow_workspace(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "input.txt").write_text("ok", encoding="utf-8")
    log_path = tmp_path / "guard.jsonl"
    env = build_guarded_env(
        framework="memento",
        workspace_dir=ws,
        audit_log_path=log_path,
        base_env=os.environ.copy(),
        command_wrappers=True,
    )

    blocked = subprocess.run(
        ["find", str(Path.home()), "-name", "quarterly_report.pdf"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    allowed = subprocess.run(
        ["find", ".", "-maxdepth", "1", "-type", "f"],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert blocked.returncode == 126
    assert "BLOCKED:" in blocked.stderr
    assert allowed.returncode == 0
    assert "./input.txt" in allowed.stdout


def test_python_fs_guard_blocks_generated_runner_host_walk(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "input.txt").write_text("ok", encoding="utf-8")
    log_path = tmp_path / "guard.jsonl"
    env = build_guarded_env(
        framework="memento",
        workspace_dir=ws,
        audit_log_path=log_path,
        base_env=os.environ.copy(),
        command_wrappers=True,
    )

    code = (
        "import glob, os\n"
        "from pathlib import Path\n"
        f"print('HOST_WALK', list(os.walk({str(Path.home())!r})))\n"
        f"print('HOST_GLOB', glob.glob({str(Path.home() / '**' / '*.pdf')!r}, recursive=True))\n"
        f"print('HOST_RGLOB', list(Path({str(Path.home())!r}).rglob('*.pdf')))\n"
        "print('USR_OK', isinstance(list(os.walk('/usr/share/fonts'))[:1], list))\n"
        "print('WS_WALK', [(r, d, f) for r, d, f in os.walk('.')])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0
    assert "HOST_WALK []" in proc.stdout
    assert "HOST_GLOB []" in proc.stdout
    assert "HOST_RGLOB []" in proc.stdout
    assert "USR_OK True" in proc.stdout
    assert "input.txt" in proc.stdout
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "python_fs_check" for e in entries)


def test_python_fs_guard_allows_isolated_run_home_but_blocks_host_root(tmp_path: Path):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    skills = home / "memento_s" / "skills" / "pdf"
    ws.mkdir()
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("skill", encoding="utf-8")
    log_path = tmp_path / "guard.jsonl"
    env = build_guarded_env(
        framework="memento",
        workspace_dir=ws,
        run_home=home,
        audit_log_path=log_path,
        base_env=os.environ.copy(),
        command_wrappers=True,
    )

    code = (
        "import os\n"
        "from pathlib import Path\n"
        f"print('RUN_HOME_SKILLS', [p.name for p in Path({str(skills)!r}).rglob('SKILL.md')])\n"
        f"print('HOST_WALK', list(os.walk({str(Path.home())!r})))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0
    assert "RUN_HOME_SKILLS ['SKILL.md']" in proc.stdout
    assert "HOST_WALK []" in proc.stdout


@pytest.mark.external_repo
def test_memento_uv_sandbox_preserves_python_fs_guard(tmp_path: Path, monkeypatch):
    """Regression test for Memento's generated Python runner bypass.

    UvLocalSandbox.run() used to replace PYTHONPATH with the task work dir,
    which removed Evolution's sitecustomize guard before the runner process
    started. The Memento sandbox must prepend its work dir while preserving
    inherited guard paths.
    """

    memento_repo = Path(
        os.environ.get(
            "MEMENTO_REPO",
            str(Path.home() / "skill_evolutions" / "Memento-Skills"),
        )
    )
    if not (memento_repo / "middleware" / "sandbox" / "uv.py").is_file():
        pytest.skip(f"Memento-Skills repo is not available at {memento_repo}")
    if str(memento_repo) not in sys.path:
        sys.path.insert(0, str(memento_repo))

    from middleware.sandbox.uv import UvLocalSandbox  # type: ignore

    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    (ws / "input.txt").write_text("ok", encoding="utf-8")
    log_path = tmp_path / "guard.jsonl"
    guarded_env = build_guarded_env(
        framework="memento",
        workspace_dir=ws,
        run_home=home,
        audit_log_path=log_path,
        base_env=os.environ.copy(),
        set_home=True,
        command_wrappers=True,
        python_fs_guard_on_pythonpath=False,
    )
    for key, value in guarded_env.items():
        monkeypatch.setenv(key, value)

    sandbox = UvLocalSandbox.__new__(UvLocalSandbox)
    sandbox._venv_path = Path(sys.prefix)
    sandbox._python_executable = Path(sys.executable)

    outcome = sandbox.run(
        [
            sys.executable,
            "-c",
            (
                "import glob, os\n"
                f"print('HOST_WALK', list(os.walk({str(Path.home())!r})))\n"
                f"print('HOST_GLOB', glob.glob({str(Path.home() / '**' / '*.pdf')!r}, recursive=True))\n"
                "print('PYTHONPATH', os.environ.get('PYTHONPATH', ''))\n"
            ),
        ],
        cwd=ws,
        pythonpath=ws,
        timeout=10,
        skill_name="guard-regression",
    )

    assert outcome.success, outcome.error
    assert "HOST_WALK []" in (outcome.result or "")
    assert "HOST_GLOB []" in (outcome.result or "")
    assert str(ws) in (outcome.result or "")
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "python_fs_check" for e in entries)
