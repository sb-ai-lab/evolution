"""Tests for lineage tracker."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evolution.cli.main import app
from evolution.core.models import LineageEntry
from evolution.core.store import SkillStore
from evolution.lineage.tracker import LineageError, LineageTracker, rebuild_lineage
from evolution.lineage.version import VersionStore


@pytest.fixture
def tracked_skill(tmp_path: Path):
    store = SkillStore(project_dir=tmp_path)
    store.create(name="tracked", description="A skill to test lineage.")
    return store.get("tracked")


class TestLineageTracker:
    def test_initial_lineage(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        assert tracker.current_version == "v1"
        assert len(tracker.lineage) == 1
        assert tracker.lineage[0].version == "v1"
        assert tracker.lineage[0].status == "active"

    def test_get_active(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        active = tracker.get_active()
        assert active is not None
        assert active.version == "v1"

    def test_record_entry(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(timezone.utc),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        assert len(tracker.lineage) == 2
        assert tracker.get_entry("v2").origin == "mutation"

    def test_promote(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(timezone.utc),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        tracker.promote("v2", fitness=0.85)

        assert tracker.current_version == "v2"
        assert tracker.get_entry("v1").status == "retired"
        assert tracker.get_entry("v2").status == "active"
        assert tracker.get_entry("v2").fitness == pytest.approx(0.85)

    def test_ancestry(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(timezone.utc),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        tracker.record(
            LineageEntry(
                version="v3",
                timestamp=datetime.now(timezone.utc),
                origin="mutation",
                parent="v2",
                status="candidate",
            )
        )
        chain = tracker.ancestry("v3")
        assert [e.version for e in chain] == ["v1", "v2", "v3"]

    def test_next_version_label(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        assert tracker.next_version_label() == "v2"

    def test_next_candidate_label(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        assert tracker.next_candidate_label("v1", 1) == "v1-candidate-1"

    def test_persistence(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(timezone.utc),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )

        # Create a new tracker from the same skill — should see v2
        tracker2 = LineageTracker(tracked_skill)
        # Force reload
        tracker2._lineage = None
        assert len(tracker2.lineage) == 2
        assert tracker2.get_entry("v2").origin == "mutation"

    def test_retire(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.retire("v1")
        assert tracker.get_entry("v1").status == "retired"

    def test_archive(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.archive("v1")
        assert tracker.get_entry("v1").status == "archived"


class TestLineageRepair:
    """Degraded lineage.json must fail loudly + be rebuildable from disk."""

    def _degrade(self, skill, payload: dict) -> None:
        (skill.path / ".evolution" / "lineage.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_degraded_lineage_raises_actionable_error(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="degraded", description="A skill with broken lineage.")
        self._degrade(store.get("degraded"), {"current_version": "v1"})

        tracker = LineageTracker(store.get("degraded"))
        with pytest.raises(LineageError) as ei:
            _ = tracker.lineage

        msg = str(ei.value)
        assert "evo repair degraded" in msg
        assert "lineage.json" in msg

    def test_rebuild_lineage_remaps_missing_current_version(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="rebuilt", description="A skill to rebuild.")
        vs = VersionStore(store.get("rebuilt"))
        vs.save("v2")
        vs.save("v4")
        self._degrade(store.get("rebuilt"), {"current_version": "v9"})

        lineage, notes = rebuild_lineage(store.get("rebuilt"))

        assert sorted(e.version for e in lineage.lineage) == ["v1", "v2", "v4"]
        assert lineage.current_version == "v4"  # v9 absent -> highest on disk
        active = lineage.get_active()
        assert active is not None and active.version == "v4"
        assert lineage.skill_id == "rebuilt"
        assert any("v9" in n for n in notes)  # remap is reported

    def test_repair_cli_dry_run_then_write(self, tmp_path: Path, monkeypatch):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="cli-fix", description="A skill repaired via CLI.")
        VersionStore(store.get("cli-fix")).save("v2")
        lp = store.get("cli-fix").path / ".evolution" / "lineage.json"
        self._degrade(store.get("cli-fix"), {"current_version": "v1"})
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        dry = runner.invoke(app, ["repair", "cli-fix", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert json.loads(lp.read_text()) == {"current_version": "v1"}  # unchanged

        wet = runner.invoke(app, ["repair", "cli-fix"])
        assert wet.exit_code == 0, wet.output
        repaired = json.loads(lp.read_text())
        assert repaired["skill_id"] == "cli-fix"
        assert {e["version"] for e in repaired["lineage"]} == {"v1", "v2"}
        # repaired file now loads cleanly through the tracker
        assert LineageTracker(store.get("cli-fix")).current_version in {"v1", "v2"}
