"""The family-14 Reflector's compact closed-round history.

ADR-0039 owns this view. Built only by the Reflector path of campaign family
14 from one journal snapshot, the measurement rows and the digest-verified
published copies; `project_history`, `history_payload(…, excerpts=False)` and
the Editor brief are untouched. Every fact this module inlines stays
readable through `HistoryReader` (`attempt:`, `diagnosis:`, `changes:` ids).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from evolution.tree.artifacts.artifact_policy import ArtifactPolicyV1
from evolution.tree.campaign_policy import CampaignPolicyV11
from evolution.tree.campaign_schema import (
    AttemptState,
    CampaignSnapshot,
    RoundState,
    effective_attempt_feedback,
)
from evolution.tree.execution.interaction import EvalCase
from evolution.tree.roles.editor.editor_brief import BUDGET_REASON_SENTENCES
from evolution.tree.roles.editor.editor_evidence import (  # noqa: PLC2701
    PublishedRefs,
    RowSource,
    _changes_with_scans,
    _diagnosis_payload,
    _feedback_scores,
    _history_attempt,
    _result,
    _verify_published_copies,
)
from evolution.tree.roles.editor_contracts import (
    BRIEF_LIMITS_V2,
    PRESENT,
    _diagnosis_for,
    history_attempt_payload,
)
from evolution.tree.roles.reflection_reader import ByteLruCache, Handler, ReaderArgumentError
from evolution.tree.roles.reflector.reflect_aggregate import (
    AggregateForms,
    FactRow,
    RoundRow,
    aggregate_forms,
    round_range,
)
from evolution.tree.roles.reflector.reflect_change import (
    PathChange,
    Screen,
    path_change,
    path_display,
)
from evolution.tree.roles.reflector.reflection_inputs import (  # noqa: PLC2701
    EVIDENCE_PAGE_SIZE,
    _bounded_entries,
    _canonical,
    _offset,
    _page,
)
from evolution.tree.roles.reflector_contracts import (
    CHANGE_FINGERPRINT_SCHEME,
    COMPACT_CHANGED_PATHS,
    COMPACT_TARGET_PATHS,
    COMPACT_TEXT_CHARS,
    REFLECT_LIMITS_V1,
    SUMMARY_WINDOW_ROUNDS,
)
from evolution.tree.roles.role_failures import EDITOR_BUDGET_REASONS
from evolution.tree.storage.journal_errors import JournalBoundaryError

HISTORY_VERSION = "reflect_history/1"
#: The limits today's Reflector history projects with: `history_rounds` 6,
#: `attempts_per_round` 8 and `changed_paths` 32 (`campaign.py` passes the same).
READER_LIMITS: Mapping[str, int] = {**BRIEF_LIMITS_V2, **REFLECT_LIMITS_V1}
_EDITOR_TERMINALS = frozenset({"edited", "declined", "failed"})


def _block(rows: Sequence[tuple[float, bool]]) -> dict[str, Any]:
    deltas = [delta for delta, _ in rows]
    return {
        "cases": len(rows),
        "mean_delta": math.fsum(deltas) / len(rows) if rows else None,
        "improved": sum(delta > 0 for delta in deltas),
        "worsened": sum(delta < 0 for delta in deltas),
        "unchanged": sum(delta == 0 for delta in deltas),
        "incomplete": sum(incomplete for _, incomplete in rows),
    }


def attempt_scores(
    state: RoundState,
    attempt: AttemptState,
    measurements: Mapping[str, RowSource],
    policy: CampaignPolicyV11,
) -> dict[str, Any] | None:
    """`exam`/`rest` blocks from the admitted Parent/Candidate pairs, paired and
    averaged exactly as frozen attempt feedback does (`project_attempt_feedback`,
    `_feedback_scores`, `attempt_feedback._case_rows`); `None` when either side
    cannot be read. `exam` is the round's gate scope."""

    try:
        parent = _feedback_scores(
            measurements[str(state.baseline_measurement_id)].sample_rows(policy.repeats),
            policy.metric_primary,
        )
        child = _feedback_scores(
            measurements[str(attempt.measurement_id)].sample_rows(policy.repeats),
            policy.metric_primary,
        )
    except (KeyError, ValueError, JournalBoundaryError):
        return None
    grouped: dict[str, list[float]] = defaultdict(list)
    for case_id, repeat in sorted(parent.keys() & child.keys()):
        grouped[case_id].append(child[(case_id, repeat)] - parent[(case_id, repeat)])
    exam_cases = frozenset(() if state.gate_scope is None else state.gate_scope["cases"])
    exam: list[tuple[float, bool]] = []
    rest: list[tuple[float, bool]] = []
    for case_id, deltas in grouped.items():
        try:
            delta = math.fsum(deltas) / len(deltas)
        except OverflowError:
            return None
        if not math.isfinite(delta):
            return None
        (exam if case_id in exam_cases else rest).append((delta, len(deltas) < policy.repeats))
    return {"exam": _block(exam), "rest": _block(rest)}


