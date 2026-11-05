"""Golden transcripts of the chat Reflector's tool loop.

Each scenario runs one scripted reflection and records every request body the
provider received, every private `AttemptLog` record and the result. The
fixtures under `tests/fixtures/chat_loop/reflector/` were generated from the
Reflector's own loop before it moved onto `roles/chat_loop.run_tool_loop`, so
the shared runner is held to the same bytes: request messages and tools, log
records and outcomes. Only the run's temporary directory and attempt durations
are normalized.

Set `EVOLUTION_REGEN_CHAT_LOOP_GOLDENS=1` to rewrite the fixtures; a reviewer
should then see no diff unless a change of bytes was intended.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.prompts import TREE_REFLECTOR_SYSTEM
from evolution.tree.roles.editor.chat_editor import ChatCredentials
from evolution.tree.roles.reflector.chat_reflector import ChatReflector
from evolution.tree.roles.reflector_contracts import ProbeAccess, ReflectRequest
from evolution.tree.roles.seals import seal_prompt
from tests.test_tree_adaptive_reflector import group
from tests.test_tree_chat_role_retry import RETRY, _server_error, _unauthorized
from tests.tree.roles.editor.test_chat_editor import _legacy_response
from tests.tree.roles.reflector.test_chat_reflector import (
    SECRET,
    _group,
    _obs_id,
    _prose,
    _response,
    _section,
    reflect_request,  # noqa: F401 - re-used fixture
)
from tests.tree.roles.reflector.test_chat_reflector_repair import (
    CROSS_CASE,
    NEEDLE,
    VALID,
    adaptive,  # noqa: F401 - re-used fixture
    finish,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "chat_loop" / "reflector"
REGENERATE = os.environ.get("EVOLUTION_REGEN_CHAT_LOOP_GOLDENS") == "1"


class _CapturingClient:
    """Pops one scripted item per request and keeps the request as it was sent.

    The loop mutates its message list after a request (and compaction rewrites
    it in place), so each body is copied through JSON at the moment of the call.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.bodies: list[Any] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.bodies.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=repr)))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        item.model = kwargs["model"]
        return item


def _bad_json() -> Any:
    call = SimpleNamespace(
        id="call", type="function", function=SimpleNamespace(name="list_evidence", arguments="{")
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])


def _two_calls() -> Any:
    calls = [
        SimpleNamespace(
            id=f"call-{index}",
            type="function",
            function=SimpleNamespace(name=name, arguments="{}"),
        )
        for index, name in enumerate(("list_evidence", "list_files"))
    ]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls))])


def _reflector(
    tmp_path: Path,
    client: _CapturingClient,
    *,
    bounds_repair: bool = False,
    result_repair: bool = False,
    role_resilience: bool = False,
    **section: Any,
) -> ChatReflector:
    if role_resilience:
        section.setdefault("retry", RETRY)
    return ChatReflector(
        section=_section(**section),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM),
        log_root=tmp_path / "run" / "reflector-logs",
        secrets=(SECRET,),
        client_factory=lambda _section, _credentials: client,
        bounds_repair=bounds_repair,
        result_repair=result_repair,
        role_resilience=role_resilience,
    )


def _normalize(value: Any, tmp_path: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: 0.0 if key == "duration_seconds" else _normalize(item, tmp_path)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize(item, tmp_path) for item in value]
    if isinstance(value, str):
        for root in {str(tmp_path.resolve()), str(tmp_path)}:
            value = value.replace(root, "<tmp>")
        return value
    return value


def _transcript(
    tmp_path: Path,
    reflector: ChatReflector,
    client: _CapturingClient,
    request: ReflectRequest,
    result: Any,
) -> dict[str, Any]:
    path = Path(reflector.log_root) / f"{request.reflection_id}.jsonl"
    log = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    outcome = json.loads(json.dumps(dataclasses.asdict(result), default=repr))
    assert not client.script, "every scripted item must be consumed"
    return _normalize({"requests": _deltas(client.bodies), "log": log, "result": outcome}, tmp_path)


