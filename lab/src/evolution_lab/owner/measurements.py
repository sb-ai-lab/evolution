"""Measurement views from core's public journals (spec 2026-09-29 §2.4)."""
from __future__ import annotations

import threading
from copy import deepcopy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any

USAGE_COVERAGE = "target_calls_only"


@dataclass(frozen=True, slots=True)
class JournalFacts:
    first_at: str | None
    last_at: str | None
    last_sequence: int
    usage: dict | None


def usage_of_events(events: Iterable[Mapping[str, Any]]) -> dict:
    """Target-call tokens only; a finished call without numbers counts as unknown."""
    tokens_in = tokens_out = unknown = 0
    for event in events:
        if event.get("event") != "target_send_finished":
            continue
        usage = (event.get("payload") or {}).get("usage")
        if not isinstance(usage, Mapping) or usage.get("unknown") is not False:
            unknown += 1
            continue
        tokens_in += usage["input_tokens"]
        tokens_out += usage["output_tokens"]
    return {"input_tokens": tokens_in, "output_tokens": tokens_out, "unknown_calls": unknown,
            "coverage": USAGE_COVERAGE}


def read_journal_facts(directory: Path) -> JournalFacts:
    from evolution.tree import inspect_journal, project_events

    events = project_events(inspect_journal(Path(directory))).to_payload()["events"]
    if not events:
        return JournalFacts(None, None, 0, usage_of_events(()))
    return JournalFacts(events[0]["timestamp"], events[-1]["timestamp"], events[-1]["sequence"],
                        usage_of_events(events))


class MeasurementCache:
    """In-memory facts per ``(operation_id, run_id, measurement_id, last_sequence)``.

    Core measurement ids (``m-r01-a01``) are local to a campaign and repeat between
    operations, hence the operation id in the key. A closed measurement keeps its
    ``last_sequence`` and is never read again; deleting an operation's results drops it."""

    def __init__(self) -> None:
        self._rows: dict[tuple, JournalFacts] = {}
        self._lock = threading.Lock()
        self._notices: dict[str, tuple] = {}
        self._scorer_components: dict[tuple, tuple] = {}

    def scorer_component(self, key, signature_of, loader):
        """Retain only fully read evidence, with nofollow inputs checked around IO.

        A count is assembled anew from these components, including failed reads;
        no unavailable aggregate or exception is retained (ADR-0029).
        """
        signature = signature_of()
        with self._lock:
            cached = self._scorer_components.get(key)
        if signature is not None and cached is not None and cached[0] == signature:
            return deepcopy(cached[1])
        with self._lock:
            self._scorer_components.pop(key, None)
        value = loader()
        if signature is not None and signature == signature_of():
            with self._lock:
                self._scorer_components[key] = (signature, deepcopy(value))
        return value

    def get(self, key: tuple, loader: Callable[[], JournalFacts]) -> JournalFacts:
        with self._lock:
            hit = self._rows.get(key)
        if hit is not None:
            return hit
        value = loader()
        with self._lock:
            stale = [k for k in self._rows if k[:3] == key[:3] and k != key]
            for k in stale:
                del self._rows[k]
            self._rows[key] = value
        return value

    def notices(self, operation_id, key, loader):
        """Only the aggregate is retained; an incomplete read never replaces evidence."""
        with self._lock:
            cached = self._notices.get(operation_id) if key is not None else None
        if cached is not None and cached[0] == key:
            return deepcopy(cached[1])
        value, stable = loader()
        with self._lock:
            self._notices.pop(operation_id, None)
            if key is not None and stable and value["lab_notices_state"] == "present":
                self._notices[operation_id] = (key, deepcopy(value))
        return value

    def drop(self, operation_id: str) -> None:
        with self._lock:
            self._notices.pop(operation_id, None)
            for k in [k for k in self._scorer_components if k[0] == operation_id]:
                del self._scorer_components[k]
            for k in [k for k in self._rows if k[0] == operation_id]:
                del self._rows[k]


_COMPARISONS = ("better", "same", "worse", "unpaired")


