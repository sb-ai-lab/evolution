"""Reflector output modes through public campaign admission and persistence."""

import dataclasses
import importlib

import pytest

from evolution.tree.campaign import inspect_campaign
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


SECRET = "reflection-secret-584396"


async def test_reflector_counts_every_leaking_sentence(tmp_path, monkeypatch):
    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_reflector
    monkeypatch.setenv("REFLECTION_TEST_KEY", SECRET)

    def configure(document):
        document["scorer"]["allowed_env"] = ["REFLECTION_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"reflection_run_secret": "shadow"}}

    async def respond(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        return dataclasses.replace(
            outcome,
            groups=tuple(
                {**g, "root_cause": SECRET, "suggested_fix": SECRET} for g in outcome.groups
            ),
        )

    monkeypatch.setattr(campaign, "run_reflector", respond)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished"
    assert payloads(run, "reflection_finished")[0]["shadowed"] == [
        {"check": "reflection_run_secret", "code": "run_secret", "count": 2}
    ]


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_reflection_secret_modes(tmp_path, monkeypatch, mode):
    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_reflector
    monkeypatch.setenv("REFLECTION_TEST_KEY", SECRET)

    def configure(document):
        document["scorer"]["allowed_env"] = ["REFLECTION_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"reflection_run_secret": mode}}

    async def respond(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        return dataclasses.replace(
            outcome, groups=tuple({**group, "root_cause": SECRET} for group in outcome.groups)
        )

    monkeypatch.setattr(campaign, "run_reflector", respond)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    if mode == "enforce":
        assert not payloads(run, "reflection_finished")
        assert payloads(run, "reflection_failed")
    else:
        assert result.terminal == "finished"
        terminal = payloads(run, "reflection_finished")[0]
        assert terminal["groups"][0]["root_cause"] == "[REDACTED]"
        expected = [{"check": "reflection_run_secret", "code": "run_secret", "count": 1}]
        assert terminal.get("shadowed", []) == (expected if mode == "shadow" else [])
        if mode == "shadow":
            logs = list((run / "reflector-logs").glob("*.jsonl"))
            assert any('"event": "check_shadowed"' in log.read_text() for log in logs)
    assert SECRET not in (run / "campaign.jsonl").read_text()
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
def test_input_secret_redaction_uses_the_pin(mode):
    from evolution.tree.execution.check_modes import CheckModes, HitSink
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
    from evolution.tree.roles.input_secrets import input_secret_value

    hits = HitSink()
    pin = EvidencePin(
        DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": mode}).input_modes()
    )
    value = {"tool": {"token": SECRET}, "messages": [SECRET]}
    shown = input_secret_value(value, (SECRET,), pin=pin, hits=hits)
    assert shown == (
        {"tool": {"token": "[REDACTED]"}, "messages": ["[REDACTED]"]}
        if mode == "enforce"
        else value
    )
    assert hits.entries() == (
        [{"check": "input_run_secret_redaction", "code": "redacted", "count": 1}]
        if mode == "shadow"
        else []
    )


async def test_a_crashed_reflection_keeps_hits_only_in_its_private_log(tmp_path, monkeypatch):
    from evolution.tree.execution.check_modes import HitSink
    from tests.test_tree_campaign_family15_delivery import PowerCut

    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_reflector
    original_hit = HitSink.hit
    monkeypatch.setenv("REFLECTION_TEST_KEY", SECRET)

    def configure(document):
        document["scorer"]["allowed_env"] = ["REFLECTION_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"reflection_run_secret": "shadow"}}

    async def respond(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        return dataclasses.replace(
            outcome, groups=tuple({**g, "root_cause": SECRET} for g in outcome.groups)
        )

    def cut(self, *args, **kwargs):
        original_hit(self, *args, **kwargs)
        raise PowerCut()

    with monkeypatch.context() as patch:
        patch.setattr(campaign, "run_reflector", respond)
        patch.setattr(HitSink, "hit", cut)
        with pytest.raises(PowerCut):
            await public_run(tmp_path, patch, configure=configure)
    run = tmp_path / "run"
    assert not payloads(run, "reflection_finished")
    assert not payloads(run, "reflection_failed")
    assert any(
        '"event": "check_shadowed"' in p.read_text()
        for p in (run / "reflector-logs").glob("*.jsonl")
    )
    _, resumed = await public_run(tmp_path, monkeypatch, configure=configure)
    assert resumed.terminal == "finished"
    assert all("shadowed" not in row for row in payloads(run, "reflection_finished"))
    assert all("shadowed" not in row for row in payloads(run, "reflection_failed"))


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
@pytest.mark.parametrize("surface", ["episode", "criterion", "feedback", "diagnostics", "note"])
def test_pinned_reflection_input_modes(tmp_path, mode, surface):
    import copy

    from evolution.tree.execution.check_modes import CheckModes, HitSink
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
    from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
    from tests.campaign_fakes import PRIMARY
    from tests.tree.execution.test_journal_identity import cases
    from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace
    from tests.tree.roles.reflector.test_reflection_inputs import _CLEAN, _scored, first_id

    trace = _Trace(
        tmp_path,
        turns=1,
        events=({"type": "tool_result", "result": SECRET if surface == "episode" else "plain"},),
    )
    try:
        stdout = copy.deepcopy(_CLEAN)
        if surface == "criterion":
            stdout["verdicts"][0]["criterion"] = SECRET
        elif surface == "note":
            stdout["verdicts"][0]["note"] = SECRET
        elif surface == "feedback":
            stdout["public_feedback"]["judge"] = SECRET
        elif surface == "diagnostics":
            stdout["diagnostics"]["route"] = SECRET
        _scored(trace, trace.request_ids[0], stdout)
        hits = HitSink()
        source = FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=(_reflect_row("s1", 1),),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=cases(),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
            scorer_diagnostics=True,
            secrets=(SECRET,),
            pin=EvidencePin(
                DEFAULT_EVIDENCE, CheckModes({"input_run_secret_redaction": mode}).input_modes()
            ),
            hits=hits,
        )
        content = source.read_evidence({"evidence_id": first_id(source)})["content"]
        if surface == "diagnostics":
            content += source.read_grouping_input({"index": 0, "section": "diagnostics"})["content"]
        assert (SECRET in content) is (mode != "enforce")
        assert bool(hits.entries()) is (mode == "shadow")
    finally:
        trace.close()


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_reflection_oracle_modes_preserve_provenance(tmp_path, monkeypatch, mode):
    campaign = importlib.import_module("evolution.tree.campaign")
    original = campaign.run_reflector
    needle = "oracle answer for mode regression"

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"reflection_oracle_leak": mode}}

    async def respond(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        return dataclasses.replace(
            outcome, groups=tuple({**g, "root_cause": needle} for g in outcome.groups)
        )

    monkeypatch.setattr(campaign, "run_reflector", respond)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure,
        extra_cases={"c3": {"critical": True, "pass": True, "answer": needle}},
    )
    if mode == "enforce":
        assert not payloads(run, "reflection_finished")
        failed = payloads(run, "reflection_failed")[0]
        assert failed["error_class"] == "oracle_leak"
        assert failed["leak"] == {"case_id": "c3", "oracle_pointer": "/answer"}
    else:
        assert result.terminal == "finished"
        terminal = payloads(run, "reflection_finished")[0]
        assert terminal["groups"][0]["root_cause"] == needle
        assert terminal.get("shadowed", []) == (
            [
                {
                    "check": "reflection_oracle_leak",
                    "code": "oracle_leak",
                    "count": 1,
                    "leak": {"case_id": "c3", "oracle_pointer": "/answer"},
                }
            ]
            if mode == "shadow"
            else []
        )
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
