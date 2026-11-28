"""Standalone scores become the first-round baseline through public admission."""

import json

import pytest

from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.evolve import evolve
from evolution.tree.execution.journal import inspect_journal
from tests.campaign_drivers import _records
from tests.campaign_fakes import FakeAdapter
from tests.test_tree_campaign_family15_baseline_import import evaluated_project
from tests.tree.manifest.test_manifest_v15 import V15_OBSERVE_GATE, v15_from

pytestmark = pytest.mark.slow


async def import_project(
    tmp_path, monkeypatch, *, schema=16, seed_mode="exact", components=None, used=None
):
    path, source, result = await evaluated_project(
        tmp_path, monkeypatch, seed_mode=seed_mode, components=components, used=used
    )
    document = json.loads(path.read_text())
    if schema == 15:
        document = v15_from(
            document,
            gate=V15_OBSERVE_GATE,
            repeats=1,
            round_stop="all_attempts",
            refresh=0,
            metric_contract=document["scorer"]["metric_contract"],
            role_visibility=document["role_visibility"],
        )
    document["baseline_import"] = {"source": str(source)}
    path.write_text(json.dumps(document))
    return path, source, result


def imported_baseline(run):
    records = _records(run / "campaign.jsonl")
    measurement = next(
        row["payload"]["measurement_id"] for row in records if row["event"] == "baseline_bound"
    )
    return records, inspect_journal(run / "measurements" / measurement).records


@pytest.mark.parametrize("schema", [15, 16])
async def test_import_core_evaluation_complete(tmp_path, monkeypatch, schema):
    path, source, evaluation = await import_project(tmp_path, monkeypatch, schema=schema)
    notes = []
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), notes=notes.append)
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["imported"] == declaration["scheduled"] == 2
    assert declaration["source"]["run_id"] == evaluation["run_id"]
    assert sum(row.event == "sample_imported" for row in rows) == 2
    assert not any(row.event == "rollout_started" for row in rows)
    assert (
        f"baseline imported from evaluation {evaluation['run_id']}: 2 pairs carried, 0 to measure"
        in notes
    )
    assert (
        "source rows not imported: 0 not_scheduled, 0 seed_differs, 0 not_scored, 0 primary_missing, 0 out_of_range, 0 dependency_differs"
        in notes
    )
    assert any("/15 cannot diagnose" in note for note in notes) == (schema == 15)


@pytest.mark.parametrize("shift", [0, 100])
async def test_exact_seed_pairing(tmp_path, monkeypatch, shift):
    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    document = json.loads(path.read_text())
    document["campaign"]["random_seed"] += shift
    path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    notes = []
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), notes=notes.append)
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["imported"] == (0 if shift else 2)
    assert declaration["not_imported"]["seed_differs"] == (2 if shift else 0)
    assert sum(row.event == "rollout_started" for row in rows) == (2 if shift else 0)


@pytest.mark.parametrize("terminal", ["suspended", "absent"])
async def test_partial_sources(tmp_path, monkeypatch, terminal):
    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    log = source / "measurements/evaluation/events.jsonl"
    rows = log.read_text().splitlines(keepends=True)
    stop = next(
        index for index, row in enumerate(rows) if json.loads(row)["event"] == "scoring_finished"
    )
    log.write_text("".join(rows[: stop + 1]))
    owner = _records(source / "evaluation.jsonl")
    owner = [row for row in owner if row["event"] != "evaluation_completed"]
    if terminal == "suspended":
        owner.append(
            {
                **owner[-1],
                "sequence": owner[-1]["sequence"] + 1,
                "event": "evaluation_suspended",
                "operation_id": None,
                "payload": {"reason": "incomplete"},
            }
        )
    (source / "evaluation.jsonl").write_text("".join(json.dumps(row) + "\n" for row in owner))
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["imported"] == 1
    assert declaration["not_imported"]["not_scored"] == 1
    assert sum(row.event == "sample_imported" for row in rows) == 1
    assert sum(row.event == "rollout_started" for row in rows) == 1


def write_lab_source(path, source, *, marker=True):
    import hashlib

    from evolution.tree.execution.corpus_partition import load_partition
    from evolution.tree.execution.journal import Journal
    from evolution.tree.execution.journal_identity import RunIdentityV1, canonical_json
    from evolution.tree.manifest.manifest_v2 import load_manifest_v2

    original = inspect_journal(source / "measurements/evaluation")
    identity = json.loads(json.dumps(original.records[0].payload["identity"]))
    identity["profile"] = "rollout_measurement"
    if not marker:
        identity.pop("row_provenance", None)
    identity["metric_plan_digest"] = hashlib.sha256(
        canonical_json({"primary": "score"})
    ).hexdigest()
    labels = {}
    for index, row in enumerate(identity["schedule"]):
        labels[row["case_id"], row["sample_id"]] = row["sample_id"] = (
            f"case-{index}-repeat-{row['repeat']}"
        )
    corpus = load_partition(load_manifest_v2(path).workflow.corpus_path).train.cases
    standalone = path.parent / "lab-evaluation"
    with Journal.create(
        standalone, "lab-fixed-evaluation", RunIdentityV1(identity), corpus
    ) as journal:
        for row in original.records[1:]:
            payload = dict(row.payload)
            if "sample_id" in payload:
                payload["sample_id"] = labels[payload["case_id"], payload["sample_id"]]
            journal.append(row.event, payload, row.to_payload()["operation_id"])
    return standalone


