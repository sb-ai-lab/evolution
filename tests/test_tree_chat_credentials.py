"""#290 §4.1, C1 and C2: what credential headers a chat role's requests carry.

A chat Editor, Reflector or Grouper without `api_key_env` is unauthenticated:
its client strips `Authorization`, `api-key`, `OpenAI-Organization` and `OpenAI-Project`
at the HTTP transport, below the SDK, so no credential reaches the wire from
the process environment (`OPENAI_API_KEY`, `OPENAI_ORG_ID`,
`OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`) or from another role. A declared key
sends exactly `Authorization: Bearer <that key>` and keeps today's client.

Every test here captures the headers on the loopback `fake_chat_server`, so it
runs against whichever `openai` version is installed: the lock's on CI, the
development environment's locally. That is the supported-version boundary C1
names.

The Grouper is exercised at the client and `run_model_grouping` level and in
a full family-14 campaign, where an undeclared key is left out of the
journaled adaptation identity (§4.2, C5); adding or removing it on a running
campaign is one `rebase`.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

import tests.fake_chat_server as fake_chat_server
from evolution.tree.roles.config import ChatEditorSection
from evolution.tree.roles.editor.chat_editor import (
    CREDENTIAL_HEADERS,
    PROTOCOL_HEADERS,
    UNAUTHENTICATED_API_KEY,
    ChatCredentials,
    _new_client,
    environment_header_names,
)
from evolution.tree.roles.grouping.model_grouping_runtime import (
    MAP_LEAD,
    new_grouping_client,
    run_model_grouping,
)
from evolution.tree.roles.workspace import open_attempt_log
from tests.model_grouping_fakes import group_everything

#: Values a careless client would forward; none may ever appear in a header.
ENVIRONMENT = {
    "OPENAI_API_KEY": "sk-environment-api-key-290",
    "OPENAI_ORG_ID": "org-environment-290",
    "OPENAI_PROJECT_ID": "proj-environment-290",
    "OPENAI_ADMIN_KEY": "sk-environment-admin-key-290",
}
#: `OPENAI_CUSTOM_HEADERS` (openai >= 2.38 merges it into its default headers):
#: one credential-looking header, one harmless one and a line that is neither.
CUSTOM_HEADERS = "X-Api-Key: sk-leaked-290\nX-Harmless: hello-290\nnot a header line"
CUSTOM_VALUES = ("sk-leaked-290", "hello-290")
KEY_A = "sk-declared-key-a-290"
KEY_B = "sk-declared-key-b-290"


@pytest.fixture
def credential_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def custom_headers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", CUSTOM_HEADERS)


@pytest.fixture
def endpoint() -> Iterator[tuple[str, Any]]:
    base_url, server = fake_chat_server.start([fake_chat_server.completion("ok")])
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()


def values(headers: tuple[tuple[str, str], ...], name: str) -> list[str]:
    return [value for key, value in headers if key == name]


def assert_credential_free(captured: list[tuple[tuple[str, str], ...]]) -> None:
    """No credential header, no header `OPENAI_CUSTOM_HEADERS` names, and no
    environment value or the placeholder key in any header at all."""

    assert captured, "the endpoint received no request"
    for headers in captured:
        names = {key for key, _ in headers}
        assert names.isdisjoint(CREDENTIAL_HEADERS), headers
        assert names.isdisjoint({"x-api-key", "x-harmless"}), headers
        for _key, value in headers:
            for secret in (*ENVIRONMENT.values(), *CUSTOM_VALUES, UNAUTHENTICATED_API_KEY):
                assert secret not in value, headers


def assert_sends_only(captured: list[tuple[tuple[str, str], ...]], key: str) -> None:
    """Exactly `Authorization: Bearer <key>`, and today's other headers.

    C2 and the §2 non-goal keep an authenticated role's client and request
    bytes as they were: the SDK still reads `OPENAI_ORG_ID` and
    `OPENAI_PROJECT_ID` for it, so those headers are pinned as today's, not
    removed. No other environment credential appears anywhere.
    """

    assert captured, "the endpoint received no request"
    for headers in captured:
        assert values(headers, "authorization") == [f"Bearer {key}"], headers
        assert values(headers, "openai-organization") == [ENVIRONMENT["OPENAI_ORG_ID"]], headers
        assert values(headers, "openai-project") == [ENVIRONMENT["OPENAI_PROJECT_ID"]], headers
        for _key, value in headers:
            for name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
                assert ENVIRONMENT[name] not in value, headers


def chat_section(base_url: str, api_key_env: str | None) -> ChatEditorSection:
    return ChatEditorSection(
        model=fake_chat_server.MODEL, base_url=base_url, api_key_env=api_key_env
    )


def grouping_section(base_url: str, api_key_env: str | None) -> Any:
    from tests.tree.roles.grouping.test_model_grouping_runtime import section

    return section(base_url=base_url, api_key_env=api_key_env)


def build(kind: str, base_url: str, credentials: ChatCredentials) -> Any:
    """The default client each chat role kind builds when nothing is injected.

    The Editor and the Reflector share `chat_editor._new_client`; under
    `role_resilience` both pass `max_retries=0`. The Grouper (and a
    Reflector's `model` grouping) build `new_grouping_client`.
    """

    declared = None if credentials.api_key is None else "DECLARED_KEY"
    if kind == "editor":
        return _new_client(chat_section(base_url, declared), credentials)
    if kind == "reflector":
        return _new_client(chat_section(base_url, declared), credentials, max_retries=0)
    return new_grouping_client(grouping_section(base_url, declared), credentials)


async def one_call(client: Any) -> None:
    try:
        await client.chat.completions.create(
            model=fake_chat_server.MODEL, messages=[{"role": "user", "content": "hi"}]
        )
    finally:
        await client.close()


ROLES = ["editor", "reflector", "grouper"]


# -- the clients ---------------------------------------------------------------


def test_the_credential_headers_are_the_sdks_and_the_azure_key() -> None:
    assert CREDENTIAL_HEADERS == (
        "authorization",
        "api-key",
        "openai-organization",
        "openai-project",
    )


@pytest.mark.parametrize("kind", ROLES)
async def test_an_unauthenticated_client_sends_no_credential_header(
    kind: str, credential_environment: None, endpoint: tuple[str, Any]
) -> None:
    """C1: with every `OPENAI_*` credential variable set, nothing reaches the wire."""

    base_url, server = endpoint
    await one_call(build(kind, base_url, ChatCredentials(None, None)))
    assert_credential_free(server.script.headers)


@pytest.mark.parametrize("kind", ROLES)
async def test_a_declared_key_sends_exactly_that_bearer(
    kind: str, credential_environment: None, endpoint: tuple[str, Any]
) -> None:
    """C2: exactly the declared key; organization and project stay as today."""

    base_url, server = endpoint
    await one_call(build(kind, base_url, ChatCredentials(KEY_A, None)))
    assert_sends_only(server.script.headers, KEY_A)


@pytest.mark.parametrize("kind", ROLES)
@pytest.mark.parametrize("api_key", [None, KEY_A], ids=["unauthenticated", "declared"])
async def test_the_proxy_is_kept_with_or_without_a_key(
    kind: str, api_key: str | None, credential_environment: None, endpoint: tuple[str, Any]
) -> None:
    """§4.1: `proxy_env` stays independent. The base URL cannot resolve, so the
    request only arrives because it went through the proxy -- the fake server,
    which answers the absolute-form request it is sent."""

    proxy_url, server = endpoint
    proxy_url = proxy_url.removesuffix("/v1")
    await one_call(build(kind, "http://models.invalid/v1", ChatCredentials(api_key, proxy_url)))
    if api_key is None:
        assert_credential_free(server.script.headers)
    else:
        assert_sends_only(server.script.headers, api_key)
    assert all(values(h, "host") == ["models.invalid"] for h in server.script.headers)


async def test_two_roles_with_different_keys_each_send_their_own(
    credential_environment: None,
) -> None:
    """C2: an Editor with key A and a Reflector with key B; then a Reflector
    without a key beside an Editor with one sends no `Authorization`."""

    servers = [fake_chat_server.start([fake_chat_server.completion("ok")]) for _ in range(4)]
    try:
        (edit_url, edit), (reflect_url, reflect), (edit2_url, edit2), (bare_url, bare) = servers
        await one_call(build("editor", edit_url, ChatCredentials(KEY_A, None)))
        await one_call(build("reflector", reflect_url, ChatCredentials(KEY_B, None)))
        await one_call(build("editor", edit2_url, ChatCredentials(KEY_A, None)))
        await one_call(build("reflector", bare_url, ChatCredentials(None, None)))
        assert_sends_only(edit.script.headers, KEY_A)
        assert_sends_only(reflect.script.headers, KEY_B)
        assert_sends_only(edit2.script.headers, KEY_A)
        assert_credential_free(bare.script.headers)
    finally:
        for _url, server in servers:
            server.shutdown()
            server.server_close()


class _Recorder:
    def __init__(self, name: str, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.name, self.calls = name, calls

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append((self.name, kwargs))
        return kwargs


@pytest.mark.parametrize("kind", ROLES)
def test_a_declared_key_client_is_built_as_before(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: the same constructor arguments as before #290, and no new transport."""

    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("httpx.AsyncClient", _Recorder("httpx", calls))
    monkeypatch.setattr("openai.AsyncOpenAI", _Recorder("openai", calls))
    build(kind, "https://gw.example/v1", ChatCredentials(KEY_A, "http://proxy.example:3128"))
    (http_name, http_kwargs), (sdk_name, sdk_kwargs) = calls
    assert (http_name, sdk_name) == ("httpx", "openai")
    assert http_kwargs == {
        "verify": True,
        "trust_env": False,
        "proxy": "http://proxy.example:3128",
        "follow_redirects": False,
    }
    expected: dict[str, Any] = {
        "api_key": KEY_A,
        "base_url": "https://gw.example/v1",
        "http_client": http_kwargs,
    }
    if kind == "reflector":
        expected["max_retries"] = 0
    if kind == "grouper":
        expected.update(max_retries=0, timeout=None)
    assert sdk_kwargs == expected


def _sdk_stub(calls: list[dict[str, Any]], *, organization: bool, project: bool) -> type:
    """An `AsyncOpenAI` stand-in whose signature has `organization` and
    `project` only when asked: early 1.x has no `project` parameter."""

    def init(
        self: Any,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: Any = None,
        http_client: Any = None,
        **named: Any,
    ) -> None:
        calls.append({"api_key": api_key, "base_url": base_url, **named})

    named_only = [
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None)
        for name, wanted in (("organization", organization), ("project", project))
        if wanted
    ]
    base = inspect.signature(init).parameters.values()
    signature = inspect.Signature(
        [p for p in base if p.kind is not inspect.Parameter.VAR_KEYWORD] + named_only
    )
    init.__signature__ = signature  # type: ignore[attr-defined]
    return type("AsyncOpenAI", (), {"__init__": init})


