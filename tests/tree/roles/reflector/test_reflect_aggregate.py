"""The bounded whole-run aggregate."""

from __future__ import annotations

import json
import random
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evolution.tree.roles.reflector.reflect_aggregate import (
    FactRow,
    RoundRow,
    aggregate_forms,
    build_aggregate,
    serialized_chars,
)
from evolution.tree.roles.reflector.reflect_change import PathChange
from evolution.tree.roles.reflector_contracts import (
    AGGREGATE_FULL_LIMITS,
    AGGREGATE_MAX_CHARS,
    AGGREGATE_REDUCED_LIMITS,
    AGGREGATE_REDUCED_MAX_CHARS,
)

FP_E = "sha256:" + "e" * 64
FP_F = "sha256:" + "f" * 64


def change(fp: str | None, excerpt: str | None = "x") -> PathChange:
    if fp is None:
        return PathChange(None, None)
    return PathChange(
        fp,
        {
            "basis": "lines",
            "before": "text",
            "after": "text",
            "added": 1,
            "removed": 0,
            "hunks": 1,
            "excerpt": excerpt,
            "excerpt_cut": False,
        },
    )


def row(
    round_: int,
    index: int,
    *,
    delta: float | None = 0.0,
    promoted: bool = False,
    paths: dict[str, str | None] | None = None,
    outcome: str = "rejected",
    reason: str | None = "focus_below_margin",
    code: str | None = None,
    measurement: str = "present",
    attempt_id: str | None = None,
) -> FactRow:
    paths = paths or {}
    return FactRow(
        round=round_,
        attempt_id=attempt_id or f"r{round_:02d}-a{index:02d}",
        index=index,
        outcome="promoted" if promoted else outcome,
        reason=reason,
        code=code,
        promoted=promoted,
        measurement=measurement,
        exam_mean_delta=delta if measurement == "present" else None,
        rest_mean_delta=0.0 if measurement == "present" else None,
        changes_status="present" if paths else "absent",
        paths=tuple(paths),
        changes=tuple(change(fp) for fp in paths.values()),
    )


def rounds_for(rows: list[FactRow], *, promoted: set[int] = frozenset()) -> list[RoundRow]:  # type: ignore[assignment]
    numbers = sorted({r.round for r in rows})
    return [
        RoundRow(n, "promoted" if n in promoted else "stalled", None, None, "present", True)
        for n in numbers
    ]


def content_contract_rows() -> list[FactRow]:
    """36 rounds, 5 Attempts each; round 3 promotes a change to
    `skills/a/SKILL.md`; rounds 9, 14, 21 make edit E to `skills/b/rules.md`
    with a negative exam delta; every other Attempt has a non-negative delta
    and leaves `skills/b/rules.md` alone."""

    rows = []
    for r in range(1, 37):
        for i in range(1, 6):
            if r == 3 and i == 1:
                rows.append(row(r, i, delta=0.08, promoted=True, paths={"skills/a/SKILL.md": FP_F}))
            elif r in (9, 14, 21) and i == 2:
                rows.append(row(r, i, delta=-0.05, paths={"skills/b/rules.md": FP_E}))
            else:
                rows.append(
                    row(
                        r,
                        i,
                        delta=0.0,
                        paths={f"skills/filler/{r}-{i}.md": f"sha256:{r:032d}{i:032d}"},
                    )
                )
    return rows


def test_the_content_contract_scenario() -> None:
    rows = content_contract_rows()
    forms = aggregate_forms(rounds_for(rows, promoted={3}), rows)
    full = forms.full
    assert full["promotions"][0]["round"] == 3
    assert full["promotions"][0]["changed_paths"] == [
        {"path": "skills/a/SKILL.md", "path_sha256": None}
    ]
    first = full["paths"][0]
    assert (first["path"], first["pattern"], first["regressed_rounds"], first["recent_rounds"]) == (
        "skills/b/rules.md",
        "repeated_regression",
        3,
        [21, 14, 9],
    )
    assert first["regressed_witness"] == {"round": 21, "attempt": "attempt:r21-a02"}
    (entry,) = first["changes"]
    assert (entry["pattern"], entry["rounds"], entry["regressed_rounds"]) == (
        "repeated_regression",
        3,
        3,
    )
    assert [w["round"] for w in entry["witnesses"]] == [21, 14]
    assert {w["mark"] for w in entry["witnesses"]} == {"regressed"}
    promoted_path = next(p for p in full["paths"] if p["path"] == "skills/a/SKILL.md")
    assert promoted_path["promoted_witness"] == {"round": 3, "attempt": "attempt:r03-a01"}


