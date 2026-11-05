#!/usr/bin/env python3
"""Iterative AFTER loop harness (protocol loop-v1).

Lane-matched train loops per framework, then Lane C + Lane N evaluation.
One cell per process invocation (framework × anchor).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EVO_ROOT = HERE.parents[1]
RUNNER_DIR = EVO_ROOT / "experiments" / "table3-4-minimal-sbbench-v2" / "runner"
ART_BASE = Path(os.environ.get("AFTER_ARTIFACTS_ROOT", str(HERE / "runs" / "artifacts_loop")))

sys.path.insert(0, str(EVO_ROOT / "src"))
sys.path.insert(0, str(EVO_ROOT / "experiments"))
sys.path.insert(0, str(RUNNER_DIR))

import afterbench_loop_common as lc  # noqa: E402
import afterbench_smoke as ab  # noqa: E402
import framework_produce_valid as fpv  # noqa: E402
from _shared.pytest_runner import run_pytest_against_workspace  # noqa: E402
from _shared.table3_base_seed import base_skill_bodies, resolve_base_skill_seed  # noqa: E402
from _shared.workspace_materializer import (  # noqa: E402
    materialize_workspace,
)

from evolution.eval.python_env import resolve_python_for_role  # noqa: E402


def _api_key(cfg: dict) -> str:
    return os.environ.get(str(cfg.get("api_key_env") or ""), "") or "EMPTY"


def _role_for_task_dir(task_dir: Path) -> str | None:
    import tomllib

    task_toml = Path(task_dir) / "task.toml"
    if not task_toml.is_file():
        return None
    try:
        data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    except Exception:
        return None
    role = ((data.get("task") or {}).get("role") or "").strip()
    return role or None


def _python_for_task_dir(task_dir: Path) -> str:
    if os.environ.get("EVO_DISABLE_ROLE_PYTHON", "0") == "1":
        return sys.executable
    role = _role_for_task_dir(task_dir)
    return resolve_python_for_role(role) if role else sys.executable


@contextmanager
def _patched_env(updates: dict[str, str | None]):
    backup = {k: os.environ.get(k) for k in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _filter_materializable(
    tuples: list[tuple[str, str, list[str], str, Path]],
) -> tuple[list[tuple[str, str, list[str], str, Path]], list[dict[str, str]]]:
    kept: list[tuple[str, str, list[str], str, Path]] = []
    excluded: list[dict[str, str]] = []
    for item in tuples:
        task_id, role, _skills, _difficulty, task_dir = item
        with tempfile.TemporaryDirectory(prefix="after_materialize_check_") as tmp:
            try:
                materialize_workspace(task_dir, Path(tmp) / "workspace", agent_solve_sh=True)
            except Exception as exc:
                excluded.append(
                    {
                        "task_id": task_id,
                        "role": role,
                        "task_dir": str(task_dir),
                        "reason": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
                continue
        kept.append(item)
    return kept, excluded


def _setup_process_env(*, iso: Path, anchor: str) -> None:
    os.environ["EVO_ADAPTER_FEEDBACK_POLICY"] = "oracle_diagnostic"
    os.environ["EVO_PROJECT_ROOT"] = str(iso)
    os.environ["EVO_SKILLS_ROOT"] = str(iso / ".agents" / "skills")
    os.environ.setdefault("EVO_REWRITE_MIN_BODY_FRAC", "0.25")
    os.environ.setdefault("SKILL_BODY_CHAR_LIMIT", "40000")
    os.environ["EVO_REFLECT_BLIND"] = "0"
    os.environ["EVO_REFLECT_ALLOW_ORACLE_ON_PAPER"] = "1"
    os.environ["EVO_DISABLE_LEAK_SCANNER"] = "1"
    os.environ.setdefault("EVO_MEMENTO_NATIVE_SERVICES_PROFILE", "native_reflection")
    os.environ.setdefault("EVO_MEMENTO_ALLOW_STDOUT_SKILL_FALLBACK", "0")
    os.environ.setdefault("EVO_MEMENTO_ALLOW_DIRECT_EDIT_FALLBACK", "0")
    os.environ.setdefault("EVO_MEMENTO_EVAL_TIMEOUT_S", "360")
    os.environ.setdefault("EVO_MEMENTO_EVAL_MAX_ITERATIONS", "28")
    os.environ.setdefault("EVO_MEMENTO_MAX_REACT_PER_STEP", "12")
    os.environ.setdefault("EVO_MEMENTO_MAX_REPLANS", "4")
    os.environ.setdefault("EVO_MEMENTO_LLM_TIMEOUT_S", "240")
    os.environ.setdefault("EVO_MEMENTO_DIRECT_EDIT_TIMEOUT_S", "480")
    os.environ.setdefault("EVO_MEMENTO_EDIT_TIMEOUT_S", "60")


def _neutral_solve(
    *,
    task_dir: Path,
    instruction: str,
    skill_body: str,
    solver_cfg: dict,
    api_key: str,
    cache_dir: Path,
    seed: int,
    timeout_s: int,
) -> dict[str, Any]:
    from _shared import native_framework_runners as nfr

    sc = dict(solver_cfg)
    sc["seed"] = seed
    return nfr.run_evolution_test(
        task_dir=task_dir,
        raw_sbbench_path=task_dir,
        instruction=instruction,
        condition="post_train",
        cache_dir=cache_dir,
        solver_cfg=sc,
        api_key=api_key,
        post_train_skill_bodies=[skill_body],
        default_skill_bodies=None,
        timeout_s=timeout_s,
    )


def _noop_edit(skill_body: str, **_kw) -> tuple[str, dict]:
    return skill_body, {"editor": "noop", "ok": True}


def _write_editor_prompt_packet(
    round_dir: Path,
    *,
    framework: str,
    skill_id: str,
    digest: str,
    used_by_framework: bool,
    native_signal: str,
) -> tuple[str, dict[str, Any]]:
    packet, meta = lc.build_editor_prompt_packet(
        framework=framework,
        skill_id=skill_id,
        digest=digest,
    )
    meta = {
        **meta,
        "used_by_framework": used_by_framework,
        "native_signal": native_signal,
    }
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "editor_prompt_packet.md").write_text(packet, encoding="utf-8")
    lc.write_json(round_dir / "editor_prompt_packet_meta.json", meta)
    return packet, meta


def _round_quality_flags(framework: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Classify harness interventions that make a round non-headline-native."""
    degraded_reasons: list[str] = []
    warnings: list[str] = []

    if meta.get("ok") is False:
        degraded_reasons.append("editor_reported_not_ok")
    if meta.get("fallback_used") is True:
        degraded_reasons.append(str(meta.get("fallback") or "fallback_used"))
    import_mode = str(meta.get("import_mode") or "")
    if import_mode == "evolution_rewrite_fallback":
        degraded_reasons.append("evolution_rewrite_fallback")
    if meta.get("native_evoskill_error"):
        degraded_reasons.append("native_evoskill_error")
    if framework == "hermes" and meta.get("violations"):
        degraded_reasons.append("hermes_skill_manage_contract_violation")

    if meta.get("fallback_suppressed"):
        warnings.append(f"fallback_suppressed:{meta.get('fallback_suppressed')}")
    if meta.get("parse_errors"):
        warnings.append(f"event_log_parse_errors:{meta.get('parse_errors')}")
    if meta.get("canonical_export_failed"):
        warnings.append("canonical_export_failed")
    if meta.get("no_new_memory"):
        warnings.append("no_new_memory")
    for item in meta.get("bridge_warnings") or []:
        warnings.append(f"bridge_warning:{item}")

    return {
        "schema": "round-quality-v1",
        "framework": framework,
        "framework_label": "degraded" if degraded_reasons else "native",
        "degraded": bool(degraded_reasons),
        "degraded_reasons": sorted(set(degraded_reasons)),
        "warnings": sorted(set(str(w) for w in warnings if str(w))),
    }


def _collect_quality_report(cell_dir: Path) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    rounds_dir = cell_dir / "rounds"
    if rounds_dir.is_dir():
        for edit_meta in sorted(
            rounds_dir.glob("*/edit_meta.json"),
            key=lambda p: int(p.parent.name) if p.parent.name.isdigit() else 10**9,
        ):
            try:
                meta = lc.read_json(edit_meta)
            except Exception as exc:
                rounds.append(
                    {
                        "round": edit_meta.parent.name,
                        "framework_label": "degraded",
                        "degraded": True,
                        "degraded_reasons": [f"edit_meta_unreadable:{type(exc).__name__}"],
                        "warnings": [],
                    }
                )
                continue
            quality = meta.get("quality")
            if not isinstance(quality, dict):
                quality = _round_quality_flags(str(meta.get("editor") or ""), meta)
            rounds.append({"round": edit_meta.parent.name, **quality})
    degraded_reasons = sorted(
        {reason for row in rounds for reason in (row.get("degraded_reasons") or []) if str(reason)}
    )
    warnings = sorted(
        {warning for row in rounds for warning in (row.get("warnings") or []) if str(warning)}
    )
    return {
        "schema": "cell-quality-v1",
        "framework_label": "degraded" if degraded_reasons else "native",
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "warnings": warnings,
        "rounds": rounds,
    }


