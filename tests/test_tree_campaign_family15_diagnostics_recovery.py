"""Diagnostic declaration, publication and scalar birth recover exact owed work."""

import json

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.authorizations import Authorization
from tests.campaign_drivers import _records
from tests.test_tree_campaign_family15_diagnostics import failing_source, linked_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize(
    "cut",
    [
        "after_campaign_created",
        "after_diagnostics_declared",
        "after_diagnostics_published",
        "after_comparison_data_declared",
        "after_comparison_data_published",
        "after_diagnostic_import_bound",
        "after_measurement_bound",
    ],
)
async def test_unborn_source_loss_is_explicit_and_authorized_fresh_measures_extra_work(
    tmp_path, monkeypatch, cut
):
    source = await failing_source(tmp_path / "source", monkeypatch)
    root = tmp_path / "successor"
    run = await linked_run(root, monkeypatch, source, stop=cut, hard=True)
    source.rename(source.with_name("moved"))
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as caught:
        await linked_run(root, monkeypatch, source)
    assert "baseline_import_unavailable" in str(caught.value)
    assert (run / "campaign.jsonl").read_bytes() == before
    finding = next(f for f in caught.value.findings if f.code == "baseline_import_unavailable")
    decision = Authorization.new(
        "baseline_import_unavailable", finding.instance_digest, "source gone"
    )
    await linked_run(root, monkeypatch, source, authorizations=(decision,))
    rows = _records(run / "campaign.jsonl")
    assert any(
        r["event"] == "authorization_applied" and r["payload"]["effect"] == "fresh_baseline"
        for r in rows
    )
    mid = next(r["payload"]["measurement_id"] for r in rows if r["event"] == "baseline_bound")
    samples = _records(run / "measurements" / mid / "events.jsonl")
    assert not any(r["event"] == "sample_imported" for r in samples)
    assert sum(r["event"] == "scoring_finished" for r in samples) == 2
    assert verify_evidence(run)["mismatches"] == []


async def test_postbirth_source_loss_keeps_actual_permitted_observations_and_citations(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign import _Campaign

    captured = {}
    original = _Campaign.adaptive_inputs_for

    def capture(self, number, **kwargs):
        inputs = original(self, number, **kwargs)
        captured.setdefault(
            str(self.run_root),
            {
                "registry": inputs.list_grouping_inputs({}),
                "failure": inputs.read_evidence({"evidence_id": "obs:c1:1"}),
            },
        )
        return inputs

    monkeypatch.setattr(_Campaign, "adaptive_inputs_for", capture)
    source = await failing_source(tmp_path / "source", monkeypatch)
    root = tmp_path / "successor"
    run = await linked_run(root, monkeypatch, source, stop="after_baseline_bound", hard=True)
    source.rename(source.with_name("moved"))
    await linked_run(root, monkeypatch, source)
    assert captured[str(run)] == captured[str(source)]
    assert '"evidence_id":"obs:c1:1"' in json.dumps(captured[str(run)], separators=(",", ":"))
    assert verify_evidence(run)["mismatches"] == []


async def test_declared_subset_is_not_reselected_after_source_trace_changes(tmp_path, monkeypatch):
    source = await failing_source(tmp_path / "source", monkeypatch)
    root = tmp_path / "successor"
    run = await linked_run(root, monkeypatch, source, stop="after_diagnostics_declared", hard=True)
    # Directory and packed sources are both supported by the pure preparation suite;
    # deleting all trace inputs changes diagnostic coverage without changing scalar evidence.
    import shutil

    shutil.rmtree(source / "scorer-traces")
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused, match="baseline_import_unavailable"):
        await linked_run(root, monkeypatch, source)
    assert (run / "campaign.jsonl").read_bytes() == before


@pytest.mark.parametrize("damage", ["missing", "changed", "symlink"])
async def test_abandoned_comparison_debt_requires_exact_retained_proof(
    tmp_path, monkeypatch, damage
):
    source = await failing_source(tmp_path / "source", monkeypatch)
    root = tmp_path / "successor"
    run = await linked_run(
        root, monkeypatch, source, stop="after_comparison_data_declared", hard=True
    )
    source.rename(source.with_name("moved"))
    with pytest.raises(EvolveRefused) as caught:
        await linked_run(root, monkeypatch, source)
    finding = next(f for f in caught.value.findings if f.code == "baseline_import_unavailable")
    decision = Authorization.new(
        "baseline_import_unavailable", finding.instance_digest, "source gone"
    )
    (proof,) = (run / "diagnostic-imports/.prepared").glob("*.json")
    if damage == "changed":
        proof.write_bytes(b"changed")
    else:
        raw = proof.read_bytes()
        proof.unlink()
        if damage == "symlink":
            other = root / "other-proof"
            other.write_bytes(raw)
            proof.symlink_to(other)
    await linked_run(root, monkeypatch, source, authorizations=(decision,))
    findings = verify_evidence(run)["mismatches"]
    assert any(f["event"] == "auxiliary_payload_declared" for f in findings), findings


async def test_source_loss_before_creation_refuses_without_successor_state(tmp_path, monkeypatch):
    source = await failing_source(tmp_path / "source", monkeypatch)
    source.rename(source.with_name("moved"))
    with pytest.raises(EvolveRefused):
        await linked_run(tmp_path / "successor", monkeypatch, source)
    assert not (tmp_path / "successor/run/campaign.jsonl").exists()


