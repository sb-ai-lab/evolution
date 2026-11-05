from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.core.bindings import SkillBinding
from evolution.core.models import SkillTrace
from evolution.core.store import SkillStore
from evolution.evolve import optimizer
from evolution.evolve.optimizer import optimize_skill
from evolution.evolve.optimizer_state import json_digest, read_state, write_state
from evolution.evolve.optimizer_validation import ValidationMeasurement
from evolution.evolve.rewrite_outcome import NO_OP_TEXT, PROMOTED_ELIGIBLE, RewriteOutcome
from evolution.lineage.traces import TraceStore
from evolution.lineage.tracker import LineageTracker
from evolution.lineage.version import VersionStore
from evolution.llm.mock_backend import MockBackend

pytestmark = pytest.mark.slow


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    tasks = tmp_path / "tasks"
    SkillStore(project_dir=project).create("target-id", "Optimizer target")
    for task_id, required in (
        ("train", ["target-id"]),
        ("validate", ["target-id"]),
    ):
        task = tasks / task_id
        (task / "tests").mkdir(parents=True)
        metadata = (
            'version = "1.0"\n'
            "[metadata]\n"
            f'name = "{task_id}"\n'
            'difficulty = "easy"\n'
            'category = "test"\n'
            "[skills]\n"
            + f"required = {required!r}\n".replace("'", '"')
            + "[evaluation]\n"
            + 'test_file = "tests/test_outputs.py"\n'
        )
        (task / "task.toml").write_text(metadata, encoding="utf-8")
        (task / "instruction.md").write_text("Write output.txt.", encoding="utf-8")
        (task / "tests" / "test_outputs.py").write_text(
            "def test_output(): assert True\n", encoding="utf-8"
        )
    return project, tasks


def test_dry_run_is_read_only_and_uses_folder_id(tmp_path: Path) -> None:
    project, tasks = make_project(tmp_path)
    skill_path = project / ".agents" / "skills" / "target-id"
    skill_md = skill_path / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace("name: target-id", "name: other-name"),
        encoding="utf-8",
    )
    version_md = skill_path / ".evolution" / "versions" / "v1" / "SKILL.md"
    version_md.write_text(
        version_md.read_text(encoding="utf-8").replace("name: target-id", "name: other-name"),
        encoding="utf-8",
    )

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="mock",
        project_dir=project,
        tasks_dir=tasks,
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.skill_name == "target-id"
    assert result.plan is not None
    assert result.plan["solver_seed_mode"] == "forwarded"
    assert [item["seed"] for item in result.plan["diagnose"]] == [
        item["seed"] for item in result.plan["collect"]
    ]
    assert {item["seed"] for item in result.plan["saturation"]}.isdisjoint(
        {item["seed"] for item in result.plan["validate"]}
    )
    assert not (skill_path / ".evolution" / "runs").exists()


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("solver_model", ["codex/gpt-5.5", "gigachat/GigaChat-3-Ultra"])
def test_optimizer_rejects_unsupported_solver_before_state_or_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    solver_model: str,
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(
        optimizer,
        "get_backend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("backend instantiated")),
    )

    with pytest.raises(
        ValueError,
        match=f"Solver model {solver_model!r} does not support seeded completions",
    ):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model=solver_model,
            project_dir=project,
            tasks_dir=tasks,
            dry_run=dry_run,
        )

    assert not (project / ".agents/skills/target-id/.evolution/runs").exists()


def test_optimizer_rejects_unknown_injected_solver_before_state(tmp_path: Path) -> None:
    project, tasks = make_project(tmp_path)
    unknown_backend = object()

    with pytest.raises(ValueError, match="Solver backend does not support seeded completions"):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="openai/gpt-4o",
            project_dir=project,
            tasks_dir=tasks,
            solver_backend=unknown_backend,  # type: ignore[arg-type]
            dry_run=True,
        )

    assert not (project / ".agents/skills/target-id/.evolution/runs").exists()


def test_optimizer_allows_unsupported_editor_with_supported_solver(tmp_path: Path) -> None:
    project, tasks = make_project(tmp_path)

    class UnsupportedEditor:
        seed_mode = "unsupported"

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="mock",
        editor_model="codex/gpt-5.5",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=UnsupportedEditor(),  # type: ignore[arg-type]
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.plan is not None
    assert result.plan["solver_seed_mode"] == "forwarded"
    assert result.plan["rewrite"][0]["model"] == "codex/gpt-5.5"