def run_hermes_round(
    *,
    round_idx: int,
    body: str,
    anchor: str,
    train_keys: list[str],
    cells: dict,
    cell_dir: Path,
    manifest: dict,
    solver_cfg: dict,
    editor_cfg: dict,
    timeout_s: int,
) -> tuple[str, dict, list[dict], float]:
    from _shared import native_framework_runners as nfr

    from evolution.adapters import hermes_adapter

    solves: list[dict] = []
    bundles: list[dict] = []
    solver_key = _api_key(solver_cfg)
    round_dir = cell_dir / "rounds" / str(round_idx)
    stage_home = round_dir / "hermes_native_stage"
    if stage_home.exists():
        shutil.rmtree(stage_home)
    hermes_adapter.seed_canonical_skill(hermes_home=stage_home, skill_id=anchor, body=body)
    stage_skills = stage_home / "skills"
    aux_cfg = manifest["models"].get("reflector") or editor_cfg
    max_workers = max(1, int(os.environ.get("HERMES_TRAIN_PARALLEL", "1") or "1"))
    max_workers = min(max_workers, max(1, len(train_keys)))

    def _solve_one(ti: int, tk: str) -> tuple[str, dict, dict]:
        task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
        instr = fpv._instruction(cells, tk)
        role = fpv._role_for_task(cells, tk) or ""
        cache = cell_dir / "rounds" / str(round_idx) / "solve" / tk
        sc = dict(solver_cfg)
        sc["seed"] = round_idx * 10 + ti
        started = time.time()
        try:
            res = nfr.run_hermes_test(
                task_dir=task_dir,
                raw_sbbench_path=task_dir,
                instruction=instr,
                condition="post_train",
                cache_dir=cache,
                solver_cfg=sc,
                aux_cfg=aux_cfg,
                api_key=solver_key,
                aux_key=_api_key(aux_cfg),
                post_train_skills_dir=stage_skills,
                max_iterations=int(
                    os.environ.get(
                        "EVO_HERMES_TRAIN_MAX_ITER",
                        manifest.get("runtime_budget", {}).get("hermes_max_iterations", 90),
                    )
                ),
                timeout_s=timeout_s,
            )
        except Exception as exc:
            res = {
                "outcome": "hard_fail",
                "error": f"hermes native solve exception: {exc!r}",
                "elapsed_s": round(time.time() - started, 1),
                "native_metrics": {"exception_type": type(exc).__name__},
            }
        bundle = lc.bundle_from_solve_result(
            task_key=tk,
            instruction=instr,
            role=role,
            cache_dir=cache,
            solve_result=res,
        )
        return tk, res, bundle

    by_key: dict[str, tuple[dict, dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_solve_one, ti, tk) for ti, tk in enumerate(train_keys)]
        for fut in as_completed(futs):
            tk, res, bundle = fut.result()
            by_key[tk] = (res, bundle)
    for tk in train_keys:
        res, bundle = by_key[tk]
        solves.append(res)
        bundles.append(bundle)
    digest, digest_audit = lc.build_digest_from_bundles(bundles)
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "digest.md").write_text(digest, encoding="utf-8")
    lc.write_json(round_dir / "digest_audit.json", digest_audit)
    prompt_packet, prompt_meta = _write_editor_prompt_packet(
        round_dir,
        framework="hermes",
        skill_id=anchor,
        digest=digest,
        used_by_framework=True,
        native_signal="trace_digest",
    )
    new_body, meta = hermes_adapter.edit_skill_from_traces(
        skill_id=anchor,
        skill_body=body,
        traces_digest=prompt_packet,
        bundles=bundles,
        run_dir=cell_dir,
        editor_cfg=editor_cfg,
        api_key=_api_key(editor_cfg),
        round_idx=round_idx,
    )
    meta["digest_audit"] = digest_audit
    meta["shared_prompt_packet"] = prompt_meta
    train_mean = lc.mean_pass_rate(solves)
    return new_body, meta, bundles, train_mean


def _memento_materialize(task_dir: Path, ws: Path) -> None:
    materialize_workspace(task_dir, ws, agent_solve_sh=True)


def _persist_round_episode_cache(cache_dir: Path, ws: Path, pytest_res: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "pytest.log").write_text(str(pytest_res.get("log_tail") or ""), encoding="utf-8")
    (cache_dir / "pytest_full.log").write_text(
        str(pytest_res.get("log_full") or pytest_res.get("log_tail") or ""),
        encoding="utf-8",
    )
    ws_copy = cache_dir / "workspace"
    if ws_copy.exists():
        shutil.rmtree(ws_copy)
    shutil.copytree(ws, ws_copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if not (ws_copy / "_solve.py").is_file() and (ws_copy / "solution.py").is_file():
        shutil.copy2(ws_copy / "solution.py", ws_copy / "_solve.py")


def _init_memento_home(home: Path, anchor: str, seed_body: str) -> Path:
    from evolution.adapters.memento_adapter import _sync_test_home_registry

    skills_dir = home / "memento_s" / "skills"
    anchor_dir = skills_dir / anchor
    anchor_dir.mkdir(parents=True, exist_ok=True)
    (anchor_dir / "SKILL.md").write_text(seed_body, encoding="utf-8")
    _sync_test_home_registry(home)
    return skills_dir


def run_memento_round(
    *,
    round_idx: int,
    anchor: str,
    train_keys: list[str],
    cells: dict,
    cell_dir: Path,
    solver_cfg: dict,
    editor_cfg: dict,
    embedding_cfg: dict,
    timeout_s: int,
) -> tuple[str, dict, list[dict], float]:
    from evolution.adapters.memento_adapter import (
        _TRAIN_ENVELOPE,
        _invoke_memento,
        _sync_test_home_registry,
        edit_skill_from_traces,
        memento_phase_max_iterations,
        memento_phase_timeout_s,
        write_llm_config,
    )

    home = cell_dir / "native_state" / "memento"
    skills_dir = home / "memento_s" / "skills"
    anchor_md = skills_dir / anchor / "SKILL.md"
    body = anchor_md.read_text(encoding="utf-8") if anchor_md.is_file() else ""
    bundles: list[dict] = []
    pytest_results: list[dict] = []

    emb_key = _api_key(embedding_cfg)
    write_llm_config(
        solver_model=solver_cfg.get("id") or solver_cfg.get("served_id_hint", ""),
        solver_base=solver_cfg.get("api_base", ""),
        solver_key=_api_key(solver_cfg),
        embedding_model=embedding_cfg["id"],
        embedding_base=embedding_cfg["api_base"],
        embedding_key=emb_key,
        config_dir=home / "memento_s",
        max_iterations=memento_phase_max_iterations("train", 24),
        served_model_id=solver_cfg.get("served_id_hint"),
    )
    os.environ["EVO_MEMENTO_NATIVE_SERVICES_PROFILE"] = "native_reflection"
    train_timeout_s = memento_phase_timeout_s("train", timeout_s, 600.0)

    for tk in train_keys:
        task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
        instr = fpv._instruction(cells, tk)
        role = fpv._role_for_task(cells, tk) or ""
        ws = cell_dir / "rounds" / str(round_idx) / "memento_ws" / tk
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        _memento_materialize(task_dir, ws)
        prompt_instr, prompt_instr_truncated = lc.clip_middle_text(
            instr,
            int(os.environ.get("EVO_TASK_INSTRUCTION_PROMPT_CHARS", "60000") or "60000"),
            label="task instruction",
        )
        envelope = _TRAIN_ENVELOPE.format(task_instruction=prompt_instr)
        rc, stdout, stderr, session_ws = _invoke_memento(
            envelope,
            home=home,
            workspace_dir=ws,
            skills_dir=skills_dir,
            timeout_s=train_timeout_s,
        )
        if session_ws and session_ws.is_dir():
            for item in session_ws.iterdir():
                dst = ws / item.name
                if item.is_dir():
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)
        task_python = _python_for_task_dir(task_dir)
        pytest_res = run_pytest_against_workspace(task_dir, ws, python=task_python, timeout_s=240)
        pytest_results.append(pytest_res)
        cache_stub = cell_dir / "rounds" / str(round_idx) / "memento_solve" / tk
        _persist_round_episode_cache(cache_stub, ws, pytest_res)
        stdout_meta = lc.write_text_artifact(cache_stub / "memento_stdout.txt", stdout)
        stderr_meta = lc.write_text_artifact(cache_stub / "memento_stderr.txt", stderr)
        (cache_stub / "transcript.txt").write_text(stdout, encoding="utf-8")
        traj = {
            "steps": [
                {"channel": "stdout", "artifact": stdout_meta},
                {"channel": "stderr", "artifact": stderr_meta},
            ],
            "task_id": tk,
            "returncode": rc,
            "task_instruction_truncated_in_prompt": prompt_instr_truncated,
        }
        lc.write_json(cache_stub / "native_trajectory.json", traj)
        bundles.append(
            lc.bundle_from_episode(
                task_key=tk,
                instruction=instr,
                role=role,
                trajectory=traj,
                pytest_result=pytest_res,
                cache_dir=cache_stub,
            )
        )
        bundles[-1]["memento_rc"] = rc
        bundles[-1]["native_stderr_artifact"] = stderr_meta
        bundles[-1]["native_stderr_tail"] = stderr_meta["preview"]

    digest, digest_audit = lc.build_digest_from_bundles(bundles)
    round_dir = cell_dir / "rounds" / str(round_idx)
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "digest.md").write_text(digest, encoding="utf-8")
    lc.write_json(round_dir / "digest_audit.json", digest_audit)
    prompt_packet, prompt_meta = _write_editor_prompt_packet(
        round_dir,
        framework="memento",
        skill_id=anchor,
        digest=digest,
        used_by_framework=True,
        native_signal="trace_digest",
    )
    current_body = anchor_md.read_text(encoding="utf-8") if anchor_md.is_file() else body
    write_llm_config(
        solver_model=editor_cfg.get("litellm_id")
        or editor_cfg.get("id")
        or editor_cfg.get("served_id_hint", ""),
        solver_base=editor_cfg.get("api_base", ""),
        solver_key=_api_key(editor_cfg),
        embedding_model=embedding_cfg["id"],
        embedding_base=embedding_cfg["api_base"],
        embedding_key=emb_key,
        config_dir=home / "memento_s",
        max_iterations=memento_phase_max_iterations("edit", 8),
    )
    new_body, meta = edit_skill_from_traces(
        skill_id=anchor,
        skill_body=current_body,
        traces_digest=prompt_packet,
        bundles=bundles,
        run_dir=cell_dir,
        editor_cfg=editor_cfg,
        api_key=_api_key(editor_cfg),
        round_idx=round_idx,
    )
    _sync_test_home_registry(home)
    native_snapshot = round_dir / "native_state" / "memento"
    lc.snapshot_dir(home, native_snapshot)
    meta["digest_audit"] = digest_audit
    meta["shared_prompt_packet"] = prompt_meta
    meta["memento_config_flip"] = "solver_solve_evolver_update"
    meta["native_state_snapshot"] = str(native_snapshot)
    train_mean = lc.mean_pass_rate(
        [{"outcome": "ok", "pass_rate": float(b.get("pass_rate", 0))} for b in bundles]
    )
    return new_body, meta, bundles, train_mean


