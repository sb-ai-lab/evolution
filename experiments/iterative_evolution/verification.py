"""Verification gates for iterative evolution runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evolution.evolution.rewrite_guards import scan_leakage

from .canonical_skill import list_version_snapshots, read_canonical, sha256_text


def scan_body_leakage(body: str, corpus: str = "") -> int:
    """Always run scan_leakage (ignores EVO_DISABLE_LEAK_SCANNER for reporting)."""
    prev = __import__("os").environ.get("EVO_DISABLE_LEAK_SCANNER")
    try:
        __import__("os").environ.pop("EVO_DISABLE_LEAK_SCANNER", None)
        return len(scan_leakage(body, corpus))
    finally:
        if prev is not None:
            __import__("os").environ["EVO_DISABLE_LEAK_SCANNER"] = prev


def path_unity_gate(*, run_dir: Path, skill_id: str, expected_sha: str) -> dict[str, Any]:
    actual = sha256_text(read_canonical(run_dir, skill_id))
    ok = actual == expected_sha
    return {"ok": ok, "expected_sha": expected_sha, "actual_sha": actual}


def versions_gate(run_dir: Path, skill_id: str, min_versions: int = 1) -> dict[str, Any]:
    versions = list_version_snapshots(run_dir, skill_id)
    return {"ok": len(versions) >= min_versions, "versions": versions}


def hermes_events_gate(events_path: Path, skill_id: str) -> dict[str, Any]:
    if not events_path.is_file():
        return {"ok": True, "note": "no events file"}
    bad: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("action") == "create":
            bad.append(ev)
        if ev.get("name") and ev.get("name") != skill_id:
            bad.append(ev)
    return {"ok": not bad, "violations": bad}


def split_gate(*, editor_trace_task_ids: set[str], test_task_ids: set[str]) -> dict[str, Any]:
    overlap = editor_trace_task_ids & test_task_ids
    return {"ok": not overlap, "overlap": sorted(overlap)}


__all__ = [
    "hermes_events_gate",
    "path_unity_gate",
    "scan_body_leakage",
    "split_gate",
    "versions_gate",
]
