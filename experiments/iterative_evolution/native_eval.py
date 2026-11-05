"""Native runtime eval for EvoSkill / Memento / MemP."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "experiments"))

from _shared.native_framework_runners import (  # noqa: E402
    run_evoskill_skill_dir_test,
    run_evoskill_test,
    run_hermes_test,
    run_memento_test,
    run_memp_test,
)

from .eval_lanes import NATIVE_RUNTIME, normalize_framework  # noqa: E402

LABEL_NATIVE = "native_retrieval"
LABEL_UPPER_BOUND = "native_retrieval_upper_bound"
LABEL_EVOSKILL = "native_evoskill_skillset"
LABEL_EVOSKILL_AGENT = "native_evoskill_best_program"


def _apply_leave_one_out_memento(
    test_home: Path, leave_one_out_task_id: str | None
) -> tuple[bool, bool]:
    """Drop skill artifacts tagged with matching task id in metadata if present."""
    if not leave_one_out_task_id:
        return False, False
    skills = test_home / "memento_s" / "skills"
    if not skills.is_dir():
        return False, False
    matched = False
    applied = False
    for md in sorted(skills.glob("*/SKILL.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if leave_one_out_task_id not in text and leave_one_out_task_id != md.parent.name:
            continue
        matched = True
        shutil.rmtree(md.parent, ignore_errors=True)
        applied = True
    return applied, matched


def _apply_leave_one_out_memp(
    memory_dir: Path, leave_one_out_task_id: str | None
) -> tuple[bool, bool]:
    if not leave_one_out_task_id or not memory_dir.is_dir():
        return False, False
    docs_paths = [
        p
        for p in (memory_dir / "direct" / "documents.json", memory_dir / "documents.json")
        if p.is_file()
    ]
    if not docs_paths:
        return False, False
    import json

    matched = False
    applied = False
    for docs_path in docs_paths:
        data = json.loads(docs_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue
        filtered = [
            d
            for d in data
            if str((d or {}).get("metadata", {}).get("task_id", "")) != leave_one_out_task_id
        ]
        if len(filtered) != len(data):
            matched = True
            docs_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
            applied = True
    return applied, matched


def _native_label(*, leave_one_out_task_id: str | None, applied: bool, matched: bool) -> str:
    if applied:
        return LABEL_NATIVE
    if leave_one_out_task_id and matched:
        return LABEL_UPPER_BOUND
    return LABEL_NATIVE


def _load_evoskill_skill_bodies(state_dir: Path) -> list[str]:
    """Load EvoSkill's native skill set from a produced-state directory."""
    root = Path(state_dir)
    candidates = [root]
    for rel in ("skills", ".claude/skills"):
        child = root / rel
        if child.is_dir():
            candidates.append(child)
    seen: set[Path] = set()
    bodies: list[str] = []
    for base in candidates:
        for p in sorted(base.glob("*/SKILL.md")):
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            bodies.append(p.read_text(encoding="utf-8", errors="replace"))
    return bodies


