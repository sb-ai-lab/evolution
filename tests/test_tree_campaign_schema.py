"""Shared durable references and current family-14 payload vocabulary."""

import copy
import json
import re
from pathlib import Path

import pytest

from evolution.tree.campaign_schema import (
    _FINAL_ATTEMPT_OUTCOMES,
    CampaignLifecycle,
    _validate_identity_connection_fields,
    campaign_family_for,
    feedback_outcomes,
    reduce,
    validate_payload,
)
from evolution.tree.execution.durable_reference import DurableReference
from evolution.tree.storage.envelope import Envelope, SchemaError


def ref(kind="journal_prefix", locator="events.jsonl#bytes=0:512"):
    return dict(version=1, kind=kind, run_id="run", locator=locator, content_digest="b" * 64)


def refuses(rule):
    """Assert the exact rule raised by the payload validator."""
    return pytest.raises(SchemaError, match=f"^{re.escape(rule)}$")


def test_durable_reference_round_trips_and_refuses_a_bad_locator():
    reference = DurableReference.from_payload(ref())
    assert reference.to_payload() == ref()
    for bad, rule in (
        (dict(ref(), version=2), "reference version is 1"),
        (dict(ref(), kind="measurement_prefix"), "reference kind is not in the registry"),
        (dict(ref(), locator="events.jsonl"), "locator does not follow the byte-range grammar"),
        (dict(ref(), locator="/etc/passwd#bytes=0:1"), "locator escapes the run"),
        (dict(ref(), locator="../events.jsonl#bytes=0:1"), "locator escapes the run"),
        (dict(ref(), locator="events.jsonl#bytes=9:2"), "locator byte range is inverted"),
        (dict(ref(), content_digest="short"), "reference content_digest is not a sha-256 digest"),
        (dict(ref(), run_id="not an identifier"), "invalid journal record"),
        ({"version": 1}, "reference payload keys"),
    ):
        with refuses(rule):
            DurableReference.from_payload(bad)


def _projection_changed_failure(**overrides):
    return {
        **dict(
            round=1,
            reflection_id="r01-x01",
            operation_id="op1",
            error_class="projection_changed",
            note_digest=None,
            reads={"entries": [], "omitted_reads": 0, "omitted_errors": 0},
            reason="projection_changed",
        ),
        **overrides,
    }


def test_projection_changed_is_a_family_14_content_failure():
    from evolution.tree.campaign_schema import ADAPTIVE_REFLECTION_ERRORS, FINAL_REFLECTION_ERRORS

    assert FINAL_REFLECTION_ERRORS == ADAPTIVE_REFLECTION_ERRORS | {
        "projection_changed",
        "input_too_large",
    }
    validate_payload("reflection_failed", _projection_changed_failure(), final=True)
    # A settled failure carries its reason, and only the table's one.
    with refuses("a settled reflection failure carries a reason"):
        failure = _projection_changed_failure()
        del failure["reason"]
        validate_payload("reflection_failed", failure, final=True)
    with refuses("reflection reason outside the family-10 table"):
        validate_payload(
            "reflection_failed", _projection_changed_failure(reason="invalid_result"), final=True
        )


@pytest.mark.parametrize("reason", ["changed_file_budget", "written_byte_budget"])
def test_the_editor_budget_reasons_are_family_14_only(reason):
    payload = {
        "attempt_id": "r01-a01",
        "operation_id": "op-1",
        "error_class": "reported",
        "note_digest": None,
        "reason": reason,
    }
    validate_payload("editor_failed", dict(payload), final=True)


def test_only_family_14_feedback_reads_the_budget_reasons():
    from evolution.tree.roles.role_failures import EDITOR_BUDGET_REASONS

    assert EDITOR_BUDGET_REASONS <= feedback_outcomes(14)["abandoned"]
    assert (
        "changed_file_budget" not in _FINAL_ATTEMPT_OUTCOMES["abandoned"]
    )  # attempt_closed is unchanged


@pytest.mark.parametrize("family", [14, 15])
def test_verbatim_answers_is_an_identity_field_of_families_14_and_15(family):
    """ADR-0038: the optional digest rides beside the connection fields."""
    _validate_identity_connection_fields(
        {"metric_contract": "a" * 64, "role_visibility": "b" * 64, "verbatim_answers": "c" * 64},
        family,
    )