def _memp_append_trajectories(memory_dir: Path, trajectories: list[dict]) -> dict[str, int]:
    from evolution.adapters.memp_adapter import _documents_json_path, _reflect_on_memory

    if memory_dir.is_dir() and _documents_json_path(memory_dir).is_file():
        _, counts = _reflect_on_memory(memory_dir, trajectories)
        return {"mode": "reflect_append", **counts}
    raise RuntimeError("memp memory missing documents.json for append")


def run_memp_round(
    *,
    round_idx: int,
    anchor: str,
    seed_body: str,
    train_keys: list[str],
    cells: dict,
    cell_dir: Path,
    manifest: dict,
    solver_cfg: dict,
    editor_cfg: dict,
    embedding_cfg: dict,
    timeout_s: int,
) -> tuple[str, dict, list[dict], float]:
    from evolution.adapters import memp_adapter
    from evolution.adapters._memp_env import TaskEnv
    from evolution.adapters.memp_adapter import _build_memory, render_memory_to_skill

    memory_dir = cell_dir / "native_state" / "memp" / "memory"
    memp_adapter.install_monkeypatches(
        embedding_model=embedding_cfg["id"],
        embedding_base=embedding_cfg["api_base"],
        embedding_key=_api_key(embedding_cfg),
        reflector_model=editor_cfg.get("reflector_id", ""),
        reflector_base=editor_cfg.get("reflector_api_base", ""),
        reflector_key=_api_key(editor_cfg),
    )
    max_steps = int(manifest.get("runtime_budget", {}).get("memp_max_steps", 40))
    episode_timeout_s = float(
        os.environ.get("EVO_MEMP_TRAIN_TIMEOUT_S", str(min(timeout_s, 600))) or "600"
    )
    served = solver_cfg.get("served_id_hint") or solver_cfg["id"]
    trajectories: list[dict] = []
    bundles: list[dict] = []

    for tk in train_keys:
        task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
        instr = fpv._instruction(cells, tk)
        role = fpv._role_for_task(cells, tk) or ""
        ws = cell_dir / "rounds" / str(round_idx) / "memp_ws" / tk
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        materialize_workspace(task_dir, ws, agent_solve_sh=True)
        test_path = task_dir / "tests" / "test_outputs.py"
        env = TaskEnv(
            task_id=tk,
            workspace=ws,
            test_path=test_path,
            task_path=task_dir,
            python=_python_for_task_dir(task_dir),
            guard_log_path=cell_dir / "rounds" / str(round_idx) / f"guard_{tk}.jsonl",
            feedback_policy="oracle_diagnostic",
        )
        retrieved: list[str] = []
        if memory_dir.is_dir() and memp_adapter._documents_json_path(memory_dir).is_file():
            try:
                mem = memp_adapter._load_memory_from_dir(memory_dir, retrieve_num=3)
                query, _query_truncated = lc.clip_middle_text(
                    instr,
                    int(os.environ.get("EVO_MEMP_RETRIEVE_QUERY_CHARS", "4000") or "4000"),
                    label="memp retrieval query",
                )
                results = mem.retrieve(query) or []
                for r in results:
                    doc = r[0] if isinstance(r, tuple) else r
                    wf = (getattr(doc, "metadata", {}) or {}).get("workflow") or ""
                    if wf:
                        retrieved.append(str(wf))
            except Exception:
                retrieved = []
        if not retrieved:
            retrieved = [seed_body]
        progress_path = cell_dir / "rounds" / str(round_idx) / "memp_progress.jsonl"
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        with progress_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "event": "episode_start",
                        "task_key": tk,
                        "max_steps": max_steps,
                        "timeout_s": episode_timeout_s,
                        "retrieved": len(retrieved),
                    }
                )
                + "\n"
            )
        traj = memp_adapter.run_episode(
            env,
            task_instruction=instr,
            retrieved_workflows=retrieved[:3],
            solver_base=solver_cfg["api_base"],
            solver_key=_api_key(solver_cfg),
            solver_model_id=served.removeprefix("openrouter/"),
            max_steps=max_steps,
            timeout_s=episode_timeout_s,
        )
        trajectories.append(traj)
        task_python = _python_for_task_dir(task_dir)
        pytest_res = run_pytest_against_workspace(task_dir, ws, python=task_python, timeout_s=240)
        cache_stub = cell_dir / "rounds" / str(round_idx) / "memp_solve" / tk
        _persist_round_episode_cache(cache_stub, ws, pytest_res)
        lc.write_json(cache_stub / "native_trajectory.json", traj)
        with progress_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "event": "episode_end",
                        "task_key": tk,
                        "steps": len(traj.get("steps") or []),
                        "solver_error": traj.get("solver_error"),
                        "final_pass_rate": traj.get("final_pass_rate"),
                    }
                )
                + "\n"
            )
        (cache_stub / "transcript.txt").write_text(
            json.dumps(traj.get("steps", []), indent=2, default=str),
            encoding="utf-8",
        )
        bundles.append(
            lc.bundle_from_episode(
                task_key=tk,
                instruction=instr,
                role=role,
                trajectory=traj,
                pytest_result=pytest_res,
                cache_dir=cache_stub,
            )
        )

    digest, digest_audit = lc.build_digest_from_bundles(bundles)
    round_dir = cell_dir / "rounds" / str(round_idx)
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "digest.md").write_text(digest, encoding="utf-8")
    lc.write_json(round_dir / "digest_audit.json", digest_audit)
    _prompt_packet, prompt_meta = _write_editor_prompt_packet(
        round_dir,
        framework="memp",
        skill_id=anchor,
        digest=digest,
        used_by_framework=False,
        native_signal="native_trajectory_memory",
    )

    if round_idx == 1:
        _, counts = _build_memory(memory_dir, trajectories, base_workflows=[(anchor, seed_body)])
        mode = {"mode": "cold_build", **counts}
    else:
        mode = _memp_append_trajectories(memory_dir, trajectories)
    lc.snapshot_dir(memory_dir, cell_dir / "rounds" / str(round_idx) / "memory")
    native_snapshot = round_dir / "native_state" / "memp"
    lc.snapshot_dir(cell_dir / "native_state" / "memp", native_snapshot)

    body, bridge = render_memory_to_skill(memory_dir=memory_dir, skill_id=anchor)
    if not bridge.get("workflow_found"):
        bridge["canonical_export_failed"] = True
    meta = {
        **mode,
        **bridge,
        "digest_audit": digest_audit,
        "shared_prompt_packet": prompt_meta,
        "native_state_snapshot": str(native_snapshot),
    }
    train_mean = lc.mean_pass_rate(
        [{"outcome": "ok", "pass_rate": float(b.get("pass_rate", 0))} for b in bundles]
    )
    return body, meta, bundles, train_mean


