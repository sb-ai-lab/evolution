#!/usr/bin/env python3
"""Subset-driven iterative-evolution sweep.

Runs the iterative_evolution loop while taking task selections from
``experiments/baselines/subset.json``. This preserves the newer iterative
skill/runtime evolution protocol and uses the baselines subset only as the
train/test manifest.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "experiments"))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from _shared.llm_telemetry import add_usage, usage_from_result  # noqa: E402

from iterative_evolution.canonical_skill import (  # noqa: E402
    read_version_body,
    versions_dir,
)
from iterative_evolution.eval_lanes import (  # noqa: E402
    CANONICAL_EXPORT,
    NATIVE_RUNTIME,
    primary_eval_lane,
)
from iterative_evolution.loop import (  # noqa: E402
    _evoskill_repo_dir_for_version,
    native_state_version_dir,
    read_instruction,
    run_cell,
    split_task_ref,
    task_dir_for,
)
from iterative_evolution.native_eval import eval_native  # noqa: E402
from iterative_evolution.neutral_solver import solve_task  # noqa: E402

DEFAULT_FRAMEWORKS = (
    "hermes",
    "codex",
    "claude",
    "memento",
    "memp",
    "evoskill",
    "evolution",
)


@dataclass(frozen=True)
class Job:
    schema: str
    n_train: int
    framework: str
    skill: str


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _csv_arg(text: str) -> list[str]:
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def _schemas_arg(text: str) -> list[int]:
    return [int(x) for x in _csv_arg(text)]


def _api_key(env_name: str, fallback: str = "EMPTY") -> str:
    return os.environ.get(env_name) or os.environ.get("OPENROUTER_API_KEY") or fallback


def _served_id_for(model: str) -> str:
    if model.startswith("openai/openai/"):
        return model.removeprefix("openai/")
    return model


def _reasoning_extra(effort: str) -> dict[str, Any] | None:
    effort = str(effort or "").strip()
    if not effort:
        return None
    return {"reasoning_effort": effort}


def build_model_cfgs(args: argparse.Namespace) -> tuple[dict, dict, dict, dict, str, str, str, str]:
    solver_served = args.solver_served_id or _served_id_for(args.solver_model)
    editor_model = args.editor_model or args.reflector_model
    editor_served = args.editor_served_id or _served_id_for(editor_model)
    solver = {
        "id": args.solver_model,
        "litellm_id": args.solver_model,
        "served_id_hint": solver_served,
        "api_base": args.solver_api_base,
        "max_output_tokens": int(args.max_tokens),
        "temperature": float(args.temperature),
        "eval_concurrency": int(args.model_concurrency),
    }
    solver_extra = _reasoning_extra(args.solver_reasoning_effort)
    if solver_extra:
        solver["extra_body"] = solver_extra
    reflector = {
        "id": args.reflector_model,
        "litellm_id": args.reflector_model,
        "served_id_hint": args.reflector_served_id or _served_id_for(args.reflector_model),
        "api_base": args.reflector_api_base,
        "max_output_tokens": int(args.editor_max_tokens or args.max_tokens),
        "temperature": float(args.editor_temperature),
    }
    reflector_extra = _reasoning_extra(args.editor_reasoning_effort)
    if reflector_extra:
        reflector["extra_body"] = reflector_extra
    embedding = {
        "id": args.embedding_model,
        "api_base": args.embedding_api_base,
        "embedding_id": args.embedding_model,
        "embedding_api_base": args.embedding_api_base,
    }
    editor = {
        "id": editor_model,
        "litellm_id": editor_model,
        "served_id_hint": editor_served,
        "api_base": args.editor_api_base or args.reflector_api_base,
        "max_output_tokens": int(args.editor_max_tokens or args.max_tokens),
        "temperature": float(args.editor_temperature),
        "embedding_id": args.embedding_model,
        "embedding_api_base": args.embedding_api_base,
        "embedding_api_key": args.embedding_api_key,
        "reflector_id": reflector["served_id_hint"],
        "reflector_api_base": reflector["api_base"],
        "reflector_api_key": args.reflector_api_key or _api_key(args.reflector_api_key_env),
        "evoskill_concurrency": int(args.model_concurrency),
        "evoskill_iterations": int(args.evoskill_iterations),
    }
    editor_extra = _reasoning_extra(args.editor_reasoning_effort)
    if editor_extra:
        editor["extra_body"] = editor_extra
    solver_key = _api_key(args.solver_api_key_env)
    editor_key = _api_key(args.editor_api_key_env)
    emb_key = os.environ.get(args.embedding_api_key_env, args.embedding_api_key or "EMPTY")
    refl_key = _api_key(args.reflector_api_key_env)
    return solver, editor, embedding, reflector, solver_key, editor_key, emb_key, refl_key


def subset_for_skill(
    subset: dict[str, Any], skill: str, n_train: int, n_test: int
) -> tuple[list[str], list[str]]:
    block = (subset.get("skills") or {}).get(skill)
    if not isinstance(block, dict):
        raise KeyError(f"skill {skill!r} missing from subset")
    train = list(block.get("train") or [])
    test = list(block.get("test") or [])
    if len(train) < n_train:
        raise ValueError(f"{skill}: subset has {len(train)} train tasks, need {n_train}")
    if len(test) < n_test:
        raise ValueError(f"{skill}: subset has {len(test)} test tasks, need {n_test}")
    return train[:n_train], test[:n_test]


def _rate(result: dict[str, Any]) -> float:
    value = result.get("pass_rate")
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0 if result.get("outcome") == "ok" else 0.0


def _condition_metrics(trials_by_task: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    all_rates: list[float] = []
    best_rates: list[float] = []
    per_task: dict[str, Any] = {}
    for task_ref, trials in sorted(trials_by_task.items()):
        rates = [_rate(t) for t in trials]
        all_rates.extend(rates)
        best = max(rates) if rates else 0.0
        best_rates.append(best)
        per_task[task_ref] = {
            "n_trials": len(trials),
            "pass_rates": rates,
            "best_of_n": best,
            "pass_at_n": best >= 1.0 - 1e-12,
        }
    solved = sum(1 for r in best_rates if r >= 1.0 - 1e-12)
    return {
        "n_tasks": len(best_rates),
        "attempts": len(all_rates),
        "mean_pass_rate": mean(all_rates) if all_rates else None,
        "best_of_n": mean(best_rates) if best_rates else None,
        "pass_at_n": (solved / len(best_rates)) if best_rates else None,
        "pass@5": (solved / len(best_rates)) if best_rates else None,
        "solved_tasks": solved,
        "perfect_pass_rate": (
            mean(1.0 if r >= 1.0 - 1e-12 else 0.0 for r in all_rates) if all_rates else None
        ),
        "per_task": per_task,
    }


def _delta(seed: dict[str, Any], evolved: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("mean_pass_rate", "best_of_n", "pass_at_n", "solved_tasks"):
        a = seed.get(key)
        b = evolved.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[f"delta_{key}"] = b - a
    out["delta_pass@5"] = out.get("delta_pass_at_n")
    return out


def _score_versions(
    *,
    framework: str,
    skill_id: str,
    run_dir: Path,
    corpus_root: Path,
    task_refs: list[str],
    split: str,
    n_trials: int,
    solver_cfg: dict[str, Any],
    api_key: str,
    embedding_cfg: dict[str, Any],
    reflector_cfg: dict[str, Any],
    emb_key: str,
    refl_key: str,
    timeout_s: int,
    conditions: dict[str, str],
    lane_override: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[dict[str, Any]]]]]:
    """Score each (condition -> version) on task_refs x n_trials in the primary lane.

    The cache path is keyed by ``condition``, so passing version ids as the
    condition keys (selection) yields collision-free per-version caches.
    """
    fw = framework.strip().lower()
    lane = lane_override or primary_eval_lane(fw)
    raw: dict[str, dict[str, list[dict[str, Any]]]] = {
        condition: defaultdict(list) for condition in conditions
    }
    jobs: list[tuple[str, str, str, int, Any, Path | None, Path | None]] = []
    for condition, version in conditions.items():
        body = None
        if lane == CANONICAL_EXPORT:
            body = read_version_body(run_dir, skill_id, version)
        state = native_state_version_dir(run_dir, fw, version) if lane == NATIVE_RUNTIME else None
        evoskill_repo = (
            _evoskill_repo_dir_for_version(run_dir, version)
            if lane == NATIVE_RUNTIME and fw == "evoskill"
            else None
        )
        for task_ref in task_refs:
            for trial in range(n_trials):
                jobs.append((condition, version, task_ref, trial, body, state, evoskill_repo))

    def run_trial(
        job: tuple[str, str, str, int, Any, Path | None, Path | None],
    ) -> tuple[str, str, dict[str, Any]]:
        condition, version, task_ref, trial, body, state, evoskill_repo = job
        task_role, _plain, stable_ref = split_task_ref("", task_ref)
        task_dir = task_dir_for(corpus_root=corpus_root, role="", task_id=task_ref)
        instruction = read_instruction(task_dir)
        sc = dict(solver_cfg)
        sc["seed"] = 10_000 + trial
        cache_root = run_dir / "final_eval" / split / lane / condition / f"trial{trial}"
        try:
            if lane == CANONICAL_EXPORT:
                res = solve_task(
                    task_dir=task_dir,
                    instruction=instruction,
                    skill_body=str(body),
                    skill_id=skill_id,
                    skill_version=version,
                    round_idx=trial,
                    task_id=stable_ref,
                    role=task_role,
                    run_dir=cache_root,
                    solver_cfg=sc,
                    api_key=api_key,
                    timeout_s=timeout_s,
                )
                res["eval_lane"] = CANONICAL_EXPORT
            else:
                res = eval_native(
                    framework=fw,
                    task_dir=task_dir,
                    instruction=instruction,
                    cache_dir=cache_root / stable_ref,
                    solver_cfg=sc,
                    api_key=api_key,
                    post_train_state=Path(state),
                    embedding_cfg=embedding_cfg,
                    reflector_cfg=reflector_cfg,
                    emb_key=emb_key,
                    refl_key=refl_key,
                    leave_one_out_task_id=stable_ref if split == "test" else None,
                    evoskill_repo_dir=evoskill_repo,
                    evoskill_condition="best_program",
                    timeout_s=timeout_s,
                )
        except Exception as exc:
            res = {
                "outcome": "hard_fail",
                "pass_rate": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
                "eval_lane": lane,
            }
        return (
            condition,
            stable_ref,
            {
                "task_ref": stable_ref,
                "trial": trial,
                "pass_rate": _rate(res),
                "outcome": res.get("outcome"),
                "error": res.get("error"),
                "cache_dir": res.get("cache_dir"),
                "eval_label": res.get("eval_label"),
                "leave_one_out_applied": res.get("leave_one_out_applied"),
                "llm_usage": usage_from_result(res),
            },
        )

    max_workers = max(
        1, int(solver_cfg.get("eval_concurrency") or os.environ.get("EVO_CONCURRENCY", "1"))
    )
    if lane == NATIVE_RUNTIME:
        cap_raw = os.environ.get(
            f"EVO_{fw.upper()}_FINAL_EVAL_PARALLEL",
            os.environ.get("EVO_NATIVE_FINAL_EVAL_PARALLEL", str(max_workers)),
        )
        try:
            max_workers = min(max_workers, max(1, int(cap_raw)))
        except ValueError:
            max_workers = min(max_workers, 5)
    max_workers = min(max_workers, len(jobs) or 1)
    if max_workers <= 1:
        for job in jobs:
            condition, stable_ref, rec = run_trial(job)
            raw[condition][stable_ref].append(rec)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(run_trial, job) for job in jobs]
            for fut in as_completed(futures):
                condition, stable_ref, rec = fut.result()
                raw[condition][stable_ref].append(rec)
    return {condition: _condition_metrics(raw[condition]) for condition in conditions}, raw


def _available_versions(run_dir: Path, skill_id: str) -> list[str]:
    vdir = versions_dir(run_dir, skill_id)
    if not vdir.is_dir():
        return ["v0"]
    vs = [
        p.name
        for p in vdir.iterdir()
        if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit()
    ]
    return sorted(vs, key=lambda v: int(v[1:])) or ["v0"]


def select_best_version_on_train(
    *,
    framework: str,
    skill_id: str,
    run_dir: Path,
    corpus_root: Path,
    train_refs: list[str],
    n_trials: int,
    solver_cfg: dict[str, Any],
    api_key: str,
    embedding_cfg: dict[str, Any],
    reflector_cfg: dict[str, Any],
    emb_key: str,
    refl_key: str,
    timeout_s: int,
) -> tuple[str, dict[str, float]]:
    """Multi-trial best-on-train selection (no val split).

    Scores every produced version on the train tasks over ``EVO_SELECT_TRIALS``
    trials and returns the best by mean pass_rate; ties resolve to the lowest
    version index, so the seed (v0) is kept unless an evolved version *strictly*
    improves train pass_rate. Removes the single-trial coin-flip in
    ``_pick_from_round_records`` that kept v0 on a 1.0/1.0 tie.
    """
    versions = _available_versions(run_dir, skill_id)
    n_select = int(os.environ.get("EVO_SELECT_TRIALS", str(n_trials)))
    metrics, _ = _score_versions(
        framework=framework,
        skill_id=skill_id,
        run_dir=run_dir,
        corpus_root=corpus_root,
        task_refs=train_refs,
        split="select",
        n_trials=n_select,
        solver_cfg=solver_cfg,
        api_key=api_key,
        embedding_cfg=embedding_cfg,
        reflector_cfg=reflector_cfg,
        emb_key=emb_key,
        refl_key=refl_key,
        timeout_s=timeout_s,
        conditions={v: v for v in versions},
    )
    scores = {v: float(metrics[v].get("mean_pass_rate") or 0.0) for v in versions}
    best = max(versions, key=lambda v: (scores[v], -int(v[1:])))
    return best, scores


def eval_repeated_primary(
    *,
    framework: str,
    skill_id: str,
    run_dir: Path,
    corpus_root: Path,
    task_refs: list[str],
    split: str,
    n_trials: int,
    solver_cfg: dict[str, Any],
    api_key: str,
    embedding_cfg: dict[str, Any],
    reflector_cfg: dict[str, Any],
    emb_key: str,
    refl_key: str,
    timeout_s: int,
    best_version: str,
    lane_override: str | None = None,
) -> dict[str, Any]:
    lane = lane_override or primary_eval_lane(framework.strip().lower())
    metrics, raw = _score_versions(
        framework=framework,
        skill_id=skill_id,
        run_dir=run_dir,
        corpus_root=corpus_root,
        task_refs=task_refs,
        split=split,
        n_trials=n_trials,
        solver_cfg=solver_cfg,
        api_key=api_key,
        embedding_cfg=embedding_cfg,
        reflector_cfg=reflector_cfg,
        emb_key=emb_key,
        refl_key=refl_key,
        timeout_s=timeout_s,
        conditions={"seed": "v0", "evolved": best_version},
        lane_override=lane,
    )
    seed_metrics = metrics["seed"]
    evolved_metrics = metrics["evolved"]
    usage_by_condition = {
        condition: add_usage(
            *(rec.get("llm_usage") for recs in raw[condition].values() for rec in recs)
        )
        for condition in ("seed", "evolved")
    }
    return {
        "split": split,
        "primary_eval_lane": lane,
        "n_trials": n_trials,
        "seed": seed_metrics,
        "evolved": evolved_metrics,
        "delta": _delta(seed_metrics, evolved_metrics),
        "llm_usage": {
            **usage_by_condition,
            "total": add_usage(*usage_by_condition.values()),
        },
        "trials": raw,
    }


def _eval_all_failed(*evals: dict[str, Any]) -> bool:
    """True when every eval trial across all conditions failed to solve."""
    seen = False
    for ev in evals:
        for condition in ("seed", "evolved"):
            for recs in (ev.get("trials", {}).get(condition) or {}).values():
                for rec in recs:
                    seen = True
                    if rec.get("outcome") == "ok":
                        return False
    return seen


_NATIVE_LIBRARY_FRAMEWORKS = {"memento", "evoskill", "memp"}


def collect_train_memory_md(
    run_dir: Path, framework: str, skill_id: str, best_version: str
) -> tuple[str, dict[str, Any]]:
    """Aggregate a framework's full train-built memory into one SKILL.md.

    Single-file frameworks (hermes/...) already evolve one SKILL.md, so the
    collected artifact is their best canonical version. Native frameworks build a skill
    library / memory store during training; fold the whole thing into one body so it can
    be scored by the shared neutral solver on the same axis. Reads on-disk artifacts only
    (no fabrication); an empty library yields the seed body (honest no-op).
    """
    from evolution.adapters.skill_artifact_export import (
        fold_evolved_skills,
        list_evolved_skill_dirs,
        split_frontmatter,
    )
    from iterative_evolution.canonical_skill import read_canonical
    from iterative_evolution.loop import native_state_dir

    fw = framework.strip().lower()
    if fw not in _NATIVE_LIBRARY_FRAMEWORKS:
        body = read_version_body(run_dir, skill_id, best_version)
        return body, {"source": "canonical_version", "n_items": 0, "items": []}

    if fw in {"evoskill", "memento"}:
        skills_root = native_state_dir(run_dir, fw)
        if fw == "memento":
            skills_root = skills_root / "memento_s" / "skills"
        anchor_md = skills_root / skill_id / "SKILL.md"
        base = (
            anchor_md.read_text(encoding="utf-8", errors="replace")
            if anchor_md.is_file()
            else read_canonical(run_dir, skill_id)
        )
        dirs = list_evolved_skill_dirs(skills_root, skill_id)
        body = fold_evolved_skills(base, dirs)
        return body, {
            "source": f"{fw}_skill_library",
            "n_items": len(dirs),
            "items": [d.name for d in dirs],
        }

    # memp: procedural-memory store. The memory memp actually retrieves from is
    # memory/direct/documents.json (LangChain docs: page_content + metadata.workflow);
    # memory/documents.json is a base-skill-only fallback. Prefer the former, read the
    # distilled procedure from metadata.workflow/page_content, and skip the base_skill doc
    # so we fold only LEARNED workflows, not a duplicate of the anchor body.
    base = read_canonical(run_dir, skill_id)
    memory_dir = native_state_dir(run_dir, fw)
    # When the native δ-gate reverted native_state to v0 (best-native < evolved),
    # the learned memory lives in native_state_versions/memp/v{N}. Use the
    # highest-numbered versioned snapshot with direct/documents.json so the
    # canonical export axis sees the most-evolved memory, not the seed.
    _versions_root = run_dir / "native_state_versions" / fw
    if _versions_root.is_dir():
        _versioned = sorted(
            [p for p in _versions_root.iterdir() if p.is_dir() and p.name.startswith("v")],
            key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0,
            reverse=True,
        )
        for _vp in _versioned:
            if (_vp / "direct" / "documents.json").is_file():
                memory_dir = _vp
                break
    docs_path = memory_dir / "direct" / "documents.json"
    if not docs_path.is_file():
        docs_path = memory_dir / "documents.json"
    items: list[str] = []
    sections: list[str] = []
    if docs_path.is_file():
        try:
            docs = json.loads(docs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            docs = []
        for i, doc in enumerate(docs if isinstance(docs, list) else []):
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            if str(meta.get("source") or "").startswith("base_skill"):
                continue  # the base skill == the anchor body; folding it would duplicate
            page = str(doc.get("page_content") or "").strip()
            text = str(
                meta.get("workflow") or page or doc.get("content") or doc.get("text") or ""
            ).strip()
            name = (page.splitlines()[0].lstrip("#").strip() if page else "") or str(
                doc.get("id") or doc.get("title") or meta.get("source") or f"memory-{i}"
            )
            if text:
                items.append(name)
                sections.append(f"## Evolved memory: {name}\n\n{text}")
    if not sections:
        return base, {"source": "memp_memory", "n_items": 0, "items": []}
    fm_block, anchor_body = split_frontmatter(base)
    anchor_body = anchor_body.strip()
    joined = "\n\n".join(sections)
    composed = f"{anchor_body}\n\n{joined}" if anchor_body else joined
    body = f"{fm_block}\n{composed}\n" if fm_block else composed + "\n"
    return body, {"source": "memp_memory", "n_items": len(items), "items": items}


def eval_canonical_collected(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    corpus_root: Path,
    train_refs: list[str],
    test_refs: list[str],
    solver_cfg: dict[str, Any],
    api_key: str,
    embedding_cfg: dict[str, Any],
    reflector_cfg: dict[str, Any],
    emb_key: str,
    refl_key: str,
    timeout_s: int,
    best_version: str,
    primary_train: dict[str, Any],
    primary_test: dict[str, Any],
) -> dict[str, Any]:
    """Score the framework's collected memory on the shared neutral solver (one axis)."""
    from iterative_evolution.canonical_skill import sha256_text

    fw = args.framework.strip().lower()
    body, meta = collect_train_memory_md(run_dir, args.framework, args.skill, best_version)
    coll_path = versions_dir(run_dir, args.skill) / "collected" / "SKILL.md"
    coll_path.parent.mkdir(parents=True, exist_ok=True)
    coll_path.write_text(body, encoding="utf-8")
    out: dict[str, Any] = {
        "axis": "canonical_shared_solver",
        "body_path": str(coll_path),
        "body_sha256": sha256_text(body),
        "body_chars": len(body),
        **meta,
    }
    if fw not in _NATIVE_LIBRARY_FRAMEWORKS:
        # single-file frameworks already score on the shared solver as their primary lane
        out["same_as_primary"] = True
        out["train"] = primary_train
        out["test"] = primary_test
        return out
    for split, refs in (("train", train_refs), ("test", test_refs)):
        out[split] = eval_repeated_primary(
            framework=args.framework,
            skill_id=args.skill,
            run_dir=run_dir,
            corpus_root=corpus_root,
            task_refs=refs,
            split=f"collected_{split}",
            n_trials=args.n_trials,
            solver_cfg=solver_cfg,
            api_key=api_key,
            embedding_cfg=embedding_cfg,
            reflector_cfg=reflector_cfg,
            emb_key=emb_key,
            refl_key=refl_key,
            timeout_s=timeout_s,
            best_version="collected",
            lane_override=CANONICAL_EXPORT,
        )
    return out