@dataclass(frozen=True)
class AttemptFacts:
    """One closed Attempt's fact row plus what its compact entry also shows."""

    row: FactRow
    editor: str | None
    look: int | None
    scores: Mapping[str, Any] | None
    #: `_changes`' own 32-path cut (`changes.truncated` of the `attempt:` record).
    truncated: bool


def attempt_facts(
    state: RoundState,
    attempt: AttemptState,
    *,
    family_version: int,
    promoted: bool,
    measurements: Mapping[str, RowSource],
    store: PublishedRefs,
    artifact_policy: ArtifactPolicyV1,
    corpus: Sequence[EvalCase],
    policy: CampaignPolicyV11,
    visible: Callable[[str], bool] | None,
    screen: Screen,
) -> AttemptFacts:
    reason = effective_attempt_feedback(state, attempt, family_version=family_version)[0]
    status, parent_root, child_root = _verify_published_copies(attempt, store, artifact_policy)
    changes, parent_scan, child_scan = _changes_with_scans(
        status, parent_root, child_root, artifact_policy, READER_LIMITS, visible=visible
    )
    facts: tuple[PathChange, ...] = ()
    if changes.status == PRESENT:
        assert parent_root is not None and child_root is not None
        assert parent_scan is not None and child_scan is not None
        before = {entry.path: entry for entry in parent_scan.artifact}
        after = {entry.path: entry for entry in child_scan.artifact}
        facts = tuple(
            path_change(
                parent_root, child_root, path, before, after, visible=visible, screen=screen
            )
            for path in changes.paths
        )
    measurement = _result(attempt, measurements, corpus, policy).status
    scores = (
        attempt_scores(state, attempt, measurements, policy) if measurement == PRESENT else None
    )

    def mean(block: str) -> float | None:
        if scores is None or scores[block]["cases"] == 0:
            return None
        return float(scores[block]["mean_delta"])

    row = FactRow(
        round=state.round,
        attempt_id=attempt.attempt_id,
        index=attempt.index,
        outcome=attempt.outcome,
        reason=reason,
        code=attempt.code,
        promoted=promoted,
        measurement=measurement,
        exam_mean_delta=mean("exam"),
        rest_mean_delta=mean("rest"),
        changes_status=changes.status,
        paths=tuple(changes.paths),
        changes=facts,
    )
    return AttemptFacts(
        row,
        attempt.editor if attempt.editor in _EDITOR_TERMINALS else None,
        attempt.look,
        scores,
        changes.truncated,
    )


def _cut(text: str, limit: int = COMPACT_TEXT_CHARS) -> tuple[str, bool]:
    return text[:limit], len(text) > limit


