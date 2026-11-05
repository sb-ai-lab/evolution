import errno
import os
import stat
import threading

import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCapacityError,
    ArtifactCorruptionError,
    ArtifactUnsafeError,
)
from evolution.tree.artifacts.artifact_ledger import MAX_LEDGER_BYTES
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import ArtifactStore, SelectionDecision
from evolution.tree.execution.candidate_integrity import ArtifactError
from evolution.tree.storage.journal_errors import JournalFault
from tests.tree.artifacts.test_artifact_policy import seed, write

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
VR = lambda _: __version__  # noqa: E731


def create(tmp_path, **kw):
    return ArtifactStore.create(
        tmp_path / "run", seed(tmp_path), POLICY, parallel_attempts=2, version_resolver=VR, **kw
    )


def decision(store, attempt, ref):
    return SelectionDecision(
        1,
        attempt,
        ref.digest,
        ref.dependency_digest,
        dict(
            version=1,
            kind="selection_decided",
            run_id=store.run_id,
            locator="campaign.jsonl#seq=3",
            content_digest="f" * 64,
        ),
    )


def events(store):
    return [r.event for r in store.ledger.snapshot().records]


def test_preflight_refuses_without_space(tmp_path, monkeypatch):
    src = seed(tmp_path)
    monkeypatch.setattr(
        os, "statvfs", lambda p: os.statvfs_result((4096, 4096, 100, 1, 1, 0, 0, 0, 0, 255))
    )
    with pytest.raises(ArtifactCapacityError) as info:
        ArtifactStore.preflight(tmp_path / "run", src, POLICY, 2)
    assert info.value.code == "storage_capacity_insufficient"
    assert not (tmp_path / "run").exists()


def test_preflight_reports_the_estimate_it_checked(tmp_path):
    src = seed(tmp_path)
    estimate = ArtifactStore.preflight(tmp_path / "run", src, POLICY, 2)
    scan = scan_tree(src, POLICY)
    assert estimate.v0_bytes == scan.byte_count
    assert estimate.required_bytes == scan.byte_count * 3 + MAX_LEDGER_BYTES
    assert estimate.free_bytes > estimate.required_bytes
    assert not (tmp_path / "run").exists()