def run_one_cell(args: argparse.Namespace) -> int:
    os.environ.setdefault("EVO_REWRITE_MIN_BODY_FRAC", "0.25")
    os.environ.setdefault("EVO_REFLECT_BLIND", "0")
    os.environ.setdefault("EVO_REFLECT_ALLOW_ORACLE_ON_PAPER", "1")
    os.environ.setdefault("EVO_DISABLE_LEAK_SCANNER", "1")
    os.environ.setdefault("EVO_MEMENTO_NATIVE_SERVICES_PROFILE", "native_reflection")
    subset = _read_json(args.subset)
    train_refs, test_refs = subset_for_skill(subset, args.skill, args.n_train, args.n_test)
    run_dir = (
        args.root / args.schema / "cells" / args.framework.strip().lower() / args.skill
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = run_dir / "cell_metrics.json"
    if metrics_path.is_file() and not args.force:
        print(json.dumps({"run_dir": str(run_dir), "skipped": "cell_metrics_exists"}), flush=True)
        return 0
    stale_crash = run_dir / "cell_crash.json"
    if stale_crash.exists():
        stale_crash.unlink()
    solver, editor, embedding, reflector, api_key, editor_key, emb_key, refl_key = build_model_cfgs(
        args
    )
    cell_manifest = {
        "schema": args.schema,
        "framework": args.framework,
        "skill": args.skill,
        "n_train": args.n_train,
        "n_test": args.n_test,
        "train_refs": train_refs,
        "test_refs": test_refs,
        "subset": str(args.subset.resolve()),
        "corpus_root": str(args.corpus.resolve()),
        "rounds": args.rounds,
        "n_trials": args.n_trials,
        "models": {
            "solver": solver,
            "editor": editor,
            "reflector": reflector,
            "embedding": embedding,
        },
    }
    _write_json(run_dir / "cell_manifest.json", cell_manifest)
    if args.dry_run:
        _write_json(run_dir / "cell_metrics.json", {"dry_run": True, **cell_manifest})
        return 0

    summary = run_cell(
        framework=args.framework,
        skill_id=args.skill,
        role="",
        train_ids=train_refs,
        test_ids=test_refs,
        n_rounds=args.rounds,
        solver_cfg=solver,
        editor_cfg=editor,
        api_key=api_key,
        editor_api_key=editor_key,
        run_dir=run_dir,
        corpus_root=args.corpus,
        embedding_cfg=embedding,
        reflector_cfg=reflector,
        emb_key=emb_key,
        refl_key=refl_key,
        timeout_s=args.timeout_s,
        continue_on_unchanged=True,
        skip_test_eval=True,
        dry_run=False,
    )
    best_version, select_scores = select_best_version_on_train(
        framework=args.framework,
        skill_id=args.skill,
        run_dir=run_dir,
        corpus_root=args.corpus,
        train_refs=train_refs,
        n_trials=args.n_trials,
        solver_cfg=solver,
        api_key=api_key,
        embedding_cfg=embedding,
        reflector_cfg=reflector,
        emb_key=emb_key,
        refl_key=refl_key,
        timeout_s=args.timeout_s,
    )
    round_records_pick = str(summary.get("best_version") or "v0")
    train_eval = eval_repeated_primary(
        framework=args.framework,
        skill_id=args.skill,
        run_dir=run_dir,
        corpus_root=args.corpus,
        task_refs=train_refs,
        split="train",
        n_trials=args.n_trials,
        solver_cfg=solver,
        api_key=api_key,
        embedding_cfg=embedding,
        reflector_cfg=reflector,
        emb_key=emb_key,
        refl_key=refl_key,
        timeout_s=args.timeout_s,
        best_version=best_version,
    )
    test_eval = eval_repeated_primary(
        framework=args.framework,
        skill_id=args.skill,
        run_dir=run_dir,
        corpus_root=args.corpus,
        task_refs=test_refs,
        split="test",
        n_trials=args.n_trials,
        solver_cfg=solver,
        api_key=api_key,
        embedding_cfg=embedding,
        reflector_cfg=reflector,
        emb_key=emb_key,
        refl_key=refl_key,
        timeout_s=args.timeout_s,
        best_version=best_version,
    )
    canonical_collected: dict[str, Any] = {}
    if os.environ.get("EVO_CANONICAL_COLLECTED", "0").strip() not in {"0", "", "false", "no"}:
        try:
            canonical_collected = eval_canonical_collected(
                args=args,
                run_dir=run_dir,
                corpus_root=args.corpus,
                train_refs=train_refs,
                test_refs=test_refs,
                solver_cfg=solver,
                api_key=api_key,
                embedding_cfg=embedding,
                reflector_cfg=reflector,
                emb_key=emb_key,
                refl_key=refl_key,
                timeout_s=args.timeout_s,
                best_version=best_version,
                primary_train=train_eval,
                primary_test=test_eval,
            )
        except Exception as exc:  # optional shared-solver axis must never break the cell
            canonical_collected = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    produce_usage = summary.get("llm_usage") or {}
    llm_usage = {
        "train_rounds": produce_usage.get("train_rounds") or add_usage(),
        "editor": produce_usage.get("editor") or add_usage(),
        "eval_train": (train_eval.get("llm_usage") or {}).get("total") or add_usage(),
        "eval_test": (test_eval.get("llm_usage") or {}).get("total") or add_usage(),
    }
    llm_usage["total"] = add_usage(*llm_usage.values())
    metrics = {
        **cell_manifest,
        "best_version": best_version,
        "version_selection": {
            "method": "multi_trial_best_on_train",
            "n_select_trials": int(os.environ.get("EVO_SELECT_TRIALS", str(args.n_trials))),
            "scores": select_scores,
            "round_records_pick": round_records_pick,
        },
        "primary_eval_lane": primary_eval_lane(args.framework),
        "summary_path": str(run_dir / "summary.json"),
        "train": train_eval,
        "test": test_eval,
        "canonical_collected": canonical_collected,
        "llm_usage": llm_usage,
        "completed_at": _now(),
    }
    _write_json(run_dir / "cell_metrics.json", metrics)
    if _eval_all_failed(train_eval, test_eval):
        # every trial hard-failed (e.g. llm-fail): never report this as a clean cell
        print(
            f"[FATAL] all eval trials failed for {args.framework}/{args.skill}; "
            "see cell_metrics.json trials for errors",
            flush=True,
        )
        return 2
    stale_crash = run_dir / "cell_crash.json"
    if stale_crash.exists():
        stale_crash.unlink()
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "framework": args.framework,
                "skill": args.skill,
                "best_version": best_version,
                "train_delta": train_eval["delta"],
                "test_delta": test_eval["delta"],
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    return 0


def _flatten_condition(prefix: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_mean_pass_rate": value.get("mean_pass_rate"),
        f"{prefix}_best_of_n": value.get("best_of_n"),
        f"{prefix}_pass_at_n": value.get("pass_at_n"),
        f"{prefix}_pass@5": value.get("pass@5"),
        f"{prefix}_solved_tasks": value.get("solved_tasks"),
        f"{prefix}_attempts": value.get("attempts"),
        f"{prefix}_n_tasks": value.get("n_tasks"),
    }


def _flatten_usage(prefix: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    return {
        f"{prefix}_llm_calls": int(usage.get("llm_calls") or 0),
        f"{prefix}_tokens_in": int(usage.get("tokens_in") or 0),
        f"{prefix}_tokens_out": int(usage.get("tokens_out") or 0),
        f"{prefix}_latency_ms": float(usage.get("latency_ms") or 0.0),
    }


def collect_metrics(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("train*/cells/*/*/cell_metrics.json")):
        data = _read_json(path)
        if data.get("dry_run"):
            continue
        cc = data.get("canonical_collected") or {}
        for split in ("train", "test"):
            block = data.get(split) or {}
            seed = block.get("seed") or {}
            evolved = block.get("evolved") or {}
            cell_usage = data.get("llm_usage") or {}
            # shared-solver axis: every framework's collected memory scored by the same
            # neutral solver (collected labelled "evolved" by eval_repeated_primary).
            cc_block = cc.get(split) or {}
            cc_seed = cc_block.get("seed") or {}
            cc_coll = cc_block.get("evolved") or {}
            row = {
                "schema": data.get("schema"),
                "framework": data.get("framework"),
                "skill": data.get("skill"),
                "split": split,
                "primary_eval_lane": data.get("primary_eval_lane"),
                "best_version": data.get("best_version"),
                "shared_solver_source": cc.get("source"),
                "shared_solver_n_items": cc.get("n_items"),
                "shared_seed_mean_pass_rate": cc_seed.get("mean_pass_rate"),
                "shared_collected_mean_pass_rate": cc_coll.get("mean_pass_rate"),
                "shared_delta_mean_pass_rate": (cc_block.get("delta") or {}).get(
                    "delta_mean_pass_rate"
                ),
                **_flatten_condition("seed", seed),
                **_flatten_condition("evolved", evolved),
                **(block.get("delta") or {}),
                **_flatten_usage("eval", (block.get("llm_usage") or {}).get("total")),
                **_flatten_usage(
                    "cell_produce",
                    add_usage(cell_usage.get("train_rounds"), cell_usage.get("editor")),
                ),
                **_flatten_usage("cell_total", cell_usage.get("total")),
                "cell_metrics_path": str(path),
            }
            rows.append(row)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["schema"]), str(row["framework"]), str(row["split"]))].append(row)
    summary: list[dict[str, Any]] = []
    metric_keys = (
        "seed_mean_pass_rate",
        "evolved_mean_pass_rate",
        "delta_mean_pass_rate",
        "seed_best_of_n",
        "evolved_best_of_n",
        "delta_best_of_n",
        "seed_pass_at_n",
        "evolved_pass_at_n",
        "delta_pass_at_n",
        "shared_seed_mean_pass_rate",
        "shared_collected_mean_pass_rate",
        "shared_delta_mean_pass_rate",
    )
    for (schema, framework, split), items in sorted(groups.items()):
        rec: dict[str, Any] = {
            "schema": schema,
            "framework": framework,
            "split": split,
            "n_cells": len(items),
            "seed_solved_tasks": sum(int(x.get("seed_solved_tasks") or 0) for x in items),
            "evolved_solved_tasks": sum(int(x.get("evolved_solved_tasks") or 0) for x in items),
            "delta_solved_tasks": sum(int(x.get("delta_solved_tasks") or 0) for x in items),
        }
        for key in metric_keys:
            vals = [float(x[key]) for x in items if isinstance(x.get(key), (int, float))]
            rec[key] = mean(vals) if vals else None
        rec["seed_pass@5"] = rec["seed_pass_at_n"]
        rec["evolved_pass@5"] = rec["evolved_pass_at_n"]
        rec["delta_pass@5"] = rec["delta_pass_at_n"]
        for prefix in ("eval", "cell_produce", "cell_total"):
            for field in ("llm_calls", "tokens_in", "tokens_out", "latency_ms"):
                key = f"{prefix}_{field}"
                rec[key] = round(sum(float(x.get(key) or 0) for x in items), 1)
        summary.append(rec)
    report = {
        "root": str(root),
        "updated_at": _now(),
        "rows": rows,
        "summary": summary,
    }
    _write_json(root / "metrics_latest.json", report)
    write_csv(root / "metrics_latest.rows.csv", rows)
    write_csv(root / "metrics_latest.summary.csv", summary)
    return report


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _job_log(root: Path, job: Job) -> Path:
    return root / job.schema / "logs" / f"{job.framework}__{job.skill}.log"