def test_optimizer_rejects_invalid_agent_permission(tmp_path: Path) -> None:
    project, tasks = make_project(tmp_path)

    with pytest.raises(ValueError, match="agent_permission"):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="mock",
            project_dir=project,
            tasks_dir=tasks,
            agent_permission="sometimes",
            dry_run=True,
        )


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("solver_temperature", float("nan"), "solver_temperature"),
        ("solver_temperature", -0.1, "solver_temperature"),
        ("editor_temperature", 2.1, "editor_temperature"),
        ("editor_reasoning_effort", "auto", "editor_reasoning_effort"),
    ],
)
def test_optimizer_validates_role_runtime_before_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    value: object,
    message: str,
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(
        optimizer,
        "get_backend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model work started")),
    )

    with pytest.raises(ValueError, match=message):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="fake",
            project_dir=project,
            tasks_dir=tasks,
            **{option: value},
        )

    assert not (project / ".agents/skills/target-id/.evolution/runs").exists()


def eligible(skill, marker: str = "candidate") -> RewriteOutcome:
    body = f"{skill.body}\n\n{marker}\n"
    return RewriteOutcome(
        body=body,
        candidate_status=PROMOTED_ELIGIBLE,
        reason="eligible",
        preservation=None,
        codex_usage=None,
        candidate_debug_dir=None,
        model_id="editor",
        parent_body=skill.body,
        candidate_body_raw=body,
    )


def no_op(skill) -> RewriteOutcome:
    return RewriteOutcome(
        body=skill.body,
        candidate_status=NO_OP_TEXT,
        reason="no change",
        preservation=None,
        codex_usage=None,
        candidate_debug_dir=None,
        model_id="editor",
        parent_body=skill.body,
        candidate_body_raw=None,
    )


def install_execution_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_pass_rate: float,
    parent_pass_rate: float,
) -> list[tuple[str, int]]:
    order: list[tuple[str, int]] = []
    monkeypatch.setattr(optimizer, "_collect_one", lambda **kwargs: f"trace-{kwargs['trial']}")
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: eligible(skill))

    def validate(
        *, arm: str, seed: int, phase: str = "promotion_validation", **_kwargs: Any
    ) -> ValidationMeasurement:
        if phase == "validation_saturation":
            return ValidationMeasurement(seed=seed, passed=1, total=2, pass_rate=0.5)
        if phase == "candidate_train_diagnostic":
            return ValidationMeasurement(seed=seed, passed=1, total=1, pass_rate=1.0)
        order.append((arm, seed))
        rate = candidate_pass_rate if arm == "candidate" else parent_pass_rate
        return ValidationMeasurement(seed=seed, passed=int(rate), total=1, pass_rate=rate)

    monkeypatch.setattr(optimizer, "_validate_one", validate)
    return order


def test_optimizer_rejects_underpowered_plan_before_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    backend = MockBackend()
    monkeypatch.setattr(
        optimizer,
        "_collect_one",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("model work started")),
    )

    with pytest.raises(ValueError, match="increase --trials"):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="mock",
            project_dir=project,
            tasks_dir=tasks,
            trials=3,
            solver_backend=backend,
            editor_backend=backend,
        )

    assert backend.calls == []
    assert not (project / ".agents/skills/target-id/.evolution/runs").exists()


def test_saturated_parent_finishes_inconclusive_before_collect_or_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    phases: list[str] = []

    def validate(*, seed: int, phase: str, **_kwargs: Any) -> ValidationMeasurement:
        phases.append(phase)
        assert phase == "validation_saturation"
        return ValidationMeasurement(seed=seed, passed=1, total=1, pass_rate=1.0)

    monkeypatch.setattr(optimizer, "_validate_one", validate)
    monkeypatch.setattr(
        optimizer,
        "_collect_one",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("collect must not run")),
    )
    monkeypatch.setattr(
        optimizer,
        "_rewrite_round",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("editor must not run")),
    )

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="fake",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    assert phases == ["validation_saturation"] * 4
    assert result.status == "inconclusive"
    assert result.reason == "validation_saturated"
    assert "validation task" in (result.recommendation or "")
    assert result.final_version == "v1"
    assert result.rounds[0]["outcome"] == "inconclusive"
    assert result.rounds[0]["saturation"]["saturated"] is True


