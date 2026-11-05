"""The first-request budget, measures, size records and provider detail."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from evolution.tree.roles import reflector_contracts
from evolution.tree.roles import seals as editor_seal
from evolution.tree.roles.editor_contracts import EMPTY_HISTORY
from evolution.tree.roles.reflector.chat_reflector import chat_first_messages, reflect_tools
from evolution.tree.roles.reflector.reflect_aggregate import serialized_chars
from evolution.tree.roles.reflector.reflect_brief import AdaptiveReflectBrief
from evolution.tree.roles.reflector.reflect_budget import (
    COMPONENTS,
    PROBE_URL_PLACEHOLDER,
    RequestBudget,
    bound_reflection_history,
    budget_record,
    chat_request_size,
    write_budget_record,
)
from evolution.tree.roles.reflector_contracts import HISTORY_MAX_CHARS
from tests.tree.roles.reflector.test_chat_reflector import reflect_request as reflect_request
from tests.tree.roles.reflector.test_reflect_history import synthetic


def test_headroom_is_capped_so_a_valid_document_never_refuses_everything() -> None:
    assert RequestBudget(800_000, 64_000).headroom == 128_000
    assert RequestBudget(800_000, 64_000).fold_target == 272_000
    assert RequestBudget(800_000, 64_000).refusal_limit == 672_000
    half = RequestBudget(100_000, 50_000)
    assert half.headroom == 25_000 and half.refusal_limit == 75_000 and half.fold_target == 25_000
    small = RequestBudget(100_000, 1_000)
    assert small.headroom == 2_000


def _brief(history: dict[str, Any]) -> AdaptiveReflectBrief:
    payload = {
        "version": "reflect_brief/5",
        "groups": [],
        "portion": [],
        "families": [],
        "history": history,
        "evidence_catalog": {"entries": [], "total": 0},
    }
    return AdaptiveReflectBrief(
        "d" * 64,
        None,
        EMPTY_HISTORY,
        ("skills/**",),
        {},
        payload,
        {},
        frozenset(),
        None,
        version="reflect_brief/5",
    )


class _Chat:
    """A measure with the chat formula over a fixed system prompt."""

    def __init__(self, budget: RequestBudget, prompt: str = "system " * 50) -> None:
        self.budget, self.prompt = budget, prompt

    def request_budget(self) -> RequestBudget:
        return self.budget

    def measure_first_request(self, brief: AdaptiveReflectBrief, **kwargs: Any) -> Any:
        from evolution.tree.roles.reflector.reflect_budget import chat_first_request

        messages = chat_first_messages(
            self.prompt,
            brief,
            editable_paths=("skills/**",),
            environment_docs=None,
            probe=kwargs.get("probe"),
            editor_max_changed_files=kwargs.get("editor_max_changed_files"),
        )
        tools = reflect_tools(
            probe=kwargs.get("probe") is not None, adaptive=True, docs=False, history=True
        )
        return chat_first_request(
            self.prompt, messages, tools, brief, probe=kwargs.get("probe"), budget=self.budget
        )


def _bound(budget: RequestBudget, rounds: int = 40):
    source = synthetic(rounds, detail_chars=5_000)
    first = _brief({})
    measure = _Chat(budget)

    def measure_history(history: Any) -> Any:
        from evolution.tree.roles.reflector.reflect_brief import with_history

        return measure.measure_first_request(with_history(first, history))

    return bound_reflection_history(source, budget=budget, measure=measure_history)


def test_the_elision_aims_at_the_fold_target_and_sends_or_refuses() -> None:
    roomy_budget = RequestBudget(800_000, 64_000)
    roomy = _bound(roomy_budget)
    assert roomy.decision == "sent" and roomy.below_fold_target is True
    assert serialized_chars(roomy.history) <= HISTORY_MAX_CHARS
    assert roomy.request.total_chars <= roomy_budget.fold_target
    refused = _bound(RequestBudget(2_000, 100))
    assert (
        refused.decision == "refused"
        and refused.layout.form == "core"
        and refused.layout.detail == 0
    )


def test_a_minimum_between_the_fold_target_and_the_limit_is_sent_with_a_flag() -> None:
    minimum = _bound(RequestBudget(2_000, 100)).request.total_chars
    budget = RequestBudget(minimum + 200, 100)  # H = 200: limit == minimum > fold target
    assert budget.fold_target < minimum <= budget.refusal_limit
    bound = _bound(budget)
    assert (bound.decision, bound.below_fold_target, bound.layout.form) == ("sent", False, "core")


def test_the_size_record_names_components_and_no_text(tmp_path: Path) -> None:
    bound = _bound(RequestBudget(2_000, 100))
    record = budget_record(bound, budget=RequestBudget(2_000, 100, 8192))
    assert set(record["components"]) == set(COMPONENTS)
    assert (
        record["decision"] == "refused"
        and record["over_by"] > 0
        and record["largest_component"] in COMPONENTS
    )
    assert record["max_output_tokens"] == 8192
    assert (
        record["detail_rounds"],
        record["summary_rounds"],
        record["aggregated_rounds"],
        record["aggregate_form"],
    ) == (0, 0, 40, "core")
    write_budget_record(tmp_path, "r41-x01", record)
    path = tmp_path / "reflector-logs" / "budget" / "r41-x01.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == record and "pad" not in text and "ssss" not in text


def test_the_chat_tools_offer_history_only_on_request_and_the_schema_moves() -> None:
    names = [t["function"]["name"] for t in reflect_tools(probe=False, adaptive=True, docs=False)]
    assert "list_history" not in names
    names = [
        t["function"]["name"]
        for t in reflect_tools(probe=False, adaptive=True, docs=False, history=True)
    ]
    assert names[-2:] == ["list_history", "read_history"]
    assert reflector_contracts.REFLECT_TOOL_SCHEMA == "reflect_tools/2"


def test_identity_moves_for_chat_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evolution.prompts import TREE_REFLECTOR_SYSTEM
    from evolution.tree.roles import reflection_role_helpers
    from evolution.tree.roles.seals import (
        agent_reflector_identity,
        chat_reflector_identity,
        seal_prompt,
    )
    from tests.tree.roles.reflector.test_agent_reflector import _reflector as agent_reflector
    from tests.tree.roles.reflector.test_chat_reflector import _section
    from tests.tree.roles.reflector.test_subprocess_reflector import DECLINED, _bind_reflector

    prompt = seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM)
    agent, _ = agent_reflector(tmp_path, [])
    subprocess = _bind_reflector(tmp_path, DECLINED)
    now = chat_reflector_identity(_section(), prompt).digest
    before_agent, before_subprocess = agent.identity.digest, subprocess.binding.identity_digest()
    monkeypatch.setattr(editor_seal, "REFLECT_TOOL_SCHEMA", "reflect_tools/1")
    monkeypatch.setattr(
        reflection_role_helpers,
        "_METHODS",
        reflection_role_helpers._METHODS - {"list_history", "read_history"},
    )
    assert chat_reflector_identity(_section(), prompt).digest != now
    assert (
        agent_reflector_identity(agent.section, agent.prompt, agent.program).digest == before_agent
    )
    assert subprocess.binding.identity_digest() == before_subprocess


def test_packaged_reflector_prompts_are_unchanged() -> None:
    import hashlib

    from evolution import prompts

    root = Path(prompts.__file__).parent
    assert (
        hashlib.sha256((root / "tree_reflector_v3.md").read_bytes()).hexdigest()
        == "f61c4132d454adfe263f5d6fecbb32627f4e0fca4e81c43a8095069f2eb33937"
    )
    assert (
        hashlib.sha256((root / "tree_agent_reflector_v3.md").read_bytes()).hexdigest()
        == "71e3f08ca3ef58bc380bf37a7d230c352b9c7b3a1ec1ff3a1d62c66c464b5954"
    )


def test_the_placeholder_is_the_longest_mediator_url() -> None:
    assert PROBE_URL_PLACEHOLDER == "http://127.0.0.1:65535/" + "0" * 32 + "/probe"


@pytest.mark.parametrize("resilience", [False, True])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"error": {"code": "context_length_exceeded", "message": "ECHO-OF-REQUEST-TEXT"}},
            "context_length_exceeded",
        ),
        (
            {"code": "context_length_exceeded", "message": "ECHO-OF-REQUEST-TEXT"},
            "context_length_exceeded",
        ),
        ({"error": {"code": "unsafe code!", "type": "safe_type"}}, "safe_type"),
        ({"error": {"code": "x" * 65}, "code": "safe_fallback"}, "safe_fallback"),
        ({"code": "unsafe code!", "type": "bad type!"}, None),
        (None, None),
    ],
)
async def test_a_provider_400_records_status_and_code_but_no_text(
    tmp_path: Path, reflect_request, resilience: bool, body: Any, expected: str | None
) -> None:
    from dataclasses import replace

    from evolution.tree.roles.role_call import RoleCallRetry
    from tests.tree.roles.reflector.test_chat_reflector import (
        _Client,
        _log_lines,
        _reflector,
        _response,
    )
    from tests.tree.roles.test_role_call import POLICY

    error = openai.BadRequestError(
        "bad request",
        response=httpx.Response(
            400, request=httpx.Request("POST", "https://gw.example/v1/chat/completions")
        ),
        body=body,
    )

    class Client(_Client):
        async def create(self, **kwargs: Any) -> Any:
            if self.calls:
                self.calls.append(kwargs)
                raise error
            return await super().create(**kwargs)

    client = Client([_response("list_files")])
    reflector, _ = _reflector(tmp_path, [])
    reflector = replace(
        reflector,
        client_factory=lambda *args: client,
        role_resilience=resilience,
        section=replace(
            reflector.section, retry=RoleCallRetry.from_payload(POLICY) if resilience else None
        ),
    )
    result = await reflector.reflect(reflect_request)
    assert result.status == "failed"
    failed = [row for row in _log_lines(reflector) if row.get("phase") == "model_request_failed"]
    assert failed[-1]["http_status"] == 400
    assert failed[-1].get("provider_error_code") == expected
    assert "ECHO-OF-REQUEST-TEXT" not in json.dumps(_log_lines(reflector))


@pytest.mark.parametrize("family15", [False, True])
async def test_history_tools_dispatch_and_the_first_chat_measure_equals_the_sent_request(
    tmp_path: Path,
    family15: bool,
) -> None:
    from evolution.tree.roles.reflection_reader import ByteLruCache, FrozenReflectionView
    from evolution.tree.roles.reflector_contracts import ReflectRequest
    from tests.tree.roles.reflector.test_chat_reflector import _reflector, _response, _seed
    from tests.tree.roles.reflector.test_reflect_history_reader import _reader, _world

    world = _world(tmp_path / "world", rounds=1)
    from dataclasses import replace

    from tests.campaign_fakes import corpus

    cases = [
        replace(case, case_id=case_id) for case, case_id in zip(corpus(4), world.cases, strict=True)
    ]
    history_reader = _reader(world, corpus=cases)
    expected = history_reader.read_history({"history_id": "attempt:r01-a01"})
    assert all(case["repeats"] for case in json.loads(expected["content"])["result"]["cases"])
    brief = _brief(_bound(RequestBudget(800_000, 64_000), rounds=0).history)
    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("list_history"),
            _response("read_history", {"history_id": "attempt:r01-a01"}),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )
    import copy

    from tests.tree.roles.reflector.test_chat_reflector import _Client

    class CapturingClient(_Client):
        async def create(self, **kwargs: Any) -> Any:
            response = await super().create(**kwargs)
            self.calls[-1] = copy.deepcopy(kwargs)
            return response

    client = CapturingClient(client._responses)
    from tests.test_tree_chat_role_retry import RETRY

    reflector = replace(
        reflector,
        client_factory=lambda *args: client,
        family15=family15,
        role_resilience=family15,
        section=replace(reflector.section, retry=RETRY if family15 else None),
    )
    view = FrozenReflectionView(history_reader.handlers(), body_cache=ByteLruCache())
    with view.open_operation("reflect-history", set(history_reader.handlers())) as reader:
        request = ReflectRequest(
            "r01-x01", 1, _seed(tmp_path), brief, None, None, ("skills/**",), 30, reader=reader
        )
        measured = reflector.measure_first_request(
            brief, editable_paths=request.editable_paths, probe=None, editor_max_changed_files=None
        )
        result = await reflector.reflect(request)
        assert result.status == "declined", result.note
        first = client.calls[1]
        assert measured.total_chars == chat_request_size(first["messages"], first["tools"])
        assert {tool["function"]["name"] for tool in first["tools"]} >= {
            "list_history",
            "read_history",
        }
        assert any(
            message.get("role") == "tool" and json.loads(message["content"]) == expected
            for message in client.calls[-1]["messages"]
            if message.get("role") == "tool"
        )
        assert any(
            entry["operation"] == "read_history" and entry["history_id"] == "attempt:r01-a01"
            for entry in reader.summary.entries
        )


async def test_role_guard_precedes_preflight(tmp_path: Path) -> None:
    from dataclasses import replace

    from evolution.tree.roles.reflection_reader import ByteLruCache, FrozenReflectionView
    from evolution.tree.roles.reflector_contracts import ReflectRequest
    from tests.tree.roles.reflector.test_chat_reflector import _reflector, _response, _seed

    reflector, client = _reflector(tmp_path, [_response("list_files")])
    reflector = replace(
        reflector,
        section=replace(
            reflector.section, context_compact_chars=2_000, context_message_max_chars=100
        ),
    )
    view = FrozenReflectionView({"list_history": lambda arguments: {}}, body_cache=ByteLruCache())
    with view.open_operation("guard", {"list_history"}) as reader:
        result = await reflector.reflect(
            ReflectRequest(
                "r01-x01",
                1,
                _seed(tmp_path),
                _brief(_bound(RequestBudget(800_000, 64_000), rounds=0).history),
                None,
                None,
                ("skills/**",),
                30,
                reader=reader,
            )
        )
        assert result.status == "failed"
        assert client.calls == []
        from tests.tree.roles.reflector.test_chat_reflector import _log_lines

        assert any(
            row.get("phase") == "exception"
            and row.get("error") == "first request exceeds its refusal limit"
            for row in _log_lines(reflector)
        )
