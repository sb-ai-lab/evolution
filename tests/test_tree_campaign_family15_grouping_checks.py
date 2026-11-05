"""Grouper checks cross real command, chat and agent CLI boundaries offline."""

import json

import pytest

from evolution.tree.campaign import inspect_campaign
from tests.agent_grouping_fakes import GROUP_ALL_AGENT_BODY, sealed_grouping_cli
from tests.model_grouping_fakes import PortionGrouper, chat_grouper_section, use_chat_grouper
from tests.test_tree_campaign_family14_grouper_run import GROUP_ALL_PROGRAM, _command_grouper
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


SECRET = "grouper-secret-584396"
NEEDLE = "oracle answer for grouping mode regression"


@pytest.mark.parametrize("kind", ["command", "chat", "agent_cli"])
@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize(
    "check,text", [("grouping_run_secret", SECRET), ("grouping_oracle_leak", NEEDLE)]
)
async def test_grouper_output_modes(tmp_path, monkeypatch, kind, mode, check, text):
    monkeypatch.setenv("GROUPING_TEST_KEY", SECRET)
    if kind == "command":
        section = _command_grouper(
            tmp_path, GROUP_ALL_PROGRAM.replace("'label': 'every failure'", f"'label': {text!r}")
        )
    elif kind == "chat":
        section = chat_grouper_section()
        # One grouping of one portion: the preflight and one answer, as the
        # `/15` script allowed one reply; a repair or a second grouping fails.
        client = use_chat_grouper(monkeypatch, PortionGrouper(title=text, budget=2))
    else:
        section = sealed_grouping_cli(
            tmp_path, body=GROUP_ALL_AGENT_BODY.replace("shared failures", text)
        )

    def configure(document):
        document["grouper"] = section
        document["scorer"]["allowed_env"] = ["GROUPING_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {check: mode}}

    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        extra_cases={"c3": {"critical": True, "pass": True, "answer": NEEDLE}},
    )
    finished = payloads(run, "grouping_finished")
    failed = payloads(run, "grouping_failed")
    if kind == "chat":
        assert len(client.requests) == 2
        assert len(finished) + len(failed) == 1
    if mode == "enforce":
        if check == "grouping_run_secret" or kind == "command":
            assert not finished and failed
            assert failed[0]["code"] == (
                "secret_detected" if check == "grouping_run_secret" else "oracle_leak"
            )
        else:
            assert finished and text not in json.dumps(finished[0]["result"])
    else:
        assert result.terminal == "finished", (result.terminal, result.owner, result.code)
        assert finished and not failed
        expected = "[REDACTED]" if check == "grouping_run_secret" else text
        assert expected in json.dumps(finished[0]["result"])
        entries = finished[0].get("shadowed", [])
        assert bool(entries) is (mode == "shadow")
        if mode == "shadow":
            assert all(entry["check"] == check for entry in entries)
            assert sum(entry["count"] for entry in entries) == 1
            if check == "grouping_oracle_leak":
                assert all(
                    entry["leak"] == {"case_id": "c3", "oracle_pointer": "/answer"}
                    for entry in entries
                )
            assert any(
                '"event": "check_shadowed"' in p.read_text()
                for p in (run / "reflector-logs").glob("*.jsonl")
            )
    assert SECRET not in (run / "campaign.jsonl").read_text()
    assert all(SECRET not in p.read_text() for p in (run / "reflector-logs").glob("*.jsonl"))
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_fields_grouper_without_output_text_has_no_hits(tmp_path, monkeypatch, mode):
    def configure(document):
        document["checks"] = {
            "version": "checks/1",
            "modes": {"grouping_oracle_leak": mode, "grouping_run_secret": mode},
        }

    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished"
    assert payloads(run, "grouping_started")[0]["grouper_kind"] == "fields"
    assert "shadowed" not in payloads(run, "grouping_finished")[0]