def _hypothesis(index: int, group: Mapping[str, Any]) -> dict[str, Any]:
    root_cause, root_cut = _cut(str(group.get("root_cause", "")))
    suggested_fix, fix_cut = _cut(str(group.get("suggested_fix", "")))
    paths = [str(path) for path in group.get("target_paths", ())]
    shown = [path[:COMPACT_TEXT_CHARS] for path in paths[:COMPACT_TARGET_PATHS]]
    return {
        "index": index,
        "root_cause": root_cause,
        "root_cause_truncated": root_cut,
        "suggested_fix": suggested_fix,
        "suggested_fix_truncated": fix_cut,
        "target_paths": shown,
        "target_paths_truncated": len(paths) > COMPACT_TARGET_PATHS
        or any(len(path) > COMPACT_TEXT_CHARS for path in paths[:COMPACT_TARGET_PATHS]),
        "affected_cases": len(group.get("affected_cases", ())),
        "evidence_ids": len(group.get("evidence_ids", ())),
    }


def _attempt_entry(facts: AttemptFacts) -> dict[str, Any]:
    row = facts.row
    return {
        "attempt_id": row.attempt_id,
        "index": row.index,
        "editor": facts.editor,
        "outcome": row.outcome,
        "reason": row.reason,
        "code": row.code,
        "look": facts.look,
        "measurement": row.measurement,
        "scores": None if facts.scores is None else dict(facts.scores),
        "changes_status": row.changes_status,
        "changed_paths": [path[:COMPACT_TEXT_CHARS] for path in row.paths[:COMPACT_CHANGED_PATHS]],
        "changed_paths_truncated": facts.truncated or len(row.paths) > COMPACT_CHANGED_PATHS,
        "details": f"attempt:{row.attempt_id}",
    }


@dataclass(frozen=True)
class ClosedRound:
    """One closed round: its row for the aggregate, its two compact forms, its
    Attempts' facts (every Attempt, index order) and its journal state."""

    row: RoundRow
    summary: Mapping[str, Any]
    detail: Mapping[str, Any]
    attempts: tuple[AttemptFacts, ...] = ()
    state: RoundState | None = None


def closed_round(
    state: RoundState,
    attempts: Sequence[AttemptFacts],
    *,
    family_version: int,
    attempts_per_round: int = READER_LIMITS["attempts_per_round"],
) -> ClosedRound:
    assert state.closed is not None
    diagnosis = _diagnosis_payload(state, True)
    status = "absent" if diagnosis is None else str(diagnosis["status"])
    groups = [] if diagnosis is None else list(diagnosis["groups"])
    selected = (state.selection or {}).get("attempt_id")
    outcome = str(state.closed["outcome"])
    reason = state.closed.get("reason")
    row = RoundRow(
        state.round,
        outcome,
        None if reason is None else str(reason),
        selected,
        status,
        diagnosis is not None,
    )
    summary = {
        "round": state.round,
        "outcome": outcome,
        "reason": row.reason,
        "selected_attempt": selected,
        "promoted": outcome == "promoted",
        "attempts_opened": len(attempts),
        "attempts_measured": sum(facts.row.measurement == PRESENT for facts in attempts),
        "diagnosis": {
            "status": status,
            "error_class": None if diagnosis is None else diagnosis["error_class"],
            "hypotheses": len(groups),
        },
        "details": [f"diagnosis:{state.round}"] if diagnosis is not None else [],
    }
    shown = attempts[:attempts_per_round]
    detail = {
        **summary,
        "hypotheses": [_hypothesis(index, group) for index, group in enumerate(groups, start=1)],
        "attempts": [_attempt_entry(facts) for facts in shown],
        "attempts_truncated": len(shown) < len(attempts),
    }
    return ClosedRound(row, summary, detail, tuple(attempts), state)


