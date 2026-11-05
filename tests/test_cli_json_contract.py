"""Machine-readable CLI commands share one success/error envelope."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.core.models import SkillTrace
from evolution.core.store import SkillStore
from evolution.lineage.traces import TraceStore
from evolution.lineage.version import VersionStore


def _payload(result) -> dict:
    return json.loads(result.stdout)


def _assert_error(result, kind: str, message: str) -> None:
    assert result.exit_code != 0
    assert _payload(result) == {
        "schema_version": 1,
        "ok": False,
        "error": {"kind": kind, "message": message},
    }
    assert message in result.stderr


def test_skill_list_json_wraps_legacy_payload(monkeypatch) -> None:
    entry = SimpleNamespace(
        name="demo",
        fitness=0.5,
        generation=2,
        status="active",
        description="Demo skill",
    )
    store = SimpleNamespace(list=lambda status=None: [entry])
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)

    result = CliRunner().invoke(app, ["skill", "list", "--json"])

    assert result.exit_code == 0
    assert _payload(result) == {
        "schema_version": 1,
        "ok": True,
        "data": [
            {
                "name": "demo",
                "fitness": 0.5,
                "generation": 2,
                "status": "active",
                "description": "Demo skill",
            }
        ],
    }


def test_skill_show_json_wraps_legacy_payload(monkeypatch, tmp_path: Path) -> None:
    skill = SimpleNamespace(
        name="demo",
        description="Demo skill",
        frontmatter=SimpleNamespace(license=None, compatibility=None),
        fitness=None,
        generation=None,
        scripts=[],
        references=[],
        path=tmp_path / "demo",
        body="Do the thing.",
    )
    store = SimpleNamespace(get=lambda name: skill)
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)

    result = CliRunner().invoke(app, ["skill", "show", "demo", "--json"])

    assert result.exit_code == 0
    assert _payload(result) == {
        "schema_version": 1,
        "ok": True,
        "data": {
            "name": "demo",
            "description": "Demo skill",
            "license": None,
            "compatibility": None,
            "fitness": None,
            "generation": None,
            "scripts": [],
            "references": [],
            "path": str(tmp_path / "demo"),
            "body": "Do the thing.",
        },
    }


def test_skill_show_json_reports_missing_version(monkeypatch, tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="demo", description="Demo skill")
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)

    result = CliRunner().invoke(app, ["skill", "show", "demo", "--version", "v99", "--json"])

    _assert_error(result, "version_not_found", "Version 'v99' not found.")


def test_skill_show_version_json_uses_version_store(monkeypatch, tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="demo", description="Demo skill")
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)
    expected = VersionStore(store.get("demo")).load("v1").skill_md_path.read_text(encoding="utf-8")

    result = CliRunner().invoke(app, ["skill", "show", "demo", "--version", "v1", "--json"])

    assert result.exit_code == 0
    assert _payload(result) == {
        "schema_version": 1,
        "ok": True,
        "data": {"name": "demo", "version": "v1", "content": expected},
    }


def test_skill_show_version_rejects_traversal_with_exact_json_error(
    monkeypatch, tmp_path: Path
) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="demo", description="Demo skill")
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)

    result = CliRunner().invoke(app, ["skill", "show", "demo", "--version", "../outside", "--json"])

    _assert_error(result, "invalid_arguments", "Invalid version label: '../outside'")


def test_skill_show_version_human_errors_exit_one(monkeypatch, tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="demo", description="Demo skill")
    monkeypatch.setattr("evolution.cli.commands.skills._get_store", lambda: store)
    runner = CliRunner()

    missing_version = runner.invoke(app, ["skill", "show", "demo", "--version", "v99"])
    missing_skill = runner.invoke(app, ["skill", "show", "missing", "--version", "v1"])

    assert missing_version.exit_code == 1
    assert "Version 'v99' not found." in missing_version.stdout
    assert missing_skill.exit_code == 1
    assert "Skill 'missing' not found." in missing_skill.stdout


def test_trace_list_json_wraps_legacy_payload(monkeypatch) -> None:
    traces = [{"id": "trace-1", "success": True}]
    monkeypatch.setattr(
        "evolution.cli.commands.traces._get_store",
        lambda: SimpleNamespace(get=lambda name: object()),
    )
    monkeypatch.setattr(
        "evolution.lineage.traces.TraceStore",
        lambda skill: SimpleNamespace(list=lambda **filters: traces),
    )

    result = CliRunner().invoke(app, ["trace", "list", "demo", "--json"])

    assert result.exit_code == 0
    assert _payload(result) == {"schema_version": 1, "ok": True, "data": traces}


def test_trace_list_json_reports_missing_skill(monkeypatch) -> None:
    def missing(name: str):
        raise KeyError(name)

    monkeypatch.setattr(
        "evolution.cli.commands.traces._get_store",
        lambda: SimpleNamespace(get=missing),
    )

    result = CliRunner().invoke(app, ["trace", "list", "missing", "--json"])

    _assert_error(result, "skill_not_found", "Skill 'missing' not found.")


def test_trace_show_rejects_traversal(monkeypatch, tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="demo", description="Demo skill")
    skill = store.get("demo")
    TraceStore(skill).save(
        SkillTrace(
            id="safe-trace",
            skill_name="demo",
            skill_version="v1",
            model="mock",
            task="task",
            timestamp=datetime.now(),
        ),
        "prompt",
        "response",
        "code",
    )
    monkeypatch.setattr("evolution.cli.commands.traces._get_store", lambda: store)

    result = CliRunner().invoke(app, ["trace", "show", "demo", "../../outside"])

    assert result.exit_code == 1
    assert "Invalid trace id" in result.stdout


def _patch_solve(
    monkeypatch, tmp_path: Path, result=None, error: Exception | None = None
) -> list[dict]:
    calls: list[dict] = []
    task = SimpleNamespace(name="demo")
    monkeypatch.setattr("evolution.eval.task.discover_tasks", lambda path: [task])
    monkeypatch.setattr("evolution.eval.workspace.setup_workspace", lambda *args: tmp_path)
    monkeypatch.setattr("evolution.llm.backend.get_backend", lambda *args, **kwargs: object())

    def solve(*args, **kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return result

    monkeypatch.setattr("evolution.eval.solver.solve_task", solve)
    return calls


def test_run_solve_json_wraps_legacy_payload(monkeypatch, tmp_path: Path) -> None:
    solve_result = SimpleNamespace(
        success=True,
        script_exit_code=0,
        exec_ms=1,
        eval_ms=2,
        completion_ms=3,
        llm_response=None,
        eval_result=None,
        errors=(),
        script_path=None,
        script_output="",
    )
    _patch_solve(monkeypatch, tmp_path, solve_result)

    result = CliRunner().invoke(
        app,
        ["run", "solve", "demo", "--model", "mock", "--no-skill", "--json"],
    )

    assert result.exit_code == 0
    assert _payload(result) == {
        "schema_version": 1,
        "ok": True,
        "data": {
            "task": "demo",
            "model": "mock",
            "success": True,
            "script_exit_code": 0,
            "exec_ms": 1,
            "eval_ms": 2,
            "completion_ms": 3,
            "tokens_in": None,
            "tokens_out": None,
            "passed": None,
            "total_tests": None,
            "failed": None,
            "errors": [],
        },
    }
    assert "Model: mock" in result.stderr


def test_run_solve_forwards_attempt_seed(monkeypatch, tmp_path: Path) -> None:
    calls = _patch_solve(
        monkeypatch,
        tmp_path,
        SimpleNamespace(
            success=True,
            llm_response=None,
            script_exit_code=0,
            script_path=None,
            eval_result=None,
            errors=(),
        ),
    )

    result = CliRunner().invoke(
        app,
        ["run", "solve", "demo", "--model", "mock", "--no-skill", "--seed", "37"],
    )

    assert result.exit_code == 0
    assert calls[0]["attempt_seed"] == 37


def test_run_solve_rejects_negative_seed(monkeypatch, tmp_path: Path) -> None:
    calls = _patch_solve(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "solve",
            "demo",
            "--model",
            "mock",
            "--no-skill",
            "--seed",
            "-1",
            "--json",
        ],
    )

    _assert_error(result, "invalid_arguments", "--seed must be non-negative.")
    assert result.exit_code == 2
    assert not calls


@pytest.mark.parametrize("model", ["codex/gpt-5.5", "gigachat/GigaChat-3-Ultra"])
def test_run_solve_rejects_seeded_unsupported_model(
    monkeypatch, tmp_path: Path, model: str
) -> None:
    calls = _patch_solve(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        app,
        ["run", "solve", "demo", "--model", model, "--no-skill", "--seed", "37", "--json"],
    )

    _assert_error(
        result, "invalid_arguments", f"Model {model!r} does not support seeded completions."
    )
    assert result.exit_code == 2
    assert not calls


def test_run_solve_json_reports_failed_result(monkeypatch, tmp_path: Path) -> None:
    solve_result = SimpleNamespace(
        success=False,
        script_exit_code=1,
        exec_ms=1,
        eval_ms=2,
        completion_ms=3,
        llm_response=None,
        eval_result=None,
        errors=("bad output",),
        script_path=None,
        script_output="",
    )
    _patch_solve(monkeypatch, tmp_path, solve_result)

    result = CliRunner().invoke(
        app,
        ["run", "solve", "demo", "--model", "mock", "--no-skill", "--json"],
    )

    _assert_error(result, "solve_failed", "bad output")


def test_run_solve_json_reports_unexpected_failure(monkeypatch, tmp_path: Path) -> None:
    _patch_solve(monkeypatch, tmp_path, error=RuntimeError("backend broke"))

    result = CliRunner().invoke(
        app,
        ["run", "solve", "demo", "--model", "mock", "--no-skill", "--json"],
    )

    _assert_error(result, "command_failed", "backend broke")


def test_run_solve_json_reports_missing_task(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("evolution.eval.task.discover_tasks", lambda path: [])

    result = CliRunner().invoke(
        app,
        ["run", "solve", "missing", "--model", "mock", "--dir", str(tmp_path), "--json"],
    )

    _assert_error(result, "task_not_found", "Task 'missing' not found.")


def test_run_solve_json_reports_invalid_manifest_arguments(monkeypatch, tmp_path: Path) -> None:
    _patch_solve(monkeypatch, tmp_path)
    message = (
        "--manifest-scope is required when --manifest is set "
        "(one of skill_evolution, dev, reporting, all)."
    )

    result = CliRunner().invoke(
        app,
        [
            "run",
            "solve",
            "demo",
            "--model",
            "mock",
            "--no-skill",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--json",
        ],
    )

    _assert_error(result, "invalid_arguments", message)
    assert result.exit_code == 2


def test_doctor_json_wraps_legacy_payload(monkeypatch, tmp_path: Path) -> None:
    class Store:
        def __init__(self, **kwargs):
            pass

        def list(self):
            return [object()]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("evolution.core.store.SkillStore", Store)
    monkeypatch.setattr("evolution.workflow.list_tasks", lambda *args, **kwargs: [object()])

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["data"]["ok"] is True
    assert payload["data"]["checks"]


def test_doctor_json_reports_failed_checks(monkeypatch, tmp_path: Path) -> None:
    class Store:
        def __init__(self, **kwargs):
            pass

        def list(self):
            return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("evolution.core.store.SkillStore", Store)
    monkeypatch.setattr("evolution.workflow.list_tasks", lambda *args, **kwargs: [])

    result = CliRunner().invoke(app, ["doctor", "--json"])

    _assert_error(result, "doctor_checks_failed", "2 doctor check(s) failed.")
