"""Offline /16 scenarios must measure the body the direct role actually sends."""

import hashlib
import json
from dataclasses import replace

import httpx
import openai
import pytest

from tests.tree.roles.editor import test_chat_editor as editor_tests
from tests.tree.roles.reflector import test_chat_reflector as reflector_tests
from tests.tree.roles.reflector.test_chat_reflector import reflect_request  # noqa: F401


@pytest.fixture
def pinned_inputs(tmp_path):
    from evolution.tree.execution.check_modes import ENFORCE_ALL
    from evolution.tree.execution.evidence_settings import DEFAULT_EVIDENCE, EvidencePin
    from tests.family15_fakes import v16_document
    from tests.test_tree_adaptive_reflector import adaptive_fixture
    from tests.tree.manifest.test_manifest_v15 import chat_roles_document, load

    document = chat_roles_document(v16_document())
    for role in ("editor", "reflector"):
        document[role].update(
            context_compact_chars=200,
            context_message_max_chars=100,
            max_turns=3,
            reasoning_effort="high",
        )
    manifest = load(tmp_path / "package", document)
    pin = EvidencePin(
        manifest.workflow.evidence or DEFAULT_EVIDENCE,
        (manifest.workflow.checks or ENFORCE_ALL).input_modes(),
    )
    trace, inputs, brief = adaptive_fixture(tmp_path / "baseline-source", pin=pin, docs=True)
    try:
        with inputs.reader_view().open_operation(
            "context-test", {"list_evidence", "read_evidence"}
        ) as reader:
            yield manifest.workflow, inputs, brief, reader
    finally:
        trace.close()


