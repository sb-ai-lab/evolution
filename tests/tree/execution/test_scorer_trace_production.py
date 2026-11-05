"""Cross-component compatibility on bytes emitted by the packed producer."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

from evolution import __version__
from evolution.tree import ScorerTraceReader, ScorerTraceUnreadable, read_scorer_trace
from evolution.tree.execution.journal import Journal
from evolution.tree.execution.journal_identity import scorer_binding
from evolution.tree.execution.journal_scoring import durable_score
from evolution.tree.execution.rollout_scoring import SubprocessScorer
from evolution.tree.execution.scorer_trace_writer import PackedTraceWriter
from tests.scorer_trace_packs import write_directory_trace
from tests.tree.execution.test_journal_identity import cases
from tests.tree.execution.test_journal_scoring import setup

pytestmark = pytest.mark.slow


def _scorer(tmp_path: Path) -> SubprocessScorer:
    return SubprocessScorer(
        (sys.executable, "-c", 'print(\'{"metrics":{"accuracy":1}}\')'),
        Path.cwd(),
        5,
        tmp_path / "run/scorer-traces",
    )


async def test_custom_library_journal_resumes_old_directories_with_a_new_pack(tmp_path):
    scorer = _scorer(tmp_path)
    journal, candidate, trajectory = setup(
        tmp_path,
        run_id="c-0123456789abcdef",
        extra_sample=True,
        identity_extra={"scorer": scorer_binding(scorer)},
    )
    run = tmp_path / "run"
    try:
        await durable_score(journal, scorer, cases()[0], trajectory, candidate)
        first = next(r for r in journal.snapshot().records if r.event == "scoring_started")
        old_id = first.payload["score_request_id"]
        old = read_scorer_trace(run, old_id, measurement_id=journal.run_id)
        assert old is not None
        scorer._close_traces()
        # Model an older invocation without changing any committed fixture.
        directory = write_directory_trace(
            run / "scorer-traces",
            old_id,
            request=old.request,
            stdout=old.stdout,
            stderr=old.stderr,
        )
        (run / old.location).unlink()
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        identity = journal.identity
        journal.close()
        journal = Journal.resume(
            run, "c-0123456789abcdef", identity, cases(), version_resolver=lambda _: __version__
        )
        second = dataclasses.replace(trajectory, sample_id="s2")
        key = dict(case_id="c", sample_id="s2", candidate_digest=candidate.digest)
        journal.append("rollout_started", key, "second")
        journal.append("rollout_finished", key | {"outcome": "completed"}, "second")
        scorer = dataclasses.replace(scorer)
        await durable_score(journal, scorer, cases()[0], second, candidate)
        started = [r for r in journal.snapshot().records if r.event == "scoring_started"]
        assert len(started) == 2
        with ScorerTraceReader(run, run_boundary="nofollow") as reader:
            for row, layout in zip(started, ("directory", "pack"), strict=True):
                trace_id = row.payload["score_request_id"]
                explicit = reader.read(trace_id, measurement_id=row.to_payload()["run_id"])
                assert explicit == reader.read(trace_id)
                assert explicit is not None and explicit.layout == layout
                assert json.loads(explicit.stdout)["metrics"] == {"accuracy": 1}
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
        assert {p.name for p in (run / "scorer-traces").iterdir()} == {
            "scorer_traces",
            "c-0123456789abcdef.pack",
        }
        assert [p.name for p in (run / "scorer-traces/scorer_traces").iterdir()] == [old_id]
    finally:
        journal.close()
        scorer._close_traces()


async def test_request_only_crash_recovers_with_a_fresh_id_and_no_fabricated_output(
    tmp_path, monkeypatch
):
    from evolution.tree.evaluation import evaluate
    from evolution.tree.execution import rollout_scoring
    from evolution.tree.execution.journal import inspect_journal
    from tests.campaign_fakes import FakeAdapter
    from tests.test_tree_train_test import evaluation_manifest

    class PowerCut(BaseException):
        pass

    manifest = evaluation_manifest(tmp_path)
    document = json.loads(manifest.read_text())
    document["repeats"] = 1
    manifest.write_text(json.dumps(document))
    run = tmp_path / "run"

    def crash(*args, **kwargs):
        raise PowerCut

    with monkeypatch.context() as patch:
        patch.setattr(rollout_scoring, "run_capture", crash)
        with pytest.raises(PowerCut):
            await evaluate(manifest, run, adapter_factory=lambda *args: FakeAdapter())
    records = inspect_journal(run / "measurements/evaluation").records
    scoring = [r for r in records if r.event.startswith("scoring_")]
    assert [r.event for r in scoring] == ["scoring_started"]
    old_id = scoring[0].payload["score_request_id"]
    old = read_scorer_trace(run, old_id, measurement_id="evaluation")
    assert old is not None and old.request is not None
    assert old.stdout is old.stderr is old.status is old.returncode is None

    result = await evaluate(manifest, run, adapter_factory=lambda *args: FakeAdapter())

    assert result["state"] == "completed"
    records = inspect_journal(run / "measurements/evaluation").records
    started = [r for r in records if r.event == "scoring_started"]
    assert len(started) > 1
    assert all(r.payload["score_request_id"] != old_id for r in started[1:])
    old_terminals = [r.event for r in records if r.payload.get("score_request_id") == old_id]
    assert old_terminals == ["scoring_started", "scoring_interrupted"]
    assert read_scorer_trace(run, old_id) == old
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        for row in started[1:]:
            new = reader.read(row.payload["score_request_id"], measurement_id="evaluation")
            assert new == reader.read(row.payload["score_request_id"])
            assert new is not None and new.status == "completed"
    assert [p.name for p in (run / "scorer-traces").iterdir()] == ["evaluation.pack"]


def _production(run: Path):
    run.mkdir(mode=0o700)
    with PackedTraceWriter(run / "scorer-traces", "m") as writer:
        request = writer.append_request("try", b"{}")
        with writer.streams() as (stdout, stderr):
            stdout.write(b"ok")
            writer.append_output("try", stdout, stderr, status="completed", returncode=0)
    return request


@pytest.mark.parametrize("boundary", ["resolved", "nofollow"])
@pytest.mark.parametrize("explicit", [False, True])
def test_produced_sections_keep_exact_caps_metadata_and_integrity_precedence(
    tmp_path, monkeypatch, boundary, explicit
):
    run = tmp_path / "run"
    request = _production(run)
    kwargs = {"measurement_id": "m"} if explicit else {}
    with ScorerTraceReader(run, run_boundary=boundary) as reader:
        exact = reader.read("try", max_section_bytes=2, **kwargs)
        assert exact is not None and (exact.request, exact.stdout, exact.stderr) == (
            b"{}",
            b"ok",
            b"",
        )
        with pytest.raises(ScorerTraceUnreadable, match="^too_large$"):
            reader.read("try", max_section_bytes=1, **kwargs)
        with monkeypatch.context() as patch:

            def no_payload(*args):
                pytest.fail("metadata-only lookup read a payload")

            patch.setattr(os, "read", no_payload)
            patch.setattr(os, "pread", no_payload)
            metadata = reader.read("try", sections=(), max_section_bytes=0, **kwargs)
            assert (
                metadata is not None
                and metadata.request is metadata.stdout is metadata.stderr is None
            )
            assert dict(metadata.sizes) == {"request": 2, "stdout": 2, "stderr": 0}
        with request.path.open("r+b") as handle:
            handle.seek(request.sections["request"].offset)
            handle.write(b"[]")
        with pytest.raises(ScorerTraceUnreadable, match="^too_large$"):
            reader.read("try", max_section_bytes=1, **kwargs)
        with pytest.raises(ScorerTraceUnreadable, match="^corrupt$"):
            reader.read("try", max_section_bytes=2, **kwargs)
    # A duplicate in an old directory takes precedence over both faults.
    write_directory_trace(run / "scorer-traces", "try", request=b"oversized")
    with pytest.raises(ScorerTraceUnreadable, match="^ambiguous$"):
        read_scorer_trace(run, "try", max_section_bytes=0, **kwargs)


def test_production_pack_strict_reader_keeps_the_admitted_root_after_replacement(tmp_path):
    run = tmp_path / "run"
    _production(run)
    with ScorerTraceReader(run, run_boundary="nofollow") as reader:
        run.rename(tmp_path / "retained")
        replacement = tmp_path / "replacement"
        _production(replacement)
        (replacement / "scorer-traces/m.pack").write_bytes(b"corrupt\n")
        run.symlink_to(replacement, target_is_directory=True)
        record = reader.read("try", measurement_id="m", max_section_bytes=2)
        assert record is not None and record.stdout == b"ok"


@pytest.mark.parametrize("boundary", ["resolved", "nofollow"])
def test_production_append_requires_a_fresh_reader(tmp_path, boundary):
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    with PackedTraceWriter(run / "scorer-traces", "m") as writer:
        writer.append_request("first", b"{}")
        with ScorerTraceReader(run, run_boundary=boundary) as reader:
            before = reader.read("first")
            assert before is not None and before.stdout is None
            assert reader.read("second") is None
            with writer.streams() as (stdout, stderr):
                stdout.write(b"ok")
                writer.append_output("first", stdout, stderr, status="completed", returncode=0)
            writer.append_request("second", b"new")
            assert reader.read("first") == before
            assert reader.read("second") is None
        with ScorerTraceReader(run, run_boundary=boundary) as fresh:
            assert fresh.read("first").stdout == b"ok"
            assert fresh.read("second").request == b"new"
