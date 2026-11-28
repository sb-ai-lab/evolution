"""Check declarations bind recovery without changing comparison conditions."""

import hashlib

import pytest

from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.check_modes import parse_checks
from evolution.tree.execution.journal_identity import canonical_json
from tests.campaign_drivers import _champion_copy, audit_mismatches, native14_created
from tests.test_tree_campaign_family14_run import payloads
from tests.test_tree_campaign_family14_visibility_change import _decision
from tests.test_tree_campaign_family15_delivery import PowerCut, public_run

pytestmark = pytest.mark.slow


def test_duplicate_keys_do_not_hide_a_trailing_escaped_secret_from_agent_grouping():
    from evolution.tree.roles.grouping.agent_grouper import _refuse_secret
    from evolution.tree.roles.role_failures import GroupingExecutionError

    text = r'{"b":"x","a":"y","b":"\u0053ECRETX"}'
    with pytest.raises(GroupingExecutionError, match="secret_detected"):
        _refuse_secret(text, lambda value: "SECRETX" in value)


def test_duplicate_key_fallback_preserves_raw_only_enforcement_boundaries():
    from evolution.tree.execution.check_modes import scan_grouping_wire

    text = r'{"b":"x","a":"y","b":"\u0053ECRETX"}'
    assert not scan_grouping_wire(lambda value: "SECRETX" in value, text, decoded_enforcement=False)


def test_duplicate_key_shadow_fallback_scans_every_string_token():
    from evolution.tree.execution.check_modes import (
        CheckModes,
        HitSink,
        check_predicate,
        scan_grouping_wire,
    )

    hits = HitSink()
    predicate = check_predicate(
        "grouping_run_secret",
        "secret_detected",
        lambda value: "SECRETX" in value,
        modes=CheckModes({"grouping_run_secret": "shadow"}),
        hits=hits,
    )
    assert not scan_grouping_wire(predicate, r'{"b":"x","a":"y","b":"\u0053ECRETX"}')
    assert hits.entries() == [
        {"check": "grouping_run_secret", "code": "secret_detected", "count": 1}
    ]


def test_shadow_field_identity_keeps_the_first_witness_and_logs_once():
    from evolution.tree.execution.check_modes import HitSink

    logs = []
    hits = HitSink(log=logs.append)
    first = {"case_id": "c1", "oracle_pointer": "/first"}
    second = {"case_id": "c1", "oracle_pointer": "/second"}
    hits.hit("grouping_oracle_leak", "oracle_leak", location="label/3", leak=first)
    hits.hit("grouping_oracle_leak", "oracle_leak", location="label/3", leak=second)
    hits.hit("grouping_oracle_leak", "oracle_leak", location="label/4", leak=first)
    assert hits.entries() == [
        {"check": "grouping_oracle_leak", "code": "oracle_leak", "count": 2, "leak": first}
    ]
    assert len(logs) == 2


def test_bind_and_preflight_hits_without_location_still_count_every_call():
    from evolution.tree.execution.check_modes import HitSink

    hits = HitSink()
    hits.hit("connection_preflight", "connection_preflight_unavailable")
    hits.hit("connection_preflight", "connection_preflight_unavailable")
    assert hits.entries()[0]["count"] == 2


def test_serialized_and_decoded_forms_count_once_with_first_witness():
    import json

    from evolution.tree.execution.check_modes import (
        CheckModes,
        HitSink,
        check_predicate,
        scan_grouping_value,
        scan_grouping_wire,
    )

    encoded = r"\u0061\u006c\u0070\u0068\u0061-needle"
    first = {"case_id": "c1", "oracle_pointer": "/encoded"}
    second = {"case_id": "c1", "oracle_pointer": "/decoded"}
    witness = None

    def matches(text):
        nonlocal witness
        if encoded in text:
            witness = first
            return True
        if "alpha-needle" in text:
            witness = second
            return True
        return False

    hits = HitSink()
    predicate = check_predicate(
        "grouping_oracle_leak",
        "oracle_leak",
        matches,
        modes=CheckModes({"grouping_oracle_leak": "shadow"}),
        hits=hits,
        leak=lambda: witness,
    )
    wire = '{"descriptions":{"3":{"label":"' + encoded + '","description":"clean"}}}'
    assert not scan_grouping_wire(predicate, wire)
    assert not scan_grouping_value(predicate, json.loads(wire))
    assert hits.entries() == [
        {"check": "grouping_oracle_leak", "code": "oracle_leak", "count": 1, "leak": first}
    ]