def test_candidate_train_diagnostic_reuses_collect_seeds_but_not_promotion_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    skill = SkillStore(project_dir=project).get("target-id")
    collect_seeds: list[int] = []
    diagnostic: list[tuple[int, str]] = []

    def collect(**kwargs: Any) -> str:
        collect_seeds.append(kwargs["attempt_seed"])
        return f"trace-{kwargs['trial']}"

    def validate(
        *, arm: str, seed: int, phase: str, bindings: list[SkillBinding], **_kwargs: Any
    ) -> ValidationMeasurement:
        if phase == "validation_saturation":
            return ValidationMeasurement(seed=seed, passed=0, total=1, pass_rate=0.0)
        if phase == "candidate_train_diagnostic":
            target_binding = next(binding for binding in bindings if binding.name == "target-id")
            diagnostic.append((seed, target_binding.version))
            return ValidationMeasurement(seed=seed, passed=0, total=1, pass_rate=0.0)
        rate = 1.0 if arm == "candidate" else 0.0
        return ValidationMeasurement(seed=seed, passed=int(rate), total=1, pass_rate=rate)

    monkeypatch.setattr(optimizer, "_collect_one", collect)
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: eligible(skill))
    monkeypatch.setattr(optimizer, "_validate_one", validate)

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="fake",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    assert result.status == "completed"
    assert result.final_version == "v2"
    assert [seed for seed, _version in diagnostic] == collect_seeds
    assert all(version.startswith("candidate@") for _seed, version in diagnostic)
    saved = result.rounds[0]["candidate_train_diagnostic"]["measurements"]["train"]
    assert [item["seed"] for item in saved] == collect_seeds
    assert LineageTracker(skill).current_version == "v2"


def test_positive_but_underpowered_gain_finishes_inconclusive_and_keeps_candidate_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    skill = SkillStore(project_dir=project).get("target-id")
    winning_seeds = {
        pair.seed
        for pair in optimizer.paired_schedule(["validate"], trials=4, base_seed=0, round_index=0)[
            :2
        ]
    }
    monkeypatch.setattr(optimizer, "_collect_one", lambda **kwargs: f"trace-{kwargs['trial']}")
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: eligible(skill))

    def validate(*, arm: str, seed: int, phase: str, **_kwargs: Any) -> ValidationMeasurement:
        if phase == "validation_saturation":
            rate = 0.0
        elif phase == "candidate_train_diagnostic":
            rate = 1.0
        else:
            rate = 1.0 if arm == "candidate" and seed in winning_seeds else 0.0
        return ValidationMeasurement(seed=seed, passed=int(rate), total=1, pass_rate=rate)

    monkeypatch.setattr(optimizer, "_validate_one", validate)

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="fake",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    assert result.status == "inconclusive"
    assert result.reason == "underpowered"
    assert "--trials" in (result.recommendation or "")
    assert result.final_version == "v1"
    assert result.rounds[0]["outcome"] == "inconclusive"
    assert result.rounds[0]["validation"]["decision"] == "inconclusive"
    assert not (VersionStore(skill).versions_dir / "v2").exists()
    candidate_hash = result.rounds[0]["candidate_artifact"]
    assert (result.run_path.parent / "candidates" / candidate_hash / "SKILL.md").is_file()
    assert not VersionStore(skill).exists("v2")
    assert not any(entry.version == "v2" for entry in LineageTracker(skill).lineage)


def test_optimizer_promotes_only_after_counterbalanced_paired_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    skill = SkillStore(project_dir=project).get("target-id")
    for root in (skill.path, VersionStore(skill).versions_dir / "v1"):
        reference = root / "references" / "keep.md"
        reference.parent.mkdir(exist_ok=True)
        reference.write_text("preserved\n", encoding="utf-8")
    order = install_execution_fakes(monkeypatch, candidate_pass_rate=1.0, parent_pass_rate=0.0)

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="fake",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    assert result.status == "completed"
    assert result.final_version == "v2"
    assert result.rounds[0]["outcome"] == "promoted"
    assert [arm for arm, _seed in order] == [
        "parent",
        "candidate",
        "candidate",
        "parent",
        "parent",
        "candidate",
        "candidate",
        "parent",
    ]
    assert all(order[index][1] == order[index + 1][1] for index in range(0, 8, 2))
    assert (VersionStore(skill).versions_dir / "v2" / "references" / "keep.md").is_file()
    assert LineageTracker(skill).current_version == "v2"


