"""The family-15 chat Grouper's read tools over the grouping reader (spec §6.1)."""

import asyncio
import hashlib
import json

import pytest

from evolution.tree.roles.editor.chat_editor import ToolError
from evolution.tree.roles.grouping.agent_grouper import GROUPING_METHODS
from evolution.tree.roles.grouping.grouping_tools import GroupingToolbox, grouping_tools
from evolution.tree.roles.reflection_reader import (
    FrozenReflectionView,
    ReaderArgumentError,
    ReaderIntegrityError,
)
from tests.tree.roles.grouping.test_grouping_portions import Built, rec, registry

TRACE = "T" * 40_000


def page_of(text, offset, size):
    if type(offset) is not int or offset < 0 or offset > len(text):
        raise ReaderArgumentError("invalid_offset")
    end = min(len(text), offset + size)
    return {
        "content": text[offset:end],
        "content_digest": hashlib.sha256(text.encode()).hexdigest(),
        "offset": offset,
        "total_characters": len(text),
        "next_offset": end if end < len(text) else None,
    }


def fake_view(reg, *, page=16_000, entries_per_page=3, traces=True, trace_page=7_000, faults=()):
    """The handlers the real grouping reader registers, with small pages."""
    records = list(reg.records)
    case_ids = sorted({r.case_id for r in records})
    calls = []

    def list_inputs(arguments):
        calls.append(("list_grouping_inputs", dict(arguments)))
        offset = arguments.get("offset", 0)
        chunk = records[offset : offset + entries_per_page]
        stop = offset + len(chunk)
        return {
            "entries": [
                {
                    "index": offset + n,
                    "evidence_id": r.evidence_id,
                    "case_index": case_ids.index(r.case_id),
                    "repeat": r.repeat,
                    "available": r.available,
                    "failing": r.failing,
                }
                for n, r in enumerate(chunk)
            ],
            "total": len(records),
            "next_offset": stop if stop < len(records) else None,
        }

    def read_input(arguments):
        calls.append(("read_grouping_input", dict(arguments)))
        if "read_input" in faults:
            raise ReaderIntegrityError("source_changed")
        record = records[arguments["index"]]
        if arguments["section"] not in record.sources and arguments["section"] != "identity":
            raise ReaderArgumentError("unknown_section")
        value = (
            {"case_id": record.case_id, "repeat": record.repeat}
            if arguments["section"] == "identity"
            else record.sources[arguments["section"]]
        )
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return page_of(text, arguments.get("offset", 0), page)

    handlers = {"list_grouping_inputs": list_inputs, "read_grouping_input": read_input}
    if traces:

        def read_trace(arguments):
            calls.append(("read_trace", dict(arguments)))
            if arguments["evidence_id"].startswith("kb#"):
                raise ReaderArgumentError("trace_unavailable")
            out = page_of(TRACE, arguments.get("offset", 0), trace_page)
            return {"evidence_id": arguments["evidence_id"], **out}

        handlers["read_trace"] = read_trace
    return FrozenReflectionView(handlers), calls


def toolbox(rows, *, read_chars=16_000, portion_index=0, **view_options):
    reg = registry(rows)
    built = Built(reg, capacity=100)
    view, calls = fake_view(reg, **view_options)
    methods = set(view._handlers)
    reader = view.open_operation("group-op1", methods)
    portion = built.plan.portions[portion_index]
    box = GroupingToolbox(
        portion, built.table, reader, read_chars=read_chars, glimpses=built.glimpses
    )
    return box, built, calls, reader


def run(box, name, arguments):
    return asyncio.run(box.run(name, arguments))


def basic_rows():
    return [
        rec("ka", 1, answer="alpha " * 400),
        rec("ka", 2, failing=False),
        rec("ka", 3, terminal="timeout", answer="beta"),
        rec("kb", 1, answer="gamma"),
        rec("kc", 1, answer="delta"),
    ]