@pytest.mark.parametrize("kind", ROLES)
def test_an_unauthenticated_client_reads_no_credential_from_the_environment(
    kind: str, monkeypatch: pytest.MonkeyPatch, credential_environment: None
) -> None:
    """C1: the SDK is handed a fixed placeholder key and an empty organization
    and project, so nothing it is given derives from the environment."""

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("openai.AsyncOpenAI", _sdk_stub(calls, organization=True, project=True))
    build(kind, "https://gw.example/v1", ChatCredentials(None, None))
    ((given,),) = (calls,)
    assert all(value not in ENVIRONMENT.values() for value in given.values())
    assert given["api_key"] == UNAUTHENTICATED_API_KEY
    assert given["organization"] == "" and given["project"] == ""


@pytest.mark.parametrize("kind", ROLES)
@pytest.mark.parametrize(
    ("organization", "project"), [(True, False), (False, False)], ids=["no-project", "neither"]
)
def test_an_sdk_without_the_parameter_is_not_handed_it(
    kind: str,
    organization: bool,
    project: bool,
    monkeypatch: pytest.MonkeyPatch,
    credential_environment: None,
) -> None:
    """`openai>=1.0` admits an SDK with no `project` parameter, which then also
    reads no `OPENAI_PROJECT_ID`: the client passes only what the signature has."""

    calls: list[dict[str, Any]] = []
    stub = _sdk_stub(calls, organization=organization, project=project)
    monkeypatch.setattr("openai.AsyncOpenAI", stub)
    build(kind, "https://gw.example/v1", ChatCredentials(None, None))
    ((given,),) = (calls,)
    assert ("organization" in given) is organization
    assert "project" not in given