class HistorySource:
    """Every closed round of this run, read once from one snapshot (the one the
    brief is built from), plus what `HistoryReader` needs to rebuild today's
    `attempt:` entries lazily."""

    def __init__(
        self,
        rounds: Sequence[ClosedRound],
        *,
        predecessor_rounds: int,
        aggregate: AggregateForms,
        family_version: int = 14,
        measurements: Mapping[str, RowSource] | None = None,
        store: PublishedRefs | None = None,
        artifact_policy: ArtifactPolicyV1 | None = None,
        corpus: Sequence[EvalCase] = (),
        policy: CampaignPolicyV11 | None = None,
        visible: Callable[[str], bool] | None = None,
    ) -> None:
        self.rounds = tuple(rounds)
        self.predecessor_rounds = predecessor_rounds
        self.aggregate = aggregate
        self.family_version = family_version
        self.measurements = measurements
        self.store = store
        self.artifact_policy = artifact_policy
        self.corpus = tuple(corpus)
        self.policy = policy
        self.visible = visible


#: The memo key of one closed Attempt's facts: a closed round never changes, so
#: `(round, attempt_id, candidate_digest, measurement_id)` names the same facts
#: for the life of a campaign process. The memo changes no byte.
MemoKey = tuple[int, str, str | None, str | None]


def build_history_source(
    snapshot: CampaignSnapshot,
    *,
    measurements: Mapping[str, RowSource],
    store: PublishedRefs,
    artifact_policy: ArtifactPolicyV1,
    corpus: Sequence[EvalCase],
    policy: CampaignPolicyV11,
    visible: Callable[[str], bool] | None,
    screen: Screen,
    memo: MutableMapping[MemoKey, AttemptFacts] | None = None,
) -> HistorySource:
    family_version = snapshot.family_version
    linked = snapshot.identity.get("linked_from")
    predecessor = int(linked["rounds_closed"]) if isinstance(linked, Mapping) else 0
    rounds: list[ClosedRound] = []
    for number in sorted(n for n, state in snapshot.rounds.items() if state.closed is not None):
        state = snapshot.rounds[number]
        assert state.closed is not None
        selected = (state.selection or {}).get("attempt_id")
        promoted_round = state.closed["outcome"] == "promoted"
        facts: list[AttemptFacts] = []
        for attempt in sorted(state.attempts.values(), key=lambda item: item.index):
            key: MemoKey = (
                number,
                attempt.attempt_id,
                attempt.candidate_digest,
                attempt.measurement_id,
            )
            cached = None if memo is None else memo.get(key)
            if cached is None:
                cached = attempt_facts(
                    state,
                    attempt,
                    family_version=family_version,
                    promoted=promoted_round and attempt.attempt_id == selected,
                    measurements=measurements,
                    store=store,
                    artifact_policy=artifact_policy,
                    corpus=corpus,
                    policy=policy,
                    visible=visible,
                    screen=screen,
                )
                if memo is not None:
                    memo[key] = cached
            facts.append(cached)
        rounds.append(closed_round(state, facts, family_version=family_version))
    aggregate = aggregate_forms(
        [entry.row for entry in rounds], [facts.row for entry in rounds for facts in entry.attempts]
    )
    return HistorySource(
        rounds,
        predecessor_rounds=predecessor,
        aggregate=aggregate,
        family_version=family_version,
        measurements=measurements,
        store=store,
        artifact_policy=artifact_policy,
        corpus=corpus,
        policy=policy,
        visible=visible,
    )


@dataclass(frozen=True)
class Layout:
    """Which closed rounds are aggregated, summarized and in detail (three
    consecutive runs, oldest first), what moved and the aggregate's form."""

    aggregated: int
    summary: int
    detail: int
    window_to_aggregate: int
    detail_to_summary: int = 0
    summary_to_aggregate: int = 0
    form: str = "full"
    budget: bool = False


def initial_layout(
    closed: int,
    *,
    detail_rounds: int = REFLECT_LIMITS_V1["history_rounds"],
    window: int = SUMMARY_WINDOW_ROUNDS,
) -> Layout:
    detail = min(detail_rounds, closed)
    summary = min(window, closed - detail)
    aggregated = closed - detail - summary
    return Layout(aggregated, summary, detail, aggregated)


