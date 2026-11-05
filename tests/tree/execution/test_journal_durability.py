import errno
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from evolution import __version__
from evolution.tree.execution.journal import (
    MAX_JOURNAL_BYTES,
    MAX_RECORD_BYTES,
    Journal,
    inspect_journal,
)
from evolution.tree.execution.journal_identity import IdentityError, RunIdentityV1
from evolution.tree.storage.journal_errors import (
    JournalAdmissionRefused,
    JournalFault,
    JournalRecoveryRequired,
)
from tests.tree.execution.test_journal_identity import cases, payload
from tests.tree.execution.test_journal_schema import IMPORT_SOURCE


def imported_payload(ident, entry, **overrides):
    """A valid `sample_imported` payload for one row of `ident`'s schedule."""
    identity = ident.to_payload()
    row = dict(
        case_id=entry["case_id"],
        sample_id=entry["sample_id"],
        candidate_digest=identity["candidate_digest"],
        outcome="completed",
        metrics={"score": 1.0},
        critical_pass=None,
        effective_seed=entry["requested_seed"],
        dependency_digest=identity["target"]["dependency_digest"],
        source=dict(IMPORT_SOURCE),
    )
    row.update(overrides)
    return row


def create(path, **kwargs):
    return Journal.create(
        path,
        "run",
        RunIdentityV1(payload()),
        cases(),
        version_resolver=lambda _: __version__,
        **kwargs,
    )


def resume(path, identity=None, **kwargs):
    return Journal.resume(
        path,
        "run",
        identity or RunIdentityV1(payload()),
        cases(),
        version_resolver=lambda _: __version__,
        **kwargs,
    )


def start(journal, op="rollout"):
    return journal.append(
        "rollout_started", dict(case_id="c", sample_id="s1", candidate_digest="candidate"), op
    )


def finish(journal):
    return journal.append(
        "rollout_finished",
        dict(case_id="c", sample_id="s1", candidate_digest="candidate", outcome="completed"),
        "rollout",
    )


