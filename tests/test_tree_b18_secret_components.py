"""#387: run secrets in reported provenance must not break durable evidence."""

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from evolution.tree.campaign import inspect_campaign
from evolution.tree.campaign_projections import verify_evidence
from evolution.tree.execution.journal_schema import declaration_digest
from evolution.tree.process_log import PROCESS_LOGS_DIR, capture
from tests import campaign_drivers
from tests import test_tree_campaign_family15_delivery as delivery
from tests.campaign_drivers import audit_mismatches
from tests.campaign_fakes import FakeAdapter
from tests.family14_public import family14_public_run
from tests.test_tree_campaign_family14_link_run import edit_document
from tests.test_tree_campaign_family15_delivery import durable_facts, public_run

pytestmark = pytest.mark.slow

SECRET = "b18-component-secret-387"
SECRET_ENV = "B18_COMPONENT_SECRET"
RETRIEVER = {"sha256": "a" * 64, "sources": ["skills/SKILL.md"]}
SECOND_SECRET = "b18-second-component-secret-387"
WARNING = re.compile(
    r"measurement (\S+): provenance withheld for (\d+) sample\(s\) and "
    r"(\d+) component\(s\) left out of published maps because they contain a run secret"
)


def _records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


async def _run(tmp_path, monkeypatch, family, *, components=None, used=None, secrets=(SECRET,)):
    env_names = [SECRET_ENV + str(index) for index in range(len(secrets))]
    for name, secret in zip(env_names, secrets, strict=True):
        monkeypatch.setenv(name, secret)

    def configure(document):
        document["scorer"]["allowed_env"] = env_names
        document["campaign"]["gate"]["provenance"] = {"kind": "reported"}

    adapter = FakeAdapter(components=components, used=used)
    # The fixture drivers own admission and offline execution. Add the same
    # process-log boundary used by the CLI, retaining its real stderr tee.
    with monkeypatch.context() as patch, capture("evolve", prefix="evo tree evolve") as process:
        owner = campaign_drivers if family == 15 else delivery
        original = owner.evolve

        async def logged(path, *args, **options):
            return await original(path, *args, **options, process_log=process.materialize)

        patch.setattr(owner, "evolve", logged)
        if family == 15:
            edit_document(patch, configure)
            return await family14_public_run(tmp_path, patch, adapter=adapter)
        return await public_run(tmp_path, patch, configure=configure, adapter=adapter)