def test_preflight_and_create_translate_an_unreadable_seed(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 file, so the failure cannot be provoked")
    src = seed(tmp_path)
    blocked = src / "skills/SKILL.md"
    blocked.chmod(0o000)
    try:
        for call in (
            lambda: ArtifactStore.preflight(tmp_path / "run", src, POLICY, 2),
            lambda: ArtifactStore.create(
                tmp_path / "run", src, POLICY, parallel_attempts=2, version_resolver=VR
            ),
        ):
            with pytest.raises(ArtifactError) as info:
                call()
            # The raw PermissionError would have carried the absolute seed path.
            assert info.value.code == "io_failure"
            assert str(src) not in str(info.value) and "SKILL.md" not in str(info.value)
    finally:
        blocked.chmod(0o644)
    assert not (tmp_path / "run").exists()


def test_preflight_and_create_refuse_a_missing_or_non_directory_seed(tmp_path):
    file_seed = tmp_path / "seed.txt"
    file_seed.write_bytes(b"not a tree")
    link_seed = tmp_path / "link"
    link_seed.symlink_to(seed(tmp_path))
    for bad in (tmp_path / "absent", file_seed, link_seed):
        with pytest.raises(ArtifactError) as info:
            ArtifactStore.preflight(tmp_path / "run", bad, POLICY, 2)
        assert info.value.code == "seed_missing"
        with pytest.raises(ArtifactError) as info:
            ArtifactStore.create(
                tmp_path / "run", bad, POLICY, parallel_attempts=2, version_resolver=VR
            )
        assert info.value.code == "seed_missing"
    assert not (tmp_path / "run").exists()


def test_create_refuses_a_seed_that_differs_from_the_expected_identity(tmp_path):
    expected = ArtifactStoreIdentityV1(POLICY, "0" * 64, "0" * 64)
    with pytest.raises(ArtifactAdmissionRefused) as caught:
        ArtifactStore.create(
            tmp_path / "run",
            seed(tmp_path),
            POLICY,
            parallel_attempts=1,
            version_resolver=VR,
            expected=expected,
        )
    assert caught.value.code == "run_identity_changed"
    assert not (tmp_path / "run" / "artifacts.jsonl").exists()


def test_negative_parallel_attempts_is_a_typed_admission_refusal(tmp_path):
    src = seed(tmp_path)
    with pytest.raises(ArtifactAdmissionRefused) as info:
        ArtifactStore.preflight(tmp_path / "run", src, POLICY, -1)
    assert info.value.code == "invalid_parallel_attempts"
    with pytest.raises(ArtifactAdmissionRefused) as info:
        ArtifactStore.create(
            tmp_path / "run", src, POLICY, parallel_attempts=-1, version_resolver=VR
        )
    assert info.value.code == "invalid_parallel_attempts"
    assert not (tmp_path / "run").exists()


def test_create_publishes_v0_from_one_prescan(tmp_path):
    with create(tmp_path) as store:
        champion = store.champion()
        assert (
            champion.artifact_id == "v0"
            and champion.root == tmp_path / "run/artifacts/champions/v0"
        )
        assert champion.digest == scan_tree(seed(tmp_path / "again"), POLICY).candidate_digest
        assert not (champion.root / ".env").exists()
        assert not (champion.root / "skills/SKILL.md").stat().st_mode & stat.S_IWUSR
        assert events(store) == ["store_created", "artifact_copy_started", "artifact_copy_finished"]
        assert store.ledger.snapshot().records[1].payload["candidate_digest"] == champion.digest
        champion.verify()
        assert champion.expected_attestation().candidate_digest == champion.digest
        assert champion.expected_attestation().dependency_digest == champion.dependency_digest


def test_materialize_edit_seal_publishes_only_the_sets(tmp_path):
    with create(tmp_path) as store:
        ws = store.materialize_candidate("a1")
        assert ws == tmp_path / "run/artifacts/.workspaces/a1"
        assert (ws / "skills/SKILL.md").stat().st_mode & stat.S_IWUSR
        write(ws, "skills/new/SKILL.md", b"new")
        write(ws, ".env", b"LEAK=1")  # excluded editor output
        write(ws, "skills/build.log", b"log")  # excluded by default pattern
        ref = store.seal("a1")
        assert ref.kind == "candidate" and ref.root == tmp_path / "run/artifacts/candidates/a1"
        assert (ref.root / "skills/new/SKILL.md").exists()
        assert not (ref.root / ".env").exists() and not (ref.root / "skills/build.log").exists()
        assert not ws.exists()
        assert events(store)[-4:] == [
            "workspace_opened",
            "workspace_ready",
            "artifact_copy_started",
            "artifact_copy_finished",
        ]
        with pytest.raises(ArtifactAdmissionRefused):
            store.seal("a1")


@pytest.mark.parametrize(
    "edit,expect",
    [
        (lambda ws: write(ws, "scripts/other.sh", b"x"), "artifact_unsafe"),  # outside writable
        (lambda ws: (ws / "scripts/run.sh").unlink(), "artifact_unsafe"),  # delete non-writable
        (lambda ws: write(ws, "uv.lock", b"changed"), "artifact_unsafe"),  # dependency bytes
        (lambda ws: os.symlink("/etc/passwd", ws / "skills/link"), "symlink"),
    ],
)
def test_seal_rejects_and_workspace_can_be_abandoned(tmp_path, edit, expect):
    with create(tmp_path) as store:
        ws = store.materialize_candidate("a1")
        edit(ws)
        with pytest.raises(ArtifactUnsafeError) as info:
            store.seal("a1")
        assert info.value.code == expect
        assert not (tmp_path / "run/artifacts/candidates/a1").exists()
        store.abandon("a1")
        assert not ws.exists() and events(store)[-1] == "candidate_abandoned"
        assert store.ledger.snapshot().records[-1].payload["reason"] == "caller_abandoned"


def test_abandoned_attempt_cannot_be_sealed_or_reopened(tmp_path):
    with create(tmp_path) as store:
        store.materialize_candidate("a1")
        store.abandon("a1")
        for call in (store.seal, store.materialize_candidate, store.abandon):
            with pytest.raises(ArtifactAdmissionRefused):
                call("a1")


def test_hard_link_out_of_run_root_is_never_published(tmp_path):
    outside = write(tmp_path / "outside", "secret.txt", b"outside")
    with create(tmp_path) as store:
        ws = store.materialize_candidate("a1")
        os.link(outside, ws / "skills/alias.txt")
        with pytest.raises(ArtifactUnsafeError):
            store.seal("a1")
    assert outside.read_bytes() == b"outside"


def test_promote_records_pending_then_committed_and_is_idempotent(tmp_path):
    with create(tmp_path) as store:
        store.materialize_candidate("a1")
        write(tmp_path / "run/artifacts/.workspaces/a1", "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        champion = store.promote(decision(store, "a1", ref))
        assert champion.artifact_id == "v1" and champion.digest == ref.digest
        assert store.champion().artifact_id == "v1"
        assert events(store)[-4:] == [
            "promotion_pending",
            "artifact_copy_started",
            "artifact_copy_finished",
            "promotion_committed",
        ]
        assert store.promote(decision(store, "a1", ref)).artifact_id == "v1"  # same decision: no-op
        other = decision(store, "a1", ref)
        other = SelectionDecision(
            1,
            "a1",
            ref.digest,
            ref.dependency_digest,
            dict(other.decision_ref, content_digest="0" * 64),
        )
        with pytest.raises(ArtifactAdmissionRefused):
            store.promote(other)
        bad = SelectionDecision(2, "a1", "9" * 64, ref.dependency_digest, other.decision_ref)
        with pytest.raises(ArtifactAdmissionRefused):
            store.promote(bad)


def test_second_round_promotes_from_the_new_champion(tmp_path):
    with create(tmp_path) as store:
        store.materialize_candidate("a1")
        write(tmp_path / "run/artifacts/.workspaces/a1", "skills/SKILL.md", b"better")
        first = store.promote(decision(store, "a1", store.seal("a1")))
        workspace = store.materialize_candidate("a2")
        assert (workspace / "skills/SKILL.md").read_bytes() == b"better"
        write(workspace, "skills/SKILL.md", b"best")
        ref = store.seal("a2")
        second = SelectionDecision(
            2, "a2", ref.digest, ref.dependency_digest, decision(store, "a2", ref).decision_ref
        )
        champion = store.promote(second)
        assert champion.artifact_id == "v2" and first.artifact_id == "v1"
        assert (champion.root / "skills/SKILL.md").read_bytes() == b"best"
        champion.verify()


def test_capacity_exhaustion_closes_session_and_publishes_nothing(tmp_path, monkeypatch):
    import shutil

    with create(tmp_path) as store:
        store.materialize_candidate("a1")
        real = shutil.copy2

        def boom(src, dst, *a, **k):
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(shutil, "copy2", boom)
        with pytest.raises(ArtifactCapacityError) as info:
            store.seal("a1")
        assert info.value.code == "storage_capacity_exhausted"
        monkeypatch.setattr(shutil, "copy2", real)
        assert not (tmp_path / "run/artifacts/candidates/a1").exists()
        assert list((tmp_path / "run/artifacts/.staging").iterdir()) == []
        assert events(store)[-1] == "artifact_copy_failed"
        with pytest.raises(ArtifactAdmissionRefused):
            store.materialize_candidate("a2")
        # Exhaustion refuses copies only: the caller can still read the champion and
        # release the workspace bytes it no longer needs.
        assert store.champion().artifact_id == "v0"
        store.abandon("a1")
        assert not (tmp_path / "run/artifacts/.workspaces/a1").exists()
        assert events(store)[-1] == "candidate_abandoned"


def test_workspace_copy_failure_records_a_failed_terminal(tmp_path, monkeypatch):
    import shutil

    def boom(src, dst, *a, **k):
        raise OSError(errno.EIO, "io")

    with create(tmp_path) as store:
        monkeypatch.setattr(shutil, "copy2", boom)
        with pytest.raises(ArtifactError) as info:
            store.materialize_candidate("a1")
        assert info.value.code == "io_failure"
        assert events(store)[-2:] == ["workspace_opened", "workspace_failed"]
        assert store.ledger.snapshot().records[-1].payload["code"] == "io_failure"
        assert not (tmp_path / "run/artifacts/.workspaces/a1").exists()
        assert list((tmp_path / "run/artifacts/.staging").iterdir()) == []


@pytest.mark.parametrize("failing", ["scan_tree", "changes"])
def test_seal_translates_workspace_read_failures(tmp_path, monkeypatch, failing):
    from evolution.tree.artifacts import artifacts as module

    with create(tmp_path) as store:
        workspace = store.materialize_candidate("a1")
        real = getattr(module, failing)

        def flaky(*args, **kwargs):
            if workspace in args:
                raise OSError(errno.EIO, "unreadable")
            return real(*args, **kwargs)

        monkeypatch.setattr(module, failing, flaky)
        before = len(events(store))
        with pytest.raises(ArtifactError) as info:
            store.seal("a1")
        assert info.value.code == "io_failure"
        # No ledger record and no state change: the workspace can be retried or dropped.
        assert len(events(store)) == before
        assert store.ledger.state.workspaces["a1"] == "ready"
        monkeypatch.setattr(module, failing, real)
        assert store.seal("a1").artifact_id == "a1"


def test_promote_refuses_a_malformed_decision_without_touching_the_ledger(tmp_path):
    with create(tmp_path) as store:
        store.materialize_candidate("a1")
        write(tmp_path / "run/artifacts/.workspaces/a1", "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        before = len(events(store))
        good = decision(store, "a1", ref)
        for broken in (
            SelectionDecision([1], "a1", ref.digest, ref.dependency_digest, good.decision_ref),
            SelectionDecision(1, "a1", ref.digest, ref.dependency_digest, "not-a-reference"),
            SelectionDecision(
                1, "a1", ref.digest, ref.dependency_digest, dict(good.decision_ref, extra=1)
            ),
            SelectionDecision(
                1, "a1", ref.digest, ref.dependency_digest, dict(good.decision_ref, locator="/abs")
            ),
        ):
            with pytest.raises(ArtifactAdmissionRefused):
                store.promote(broken)
        assert len(events(store)) == before
        assert store.promote(good).artifact_id == "v1"


def test_champion_corruption_stops_store(tmp_path):
    with create(tmp_path) as store:
        root = store.champion().root
        (root / "skills/SKILL.md").chmod(0o644)
        (root / "skills/SKILL.md").write_bytes(b"tampered")
        with pytest.raises(ArtifactCorruptionError):
            store.materialize_candidate("a1")
        assert store.stopped == "artifact_corrupted" and events(store)[-1] == "store_stopped"


def test_transient_read_failure_on_a_published_root_never_stops_the_store(tmp_path, monkeypatch):
    """An `EIO` says nothing about the champion's bytes, so it must not write the
    permanent `store_stopped(artifact_corrupted)` that nothing can undo.
    """
    from evolution.tree.artifacts import artifacts as module

    with create(tmp_path) as store:
        champion_root = tmp_path / "run/artifacts/champions/v0"
        real = module.scan_tree

        def flaky(root, policy):
            if root == champion_root:
                raise OSError(errno.EIO, "unreadable")
            return real(root, policy)

        monkeypatch.setattr(module, "scan_tree", flaky)
        before = len(events(store))
        with pytest.raises(ArtifactError) as info:
            store.champion()
        assert type(info.value) is ArtifactError and info.value.code == "io_failure"
        assert store.stopped is None and "store_stopped" not in events(store)
        assert len(events(store)) == before
        monkeypatch.setattr(module, "scan_tree", real)
        assert store.champion().artifact_id == "v0"
        assert store.materialize_candidate("a1").exists()


def test_stopped_store_refuses_every_new_operation(tmp_path):
    with create(tmp_path) as store:
        ws = store.materialize_candidate("a1")
        root = store.champion().root
        (root / "skills/SKILL.md").chmod(0o644)
        (root / "skills/SKILL.md").write_bytes(b"tampered")
        with pytest.raises(ArtifactCorruptionError):
            store.seal("a1")
        assert store.stopped == "artifact_corrupted"
        assert ws.exists() and not (tmp_path / "run/artifacts/candidates/a1").exists()
        with pytest.raises(ArtifactAdmissionRefused):
            store.materialize_candidate("a2")
        with pytest.raises(ArtifactAdmissionRefused):
            store.champion()


def test_hooks_run_at_every_declared_point(tmp_path):
    seen: list[str] = []
    names = (
        "after_store_created",
        "after_started",
        "after_replace",
        "after_finished",
        "after_workspace_opened",
        "after_workspace_ready",
        "after_seal_finished",
        "after_pending",
    )
    hooks = {name: (lambda name=name: seen.append(name)) for name in names}
    with create(tmp_path, _hooks=hooks) as store:
        assert seen == ["after_store_created", "after_started", "after_replace", "after_finished"]
        ws = store.materialize_candidate("a1")
        assert seen[-2:] == ["after_workspace_opened", "after_workspace_ready"]
        write(ws, "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        assert seen[-4:] == [
            "after_started",
            "after_replace",
            "after_finished",
            "after_seal_finished",
        ]
        assert ws.exists() is False
        store.promote(decision(store, "a1", ref))
        assert seen[-4:] == ["after_pending", "after_started", "after_replace", "after_finished"]


def test_parallel_materialization_keeps_the_ledger_contiguous(tmp_path):
    with create(tmp_path) as store:
        workspaces: dict[str, object] = {}
        failures: list[BaseException] = []
        barrier = threading.Barrier(2)

        def open_workspace(attempt: str) -> None:
            try:
                barrier.wait(30)
                workspaces[attempt] = store.materialize_candidate(attempt)
            except BaseException as exc:  # pragma: no cover - reported by the assertion
                failures.append(exc)

        threads = [threading.Thread(target=open_workspace, args=(a,)) for a in ("a1", "a2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not failures and set(workspaces) == {"a1", "a2"}
        for attempt in ("a1", "a2"):
            assert workspaces[attempt] == tmp_path / f"run/artifacts/.workspaces/{attempt}"
            assert (workspaces[attempt] / "skills/SKILL.md").read_bytes() == b"skill"
        assert store.ledger.state.workspaces == {"a1": "ready", "a2": "ready"}
        records = store.ledger.snapshot().records
        assert [r.to_payload()["sequence"] for r in records] == list(range(1, len(records) + 1))
        assert sorted(events(store)[3:]) == [
            "workspace_opened",
            "workspace_opened",
            "workspace_ready",
            "workspace_ready",
        ]


def test_racing_on_one_attempt_id_refuses_exactly_one_caller(tmp_path):
    with create(tmp_path) as store:
        opened: list[object] = []
        refused: list[BaseException] = []
        failures: list[BaseException] = []
        barrier = threading.Barrier(2)

        def open_workspace() -> None:
            try:
                barrier.wait(30)
                opened.append(store.materialize_candidate("a1"))
            except ArtifactAdmissionRefused as exc:
                refused.append(exc)
            except BaseException as exc:  # pragma: no cover - reported by the assertion
                failures.append(exc)

        threads = [threading.Thread(target=open_workspace) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not failures and len(opened) == 1 and len(refused) == 1
        assert store.ledger.state.workspaces == {"a1": "ready"}
        # The refused caller left the writer healthy.
        assert store.materialize_candidate("a2").exists()
        records = store.ledger.snapshot().records
        assert [r.to_payload()["sequence"] for r in records] == list(range(1, len(records) + 1))


def test_a_capacity_fault_at_the_terminal_append_is_typed_and_recoverable(tmp_path, monkeypatch):
    """On a full filesystem the ledger cannot record `artifact_copy_finished`,
    so the still-open operation is the last durable boundary. The caller sees the typed
    capacity error, not the raw `JournalFault` from a copy that already reached disk.
    """
    run = tmp_path / "run"
    store = create(tmp_path)
    try:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")
        real = store.ledger.append

        def append(event, payload, operation_id=None):
            if event == "artifact_copy_finished":
                raise JournalFault("storage_capacity_exhausted")
            return real(event, payload, operation_id)

        monkeypatch.setattr(store.ledger, "append", append)
        with pytest.raises(ArtifactCapacityError) as info:
            store.seal("a1")
        assert info.value.code == "storage_capacity_exhausted"
        monkeypatch.undo()
        # The session refuses further copies from the moment exhaustion is known,
        with pytest.raises(ArtifactAdmissionRefused) as refused:
            store.materialize_candidate("a2")
        assert refused.value.code == "storage_capacity_exhausted"
        # ... while nothing permanent is written and the operation stays open.
        assert store.stopped is None
        assert events(store)[-1] == "artifact_copy_started"
        assert (run / "artifacts/candidates/a1").is_dir()
    finally:
        store.close()
    with ArtifactStore.resume(run, POLICY, version_resolver=VR) as resumed:
        # The workspace outlived the failed seal and still reproduces the started
        # expectation, so recovery repeats the copy; `publish` reuses the identical
        # destination the interrupted operation had already written.
        assert resumed.last_recovery.actions == ("seal_rerun",)
        assert resumed.last_recovery.removed_workspaces == ("a1",)
        assert resumed.ledger.state.workspaces["a1"] == "sealed"
        assert events(resumed)[-1] == "artifact_copy_finished"


def test_an_owed_rebase_is_written_before_recovery_touches_the_ledger(tmp_path, monkeypatch):
    """ADR-0015: "a resume that finds the first without the second writes the
    second first" -- first of everything, not merely first of this session's
    publications. Here an artifact operation was open at the crash, so `_recover`
    has an `operation_interrupted` of its own to write; the owed `store_rebased`
    still precedes it, and an audit reads the campaign's declaration beside its
    completion instead of a repair record.
    """

    run = tmp_path / "run"
    store = create(tmp_path)
    try:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")
        real = store.ledger.append

        def append(event, payload, operation_id=None):
            if event == "artifact_copy_finished":
                raise JournalFault("storage_capacity_exhausted")
            return real(event, payload, operation_id)

        monkeypatch.setattr(store.ledger, "append", append)
        with pytest.raises(ArtifactCapacityError):
            store.seal("a1")
        monkeypatch.undo()
        assert events(store)[-1] == "artifact_copy_started"
    finally:
        store.close()

    owed = dict(campaign_rebase="c" * 64, authorization_id="0" * 64, producing_version="0.3.0a11")
    with ArtifactStore.resume(
        run, POLICY, version_resolver=VR, owed_rebases=[owed], admitted_versions={"0.3.0a11"}
    ) as resumed:
        names = events(resumed)
        assert names.index("store_rebased") < names.index("operation_interrupted")
        stamp = [r for r in resumed.ledger.snapshot().records if r.event == "store_rebased"][0]
        assert stamp.payload["campaign_rebase"] == "c" * 64
        assert stamp.payload["producing_version"] == "0.3.0a11"
        # The repair itself still happened, after the debt was paid.
        assert resumed.last_recovery.actions == ("seal_rerun",)


def test_a_capacity_fault_on_the_workspace_terminal_is_typed_too(tmp_path, monkeypatch):
    """The same terminal-append boundary on the `workspace` operation pair."""
    store = create(tmp_path)
    try:
        real = store.ledger.append

        def append(event, payload, operation_id=None):
            if event == "workspace_ready":
                raise JournalFault("storage_capacity_exhausted")
            return real(event, payload, operation_id)

        monkeypatch.setattr(store.ledger, "append", append)
        with pytest.raises(ArtifactCapacityError) as info:
            store.materialize_candidate("a1")
        assert info.value.code == "storage_capacity_exhausted"
        monkeypatch.undo()
        assert store.stopped is None
        assert events(store)[-1] == "workspace_opened"
        with pytest.raises(ArtifactAdmissionRefused) as refused:
            store.materialize_candidate("a2")
        assert refused.value.code == "storage_capacity_exhausted"
    finally:
        store.close()


def test_a_non_capacity_fault_at_the_terminal_append_is_not_translated(tmp_path, monkeypatch):
    """Any other ledger fault is an unusable writer, and stays untranslated."""
    store = create(tmp_path)
    try:
        real = store.ledger.append

        def append(event, payload, operation_id=None):
            if event == "workspace_ready":
                raise JournalFault("journal_unavailable")
            return real(event, payload, operation_id)

        monkeypatch.setattr(store.ledger, "append", append)
        with pytest.raises(JournalFault) as info:
            store.materialize_candidate("a1")
        assert info.value.code == "journal_unavailable"
        monkeypatch.undo()
        assert store._session_stopped is False
    finally:
        store.close()


def test_create_refuses_a_seed_with_an_unreadable_included_directory(tmp_path):
    """A silently truncated seed scan would publish `v0` as an exact identity for a
    tree that was never fully read; the walk fails instead and nothing is published.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 0o000 directory, so the failure cannot be provoked")
    src = seed(tmp_path)
    write(src, "skills/private/secret.md", b"private")
    private = src / "skills/private"
    private.chmod(0o000)
    try:
        with pytest.raises(ArtifactError) as info:
            ArtifactStore.create(
                tmp_path / "run", src, POLICY, parallel_attempts=2, version_resolver=VR
            )
        assert type(info.value) is ArtifactError and info.value.code == "io_failure"
        assert not (tmp_path / "run").exists()
    finally:
        private.chmod(0o700)


def test_an_excluded_entry_inside_a_published_champion_is_corruption(tmp_path):
    """A published copy never contains an excluded entry. The digests cover
    only what the policy includes, so a stray `.env` reproduces them exactly; only the
    whole on-disk entry set sees it, and it stops the store like any other corruption.
    """
    with create(tmp_path) as store:
        root = store.champion().root
        root.chmod(0o700)
        (root / ".env").write_bytes(b"SECRET=1")
        # The scan itself is unchanged: both digests still match what was published.
        assert (
            scan_tree(root, POLICY).candidate_digest
            == store.ledger.state.published["champion/v0"].payload["candidate_digest"]
        )
        with pytest.raises(ArtifactCorruptionError) as info:
            store.champion()
        assert info.value.code == "artifact_unsafe"
        assert store.stopped == "artifact_corrupted"
        assert events(store)[-1] == "store_stopped"
        assert store.ledger.snapshot().records[-1].payload["code"] == "artifact_corrupted"


def test_a_capacity_fault_at_the_failure_terminal_is_typed_and_recoverable(tmp_path, monkeypatch):
    """A full filesystem fails the copy *and* the append that records the failure.
    The second fault must reach the caller as the same typed capacity error, not as a
    raw `JournalFault`: the open `artifact_copy_started` stays the durable boundary.
    """
    import shutil

    run = tmp_path / "run"
    store = create(tmp_path)
    try:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")

        def boom(src, dst, *a, **k):
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(shutil, "copy2", boom)
        real = store.ledger.append

        def append(event, payload, operation_id=None):
            if event == "artifact_copy_failed":
                raise JournalFault("storage_capacity_exhausted")
            return real(event, payload, operation_id)

        monkeypatch.setattr(store.ledger, "append", append)
        with pytest.raises(ArtifactCapacityError) as info:
            store.seal("a1")
        assert info.value.code == "storage_capacity_exhausted"
        monkeypatch.undo()
        assert events(store)[-1] == "artifact_copy_started"
        assert store.stopped is None
        assert not (run / "artifacts/candidates/a1").exists()
        assert list((run / "artifacts/.staging").iterdir()) == []
        with pytest.raises(ArtifactAdmissionRefused) as refused:
            store.materialize_candidate("a2")
        assert refused.value.code == "storage_capacity_exhausted"
    finally:
        store.close()
    with ArtifactStore.resume(run, POLICY, version_resolver=VR) as resumed:
        assert resumed.last_recovery.actions == ("seal_rerun",)
        assert resumed.ledger.state.workspaces["a1"] == "sealed"
        assert events(resumed)[-1] == "artifact_copy_finished"


def test_published_ref_is_unverified_and_never_stops_the_store(tmp_path):
    with create(tmp_path) as store:
        ws = store.materialize_candidate("a1")
        write(ws, "skills/new/SKILL.md", b"new")
        sealed = store.seal("a1")

        ref = store.published_ref("candidate", "a1")
        assert (ref.kind, ref.artifact_id, ref.root) == ("candidate", "a1", sealed.root)
        assert (ref.digest, ref.dependency_digest) == (sealed.digest, sealed.dependency_digest)
        ref.verify()
        store.published_ref("champion", "v0").verify()
        with pytest.raises(ArtifactAdmissionRefused):
            store.published_ref("candidate", "a2")

        target = ref.root / "skills/new/SKILL.md"
        os.chmod(target, 0o600)
        target.write_bytes(b"changed")
        with pytest.raises(ArtifactError):
            ref.verify()
        assert store.stopped is None
        assert events(store)[-1] == "artifact_copy_finished"