def test_optimizer_rejection_is_a_completed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    install_execution_fakes(monkeypatch, candidate_pass_rate=0.0, parent_pass_rate=1.0)

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="fake",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    skill = SkillStore(project_dir=project).get("target-id")
    assert result.status == "completed"
    assert result.final_version == "v1"
    assert result.rounds[0]["outcome"] == "rejected"
    assert not VersionStore(skill).exists("v2")
    assert not any(entry.version == "v2" for entry in LineageTracker(skill).lineage)
    candidate_hash = result.rounds[0]["candidate_artifact"]
    assert (result.run_path.parent / "candidates" / candidate_hash / "SKILL.md").is_file()


def test_optimizer_never_overwrites_a_parent_changed_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    skill = SkillStore(project_dir=project).get("target-id")
    install_execution_fakes(monkeypatch, candidate_pass_rate=1.0, parent_pass_rate=0.0)
    validate = optimizer._validate_one
    calls = 0

    def change_parent(**kwargs: Any) -> ValidationMeasurement:
        nonlocal calls
        measurement = validate(**kwargs)
        calls += 1
        if calls == 8:
            skill.skill_md_path.write_text(
                skill.skill_md_path.read_text(encoding="utf-8") + "\nmanual concurrent edit\n",
                encoding="utf-8",
            )
        return measurement

    monkeypatch.setattr(optimizer, "_validate_one", change_parent)

    with pytest.raises(RuntimeError, match="promotion_parent_changed"):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="fake",
            project_dir=project,
            tasks_dir=tasks,
            solver_backend=MockBackend(),
            editor_backend=object(),  # type: ignore[arg-type]
            trials=4,
        )

    assert LineageTracker(skill).current_version == "v1"
    assert "manual concurrent edit" in skill.skill_md_path.read_text(encoding="utf-8")


def test_supporting_skill_is_pinned_and_preserved_in_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    support, _ = SkillStore(project_dir=project).create("support-id", "Supporting skill")
    for task_id in ("train", "validate"):
        metadata = (tasks / task_id / "task.toml").read_text(encoding="utf-8")
        (tasks / task_id / "task.toml").write_text(
            metadata.replace(
                'required = ["target-id"]',
                'required = ["support-id", "target-id"]',
            ),
            encoding="utf-8",
        )
    seen: list[list[SkillBinding]] = []
    seen_env: list[tuple[str, ...]] = []

    def collect(**kwargs: Any) -> str:
        seen.append(kwargs["bindings"])
        seen_env.append(tuple(kwargs["pass_env"]))
        return f"trace-{kwargs['trial']}"

    monkeypatch.setattr(optimizer, "_collect_one", collect)
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: no_op(skill))
    optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="fake",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=MockBackend(),
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
        pass_env=("SAFE_INPUT",),
    )

    assert seen
    assert [binding.owner.path.name for binding in seen[0]] == ["support-id", "target-id"]
    assert seen[0][0].sha256 == VersionStore(support).digest("v1")
    assert seen_env == [("SAFE_INPUT",)] * 4


def test_training_has_normal_traces_but_validation_has_only_scalar_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "training", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: eligible(skill))
    response = "```python\nprint('SECRET_VALIDATION_SENTINEL')\n```"
    backend = MockBackend(responses=[response] * 16)

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="mock",
        project_dir=project,
        tasks_dir=tasks,
        solver_backend=backend,
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    skill = SkillStore(project_dir=project).get("target-id")
    assert TraceStore(skill).count() == 4
    state_text = result.run_path.read_text(encoding="utf-8")
    assert "SECRET_VALIDATION_SENTINEL" not in state_text
    measurements = read_state(result.run_path)["rounds"][0]["validation"]["measurements"]
    for arm in ("parent", "candidate"):
        for item in measurements[arm]["validate"]:
            assert set(item) == {"seed", "passed", "total", "pass_rate"}


