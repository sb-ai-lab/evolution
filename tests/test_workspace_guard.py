from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evolution.eval.workspace_guard import (  # noqa: E402
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


def test_read_root_allows_broad_read_but_confines_write(tmp_path: Path):
    """read1edit mode: read across the whole cell dir, write only the skill dir."""
    cell = tmp_path / "cell"
    skill = cell / "skills" / "pdf"
    skill.mkdir(parents=True)
    other = cell / "traces" / "t.json"
    other.parent.mkdir(parents=True)
    other.write_text("{}", encoding="utf-8")
    guard = make_workspace_guard(
        framework="hermes",
        workspace_dir=skill,  # write target (narrow)
        read_root=cell,  # read scope (broad)
    )

    # writes stay confined to the one skill dir
    assert guard.path_block_reason(str(skill / "SKILL.md"), label="write") is None
    assert guard.path_block_reason(str(other), label="write") is not None

    # reads are allowed anywhere under the cell dir
    assert guard.read_block_reason(str(other), label="read") is None
    assert guard.read_block_reason(str(skill / "SKILL.md"), label="read") is None

    # reads outside the cell dir are still blocked
    assert guard.read_block_reason(str(Path.home() / "secret.txt"), label="read") is not None


def test_read_block_reason_falls_back_to_workspace_without_read_root(tmp_path: Path):
    """No read_root => reads confined to workspace_dir (back-compat)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    guard = make_workspace_guard(framework="hermes", workspace_dir=ws)

    assert guard.read_block_reason(str(ws / "a.txt"), label="read") is None
    assert guard.read_block_reason(str(tmp_path / "outside.txt"), label="read") is not None


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
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_build_guarded_env_honors_an_explicitly_empty_base_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    env = build_guarded_env(
        framework="test",
        workspace_dir=tmp_path / "workspace",
        base_env={},
    )

    assert "OPENAI_API_KEY" not in env


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
    pytest.importorskip("loguru", reason="Memento-Skills runtime dependency is unavailable")
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


def test_python_fs_guard_blocks_os_open_write_to_host(tmp_path: Path):
    """os.open (low-level fd) writes outside the workspace are blocked, but
    in-workspace writes, reads, and /dev/null all still work."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    log_path = tmp_path / "guard.jsonl"
    # tmp_path is under /tmp (an allowed MUTATE_ROOT); a real out-of-roots target
    # must live in the real $HOME. The guard should block it so it is never
    # created — unlink defensively in case of regression.
    host_target = Path.home() / f"__os_open_leak_probe_{os.getpid()}__.py"
    host_target.unlink(missing_ok=True)
    env = build_guarded_env(
        framework="evolution",
        workspace_dir=ws,
        audit_log_path=log_path,
        base_env=os.environ.copy(),
        command_wrappers=True,
    )

    code = (
        "import os\n"
        "def try_open(path, flags):\n"
        "    try:\n"
        "        fd = os.open(path, flags, 0o644)\n"
        "        os.close(fd)\n"
        "        return 'OK'\n"
        "    except PermissionError:\n"
        "        return 'BLOCKED'\n"
        f"print('WS', try_open('in_ws.py', os.O_WRONLY | os.O_CREAT))\n"
        f"print('HOST', try_open({str(host_target)!r}, os.O_WRONLY | os.O_CREAT))\n"
        "print('DEVNULL', try_open('/dev/null', os.O_WRONLY))\n"
        "print('READ', try_open('in_ws.py', os.O_RDONLY))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    assert "WS OK" in proc.stdout
    assert "HOST BLOCKED" in proc.stdout
    assert "DEVNULL OK" in proc.stdout
    assert "READ OK" in proc.stdout
    assert not host_target.exists()  # the write never happened
    assert (ws / "in_ws.py").exists()
    host_target.unlink(missing_ok=True)  # defensive: clean up on regression


def test_python_fs_guard_remaps_workspace_alias_for_generated_code(tmp_path: Path):
    """Generated MemP/Hermes-style runners often chdir/write to /workspace."""
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    log_path = tmp_path / "guard.jsonl"
    env = build_guarded_env(
        framework="memp",
        workspace_dir=ws,
        audit_log_path=log_path,
        base_env=os.environ.copy(),
        command_wrappers=True,
    )

    code = (
        "import os\n"
        "os.chdir('/workspace/output')\n"
        "open('relative.txt', 'w').write('ok')\n"
        "open('/workspace/output/absolute.txt', 'w').write('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    assert (ws / "output" / "relative.txt").read_text(encoding="utf-8") == "ok"
    assert (ws / "output" / "absolute.txt").read_text(encoding="utf-8") == "ok"
