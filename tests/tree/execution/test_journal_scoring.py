import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from evolution import __version__
from evolution.tree.execution.candidate_integrity import artifact_digest
from evolution.tree.execution.interaction import (
    CandidateView,
    History,
    InstanceProvenance,
    Provenance,
    ScoreResult,
    ScorerIdentity,
    SemanticIdentity,
    TargetIdentity,
    Trajectory,
)
from evolution.tree.execution.journal import Journal
from evolution.tree.execution.journal_identity import RunIdentityV1
from tests.tree.execution.test_journal_identity import cases, identity_payload


def module():
    assert importlib.util.find_spec("evolution.tree.execution.journal_scoring") is not None, (
        "durable scorer missing"
    )
    return importlib.import_module("evolution.tree.execution.journal_scoring")


def setup(tmp_path, outcome="completed", *, extra_sample=False, identity_extra=None, run_id="run"):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "skill.txt").write_text("candidate")
    candidate = CandidateView(root, artifact_digest(root, ("skill.txt",)), ("skill.txt",))
    p = {**identity_payload(), **(identity_extra or {})}
    p["candidate_digest"] = candidate.digest
    if extra_sample:
        p["schedule"].append(dict(case_id="c", sample_id="s2", repeat=2, requested_seed=42))
    identity = RunIdentityV1(p)
    journal = Journal.create(
        tmp_path / "run", run_id, identity, cases(), version_resolver=lambda _: __version__
    )
    key = dict(case_id="c", sample_id="s1", candidate_digest=candidate.digest)
    if identity_extra and "sample_retries" in identity_extra:
        key["retry_ordinal"] = 0
    journal.append("rollout_started", key, "rollout")
    journal.append("rollout_finished", key | {"outcome": outcome}, "rollout")
    target = TargetIdentity(
        SemanticIdentity(**p["target"]), InstanceProvenance("boot", candidate.digest), frozenset()
    )
    trajectory = Trajectory(
        "c", "s1", candidate.digest, "envdigest", History(), outcome, Provenance(target, 42, 42)
    )
    return journal, candidate, trajectory


@pytest.mark.asyncio
async def test_score_once_and_reuse_only_safe_summary(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path)
    calls = []

    async def score(*args):
        calls.append("score")
        return ScoreResult({"accuracy": 1}, details={"secret": "PRIVATE-CANARY"})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        result = await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
        again = await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
        assert result.metrics == again.metrics == {"accuracy": 1} and calls == ["score"]
        assert "PRIVATE-CANARY" not in (tmp_path / "run/events.jsonl").read_text()
        assert journal.snapshot().records[-1].event == "scoring_finished"
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_scoring_started_declares_input_schema_version_two(tmp_path):
    # Adapted from the brief: `setup` returns `(journal, candidate, trajectory)`,
    # not a list of records, and journal records are `Envelope`s (`.event`,
    # `.payload` attributes), not dicts — so scoring is driven through
    # `durable_score` first, then the written `scoring_started` record is read
    # back off the journal the same way the neighboring test above does.
    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
        started = next(r for r in journal.snapshot().records if r.event == "scoring_started")
        assert started.payload["input_schema_version"] == 2
    finally:
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["time_budget", "target_failed", "cancelled", "interrupted"])
async def test_nonscoreable_never_invokes_scorer(tmp_path, outcome):
    m = module()
    journal, candidate, trajectory = setup(tmp_path, outcome)
    calls = []

    async def score(*args):
        calls.append(True)
        return ScoreResult({"accuracy": 1})

    try:
        with pytest.raises(ValueError):
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
                cases()[0],
                trajectory,
                candidate,
            )
        assert not calls and journal.snapshot().sequence == 3
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_known_scorer_error_vs_task_interruption(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        raise RuntimeError("SECRET-CANARY")

    try:
        with pytest.raises(m.ScoringError) as exc:
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
                cases()[0],
                trajectory,
                candidate,
            )
        assert "SECRET-CANARY" not in str(exc.value)
        assert journal.snapshot().records[-1].event == "scoring_failed"
    finally:
        journal.close()


