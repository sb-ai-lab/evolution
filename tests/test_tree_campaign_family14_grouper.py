"""Family 14 H8: the Grouper's registries, journal grammar and lifecycle (ADR-0034).

The grammar tests (F0 R45, R46) are validator-level: their payloads are
synthetic schema inputs, not records an engine wrote. The lifecycle tests (F0
R45, R46, R50) replay genuine engine prefixes and accept synthetic terminals
after them (see the lifecycle section). The genuine runs live in
`test_tree_campaign_family14_grouper_run.py`.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

import pytest

from evolution.tree import campaign_schema as schema
from evolution.tree.campaign_schema import SchemaError, campaign_family_for
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.manifest.manifest_v2 import GROUPER_KINDS
from evolution.tree.roles.role_failures import GROUPER_REASONS
from tests.model_grouping_fakes import (
    ScriptedClient,
    chat_grouper_section,
    group_everything,
    use_chat_grouper,
)

SHA = "a" * 64
READS = {"entries": [], "omitted_reads": 0, "omitted_errors": 0}
CHAT_CALLS = {"map_requests": 1, "reduce": "none", "attempts": 1, "repairs": 0}
AGENT_CALLS = {"agent_calls": 1, "repairs": 0, "turns": 3}


def _started(**extra: Any) -> dict[str, Any]:
    return {
        "round": 1,
        "grouping_id": "r01-g-x",
        "baseline_binding": SHA,
        "policy_digest": SHA,
        "registry_digest": SHA,
        **extra,
    }


def _failed(code: str = "malformed_result", **extra: Any) -> dict[str, Any]:
    return {"round": 1, "grouping_id": "r01-g-x", "code": code, "reads": READS, **extra}


def _finished(fallback: object = None, calls: object = None) -> dict[str, Any]:
    result = {
        "version": "reflect_grouping_result/1",
        "registry_digest": SHA,
        "assignments": [0, None],
        "descriptions": {},
        "variants": {},
    }
    return {
        "round": 1,
        "grouping_id": "r01-g-x",
        "result": result,
        "result_digest": hashlib.sha256(canonical_json(result)).hexdigest(),
        "reads": READS,
        "fallback": fallback,
        "calls": calls,
    }


def _fallback(reason: str, scope: str = "all", kind: str = "fields") -> dict[str, str]:
    return {"kind": kind, "scope": scope, "reason": reason}


def _suspended(reason: str, owner: str = "grouper", kind: str = "stage_record") -> dict[str, Any]:
    return {
        "reason": reason,
        "owner": owner,
        "evidence": {"evidence_kind": kind, "seq": 7, "operation_id": "op-1"},
    }


def _check(family: int, event: str, payload: dict[str, Any]) -> None:
    campaign_family_for(family).validate_payload(event, copy.deepcopy(payload))


def _refused(family: int, event: str, payload: dict[str, Any], message: str) -> None:
    with pytest.raises(SchemaError) as caught:
        _check(family, event, payload)
    assert str(caught.value) == message


# -- registries --------------------------------------------------------------


def test_family14_registries_sit_beside_the_shared_ones():
    """Plan decision 5: the shared registries that validate family 13 are not widened."""

    assert schema.GROUPING_FAILURE_CODES14 == schema.GROUPING_FAILURE_CODES | {"role_failed"}
    assert "role_failed" not in schema.GROUPING_FAILURE_CODES
    assert schema.GROUPING_FALLBACK_REASONS14 == frozenset(
        {"invalid_output", "input_too_large", "turns_exhausted"}
    )
    assert "turns_exhausted" not in schema.GROUPING_FALLBACK_REASONS
    assert schema.GROUPING_AGENT_CALL_KEYS == frozenset({"agent_calls", "repairs", "turns"})
    assert schema.GROUPER_KINDS14 == frozenset({"fields", "command", "chat", "agent_cli"})
    assert schema.GROUPER_KINDS14 == frozenset(GROUPER_KINDS)


def test_grouper_suspensions_are_registered_owner_grouper_stage_record():
    assert dict(schema.GROUPER_SUSPENSIONS) == {
        "grouper_provider_failed": ("grouper", "stage_record"),
        "grouper_failing": ("grouper", "stage_record"),
    }
    for reason, row in schema.GROUPER_SUSPENSIONS.items():
        assert schema.SUSPENSION_REASONS[reason] == row


# -- grouping_started --------------------------------------------------------


@pytest.mark.parametrize("kind", ["fields", "command", "chat", "agent_cli"])
def test_family14_grouping_started_records_the_grouper_kind(kind):
    _check(14, "grouping_started", _started(grouper_kind=kind))


def test_family14_grouping_started_requires_the_grouper_kind():
    _refused(14, "grouping_started", _started(), "invalid journal record")


@pytest.mark.parametrize("kind", ["model", "agent", "", None, 3, ["chat"]])
def test_family14_grouping_started_refuses_a_kind_outside_the_four(kind):
    _refused(
        14, "grouping_started", _started(grouper_kind=kind), "grouper kind is not in the registry"
    )


# -- grouping_failed ---------------------------------------------------------


@pytest.mark.parametrize(("reason", "error_class"), sorted(GROUPER_REASONS.items()))
def test_family14_grouping_failed_admits_role_failed_with_its_class(reason, error_class):
    _check(14, "grouping_failed", _failed("role_failed", reason=reason, error_class=error_class))


@pytest.mark.parametrize("code", sorted(schema.GROUPING_FAILURE_CODES))
def test_family14_other_codes_keep_their_shape(code):
    _check(14, "grouping_failed", _failed(code))


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"reason": "provider_exhausted"},
        {"error_class": "provider"},
    ],
)
def test_family14_role_failed_requires_both_keys(extra):
    _refused(
        14,
        "grouping_failed",
        _failed("role_failed", **extra),
        "reason and error_class are present exactly on a role_failed grouping",
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"reason": "provider_exhausted", "error_class": "provider"},
        {"reason": "provider_exhausted"},
        {"error_class": "provider"},
    ],
)
def test_family14_refuses_reason_or_class_on_another_code(extra):
    _refused(
        14,
        "grouping_failed",
        _failed("malformed_result", **extra),
        "reason and error_class are present exactly on a role_failed grouping",
    )


@pytest.mark.parametrize(
    "reason", ["provider_unavailable", "deadline_exhausted", "unclassified", "", None, ["timeout"]]
)
def test_family14_role_failed_refuses_an_unknown_reason(reason):
    _refused(
        14,
        "grouping_failed",
        _failed("role_failed", reason=reason, error_class="provider"),
        "grouper reason is not in the registry",
    )


@pytest.mark.parametrize("error_class", ["exception", "lifecycle", "", None, ["provider"]])
def test_family14_role_failed_refuses_an_unknown_class(error_class):
    _refused(
        14,
        "grouping_failed",
        _failed("role_failed", reason="timeout", error_class=error_class),
        "grouper error_class is not a failure class",
    )


@pytest.mark.parametrize(
    ("reason", "error_class"),
    [
        ("provider_exhausted", "unknown"),
        ("provider_rejected", "content"),
        ("invalid_output", "provider"),
        ("turns_exhausted", "unknown"),
        ("timeout", "provider"),
        ("exception", "content"),
    ],
)
def test_family14_role_failed_refuses_a_class_that_is_not_the_reasons(reason, error_class):
    _refused(
        14,
        "grouping_failed",
        _failed("role_failed", reason=reason, error_class=error_class),
        "grouper error_class is not the reason's class",
    )


# -- grouping_finished -------------------------------------------------------


@pytest.mark.parametrize("reason", ["invalid_output", "input_too_large", "turns_exhausted"])
@pytest.mark.parametrize("scope", ["all", "uncovered"])
def test_family14_fallback_reasons_are_the_narrowed_set(reason, scope):
    _check(14, "grouping_finished", _finished(_fallback(reason, scope), CHAT_CALLS))


@pytest.mark.parametrize(
    "reason", ["provider_unavailable", "provider_rejected", "deadline_exhausted"]
)
def test_family14_refuses_the_provider_fallback_reasons(reason):
    _refused(
        14,
        "grouping_finished",
        _finished(_fallback(reason), CHAT_CALLS),
        "grouping fallback reason",
    )


@pytest.mark.parametrize("calls", [None, CHAT_CALLS, AGENT_CALLS])
def test_family14_admits_both_closed_calls_shapes(calls):
    _check(14, "grouping_finished", _finished(None, calls))


@pytest.mark.parametrize(
    "calls",
    [
        {"agent_calls": 1, "repairs": 0, "turns": 3, "attempts": 1},
        {"agent_calls": 1, "repairs": 0},
        {"agent_calls": 1, "repairs": 0, "turns": 3, "map_requests": 1},
        {"map_requests": 1, "reduce": "none", "attempts": 1, "repairs": 0, "turns": 3},
        {"map_requests": 1, "reduce": "none", "attempts": 1, "turns": 3},
        {"map_requests": 1, "reduce": "none", "attempts": 1, "repairs": 0, "extra": 0},
    ],
)
def test_family14_refuses_a_mixed_or_extra_calls_key(calls):
    _refused(14, "grouping_finished", _finished(None, calls), "invalid journal record")


@pytest.mark.parametrize("name", ["agent_calls", "repairs", "turns"])
@pytest.mark.parametrize("value", [-1, 1.0, "1", None, True])
def test_family14_agent_calls_are_counts(name, value):
    _refused(
        14,
        "grouping_finished",
        _finished(None, {**AGENT_CALLS, name: value}),
        f"grouping calls {name} is not a count",
    )


# -- campaign_suspended ------------------------------------------------------


@pytest.mark.parametrize("reason", ["grouper_provider_failed", "grouper_failing"])
def test_family14_admits_the_grouper_suspensions(reason):
    _check(14, "campaign_suspended", _suspended(reason))


@pytest.mark.parametrize("reason", ["grouper_provider_failed", "grouper_failing"])
@pytest.mark.parametrize("owner", ["campaign", "reflector", "editor", "target"])
def test_family14_grouper_suspension_refuses_another_owner(reason, owner):
    _refused(
        14,
        "campaign_suspended",
        _suspended(reason, owner=owner),
        "reason and owner are not a stage_record suspension",
    )


@pytest.mark.parametrize("reason", ["grouper_provider_failed", "grouper_failing"])
@pytest.mark.parametrize("kind", ["threshold", "fault_rows"])
def test_family14_grouper_suspension_refuses_another_evidence_kind(reason, kind):
    _refused(
        14,
        "campaign_suspended",
        _suspended(reason, kind=kind),
        f"reason and owner are not a {kind} suspension",
    )


def test_family14_grouping_failed_suspension_keeps_its_owner():
    _check(14, "campaign_suspended", _suspended("grouping_failed", owner="campaign"))


# ============================================================================
# Lifecycle (Task 3): kind, outcome shapes, `role_failed`, the chain and the
# suspension cause (ADR-0034, F0 R45, R46, R50; plan decisions 2, 3, 6, 7).
#
# Every lifecycle below replays a *genuine* engine prefix cut at the hook
# `after_grouping_started` (`_cut_hook`), so the pending `grouping_started`
# is the engine's own record. The terminals and suspensions accepted after it
# are synthetic lifecycle inputs built from that prefix -- never evidence of
# what the engine writes (that is Tasks 5-7, in the `_run` module).
#
# `agent_cli` has no runtime before H9 (`agent_cli_grouper_unavailable`), so
# its lifecycle swaps a profile-`/7` `agent_cli` slot into a genuine `fields`
# prefix's creation: validator/lifecycle only, genuine in H9. The family-13
# `model` lifecycle swaps the `chat` prefix's slot back to its `/14` name the
# same way, since no non-retired family-13 `model` run exists.
# ============================================================================

_LIFE_PREFIXES: dict[str, list[dict[str, Any]]] = {}
#: A `chat` map prompt longer than `max_input_chars`' floor (8000): every
#: grouping falls back `("all", "input_too_large")` before any provider call.
_OVERSIZED_PROMPT = "Group the failing observations.\n" + "x" * 9000 + "\n"
COMMAND_SECTION = {
    "kind": "command",
    "argv": ["python3", "group.py"],
    "cwd": "grouping",
    "dependency_paths": [],
    "allowed_env": [],
    "timeout_seconds": 30,
    "config": {},
}


async def _cut_prefix(tmp_path, monkeypatch, name: str) -> list[dict[str, Any]]:
    """The campaign records of genuine run `name`, cut at a `grouping_started`.

    Cached per worker: each prefix is a pure function of its configuration.
    """
    from tests.campaign_drivers import (
        OBSERVE,
        STATISTICAL,
        WORSE_SCORER,
        _cut_hook,
        _records,
        _run_kwargs,
    )
    from tests.campaign_drivers import two_case_run as _two_case_run
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run

    if name not in _LIFE_PREFIXES:
        root = tmp_path / name
        root.mkdir()
        with monkeypatch.context() as patch:
            settings = None
            if name == "chat":
                use_chat_grouper(patch, ScriptedClient([group_everything()]))
                settings = {"grouper": chat_grouper_section()}
            elif name == "chat_chain":
                use_chat_grouper(patch, ScriptedClient([]))
                (root / "grouper-prompt.md").write_text(_OVERSIZED_PROMPT)
                settings = {
                    "grouper": chat_grouper_section(
                        system_prompt="grouper-prompt.md", max_input_chars=8000
                    ),
                    "refresh": 1,
                }
            elif name == "command":
                (root / "grouping").mkdir()
                (root / "grouping" / "group.py").write_text("print('{}')\n")
                settings = {"grouper": COMMAND_SECTION}
            _cut_hook(patch, "after_grouping_started", call=2 if name == "chat_chain" else 1)
            with pytest.raises(Crash):
                if name == "family13":
                    as_family_13, rest = _run_kwargs(OBSERVE)
                    await _two_case_run(root, as_family_13=as_family_13, terminal=None, **rest)
                elif name == "chat_chain":
                    await family14_run(
                        root,
                        patch,
                        {**STATISTICAL, "scorer": WORSE_SCORER},
                        family14_settings=settings,
                        campaign={"rounds": 3},
                    )
                else:
                    await family14_run(root, patch, family14_settings=settings)
        records = _records(root / "run" / "campaign.jsonl")
        assert records[-1]["event"] == "grouping_started"
        _LIFE_PREFIXES[name] = records
    return copy.deepcopy(_LIFE_PREFIXES[name])


def _swap_grouping(records: list[dict[str, Any]], slot: dict[str, Any]) -> list[dict[str, Any]]:
    """`records` with the creation's adaptation grouping slot replaced by `slot`.

    Synthetic identity: the slot's digest and the adaptation slot are renewed,
    and a family-14 `grouping_started` names the new kind.
    """
    from evolution.tree.campaign_identity import AdaptationIdentityV1, adaptation_identity_slot

    rows = copy.deepcopy(records)
    created = rows[0]["payload"]
    adaptation = dict(created["adaptation_payload"])
    adaptation["grouping"] = copy.deepcopy(slot)
    full = AdaptationIdentityV1(adaptation)
    created["adaptation"] = adaptation_identity_slot(full)
    created["adaptation_payload"] = full.to_payload()
    for row in rows:
        if row["event"] == "grouping_started" and "grouper_kind" in row["payload"]:
            row["payload"]["grouper_kind"] = slot["kind"]
    return rows


def _agent_slot(fallback: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": "agent_cli",
        "protocol": "agent_grouping_request/1",
        "model": "codex",
        "permission": "safe",
        "allowed_env": [],
        "reasoning_effort": None,
        "max_turns": None,
        "max_payload_bytes": 1_000_000,
        "timeout_seconds": 600,
        "prompt_sha256": SHA,
        "program_sha256": SHA,
        "fallback": fallback,
    }


async def _family14_life(tmp_path, monkeypatch, name: str):
    from tests.campaign_fakes import Family14Life

    if name == "agent_cli":
        fields = await _cut_prefix(tmp_path, monkeypatch, "fields")
        declared = copy.deepcopy(fields[0]["payload"]["adaptation_payload"]["grouping"])
        return Family14Life(_swap_grouping(fields, _agent_slot(declared)))
    return Family14Life(await _cut_prefix(tmp_path, monkeypatch, name))


# -- lifecycle helpers --------------------------------------------------------


CHAT_ROLE_FAILED = (
    "provider_exhausted",
    "provider_rejected",
    "invalid_output",
    "input_too_large",
    "exception",
)
AGENT_ROLE_FAILED = ("invalid_output", "turns_exhausted", "agent_error", "timeout", "exception")


def _start(life) -> Any:
    """The latest `grouping_started` envelope (the genuine pending one, unless replaced)."""
    return next(e for e in reversed(life.envelopes) if e.event == "grouping_started")


def _finish(life, fallback: object = None, calls: object = None) -> int:
    started = _start(life).payload
    result = {
        "version": "reflect_grouping_result/1",
        "registry_digest": started["registry_digest"],
        "assignments": [None],
        "descriptions": {},
        "variants": {},
    }
    return life.accept(
        "grouping_finished",
        op=_start(life).operation_id,
        round=started["round"],
        grouping_id=started["grouping_id"],
        result=result,
        result_digest=hashlib.sha256(canonical_json(result)).hexdigest(),
        reads=READS,
        fallback=fallback,
        calls=calls,
    )


def _fail(life, code: str = "malformed_result", **extra: Any) -> int:
    started = _start(life).payload
    return life.accept(
        "grouping_failed",
        op=_start(life).operation_id,
        round=started["round"],
        grouping_id=started["grouping_id"],
        code=code,
        reads=READS,
        **extra,
    )


def _role_failed(life, reason: str) -> int:
    return _fail(life, "role_failed", reason=reason, error_class=GROUPER_REASONS[reason])


def _suspend(life, reason: str, seq: int, owner: str = "grouper") -> int:
    return life.accept(
        "campaign_suspended",
        reason=reason,
        owner=owner,
        evidence={
            "evidence_kind": "stage_record",
            "seq": seq,
            "operation_id": life.envelopes[seq - 1].operation_id,
        },
    )


def _restart(life) -> int:
    """A new `grouping_started` on the same baseline, under a new id."""
    started = _start(life).payload
    return life.accept(
        "grouping_started", **{**started, "grouping_id": f"r{started['round']:02d}-g-resumed"}
    )


def _lifecycle_refused(message: str):
    return pytest.raises(SchemaError, match=f"^{re.escape(message)}$")


def _chain(life) -> int:
    return life.snapshot.grouper_chain


def _grouping(life) -> Any:
    return life.snapshot.rounds[_start(life).payload["round"]].grouping


# -- grouping_started: the identity's kind --------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("name", ["chat", "fields", "command", "agent_cli"])
async def test_grouping_started_names_the_identitys_grouper_kind(tmp_path, monkeypatch, name):
    from tests.campaign_fakes import Family14Life

    genuine = await _family14_life(tmp_path, monkeypatch, name)
    records = [envelope.to_payload() for envelope in genuine.envelopes]
    started = records[-1]
    assert started["payload"]["grouper_kind"] == name
    for other in sorted(schema.GROUPER_KINDS14 - {name}):
        life = Family14Life(records[:-1])
        with _lifecycle_refused("grouper_kind is not the identity's grouping kind"):
            life.accept(
                "grouping_started",
                op=started["operation_id"],
                **{**started["payload"], "grouper_kind": other},
            )
    life = Family14Life(records[:-1])
    life.accept("grouping_started", op=started["operation_id"], **started["payload"])
    assert _grouping(life)["status"] == "started"


# -- grouping_finished: per-kind shapes (decision 3) ---------------------------


@pytest.mark.slow
async def test_a_chat_grouping_finishes_with_its_calls(tmp_path, monkeypatch):
    life = await _family14_life(tmp_path, monkeypatch, "chat")
    _finish(life, None, CHAT_CALLS)
    grouping = _grouping(life)
    assert (grouping["status"], grouping["fallback"], grouping["calls"]) == (
        "finished",
        None,
        CHAT_CALLS,
    )


@pytest.mark.slow
async def test_an_agent_cli_grouping_finishes_with_its_calls(tmp_path, monkeypatch):
    life = await _family14_life(tmp_path, monkeypatch, "agent_cli")
    _finish(life, None, AGENT_CALLS)
    assert _grouping(life)["calls"] == AGENT_CALLS


@pytest.mark.slow
@pytest.mark.parametrize(
    ("name", "calls", "message"),
    [
        ("chat", None, "a chat or agent_cli Grouper journals its calls"),
        ("chat", AGENT_CALLS, "the calls are not the Grouper kind's shape"),
        ("agent_cli", None, "a chat or agent_cli Grouper journals its calls"),
        ("agent_cli", CHAT_CALLS, "the calls are not the Grouper kind's shape"),
    ],
)
async def test_a_model_grouper_journals_its_own_calls_shape(
    tmp_path, monkeypatch, name, calls, message
):
    life = await _family14_life(tmp_path, monkeypatch, name)
    with _lifecycle_refused(message):
        _finish(life, None, calls)


@pytest.mark.slow
@pytest.mark.parametrize("name", ["fields", "command"])
async def test_fields_and_command_never_journal_calls_or_fall_back(tmp_path, monkeypatch, name):
    life = await _family14_life(tmp_path, monkeypatch, name)
    for calls in (CHAT_CALLS, AGENT_CALLS):
        with _lifecycle_refused("only a chat or agent_cli Grouper journals calls"):
            _finish(life, None, calls)
    for scope in ("all", "uncovered"):
        for kind in ("fields", "singletons"):
            with _lifecycle_refused("only a chat or agent_cli Grouper falls back"):
                _finish(life, _fallback("invalid_output", scope, kind), None)
    _finish(life, None, None)
    grouping = _grouping(life)
    assert (grouping["status"], grouping["fallback"], grouping["calls"]) == (
        "finished",
        None,
        None,
    )
    assert _chain(life) == 0


#: Each kind's fallback reasons and the scopes each admits (decision 3). A
#: `chat` slot without a declared fallback falls back to singletons; the
#: synthetic `agent_cli` slot declares the `fields` fallback.
_FALLBACKS = {
    "chat": ("singletons", {"invalid_output": ("all", "uncovered"), "input_too_large": ("all",)}),
    "agent_cli": ("fields", {"invalid_output": ("all", "uncovered"), "turns_exhausted": ("all",)}),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", ["chat", "agent_cli"])
@pytest.mark.parametrize("reason", sorted(schema.GROUPING_FALLBACK_REASONS14))
@pytest.mark.parametrize("scope", ["all", "uncovered"])
async def test_a_model_grouper_falls_back_with_its_kinds_reasons(
    tmp_path, monkeypatch, name, reason, scope
):
    life = await _family14_life(tmp_path, monkeypatch, name)
    calls = CHAT_CALLS if name == "chat" else AGENT_CALLS
    declared, reasons = _FALLBACKS[name]
    fallback = _fallback(reason, scope, declared)
    if reason not in reasons:
        with _lifecycle_refused("the fallback reason is not the Grouper kind's"):
            _finish(life, fallback, calls)
    elif scope not in reasons[reason]:
        with _lifecycle_refused("an input_too_large or turns_exhausted fallback has scope all"):
            _finish(life, fallback, calls)
    else:
        _finish(life, fallback, calls)
        assert _grouping(life)["fallback"] == fallback
        assert _chain(life) == (1 if scope == "all" else 0)


@pytest.mark.slow
@pytest.mark.parametrize("name", ["chat", "agent_cli"])
async def test_a_model_grouper_falls_back_to_the_declared_kind(tmp_path, monkeypatch, name):
    life = await _family14_life(tmp_path, monkeypatch, name)
    calls = CHAT_CALLS if name == "chat" else AGENT_CALLS
    declared, _ = _FALLBACKS[name]
    other = "fields" if declared == "singletons" else "singletons"
    with _lifecycle_refused("the fallback is not the declared one"):
        _finish(life, _fallback("invalid_output", "uncovered", other), calls)
    _finish(life, _fallback("invalid_output", "uncovered", declared), calls)


# -- role_failed per kind (decisions 2, 3) --------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("name", ["fields", "command"])
@pytest.mark.parametrize("reason", sorted(GROUPER_REASONS))
async def test_fields_and_command_never_fail_with_role_failed(tmp_path, monkeypatch, name, reason):
    life = await _family14_life(tmp_path, monkeypatch, name)
    with _lifecycle_refused("only a chat or agent_cli Grouper fails with role_failed"):
        _role_failed(life, reason)
    # Their family-13 codes keep their route.
    seq = _fail(life, "malformed_result")
    assert life.state._suspension_causes[seq] == ("grouping_failed", _start(life).operation_id)


@pytest.mark.slow
@pytest.mark.parametrize("name", ["chat", "agent_cli"])
@pytest.mark.parametrize("reason", sorted(GROUPER_REASONS))
async def test_role_failed_reasons_are_the_kinds(tmp_path, monkeypatch, name, reason):
    """At chain 0: a provider or unknown reason of the kind is admitted, a
    content one is not yet the second full fallback, another kind's is refused."""

    life = await _family14_life(tmp_path, monkeypatch, name)
    allowed = CHAT_ROLE_FAILED if name == "chat" else AGENT_ROLE_FAILED
    if reason not in allowed:
        with _lifecycle_refused("the role_failed reason is not the Grouper kind's"):
            _role_failed(life, reason)
    elif GROUPER_REASONS[reason] == "content":
        with _lifecycle_refused("a content role_failed is only the second full fallback"):
            _role_failed(life, reason)
    else:
        _role_failed(life, reason)
        grouping = _grouping(life)
        assert (grouping["status"], grouping["code"]) == ("failed", "role_failed")
        assert (grouping["reason"], grouping["error_class"]) == (reason, GROUPER_REASONS[reason])
        assert _chain(life) == 0


