"""Portion sessions of the family-15 chat Grouper (b16 spec §6.2-§6.5, task C4)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.roles.chat_history import tool_exchange
from evolution.tree.roles.chat_loop import chat_request_size
from evolution.tree.roles.config import GroupingV2Limits, ModelGroupingSection
from evolution.tree.roles.grouping import grouping_portions as gp
from evolution.tree.roles.grouping.grouping_tools import (
    RESULT_ENVELOPE_CHARS,
    GroupingToolbox,
    grouping_tools,
)
from evolution.tree.roles.grouping.model_grouping import (
    parse_map_response,
    parse_portion_answer,
)
from evolution.tree.roles.grouping.model_grouping_runtime import admit_model_groups
from evolution.tree.roles.grouping.portion_session import (
    MAX_REQUESTS,
    PORTION_LEAD,
    PortionContext,
    PortionOutcome,
    RequestBudget,
    checkpoint_body,
    checkpoint_size,
    group_record,
    map_portions,
    model_group,
    run_portion,
)
from evolution.tree.roles.role_call import RoleCallExhausted, RoleCallRetry
from evolution.tree.roles.role_failures import GroupingExecutionError
from evolution.tree.roles.workspace import open_attempt_log
from tests.model_grouping_fakes import (
    ScriptedToolClient,
    grouping_answer,
    map_group,
    server_error,
)
from tests.tree.roles.grouping.test_grouping_portions import Built, rec, registry
from tests.tree.roles.grouping.test_grouping_tools import fake_view

SECRET = "sk-portion-secret-0123456789"
RETRY = RoleCallRetry(
    max_attempts=1, total_budget_seconds=30, initial_seconds=0, multiplier=1, max_seconds=0
)
RECORD_BUDGET = 64 * 1024 - gp.CHECKPOINT_ENVELOPE_BYTES


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


class Client(ScriptedToolClient):
    """`ScriptedToolClient` whose items may also be an exception (raised) or a
    callable of the request body returning the item, so a script can answer
    whichever portion it is sent."""

    async def _create(self, **kw: Any) -> Any:
        item = self.responses[0]
        if callable(item):
            self.responses[0] = item(json.loads(json.dumps(kw, default=repr)))
        if isinstance(self.responses[0], BaseException):
            self.bodies.append(json.loads(json.dumps(kw, ensure_ascii=False, default=repr)))
            raise self.responses.pop(0)
        return await super()._create(**kw)


def group(local: str, refs: Sequence[str], *, variant: str = "v", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": local,
        "title": "t",
        "failure_mode": "fm",
        "expected_behaviour": "eb",
        "question": "q?",
        "members": [{"ref": ref, "variant": variant} for ref in refs],
        "citations": ["verdict:c1"],
        "priority": "p",
    }
    body.update(extra)
    return body


def finish(*groups: dict[str, Any], **item: Any) -> dict[str, Any]:
    return {"tool_calls": [("finish_portion", {"groups": list(groups)})], **item}


def portion_units(body: dict[str, Any]) -> list[str]:
    """The unit references of the portion a request carries."""

    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    rendered = json.loads(user[len(PORTION_LEAD) :])
    return [unit["ref"] for case in rendered["cases"] for unit in case["units"]]


def cover_all(**item: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Answer any portion with one group over all its units."""

    return lambda body: finish(group("g1", portion_units(body)), **item)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        rows: list[Any],
        *,
        capacity: int = 16,
        limits: GroupingV2Limits | None = None,
        reported: dict[str, frozenset[str]] | None = None,
        record_budget: int = RECORD_BUDGET,
        seconds: float = 60,
        cap: int | None = None,
        **overrides: Any,
    ) -> None:
        self.tmp_path = tmp_path
        self.registry = registry(rows)
        self.built = Built(self.registry, capacity=capacity)
        view, _ = fake_view(self.registry)
        self.reader = view.open_operation("group-op1", set(view._handlers))
        self.section = section(**overrides)
        self.limits = limits or GroupingV2Limits()
        self.reported = reported or {}
        self.record_budget = record_budget
        self.seconds = seconds
        self.budget = RequestBudget(cap)
        self.finished: list[PortionOutcome] = []

    @property
    def plan(self) -> gp.PortionPlan:
        return self.built.plan

    def context(self, client: Any, log: Any) -> PortionContext:
        return PortionContext(
            client=client,
            section=self.section,
            log=log,
            recorder=None,
            limits=self.limits,
            request_style="modern",
            system="map prompt",
            render=self.built.render,
            reader=self.reader,
            table=self.built.table,
            glimpses=self.built.glimpses,
            reported=self.reported,
            record_budget=self.record_budget,
            session_deadline=lambda: time.monotonic() + self.seconds,
            forbidden_secret=lambda text: SECRET in text,
            oracle_needle=lambda text: "ORACLE-NEEDLE" in text,
            budget=self.budget,
        )

    async def map(
        self, script: list[Any], *, adopted: dict[str, PortionOutcome] | None = None
    ) -> tuple[tuple[PortionOutcome, ...], Client, list[dict[str, Any]]]:
        client = Client(script)
        directory = self.tmp_path / f"logs-{len(list(self.tmp_path.glob('logs-*')))}"
        with open_attempt_log(directory, "a", secrets=()) as log:
            try:
                outcomes = await map_portions(
                    self.context(client, log),
                    self.plan,
                    adopted=adopted or {},
                    on_finished=self.finished.append,
                )
            finally:
                self.records = read_log(log.path)
        return outcomes, client, self.records

    async def run(self, script: list[Any], portion_index: int = 0) -> tuple[Any, Client]:
        client = Client(script)
        with open_attempt_log(self.tmp_path / "logs-run", "a", secrets=()) as log:
            try:
                outcome = await run_portion(
                    self.context(client, log), self.plan.portions[portion_index]
                )
            finally:
                self.records = read_log(log.path)
        return outcome, client


