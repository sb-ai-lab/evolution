"""Candidate 6 guards: failures are loud (recorded), lineage writes are atomic.

Locks in the repo's no-silent-fallback rule for the index builders and the
tmp+os.replace lineage write that mirrors TraceStore.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from evolution.core.models import LineageEntry
from evolution.core.store import SkillStore
from evolution.lineage.tracker import LineageTracker


def test_lineage_save_is_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    store = SkillStore(project_dir=tmp_path)
    store.create(name="atomic", description="atomic write test")
    tracker = LineageTracker(store.get("atomic"))
    tracker.record(
        LineageEntry(
            version="v2",
            timestamp=datetime.now(UTC),
            origin="mutation",
            parent="v1",
            status="candidate",
        )
    )

    evo_dir = store.get("atomic").path / ".evolution"
    # No staging file left behind ...
    assert not list(evo_dir.glob("*.tmp"))
    # ... and lineage.json is complete, parseable JSON (never torn).
    data = json.loads((evo_dir / "lineage.json").read_text(encoding="utf-8"))
    assert any(e["version"] == "v2" for e in data["lineage"])


def test_unparseable_skill_is_logged_not_silent(tmp_path: Path, caplog) -> None:
    broken = tmp_path / ".agents" / "skills" / "broken"
    broken.mkdir(parents=True)
    # Frontmatter missing required name/description -> validation error.
    (broken / "SKILL.md").write_text("---\nfoo: bar\n---\nbody\n", encoding="utf-8")

    store = SkillStore(project_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="evolution.core.store"):
        catalog = store.discover()

    # The bad skill is dropped from the index ...
    assert "broken" not in {e.name for e in catalog}
    # ... but never silently: a warning names it.
    assert any("unparseable skill" in m.lower() for m in caplog.messages)
