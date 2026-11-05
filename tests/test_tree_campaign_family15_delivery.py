"""Public /16 campaigns cross the real admission, recovery and privacy boundaries."""

import copy
import json
import shutil
from collections import Counter

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import evolve, exit_code_for
from tests.campaign_drivers import TWO_CASE_SCORER, _records
from tests.campaign_drivers import two_case_run as _two_case_run
from tests.campaign_fakes import family14_document
from tests.family15_fakes import v16_document

pytestmark = pytest.mark.slow


class PowerCut(BaseException):
    pass


async def public_run(
    root,
    monkeypatch,
    *,
    gate=None,
    cut=None,
    campaign=None,
    configure=None,
    **options,
):
    """Use real evolve admission; replace only offline fixture inputs/executors."""
    from evolution.tree.campaign import _Campaign
    from tests import campaign_drivers as fixtures

    results = []
    original_hook = _Campaign.hook

    async def configured(path, *args, **kwargs):
        document = family14_document(json.loads(path.read_text()), repeats=1)
        document["schema"] = "evolution-v2-manifest/16"
        document["campaign"]["baseline_refresh_after_rounds"] = 0
        document["campaign"]["gate"] = copy.deepcopy(v16_document()["campaign"]["gate"])
        document["campaign"]["gate"].update(copy.deepcopy(gate or {}))
        if campaign:
            document["campaign"].update(campaign)
        if configure:
            configure(document)
        path.write_text(json.dumps(document))
        result = await evolve(path, *args, **kwargs)
        results.append(result)
        return result

    def hook(self, name):
        original_hook(self, name)
        if name == cut:
            raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", configured)
        patch.setattr(_Campaign, "hook", hook)
        await _two_case_run(root, terminal=None, campaign={"attempts_per_round": 1}, **options)
    return root / "run", results[-1]


def durable_facts(run):
    """Compare paid work and durable decisions, independent of UUIDs/clock/path."""
    records = _records(run / "campaign.jsonl")
    events = Counter(
        r["event"]
        for r in records
        if r["event"] not in {"instance_attested", "authorization_applied"}
    )
    measurements = {}
    for path in (run / "measurements").glob("*/events.jsonl"):
        rows = _records(path)
        measurements[path.parent.name] = {
            "paid": sum(r["event"] == "rollout_started" for r in rows),
            "imports": sum(r["event"] == "sample_imported" for r in rows),
            "scores": sorted(
                (r["payload"]["sample_id"], json.dumps(r["payload"]["metrics"], sort_keys=True))
                for r in rows
                if r["event"] in {"scoring_finished", "sample_imported"}
            ),
        }
    decisions = [
        (r["event"], r["payload"].get("outcome"), r["payload"].get("reason"))
        for r in records
        if r["event"]
        in {
            "comparison_completed",
            "global_comparison_completed",
            "selection_decided",
            "round_closed",
            "campaign_finished",
        }
    ]
    ledger = Counter(
        r["event"] for r in _records(run / "artifacts.jsonl") if r["event"].endswith("committed")
    )
    return events, measurements, decisions, ledger


@pytest.mark.parametrize(
    "gate,campaign",
    [
        ({}, {}),
        ({"execution_noise": {"kind": "observed"}}, {}),
        ({"stability": "unstable", "verification": "fresh", "fresh_parent": True}, {}),
        ({}, {"round_stop": "all_attempts", "attempts_per_round": 2}),
        (
            {
                "mode": "confidence",
                "focus": {"margin": -0.9, "confidence": 0.1},
                "causal": {"tolerance": 1, "confidence": 0.1},
                "statistics": {"method": "bounded-weighted-mean/1", "unit": "case"},
            },
            {},
        ),
    ],
)
async def test_public_r1_campaigns_finish_with_complete_auditable_evidence(
    tmp_path, monkeypatch, gate, campaign
):
    run, result = await public_run(tmp_path, monkeypatch, gate=gate, campaign=campaign)
    assert result.terminal == "finished" and exit_code_for(result) == 0
    records = _records(run / "campaign.jsonl")
    assert records[0]["payload"]["family_version"] == 15
    assert any(r["event"] == "selection_decided" for r in records)
    assert not any(r["event"] in {"campaign_suspended", "noise_calibration_bound"} for r in records)
    assert verify_evidence(run)["mismatches"] == []
    if gate.get("execution_noise", {}).get("kind") == "observed":
        bodies = [
            json.loads(p.read_bytes()) for p in (run / "artifacts/auxiliary").glob("*/payload.json")
        ]
        comparisons = [b for b in bodies if b["kind"] == "result"]
        assert comparisons and all(b["variation"] == "unavailable" for b in comparisons)
        assert all(
            d["unavailable"] == "insufficient_repeats"
            for b in comparisons
            for d in b["repeat_diagnostics"]
        )