def read_log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def phases(records: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("phase") == phase]


def members(outcome: PortionOutcome) -> list[str]:
    return [m["evidence_id"] for g in outcome.groups for m in g["members"]]


def basic_rows() -> list[Any]:
    return [
        rec("a", 1, criteria=("x",)),
        rec("a", 2, criteria=("y",)),
        rec("a", 3, failing=False),
        rec("b", 1, criteria=("x",)),
    ]


def set_rows() -> list[Any]:
    """Four identical failures of case c: c#1 is its representative (o4), the
    rest form the set s1 (o5-o7)."""
    return [rec("c", n, criteria=("z",), answer="same") for n in range(1, 5)]


# --- Expansion and defects -----------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_answer_expands_like_v1(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows() + set_rows())
    (portion,) = h.plan.portions
    assert [unit.ref for unit in portion.units] == ["o1", "o2", "o3", "o4", "s1"]
    v2 = parse_portion_answer(
        [
            group("g1", ["o1", "o3"], variant="rule", title="Wrong rule"),
            group("g2", ["o2"], variant="product"),
            group("g3", ["o4", "s1"], variant="same"),
        ],
        portion=portion,
        table=h.built.table,
        reported={},
    )
    failing = [r.evidence_id for r in h.registry.records if r.failing]
    v1 = parse_map_response(
        grouping_answer(
            map_group("g1", [("a#1", "rule"), ("b#1", "rule")], title="Wrong rule"),
            map_group("g2", [("a#2", "product")]),
            map_group("g3", [(f"c#{n}", "same") for n in range(1, 5)]),
        ),
        chunk_failing=failing,
        case_of={r.evidence_id: r.case_id for r in h.registry.records},
        provenance_available=False,
    )
    assert v2.defects == v1.defects == ()
    assert v2.groups == v1.groups
    # The session admits the same members under portion-scoped ids.
    answer = finish(
        group("g1", ["o1", "o3"], variant="rule"),
        group("g2", ["o2"]),
        group("g3", ["o4", "s1"]),
    )
    outcome, client = await h.run([answer])
    assert outcome.outcome is None and outcome.requests == 1 and outcome.repairs == 0
    # Admission order: distinct cases, then members (`resolve_overlaps`).
    assert [g["id"] for g in outcome.groups] == ["p1-g1", "p1-g3", "p1-g2"]
    assert sorted(members(outcome)) == sorted(failing)
    assert outcome.groups[0]["affected_cases"] == ["a", "b"]
    assert "evidence_id" not in json.dumps(client.bodies[0]["messages"])


