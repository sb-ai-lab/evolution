"""Synthetic, populated real-tree acceptance; no provider output."""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.editor.editor_evidence import project_history
from evolution.tree.roles.editor_contracts import BRIEF_LIMITS_V2, history_payload
from evolution.tree.roles.reflection_reader import ByteLruCache
from evolution.tree.roles.reflector.reflect_aggregate import serialized_chars
from evolution.tree.roles.reflector.reflect_brief import with_history
from evolution.tree.roles.reflector.reflect_budget import RequestBudget, bound_reflection_history
from evolution.tree.roles.reflector.reflect_change import excerpt_screen
from evolution.tree.roles.reflector.reflect_history import (
    HistoryReader,
    build_history_source,
    compact_budget_reason_sentences,
    initial_layout,
    render_history,
)
from evolution.tree.roles.reflector_contracts import HISTORY_MAX_CHARS, REFLECT_LIMITS_V1
from tests.campaign_fakes import corpus
from tests.reflect_history_fakes import World
from tests.tree.roles.reflector.test_reflect_budget import _brief, _Chat
from tests.tree.roles.reflector.test_reflect_change import GOLDEN

pytestmark = pytest.mark.slow


def sources(world: World) -> dict[str, Any]:
    return {
        **world.sources(),
        "corpus": [
            replace(c, case_id=k)
            for c, k in zip(corpus(len(world.cases)), world.cases, strict=True)
        ],
    }


def source(world: World, **kwargs: Any):
    return build_history_source(
        world.snapshot(),
        **{**sources(world), "visible": None, "screen": lambda _l: False, **kwargs},
    )


def bounded(world: World, **kwargs: Any):
    built = source(world, **kwargs)
    return bound_reflection_history(
        built,
        budget=None,
        measure=lambda h: _Chat(RequestBudget(800_000, 64_000)).measure_first_request(
            _brief(dict(h))
        ),
    )


def read(reader: HistoryReader, history_id: str) -> Any:
    page = reader.read_history({"history_id": history_id})
    content = page["content"]
    while page["next_offset"] is not None:
        page = reader.read_history({"history_id": history_id, "offset": page["next_offset"]})
        content += page["content"]
    return json.loads(content)


def long_world(root: Path) -> World:
    world = World(root)
    parent = "v0"
    for n in range(1, 37):
        path = (
            "skills/a/SKILL.md"
            if n == 3
            else "skills/b/rules.md"
            if n in (9, 14, 21)
            else "other.md"
        )
        attempt = world.attempt(
            n,
            1,
            {path: "new rule\n" if n in (9, 14, 21) else f"new {n}\n"},
            parent=parent,
            score=0.25 if n in (9, 14, 21) else 0.75 if n == 3 else 0.5,
        )
        fillers = [
            world.attempt(n, index, {"other.md": f"new {n}\n"}, parent=parent, score=0.5)
            for index in range(2, 6)
        ]
        world.add_round(
            n,
            [attempt, *fillers],
            outcome="promoted" if n == 3 else "stalled",
            selected=attempt.attempt_id if n == 3 else None,
            exam=world.cases,
            diagnosis=[
                {
                    "root_cause": "r",
                    "suggested_fix": "f",
                    "target_paths": [path],
                    "affected_cases": world.cases,
                    "evidence_ids": [],
                }
            ],
        )
        if n == 3:
            world.promote(attempt, "v3")
            parent = "v3"
    return world


