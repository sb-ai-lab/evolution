"""Family-14 bounded Reflector openings, on-demand history and recovery (#228)."""

from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.campaign import inspect_campaign
from evolution.tree.campaign_schema import SchemaError
from evolution.tree.evolve import EvolveRefused
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor.editor_evidence import project_history
from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2, history_payload
from evolution.tree.roles.reflector.reflect_aggregate import serialized_chars
from evolution.tree.roles.reflector.reflect_budget import COMPONENTS, chat_request_size
from evolution.tree.roles.reflector.reflect_history import HISTORY_COMPACT_SENTENCE
from evolution.tree.roles.reflector_contracts import REFLECT_LIMITS_V1
from tests import fake_chat_server
from tests.campaign_drivers import STATISTICAL, WORSE_SCORER, _cut_hook
from tests.campaign_fakes import Crash, Family14Life
from tests.family14_b2_scenario import journal_records
from tests.test_tree_campaign_family14_role_feedback import DIAGNOSIS, dumping_reflector
from tests.test_tree_campaign_family14_run import family14_run, payloads

pytestmark = pytest.mark.slow

NONPROMOTING = {**STATISTICAL, "scorer": WORSE_SCORER}


def chat_reflector(
    base_url: str, *, compact: int = 800_000, message_max: int = 64_000
) -> dict[str, Any]:
    from tests.tree.manifest.test_manifest_v2 import RETRY

    return {
        "kind": "chat",
        "model": fake_chat_server.MODEL,
        "base_url": base_url,
        "api_key_env": "E1_KEY",
        "max_turns": 8,
        "retry": RETRY,
        "context_compact_chars": compact,
        "context_message_max_chars": message_max,
    }


def with_chat_reflector(
    monkeypatch: pytest.MonkeyPatch, section: dict[str, Any], *, probe: dict[str, Any] | None = None
) -> None:
    from tests import test_tree_campaign_family14_run as driver

    document = driver.family14_document

    def patched(*args: Any, **kwargs: Any) -> dict[str, Any]:
        moved = document(*args, **kwargs)
        kept = {
            k: v
            for k, v in moved["reflector"].items()
            if k
            in {
                "max_consecutive_failures",
                "group_text_chars",
                "environment_docs",
                "probe",
                "selection",
                "families",
            }
        }
        moved["reflector"] = {**kept, **section, **({"probe": probe} if probe is not None else {})}
        return moved

    monkeypatch.setattr(driver, "family14_document", patched)
    monkeypatch.setenv("E1_KEY", "k")


def _probe(port: int) -> dict[str, Any]:
    return {
        "base_url": f"http://127.0.0.1:{port}",
        "health_path": "/health",
        "methods": ["GET"],
        "budget": 2,
        "request_timeout_seconds": 1.0,
        "description": None,
    }


@contextmanager
def _chat_server():
    base_url, server = fake_chat_server.start(
        [
            fake_chat_server.tool_call("list_files"),
            fake_chat_server.tool_call("finish_reflection", {"groups": [], "declined": True}),
        ]
    )
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()


def _budget(run: Path, reflection_id: str) -> tuple[Path, dict[str, Any]]:
    path = run / "reflector-logs" / "budget" / f"{reflection_id}.json"
    return path, json.loads(path.read_text())


def _clean(run: Path) -> None:
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []


def _dump_history(out: Path) -> str:
    """Read through real subprocess launchers before saving each role request."""
    return (
        "import json, pathlib, subprocess, sys\n"
        "request = json.load(sys.stdin)\n"
        "def read(name, arguments):\n"
        "    return json.loads(subprocess.check_output(request['readers'][name] + [json.dumps(arguments)]))\n"
        "request['history_listing'] = read('list_history', {})\n"
        "attempts = [entry for entry in request['history_listing']['entries'] if entry['kind'] == 'attempt']\n"
        "if attempts:\n"
        "    history_id = attempts[0]['history_id']\n"
        "    page = read('read_history', {'history_id': history_id})\n"
        "    content = page['content']\n"
        "    while page['next_offset'] is not None:\n"
        "        page = read('read_history', {'history_id': history_id, 'offset': page['next_offset']})\n"
        "        content += page['content']\n"
        "    request['history_read'] = {'history_id': history_id, 'content': content}\n"
        f"out = pathlib.Path({str(out)!r})\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / f'{len(list(out.iterdir())):02d}.json').write_text(json.dumps(request))\n"
        f"print({json.dumps(DIAGNOSIS)!r})\n"
    )


@pytest.fixture
async def history_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from evolution.tree.campaign import _Campaign

    captured = []
    original = _Campaign._history_source

    def source(self, snapshot):
        built = original(self, snapshot)
        today = history_payload(
            project_history(
                snapshot,
                measurements=built.measurements,
                store=built.store,
                artifact_policy=built.artifact_policy,
                corpus=built.corpus,
                policy=built.policy,
                limits={**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1},
                excerpts=True,
                visible=built.visible,
            ),
            excerpts=True,
        )
        captured.append((snapshot, built, today))
        return built

    monkeypatch.setattr(_Campaign, "_history_source", source)
    run, snapshot = await family14_run(
        tmp_path,
        monkeypatch,
        NONPROMOTING,
        campaign={"rounds": 3},
        reflector=_dump_history(tmp_path / "requests"),
    )
    requests = [
        json.loads(path.read_text()) for path in sorted((tmp_path / "requests").glob("*.json"))
    ]
    assert len(requests) == len(captured) == 3
    return run, snapshot, requests, captured


