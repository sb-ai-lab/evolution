"""S2 artifact ledger vocabulary: identity, closed payloads and lifecycle admission."""

import pytest

from evolution.tree.artifacts.artifact_ledger import ArtifactLedger
from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.artifacts.artifact_schema import (
    ARTIFACT_FAMILY,
    ArtifactLifecycle,
    ArtifactStoreIdentityV1,
)
from evolution.tree.storage.append_log import Snapshot
from evolution.tree.storage.envelope import Envelope, SchemaError

POLICY = ArtifactPolicyV1((), ("uv.lock",), ("skills/**",))
IDENT = ArtifactStoreIdentityV1(POLICY, "a" * 64, "b" * 64)


def env(seq, event, payload, op=None):
    return Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=seq,
            timestamp="2026-01-01T00:00:00Z",
            event=event,
            operation_id=op,
            payload=payload,
        ),
        family=ARTIFACT_FAMILY,
    )


def created():
    return env(
        1,
        "store_created",
        dict(
            identity=IDENT.to_payload(), identity_digest=IDENT.digest, producing_version="0.2.0rc2"
        ),
    )


def start(seq, op, kind, aid, parent, source, cd="a" * 64, dd="b" * 64):
    return env(
        seq,
        "artifact_copy_started",
        dict(
            kind=kind,
            artifact_id=aid,
            parent_id=parent,
            source=source,
            candidate_digest=cd,
            dependency_digest=dd,
            file_count=1,
            byte_count=1,
        ),
        op,
    )


def test_identity_roundtrip_and_digest():
    assert ArtifactStoreIdentityV1.from_payload(IDENT.to_payload()) == IDENT
    assert IDENT.to_payload()["profile"] == "artifact_store"
    assert IDENT.to_payload()["policy_digest"] == POLICY.digest


def test_family_rejects_unknown_and_open_payloads():
    with pytest.raises(SchemaError):
        env(2, "run_created", {}, None)
    with pytest.raises(SchemaError):
        env(2, "artifact_copy_finished", dict(latency_ms=1, extra=1), "op")
    with pytest.raises(SchemaError):
        env(2, "store_stopped", dict(code="bogus"))