def elision_steps(start: Layout, *, window: int = SUMMARY_WINDOW_ROUNDS) -> Iterator[Layout]:
    """The history budget steps, one yielded layout per step."""

    current = replace(start, budget=True)
    while current.detail > 1:  # step 1
        current = replace(
            current,
            detail=current.detail - 1,
            summary=current.summary + 1,
            detail_to_summary=current.detail_to_summary + 1,
        )
        if current.summary > window:
            current = replace(
                current,
                summary=current.summary - 1,
                aggregated=current.aggregated + 1,
                summary_to_aggregate=current.summary_to_aggregate + 1,
            )
        yield current
    while current.summary > 0:  # step 2
        current = replace(
            current,
            summary=current.summary - 1,
            aggregated=current.aggregated + 1,
            summary_to_aggregate=current.summary_to_aggregate + 1,
        )
        yield current
    current = replace(current, form="reduced")  # step 3
    yield current
    current = replace(current, form="core")  # step 4
    yield current
    if current.detail == 1:
        current = replace(  # step 5
            current, detail=0, summary=1, detail_to_summary=current.detail_to_summary + 1
        )
        yield current
        current = replace(  # step 6
            current,
            summary=0,
            aggregated=current.aggregated + 1,
            summary_to_aggregate=current.summary_to_aggregate + 1,
        )
        yield current


def render_history(source: HistorySource, layout: Layout) -> dict[str, Any]:
    rounds = source.rounds
    aggregated = rounds[: layout.aggregated]
    summaries = rounds[layout.aggregated : layout.aggregated + layout.summary]
    details = rounds[layout.aggregated + layout.summary :]
    assert len(details) == layout.detail
    reason = "budget" if layout.budget else "window" if layout.window_to_aggregate else None
    return {
        "version": HISTORY_VERSION,
        "rounds_closed": len(rounds),
        "predecessor_rounds": source.predecessor_rounds,
        "detail_rounds": round_range([entry.row.round for entry in details]),
        "summary_rounds": round_range([entry.row.round for entry in summaries]),
        "aggregated_rounds": round_range([entry.row.round for entry in aggregated]),
        "elided": {
            "reason": reason,
            "window_to_aggregate": layout.window_to_aggregate,
            "detail_to_summary": layout.detail_to_summary,
            "summary_to_aggregate": layout.summary_to_aggregate,
            "aggregate_form": layout.form,
        },
        "rounds": [dict(entry.summary) for entry in summaries]
        + [dict(entry.detail) for entry in details],
        "aggregate": dict(source.aggregate.form(layout.form)),
    }


def bound_history(
    source: HistorySource, *, fits: Callable[[Mapping[str, Any]], bool]
) -> tuple[dict[str, Any], Layout]:
    """The first layout of the window placement and the budget steps that
    `fits`; the minimum (everything aggregated, core form) when none does.
    `fits` decides; this function never measures anything itself."""

    layout = initial_layout(len(source.rounds))
    history = render_history(source, layout)
    if fits(history):
        return history, layout
    for next_layout in elision_steps(layout):
        layout = next_layout
        history = render_history(source, layout)
        if fits(history):
            return history, layout
    return history, layout