async def test_a_subprocess_reflector_reads_a_compact_history_and_its_records(history_run) -> None:
    run, _, requests, captured = history_run
    request = requests[-1]
    evidence = request["evidence"]
    history = evidence["history"]
    assert evidence["version"] == "reflect_brief/5"
    assert history["version"] == "reflect_history/1"
    assert evidence["limits"]["history_chars"] == 96000
    assert history["detail_rounds"]["count"] == history["rounds_closed"] == 2
    assert '"result":' not in canonical_json(history).decode()
    assert '"edit_excerpt":' not in canonical_json(history).decode()
    assert HISTORY_COMPACT_SENTENCE in request["opening"]
    assert request["opening"].splitlines()[-1] == canonical_json(evidence).decode()
    assert {"list_history", "read_history"} <= set(request["readers"])
    _, source, today = captured[-1]
    saved = request["history_read"]
    expected = next(
        attempt
        for round_ in today["rounds"]
        for attempt in round_["attempts"]
        if saved["history_id"] == "attempt:" + attempt["attempt_id"]
    )
    assert expected["result"]["status"] == "present"
    assert [case["case_id"] for case in expected["result"]["cases"]] == [
        case.case_id for case in source.corpus
    ]
    for case, scores in zip(
        expected["result"]["cases"], ([0.0, 0.05, 0.1], [0.8, 0.9, 0.85]), strict=True
    ):
        # The gate rejects at look 3; repeat 4 was never dispatched.
        assert [repeat["terminal"] for repeat in case["repeats"]] == ["completed"] * 3 + [
            "not_started"
        ]
        assert [repeat["primary_score"] for repeat in case["repeats"]] == [*scores, None]
    assert saved["content"] == canonical_json(expected).decode()
    terminal = payloads(run, "reflection_finished")[-1]
    assert any(
        entry["operation"] == "read_history" and entry["history_id"] == saved["history_id"]
        for entry in terminal["reads"]["entries"]
    )
    _clean(run)


async def test_every_reflection_writes_a_private_size_record(history_run) -> None:
    run, _, requests, _ = history_run
    records = payloads(run, "reflection_started")
    assert len(records) == 3
    for started, request in zip(records, requests, strict=True):
        path, record = _budget(run, started["reflection_id"])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert record["decision"] == "sent" and record["budget_chars"] is None
        assert set(record["components"]) == set(COMPONENTS)
        assert record["components"]["history"] == 2 * serialized_chars(
            request["evidence"]["history"]
        )
        assert "hello" not in path.read_text()  # fixture's public corpus canary


@pytest.mark.parametrize("declared_probe", [False, True])
async def test_a_chat_reflector_whose_minimum_does_not_fit_is_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int, declared_probe: bool
) -> None:
    from evolution.tree import campaign as campaign_module

    calls = []

    async def forbidden_health(*args, **kwargs):
        calls.append("health")
        raise AssertionError("refused reflection checked its probe")

    monkeypatch.setattr(campaign_module, "check_probe_health", forbidden_health)
    with _chat_server() as (base_url, server):
        with_chat_reflector(
            monkeypatch,
            chat_reflector(base_url, compact=2_000, message_max=100),
            probe=_probe(unused_tcp_port) if declared_probe else None,
        )
        run, snapshot = await family14_run(tmp_path, monkeypatch)
        assert snapshot.finished is None
        assert payloads(run, "campaign_suspended")[-1]["reason"] == "reflector_failing"
        assert len(payloads(run, "reflection_started")) == 1
        failure = payloads(run, "reflection_failed")[-1]
        assert (failure["error_class"], failure["reason"]) == ("input_too_large", "input_too_large")
        events = [record["event"] for record in journal_records(run)]
        start = events.index("reflection_started")
        assert events[start : start + 2] == ["reflection_started", "reflection_failed"]
        assert not payloads(run, "probe_checked") and not payloads(run, "probe_completed")
        assert server.script.requests == [] and calls == []
        path, record = _budget(run, failure["reflection_id"])
        assert record["decision"] == "refused" and record["over_by"] > 0
        assert record["largest_component"] in COMPONENTS and "hello" not in path.read_text()
        _clean(run)


