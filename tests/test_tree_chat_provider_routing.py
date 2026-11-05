"""Soft routes change requests, admission and private telemetry together."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evolution.tree.roles.editor.chat_editor import (
    ProviderIdentityError,
    _verify_identity,
    provider_request,
    request_options,
)
from evolution.tree.roles.grouping.model_grouping_runtime import _request_options
from evolution.tree.roles.workspace import open_attempt_log
from tests.model_grouping_fakes import ScriptedClient
from tests.test_tree_b14_manifest_identity import parse_role
from tests.tree.roles.grouping.test_model_grouping_runtime import VALID, run, section


@pytest.mark.parametrize(
    "keys,want",
    [
        ({}, {}),
        (
            {"provider_only": ["fireworks"], "require_parameters": False},
            {"only": ["fireworks"], "allow_fallbacks": False, "require_parameters": False},
        ),
        (
            {"provider_order": ["fireworks"], "require_parameters": False},
            {"order": ["fireworks"], "allow_fallbacks": False, "require_parameters": False},
        ),
        (
            {"provider_only": ["fireworks"]},
            {"only": ["fireworks"], "allow_fallbacks": False, "require_parameters": True},
        ),
        (
            {"provider_order": ["fireworks", "together"]},
            {
                "order": ["fireworks", "together"],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        ),
        (
            {"provider_order": ["fireworks"], "allow_fallbacks": True},
            {"order": ["fireworks"], "allow_fallbacks": True, "require_parameters": True},
        ),
        (
            {"provider_order": ["fireworks"], "quantizations": ["fp8"]},
            {
                "order": ["fireworks"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "quantizations": ["fp8"],
            },
        ),
    ],
)
def test_request_body_table(tmp_path, keys, want):
    role = parse_role(tmp_path, "editor", "evolution-v2-manifest/16", **keys)
    assert request_options(role) == ({"extra_body": {"provider": want}} if want else {})


@pytest.mark.parametrize("fallbacks", [None, False, True])
def test_grouper_request_body_matches(tmp_path, fallbacks):
    keys = {"provider_order": ["fireworks", "together"]}
    if fallbacks is not None:
        keys["allow_fallbacks"] = fallbacks
    editor = parse_role(tmp_path, "editor", "evolution-v2-manifest/16", **keys)
    grouper = parse_role(tmp_path, "grouper", "evolution-v2-manifest/16", **keys)
    assert _request_options(grouper) == request_options(editor)
    assert _request_options(grouper)["extra_body"]["provider"]["order"] == ["fireworks", "together"]


def response(provider, model="test-model"):
    return SimpleNamespace(provider=provider, model=model, choices=[SimpleNamespace()])


@pytest.mark.parametrize("provider", ["fireworks", "together", "Fire-Works"])
def test_order_without_fallbacks_accepts_listed_and_normalized(tmp_path, provider):
    role = parse_role(
        tmp_path, "editor", "evolution-v2-manifest/16", provider_order=["fireworks", "together"]
    )
    _verify_identity(response(provider), role)


@pytest.mark.parametrize("provider", [None, "unlisted"])
def test_order_without_fallbacks_rejects_unlisted(tmp_path, provider):
    role = parse_role(tmp_path, "editor", "evolution-v2-manifest/16", provider_order=["fireworks"])
    with pytest.raises(ProviderIdentityError):
        _verify_identity(response(provider), role)


@pytest.mark.parametrize(
    "provider,fallback", [("together", True), (None, True), ("Fire-Works", False)]
)
@pytest.mark.parametrize("retry", [False, True])
async def test_fallback_accepts_any_and_logs(tmp_path, provider, fallback, retry):
    role = parse_role(
        tmp_path,
        "reflector",
        "evolution-v2-manifest/16",
        provider_order=["fireworks"],
        allow_fallbacks=True,
    )

    async def create(**kwargs):
        return response(provider)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with open_attempt_log(tmp_path / "logs", "route", secrets=()) as log:
        result = await provider_request(
            client=client,
            section=role,
            log=log,
            messages=[],
            tools=[],
            request_style="modern",
            turn=1,
            remaining=lambda: 10,
            retry=role.retry if retry else None,
            deadline=time.monotonic() + 10,
        )
    assert result.provider == provider
    records = [
        json.loads(line) for line in (tmp_path / "logs/route.jsonl").read_text().splitlines()
    ]
    assert [record for record in records if record["phase"] == "provider_route"] == [
        {"phase": "provider_route", "returned_provider": provider, "fallback": fallback}
    ]


def test_fallback_still_requires_model(tmp_path):
    role = parse_role(
        tmp_path,
        "editor",
        "evolution-v2-manifest/16",
        provider_order=["fireworks"],
        allow_fallbacks=True,
    )
    with pytest.raises(ProviderIdentityError):
        _verify_identity(response("together", "wrong-model"), role)


def test_provider_only_unchanged(tmp_path):
    role = parse_role(
        tmp_path,
        "editor",
        "evolution-v2-manifest/15",
        provider_only=["fireworks"],
        quantizations=["fp8"],
    )
    assert request_options(role) == {
        "extra_body": {
            "provider": {
                "only": ["fireworks"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "quantizations": ["fp8"],
            }
        }
    }
    _verify_identity(response("Fire-Works"), role)
    with pytest.raises(ProviderIdentityError):
        _verify_identity(response("together"), role)


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"provider_only": ("fireworks",)},
        {"provider_order": ("fireworks",)},
        {"provider_order": ("fireworks",), "allow_fallbacks": True},
    ],
)
async def test_grouper_never_verifies(tmp_path, keys):
    # The real grouping runtime receives a response with no model/provider.
    client = ScriptedClient([VALID])
    result = await run(client, tmp_path, sec=replace(section(), **keys))
    assert len(result.grouped.neighborhoods) == 2
    assert result.fallback is None
