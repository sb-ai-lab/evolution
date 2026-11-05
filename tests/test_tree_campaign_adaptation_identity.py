"""Private adaptation identity excludes diagnostic metadata from measurement identity."""

from __future__ import annotations

import pytest

from evolution.tree.campaign_identity import (
    AdaptationIdentityV1,
    build_adaptation_identity,
    reflection_group_text_chars,
)


def _identity(**changes):
    values = dict(
        profile="campaign_adaptation/1",
        grouping={
            "kind": "fields",
            "features": [
                {
                    "name": "terminal",
                    "source": "observation",
                    "path": "/terminal",
                    "description": "",
                }
            ],
        },
        selection={"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        families={"kind": "none"},
        attempt_feedback="none",
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
        reader_protocol="reflection_reader/1",
    )
    values.update(changes)
    return AdaptationIdentityV1(values)


def test_adaptation_identity_keeps_metadata_out_of_its_payload() -> None:
    identity = _identity()
    assert identity.to_payload()["reflection_metadata_digest"] == "a" * 64
    assert "reflection_metadata" not in identity.to_payload()


def test_builder_freezes_a_compact_command_grouping_proof() -> None:
    identity = build_adaptation_identity(
        grouping={"kind": "command", "command_digest": "c" * 64, "config_digest": "d" * 64},
        selection={"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        families={"kind": "none"},
        attempt_feedback="none",
        reflection_metadata_digest="a" * 64,
        environment_docs_digest=None,
    )
    assert identity.to_payload()["grouping"]["command_digest"] == "c" * 64


@pytest.mark.parametrize(
    ("reflection", "limit"),
    [
        ({"max_consecutive_failures": 2}, 512),
        ({"max_consecutive_failures": 2, "group_text_chars": 4096}, 4096),
        ({"max_consecutive_failures": 2, "group_text_chars": None}, None),
    ],
)
def test_a_journaled_reflection_without_the_limit_means_512(reflection, limit) -> None:
    """#282, R2: an absent key is 512 in both families, whatever a manifest now means by it."""

    assert reflection_group_text_chars(reflection) == limit


def _final_chat_payload(tmp_path, **keys):
    from tests.tree.manifest.test_manifest_v16 import (
        _role_adaptation,
        chat_roles_document,
        load,
        v16_document,
    )

    document = chat_roles_document(v16_document())
    document["grouper"].update(keys)
    return _role_adaptation(load(tmp_path, document), tmp_path).to_payload()


def test_final_profile_admits_model_grouping_v2_and_its_keys(tmp_path) -> None:
    keys = {
        "group_output_tokens": 10**9,
        "glimpse_chars": 1,
        "max_set_members": 2,
        "read_chars": 1,
        "max_turns": 1,
        "card_examples": 0,
        "max_reduce_levels": 1,
        "max_requests": 1,
    }
    payload = _final_chat_payload(tmp_path, **keys)
    assert payload["grouping"]["protocol"] == "model_grouping/2"
    assert AdaptationIdentityV1(payload).to_payload() == payload


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("group_output_tokens", 0),
        ("glimpse_chars", True),
        ("max_set_members", 1),
        ("read_chars", "16000"),
        ("max_turns", 0),
        ("card_examples", -1),
        ("max_reduce_levels", 1.0),
        ("max_requests", None),
    ],
)
def test_final_profile_refuses_a_bad_grouping_v2_key(tmp_path, key, value) -> None:
    from evolution.tree.campaign_identity import IdentityError

    payload = _final_chat_payload(tmp_path)
    payload["grouping"][key] = value
    with pytest.raises(IdentityError):
        AdaptationIdentityV1(payload)


def test_grouping_v2_keys_need_the_v2_protocol(tmp_path) -> None:
    from evolution.tree.campaign_identity import IdentityError

    payload = _final_chat_payload(tmp_path, max_turns=4)
    payload["grouping"]["protocol"] = "model_grouping/1"
    with pytest.raises(IdentityError):
        AdaptationIdentityV1(payload)
    del payload["grouping"]["max_turns"]
    AdaptationIdentityV1(payload)
    payload["grouping"]["protocol"] = "model_grouping/3"
    with pytest.raises(IdentityError):
        AdaptationIdentityV1(payload)


def test_older_profiles_refuse_model_grouping_v2(tmp_path) -> None:
    from evolution.tree.campaign_identity import IdentityError
    from tests.tree.manifest.test_manifest_v16 import (
        _role_adaptation,
        chat_roles_document,
        load,
        v15_document,
    )

    # The /15 payload: an older profile admits neither /16's evidence nor checks.
    payload = _role_adaptation(
        load(tmp_path, chat_roles_document(v15_document())), tmp_path
    ).to_payload()
    payload["profile"] = "campaign_adaptation/6"
    payload["grouping"]["kind"] = "model"
    AdaptationIdentityV1(payload)
    payload["grouping"]["protocol"] = "model_grouping/2"
    with pytest.raises(IdentityError):
        AdaptationIdentityV1(payload)