def test_the_kind_reason_sets_are_the_reason_tables_rows():
    assert set(CHAT_ROLE_FAILED) | set(AGENT_ROLE_FAILED) == set(GROUPER_REASONS)
    assert schema.GROUPER_ROLE_FAILED_REASONS14 == {
        "chat": frozenset(CHAT_ROLE_FAILED),
        "agent_cli": frozenset(AGENT_ROLE_FAILED),
    }
    assert schema.GROUPER_FALLBACK_REASONS_BY_KIND14 == {
        "chat": frozenset({"invalid_output", "input_too_large"}),
        "agent_cli": frozenset({"invalid_output", "turns_exhausted"}),
    }


# -- the fallback chain (decisions 6, 7) ----------------------------------------


@pytest.mark.slow
async def test_the_chain_prefix_is_one_genuine_full_fallback(tmp_path, monkeypatch):
    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    finished = [e.payload for e in life.envelopes if e.event == "grouping_finished"]
    assert [p["fallback"] for p in finished] == [
        {"kind": "singletons", "scope": "all", "reason": "input_too_large"}
    ]
    assert _start(life).payload["round"] == 2
    assert _chain(life) == 1


@pytest.mark.slow
@pytest.mark.parametrize("reason", ["invalid_output", "input_too_large"])
async def test_a_second_full_fallback_is_refused(tmp_path, monkeypatch, reason):
    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    with _lifecycle_refused("the second full fallback is written as role_failed"):
        _finish(life, _fallback(reason, "all", "singletons"), CHAT_CALLS)