async def test_resuming_unchanged_refuses_again_and_an_authorized_raise_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_tree_campaign_family14_visibility_change import _decision

    with _chat_server() as (base_url, server):
        section = chat_reflector(base_url, compact=2_000, message_max=100)
        with_chat_reflector(monkeypatch, section)
        await family14_run(tmp_path, monkeypatch)
        run, _ = await family14_run(tmp_path, monkeypatch)
        assert [p["reflection_id"] for p in payloads(run, "reflection_failed")] == [
            "r01-x01",
            "r01-x02",
        ]
        assert {p["error_class"] for p in payloads(run, "reflection_failed")} == {"input_too_large"}
        assert server.script.requests == []
        section.update(context_compact_chars=800_000, context_message_max_chars=64_000)
        with pytest.raises(EvolveRefused) as caught:
            await family14_run(tmp_path, monkeypatch)
        refused = caught.value
        assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
        assert any(
            difference.field == "identity.reflector.digest" for difference in refused.differences
        )
        decision = _decision(refused, None, reason="accept the raised Reflector request budget")
        run, snapshot = await family14_run(tmp_path, monkeypatch, authorizations=[decision])
        assert snapshot.finished is not None
        assert payloads(run, "reflection_finished")[-1]["reflection_id"] == "r01-x03"
        assert len(server.script.requests) == 2
        _clean(run)


async def test_a_probe_less_start_admits_only_its_own_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int
) -> None:
    with _chat_server() as (base_url, _):
        with_chat_reflector(
            monkeypatch,
            chat_reflector(base_url, compact=2_000, message_max=100),
            probe=_probe(unused_tcp_port),
        )
        run, _ = await family14_run(tmp_path, monkeypatch)
    records = journal_records(run)
    index = next(i for i, record in enumerate(records) if record["event"] == "reflection_started")
    prefix = records[: index + 1]
    operation = records[index]["operation_id"]
    failure = payloads(run, "reflection_failed")[0]
    with pytest.raises(SchemaError, match="without a probe check makes no probe call"):
        Family14Life(prefix).accept(
            "probe_completed",
            round=1,
            reflection_id=failure["reflection_id"],
            seq=1,
            method="GET",
            path="/api",
            status=200,
            error=None,
        )
    common = {key: value for key, value in failure.items() if key not in {"error_class", "reason"}}
    with pytest.raises(SchemaError, match="without a probe check ends"):
        Family14Life(prefix).accept(
            "reflection_finished",
            op=operation,
            **common,
            status="declined",
            groups=[],
            out_of_scope_paths=[],
            probes_used=0,
        )
    with pytest.raises(SchemaError, match="without a probe check ends"):
        Family14Life(prefix).accept(
            "reflection_failed",
            op=operation,
            **common,
            error_class="reported",
            reason="provider_rejected",
        )
    life = Family14Life(prefix)
    life.accept("reflection_failed", op=operation, **common, error_class="interrupted")
    assert life.snapshot.rounds[1].reflections[-1]["error_class"] == "interrupted"


@pytest.mark.parametrize("declared_probe", [False, True])
async def test_an_interrupted_refusal_resumes_as_interrupted_and_refuses_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unused_tcp_port: int, declared_probe: bool
) -> None:
    with _chat_server() as (base_url, server):
        with_chat_reflector(
            monkeypatch,
            chat_reflector(base_url, compact=2_000, message_max=100),
            probe=_probe(unused_tcp_port) if declared_probe else None,
        )
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_reflection_started")
            with pytest.raises(Crash):
                await family14_run(tmp_path, monkeypatch)
        run, snapshot = await family14_run(tmp_path, monkeypatch)
        assert snapshot.finished is None
        failures = payloads(run, "reflection_failed")
        assert [(p["reflection_id"], p["error_class"]) for p in failures] == [
            ("r01-x01", "interrupted"),
            ("r01-x02", "input_too_large"),
        ]
        assert server.script.requests == [] and not payloads(run, "probe_checked")
        _clean(run)


