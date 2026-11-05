"""Tests for skill traces — TraceStore and SkillTrace model."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from evolution.core.models import Skill, SkillFrontmatter, SkillTrace
from evolution.lineage.traces import TraceIndexError, TraceStore, load_trace_evidence, new_trace_id


def _make_skill(tmp_path: Path) -> Skill:
    """Create a minimal skill for testing."""
    skill_dir = tmp_path / ".agents" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n# Test Skill\n\nDo the thing.\n"
    )
    return Skill(
        frontmatter=SkillFrontmatter(name="test-skill", description="A test skill"),
        body="# Test Skill\n\nDo the thing.",
        path=skill_dir,
        skill_md_path=skill_md,
    )


def _make_trace(
    task: str = "my-task",
    version: str = "v1",
    model: str = "claude-sonnet-4",
    passed: int = 5,
    total: int = 5,
    pass_rate: float = 1.0,
    success: bool = True,
) -> SkillTrace:
    return SkillTrace(
        id=f"20260320_120000_{model}_{task}",
        skill_name="test-skill",
        skill_version=version,
        model=model,
        task=task,
        timestamp=datetime(2026, 3, 20, 12, 0, 0),
        passed=passed,
        total=total,
        pass_rate=pass_rate,
        success=success,
        tokens_in=1000,
        tokens_out=500,
        time_ms=5000,
        errors=[],
    )


class TestSkillTraceModel:
    def test_basic_creation(self):
        trace = _make_trace()
        assert trace.skill_name == "test-skill"
        assert trace.skill_version == "v1"
        assert trace.model == "claude-sonnet-4"
        assert trace.pass_rate == 1.0
        assert trace.success is True

    def test_serialization_roundtrip(self):
        trace = _make_trace()
        data = trace.model_dump(mode="json")
        restored = SkillTrace.model_validate(data)
        assert restored.id == trace.id
        assert restored.skill_name == trace.skill_name
        assert restored.pass_rate == trace.pass_rate

    def test_default_errors_empty(self):
        trace = _make_trace()
        assert trace.errors == []

    def test_with_errors(self):
        trace = SkillTrace(
            id="t1",
            skill_name="s",
            skill_version="v1",
            model="m",
            task="t",
            timestamp=datetime.now(),
            errors=["Script exited with code 1"],
        )
        assert len(trace.errors) == 1


class TestTraceStore:
    def test_new_trace_ids_are_unique_uuids(self):
        first = new_trace_id()
        second = new_trace_id()

        assert uuid.UUID(first).version == 4
        assert uuid.UUID(second).version == 4
        assert first != second

    def test_save_creates_files(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)
        trace = _make_trace()

        trace_dir = store.save(trace, "the prompt", "the response", "print('hi')")

        assert trace_dir.exists()
        assert (trace_dir / "prompt.md").read_text() == "the prompt"
        assert (trace_dir / "response.md").read_text() == "the response"
        assert (trace_dir / "solve.py").read_text() == "print('hi')"
        assert (trace_dir / "result.json").exists()

        # Verify result.json is valid
        result_data = json.loads((trace_dir / "result.json").read_text())
        assert result_data["skill_name"] == "test-skill"
        assert result_data["pass_rate"] == 1.0

    def test_save_creates_index(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)
        trace = _make_trace()

        store.save(trace, "p", "r", "c")

        index_path = skill.path / ".traces" / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert len(index) == 1
        assert index[0]["id"] == trace.id

    def test_save_appends_to_index(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(task="task-1")
        t2 = _make_trace(task="task-2")
        t2.id = "20260320_120001_claude-sonnet-4_task-2"

        store.save(t1, "p1", "r1", "c1")
        store.save(t2, "p2", "r2", "c2")

        index = json.loads((skill.path / ".traces" / "index.json").read_text())
        assert len(index) == 2

    def test_list_all(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        store.save(_make_trace(task="t1"), "p", "r", "c")
        t2 = _make_trace(task="t2")
        t2.id = "20260320_120001_claude-sonnet-4_t2"
        store.save(t2, "p", "r", "c")

        traces = store.list()
        assert len(traces) == 2

    def test_list_filter_by_version(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(version="v1")
        t2 = _make_trace(version="v2")
        t2.id = "20260320_120001_claude-sonnet-4_my-task-v2"

        store.save(t1, "p", "r", "c")
        store.save(t2, "p", "r", "c")

        v1_traces = store.list(skill_version="v1")
        assert len(v1_traces) == 1
        assert v1_traces[0]["skill_version"] == "v1"

        v2_traces = store.list(skill_version="v2")
        assert len(v2_traces) == 1

    def test_list_filter_by_model(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(model="claude-sonnet-4")
        t2 = _make_trace(model="llama3")
        t2.id = "20260320_120001_llama3_my-task"

        store.save(t1, "p", "r", "c")
        store.save(t2, "p", "r", "c")

        claude_traces = store.list(model="claude-sonnet-4")
        assert len(claude_traces) == 1

        llama_traces = store.list(model="llama3")
        assert len(llama_traces) == 1

    def test_list_filter_by_task(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(task="offer-letter")
        t1.id = "20260320_120000_claude-sonnet-4_offer-letter"
        t2 = _make_trace(task="court-form")
        t2.id = "20260320_120001_claude-sonnet-4_court-form"

        store.save(t1, "p", "r", "c")
        store.save(t2, "p", "r", "c")

        traces = store.list(task="offer-letter")
        assert len(traces) == 1
        assert traces[0]["task"] == "offer-letter"

    def test_list_combined_filters(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(version="v1", model="claude-sonnet-4", task="t1")
        t1.id = "id1"
        t2 = _make_trace(version="v1", model="llama3", task="t1")
        t2.id = "id2"
        t3 = _make_trace(version="v2", model="claude-sonnet-4", task="t1")
        t3.id = "id3"

        store.save(t1, "p", "r", "c")
        store.save(t2, "p", "r", "c")
        store.save(t3, "p", "r", "c")

        traces = store.list(skill_version="v1", model="claude-sonnet-4")
        assert len(traces) == 1
        assert traces[0]["id"] == "id1"

    def test_list_empty(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        traces = store.list()
        assert traces == []

    def test_fitness_average(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(version="v1", pass_rate=0.8)
        t1.id = "id1"
        t2 = _make_trace(version="v1", pass_rate=1.0)
        t2.id = "id2"

        store.save(t1, "p", "r", "c")
        store.save(t2, "p", "r", "c")

        fitness = store.fitness("v1")
        assert fitness == pytest.approx(0.9)

    def test_fitness_no_traces(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        assert store.fitness("v1") is None

    def test_fitness_single_trace(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        store.save(_make_trace(version="v2", pass_rate=0.75), "p", "r", "c")

        assert store.fitness("v2") == pytest.approx(0.75)

    def test_count(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        assert store.count() == 0

        store.save(_make_trace(), "p", "r", "c")
        assert store.count() == 1

    def test_count_by_version(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        t1 = _make_trace(version="v1")
        t1.id = "id1"
        t2 = _make_trace(version="v2")
        t2.id = "id2"
        t3 = _make_trace(version="v2")
        t3.id = "id3"

        store.save(t1, "p", "r", "c")
        store.save(t2, "p", "r", "c")
        store.save(t3, "p", "r", "c")

        assert store.count(skill_version="v1") == 1
        assert store.count(skill_version="v2") == 2

    def test_traces_dir_property(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        assert store.traces_dir == skill.path / ".traces"

    def test_corrupted_index_raises_integrity_error(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)

        # Write corrupted index
        traces_dir = skill.path / ".traces"
        traces_dir.mkdir(parents=True)
        (traces_dir / "index.json").write_text("not valid json")

        with pytest.raises(TraceIndexError, match="repair"):
            store.list()

    def test_structurally_invalid_index_entry_raises_integrity_error(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)
        store.traces_dir.mkdir(parents=True)
        (store.traces_dir / "index.json").write_text(
            json.dumps([{"id": "partial"}]), encoding="utf-8"
        )

        with pytest.raises(TraceIndexError, match="repair"):
            store.list()

    def test_repair_index_rebuilds_from_valid_trace_directories(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)
        trace = _make_trace()
        store.save(trace, "prompt", "response", "code")
        (store.traces_dir / "index.json").write_text("broken", encoding="utf-8")

        report = store.repair_index()

        assert report.indexed == 1
        assert report.skipped == ()
        assert [entry["id"] for entry in store.list()] == [trace.id]

    def test_save_rejects_duplicate_trace_id_without_overwriting(self, tmp_path: Path):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)
        trace = _make_trace()
        store.save(trace, "first prompt", "response", "code")

        with pytest.raises(FileExistsError, match=trace.id):
            store.save(trace, "replacement prompt", "response", "code")

        assert (store.traces_dir / trace.id / "prompt.md").read_text() == "first prompt"
        assert store.count() == 1

    @pytest.mark.parametrize(
        "trace_id", ["", ".", "..", "../outside", "a/b", "a\\b", "a\nb", " victim "]
    )
    def test_save_rejects_unsafe_trace_id(self, tmp_path: Path, trace_id: str):
        store = TraceStore(_make_skill(tmp_path))
        trace = _make_trace()
        trace.id = trace_id

        with pytest.raises(ValueError, match="Invalid trace id"):
            store.save(trace, "prompt", "response", "code")

    def test_retention_rejects_malicious_persisted_id_without_deleting_outside_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        skill = _make_skill(tmp_path)
        store = TraceStore(skill)
        victim = skill.path.parent / "victim"
        victim.mkdir()
        marker = victim / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        malicious = _make_trace()
        malicious.id = "../../victim"
        store.traces_dir.mkdir(parents=True)
        (store.traces_dir / "index.json").write_text(
            json.dumps([malicious.model_dump(mode="json")]), encoding="utf-8"
        )
        monkeypatch.setenv("EVO_TRACE_RETENTION", "1")
        fresh = _make_trace()
        fresh.id = "fresh"

        with pytest.raises(TraceIndexError, match="invalid trace entry"):
            store.save(fresh, "prompt", "response", "code")

        assert marker.read_text(encoding="utf-8") == "keep"

    def test_index_and_evidence_symlinks_are_never_read(self, tmp_path: Path):
        store = TraceStore(_make_skill(tmp_path))
        store.traces_dir.mkdir(parents=True)
        external_index = tmp_path / "external-index.json"
        external_index.write_text("[]", encoding="utf-8")
        (store.traces_dir / "index.json").symlink_to(external_index)

        with pytest.raises(TraceIndexError, match="symlink"):
            store.list()

        trace_dir = store.traces_dir / "safe"
        trace_dir.mkdir()
        secret = tmp_path / "secret.json"
        secret.write_text('{"secret": true}', encoding="utf-8")
        (trace_dir / "evidence.json").symlink_to(secret)
        with pytest.raises(ValueError, match="symlink"):
            load_trace_evidence(trace_dir)
