"""H9 audit genuine agent journals and dedicated corruptions of their bytes.

The H8 forge helper rebases locators when record lengths change. Grammar
forgeries use raw JSON because Envelope correctly refuses to encode them.
Lifecycle refusals are asserted as suffix_refused, without disabling any rule;
the separate order forgery reaches verify_evidence's own precondition check.
"""

from __future__ import annotations

import json
import uuid

import pytest

from evolution.tree.campaign_schema import campaign_family_for
from evolution.tree.storage.envelope import Envelope, SchemaError
from tests.agent_grouping_fakes import scripted_grouping_cli
from tests.test_tree_campaign_family14_agent_grouper_chain import agent_chain_run
from tests.test_tree_campaign_family14_agent_grouper_recovery import (
    CONTENT,
    UNKNOWN,
    assert_terminals,
    calls,
    scenario,
)
from tests.test_tree_campaign_family14_audit import audit, forge
from tests.test_tree_campaign_family14_grouper_audit import (
    NOT_FOLLOWED,
    _forge_last,
    _forge_round,
    _refused,
    _rows,
)
from tests.test_tree_campaign_family14_grouper_recovery import _cut, _journal, _of

pytestmark = pytest.mark.slow


async def genuine(tmp_path, monkeypatch, *modes, rounds=1):
    section = scenario(tmp_path, monkeypatch, *modes)
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, rounds)
    assert calls(tmp_path)
    assert_terminals(run)
    result = audit(run)
    assert result["corpus"] == "proven" and result["mismatches"] == []
    return run, snapshot, section


@pytest.mark.parametrize("mode", ["admitted", "partial", *CONTENT, *UNKNOWN])
async def test_genuine_agent_success_content_and_unknown_journals_audit_clean(
    tmp_path, monkeypatch, mode
):
    run, snapshot, _ = await genuine(tmp_path, monkeypatch, mode)
    assert (snapshot.finished is None) == (mode in UNKNOWN)
    starts = _of(run, "grouping_started")
    assert len(starts) == 1 and starts[0]["payload"]["grouper_kind"] == "agent_cli"


@pytest.mark.parametrize("reason", CONTENT)
async def test_genuine_second_link_and_recovered_chain_audit_clean(tmp_path, monkeypatch, reason):
    run, snapshot, section = await genuine(tmp_path, monkeypatch, reason, reason, reason, rounds=2)
    assert snapshot.finished is None and snapshot.grouper_chain == 0
    run, snapshot = await agent_chain_run(tmp_path, monkeypatch, section, 2)
    assert snapshot.finished["outcome"] == "completed" and snapshot.grouper_chain == 1
    assert_terminals(run)
    assert audit(run)["mismatches"] == []


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("chat_calls", "the calls are not the Grouper kind's shape"),
        ("no_calls", "a chat or agent_cli Grouper journals its calls"),
        ("chat_kind", "grouper_kind is not the identity's grouping kind"),
        ("chat_fallback", "the fallback reason is not the Grouper kind's"),
    ],
)
async def test_lifecycle_refuses_agent_kind_calls_and_fallback_mismatch(
    tmp_path, monkeypatch, mutation, message
):
    run, _, _ = await genuine(tmp_path, monkeypatch, "invalid_output")

    def edit(event, payload):
        if event == "grouping_started" and mutation == "chat_kind":
            payload["grouper_kind"] = "chat"
        if event == "grouping_finished":
            if mutation == "chat_calls":
                payload["calls"] = {
                    "map_requests": 1,
                    "reduce": "none",
                    "attempts": 1,
                    "repairs": 0,
                }
            elif mutation == "no_calls":
                payload["calls"] = None
            elif mutation == "chat_fallback":
                payload["fallback"]["reason"] = "input_too_large"

    forge(run, edit)
    _refused(run, message)


@pytest.mark.parametrize("reason", ["provider_exhausted", "provider_rejected", "input_too_large"])
async def test_lifecycle_refuses_chat_only_role_failure_on_agent(tmp_path, monkeypatch, reason):
    run, _, _ = await genuine(tmp_path, monkeypatch, "agent_error")

    def edit(event, payload):
        if event == "grouping_failed":
            payload.update(
                reason=reason, error_class="content" if reason == "input_too_large" else "provider"
            )

    forge(run, edit)
    _refused(run, "the role_failed reason is not the Grouper kind's")