async def test_import_lab_journal(tmp_path, monkeypatch):
    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15, seed_mode="none")
    standalone = write_lab_source(path, source)
    document = json.loads(path.read_text())
    document["baseline_import"]["source"] = str(standalone)
    path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(seed_mode="none"))
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["source"]["kind"] == "measurement"
    assert declaration["imported"] == 2 and declaration["authorization"] is None
    assert [row.payload["sample_id"] for row in rows if row.event == "sample_imported"] == [
        "champion-01",
        "champion-01",
    ]
    assert not any(row.event == "rollout_started" for row in rows)


@pytest.mark.parametrize("change", ["budget", "environment", "corpus", "concurrency", "retries"])
async def test_caveat_fields_only(tmp_path, monkeypatch, change):
    from evolution.tree.campaign import _Campaign
    from tests.test_tree_campaign_family15_gate import GateBoundary

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    document = json.loads(path.read_text())
    expected = []
    if change == "budget":
        document["campaign"]["budget"]["max_turns"] += 1
        expected = ["identity.budget.max_turns"]
    elif change == "environment":
        document["environment"] = {"kind": "scripted"}
        expected = ["identity.environment.digest", "identity.environment.kind"]
    elif change == "corpus":
        corpus = path.parent / document["corpus"]["path"]
        row = json.loads(corpus.read_text().splitlines()[0])
        row.update(case_id="extra", group_id="extra")
        with corpus.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        expected = ["identity.corpus_cases", "identity.schedule"]
    elif change == "concurrency":
        document["campaign"]["max_scorer_concurrency"] += 1
    else:
        document["campaign"]["sample_retries"] = 2
    path.write_text(json.dumps(document))
    original = _Campaign.hook

    def stop(self, name):
        original(self, name)
        if name == "after_baseline_bound":
            raise GateBoundary()

    monkeypatch.setattr(_Campaign, "hook", stop)
    run = path.parent / "import-run"
    notes = []
    with pytest.raises(GateBoundary):
        await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), notes=notes.append)
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["fields"] == expected
    assert all(
        row.payload["source"]["condition_fields"] == expected
        for row in rows
        if row.event == "sample_imported"
    )
    assert any("condition changed between the baseline source" in note for note in notes) == bool(
        expected
    )


async def test_none_source_does_not_pair_with_exact_campaign(tmp_path, monkeypatch):
    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15, seed_mode="none")
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["imported"] == 0
    assert declaration["not_imported"]["seed_differs"] == 2
    assert sum(row.event == "rollout_started" for row in rows) == 2


def accept_import_finding(finding):
    from evolution.tree.execution.authorizations import Authorization

    return Authorization.new(finding.code, finding.instance_digest, "fixture decision")


@pytest.mark.parametrize("change", ["scorer", "metric_plan", "candidate"])
async def test_incompatible_requires_authorization(tmp_path, monkeypatch, change):
    from evolution.tree.evolve import journaled_authorization_ids
    from evolution.tree.execution.output_admission import EvolveRefused

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    document = json.loads(path.read_text())
    if change == "scorer":
        scorer = path.parent / document["scorer"]["cwd"] / "score.py"
        scorer.write_text(scorer.read_text() + "\n# changed scorer binding\n")
    elif change == "metric_plan":
        document["campaign"]["metric"]["primary"] = "other"
        path.write_text(json.dumps(document))
    else:
        seed = path.parent / document["seed_root"] / "skills/SKILL.md"
        seed.write_text(seed.read_text() + "\nChanged candidate\n")
    adapter = FakeAdapter()
    run = path.parent / "import-run"
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: adapter)
    finding = caught.value.findings[0]
    assert finding.code == "baseline_import_incompatible" and finding.path == "baseline_import"
    assert finding.effect == "import" and finding.differences
    assert not adapter.materialized and not run.exists()
    decision = accept_import_finding(finding)
    await evolve(path, run, adapter_factory=lambda *args: adapter, authorizations=[decision])
    declaration = _records(run / "campaign.jsonl")[0]["payload"]["identity"]["baseline_import"]
    assert declaration["incompatible"] == [
        {"scorer": "scorer", "metric_plan": "metric_plan_digest", "candidate": "candidate_digest"}[
            change
        ]
    ]
    assert declaration["authorization"] == decision.decision_payload()
    assert decision.id in journaled_authorization_ids(run)
    assert not verify_evidence(run)["mismatches"]


def edit_during_preflight(monkeypatch, edit):
    """Run `edit` once inside Target preflight: after the import comparison, before the build."""
    from evolution.tree import campaign as campaign_module

    original = campaign_module._Campaign.preflight

    async def preflight(self):
        edit()
        return await original(self)

    monkeypatch.setattr(campaign_module._Campaign, "preflight", preflight)


def change_seed(path):
    document = json.loads(path.read_text())
    seed = path.parent / document["seed_root"] / "skills/SKILL.md"
    seed.write_text(seed.read_text() + "\nChanged candidate\n")


def change_scorer_binding(monkeypatch):
    from evolution.tree import campaign_identity

    binding = campaign_identity.scorer_binding
    monkeypatch.setattr(
        campaign_identity,
        "scorer_binding",
        lambda scorer: dict(binding(scorer), digest="e" * 64),
    )


