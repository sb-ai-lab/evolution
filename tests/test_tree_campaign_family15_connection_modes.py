"""Connection checks through real offline family-15 campaigns."""

import pytest

from tests.campaign_drivers import EDITED, audit_mismatches
from tests.test_tree_campaign_family14_connection_run import ConnectedAdapter, payloads
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
async def test_preflight_modes_settle_an_inconclusive_binding(tmp_path, monkeypatch, mode):
    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_send = "time_budget"

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"connection_preflight": mode}}

    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
    assert result.terminal == ("suspended" if mode == "enforce" else "finished")
    failed = payloads(run, "connection_preflight_failed")
    assert len(failed) == (0 if mode == "off" else 1)
    hits = [p for p in payloads(run, "check_shadowed") if p["check"] == "connection_preflight"]
    assert len(hits) == (1 if mode == "shadow" else 0)
    if hits:
        assert (
            hits[0]["operation_id"]
            == payloads(run, "connection_preflight_started")[0]["operation_id"]
        )
        assert hits[0]["code"] == "connection_preflight_inconclusive"
    assert audit_mismatches(run) == []
    if mode != "enforce":
        assert payloads(run, "instance_attested")
        assert not payloads(run, "campaign_suspended")
        probes = len(adapter.probes)
        _, resumed = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
        assert resumed.terminal == "finished"
        assert len(adapter.probes) == probes


async def test_declared_probe_retry_reaches_the_scored_last_try(tmp_path, monkeypatch):
    from tests.tree.target.test_connection_probe_runner import ScriptedDeadlineExceeded

    adapter = ConnectedAdapter(used=EDITED["used"])
    calls = []

    def send():
        calls.append(1)
        if len(calls) == 1:
            raise ScriptedDeadlineExceeded("first try")

    adapter.probe_send = send

    def configure(document):
        document["target"]["preflight_sample_retries"] = 9

    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
    assert result.terminal == "finished"
    assert len(calls) == 2
    assert len(payloads(run, "connection_preflight_finished")) == 1
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("preflight", ["enforce", "shadow", "off"])
@pytest.mark.parametrize("description_mode", ["enforce", "shadow", "off"])
async def test_description_check_is_independent_and_always_redacts(
    tmp_path, monkeypatch, preflight, description_mode
):
    from tests.test_tree_campaign_family14_connection_run import (
        JUDGE,
        assert_nowhere,
        forward_judge,
    )

    adapter = ConnectedAdapter(used=EDITED["used"], description={"model": JUDGE})

    def configure(document):
        document["checks"] = {
            "version": "checks/1",
            "modes": {
                "connection_preflight": preflight,
                "connection_description_secret": description_mode,
            },
        }

    forward_judge(monkeypatch)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
    if description_mode == "enforce":
        assert (result.terminal, result.code) == (
            ("persistence_failed", "record_matches_secret")
            if preflight == "off"
            else ("suspended", "connection_incompatible")
        )
    else:
        assert result.terminal == "finished", (result.terminal, result.code)
        descriptions = payloads(run, "instance_attested")
        assert descriptions and all(
            p["description"] == {"model": "[REDACTED]"} for p in descriptions
        )
        hits = [
            p
            for p in payloads(run, "check_shadowed")
            if p["check"] == "connection_description_secret"
        ]
        assert len(hits) == (
            len(descriptions) + (preflight != "off") if description_mode == "shadow" else 0
        )
    assert audit_mismatches(run) == []
    assert_nowhere(run, JUDGE)


async def test_shadow_recovers_the_gap_after_the_failed_terminal(tmp_path, monkeypatch):
    from tests.test_tree_campaign_family15_delivery import PowerCut

    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_send = "time_budget"

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {"connection_preflight": "shadow"}}

    with pytest.raises(PowerCut):
        await public_run(
            tmp_path,
            monkeypatch,
            configure=configure,
            adapter=adapter,
            cut="after_connection_preflight_terminal",
        )
    assert len(adapter.probes) == 1
    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
    assert result.terminal == "finished"
    assert len(adapter.probes) == 1
    assert len(payloads(run, "check_shadowed")) == 1
    assert not payloads(run, "campaign_suspended")
    assert audit_mismatches(run) == []


async def test_preflight_shadow_does_not_hide_incompatible_fields(tmp_path, monkeypatch):
    from tests.tree.manifest.test_manifest_v15 import METRIC_CONTRACT

    adapter = ConnectedAdapter(used=EDITED["used"])
    adapter.probe_events = ()

    def configure(document):
        document["scorer"]["metric_contract"] = METRIC_CONTRACT
        document["checks"] = {"version": "checks/1", "modes": {"connection_preflight": "shadow"}}

    run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
    assert (result.terminal, result.code) == ("suspended", "connection_incompatible")
    assert not payloads(run, "check_shadowed")
    assert audit_mismatches(run) == []


@pytest.mark.parametrize("mode", ["shadow", "off", "enforce"])
@pytest.mark.parametrize("surface", ["value_at_limit", "key"])
async def test_unrepresentable_redacted_description_is_a_repeatable_persistence_fault(
    tmp_path, monkeypatch, mode, surface
):
    from evolution.tree.execution.check_modes import redact_for_journal
    from evolution.tree.execution.wire_v2 import canonical_body, validate_instance_description

    secret = "pw-12345"
    monkeypatch.setenv("DESCRIPTION_BOUND_KEY", secret)
    if surface == "value_at_limit":
        description = {"note": secret, "pad": ""}
        description["pad"] = "x" * (4096 - len(canonical_body(description)))
        assert len(canonical_body(description)) == 4096
        assert len(canonical_body(redact_for_journal(description, (secret,)))) == 4098
    else:
        description = {secret: "target description"}
    assert validate_instance_description(description) == description
    adapter = ConnectedAdapter(used=EDITED["used"], description=description)

    def configure(document):
        document["scorer"]["allowed_env"] = ["DESCRIPTION_BOUND_KEY"]
        document["checks"] = {
            "version": "checks/1",
            "modes": {"connection_preflight": "off", "connection_description_secret": mode},
        }

    for _ in range(2):
        run, result = await public_run(tmp_path, monkeypatch, configure=configure, adapter=adapter)
        assert (result.terminal, result.owner, result.code) == (
            "persistence_failed",
            "campaign_journal",
            "record_matches_secret",
        )
        assert not payloads(run, "instance_attested")
        assert secret not in (run / "campaign.jsonl").read_text()
        assert audit_mismatches(run) == []