def test_recent_rounds_follow_changes_and_witnesses_prefer_regressions() -> None:
    rows = [
        row(5, 1, delta=-1.0, paths={"p.md": FP_E}),
        row(8, 1, delta=-1.0, paths={"p.md": FP_E}),
        row(30, 1, delta=-1.0, paths={"p.md": FP_E}),
        row(31, 1, delta=1.0, paths={"p.md": FP_E}),
    ]
    path = build_aggregate(rounds_for(rows), rows)["paths"][0]
    assert path["recent_rounds"] == [31, 30, 8]
    assert [w["round"] for w in path["changes"][0]["witnesses"]] == [30, 8]


@pytest.mark.parametrize(
    ("deltas", "promoted", "expected"),
    [
        ([-1, -1, 0], set(), "repeated_regression"),
        ([-1, 0, 0], set(), "repeated_no_promotion"),
        ([-1, -1, 0, 0], set(), "repeated_no_promotion"),  # 2 × 2 > 4 is false
        ([1], {0}, "promoted"),
        ([-1, 1], {1}, "promoted_and_regressed"),
        ([0], set(), "single"),
    ],
)
def test_change_pattern_precedence(deltas: list[float], promoted: set[int], expected: str) -> None:
    rows = [
        row(k + 1, 1, delta=d, promoted=k in promoted, paths={"p.md": FP_E})
        for k, d in enumerate(deltas)
    ]
    assert build_aggregate(rounds_for(rows), rows)["paths"][0]["changes"][0]["pattern"] == expected


def test_distinct_regressed_changes_label_the_path_but_no_change() -> None:
    rows = [row(r, 1, delta=-1.0, paths={"s.md": f"sha256:{r:064d}"}) for r in (4, 8, 10)]
    path = build_aggregate(rounds_for(rows), rows)["paths"][0]
    assert path["pattern"] == "repeated_regression_distinct"
    assert all(
        c["pattern"] == "single" and [w["mark"] for w in c["witnesses"]] == ["regressed"]
        for c in path["changes"]
    )
    assert path["regressed_witness"] == {"round": 10, "attempt": "attempt:r10-a01"}
    neutral = rows[:2] + [
        row(r, 1, delta=0.0, paths={"s.md": f"sha256:{r:064d}"}) for r in (11, 12, 13, 14, 15)
    ]
    assert (
        build_aggregate(rounds_for(neutral), neutral)["paths"][0]["pattern"]
        != "repeated_regression_distinct"
    )


def test_a_mixed_path_puts_the_repeated_regression_first() -> None:
    rows = [
        row(4, 1, delta=-1.0, paths={"s.md": FP_E}),
        row(8, 1, delta=-1.0, paths={"s.md": FP_E}),
        row(10, 1, delta=1.0, promoted=True, paths={"s.md": FP_F}),
    ]
    path = build_aggregate(rounds_for(rows, promoted={10}), rows)["paths"][0]
    assert path["pattern"] == "repeated_regression"
    assert [c["fingerprint"] for c in path["changes"]] == [FP_E, FP_F]
    assert [w["mark"] for w in path["changes"][1]["witnesses"]] == ["promoted"]


def test_a_promoted_row_that_regressed_is_one_witness() -> None:
    rows = [row(4, 1, delta=-0.5, promoted=True, paths={"s.md": FP_E})]
    entry = build_aggregate(rounds_for(rows, promoted={4}), rows)["paths"][0]["changes"][0]
    assert entry["pattern"] == "promoted_and_regressed"
    assert [w["mark"] for w in entry["witnesses"]] == ["promoted_and_regressed"]


def test_two_regressed_singles_sort_before_a_neutral_repeat() -> None:
    rows = [
        row(1, 1, delta=-1.0, paths={"s.md": "sha256:" + "1" * 64}),
        row(2, 1, delta=-1.0, paths={"s.md": "sha256:" + "2" * 64}),
        row(3, 1, delta=0.0, paths={"s.md": "sha256:" + "3" * 64}),
        row(4, 1, delta=0.0, paths={"s.md": "sha256:" + "3" * 64}),
    ]
    changes = build_aggregate(rounds_for(rows), rows)["paths"][0]["changes"]
    assert [c["pattern"] for c in changes] == ["single", "single", "repeated_no_promotion"]


