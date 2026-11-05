"""Persistence of already admitted role text is independent of check modes."""

import dataclasses
import json

import pytest

from evolution.tree.campaign import inspect_campaign
from evolution.tree.roles.editor_contracts import brief_payload
from evolution.tree.run_watch import Snapshot
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family15_delivery import public_run

pytestmark = pytest.mark.slow


SECRET = "journal-test-secret-564891"


@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_the_editor_reads_the_same_redacted_diagnosis_the_journal_holds(
    tmp_path, monkeypatch, mode
):
    import importlib

    campaign = importlib.import_module("evolution.tree.campaign")
    admitted = campaign.admit_reflection_result
    run_editor = campaign.run_editor
    briefs = []
    monkeypatch.setenv("JOURNAL_TEST_KEY", SECRET)

    def configure(document):
        document["scorer"]["allowed_env"] = ["JOURNAL_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"reflection_run_secret": mode}}

    def admit(*args, **kwargs):
        # Inject at the admission/persistence boundary. Mode admission itself is
        # exercised by the reflection-check tests; this tests the fixed guard.
        groups = admitted(*args, **kwargs)
        return tuple(dataclasses.replace(group, root_cause="key " + SECRET) for group in groups)

    async def capture(editor, request, **kwargs):
        briefs.append(brief_payload(request.brief))
        return await run_editor(editor, request, **kwargs)

    monkeypatch.setattr(campaign, "admit_reflection_result", admit)
    monkeypatch.setattr(campaign, "run_editor", capture)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished"
    groups = payloads(run, "reflection_finished")[0]["groups"]
    assert groups[0]["root_cause"] == "key [REDACTED]"
    assert briefs and "key [REDACTED]" in json.dumps(briefs)
    assert SECRET not in json.dumps(briefs)
    assert SECRET not in (run / "campaign.jsonl").read_text()
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
    assert SECRET not in "\n".join(Snapshot.from_state(run).lines())
    _, resumed = await public_run(tmp_path, monkeypatch, configure=configure)
    assert resumed.terminal == "finished"


@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_a_secret_inside_the_fixed_marker_is_a_controlled_persistence_fault(
    tmp_path, monkeypatch, mode
):
    import importlib

    campaign = importlib.import_module("evolution.tree.campaign")
    admitted = campaign.admit_reflection_result
    monkeypatch.setenv("JOURNAL_TEST_KEY", "REDACTED")

    def configure(document):
        document["scorer"]["allowed_env"] = ["JOURNAL_TEST_KEY"]
        document["checks"] = {"version": "checks/1", "modes": {"reflection_run_secret": mode}}

    def admit(*args, **kwargs):
        return tuple(
            dataclasses.replace(group, root_cause="key REDACTED")
            for group in admitted(*args, **kwargs)
        )

    monkeypatch.setattr(campaign, "admit_reflection_result", admit)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "persistence_failed"
    assert result.owner == "campaign_journal"
    assert result.code == "record_matches_secret"
    assert not payloads(run, "reflection_finished")
    assert "REDACTED" not in (run / "campaign.jsonl").read_text()
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