@pytest.mark.slow
@pytest.mark.parametrize("reason", ["invalid_output", "input_too_large"])
async def test_the_second_full_fallback_is_a_content_role_failed(tmp_path, monkeypatch, reason):
    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    seq = _role_failed(life, reason)
    assert _chain(life) == 1  # `grouping_failed` leaves the chain
    op = _start(life).operation_id
    assert life.state._suspension_causes[seq] == ("grouper_failing", op)
    _suspend(life, "grouper_failing", seq)
    assert _chain(life) == 0


@pytest.mark.slow
async def test_an_uncovered_fallback_leaves_the_chain(tmp_path, monkeypatch):
    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    _finish(life, _fallback("invalid_output", "uncovered", "singletons"), CHAT_CALLS)
    assert _chain(life) == 1


@pytest.mark.slow
async def test_a_fallback_free_grouping_breaks_the_chain(tmp_path, monkeypatch):
    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    _finish(life, None, CHAT_CALLS)
    assert _chain(life) == 0


@pytest.mark.slow
@pytest.mark.parametrize(
    ("code", "reason", "suspension", "owner"),
    [
        ("role_failed", "provider_exhausted", "grouper_provider_failed", "grouper"),
        ("role_failed", "exception", "grouper_failing", "grouper"),
        ("malformed_result", None, "grouping_failed", "campaign"),
    ],
)
async def test_any_suspension_breaks_the_chain(
    tmp_path, monkeypatch, code, reason, suspension, owner
):
    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    seq = _role_failed(life, reason) if reason is not None else _fail(life, code)
    assert _chain(life) == 1
    _suspend(life, suspension, seq, owner=owner)
    assert _chain(life) == 0


