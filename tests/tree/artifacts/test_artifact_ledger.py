"""Owned S2 ledger on disk: creation, resume proofs, layout guard and the bound."""

import pytest

from evolution import __version__
from evolution.tree.artifacts.artifact_errors import ArtifactCorruptionError, ArtifactStoreStopped
from evolution.tree.artifacts.artifact_ledger import ArtifactLedger
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_schema import ArtifactStoreIdentityV1
from evolution.tree.storage.journal_errors import JournalFault

POLICY = ArtifactPolicyV1((), (), ("skills/**",))
IDENT = ArtifactStoreIdentityV1(POLICY, "a" * 64, "b" * 64)
VR = lambda _: __version__  # noqa: E731


def test_create_resume_policy_and_version_checks(tmp_path):
    root = tmp_path / "run"
    ledger = ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    assert (root / "artifacts.jsonl").exists() and (root / ".artifacts.lock").exists()
    ledger.close()
    with pytest.raises(ArtifactCorruptionError) as info:
        ArtifactLedger.resume(
            root, ArtifactPolicyV1((), (), ("other/**",)), version_resolver=VR, secrets=()
        )
    assert info.value.code == "policy_mismatch"
    with pytest.raises(JournalFault):
        ArtifactLedger.resume(root, POLICY, version_resolver=lambda _: "9.9.9", secrets=())
    ledger = ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
    assert ledger.state.identity["seed_candidate_digest"] == "a" * 64
    ledger.close()
    assert ArtifactLedger.inspect(root).sequence == 1


def publish_v0(ledger):
    """The seed champion: no workspace may be opened before it is published."""
    ledger.append(
        "artifact_copy_started",
        dict(
            kind="champion",
            artifact_id="v0",
            parent_id=None,
            source="seed",
            candidate_digest="a" * 64,
            dependency_digest="b" * 64,
            file_count=2,
            byte_count=9,
        ),
        "c0",
    )
    ledger.append("artifact_copy_finished", dict(latency_ms=3), "c0")


def test_bound_stops_permanently(tmp_path):
    ledger = ArtifactLedger.create(
        tmp_path / "run", IDENT, version_resolver=VR, secrets=(), max_ledger_bytes=4000
    )
    publish_v0(ledger)
    with pytest.raises(ArtifactStoreStopped):
        for i in range(100):
            ledger.append("workspace_opened", dict(attempt_id=f"a{i}", parent_id="v0"), f"w{i}")
    assert ledger.state.stopped == "ledger_limit_exceeded"
    ledger.close()
    ledger = ArtifactLedger.resume(tmp_path / "run", POLICY, version_resolver=VR, secrets=())
    with pytest.raises(ArtifactStoreStopped):
        ledger.append("workspace_opened", dict(attempt_id="z", parent_id="v0"), "wz")
    ledger.close()


def test_create_owns_a_run_root_that_already_holds_measurements(tmp_path):
    root = tmp_path / "run"
    (root / "measurements" / "m1").mkdir(parents=True)
    (root / "campaign.jsonl").write_text("{}\n")
    ledger = ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    try:
        assert (root / "measurements" / "m1").is_dir()
        assert ledger.run_id and ledger.state.sequence == 1
        assert ledger.snapshot().records[0].payload["producing_version"] == __version__
    finally:
        ledger.close()


def test_create_refuses_an_existing_store_and_both_refuse_old_layouts(tmp_path):
    root = tmp_path / "run"
    ledger = ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    ledger.close()
    with pytest.raises(JournalFault):
        ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    for name in ("versions", "rejected", "candidates"):
        (root / name).mkdir()
        with pytest.raises(JournalFault):
            ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
        (root / name).rmdir()
    ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=()).close()
    fresh = tmp_path / "fresh"
    (fresh / "artifacts").mkdir(parents=True)
    with pytest.raises(JournalFault):
        ArtifactLedger.create(fresh, IDENT, version_resolver=VR, secrets=())


def test_resume_refuses_a_tampered_identity_digest(tmp_path):
    root = tmp_path / "run"
    ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=()).close()
    path = root / "artifacts.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"' + b"a" * 64 + b'"', b'"' + b"c" * 64 + b'"'))
    with pytest.raises(JournalFault):
        ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())


def test_ledger_appends_a_copy_pair_and_survives_reopen(tmp_path):
    root = tmp_path / "run"
    ledger = ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    publish_v0(ledger)
    run_id = ledger.run_id
    ledger.close()
    ledger = ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
    try:
        assert ledger.run_id == run_id
        assert ledger.state.champion_id == "v0"
        assert ledger.state.published["champion/v0"].payload["file_count"] == 2
        assert ledger.snapshot().consistency == "durable"
    finally:
        ledger.close()
    assert ArtifactLedger.inspect(root).records[-1].event == "artifact_copy_finished"