def test_environment_header_names_follows_the_sdk_parse() -> None:
    """Newline-separated lines, split at the first colon, names stripped; a line
    without a colon is not a header and an empty name is no name."""

    assert environment_header_names({"OPENAI_CUSTOM_HEADERS": CUSTOM_HEADERS}) == (
        "x-api-key",
        "x-harmless",
    )
    assert environment_header_names({"OPENAI_CUSTOM_HEADERS": " X-A : b:c\r\nx-a: d\n: e"}) == (
        "x-a",
    )
    assert environment_header_names({}) == ()


@pytest.mark.parametrize("kind", ROLES)
async def test_an_unauthenticated_client_drops_the_environment_custom_headers(
    kind: str,
    credential_environment: None,
    custom_headers_environment: None,
    endpoint: tuple[str, Any],
) -> None:
    """C1: a header the SDK merges from `OPENAI_CUSTOM_HEADERS` -- credential or
    not -- never leaves an unauthenticated role."""

    base_url, server = endpoint
    await one_call(build(kind, base_url, ChatCredentials(None, None)))
    assert_credential_free(server.script.headers)


#: `OPENAI_CUSTOM_HEADERS` naming protocol headers beside a custom header and
#: a credential: the protocol ones stay, the other two go (#290 final review).
PROTOCOL_CUSTOM_HEADERS = (
    "Content-Type: application/json\n"
    "Accept: application/json\n"
    "User-Agent: protocol-agent-290\n"
    "X-Custom: custom-290\n"
    "Authorization: Bearer sk-custom-auth-290\n"
    "api-key: sk-custom-azure-290"
)