def test_the_scaling_fixture_stays_within_the_cap_and_the_fold_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.editor import editor_evidence

    graphs = []
    original = editor_evidence.result_cases

    def counted(rows, cases, policy):
        graphs.append((len(cases), len(rows)))
        return original(rows, cases, policy)

    monkeypatch.setattr(editor_evidence, "result_cases", counted)
    projection_seconds = []
    world = World(tmp_path, cases=300, repeats=3)
    budget = RequestBudget(800_000, 64_000)
    chat = _Chat(budget)
    sizes = []
    requests = []
    for n in range(1, 9):
        attempts = [
            world.attempt(n, 1, {"skills/a/SKILL.md": "rejected\n"}, score=0.25),
            world.attempt(
                n,
                2,
                {"skills/a/SKILL.md": "finalist\n"},
                outcome="finalist",
                reason="observe_finalist",
                score=0.75,
            ),
            world.attempt(
                n,
                3,
                None,
                outcome="abandoned",
                reason="editor_failed",
                editor="failed",
                editor_reason="changed_file_budget",
                score=None,
            ),
            world.attempt(
                n,
                4,
                None,
                outcome="abandoned",
                reason="seal_refused",
                code="oracle_leak",
                score=None,
            ),
            world.attempt(n, 5, {"skills/a/SKILL.md": "unmeasured\n"}, score=None),
        ]
        world.add_round(
            n,
            attempts,
            exam=world.cases[:150],
            diagnosis=[
                {
                    "root_cause": "r" * 256,
                    "suggested_fix": "f" * 256,
                    "target_paths": ["skills/a/SKILL.md"],
                    "affected_cases": world.cases,
                    "evidence_ids": [],
                }
                for _ in range(8)
            ],
        )
        started = time.monotonic()
        built = source(world)
        projection_seconds.append(time.monotonic() - started)
        bound = bound_reflection_history(
            built,
            budget=budget,
            measure=lambda h: chat.measure_first_request(with_history(_brief({}), h)),
        )
        sizes.append(serialized_chars(bound.history))
        requests.append(bound.request.total_chars)
        assert sizes[-1] <= HISTORY_MAX_CHARS and requests[-1] <= budget.fold_target
    assert sizes == sorted(sizes)
    assert len(graphs) == 72 and set(graphs) == {(300, 900)}
    print(
        f"synthetic retained U2-R2-001 scale result_cases_graphs={len(graphs)} case_objects={72 * 300} repeat_objects={72 * 900} source_seconds={projection_seconds}"
    )
    old = history_payload(
        project_history(
            world.snapshot(),
            **sources(world),
            limits={**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1},
            excerpts=True,
            visible=None,
        ),
        excerpts=True,
    )
    measured = [
        a for r in old["rounds"] for a in r["attempts"] if a["result"]["status"] == "present"
    ]
    assert len(measured) == 12 and all(len(a["result"]["cases"]) == 300 for a in measured)
    assert all(
        r["primary_score"] == (0.25 if a["index"] == 1 else 0.75)
        for a in measured
        for c in a["result"]["cases"]
        for r in c["repeats"]
    )
    assert all(
        len(c["repeats"]) == 3 and all(r["terminal"] == "completed" for r in c["repeats"])
        for a in measured
        for c in a["result"]["cases"]
    )
    old_size = serialized_chars(old)
    assert old_size > 1_000_000
    print(f"synthetic scale history={sizes}; first_requests={requests}; legacy={old_size}")


def test_the_worst_case_detail_round_fits_twice_under_the_cap(tmp_path: Path) -> None:
    world = World(tmp_path)
    paths = ["d/" + "a" * 140 + "/" + "b" * 150 + f"{k:07d}" for k in range(16)]
    assert all(len(p) == 300 for p in paths)
    attempts = [world.attempt(1, k, {p: "new\n" for p in paths[:8]}) for k in range(1, 9)]
    world.add_round(
        1,
        attempts,
        diagnosis=[
            {
                "root_cause": "r" * 256,
                "suggested_fix": "f" * 256,
                "target_paths": paths,
                "affected_cases": world.cases,
                "evidence_ids": [],
            }
            for _ in range(8)
        ],
    )
    size = serialized_chars(source(world).rounds[0].detail)
    assert size <= 48_000 and size * 2 <= HISTORY_MAX_CHARS
    print(f"synthetic worst detail={size}")