def test_null_fingerprints_enter_tallies_only() -> None:
    rows = [
        row(1, 1, delta=-1.0, paths={"s.md": None}),
        row(2, 1, delta=-1.0, paths={"s.md": FP_E}),
    ]
    path = build_aggregate(rounds_for(rows), rows)["paths"][0]
    assert (path["rounds"], path["changes_total"], path["changes_unfingerprinted"]) == (2, 1, 1)


def test_permuting_rows_changes_no_byte() -> None:
    rows = content_contract_rows()
    rounds = rounds_for(rows, promoted={3})
    expected = json.dumps(build_aggregate(rounds, rows), sort_keys=True)
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert json.dumps(build_aggregate(rounds, shuffled), sort_keys=True) == expected


def test_caps_and_totals_and_reduced_prefixes() -> None:
    rows = [
        row(
            r,
            1,
            delta=-1.0 if r % 2 else 1.0,
            promoted=r % 7 == 0,
            reason=f"reason_{r % 13}",
            paths={f"p{r % 9}.md": f"sha256:{r % 5:064d}", f"q{r % 4}.md": f"sha256:{r % 3:064d}"},
        )
        for r in range(1, 60)
    ]
    forms = aggregate_forms(
        rounds_for(rows, promoted={r for r in range(1, 60) if r % 7 == 0}), rows
    )
    full, reduced = forms.full, forms.reduced
    for key in ("paths", "reasons", "promotions", "gains"):
        assert len(full[key]) == min(AGGREGATE_FULL_LIMITS[key], full[f"{key}_total"])
        assert len(reduced[key]) == min(AGGREGATE_REDUCED_LIMITS[key], full[f"{key}_total"])
        assert reduced[f"{key}_total"] == full[f"{key}_total"]
    # Same orders with smaller caps: every reduced list is a prefix by identity.
    assert reduced["reasons"] == full["reasons"][: len(reduced["reasons"])]
    assert reduced["gains"] == full["gains"][: len(reduced["gains"])]
    assert [(e["round"], e["attempt_id"]) for e in reduced["promotions"]] == [
        (e["round"], e["attempt_id"]) for e in full["promotions"][: len(reduced["promotions"])]
    ]
    assert all(len(e["changed_paths"]) <= 1 for e in reduced["promotions"])
    for full_path, reduced_path in zip(
        full["paths"][: len(reduced["paths"])], reduced["paths"], strict=True
    ):
        assert reduced_path["path"] == full_path["path"]
        assert [c["fingerprint"] for c in reduced_path["changes"]] == [
            c["fingerprint"] for c in full_path["changes"][: len(reduced_path["changes"])]
        ]
    assert forms.core["form"] == "core"
    assert set(forms.core) == {
        "version",
        "form",
        "limits",
        "rounds",
        "attempts",
        "round_outcomes",
        "diagnoses",
    }


def test_the_guard_drops_from_paths_first_and_keeps_totals() -> None:
    rows = content_contract_rows()
    rounds = rounds_for(rows, promoted={3})
    full = build_aggregate(rounds, rows)
    lowered = build_aggregate(rounds, rows, max_chars=serialized_chars(full) - 1)
    assert len(lowered["paths"]) == len(full["paths"]) - 1
    assert lowered["paths_total"] == full["paths_total"]


_U1 = "\x01"
_REASONS = (
    "focus_below_margin",
    "global_bound_not_positive",
    "critical_failure",
    "lower_bound_not_positive",
    "changed_file_budget",
    "written_byte_budget",
    "editor_declined",
    "seal_refused",
    "actor_violation",
    "no_exam_impact",
    "editor_failed",
)
_ROUND_OUTCOMES = tuple(
    (outcome, reason)
    for outcome in (
        "condition_changed",
        "unmeasurable",
        "unplanned",
        "stalled",
        "promoted",
        "unscored",
    )
    for reason in ("reflection_declined", "reflection_failed")
)


