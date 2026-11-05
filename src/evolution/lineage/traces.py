"""Manage .traces/ directory for a skill — save, list, and query traces."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from evolution.core.models import Skill, SkillTrace

_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


def load_trace_evidence(trace_dir: Path) -> dict | None:
    """Read a trace evidence packet, returning None if it is absent or invalid."""
    path = Path(trace_dir) / "evidence.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _index_lock_for(index_path: Path) -> threading.Lock:
    key = str(index_path.resolve())
    with _INDEX_LOCKS_GUARD:
        lk = _INDEX_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _INDEX_LOCKS[key] = lk
        return lk


class TraceStore:
    """Manages .traces/ directory for a skill."""

    def __init__(self, skill: Skill) -> None:
        self._skill = skill
        self._traces_dir = skill.path / ".traces"
        self._index_path = self._traces_dir / "index.json"

    @property
    def traces_dir(self) -> Path:
        return self._traces_dir

    def save(
        self,
        trace: SkillTrace,
        prompt: str,
        response: str,
        code: str,
        evidence: dict | None = None,
    ) -> Path:
        """Save a trace: writes files + appends to index.

        Creates:
            .traces/<trace_id>/prompt.md
            .traces/<trace_id>/response.md
            .traces/<trace_id>/solve.py
            .traces/<trace_id>/result.json
            .traces/<trace_id>/evidence.json   (only if ``evidence`` given)

        ``result.json`` is unchanged/backward-compatible; the observable
        evidence packet is written to a separate ``evidence.json`` so older
        readers keep working.

        Returns the trace directory path.
        """
        trace_dir = self._traces_dir / trace.id
        trace_dir.mkdir(parents=True, exist_ok=True)

        # Write trace files
        (trace_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        (trace_dir / "response.md").write_text(response, encoding="utf-8")
        (trace_dir / "solve.py").write_text(code, encoding="utf-8")
        (trace_dir / "result.json").write_text(
            trace.model_dump_json(indent=2),
            encoding="utf-8",
        )
        if evidence is not None:
            (trace_dir / "evidence.json").write_text(
                json.dumps(evidence, indent=2, default=str),
                encoding="utf-8",
            )

        # Lock the index read-modify-write: parallel K-attempt produce shares it.
        with _index_lock_for(self._index_path):
            index = self._load_index()
            index.append(trace.model_dump(mode="json"))
            self._save_index(index)

        return trace_dir

    def list(
        self,
        skill_version: str | None = None,
        model: str | None = None,
        task: str | None = None,
    ) -> list[dict]:
        """Query traces with optional filters."""
        index = self._load_index()
        if skill_version:
            index = [t for t in index if t["skill_version"] == skill_version]
        if model:
            index = [t for t in index if t["model"] == model]
        if task:
            index = [t for t in index if t["task"] == task]
        return index

    def fitness(self, skill_version: str) -> float | None:
        """Average pass_rate across all traces for a version."""
        traces = self.list(skill_version=skill_version)
        if not traces:
            return None
        return sum(t["pass_rate"] for t in traces) / len(traces)

    def count(self, skill_version: str | None = None) -> int:
        """Number of traces, optionally filtered by version."""
        return len(self.list(skill_version=skill_version))

    # -- Private helpers --

    def _load_index(self) -> list[dict]:
        if not self._index_path.exists():
            return []
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except (json.JSONDecodeError, OSError):
            return []

    def _save_index(self, index: list[dict]) -> None:
        self._traces_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self._index_path)