def test_the_long_campaign_content_contract(tmp_path: Path) -> None:
    world = long_world(tmp_path)
    built = source(world)
    history = bounded(world).history
    aggregate = history["aggregate"]
    assert [
        history[k]["count"] for k in ("aggregated_rounds", "summary_rounds", "detail_rounds")
    ] == [18, 12, 6]
    summary = next(r for r in history["rounds"] if r["round"] == 21)
    assert "attempts" not in summary and "hypotheses" not in summary
    promotion = aggregate["promotions"][0]
    assert (
        promotion["round"] == 3
        and promotion["changed_paths"][0]["path"] == "skills/a/SKILL.md"
        and "attempt:r03-a01" in promotion["details"]
    )
    path = aggregate["paths"][0]
    assert (path["path"], path["pattern"], path["regressed_rounds"], path["recent_rounds"]) == (
        "skills/b/rules.md",
        "repeated_regression",
        3,
        [21, 14, 9],
    )
    assert path["regressed_witness"] == {"round": 21, "attempt": "attempt:r21-a01"}
    (change,) = path["changes"]
    assert change["pattern"] == "repeated_regression" and [
        (w["round"], w["mark"]) for w in change["witnesses"]
    ] == [(21, "regressed"), (14, "regressed")]
    assert aggregate["attempts"]["opened"] == aggregate["attempts"]["measured"] == 180
    assert aggregate["attempts"]["promoted"] == len(aggregate["promotions"]) == 1
    for closed in built.rounds:
        assert len(closed.attempts) == len(closed.state.attempts) == 5
        assert [facts.row.index for facts in closed.attempts] == [1, 2, 3, 4, 5]
        for facts in closed.attempts:
            assert facts.scores["exam"]["cases"] == len(world.cases)
            expected = 0.0
            if facts.row.index == 1:
                if closed.row.round == 3:
                    expected = 0.25
                elif closed.row.round in (9, 14, 21):
                    expected = -0.25
            assert facts.scores["exam"]["mean_delta"] == expected
            if facts.row.index > 1:
                assert "skills/b/rules.md" not in facts.row.paths
                assert facts.row.promoted is False
            assert closed.state.attempts[facts.row.attempt_id].parent_version == (
                "v3" if closed.row.round > 3 else "v0"
            )
    a = next(p for p in aggregate["paths"] if p["path"] == "skills/a/SKILL.md")
    assert a["promoted_witness"] == {"round": 3, "attempt": "attempt:r03-a01"}
    reader = HistoryReader(built, reflection_id="r37-x01", cache=ByteLruCache())
    visible = {p for r in built.rounds for f in r.attempts for p in f.row.paths}
    codes = {
        "reflect_history_aggregate/1",
        "full",
        "reflect_change_fingerprint/1",
        "present",
        "absent",
        "unavailable",
        "text",
        "lines",
        "stalled",
        "promoted",
        "rejected",
        "focus_below_margin",
        "repeated_regression",
        "repeated_no_promotion",
        "single",
        "regressed",
        "other",
    }

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            if value.startswith(("attempt:", "diagnosis:", "changes:")):
                body = read(reader, value)
                assert body is not None
            elif key == "attempt_id":
                assert read(reader, "attempt:" + value)["attempt_id"] == value
            elif key == "excerpt":
                assert value in {"new rule", "new 3", *[f"new {n}" for n in range(1, 37)]}
                assert value == value.strip()
            else:
                assert (
                    value in visible
                    or value in codes
                    or re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value)
                ), value

    walk(aggregate)
    for r in history["rounds"]:
        for history_id in r["details"]:
            assert read(reader, history_id)
        for entry in r.get("attempts", []):
            assert read(reader, entry["details"])


