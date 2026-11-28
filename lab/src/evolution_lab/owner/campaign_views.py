"""``lab-campaign-view/15`` from admitted core projections and ``core_raw`` details (spec 2026-10-19 D4).

Family 14 keeps its prior Core content; both families add the shared Owner reflector-probe triplet (dev62).
Family 15 carries the ``independent`` block
(``independent_view``) instead of the family-14 gate mode, confidence and final blocks. Lab copies core's decisions,
confidence and measurement mappings without recomputing them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

ATTEMPT_GROUP: Mapping[str, str] = {
    "critical_failure": "agent_worse", "no_evaluable_pairs": "not_enough_data",
    "no_evaluable_focus": "not_enough_data",
    "incomplete_sample": "infrastructure", "scorer_metric_missing": "infrastructure",
    "infrastructure_failed": "infrastructure",
    "editor_declined": "no_candidate", "editor_no_change": "no_candidate",
    "editor_failed": "stopped", "editor_unresponsive": "stopped", "seal_refused": "stopped",
    "changed_file_budget": "stopped", "written_byte_budget": "stopped",
    "interrupted": "stopped", "cancelled": "stopped", "condition_changed": "stopped",
    "gate_passed": "accepted", "observe_finalist": "accepted",
    "causal_below_tolerance": "agent_worse",
    "insufficient_repeats": "not_enough_data",
    "insufficient_calibration": "not_enough_data",
    "no_exam_impact": "no_candidate",
}
GROUPS = ("accepted", "agent_worse", "not_enough_data", "infrastructure", "no_candidate", "stopped",
          "in_progress", "unknown")

#: Where an attempt that never reached a decision stopped, by core's closing reason (spec §2.4).
ATTEMPT_STAGE: Mapping[str, tuple[str, str | None]] = {
    "editor_failed": ("editor", "editor"), "editor_unresponsive": ("editor", "editor"),
    "changed_file_budget": ("editor", "editor"), "written_byte_budget": ("editor", "editor"),
    "editor_declined": ("editor", "editor"), "editor_no_change": ("editor", "editor"),
    "seal_refused": ("seal", None),
    "incomplete_sample": ("measurement", None), "scorer_metric_missing": ("measurement", None),
    "infrastructure_failed": ("measurement", None), "no_evaluable_pairs": ("measurement", None),
    "no_evaluable_focus": ("measurement", None), "no_exam_impact": ("measurement", None),
    "insufficient_calibration": ("measurement", None), "insufficient_repeats": ("measurement", None),
    "interrupted": ("lifecycle", None), "cancelled": ("lifecycle", None),
    "condition_changed": ("lifecycle", None),
    "focus_below_margin": ("measurement", None),
}
#: Reasons whose last Editor terminal is ``editor_failed``; a decline or no-change ends in ``editor_finished``.
_EDITOR_FAILURES = frozenset({"editor_failed", "editor_unresponsive"})
_HYPOTHESIS_SENTENCES = ("counterevidence", "regression_risks", "why_this_layer")
#: Core's terminal reflection statuses (journal `status`) and the state each gives; a new one in core must
#: reach this map on a re-pin (test against ``core_raw.reflection_terminal_statuses``).
REFLECTION_STATE: Mapping[str, str] = {"reflected": "present", "declined": "declined", "failed": "failed"}


def failure_of(outcome: str | None, reason: str | None, raw: Mapping | None) -> dict | None:
    """``failure`` of a closed attempt that ended ``abandoned`` or ``insufficient_evidence``, else ``None``."""
    if outcome not in {"abandoned", "insufficient_evidence"}:
        return None
    stage, role = ATTEMPT_STAGE.get(reason, ("unknown", None))
    raw = raw or {}
    editor = reason in _EDITOR_FAILURES
    return {"stage": stage, "role": role,
            "error_class": raw.get("editor_error") if editor else None,
            "role_reason": raw.get("editor_reason") if editor else None,
            "code": raw.get("code")}


def _hypothesis(index: int, group: Mapping) -> dict:
    return {"index": index, "root_cause": group["root_cause"], "suggested_fix": group["suggested_fix"],
            "affected_cases": list(group.get("affected_cases") or ()),
            "target_paths": list(group.get("target_paths") or ()),
            **{name: group.get(name) for name in _HYPOTHESIS_SENTENCES}}


def reflection_of(raw: Mapping | None) -> dict:
    """The round's reflection by core's counted entry, else by its last entry (spec §2.3)."""
    counted = (raw or {}).get("diagnosis")
    last = (raw or {}).get("last_reflection")
    empty = {"reflection_id": None, "error_class": None, "reason": None, "hypotheses": []}
    if counted is not None:
        state = REFLECTION_STATE[counted["status"]]
        failed = state == "failed"
        return {"state": state, "reflection_id": counted["reflection_id"],
                "error_class": counted["error_class"] if failed else None,
                "reason": counted["reason"] if failed else None,
                "hypotheses": [_hypothesis(i, g) for i, g in enumerate(counted["groups"], start=1)]
                              if state == "present" else []}
    if last is None:
        return {"state": "absent", **empty}
    if last["status"] == "started":
        return {"state": "in_progress", **empty, "reflection_id": last["reflection_id"]}
    return {"state": "unsettled", **empty, "reflection_id": last["reflection_id"],
            "error_class": last["error_class"], "reason": last["reason"]}


def details_unavailable(code: str) -> dict:
    return {"journal": "unavailable", "reason": code}


#: A family-14 finalist's group by its final decision (spec 2026-10-13 §6.3): core's final contrast decides.
DECISION_GROUP: Mapping[str, str] = {"accepted": "accepted", "rejected": "agent_worse", "pending": "in_progress",
                                     "unavailable": "not_enough_data"}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _margin_group(block: Mapping | None) -> str:
    """``focus_below_margin`` (family 14 uses one reason on the early and the final look, spec §6.3): an early look
    rejects only when even the upper bound is at or under the margin — agent worse; on the final look a point above
    the margin is positive but unconfirmed — not enough data; at or under it — agent worse. Without core's look,
    final flag, point or margin — unknown."""
    try:
        last = block["evaluated"][-1]
        final, point, margin = last["final"], last["focus"]["point"], block["margin"]
    except (KeyError, IndexError, TypeError):
        return "unknown"
    if not isinstance(final, bool) or not _number(point) or not _number(margin):
        return "unknown"
    if not final:
        return "agent_worse"
    return "not_enough_data" if point > margin else "agent_worse"


def campaign_finished(core) -> bool:
    """Whether Core recorded the campaign's terminal: its ``complete`` (``snapshot.finished is not None``,
    campaign_projections.py:1982, :2117) or its ``campaign_outcome`` (``_outcome``, :1829-1831, null until then).
    Recorded state only; anything unreadable is not finished."""
    if not isinstance(core, Mapping):
        return False
    return core.get("complete") is True or isinstance(core.get("campaign_outcome"), Mapping)


def attempt_group(outcome: str | None, reason: str | None, *,
                  decision: Mapping | None = None, confidence: Mapping | None = None,
                  campaign_finished: bool = False) -> str:
    """A finalist follows Core's final decision; an unresolved attempt is unknown once Core finished."""
    if outcome is None:
        return "unknown" if campaign_finished else "in_progress"
    if outcome == "finalist":
        # A gate finalist is not accepted without core's final decision.
        return DECISION_GROUP.get((decision or {}).get("state"), "unknown")
    if reason == "focus_below_margin":
        return _margin_group(confidence)
    return ATTEMPT_GROUP.get(reason, "unknown")


