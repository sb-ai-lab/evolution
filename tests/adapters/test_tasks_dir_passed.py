"""``--tasks-dir`` is plumbed into both ``evo reflect`` and ``evo rewrite``.

Task 1 set the evolve subprocess ``cwd=PROJECT_ROOT`` (an isolated mutable
root with no ``tasks/``). ``evo reflect``/``evo rewrite`` discover task
instruction files relative to ``Path.cwd()/tasks``, so they must be told the
real read-only tasks directory explicitly. That directory defaults to
``REPO_ROOT/"tasks"`` (the code checkout — task instructions are read-only
source assets, NOT the mutable PROJECT_ROOT) and is overridable via the
existing ``EVO_TASKS_ROOT`` env var.

The capture seam mirrors ``test_project_root_isolation.py``: monkeypatch
``_run_evo_cli_with_retries`` and assert on the *real* argv the adapter
builds.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REAL_REPO_ROOT = Path(__file__).resolve().parents[2]


def _reload_adapter_modules():
    import evolution.adapters.common as common

    common = importlib.reload(common)
    import evolution.adapters.evolution_adapter as evolution_adapter

    evolution_adapter = importlib.reload(evolution_adapter)
    return common, evolution_adapter


@pytest.fixture
def restore_adapter_modules():
    yield
    _reload_adapter_modules()


def _drive_one_round(adapter, monkeypatch, tmp_path, *, evolution_tasks_dir=None):
    captured_cmds: list[list[str]] = []

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run_evo(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(adapter, "_run_evo_cli_with_retries", _fake_run_evo)

    class _FakeProcRun:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_subprocess_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return _FakeProcRun()

    monkeypatch.setattr(adapter, "run_capture", _fake_subprocess_run)

    class _FakeStore:
        def __init__(self, *, project_dir):
            pass

        def has(self, _name):
            return True

    monkeypatch.setattr(adapter, "SkillStore", _FakeStore)

    out = adapter.evolve_skills_one_round(
        skill_to_failing_tasks={"docx": ["some-task"]},
        model_id="openrouter/anthropic/claude-sonnet-4",
        api_base="https://openrouter.ai/api/v1",
        api_key="k",
        manifest_path=tmp_path / "manifest.json",
        trace_model_id=None,
        evolution_tasks_dir=evolution_tasks_dir,
    )
    assert "docx" in out
    return captured_cmds


def _cmds_by_verb(captured_cmds):
    reflect = [c for c in captured_cmds if "reflect" in c]
    rewrite = [c for c in captured_cmds if "rewrite" in c]
    assert reflect, "expected at least one `evo reflect` argv"
    assert rewrite, "expected at least one `evo rewrite` argv"
    return reflect, rewrite


def _assert_has_tasks_dir(cmd, expected):
    assert "--tasks-dir" in cmd, f"--tasks-dir missing from argv: {cmd}"
    idx = cmd.index("--tasks-dir")
    assert cmd[idx + 1] == expected, (
        f"--tasks-dir value {cmd[idx + 1]!r} != expected {expected!r} in {cmd}"
    )


def test_tasks_dir_default_is_repo_root_tasks(tmp_path, monkeypatch, restore_adapter_modules):
    """Default --tasks-dir is REPO_ROOT/tasks (read-only code checkout)."""
    monkeypatch.delenv("EVO_TASKS_ROOT", raising=False)
    # Even with the mutable root isolated elsewhere, --tasks-dir must point
    # at the code checkout, NOT PROJECT_ROOT.
    monkeypatch.setenv("EVO_PROJECT_ROOT", str(tmp_path / "worker_root"))
    monkeypatch.delenv("EVO_SKILLS_ROOT", raising=False)

    common, adapter = _reload_adapter_modules()

    captured = _drive_one_round(adapter, monkeypatch, tmp_path)
    reflect, rewrite = _cmds_by_verb(captured)

    expected = str(common.REPO_ROOT / "tasks")
    assert expected != str(common.PROJECT_ROOT / "tasks")
    for cmd in reflect:
        _assert_has_tasks_dir(cmd, expected)
    for cmd in rewrite:
        _assert_has_tasks_dir(cmd, expected)


def test_tasks_dir_honors_evo_tasks_root_env(tmp_path, monkeypatch, restore_adapter_modules):
    """EVO_TASKS_ROOT overrides the default for both reflect and rewrite."""
    custom = tmp_path / "custom_tasks"
    custom.mkdir()
    monkeypatch.setenv("EVO_TASKS_ROOT", str(custom))

    _common, adapter = _reload_adapter_modules()

    captured = _drive_one_round(adapter, monkeypatch, tmp_path)
    reflect, rewrite = _cmds_by_verb(captured)

    for cmd in reflect:
        _assert_has_tasks_dir(cmd, str(custom))
    for cmd in rewrite:
        _assert_has_tasks_dir(cmd, str(custom))


def test_tasks_dir_explicit_arg_wins(tmp_path, monkeypatch, restore_adapter_modules):
    """Per-row task adapter dirs are passed through to reflect and rewrite."""
    custom = tmp_path / "env_tasks"
    explicit = tmp_path / "row_tasks"
    custom.mkdir()
    explicit.mkdir()
    monkeypatch.setenv("EVO_TASKS_ROOT", str(custom))

    _common, adapter = _reload_adapter_modules()

    captured = _drive_one_round(
        adapter,
        monkeypatch,
        tmp_path,
        evolution_tasks_dir=explicit,
    )
    reflect, rewrite = _cmds_by_verb(captured)

    for cmd in reflect:
        _assert_has_tasks_dir(cmd, str(explicit.resolve()))
    for cmd in rewrite:
        _assert_has_tasks_dir(cmd, str(explicit.resolve()))
