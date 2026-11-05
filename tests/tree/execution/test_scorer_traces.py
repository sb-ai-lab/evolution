"""The trace a `SubprocessScorer` leaves behind when the caller names it."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from evolution.tree import read_scorer_trace
from evolution.tree.execution import scorer_traces
from evolution.tree.execution.candidate_integrity import artifact_digest
from evolution.tree.execution.interaction import (
    CandidateView,
    EvalCase,
    History,
    ScoringView,
    SemanticIdentity,
)
from evolution.tree.execution.rollout_scoring import (
    ScoringError,
    SubprocessScorer,
    parse_scorer_stdout,
)
from evolution.tree.execution.scorer_traces import ScorerTraceStorageFault
from evolution.tree.storage.journal_errors import JournalFault

FIXTURE = Path(__file__).parents[2] / "fixtures" / "rollout_scorer.py"


@pytest.fixture(autouse=True)
def close_test_owned_scorers(monkeypatch: pytest.MonkeyPatch) -> Any:
    """These library-call tests own their bindings and end each session explicitly."""
    owned: list[SubprocessScorer] = []
    initialize = SubprocessScorer.__post_init__

    def track(scorer: SubprocessScorer) -> None:
        initialize(scorer)
        owned.append(scorer)

    monkeypatch.setattr(SubprocessScorer, "__post_init__", track)
    yield
    for scorer in owned:
        scorer._close_traces()


def _scorer(tmp_path: Path) -> SubprocessScorer:
    return SubprocessScorer(
        argv=(sys.executable, str(FIXTURE)),
        cwd=Path.cwd(),
        timeout_seconds=10,
        trace_root=tmp_path / "run" / "scorer-traces",
        dependency_paths=(FIXTURE,),
    )


def _inputs(tmp_path: Path) -> tuple[EvalCase, ScoringView, CandidateView]:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "skill.py").write_text("VALUE = 1\n", encoding="utf-8")
    candidate = CandidateView(root, artifact_digest(root, ("skill.py",)), ("skill.py",))
    case = EvalCase("c", "g", "s", (), {"x": 1}, {"secret": "PRIVATE-CANARY"}, {"critical": False})
    view = ScoringView(History(()), "completed", SemanticIdentity("c", "d", "s", "exact", "2", "r"))
    return case, view, candidate


@pytest.mark.asyncio
async def test_score_traced_names_the_trace_by_the_callers_id(tmp_path: Path) -> None:
    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)

    result = await scorer.score_traced(
        case, view, candidate, trace_id="abc123", pack_id="evaluation"
    )

    assert result.metrics == {"quality": 1.0}
    record = read_scorer_trace(tmp_path / "run", "abc123", measurement_id="evaluation")
    assert record is not None and record.request is not None and record.stdout is not None
    request = json.loads(record.request)
    assert set(request) == {"scorer", "trace_id", "input", "input_sha256"}
    assert request["trace_id"] == "abc123"
    assert request["scorer"] == scorer.identity.digest
    assert set(request["input"]) == {"case_id", "public", "oracle", "view"}
    stdin_bytes = json.dumps(
        request["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert request["input_sha256"] == hashlib.sha256(stdin_bytes).hexdigest()
    assert parse_scorer_stdout(record.stdout.decode())["public_feedback"] == {
        "summary": "offline fixture passed"
    }


def test_a_pathologically_nested_stdout_is_a_scoring_error_not_a_raise() -> None:
    """A `RecursionError` from the parser is still a document the contract refuses."""

    with pytest.raises(ScoringError):
        parse_scorer_stdout("[" * 100_000 + "]" * 100_000)


@pytest.mark.asyncio
async def test_a_reused_trace_id_is_a_scoring_error_not_an_overwrite(tmp_path: Path) -> None:
    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)
    await scorer.score_traced(case, view, candidate, trace_id="abc123", pack_id="evaluation")
    pack = scorer.trace_root / "evaluation.pack"
    before = pack.read_bytes()

    with pytest.raises(ScoringError):
        await scorer.score_traced(case, view, candidate, trace_id="abc123", pack_id="evaluation")

    assert pack.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("trace_id", ["", "../escape", "a/b", "x" * 129, ".hidden"])
async def test_a_trace_id_outside_the_grammar_is_refused_before_any_write(
    tmp_path: Path, trace_id: str
) -> None:
    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)

    with pytest.raises(ScoringError):
        await scorer.score_traced(case, view, candidate, trace_id=trace_id, pack_id="evaluation")

    assert not (tmp_path / "run").exists()


def _printing_scorer(tmp_path: Path, code: str) -> SubprocessScorer:
    return SubprocessScorer(
        argv=(sys.executable, "-c", code),
        cwd=Path.cwd(),
        timeout_seconds=10,
        trace_root=tmp_path / "run" / "scorer-traces",
    )


def _enospc(*_args: object, **_kwargs: object) -> NoReturn:
    raise OSError(errno.ENOSPC, "No space left on device")


def _eio(*_args: object, **_kwargs: object) -> NoReturn:
    raise OSError(errno.EIO, "Input/output error")


def test_a_chmod_failure_is_kept_when_close_also_fails_and_removes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.execution import scorer_traces

    path = tmp_path / "stderr"
    real_close = scorer_traces._raw_close

    def fail_chmod(_path: Path, _mode: int) -> None:
        _eio()

    def fail_close_after_closing(stream: scorer_traces.TraceStream) -> None:
        real_close(stream)
        _enospc()

    monkeypatch.setattr(scorer_traces.os, "chmod", fail_chmod)
    monkeypatch.setattr(scorer_traces, "_raw_close", fail_close_after_closing)

    with pytest.raises(ScorerTraceStorageFault) as fault:
        scorer_traces.open_trace_stream(path, "scorer_traces/t/stderr")

    assert fault.value.code == "journal_unavailable"
    assert "scorer_traces/t/stderr: EIO (create)" in fault.value.detail
    assert not path.exists()


@pytest.mark.asyncio
async def test_pack_request_is_reader_visible_before_spawn_and_output_before_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.execution import rollout_scoring, scorer_trace_writer

    run = tmp_path / "run"
    scorer = dataclasses.replace(_scorer(tmp_path), trace_root=run / "scorer-traces")
    case, view, candidate = _inputs(tmp_path)
    capture = rollout_scoring.run_capture
    parse = rollout_scoring._parse_json_object
    observed = []
    synced = set()
    original_sync = scorer_trace_writer._fsync

    def sync(fd: int) -> None:
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))
        original_sync(fd)

    monkeypatch.setattr(scorer_trace_writer, "_fsync", sync)

    def capture_after_request(*args: Any, **kwargs: Any) -> Any:
        for parent in (tmp_path, run):
            info = parent.stat()
            assert (info.st_dev, info.st_ino) in synced
        record = read_scorer_trace(run, "ordered", measurement_id="m-r03-champion@e2")
        assert record is not None and record.request is not None and record.stdout is None
        request = json.loads(record.request)
        assert (
            record.request
            == (json.dumps(request, indent=2, ensure_ascii=False, default=str) + "\n").encode()
        )
        assert not list((run / "scorer-traces").glob("scorer_traces"))
        observed.append("request")
        return capture(*args, **kwargs)

    def parse_after_output(raw: str) -> Any:
        record = read_scorer_trace(run, "ordered", measurement_id="m-r03-champion@e2")
        assert record is not None and record.stdout == raw.encode() and record.status == "completed"
        observed.append("output")
        return parse(raw)

    monkeypatch.setattr(rollout_scoring, "run_capture", capture_after_request)
    monkeypatch.setattr(rollout_scoring, "_parse_json_object", parse_after_output)
    await scorer.score_traced(
        case, view, candidate, trace_id="ordered", pack_id="m-r03-champion@e2"
    )
    assert observed == ["request", "output"]
    assert [path.name for path in scorer.trace_root.iterdir()] == ["m-r03-champion@e2.pack"]


@pytest.mark.asyncio
async def test_untraced_calls_share_one_pack_with_fresh_ids_and_null_request_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from evolution.tree.execution import rollout_scoring

    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)
    ids = iter(["first", "second"])
    monkeypatch.setattr(rollout_scoring, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    await scorer.score(case, view, candidate)
    await scorer.score(case, view, candidate)
    (pack,) = scorer.trace_root.iterdir()
    assert pack.name.startswith("untraced-") and pack.suffix == ".pack"
    for trace_id in ("first", "second"):
        record = read_scorer_trace(tmp_path / "run", trace_id)
        assert record is not None and record.request is not None
        assert json.loads(record.request)["trace_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pack_id", ["", "x:y", "../escape", "a/b", "x" * 129, ".hidden", "é"])
async def test_bad_pack_id_is_a_caller_error_before_writing(tmp_path: Path, pack_id: str) -> None:
    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)
    with pytest.raises(ValueError, match="pack id is not a safe file name"):
        await scorer.score_traced(case, view, candidate, trace_id="try", pack_id=pack_id)
    assert not scorer.trace_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "status", "returncode", "error"),
    [
        ('print(\'{"metrics":{"quality":1}}\')', "completed", 0, None),
        ("print('bad json')", "completed", 0, "output_invalid"),
        ("import sys; print('partial'); sys.exit(3)", "failed", 3, "process_failed"),
        ("import time; print('partial', flush=True); time.sleep(10)", "timeout", None, "timeout"),
    ],
)
async def test_every_process_outcome_has_complete_packed_streams_and_audit_offsets(
    tmp_path: Path, script: str, status: str, returncode: int | None, error: str | None
) -> None:
    audit = []
    script = (
        "import sys; sys.stdin.read(); sys.stderr.buffer.write(b'raw\\xff'); sys.stderr.flush(); "
        + script
    )
    scorer = dataclasses.replace(
        _printing_scorer(tmp_path, script), timeout_seconds=0.3, audit=audit.append
    )
    case, view, candidate = _inputs(tmp_path)
    if error is None:
        await scorer.score_traced(case, view, candidate, trace_id="try", pack_id="evaluation")
    else:
        with pytest.raises(ScoringError) as failure:
            await scorer.score_traced(case, view, candidate, trace_id="try", pack_id="evaluation")
        assert failure.value.code == error
    record = read_scorer_trace(tmp_path / "run", "try", measurement_id="evaluation")
    assert record is not None and record.stdout is not None
    assert (
        record.status == status and record.returncode == returncode and record.stderr == b"raw\xff"
    )
    (event,) = audit
    assert event["event"] == "scorer_command" and event["status"] == status
    assert event.get("returncode") == returncode
    assert event["input_bytes"] > 0 and event["duration_seconds"] >= 0
    for stream in ("stdout", "stderr"):
        location = event["trace"][stream]
        data = getattr(record, stream)
        assert location["path"] == "evaluation.pack"
        pack = (scorer.trace_root / location["path"]).read_bytes()
        assert pack[location["offset"] : location["offset"] + location["bytes"]] == data
        assert location["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_start_failure_has_durable_empty_output_and_keeps_external_oserror_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.execution import rollout_scoring

    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)
    monkeypatch.setattr(rollout_scoring, "run_capture", _enospc)
    with pytest.raises(ScoringError) as failure:
        await scorer.score_traced(case, view, candidate, trace_id="try", pack_id="evaluation")
    assert failure.value.code == "process_failed"
    record = read_scorer_trace(tmp_path / "run", "try")
    assert record is not None and record.status == "start_failed" and record.returncode is None
    assert record.stdout == record.stderr == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["request", "output", "stream", "buffers", "root_parent"])
@pytest.mark.parametrize("failure", [_enospc, _eio])
async def test_owned_storage_faults_stay_typed_and_fault_the_whole_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, failure: Callable[..., NoReturn]
) -> None:
    from evolution.tree.execution import rollout_scoring, scorer_trace_writer

    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)
    spawned = []
    capture = rollout_scoring.run_capture

    def capture_once(*args: Any, **kwargs: Any) -> Any:
        spawned.append(True)
        return capture(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rollout_scoring, "run_capture", capture_once)
        if stage in ("request", "output"):
            # The fd write seam keeps the real writer's fault and rollback path.
            real_append = scorer_trace_writer.PackedTraceWriter._append

            def fail_record(self: Any, fd: Any, fields: Any, sources: Any) -> Any:
                if fields["kind"] != stage:
                    return real_append(self, fd, fields, sources)
                with monkeypatch.context() as write_patch:
                    write_patch.setattr(scorer_trace_writer, "_write", failure)
                    return real_append(self, fd, fields, sources)

            patch.setattr(scorer_trace_writer.PackedTraceWriter, "_append", fail_record)
        elif stage == "root_parent":
            original_sync = scorer_trace_writer._fsync

            def sync(fd: int) -> None:
                info = os.fstat(fd)
                parent = scorer.trace_root.parent.stat()
                if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                    failure()
                original_sync(fd)

            patch.setattr(scorer_trace_writer, "_fsync", sync)
        elif stage == "stream":
            patch.setattr(scorer_traces, "_raw_write", failure)
        else:
            patch.setattr(scorer_trace_writer.tempfile, "TemporaryFile", failure)
        with pytest.raises(ScorerTraceStorageFault) as caught:
            await scorer.score_traced(case, view, candidate, trace_id="first", pack_id="evaluation")
    assert isinstance(caught.value, JournalFault)
    assert caught.value.code == (
        "storage_capacity_exhausted" if failure is _enospc else "journal_unavailable"
    )
    assert "scorer-traces/evaluation.pack:" in caught.value.detail
    assert str(tmp_path) not in caught.value.detail
    assert caught.value.detail.endswith("and run the same command again")
    with pytest.raises(ScorerTraceStorageFault) as repeated:
        await scorer.score_traced(case, view, candidate, trace_id="second", pack_id="evaluation")
    assert repeated.value is caught.value
    assert len(spawned) == (1 if stage in ("output", "stream") else 0)
    # Retaining both exceptions cannot keep an explicitly ended owner session alive.
    scorer._close_traces()
    replacement = dataclasses.replace(scorer)
    await replacement.score_traced(case, view, candidate, trace_id="third", pack_id="evaluation")
    record = read_scorer_trace(tmp_path / "run", "third")
    assert record is not None and record.status == "completed"


@pytest.mark.asyncio
async def test_partial_stream_writes_keep_complete_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scorer = _printing_scorer(
        tmp_path,
        "import sys; sys.stdin.read(); print('{\"metrics\":{\"quality\":1}}'); sys.stderr.write('diagnostic')",
    )
    case, view, candidate = _inputs(tmp_path)
    raw_write = scorer_traces._raw_write
    monkeypatch.setattr(
        scorer_traces, "_raw_write", lambda stream, data: raw_write(stream, data[:3])
    )
    await scorer.score_traced(case, view, candidate, trace_id="short", pack_id="evaluation")
    record = read_scorer_trace(tmp_path / "run", "short")
    assert record is not None and record.stdout == b'{"metrics":{"quality":1}}\n'
    assert record.stderr == b"diagnostic"


@pytest.mark.asyncio
async def test_an_external_program_reporting_enospc_stays_a_scorer_failure(tmp_path: Path) -> None:
    scorer = _printing_scorer(
        tmp_path, "import sys; sys.stdin.read(); sys.stderr.write('ENOSPC'); sys.exit(1)"
    )
    case, view, candidate = _inputs(tmp_path)
    with pytest.raises(ScoringError) as failure:
        await scorer.score_traced(case, view, candidate, trace_id="external", pack_id="evaluation")
    assert failure.value.code == "process_failed"
    record = read_scorer_trace(tmp_path / "run", "external")
    assert record is not None and record.stderr == b"ENOSPC" and record.status == "failed"


@pytest.mark.asyncio
async def test_cancellation_waits_for_durable_output_and_releases_anonymous_buffers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from evolution.tree.execution import rollout_scoring

    scorer = _scorer(tmp_path)
    case, view, candidate = _inputs(tmp_path)
    capture = rollout_scoring.run_capture
    started = threading.Event()
    finish = threading.Event()
    streams = []

    def wait_for_cancel(*args: Any, **kwargs: Any) -> Any:
        streams.extend([kwargs["stdout_sink"], kwargs["stderr_sink"]])
        assert all(__import__("os").fstat(stream.fileno()).st_nlink == 0 for stream in streams)
        started.set()
        assert finish.wait(5)
        return capture(*args, **kwargs)

    monkeypatch.setattr(rollout_scoring, "run_capture", wait_for_cancel)
    task = asyncio.create_task(
        scorer.score_traced(case, view, candidate, trace_id="cancelled", pack_id="evaluation")
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = read_scorer_trace(tmp_path / "run", "cancelled")
    assert record is not None and record.status == "completed" and record.stdout is not None
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_output_precedes_command_and_candidate_postchecks(tmp_path: Path) -> None:
    from evolution.tree.execution.interaction import CandidateIntegrityError

    case, view, candidate = _inputs(tmp_path)
    script = tmp_path / "scorer.py"
    script.write_text(
        "import os\nfrom pathlib import Path\nPath(__file__).write_text('changed')\nPath(os.environ['EVO_CANDIDATE_ROOT'], 'skill.py').write_text('changed')\nprint('{\"metrics\":{\"quality\":1}}')\n"
    )
    scorer = SubprocessScorer(
        (sys.executable, str(script)), Path.cwd(), 5, tmp_path / "run" / "scorer-traces"
    )
    with pytest.raises(CandidateIntegrityError):
        await scorer.score_traced(case, view, candidate, trace_id="postcheck", pack_id="evaluation")
    record = read_scorer_trace(tmp_path / "run", "postcheck")
    assert record is not None and record.status == "completed" and record.stdout is not None


@pytest.mark.asyncio
async def test_storage_fault_survives_a_candidate_postcheck_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.execution import scorer_trace_writer

    case, view, candidate = _inputs(tmp_path)
    scorer = _printing_scorer(
        tmp_path,
        "import os\nfrom pathlib import Path\nPath(os.environ['EVO_CANDIDATE_ROOT'], 'skill.py').write_text('changed')\nprint('{\"metrics\":{\"quality\":1}}')",
    )
    verify = CandidateView.verify
    verifications = []

    def tracked_verify(self: CandidateView) -> None:
        verifications.append(True)
        verify(self)

    monkeypatch.setattr(CandidateView, "verify", tracked_verify)
    append = scorer_trace_writer.PackedTraceWriter._append

    def output_fault(self: Any, fd: int, fields: Any, sections: Any) -> Any:
        if fields["kind"] == "output":
            with monkeypatch.context() as patch:
                patch.setattr(scorer_trace_writer, "_write", _enospc)
                return append(self, fd, fields, sections)
        return append(self, fd, fields, sections)

    monkeypatch.setattr(scorer_trace_writer.PackedTraceWriter, "_append", output_fault)
    with pytest.raises(ScorerTraceStorageFault) as fault:
        await scorer.score_traced(case, view, candidate, trace_id="postcheck", pack_id="evaluation")
    assert fault.value.code == "storage_capacity_exhausted"
    assert len(verifications) == 2


@pytest.mark.asyncio
async def test_product_evolve_ends_faulted_sessions_while_the_binding_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree import evolve as evolve_module
    from tests.family14_public import family14_public_run

    bindings = []
    build = evolve_module.build_scorer

    def retain_binding(*args: Any, **kwargs: Any) -> Any:
        scorer = build(*args, **kwargs)
        bindings.append(scorer)
        return scorer

    monkeypatch.setattr(evolve_module, "build_scorer", retain_binding)
    with monkeypatch.context() as patch:
        patch.setattr(scorer_traces, "_raw_write", _enospc)
        run, faulted = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 1})
    assert faulted.terminal == "storage_capacity_exhausted"
    assert bindings and all(not scorer._writers for scorer in bindings)
    _, completed = await family14_public_run(tmp_path, monkeypatch, campaign={"rounds": 1})
    assert completed.terminal == "finished"
    assert all(not scorer._writers for scorer in bindings)
    assert any((run / "scorer-traces").glob("*.pack"))
