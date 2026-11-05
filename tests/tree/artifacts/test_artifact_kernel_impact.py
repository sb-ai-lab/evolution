"""Artifact integration with execution, scoring, public imports and staging cleanup."""

import re
from types import SimpleNamespace

import pytest

import evolution.tree as tree
from evolution import __version__
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1, scan_tree
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.execution.candidate_integrity import ArtifactError, artifact_digest
from evolution.tree.execution.interaction import (
    CandidateIntegrityError,
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
from evolution.tree.execution.journal_identity import (
    IdentityError,
    RunIdentityV1,
    scorer_binding,
    scoring_input_digest,
)
from evolution.tree.execution.journal_scoring import durable_score
from evolution.tree.execution.rollout_scoring import ScoringError
from tests.tree.artifacts.test_artifact_policy import seed
from tests.tree.execution.test_journal_identity import cases, identity_payload

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
VR = lambda _: __version__  # noqa: E731
# A metric name is an identifier, so a secret can collide with one.
SECRET_METRIC = "canary_metric_7f3a"


def store_view(tmp_path):
    """The artifact Champion as an execution view: no `artifact_paths`, an artifact-set verifier."""
    store = ArtifactStore.create(
        tmp_path / "run2", seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    )
    try:
        return store.champion().candidate_view()
    finally:
        store.close()


def scoring_setup(tmp_path, secrets=(), candidate=None):
    if candidate is None:
        root = tmp_path / "candidate"
        root.mkdir()
        (root / "skill.txt").write_text("candidate")
        candidate = CandidateView(root, artifact_digest(root, ("skill.txt",)), ("skill.txt",))
    p = identity_payload()
    p["candidate_digest"] = candidate.digest
    journal = Journal.create(
        tmp_path / "run",
        "run",
        RunIdentityV1(p),
        cases(),
        version_resolver=VR,
        secrets=secrets,
    )
    key = dict(case_id="c", sample_id="s1", candidate_digest=candidate.digest)
    journal.append("rollout_started", key, "rollout")
    journal.append("rollout_finished", key | {"outcome": "completed"}, "rollout")
    target = TargetIdentity(
        SemanticIdentity(**p["target"]), InstanceProvenance("boot", candidate.digest), frozenset()
    )
    trajectory = Trajectory(
        "c", "s1", candidate.digest, "envdigest", History(), "completed", Provenance(target, 42, 42)
    )
    return journal, candidate, trajectory


@pytest.mark.asyncio
async def test_secret_metric_name_fails_scoring_and_keeps_the_writer(tmp_path):
    """The callee's summary is validated against the writer's own secrets."""
    journal, candidate, trajectory = scoring_setup(tmp_path, secrets=(SECRET_METRIC,))

    async def score(*args):
        return ScoreResult({SECRET_METRIC: 1.0})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        assert journal.secrets == (SECRET_METRIC,)
        with pytest.raises(ScoringError):
            await durable_score(journal, scorer, cases()[0], trajectory, candidate)
        # The writer is healthy: it recorded the terminal and still admits work.
        assert journal.snapshot().records[-1].event == "scoring_failed"
        assert journal.snapshot().records[-1].payload["failure"] == "scorer_error"
        assert SECRET_METRIC not in (tmp_path / "run/events.jsonl").read_text()
        # `snapshot` checks the writer: an invalidated one would fault here.
        assert journal.snapshot().sequence == 5
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_a_healthy_metric_name_still_scores(tmp_path):
    journal, candidate, trajectory = scoring_setup(tmp_path, secrets=(SECRET_METRIC,))

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        result = await durable_score(journal, scorer, cases()[0], trajectory, candidate)
        assert result.metrics == {"accuracy": 1}
        assert journal.snapshot().records[-1].event == "scoring_finished"
    finally:
        journal.close()


def test_tree_exports_the_journal_and_artifact_boundary():
    """The boundary exceptions and the S2 surface are importable."""
    expected = {
        "ArtifactAdmissionRefused",
        "ArtifactCapacityError",
        "ArtifactCorruptionError",
        "ArtifactError",
        "ArtifactPolicyV1",
        "ArtifactReference",
        "ArtifactRef",
        "ArtifactStore",
        "ArtifactStoreStopped",
        "ArtifactUnsafeError",
        "CapacityEstimate",
        "ExpectedAttestation",
        "JournalAdmissionRefused",
        "JournalBoundaryError",
        "JournalFault",
        "JournalRecoveryRequired",
        "LineageProjection",
        "RecoveryReport",
        "SelectionDecision",
        "project_lineage",
    }
    assert expected <= set(tree.__all__)
    for name in tree.__all__:
        assert getattr(tree, name, None) is not None, name


def test_staging_cleanup_finishes_its_loop_before_reporting_failure(tmp_path, monkeypatch):
    from evolution.tree.artifacts import artifacts as module

    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ):
        pass
    staging = run / "artifacts/.staging"
    for name in ("a", "b"):
        (staging / name).mkdir()
    real = module.remove_tree
    seen = []

    def flaky(root):
        seen.append(root.name)
        if len(seen) == 1:
            raise OSError(5, "io")
        real(root)

    monkeypatch.setattr(module, "remove_tree", flaky)
    with pytest.raises(ArtifactError) as info:
        ArtifactStore.resume(run, POLICY, version_resolver=VR)
    assert info.value.code == "io_failure"
    # Every staged tree was attempted; only the failing one is left behind.
    assert sorted(seen) == ["a", "b"]
    assert [p.name for p in staging.iterdir()] == [seen[0]]


