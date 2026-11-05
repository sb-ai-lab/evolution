"""End-to-end tests for the `evo split-check` and `evo split-list` CLI commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app

FIXTURE = Path(__file__).parent / "fixtures" / "manifests" / "tiny-v1.json"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_split_list_default_all(runner: CliRunner):
    result = runner.invoke(app, ["split-list", str(FIXTURE)])
    assert result.exit_code == 0, result.stdout
    ids = [line for line in result.stdout.strip().splitlines() if line]
    assert ids == ["task-test-D", "task-train-A", "task-train-B", "task-val-C"]


def test_split_list_skill_evolution_scope(runner: CliRunner):
    result = runner.invoke(app, ["split-list", str(FIXTURE), "-s", "skill_evolution"])
    assert result.exit_code == 0, result.stdout
    ids = [line for line in result.stdout.strip().splitlines() if line]
    assert ids == ["task-train-A", "task-train-B"]


def test_split_list_unknown_scope_fails(runner: CliRunner):
    result = runner.invoke(app, ["split-list", str(FIXTURE), "-s", "bogus"])
    assert result.exit_code == 1


def test_split_list_missing_file_fails(runner: CliRunner):
    result = runner.invoke(app, ["split-list", "/no/such/file.json"])
    assert result.exit_code == 1


def test_split_check_clean(runner: CliRunner):
    result = runner.invoke(
        app,
        [
            "split-check",
            str(FIXTURE),
            "--evolution-tasks",
            "task-train-A,task-train-B",
            "--reporting-tasks",
            "task-test-D",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "OK" in result.stdout


def test_split_check_detects_leakage(runner: CliRunner):
    result = runner.invoke(
        app,
        [
            "split-check",
            str(FIXTURE),
            "--evolution-tasks",
            "task-train-A,task-test-D",
        ],
    )
    assert result.exit_code == 1
    assert "LEAK" in result.stdout
    assert "task-test-D" in result.stdout


def test_split_check_no_lists_warns(runner: CliRunner):
    result = runner.invoke(app, ["split-check", str(FIXTURE)])
    assert result.exit_code == 0
    assert "No task lists supplied" in result.stdout
