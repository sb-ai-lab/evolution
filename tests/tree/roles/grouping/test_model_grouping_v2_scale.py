"""Scale checks of the family-15 chat Grouper on synthetic registries (b16 spec §4, §18).

Offline, with a scripted model. Each test records the size in characters and
the estimated tokens (characters / 4) of every request and answer: in its
output (`-s`), and as the `sizes` property of the JUnit report.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.roles.config import GroupingV2Limits
from evolution.tree.roles.grouping.reflect_grouping import GroupingRecord
from evolution.tree.roles.reflection_reader import FrozenReflectionView
from tests.tree.roles.grouping.test_grouping_portions import rec, registry
from tests.tree.roles.grouping.test_grouping_tools import fake_view, page_of
from tests.tree.roles.grouping.test_model_grouping_v2 import (
    Model,
    Run,
    answer_group,
    by_criteria,
    finish,
    members,
    portion_units,
)

#: One setting for every R (§18): `timeout_seconds` included.
SETTINGS: dict[str, Any] = {
    "max_input_chars": 40_000,
    "max_output_tokens": 4096,
    "concurrency": 4,
    "timeout_seconds": 60,
}
#: Four units per portion and eight cards per batch, so that from R = 10 on
#: both a portion and a batch are full and only their number grows.
LIMITS = GroupingV2Limits(
    glimpse_chars=400, read_chars=2_000, group_output_tokens=1024, max_reduce_levels=10
)
CASES = 6


def sizes(model: Model) -> list[dict[str, Any]]:
    """Every exchange's request and answer size, characters and estimated tokens."""

    return [
        {
            "kind": e["kind"],
            "request_chars": e["request_chars"],
            "request_tokens": e["request_chars"] // 4,
            "answer_chars": e["answer_chars"],
            "answer_tokens": e["answer_chars"] // 4,
        }
        for e in model.exchanges
    ]


def report(name: str, rows: list[dict[str, Any]], record: Callable[[str, object], None]) -> None:
    record("sizes", json.dumps(rows))
    print(f"\n{name}: {len(rows)} exchanges")
    for n, row in enumerate(rows):
        print(
            f"  {n:4d} {row['kind']:9s} request {row['request_chars']:7d} chars "
            f"~{row['request_tokens']:6d} tok | answer {row['answer_chars']:6d} chars "
            f"~{row['answer_tokens']:5d} tok"
        )


def scale_rows(repeats: int) -> list[GroupingRecord]:
    """``CASES`` cases of ``repeats`` failing observations; each answer is its own,
    so no two observations share a glimpse, and the criteria cycle over three."""

    rows = []
    for case in range(CASES):
        for repeat in range(1, repeats + 1):
            criteria = ("x", "y", "z")[(case + repeat) % 3]
            rows.append(
                rec(
                    f"k{case}",
                    repeat,
                    criteria=(criteria,),
                    answer=f"answer {case}/{repeat} " + "w" * 600,
                )
            )
    return rows


_RUNS: dict[int, dict[str, Any]] = {}


async def scale_run(tmp_path: Path, repeats: int) -> dict[str, Any]:
    if repeats not in _RUNS:
        run = Run(tmp_path / f"r{repeats}", scale_rows(repeats), limits=LIMITS, **SETTINGS)
        model = Model()
        outcome = await run(model)
        plan = next(r for r in run.records if r["phase"] == "portion_plan")
        rows = sizes(model)
        _RUNS[repeats] = {
            "outcome": outcome,
            "portions": plan["portions"],
            "requests": len(rows),
            "max_request": max(r["request_chars"] for r in rows),
            "max_answer": max(r["answer_chars"] for r in rows),
            "sizes": rows,
            "max_tokens": {e["body"].get("max_tokens") for e in model.exchanges},
            "units": [
                len(portion_units(e["body"])) for e in model.exchanges if e["kind"] == "portion"
            ],
        }
    return _RUNS[repeats]


@pytest.mark.parametrize("repeats", [1, 10, 100])
async def test_scale_requests_and_answers_bounded_whatever_r(
    tmp_path: Path, repeats: int, record_property: Callable[[str, object], None]
) -> None:
    result = await scale_run(tmp_path, repeats)
    report(f"R={repeats}", result["sizes"], record_property)
    outcome = result["outcome"]
    assert outcome.fallback is None
    # Consolidation merged the groups of different portions: one per criteria list.
    assert len(outcome.grouped.neighborhoods) == 3
    assert sum(len(n.member_ids) for n in outcome.grouped.neighborhoods) == CASES * repeats
    # Every request within `max_input_chars`; every request asks for at most
    # `max_output_tokens`, and no portion holds more units than its capacity.
    assert result["max_request"] <= SETTINGS["max_input_chars"]
    assert result["max_tokens"] == {SETTINGS["max_output_tokens"]}
    capacity = SETTINGS["max_output_tokens"] // LIMITS.group_output_tokens
    assert result["units"] and max(result["units"]) <= capacity
    if repeats == 1:
        return
    smaller = await scale_run(tmp_path, {10: 1, 100: 10}[repeats])
    # The numbers of portions and requests grow with R ...
    assert result["portions"] > smaller["portions"]
    assert result["requests"] > smaller["requests"]
    # ... and the largest request and answer do not, once portions and batches
    # are full: only the digits of the case headers' counts grow.
    if repeats == 100:
        for kind in ("portion", "batch"):
            for side in ("request_chars", "answer_chars"):
                now = max(row[side] for row in result["sizes"] if row["kind"] == kind)
                before = max(row[side] for row in smaller["sizes"] if row["kind"] == kind)
                assert now <= before * 1.05, (kind, side, now, before)


