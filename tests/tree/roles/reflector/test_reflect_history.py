"""The family-14 Reflector's compact history."""

from __future__ import annotations

import re
from pathlib import Path

from evolution.tree.campaign_schema import effective_attempt_feedback
from evolution.tree.execution.journal_identity import canonical_json
from evolution.tree.roles.reflector.reflect_aggregate import RoundRow, aggregate_forms
from evolution.tree.roles.reflector.reflect_history import (
    ClosedRound,
    HistorySource,
    bound_history,
    build_history_source,
    compact_budget_reason_sentences,
    elision_steps,
    history_sentences,
    initial_layout,
    render_history,
)
from evolution.tree.roles.reflector_contracts import HISTORY_MAX_CHARS, SUMMARY_WINDOW_ROUNDS
from tests.reflect_history_fakes import World


def _source(world: World, **kwargs):  # type: ignore[no-untyped-def]
    return build_history_source(
        world.snapshot(), visible=None, screen=lambda _line: False, **world.sources(), **kwargs
    )


def test_every_attempt_kind_has_a_compact_entry(tmp_path: Path) -> None:
    world = World(tmp_path)
    measured = world.attempt(1, 1, {"skills/a/SKILL.md": "edited\n"}, score=0.25)
    finalist = world.attempt(
        1,
        2,
        {"skills/a/SKILL.md": "other\n"},
        outcome="finalist",
        reason="lower_bound_positive",
        score=0.75,
    )
    budget = world.attempt(
        1,
        3,
        None,
        outcome="abandoned",
        reason="editor_failed",
        editor="failed",
        editor_reason="changed_file_budget",
    )
    leak = world.attempt(1, 4, None, outcome="abandoned", reason="seal_refused", code="oracle_leak")
    declined = world.attempt(
        1, 5, None, outcome="abandoned", reason="editor_declined", editor="declined"
    )
    unmeasured = world.attempt(
        1,
        6,
        {"skills/b/rules.md": "x\n"},
        outcome="abandoned",
        reason="measurement_failed",
        score=None,
    )
    unreadable = world.attempt(1, 7, {"skills/b/rules.md": "y\n"}, score=0.5)
    world.rows[str(unreadable.measurement_id)] = type(world.rows[str(unreadable.measurement_id)])(
        ValueError("unreadable")
    )
    world.add_round(
        1,
        [measured, finalist, budget, leak, declined, unmeasured, unreadable],
        exam=world.cases[:2],
        global_comparisons={
            finalist.attempt_id: {"accepted": False, "reason": "global_bound_not_positive"}
        },
        diagnosis=[
            {
                "root_cause": "r" * 300,
                "suggested_fix": "f",
                "target_paths": [f"skills/{k}.md" for k in range(10)],
                "affected_cases": ["case-001"],
                "evidence_ids": ["obs:1", "obs:2"],
            }
        ],
    )
    source = _source(world)
    (round_,) = source.rounds
    detail = round_.detail
    by_id = {entry["attempt_id"]: entry for entry in detail["attempts"]}
    state = world.rounds[1]
    for attempt in state.attempts.values():
        entry = by_id[attempt.attempt_id]
        assert entry["reason"] == effective_attempt_feedback(state, attempt, family_version=14)[0]
        assert entry["details"] == f"attempt:{attempt.attempt_id}"
        assert (entry["scores"] is None) is (entry["measurement"] != "present")
    assert by_id["r01-a02"]["reason"] == "global_bound_not_positive"
    assert by_id["r01-a03"]["reason"] == "changed_file_budget"
    assert by_id["r01-a04"]["code"] == "oracle_leak" and by_id["r01-a04"]["changed_paths"] == []
    assert by_id["r01-a06"]["measurement"] == "absent"
    assert by_id["r01-a07"]["measurement"] == "unavailable"
    scores = by_id["r01-a01"]["scores"]
    assert scores["exam"]["cases"] == 2 and scores["rest"]["cases"] == 2
    assert scores["exam"]["mean_delta"] == -0.25 and scores["exam"]["worsened"] == 2
    (hypothesis,) = detail["hypotheses"]
    assert (
        hypothesis["index"] == 1
        and len(hypothesis["root_cause"]) == 256
        and hypothesis["root_cause_truncated"] is True
    )
    assert len(hypothesis["target_paths"]) == 8 and hypothesis["target_paths_truncated"] is True
    assert (hypothesis["affected_cases"], hypothesis["evidence_ids"]) == (1, 2)
    assert detail["diagnosis"] == {"status": "present", "error_class": None, "hypotheses": 1}
    assert detail["details"] == ["diagnosis:1"]
    assert set(round_.summary) == {
        "round",
        "outcome",
        "reason",
        "selected_attempt",
        "promoted",
        "attempts_opened",
        "attempts_measured",
        "diagnosis",
        "details",
    }
    assert detail["attempts_truncated"] is False