@pytest.mark.parametrize("schema", [15, 16])
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("change", ["candidate", "scorer"])
async def test_compared_inputs_changed_during_start_refuse(
    tmp_path, monkeypatch, change, authorized, schema
):
    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app
    from evolution.tree.execution.authorizations import read_decisions
    from evolution.tree.execution.output_admission import EvolveRefused

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=schema)
    run = path.parent / "import-run"
    runner = CliRunner()
    options = {}
    if authorized:
        change_seed(path)
        with pytest.raises(EvolveRefused) as caught:
            await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
        finding = caught.value.findings[0]
        assert finding.code == "baseline_import_incompatible"
        accepted = runner.invoke(
            tree_app,
            [
                "authorize",
                str(run),
                finding.code,
                "--digest",
                finding.instance_digest,
                "--reason",
                "fixture decision",
            ],
        )
        assert accepted.exit_code == 0, accepted.output
        options = dict(file_authorizations=read_decisions(run), decision_directory=True)
    edit_during_preflight(
        monkeypatch,
        (lambda: change_seed(path))
        if change == "candidate"
        else (lambda: change_scorer_binding(monkeypatch)),
    )
    adapter = FakeAdapter()
    with pytest.raises(EvolveRefused) as refused:
        await evolve(path, run, adapter_factory=lambda *args: adapter, **options)
    # A hard refusal: no digest to quote, so no decision can accept it.
    assert [(f.code, f.path, f.instance_digest) for f in refused.value.findings] == [
        ("baseline_import_inputs_changed", "baseline_import", None)
    ]
    expected = "identity.candidate_digest" if change == "candidate" else "identity.scorer.digest"
    assert [item.field for item in refused.value.differences] == [expected]
    assert not adapter.materialized
    assert not (run / "campaign.jsonl").exists()
    assert not (run / "measurements").exists()
    if authorized:
        assert sorted(entry.name for entry in run.iterdir()) == ["authorizations"]
        listed = runner.invoke(tree_app, ["authorize", str(run), "--list"])
        assert listed.exit_code == 0, listed.output
        assert [json.loads(line)["status"] for line in listed.output.splitlines()] == ["pending"]


async def test_inputs_changed_during_start_is_a_hard_cli_refusal(tmp_path, monkeypatch):
    """`evo tree evolve` prints the hard block and exits 2, never a traceback with exit 1."""
    import asyncio
    from contextlib import nullcontext

    from typer.testing import CliRunner

    from evolution.tree import cli as cli_module
    from evolution.tree import evolve as evolve_module
    from evolution.tree.cli import tree_app

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    run = path.parent / "import-run"
    edit_during_preflight(monkeypatch, lambda: change_seed(path))
    adapter = FakeAdapter()
    monkeypatch.setattr(evolve_module, "_http_session_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(cli_module, "_stop_flag", lambda: nullcontext(lambda: False))
    result = await asyncio.to_thread(
        CliRunner().invoke, tree_app, ["evolve", "--manifest", str(path), "--run-dir", str(run)]
    )
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    # The corpus-split notes of admission come first; the refusal block ends stderr.
    start = lines.index(
        "evo tree evolve: refused: baseline_import_inputs_changed at baseline_import"
    )
    assert lines[start + 1].startswith(
        "evo tree evolve: differs: identity.candidate_digest  journaled "
    )
    assert lines[start + 2 :] == [
        "evo tree evolve: class: hard",
        "evo tree evolve: the seed tree, scorer or metric plan changed while the campaign was "
        "starting (the differs: lines name which); nothing was written, so rerun the same "
        "command once they are stable",
    ]
    assert not adapter.materialized
    assert not (run / "campaign.jsonl").exists()


async def test_audit_row_source_tree_without_a_decision(tmp_path, monkeypatch):
    from evolution.tree.baseline_import import import_pairs_digest

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert not verify_evidence(run)["mismatches"]
    log = run / "measurements/m-r00-champion/events.jsonl"
    records = _records(log)
    assert records[1]["event"] == "sample_imported"
    records[1]["payload"]["source"]["candidate_digest"] = "0" * 64
    write_records(log, records)
    # Keep the declaration/copy hashes consistent so only the row's source tree differs.
    digest = import_pairs_digest(
        [row["payload"] for row in records if row["event"] == "sample_imported"]
    )
    rewrite_creation(run, lambda identity: identity["baseline_import"].update(pairs_digest=digest))
    findings = verify_evidence(run)["mismatches"]
    assert any(
        item["code"] == "baseline_import_mismatch"
        and item["detail"] == "an imported row was measured on another tree without a decision"
        for item in findings
    ), findings


async def test_held_out_refused_then_authorized(tmp_path, monkeypatch):
    import hashlib

    from evolution.tree.evolve import journaled_authorization_ids
    from evolution.tree.execution.authorizations import Authorization
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    owner = _records(source / "evaluation.jsonl")
    owner[0]["payload"]["identity"]["split"] = "test"
    owner[0]["payload"]["identity_digest"] = hashlib.sha256(
        canonical_json(owner[0]["payload"]["identity"])
    ).hexdigest()
    (source / "evaluation.jsonl").write_text("".join(json.dumps(row) + "\n" for row in owner))
    adapter = FakeAdapter()
    run = path.parent / "import-run"
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: adapter)
    finding = caught.value.findings[0]
    assert finding.code == "baseline_import_held_out" and finding.effect == "import"
    assert not adapter.materialized and not run.exists()
    wrong = Authorization.new("baseline_import_incompatible", finding.instance_digest, "wrong code")
    with pytest.raises(EvolveRefused, match="baseline_import_held_out"):
        await evolve(path, run, adapter_factory=lambda *args: adapter, authorizations=[wrong])
    decision = accept_import_finding(finding)
    await evolve(path, run, adapter_factory=lambda *args: adapter, authorizations=[decision])
    declaration = _records(run / "campaign.jsonl")[0]["payload"]["identity"]["baseline_import"]
    assert declaration["held_out"] == {"split": "test", "cases": []}
    assert declaration["held_out_authorization"] == decision.decision_payload()
    assert decision.id in journaled_authorization_ids(run)


