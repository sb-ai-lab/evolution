"""Task 5 — no trace cap + cross-source trace-model fix.

Part A: ``reflect_failing_traces(limit=None)`` reflects EVERY eligible
        failing trace (not the legacy implicit 10). The adapter must NOT
        append ``--limit`` and must not introduce any ``EVO_REFLECT_*LIMIT``
        env var.

Part B: in a cross-source cell (failing traces span >1 source model) the
        adapter omits ``--trace-model`` so every in-cell source model's
        failures get reflected, while ``--task`` + ``--manifest`` scoping
        keeps out-of-cell traces out.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from evolution.core.models import Skill, SkillFrontmatter, SkillTrace
from evolution.evolution.reflect import reflect_failing_traces
from evolution.lineage.traces import TraceStore
from evolution.llm.mock_backend import MockBackend

_VALID_REFLECTION = json.dumps(
    {
        "root_cause": "wrong output",
        "skill_gap": "missing validation",
        "suggested_fix": "check the expected file",
        "severity": "major",
        "pattern": "logic_error",
    }
)


def _make_skill(tmp_path: Path) -> Skill:
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nUse Python.\n",
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body="Use Python.",
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _save_failed_trace(
    skill: Skill,
    trace_id: str,
    *,
    task: str = "task-name",
    model: str = "solver-model",
) -> Path:
    trace = SkillTrace(
        id=trace_id,
        skill_name=skill.name,
        skill_version="v1",
        model=model,
        task=task,
        timestamp=datetime(2026, 5, 15, 12, 0, 0),
        passed=0,
        total=1,
        pass_rate=0.0,
        success=False,
        errors=["failed"],
    )
    return TraceStore(skill).save(
        trace,
        prompt="prompt",
        response="response",
        code="print('wrong')",
        evidence={
            "schema": "evolution-trace-evidence-v3",
            "agent_solve": {"exit_code": 1, "timeout": False, "diagnostic": "nonzero_exit"},
            "produced_artifacts": [],
        },
    )


# ---------------------------------------------------------------------------
# Part A — uncap
# ---------------------------------------------------------------------------


def test_reflect_without_limit_reflects_all_failing_traces(tmp_path: Path):
    """>10 distinct failing traces; no --limit ⇒ ALL reflected (not 10)."""
    skill = _make_skill(tmp_path)
    n = 17
    for i in range(n):
        _save_failed_trace(skill, f"trace-{i:02d}")

    backend = MockBackend(default_response=_VALID_REFLECTION)

    reflections = reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="task-name",
        strict=True,
        # limit defaults to None ⇒ no cap
    )

    assert len(reflections) == n, (
        f"expected ALL {n} failing traces reflected, got {len(reflections)} "
        "(legacy implicit-10 cap leaked through)"
    )
    # Every trace folder now carries a reflection.
    ts = TraceStore(skill)
    reflected = sum(1 for d in ts.traces_dir.iterdir() if (d / "reflection.json").exists())
    assert reflected == n


def test_reflect_default_is_none_not_ten(tmp_path: Path):
    """Default arg is None (no cap), not the legacy 10."""
    import inspect

    sig = inspect.signature(reflect_failing_traces)
    assert sig.parameters["limit"].default is None


def test_explicit_limit_still_caps(tmp_path: Path):
    """Callers that still pass an int keep the bounded behaviour."""
    skill = _make_skill(tmp_path)
    for i in range(8):
        _save_failed_trace(skill, f"trace-{i:02d}")

    backend = MockBackend(default_response=_VALID_REFLECTION)

    reflections = reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="task-name",
        limit=3,
        strict=True,
    )
    assert len(reflections) == 3


def test_adapter_argv_has_no_limit_and_no_limit_env(tmp_path: Path, monkeypatch):
    """`evo reflect`/`rewrite` argv built by the adapter carries NO --limit,
    and no EVO_REFLECT_*LIMIT env var is introduced anywhere."""
    from evolution.adapters import evolution_adapter as ea

    captured: list[list[str]] = []
    captured_env: list[dict] = []

    def _fake_cli(cmd, *, env, timeout_s, attempts=3):
        captured.append(list(cmd))
        captured_env.append(dict(env))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    def _fake_run(cmd, **kw):  # commit
        captured.append(list(cmd))
        if "env" in kw:
            captured_env.append(dict(kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ea, "_run_evo_cli_with_retries", _fake_cli)
    monkeypatch.setattr(ea.subprocess, "run", _fake_run)
    monkeypatch.setattr(ea, "_archive_stale_reflections", lambda skill_name: [])
    monkeypatch.setattr(ea, "_restore_archived_reflections", lambda archived: None)
    monkeypatch.setattr(ea, "_current_skill_version", lambda s: "v1")

    ea.evolve_skills_one_round(
        skill_to_failing_tasks={"test-skill": ["task-name"]},
        model_id="openrouter/anthropic/claude-sonnet-4",
        api_base="https://openrouter.ai/api/v1",
        api_key="k",
        manifest_path=tmp_path / "manifest.json",
        trace_model_id="openai/Qwen/Qwen3.6-35B",
    )

    assert captured, "no CLI commands were built"
    for cmd in captured:
        assert "--limit" not in cmd, f"adapter must not append --limit: {cmd}"
    for env in captured_env:
        assert not any(k for k in env if "LIMIT" in k.upper() and "REFLECT" in k.upper()), (
            f"no EVO_REFLECT_*LIMIT env var allowed: {sorted(env)}"
        )
        # Be strict: no limit-bearing env at all introduced by the adapter.
        assert "EVO_REFLECT_LIMIT" not in env
        assert "EVO_REFLECT_TASK_LIMIT" not in env

    # Source-code level guard: the adapter never references a reflect limit.
    src = Path(ea.__file__).read_text(encoding="utf-8")
    assert "EVO_REFLECT_LIMIT" not in src
    assert "EVO_REFLECT_TASK_LIMIT" not in src
    assert '"--limit"' not in src


# ---------------------------------------------------------------------------
# Part B — cross-source trace-model fix
# ---------------------------------------------------------------------------


def test_cross_source_reflect_covers_all_models_no_out_of_cell(tmp_path: Path):
    """With trace_model=None, failing traces from BOTH source models for the
    cell's task are reflected; the --task filter still excludes an
    out-of-cell task's trace (no contamination)."""
    skill = _make_skill(tmp_path)
    # Cell task "cell-task" produced by two source models (cross-source row).
    _save_failed_trace(skill, "a-gptoss", task="cell-task", model="gpt-oss-120b")
    _save_failed_trace(skill, "b-gemma", task="cell-task", model="gemma-7b")
    _save_failed_trace(skill, "c-llama", task="cell-task", model="Llama-3.1-8B")
    # An out-of-cell task trace that must NOT be pulled in.
    _save_failed_trace(skill, "x-other", task="other-task", model="gpt-oss-120b")

    backend = MockBackend(default_response=_VALID_REFLECTION)

    reflections = reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="cell-task",  # cell scoping (per failing task)
        trace_model=None,  # cross-source ⇒ adapter omits --trace-model
        strict=True,
    )

    ts = TraceStore(skill)
    reflected_ids = {d.name for d in ts.traces_dir.iterdir() if (d / "reflection.json").exists()}
    # Every in-cell source model's failure reflected.
    assert reflected_ids == {"a-gptoss", "b-gemma", "c-llama"}, reflected_ids
    assert len(reflections) == 3
    # Out-of-cell task trace NOT reflected (no over-broadening).
    assert "x-other" not in reflected_ids
    assert not (ts.traces_dir / "x-other" / "reflection.json").exists()