def test_tools_serve_only_portion_cases():
    reg = registry(basic_rows())
    built = Built(reg, capacity=100, input_budget=2_000)
    assert len(built.plan.portions) > 1
    portion = built.plan.portions[0]
    other = next(c for c in built.table.cases if c not in portion.case_ids)
    view, _ = fake_view(reg)
    reader = view.open_operation("group-op1", set(view._handlers))
    box = GroupingToolbox(portion, built.table, reader, read_chars=2_000, glimpses=built.glimpses)
    mine = built.table.case_ref(portion.case_ids[0])
    assert run(box, "list_case", {"case": mine})["repeats"]
    assert run(box, "read_observation", {"case": mine, "repeat": 1, "section": "observation"})
    foreign = built.table.case_ref(other)
    for name, args in (
        ("list_case", {"case": foreign}),
        ("read_observation", {"case": foreign, "repeat": 1, "section": "observation"}),
        ("read_trace", {"case": foreign, "repeat": 1}),
        ("list_case", {"case": "c99"}),
        ("list_case", {"case": 1}),
        ("read_observation", {"case": mine, "repeat": 99, "section": "observation"}),
        ("read_observation", {"case": mine, "repeat": True, "section": "observation"}),
        ("read_observation", {"case": mine, "repeat": 1, "section": "nope"}),
        ("read_observation", {"case": mine, "repeat": 1}),
        ("list_case", {"case": mine, "offset": -1}),
        ("write_file", {}),
    ):
        with pytest.raises(ToolError):
            run(box, name, args)


def test_read_trace_absent_without_traces():
    assert [t["function"]["name"] for t in grouping_tools(traces=False)] == [
        "list_case",
        "read_observation",
        "finish_portion",
    ]
    assert [t["function"]["name"] for t in grouping_tools(traces=True)] == [
        "list_case",
        "read_observation",
        "read_trace",
        "finish_portion",
    ]
    box, built, _, _ = toolbox(basic_rows(), traces=False)
    with pytest.raises(ToolError):
        run(box, "read_trace", {"case": "c1", "repeat": 1})


def test_missing_trace_reports_unavailable():
    box, *_ = toolbox(basic_rows())
    with pytest.raises(ToolError, match="trace_unavailable"):
        run(box, "read_trace", {"case": "c2", "repeat": 1})


@pytest.mark.parametrize("read_chars", [1_000, 50_000])
def test_each_tool_pages_within_read_chars(read_chars):
    box, built, calls, _ = toolbox(basic_rows(), read_chars=read_chars)
    # A trace of 40 000 characters, read in the box's pages, equals the source.
    text, offset = "", 0
    while offset is not None:
        result = run(box, "read_trace", {"case": "c1", "repeat": 1, "offset": offset})
        assert len(result["content"]) <= read_chars
        assert result["offset"] == offset
        assert result["total_characters"] == len(TRACE)
        assert "evidence_id" not in result
        text += result["content"]
        offset = result["next_offset"]
    assert text == TRACE
    # A section the reader serves in 16 000 characters or fewer.
    text, offset = "", 0
    section = json.dumps(
        registry(basic_rows()).records[0].sources["observation"],
        sort_keys=True,
        separators=(",", ":"),
    )
    while offset is not None:
        result = run(
            box,
            "read_observation",
            {"case": "c1", "repeat": 1, "section": "observation", "offset": offset},
        )
        assert len(result["content"]) <= read_chars
        text += result["content"]
        offset = result["next_offset"]
    assert text == section
    # list_case pages by entries within read_chars.
    seen, offset = [], 0
    while offset is not None:
        result = run(box, "list_case", {"case": "c1", "offset": offset})
        assert len(json.dumps(result["repeats"], ensure_ascii=False)) <= read_chars
        seen += [r["repeat"] for r in result["repeats"]]
        offset = result["next_offset"]
    assert seen == [1, 2, 3]


def test_reader_pages_larger_than_read_chars_are_resliced():
    box, *_ = toolbox(basic_rows(), read_chars=50_000, page=1_000, trace_page=500)
    result = run(box, "read_trace", {"case": "c1", "repeat": 1})
    assert len(result["content"]) == 40_000 and result["next_offset"] is None