@pytest.mark.slow
async def test_a_content_role_failed_needs_the_chain_at_one(tmp_path, monkeypatch):
    """A suspension breaks the chain, so the regrouping's content failure is
    a fallback again, not the second link."""

    life = await _family14_life(tmp_path, monkeypatch, "chat_chain")
    _suspend(life, "grouping_failed", _fail(life), owner="campaign")
    _restart(life)
    with _lifecycle_refused("a content role_failed is only the second full fallback"):
        _role_failed(life, "invalid_output")
    _finish(life, _fallback("invalid_output", "all", "singletons"), CHAT_CALLS)
    assert _chain(life) == 1


# -- the suspension cause (overlap table: `_suspension_cause`, `apply`) ---------


@pytest.mark.slow
@pytest.mark.parametrize(
    ("reason", "suspension"),
    [
        ("provider_exhausted", "grouper_provider_failed"),
        ("provider_rejected", "grouper_provider_failed"),
        ("exception", "grouper_failing"),
    ],
)
async def test_a_role_failed_registers_its_grouper_suspension(
    tmp_path, monkeypatch, reason, suspension
):
    life = await _family14_life(tmp_path, monkeypatch, "chat")
    seq = _role_failed(life, reason)
    op = _start(life).operation_id
    assert life.state._suspension_causes[seq] == (suspension, op)
    other = (
        "grouper_failing" if suspension == "grouper_provider_failed" else "grouper_provider_failed"
    )
    for wrong, owner in (("grouping_failed", "campaign"), (other, "grouper")):
        with _lifecycle_refused("stage_record names a record of another kind"):
            _suspend(life, wrong, seq, owner=owner)
    # Until a suspension clears it, the failed grouping is terminal.
    with _lifecycle_refused("completed grouping failure is terminal"):
        _restart(life)
    _suspend(life, suspension, seq)
    assert _grouping(life) is None
    assert life.snapshot.suspensions[-1]["reason"] == suspension
    _restart(life)
    grouping = _grouping(life)
    assert (grouping["status"], grouping["grouping_id"]) == ("started", "r01-g-resumed")
    _finish(life, None, CHAT_CALLS)