def _assert_finished_without_secret(run: Path, result, *, secrets=(SECRET,)):
    assert result.terminal == "finished", (result.terminal, result.owner, result.code)
    for path in sorted(run.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            for secret in secrets:
                assert secret.encode() not in raw, path.relative_to(run)
            assert b"[REDACTED]" not in raw, path.relative_to(run)


def _journals(run):
    return sorted((run / "measurements").glob("*/events.jsonl")) + sorted(
        (run / "inspections").glob("*/events.jsonl")
    )


async def _assert_end_to_end(
    tmp_path, monkeypatch, family, capsys, *, overflow, withheld, **options
):
    capsys.readouterr()
    run, result = await _run(tmp_path, monkeypatch, family, **options)
    stderr = capsys.readouterr().err
    secrets = options.get("secrets", (SECRET,))
    _assert_finished_without_secret(run, result, secrets=secrets)
    campaign = _records(run / "campaign.jsonl")
    (selection,) = [r["payload"] for r in campaign if r["event"] == "affected_cases_selected"]
    baseline = _records(run / "measurements/m-r00-champion/events.jsonl")
    samples = [r["payload"] for r in baseline if r["event"] == "rollout_finished"]
    (first,) = [r for r in samples if r["case_id"] == "c1"]
    if overflow:
        assert first["provenance_overflow"] is True
        assert "provenance" not in first
        assert 0 in selection["unknown_cases"] or (
            selection["cases"] == [0, 1] and selection["fallback_reason"] == "components_withheld"
        )
    else:
        assert first["provenance"] == ["retriever"]
        assert "provenance_overflow" not in first
    if withheld:
        assert selection["cases"] == [0, 1]
        assert selection["fallback_reason"] == "components_withheld"

    expected_warnings = Counter()
    saw_withheld = False
    for path in _journals(run):
        rows = _records(path)
        declared = [r["payload"] for r in rows if r["event"] == "components_declared"]
        for declaration in declared:
            count = declaration.get("components_withheld", 0)
            if count:
                saw_withheld = True
                assert count == 1
                assert declaration["components"] == (
                    {} if secrets == ("retriever",) else {"retriever": RETRIEVER}
                )
            else:
                assert "components_withheld" not in declaration
            assert declaration["components_digest"] == declaration_digest(
                declaration["components"], count
            )
        hidden_samples = sum(
            r["event"] == "rollout_finished" and r["payload"].get("provenance_overflow", False)
            for r in rows
        )
        hidden_components = sum(d.get("components_withheld", 0) for d in declared)
        if hidden_samples or hidden_components:
            expected_warnings[(path.parent.name, str(hidden_samples), str(hidden_components))] += 1
        if path.parent.parent.name == "inspections":
            (declaration,) = declared
            (inspected,) = [r["payload"] for r in campaign if r["event"] == "candidate_inspected"]
            assert inspected["components"] == declaration["components"]
            assert inspected.get("components_withheld", 0) == declaration.get(
                "components_withheld", 0
            )
            assert (
                declaration_digest(inspected["components"], inspected.get("components_withheld", 0))
                == declaration["components_digest"]
            )
    assert saw_withheld is withheld
    assert expected_warnings
    assert Counter(WARNING.findall(stderr)) == expected_warnings
    process_logs = list((run / PROCESS_LOGS_DIR).glob("*.log"))
    assert len(process_logs) == 1
    assert Counter(WARNING.findall(process_logs[0].read_text())) == expected_warnings
    # Each warning carries only the safe measurement ID and the two counts.
    for line in stderr.splitlines():
        if "provenance withheld" in line:
            assert WARNING.fullmatch(line)
            assert all(secret not in line for secret in secrets)

    assert audit_mismatches(run) == []
    assert verify_evidence(run)["mismatches"] == []
    status = inspect_campaign(run, verify=True).to_payload()
    assert status["verification"]["mismatches"] == []
    before = {
        p.relative_to(run): p.read_bytes()
        for p in run.rglob("*")
        if p.is_file() and PROCESS_LOGS_DIR not in p.relative_to(run).parts
    }
    capsys.readouterr()
    resumed_run, resumed = await _run(tmp_path, monkeypatch, family, **options)
    resumed_stderr = capsys.readouterr().err
    assert resumed_run == run
    _assert_finished_without_secret(run, resumed, secrets=secrets)
    assert WARNING.findall(resumed_stderr) == []
    after = {
        p.relative_to(run): p.read_bytes()
        for p in run.rglob("*")
        if p.is_file() and PROCESS_LOGS_DIR not in p.relative_to(run).parts
    }
    assert after == before
    resumed_logs = [p for p in (run / PROCESS_LOGS_DIR).glob("*.log") if p not in process_logs]
    assert resumed_logs == []


@pytest.mark.parametrize("family", [15, 16])
@pytest.mark.parametrize("scenario", ["equal", "containing", "two-secrets", "real-component"])
async def test_secret_in_used_union(tmp_path, monkeypatch, family, scenario, capsys):
    """Withholding must keep a dependency unknown, including a real-name collision."""
    secrets = (SECRET,)
    used = [SECRET]
    components = {"retriever": RETRIEVER}
    if scenario == "containing":
        used = [f"retriever:{SECRET}", "retriever"]
    elif scenario == "two-secrets":
        secrets = (SECRET, SECOND_SECRET)
        used = [SECRET, SECOND_SECRET, "retriever"]
    elif scenario == "real-component":
        secrets = ("retriever",)
        used = ["retriever"]
    await _assert_end_to_end(
        tmp_path,
        monkeypatch,
        family,
        capsys,
        components=components,
        used=lambda case_id, turn: used if case_id == "c1" else [],
        secrets=secrets,
        overflow=True,
        withheld=scenario == "real-component",
    )


@pytest.mark.parametrize("family", [15, 16])
async def test_secret_component_id_in_baseline_map(tmp_path, monkeypatch, family, capsys):
    """A Parent's omitted map key forces every case into the selection."""
    await _assert_end_to_end(
        tmp_path,
        monkeypatch,
        family,
        capsys,
        components={SECRET: RETRIEVER, "retriever": RETRIEVER},
        used=lambda case_id, turn: [SECRET] if case_id == "c1" else [],
        overflow=True,
        withheld=True,
    )


@pytest.mark.parametrize("family", [15, 16])
@pytest.mark.parametrize("overlapping_path", [False, True])
async def test_secret_component_id_at_candidate_inspection(
    tmp_path, monkeypatch, family, overlapping_path, capsys
):
    """Even an added hidden component over an already covered path selects all cases."""
    parent_digest = None

    def components(candidate_digest):
        nonlocal parent_digest
        if parent_digest is None:
            parent_digest = candidate_digest
        declared = {"retriever": RETRIEVER}
        if candidate_digest != parent_digest:
            declared[f"retriever:{SECRET}"] = {
                **RETRIEVER,
                "sources": ["skills/SKILL.md"] if overlapping_path else ["skills/extra.md"],
            }
        return declared

    await _assert_end_to_end(
        tmp_path,
        monkeypatch,
        family,
        capsys,
        components=components,
        used=lambda case_id, turn: ["retriever"] if case_id == "c1" else [],
        overflow=False,
        withheld=True,
    )


@pytest.mark.parametrize("family", [15, 16])
async def test_inspection_publishes_screened_map(tmp_path, monkeypatch, family):
    """Both journals and the reducer bind the screened map AND omitted count."""
    from dataclasses import replace

    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import (
        _check_candidate_inspected,
        _evidence_of,
        _PreconditionFailed,
    )
    from evolution.tree.campaign_schema import campaign_family_for, reduce
    from evolution.tree.execution.journal_schema import declaration_digest
    from evolution.tree.storage.envelope import Envelope
    from tests.campaign_fakes import Crash

    parent_digest = None

    def components(candidate_digest):
        nonlocal parent_digest
        if parent_digest is None:
            parent_digest = candidate_digest
        result = {"retriever": RETRIEVER}
        if candidate_digest != parent_digest:
            result[f"retriever:{SECRET}"] = RETRIEVER
        return result

    original_hook = _Campaign.hook

    def cut(self, name):
        original_hook(self, name)
        if name == "after_candidate_inspected":
            raise Crash("inspection durable")

    monkeypatch.setattr(_Campaign, "hook", cut)
    with pytest.raises(Crash, match="inspection durable"):
        await _run(
            tmp_path,
            monkeypatch,
            family,
            components=components,
            used=lambda case_id, turn: ["retriever"],
        )
    run = tmp_path / "run"
    campaign = _records(run / "campaign.jsonl")
    (inspected,) = [row["payload"] for row in campaign if row["event"] == "candidate_inspected"]
    (inspection_path,) = (run / "inspections").glob("*/events.jsonl")
    (declared,) = [
        row["payload"] for row in _records(inspection_path) if row["event"] == "components_declared"
    ]
    assert inspected["components"] == declared["components"] == {"retriever": RETRIEVER}
    assert inspected["components_withheld"] == declared["components_withheld"] == 1
    assert (
        declaration_digest(inspected["components"], inspected["components_withheld"])
        == declared["components_digest"]
    )
    assert SECRET.encode() not in (run / "campaign.jsonl").read_bytes()
    assert SECRET.encode() not in inspection_path.read_bytes()

    context, log = _evidence_of(run)
    record = next(row for row in log.records if row.event == "candidate_inspected")
    attempt = (
        reduce(log.records, selector=None, family_version=family - 1).rounds[1].attempts["r01-a01"]
    )
    assert attempt.inspected_components == inspected["components"]
    assert attempt.inspected_withheld == 1
    _check_candidate_inspected(context, record)
    changed = record.to_payload()
    changed["payload"]["components_withheld"] = 2
    wrong = Envelope.from_payload(changed, family=campaign_family_for(family - 1))
    changed_context = replace(
        context, records=tuple(wrong if row is record else row for row in context.records)
    )
    with pytest.raises(_PreconditionFailed, match="never declared"):
        _check_candidate_inspected(changed_context, wrong)


async def _withheld_selection_run(tmp_path, monkeypatch, family, side, *, resume=False):
    """Stop at durable selection, optionally reopening a counted inspection."""
    from evolution.tree.campaign import _Campaign
    from evolution.tree.campaign_projections import _evidence_of
    from tests.campaign_fakes import Crash

    parent_digest = None

    def components(candidate_digest):
        nonlocal parent_digest
        if parent_digest is None:
            parent_digest = candidate_digest
        mapping = {"retriever": RETRIEVER}
        if side == "parent" or candidate_digest != parent_digest:
            mapping[f"{SECRET}-tool"] = RETRIEVER
        return mapping

    async def invoke(patch):
        return await _run(
            tmp_path,
            patch,
            family,
            components=components,
            used=lambda case_id, turn: ["retriever"] if case_id == "c1" else [],
        )

    original = _Campaign.hook

    def cut_at(seam):
        def hook(self, name):
            original(self, name)
            if name == seam:
                raise Crash(seam)

        return hook

    if resume:
        assert side == "candidate"
        with monkeypatch.context() as patch:
            patch.setattr(_Campaign, "hook", cut_at("after_candidate_inspected"))
            with pytest.raises(Crash, match="after_candidate_inspected"):
                await invoke(patch)
        rows = _records(tmp_path / "run" / "campaign.jsonl")
        assert rows[-1]["event"] == "candidate_inspected"
        assert rows[-1]["payload"]["components_withheld"] == 1
        assert not any(row["event"] == "affected_cases_selected" for row in rows)

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "hook", cut_at("after_affected_cases_selected"))
        with pytest.raises(Crash, match="after_affected_cases_selected"):
            await invoke(patch)
    run = tmp_path / "run"
    context, log = _evidence_of(run)
    # The public audit reads this ledger while authenticating earlier store refs.
    context.cache["artifacts.jsonl"] = context.read_file(run / "artifacts.jsonl")
    selected = next(row for row in log.records if row.event == "affected_cases_selected")
    assert selected.payload["cases"] == [0, 1]
    assert selected.payload["fallback_reason"] == "components_withheld"
    inspections = [row for row in log.records if row.event == "candidate_inspected"]
    assert len(inspections) == (0 if side == "parent" else 1)
    return context, selected