@pytest.mark.parametrize("role", ["editor", "reflector"])
@pytest.mark.parametrize("style", ["legacy", "modern"])
async def test_first_and_compacted_page_equal_sdk_transport(
    tmp_path,
    reflect_request,  # noqa: F811 - imported pytest fixture
    pinned_inputs,
    role,
    style,
):
    from evolution.tree.roles.context_preflight import measure_chat_scenario
    from evolution.tree.roles.editor.editor_brief import render_catalog

    workflow, inputs, projected, reader = pinned_inputs
    page_id = next(iter(projected.observation_ids)) if role == "reflector" else "summary"
    captured = []

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        name, args = (
            ("list_files", {})
            if len(captured) == 1
            else ("read_evidence", {"evidence_id": page_id})
        )
        call = {"name": name, "arguments": json.dumps(args)}
        message = {"role": "assistant"}
        if style == "legacy":
            message["function_call"] = call
        else:
            message["tool_calls"] = [{"id": "call", "type": "function", "function": call}]
        return httpx.Response(
            200,
            json={
                "id": "chat",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}],
            },
        )

    if role == "reflector":
        runner, _ = reflector_tests._reflector(tmp_path, [], result_repair=True)
        section = workflow.reflector.role
        request = replace(reflect_request, evidence=projected, reader=reader)
        brief = projected
        page = inputs.read_evidence({"evidence_id": page_id})
    else:
        runner, _ = editor_tests._editor(tmp_path, [])
        section = workflow.editor
        brief = replace(editor_tests._brief(), pin=inputs.pin)
        request = replace(
            editor_tests._request(editor_tests._ws(tmp_path), brief=brief), traces=inputs.traces
        )
        page_text = next(r.content for r in render_catalog(brief) if r.evidence_id == page_id)
        page = {
            "id": page_id,
            "content": page_text,
            "next_offset": None,
            "total_chars": len(page_text),
            "content_digest": hashlib.sha256(page_text.encode()).hexdigest(),
        }
    client = openai.AsyncOpenAI(
        base_url="https://local.invalid/v1",
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    # The SDK otherwise launches a worker thread to discover host metadata.
    client._platform = "Linux"
    runner = replace(
        runner,
        section=section,
        family15=True,
        role_resilience=True,
        client_factory=lambda *_: client,
    )
    if role == "reflector":
        await runner.reflect(request)
    else:
        await runner.edit(request)
    assert len(captured) == 3
    counted = []

    def count(body):
        counted.append(json.loads(json.dumps(body)))
        return 42

    result = measure_chat_scenario(
        runner.section,
        role=role,
        request_style=style,
        writable_paths=("skills/**",),
        page_chars=len(page["content"]),
        prompt_text=runner.prompt.text(),
        brief=brief,
        history_tools=False,
        traces=True,
        page=page,
        page_id=page_id,
        count_tokens=count,
    )
    assert counted[:2] == captured[1:]
    late = result["after_page"]
    assert late["compacted"] and late["protected_exchange_chars"] > 100
    assert late["input_tokens"] == 42
    assert (
        len(json.dumps(captured[2], ensure_ascii=False, separators=(",", ":")).encode())
        == late["serialized_bytes"]
    )
    assert page["content"] not in json.dumps(result)


@pytest.mark.parametrize("role", ["editor", "reflector"])
@pytest.mark.parametrize("style", ["modern", "modern_single", "modern_auto", "legacy"])
def test_bundle_only_scenario_measures_wire_style_and_can_reach_compaction(role, style):
    from evolution.tree.roles.context_preflight import measure_chat_scenario

    section = editor_tests._section(context_compact_chars=20000, context_message_max_chars=1000)
    options = dict(role=role, request_style=style, writable_paths=("skills/**",), page_chars=4000)
    result = measure_chat_scenario(section, **options)
    assert result == measure_chat_scenario(section, **options)
    assert result["first"]["protected_exchange_chars"] == 0
    assert result["after_page"]["compacted"] is False
    assert result["after_compaction"]["compacted"] is True
    assert result["first"]["input_tokens"] is None
    assert result["first"]["tool_count"] > 1
    assert result["first"]["serialized_bytes"] >= result["first"]["serialized_chars"]
    captured = []
    measure_chat_scenario(section, **options, count_tokens=lambda body: captured.append(body) or 0)
    first = captured[0]
    if style == "legacy":
        assert first["function_call"] == "auto" and "tools" not in first
    else:
        assert first["tool_choice"] == ("auto" if style == "modern_auto" else "required")
        assert ("parallel_tool_calls" in first) == (style == "modern")
    if role == "reflector":
        assert result["first"]["components"]["tools"] > 0


def test_manifest_scenario_uses_role_scope_prompts_and_wire_reserve(tmp_path):
    from evolution.tree import measure_manifest_chat_scenario
    from tests.family15_fakes import v16_document
    from tests.tree.manifest.test_manifest_v15 import chat_roles_document, load

    document = chat_roles_document(v16_document())
    document["artifacts"]["writable_paths"] = ["skills/**"]
    document["campaign"]["editor_context"] = "editor-only-context"
    document["campaign"]["reflector_context"] = "reflector-only-context"
    manifest = load(tmp_path, document)
    for role in ("editor", "reflector"):
        seen = []
        rows = measure_manifest_chat_scenario(
            manifest,
            role=role,
            request_style="modern_single",
            page_chars=10**99,
            max_output_tokens=7222,
            model="scenario-model",
            seed_files=["skills/file.yml", "hidden/file.yml"],
            count_tokens=lambda body, seen=seen: seen.append(json.loads(json.dumps(body))) or 35,
        )
        assert list(rows) == ["first"]
        assert rows["first"]["provenance"] == "synthetic"
        assert seen[0]["model"] == "scenario-model"
        assert seen[0]["max_tokens"] == rows["first"]["max_output_tokens"] == 7222
        assert role + "-only-context" in seen[0]["messages"][1]["content"]
        assert "synthetic_page_size_limit" in rows["first"]["limitations"]
        if role == "editor":
            assert "edit_yaml" in [t["function"]["name"] for t in seen[0]["tools"]]


def test_grouper_scenario_uses_one_bounded_map_portion(tmp_path):
    from evolution.tree import measure_manifest_chat_scenario
    from tests.family15_fakes import v16_document
    from tests.tree.manifest.test_manifest_v15 import chat_roles_document, load

    manifest = load(tmp_path, chat_roles_document(v16_document()))
    bodies = []
    rows = measure_manifest_chat_scenario(
        manifest,
        role="grouper",
        request_style="legacy",
        count_tokens=lambda body: bodies.append(body) or 97,
    )
    first = rows["first"]
    assert first["input_tokens"] == 97
    assert first["portion_observations"] == 1
    assert first["portion_input_chars"] <= first["portion_budget_chars"]
    assert "synthetic_grouping_observation" in first["limitations"]
    assert "finish_portion" in [tool["name"] for tool in bodies[0]["functions"]]
    assert "scenario-observation" not in bodies[0]["messages"][1]["content"]


def test_scenario_reflector_page_respects_the_native_reader_and_has_a_citable_id(tmp_path):
    from evolution.tree import measure_manifest_chat_scenario
    from tests.family15_fakes import v16_document
    from tests.tree.manifest.test_manifest_v15 import chat_roles_document, load

    manifest = load(tmp_path, chat_roles_document(v16_document()))
    bodies = []
    rows = measure_manifest_chat_scenario(
        manifest,
        role="reflector",
        request_style="modern",
        page_chars=32222,
        count_tokens=lambda body: bodies.append(body) or 1,
    )
    page = json.loads(bodies[1]["messages"][-1]["content"])
    assert len(page["content"]) == rows["after_page"]["page_chars"] == 16000
    assert rows["after_page"]["requested_page_chars"] == 32222
    assert page["next_offset"] == 16000
    assert "obs:scenario:1" in bodies[0]["messages"][1]["content"]
    assert "reflector_page_limit" in rows["after_page"]["limitations"]