def test_ledger_refuses_secrets_and_an_unowned_second_writer(tmp_path):
    root = tmp_path / "run"
    with pytest.raises(ValueError):
        ArtifactLedger.create(
            root,
            ArtifactStoreIdentityV1(POLICY, "a" * 64, "b" * 64),
            version_resolver=VR,
            secrets=("a" * 64,),
        )
    assert not root.exists()
    ledger = ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    try:
        with pytest.raises(JournalFault) as info:
            ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
        assert info.value.code == "journal_busy"
    finally:
        ledger.close()


def test_a_stopped_ledger_refuses_new_work_without_poisoning_the_writer(tmp_path):
    root = tmp_path / "run"
    ledger = ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=())
    try:
        publish_v0(ledger)
        ledger.append("workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1")
        ledger.append("store_stopped", dict(code="artifact_corrupted"))
        refused = (
            ("workspace_opened", dict(attempt_id="a2", parent_id="v0"), "w2"),
            ("candidate_abandoned", dict(attempt_id="a1", reason="caller_abandoned"), None),
            ("store_stopped", dict(code="ledger_limit_exceeded"), None),
            ("workspace_ready", {}, "w9"),  # a terminal of no open operation
        )
        for event, payload, operation_id in refused:
            with pytest.raises(ArtifactStoreStopped) as info:
                ledger.append(event, payload, operation_id)
            assert info.value.code == "artifact_corrupted"
        # The writer is still healthy: the reserved terminal is admitted.
        assert ledger.append("workspace_ready", {}, "w1") == 6
        assert ledger.state.workspaces["a1"] == "ready" and not ledger.state.pending
    finally:
        ledger.close()
    assert ArtifactLedger.inspect(root).sequence == 6


def test_rebase_moves_the_record_resume_proves_its_version_against(tmp_path):
    root = tmp_path / "run"
    ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=()).close()
    with pytest.raises(JournalFault):
        ArtifactLedger.resume(root, POLICY, version_resolver=lambda _: "0.3.0a0", secrets=())

    ledger = ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
    ledger.rebase(
        IDENT,
        campaign_rebase="c" * 64,
        authorization_id="0" * 64,
        producing_version=__version__,
    )
    ledger.close()

    latest = ArtifactLedger.latest_store_record(ArtifactLedger.inspect(root))
    assert latest.event == "store_rebased"
    assert latest.payload["campaign_rebase"] == "c" * 64
    assert latest.payload["authorization_id"] == "0" * 64
    assert latest.payload["producing_version"] == __version__
    # The creation record no longer decides: an older version on it is ignored.
    _rewrite_producing_version(root, "0.3.0a0")
    ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=()).close()


def test_rebase_stamps_the_version_the_caller_declares_not_the_running_one(tmp_path):
    """The ledger half of a rebase carries the campaign record's own version.

    A resume that finds `identity_rebased` without its `store_rebased` completes
    the owed record however many versions later, and it must not restamp it with
    whatever package is running now (ADR-0015).
    """

    root = tmp_path / "run"
    ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=()).close()
    ledger = ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
    ledger.rebase(
        IDENT,
        campaign_rebase="c" * 64,
        authorization_id="0" * 64,
        producing_version="0.3.0a11",
    )
    ledger.close()

    latest = ArtifactLedger.latest_store_record(ArtifactLedger.inspect(root))
    assert latest.payload["producing_version"] == "0.3.0a11"
    assert latest.payload["producing_version"] != __version__
    # And that stamp is what a later resume proves itself against: the running
    # version is no longer the record's, so it needs the campaign's admission.
    with pytest.raises(JournalFault):
        ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())
    ArtifactLedger.resume(
        root, POLICY, version_resolver=VR, secrets=(), admitted_versions={"0.3.0a11"}
    ).close()


def test_resume_admits_an_older_producing_version_named_in_admitted_versions(tmp_path):
    root = tmp_path / "run"
    ArtifactLedger.create(root, IDENT, version_resolver=VR, secrets=()).close()
    _rewrite_producing_version(root, "0.3.0a0")

    with pytest.raises(JournalFault):
        ArtifactLedger.resume(root, POLICY, version_resolver=VR, secrets=())

    ledger = ArtifactLedger.resume(
        root, POLICY, version_resolver=VR, secrets=(), admitted_versions={"0.3.0a0"}
    )
    assert ledger.state.identity["seed_candidate_digest"] == "a" * 64
    ledger.close()


def _rewrite_producing_version(root, version):
    """Rewrite only the `store_created` record's `producing_version` in place."""
    ledger_file = root / "artifacts.jsonl"
    lines = ledger_file.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(__version__.encode(), version.encode())
    ledger_file.write_bytes(b"".join(lines))