def test_single_source_trace_model_still_filters(tmp_path: Path):
    """Single-source rows keep the substring filter behaviour unchanged."""
    skill = _make_skill(tmp_path)
    _save_failed_trace(skill, "a-gptoss", task="cell-task", model="gpt-oss-120b")
    _save_failed_trace(skill, "b-gemma", task="cell-task", model="gemma-7b")

    backend = MockBackend(default_response=_VALID_REFLECTION)

    reflections = reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="cell-task",
        trace_model="gpt-oss",  # single-source ⇒ scalar substring filter
        strict=True,
    )
    ts = TraceStore(skill)
    reflected_ids = {d.name for d in ts.traces_dir.iterdir() if (d / "reflection.json").exists()}
    assert reflected_ids == {"a-gptoss"}
    assert len(reflections) == 1


def test_adapter_omits_trace_model_when_cross_source(tmp_path: Path, monkeypatch):
    """`evolve_skills_one_round(trace_model_id=None)` ⇒ no --trace-model in
    reflect/rewrite argv (cross-source cell reflects all source models)."""
    from evolution.adapters import evolution_adapter as ea

    captured: list[list[str]] = []

    def _fake_cli(cmd, *, env, timeout_s, attempts=3):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    def _fake_run(cmd, **kw):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ea, "_run_evo_cli_with_retries", _fake_cli)
    monkeypatch.setattr(ea.subprocess, "run", _fake_run)
    monkeypatch.setattr(ea, "_archive_stale_reflections", lambda skill_name: [])
    monkeypatch.setattr(ea, "_restore_archived_reflections", lambda archived: None)
    monkeypatch.setattr(ea, "_current_skill_version", lambda s: "v1")

    ea.evolve_skills_one_round(
        skill_to_failing_tasks={"test-skill": ["cell-task"]},
        model_id="openrouter/anthropic/claude-sonnet-4",
        api_base="https://openrouter.ai/api/v1",
        api_key="k",
        manifest_path=tmp_path / "manifest.json",
        trace_model_id=None,  # cross-source cell
    )

    assert captured
    for cmd in captured:
        assert "--trace-model" not in cmd, (
            f"cross-source cell must omit --trace-model so all in-cell source models reflect: {cmd}"
        )