def test_resume_is_relocatable_and_does_not_repeat_completed_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    collected: list[str] = []
    reflected: list[str] = []

    def collect(**kwargs: Any) -> str:
        trace_id = f"trace-{kwargs['trial']}"
        collected.append(trace_id)
        return trace_id

    monkeypatch.setattr(optimizer, "_collect_one", collect)

    def reflect(**_kwargs: Any) -> list[dict[str, str]]:
        reflected.append("called")
        return [{"trace_id": "trace-0", "kind": "failure"}]

    monkeypatch.setattr(optimizer, "_reflect_round", reflect)
    monkeypatch.setattr(
        optimizer,
        "_rewrite_round",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("editor_outage")),
    )
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "fake",
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    with pytest.raises(RuntimeError, match="editor_outage"):
        optimize_skill(
            "target-id",
            project_dir=project,
            tasks_dir=tasks,
            **kwargs,  # type: ignore[arg-type]
        )
    runs = project / ".agents" / "skills" / "target-id" / ".evolution" / "runs"
    run_id = next(runs.iterdir()).name
    old_root = str(tmp_path)
    relocated = tmp_path.parent / f"{tmp_path.name}-relocated"
    relocated.mkdir()
    project.rename(relocated / "project")
    tasks.rename(relocated / "tasks")
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: no_op(skill))
    monkeypatch.setattr(
        optimizer,
        "_load_round_reflections",
        lambda _skill, trace_ids: [{"trace_id": trace_id} for trace_id in trace_ids],
    )

    result = optimize_skill(
        "target-id",
        project_dir=relocated / "project",
        tasks_dir=relocated / "tasks",
        resume=run_id,
        **kwargs,  # type: ignore[arg-type]
    )

    assert result.status == "completed"
    assert len(collected) == 4
    assert reflected == ["called"]
    assert old_root not in result.run_path.read_text(encoding="utf-8")


def test_resume_refuses_task_content_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    monkeypatch.setattr(optimizer, "_collect_one", lambda **kwargs: f"trace-{kwargs['trial']}")
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reflection_outage")),
    )
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "fake",
        "project_dir": project,
        "tasks_dir": tasks,
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    with pytest.raises(RuntimeError):
        optimize_skill("target-id", **kwargs)  # type: ignore[arg-type]
    runs = project / ".agents" / "skills" / "target-id" / ".evolution" / "runs"
    run_id = next(runs.iterdir()).name
    (tasks / "train" / "instruction.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Resume identity"):
        optimize_skill("target-id", **kwargs, resume=run_id)  # type: ignore[arg-type]


def test_resume_identity_tracks_sanitized_role_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    monkeypatch.setattr(optimizer, "_collect_one", lambda **kwargs: f"trace-{kwargs['trial']}")
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reflection_outage")),
    )
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "solver",
        "editor_model": "editor",
        "project_dir": project,
        "tasks_dir": tasks,
        "solver_api_base": (
            "http://solver-user:solver-secret@127.0.0.1:32035/v1?key=one#solver-frag"
        ),
        "editor_api_base": (
            "http://editor-user:editor-secret@127.0.0.1:30004/v1?token=two#editor-frag"
        ),
        "solver_temperature": 0,
        "editor_temperature": 1,
        "solver_reasoning_effort": "off",
        "editor_reasoning_effort": "medium",
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    expected_runtime = {
        "solver": {
            "api_base": "http://127.0.0.1:32035/v1",
            "temperature": 0.0,
            "reasoning_effort": "off",
        },
        "editor": {
            "api_base": "http://127.0.0.1:30004/v1",
            "temperature": 1.0,
            "reasoning_effort": "medium",
        },
    }
    plan = optimize_skill("target-id", **kwargs, dry_run=True)  # type: ignore[arg-type]
    assert plan.plan is not None and plan.plan["runtime"] == expected_runtime

    with pytest.raises(optimizer.OptimizationRunError, match="runtimeerror"):
        optimize_skill("target-id", **kwargs)  # type: ignore[arg-type]
    runs = project / ".agents" / "skills" / "target-id" / ".evolution" / "runs"
    run_path = next(runs.glob("*/run.json"))
    state = read_state(run_path)

    assert state["identity"]["runtime"] == expected_runtime
    assert state["identity"]["solver_seed_mode"] == "forwarded"
    state_text = f"{plan.plan!s}\n{run_path.read_text(encoding='utf-8')}"
    for secret in (
        "solver-user",
        "solver-secret",
        "key=one",
        "solver-frag",
        "editor-user",
        "editor-secret",
        "token=two",
        "editor-frag",
    ):
        assert secret not in state_text

    with pytest.raises(ValueError, match="Resume identity"):
        optimize_skill(
            "target-id",
            **{**kwargs, "editor_temperature": 0.5},
            resume=run_path.parent.name,
        )  # type: ignore[arg-type]