@pytest.mark.parametrize("kind", ROLES)
async def test_an_unauthenticated_client_keeps_the_protocol_headers_custom_headers_name(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    credential_environment: None,
    endpoint: tuple[str, Any],
) -> None:
    """A protocol header such as `Content-Type` is never removed, even when
    `OPENAI_CUSTOM_HEADERS` names it; every other header it names, and every
    credential header, still is. The call succeeds on any `openai` version."""

    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", PROTOCOL_CUSTOM_HEADERS)
    base_url, server = endpoint
    await one_call(build(kind, base_url, ChatCredentials(None, None)))
    (headers,) = server.script.headers
    (content_type,) = values(headers, "content-type")
    assert content_type.startswith("application/json"), headers
    assert values(headers, "accept"), headers
    assert values(headers, "user-agent"), headers
    assert int(values(headers, "content-length")[0]) > 0, headers
    names = {key for key, _ in headers}
    assert names.isdisjoint({"x-custom", "authorization", "api-key"}), headers
    for _key, value in headers:
        assert "custom-290" not in value and "sk-custom" not in value, headers


def test_the_protocol_headers_are_never_stripped() -> None:
    """Named or not, the transport keeps the headers HTTP itself needs, and
    drops the credential headers whatever the exemption says."""

    assert {"content-type", "content-length", "accept", "user-agent", "host"} <= set(
        PROTOCOL_HEADERS
    )
    assert set(PROTOCOL_HEADERS).isdisjoint(CREDENTIAL_HEADERS)


def _today(kind: str, base_url: str, key: str) -> Any:
    """The declared-key client exactly as it was built before #290."""

    import httpx
    from openai import AsyncOpenAI

    extra: dict[str, Any] = {}
    if kind == "reflector":
        extra = {"max_retries": 0}
    if kind == "grouper":
        extra = {"max_retries": 0, "timeout": None}
    return AsyncOpenAI(
        api_key=key,
        base_url=base_url,
        http_client=httpx.AsyncClient(
            verify=True, trust_env=False, proxy=None, follow_redirects=False
        ),
        **extra,
    )


@pytest.mark.parametrize("kind", ROLES)
async def test_a_declared_key_sends_the_same_headers_as_before(
    kind: str, credential_environment: None, custom_headers_environment: None
) -> None:
    """C2 pin: with `OPENAI_CUSTOM_HEADERS` and the credential variables set, a
    declared-key role's request headers equal today's client's, whatever the
    installed SDK does with that environment."""

    base_url, server = fake_chat_server.start([fake_chat_server.completion("ok")])
    try:
        await one_call(build(kind, base_url, ChatCredentials(KEY_A, None)))
        await one_call(_today(kind, base_url, KEY_A))
    finally:
        server.shutdown()
        server.server_close()
    ours, before = server.script.headers
    assert sorted(ours) == sorted(before)
    assert values(ours, "authorization") == [f"Bearer {KEY_A}"]