def final_decisions(core: Mapping) -> dict[str, dict]:
    """``{attempt_id: {state, reason}}`` for every family-14 finalist, from core's own final contrasts
    (``rounds[].final[].contrasts[]`` matched by ``attempt_id``; a later entry of the round wins). ``accepted`` —
    core's ``accepted: true``; ``rejected`` — ``false`` with core's reason; ``unavailable`` — the contrast's reason is
    ``final_incomplete`` or core gave no verdict, the campaign is suspended ``final_incomplete``, or it ended without a
    contrast (reason: the round's); ``pending`` — not measured yet while the campaign runs. Indexes core's blocks
    directly: a key this pin does not have raises."""
    rounds = [r for r in core.get("rounds") or () if isinstance(r, Mapping)]
    terminal = campaign_finished(core)
    suspensions = core.get("suspensions") or ()
    final_paused = core.get("suspended") is True and bool(suspensions) \
        and suspensions[-1].get("reason") == "final_incomplete"
    out: dict[str, dict] = {}
    for row in rounds:
        contrasts: dict[str, Mapping] = {}
        for entry in row["final"]:
            for contrast in entry["contrasts"]:
                contrasts[contrast["attempt_id"]] = contrast
        for attempt in row["attempts"]:
            if attempt["outcome"] != "finalist":
                continue
            contrast = contrasts.get(attempt["attempt_id"])
            if contrast is not None:
                if contrast["accepted"] is True:
                    state, reason = "accepted", contrast["reason"]
                elif contrast["accepted"] is False and contrast["reason"] != "final_incomplete":
                    state, reason = "rejected", contrast["reason"]
                else:
                    state, reason = "unavailable", contrast["reason"] or contrast.get("unavailable")
            elif final_paused:
                state, reason = "unavailable", "final_incomplete"
            elif terminal or row.get("outcome") is not None:
                # The final did not take place (spec §6.3): the campaign or this round is closed without a contrast.
                state, reason = "unavailable", row.get("reason")
            else:
                state, reason = "pending", None
            out[attempt["attempt_id"]] = {"state": state, "reason": reason}
    return out


