#!/usr/bin/env python3
"""Collate loop-v1 pilot cells and evaluate the overfit gate per framework."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROTOCOL = "loop-v1"
GATE_DELTA = 0.05
GATE_MIN_ANCHORS = 2

LANE_C_FWS = {"hermes", "evolution", "noop"}
LANE_N_FWS = {"memento", "memp", "evoskill"}
DEFAULT_FRAMEWORKS = ("hermes", "memento", "memp", "evoskill", "evolution")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _mean_pass(trials: list[dict]) -> float | None:
    vals = [float(t.get("pass_rate") or 0) for t in trials if t.get("pass_rate") is not None]
    return sum(vals) / len(vals) if vals else None


def _read_metrics(cell_dir: Path) -> dict:
    path = cell_dir / "metrics.json"
    if not path.is_file():
        return {}
    try:
        obj = _read_json(path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return obj if isinstance(obj, dict) else {}


def _lane_c_delta(cell_dir: Path) -> float | None:
    p = cell_dir / "confirmation.json"
    if not p.is_file():
        return None
    obj = _read_json(p)
    return float(obj.get("delta", 0))


def _lane_n_delta(cell_dir: Path) -> float | None:
    p = cell_dir / "native_train_confirm.json"
    if not p.is_file():
        return None
    obj = _read_json(p)
    trained = obj.get("trained_state") or {}
    seed = obj.get("seed_state") or obj.get("seed_program") or {}
    if not trained or not seed:
        # evoskill uses best_program / seed_program keys
        trained = obj.get("best_program") or trained
        seed = obj.get("seed_program") or seed
    deltas: list[float] = []
    for tk, trials_t in trained.items():
        trials_s = (seed or {}).get(tk)
        if not isinstance(trials_s, list):
            continue
        mt = _mean_pass(trials_t if isinstance(trials_t, list) else [])
        ms = _mean_pass(trials_s)
        if mt is not None and ms is not None:
            deltas.append(mt - ms)
    return sum(deltas) / len(deltas) if deltas else None


def _collect_cells(root: Path, anchors: list[str]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    cells_root = root / "cells"
    if not cells_root.is_dir():
        return out
    for fw_dir in sorted(cells_root.iterdir()):
        if not fw_dir.is_dir():
            continue
        fw = fw_dir.name
        out.setdefault(fw, {})
        for skill_dir in sorted(fw_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill = skill_dir.name
            if anchors and skill not in anchors:
                continue
            summary_path = skill_dir / "cell_summary.json"
            summary = {}
            if summary_path.is_file():
                summary = _read_json(summary_path)
                if summary.get("protocol_version") != PROTOCOL:
                    continue
            train_curve = []
            tc_path = skill_dir / "train_curve.json"
            if tc_path.is_file():
                train_curve = _read_json(tc_path)
            primary = "lane_c" if fw in LANE_C_FWS else "lane_n"
            delta = _lane_c_delta(skill_dir) if primary == "lane_c" else _lane_n_delta(skill_dir)
            metrics = _read_metrics(skill_dir)
            out[fw][skill] = {
                "cell_dir": str(skill_dir),
                "primary_lane": primary,
                "train_delta": delta,
                "framework_label": summary.get("framework_label", "unknown"),
                "degraded": bool(summary.get("degraded", False)),
                "degraded_reasons": summary.get("degraded_reasons") or [],
                "quality_warnings": summary.get("quality_warnings") or [],
                "all_rounds_no_op": summary.get("all_rounds_no_op"),
                "train_curve": train_curve,
                "metrics": metrics,
            }
    return out


def _aggregate_condition_metrics(blocks: list[dict]) -> dict | None:
    per_task: list[dict] = []
    n_trials = 0
    n_observed = 0
    n_scored = 0
    n_full = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        rows = block.get("per_task") or []
        if isinstance(rows, list):
            per_task.extend(row for row in rows if isinstance(row, dict))
        n_trials += int(block.get("n_trials") or 0)
        n_observed += int(block.get("n_observed_trials") or 0)
        n_scored += int(block.get("n_scored_trials") or 0)
        n_full += int(block.get("n_full_pass_trials") or 0)
    if not per_task:
        return None
    n_tasks = len(per_task)
    solved = sum(1 for row in per_task if row.get("solved"))
    pass_rate = sum(float(row.get("pass_rate") or 0.0) for row in per_task) / n_tasks
    best_of_n = sum(float(row.get("best_of_n") or 0.0) for row in per_task) / n_tasks
    pass_at_5 = solved / n_tasks
    return {
        "n_tasks": n_tasks,
        "n_trials": n_trials,
        "n_observed_trials": n_observed,
        "n_scored_trials": n_scored,
        "n_full_pass_trials": n_full,
        "pass_rate": round(pass_rate, 4),
        "pass@5": round(pass_at_5, 4),
        "pass_at_5": round(pass_at_5, 4),
        "best_of_n": round(best_of_n, 4),
        "solved_tests": solved,
        "number_solved_tests": solved,
    }


def _aggregate_framework_metrics(per_anchor: dict[str, dict]) -> dict:
    buckets: dict[str, dict[str, dict[str, list[dict]]]] = {"train": {}, "test": {}}
    for row in per_anchor.values():
        metrics = row.get("metrics") or {}
        if not isinstance(metrics, dict):
            continue
        for split in ("train", "test"):
            split_obj = metrics.get(split) or {}
            if not isinstance(split_obj, dict):
                continue
            for lane, lane_obj in split_obj.items():
                if not isinstance(lane_obj, dict):
                    continue
                conditions = lane_obj.get("conditions") or {}
                if not isinstance(conditions, dict):
                    continue
                lane_bucket = buckets.setdefault(split, {}).setdefault(lane, {})
                for cond, cond_metrics in conditions.items():
                    lane_bucket.setdefault(str(cond), []).append(cond_metrics)

    out: dict[str, dict] = {"train": {}, "test": {}}
    for split, lanes in buckets.items():
        for lane, conditions in lanes.items():
            out[split].setdefault(lane, {"conditions": {}})
            for cond, blocks in conditions.items():
                agg = _aggregate_condition_metrics(blocks)
                if agg is not None:
                    out[split][lane]["conditions"][cond] = agg
    return out


def _expected_cells(frameworks: list[str], anchors: list[str], *, expect_noop_pdf: bool) -> set[tuple[str, str]]:
    expected = {(fw, anchor) for fw in frameworks for anchor in anchors}
    if expect_noop_pdf and "pdf" in anchors:
        expected.add(("noop", "pdf"))
    return expected


def _missing_or_invalid(
    root: Path,
    cells: dict[str, dict[str, dict]],
    expected: set[tuple[str, str]],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for fw, anchor in sorted(expected):
        row = (cells.get(fw) or {}).get(anchor)
        cell_dir = root / "cells" / fw / anchor
        if row is None:
            out.append({"framework": fw, "anchor": anchor, "reason": "missing_cell_summary"})
            continue
        if not (cell_dir / "cell_summary.json").is_file():
            out.append({"framework": fw, "anchor": anchor, "reason": "missing_cell_summary"})
        primary = row.get("primary_lane")
        if row.get("train_delta") is None:
            needed = "confirmation.json" if primary == "lane_c" else "native_train_confirm.json"
            out.append({"framework": fw, "anchor": anchor, "reason": f"missing_or_empty_{needed}"})
        if not (cell_dir / "fairness.json").is_file():
            out.append({"framework": fw, "anchor": anchor, "reason": "missing_fairness_json"})
    return out


def _gate_decision(fw: str, per_anchor: dict[str, dict], *, noop_delta: float | None) -> dict:
    deltas = [
        (skill, row["train_delta"])
        for skill, row in per_anchor.items()
        if row.get("train_delta") is not None
    ]
    degraded_anchors = sorted(s for s, row in per_anchor.items() if row.get("degraded"))
    eligible_deltas = [
        (skill, delta)
        for skill, delta in deltas
        if not (per_anchor.get(skill) or {}).get("degraded")
    ]
    passed_anchors = [s for s, d in eligible_deltas if d >= GATE_DELTA]
    decision = {
        "framework": fw,
        "gate_delta_threshold": GATE_DELTA,
        "gate_min_anchors": GATE_MIN_ANCHORS,
        "anchors_with_delta": {s: d for s, d in deltas},
        "eligible_anchors_with_delta": {s: d for s, d in eligible_deltas},
        "degraded_anchors": degraded_anchors,
        "degraded_reasons": {
            s: (row.get("degraded_reasons") or [])
            for s, row in per_anchor.items()
            if row.get("degraded")
        },
        "passed_anchors": passed_anchors,
        "passes_gate": len(passed_anchors) >= GATE_MIN_ANCHORS,
        "noop_control_delta": noop_delta,
    }
    warnings: list[str] = []
    if fw in LANE_C_FWS and noop_delta is not None and noop_delta >= GATE_DELTA:
        warnings.append("noop control meets gate threshold - raise bar or investigate noise")
    if degraded_anchors:
        warnings.append("degraded cells are excluded from passed_anchors")
    if warnings:
        decision["warnings"] = warnings
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Collate loop-v1 pilot and evaluate gate")
    parser.add_argument(
        "--artifacts-root",
        default=str(HERE / "runs" / "artifacts_loop"),
        help="AFTER_ARTIFACTS_ROOT (default: runs/artifacts_loop/)",
    )
    parser.add_argument(
        "--anchors",
        default="testing,pdf,pptx",
        help="Comma-separated pilot anchors",
    )
    parser.add_argument(
        "--out", default="", help="Write JSON report (default: <root>/gate_report.json)"
    )
    parser.add_argument(
        "--frameworks",
        default=",".join(DEFAULT_FRAMEWORKS),
        help="Comma-separated expected frameworks",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Do not fail if expected cells or primary confirmation files are missing",
    )
    parser.add_argument(
        "--no-noop-control",
        action="store_true",
        help="Do not expect the noop/pdf control cell",
    )
    args = parser.parse_args()

    root = Path(args.artifacts_root).resolve()
    anchors = [a.strip() for a in args.anchors.split(",") if a.strip()]
    frameworks = [f.strip() for f in args.frameworks.split(",") if f.strip()]
    cells = _collect_cells(root, anchors)
    expected = _expected_cells(
        frameworks,
        anchors,
        expect_noop_pdf=not args.no_noop_control,
    )
    invalid = _missing_or_invalid(root, cells, expected)

    noop_pdf = (cells.get("noop") or {}).get("pdf", {}).get("train_delta")
    report: dict[str, Any] = {
        "protocol_version": PROTOCOL,
        "artifacts_root": str(root),
        "anchors": anchors,
        "expected_cells": [{"framework": fw, "anchor": anchor} for fw, anchor in sorted(expected)],
        "missing_or_invalid_cells": invalid,
        "cells": cells,
        "frameworks": {},
        "metric_summary": {},
    }

    for fw in sorted((LANE_C_FWS | LANE_N_FWS) & set(cells.keys())):
        report["frameworks"][fw] = _gate_decision(fw, cells.get(fw, {}), noop_delta=noop_pdf)
        report["metric_summary"][fw] = _aggregate_framework_metrics(cells.get(fw, {}))

    out_path = Path(args.out) if args.out else root / "gate_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({"gate_report": str(out_path), "frameworks": report["frameworks"]}, indent=2))
    if invalid and not args.allow_missing:
        print(
            json.dumps({"error": "missing_or_invalid_cells", "items": invalid}, indent=2),
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
