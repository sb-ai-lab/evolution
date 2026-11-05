"""The async grouping-model runtime (model error grouping)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution import prompts
from evolution.tree.campaign_identity import FINAL_ADAPTATION_PROFILE, build_adaptation_identity
from evolution.tree.roles.config import FieldsGroupingSection, ModelGroupingSection
from evolution.tree.roles.editor.chat_editor import ChatCredentials
from evolution.tree.roles.grouping import model_grouping_runtime as grouping_runtime
from evolution.tree.roles.grouping.grouping_runtime import (
    AdaptiveCampaignRuntimeConfig,
    GroupingRuntime,
)
from evolution.tree.roles.grouping.model_grouping import (
    BUNDLE_OVERHEAD,
    FallbackOutcome,
    build_bundle,
    build_case_profiles,
    parse_map_response,
)
from evolution.tree.roles.grouping.model_grouping_runtime import (
    MAP_LEAD,
    ModelGroupingOutcome,
    new_grouping_client,
    run_model_grouping,
    split_prompt,
)
from evolution.tree.roles.grouping.reflect_grouping import (
    FieldFeature,
    GroupingRecord,
    build_registry,
)
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.role_failures import GrouperRoleFailure, GroupingExecutionError
from evolution.tree.roles.workspace import open_attempt_log
from tests.campaign_fakes import model_grouping_identity_payload
from tests.model_grouping_fakes import (
    ScriptedClient,
    ScriptedReply,
    bad_request,
    grouping_answer,
    is_repair,
    map_group,
    rate_limited,
    server_error,
)

PROMPT = "map system\n\n## Reduce\n\nreduce system"
SECRET = "SEKRIT-value"
NEEDLE = "ORACLE-needle"


def rec(case, repeat, *, failing, available=True, criteria=("c1",), answer="a"):
    observation = {
        "terminal": "completed",
        "critical": not failing,
        "failed_criteria": list(criteria) if failing else [],
        "primary_score": 0.0,
        "verdicts": [],
        "answer_text": [answer],
    }
    return GroupingRecord(
        f"{case}#{repeat}",
        case,
        repeat,
        failing,
        available,
        {"case": {}, "public": {}, "observation": observation, "feedback": {}},
    )


REG = build_registry(
    "b" * 64,
    [
        rec("a", 1, failing=True, criteria=("x",)),
        rec("a", 2, failing=True, criteria=("y",)),
        rec("a", 3, failing=False),
        rec("b", 1, failing=True, criteria=("x",)),
    ],
)

VALID = grouping_answer(
    map_group("g1", [("a#1", "rule"), ("b#1", "rule")], title="Wrong rule"),
    map_group("g2", [("a#2", "product")]),
)


def section(**overrides) -> ModelGroupingSection:
    values = {
        "backend": "openai_chat",
        "model": "grouper-small",
        "base_url": "https://llm.example.test/v1",
        "api_key_env": "GROUPER_KEY",
        "timeout_seconds": 600,
        "concurrency": 4,
        "max_input_chars": 200000,
        "max_output_tokens": 8192,
        "retry": RoleCallRetry(3, 90, 1, 2, 15),
    }
    values.update(overrides)
    return ModelGroupingSection(**values)


def two_chunk_limit() -> int:
    """A `max_input_chars` that fits either case bundle alone, never both."""

    profiles = build_case_profiles(REG, metric_primary="score", representatives_per_case=3)
    sizes = [len(build_bundle(p, REG, max_chars=10**9).text) for p in profiles]
    map_system, _ = split_prompt(PROMPT)
    reserve = getattr(grouping_runtime, "REPAIR_RESERVE", 0)
    limit = len(map_system) + len(MAP_LEAD) + reserve + max(sizes) + 2 * BUNDLE_OVERHEAD + 8
    assert (
        max(sizes) + BUNDLE_OVERHEAD
        <= limit - len(map_system) - len(MAP_LEAD) - reserve
        < sum(sizes) + 2 * BUNDLE_OVERHEAD
    )
    return limit


async def run(
    client,
    tmp_path,
    *,
    registry=REG,
    sec=None,
    cancelled=lambda: False,
    fallback_needle=False,
    oracle_needle=lambda text: NEEDLE in text,
    clock=lambda: 0.0,
    family14=False,
    prompt=PROMPT,
) -> ModelGroupingOutcome:
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=(SECRET,))
    try:
        return await run_model_grouping(
            registry,
            section=sec or section(),
            prompt=prompt,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials("key", None),
            log=log,
            deadline=600.0,
            cancelled=cancelled,
            forbidden_secret=lambda text: SECRET in text,
            oracle_needle=oracle_needle,
            client_factory=client.factory(),
            clock=clock,
            sleep=_no_sleep,
            rng=lambda: 0.0,
            family14=family14,
        )
    finally:
        log.close()


async def _no_sleep(_seconds: float) -> None:
    return None


def log_records(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "logs" / "grouping-op.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def user_texts(request: dict) -> list[str]:
    return [m["content"] for m in request["messages"] if m["role"] == "user"]


def is_map(request: dict) -> bool:
    return request["messages"][0]["content"] == "map system"


async def test_valid_single_chunk_admits_without_fallback(tmp_path):
    client = ScriptedClient([VALID])
    outcome = await run(client, tmp_path)
    assert outcome.fallback is None
    assert outcome.calls == {"map_requests": 1, "reduce": "none", "attempts": 1, "repairs": 0}
    (request,) = client.requests
    assert request["model"] == "grouper-small"
    assert request["max_tokens"] == 8192
    assert user_texts(request)[0].startswith(MAP_LEAD)
    assert {n.member_ids for n in outcome.grouped.neighborhoods} == {("a#1", "b#1"), ("a#2",)}
    phases = [record["phase"] for record in log_records(tmp_path)]
    assert phases == ["request", "provider_attempt", "response"]
    assert client.closed


async def test_a_conversation_repair_is_kept_when_it_fits(tmp_path):
    client = ScriptedClient(["not json at all", VALID])
    outcome = await run(client, tmp_path)
    assert outcome.fallback is None
    assert outcome.calls["repairs"] == 1
    assert outcome.calls["attempts"] == 2
    second = client.requests[1]["messages"]
    assert len(second) == 4
    assert second[-2] == {"role": "assistant", "content": "not json at all"}
    assert second[-1]["role"] == "user"
    assert "invalid_json" in second[-1]["content"]
    assert "complete corrected JSON" in second[-1]["content"]
    repairs = [r for r in log_records(tmp_path) if r["phase"] == "repair"]
    assert repairs == [
        {
            "phase": "repair",
            "request_id": "map-0-1",
            "form": "conversation",
            "defects": ["invalid_json"],
        }
    ]


async def test_repair_exhausted_uses_uncovered_fallback(tmp_path):
    partial = grouping_answer(map_group("g1", [("a#1", "rule")]))
    client = ScriptedClient([partial, partial, partial])
    outcome = await run(client, tmp_path)
    assert len(client.requests) == 3
    assert outcome.calls["repairs"] == 2
    assert outcome.fallback == FallbackOutcome("singletons", "uncovered", "invalid_output")
    assert ("a#1",) in {n.member_ids for n in outcome.grouped.neighborhoods}


async def test_provider_429_exhaustion_falls_back_all_and_sends_no_extra_request(tmp_path):
    client = ScriptedClient([rate_limited(), rate_limited(), rate_limited()])
    outcome = await run(client, tmp_path)
    assert len(client.requests) == 3
    assert outcome.fallback is not None
    assert outcome.fallback.scope == "all"
    assert outcome.fallback.reason == "provider_unavailable"
    assert outcome.calls["attempts"] == 3
    assert all(n.description is None for n in outcome.grouped.neighborhoods)


async def test_provider_rejection_uses_declared_fields_fallback(tmp_path):
    features = (FieldFeature("c", "observation", "/failed_criteria", "criteria"),)
    sec = section(fallback=FieldsGroupingSection(features))
    client = ScriptedClient([bad_request()])
    outcome = await run(client, tmp_path, sec=sec)
    assert outcome.fallback == FallbackOutcome("fields", "all", "provider_rejected")
    assert len(client.requests) == 1


async def test_retry_preserves_map_state(tmp_path):
    sec = section(max_input_chars=two_chunk_limit(), concurrency=1)
    first = grouping_answer(
        map_group("g1", [("a#1", "rule")], title="A rule"), map_group("g2", [("a#2", "product")])
    )
    second = grouping_answer(map_group("g1", [("b#1", "rule")], title="B rule"))
    client = ScriptedClient([first, server_error(), second, json.dumps({"merges": []})])
    outcome = await run(client, tmp_path, sec=sec)
    map_requests = [r for r in client.requests if is_map(r)]
    assert len(map_requests) == 3
    assert '"case_id":"a"' in user_texts(map_requests[0])[0]
    assert '"case_id":"b"' in user_texts(map_requests[1])[0]
    assert map_requests[1] == map_requests[2]
    assert outcome.fallback is None
    assert outcome.calls == {"map_requests": 2, "reduce": "done", "attempts": 4, "repairs": 0}
    assert {n.member_ids for n in outcome.grouped.neighborhoods} == {("a#1",), ("a#2",), ("b#1",)}


async def test_each_provider_attempt_records_its_status_and_code(tmp_path):
    """A failed attempt's HTTP status (none without a response) and
    provider code; a successful one has neither; the outcome is unchanged."""

    import openai

    client = ScriptedClient([server_error(), openai.APITimeoutError(request=None), VALID])
    outcome = await run(client, tmp_path)
    assert outcome.fallback is None
    attempts = [
        (r["outcome"], r["http_status"], r["provider_code"])
        for r in log_records(tmp_path)
        if r["phase"] == "provider_attempt"
    ]
    assert attempts == [("http_5xx", 503, None), ("timeout", None, None), ("ok", None, None)]


async def test_reduce_merges_whole_map_groups(tmp_path):
    sec = section(max_input_chars=two_chunk_limit())
    first = grouping_answer(map_group("g1", [("a#1", "rule")]), map_group("g2", [("a#2", "x")]))
    second = grouping_answer(map_group("g1", [("b#1", "rule")]))
    merge = json.dumps({"merges": [{"groups": ["m0-g1", "m1-g1"]}]})
    client = ScriptedClient([first, second, "prose", merge])
    outcome = await run(client, tmp_path, sec=sec)
    reduce_request = client.requests[2]
    assert reduce_request["messages"][0]["content"] == "reduce system"
    assert "m0-g1" in user_texts(reduce_request)[0]
    assert outcome.calls["reduce"] == "done"
    assert outcome.calls["repairs"] == 1
    assert {n.member_ids for n in outcome.grouped.neighborhoods} == {("a#1", "b#1"), ("a#2",)}


async def test_reduce_invalid_keeps_map_groups(tmp_path):
    sec = section(max_input_chars=two_chunk_limit())
    first = grouping_answer(map_group("g1", [("a#1", "rule")]), map_group("g2", [("a#2", "x")]))
    second = grouping_answer(map_group("g1", [("b#1", "rule")]))
    client = ScriptedClient([first, second, "prose", "prose", "prose"])
    outcome = await run(client, tmp_path, sec=sec)
    assert outcome.calls["reduce"] == "invalid"
    assert outcome.fallback is None
    assert len(outcome.grouped.neighborhoods) == 3


async def test_concurrency_does_not_change_result(tmp_path):
    first = grouping_answer(
        map_group("g1", [("a#1", "rule")], title="A rule"), map_group("g2", [("a#2", "product")])
    )
    second = grouping_answer(map_group("g1", [("b#1", "rule")], title="B rule"))

    def answer(kw):
        text = user_texts(kw)[0]
        if not is_map(kw):
            return json.dumps({"merges": []})

        async def delayed():
            # Case a is answered last, so completion order is the reverse of chunk order.
            await asyncio.sleep(0.05 if '"case_id":"a"' in text else 0.0)
            return first if '"case_id":"a"' in text else second

        return delayed()

    results = []
    for concurrency in (1, 4):
        sec = section(max_input_chars=two_chunk_limit(), concurrency=concurrency)
        client = ScriptedClient([answer] * 3)
        outcome = await run(client, tmp_path / str(concurrency), sec=sec)
        results.append(outcome.grouped.result)
    assert results[0] == results[1]


async def test_secret_in_model_text_is_integrity_failure(tmp_path):
    leaked = grouping_answer(
        map_group("g1", [("a#1", "rule"), ("b#1", "rule")], title=f"has {SECRET}"),
        map_group("g2", [("a#2", "product")]),
    )
    client = ScriptedClient([leaked])
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(client, tmp_path)
    assert excinfo.value.code == "secret_detected"
    assert SECRET not in (tmp_path / "logs" / "grouping-op.jsonl").read_text()


async def test_oracle_needle_drops_description_only(tmp_path):
    needled = grouping_answer(
        map_group("g1", [("a#1", "rule"), ("b#1", "rule")], title=f"has {NEEDLE}"),
        map_group("g2", [("a#2", "product")]),
    )
    client = ScriptedClient([needled])
    outcome = await run(client, tmp_path)
    assert outcome.fallback is None
    by_id = {n.member_ids: n for n in outcome.grouped.neighborhoods}
    assert by_id[("a#1", "b#1")].description is None
    assert by_id[("a#2",)].description is not None


async def test_oracle_needle_in_a_variant_label_is_replaced(tmp_path):
    needled = grouping_answer(
        map_group("g1", [("a#1", NEEDLE), ("b#1", "rule")]),
        map_group("g2", [("a#2", "product")]),
    )
    client = ScriptedClient([needled])
    outcome = await run(client, tmp_path)
    assert all(NEEDLE not in label for label in outcome.grouped.variants.values())
    assert {n.member_ids for n in outcome.grouped.neighborhoods} == {("a#1", "b#1"), ("a#2",)}


async def test_input_too_large_falls_back_without_request(tmp_path):
    client = ScriptedClient([])
    outcome = await run(client, tmp_path, sec=section(max_input_chars=len(PROMPT) + 40))
    assert client.requests == []
    assert outcome.fallback == FallbackOutcome("singletons", "all", "input_too_large")
    assert outcome.calls == {"map_requests": 0, "reduce": "none", "attempts": 0, "repairs": 0}


async def test_cancel_before_request(tmp_path):
    client = ScriptedClient([VALID])
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(client, tmp_path, cancelled=lambda: True)
    assert excinfo.value.code == "cancelled"
    assert client.requests == []


async def test_no_failing_available_observation_sends_nothing(tmp_path):
    registry = build_registry(
        "c" * 64,
        [rec("a", 1, failing=False), rec("a", 2, failing=True, available=False)],
    )
    client = ScriptedClient([])
    outcome = await run(client, tmp_path, registry=registry)
    assert client.requests == []
    assert outcome.fallback is None
    assert outcome.grouped.neighborhoods == ()


async def test_deadline_elapsed_between_requests_falls_back(tmp_path):
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=())
    client = ScriptedClient([])
    try:
        outcome = await run_model_grouping(
            REG,
            section=section(),
            prompt=PROMPT,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials("key", None),
            log=log,
            deadline=10.0,
            cancelled=lambda: False,
            forbidden_secret=lambda _text: False,
            oracle_needle=lambda _text: False,
            client_factory=client.factory(),
            clock=lambda: 10.0,
        )
    finally:
        log.close()
    assert client.requests == []
    assert outcome.fallback == FallbackOutcome("singletons", "all", "deadline_exhausted")


def test_grouping_client_disables_sdk_retries():
    client = new_grouping_client(section(), ChatCredentials("key", None))
    assert client.max_retries == 0
    assert client.timeout is None


def test_prompt_mentions_no_metric_direction():
    text = prompts.TREE_GROUPING_MODEL_SYSTEM
    for phrase in ("higher is better", "lower is better", "1.0"):
        assert phrase not in text
    map_system, reduce_system = split_prompt(text)
    assert map_system and reduce_system
    assert "## Reduce" not in map_system and "## Reduce" not in reduce_system
    for required in (
        "root_cause",
        "contrast_omitted",
        "omitted_failing",
        "variant",
        "exactly once",
    ):
        assert required in map_system


async def test_exhaustion_in_one_chunk_cancels_the_other_map_requests(tmp_path):
    sec = section(max_input_chars=two_chunk_limit(), concurrency=1)
    client = ScriptedClient([rate_limited(), rate_limited(), rate_limited(), VALID])
    outcome = await run(client, tmp_path, sec=sec)
    assert len(client.requests) == 3
    assert all('"case_id":"a"' in user_texts(r)[0] for r in client.requests)
    assert outcome.fallback == FallbackOutcome("singletons", "all", "provider_unavailable")
    assert outcome.calls["map_requests"] == 1


async def test_deadline_passing_inside_a_provider_call_is_deadline_exhausted(tmp_path):
    now = [0.0]

    async def slow(kw):
        now[0] = 700.0  # past the operation deadline of 600
        raise TimeoutError

    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=())
    client = ScriptedClient([slow])
    try:
        outcome = await run_model_grouping(
            REG,
            section=section(),
            prompt=PROMPT,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials("key", None),
            log=log,
            deadline=600.0,
            cancelled=lambda: False,
            forbidden_secret=lambda _text: False,
            oracle_needle=lambda _text: False,
            client_factory=client.factory(),
            clock=lambda: now[0],
            sleep=_no_sleep,
            rng=lambda: 0.0,
        )
    finally:
        log.close()
    assert len(client.requests) == 1
    assert outcome.fallback == FallbackOutcome("singletons", "all", "deadline_exhausted")


async def test_secret_response_text_is_never_logged(tmp_path):
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=())  # no log redaction
    client = ScriptedClient([f"prose {SECRET}"])
    try:
        with pytest.raises(GroupingExecutionError) as excinfo:
            await run_model_grouping(
                REG,
                section=section(),
                prompt=PROMPT,
                metric_primary="score",
                policy_identity="p",
                credentials=ChatCredentials("key", None),
                log=log,
                deadline=600.0,
                cancelled=lambda: False,
                forbidden_secret=lambda text: SECRET in text,
                oracle_needle=lambda _text: False,
                client_factory=client.factory(),
                clock=lambda: 0.0,
            )
    finally:
        log.close()
    assert excinfo.value.code == "secret_detected"
    records = log_records(tmp_path)
    assert SECRET not in json.dumps(records)
    assert records[-1] == {
        "phase": "response",
        "request_id": "map-0-0",
        "text": "[redacted]",
        "finish_reason": None,
        "usage": None,
    }


USAGE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "completion_tokens_details": {"reasoning_tokens": 3},
}


async def test_response_records_carry_finish_reason_and_usage(tmp_path):
    client = ScriptedClient([ScriptedReply(VALID, finish_reason="stop", usage=USAGE)])
    await run(client, tmp_path)
    response = [r for r in log_records(tmp_path) if r["phase"] == "response"][0]
    assert response["finish_reason"] == "stop"
    assert response["usage"] == USAGE
    assert response["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3


async def test_response_records_null_when_provider_omits(tmp_path):
    client = ScriptedClient([VALID])
    await run(client, tmp_path)
    response = [r for r in log_records(tmp_path) if r["phase"] == "response"][0]
    assert response["finish_reason"] is None
    assert response["usage"] is None


async def test_outer_cancellation_propagates_and_closes_the_client(tmp_path):
    started = asyncio.Event()

    async def hang(kw):
        started.set()
        await asyncio.Event().wait()

    client = ScriptedClient([hang])
    task = asyncio.create_task(run(client, tmp_path))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed
    assert len(client.requests) == 1


async def test_exhaustion_cancels_an_in_flight_sibling_request(tmp_path):
    sec = section(max_input_chars=two_chunk_limit(), concurrency=2)
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    def answer(kw):
        async def respond():
            if '"case_id":"b"' in user_texts(kw)[0]:
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    sibling_cancelled.set()
                    raise
            await sibling_started.wait()
            return rate_limited()

        return respond()

    client = ScriptedClient([answer] * 4)
    outcome = await run(client, tmp_path, sec=sec)
    assert sibling_cancelled.is_set()
    assert outcome.fallback == FallbackOutcome("singletons", "all", "provider_unavailable")
    assert outcome.calls["map_requests"] == 2


async def test_map_requests_counts_only_requests_sent(tmp_path):
    client = ScriptedClient([])
    log = open_attempt_log(tmp_path / "logs", "grouping-op", secrets=())
    try:
        outcome = await run_model_grouping(
            REG,
            section=section(),
            prompt=PROMPT,
            metric_primary="score",
            policy_identity="p",
            credentials=ChatCredentials("key", None),
            log=log,
            deadline=10.0,
            cancelled=lambda: False,
            forbidden_secret=lambda _text: False,
            oracle_needle=lambda _text: False,
            client_factory=client.factory(),
            clock=lambda: 10.0,
        )
    finally:
        log.close()
    assert outcome.calls["map_requests"] == 0


# --- Final review Important 1: a large valid grouping fits the sparse caps ---

_CYRILLIC = "Агент путает правило расчёта и продукт клиента; "


def _long(chars: int, salt: str) -> str:
    return (salt + " " + _CYRILLIC * (chars // len(_CYRILLIC) + 1))[:chars]


def _large_registry(cases: int, repeats: int):
    return build_registry(
        "b" * 64,
        [
            rec(f"c{case:03d}", repeat, failing=True)
            for case in range(cases)
            for repeat in range(1, repeats + 1)
        ],
    )


async def test_many_long_non_ascii_descriptions_are_fitted_not_refused(tmp_path):
    registry = _large_registry(100, 1)
    answer = grouping_answer(
        *(
            map_group(
                f"g{case}",
                [(f"c{case:03d}#1", "правило")],
                title=_long(128, f"Группа {case}"),
                failure_mode=_long(512, f"fm {case}"),
                expected_behaviour=_long(512, f"eb {case}"),
                question=_long(512, f"q {case}"),
            )
            for case in range(100)
        )
    )
    client = ScriptedClient([answer])
    outcome = await run(client, tmp_path, registry=registry, sec=section(max_input_chars=2_000_000))
    assert len(client.requests) == 1
    assert outcome.fallback is None
    neighborhoods = outcome.grouped.neighborhoods
    assert len(neighborhoods) == 100
    assert {m for n in neighborhoods for m in n.member_ids} == {
        r.evidence_id for r in registry.records
    }
    described = [n for n in neighborhoods if n.description is not None]
    assert 0 < len(described) < 100  # truncated first, then dropped at the bottom
    trims = [r for r in log_records(tmp_path) if r["phase"] == "grouping_trimmed"]
    assert len(trims) == 1
    assert trims[0]["descriptions_truncated"] == 100
    assert trims[0]["descriptions_dropped"] == 100 - len(described)


async def test_large_non_ascii_variant_set_is_fitted_not_refused(tmp_path):
    registry = _large_registry(40, 16)
    groups = [
        map_group(
            f"g{case}",
            [(f"c{case:03d}#{repeat}", _long(64, f"вариант {repeat}")) for repeat in range(1, 17)],
        )
        for case in range(40)
    ]
    client = ScriptedClient([grouping_answer(*groups)])
    outcome = await run(client, tmp_path, registry=registry, sec=section(max_input_chars=2_000_000))
    assert len(client.requests) == 1
    assert outcome.fallback is None
    assert len(outcome.grouped.neighborhoods) == 40
    assert {m for n in outcome.grouped.neighborhoods for m in n.member_ids} == {
        r.evidence_id for r in registry.records
    }
    variants = outcome.grouped.variants
    assert len(variants) == 640  # shortened, none dropped
    assert any(len(label) == 64 for label in variants.values())  # top groups untouched
    assert any(len(label) <= 12 for label in variants.values())
    # Shortening keeps distinct variants of a group distinct.
    for n in outcome.grouped.neighborhoods:
        labels = [variants[i] for i in n.member_indices if i in variants]
        assert len(set(labels)) == len(labels)
    trims = [r for r in log_records(tmp_path) if r["phase"] == "grouping_trimmed"]
    assert len(trims) == 1 and trims[0]["variants_shortened"] > 0


# --- Final review Minor 1: a repair never exceeds the request budget ---------


def packed_case():
    """One complete bundle fills the packing budget, including its overhead."""

    registry = build_registry("b" * 64, [rec("a", 1, failing=True)])
    (profile,) = build_case_profiles(registry, metric_primary="score", representatives_per_case=3)
    bundle = build_bundle(profile, registry, max_chars=10**9)
    map_system, _ = split_prompt(PROMPT)
    limit = len(map_system) + len(MAP_LEAD) + len(bundle.text) + BUNDLE_OVERHEAD + 600
    valid = grouping_answer(map_group("g1", [("a#1", "rule")]))
    # Valid JSON with an invalid top-level shape, within the output budget.
    invalid = "[" + " " * 5000 + "]"
    return registry, limit, valid, invalid


def assert_requests_fit(client, limit):
    assert all(
        sum(len(m["content"]) for m in request["messages"]) <= limit for request in client.requests
    )


async def test_a_packed_chunk_gets_a_fresh_repair_and_no_fallback(tmp_path):
    registry, limit, valid, invalid = packed_case()
    client = ScriptedClient([invalid, valid])
    outcome = await run(client, tmp_path, registry=registry, sec=section(max_input_chars=limit))
    assert outcome.calls["repairs"] == 1
    assert outcome.fallback is None
    original, repair = [r["messages"] for r in client.requests]
    assert len(repair) == 2
    assert repair[0] == original[0]
    assert repair[1]["content"].startswith(original[1]["content"] + "\n\n")
    note = repair[1]["content"].removeprefix(original[1]["content"] + "\n\n")
    assert note.startswith("An earlier answer to this request was rejected. Your answer")
    assert "not_an_object" in note
    assert invalid not in note
    assert_requests_fit(client, limit)
    records = [r for r in log_records(tmp_path) if r["phase"] == "repair"]
    assert records == [
        {"phase": "repair", "request_id": "map-0-1", "form": "fresh", "defects": ["not_an_object"]}
    ]


async def test_two_fresh_repairs_then_the_uncovered_fallback(tmp_path):
    registry, limit, _, invalid = packed_case()
    client = ScriptedClient([invalid] * 3)
    outcome = await run(client, tmp_path, registry=registry, sec=section(max_input_chars=limit))
    assert outcome.calls["repairs"] == 2
    assert outcome.fallback == FallbackOutcome("singletons", "uncovered", "invalid_output")
    assert len(client.requests) == 3
    assert all(len(r["messages"]) == 2 for r in client.requests)
    assert_requests_fit(client, limit)
    records = [r for r in log_records(tmp_path) if r["phase"] == "repair"]
    assert [r["request_id"] for r in records] == ["map-0-1", "map-0-2"]
    assert all(r["form"] == "fresh" and r["defects"] == ["not_an_object"] for r in records)


async def test_the_repair_defects_are_bounded(tmp_path):
    case = "case_" + "x" * 55
    evidence_id = case + "#1"
    registry = build_registry("b" * 64, [rec(case, 1, failing=True)])
    invalid = grouping_answer(map_group("g1", [(evidence_id, "v")] * 301))
    valid = grouping_answer(map_group("g1", [(evidence_id, "v")]))
    parsed = parse_map_response(
        invalid,
        chunk_failing=[evidence_id],
        case_of={evidence_id: case},
        provenance_available=False,
    )
    # The parser already limits the number of codes, but not their characters.
    assert len(parsed.defects) == 32
    assert len(", ".join(parsed.defects)) > 256
    client = ScriptedClient([invalid, valid])
    outcome = await run(client, tmp_path, registry=registry)
    assert outcome.fallback is None
    assert outcome.calls["repairs"] == 1
    note = client.requests[1]["messages"][-1]["content"]
    defects = note.split("validation defects: ", 1)[1].split(". Every evidence_id", 1)[0]
    assert len(defects) <= 256
    suffix = re.search(r", … and ([1-9][0-9]*) more$", defects)
    assert suffix is not None
    kept = defects[: suffix.start()].split(", ")
    assert kept == list(parsed.defects[: len(kept)])
    assert int(suffix[1]) == len(parsed.defects) - len(kept)
    records = [r for r in log_records(tmp_path) if r["phase"] == "repair"]
    assert len(records) == 1
    assert records[0]["defects"] == [*kept, defects[suffix.start() + 2 :]]
    assert len(", ".join(records[0]["defects"])) <= 256


async def test_a_repair_that_cannot_fit_is_logged_as_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(grouping_runtime, "REPAIR_RESERVE", 0, raising=False)
    registry, limit, _, invalid = packed_case()
    limit -= 600
    client = ScriptedClient([invalid])
    outcome = await run(client, tmp_path, registry=registry, sec=section(max_input_chars=limit))
    assert len(client.requests) == 1
    assert outcome.calls["repairs"] == 0
    assert outcome.fallback == FallbackOutcome("singletons", "uncovered", "invalid_output")
    assert ("a#1",) in {n.member_ids for n in outcome.grouped.neighborhoods}
    records = [r for r in log_records(tmp_path) if r["phase"] == "repair_skipped"]
    assert records == [
        {
            "phase": "repair_skipped",
            "request_id": "map-0-0",
            "reason": "size",
            "defects": ["not_an_object"],
        }
    ]
    assert_requests_fit(client, limit)


async def test_a_packed_reduce_gets_a_fresh_repair(tmp_path):
    limit = two_chunk_limit()
    answers = {
        "a": grouping_answer(map_group("g1", [("a#1", "rule"), ("a#2", "product")])),
        "b": grouping_answer(map_group("g1", [("b#1", "rule")])),
    }

    def reply(request):
        user = user_texts(request)[0]
        if is_map(request):
            return answers["a"] if '"case_id":"a"' in user else answers["b"]
        if is_repair(request):
            return json.dumps({"merges": []})
        # The answer, rather than an oversized input, forces conversation overflow.
        assert limit <= request["max_tokens"]
        return "x" * limit

    client = ScriptedClient([reply] * 4)
    outcome = await run(client, tmp_path, sec=section(max_input_chars=limit, concurrency=1))
    assert outcome.calls["reduce"] == "done"
    assert outcome.calls["repairs"] == 1
    assert len(client.requests) == 4
    assert outcome.fallback is None
    original, repair = [r["messages"] for r in client.requests[2:]]
    assert len(repair) == 2
    assert repair[0] == original[0]
    assert repair[1]["content"].startswith(original[1]["content"] + "\n\nAn earlier answer")
    assert_requests_fit(client, limit)
    records = [r for r in log_records(tmp_path) if r["phase"] == "repair"]
    assert records == [
        {
            "phase": "repair",
            "request_id": "reduce-0-1",
            "form": "fresh",
            "defects": ["invalid_merge_list"],
        }
    ]


async def test_packing_reserves_room_for_a_repair(tmp_path):
    profiles = build_case_profiles(REG, metric_primary="score", representatives_per_case=3)
    sizes = [len(build_bundle(p, REG, max_chars=10**9).text) for p in profiles]
    map_system, _ = split_prompt(PROMPT)
    budget = max(sizes) + 2 * BUNDLE_OVERHEAD + 8
    limit = len(map_system) + len(MAP_LEAD) + 600 + budget
    assert max(sizes) + BUNDLE_OVERHEAD <= budget < sum(sizes) + 2 * BUNDLE_OVERHEAD

    def reply(request):
        if not is_map(request):
            return json.dumps({"merges": []})
        user = user_texts(request)[0]
        members = [(eid, "rule") for eid in ("a#1", "a#2", "b#1") if eid in user]
        return grouping_answer(map_group("g1", members))

    client = ScriptedClient([reply] * 3)
    outcome = await run(client, tmp_path, sec=section(max_input_chars=limit))
    assert outcome.calls["map_requests"] == 2
    maps = [r for r in client.requests if is_map(r)]
    assert sorted(len(user_texts(r)[0]) - len(MAP_LEAD) for r in maps) == sorted(sizes)
    assert all(len(user_texts(r)[0]) - len(MAP_LEAD) + BUNDLE_OVERHEAD <= budget for r in maps)
    assert outcome.fallback is None
    assert getattr(grouping_runtime, "REPAIR_RESERVE", None) == 600
    assert getattr(grouping_runtime, "DEFECTS_BOUND", None) == 256


async def test_the_minimum_budget_still_groups(tmp_path):
    registry = build_registry("b" * 64, [rec("a", 1, failing=True)])
    client = ScriptedClient([grouping_answer(map_group("g1", [("a#1", "rule")]))])
    outcome = await run(
        client,
        tmp_path,
        registry=registry,
        sec=section(max_input_chars=8000),
        prompt=prompts.TREE_GROUPING_MODEL_SYSTEM,
    )
    assert outcome.fallback is None
    assert outcome.calls["map_requests"] == 1
    assert_requests_fit(client, 8000)


async def test_the_minimum_budget_groups_an_ordinary_case(tmp_path):
    """At 8000 characters the packaged prompt leaves room for a realistic case."""

    registry = build_registry(
        "b" * 64,
        [
            rec("case-0001", 1, failing=True, criteria=("format",), answer="An ordinary answer."),
            rec("case-0001", 2, failing=True, criteria=("rule",), answer="Another answer."),
        ],
    )
    answer = grouping_answer(map_group("g1", [("case-0001#1", "format"), ("case-0001#2", "rule")]))
    client = ScriptedClient([answer])
    outcome = await run(
        client,
        tmp_path,
        registry=registry,
        sec=section(max_input_chars=8000),
        prompt=prompts.TREE_GROUPING_MODEL_SYSTEM,
    )
    assert outcome.fallback is None
    assert outcome.calls == {"map_requests": 1, "reduce": "none", "attempts": 1, "repairs": 0}
    assert {n.member_ids for n in outcome.grouped.neighborhoods} == {("case-0001#1", "case-0001#2")}
    assert_requests_fit(client, 8000)


def _fresh_note(follow_up: str) -> str:
    """The note `_repair_request` appends when only the fresh form fits."""

    original = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    session = SimpleNamespace(section=SimpleNamespace(max_input_chars=10_000))
    request = grouping_runtime._repair_request(session, original, "x" * 20_000, follow_up)
    assert request is not None
    messages, form = request
    assert form == "fresh"
    assert messages[0] == original[0]
    return messages[1]["content"].removeprefix("u")


def test_the_repair_reserve_holds_the_largest_fresh_note():
    """A prompt or note change that outgrows the reserve fails here, not in a run."""

    bound = grouping_runtime.DEFECTS_BOUND
    widest, _ = grouping_runtime._bounded_defects(["d" * bound])
    assert len(widest) == bound
    many, _ = grouping_runtime._bounded_defects([f"ineligible_member:{'e' * 60}"] * 32)
    assert len(many) <= bound
    map_note = _fresh_note(grouping_runtime._MAP_REPAIR.format(defects=widest))
    reduce_note = _fresh_note(grouping_runtime._REDUCE_REPAIR)
    assert map_note.startswith("\n\nAn earlier answer to this request was rejected. ")
    assert reduce_note.startswith("\n\nAn earlier answer to this request was rejected. ")
    # 531 and 479 characters when the reserve was set to 600 (2026-10-15).
    assert len(map_note) <= grouping_runtime.REPAIR_RESERVE
    assert len(reduce_note) <= grouping_runtime.REPAIR_RESERVE


async def test_a_reduce_without_room_for_a_repair_is_skipped(tmp_path, monkeypatch):
    """An initial reduce leaves `REPAIR_RESERVE` for a fresh repair's note."""

    limit = two_chunk_limit()
    reserve = grouping_runtime.REPAIR_RESERVE
    _, reduce_system = split_prompt(PROMPT)
    sizes: list[int] = []
    reduce_input = grouping_runtime._reduce_input

    def spy(groups):
        text = reduce_input(groups)
        sizes.append(len(text))
        return text

    monkeypatch.setattr(grouping_runtime, "_reduce_input", spy)

    def script(pad):
        header = {"failure_mode": "f" * pad, "question": "q" * pad}
        answers = {
            "a": grouping_answer(map_group("g1", [("a#1", "rule"), ("a#2", "x")], **header)),
            "b": grouping_answer(map_group("g1", [("b#1", "rule")], **header)),
        }

        def reply(request):
            if not is_map(request):
                return json.dumps({"merges": []})
            return answers["a"] if '"case_id":"a"' in user_texts(request)[0] else answers["b"]

        return ScriptedClient([reply] * 3)

    sec = section(max_input_chars=limit, concurrency=1)
    probe = script(2)
    probed = await run(probe, tmp_path / "probe", sec=sec)
    assert probed.calls["reduce"] == "done"
    # Four padded fields grow the reduce input to the middle of the reserve.
    pad = 2 + (limit - reserve // 2 - len(reduce_system) - sizes[-1]) // 4
    assert pad <= 512
    client = script(pad)
    outcome = await run(client, tmp_path / "run", sec=sec)
    sent = len(reduce_system) + sizes[-1]
    assert limit - reserve < sent <= limit  # it would fit, but leaves no room to repair
    assert len(client.requests) == 2
    assert all(is_map(request) for request in client.requests)
    assert outcome.calls == {"map_requests": 2, "reduce": "skipped", "attempts": 2, "repairs": 0}
    assert outcome.fallback is None
    assert len(outcome.grouped.neighborhoods) == 2


# --- Final review Minor 4: a programming error is not a provider error -------


async def test_programming_error_in_send_propagates(tmp_path):
    client = ScriptedClient([TypeError("bug in send")])
    with pytest.raises(TypeError, match="bug in send"):
        await run(client, tmp_path)
    assert len(client.requests) == 1


# --- Final review Minor 5: a member from another chunk is never admitted -----


async def test_member_from_another_chunk_is_refused_and_never_admitted(tmp_path):
    long = "z" * 1500  # bundles large enough that a repair fits the budget
    registry = build_registry(
        "b" * 64,
        [
            rec("a", 1, failing=True, criteria=("x",), answer=long),
            rec("a", 2, failing=True, criteria=("y",), answer=long),
            rec("a", 3, failing=False),
            rec("b", 1, failing=True, criteria=("x",), answer=long),
        ],
    )
    profiles = build_case_profiles(registry, metric_primary="score", representatives_per_case=3)
    sizes = [len(build_bundle(p, registry, max_chars=10**9).text) for p in profiles]
    map_system, _ = split_prompt(PROMPT)
    # Either bundle alone plus a repair fits; both bundles never share a chunk.
    limit = len(map_system) + len(MAP_LEAD) + sum(sizes) + 2 * BUNDLE_OVERHEAD - 1
    sec = section(max_input_chars=limit, concurrency=1)
    stolen = grouping_answer(
        map_group("g1", [("a#1", "rule"), ("a#2", "product")], title="A"),
        map_group("g2", [("b#1", "rule")], title="Stolen"),
    )
    own = grouping_answer(map_group("g1", [("b#1", "rule")], title="B"))

    def reply(request):
        if not is_map(request):
            return json.dumps({"merges": []})
        return stolen if '"case_id":"a"' in user_texts(request)[0] else own

    client = ScriptedClient([reply] * 6)
    outcome = await run(client, tmp_path, registry=registry, sec=sec)
    assert sum(is_map(r) and not is_repair(r) for r in client.requests) == 2
    repairs = [r for r in client.requests if is_repair(r)]
    assert len(repairs) == 2
    assert all("ineligible_member:b#1" in r["messages"][-1]["content"] for r in repairs)
    labels = {
        n.member_ids: n.description.label
        for n in outcome.grouped.neighborhoods
        if n.description is not None
    }
    assert labels[("b#1",)] == "B"
    assert "Stolen" not in labels.values()
    assert {m for n in outcome.grouped.neighborhoods for m in n.member_ids} == {"a#1", "a#2", "b#1"}


# --- Family 14 (ADR-0034, H8 decision 9): the `chat` runtime under the flag ----
#
# Every script runs twice, once per flag, each against its own fresh client and
# log. Families 9-13 (`family14=False`) keep their fallback; family 14 raises a
# provider `GrouperRoleFailure` instead, and narrows only the empty-coverage
# content case to scope `all`. Requests, provider attempts, `calls` and the
# private log are the same under both flags.

PARTITION = {
    "profile": "corpus_partition/1",
    "mode": "implicit_train",
    "assignments": [["c1", "train"]],
    "corpus_digest": "a" * 64,
    "metadata_digest": "b" * 64,
    "train_digest": "a" * 64,
    "test_digest": "b" * 64,
}


def _past_deadline(now):
    """A provider call during which the operation deadline (600) passes."""

    async def slow(_kw):
        now[0] = 700.0
        raise TimeoutError

    return slow


def _then_past_deadline(now, answer):
    """A map answer after which the operation deadline (600) has passed, so the
    next request (the reduce) is never sent (`_DeadlineElapsed`)."""

    def respond(_kw):
        now[0] = 700.0
        return answer

    return respond


def _provider_scenarios():
    two = {"max_input_chars": two_chunk_limit(), "concurrency": 1}
    first = grouping_answer(map_group("g1", [("a#1", "rule")]), map_group("g2", [("a#2", "x")]))
    second = grouping_answer(map_group("g1", [("b#1", "rule")]))
    return {
        # name: (script(now), section overrides, clock start, family-9 reason, family-14 reason)
        "map_rate_limit_outage": (
            lambda now: [rate_limited(), rate_limited(), rate_limited()],
            {},
            0.0,
            "provider_unavailable",
            "provider_exhausted",
        ),
        "map_bad_request": (
            lambda now: [bad_request()],
            {},
            0.0,
            "provider_rejected",
            "provider_rejected",
        ),
        "map_deadline_in_role_call_retry": (
            lambda now: [_past_deadline(now)],
            {},
            0.0,
            "deadline_exhausted",
            "provider_exhausted",
        ),
        "map_deadline_before_request": (
            lambda now: [],
            {},
            600.0,
            "deadline_exhausted",
            "provider_exhausted",
        ),
        "reduce_rate_limit_outage": (
            lambda now: [first, second, rate_limited(), rate_limited(), rate_limited()],
            two,
            0.0,
            "provider_unavailable",
            "provider_exhausted",
        ),
        "reduce_bad_request": (
            lambda now: [first, second, bad_request()],
            two,
            0.0,
            "provider_rejected",
            "provider_rejected",
        ),
        "reduce_deadline_before_request": (
            lambda now: [first, _then_past_deadline(now, second)],
            two,
            0.0,
            "deadline_exhausted",
            "provider_exhausted",
        ),
        "reduce_deadline_in_role_call_retry": (
            lambda now: [first, second, _past_deadline(now)],
            two,
            0.0,
            "deadline_exhausted",
            "provider_exhausted",
        ),
    }


@pytest.mark.parametrize("name", sorted(_provider_scenarios()))
async def test_family14_provider_failure_raises_where_families_9_to_13_fall_back(tmp_path, name):
    script, overrides, start, reason9, reason14 = _provider_scenarios()[name]
    sec = section(**overrides)

    now9 = [start]
    client9 = ScriptedClient(script(now9))
    outcome = await run(client9, tmp_path / "f9", sec=sec, clock=lambda: now9[0])
    assert outcome.fallback == FallbackOutcome("singletons", "all", reason9)

    now14 = [start]
    client14 = ScriptedClient(script(now14))
    with pytest.raises(GrouperRoleFailure) as excinfo:
        await run(client14, tmp_path / "f14", sec=sec, clock=lambda: now14[0], family14=True)
    assert (excinfo.value.reason, excinfo.value.error_class) == (reason14, "provider")

    # Same traffic and the same private log under both flags.
    assert client14.requests == client9.requests
    assert client9.closed and client14.closed
    assert client14.script == client9.script
    assert log_records(tmp_path / "f14") == log_records(tmp_path / "f9")
    attempts = sum(r["phase"] == "provider_attempt" for r in log_records(tmp_path / "f9"))
    assert attempts == outcome.calls["attempts"]
    if name.startswith("reduce") and name != "reduce_deadline_before_request":
        assert len(client9.requests) >= 3  # the failure happened during reduce
    if name == "map_deadline_before_request":
        assert client9.requests == []
    if name == "reduce_deadline_before_request":
        # Both maps answered; the deadline stopped the reduce before it was sent.
        assert len(client9.requests) == 2
        assert all(is_map(r) for r in client9.requests)
        assert outcome.calls["map_requests"] == 2


def _content_scenarios():
    partial = grouping_answer(map_group("g1", [("a#1", "rule")]))
    empty = json.dumps({"groups": []})
    # `project` renders each group's description as "Observed: <failure_mode>\n
    # Expected:...\nQuestion:..." (`model_grouping._description`), and every
    # `map_group` has failure_mode "fm". `_scrub` checks each header field on its
    # own, so this predicate leaves the groups intact and only admission's
    # `oracle_leak` check on the rendered description refuses them.
    refused_everywhere = lambda text: "Observed: fm" in text  # noqa: E731
    uncovered = FallbackOutcome("singletons", "uncovered", "invalid_output")
    full = FallbackOutcome("singletons", "all", "invalid_output")
    return {
        # name: (script, run overrides, family-9 fallback, family-14 fallback)
        "valid": ([VALID], {}, None, None),
        "input_too_large": (
            [],
            {"sec": section(max_input_chars=len(PROMPT) + 40)},
            FallbackOutcome("singletons", "all", "input_too_large"),
            FallbackOutcome("singletons", "all", "input_too_large"),
        ),
        "repairs_exhausted_partial_coverage": ([partial] * 3, {}, uncovered, uncovered),
        "repairs_exhausted_empty_answer": ([empty] * 3, {}, uncovered, full),
        "every_group_refused_by_project": (
            [VALID],
            {"oracle_needle": refused_everywhere},
            full,
            full,
        ),
    }


@pytest.mark.parametrize("name", sorted(_content_scenarios()))
async def test_family14_content_outcomes_and_the_scope_all_rule(tmp_path, name):
    script, overrides, fallback9, fallback14 = _content_scenarios()[name]
    client9 = ScriptedClient(list(script))
    outcome9 = await run(client9, tmp_path / "f9", **overrides)
    client14 = ScriptedClient(list(script))
    outcome14 = await run(client14, tmp_path / "f14", family14=True, **overrides)

    assert outcome9.fallback == fallback9
    assert outcome14.fallback == fallback14
    # Only the recorded scope may differ: the admitted grouping, the counts,
    # the traffic and the private log are the same.
    assert outcome14.grouped.result == outcome9.grouped.result
    assert outcome14.calls == outcome9.calls
    assert client14.requests == client9.requests
    assert log_records(tmp_path / "f14") == log_records(tmp_path / "f9")


@pytest.mark.parametrize("family14", [True])
async def test_empty_answer_in_one_chunk_with_coverage_in_the_other_stays_uncovered(
    tmp_path, family14
):
    sec = section(max_input_chars=two_chunk_limit(), concurrency=1)
    covered_a = grouping_answer(map_group("g1", [("a#1", "rule")]), map_group("g2", [("a#2", "x")]))
    empty = json.dumps({"groups": []})
    no_merges = json.dumps({"merges": []})
    client = ScriptedClient([covered_a, empty, empty, empty, no_merges])
    outcome = await run(client, tmp_path, sec=sec, family14=family14)
    assert outcome.fallback == FallbackOutcome("singletons", "uncovered", "invalid_output")
    assert {("a#1",), ("a#2",), ("b#1",)} == {n.member_ids for n in outcome.grouped.neighborhoods}


@pytest.mark.parametrize("family14", [True])
async def test_secret_detected_outranks_a_provider_failure_under_both_flags(tmp_path, family14):
    """Chunk `a` answers with a run secret while chunk `b` is rejected by the
    provider in the same pass: the integrity failure wins under both flags."""

    arrived = {"a": asyncio.Event(), "b": asyncio.Event()}

    def reply(request):
        case = "a" if '"case_id":"a"' in user_texts(request)[0] else "b"

        async def respond():
            # Each chunk answers only once both requests are in flight.
            arrived[case].set()
            await arrived["b" if case == "a" else "a"].wait()
            return f"prose {SECRET}" if case == "a" else bad_request()

        return respond()

    sec = section(max_input_chars=two_chunk_limit(), concurrency=2)
    client = ScriptedClient([reply, reply])
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(client, tmp_path, sec=sec, family14=family14)
    assert excinfo.value.code == "secret_detected"
    assert len(client.requests) == 2  # both chunks were sent and both failed


def _chat_runtime(sec: ModelGroupingSection, prompt_sha256: str) -> GroupingRuntime:
    """A family-14 runtime: the `/7` identity with a `chat` Grouper."""

    grouping = {**model_grouping_identity_payload(), "kind": "chat", "prompt_sha256": prompt_sha256}
    identity = build_adaptation_identity(
        grouping=grouping,
        selection={"focus_cases": 16, "representatives_per_case": 2, "context_page_size": 32},
        families={"kind": "none"},
        attempt_feedback="none",
        reflection_metadata_digest="0" * 64,
        environment_docs_digest=None,
        family_membership={},
        profile=FINAL_ADAPTATION_PROFILE,
        reflection={"max_consecutive_failures": 2},
        corpus_partition=PARTITION,
    )
    return GroupingRuntime(AdaptiveCampaignRuntimeConfig(identity=identity, model_grouping=sec))


async def _chat_model(runtime, client, tmp_path, **flag):
    return await runtime.model(
        REG,
        operation="op-1",
        credentials=ChatCredentials("key", None),
        log_root=tmp_path / "reflector-logs",
        secrets=(SECRET,),
        cancelled=lambda: False,
        forbidden_secret=lambda text: SECRET in text,
        oracle_needle=lambda _text: False,
        metric_primary="score",
        client_factory=client.factory(),
        **flag,
    )


_PACKAGED = hashlib.sha256(prompts.TREE_GROUPING_MODEL_SYSTEM.encode("utf-8")).hexdigest()
_INSTANT = RoleCallRetry(3, 90, 0, 1, 0)


async def test_runtime_model_threads_the_family14_flag(tmp_path):
    runtime = _chat_runtime(section(retry=_INSTANT), _PACKAGED)
    client = ScriptedClient([rate_limited(), rate_limited(), rate_limited()])
    with pytest.raises(GrouperRoleFailure) as excinfo:
        await _chat_model(runtime, client, tmp_path / "f14", family14=True)
    assert (excinfo.value.reason, excinfo.value.error_class) == ("provider_exhausted", "provider")
    assert len(client.requests) == 3


@pytest.mark.parametrize("family14", [True])
async def test_command_changed_outranks_under_both_flags(tmp_path, family14):
    declared = tmp_path / "prompt.md"
    declared.write_text("changed\n")
    client = ScriptedClient([rate_limited()])
    runtime = _chat_runtime(section(system_prompt=declared), "0" * 64)
    with pytest.raises(GroupingExecutionError) as excinfo:
        await _chat_model(runtime, client, tmp_path, family14=family14)
    assert excinfo.value.code == "command_changed"
    assert client.requests == []


# --- Family 14 (ADR-0034): an `OSError` of the
# client's construction or of its close is part of the `chat` role call, so in
# family 14 it is `exception / unknown`; families 9-13 propagate it unchanged.


class _UnbuildableClient(ScriptedClient):
    """A provider client whose construction fails, as an SDK loading a missing
    CA bundle does (`OSError` outside the retry layer)."""

    def factory(self):
        def build(_section, _credentials):
            raise OSError(2, "No such file or directory: 'ca-bundle.pem'")

        return build


class _UnclosableClient(ScriptedClient):
    async def close(self) -> None:
        raise OSError(5, "Input/output error")


@pytest.mark.parametrize("where", ["factory", "close"])
@pytest.mark.parametrize("family14", [True])
async def test_a_client_oserror_is_the_role_calls_exception_only_in_family14(
    tmp_path, where, family14
):
    client = _UnbuildableClient([]) if where == "factory" else _UnclosableClient([VALID])
    if not family14:
        with pytest.raises(OSError) as raised:
            await run(client, tmp_path, family14=False)
        assert not isinstance(raised.value, GrouperRoleFailure)
        return
    with pytest.raises(GrouperRoleFailure) as excinfo:
        await run(client, tmp_path, family14=True)
    assert (excinfo.value.reason, excinfo.value.error_class) == ("exception", "unknown")
    assert isinstance(excinfo.value.__cause__, OSError)
    assert client.script == []


# --- A close `OSError` never replaces an
# exception that is already propagating in family 14. Each one keeps its own
# route; families 9-13 are unchanged (the close's `OSError` replaces it, as
# before).


async def _cancelled_by_the_caller(client, tmp_path, family14):
    started = asyncio.Event()

    async def hang(_kw):
        started.set()
        await asyncio.Event().wait()

    client.script.append(hang)
    task = asyncio.create_task(run(client, tmp_path, family14=family14))
    await started.wait()
    task.cancel()
    await task


async def _stopped_mid_run(client, tmp_path, family14):
    stopping: list[bool] = []

    def answer_then_stop(_kw):
        stopping.append(True)
        return "not json at all"  # a repair is owed; the stop check refuses it

    client.script.append(answer_then_stop)
    await run(client, tmp_path, cancelled=lambda: bool(stopping), family14=family14)


async def _provider_exhausted(client, tmp_path, family14):
    client.script.extend(rate_limited() for _ in range(3))
    await run(client, tmp_path, family14=family14)


_PROPAGATING = {
    "provider": (_provider_exhausted, GrouperRoleFailure),
    "stopped": (_stopped_mid_run, GroupingExecutionError),
    "cancelled": (_cancelled_by_the_caller, asyncio.CancelledError),
}


@pytest.mark.parametrize("name", sorted(_PROPAGATING))
@pytest.mark.parametrize("family14", [True])
async def test_a_close_oserror_never_replaces_a_propagating_exception_in_family14(
    tmp_path, name, family14
):
    scenario, expected = _PROPAGATING[name]
    client = _UnclosableClient([])
    if not family14:
        with pytest.raises(OSError) as raised:
            await scenario(client, tmp_path, family14)
        assert not isinstance(raised.value, GrouperRoleFailure)
        return
    with pytest.raises(expected) as excinfo:
        await scenario(client, tmp_path, family14)
    if name == "provider":
        assert (excinfo.value.reason, excinfo.value.error_class) == (
            "provider_exhausted",
            "provider",
        )
    if name == "stopped":
        assert excinfo.value.code == "cancelled"


@pytest.mark.parametrize("limit", [0, 5])
async def test_declared_grouper_map_repairs(tmp_path, limit):
    client = ScriptedClient([*["not json" for _ in range(4)], VALID])
    outcome = await run(client, tmp_path, sec=section(max_repairs=limit))
    assert outcome.calls["repairs"] == (0 if limit == 0 else 4)
    assert (outcome.fallback is None) == (limit == 5)


@pytest.mark.parametrize("limit", [0, 5])
async def test_declared_grouper_reduce_repairs(tmp_path, limit):
    first = grouping_answer(
        map_group("g1", [("a#1", "rule")], title="A rule"), map_group("g2", [("a#2", "product")])
    )
    second = grouping_answer(map_group("g1", [("b#1", "rule")], title="B rule"))
    client = ScriptedClient(
        [first, second, *["not json" for _ in range(4)], json.dumps({"merges": []})]
    )
    outcome = await run(
        client,
        tmp_path,
        sec=section(max_input_chars=two_chunk_limit(), concurrency=1, max_repairs=limit),
    )
    assert outcome.calls["repairs"] == (0 if limit == 0 else 4)
    assert outcome.calls["reduce"] == ("invalid" if limit == 0 else "done")


async def test_v15_consolidation_error_body_stays_invalid_output(tmp_path):
    # The historical text path still returns empty text for an error body.
    from evolution.tree.roles.grouping.model_grouping_runtime import _Session

    class ErrorBodyClient:
        chat = property(lambda self: SimpleNamespace(completions=self))

        async def create(self, **kwargs):
            return SimpleNamespace(error={"code": 503, "message": "PRIVATE-BATCH-MESSAGE"})

    with open_attempt_log(tmp_path / "logs", "legacy", secrets=()) as log:
        session = _Session(
            ErrorBodyClient(),
            section(),
            log,
            600,
            lambda: False,
            lambda _: False,
            lambda: 0,
            _no_sleep,
            lambda: 0,
        )
        text = await session.request([], "batch-1")
    assert text == ""
    assert not [
        r
        for r in [json.loads(line) for line in log.path.read_text().splitlines()]
        if r.get("phase") == "provider_refused"
    ]