@pytest.mark.asyncio
async def test_ref_defects_and_repair_names_orefs(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows() + set_rows(), capacity=3)
    first, second = h.plan.portions
    assert [u.ref for u in first.units] == ["o1", "o2", "o3"]
    assert [u.ref for u in second.units] == ["o4", "s1"]
    table = h.built.table

    def defects(*groups: dict[str, Any], portion: gp.MapPortion = first) -> tuple[str, ...]:
        parsed = parse_portion_answer(list(groups), portion=portion, table=table, reported={})
        return parsed.defects

    assert defects(group("g1", ["o1", "o2", "o99"]), group("g2", ["o3"])) == (
        "unknown_ref:o99",
        "uncovered:o1",
        "uncovered:o2",
    )
    # o4 and o5 (a member of s1) are references of another portion.
    for ref in ("o4", "o5", "s1"):
        assert f"unknown_ref:{ref}" in defects(group("g1", ["o1", "o2", "o3", ref]))
    assert "unknown_ref" in defects(group("g1", [5]), group("g2", ["o1", "o2", "o3"]))  # type: ignore[list-item]
    assert defects(group("g1", ["o1", "o1"]), group("g2", ["o2", "o3"])) == (
        "repeated_member:o1",
        "uncovered:o1",
    )
    o4 = group("g2", ["o4"])
    assert defects(group("g1", ["s1", "o5"]), o4, portion=second) == (
        "repeated_member:o5",
        "uncovered:s1",
    )
    assert defects(group("g1", ["o5", "s1"]), o4, portion=second)[0] == "repeated_member:s1"
    # A set covered in part names its uncovered o-refs.
    assert defects(group("g1", ["o5", "o6"]), o4, portion=second) == ("uncovered:o7",)
    assert defects(group("g1", ["o1", "o2"]), group("g2", ["o2", "o3"])) == ("overlap:o2",)

    # In a session, the defects go back as one protocol repair, by o-ref.
    script = [
        finish(group("g1", ["o1", "o3"])),
        finish(group("g1", ["o1", "o3"]), group("g2", ["o2"])),
    ]
    outcome, client = await h.run(script)
    assert outcome.outcome is None and outcome.repairs == 1 and outcome.requests == 2
    repair = client.bodies[1]["messages"][-1]["content"]
    assert "uncovered:o2" in repair and "a#2" not in repair
    assert phases(h.records, "protocol_repair_requested")[0]["portion"] == "p1"


@pytest.mark.asyncio
async def test_repairs_spent_keeps_valid_groups_and_uncovers_the_rest(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), max_repairs=1)
    partial = finish(group("g1", ["o1", "o3"]))
    outcome, client = await h.run([partial, partial])
    assert len(client.bodies) == 2 and outcome.repairs == 1
    assert outcome.outcome is None and members(outcome) == ["a#1", "b#1"]
    # An answer with no valid group admits nothing.
    h = Harness(tmp_path / "b", basic_rows(), max_repairs=0)
    outcome, _ = await h.run([finish(group("g1", ["o9"]))])
    assert outcome.outcome == "invalid_output" and outcome.groups == ()


@pytest.mark.asyncio
async def test_invented_provenance_and_named_component_admitted(tmp_path: Path) -> None:
    reported = {"a#1": frozenset({"router", "docs/a b.md"}), "a#2": frozenset({"db"})}
    h = Harness(tmp_path, basic_rows(), reported=reported)
    (portion,) = h.plan.portions
    table = h.built.table

    def parse(*groups: dict[str, Any]) -> Any:
        return parse_portion_answer(list(groups), portion=portion, table=table, reported=reported)

    rest = group("g2", ["o2", "o3"])
    assert parse(group("g1", ["o1"], provenance="db"), rest).defects[0] == "invented_provenance:g1"
    assert parse(group("g1", ["o1"], provenance="route"), rest).defects[0] == (
        "invented_provenance:g1"
    )
    # o3 (b#1) reported nothing: any named ID is invented.
    assert parse(group("g1", ["o1", "o2"]), group("g2", ["o3"], provenance="db")).defects == (
        "invented_provenance:g2",
        "uncovered:o3",
    )
    ok = parse(group("g1", ["o1", "o2"], provenance="router, db; docs/a b.md"), group("g2", ["o3"]))
    assert ok.defects == () and ok.provenance == (("g1", "router, db; docs/a b.md"),)

    answer = finish(
        group(
            "g1",
            ["o1", "o2"],
            provenance="router",
            failure_mode="the router component dropped the request",
        ),
        group("g2", ["o3"]),
    )
    outcome, _ = await h.run([answer])
    assert outcome.outcome is None
    first = outcome.groups[0]
    assert first["provenance"] == "router"
    assert first["failure_mode"] == "the router component dropped the request"
    assert "provenance" not in outcome.groups[1]