def test_a_failed_reflection_round_shows_no_text(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.add_round(
        1, [], outcome="unplanned", reason="reflection_failed", reflection_error="oracle_leak"
    )
    (round_,) = _source(world).rounds
    assert round_.summary["diagnosis"] == {
        "status": "unavailable",
        "error_class": "oracle_leak",
        "hypotheses": 0,
    }
    assert round_.detail["hypotheses"] == []


def test_visibility_filters_every_path(tmp_path: Path) -> None:
    world = World(tmp_path)
    attempt = world.attempt(1, 1, {"skills/a/SKILL.md": "edited\n", "private/hidden.md": "h\n"})
    world.add_round(1, [attempt])
    source = build_history_source(
        world.snapshot(),
        visible=lambda path: path.startswith("skills/"),
        screen=lambda _l: False,
        **world.sources(),
    )
    (round_,) = source.rounds
    assert round_.detail["attempts"][0]["changed_paths"] == ["skills/a/SKILL.md"]
    assert "private/hidden.md" not in repr(source.aggregate.full)


def test_attempts_beyond_the_round_cap_are_facts_but_not_entries(tmp_path: Path) -> None:
    world = World(tmp_path)
    attempts = [world.attempt(1, k, {"skills/a/SKILL.md": f"v{k}\n"}) for k in range(1, 11)]
    world.add_round(1, attempts)
    (round_,) = _source(world).rounds
    assert len(round_.detail["attempts"]) == 8 and round_.detail["attempts_truncated"] is True
    assert len(round_.attempts) == 10


def synthetic(n: int, *, first: int = 1, detail_chars: int = 2_000) -> HistorySource:
    """`n` closed rounds of fixed-size compact entries (no files, no campaign)."""

    rounds = []
    outcomes = [
        (o, r)
        for o in (
            "condition_changed",
            "unmeasurable",
            "unplanned",
            "stalled",
            "promoted",
            "unscored",
        )
        for r in ("reflection_declined", "reflection_failed")
    ]
    for k in range(n):
        number = first + k
        outcome, reason = outcomes[k % len(outcomes)]
        row = RoundRow(number, outcome, reason, None, "present", True)
        summary = {"round": number, "outcome": outcome, "reason": reason, "pad": "s" * 200}
        detail = {**summary, "pad": "d" * detail_chars}
        rounds.append(ClosedRound(row, summary, detail))
    return HistorySource(
        rounds, predecessor_rounds=0, aggregate=aggregate_forms([r.row for r in rounds], [])
    )


def _size(value: object) -> int:
    return len(canonical_json(value).decode("utf-8"))


def test_the_window_places_six_details_twelve_summaries_and_aggregates_the_rest() -> None:
    history = render_history(synthetic(36), initial_layout(36))
    assert (history["detail_rounds"], history["summary_rounds"], history["aggregated_rounds"]) == (
        {"count": 6, "first": 31, "last": 36},
        {"count": 12, "first": 19, "last": 30},
        {"count": 18, "first": 1, "last": 18},
    )
    assert history["elided"] == {
        "reason": "window",
        "window_to_aggregate": 18,
        "detail_to_summary": 0,
        "summary_to_aggregate": 0,
        "aggregate_form": "full",
    }
    assert [r["round"] for r in history["rounds"]] == list(range(19, 37))
    assert history["aggregate"]["rounds"] == {"count": 36, "first": 1, "last": 36}


def test_the_steps_run_in_order_and_keep_the_window() -> None:
    layouts = list(elision_steps(initial_layout(30)))
    assert all(layout.summary <= SUMMARY_WINDOW_ROUNDS for layout in layouts)
    forms = [layout.form for layout in layouts]
    first_reduced = forms.index("reduced")
    assert all(layout.detail >= 1 for layout in layouts[: forms.index("core") + 1])
    assert layouts[first_reduced - 1].summary == 0 and layouts[first_reduced - 1].detail == 1
    assert (layouts[-2].detail, layouts[-2].summary) == (0, 1)
    assert (layouts[-1].detail, layouts[-1].summary, layouts[-1].aggregated, layouts[-1].form) == (
        0,
        0,
        30,
        "core",
    )
    assert (
        layouts[-1].detail_to_summary == 6
        and layouts[-1].summary_to_aggregate + layouts[-1].window_to_aggregate == 30
    )


def test_fits_stops_the_elision_at_the_first_fitting_layout() -> None:
    source = synthetic(20)
    seen: list[int] = []

    def fits(history: object) -> bool:
        seen.append(_size(history))
        return len(seen) == 4

    history, layout = bound_history(source, fits=fits)
    assert layout.budget is True and len(seen) == 4 and history["elided"]["reason"] == "budget"
    assert seen == sorted(seen, reverse=True)  # every step shrinks


def test_the_minimum_is_a_constant_independent_of_the_round_count() -> None:
    sizes = {}
    texts = {}
    # Multiples of the 12 outcome pairs, so both runs list the same pairs in the same order.
    for n in (12, 20_004):
        source = synthetic(n, first=9_999_970_000)
        history, layout = bound_history(source, fits=lambda _h: False)
        assert (layout.detail, layout.summary, layout.form) == (0, 0, "core")
        sizes[n] = _size(history)
        texts[n] = re.sub(r"\d+", "0", canonical_json(history).decode("utf-8"))
    assert sizes[12] < 2_100 and sizes[20_004] < 2_100
    assert texts[12] == texts[20_004]


def test_the_history_never_exceeds_the_cap_after_the_cap_step() -> None:
    source = synthetic(40, detail_chars=20_000)
    history, _ = bound_history(source, fits=lambda h: _size(h) <= HISTORY_MAX_CHARS)
    assert _size(history) <= HISTORY_MAX_CHARS


def test_sentences_follow_the_elision_state() -> None:
    plain = render_history(synthetic(3), initial_layout(3))
    window = render_history(synthetic(30), initial_layout(30))
    budget = render_history(synthetic(30), list(elision_steps(initial_layout(30)))[-1])
    assert len(history_sentences(plain)) == 5
    assert any("summary window" in s for s in history_sentences(window))
    tight = history_sentences(budget)
    assert any("to fit the request budget" in s for s in tight)
    assert any("reduced to counts" in s for s in tight)


def test_budget_reason_sentences_include_aggregate_only_reasons() -> None:
    history = {"rounds": [], "aggregate": {"reasons": [{"reason": "changed_file_budget"}]}}
    assert len(compact_budget_reason_sentences(history)) == 1