def _worst_rows() -> tuple[list[RoundRow], list[FactRow]]:
    """Synthetic worst case: 256-character ids, 10-digit
    numbers, 24-character floats, the escaped path and the 120-character
    excerpt literal, and every list of the full form at its cap: 6 paths with
    5 changes of 3 rows each (so 5 paths x 4 changes x 2 witnesses show),
    6 promotions, 11 reasons, many gains and 12 round outcomes."""

    base = 9_999_999_000
    excerpt = _U1 * 19 + "abcd"
    float24 = -1.2345678901234567e-123
    assert len(repr(float24)) == 24
    rows: list[FactRow] = []
    for p in range(6):
        path = "d/" + _U1 * 250 + f"/f{p:02d}"
        for c in range(5):
            for w in range(3):
                n = base + p * 100 + c * 10 + w
                promoted = (c, w) == (0, 0)
                description = {
                    "basis": "too_large",
                    "before": "binary+x",
                    "after": "binary+x",
                    "added": 9_999_999_999,
                    "removed": 9_999_999_999,
                    "hunks": 9_999_999_999,
                    "excerpt": excerpt,
                    "excerpt_cut": True,
                }
                rows.append(
                    FactRow(
                        round=n,
                        attempt_id=("a" * 240) + f"{p:05d}{c:05d}{w:06d}",
                        index=9_999_999_999,
                        outcome="promoted" if promoted else "abandoned",
                        reason=None if promoted else _REASONS[(p * 15 + c * 3 + w) % len(_REASONS)],
                        code=None if promoted else "artifact_unsafe",
                        promoted=promoted,
                        measurement="present",
                        exam_mean_delta=float24 if w == 1 else -float24,
                        rest_mean_delta=float24,
                        changes_status="present",
                        paths=(path,),
                        changes=(PathChange(f"sha256:{p:032d}{c:032d}", description),),
                    )
                )
    rounds = [
        RoundRow(r.round, *_ROUND_OUTCOMES[k % len(_ROUND_OUTCOMES)], None, "present", True)
        for k, r in enumerate(rows)
    ]
    return rounds, rows


def test_serialized_maxima_hold_at_the_worst_case() -> None:
    rounds, rows = _worst_rows()
    forms = aggregate_forms(rounds, rows)
    # The caps are reached, so the maxima below are tested at the worst case.
    assert len(forms.full["paths"]) == 5 and all(
        len(p["changes"]) == 4 for p in forms.full["paths"]
    )
    assert all(len(c["witnesses"]) == 2 for p in forms.full["paths"] for c in p["changes"])
    assert (
        len(forms.full["promotions"]),
        len(forms.full["reasons"]),
        len(forms.full["gains"]),
        len(forms.full["round_outcomes"]),
    ) == (4, 10, 4, 12)
    assert serialized_chars(forms.full) <= AGGREGATE_MAX_CHARS
    assert serialized_chars(forms.reduced) <= AGGREGATE_REDUCED_MAX_CHARS
    assert serialized_chars(forms.core) <= 1_550
    for entry in forms.full["paths"]:
        assert serialized_chars(entry) <= 6_600
        for change_entry in entry["changes"]:
            assert serialized_chars(change_entry) <= 1_250
            assert serialized_chars(change_entry["change"]) <= 270
    for entry in forms.reduced["paths"]:
        assert serialized_chars(entry) <= 4_100
    assert all(serialized_chars(e) <= 1_600 for e in forms.full["promotions"])
    assert all(serialized_chars(e) <= 500 for e in forms.full["reasons"])
    assert all(serialized_chars(e) <= 800 for e in forms.full["gains"])


def _numbers_to_zero(value: Any) -> str:
    return re.sub(r"-?\d+(\.\d+)?(e-?\d+)?", "0", json.dumps(value, sort_keys=True))


def test_a_steady_run_has_the_same_aggregate_numbers_aside() -> None:
    def steady(n: int) -> tuple[list[RoundRow], list[FactRow]]:
        rows = [
            row(
                r,
                i,
                delta=-1.0 if i == 1 else 0.0,
                paths={f"p{k:02d}.md": f"sha256:{k:064d}" for k in range(20)},
            )
            for r in range(1, n + 1)
            for i in range(1, 6)
        ]
        return rounds_for(rows), rows

    small = aggregate_forms(*steady(100)).full
    large = aggregate_forms(*steady(20_000)).full
    assert _numbers_to_zero(small) == _numbers_to_zero(large)


_MAX_BOUND = 9_999_999_999
_MAX_ID = "a" * 256
_LONG_CODE = "c" * 256


