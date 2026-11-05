"""Recorded outbound dialects preserve the typed probe's JSON schema."""

import json
from pathlib import Path

import pytest

from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.editor.chat_editor import _protocol_options
from evolution.tree.roles.reflector.chat_reflector import reflect_tools

FIXTURES = Path(__file__).parent / "fixtures/tool_schemas"
ROUTES = {
    "modern": ("https://api.openai.com/v1", "modern"),
    "modern_single": ("https://api.openai.com/v1", "modern_single"),
    "modern_auto": ("https://api.openai.com/v1", "modern_auto"),
    "legacy": ("https://api.openai.com/v1", "legacy"),
    "legacy_preflight": ("https://api.openai.com/v1", "legacy_preflight"),
    "openrouter": ("https://openrouter.ai/api/v1", "modern"),
}


def outbound_options(route):
    base_url, style = ROUTES[route]
    section = ChatEditorSection(model="offline-model", base_url=base_url, api_key_env=None)
    tools = reflect_tools(probe=True, adaptive=True, trace=True, typed_body=True)
    return _protocol_options(section, tools, style)


@pytest.mark.parametrize("route", ROUTES)
def test_typed_probe_schema_in_recorded_outbound_dialect(route):
    actual = json.loads(json.dumps(outbound_options(route)))
    expected = json.loads((FIXTURES / f"{route}.json").read_text())
    assert actual == expected
    functions = actual.get("functions", [tool["function"] for tool in actual.get("tools", [])])
    body = next(f for f in functions if f["name"] == "probe_request")["parameters"]["properties"][
        "body"
    ]
    assert body["type"] == ["object", "array", "string", "number", "boolean", "null"]


def test_only_pinned_probe_openings_explain_json_values():
    from evolution.tree.roles.reflection_role_helpers import probe_opening
    from evolution.tree.roles.reflector_contracts import ProbeAccess

    access = ProbeAccess("http://localhost:1234/probe", 2, ("POST",), None)
    sentence = "body is the JSON value to send (an object, a list, a string, a number, true, false or null), not text that contains JSON."
    assert sentence in probe_opening(access, typed_body=True)
    assert sentence not in probe_opening(access)
