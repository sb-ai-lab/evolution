"""Tests for lineage tracker."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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

    def test_record_entry(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        assert len(tracker.lineage) == 2
        assert tracker.get_entry("v2").origin == "mutation"

    def test_promote(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        VersionStore(tracked_skill).save("v2")
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
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

    def test_promote_restores_version_resource_directories(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        script = tracked_skill.path / "scripts" / "run.py"
        script.write_text("version two", encoding="utf-8")
        VersionStore(tracked_skill).save("v2")
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        script.write_text("working copy", encoding="utf-8")
        (tracked_skill.path / "scripts" / "stale.py").write_text("stale", encoding="utf-8")

        tracker.promote("v2")

        assert script.read_text(encoding="utf-8") == "version two"
        assert not (tracked_skill.path / "scripts" / "stale.py").exists()

    def test_promote_missing_snapshot_preserves_lineage(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )

        with pytest.raises(KeyError, match="Version 'v2'"):
            tracker.promote("v2")

        assert tracker.current_version == "v1"
        assert tracker.get_entry("v1").status == "active"
        assert tracker.get_entry("v2").status == "candidate"

    def test_promote_rolls_back_managed_tree_when_lineage_save_fails(
        self, tracked_skill, monkeypatch: pytest.MonkeyPatch
    ):
        tracker = LineageTracker(tracked_skill)
        original = tracked_skill.skill_md_path.read_text(encoding="utf-8")
        tracked_skill.skill_md_path.write_text(original + "\nversion two\n", encoding="utf-8")
        versions = VersionStore(tracked_skill)
        versions.save("v2")
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        versions.restore("v1")

        def fail_save() -> None:
            raise OSError("disk failure")

        monkeypatch.setattr(tracker, "_save", fail_save)
        with pytest.raises(OSError, match="disk failure"):
            tracker.promote("v2")

        assert tracked_skill.skill_md_path.read_text(encoding="utf-8") == original
        assert LineageTracker(tracked_skill).current_version == "v1"

    def test_next_version_label(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        assert tracker.next_version_label() == "v2"

    def test_persistence(self, tracked_skill):
        tracker = LineageTracker(tracked_skill)
        tracker.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
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

    def test_record_reloads_under_transaction_lock_before_appending(self, tracked_skill):
        first = LineageTracker(tracked_skill)
        stale = LineageTracker(tracked_skill)
        _ = first.lineage
        _ = stale.lineage

        first.record(
            LineageEntry(
                version="v2",
                timestamp=datetime.now(UTC),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )
        stale.record(
            LineageEntry(
                version="v3",
                timestamp=datetime.now(UTC),
                origin="mutation",
                parent="v1",
                status="candidate",
            )
        )

        assert {entry.version for entry in LineageTracker(tracked_skill).lineage} == {
            "v1",
            "v2",
            "v3",
        }


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
        assert "evo skill repair degraded" in msg
        assert "lineage.json" in msg

    def test_corrupt_lineage_json_raises_lineage_error(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="corrupt", description="A skill with corrupt lineage JSON.")
        skill = store.get("corrupt")
        (skill.path / ".evolution" / "lineage.json").write_text("{", encoding="utf-8")

        with pytest.raises(LineageError, match="evo skill repair corrupt"):
            _ = LineageTracker(skill).lineage

    def test_unreadable_lineage_json_raises_lineage_error(self, tmp_path: Path):
        store = SkillStore(project_dir=tmp_path)
        store.create(name="unreadable", description="A skill with unreadable lineage JSON.")
        skill = store.get("unreadable")
        lineage_path = skill.path / ".evolution" / "lineage.json"
        lineage_path.unlink()
        lineage_path.mkdir()

        with pytest.raises(LineageError, match="evo skill repair unreadable"):
            _ = LineageTracker(skill).lineage

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
        assert lineage.get_entry(lineage.current_version).status == "active"
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

        dry = runner.invoke(app, ["skill", "repair", "cli-fix", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert json.loads(lp.read_text()) == {"current_version": "v1"}  # unchanged

        wet = runner.invoke(app, ["skill", "repair", "cli-fix"])
        assert wet.exit_code == 0, wet.output
        repaired = json.loads(lp.read_text())
        assert repaired["skill_id"] == "cli-fix"
        assert {e["version"] for e in repaired["lineage"]} == {"v1", "v2"}
        # repaired file now loads cleanly through the tracker
        assert LineageTracker(store.get("cli-fix")).current_version in {"v1", "v2"}