def run_evoskill_cell(
    *,
    anchor: str,
    train_keys: list[str],
    cells: dict,
    cell_dir: Path,
    base_seed,
    manifest: dict,
    solver_cfg: dict,
    editor_cfg: dict,
    timeout_s: int,
    iterations: int,
) -> tuple[str, dict, Path]:
    from _shared.evoskill_bridge import TaskRef, evolve_skill

    train_refs = []
    for tk in train_keys:
        td = Path(cells["tasks"][tk]["raw_sbbench_path"])
        train_refs.append(
            TaskRef(
                task_key=tk,
                task_dir=td,
                instruction=fpv._instruction(cells, tk),
            )
        )
    repo = cell_dir / "evoskill_repo"
    produced = cell_dir / "evoskill_produced"
    solve_root = cell_dir / "evoskill_solves"
    proposer_model = (
        editor_cfg.get("evoskill_model")
        or editor_cfg.get("served_id_hint")
        or editor_cfg.get("id")
        or ""
    )
    env_updates = {
        "EVO_OPENAI_COMPAT_BASE_URL": editor_cfg.get("api_base") or None,
        "EVO_OPENAI_COMPAT_API_KEY": _api_key(editor_cfg),
        "EVO_OPENAI_COMPAT_MODEL": proposer_model,
        "EVO_OPENAI_COMPAT_TEMPERATURE": str(editor_cfg.get("temperature", 0.7)),
        "EVO_OPENAI_COMPAT_MAX_TOKENS": str(editor_cfg.get("max_output_tokens") or 8192),
    }
    with _patched_env(env_updates):
        result = evolve_skill(
            skill_names=[anchor],
            train_refs=train_refs,
            base_seed=base_seed,
            repo=repo,
            solve_root=solve_root,
            produced_state=produced,
            solver_cfg=solver_cfg,
            api_key=_api_key(solver_cfg),
            timeout_s=timeout_s,
            max_iterations=iterations,
            concurrency=int(
                os.environ.get(
                    "EVOSKILL_CONCURRENCY",
                    os.environ.get("EVO_CONCURRENCY", "1"),
                )
            ),
            model=proposer_model,
        )
    anchor_md = produced / anchor / "SKILL.md"
    body = anchor_md.read_text(encoding="utf-8") if anchor_md.is_file() else ""
    meta = {
        "editor": "evoskill",
        "best_program": result.best_program,
        "best_score": result.best_score,
        "iterations_completed": result.iterations_completed,
        "frontier": result.frontier,
        "harness_model": solver_cfg["id"],
        "proposer_model": editor_cfg.get("id"),
        "shared_prompt_packet": {
            "version": lc.SHARED_EDITOR_PROMPT_VERSION,
            "framework": "evoskill",
            "skill_id": anchor,
            "shared_policy_sha256": lc.sha256_text(
                lc.SHARED_EDITOR_POLICY_TEMPLATE.format(skill_id=anchor)
            ),
            "shared_policy_template_sha256": lc.sha256_text(lc.SHARED_EDITOR_POLICY_TEMPLATE),
            "used_by_framework": False,
            "native_signal": "evoskill_frontier_failures_and_scores",
            "reason": (
                "EvoSkill is evaluated through its native proposer/generator/frontier "
                "loop; the loop consumes train failures and scorer outputs directly "
                "rather than a trace-digest editor prompt."
            ),
        },
    }
    return body, meta, repo


def run_evolution_cell(
    *,
    anchor: str,
    train_keys: list[str],
    cells: dict,
    cell_id: str,
    cell_dir: Path,
    base_seed,
    manifest: dict,
    specs: list[dict],
    rounds: int,
    timeout_s: float,
) -> tuple[str, dict, list[float]]:
    dispatch = fpv._model_dispatch(manifest)
    seed_root: Path | None = None
    curves: list[float] = []
    final_state: dict = {}
    for r in range(1, rounds + 1):
        work = cell_dir / "evolution_work" / f"round{r}"
        records, state, notes = fpv._produce_evolution(
            row={"row_id": 3, "label": f"loop-r{r}", "task_diversity": ""},
            cell_id=cell_id,
            specs=specs,
            base_seed=base_seed,
            cells=cells,
            model_dispatch=dispatch,
            manifest=manifest,
            work_root=work,
            timeout_s=timeout_s,
            out_path=cell_dir / f"evolution_round{r}.json",
            seed_skill_root=seed_root,
            validation_specs=None,
        )
        seed_root = Path(state.get("skills_dir") or "")
        final_state = state
        round_skill = seed_root / anchor / "SKILL.md"
        if round_skill.is_file():
            out = cell_dir / "rounds" / str(r)
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(round_skill, out / "SKILL.md")
            lc.write_json(
                out / "edit_meta.json",
                {
                    "editor": "evolution",
                    "round": r,
                    "skills_dir": str(seed_root),
                    "notes": notes,
                    "quality": _round_quality_flags(
                        "evolution",
                        {"editor": "evolution", "notes": notes},
                    ),
                },
            )
        prs = [
            float(rec.get("pytest_pass_rate") or 0)
            for rec in records
            if rec.get("pytest_status") == "completed"
        ]
        curves.append(sum(prs) / len(prs) if prs else 0.0)
    skills_dir = Path(final_state.get("skills_dir") or "")
    body_path = skills_dir / anchor / "SKILL.md"
    body = body_path.read_text(encoding="utf-8") if body_path.is_file() else ""
    return body, {"editor": "evolution", "rounds": rounds, "skills_dir": str(skills_dir)}, curves


def lane_c_confirm(
    *,
    promoted_body: str,
    seed_body: str,
    train_keys: list[str],
    cells: dict,
    solver_cfg: dict,
    confirm_trials: int,
    timeout_s: int,
    out_path: Path,
) -> dict:
    solver_key = _api_key(solver_cfg)
    per_task: list[dict] = []
    for tk in train_keys:
        task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
        instr = fpv._instruction(cells, tk)
        seed_rates: list[float] = []
        promo_rates: list[float] = []
        seed_trials: list[dict[str, Any]] = []
        promo_trials: list[dict[str, Any]] = []
        for ti in range(confirm_trials):
            seed = 100 + ti
            for body, bucket, trial_bucket in (
                (seed_body, seed_rates, seed_trials),
                (promoted_body, promo_rates, promo_trials),
            ):
                cache = (
                    out_path.parent
                    / "confirm"
                    / tk
                    / f"{bucket is promo_rates and 'promoted' or 'seed'}_trial{ti}"
                )
                res = _neutral_solve(
                    task_dir=task_dir,
                    instruction=instr,
                    skill_body=body,
                    solver_cfg=solver_cfg,
                    api_key=solver_key,
                    cache_dir=cache,
                    seed=seed,
                    timeout_s=timeout_s,
                )
                trial = {
                    "trial": ti,
                    "seed": seed,
                    "outcome": res.get("outcome"),
                    "pass_rate": res.get("pass_rate"),
                    "total_tests": res.get("total_tests"),
                    "pytest_status": res.get("pytest_status"),
                    "elapsed_s": res.get("elapsed_s"),
                    "error": res.get("error"),
                }
                trial_bucket.append(trial)
                if res.get("pass_rate") is not None:
                    bucket.append(float(res.get("pass_rate") or 0))
        per_task.append(
            {
                "task_id": tk,
                "seed_trials": seed_trials,
                "promoted_trials": promo_trials,
                "seed_mean": sum(seed_rates) / len(seed_rates) if seed_rates else 0.0,
                "promoted_mean": sum(promo_rates) / len(promo_rates) if promo_rates else 0.0,
                "delta": (sum(promo_rates) / len(promo_rates) if promo_rates else 0.0)
                - (sum(seed_rates) / len(seed_rates) if seed_rates else 0.0),
            }
        )
    overall_seed = sum(t["seed_mean"] for t in per_task) / len(per_task)
    overall_promo = sum(t["promoted_mean"] for t in per_task) / len(per_task)
    out = {
        "schema": "lane-c-confirmation-v1",
        "per_task": per_task,
        "seed_mean": overall_seed,
        "promoted_mean": overall_promo,
        "delta": overall_promo - overall_seed,
    }
    lc.write_json(out_path, out)
    return out


def _mean_condition_pass_rate(result: dict, condition: str) -> float:
    rates: list[float] = []
    for trials in (result.get(condition) or {}).values():
        for trial in trials if isinstance(trials, list) else []:
            # Some native runtimes can fail output parsing while still leaving a
            # pytest-scored workspace. Keep those as scored low-quality attempts
            # instead of making the condition look empty.
            if trial.get("pass_rate") is not None:
                rates.append(float(trial.get("pass_rate") or 0.0))
    return sum(rates) / len(rates) if rates else 0.0


def _trial_pass_rate(trial: Any) -> float | None:
    if isinstance(trial, dict):
        value = trial.get("pass_rate")
    else:
        value = trial
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarize_task_trials(
    task_trials: dict[str, list[Any]],
    *,
    expected_trials: int,
    pass_at_k: int = 5,
) -> dict[str, Any]:
    """Summarize repeated attempts without hiding missing/null attempts.

    ``pass_rate`` is averaged per task, padding missing trials to zero up to
    ``expected_trials``. ``pass@5`` and ``best_of_n`` are task-level metrics:
    a task is solved if any considered trial reaches pass_rate >= 1.0, and
    best_of_n is the mean best pass_rate across tasks.
    """
    expected = max(1, int(expected_trials or 1))
    k = max(1, int(pass_at_k or expected))
    per_task: list[dict[str, Any]] = []
    total_expected_trials = 0
    total_observed_trials = 0
    total_scored_trials = 0
    total_full_pass_trials = 0
    task_mean_sum = 0.0
    best_sum = 0.0
    solved = 0

    for task_id in sorted(task_trials):
        trials = list(task_trials.get(task_id) or [])
        observed_rates = [_trial_pass_rate(t) for t in trials[:expected]]
        scored_rates = [r for r in observed_rates if r is not None]
        padded_rates = [(r if r is not None else 0.0) for r in observed_rates]
        if len(padded_rates) < expected:
            padded_rates.extend([0.0] * (expected - len(padded_rates)))
        considered = padded_rates[:expected]
        first_k = considered[: min(k, expected)]
        task_mean = sum(considered) / expected
        task_best = max(considered) if considered else 0.0
        task_solved = any(rate >= 1.0 for rate in first_k)
        best_trial = (
            max(range(len(considered)), key=lambda i: considered[i]) if considered else None
        )

        total_expected_trials += expected
        total_observed_trials += len(trials)
        total_scored_trials += len(scored_rates)
        total_full_pass_trials += sum(1 for r in scored_rates if r >= 1.0)
        task_mean_sum += task_mean
        best_sum += task_best
        solved += int(task_solved)
        per_task.append(
            {
                "task_id": task_id,
                "n_trials": expected,
                "n_observed_trials": len(trials),
                "n_scored_trials": len(scored_rates),
                "pass_rate": round(task_mean, 4),
                "pass@5": bool(task_solved),
                "pass_at_5": bool(task_solved),
                "best_of_n": round(task_best, 4),
                "solved": bool(task_solved),
                "best_trial": best_trial,
                "rates": [round(r, 4) for r in considered],
            }
        )

    n_tasks = len(per_task)
    pass_at_5 = solved / n_tasks if n_tasks else 0.0
    return {
        "n_tasks": n_tasks,
        "n_trials_per_task": expected,
        "n_trials": total_expected_trials,
        "n_observed_trials": total_observed_trials,
        "n_scored_trials": total_scored_trials,
        "n_full_pass_trials": total_full_pass_trials,
        "pass_rate": round(task_mean_sum / n_tasks, 4) if n_tasks else 0.0,
        "pass@5": round(pass_at_5, 4),
        "pass_at_5": round(pass_at_5, 4),
        "best_of_n": round(best_sum / n_tasks, 4) if n_tasks else 0.0,
        "solved_tests": solved,
        "number_solved_tests": solved,
        "solved_task_ids": [row["task_id"] for row in per_task if row["solved"]],
        "per_task": per_task,
    }


