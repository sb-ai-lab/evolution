"""Own sealed Candidate transfer and real missing-only completion."""

import copy
import hashlib
from dataclasses import replace

import pytest

from evolution.tree.baseline_import import FrozenSourceCorrupt
from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json
from tests.test_tree_baseline_import import source_measurement, target_identity

pytestmark = pytest.mark.slow


def prepared_source(**kwargs):
    source = source_measurement(candidate_digest="c" * 64, **kwargs)
    raw = b"".join(r.encode() for r in source.records)
    source = replace(source, prefix_digest=hashlib.sha256(raw).hexdigest(), valid_bytes=len(raw))
    identity = source.records[0].payload["identity"]
    seal = dict(
        format="comparison-data/1",
        kind="gate_seal",
        measurement=dict(
            measurement_id=source.measurement_id,
            identity=identity,
            identity_digest=source.identity_digest,
            prefix=dict(
                version=1,
                kind="journal_prefix",
                run_id="measurement-run",
                locator=f"measurements/{source.measurement_id}/events.jsonl#bytes=0:{len(raw)}",
                content_digest=source.prefix_digest,
            ),
        ),
        slots_digest=hashlib.sha256(canonical_json(identity["schedule"])).hexdigest(),
    )
    return source, seal


def destination(identity=None, **kwargs):
    from evolution.tree.candidate_completion import MeasurementBinding

    return MeasurementBinding(
        measurement_id="m-r01-completion-a01",
        identity=identity or target_identity(cases=["c", "d"], candidate_digest="c" * 64),
        primary="score",
        **kwargs,
    )


def test_completion_imports_own_gate_and_preserves_missing_schedule():
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source()
    admitted = prepare_candidate_completion(source, destination(), gate_seal=seal)
    assert (admitted.imported, admitted.scheduled) == (1, 2)
    assert admitted.rows[0]["source"]["run_id"] == source.run_id
    assert admitted.rows[0]["source"]["measurement_id"] == source.measurement_id


@pytest.mark.parametrize(
    "field", ["candidate_digest", "scorer", "corpus_digest", "metric_plan_digest", "row_provenance"]
)
def test_completion_rejects_changed_identity(field):
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source()
    payload = target_identity(cases=["c", "d"], candidate_digest="c" * 64).to_payload()
    payload[field] = (
        {**payload[field], "digest": "changed"}
        if field == "scorer"
        else 1
        if field == "row_provenance"
        else "d" * 64
    )
    with pytest.raises(FrozenSourceCorrupt):
        prepare_candidate_completion(
            source, destination(RunIdentityV1.from_payload(payload)), gate_seal=seal
        )


@pytest.mark.parametrize("change", ["unsealed", "prefix", "slots", "unscored"])
def test_completion_never_remeasures_invalid_sealed_gate(change):
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source(
        rows={("c", "01"): "time_budget"} if change == "unscored" else None
    )
    target = destination()
    seal = copy.deepcopy(seal)
    if change == "unsealed":
        seal = {}
    elif change == "prefix":
        source = replace(source, records=source.records[:-1])
    elif change == "slots":
        seal["slots_digest"] = "0" * 64
    with pytest.raises(FrozenSourceCorrupt):
        prepare_candidate_completion(source, target, gate_seal=seal)


def test_completion_preserves_condition_fields_and_original_execution():
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source()
    source = replace(source, condition_fields=("environment",))
    admitted = prepare_candidate_completion(
        source, destination(condition_fields=("target",)), gate_seal=seal
    )
    assert admitted.rows[0]["source"]["condition_fields"] == ["environment", "target"]


async def completion_run(
    tmp_path, monkeypatch, *, verification="reuse", stability="stable", stop=None, **options
):
    import json

    from tests import campaign_drivers as fixtures
    from tests.test_tree_campaign_family15_gate import gate_run

    original = fixtures.evolve

    async def configured(path, *args, **kwargs):
        document = json.loads(path.read_text())
        document["campaign"]["gate"].update(stability=stability, verification=verification)
        path.write_text(json.dumps(document))
        return await original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", configured)
        return await gate_run(
            tmp_path,
            patch,
            stop=stop
            or (
                "after_candidate_completion_completed"
                if verification == "reuse"
                else "after_final_candidate_completed"
            ),
            campaign_options=options.pop("campaign_options", {"attempts_per_round": 1}),
            **options,
        )


