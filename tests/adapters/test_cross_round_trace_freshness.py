"""Task 5 fix — per-round trace freshness (cross-round stale-trace bleed).

Regression for the CRITICAL defect found reviewing commit d426020:

Part B of Task 5 omits ``--trace-model`` for cross-source cells (rows
4/6/8) and Part A removed the ``limit=10`` cap. Part B's safety premise
("the trace store only holds THIS round's produce traces") is FALSE at
cumulative round N>1, because the runner's cumulative seed copytree
(``framework_produce_valid.py``: cumulative ``seed_skill_root`` ->
``live_dir``) and the produced-state snapshot copytree both copied the
prior round's ``.traces/`` forward with NO ``ignore=``. Net: at round
N>1 a cross-source cell with ``--trace-model`` omitted + ``limit=None``
re-reflects round-(N-1)'s failing traces (whose ``reflection.json`` was
just re-archived by ``_archive_stale_reflections``), so round N's
rewrite is partly driven by stale prior-round failures.

The fix makes each round start from a FRESH trace store while still
inheriting the evolved ``SKILL.md`` + ``.evolution/versions/``. These
tests:

  * ``test_cumulative_seed_excludes_traces`` — the runner's cumulative
    seed/snapshot copytree seam must NOT carry ``.traces/`` forward but
    MUST carry ``SKILL.md`` + ``.evolution/versions/`` (skill version
    state). FAILS at d426020 (bare copytree copies ``.traces/``).

  * ``test_round_n_reflect_ignores_stale_prior_round_trace`` —
    end-to-end: prior-round failing trace (already has reflection) +
    current-round failing trace for the same task, cross-source cell
    (``trace_model=None``, ``limit=None``). After the runner's
    cumulative seed, reflect at round N must reflect ONLY the
    current-round trace. FAILS at d426020 (stale trace re-reflected
    because its reflection was archived and ``.traces/`` bled forward).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from evolution.core.models import Skill, SkillFrontmatter, SkillTrace
from evolution.evolution.reflect import reflect_failing_traces
from evolution.lineage.traces import TraceStore
from evolution.llm.mock_backend import MockBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "experiments"))

_FPV_PATH = (
    REPO_ROOT
    / "experiments"
    / "table3-4-minimal-sbbench-v2"
    / "runner"
    / "framework_produce_valid.py"
)
_spec = importlib.util.spec_from_file_location("framework_produce_valid_t34", _FPV_PATH)
framework_produce_valid = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["framework_produce_valid_t34"] = framework_produce_valid
_spec.loader.exec_module(framework_produce_valid)

_VALID_REFLECTION = json.dumps(
    {
        "root_cause": "wrong output",
        "skill_gap": "missing validation",
        "suggested_fix": "check the expected file",
        "severity": "major",
        "pattern": "logic_error",
    }
)


def _make_skill(root: Path, name: str = "test-skill") -> Skill:
    skill_dir = root / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: A test skill\n---\n\nUse Python.\n",
        encoding="utf-8",
    )
    return Skill(
        frontmatter=SkillFrontmatter(name=name, description="A test skill"),
        body="Use Python.",
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _save_failed_trace(skill: Skill, trace_id: str, *, task: str, model: str) -> Path:
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
        prompt="p",
        response="r",
        code="print('wrong')",
        evidence={
            "schema": "evolution-trace-evidence-v3",
            "agent_solve": {"exit_code": 1, "timeout": False, "diagnostic": "nonzero_exit"},
            "produced_artifacts": [],
        },
    )


def _runner_cumulative_copytree(src: Path, dst: Path) -> None:
    """Reproduce the runner's cumulative seed/snapshot copytree EXACTLY.

    At d426020 the runner had no ``ignore=`` (``_IGNORE_TRACES`` absent)
    so ``getattr`` falls back to ``None`` ⇒ bare copytree ⇒ ``.traces/``
    bleeds forward and these tests FAIL. With the fix ``_IGNORE_TRACES``
    exists ⇒ ``.traces/`` excluded ⇒ tests PASS.
    """
    ignore = getattr(framework_produce_valid, "_IGNORE_TRACES", None)
    shutil.copytree(src, dst, symlinks=False, ignore=ignore)


# ---------------------------------------------------------------------------
# Seam regression: cumulative copytree must drop .traces, keep version state
# ---------------------------------------------------------------------------


def test_cumulative_seed_excludes_traces(tmp_path: Path):
    """The runner's cumulative seed/snapshot copy must NOT carry
    ``.traces/`` forward, but MUST carry ``SKILL.md`` +
    ``.evolution/versions/`` (cumulative skill-version inheritance)."""
    # Prior-round produced_state skill dir (becomes next round's
    # seed_skill_root): evolved SKILL.md + .evolution/versions/ + a
    # round-(N-1) .traces/ that the next round must NOT inherit.
    prev = tmp_path / "produced_state_roundN-1" / "test-skill"
    prev.mkdir(parents=True)
    (prev / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: evolved v3\n---\n\nEvolved body.\n",
        encoding="utf-8",
    )
    versions = prev / ".evolution" / "versions"
    versions.mkdir(parents=True)
    (versions / "v3.md").write_text("evolved snapshot", encoding="utf-8")
    (prev / ".evolution" / "lineage.json").write_text("[]", encoding="utf-8")
    traces = prev / ".traces"
    traces.mkdir()
    (traces / "index.json").write_text("[]", encoding="utf-8")
    (traces / "stale-trace").mkdir()
    (traces / "stale-trace" / "result.json").write_text("{}", encoding="utf-8")

    # The exact seam the runner uses for the cumulative seed copy.
    live = tmp_path / "live" / "test-skill"
    _runner_cumulative_copytree(prev, live)

    # Cumulative skill-version state preserved...
    assert (live / "SKILL.md").read_text(encoding="utf-8").endswith("Evolved body.\n")
    assert (live / ".evolution" / "versions" / "v3.md").is_file()
    assert (live / ".evolution" / "lineage.json").is_file()
    # ...but the prior round's trace store is NOT inherited.
    assert not (live / ".traces").exists(), (
        "round N must start with a FRESH trace store; prior round's "
        ".traces/ bled forward through the cumulative seed copytree"
    )


# ---------------------------------------------------------------------------
# End-to-end: cross-source round N reflect must ignore stale prior-round
# ---------------------------------------------------------------------------


def test_round_n_reflect_ignores_stale_prior_round_trace(tmp_path: Path):
    """Cumulative round N>1, cross-source cell (trace_model=None,
    limit=None). Prior round produced a failing trace that the prior
    round already reflected; the current round produced its own failing
    trace for the SAME task. Reflect at round N must reflect ONLY the
    current round's trace, never the stale prior-round one."""
    # --- Round N-1: build produced_state with a reflected failing trace
    prev_root = tmp_path / "roundN-1"
    prev_skill = _make_skill(prev_root)
    _save_failed_trace(prev_skill, "stale-r1", task="cell-task", model="gpt-oss-120b")
    # Round N-1 addressed it -> reflection.json exists in its trace dir.
    ts_prev = TraceStore(prev_skill)
    (ts_prev.traces_dir / "stale-r1" / "reflection.json").write_text(
        _VALID_REFLECTION, encoding="utf-8"
    )
    # Snapshot of round N-1's live skill dir == next round's seed root.
    produced_state = tmp_path / "produced_state" / "test-skill"
    _runner_cumulative_copytree(prev_skill.path, produced_state)

    # --- Round N: cumulative seed from produced_state into a fresh live
    live_root = tmp_path / "roundN"
    live_skill_dir = live_root / ".agents" / "skills" / "test-skill"
    live_skill_dir.parent.mkdir(parents=True)
    _runner_cumulative_copytree(produced_state, live_skill_dir)
    live_skill = Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body="Evolved body.",
        path=live_skill_dir,
        skill_md_path=live_skill_dir / "SKILL.md",
    )
    # Round N runs its OWN solves -> its own fresh failing trace, same
    # task, a (cross-source) different in-cell source model.
    _save_failed_trace(live_skill, "fresh-rN", task="cell-task", model="gemma-7b")

    # The adapter archives ALL reflection.json before reflect runs
    # (_archive_stale_reflections, restored in a finally). If the stale
    # prior-round trace bled forward, its reflection.json is now archived
    # too, so it re-qualifies as an unreflected failing trace and — with
    # --trace-model omitted + limit=None — gets re-reflected. Emulate
    # that archive step scoped to this skill's trace dir.
    for refl in (live_skill_dir / ".traces").glob("*/reflection.json"):
        refl.rename(refl.with_suffix(".json.archived"))

    backend = MockBackend(default_response=_VALID_REFLECTION)
    reflections = reflect_failing_traces(
        backend,
        live_skill,
        "task",
        task_filter="cell-task",  # per failing-task scope
        trace_model=None,  # cross-source ⇒ adapter omits --trace-model
        limit=None,  # Part A: no cap
        strict=True,
    )

    ts = TraceStore(live_skill)
    reflected = {d.name for d in ts.traces_dir.iterdir() if (d / "reflection.json").exists()}
    assert reflected == {"fresh-rN"}, (
        f"round N must reflect ONLY its own round's trace; got {reflected}. "
        "Stale prior-round trace bled forward and was re-reflected."
    )
    assert "stale-r1" not in reflected
    assert not (ts.traces_dir / "stale-r1").exists()
    assert len(reflections) == 1