def _metrics_from_lane_c_confirmation(
    obj: dict[str, Any],
    *,
    expected_trials: int,
) -> dict[str, Any]:
    seed: dict[str, list[Any]] = {}
    promoted: dict[str, list[Any]] = {}
    for row in obj.get("per_task") or []:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        seed[task_id] = list(row.get("seed_trials") or [])
        promoted[task_id] = list(row.get("promoted_trials") or [])
    return {
        "conditions": {
            "seed": _summarize_task_trials(seed, expected_trials=expected_trials),
            "promoted": _summarize_task_trials(promoted, expected_trials=expected_trials),
        }
    }


def _metrics_from_lane_c_eval(
    obj: dict[str, Any],
    *,
    expected_trials: int,
) -> dict[str, Any]:
    by_condition: dict[str, dict[str, list[Any]]] = {}
    for task_id, ev in obj.items():
        if not isinstance(ev, dict):
            continue
        for cond, cres in (ev.get("conditions") or {}).items():
            trials = cres.get("trials") if isinstance(cres, dict) else None
            by_condition.setdefault(str(cond), {})[str(task_id)] = list(trials or [])
    return {
        "conditions": {
            cond: _summarize_task_trials(task_map, expected_trials=expected_trials)
            for cond, task_map in sorted(by_condition.items())
        }
    }


def _metrics_from_native_eval(
    obj: dict[str, Any],
    *,
    expected_trials: int,
) -> dict[str, Any]:
    by_condition: dict[str, dict[str, list[Any]]] = {}
    metadata_keys = {"framework", "train_confirm_only", "tasks"}
    for cond, task_map in obj.items():
        if cond in metadata_keys or not isinstance(task_map, dict):
            continue
        by_condition[str(cond)] = {
            str(task_id): list(trials or [])
            for task_id, trials in task_map.items()
            if isinstance(trials, list)
        }
    return {
        "conditions": {
            cond: _summarize_task_trials(task_map, expected_trials=expected_trials)
            for cond, task_map in sorted(by_condition.items())
        }
    }


def build_cell_metrics(
    *,
    cell_dir: Path,
    fw: str,
    skill: str,
    primary_lane: str,
    train_trials: int,
    test_trials: int,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "schema": "loop-cell-metrics-v1",
        "framework": fw,
        "skill": skill,
        "primary_lane": primary_lane,
        "train": {"primary_lane": primary_lane},
        "test": {"primary_lane": primary_lane},
    }

    confirmation = cell_dir / "confirmation.json"
    if confirmation.is_file():
        metrics["train"]["lane_c_confirmation"] = _metrics_from_lane_c_confirmation(
            lc.read_json(confirmation),
            expected_trials=train_trials,
        )

    native_confirm = cell_dir / "native_train_confirm.json"
    if native_confirm.is_file():
        metrics["train"]["lane_n_confirmation"] = _metrics_from_native_eval(
            lc.read_json(native_confirm),
            expected_trials=train_trials,
        )

    lane_c_eval = cell_dir / "lane_c_eval.json"
    if lane_c_eval.is_file():
        metrics["test"]["lane_c"] = _metrics_from_lane_c_eval(
            lc.read_json(lane_c_eval),
            expected_trials=test_trials,
        )

    lane_n_eval = cell_dir / "lane_n_eval.json"
    if lane_n_eval.is_file():
        metrics["test"]["lane_n"] = _metrics_from_native_eval(
            lc.read_json(lane_n_eval),
            expected_trials=test_trials,
        )

    lc.write_json(cell_dir / "metrics.json", metrics)
    return metrics


def _native_confirm_trial_count(confirm_trials: int) -> int:
    cap_text = os.environ.get("EVO_NATIVE_CONFIRM_TRIALS_CAP")
    cap = int(cap_text) if cap_text else int(confirm_trials)
    return max(1, min(int(confirm_trials), cap))


def _score_canonical_train_body(
    *,
    body: str,
    train_keys: list[str],
    cells: dict,
    solver_cfg: dict,
    cell_dir: Path,
    round_idx: int,
    timeout_s: int,
) -> tuple[float, dict]:
    n_trials = max(1, int(os.environ.get("EVO_POST_UPDATE_TRAIN_SCORE_TRIALS", "1") or "1"))
    solver_key = _api_key(solver_cfg)
    results: dict[str, Any] = {"condition": "post_update_canonical", "tasks": {}}
    jobs: list[tuple[str, int]] = [(tk, ti) for tk in train_keys for ti in range(n_trials)]
    max_workers = max(1, min(len(jobs), int(os.environ.get("SMOKE4WAY_PARALLEL", "1") or "1")))

    def _run_one(tk: str, ti: int) -> tuple[str, dict]:
        task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
        instr = fpv._instruction(cells, tk)
        cache = (
            cell_dir / "rounds" / str(round_idx) / "post_update_train_score" / tk / f"trial_{ti}"
        )
        return tk, _neutral_solve(
            task_dir=task_dir,
            instruction=instr,
            skill_body=body,
            solver_cfg=solver_cfg,
            api_key=solver_key,
            cache_dir=cache,
            seed=round_idx * 1000 + ti,
            timeout_s=timeout_s,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_run_one, tk, ti) for tk, ti in jobs]
        for fut in as_completed(futs):
            tk, res = fut.result()
            results.setdefault("post_update_canonical", {}).setdefault(tk, []).append(res)
    mean = _mean_condition_pass_rate(results, "post_update_canonical")
    results["mean_pass_rate"] = mean
    lc.write_json(cell_dir / "rounds" / str(round_idx) / "post_update_train_score.json", results)
    return mean, results


