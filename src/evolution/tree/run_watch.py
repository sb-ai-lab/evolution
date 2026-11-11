"""The watch renderer and the one-shot snapshot for run observability (#197).

Decision 17 is the renderer contract: `Renderer(kind, family, clock)` turns
each new journal record into zero or more prefixed lines, reading only ids,
counts, codes, outcomes, decisions, reasons and role/kind names — never
`description(s)`, `note`, `diagnosis`, `groups` text, `public`, `oracle`,
messages, scorer output or any other free-text payload field (decision 22).
Families 13/14 pick their dialect through `campaign_vocabulary_for`;
families 5-12 reuse the shared dedicated wording and the generic
`round <n> · <event> <id?>` line everywhere else; unknown events get the
generic line. Measurement progress
(`<id> · <scored>/<scheduled> pairs scored (<r> retried, ...)`)
is rate-limited to one line per measurement per
`PROGRESS_INTERVAL_SECONDS` by the injected monotonic clock, plus one forced
line when the measurement completes. After `IDLE_FIRST_SECONDS` without a
record one idle line appears, then one every `IDLE_EVERY_SECONDS`.

`Snapshot.from_state` reduces the same read-only facts into the `--once`
multi-line view and the one-line `snapshot_line` form `--progress` prints
(spec §4.3): the created header, the current round and stage, open Attempts
and their stage, each incomplete measurement's progress, the last decision,
the suspensions so far and whether the run is finished. A run whose journal
has no records has no snapshot line. Everything here is a pure reader: no
lease, no write, nothing created under a run directory.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from evolution.tree.campaign_projections import CAMPAIGN_FILE
from evolution.tree.campaign_schema import (
    AttemptState,
    CampaignLifecycle,
    CampaignSnapshot,
    RoundState,
    campaign_family_for,
    reduce,
)
from evolution.tree.campaign_vocabulary import campaign_vocabulary_for
from evolution.tree.evaluation import MEASUREMENT_ID
from evolution.tree.evaluation_journal import (
    EVALUATION_FAMILY,
    EVALUATION_FILE,
    EvaluationState,
)
from evolution.tree.evaluation_journal import (
    MAX_LOG_BYTES as EVALUATION_MAX_LOG_BYTES,
)
from evolution.tree.evaluation_journal import (
    MAX_RECORD_BYTES as EVALUATION_MAX_RECORD_BYTES,
)
from evolution.tree.evolve import _EXIT_FOR_OUTCOME
from evolution.tree.execution.journal import MAX_JOURNAL_BYTES, MAX_RECORD_BYTES, inspect_journal
from evolution.tree.execution.journal_schema import S1_FAMILY, Lifecycle
from evolution.tree.execution.sample_outcomes import SampleOutcome, disposition_for
from evolution.tree.roles.role_failures import (
    EDITOR_PROTOCOL_CATEGORIES,
    REFLECTION_DETAIL_CATEGORIES,
    REFLECTION_DETAIL_PATH,
)
from evolution.tree.run_reader import (
    JournalTail,
    MeasurementInfo,
    RunHandle,
    RunKind,
    RunRefused,
    TryState,
    measurement_infos,
    open_run,
    tries,
)
from evolution.tree.storage.append_log import Snapshot as LogSnapshot
from evolution.tree.storage.append_log import _directory, _file, _read
from evolution.tree.storage.envelope import Envelope
from evolution.tree.storage.journal_errors import JournalBoundaryError

__all__ = [
    "IDLE_EVERY_SECONDS",
    "IDLE_FIRST_SECONDS",
    "PROGRESS_INTERVAL_SECONDS",
    "Renderer",
    "Snapshot",
    "WatchSession",
    "ending_code",
    "follow",
]

#: One progress line per measurement per window; a completion always flushes.
PROGRESS_INTERVAL_SECONDS = 10.0
#: The first idle line after this long without a record, then every ...
IDLE_FIRST_SECONDS = 60.0
#: ... this many seconds, for as long as nothing arrives.
IDLE_EVERY_SECONDS = 300.0

_RESUME_EVOLVE = "run the same evolve command after repair"
_RESUME_EVALUATE = "run the same evaluate command after repair"

_COMPARISON_LOOK = "comparison_look_evaluated"
_SELECTION_DECIDED = "selection_decided"
_COMPLETION_FLUSH_EVENTS = frozenset({"baseline_complete", "champion_complete"})


def _local_time(timestamp: str) -> str:
    """A journal timestamp (UTC, `Z`-suffixed) rendered as local HH:MM:SS."""

    utc = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    return utc.astimezone().strftime("%H:%M:%S")


def _humanize(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole}s" if whole < 60 else f"{whole // 60}m"


def _progress_text(scored: int, scheduled: int, retried: int, outcomes: Mapping[str, int]) -> str:
    """`<scored>/<scheduled> pairs scored (<r> retried, ...)` (decision 17)."""

    indeterminate = sum(
        count
        for outcome, count in outcomes.items()
        if disposition_for(cast(SampleOutcome, outcome)).kind == "indeterminate"
    )
    parts = f"{retried} retried, {indeterminate} indeterminate"
    failures = [(name, count) for name, count in outcomes.items() if name != "completed"]
    if failures:
        parts += ", " + ", ".join(f"{count} {name}" for name, count in failures)
    return f"{scored}/{scheduled} pairs scored ({parts})"


def _progress_from_tries(states: list[TryState]) -> tuple[int, int, dict[str, int]]:
    """Latest effective pair outcomes; retries summarize all started tries."""

    latest = {(state.case_id, state.sample_id): state for state in states}
    scored = sum(
        state.imported is not None or state.scoring.status == "scored" for state in latest.values()
    )
    retried = sum(1 for state in states if state.retry_ordinal)
    outcomes: dict[str, int] = {}
    for state in latest.values():
        outcome = {"failed": "scorer_failed", "interrupted": "interrupted"}.get(
            state.scoring.status, state.rollout.outcome
        )
        if outcome is not None:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return scored, retried, outcomes


def _campaign_created_line(payload: Mapping[str, Any]) -> str:
    identity = payload.get("identity") or {}
    text = f"campaign created · family {payload['family_version']}"
    cases = identity.get("corpus_cases")
    if isinstance(cases, list):
        text += f" · {len(cases)} cases"
        evaluation = (identity.get("policy") or {}).get("evaluation")
        repeats = (
            evaluation.get("repeats_per_question") if isinstance(evaluation, Mapping) else None
        )
        if repeats is not None:
            text += f" × {repeats} repeats"
            assert isinstance(evaluation, Mapping)
            focus = evaluation.get("focus_repeats_per_question")
            if focus is not None:
                text += f" (focus × {focus})"
    return text


def _evaluation_created_line(payload: Mapping[str, Any]) -> str:
    identity = payload.get("identity") or {}
    text = "evaluation created"
    profile = identity.get("profile")
    if profile:
        text += f" · {profile}"
    split = identity.get("split")
    if split:
        text += f" · {split}"
    cases = identity.get("case_groups")
    if isinstance(cases, list):
        text += f" · {len(cases)} cases"
        repeats = identity.get("repeats")
        if repeats is not None:
            text += f" × {repeats} repeats"
    return text


def _comparison_look_line(payload: Mapping[str, Any], family: int) -> str:
    role = campaign_vocabulary_for(family).registry_role
    return f"{payload['attempt_id']} · {role} look · {payload['reason']} → {payload['decision']}"


def _selection_line(payload: Mapping[str, Any]) -> str:
    reason = payload.get("reason")
    if payload.get("attempt_id") is not None:
        text = f"{payload['attempt_id']} · selected"
    else:
        text = "selection retained"
    if reason is not None:
        text += f" · {reason}"
    return text


def _decision_text(event: str, payload: Mapping[str, Any], family: int | None) -> str:
    if event == _COMPARISON_LOOK:
        role = campaign_vocabulary_for(family).registry_role if family == 14 else ""
        role_text = f"{role} " if role else ""
        return f"{payload['attempt_id']} · {role_text}look · {payload['reason']} → {payload['decision']}"
    return _selection_line(payload)


def _attempt_stage(attempt: AttemptState, measurements: Mapping[str, Any]) -> str:
    if attempt.editor is None or attempt.editor == "started":
        return "editor"
    for mid in (attempt.measurement_id, attempt.parent_measurement_id):
        if mid is None:
            continue
        stages = measurements.get(mid, {}).get("stages", ())
        # Current bindings may be new/rebound, or may have opened the next
        # look after an earlier continue. Historical looks do not settle them.
        if not stages or stages[-1]["look"] not in attempt.looks:
            scope = stages[-1].get("scope") if stages else None
            return f"measurement ({scope})" if scope else "measurement"
    if attempt.looks:
        return f"look {max(attempt.looks)}"
    return "measurement"


def _current_members(round_state: RoundState, family: int) -> tuple[str, ...]:
    if family == 14:
        return round_state.finals_current[-1] if round_state.finals_current else ()
    return ()


def _round_stage(
    snapshot: CampaignSnapshot, round_state: RoundState | None, family: int
) -> str | None:
    if snapshot.finished is not None or round_state is None:
        return None
    if not round_state.baseline_complete:
        return "baseline"
    if round_state.closed is not None:
        return "closed"
    if any(entry.get("status") == "started" for entry in round_state.reflections):
        return "reflection"
    open_attempts = [a for a in round_state.attempts.values() if a.outcome is None]
    if open_attempts:
        stages = [_attempt_stage(a, snapshot.measurements) for a in open_attempts]
        if "editor" in stages:
            return "editing"
        return "measurement" if any(stage.startswith("measurement") for stage in stages) else "look"
    members = _current_members(round_state, family)
    if members:
        if any(mid not in round_state.cohort_completed for mid in members):
            return campaign_vocabulary_for(family).registry_role
        return "selection"
    if round_state.selection is not None:
        return "selection"
    if round_state.reflections:
        return "editing"
    return "grouping"


def _live_measurements(
    snapshot: CampaignSnapshot, state: RoundState | None, family: int
) -> set[str]:
    """Only current bindings whose work can still continue in this round."""
    if snapshot.finished is not None or state is None:
        return set()
    if not state.baseline_complete:
        return {state.baseline_measurement_id} if state.baseline_measurement_id else set()
    if state.closed is not None:
        return set()
    members = _current_members(state, family)
    if members:
        return {mid for mid in members if mid not in state.cohort_completed}
    open_ids = {a.attempt_id for a in state.attempts.values() if a.outcome is None}
    # Follow rebindings from journaled origin, never by parsing id suffixes.
    superseded = {entry.get("rebound_from") for entry in snapshot.measurements.values()}
    return {
        mid
        for mid, entry in snapshot.measurements.items()
        if entry.get("round") == state.round
        and entry.get("attempt_id") in open_ids
        and mid not in superseded
    }


def _measurement_progress_line(
    run_dir: Path,
    info: MeasurementInfo,
    role: str,
    records: tuple[Envelope, ...] | None = None,
) -> str | None:
    """One incomplete measurement's progress line, or `None` when its pairs
    are all scored (the registry's `complete` flag only tracks the baseline
    and winner completions, so the journal's own counts decide here)."""

    journal = run_dir / "measurements" / info.id / "events.jsonl"
    prefix = f"{info.id} · {role}" if role and role != info.id else info.id
    if records is None:
        if not journal.exists():
            return f"{prefix} · not started"
        records = inspect_journal(journal.parent).records
    if not records:
        return f"{prefix} · not started"
    identity = records[0].payload["identity"]
    scheduled = len(identity.get("schedule", ()))
    scored, retried, outcomes = _progress_from_tries(list(tries(records, identity)))
    if scored >= scheduled:
        return None
    return f"{prefix} · {_progress_text(scored, scheduled, retried, outcomes)}"


@dataclass
class _Progress:
    """Streaming per-measurement progress counts (decision 17)."""

    scheduled: int | None = None
    scored: set[tuple[str, str]] = field(default_factory=set)
    retried: int = 0
    pair_outcomes: dict[tuple[str, str], str] = field(default_factory=dict)
    prefix: str = ""
    completion_emitted: bool = False
    emitted: tuple[Any, ...] | None = None
    emitted_at: float | None = None

    def state(self) -> tuple[Any, ...]:
        outcomes: dict[str, int] = {}
        for outcome in self.pair_outcomes.values():
            if outcome != "completed":
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return (len(self.scored), self.scheduled, self.retried, tuple(sorted(outcomes.items())))


def last_protocol_category(run_dir: Path, attempt_id: str) -> str | None:
    """Read only the last closed category from a regular, non-symlink log."""

    if not attempt_id or Path(attempt_id).name != attempt_id or attempt_id in {".", ".."}:
        return None
    category: str | None = None
    try:
        directory = os.open(run_dir / "editor-logs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                f"{attempt_id}.jsonl",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            for line in stream:
                try:
                    entry = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(entry, dict) and entry.get("phase") in (
                    "protocol_repair_requested",
                    "plan_correction_requested",
                ):
                    value = entry.get("category")
                    category = (
                        value
                        if isinstance(value, str) and value in EDITOR_PROTOCOL_CATEGORIES
                        else None
                    )
    except OSError:
        return None
    return category


class Renderer:
    """Turn new journal records into watch lines (decision 17).

    `kind`/`family` come from `run_reader.open_run` (family `None` for an
    evaluation run). `clock` is a monotonic-second callable used for the
    progress rate limit and the idle schedule; tests inject a fake. Lines are
    prefixed with the record's local time. The renderer is pure: it keeps
    only counts, ids and codes — never task data (decision 22).
    """

    def __init__(
        self,
        kind: RunKind,
        family: int | None,
        clock: Callable[[], float],
        *,
        editor_log_category: Callable[[str], str | None] | None = None,
    ) -> None:
        self._kind = kind
        self._family = family
        self._clock = clock
        self._editor_log_category = editor_log_category
        self._progress: dict[str, _Progress] = {}
        self._measurement_rounds: dict[str, int] = {}
        self._attempt_rounds: dict[str, int] = {}
        self._baseline_ids: set[str] = set()
        self._last_record_at: float | None = None
        self._last_event = ""
        self._last_event_time = ""
        self._next_idle_at: float | None = None

    def feed(self, record: Envelope, *, measurement_id: str | None = None) -> list[str]:
        """Zero or more prefixed lines for one new journal record.

        `measurement_id` names the S1 measurement journal the record was read
        from (the S1 records carry no measurement id of their own); campaign
        and evaluation journal records pass none.
        """

        body = record.to_payload()
        stamp = _local_time(body["timestamp"])
        now = self._clock()
        self._last_record_at = now
        self._last_event = record.event
        self._last_event_time = stamp
        self._next_idle_at = now + IDLE_FIRST_SECONDS
        payload = body["payload"]
        prefix = f"{stamp}  "
        if measurement_id is not None:
            return self._feed_measurement(measurement_id, record.event, payload, prefix)
        if self._kind is RunKind.EVALUATION:
            return self._feed_evaluation(record.event, payload, prefix)
        lines = self._feed_campaign(record.event, payload, prefix)
        if self._family == 15:
            if record.event in {"campaign_created", "identity_rebased"}:
                from evolution.tree.execution.check_modes import parse_checks

                checks = payload.get("adaptation_payload", {}).get("checks")
                modes = () if checks is None else parse_checks(checks).non_enforce()
                if modes:
                    lines.append(
                        prefix + "checks: " + ", ".join(f"{name}={mode}" for name, mode in modes)
                    )
            entries = [payload] if record.event == "check_shadowed" else payload.get("shadowed", ())
            for entry in entries:
                line = (
                    f"check shadowed: {entry['check']} · {entry['code']} · count {entry['count']}"
                )
                number = payload.get(
                    "round", self._attempt_rounds.get(payload.get("attempt_id", ""))
                )
                if number is not None:
                    line += f" · round {number}"
                if entry.get("leak") is not None:
                    line += f" · case {entry['leak']['case_id']}"
                lines.append(prefix + line)
        return lines

    def idle(self) -> list[str]:
        """The idle lines due at the clock's current reading (zero or more)."""

        if self._next_idle_at is None or self._last_record_at is None:
            return []
        now = self._clock()
        lines = []
        while now >= self._next_idle_at:
            elapsed = int(self._next_idle_at - self._last_record_at)
            lines.append(
                f"no new records for {_humanize(elapsed)} "
                f"(last: {self._last_event} at {self._last_event_time})"
            )
            self._next_idle_at += IDLE_EVERY_SECONDS
        return lines

    def tick(self) -> list[str]:
        """Flush pending progress after its rate window, without a new record.

        Lines carry the last progress event's local timestamp. Idle lines keep
        their separate unprefixed contract for the follower's wall clock.
        """
        return [
            line
            for measurement_id, progress in self._progress.items()
            if progress.scheduled is not None and progress.prefix
            for line in self._emit(measurement_id, progress, progress.prefix, force=False)
        ]

    def mark_start(self) -> None:
        """Discard pending historical progress after a silent initial replay."""
        for progress in self._progress.values():
            progress.emitted = progress.state()
            progress.emitted_at = self._clock()

    # -- measurement journals ----------------------------------------------

    def _feed_measurement(
        self, measurement_id: str, event: str, payload: Mapping[str, Any], prefix: str
    ) -> list[str]:
        progress = self._progress.setdefault(measurement_id, _Progress())
        changed = False
        if event == "run_created":
            schedule = (payload.get("identity") or {}).get("schedule", ())
            progress.scheduled = len(schedule)
            return []
        if "case_id" not in payload or "sample_id" not in payload:
            return []
        pair = (str(payload["case_id"]), str(payload["sample_id"]))
        if event == "rollout_started":
            progress.scored.discard(pair)
            progress.pair_outcomes.pop(pair, None)
            progress.completion_emitted = False
            if int(payload.get("retry_ordinal", 0)) > 0:
                progress.retried += 1
                changed = True
        elif event in ("rollout_finished", "rollout_failed", "rollout_interrupted"):
            outcome = str(payload["outcome"])
            progress.pair_outcomes[pair] = outcome
            changed = outcome != "completed"
        elif event in ("scoring_finished", "scoring_failed", "scoring_interrupted"):
            if event == "scoring_finished":
                progress.scored.add(pair)
            else:
                progress.scored.discard(pair)
                progress.pair_outcomes[pair] = (
                    "scorer_failed" if event == "scoring_failed" else "interrupted"
                )
            changed = True
        elif event == "sample_imported":
            progress.scored.add(pair)
            progress.pair_outcomes[pair] = str(payload["outcome"])
            changed = True
        if not changed or progress.scheduled is None:
            return []
        progress.prefix = prefix
        complete = len(progress.scored) >= progress.scheduled
        lines = self._emit(measurement_id, progress, prefix, force=complete)
        if complete and lines:
            progress.completion_emitted = True
        return lines

    def _emit(
        self, measurement_id: str, progress: _Progress, prefix: str, *, force: bool
    ) -> list[str]:
        state = progress.state()
        if not force and state == progress.emitted:
            return []
        now = self._clock()
        if (
            not force
            and progress.emitted_at is not None
            and now - progress.emitted_at < PROGRESS_INTERVAL_SECONDS
        ):
            return []
        progress.emitted = state
        progress.emitted_at = now
        scored, scheduled, retried = state[0], state[1], state[2]
        return [
            f"{prefix}{measurement_id} · "
            f"{_progress_text(scored, int(scheduled), retried, dict(state[3]))}"
        ]

    def _flush_progress(self, measurement_id: str, prefix: str) -> list[str]:
        """The completion line: printed even inside the rate-limit window."""

        progress = self._progress.get(measurement_id)
        if progress is None or progress.scheduled is None or progress.completion_emitted:
            return []
        lines = self._emit(measurement_id, progress, prefix, force=True)
        progress.completion_emitted = True
        return lines

    # -- the campaign journal ------------------------------------------------

    def _generic_line(self, event: str, payload: Mapping[str, Any]) -> str:
        round_no = payload.get("round")
        ident = payload.get("attempt_id") or payload.get("attempt") or payload.get("measurement_id")
        if round_no is None and ident is not None:
            round_no = self._attempt_rounds.get(str(ident))
            if round_no is None:
                round_no = self._measurement_rounds.get(str(ident))
        text = f"probe {payload['status']}" if event == "probe_checked" else event
        if event == "stage_opened" and "scope" in payload:
            text += f" ({payload['scope']})"
        if ident is not None:
            text += f" {ident}"
        if round_no is not None:
            text = f"round {round_no} · {text}"
        return text

    def _note_binding(self, payload: Mapping[str, Any]) -> str:
        measurement_id = str(payload["measurement_id"])
        if payload.get("round") is not None:
            self._measurement_rounds[measurement_id] = int(payload["round"])
        return measurement_id

    def _feed_campaign(self, event: str, payload: Mapping[str, Any], prefix: str) -> list[str]:
        if event == "check_shadowed" and self._family == 15:
            return []
        if payload.get("measurement_id") is not None and payload.get("round") is not None:
            self._measurement_rounds[str(payload["measurement_id"])] = int(payload["round"])
        line: str | None = None
        if event == "campaign_created":
            line = _campaign_created_line(payload)
        elif event == "baseline_bound":
            measurement_id = self._note_binding(payload)
            self._baseline_ids.add(measurement_id)
            line = f"round {payload['round']} · baseline {measurement_id} bound"
        elif event == "baseline_refresh_bound":
            measurement_id = self._note_binding(payload)
            line = f"round {payload['round']} · refresh {measurement_id} bound"
        elif event == "measurement_bound":
            measurement_id = self._note_binding(payload)
            if measurement_id in self._baseline_ids:
                return []  # `baseline_bound` already announced this binding
            if payload.get("attempt_id") == "champion":
                line = f"{measurement_id} · champion measurement bound"
            else:
                line = f"{measurement_id} · candidate measurement bound"
        elif event == "parent_measurement_bound":
            measurement_id = self._note_binding(payload)
            line = f"{measurement_id} · parent measurement bound"
        elif event == "grouping_started":
            kind = payload.get("grouper_kind")
            middle = f" ({kind})" if kind else ""
            line = f"round {payload['round']} · grouping{middle} started"
        elif event == "grouping_finished":
            assignments = (payload.get("result") or {}).get("assignments") or ()
            groups = {index for index in assignments if index is not None}
            line = f"round {payload['round']} · grouping finished · {len(groups)} groups"
            if "fallback" in payload:
                fallback = payload["fallback"]
                reason = fallback.get("reason") if isinstance(fallback, Mapping) else fallback
                line += " · no fallback" if reason is None else f" · fallback ({reason})"
        elif event == "reflection_started":
            line = f"round {payload['round']} · reflection {payload['reflection_id']} started"
        elif event == "reflection_finished":
            line = (
                f"round {payload['round']} · reflection {payload['reflection_id']} "
                f"finished · {payload['status']}"
            )
        elif event == "reflection_failed":
            line = (
                f"round {payload['round']} · reflection {payload['reflection_id']} "
                f"failed · {payload['error_class']}"
            )
            detail = payload.get("detail")
            if self._family == 15 and isinstance(detail, Mapping):
                category = detail.get("category")
                path = detail.get("path")
                if (
                    isinstance(category, str)
                    and category in REFLECTION_DETAIL_CATEGORIES
                    and (
                        "path" not in detail
                        or isinstance(path, str)
                        and REFLECTION_DETAIL_PATH.fullmatch(path)
                    )
                ):
                    line += f" · {category}"
                    if path is not None:
                        line += f" at {path}"
        elif event == "attempt_opened":
            attempt_id = str(payload["attempt_id"])
            self._attempt_rounds[attempt_id] = int(payload["round"])
            line = f"{attempt_id} · attempt opened"
        elif event == "editor_started":
            line = f"{payload['attempt_id']} · editor started"
        elif event == "editor_finished":
            line = f"{payload['attempt_id']} · editor finished · {payload['status']}"
        elif event == "editor_failed":
            line = f"{payload['attempt_id']} · editor failed · {payload['error_class']}"
            if self._family == 15 and self._editor_log_category is not None:
                attempt_id = str(payload["attempt_id"])
                category = self._editor_log_category(attempt_id)
                path = f"editor-logs/{attempt_id}.jsonl"
                if isinstance(category, str) and category in EDITOR_PROTOCOL_CATEGORIES:
                    line += f" · last protocol error: {category} ({path})"
                else:
                    line += f" · log {path}"
        elif event == "attempt_sealed":
            line = f"{payload['attempt_id']} · sealed"
        elif event == "instance_attested":
            line = f"{payload['measurement_id']} · instance attested"
        elif event == _COMPARISON_LOOK:
            if self._family == 14:
                line = _comparison_look_line(payload, self._family)
            else:
                line = self._generic_line(event, payload)
        elif event == "attempt_closed":
            line = f"{payload['attempt_id']} · attempt closed · {payload['outcome']}"
            if payload.get("reason") is not None:
                line += f" ({payload['reason']})"
        elif event == _SELECTION_DECIDED:
            line = _selection_line(payload)
        elif event == "round_closed":
            line = f"round {payload['round']} closed · {payload['outcome']}"
            if payload.get("reason") is not None:
                line += f" · {payload['reason']}"
        elif event == "finalists_bound":
            members = payload.get("members") or ()
            line = f"round {payload['round']} · finalists bound · {len(members)} members"
        elif event == "global_comparison_completed":
            key = (
                campaign_vocabulary_for(self._family).contrast_key
                if self._family == 14
                else "accepted"
            )
            verdict = (
                key if payload.get(key) else ("rejected" if key == "accepted" else f"not {key}")
            )
            line = f"{payload['attempt_id']} · global comparison · {payload['reason']} → {verdict}"
        elif event == "campaign_suspended":
            line = f"suspended: {payload['reason']} ({payload['owner']}) → {_RESUME_EVOLVE}"
        elif event == "campaign_finished":
            line = f"campaign finished · {payload['outcome']}"
            if payload.get("reason") is not None:
                line += f" · {payload['reason']}"
        elif event in _COMPLETION_FLUSH_EVENTS:
            return self._flush_progress(str(payload["measurement_id"]), prefix)
        elif self._family == 14:
            vocabulary = campaign_vocabulary_for(self._family)
            if event == vocabulary.measurement_bound:
                measurement_id = self._note_binding(payload)
                line = (
                    f"{measurement_id} · {vocabulary.registry_role} measurement bound "
                    f"· {payload['role']}"
                )
            elif event == vocabulary.measurement_completed:
                return self._flush_progress(str(payload["measurement_id"]), prefix)
            else:
                line = self._generic_line(event, payload)
        else:
            line = self._generic_line(event, payload)
        if self._family == 15 and event in {
            "reflection_failed",
            "grouping_failed",
            "attempt_closed",
        }:
            leak = payload.get("leak")
            if isinstance(leak, Mapping) and isinstance(leak.get("case_id"), str):
                line += f" · case {leak['case_id']}"
        return [f"{prefix}{line}"]

    # -- the evaluation journal ----------------------------------------------

    def _feed_evaluation(self, event: str, payload: Mapping[str, Any], prefix: str) -> list[str]:
        if event == "evaluation_completed":
            return [*self._flush_progress(MEASUREMENT_ID, prefix), f"{prefix}evaluation completed"]
        if event == "evaluation_created":
            line = _evaluation_created_line(payload)
        elif event == "measurement_bound":
            line = "evaluation · measurement bound"
        elif event == "instance_attested":
            line = "evaluation · instance attested"
        elif event == "evaluation_suspended":
            line = f"suspended: {payload['reason']} → {_RESUME_EVALUATE}"
        else:
            line = event
        return [f"{prefix}{line}"]


@dataclass(frozen=True)
class Snapshot:
    """The `--once` view and the `--progress` line (spec §4.3).

    Built from `run_reader` state by `from_state`; every field renders a
    fact an operator would otherwise assemble with `inspect_campaign`, without
    verification and without task data.
    """

    header: str | None
    round: int | None
    stage: str | None
    open_attempts: tuple[tuple[str, str], ...] = ()
    incomplete: tuple[str, ...] = ()
    last_decision: str | None = None
    suspension_count: int = 0
    last_suspension: str | None = None
    finished: str | None = None
    status: Literal["running", "suspended", "finished"] = "running"
    independent_lines: tuple[str, ...] = ()

    @classmethod
    def from_state(cls, run_dir: str | Path) -> Snapshot:
        """Reduce one open run directory read-only through `run_reader`."""

        handle = open_run(run_dir)
        return cls.from_handle(handle)

    @classmethod
    def from_handle(
        cls,
        handle: RunHandle,
        *,
        measurement_records: Mapping[str, tuple[Envelope, ...]] | None = None,
    ) -> Snapshot:
        """Build from admitted journal prefixes without reopening measurements."""
        if handle.kind is RunKind.EVALUATION:
            return cls._from_evaluation(handle, measurement_records)
        return cls._from_campaign(handle, measurement_records)

    @classmethod
    def _from_campaign(
        cls,
        handle: RunHandle,
        measurement_records: Mapping[str, tuple[Envelope, ...]] | None = None,
    ) -> Snapshot:
        snapshot = handle.snapshot
        if not isinstance(snapshot, CampaignSnapshot):
            raise TypeError("campaign snapshot required")
        family = handle.family or 0
        records = handle.records
        created = next((record for record in records if record.event == "campaign_created"), None)
        header = (
            f"{_local_time(created.to_payload()['timestamp'])}  "
            f"{_campaign_created_line(created.payload)}"
            if created is not None
            else None
        )
        current = snapshot.current_round()
        round_no = (
            current if current is not None else (max(snapshot.rounds) if snapshot.rounds else None)
        )
        round_state = snapshot.rounds.get(round_no) if round_no is not None else None
        open_attempts = (
            tuple(
                (attempt.attempt_id, _attempt_stage(attempt, snapshot.measurements))
                for attempt in snapshot.open_attempts(round_no)
            )
            if round_no is not None
            else ()
        )
        last_decision = None
        for record in reversed(records):
            if record.event in (_COMPARISON_LOOK, _SELECTION_DECIDED):
                last_decision = _decision_text(record.event, record.payload, family)
                break
        last_suspension = None
        if snapshot.suspensions:
            last = snapshot.suspensions[-1]
            last_suspension = f"{last['reason']} ({last['owner']})"
        finished = None
        if snapshot.finished is not None:
            finished = str(snapshot.finished["outcome"])
            if snapshot.finished.get("reason") is not None:
                finished += f" ({snapshot.finished['reason']})"
        live = _live_measurements(snapshot, round_state, family)
        incomplete = [
            line
            for info in measurement_infos(snapshot)
            if info.id in live
            for line in [
                _measurement_progress_line(
                    handle.run_dir,
                    info,
                    info.role or "",
                    measurement_records.get(info.id, ())
                    if measurement_records is not None
                    else None,
                )
            ]
            if line is not None
        ]
        independent_lines: tuple[str, ...] = ()
        if family == 15:
            from evolution.tree.campaign_projections import inspect_view
            from evolution.tree.family15_report import campaign15_projection, report_lines

            preflights = {}
            try:
                inspected = inspect_view(handle.run_dir)
                if inspected.snapshot == snapshot:
                    preflights = dict(inspected.global_preflights)
            except (OSError, JournalBoundaryError, ValueError):
                pass  # The primary admitted prefix remains useful to watch.
            report = campaign15_projection(
                snapshot,
                run_root=handle.run_dir,
                preflights=preflights,
                campaign_records=handle.records,
                measurement_records=measurement_records,
            )
            independent_lines = tuple(report_lines(report))
        return cls(
            independent_lines=independent_lines,
            header=header,
            round=round_no,
            stage=_round_stage(snapshot, round_state, family),
            open_attempts=open_attempts,
            incomplete=tuple(incomplete),
            last_decision=last_decision,
            suspension_count=len(snapshot.suspensions),
            last_suspension=last_suspension,
            finished=finished,
            status="finished"
            if finished is not None
            else (
                "suspended" if records and records[-1].event == "campaign_suspended" else "running"
            ),
        )

    @classmethod
    def _from_evaluation(
        cls,
        handle: RunHandle,
        measurement_records: Mapping[str, tuple[Envelope, ...]] | None = None,
    ) -> Snapshot:
        log_snapshot = handle.snapshot
        if isinstance(log_snapshot, CampaignSnapshot):
            raise TypeError("evaluation snapshot required")
        records = list(log_snapshot.records)
        created = next((record for record in records if record.event == "evaluation_created"), None)
        header = (
            f"{_local_time(created.to_payload()['timestamp'])}  "
            f"{_evaluation_created_line(created.payload)}"
            if created is not None
            else None
        )
        suspensions = [record for record in records if record.event == "evaluation_suspended"]
        finished = (
            "completed"
            if any(record.event == "evaluation_completed" for record in records)
            else None
        )
        incomplete = [
            line
            for info in measurement_infos(handle.snapshot)
            if finished is None
            for line in [
                _measurement_progress_line(
                    handle.run_dir,
                    info,
                    "",
                    measurement_records.get(info.id, ())
                    if measurement_records is not None
                    else None,
                )
            ]
            if line is not None
        ]
        return cls(
            header=header,
            round=None,
            stage=None,
            incomplete=tuple(incomplete),
            suspension_count=len(suspensions),
            last_suspension=(str(suspensions[-1].payload["reason"]) if suspensions else None),
            finished=finished,
            status="finished"
            if finished is not None
            else (
                "suspended"
                if records and records[-1].event == "evaluation_suspended"
                else "running"
            ),
        )

    def _status_text(self, *, one_line: bool = False) -> str:
        if self.finished is not None:
            return f"finished{' ' if one_line else ': '}{self.finished}"
        if self.status == "suspended":
            return f"suspended: {self.last_suspension} · waiting for a resume"
        return "running"

    def lines(self) -> list[str]:
        """The multi-line `--once` rendering: header, then the state."""

        if self.header is None:
            return []
        out = [self.header]
        if self.round is not None:
            head = f"round {self.round}"
            if self.stage is not None:
                head += f" · {self.stage}"
            out.append(head)
            open_text = ", ".join(f"{attempt} ({stage})" for attempt, stage in self.open_attempts)
            out.append(f"open attempts: {open_text or 'none'}")
        out.extend(self.independent_lines)
        out.extend(self.incomplete)
        if self.last_decision is not None:
            out.append(f"last decision: {self.last_decision}")
        suspensions = f"suspensions: {self.suspension_count}"
        if self.last_suspension is not None:
            suspensions += f" · last: {self.last_suspension}"
        out.append(suspensions)
        out.append(self._status_text())
        return out

    def snapshot_line(self) -> str | None:
        """The one-line form `--progress` prints; `None` for a new empty run."""

        if self.header is None:
            return None
        parts: list[str] = []
        if self.round is not None:
            parts.append(f"round {self.round}")
        if self.stage is not None:
            parts.append(self.stage)
        if self.open_attempts:
            parts.append("open " + ", ".join(f"{a} ({s})" for a, s in self.open_attempts))
        parts.extend(self.independent_lines)
        parts.extend(self.incomplete)
        if self.last_decision is not None:
            parts.append(f"last: {self.last_decision}")
        if self.suspension_count:
            parts.append(f"suspended {self.suspension_count}×")
        parts.append(self._status_text(one_line=True))
        return " · ".join(parts)


def ending_code(record: Envelope) -> int | None:
    """The evolve/evaluate exit code of an ending, or None for other events."""
    if record.event == "campaign_finished":
        return _EXIT_FOR_OUTCOME.get(str(record.payload.get("outcome")), 1)
    if record.event in ("campaign_suspended", "evaluation_suspended"):
        return 1
    if record.event == "evaluation_completed":
        return 0
    return None


def _family15_ending(snapshot: CampaignSnapshot, *, suspended: bool = True) -> int | None:
    if snapshot.finished is not None:
        return _EXIT_FOR_OUTCOME.get(str(snapshot.finished["outcome"]), 1)
    return 1 if suspended and snapshot.suspended else None


def _wall_time() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def _prefix_records(
    records: tuple[Envelope, ...], head: int, directory: Path, name: str
) -> tuple[Envelope, ...]:
    """Select whole admitted records through an invocation's byte head."""
    directory_fd = _directory(directory)
    try:
        fd = _file(directory_fd, name, os.O_RDONLY)
        try:
            # Encoded Envelope lengths need not equal journal byte lengths:
            # admitted JSON may include whitespace or a different key order.
            count = _read(fd, head).count(b"\n")
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    return records[:count]


class WatchSession:
    """One reusable polling session for watch and the progress controller.

    Construction silently replays history and retains open read-only tails.
    `snapshot` and `heads` describe that start; `poll()` yields only later
    lines and their first ending's exit code. `close()` releases descriptors.
    A controller can construct before invoking the engine, buffer poll output
    until admission's durable append, then drain with poll before joining.
    `records` exposes the admitted primary-journal prefix (empty before
    birth). A controller compares record counts/events and measurement byte
    heads to recognize activity without parsing human lines; in particular,
    evaluation_created alone is not evaluation admission.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], str] = _wall_time,
        start_heads: Mapping[str, int] | None = None,
        allow_unborn: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.wall = wall
        self._clock = clock
        self._unborn = allow_unborn and not any(
            (self.run_dir / name).exists() or (self.run_dir / name).is_symlink()
            for name in (CAMPAIGN_FILE, EVALUATION_FILE)
        )
        self.tails: dict[str, JournalTail] = {}
        self.records: list[Envelope] = []
        self._pending: list[Envelope] = []
        self._measurement_pending: dict[str, tuple[Envelope, ...]] = {}
        self._measurement_history: dict[str, tuple[Envelope, ...]] = {}
        self.snapshot = Snapshot(None, None, None)
        self.initial_ending: int | None = None
        self._ending_code: int | None = None
        if self._unborn:
            return
        try:
            handle = open_run(self.run_dir)
            self.kind, self.family = handle.kind, handle.family
            self.renderer = Renderer(
                self.kind,
                self.family,
                clock,
                editor_log_category=partial(last_protocol_category, self.run_dir),
            )
            if self.kind is RunKind.CAMPAIGN:
                self.journal = CAMPAIGN_FILE
                self.state: CampaignLifecycle | EvaluationState = CampaignLifecycle(
                    selector=None, family_version=self.family or 0
                )
                tail = JournalTail(
                    self.run_dir,
                    self.journal,
                    family=campaign_family_for(self.family or 0),
                    state=self.state,
                    max_record_bytes=MAX_RECORD_BYTES,
                    max_log_bytes=MAX_JOURNAL_BYTES,
                )
            else:
                self.journal = EVALUATION_FILE
                self.state = EvaluationState()
                tail = JournalTail(
                    self.run_dir,
                    self.journal,
                    family=EVALUATION_FAMILY,
                    state=self.state,
                    max_record_bytes=EVALUATION_MAX_RECORD_BYTES,
                    max_log_bytes=EVALUATION_MAX_LOG_BYTES,
                )
            self.tails[self.journal] = tail
            admitted = tail.poll()
            if not admitted:
                # Select the family afresh once the creation line is whole.
                # Keeping the fallback family for a half-line would reject a
                # current-family creation when its remaining bytes arrive.
                self.close()
                self.tails.clear()
                self._unborn = True
                return
            history = (
                admitted
                if start_heads is None
                else _prefix_records(
                    admitted,
                    min(start_heads.get(self.journal, 0), tail.position),
                    self.run_dir,
                    self.journal,
                )
            )
            self.records = list(history)
            self._pending.extend(admitted[len(history) :])
            historical = self._handle(history)
            for record in history:
                self.renderer.feed(record)
            self._read_measurements(historical, start_heads=start_heads, history=True)
            self.renderer.mark_start()
            self.snapshot = Snapshot.from_handle(
                historical,
                measurement_records=self._measurement_history,
            )
            self.initial_ending = (
                ending_code(history[-1])
                if history and history[-1].event in ("campaign_finished", "evaluation_completed")
                else None
            )
            if self.family == 15 and isinstance(historical.snapshot, CampaignSnapshot):
                self.initial_ending = _family15_ending(historical.snapshot, suspended=False)
        except BaseException:
            self.close()
            raise

    @property
    def heads(self) -> dict[str, int]:
        """Consumed byte heads, suitable for a later session's start_heads."""
        return {name: tail.position for name, tail in self.tails.items()}

    @property
    def observed_byte_heads(self) -> dict[str, int]:
        """Verified physical sizes for first-byte progress admission only."""
        return {name: tail.observed_size for name, tail in self.tails.items()}

    def _handle(self, records: tuple[Envelope, ...]) -> RunHandle:
        snapshot = (
            reduce(records, selector=None, family_version=self.family or 0)
            if self.kind is RunKind.CAMPAIGN
            else LogSnapshot(
                records, "observed", "", sum(len(r.encode()) for r in records), True, "complete"
            )
        )
        return RunHandle(self.kind, self.family, self.run_dir, snapshot, records)

    def _read_measurements(
        self,
        handle: RunHandle,
        *,
        start_heads: Mapping[str, int] | None = None,
        history: bool = False,
    ) -> list[str]:
        lines = []
        for info in measurement_infos(handle.snapshot):
            name = f"measurements/{info.id}/events.jsonl"
            tail = self.tails.get(name)
            if tail is None:
                path = self.run_dir / name
                if not path.exists() and not path.is_symlink():
                    continue
                tail = JournalTail(
                    path.parent,
                    path.name,
                    family=S1_FAMILY,
                    state=Lifecycle(),
                    max_record_bytes=MAX_RECORD_BYTES,
                    max_log_bytes=MAX_JOURNAL_BYTES,
                )
                self.tails[name] = tail
            records = (*self._measurement_pending.pop(name, ()), *tail.poll())
            if history and start_heads is not None:
                prefix = _prefix_records(
                    records,
                    min(start_heads.get(name, 0), tail.position),
                    self.run_dir / name.rsplit("/", 1)[0],
                    "events.jsonl",
                )
                self._measurement_pending[name] = records[len(prefix) :]
                records = prefix
            if history or self.family == 15:
                self._measurement_history[info.id] = (
                    *self._measurement_history.get(info.id, ()),
                    *records,
                )
            for record in records:
                rendered = self.renderer.feed(record, measurement_id=info.id)
                if not history:
                    lines.extend(rendered)
        return lines

    def poll(self) -> tuple[list[str], int | None]:
        """Read one poll, flush rate-limited progress, and prefix idle lines."""
        if self._ending_code is not None:
            return [], self._ending_code
        if self._unborn:
            if not any(
                (self.run_dir / name).exists() or (self.run_dir / name).is_symlink()
                for name in (CAMPAIGN_FILE, EVALUATION_FILE)
            ):
                return [], None
            # An unborn invocation has no history: its first records must be
            # returned to the controller, which owns admission gating.
            opened = WatchSession(self.run_dir, clock=self._clock, wall=self.wall, start_heads={})
            self.__dict__.update(opened.__dict__)
            if self._unborn:
                return [], None
        # Kind markers remain the only directory entries we inspect.
        if (self.run_dir / CAMPAIGN_FILE).exists() and (self.run_dir / EVALUATION_FILE).exists():
            raise RunRefused("run_dir_ambiguous")
        records = [*self._pending, *self.tails[self.journal].poll()]
        self._pending.clear()
        terminal = None
        for index, record in enumerate(records):
            if self.family == 15:
                break  # Family15 endings belong to the complete current batch.
            if ending_code(record) is not None:
                terminal = record
                records = records[: index + 1]
                break
        self.records.extend(records)
        lines = []
        for record in records:
            if record is terminal:
                break
            lines.extend(self.renderer.feed(record))
        current = (
            RunHandle(
                self.kind, self.family, self.run_dir, self.state.snapshot, tuple(self.records)
            )
            # The tail admits a whole batch before rendering. Once this
            # batch contains an ending, later bindings in its reducer state
            # belong to a resumed invocation, outside this follow session.
            if terminal is None and isinstance(self.state, CampaignLifecycle)
            else self._handle(tuple(self.records))
        )
        previous_heads = self.heads if self.family == 15 else {}
        lines.extend(self._read_measurements(current))
        lines.extend(self.renderer.tick())
        if self.family == 15 and isinstance(current.snapshot, CampaignSnapshot):
            if records or self.heads != previous_heads:
                updated = Snapshot.from_handle(
                    current, measurement_records=self._measurement_history
                )
                if updated.independent_lines != self.snapshot.independent_lines:
                    lines.extend(f"{self.wall()}  {line}" for line in updated.independent_lines)
                self.snapshot = updated
            code = _family15_ending(
                current.snapshot,
                suspended=any(record.event == "campaign_suspended" for record in records),
            )
            if code is not None:
                self._ending_code = code
                return lines, code
        if terminal is not None:
            lines.extend(self.renderer.feed(terminal))
            self._ending_code = ending_code(terminal)
            return lines, self._ending_code
        lines.extend(f"{self.wall()}  {line}" for line in self.renderer.idle())
        return lines, None

    def close(self) -> None:
        for tail in self.tails.values():
            tail.close()


class ProgressController:
    """Follow one CLI invocation without changing its admission or outcome.

    WatchSession owns the shared reader and renderer. This controller owns
    admission gating and a daemon's lifetime: an evaluation birth alone is
    buffered, refusals discard that buffer, and normal returns drain before
    the CLI writes its document. Reader/output errors disable presentation;
    the engine remains the authority on the command's result.
    """

    def __init__(self, run_dir: str | Path, *, say: Callable[[str], Any]) -> None:
        self._say = say
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session: WatchSession | None = None
        self._buffer: list[str] = []
        self._active = False
        self._closed = False
        self._failed = False
        try:
            self._session = WatchSession(run_dir, allow_unborn=True)
            self._snapshot = self._session.snapshot.snapshot_line()
            self._byte_heads = self._session.observed_byte_heads
            self._record_count = len(self._session.records)
            self._thread = threading.Thread(target=self._run, name="evo-tree-progress", daemon=True)
            self._thread.start()
        except Exception:
            # Invalid paths and evidence are the engine's own refusal/fault
            # to report. Progress must neither pre-empt nor replace it.
            self._failed = True
            self._close()

    def _close(self) -> None:
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            self._failed = True

    def _has_append(self) -> bool:
        assert self._session is not None
        session = self._session
        if any(
            head > self._byte_heads.get(name, 0)
            for name, head in session.observed_byte_heads.items()
            if name == CAMPAIGN_FILE or name.startswith("measurements/")
        ):
            return True
        new_records = session.records[self._record_count :]
        return any(record.event != "evaluation_created" for record in new_records)

    def _flush(self) -> None:
        if not self._active:
            self._active = True
            if self._snapshot is not None:
                self._say(f"{_wall_time()}  {self._snapshot}")
        for line in self._buffer:
            self._say(line)
        self._buffer.clear()

    def _poll(self) -> int | None:
        assert self._session is not None
        lines, ending = self._session.poll()
        self._buffer.extend(lines)
        if self._active or self._has_append():
            self._flush()
        return ending

    def _run(self) -> None:
        try:
            assert self._session is not None
            if self._session.initial_ending is not None:
                return
            while not self._stop.is_set():
                if self._poll() is not None:
                    return
                self._stop.wait(1.0)
        except Exception:
            self._failed = True

    def _join(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()

    def finish(self) -> None:
        """Stop, join and drain before any final CLI output."""
        if self._closed:
            return
        self._join()
        try:
            if self._session is not None and not self._failed:
                self._poll()
                self._flush()
        except Exception:
            self._failed = True
        finally:
            self.discard()

    def discard(self) -> None:
        """Stop silently on refusal or an escaping engine exception."""
        if self._closed:
            return
        self._join()
        self._buffer.clear()
        self._close()
        self._closed = True


def follow(
    run_dir: str | Path,
    *,
    emit: Callable[[str], Any],
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], str] = _wall_time,
    poll_seconds: float = 1.0,
    start_heads: Mapping[str, int] | None = None,
    mode: Literal["watch", "progress"] = "watch",
    once: bool = False,
) -> int:
    """Print the start snapshot and follow only records after its heads.

    Safety refusals and journal faults propagate for the CLI's path-free
    diagnostics. Only Ctrl-C is handled here (130). No lock or write occurs.
    `mode=progress` uses the one-line snapshot; Task 7 owns admission gating
    and stop/drain via WatchSession.
    """
    session = None
    try:
        path = Path(run_dir)
        if not path.is_dir():
            raise RunRefused("not_a_run_directory")
        try:
            directory_fd = _directory(path)
            os.close(directory_fd)
        except (JournalBoundaryError, OSError):
            raise RunRefused("not_a_run_directory") from None
        waiting = False
        while session is None:
            if not any(
                (path / name).exists() or (path / name).is_symlink()
                for name in (CAMPAIGN_FILE, EVALUATION_FILE)
            ):
                if not waiting:
                    emit(f"{wall()}  waiting for the campaign journal")
                    waiting = True
                if once:
                    return 0
                sleep(poll_seconds)
                continue
            try:
                session = WatchSession(path, clock=clock, wall=wall, start_heads=start_heads)
            except JournalBoundaryError as exc:
                if exc.code == "journal_unavailable":
                    raise RunRefused("not_a_run_directory") from None
                raise
        if mode == "watch":
            for line in session.snapshot.lines():
                # Snapshot header already carries its creation timestamp.
                emit(line if len(line) > 8 and line[2] == ":" else f"{wall()}  {line}")
        else:
            snapshot = session.snapshot.snapshot_line()
            if snapshot is not None:
                emit(f"{wall()}  {snapshot}")
        if once:
            return 0
        if session.initial_ending is not None:
            return session.initial_ending
        while True:
            lines, code = session.poll()
            for line in lines:
                emit(line)
            if code is not None:
                return code
            sleep(poll_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        if session is not None:
            session.close()