@pytest.mark.parametrize(
    "verification,cut",
    [
        ("reuse", "after_comparison_data_declared"),
        ("reuse", "after_comparison_data_published"),
        ("reuse", "after_comparison_bound"),
        ("reuse", "after_gate_measurement_completed"),
        ("reuse", "after_comparison_completed"),
        ("reuse", "after_parent_measurement_bound"),
        ("reuse", "after_candidate_gate_sealed"),
        ("reuse", "after_finalists_bound"),
        ("reuse", "after_candidate_completion_bound"),
        ("reuse", "before_candidate_completion_birth"),
        ("reuse", "after_candidate_completion_birth"),
        ("reuse", "after_candidate_completion_completed"),
        ("fresh", "after_independent_final_bound"),
        ("fresh", "after_candidate_final_birth"),
        ("fresh", "after_final_candidate_completed"),
        ("fresh", "after_champion_final_bound"),
        ("fresh", "after_final_champion_completed"),
        ("reuse", "after_global_comparison_completed"),
        ("reuse", "after_selection_decided"),
        ("reuse", "after_round_closed"),
    ],
)
async def test_public_cut_restart_matches_uncut_durable_facts(
    tmp_path, monkeypatch, verification, cut
):
    gate = {"verification": verification}
    if cut == "after_parent_measurement_bound":
        gate["fresh_parent"] = True
    if cut in {"after_champion_final_bound", "after_final_champion_completed"}:
        gate["stability"] = "unstable"
    control, _ = await public_run(tmp_path / "control", monkeypatch, gate=gate)
    with pytest.raises(PowerCut):
        await public_run(tmp_path / "cut", monkeypatch, gate=gate, cut=cut)
    run, result = await public_run(tmp_path / "cut", monkeypatch, gate=gate)
    assert result.terminal == "finished"
    assert durable_facts(run) == durable_facts(control)
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize("missing", [False, True])
async def test_public_link_imports_diagnostics_or_reports_gap_without_hidden_refresh(
    tmp_path, monkeypatch, missing
):
    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.5')
    source, _ = await public_run(tmp_path / "source", monkeypatch, scorer=scorer)
    if missing:
        shutil.rmtree(source / "scorer-traces")
    run, _ = await public_run(
        tmp_path / "successor",
        monkeypatch,
        scorer=scorer,
        linked_from=(source, source.parent / "seed"),
    )
    records = _records(run / "campaign.jsonl")
    diagnostic = next(r["payload"] for r in records if r["event"] == "diagnostic_import_declared")
    assert (diagnostic["scheduled"], diagnostic["included"]) == (2, 0 if missing else 2)
    baseline = _records(run / "measurements/m-r00-champion/events.jsonl")
    assert sum(r["event"] == "sample_imported" for r in baseline) == 2
    assert not any(r["event"] == "rollout_started" for r in baseline)
    if missing:
        assert diagnostic["sufficiency"] == "unavailable"
        assert not any(r["event"] == "attempt_opened" for r in records)
    assert verify_evidence(run)["mismatches"] == []


async def test_public_noise_source_secret_is_refused_before_creation(tmp_path, monkeypatch):
    from evolution.tree.evolve import EvolveRefused

    secret = "TASK13-PRIVATE-NOISE-CANARY"
    monkeypatch.setenv("TASK13_TOKEN", secret)

    def configure(document):
        document["scorer"]["allowed_env"] = ["TASK13_TOKEN"]

    with pytest.raises(EvolveRefused) as caught:
        await public_run(
            tmp_path,
            monkeypatch,
            configure=configure,
            gate={"execution_noise": {"kind": "bounded", "allowance": 0.01, "source": secret}},
        )
    assert not (tmp_path / "run/campaign.jsonl").exists()
    assert caught.value.code == "record_matches_secret"
    assert secret not in str(caught.value)


