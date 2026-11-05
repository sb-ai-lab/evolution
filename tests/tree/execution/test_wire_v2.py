"""Behavioral contracts for the `/evolution/v2` wire schemas and identifier grammar."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from evolution.tree.execution.wire_v2 import (
    IDENTIFIER,
    REQUIRED_ATTESTS,
    WireError,
    canonical_body,
    is_instance_id,
    new_instance_id,
    parse_close,
    parse_error,
    parse_health,
    parse_instance,
    parse_release,
    parse_session,
    parse_turn,
    validate_instance_components,
    validate_instance_description,
    validate_turn_provenance,
    validate_turn_reason,
)

SHA = "a" * 64


def health(**over):
    base = dict(
        ready=True,
        protocol_version="2",
        target_id="t",
        boot_id="boot-1",
        contract_digest="c1",
        settings_hash="s1",
        runtime_ref_digest="r1",
        seed_mode="exact",
        allowed_artifact_root="/tmp/runs",
        policy_digest=SHA,
        capabilities=dict(
            max_instances=2,
            max_concurrent_sessions=4,
            materialization=["artifact_root_injection"],
            attests=list(REQUIRED_ATTESTS),
            max_input_bytes=1 << 20,
            max_output_bytes=1 << 20,
            instantiable=True,
        ),
        instances=[],
    )
    base.update(over)
    return base


def instance(**over):
    base = dict(
        instance_id="run1-" + "0" * 32,
        boot_id="boot-1",
        contract_digest="c1",
        settings_hash="s1",
        runtime_ref_digest="r1",
        seed_mode="exact",
        protocol_version="2",
        strategy="artifact_root_injection",
        loaded_candidate_digest=SHA,
        dependency_digest=SHA,
        attested=list(REQUIRED_ATTESTS),
        file_count=3,
        byte_count=18,
    )
    base.update(over)
    return base


def test_identifier_grammar_and_instance_ids():
    assert (
        IDENTIFIER.match("run-1.a_b")
        and not IDENTIFIER.match("a/b")
        and not IDENTIFIER.match("x" * 129)
    )
    iid = new_instance_id("run1")
    assert is_instance_id(iid, "run1") and iid.startswith("run1-") and len(iid.split("-")[-1]) == 32
    assert not is_instance_id("run1-short", "run1") and not is_instance_id(iid, "other")
    with pytest.raises(ValueError):
        new_instance_id("bad/run")


def test_canonical_body_is_sorted_compact_utf8():
    assert canonical_body({"b": 1, "a": {"y": "ю", "x": [1, 2]}}) == (
        b'{"a":{"x":[1,2],"y":"\xd1\x8e"},"b":1}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_body_refuses_the_json_extensions(value):
    """`NaN`/`Infinity` are Python's extension to JSON, not JSON: a peer that
    parses strictly could not read them back, so they can never be a replay key."""
    with pytest.raises(ValueError):
        canonical_body({"latency_ms": value})


def test_canonical_body_refuses_an_unserializable_body():
    with pytest.raises(ValueError):
        canonical_body({"root": object()})


def test_parse_health_accepts_exact_shape_and_rejects_unknown_or_bad():
    h = parse_health(health())
    assert h.capabilities.max_instances == 2 and h.capabilities.attests == REQUIRED_ATTESTS
    for bad in (
        health(extra=1),
        health(protocol_version="1"),
        health(seed_mode="random"),
        health(capabilities=dict(health()["capabilities"], max_instances=0)),
        health(policy_digest="nope"),
        health(boot_id=""),
    ):
        with pytest.raises(ValueError):
            parse_health(bad)


def test_parse_instance_session_turn_and_error():
    inst = parse_instance(instance())
    assert inst.attested == REQUIRED_ATTESTS
    assert (
        parse_session(dict(session_id="s1", requested_seed=7, effective_seed=7)).effective_seed == 7
    )
    with pytest.raises(ValueError):
        parse_session(dict(session_id="s/1", requested_seed=None, effective_seed=None))
    turn = parse_turn(
        dict(
            events=[{"type": "assistant_message", "content": "x"}],
            usage=None,
            latency_ms=3,
            outcome="ok",
            truncated=False,
        )
    )
    assert turn.outcome == "ok" and turn.truncated is False
    with pytest.raises(ValueError):
        parse_turn(dict(events=[], usage=None, latency_ms=3, outcome="ok", truncated=False))
    err = parse_error(409, dict(error=dict(code="request_conflict", message="m", retryable=False)))
    assert isinstance(err, WireError) and err.code == "request_conflict" and err.status == 409
    assert (
        parse_error(500, dict(error=dict(code="weird", message="", retryable=True))).code
        == "internal"
    )


def test_parse_release_and_close_are_closed_state_transitions():
    """Both deletes have a closed body; a 200 that does not carry it proves
    nothing about the resource the caller addressed."""
    instance_id = "run1-" + "0" * 32
    assert parse_release(dict(instance_id=instance_id, released=True)).instance_id == instance_id
    assert parse_release(dict(instance_id=instance_id, released=False)).released is False
    assert parse_close(dict(session_id="s1", closed=True)).session_id == "s1"
    assert parse_close(dict(session_id="s1", closed=False)).closed is False

    for bad in (
        None,
        [],
        dict(instance_id=instance_id),
        dict(instance_id=instance_id, released="yes"),
        dict(instance_id=instance_id, released=1),
        dict(instance_id=instance_id, released=True, extra=1),
        dict(instance_id="a/b", released=True),
    ):
        with pytest.raises(ValueError):
            parse_release(bad)

    for bad in (
        None,
        dict(closed=True),
        dict(session_id="s1", closed="no"),
        dict(session_id="s1", closed=True, extra=1),
        dict(session_id="s/1", closed=True),
    ):
        with pytest.raises(ValueError):
            parse_close(bad)


@pytest.mark.parametrize(
    "root", ["", "   ", ".", "runs", "./runs", "../runs", "runs/nested", "/runs/\x00bad"]
)
def test_parse_health_refuses_a_non_absolute_allowed_artifact_root(root):
    """`allowed_artifact_root` must be an absolute directory. A relative
    one would be resolved in the *client's* working directory, so conformance
    could create its run root anywhere the CLI happened to be started."""
    with pytest.raises(ValueError):
        parse_health(health(allowed_artifact_root=root))


@pytest.mark.parametrize("root", ["/", "/tmp/runs", "/tmp/runs with space"])
def test_parse_health_accepts_an_absolute_allowed_artifact_root(root):
    assert parse_health(health(allowed_artifact_root=root)).allowed_artifact_root == root


def _turn_body(**overrides):
    body = {
        "events": [{"type": "assistant_message", "content": "hi"}],
        "usage": None,
        "latency_ms": 5,
        "outcome": "ok",
        "truncated": False,
    }
    body.update(overrides)
    return body


def test_parse_turn_admits_candidate_failed():
    parsed = parse_turn(_turn_body(outcome="candidate_failed"))
    assert parsed.outcome == "candidate_failed"


def test_parse_turn_still_refuses_an_unknown_outcome():
    with pytest.raises(ValueError):
        parse_turn(_turn_body(outcome="agent_failed"))


def test_parse_turn_accepts_a_reason():
    parsed = parse_turn(
        _turn_body(outcome="candidate_failed", reason={"category": "format", "code": "no_json"})
    )
    assert parsed.reason == {"category": "format", "code": "no_json"}


def test_parse_turn_without_a_reason_is_unchanged():
    assert parse_turn(_turn_body()).reason is None


def test_parse_turn_accepts_a_reason_on_ok():
    # PDR-0014: permitted on any outcome; core carries it and concludes nothing.
    assert parse_turn(_turn_body(reason={"category": "retry", "code": "once"})).reason is not None


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "format"},
        {"category": "format", "code": "no_json", "extra": "x"},
        {"category": "", "code": "no_json"},
        {"category": "format", "code": "a" * 65},
        {"category": "has space", "code": "no_json"},
        {"category": 1, "code": "no_json"},
        "format/no_json",
    ],
)
def test_parse_turn_refuses_a_malformed_reason(bad):
    with pytest.raises(ValueError):
        parse_turn(_turn_body(reason=bad))


def test_parse_instance_accepts_a_description():
    parsed = parse_instance(instance(description={"config_hash": "abc", "sealed": True}))
    assert parsed.description == {"config_hash": "abc", "sealed": True}


def test_parse_instance_without_a_description_is_unchanged():
    assert parse_instance(instance()).description is None


@pytest.mark.parametrize(
    "bad",
    [
        {"nested": {"a": 1}},
        {"listy": [1, 2]},
        {"bad key!": 1},
        {"inf": float("inf")},
        "not-an-object",
        {f"k{i}": i for i in range(33)},
    ],
)
def test_parse_instance_refuses_a_malformed_description(bad):
    with pytest.raises(ValueError):
        parse_instance(instance(description=bad))


# -- ADR-0018: additive wire widening, both directions the asymmetry bites ---

_V6_ERA_INSTANCE_KEYS = frozenset(
    {
        "instance_id",
        "boot_id",
        "contract_digest",
        "settings_hash",
        "runtime_ref_digest",
        "seed_mode",
        "protocol_version",
        "strategy",
        "loaded_candidate_digest",
        "dependency_digest",
        "attested",
        "file_count",
        "byte_count",
    }
)


def test_an_old_parser_refuses_a_new_targets_description():
    # ADR-0018: old core against a new target fails at instance creation,
    # before any turn -- loudly, never as a silent misreading.
    body = instance(description={"config_hash": "abc"})
    assert not body.keys() <= _V6_ERA_INSTANCE_KEYS


_V6_ERA_TURN_OUTCOMES = frozenset({"ok", "target_failed"})


def test_an_old_parser_refuses_a_new_outcome():
    # ADR-0018, the same direction as the description above: a body a new
    # target really emits, checked against the `/6`-era outcome set an old
    # core would have compared against. Driven through `parse_turn`, so the
    # test is about the served value and not about two literals.
    served = parse_turn(_turn_body(outcome="candidate_failed"))
    assert served.outcome not in _V6_ERA_TURN_OUTCOMES
    assert _V6_ERA_TURN_OUTCOMES < frozenset({"ok", "target_failed", "candidate_failed"})


def test_a_new_parser_accepts_an_old_target():
    # The direction the `optional` slot actually buys (ADR-0018). Both
    # helpers emit a body with no new key at all, exactly as a `/6`-era
    # target would.
    assert parse_instance(instance()).description is None
    assert parse_turn(_turn_body()).reason is None


# -- the declared parameter type is `Mapping`, not `dict` (ADR-0009, PDR-0014) --


def test_a_turn_reason_may_arrive_as_any_mapping():
    """`TurnResponse.reason` and `Turn.reason` are both annotated
    `Mapping[str, str] | None`, so a non-`dict` `Mapping` validates."""

    value = validate_turn_reason(MappingProxyType({"category": "format", "code": "no_json"}))
    assert value == {"category": "format", "code": "no_json"}
    assert type(value) is dict


def test_an_instance_description_may_arrive_as_any_mapping():
    value = validate_instance_description(MappingProxyType({"config_hash": "abc"}))
    assert value == {"config_hash": "abc"}
    assert type(value) is dict


@pytest.mark.parametrize(
    "factory",
    [
        lambda: {"category": "format", "code": "no_json"},
        lambda: MappingProxyType({"category": "format", "code": "no_json"}),
    ],
)
def test_a_validated_reason_is_detached_from_the_caller(factory):
    source = factory()
    validated = validate_turn_reason(source)
    assert validated is not source


def test_a_non_mapping_reason_is_still_refused():
    for bad in ([("category", "a"), ("code", "b")], "category", 3):
        with pytest.raises(ValueError):
            validate_turn_reason(bad)
    for bad in ([("config_hash", "a")], "config_hash", 3):
        with pytest.raises(ValueError):
            validate_instance_description(bad)


@pytest.mark.parametrize(
    "used",
    [
        [],
        ["skills/deposits/SKILL.md", "prompts/system.md"],
        ["skills/deposits#body", "catalog"],
        ["skills/депозиты/SKILL.md"],
        ["a" * 256],
        ["x"] * 1024,
        ["dup", "dup"],
    ],
)
def test_valid_provenance_is_accepted_and_copied(used):
    value = {"used": used}
    copy = validate_turn_provenance(value)
    assert copy == {"used": used}
    assert copy is not value and copy["used"] is not used


def test_provenance_accepts_a_leading_slash_as_a_component_identifier():
    # Spec 4.1: the component grammar is [A-Za-z0-9._/#:-]{1,256}, so "/abs"
    # is a valid identifier even though it is not an artifact path.
    assert validate_turn_provenance({"used": ["/abs"]}) == {"used": ["/abs"]}


def test_provenance_accepts_a_mapping_proxy_and_none():
    assert validate_turn_provenance(None) is None
    assert validate_turn_provenance(MappingProxyType({"used": ("a",)})) == {"used": ["a"]}


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"used": "a"},
        {"used": ["a"], "extra": 1},
        {"used": [1]},
        {"used": [""]},
        {"used": ["a" * 257]},
        {"used": ["a b\\c"]},
        {"used": ["a\\b"]},
        {"used": ["a/../b c"]},
        {"used": ["line\nbreak"]},
        {"used": ["x"] * 1025},
    ],
)
def test_malformed_provenance_is_refused(value):
    with pytest.raises(ValueError):
        validate_turn_provenance(value)


def test_parse_turn_carries_provenance_on_every_outcome():
    for outcome in ("ok", "target_failed", "candidate_failed"):
        turn = parse_turn(_turn_body(outcome=outcome, provenance={"used": ["a"]}))
        assert turn.provenance == {"used": ["a"]}
    assert parse_turn(_turn_body()).provenance is None


def test_parse_turn_refuses_malformed_provenance_and_null():
    with pytest.raises(ValueError):
        parse_turn(_turn_body(provenance={"used": ["a b\\c"]}))
    with pytest.raises(ValueError):
        parse_turn(_turn_body(provenance=None))


def test_valid_components_are_accepted_and_copied():
    sources = ["skills/deposits/SKILL.md"]
    value = {
        "skills/deposits#body": {"sha256": SHA, "sources": sources},
        "system": {"sha256": SHA, "sources": []},
    }
    copy = validate_instance_components(value)
    assert copy == value
    assert copy["skills/deposits#body"]["sources"] is not sources
    assert validate_instance_components({}) == {}
    assert validate_instance_components(None) is None


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"bad key!": {"sha256": SHA, "sources": []}},
        {"k": {"sha256": "A" * 64, "sources": []}},
        {"k": {"sha256": SHA}},
        {"k": {"sha256": SHA, "sources": [], "x": 1}},
        {"k": {"sha256": SHA, "sources": ["/abs"]}},
        {"k": {"sha256": SHA, "sources": ["a", "a"]}},
        {f"k{i}": {"sha256": SHA, "sources": []} for i in range(4097)},
    ],
)
def test_malformed_components_are_refused(value):
    with pytest.raises(ValueError):
        validate_instance_components(value)


def test_components_over_256_kib_are_refused():
    long_path = "/".join(["d" * 200] * 20)  # ~4 KiB, valid path grammar
    value = {f"c{i}": {"sha256": SHA, "sources": [long_path]} for i in range(70)}
    with pytest.raises(ValueError):
        validate_instance_components(value)


def test_parse_instance_carries_components():
    comps = {"c": {"sha256": SHA, "sources": []}}
    assert parse_instance(instance(components=comps)).components == comps
    assert parse_instance(instance()).components is None
    with pytest.raises(ValueError):
        parse_instance(instance(components=None))