#: Groups whose attempt got a decision on a measurement: a candidate was measured (spec dev46 §4.1, PDR-0025).
MEASURED_GROUPS = frozenset({"accepted", "agent_worse", "not_enough_data"})


def lab_notices(outcome: Mapping | None, rounds: Sequence[Mapping],
                reflections: Mapping[int, Mapping | None] | None, *,
                decisions: Mapping[str, Mapping] | None = None, family: int = 14) -> list[dict]:
    """Lab's notices beside core's outcome (PDR-0025): ``no_candidate_measured`` when core has an outcome and no
    attempt of any round was measured. An attempt still running or of a group Lab does not know suppresses it --
    Lab does not claim what it cannot tell. ``rounds`` are core's rounds (they carry the round ``reason``);
    ``reflections`` maps a round number to its ``reflection_of`` and is None when the round details were not read
    (then the notice has no ``reflection_errors``). Codes and counts only, never texts.

    Family 15 groups by ``attempt15_group`` (``decisions`` is family 14's and unused). An attempt closed in preflight
    (``structurally_unattainable``) had no candidate measured (campaign.py:8593-8604: the preflight fails before
    ``comparison_bound``), so it counts as not measured although its group is ``not_enough_data``."""
    from .independent_view import PREFLIGHT_CLOSES, attempt15_group

    if not isinstance(outcome, Mapping):
        return []
    by_reason: dict[str, int] = {}
    errors: dict[str, int] = {}
    for row in rounds:
        attempts = list(row.get("attempts") or ())
        for attempt in attempts:
            if family == 15:
                group = attempt15_group(attempt.get("outcome"), attempt.get("reason"))
                measured = group in MEASURED_GROUPS and attempt.get("reason") not in PREFLIGHT_CLOSES
            else:
                group = attempt_group(attempt.get("outcome"), attempt.get("reason"),
                                      decision=(decisions or {}).get(attempt.get("attempt_id")),
                                      confidence=attempt.get("confidence"))
                measured = group in MEASURED_GROUPS
            if measured or group in {"in_progress", "unknown"}:
                return []
            key = attempt.get("reason") or "unknown"
            by_reason[key] = by_reason.get(key, 0) + 1
        if not attempts:
            key = row.get("reason") or row.get("outcome") or "unknown"
            by_reason[key] = by_reason.get(key, 0) + 1
        reflection = (reflections or {}).get(row.get("round"))
        if isinstance(reflection, Mapping) and reflection.get("state") in {"failed", "unsettled"}:
            key = reflection.get("error_class") or reflection.get("reason") or "unknown"
            errors[key] = errors.get(key, 0) + 1
    notice = {"code": "no_candidate_measured", "rounds": len(rounds), "by_reason": dict(sorted(by_reason.items()))}
    if reflections is not None:
        notice["reflection_errors"] = dict(sorted(errors.items()))
    return [notice]


OBSERVE_CAVEAT = "observe_gate_not_enforced"


def _caveat_items(core: Mapping) -> list:
    """Core's required family-14 ``caveats`` list; unreadable data never means no caveats."""
    items = core.get("caveats")
    if not isinstance(items, list):
        raise ValueError("caveats of a family-14 projection are not a list")
    return items


def gate_mode_of(core: Mapping) -> str:
    """Core's campaign-wide observe caveat determines observe; otherwise the gate is statistical."""
    if core.get("family_version") == 15:
        mode = core["independent"]["settings"]["mode"]
        if mode not in {"point", "confidence", "unfiltered", "statistical"}:
            raise ValueError("gate mode not described by this pin")
        return mode
    observe = any(isinstance(item, Mapping) and item.get("kind") == OBSERVE_CAVEAT and item.get("round") is None
                  for item in _caveat_items(core))
    return "observe" if observe else "statistical"


