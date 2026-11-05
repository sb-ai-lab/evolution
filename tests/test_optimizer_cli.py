"""CLI coverage for the production optimizer surface."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.evolve.optimizer_state import OptimizationResult


def test_optimizer_help_exposes_the_supported_surface() -> None:
    result = CliRunner().invoke(app, ["evolve", "optimize", "--help"])

    assert result.exit_code == 0
    for option in (
        "--train",
        "--validate",
        "--solver-model",
        "--editor-model",
        "--solver-api-base",
        "--editor-api-base",
        "--solver-temperature",
        "--editor-temperature",
        "--solver-reasoning-effort",
        "--editor-reasoning-effort",
        "--resume",
        "--dry-run",
        "--pass-env",
        "--json",
    ):
        assert option in result.stdout


def test_optimizer_json_wraps_the_result(monkeypatch) -> None:
    result_value = OptimizationResult(
        run_id="dry-run",
        status="dry_run",
        skill_name="demo",
        initial_version="v1",
        final_version="v1",
        rounds=[],
        run_path=None,
        plan={"train_tasks": ["train-a"]},
    )
    call = {}

    def optimize(*args, **kwargs):
        call.update(skill_name=args[0], **kwargs)
        return result_value

    monkeypatch.setattr("evolution.cli.commands.evolve.optimize_skill", optimize)

    result = CliRunner().invoke(
        app,
        [
            "evolve",
            "optimize",
            "demo",
            "--train",
            "train-a",
            "--validate",
            "val-a",
            "--solver-model",
            "mock",
            "--api-base",
            "https://shared.example/v1",
            "--solver-api-base",
            "https://solver.example/v1",
            "--editor-api-base",
            "https://editor.example/v1",
            "--solver-temperature",
            "0",
            "--editor-temperature",
            "1",
            "--solver-reasoning-effort",
            "off",
            "--editor-reasoning-effort",
            "medium",
            "--dry-run",
            "--pass-env",
            "VISIBLE_TOKEN",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "ok": True,
        "data": result_value.to_dict(),
    }
    assert call["skill_name"] == "demo"
    assert call["train_tasks"] == ["train-a"]
    assert call["validate_tasks"] == ["val-a"]
    assert call["solver_model"] == "mock"
    assert call["api_base"] == "https://shared.example/v1"
    assert call["solver_api_base"] == "https://solver.example/v1"
    assert call["editor_api_base"] == "https://editor.example/v1"
    assert call["solver_temperature"] == 0.0
    assert call["editor_temperature"] == 1.0
    assert call["solver_reasoning_effort"] == "off"
    assert call["editor_reasoning_effort"] == "medium"
    assert call["pass_env"] == ["VISIBLE_TOKEN"]


def test_optimizer_missing_inputs_use_validation_exit_code() -> None:
    result = CliRunner().invoke(app, ["evolve", "optimize", "demo", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["kind"] == "invalid_arguments"


def test_optimizer_runtime_validation_returns_valid_json() -> None:
    result = CliRunner().invoke(
        app,
        [
            "evolve",
            "optimize",
            "demo",
            "--train",
            "train-a",
            "--validate",
            "val-a",
            "--solver-model",
            "mock",
            "--solver-temperature",
            "nan",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["kind"] == "invalid_arguments"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("solver_model", ["codex/gpt-5.5", "gigachat/GigaChat-3-Ultra"])
def test_optimizer_cli_rejects_unsupported_solver_without_state(
    tmp_path, monkeypatch, dry_run: bool, solver_model: str
) -> None:
    monkeypatch.chdir(tmp_path)
    args = [
        "evolve",
        "optimize",
        "demo",
        "--train",
        "train-a",
        "--validate",
        "val-a",
        "--solver-model",
        solver_model,
        "--json",
    ]
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == {
        "kind": "invalid_arguments",
        "message": f"Solver model {solver_model!r} does not support seeded completions",
    }
    assert not (tmp_path / ".agents").exists()


def test_optimizer_human_output_explains_inconclusive_result(monkeypatch) -> None:
    result_value = OptimizationResult(
        run_id="run-1",
        status="inconclusive",
        skill_name="demo",
        initial_version="v1",
        final_version="v1",
        rounds=[],
        run_path=None,
        reason="underpowered",
        recommendation="Increase --trials and rerun.",
    )
    monkeypatch.setattr(
        "evolution.cli.commands.evolve.optimize_skill", lambda *_args, **_kwargs: result_value
    )

    result = CliRunner().invoke(
        app,
        [
            "evolve",
            "optimize",
            "demo",
            "--train",
            "train-a",
            "--validate",
            "val-a",
            "--solver-model",
            "mock",
        ],
    )

    assert result.exit_code == 0
    assert "reason=underpowered" in result.stdout
    assert "Increase --trials" in result.stdout


def test_optimizer_state_failure_uses_infrastructure_exit_code(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise ValueError("Invalid optimizer run state")

    monkeypatch.setattr("evolution.cli.commands.evolve.optimize_skill", fail)
    result = CliRunner().invoke(
        app,
        [
            "evolve",
            "optimize",
            "demo",
            "--train",
            "train-a",
            "--validate",
            "val-a",
            "--solver-model",
            "mock",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["kind"] == "command_failed"


def test_optimizer_unexpected_failure_does_not_echo_provider_secret(monkeypatch) -> None:
    secret = "sk-secret-123"

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"Authorization failed: {secret}")

    monkeypatch.setattr("evolution.cli.commands.evolve.optimize_skill", fail)
    result = CliRunner().invoke(
        app,
        [
            "evolve",
            "optimize",
            "demo",
            "--train",
            "train-a",
            "--validate",
            "val-a",
            "--solver-model",
            "mock",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert secret not in result.output
    assert json.loads(result.stdout)["error"] == {
        "kind": "command_failed",
        "message": "Optimizer command failed.",
    }


def test_optimizer_status_json_returns_the_checkpoint(monkeypatch) -> None:
    state = {
        "run_id": "run-1",
        "status": "completed",
        "phase": "completed",
        "initial_version": "v1",
        "final_version": "v2",
    }
    monkeypatch.setattr(
        "evolution.cli.commands.evolve.load_optimization_status",
        lambda skill_name, run_id: state,
    )

    result = CliRunner().invoke(app, ["evolve", "status", "demo", "--run", "run-1", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"] == state
