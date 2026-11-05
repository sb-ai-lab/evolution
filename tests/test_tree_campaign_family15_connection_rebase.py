"""Admission audit follows mode windows while a measurement is still open."""

import pytest

from evolution.tree.evolve import EvolveRefused
from tests.campaign_drivers import EDITED, audit_mismatches
from tests.test_tree_campaign_family14_connection_run import ConnectedAdapter, payloads
from tests.test_tree_campaign_family14_visibility_change import _decision
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow


def preflight_mode(mode):
    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"connection_preflight": mode}}

    return configure


@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_preflight_rebase_allows_starts_in_an_already_bound_measurement(
    tmp_path, monkeypatch, mode
):
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_send = "time_budget"
    run, result = await public_run(
        tmp_path, monkeypatch, configure=preflight_mode("enforce"), adapter=adapter
    )
    assert (result.terminal, result.code) == ("suspended", "connection_preflight_inconclusive")
    with pytest.raises(EvolveRefused) as refused:
        await public_run(tmp_path, monkeypatch, configure=preflight_mode(mode), adapter=adapter)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=preflight_mode(mode),
        adapter=adapter,
        authorizations=[_decision(refused.value, None, "accept preflight mode")],
    )
    assert result.terminal == "finished"
    assert len(payloads(run, "connection_preflight_failed")) == (2 if mode == "shadow" else 1)
    hits = [p for p in payloads(run, "check_shadowed") if p["check"] == "connection_preflight"]
    assert len(hits) == (1 if mode == "shadow" else 0)
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("compatible", [False, True])
async def test_old_shadow_starts_remain_valid_after_a_new_enforced_preflight(
    tmp_path, monkeypatch, compatible
):
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_send = "time_budget"
    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=preflight_mode("shadow"),
            adapter=adapter,
            cut="after_attempt_input_frozen",
        )
    run = tmp_path / "run"
    assert audit_mismatches(run) == []
    if compatible:
        adapter.probe_send = None
    with pytest.raises(EvolveRefused) as refused:
        await public_run(
            tmp_path, monkeypatch, configure=preflight_mode("enforce"), adapter=adapter
        )
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=preflight_mode("enforce"),
        adapter=adapter,
        authorizations=[_decision(refused.value, None, "require preflight again")],
    )
    assert result.terminal == ("finished" if compatible else "suspended")
    assert len(payloads(run, "connection_preflight_started")) == 2
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("later_off", [False, True])
async def test_enforced_starts_without_an_admission_remain_an_audit_finding(
    tmp_path, monkeypatch, later_off
):
    from evolution.tree.execution.journal_rollout import ConnectionAdmission

    claim = ConnectionAdmission.claim

    def drop_admission(self):
        # Simulate a producer omitting the required admission without editing
        # saved journals or their references; all other lifecycle work is real.
        claim(self)
        return None

    monkeypatch.setattr(ConnectionAdmission, "claim", drop_admission)
    adapter = ConnectedAdapter(used=EDITED["used"])
    if later_off:
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                monkeypatch,
                configure=preflight_mode("enforce"),
                adapter=adapter,
                cut="after_attempt_input_frozen",
            )
        with pytest.raises(EvolveRefused) as refused:
            await public_run(
                tmp_path, monkeypatch, configure=preflight_mode("off"), adapter=adapter
            )
        run, result = await public_run(
            tmp_path,
            monkeypatch,
            configure=preflight_mode("off"),
            adapter=adapter,
            authorizations=[_decision(refused.value, None, "off after baseline completed")],
        )
    else:
        run, result = await public_run(
            tmp_path, monkeypatch, configure=preflight_mode("enforce"), adapter=adapter
        )
    assert result.terminal == "finished"
    findings = audit_mismatches(run)
    assert any(
        f["code"] == "precondition_failed"
        and f["detail"] == "the binding's executed starts carry no admission"
        and f["locator"].startswith("measurements/m-r00-champion/")
        for f in findings
    ), findings
