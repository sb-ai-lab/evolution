"""Round driver for iterative skill evolution."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "experiments"))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


from _shared.llm_telemetry import (  # noqa: E402
    add_usage,
    install_produce_telemetry,
    read_telemetry_jsonl,
    uninstall_openai_class_patch,
    usage_from_result,
)

from evolution.adapters._envelopes import WORKSPACE_ROOT_WRITE_RULE  # noqa: E402

from .canonical_skill import (  # noqa: E402
    canonical_skill_path,
    read_canonical,
    read_version_body,
    seed_v0,
    sha256_text,
    snapshot_version,
    write_canonical,
)
from .eval_lanes import (  # noqa: E402
    CANONICAL_EXPORT,
    NATIVE_RUNTIME,
    primary_eval_lane,
    supported_eval_lanes,
    supports_native_runtime,
)
from .native_eval import eval_native  # noqa: E402
from .neutral_solver import EVAL_LABEL as INJECTION_LABEL  # noqa: E402
from .neutral_solver import solve_task  # noqa: E402
from .skill_editors import edit_skill  # noqa: E402
from .trace_bundle import (  # noqa: E402
    bundle_from_solve_result,
    bundle_path,
    load_bundle,
    write_bundle,
)
from .verification import path_unity_gate, split_gate, versions_gate  # noqa: E402

VERSION_SELECTION_LABEL = "best-on-train"


def split_task_ref(default_role: str, task_id: str) -> tuple[str, str, str]:
    """Return ``(role, plain_task_id, stable_ref)`` for corpus task references.

    Historical iterative runs passed one role separately plus bare task ids.
    The baselines subset manifest stores explicit ``role/task_id`` refs and
    can mix roles inside one skill slice, so the loop must preserve those refs
    instead of silently resolving everything under a single role.
    """
    text = str(task_id)
    if "/" in text:
        ref_role, plain = text.split("/", 1)
        return ref_role, plain, text
    return default_role, text, text


def task_dir_for(*, corpus_root: Path, role: str, task_id: str) -> Path:
    ref_role, plain, _stable = split_task_ref(role, task_id)
    return corpus_root / "tasks" / ref_role / plain


def read_instruction(task_dir: Path) -> str:
    p = task_dir / "instruction.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _append_rounds_jsonl(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "rounds.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _remove_path_for_retry(path: Path) -> None:
    """Remove a stale artifact without blocking retries on slow native cleanup."""
    if not path.exists():
        return
    if not path.is_dir():
        path.unlink()
        return
    stale = path.with_name(f"{path.name}.stale-{os.getpid()}-{time.time_ns()}")
    try:
        path.rename(stale)
    except FileNotFoundError:
        return
    except OSError:
        for attempt, delay_s in enumerate((0.05, 0.1, 0.2, 0.5, 1.0), start=1):
            try:
                shutil.rmtree(path)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(delay_s)
        return
    for delay_s in (0.05, 0.1, 0.2):
        try:
            shutil.rmtree(stale)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(delay_s)


def _mean_pass_rate(results: list[dict[str, Any]]) -> float:
    if not results:
        return 0.0
    return sum(float(r.get("pass_rate", 0.0) or 0.0) for r in results) / len(results)


def _inner_workers(kind: str, n_jobs: int) -> int:
    """Bound per-cell parallelism for independent train/version solves."""
    if n_jobs <= 1:
        return 1
    env_key = f"EVO_{kind.upper()}_PARALLEL"
    raw = os.environ.get(env_key, os.environ.get("EVO_INNER_PARALLEL", "1"))
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = 1
    return max(1, min(n_jobs, cap))


def _run_indexed_parallel(kind: str, jobs: list[tuple[int, Any]], fn) -> list[tuple[int, Any]]:
    workers = _inner_workers(kind, len(jobs))
    if workers <= 1:
        return [fn(job) for job in jobs]
    results: list[tuple[int, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, job) for job in jobs]
        for fut in as_completed(futures):
            results.append(fut.result())
    return sorted(results, key=lambda item: item[0])


def _maybe_mean_pass_rate(results: list[dict[str, Any]]) -> float | None:
    if not results:
        return None
    return _mean_pass_rate(results)


def _evaluation_summary(
    *,
    framework: str,
    primary_lane: str,
    test_results: list[dict[str, Any]],
) -> dict[str, Any]:
    by_lane = {
        lane: [r for r in test_results if r.get("eval_lane") == lane]
        for lane in supported_eval_lanes(framework)
    }
    canonical = _maybe_mean_pass_rate(by_lane.get(CANONICAL_EXPORT, []))
    native = _maybe_mean_pass_rate(by_lane.get(NATIVE_RUNTIME, []))
    primary = native if primary_lane == NATIVE_RUNTIME else canonical
    return {
        "primary_eval_lane": primary_lane,
        "supported_eval_lanes": list(supported_eval_lanes(framework)),
        "canonical_export_score": canonical,
        "native_runtime_score": native,
        "export_gap_native_minus_canonical": (
            round(native - canonical, 6) if native is not None and canonical is not None else None
        ),
        "single_test_score": primary,
    }


def _version_sort_key(version: str) -> tuple[int, str]:
    if version.startswith("v") and version[1:].isdigit():
        return (int(version[1:]), version)
    return (10**9, version)


def _pick_from_round_records(run_dir: Path, *, lane: str, framework: str = "") -> dict[str, Any]:
    """Round r solved exactly v_r: reuse rounds.jsonl instead of re-solving each version."""
    by_version: dict[str, list[float]] = {}
    path = run_dir / "rounds.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if str(rec.get("eval_lane") or "") != lane:
                continue
            by_version.setdefault(str(rec.get("skill_version")), []).append(
                float(rec.get("train_pass_rate") or 0.0)
            )
    scores: list[dict[str, Any]] = []
    for version, rates in sorted(by_version.items(), key=lambda kv: _version_sort_key(kv[0])):
        score = {
            "version": version,
            "eval_lane": lane,
            "mean_pass_rate": sum(rates) / len(rates),
            "per_task": [],
            "round_tag": "round_records",
        }
        if framework:
            score["native_state_dir"] = str(native_state_version_dir(run_dir, framework, version))
        scores.append(score)
    best = max(scores, key=lambda s: s["mean_pass_rate"]) if scores else None
    return {
        "selection": VERSION_SELECTION_LABEL,
        "eval_lane": lane,
        "scores": scores,
        "best": best,
    }


def pick_best_version(
    *,
    skill_id: str,
    run_dir: Path,
    corpus_root: Path,
    role: str,
    train_ids: list[str],
    solver_cfg: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    return _pick_from_round_records(run_dir, lane=CANONICAL_EXPORT)


def pick_best_native_version(
    *,
    framework: str,
    run_dir: Path,
    corpus_root: Path,
    role: str,
    train_ids: list[str],
    solver_cfg: dict[str, Any],
    api_key: str,
    embedding_cfg: dict[str, Any] | None = None,
    reflector_cfg: dict[str, Any] | None = None,
    emb_key: str = "",
    refl_key: str = "",
) -> dict[str, Any]:
    return _pick_from_round_records(run_dir, lane=NATIVE_RUNTIME, framework=framework)


def _evoskill_repo_dir_for_version(run_dir: Path, version: str) -> Path | None:
    if not version.startswith("v") or not version[1:].isdigit():
        return None
    version_idx = int(version[1:])
    if version_idx <= 0:
        repo = run_dir / "evoskill_repo" / "v0"  # seed base-program repo (runtime symmetry)
        return repo if repo.is_dir() else None
    repo = run_dir / "evoskill_repo" / f"round{version_idx - 1}"
    return repo if repo.is_dir() else None


def native_state_dir(run_dir: Path, framework: str) -> Path:
    fw = framework.strip().lower()
    if fw == "evoskill":
        return run_dir / "native_state" / "evoskill" / "skills"
    if fw == "memento":
        return run_dir / "native_state" / "memento"
    if fw == "memp":
        return run_dir / "native_state" / "memp" / "memory"
    raise ValueError(f"no native state for framework {framework}")


def native_state_version_dir(run_dir: Path, framework: str, version: str) -> Path:
    return run_dir / "native_state_versions" / framework.strip().lower() / version


def _copy_state(src: Path, dst: Path) -> bool:
    if not src.is_dir():
        return False
    if dst.exists():
        _remove_path_for_retry(dst)
    shutil.copytree(src, dst, symlinks=False)
    return True


def snapshot_native_state(run_dir: Path, framework: str, version: str) -> bool:
    if framework.strip().lower() == "memento":
        try:
            from evolution.adapters.memento_adapter import redact_memento_configs

            redact_memento_configs(native_state_dir(run_dir, framework))
        except Exception:
            pass
    return _copy_state(
        native_state_dir(run_dir, framework), native_state_version_dir(run_dir, framework, version)
    )


def _write_anchor_native_skill(run_dir: Path, framework: str, skill_id: str, body: str) -> Path:
    skill_dir = native_state_dir(run_dir, framework) / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def _replace_native_state_from_dir(run_dir: Path, framework: str, src: Path) -> bool:
    src = Path(src)
    if not src.is_dir():
        return False
    dst = native_state_dir(run_dir, framework)
    if dst.exists():
        _remove_path_for_retry(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=False)
    return True


def _update_native_state_from_edit(
    *,
    run_dir: Path,
    framework: str,
    skill_id: str,
    body: str,
    telemetry: dict[str, Any],
) -> bool:
    fw = framework.strip().lower()
    if fw == "evoskill":
        native_src = str(telemetry.get("native_state_dir") or "")
        if native_src and _replace_native_state_from_dir(run_dir, fw, Path(native_src)):
            return True
        _write_anchor_native_skill(run_dir, fw, skill_id, body)
        return True
    if supports_native_runtime(fw):
        return native_state_dir(run_dir, fw).is_dir()
    return False


def _seed_native_v0(run_dir: Path, framework: str, skill_id: str, body: str) -> bool:
    """Seed a native default-state snapshot for primary native version selection."""
    fw = framework.strip().lower()
    if fw == "evoskill":
        _write_anchor_native_skill(run_dir, fw, skill_id, body)
        from _shared.evoskill_bridge import init_seed_program_repo

        init_seed_program_repo(
            repo=run_dir / "evoskill_repo" / "v0", skill_id=skill_id, skill_body=body
        )
        return snapshot_native_state(run_dir, fw, "v0")
    if fw == "memento":
        state = native_state_dir(run_dir, fw)
        skill_dir = state / "memento_s" / "skills" / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
        try:
            from evolution.adapters.memento_adapter import _sync_test_home_registry

            _sync_test_home_registry(state)
        except Exception:
            pass
        return snapshot_native_state(run_dir, fw, "v0")
    if fw == "memp":
        state = native_state_dir(run_dir, fw)
        state.mkdir(parents=True, exist_ok=True)
        docs = [
            {
                "page_content": body[:2000],
                "metadata": {
                    "source": f"base_skill:{skill_id}",
                    "workflow": body,
                    "task_id": "__seed__",
                    "seed_policy": "iterative_v0_seed",
                },
            }
        ]
        (state / "documents.json").write_text(json.dumps(docs, indent=2), encoding="utf-8")
        direct = state / "direct"
        direct.mkdir(parents=True, exist_ok=True)
        (direct / "documents.json").write_text(json.dumps(docs, indent=2), encoding="utf-8")
        return snapshot_native_state(run_dir, fw, "v0")
    return False


def _reset_run_artifacts(run_dir: Path, skill_id: str) -> None:
    """Remove artifacts that would make deterministic run dirs stale."""
    for rel in (
        Path("traces"),
        Path("solver_cache"),
        Path("version_score"),
        Path("version_score_native"),
        Path("test_eval"),
        Path("final_eval"),
        Path("native_state"),
        Path("native_state_versions"),
        Path("hermes_home"),
        Path("hermes_edit_workspace"),
        Path("codex_edit_workspace"),
        Path("claude_edit_workspace"),
        Path("memento_edit_workspace"),
        Path("evoskill_repo"),
        Path("evoskill_solves"),
        Path("evoskill_state"),
        Path("skills") / skill_id,
    ):
        p = run_dir / rel
        if p.exists():
            _remove_path_for_retry(p)
    for name in (
        "rounds.jsonl",
        "summary.json",
        "seed_meta.json",
        "cell_crash.json",
        "editor_llm_calls.jsonl",
    ):
        p = run_dir / name
        if p.exists():
            p.unlink()


def _native_train_solve(
    *,
    framework: str,
    task_dir: Path,
    instruction: str,
    skill_id: str,
    skill_body: str,
    skill_version: str,
    round_idx: int,
    task_id: str,
    role: str,
    run_dir: Path,
    solver_cfg: dict[str, Any],
    api_key: str,
    embedding_cfg: dict[str, Any] | None = None,
    reflector_cfg: dict[str, Any] | None = None,
    emb_key: str = "",
    refl_key: str = "",
    timeout_s: int = 600,
) -> dict[str, Any]:
    fw = framework.strip().lower()
    cache_dir = run_dir / "native_solver_cache" / f"round{round_idx}" / task_id
    res = eval_native(
        framework=fw,
        task_dir=task_dir,
        instruction=instruction,
        cache_dir=cache_dir,
        solver_cfg=solver_cfg,
        api_key=api_key,
        post_train_state=native_state_dir(run_dir, fw),
        embedding_cfg=embedding_cfg,
        reflector_cfg=reflector_cfg,
        emb_key=emb_key,
        refl_key=refl_key,
        leave_one_out_task_id=None,
        evoskill_repo_dir=_evoskill_repo_dir_for_version(run_dir, skill_version),
        evoskill_condition="best_program",
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
    bundle["eval_lane"] = NATIVE_RUNTIME
    bundle["eval_label"] = res.get("eval_label") or "native_runtime"
    # memp's single-shot executor runs solution.py from output/ (canonical ../ contract); the
    # native-workspace-root narrative only applies to the episode-loop executor.
    memp_single_shot = (
        fw == "memp" and os.environ.get("EVO_MEMP_NATIVE_EXECUTOR", "single_shot") != "episode"
    )
    if not memp_single_shot:
        bundle["runtime_contract"] = {
            "kind": "native_workspace_root",
            "framework": fw,
            "solver_cwd": "<workspace>",
            "read_path_field": "solver_read_path_from_workspace_root",
            "read_rule": "read inputs directly from workspace-root relative paths",
            "write_rule": WORKSPACE_ROOT_WRITE_RULE,
        }
    write_bundle(bundle_path(run_dir, round_idx, task_id), bundle)
    return {
        **res,
        "bundle": bundle,
        "cache_dir": str(cache_dir),
        "eval_lane": NATIVE_RUNTIME,
    }


def _editor_llm_jsonl(run_dir: Path) -> Path:
    return Path(run_dir) / "editor_llm_calls.jsonl"


def _editor_usage_in_window(run_dir: Path, t0: float, t1: float) -> dict[str, Any]:
    per_turn, _totals = read_telemetry_jsonl(_editor_llm_jsonl(run_dir))
    rows = [t for t in per_turn if t0 <= float(t.get("ts") or 0.0) <= t1]
    return add_usage(
        *(
            {
                "llm_calls": 1,
                "tokens_in": t["tokens_in"],
                "tokens_out": t["tokens_out"],
                "latency_ms": t["latency_ms"],
            }
            for t in rows
        )
    )


def run_cell(**kwargs: Any) -> dict[str, Any]:
    """Install cell-wide LLM-call telemetry, then run the cell."""
    fw = str(kwargs.get("framework", "")).strip().lower()
    run_dir = Path(kwargs["run_dir"]).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    _reset_run_artifacts(run_dir, str(kwargs["skill_id"]))
    jsonl_path = _editor_llm_jsonl(run_dir)
    recorder = install_produce_telemetry(jsonl_path, source=fw)
    prev_tele_env = os.environ.get("EVO_PRODUCE_TELEMETRY_JSONL")
    os.environ["EVO_PRODUCE_TELEMETRY_JSONL"] = str(jsonl_path)
    try:
        return _run_cell_inner(**kwargs)
    finally:
        uninstall_openai_class_patch()
        recorder.close()
        if prev_tele_env is None:
            os.environ.pop("EVO_PRODUCE_TELEMETRY_JSONL", None)
        else:
            os.environ["EVO_PRODUCE_TELEMETRY_JSONL"] = prev_tele_env


def _run_cell_inner(
    *,
    framework: str,
    skill_id: str,
    role: str,
    train_ids: list[str],
    test_ids: list[str],
    n_rounds: int,
    solver_cfg: dict[str, Any],
    editor_cfg: dict[str, Any],
    api_key: str,
    run_dir: Path,
    corpus_root: Path,
    editor_api_key: str | None = None,
    embedding_cfg: dict[str, Any] | None = None,
    reflector_cfg: dict[str, Any] | None = None,
    emb_key: str = "",
    refl_key: str = "",
    timeout_s: int = 600,
    continue_on_unchanged: bool = False,
    skip_test_eval: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Seed v0 → solve/edit rounds → best-on-train → single test eval."""
    # TODO(D2): optional inner train_a/train_b hardening via val_ids.
    fw = framework.strip().lower()
    run_dir = Path(run_dir).resolve()
    corpus_root = Path(corpus_root).resolve()

    seed_meta = seed_v0(
        run_dir=run_dir,
        corpus_root=corpus_root,
        skill_id=skill_id,
        source_task_ids=train_ids or test_ids[:1],
    )
    current_version = "v0"
    primary_lane = primary_eval_lane(fw)
    if supports_native_runtime(fw):
        _seed_native_v0(run_dir, fw, skill_id, read_canonical(run_dir, skill_id))
    edit_api_key = api_key if editor_api_key is None else editor_api_key
    editor_trace_ids: set[str] = set()
    round_summaries: list[dict[str, Any]] = []

    for r in range(n_rounds + 1):
        bundles: list[dict[str, Any]] = []
        solve_results: list[dict[str, Any]] = []
        body = read_canonical(run_dir, skill_id)
        for task_id in train_ids:
            _task_role, _plain_task_id, stable_ref = split_task_ref(role, task_id)
            editor_trace_ids.add(stable_ref)
        train_jobs = list(enumerate(train_ids))

        def solve_train_one(
            job: tuple[int, str],
        ) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
            idx, task_id = job
            task_role, _plain_task_id, stable_ref = split_task_ref(role, task_id)
            tdir = task_dir_for(corpus_root=corpus_root, role=role, task_id=task_id)
            if dry_run:
                return idx, {}, None
            instruction = read_instruction(tdir)
            if primary_lane == NATIVE_RUNTIME:
                res = _native_train_solve(
                    framework=fw,
                    task_dir=tdir,
                    instruction=instruction,
                    skill_id=skill_id,
                    skill_body=body,
                    skill_version=current_version,
                    round_idx=r,
                    task_id=stable_ref,
                    role=task_role,
                    run_dir=run_dir,
                    solver_cfg=solver_cfg,
                    api_key=api_key,
                    embedding_cfg=embedding_cfg,
                    reflector_cfg=reflector_cfg,
                    emb_key=emb_key,
                    refl_key=refl_key,
                    timeout_s=timeout_s,
                )
            else:
                res = solve_task(
                    task_dir=tdir,
                    instruction=instruction,
                    skill_body=body,
                    skill_id=skill_id,
                    skill_version=current_version,
                    round_idx=r,
                    task_id=stable_ref,
                    role=task_role,
                    run_dir=run_dir,
                    solver_cfg=solver_cfg,
                    api_key=api_key,
                    timeout_s=timeout_s,
                )
            bp = bundle_path(run_dir, r, task_id)
            bundle = load_bundle(bp) if bp.is_file() else None
            return idx, res, bundle

        for _idx, res, bundle in _run_indexed_parallel("train_task", train_jobs, solve_train_one):
            if res:
                solve_results.append(res)
            if bundle is not None:
                bundles.append(bundle)

        train_pass = _mean_pass_rate(solve_results)
        sha = sha256_text(body)
        round_rec = {
            "round": r,
            "skill_version": current_version,
            "train_pass_rate": train_pass,
            "skill_sha256": sha,
            "n_bundles": len(bundles),
            "eval_lane": primary_lane,
            "train_llm_usage": add_usage(*(usage_from_result(res) for res in solve_results)),
        }
        _append_rounds_jsonl(run_dir, round_rec)
        round_summaries.append(round_rec)

        if r >= n_rounds:
            break
        if dry_run or not bundles:
            break

        editor_runtime_cfg = {
            **editor_cfg,
            "corpus_root": str(corpus_root),
            "role": role,
            "train_ids": list(train_ids),
            "solver_cfg": dict(solver_cfg),
            "solver_api_key": api_key,
            "n_rounds": int(n_rounds),
            "timeout_s": int(timeout_s),
        }
        edit_t0 = time.time()
        edit_res = edit_skill(
            fw,
            skill_id=skill_id,
            skill_body=body,
            bundles=bundles,
            run_dir=run_dir,
            editor_cfg=editor_runtime_cfg,
            api_key=edit_api_key,
            round_idx=r,
            role=role,
        )
        round_rec["editor_llm_usage"] = _editor_usage_in_window(run_dir, edit_t0, time.time())
        if not edit_res.changed:
            round_rec["unchanged_body"] = True
            round_rec["editor_telemetry"] = edit_res.telemetry
            if not continue_on_unchanged:
                round_rec["early_stop"] = "unchanged_body"
                break
            if supports_native_runtime(fw):
                # editor may have mutated live native state; rewind to the current version snapshot
                _replace_native_state_from_dir(
                    run_dir, fw, native_state_version_dir(run_dir, fw, current_version)
                )
            continue

        next_version = f"v{r + 1}"
        write_canonical(run_dir, skill_id, edit_res.new_body)
        snapshot_version(run_dir, skill_id, next_version)
        if supports_native_runtime(fw):
            _update_native_state_from_edit(
                run_dir=run_dir,
                framework=fw,
                skill_id=skill_id,
                body=edit_res.new_body,
                telemetry=edit_res.telemetry,
            )
            snapshot_native_state(run_dir, fw, next_version)
        current_version = next_version
        round_rec.update(
            {
                "edited_to": next_version,
                "leak_findings": edit_res.leak_findings,
                "editor_telemetry": edit_res.telemetry,
            }
        )

    canonical_pick = (
        {
            "selection": VERSION_SELECTION_LABEL,
            "eval_lane": CANONICAL_EXPORT,
            "scores": [],
            "best": None,
        }
        if dry_run
        else pick_best_version(
            skill_id=skill_id,
            run_dir=run_dir,
            corpus_root=corpus_root,
            role=role,
            train_ids=train_ids,
            solver_cfg=solver_cfg,
            api_key=api_key,
        )
    )
    native_pick = (
        {
            "selection": VERSION_SELECTION_LABEL,
            "eval_lane": NATIVE_RUNTIME,
            "scores": [],
            "best": None,
        }
        if dry_run or not supports_native_runtime(fw)
        else pick_best_native_version(
            framework=fw,
            run_dir=run_dir,
            corpus_root=corpus_root,
            role=role,
            train_ids=train_ids,
            solver_cfg=solver_cfg,
            api_key=api_key,
            embedding_cfg=embedding_cfg,
            reflector_cfg=reflector_cfg,
            emb_key=emb_key,
            refl_key=refl_key,
        )
    )
    pick = (
        native_pick
        if primary_lane == NATIVE_RUNTIME and native_pick.get("best")
        else canonical_pick
    )
    best_version = (pick.get("best") or {}).get("version") or current_version
    best_body = read_version_body(run_dir, skill_id, best_version)
    write_canonical(run_dir, skill_id, best_body)
    if supports_native_runtime(fw):
        _replace_native_state_from_dir(
            run_dir,
            fw,
            native_state_version_dir(run_dir, fw, best_version),
        )

    test_results: list[dict[str, Any]] = []
    if not dry_run and not skip_test_eval:
        for task_id in test_ids:
            task_role, _plain_task_id, stable_ref = split_task_ref(role, task_id)
            tdir = task_dir_for(corpus_root=corpus_root, role=role, task_id=task_id)
            instruction = read_instruction(tdir)
            if CANONICAL_EXPORT in supported_eval_lanes(fw):
                res = solve_task(
                    task_dir=tdir,
                    instruction=instruction,
                    skill_body=best_body,
                    skill_id=skill_id,
                    skill_version=best_version,
                    round_idx=999,
                    task_id=stable_ref,
                    role=task_role,
                    run_dir=run_dir / "test_eval" / CANONICAL_EXPORT,
                    solver_cfg=solver_cfg,
                    api_key=api_key,
                    timeout_s=timeout_s,
                )
                res["eval_label"] = INJECTION_LABEL
                res["eval_lane"] = CANONICAL_EXPORT
                res["primary_eval"] = primary_lane == CANONICAL_EXPORT
                res["version_selection"] = VERSION_SELECTION_LABEL
                res["version_selection_lane"] = str(pick.get("eval_lane") or "")
                test_results.append(res)
            if NATIVE_RUNTIME in supported_eval_lanes(fw):
                state = native_state_version_dir(run_dir, fw, best_version)
                res = eval_native(
                    framework=fw,
                    task_dir=tdir,
                    instruction=instruction,
                    cache_dir=run_dir / "test_eval" / NATIVE_RUNTIME / stable_ref,
                    solver_cfg=solver_cfg,
                    api_key=api_key,
                    post_train_state=state,
                    embedding_cfg=embedding_cfg,
                    reflector_cfg=reflector_cfg,
                    emb_key=emb_key,
                    refl_key=refl_key,
                    leave_one_out_task_id=stable_ref,
                    evoskill_repo_dir=_evoskill_repo_dir_for_version(run_dir, best_version),
                    evoskill_condition="best_program",
                    timeout_s=timeout_s,
                )
                res["primary_eval"] = primary_lane == NATIVE_RUNTIME
                res["version_selection"] = VERSION_SELECTION_LABEL
                res["version_selection_lane"] = str(pick.get("eval_lane") or "")
                test_results.append(res)

    unity = path_unity_gate(
        run_dir=run_dir,
        skill_id=skill_id,
        expected_sha=sha256_text(best_body),
    )
    split_chk = split_gate(
        editor_trace_task_ids=editor_trace_ids,
        test_task_ids={split_task_ref(role, task_id)[2] for task_id in test_ids},
    )
    ver_chk = versions_gate(run_dir, skill_id, min_versions=1)
    selected_path = str(canonical_skill_path(run_dir, skill_id))

    summary = {
        "framework": fw,
        "skill_id": skill_id,
        "role": role,
        "seed": seed_meta,
        "rounds": round_summaries,
        "llm_usage": {
            "train_rounds": add_usage(*(r.get("train_llm_usage") for r in round_summaries)),
            "editor": add_usage(*(r.get("editor_llm_usage") for r in round_summaries)),
        },
        "version_pick": pick,
        "version_picks": {
            CANONICAL_EXPORT: canonical_pick,
            NATIVE_RUNTIME: native_pick if supports_native_runtime(fw) else None,
        },
        "best_version": best_version,
        "primary_eval_lane": primary_lane,
        "selected_skill_path": selected_path,
        "test_results": test_results,
        "evaluation": _evaluation_summary(
            framework=fw,
            primary_lane=primary_lane,
            test_results=test_results,
        ),
        "gates": {
            "path_unity": unity,
            "split": split_chk,
            "versions": ver_chk,
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return summary


__all__ = [
    "VERSION_SELECTION_LABEL",
    "native_state_dir",
    "native_state_version_dir",
    "pick_best_version",
    "pick_best_native_version",
    "run_cell",
    "snapshot_native_state",
    "split_task_ref",
    "task_dir_for",
]
