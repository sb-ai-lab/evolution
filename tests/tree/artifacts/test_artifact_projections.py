import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_ledger import ArtifactLedger
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_projections import (
    ArtifactReference,
    LineageProjection,
    project_lineage,
)
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.artifacts.artifacts import ArtifactStore, SelectionDecision
from tests.tree.artifacts.test_artifact_policy import seed, write

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
VR = lambda _: __version__  # noqa: E731


def decision(store, attempt, ref, round_id=1, locator="c#1"):
    return SelectionDecision(
        round_id,
        attempt,
        ref.digest,
        ref.dependency_digest,
        dict(
            version=1,
            kind="selection_decided",
            run_id=store.run_id,
            locator=locator,
            content_digest="f" * 64,
        ),
    )


def test_projection_reports_only_observed_state(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        store.materialize_candidate("a1")
        write(run / "artifacts/.workspaces/a1", "skills/SKILL.md", b"better")
        ref = store.seal("a1")
        store.materialize_candidate("a2")
        view = ArtifactStore.inspect(run)
        assert view.consistency == "observed" and view.store_state == "active"
        assert view.champion.version_id == "v0"
        assert view.champion.reference.locator == "artifacts/champions/v0"
        assert view.champion.attempt_id is None and view.champion.round_id is None
        assert view.champion.decision_ref is None
        states = {c.attempt_id: c.state for c in view.candidates}
        assert states == {"a1": "sealed", "a2": "editing"}
        store.promote(decision(store, "a1", ref))
        view = ArtifactStore.inspect(run, verify=True)
        assert [c.version_id for c in view.champions] == ["v0", "v1"]
        assert view.champion.version_id == "v1" and view.champion.decision_ref["locator"] == "c#1"
        assert view.champion.attempt_id == "a1" and view.champion.round_id == 1
        assert {c.attempt_id: c.state for c in view.candidates}["a1"] == "promoted"
        assert all(c.corruption is None for c in view.candidates)
        assert all(c.corruption is None for c in view.champions)
        (run / "artifacts/candidates/a1/skills/SKILL.md").chmod(0o644)
        (run / "artifacts/candidates/a1/skills/SKILL.md").write_bytes(b"x")
        view = ArtifactStore.inspect(run, verify=True)
        assert {c.attempt_id: c.corruption for c in view.candidates}["a1"] == (
            "artifact_digest_mismatch"
        )
        # The champion copied from that Candidate is a separate frozen tree.
        assert all(c.corruption is None for c in view.champions)


def test_projection_before_v0_has_no_champion(tmp_path):
    ledger = ArtifactLedger.create(
        tmp_path / "run",
        ArtifactStoreIdentityV1(POLICY, "a" * 64, "b" * 64),
        version_resolver=VR,
        secrets=(),
    )
    ledger.close()
    view = ArtifactStore.inspect(tmp_path / "run")
    assert view.store_state == "initializing" and view.champion is None
    assert view.champions == () and view.candidates == ()
    assert view.stopped_code is None and view.sequence == 1


def test_projection_carries_snapshot_provenance_and_no_paths(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        run_id = store.run_id
    snapshot = ArtifactLedger.inspect(run)
    view = project_lineage(snapshot)
    assert isinstance(view, LineageProjection)
    assert view.run_id == run_id and view.consistency == snapshot.consistency
    assert view.sequence == snapshot.sequence and view.prefix_digest == snapshot.prefix_digest
    for value in vars(view).values():
        assert not isinstance(value, type(run))
    assert view.champion.reference.run_id == run_id


def test_projection_reports_every_candidate_state(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=4, version_resolver=VR
    ) as store:
        store.materialize_candidate("kept")
        store.materialize_candidate("gone")
        store.abandon("gone")
        store.materialize_candidate("sealed")
        write(run / "artifacts/.workspaces/sealed", "skills/SKILL.md", b"sealed")
        store.seal("sealed")
        view = ArtifactStore.inspect(run)
    states = {c.attempt_id: c.state for c in view.candidates}
    assert states == {"kept": "editing", "gone": "abandoned", "sealed": "sealed"}
    entries = {c.attempt_id: c for c in view.candidates}
    assert entries["kept"].reference is None and entries["kept"].candidate_digest is None
    assert entries["gone"].reference is None
    assert entries["sealed"].reference.locator == "artifacts/candidates/sealed"
    assert entries["sealed"].candidate_digest == entries["sealed"].reference.content_digest
    assert all(c.parent_id == "v0" for c in view.candidates)
    # Ledger order, not dictionary order.
    assert [c.attempt_id for c in view.candidates] == ["kept", "gone", "sealed"]


def test_projection_reports_a_failed_workspace_copy(tmp_path, monkeypatch):
    from evolution.tree.artifacts import artifacts as module

    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        monkeypatch.setattr(
            module, "stage_copy", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io"))
        )
        with pytest.raises(module.ArtifactError):
            store.materialize_candidate("broken")
        view = ArtifactStore.inspect(run)
    assert {c.attempt_id: c.state for c in view.candidates} == {"broken": "failed"}


def test_projection_reports_a_stopped_store(tmp_path):
    from evolution.tree.artifacts.artifact_copy import remove_tree
    from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError

    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        remove_tree(run / "artifacts/champions/v0")
        with pytest.raises(ArtifactCorruptionError):
            store.champion()
        view = ArtifactStore.inspect(run)
    assert view.store_state == "stopped" and view.stopped_code == "artifact_corrupted"
    # A stopped store still names the champion its ledger published.
    assert view.champion.version_id == "v0"
    view = ArtifactStore.inspect(run, verify=True)
    assert view.champion.corruption == "artifact_missing"


def test_verification_reports_an_unsafe_published_copy(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        store.materialize_candidate("a1")
        write(run / "artifacts/.workspaces/a1", "skills/SKILL.md", b"better")
        store.seal("a1")
    copy = run / "artifacts/candidates/a1"
    copy.rename(run / "artifacts/candidates/moved")
    (run / "artifacts/candidates/a1").symlink_to(run / "artifacts/candidates/moved")
    view = ArtifactStore.inspect(run, verify=True)
    assert {c.attempt_id: c.corruption for c in view.candidates}["a1"] == "artifact_unsafe"


def test_verification_reports_an_unreadable_published_copy(tmp_path, monkeypatch):
    """Inspection must not invent a corruption code for a transient read failure:
    `unreadable` says the copy could not be read, not that its bytes are wrong.
    """
    import errno

    from evolution.tree.artifacts import artifact_projections as module

    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ):
        pass
    champion_root = run / "artifacts/champions/v0"
    real = module.scan_tree

    def flaky(root, policy):
        if root == champion_root:
            raise OSError(errno.EIO, "unreadable")
        return real(root, policy)

    monkeypatch.setattr(module, "scan_tree", flaky)
    view = ArtifactStore.inspect(run, verify=True)
    assert view.champion.corruption == "unreadable"
    monkeypatch.setattr(module, "scan_tree", real)
    assert ArtifactStore.inspect(run, verify=True).champion.corruption is None


def test_inspection_never_writes(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        store.materialize_candidate("a1")
        before = sorted((p, p.stat().st_mtime_ns, p.stat().st_size) for p in run.rglob("*"))
        ArtifactStore.inspect(run, verify=True)
        # A live store still owns its lease: inspection took none and appended nothing.
        assert store.ledger.snapshot().sequence == 5
    assert sorted((p, p.stat().st_mtime_ns, p.stat().st_size) for p in run.rglob("*")) == before


def test_verification_requires_a_run_root(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ):
        pass
    with pytest.raises(ValueError):
        project_lineage(ArtifactLedger.inspect(run), verify=True)


@pytest.mark.parametrize(
    "locator",
    [
        "/artifacts/champions/v0",
        "artifacts/champions/../../etc",
        "artifacts/workspaces/a1",
        "champions/v0",
        "artifacts/champions/",
        "artifacts/candidates/a/b",
    ],
)
def test_artifact_reference_refuses_a_locator_out_of_the_run(locator):
    with pytest.raises(ValueError):
        ArtifactReference(1, "artifact", "run", locator, "a" * 64, "b" * 64)


@pytest.mark.parametrize(
    "field,value",
    [("version", 2), ("kind", "journal_record"), ("content_digest", "nope"), ("run_id", "a/b")],
)
def test_artifact_reference_refuses_a_foreign_shape(field, value):
    kw = dict(
        version=1,
        kind="artifact",
        run_id="run",
        locator="artifacts/champions/v0",
        content_digest="a" * 64,
        dependency_digest="b" * 64,
    )
    kw[field] = value
    with pytest.raises(ValueError):
        ArtifactReference(**kw)


def test_candidate_view_uses_artifact_digest_strategy(tmp_path):
    from evolution.tree.execution.interaction import CandidateIntegrityError

    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        view = store.champion().candidate_view()
        assert view.artifact_paths == ()
        view.verify()
        (view.root / "skills/SKILL.md").chmod(0o644)
        (view.root / "skills/SKILL.md").write_bytes(b"tampered")
        with pytest.raises(CandidateIntegrityError):
            view.verify()


def test_candidate_view_reports_an_unreadable_root(tmp_path):
    from evolution.tree.artifacts.artifact_copy import remove_tree
    from evolution.tree.execution.interaction import CandidateIntegrityError

    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        view = store.champion().candidate_view()
        remove_tree(view.root)
        with pytest.raises(CandidateIntegrityError):
            view.verify()


def test_verification_reports_an_excluded_entry_inside_a_published_copy(tmp_path):
    """A published copy never contains an excluded entry. A stray `.env`
    reproduces both digests, so only the on-disk entry set sees it; inspection reports
    it per copy and never raises, because a corrupt non-winner stops no store.
    """
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        store.materialize_candidate("a1")
        write(run / "artifacts/.workspaces/a1", "skills/SKILL.md", b"better")
        store.seal("a1")
    copy = run / "artifacts/candidates/a1"
    copy.chmod(0o700)
    (copy / ".env").write_bytes(b"SECRET=1")
    view = ArtifactStore.inspect(run, verify=True)
    assert {c.attempt_id: c.corruption for c in view.candidates}["a1"] == "artifact_unsafe"
    assert view.champion.corruption is None