@pytest.mark.parametrize("purpose", ["baseline_diagnostics", "comparison"])
async def test_abandonment_never_releases_unrelated_auxiliary_debt(tmp_path, monkeypatch, purpose):
    import copy

    from evolution.tree.campaign import _Campaign

    source = await failing_source(tmp_path / "source", monkeypatch)
    original = _Campaign.hook

    def extra(self, name):
        original(self, name)
        if name != "after_diagnostics_declared":
            return
        previous = next(
            r.payload
            for r in self.log.snapshot().records
            if r.event == "auxiliary_payload_declared"
            and r.payload["purpose"] == "baseline_diagnostics"
        )
        declaration = copy.deepcopy(previous)
        declaration["content_digest"] = "a" * 64
        declaration["files"][0]["digest"] = "a" * 64
        if purpose == "comparison":
            declaration.update(
                purpose=purpose,
                format="comparison-data/1",
                files=[dict(name="payload.json", byte_count=1, digest="a" * 64)],
            )
        self.append("auxiliary_payload_declared", declaration)

    root = tmp_path / "successor"
    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", extra)
        run = await linked_run(root, patch, source, stop="after_diagnostics_declared", hard=True)
    source.rename(source.with_name("moved"))
    with pytest.raises(EvolveRefused) as caught:
        await linked_run(root, monkeypatch, source)
    finding = next(f for f in caught.value.findings if f.code == "baseline_import_unavailable")
    decision = Authorization.new(
        "baseline_import_unavailable", finding.instance_digest, "source gone"
    )
    await linked_run(root, monkeypatch, source, authorizations=(decision,))
    assert any(
        f["code"] == "family15_publication_pending" for f in verify_evidence(run)["mismatches"]
    )


@pytest.mark.parametrize(
    "cut", ["after_auxiliary_started", "after_auxiliary_replace", "after_auxiliary_finished"]
)
@pytest.mark.parametrize("source_lost", [False, True])
async def test_actual_s2_diagnostic_publication_cuts_recover_once(
    tmp_path, monkeypatch, cut, source_lost
):
    from evolution.tree.artifacts.artifacts import ArtifactStore
    from tests.test_tree_campaign_family15_gate import GatePowerCut

    source = await failing_source(tmp_path / "source", monkeypatch)
    root = tmp_path / "successor"
    original = ArtifactStore._hook

    def interrupt(self, name):
        original(self, name)
        if name == cut:
            raise GatePowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "_hook", interrupt)
        run = await linked_run(root, patch, source, stop="after_baseline_bound", hard=True)
    authorizations = ()
    if source_lost:
        source.rename(source.with_name("moved"))
        with pytest.raises(EvolveRefused) as caught:
            await linked_run(root, monkeypatch, source)
        finding = next(f for f in caught.value.findings if f.code == "baseline_import_unavailable")
        authorizations = (
            Authorization.new(
                "baseline_import_unavailable", finding.instance_digest, "source gone"
            ),
        )
    await linked_run(root, monkeypatch, source, authorizations=authorizations)
    if not source_lost:
        rows = _records(run / "artifacts.jsonl")
        assert sum(r["event"] == "auxiliary_publish_finished" for r in rows) == 2
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("role", ["grouping", "reflection"])
async def test_damage_during_role_read_is_sticky_persistence_failure(tmp_path, monkeypatch, role):
    from evolution.tree.evolve import evolve
    from evolution.tree.roles.reflection_reader import ReaderError, ReflectionReadSession
    from tests.campaign_fakes import FakeAdapter

    source = await failing_source(tmp_path / "source", monkeypatch)
    root = tmp_path / "successor"
    run = await linked_run(root, monkeypatch, source, stop="after_baseline_bound", hard=True)
    original = ReflectionReadSession.verify_admission
    observed = []

    def damage_then_read(self, check):
        method = "read_grouping_input" if role == "grouping" else "read_evidence"
        if not observed and method in self.methods:
            (payload,) = (run / "diagnostic-imports").glob("*/payload")
            payload.parent.chmod(0o700)
            payload.unlink()
            observed.append("deleted")
            self._view.body_cache.clear()
            arguments = (
                {"index": 0, "section": "observation"}
                if role == "grouping"
                else {"evidence_id": "obs:c1:1"}
            )
            try:
                self.dispatch(method, arguments)
            except ReaderError as exc:
                observed.append((type(exc).__name__, exc.code))
            observed.append(self.integrity_fault)
            try:
                self.dispatch(method, arguments)
            except ReaderError as exc:
                observed.append((type(exc).__name__, exc.code))
        return original(self, check)

    monkeypatch.setattr(ReflectionReadSession, "verify_admission", damage_then_read)
    result = await evolve(
        root / "manifest.json", run, adapter_factory=lambda *a, **k: FakeAdapter()
    )
    assert observed == [
        "deleted",
        ("ReaderIntegrityError", "diagnostic_bundle_changed"),
        "diagnostic_bundle_changed",
        ("ReaderIntegrityError", "diagnostic_bundle_changed"),
    ]
    assert (result.terminal, result.owner, result.code) == (
        "persistence_failed",
        "artifact_store",
        "artifact_digest_mismatch",
    )
    rows = _records(run / "campaign.jsonl")
    assert sum(r["event"] == f"{role}_started" for r in rows) == 1
    assert sum(r["event"] == f"{role}_failed" for r in rows) == 1
    assert not any(r["event"] == f"{role}_finished" for r in rows)
    assert verify_evidence(run)["mismatches"]