def test_resume_migrates_legacy_identity_without_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    monkeypatch.setattr(optimizer, "_collect_one", lambda **kwargs: f"trace-{kwargs['trial']}")
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reflection_outage")),
    )
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "solver",
        "editor_model": "editor",
        "project_dir": project,
        "tasks_dir": tasks,
        "api_base": "http://127.0.0.1:8000/v1",
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    with pytest.raises(optimizer.OptimizationRunError):
        optimize_skill("target-id", **kwargs)  # type: ignore[arg-type]
    run_path = next((project / ".agents/skills/target-id/.evolution/runs").glob("*/run.json"))
    legacy_state = read_state(run_path)
    legacy_state["identity"].pop("runtime")
    legacy_state["identity_sha256"] = json_digest(legacy_state["identity"])
    write_state(run_path, legacy_state)
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: no_op(skill))

    result = optimize_skill(
        "target-id",
        **kwargs,
        resume=run_path.parent.name,  # type: ignore[arg-type]
    )

    migrated = read_state(run_path)
    assert result.status == "completed"
    assert migrated["identity"]["runtime"]["solver"]["api_base"] == ("http://127.0.0.1:8000/v1")
    assert migrated["identity_sha256"] == json_digest(migrated["identity"])


def test_resume_rejects_legacy_identity_without_solver_seed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    monkeypatch.setattr(optimizer, "_collect_one", lambda **kwargs: f"trace-{kwargs['trial']}")
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reflection_outage")),
    )
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "solver",
        "project_dir": project,
        "tasks_dir": tasks,
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    with pytest.raises(optimizer.OptimizationRunError):
        optimize_skill("target-id", **kwargs)  # type: ignore[arg-type]
    run_path = next((project / ".agents/skills/target-id/.evolution/runs").glob("*/run.json"))
    legacy_state = read_state(run_path)
    legacy_state["identity"].pop("runtime")
    legacy_state["identity"].pop("solver_seed_mode")
    write_state(run_path, legacy_state)

    with pytest.raises(ValueError, match="Resume identity"):
        optimize_skill(
            "target-id",
            **kwargs,
            resume=run_path.parent.name,
        )  # type: ignore[arg-type]


def test_optimizer_rejects_opaque_api_base_without_persisting_secret(tmp_path: Path) -> None:
    project, tasks = make_project(tmp_path)

    with pytest.raises(ValueError, match="absolute HTTP"):
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="solver",
            project_dir=project,
            tasks_dir=tasks,
            solver_api_base="https:secret@example.test/v1?token=x",
            dry_run=True,
        )

    assert not (project / ".agents/skills/target-id/.evolution/runs").exists()


def test_blank_legacy_temperature_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setenv("EVO_LLM_TEMPERATURE", "")

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="solver",
        project_dir=project,
        tasks_dir=tasks,
        dry_run=True,
    )

    assert result.plan is not None
    assert result.plan["runtime"]["solver"]["temperature"] is None


def test_resume_refuses_candidate_tree_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    install_execution_fakes(monkeypatch, candidate_pass_rate=1.0, parent_pass_rate=0.0)
    monkeypatch.setattr(
        optimizer,
        "_validate_one",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("validation_unavailable")),
    )
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "fake",
        "project_dir": project,
        "tasks_dir": tasks,
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    with pytest.raises(RuntimeError, match="validation_unavailable"):
        optimize_skill("target-id", **kwargs)  # type: ignore[arg-type]
    evolution = project / ".agents" / "skills" / "target-id" / ".evolution"
    run_id = next((evolution / "runs").iterdir()).name
    state = json.loads((evolution / "runs" / run_id / "run.json").read_text())
    candidate_hash = state["rounds"][0]["candidate_artifact"]
    tamper = (
        evolution / "runs" / run_id / "candidates" / candidate_hash / "references" / "tamper.txt"
    )
    tamper.parent.mkdir(exist_ok=True)
    tamper.write_text("changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate_content_tampered"):
        optimize_skill("target-id", **kwargs, resume=run_id)  # type: ignore[arg-type]