def _deltas(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each request body with its messages as a delta over the previous body's.

    `messages_after` counts the leading messages equal to the previous request's;
    `messages` holds the rest. Repeated `tools` are shown as `"same"`. Every
    byte of every request is still determined by the fixture.
    """

    encoded: list[dict[str, Any]] = []
    previous: dict[str, Any] = {"messages": [], "tools": None}
    for body in bodies:
        messages = body["messages"]
        after = 0
        while (
            after < min(len(messages), len(previous["messages"]))
            and messages[after] == previous["messages"][after]
        ):
            after += 1
        entry = {**body, "messages_after": after, "messages": messages[after:]}
        if "tools" in body and body["tools"] == previous.get("tools"):
            entry["tools"] = "same"
        encoded.append(entry)
        previous = body
    return encoded


def _check(name: str, transcript: dict[str, Any]) -> None:
    fixture = FIXTURES / f"{name}.json"
    text = json.dumps(transcript, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if REGENERATE:
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(text, encoding="utf-8")
    assert SECRET not in text
    assert json.loads(fixture.read_text(encoding="utf-8")) == transcript


async def _run(
    tmp_path: Path,
    name: str,
    request: ReflectRequest,
    script: list[Any],
    **options: Any,
) -> dict[str, Any]:
    client = _CapturingClient(script)
    reflector = _reflector(tmp_path, client, **options)
    result = await reflector.reflect(request)
    transcript = _transcript(tmp_path, reflector, client, request, result)
    _check(name, transcript)
    return transcript


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


async def test_finish(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    obs = _obs_id(reflect_request)
    transcript = await _run(
        tmp_path,
        "finish",
        reflect_request,
        [
            _response("list_files"),
            _response("list_files"),
            _response("list_evidence"),
            _response("read_evidence", {"evidence_id": obs}),
            _response("read_file", {"path": "skills/SKILL.md"}),
            _response("finish_reflection", {"groups": [_group(obs)]}),
        ],
    )
    assert transcript["result"]["status"] == "reflected"


async def test_protocol_repair(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "protocol_repair",
        reflect_request,
        [
            _response("list_files"),
            _prose("thinking first"),
            _response("no_such_tool"),
            _bad_json(),
            _two_calls(),
            _response("read_file", {"path": "skills/SKILL.md", "extra": 1}),
            _response("finish_reflection", {"groups": []}),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        max_protocol_repairs=6,
    )
    phases = [line["phase"] for line in transcript["log"]]
    assert phases.count("protocol_repair_requested") == 5
    assert "parallel_calls_dropped" in phases
    assert transcript["result"]["status"] == "declined"


async def test_protocol_repair_limit(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "protocol_repair_limit",
        reflect_request,
        [_response("list_files"), _prose(), _prose(), _prose()],
    )
    assert transcript["result"]["code"] == "protocol"


async def test_semantic_correction(
    tmp_path: Path,
    adaptive: Any,  # noqa: F811 - the imported fixture, used by name
) -> None:
    bad = group(evidence_ids=["obs:c:1"], affected_cases=[adaptive.handle])
    oversized = group(evidence_ids=["obs:c:1"], affected_cases=["c"], why_this_layer="x" * 600)
    transcript = await _run(
        tmp_path,
        "semantic_correction",
        adaptive(),
        [
            _response("list_files"),
            _response("list_evidence"),
            _response("read_evidence", {"evidence_id": "obs:c:1"}),
            finish(bad),
            finish(CROSS_CASE),
            finish(oversized),
            _response("finish_reflection", {"groups": [{**VALID, "notes": ""}]}),
        ],
        bounds_repair=True,
        result_repair=True,
        max_turns=8,
    )
    phases = [line["phase"] for line in transcript["log"]]
    assert phases.count("finish_reflection_rejected") == 3
    assert phases.count("protocol_repair_requested") == 1
    assert transcript["result"]["status"] == "reflected"


async def test_semantic_turns_exhausted(
    tmp_path: Path,
    adaptive: Any,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "semantic_turns_exhausted",
        adaptive(),
        [_response("list_files"), finish(CROSS_CASE), finish(CROSS_CASE)],
        bounds_repair=True,
        result_repair=True,
        max_turns=3,
    )
    assert transcript["result"]["code"] == "turns_exhausted"
    assert transcript["result"]["rejection"] is not None


async def test_leak_ends_without_correction(
    tmp_path: Path,
    adaptive: Any,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "leak",
        adaptive(),
        [_response("list_files"), finish({**VALID, "root_cause": NEEDLE})],
        bounds_repair=True,
        result_repair=True,
        max_turns=8,
    )
    assert transcript["result"]["status"] == "reflected"


async def test_bounds_without_repair(
    tmp_path: Path,
    adaptive: Any,  # noqa: F811 - the imported fixture, used by name
) -> None:
    oversized = group(evidence_ids=["obs:c:1"], affected_cases=["c"], why_this_layer="x" * 600)
    transcript = await _run(
        tmp_path,
        "bounds_without_repair",
        adaptive(),
        [_response("list_files"), finish(oversized)],
    )
    assert transcript["result"]["code"] == "invalid_result"


async def test_tool_failures_blocked(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    missing = _response("read_file", {"path": "skills/MISSING.md"})
    transcript = await _run(
        tmp_path,
        "tool_failures_blocked",
        reflect_request,
        [
            _response("list_files"),
            missing,
            missing,
            missing,
            _response("list_evidence"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        max_consecutive_tool_failures=2,
        max_turns=10,
    )
    last = [body["tools"] for body in transcript["requests"] if body["tools"] != "same"][-1]
    assert "read_file" not in [tool["function"]["name"] for tool in last]
    assert transcript["result"]["status"] == "declined"


async def test_compaction(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    obs = _obs_id(reflect_request)
    transcript = await _run(
        tmp_path,
        "compaction",
        reflect_request,
        [
            _response("list_files"),
            _response("list_files"),
            _response("list_evidence"),
            _response("read_evidence", {"evidence_id": obs}),
            _response("read_file", {"path": "skills/SKILL.md"}),
            _response("list_evidence"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        context_compact_chars=4_000,
        context_message_max_chars=200,
        max_turns=10,
    )
    contents = [
        message.get("content") or ""
        for body in transcript["requests"]
        for message in body["messages"]
    ]
    assert any(body["messages_after"] < 3 for body in transcript["requests"][2:])
    assert any("Earlier editor context was compacted" in text for text in contents)


async def test_provider_retry(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "provider_retry",
        reflect_request,
        [
            _response("list_files"),
            _server_error(),
            _response("list_evidence"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        role_resilience=True,
    )
    assert transcript["result"]["status"] == "declined"


async def test_provider_rejected(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "provider_rejected",
        reflect_request,
        [_response("list_files"), _unauthorized()],
        role_resilience=True,
    )
    assert "model_request_failed" in [line["phase"] for line in transcript["log"]]
    assert transcript["result"]["code"] == "provider_rejected"


async def test_provider_retry_without_policy(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("evolution.tree.roles.editor.chat_editor.asyncio.sleep", no_sleep)
    transcript = await _run(
        tmp_path,
        "provider_retry_without_policy",
        reflect_request,
        [
            _response("list_files"),
            RuntimeError("transient"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )
    assert transcript["result"]["status"] == "declined"


async def test_legacy_preflight(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    transcript = await _run(
        tmp_path,
        "legacy_preflight",
        reflect_request,
        [
            _prose("I will call list_files."),
            _prose("I will call list_files."),
            _legacy_response("list_files"),
            _legacy_response("list_evidence"),
            _prose("no call"),
            _legacy_response("finish_reflection", '{"groups":[],"declined":true}'),
        ],
    )
    assert transcript["requests"][2]["function_call"] == {"name": "list_files"}
    assert transcript["result"]["status"] == "declined"


async def test_probe_context_exhausted(
    tmp_path: Path,
    reflect_request: ReflectRequest,  # noqa: F811 - the imported fixture, used by name
) -> None:
    class Session:
        async def request(self, value: Any) -> Any:
            return {"error": "context_exhausted", "detail": "probe request context exhausted"}

    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 2, ("GET",), None),
        probe_session=Session(),
    )
    probe = _response("probe_request", {"method": "GET", "path": "/x", "body": None})
    transcript = await _run(
        tmp_path,
        "probe_context_exhausted",
        request,
        [
            _response("list_files"),
            probe,
            probe,
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )
    assert transcript["result"]["status"] == "declined"