@pytest.mark.parametrize("family", [15, 16])
async def test_resumed_selection_retains_withheld_count(tmp_path, monkeypatch, family):
    from evolution.tree.campaign_projections import _check_affected_cases_selected

    context, selected = await _withheld_selection_run(
        tmp_path, monkeypatch, family, "candidate", resume=True
    )
    _check_affected_cases_selected(context, selected)


# Captured from successful real /15 and /16 campaigns at C1 source 5434e0d24.
# UUIDs/clocks and their derived evidence references are excluded only by the
# existing durable_facts projection. The payload bytes below are unmodified.
_C1_DURABLE_BYTES = {
    15: (
        b'[{"affected_cases_selected":1,"attempt_closed":1,"attempt_input_frozen":1,"attempt_op'
        b'ened":1,"attempt_sealed":1,"baseline_bound":1,"baseline_complete":1,"campaign_created'
        b'":1,"campaign_finished":1,"candidate_inspected":1,"connection_preflight_finished":1,"'
        b'connection_preflight_started":1,"editor_finished":1,"editor_started":1,"focus_portion'
        b'_selected":1,"gate_scope_selected":1,"grouping_finished":1,"grouping_started":1,"meas'
        b'urement_bound":1,"reflection_finished":1,"reflection_started":1,"round_closed":1,"sel'
        b'ection_decided":1,"stage_opened":1},{"m-r00-champion":{"imports":0,"paid":2,"scores":'
        b'[["01","{\\"score\\": 0.0}"],["01","{\\"score\\": 1.0}"]]}},[["selection_decided",null,"n'
        b'o_finalists"],["round_closed","unscored",null],["campaign_finished","completed","roun'
        b'ds_exhausted"]],{}]'
    ),
    16: (
        b'[{"affected_cases_selected":1,"attempt_closed":1,"attempt_input_frozen":1,"attempt_op'
        b'ened":1,"attempt_sealed":1,"auxiliary_payload_declared":1,"baseline_bound":1,"baselin'
        b'e_complete":1,"campaign_created":1,"campaign_finished":1,"candidate_inspected":1,"con'
        b'nection_preflight_finished":1,"connection_preflight_started":1,"editor_finished":1,"e'
        b'ditor_started":1,"finalists_bound":1,"focus_portion_selected":1,"gate_scope_selected"'
        b':1,"grouping_finished":1,"grouping_started":1,"measurement_bound":1,"reflection_finis'
        b'hed":1,"reflection_started":1,"round_closed":1,"selection_decided":1,"stage_opened":1'
        b'},{"m-r00-champion":{"imports":0,"paid":2,"scores":[["01","{\\"score\\": 0.0}"],["01","'
        b'{\\"score\\": 1.0}"]]}},[["selection_decided",null,"no_eligible_candidate"],["round_clo'
        b'sed","retained",null],["campaign_finished","completed","rounds_exhausted"]],{}]'
    ),
}
_C1_PAYLOAD_BYTES = (
    (
        "campaign.jsonl",
        (
            b'{"attempt":"r01-a01","cases":[],"changed":["skills/SKILL.md"],"fallback_reason":null,'
            b'"mode":"component","round":1,"source":"reported","unknown_cases":[]}'
        ),
    ),
    (
        "measurements/m-r00-champion/events.jsonl",
        (
            b'{"components":{"retriever":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            b'aaaaaaaaaaaaaaaaa","sources":["skills/SKILL.md"]}},"components_digest":"26e998623be28'
            b'2b223aa8cb6ead94c8222e2dda5c18a8dba3c730c9659f09c45"}'
        ),
    ),
    (
        "inspections/r01-a01-i1/events.jsonl",
        (
            b'{"components":{"retriever":{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            b'aaaaaaaaaaaaaaaaa","sources":["skills/SKILL.md"]}},"components_digest":"26e998623be28'
            b'2b223aa8cb6ead94c8222e2dda5c18a8dba3c730c9659f09c45"}'
        ),
    ),
)


@pytest.mark.parametrize("family", [15, 16])
async def test_successful_run_bytes_unchanged(tmp_path, monkeypatch, family, capsys):
    """Successful pre-fix decisions and declaration/selection bytes stay identical."""
    run, result = await _run(
        tmp_path,
        monkeypatch,
        family,
        secrets=(),
        components={"retriever": RETRIEVER},
        used=lambda case_id, turn: ["retriever"],
    )
    _assert_finished_without_secret(run, result)
    assert WARNING.findall(capsys.readouterr().err) == []
    facts = json.dumps(durable_facts(run), sort_keys=True, separators=(",", ":")).encode()
    assert facts == _C1_DURABLE_BYTES[family]
    payloads = []
    for path in [run / "campaign.jsonl", *_journals(run)]:
        for line in path.read_bytes().splitlines():
            if json.loads(line)["event"] in {"components_declared", "affected_cases_selected"}:
                # Read the real canonical envelope without reserializing its payload.
                match = re.fullmatch(rb'.*,"payload":(.*),"run_id":.*', line)
                assert match is not None
                payloads.append((str(path.relative_to(run)), match[1]))
    assert tuple(payloads) == _C1_PAYLOAD_BYTES