# --- Session endings ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_turns_gives_turns_exhausted(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), limits=GroupingV2Limits(max_turns=2))
    read = {"tool_calls": [("list_case", {"case": "c1"})]}
    outcomes, client, _ = await h.map([read, read])
    assert len(client.bodies) == 2
    (outcome,) = outcomes
    assert outcome.outcome == "turns_exhausted" and outcome.groups == ()
    assert h.finished == [outcome]


@pytest.mark.asyncio
async def test_cut_answer_splits_portion(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows())
    cut = {"tool_calls": [("finish_portion", '{"groups": [{"id": "g')], "finish_reason": "length"}
    outcomes, client, records = await h.map([cut, cover_all(), cover_all()])
    assert [o.portion_id for o in outcomes] == ["p1.1", "p1.2"]
    assert [members(o) for o in outcomes] == [["a#1", "a#2"], ["b#1"]]
    (split,) = phases(records, "portion_split")
    assert split["reason"] == "cut" and split["halves"] == ["p1.1", "p1.2"]
    assert split["requests"] == 1
    assert not phases(records, "protocol_repair_requested")  # cut is not repaired
    assert h.budget.requests == 3


@pytest.mark.asyncio
async def test_context_exhausted_splits(tmp_path: Path) -> None:
    rows = [rec("a", n, criteria=(f"k{n}",), answer=f"answer {n} " + "w" * 600) for n in (1, 2)]
    h = Harness(tmp_path, rows)
    (portion,) = h.plan.portions
    tools = grouping_tools(traces=True)

    def opening(p: gp.MapPortion) -> int:
        messages = [
            {"role": "system", "content": "map prompt"},
            {"role": "user", "content": PORTION_LEAD + h.built.render(p)},
        ]
        return chat_request_size(messages, tools)

    halves = gp.split_portion(portion)
    limit = (opening(portion) + max(opening(half) for half in halves)) // 2
    assert max(opening(half) for half in halves) <= limit < opening(portion)
    h.section = section(max_input_chars=limit)
    outcomes, client, records = await h.map([cover_all(), cover_all()])
    assert [o.portion_id for o in outcomes] == ["p1.1", "p1.2"]
    assert len(client.bodies) == 2  # the whole portion was never sent
    assert phases(records, "portion_split")[0]["reason"] == "context_exhausted"
    for body in client.bodies:
        assert chat_request_size(body["messages"], body["tools"]) <= limit


@pytest.mark.asyncio
async def test_long_rejected_answer_ends_context_exhausted_within_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), max_input_chars=30_000)
    long = finish(group("g1", ["o1", "o2", "o3"], title="x" * 40_000))
    outcomes, client, records = await h.map([long, cover_all(), cover_all()])
    assert phases(records, "portion_split")[0]["reason"] == "context_exhausted"
    assert [o.portion_id for o in outcomes] == ["p1.1", "p1.2"]
    for body in client.bodies:
        assert chat_request_size(body["messages"], body["tools"]) <= 30_000


@pytest.mark.asyncio
async def test_session_timeout_splits(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), seconds=0.3)
    slow = {**finish(group("g1", ["o1", "o2", "o3"])), "delay": 1.0}
    outcomes, client, records = await h.map([slow, cover_all(), cover_all()])
    assert [o.portion_id for o in outcomes] == ["p1.1", "p1.2"]
    assert all(o.outcome is None for o in outcomes)
    assert phases(records, "portion_split")[0]["reason"] == "session_timeout"
    assert len(client.bodies) == 3


@pytest.mark.asyncio
async def test_halves_deterministic(tmp_path: Path) -> None:
    rows = basic_rows() + set_rows()
    first = Harness(tmp_path / "one", rows)
    cut = {"tool_calls": [("finish_portion", "{")], "finish_reason": "length"}
    script = [cut, cut, cover_all(), cover_all(), cover_all()]
    one, _, _ = await first.map(list(script))
    second = Harness(tmp_path / "four", rows, concurrency=4)
    four, _, _ = await second.map(list(script))
    assert one == four
    assert [o.portion_id for o in one] == ["p1.1.1", "p1.1.2", "p1.2"]
    # A one-set portion halves its set in o-ref order.
    lone = gp.MapPortion("p9", (first.plan.portions[0].units[4],), ("c",))
    halves = gp.split_portion(lone)
    assert [h.id for h in halves] == ["p9.1", "p9.2"]
    assert [[u.ref for u in h.units] for h in halves] == [["s1.1"], ["o7"]]
    assert halves[0].units[0].evidence_ids == ("c#2", "c#3")