async def test_a_reflection_re_run_after_an_interrupt_has_the_same_input_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.campaign import _Campaign
    from evolution.tree.roles.reflector.reflect_brief import (
        reflect_brief_payload,
        render_reflect_opening,
    )

    rendered = {}
    original = _Campaign._bound_reflection

    def bound(self, source, reflection_id, brief, inputs):
        result = original(self, source, reflection_id, brief, inputs)
        rendered[reflection_id] = (
            canonical_json(reflect_brief_payload(result[0])),
            render_reflect_opening(
                result[0],
                (),
                self.artifact_policy.writable_paths,
                editor_max_changed_files=self._editor_file_budget(),
            ),
        )
        return result

    monkeypatch.setattr(_Campaign, "_bound_reflection", bound)
    options = {"campaign": {"rounds": 3}, "reflector": dumping_reflector(tmp_path / "requests")}
    with monkeypatch.context() as patch:
        _cut_hook(patch, "after_reflection_started", call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
    run, _ = await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
    digests = {p["reflection_id"]: p["input_digest"] for p in payloads(run, "reflection_started")}
    assert digests["r02-x01"] == digests["r02-x02"]
    assert rendered["r02-x01"] == rendered["r02-x02"]
    _clean(run)


def with_text_limit(monkeypatch: pytest.MonkeyPatch, declared: dict[str, Any]) -> None:
    """Declare `reflector.group_text_chars` as `declared["limit"]`, read at each
    run, so a test can change it between a crash and its resume (#282)."""

    from tests import test_tree_campaign_family14_run as driver

    document = driver.family14_document

    def patched(*args: Any, **kwargs: Any) -> dict[str, Any]:
        moved = document(*args, **kwargs)
        if "limit" in declared:
            moved["reflector"]["group_text_chars"] = declared["limit"]
        return moved

    monkeypatch.setattr(driver, "family14_document", patched)


@pytest.mark.parametrize("seam", ["after_reflection_started", "after_attempt_input_frozen"])
async def test_a_retried_reflection_after_a_mid_round_change_keeps_its_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    """#282 (R3, design §4.4): a limit authorized while round 2 is open takes
    effect at round 3. Round 2's retried reflection keeps its input digest and
    brief, and its resumed Attempt keeps its frozen Editor common input."""

    from evolution.tree.campaign import _Campaign
    from evolution.tree.roles.reflector.reflect_brief import reflect_brief_payload
    from tests.test_tree_campaign_family14_visibility_change import _decision

    briefs: dict[str, bytes] = {}
    editor_limits: dict[int, set[int | None]] = {}
    original_bound, original_brief_for = _Campaign._bound_reflection, _Campaign.brief_for

    def bound(self, source, reflection_id, brief, inputs):
        result = original_bound(self, source, reflection_id, brief, inputs)
        briefs[reflection_id] = canonical_json(reflect_brief_payload(result[0]))
        return result

    def brief_for(self, attempt):
        brief = original_brief_for(self, attempt)
        editor_limits.setdefault(attempt.round, set()).add(brief.limits.get("sentence_chars"))
        return brief

    monkeypatch.setattr(_Campaign, "_bound_reflection", bound)
    monkeypatch.setattr(_Campaign, "brief_for", brief_for)
    declared: dict[str, Any] = {}
    with_text_limit(monkeypatch, declared)
    options = {"campaign": {"rounds": 3}, "reflector": dumping_reflector(tmp_path / "requests")}
    with monkeypatch.context() as patch:
        _cut_hook(patch, seam, call=2)
        with pytest.raises(Crash):
            await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
    declared["limit"] = 4096
    with pytest.raises(EvolveRefused) as caught:
        await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
    refused = caught.value
    assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
    decision = _decision(refused, None, reason="accept the raised Reflector text limit")
    run, snapshot = await family14_run(
        tmp_path, monkeypatch, NONPROMOTING, authorizations=[decision], **options
    )
    assert snapshot.finished is not None
    assert [snapshot.round_group_text_chars(number) for number in (1, 2, 3)] == [512, 512, 4096]
    digests = {p["reflection_id"]: p["input_digest"] for p in payloads(run, "reflection_started")}
    if seam == "after_reflection_started":
        assert digests["r02-x01"] == digests["r02-x02"]
        assert briefs["r02-x01"] == briefs["r02-x02"]
    assert b'"sentence_chars":512' in briefs["r02-x01"]
    assert b'"sentence_chars":4096' in briefs["r03-x01"]
    # A changed frozen common input would have raised `CampaignInvariantViolated`.
    assert editor_limits[2] == {512} and editor_limits[3] == {4096}
    _clean(run)


def _group_items(tools: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = next(t["function"] for t in tools if t["function"]["name"] == "finish_reflection")
    return terminal["parameters"]["properties"]["groups"]["items"]


#: A Reflector diagnosis with 900-character text fields (#282: valid `/16`
#: `root_cause` values ran to 925 and 1,198 characters).
LONG_GROUPS = [
    {**group, **{key: "x" * 900 for key in ("root_cause", "counterevidence", "why_this_layer")}}
    for group in DIAGNOSIS["groups"]
]


@pytest.mark.parametrize("limit", [..., 4096, None])
async def test_a_900_character_field_runs_at_the_round_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: Any
) -> None:
    """Under a declared 4,096 or `null` the long diagnosis is the reflection's
    result; under the absent `/15` key (512) #180's repair names the bound and
    the shorter diagnosis that follows is journaled. The first request's size
    record and the request sent agree in every case: one limit, one tool list."""

    base_url, server = fake_chat_server.start(
        [
            fake_chat_server.tool_call("list_files"),
            fake_chat_server.tool_call(
                "finish_reflection", {"groups": LONG_GROUPS, "declined": False}
            ),
            fake_chat_server.tool_call(
                "finish_reflection", {"groups": DIAGNOSIS["groups"], "declined": False}
            ),
        ]
    )
    try:
        section = chat_reflector(base_url)
        if limit is not ...:
            section["group_text_chars"] = limit
        with_chat_reflector(monkeypatch, section)
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, NONPROMOTING, campaign={"rounds": 1}
        )
        assert snapshot.round_group_text_chars(1) == (512 if limit is ... else limit)
        (finished,) = payloads(run, "reflection_finished")
        stored = finished["groups"][0]["root_cause"]
        body = server.script.requests[1]
        root_cause = _group_items(body["tools"])["properties"]["root_cause"]
        _, record = _budget(run, finished["reflection_id"])
        assert chat_request_size(body["messages"], body["tools"]) == record["total_chars"]
        if limit is ...:
            assert root_cause == {"type": "string", "maxLength": 512}
            assert stored == DIAGNOSIS["groups"][0]["root_cause"]
            assert len(server.script.requests) == 3
            assert "root_cause exceeds 512 characters" in json.dumps(
                server.script.requests[2]["messages"][-1]
            )
        else:
            assert root_cause == (
                {"type": "string"} if limit is None else {"type": "string", "maxLength": limit}
            )
            assert stored == "x" * 900 and len(server.script.requests) == 2
        _clean(run)
    finally:
        server.shutdown()
        server.server_close()


