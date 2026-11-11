"""Real ledger bytes, campaign declaration authority and sibling evidence."""

from __future__ import annotations

import hashlib
import json

import pytest

from evolution.tree.artifacts.artifact_payloads import (
    AuxiliaryDeclaration,
    describe_auxiliary_files,
)
from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_fakes import LifecycleFixture
from tests.family15_fakes import family15_created, v16_document
from tests.tree.artifacts.test_artifact_store import create
from tests.tree.manifest.test_manifest_v15 import load

pytestmark = pytest.mark.slow


def publication_run(tmp_path, *, family=15, value=None, purpose="comparison"):
    """Publish through the actual store with an admitted campaign declaration."""
    policy = load(tmp_path, v16_document()).workflow.campaign
    with create(tmp_path) as store:
        payload = family15_created(policy)
        if family == 14:
            from tests.campaign_drivers import native14_created

            payload = native14_created(round_stop="all_attempts")
        row = dict(
            schema_version=1,
            run_id="campaign-run",
            sequence=1,
            timestamp="2026-10-18T00:00:00Z",
            event="campaign_created",
            operation_id=None,
            payload=payload,
        )
        life = LifecycleFixture.__new__(LifecycleFixture)
        life._initialize([row], family)
        prepared = tmp_path / "prepared"
        prepared.mkdir()
        raw = canonical_json(value if value is not None else {"unknown": "body"})
        if purpose == "comparison":
            (prepared / "payload.json").write_bytes(raw)
        else:
            (prepared / "index.json").write_bytes(raw)
            (prepared / "payload").write_bytes(b"diagnostics")
            (prepared / "gaps.json").write_bytes(canonical_json({"gaps": []}))
        files = describe_auxiliary_files(prepared, purpose=purpose)
        declaration = dict(
            family_version=15,
            purpose=purpose,
            format="comparison-data/1"
            if purpose == "comparison"
            else "baseline-diagnostic-bundle/1",
            content_digest=hashlib.sha256(raw).hexdigest(),
            files=[f.to_payload() for f in files],
        )
        if family == 15:
            seq = life.accept("auxiliary_payload_declared", **declaration)
            ref = life.reference(seq)
        else:
            ref = life.reference(1)
        (tmp_path / "run" / "campaign.jsonl").write_bytes(
            b"".join(e.encode() for e in life.envelopes)
        )
        publication = store.publish_auxiliary(
            AuxiliaryDeclaration.from_payload({**declaration, "declaration_ref": ref}), prepared
        )
    return tmp_path / "run", publication


def test_unknown_published_body_is_not_silently_verified(tmp_path):
    root, _ = publication_run(tmp_path)
    audit = verify_evidence(root)
    assert any(m["code"] == "family15_payload_invalid" for m in audit["mismatches"])


@pytest.mark.parametrize("fresh", [False, True])
async def test_audit_rederives_structural_fallback_not_just_its_declared_shape(
    tmp_path, monkeypatch, fresh
):
    import evolution.tree.case_comparison as comparisons
    from tests.campaign_drivers import EDITED
    from tests.test_tree_campaign_family15_gate import gate_run

    real = comparisons.fallback_reason

    def forged(scope, policy, *, threshold):
        if scope["name"] == "causal" and scope["cases"]:
            # At tolerance=1 the inclusive all-zero bound passes. Opting in
            # does not authorize this unnecessary weakening of the rule.
            return "causal_unconfirmable"
        return real(scope, policy, threshold=threshold)

    with monkeypatch.context() as patch:
        patch.setattr(comparisons, "fallback_reason", forged)
        run = await gate_run(
            tmp_path,
            patch,
            mode="confidence",
            fresh=fresh,
            gate_options={"bound_fallback": "point"},
            **EDITED,
        )
    audit = verify_evidence(run)
    assert any(m["code"] == "family15_evidence_mismatch" for m in audit["mismatches"]), audit


def test_auxiliary_family_assertion_does_not_authorize_family14_campaign(tmp_path):
    from evolution.tree.family15_audit import EvidenceMismatch, authenticate_auxiliary

    root, publication = publication_run(tmp_path, family=14)
    with pytest.raises(EvidenceMismatch, match="family"):
        authenticate_auxiliary(root, publication)


@pytest.mark.parametrize("tamper", ["missing", "bytes", "symlink", "ledger"])
def test_publication_corruption_is_a_read_only_audit_finding(tmp_path, tamper):
    root, publication = publication_run(tmp_path)
    path = root / publication.locator / "payload.json"
    path.parent.chmod(0o700)
    if tamper == "ledger":
        ledger = root / "artifacts.jsonl"
        ledger.write_bytes(ledger.read_bytes().rsplit(b"\n", 2)[0] + b"\n")
    elif tamper == "missing":
        path.unlink()
    elif tamper == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "prepared" / "payload.json")
    else:
        path.chmod(0o600)
        path.write_bytes(b"changed")
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    audit = verify_evidence(root)
    expected = (
        "family15_publication_pending" if tamper == "ledger" else "family15_artifact_integrity"
    )
    assert [m["code"] for m in audit["mismatches"]] == [expected]
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def disk_gate(tmp_path, request):
    import asyncio

    from tests.campaign_drivers import EDITED, OBSERVE, _cut_hook, _records, _Resettable
    from tests.campaign_fakes import Crash
    from tests.family14_public import family14_public_run
    from tests.family15_fakes import Family15Disk

    settings = dict(getattr(request, "param", {}))
    prebirth = settings.pop("prebirth", False)
    execution = {}
    config = {
        **OBSERVE,
        "gate": {
            **OBSERVE["gate"],
            "fresh_parent": settings.get("fresh_parent", False),
            "causal": {"tolerance": 0},
        },
    }

    with pytest.MonkeyPatch.context() as patch:
        if settings.get("verification") == "fresh":
            from evolution.tree.campaign import _Campaign
            from evolution.tree.campaign_identity import fresh_final_identity
            from evolution.tree.campaign_measurement import Measurement

            derive = _Campaign.derive_with

            def fresh_derive(self, identity, mid, digest, side, **kwargs):
                measurement = derive(self, identity, mid, digest, side, **kwargs)
                if "-final-" not in mid:
                    return measurement
                # Execute the fixture's donor final under the successor's
                # declared schedule from birth; never rewrite paid evidence.
                return Measurement.declare(
                    run_root=self.run_root,
                    campaign_run_id=self.run_id,
                    measurement_id=mid,
                    identity=fresh_final_identity(measurement.identity, mid, self.policy.repeats),
                    corpus=self.corpus,
                    secrets=self.secrets,
                    primary_range=self.range_of(mid),
                )

            patch.setattr(_Campaign, "derive_with", fresh_derive)
        _cut_hook(
            patch,
            "after_cohort_measurement_bound"
            if prebirth
            else "before_global_comparison"
            if settings.get("verification") == "fresh"
            else "before_finalists_bound",
        )
        if prebirth:
            from evolution.tree.campaign import _Campaign

            hooked = _Campaign.hook

            def capture(self, name):
                if name == "after_cohort_measurement_bound":
                    state = self.snapshot().rounds[1]
                    attempt = next(iter(state.attempts.values()))
                    execution.update(
                        corpus=self.corpus,
                        adapter=self.adapter,
                        environment=self.environment,
                        scorer=self.scorer,
                        strategy=self.strategy,
                        expected=self.expected_semantic,
                        connection=next(iter(self.connection_tokens.values())),
                        connection_epoch=0,
                        metric_contract=self.connection.metric_contract,
                        call_attempts=self.call_attempts(),
                        candidate=self.candidate_ref(attempt.measurement_id),
                        champion=self.candidate_ref(state.baseline_measurement_id),
                    )
                hooked(self, name)

            patch.setattr(_Campaign, "hook", capture)
        with pytest.raises(Crash):
            asyncio.run(
                family14_public_run(
                    tmp_path,
                    patch,
                    config,
                    family14_settings={"stability": settings.get("stability", "stable")},
                    **({"adapter": _Resettable(used=EDITED["used"])} if prebirth else {}),
                )
            )
    records = _records(tmp_path / "run" / "campaign.jsonl")
    disk = Family15Disk(tmp_path / "run", records, **settings)
    if prebirth:
        disk.execution = execution
    return disk


