"""Tests for iterative-evolution GUI artifact discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVO_SRC = REPO_ROOT / "src"
if str(EVO_SRC) not in sys.path:
    sys.path.insert(0, str(EVO_SRC))

from evolution.gui.iterative_evolution import (
    build_side_by_side_rows,
    build_unified_diff,
    default_diff_versions,
    discover_iterative_runs,
    discover_skill_artifacts,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(path: Path, payload: object) -> Path:
    return _write(path, json.dumps(payload, indent=2))


def test_discovers_direct_and_sweep_cells(tmp_path: Path):
    runs_root = tmp_path / "runs"
    direct = runs_root / "evolution__de.pipelines__pipelines"
    _write_json(
        direct / "summary.json",
        {
            "framework": "evolution",
            "skill_id": "pipelines",
            "role": "de",
            "best_version": "v0",
            "primary_eval_lane": "canonical_export",
        },
    )
    _write(direct / "skills" / "pipelines" / "versions" / "v0" / "SKILL.md", "seed")

    sweep = runs_root / "sweep-a"
    cell = sweep / "train1" / "cells" / "memp" / "sql"
    _write_json(
        cell / "cell_metrics.json",
        {
            "schema": "train1",
            "framework": "memp",
            "skill": "sql",
            "best_version": "v1",
            "primary_eval_lane": "native_runtime",
            "train": {
                "seed": {"mean_pass_rate": 0.2},
                "evolved": {"mean_pass_rate": 0.6},
                "delta": {"delta_mean_pass_rate": 0.4},
            },
            "test": {
                "seed": {"mean_pass_rate": 0.1},
                "evolved": {"mean_pass_rate": 0.3},
                "delta": {"delta_mean_pass_rate": 0.2},
            },
        },
    )

    runs = discover_iterative_runs(runs_root)

    assert [run.name for run in runs] == ["sweep-a", "evolution__de.pipelines__pipelines"]
    sweep_run = runs[0]
    assert sweep_run.kind == "sweep"
    assert [(c.schema, c.framework, c.skill, c.best_version) for c in sweep_run.cells] == [
        ("train1", "memp", "sql", "v1")
    ]
    direct_run = runs[1]
    assert direct_run.kind == "direct"
    assert [(c.schema, c.framework, c.skill, c.best_version) for c in direct_run.cells] == [
        ("direct", "evolution", "pipelines", "v0")
    ]


def test_discovers_canonical_and_skill_text_native_artifacts(tmp_path: Path):
    cell = tmp_path / "train1" / "cells" / "memp" / "sql"
    _write(cell / "skills" / "sql" / "versions" / "v0" / "SKILL.md", "seed skill\n")
    _write(cell / "skills" / "sql" / "versions" / "v1" / "SKILL.md", "evolved skill\n")
    _write_json(
        cell / "native_state_versions" / "memp" / "v1" / "documents.json",
        [
            {
                "page_content": "learned native procedure\n",
                "metadata": {"workflow": "longer native workflow\n"},
            }
        ],
    )
    _write(cell / "native_state_versions" / "memp" / "v1" / "logs" / "app.log", "noise")
    _write(cell / "native_state_versions" / "memp" / "v1" / "config.json", "{}")
    _write(cell / "native_state_versions" / "memp" / "v1" / "db" / "memento_s.db", "\x00\x01")

    artifacts = discover_skill_artifacts(cell, skill="sql", best_version="v1")

    by_label = {artifact.label: artifact for artifact in artifacts}
    assert by_label["canonical/sql/SKILL.md"].versions["v0"].text == "seed skill\n"
    assert by_label["canonical/sql/SKILL.md"].versions["v1"].text == "evolved skill\n"
    assert "native/memp/documents.json#0" in by_label
    native_doc = by_label["native/memp/documents.json#0"]
    assert native_doc.versions["v1"].text == "learned native procedure\n"
    assert all("app.log" not in artifact.label for artifact in artifacts)
    assert all("config.json" not in artifact.label for artifact in artifacts)
    assert all("memento_s.db" not in artifact.label for artifact in artifacts)


def test_default_versions_prefer_seed_to_best_with_fallbacks(tmp_path: Path):
    cell = tmp_path / "cell"
    _write(cell / "skills" / "api" / "versions" / "v0" / "SKILL.md", "seed\n")
    _write(cell / "skills" / "api" / "versions" / "v2" / "SKILL.md", "best\n")

    artifact = discover_skill_artifacts(cell, skill="api", best_version="v2")[0]

    assert default_diff_versions(artifact, "v2") == ("v0", "v2")
    assert default_diff_versions(artifact, "v9") == ("v0", "v2")


def test_build_unified_diff_labels_and_contents():
    diff = build_unified_diff(
        old_text="line one\nold\n",
        new_text="line one\nnew\n",
        old_label="v0",
        new_label="v1",
    )

    assert "--- v0" in diff
    assert "+++ v1" in diff
    assert "-old" in diff
    assert "+new" in diff


def test_side_by_side_rows_include_nearby_context():
    rows = build_side_by_side_rows(
        "before\nold\nsame\nafter\n",
        "before\nnew\nsame\nafter\n",
        context_lines=1,
    )

    assert [(row["kind"], row["old_text"], row["new_text"]) for row in rows] == [
        ("context", "before", "before"),
        ("replace", "old", "new"),
        ("context", "same", "same"),
    ]