def test_resume_accepts_completed_promotion_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    install_execution_fakes(monkeypatch, candidate_pass_rate=1.0, parent_pass_rate=0.0)
    original = LineageTracker.promote
    interrupted = True

    def promote_then_crash(self, version: str, fitness: float | None = None) -> None:
        nonlocal interrupted
        original(self, version, fitness)
        if interrupted:
            interrupted = False
            raise RuntimeError("promotion_interrupted")

    monkeypatch.setattr(LineageTracker, "promote", promote_then_crash)
    kwargs = {
        "train_tasks": ["train"],
        "validate_tasks": ["validate"],
        "solver_model": "fake",
        "project_dir": project,
        "tasks_dir": tasks,
        "solver_backend": MockBackend(),
        "editor_backend": object(),
        "trials": 4,
    }
    with pytest.raises(RuntimeError, match="promotion_interrupted"):
        optimize_skill("target-id", **kwargs)  # type: ignore[arg-type]
    runs = project / ".agents" / "skills" / "target-id" / ".evolution" / "runs"
    run_id = next(runs.iterdir()).name
    recalled: list[str] = []

    def spy(self, version: str, fitness: float | None = None) -> None:
        recalled.append(version)
        original(self, version, fitness)

    monkeypatch.setattr(LineageTracker, "promote", spy)
    result = optimize_skill("target-id", **kwargs, resume=run_id)  # type: ignore[arg-type]

    assert result.status == "completed"
    assert result.final_version == "v2"
    assert recalled == []


def test_validation_failure_persists_only_a_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    install_execution_fakes(monkeypatch, candidate_pass_rate=1.0, parent_pass_rate=0.0)
    secret = "SECRET_PROVIDER_DIAGNOSTIC"
    monkeypatch.setattr(
        optimizer,
        "_validate_one",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    with pytest.raises(optimizer.OptimizationRunError, match="runtimeerror") as exc_info:
        optimize_skill(
            "target-id",
            train_tasks=["train"],
            validate_tasks=["validate"],
            solver_model="fake",
            project_dir=project,
            tasks_dir=tasks,
            solver_backend=MockBackend(),
            editor_backend=object(),  # type: ignore[arg-type]
            trials=4,
        )
    assert secret in str(exc_info.value.__cause__)

    runs = project / ".agents" / "skills" / "target-id" / ".evolution" / "runs"
    state_text = next(runs.glob("*/run.json")).read_text(encoding="utf-8")
    assert secret not in state_text
    assert '"reason_code": "runtimeerror"' in state_text


def test_explicit_runtime_settings_precede_environment_and_route_by_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    install_execution_fakes(monkeypatch, candidate_pass_rate=0.0, parent_pass_rate=1.0)
    calls: list[tuple[str, dict[str, Any]]] = []

    def backend(model: str, **kwargs: Any) -> object:
        calls.append((model, kwargs))
        return MockBackend()

    monkeypatch.setattr(optimizer, "get_backend", backend)
    monkeypatch.setenv("EVO_LLM_MAX_TOKENS", "111")
    monkeypatch.setenv("EVO_LLM_TEMPERATURE", "2")
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "high")

    optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="openai/Qwen/Qwen3.5-35B-A3B",
        editor_model="openai/openai/gpt-oss-120b",
        project_dir=project,
        tasks_dir=tasks,
        api_base="https://shared.example/v1",
        solver_api_base="http://127.0.0.1:32035/v1",
        editor_api_base="http://127.0.0.1:30004/v1",
        solver_temperature=0,
        editor_temperature=1,
        solver_reasoning_effort="off",
        editor_reasoning_effort="medium",
        max_tokens=222,
        trials=4,
    )

    assert calls == [
        (
            "openai/Qwen/Qwen3.5-35B-A3B",
            {
                "agent_permission": "safe",
                "max_tokens": 222,
                "api_base": "http://127.0.0.1:32035/v1",
                "temperature": 0.0,
                "reasoning_effort": "off",
            },
        ),
        (
            "openai/openai/gpt-oss-120b",
            {
                "agent_permission": "safe",
                "max_tokens": 222,
                "api_base": "http://127.0.0.1:30004/v1",
                "temperature": 1.0,
                "reasoning_effort": "medium",
            },
        ),
    ]


