"""Every durable prefix reaches publication, `promotion_committed`, a
recoverable pre-`v0` state or a typed durable stop, and repeated recovery is a
no-op. SIGKILL children produce the prefixes no in-process failure can.
"""

import errno
import multiprocessing as mp
import os
import shutil
import signal

import pytest

from evolution import __version__
from evolution.tree.artifacts import artifacts as module
from evolution.tree.artifacts.artifact_errors import (
    ArtifactAdmissionRefused,
    ArtifactCapacityError,
    ArtifactCorruptionError,
    ArtifactStoreStopped,
    ArtifactUnsafeError,
)
from evolution.tree.artifacts.artifact_ledger import inspect_ledger
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifacts import ArtifactStore, SelectionDecision
from evolution.tree.execution.candidate_integrity import ArtifactError
from tests.tree.artifacts.test_artifact_policy import seed, write

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
VR = lambda _: __version__  # noqa: E731


def create(run, src, **kw):
    return ArtifactStore.create(run, src, POLICY, parallel_attempts=1, version_resolver=VR, **kw)


def resume(run, src=None):
    return ArtifactStore.resume(run, POLICY, seed_root=src, version_resolver=VR)


def decision(store, attempt, ref, round_id=1):
    return SelectionDecision(
        round_id,
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


def records(store):
    return store.ledger.snapshot().records


def raiser(code):
    def boom(*args, **kwargs):
        raise OSError(code, "injected")

    return boom


def workspace_of(run, attempt="a1"):
    return run / f"artifacts/.workspaces/{attempt}"


# -- crash fixtures ------------------------------------------------------------


def kill():
    os.kill(os.getpid(), signal.SIGKILL)


def child(run, src, cut):
    """Run the lifecycle until `phase:hook`, then SIGKILL inside that hook.

    The hooks are installed only once the phase they belong to is reached, so the
    same hook name can cut the `v0`, the seal and the promotion copy.
    """
    phase, _, hook = cut.partition(":")
    hooks = {hook: kill}
    if phase == "v0":
        create(run, src, _hooks=hooks)
        return
    store = create(run, src)
    if phase == "workspace":
        store._hooks = hooks
    workspace = store.materialize_candidate("a1")
    write(workspace, "skills/SKILL.md", b"better")
    if phase == "seal":
        store._hooks = hooks
    ref = store.seal("a1")
    if phase == "promote":
        store._hooks = hooks
    store.promote(decision(store, "a1", ref))


def capacity_v0_child(run, src, _cut=None):
    """A `v0` copy that fails on a full filesystem, then a hard kill.

    The patch lives in the child only, so the resuming parent copies normally.
    """
    shutil.copy2 = raiser(errno.ENOSPC)
    try:
        create(run, src)
    except ArtifactCapacityError:
        pass
    kill()


def crash(tmp_path, cut, target=child):
    run, src = tmp_path / "run", seed(tmp_path)
    process = mp.get_context("fork").Process(target=target, args=(run, src, cut))
    process.start()
    process.join(60)
    if process.is_alive():  # pragma: no cover - only on a wedged child
        process.kill()
        process.join(5)
        pytest.fail(f"child did not reach {cut}")
    assert process.exitcode == -signal.SIGKILL
    return run, src


# -- pre-`v0` prefixes ---------------------------------------------------------


@pytest.mark.parametrize("cut", ["v0:after_store_created", "v0:after_started", "v0:after_replace"])
def test_v0_crash_requires_seed_and_reruns(tmp_path, cut):
    run, src = crash(tmp_path, cut)
    with pytest.raises(ArtifactAdmissionRefused) as refused:
        resume(run)  # seed_root required before v0
    assert refused.value.code == "seed_root_required"
    write(src, "skills/SKILL.md", b"changed")
    with pytest.raises(ArtifactCorruptionError) as info:
        resume(run, src)
    assert info.value.code == "seed_mismatch"
    write(src, "skills/SKILL.md", b"skill")
    with resume(run, src) as store:
        assert store.champion().artifact_id == "v0"
        assert events(store)[-1] == "artifact_copy_finished"
        assert ("operation_interrupted" in events(store)) == (cut != "v0:after_store_created")
        assert store.last_recovery.actions == ("v0_rerun",)
        after = len(events(store))
    with resume(run) as store:  # v0 published: seed_root now rejected
        assert len(events(store)) == after
        assert store.last_recovery.actions == ()
    with pytest.raises(ArtifactAdmissionRefused) as rejected:
        resume(run, src)
    assert rejected.value.code == "seed_root_rejected"


def test_v0_finished_then_resume_appends_nothing(tmp_path):
    run, src = crash(tmp_path, "v0:after_finished")
    with resume(run) as store:
        before = len(events(store))
        assert store.champion().artifact_id == "v0"
        assert store.last_recovery.actions == ()
    with resume(run) as store:
        assert len(events(store)) == before
    with pytest.raises(ArtifactAdmissionRefused):
        resume(run, src)


def test_capacity_failed_v0_reruns_once_capacity_returns(tmp_path):
    run, src = crash(tmp_path, "capacity", target=capacity_v0_child)
    with resume(run, src) as store:
        assert events(store)[:3] == [
            "store_created",
            "artifact_copy_started",
            "artifact_copy_failed",
        ]
        assert records(store)[2].payload["code"] == "storage_capacity_exhausted"
        assert store.last_recovery.actions == ("v0_rerun",)
        assert store.champion().artifact_id == "v0"
        assert events(store)[-1] == "artifact_copy_finished"


def test_unsafe_v0_copy_stays_a_recoverable_pre_v0_state(tmp_path, monkeypatch):
    run, src = tmp_path / "run", seed(tmp_path)

    def unsafe(*args, **kwargs):
        raise ArtifactUnsafeError("symlink")

    monkeypatch.setattr(module, "stage_copy", unsafe)
    with pytest.raises(ArtifactUnsafeError):
        create(run, src)
    monkeypatch.undo()
    with resume(run, src) as store:
        assert store.champion().artifact_id == "v0"
        assert store.last_recovery.actions == ("v0_rerun",)
        assert events(store)[:4] == [
            "store_created",
            "artifact_copy_started",
            "artifact_copy_failed",
            "artifact_copy_started",
        ]


# -- workspace prefixes --------------------------------------------------------


def test_workspace_open_crash_is_abandoned_and_removed(tmp_path):
    run, _ = crash(tmp_path, "workspace:after_workspace_opened")
    with resume(run) as store:
        assert events(store)[-2:] == ["operation_interrupted", "candidate_abandoned"]
        assert records(store)[-1].payload["reason"] == "workspace_not_ready"
        assert not workspace_of(run).exists()


def test_ready_workspace_without_seal_is_abandoned(tmp_path):
    run, _ = crash(tmp_path, "workspace:after_workspace_ready")
    assert workspace_of(run).exists()
    with resume(run) as store:
        assert records(store)[-1].payload["reason"] == "unsealed_at_recovery"
        assert not workspace_of(run).exists()
        assert store.last_recovery.removed_workspaces == ("a1",)


def test_failed_workspace_without_abandonment_is_abandoned(tmp_path, monkeypatch):
    run, src = tmp_path / "run", seed(tmp_path)
    with create(run, src) as store:
        monkeypatch.setattr(shutil, "copy2", raiser(errno.EIO))
        with pytest.raises(ArtifactError):
            store.materialize_candidate("a1")
        assert events(store)[-1] == "workspace_failed"
    monkeypatch.undo()
    with resume(run) as store:
        assert events(store)[-1] == "candidate_abandoned"
        assert records(store)[-1].payload["reason"] == "workspace_not_ready"
        assert store.ledger.state.workspaces["a1"] == "abandoned"


# -- seal prefixes -------------------------------------------------------------


def test_open_seal_copy_is_rerun_from_the_intact_workspace(tmp_path):
    run, _ = crash(tmp_path, "seal:after_started")
    assert workspace_of(run).exists()
    with resume(run) as store:
        assert events(store)[-3:] == [
            "operation_interrupted",
            "artifact_copy_started",
            "artifact_copy_finished",
        ]
        assert store.last_recovery.actions == ("seal_rerun",)
        assert store.ledger.state.workspaces["a1"] == "sealed"
        assert (run / "artifacts/candidates/a1/skills/SKILL.md").read_bytes() == b"better"
        assert not workspace_of(run).exists()


def test_open_seal_copy_with_a_modified_workspace_is_abandoned(tmp_path):
    run, _ = crash(tmp_path, "seal:after_started")
    write(workspace_of(run), "skills/SKILL.md", b"edited after the cut")
    with resume(run) as store:
        assert events(store)[-2:] == ["operation_interrupted", "candidate_abandoned"]
        assert records(store)[-1].payload["reason"] == "seal_not_published"
        assert not (run / "artifacts/candidates/a1").exists()
        assert not workspace_of(run).exists()


def test_capacity_failed_seal_copy_is_rerun(tmp_path, monkeypatch):
    run, src = tmp_path / "run", seed(tmp_path)
    with create(run, src) as store:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")
        monkeypatch.setattr(shutil, "copy2", raiser(errno.ENOSPC))
        with pytest.raises(ArtifactCapacityError):
            store.seal("a1")
        assert events(store)[-1] == "artifact_copy_failed"
    monkeypatch.undo()
    with resume(run) as store:
        assert store.last_recovery.actions == ("seal_rerun",)
        assert store.ledger.state.workspaces["a1"] == "sealed"
        assert not workspace_of(run).exists()


def test_unsafe_seal_copy_is_never_retried(tmp_path, monkeypatch):
    run, src = tmp_path / "run", seed(tmp_path)
    real = module.stage_copy
    with create(run, src) as store:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")

        def unsafe(source, *args, **kwargs):
            if source == workspace:
                raise ArtifactUnsafeError("symlink")
            return real(source, *args, **kwargs)

        monkeypatch.setattr(module, "stage_copy", unsafe)
        with pytest.raises(ArtifactUnsafeError):
            store.seal("a1")
        assert records(store)[-1].payload["code"] == "artifact_unsafe"
    monkeypatch.undo()
    # The workspace is intact, but an unsafe seal is terminal, not retriable.
    assert workspace_of(run).exists()
    with resume(run) as store:
        assert events(store)[-1] == "candidate_abandoned"
        assert records(store)[-1].payload["reason"] == "seal_not_published"
        assert store.last_recovery.actions == ("abandoned_seal_not_published",)
        assert not workspace_of(run).exists()


def test_sealed_candidate_without_promotion_only_loses_its_workspace(tmp_path):
    run, _ = crash(tmp_path, "seal:after_seal_finished")
    assert workspace_of(run).exists()
    with resume(run) as store:
        assert not workspace_of(run).exists()
        assert store.last_recovery.removed_workspaces == ("a1",)
        assert store.last_recovery.actions == ()
        assert events(store)[-1] == "artifact_copy_finished"


# -- promotion prefixes --------------------------------------------------------


@pytest.mark.parametrize(
    "cut", ["promote:after_pending", "promote:after_started", "promote:after_finished"]
)
def test_pending_promotion_rolls_forward_to_the_same_winner(tmp_path, cut):
    run, _ = crash(tmp_path, cut)
    with resume(run) as store:
        assert store.champion().artifact_id == "v1"
        assert events(store)[-1] == "promotion_committed"
        assert store.last_recovery.actions == ("promotion_completed",)
        assert (run / "artifacts/champions/v1/skills/SKILL.md").read_bytes() == b"better"
        after = len(events(store))
    with resume(run) as store:
        assert len(events(store)) == after
        assert store.last_recovery.actions == ()


def test_capacity_failed_promotion_copy_is_rerun(tmp_path, monkeypatch):
    run, src = tmp_path / "run", seed(tmp_path)
    with create(run, src) as store:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        monkeypatch.setattr(shutil, "copy2", raiser(errno.ENOSPC))
        with pytest.raises(ArtifactCapacityError):
            store.promote(decision(store, "a1", ref))
    monkeypatch.undo()
    with resume(run) as store:
        assert store.champion().artifact_id == "v1"
        assert store.last_recovery.actions == ("promotion_completed",)


def test_pending_promotion_with_a_corrupt_source_stops_the_store(tmp_path):
    run, _ = crash(tmp_path, "promote:after_pending")
    path = run / "artifacts/candidates/a1/skills/SKILL.md"
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactCorruptionError) as info:
        resume(run)
    assert info.value.code == "artifact_digest_mismatch"
    with resume(run) as store:
        assert store.stopped == "artifact_corrupted"
        assert store.last_recovery.actions == ()
        # Inspection stays available; every copy is refused.
        assert events(store)[-1] == "store_stopped"
        with pytest.raises(ArtifactStoreStopped):
            store.materialize_candidate("a2")