def test_subprocess_declares_actual_readonly_recovery():
    module()
    from evolution.tree.execution.rollout_scoring import SubprocessScorer

    scorer = object.__new__(SubprocessScorer)
    assert scorer.recovery_mode == "deterministic"
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(scorer, "recovery_mode", "non_replayable")


@pytest.mark.asyncio
async def test_cancelled_scoring_preserves_execution_and_closes_core_interruption(tmp_path):
    import asyncio

    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        raise asyncio.CancelledError()

    try:
        with pytest.raises(asyncio.CancelledError):
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
                cases()[0],
                trajectory,
                candidate,
            )
        from evolution.tree.execution.journal_recovery import recovery_plan

        result = recovery_plan(journal.snapshot().records).executions[0]
        assert result.outcome == "completed" and result.evaluation.owner == "rollout"
        assert journal.snapshot().records[-1].event == "scoring_interrupted"
    finally:
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["start", "terminal"])
async def test_scoring_write_failure_cannot_become_scorer_failure(tmp_path, monkeypatch, stage):
    import errno

    import evolution.tree.execution.journal as storage
    from evolution.tree.storage.journal_errors import JournalFault

    m = module()
    journal, candidate, trajectory = setup(tmp_path)
    calls = []
    real_fsync = storage.os.fsync

    def fail(fd):
        raise OSError(errno.ENOSPC, "SECRET-CANARY")

    async def score(*args):
        calls.append(True)
        if stage == "terminal":
            monkeypatch.setattr(storage.os, "fsync", fail)
        return ScoreResult({"accuracy": 1})

    if stage == "start":
        monkeypatch.setattr(storage.os, "fsync", fail)
    try:
        with pytest.raises(JournalFault) as exc:
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
                cases()[0],
                trajectory,
                candidate,
            )
        assert exc.value.code == "storage_capacity_exhausted"
        assert bool(calls) == (stage == "terminal")
        assert b"scoring_failed" not in (tmp_path / "run/events.jsonl").read_bytes()
        assert "SECRET-CANARY" not in str(exc.value)
    finally:
        monkeypatch.setattr(storage.os, "fsync", real_fsync)
        journal.close()


@pytest.mark.asyncio
async def test_score_uses_maintained_sample_state_without_history_replay(tmp_path, monkeypatch):
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    monkeypatch.setattr(journal, "snapshot", lambda: pytest.fail("score replayed journal"))
    try:
        result = await module().durable_score(
            journal,
            SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
            cases()[0],
            trajectory,
            candidate,
        )
        assert result.metrics == {"accuracy": 1}
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_resumed_scoring_is_healthy_busy_until_other_execution_recovered(tmp_path):
    from evolution.tree.execution.journal_recovery import recover_journal
    from evolution.tree.storage.journal_errors import JournalRecoveryRequired

    journal, candidate, trajectory = setup(tmp_path, extra_sample=True)
    identity = journal.identity
    base = dict(case_id="c", sample_id="s2", candidate_digest=candidate.digest)
    journal.append("rollout_started", base, "other")
    journal.close()
    journal = Journal.resume(
        tmp_path / "run", "run", identity, cases(), version_resolver=lambda _: __version__
    )

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        with pytest.raises(JournalRecoveryRequired, match="journal_busy"):
            await module().durable_score(journal, scorer, cases()[0], trajectory, candidate)
        assert journal.snapshot().sequence == 4
        await recover_journal(journal)
        assert (
            await module().durable_score(journal, scorer, cases()[0], trajectory, candidate)
        ).metrics == {"accuracy": 1}
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_durable_score_prefers_score_traced_with_the_journaled_request_id(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path)
    calls = []

    async def score(*args):
        calls.append(("score", None))
        return ScoreResult({"accuracy": 1})

    async def score_traced(*args, trace_id, pack_id):
        calls.append(("score_traced", trace_id, pack_id))
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(
        identity=ScorerIdentity("judge", "scorer"), score=score, score_traced=score_traced
    )
    try:
        await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
        started = [r for r in journal.snapshot().records if r.event == "scoring_started"]
    finally:
        journal.close()
    assert len(started) == 1
    assert calls == [("score_traced", started[0].payload["score_request_id"], journal.run_id)]