def caveats_of(core: Mapping) -> list[dict]:
    """Core's result caveats as ``{kind, round, attempt_id}``; campaign and lineage caveats have null scope."""
    return [{"kind": item["kind"], "round": item.get("round"), "attempt_id": item.get("attempt_id")}
            for item in _caveat_items(core)]


def _interval(value: Mapping | None) -> dict | None:
    """A look's estimate with core's ``exact`` (null in observe)."""
    if value is None:
        return None
    return {"point": value["point"], "lower": value["lower"], "upper": value["upper"],
            "exact": value["exact"]}


def confidence_of(block: Mapping | None) -> dict | None:
    """Core's ``attempts[].confidence`` folded (spec 2026-10-09 §6.1, 2026-10-13 §7.1): the comparison's metadata,
    family 14's ``margin``, and ``latest`` — the last evaluated look as core decided it, ``None`` while none was
    evaluated (a legal state, not an adapter failure)."""
    if block is None:
        return None
    evaluated = list(block["evaluated"])
    last = evaluated[-1] if evaluated else None
    return {"mode": block["mode"], "repeats": block["repeats"], "focus_cases": block["focus_cases"],
            "causal_cases": block["causal_cases"], "calibration": block["calibration"],
            "margin": block["margin"],
            "latest": None if last is None else {
                "look": last["look"], "final": last["final"], "decision": last["decision"], "reason": last["reason"],
                "focus": _interval(last["focus"]), "causal": _interval(last["causal"])}}


_FINAL_MEMBER = ("measurement_id", "role", "attempt_id", "complete", "point", "missing", "critical")
_FINAL_CONTRAST = ("attempt_id", "point", "lower", "level", "unavailable", "pairing", "accepted", "reason", "exact",
                   "removed_pairs", "removed_cases", "condition_changed_in_place")


def final_of(entries: Sequence[Mapping] | None) -> list[dict] | None:
    """Core's ``rounds[].final`` of family 14 (spec 2026-10-13 §7.1), entry by entry as core wrote it; ``listed`` is
    the number of unmeasured pairs core lists (the rows themselves are not shown). ``unavailable``,
    ``removed_pairs`` and ``removed_cases`` are per contrast in core and stay there."""
    if entries is None:
        return None
    return [{"ordinal": e["ordinal"], "stability": e["stability"], "champion_side": e["champion_side"],
             "stored_measurement_id": e["stored_measurement_id"], "mode": e["mode"], "global": e["global"],
             "members": [{key: m[key] for key in _FINAL_MEMBER} for m in e["members"]],
             "contrasts": [{key: c[key] for key in _FINAL_CONTRAST} for c in e["contrasts"]],
             "selection": None if e["selection"] is None else {
                 key: e["selection"][key] for key in ("reason", "attempt_id", "measurement_id")},
             "listed": len(e["listed"]),
             "next_baseline_measurement_id": e["next_baseline_measurement_id"],
             "condition_changed_in_place": e["condition_changed_in_place"]} for e in entries]


def _family_14_fields(rounds: list) -> dict:
    """Per public round: ``(final, {attempt_id: confidence})``; indexes core's blocks directly (ADR-0019 p. 3)."""
    return {r["round"]: (final_of(r["final"]), {a["attempt_id"]: confidence_of(a["confidence"])
                                                for a in r["attempts"]})
            for r in rounds}


def _family_14_blocks(core: Mapping) -> dict:
    """Core's ``roles``, ``settings`` and ``connection`` of family 14, as core gave them (spec §7.1)."""
    roles, settings, connection = core["roles"], core["settings"], core["connection"]
    return {"roles": {name: {"kind": roles[name]["kind"]} for name in ("editor", "reflector", "grouper")},
            "settings": {"stability": settings["stability"], "round_stop": settings["round_stop"],
                         "baseline_refresh": settings["baseline_refresh"]},
            "connection": {key: connection[key] for key in
                           ("state", "outcome", "attempts", "binding_digest", "operation_id", "detail")}}