def delta_finding(disk, sequence):
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.family15_audit import Family15Audit

    context, log = _evidence_of(disk.root)
    assert log.journal_integrity == "complete"
    return Family15Audit(context).finding(log.records[sequence - 1])


def test_real_gate_side_completion_authenticates_all_scored_slots(disk_gate):
    bound, _ = disk_gate.bind_gate()
    complete = disk_gate.settle_gate(bound)
    assert delta_finding(disk_gate, complete) is None


@pytest.mark.parametrize("side", ["candidate", "parent"])
@pytest.mark.parametrize("disk_gate", [{"fresh_parent": True}], indirect=True)
def test_gate_completion_does_not_accept_unscored_tail(disk_gate, side):
    bound, _ = disk_gate.bind_gate()
    if side == "parent":
        candidate = disk_gate.settle_gate(bound)
        assert delta_finding(disk_gate, candidate) is None
    attempt = disk_gate.life.attempt(disk_gate.life.sealed)
    mid = attempt.measurement_id if side == "candidate" else attempt.parent_measurement_id
    path = disk_gate.root / "measurements" / mid / "events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    # A valid interrupted measurement prefix with one final scoring still owed.
    last = max(i for i, line in enumerate(lines) if json.loads(line)["event"] == "scoring_started")
    path.write_bytes(b"".join(lines[: last + 1]))
    complete = disk_gate.settle_gate(bound, side=side)
    finding = delta_finding(disk_gate, complete)
    assert finding is not None and "coverage" in finding["detail"]


def test_gate_binding_has_exact_numerical_audit(disk_gate):
    bound, _ = disk_gate.bind_gate()
    finding = delta_finding(disk_gate, bound)
    assert finding is None


def test_whole_family15_audit_preserves_shared_checks_and_does_not_crash(disk_gate):
    bound, _ = disk_gate.bind_gate()
    disk_gate.settle_gate(bound)
    audit = verify_evidence(disk_gate.root)
    assert not audit["mismatches"]
    assert audit["checked"] > 2 and audit["preconditions_checked"] > 2