def test_round_n_reflect_includes_rejected_edit_buffer(tmp_path: Path, monkeypatch):
    """At round N>=2, after round N-1's rewrite was rejected (e.g. a leak), the
    reflector prompt must carry the ``## Rejected-Edit Buffer`` so the next
    reflection can steer clear of the same dead end."""
    from evolution.evolution.meta_skill import build_rejected_entry, write_round_record

    monkeypatch.delenv("EVO_REFLECT_ROW_ID", raising=False)
    skill = _make_skill(tmp_path)
    # Round N-1 produced a rejected (leak-scanned) candidate -> round record.
    write_round_record(
        skill.path,
        {
            "timestamp": "20260601_120000_000001",
            "round": 1,
            "gate_status": "no_op",
            "rejected": build_rejected_entry(
                row_id="row3__cell",
                candidate_status="leak_rejected",
                new_headings=["## Overfit"],
            ),
        },
    )
    # Round N has its own fresh, unreflected failing trace.
    _save_failed_trace(skill, "fresh-rN", task="cell-task", model="gpt-oss-120b")

    backend = MockBackend(default_response=_VALID_REFLECTION)
    reflect_failing_traces(
        backend,
        skill,
        "task",
        task_filter="cell-task",
        strict=True,
    )

    prompt = backend.calls[0]["messages"][0]["content"]
    assert "## Rejected-Edit Buffer" in prompt
    assert "leak_rejected" in prompt