def _measurement_14(champion: Mapping, rounds: list, core: Mapping) -> str | None:
    """Family 14 (spec 2026-10-13 §7.1): the measurement core holds this Champion by. The final champion is core's
    ``final_champion.measurement_id``; the initial one starts at round 1's base, a promoted finalist at its promotion
    round's last ``final`` entry's ``next_baseline_measurement_id``. In each later round of its reign it moves to what
    core made the next base: that round's baseline refresh (``settings.baseline_refresh.performed``), then — later in
    the round — a ``retained`` round's ``next_baseline_measurement_id``. Not placeable — ``None``, not a guess."""
    final = core.get("final_champion")
    if isinstance(final, Mapping) and final.get("version") == champion["version"]:
        return final.get("measurement_id")
    refresh = (core.get("settings") or {}).get("baseline_refresh")
    refreshed = ({p["round"]: p["measurement_id"] for p in refresh.get("performed") or ()}
                 if isinstance(refresh, Mapping) else {})

    def last_entry(row):
        entries = row.get("final") or []
        return entries[-1] if entries else {}

    promotions = {}
    for row in rounds:
        entry = last_entry(row)
        selection = entry.get("selection") or {}
        if row.get("outcome") == "promoted" and selection.get("attempt_id") is not None:
            promotions[selection["attempt_id"]] = (row.get("round"), entry.get("next_baseline_measurement_id"))
    if champion["origin"] == "initial":
        start, measurement = 0, (rounds[0].get("baseline_measurement_id") if rounds else None)
    elif champion["attempt_id"] in promotions:
        start, measurement = promotions[champion["attempt_id"]]
    else:
        return None
    later = [number for number, _ in promotions.values() if number > start]
    end = min(later) if later else None
    for row in rounds:
        number = row.get("round")
        if number <= start or (end is not None and number > end):
            continue
        if number in refreshed:
            measurement = refreshed[number]
        if (end is None or number < end) and row.get("outcome") == "retained":
            measurement = last_entry(row).get("next_baseline_measurement_id") or measurement
    return measurement


def lineage_champions(run: Path) -> tuple[list[dict], frozenset[str]]:
    """Champion chain and the attempts whose candidate tree is on disk (public ``inspect``)."""
    from evolution.tree import ArtifactStore
    lineage = ArtifactStore.inspect(Path(run), verify=False)
    champions = [{"version": c.version_id, "digest": c.candidate_digest,
                  "origin": "initial" if c.attempt_id is None else "promoted_attempt",
                  "attempt_id": c.attempt_id} for c in lineage.champions]
    trees = frozenset(c.attempt_id for c in lineage.candidates
                      if c.reference is not None and c.corruption is None
                      and (Path(run) / c.reference.locator).is_dir())
    return champions, trees


def _detail_fields(rounds: list, raw_rounds: Mapping) -> dict:
    """Per public round: ``(baseline_version, reflection, {attempt_id: failure})`` from core's raw data.

    Indexes raw fields directly; the caller treats any exception as an unreadable journal."""
    fields = {}
    for r in rounds:
        raw_round = raw_rounds.get(r.get("round"))
        raw_attempts = (raw_round or {}).get("attempts", {})
        fields[r.get("round")] = (
            (raw_round or {}).get("baseline_version"), reflection_of(raw_round),
            {a.get("attempt_id"): failure_of(a.get("outcome"), a.get("reason"), raw_attempts.get(a.get("attempt_id")))
             for a in r.get("attempts") or ()})
    return fields


def round_reflections(rounds: list, raw_rounds: Mapping) -> dict:
    """Per public round its ``reflection_of``, built exactly as the view builds its round details, so the view and the
    report count one notice from the same data (spec dev46 §4.2). Raises on raw data the pin does not describe."""
    return {n: fields[1] for n, fields in _detail_fields(rounds, raw_rounds).items()}


#: The reflection fields a report's ``lab_rounds`` keeps: codes only, never the Reflector's texts (spec dev46 §4.2).
REFLECTION_CODES = ("state", "error_class", "reason")


def reflection_codes(reflection: Mapping) -> dict:
    return {name: reflection.get(name) for name in REFLECTION_CODES}


def baseline_import_fields(block: Mapping) -> dict:
    """Present Core's import counts in terms of the measurement still needed."""
    imported = 0 if block["abandoned"] else block["imported"]
    fields = {
        "imported": imported, "to_measure": block["scheduled"] - imported,
        "not_imported": dict(block["not_imported"]), "fields": list(block["fields"]),
        "authorization_id": block["authorization_id"],
        "held_out_authorization_id": block["held_out_authorization_id"],
    }
    if block["abandoned"]:
        fields["abandoned"] = True
    return fields