def test_list_case_shows_other_portions_without_assigning():
    reg = registry(basic_rows())
    built = Built(reg, capacity=1)
    first = built.plan.portions[0]
    assert len(built.plan.portions) > 1
    view, _ = fake_view(reg)
    reader = view.open_operation("group-op1", set(view._handlers))
    box = GroupingToolbox(first, built.table, reader, read_chars=16_000, glimpses=built.glimpses)
    case = built.table.case_ref(first.case_ids[-1])
    refs = first.references()
    repeats = run(box, "list_case", {"case": case})["repeats"]
    assert {r["repeat"] for r in repeats} >= {1}
    states = {r["repeat"]: r["state"] for r in repeats}
    outside = [r for r in repeats if r["ref"] is not None and r["ref"] not in refs]
    assert all(r["in_portion"] is False for r in outside)
    for r in repeats:
        assert r["in_portion"] == (r["ref"] in refs)
        assert (r["ref"] is None) == (states[r["repeat"]] != "failing")
        if r["state"] == "failing":
            assert r["glimpse"] == built.glimpses[f"{case_id(built, case)}#{r['repeat']}"]
    assert any(
        r["state"] == "passing" for r in run(box, "list_case", {"case": "c1"})["repeats"]
    ) or (case != "c1")


def case_id(built, ref):
    return built.table.cases[int(ref[1:]) - 1]


def test_integrity_error_propagates():
    box, *_ = toolbox(basic_rows(), faults=("read_input",))
    with pytest.raises(ReaderIntegrityError):
        run(box, "read_observation", {"case": "c1", "repeat": 1, "section": "observation"})


def test_agent_cli_method_set_unchanged():
    assert GROUPING_METHODS == {
        "list_grouping_inputs",
        "read_grouping_input",
        "stream_grouping_inputs",
    }


def test_finish_portion_schema_and_never_executed():
    tool = grouping_tools(traces=True)[-1]["function"]
    group = tool["parameters"]["properties"]["groups"]["items"]
    assert set(group["required"]) == {
        "id", "title", "failure_mode", "expected_behaviour", "question", "members",
        "citations", "priority",
    }  # fmt: skip
    assert {"metrics", "provenance"} <= set(group["properties"])
    box, *_ = toolbox(basic_rows())
    with pytest.raises(AssertionError):
        run(box, "finish_portion", {"groups": []})


@pytest.mark.parametrize("read_chars", [300, 2_000])
def test_read_trace_over_real_baseline_traces_is_bounded_by_read_chars(tmp_path, read_chars):
    """`read_trace` serves the recorded episode through a real `BaselineTraces`
    (spec §6.1): each result holds at most ``read_chars`` characters, and the
    pages join to the reader's whole redacted trace."""

    from evolution.tree.execution.journal_identity import canonical_json
    from tests.test_tree_baseline_traces import reader as baseline_reader
    from tests.tree.roles.editor.test_editor_evidence import _Trace

    trace = _Trace(tmp_path, turns=2)
    try:
        traces = baseline_reader(trace)  # the reader's own pages are 256 characters
        (evidence_id,) = traces.ids()
        whole = traces.whole(evidence_id)
        # The recorded episode itself: the scorer's view of the sample.
        assert whole == canonical_json(trace.input["view"]).decode()
        assert len(whole) > 2 * read_chars
        reg = registry([rec("c", 1, evidence_id=evidence_id)])
        built = Built(reg, capacity=100)
        handlers = dict(fake_view(reg, traces=False)[0]._handlers)
        handlers["read_trace"] = traces.read
        view = FrozenReflectionView(handlers)
        session = view.open_operation("group-op1", set(handlers))
        box = GroupingToolbox(
            built.plan.portions[0],
            built.table,
            session,
            read_chars=read_chars,
            glimpses=built.glimpses,
        )
        assert box.traces
        text, offset, results = "", 0, 0
        while offset is not None:
            result = run(box, "read_trace", {"case": "c1", "repeat": 1, "offset": offset})
            assert len(result["content"]) <= read_chars
            assert result["offset"] == offset and result["total_characters"] == len(whole)
            assert "evidence_id" not in result
            text += result["content"]
            offset = result["next_offset"]
            results += 1
        assert text == whole and results > 1
    finally:
        trace.close()