@pytest.mark.parametrize("verification", ["reuse", "fresh"])
async def test_real_candidate_completion_precedes_global_work(tmp_path, monkeypatch, verification):
    from evolution.tree.campaign_projections import verify_evidence
    from tests.campaign_drivers import _records

    run = await completion_run(tmp_path, monkeypatch, verification=verification)
    events = _records(run / "campaign.jsonl")
    names = [r["event"] for r in events]
    assert names.index("candidate_gate_sealed") < names.index("final_measurement_bound")
    assert "global_comparison_completed" not in names
    audit = verify_evidence(run)
    assert audit["mismatches"] == [], audit
    if verification == "reuse":
        completed = next(
            r["payload"] for r in events if r["event"] == "candidate_completion_completed"
        )
        assert completed["required"] == completed["admissible"] == 2
    else:
        seal = next(r["payload"] for r in events if r["event"] == "candidate_gate_sealed")
        final = next(r["payload"] for r in events if r["event"] == "final_measurement_completed")
        gate_rows = _records(
            run / "measurements" / seal["source"]["measurement_id"] / "events.jsonl"
        )
        final_rows = _records(
            run / "measurements" / final["source"]["measurement_id"] / "events.jsonl"
        )
        assert (
            gate_rows[0]["payload"]["identity"]["schedule"]
            != final_rows[0]["payload"]["identity"]["schedule"]
        )
        assert {
            s["requested_seed"] for s in gate_rows[0]["payload"]["identity"]["schedule"]
        }.isdisjoint(
            {s["requested_seed"] for s in final_rows[0]["payload"]["identity"]["schedule"]}
        )


@pytest.mark.parametrize(
    "verification,repeats,expected",
    [("reuse", 1, 133), ("fresh", 1, 266), ("reuse", 3, 399), ("fresh", 3, 798)],
)
async def test_133_case_paid_candidate_checkpoint(
    tmp_path, monkeypatch, verification, repeats, expected
):
    from tests.campaign_drivers import _records

    run = await completion_run(
        tmp_path,
        monkeypatch,
        verification=verification,
        repeats=repeats,
        extra_cases={f"c{i}": {"critical": True, "pass": True} for i in range(3, 134)},
    )
    events = _records(run / "campaign.jsonl")
    seal = next(r["payload"] for r in events if r["event"] == "candidate_gate_sealed")
    final = next(r["payload"] for r in events if r["event"] == "final_measurement_bound")
    mids = [seal["source"]["measurement_id"], final["candidate"]["measurement_id"]]
    journals = [_records(run / "measurements" / mid / "events.jsonl") for mid in mids]
    assert len(journals[0][0]["payload"]["identity"]["schedule"]) == 133 * repeats
    assert sum(r["event"] == "rollout_started" for rows in journals for r in rows) == expected
    assert sum(r["event"] == "sample_imported" for rows in journals for r in rows) == (
        133 * repeats if verification == "reuse" else 0
    )
    baseline = _records(run / "measurements" / "m-r00-champion" / "events.jsonl")
    assert sum(r["event"] == "rollout_started" for r in baseline) == 133 * repeats
    assert not any(r["event"] == "parent_measurement_bound" for r in events)
    assert sum(r["event"] == "connection_preflight_started" for r in events) == 1


@pytest.mark.parametrize("round_stop", ["first_accepted", "all_attempts"])
async def test_unstable_declares_champion_before_candidate_final(tmp_path, monkeypatch, round_stop):
    from tests.campaign_drivers import _records

    run = await completion_run(
        tmp_path,
        monkeypatch,
        stability="unstable",
        stop="after_independent_final_bound",
        campaign_options={"attempts_per_round": 2, "round_stop": round_stop},
    )
    events = _records(run / "campaign.jsonl")
    names = [r["event"] for r in events]
    assert (
        names.index("champion_final_bound")
        < names.index("candidate_completion_bound")
        < names.index("final_measurement_bound")
    )
    if round_stop == "all_attempts":
        assert names.count("attempt_closed") == 2
        assert names.index("finalists_bound") < names.index("champion_final_bound")
    champion = next(r["payload"] for r in events if r["event"] == "champion_final_bound")
    assert not (run / "measurements" / champion["measurement_id"] / "events.jsonl").exists()


def test_completion_uses_authenticated_physical_prefix_not_reencoding(tmp_path):
    import json

    from evolution.tree.baseline_import import read_frozen_source
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source(measurement_id="measurement-run")
    path = tmp_path / "measurements" / source.measurement_id / "events.jsonl"
    path.parent.mkdir(parents=True)
    physical = b"".join(
        (json.dumps(r.to_payload(), sort_keys=False) + "\n").encode() for r in source.records
    )
    path.write_bytes(physical)
    seal["measurement"]["prefix"].update(
        locator=f"measurements/{source.measurement_id}/events.jsonl#bytes=0:{len(physical)}",
        content_digest=hashlib.sha256(physical).hexdigest(),
    )
    read = read_frozen_source(
        tmp_path,
        source.run_id,
        source.measurement_id,
        expected_identity_digest=source.identity_digest,
    )
    admitted = prepare_candidate_completion(read, destination(), gate_seal=seal)
    assert admitted.source_prefix_digest == hashlib.sha256(physical).hexdigest()
    assert admitted.imported == 1