def test_optimizer_keeps_legacy_runtime_environment_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setenv("EVO_LLM_TEMPERATURE", "0.75")
    monkeypatch.setenv("EVO_LLM_REASONING_EFFORT", "xhigh")

    result = optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="solver",
        editor_model="editor",
        project_dir=project,
        tasks_dir=tasks,
        api_base="https://shared.example/v1",
        dry_run=True,
    )

    expected = {
        "api_base": "https://shared.example/v1",
        "temperature": 0.75,
        "reasoning_effort": "xhigh",
    }
    assert result.plan is not None and result.plan["runtime"] == {
        "solver": expected,
        "editor": expected,
    }


def test_optimizer_passes_local_api_base_to_solver_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, tasks = make_project(tmp_path)
    monkeypatch.setattr(optimizer, "_parent_saturation_preflight", lambda **_kwargs: False)
    seen_api_bases: list[str | None] = []

    def collect(**kwargs: Any) -> str:
        seen_api_bases.append(kwargs["backend"]._kwargs.get("api_base"))
        return f"trace-{kwargs['trial']}"

    monkeypatch.setattr(optimizer, "_collect_one", collect)
    monkeypatch.setattr(
        optimizer,
        "_reflect_round",
        lambda **_kwargs: [{"trace_id": "trace-0", "kind": "failure"}],
    )
    monkeypatch.setattr(optimizer, "_rewrite_round", lambda skill, **_kwargs: no_op(skill))
    monkeypatch.setattr(
        optimizer,
        "get_backend",
        lambda _model, **kwargs: SimpleNamespace(_kwargs=kwargs),
    )
    optimize_skill(
        "target-id",
        train_tasks=["train"],
        validate_tasks=["validate"],
        solver_model="openai/local-model",
        editor_model="codex/gpt-5.6-luna",
        project_dir=project,
        tasks_dir=tasks,
        api_base="http://127.0.0.1:30004/v1",
        editor_backend=object(),  # type: ignore[arg-type]
        trials=4,
    )

    assert seen_api_bases == ["http://127.0.0.1:30004/v1"] * 4


def test_resume_reflections_fail_closed_when_sidecar_is_missing(tmp_path: Path) -> None:
    project, _tasks = make_project(tmp_path)
    skill = SkillStore(project_dir=project).get("target-id")

    with pytest.raises(RuntimeError, match="reflection_checkpoint_missing"):
        optimizer._load_round_reflections(skill, ["missing-trace"])


def test_resume_reuses_only_valid_orphan_collect_trace(tmp_path: Path) -> None:
    project, _tasks = make_project(tmp_path)
    skill = SkillStore(project_dir=project).get("target-id")
    context = {
        "phase": "collect",
        "round_id": "run.r000",
        "attempt_seed": 42,
        "trace_spec_idx": 0,
        "scored_measurement_valid": False,
    }
    invalid = SkillTrace(
        id="transport-failure",
        skill_name="target-id",
        skill_version="v1",
        model="openai/local-model",
        task="train",
        timestamp=datetime.now(UTC),
        total=1,
        errors=["LLM call failed after retry"],
        trace_context=context,
    )
    TraceStore(skill).save(invalid, "prompt", "", "raise SystemExit(1)")

    assert (
        optimizer._find_collect_trace(
            skill,
            task_id="train",
            round_id="run.r000",
            seed=42,
            trace_spec_idx=0,
        )
        is None
    )

    valid = invalid.model_copy(
        update={
            "id": "valid-measurement",
            "trace_context": {**context, "scored_measurement_valid": True},
        }
    )
    TraceStore(skill).save(valid, "prompt", "response", "print('ok')")

    assert (
        optimizer._find_collect_trace(
            skill,
            task_id="train",
            round_id="run.r000",
            seed=42,
            trace_spec_idx=0,
        )
        == "valid-measurement"
    )