@pytest.mark.asyncio
async def test_durable_score_writes_the_ordinal_and_reads_the_latest_try(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path, identity_extra={"sample_retries": 1})
    calls = []

    async def fail(*args):
        calls.append("fail")
        raise RuntimeError("scorer down")

    async def score(*args):
        calls.append("score")
        return ScoreResult({"accuracy": 1})

    try:
        with pytest.raises(m.ScoringError):
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=fail),
                cases()[0],
                trajectory,
                candidate,
                retry_ordinal=0,
            )
        key = dict(case_id="c", sample_id="s1", candidate_digest=candidate.digest, retry_ordinal=1)
        journal.append("rollout_started", key, "rollout-1")
        journal.append("rollout_finished", key | {"outcome": "completed"}, "rollout-1")
        result = await m.durable_score(
            journal,
            SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
            cases()[0],
            trajectory,
            candidate,
            retry_ordinal=1,
        )
        assert result.metrics == {"accuracy": 1} and calls == ["fail", "score"]
        scoring = [r for r in journal.snapshot().records if r.event.startswith("scoring_")]
        assert [(r.event, r.payload["retry_ordinal"]) for r in scoring] == [
            ("scoring_started", 0),
            ("scoring_failed", 0),
            ("scoring_started", 1),
            ("scoring_finished", 1),
        ]
        tries = journal.sample_tries("c", "s1")
        assert [(e.event, s.event if s else None) for e, s in tries] == [
            ("rollout_finished", "scoring_failed"),
            ("rollout_finished", "scoring_finished"),
        ]
        assert journal.sample_records("c", "s1") == tries[-1]
        assert journal.sample_tries("c", "s2") == ()
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_durable_score_without_an_ordinal_writes_none(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
        scoring = [r for r in journal.snapshot().records if r.event.startswith("scoring_")]
        assert scoring and all("retry_ordinal" not in r.payload for r in scoring)
        assert len(journal.sample_tries("c", "s1")) == 1
    finally:
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_extra", "retry_ordinal"),
    [
        (None, 0),
        ({"sample_retries": 1}, None),
        ({"sample_retries": 1}, 2),
        ({"sample_retries": 1}, -1),
    ],
)
async def test_durable_score_refuses_an_ordinal_the_identity_does_not_admit(
    tmp_path, identity_extra, retry_ordinal
):
    m = module()
    journal, candidate, trajectory = setup(tmp_path, identity_extra=identity_extra)
    calls = []

    async def score(*args):
        calls.append("score")
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        before = journal.snapshot().records
        with pytest.raises(ValueError, match="^retry_ordinal_inconsistent$"):
            await m.durable_score(
                journal, scorer, cases()[0], trajectory, candidate, retry_ordinal=retry_ordinal
            )
        assert journal.snapshot().records == before and calls == []
        assert journal.healthy
    finally:
        journal.close()


