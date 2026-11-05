"""Unit tests for iterative evolution loop primitives."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
EVO_SRC = REPO_ROOT / "src"
EXP = REPO_ROOT / "experiments"
for p in (str(EVO_SRC), str(EXP)):
    if p not in sys.path:
        sys.path.insert(0, p)

from iterative_evolution.canonical_skill import (  # noqa: E402
    read_canonical,
    seed_v0,
    snapshot_version,
    write_canonical,
)
from iterative_evolution.trace_bundle import (  # noqa: E402
    SCHEMA,
    bundle_from_solve_result,
    render_traces_digest,
    write_bundle,
)
from iterative_evolution.verification import (  # noqa: E402
    hermes_events_gate,
    path_unity_gate,
    split_gate,
)

SAMPLE_SKILL = """---
name: pipelines
description: test skill
---

# Procedure

Do the thing step by step.
"""


def test_trace_bundle_schema_fields():
    cache = Path(tempfile.mkdtemp())
    solve = {
        "passed": 1,
        "total": 2,
        "pass_rate": 0.5,
        "outcome": "ok",
        "elapsed_s": 1.0,
    }
    (cache / "prompt.md").write_text("prompt text", encoding="utf-8")
    (cache / "response.md").write_text("response text", encoding="utf-8")
    (cache / "solve.py").write_text("print(1)", encoding="utf-8")
    (cache / "pytest.log").write_text("FAILED test_x", encoding="utf-8")
    bundle = bundle_from_solve_result(
        round_idx=0,
        task_id="t1",
        role="de",
        skill_id="pipelines",
        skill_version="v0",
        skill_sha256="abc",
        instruction="do task",
        solve_result=solve,
        cache_dir=cache,
    )
    assert "prompt" in bundle
    path = write_bundle(cache / "bundle.json", bundle)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema"] == SCHEMA
    assert loaded["prompt"] == "prompt text"
    assert loaded["code"] == "print(1)"
    assert "FAILED" in loaded["pytest_full"]


def test_editor_digest_redacts_runtime_and_train_literals(tmp_path: Path):
    cache = tmp_path / "cache"
    ws = cache / "workspace"
    (ws / "environment" / "data").mkdir(parents=True)
    (ws / "environment" / "data" / "timing_log.json").write_text(
        json.dumps({"lint": {"avg_duration_minutes": 2, "p95_duration_minutes": 3}}),
        encoding="utf-8",
    )
    (ws / "environment" / "data" / "workflow.yml").write_text(
        "jobs:\n  lint:\n    steps: []\n",
        encoding="utf-8",
    )
    (cache / "solve.py").write_text(
        "open('../environment/data/timing_log.json')\nprint('avg_duration_minutes')",
        encoding="utf-8",
    )
    (cache / "solve_run.log").write_text(
        "TypeError: unsupported operand for avg_duration_minutes", encoding="utf-8"
    )
    (cache / "pytest_full.log").write_text(
        "output/optimized_workflow.yml not found", encoding="utf-8"
    )
    bundle = bundle_from_solve_result(
        round_idx=0,
        task_id="sb-bench-m2-ci-optimization",
        role="infra",
        skill_id="pipelines",
        skill_version="v0",
        skill_sha256="abc",
        instruction="Read environment/data/timing_log.json and write optimized_workflow.yml",
        solve_result={"passed": 0, "total_tests": 1, "pass_rate": 0.0, "outcome": "ok"},
        cache_dir=cache,
    )
    digest = render_traces_digest([bundle], skill_body=SAMPLE_SKILL)
    for literal in (
        "environment/data",
        "output/",
        "timing_log.json",
        "workflow.yml",
        "optimized_workflow.yml",
        "avg_duration_minutes",
    ):
        assert literal not in digest
    assert "TypeError" in digest
    assert "<task-file>" in digest
    assert "solver_read_path_from_output_cwd" in digest
    assert "../<task-input-path>" in digest
    assert "<average-duration-field>" in digest or "<task-schema-field>" in digest


def test_unredacted_editor_digest_explains_solver_cwd_paths(tmp_path: Path):
    cache = tmp_path / "cache"
    ws = cache / "workspace"
    (ws / "environment" / "data").mkdir(parents=True)
    (ws / "environment" / "data" / "workflow.yml").write_text("jobs: {}\n", encoding="utf-8")
    (ws / "output").mkdir()
    (cache / "solve.py").write_text(
        "open('environment/data/workflow.yml')\n",
        encoding="utf-8",
    )
    (cache / "pytest_full.log").write_text(
        "FileNotFoundError: environment/data/workflow.yml",
        encoding="utf-8",
    )
    bundle = bundle_from_solve_result(
        round_idx=0,
        task_id="sb-bench-m2-ci-optimization",
        role="infra",
        skill_id="pipelines",
        skill_version="v0",
        skill_sha256="abc",
        instruction="Read environment/data/workflow.yml and write optimized_workflow.yml",
        solve_result={"passed": 0, "total_tests": 1, "pass_rate": 0.0, "outcome": "ok"},
        cache_dir=cache,
    )
    digest = render_traces_digest(
        [bundle],
        skill_body=SAMPLE_SKILL,
        redact=False,
        blind=False,
    )
    assert "Runtime Path Contract" in digest
    assert "cwd set to `<workspace>/output`" in digest
    assert '"path": "environment/data/workflow.yml"' in digest
    assert '"solver_read_path_from_output_cwd": "../environment/data/workflow.yml"' in digest
    assert "Do not teach workspace-root input paths" in digest


def test_native_editor_digest_uses_workspace_root_contract(tmp_path: Path):
    cache = tmp_path / "cache"
    ws = cache / "workspace"
    (ws / "environment" / "data").mkdir(parents=True)
    (ws / "environment" / "data" / "workflow.yml").write_text("jobs: {}\n", encoding="utf-8")
    (cache / "pytest_full.log").write_text(
        "output/optimized_workflow.yml not found",
        encoding="utf-8",
    )
    bundle = bundle_from_solve_result(
        round_idx=0,
        task_id="sb-bench-m2-ci-optimization",
        role="infra",
        skill_id="pipelines",
        skill_version="v0",
        skill_sha256="abc",
        instruction="Read environment/data/workflow.yml and write optimized_workflow.yml",
        solve_result={"passed": 0, "total_tests": 1, "pass_rate": 0.0, "outcome": "ok"},
        cache_dir=cache,
    )
    bundle["runtime_contract"] = {"kind": "native_workspace_root", "framework": "memento"}
    digest = render_traces_digest(
        [bundle],
        skill_body=SAMPLE_SKILL,
        redact=False,
        blind=False,
    )
    assert "native framework runtime" in digest
    assert "cwd set to the workspace root" in digest
    assert '"solver_read_path_from_workspace_root": "environment/data/workflow.yml"' in digest
    assert "`output/...` for writes" in digest
    assert "cwd set to `<workspace>/output`" not in digest


def test_portable_skill_sanitizer_rewrites_harness_lines():
    from iterative_evolution.skill_editors import finalize_skill_md_with_portability

    bundle = {
        "instruction": """# GitHub Actions CI Pipeline Optimization

Read environment/data/timing_log.json and write optimized_workflow.yml