async def test_import_and_hygiene_findings_keep_order(tmp_path, monkeypatch):

    from evolution.tree import cli as cli_module
    from evolution.tree.execution.output_admission import EvolveRefused

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    document = json.loads(path.read_text())
    document["campaign"]["metric"]["primary"] = "other"
    path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    run.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (run / "scorer-traces").symlink_to(foreign, target_is_directory=True)
    adapter = FakeAdapter()
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: adapter)
    assert [finding.code for finding in caught.value.findings] == [
        "run_dir_unsafe_entry",
        "baseline_import_incompatible",
    ]
    assert not adapter.materialized and not (run / "campaign.jsonl").exists()
    # The generic printer owns one combined command; no CLI invocation runs a campaign.
    output = []
    monkeypatch.setattr(cli_module.typer, "echo", lambda text, **kwargs: output.append(text))
    cli_module._print_refusal(caught.value, run, ())
    commands = [line for line in output if " --accept " in line]
    assert len(commands) == 1
    assert all(
        code in commands[0] for code in ("run_dir_unsafe_entry", "baseline_import_incompatible")
    )


async def test_lab_held_out_rows_stay_outside_the_baseline_schedule(tmp_path, monkeypatch):
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    standalone = write_lab_source(path, source)
    document = json.loads(path.read_text())
    document["baseline_import"]["source"] = str(standalone)
    path.write_text(json.dumps(document))
    corpus = path.parent / document["corpus"]["path"]
    cases = [json.loads(line) for line in corpus.read_text().splitlines()]
    for index, case in enumerate(cases):
        case.update(split="test" if index else "train", group_id=f"group-{index}")
    corpus.write_text("".join(json.dumps(case) + "\n" for case in cases))
    run = path.parent / "import-run"
    adapter = FakeAdapter()
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: adapter)
    assert not adapter.materialized
    decision = accept_import_finding(caught.value.findings[0])
    await evolve(path, run, adapter_factory=lambda *args: adapter, authorizations=[decision])
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    assert declaration["held_out"] == {"split": None, "cases": ["c2"]}
    assert declaration["imported"] == 1 and declaration["not_imported"]["not_scheduled"] == 1
    assert [row.payload["case_id"] for row in rows if row.event == "sample_imported"] == ["c1"]
    assert "identity.corpus_cases" in declaration["fields"]


async def test_import_decisions_are_consumed(tmp_path, monkeypatch):
    import hashlib

    from typer.testing import CliRunner

    from evolution.tree.cli import tree_app
    from evolution.tree.execution.authorizations import read_decisions
    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    owner = _records(source / "evaluation.jsonl")
    configuration = owner[0]["payload"]["identity"]
    configuration["split"] = "test"
    owner[0]["payload"]["identity_digest"] = hashlib.sha256(
        canonical_json(configuration)
    ).hexdigest()
    (source / "evaluation.jsonl").write_text("".join(json.dumps(row) + "\n" for row in owner))
    scorer = path.parent / "scorer/score.py"
    scorer.write_text(scorer.read_text() + "\n# another scorer\n")
    run = path.parent / "import-run"
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert [finding.code for finding in caught.value.findings] == [
        "baseline_import_held_out",
        "baseline_import_incompatible",
    ]
    runner = CliRunner()
    for finding in caught.value.findings:
        accepted = runner.invoke(
            tree_app,
            [
                "authorize",
                str(run),
                finding.code,
                "--digest",
                finding.instance_digest,
                "--reason",
                "fixture decision",
            ],
        )
        assert accepted.exit_code == 0, accepted.output
    assert sorted(entry.name for entry in run.iterdir()) == ["authorizations"]
    await evolve(
        path,
        run,
        adapter_factory=lambda *args: FakeAdapter(),
        file_authorizations=read_decisions(run),
        decision_directory=True,
    )
    listed = runner.invoke(tree_app, ["authorize", str(run), "--list"])
    assert listed.exit_code == 0, listed.output
    assert [json.loads(line)["status"] for line in listed.output.splitlines()] == [
        "consumed",
        "consumed",
    ]


async def test_decision_for_replaced_source_does_not_match(tmp_path, monkeypatch):
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    scorer = path.parent / "scorer/score.py"
    scorer.write_text(scorer.read_text() + "\n# changed\n")
    run = path.parent / "import-run"
    with pytest.raises(EvolveRefused) as first:
        await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    decision = accept_import_finding(first.value.findings[0])
    child = _records(source / "measurements/evaluation/events.jsonl")
    identity = child[0]["payload"]["identity"]
    identity["max_scorer_concurrency"] += 1
    digest = hashlib.sha256(canonical_json(identity)).hexdigest()
    child[0]["payload"]["identity_digest"] = digest
    (source / "measurements/evaluation/events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in child)
    )
    owner = _records(source / "evaluation.jsonl")
    bound = next(row for row in owner if row["event"] == "measurement_bound")
    bound["payload"].update(identity=identity, identity_digest=digest)
    (source / "evaluation.jsonl").write_text("".join(json.dumps(row) + "\n" for row in owner))
    with pytest.raises(EvolveRefused) as second:
        await evolve(
            path, run, adapter_factory=lambda *args: FakeAdapter(), authorizations=[decision]
        )
    assert second.value.code == "baseline_import_incompatible"
    assert second.value.instance_digest != decision.digest
    assert not run.exists()