def test_unsafe_promotion_copy_stops_the_store(tmp_path, monkeypatch):
    run, src = tmp_path / "run", seed(tmp_path)
    real = module.stage_copy
    with create(run, src) as store:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        candidate = run / "artifacts/candidates/a1"

        def unsafe(source, *args, **kwargs):
            if source == candidate:
                raise ArtifactUnsafeError("destination_exists")
            return real(source, *args, **kwargs)

        monkeypatch.setattr(module, "stage_copy", unsafe)
        with pytest.raises(ArtifactUnsafeError):
            store.promote(decision(store, "a1", ref))
    monkeypatch.undo()
    with pytest.raises(ArtifactCorruptionError) as info:
        resume(run)
    assert info.value.code == "artifact_unsafe"
    with resume(run) as store:
        assert store.stopped == "artifact_corrupted"
        with pytest.raises(ArtifactStoreStopped):
            store.materialize_candidate("a2")


# -- idempotence and derived cleanup ------------------------------------------


def test_repeated_recovery_of_a_clean_run_appends_nothing(tmp_path):
    run, src = tmp_path / "run", seed(tmp_path)
    with create(run, src) as store:
        workspace = store.materialize_candidate("a1")
        write(workspace, "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        store.promote(decision(store, "a1", ref))
        before = len(events(store))
    for _ in range(2):
        with resume(run) as store:
            assert len(events(store)) == before
            assert store.last_recovery.actions == ()
            assert store.last_recovery.removed_workspaces == ()
            assert store.champion().artifact_id == "v1"


def test_resume_clears_staging_and_recreates_a_missing_layout(tmp_path):
    run, src = tmp_path / "run", seed(tmp_path)
    with create(run, src) as store:
        pass
    staged = run / "artifacts/.staging/.copy.leftover"
    write(staged, "skills/SKILL.md", b"orphan")
    shutil.rmtree(run / "artifacts/.workspaces")
    with resume(run) as store:
        assert list((run / "artifacts/.staging").iterdir()) == []
        assert (run / "artifacts/.workspaces").is_dir()
        assert store.champion().artifact_id == "v0"


def test_derived_cleanup_reports_io_failures(tmp_path, monkeypatch):
    run, _ = crash(tmp_path, "workspace:after_workspace_ready")

    def boom(root):
        raise OSError(errno.EIO, "unremovable")

    monkeypatch.setattr(module, "remove_tree", boom)
    with pytest.raises(ArtifactError) as info:
        resume(run)
    assert info.value.code == "io_failure"
    monkeypatch.undo()
    # The abandonment is durable, so the next resume only has to remove the tree.
    with resume(run) as store:
        assert store.last_recovery.removed_workspaces == ("a1",)
        assert store.last_recovery.actions == ()
        assert not workspace_of(run).exists()


# -- layout tampering ----------------------------------------------------------


@pytest.mark.parametrize("component", ["artifacts", "artifacts/champions"])
def test_resume_refuses_a_layout_component_replaced_by_a_symlink(tmp_path, component):
    """ADR-0005: the layout is created, never followed. A symlinked component would
    otherwise be adopted by `mkdir(exist_ok=True)` and chmod-ed through.
    """
    run = tmp_path / "run"
    with create(run, seed(tmp_path)) as store:
        assert store.champion().artifact_id == "v0"
    ledger_before = (run / "artifacts.jsonl").read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = run / component
    target.rename(tmp_path / "stashed")
    os.symlink(outside, target)
    with pytest.raises(ArtifactUnsafeError) as info:
        resume(run)
    assert info.value.code == "escape"
    assert list(outside.iterdir()) == []
    assert (run / "artifacts.jsonl").read_bytes() == ledger_before
    assert target.is_symlink()


def test_resume_refuses_a_foreign_owned_layout_component(tmp_path, monkeypatch):
    run = tmp_path / "run"
    with create(run, seed(tmp_path)):
        pass
    real_euid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
    with pytest.raises(ArtifactUnsafeError) as info:
        resume(run)
    assert info.value.code == "foreign_owner"


# -- promotion source re-verification ------------------------------------------


def test_a_published_champion_copy_does_not_excuse_a_corrupt_promotion_source(tmp_path):
    """The champion copy is durable but `promotion_committed` is not: the selected
    Candidate is still the promotion source and its corruption must stop the store.
    """
    run, _ = crash(tmp_path, "promote:after_finished")
    # The prefix the cut produced: the champion copy is durable, the commit is not.
    assert (run / "artifacts/champions/v1").is_dir()
    prefix = [r.event for r in inspect_ledger(run).records]
    assert prefix[-1] == "artifact_copy_finished" and "promotion_committed" not in prefix
    path = run / "artifacts/candidates/a1/skills/SKILL.md"
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(ArtifactCorruptionError) as info:
        resume(run)
    assert info.value.code == "artifact_digest_mismatch"
    with resume(run) as store:
        assert store.stopped == "artifact_corrupted"
        assert events(store)[-1] == "store_stopped"
        assert "promotion_committed" not in events(store)
        assert store.last_recovery.actions == ()


# -- a stopped store still closes its open operations --------------------------


def test_a_stopped_store_closes_open_operations_and_repairs_nothing_else(tmp_path):
    run = tmp_path / "run"
    with create(run, seed(tmp_path)) as store:
        # A durable open operation under a permanent stop: the ledger admits the
        # reserved terminal afterwards, so recovery must still write it.
        store.ledger.append("workspace_opened", dict(attempt_id="a1", parent_id="v0"), "op1")
        store.ledger.append("store_stopped", dict(code="ledger_limit_exceeded"))
    with resume(run) as store:
        assert store.stopped == "ledger_limit_exceeded"
        assert store.last_recovery.actions == ()
        assert events(store)[-1] == "operation_interrupted"
        assert events(store).count("operation_interrupted") == 1
        assert "candidate_abandoned" not in events(store)
        assert store.ledger.state.pending == {}
        after = len(events(store))
    with resume(run) as store:  # repeated recovery appends nothing
        assert len(events(store)) == after
        assert store.last_recovery.actions == ()
