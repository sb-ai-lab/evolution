from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path


def _load_runner():
    runner_dir = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "5p3-adapter-specialization"
        / "runner"
    )
    sys.path.insert(0, str(runner_dir))
    spec = importlib.util.spec_from_file_location("run_5p3_test", runner_dir / "run_5p3.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cell(task_id: str, condition: str, trial: int) -> dict:
    return {
        "skill": "pdf",
        "task_id": task_id,
        "role": "pm",
        "task_family": "pdf-form-fill",
        "model_id": "openai/openai/gpt-oss-20b",
        "condition": condition,
        "trial": trial,
    }


def test_resume_filter_skips_only_exact_completed_cells(tmp_path: Path) -> None:
    runner = _load_runner()
    csv_path = tmp_path / "table5_flat.csv"
    row = {field: "" for field in runner.CSV_FIELDS}
    row.update(_cell("done-task", "base", 0))
    row["schema_version"] = "5p3_v3"

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=runner.CSV_FIELDS)
        writer.writeheader()
        writer.writerow(row)

    cells = [
        _cell("done-task", "base", 0),
        _cell("done-task", "base", 1),
        _cell("done-task", "m_adapter", 0),
        _cell("other-task", "base", 0),
    ]
    remaining, skipped = runner.filter_completed_cells(cells, csv_path)

    assert skipped == 1
    assert remaining == cells[1:]