@pytest.mark.parametrize("secret", [False, True])
async def test_carried_component_maps(tmp_path, monkeypatch, secret):
    import os

    from evolution.tree.affected_cases import baseline_provenance, known_maps, parent_mode
    from evolution.tree.execution.journal_schema import components_digest

    token = "private-map-token"
    components = {
        "catalog": {
            "sha256": "a" * 64,
            "sources": [f"skills/{token}/SKILL.md" if secret else "skills/SKILL.md"],
        }
    }
    path, _, _ = await import_project(
        tmp_path, monkeypatch, schema=15, components=components, used=lambda case, turn: ["catalog"]
    )
    document = json.loads(path.read_text())
    if secret:
        document["editor"].setdefault("allowed_env", []).append("MAP_TOKEN")
        path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    notes = []
    await evolve(
        path,
        run,
        adapter_factory=lambda *args: FakeAdapter(),
        notes=notes.append,
        environ={**os.environ, **({"MAP_TOKEN": token} if secret else {})},
    )
    records, rows = imported_baseline(run)
    declaration = records[0]["payload"]["identity"]["baseline_import"]
    digest = components_digest(components)
    assert declaration["components"] == ([] if secret else [digest])
    assert declaration["provenance"] == {"reported": 2, "unknown": 0, "source_recorded": True}
    assert [row.event for row in rows] == ["run_created", "sample_imported", "sample_imported"] + (
        [] if secret else ["components_declared"]
    )
    provenance = baseline_provenance(rows)
    known = known_maps(rows, provenance, "campaign", lambda name: None)
    assert parent_mode(provenance, known) == (
        "component",
        digest,
        "components_unavailable" if secret else None,
    )
    assert ("1 component maps not carried" in notes) == secret
    if secret:
        assert token not in "".join(row.encode().decode() for row in rows)