def run_lane_n_eval(
    *,
    fw: str,
    anchor: str,
    train_keys: list[str],
    test_keys: list[str],
    cells: dict,
    manifest: dict,
    cell_dir: Path,
    n_trials: int,
    timeout_s: int,
    train_confirm_only: bool = False,
    conditions: tuple[str, ...] | None = None,
    output_path: Path | None = None,
) -> dict:
    from _shared import native_framework_runners as nfr

    keys = train_keys if train_confirm_only else test_keys
    solver_cfg = manifest["models"]["primary_eval_solver"]
    refl_cfg = manifest["models"]["reflector"]
    emb_cfg = manifest["models"]["embedding"]
    results: dict[str, Any] = {
        "framework": fw,
        "train_confirm_only": train_confirm_only,
        "tasks": {},
    }

    if fw == "memento":
        trained_home = cell_dir / "native_state" / "memento"
        seed_home = cell_dir / "native_state" / "memento_seed"
        if not seed_home.is_dir():
            sb = cell_dir / "seed_body.txt"
            seed_text = sb.read_text(encoding="utf-8") if sb.is_file() else ""
            _init_memento_home(seed_home, anchor, seed_text)
        available = {"seed_state": seed_home, "trained_state": trained_home}
        active_conditions = conditions or ("seed_state", "trained_state")
        for cond in active_conditions:
            home = available[cond]
            for tk in keys:
                task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
                instr = fpv._instruction(cells, tk)
                trials = []
                for ti in range(n_trials):
                    cache = cell_dir / "lane_n" / cond / tk / f"trial_{ti}"
                    res = nfr.run_memento_test(
                        task_dir=task_dir,
                        raw_sbbench_path=task_dir,
                        instruction=instr,
                        condition="post_train",
                        cache_dir=cache,
                        solver_cfg=solver_cfg,
                        embedding_cfg=emb_cfg,
                        api_key=_api_key(solver_cfg),
                        emb_key=_api_key(emb_cfg),
                        post_train_test_home=home,
                        timeout_s=min(timeout_s, 600),
                    )
                    trials.append(res)
                results.setdefault(cond, {})[tk] = trials

    elif fw == "memp":
        old_disable = os.environ.get("EVO_MEMP_EVAL_DISABLE_RUN_TESTS")
        # MemP's native loop learns and repairs through train-task feedback.
        # Keep held-out Lane N blind, but do not blind the train-confirm phase
        # that is meant to verify whether the persisted memory improved.
        os.environ["EVO_MEMP_EVAL_DISABLE_RUN_TESTS"] = "0" if train_confirm_only else "1"
        try:
            trained_mem = cell_dir / "native_state" / "memp" / "memory"
            seed_mem = cell_dir / "native_state" / "memp_seed" / "memory"
            if not (seed_mem / "direct" / "documents.json").is_file():
                from evolution.adapters import memp_adapter
                from evolution.adapters.memp_adapter import _build_memory

                memp_adapter.install_monkeypatches(
                    embedding_model=emb_cfg["id"],
                    embedding_base=emb_cfg["api_base"],
                    embedding_key=_api_key(emb_cfg),
                    reflector_model=refl_cfg["id"],
                    reflector_base=refl_cfg["api_base"],
                    reflector_key=_api_key(refl_cfg),
                )
                seed_mem.parent.mkdir(parents=True, exist_ok=True)
                seed_body = ""
                sb = cell_dir / "seed_body.txt"
                if sb.is_file():
                    seed_body = sb.read_text(encoding="utf-8")
                _build_memory(seed_mem, [], base_workflows=[(anchor, seed_body)])
            available = {"seed_state": seed_mem, "trained_state": trained_mem}
            active_conditions = conditions or ("seed_state", "trained_state")
            for cond in active_conditions:
                mem_dir = available[cond]
                for tk in keys:
                    task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
                    instr = fpv._instruction(cells, tk)
                    trials = []
                    for ti in range(n_trials):
                        cache = cell_dir / "lane_n" / cond / tk / f"trial_{ti}"
                        res = nfr.run_memp_test(
                            task_dir=task_dir,
                            raw_sbbench_path=task_dir,
                            instruction=instr,
                            condition="post_train",
                            cache_dir=cache,
                            solver_cfg=solver_cfg,
                            reflector_cfg=refl_cfg,
                            embedding_cfg=emb_cfg,
                            api_key=_api_key(solver_cfg),
                            refl_key=_api_key(refl_cfg),
                            emb_key=_api_key(emb_cfg),
                            post_train_memory_dir=mem_dir,
                            timeout_s=min(timeout_s, 600),
                        )
                        trials.append(res)
                    results.setdefault(cond, {})[tk] = trials
        finally:
            if old_disable is None:
                os.environ.pop("EVO_MEMP_EVAL_DISABLE_RUN_TESTS", None)
            else:
                os.environ["EVO_MEMP_EVAL_DISABLE_RUN_TESTS"] = old_disable

    elif fw == "evoskill":
        repo = cell_dir / "evoskill_repo"
        active_conditions = conditions or ("seed_program", "best_program")
        for cond in active_conditions:
            for tk in keys:
                task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
                instr = fpv._instruction(cells, tk)
                trials = []
                for ti in range(n_trials):
                    cache = cell_dir / "lane_n" / cond / tk / f"trial_{ti}"
                    res = nfr.run_evoskill_test(
                        task_dir=task_dir,
                        instruction=instr,
                        condition=cond,
                        cache_dir=cache,
                        repo_dir=repo,
                        solver_cfg=solver_cfg,
                        api_key=_api_key(solver_cfg),
                        timeout_s=min(timeout_s, 900),
                    )
                    trials.append(res)
                results.setdefault(cond, {})[tk] = trials

    lc.write_json(
        output_path
        or cell_dir / ("native_train_confirm.json" if train_confirm_only else "lane_n_eval.json"),
        results,
    )
    return results


def _assert_lane_n_nonempty(result: dict, *, fw: str, label: str) -> None:
    conds = (
        ("seed_program", "best_program") if fw == "evoskill" else ("seed_state", "trained_state")
    )
    missing: list[str] = []
    for cond in conds:
        task_map = result.get(cond) or {}
        ok = [
            trial
            for trials in task_map.values()
            for trial in (trials if isinstance(trials, list) else [])
            if trial.get("pass_rate") is not None
        ]
        if not ok:
            missing.append(cond)
    if missing:
        print(f"{label}: all-null native condition(s): {missing}", file=sys.stderr)
        raise SystemExit(2)