@pytest.mark.asyncio
async def test_one_observation_cut_is_unsplittable_and_takes_fallback(tmp_path: Path) -> None:
    h = Harness(tmp_path, [rec("a", 1), rec("a", 2, failing=False)], max_output_tokens=77)
    cut = {"tool_calls": [("finish_portion", "{")], "finish_reason": "length"}
    outcomes, _, records = await h.map([cut])
    (outcome,) = outcomes
    assert outcome.outcome == "portion_unsplittable" and outcome.requests == 1
    assert h.finished == [outcome]
    (logged,) = phases(records, "portion_unsplittable")
    assert logged["reason"] == "cut" and logged["limit"] == {"max_output_tokens": 77}
    with open_attempt_log(tmp_path / "admit", "a", secrets=()) as log:
        admitted = admit_model_groups(
            h.registry,
            [],
            None,
            family14=False,
            failing=frozenset({"a#1"}),
            policy_identity="p" * 64,
            fallback_features=None,
            forbidden_secret=lambda _: False,
            oracle_needle=lambda _: False,
            log=log,
            calls={},
        )
    assert admitted.fallback is not None and admitted.fallback.scope == "uncovered"


@pytest.mark.asyncio
async def test_one_observation_timeout_raises_deadline_exhausted(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), capacity=1, seconds=0.3)
    slow = {**finish(group("g1", ["o2"])), "delay": 1.0}
    with pytest.raises(RoleCallExhausted) as caught:
        await h.map([cover_all(), slow])
    assert caught.value.reason == "deadline_exhausted"
    # The finished portion was reported before the failure; p3 never started.
    assert [o.portion_id for o in h.finished] == ["p1"]
    assert phases(h.records, "portion_deadline_exhausted")[0]["portion"] == "p2"
    assert not [r for r in phases(h.records, "portion_started") if r["portion"] == "p3"]


@pytest.mark.asyncio
async def test_unsplittable_half_timeout_starts_no_queued_sibling(tmp_path: Path) -> None:
    """p1 times out and splits; its half p1.1
    times out with one observation, a provider failure, so the queued p1.2
    never starts and sends no request."""

    rows = [rec("a", 1, criteria=("x",)), rec("a", 2, criteria=("y",))]
    h = Harness(tmp_path, rows, seconds=0.3)
    slow = {**finish(group("g1", ["o1", "o2"])), "delay": 1.0}
    slow_half = {**finish(group("g1", ["o1"])), "delay": 1.0}
    client = Client([slow, slow_half, cover_all()])
    with open_attempt_log(tmp_path / "logs-q", "a", secrets=()) as log:
        try:
            with pytest.raises(RoleCallExhausted) as caught:
                await map_portions(
                    h.context(client, log), h.plan, adopted={}, on_finished=h.finished.append
                )
        finally:
            records = read_log(log.path)
    assert caught.value.reason == "deadline_exhausted"
    assert phases(records, "portion_split")[0]["halves"] == ["p1.1", "p1.2"]
    assert phases(records, "portion_deadline_exhausted")[0]["portion"] == "p1.1"
    assert [r["portion"] for r in phases(records, "portion_started")] == ["p1", "p1.1"]
    assert len(client.bodies) == 2  # no provider request for p1.2
    assert h.finished == []