def test_scoring_input_digest_accepts_the_verifier_mode(tmp_path):
    """A view that names no path still binds a scoring input."""
    view = store_view(tmp_path)
    _, _, trajectory = scoring_setup(tmp_path, candidate=view)
    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=None)
    binding = scorer_binding(scorer)
    scoring_view = trajectory.scoring_view()
    digest = scoring_input_digest(cases()[0], scoring_view, view, binding)
    assert re.fullmatch("[a-f0-9]{64}", digest)
    assert scoring_input_digest(cases()[0], scoring_view, view, binding) == digest
    # The verifier is what reads the tree, so tampering with it is caught here.
    (view.root / "skills/SKILL.md").chmod(0o644)
    (view.root / "skills/SKILL.md").write_bytes(b"tampered")
    with pytest.raises(CandidateIntegrityError):
        scoring_input_digest(cases()[0], scoring_view, view, binding)


def test_scoring_input_digest_still_refuses_empty_paths_without_a_verifier(tmp_path):
    view = store_view(tmp_path)
    _, _, trajectory = scoring_setup(tmp_path, candidate=view)
    # A view cannot be built this way; only a stand-in can carry no strategy at all.
    pathless = SimpleNamespace(artifact_paths=(), digest=view.digest, verify=lambda: None)
    with pytest.raises(IdentityError) as info:
        scoring_input_digest(
            cases()[0],
            trajectory.scoring_view(),
            pathless,
            scorer_binding(SimpleNamespace(identity=ScorerIdentity("judge", "scorer"))),
        )
    assert str(info.value) == "invalid_artifact_path"


@pytest.mark.asyncio
async def test_durable_score_accepts_a_store_candidate_view(tmp_path):
    view = store_view(tmp_path)
    journal, candidate, trajectory = scoring_setup(tmp_path, candidate=view)

    async def score(*args):
        return ScoreResult({"accuracy": 1})

    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=score)
    try:
        result = await durable_score(journal, scorer, cases()[0], trajectory, candidate)
        assert result.metrics == {"accuracy": 1}
        assert journal.snapshot().records[-1].event == "scoring_finished"
    finally:
        journal.close()


def test_candidate_views_of_one_ref_compare_equal(tmp_path):
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        ref = store.champion()
        # The strategy is an implementation detail of the view, not part of identity.
        assert ref.candidate_view() == ref.candidate_view()
        assert "verifier" not in repr(ref.candidate_view())


def test_candidate_view_verifies_the_dependency_digest_too(tmp_path):
    """A tampered `uv.lock` leaves the artifact-set digest intact, so only the
    view's own dependency check can stop execution from scoring the wrong tree.
    """
    run = tmp_path / "run"
    with ArtifactStore.create(
        run, seed(tmp_path), POLICY, parallel_attempts=1, version_resolver=VR
    ) as store:
        ref = store.champion()
        view = ref.candidate_view()
        view.verify()
        lock = ref.root / "uv.lock"
        lock.chmod(0o644)
        lock.write_bytes(b"tampered")
        assert scan_tree(ref.root, POLICY).candidate_digest == ref.digest
        with pytest.raises(CandidateIntegrityError):
            view.verify()


def test_candidate_view_rejects_an_excluded_entry_added_to_a_published_root(tmp_path):
    """The verifier is `ArtifactRef.verify`, so execution sees what a bare `scan_tree`
    prunes: an added `.env` reproduces both digests and is still not the copy.
    """
    view = store_view(tmp_path)
    view.verify()
    view.root.chmod(0o700)
    (view.root / ".env").write_bytes(b"SECRET=1")
    assert scan_tree(view.root, POLICY).candidate_digest == view.digest
    with pytest.raises(CandidateIntegrityError):
        view.verify()
    _, _, trajectory = scoring_setup(tmp_path, candidate=view)
    scorer = SimpleNamespace(identity=ScorerIdentity("judge", "scorer"), score=None)
    with pytest.raises(CandidateIntegrityError):
        scoring_input_digest(cases()[0], trajectory.scoring_view(), view, scorer_binding(scorer))


@pytest.mark.parametrize(
    "patterns",
    [("**/*.txt",), ("nested/*.txt", "a.txt"), ("a.txt", "**/*.txt", "a.txt")],
)
def test_selected_file_digest_preserves_framing_order_and_hidden_exclusion(tmp_path, patterns):
    (tmp_path / "nested").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "a.txt").write_bytes(b"a\0b")
    (tmp_path / "nested/z.txt").write_bytes("π\n".encode())
    (tmp_path / ".hidden.txt").write_text("excluded")
    (tmp_path / ".hidden/secret.txt").write_text("excluded")

    assert artifact_digest(tmp_path, patterns) == (
        "e4043c91d6c8a233ae888f80d35c11f8c933e2399fb3df95dbe8e762d711af47"
    )
