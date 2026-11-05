"""Native storage/kernel integration, including real process-death boundaries."""

import asyncio
import hashlib
import multiprocessing
import os
import signal
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evolution import __version__
from evolution.tree.execution.interaction import (
    EnvironmentIdentity,
    Message,
    RolloutBudget,
    RolloutEnvironmentError,
    RolloutInfrastructureError,
    RolloutSample,
    ScorerIdentity,
    SemanticIdentity,
)
from evolution.tree.execution.journal import Journal, inspect_journal
from evolution.tree.execution.journal_identity import IdentityError, RunIdentityV1, corpus_digest
from evolution.tree.execution.journal_recovery import recover_journal, recovery_plan
from evolution.tree.execution.journal_rollout import durable_rollout
from tests.tree.execution.test_journal_identity import cases, identity_payload
from tests.tree.execution.test_rollout_core import _InMemoryInstance


def inputs(count=1):
    corpus = [
        replace(
            cases()[0],
            public={"input": "PUBLIC-CANARY"},
            private={"secret": "PRIVATE-CANARY"},
            oracle={"expected": "ORACLE-CANARY"},
        )
    ]
    data = identity_payload()
    data["corpus_digest"] = corpus_digest(corpus)
    data["budget"]["timeout_seconds"] = 60.0
    data["max_concurrency"] = 4
    data["schedule"] = [
        dict(case_id="c", sample_id=f"s{i}", repeat=i + 1, requested_seed=42) for i in range(count)
    ]
    return RunIdentityV1(data), corpus


def open_journal(path, identity, corpus, *, resume=False):
    return (Journal.resume if resume else Journal.create)(
        path, "integration", identity, corpus, version_resolver=lambda _: __version__
    )


def kwargs(journal, sample_id="s0", *, error=False, sent_hook=None):
    identity = journal.identity
    data = identity.to_payload()
    instance = _InMemoryInstance(instance_id="instance-" + sample_id)
    instance.target_identity = replace(
        instance.target_identity, semantic=SemanticIdentity(**data["target"])
    )
    original_send = instance.send

    async def send(*args):
        snapshot = journal.snapshot()
        assert snapshot.consistency == "durable"
        assert any(
            record.event == "target_send_started"
            and record.payload["instance_id"] == instance.instance_id
            for record in snapshot.records
        )
        result = await original_send(*args)
        if sent_hook:
            sent_hook()
        return result

    instance.send = send

    async def next_message(case, history):
        await asyncio.sleep(0)
        if error:
            raise RuntimeError("EXCEPTION-CANARY")
        return Message("MESSAGE-CANARY") if not history.turns else None

    return dict(
        journal=journal,
        identity=identity,
        corpus=journal.corpus,
        case=journal.corpus[0],
        sample=RolloutSample(sample_id, "candidate", 42),
        instance=instance,
        environment=SimpleNamespace(
            identity=EnvironmentIdentity(**data["environment"]), next=next_message
        ),
        scorer=SimpleNamespace(identity=ScorerIdentity("judge", "scorer")),
        budget=RolloutBudget(**data["budget"]),
        expected_target=instance.target_identity,
    )


def test_real_journal_durable_kernel_and_frozen_inputs(tmp_path):
    identity, corpus = inputs()
    path = tmp_path / "run"
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("unchanged")
    with open_journal(path, identity, corpus) as journal:
        call = kwargs(journal)
        wrong = dict(call, sample=RolloutSample("s0", "other-candidate", 42))
        with pytest.raises(IdentityError):
            asyncio.run(durable_rollout(**wrong))
        assert journal.snapshot().sequence == 1
        trajectory = asyncio.run(durable_rollout(**call))
        assert trajectory.terminal == "completed"
        assert len(call["instance"].sent) == 1
        snapshot = journal.snapshot()
        assert snapshot.consistency == "durable"
        decision = recovery_plan(snapshot.records).executions[0]
        assert decision.outcome == "completed" and decision.evaluation is None
    raw = (path / "events.jsonl").read_bytes()
    for canary in (b"PUBLIC-CANARY", b"PRIVATE-CANARY", b"ORACLE-CANARY", b"MESSAGE-CANARY"):
        assert canary not in raw
    assert sentinel.read_text() == "unchanged"
    with open_journal(path, identity, corpus, resume=True) as journal:
        assert not journal.recovery_required
        assert journal.snapshot().prefix_digest == hashlib.sha256(raw).hexdigest()


def test_real_known_environment_exception_does_not_leak(tmp_path):
    identity, corpus = inputs()
    with open_journal(tmp_path / "run", identity, corpus) as journal:
        with pytest.raises(RolloutEnvironmentError):
            asyncio.run(durable_rollout(**kwargs(journal, error=True)))
        decision = recovery_plan(journal.snapshot().records).executions[0]
        assert decision.outcome == "environment_failed"
        assert decision.evaluation.reason == "environment_unstable"
    assert b"EXCEPTION-CANARY" not in (tmp_path / "run" / "events.jsonl").read_bytes()