async def test_linked_inherit_carries_maps(tmp_path, monkeypatch):
    from tests.campaign_drivers import _champion_copy

    components = {"catalog": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    path, _, _ = await import_project(
        tmp_path, monkeypatch, schema=15, components=components, used=lambda case, turn: ["catalog"]
    )
    predecessor = path.parent / "import-run"
    await evolve(path, predecessor, adapter_factory=lambda *args: FakeAdapter())
    document = json.loads(path.read_text())
    del document["baseline_import"]
    document.update(
        linked_from={"run_dir": str(predecessor), "baseline": "inherit"},
        seed_root=str(_champion_copy(predecessor)),
    )
    path.write_text(json.dumps(document))
    run = path.parent / "linked-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    records, rows = imported_baseline(run)
    baseline = records[0]["payload"]["identity"]["linked_from"]["baseline"]
    assert set(baseline) >= {"not_imported", "components", "provenance"}
    assert len(baseline["components"]) == 1
    assert sum(row.event == "components_declared" for row in rows) == 1
    assert sum(row.event == "sample_imported" for row in rows) == 2


async def test_later_baseline_instance_does_not_redeclare_carried_map(tmp_path, monkeypatch):
    components = {"catalog": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    path, source, _ = await import_project(
        tmp_path, monkeypatch, schema=15, components=components, used=lambda case, turn: ["catalog"]
    )
    log = source / "measurements/evaluation/events.jsonl"
    rows = log.read_text().splitlines(keepends=True)
    end = next(
        index for index, row in enumerate(rows) if json.loads(row)["event"] == "scoring_finished"
    )
    log.write_text("".join(rows[: end + 1]))
    owner = [
        row
        for row in _records(source / "evaluation.jsonl")
        if row["event"] != "evaluation_completed"
    ]
    (source / "evaluation.jsonl").write_text("".join(json.dumps(row) + "\n" for row in owner))
    run = path.parent / "import-run"
    await evolve(
        path,
        run,
        adapter_factory=lambda *args: FakeAdapter(
            components=components, used=lambda case, turn: ["catalog"]
        ),
    )
    _, baseline = imported_baseline(run)
    assert sum(row.event == "sample_imported" for row in baseline) == 1
    assert sum(row.event == "rollout_started" for row in baseline) == 1
    assert sum(row.event == "components_declared" for row in baseline) == 1


async def test_imported_maps_feed_real_attempt_component_selection(tmp_path, monkeypatch):
    components = {"catalog": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    path, _, _ = await import_project(
        tmp_path, monkeypatch, schema=16, components=components, used=lambda case, turn: ["catalog"]
    )
    run = path.parent / "import-run"
    await evolve(
        path,
        run,
        adapter_factory=lambda *args: FakeAdapter(
            components=components, used=lambda case, turn: ["catalog"]
        ),
    )
    records, _ = imported_baseline(run)
    assert any(row["event"] == "attempt_opened" for row in records)
    selections = [row["payload"] for row in records if row["event"] == "affected_cases_selected"]
    assert selections
    assert all(row["mode"] == "component" and row["fallback_reason"] is None for row in selections)


@pytest.mark.parametrize("schema", [15, 16])
async def test_import_status_and_caveats(tmp_path, monkeypatch, schema):
    from evolution.tree.campaign import inspect_campaign

    path, _, evaluation = await import_project(tmp_path, monkeypatch, schema=schema)
    run = path.parent / "import-run"
    result = await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    status = inspect_campaign(run).to_payload()
    block = status["baseline_import"]
    assert block == {
        "source_kind": "evaluation",
        "from_run": evaluation["run_id"],
        "source_measurement_id": "evaluation",
        "imported": 2,
        "scheduled": 2,
        "fields": [],
        "not_imported": dict.fromkeys(
            (
                "not_scheduled",
                "seed_differs",
                "not_scored",
                "primary_missing",
                "out_of_range",
                "dependency_differs",
            ),
            0,
        ),
        "components_carried": 0,
        "provenance": {"reported": 0, "unknown": 2, "source_recorded": True},
        "abandoned": False,
        "authorization_id": None,
        "held_out": None,
        "held_out_authorization_id": None,
    }
    assert status["linked_from"] is None
    assert result.status.to_payload()["baseline_import"] == block
    kinds = [item["kind"] for item in status["caveats"]]
    assert "baseline_imported" in kinds and "imported_provenance_unknown" in kinds


async def test_old_runs_report_nothing_new(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign

    path, _, _ = await import_project(tmp_path, monkeypatch)
    # The pre-existing campaign fixture was created before baseline_import was added to its manifest.
    status = inspect_campaign(path.parent / "run").to_payload()
    assert status["baseline_import"] is None
    assert not {"baseline_imported", "imported_provenance_unknown"} & {
        item["kind"] for item in status["caveats"]
    }


@pytest.mark.parametrize("kind", ["reported", "metadata"])
async def test_unmarked_lab_provenance_caveat_and_note(tmp_path, monkeypatch, kind):
    from evolution.tree.campaign import inspect_campaign

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    standalone = write_lab_source(path, source, marker=False)
    document = json.loads(path.read_text())
    document["baseline_import"]["source"] = str(standalone)
    document["campaign"]["gate"]["provenance"] = {
        "kind": kind,
        **({"path": "/uses"} if kind == "metadata" else {}),
    }
    path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    notes = []
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), notes=notes.append)
    status = inspect_campaign(run).to_payload()
    assert status["baseline_import"]["provenance"] == {
        "reported": 0,
        "unknown": 2,
        "source_recorded": False,
    }
    assert any(item["kind"] == "imported_provenance_unknown" for item in status["caveats"]) == (
        kind == "reported"
    )
    assert any("2 of 2 imported rows carry no provenance" in note for note in notes) == (
        kind == "reported"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", 1),
        ("imported", True),
        ("scheduled", 0),
        ("components_carried", -1),
        ("abandoned", 1),
        ("provenance", {"reported": 0, "unknown": 0, "source_recorded": True}),
        ("held_out", {"split": "test", "cases": []}),
    ],
)
def test_baseline_import_status_is_closed(field, value):
    from types import SimpleNamespace

    from evolution.tree.campaign_projections import (
        _baseline_import_status,
        _validate_baseline_import_status,
    )
    from evolution.tree.storage.envelope import SchemaError
    from tests.test_tree_campaign_identity import standalone_declaration

    snapshot = SimpleNamespace(
        identity={"baseline_import": standalone_declaration()}, baseline_abandoned=None
    )
    block = _baseline_import_status(snapshot)
    _validate_baseline_import_status(block)
    block[field] = value
    with pytest.raises(SchemaError):
        _validate_baseline_import_status(block)


def write_records(path, rows):
    path.write_text(
        "".join(json.dumps(dict(row, sequence=index)) + "\n" for index, row in enumerate(rows, 1))
    )


def rewrite_creation(run, mutate):
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json

    records = _records(run / "campaign.jsonl")
    identity = records[0]["payload"]["identity"]
    mutate(identity)
    records[0]["payload"]["identity_digest"] = hashlib.sha256(canonical_json(identity)).hexdigest()
    write_records(run / "campaign.jsonl", records)


@pytest.mark.parametrize("schema", [15, 16])
async def test_clean_import_audit_never_reopens_the_source(tmp_path, monkeypatch, schema):
    from evolution.tree.campaign import inspect_campaign

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=schema)
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    source.rename(source.with_name("moved-evaluation"))
    audit = inspect_campaign(run, verify=True).to_payload()["verification"]
    assert not audit["mismatches"], audit