HISTORY_COMPACT_SENTENCE = (
    "History is compact. `measurement: absent` means no Candidate of that Attempt was scored; "
    "it is not evidence for or against any hypothesis. `measurement: unavailable` means a "
    "measurement exists but could not be read; treat its result as unknown."
)
HISTORY_READ_SENTENCE = (
    "Read the full per-case results, the edit excerpt and the whole diagnosis with "
    "read_history, using the ids in `details`."
)
AGGREGATE_SENTENCE = (
    "The aggregate summarizes every closed round of this run: which paths were changed, which "
    "distinct changes were made to each path, what was measured afterward, which Attempts were "
    "promoted and which reasons recurred. It reports what happened together, not what caused it: "
    "a change marked repeated_regression was made in Attempts whose exam measured lower in at "
    "least two rounds and in most of the rounds that made it, and a path marked "
    "repeated_regression_distinct received at least two different changes that were each "
    "followed by a lower exam, in most of the rounds that changed it, with none promoted; "
    "neither shows that a change caused the drop."
)
FINGERPRINT_SENTENCE = (
    "Change entries with the same fingerprint made the same change to that path's lines, "
    "wherever in the file; different fingerprints are usually different changes; the same edit "
    "can get two fingerprints when the lines beside it differ between Parents. The excerpt is "
    "the first non-blank line the change added, possibly cut, and it can be missing."
)
AGGREGATE_READ_SENTENCE = (
    "Read the records named in the aggregate (`details`, `last_attempt`, and "
    "`diagnosis:<round>` for the rounds it lists) with read_history before proposing a change "
    "to a path that already has a history. `changes:<attempt_id>` gives one Attempt's "
    "fingerprint and description for every visible path it changed."
)
WINDOW_SENTENCE = (
    "Rounds older than the summary window appear only in the aggregate; their records are "
    "readable with read_history."
)
BUDGET_SENTENCE = (
    "Some rounds are shown as summaries or only in the aggregate to fit the request budget; "
    "their records are readable with read_history."
)
REDUCED_SENTENCE = (
    "The aggregate's lists are shortened to fit the request budget; `limits` shows the caps in "
    "force and the `*_total` fields count every entry."
)
CORE_SENTENCE = (
    "The aggregate is reduced to counts to fit the request budget; use list_history to find "
    "earlier records."
)


def history_sentences(history: Mapping[str, Any]) -> list[str]:
    """Readable history sentences outside the digested payload."""

    lines = [
        HISTORY_COMPACT_SENTENCE,
        HISTORY_READ_SENTENCE,
        AGGREGATE_SENTENCE,
        FINGERPRINT_SENTENCE,
        AGGREGATE_READ_SENTENCE,
    ]
    reason = history["elided"]["reason"]
    if reason == "window":
        lines.append(WINDOW_SENTENCE)
    elif reason == "budget":
        lines.append(BUDGET_SENTENCE)
    form = history["aggregate"]["form"]
    if form == "reduced":
        lines.append(REDUCED_SENTENCE)
    elif form == "core":
        lines.append(CORE_SENTENCE)
    return lines


def compact_budget_reason_sentences(history: Mapping[str, Any]) -> list[str]:
    """`budget_reason_sentences` over every reason the compact history shows:
    detail entries and the aggregate's `reasons`."""

    shown = {
        attempt.get("reason")
        for round_ in history.get("rounds", ())
        for attempt in round_.get("attempts", ())
    }
    shown |= {entry.get("reason") for entry in history.get("aggregate", {}).get("reasons", ())}
    return [BUDGET_REASON_SENTENCES[reason] for reason in sorted(EDITOR_BUDGET_REASONS & shown)]


ATTEMPT_CHANGES_VERSION = "reflect_attempt_changes/1"


def attempt_changes_record(facts: AttemptFacts) -> dict[str, Any]:
    """One Attempt's fingerprint and description for every
    visible changed path within the 32-path cap, equal to any aggregate's."""

    row = facts.row
    return {
        "version": ATTEMPT_CHANGES_VERSION,
        "attempt": f"attempt:{row.attempt_id}",
        "round": row.round,
        "fingerprint_scheme": CHANGE_FINGERPRINT_SCHEME,
        "status": row.changes_status,
        "truncated": facts.truncated,
        "paths": [
            {
                **path_display(path),
                "fingerprint": fact.fingerprint,
                "change": None if fact.change is None else dict(fact.change),
            }
            for path, fact in zip(row.paths, row.changes, strict=True)
        ],
    }