def test_two_fields_with_the_same_witness_share_one_entry():
    from evolution.tree.execution.check_modes import (
        CheckModes,
        HitSink,
        check_predicate,
        scan_grouping_wire,
    )

    hits = HitSink()
    predicate = check_predicate(
        "grouping_run_secret",
        "secret_detected",
        lambda text: "secret88" in text,
        modes=CheckModes({"grouping_run_secret": "shadow"}),
        hits=hits,
    )
    assert not scan_grouping_wire(
        predicate, '{"descriptions":{"0":{"label":"secret88","description":"secret88"}}}'
    )
    assert hits.entries() == [
        {"check": "grouping_run_secret", "code": "secret_detected", "count": 2}
    ]


@pytest.mark.parametrize("needle", ['"needle"', "1.0"])
def test_located_wire_scan_keeps_the_original_enforcement_surface(needle):
    from evolution.tree.execution.check_modes import scan_grouping_wire

    # Wire guards saw the complete document and decoded string tokens, never
    # newly encoded scalar fragments. Location tracking must not add refusals.
    assert not scan_grouping_wire(lambda text: text == needle, '{"text":"needle","number":1e0}')


def declaring(mode):
    def configure(document):
        if mode is not None:
            document["checks"] = {"version": "checks/1", "modes": {"seal_oracle_leak": mode}}

    return configure


@pytest.mark.parametrize("mode", [None, "off"])
async def test_checks_bind_an_identity_digest_and_an_adaptation_member_only_when_declared(
    tmp_path, monkeypatch, mode
):
    run, result = await public_run(tmp_path, monkeypatch, configure=declaring(mode))
    assert result.terminal == "finished"
    (created,) = payloads(run, "campaign_created")
    assert ("checks" in created["identity"]) is (mode is not None)
    assert ("checks" in created["adaptation_payload"]) is (mode is not None)
    if mode is not None:
        modes = parse_checks({"version": "checks/1", "modes": {"seal_oracle_leak": mode}})
        assert created["identity"]["checks"] == modes.digest
        assert created["adaptation_payload"]["checks"] == modes.to_payload()
    assert audit_mismatches(run) == []


async def test_a_changed_checks_declaration_is_one_rebase(tmp_path, monkeypatch):
    with pytest.raises(PowerCut):
        await public_run(tmp_path, monkeypatch, cut="after_attempt_input_frozen")
    run = tmp_path / "run"
    before = (run / "campaign.jsonl").read_bytes()
    with pytest.raises(EvolveRefused) as caught:
        await public_run(tmp_path, monkeypatch, configure=declaring("shadow"))
    refused = caught.value
    assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
    assert {d.field for d in refused.differences} == {"identity.checks", "adaptation"}
    assert (run / "campaign.jsonl").read_bytes() == before
    run, result = await public_run(
        tmp_path,
        monkeypatch,
        configure=declaring("shadow"),
        authorizations=[_decision(refused, None, reason="accept checks")],
    )
    assert result.terminal == "finished"
    (rebased,) = payloads(run, "identity_rebased")
    assert set(rebased["changed_fields"]) == {"identity.checks", "adaptation"}
    assert audit_mismatches(run) == []


async def test_a_link_ignores_checks(tmp_path, monkeypatch):
    source, _ = await public_run(tmp_path / "source", monkeypatch)
    run, result = await public_run(
        tmp_path / "next",
        monkeypatch,
        linked_from=(source, _champion_copy(source)),
        configure=declaring("off"),
    )
    assert result.terminal == "finished"
    (created,) = payloads(run, "campaign_created")
    linked = created["identity"]["linked_from"]
    assert linked["predecessor_family"] == 15
    assert "condition_changed" not in linked
    assert audit_mismatches(run) == []


def test_family14_refuses_an_identity_with_checks():
    from evolution.tree.campaign_schema import validate_payload
    from evolution.tree.storage.envelope import SchemaError

    value = native14_created()
    value["identity"]["checks"] = "a" * 64
    value["identity_digest"] = hashlib.sha256(canonical_json(value["identity"])).hexdigest()
    with pytest.raises(SchemaError):
        validate_payload("campaign_created", value, final=True)