async def test_subset_completion_dispatches_only_missing_candidate_slots(tmp_path, monkeypatch):
    from tests.campaign_drivers import _only_c1, _records

    run = await completion_run(tmp_path, monkeypatch, used=_only_c1)
    records = _records(run / "campaign.jsonl")
    bound = next(r["payload"] for r in records if r["event"] == "candidate_completion_bound")
    assert (bound["imported"], bound["missing_count"]) == (1, 1)
    rows = _records(run / "measurements" / bound["measurement_id"] / "events.jsonl")
    assert [r["payload"]["case_id"] for r in rows if r["event"] == "sample_imported"] == ["c1"]
    assert [r["payload"]["case_id"] for r in rows if r["event"] == "rollout_started"] == ["c2"]


@pytest.mark.parametrize("seed_mode", ["exact", "unpaired"])
def test_fresh_identity_namespace_survives_epoch_without_inventing_seeds(seed_mode):
    from evolution.tree.campaign_identity import fresh_final_identity

    gate = target_identity(candidate_digest="c" * 64, seed_mode=seed_mode)
    fresh = fresh_final_identity(gate, "m-r01-a01-final", 1)
    rebound = fresh_final_identity(gate, "m-r01-a01-final@e1", 1)
    assert fresh == rebound and fresh != gate
    assert fresh.to_payload()["schedule"][0]["requested_seed"] == (
        3 if seed_mode == "exact" else None
    )


async def test_audit_converts_sealed_source_admission_failure_to_finding(tmp_path, monkeypatch):
    import evolution.tree.candidate_completion as completion
    from evolution.tree.campaign_projections import verify_evidence

    run = await completion_run(tmp_path, monkeypatch)

    def corrupt(source, destination, *, gate_seal):
        raise FrozenSourceCorrupt(source.measurement_id, "candidate_gate_seal_mismatch")

    monkeypatch.setattr(completion, "prepare_candidate_completion", corrupt)
    findings = verify_evidence(run)["mismatches"]
    assert any(
        m["code"] == "family15_evidence_mismatch" and m["event"] == "candidate_completion_bound"
        for m in findings
    )


@pytest.mark.parametrize(
    "change", [{"dependency_digest": "new-deps"}, {"seed_mode": "unpaired"}, {"seed": 7}]
)
def test_completion_destination_nonadmission_is_not_source_corruption(change):
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source()
    target = destination(
        target_identity(cases=["c", "d"], candidate_digest="c" * 64, **change),
        condition_fields=("target",),
    )
    admitted = prepare_candidate_completion(source, target, gate_seal=seal)
    assert admitted.imported == 0
    assert admitted.scheduled == 2


@pytest.mark.parametrize("change", [{"dependency_digest": "new-deps"}, {"seed_mode": "unpaired"}])
def test_incompatible_destination_does_not_hide_invalid_original_gate_coverage(change):
    from evolution.tree.candidate_completion import prepare_candidate_completion

    source, seal = prepared_source(rows={("c", "01"): "time_budget"})
    target = destination(target_identity(cases=["c", "d"], candidate_digest="c" * 64, **change))
    with pytest.raises(FrozenSourceCorrupt):
        prepare_candidate_completion(source, target, gate_seal=seal)


@pytest.mark.parametrize("retained", [0, 1])
async def test_audit_rejects_arbitrary_omission_of_admissible_gate_rows(
    tmp_path, monkeypatch, retained
):
    from evolution.tree.baseline_import import import_pairs_digest
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import verify_evidence

    original = _Campaign.independent_completion_import

    def omit(self, *args, **kwargs):
        admitted = original(self, *args, **kwargs)
        rows = admitted.rows[:retained]
        return replace(
            admitted,
            rows=rows,
            imported=len(rows),
            pairs_digest=import_pairs_digest(rows),
            origin_runs=int(bool(rows)),
        )

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "independent_completion_import", omit)
        run = await completion_run(tmp_path, patch)
    findings = verify_evidence(run)["mismatches"]
    assert any(
        m["code"] == "family15_evidence_mismatch" and m["event"] == "candidate_completion_bound"
        for m in findings
    ), findings