def metric_means(samples: Iterable[Mapping]) -> dict[str, dict]:
    """``{name: {"mean", "count"}}`` over scored samples; the report and the views share it."""
    values: dict[str, list[float]] = {}
    for sample in samples:
        if not isinstance(sample, Mapping) or sample.get("scoring_state") != "finished":
            continue
        for key, value in (sample.get("metrics") or {}).items():
            values.setdefault(key, []).append(value)
    return {key: {"mean": fmean(v), "count": len(v)} for key, v in sorted(values.items())}


def _scored(samples) -> dict[tuple[str, str], Mapping]:
    return {(s["case_id"], s["sample_id"]): s for s in samples
            if isinstance(s, Mapping) and s.get("scoring_state") == "finished"}


def _roles(core: Mapping) -> dict[str, tuple]:
    """measurement id -> (role, round, attempt_id, baseline measurement id)."""
    roles: dict[str, tuple] = {}
    for row in core.get("rounds") or ():
        base = row.get("baseline_measurement_id")
        if base is not None:
            roles.setdefault(base, ("baseline", row.get("round"), None, None))
        for attempt in row.get("attempts") or ():
            mid = attempt.get("measurement_id")
            if mid is not None:
                roles[mid] = ("attempt", row.get("round"), attempt.get("attempt_id"), base)
    return roles


def _compare(value, base) -> str:
    if value is None or base is None:
        return "unknown"
    return "better" if value > base else "worse" if value < base else "same"


def _baseline(entry, base_entry, base_id, metric, samples):
    """(baseline section, per-pair verdicts); Δ is a view, never the attempt's outcome."""
    if base_id is None or base_entry is None or not isinstance(base_entry.get("status"), Mapping):
        return ({"measurement_id": base_id, "state": "unavailable", "delta": None, "cases": None}
                if base_id is not None else None), {}
    excluded = entry.get("excluded_pairs") or {}
    if excluded.get("truncated") is True:
        # Core listed only part of the excluded pairs: no pairing is trustworthy.
        return {"measurement_id": base_id, "state": "exclusions_truncated", "delta": None, "cases": None}, None
    skip = {(p["case_id"], p["sample_id"]) for p in excluded.get("pairs") or ()}
    mine, theirs = _scored(samples), _scored(base_entry["status"].get("samples") or ())
    cases = dict.fromkeys(_COMPARISONS, 0)
    deltas: list[float] = []
    per_sample: dict[tuple, str] = {}
    for key, sample in mine.items():
        if key in skip:
            continue
        partner = theirs.get(key)
        mine_v = (sample.get("metrics") or {}).get(metric)
        if partner is None:
            per_sample[key] = "unpaired"
            cases["unpaired"] += 1
            continue
        base_v = (partner.get("metrics") or {}).get(metric)
        verdict = _compare(mine_v, base_v)
        per_sample[key] = verdict
        if verdict == "unknown":
            cases["unpaired"] += 1
            continue
        cases[verdict] += 1
        deltas.append(mine_v - base_v)
    delta = {metric: {"mean": fmean(deltas), "pairs": len(deltas)}} if deltas else {}
    return {"measurement_id": base_id, "state": "present", "delta": delta, "cases": cases}, per_sample


def _usage(facts: JournalFacts | None) -> tuple[dict | None, str]:
    if facts is None or facts.usage is None:
        return None, "unavailable"
    return dict(facts.usage), "present"


def _repairs(entry: Mapping, kind: str) -> dict | None:
    """Core's repair counters of one measurement (core a24, #78): starts of repair tries, cumulative over
    resumes and of both kinds (fault-row and threshold), and distinct repaired pairs whose latest row is
    scored. Informational only; ``None`` when core omits either key, and for a fixed evaluation."""
    tries, scored = entry.get("repair_tries"), entry.get("repairs_scored")
    if kind == "evaluation" or not isinstance(tries, int) or not isinstance(scored, int):
        return None
    return {"tries": tries, "pairs_scored": scored}


def _scorer_component(cache, key, signature_of, loader):
    return loader() if cache is None else cache.scorer_component(key, signature_of, loader)