@pytest.mark.parametrize(
    "fault",
    ["row_removed", "count", "source", "provenance", "authorization", "held_out_authorization"],
)
async def test_audit_import_declaration(tmp_path, monkeypatch, fault):
    from evolution.tree.baseline_import import import_pairs_digest
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.authorizations import Authorization
    from evolution.tree.execution.output_admission import EvolveRefused

    path, _, _ = await import_project(tmp_path, monkeypatch, schema=15)
    decisions = []
    if "authorization" in fault:
        if fault == "authorization":
            scorer = path.parent / "scorer/score.py"
            scorer.write_text(scorer.read_text() + "\n# changed scorer\n")
        else:
            import hashlib

            from evolution.tree.execution.journal_identity import canonical_json

            source = path.parent / "evaluation-run/evaluation.jsonl"
            records = _records(source)
            records[0]["payload"]["identity"]["split"] = "test"
            records[0]["payload"]["identity_digest"] = hashlib.sha256(
                canonical_json(records[0]["payload"]["identity"])
            ).hexdigest()
            write_records(source, records)
    run = path.parent / "import-run"
    if "authorization" in fault:
        with pytest.raises(EvolveRefused) as caught:
            await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
        decisions = [accept_import_finding(item) for item in caught.value.findings]
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), authorizations=decisions)
    assert not verify_evidence(run)["mismatches"]
    if fault in ("source", "row_removed"):
        log = run / "measurements/m-r00-champion/events.jsonl"
        records = _records(log)
        if fault == "row_removed":
            del records[1]
        else:
            records[1]["payload"]["source"]["run_id"] = "another-source"
        write_records(log, records)
        if fault == "source":
            # Keep scalar declaration/copy hashes consistent to exercise origin proof independently.
            digest = import_pairs_digest(
                [row["payload"] for row in records if row["event"] == "sample_imported"]
            )
            rewrite_creation(
                run, lambda identity: identity["baseline_import"].update(pairs_digest=digest)
            )
    elif fault == "count":

        def count_off(identity):
            identity["baseline_import"].update(
                imported=1, provenance={"reported": 0, "unknown": 1, "source_recorded": True}
            )

        rewrite_creation(run, count_off)
    elif fault == "provenance":
        rewrite_creation(
            run,
            lambda identity: identity["baseline_import"]["provenance"].update(
                reported=1, unknown=1
            ),
        )
    else:

        def wrong_digest(identity):
            key = "authorization" if fault == "authorization" else "held_out_authorization"
            previous = identity["baseline_import"][key]
            identity["baseline_import"][key] = Authorization.new(
                previous["code"], "f" * 64, previous["reason"], now=previous["issued_at"]
            ).decision_payload()

        rewrite_creation(run, wrong_digest)
    findings = verify_evidence(run)["mismatches"]
    assert any(item["code"] == "baseline_import_mismatch" for item in findings), findings


@pytest.mark.parametrize("fault", ["removed", "extra", "between_imports", "reordered"])
async def test_audit_carried_component_order(tmp_path, monkeypatch, fault):
    from evolution.tree.campaign_projections import verify_evidence
    from evolution.tree.execution.journal_schema import components_digest

    components = {"catalog": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    path, _, _ = await import_project(
        tmp_path, monkeypatch, schema=15, components=components, used=lambda case, turn: ["catalog"]
    )
    run = path.parent / "import-run"
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert not verify_evidence(run)["mismatches"]
    log = run / "measurements/m-r00-champion/events.jsonl"
    records = _records(log)
    if fault == "removed":
        records.pop()
    elif fault == "between_imports":
        records.insert(2, records.pop())
    else:
        other = {"other": {"sha256": "b" * 64, "sources": ["skills/SKILL.md"]}}
        records.append(
            {
                **records[-1],
                "payload": {"components": other, "components_digest": components_digest(other)},
            }
        )
    if fault == "reordered":
        declarations = sorted(records[-2:], key=lambda row: row["payload"]["components_digest"])
        digests = [row["payload"]["components_digest"] for row in declarations]
        rewrite_creation(
            run, lambda identity: identity["baseline_import"].update(components=digests)
        )
        records[-2:] = reversed(declarations)
    write_records(log, records)
    findings = verify_evidence(run)["mismatches"]
    assert any(item["code"] == "baseline_import_mismatch" for item in findings), findings


async def cut_import_creation(path, run, monkeypatch, hook="after_campaign_created"):
    from evolution.tree.campaign import _Campaign
    from tests.test_tree_campaign_family15_gate import GateBoundary

    original = _Campaign.hook

    def stop(self, name):
        original(self, name)
        if name == hook:
            raise GateBoundary()

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", stop)
        with pytest.raises(GateBoundary):
            await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())


@pytest.mark.parametrize("hook", ["after_campaign_created", "after_measurement_bound"])
@pytest.mark.parametrize("schema", [15, 16])
async def test_owed_copy_recovery(tmp_path, monkeypatch, hook, schema):
    from evolution.tree.campaign import inspect_campaign

    components = {"catalog": {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}}
    path, _, _ = await import_project(
        tmp_path,
        monkeypatch,
        schema=schema,
        components=components,
        used=lambda case, turn: ["catalog"],
    )
    run = path.parent / "import-run"
    await cut_import_creation(path, run, monkeypatch, hook)
    assert not (run / "measurements/m-r00-champion/events.jsonl").exists()
    await evolve(
        path,
        run,
        adapter_factory=lambda *args: FakeAdapter(
            components=components, used=lambda case, turn: ["catalog"]
        ),
    )
    _, rows = imported_baseline(run)
    assert [row.event for row in rows] == [
        "run_created",
        "sample_imported",
        "sample_imported",
        "components_declared",
    ]
    assert not inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"]


