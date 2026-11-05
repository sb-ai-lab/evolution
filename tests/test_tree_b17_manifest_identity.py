"""b17 settings admit only /16, bind when declared, and reach both requests."""

import hashlib

import pytest

from evolution.tree.campaign_identity import AdaptationIdentityV1, adaptation_identity_slot
from evolution.tree.campaign_schema import SchemaError, _validate_adaptation_payload
from evolution.tree.evolve import _chat_grouper_identity
from evolution.tree.execution.journal_identity import IdentityError, canonical_json
from evolution.tree.manifest import manifest_v2 as manifest
from evolution.tree.roles.editor.chat_editor import request_options
from evolution.tree.roles.grouping.model_grouping_runtime import _request_options
from evolution.tree.roles.grouping.portion_session import chat_section
from evolution.tree.roles.seals import section_payload
from tests.test_tree_b14_manifest_identity import parse_role
from tests.test_tree_family14_identity import adaptation


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize("pin", ["provider_only", "provider_order"])
@pytest.mark.parametrize("value", [True, False])
def test_v16_require_parameters_parses_on_pinned_roles(tmp_path, role, pin, value):
    section = parse_role(
        tmp_path, role, manifest.SCHEMA_V16, **{pin: ["provider"], "require_parameters": value}
    )
    assert section.require_parameters is value


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
def test_v16_require_parameters_without_pin_is_bad_value(tmp_path, role):
    with pytest.raises(manifest.ManifestError) as caught:
        parse_role(tmp_path, role, manifest.SCHEMA_V16, require_parameters=False)
    assert (caught.value.code, caught.value.detail, caught.value.expected) == (
        "bad_value",
        f"{role}.require_parameters",
        "require_parameters applies only with provider_only or provider_order",
    )


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize("value", ["false", 0, None])
def test_v16_require_parameters_must_be_boolean(tmp_path, role, value):
    with pytest.raises(manifest.ManifestError) as caught:
        parse_role(
            tmp_path,
            role,
            manifest.SCHEMA_V16,
            provider_only=["provider"],
            require_parameters=value,
        )
    assert (caught.value.code, caught.value.detail) == ("bad_type", f"{role}.require_parameters")


@pytest.mark.parametrize("role", ["editor", "reflector"])
@pytest.mark.parametrize("value", [0, 10**9])
def test_v16_nearest_paths_bounds(tmp_path, role, value):
    assert (
        parse_role(tmp_path, role, manifest.SCHEMA_V16, nearest_paths=value).nearest_paths == value
    )


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize("value,code", [(-1, "bad_value"), (True, "bad_type")])
def test_v16_nearest_paths_invalid_or_unknown(tmp_path, role, value, code):
    with pytest.raises(manifest.ManifestError) as caught:
        parse_role(tmp_path, role, manifest.SCHEMA_V16, nearest_paths=value)
    assert (caught.value.code, caught.value.detail) == (
        "unknown_key" if role == "grouper" else code,
        f"{role}.nearest_paths",
    )


@pytest.mark.parametrize("role", ["editor", "reflector", "grouper"])
@pytest.mark.parametrize("key,value", [("require_parameters", False), ("nearest_paths", 0)])
def test_v15_refuses_b17_role_keys(tmp_path, role, key, value):
    with pytest.raises(manifest.ManifestError) as caught:
        parse_role(tmp_path, role, manifest.SCHEMA_V15, **{key: value})
    assert (caught.value.code, caught.value.detail) == ("unknown_key", f"{role}.{key}")


def _payload(section, role):
    return (
        _chat_grouper_identity(section, family15=True)
        if role == "grouper"
        else section_payload(section)
    )


@pytest.mark.parametrize(
    "role,digest",
    [
        ("editor", "599a334d15e925c3eefa6768a57cb4cd628b2b7d8db1bc20bac3b4142c216aee"),
        ("reflector", "599a334d15e925c3eefa6768a57cb4cd628b2b7d8db1bc20bac3b4142c216aee"),
        ("grouper", "926c57741fc0fb7d0fe2e560a1fc21260cfdf6fff785d1ca202af20f686460be"),
    ],
)
def test_absent_b17_role_keys_keep_identity_bytes(tmp_path, role, digest):
    # Captured before b17 production edits at 7f1eebcaa.
    payload = _payload(parse_role(tmp_path, role, manifest.SCHEMA_V16), role)
    assert hashlib.sha256(canonical_json(payload)).hexdigest() == digest
    assert not {"require_parameters", "nearest_paths"} & payload.keys()


@pytest.mark.parametrize(
    "role,key,value",
    [
        (role, "require_parameters", value)
        for role in ("editor", "reflector", "grouper")
        for value in (True, False)
    ]
    + [(role, "nearest_paths", 0) for role in ("editor", "reflector")],
)
def test_declared_b17_role_keys_enter_identity(tmp_path, role, key, value):
    base = parse_role(tmp_path, role, manifest.SCHEMA_V16, provider_only=["provider"])
    declared = parse_role(
        tmp_path, role, manifest.SCHEMA_V16, provider_only=["provider"], **{key: value}
    )
    assert _payload(declared, role) == {**_payload(base, role), key: value}


@pytest.mark.parametrize("pin", ["provider_only", "provider_order"])
@pytest.mark.parametrize("value", [True, False])
def test_grouper_request_body_honours_require_parameters(tmp_path, pin, value):
    section = parse_role(
        tmp_path, "grouper", manifest.SCHEMA_V16, **{pin: ["provider"], "require_parameters": value}
    )
    expected = {
        "extra_body": {
            "provider": {
                "only" if pin == "provider_only" else "order": ["provider"],
                "allow_fallbacks": False,
                "require_parameters": value,
            }
        }
    }
    assert _request_options(section) == expected
    assert request_options(chat_section(section)) == expected


@pytest.mark.parametrize("pin", ["provider_only", "provider_order"])
def test_family15_grouping_identity_admits_require_parameters_only_with_pin(tmp_path, pin):
    base = _chat_grouper_identity(parse_role(tmp_path, "grouper", manifest.SCHEMA_V15))
    base.update({pin: ["provider"], "require_parameters": False})
    if pin == "provider_order":
        base["provider_only"] = None
    identity = adaptation(base)
    _validate_adaptation_payload(
        identity.to_payload(), adaptation_identity_slot(identity), family_version=15
    )
    base.pop(pin)
    with pytest.raises(IdentityError):
        adaptation(base)


def test_family14_grouping_identity_refuses_require_parameters(tmp_path):
    grouping = _chat_grouper_identity(
        parse_role(tmp_path, "grouper", manifest.SCHEMA_V15, provider_only=["provider"])
    )
    grouping["require_parameters"] = False
    identity = adaptation(grouping)
    with pytest.raises(SchemaError, match="family 14 adaptation"):
        _validate_adaptation_payload(
            identity.to_payload(), adaptation_identity_slot(identity), family_version=14
        )


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_grouping_identity_refuses_nonboolean_require_parameters(tmp_path, value):
    grouping = _chat_grouper_identity(
        parse_role(tmp_path, "grouper", manifest.SCHEMA_V15, provider_only=["provider"])
    )
    grouping["require_parameters"] = value
    with pytest.raises(IdentityError):
        AdaptationIdentityV1.from_payload(adaptation().to_payload() | {"grouping": grouping})
