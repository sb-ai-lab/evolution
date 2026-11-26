"""The installed Core help explains its place without renaming commands."""

from typer.testing import CliRunner

from evolution.cli.main import app


def test_evo_help_names_core_and_points_to_lab() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Evolution Core" in text
    assert "lab" in text and "primary" in text
    assert "campaign engine" in text
    for group in ["task", "skill", "run", "trace", "evolve", "split"]:
        result = runner.invoke(app, [group, "--help"])
        assert result.exit_code == 0, result.output
        assert "manual task and skill workflow" in " ".join(result.output.split()).lower()