def test_produce_spec_picks_none_trace_model_for_cross_source():
    """The cross-source decision logic: distinct source models across a
    cell's selected failing traces > 1 ⇒ trace_model_id is None."""
    from evolution.adapters.evolution_adapter import _select_all_failing_traces

    class _R:
        def __init__(self, task_id, success, pass_rate, total_tests, smid, skills):
            self.task_id = task_id
            self.success = success
            self.pass_rate = pass_rate
            self.total_tests = total_tests
            self.source_model_id = smid
            self.skills_used = skills

    results = [
        _R("cell-task", False, 0.0, 1, "gpt-oss-120b", ["test-skill"]),
        _R("cell-task", False, 0.0, 1, "gemma-7b", ["test-skill"]),
        _R("cell-task", False, 0.0, 1, "Llama-3.1-8B", ["test-skill"]),
    ]
    selected = _select_all_failing_traces(results, "test-skill")
    distinct_models = {mid for _t, mid in selected if mid is not None}
    assert distinct_models == {"gpt-oss-120b", "gemma-7b", "Llama-3.1-8B"}
    # The adapter sets trace_model_id=None whenever this set has >1 member.
    trace_model_id = None if len(distinct_models) > 1 else next(iter(distinct_models))
    assert trace_model_id is None

    # Single-source sanity: one model ⇒ that model is kept.
    one = [_R("cell-task", False, 0.0, 1, "gpt-oss-120b", ["test-skill"])]
    sel1 = _select_all_failing_traces(one, "test-skill")
    dm1 = {mid for _t, mid in sel1 if mid is not None}
    tm1 = None if len(dm1) > 1 else next(iter(dm1))
    assert tm1 == "gpt-oss-120b"