def test_a_lifecycle_admitted_long_seal_code_survives_aggregate_and_guard() -> None:
    from evolution.tree.campaign_schema import CampaignLifecycle, campaign_family_for
    from evolution.tree.storage.envelope import Envelope

    journal = Path(__file__).parents[3] / "fixtures/family14_b2/run/campaign.jsonl"
    prefix: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        prefix.append(record)
        if record["event"] == "editor_finished" and record["payload"].get("status") == "edited":
            break
    assert len(prefix) == 23 and prefix[-1]["payload"]["attempt_id"] == "r01-a02"

    def replace_attempt_id(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    _MAX_ID
                    if key == "attempt_id" and item == "r01-a02"
                    else replace_attempt_id(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [replace_attempt_id(item) for item in value]
        return value

    life = CampaignLifecycle(selector=None, adaptive=True, family_version=14)
    for record in prefix:
        life.accept(
            Envelope.from_payload(replace_attempt_id(record), family=campaign_family_for(14))
        )
    life.accept(
        Envelope.from_payload(
            {
                "event": "attempt_closed",
                "operation_id": "f" * 32,
                "payload": {
                    "attempt_id": _MAX_ID,
                    "outcome": "abandoned",
                    "reason": "seal_refused",
                    "code": _LONG_CODE,
                    "look": None,
                },
                "run_id": "c-run",
                "schema_version": 1,
                "sequence": 24,
                "timestamp": "2026-10-16T00:00:00.000000Z",
            },
            family=campaign_family_for(14),
        )
    )
    state = life.snapshot.rounds[1].attempts[_MAX_ID]
    assert (state.outcome, state.reason, state.code) == ("abandoned", "seal_refused", _LONG_CODE)

    fact = FactRow(
        round=state.round,
        attempt_id=state.attempt_id,
        index=state.index,
        outcome=state.outcome,
        reason=state.reason,
        code=state.code,
        promoted=False,
        measurement="absent",
        exam_mean_delta=None,
        rest_mean_delta=None,
        changes_status="absent",
    )
    round_row = RoundRow(1, "stalled", None, None, "present", False)
    aggregate = build_aggregate([round_row], [fact])
    assert aggregate["reasons"][0]["code"] == _LONG_CODE
    assert aggregate["reasons"][0]["last_attempt"] == f"attempt:{_MAX_ID}"
    assert serialized_chars(aggregate["reasons"][0]) <= 661

    reasons = [
        "focus_below_margin",
        "global_bound_not_positive",
        "critical_failure",
        "lower_bound_not_positive",
        "changed_file_budget",
        "written_byte_budget",
        "editor_declined",
        "seal_refused",
        "actor_violation",
        "no_exam_impact",
        "editor_failed",
    ]
    many = [
        row(
            9_999_999_999 - i,
            i,
            outcome="abandoned",
            reason=reason,
            code=_LONG_CODE,
            measurement="absent",
            attempt_id=_MAX_ID[:-4] + f"{i:04d}",
        )
        for i, reason in enumerate(reasons)
    ]
    rounds = [RoundRow(item.round, "stalled", None, None, "present", False) for item in many]
    forms = aggregate_forms(rounds, many)
    assert forms.full["reasons_total"] == len(reasons)
    assert forms.reduced["reasons_total"] == len(reasons)
    assert serialized_chars(forms.full) <= AGGREGATE_MAX_CHARS
    assert serialized_chars(forms.reduced) <= AGGREGATE_REDUCED_MAX_CHARS
    guarded = build_aggregate(rounds, many, max_chars=4_000)
    assert serialized_chars(guarded) <= 4_000
    assert guarded["reasons_total"] == len(reasons)


def test_conservative_serialized_entry_envelopes_fit_with_long_codes() -> None:
    assert len(json.dumps("d/" + "x" * 254, ensure_ascii=False)) == 258
    # The 661-character reason envelope uses the admitted identifier maximum,
    # the full identifier prefix, and all three ten-digit counters.
    reason = {
        "outcome": "abandoned",
        "reason": "seal_refused",
        "code": _LONG_CODE,
        "attempts": _MAX_BOUND,
        "rounds": _MAX_BOUND,
        "last_round": _MAX_BOUND,
        "last_attempt": f"attempt:{_MAX_ID}",
    }
    assert serialized_chars(reason) == 661
    full_round, full_rows = _worst_rows()
    full_rows = [item if item.promoted else replace(item, code=_LONG_CODE) for item in full_rows]
    forms = aggregate_forms(full_round, full_rows)
    assert all(serialized_chars(entry) <= 661 for entry in forms.full["reasons"])
    assert all(serialized_chars(entry) <= 661 for entry in forms.reduced["reasons"])
    assert serialized_chars(forms.full) <= AGGREGATE_MAX_CHARS
    assert serialized_chars(forms.reduced) <= AGGREGATE_REDUCED_MAX_CHARS


def test_conservative_path_change_and_promotion_entry_envelopes_fit() -> None:
    displayed = "d/" + "x" * 254
    other_paths = ("e/" + "y" * 254, "f/" + "z" * 254)
    assert all(
        len(json.dumps(path, ensure_ascii=False)) == 258 for path in (displayed, *other_paths)
    )
    excerpt = "\x01" * 19 + "abcd"
    description = {
        "basis": "too_large",
        "before": "binary+x",
        "after": "binary+x",
        "added": _MAX_BOUND,
        "removed": _MAX_BOUND,
        "hunks": _MAX_BOUND,
        "excerpt": excerpt,
        "excerpt_cut": True,
    }
    fingerprint = FP_E
    promoted = FactRow(
        round=_MAX_BOUND - 1,
        attempt_id=_MAX_ID,
        index=_MAX_BOUND,
        outcome="promoted",
        reason=None,
        code=None,
        promoted=True,
        measurement="present",
        exam_mean_delta=-1.2345678901234567e-123,
        rest_mean_delta=1.2345678901234567e-123,
        changes_status="present",
        paths=(displayed, *other_paths),
        changes=tuple(PathChange(fingerprint, description) for _ in range(3)),
    )
    regressed = FactRow(
        round=_MAX_BOUND,
        attempt_id="b" * 256,
        index=_MAX_BOUND,
        outcome="abandoned",
        reason="seal_refused",
        code=_LONG_CODE,
        promoted=False,
        measurement="present",
        exam_mean_delta=-1.2345678901234567e-123,
        rest_mean_delta=1.2345678901234567e-123,
        changes_status="present",
        paths=(displayed,),
        changes=(PathChange(fingerprint, description),),
    )
    rounds = [
        RoundRow(_MAX_BOUND - 1, "promoted", None, promoted.attempt_id, "present", True),
        RoundRow(_MAX_BOUND, "stalled", None, None, "present", True),
    ]
    forms = aggregate_forms(rounds, [promoted, regressed])
    path_entry = next(entry for entry in forms.full["paths"] if entry["path"] == displayed)
    change_entry = path_entry["changes"][0]
    promotion_entry = forms.full["promotions"][0]
    assert len(promotion_entry["changed_paths"]) == 3
    assert all(item["path"] is not None for item in promotion_entry["changed_paths"])
    assert len(change_entry["witnesses"]) == 2
    assert serialized_chars(change_entry["change"]) <= 270
    assert serialized_chars(change_entry) <= 1_250
    assert serialized_chars(path_entry) <= 6_600
    assert serialized_chars(promotion_entry) <= 1_600
    assert serialized_chars(forms.full["reasons"][0]) <= 661
    assert serialized_chars(forms.full) <= AGGREGATE_MAX_CHARS
    assert serialized_chars(forms.reduced) <= AGGREGATE_REDUCED_MAX_CHARS
    assert serialized_chars(forms.core) <= 1_550


def test_composed_serialized_output_upper_envelopes() -> None:
    """Conservative impossible combinations close U1-R1-003, without row billions."""
    from copy import deepcopy

    from evolution.tree.roles.reflector import reflect_aggregate
    from evolution.tree.roles.reflector_contracts import (
        AGGREGATE_FULL_LIMITS,
        AGGREGATE_REDUCED_LIMITS,
    )

    maximum = 9_999_999_999
    attempt = "attempt:" + "a" * 256
    description = {
        "basis": "too_large",
        "before": "binary+x",
        "after": "binary+x",
        "added": maximum,
        "removed": maximum,
        "hunks": maximum,
        "excerpt": "\x01" * 19 + "abcd",
        "excerpt_cut": False,
    }
    witness = {"round": maximum, "attempt": attempt, "mark": "promoted_and_regressed"}
    change = {
        "fingerprint": "sha256:" + "f" * 64,
        "pattern": "promoted_and_regressed",
        "change": description,
        **{
            k: maximum
            for k in (
                "rounds",
                "attempts",
                "measured",
                "gained",
                "promoted",
                "regressed_rounds",
                "last_round",
            )
        },
        "witnesses": [deepcopy(witness), deepcopy(witness)],
    }
    path = {
        "path": "p" * 256,
        "path_sha256": None,
        "pattern": "repeated_regression_distinct",
        **{
            k: maximum
            for k in (
                "rounds",
                "attempts",
                "measured",
                "promoted",
                "gained",
                "regressed_rounds",
                "first_round",
                "last_round",
                "changes_total",
                "changes_unfingerprinted",
            )
        },
        "recent_rounds": [maximum] * 3,
        "last_attempt": attempt,
        "regressed_witness": {"round": maximum, "attempt": attempt},
        "promoted_witness": {"round": maximum, "attempt": attempt},
        "changes": [deepcopy(change) for _ in range(4)],
    }
    reduced_path = {
        **deepcopy(path),
        "recent_rounds": [maximum] * 3,
        "changes": [deepcopy(change) for _ in range(2)],
    }
    assert serialized_chars(path["path"]) == 258
    assert serialized_chars(description["excerpt"]) == 120
    assert serialized_chars(path) <= 6600
    assert serialized_chars(reduced_path) <= 4100
    rounds, rows = _worst_rows()
    forms = aggregate_forms(rounds, rows)

    def envelope(template, limits, entry):
        value = deepcopy(template)
        value["rounds"] = {"count": maximum, "first": maximum, "last": maximum}
        value["attempts"] = dict.fromkeys(value["attempts"], maximum)
        value["diagnoses"] = dict.fromkeys(value["diagnoses"], maximum)
        value["round_outcomes"] = [
            {"outcome": "condition_changed", "reason": "reflection_declined", "count": maximum}
            for _ in range(12)
        ]
        value["paths"] = [deepcopy(entry) for _ in range(limits["paths"])]
        value["paths_total"] = maximum
        for name, count in (
            ("promotions", limits["promotions"]),
            ("gains", limits["gains"]),
            ("reasons", limits["reasons"]),
        ):
            sample = deepcopy(value[name][0])
            for key, item in sample.items():
                if isinstance(item, int) and not isinstance(item, bool):
                    sample[key] = maximum
            if name == "promotions":
                sample["changed_paths"] = [
                    {"path": "p" * 256, "path_sha256": None}
                    for _ in range(limits["promotion_paths"])
                ]
                sample["changed_paths_truncated"] = False
            if name in ("promotions", "gains"):
                sample["attempt_id"] = "a" * 256
                sample["details"] = [attempt, "diagnosis:9999999999"]
                sample["exam_mean_delta"] = sample["rest_mean_delta"] = -1.2345678901234567e-123
            else:
                sample.update(
                    outcome="abandoned",
                    reason="lower_bound_not_positive",
                    code="target_undeclared",
                    last_attempt=attempt,
                )
            value[name] = [deepcopy(sample) for _ in range(count)]
            value[name + "_total"] = maximum
        return value

    full = envelope(forms.full, AGGREGATE_FULL_LIMITS, path)
    reduced = envelope(forms.reduced, AGGREGATE_REDUCED_LIMITS, reduced_path)
    core = reflect_aggregate.core_aggregate(full)
    assert serialized_chars(full) <= 50000
    assert serialized_chars(reduced) <= 22000
    assert serialized_chars(core) <= 1550
    for value, cap in ((full, 50000), (reduced, 22000)):
        long = deepcopy(value)
        for reason in long["reasons"]:
            reason.update(reason="seal_refused", code="c" * 256)
            assert serialized_chars(reason) == 661
        before = serialized_chars(long)
        if before > cap:
            guarded = reflect_aggregate._guard(long, cap)
            assert serialized_chars(guarded) <= cap
            assert guarded["paths_total"] == maximum and len(guarded["paths"]) < len(value["paths"])
        print(
            f"synthetic composed {value['form']} short={serialized_chars(value)} long_before_guard={before} long_after_guard={serialized_chars(long)}"
        )
    print(
        f"synthetic composed path full={serialized_chars(path)} reduced={serialized_chars(reduced_path)} core={serialized_chars(core)}"
    )
