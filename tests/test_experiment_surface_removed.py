"""Public contracts for the removed isolated-experiment feature."""

from __future__ import annotations

import importlib.util
import inspect

from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.workflow import (
    commit_skill,
    list_traces,
    load_trace,
    promote_skill,
    reflect,
    reject_skill,
    rewrite,
    skill_store,
    solve,
)


def test_cli_help_has_no_isolated_experiment_commands_or_options() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])

    assert root.exit_code == 0, root.output
    for command in ("experiments", "create-experiment", "delete-experiment"):
        assert command not in root.output

    command_paths = [
        ["skill", "show"],
        ["skill", "lineage"],
        ["skill", "diff"],
        ["skill", "commit"],
        ["run", "solve"],
        ["evolve", "reflect"],
        ["evolve", "rewrite"],
    ]
    for path in command_paths:
        result = runner.invoke(app, [*path, "--help"])
        assert result.exit_code == 0, f"{path}: {result.output}"
        assert "--experiment" not in result.output, path


def test_public_workflow_signatures_have_no_experiment_keyword() -> None:
    public_functions = (
        skill_store,
        solve,
        list_traces,
        load_trace,
        reflect,
        rewrite,
        commit_skill,
        promote_skill,
        reject_skill,
    )

    for function in public_functions:
        assert "experiment" not in inspect.signature(function).parameters, function.__name__


def test_removed_core_module_is_absent() -> None:
    assert importlib.util.find_spec("evolution.core.experiments") is None