def test_identical_and_distinct_edits_to_one_path(tmp_path: Path) -> None:
    def scenario(name: str, variant: str):
        world = World(tmp_path / name)
        world.publish("champion", "base", {"p.md": "a\nold\nz\n"})
        parent = "base"
        for n in range(1, 11):
            files = {}
            promoted = False
            score = 0.5
            base = (world.refs[("champion", parent)].root / "p.md").read_text()
            if n in (2, 6) and variant == "same":
                files = {"p.md": "top1\ntop2\ntop3\n" + base}
                promoted = True
            if n in (4, 8, 10) and not (variant == "neutral" and n == 10):
                new = (
                    "new\nadded"
                    if variant in ("same", "mixed") and n != 10 or variant == "same"
                    else f"different{n}\nadded"
                )
                changed = base.replace("old", new)
                if n == 8:
                    changed = changed.replace("\n", "\r\n")
                files = {"p.md": changed}
                score = 0.25
                if variant == "mixed" and n == 10:
                    promoted = True
                    score = 0.75
            if variant == "neutral" and n in (1, 3, 5, 7, 9):
                files = {"p.md": base.replace("old", f"neutral{n}")}
            attempt = world.attempt(n, 1, files, parent=parent, score=score)
            world.add_round(
                n,
                [attempt],
                outcome="promoted" if promoted else "stalled",
                selected=attempt.attempt_id if promoted else None,
                exam=world.cases,
            )
            if promoted:
                world.promote(attempt, f"v{n}")
                parent = f"v{n}"
        built = source(world)
        path = next(p for p in built.aggregate.full["paths"] if p["path"] == "p.md")
        reader = HistoryReader(built, reflection_id="r11-x01", cache=ByteLruCache())
        for change in path["changes"]:
            for witness in change["witnesses"]:
                body = read(reader, witness["attempt"].replace("attempt:", "changes:"))
                assert any(
                    p["path"] == "p.md"
                    and p["fingerprint"] == change["fingerprint"]
                    and p["change"] == change["change"]
                    for p in body["paths"]
                )
        return path

    same = scenario("same", "same")
    e = next(c for c in same["changes"] if c["pattern"] == "repeated_regression")
    expected = {
        "scheme": "reflect_change_fingerprint/1",
        "basis": "lines",
        "before": "text",
        "after": "text",
        "hunks": [[["old\n"], ["new\n", "added\n"]]],
        "bytes": None,
    }
    assert (
        e["fingerprint"]
        == GOLDEN["edit_e"]
        == "sha256:" + hashlib.sha256(canonical_json(expected)).hexdigest()
    )
    assert (e["change"]["added"], e["change"]["removed"], e["change"]["hunks"]) == (2, 1, 1)
    assert len([c for c in same["changes"] if c["fingerprint"] == e["fingerprint"]]) == 1
    assert e["rounds"] == e["regressed_rounds"] == 3
    assert [(w["round"], w["mark"]) for w in e["witnesses"]] == [
        (10, "regressed"),
        (8, "regressed"),
    ]
    assert e["change"]["excerpt"] == "new"
    distinct = scenario("distinct", "distinct")
    assert len([c for c in distinct["changes"] if c["pattern"] == "single"]) == 3
    assert distinct["pattern"] == "repeated_regression_distinct"
    neutral = scenario("neutral", "neutral")
    assert neutral["pattern"] != "repeated_regression_distinct"
    mixed = scenario("mixed", "mixed")
    assert (
        mixed["pattern"] == "repeated_regression"
        and mixed["changes"][0]["fingerprint"] == GOLDEN["edit_e"]
    )
    assert len(mixed["changes"]) == 2
    assert mixed["changes"][1]["witnesses"] == [
        {"round": 10, "attempt": "attempt:r10-a01", "mark": "promoted"}
    ]


def test_hidden_sides_and_unavailable_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.tree.roles.reflector import reflect_change

    world = World(tmp_path)
    for k, content in enumerate(("HIDDEN-CANARY", b"\xffBINARY-HIDDEN"), 1):
        world.publish(
            "champion",
            f"hidden{k}",
            {"private/node": content},
            modes={"private/node": 0o755 if k == 1 else 0o644},
        )
        attempt = world.attempt(
            k,
            1,
            {"private/node/visible.md": "visible\n"},
            parent=f"hidden{k}",
            remove=("private/node",),
        )
        world.add_round(k, [attempt], exam=world.cases)

    def visible(p):
        return p != "private/node"  # old file hidden, retained descendant visible

    assert not visible("private/node") and visible("private/node/visible.md")
    built = source(world, visible=visible)
    # Ancestor directory is retained because it contains a visible descendant;
    # its old hidden file contributes only its kind.
    path = next(p for p in built.aggregate.full["paths"] if p["path"] == "private/node")
    (change,) = path["changes"]
    assert change["change"]["before"] == "file"
    assert change["change"]["after"] == "directory"
    assert change["rounds"] == 2
    from evolution.tree.roles.reflector.reflect_change import Side, change_record

    record = change_record(Side("file"), Side("directory"))
    assert record["bytes"]["before"] is None
    text = canonical_json(bounded(world, visible=visible).history).decode()
    assert "HIDDEN-CANARY" not in text and "BINARY-HIDDEN" not in text and "binary+x" not in text
    assert all(
        hashlib.sha256(data).hexdigest() not in text
        for data in (b"HIDDEN-CANARY", b"\xffBINARY-HIDDEN")
    )
    shutil.rmtree(world.refs[("candidate", "r01-a01")].root)
    unavailable = source(world, visible=visible)
    assert unavailable.rounds[0].attempts[0].row.changes_status == "unavailable"
    assert unavailable.aggregate.full["attempts"]["changes_unavailable"] == 1
    original = reflect_change.read_side

    def failed(*args, **kwargs):
        if args[1] == "private/node/visible.md":
            raise OSError("synthetic vanished leaf")
        return original(*args, **kwargs)

    monkeypatch.setattr(reflect_change, "read_side", failed)
    partial = source(world, visible=visible)
    leaf = next(
        p for p in partial.aggregate.full["paths"] if p["path"] == "private/node/visible.md"
    )
    assert leaf["changes_unfingerprinted"] == 1 and leaf["changes"] == []