def _scoring_journal(journal, cache, key):
    from . import core_raw
    from .projection_signature import input_signature
    return _scorer_component(cache, (*key, 'journal', str(journal)),
        lambda: input_signature(journal),
        lambda: core_raw.scoring_index(journal))


def _scorer_verdict(root, request, case_id, measurement_id, cache, key):
    from . import core_raw
    from .sample_view import _scorer_output
    def load():
        trace = core_raw.scorer_trace(root, request, case_id, measurement_id=measurement_id, require_output=True)
        output = _scorer_output(trace['stdout'])
        if trace['output_reason'] is not None or output is None:
            raise core_raw.CoreRawUnavailable('unreadable')
        verdicts = output.get('verdicts') or ()
        # No scorer input/output, note or criterion text survives.
        answered = tuple(v['passed'] for v in verdicts if v['criterion'] == 'answered')
        return answered, bool(verdicts)
    return _scorer_component(cache, (*key, 'trace', str(root), request, case_id),
        lambda: core_raw.scorer_trace_signature(root, request, measurement_id=measurement_id), load)


def evaluation_focus_notices(core: Mapping, root: Path, journal: Path, *, terminal: bool = True,
                             cache=None, operation_id=None) -> dict:
    """Journal admission/critical_pass plus the same bounded scorer output as lab sample.

    No scorer content is exposed or retained: only a code, counters and read state.
    Check every admitted sample even after finding a signal, so unknown stays explicit.
    """
    from . import core_raw

    result = {"lab_notices": [], "lab_notices_state": "present"}
    admitted = list(_scored(core.get("samples") or ()).values())
    if not terminal or not admitted:
        return result
    key = (operation_id, core['run_id'])
    try:
        scoring_by_sample = _scoring_journal(journal, cache, key)
    except Exception:
        return {"lab_notices": [], "lab_notices_state": "unavailable"}
    signal = False
    unavailable = False
    for sample in admitted:
        critical = sample.get("critical_pass")
        if "critical_pass" not in sample or (critical is not None and type(critical) is not bool):
            unavailable = True
        signal |= type(critical) is bool
        try:
            scoring = core_raw.scoring_from_index(scoring_by_sample, sample['case_id'], sample['sample_id'],
                                                  require_critical=True)
            if scoring["state"] != "finished" or "critical_pass" not in scoring \
                    or scoring["critical_pass"] is not critical:
                unavailable = True
                continue
            _, has_verdicts = _scorer_verdict(root, scoring['score_request_id'], sample['case_id'],
                core['run_id'], cache, key)
            signal |= has_verdicts
        except core_raw.CoreRawUnavailable:
            unavailable = True
    if unavailable:
        result["lab_notices_state"] = "unavailable"
    elif not signal:
        result["lab_notices"] = [{"code": "scorer_without_focus_signal", "admitted": len(admitted),
            "not_completed": sum((sample.get("execution") or {}).get("outcome") != "completed"
                                 for sample in core.get("samples") or ())}]
    return result


def unanswered_count(samples, root: Path, journal: Path, measurement_id: str, *,
                     cache=None, operation_id=None) -> dict:
    """Count only admitted samples using bounded, authenticated scorer output."""
    from . import core_raw
    key = (operation_id, measurement_id)

    unavailable = {"unanswered": None, "unanswered_state": "unavailable", "answered_reported": None}
    admitted = list(_scored(samples).values())
    try:
        scoring_by_sample = _scoring_journal(journal, cache, key) if admitted else {}
    except Exception:
        return unavailable
    reported = missing = 0
    for sample in admitted:
        try:
            scoring = core_raw.scoring_from_index(scoring_by_sample, sample['case_id'], sample['sample_id'])
            if scoring['state'] != 'finished':
                return unavailable
            request = scoring['score_request_id']
            answered, _ = _scorer_verdict(root, request, sample['case_id'], measurement_id, cache, key)
            if len(answered) > 1:
                return unavailable
            if answered:
                reported += 1
                missing += answered[0] is False
        except Exception:
            return unavailable
    if not reported:
        return {"unanswered": None, "unanswered_state": "not_reported", "answered_reported": 0}
    return {"unanswered": missing, "unanswered_state": "present" if reported == len(admitted) else "partial",
            "answered_reported": reported}