async def test_status_report_and_watch_list_non_enforce_modes(tmp_path, monkeypatch):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.run_watch import Snapshot

    run, result = await public_run(tmp_path, monkeypatch, configure=declaring("off"))
    expected = {"seal_oracle_leak": "off"}
    assert result.result.to_payload()["check_modes"] == expected
    status = inspect_campaign(run, verify=True)
    assert status.to_payload()["check_modes"] == expected
    assert "checks: seal_oracle_leak=off" in "\n".join(Snapshot.from_state(run).lines())


async def test_finished_carriers_admit_shadowed_without_changing_the_original_payload(
    tmp_path, monkeypatch
):
    from evolution.tree.campaign_schema import validate_payload
    from evolution.tree.family15_schema import validate_family15_payload
    from evolution.tree.storage.envelope import SchemaError
    from tests.campaign_drivers import _records

    run, _ = await public_run(tmp_path, monkeypatch)
    expected = {
        "grouping_finished",
        "reflection_finished",
        "attempt_input_frozen",
        "editor_finished",
        "attempt_closed",
    }
    seen = set()
    entry = {"check": "input_run_secret_redaction", "code": "redacted", "count": 1}
    for record in _records(run / "campaign.jsonl"):
        event, payload = record["event"], record["payload"]
        if event in expected:
            value = {**payload, "shadowed": [entry]}
            validate_family15_payload(event, value)
            assert value["shadowed"] == [entry]
            with pytest.raises(SchemaError):
                validate_payload(event, value, final=True)
            seen.add(event)
        elif event in {"campaign_created", "baseline_bound", "instance_attested"}:
            with pytest.raises(SchemaError):
                validate_family15_payload(event, {**payload, "shadowed": [entry]})
    assert seen == expected


@pytest.mark.parametrize("guard", ["journal_secret", "frozen_digest", "trace_identity"])
async def test_guards_ignore_checks(tmp_path, monkeypatch, guard):
    from evolution.tree.campaign import CampaignInvariantViolated, _Campaign
    from evolution.tree.execution.check_modes import CHECK_NAMES, CheckModes

    modes = CheckModes(dict.fromkeys(CHECK_NAMES, "off"))
    secret = "guard-secret-948573"
    monkeypatch.setenv("GUARD_TEST_KEY", secret)

    def configure(document):
        document["checks"] = modes.to_payload()
        document["scorer"]["allowed_env"] = ["GUARD_TEST_KEY"]

    if guard == "journal_secret":
        from evolution.tree.storage import envelope

        original = _Campaign.hook
        secret_match = envelope.secret_match
        refused = []

        def observe_guard(value, secrets):
            found = secret_match(value, secrets)
            if found is not None:
                refused.append(value)
            return found

        monkeypatch.setattr(envelope, "secret_match", observe_guard)

        def inject(self, name):
            original(self, name)
            if name == "after_campaign_created":
                assert self.checks == modes
                self.append(
                    "check_shadowed",
                    dict(
                        check="seal_oracle_leak",
                        code="oracle_leak",
                        count=1,
                        round=None,
                        operation_id=None,
                        leak={"case_id": secret, "oracle_pointer": "/answer"},
                    ),
                )

        monkeypatch.setattr(_Campaign, "hook", inject)
        with pytest.raises(CampaignInvariantViolated, match="invalid journal record"):
            await public_run(tmp_path, monkeypatch, configure=configure)
        assert len(refused) == 1 and refused[0]["event"] == "check_shadowed"
        assert secret not in (tmp_path / "run" / "campaign.jsonl").read_text()
    elif guard == "frozen_digest":
        from dataclasses import replace

        with pytest.raises(PowerCut):
            await public_run(
                tmp_path, monkeypatch, configure=configure, cut="after_attempt_input_frozen"
            )
        original = _Campaign.brief_for

        def drift(self, *args, **kwargs):
            assert self.checks == modes
            return replace(original(self, *args, **kwargs), operator_context="changed after freeze")

        monkeypatch.setattr(_Campaign, "brief_for", drift)
        with pytest.raises(CampaignInvariantViolated, match="frozen editor common input changed"):
            await public_run(tmp_path, monkeypatch, configure=configure)
    else:
        from evolution.tree.baseline_diagnostics import DiagnosticTraceReader
        from evolution.tree.baseline_traces import BaselineTraces
        from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
        from evolution.tree.roles.reflection_reader import ReaderIntegrityError
        from tests.tree.execution.test_journal_identity import cases
        from tests.tree.roles.editor.test_editor_evidence import _Trace

        trace = _Trace(tmp_path, turns=1)
        try:
            reader = BaselineTraces(
                traces=DiagnosticTraceReader(trace.run_root),
                measurement_records=trace.journal.snapshot().records,
                pin=EvidencePin(DEFAULT_EVIDENCE, modes.input_modes()),
                secrets=(),
                corpus=cases(),
                baseline=trace.baseline,
            )
            reader.page("obs:c:1")
            path = trace.directory / "request.json"
            path.write_bytes(path.read_bytes() + b" ")
            with pytest.raises(ReaderIntegrityError, match="source_changed"):
                reader.page("obs:c:1")
        finally:
            trace.close()