def _arguments(
    arguments: Mapping[str, Any], allowed: set[str], *, required: frozenset[str] = frozenset()
) -> None:
    if (
        not isinstance(arguments, Mapping)
        or set(arguments) - allowed
        or not required <= set(arguments)
    ):
        raise ReaderArgumentError("invalid_arguments")


class HistoryReader:
    """`list_history` and `read_history` for one reflection.

    Built over the `HistorySource` of the snapshot the reflection's brief was
    built from, never inside the baseline-cached `FrozenReflectionInputs`, so
    two reflections on one cached baseline can never serve each other's
    history. Bodies are built lazily, one per read, and cached under
    `history:<reflection_id>:<history_id>`. History ids are reference reading,
    never evidence ids.
    """

    def __init__(self, source: HistorySource, *, reflection_id: str, cache: ByteLruCache) -> None:
        self._source = source
        self._reflection_id = reflection_id
        self._cache = cache
        entries: list[dict[str, Any]] = []
        index: dict[str, tuple[ClosedRound, AttemptFacts | None]] = {}
        for closed in reversed(source.rounds):
            number = closed.row.round
            if closed.row.diagnosed:
                history_id = f"diagnosis:{number}"
                entries.append({"history_id": history_id, "round": number, "kind": "diagnosis"})
                index[history_id] = (closed, None)
            for facts in closed.attempts:
                for kind in ("attempt", "changes"):
                    history_id = f"{kind}:{facts.row.attempt_id}"
                    entries.append({"history_id": history_id, "round": number, "kind": kind})
                    index[history_id] = (closed, facts)
        self._entries = tuple(entries)
        self._index = index

    def handlers(self) -> dict[str, Handler]:
        return {"list_history": self.list_history, "read_history": self.read_history}

    def list_history(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        _arguments(arguments, {"offset"})
        offset = _offset(arguments, total=len(self._entries))
        if offset > len(self._entries):
            raise ReaderArgumentError(
                "invalid_offset", f"offset must be between 0 and {len(self._entries)}"
            )
        page, end = _bounded_entries(
            list(self._entries[offset : offset + EVIDENCE_PAGE_SIZE]), 0, EVIDENCE_PAGE_SIZE
        )
        absolute = offset + end
        return {
            "entries": page,
            "total": len(self._entries),
            "next_offset": absolute if absolute < len(self._entries) else None,
        }

    def read_history(self, arguments: Mapping[str, Any]) -> dict[str, object]:
        _arguments(arguments, {"history_id", "offset"}, required=frozenset({"history_id"}))
        history_id = arguments["history_id"]
        if not isinstance(history_id, str) or history_id not in self._index:
            raise ReaderArgumentError("unknown_history")
        key = f"history:{self._reflection_id}:{history_id}"
        cached = self._cache.get(key)
        if cached is None:
            text = self._body(history_id)
            self._cache.put(key, text.encode("utf-8"))
        else:
            text = cached.decode("utf-8")
        return _page(text, arguments.get("offset", 0))

    def _body(self, history_id: str) -> str:
        closed, facts = self._index[history_id]
        kind = history_id.split(":", 1)[0]
        source = self._source
        state = closed.state
        assert state is not None
        if kind == "diagnosis":
            payload = _diagnosis_payload(state, True)
            return _canonical(_diagnosis_for(payload, excerpts=True))
        assert facts is not None
        if kind == "changes":
            return _canonical(attempt_changes_record(facts))
        assert source.measurements is not None and source.store is not None
        assert source.artifact_policy is not None and source.policy is not None
        attempt = state.attempts[facts.row.attempt_id]
        entry = _history_attempt(
            attempt,
            source.measurements,
            source.store,
            source.artifact_policy,
            source.corpus,
            source.policy,
            READER_LIMITS,
            excerpts=True,
            reason=facts.row.reason,
            visible=source.visible,
        )
        return _canonical(history_attempt_payload(entry, excerpts=True))