def baseline_notice_fields(run, snapshot, *, cache=None, read=None):
    """Read the admitted baseline; unreadable evidence is never an empty result."""
    from .core_raw import CoreRawUnavailable, baseline_provenance_state
    try:
        if snapshot is None:
            raise CoreRawUnavailable("unreadable")
        if cache is not None:
            state = cache.read(run, snapshot, read=read)
        else:
            state = baseline_provenance_state(run, snapshot, **({"read": read} if read is not None else {}))
    except CoreRawUnavailable:
        return {"lab_notices": [], "lab_notices_state": "unavailable"}
    notices = []
    if state is not None and state["samples"] > 0 and state["admitted"] > 0 and state["known"] == 0:
        notices.append({"code": "baseline_provenance_unknown", "round": state["round"],
                        "measurement_id": state["measurement_id"], "samples": state["samples"]})
    return {"lab_notices": notices, "lab_notices_state": "present"}


VIEW_SCHEMA = "lab-campaign-view/15"


def campaign_view(operation_id, core, plan, progress, champions, trees, measurements, details, *, reflector_probe=None, baseline=None) -> dict:
    """``champions`` and ``trees`` are ``None`` when the lineage could not be read: then
    ``champions_state`` is ``unavailable``, ``champions`` is null and every ``has_tree`` is null.
    ``details`` is ``core_raw.campaign_details`` or ``details_unavailable(code)``: without it
    ``baseline_version``, ``reflection`` and ``failure`` are null next to ``details_state``; so are they,
    with reason ``unreadable``, when building them from ``details`` raises.

    ``core`` is Core's status payload (``campaign_read(...).status``). Core writes ``family_version: 15`` into it only
    for a family-15 snapshot (campaign_projections.py:2133) and no such key for family 14, so the key is the
    snapshot's family as ``CampaignRead.family`` gave it: 15 is family 15, absent is family 14."""
    # The embedded closed /14 rows predate measurement/list /3 notice fields.
    # Copy them so the standalone projection retains its required /3 fields.
    measurements = [{key: value for key, value in row.items()
                     if key not in {"lab_notices", "lab_notices_state", "unanswered", "unanswered_state",
                                    "answered_reported"}} for row in measurements]
    view = _view_15 if core.get("family_version") == 15 else _view_14
    from evolution_lab._reflector_probe import probe_fields
    result = view(operation_id, core, plan, progress, champions, trees, measurements, details)
    baseline = baseline if baseline is not None else {"lab_notices": [], "lab_notices_state": "unavailable"}
    result["lab_notices"].extend(baseline["lab_notices"])
    result["lab_notices_state"] = baseline["lab_notices_state"]
    return {**result, **probe_fields(reflector_probe)}


def _round_details(rounds: list, details: Mapping) -> tuple[bool, Mapping, dict | None, dict | None]:
    """``(read, details, detail_fields, reflections)`` of the round details, shared by both families."""
    journal = details.get("journal")
    if journal == "missing" and rounds:
        # Core results were kept while the journal is gone (Owner keeps the last projection).
        journal, details = "unavailable", details_unavailable("unreadable")
    read = journal in {"present", "missing"}
    detail_fields = None
    # The notice's reflections depend only on the round details, as in the report (final review I2): taken right
    # after they are read, before an unreadable family block or caveats flips ``read``. None (no
    # reflection_errors) when the round details were not read (PDR-0025).
    reflections = None
    if read:
        try:
            detail_fields = _detail_fields(rounds, details.get("rounds") or {})
            reflections = {n: fields[1] for n, fields in detail_fields.items()}
        except Exception:
            # Raw core data the pin does not describe (ADR-0019 p.3): a state next to null, not a route error.
            read, details = False, details_unavailable("unreadable")
    return read, details, detail_fields, reflections