def eval_native(
    *,
    framework: str,
    task_dir: Path,
    instruction: str,
    cache_dir: Path,
    solver_cfg: dict[str, Any],
    api_key: str,
    post_train_state: Path,
    embedding_cfg: dict[str, Any] | None = None,
    reflector_cfg: dict[str, Any] | None = None,
    emb_key: str = "",
    refl_key: str = "",
    leave_one_out_task_id: str | None = None,
    evoskill_repo_dir: Path | None = None,
    evoskill_condition: str = "best_program",
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Run Table B native eval; label LOO vs upper-bound."""
    fw = normalize_framework(framework)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not Path(post_train_state).is_dir():
        return {
            "outcome": "hard_fail",
            "error": f"native post_train_state missing: {post_train_state}",
            "eval_label": LABEL_UPPER_BOUND,
            "eval_lane": NATIVE_RUNTIME,
            "cache_dir": str(cache_dir),
            "leave_one_out_applied": False,
            "leave_one_out_matched": False,
            "leave_one_out_task_id": leave_one_out_task_id or "",
        }
    loo_applied = False
    loo_matched = False
    if fw == "evoskill":
        if evoskill_repo_dir is not None:
            res = run_evoskill_test(
                task_dir=task_dir,
                instruction=instruction,
                condition=evoskill_condition,
                cache_dir=cache_dir,
                repo_dir=Path(evoskill_repo_dir),
                solver_cfg=solver_cfg,
                api_key=api_key,
                timeout_s=timeout_s,
            )
            label = LABEL_EVOSKILL_AGENT
            res = dict(res)
            native_metrics = dict(res.get("native_metrics") or {})
            native_metrics.update(
                {
                    "framework": fw,
                    "native_repo_dir": str(evoskill_repo_dir),
                    "evoskill_condition": evoskill_condition,
                }
            )
            res["native_metrics"] = native_metrics
        else:
            bodies = _load_evoskill_skill_bodies(Path(post_train_state))
            if not bodies:
                return {
                    "outcome": "hard_fail",
                    "error": f"evoskill native state has no */SKILL.md files: {post_train_state}",
                    "eval_label": LABEL_EVOSKILL,
                    "eval_lane": NATIVE_RUNTIME,
                    "cache_dir": str(cache_dir),
                    "leave_one_out_applied": False,
                    "leave_one_out_matched": False,
                    "leave_one_out_task_id": leave_one_out_task_id or "",
                    "native_metrics": {
                        "framework": fw,
                        "native_state_dir": str(post_train_state),
                        "skill_count": 0,
                    },
                }
            res = run_evoskill_skill_dir_test(
                task_dir=task_dir,
                instruction=instruction,
                cache_dir=cache_dir,
                skills_dir=Path(post_train_state),
                solver_cfg=solver_cfg,
                api_key=api_key,
                timeout_s=timeout_s,
                native_metrics_extra={
                    "framework": fw,
                    "native_state_dir": str(post_train_state),
                    "skill_count": len(bodies),
                },
            )
            label = LABEL_EVOSKILL
            res = dict(res)
            native_metrics = dict(res.get("native_metrics") or {})
            native_metrics.update(
                {
                    "framework": fw,
                    "native_state_dir": str(post_train_state),
                    "skill_count": len(bodies),
                }
            )
            res["native_metrics"] = native_metrics
    elif fw == "memento":
        work_home = cache_dir / "_memento_eval_home"
        if work_home.exists():
            shutil.rmtree(work_home)
        shutil.copytree(post_train_state, work_home)
        loo_applied, loo_matched = _apply_leave_one_out_memento(work_home, leave_one_out_task_id)
        emb = embedding_cfg or solver_cfg
        res = run_memento_test(
            task_dir=task_dir,
            raw_sbbench_path=task_dir,
            instruction=instruction,
            condition="post_train",
            cache_dir=cache_dir,
            solver_cfg=solver_cfg,
            embedding_cfg=emb,
            api_key=api_key,
            emb_key=emb_key or api_key,
            post_train_test_home=work_home,
            timeout_s=timeout_s,
        )
        label = _native_label(
            leave_one_out_task_id=leave_one_out_task_id,
            applied=loo_applied,
            matched=loo_matched,
        )
    elif fw == "memp":
        work_mem = cache_dir / "_memp_eval_memory"
        if work_mem.exists():
            shutil.rmtree(work_mem)
        shutil.copytree(post_train_state, work_mem)
        loo_applied, loo_matched = _apply_leave_one_out_memp(work_mem, leave_one_out_task_id)
        refl = reflector_cfg or solver_cfg
        emb = embedding_cfg or solver_cfg
        res = run_memp_test(
            task_dir=task_dir,
            raw_sbbench_path=task_dir,
            instruction=instruction,
            condition="post_train",
            cache_dir=cache_dir,
            solver_cfg=solver_cfg,
            reflector_cfg=refl,
            embedding_cfg=emb,
            api_key=api_key,
            refl_key=refl_key or api_key,
            emb_key=emb_key or api_key,
            post_train_memory_dir=work_mem,
            timeout_s=timeout_s,
        )
        label = _native_label(
            leave_one_out_task_id=leave_one_out_task_id,
            applied=loo_applied,
            matched=loo_matched,
        )
    elif fw == "hermes":
        res = run_hermes_test(
            task_dir=task_dir,
            raw_sbbench_path=task_dir,
            instruction=instruction,
            condition="post_train",
            cache_dir=cache_dir,
            solver_cfg=solver_cfg,
            aux_cfg=reflector_cfg or solver_cfg,
            api_key=api_key,
            aux_key=refl_key or api_key,
            post_train_skills_dir=Path(post_train_state),
            max_iterations=90,
            timeout_s=timeout_s,
        )
        label = LABEL_NATIVE
    else:
        raise ValueError(f"native_eval unsupported framework: {framework}")

    res = dict(res)
    res["eval_label"] = label
    res["eval_lane"] = NATIVE_RUNTIME
    res["leave_one_out_applied"] = loo_applied
    res["leave_one_out_matched"] = loo_matched
    res["leave_one_out_task_id"] = leave_one_out_task_id or ""
    res["cache_dir"] = str(cache_dir)
    return res


__all__ = [
    "LABEL_EVOSKILL",
    "LABEL_EVOSKILL_AGENT",
    "LABEL_NATIVE",
    "LABEL_UPPER_BOUND",
    "_load_evoskill_skill_bodies",
    "eval_native",
]
