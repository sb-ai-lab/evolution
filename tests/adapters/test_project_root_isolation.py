"""Isolated mutable project root (PROJECT_ROOT) vs code checkout (REPO_ROOT).

Concurrent anchor workers each mutate ``.agents/skills/<skill>/``. A shared
root races, so the mutable skill state must be relocatable via
``EVO_PROJECT_ROOT`` / ``EVO_SKILLS_ROOT`` while ``REPO_ROOT`` stays the code
checkout (imports, ``REPO_ROOT/"experiments"``, task/asset reads).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# The real code checkout root (this test file lives at
# <repo>/tests/adapters/test_project_root_isolation.py).
REAL_REPO_ROOT = Path(__file__).resolve().parents[2]


def _reload_adapter_modules():
    """Reload common + evolution_adapter so module-level constants re-read env.

    ``evolution_adapter`` binds ``REPO_ROOT``/``SKILLS_ROOT`` by value at
    import time, so it must be reloaded *after* ``common``.
    """
    import evolution.adapters.common as common

    common = importlib.reload(common)
    import evolution.adapters.evolution_adapter as evolution_adapter

    evolution_adapter = importlib.reload(evolution_adapter)
    return common, evolution_adapter


@pytest.fixture
def restore_adapter_modules():
    """Ensure the modules are reloaded back to default-env state after a test."""
    yield
    # monkeypatch already restores env; reload resets module-level
    # REPO_ROOT/PROJECT_ROOT/SKILLS_ROOT constants to default state.
    _reload_adapter_modules()


def test_project_root_env_override_isolates_mutable_state(
    tmp_path, monkeypatch, restore_adapter_modules
):
    proj = tmp_path / "worker_root"
    monkeypatch.setenv("EVO_PROJECT_ROOT", str(proj))
    monkeypatch.delenv("EVO_SKILLS_ROOT", raising=False)

    common, _adapter = _reload_adapter_modules()

    # Mutable roots move under the tmp project root.
    assert common.PROJECT_ROOT == proj.resolve()
    assert common.SKILLS_ROOT == (proj / ".agents" / "skills").resolve()
    # The code checkout root is unchanged — imports/assets depend on it.
    assert common.REPO_ROOT == REAL_REPO_ROOT
    assert common.PROJECT_ROOT != common.REPO_ROOT
    # Read-only asset roots stay anchored at the code checkout.
    assert common.TASKS_ROOT == common.REPO_ROOT / "tasks"


def test_skills_root_explicit_env_override(tmp_path, monkeypatch, restore_adapter_modules):
    proj = tmp_path / "worker_root"
    skills = tmp_path / "custom_skills"
    monkeypatch.setenv("EVO_PROJECT_ROOT", str(proj))
    monkeypatch.setenv("EVO_SKILLS_ROOT", str(skills))

    common, _adapter = _reload_adapter_modules()

    assert common.PROJECT_ROOT == proj.resolve()
    assert common.SKILLS_ROOT == skills.resolve()
    assert common.REPO_ROOT == REAL_REPO_ROOT


def test_default_project_root_equals_repo_root(monkeypatch, restore_adapter_modules):
    monkeypatch.delenv("EVO_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("EVO_SKILLS_ROOT", raising=False)

    common, _adapter = _reload_adapter_modules()

    assert common.PROJECT_ROOT == common.REPO_ROOT
    assert common.SKILLS_ROOT == common.REPO_ROOT / ".agents" / "skills"


def test_evolve_subprocess_cwd_and_store_use_project_root(
    tmp_path, monkeypatch, restore_adapter_modules
):
    """``evolve_skills_one_round`` and ``_store_has`` target PROJECT_ROOT."""
    proj = tmp_path / "worker_root"
    monkeypatch.setenv("EVO_PROJECT_ROOT", str(proj))
    monkeypatch.delenv("EVO_SKILLS_ROOT", raising=False)

    common, adapter = _reload_adapter_modules()

    captured_cwds: list[str] = []

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    captured_subprocess_cwds: list[str] = []

    def _fake_subprocess_run(cmd, **kwargs):
        captured_subprocess_cwds.append(kwargs.get("cwd"))
        return _FakeProc()

    monkeypatch.setattr(adapter, "run_capture", _fake_subprocess_run)

    # Capture SkillStore(project_dir=...) calls made by _store_has.
    captured_project_dirs: list[Path] = []

    class _FakeStore:
        def __init__(self, *, project_dir):
            captured_project_dirs.append(Path(project_dir))

        def has(self, _name):
            return True

    monkeypatch.setattr(adapter, "SkillStore", _FakeStore)

    # Drive a single evolve round end-to-end (reflect/rewrite/commit stubbed).
    out = adapter.evolve_skills_one_round(
        skill_to_failing_tasks={"docx": ["some-task"]},
        model_id="openrouter/anthropic/claude-sonnet-4",
        api_base="https://openrouter.ai/api/v1",
        api_key="k",
        manifest_path=tmp_path / "manifest.json",
        trace_model_id=None,
    )
    assert "docx" in out

    # Every subprocess.run cwd must be the isolated PROJECT_ROOT, not the
    # shared code checkout.
    assert captured_subprocess_cwds, "expected at least one subprocess.run call"
    for cwd in captured_subprocess_cwds:
        assert cwd == str(common.PROJECT_ROOT)
        assert cwd != str(common.REPO_ROOT)

    # _store_has must resolve the SkillStore at PROJECT_ROOT.
    out2 = adapter._store_has("docx")
    assert out2 is True
    assert captured_project_dirs, "expected SkillStore(project_dir=...) call"
    for pd in captured_project_dirs:
        assert pd == common.PROJECT_ROOT
        assert pd != common.REPO_ROOT


def test_partial_isolation_shared_checkout_unmutated(
    tmp_path, monkeypatch, restore_adapter_modules
):
    """A stubbed produce/seed path must not touch the shared checkout.

    This catches any REPO_ROOT mutable-state read missed during the sweep:
    with EVO_PROJECT_ROOT set, all skill-state paths the adapter helpers
    compute resolve under the isolated tmp project root and never under the
    shared ``REPO_ROOT/.agents/skills`` (verified via path-prefix assertions,
    not hashing).
    """
    proj = tmp_path / "worker_root"
    monkeypatch.setenv("EVO_PROJECT_ROOT", str(proj))
    monkeypatch.delenv("EVO_SKILLS_ROOT", raising=False)

    common, adapter = _reload_adapter_modules()

    # Helper functions that compute mutable skill-state paths must all be
    # rooted at SKILLS_ROOT (== PROJECT_ROOT/.agents/skills), never the
    # shared code checkout.
    shared_skills = common.REPO_ROOT / ".agents" / "skills"

    # _current_skill_version reads <SKILLS_ROOT>/<skill>/.evolution/lineage.json
    # Seed a fake lineage under the isolated root and assert the function
    # reads it (proving it does NOT look under the shared checkout).
    isolated_lineage = common.SKILLS_ROOT / "docx" / ".evolution" / "lineage.json"
    isolated_lineage.parent.mkdir(parents=True, exist_ok=True)
    isolated_lineage.write_text('{"current_version": "v9"}', encoding="utf-8")

    assert str(common.SKILLS_ROOT).startswith(str(proj))
    assert not str(common.SKILLS_ROOT).startswith(str(shared_skills))
    assert adapter._current_skill_version("docx") == "v9"

    # _archive_stale_reflections globs <SKILLS_ROOT>/<skill>/.traces/*.
    # Create a reflection under the isolated root; archiving it must not
    # require (or touch) anything under the shared checkout.
    refl = common.SKILLS_ROOT / "docx" / ".traces" / "t1" / "reflection.json"
    refl.parent.mkdir(parents=True, exist_ok=True)
    refl.write_text("{}", encoding="utf-8")
    archived = adapter._archive_stale_reflections("docx")
    assert archived, "expected the isolated reflection to be archived"
    for p in archived:
        assert str(p).startswith(str(proj))
        assert not str(p).startswith(str(shared_skills))


def test_hermes_terminal_env_isolates_home(tmp_path):
    """$HOME must point at the ephemeral run home so agent/uv dotfile writes
    can't land in the real ~/.zshrc (regression: a leaked `. "<tmp>/bin/env"`
    line once broke the user's shell on every new terminal)."""
    import os

    from evolution.adapters.workspace_guard import configure_hermes_terminal_env

    saved = dict(os.environ)
    real_home = os.environ.get("HOME")
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        run_home = tmp_path / "h"
        configure_hermes_terminal_env(workspace_dir=ws, run_home=run_home, terminal_timeout_s=5)
        assert os.environ["HOME"] == str(run_home / "home")
        assert os.environ["HOME"] != real_home
    finally:
        os.environ.clear()
        os.environ.update(saved)
