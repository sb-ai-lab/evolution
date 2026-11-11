"""The configured claim threshold survives campaign admission and audit."""

import pytest

from evolution.tree.campaign import inspect_campaign
from evolution.tree.evolve import EvolveRefused
from tests.campaign_drivers import TWO_CASE_SCORER
from tests.campaign_fakes import CONNECTION_PROBING, FakeAdapter
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family14_visibility_change import _decision
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run


@pytest.mark.parametrize(("minimum", "source"), [(None, "single-case"), (1, "claim")])
async def test_one_case_claim_selects_configured_scope(tmp_path, monkeypatch, minimum, source):
    def configure(document):
        if minimum is not None:
            document["reflector"]["selection"]["min_claim_cases"] = minimum

    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished"
    scope = payloads(run, "gate_scope_selected")[0]
    assert scope["source"] == source
    assert scope["claimed_cases"] == ["c1"]
    if minimum == 1:
        assert scope["selected_cases"] == ["c1"]
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


async def test_audit_uses_identity_in_force_at_gate_record(tmp_path, monkeypatch):
    import json

    group = {
        "root_cause": "r",
        "suggested_fix": "f",
        "evidence_ids": ["obs:c1:1", "obs:c2:1"],
        "affected_cases": ["c1", "c2"],
        "target_paths": ["skills/SKILL.md"],
        "counterevidence": "c",
        "regression_risks": "r",
        "why_this_layer": "w",
    }
    reflector = (
        "print(" + repr(json.dumps({"status": "reflected", "note": "n", "groups": [group]})) + ")\n"
    )
    scorer = TWO_CASE_SCORER.replace('or bool(payload["oracle"].get("pass"))', "or False")

    def configure(minimum):
        def change(document):
            document["reflector"]["selection"]["min_claim_cases"] = minimum

        return change

    options = {"reflector": reflector, "scorer": scorer}
    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=configure(2),
            cut="after_attempt_input_frozen",
            **options,
        )
    run = tmp_path / "run"
    assert payloads(run, "gate_scope_selected")[0]["source"] == "claim"
    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, configure=configure(3), **options)
    assert caught.value.effects == ("rebase",)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=configure(3),
        authorizations=[_decision(caught.value, None, "accept threshold")],
        **options,
    )
    assert result.terminal == "finished"
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


async def test_one_case_claim_with_all_pairs_excluded_is_unevaluable(tmp_path, monkeypatch):
    import json

    class ExcludeSecond(FakeAdapter):
        def instance_for(self, candidate, instance_id, boot_id):
            instance = super().instance_for(candidate, instance_id, boot_id)
            if not CONNECTION_PROBING.get():
                instance._lookup = lambda session: (
                    "target_failed" if session.case_id == "c2" else "completed"
                )
            return instance

    group = {
        "root_cause": "r",
        "suggested_fix": "f",
        "evidence_ids": ["obs:c2:1"],
        "affected_cases": ["c2"],
        "target_paths": ["skills/SKILL.md"],
        "counterevidence": "c",
        "regression_risks": "r",
        "why_this_layer": "w",
    }
    reflector = (
        "print(" + repr(json.dumps({"status": "reflected", "note": "n", "groups": [group]})) + ")\n"
    )

    def configure(document):
        document["reflector"]["selection"]["min_claim_cases"] = 1
        document["campaign"]["sample_retries"] = 0
        document["campaign"]["indeterminate"]["target"] = {"max_count": 100, "max_rate": "1"}

    run, _ = await public_run(
        tmp_path, monkeypatch, configure=configure, reflector=reflector, adapter_class=ExcludeSecond
    )
    scope = payloads(run, "gate_scope_selected")[0]
    assert (scope["source"], scope["selected_cases"], scope["unevaluable_cases"]) == (
        "claim",
        ["c2"],
        ["c2"],
    )
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