def _finish(groups: list[dict[str, Any]]) -> dict[str, Any]:
    return fake_chat_server.tool_call("finish_reflection", {"groups": groups, "declined": False})


def text_limit_reply(text_for: Any, seen: list[int | None]) -> Any:
    """A chat Reflector turn for running-campaign limit tests (#282).

    The connection preflight's turn and a reflection's first turn list files; the next finishes with groups whose three text
    fields hold `text_for(limit)` characters, `limit` being the `maxLength` the
    request's own tool schema publishes; after a bounds repair (#180) names the
    limit, it finishes with the short `DIAGNOSIS`. `seen` collects each
    request's `maxLength`, in order."""

    def reply(body: dict[str, Any]) -> dict[str, Any]:
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "finish_reflection" not in names:  # the connection preflight's turn
            return fake_chat_server.tool_call("list_files")
        limit = _group_items(body["tools"])["properties"]["root_cause"].get("maxLength")
        seen.append(limit)
        if "exceeds" in json.dumps(body["messages"][-1]):
            return _finish(DIAGNOSIS["groups"])
        if not any(message.get("role") == "tool" for message in body["messages"]):
            return fake_chat_server.tool_call("list_files")
        chars = text_for(limit)
        return _finish(
            [
                {
                    **group,
                    **{k: "x" * chars for k in ("root_cause", "counterevidence", "why_this_layer")},
                }
                for group in DIAGNOSIS["groups"]
            ]
        )

    return reply


def _runs(seen: list[Any]) -> list[Any]:
    """`seen` with consecutive repeats collapsed: one entry per limit era."""

    return [value for i, value in enumerate(seen) if i == 0 or seen[i - 1] != value]


