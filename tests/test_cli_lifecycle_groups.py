"""CLI lifecycle groups keep the generic framework path discoverable."""

from __future__ import annotations

from typer.testing import CliRunner

from evolution.cli.main import app


def test_root_help_shows_framework_lifecycle_groups() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for name in ("task", "skill", "run", "trace", "evolve", "paper"):
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
        ["paper", "split-check", "--help"],
    ]

    for args in checks:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args}: {result.output}"
        assert "Usage" in result.output