def test_seed_v1_snapshot_is_handcraft(tmp_path: Path, monkeypatch):
    """Round-1 seed: the SkillStore lineage v1 (what the evolution engine
    evolves from) must be the SKILL_HANDCRAFT.md body, not the canonical
    SKILL.md that import_skill snapshots before the handcraft overlay.
    Fails before the framework_produce_valid handcraft re-snapshot fix,
    passes after. When no handcraft exists the run legitimately falls
    back to the canonical SKILL.md (covered by the no-handcraft case)."""
    import hashlib

    from _shared.table3_base_seed import BASE_SEED_POLICY, BaseSkillSeed

    proj = tmp_path / "proj"
    skills_root = proj / ".agents" / "skills"
    monkeypatch.setattr(framework_produce_valid, "EVO_PROJECT_ROOT", proj)
    monkeypatch.setattr(framework_produce_valid, "EVO_SKILLS_ROOT", skills_root)

    src = tmp_path / "corpus" / "skills" / "pdf"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: canonical synthetic body\n---\n\nCANONICAL-BODY-MARKER\n",
        encoding="utf-8",
    )
    hc_text = "---\nname: pdf\ndescription: handcraft body\n---\n\nHANDCRAFT-BODY-MARKER\n"
    (src / "SKILL_HANDCRAFT.md").write_text(hc_text, encoding="utf-8")

    seed = BaseSkillSeed(
        policy=BASE_SEED_POLICY,
        corpus_root=tmp_path / "corpus",
        source_task_ids=["t"],
        skill_names=["pdf"],
        skill_dirs={"pdf": src},
        skill_files={"pdf": "SKILL_HANDCRAFT.md"},
        skill_hashes={"pdf": hashlib.sha256(hc_text.encode()).hexdigest()},
    )

    framework_produce_valid._snapshot_and_seed_evolution_skills(
        base_seed=seed, work_root=tmp_path / "work", seed_skill_root=None
    )

    live = skills_root / "pdf"
    active = (live / "SKILL.md").read_text(encoding="utf-8")
    assert "HANDCRAFT-BODY-MARKER" in active
    assert "CANONICAL-BODY-MARKER" not in active

    v1 = live / ".evolution" / "versions" / "v1" / "SKILL.md"
    assert v1.is_file()
    v1_text = v1.read_text(encoding="utf-8")
    assert "HANDCRAFT-BODY-MARKER" in v1_text, "lineage v1 must be the handcraft body"
    assert "CANONICAL-BODY-MARKER" not in v1_text

    lineage = json.loads((live / ".evolution" / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["current_version"] == "v1"
    assert lineage["lineage"][0]["origin"] == "import"
    assert lineage["lineage"][0]["parent"] is None