def unchanged_metadata(path):
    stat = path.stat()
    # A read may update atime on a `relatime` mount (#322); a mutation changes these.
    return (
        stat.st_mode,
        stat.st_ino,
        stat.st_nlink,
        stat.st_uid,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def test_create_durable_and_nonmutating_inspection(tmp_path, monkeypatch):
    path = tmp_path / "run"
    with create(path) as journal:
        assert start(journal) == 2
        assert journal.snapshot().consistency == "durable"
        assert path.stat().st_mode & 0o777 == 0o700
        assert (path / "events.jsonl").stat().st_mode & 0o777 == 0o600
        monkeypatch.setattr(os, "chmod", lambda *a, **kw: pytest.fail("reader chmod"))
        monkeypatch.setattr(os, "fchmod", lambda *a, **kw: pytest.fail("reader fchmod"))
        before = unchanged_metadata(path / "events.jsonl"), (path / "events.jsonl").read_bytes()
        snapshot = inspect_journal(path)
        assert snapshot.consistency == "observed"
        assert snapshot.sequence == 2 and snapshot.complete
        assert before == (
            unchanged_metadata(path / "events.jsonl"),
            (path / "events.jsonl").read_bytes(),
        )
    with resume(path) as journal:
        assert finish(journal) == 3


def test_owner_exclusion_and_threads(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        with pytest.raises(JournalFault, match="journal_busy"):
            resume(path)

        def work(i):
            p = dict(
                instance_id=f"i{i}",
                candidate_digest="candidate",
                strategy="filesystem_overlay",
                retry_ordinal=0,
            )
            journal.append("target_materialize_started", p, f"m{i}")
            journal.append("target_materialize_finished", dict(p, latency_ms=0), f"m{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(20)))
        assert journal.snapshot().sequence == 41


def test_identity_before_tail_repair_and_recovery(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        start(journal)
    file = path / "events.jsonl"
    good = file.read_bytes()
    file.write_bytes(good + b'{"torn":"secret"')
    changed = payload()
    changed["max_concurrency"] = 2
    with pytest.raises(IdentityError):
        resume(path, RunIdentityV1(changed))
    assert file.read_bytes() == good + b'{"torn":"secret"'
    assert inspect_journal(path).journal_integrity == "incomplete_tail"
    with resume(path) as journal:
        assert journal.snapshot().sequence == 2
        assert file.read_bytes() == good


def test_corruption_is_partial_never_repaired(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        start(journal)
    file = path / "events.jsonl"
    original = file.read_bytes() + b'{"private":"secret"}\n'
    file.write_bytes(original)
    snapshot = inspect_journal(path)
    assert snapshot.sequence == 2 and not snapshot.complete
    assert snapshot.journal_integrity == "corrupt_suffix"
    assert "secret" not in repr(snapshot)
    with pytest.raises(JournalFault):
        resume(path)
    assert file.read_bytes() == original


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
def test_fsync_failure_invalidates_and_reopens(tmp_path, monkeypatch, code):
    path = tmp_path / "run"
    journal = create(path)
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(code, "SECRET")))
    with pytest.raises(JournalFault) as caught:
        start(journal)
    assert caught.value.code == (
        "journal_unavailable" if code == errno.EIO else "storage_capacity_exhausted"
    )
    assert "SECRET" not in str(caught.value)
    with pytest.raises(JournalFault):
        journal.snapshot()
    monkeypatch.setattr(os, "fsync", real)
    journal.close()
    with resume(path) as recovered:
        assert recovered.snapshot().sequence == 2


def test_containment_and_incomplete_creation(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(JournalFault):
        create(link / "run")
    path = tmp_path / "run"
    with create(path):
        pass
    file = path / "events.jsonl"
    elsewhere = tmp_path / "other"
    elsewhere.write_bytes(file.read_bytes())
    file.unlink()
    file.symlink_to(elsewhere)
    with pytest.raises(JournalFault):
        inspect_journal(path)
    file.unlink()
    file.write_bytes(b"{")
    with pytest.raises(JournalFault):
        resume(path)
    assert file.read_bytes() == b"{"


def test_capacity_stops_dispatch_and_preserves_terminal_credit(tmp_path):
    assert MAX_RECORD_BYTES == 1024 * 1024
    assert MAX_JOURNAL_BYTES == 256 * 1024 * 1024
    path = tmp_path / "run"
    with create(path, max_record_bytes=2048, max_journal_bytes=7000) as journal:
        start(journal)
        p = dict(
            instance_id="instance",
            candidate_digest="candidate",
            strategy="filesystem_overlay",
            retry_ordinal=0,
        )
        with pytest.raises(JournalAdmissionRefused):
            journal.append("target_materialize_started", p, "mat")
        assert journal.snapshot().records[-1].event == "journal_dispatch_stopped"
        finish(journal)
        assert journal.snapshot().records[-1].event == "rollout_finished"
    with resume(path, max_record_bytes=2048, max_journal_bytes=7000) as journal:
        with pytest.raises(JournalAdmissionRefused):
            journal.append("target_materialize_started", p, "mat")


def test_reader_cap_equal_and_hardlinks(tmp_path):
    path = tmp_path / "run"
    with create(path):
        pass
    size = (path / "events.jsonl").stat().st_size
    assert inspect_journal(path, max_journal_bytes=size).sequence == 1
    os.link(path / "events.jsonl", tmp_path / "hard")
    with pytest.raises(JournalFault):
        inspect_journal(path)


def test_resume_requires_recovery_before_new_dispatch(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        start(journal)
    with resume(path) as journal:
        assert journal.recovery_required
        assert journal.run_id == "run" and journal.corpus == tuple(cases())
        with pytest.raises(JournalRecoveryRequired, match="journal_busy"):
            journal.finish_recovery()
        journal.append(
            "rollout_interrupted",
            dict(case_id="c", sample_id="s1", candidate_digest="candidate", outcome="interrupted"),
            "rollout",
        )
        journal.finish_recovery()
        assert not journal.recovery_required


def test_process_bound_and_other_process_owner(tmp_path):
    import multiprocessing

    path = tmp_path / "run"
    context = multiprocessing.get_context("fork")
    with create(path) as journal:
        receive, send = context.Pipe(False)

        def child():
            results = []
            for action in (lambda: start(journal), lambda: resume(path)):
                try:
                    action()
                except JournalFault as error:
                    results.append(error.code)
            journal.close()
            send.send(results)

        process = context.Process(target=child)
        process.start()
        assert receive.poll(5)
        assert receive.recv() == ["journal_unavailable", "journal_busy"]
        process.join(5)
        assert process.exitcode == 0
        start(journal)


def test_reserved_release_fits_after_dispatch_stop(tmp_path):
    path = tmp_path / "run"
    with create(path, max_record_bytes=2048, max_journal_bytes=11500) as journal:
        p = dict(
            instance_id="instance",
            candidate_digest="candidate",
            strategy="filesystem_overlay",
            retry_ordinal=0,
        )
        journal.append("target_materialize_started", p, "mat")
        journal.append("target_materialize_finished", dict(p, latency_ms=0), "mat")
        with pytest.raises(JournalAdmissionRefused):
            journal.append("target_materialize_started", dict(p, instance_id="another"), "mat2")
        journal.append("target_release_started", p, "release")
        journal.append(
            "target_release_failed", dict(p, latency_ms=0, failure="target_error"), "release"
        )
        with pytest.raises(JournalAdmissionRefused):
            journal.append("target_release_started", p, "retry")


def test_unstable_reader_two_attempts(tmp_path, monkeypatch):
    import evolution.tree.execution.journal as module

    path = tmp_path / "run"
    with create(path):
        pass
    original = module._read
    count = 0

    def unstable(fd, length):
        nonlocal count
        count += 1
        raw = original(fd, length)
        return raw if count % 2 else b"changed"

    monkeypatch.setattr(module, "_read", unstable)
    with pytest.raises(JournalFault, match="journal_busy"):
        inspect_journal(path)
    assert count == 4


def test_partial_write_and_torn_terminal_never_fabricate_completion(tmp_path, monkeypatch):
    path = tmp_path / "run"
    journal = create(path)
    start(journal)
    original = os.write
    calls = 0

    def torn(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(fd, data[:20])
        raise OSError(errno.ENOSPC, "secret")

    monkeypatch.setattr(os, "write", torn)
    with pytest.raises(JournalFault, match="storage_capacity_exhausted"):
        finish(journal)
    journal.close()
    monkeypatch.setattr(os, "write", original)
    assert inspect_journal(path).journal_integrity == "incomplete_tail"
    with resume(path) as recovered:
        assert recovered.snapshot().sequence == 2
        assert recovered.snapshot().records[-1].event == "rollout_started"


def test_validation_fault_invalidates_without_append(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        before = (path / "events.jsonl").read_bytes()
        with pytest.raises(JournalFault):
            journal.append("unknown_event", {"secret": "private"}, "operation")
        assert (path / "events.jsonl").read_bytes() == before
        with pytest.raises(JournalFault):
            start(journal)


def test_failed_creation_kept_and_requires_fresh_directory(tmp_path, monkeypatch):
    path = tmp_path / "run"
    path.mkdir()
    original = os.write

    def failure(fd, data):
        original(fd, data[:10])
        raise OSError(errno.EDQUOT, "private")

    monkeypatch.setattr(os, "write", failure)
    with pytest.raises(JournalFault, match="storage_capacity_exhausted"):
        create(path)
    monkeypatch.setattr(os, "write", original)
    original_bytes = (path / "events.jsonl").read_bytes()
    with pytest.raises(JournalFault):
        resume(path)
    with pytest.raises(JournalFault):
        create(path)
    assert (path / "events.jsonl").read_bytes() == original_bytes
    with create(tmp_path / "fresh"):
        pass


def test_owner_refuses_changed_physical_length(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        with (path / "events.jsonl").open("ab") as file:
            file.write(b"unknown")
        with pytest.raises(JournalFault):
            start(journal)


@pytest.mark.asyncio
async def test_async_tasks_share_owner_without_thread_local_lease(tmp_path):
    import asyncio

    with create(tmp_path / "run") as journal:

        async def operation(index):
            p = dict(
                instance_id=f"instance{index}",
                candidate_digest="candidate",
                strategy="filesystem_overlay",
                retry_ordinal=0,
            )
            journal.append("target_materialize_started", p, f"m{index}")
            await asyncio.sleep(0)
            journal.append("target_materialize_finished", dict(p, latency_ms=0), f"m{index}")

        await asyncio.gather(*(operation(i) for i in range(8)))
        assert journal.snapshot().sequence == 17


def test_inspector_accepts_stable_prefix_while_writer_grows(tmp_path, monkeypatch):
    import evolution.tree.execution.journal as module

    path = tmp_path / "run"
    with create(path) as journal:
        original = module._read
        calls = 0

        def growing(fd, length):
            nonlocal calls
            calls += 1
            data = original(fd, length)
            if calls == 1:
                start(journal)
            return data

        monkeypatch.setattr(module, "_read", growing)
        snapshot = inspect_journal(path)
        assert snapshot.sequence == 1 and snapshot.complete
        assert journal.snapshot().sequence == 2
        assert calls == 2


def test_failed_tail_repair_sync_refuses_authority(tmp_path, monkeypatch):
    path = tmp_path / "run"
    with create(path) as journal:
        start(journal)
    with (path / "events.jsonl").open("ab") as file:
        file.write(b'{"torn":')
    real = os.fsync
    monkeypatch.setattr(
        os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "private"))
    )
    with pytest.raises(JournalFault):
        resume(path)
    monkeypatch.setattr(os, "fsync", real)
    with resume(path) as journal:
        assert journal.recovery_required
        assert journal.snapshot().sequence == 2


def test_retry_cleanup_after_resume_with_fresh_capacity(tmp_path):
    path = tmp_path / "run"
    p = dict(
        instance_id="instance",
        candidate_digest="candidate",
        strategy="filesystem_overlay",
        retry_ordinal=0,
    )
    with create(path) as journal:
        journal.append("target_materialize_started", p, "mat")
        journal.append("target_materialize_finished", dict(p, latency_ms=0), "mat")
        journal.append("target_release_started", p, "release")
        journal.append(
            "target_release_failed", dict(p, latency_ms=0, failure="target_error"), "release"
        )
    with resume(path) as journal:
        journal.append("target_release_started", p, "retry")
        journal.append("target_release_finished", dict(p, latency_ms=0), "retry")
        journal.finish_recovery()


def test_binding_is_readonly_and_empty_inspection_incomplete(tmp_path):
    path = tmp_path / "run"
    with create(path) as journal:
        for name, value in [
            ("identity", RunIdentityV1(payload())),
            ("corpus", ()),
            ("recovery_required", False),
        ]:
            with pytest.raises(AttributeError):
                setattr(journal, name, value)
    (path / "events.jsonl").write_bytes(b"")
    snapshot = inspect_journal(path)
    assert not snapshot.complete
    assert snapshot.sequence == 0


def test_corrupt_suffix_carries_safe_length_digest(tmp_path):
    import hashlib

    path = tmp_path / "run"
    with create(path):
        pass
    suffix = b"broken private bytes\nother bytes\n"
    with (path / "events.jsonl").open("ab") as file:
        file.write(suffix)
    diagnostic = inspect_journal(path).diagnostics[0]
    assert diagnostic["length"] == len(suffix)
    assert diagnostic["digest"] == hashlib.sha256(suffix).hexdigest()


def test_fork_while_other_thread_holds_mutex_refuses_without_deadlock(tmp_path):
    import multiprocessing
    import signal
    import threading

    path = tmp_path / "run"
    context = multiprocessing.get_context("fork")
    with create(path) as journal:
        held = threading.Event()
        release = threading.Event()

        def hold_lock():
            with journal._mutex:
                held.set()
                release.wait(10)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        assert held.wait(5)
        receive, send = context.Pipe(False)

        def child():
            signal.alarm(3)
            results = []
            for action in (journal.snapshot, lambda: start(journal), journal.finish_recovery):
                try:
                    action()
                except JournalFault as error:
                    results.append(error.code)
            journal.close()
            send.send(results)

        process = context.Process(target=child)
        try:
            process.start()
            process.join(5)
            assert process.exitcode == 0
            assert receive.poll(1)
            assert receive.recv() == ["journal_unavailable"] * 3
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            release.set()
            thread.join(5)
            receive.close()
            send.close()
        with pytest.raises(JournalFault, match="journal_busy"):
            resume(path)
        start(journal)


def test_recovery_precondition_is_healthy_busy_and_can_be_resolved(tmp_path):
    from evolution.tree.storage import journal_errors as errors

    path = tmp_path / "run"
    with create(path) as journal:
        start(journal)
    with resume(path) as journal:
        for action in (lambda: start(journal, "other"), journal.finish_recovery):
            with pytest.raises(errors.JournalBoundaryError) as caught:
                action()
            assert type(caught.value).__name__ == "JournalRecoveryRequired"
            assert caught.value.code == "journal_busy"
            assert not isinstance(caught.value, (JournalFault, JournalAdmissionRefused))
            assert journal.snapshot().sequence == 2
        finish(journal)
        journal.finish_recovery()
        assert not journal.recovery_required


def test_failed_fsync_does_not_publish_prepared_lifecycle(tmp_path, monkeypatch):
    with create(tmp_path / "run") as journal:
        before = journal._lifecycle
        monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
        with pytest.raises(JournalFault):
            start(journal)
        assert journal._lifecycle is before
        assert before.sequence == 1 and not before.executions and not before.pending


def test_create_with_imports_is_born_whole(tmp_path):
    ident = RunIdentityV1(payload())
    entry = ident.to_payload()["schedule"][0]
    row = imported_payload(ident, entry)
    journal = Journal.create(tmp_path / "m", "run-1", ident, cases(), imported=[row])
    journal.close()
    snapshot = inspect_journal(tmp_path / "m")
    assert [r.event for r in snapshot.records] == ["run_created", "sample_imported"]
    assert not (tmp_path / "m" / "events.jsonl.partial").exists()
    reopened = Journal.resume(tmp_path / "m", "run-1", ident, cases())
    assert reopened.imported_record(entry["case_id"], entry["sample_id"]).payload == row
    reopened.close()


def test_a_crash_before_the_rename_leaves_an_unborn_directory(tmp_path, monkeypatch):
    ident = RunIdentityV1(payload())
    entry = ident.to_payload()["schedule"][0]

    def boom(*args, **kwargs):
        raise OSError("crash before rename")

    monkeypatch.setattr("evolution.tree.storage.append_log.os.replace", boom)
    with pytest.raises(Exception):
        Journal.create(
            tmp_path / "m", "run-1", ident, cases(), imported=[imported_payload(ident, entry)]
        )
    assert not (tmp_path / "m" / "events.jsonl").exists()


def test_sample_records_are_detached_and_do_not_scan_history(tmp_path, monkeypatch):
    with create(tmp_path / "run") as journal:
        start(journal)
        finish(journal)
        monkeypatch.setattr(journal, "snapshot", lambda: pytest.fail("full snapshot"))
        execution, scoring = journal.sample_records("c", "s1")
        assert scoring is None and execution.payload["outcome"] == "completed"
        execution.payload["outcome"] = "cancelled"
        assert journal.sample_records("c", "s1")[0].payload["outcome"] == "completed"


# -- components_declared ------------------------------------------------

DECLARED_MAP = {"catalog": {"sha256": "a" * 64, "sources": ["skills/a/SKILL.md"]}}


def create_marked(path, **kwargs):
    return Journal.create(
        path,
        "run",
        RunIdentityV1({**payload(), "row_provenance": 1}),
        cases(),
        version_resolver=lambda _: __version__,
        **kwargs,
    )


def declaration():
    from evolution.tree.execution.journal_schema import components_digest

    return dict(components_digest=components_digest(DECLARED_MAP), components=DECLARED_MAP)


def test_a_declaration_is_durable_and_read_back_by_digest(tmp_path):
    path = tmp_path / "run"
    with create_marked(path) as journal:
        journal.append("components_declared", declaration())
        declared = journal.declared_components()
        assert set(declared) == {declaration()["components_digest"]}
        assert declared[declaration()["components_digest"]].payload == declaration()
    with Journal.resume(
        path,
        "run",
        RunIdentityV1({**payload(), "row_provenance": 1}),
        cases(),
        version_resolver=lambda _: __version__,
    ) as journal:
        assert set(journal.declared_components()) == {declaration()["components_digest"]}


def test_a_declaration_without_room_stops_dispatch_and_leaves_the_writer_healthy(tmp_path):
    path = tmp_path / "run"
    with create_marked(path) as journal:
        size = journal.snapshot().valid_bytes
    # Room for the stop record, not for a declaration beside the reserved one.
    with Journal.resume(
        path,
        "run",
        RunIdentityV1({**payload(), "row_provenance": 1}),
        cases(),
        version_resolver=lambda _: __version__,
        max_record_bytes=2048,
        max_journal_bytes=size + 2048 + 16,
    ) as journal:
        with pytest.raises(JournalAdmissionRefused):
            journal.append("components_declared", declaration())
        assert journal.snapshot().records[-1].event == "journal_dispatch_stopped"
        assert journal.healthy
        # A stopped journal refuses a further declaration before the lifecycle.
        with pytest.raises(JournalAdmissionRefused):
            journal.append("components_declared", declaration())
        assert journal.healthy
        assert dict(journal.declared_components()) == {}
