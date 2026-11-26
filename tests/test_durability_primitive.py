from __future__ import annotations

import os

import pytest

from evolution.lineage import state_io


@pytest.mark.parametrize("value", [None, ""])
def test_default_is_relaxed(monkeypatch, value):
    monkeypatch.delenv("EVOLUTION_DURABILITY", raising=False)
    if value is not None:
        monkeypatch.setenv("EVOLUTION_DURABILITY", value)
    assert state_io.durability() == "relaxed"


@pytest.mark.parametrize("value", ["Strict", " strict", "strict ", "off", "full"])
def test_unknown_value_refused(monkeypatch, value):
    monkeypatch.setenv("EVOLUTION_DURABILITY", value)
    with pytest.raises(state_io.DurabilityConfigError) as exc:
        state_io.check_durability()
    assert str(exc.value) == f'EVOLUTION_DURABILITY must be "relaxed" or "strict" (got "{value}")'


def test_relaxed_makes_no_syscall(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOLUTION_DURABILITY", "relaxed")

    def forbidden(*args):
        pytest.fail("unexpected sync syscall")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", forbidden)
        patch.setattr(os, "open", forbidden)
        state_io.sync_file(-1)
        state_io.sync_directory(tmp_path)


def test_strict_calls_fsync(monkeypatch, tmp_path):
    monkeypatch.setenv("EVOLUTION_DURABILITY", "strict")
    calls = []
    monkeypatch.setattr(os, "fsync", calls.append)
    state_io.sync_file(123)
    state_io.sync_directory(tmp_path)
    assert calls[0] == 123
    assert len(calls) == 2


@pytest.mark.parametrize("mode, count", [("relaxed", 0), ("strict", 2)])
def test_atomic_write_syncs(monkeypatch, tmp_path, mode, count):
    monkeypatch.setenv("EVOLUTION_DURABILITY", mode)
    calls = []
    monkeypatch.setattr(os, "fsync", calls.append)
    target = tmp_path / "state"
    state_io.atomic_write_bytes(target, b"new state")
    assert target.read_bytes() == b"new state"
    assert len(calls) == count


def test_append_log_relaxed_no_fsync(monkeypatch, tmp_path):
    from tests.tree.storage.test_append_log import open_log

    monkeypatch.setenv("EVOLUTION_DURABILITY", "relaxed")

    def forbidden(fd):
        pytest.fail("AppendLog called fsync in relaxed mode")

    monkeypatch.setattr(os, "fsync", forbidden)
    log = open_log(tmp_path / "run", True)
    try:
        log.append("r", "opened", {}, None)
        log.append("r", "x_started", {}, "op")
        log.append("r", "x_finished", {}, "op")
        snapshot, _ = log.scan()
        assert snapshot.sequence == 3
    finally:
        log.close()


def test_publish_relaxed_no_fsync(monkeypatch, tmp_path):
    from evolution.tree.artifacts.artifact_copy import publish, stage_copy
    from evolution.tree.artifacts.artifact_policy import scan_tree
    from tests.tree.artifacts.test_artifact_copy import POLICY
    from tests.tree.artifacts.test_artifact_policy import seed

    monkeypatch.setenv("EVOLUTION_DURABILITY", "relaxed")

    def forbidden(fd):
        pytest.fail("publish called fsync in relaxed mode")

    monkeypatch.setattr(os, "fsync", forbidden)
    source = seed(tmp_path)
    expected = scan_tree(source, POLICY)
    staged = stage_copy(source, expected, POLICY, tmp_path / "staging", freeze=True)
    dest = tmp_path / "published"
    assert publish(staged, dest, expected, POLICY) == "published"
    assert scan_tree(dest, POLICY).candidate_digest == expected.candidate_digest


@pytest.mark.parametrize("value", ["Strict", "strict "])
def test_evo_refuses_unknown_durability(monkeypatch, value):
    from typer.testing import CliRunner

    from evolution.cli.main import app

    monkeypatch.setenv("EVOLUTION_DURABILITY", value)
    runner = CliRunner()
    result = runner.invoke(app, ["tree", "evaluate", "--help"])
    assert result.exit_code == 2
    assert f'EVOLUTION_DURABILITY must be "relaxed" or "strict" (got "{value}")' in result.stderr
    assert runner.invoke(app, ["--version"]).exit_code == 0


@pytest.mark.parametrize(
    "module,name",
    [
        ("evolve", "evolve"),
        ("evaluation", "evaluate"),
        ("campaign", "run_campaign"),
        ("campaign", "resume_campaign"),
    ],
)
@pytest.mark.parametrize("value", ["Strict", "strict "])
async def test_library_entry_refuses_before_write(monkeypatch, tmp_path, module, name, value):
    import importlib
    import inspect

    entry = getattr(importlib.import_module("evolution.tree." + module), name)
    kwargs = {
        key: None
        for key, param in inspect.signature(entry).parameters.items()
        if param.default is inspect.Parameter.empty
    }
    run = tmp_path / "run"
    kwargs["run_dir" if name in ("evolve", "evaluate") else "run_root"] = run
    monkeypatch.setenv("EVOLUTION_DURABILITY", value)
    with pytest.raises(state_io.DurabilityConfigError):
        await entry(**kwargs)
    assert not run.exists()