@pytest.mark.asyncio
async def test_empty_answer_without_length_is_repaired_not_split(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows())
    empty = {"content": "", "finish_reason": "stop"}
    outcomes, client, records = await h.map([empty, cover_all()])
    (outcome,) = outcomes
    assert outcome.portion_id == "p1" and outcome.repairs == 1
    assert not phases(records, "portion_split")
    assert "rejected locally" in client.bodies[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_record_budget_splits(tmp_path: Path) -> None:
    rows = [rec("a", n, criteria=(f"k{n}",)) for n in range(1, 5)]
    h = Harness(tmp_path, rows)
    (portion,) = h.plan.portions
    whole = await h.run([cover_all()])
    size = checkpoint_size(whole[0])
    h.record_budget = size - 1
    outcomes, _, records = await h.map([cover_all(), cover_all(), cover_all()])
    assert phases(records, "portion_split")[0]["reason"] == "record_budget"
    assert [o.portion_id for o in outcomes] == ["p1.1", "p1.2"]
    assert all(checkpoint_size(o) <= size - 1 for o in outcomes)
    assert [o.portion_id for o in h.finished] == ["p1.1", "p1.2"]
    assert portion.id == "p1"


# --- The map: order, adoption, failures ----------------------------------------------------


@pytest.mark.asyncio
async def test_on_finished_called_once_per_admitted_portion_in_plan_order(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, basic_rows(), capacity=1)
    outcomes, _, _ = await h.map([cover_all(), cover_all(), cover_all()])
    assert [o.portion_id for o in outcomes] == ["p1", "p2", "p3"]
    assert h.finished == list(outcomes)

    # Adopted portions are neither requested nor reported again.
    again = Harness(tmp_path / "adopted", basic_rows(), capacity=1)
    out, client, _ = await again.map([cover_all()], adopted={"p1": outcomes[0], "p3": outcomes[2]})
    assert out == outcomes and len(client.bodies) == 1
    assert [o.portion_id for o in again.finished] == ["p2"]

    # Under concurrency, completion order does not change the result's order.
    def slow(seconds: float) -> Callable[[dict[str, Any]], dict[str, Any]]:
        return lambda body: {**cover_all()(body), "delay": seconds}

    racing = Harness(tmp_path / "racing", basic_rows(), capacity=1, concurrency=3)
    out, _, _ = await racing.map([slow(0.3), slow(0.15), slow(0.0)])
    assert [o.portion_id for o in out] == ["p1", "p2", "p3"]
    assert sorted(o.portion_id for o in racing.finished) == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_adopted_half_splits_again_without_a_request(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows())
    half = PortionOutcome("p1.1", (), "turns_exhausted", 2, 0)
    outcomes, client, _ = await h.map([cover_all()], adopted={"p1.1": half})
    assert [o.portion_id for o in outcomes] == ["p1.1", "p1.2"]
    assert outcomes[0] is half and len(client.bodies) == 1
    assert portion_units(client.bodies[0]) == ["o3"]


@pytest.mark.asyncio
async def test_provider_failure_stops_new_sessions_and_cancels_running(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), capacity=1, concurrency=2)
    script: list[Any] = [server_error(), {**finish(group("g1", ["o2"])), "delay": 5.0}]
    started = time.monotonic()
    with pytest.raises(RoleCallExhausted) as caught:
        await h.map(script)
    assert caught.value.reason == "provider_unavailable"
    assert time.monotonic() - started < 4  # the slow session was cancelled
    assert h.finished == []
    assert not [r for r in phases(h.records, "portion_started") if r["portion"] == "p3"]


@pytest.mark.asyncio
async def test_max_requests_stops_new_requests(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows(), capacity=1, cap=2)
    outcomes, client, records = await h.map([cover_all(), cover_all(), cover_all()])
    assert len(client.bodies) == 2
    assert [o.outcome for o in outcomes] == [None, None, MAX_REQUESTS]
    assert [o.portion_id for o in h.finished] == ["p1", "p2"]
    assert phases(records, "max_requests_reached")[0]["portion"] == "p3"


@pytest.mark.asyncio
async def test_secret_in_response_is_logged_redacted(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows())
    answer = finish(group("g1", ["o1", "o2", "o3"], title=SECRET), usage={"completion_tokens": 9})
    with pytest.raises(GroupingExecutionError, match="secret_detected"):
        await h.map([answer])
    text = json.dumps(h.records)
    assert SECRET not in text
    (redacted,) = phases(h.records, "model_response")
    assert redacted["content"] == "[redacted]" and redacted["portion"] == "p1"
    assert redacted["usage"] == {"completion_tokens": 9} and "finish_reason" in redacted


@pytest.mark.asyncio
async def test_cancelled_grouping_sends_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows())
    client = Client([cover_all()])
    with open_attempt_log(tmp_path / "logs-c", "a", secrets=()) as log:
        ctx = h.context(client, log)
        ctx = PortionContext(**{**ctx.__dict__, "cancelled": lambda: True})
        with pytest.raises(GroupingExecutionError, match="cancelled"):
            await map_portions(ctx, h.plan, adopted={}, on_finished=h.finished.append)
    assert client.bodies == []