def _view_14(operation_id, core, plan, progress, champions, trees, measurements, details) -> dict:
    """Family 14: ``/7``'s content and key set, with schema ``/8`` (spec D4: family 14 unchanged)."""
    lineage_read = champions is not None and trees is not None
    rounds = [r for r in core.get("rounds") or () if isinstance(r, Mapping)]
    outcome = core.get("campaign_outcome")
    finished = campaign_finished(core)
    read, details, detail_fields, reflections = _round_details(rounds, details)
    public, gate_mode, champion_of, decisions, blocks = None, None, {}, {}, None
    try:
        # The caveats and the gate mode are read on their own (final review I1): a malformed final or confidence
        # block must not erase the observe signal (spec 2026-10-09 §6.1).
        caveats = caveats_of(core)
        gate_mode = gate_mode_of(core)
    except Exception:
        # Unreadable caveats (ADR-0019 p.3): unreadable details, not a route error; the gate mode is then unknown
        # (null), never inferred as statistical from caveats that could not be read.
        caveats, gate_mode = [], None
        read, details = False, details_unavailable("unreadable")
    try:
        public = _family_14_fields(rounds)
        decisions = final_decisions(core)
        blocks = _family_14_blocks(core)
        if lineage_read:
            champion_of = {c["version"]: _measurement_14(c, rounds, core) for c in champions}
    except Exception:
        # A family-14 block the pin does not describe (ADR-0019 p.3): unreadable details, not a route error; no
        # final, confidence, decision, family-14 block or champion measurement survives. The caveats and
        # gate mode above stay.
        public, champion_of, decisions, blocks = None, {}, {}, None
        read, details = False, details_unavailable("unreadable")
    if lineage_read and any(value is None for value in champion_of.values()):
        # A Champion core's family-14 blocks cannot place (spec 2026-10-13 §7.1): its measurement is null and the
        # details are unreadable; core's readable blocks, final and decisions stay.
        read, details = False, details_unavailable("unreadable")

    def attempt_row(a, round_failures, round_confidence):
        return {"attempt_id": a.get("attempt_id"), "outcome": a.get("outcome"), "reason": a.get("reason"),
                "group": attempt_group(a.get("outcome"), a.get("reason"),
                                       decision=decisions.get(a.get("attempt_id")), confidence=a.get("confidence"),
                                       campaign_finished=finished),
                "look": a.get("look"),
                "measurement_id": a.get("measurement_id"),
                "has_tree": a.get("attempt_id") in trees if lineage_read else None,
                "failure": round_failures.get(a.get("attempt_id")) if read else None,
                "confidence": round_confidence.get(a.get("attempt_id")),
                "final_decision": decisions.get(a.get("attempt_id"))}

    def round_row(r):
        baseline_version, reflection, failures = detail_fields[r.get("round")] if read else (None, None, {})
        block, confidence = public[r.get("round")] if public is not None else (None, {})
        return {"round": r.get("round"), "outcome": r.get("outcome"),
                "baseline_measurement_id": r.get("baseline_measurement_id"),
                "baseline_version": baseline_version, "reflection": reflection,
                "attempts": [attempt_row(a, failures, confidence) for a in r.get("attempts") or ()],
                "final": block}

    notices = lab_notices(outcome, rounds, reflections, decisions=decisions)
    return {
        "schema": VIEW_SCHEMA, "operation_id": operation_id,
        "family": 14, "gate_mode": gate_mode, "caveats": caveats,
        "roles": (blocks or {}).get("roles"), "settings": (blocks or {}).get("settings"),
        "connection": (blocks or {}).get("connection"),
        "policy": {"rounds": plan.rounds, "attempts_per_round": plan.attempts_per_round,
                   "metric_primary": plan.metric_primary},
        "progress": progress,
        "outcome": None if not isinstance(outcome, Mapping) else
                   {"outcome": outcome.get("outcome"), "reason": outcome.get("reason")},
        "champions": [dict(c, measurement_id=champion_of.get(c["version"])) for c in champions]
                     if lineage_read else None,
        "champions_state": "present" if lineage_read else "unavailable",
        "rounds": [round_row(r) for r in rounds],
        "measurements": measurements,
        "suspensions": list(core.get("suspensions") or ()),
        "details_state": "present" if read else "unavailable",
        "details_reason": None if read else details.get("reason"),
        "details_source": "core_raw",
        "lab_notices": notices,
    }


def _champion_measurement_15(champion: Mapping, core: Mapping) -> str | None:
    """Family 15: core's ``final_champion.measurement_id`` for the final champion. Core's family-15 rounds carry no
    ``final`` entries to walk (always ``[]``), so any other champion's measurement is null -- not a guess, and not
    an unreadable state."""
    final = core.get("final_champion")
    if isinstance(final, Mapping) and final.get("version") == champion["version"]:
        return final.get("measurement_id")
    return None