async def test_public_candidate_description_secret_fails_resumably_and_resumes(
    tmp_path, monkeypatch
):
    """#287 under /16: the Candidate's description holds a forwarded Scorer
    value. `evolve` ends `persistence_failed` before the stage opens, names the
    manifest path, writes the value nowhere; repaired, the same command finishes."""
    from tests.test_tree_campaign import (
        DESCRIBED_SECRET,
        DESCRIPTION_SECRET_DETAIL,
        REPAIRED_DESCRIPTION,
        CandidateDescribing,
        assert_nowhere,
        candidate_instances,
    )

    monkeypatch.setenv("JUDGE", DESCRIBED_SECRET)

    def configure(document):
        document["scorer"]["allowed_env"] = sorted(
            {*document["scorer"].get("allowed_env", ()), "JUDGE"}
        )

    adapter = CandidateDescribing({"model": DESCRIBED_SECRET}, first={"model": "clean"})
    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
    assert (result.terminal, result.owner, result.code, result.fault_detail) == (
        "persistence_failed",
        "campaign_journal",
        "record_matches_secret",
        DESCRIPTION_SECRET_DETAIL,
    )
    assert exit_code_for(result) == 1
    records = _records(run / "campaign.jsonl")
    refused = candidate_instances(adapter)
    assert len(refused) == 2
    assert not refused & {
        r["payload"]["instance_id"] for r in records if r["event"] == "instance_attested"
    }
    assert not {"campaign_finished", "campaign_suspended"} & {r["event"] for r in records}
    assert_nowhere(tmp_path, DESCRIBED_SECRET)

    repaired = CandidateDescribing(REPAIRED_DESCRIPTION)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=repaired)
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    attested = [
        r["payload"] for r in _records(run / "campaign.jsonl") if r["event"] == "instance_attested"
    ]
    assert not refused & {row["instance_id"] for row in attested}
    assert any(row.get("description") == REPAIRED_DESCRIPTION for row in attested)
    assert verify_evidence(run)["mismatches"] == []
    assert_nowhere(tmp_path, DESCRIBED_SECRET)


