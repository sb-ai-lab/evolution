"""The `model_grouping/2` runtime of the family-15 chat Grouper (b16 spec §3, §8, §10, §12; task C6).

`run_model_grouping_v2` composes the planner, the portion sessions and the
consolidation: the reference table goes to the log, the tool dialect is
negotiated once, each portion is one tool session, the groups of different
portions meet in bounded batches, and the result is admitted as `/15`'s is.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evolution import prompts
from evolution.tree.campaign_identity import FINAL_ADAPTATION_PROFILE, build_adaptation_identity
from evolution.tree.execution.check_modes import (
    CheckModes,
    HitSink,
    check_predicate,
    redact_for_journal,
)
from evolution.tree.roles.chat_loop import chat_request_size
from evolution.tree.roles.config import GroupingV2Limits, ModelGroupingSection
from evolution.tree.roles.editor.chat_editor import ChatCredentials
from evolution.tree.roles.grouping import grouping_portions as gp
from evolution.tree.roles.grouping.consolidation import BATCH_LEAD, BatchAnswer, BatchCheckpoint
from evolution.tree.roles.grouping.grouping_runtime import (
    AdaptiveCampaignRuntimeConfig,
    GroupingRuntime,
)
from evolution.tree.roles.grouping.grouping_tools import RESULT_ENVELOPE_CHARS, grouping_tools
from evolution.tree.roles.grouping.model_grouping import FallbackOutcome
from evolution.tree.roles.grouping.model_grouping_runtime import (
    REPAIR_RESERVE,
    ModelGroupingOutcome,
    split_prompt,
)
from evolution.tree.roles.grouping.model_grouping_v2 import (
    Adopted,
    AdoptedBatch,
    AdoptedPortion,
    input_budget,
    run_model_grouping_v2,
)
from evolution.tree.roles.grouping.portion_session import PORTION_LEAD, PortionOutcome
from evolution.tree.roles.grouping.reflect_grouping import GroupingRegistry
from evolution.tree.roles.role_call import AttemptRecorder, RoleCallRetry
from evolution.tree.roles.role_failures import GrouperRoleFailure, GroupingExecutionError
from evolution.tree.roles.workspace import open_attempt_log
from tests.campaign_fakes import model_grouping_identity_payload
from tests.model_grouping_fakes import ScriptedClient, server_error
from tests.tree.roles.grouping.test_grouping_portions import rec, registry
from tests.tree.roles.grouping.test_grouping_tools import fake_view
from tests.tree.roles.grouping.test_model_grouping_runtime import PARTITION

PROMPT = "map system\n\n## Reduce\n\nreduce system"
SECRET = "sk-v2-secret-0123456789"
NEEDLE = "ORACLE-NEEDLE"
RETRY = RoleCallRetry(
    max_attempts=1, total_budget_seconds=30, initial_seconds=0, multiplier=1, max_seconds=0
)
RECORD_BUDGET = 64 * 1024 - gp.CHECKPOINT_ENVELOPE_BYTES
CALL_KEYS = {"map_requests", "reduce", "attempts", "repairs"}


def section(**overrides: Any) -> ModelGroupingSection:
    values: dict[str, Any] = {
        "backend": "openai_chat",
        "model": "grouper-small",
        "base_url": "https://llm.example.test/v1",
        "api_key_env": "GROUPER_KEY",
        "timeout_seconds": 60,
        "concurrency": 1,
        "max_input_chars": 200_000,
        "max_output_tokens": 8192,
        "retry": RETRY,
    }
    values.update(overrides)
    return ModelGroupingSection(**values)


# --- A scripted chat Grouper --------------------------------------------------------


def portion_input(body: dict[str, Any]) -> dict[str, Any]:
    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    return json.loads(user[len(PORTION_LEAD) :])


def portion_units(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [unit for case in portion_input(body)["cases"] for unit in case["units"]]


def criteria_of(unit: dict[str, Any]) -> str:
    """The failed criteria a unit shows, as the model reads them."""

    if "glimpse" in unit:
        return unit["glimpse"].split("\n", 1)[0].removeprefix("failed_criteria: ")
    return json.dumps(unit["representative"]["failed_criteria"], separators=(",", ":"))


def answer_group(local: str, refs: Sequence[str], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": local,
        "title": "t",
        "failure_mode": "fm",
        "expected_behaviour": "eb",
        "question": "q?",
        "members": [{"ref": ref, "variant": "v"} for ref in refs],
        "citations": [],
        "priority": "p",
    }
    body.update(extra)
    return body


def finish(*groups: dict[str, Any], **item: Any) -> dict[str, Any]:
    return {"tool_calls": [("finish_portion", {"groups": list(groups)})], **item}


def by_criteria(body: dict[str, Any]) -> dict[str, Any]:
    """Answer any portion with one group per failed-criteria list."""

    keys: dict[str, list[str]] = {}
    for unit in portion_units(body):
        keys.setdefault(criteria_of(unit), []).append(unit["ref"])
    return finish(
        *(
            answer_group(f"g{n}", refs, title=f"criteria {key}")
            for n, (key, refs) in enumerate(keys.items(), start=1)
        )
    )


def batch_cards(body: dict[str, Any]) -> list[dict[str, Any]]:
    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    assert user.startswith(BATCH_LEAD)
    return json.loads(user[len(BATCH_LEAD) :])["cards"]


def merge_equal_titles(body: dict[str, Any]) -> str:
    titles: dict[str, list[str]] = {}
    for card in batch_cards(body):
        titles.setdefault(card["title"], []).append(card["id"])
    return json.dumps({"merges": [{"groups": ids} for ids in titles.values() if len(ids) > 1]})


def preflight(body: dict[str, Any]) -> dict[str, Any]:
    return {"tool_calls": [("list_case", {"case": "c1"})]}


def kind_of(body: dict[str, Any]) -> str:
    tools = [tool["function"]["name"] for tool in body.get("tools") or ()]
    tools += [function["name"] for function in body.get("functions") or ()]  # legacy dialect
    if not tools:
        return "batch"
    return "preflight" if tools == ["list_case"] else "portion"


class Model:
    """A chat Grouper answering by request kind: the preflight, a portion
    session turn (tools offered) or a consolidation batch (no tools).

    A handler returns a response item (``content``, ``tool_calls``,
    ``finish_reason``, ``usage``), a text (a batch's content) or an exception
    (raised). Every exchange is kept with its request size as
    `chat_request_size` counts it and its answer size in characters.
    """

    def __init__(
        self,
        *,
        portion: Callable[[dict[str, Any]], Any] = by_criteria,
        batch: Callable[[dict[str, Any]], Any] = merge_equal_titles,
        first: Callable[[dict[str, Any]], Any] = preflight,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.handlers = {"preflight": first, "portion": portion, "batch": batch}
        self.delays = delays or {}
        self.exchanges: list[dict[str, Any]] = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def kinds(self) -> list[str]:
        return [exchange["kind"] for exchange in self.exchanges]

    async def close(self) -> None:
        self.closed = True

    async def _create(self, **kw: Any) -> Any:
        body = json.loads(json.dumps(kw, ensure_ascii=False, default=repr))
        kind = kind_of(body)
        exchange: dict[str, Any] = {
            "kind": kind,
            "request_chars": chat_request_size(
                body["messages"], body.get("tools") or body.get("functions") or []
            ),
            "body": body,
        }
        self.exchanges.append(exchange)
        if self.delays.get(kind):
            await asyncio.sleep(self.delays[kind])
        item = self.handlers[kind](body)
        if isinstance(item, BaseException):
            exchange["answer_chars"] = 0
            raise item
        if isinstance(item, str):
            item = {"content": item}
        if isinstance(item.get("error"), dict):
            exchange["answer_chars"] = 0
            return SimpleNamespace(**item)
        calls = [
            SimpleNamespace(
                id=f"call-{len(self.exchanges)}-{index}",
                type="function",
                function=SimpleNamespace(
                    name=name, arguments=args if isinstance(args, str) else json.dumps(args)
                ),
            )
            for index, (name, args) in enumerate(item.get("tool_calls", ()))
        ]
        exchange["answer_chars"] = len(item.get("content") or "") + sum(
            len(call.function.arguments) for call in calls
        )
        message = SimpleNamespace(content=item.get("content"), tool_calls=calls or None)
        extra = {"usage": dict(item["usage"])} if item.get("usage") is not None else {}
        choice = SimpleNamespace(message=message, finish_reason=item.get("finish_reason"))
        return SimpleNamespace(choices=[choice], model=kw["model"], **extra)


class Sink:
    """A `CheckpointSink` that keeps what it is told."""

    def __init__(self) -> None:
        self.portions: list[tuple[PortionOutcome, str, str]] = []
        self.batches: list[tuple[int, str, BatchAnswer, str, str]] = []

    def portion(self, outcome: PortionOutcome, plan_digest: str, input_digest: str) -> None:
        self.portions.append((outcome, plan_digest, input_digest))

    def batch(
        self, level: int, index: str, answer: BatchAnswer, plan_digest: str, input_digest: str
    ) -> None:
        self.batches.append((level, index, answer, plan_digest, input_digest))

    def adopted(self) -> Adopted:
        return Adopted(
            portions={
                outcome.portion_id: AdoptedPortion(plan, digest, outcome)
                for outcome, plan, digest in self.portions
            },
            batches={
                (level, index): AdoptedBatch(plan, BatchCheckpoint(level, index, digest, answer))
                for level, index, answer, plan, digest in self.batches
            },
        )


def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def phases(records: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("phase") == phase]


class Run:
    """One `run_model_grouping_v2` call over ``rows`` with C3's fake grouping reader."""

    def __init__(
        self,
        tmp_path: Path,
        rows: Sequence[Any] | GroupingRegistry,
        *,
        limits: GroupingV2Limits | None = None,
        seconds: float = 60,
        reader: Any = None,
        provenance: dict[str, tuple[str, ...] | None] | None = None,
        prompt: str = PROMPT,
        forbidden_secret: Callable[[str], bool] = lambda text: SECRET in text,
        normalize: Callable[[Any], Any] | None = None,
        **overrides: Any,
    ) -> None:
        self.tmp_path = tmp_path
        self.prompt = prompt
        self.forbidden_secret = forbidden_secret
        self.normalize = normalize
        self.registry = rows if isinstance(rows, GroupingRegistry) else registry(list(rows))
        self.section = section(**overrides)
        self.limits = limits or GroupingV2Limits()
        self.seconds = seconds
        self.provenance = provenance or {}
        if reader is None:
            view, self.reads = fake_view(self.registry)
            reader = view.open_operation("group-op1", set(view._handlers))
        self.reader = reader
        self.records: list[dict[str, Any]] = []
        self.runs = 0

    async def __call__(
        self,
        model: Any,
        *,
        sink: Sink | None = None,
        adopted: Adopted | None = None,
        attempts: AttemptRecorder | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> ModelGroupingOutcome:
        self.runs += 1
        log = open_attempt_log(self.tmp_path / "logs", f"grouping-{self.runs}", secrets=(SECRET,))
        try:
            return await run_model_grouping_v2(
                self.registry,
                section=self.section,
                limits=self.limits,
                prompt=self.prompt,
                metric_primary="score",
                policy_identity="p",
                credentials=ChatCredentials("key", None),
                log=log,
                session_seconds=self.seconds,
                reader=self.reader,
                provenance=self.provenance,
                checkpoints=sink if sink is not None else Sink(),
                adopted=adopted if adopted is not None else Adopted(),
                cancelled=cancelled,
                forbidden_secret=self.forbidden_secret,
                oracle_needle=lambda text: NEEDLE in text,
                record_budget=RECORD_BUDGET,
                normalize=self.normalize,
                client_factory=lambda _section, _credentials: model,
                attempts=attempts,
            )
        finally:
            log.close()
            self.records = read_log(log.path)


def members(outcome: ModelGroupingOutcome) -> set[frozenset[str]]:
    return {frozenset(n.member_ids) for n in outcome.grouped.neighborhoods}


def spread_rows() -> list[Any]:
    """Five failing units over four cases: `x` fails in every case, `y` once."""

    return [
        rec("a", 1, criteria=("x",)),
        rec("a", 2, criteria=("y",)),
        rec("a", 3, failing=False),
        rec("b", 1, criteria=("x",)),
        rec("c", 1, criteria=("x",)),
        rec("d", 1, criteria=("x",)),
    ]


#: Two units per portion (``8192 // 4096``), so `spread_rows` makes three portions.
SMALL = GroupingV2Limits(group_output_tokens=4096)


# --- End to end -----------------------------------------------------------------------------


async def test_v2_end_to_end_scripted(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows(), limits=SMALL)
    model, sink = Model(), Sink()
    outcome = await run(model, sink=sink)

    assert outcome.fallback is None
    assert members(outcome) == {frozenset({"a#1", "b#1", "c#1", "d#1"}), frozenset({"a#2"})}
    assert model.kinds() == ["preflight", "portion", "portion", "portion", "batch"]
    assert outcome.calls == {"map_requests": 3, "reduce": "done", "attempts": 5, "repairs": 0}
    assert model.closed

    # Every portion and batch decision reaches the sink once, with its digests.
    assert [o.portion_id for o, _, _ in sink.portions] == ["p1", "p2", "p3"]
    (plan_digest,) = {plan for _, plan, _ in sink.portions}
    assert len({digest for _, _, digest in sink.portions}) == 3
    assert [(level, index) for level, index, *_ in sink.batches] == [(0, "b1")]
    assert sink.batches[0][2].outcome is None and len(sink.batches[0][2].merges) == 1

    # The reference table is logged once, before the first request.
    order = [r["phase"] for r in run.records]
    assert order.count("ref_registry") == 1
    first_request = min(
        i for i, r in enumerate(run.records) if r["phase"] in ("model_request", "request")
    )
    assert order.index("ref_registry") < first_request
    ref = phases(run.records, "ref_registry")[0]
    assert ref["observations"] == {"o1": "a#1", "o2": "a#2", "o3": "b#1", "o4": "c#1", "o5": "d#1"}
    (plan,) = phases(run.records, "portion_plan")
    assert (plan["digest"], plan["portions"], plan["capacity"]) == (plan_digest, 3, 2)
    # The one preflight offers only `list_case` and is followed by no other.
    assert len(phases(run.records, "preflight")) == 1
    # Every request, map and batch alike, stays within `max_input_chars`.
    assert max(e["request_chars"] for e in model.exchanges) <= run.section.max_input_chars


async def test_one_portion_skips_consolidation(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows())
    model = Model()
    outcome = await run(model)
    assert model.kinds() == ["preflight", "portion"]
    assert outcome.calls["reduce"] == "none"
    assert members(outcome) == {frozenset({"a#1", "b#1", "c#1", "d#1"}), frozenset({"a#2"})}


async def test_calls_shape_stays_closed(tmp_path: Path) -> None:
    """`map_requests` counts every portion session's
    requests (a repair included), never the preflight or a batch."""

    turns: dict[str, int] = {}

    def repair_first(body: dict[str, Any]) -> dict[str, Any]:
        key = json.dumps(portion_input(body), sort_keys=True)
        turns[key] = turns.get(key, 0) + 1
        if turns[key] == 1:
            return finish(answer_group("g1", ["o99"]))  # unknown_ref: one protocol repair
        return by_criteria(body)

    run = Run(tmp_path, spread_rows(), limits=SMALL)
    model = Model(portion=repair_first)
    outcome = await run(model)
    assert set(outcome.calls) == CALL_KEYS
    assert outcome.calls == {"map_requests": 6, "reduce": "done", "attempts": 8, "repairs": 3}
    assert outcome.fallback is None


async def test_batch_header_with_an_oracle_needle_is_checkpointed_scrubbed(
    tmp_path: Path,
) -> None:
    """A merged header is scrubbed before its batch is checkpointed, as a
    portion's groups are: a needle in any header text blanks every one."""

    def merge_all_needled(body: dict[str, Any]) -> str:
        ids = [card["id"] for card in batch_cards(body)]
        header = {
            "title": "merged",
            "failure_mode": f"the answer leaks {NEEDLE}",
            "expected_behaviour": "eb",
            "question": "q?",
            "priority": "p",
        }
        return json.dumps({"merges": [{"groups": ids, **header}]})

    run = Run(tmp_path, spread_rows(), limits=SMALL)
    model, sink = Model(batch=merge_all_needled), Sink()
    outcome = await run(model, sink=sink)

    assert outcome.fallback is None
    ((_, _, answer, _, _),) = sink.batches
    ((_, header),) = answer.merges
    assert header is not None
    assert (header.title, header.failure_mode, header.expected_behaviour, header.question) == (
        "",
        "",
        "",
        "",
    )
    assert NEEDLE not in json.dumps(outcome.grouped.result, default=str)


async def test_batch_repair_refused_by_max_requests_is_not_counted(tmp_path: Path) -> None:
    """A repair counts once its request is sent: one stopped by
    ``max_requests`` spends no repair."""

    # The preflight, three portions and the first batch request use all five.
    run = Run(
        tmp_path, spread_rows(), limits=GroupingV2Limits(group_output_tokens=4096, max_requests=5)
    )
    model = Model(batch=lambda body: "not a merge list")
    outcome = await run(model)

    assert model.kinds() == ["preflight", "portion", "portion", "portion", "batch"]
    assert outcome.calls["repairs"] == 0
    assert phases(run.records, "repair")  # the repair was prepared, never sent
    assert any(
        r.get("stage") == "consolidation" for r in phases(run.records, "max_requests_reached")
    )


async def test_adoption_replays_matching_checkpoints_only(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows(), limits=SMALL)
    sink = Sink()
    first = await run(Model(), sink=sink)

    # Everything adopted: no request at all, not even the preflight.
    replay = Model()
    again = await run(replay, adopted=sink.adopted())
    assert replay.kinds() == []
    assert members(again) == members(first) and again.calls["reduce"] == "done"
    assert again.grouped.result == first.grouped.result

    # A portion whose input digest no longer matches is asked again.
    stale = sink.adopted()
    p2 = stale.portions["p2"]
    portions = {**stale.portions, "p2": AdoptedPortion(p2.plan_digest, "0" * 64, p2.outcome)}
    partial = Model()
    await run(partial, adopted=Adopted(portions, stale.batches))
    assert partial.kinds() == ["preflight", "portion"]
    # A different plan digest adopts nothing.
    other = {k: AdoptedPortion("f" * 64, v.input_digest, v.outcome) for k, v in portions.items()}
    fresh = Model()
    await run(fresh, adopted=Adopted(other, {}))
    assert fresh.kinds() == ["preflight", "portion", "portion", "portion", "batch"]
    # A batch of another plan is not adopted, though its input matches (§9).
    batches = {k: AdoptedBatch("f" * 64, v.checkpoint) for k, v in stale.batches.items()}
    replanned = Model()
    await run(replanned, adopted=Adopted(sink.adopted().portions, batches))
    assert replanned.kinds() == ["batch"]
    (offered,) = phases(run.records, "batches_offered")
    assert (offered["offered"], offered["same_plan"]) == (len(batches), 0)


# --- max_requests -------------------------------------------------------------------------


async def test_max_requests_stops_and_routes(tmp_path: Path) -> None:
    """With three requests the preflight and two portions are sent; the third
    portion is uncovered, consolidation is skipped and the log says so."""

    run = Run(
        tmp_path, spread_rows(), limits=GroupingV2Limits(group_output_tokens=4096, max_requests=3)
    )
    model, sink = Model(), Sink()
    outcome = await run(model, sink=sink)

    assert model.kinds() == ["preflight", "portion", "portion"]
    assert outcome.fallback == FallbackOutcome("singletons", "uncovered", "invalid_output")
    assert outcome.calls == {"map_requests": 2, "reduce": "skipped", "attempts": 3, "repairs": 0}
    assert [o.portion_id for o, _, _ in sink.portions] == ["p1", "p2"]  # p3 never checkpointed
    reached = phases(run.records, "max_requests_reached")
    assert {r.get("portion") for r in reached} >= {"p3"}
    assert any(r.get("stage") == "consolidation" for r in reached)
    # p3's observation (d#1) ends in the fallback; the model's groups stay.
    assert frozenset({"d#1"}) in members(outcome)
    assert frozenset({"a#2"}) in members(outcome)


async def test_max_requests_spent_by_the_preflight_falls_back_whole(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows(), limits=GroupingV2Limits(max_requests=1))
    model = Model(first=lambda body: {"content": "no call"})
    outcome = await run(model)
    assert model.kinds() == ["preflight"]
    assert outcome.fallback == FallbackOutcome("singletons", "all", "invalid_output")
    assert outcome.calls["map_requests"] == 0 and outcome.calls["reduce"] == "skipped"
    assert any(r.get("stage") == "preflight" for r in phases(run.records, "max_requests_reached"))


# --- Deadlines ------------------------------------------------------------------------------


async def test_total_time_over_timeout_finishes_when_sessions_fit(tmp_path: Path) -> None:
    """`timeout_seconds` bounds one session (§6.2): five requests of 0.3 s
    each take longer than one second in all, and the grouping still finishes."""

    run = Run(tmp_path, spread_rows(), limits=SMALL, seconds=1)
    model = Model(delays={"preflight": 0.3, "portion": 0.3, "batch": 0.3})
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await run(model)
    assert loop.time() - started > 1.2
    assert outcome.fallback is None
    assert model.kinds() == ["preflight", "portion", "portion", "portion", "batch"]


async def test_one_observation_session_timeout_is_a_provider_failure(tmp_path: Path) -> None:
    run = Run(tmp_path, [rec("a", 1)], seconds=1)
    attempts = AttemptRecorder()
    with pytest.raises(GrouperRoleFailure) as excinfo:
        await run(Model(delays={"portion": 2}), attempts=attempts)
    assert (excinfo.value.reason, excinfo.value.error_class) == ("provider_exhausted", "provider")
    assert attempts.failure == "deadline_exhausted"
    assert phases(run.records, "portion_deadline_exhausted")


def _runtime(sec: ModelGroupingSection, *, protocol: str, prompt: str) -> GroupingRuntime:
    grouping = {
        **model_grouping_identity_payload(),
        "kind": "chat",
        "protocol": protocol,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
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


async def _model(runtime: GroupingRuntime, client: Any, tmp_path: Path, reg: Any, **flags: Any):
    return await runtime.model(
        reg,
        operation="op-1",
        credentials=ChatCredentials("key", None),
        log_root=tmp_path / "reflector-logs",
        secrets=(SECRET,),
        cancelled=lambda: False,
        forbidden_secret=lambda text: SECRET in text,
        oracle_needle=lambda _text: False,
        metric_primary="score",
        client_factory=lambda _section, _credentials: client,
        **flags,
    )


async def test_family14_keeps_operation_deadline(tmp_path: Path) -> None:
    """The `/15` path still bounds the whole operation by `timeout_seconds`:
    a clock already past the deadline sends no request at all."""

    sec = section(timeout_seconds=600, concurrency=4)
    runtime = _runtime(sec, protocol="model_grouping/1", prompt=prompts.TREE_GROUPING_MODEL_SYSTEM)
    times = iter([0.0, 700.0, 700.0, 700.0, 700.0])
    client = ScriptedClient([])
    with pytest.raises(GrouperRoleFailure) as excinfo:
        await _model(
            runtime,
            client,
            tmp_path,
            registry(spread_rows()),
            clock=lambda: next(times),
            family14=True,
            family15=False,
        )
    assert (excinfo.value.reason, excinfo.value.error_class) == ("provider_exhausted", "provider")
    assert client.requests == []


async def test_runtime_dispatches_v2_by_protocol_and_family(tmp_path: Path) -> None:
    sec = section()
    reg = registry(spread_rows())
    view, _ = fake_view(reg)
    reader = view.open_operation("group-op1", set(view._handlers))
    # b17 seals the packaged `_v3` prompt for a `model_grouping/2` identity
    # (ADR-0057); the runtime splits it on its `## Reduce` line as before.
    runtime = _runtime(
        sec, protocol="model_grouping/2", prompt=prompts.TREE_GROUPING_MODEL_V3_SYSTEM
    )
    model = Model()
    sink = Sink()
    # A clock already "past" any operation deadline: family 15 bounds sessions only.
    outcome = await _model(
        runtime,
        model,
        tmp_path,
        reg,
        clock=lambda: 10**9,
        family14=True,
        family15=True,
        reader=reader,
        checkpoints=sink,
        record_budget=RECORD_BUDGET,
    )
    assert outcome.fallback is None and model.kinds() == ["preflight", "portion"]
    assert [o.portion_id for o, _, _ in sink.portions] == ["p1"]
    # The v2 map system prompt is the `_v3` file's map part.
    system = model.exchanges[1]["body"]["messages"][0]["content"]
    assert system == prompts.TREE_GROUPING_MODEL_V3_SYSTEM.split("\n## Reduce")[0].strip()

    # Without checkpoints, adoption or provenance the path still runs.
    plain = Model()
    await _model(
        runtime,
        plain,
        tmp_path / "plain",
        reg,
        family14=True,
        family15=True,
        reader=reader,
        record_budget=RECORD_BUDGET,
    )
    assert plain.kinds() == ["preflight", "portion"]

    # A v2 identity outside family 15 is a wiring defect, never a v1 run.
    with pytest.raises(ValueError, match="model_grouping_v2"):
        await _model(runtime, Model(), tmp_path / "f14", reg, family14=True, family15=False)


# --- Failures --------------------------------------------------------------------------------


async def test_provider_exhaustion_in_a_session_suspends(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows(), limits=SMALL)
    sink = Sink()
    calls = 0

    def second_fails(body: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        return server_error() if calls == 2 else by_criteria(body)

    with pytest.raises(GrouperRoleFailure) as excinfo:
        await run(Model(portion=second_fails), sink=sink)
    assert (excinfo.value.reason, excinfo.value.error_class) == ("provider_exhausted", "provider")
    assert [o.portion_id for o, _, _ in sink.portions] == ["p1"]  # finished work stays


async def test_input_budget_refusal_falls_back(tmp_path: Path) -> None:
    run = Run(
        tmp_path,
        spread_rows(),
        limits=GroupingV2Limits(read_chars=16_000),
        max_input_chars=20_000,
    )
    model = Model()
    outcome = await run(model)
    assert model.kinds() == []
    assert outcome.fallback == FallbackOutcome("singletons", "all", "input_too_large")
    (refused,) = phases(run.records, "input_budget_refused")
    assert refused["max_input_chars"] == 20_000 and refused["read_chars"] == 16_000
    # Spec §6.2: the fixed opening plus one tool result, its envelope and the
    # repair reserve; no escape allowance, preview or repair-schema term.
    assert refused["reserve"] == opening_chars("map system") + 16_000 + 512 + 600
    assert refused["reserve"] > 20_000


def opening_chars(system: str) -> int:
    """The fixed opening of a portion request: system, portion lead and every tool."""

    opening = [{"role": "system", "content": system}, {"role": "user", "content": PORTION_LEAD}]
    return chat_request_size(opening, grouping_tools(traces=True))


async def test_max_input_chars_just_above_the_reserve_groups(tmp_path: Path) -> None:
    """With the packaged prompt and default keys, the reserve is the spec's
    (§6.2), so a limit a few thousand characters above it still groups."""

    map_system, _ = split_prompt(prompts.TREE_GROUPING_MODEL_V2_SYSTEM)
    limits = GroupingV2Limits()
    reserve = opening_chars(map_system) + limits.read_chars + RESULT_ENVELOPE_CHARS + REPAIR_RESERVE
    _, computed = input_budget(section(), limits, map_system)
    assert computed == reserve
    # The pre-fix reserve (two `read_chars`, the preview and the repair
    # schema) was above 48,000 characters and refused this limit outright.
    assert reserve + 4_000 < 48_000
    run = Run(
        tmp_path,
        spread_rows(),
        limits=limits,
        prompt=prompts.TREE_GROUPING_MODEL_V2_SYSTEM,
        max_input_chars=reserve + 4_000,
    )
    model = Model()
    outcome = await run(model)
    assert phases(run.records, "input_budget_refused") == []
    assert phases(run.records, "portion_unit_too_large") == []
    assert outcome.fallback is None
    assert "portion" in model.kinds()
    assert max(exchange["request_chars"] for exchange in model.exchanges) <= reserve + 4_000


async def test_preflight_without_the_probe_call_falls_back(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows())
    model = Model(first=lambda body: {"content": "I will not call tools"})
    outcome = await run(model)
    assert set(model.kinds()) == {"preflight"}
    assert outcome.fallback == FallbackOutcome("singletons", "all", "invalid_output")
    assert outcome.calls["map_requests"] == 0


async def test_preflight_repairs_stay_within_max_input_chars(tmp_path: Path) -> None:
    """PDR-0047: every request stays within the limits, the preflight's too.

    Wrong preflight calls with long arguments are echoed in each protocol
    correction, so the negotiation grows. A request over ``max_input_chars``
    is not sent; the preflight fails, and every failure falls back.
    """

    limit = 8_000
    run = Run(tmp_path, spread_rows(), limits=GroupingV2Limits(read_chars=1), max_input_chars=limit)
    wrong = {"tool_calls": [("read_observation", {"case": "c1", "note": "x" * 3_500})]}
    model = Model(first=lambda body: wrong)
    outcome = await run(model)

    assert model.kinds() and set(model.kinds()) == {"preflight"}
    assert max(exchange["request_chars"] for exchange in model.exchanges) <= limit
    assert outcome.fallback == FallbackOutcome("singletons", "all", "invalid_output")
    (refused,) = phases(run.records, "preflight_context_exhausted")
    assert refused["max_input_chars"] == limit and refused["request_chars"] > limit
    assert outcome.calls["map_requests"] == 0
    # The refused request was never sent, so it is not counted either.
    assert len(phases(run.records, "provider_attempt")) == len(model.exchanges)


async def test_secret_in_the_preflight_answer_is_never_logged(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows())
    model = Model(
        first=lambda body: {
            "content": f"key {SECRET}",
            "tool_calls": [("list_case", {"case": "c1"})],
        }
    )
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(model)
    assert excinfo.value.code == "secret_detected"
    responses = phases(run.records, "model_response")
    assert responses and responses[-1]["content"] == "[redacted]"


#: JSON text whose decoded value starts with `SECRET`: the raw wire never holds it.
ESCAPED = "\\u0073" + SECRET[1:]
PLACE = '"@SECRET@"'


def escaped(value: Any) -> str:
    """``value`` as JSON, each ``"@SECRET@"`` holding the Unicode-escaped secret."""

    return json.dumps(value).replace(PLACE, f'"{ESCAPED} text"')


def portion_with_secret(body: dict[str, Any]) -> dict[str, Any]:
    item = by_criteria(body)
    ((name, args),) = item["tool_calls"]
    args["groups"][0]["title"] = "@SECRET@"
    return {"tool_calls": [(name, escaped(args))]}


def merge_all_with_secret(body: dict[str, Any]) -> str:
    ids = [card["id"] for card in batch_cards(body)]
    header = {
        "title": "@SECRET@",
        "failure_mode": "fm",
        "expected_behaviour": "eb",
        "question": "q?",
        "priority": "p",
    }
    return escaped({"merges": [{"groups": ids, **header}]})


def secret_check(mode: str, hits: HitSink) -> Callable[[str], bool]:
    """The campaign's `grouping_run_secret` predicate under ``mode``."""

    return check_predicate(
        "grouping_run_secret",
        "secret_detected",
        lambda value: isinstance(value, str) and SECRET in value,
        modes=CheckModes({"grouping_run_secret": mode}),  # type: ignore[dict-item]
        hits=hits,
    )


def journal_form(value: Any) -> Any:
    """The campaign's `normalize` hook: the journal's run-secret redaction."""

    return redact_for_journal(value, (SECRET,))


@pytest.mark.parametrize("normalize", [None, journal_form], ids=["raw", "normalized"])
async def test_escaped_secret_in_a_batch_header_is_refused(
    tmp_path: Path, normalize: Callable[[Any], Any] | None
) -> None:
    """PDR-0044: under ``enforce`` the decoded merge header is admitted
    before it takes its persisted form, so the journal's redaction cannot
    hide a run secret the raw-wire screen let through."""

    hits = HitSink()
    run = Run(
        tmp_path,
        spread_rows(),
        limits=SMALL,
        forbidden_secret=secret_check("enforce", hits),
        normalize=normalize,
    )
    model, sink = Model(batch=merge_all_with_secret), Sink()
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(model, sink=sink)
    assert excinfo.value.code == "secret_detected"
    assert model.kinds()[-1] == "batch"
    assert [o.portion_id for o, _, _ in sink.portions] == ["p1", "p2", "p3"]
    assert sink.batches == []  # the secret-bearing answer is never checkpointed


@pytest.mark.parametrize("normalize", [None, journal_form], ids=["raw", "normalized"])
async def test_escaped_secret_in_a_portion_title_is_refused(
    tmp_path: Path, normalize: Callable[[Any], Any] | None
) -> None:
    """The portion path admits its decoded groups before normalizing them."""

    hits = HitSink()
    run = Run(
        tmp_path,
        spread_rows(),
        forbidden_secret=secret_check("enforce", hits),
        normalize=normalize,
    )
    model, sink = Model(portion=portion_with_secret), Sink()
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(model, sink=sink)
    assert excinfo.value.code == "secret_detected"
    assert model.kinds() == ["preflight", "portion"]
    assert sink.portions == []


@pytest.mark.parametrize(("mode", "count"), [("shadow", 1), ("off", 0)])
@pytest.mark.parametrize("where", ["batch", "portion"])
async def test_non_enforced_escaped_secret_is_admitted_redacted(
    tmp_path: Path, mode: str, count: int, where: str
) -> None:
    """Modes decide admission, never persistence (PDR-0044): a shadowed hit
    is counted once per field, and the value is admitted and used redacted."""

    hits = HitSink()
    run = Run(
        tmp_path,
        spread_rows(),
        # Three portions and one batch, or one portion: one secret field.
        limits=SMALL if where == "batch" else None,
        forbidden_secret=secret_check(mode, hits),
        normalize=journal_form,
    )
    model = (
        Model(batch=merge_all_with_secret)
        if where == "batch"
        else Model(portion=portion_with_secret)
    )
    sink = Sink()
    outcome = await run(model, sink=sink)

    assert outcome.fallback is None
    expected = (
        [{"check": "grouping_run_secret", "code": "secret_detected", "count": count}]
        if count
        else []
    )
    assert hits.entries() == expected
    if where == "batch":
        ((_, _, answer, _, _),) = sink.batches
        ((_, header),) = answer.merges
        assert header is not None and header.title == "[REDACTED] text"
    else:
        titles = [g["title"] for outcome_, _, _ in sink.portions for g in outcome_.groups]
        assert "[REDACTED] text" in titles
    result = json.dumps(outcome.grouped.result, default=str)
    assert SECRET not in result and "[REDACTED] text" in result


async def test_cancelled_before_start(tmp_path: Path) -> None:
    run = Run(tmp_path, spread_rows())
    with pytest.raises(GroupingExecutionError) as excinfo:
        await run(Model(), cancelled=lambda: True)
    assert excinfo.value.code == "cancelled"


# --- A real grouping reader (deferred from C3/C4) ---------------------------------------------


@contextmanager
def failing_inputs(
    root: Path, *, samples: int = 3, failing: Sequence[int] = (1, 2)
) -> Iterator[Any]:
    """`FrozenReflectionInputs` over a real trace store whose ``failing``
    samples carry a failed verdict."""

    from evolution.tree.roles.editor.editor_evidence import TRACE_ROOT, TRACE_SUBDIR
    from evolution.tree.roles.reflector.reflection_inputs import FrozenReflectionInputs
    from tests.campaign_fakes import PRIMARY
    from tests.tree.roles.editor.test_editor_evidence import _reflect_row, _Trace

    trace = _Trace(root, turns=1, samples=samples)
    try:
        for index, request_id in enumerate(trace.request_ids, start=1):
            stdout = {
                "metrics": {"accuracy": 1.0},
                "verdicts": [
                    {"criterion": "checks_x", "passed": index not in failing, "note": "missing"}
                ],
                "public_feedback": {"note": f"feedback of sample {index}"},
            }
            directory = trace.run_root / TRACE_ROOT / TRACE_SUBDIR / request_id
            (directory / "stdout").write_text(json.dumps(stdout), encoding="utf-8")
        yield FrozenReflectionInputs(
            baseline_binding=trace.baseline.digest,
            rows=tuple(_reflect_row(f"s{index}", index) for index in range(1, samples + 1)),
            records=trace.journal.snapshot().records,
            run_root=trace.run_root,
            corpus=(trace.case,),
            baseline=trace.baseline,
            environment_kind="static",
            metric_primary=PRIMARY,
        )
    finally:
        trace.close()


async def test_real_reflection_inputs_session_reads_and_lists(tmp_path: Path) -> None:
    with failing_inputs(tmp_path / "inputs") as inputs:
        reg = inputs.registry
        assert [r.failing for r in reg.records] == [True, True, False]
        reader = inputs.reader_view().open_operation(
            "group-real", {"list_grouping_inputs", "read_grouping_input", "stream_grouping_inputs"}
        )
        results: list[dict[str, Any]] = []

        def session(body: dict[str, Any]) -> dict[str, Any]:
            tool_results = [m for m in body["messages"] if m["role"] == "tool"]
            results[:] = [json.loads(m["content"]) for m in tool_results]
            if not tool_results:
                return {"tool_calls": [("list_case", {"case": "c1"})]}
            if len(tool_results) == 1:
                return {
                    "tool_calls": [
                        (
                            "read_observation",
                            {"case": "c1", "repeat": 3, "section": "observation"},
                        )
                    ]
                }
            return by_criteria(body)

        try:
            run = Run(tmp_path, reg, reader=reader)
            outcome = await run(Model(portion=session))
        finally:
            reader.close()
    listed, observed = results
    assert [item["state"] for item in listed["repeats"]] == ["failing", "failing", "passing"]
    assert observed["section"] == "observation" and observed["content"]
    assert "error" not in listed and "error" not in observed
    failing = {r.evidence_id for r in reg.records if r.failing}
    assert members(outcome) == {frozenset(failing)} and outcome.fallback is None


@pytest.mark.parametrize(
    "code,attempts,failure", [(503, 3, "provider_exhausted"), (400, 1, "provider_rejected")]
)
async def test_v16_consolidation_error_body_is_refusal_not_invalid_output(
    tmp_path, code, attempts, failure
):
    from dataclasses import replace

    message = "PRIVATE-BATCH-ROUTER-MESSAGE"
    run = Run(tmp_path, spread_rows(), limits=SMALL, retry=replace(RETRY, max_attempts=3))
    model = Model(batch=lambda body: {"error": {"code": code, "message": message}})
    with pytest.raises(GrouperRoleFailure) as caught:
        await run(model)
    assert caught.value.reason == failure
    assert model.kinds().count("batch") == attempts
    refused = phases(run.records, "provider_refused")
    assert len(refused) == attempts
    assert all(r["request_style"] is None and r["turn"] is None for r in refused)
    assert all(r["request_id"].startswith("batch-") for r in refused)
    assert all(r["message"] == message and r["http_status"] == 200 for r in refused)
    assert not phases(run.records, "reduce_invalid")
    assert not any(
        message in json.dumps(r) for r in run.records if r.get("phase") != "provider_refused"
    )


async def test_v16_consolidation_transient_refusal_then_answer_is_admitted(tmp_path):
    from dataclasses import replace

    calls = 0

    def batch(body):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"error": {"code": 503, "message": "PRIVATE-BATCH-MESSAGE"}}
        return merge_equal_titles(body)

    run = Run(tmp_path, spread_rows(), limits=SMALL, retry=replace(RETRY, max_attempts=3))
    model = Model(batch=batch)
    outcome = await run(model)
    assert outcome.fallback is None
    assert outcome.calls["repairs"] == 0
    assert len(phases(run.records, "provider_refused")) == 1
    assert members(outcome) == {frozenset({"a#1", "b#1", "c#1", "d#1"}), frozenset({"a#2"})}


async def test_consolidation_require_parameters_false_in_provider_object(tmp_path):
    run = Run(
        tmp_path,
        spread_rows(),
        limits=SMALL,
        provider_only=("CoreWeave",),
        require_parameters=False,
    )
    # The Grouper verifies no provider, on either path.
    model = Model()
    outcome = await run(model)
    assert outcome.fallback is None
    batches = [e["body"] for e in model.exchanges if e["kind"] == "batch"]
    assert batches
    assert all(b["extra_body"]["provider"]["require_parameters"] is False for b in batches)


async def test_v16_chat_fields_fallback_carries_descriptions(tmp_path):
    from evolution.tree.roles.config import FieldsGroupingSection
    from evolution.tree.roles.grouping.reflect_grouping import FieldFeature

    run = Run(
        tmp_path,
        [rec("a", 1), rec("b", 1)],
        fallback=FieldsGroupingSection(
            (FieldFeature("terminal", "observation", "/terminal", "status"),)
        ),
    )
    outcome = await run(Model(portion=lambda body: finish()))
    assert outcome.fallback.kind == "fields"
    assert all(n.description is not None for n in outcome.grouped.neighborhoods)


async def test_fields_fallback_shadow_secret_is_counted_only_at_admission(tmp_path):
    from evolution.tree.roles.config import FieldsGroupingSection
    from evolution.tree.roles.grouping.reflect_grouping import FieldFeature

    hits = HitSink()
    screen = check_predicate(
        "grouping_run_secret",
        "secret_detected",
        lambda text: SECRET in text,
        modes=CheckModes({"grouping_run_secret": "shadow"}),
        hits=hits,
    )
    run = Run(
        tmp_path,
        [rec("a", 1, criteria=(SECRET,))],
        forbidden_secret=screen,
        fallback=FieldsGroupingSection(
            (FieldFeature("criteria", "observation", "/failed_criteria", ""),)
        ),
    )
    outcome = await run(Model(portion=lambda body: finish()))
    assert outcome.grouped.neighborhoods[0].description is not None
    assert sum(entry["count"] for entry in hits.entries()) == 2