def measurement_rows(core: Mapping, kind: str, facts: Mapping[str, JournalFacts | None],
                     metric_primary: str | None, *, detail: str | None = None,
                     notices: Mapping | None = None, unanswered: Mapping | None = None) -> list[dict]:
    """Summaries of a result's measurements, or the one ``detail`` measurement with samples."""
    if kind == "evaluation":
        entries = [{"measurement_id": core["run_id"], "candidate_digest": core.get("candidate_digest"),
                    "status": {"samples": core.get("samples") or []}, "excluded_pairs": None}]
        roles = {core["run_id"]: ("evaluation", None, None, None)}
    else:
        entries = [e for e in core.get("measurements") or () if isinstance(e, Mapping)]
        roles = _roles(core)
    by_id = {e["measurement_id"]: e for e in entries}
    rows = []
    for entry in entries:
        mid = entry["measurement_id"]
        if detail is not None and mid != detail:
            continue
        role, round_, attempt_id, base_id = roles.get(mid, ("other", None, None, None))
        samples = (entry.get("status") or {}).get("samples") or []
        if role != "attempt" or core.get("family_version") == 15:
            # Family 15: Core decides on its own weighted estimate against a fresh or stored gate reference, so a Lab
            # delta against the round baseline would be a second comparison beside Core's (anti-goal).
            baseline, per_sample = None, {}
        elif metric_primary is None:  # policy unread: no primary metric, so no honest delta
            baseline = ({"measurement_id": base_id, "state": "unavailable", "delta": None, "cases": None}
                        if base_id is not None else None)
            per_sample = {}
        else:
            baseline, per_sample = _baseline(entry, by_id.get(base_id), base_id, metric_primary, samples)
        excluded = entry.get("excluded_pairs") or {}
        journal = facts.get(mid)
        usage, usage_state = _usage(journal)
        row = {
            "measurement_id": mid, "role": role, "round": round_, "attempt_id": attempt_id,
            "candidate_digest": entry.get("candidate_digest"),
            "coverage": {"expected": len(samples), "scored": len(_scored(samples)),
                         "excluded_pairs": excluded.get("count", 0)},
            "metrics": metric_means(samples), "baseline": baseline,
            "times": None if journal is None or journal.first_at is None else
                     {"started_at": journal.first_at, "last_event_at": journal.last_at},
            "usage": usage, "usage_state": usage_state,
            "retries": entry.get("call_retries"), "call_failures_by_code": entry.get("call_failures_by_code"),
            "classification": entry.get("classification"), "repairs": _repairs(entry, kind),
            **((unanswered or {}).get(mid) or {"unanswered": None, "unanswered_state": "unavailable",
                                                   "answered_reported": None}),
            **(notices if kind == "evaluation" and notices is not None else
               {"lab_notices": [], "lab_notices_state": "present"}),
        }
        if detail is not None:
            base_samples = _scored(((by_id.get(base_id) or {}).get("status") or {}).get("samples") or []) \
                if role == "attempt" and core.get("family_version") != 15 else {}
            row["samples"] = [{
                "case_id": s["case_id"], "sample_id": s["sample_id"],
                "execution_state": s.get("execution_state"), "scoring_state": s.get("scoring_state"),
                "metrics": s.get("metrics"), "critical_pass": s.get("critical_pass"), "tries": s.get("tries"),
                "baseline": None if (s["case_id"], s["sample_id"]) not in base_samples else {
                    "metrics": base_samples[(s["case_id"], s["sample_id"])].get("metrics"),
                    "critical_pass": base_samples[(s["case_id"], s["sample_id"])].get("critical_pass")},
                "comparison": "unknown" if per_sample is None else
                              per_sample.get((s["case_id"], s["sample_id"]), "unknown"),
            } for s in samples if isinstance(s, Mapping)]
        rows.append(row)
    return rows
