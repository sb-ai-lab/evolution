"""Table A neutral solver — body-injection via run_evolution_test."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "experiments"))

from _shared.native_framework_runners import run_evolution_test  # noqa: E402

from .canonical_skill import sha256_text  # noqa: E402
from .skill_body import maybe_compact_skill_body  # noqa: E402
from .trace_bundle import bundle_from_solve_result, bundle_path, write_bundle  # noqa: E402

EVAL_LABEL = "skill_body_injection"


def solve_task(
    *,
    task_dir: Path,
    instruction: str,
    skill_body: str,
    skill_id: str,
    skill_version: str,
    round_idx: int,
    task_id: str,
    role: str,
    run_dir: Path,
    solver_cfg: dict[str, Any],
    api_key: str,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Run neutral body-injection solve; write trace bundle."""
    body_for_solve, compacted = maybe_compact_skill_body(
        body=skill_body,
        skill_id=skill_id,
        solver_cfg=solver_cfg,
        api_key=api_key,
    )
    cache_dir = run_dir / "solver_cache" / f"round{round_idx}" / task_id
    if cache_dir.exists():
        import shutil

        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    res = run_evolution_test(
        task_dir=task_dir,
        raw_sbbench_path=task_dir,
        instruction=instruction,
        condition="post_train",
        cache_dir=cache_dir,
        solver_cfg=solver_cfg,
        api_key=api_key,
        post_train_skill_bodies=[body_for_solve],
        timeout_s=timeout_s,
    )
    bundle = bundle_from_solve_result(
        round_idx=round_idx,
        task_id=task_id,
        role=role,
        skill_id=skill_id,
        skill_version=skill_version,
        skill_sha256=sha256_text(skill_body),
        instruction=instruction,
        solve_result=res,
        cache_dir=cache_dir,
    )
    bundle["eval_label"] = EVAL_LABEL
    bundle["body_compacted"] = compacted
    bundle["body_chars_injected"] = len(body_for_solve)
    write_bundle(bundle_path(run_dir, round_idx, task_id), bundle)
    return {**res, "bundle": bundle, "cache_dir": str(cache_dir)}


__all__ = ["EVAL_LABEL", "solve_task"]