def test_readonly_inspection_and_run_reader_select_family15(disk_gate):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.run_reader import open_run

    bound, _ = disk_gate.bind_gate()
    disk_gate.settle_gate(bound)
    before = {str(p): p.read_bytes() for p in disk_gate.root.rglob("*") if p.is_file()}
    handle = open_run(disk_gate.root)
    assert handle.family == 15
    inspected = inspect_campaign(disk_gate.root, verify=True).to_payload()
    assert not inspected["verification"]["mismatches"]
    assert before == {str(p): p.read_bytes() for p in disk_gate.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("tamper", ["identity", "kind", "foreign", "hash", "range"])
def test_binding_rejects_authenticated_but_false_source_or_body(disk_gate, tamper):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.family15_audit import Family15Audit

    bound, body = disk_gate.bind_gate()
    if tamper == "identity":
        body["candidate"]["identity"]["scorer"]["digest"] = "wrong"
    elif tamper == "kind":
        body = {
            "format": "comparison-data/1",
            "kind": "gate_seal",
            "measurement": disk_gate.measurement(
                disk_gate.life.attempt(disk_gate.life.sealed).measurement_id, settled=True
            ),
            "slots_digest": "a" * 64,
        }
    else:
        ref = body["reference"]["prefix"]
        if tamper == "foreign":
            ref["run_id"] = "other-run"
        elif tamper == "hash":
            ref["content_digest"] = "f" * 64
        else:
            ref["locator"] = ref["locator"].rsplit(":", 1)[0] + ":99999999"
    publication = disk_gate.publish(body)
    context, _ = _evidence_of(disk_gate.root)
    audit = Family15Audit(context)
    if tamper in {"identity", "kind"}:
        from evolution.tree.family15_audit import EvidenceMismatch

        with pytest.raises(EvidenceMismatch):
            audit.publication(AuxiliaryPublication.from_payload(publication))
    else:
        from evolution.tree.family15_audit import EvidenceMismatch

        with pytest.raises(EvidenceMismatch):
            audit.source(
                body["reference"]
                | {"candidate_digest": body["reference"]["identity"]["candidate_digest"]},
                complete=False,
            )


def test_gate_seal_authenticates_own_completed_candidate(disk_gate):
    seal = disk_gate.finish_gate()
    assert delta_finding(disk_gate, seal) is None


def test_completion_authenticates_actual_imports_and_all_imported_completion(disk_gate):
    bound, body = disk_gate.bind_completion()
    assert delta_finding(disk_gate, bound) is None
    completed = disk_gate.finish_completion(bound, body)
    assert delta_finding(disk_gate, completed) is None, str(delta_finding(disk_gate, completed))
    audit = verify_evidence(disk_gate.root)
    assert not any(m["event"] == "candidate_completion_completed" for m in audit["mismatches"])


def test_complete_reuse_final_and_selection_have_verified_numerics(disk_gate):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.run_reader import measurement_infos, open_run

    selection = disk_gate.finish_final()
    finding = delta_finding(disk_gate, selection)
    assert finding is None
    audit = verify_evidence(disk_gate.root)
    assert audit["mismatches"] == []
    assert any(
        m.id == "completion" and m.complete
        for m in measurement_infos(open_run(disk_gate.root).snapshot)
    )
    assert inspect_campaign(disk_gate.root, verify=True).to_payload()["verification"] == audit


@pytest.mark.parametrize("disk_gate", [{"fresh_parent": True}], indirect=True)
def test_completed_candidate_survives_parent_only_epoch_carry(disk_gate):
    import copy

    from evolution.tree.baseline_import import admit_rows, read_frozen_source
    from evolution.tree.campaign_identity import CampaignIdentityV1, derived_identity

    bound, _ = disk_gate.bind_gate()
    completed = disk_gate.settle_gate(bound)
    life = disk_gate.life
    candidate = life.attempt(life.sealed).measurement_id
    parent = life.attempt(life.sealed).parent_measurement_id
    source = read_frozen_source(
        disk_gate.root,
        life.run_id,
        parent,
        expected_identity_digest=life.snapshot.measurements[parent]["identity_digest"],
    )
    changed = copy.deepcopy(dict(life.snapshot.identity))
    changed["target"]["settings_hash"] = "settings-2"
    entry = life.snapshot.measurements[parent]
    destination = derived_identity(
        CampaignIdentityV1.from_payload(changed),
        entry["candidate_digest"],
        "champion",
        case_subset=entry["case_subset"],
    )
    carried = admit_rows(
        source,
        destination,
        primary=life.state.policy.metric_primary,
        condition_changed=["target"],
        primary_range=True,
    )
    moved = life.continue_block(
        [parent],
        identity_digest=destination.digest,
        imported=carried.imported,
        pairs_digest=carried.pairs_digest,
    )
    from evolution import __version__
    from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, Journal

    rebound = moved[0][1]
    raw = Journal._birth_bytes(
        rebound,
        dict(
            identity=destination.to_payload(),
            identity_digest=destination.digest,
            producing_version=__version__,
        ),
        carried.rows,
        secrets=(),
        max_record_bytes=MAX_RECORD_BYTES,
        max_journal_bytes=MAX_JOURNAL_BYTES,
    )
    path = disk_gate.root / "measurements" / rebound
    path.mkdir()
    (path / "events.jsonl").write_bytes(raw)
    disk_gate.write()
    from evolution.tree.campaign_projections import _check_rebound_carry, _evidence_of

    context, _ = _evidence_of(disk_gate.root)
    assert _check_rebound_carry(context) == []
    assert all(row["source"]["condition_fields"] == ["target"] for row in carried.rows)
    assert life.snapshot.measurements[candidate]["complete"]
    from evolution.tree.campaign_epochs import epoch_identity

    for mid in (candidate, parent):
        entry = life.snapshot.measurements[mid]
        actual = disk_gate.measurement(mid)["identity"]
        expected = derived_identity(
            epoch_identity(life.snapshot, mid),
            entry["candidate_digest"],
            "champion" if mid == parent else "candidate",
            case_subset=entry.get("case_subset"),
        ).to_payload()
        assert actual == expected, str(
            {
                k: (actual.get(k), expected.get(k))
                for k in actual.keys() | expected.keys()
                if actual.get(k) != expected.get(k)
            }
        )
    assert delta_finding(disk_gate, completed) is None, str(delta_finding(disk_gate, completed))
    assert (
        life.attempt(life.sealed).family15.gate_candidate.payload["source"]["measurement_id"]
        == candidate
    )
    corrupted = copy.deepcopy(list(carried.rows))
    corrupted[0]["source"]["condition_fields"] = []
    (path / "events.jsonl").write_bytes(
        Journal._birth_bytes(
            rebound,
            dict(
                identity=destination.to_payload(),
                identity_digest=destination.digest,
                producing_version=__version__,
            ),
            corrupted,
            secrets=(),
            max_record_bytes=MAX_RECORD_BYTES,
            max_journal_bytes=MAX_JOURNAL_BYTES,
        )
    )
    context, _ = _evidence_of(disk_gate.root)
    assert _check_rebound_carry(context)
    assert delta_finding(disk_gate, completed) is None


@pytest.mark.parametrize("tamper", ["counts", "hash", "symlink", "extra_tail"])
def test_completed_candidate_rejects_false_coverage_or_mutated_prefix(disk_gate, tamper):
    bound, _ = disk_gate.bind_gate()
    complete = disk_gate.settle_gate(bound)
    life = disk_gate.life
    mid = life.attempt(life.sealed).measurement_id
    path = disk_gate.root / "measurements" / mid / "events.jsonl"
    if tamper == "symlink":
        copy = disk_gate.root.parent / "other-events"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
    elif tamper == "extra_tail":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        from evolution.tree.campaign_projections import _evidence_of
        from evolution.tree.family15_audit import Family15Audit
        from evolution.tree.storage.envelope import Envelope

        payload = life.envelopes[complete - 1].to_payload()
        if tamper == "counts":
            payload["payload"].update(required=99, admissible=99)
        else:
            payload["payload"]["source"]["prefix"]["content_digest"] = "f" * 64
        context, _ = _evidence_of(disk_gate.root)
        finding = Family15Audit(context).finding(Envelope.from_payload(payload, family=life.family))
        assert finding is not None and finding["code"] != "family15_audit_incomplete"
        return
    finding = delta_finding(disk_gate, complete)
    assert finding is not None and finding["code"] != "family15_audit_incomplete"
    audit = verify_evidence(disk_gate.root)
    assert any(m["code"] != "family15_audit_incomplete" for m in audit["mismatches"])


@pytest.mark.parametrize("purpose", ["comparison", "baseline_diagnostics"])
@pytest.mark.parametrize(
    "tamper",
    ["ledger_run", "ledger_hash", "declaration_run", "declaration_hash", "declaration_range"],
)
def test_campaign_and_ledger_namespace_integrity_is_external_authority(tmp_path, purpose, tamper):
    from dataclasses import replace

    from evolution.tree.execution.candidate_integrity import ArtifactError
    from evolution.tree.family15_audit import EvidenceMismatch, authenticate_auxiliary
    from tests.test_tree_campaign_family15_schema import binding

    root, publication = publication_run(tmp_path, value=binding(), purpose=purpose)
    # Both comparison and diagnostic publication use their true separate namespaces.
    authenticate_auxiliary(root, publication)
    if tamper.startswith("ledger"):
        ref = replace(
            publication.publication_ref,
            **(
                {"run_id": "other-ledger"}
                if tamper == "ledger_run"
                else {"content_digest": "f" * 64}
            ),
        )
        broken = replace(publication, publication_ref=ref)
    else:
        change = (
            {"run_id": "other-campaign"}
            if tamper == "declaration_run"
            else {"content_digest": "f" * 64}
            if tamper == "declaration_hash"
            else {"locator": "campaign.jsonl#bytes=1:9"}
        )
        declaration = replace(
            publication.declaration,
            declaration_ref=replace(publication.declaration.declaration_ref, **change),
        )
        broken = replace(publication, declaration=declaration)
    with pytest.raises((EvidenceMismatch, ArtifactError)):
        authenticate_auxiliary(root, broken)


def test_actual_different_journal_run_ids_authenticate(disk_gate):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.family15_audit import authenticate_auxiliary

    bound, body = disk_gate.bind_gate()
    publication = AuxiliaryPublication.from_payload(
        disk_gate.life.envelopes[bound - 1].payload["data"]
    )
    assert publication.declaration.declaration_ref.run_id != publication.publication_ref.run_id
    assert authenticate_auxiliary(disk_gate.root, publication) == body


@pytest.mark.parametrize("tamper", ["metric", "provenance", "missing"])
def test_completion_birth_must_match_rederived_source_rows(disk_gate, tamper):
    import copy

    bound, body = disk_gate.bind_completion()
    changed = copy.deepcopy(body)
    if tamper == "metric":
        changed["rows"][0]["metrics"][disk_gate.life.state.policy.metric_primary] = 0.25
    elif tamper == "provenance":
        changed["rows"][0]["source"]["condition_fields"] = ["target"]
    else:
        changed["rows"].pop()
    complete = disk_gate.finish_completion(bound, changed)
    finding = delta_finding(disk_gate, complete)
    assert finding is not None and "imports differ" in finding["detail"]


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
def test_fresh_candidate_and_champion_authenticate_real_independent_s1(disk_gate):
    disk_gate.finish_final()
    sides = [e for e in disk_gate.life.envelopes if e.event == "final_measurement_completed"]
    assert {e.payload["side"] for e in sides} == {"candidate", "champion"}
    for event in sides:
        assert delta_finding(disk_gate, event.to_payload()["sequence"]) is None
        raw = (
            disk_gate.root
            / "measurements"
            / event.payload["source"]["measurement_id"]
            / "events.jsonl"
        ).read_bytes()
        assert b'"event":"sample_imported"' not in raw
    audit = verify_evidence(disk_gate.root)
    assert audit["mismatches"] == []


def test_family15_readonly_refresh_due_uses_shared_retention_count():
    from types import SimpleNamespace

    from evolution.tree.campaign_view import refresh_due_for

    snapshot = SimpleNamespace(
        family_version=15,
        first_round=1,
        rebaseline_from_round=None,
        epochs=[{}],
        rounds={
            1: SimpleNamespace(
                selection={"measurement_id": "baseline"},
                closed={"outcome": "retained"},
                baseline_measurement_id="baseline",
            )
        },
    )
    due = refresh_due_for(snapshot, SimpleNamespace(baseline_refresh_after_rounds=1), 2)
    assert due is not None and (due.replaced, due.first, due.last) == ("baseline", 1, 1)


@pytest.mark.parametrize("tamper", [None, "scheduled", "included", "gaps_digest"])
def test_diagnostic_consumer_requires_scalar_binding_and_exact_bundle_coverage(disk_gate, tamper):
    seq = diagnostic_consumer(disk_gate, tamper)
    finding = delta_finding(disk_gate, seq)
    # This structural fixture has no linked scalar admission. Task 11 now
    # rejects that semantic mismatch, including the untampered bundle shape.
    assert finding["code"] == "family15_evidence_mismatch", finding


def diagnostic_consumer(disk_gate, tamper=None):
    from evolution.tree.baseline_diagnostics import GAP_REASONS
    from tests.test_tree_campaign_family15_schema import source_summary

    _, completion = disk_gate.bind_completion()
    counts = dict.fromkeys(GAP_REASONS, 0)
    counts["missing"] = 2
    gaps = canonical_json(
        dict(
            format="baseline-diagnostic-bundle/1",
            scheduled=2,
            included=0,
            counts=counts,
            samples=[],
        )
    )
    bundle = disk_gate.publish(
        None,
        diagnostic_files={
            "index.json": canonical_json(dict(format="baseline-diagnostic-bundle/1", entries=[])),
            "payload": b"baseline-diagnostic-bundle/1\n\nend\n",
            "gaps.json": gaps,
        },
    )
    body = dict(
        format="comparison-data/1",
        kind="diagnostic_import",
        destination=dict(completion["destination"], measurement_id="next-baseline"),
        source_prefixes=[source_summary(completion["source"])],
        rows_digest=hashlib.sha256(canonical_json(completion["rows"])).hexdigest(),
        slot_index=completion["rows"],
        gaps_digest=hashlib.sha256(gaps).hexdigest(),
        counts=counts,
        authentication=dict(
            score="journal_bound", request="unavailable", stdout="captured_at_import"
        ),
    )
    if tamper == "gaps_digest":
        body["gaps_digest"] = "a" * 64
    data = disk_gate.publish(body)
    seq = disk_gate.life.accept(
        "diagnostic_import_declared",
        measurement_id="next-baseline",
        data=data,
        bundle=bundle,
        rows_digest=body["rows_digest"],
        scheduled=3 if tamper == "scheduled" else 2,
        included=1 if tamper == "included" else 0,
        sufficiency="partial" if tamper == "included" else "unavailable",
        authentication=body["authentication"],
    )
    disk_gate.write()
    return seq


@pytest.mark.parametrize("disk_gate", [{"early": True, "stability": "unstable"}], indirect=True)
def test_early_fresh_preflight_authenticates_identity_then_rejects_wrong_policy(disk_gate):
    from fractions import Fraction

    from tests.test_tree_campaign_family15_schema import preflight, q

    life = disk_gate.life
    life.early_attempt()
    body = preflight(threshold=Fraction(1))
    body.update(
        policy_digest=life.state.policy.digest,
        reference_kind="fresh",
        reference=dict(
            disk_gate.measurement(life.snapshot.rounds[1].baseline_measurement_id),
            measurement_id="m-r01-preflight-champion",
        ),
    )
    scope = body["scopes"][0]
    scope.update(
        declared_cases=["c1", "c2"],
        cases=[
            dict(case_id=c, group="g1", weight=q(Fraction(1, 2)), repeats=[1]) for c in ("c1", "c2")
        ],
    )
    body["results"][0].update(
        ceilings=[dict(case_id=c, value=q(1)) for c in ("c1", "c2")], delta_max=q(1)
    )
    seq = life.accept(
        "comparison_preflight_failed",
        round=1,
        attempt_id="r1-a1",
        epoch=0,
        phase="early_global",
        mode="point",
        reason="structurally_unattainable",
        data=disk_gate.publish(body),
    )
    disk_gate.write()
    finding = delta_finding(disk_gate, seq)
    assert finding["code"] == "family15_evidence_mismatch", finding
    assert "preflight exact" in str(finding), finding


def test_every_closed_family15_delta_has_explicit_e_dispatch():
    from evolution.tree.family15_audit import EVENTS
    from evolution.tree.family15_schema import _EVENT_FIELDS

    assert EVENTS == frozenset(_EVENT_FIELDS) | {"grouping_finished"}


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
def test_fresh_final_cannot_substitute_imported_scores(disk_gate, side):
    from evolution.tree.baseline_import import admit_rows, read_frozen_source
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, Journal
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from tests.test_tree_campaign_family15_schema import source_summary

    disk_gate.finish_final()
    event = next(
        e
        for e in disk_gate.life.envelopes
        if e.event == "final_measurement_completed" and e.payload["side"] == side
    )
    mid = event.payload["source"]["measurement_id"]
    source = read_frozen_source(
        disk_gate.root,
        disk_gate.life.run_id,
        mid,
        expected_identity_digest=event.payload["source"]["identity_digest"],
    )
    created = source.records[0].to_payload()
    admitted = admit_rows(
        source,
        RunIdentityV1.from_payload(created["payload"]["identity"]),
        primary=disk_gate.life.state.policy.metric_primary,
        condition_changed=(),
        primary_range=True,
    )
    raw = Journal._birth_bytes(
        created["run_id"],
        created["payload"],
        admitted.rows,
        secrets=(),
        max_record_bytes=MAX_RECORD_BYTES,
        max_journal_bytes=MAX_JOURNAL_BYTES,
    )
    (disk_gate.root / "measurements" / mid / "events.jsonl").write_bytes(raw)
    context, _ = _evidence_of(disk_gate.root)
    with pytest.raises(EvidenceMismatch, match="fresh.*import"):
        Family15Audit(context).source(source_summary(disk_gate.measurement(mid, settled=True)))


@pytest.mark.parametrize("purpose", ["comparison", "baseline_diagnostics"])
@pytest.mark.parametrize("journal", ["campaign.jsonl", "artifacts.jsonl"])
@pytest.mark.parametrize("where", ["before", "within"])
def test_publication_authenticates_physical_record_bytes(tmp_path, purpose, journal, where):
    from evolution.tree.family15_audit import EvidenceMismatch, authenticate_auxiliary
    from tests.test_tree_campaign_family15_schema import binding

    root, publication = publication_run(tmp_path, value=binding(), purpose=purpose)
    assert publication.declaration.declaration_ref.run_id != publication.publication_ref.run_id
    authenticate_auxiliary(root, publication)
    ref = (
        publication.declaration.declaration_ref
        if journal == "campaign.jsonl"
        else publication.publication_ref
    )
    start, end = map(int, ref.locator.split("=")[1].split(":"))
    path = root / journal
    raw = path.read_bytes()
    at = 1 if where == "before" else start + 1
    path.write_bytes(raw[:at] + b" " + raw[at:])
    before = path.read_bytes()
    with pytest.raises(EvidenceMismatch):
        authenticate_auxiliary(root, publication)
    audit = verify_evidence(root)
    if journal == "campaign.jsonl":
        assert any(m["code"] == "family15_evidence_mismatch" for m in audit["mismatches"]), audit
    else:
        # A declaration alone does not persist a terminal reference; there is
        # no stale terminal claim in this campaign until a consumer cites it.
        assert audit["mismatches"] == []
    assert path.read_bytes() == before
    # Historical parsing still admits whitespace. The physical boundary may be
    # authenticated when the caller actually cites its new bytes/range.
    from dataclasses import replace

    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.family15_audit import Family15Audit

    new_start = start + (where == "before")
    new_end = end + 1
    current = replace(
        ref,
        locator=f"{journal}#bytes={new_start}:{new_end}",
        content_digest=hashlib.sha256(before[new_start:new_end]).hexdigest(),
    )
    if journal == "artifacts.jsonl":
        authenticate_auxiliary(root, replace(publication, publication_ref=current))
    else:
        context, _ = _evidence_of(root)
        assert (
            Family15Audit(context).campaign_record(current.to_payload()).event
            == "auxiliary_payload_declared"
        )


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
@pytest.mark.parametrize("hops", [1, 2])
def test_authorized_own_fresh_carry_authenticates_and_preserves_completed_sibling(
    disk_gate, side, hops
):
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.family15_audit import Family15Audit
    from tests.test_tree_campaign_family15_schema import source_summary

    carry = disk_gate.fresh_carry(side, hops=hops)
    context, _ = _evidence_of(disk_gate.root)
    rows = Family15Audit(context).source(
        source_summary(disk_gate.measurement(carry["destination"], settled=True))
    )
    assert len(rows) == 2
    assert delta_finding(disk_gate, carry["sibling"]) is None
    assert all(row["source"]["condition_fields"] == ["target"] for row in carry["rows"])
    life = disk_gate.life
    life.accept(
        "stage_opened", measurement_id=carry["destination"], stage=1, look=1, sample_count=0
    )
    completed = life.accept(
        "final_measurement_completed",
        round=1,
        side=side,
        attempt_id=life.sealed if side == "candidate" else None,
        binding=life.reference(carry["binding"]),
        source=source_summary(disk_gate.measurement(carry["destination"], settled=True)),
        required=2,
        admissible=2,
    )
    disk_gate.write()
    assert delta_finding(disk_gate, completed) is None
    candidate_mid = (
        carry["destination"]
        if side == "candidate"
        else carry["body"]["candidate"]["measurement_id"]
    )
    champion_mid = (
        carry["destination"] if side == "champion" else carry["body"]["champion"]["measurement_id"]
    )
    disk_gate.finish_global(
        life.sequence_of("final_measurement_bound"),
        carry["body"]["comparison"],
        disk_gate.measurement(candidate_mid, settled=True),
        disk_gate.measurement(champion_mid, settled=True),
    )
    audit = verify_evidence(disk_gate.root)
    assert audit["mismatches"] == [], audit


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("location", ["source", "destination", "store"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo"])
def test_unsafe_fresh_carry_is_readonly_classified_evidence(disk_gate, location, unsafe):
    import os

    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection, inspect_view
    from evolution.tree.campaign_view import classify_siblings, next_actions
    from evolution.tree.storage.envelope import SchemaError
    from evolution.tree.storage.journal_errors import JournalFault

    carry = disk_gate.fresh_carry("candidate")
    key = "artifact_store" if location == "store" else carry[location]
    path = (
        disk_gate.root / "artifacts.jsonl"
        if location == "store"
        else disk_gate.root / "measurements" / key / "events.jsonl"
    )
    raw = path.read_bytes()
    other = disk_gate.root.parent / "saved-events"
    other.write_bytes(raw)
    path.unlink()
    if unsafe == "symlink":
        path.symlink_to(other)
    elif unsafe == "hardlink":
        os.link(other, path)
    else:
        os.mkfifo(path)

    def unchanged_metadata():
        stat = path.lstat()
        # Read-only calls may update atime, including legacy symlink probing.
        return (
            stat.st_mode,
            stat.st_ino,
            stat.st_nlink,
            stat.st_uid,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    before = (
        unchanged_metadata(),
        other.read_bytes(),
        (disk_gate.root / "campaign.jsonl").read_bytes(),
    )
    audit = verify_evidence(disk_gate.root)
    expected = "family15_artifact_integrity" if location == "store" else "baseline_import_mismatch"
    assert any(m["code"] == expected for m in audit["mismatches"]), audit
    projected = inspect_campaign(disk_gate.root, verify=True)
    payload = projected.to_payload()
    assert payload["verification"] == audit
    assert payload["family_version"] == 15
    assert payload["siblings"][key] == "unreadable"
    assert payload["actions"] == []
    assert CampaignProjection.from_bytes(projected.encode()) == projected
    with pytest.raises(SchemaError):
        CampaignProjection.from_payload(
            dict(payload, actions=[dict(kind="bind_baseline", fields={})])
        )
    legacy = dict(payload)
    legacy.pop("family_version")
    with pytest.raises(SchemaError):
        CampaignProjection.from_payload(legacy)
    with pytest.raises(SchemaError):
        CampaignProjection.from_payload(dict(payload, family_version=14))
    view = inspect_view(disk_gate.root)
    assert view.snapshot.run_id == disk_gate.life.snapshot.run_id
    assert next_actions(view, disk_gate.life.state.policy) == ()
    # The executor still uses the original three-state classifier and raises.
    with pytest.raises((OSError, JournalFault)):
        classify_siblings(disk_gate.root, view.snapshot)
    if location == "store":
        assert payload["lineage"] is None
    else:
        assert key not in view.measurements and key not in view.tries
        entry = next(row for row in payload["measurements"] if row["measurement_id"] == key)
        assert entry["status"] is None
        for field in (
            "tries",
            "imported",
            "call_retries",
            "call_failures_by_code",
            "repair_tries",
            "repairs_scored",
        ):
            assert entry[field] is None
    assert before == (
        unchanged_metadata(),
        other.read_bytes(),
        (disk_gate.root / "campaign.jsonl").read_bytes(),
    )


@pytest.mark.parametrize("purpose", ["comparison", "baseline_diagnostics"])
@pytest.mark.parametrize("where", ["before", "within"])
def test_consumers_reject_stale_physical_s2_references(disk_gate, purpose, where):
    if purpose == "comparison":
        seq, _ = disk_gate.bind_gate()
        pub = disk_gate.life.envelopes[seq - 1].payload["data"]
    else:
        seq = diagnostic_consumer(disk_gate)
        pub = disk_gate.life.envelopes[seq - 1].payload["bundle"]
    start = int(pub["publication_ref"]["locator"].split("=")[1].split(":")[0])
    path = disk_gate.root / "artifacts.jsonl"
    raw = path.read_bytes()
    at = 1 if where == "before" else start + 1
    path.write_bytes(raw[:at] + b" " + raw[at:])
    finding = delta_finding(disk_gate, seq)
    assert finding["code"] == "family15_artifact_integrity", finding
    audit = verify_evidence(disk_gate.root)
    assert any(
        m["sequence"] == seq and m["code"] == "family15_artifact_integrity"
        for m in audit["mismatches"]
    ), audit


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
@pytest.mark.parametrize(
    "tamper", ["metrics", "condition_fields", "origin_run", "other_finalist", "gate"]
)
def test_fresh_rebound_rejects_substituted_or_forged_imports(disk_gate, side, tamper):
    import copy

    from evolution.tree.baseline_import import admit_rows, read_frozen_source
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, Journal
    from evolution.tree.execution.journal_identity import RunIdentityV1
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from tests.test_tree_campaign_family15_schema import source_summary

    carry = disk_gate.fresh_carry(side, hops=2)
    path = disk_gate.root / "measurements" / carry["destination"] / "events.jsonl"
    created = json.loads(path.read_bytes().splitlines()[0])
    rows = copy.deepcopy(list(carry["rows"]))
    if tamper == "metrics":
        rows[0]["metrics"][disk_gate.life.state.policy.metric_primary] = 0.125
    elif tamper == "condition_fields":
        rows[0]["source"]["condition_fields"] = []
    elif tamper == "origin_run":
        rows[0]["source"]["run_id"] = "foreign-campaign"
    elif tamper == "other_finalist":
        rows[0]["source"]["measurement_id"] = "other-finalist"
    else:
        mid = (
            disk_gate.life.attempt(disk_gate.life.sealed).measurement_id
            if side == "candidate"
            else disk_gate.life.snapshot.rounds[1].baseline_measurement_id
        )
        source = read_frozen_source(
            disk_gate.root,
            disk_gate.life.run_id,
            mid,
            expected_identity_digest=disk_gate.life.snapshot.measurements[mid]["identity_digest"],
        )
        rows = admit_rows(
            source,
            RunIdentityV1.from_payload(created["payload"]["identity"]),
            primary=disk_gate.life.state.policy.metric_primary,
            condition_changed=["target"],
            primary_range=True,
        ).rows
    path.write_bytes(
        Journal._birth_bytes(
            created["run_id"],
            created["payload"],
            rows,
            secrets=(),
            max_record_bytes=MAX_RECORD_BYTES,
            max_journal_bytes=MAX_JOURNAL_BYTES,
        )
    )
    context, _ = _evidence_of(disk_gate.root)
    with pytest.raises(EvidenceMismatch, match="fresh carry imports"):
        Family15Audit(context).source(
            source_summary(disk_gate.measurement(carry["destination"], settled=True))
        )


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
def test_unborn_fresh_rebound_remains_owed_not_corrupt(disk_gate):
    from evolution.tree.campaign import inspect_campaign

    carry = disk_gate.fresh_carry("candidate", born=False)
    audit = verify_evidence(disk_gate.root)
    assert audit["mismatches"] == [], audit
    status = inspect_campaign(disk_gate.root, verify=True).to_payload()
    assert status["siblings"][carry["destination"]] == "absent"
    assert status["verification"] == audit


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("location", ["destination", "store"])
@pytest.mark.parametrize("contents", ["corrupt", "empty", "missing"])
def test_inspection_distinguishes_unreadable_unborn_and_absent(disk_gate, location, contents):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import CampaignProjection

    carry = disk_gate.fresh_carry("candidate")
    key = "artifact_store" if location == "store" else carry[location]
    path = (
        disk_gate.root / "artifacts.jsonl"
        if location == "store"
        else disk_gate.root / "measurements" / key / "events.jsonl"
    )
    if contents == "missing":
        path.unlink()
    else:
        path.write_bytes(b"invalid\n" if contents == "corrupt" else b"")
    audit = verify_evidence(disk_gate.root)
    projection = inspect_campaign(disk_gate.root, verify=True)
    payload = projection.to_payload()
    assert payload["verification"] == audit
    assert payload["siblings"][key] == (
        "unreadable"
        if contents == "corrupt"
        else "absent"
        if location == "store" and contents == "missing"
        else "unborn"
    )
    assert CampaignProjection.from_bytes(projection.encode()) == projection
    assert bool(audit["mismatches"]) == (location == "store" or contents == "corrupt")
    if contents == "corrupt":
        assert payload["actions"] == []


@pytest.mark.parametrize(
    "disk_gate",
    [{"verification": "fresh", "stability": "unstable", "prebirth": True}],
    indirect=True,
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
@pytest.mark.parametrize("hops", [1, 2])
@pytest.mark.parametrize("layout", ["absent", "empty"])
def test_never_born_fresh_source_can_rebind_and_execute_locally(disk_gate, side, hops, layout):
    carry = disk_gate.prebirth_carry(side, hops=hops, source_layout=layout)
    source = disk_gate.root / "measurements" / carry["source"]
    if layout == "absent":
        assert not source.exists()
    else:
        assert (source / "events.jsonl").read_bytes() == b""
    destination = disk_gate.root / "measurements" / carry["destination"] / "events.jsonl"
    events = [json.loads(line)["event"] for line in destination.read_bytes().splitlines()]
    assert "sample_imported" not in events
    assert events.count("scoring_finished") == 2
    assert delta_finding(disk_gate, carry["completion"]) is None
    assert delta_finding(disk_gate, carry["sibling"]) is None
    audit = verify_evidence(disk_gate.root)
    assert audit["mismatches"] == []


@pytest.mark.parametrize(
    "disk_gate",
    [{"verification": "fresh", "stability": "unstable", "prebirth": True}],
    indirect=True,
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
@pytest.mark.parametrize("tamper", ["missing", "symlink", "corrupt", "empty"])
def test_zero_admissible_born_fresh_source_cannot_disappear(disk_gate, side, tamper):
    carry = disk_gate.prebirth_carry(side, born_source=True)
    assert delta_finding(disk_gate, carry["completion"]) is None
    path = disk_gate.root / "measurements" / carry["source"] / "events.jsonl"
    original = path.read_bytes()
    assert len(original.splitlines()) == 1  # Born, no admissible source observations.
    if tamper in {"missing", "symlink"}:
        path.unlink()
        if tamper == "symlink":
            other = disk_gate.root.parent / "source-copy"
            other.write_bytes(original)
            path.symlink_to(other)
    else:
        path.write_bytes(b"invalid\n" if tamper == "corrupt" else b"")
    finding = delta_finding(disk_gate, carry["completion"])
    assert finding is not None and finding["code"] != "family15_audit_incomplete", finding
    audit = verify_evidence(disk_gate.root)
    assert any(
        m["sequence"] == carry["completion"] and m["code"] != "family15_audit_incomplete"
        for m in audit["mismatches"]
    ), audit


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
def test_fresh_carry_authenticates_frozen_valid_prefix_with_torn_tail(disk_gate, side):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.campaign_projections import _evidence_of, inspect_view
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from tests.test_tree_campaign_family15_schema import source_summary

    carry = disk_gate.fresh_carry(side, hops=2)
    clean = inspect_campaign(disk_gate.root).to_payload()
    assert clean["actions"]  # The frozen tail must not suppress an owed action.
    clean_status = inspect_view(disk_gate.root).measurements[carry["root"]]
    path = disk_gate.root / "measurements" / carry["root"] / "events.jsonl"
    path.write_bytes(path.read_bytes() + b'{"event":')
    before = path.read_bytes()
    context, _ = _evidence_of(disk_gate.root)
    source = source_summary(disk_gate.measurement(carry["destination"], settled=True))
    assert len(Family15Audit(context).source(source)) == 2
    audit = verify_evidence(disk_gate.root)
    assert audit["mismatches"] == [], audit
    assert path.read_bytes() == before
    inspected = inspect_campaign(disk_gate.root, verify=True).to_payload()
    assert inspected["verification"] == audit
    assert inspected["siblings"][carry["root"]] == "born"
    status = inspect_view(disk_gate.root).measurements[carry["root"]]
    assert status["journal_integrity"] == "incomplete_tail"
    for key in ("run_identity_digest", "prefix_digest", "valid_bytes", "last_sequence"):
        assert status[key] == clean_status[key]
    assert inspected["actions"] == clean["actions"]
    assert path.read_bytes() == before
    # Complete destination references still forbid a later mutable/torn tail.
    destination = disk_gate.root / "measurements" / carry["destination"] / "events.jsonl"
    destination_raw = destination.read_bytes()
    destination.write_bytes(destination_raw + b'{"event":')
    context, _ = _evidence_of(disk_gate.root)
    with pytest.raises(EvidenceMismatch, match="later mutable"):
        Family15Audit(context).source(source)
    assert inspect_view(disk_gate.root).siblings[carry["destination"]] == "unreadable"
    destination.write_bytes(destination_raw)
    completed = disk_gate.life.envelopes[carry["sibling"] - 1].payload["source"]["measurement_id"]
    completed_path = disk_gate.root / "measurements" / completed / "events.jsonl"
    completed_path.write_bytes(completed_path.read_bytes() + b'{"event":')
    assert inspect_view(disk_gate.root).siblings[completed] == "unreadable"
    assert delta_finding(disk_gate, carry["sibling"]) is not None
    ledger = disk_gate.root / "artifacts.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b'{"event":')
    assert inspect_view(disk_gate.root).siblings["artifact_store"] == "unreadable"


@pytest.mark.parametrize(
    "disk_gate", [{"verification": "fresh", "stability": "unstable"}], indirect=True
)
@pytest.mark.parametrize("side", ["candidate", "champion"])
@pytest.mark.parametrize("tamper", ["missing", "corrupt", "empty"])
def test_scored_frozen_fresh_source_cannot_disappear(disk_gate, side, tamper):
    from evolution.tree.campaign_projections import _evidence_of
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from tests.test_tree_campaign_family15_schema import source_summary

    carry = disk_gate.fresh_carry(side)
    path = disk_gate.root / "measurements" / carry["source"] / "events.jsonl"
    if tamper == "missing":
        path.unlink()
    else:
        path.write_bytes(b"" if tamper == "empty" else path.read_bytes() + b"invalid\n")
    context, _ = _evidence_of(disk_gate.root)
    with pytest.raises(EvidenceMismatch, match="unborn fresh source|frozen fresh source"):
        Family15Audit(context).source(
            source_summary(disk_gate.measurement(carry["destination"], settled=True))
        )


@pytest.mark.parametrize("missing", [None, "candidate", "reference", "early_causal"])
def test_focus_audit_requires_every_owned_focus_row(missing):
    from dataclasses import replace
    from types import SimpleNamespace

    from evolution.tree.campaign_policy import CampaignPolicyV12
    from evolution.tree.case_comparison import source_summary
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from evolution.tree.family15_schema import FAMILY15_CONTRACT
    from evolution.tree.storage.envelope import Envelope
    from tests.family15_fakes import publication15, reference15
    from tests.test_tree_campaign_family15_schema import q, result_data
    from tests.test_tree_case_comparison import built, row

    profile, _ = built(
        fresh=True, groups={"a": "g", "b": "g"}, scopes={"focus": ["a"], "causal": ["b"]}
    )
    policy_body = profile.policy.to_payload()
    policy_body["gate"]["stages"] = "focus_first"
    policy = CampaignPolicyV12.from_payload(policy_body)
    binding = profile.to_payload() | {"policy_digest": policy.digest}
    body = result_data() | {
        "binding_digest": hashlib.sha256(canonical_json(binding)).hexdigest(),
        "outcome": "passed",
        "reason": "passed",
        "repeat_diagnostics": [],
        "variation": "not_requested",
    }
    for side in ("candidate", "reference"):
        body[side] = source_summary(binding[side])
        body[side]["prefix"] = reference15("journal_prefix", body[side]["measurement_id"])
    body["scopes"][0]["point"] = q(1)
    body["retained_pairs"] = [
        {side: binding[side]["identity"]["schedule"][0] for side in ("candidate", "reference")}
    ]
    payload = dict(
        round=1,
        attempt_id="a",
        binding=reference15(),
        outcome="passed",
        reason="passed",
        data=publication15(),
    )
    record = Envelope.from_payload(
        dict(
            schema_version=1,
            run_id="run",
            sequence=3,
            timestamp="2026-10-28T00:00:00Z",
            event="focus_decided",
            operation_id=None,
            payload=payload,
        ),
        family=FAMILY15_CONTRACT,
    )

    class AuthenticatedRows(Family15Audit):
        # Stand in only for authenticated I/O; exercise the real structural audit.
        def __init__(self):
            self.context = SimpleNamespace(
                policy_for=lambda mid: policy,
                measurement_state=lambda prefix: (
                    [
                        SimpleNamespace(
                            event="rollout_started",
                            payload={"case_id": "b" if missing == "early_causal" else "a"},
                        )
                    ],
                    None,
                ),
            )

        def data(self, record, kind):
            return body

        def campaign_record(self, ref, before):
            return None

        def binding(self, record):
            return binding

        def source(self, source, *, complete=True):
            side = (
                "candidate"
                if source["measurement_id"] == binding["candidate"]["measurement_id"]
                else "reference"
            )
            focus = row(1 if side == "candidate" else 0, case="a")
            if side == missing:
                focus = replace(focus, disposition="not_started", outcome=None, metrics=None)
            return (focus, replace(row(None, case="b"), disposition="not_started", outcome=None))

    if missing is None:
        assert AuthenticatedRows().result(record) == body
    else:
        with pytest.raises(EvidenceMismatch):
            AuthenticatedRows().result(record)


@pytest.mark.parametrize("mode", ["point", "statistical"])
@pytest.mark.parametrize("event", ["focus_decided", "comparison_completed"])
@pytest.mark.parametrize("passing", [False, True])
@pytest.mark.parametrize("tamper", [False, True])
def test_scoped_numerical_audit_rebuilds_authenticated_rows(mode, event, passing, tamper):
    from types import SimpleNamespace

    from evolution.tree.case_comparison import (
        evaluate_causal_after_focus,
        evaluate_focus,
        focus_rejection,
    )
    from evolution.tree.family15_audit import EvidenceMismatch, Family15Audit
    from tests.test_tree_case_comparison import built, row

    profile, refs = built(
        stages="focus_first",
        mode=mode,
        repeats=2,
        groups={"a": "g", "b": "g"},
        scopes={"focus": ["a"], "causal": ["b"]},
    )
    rows = [row(1 if passing else 0, case=c, repeat=i) for c in ("a", "b") for i in (1, 2)]
    seed = profile.policy.random_seed
    focus = evaluate_focus(profile, rows, refs, campaign_seed=seed)
    body = (
        focus
        if event == "focus_decided"
        else evaluate_causal_after_focus(profile, focus, rows, refs, campaign_seed=seed)
        if passing
        else focus_rejection(profile, focus)
    ).to_payload()
    if tamper:
        body["scopes"][0]["point"] = {"n": "0x1", "d": "0x2"}
    focus_record = SimpleNamespace(event="focus_decided")
    record = SimpleNamespace(
        event=event,
        payload=dict(binding={}, round=1, attempt_id="a"),
        to_payload=lambda: {"sequence": 10},
    )
    attempt = SimpleNamespace(
        family15=SimpleNamespace(focus_decision=SimpleNamespace(envelope=focus_record))
    )

    class AuthenticatedRows(Family15Audit):
        # Replace authenticated I/O only; execute the real numerical dispatcher.
        def __init__(self):
            self.context = SimpleNamespace(
                recompute=True,
                policy_for=lambda mid: profile.policy,
                snapshot_before=lambda seq: SimpleNamespace(
                    rounds={1: SimpleNamespace(attempts={"a": attempt})}
                ),
            )

        def result(self, rec):
            return focus.to_payload() if rec is focus_record else body

        def campaign_record(self, ref, before):
            return None

        def numerical_binding(self, rec):
            return profile

        def source(self, source, *, complete=True):
            return rows if source["measurement_id"] == body["candidate"]["measurement_id"] else refs

    if tamper:
        with pytest.raises(EvidenceMismatch, match="exact authenticated-row computation"):
            AuthenticatedRows().numerical_result(record)
    else:
        AuthenticatedRows().numerical_result(record)