# -- the Grouper's runtime -----------------------------------------------------


def _grouping_answer(request: Mapping[str, Any]) -> dict[str, Any]:
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    if user.startswith(MAP_LEAD):
        return fake_chat_server.completion(group_everything()(request))
    return fake_chat_server.completion(json.dumps({"merges": []}))


@pytest.mark.parametrize("api_key", [None, KEY_A], ids=["unauthenticated", "declared"])
async def test_a_family14_chat_grouping_runs_with_the_default_client(
    tmp_path: Path, api_key: str | None, credential_environment: None
) -> None:
    """The `chat` Grouper's own path: `run_model_grouping` with no injected
    factory builds `new_grouping_client`, and the grouping is admitted."""

    from tests.tree.roles.grouping.test_model_grouping_runtime import NEEDLE, PROMPT, REG, SECRET

    base_url, server = fake_chat_server.start([_grouping_answer])
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=(SECRET,))
    try:
        outcome = await run_model_grouping(
            REG,
            section=grouping_section(base_url, None if api_key is None else "GROUPER_KEY"),
            prompt=PROMPT,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials(api_key, None),
            log=log,
            deadline=float("inf"),
            cancelled=lambda: False,
            forbidden_secret=lambda text: SECRET in text,
            oracle_needle=lambda text: NEEDLE in text,
            family14=True,
        )
    finally:
        log.close()
        server.shutdown()
        server.server_close()
    assert outcome.fallback is None
    if api_key is None:
        assert_credential_free(server.script.headers)
    else:
        assert_sends_only(server.script.headers, api_key)


# -- a campaign: the chat Editor and the chat Reflector, bound by `evolve` -----


@pytest.mark.slow
@pytest.mark.parametrize(
    ("editor_key", "reflector_key"),
    [(None, None), (KEY_A, KEY_B), (KEY_A, None), (None, KEY_B)],
    ids=["neither", "both", "editor-only", "reflector-only"],
)
async def test_a_chat_campaign_sends_each_role_its_own_credential_or_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_environment: None,
    custom_headers_environment: None,
    editor_key: str | None,
    reflector_key: str | None,
) -> None:
    """A full `/15` campaign against two fake servers finishes, and each role's
    requests carry its own declared key or no credential at all."""

    from evolution.tree.evolve import evolve
    from tests.test_tree_evolve import (
        CHAT_REFLECT_SCRIPT,
        CHAT_SCRIPT,
        chat_editor_section,
        chat_reflector_section,
        fake_adapters,
    )
    from tests.tree_evolve_support import write_workspace

    edit_url, edit = fake_chat_server.start(CHAT_SCRIPT)
    reflect_url, reflect = fake_chat_server.start(CHAT_REFLECT_SCRIPT)
    try:
        editor = chat_editor_section(edit_url)
        reflector = chat_reflector_section(reflect_url)
        for section, name, key in (
            (editor, "EDIT_KEY", editor_key),
            (reflector, "REFLECT_KEY", reflector_key),
        ):
            if key is None:
                del section["api_key_env"]
            else:
                section["api_key_env"] = name
                monkeypatch.setenv(name, key)
        workspace = tmp_path / "ws"
        manifest = write_workspace(workspace, editor=editor, reflector=reflector)
        result = await evolve(manifest, workspace / "run", adapter_factory=fake_adapters())
    finally:
        for server in (edit, reflect):
            server.shutdown()
            server.server_close()

    assert result.terminal == "finished"
    assert result.finished is not None and result.finished["outcome"] == "completed"
    for server, key in ((edit, editor_key), (reflect, reflector_key)):
        if key is None:
            assert_credential_free(server.script.headers)
        else:
            assert_sends_only(server.script.headers, key)


# -- a campaign: the chat Grouper, bound by `evolve` (#290 Task 4) -------------


def _grouper(base_url: str, api_key_env: str | None) -> dict[str, Any]:
    from tests.model_grouping_fakes import chat_grouper_section

    section = chat_grouper_section(base_url=base_url, model=fake_chat_server.MODEL)
    if api_key_env is None:
        del section["api_key_env"]
    else:
        section["api_key_env"] = api_key_env
    return section