def test_the_work_bound_end_to_end(tmp_path: Path) -> None:
    start = time.monotonic()
    world = World(tmp_path)
    big = "same\n" * 4000
    world.publish("champion", "large", {"a.md": big, "z.md": "old\n"})
    for n in (1, 2):
        files = {"z.md": "new\n"}
        if n == 1:
            files["a.md"] = big.replace("same", "other", 1)
        world.add_round(
            n, [world.attempt(n, 1, files, parent="large", score=0.25)], exam=world.cases
        )
    built = source(world)
    a = next(p for p in built.aggregate.full["paths"] if p["path"] == "a.md")
    z = next(p for p in built.aggregate.full["paths"] if p["path"] == "z.md")
    assert a["changes"][0]["change"]["basis"] == "too_large"
    assert (
        z["pattern"] == "repeated_regression"
        and len(z["changes"]) == 1
        and z["changes"][0]["change"]["basis"] == "lines"
    )
    world.publish("champion", "many", {f"p{k:02d}.md": "same\n" * 1000 for k in range(32)})
    attempt = world.attempt(
        3, 1, {f"p{k:02d}.md": "other\n" + "same\n" * 999 for k in range(32)}, parent="many"
    )
    world.add_round(3, [attempt], exam=world.cases)
    facts = source(world).rounds[-1].attempts[0].row
    assert len(facts.changes) == 32 and all(c.change["basis"] == "lines" for c in facts.changes)
    assert time.monotonic() - start < 30


def test_determinism_memo_and_growth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evolution.tree.roles.editor import editor_evidence

    world = World(tmp_path)
    memo = {}
    fingerprints = None
    graphs = []
    original = editor_evidence.result_cases

    def counted(rows, cases, policy):
        graphs.append((len(cases), len(rows)))
        return original(rows, cases, policy)

    monkeypatch.setattr(editor_evidence, "result_cases", counted)
    for n in range(1, 41):
        world.add_round(
            n, [world.attempt(n, 1, {"p.md": "same edit\n"}, score=0.25)], exam=world.cases
        )
        if n == 5:
            fingerprints = source(world).rounds[0].attempts[0].row.changes
    first = source(world, memo=memo)
    second = source(world, memo=memo)
    third = source(world)
    assert (
        canonical_json(render_history(first, initial_layout(40)))
        == canonical_json(render_history(second, initial_layout(40)))
        == canonical_json(render_history(third, initial_layout(40)))
    )
    assert first.rounds[0].attempts[0].row.changes == fingerprints
    reader = HistoryReader(first, reflection_id="r41-x01", cache=ByteLruCache())
    before = len(graphs)
    reader.list_history({})
    assert len(graphs) == before
    read(reader, "attempt:r01-a01")
    assert len(graphs) == before + 1
    print(
        f"synthetic retained U2-R2-001 result_cases graphs before read={before}, per graph={graphs[0]}, memo_hit_additional=0"
    )


def _normalize(value: Any) -> str:
    # Normalize numeric JSON values AND decimal runs embedded in round/Attempt
    # references, keeping every structural field and every fingerprint byte.
    def walk(v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            return {k: walk(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, key) for x in v]
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (float, int)):
            return 0
        if key in {
            "attempt_id",
            "selected_attempt",
            "last_attempt",
            "attempt",
            "details",
        } or v.startswith(("attempt:", "diagnosis:", "changes:")):
            return re.sub(r"\d+", "0", v)
        return v

    return canonical_json(walk(value)).decode()


def test_a_steady_campaign_plateaus(tmp_path: Path) -> None:
    world = World(tmp_path)
    memo = {}
    histories = []
    for n in range(1, 41):
        world.add_round(
            n,
            [
                world.attempt(
                    n,
                    k,
                    {f"p{p:02d}.md": "new\n" for p in range(20)},
                    score=0.25 if k == 1 else 0.5,
                )
                for k in range(1, 6)
            ],
            exam=world.cases,
        )
        if n >= 20:
            histories.append(_normalize(bounded(world, memo=memo).history))
    assert all(h == histories[0] for h in histories)


