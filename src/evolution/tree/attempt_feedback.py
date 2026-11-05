"""Bounded sequential-attempt feedback from admitted immutable facts.

This module deliberately knows nothing about workspaces, artifact stores, or
measurements.  Its caller supplies facts already admitted by the campaign; the
only output is a detached JSON-compatible projection suitable for freezing into
an editor input.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

# The close vocabulary every family up to 11 checks feedback against. A
# family-12 caller passes its own (`no_exam_impact`) as `outcomes`.
from evolution.tree.campaign_schema import _CONTINUABLE_ATTEMPT_OUTCOMES, REJECTION_LAYERS
from evolution.tree.storage.envelope import is_identifier

__all__ = ["FeedbackIntegrityError", "FeedbackSource", "ScorePair", "build_feedback"]


_ATTEMPT_LIMIT = 8
_CASE_LIMIT = 16
_PATH_LIMIT = 8
_EXCERPT_LIMIT = 2048
_SECTION_LIMIT = 128 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:@+\-]{0,255}\Z")
_PATH_MAX = 4096
_NAME_MAX = 255
_CHANGE_STATUSES = frozenset({"available", "unavailable"})
_MODES = frozenset({"none", "sequential"})


class FeedbackIntegrityError(ValueError):
    """An admitted immutable feedback source no longer has a valid shape."""


@dataclass(frozen=True, slots=True)
class ScorePair:
    """One admitted Parent/Candidate primary-score pair for the same repeat."""

    case_id: str
    repeat: int
    parent: float
    candidate: float


@dataclass(frozen=True, slots=True)
class FeedbackSource:
    """One closed attempt represented only by campaign-admitted facts.

    ``changed_paths`` and ``excerpt`` must already originate from verified,
    immutable published copies.  A caller must represent an unavailable copy as
    ``changes_status='unavailable'`` with no trusted change content.
    """

    attempt_id: str
    index: int
    assessed: bool
    outcome: str
    reason: str
    expected_repeats: int
    changes_status: Literal["available", "unavailable"] = "unavailable"
    changed_paths: tuple[str, ...] = ()
    excerpt: str = ""
    pairs: tuple[ScorePair, ...] = ()
    rejection_layer: str | None = None
    source_refs: tuple[str, ...] = ()
    not_measured: tuple[str, ...] = ()


def build_feedback(
    sources: Iterable[FeedbackSource],
    *,
    mode: Literal["none", "sequential"] = "sequential",
    current_index: int,
    exam_cases: Iterable[str],
    forbidden: Callable[[str], bool] | None = None,
    redact: Callable[[str], str] | None = None,
    outcomes: Mapping[str, frozenset[str]] = _CONTINUABLE_ATTEMPT_OUTCOMES,
    allow_not_measured: bool = False,
) -> dict[str, Any]:
    """Project prior same-round facts into deterministic bounded feedback.

    ``outcomes`` is the Attempt close vocabulary of the campaign's own family,
    which every source's outcome and reason must belong to.

    ``none`` intentionally returns the empty section without inspecting history.
    In sequential mode only closed attempts with an index lower than
    ``current_index`` are eligible.  The newest eight are then subject to the
    whole-section byte cap.
    """

    if mode not in _MODES:
        raise ValueError("feedback mode is invalid")
    if mode == "none":
        return {"attempts": [], "omitted_attempts": 0}
    if type(current_index) is not int or current_index < 1:
        raise ValueError("current_index must be a positive integer")

    exam = _exam_cases(exam_cases)
    admitted = tuple(sources)
    _validate_sources(admitted, outcomes, allow_not_measured=allow_not_measured)
    eligible = sorted(
        (source for source in admitted if source.index < current_index),
        key=lambda source: source.index,
    )
    bounded = eligible[-_ATTEMPT_LIMIT:]
    omitted_before_bytes = len(eligible) - len(bounded)
    projected = [_feedback_row(source, exam, forbidden, redact) for source in bounded]
    rows = [row for row in projected if row is not None]
    omitted_before_bytes += len(projected) - len(rows)

    # The section's own omitted count affects its canonical byte size.  Build
    # the retained suffix from newest to oldest, checking the final ordered
    # shape each time so an admitted row is never partially retained.
    retained_reversed: list[dict[str, Any]] = []
    omitted_by_bytes = 0
    for row in reversed(rows):
        candidate = [row, *retained_reversed]
        omitted = omitted_before_bytes + (len(rows) - len(candidate))
        section = {"attempts": candidate, "omitted_attempts": omitted}
        if len(_canonical_json(section)) <= _SECTION_LIMIT:
            retained_reversed = candidate
        else:
            omitted_by_bytes += 1
    retained = sorted(retained_reversed, key=lambda row: row["index"])
    result = {
        "attempts": retained,
        "omitted_attempts": omitted_before_bytes + omitted_by_bytes,
    }
    # A row can be individually too large only if an invalid source bypassed a
    # bound added above.  Keep this assertion local: no partially safe section
    # is ever returned.
    if len(_canonical_json(result)) > _SECTION_LIMIT:
        raise FeedbackIntegrityError("feedback section exceeds its byte limit")
    return result


def _validate_sources(
    sources: Sequence[FeedbackSource],
    outcomes: Mapping[str, frozenset[str]],
    *,
    allow_not_measured: bool = False,
) -> None:
    seen_attempts: set[str] = set()
    seen_indices: set[int] = set()
    for source in sources:
        if not isinstance(source, FeedbackSource):
            raise ValueError("feedback source is invalid")
        _identifier(source.attempt_id, "attempt_id")
        if source.attempt_id in seen_attempts:
            raise ValueError("duplicate feedback attempt")
        seen_attempts.add(source.attempt_id)
        if type(source.index) is not int or source.index < 1:
            raise ValueError("attempt index must be a positive integer")
        if source.index in seen_indices:
            raise ValueError("duplicate feedback attempt index")
        seen_indices.add(source.index)
        if type(source.assessed) is not bool:
            raise ValueError("assessed must be a boolean")
        if source.outcome not in outcomes:
            raise ValueError("attempt outcome is invalid")
        if source.reason not in outcomes[source.outcome]:
            raise ValueError("attempt reason is invalid for its outcome")
        if type(source.expected_repeats) is not int or source.expected_repeats < 1:
            raise ValueError("expected_repeats must be a positive integer")
        if source.changes_status not in _CHANGE_STATUSES:
            raise FeedbackIntegrityError("published change source is corrupt")
        # An unavailable source has no display authority.  Its incidental
        # payload is deliberately ignored, rather than becoming a reason to
        # reveal or preserve it in an exception.
        if source.changes_status == "available":
            if not isinstance(source.changed_paths, tuple) or not isinstance(source.excerpt, str):
                raise FeedbackIntegrityError("published change source is corrupt")
            for path in source.changed_paths:
                if not _artifact_path(path):
                    raise FeedbackIntegrityError("published change path is corrupt")
        if source.rejection_layer is not None:
            _identifier(source.rejection_layer, "rejection_layer")
            if REJECTION_LAYERS.get(source.reason) != source.rejection_layer:
                raise ValueError("rejection layer does not match the attempt reason")
        if not isinstance(source.source_refs, tuple):
            raise ValueError("source_refs must be a tuple")
        for reference in source.source_refs:
            _identifier(reference, "source_ref")
        _validate_pairs(source)
        if not isinstance(source.not_measured, tuple) or (
            source.not_measured and not allow_not_measured
        ):
            raise ValueError("unmeasured feedback requires its family-15 decision")
        if len(set(source.not_measured)) != len(source.not_measured):
            raise ValueError("duplicate unmeasured feedback case")
        for case in source.not_measured:
            _case_identifier(case, "case_id")
        if set(source.not_measured) & {pair.case_id for pair in source.pairs}:
            raise ValueError("unmeasured case cannot have score pairs")


def _validate_pairs(source: FeedbackSource) -> None:
    if not isinstance(source.pairs, tuple):
        raise ValueError("score pairs must be a tuple")
    seen: set[tuple[str, int]] = set()
    per_case: defaultdict[str, int] = defaultdict(int)
    for pair in source.pairs:
        if not isinstance(pair, ScorePair):
            raise ValueError("score pair is invalid")
        _case_identifier(pair.case_id, "case_id")
        if type(pair.repeat) is not int or pair.repeat < 1:
            raise ValueError("pair repeat must be a positive integer")
        key = (pair.case_id, pair.repeat)
        if key in seen:
            raise ValueError("duplicate score pair")
        seen.add(key)
        per_case[pair.case_id] += 1
        _finite_number(pair.parent, "parent score")
        _finite_number(pair.candidate, "candidate score")
    if any(count > source.expected_repeats for count in per_case.values()):
        raise ValueError("score pair count exceeds expected repeats")


def _feedback_row(
    source: FeedbackSource,
    exam_cases: frozenset[str],
    forbidden: Callable[[str], bool] | None,
    redact: Callable[[str], str] | None = None,
) -> dict[str, Any] | None:
    cases, omitted_exam, omitted_rest = _case_rows(source, exam_cases)
    changes_status, paths, excerpt, paths_truncated, excerpt_truncated = _changes(
        source, forbidden, redact
    )
    # Redaction can expand a path beyond the fixed display grammar. As with
    # the section's byte cap, omit the whole row and disclose that omission.
    if any(not _artifact_path(path) for path in paths):
        return None
    row: dict[str, Any] = {
        "attempt_id": source.attempt_id,
        "index": source.index,
        "assessed": source.assessed,
        "outcome": source.outcome,
        "reason": source.reason,
        "changes_status": changes_status,
        "changed_paths": paths,
        "changed_paths_truncated": paths_truncated,
        "diff_excerpt": excerpt,
        "diff_excerpt_truncated": excerpt_truncated,
        "cases": cases,
        "omitted_exam_cases": omitted_exam,
        "omitted_rest_cases": omitted_rest,
    }
    if source.rejection_layer is not None:
        row["rejection_layer"] = source.rejection_layer
    if source.source_refs:
        row["source_refs"] = list(source.source_refs)
    return row


def _case_rows(
    source: FeedbackSource, exam_cases: frozenset[str]
) -> tuple[list[dict[str, Any]], int, int]:
    grouped: defaultdict[str, list[ScorePair]] = defaultdict(list)
    for pair in source.pairs:
        grouped[pair.case_id].append(pair)
    rows: list[dict[str, Any]] = []
    for case_id, pairs in grouped.items():
        ordered = sorted(pairs, key=lambda pair: pair.repeat)
        try:
            delta = math.fsum(pair.candidate - pair.parent for pair in ordered) / len(ordered)
        except OverflowError as exc:
            raise ValueError("score delta must be finite") from exc
        if not math.isfinite(delta):
            raise ValueError("score delta must be finite")
        rows.append(
            {
                "case_id": case_id,
                "delta": delta,
                "paired_repeats": len(ordered),
                "expected_repeats": source.expected_repeats,
                "incomplete": len(ordered) < source.expected_repeats,
            }
        )
    rows.extend(
        dict(
            case_id=case,
            status="not_measured",
            delta=None,
            paired_repeats=0,
            expected_repeats=source.expected_repeats,
            incomplete=False,
        )
        for case in source.not_measured
    )
    exam = sorted((row for row in rows if row["case_id"] in exam_cases), key=_priority)
    rest = sorted((row for row in rows if row["case_id"] not in exam_cases), key=_priority)
    chosen_exam = exam[:_CASE_LIMIT]
    slots = _CASE_LIMIT - len(chosen_exam)
    chosen_rest = rest[:slots]
    return (
        sorted([*chosen_exam, *chosen_rest], key=lambda row: row["case_id"]),
        len(exam) - len(chosen_exam),
        len(rest) - len(chosen_rest),
    )


def _priority(row: dict[str, Any]) -> tuple[bool, float, str]:
    return row["delta"] is None, float(row["delta"] or 0), str(row["case_id"])


def _changes(
    source: FeedbackSource,
    forbidden: Callable[[str], bool] | None,
    redact: Callable[[str], str] | None = None,
) -> tuple[str, list[str], str, bool, bool]:
    if source.changes_status == "unavailable":
        return "unavailable", [], "", False, False
    # Guard the complete supplied values before either source can be shortened.
    if forbidden is not None and any(
        forbidden(value) for value in (*source.changed_paths, source.excerpt)
    ):
        return "withheld", [], "", False, False
    paths = list(source.changed_paths[:_PATH_LIMIT])
    excerpt = source.excerpt
    if redact is not None:
        paths = [redact(path) for path in paths]
        excerpt = redact(excerpt)
    return (
        "available",
        paths,
        excerpt[:_EXCERPT_LIMIT],
        len(paths) < len(source.changed_paths),
        len(excerpt) > _EXCERPT_LIMIT,
    )


def _exam_cases(values: Iterable[str]) -> frozenset[str]:
    result = frozenset(values)
    for value in result:
        _case_identifier(value, "exam case")
    return result


def _case_identifier(value: object, name: str) -> None:
    if not is_identifier(value):
        raise ValueError(f"{name} must be a bounded safe identifier")


def _identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded safe identifier")


def _finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _artifact_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > _PATH_MAX:
        return False
    if "\\" in value or value.startswith("/") or not value.isprintable():
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") and len(part) <= _NAME_MAX for part in parts)


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FeedbackIntegrityError("feedback section is not JSON-safe") from exc
