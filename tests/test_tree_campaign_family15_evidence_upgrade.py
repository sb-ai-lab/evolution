"""An upgrade after the input freeze keeps old bytes until the next round."""

from dataclasses import replace

import pytest

from evolution.tree import campaign as campaign_module
from evolution.tree import evolve as evolve_module
from evolution.tree.campaign_identity import AdaptationIdentityV1
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor_contracts import brief_payload
from tests.campaign_drivers import TWO_CASE_SCORER, audit_mismatches
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family14_visibility_change import _decision
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("old_writer", [True, False], ids=["b12-upgrade", "changed-turns"])
async def test_frozen_common_input_survives_rebase_and_next_round_uses_new_evidence(
    tmp_path, monkeypatch, old_writer
):
    scorer = TWO_CASE_SCORER.replace(
        'passed = "edited" in view or "edited" in skill or bool(payload["oracle"].get("pass"))',
        'passed = bool(payload["oracle"].get("pass"))',
    )
    built = []
    sent = []
    real_brief = campaign_module._Campaign.brief_for
    real_editor = campaign_module.run_editor
    real_config = evolve_module._adaptive_config

    def capture_brief(self, attempt, **kwargs):
        brief = real_brief(self, attempt, **kwargs)
        built.append((attempt.round, brief_payload(brief)))
        return brief

    async def capture_editor(editor, request, **kwargs):
        sent.append(brief_payload(request.brief))
        return await real_editor(editor, request, **kwargs)

    def b12_config(*args, **kwargs):
        config = real_config(*args, **kwargs)
        payload = config.identity.to_payload()
        payload.pop("evidence", None)
        return replace(config, identity=AdaptationIdentityV1(payload))

    monkeypatch.setattr(campaign_module._Campaign, "brief_for", capture_brief)
    monkeypatch.setattr(campaign_module, "run_editor", capture_editor)
    with monkeypatch.context() as patch:
        if old_writer:
            patch.setattr(evolve_module, "_adaptive_config", b12_config)
        with pytest.raises(PowerCut):
            await public_run(
                tmp_path,
                patch,
                campaign={"rounds": 2},
                scorer=scorer,
                cut="after_attempt_input_frozen",
            )

    run = tmp_path / "run"
    frozen = payloads(run, "attempt_input_frozen")[0]
    old_bytes = canonical_json(built[-1][1])
    assert built[-1][1]["version"] == ("edit_brief/4" if old_writer else "edit_brief/6")
    assert payloads(run, "editor_started") == []
    assert sent == []
    built.clear()

    def configure(document):
        if not old_writer:
            document["evidence"] = {"version": "evidence/1", "turns": 20}

    with pytest.raises(EvolveRefused) as caught:
        await public_run(
            tmp_path, monkeypatch, campaign={"rounds": 2}, scorer=scorer, configure=configure
        )
    refusal = caught.value
    assert refusal.code == "run_identity_changed" and refusal.effects == ("rebase",)
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        campaign={"rounds": 2},
        scorer=scorer,
        configure=configure,
        authorizations=[_decision(refusal, None, "accept new evidence inputs")],
    )
    assert result.terminal == "finished", (result.owner, result.code)
    assert len(payloads(run, "identity_rebased")) == 1
    assert (
        payloads(run, "editor_started")[0]["common_input_digest"] == frozen["common_input_digest"]
    )
    first_round = [payload for round_, payload in built if round_ == 1]
    assert first_round and all(canonical_json(payload) == old_bytes for payload in first_round)
    second_round = [payload for round_, payload in built if round_ == 2]
    assert second_round
    assert all(payload["version"] == "edit_brief/6" for payload in second_round)
    assert all(
        payload["evidence"]["turns"] == (8 if old_writer else 20) for payload in second_round
    )
    observations = [
        repeat["observation"] for case in second_round[0]["cases"] for repeat in case["repeats"]
    ]
    assert observations and all(observation["view"] == "full" for observation in observations)
    assert [payload["version"] for payload in sent] == [
        "edit_brief/5" if old_writer else "edit_brief/7",
        "edit_brief/7",
    ]
    assert audit_mismatches(run) == []


async def test_full_episode_shows_only_script_entries_actually_sent(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from tests.campaign_fakes import CONNECTION_PROBING, FakeInstance

    captured = []
    real_send = FakeInstance.send

    async def stop_after_second(self, session, message, request_id):
        result = await real_send(self, session, message, request_id)
        if not CONNECTION_PROBING.get() and self._turns[session] == 2:
            return replace(result, outcome="candidate_failed")
        return result

    monkeypatch.setattr(FakeInstance, "send", stop_after_second)
    real_editor = campaign_module.run_editor

    async def capture_editor(editor, request, **kwargs):
        captured.append((brief_payload(request.brief), request.traces.whole("obs:c1:1")))
        return await real_editor(editor, request, **kwargs)

    def configure(document):
        document["environment"] = {"kind": "scripted"}
        document["campaign"]["budget"]["max_turns"] = 4
        document["campaign"]["budget"]["max_failed_turns"] = 1
        path = Path(document["corpus"]["path"])
        if not path.is_absolute():
            path = tmp_path / path
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row["private"] = {
                "secret": "PRIVATE-CANARY",
                "script": [
                    {"content": "RECEIVED-FIRST", "fields": {"slot": "FIELDS-FIRST"}},
                    {"content": "RECEIVED-SECOND", "fields": {"slot": "FIELDS-SECOND"}},
                    {"content": "UNSENT-THIRD", "fields": {"slot": "UNSENT-FIELDS"}},
                ],
            }
            row["oracle"]["expected"] = "ORACLE-CANARY"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    monkeypatch.setattr(campaign_module, "run_editor", capture_editor)
    run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    assert result.terminal == "finished", (result.owner, result.code)
    assert captured
    brief, raw = captured[0]
    observation = brief["cases"][0]["repeats"][0]["observation"]
    assert observation["view"] == "full"
    assert [turn["input"]["content"] for turn in observation["turns"]] == [
        "RECEIVED-FIRST",
        "RECEIVED-SECOND",
    ]
    for shown in (canonical_json(brief).decode(), raw):
        assert "FIELDS-FIRST" in shown and "FIELDS-SECOND" in shown
        assert all(
            canary not in shown
            for canary in ("UNSENT-THIRD", "UNSENT-FIELDS", "PRIVATE-CANARY", "ORACLE-CANARY")
        )
    assert audit_mismatches(run) == []