# The `scoring_failed` record a failing scorer leaves, as the immutable base
# wrote it (read from an export of the base, never from this tree). The
# `score_request_id` is a fresh uuid per run, so the test masks it.
_BASE_SCORING_FAILED = {
    "case_id": "c",
    "failure": "scorer_error",
    "sample_id": "s1",
    "score_request_id": "<masked>",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "code", "metric"),
    [
        (None, None, None),
        ("timeout", "timeout", None),
        ("process_failed", "process_failed", None),
        ("output_invalid", "output_invalid", None),
        ("metric_type", "metric_type", "quality"),
        ("metric_nonfinite", "metric_nonfinite", "quality"),
    ],
)
async def test_scoring_failed_record_is_unchanged_and_error_carries_the_code(
    tmp_path, raised, code, metric
):
    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        if raised is None:
            raise RuntimeError("SECRET-CANARY")
        raise m.ScoringError(
            "SECRET-CANARY", code=raised, metric="quality" if "metric" in raised else None
        )

    try:
        with pytest.raises(m.ScoringError) as exc:
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
                cases()[0],
                trajectory,
                candidate,
            )
        error = exc.value
        assert (error.code, error.metric) == (code, metric)
        assert str(error) == "scorer failed"
        assert error.__cause__ is None and error.__suppress_context__
        record = journal.snapshot().records[-1]
        assert record.event == "scoring_failed"
        payload = dict(record.payload)
        payload["score_request_id"] = "<masked>"
        payload.pop("candidate_digest")
        assert payload == _BASE_SCORING_FAILED
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_score_result_error_from_an_in_process_scorer_leaves_the_same_record(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score(*args):
        return ScoreResult({"quality": True})

    try:
        with pytest.raises(m.ScoringError) as exc:
            await m.durable_score(
                journal,
                SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score),
                cases()[0],
                trajectory,
                candidate,
            )
        assert (exc.value.code, exc.value.metric) == ("metric_type", "quality")
    finally:
        journal.close()


def _tracing_scorer(calls):
    async def score(*args):
        calls.append("score")
        return ScoreResult({"accuracy": 1})

    async def score_traced(*args, trace_id, pack_id):
        calls.append("score_traced")
        return ScoreResult({"accuracy": 1})

    return SimpleNamespace(
        identity=ScorerIdentity("judge", "scorer"), score=score, score_traced=score_traced
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["a:b", "\u00fc-run", "x" * 129])
async def test_a_run_id_outside_the_pack_id_grammar_is_refused_before_anything_is_journaled(
    tmp_path, run_id
):
    """ADR-0040 D1 (L6): every caller's pack is named by its journal's `run_id`."""

    m = module()
    journal, candidate, trajectory = setup(tmp_path, run_id=run_id)
    calls = []
    try:
        before = journal.snapshot().records
        with pytest.raises(ValueError, match="pack id is not a safe file name"):
            await m.durable_score(
                journal, _tracing_scorer(calls), cases()[0], trajectory, candidate
            )
        assert journal.snapshot().records == before and calls == []
    finally:
        journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_id",
    [
        "m-r01-a02",
        "m-r03-champion@e2",
        "evaluation",
        "5a2a4ac3f07242de92bed5edd781927e",
        "c-" + "a" * 32,
    ],
)
async def test_every_product_and_lab_run_id_is_a_pack_id(tmp_path, run_id):
    m = module()
    journal, candidate, trajectory = setup(tmp_path, run_id=run_id)
    calls = []
    try:
        await m.durable_score(journal, _tracing_scorer(calls), cases()[0], trajectory, candidate)
    finally:
        journal.close()
    assert calls == ["score_traced"]


@pytest.mark.asyncio
async def test_a_scorer_without_score_traced_is_not_checked(tmp_path):
    m = module()
    journal, candidate, trajectory = setup(tmp_path, run_id="a:b")

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        result = await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
    finally:
        journal.close()
    assert result.metrics == {"accuracy": 1}


@pytest.mark.asyncio
async def test_a_trace_storage_fault_journals_no_scorer_failure(tmp_path):
    """ADR-0040 D7: the fault passes `durable_score` untouched; no terminal is written."""

    from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault

    m = module()
    journal, candidate, trajectory = setup(tmp_path)

    async def score_traced(*args, trace_id, pack_id):
        raise ScorerTraceStorageFault(
            "storage_capacity_exhausted", "scorer trace x: ENOSPC (write); free space"
        )

    scorer = SimpleNamespace(
        identity=ScorerIdentity("judge", "scorer"), score=None, score_traced=score_traced
    )
    try:
        with pytest.raises(ScorerTraceStorageFault):
            await m.durable_score(journal, scorer, cases()[0], trajectory, candidate)
        events = [record.event for record in journal.snapshot().records]
    finally:
        journal.close()
    assert events[-1] == "scoring_started" and "scoring_failed" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "invalid", "exit", "timeout", "start"])