def _assert_lane_c_nonempty(evals: list[tuple[str, dict]], *, label: str) -> None:
    missing: list[str] = []
    for cond in ("default_skill", "post_train"):
        ok_total = 0
        for _tk, ev in evals:
            c = (ev.get("conditions") or {}).get(cond) or {}
            ok_total += int(c.get("n_ok") or 0)
        if ok_total == 0:
            missing.append(cond)
    if missing:
        print(f"{label}: all-null canonical condition(s): {missing}", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="AFTER iterative loop harness (loop-v1)")
    parser.add_argument("--framework", required=True)
    parser.add_argument("--subset", default=str(HERE / "subset.json"))
    parser.add_argument("--skill", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--n-trials", type=int, default=5)
    parser.add_argument("--confirm-trials", type=int, default=3)
    parser.add_argument("--timeout-s", type=int, default=2400)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--train-confirm-only", action="store_true")
    parser.add_argument("--skip-lane-n", action="store_true")
    parser.add_argument("--skip-lane-c-eval", action="store_true")
    args = parser.parse_args()

    fw = args.framework.strip().lower()
    blk = json.loads(Path(args.subset).read_text(encoding="utf-8"))["skills"][args.skill]
    train_rels = list(blk["train"])
    test_rels = list(blk["test"])
    train_tuples = [ab._task_skill_role(r) for r in train_rels]
    test_tuples = [ab._task_skill_role(r) for r in test_rels]
    cell_dir = (ART_BASE / "cells" / fw / args.skill).resolve()
    cell_dir.mkdir(parents=True, exist_ok=True)
    train_tuples, train_excluded = _filter_materializable(train_tuples)
    test_tuples, test_excluded = _filter_materializable(test_tuples)
    materialization_exclusions = {
        "train": train_excluded,
        "test": test_excluded,
    }
    lc.write_json(cell_dir / "materialization_exclusions.json", materialization_exclusions)
    if not train_tuples:
        raise SystemExit(f"all train tasks for {args.skill} failed materialization")
    if not test_tuples:
        raise SystemExit(f"all test tasks for {args.skill} failed materialization")
    cells, train_keys, test_keys = ab._build_cells_multi(train_tuples, test_tuples, args.skill)
    cell_id = cells["cells"][0]["cell_id"]
    manifest = ab._build_manifest()
    lc.assert_role_manifest(manifest)
    editor_cfg = lc.editor_cfg_from_manifest(manifest)
    solver_cfg = manifest["models"]["primary_eval_solver"]
    embedding_cfg = manifest["models"]["embedding"]

    iso = (ART_BASE / "iso" / fw / args.skill).resolve()
    iso.mkdir(parents=True, exist_ok=True)
    _setup_process_env(iso=iso, anchor=args.skill)

    specs = [
        {
            "task_id": k,
            "source_model_id": manifest["models"]["source_models_single"][0]["id"],
            "attempt_seed": 0,
        }
        for k in train_keys
    ]
    base_seed = resolve_base_skill_seed(cells=cells, specs=specs)
    seed_body = base_skill_bodies(base_seed)[0][1]
    lc.write_json(cell_dir / "roles_audit.json", lc.roles_snapshot(manifest))

    fairness = {
        "protocol_version": lc.PROTOCOL_VERSION,
        "framework": fw,
        "skill": args.skill,
        "materialization_filter": {
            "train_excluded": len(train_excluded),
            "test_excluded": len(test_excluded),
            "path": str(cell_dir / "materialization_exclusions.json"),
        },
        "models": lc.roles_snapshot(manifest),
        "editor_cfg_id": editor_cfg.get("id"),
        "feedback_policy": os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY"),
        "shared_editor_prompt": {
            "version": lc.SHARED_EDITOR_PROMPT_VERSION,
            "shared_policy_template_sha256": lc.sha256_text(lc.SHARED_EDITOR_POLICY_TEMPLATE),
            "path_per_round": "rounds/<round>/editor_prompt_packet.md",
            "consumed_as_trace_digest": ["hermes", "memento"],
            "audit_only_native_signal": {
                "noop": "noop_control",
                "memp": "native_trajectory_memory",
                "evoskill": "evoskill_frontier_failures_and_scores",
            },
            "policy": (
                "Use only train evidence, keep framework-native memory/runtime, "
                "generalize procedures, and do not copy pytest source or hidden answers."
            ),
        },
        "train_evidence_policy": {
            "split": "train_only",
            "contains_task_instruction": True,
            "contains_solver_code": True,
            "contains_pytest_diagnostics": True,
            "contains_native_stdout_stderr": True,
            "contains_native_trajectory_preview_when_available": True,
            "feedback_policy": os.environ.get("EVO_ADAPTER_FEEDBACK_POLICY"),
        },
        "reflect_blind": os.environ.get("EVO_REFLECT_BLIND"),
        "memp_eval_disable_run_tests": os.environ.get("EVO_MEMP_EVAL_DISABLE_RUN_TESTS"),
        "memp_eval_disable_run_tests_effective_eval": fw == "memp",
        "hermes_runtime_budget": {
            "native_timeout_s": os.environ.get("EVO_HERMES_NATIVE_TIMEOUT_S"),
            "request_timeout_s": os.environ.get("EVO_HERMES_REQUEST_TIMEOUT_S"),
            "stale_timeout_s": os.environ.get("EVO_HERMES_STALE_TIMEOUT_S"),
            "train_max_iterations": os.environ.get("EVO_HERMES_TRAIN_MAX_ITER"),
            "edit_max_iterations": os.environ.get("EVO_HERMES_EDIT_MAX_ITER"),
            "train_parallel": os.environ.get("HERMES_TRAIN_PARALLEL"),
        },
        "memento_native_profile": os.environ.get("EVO_MEMENTO_NATIVE_SERVICES_PROFILE"),
        "memento_allow_direct_edit_fallback": os.environ.get(
            "EVO_MEMENTO_ALLOW_DIRECT_EDIT_FALLBACK", "0"
        ),
        "memento_runtime_budget": {
            "max_iterations": int(os.environ.get("EVO_MEMENTO_MAX_ITERATIONS", "24") or "24"),
            "train_max_iterations": int(
                os.environ.get("EVO_MEMENTO_TRAIN_MAX_ITERATIONS", "24") or "24"
            ),
            "eval_max_iterations": int(
                os.environ.get("EVO_MEMENTO_EVAL_MAX_ITERATIONS", "28") or "28"
            ),
            "edit_max_iterations": int(
                os.environ.get("EVO_MEMENTO_EDIT_MAX_ITERATIONS", "8") or "8"
            ),
            "llm_timeout_s": float(os.environ.get("EVO_MEMENTO_LLM_TIMEOUT_S", "240") or "240"),
            "train_timeout_s": float(os.environ.get("EVO_MEMENTO_TRAIN_TIMEOUT_S", "600") or "600"),
            "eval_timeout_s": float(os.environ.get("EVO_MEMENTO_EVAL_TIMEOUT_S", "360") or "360"),
            "edit_timeout_s": float(os.environ.get("EVO_MEMENTO_EDIT_TIMEOUT_S", "60") or "60"),
            "direct_edit_timeout_s": float(
                os.environ.get(
                    "EVO_MEMENTO_DIRECT_EDIT_TIMEOUT_S",
                    "480",
                )
                or "480"
            ),
            "max_react_per_step": int(
                os.environ.get("EVO_MEMENTO_MAX_REACT_PER_STEP", "12") or "12"
            ),
            "max_replans": int(os.environ.get("EVO_MEMENTO_MAX_REPLANS", "4") or "4"),
        },
        "rounds": args.rounds,
        "experiences_per_round": len(train_keys),
        "update_calls": args.rounds if fw not in ("evoskill", "evolution") else 1,
        "native_confirm_trials": _native_confirm_trial_count(args.confirm_trials),
        "primary_lane": "lane_c" if fw in ("hermes", "evolution", "noop") else "lane_n",
        "declared_non_exercised": [
            "hermes_curator",
            "hermes_fts5",
            "memento_dream_daemon",
        ],
    }
    lc.write_json(cell_dir / "fairness.json", fairness)

    if args.train_confirm_only:
        native_confirm = run_lane_n_eval(
            fw=fw,
            anchor=args.skill,
            train_keys=train_keys,
            test_keys=test_keys,
            cells=cells,
            manifest=manifest,
            cell_dir=cell_dir,
            n_trials=_native_confirm_trial_count(args.confirm_trials),
            timeout_s=args.timeout_s,
            train_confirm_only=True,
        )
        _assert_lane_n_nonempty(native_confirm, fw=fw, label="native_train_confirm")
        return 0

    body = seed_body
    (cell_dir / "seed_body.txt").write_text(seed_body, encoding="utf-8")
    train_curve_path = cell_dir / "train_curve.json"
    train_curve: list[dict] = (
        lc.read_json(train_curve_path) if args.resume and train_curve_path.is_file() else []
    )
    all_no_op = all(bool(row.get("no_op", False)) for row in train_curve) if train_curve else True

    if fw == "evoskill":
        body, meta, repo = run_evoskill_cell(
            anchor=args.skill,
            train_keys=train_keys,
            cells=cells,
            cell_dir=cell_dir,
            base_seed=base_seed,
            manifest=manifest,
            solver_cfg=solver_cfg,
            editor_cfg={**editor_cfg, "evoskill_model": editor_cfg["id"]},
            timeout_s=args.timeout_s,
            iterations=args.rounds,
        )
        meta["quality"] = _round_quality_flags("evoskill", meta)
        train_curve.append({"round": 1, "train_mean": meta.get("best_score", 0), "meta": meta})
        lc.write_json(cell_dir / "rounds" / "1" / "edit_meta.json", meta)
        (cell_dir / "rounds" / "1" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
        (cell_dir / "rounds" / "1" / "SKILL.md").write_text(body, encoding="utf-8")
        lc.write_json(train_curve_path, train_curve)
        promoted_round = 1
        all_no_op = False
    elif fw == "evolution":
        body, meta, curves = run_evolution_cell(
            anchor=args.skill,
            train_keys=train_keys,
            cells=cells,
            cell_id=cell_id,
            cell_dir=cell_dir,
            base_seed=base_seed,
            manifest=manifest,
            specs=specs,
            rounds=args.rounds,
            timeout_s=float(args.timeout_s),
        )
        for i, m in enumerate(curves, 1):
            train_curve.append({"round": i, "train_mean": m})
        lc.write_json(train_curve_path, train_curve)
        promoted_round = max(range(1, len(curves) + 1), key=lambda i: curves[i - 1])
        promoted_path = (
            cell_dir
            / "evolution_work"
            / f"round{promoted_round}"
            / "state"
            / "evolution_skills"
            / args.skill
            / "SKILL.md"
        )
        if promoted_path.is_file():
            body = promoted_path.read_text(encoding="utf-8")
        all_no_op = False
    else:
        start_round = 1
        if args.resume:
            native_state = cell_dir / "native_state"
            for r in range(args.rounds, 0, -1):
                skill_path = cell_dir / "rounds" / str(r) / "SKILL.md"
                if not skill_path.is_file():
                    continue
                if fw in ("memento", "memp"):
                    manifest_path = cell_dir / "rounds" / str(r) / "native_state_manifest.json"
                    if not manifest_path.is_file():
                        continue
                    try:
                        expected_manifest = lc.read_json(manifest_path)
                    except Exception:
                        continue
                    if expected_manifest != lc.sha256_tree(native_state):
                        continue
                start_round = r + 1
                body = skill_path.read_text(encoding="utf-8")
                break

        if fw == "memp":
            from evolution.adapters import memp_adapter
            from evolution.adapters.memp_adapter import _build_memory

            memory_dir = cell_dir / "native_state" / "memp" / "memory"
            memp_adapter.install_monkeypatches(
                embedding_model=embedding_cfg["id"],
                embedding_base=embedding_cfg["api_base"],
                embedding_key=_api_key(embedding_cfg),
                reflector_model=editor_cfg.get("reflector_id", ""),
                reflector_base=editor_cfg.get("reflector_api_base", ""),
                reflector_key=_api_key(editor_cfg),
            )
            if start_round == 1 and memory_dir.parent.exists():
                shutil.rmtree(memory_dir.parent)
            if start_round == 1:
                _build_memory(memory_dir, [], base_workflows=[(args.skill, seed_body)])

        if fw == "memento":
            home = cell_dir / "native_state" / "memento"
            if start_round == 1 and home.exists():
                shutil.rmtree(home)
            if start_round == 1:
                _init_memento_home(home, args.skill, seed_body)

        for r in range(start_round, args.rounds + 1):
            round_dir = cell_dir / "rounds" / str(r)
            round_dir.mkdir(parents=True, exist_ok=True)
            if fw == "noop":
                bundles = []
                solves = []
                solver_key = _api_key(solver_cfg)
                for ti, tk in enumerate(train_keys):
                    task_dir = Path(cells["tasks"][tk]["raw_sbbench_path"])
                    instr = fpv._instruction(cells, tk)
                    cache = cell_dir / "rounds" / str(r) / "solve" / tk
                    res = _neutral_solve(
                        task_dir=task_dir,
                        instruction=instr,
                        skill_body=body,
                        solver_cfg=solver_cfg,
                        api_key=solver_key,
                        cache_dir=cache,
                        seed=r * 10 + ti,
                        timeout_s=args.timeout_s,
                    )
                    solves.append(res)
                    bundles.append(
                        lc.bundle_from_solve_result(
                            task_key=tk,
                            instruction=instr,
                            role=fpv._role_for_task(cells, tk) or "",
                            cache_dir=cache,
                            solve_result=res,
                        )
                    )
                train_mean = lc.mean_pass_rate(solves)
                digest, digest_audit = lc.build_digest_from_bundles(bundles)
                (round_dir / "digest.md").write_text(digest, encoding="utf-8")
                lc.write_json(round_dir / "digest_audit.json", digest_audit)
                _prompt_packet, prompt_meta = _write_editor_prompt_packet(
                    round_dir,
                    framework="noop",
                    skill_id=args.skill,
                    digest=digest,
                    used_by_framework=False,
                    native_signal="noop_control",
                )
                new_body, meta = _noop_edit(body)
                meta["train_mean"] = train_mean
                meta["digest_audit"] = digest_audit
                meta["shared_prompt_packet"] = prompt_meta
            elif fw == "hermes":
                new_body, meta, bundles, train_mean = run_hermes_round(
                    round_idx=r,
                    body=body,
                    anchor=args.skill,
                    train_keys=train_keys,
                    cells=cells,
                    cell_dir=cell_dir,
                    manifest=manifest,
                    solver_cfg=solver_cfg,
                    editor_cfg=editor_cfg,
                    timeout_s=args.timeout_s,
                )
            elif fw == "memento":
                new_body, meta, bundles, train_mean = run_memento_round(
                    round_idx=r,
                    anchor=args.skill,
                    train_keys=train_keys,
                    cells=cells,
                    cell_dir=cell_dir,
                    solver_cfg=solver_cfg,
                    editor_cfg=editor_cfg,
                    embedding_cfg=embedding_cfg,
                    timeout_s=args.timeout_s,
                )
            elif fw == "memp":
                new_body, meta, bundles, train_mean = run_memp_round(
                    round_idx=r,
                    anchor=args.skill,
                    seed_body=seed_body,
                    train_keys=train_keys,
                    cells=cells,
                    cell_dir=cell_dir,
                    manifest=manifest,
                    solver_cfg=solver_cfg,
                    editor_cfg=editor_cfg,
                    embedding_cfg=embedding_cfg,
                    timeout_s=args.timeout_s,
                )
            else:
                raise ValueError(f"unknown framework {fw}")

            if r == 1 and fw in ("hermes", "noop"):
                first_tk = train_keys[0]
                audit = cell_dir / "rounds" / "1" / "solve" / first_tk / "prompt_audit.json"
                expected = solver_cfg.get("served_id_hint") or solver_cfg["id"]
                lc.assert_prompt_audit_model(audit, expected, label="first_solve")

            no_op = lc.sha256_text(new_body) == lc.sha256_text(body)
            if not no_op:
                all_no_op = False
            pre_update_train_mean = train_mean
            post_update_score: dict[str, Any] | None = None
            if fw == "hermes":
                train_mean, post_update_score = _score_canonical_train_body(
                    body=new_body,
                    train_keys=train_keys,
                    cells=cells,
                    solver_cfg=solver_cfg,
                    cell_dir=cell_dir,
                    round_idx=r,
                    timeout_s=args.timeout_s,
                )
            elif fw in ("memento", "memp"):
                post_update_score = run_lane_n_eval(
                    fw=fw,
                    anchor=args.skill,
                    train_keys=train_keys,
                    test_keys=test_keys,
                    cells=cells,
                    manifest=manifest,
                    cell_dir=cell_dir,
                    n_trials=max(
                        1,
                        int(os.environ.get("EVO_POST_UPDATE_TRAIN_SCORE_TRIALS", "1") or "1"),
                    ),
                    timeout_s=args.timeout_s,
                    train_confirm_only=True,
                    conditions=("trained_state",),
                    output_path=round_dir / "post_update_native_train_score.json",
                )
                train_mean = _mean_condition_pass_rate(post_update_score, "trained_state")
            meta["no_op"] = no_op
            meta["pre_update_train_mean"] = pre_update_train_mean
            meta["post_update_train_mean"] = train_mean
            meta["train_mean"] = train_mean
            meta["role_models"] = {
                "solver": solver_cfg.get("id"),
                "solver_served": solver_cfg.get("served_id_hint"),
                "evolver": editor_cfg.get("id"),
            }
            if post_update_score is not None:
                meta["post_update_score_path"] = (
                    str(round_dir / "post_update_native_train_score.json")
                    if fw in ("memento", "memp")
                    else str(round_dir / "post_update_train_score.json")
                )
            meta["quality"] = _round_quality_flags(fw, meta)
            (round_dir / "SKILL.md").write_text(new_body, encoding="utf-8")
            lc.write_json(round_dir / "bundles.json", bundles)
            lc.write_json(round_dir / "edit_meta.json", meta)
            lc.write_json(
                round_dir / "native_state_manifest.json", lc.sha256_tree(cell_dir / "native_state")
            )
            train_curve.append(
                {
                    "round": r,
                    "train_mean": train_mean,
                    "pre_update_train_mean": pre_update_train_mean,
                    "no_op": no_op,
                }
            )
            lc.write_json(train_curve_path, train_curve)
            body = new_body

        if train_curve:
            promoted_round = max(train_curve, key=lambda x: x["train_mean"])["round"]
            body = (cell_dir / "rounds" / str(promoted_round) / "SKILL.md").read_text(
                encoding="utf-8"
            )
        else:
            promoted_round = 0
            body = seed_body

    lc.write_json(cell_dir / "train_curve.json", train_curve)
    promoted_dir = cell_dir / "promoted"
    promoted_dir.mkdir(parents=True, exist_ok=True)
    promoted_body = body
    (promoted_dir / "SKILL.md").write_text(promoted_body, encoding="utf-8")
    promoted_anchor_dir = promoted_dir / args.skill
    promoted_anchor_dir.mkdir(parents=True, exist_ok=True)
    (promoted_anchor_dir / "SKILL.md").write_text(promoted_body, encoding="utf-8")

    primary_lane = "lane_c" if fw in ("hermes", "evolution", "noop") else "lane_n"
    confirm: dict | None = None
    if primary_lane == "lane_c":
        confirm = lane_c_confirm(
            promoted_body=promoted_body,
            seed_body=seed_body,
            train_keys=train_keys,
            cells=cells,
            solver_cfg=solver_cfg,
            confirm_trials=args.confirm_trials,
            timeout_s=args.timeout_s,
            out_path=cell_dir / "confirmation.json",
        )
    else:
        native_confirm = run_lane_n_eval(
            fw=fw,
            anchor=args.skill,
            train_keys=train_keys,
            test_keys=test_keys,
            cells=cells,
            manifest=manifest,
            cell_dir=cell_dir,
            n_trials=_native_confirm_trial_count(args.confirm_trials),
            timeout_s=args.timeout_s,
            train_confirm_only=True,
        )
        _assert_lane_n_nonempty(native_confirm, fw=fw, label="native_train_confirm")

    if not args.skip_lane_c_eval and fw not in ("noop",):
        state = {
            "skills_dir": str(promoted_dir),
            "skill_selection_policy": "anchor_only",
            "skill_names": [args.skill],
        }
        lane_c_fw = "evolution" if fw == "memp" else fw
        seed_bodies = [seed_body]
        evals = []
        for tk in test_keys:
            ev = ab._run_after_eval(
                fw=lane_c_fw,
                task_key=tk,
                cells=cells,
                manifest=manifest,
                state=state,
                conditions=["default_skill", "post_train"],
                n_trials=args.n_trials,
                timeout_s=args.timeout_s,
                out_dir=cell_dir,
                seed_skill_bodies=seed_bodies,
            )
            evals.append((tk, ev))
        _assert_lane_c_nonempty(evals, label="lane_c_eval")
        lc.write_json(cell_dir / "lane_c_eval.json", dict(evals))

        if fw == "memp":
            retrieval_state = {"memory_dir": str(cell_dir / "native_state" / "memp" / "memory")}
            retrieval_evals = []
            for tk in test_keys:
                ev = ab._run_after_eval(
                    fw="memp",
                    task_key=tk,
                    cells=cells,
                    manifest=manifest,
                    state=retrieval_state,
                    conditions=["default_skill", "post_train"],
                    n_trials=args.n_trials,
                    timeout_s=args.timeout_s,
                    out_dir=cell_dir,
                    seed_skill_bodies=seed_bodies,
                )
                retrieval_evals.append((tk, ev))
            _assert_lane_c_nonempty(retrieval_evals, label="memp_retrieval_neutral_eval")
            lc.write_json(cell_dir / "memp_retrieval_neutral_eval.json", dict(retrieval_evals))

    if not args.skip_lane_n and fw in ("memento", "memp", "evoskill"):
        lane_n_eval = run_lane_n_eval(
            fw=fw,
            anchor=args.skill,
            train_keys=train_keys,
            test_keys=test_keys,
            cells=cells,
            manifest=manifest,
            cell_dir=cell_dir,
            n_trials=args.n_trials,
            timeout_s=args.timeout_s,
        )
        _assert_lane_n_nonempty(lane_n_eval, fw=fw, label="lane_n_eval")

    metrics = build_cell_metrics(
        cell_dir=cell_dir,
        fw=fw,
        skill=args.skill,
        primary_lane=primary_lane,
        train_trials=(
            max(1, args.confirm_trials)
            if primary_lane == "lane_c"
            else _native_confirm_trial_count(args.confirm_trials)
        ),
        test_trials=max(1, args.n_trials),
    )
    quality_report = _collect_quality_report(cell_dir)
    lc.write_json(cell_dir / "quality_report.json", quality_report)
    summary = {
        "protocol_version": lc.PROTOCOL_VERSION,
        "framework": fw,
        "skill": args.skill,
        "framework_label": quality_report["framework_label"],
        "degraded": quality_report["degraded"],
        "degraded_reasons": quality_report["degraded_reasons"],
        "quality_warnings": quality_report["warnings"],
        "all_rounds_no_op": all_no_op,
        "promoted_round": promoted_round if fw not in ("evoskill",) else "frontier_best",
        "primary_lane": primary_lane,
        "confirmation_delta": confirm.get("delta") if confirm else None,
        "metrics_path": str(cell_dir / "metrics.json"),
        "cell_dir": str(cell_dir),
    }
    lc.write_json(cell_dir / "cell_summary.json", summary)
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