# -- family 13 is unchanged -------------------------------------------------------


# ============================================================================
# Planner and CLI (Task 5; overlap table rows "planner grouping branch" and
# "CLI cause tables"; plan decision 11).
# ============================================================================


@pytest.mark.slow
@pytest.mark.parametrize(
    ("code", "reason", "suspension"),
    [
        ("role_failed", "provider_exhausted", "grouper_provider_failed"),
        ("role_failed", "provider_rejected", "grouper_provider_failed"),
        ("role_failed", "exception", "grouper_failing"),
        ("malformed_result", None, "grouping_failed"),
    ],
)
async def test_the_planner_owes_the_failed_groupings_suspension(
    tmp_path, monkeypatch, code, reason, suspension
):
    """A prefix that ends at a failed grouping plans the suspension it owes
    (the resume of a process that died before writing it)."""

    from evolution.tree.campaign_view import CampaignView, FinishCampaign, next_actions

    life = await _family14_life(tmp_path, monkeypatch, "chat")
    if reason is None:
        _fail(life, code)
    else:
        _role_failed(life, reason)
    view = CampaignView(life.snapshot, {}, None, {})
    assert next_actions(view, life.state.policy) == (
        FinishCampaign("infrastructure_failed", suspension, "none"),
    )


def test_the_cli_names_the_grouper_suspension_causes():
    from evolution.tree.cli import _suspension_detail

    causes = {
        "grouper_provider_failed": "the Grouper's provider failed after retries",
        "grouper_failing": (
            "the Grouper failed for an unexplained reason or fell back for every failure "
            "twice in a row"
        ),
    }
    for reason, cause in causes.items():
        assert _suspension_detail({"evidence_kind": "stage_record", "seq": 9}, reason=reason) == (
            cause
        )
    # An older reason keeps its own text.
    assert _suspension_detail(
        {"evidence_kind": "stage_record", "seq": 9}, reason="grouping_failed"
    ) == ("decided by the record at sequence 9")
    assert _suspension_detail({"evidence_kind": "stage_record"}, reason="reflector_failing") == (
        "the Reflector failed for an unexplained reason or twice skipped in a row"
    )
