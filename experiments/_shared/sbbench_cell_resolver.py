"""Resolve Table 3 cells from ``datasets/sbbench_latest/sb-bench``.

Unlike the historical pilot resolver, this reads the dataset's
``tasks/manifest.json`` directly and only accepts rows with
``included: true``. The emitted ``cells_resolved.json`` is a frozen source
of truth for downstream produce/eval/report artifacts.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_CELLS = ("pm.pdf", "ds.pdf", "infra.terraform")
DEFAULT_ANCHOR_PAIR = ("pm.pdf", "ds.pdf")

REQUIRED_TASK_FILES = ("task.toml", "instruction.md", "tests/test_outputs.py")
FINGERPRINT_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "output"}
FINGERPRINT_SKIP_FILES = {".DS_Store"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_task_dir(task_dir: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in task_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(task_dir)
        if any(part in FINGERPRINT_SKIP_DIRS for part in rel.parts):
            continue
        if path.name in FINGERPRINT_SKIP_FILES:
            continue
        h.update(str(rel).encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(path).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def split_train_streams(task_ids: list[str]) -> tuple[list[str], list[str]]:
    ordered = sorted(task_ids)
    half = (len(ordered) + 1) // 2
    return ordered[:half], ordered[half:]


def _task_dir(corpus_root: Path, role: str, task_id: str) -> Path:
    return corpus_root / "tasks" / role / task_id


def _validate_task_files(task_dir: Path) -> list[str]:
    missing: list[str] = []
    for rel in REQUIRED_TASK_FILES:
        if not (task_dir / rel).is_file():
            missing.append(rel)
    return missing


def _group_included(manifest_rows: list[dict[str, Any]]) -> dict[str, dict[str, list[dict]]]:
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"train": [], "val": [], "test": []}
    )
    for row in manifest_rows:
        if row.get("included") is not True:
            continue
        role = str(row.get("role") or "")
        split = str(row.get("split") or "")
        if split not in {"train", "val", "test"}:
            continue
        for skill in row.get("skills") or []:
            cell_id = f"{role}.{skill}"
            grouped[cell_id][split].append(row)
    return grouped


def resolve_cells(
    *,
    corpus_root: Path,
    selected_cells: tuple[str, ...] = DEFAULT_CELLS,
    anchor_pair: tuple[str, str] = DEFAULT_ANCHOR_PAIR,
) -> dict[str, Any]:
    """Resolve a frozen Table 3 cell manifest from sbbench_latest."""
    corpus_root = Path(corpus_root).resolve()
    manifest_path = corpus_root / "tasks" / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing sb-bench manifest: {manifest_path}")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_rows = json.loads(manifest_text)
    if not isinstance(manifest_rows, list):
        raise ValueError(f"expected manifest list at {manifest_path}")

    grouped = _group_included(manifest_rows)
    missing_cells = [cell for cell in selected_cells if cell not in grouped]
    if missing_cells:
        raise ValueError(f"selected cells absent from included manifest rows: {missing_cells}")

    out_cells: list[dict[str, Any]] = []
    out_tasks: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for cell_id in selected_cells:
        role, skill = cell_id.split(".", 1)
        streams_raw = grouped[cell_id]
        train_ids = [str(r["task_id"]) for r in streams_raw["train"]]
        val_ids = [str(r["task_id"]) for r in streams_raw["val"]]
        test_ids = [str(r["task_id"]) for r in streams_raw["test"]]
        train_a, train_b = split_train_streams(train_ids)
        streams = {
            "train_a": train_a,
            "train_b": train_b,
            "val": sorted(val_ids),
            "test": sorted(test_ids),
        }
        if not train_a or not train_b:
            issues.append(f"{cell_id}: needs at least two train tasks for train_a/train_b")
        if not streams["val"]:
            issues.append(f"{cell_id}: missing included val tasks; cannot support Spec")
        if not streams["test"]:
            issues.append(f"{cell_id}: missing included test tasks; cannot support GenT/GenM")

        is_anchor = cell_id in set(anchor_pair)
        partner = (
            anchor_pair[1] if cell_id == anchor_pair[0] else anchor_pair[0] if is_anchor else None
        )
        out_cells.append(
            {
                "cell_id": cell_id,
                "role": role,
                "skill": skill,
                "is_anchor": is_anchor,
                "anchor_partner": partner,
                "streams": streams,
            }
        )

        stream_to_split_class = {
            "train_a": "local",
            "train_b": "local",
            "val": "local",
            "test": "held-task",
        }
        for stream, ids in streams.items():
            for task_id in ids:
                task_dir = _task_dir(corpus_root, role, task_id)
                missing = _validate_task_files(task_dir)
                if missing:
                    issues.append(f"{cell_id}/{task_id}: missing files {missing}")
                    fingerprint = ""
                else:
                    fingerprint = fingerprint_task_dir(task_dir)
                out_tasks[task_id] = {
                    "id": task_id,
                    "role": role,
                    "cell_id": cell_id,
                    "skill": skill,
                    "stream": stream,
                    "split": "train" if stream in {"train_a", "train_b"} else stream,
                    "split_class": stream_to_split_class[stream],
                    "eval_skill_source": cell_id,
                    "raw_sbbench_path": str(task_dir),
                    "skills": [skill],
                    "task_fingerprint": fingerprint,
                }

    overlap: set[str] = set()
    for cell in out_cells:
        streams = cell["streams"]
        seen_by_stream: dict[str, str] = {}
        for stream, ids in streams.items():
            for task_id in ids:
                if task_id in seen_by_stream:
                    overlap.add(task_id)
                seen_by_stream[task_id] = stream
    if overlap:
        issues.append(f"split overlap detected: {sorted(overlap)}")

    if anchor_pair[0] not in selected_cells or anchor_pair[1] not in selected_cells:
        issues.append(f"anchor_pair {anchor_pair} must be inside selected_cells={selected_cells}")
    a_role, a_skill = anchor_pair[0].split(".", 1)
    b_role, b_skill = anchor_pair[1].split(".", 1)
    if a_role == b_role or a_skill != b_skill:
        issues.append(f"invalid anchor pair {anchor_pair}: need different roles, same skill")

    payload = {
        "schema_version": "table3-cells-v2",
        "corpus_root": str(corpus_root),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_text(manifest_text),
        "selected_cells": list(selected_cells),
        "anchor_pair": list(anchor_pair),
        "non_anchor_cell": next((c for c in selected_cells if c not in set(anchor_pair)), ""),
        "cells": out_cells,
        "tasks": out_tasks,
        "issues": issues,
        "valid": not issues,
    }
    return payload


def write_cells_resolved(cells_resolved: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cells_resolved, indent=2, ensure_ascii=False), encoding="utf-8")


__all__ = [
    "DEFAULT_ANCHOR_PAIR",
    "DEFAULT_CELLS",
    "resolve_cells",
    "sha256_file",
    "sha256_text",
    "split_train_streams",
    "write_cells_resolved",
]