def test_lifecycle_v0_must_match_seed_identity_and_publishes():
    life = ArtifactLifecycle()
    life.accept(created())
    with pytest.raises(SchemaError):
        life.accept(start(2, "op1", "champion", "v0", None, "seed", cd="c" * 64))
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    assert life.champion_id is None
    life.accept(env(3, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    assert life.champion_id == "v0" and "champion/v0" in life.published


def test_lifecycle_workspace_seal_and_promotion_order():
    life = ArtifactLifecycle()
    life.accept(created())
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    life.accept(env(3, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    with pytest.raises(SchemaError):  # no ready workspace
        life.accept(start(4, "op2", "candidate", "a1", "v0", "workspace"))
    life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    life.accept(env(5, "workspace_ready", {}, "w1"))
    life.accept(start(6, "op2", "candidate", "a1", "v0", "workspace", cd="d" * 64))
    life.accept(env(7, "artifact_copy_finished", dict(latency_ms=5), "op2"))
    assert life.workspaces["a1"] == "sealed"
    with pytest.raises(SchemaError):  # digest mismatch against published candidate
        life.accept(
            env(
                8,
                "promotion_pending",
                dict(
                    round_id=1,
                    attempt_id="a1",
                    version_id="v1",
                    candidate_digest="e" * 64,
                    dependency_digest="b" * 64,
                    decision_ref=ref(),
                ),
            )
        )
    life.accept(
        env(
            8,
            "promotion_pending",
            dict(
                round_id=1,
                attempt_id="a1",
                version_id="v1",
                candidate_digest="d" * 64,
                dependency_digest="b" * 64,
                decision_ref=ref(),
            ),
        )
    )
    with pytest.raises(SchemaError):  # champion copy not published yet
        life.accept(
            env(
                9,
                "promotion_committed",
                dict(
                    round_id=1,
                    version_id="v1",
                    attempt_id="a1",
                    candidate_digest="d" * 64,
                    dependency_digest="b" * 64,
                ),
            )
        )
    life.accept(start(9, "op3", "champion", "v1", "a1", "candidate", cd="d" * 64))
    life.accept(env(10, "artifact_copy_finished", dict(latency_ms=5), "op3"))
    life.accept(
        env(
            11,
            "promotion_committed",
            dict(
                round_id=1,
                version_id="v1",
                attempt_id="a1",
                candidate_digest="d" * 64,
                dependency_digest="b" * 64,
            ),
        )
    )
    assert life.champion_id == "v1" and life.reserved_records == 1


def test_lifecycle_stop_blocks_starts():
    life = ArtifactLifecycle()
    life.accept(created())
    life.accept(env(2, "store_stopped", dict(code="artifact_corrupted")))
    assert life.reserved_records == 0
    with pytest.raises(SchemaError):
        life.accept(start(3, "op1", "champion", "v0", None, "seed"))


def ref():
    return dict(
        version=1,
        kind="selection_decided",
        run_id="run",
        locator="campaign.jsonl#seq=7",
        content_digest="f" * 64,
    )


def test_identity_rejects_tampered_and_foreign_payloads():
    payload = IDENT.to_payload()
    with pytest.raises(ValueError):
        ArtifactStoreIdentityV1.from_payload(payload | dict(policy_digest="c" * 64))
    with pytest.raises(ValueError):
        ArtifactStoreIdentityV1.from_payload(payload | dict(profile="run"))
    with pytest.raises(ValueError):
        ArtifactStoreIdentityV1.from_payload(payload | dict(seed_candidate_digest="zz"))
    with pytest.raises(ValueError):
        ArtifactStoreIdentityV1.from_payload(payload | dict(extra=1))
    assert IDENT.digest != ArtifactStoreIdentityV1(POLICY, "c" * 64, "b" * 64).digest


def test_family_admits_no_s1_event_and_keeps_operation_ids_closed():
    for event, payload in (
        ("rollout_started", dict(case_id="c", sample_id="s", candidate_digest="d")),
        ("target_send_started", dict(case_id="c", sample_id="s", candidate_digest="d")),
        ("journal_dispatch_stopped", dict(code="journal_limit_exceeded")),
    ):
        with pytest.raises(SchemaError):
            env(2, event, payload, "op")
    with pytest.raises(SchemaError):  # operationless events carry no operation_id
        env(
            2,
            "promotion_committed",
            dict(
                round_id=1,
                version_id="v1",
                attempt_id="a1",
                candidate_digest="d" * 64,
                dependency_digest="b" * 64,
            ),
            "op",
        )
    with pytest.raises(SchemaError):  # copy events require one
        env(2, "artifact_copy_failed", dict(code="io_failure"))
    with pytest.raises(SchemaError):  # closed code set
        env(2, "artifact_copy_failed", dict(code="journal_unavailable"), "op")
    with pytest.raises(SchemaError):  # closed interruption families
        env(2, "operation_interrupted", dict(operation="target_send"), "op")
    with pytest.raises(SchemaError):  # closed decision reference
        env(
            2,
            "promotion_pending",
            dict(
                round_id=1,
                attempt_id="a1",
                version_id="v1",
                candidate_digest="d" * 64,
                dependency_digest="b" * 64,
                decision_ref=ref() | dict(extra=1),
            ),
        )
    for locator in ("/etc/passwd", "campaign\u0007.jsonl", "../campaign.jsonl", "", "x" * 513):
        with pytest.raises(SchemaError):  # a locator is a bounded printable relative ref
            env(
                2,
                "promotion_pending",
                dict(
                    round_id=1,
                    attempt_id="a1",
                    version_id="v1",
                    candidate_digest="d" * 64,
                    dependency_digest="b" * 64,
                    decision_ref=ref() | dict(locator=locator),
                ),
            )
    assert ARTIFACT_FAMILY.is_start("workspace_opened")
    assert ARTIFACT_FAMILY.is_start("artifact_copy_started")
    assert not ARTIFACT_FAMILY.is_start("workspace_ready")


def test_lifecycle_pairs_terminals_with_their_open_start():
    life = _with_champion()
    life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    with pytest.raises(SchemaError):  # a copy terminal never closes a workspace start
        life.accept(env(5, "artifact_copy_finished", dict(latency_ms=1), "w1"))
    with pytest.raises(SchemaError):  # interruption family must match the start
        life.accept(env(5, "operation_interrupted", dict(operation="artifact_copy"), "w1"))
    with pytest.raises(SchemaError):  # unknown operation
        life.accept(env(5, "workspace_ready", {}, "w9"))
    with pytest.raises(SchemaError):  # one workspace per attempt
        life.accept(env(5, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w2"))
    assert life.reserved_records == 2
    life.accept(env(5, "operation_interrupted", dict(operation="workspace"), "w1"))
    assert life.workspaces["a1"] == "opening" and life.reserved_records == 1
    life.accept(env(6, "candidate_abandoned", dict(attempt_id="a1", reason="workspace_not_ready")))
    assert life.workspaces["a1"] == "abandoned"
    with pytest.raises(SchemaError):  # abandoned once
        life.accept(env(7, "candidate_abandoned", dict(attempt_id="a1", reason="caller_abandoned")))
    with pytest.raises(SchemaError):  # unknown attempt
        life.accept(env(7, "candidate_abandoned", dict(attempt_id="a2", reason="caller_abandoned")))


def test_lifecycle_refuses_a_workspace_without_a_published_champion():
    life = ArtifactLifecycle()
    life.accept(created())
    with pytest.raises(SchemaError):  # every attempt starts from the frozen champion
        life.accept(env(2, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    with pytest.raises(SchemaError):  # the `v0` copy is not published until it finishes
        life.accept(env(3, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    life.accept(env(3, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    assert life.workspace_parents["a1"] == "v0"


def test_lifecycle_never_seals_an_abandoned_attempt():
    life = _with_champion()
    life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    life.accept(env(5, "workspace_ready", {}, "w1"))
    life.accept(start(6, "op2", "candidate", "a1", "v0", "workspace", cd="d" * 64))
    # The caller may abandon while the seal copy is still open; recovery does too.
    life.accept(env(7, "candidate_abandoned", dict(attempt_id="a1", reason="caller_abandoned")))
    with pytest.raises(SchemaError):  # an abandoned attempt is never published
        life.accept(env(8, "artifact_copy_finished", dict(latency_ms=5), "op2"))
    assert "candidate/a1" not in life.published and life.workspaces["a1"] == "abandoned"
    life.accept(env(8, "artifact_copy_failed", dict(code="artifact_unsafe"), "op2"))
    with pytest.raises(SchemaError):  # nor sealed by a fresh copy operation
        life.accept(start(9, "op3", "candidate", "a1", "v0", "workspace", cd="d" * 64))
    with pytest.raises(SchemaError):  # and it can never be promoted
        life.accept(
            env(
                9,
                "promotion_pending",
                dict(
                    round_id=1,
                    attempt_id="a1",
                    version_id="v1",
                    candidate_digest="d" * 64,
                    dependency_digest="b" * 64,
                    decision_ref=ref(),
                ),
            )
        )


def test_lifecycle_refuses_duplicate_publication_and_foreign_sources():
    life = ArtifactLifecycle()
    life.accept(created())
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    life.accept(env(3, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    with pytest.raises(SchemaError):  # champion/v0 is published exactly once
        life.accept(start(4, "op2", "champion", "v0", None, "seed"))
    with pytest.raises(SchemaError):  # a seed copy is only ever v0
        life.accept(start(4, "op2", "champion", "v1", None, "seed"))
    with pytest.raises(SchemaError):  # a candidate is never copied from the seed
        life.accept(start(4, "op2", "candidate", "a1", None, "seed"))
    with pytest.raises(SchemaError):  # no pending promotion for this version
        life.accept(start(4, "op2", "champion", "v1", "a1", "candidate", cd="d" * 64))
    with pytest.raises(SchemaError):  # the workspace parent must be the current champion
        life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v7"), "w1"))
    life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    life.accept(env(5, "workspace_ready", {}, "w1"))
    with pytest.raises(SchemaError):  # the copy parent must be the workspace parent
        life.accept(start(6, "op2", "candidate", "a1", "v9", "workspace", cd="d" * 64))
    life.accept(start(6, "op2", "candidate", "a1", "v0", "workspace", cd="d" * 64))
    assert life.workspaces["a1"] == "sealing"
    life.accept(env(7, "artifact_copy_failed", dict(code="io_failure"), "op2"))
    assert life.workspaces["a1"] == "sealing" and life.reserved_records == 1


def test_lifecycle_promotion_versions_are_sequential_and_unique():
    life = _promotable()
    ok = dict(
        round_id=1,
        attempt_id="a1",
        version_id="v1",
        candidate_digest="d" * 64,
        dependency_digest="b" * 64,
        decision_ref=ref(),
    )
    with pytest.raises(SchemaError):  # v0 is the seed champion; promotions start at v1
        life.accept(env(8, "promotion_pending", ok | dict(version_id="v2")))
    with pytest.raises(SchemaError):  # the attempt must be a published candidate
        life.accept(env(8, "promotion_pending", ok | dict(attempt_id="a2")))
    life.accept(env(8, "promotion_pending", ok))
    with pytest.raises(SchemaError):  # one pending promotion per round
        life.accept(env(9, "promotion_pending", ok | dict(round_id=1)))
    with pytest.raises(SchemaError):  # and one pending version at a time
        life.accept(env(9, "promotion_pending", ok | dict(round_id=2)))
    assert life.promotions[1].payload["decision_ref"]["kind"] == "selection_decided"
    life.accept(start(9, "op3", "champion", "v1", "a1", "candidate", cd="d" * 64))
    life.accept(env(10, "artifact_copy_finished", dict(latency_ms=5), "op3"))
    with pytest.raises(SchemaError):  # committed fields must equal the pending record
        life.accept(
            env(
                11,
                "promotion_committed",
                dict(
                    round_id=1,
                    version_id="v1",
                    attempt_id="a1",
                    candidate_digest="e" * 64,
                    dependency_digest="b" * 64,
                ),
            )
        )
    life.accept(
        env(
            11,
            "promotion_committed",
            dict(
                round_id=1,
                version_id="v1",
                attempt_id="a1",
                candidate_digest="d" * 64,
                dependency_digest="b" * 64,
            ),
        )
    )
    assert life.committed[0].payload["version_id"] == "v1" and not life.promotions
    assert life.workspaces["a1"] == "promoted" and life.champion_id == "v1"
    with pytest.raises(SchemaError):  # a committed round is never reopened
        life.accept(env(12, "promotion_pending", ok | dict(version_id="v2")))


def test_lifecycle_after_stop_admits_only_reserved_terminals():
    life = ArtifactLifecycle()
    life.accept(created())
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    assert life.reserved_records == 2
    life.accept(env(3, "store_stopped", dict(code="artifact_corrupted")))
    assert life.stopped == "artifact_corrupted" and life.reserved_records == 1
    with pytest.raises(SchemaError):  # no second stop
        life.accept(env(4, "store_stopped", dict(code="ledger_limit_exceeded")))
    with pytest.raises(SchemaError):  # no operationless work after the stop
        life.accept(env(4, "candidate_abandoned", dict(attempt_id="a1", reason="caller_abandoned")))
    life.accept(env(4, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    assert life.published["champion/v0"].payload["source"] == "seed"
    assert life.reserved_records == 0


def test_lifecycle_requires_store_created_first_and_one_run():
    life = ArtifactLifecycle()
    with pytest.raises(SchemaError):
        life.accept(env(1, "store_stopped", dict(code="artifact_corrupted")))
    life.accept(created())
    with pytest.raises(SchemaError):  # contiguous sequence
        life.accept(env(3, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    with pytest.raises(SchemaError):  # one identity per ledger
        life.accept(env(2, "store_created", created().payload))
    other = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="other",
            sequence=2,
            timestamp="2026-01-01T00:00:00Z",
            event="workspace_opened",
            operation_id="w1",
            payload=dict(attempt_id="a1", parent_id="v0"),
        ),
        family=ARTIFACT_FAMILY,
    )
    with pytest.raises(SchemaError):  # one run per ledger
        life.accept(other)
    assert (
        life.sequence == 1 and life.run_id == "run" and life.identity["profile"] == "artifact_store"
    )


def _with_champion():
    life = ArtifactLifecycle()
    life.accept(created())
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    life.accept(env(3, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    return life


def _promotable():
    life = ArtifactLifecycle()
    life.accept(created())
    life.accept(start(2, "op1", "champion", "v0", None, "seed"))
    life.accept(env(3, "artifact_copy_finished", dict(latency_ms=5), "op1"))
    life.accept(env(4, "workspace_opened", dict(attempt_id="a1", parent_id="v0"), "w1"))
    life.accept(env(5, "workspace_ready", {}, "w1"))
    life.accept(start(6, "op2", "candidate", "a1", "v0", "workspace", cd="d" * 64))
    life.accept(env(7, "artifact_copy_finished", dict(latency_ms=5), "op2"))
    return life


def rebased(seq, **overrides):
    """PDR-0010's re-stamp: the store identity unchanged, a newer version."""
    payload = dict(
        identity=IDENT.to_payload(),
        identity_digest=IDENT.digest,
        producing_version="0.3.0a12",
        campaign_rebase="c" * 64,
        authorization_id="0" * 64,
    )
    payload.update(overrides)
    return env(seq, "store_rebased", payload)


def test_store_rebased_is_a_closed_payload():
    assert rebased(2).event == "store_rebased"
    foreign = ArtifactStoreIdentityV1(POLICY, "c" * 64, "d" * 64)
    for overrides in (
        # The digest must prove the identity beside it.
        dict(identity_digest="f" * 64),
        dict(identity=foreign.to_payload()),
        dict(producing_version="not a version"),
        dict(campaign_rebase="c" * 63),
        dict(authorization_id=None),
        dict(extra=1),
    ):
        with pytest.raises(SchemaError):
            rebased(2, **overrides)
    with pytest.raises(SchemaError):  # operationless: it pairs with nothing
        env(2, "store_rebased", rebased(2).payload, "op1")


def snapshot(*records):
    """The prefix `ArtifactLedger.resume` reads the stamp from, without a ledger."""
    return Snapshot(
        records=records,
        consistency="durable",
        prefix_digest="e" * 64,
        valid_bytes=0,
        complete=True,
        journal_integrity="intact",
    )


def test_lifecycle_admits_a_rebase_and_moves_the_latest_store_record():
    life = ArtifactLifecycle()
    life.accept(created())
    # The lifecycle indexes nothing for a rebase: it admits the record and
    # advances. Which record a resume proves its version against is the
    # ledger's own reading over the prefix it holds.
    assert ArtifactLedger.latest_store_record(snapshot(created())) is not None
    assert ArtifactLedger.latest_store_record(snapshot(created())).event == "store_created"

    record = rebased(2)
    life.accept(record)

    assert life.sequence == 2
    assert ArtifactLedger.latest_store_record(snapshot(created(), record)) is record
    # A second one moves it again; the created record stays where it is.
    second = rebased(3, campaign_rebase="d" * 64)
    life.accept(second)
    assert life.sequence == 3
    assert ArtifactLedger.latest_store_record(snapshot(created(), record, second)) is second
    assert life.created is not None and life.created.event == "store_created"


def test_lifecycle_refuses_a_rebase_of_another_identity_or_after_a_stop():
    life = ArtifactLifecycle()
    life.accept(created())
    # A rebase changes the producing-version stamp, never the store identity.
    foreign = ArtifactStoreIdentityV1(POLICY, "c" * 64, "d" * 64)
    with pytest.raises(SchemaError):
        life.accept(rebased(2, identity=foreign.to_payload(), identity_digest=foreign.digest))
    life.accept(env(2, "store_stopped", dict(code="artifact_corrupted")))
    with pytest.raises(SchemaError):
        life.accept(rebased(3))
    # Nothing was admitted after the creation record, so the stamp never moved.
    assert life.sequence == 2
    assert ArtifactLedger.latest_store_record(snapshot(created())).event == "store_created"