def _grouper_kind(run: Path) -> tuple[str, dict[str, Any]]:
    created = json.loads((run / "campaign.jsonl").read_text().splitlines()[0])["payload"]
    grouping = created["adaptation_payload"]["grouping"]
    return grouping["kind"], grouping


@pytest.mark.slow
@pytest.mark.parametrize("api_key", [None, KEY_A], ids=["unauthenticated", "declared"])
async def test_a_family14_campaign_with_a_chat_grouper_sends_its_credential_or_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_environment: None,
    custom_headers_environment: None,
    api_key: str | None,
) -> None:
    """A full `/15` campaign whose `chat` Grouper talks to a loopback server
    through its real client finishes; an undeclared key is left out of the
    journaled adaptation identity and its requests carry no credential."""

    from tests.test_tree_campaign_family14_run import family14_run, payloads

    base_url, server = fake_chat_server.start([_grouping_answer])
    if api_key is not None:
        monkeypatch.setenv("GROUPER_KEY_290", api_key)
    try:
        run, snapshot = await family14_run(
            tmp_path,
            monkeypatch,
            family14_settings={
                "grouper": _grouper(base_url, None if api_key is None else "GROUPER_KEY_290")
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    kind, grouping = _grouper_kind(run)
    assert kind == "chat"
    if api_key is None:
        assert "api_key_env" not in grouping
        assert_credential_free(server.script.headers)
    else:
        assert grouping["api_key_env"] == "GROUPER_KEY_290"
        assert_sends_only(server.script.headers, api_key)
    (finished,) = payloads(run, "grouping_finished")
    assert finished["fallback"] is None


@pytest.mark.slow
@pytest.mark.parametrize("direction", ["removed", "added"])
async def test_adding_or_removing_the_grouper_key_needs_one_rebase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_environment: None,
    direction: str,
) -> None:
    """#290 §4.2: a family-14 run whose chat Grouper gains or loses
    `api_key_env` is refused once as `run_identity_changed` on the adaptation
    identity, a `rebase` one authorization accepts; the run then finishes and
    the later Grouper requests carry the new credential state."""

    from evolution.tree.evolve import EvolveRefused
    from tests.campaign_drivers import STATISTICAL, WORSE_SCORER, _cut_hook
    from tests.campaign_fakes import Crash
    from tests.test_tree_campaign_family14_run import family14_run, payloads
    from tests.test_tree_campaign_family14_visibility_change import _decision

    monkeypatch.setenv("GROUPER_KEY_290", KEY_A)
    base_url, server = fake_chat_server.start([_grouping_answer])
    first, second = (
        ("GROUPER_KEY_290", None) if direction == "removed" else (None, "GROUPER_KEY_290")
    )
    nonpromoting = {**STATISTICAL, "scorer": WORSE_SCORER}
    options: dict[str, Any] = {"campaign": {"rounds": 2}}
    try:
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_attempt_input_frozen")
            with pytest.raises(Crash):
                await family14_run(
                    tmp_path,
                    monkeypatch,
                    nonpromoting,
                    family14_settings={"grouper": _grouper(base_url, first)},
                    **options,
                )
        before = list(server.script.headers)
        if first is None:
            assert_credential_free(before)
        else:
            assert_sends_only(before, KEY_A)

        settings = {"grouper": _grouper(base_url, second)}
        with pytest.raises(EvolveRefused) as caught:
            await family14_run(
                tmp_path, monkeypatch, nonpromoting, family14_settings=settings, **options
            )
        refusal = caught.value
        fields = {difference.field for difference in refusal.differences}
        assert fields == {"adaptation"}
        assert refusal.code == "run_identity_changed" and refusal.effects == ("rebase",)
        decision = _decision(refusal, None, reason="accept the Grouper credential change")
        run, snapshot = await family14_run(
            tmp_path,
            monkeypatch,
            nonpromoting,
            family14_settings=settings,
            authorizations=[decision],
            **options,
        )
        after = server.script.headers[len(before) :]
    finally:
        server.shutdown()
        server.server_close()

    assert snapshot.finished is not None and snapshot.finished["outcome"] == "completed"
    assert [p["changed_fields"] for p in payloads(run, "identity_rebased")] == [sorted(fields)]
    assert after, "the resumed run made no Grouper request"
    if second is None:
        assert_credential_free(after)
    else:
        assert_sends_only(after, KEY_A)