@pytest.mark.parametrize("fault", ["moved", "grown", "replacement", "held_lease"])
async def test_lost_owed_source_can_be_measured_fresh(tmp_path, monkeypatch, fault):
    import fcntl

    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    log = source / "measurements/evaluation/events.jsonl"
    full = log.read_bytes()
    if fault == "grown":
        lines = log.read_text().splitlines(keepends=True)
        end = next(
            index
            for index, row in enumerate(lines)
            if json.loads(row)["event"] == "scoring_finished"
        )
        log.write_text("".join(lines[: end + 1]))
        owner = [
            row
            for row in _records(source / "evaluation.jsonl")
            if row["event"] != "evaluation_completed"
        ]
        write_records(source / "evaluation.jsonl", owner)
    run = path.parent / "import-run"
    await cut_import_creation(path, run, monkeypatch)
    before = {
        str(path.relative_to(run)): path.read_bytes() for path in run.rglob("*") if path.is_file()
    }
    lock = None
    if fault == "moved":
        source.rename(source.with_name("source-moved"))
    elif fault == "grown":
        log.write_bytes(full)
    elif fault == "replacement":
        from evolution.tree.evaluation import evaluate

        replacement = path.parent / "replacement-evaluation"
        await evaluate(
            path.parent / "evaluation.json",
            replacement,
            adapter_factory=lambda *args: FakeAdapter(),
        )
        document = json.loads(path.read_text())
        document["baseline_import"]["source"] = str(replacement)
        path.write_text(json.dumps(document))
    else:
        lock = (source / "evaluation.lock").open("r+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(EvolveRefused) as caught:
            await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
        assert caught.value.code == "baseline_import_unavailable"
        assert caught.value.effect == "fresh_baseline"
        assert {
            str(path.relative_to(run)): path.read_bytes()
            for path in run.rglob("*")
            if path.is_file()
        } == before
        decision = accept_import_finding(caught.value.findings[0])
        await evolve(
            path, run, adapter_factory=lambda *args: FakeAdapter(), authorizations=[decision]
        )
    finally:
        if lock is not None:
            lock.close()
    _, rows = imported_baseline(run)
    assert not any(row.event == "sample_imported" for row in rows)
    assert sum(row.event == "rollout_started" for row in rows) == 2
    status = inspect_campaign(run, verify=True).to_payload()
    assert status["baseline_import"]["abandoned"] is True
    assert not any(
        item["kind"] in ("baseline_imported", "imported_provenance_unknown")
        for item in status["caveats"]
    )
    assert not status["verification"]["mismatches"]


@pytest.mark.parametrize("schema", [15, 16])
async def test_after_birth_resume_does_not_read_source(tmp_path, monkeypatch, schema):
    path, source, _ = await import_project(tmp_path, monkeypatch, schema=schema)
    run = path.parent / "import-run"
    await cut_import_creation(path, run, monkeypatch, "after_baseline_bound")
    source.rename(source.with_name("source-moved"))
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    _, rows = imported_baseline(run)
    assert sum(row.event == "sample_imported" for row in rows) == 2


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("schema", [15, 16])
async def test_baseline_import_presence_on_resume(tmp_path, monkeypatch, initial, schema):
    import asyncio

    from typer.testing import CliRunner

    from evolution.tree import evolve as evolve_module
    from evolution.tree.cli import tree_app
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=schema)
    document = json.loads(path.read_text())
    if not initial:
        del document["baseline_import"]
        path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    await cut_import_creation(path, run, monkeypatch)
    if initial:
        del document["baseline_import"]
    else:
        document["baseline_import"] = {"source": str(source)}
    path.write_text(json.dumps(document))
    adapter = FakeAdapter()
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: adapter)
    assert caught.value.code == "run_identity_changed" and caught.value.instance_digest is None
    assert [(item.field, item.journaled, item.observed) for item in caught.value.differences] == [
        (
            "identity.baseline_import",
            "present" if initial else "absent",
            "absent" if initial else "present",
        )
    ]
    assert not adapter.materialized and (run / "campaign.jsonl").read_bytes() == before
    monkeypatch.setattr(evolve_module, "_http_session_adapter", lambda *args, **kwargs: adapter)
    from contextlib import nullcontext

    from evolution.tree import cli as cli_module

    monkeypatch.setattr(cli_module, "_stop_flag", lambda: nullcontext(lambda: False))
    result = await asyncio.to_thread(
        CliRunner().invoke, tree_app, ["evolve", "--manifest", str(path), "--run-dir", str(run)]
    )
    assert result.exit_code == 2, result.output
    assert "baseline_import cannot be added to or removed" in result.output


async def test_empty_declared_import_owes_no_source(tmp_path, monkeypatch):
    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    document = json.loads(path.read_text())
    document["campaign"]["random_seed"] += 100
    path.write_text(json.dumps(document))
    run = path.parent / "import-run"
    await cut_import_creation(path, run, monkeypatch)
    source.rename(source.with_name("source-moved"))
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    _, rows = imported_baseline(run)
    assert sum(row.event == "rollout_started" for row in rows) == 2
    assert not any(row.event == "sample_imported" for row in rows)


async def test_rebaseline_before_round_one_abandons_import(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.execution.authorizations import Authorization
    from evolution.tree.execution.output_admission import EvolveRefused

    path, source, _ = await import_project(tmp_path, monkeypatch, schema=15)
    run = path.parent / "import-run"
    await cut_import_creation(path, run, monkeypatch)
    source.rename(source.with_name("source-moved"))
    document = json.loads(path.read_text())
    document["campaign"]["budget"]["max_turns"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(EvolveRefused) as caught:
        await evolve(path, run, adapter_factory=lambda *args: FakeAdapter())
    assert caught.value.code == "run_identity_changed" and "rebaseline" in caught.value.effects
    decision = Authorization.new(
        "run_identity_changed",
        caught.value.instance_digest,
        "fixture rebaseline",
        effect="rebaseline",
    )
    await evolve(path, run, adapter_factory=lambda *args: FakeAdapter(), authorizations=[decision])
    _, rows = imported_baseline(run)
    assert sum(row.event == "rollout_started" for row in rows) == 2
    assert not any(row.event == "sample_imported" for row in rows)
    status = inspect_campaign(run, verify=True).to_payload()
    assert status["baseline_import"]["abandoned"] is True
    assert not status["verification"]["mismatches"]
