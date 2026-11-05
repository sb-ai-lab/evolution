"""Bind checks: modes never disable the journal secret guard."""

import importlib

import pytest

from evolution.tree.evolve import _refuse_secret_oracle_paths, _refuse_verbatim_answers
from evolution.tree.execution.check_modes import CheckModes, HitSink
from evolution.tree.execution.output_admission import EvolveRefused
from evolution.tree.target.connection_contract import SecretOraclePaths, VerbatimAnswers
from tests.campaign_drivers import audit_mismatches
from tests.test_tree_campaign_family14_connection_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize(
    "check,declared,call",
    [
        (
            "verbatim_answer_unmatched",
            VerbatimAnswers(("not in any oracle",)),
            _refuse_verbatim_answers,
        ),
        (
            "secret_oracle_path_unmatched",
            SecretOraclePaths(("/absent",)),
            _refuse_secret_oracle_paths,
        ),
    ],
)
def test_unmatched_bind_checks_have_independent_modes(mode, check, declared, call):
    hits = HitSink()
    if mode == "enforce":
        with pytest.raises(EvolveRefused) as caught:
            call(declared, (), (), modes=CheckModes({check: mode}), hits=hits)
        assert caught.value.code == check
    else:
        call(declared, (), (), modes=CheckModes({check: mode}), hits=hits)
    assert hits.entries() == ([dict(check=check, code=check, count=1)] if mode == "shadow" else [])


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize(
    "check,declared,call",
    [
        (
            "verbatim_answer_unmatched",
            VerbatimAnswers(("secret-string",)),
            _refuse_verbatim_answers,
        ),
        (
            "secret_oracle_path_unmatched",
            SecretOraclePaths(("/secret-string",)),
            _refuse_secret_oracle_paths,
        ),
    ],
)
def test_bind_modes_keep_the_secret_guard(mode, check, declared, call):
    with pytest.raises(EvolveRefused) as caught:
        call(declared, (), ("secret-string",), modes=CheckModes({check: mode}), hits=HitSink())
    assert caught.value.code == "record_matches_secret"


def shadow_settings(document):
    document["checks"] = {
        "version": "checks/1",
        "modes": {
            "verbatim_answer_unmatched": "shadow",
            "secret_oracle_path_unmatched": "shadow",
            "seal_oracle_leak": "off",
        },
    }
    document["verbatim_answers"] = {
        "version": "verbatim_answers/1",
        "strings": ["no such oracle value", "another absent value"],
    }
    document["secret_oracle_paths"] = {"version": "secret_oracle_paths/1", "pointers": ["/absent"]}


async def test_bind_hits_are_printed_each_run_and_journaled_only_on_creation(tmp_path, monkeypatch):
    delivery = importlib.import_module("tests.test_tree_campaign_family15_delivery")
    evolve = delivery.evolve
    notes = []

    async def capture(*args, **kwargs):
        kwargs["notes"] = notes.append
        return await evolve(*args, **kwargs)

    monkeypatch.setattr(delivery, "evolve", capture)
    run, result = await public_run(tmp_path, monkeypatch, configure=shadow_settings)
    assert result.terminal == "finished"
    hits = payloads(run, "check_shadowed")
    assert [p["count"] for p in hits] == [2, 1]
    assert all(p["round"] is None and p["operation_id"] is None for p in hits)
    assert "check shadowed: verbatim_answer_unmatched · bind · count 2" in notes
    notes.clear()
    _, result = await public_run(tmp_path, monkeypatch, configure=shadow_settings)
    assert result.terminal == "finished"
    assert payloads(run, "check_shadowed") == hits
    assert "check shadowed: secret_oracle_path_unmatched · bind · count 1" in notes
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("changed", ["checks", "connection", "editor"])
async def test_rebase_journals_bind_hits_only_for_checks_or_connection(
    tmp_path, monkeypatch, changed
):
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut

    with pytest.raises(PowerCut):
        await public_run(
            tmp_path, monkeypatch, configure=shadow_settings, cut="after_attempt_input_frozen"
        )

    def configure(document):
        shadow_settings(document)
        if changed == "checks":
            document["checks"]["modes"]["seal_oracle_leak"] = "shadow"
        elif changed == "connection":
            document["secret_oracle_paths"]["pointers"].append("/also_absent")
        else:
            document["editor"]["argv"].append("unused-argument")

    with pytest.raises(EvolveRefused) as refusal:
        await public_run(tmp_path, monkeypatch, configure=configure)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        authorizations=[_decision(refusal.value, None, "accept changed bind settings")],
    )
    assert result.terminal == "finished"
    hits = payloads(run, "check_shadowed")
    assert len(hits) == (2 if changed == "editor" else 4)
    from tests.test_tree_campaign_family14_connection_run import campaign

    rows = campaign(run)
    at = next(i for i, row in enumerate(rows) if row["event"] == "identity_rebased")
    assert (rows[at + 1]["event"] == "check_shadowed") == (changed != "editor")
    assert audit_mismatches(run) == []


