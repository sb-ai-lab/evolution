"""Task 3: outer anchor-skill loop parallelism with isolated per-anchor roots.

Verifies that ``run_publication`` parallelizes the across-anchor loop with a
bounded pool while:
  * giving each anchor a DISTINCT ``EVO_PROJECT_ROOT`` / ``EVO_SKILLS_ROOT``,
  * never mutating the shared parent ``env`` dict or ``os.environ``,
  * finishing well under the serial wall-clock (4 anchors x ~0.3s sleep).

The per-anchor produce/eval subprocess work is stubbed (no real children).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.paper

REPO_ROOT = Path(__file__).resolve().parents[2]
EXP = REPO_ROOT / "experiments" / "table3-4-minimal-sbbench-v2"
RUN_PUBLICATION = EXP / "runner" / "run_publication.py"
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "experiments"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_publication = _load_module("t34_run_publication", RUN_PUBLICATION)


def _fake_skill(idx: int, root: Path) -> dict:
    d = root / f"artifact_{idx}"
    d.mkdir(parents=True, exist_ok=True)
    return {
        "skill": f"skill-{idx}",
        "source_cell": f"cell.{idx}",
        "artifact_dir": str(d),
    }


def test_seed_anchor_root_isolates_and_preserves_resumable_artifacts(tmp_path):
    """Re-seeding must NOT clobber resumable produce artifacts under run_root/<skill>.

    Exercises the REAL ``_seed_anchor_root`` (rmtree + copytree), which the
    parallelism test stubs out via ``_run_one_anchor``. Proves the isolated
    ``_roots/<skill>/.agents/skills`` tree is disjoint from the sibling
    ``run_root/<skill>`` produce artifacts, only contains the anchor skill,
    and that a second (resume) call is idempotent without collateral data loss.
    """

    skill = "demo-skill"

    # (1) Fake source skills tree: the anchor plus unrelated auxiliaries.
    src_skills = tmp_path / "src" / ".agents" / "skills"
    (src_skills / skill).mkdir(parents=True, exist_ok=True)
    (src_skills / skill / "SKILL.md").write_text(
        f"---\nname: {skill}\n---\nanchor-body\n", encoding="utf-8"
    )
    (src_skills / skill / ".traces" / "old-trace").mkdir(parents=True)
    (src_skills / skill / ".traces" / "old-trace" / "result.json").write_text(
        "{}", encoding="utf-8"
    )
    (src_skills / "alpha").mkdir(parents=True, exist_ok=True)
    (src_skills / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\n---\nalpha-body\n", encoding="utf-8"
    )
    (src_skills / "beta").mkdir(parents=True, exist_ok=True)
    (src_skills / "beta" / "SKILL.md").write_text(
        "---\nname: beta\n---\nbeta-body\n", encoding="utf-8"
    )

    base_env = {"EVO_SKILLS_ROOT": str(src_skills)}

    run_root = tmp_path / "run"
    run_root.mkdir(parents=True, exist_ok=True)

    # (2) Sibling run_root/<skill> "skill_dir" with a prior-round produce artifact.
    sibling_skill_dir = run_root / skill
    sibling_skill_dir.mkdir(parents=True, exist_ok=True)
    prior_artifact = sibling_skill_dir / "round3_produce.json"
    prior_payload = '{"round": 3, "produced": true}\n'
    prior_artifact.write_text(prior_payload, encoding="utf-8")

    # (3) First seed call: builds the isolated tree with the copied source.
    proj, skl = run_publication._seed_anchor_root(base_env=base_env, run_root=run_root, skill=skill)
    assert proj == (run_root / "_roots" / skill).resolve()
    assert skl == proj / ".agents" / "skills"
    assert skl.is_dir()
    assert (skl / skill / "SKILL.md").read_text(encoding="utf-8") == (
        f"---\nname: {skill}\n---\nanchor-body\n"
    )
    assert not (skl / "alpha").exists()
    assert not (skl / "beta").exists()
    assert not (skl / skill / ".traces").exists()
    # Sibling prior-round artifact is byte-unchanged (disjointness proof).
    assert prior_artifact.read_text(encoding="utf-8") == prior_payload

    # (4) Second seed call (resume scenario): rm+recreates the isolated tree,
    # must NOT touch/remove the sibling run_root/<skill> artifact.
    stale_marker = skl / "stale_from_prev_round.txt"
    stale_marker.write_text("should be wiped by re-seed", encoding="utf-8")

    proj2, skl2 = run_publication._seed_anchor_root(
        base_env=base_env, run_root=run_root, skill=skill
    )
    assert (proj2, skl2) == (proj, skl)
    # Isolated tree was freshly recreated from source: stale file gone, source back.
    assert not stale_marker.exists()
    assert (skl2 / skill / "SKILL.md").read_text(encoding="utf-8") == (
        f"---\nname: {skill}\n---\nanchor-body\n"
    )
    assert not (skl2 / "alpha").exists()
    # Sibling produce artifact survived the second (resume) seed untouched.
    assert prior_artifact.exists()
    assert prior_artifact.read_text(encoding="utf-8") == prior_payload


def test_anchor_pool_reports_all_failed_anchors_with_types(tmp_path, monkeypatch):
    """The post-drain raise lists ALL failed anchors (sorted) with exc types,
    chains a real traceback, and avoids precedence-implying wording.
    """

    skills = [_fake_skill(i, tmp_path / "arts") for i in range(3)]

    def fake_one_anchor(skill, *, base_env, **_kw):
        name = str(skill["skill"])
        if name == "skill-1":
            raise RuntimeError("boom-one")
        if name == "skill-2":
            raise ValueError("boom-two")
        return {"skill": name, "ok": True}

    monkeypatch.setattr(run_publication, "_run_one_anchor", fake_one_anchor)

    src_skills = tmp_path / "src_agents" / ".agents" / "skills" / "demo"
    src_skills.mkdir(parents=True, exist_ok=True)
    (src_skills / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")

    base_env = {"EVO_SKILLS_ROOT": str(src_skills.parent)}
    monkeypatch.setenv("EVO_SKILL_WORKERS", "3")

    run_root = tmp_path / "run"
    run_root.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(run_id="testrun")

    with pytest.raises(SystemExit) as ei:
        run_publication._run_anchor_pool(
            skills,
            base_env=base_env,
            run_root=run_root,
            args=args,
        )

    msg = str(ei.value)
    # Both failed anchors named, sorted; the succeeding one absent.
    assert "skill-1, skill-2" in msg
    assert "skill-0" not in msg
    # Per-anchor exception types + messages reported.
    assert "skill-1: RuntimeError: boom-one" in msg
    assert "skill-2: ValueError: boom-two" in msg
    # Neutral wording — no precedence-implying "first error".
    assert "first error" not in msg
    # Chained from a real worker exception with a printed traceback.
    assert ei.value.__cause__ is not None
    assert "Traceback (most recent call last)" in msg


def test_anchor_pool_runs_parallel_isolated_and_no_env_leak(tmp_path, monkeypatch):
    skills = [_fake_skill(i, tmp_path / "arts") for i in range(4)]

    seen: dict[str, dict[str, str]] = {}

    # Stub the per-anchor body so it does no real subprocess work: it sleeps a
    # little (so a serial driver would take ~4x as long) and records the
    # isolated roots its anchor_env carries.
    def fake_one_anchor(skill, *, base_env, **_kw):
        anchor_env = base_env.copy()
        skill_name = str(skill["skill"])
        # The driver is responsible for handing us a per-anchor isolated root
        # via the kwargs; mirror what _run_one_anchor must construct.
        proj = _kw["anchor_project_root"]
        skl = _kw["anchor_skills_root"]
        anchor_env["EVO_PROJECT_ROOT"] = str(proj)
        anchor_env["EVO_SKILLS_ROOT"] = str(skl)
        time.sleep(0.3)
        seen[skill_name] = {
            "EVO_PROJECT_ROOT": anchor_env["EVO_PROJECT_ROOT"],
            "EVO_SKILLS_ROOT": anchor_env["EVO_SKILLS_ROOT"],
        }
        return {"skill": skill_name, "ok": True}

    monkeypatch.setattr(run_publication, "_run_one_anchor", fake_one_anchor)

    # Tiny isolated source skill tree so the per-anchor copytree is trivial;
    # the measured wall then reflects the parallel worker bodies, not a
    # one-time real .agents/skills copy.
    src_skills = tmp_path / "src_agents" / ".agents" / "skills" / "demo"
    src_skills.mkdir(parents=True, exist_ok=True)
    (src_skills / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")

    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "SENTINEL": "keep",
        "EVO_SKILLS_ROOT": str(src_skills.parent),
    }
    parent_env_snapshot = dict(base_env)
    osenv_snapshot = dict(os.environ)

    monkeypatch.setenv("EVO_SKILL_WORKERS", "4")

    run_root = tmp_path / "run"
    run_root.mkdir(parents=True, exist_ok=True)

    args = SimpleNamespace(run_id="testrun")

    t0 = time.monotonic()
    records = run_publication._run_anchor_pool(
        skills,
        base_env=base_env,
        run_root=run_root,
        args=args,
    )
    wall = time.monotonic() - t0

    # (a) wall < serial: 4 x 0.3s serial would be >= 1.2s; parallel < 0.9s.
    assert wall < 0.9, f"pool not parallel: wall={wall:.3f}s"

    # (b) all 4 skills produced a result, keyed correctly + deterministic order.
    assert [r["skill"] for r in records] == [s["skill"] for s in skills]
    assert all(r["ok"] for r in records)
    assert set(seen) == {s["skill"] for s in skills}

    # (c) each anchor saw a DISTINCT EVO_PROJECT_ROOT and EVO_SKILLS_ROOT.
    proj_roots = [v["EVO_PROJECT_ROOT"] for v in seen.values()]
    skl_roots = [v["EVO_SKILLS_ROOT"] for v in seen.values()]
    assert len(set(proj_roots)) == 4, f"project roots not distinct: {proj_roots}"
    assert len(set(skl_roots)) == 4, f"skills roots not distinct: {skl_roots}"
    # The skills root must live under its own project root.
    for v in seen.values():
        assert v["EVO_SKILLS_ROOT"].startswith(v["EVO_PROJECT_ROOT"])

    # (d) shared parent env / os.environ were NOT mutated. The shared dict must
    # be byte-identical after the run (no per-anchor EVO_PROJECT_ROOT added, no
    # per-anchor EVO_SKILLS_ROOT clobbering the source pointer).
    assert base_env == parent_env_snapshot, "shared base_env mutated by a worker"
    assert "EVO_PROJECT_ROOT" not in base_env
    assert base_env["EVO_SKILLS_ROOT"] == parent_env_snapshot["EVO_SKILLS_ROOT"]
    leaked = {
        k: os.environ.get(k)
        for k in ("EVO_PROJECT_ROOT", "EVO_SKILLS_ROOT")
        if os.environ.get(k) != osenv_snapshot.get(k)
    }
    assert not leaked, f"os.environ leaked per-anchor roots: {leaked}"
