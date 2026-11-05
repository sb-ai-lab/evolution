"""`metric_contract/1` and `role_visibility/1`."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.manifest.manifest_v15_help import connection_expected
from evolution.tree.target.connection_contract import (
    MAX_CONTRACT_BYTES,
    ConnectionContract,
    ContractError,
    MetricContract,
    RoleVisibility,
    TargetField,
    VerbatimAnswers,
    parse_metric_contract,
    parse_role_visibility,
    parse_verbatim_answers,
)

FIELD = {"event": "assistant_message", "pointer": "/content", "value": "nonempty_string"}


def contract(*fields, **overrides):
    payload = {"version": "metric_contract/1", "kind": "pointwise", "target_fields": list(fields)}
    payload.update(overrides)
    return payload


def refused(parser, value):
    with pytest.raises(ContractError) as caught:
        parser(value)
    return caught.value.code, caught.value.path, caught.value.section


def test_a_pointwise_contract_parses_and_has_a_canonical_digest():
    parsed = parse_metric_contract(contract(FIELD))
    assert parsed == MetricContract(
        "pointwise", (TargetField("assistant_message", "/content", "nonempty_string"),)
    )
    payload = parsed.to_payload()
    assert payload == contract(FIELD)
    assert parsed.digest == hashlib.sha256(canonical_json(payload)).hexdigest()
    assert parse_metric_contract(contract()).target_fields == ()


@pytest.mark.parametrize(
    "value",
    ["nonempty_string", "string", "finite_number", "boolean", "object", "array", "finite_json"],
)
def test_every_value_rule_is_admitted(value):
    assert (
        parse_metric_contract(contract({**FIELD, "value": value})).target_fields[0].value == value
    )


@pytest.mark.parametrize("pointer", ["", "/", "/a~0b/c~1d", "/0/content"])
def test_rfc_6901_pointers_are_admitted(pointer):
    assert (
        parse_metric_contract(contract({**FIELD, "pointer": pointer})).target_fields[0].pointer
        == pointer
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], ("bad_type", "", None)),
        ({"kind": "pointwise", "target_fields": []}, ("missing_key", "version", None)),
        (contract(zz=1), ("unknown_key", "zz", "")),
        (contract(version="metric_contract/2"), ("bad_value", "version", None)),
        (contract(version=1), ("bad_type", "version", None)),
        (contract(kind="collection"), ("bad_value", "kind", None)),
        (contract(kind=None), ("bad_type", "kind", None)),
        (contract(target_fields={}), ("bad_type", "target_fields", None)),
        (contract("x"), ("bad_type", "target_fields[0]", None)),
        (
            contract({"event": "e", "value": "string"}),
            ("missing_key", "target_fields[0].pointer", None),
        ),
        (contract({**FIELD, "zz": 1}), ("unknown_key", "target_fields[0].zz", "target_fields[0]")),
        (contract({**FIELD, "event": 3}), ("bad_type", "target_fields[0].event", None)),
        (contract({**FIELD, "event": "  "}), ("bad_value", "target_fields[0].event", None)),
        (contract({**FIELD, "event": "e" * 129}), ("bad_value", "target_fields[0].event", None)),
        (contract({**FIELD, "event": "é" * 65}), ("bad_value", "target_fields[0].event", None)),
        (contract({**FIELD, "pointer": 3}), ("bad_type", "target_fields[0].pointer", None)),
        (
            contract({**FIELD, "pointer": "content"}),
            ("bad_value", "target_fields[0].pointer", None),
        ),
        (contract({**FIELD, "pointer": "/a~2"}), ("bad_value", "target_fields[0].pointer", None)),
        (
            contract({**FIELD, "pointer": "/" + "a" * 1024}),
            ("bad_value", "target_fields[0].pointer", None),
        ),
        (contract({**FIELD, "value": "number"}), ("bad_value", "target_fields[0].value", None)),
        (contract({**FIELD, "value": None}), ("bad_type", "target_fields[0].value", None)),
        (
            contract(FIELD, {**FIELD, "value": "string"}),
            ("bad_value", "target_fields[1]", None),
        ),
        (
            contract(*({**FIELD, "event": f"e{index}"} for index in range(65))),
            ("bad_value", "target_fields", None),
        ),
    ],
)
def test_metric_contract_refusals(value, expected):
    assert refused(parse_metric_contract, value) == expected


def test_the_bounds_are_inclusive():
    parse_metric_contract(contract({**FIELD, "event": "e" * 128}))
    parse_metric_contract(contract({**FIELD, "event": "é" * 64}))
    parse_metric_contract(contract({**FIELD, "pointer": "/" + "a" * 1023}))
    parse_metric_contract(contract(*({**FIELD, "event": f"e{index}"} for index in range(64))))


def test_the_canonical_object_is_at_most_64_kib():
    def wide(count):
        return contract(
            *(
                {"event": f"e{index:02d}", "pointer": "/" + "a" * 1023, "value": "string"}
                for index in range(count)
            )
        )

    small = parse_metric_contract(wide(32))
    assert len(canonical_json(small.to_payload())) <= MAX_CONTRACT_BYTES
    assert len(canonical_json(wide(64))) > MAX_CONTRACT_BYTES
    assert refused(parse_metric_contract, wide(64)) == ("bad_value", "", None)


def test_a_lone_surrogate_is_a_bad_value_not_a_crash():
    assert refused(parse_metric_contract, contract({**FIELD, "event": "\ud800"})) == (
        "bad_value",
        "target_fields[0].event",
        None,
    )
    assert refused(parse_metric_contract, contract({**FIELD, "pointer": "/\ud800"})) == (
        "bad_value",
        "target_fields[0].pointer",
        None,
    )
    assert refused(
        parse_role_visibility, {"version": "role_visibility/1", "readable_paths": ["\ud800/x"]}
    ) == ("bad_value", "readable_paths[0]", None)


VISIBILITY = {"version": "role_visibility/1", "readable_paths": ["src/**/*.py", "docs/**/*.md"]}


def test_role_visibility_parses_in_written_order():
    parsed = parse_role_visibility(VISIBILITY)
    assert parsed == RoleVisibility(("src/**/*.py", "docs/**/*.md"))
    assert parsed.to_payload() == VISIBILITY
    assert parsed.digest == hashlib.sha256(canonical_json(VISIBILITY)).hexdigest()
    reordered = parse_role_visibility(
        {"version": "role_visibility/1", "readable_paths": ["docs/**/*.md", "src/**/*.py"]}
    )
    assert reordered.digest != parsed.digest
    assert (
        parse_role_visibility({"version": "role_visibility/1", "readable_paths": []}).readable_paths
        == ()
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("x", ("bad_type", "", None)),
        ({"version": "role_visibility/1"}, ("missing_key", "readable_paths", None)),
        ({**VISIBILITY, "zz": 1}, ("unknown_key", "zz", "")),
        ({**VISIBILITY, "version": "role_visibility/2"}, ("bad_value", "version", None)),
        ({**VISIBILITY, "readable_paths": "src/**"}, ("bad_type", "readable_paths", None)),
        ({**VISIBILITY, "readable_paths": [3]}, ("bad_type", "readable_paths[0]", None)),
        ({**VISIBILITY, "readable_paths": [""]}, ("bad_value", "readable_paths[0]", None)),
        ({**VISIBILITY, "readable_paths": ["/abs"]}, ("bad_value", "readable_paths[0]", None)),
        ({**VISIBILITY, "readable_paths": ["!neg"]}, ("bad_value", "readable_paths[0]", None)),
        ({**VISIBILITY, "readable_paths": ["a//b"]}, ("bad_value", "readable_paths[0]", None)),
        ({**VISIBILITY, "readable_paths": ["../x"]}, ("bad_value", "readable_paths[0]", None)),
        (
            {**VISIBILITY, "readable_paths": ["a", "b", "a"]},
            ("bad_value", "readable_paths[2]", None),
        ),
    ],
)
def test_role_visibility_refusals(value, expected):
    assert refused(parse_role_visibility, value) == expected


def test_the_connection_contract_names_its_two_identity_fields():
    connection = ConnectionContract(
        parse_metric_contract(contract(FIELD)), parse_role_visibility(VISIBILITY)
    )
    assert connection.identity_fields() == {
        "metric_contract": connection.metric_contract.digest,
        "role_visibility": connection.role_visibility.digest,
    }


@pytest.mark.parametrize(
    ("code", "detail", "section"),
    [
        ("missing_key", "scorer.metric_contract", None),
        ("bad_value", "scorer.metric_contract", None),
        ("unknown_key", "scorer.metric_contract.zz", "scorer.metric_contract"),
        ("bad_value", "scorer.metric_contract.version", None),
        ("bad_value", "scorer.metric_contract.kind", None),
        ("bad_type", "scorer.metric_contract.target_fields", None),
        ("bad_type", "scorer.metric_contract.target_fields[12]", None),
        (
            "unknown_key",
            "scorer.metric_contract.target_fields[3].zz",
            "scorer.metric_contract.target_fields[3]",
        ),
        ("missing_key", "scorer.metric_contract.target_fields[0].event", None),
        ("bad_value", "scorer.metric_contract.target_fields[0].pointer", None),
        ("bad_value", "scorer.metric_contract.target_fields[63].value", None),
        ("missing_key", "role_visibility", None),
        ("unknown_key", "role_visibility.zz", "role_visibility"),
        ("bad_value", "role_visibility.version", None),
        ("bad_type", "role_visibility.readable_paths", None),
        ("bad_value", "role_visibility.readable_paths[7]", None),
    ],
)
def test_every_connection_refusal_has_a_fixed_sentence(code, detail, section):
    text = connection_expected(code, detail, section=section)
    assert text.startswith("expected ")


def test_the_kind_sentence_names_collection_as_a_later_schema():
    text = connection_expected("bad_value", "scorer.metric_contract.kind")
    assert "'pointwise'" in text and "collection" in text


def test_validate_pattern_is_public():
    from evolution.tree.artifacts import artifact_policy

    artifact_policy.validate_pattern("skills/**")
    with pytest.raises(ValueError):
        artifact_policy.validate_pattern("../x")
    assert not hasattr(artifact_policy, "_validate_pattern")


def test_the_verbatim_answers_digest_binds_the_canonical_object_in_order():
    first = parse_verbatim_answers(
        {"version": "verbatim_answers/1", "strings": ["alpha one", "beta two"]}
    )
    again = parse_verbatim_answers(
        {"strings": ["alpha one", "beta two"], "version": "verbatim_answers/1"}
    )
    swapped = parse_verbatim_answers(
        {"version": "verbatim_answers/1", "strings": ["beta two", "alpha one"]}
    )
    assert (
        first.digest
        == again.digest
        == hashlib.sha256(canonical_json(first.to_payload())).hexdigest()
    )
    assert first.digest != swapped.digest


def test_a_declaration_adds_one_identity_field():
    contract = ConnectionContract(MetricContract("pointwise", ()), RoleVisibility(()))
    assert set(contract.identity_fields()) == {"metric_contract", "role_visibility"}
    declared = replace(
        contract, verbatim_answers=VerbatimAnswers(("This request is not implemented yet.",))
    )
    fields = declared.identity_fields()
    assert set(fields) == {"metric_contract", "role_visibility", "verbatim_answers"}
    assert declared.verbatim_answers is not None
    assert fields["verbatim_answers"] == declared.verbatim_answers.digest


def test_the_secret_oracle_paths_digest_binds_the_canonical_object_in_order():
    from evolution.tree.target.connection_contract import parse_secret_oracle_paths

    a = parse_secret_oracle_paths({"version": "secret_oracle_paths/1", "pointers": ["/a", "/b"]})
    b = parse_secret_oracle_paths({"pointers": ["/b", "/a"], "version": "secret_oracle_paths/1"})
    assert a.digest == hashlib.sha256(canonical_json(a.to_payload())).hexdigest()
    assert a.digest != b.digest