def _job_done(root: Path, job: Job) -> bool:
    return (root / job.schema / "cells" / job.framework / job.skill / "cell_metrics.json").is_file()


def _per_cell_model_concurrency(args: argparse.Namespace) -> int:
    """Approximate a global model budget across concurrently running cells."""
    outer = max(1, int(getattr(args, "parallel", 1) or 1))
    global_budget = max(1, int(getattr(args, "model_concurrency", 1) or 1))
    return max(1, global_budget // outer)


def _launch(args: argparse.Namespace, job: Job, attempt: int) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    per_cell_concurrency = _per_cell_model_concurrency(args)
    env.setdefault("EVO_REFLECT_BLIND", "0")
    env.setdefault("EVO_REFLECT_ALLOW_ORACLE_ON_PAPER", "1")
    env.setdefault("EVO_DISABLE_LEAK_SCANNER", "1")
    env.setdefault("EVO_REWRITE_MIN_BODY_FRAC", "0.25")
    env["EVO_CONCURRENCY"] = str(per_cell_concurrency)
    env["EVOSKILL_CONCURRENCY"] = str(per_cell_concurrency)
    env["HERMES_TRAIN_PARALLEL"] = str(per_cell_concurrency)
    env["MEMENTO_TRAIN_PARALLEL"] = str(per_cell_concurrency)
    env.setdefault("EVO_NATIVE_FINAL_EVAL_PARALLEL", str(per_cell_concurrency))
    env["PYTHONUNBUFFERED"] = "1"
    log = _job_log(args.root, job)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(
            f"=== START schema={job.schema} framework={job.framework} "
            f"skill={job.skill} attempt={attempt} at={_now()} ===\n"
        )
    entry_script = Path(getattr(args, "entry_script", "") or __file__).resolve()
    cmd = [
        args.python,
        str(entry_script),
        "--run-cell",
        "--root",
        str(args.root),
        "--subset",
        str(args.subset),
        "--corpus",
        str(args.corpus),
        "--schema",
        job.schema,
        "--n-train",
        str(job.n_train),
        "--n-test",
        str(args.n_test),
        "--framework",
        job.framework,
        "--skill",
        job.skill,
        "--rounds",
        str(args.rounds),
        "--n-trials",
        str(args.n_trials),
        "--timeout-s",
        str(args.timeout_s),
        "--solver-model",
        args.solver_model,
        "--solver-served-id",
        args.solver_served_id,
        "--solver-api-base",
        args.solver_api_base,
        "--reflector-model",
        args.reflector_model,
        "--reflector-served-id",
        args.reflector_served_id,
        "--reflector-api-base",
        args.reflector_api_base,
        "--editor-model",
        args.editor_model,
        "--editor-served-id",
        args.editor_served_id,
        "--editor-api-base",
        args.editor_api_base,
        "--embedding-model",
        args.embedding_model,
        "--embedding-api-base",
        args.embedding_api_base,
        "--model-concurrency",
        str(per_cell_concurrency),
        "--max-tokens",
        str(args.max_tokens),
        "--editor-max-tokens",
        str(args.editor_max_tokens),
        "--temperature",
        str(args.temperature),
        "--editor-temperature",
        str(args.editor_temperature),
        "--solver-reasoning-effort",
        args.solver_reasoning_effort,
        "--editor-reasoning-effort",
        args.editor_reasoning_effort,
    ]
    fh = log.open("ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._iter_log_fh = fh  # type: ignore[attr-defined]
    proc._iter_started_at = time.time()  # type: ignore[attr-defined]
    return proc


def _close_proc(proc: subprocess.Popen[bytes]) -> None:
    fh = getattr(proc, "_iter_log_fh", None)
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass


def _write_progress(
    args: argparse.Namespace,
    *,
    jobs_total: int,
    complete: int,
    pending: int,
    running: dict[Job, subprocess.Popen[bytes]],
    failed: list[dict[str, Any]],
    started_at: float,
) -> None:
    payload = {
        "updated_at": _now(),
        "root": str(args.root),
        "jobs_total": jobs_total,
        "complete": complete,
        "pending": pending,
        "failed_count": len(failed),
        "failed": failed,
        "running": [
            {
                **asdict(job),
                "pid": proc.pid,
                "elapsed_s": round(time.time() - getattr(proc, "_iter_started_at", started_at), 1),
            }
            for job, proc in sorted(
                running.items(), key=lambda item: (item[0].schema, item[0].framework, item[0].skill)
            )
        ],
        "elapsed_s": round(time.time() - started_at, 1),
    }
    _write_json(args.root / "progress.json", payload)
    print(
        "PROGRESS "
        f"complete={complete}/{jobs_total} pending={pending} "
        f"running={len(running)} failed={len(failed)} "
        f"elapsed_s={payload['elapsed_s']}",
        flush=True,
    )


def supervise(args: argparse.Namespace) -> int:
    subset = _read_json(args.subset)
    anchors = _csv_arg(args.anchors) or sorted((subset.get("skills") or {}).keys())
    frameworks = _csv_arg(args.frameworks) or list(DEFAULT_FRAMEWORKS)
    schemas = _schemas_arg(args.schemas)
    args.root.mkdir(parents=True, exist_ok=True)
    config = {
        "started_at": _now(),
        "root": str(args.root),
        "subset": str(args.subset.resolve()),
        "corpus": str(args.corpus.resolve()),
        "schemas": schemas,
        "n_test": args.n_test,
        "anchors": anchors,
        "frameworks": frameworks,
        "rounds": args.rounds,
        "n_trials": args.n_trials,
        "parallel": args.parallel,
        "model_concurrency": args.model_concurrency,
        "per_cell_model_concurrency": _per_cell_model_concurrency(args),
        "timeout_s": args.timeout_s,
        "cell_timeout_s": args.cell_timeout_s,
        "solver_model": args.solver_model,
        "reflector_model": args.reflector_model,
    }
    _write_json(args.root / "sweep_config.json", config)
    jobs = [
        Job(schema=f"train{n_train}", n_train=n_train, framework=fw, skill=skill)
        for n_train in schemas
        for fw in frameworks
        for skill in anchors
    ]
    attempts: dict[Job, int] = {job: 0 for job in jobs}
    pending = [job for job in jobs if not _job_done(args.root, job)]
    complete = len(jobs) - len(pending)
    running: dict[Job, subprocess.Popen[bytes]] = {}
    failed: list[dict[str, Any]] = []
    started_at = time.time()
    last_metrics = time.time()
    last_progress = 0.0

    def launch_more() -> None:
        while pending and len(running) < args.parallel:
            job = pending.pop(0)
            attempts[job] += 1
            running[job] = _launch(args, job, attempts[job])

    try:
        launch_more()
        while pending or running:
            time.sleep(2)
            for job, proc in list(running.items()):
                rc = proc.poll()
                elapsed = time.time() - getattr(proc, "_iter_started_at", started_at)
                if rc is None and args.cell_timeout_s > 0 and elapsed > args.cell_timeout_s:
                    with _job_log(args.root, job).open("a", encoding="utf-8") as fh:
                        fh.write(
                            f"=== CELL TIMEOUT schema={job.schema} framework={job.framework} "
                            f"skill={job.skill} attempt={attempts[job]} "
                            f"elapsed_s={elapsed:.1f} limit_s={args.cell_timeout_s} "
                            f"at={_now()} ===\n"
                        )
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait(timeout=10)
                    rc = proc.returncode if proc.returncode is not None else -124
                if rc is None:
                    continue
                running.pop(job, None)
                _close_proc(proc)
                done = _job_done(args.root, job)
                with _job_log(args.root, job).open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"=== DONE schema={job.schema} framework={job.framework} "
                        f"skill={job.skill} attempt={attempts[job]} rc={rc} "
                        f"metrics={int(done)} at={_now()} ===\n"
                    )
                if rc == 0 and done:
                    complete += 1
                elif attempts[job] <= args.retries:
                    pending.append(job)
                else:
                    complete += 1
                    failed.append(
                        {
                            **asdict(job),
                            "rc": rc,
                            "attempts": attempts[job],
                            "log": str(_job_log(args.root, job)),
                        }
                    )
            launch_more()
            now = time.time()
            if now - last_progress >= 30:
                _write_progress(
                    args,
                    jobs_total=len(jobs),
                    complete=complete,
                    pending=len(pending),
                    running=running,
                    failed=failed,
                    started_at=started_at,
                )
                last_progress = now
            if now - last_metrics >= 300:
                collect_metrics(args.root)
                last_metrics = now
    except KeyboardInterrupt:
        for proc in running.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        raise
    finally:
        _write_progress(
            args,
            jobs_total=len(jobs),
            complete=complete,
            pending=len(pending),
            running=running,
            failed=failed,
            started_at=started_at,
        )
        _write_json(args.root / "failures_latest.json", failed)
        collect_metrics(args.root)
    report = collect_metrics(args.root)
    _write_json(args.root / "metrics_final.json", report)
    _write_json(args.root / "failures_final.json", failed)
    return 0 if not failed else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run iterative_evolution on baselines subset slices")
    ap.add_argument("--run-cell", action="store_true")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument(
        "--subset", type=Path, default=REPO_ROOT / "experiments" / "baselines" / "subset.json"
    )
    ap.add_argument("--corpus", type=Path, default=REPO_ROOT.parent / "datasets" / "AFTER")
    ap.add_argument("--schemas", default="1,2,3,4,5")
    ap.add_argument("--schema", default="")
    ap.add_argument("--n-train", type=int, default=1)
    ap.add_argument("--n-test", type=int, default=3)
    ap.add_argument("--anchors", default="")
    ap.add_argument("--frameworks", default=",".join(DEFAULT_FRAMEWORKS))
    ap.add_argument("--framework", default="")
    ap.add_argument("--skill", default="")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--n-trials", type=int, default=5)
    ap.add_argument("--parallel", type=int, default=50)
    ap.add_argument("--model-concurrency", type=int, default=50)
    ap.add_argument(
        "--evoskill-iterations",
        type=int,
        default=int(os.environ.get("EVO_EVOSKILL_ITERATIONS", "4")),
    )
    ap.add_argument("--timeout-s", type=int, default=2400)
    ap.add_argument("--cell-timeout-s", type=int, default=7200)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--python", default=os.environ.get("EVO_PYTHON", "python3"))
    ap.add_argument("--entry-script", type=Path, default=Path(__file__).resolve())
    ap.add_argument(
        "--solver-model", default=os.environ.get("EVO_SOLVER_MODEL", "openai/gpt-oss-20b")
    )
    ap.add_argument("--solver-served-id", default=os.environ.get("EVO_SOLVER_SERVED_ID", ""))
    ap.add_argument(
        "--solver-api-base",
        default=os.environ.get("EVO_SOLVER_API_BASE", "https://openrouter.ai/api/v1"),
    )
    ap.add_argument("--solver-api-key-env", default="OPENROUTER_API_KEY")
    ap.add_argument(
        "--reflector-model",
        default=os.environ.get("EVO_REFLECTOR_MODEL", "deepseek/deepseek-v4-flash"),
    )
    ap.add_argument("--reflector-served-id", default=os.environ.get("EVO_REFLECTOR_SERVED_ID", ""))
    ap.add_argument(
        "--reflector-api-base",
        default=os.environ.get("EVO_REFLECTOR_API_BASE", "https://openrouter.ai/api/v1"),
    )
    ap.add_argument("--reflector-api-key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--reflector-api-key", default=os.environ.get("EVO_REFLECTOR_API_KEY", ""))
    ap.add_argument(
        "--editor-model", default=os.environ.get("EVO_EDITOR_MODEL", "deepseek/deepseek-v4-flash")
    )
    ap.add_argument("--editor-served-id", default=os.environ.get("EVO_EDITOR_SERVED_ID", ""))
    ap.add_argument(
        "--editor-api-base",
        default=os.environ.get("EVO_EDITOR_API_BASE", "https://openrouter.ai/api/v1"),
    )
    ap.add_argument("--editor-api-key-env", default="OPENROUTER_API_KEY")
    ap.add_argument(
        "--embedding-model",
        default=os.environ.get("EVO_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
    )
    ap.add_argument(
        "--embedding-api-base",
        default=os.environ.get("EVO_EMBEDDING_API_BASE", "http://localhost:10225/v1"),
    )
    ap.add_argument("--embedding-api-key", default=os.environ.get("EVO_EMBEDDING_API_KEY", "EMPTY"))
    ap.add_argument("--embedding-api-key-env", default="EVO_EMBEDDING_API_KEY")
    ap.add_argument("--max-tokens", default=os.environ.get("EVO_MAX_TOKENS", "8000"))
    ap.add_argument("--editor-max-tokens", default=os.environ.get("EVO_EDITOR_MAX_TOKENS", "8000"))
    ap.add_argument("--temperature", default=os.environ.get("EVO_TEMPERATURE", "0.2"))
    ap.add_argument("--editor-temperature", default=os.environ.get("EVO_EDITOR_TEMPERATURE", "0.2"))
    ap.add_argument(
        "--solver-reasoning-effort",
        default=os.environ.get(
            "EVO_SOLVER_REASONING_EFFORT", os.environ.get("EVO_LLM_REASONING_EFFORT", "medium")
        ),
    )
    ap.add_argument(
        "--editor-reasoning-effort",
        default=os.environ.get(
            "EVO_EDITOR_REASONING_EFFORT", os.environ.get("EVO_LLM_REASONING_EFFORT", "medium")
        ),
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.root = args.root.resolve()
    args.subset = args.subset.resolve()
    args.corpus = args.corpus.resolve()
    try:
        if args.run_cell:
            if not args.framework or not args.skill or not args.schema:
                raise SystemExit("--run-cell requires --schema, --framework, and --skill")
            return run_one_cell(args)
        return supervise(args)
    except BaseException as exc:
        try:
            crash_path = args.root / "supervisor_crash.json"
            if args.run_cell:
                crash_path = (
                    args.root
                    / args.schema
                    / "cells"
                    / args.framework.strip().lower()
                    / args.skill
                    / "cell_crash.json"
                )
            _write_json(
                crash_path,
                {
                    "at": _now(),
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                    "run_cell": bool(args.run_cell),
                    "framework": args.framework,
                    "skill": args.skill,
                    "schema": args.schema,
                },
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