def test_offline_tutorial_commands_evolve_watch_trace(tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path

    from typer.testing import CliRunner

    from evolution.tree import evolve as module
    from evolution.tree.cli import tree_app
    from tests.campaign_fakes import FakeAdapter

    # Prepare complete on-disk offline role programs/corpus using the same
    # inputs as public delivery, then execute the guide's commands through CLI.
    asyncio.run(public_run(tmp_path, monkeypatch))
    # Execute the maintained JSON example as configuration, not a text check.
    guide = (Path(__file__).parents[1] / "docs/manifest-v2.md").read_text()
    fragment = (
        guide.partition("### Independent controls (manifest /16)")[2]
        .partition("```json\n")[2]
        .partition("```")[0]
    )
    manifest = tmp_path / "manifest.json"
    document = json.loads(manifest.read_text())
    document["campaign"].update(json.loads(fragment))
    manifest.write_text(json.dumps(document))
    monkeypatch.setattr(module, "_http_session_adapter", lambda *a, **kw: FakeAdapter())
    runner = CliRunner()
    run = tmp_path / "runs/run-16"
    run.parent.mkdir()
    commands = [
        ["evolve", "--manifest", str(tmp_path / "manifest.json"), "--run-dir", str(run)],
        ["watch", "--run-dir", str(run), "--once"],
        ["trace", "--run-dir", str(run), "--out", str(tmp_path / "run-16.jsonl")],
    ]
    for command in commands:
        result = runner.invoke(tree_app, command)
        assert result.exit_code == 0, result.output
    assert (tmp_path / "run-16.jsonl").stat().st_size > 0
    assert verify_evidence(run)["mismatches"] == []


async def test_public_train_partition_and_editor_boundary(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.evolve import EvolveRefused
    from evolution.tree.roles.editor.subprocess_editor import SubprocessEditor
    from evolution.tree.roles.editor_contracts import brief_payload

    edited = []
    original = SubprocessEditor.edit

    async def capture(self, request):
        edited.append(brief_payload(request.brief))
        return await original(self, request)

    monkeypatch.setattr(SubprocessEditor, "edit", capture)

    def partition(document):
        corpus = tmp_path / document["corpus"]["path"]
        rows = [json.loads(line) for line in corpus.read_text().splitlines()]
        for row in rows:
            row["split"] = "train"
            row["private"] = {"marker": "TRAIN-PRIVATE-CANARY"}
        rows.append(
            dict(
                case_id="held-out",
                group_id="test-group",
                stratum="s1",
                split="test",
                public={"text": "TEST-PUBLIC-CANARY"},
                oracle={"private": "TEST-ORACLE-CANARY"},
            )
        )
        corpus.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    run, _ = await public_run(tmp_path, monkeypatch, configure=partition)
    created = _records(run / "campaign.jsonl")[0]["payload"]
    assert [c["case_id"] for c in created["identity"]["corpus_cases"]] == ["c1", "c2"]
    assert edited
    public = json.dumps(edited) + json.dumps(inspect_campaign(run).to_payload()["independent"])
    for canary in ("TRAIN-PRIVATE-CANARY", "TEST-PUBLIC-CANARY", "TEST-ORACLE-CANARY"):
        assert canary not in public
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(corpus.read_text().replace("TEST-ORACLE-CANARY", "CHANGED-TEST-ORACLE"))
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused):
        await evolve(tmp_path / "manifest.json", run)
    assert (run / "campaign.jsonl").read_bytes() == before


async def test_public_unavailable_causal_support_keeps_champion_without_suspension(
    tmp_path, monkeypatch
):
    from tests.campaign_drivers import LENIENT
    from tests.campaign_fakes import FakeAdapter

    class BaselineExcludesC2(FakeAdapter):
        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if candidate.kind == "champion":
                instance._lookup = lambda session: (
                    "target_failed" if session.case_id == "c2" else "completed"
                )
            return instance

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        adapter_class=BaselineExcludesC2,
        used=lambda case, turn: ["skills/SKILL.md"],
        campaign={"indeterminate": LENIENT},
    )
    assert result.terminal == "finished"
    records = _records(run / "campaign.jsonl")
    comparison = next(r["payload"] for r in records if r["event"] == "comparison_completed")
    assert (comparison["outcome"], comparison["reason"], comparison["deciding_scope"]) == (
        "comparison_unavailable",
        "empty_support",
        "causal",
    )
    assert not any(r["event"] == "campaign_suspended" for r in records)
    selection = next(r["payload"] for r in records if r["event"] == "selection_decided")
    assert selection["selected_candidate"] is None
    assert selection["reason"] == "no_eligible_candidate"
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize(
    "cut",
    [
        "after_diagnostics_declared",
        "after_diagnostics_published",
        "after_diagnostic_import_bound",
        "after_baseline_bound",
    ],
)
async def test_public_link_birth_cuts_match_uncut_durable_facts(tmp_path, monkeypatch, cut):
    scorer = TWO_CASE_SCORER.replace('"score": 1.0 if passed else 0.0', '"score": 0.5')
    source, _ = await public_run(tmp_path / "source", monkeypatch, scorer=scorer)
    options = dict(scorer=scorer, linked_from=(source, source.parent / "seed"))
    control, _ = await public_run(tmp_path / "control", monkeypatch, **options)
    with pytest.raises(PowerCut):
        await public_run(tmp_path / "cut", monkeypatch, cut=cut, **options)
    run, _ = await public_run(tmp_path / "cut", monkeypatch, **options)
    assert durable_facts(run) == durable_facts(control)
    assert verify_evidence(run)["mismatches"] == []


@pytest.mark.parametrize(
    "cut", ["after_pending", "after_started", "after_replace", "after_finished"]
)
async def test_public_promotion_transaction_cuts_match_uncut_durable_facts(
    tmp_path, monkeypatch, cut
):
    from evolution.tree.artifacts.artifacts import ArtifactStore

    control, _ = await public_run(tmp_path / "control", monkeypatch)
    ledger = Counter(r["event"] for r in _records(control / "artifacts.jsonl"))
    assert ledger["promotion_committed"] == 1
    original = ArtifactStore._hook

    def interrupted(self, name):
        original(self, name)
        if name == cut:
            raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "_hook", interrupted)
        with pytest.raises(PowerCut):
            await public_run(tmp_path / "cut", monkeypatch)
    run, result = await public_run(tmp_path / "cut", monkeypatch)
    assert exit_code_for(result) == 0
    assert durable_facts(run) == durable_facts(control)
    names = Counter(r["event"] for r in _records(run / "campaign.jsonl"))
    assert names["selection_decided"] == names["round_closed"] == 1
    ledger = Counter(r["event"] for r in _records(run / "artifacts.jsonl"))
    assert ledger["promotion_committed"] == 1
    assert verify_evidence(run)["mismatches"] == []