def _reflection_finished(groups):
    return dict(
        round=1,
        reflection_id="r01-x01",
        operation_id="op1",
        status="reflected",
        groups=groups,
        out_of_scope_paths=[],
        probes_used=0,
        note_digest=None,
        reads={"entries": [], "omitted_reads": 0, "omitted_errors": 0},
    )


def _stored_group(**changes):
    return {
        "root_cause": "r",
        "suggested_fix": "f",
        "counterevidence": "c",
        "regression_risks": "g",
        "why_this_layer": "w",
        "evidence_ids": ["obs:c1:1"],
        "affected_cases": ["c1"],
        "target_paths": ["SKILL.md"],
        "probe_refs": [],
        **changes,
    }


def test_every_layer_reads_one_adaptive_limit_source(monkeypatch):
    """#282 (ADR-0047): the tool schema, the backend bounds check, the journal and
    the Editor brief read `campaign_reflector.ADAPTIVE_GROUP_LIMITS`, not their own
    copies."""
    from types import MappingProxyType

    from evolution.tree.roles import limits
    from evolution.tree.roles.limits import adaptive_limits
    from evolution.tree.roles.reflector.chat_reflector import reflect_tools
    from evolution.tree.roles.reflector_contracts import (
        GroupError,
        adaptive_group_violation,
        check_adaptive_group_bounds,
    )

    patched = {
        "groups": 2,
        "sentence_chars": 512,
        "evidence_ids": 3,
        "affected_cases": 2,
        "target_paths": 1,
        "document_ids": 1,
        "groups_bytes": 2048,
    }
    monkeypatch.setattr(limits, "ADAPTIVE_GROUP_LIMITS", MappingProxyType(patched))
    assert adaptive_limits(512) == patched
    assert adaptive_limits(40)["sentence_chars"] == 40
    assert adaptive_limits(None) == {**patched, "sentence_chars": None}

    terminal = next(
        tool["function"]["parameters"]["properties"]["groups"]
        for tool in reflect_tools(probe=False, adaptive=True, docs=True)
        if tool["function"]["name"] == "finish_reflection"
    )
    items = terminal["items"]["properties"]
    assert terminal["maxItems"] == 2
    assert items["root_cause"]["maxLength"] == 512
    assert items["evidence_ids"]["description"] == "At most 3 distinct observation IDs."
    assert (items["affected_cases"]["maxItems"], items["target_paths"]["maxItems"]) == (2, 1)
    assert items["document_ids"]["maxItems"] == 1

    # The journal's group count no longer comes from the legacy `REFLECT_LIMITS_V1`.
    from evolution.tree import campaign_schema

    assert not hasattr(campaign_schema, "_reflect_limits")
    validate_payload("reflection_finished", _reflection_finished([_stored_group()]), final=True)
    for groups, rule in (
        ([_stored_group()] * 3, "groups exceeds the protocol count limit"),
        ([_stored_group(evidence_ids=["e"] * 4)], "evidence_ids exceeds the protocol count limit"),
        (
            [_stored_group(affected_cases=["c"] * 3)],
            "affected_cases exceeds the protocol count limit",
        ),
        (
            [_stored_group(target_paths=["a.md"] * 2)],
            "target_paths exceeds the protocol count limit",
        ),
        ([_stored_group(document_ids=["d"] * 2)], "document_ids exceeds the protocol count limit"),
        (
            [
                _stored_group(root_cause=f"{index}" * 500, why_this_layer="w" * 500)
                for index in range(2)
            ],
            "groups exceeds 2048 bytes",
        ),
    ):
        assert adaptive_group_violation(groups, adaptive_limits(512)) == rule
        with pytest.raises(GroupError, match=f"^invalid_result: {re.escape(rule)}$"):
            check_adaptive_group_bounds(groups)
        with refuses(rule):
            validate_payload("reflection_finished", _reflection_finished(groups), final=True)
    # The text limit is the round's (R3): the lifecycle checks it, the stateless
    # record check does not, so a longer limit's stored groups still decode.
    long = [_stored_group(root_cause="x" * 513)]
    assert (
        adaptive_group_violation(long, adaptive_limits(512)) == "root_cause exceeds 512 characters"
    )
    with pytest.raises(GroupError, match="^invalid_result: root_cause exceeds 512 characters$"):
        check_adaptive_group_bounds(long)
    validate_payload("reflection_finished", _reflection_finished(long), final=True)

    # The Editor brief publishes the same group count (ADR-0047), at every text limit.
    from evolution.tree.roles.editor_contracts import brief_payload
    from tests.tree.roles.editor.test_editor_brief import _built

    for limit in (512, 4096, None):
        published = brief_payload(_built(sentence_chars=limit))["limits"]
        assert published["groups"] == 2
        assert published.get("sentence_chars") == limit