@pytest.mark.parametrize("parallelism", [1, 4])
def test_live_concurrency_semantics_with_durable_shared_writer(tmp_path, parallelism):
    identity, corpus = inputs(8)
    with open_journal(tmp_path / "run", identity, corpus) as journal:

        async def execute():
            gate = asyncio.Semaphore(parallelism)

            async def sample(index):
                async with gate:
                    return await durable_rollout(**kwargs(journal, f"s{index}"))

            return await asyncio.gather(*(sample(i) for i in range(8)))

        results = asyncio.run(execute())
        assert [result.terminal for result in results] == ["completed"] * 8
        plan = recovery_plan(journal.snapshot().records)
        assert not plan.actions and not plan.cleanup
        assert [
            (d.sample_id, d.outcome, d.scoring_state, d.evaluation) for d in plan.executions
        ] == [(f"s{i}", "completed", "not_started", None) for i in range(8)]


def _crash_worker(path, marker, boundary):
    identity, corpus = inputs()
    with open_journal(path, identity, corpus) as journal:
        original_append = journal.append

        def append(event, payload, operation_id=None):
            if boundary == "torn_terminal" and event == "target_send_finished":
                os.write(journal._fd, b'{"unfinished-terminal":')
                os.fsync(journal._fd)
                os.kill(os.getpid(), signal.SIGKILL)
            result = original_append(event, payload, operation_id)
            if boundary == "before_effect" and event == "target_send_started":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        journal.append = append

        def side_effect():
            with open(marker, "wb") as file:
                file.write(b"side effect")
                file.flush()
                os.fsync(file.fileno())
            if boundary == "after_effect":
                os.kill(os.getpid(), signal.SIGKILL)

        asyncio.run(durable_rollout(**kwargs(journal, sent_hook=side_effect)))


@pytest.mark.parametrize("boundary", ["before_effect", "after_effect", "torn_terminal"])
def test_sigkill_cuts_require_conservative_recovery_without_replay(tmp_path, boundary):
    path = tmp_path / "run"
    marker = tmp_path / "effect"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_worker, args=(path, marker, boundary)
    )
    process.start()
    process.join(10)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("child failed to reach crash boundary")
    assert process.exitcode == -signal.SIGKILL
    assert marker.exists() == (boundary != "before_effect")
    observed = inspect_journal(path)
    assert observed.records[-1].event == "target_send_started"
    assert observed.journal_integrity == (
        "incomplete_tail" if boundary == "torn_terminal" else "complete"
    )
    identity, corpus = inputs()
    with open_journal(path, identity, corpus, resume=True) as journal:
        assert journal.recovery_required
        plan = recovery_plan(journal.snapshot().records)
        assert [(a.event, a.payload.get("operation")) for a in plan.actions] == [
            ("operation_interrupted", "target_send"),
            ("rollout_interrupted", None),
        ]
        before = marker.read_bytes() if marker.exists() else None
        final = asyncio.run(recover_journal(journal))
        recovered_sequence = journal.snapshot().sequence
        assert asyncio.run(recover_journal(journal)) == final
        assert journal.snapshot().sequence == recovered_sequence
        assert final.executions[0].outcome == "interrupted"
        assert final.executions[0].evaluation.kind == "indeterminate"
        assert not final.actions
        assert (marker.read_bytes() if marker.exists() else None) == before


def test_external_target_exception_keeps_safe_sample_disposition(tmp_path):
    identity, corpus = inputs()
    path = tmp_path / "run"
    with open_journal(path, identity, corpus) as journal:
        call = kwargs(journal)

        async def failed_send(*args):
            raise RuntimeError("TARGET-EXCEPTION-CANARY")

        call["instance"].send = failed_send
        with pytest.raises(RolloutInfrastructureError):
            asyncio.run(durable_rollout(**call))
        decision = recovery_plan(journal.snapshot().records).executions[0]
        assert decision.evaluation.reason == "infrastructure_failed"
    assert b"TARGET-EXCEPTION-CANARY" not in (path / "events.jsonl").read_bytes()


def test_start_fsync_failure_prevents_real_target_send(tmp_path, monkeypatch):
    import errno

    from evolution.tree.storage.journal_errors import JournalFault

    identity, corpus = inputs()
    path = tmp_path / "run"
    with open_journal(path, identity, corpus) as journal:
        call = kwargs(journal)
        original_append = journal.append
        original_sync = os.fsync

        def append(event, payload, operation_id=None):
            if event == "target_send_started":
                monkeypatch.setattr(
                    os,
                    "fsync",
                    lambda fd: (_ for _ in ()).throw(OSError(errno.ENOSPC, "IO-CANARY")),
                )
            return original_append(event, payload, operation_id)

        monkeypatch.setattr(journal, "append", append)
        with pytest.raises(JournalFault, match="storage_capacity_exhausted"):
            asyncio.run(durable_rollout(**call))
        assert not call["instance"].sent
        assert not call["instance"].closed
        monkeypatch.setattr(os, "fsync", original_sync)
    assert b"IO-CANARY" not in (path / "events.jsonl").read_bytes()
    with open_journal(path, identity, corpus, resume=True) as journal:
        plan = recovery_plan(journal.snapshot().records)
        assert any(action.payload.get("operation") == "target_send" for action in plan.actions)
