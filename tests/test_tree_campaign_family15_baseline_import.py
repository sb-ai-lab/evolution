"""An evaluation's authenticated diagnostics must remain usable after import."""

import json
import shutil

import pytest

from evolution.tree import baseline_diagnostics as diagnostics
from evolution.tree.artifacts.artifacts import ArtifactStore
from evolution.tree.baseline_import import SourceMeasurement, admit_rows
from evolution.tree.campaign_identity import CampaignIdentityV1, measurement_identity
from evolution.tree.evaluation import evaluate
from evolution.tree.evolve import evolve
from evolution.tree.execution.corpus_partition import load_partition
from evolution.tree.execution.journal import inspect_journal
from evolution.tree.manifest.manifest_v2 import load_manifest_v2
from tests.campaign_drivers import TWO_CASE_SCORER, _records
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_campaign_family15_gate import gate_run

pytestmark = pytest.mark.slow

VERDICT_ONLY_SCORER = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.5')


async def evaluated_project(
    tmp_path, monkeypatch, *, seed_mode="exact", components=None, used=None
):
    work = tmp_path / "project"
    await gate_run(
        work,
        monkeypatch,
        stop="after_baseline_bound",
        scorer=VERDICT_ONLY_SCORER,
        adapter=FakeAdapter(seed_mode=seed_mode, components=components, used=used),
        campaign_options={"attempts_per_round": 1},
    )
    manifest_path = work / "manifest.json"
    document = json.loads(manifest_path.read_text())
    manifest = load_manifest_v2(manifest_path)
    config = {
        key: document[key] for key in ("target", "artifacts", "environment", "scorer", "corpus")
    }
    config["scorer"] = {
        key: value for key, value in config["scorer"].items() if key != "metric_contract"
    }
    policy = document["campaign"]
    config.update(
        schema="evolution-v2-evaluation/1",
        snapshot={"root": document["seed_root"]},
        split="train",
        repeats=1,
        random_seed=policy["random_seed"],
        max_concurrency=policy["max_concurrency"],
        max_scorer_concurrency=policy["max_scorer_concurrency"],
        metric={"primary": policy["metric"]["primary"]},
        budget=policy["budget"],
        sample_retries=manifest.workflow.campaign.sample_retries,
        call_retries=policy["call_retries"],
    )
    evaluation_path = work / "evaluation.json"
    evaluation_path.write_text(json.dumps(config))
    source_root = work / "evaluation-run"
    result = await evaluate(
        evaluation_path,
        source_root,
        adapter_factory=lambda *args: FakeAdapter(
            seed_mode=seed_mode, components=components, used=used
        ),
    )
    assert result["state"] == "completed"
    return manifest_path, source_root, result


async def test_evaluation_source_feeds_the_import_bundle(tmp_path, monkeypatch):
    manifest_path, source_root, result = await evaluated_project(tmp_path, monkeypatch)
    run = manifest_path.parent / "run"
    manifest = load_manifest_v2(manifest_path)
    policy = json.loads(manifest_path.read_text())["campaign"]
    created = _records(run / "campaign.jsonl")[0]["payload"]
    identity = CampaignIdentityV1.from_payload(created["identity"])
    corpus = load_partition(manifest.workflow.corpus_path).select("train").cases
    target = measurement_identity(
        identity, corpus, created["identity"]["artifact_store"]["seed_candidate_digest"], "champion"
    )
    evidence = inspect_journal(source_root / "measurements" / "evaluation")
    source = SourceMeasurement(
        run_id=result["run_id"],
        measurement_id="evaluation",
        identity_digest=evidence.records[0].payload["identity_digest"],
        prefix_digest=evidence.prefix_digest,
        records=evidence.records,
    )
    assert (
        source.records[0].payload["identity"]["environment"] == target.to_payload()["environment"]
    )
    assert source.records[0].payload["identity"]["scorer"] == target.to_payload()["scorer"]
    finished = [row for row in source.records if row.event == "scoring_finished"]
    assert len(finished) == 2
    assert all(row.payload["critical_pass"] is True for row in finished)
    admitted = admit_rows(source, target, primary=policy["metric"]["primary"], condition_changed=())
    assert admitted.imported == admitted.scheduled == 2
    slots = []
    original = diagnostics.prepare_diagnostics

    def capture(admitted_slots, **kwargs):
        slots.extend(admitted_slots)
        return original(admitted_slots, **kwargs)

    monkeypatch.setattr(diagnostics, "prepare_diagnostics", capture)
    with ArtifactStore.resume(run, manifest.policy) as store:
        candidate = store.champion()
        prepared = diagnostics.prepare_import_diagnostics(
            source,
            admitted.rows,
            target=target,
            source_root=source_root,
            destination=tmp_path / "bundle",
            corpus=corpus,
            candidate=candidate.candidate_view(),
        )
    assert len(slots) == 2 and all(
        isinstance(slot, diagnostics.DiagnosticSlot) for slot in slots
    ), slots
    assert prepared.included == prepared.gaps.scheduled == 2, dict(prepared.gaps.counts)
    entries = json.loads(prepared.index_path.read_bytes())["entries"]
    payload = prepared.payload_path.read_bytes()
    verdicts = {}
    for entry in entries:
        section = entry["sections"]["stdout"]
        stdout = payload[section["offset"] : section["offset"] + section["length"]]
        score = json.loads(stdout)
        assert score["critical_pass"] is True
        verdicts[entry["destination"]["case_id"]] = score["verdicts"]
    assert verdicts == {
        "c1": [{"criterion": "answered", "passed": False}],
        "c2": [{"criterion": "answered", "passed": True}],
    }