#: The committed two-round family-14 journal (`test_tree_family14_b2_pins.py`),
#: created without `reflector.group_text_chars`, so at 512 (R2).
COMMITTED14 = (
    Path(__file__).resolve().parent / "fixtures" / "family14_b2" / "run" / "campaign.jsonl"
)
#: Sequences in `COMMITTED14`: each round's `baseline_bound` and `reflection_finished`.
OPENS = {1: 2, 2: 49}
FINISHED = {1: 13, 2: 55}
#: Payload keys that cite an earlier record by its sequence (the comparison,
#: calibration and superseded bindings), besides the lists in `probe_refs`.
_SEQ_KEYS = ("comparison", "binding", "supersedes")


def _limit_rebase(current, limit):
    """An `identity_rebased` moving only `reflection.group_text_chars` off `current`
    (the identity in force), as `evo tree authorize` accepts it (#282)."""
    from evolution.tree.campaign_identity import AdaptationIdentityV1
    from evolution.tree.execution.authorizations import (
        Authorization,
        identity_differences,
        identity_instance_digest,
        identity_record,
    )

    before = copy.deepcopy(current)
    after = copy.deepcopy(current)
    after["adaptation_payload"]["reflection"]["group_text_chars"] = limit
    after["adaptation"]["digest"] = AdaptationIdentityV1.from_payload(
        after["adaptation_payload"]
    ).digest
    instance = identity_instance_digest(identity_record(before), identity_record(after))
    decision = Authorization.new(
        "run_identity_changed", instance, "the reflector limit moved", now="2026-10-21T00:00:00Z"
    )
    rebased = {
        **after,
        "previous_digest": before["identity_digest"],
        "instance_digest": instance,
        "changed_fields": sorted(
            d.field for d in identity_differences(identity_record(before), identity_record(after))
        ),
        "authorization": decision.journal_payload(),
    }
    current.clear()
    current.update(after)
    return rebased