def raw_corruption(run, event, edit, message):
    """Prove the grammar rejection first, then give those bytes to the reader."""
    rows = _journal(run)
    record = next(row for row in rows if row["event"] == event)
    edit(record["payload"])
    with pytest.raises(SchemaError, match=message):
        campaign_family_for(14).validate_payload(event, record["payload"])
    (run / "campaign.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    )
    _refused(run, "invalid journal record")


@pytest.mark.parametrize(
    "mutation", ["mixed_calls", "negative_calls", "wrong_class", "wrong_owner"]
)
async def test_grammar_refuses_invalid_agent_counts_class_and_suspension_owner(
    tmp_path, monkeypatch, mutation
):
    mode = "admitted" if mutation.endswith("calls") else "agent_error"
    run, _, _ = await genuine(tmp_path, monkeypatch, mode)
    if mutation == "mixed_calls":
        raw_corruption(
            run,
            "grouping_finished",
            lambda p: p["calls"].update(map_requests=1),
            "invalid journal record",
        )
    elif mutation == "negative_calls":
        raw_corruption(
            run, "grouping_finished", lambda p: p["calls"].update(agent_calls=-1), "not a count"
        )
    elif mutation == "wrong_class":
        raw_corruption(
            run,
            "grouping_failed",
            lambda p: p.update(error_class="content"),
            "not the reason's class",
        )
    else:
        raw_corruption(
            run, "campaign_suspended", lambda p: p.update(owner="campaign"), "reason and owner"
        )


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("kind", "stage_record names a record of another kind"),
        ("sequence", "stage_record names no failing record"),
        ("operation", "stage_record names another operation"),
    ],
)
async def test_lifecycle_refuses_wrong_suspension_evidence(
    tmp_path, monkeypatch, mutation, message
):
    run, _, _ = await genuine(tmp_path, monkeypatch, "agent_error")

    def edit(payload):
        if mutation == "kind":
            payload.update(reason="grouper_provider_failed")
        elif mutation == "sequence":
            payload["evidence"]["seq"] = _of(run, "grouping_started")[0]["sequence"]
        else:
            payload["evidence"]["operation_id"] = uuid.uuid4().hex

    _forge_last(run, edit)
    _refused(run, message)


async def test_lifecycle_refuses_bogus_suspension_as_chain_breaker(tmp_path, monkeypatch):
    section = scripted_grouping_cli(tmp_path, "invalid_output")
    await _cut(
        lambda patch: agent_chain_run(tmp_path, patch, section, 2),
        monkeypatch,
        "after_grouping_finished",
    )
    run = tmp_path / "run"
    assert audit(run)["mismatches"] == []
    last = _journal(run)[-1]
    assert last["event"] == "grouping_finished" and last["payload"]["fallback"]["scope"] == "all"
    # A fabricated suspension cannot reset the chain by citing a success.
    forged = {
        **last,
        "sequence": last["sequence"] + 1,
        "event": "campaign_suspended",
        "operation_id": None,
        "payload": {
            "reason": "grouper_failing",
            "owner": "grouper",
            "evidence": {
                "evidence_kind": "stage_record",
                "seq": last["sequence"],
                "operation_id": last["operation_id"],
            },
        },
    }
    with (run / "campaign.jsonl").open("ab") as stream:
        stream.write(Envelope.from_payload(forged, family=campaign_family_for(14)).encode())
    _refused(run, "stage_record names no failing record")


@pytest.mark.parametrize("reason", CONTENT)
async def test_lifecycle_refuses_unjustified_content_role_failed(tmp_path, monkeypatch, reason):
    run, _, _ = await genuine(tmp_path, monkeypatch, reason, reason, rounds=2)
    _forge_round(run, "grouping_finished", 1, fallback=None)
    _refused(run, "a content role_failed is only the second full fallback")


@pytest.mark.parametrize("reason", CONTENT)
async def test_lifecycle_refuses_second_full_grouping_finished(tmp_path, monkeypatch, reason):
    run, _, _ = await genuine(tmp_path, monkeypatch, reason, "admitted", rounds=2)
    _forge_round(
        run,
        "grouping_finished",
        2,
        fallback={"kind": "singletons", "scope": "all", "reason": reason},
    )
    _refused(run, "the second full fallback is written as role_failed")


async def test_audit_reports_role_failure_missing_its_suspension(tmp_path, monkeypatch):
    """The lifecycle admits an operator stop; the audit owns the order check."""
    run, _, _ = await genuine(tmp_path, monkeypatch, "agent_error")
    rows = _journal(run)
    rows[-1] = {
        **rows[-1],
        "event": "campaign_finished",
        "operation_id": uuid.uuid4().hex,
        "payload": {
            "outcome": "stopped",
            "reason": "cancelled",
            "continuation": {"kind": "linked_run", "reason": "cancelled"},
            "evidence": None,
        },
    }
    (run / "campaign.jsonl").write_bytes(
        b"".join(
            Envelope.from_payload(row, family=campaign_family_for(14)).encode() for row in rows
        )
    )
    assert _rows(run) == [("grouping_failed", "precondition_failed", NOT_FOLLOWED)]