@pytest.mark.parametrize("traces", [True, False], ids=["traced", "missing-traces"])
async def test_import_verdict_only_failures_diagnosed(tmp_path, monkeypatch, traces):
    manifest_path, source_root, _ = await evaluated_project(tmp_path, monkeypatch)
    if not traces:
        shutil.rmtree(source_root / "scorer-traces")
    document = json.loads(manifest_path.read_text())
    document["baseline_import"] = {"source": str(source_root)}
    manifest_path.write_text(json.dumps(document))
    destination = manifest_path.parent / "import-run"
    await evolve(manifest_path, destination, adapter_factory=lambda *args: FakeAdapter())
    records = _records(destination / "campaign.jsonl")
    bound = next(
        row["payload"]["measurement_id"] for row in records if row["event"] == "baseline_bound"
    )
    baseline = _records(destination / "measurements" / bound / "events.jsonl")
    assert sum(row["event"] == "sample_imported" for row in baseline) == 2
    assert not any(row["event"] == "rollout_started" for row in baseline)
    declaration = next(
        row["payload"] for row in records if row["event"] == "diagnostic_import_declared"
    )
    assert declaration["scheduled"] == 2
    attempts = [row for row in records if row["event"] == "attempt_opened"]
    if traces:
        assert declaration["included"] == 2
        # An Attempt opens only after a nonempty focus has been selected.
        assert attempts
    else:
        assert declaration["included"] == 0
        assert declaration["sufficiency"] == "unavailable"
        assert not attempts
        assert (
            next(row["payload"]["outcome"] for row in records if row["event"] == "round_closed")
            == "stalled"
        )


@pytest.mark.parametrize("kind", ["core", "missing-traces", "standalone", "scorer-differs"])
async def test_import_diagnostics_family15(tmp_path, monkeypatch, kind):
    from evolution.tree.artifacts.artifact_projections import AuxiliaryPublication
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.output_admission import EvolveRefused
    from tests.test_tree_campaign_baseline_import import accept_import_finding, write_lab_source

    path, source, _ = await evaluated_project(tmp_path, monkeypatch)
    if kind == "missing-traces":
        shutil.rmtree(source / "scorer-traces")
    elif kind == "standalone":
        standalone = write_lab_source(path, source)
        traces = standalone / "scorer-traces"
        shutil.copytree(source / "scorer-traces", traces)
        from evolution.tree.execution.scorer_traces import encode_pack_record

        raw = (traces / "evaluation.pack").read_bytes()
        end = raw.index(b"\n")
        header = json.loads(raw[:end])
        header.pop("hsha")
        header["pack_id"] = "lab-fixed-evaluation"
        # The standalone fixture owns a new namespace; retain exact request/output bodies.
        assert raw[end + 1 : end + 2] == b"\n"
        (traces / "lab-fixed-evaluation.pack").write_bytes(
            encode_pack_record(header) + raw[end + 2 :]
        )
        (traces / "evaluation.pack").unlink()
        source = standalone
    elif kind == "scorer-differs":
        scorer = path.parent / "scorer/score.py"
        scorer.write_text(scorer.read_text() + "\n# another bound scorer\n")
    document = json.loads(path.read_text())
    document["baseline_import"] = {"source": str(source)}
    path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    decisions = []
    if kind == "scorer-differs":
        with pytest.raises(EvolveRefused) as caught:
            await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
        decisions = [accept_import_finding(item) for item in caught.value.findings]
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), authorizations=decisions)
    records = _records(run / "campaign.jsonl")
    declared = next(
        row["payload"] for row in records if row["event"] == "diagnostic_import_declared"
    )
    bundle = AuxiliaryPublication.from_payload(declared["bundle"])
    gaps = json.loads((run / bundle.locator / "gaps.json").read_bytes())
    complete = kind in ("core", "standalone")
    assert declared["sufficiency"] == ("complete" if complete else "unavailable"), gaps
    assert declared["included"] == (2 if complete else 0), gaps
    if not complete:
        reason = "projection_mismatch" if kind == "scorer-differs" else "missing"
        assert gaps["counts"][reason] == 2
    carrier = AuxiliaryPublication.from_payload(declared["data"])
    data = json.loads((run / carrier.locator / "payload.json").read_bytes())
    locator = data["source_prefixes"][0]["prefix"]["locator"]
    assert locator.startswith(
        "events.jsonl#bytes="
        if kind == "standalone"
        else "measurements/evaluation/events.jsonl#bytes="
    )
    source.rename(source.with_name("moved-source"))
    assert not verify_evidence(run)["mismatches"]