@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_repeated_reads_follow_both_roles_to_the_report(tmp_path, monkeypatch, mode):
    from evolution.tree.campaign import inspect_campaign
    from evolution.tree.run_watch import Snapshot
    from tests import fake_chat_server
    from tests.test_tree_campaign_family14_reflector_history import chat_reflector
    from tests.test_tree_campaign_family14_role_feedback import (
        _chat_plan,
        _chat_write,
        chat_budget_section,
    )
    from tests.test_tree_campaign_family15_role_contracts import b17_logs, b17_reflector
    from tests.test_tree_campaign_family15_role_failures import CHAT_GROUP

    check = "repeated_read_without_progress"
    tool = fake_chat_server.tool_call
    read = {"path": "skills/SKILL.md", "start_line": 1, "end_line": 1}
    page = tool("read_file", read)
    quote = tool("read_file", {**read, "reread": True})
    base, server = fake_chat_server.start(
        [
            tool("list_files"),
            page,
            quote,
            tool("list_evidence"),
            quote,
            tool("finish_reflection", {"groups": [CHAT_GROUP]}),
            tool("list_files"),
            page,
            quote,
            tool("list_evidence"),
            quote,
            quote,
            _chat_plan(["skills/SKILL.md"]),
            _chat_write("skills/SKILL.md", "edited\n"),
            tool("finish_edit", {"summary": "done"}),
        ]
    )
    monkeypatch.setenv("E1_KEY", "test-key")

    def configure(document):
        document["checks"] = {"version": "checks/1", "modes": {check: mode}}
        b17_reflector(document, {**chat_reflector(base), "max_protocol_repairs": 0})
        document["editor"] = {**chat_budget_section(base), "max_protocol_repairs": 0}

    try:
        run, result = await public_run(tmp_path, monkeypatch, configure=configure)
    finally:
        server.shutdown()
        server.server_close()

    for event, count in [("reflection_finished", 1), ("editor_finished", 2)]:
        (ended,) = payloads(run, event)
        entries = [{"check": check, "code": "protocol", "count": count}] if mode == "shadow" else []
        assert ended.get("shadowed", []) == entries
    for role, quotes in (("reflector", 2), ("editor", 3)):
        results = [
            row["result"]["content"]
            for row in b17_logs(run, f"{role}-logs")
            if row.get("phase") == "tool_call"
            and row["tool"] == "read_file"
            and row["arguments"].get("reread") is True
        ]
        assert results == ["seed skill\n"] * quotes
    expected_hits = {check: 3} if mode == "shadow" else {}
    assert result.result.to_payload().get("shadow_hits", {}) == expected_hits
    status = inspect_campaign(run, verify=True).to_payload()
    assert status["check_modes"] == {check: mode}
    assert status.get("shadow_hits", {}) == expected_hits
    snapshot = "\n".join(Snapshot.from_state(run).lines())
    assert f"checks: {check}={mode}" in snapshot
    assert (f"shadow hits: {check} · count 3" in snapshot) == (mode == "shadow")


def test_repeated_read_shadow_entry_admits_only_its_count():
    from evolution.tree.family15_schema import validate_family15_payload
    from evolution.tree.storage.envelope import SchemaError

    entry = {"check": "repeated_read_without_progress", "code": "protocol", "count": 1}
    terminal = {
        "attempt_id": "r01-a01",
        "operation_id": "op",
        "status": "edited",
        "continued": False,
        "note_digest": "a" * 64,
        "shadowed": [entry],
    }
    validate_family15_payload("editor_finished", terminal)
    with pytest.raises(SchemaError):
        validate_family15_payload(
            "editor_finished", {**terminal, "shadowed": [{**entry, "leak": {"case_id": "c1"}}]}
        )