def _view_15(operation_id, core, plan, progress, champions, trees, measurements, details) -> dict:
    """Family 15 (spec D4): no gate mode, round ``final`` or attempt ``look``/``confidence``/``final_decision``
    (Core leaves them empty or null for 15); the ``independent`` block instead. Core's status still carries
    ``caveats``, ``roles``, ``settings`` and ``connection`` for 15 (Task 1 audit), so they stay. A malformed
    ``independent`` block is ``independent: null`` next to unreadable details; malformed caveats or family-14 trio
    are empty/null next to unreadable details with the block kept. Never a route error."""
    from .independent_view import attempt15_group, closed_rounds, independent_view

    lineage_read = champions is not None and trees is not None
    rounds = [r for r in core.get("rounds") or () if isinstance(r, Mapping)]
    outcome = core.get("campaign_outcome")
    finished = campaign_finished(core)   # Core's recorded terminal: nothing of a finished campaign is in progress
    from .core_raw import family15_checks
    try:
        checks = family15_checks(core["independent"])
        checks_state, checks_reason = "present", None
    except Exception:
        checks, checks_state, checks_reason = None, "unavailable", "unreadable"
    binding_details = details
    read, details, detail_fields, reflections = _round_details(rounds, details)
    try:
        caveats = caveats_of(core)
    except Exception:
        caveats = []
        read, details = False, details_unavailable("unreadable")
    try:
        blocks = _family_14_blocks(core)
    except Exception:
        blocks = None
        read, details = False, details_unavailable("unreadable")
    round_details_read = read
    try:
        closed = closed_rounds(rounds)
        if core["independent"]["attempts"] and (binding_details.get("bindings_reason") is not None or
                                               binding_details.get("journal") != "present"):
            raise ValueError("binding publications unreadable")
        independent = independent_view(core["independent"], closed_rounds=closed, campaign_finished=finished,
                                       bindings=binding_details.get("bindings"), stages=binding_details.get("stages"))
    except Exception:
        # A block/rational/binding the pin cannot read is unavailable. Keep the
        # independently admitted round/reflection facts; this failure does not
        # make them unknown (the overall details reason still records the fault).
        independent = None
        read, details = False, details_unavailable("unreadable")
    comparisons = {a["attempt_id"]: a["global_comparison"] for a in (independent or {}).get("attempts") or ()}
    champion_of = ({c["version"]: _champion_measurement_15(c, core) for c in champions} if lineage_read else {})

    def attempt_row(a, round_failures, round_closed):
        return {"attempt_id": a.get("attempt_id"), "outcome": a.get("outcome"), "reason": a.get("reason"),
                "group": attempt15_group(a.get("outcome"), a.get("reason"),
                                         global_comparison=comparisons.get(a.get("attempt_id")),
                                         round_closed=round_closed, campaign_finished=finished),
                "measurement_id": a.get("measurement_id"),
                "has_tree": a.get("attempt_id") in trees if lineage_read else None,
                "failure": round_failures.get(a.get("attempt_id")) if round_details_read else None}

    def round_row(r):
        baseline_version, reflection, failures = detail_fields[r.get("round")] if round_details_read else (None, None, {})
        return {"round": r.get("round"), "outcome": r.get("outcome"),
                "baseline_measurement_id": r.get("baseline_measurement_id"),
                "baseline_version": baseline_version, "reflection": reflection,
                "attempts": [attempt_row(a, failures, r.get("outcome") is not None) for a in r.get("attempts") or ()]}

    return {
        "schema": VIEW_SCHEMA, "operation_id": operation_id, "family": 15, "caveats": caveats,
        "roles": (blocks or {}).get("roles"), "settings": (blocks or {}).get("settings"),
        "connection": (blocks or {}).get("connection"),
        "policy": {"rounds": plan.rounds, "attempts_per_round": plan.attempts_per_round,
                   "metric_primary": plan.metric_primary},
        "progress": progress,
        "outcome": None if not isinstance(outcome, Mapping) else
                   {"outcome": outcome.get("outcome"), "reason": outcome.get("reason")},
        "champions": [dict(c, measurement_id=champion_of.get(c["version"])) for c in champions]
                     if lineage_read else None,
        "champions_state": "present" if lineage_read else "unavailable",
        "rounds": [round_row(r) for r in rounds],
        "measurements": measurements,
        "suspensions": list(core.get("suspensions") or ()),
        "details_state": "present" if read else "unavailable",
        "details_reason": None if read else details.get("reason"),
        "details_source": "core_raw",
        "lab_notices": lab_notices(outcome, rounds, reflections, family=15),
        "independent": independent,
        "checks": checks, "checks_state": checks_state, "checks_reason": checks_reason,
    }