```json
{
  "original_duration_minutes": 30,
  "optimized_duration_minutes": 20,
  "speedup_pct": 33.3,
  "dependency_graph": {}
}
```
""",
        "workspace_file_previews": [
            {
                "path": "environment/data/timing_log.json",
                "preview": json.dumps({"lint": {"avg_duration_minutes": 2}}),
            }
        ],
    }
    candidate = (
        SAMPLE_SKILL
        + """
- Read environment/data/timing_log.json.
- Sum avg_duration_minutes values.
- Write output/optimized_workflow.yml.
**When to use**: similar to the training example "GitHub Actions CI Pipeline Optimization".
- `original_duration`, `optimized_duration`, and `speedup_percent` are exact field names.
These exact field names must be used in the JSON report for the verifier to succeed.
```json
{
  "original_duration_minutes": 30,
  "optimized_duration_minutes": 20,
  "speedup_pct": 33.3,
  "dependency_graph": {}
}
```
"""
    )
    finalized, leak, telemetry = finalize_skill_md_with_portability(
        original_full=SAMPLE_SKILL,
        candidate_full=candidate,
        corpus=bundle["instruction"],
        bundles=[bundle],
    )
    for literal in (
        "environment/data",
        "timing_log.json",
        "optimized_workflow.yml",
        "avg_duration_minutes",
        "output/",
        "original_duration_minutes",
        "optimized_duration_minutes",
        "speedup_pct",
        "dependency_graph",
        "GitHub Actions CI Pipeline Optimization",
        "original_duration",
        "optimized_duration",
        "speedup_percent",
        "verifier",
    ):
        assert literal not in finalized
    assert "structured JSON/YAML" in finalized
    assert telemetry["portable_findings_count"] >= 3
    assert leak >= telemetry["portable_findings_count"]


def test_portable_skill_sanitizer_strips_nested_frontmatter():
    from iterative_evolution.skill_editors import finalize_skill_md_with_portability

    candidate = (
        SAMPLE_SKILL
        + """
# Distilled workflow

---
name: copied-skill
description: nested copy
version: "1.0.0"
---

Use the workflow steps.
"""
    )
    finalized, _leak, telemetry = finalize_skill_md_with_portability(
        original_full=SAMPLE_SKILL,
        candidate_full=candidate,
        corpus="",
        bundles=[],
    )
    assert "name: copied-skill" not in finalized
    assert "description: nested copy" not in finalized
    assert "Use the workflow steps." in finalized
    assert any(f["kind"] == "nested_frontmatter" for f in telemetry["portable_findings"])


def test_portable_skill_sanitizer_restores_missing_original_sections():
    from iterative_evolution.skill_editors import finalize_skill_md_with_portability

    original = (
        SAMPLE_SKILL
        + """
## Transformations

Keep reusable transform guidance.

## Destinations

Keep reusable destination guidance.
"""
    )
    candidate = (
        SAMPLE_SKILL
        + """
## Destinations