def test_cli_prints_modes_and_bind_hits_on_start_and_resume_including_process_log(
    tmp_path, monkeypatch
):
    import asyncio

    from typer.testing import CliRunner

    from evolution.tree import evolve as module
    from evolution.tree.cli import tree_app
    from evolution.tree.execution.check_modes import CONSEQUENCES
    from tests.campaign_fakes import FakeAdapter

    asyncio.run(public_run(tmp_path, monkeypatch, configure=shadow_settings))
    monkeypatch.setattr(module, "_http_session_adapter", lambda *a, **kw: FakeAdapter())
    run = tmp_path / "cli-run"
    command = ["evolve", "--manifest", str(tmp_path / "manifest.json"), "--run-dir", str(run)]
    for _ in range(2):
        result = CliRunner().invoke(tree_app, command)
        assert result.exit_code == 0, result.output
        assert "check shadowed: verbatim_answer_unmatched · bind · count 2" in result.output
        for check, mode in [
            ("verbatim_answer_unmatched", "shadow"),
            ("secret_oracle_path_unmatched", "shadow"),
            ("seal_oracle_leak", "off"),
        ]:
            assert f"note: check {check} is {mode}: {CONSEQUENCES[check]}" in result.output
        logs = list((run / "logs").glob("evolve-*.log"))
        assert logs
        assert any(
            "check shadowed: verbatim_answer_unmatched · bind · count 2" in p.read_text()
            for p in logs
        )
    assert len(payloads(run, "check_shadowed")) == 2


@pytest.mark.parametrize("anchor", ["campaign_created", "identity_rebased"])
@pytest.mark.parametrize("written", [0, 1])
async def test_resume_restores_missing_bind_hits_before_other_records(
    tmp_path, monkeypatch, anchor, written
):
    from evolution.tree.campaign import _Campaign
    from tests.test_tree_campaign_family14_connection_run import campaign
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut

    configure = shadow_settings
    authorizations = []
    if anchor == "identity_rebased":
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path, monkeypatch, configure=shadow_settings, cut="after_attempt_input_frozen"
            )

        def configure(document):
            shadow_settings(document)
            document["checks"]["modes"]["seal_oracle_leak"] = "shadow"

        with pytest.raises(EvolveRefused) as refusal:
            await public_run(tmp_path, monkeypatch, configure=configure)
        authorizations = [_decision(refusal.value, None, "accept check change")]
    original = _Campaign._write_bind_hits

    def cut(self):
        if self.log.snapshot().records[-1].event == anchor:
            if written:
                self.append(
                    "check_shadowed",
                    {**self.bind_hits[0], "round": None, "operation_id": None},
                )
            raise PowerCut()
        original(self)

    with monkeypatch.context() as patch:
        patch.setattr(_Campaign, "_write_bind_hits", cut)
        with pytest.raises(PowerCut):
            await public_run(tmp_path, patch, configure=configure, authorizations=authorizations)
    run = tmp_path / "run"
    prefix = campaign(run)
    at = max(i for i, row in enumerate(prefix) if row["event"] == anchor)
    assert len(prefix) - at - 1 == written
    # No authorization is needed again: the qualifying identity is durable.
    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished"
    rows = campaign(run)
    assert rows[: len(prefix)] == prefix
    hits = rows[at + 1 : at + 3]
    assert [row["event"] for row in hits] == ["check_shadowed", "check_shadowed"]
    assert [(row["payload"]["check"], row["payload"]["count"]) for row in hits] == [
        ("verbatim_answer_unmatched", 2),
        ("secret_oracle_path_unmatched", 1),
    ]
    assert all(
        row["payload"]["round"] is None and row["payload"]["operation_id"] is None for row in hits
    )
    assert len(payloads(run, "check_shadowed")) == (2 if anchor == "campaign_created" else 4)
    assert audit_mismatches(run) == []
    _, resumed = await public_run(tmp_path, monkeypatch, configure=configure)
    assert resumed.terminal == "finished"
    assert campaign(run) == rows