# --- Records -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_record_round_trips(tmp_path: Path) -> None:
    h = Harness(tmp_path, basic_rows())
    answer = finish(
        group("g1", ["o1", "o3"], metrics={"score": 0.25}, provenance=""),
        group("g2", ["o2"]),
    )
    outcome, _ = await h.run([answer])
    case_of = {r.evidence_id: r.case_id for r in h.registry.records}
    for record in outcome.groups:
        assert group_record(model_group(record, case_of), record.get("provenance")) == record
    body = checkpoint_body(outcome)
    assert set(body) == {"portion_id", "requests", "groups"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert checkpoint_size(outcome) == len(canonical.encode())
    empty = PortionOutcome("p2", (), "invalid_output", 3, 2)
    assert checkpoint_body(empty) == {
        "portion_id": "p2",
        "requests": 3,
        "outcome": "invalid_output",
    }


def test_result_envelope_covers_a_full_tool_result(tmp_path: Path) -> None:
    rows = [rec("a", 1, answer="a" * 50_000), rec("a", 2, failing=False)]
    reg = registry(rows)
    built = Built(reg, capacity=16)
    view, _ = fake_view(reg, page=7_000)
    reader = view.open_operation("group-op1", set(view._handlers))
    box = GroupingToolbox(
        built.plan.portions[0], built.table, reader, read_chars=16_000, glimpses=built.glimpses
    )
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-12-0", "type": "function", "function": {}}],
    }
    for name, arguments in (
        ("read_observation", {"case": "c1", "repeat": 1, "section": "observation"}),
        ("read_trace", {"case": "c1", "repeat": 1, "offset": 123}),
    ):
        output = asyncio.run(box.run(name, arguments))
        assert len(output["content"]) == 16_000
        (_, message) = tool_exchange(assistant, output)
        envelope = chat_request_size([message], []) - len(output["content"])
        assert envelope <= RESULT_ENVELOPE_CHARS


# --- `/16`: a broken tool call is retained as `{}` (ADR-0061, b18 Part A) ------------------


@pytest.mark.parametrize("mode", ["http_400", "error_body"])
@pytest.mark.asyncio
async def test_v16_portion_session_survives_strict_provider(tmp_path: Path, mode: str) -> None:
    from tests.strict_history import StrictScriptedClient, broken_history_calls, holds, measures

    answer = finish(group("g1", ["o1", "o3"]), group("g2", ["o2"]))
    reference, _ = await Harness(tmp_path / "reference", basic_rows()).run([answer])

    h = Harness(tmp_path, basic_rows())
    marker = "broken-portion-marker-91c"
    broken = '{"groups": [{"id": "' + marker + '", "members": ['
    inner = Client([{"tool_calls": [("finish_portion", broken)]}, answer])
    strict = StrictScriptedClient(inner, mode)  # type: ignore[arg-type]
    with open_attempt_log(tmp_path / "logs-strict", "a", secrets=()) as log:
        try:
            outcome = await run_portion(h.context(strict, log), h.plan.portions[0])
        finally:
            records = read_log(log.path)

    assert strict.refusals == []
    assert outcome.outcome is None and outcome.repairs == 1 and outcome.requests == 2
    # The admitted answer is the one a session without the broken turn admits.
    assert outcome.groups == reference.groups
    (repair,) = phases(records, "protocol_repair_requested")
    assert repair["error"] == "tool arguments are not JSON"
    assert repair["replaced_calls"] == [
        {"id": "call-1-0", "name": "finish_portion", **measures(broken)}
    ]
    assert repair["portion"] == "p1"
    retained = inner.bodies[1]["messages"][len(inner.bodies[0]["messages"])]
    assert retained["tool_calls"][0]["function"]["arguments"] == "{}"
    assert all(broken_history_calls(body) == [] for body in inner.bodies)
    assert holds(phases(records, "model_response"), broken)  # the check below can see it
    for record in records:
        if record["phase"] != "model_response":
            assert not holds(record, broken) and not holds(record, marker), record["phase"]
    for body in inner.bodies:
        assert not holds(body, broken) and not holds(body, marker)