async def test_a_raised_then_lowered_limit_keeps_every_stored_group_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#282 (design §4.4, C6): 512 in round 1, 4,096 authorized for round 2,
    which stores 900-character groups, then 512 authorized for round 3, which
    repairs at 512. The lowered limit never invalidates round 2's groups: the
    run finishes and replays clean."""

    from tests.test_tree_campaign_family14_visibility_change import _decision

    seen: list[int | None] = []
    base_url, server = fake_chat_server.start([text_limit_reply(lambda limit: 900, seen)])
    try:
        with_chat_reflector(monkeypatch, chat_reflector(base_url))
        declared: dict[str, Any] = {}
        with_text_limit(monkeypatch, declared)
        options = {"campaign": {"rounds": 3}}

        async def until_round_closed(*authorizations: Any) -> None:
            with monkeypatch.context() as patch:
                _cut_hook(patch, "after_round_closed")
                with pytest.raises(Crash):
                    await family14_run(
                        tmp_path,
                        monkeypatch,
                        NONPROMOTING,
                        authorizations=list(authorizations),
                        **options,
                    )

        async def authorized(limit: int, reason: str) -> Any:
            declared["limit"] = limit
            with pytest.raises(EvolveRefused) as caught:
                await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
            refused = caught.value
            assert refused.code == "run_identity_changed" and refused.effects == ("rebase",)
            assert [difference.field for difference in refused.differences] == ["adaptation"]
            return _decision(refused, None, reason=reason)

        await until_round_closed()
        await until_round_closed(await authorized(4096, "raise the Reflector text limit"))
        lowered = await authorized(512, "lower the Reflector text limit")
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, NONPROMOTING, authorizations=[lowered], **options
        )
    finally:
        server.shutdown()
        server.server_close()
    assert snapshot.finished is not None
    assert [snapshot.round_group_text_chars(number) for number in (1, 2, 3)] == [512, 4096, 512]
    assert _runs(seen) == [512, 4096, 512]
    stored = {
        p["reflection_id"][:3]: len(p["groups"][0]["root_cause"])
        for p in payloads(run, "reflection_finished")
    }
    assert stored == {"r01": 1, "r02": 900, "r03": 1}
    repairs = [
        body
        for body in server.script.requests
        if "root_cause exceeds 512 characters" in json.dumps(body["messages"][-1])
    ]
    assert len(repairs) == 2  # rounds 1 and 3; round 2 kept its 900 characters
    assert len(payloads(run, "identity_rebased")) == 2
    assert inspect_campaign(run, verify=True).to_payload()["verification"]["mismatches"] == []
    _clean(run)


async def test_two_reflections_on_one_baseline_see_their_own_history(history_run) -> None:
    _, snapshot, requests, captured = history_run
    assert snapshot.rounds[2].baseline_measurement_id == snapshot.rounds[3].baseline_measurement_id
    second = {entry["history_id"] for entry in requests[1]["history_listing"]["entries"]}
    third = {entry["history_id"] for entry in requests[2]["history_listing"]["entries"]}
    assert "attempt:r02-a01" not in second and "attempt:r02-a01" in third
    assert "changes:r02-a01" in third and "diagnosis:2" in third
    assert captured[1][1] is not captured[2][1]


async def test_the_role_check_agrees_with_the_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _chat_server() as (base_url, server):
        with_chat_reflector(monkeypatch, chat_reflector(base_url))
        run, _ = await family14_run(tmp_path, monkeypatch)
        assert not payloads(run, "reflection_failed")
        started = payloads(run, "reflection_started")[0]
        _, record = _budget(run, started["reflection_id"])
        assert record["decision"] == "sent" and len(server.script.requests) == 2
        body = server.script.requests[1]
        assert chat_request_size(body["messages"], body["tools"]) == record["total_chars"]
        assert {"list_history", "read_history"} <= {
            tool["function"]["name"] for tool in body["tools"]
        }
        _clean(run)


async def test_the_opening_keeps_c1_and_c3_under_every_budget_form(tmp_path, monkeypatch):
    from evolution.tree import campaign as campaign_module
    from evolution.tree.roles.reflector.reflect_brief import (
        reflect_brief_payload,
        render_reflect_opening,
        with_history,
    )
    from evolution.tree.roles.reflector.reflect_history import (
        elision_steps,
        history_sentences,
        initial_layout,
        render_history,
    )
    from tests import campaign_drivers as fixtures
    from tests.tree.roles.reflector.test_reflect_history_scale import long_world, source

    workspace = fixtures.write_workspace

    def with_documents(*args, **kwargs):
        return workspace(
            *args, **{**kwargs, "environment_docs": {"guide.md": "Target investigation guide.\n"}}
        )

    monkeypatch.setattr(fixtures, "write_workspace", with_documents)
    captured = []
    original = campaign_module.build_adaptive_reflect_brief

    def capture(*args, **kwargs):
        brief = original(*args, **kwargs)
        captured.append(brief)
        return brief

    monkeypatch.setattr(campaign_module, "build_adaptive_reflect_brief", capture)
    await family14_run(
        tmp_path,
        monkeypatch,
        NONPROMOTING,
        campaign={"rounds": 2},
        reflector=dumping_reflector(tmp_path / "requests"),
    )
    brief = captured[1]
    baseline = reflect_brief_payload(brief)
    assert baseline["version"] == "reflect_brief/5" and baseline["groups"] and baseline["portion"]
    assert baseline["documents"]
    built = source(long_world(tmp_path / "long"))
    start = initial_layout(36)
    stable = None
    forms = set()
    for layout in [start, *elision_steps(start)]:
        history = render_history(built, layout)
        moved = with_history(brief, history)
        payload = reflect_brief_payload(moved)
        assert {k: v for k, v in payload.items() if k != "history"} == {
            k: v for k, v in baseline.items() if k != "history"
        }
        opening = render_reflect_opening(moved, (), ("skills/**",), editor_max_changed_files=3)
        lines = opening.splitlines()
        assert lines[-1] == canonical_json(payload).decode()
        assert "3" in lines[-2]  # Editor budget remains immediately before payload.
        fixed = [line for line in lines[:-1] if line not in history_sentences(history)]
        if stable is None:
            stable = fixed
        assert fixed == stable
        forms.add(layout.form)
    assert forms == {"full", "reduced", "core"}


@pytest.mark.parametrize("kind", ["chat", "subprocess"])
async def test_a_b4_chat_reflector_run_needs_one_more_rebase(tmp_path, monkeypatch, kind):
    from evolution.tree import campaign as campaign_module
    from evolution.tree.roles import seals as editor_seal
    from tests.test_tree_campaign_family14_visibility_change import _decision, _version

    captured = []
    original = campaign_module.build_adaptive_reflect_brief

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(campaign_module, "build_adaptive_reflect_brief", capture)
    base_url, server = fake_chat_server.start(
        [
            fake_chat_server.tool_call("list_files"),
            fake_chat_server.tool_call(
                "finish_reflection", {"groups": DIAGNOSIS["groups"], "declined": False}
            ),
        ]
    )
    try:
        if kind == "chat":
            with_chat_reflector(monkeypatch, chat_reflector(base_url))
        options = {"campaign": {"rounds": 2}, "reflector": dumping_reflector(tmp_path / "requests")}
        with monkeypatch.context() as patch:
            patch.setattr(editor_seal, "REFLECT_TOOL_SCHEMA", "reflect_tools/1")
            _version(patch, "2.0.0b4")
            _cut_hook(patch, "after_attempt_input_frozen")
            with pytest.raises(Crash):
                await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
        with pytest.raises(EvolveRefused) as caught:
            await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
        refusal = caught.value
        fields = {difference.field for difference in refusal.differences}
        assert fields == (
            {"producing_version", "identity.reflector.digest"}
            if kind == "chat"
            else {"producing_version"}
        )
        assert refusal.code == "run_identity_changed" and refusal.effects == ("rebase",)
        decision = _decision(
            refusal, None, reason="accept current producing version and Reflector tool identity"
        )
        assert decision.effect is None
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, NONPROMOTING, authorizations=[decision], **options
        )
        assert snapshot.finished is not None and len(captured) >= 2
        assert (
            captured[-1].version == "reflect_brief/5"
            and captured[-1].payload["history"]["version"] == "reflect_history/1"
        )
        assert payloads(run, "identity_rebased")[-1]["changed_fields"] == sorted(fields)
        _clean(run)
    finally:
        server.shutdown()
        server.server_close()


async def test_an_agent_cli_reads_history_via_its_installed_launcher(tmp_path, monkeypatch):
    import sys

    from evolution.tree.campaign import _Campaign

    expected = []
    original = _Campaign._history_source

    def capture(self, snapshot):
        built = original(self, snapshot)
        expected.append(
            history_payload(
                project_history(
                    snapshot,
                    measurements=built.measurements,
                    store=built.store,
                    artifact_policy=built.artifact_policy,
                    corpus=built.corpus,
                    policy=built.policy,
                    limits={**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1},
                    excerpts=True,
                    visible=built.visible,
                ),
                excerpts=True,
            )
        )
        return built

    monkeypatch.setattr(_Campaign, "_history_source", capture)
    out = tmp_path / "agent-requests.jsonl"
    program = tmp_path / "reflector-cli"
    program.write_text(
        f"#!{sys.executable}\n"
        + "import json, subprocess, sys\nfrom pathlib import Path\n"
        + "sys.stdin.read()\nwork=Path(sys.argv[sys.argv.index('-C')+1])\nrequest=json.loads((work/'.evolution/REFLECT_REQUEST.json').read_text())\n"
        + "def read(name,args):\n    return json.loads(subprocess.check_output([*request['readers'][name],json.dumps(args)]))\n"
        + "listing=read('list_history',{})\nattempts=[e for e in listing['entries'] if e['kind']=='attempt']\n"
        + "if attempts:\n    history_id=attempts[0]['history_id']\n    page=read('read_history',{'history_id':history_id})\n    content=page['content']\n    while page['next_offset'] is not None:\n        page=read('read_history',{'history_id':history_id,'offset':page['next_offset']})\n        content+=page['content']\n    request['saved']={'history_id':history_id,'content':content}\n"
        + f"with Path({str(out)!r}).open('a') as output:\n    output.write(json.dumps(request)+'\\n')\n"
        + f"(work/'.evolution/REFLECTION.json').write_text({json.dumps(DIAGNOSIS)!r})\n"
        + "Path(sys.argv[sys.argv.index('-o')+1]).write_text('done')\nprint(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'read closed history','exit_code':0}}))\n"
    )
    program.chmod(0o755)
    with_chat_reflector(
        monkeypatch,
        {"kind": "agent_cli", "model": "codex", "agent_bin": str(program)},
    )
    run, snapshot = await family14_run(tmp_path, monkeypatch, NONPROMOTING, campaign={"rounds": 2})
    assert snapshot.finished is not None
    requests = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(requests) == len(expected) == 2
    saved = requests[-1]["saved"]
    legacy = next(
        a
        for r in expected[-1]["rounds"]
        for a in r["attempts"]
        if saved["history_id"] == "attempt:" + a["attempt_id"]
    )
    assert legacy["result"]["status"] == "present" and len(legacy["result"]["cases"]) == 2
    for case, scores in zip(
        legacy["result"]["cases"], ([0, 0.05, 0.1], [0.8, 0.9, 0.85]), strict=True
    ):
        assert [r["terminal"] for r in case["repeats"]] == ["completed"] * 3 + ["not_started"]
        assert [r["primary_score"] for r in case["repeats"]] == [*scores, None]
    assert saved["content"] == canonical_json(legacy).decode()
    assert any(
        e.get("history_id") == saved["history_id"] and e["operation"] == "read_history"
        for e in payloads(run, "reflection_finished")[-1]["reads"]["entries"]
    )
    for started, request in zip(payloads(run, "reflection_started"), requests, strict=True):
        path, record = _budget(run, started["reflection_id"])
        assert (
            stat.S_IMODE(path.stat().st_mode) == 0o600
            and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        )
        assert record["components"]["history"] == 2 * serialized_chars(
            request["evidence"]["history"]
        )
        assert record["decision"] == "sent" and "hello" not in path.read_text()
    _clean(run)


async def test_later_turns_are_unchanged(tmp_path, monkeypatch):
    from copy import deepcopy

    from evolution.tree.roles.reflector import chat_reflector as role

    before_compaction = []
    calls = []
    original = role._compact_messages

    def spy(messages, **kwargs):
        calls.append(kwargs.copy())
        if len(messages) >= 2:
            assistant = messages[-2]
            if assistant.get("tool_calls") and assistant["tool_calls"][0]["id"] == "history-0":
                before_compaction.append(deepcopy(messages[-2:]))
        return original(messages, **kwargs)

    monkeypatch.setattr(role, "_compact_messages", spy)
    stage = 0
    protected = []

    def scripted(request):
        nonlocal stage
        if len(request["messages"]) < 2:
            return fake_chat_server.tool_call("list_files")
        history = json.loads(request["messages"][1]["content"].splitlines()[-1])["history"]
        if history["rounds_closed"] == 0:
            stage += 1
            return (
                fake_chat_server.tool_call("list_files")
                if stage == 1
                else fake_chat_server.tool_call(
                    "finish_reflection", {"groups": DIAGNOSIS["groups"], "declined": False}
                )
            )
        if not protected:
            response = fake_chat_server.tool_call("read_history", {"history_id": "attempt:r01-a01"})
            response["choices"][0]["message"]["tool_calls"][0]["id"] = "history-0"
            response["choices"][0]["message"]["reasoning_content"] = "continuation " * 7600
            protected.append(response["choices"][0]["message"])
            return response
        return {"status": 400}

    base_url, server = fake_chat_server.start([scripted])
    try:
        with_chat_reflector(
            monkeypatch, chat_reflector(base_url, compact=100_000, message_max=1000)
        )
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, NONPROMOTING, campaign={"rounds": 2}
        )
        assert snapshot.finished is None
        assert payloads(run, "reflection_failed")[-1]["reason"] == "provider_rejected"
        assert payloads(run, "campaign_suspended")[-1]["reason"] == "reflector_provider_failed"
        assert calls and all(
            c == {"trigger_chars": 100_000, "message_max_chars": 1000} for c in calls
        )
        last = server.script.requests[-1]["messages"]
        assistant = next(
            i
            for i, m in enumerate(last)
            if m.get("tool_calls") and m["tool_calls"][0]["id"] == "history-0"
        )
        exchange = last[assistant:]
        assert len(exchange) == 2
        assert len(before_compaction) == 1
        assert canonical_json(exchange) == canonical_json(before_compaction[0])
        admitted = json.loads(json.loads(exchange[1]["content"])["content"])
        assert admitted["result"]["status"] == "present"
        assert len(admitted["result"]["cases"]) == 2
        assert all(
            [repeat["terminal"] for repeat in case["repeats"]]
            == ["completed"] * 3 + ["not_started"]
            for case in admitted["result"]["cases"]
        )
        assert len(json.dumps(exchange[0], ensure_ascii=False)) < 100_000
        assert len(json.dumps(exchange, ensure_ascii=False)) > 100_000
        assert exchange[0]["reasoning_content"] == protected[0]["reasoning_content"]
        assert exchange[0]["tool_calls"] == protected[0]["tool_calls"]
        assert all(
            m["role"] == "tool"
            and "[context truncated" not in m["content"]
            and json.loads(m["content"])["content"]
            for m in exchange[1:]
        )
        _clean(run)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("direction", ["removed", "added"])
async def test_adding_or_removing_the_reflector_key_needs_one_rebase(
    tmp_path, monkeypatch, direction
):
    """#290 §4.2: a family-14 run created with `api_key_env` and resumed
    without it (or the reverse) is refused once as `run_identity_changed` on
    the Reflector's identity alone, a `rebase` one authorization accepts; the
    run then finishes and its later Reflector requests carry the new
    credential state on the wire."""

    from tests.test_tree_campaign_family14_visibility_change import _decision
    from tests.test_tree_chat_credentials import (
        ENVIRONMENT,
        assert_credential_free,
        assert_sends_only,
    )

    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    base_url, server = fake_chat_server.start(
        [
            fake_chat_server.tool_call("list_files"),
            fake_chat_server.tool_call(
                "finish_reflection", {"groups": DIAGNOSIS["groups"], "declined": False}
            ),
        ]
    )
    section = chat_reflector(base_url)
    if direction == "added":
        section.pop("api_key_env")
    try:
        # The patched document reads `section` when each run writes it.
        with_chat_reflector(monkeypatch, section)
        options = {"campaign": {"rounds": 2}}
        with monkeypatch.context() as patch:
            _cut_hook(patch, "after_attempt_input_frozen")
            with pytest.raises(Crash):
                await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
        before = list(server.script.headers)
        assert before, "the first run made no Reflector request"
        if direction == "removed":
            assert_sends_only(before, "k")
            section.pop("api_key_env")
        else:
            assert_credential_free(before)
            section["api_key_env"] = "E1_KEY"

        with pytest.raises(EvolveRefused) as caught:
            await family14_run(tmp_path, monkeypatch, NONPROMOTING, **options)
        refusal = caught.value
        fields = {difference.field for difference in refusal.differences}
        assert fields == {"identity.reflector.digest"}
        assert refusal.code == "run_identity_changed" and refusal.effects == ("rebase",)
        decision = _decision(refusal, None, reason="accept the Reflector credential change")
        assert decision.effect is None
        run, snapshot = await family14_run(
            tmp_path, monkeypatch, NONPROMOTING, authorizations=[decision], **options
        )
        assert snapshot.finished is not None
        assert snapshot.finished["outcome"] == "completed"
        assert [p["changed_fields"] for p in payloads(run, "identity_rebased")] == [sorted(fields)]
        after = server.script.headers[len(before) :]
        assert after, "the resumed run made no Reflector request"
        if direction == "removed":
            assert_credential_free(after)
        else:
            assert_sends_only(after, "k")
        _clean(run)
    finally:
        server.shutdown()
        server.server_close()
