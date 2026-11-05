#!/usr/bin/env python3
"""Collect AFTER loop train/test metrics from baselines artifacts.

Reads artifacts produced by ``experiments/baselines/afterbench_loop.py``.
The collector understands both canonical train/test JSON and native
Memento/MemP/EvoSkill JSON. For canonical train confirmation, per-trial
pass rates are reconstructed from the saved pytest logs.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from statistics import mean
from typing import Any


FRAMEWORKS = ("hermes", "memento", "memp", "evoskill", "evolution")
NATIVE_PAIRS = (
    ("seed_state", "trained_state"),
    ("seed_program", "best_program"),
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_pytest_pass_rate(path: Path) -> float | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    collected = None
    m = re.search(r"collected\s+(\d+)\s+items?", text)
    if m:
        collected = int(m.group(1))
    passed = 0
    failed = 0
    errors = 0
    for m in re.finditer(r"(\d+)\s+(passed|failed|error|errors)\b", text):
        n = int(m.group(1))
        kind = m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        else:
            errors = n
    total = collected or (passed + failed + errors)
    if total <= 0:
        return None
    return passed / total


def _condition_summary(task_trials: dict[str, list[float]]) -> dict[str, Any]:
    all_rates = [r for trials in task_trials.values() for r in trials]
    best_by_task = [max(trials) for trials in task_trials.values() if trials]
    solved = sum(1 for r in best_by_task if r >= 1.0 - 1e-12)
    n_tasks = len(best_by_task)
    n_trials_by_task = {task: len(trials) for task, trials in sorted(task_trials.items())}
    return {
        "n_tasks": n_tasks,
        "attempts": len(all_rates),
        "n_trials_min": min(n_trials_by_task.values()) if n_trials_by_task else 0,
        "n_trials_max": max(n_trials_by_task.values()) if n_trials_by_task else 0,
        "mean_pass_rate": mean(all_rates) if all_rates else None,
        "best_of_n_mean": mean(best_by_task) if best_by_task else None,
        "pass_at_n": (solved / n_tasks) if n_tasks else None,
        "solved_tasks": solved,
        "n_trials_by_task": n_trials_by_task,
    }


def _trial_rate(trial: dict[str, Any]) -> float | None:
    value = trial.get("pass_rate")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _conditions_from_eval_json(path: Path) -> dict[str, dict[str, list[float]]]:
    """Return condition -> task -> trial pass rates for lane_c_eval.json."""
    data = _load_json(path)
    out: dict[str, dict[str, list[float]]] = {}
    for task, payload in data.items():
        if not isinstance(payload, dict):
            continue
        for cond, cond_payload in (payload.get("conditions") or {}).items():
            trials = []
            for trial in cond_payload.get("trials") or []:
                rate = _trial_rate(trial)
                if rate is not None:
                    trials.append(rate)
            if trials:
                out.setdefault(cond, {})[task] = trials
    return out


def _conditions_from_native_json(path: Path) -> dict[str, dict[str, list[float]]]:
    """Return condition -> task -> trial pass rates for lane_n/native train JSON."""
    data = _load_json(path)
    out: dict[str, dict[str, list[float]]] = {}
    for cond in data:
        if cond in {"framework", "train_confirm_only", "tasks"}:
            continue
        cond_payload = data.get(cond)
        if not isinstance(cond_payload, dict):
            continue
        for task, trials_payload in cond_payload.items():
            trials = []
            for trial in trials_payload if isinstance(trials_payload, list) else []:
                rate = _trial_rate(trial)
                if rate is not None:
                    trials.append(rate)
            if trials:
                out.setdefault(cond, {})[task] = trials
    return out


def _conditions_from_canonical_confirm(cell_dir: Path) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {"seed": {}, "promoted": {}}
    confirm_root = cell_dir / "confirm"
    if not confirm_root.is_dir():
        return {}
    for task_dir in sorted(p for p in confirm_root.iterdir() if p.is_dir()):
        for cond in ("seed", "promoted"):
            rates = []
            for trial_dir in sorted(task_dir.glob(f"{cond}_trial*")):
                rate = _parse_pytest_pass_rate(trial_dir / "pytest.log")
                if rate is not None:
                    rates.append(rate)
            if rates:
                out[cond][task_dir.name] = rates
    return {cond: tasks for cond, tasks in out.items() if tasks}


def _delta(seed: dict[str, Any] | None, evolved: dict[str, Any] | None) -> dict[str, Any]:
    if not seed or not evolved:
        return {}
    fields = {}
    for key in ("mean_pass_rate", "best_of_n_mean", "pass_at_n", "solved_tasks"):
        a = seed.get(key)
        b = evolved.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            fields[f"delta_{key}"] = b - a
    return fields


def _scope_record(
    *,
    schema: str,
    framework: str,
    skill: str,
    scope: str,
    conditions: dict[str, dict[str, list[float]]],
    seed_condition: str,
    evolved_condition: str,
) -> dict[str, Any] | None:
    summaries = {cond: _condition_summary(tasks) for cond, tasks in conditions.items()}
    if seed_condition not in summaries and evolved_condition not in summaries:
        return None
    seed = summaries.get(seed_condition)
    evolved = summaries.get(evolved_condition)
    record = {
        "schema": schema,
        "framework": framework,
        "skill": skill,
        "scope": scope,
        "seed_condition": seed_condition,
        "evolved_condition": evolved_condition,
        "conditions": summaries,
        "seed": seed,
        "evolved": evolved,
        **_delta(seed, evolved),
    }
    return record


def collect_schema(schema_root: Path, schema: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cells_root = schema_root / "cells"
    if not cells_root.is_dir():
        return rows
    for fw_dir in sorted(p for p in cells_root.iterdir() if p.is_dir()):
        framework = fw_dir.name
        for cell_dir in sorted(p for p in fw_dir.iterdir() if p.is_dir()):
            skill = cell_dir.name

            # Train: canonical for Hermes/Evolution.
            canonical_train = _conditions_from_canonical_confirm(cell_dir)
            rec = _scope_record(
                schema=schema,
                framework=framework,
                skill=skill,
                scope="train_canonical",
                conditions=canonical_train,
                seed_condition="seed",
                evolved_condition="promoted",
            )
            if rec:
                rows.append(rec)

            # Train: native for Memento/MemP/EvoSkill.
            native_train_path = cell_dir / "native_train_confirm.json"
            if native_train_path.is_file():
                native_train = _conditions_from_native_json(native_train_path)
                for seed_cond, evolved_cond in NATIVE_PAIRS:
                    rec = _scope_record(
                        schema=schema,
                        framework=framework,
                        skill=skill,
                        scope="train_native",
                        conditions=native_train,
                        seed_condition=seed_cond,
                        evolved_condition=evolved_cond,
                    )
                    if rec:
                        rows.append(rec)

            # Test: common/canonical lane C.
            lane_c_path = cell_dir / "lane_c_eval.json"
            if lane_c_path.is_file():
                lane_c = _conditions_from_eval_json(lane_c_path)
                rec = _scope_record(
                    schema=schema,
                    framework=framework,
                    skill=skill,
                    scope="test_lane_c",
                    conditions=lane_c,
                    seed_condition="default_skill",
                    evolved_condition="post_train",
                )
                if rec:
                    rows.append(rec)

            # Test: native lane N.
            lane_n_path = cell_dir / "lane_n_eval.json"
            if lane_n_path.is_file():
                lane_n = _conditions_from_native_json(lane_n_path)
                for seed_cond, evolved_cond in NATIVE_PAIRS:
                    rec = _scope_record(
                        schema=schema,
                        framework=framework,
                        skill=skill,
                        scope="test_lane_n",
                        conditions=lane_n,
                        seed_condition=seed_cond,
                        evolved_condition=evolved_cond,
                    )
                    if rec:
                        rows.append(rec)
    return rows


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["schema"], row["framework"], row["scope"]), []).append(row)
    out = []
    for (schema, framework, scope), items in sorted(groups.items()):
        evolved = [r["evolved"] for r in items if r.get("evolved")]
        seed = [r["seed"] for r in items if r.get("seed")]
        deltas = [r for r in items if "delta_mean_pass_rate" in r]
        out.append(
            {
                "schema": schema,
                "framework": framework,
                "scope": scope,
                "n_cells": len(items),
                "seed_mean_pass_rate": mean([x["mean_pass_rate"] for x in seed])
                if seed
                else None,
                "evolved_mean_pass_rate": mean([x["mean_pass_rate"] for x in evolved])
                if evolved
                else None,
                "delta_mean_pass_rate": mean([x["delta_mean_pass_rate"] for x in deltas])
                if deltas
                else None,
                "seed_best_of_n_mean": mean([x["best_of_n_mean"] for x in seed]) if seed else None,
                "evolved_best_of_n_mean": mean([x["best_of_n_mean"] for x in evolved])
                if evolved
                else None,
                "delta_best_of_n_mean": mean([x["delta_best_of_n_mean"] for x in deltas])
                if deltas
                else None,
                "seed_pass_at_n": mean([x["pass_at_n"] for x in seed]) if seed else None,
                "evolved_pass_at_n": mean([x["pass_at_n"] for x in evolved]) if evolved else None,
                "delta_pass_at_n": mean([x["delta_pass_at_n"] for x in deltas])
                if deltas
                else None,
                "seed_solved_tasks": sum(int(x["solved_tasks"]) for x in seed),
                "evolved_solved_tasks": sum(int(x["solved_tasks"]) for x in evolved),
                "delta_solved_tasks": sum(int(x.get("delta_solved_tasks", 0)) for x in deltas),
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "schema",
        "framework",
        "skill",
        "scope",
        "seed_condition",
        "evolved_condition",
        "seed_mean_pass_rate",
        "evolved_mean_pass_rate",
        "delta_mean_pass_rate",
        "seed_best_of_n_mean",
        "evolved_best_of_n_mean",
        "delta_best_of_n_mean",
        "seed_pass_at_n",
        "evolved_pass_at_n",
        "delta_pass_at_n",
        "seed_solved_tasks",
        "evolved_solved_tasks",
        "delta_solved_tasks",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            seed = row.get("seed") or {}
            evolved = row.get("evolved") or {}
            flat = {
                "schema": row["schema"],
                "framework": row["framework"],
                "skill": row["skill"],
                "scope": row["scope"],
                "seed_condition": row["seed_condition"],
                "evolved_condition": row["evolved_condition"],
                "seed_mean_pass_rate": seed.get("mean_pass_rate"),
                "evolved_mean_pass_rate": evolved.get("mean_pass_rate"),
                "delta_mean_pass_rate": row.get("delta_mean_pass_rate"),
                "seed_best_of_n_mean": seed.get("best_of_n_mean"),
                "evolved_best_of_n_mean": evolved.get("best_of_n_mean"),
                "delta_best_of_n_mean": row.get("delta_best_of_n_mean"),
                "seed_pass_at_n": seed.get("pass_at_n"),
                "evolved_pass_at_n": evolved.get("pass_at_n"),
                "delta_pass_at_n": row.get("delta_pass_at_n"),
                "seed_solved_tasks": seed.get("solved_tasks"),
                "evolved_solved_tasks": evolved.get("solved_tasks"),
                "delta_solved_tasks": row.get("delta_solved_tasks"),
            }
            writer.writerow(flat)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    root = args.root.resolve()
    schemas = [p for p in sorted(root.iterdir()) if p.is_dir() and (p / "cells").is_dir()]
    if not schemas and (root / "cells").is_dir():
        schemas = [root]

    rows: list[dict[str, Any]] = []
    for schema_root in schemas:
        rows.extend(collect_schema(schema_root, schema_root.name))
    summary = aggregate(rows)
    report = {
        "root": str(root),
        "schemas": [p.name for p in schemas],
        "rows": rows,
        "summary": summary,
    }
    out = args.out or (root / "metrics.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(out.with_suffix(".rows.csv"), rows)
    with out.with_suffix(".summary.csv").open("w", newline="", encoding="utf-8") as fh:
        fieldnames = list(summary[0].keys()) if summary else []
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(summary)
    print(json.dumps({"out": str(out), "rows": len(rows), "summary_rows": len(summary)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