def test_an_upper_bound_over_a_varied_campaign(tmp_path: Path) -> None:
    rng = random.Random(7)
    world = World(tmp_path)
    memo = {}
    parent = "v0"
    escaped = "d/" + "\x01" * 250 + "/f.md"
    for n in range(1, 61):
        attempts = []
        for k in range(1, 9):
            outcome, reason = rng.choice(
                [
                    ("rejected", "focus_below_margin"),
                    ("finalist", "observe_finalist"),
                    ("abandoned", "editor_failed"),
                ]
            )
            attempts.append(
                world.attempt(
                    n,
                    k,
                    {
                        escaped: f"edit{rng.randrange(15)}\n",
                        **{f"p{p:02d}.md": f"e{rng.randrange(8)}\n" for p in range(8)},
                    },
                    parent=parent,
                    outcome=outcome,
                    reason=reason,
                    score=rng.choice([0.25, 0.5, 0.75]),
                )
            )
        world.add_round(
            n,
            attempts,
            outcome="promoted" if n in (45, 57) else "stalled",
            selected=attempts[-1].attempt_id if n in (45, 57) else None,
            exam=world.cases,
            diagnosis=[
                {
                    "root_cause": "r" * 256,
                    "suggested_fix": "f" * 256,
                    "target_paths": [escaped] * 16,
                    "affected_cases": world.cases,
                    "evidence_ids": [],
                }
                for _ in range(8)
            ],
        )
        if n in (45, 57):
            world.promote(attempts[-1], f"v{n}")
            parent = f"v{n}"
        bound = bounded(world, memo=memo)
        assert serialized_chars(bound.history) <= HISTORY_MAX_CHARS
        assert (
            serialized_chars(bound.history["aggregate"])
            <= {"full": 50_000, "reduced": 22_000, "core": 1_550}[bound.layout.form]
        )


def test_b3_codes_and_leaks_in_aggregated_rounds(tmp_path: Path) -> None:
    world = World(tmp_path)
    needle = "SYNTHETIC-ORACLE-NEEDLE"
    secret = "SYNTHETIC-RUN-SECRET"
    for n in range(1, 21):
        attempts = (
            [
                world.attempt(
                    n,
                    1,
                    None,
                    outcome="abandoned",
                    reason="editor_failed",
                    editor="failed",
                    editor_reason="changed_file_budget",
                    score=None,
                ),
                world.attempt(
                    n,
                    2,
                    None,
                    outcome="abandoned",
                    reason="seal_refused",
                    code="oracle_leak",
                    score=None,
                ),
            ]
            if n == 1
            else [world.attempt(n, 1, {"p.md": secret + "\n"} if n == 2 else {"other.md": "new\n"})]
        )
        world.add_round(
            n, attempts, exam=world.cases, reflection_error="oracle_leak" if n == 1 else None
        )
    from evolution.tree.roles.reflector_contracts import oracle_needles

    cases = [replace(c, oracle={"answer": needle}) for c in sources(world)["corpus"]]
    needles = oracle_needles(cases)
    assert needle in needles
    screened = source(world, corpus=cases, screen=excerpt_screen(needles, (secret,)))
    unscreened = source(world, corpus=cases, screen=excerpt_screen(needles, ()))
    history = bounded(world, corpus=cases, screen=excerpt_screen(needles, (secret,))).history
    assert any(r["reason"] == "changed_file_budget" for r in history["aggregate"]["reasons"])
    assert compact_budget_reason_sentences(history)
    assert any(r["code"] == "oracle_leak" for r in history["aggregate"]["reasons"])
    assert history["aggregate"]["diagnoses"]["unavailable"] == 1
    assert all("r01-a02" not in canonical_json(p).decode() for p in history["aggregate"]["paths"])
    change = screened.rounds[1].attempts[0].row.changes[0]
    other = unscreened.rounds[1].attempts[0].row.changes[0]
    assert change.change["excerpt"] is None and change.fingerprint == other.fingerprint
    assert (
        needle not in canonical_json(history).decode()
        and secret not in canonical_json(history).decode()
    )