Updated destination guidance.
"""
    )
    finalized, _leak, telemetry = finalize_skill_md_with_portability(
        original_full=original,
        candidate_full=candidate,
        corpus="",
        bundles=[],
    )
    assert "## Transformations" in finalized
    assert "Keep reusable transform guidance." in finalized
    assert "Updated destination guidance." in finalized
    assert telemetry["restored_original_sections"] == ["## Transformations"]


def test_hermes_wrapped_rejects_create_and_foreign_name():
    from evolution.adapters.hermes_adapter import _install_skill_manage_logger

    with tempfile.TemporaryDirectory() as td:
        events = Path(td) / "events.jsonl"
        fake_orig = mock.Mock(return_value='{"success": true}')
        import evolution.adapters.hermes_adapter as ha

        ha._skill_manage_orig = fake_orig
        _install_skill_manage_logger(events, allowed_skill_id="pipelines")
        from tools import skill_manager_tool  # type: ignore

        wrapped = skill_manager_tool.skill_manage
        blocked_create = json.loads(wrapped(action="create", name="pipelines", content="x"))
        assert blocked_create["success"] is False
        blocked_name = json.loads(wrapped(action="patch", name="other-skill", content="x"))
        assert blocked_name["success"] is False
        events_seen = [
            json.loads(line)
            for line in events.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [e["blocked"] for e in events_seen] == [True, True]
        fake_orig.reset_mock()
        allowed = json.loads(wrapped(action="patch", name="pipelines", content="x"))
        assert allowed["success"] is True
        fake_orig.assert_called_once()


def test_hermes_edit_disables_terminal_and_task_venv(tmp_path: Path):
    from evolution.adapters.hermes_adapter import edit_skill_from_traces

    captured: dict = {}

    def fake_run_one_train_task(**kwargs):
        captured.update(kwargs)
        return {"task_id": kwargs["task_id"], "ok": True, "elapsed_s": 0.0}

    with mock.patch(
        "evolution.adapters.hermes_adapter._run_one_train_task",
        side_effect=fake_run_one_train_task,
    ):
        body, telemetry = edit_skill_from_traces(
            skill_id="pipelines",
            skill_body=SAMPLE_SKILL,
            traces_digest="digest",
            bundles=[],
            run_dir=tmp_path,
            editor_cfg={"id": "openai/gpt-oss-120b", "api_base": "http://localhost:30004/v1"},
            api_key="EMPTY",
            round_idx=0,
        )
    assert "name: pipelines" in body
    assert telemetry["editor"] == "hermes"
    assert captured["make_task_venv"] is False
    assert captured["disable_terminal"] is True


class _FakeAgentBackend:
    """Stand-in for AgentBackend that edits work_dir/SKILL.md in place (no real CLI)."""

    def __init__(self, model: str):
        self.model_label = model
        self.last_usage = {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "tool_turns": 1,
            "wall_time_ms": 7,
        }
        self.last_codex_diagnostics = {"returncode": 0, "stderr_tail": ""}

    async def complete_file_edit(
        self, work_dir, prompt, system=None, *, timeout_s=None, events_out=None
    ):
        if events_out is not None:
            # benign tool-call stream (a Read of SKILL.md + an Edit) -> audit should be clean
            Path(events_out).write_text(
                '{"type":"tool_use","name":"Read","input":{"file_path":"SKILL.md"}}\n'
                '{"type":"result","result":"ok"}\n',
                encoding="utf-8",
            )
        p = Path(work_dir) / "SKILL.md"
        p.write_text(
            p.read_text(encoding="utf-8") + "\n## Agent Fix\n\nExtract scalars before summing.\n",
            encoding="utf-8",
        )
        return None


@pytest.mark.parametrize("framework", ["codex", "claude"])
def test_agent_cli_framework_file_edit_dispatch(tmp_path: Path, monkeypatch, framework: str):
    from iterative_evolution.skill_editors import edit_skill

    monkeypatch.setattr(
        "evolution.adapters.agent_cli_adapter.get_backend",
        lambda model, **_kw: _FakeAgentBackend(model),
    )
    bundle = {
        "instruction": "read timing records",
        "passed": 0,
        "total": 1,
        "pass_rate": 0.0,
        "failure_categories": ["TypeError"],
        "stdout": "TypeError: unsupported operand type(s)",
    }
    res = edit_skill(
        framework,
        skill_id="pipelines",
        skill_body=SAMPLE_SKILL,
        bundles=[bundle],
        run_dir=tmp_path,
        editor_cfg={},
        api_key="EMPTY",
        round_idx=0,
        role="de",
    )
    assert res.changed is True
    assert "Agent Fix" in res.new_body
    assert res.telemetry["editor"] == framework
    assert res.telemetry["ok"] is True
    assert res.telemetry["tokens_out"] == 22
    assert (tmp_path / f"{framework}_edit_workspace" / "round0" / "SKILL.md").is_file()
    # tool-call stream captured outside the agent cwd + audited clean
    fa = res.telemetry["fs_audit"]
    assert fa["events_captured"] is True and fa["clean"] is True
    assert (tmp_path / f"{framework}_edit_audit" / "round0.events").is_file()


def test_agent_fs_audit_flags_oracle_access(tmp_path: Path):
    from evolution.adapters.agent_cli_adapter import _audit_agent_events

    ev = tmp_path / "ev.events"
    ev.write_text(
        '{"type":"tool_use","name":"Read","input":{"file_path":"SKILL.md"}}\n'
        '{"type":"command_execution","command":"cat ../../datasets/AFTER/tasks/de/x/tests/test_outputs.py"}\n'
        '{"type":"tool_use","name":"Read","input":{"file_path":"/x/skills/xlsx/SKILL.md"}}\n',
        encoding="utf-8",
    )
    a = _audit_agent_events(ev, tmp_path)  # work_dir = tmp_path: SKILL.md is in-workspace
    assert a["events_captured"] is True
    assert a["clean"] is False
    # the dataset/verifier read (../ escape) and the other-skill read (abs outside) are flagged;
    # the in-workspace SKILL.md is not
    joined = " ".join(a["suspicious_accesses"])
    assert "datasets/AFTER" in joined and "skills/xlsx" in joined
    assert a["n_events"] == 3

    clean = tmp_path / "clean.events"
    clean.write_text(
        '{"type":"tool_use","name":"Edit","input":{"file_path":"SKILL.md"}}\n'
        '{"type":"command_execution","command":"cat tasks/de_x/instruction.md"}\n',
        encoding="utf-8",
    )
    assert _audit_agent_events(clean, tmp_path)["clean"] is True


def test_hermes_terminal_env_pins_sandbox_and_cache_dirs(tmp_path: Path):
    from evolution.adapters.workspace_guard import configure_hermes_terminal_env

    workspace = tmp_path / "workspace"
    run_home = tmp_path / "hermes_home"
    with mock.patch.dict("os.environ", {}, clear=True):
        configure_hermes_terminal_env(
            workspace_dir=workspace,
            run_home=run_home,
            make_task_venv=False,
        )
        assert not (workspace / ".venv").exists()
        assert Path(__import__("os").environ["TERMINAL_SANDBOX_DIR"]).is_relative_to(run_home)
        assert Path(__import__("os").environ["TERMINAL_SCRATCH_DIR"]).is_relative_to(run_home)
        assert Path(__import__("os").environ["APPTAINER_CACHEDIR"]).is_relative_to(run_home)
        assert Path(__import__("os").environ["XDG_CACHE_HOME"]).is_relative_to(run_home)
        assert Path(__import__("os").environ["TMPDIR"]).is_relative_to(run_home)
        assert __import__("os").environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_evoskill_edit_returns_anchor_path(tmp_path: Path):
    from evolution.adapters.evoskill_adapter import edit_skill_from_traces

    bundles = [
        {
            "task_id": "t1",
            "passed": 0,
            "total": 1,
            "pass_rate": 0.0,
            "failure_categories": ["fail"],
            "instruction": "solve",
        }
    ]

    async def _fake_rewrite(*args, **kwargs):
        return SAMPLE_SKILL + "\n# improved\n"

    with (
        mock.patch("evolution.llm.litellm_backend.LiteLLMBackend", return_value=object()),
        mock.patch("evolution.evolution.reflect.rewrite_skill", side_effect=_fake_rewrite),
        mock.patch.dict("os.environ", {"EVO_EVOSKILL_ALLOW_EVOLUTION_REWRITE_FALLBACK": "1"}),
    ):
        body, telemetry = edit_skill_from_traces(
            skill_id="pipelines",
            skill_body=SAMPLE_SKILL,
            traces_digest="digest",
            bundles=bundles,
            run_dir=tmp_path,
            editor_cfg={"id": "mock", "api_base": "http://localhost"},
            api_key="EMPTY",
        )
    assert "pipelines" in body
    assert telemetry.get("anchor_skill_id") == "pipelines"
    assert telemetry.get("import_mode") == "evolution_rewrite_fallback"
    assert "evoskill-" not in body


def test_eval_lane_policy_marks_evoskill_native_primary():
    from iterative_evolution.eval_lanes import (
        CANONICAL_EXPORT,
        NATIVE_RUNTIME,
        primary_eval_lane,
        supported_eval_lanes,
        supports_native_runtime,
    )

    assert primary_eval_lane("hermes") == CANONICAL_EXPORT
    assert primary_eval_lane("evolution") == CANONICAL_EXPORT
    assert primary_eval_lane("codex") == CANONICAL_EXPORT
    assert primary_eval_lane("claude") == CANONICAL_EXPORT
    assert primary_eval_lane("evoskill") == NATIVE_RUNTIME
    assert primary_eval_lane("memento") == NATIVE_RUNTIME
    assert supports_native_runtime("evoskill") is True
    assert supports_native_runtime("codex") is False
    assert supports_native_runtime("claude") is False
    assert supported_eval_lanes("codex") == (CANONICAL_EXPORT,)
    assert supported_eval_lanes("evoskill") == (CANONICAL_EXPORT, NATIVE_RUNTIME)


def test_evoskill_native_eval_injects_full_skill_set(tmp_path: Path):
    from iterative_evolution.eval_lanes import NATIVE_RUNTIME
    from iterative_evolution.native_eval import LABEL_EVOSKILL, eval_native

    state = tmp_path / "state"
    for name in ("pipelines", "helper"):
        d = state / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            SAMPLE_SKILL.replace("pipelines", name, 1),
            encoding="utf-8",
        )

    captured = {}

    def _fake_run_evoskill_skill_dir_test(**kwargs):
        captured.update(kwargs)
        return {"outcome": "ok", "pass_rate": 1.0, "native_metrics": {"existing": True}}

    with mock.patch(
        "iterative_evolution.native_eval.run_evoskill_skill_dir_test",
        side_effect=_fake_run_evoskill_skill_dir_test,
    ):
        res = eval_native(
            framework="evoskill",
            task_dir=tmp_path / "task",
            instruction="do it",
            cache_dir=tmp_path / "cache",
            solver_cfg={"id": "mock", "api_base": "http://localhost"},
            api_key="EMPTY",
            post_train_state=state,
        )

    assert captured["skills_dir"] == state
    assert captured["native_metrics_extra"]["skill_count"] == 2
    assert res["eval_label"] == LABEL_EVOSKILL
    assert res["eval_lane"] == NATIVE_RUNTIME
    assert res["native_metrics"]["skill_count"] == 2


def test_evoskill_native_eval_can_run_best_program_repo(tmp_path: Path):
    from iterative_evolution.eval_lanes import NATIVE_RUNTIME
    from iterative_evolution.native_eval import LABEL_EVOSKILL_AGENT, eval_native

    repo = tmp_path / "evoskill_repo"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    captured = {}

    def _fake_run_evoskill_test(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "ok",
            "pass_rate": 0.75,
            "native_metrics": {"branch": "program/iter-skill-1"},
        }

    with mock.patch(
        "iterative_evolution.native_eval.run_evoskill_test",
        side_effect=_fake_run_evoskill_test,
    ):
        res = eval_native(
            framework="evoskill",
            task_dir=tmp_path / "task",
            instruction="do it",
            cache_dir=tmp_path / "cache",
            solver_cfg={"id": "mock", "api_base": "http://localhost"},
            api_key="EMPTY",
            post_train_state=state,
            evoskill_repo_dir=repo,
            evoskill_condition="best_program",
        )

    assert captured["condition"] == "best_program"
    assert captured["repo_dir"] == repo
    assert res["eval_label"] == LABEL_EVOSKILL_AGENT
    assert res["eval_lane"] == NATIVE_RUNTIME
    assert res["native_metrics"]["native_repo_dir"] == str(repo)


def test_evoskill_repo_dir_for_version_maps_vn_to_previous_round(tmp_path: Path):
    from iterative_evolution.loop import _evoskill_repo_dir_for_version

    repo = tmp_path / "evoskill_repo" / "round0"
    repo.mkdir(parents=True)
    assert _evoskill_repo_dir_for_version(tmp_path, "v0") is None
    assert _evoskill_repo_dir_for_version(tmp_path, "v1") == repo
    assert _evoskill_repo_dir_for_version(tmp_path, "v2") is None


def test_run_cell_rounds_one_edits_then_solves_v1(tmp_path: Path):
    from iterative_evolution import loop
    from iterative_evolution.skill_editors import EditResult

    corpus = tmp_path / "corpus"
    task_dir = corpus / "tasks" / "infra" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("solve task", encoding="utf-8")
    run_dir = tmp_path / "run"
    solve_versions: list[str] = []

    def _fake_seed_v0(**kwargs):
        loop.write_canonical(kwargs["run_dir"], kwargs["skill_id"], SAMPLE_SKILL)
        loop.snapshot_version(kwargs["run_dir"], kwargs["skill_id"], "v0")
        return {"version": "v0"}

    def _fake_solve_task(**kwargs):
        solve_versions.append(kwargs["skill_version"])
        cache = (
            kwargs["run_dir"] / "solver_cache" / f"round{kwargs['round_idx']}" / kwargs["task_id"]
        )
        cache.mkdir(parents=True, exist_ok=True)
        bundle = {
            "round": kwargs["round_idx"],
            "task_id": kwargs["task_id"],
            "role": kwargs["role"],
            "skill_id": kwargs["skill_id"],
            "skill_version": kwargs["skill_version"],
            "instruction": kwargs["instruction"],
            "passed": 0,
            "total": 1,
            "pass_rate": 0.0,
            "failure_categories": ["fail"],
            "solve_result": {"outcome": "ok"},
        }
        loop.write_bundle(
            loop.bundle_path(kwargs["run_dir"], kwargs["round_idx"], kwargs["task_id"]), bundle
        )
        return {
            "outcome": "ok",
            "pass_rate": 0.0,
            "passed": 0,
            "failed": 1,
            "total_tests": 1,
            "cache_dir": str(cache),
        }

    with (
        mock.patch("iterative_evolution.loop.seed_v0", side_effect=_fake_seed_v0),
        mock.patch("iterative_evolution.loop.solve_task", side_effect=_fake_solve_task),
        mock.patch(
            "iterative_evolution.loop.edit_skill",
            return_value=EditResult(
                new_body=SAMPLE_SKILL + "\n## Fix\n\nUse the trace.\n",
                changed=True,
                telemetry={"editor": "mock"},
            ),
        ) as edit,
        mock.patch(
            "iterative_evolution.loop.pick_best_version",
            return_value={
                "selection": "best-on-train",
                "eval_lane": "canonical_export",
                "scores": [],
                "best": {"version": "v1", "mean_pass_rate": 1.0},
            },
        ),
    ):
        summary = loop.run_cell(
            framework="evolution",
            skill_id="pipelines",
            role="infra",
            train_ids=["t1"],
            test_ids=[],
            n_rounds=1,
            solver_cfg={"id": "mock", "api_base": "http://localhost"},
            editor_cfg={},
            api_key="EMPTY",
            run_dir=run_dir,
            corpus_root=corpus,
        )

    assert solve_versions == ["v0", "v1"]
    assert edit.call_count == 1
    assert summary["best_version"] == "v1"


def test_evolution_single_task_editor_uses_full_trace_digest(tmp_path: Path):
    from iterative_evolution.skill_editors import edit_skill

    bundle = {
        "instruction": "read timing records",
        "passed": 0,
        "total": 1,
        "pass_rate": 0.0,
        "failure_categories": ["TypeError"],
        "stdout": "TypeError: unsupported operand type(s) for +: 'int' and 'dict'",
    }
    candidate = SAMPLE_SKILL + "\n## Trace Fix\n\nExtract nested duration values before summing.\n"

    with (
        mock.patch(
            "iterative_evolution.skill_editors.rewrite_skill_from_trace_digest",
            return_value=(candidate, {"editor": "evolution_trace_digest_rewrite"}),
        ) as direct,
        mock.patch(
            "iterative_evolution.skill_editors.rewrite_skill_from_traces",
            side_effect=AssertionError("single-task editor must not use lossy reflections"),
        ),
    ):
        res = edit_skill(
            "evolution",
            skill_id="pipelines",
            skill_body=SAMPLE_SKILL,
            bundles=[bundle],
            run_dir=tmp_path,
            editor_cfg={"single_task_trace_fidelity": True},
            api_key="EMPTY",
        )

    assert res.changed is True
    assert res.telemetry["editor"] == "evolution_trace_digest_rewrite"
    assert "Trace Fix" in res.new_body
    assert "TypeError" in direct.call_args.kwargs["traces_digest"]


def test_evoskill_bridge_train_scorer_uses_native_skill_dir(tmp_path: Path):
    from _shared.evoskill_bridge import NativeEvoSkillScorerAgent, TaskRef

    repo = tmp_path / "repo"
    skills = repo / ".claude" / "skills" / "pipelines"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(SAMPLE_SKILL, encoding="utf-8")
    ref = TaskRef(
        task_key="task1",
        task_dir=tmp_path / "task1",
        instruction="optimize this pipeline",
    )
    agent = NativeEvoSkillScorerAgent(
        skill_names=["pipelines"],
        repo=repo,
        lookup={ref.instruction: ref},
        solver_cfg={"id": "openai/gpt-oss-20b", "api_base": "https://openrouter.ai/api/v1"},
        api_key="key",
        timeout_s=123,
        solve_root=tmp_path / "solves",
    )
    captured = {}

    def _fake_native(**kwargs):
        captured.update(kwargs)
        return {"outcome": "ok", "pass_rate": 0.8, "total_tests": 5}

    with (
        mock.patch(
            "_shared.native_framework_runners.run_evoskill_skill_dir_test",
            side_effect=_fake_native,
        ),
        mock.patch(
            "_shared.native_framework_runners.run_evolution_test",
            side_effect=AssertionError("EvoSkill train scorer must stay native"),
        ),
    ):
        res = agent._solve_blocking(ref)

    assert res["pass_rate"] == 0.8
    assert captured["skills_dir"] == repo / ".claude" / "skills"
    assert captured["solver_cfg"]["id"] == "openai/gpt-oss-20b"
    assert captured["native_metrics_extra"]["train_scorer"] == "evoskill_native_agent"


def test_seed_native_v0_supports_evoskill_skillset(tmp_path: Path):
    from iterative_evolution.loop import _seed_native_v0, native_state_version_dir

    assert _seed_native_v0(tmp_path, "evoskill", "pipelines", SAMPLE_SKILL) is True
    snap = native_state_version_dir(tmp_path, "evoskill", "v0")
    assert (snap / "pipelines" / "SKILL.md").is_file()
    assert "name: pipelines" in (snap / "pipelines" / "SKILL.md").read_text(encoding="utf-8")


def test_seed_native_v0_supports_memp_direct_policy(tmp_path: Path):
    from iterative_evolution.loop import _seed_native_v0, native_state_dir, native_state_version_dir

    assert _seed_native_v0(tmp_path, "memp", "pipelines", SAMPLE_SKILL) is True
    state = native_state_dir(tmp_path, "memp")
    snap = native_state_version_dir(tmp_path, "memp", "v0")
    assert (state / "direct" / "documents.json").is_file()
    assert (snap / "direct" / "documents.json").is_file()


def test_memp_leave_one_out_filters_direct_documents(tmp_path: Path):
    from iterative_evolution.native_eval import _apply_leave_one_out_memp

    direct = tmp_path / "direct"
    direct.mkdir()
    docs = [
        {"page_content": "drop", "metadata": {"task_id": "heldout"}},
        {"page_content": "keep", "metadata": {"task_id": "other"}},
    ]
    (direct / "documents.json").write_text(json.dumps(docs), encoding="utf-8")
    applied, matched = _apply_leave_one_out_memp(tmp_path, "heldout")
    remaining = json.loads((direct / "documents.json").read_text(encoding="utf-8"))
    assert applied is True
    assert matched is True
    assert [d["metadata"]["task_id"] for d in remaining] == ["other"]


def test_memento_harvest_anchor_only(tmp_path: Path):
    from evolution.adapters.memento_adapter import edit_skill_from_traces

    test_home = tmp_path / "native_state" / "memento"
    anchor = test_home / "memento_s" / "skills" / "pipelines" / "SKILL.md"
    anchor.parent.mkdir(parents=True)
    anchor.write_text(SAMPLE_SKILL, encoding="utf-8")

    with mock.patch(
        "evolution.adapters.memento_adapter._invoke_memento",
        return_value=(0, "ok", "", None),
    ):
        body, telemetry = edit_skill_from_traces(
            skill_id="pipelines",
            skill_body=SAMPLE_SKILL,
            traces_digest="digest",
            bundles=[],
            run_dir=tmp_path,
            editor_cfg={},
            api_key="",
            round_idx=0,
        )
    assert "pipelines" in body
    assert telemetry.get("editor") == "memento"


def test_memento_accepts_folder_anchor_with_different_frontmatter_name():
    from evolution.adapters.memento_adapter import _extract_skill_md_candidate

    md = """```markdown
---
name: data-pipeline
description: folder anchor remains pipelines
---

# Updated skill
```"""
    extracted = _extract_skill_md_candidate(md, skill_id="pipelines")
    assert "name: data-pipeline" in extracted
    assert "# Updated skill" in extracted


def test_memento_single_task_uses_direct_fallback_when_native_edit_fails(tmp_path: Path):
    from evolution.adapters.memento_adapter import edit_skill_from_traces

    async def _fake_direct_edit(**kwargs):
        return (
            SAMPLE_SKILL + "\n## Native Runtime Fix\n\nWrite complete output files.\n",
            {"fallback_model": "mock"},
        )

    with (
        mock.patch(
            "evolution.adapters.memento_adapter._invoke_memento",
            return_value=(1, "", "timeout after 1s", None),
        ),
        mock.patch(
            "evolution.adapters.memento_adapter._direct_edit_skill_md",
            side_effect=_fake_direct_edit,
        ),
        mock.patch.dict("os.environ", {"EVO_MEMENTO_ALLOW_DIRECT_EDIT_FALLBACK": ""}),
    ):
        body, telemetry = edit_skill_from_traces(
            skill_id="pipelines",
            skill_body=SAMPLE_SKILL,
            traces_digest="TypeError plus missing output artifacts",
            bundles=[],
            run_dir=tmp_path,
            editor_cfg={"single_task_trace_fidelity": True},
            api_key="EMPTY",
            round_idx=0,
        )

    assert "Native Runtime Fix" in body
    assert telemetry["ok"] is True
    assert telemetry["fallback_used"] is True
    assert telemetry["fallback"] == "direct_skill_md_rewrite"


def test_memento_openrouter_model_id_is_not_double_prefixed(tmp_path: Path):
    from evolution.adapters.memento_adapter import write_llm_config

    config_dir = tmp_path / "memento_s"
    with mock.patch.dict(
        "os.environ",
        {
            "EVO_MEMENTO_MAX_REACT_PER_STEP": "12",
            "EVO_MEMENTO_MAX_REPLANS": "4",
        },
    ):
        write_llm_config(
            solver_model="openai/openai/gpt-oss-20b",
            solver_base="https://openrouter.ai/api/v1",
            solver_key="key",
            embedding_model="Qwen/Qwen3-Embedding-8B",
            embedding_base="http://localhost:10225/v1",
            embedding_key="EMPTY",
            config_dir=config_dir,
        )

    cfg = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    profile = cfg["llm"]["profiles"]["pilot"]
    assert profile["litellm_provider"] == "openrouter"
    assert profile["model"] == "openai/gpt-oss-20b"
    assert cfg["agent"]["max_react_per_step"] == 12
    assert cfg["agent"]["max_replans"] == 4


def test_memp_render_memory_to_skill_lossy(tmp_path: Path):
    from evolution.adapters.memp_adapter import render_memory_to_skill

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    docs = [
        {
            "page_content": "seed",
            "metadata": {
                "source": "base_skill:pipelines",
                "workflow": SAMPLE_SKILL,
            },
        },
        {
            "page_content": "x",
            "metadata": {
                "source": "trace:t1",
                "workflow": "---\nname: nested\n---\nStep 1: fetch\nStep 2: transform",
            },
        },
    ]
    (mem_dir / "documents.json").write_text(json.dumps(docs), encoding="utf-8")
    body, meta = render_memory_to_skill(memory_dir=mem_dir, skill_id="pipelines")
    assert meta.get("lossy_bridge") is True
    assert meta.get("workflow_found") is True
    assert "Step 1" in body
    assert "name: nested" not in body
    assert "name: pipelines" in body
    assert "lossy" in body


def test_path_unity_and_split_gates(tmp_path: Path):
    write_canonical(tmp_path, "pipelines", SAMPLE_SKILL)
    snapshot_version(tmp_path, "pipelines", "v0")
    sha = path_unity_gate(
        run_dir=tmp_path,
        skill_id="pipelines",
        expected_sha=__import__(
            "iterative_evolution.canonical_skill", fromlist=["sha256_text"]
        ).sha256_text(SAMPLE_SKILL),
    )
    assert sha["ok"] is True
    split = split_gate(editor_trace_task_ids={"t1", "t2"}, test_task_ids={"t3"})
    assert split["ok"] is True
    bad = split_gate(editor_trace_task_ids={"t1"}, test_task_ids={"t1"})
    assert bad["ok"] is False


def test_hermes_events_gate():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "events.jsonl"
        path.write_text(
            json.dumps({"action": "create", "name": "pipelines"}) + "\n",
            encoding="utf-8",
        )
        bad = hermes_events_gate(path, "pipelines")
        assert bad["ok"] is False
        path.write_text(
            json.dumps({"action": "patch", "name": "pipelines"}) + "\n",
            encoding="utf-8",
        )
        ok = hermes_events_gate(path, "pipelines")
        assert ok["ok"] is True


@pytest.mark.skipif(
    not (REPO_ROOT.parent / "datasets" / "AFTER" / "skills" / "pipelines").exists(),
    reason="AFTER pipelines skill seed not present",
)
def test_seed_v0_creates_versions(tmp_path: Path):
    corpus = REPO_ROOT.parent / "datasets" / "AFTER"
    meta = seed_v0(
        run_dir=tmp_path,
        corpus_root=corpus,
        skill_id="pipelines",
        source_task_ids=["sales-pivot-analysis"],
    )
    assert meta["version"] == "v0"
    body = read_canonical(tmp_path, "pipelines")
    assert "name: pipelines" in body or "pipelines" in body


# --- D5: sanitizer narrowing ------------------------------------------------


def test_sanitizer_keeps_pytest_and_output_contract_lines():
    from iterative_evolution.skill_editors import finalize_skill_md_with_portability

    candidate = (
        SAMPLE_SKILL
        + "\n- Validate your artifacts by running pytest locally.\n"
        + "- Write requested artifacts under output/ from the workspace root.\n"
    )
    finalized, _leak, telemetry = finalize_skill_md_with_portability(
        original_full=SAMPLE_SKILL,
        candidate_full=candidate,
        corpus="",
        bundles=[],
    )
    assert "pytest" in finalized
    assert "output/" in finalized
    assert telemetry["portable_findings_count"] == 0


def test_sanitizer_still_guts_harness_runtime_files():
    from iterative_evolution.skill_editors import finalize_skill_md_with_portability

    candidate = SAMPLE_SKILL + "\n- Inspect solve.sh and environment/data before running.\n"
    finalized, _leak, telemetry = finalize_skill_md_with_portability(
        original_full=SAMPLE_SKILL,
        candidate_full=candidate,
        corpus="",
        bundles=[],
    )
    assert "solve.sh" not in finalized
    assert "environment/data" not in finalized
    assert telemetry["portable_findings_count"] >= 1


# --- D6: version selection from round records --------------------------------


def _write_rounds(run_dir: Path, records: list[dict]) -> None:
    with (run_dir / "rounds.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_pick_best_version_reads_round_records_without_resolving(tmp_path: Path):
    from iterative_evolution import loop

    _write_rounds(
        tmp_path,
        [
            {
                "round": 0,
                "skill_version": "v0",
                "train_pass_rate": 0.2,
                "eval_lane": "canonical_export",
            },
            {
                "round": 1,
                "skill_version": "v1",
                "train_pass_rate": 0.8,
                "eval_lane": "canonical_export",
            },
        ],
    )
    with mock.patch.object(loop, "solve_task", side_effect=AssertionError("must not re-solve")):
        pick = loop.pick_best_version(
            skill_id="pipelines",
            run_dir=tmp_path,
            corpus_root=tmp_path,
            role="de",
            train_ids=["t1"],
            solver_cfg={},
            api_key="EMPTY",
        )
    assert pick["best"]["version"] == "v1"
    assert pick["best"]["mean_pass_rate"] == 0.8
    assert [s["version"] for s in pick["scores"]] == ["v0", "v1"]


def test_pick_best_native_version_filters_lane_and_ties_to_earliest(tmp_path: Path):
    from iterative_evolution import loop

    _write_rounds(
        tmp_path,
        [
            {
                "round": 0,
                "skill_version": "v0",
                "train_pass_rate": 0.5,
                "eval_lane": "native_runtime",
            },
            {
                "round": 1,
                "skill_version": "v1",
                "train_pass_rate": 0.5,
                "eval_lane": "native_runtime",
            },
            {
                "round": 0,
                "skill_version": "v0",
                "train_pass_rate": 0.9,
                "eval_lane": "canonical_export",
            },
        ],
    )
    with mock.patch.object(loop, "eval_native", side_effect=AssertionError("must not re-solve")):
        pick = loop.pick_best_native_version(
            framework="memento",
            run_dir=tmp_path,
            corpus_root=tmp_path,
            role="de",
            train_ids=["t1"],
            solver_cfg={},
            api_key="EMPTY",
        )
    assert pick["best"]["version"] == "v0"
    assert all(s["eval_lane"] == "native_runtime" for s in pick["scores"])


# --- D4: evoskill fallback opt-in -------------------------------------------


def test_evoskill_editor_no_silent_fallback_by_default(tmp_path: Path, monkeypatch):
    from evolution.adapters.evoskill_adapter import edit_skill_from_traces

    monkeypatch.delenv("EVO_EVOSKILL_ALLOW_EVOLUTION_REWRITE_FALLBACK", raising=False)
    with mock.patch(
        "evolution.evolution.reflect.rewrite_skill",
        side_effect=AssertionError("fallback rewriter must not run by default"),
    ):
        body, telemetry = edit_skill_from_traces(
            skill_id="pipelines",
            skill_body=SAMPLE_SKILL,
            traces_digest="digest",
            bundles=[{"task_id": "t1", "instruction": "solve"}],
            run_dir=tmp_path,
            editor_cfg={"id": "mock", "api_base": "http://localhost"},
            api_key="EMPTY",
        )
    assert body == SAMPLE_SKILL
    assert telemetry["import_mode"] == "native_evoskill_error_no_fallback"


# --- D2: evoskill v0 seed repo resolution -----------------------------------


def test_evoskill_repo_dir_v0_resolves_seed_repo(tmp_path: Path):
    from iterative_evolution.loop import _evoskill_repo_dir_for_version

    assert _evoskill_repo_dir_for_version(tmp_path, "v0") is None
    (tmp_path / "evoskill_repo" / "v0").mkdir(parents=True)
    assert _evoskill_repo_dir_for_version(tmp_path, "v0") == tmp_path / "evoskill_repo" / "v0"


# --- D3: memento edit timeout default ---------------------------------------


def test_memento_edit_timeout_default_480(tmp_path: Path, monkeypatch):
    from evolution.adapters import memento_adapter

    monkeypatch.delenv("EVO_MEMENTO_EDIT_TIMEOUT_S", raising=False)
    captured: dict = {}

    def _fake_invoke(envelope, *, home, workspace_dir, skills_dir, timeout_s):
        captured["timeout_s"] = timeout_s
        (skills_dir / "pipelines").mkdir(parents=True, exist_ok=True)
        (skills_dir / "pipelines" / "SKILL.md").write_text(SAMPLE_SKILL, encoding="utf-8")
        return 0, "", "", workspace_dir

    monkeypatch.setattr(memento_adapter, "_invoke_memento", _fake_invoke)
    monkeypatch.setattr(memento_adapter, "write_llm_config", lambda **k: None)
    memento_adapter.edit_skill_from_traces(
        skill_id="pipelines",
        skill_body=SAMPLE_SKILL,
        traces_digest="digest",
        bundles=[{"task_id": "t1", "instruction": "solve"}],
        run_dir=tmp_path,
        editor_cfg={"id": "mock", "api_base": "http://localhost"},
        api_key="EMPTY",
    )
    assert captured["timeout_s"] == 480.0


def test_memento_workspace_listing_excludes_tests(tmp_path: Path):
    from evolution.adapters.memento_adapter import _workspace_listing

    (tmp_path / "environment" / "data").mkdir(parents=True)
    (tmp_path / "environment" / "data" / "in.csv").write_text("x", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_outputs.py").write_text("x", encoding="utf-8")
    (tmp_path / "data_generator.py").write_text("x", encoding="utf-8")
    listing = _workspace_listing(tmp_path)
    assert "environment/data/in.csv" in listing
    assert "tests/" not in listing
    assert "data_generator.py" not in listing


# --- D8: hermes native solve-and-update -------------------------------------


def test_hermes_native_solve_edit_runs_train_tasks(tmp_path: Path, monkeypatch):
    from evolution.adapters import hermes_adapter

    corpus = tmp_path / "corpus"
    task_dir = corpus / "tasks" / "de" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("solve t1", encoding="utf-8")

    monkeypatch.setenv("EVO_HERMES_NATIVE_SOLVE_EDIT", "1")
    monkeypatch.setattr(hermes_adapter, "write_hermes_config", lambda **k: None)

    def _fake_seed(*, hermes_home, skill_id, body):
        (hermes_home / "skills" / skill_id).mkdir(parents=True, exist_ok=True)
        (hermes_home / "skills" / skill_id / "SKILL.md").write_text(body, encoding="utf-8")

    monkeypatch.setattr(hermes_adapter, "seed_canonical_skill", _fake_seed)
    monkeypatch.setattr(hermes_adapter, "_redact_config_api_key", lambda *a, **k: None)
    monkeypatch.setattr(
        hermes_adapter,
        "_read_skill_manage_events_with_stats",
        lambda p: ([{"action": "edit", "name": "pipelines"}], 0),
    )
    import _shared.workspace_materializer as wm

    monkeypatch.setattr(wm, "materialize_workspace", lambda *a, **k: None)
    calls: list[dict] = []

    def _fake_run(**kwargs):
        hh = kwargs["hermes_home"]
        (hh / "skills" / "pipelines").mkdir(parents=True, exist_ok=True)
        (hh / "skills" / "pipelines" / "SKILL.md").write_text(
            SAMPLE_SKILL + "\n# learned\n", encoding="utf-8"
        )
        calls.append(kwargs)
        return {"ok": True, "elapsed_s": 1.0}

    monkeypatch.setattr(hermes_adapter, "_run_one_train_task", _fake_run)

    body, telemetry = hermes_adapter.edit_skill_from_traces(
        skill_id="pipelines",
        skill_body=SAMPLE_SKILL,
        traces_digest="digest",
        bundles=[{"task_id": "de/t1", "instruction": "solve t1", "pass_rate": 0.0}],
        run_dir=tmp_path,
        editor_cfg={
            "id": "mock",
            "api_base": "http://localhost",
            "corpus_root": str(corpus),
            "role": "de",
            "train_ids": ["de/t1"],
        },
        api_key="EMPTY",
        round_idx=0,
        role="de",
    )
    assert telemetry["editor"] == "hermes_native_solve"
    assert len(calls) == 1
    assert calls[0]["disable_terminal"] is False
    assert calls[0]["allowed_skill_id"] == "pipelines"
    assert calls[0]["make_task_venv"] is True
    assert "learned" in body


def test_hermes_native_solve_edit_falls_back_without_corpus(tmp_path: Path, monkeypatch):
    from evolution.adapters import hermes_adapter

    monkeypatch.setenv("EVO_HERMES_NATIVE_SOLVE_EDIT", "1")
    monkeypatch.setattr(hermes_adapter, "write_hermes_config", lambda **k: None)

    def _fake_seed(*, hermes_home, skill_id, body):
        (hermes_home / "skills" / skill_id).mkdir(parents=True, exist_ok=True)
        (hermes_home / "skills" / skill_id / "SKILL.md").write_text(body, encoding="utf-8")

    monkeypatch.setattr(hermes_adapter, "seed_canonical_skill", _fake_seed)
    monkeypatch.setattr(hermes_adapter, "_redact_config_api_key", lambda *a, **k: None)
    monkeypatch.setattr(hermes_adapter, "_read_skill_manage_events_with_stats", lambda p: ([], 0))
    seen: dict = {}

    def _fake_run(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "elapsed_s": 1.0}

    monkeypatch.setattr(hermes_adapter, "_run_one_train_task", _fake_run)
    hermes_adapter.edit_skill_from_traces(
        skill_id="pipelines",
        skill_body=SAMPLE_SKILL,
        traces_digest="digest",
        bundles=[{"task_id": "t1", "instruction": "solve"}],
        run_dir=tmp_path,
        editor_cfg={"id": "mock", "api_base": "http://localhost"},  # no corpus_root/train_ids
        api_key="EMPTY",
        round_idx=0,
    )
    assert seen["disable_terminal"] is True  # blind digest fallback path


# --- D1: memp native executor default ---------------------------------------


def test_memp_native_executor_defaults_to_single_shot(tmp_path: Path, monkeypatch):
    import _shared.native_framework_runners as nfr

    from evolution.adapters import memp_adapter

    monkeypatch.delenv("EVO_MEMP_NATIVE_EXECUTOR", raising=False)

    # Stub the heavy ProcedureMem import + memory load.
    mem_mod = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "ProcedureMem", mock.MagicMock())
    monkeypatch.setitem(sys.modules, "ProcedureMem.memory", mem_mod)

    class _Doc:
        metadata = {"workflow": "WF-1"}
        page_content = ""

    class _Mem:
        documents = [object()]

        def retrieve(self, q):
            return [(_Doc(), 0.9)]

    monkeypatch.setattr(memp_adapter, "install_monkeypatches", lambda **k: None)
    monkeypatch.setattr(memp_adapter, "_load_memory_from_dir", lambda *a, **k: _Mem())
    monkeypatch.setattr(
        memp_adapter,
        "run_episode",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("episode")),
    )

    task_dir = tmp_path / "task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "tests" / "test_outputs.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (task_dir / "instruction.md").write_text("solve", encoding="utf-8")
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    (mem_dir / "documents.json").write_text("[]", encoding="utf-8")

    captured: dict = {}

    def _fake_run_evolution_test(**kwargs):
        captured.update(kwargs)
        return {"outcome": "ok", "pass_rate": 1.0}

    monkeypatch.setattr(nfr, "run_evolution_test", _fake_run_evolution_test)
    monkeypatch.setattr(nfr, "_setup_workspace", lambda *a, **k: {})

    res = nfr.run_memp_test(
        task_dir=task_dir,
        raw_sbbench_path=task_dir,
        instruction="solve",
        condition="post_train",
        cache_dir=tmp_path / "cache",
        solver_cfg={"id": "mock", "api_base": "http://localhost", "served_id_hint": "mock"},
        reflector_cfg={"id": "mock", "api_base": "http://localhost"},
        embedding_cfg={"id": "emb", "api_base": "http://localhost"},
        api_key="EMPTY",
        refl_key="EMPTY",
        emb_key="EMPTY",
        post_train_memory_dir=mem_dir,
    )
    assert captured["post_train_skill_bodies"] == ["WF-1"]
    assert res["native_metrics"]["memp_executor"] == "single_shot"


# --- Part B cleanup guards: shared envelopes + unchanged-edit state restore ---


def test_edit_envelopes_share_anchor_rule():
    from evolution.adapters import hermes_adapter, memento_adapter
    from evolution.adapters._envelopes import ANCHOR_EDIT_RULE, GENERIC_PROCEDURE_RULE

    lead = ANCHOR_EDIT_RULE.split("{edit_mechanism}")[0].format(skill_id="pdf")
    hermes_edit = hermes_adapter._EDIT_ENVELOPE.format(skill_id="pdf", trace_digest="D")
    hermes_native = hermes_adapter._NATIVE_EDIT_ENVELOPE.format(
        skill_id="pdf", round_idx=1, prior_pass_rate=0.5, task_instruction="I"
    )
    memento_edit = memento_adapter._MEMENTO_EDIT_ENVELOPE.format(skill_id="pdf", trace_digest="D")
    for env in (hermes_edit, hermes_native, memento_edit):
        assert env.startswith(lead)
        assert GENERIC_PROCEDURE_RULE in env
    assert "skill_manage(action='edit'" in hermes_edit
    assert "skill_manage(action='edit'" in hermes_native
    assert "skill_manage" not in memento_edit


def test_memento_runner_envelope_bytes_stable():
    from _shared.native_framework_runners import _memento_test_envelope

    from evolution.adapters._envelopes import (
        FILE_OUTPUT_EXAMPLE_FLAT,
        FILE_OUTPUT_EXAMPLE_NESTED,
        NO_PACKAGE_INSTALL_RULE,
        WORKSPACE_LISTING_HEADER,
    )

    env = _memento_test_envelope("a.csv\nb.json", "Do the task.")
    assert env.startswith(
        "Solve the Python task whose instruction follows. You are in a "
        "benchmark workspace sandbox. Read input files from your CWD using "
        "tools, and write required solution artifacts as real files under "
        "`output/` from the workspace root.\n\n"
    )
    assert WORKSPACE_LISTING_HEADER + "\na.csv\nb.json\n\n" in env
    assert "- " + FILE_OUTPUT_EXAMPLE_FLAT + "\n" in env
    assert "- " + FILE_OUTPUT_EXAMPLE_NESTED + "\n" in env
    assert NO_PACKAGE_INSTALL_RULE + "\n\n" in env
    assert env.endswith("Task instruction:\n---\nDo the task.\n---\n")


def test_run_cell_unchanged_edit_restores_native_state(tmp_path: Path):
    from iterative_evolution import loop
    from iterative_evolution.skill_editors import EditResult

    corpus = tmp_path / "corpus"
    task_dir = corpus / "tasks" / "infra" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("solve task", encoding="utf-8")
    run_dir = tmp_path / "run"

    def _fake_seed_v0(**kwargs):
        loop.write_canonical(kwargs["run_dir"], kwargs["skill_id"], SAMPLE_SKILL)
        loop.snapshot_version(kwargs["run_dir"], kwargs["skill_id"], "v0")
        return {"version": "v0"}

    def _fake_native_solve(**kwargs):
        bundle = {
            "round": kwargs["round_idx"],
            "task_id": kwargs["task_id"],
            "role": kwargs["role"],
            "skill_id": kwargs["skill_id"],
            "skill_version": kwargs["skill_version"],
            "instruction": kwargs["instruction"],
            "passed": 0,
            "total": 1,
            "pass_rate": 0.0,
            "failure_categories": ["fail"],
            "solve_result": {"outcome": "ok"},
        }
        loop.write_bundle(
            loop.bundle_path(kwargs["run_dir"], kwargs["round_idx"], kwargs["task_id"]), bundle
        )
        return {"outcome": "ok", "pass_rate": 0.0, "passed": 0, "total_tests": 1}

    def _mutating_unchanged_edit(*args, **kwargs):
        state = loop.native_state_dir(run_dir, "memp")
        (state / "drift.marker").write_text("mutated", encoding="utf-8")
        return EditResult(new_body=SAMPLE_SKILL, changed=False, telemetry={"editor": "mock"})

    with (
        mock.patch("iterative_evolution.loop.seed_v0", side_effect=_fake_seed_v0),
        mock.patch("iterative_evolution.loop._native_train_solve", side_effect=_fake_native_solve),
        mock.patch("iterative_evolution.loop.edit_skill", side_effect=_mutating_unchanged_edit),
    ):
        summary = loop.run_cell(
            framework="memp",
            skill_id="pipelines",
            role="infra",
            train_ids=["t1"],
            test_ids=[],
            n_rounds=2,
            solver_cfg={"id": "mock", "api_base": "http://localhost"},
            editor_cfg={},
            api_key="EMPTY",
            run_dir=run_dir,
            corpus_root=corpus,
            continue_on_unchanged=True,
        )

    live_state = loop.native_state_dir(run_dir, "memp")
    assert not (live_state / "drift.marker").exists()
    assert (live_state / "documents.json").is_file()
    unchanged_rounds = [r for r in summary["rounds"] if r.get("unchanged_body")]
    assert unchanged_rounds and all("editor_telemetry" in r for r in unchanged_rounds)


# --- local-endpoint model-id resolution + loud all-fail eval gate ---


def test_evolution_chat_wraps_local_served_names(monkeypatch):
    from _shared import native_framework_runners as nfr

    captured: list[str] = []

    class _FakeBackend:
        def __init__(self, *, model, api_base, api_key, **kwargs):
            captured.append(model)

        async def complete(self, *a, **k):
            import types

            return types.SimpleNamespace(content="ok", tokens_in=1, tokens_out=1, time_ms=1)

    monkeypatch.setattr("evolution.llm.litellm_backend.LiteLLMBackend", _FakeBackend)
    for model_in, expected in (
        ("openai/gpt-oss-120b", "openai/openai/gpt-oss-120b"),  # vLLM served name
        ("gpt-oss-120b", "openai/gpt-oss-120b"),  # sglang bare name
        ("openai/openai/gpt-oss-120b", "openai/openai/gpt-oss-120b"),  # litellm id passthrough
    ):
        nfr._evolution_chat("p", base="http://localhost:30004/v1", key="EMPTY", model_id=model_in)
        assert captured[-1] == expected, (model_in, captured[-1])
    nfr._evolution_chat(
        "p", base="https://openrouter.ai/api/v1", key="k", model_id="openai/gpt-oss-20b"
    )
    assert captured[-1] == "openrouter/openai/gpt-oss-20b"


def test_eval_all_failed_gate():
    from iterative_evolution.subset_sweep import _eval_all_failed

    failed = {
        "trials": {
            "seed": {"t1": [{"outcome": "hard_fail"}]},
            "evolved": {"t1": [{"outcome": "hard_fail"}]},
        }
    }
    ok = {
        "trials": {
            "seed": {"t1": [{"outcome": "ok"}]},
            "evolved": {"t1": [{"outcome": "hard_fail"}]},
        }
    }
    assert _eval_all_failed(failed, failed) is True
    assert _eval_all_failed(failed, ok) is False
    assert _eval_all_failed({"trials": {}}) is False
