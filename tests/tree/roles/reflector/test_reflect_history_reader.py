"""History on demand."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor.editor_evidence import project_history
from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2, history_payload
from evolution.tree.roles.reflection_reader import (
    ByteLruCache,
    FrozenReflectionView,
    ReaderArgumentError,
)
from evolution.tree.roles.reflection_role_helpers import install_helpers
from evolution.tree.roles.reflector.reflect_aggregate import FactRow, serialized_chars
from evolution.tree.roles.reflector.reflect_change import PathChange
from evolution.tree.roles.reflector.reflect_history import (
    AttemptFacts,
    HistoryReader,
    attempt_changes_record,
    build_history_source,
)
from evolution.tree.roles.reflector_contracts import ATTEMPT_CHANGES_MAX_CHARS, REFLECT_LIMITS_V1
from tests.campaign_fakes import corpus
from tests.reflect_history_fakes import World


def _world(tmp_path: Path, rounds: int = 3) -> World:
    world = World(tmp_path)
    for number in range(1, rounds + 1):
        attempts = [
            world.attempt(number, k, {"skills/a/SKILL.md": f"r{number} k{k}\n"}, score=0.25 * k)
            for k in (1, 2)
        ]
        world.add_round(
            number,
            attempts,
            diagnosis=[
                {
                    "root_cause": "r",
                    "suggested_fix": "f",
                    "target_paths": ["skills/a/SKILL.md"],
                    "affected_cases": ["case-001"],
                    "evidence_ids": ["obs:1"],
                }
            ],
            exam=world.cases[:2],
        )
    return world


def _reader(
    world: World, reflection_id: str = "r09-x01", cache: ByteLruCache | None = None, **kwargs: Any
) -> HistoryReader:
    sources = {**world.sources(), **kwargs}
    source = build_history_source(
        world.snapshot(), visible=None, screen=lambda _l: False, **sources
    )
    return HistoryReader(source, reflection_id=reflection_id, cache=cache or ByteLruCache())


def test_list_history_enumerates_every_id_newest_round_first(tmp_path: Path) -> None:
    reader = _reader(_world(tmp_path))
    page = reader.list_history({})
    ids = [entry["history_id"] for entry in page["entries"]]
    assert ids[:5] == [
        "diagnosis:3",
        "attempt:r03-a01",
        "changes:r03-a01",
        "attempt:r03-a02",
        "changes:r03-a02",
    ]
    assert page["total"] == 15 and page["next_offset"] is None
    assert {entry["kind"] for entry in page["entries"]} == {"diagnosis", "attempt", "changes"}
    with pytest.raises(ReaderArgumentError, match="invalid_offset"):
        reader.list_history({"offset": 99})
    with pytest.raises(ReaderArgumentError, match="invalid_arguments"):
        reader.list_history({"cursor": 1})


def test_attempt_and_diagnosis_bodies_are_todays_entries(tmp_path: Path) -> None:
    world = _world(tmp_path)
    cases = [
        replace(case, case_id=case_id) for case, case_id in zip(corpus(4), world.cases, strict=True)
    ]
    reader = _reader(world, corpus=cases)
    today = history_payload(
        project_history(
            world.snapshot(),
            measurements=world.rows,
            store=world.store(),
            artifact_policy=world.sources()["artifact_policy"],
            corpus=cases,
            policy=world.sources()["policy"],
            limits={**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1},
            excerpts=True,
            visible=None,
        ),
        excerpts=True,
    )
    for round_ in today["rounds"]:
        for attempt in round_["attempts"]:
            assert attempt["result"]["status"] == "present"
            assert [case["case_id"] for case in attempt["result"]["cases"]] == world.cases
            for case in attempt["result"]["cases"]:
                assert len(case["repeats"]) == world.repeats
                assert all(repeat["terminal"] == "completed" for repeat in case["repeats"])
                assert all(
                    repeat["primary_score"] == 0.25 * attempt["index"] for repeat in case["repeats"]
                )
            body = reader.read_history({"history_id": f"attempt:{attempt['attempt_id']}"})
            assert body["content"] == canonical_json(attempt).decode("utf-8")
        body = reader.read_history({"history_id": f"diagnosis:{round_['round']}"})
        assert body["content"] == canonical_json(round_["diagnosis"]).decode("utf-8")


def test_changes_records_match_the_aggregate(tmp_path: Path) -> None:
    world = _world(tmp_path)
    reader = _reader(world)
    source = build_history_source(
        world.snapshot(), visible=None, screen=lambda _l: False, **world.sources()
    )
    for path in source.aggregate.full["paths"]:
        for change in path["changes"]:
            for witness in change["witnesses"]:
                record = json.loads(
                    reader.read_history(
                        {"history_id": witness["attempt"].replace("attempt:", "changes:")}
                    )["content"]
                )
                assert record["version"] == "reflect_attempt_changes/1"
                assert {(p["path"], p["fingerprint"]) for p in record["paths"]} >= {
                    (path["path"], change["fingerprint"])
                }
                assert (
                    next(
                        p["change"]
                        for p in record["paths"]
                        if p["fingerprint"] == change["fingerprint"]
                    )
                    == change["change"]
                )


def test_unknown_ids_and_stale_history_are_refused(tmp_path: Path) -> None:
    cache = ByteLruCache()
    early = _world(tmp_path / "early", rounds=1)
    late = _world(tmp_path / "late", rounds=2)
    first = _reader(early, "r02-x01", cache)
    second = _reader(late, "r03-x01", cache)
    second.read_history({"history_id": "attempt:r02-a01"})
    with pytest.raises(ReaderArgumentError, match="unknown_history"):
        first.read_history({"history_id": "attempt:r02-a01"})
    with pytest.raises(ReaderArgumentError, match="unknown_history"):
        first.read_history({"history_id": "attempt:nope"})
    assert all(key.startswith("history:r03-x01:") for key in cache._items)  # noqa: SLF001


def test_a_changes_record_stays_within_its_bound() -> None:
    worst = {
        "basis": "too_large",
        "before": "binary+x",
        "after": "binary+x",
        "added": 9_999_999_999,
        "removed": 9_999_999_999,
        "hunks": 9_999_999_999,
        "excerpt": "\x01" * 19 + "abcd",
        "excerpt_cut": True,
    }
    paths = tuple("d/" + "a" * 250 + f"/f{k:02d}" for k in range(32))
    row = FactRow(
        9_999_999_999,
        "a" * 256,
        9_999_999_999,
        "rejected",
        "focus_below_margin",
        None,
        False,
        "present",
        0.0,
        0.0,
        "present",
        paths,
        tuple(PathChange("sha256:" + "f" * 64, worst) for _ in paths),
    )
    record = attempt_changes_record(AttemptFacts(row, "edited", 3, None, True))
    assert all(serialized_chars(path) == 258 for path in paths)
    assert all(serialized_chars(path) <= 660 for path in record["paths"])
    assert serialized_chars(record) <= 21_400
    assert serialized_chars(record) <= ATTEMPT_CHANGES_MAX_CHARS
    assert record["truncated"] is True


def test_reads_record_the_history_id_and_helpers_install_launchers(tmp_path: Path) -> None:
    reader = _reader(_world(tmp_path / "w"))
    view = FrozenReflectionView(reader.handlers())
    session = view.open_operation("reflect-test", {"list_history", "read_history"})
    try:
        session.dispatch("read_history", {"history_id": "diagnosis:3"})
        (entry,) = session.summary.to_payload()["entries"]
        assert entry["operation"] == "read_history" and entry["history_id"] == "diagnosis:3"
        helpers = install_helpers(tmp_path / "control", session)
        assert {"list_history", "read_history"} <= set(helpers.descriptors)
        argv = list(helpers.descriptors["read_history"]) + [
            json.dumps({"history_id": "diagnosis:3"})
        ]
        output = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30)
        assert (
            json.loads(output.stdout)["content"]
            == reader.read_history({"history_id": "diagnosis:3"})["content"]
        )
    finally:
        session.close()


def test_a_grouping_reader_gets_no_history_launcher(tmp_path: Path) -> None:
    reader = _reader(_world(tmp_path / "w"))
    handlers = {**reader.handlers(), "list_grouping_inputs": lambda _a: {"entries": []}}
    session = FrozenReflectionView(handlers).open_operation("group-test", {"list_grouping_inputs"})
    try:
        assert set(install_helpers(tmp_path / "control", session).descriptors) == {
            "list_grouping_inputs"
        }
    finally:
        session.close()


def test_catalog_pages_and_bodies_are_lazy_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(_world(tmp_path, rounds=9))
    calls = []
    original = reader._body

    def body(history_id: str) -> str:
        calls.append(history_id)
        return original(history_id)

    monkeypatch.setattr(reader, "_body", body)
    first = reader.list_history({})
    second = reader.list_history({"offset": first["next_offset"]})
    assert len(first["entries"]) == 32 and len(second["entries"]) == 13
    assert first["total"] == second["total"] == 45
    assert second["next_offset"] is None
    assert reader.list_history({"offset": 45})["entries"] == []
    assert calls == []
    assert reader.read_history({"history_id": "diagnosis:9"}) == reader.read_history(
        {"history_id": "diagnosis:9"}
    )
    assert calls == ["diagnosis:9"]
    with pytest.raises(ReaderArgumentError, match="invalid_offset"):
        reader.list_history({"offset": True})


def test_extra_handlers_are_reflection_scoped_and_share_the_bounded_cache(tmp_path: Path) -> None:
    from tests.test_tree_adaptive_reflector import adaptive_fixture

    trace, inputs, _brief = adaptive_fixture(tmp_path / "baseline")
    try:
        reader = _reader(_world(tmp_path / "history"), cache=inputs.body_cache)
        extra = reader.handlers()
        with inputs.reader_view(extra_handlers=extra).open_operation(
            "reflect-extra", set(extra)
        ) as session:
            assert session.dispatch(
                "read_history", {"history_id": "diagnosis:3"}
            ) == reader.read_history({"history_id": "diagnosis:3"})
            assert inputs.body_cache.size > 0
        assert inputs.body_cache.size == 0
        with pytest.raises(ValueError, match="operation_methods"):
            inputs.reader_view().open_operation("baseline-only", set(extra))
        with pytest.raises(ValueError, match="reader_handlers"):
            inputs.reader_view(extra_handlers={"list_evidence": lambda _a: {}})
    finally:
        trace.close()


@pytest.mark.parametrize("method", ["list_history", "read_history"])
@pytest.mark.parametrize("offset", [-1, "bad", 10**9])
def test_history_invalid_offset_detail_names_range(tmp_path, method, offset):
    reader = _reader(_world(tmp_path, rounds=1))
    args = {} if method == "list_history" else {"history_id": "attempt:r01-a01"}
    page = getattr(reader, method)(args)
    total = page.get("total", page.get("total_characters"))
    with pytest.raises(ReaderArgumentError) as caught:
        getattr(reader, method)({**args, "offset": offset})
    assert str(caught.value) == "invalid_offset"
    assert caught.value.detail == f"offset must be between 0 and {total}"
