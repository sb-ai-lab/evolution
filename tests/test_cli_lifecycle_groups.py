"""CLI lifecycle groups keep the generic framework path discoverable."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.core.store import SkillStore


def test_root_help_shows_framework_lifecycle_groups() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for name in ("task", "skill", "run", "trace", "evolve", "split"):
        assert name in result.output
    assert "trace-driven skill lifecycle" in result.output


def test_grouped_commands_wrap_existing_lifecycle_commands() -> None:
    runner = CliRunner()

    checks = [
        ["task", "list", "--help"],
        ["skill", "commit", "--help"],
        ["run", "solve", "--help"],
        ["trace", "show", "--help"],
        ["evolve", "rewrite", "--help"],
        ["split", "check", "--help"],
    ]

    for args in checks:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
        assert "Usage" in result.output


RELEASE_BASE_REGISTRY = {
    "init",
    "doctor",
    "quickstart",
    "task",
    "skill",
    "run",
    "trace",
    "evolve",
    "split",
    "tree",
}


def test_public_tree_has_no_flat_aliases_and_nests_containers() -> None:
    root = get_command(app)

    assert set(root.commands) == RELEASE_BASE_REGISTRY
    assert "gui" not in root.commands
    assert "bench" not in root.commands
    for alias in ("solve", "create", "tasks", "traces", "reflect", "split-check"):
        assert alias not in root.commands

    skill = root.commands["skill"]
    container = skill.commands["container"]
    assert set(container.commands) == {"list", "show", "create", "delete", "snapshot", "snapshots"}
    assert "repair" in root.commands["trace"].commands
    assert set(root.commands["tree"].commands) == {
        "evolve",
        "conformance",
        "authorize",
        "evaluate",
        "trace",
        "watch",
    }


def test_removed_commands_get_the_generic_unknown_command_error() -> None:
    runner = CliRunner()

    for args in (["gui"], ["tree", "edit"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, f"{args}: {result.output}"
        assert "No such command" in result.output, f"{args}: {result.output}"
        for word in ("Streamlit", "dashboard", "paused", "attempt"):
            assert word not in result.output, f"{args}: names the removed purpose: {result.output}"


def test_llm_commands_use_environment_credentials_and_explicit_permissions() -> None:
    runner = CliRunner()

    for args in (
        ["run", "solve", "--help"],
        ["run", "bench", "run", "--help"],
        ["evolve", "reflect", "--help"],
        ["evolve", "rewrite", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
        assert "--api-key" not in result.output
        assert "--permission" in result.output
        assert "--max-tokens" in result.output

    solve_help = runner.invoke(app, ["run", "solve", "--help"])
    assert "--pass-env" in solve_help.output


def test_command_help_never_recommends_deleted_flat_aliases() -> None:
    runner = CliRunner()
    for args, deleted in (
        (["run", "solve", "--help"], "evo solve"),
        (["run", "bench", "run", "--help"], "evo bench"),
        (["trace", "list", "--help"], "evo traces"),
        (["trace", "show", "--help"], "evo show-trace"),
        (["skill", "commit", "--help"], "evo commit"),
        (["evolve", "rewrite", "--help"], "evo rewrite"),
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
        assert deleted not in result.output


def test_manual_mutation_reports_busy_skill_without_traceback(tmp_path: Path, monkeypatch) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="busy", description="Busy skill")
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)
    skill = store.get("busy")
    lock_path = skill.path.parent / ".busy.mutation.lock"
    holder = os.open(lock_path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = CliRunner().invoke(app, ["skill", "commit", "busy"])
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert result.exit_code == 2
    assert "is busy" in result.output
    assert "Traceback" not in result.output
