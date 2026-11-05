#!/usr/bin/env python3
"""Write markdown metrics for the baselines subset sweep.

This is intentionally a reporting-only utility for artifacts produced by
``experiments/baselines/subset_sweep.py``. It does not drive framework
execution or change the shared iterative sweep implementation.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _flatten_condition(prefix: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_mean_pass_rate": value.get("mean_pass_rate"),
        f"{prefix}_best_of_n": value.get("best_of_n"),
        f"{prefix}_pass_at_n": value.get("pass_at_n"),
        f"{prefix}_pass@5": value.get("pass@5"),
        f"{prefix}_solved_tasks": value.get("solved_tasks"),
        f"{prefix}_n_tasks": value.get("n_tasks"),
    }


def _rows_from_cells(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("train*/cells/*/*/cell_metrics.json")):
        data = _read_json(path)
        if data.get("dry_run"):
            continue
        for split in ("train", "test"):
            block = data.get(split) or {}
            seed = block.get("seed") or {}
            evolved = block.get("evolved") or {}
            rows.append(
                {
                    "schema": data.get("schema"),
                    "framework": data.get("framework"),
                    "skill": data.get("skill"),
                    "split": split,
                    "primary_eval_lane": data.get("primary_eval_lane"),
                    "best_version": data.get("best_version"),
                    **_flatten_condition("seed", seed),
                    **_flatten_condition("evolved", evolved),
                    **(block.get("delta") or {}),
                    "cell_metrics_path": str(path),
                }
            )
    return rows


def _schema_n_train(schema: Any) -> int | None:
    text = str(schema or "")
    if text.startswith("train") and text[5:].isdigit():
        return int(text[5:])
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mean_key(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [parsed for row in rows if (parsed := _float_or_none(row.get(key))) is not None]
    return mean(vals) if vals else None


def _fmt(value: Any) -> str:
    parsed = _float_or_none(value)
    return "" if parsed is None else f"{parsed:.4f}"


def _solved(done: int, total: int) -> str:
    return f"{done}/{total}" if total else str(done)


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["No completed cells yet."]
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    out.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return out


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    seed_solved = sum(int(row.get("seed_solved_tasks") or 0) for row in rows)
    evolved_solved = sum(int(row.get("evolved_solved_tasks") or 0) for row in rows)
    seed_tasks = sum(int(row.get("seed_n_tasks") or 0) for row in rows)
    evolved_tasks = sum(int(row.get("evolved_n_tasks") or 0) for row in rows)
    schemas = sorted({str(row.get("schema")) for row in rows if row.get("schema")})
    return {
        "cells": len({str(row.get("cell_metrics_path")) for row in rows}),
        "schemas": ", ".join(schemas),
        "seed_mean_pass_rate": _mean_key(rows, "seed_mean_pass_rate"),
        "evolved_mean_pass_rate": _mean_key(rows, "evolved_mean_pass_rate"),
        "delta_mean_pass_rate": _mean_key(rows, "delta_mean_pass_rate"),
        "seed_best_of_n": _mean_key(rows, "seed_best_of_n"),
        "evolved_best_of_n": _mean_key(rows, "evolved_best_of_n"),
        "delta_best_of_n": _mean_key(rows, "delta_best_of_n"),
        "seed_pass@5": _mean_key(rows, "seed_pass@5"),
        "evolved_pass@5": _mean_key(rows, "evolved_pass@5"),
        "delta_pass@5": _mean_key(rows, "delta_pass@5"),
        "seed_solved": seed_solved,
        "evolved_solved": evolved_solved,
        "seed_tasks": seed_tasks,
        "evolved_tasks": evolved_tasks,
        "delta_solved": evolved_solved - seed_solved,
    }


def _metric_row(prefix: list[Any], agg: dict[str, Any]) -> list[Any]:
    return [
        *prefix,
        agg["cells"],
        agg["schemas"],
        _fmt(agg["seed_mean_pass_rate"]),
        _fmt(agg["evolved_mean_pass_rate"]),
        _fmt(agg["delta_mean_pass_rate"]),
        _fmt(agg["seed_best_of_n"]),
        _fmt(agg["evolved_best_of_n"]),
        _fmt(agg["delta_best_of_n"]),
        _fmt(agg["seed_pass@5"]),
        _fmt(agg["evolved_pass@5"]),
        _fmt(agg["delta_pass@5"]),
        _solved(int(agg["seed_solved"]), int(agg["seed_tasks"])),
        _solved(int(agg["evolved_solved"]), int(agg["evolved_tasks"])),
        int(agg["delta_solved"]),
    ]


def _summary_rows(rows: list[dict[str, Any]], split: str) -> list[list[Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("split") == split:
            groups[(str(row.get("schema")), str(row.get("framework")))].append(row)
    return [
        _metric_row([schema, framework], _aggregate(items))
        for (schema, framework), items in sorted(groups.items())
    ]


def _skill_rows(rows: list[dict[str, Any]], split: str) -> list[list[Any]]:
    out = []
    for row in sorted(rows, key=lambda x: (str(x.get("schema")), str(x.get("framework")), str(x.get("skill")))):
        if row.get("split") != split:
            continue
        out.append(
            [
                row.get("schema"),
                row.get("framework"),
                row.get("skill"),
                row.get("primary_eval_lane"),
                row.get("best_version"),
                _fmt(row.get("seed_mean_pass_rate")),
                _fmt(row.get("evolved_mean_pass_rate")),
                _fmt(row.get("delta_mean_pass_rate")),
                _fmt(row.get("seed_best_of_n")),
                _fmt(row.get("evolved_best_of_n")),
                _fmt(row.get("delta_best_of_n")),
                _fmt(row.get("seed_pass@5")),
                _fmt(row.get("evolved_pass@5")),
                _fmt(row.get("delta_pass@5")),
                _solved(int(row.get("seed_solved_tasks") or 0), int(row.get("seed_n_tasks") or 0)),
                _solved(int(row.get("evolved_solved_tasks") or 0), int(row.get("evolved_n_tasks") or 0)),
            ]
        )
    return out


def render_report(root: Path) -> str:
    rows = _rows_from_cells(root)
    config = _read_json(root / "sweep_config.json") if (root / "sweep_config.json").is_file() else {}
    progress = _read_json(root / "progress.json") if (root / "progress.json").is_file() else {}
    run_status = _read_json(root / "run_status.json") if (root / "run_status.json").is_file() else {}
    frameworks = list(config.get("frameworks") or sorted({str(row.get("framework")) for row in rows if row.get("framework")}))
    schemas = [f"train{n}" for n in config.get("schemas", [])] or sorted({str(row.get("schema")) for row in rows if row.get("schema")})
    anchors = list(config.get("anchors") or sorted({str(row.get("skill")) for row in rows if row.get("skill")}))
    expected = len(anchors)
    expected_total = len(frameworks) * len(schemas) * expected if expected else ""
    completed_total = len({str(row.get("cell_metrics_path")) for row in rows})
    crash_markers = sum(1 for _ in root.glob("train*/cells/*/*/cell_crash.json"))

    completed: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        completed[(str(row.get("schema")), str(row.get("framework")))].add(str(row.get("cell_metrics_path")))

    lines = [
        "# Partial Metrics Report",
        "",
        f"Run root: `{root}`",
        f"Generated: `{_now()}`",
        "",
        "This is a partial report over completed `cell_metrics.json` files only. Running and pending cells are not included in metric averages yet.",
        "",
        "Definitions: `pass rate` is mean pass rate over all trials, `best of N` is the mean per-task best trial score, `pass@5` is the fraction of tasks solved at least once across 5 trials, and `solved tasks` is the count of tasks solved at least once.",
        "",
        "## Run Status",
        "",
    ]
    if run_status:
        lines.extend(
            _table(
                ["state", "updated_at", "active_processes", "note"],
                [[
                    run_status.get("state", ""),
                    run_status.get("updated_at", ""),
                    run_status.get("active_processes", ""),
                    run_status.get("note", ""),
                ]],
            )
        )
    else:
        lines.append("No explicit run status marker found.")

    lines.extend(["", "## Sweep Coverage", ""])
    lines.extend(
        _table(
            ["expected cells", "completed cells", "remaining cells", "crash markers"],
            [[
                expected_total,
                completed_total,
                expected_total - completed_total if isinstance(expected_total, int) else "",
                crash_markers,
            ]],
        )
    )

    lines.extend(["", "## Sweep Progress Snapshot", ""])
    if progress:
        lines.extend(
            _table(
                ["updated_at", "complete", "pending", "running", "failed", "elapsed_s"],
                [[
                    progress.get("updated_at", ""),
                    progress.get("complete", ""),
                    progress.get("pending", ""),
                    len(progress.get("running") or []),
                    progress.get("failed_count", ""),
                    progress.get("elapsed_s", ""),
                ]],
            )
        )
    else:
        lines.append("No progress file found.")

    lines.extend(["", "## Completed Cells", ""])
    lines.extend(
        _table(
            ["schema", *frameworks],
            [
                [
                    schema,
                    *[
                        f"{len(completed.get((schema, framework), set()))}/{expected}"
                        if expected
                        else len(completed.get((schema, framework), set()))
                        for framework in frameworks
                    ],
                ]
                for schema in schemas
            ],
        )
    )

    metric_headers = [
        "split",
        "framework",
        "cells",
        "schemas",
        "seed pass rate",
        "evolved pass rate",
        "delta",
        "seed best of N",
        "evolved best of N",
        "delta",
        "seed pass@5",
        "evolved pass@5",
        "delta",
        "seed solved",
        "evolved solved",
        "delta solved",
    ]
    lines.extend(["", "## Framework Averages", ""])
    framework_rows = []
    for split in ("train", "test"):
        for framework in sorted({str(row.get("framework")) for row in rows if row.get("split") == split}):
            items = [row for row in rows if row.get("split") == split and row.get("framework") == framework]
            framework_rows.append(_metric_row([split, framework], _aggregate(items)))
    lines.extend(_table(metric_headers, framework_rows))

    lines.extend(["", "## Framework By Train Count", ""])
    grouped: dict[tuple[str, str, int | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("split")), str(row.get("framework")), _schema_n_train(row.get("schema")))].append(row)
    by_train_rows = []
    split_order = {"train": 0, "test": 1}
    for (split, framework, n_train), items in sorted(
        grouped.items(),
        key=lambda item: (split_order.get(item[0][0], 99), item[0][1], item[0][2] or -1),
    ):
        by_train_rows.append(_metric_row([split, framework, n_train or "", f"train{n_train}" if n_train else ""], _aggregate(items)))
    lines.extend(_table(["split", "framework", "n_train", "schema", *metric_headers[2:]], by_train_rows))

    lines.extend(["", "## Per-Framework Train Count Tables", ""])
    for framework in sorted({str(row.get("framework")) for row in rows if row.get("framework")}):
        lines.extend([f"### {framework}", ""])
        fw_rows = []
        train_counts = sorted({
            n for row in rows
            if row.get("framework") == framework and (n := _schema_n_train(row.get("schema"))) is not None
        })
        for n_train in train_counts:
            for split in ("train", "test"):
                items = [
                    row for row in rows
                    if row.get("framework") == framework
                    and row.get("split") == split
                    and _schema_n_train(row.get("schema")) == n_train
                ]
                if items:
                    fw_rows.append(_metric_row([n_train, f"train{n_train}", split], _aggregate(items)))
        lines.extend(_table(["n_train", "schema", "split", *metric_headers[2:]], fw_rows))
        lines.append("")

    summary_headers = ["schema", "framework", *metric_headers[2:]]
    for split, title in (("train", "Train Summary"), ("test", "Test Summary")):
        lines.extend([f"## {title}", ""])
        lines.extend(_table(summary_headers, _summary_rows(rows, split)))
        lines.append("")

    skill_headers = [
        "schema",
        "framework",
        "skill",
        "lane",
        "best version",
        "seed pass rate",
        "evolved pass rate",
        "delta",
        "seed best of N",
        "evolved best of N",
        "delta",
        "seed pass@5",
        "evolved pass@5",
        "delta",
        "seed solved",
        "evolved solved",
    ]
    for split, title in (("train", "Train Per-Skill Rows"), ("test", "Test Per-Skill Rows")):
        lines.extend([f"## {title}", ""])
        lines.extend(_table(skill_headers, _skill_rows(rows, split)))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--mirror-out", type=Path, default=None)
    args = parser.parse_args()
    text = render_report(args.root)
    out = args.out or (args.root / "metrics_partial.md")
    _write_text(out, text)
    if args.mirror_out:
        _write_text(args.mirror_out, text)
    print(out)
    if args.mirror_out:
        print(args.mirror_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
