"""Offline tests for the clean Table 3 small runner."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.paper

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from _shared.sbbench_cell_resolver import resolve_cells  # noqa: E402
from _shared.table3_adapter_contract import (  # noqa: E402
    REQUIRED_FRAMEWORKS,
    framework_contracts,
    sha256_file,
    validate_produce_plan,
)
from _shared.table3_schema import (  # noqa: E402
    TABLE3_ROWS,
    assert_genr_uses_source_artifact,
    build_eval_plan,
)
from _shared.training_subset import select_training_subset  # noqa: E402
from _shared.workspace_materializer import materialize_workspace  # noqa: E402

CORPUS = Path(
    os.environ.get(
        "AFTER_DATA_ROOT", str(WORKSPACE_ROOT / "datasets" / "AFTER")
    )
).resolve()
FRAMEWORK_PRODUCE_PATH = (
    REPO_ROOT / "experiments" / "5p2-table3-small" / "framework_produce_valid.py"
)
FRAMEWORK_EVAL_PATH = REPO_ROOT / "experiments" / "5p2-table3-small" / "framework_eval_valid.py"

_produce_spec = importlib.util.spec_from_file_location(
    "framework_produce_valid", FRAMEWORK_PRODUCE_PATH
)
framework_produce_valid = importlib.util.module_from_spec(_produce_spec)  # type: ignore[arg-type]
assert _produce_spec is not None and _produce_spec.loader is not None
sys.modules["framework_produce_valid"] = framework_produce_valid
_produce_spec.loader.exec_module(framework_produce_valid)

_eval_spec = importlib.util.spec_from_file_location("framework_eval_valid", FRAMEWORK_EVAL_PATH)
framework_eval_valid = importlib.util.module_from_spec(_eval_spec)  # type: ignore[arg-type]
assert _eval_spec is not None and _eval_spec.loader is not None
sys.modules["framework_eval_valid"] = framework_eval_valid
_eval_spec.loader.exec_module(framework_eval_valid)


def test_resolver_uses_sbbench_latest_and_included_true_only():
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    assert cells["valid"], cells["issues"]
    assert cells["corpus_root"] == str(CORPUS.resolve())
    assert [c["cell_id"] for c in cells["cells"]] == ["pm.pdf", "ds.pdf", "infra.terraform"]

    manifest_rows = json.loads((CORPUS / "tasks" / "manifest.json").read_text())
    included = {r["task_id"] for r in manifest_rows if r.get("included") is True}
    all_resolved = set(cells["tasks"])
    assert all_resolved <= included
    assert "infra.configs" not in [c["cell_id"] for c in cells["cells"]]


def test_resolver_counts_for_small_slice():
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    by_id = {c["cell_id"]: c for c in cells["cells"]}
    assert (
        len(by_id["pm.pdf"]["streams"]["train_a"]) + len(by_id["pm.pdf"]["streams"]["train_b"]) == 5
    )
    assert len(by_id["pm.pdf"]["streams"]["val"]) == 3
    assert len(by_id["pm.pdf"]["streams"]["test"]) == 3
    assert (
        len(by_id["ds.pdf"]["streams"]["train_a"]) + len(by_id["ds.pdf"]["streams"]["train_b"]) == 3
    )
    assert len(by_id["ds.pdf"]["streams"]["val"]) == 1
    assert len(by_id["ds.pdf"]["streams"]["test"]) == 2
    assert (
        len(by_id["infra.terraform"]["streams"]["train_a"])
        + len(by_id["infra.terraform"]["streams"]["train_b"])
        == 5
    )
    assert len(by_id["infra.terraform"]["streams"]["val"]) == 3
    assert len(by_id["infra.terraform"]["streams"]["test"]) == 3


def test_genr_uses_source_artifact_and_partner_tasks():
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    units = build_eval_plan(cells_resolved=cells)
    assert_genr_uses_source_artifact(units)
    genr = [u for u in units if u.metric == "genr"]
    assert genr
    assert all(u.source_artifact_cell_id == u.source_cell_id for u in genr)
    assert all(u.target_cell_id != u.source_cell_id for u in genr)
    assert {u.source_cell_id for u in genr} == {"pm.pdf", "ds.pdf"}
    assert {u.target_cell_id for u in genr} == {"pm.pdf", "ds.pdf"}


def test_trace_specs_are_fixed_n_for_all_produce_rows():
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    by_cell = {c["cell_id"]: c for c in cells["cells"]}
    source_single = ["anthropic/claude-sonnet-4.6"]
    source_cross = [
        "anthropic/claude-sonnet-4.6",
        "openai/Qwen/Qwen3.6-35B-A3B",
        "meta-llama/llama-3.1-8b-instruct",
    ]
    anchor_train = []
    for cell_id in cells["anchor_pair"]:
        s = by_cell[cell_id]["streams"]
        anchor_train.extend([*s["train_a"], *s["train_b"]])

    for row in TABLE3_ROWS:
        if row.kind != "produce":
            continue
        pool = source_single if row.model_diversity == "Single" else source_cross
        for cell in cells["cells"]:
            if row.anchor_only and cell["cell_id"] not in set(cells["anchor_pair"]):
                continue
            streams = cell["streams"]
            train = sorted([*streams["train_a"], *streams["train_b"]])
            if row.task_diversity == "T1":
                specs = select_training_subset(
                    task_diversity="T1",
                    source_pool=pool,
                    n_traces=24,
                    seed=0,
                    t1_task_id=train[0],
                )
                assert len({s.task_id for s in specs}) == 1
            elif row.task_diversity == "T2":
                specs = select_training_subset(
                    task_diversity="T2",
                    source_pool=pool,
                    n_traces=24,
                    seed=0,
                    role_train_tasks=train,
                )
                assert len({s.task_id for s in specs}) >= 2
            elif row.task_diversity == "T3":
                specs = select_training_subset(
                    task_diversity="T3",
                    source_pool=pool,
                    n_traces=24,
                    seed=0,
                    anchor_pair_train_tasks=anchor_train,
                )
                assert len({s.task_id for s in specs}) >= 2
            else:
                pytest.fail(f"unexpected task_diversity={row.task_diversity}")
            assert len(specs) == 24
            if row.model_diversity == "Single":
                assert len({s.source_model_id for s in specs}) == 1
            else:
                assert sorted({s.source_model_id for s in specs}) == sorted(source_cross)


def test_framework_produce_plans_nonzero_jobs_from_manifest_rows():
    manifest = json.loads(
        (REPO_ROOT / "experiments" / "5p2-table3-small" / "table3_manifest.json").read_text()
    )
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    jobs = framework_produce_valid._planned_jobs(
        manifest=manifest,
        cells=cells,
        frameworks=["evolution", "memp", "hermes", "memento"],
        row_ids={3, 4, 5, 6, 7, 8},
    )
    # rows 3-6 run on 3 cells; rows 7-8 are anchor-only on 2 cells.
    assert len(jobs) == 4 * ((4 * 3) + (2 * 2))
    assert any(fw == "evolution" for fw, _row, _cell in jobs)


def test_framework_produce_fallback_rows_prevents_silent_noop():
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    jobs = framework_produce_valid._planned_jobs(
        manifest={"frameworks": ["evolution", "memp", "hermes", "memento"]},
        cells=cells,
        frameworks=["memp"],
        row_ids={3},
        only_cell="pm.pdf",
    )
    assert len(jobs) == 1
    fw, row, cell = jobs[0]
    assert fw == "memp"
    assert row["row_id"] == 3
    assert cell == "pm.pdf"


def test_trace_budget_override_writes_nonreportable_n2_spec(tmp_path):
    trace_path = tmp_path / "row3__pm.pdf.json"
    payload = {
        "row": {"row_id": 3},
        "cell_id": "pm.pdf",
        "trace_budget": 4,
        "trace_spec": [
            {"task_id": f"t{i}", "source_model_id": "m", "attempt_seed": i} for i in range(4)
        ],
    }
    trace_path.write_text(json.dumps(payload), encoding="utf-8")
    derived_path, derived = framework_produce_valid._derive_trace_payload(
        trace_payload=payload,
        trace_path=trace_path,
        trace_budget_override=2,
        derived_dir=tmp_path / "derived",
    )
    assert derived_path.is_file()
    assert derived["trace_budget"] == 2
    assert len(derived["trace_spec"]) == 2
    assert derived["non_reportable"] is True
    assert derived["parent_trace_spec_hash"] == sha256_file(trace_path)


def test_base_seed_resolves_canonical_source_trace_skills(tmp_path):
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    infra = next(c for c in cells["cells"] if c["cell_id"] == "infra.terraform")
    task_id = sorted([*infra["streams"]["train_a"], *infra["streams"]["train_b"]])[0]
    expected_skills = [str(s) for s in cells["tasks"][task_id]["skills"]]

    # The seed loader now reads SKILL_HANDCRAFT.md (handcraft baseline), so the
    # shared fixture corpus is mirrored into tmp_path with an extra distinct
    # SKILL_HANDCRAFT.md alongside each SKILL.md. This keeps the test
    # self-contained without writing to the read-only shared fixture.
    shadow_corpus = tmp_path / "corpus"
    shadow_corpus.mkdir()
    shadow_skills = shadow_corpus / "skills"
    shadow_skills.mkdir()
    for skill in expected_skills:
        src = CORPUS / "skills" / skill
        dst = shadow_skills / skill
        shutil.copytree(src, dst)
        # Synthesize a distinct handcraft body so the resolver hash differs
        # from SKILL.md — the test asserts the loader picks the handcraft.
        (dst / "SKILL_HANDCRAFT.md").write_text(
            f"---\nname: {skill}\ndescription: handcraft test fixture\n---\n"
            f"# {skill} handcraft body (test).\n",
            encoding="utf-8",
        )
    cells = dict(cells)
    cells["corpus_root"] = str(shadow_corpus.resolve())

    seed = framework_produce_valid.resolve_base_skill_seed(
        cells=cells,
        specs=[{"task_id": task_id, "source_model_id": "m", "attempt_seed": 0}],
    )
    assert seed.skill_names == expected_skills
    assert seed.to_jsonable()["base_skill_names"] == expected_skills
    for skill in expected_skills:
        assert seed.skill_hashes[skill] == sha256_file(shadow_skills / skill / "SKILL_HANDCRAFT.md")
        # And explicitly NOT equal to the SKILL.md hash — confirms the loader
        # is reading the handcraft body, not the slim generated one.
        assert seed.skill_hashes[skill] != sha256_file(shadow_skills / skill / "SKILL.md")

    copied = framework_produce_valid.copy_base_skill_dirs(seed, tmp_path / "out")
    assert copied["base_seed_ok"] is True
    for skill in expected_skills:
        dst_md = tmp_path / "out" / skill / "SKILL.md"
        assert dst_md.is_file()
        # The destination workspace SKILL.md must contain the handcraft body
        # so downstream evolution starts from the same artifact as Table 4.
        assert sha256_file(dst_md) == sha256_file(shadow_skills / skill / "SKILL_HANDCRAFT.md")


def test_base_seed_uses_folder_id_over_handcraft_frontmatter_name(tmp_path):
    corpus = tmp_path / "corpus"
    skill_dir = corpus / "skills" / "pipelines"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pipelines\ndescription: fallback\n---\n\nFALLBACK-BODY\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL_HANDCRAFT.md").write_text(
        "---\nname: data-pipeline\ndescription: handcraft\n---\n\nHANDCRAFT-BODY\n",
        encoding="utf-8",
    )
    cells = {
        "corpus_root": str(corpus),
        "tasks": {"t1": {"skills": ["pipelines"]}},
    }
    seed = framework_produce_valid.resolve_base_skill_seed(
        cells=cells,
        specs=[{"task_id": "t1", "source_model_id": "m", "attempt_seed": 0}],
    )
    assert seed.skill_files["pipelines"] == "SKILL_HANDCRAFT.md"
    out = tmp_path / "out"
    copied = framework_produce_valid.copy_base_skill_dirs(seed, out)
    assert copied["base_seed_ok"] is True
    staged = (out / "pipelines" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: pipelines" in staged
    assert "name: data-pipeline" not in staged
    assert "HANDCRAFT-BODY" in staged
    assert "FALLBACK-BODY" not in staged


def test_evolution_seed_overlay_preserves_hidden_metadata(tmp_path):
    source = tmp_path / "source"
    live = tmp_path / "live"
    source.mkdir()
    live.mkdir()
    (source / "SKILL.md").write_text("canonical\n", encoding="utf-8")
    (source / "notes.txt").write_text("source note\n", encoding="utf-8")
    (live / "SKILL.md").write_text("normalized import\n", encoding="utf-8")
    (live / ".evolution").mkdir()
    (live / ".evolution" / "lineage.json").write_text("{}", encoding="utf-8")

    framework_produce_valid._overlay_canonical_skill_source(source, live)

    assert (live / "SKILL.md").read_text(encoding="utf-8") == "canonical\n"
    assert (live / "notes.txt").read_text(encoding="utf-8") == "source note\n"
    assert (live / ".evolution" / "lineage.json").read_text(encoding="utf-8") == "{}"


def test_memp_task_env_runs_pytest_with_relative_workspace(tmp_path, monkeypatch):
    from evolution.adapters._memp_env import TaskEnv

    root = tmp_path / "relroot"
    workspace = root / "workspace"
    tests_dir = root / "task" / "tests"
    workspace.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (workspace / "_solve.py").write_text(
        "from pathlib import Path\nPath('output.txt').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (tests_dir / "test_outputs.py").write_text(
        "from pathlib import Path\n\n"
        "def test_solution_output():\n"
        "    assert Path('output.txt').read_text(encoding='utf-8') == 'ok'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    env = TaskEnv(
        task_id="relative-task",
        workspace=Path("relroot/workspace"),
        test_path=Path("relroot/task/tests/test_outputs.py"),
        task_path=Path("relroot/task"),
        python=sys.executable,
    )

    result = env.step("run_tests")

    assert env.total_tests == 1
    assert env.last_pass_rate == 1.0
    assert "1/1 pytest tests passed" in result.observation


def test_hermes_small_context_source_model_gets_config_override(tmp_path):
    from evolution.adapters import hermes_adapter

    cfg = {"id": "meta-llama/llama-3.1-8b-instruct"}
    assert framework_produce_valid._hermes_context_length(cfg) == 64_000

    hermes_adapter.write_hermes_config(
        hermes_home=tmp_path,
        solver_model=cfg["id"],
        solver_base="https://openrouter.ai/api/v1",
        solver_key="EMPTY",
        context_length=framework_produce_valid._hermes_context_length(cfg),
    )

    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "context_length: 64000" in text
    assert "auxiliary:" in text
    assert "  compression:" in text


def test_hermes_train_task_uses_absolute_home_after_chdir(tmp_path, monkeypatch):
    from evolution.adapters import hermes_adapter

    root = tmp_path / "relroot"
    workspace = root / "workspace"
    hermes_home = root / "hermes_home"
    workspace.mkdir(parents=True)
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text("model:\n  context_length: 64000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    class FakeAIAgent:
        def __init__(self, **_kwargs):
            home = Path(os.environ["HERMES_HOME"])
            assert home.is_absolute()
            assert (home / "config.yaml").is_file()

        def run_conversation(self, _envelope):
            assert Path.cwd() == workspace.resolve()
            return {"final_response": "ok"}

    fake_module = types.SimpleNamespace(AIAgent=FakeAIAgent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_module)

    result = hermes_adapter._run_one_train_task(
        task_id="relative-hermes",
        task_instruction="do the task",
        workspace_dir=Path("relroot/workspace"),
        hermes_home=Path("relroot/hermes_home"),
        solver_model_served="fake/model",
        solver_base="https://example.invalid/v1",
        solver_key="EMPTY",
        max_iterations=1,
        events_path=Path("relroot/hermes_home/events.jsonl"),
    )

    assert result["ok"] is True
    assert Path.cwd() == tmp_path


def test_memento_session_workspace_is_seeded_with_task_inputs(tmp_path):
    from evolution.adapters import memento_adapter

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "instruction.md").write_text("task", encoding="utf-8")
    (workspace / "input.pdf").write_bytes(b"%PDF-1.4\n")
    (workspace / "environment").mkdir()
    (workspace / "environment" / "data.txt").write_text("data", encoding="utf-8")

    session_workspace = memento_adapter._memento_session_workspace(workspace, "abc123")
    copied = memento_adapter._copy_workspace_inputs_to_session(workspace, session_workspace)

    assert copied == 3
    assert (session_workspace / "instruction.md").read_text(encoding="utf-8") == "task"
    assert (session_workspace / "input.pdf").read_bytes() == b"%PDF-1.4\n"
    assert (session_workspace / "environment" / "data.txt").read_text(encoding="utf-8") == "data"


def test_agent_workspace_materializer_hides_oracle_and_verifier(tmp_path):
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    task_id = next(c for c in cells["cells"] if c["cell_id"] == "pm.pdf")["streams"]["train_a"][0]
    task_dir = Path(cells["tasks"][task_id]["raw_sbbench_path"])
    ws = tmp_path / "workspace"

    materialize_workspace(task_dir, ws, agent_solve_sh=True)

    assert not (ws / "tests").exists()
    assert not (ws / "source_artifacts").exists()
    assert not (ws / "_test_adapted.py").exists()
    assert not (ws / "solution.py").exists()
    assert (ws / "solve.sh").is_file()


def _valid_readiness_plan() -> dict:
    return {
        "schema": "table3-framework-produce-v2",
        "status": "completed",
        "valid_for_eval": True,
        "trace_spec_hash": "abc123",
        "n_traces_used": 2,
        "trace_budget": 2,
        "source_model_ids": ["anthropic/claude-sonnet-4.6"],
        "used_openrouter": True,
        "base_seed": {
            "policy": "source_trace_task_skill_union_v1",
            "corpus_root": str(CORPUS),
            "seed_source_task_ids": ["task-a", "task-b"],
            "base_skill_names": ["pdf"],
            "base_skill_hashes": {
                "pdf": "0" * 64,
            },
        },
        "records": [
            {
                "task_id": "task-a",
                "source_model_id": "anthropic/claude-sonnet-4.6",
                "solve_status": "completed",
                "pytest_status": "completed",
            },
            {
                "task_id": "task-b",
                "source_model_id": "anthropic/claude-sonnet-4.6",
                "solve_status": "completed",
                "pytest_status": "completed",
            },
        ],
        "health": {
            "persistence_ok": True,
            "base_seed_ok": True,
            "eval_writes_blocked": True,
            "skipped_or_noop_labelled": True,
        },
    }


def test_readiness_produce_contract_accepts_complete_plan():
    issues = validate_produce_plan(
        _valid_readiness_plan(),
        trace_spec_hash="abc123",
        trace_budget=2,
        heldout_model_id="openai/gpt-4o-mini",
    )
    assert issues == []


def test_readiness_produce_contract_rejects_stale_and_heldout_source():
    plan = _valid_readiness_plan()
    plan["trace_spec_hash"] = "old"
    plan["source_model_ids"] = ["openai/gpt-4o-mini"]

    issues = validate_produce_plan(
        plan,
        trace_spec_hash="abc123",
        trace_budget=2,
        heldout_model_id="openai/gpt-4o-mini",
    )
    assert any("trace_spec_hash" in issue for issue in issues)
    assert any("held-out GenM model" in issue for issue in issues)


def test_readiness_produce_contract_rejects_v1_or_missing_seed():
    plan = _valid_readiness_plan()
    plan["schema"] = "table3-framework-produce-v1"

    issues = validate_produce_plan(
        plan,
        trace_spec_hash="abc123",
        trace_budget=2,
        heldout_model_id="openai/gpt-4o-mini",
    )
    assert any("schema" in issue for issue in issues)

    plan = _valid_readiness_plan()
    plan.pop("base_seed")
    issues = validate_produce_plan(
        plan,
        trace_spec_hash="abc123",
        trace_budget=2,
        heldout_model_id="openai/gpt-4o-mini",
    )
    assert any("base_seed" in issue for issue in issues)


def test_eval_rejects_stale_v1_produce_plan(tmp_path):
    produce_dir = tmp_path / "produce"
    plan_dir = produce_dir / "hermes"
    plan_dir.mkdir(parents=True)
    plan = _valid_readiness_plan()
    plan["schema"] = "table3-framework-produce-v1"
    (plan_dir / "row3__pm.pdf.json").write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(RuntimeError, match="schema is not reportable"):
        framework_eval_valid._plan_for_unit(
            produce_dir=produce_dir,
            framework="hermes",
            unit={"row_id": 3, "source_artifact_cell_id": "pm.pdf"},
        )


def test_readiness_framework_contracts_cover_all_required_frameworks():
    contracts = framework_contracts(WORKSPACE_ROOT, REPO_ROOT / "experiments" / "tmp-artifacts")
    assert tuple(contracts) == REQUIRED_FRAMEWORKS
    for framework, contract in contracts.items():
        assert contract.framework == framework
        assert contract.module.startswith("evolution.adapters.")
        assert contract.produce_symbol
        assert contract.plan_class
        assert (
            "trace_driver" in contract.fixed_trace_driver
            or contract.fixed_trace_driver.startswith("native_evolution")
        )


def test_valid_framework_producer_emits_contract_shape_without_global_state():
    cells = resolve_cells(
        corpus_root=CORPUS,
        selected_cells=("pm.pdf", "ds.pdf", "infra.terraform"),
        anchor_pair=("pm.pdf", "ds.pdf"),
    )
    base_seed = framework_produce_valid.resolve_base_skill_seed(
        cells=cells,
        specs=[{"task_id": "sonnet-7b445923fffa", "source_model_id": "m", "attempt_seed": 0}],
    )
    records = [
        {
            "task_id": "sonnet-7b445923fffa",
            "source_model_id": "anthropic/claude-sonnet-4.6",
            "solve_status": "completed",
            "pytest_status": "completed",
            "api_base": "https://openrouter.ai/api/v1",
        },
        {
            "task_id": "sonnet-7b445923fffa",
            "source_model_id": "anthropic/claude-sonnet-4.6",
            "solve_status": "completed",
            "pytest_status": "completed",
            "api_base": "https://openrouter.ai/api/v1",
        },
    ]
    plan = framework_produce_valid._base_plan(
        framework="hermes",
        row_id=3,
        row_label="T1 x Single",
        cell_id="pm.pdf",
        trace_path=Path("trace.json"),
        trace_hash="hash",
        trace_budget=2,
        records=records,
        state={
            "hermes_home": "artifacts/framework_produce_work/hermes/row3__pm.pdf/state",
            "skills_dir": "artifacts/framework_produce_work/hermes/row3__pm.pdf/state/skills",
            "persistence_ok": True,
            "base_seed_ok": True,
        },
        base_seed=base_seed,
        notes=["isolated"],
    )

    assert plan["schema"] == "table3-framework-produce-v2"
    assert plan["status"] == "completed"
    assert plan["valid_for_eval"] is True
    assert plan["trace_spec_hash"] == "hash"
    assert plan["n_traces_used"] == 2
    assert plan["used_openrouter"] is True
    assert plan["base_seed"]["base_skill_names"] == ["pdf"]
    assert "state" in plan
    assert "global" not in json.dumps(plan["state"]).lower()