# --- Very long traces -------------------------------------------------------------------------


def trace_of(evidence_id: str) -> str:
    lines = [f"{evidence_id} step {n:05d}: the agent did something\n" for n in range(3_000)]
    return "".join(lines)


async def test_long_traces_glimpses_bounded_read_trace_pages_all(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    rows = [rec("long", n, answer=f"{n} " + "v" * 80_000) for n in (1, 2)]
    reg = registry(rows)
    view, _ = fake_view(reg)
    handlers = dict(view._handlers)

    def read_trace(arguments: dict[str, Any]) -> dict[str, Any]:
        out = page_of(trace_of(arguments["evidence_id"]), arguments.get("offset", 0), 5_000)
        return {"evidence_id": arguments["evidence_id"], **out}

    handlers["read_trace"] = read_trace
    reader = FrozenReflectionView(handlers).open_operation("group-long", set(handlers))
    pages: list[str] = []

    def session(body: dict[str, Any]) -> dict[str, Any]:
        messages = body["messages"]
        last = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else None
        if last is None:
            return {"tool_calls": [("read_trace", {"case": "c1", "repeat": 1, "offset": 0})]}
        pages.append(last["content"])
        if last["next_offset"] is not None:
            arguments = {"case": "c1", "repeat": 1, "offset": last["next_offset"]}
            return {"tool_calls": [("read_trace", arguments)]}
        return by_criteria(body)

    limits = GroupingV2Limits(glimpse_chars=400, read_chars=2_000, max_turns=200)
    run = Run(tmp_path, reg, limits=limits, reader=reader, **SETTINGS)
    model = Model(portion=session)
    outcome = await run(model)
    report("long traces", sizes(model), record_property)

    assert outcome.fallback is None
    assert "".join(pages) == trace_of("long#1")  # the whole recorded episode, page by page
    first = next(e for e in model.exchanges if e["kind"] == "portion")
    glimpses = [u.get("glimpse", "") for u in portion_units(first["body"])]
    assert all(len(g) <= limits.glimpse_chars for g in glimpses)
    assert max(e["request_chars"] for e in model.exchanges) <= SETTINGS["max_input_chars"]
    assert any(r["phase"] == "tool_call" for r in run.records)


# --- One signature hiding two failures --------------------------------------------------------


async def test_one_signature_hiding_two_failures_split_by_model(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    """Six observations of one case with one signature and one glimpse form a
    set; the answers differ only past the glimpse. The scripted model reads
    them and splits the set by listing o-refs; no answer is large."""

    def answer(marker: str) -> str:
        return "H" * 6_000 + marker + "T" * 6_000

    rows = [rec("k", n, answer=answer("ALPHA" if n <= 3 else "OMEGA")) for n in range(1, 7)]
    reg = registry(rows)

    listed: list[dict[str, Any]] = []
    read: list[str] = []

    def session(body: dict[str, Any]) -> dict[str, Any]:
        # Older reads are elided from the conversation to stay within
        # `max_input_chars`; the model keeps what it read, as a model would.
        last = body["messages"][-1]
        if last["role"] != "tool":
            return {"tool_calls": [("list_case", {"case": "c1"})]}
        result = json.loads(last["content"])
        if "repeats" in result:
            listed.extend(result["repeats"])
        else:
            read.append(result["content"])
        if len(read) < len(listed):
            repeat = listed[len(read)]["repeat"]
            arguments = {"case": "c1", "repeat": repeat, "section": "observation"}
            return {"tool_calls": [("read_observation", arguments)]}
        alpha = [item["ref"] for item, page in zip(listed, read, strict=True) if "ALPHA" in page]
        omega = [item["ref"] for item, page in zip(listed, read, strict=True) if "OMEGA" in page]
        return finish(
            answer_group("g1", alpha, title="alpha"), answer_group("g2", omega, title="omega")
        )

    limits = GroupingV2Limits(glimpse_chars=400, read_chars=8_000, max_turns=12)
    run = Run(tmp_path, reg, limits=limits, representatives_per_case=1, **SETTINGS)
    model = Model(portion=session)
    outcome = await run(model)
    report("one signature, two failures", sizes(model), record_property)

    first = next(e for e in model.exchanges if e["kind"] == "portion")
    units = portion_units(first["body"])
    assert any("members" in unit for unit in units)  # code formed a set ...
    assert members(outcome) == {
        frozenset({"k#1", "k#2", "k#3"}),
        frozenset({"k#4", "k#5", "k#6"}),
    }  # ... and the model split it
    assert outcome.fallback is None
    assert max(e["answer_chars"] for e in model.exchanges) < 2_000
    assert max(e["request_chars"] for e in model.exchanges) <= SETTINGS["max_input_chars"]