async def test_real_scorer_output_is_durable_before_every_journal_terminal(
    tmp_path, monkeypatch, ending
):
    import errno
    import sys
    from pathlib import Path

    from evolution.tree import read_scorer_trace
    from evolution.tree.execution import rollout_scoring
    from evolution.tree.execution.interaction import ScoringError
    from evolution.tree.execution.journal_identity import scorer_binding
    from evolution.tree.execution.rollout_scoring import SubprocessScorer

    scripts = {
        "success": 'print(\'{"metrics":{"accuracy":1}}\')',
        "invalid": "print('bad json')",
        "exit": "import sys; print('partial'); sys.exit(3)",
        "timeout": "import time; print('partial', flush=True); time.sleep(10)",
        "start": "pass",
    }
    scorer = SubprocessScorer(
        (sys.executable, "-c", scripts[ending]),
        Path.cwd(),
        0.3,
        tmp_path / "run" / "scorer-traces",
    )
    journal, candidate, trajectory = setup(
        tmp_path, identity_extra={"scorer": scorer_binding(scorer)}, run_id="c-0123456789abcdef"
    )
    if ending == "start":

        def start_failure(*args, **kwargs):
            raise OSError(errno.ENOSPC, "external failure")

        monkeypatch.setattr(rollout_scoring, "run_capture", start_failure)
    append = Journal.append
    terminals = []

    def append_after_output(self, event, payload, *args, **kwargs):
        if event in ("scoring_finished", "scoring_failed"):
            trace = read_scorer_trace(
                tmp_path / "run", payload["score_request_id"], measurement_id=journal.run_id
            )
            assert trace is not None and trace.stdout is not None and trace.stderr == b""
            assert trace.status == {
                "exit": "failed",
                "timeout": "timeout",
                "start": "start_failed",
            }.get(ending, "completed")
            terminals.append(event)
        return append(self, event, payload, *args, **kwargs)

    monkeypatch.setattr(Journal, "append", append_after_output)
    try:
        if ending == "success":
            await module().durable_score(journal, scorer, cases()[0], trajectory, candidate)
        else:
            with pytest.raises(ScoringError):
                await module().durable_score(journal, scorer, cases()[0], trajectory, candidate)
    finally:
        journal.close()
        scorer._close_traces()
    assert terminals == ["scoring_finished" if ending == "success" else "scoring_failed"]


@pytest.mark.asyncio
async def test_failed_real_output_append_suppresses_the_scoring_terminal(tmp_path, monkeypatch):
    import errno
    import sys
    from pathlib import Path

    from evolution.tree import read_scorer_trace
    from evolution.tree.execution import scorer_trace_writer
    from evolution.tree.execution.journal_identity import scorer_binding
    from evolution.tree.execution.rollout_scoring import SubprocessScorer
    from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault

    scorer = SubprocessScorer(
        (sys.executable, "-c", 'print(\'{"metrics":{"accuracy":1}}\')'),
        Path.cwd(),
        5,
        tmp_path / "run" / "scorer-traces",
    )
    journal, candidate, trajectory = setup(
        tmp_path, identity_extra={"scorer": scorer_binding(scorer)}
    )
    append = scorer_trace_writer.PackedTraceWriter._append

    def failed_append(self, fd, fields, sections):
        if fields["kind"] == "output":

            def enospc(*args):
                raise OSError(errno.ENOSPC, "full")

            with monkeypatch.context() as patch:
                patch.setattr(scorer_trace_writer, "_write", enospc)
                return append(self, fd, fields, sections)
        return append(self, fd, fields, sections)

    monkeypatch.setattr(scorer_trace_writer.PackedTraceWriter, "_append", failed_append)
    try:
        with pytest.raises(ScorerTraceStorageFault):
            await module().durable_score(journal, scorer, cases()[0], trajectory, candidate)
        records = journal.snapshot().records
        assert records[-1].event == "scoring_started"
        trace = read_scorer_trace(
            tmp_path / "run", records[-1].payload["score_request_id"], measurement_id="run"
        )
        assert trace is not None and trace.request is not None and trace.stdout is None
    finally:
        journal.close()
        scorer._close_traces()
