"""Public lifecycle APIs for trace-driven skill evolution.

This module is the framework-facing surface for users who want to compose
Evolution from Python instead of shelling out to ``evo``.  Paper experiments
may call these APIs, but the APIs deliberately use generic task, skill, trace,
and version vocabulary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from evolution.core.models import LineageEntry, LineageOrigin, Skill, SkillFrontmatter
from evolution.core.parser import parse_skill_md, write_skill_md
from evolution.core.store import SkillStore
from evolution.core.validator import ValidationResult, validate_skill
from evolution.eval.runner import run_task
from evolution.eval.solver import SolveResult, solve_task
from evolution.eval.task import Task, TaskResult, discover_tasks
from evolution.eval.workspace import setup_workspace
from evolution.evolution.reflect import (
    collect_reflections,
    reflect_failing_traces,
    rewrite_skill_with_audit,
)
from evolution.evolution.rewrite_outcome import PROMOTED_ELIGIBLE, RewriteOutcome
from evolution.lineage.tracker import LineageTracker
from evolution.lineage.traces import TraceStore
from evolution.lineage.version import VersionStore
from evolution.llm.backend import LLMBackend, get_backend
from evolution.splits import SplitManifest


@dataclass(frozen=True)
class TraceView:
    """A resolved trace directory plus its metadata and loaded sections."""

    trace_id: str
    trace_dir: Path
    metadata: dict[str, Any]
    sections: dict[str, str]


def skill_store(
    *,
    project_dir: Path | str | None = None,
    user_dir: Path | str | None = None,
    experiment: str | None = None,
) -> SkillStore:
    """Return a skill store for a project or a named skill-fork experiment."""

    project = Path(project_dir or Path.cwd())
    if experiment:
        from evolution.core.experiments import get_experiment_store

        return get_experiment_store(project, experiment)
    return SkillStore(project_dir=project, user_dir=_user_dir(user_dir))


def list_tasks(
    tasks_dir: Path | str | None = None,
    *,
    project_dir: Path | str | None = None,
) -> list[Task]:
    """Discover framework tasks under a task root."""

    root = Path(tasks_dir) if tasks_dir is not None else Path(project_dir or Path.cwd()) / "tasks"
    return discover_tasks(root)


def get_task(
    task_name: str,
    *,
    tasks_dir: Path | str | None = None,
    project_dir: Path | str | None = None,
) -> Task:
    """Load one task by name from a task root."""

    by_name = {task.name: task for task in list_tasks(tasks_dir, project_dir=project_dir)}
    try:
        return by_name[task_name]
    except KeyError as exc:
        available = ", ".join(sorted(by_name)) or "<none>"
        raise KeyError(f"Task {task_name!r} not found. Available tasks: {available}") from exc


def resolve_skills(
    name: str,
    *,
    store: SkillStore | None = None,
    project_dir: Path | str | None = None,
    user_dir: Path | str | None = None,
    snapshot: str | None = None,
) -> list[Skill]:
    """Resolve a skill or skill container name to concrete skill objects."""

    project = Path(project_dir or Path.cwd())
    store = store or skill_store(project_dir=project, user_dir=user_dir)

    from evolution.core.containers import ContainerStore

    containers = ContainerStore(project_dir=project, user_dir=_user_dir(user_dir))
    if containers.has(name):
        return containers.resolve(name, store, snapshot_label=snapshot)
    if store.has(name):
        return [store.get(name)]
    return []


def evaluate_task(
    task_name: str,
    workspace: Path | str,
    *,
    tasks_dir: Path | str | None = None,
    project_dir: Path | str | None = None,
    timeout: float | None = None,
) -> TaskResult:
    """Run one task evaluator against an existing workspace."""

    task = get_task(task_name, tasks_dir=tasks_dir, project_dir=project_dir)
    return run_task(task, Path(workspace), timeout=timeout)


def solve(
    task_name: str,
    *,
    model: str,
    project_dir: Path | str | None = None,
    tasks_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    backend: LLMBackend | None = None,
    skill_name: str | None = None,
    no_skill: bool = False,
    snapshot: str | None = None,
    experiment: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 16384,
    timeout: float = 300.0,
    manifest: SplitManifest | Path | str | None = None,
    manifest_scope: str | None = None,
    attempt_seed: int | None = None,
    trace_spec_idx: int | None = None,
    round_id: str | None = None,
    phase: str = "collect",
) -> SolveResult:
    """Set up a workspace, solve a task, evaluate it, and record traces."""

    project = Path(project_dir or Path.cwd())
    task = get_task(task_name, tasks_dir=tasks_dir, project_dir=project)
    ws = Path(workspace) if workspace is not None else Path(f"/tmp/evo-{task.name}")
    setup_workspace(task, ws)

    if backend is None:
        backend_kwargs: dict[str, Any] = {"max_tokens": max_tokens}
        if api_base:
            backend_kwargs["api_base"] = api_base
        if api_key:
            backend_kwargs["api_key"] = api_key
        elif api_base:
            backend_kwargs["api_key"] = "not-needed"
        backend = get_backend(model, **backend_kwargs)

    resolved_skills: list[Skill] = []
    if not no_skill:
        store = skill_store(project_dir=project, experiment=experiment)
        primary = skill_name or (task.skills_required[0] if task.skills_required else None)
        if primary:
            resolved_skills = resolve_skills(primary, store=store, project_dir=project, snapshot=snapshot)

    split_manifest = _coerce_manifest(manifest)

    return solve_task(
        task,
        backend,
        ws,
        skills=resolved_skills or None,
        timeout=timeout,
        model_name=model,
        manifest=split_manifest,
        manifest_scope=manifest_scope,
        attempt_seed=attempt_seed,
        trace_spec_idx=trace_spec_idx,
        round_id=round_id,
        phase=phase,
    )


def list_traces(
    skill_name: str,
    *,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    version: str | None = None,
    model: str | None = None,
    task: str | None = None,
) -> list[dict]:
    """List saved traces for a skill."""

    skill = skill_store(project_dir=project_dir, experiment=experiment).get(skill_name)
    return TraceStore(skill).list(skill_version=version, model=model, task=task)


def load_trace(
    skill_name: str,
    trace_id: str | int | None = None,
    *,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    last: bool = False,
    sections: tuple[str, ...] = ("prompt", "response", "code", "result"),
) -> TraceView:
    """Resolve and load a trace by id, one-based index, negative index, or latest."""

    skill = skill_store(project_dir=project_dir, experiment=experiment).get(skill_name)
    store = TraceStore(skill)
    index = store.list()
    if not index:
        raise KeyError(f"No traces found for skill {skill_name!r}")

    if last or trace_id is None:
        resolved = index[-1]["id"]
    elif isinstance(trace_id, int) or str(trace_id).lstrip("-").isdigit():
        idx = int(trace_id)
        if idx > 0:
            idx -= 1
        try:
            resolved = index[idx]["id"]
        except IndexError as exc:
            raise IndexError(f"Trace index {trace_id!r} is out of range") from exc
    else:
        resolved = str(trace_id)

    trace_dir = store.traces_dir / resolved
    if not trace_dir.is_dir():
        raise KeyError(f"Trace {resolved!r} not found for skill {skill_name!r}")

    metadata_path = trace_dir / "result.json"
    metadata = {}
    if metadata_path.exists():
        import json

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    filenames = {
        "prompt": "prompt.md",
        "response": "response.md",
        "code": "solve.py",
        "result": "result.json",
        "evidence": "evidence.json",
        "reflection": "reflection.json",
    }
    loaded: dict[str, str] = {}
    for section in sections:
        path = trace_dir / filenames[section]
        if path.exists():
            loaded[section] = path.read_text(encoding="utf-8")

    return TraceView(trace_id=resolved, trace_dir=trace_dir, metadata=metadata, sections=loaded)


def reflect(
    skill_name: str,
    *,
    model: str = "openrouter/anthropic/claude-sonnet-4",
    backend: LLMBackend | None = None,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    task: str | None = None,
    tasks_dir: Path | str | None = None,
    trace_model: str | None = None,
    limit: int | None = None,
    api_base: str | None = None,
    manifest: SplitManifest | Path | str | None = None,
    scope: str = "skill_evolution",
    strict: bool = True,
    require_v3_evidence: bool = False,
) -> list[dict]:
    """Analyze eligible failing traces and save ``reflection.json`` files."""

    project = Path(project_dir or Path.cwd())
    skill = skill_store(project_dir=project, experiment=experiment).get(skill_name)
    task_instruction = ""
    if task:
        task_instruction = get_task(task, tasks_dir=tasks_dir, project_dir=project).instruction

    if backend is None:
        backend_kwargs: dict[str, Any] = {}
        if api_base:
            backend_kwargs["api_base"] = api_base
        backend = get_backend(model, **backend_kwargs)

    split_manifest = _coerce_manifest(manifest)

    return reflect_failing_traces(
        backend,
        skill,
        task_instruction,
        task_filter=task,
        trace_model=trace_model,
        limit=limit,
        manifest=split_manifest,
        scope=scope,
        strict=strict,
        require_v3_evidence=require_v3_evidence,
    )


def rewrite(
    skill_name: str,
    *,
    model: str = "openrouter/anthropic/claude-sonnet-4",
    backend: LLMBackend | None = None,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    task: str | None = None,
    tasks_dir: Path | str | None = None,
    trace_model: str | None = None,
    skill_version: str | None = None,
    api_base: str | None = None,
    manifest: SplitManifest | Path | str | None = None,
    scope: str = "skill_evolution",
    include_best_trace_code: bool = False,
    file_edit: bool = False,
    apply: bool = False,
) -> RewriteOutcome:
    """Rewrite a skill from saved reflections and optionally update SKILL.md."""

    project = Path(project_dir or Path.cwd())
    skill = skill_store(project_dir=project, experiment=experiment).get(skill_name)
    split_manifest = _coerce_manifest(manifest)
    reflections = collect_reflections(
        skill,
        task_filter=task,
        trace_model=trace_model,
        skill_version=skill_version,
        manifest=split_manifest,
        scope=scope,
    )
    if not reflections:
        raise ValueError(f"No reflections found for skill {skill_name!r}")

    task_instruction = ""
    if task:
        task_instruction = get_task(task, tasks_dir=tasks_dir, project_dir=project).instruction

    if backend is None:
        backend_kwargs: dict[str, Any] = {}
        if api_base:
            backend_kwargs["api_base"] = api_base
        backend = get_backend(model, **backend_kwargs)

    outcome = asyncio.run(
        rewrite_skill_with_audit(
            backend,
            skill,
            reflections,
            task_instruction=task_instruction,
            task_filter=task,
            manifest=split_manifest,
            include_best_trace_code=include_best_trace_code,
            file_edit=file_edit,
        )
    )

    if apply and outcome.candidate_status == PROMOTED_ELIGIBLE:
        raw_fm, _ = parse_skill_md(skill.skill_md_path)
        write_skill_md(skill.skill_md_path, SkillFrontmatter.model_validate(raw_fm), outcome.body)

    return outcome


def validate_candidate(skill_name: str, *, project_dir: Path | str | None = None) -> ValidationResult:
    """Validate the current working-copy SKILL.md for a skill."""

    skill = skill_store(project_dir=project_dir).get(skill_name)
    return validate_skill(skill.path)


def commit_skill(
    skill_name: str,
    *,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    origin: str = "manual",
    mutation_type: str = "manual-edit",
    promote: bool = True,
    fitness: float | None = None,
) -> str:
    """Snapshot the current working-copy skill and optionally promote it."""

    skill = skill_store(project_dir=project_dir, experiment=experiment).get(skill_name)
    validation = validate_skill(skill.path)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))

    tracker = LineageTracker(skill)
    version = tracker.next_version_label()
    VersionStore(skill).save(version)
    tracker.record(
        LineageEntry(
            version=version,
            parent=tracker.current_version or None,
            timestamp=datetime.now(),
            origin=cast(LineageOrigin, origin),
            mutation_type=mutation_type,
            fitness=fitness,
            status="candidate",
        )
    )
    if promote:
        tracker.promote(version, fitness=fitness)
    return version


def promote_skill(
    skill_name: str,
    version: str,
    *,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    fitness: float | None = None,
) -> None:
    """Promote an existing skill version."""

    skill = skill_store(project_dir=project_dir, experiment=experiment).get(skill_name)
    LineageTracker(skill).promote(version, fitness=fitness)


def reject_skill(
    skill_name: str,
    version: str,
    *,
    project_dir: Path | str | None = None,
    experiment: str | None = None,
    fitness: float | None = None,
) -> None:
    """Mark an existing candidate version as rejected."""

    skill = skill_store(project_dir=project_dir, experiment=experiment).get(skill_name)
    LineageTracker(skill).reject(version, fitness=fitness)


def _user_dir(user_dir: Path | str | None) -> Path:
    return Path(user_dir).expanduser() if user_dir else Path.home()


def _coerce_manifest(manifest: SplitManifest | Path | str | None) -> SplitManifest | None:
    if manifest is None or isinstance(manifest, SplitManifest):
        return manifest
    return SplitManifest.load(Path(manifest))