async def test_shadowed_oracle_does_not_relabel_an_enforced_secret_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUPING_TEST_KEY", SECRET)
    section = _command_grouper(
        tmp_path, GROUP_ALL_PROGRAM.replace("every failure", NEEDLE + " " + SECRET)
    )

    def configure(document):
        document["grouper"] = section
        document["scorer"]["allowed_env"] = ["GROUPING_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"grouping_oracle_leak": "shadow"}}

    run, _ = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        extra_cases={"c3": {"critical": True, "pass": True, "answer": NEEDLE}},
    )
    terminal = payloads(run, "grouping_failed")[0]
    assert terminal["code"] == "secret_detected"
    assert terminal["shadowed"][0]["check"] == "grouping_oracle_leak"
    assert "leak" not in terminal
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_redacting_an_admitted_group_does_not_relax_its_label_bound(
    tmp_path, monkeypatch, mode
):
    secret = "secret88"
    monkeypatch.setenv("GROUPING_TEST_KEY", secret)
    label = "x" * (128 - len(secret)) + secret
    body = GROUP_ALL_PROGRAM.replace("'label': 'every failure'", f"'label': {label!r}")
    section = _command_grouper(tmp_path, body)

    def configure(document):
        document["grouper"] = section
        document["scorer"]["allowed_env"] = ["GROUPING_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"grouping_run_secret": mode}}

    run, _ = await public_run(tmp_path, monkeypatch, configure=configure)
    assert not payloads(run, "grouping_finished")
    assert payloads(run, "grouping_failed")[0]["code"] == "malformed_result"
    assert secret not in (run / "campaign.jsonl").read_text()
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


async def test_a_crashed_grouping_keeps_hits_only_in_the_private_log(tmp_path, monkeypatch):
    from evolution.tree.execution.check_modes import HitSink
    from tests.test_tree_campaign_family15_delivery import PowerCut

    monkeypatch.setenv("GROUPING_TEST_KEY", SECRET)
    section = _command_grouper(tmp_path, GROUP_ALL_PROGRAM.replace("every failure", SECRET))

    def configure(document):
        document["grouper"] = section
        document["scorer"]["allowed_env"] = ["GROUPING_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"grouping_run_secret": "shadow"}}

    original = HitSink.hit

    def cut(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(HitSink, "hit", cut)
        with pytest.raises(PowerCut):
            await public_run(tmp_path, patch, configure=configure)
    run = tmp_path / "run"
    assert not payloads(run, "grouping_finished")
    assert not payloads(run, "grouping_failed")
    _, resumed = await public_run(tmp_path, monkeypatch, configure=configure)
    assert resumed.terminal == "finished"
    assert all("shadowed" not in row for row in payloads(run, "grouping_failed"))
    terminal = payloads(run, "grouping_finished")[0]
    logged = [
        json.loads(line)
        for p in (run / "reflector-logs").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    hits = [row for row in logged if row.get("event") == "check_shadowed"]
    assert sum(row["count"] for row in hits) == 1 + sum(
        row["count"] for row in terminal["shadowed"]
    )
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


async def test_reused_baseline_readers_follow_the_next_round_pin(tmp_path, monkeypatch):
    from evolution.tree.campaign import _Campaign
    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import TWO_CASE_SCORER
    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_campaign_family15_delivery import PowerCut

    scorer = TWO_CASE_SCORER.replace(
        'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
        'passed = bool(payload["oracle"].get("pass"))',
    )
    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            campaign={"rounds": 2},
            scorer=scorer,
            cut="after_attempt_input_frozen",
        )

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"input_run_secret_redaction": "off"}}

    with pytest.raises(EvolveRefused) as refusal:
        await public_run(
            tmp_path, monkeypatch, campaign={"rounds": 2}, scorer=scorer, configure=configure
        )
    original = _Campaign.adaptive_inputs_for
    seen = []

    def capture(self, number, **kwargs):
        inputs = original(self, number, **kwargs)
        pin = self.snapshot().round_evidence(number)
        assert inputs.pin == pin
        seen.append(
            (number, inputs.baseline_binding, pin.input_modes["input_run_secret_redaction"])
        )
        return inputs

    monkeypatch.setattr(_Campaign, "adaptive_inputs_for", capture)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        campaign={"rounds": 2},
        scorer=scorer,
        configure=configure,
        authorizations=[_decision(refusal.value, None, "accept input mode")],
    )
    assert result.terminal == "finished"
    assert {number for number, _, _ in seen} == {1, 2}
    assert {mode for number, _, mode in seen if number == 1} == {"enforce"}
    assert {mode for number, _, mode in seen if number == 2} == {"off"}
    assert len({binding for _, binding, _ in seen}) == 1
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


def test_oracle_reduction_cache_follows_pinned_criterion_visibility():
    from dataclasses import replace
    from types import SimpleNamespace

    from evolution.tree.campaign import _Campaign
    from evolution.tree.execution.check_modes import CheckModes
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
    from tests.tree.execution.test_journal_identity import cases

    criterion = "oracle_criterion_584396"
    controller = object.__new__(_Campaign)
    controller._family_version = 15
    controller.corpus = (replace(cases()[0], oracle={"answer": criterion}),)
    controller.declared_answers = ()
    controller.secret_oracle_paths = None
    controller.baseline_needles_cache = None
    first = SimpleNamespace(
        baseline_binding="same-baseline",
        published_criteria=frozenset(),
        pin=EvidencePin(DEFAULT_EVIDENCE, CheckModes({}).input_modes()),
    )
    next_round = SimpleNamespace(
        baseline_binding="same-baseline",
        published_criteria=frozenset({criterion}),
        pin=EvidencePin(
            DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": "off"}).input_modes()
        ),
    )
    assert criterion in controller._reflection_needles_for(first)
    assert criterion not in controller._reflection_needles_for(next_round)
