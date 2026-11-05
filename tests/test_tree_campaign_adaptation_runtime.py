"""Adaptive assembly through real campaign journals, measurements and artifacts."""

import pytest

pytestmark = pytest.mark.slow


def test_recover_editor_workspace_refuses_wrong_parent_and_published_attempt(tmp_path):
    from evolution.tree.artifacts.artifact_errors import ArtifactAdmissionRefused
    from tests.tree.artifacts.test_artifact_store import create

    with create(tmp_path) as store:
        parent = store.champion()
        workspace = store.materialize_candidate("retry")
        (workspace / "extra.txt").write_text("interrupted output")
        before = len(store.ledger.snapshot().records)
        with pytest.raises(ArtifactAdmissionRefused, match="parent_changed"):
            store.restore_candidate_workspace(
                "retry", parent_id=parent.artifact_id, parent_digest="0" * 64
            )
        restored = store.restore_candidate_workspace(
            "retry", parent_id=parent.artifact_id, parent_digest=parent.digest
        )
        assert not (restored / "extra.txt").exists()
        assert len(store.ledger.snapshot().records) == before
        store.abandon("retry")
        with pytest.raises(ArtifactAdmissionRefused, match="workspace_not_ready"):
            store.restore_candidate_workspace(
                "retry", parent_id=parent.artifact_id, parent_digest=parent.digest
            )
        published = store.materialize_candidate("published")
        (published / "skills" / "SKILL.md").write_text("updated skill")
        store.seal("published")
        with pytest.raises(ArtifactAdmissionRefused, match="workspace_not_ready"):
            store.restore_candidate_workspace(
                "published", parent_id=parent.artifact_id, parent_digest=parent.digest
            )


@pytest.mark.parametrize(
    "failure_errno, error_code", [(5, "io_failure"), (28, "storage_capacity_exhausted")]
)
@pytest.mark.parametrize("boundary", ["stage_copy", "fsync_tree"])
def test_editor_workspace_recovery_maps_storage_errors(
    tmp_path, monkeypatch, failure_errno, error_code, boundary
):
    from evolution.tree.artifacts import artifacts
    from evolution.tree.execution.candidate_integrity import ArtifactError
    from tests.tree.artifacts.test_artifact_store import create

    with create(tmp_path) as store:
        parent = store.champion()
        store.materialize_candidate("retry")
        before = tuple(store.ledger.snapshot().records)

        def fail(*args, **kwargs):
            raise OSError(failure_errno, "injected recovery write failure")

        monkeypatch.setattr(artifacts, boundary, fail)
        with pytest.raises(ArtifactError, match=error_code):
            store.restore_candidate_workspace(
                "retry", parent_id=parent.artifact_id, parent_digest=parent.digest
            )
        parent.verify()
        assert tuple(store.ledger.snapshot().records) == before
        assert store.ledger.state.workspaces["retry"] == "ready"
        assert not list((tmp_path / "run" / "artifacts" / ".staging").iterdir())
