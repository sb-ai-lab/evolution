"""Evolve timeouts resolve from env at call entry, not from default args.

The in-loop reflector/rewriter is Codex (gpt-5.5, reasoning xhigh); a single
reflect or rewrite call can take many minutes, far exceeding the historical
300s/600s defaults. The orchestrator exports ``EVO_REFLECT_TIMEOUT_S`` /
``EVO_REWRITE_TIMEOUT_S`` / ``EVO_COMMIT_TIMEOUT_S`` *after* import, so these
must be resolved inside the function body (None defaults), NOT baked into
default-arg expressions (which evaluate once at import).

The capture seam mirrors ``test_tasks_dir_passed.py`` /
``test_project_root_isolation.py``: monkeypatch ``_run_evo_cli_with_retries``
and ``subprocess.run`` and assert on the *real* timeout the adapter passes.
"""

from __future__ import annotations

import importlib

import pytest


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


def _drive_capture_timeouts(adapter, monkeypatch, tmp_path, **override_kwargs):
    """Run one evolve round, capturing the timeout passed to each seam.

    ``override_kwargs`` are forwarded verbatim into the real
    ``evolve_skills_one_round`` call, so a caller can pass explicit
    ``timeout_reflect_s=`` / ``timeout_rewrite_s=`` / ``timeout_commit_s=``
    to exercise the explicit-arg-wins-over-env precedence. With no
    overrides the behavior is identical to the prior fixed call.
    """
    captured: dict[str, list[float]] = {"reflect_rewrite": [], "commit": []}

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run_evo(cmd, **kwargs):
        captured["reflect_rewrite"].append(kwargs.get("timeout_s"))
        return _FakeProc()

    monkeypatch.setattr(adapter, "_run_evo_cli_with_retries", _fake_run_evo)

    class _FakeProcRun:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_subprocess_run(cmd, **kwargs):
        captured["commit"].append(kwargs.get("timeout"))
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
        **override_kwargs,
    )
    assert "docx" in out
    return captured


def test_timeouts_resolve_from_env(tmp_path, monkeypatch, restore_adapter_modules):
    """Env vars (exported AFTER import) drive reflect/rewrite/commit timeouts."""
    monkeypatch.setenv("EVO_REFLECT_TIMEOUT_S", "14400")
    monkeypatch.setenv("EVO_REWRITE_TIMEOUT_S", "5400")
    monkeypatch.setenv("EVO_COMMIT_TIMEOUT_S", "120")

    _common, adapter = _reload_adapter_modules()

    captured = _drive_capture_timeouts(adapter, monkeypatch, tmp_path)

    # reflect happens first (per failing task), then rewrite. Both flow
    # through _run_evo_cli_with_retries with timeout_s=.
    assert captured["reflect_rewrite"], "expected reflect/rewrite seam calls"
    reflect_timeout = captured["reflect_rewrite"][0]
    rewrite_timeout = captured["reflect_rewrite"][-1]
    assert reflect_timeout == 14400.0, (
        f"reflect timeout {reflect_timeout!r} != env 14400 "
        "(env-in-default-args would yield stale 300)"
    )
    assert rewrite_timeout == 5400.0, (
        f"rewrite timeout {rewrite_timeout!r} != env 5400 "
        "(env-in-default-args would yield stale 600)"
    )
    assert captured["commit"], "expected commit subprocess.run call"
    assert captured["commit"][0] == 120.0, f"commit timeout {captured['commit'][0]!r} != env 120"


def test_timeouts_default_when_no_env(tmp_path, monkeypatch, restore_adapter_modules):
    """Behavior unchanged (300/600/60) when neither explicit arg nor env set."""
    monkeypatch.delenv("EVO_REFLECT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("EVO_REWRITE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("EVO_COMMIT_TIMEOUT_S", raising=False)

    _common, adapter = _reload_adapter_modules()

    captured = _drive_capture_timeouts(adapter, monkeypatch, tmp_path)

    assert captured["reflect_rewrite"][0] == 300.0
    assert captured["reflect_rewrite"][-1] == 600.0
    assert captured["commit"][0] == 60.0


def test_explicit_arg_overrides_env(tmp_path, monkeypatch, restore_adapter_modules):
    """An explicit non-None caller arg wins over env."""
    monkeypatch.setenv("EVO_REFLECT_TIMEOUT_S", "14400")
    monkeypatch.setenv("EVO_REWRITE_TIMEOUT_S", "5400")
    monkeypatch.setenv("EVO_COMMIT_TIMEOUT_S", "120")

    _common, adapter = _reload_adapter_modules()

    captured = _drive_capture_timeouts(
        adapter,
        monkeypatch,
        tmp_path,
        timeout_reflect_s=999.0,
        timeout_rewrite_s=888.0,
        timeout_commit_s=77.0,
    )

    assert captured["reflect_rewrite"][0] == 999.0
    assert captured["reflect_rewrite"][-1] == 888.0
    assert captured["commit"][0] == 77.0