def _journal14(*, rebases, texts):
    """`COMMITTED14` with a limit rebase before each `rebases` sequence ({seq: limit})
    and each round's stored `root_cause` set to `texts[round]` characters.

    Rows are renumbered, and what cites a moved row follows it: every
    `journal_record` reference (re-addressed as `family15_fakes.Family15Life`
    does), the `seq` fields in `_SEQ_KEYS` and each frozen input's digest.
    """
    import hashlib

    from evolution.tree.execution.journal_identity import canonical_json

    rows = [json.loads(line) for line in COMMITTED14.read_text().splitlines()]
    created = rows[0]["payload"]
    current = {
        key: copy.deepcopy(created[key])
        for key in (
            "identity",
            "identity_digest",
            "producing_version",
            "adaptation",
            "adaptation_payload",
        )
    }
    out = []
    sources = []
    for row in rows:
        if row["sequence"] in rebases:
            out.append(
                {
                    **{k: row[k] for k in ("schema_version", "run_id", "timestamp")},
                    "sequence": 0,
                    "event": "identity_rebased",
                    "operation_id": None,
                    "payload": _limit_rebase(current, rebases[row["sequence"]]),
                }
            )
            sources.append(None)
        sources.append(hashlib.sha256(canonical_json(row) + b"\n").hexdigest())
        row = copy.deepcopy(row)
        if row["event"] == "reflection_finished" and row["payload"]["round"] in texts:
            group = row["payload"]["groups"][0]
            group["root_cause"] = "x" * texts[row["payload"]["round"]]
        out.append(row)
    moved = {}
    renamed = {}
    # `COMMITTED14` has no rebase of its own: every other row is an original.
    numbers = {
        row["sequence"]: sequence
        for sequence, row in enumerate(out, 1)
        if row["event"] != "identity_rebased"
    }

    def readdress(value):
        if isinstance(value, str):
            return renamed.get(value, value)
        if isinstance(value, dict):
            if value.get("kind") == "journal_record" and value.get("content_digest") in moved:
                return moved[value["content_digest"]]
            return {
                key: (
                    numbers[child]
                    if key in _SEQ_KEYS and type(child) is int
                    else [numbers[n] for n in child]
                    if key == "probe_refs"
                    else readdress(child)
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [readdress(child) for child in value]
        return value

    offset = 0
    for sequence, (row, source) in enumerate(zip(out, sources, strict=True), 1):
        row["sequence"] = sequence
        row["payload"] = readdress(row["payload"])
        if row["event"] == "attempt_input_frozen":
            # The frozen input binds its source references (`_attempt_input_frozen`).
            frozen = {
                key: row["payload"][key]
                for key in ("common_input_digest", "feedback", "feedback_mode", "source_refs")
            }
            digest = hashlib.sha256(canonical_json(frozen)).hexdigest()
            renamed[row["payload"]["input_digest"]] = digest
            row["payload"]["input_digest"] = digest
        encoded = Envelope.from_payload(row, family=campaign_family_for(14)).encode()
        if source is not None:
            moved[source] = dict(
                version=1,
                kind="journal_record",
                run_id=row["run_id"],
                locator=f"campaign.jsonl#bytes={offset}:{offset + len(encoded)}",
                content_digest=hashlib.sha256(encoded).hexdigest(),
            )
        offset += len(encoded)
    return out


def _envelopes(rows):
    family = campaign_family_for(14)
    return [Envelope.from_payload(row, family=family) for row in rows]


def test_a_round_keeps_the_limit_in_force_when_it_opened():
    """C6/R3: round 1 opened at 512; the 4,096 rebase mid-round applies from round 2."""

    rows = _journal14(rebases={FINISHED[1] - 1: 4096}, texts={1: 900, 2: 900})
    state = CampaignLifecycle(selector=None, adaptive=True, family_version=14)
    envelopes = _envelopes(rows)
    finished = [i for i, row in enumerate(rows) if row["event"] == "reflection_finished"]
    for envelope in envelopes[: finished[0]]:
        state.accept(envelope)
    assert state.snapshot.rounds[1].group_text_chars == 512
    assert state.snapshot.round_group_text_chars(1) == 512
    with pytest.raises(SchemaError, match="^root_cause exceeds 512 characters$"):
        state.accept(envelopes[finished[0]])
    # The same round's reflection at 512 characters is admitted, and the round
    # the rebase precedes runs at 4,096: its 900 characters are stored.
    short = _journal14(rebases={FINISHED[1] - 1: 4096}, texts={1: 512, 2: 900})
    snapshot = reduce(_envelopes(short), selector=None, adaptive=True, family_version=14)
    assert snapshot.round_group_text_chars(1) == 512
    assert snapshot.round_group_text_chars(2) == 4096
    assert len(snapshot.rounds[2].reflections[0]["groups"][0]["root_cause"]) == 900


def test_lowering_the_limit_never_invalidates_stored_groups():
    """900-character groups stored under 4,096 replay after a later rebase to 512."""

    rows = _journal14(rebases={OPENS[1]: 4096, FINISHED[1] + 1: 512}, texts={1: 900, 2: 512})
    snapshot = reduce(_envelopes(rows), selector=None, adaptive=True, family_version=14)
    assert snapshot.finished is not None
    assert (snapshot.round_group_text_chars(1), snapshot.round_group_text_chars(2)) == (4096, 512)
    assert len(snapshot.rounds[1].reflections[0]["groups"][0]["root_cause"]) == 900
    assert snapshot.max_consecutive_failures == 2
    # The committed journal itself, without the key, keeps 512 on every round.
    committed = _envelopes(_journal14(rebases={}, texts={}))
    plain = reduce(committed, selector=None, adaptive=True, family_version=14)
    assert (plain.round_group_text_chars(1), plain.round_group_text_chars(2)) == (512, 512)


def test_a_null_limit_has_no_per_field_cap():
    rows = _journal14(rebases={OPENS[1]: None}, texts={1: 20_000})
    snapshot = reduce(_envelopes(rows), selector=None, adaptive=True, family_version=14)
    assert snapshot.round_group_text_chars(1) is None
    assert len(snapshot.rounds[1].reflections[0]["groups"][0]["root_cause"]) == 20_000
    # The whole-groups byte cap still holds under `null`.
    with pytest.raises(SchemaError, match="^invalid journal record$"):
        _journal14(rebases={OPENS[1]: None}, texts={1: 70_000})


def test_secret_oracle_paths_is_a_family_15_identity_field_only():
    fields = {
        "metric_contract": "a" * 64,
        "role_visibility": "b" * 64,
        "secret_oracle_paths": "c" * 64,
    }
    _validate_identity_connection_fields(fields, 15)
    with pytest.raises(SchemaError):
        _validate_identity_connection_fields(fields, 14)
