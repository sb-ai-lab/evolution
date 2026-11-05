"""`ChatReflector`, the `chat` `CandidateReflector`.

The doubles are `tests/tree/roles/editor/test_chat_editor.py`'s: a scripted client handed to
the reflector through `client_factory`, so the tool loop, the evidence catalog
and the transcript are real and only the provider is fake. What differs from
the editor's own tests is the whole point of the kind -- five read-only tools,
a terminal that carries claimed diagnosis groups rather than a published tree,
and a workspace the reflection must leave exactly as it found it.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution.prompts import TREE_REFLECTOR_SYSTEM
from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.editor.chat_editor import ChatCredentials
from evolution.tree.roles.editor_contracts import EMPTY_HISTORY
from evolution.tree.roles.environment_docs import EnvironmentDocs
from evolution.tree.roles.reflector.chat_reflector import ChatReflector, reflect_tools
from evolution.tree.roles.reflector.reflect_brief import (
    build_reflect_brief,
    catalog_case_ids,
    render_reflect_catalog,
)
from evolution.tree.roles.reflector.reflect_evidence import select_focus
from evolution.tree.roles.reflector_contracts import (
    GroupError,
    ProbeAccess,
    ReflectRequest,
    validate_groups,
)
from evolution.tree.roles.seals import chat_reflector_identity, seal_prompt
from evolution.tree.roles.workspace import discard, freeze_projection, project
from tests.tree.roles.reflector.test_reflect_brief import _CHAMPION, _corpus
from tests.tree.roles.reflector.test_reflect_evidence import _snapshot, rep

SECRET = "sekret-key-value"


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


def _response(name: str, arguments: dict[str, Any] | None = None) -> Any:
    call = SimpleNamespace(
        id="call",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))])


def _prose(content: str = "no tool") -> Any:
    message = SimpleNamespace(tool_calls=None, function_call=None, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _Client:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # The script loops, as the fake HTTP endpoint's does, so a test about a
        # turn budget never runs out of responses before the budget runs out.
        response = self._responses[(len(self.calls) - 1) % len(self._responses)]
        response.model = kwargs["model"]
        return response


def _section(**kw: Any) -> ChatEditorSection:
    kw.setdefault("base_url", "https://gw.example/v1")
    return ChatEditorSection(model="m", api_key_env="K", **kw)


def _reflector(tmp_path: Path, responses: list[Any], **kw: Any) -> tuple[ChatReflector, _Client]:
    client = _Client(responses)
    bounds_repair = kw.pop("bounds_repair", False)
    result_repair = kw.pop("result_repair", False)
    reflector = ChatReflector(
        section=_section(**kw),
        credentials=ChatCredentials(SECRET, None),
        prompt=seal_prompt(None, packaged=TREE_REFLECTOR_SYSTEM),
        log_root=tmp_path / "run" / "reflector-logs",
        secrets=(SECRET,),
        client_factory=lambda section, credentials: client,
        bounds_repair=bounds_repair,
        result_repair=result_repair,
    )
    return reflector, client


# --------------------------------------------------------------------------
# The request: a frozen read-only projection and one reflect brief
# --------------------------------------------------------------------------


def _evidence():
    reps = (
        rep("case-01", 1, failed=("answers",)),
        rep("case-01", 2),
        rep("case-02", 1),
    )
    focus = select_focus(reps, snapshot=_snapshot({}))
    return build_reflect_brief(
        focus=focus,
        repeats=reps,
        corpus=_corpus(),
        champion_digest=_CHAMPION,
        operator_context="operator says hello",
        history=EMPTY_HISTORY,
        editable_paths=("skills/**",),
    )


def _seed(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / "skills").mkdir(parents=True, exist_ok=True)
    (seed / "skills" / "SKILL.md").write_text("seed\n", encoding="utf-8")
    return seed


@pytest.fixture
def reflect_request(tmp_path: Path):
    tmp_root = tmp_path / "run" / ".reflections"
    tmp_root.mkdir(parents=True, exist_ok=True)
    projection = project(_seed(tmp_path), tmp_root=tmp_root)
    freeze_projection(projection)
    try:
        yield ReflectRequest(
            reflection_id="r01-x01",
            round=1,
            workspace=projection.root,
            evidence=_evidence(),
            environment_docs=None,
            probe=None,
            editable_paths=("skills/**",),
            deadline_seconds=30.0,
        )
    finally:
        discard(projection)


def _obs_id(request: ReflectRequest) -> str:
    catalog = render_reflect_catalog(request.evidence)
    return next(record.evidence_id for record in catalog if record.kind == "observation")


def _group(evidence_id: str) -> dict[str, Any]:
    return {
        "root_cause": "the skill never states the required output shape",
        "suggested_fix": "state the output shape in SKILL.md",
        "evidence_ids": [evidence_id],
        "affected_cases": ["case-01"],
        "target_paths": ["skills/SKILL.md"],
    }


def _listing(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(root).as_posix(): (stat.S_IMODE(path.lstat().st_mode), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _log_lines(reflector: ChatReflector, reflection_id: str = "r01-x01") -> list[dict[str, Any]]:
    path = Path(reflector.log_root) / f"{reflection_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------------------
# Identity and tools
# --------------------------------------------------------------------------


def test_identity_is_the_declared_configuration(tmp_path: Path) -> None:
    reflector, _client = _reflector(tmp_path, [])

    assert reflector.identity == chat_reflector_identity(reflector.section, reflector.prompt)
    assert reflector.identity.kind == "chat"


def test_the_tools_are_the_five_read_only_ones() -> None:
    """Without a probe the terminal offers exactly the five keys the gate requires.

    Offering an optional `probe_refs` unconditionally would conflict with
    `validate_groups`. The campaign calls
    `validate_groups(..., probe_seqs=None)`, which requires exactly the five
    non-probe keys -- so a provider-conforming `probe_refs: []` became
    `reflection_failed/invalid_result`.
    """

    tools = reflect_tools(probe=False)
    names = [tool["function"]["name"] for tool in tools]

    assert names == [
        "list_files",
        "read_file",
        "list_evidence",
        "read_evidence",
        "finish_reflection",
    ]
    finish = tools[-1]["function"]["parameters"]
    group = finish["properties"]["groups"]["items"]
    assert finish["required"] == ["groups"]
    assert set(group["required"]) == {
        "root_cause",
        "suggested_fix",
        "evidence_ids",
        "affected_cases",
        "target_paths",
    }
    assert "probe_refs" not in group["properties"]
    assert set(group["properties"]) == set(group["required"])

    with_probe = reflect_tools(probe=True)[-1]["function"]["parameters"]
    probed_group = with_probe["properties"]["groups"]["items"]
    assert probed_group["properties"]["probe_refs"] == {
        "type": "array",
        "items": {"type": "integer"},
    }
    assert "probe_refs" not in probed_group["required"]


#: every adaptive `reflect_tools` form (probe x docs x history), digested
#: in that order at `8f7ed0407`, before the round limit reached the tools: a
#: `/15` round without the key (512) keeps these bytes, and so the budget
#: record's `tools` size and the history-fit margin that read them.
V15_ADAPTIVE_TOOLS_SHA256 = "5c43a0b50e004769f062eaeaa5b07e46b0dd84fb01a5bc83bf2b85e87903bd66"


def _adaptive_tools_digest(**limit: Any) -> str:
    import hashlib
    import itertools

    from evolution.tree.execution.journal_identity import canonical_json

    digest = hashlib.sha256()
    for probe, docs, history in itertools.product([False, True], repeat=3):
        digest.update(
            canonical_json(
                reflect_tools(probe=probe, adaptive=True, docs=docs, history=history, **limit)
            )
        )
    return digest.hexdigest()


def _adaptive_group_schema(limit: int | None) -> dict[str, Any]:
    terminal = next(
        tool["function"]
        for tool in reflect_tools(probe=False, adaptive=True, group_text_chars=limit)
        if tool["function"]["name"] == "finish_reflection"
    )
    return terminal["parameters"]["properties"]["groups"]["items"]


def test_an_absent_v15_limit_keeps_the_adaptive_tool_bytes() -> None:
    assert _adaptive_tools_digest() == V15_ADAPTIVE_TOOLS_SHA256
    assert _adaptive_tools_digest(group_text_chars=512) == V15_ADAPTIVE_TOOLS_SHA256
    description = _adaptive_group_schema(512)["properties"]["evidence_ids"]["description"]
    assert description == "At most 64 distinct observation IDs."


@pytest.mark.parametrize("limit", [4096, 900, None])
def test_the_tool_schema_states_the_round_limit(limit: int | None) -> None:
    from evolution.tree.roles.reflector_contracts import ADAPTIVE_GROUP_TEXT_KEYS

    schema = _adaptive_group_schema(limit)
    for key in ADAPTIVE_GROUP_TEXT_KEYS:
        expected = {"type": "string"} if limit is None else {"type": "string", "maxLength": limit}
        assert schema["properties"][key] == expected
    # The bound counts every listed id: "distinct" leaves with the old limit.
    assert schema["properties"]["evidence_ids"]["description"] == "At most 64 observation IDs."
    assert _adaptive_tools_digest(group_text_chars=limit) != V15_ADAPTIVE_TOOLS_SHA256


# --------------------------------------------------------------------------
# The three terminals
# --------------------------------------------------------------------------


async def test_reflected_returns_the_claimed_group_raw_and_writes_a_redacted_transcript(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """The kind proves nothing about a group: the campaign owns `validate_groups`."""

    obs = _obs_id(reflect_request)
    group = _group(obs)
    before = _listing(reflect_request.workspace)
    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("read_evidence", {"evidence_id": obs}),
            _response("finish_reflection", {"groups": [group]}),
        ],
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "reflected"
    assert result.groups == (group,)
    assert _listing(reflect_request.workspace) == before, "a reflection writes nothing"
    lines = _log_lines(reflector)
    read = next(line for line in lines if line.get("tool") == "read_evidence")
    assert read["result"]["id"] == obs
    transcript = (Path(reflector.log_root) / "r01-x01.jsonl").read_text(encoding="utf-8")
    assert SECRET not in transcript
    assert [call["model"] for call in client.calls] == ["m"] * len(client.calls)


async def test_an_empty_probe_refs_without_a_probe_reaches_the_campaign_dropped(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """A key that claims nothing must not fail the whole reflection.

    A model that
    writes `probe_refs: []` anyway claims no probe, so the key is dropped at
    the terminal and the group passes the campaign's one gate; a *non-empty*
    one is a claim about probes that did not run and is carried through raw,
    for `validate_groups` to refuse.
    """

    obs = _obs_id(reflect_request)
    catalog = render_reflect_catalog(reflect_request.evidence)
    reflector, _client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("finish_reflection", {"groups": [{**_group(obs), "probe_refs": []}]}),
        ],
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "reflected"
    assert result.groups == (_group(obs),)
    assert "probe_refs" not in result.groups[0]
    validated = validate_groups(
        result.groups,
        catalog_ids=catalog_case_ids(catalog),
        case_ids={"case-01", "case-02"},
        editable_paths=reflect_request.editable_paths,
        probe_seqs=None,
        needles=(),
    )
    assert validated[0].probe_refs == ()

    claiming, _client = _reflector(
        tmp_path / "claim",
        [
            _response("list_files"),
            _response("finish_reflection", {"groups": [{**_group(obs), "probe_refs": [1]}]}),
        ],
    )

    claimed = await claiming.reflect(reflect_request)

    assert claimed.groups[0]["probe_refs"] == [1]
    with pytest.raises(GroupError):
        validate_groups(
            claimed.groups,
            catalog_ids=catalog_case_ids(catalog),
            case_ids={"case-01", "case-02"},
            editable_paths=reflect_request.editable_paths,
            probe_seqs=None,
            needles=(),
        )


async def test_declined_when_finish_reflection_claims_nothing(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, _client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("list_evidence"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )

    result = await reflector.reflect(reflect_request)

    assert (result.status, result.groups) == ("declined", ())


async def test_turns_exhausted_when_the_loop_never_finishes(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, client = _reflector(tmp_path, [_response("list_files")], max_turns=3)

    result = await reflector.reflect(reflect_request)

    assert result == type(result)("failed", (), "turns_exhausted", code="turns_exhausted")
    # The budget is provider turns, preflight included: three requests, then the
    # fourth turn is refused before anything is sent.
    assert len(client.calls) == 3


async def test_the_reflector_compacts_at_its_section_trigger(
    tmp_path: Path, reflect_request: ReflectRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.reflector import chat_reflector

    seen: list[tuple[int, int]] = []

    def spy(messages: list[dict[str, Any]], *, trigger_chars: int, message_max_chars: int) -> None:
        seen.append((trigger_chars, message_max_chars))

    monkeypatch.setattr(chat_reflector, "_compact_messages", spy)
    reflector, _client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("list_evidence"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        context_compact_chars=400_000,
        context_message_max_chars=60_000,
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "declined"
    assert seen and set(seen) == {(400_000, 60_000)}


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


async def test_probe_calls_are_native_and_errors_do_not_block_tools(tmp_path, reflect_request):
    from dataclasses import replace

    token = "unique-capability-" + "q" * 32
    access = ProbeAccess(f"http://127.0.0.1:9/{token}/probe", 4, ("GET",), None)

    class Session:
        calls = []

        async def request(self, value):
            self.calls.append(value)
            return {"error": "transport", "detail": access.url}

    session = Session()
    request = replace(reflect_request, probe=access, probe_session=session)
    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            *[
                _response("probe_request", {"method": "GET", "path": "/x", "body": None})
                for _ in range(4)
            ],
            _response("read_evidence", {"evidence_id": _obs_id(request)}),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        max_turns=2,
    )
    result = await reflector.reflect(request)
    assert result.status == "declined", result.note
    assert len(session.calls) == 4
    assert len(client.calls) == 7
    assert "probe_request" in {tool["function"]["name"] for tool in client.calls[-1]["tools"]}
    log = (reflector.log_root / (request.reflection_id + ".jsonl")).read_text()
    assert token not in log and access.url not in log and "probe_ignored" not in log
    opening = client.calls[-1]["messages"][1]["content"]
    assert "HTTP" in opening and "affected_cases" in opening
    assert access.url not in opening


async def test_probe_context_exhaustion_keeps_evidence_and_finish(tmp_path, reflect_request):
    from dataclasses import replace

    class Session:
        calls = 0

        async def request(self, value):
            self.calls += 1
            return {"error": "context_exhausted", "detail": "probe request context exhausted"}

    session = Session()
    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 2, ("GET",), None),
        probe_session=session,
    )
    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("probe_request", {"method": "GET", "path": "/x", "body": None}),
            _response("probe_request", {"method": "GET", "path": "/x", "body": None}),
            _response("read_evidence", {"evidence_id": _obs_id(request)}),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )
    assert (await reflector.reflect(request)).status == "declined"
    assert session.calls == 1
    names = {tool["function"]["name"] for tool in client.calls[-1]["tools"]}
    assert "probe_request" not in names
    assert {"read_evidence", "finish_reflection"} <= names


async def test_the_opening_carries_the_context_the_editable_paths_and_the_docs(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    request = ReflectRequest(
        reflect_request.reflection_id,
        reflect_request.round,
        reflect_request.workspace,
        reflect_request.evidence,
        # The `chat` kind reads the rendering and nothing else: the snapshot's
        # own files are the `agent_cli` control plane's business.
        EnvironmentDocs("The target exposes /health.", "d" * 64, (("api.md", b"unused"),)),
        None,
        reflect_request.editable_paths,
        reflect_request.deadline_seconds,
    )
    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )

    await reflector.reflect(request)

    conversation = client.calls[-1]["messages"]
    assert conversation[0]["role"] == "system"
    assert conversation[0]["content"] == TREE_REFLECTOR_SYSTEM
    opening = conversation[1]["content"]
    assert "operator says hello" in opening
    assert "skills/**" in opening
    assert "The target exposes /health." in opening


async def test_an_unknown_tool_is_repaired_and_a_missing_declined_flag_too(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    """The editor's protocol repairs, over the Reflector's own tool set."""

    obs = _obs_id(reflect_request)
    reflector, _client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("write_file", {"path": "skills/SKILL.md", "content": "no"}),
            _response("finish_reflection", {"groups": []}),
            _response("finish_reflection", {"groups": [_group(obs)]}),
        ],
    )

    result = await reflector.reflect(reflect_request)

    assert result.status == "reflected"
    repairs = [
        line for line in _log_lines(reflector) if line["phase"] == "protocol_repair_requested"
    ]
    assert [repair["error"] for repair in repairs] == [
        "unknown tool: write_file",
        "finish_reflection without groups must set declined: true",
    ]


async def test_a_protocol_repair_limit_ends_the_reflection(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, _client = _reflector(tmp_path, [_response("list_files"), _prose()], max_turns=16)

    result = await reflector.reflect(reflect_request)

    assert result.status == "failed"
    assert result.note.startswith("protocol ")
    assert result.code == "protocol"


async def test_preflight_that_never_calls_list_files_is_recoded_as_preflight_failed(
    tmp_path: Path, reflect_request: ReflectRequest
) -> None:
    reflector, _client = _reflector(tmp_path, [_prose() for _ in range(5)])

    result = await reflector.reflect(reflect_request)

    assert result.status == "failed"
    assert result.note.startswith("protocol ")
    assert result.code == "preflight_failed"


@pytest.mark.parametrize("configured", [2, 25])
async def test_probe_turn_ceiling_is_exact_and_does_not_mutate_section(
    tmp_path, reflect_request, configured
):
    from dataclasses import replace

    class Session:
        async def request(self, value):
            return {"error": "budget_exhausted", "detail": "probe budget exhausted"}

    request = replace(
        reflect_request,
        probe=ProbeAccess("http://127.0.0.1:9/" + "q" * 32 + "/probe", 4, ("GET",), None),
        probe_session=Session(),
    )
    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("probe_request", {"method": "GET", "path": "/x", "body": None}),
        ],
        max_turns=configured,
    )
    result = await reflector.reflect(request)
    assert result.status == "failed" and result.note == "turns_exhausted"
    assert result.code == "turns_exhausted"
    assert len(client.calls) == max(configured, 16)
    assert reflector.section.max_turns == configured


def test_the_chat_reflector_offers_read_trace_only_in_a_pinned_round():
    legacy = reflect_tools(probe=False, adaptive=True, history=True)
    pinned = reflect_tools(probe=False, adaptive=True, history=True, trace=True)
    names = [tool["function"]["name"] for tool in pinned]
    assert names[names.index("read_evidence") + 1] == "read_trace"
    assert [tool for tool in pinned if tool["function"]["name"] != "read_trace"] == legacy
    assert _adaptive_tools_digest() == V15_ADAPTIVE_TOOLS_SHA256


def test_pinned_probe_body_accepts_any_json_value():
    from evolution.tree.roles.reflector.chat_reflector import reflect_tools

    tools = reflect_tools(probe=True, typed_body=True)
    probe = next(tool["function"] for tool in tools if tool["function"]["name"] == "probe_request")
    assert probe["parameters"]["properties"]["body"] == {
        "type": ["object", "array", "string", "number", "boolean", "null"],
        "description": "the JSON value to send as the request body; not text that contains JSON",
    }


@pytest.mark.parametrize("contracts", [False, True])
def test_case_id_schema_description_is_gated(contracts):
    from evolution.tree.roles.reflector.chat_reflector import reflect_tools

    tools = reflect_tools(probe=False, adaptive=True, contracts=contracts)
    schema = next(t for t in tools if t["function"]["name"] == "list_evidence")["function"][
        "parameters"
    ]["properties"]["case_id"]
    assert ("description" in schema) is contracts
    if contracts:
        assert (
            schema["description"]
            == "A case_handle or a case_id from a list_evidence catalog entry."
        )


@pytest.mark.parametrize("family15", [False, True])
async def test_chat_reflector_reader_error_detail_is_family_gated(
    tmp_path, reflect_request, family15
):
    from dataclasses import replace

    from evolution.tree.roles.reflection_reader import (
        ByteLruCache,
        FrozenReflectionView,
        ReaderArgumentError,
    )
    from tests.test_tree_chat_role_retry import RETRY
    from tests.tree.roles.reflector.test_reflect_budget import RequestBudget, _bound, _brief

    def read_error(arguments):
        raise ReaderArgumentError("invalid_offset", "offset must be between 0 and 17")

    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("read_evidence", {"evidence_id": "obs:x", "offset": 100}),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
    )
    reflector = replace(
        reflector,
        family15=family15,
        role_resilience=True,
        section=replace(reflector.section, retry=RETRY),
    )
    view = FrozenReflectionView({"read_evidence": read_error}, body_cache=ByteLruCache())
    with view.open_operation("range", {"read_evidence"}) as reader:
        brief = _brief(_bound(RequestBudget(800_000, 64_000), rounds=0).history)
        request = replace(reflect_request, evidence=brief, reader=reader)
        result = await reflector.reflect(request)
    assert result.status == "declined", result.note
    outputs = [json.loads(m["content"]) for m in client.calls[2]["messages"] if m["role"] == "tool"]
    assert outputs[0]["error"] == (
        "invalid_offset: offset must be between 0 and 17." if family15 else "invalid_offset."
    )


@pytest.mark.parametrize("family15", [False, True])
async def test_reflector_missing_file_hint_is_family_gated(tmp_path, reflect_request, family15):
    from dataclasses import replace

    from tests.test_tree_chat_role_retry import RETRY

    reflector, client = _reflector(
        tmp_path,
        [
            _response("list_files"),
            _response("read_file", {"path": "skills/MISSING.md"}),
            _response("finish_reflection", {"groups": [], "declined": True}),
        ],
        retry=RETRY,
    )
    reflector = replace(reflector, family15=family15, role_resilience=True)
    result = await reflector.reflect(reflect_request)
    assert result.status == "declined", result.note
    errors = [r["error"] for r in _log_lines(reflector) if r.get("phase") == "tool_error"]
    assert errors == (
        ["path does not exist: skills/MISSING.md; nearest existing paths: skills/SKILL.md"]
        if family15
        else ["cannot read file: MISSING.md"]
    )


@pytest.mark.parametrize("family15", [False, True])
async def test_reflector_opening_states_probe_adjusted_limits(tmp_path, reflect_request, family15):
    from dataclasses import replace

    from evolution.tree.roles.reflector_contracts import ProbeAccess
    from tests.test_tree_chat_role_retry import RETRY

    reflector, client = _reflector(
        tmp_path,
        [_response("list_files"), _response("finish_reflection", {"groups": [], "declined": True})],
        retry=RETRY,
        max_turns=12,
        max_protocol_repairs=4,
    )
    reflector = replace(reflector, family15=family15, role_resilience=True)
    access = ProbeAccess("http://localhost:1234/probe", 10, ("GET",), None)
    request = replace(reflect_request, probe=access, probe_session=SimpleNamespace())
    result = await reflector.reflect(request)
    assert result.status == "declined", result.note
    opening = client.calls[1]["messages"][1]["content"]
    sentence = "Session limits: at most 28 model turns in this session, the preflight included, and 4 protocol repairs."
    assert (sentence in opening) is family15
    if family15:
        assert opening.endswith(sentence)
