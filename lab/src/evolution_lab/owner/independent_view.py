"""Family-15 ``independent`` block of ``lab-campaign-view/15`` (dev58 spec §3).

Lab copies what Core recorded and adds three derived words: per comparison its ``layer`` (Q1) and the
``reference`` measurement of the round's champion (Q2 as corrected), per scope its ``display`` (Q5). Every
statistical quantity becomes ``{value, exact}`` through ``exact_numbers.statistical``; counts stay plain integers
and null stays null. Lab never compares a bound with a threshold, averages or recomputes anything: the gate is
Core's (tenet ``one-campaign-authority``). ``enclosure.upper`` is kept for audit and is never a bound.

Core's keys are indexed directly. A key or value this pin does not describe raises, and the caller makes the whole
block unreadable (ADR-0019 p.3). Blocks copied whole must have exactly Core's keys, so nothing unknown can reach the
closed view. Core's ``diagnostics[]`` is not part of D4 and is not projected.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from . import core_raw
from .campaign_views import campaign_finished
from .exact_numbers import statistical
from .focus_stages import STATES, attempt_stages

#: How a scope's statement reads, from that scope's own recorded rule, status and threshold (never the campaign mode).
DISPLAY = ("bound", "point", "bootstrap", "no_threshold", "status")
#: The comparison's layer: ``global`` when Core's ``deciding_layer`` is ``global``, else the ``gate`` (focus/causal).
LAYERS = ("gate", "global")
#: Which measurement of the round's champion the comparison used (family15_report.py:272-277). In family 15
#: the Parent is always the round's champion (family15_lifecycle.py:316-319), so there is no champion/parent word.
REFERENCES = ("stored", "fresh", "unknown")
#: Core's evidence kinds (family15_report.py:1144), equal to ``core_raw.family15_scope_vocabulary()["evidence"]``
#: (a test holds them equal); kept here so the CLI decoder needs no Core import.
EVIDENCE = ("snapshot_only", "unavailable", "authenticated_publications")
_REFERENCE_OF: Mapping[str | None, str] = {"stored_non_contemporaneous": "stored", "fresh": "fresh", None: "unknown"}

# Closed key sets of Lab's block; ``cli.responses`` decodes with the same tuples.
BLOCK_KEYS = ("evidence", "next_action", "settings", "attempts", "attainability", "work")
SETTINGS_KEYS = ("mode", "repeats", "verification", "stability", "fresh_parent", "global_rule", "train_cases",
                 "train_slots", "execution_noise", "statistics", "bound_fallback")
EXECUTION_NOISE_KEYS = ("kind", "allowance", "source", "source_kind")
STATISTICS_KEYS = ("method", "unit")
ATTEMPT_KEYS = ("round", "attempt_id", "outcome", "reason", "group", "eligible_final_score", "comparison",
                "gate_comparison", "global_comparison", "preflight", "conditional_cost", "gate_binding", "global_binding")
BINDING_KEYS = ("source", "scopes")
BINDING_SCOPE_KEYS = ("name", "rule")
COMPARISON_KEYS = ("outcome", "reason", "layer", "deciding_layer", "reference", "reference_kind",
                   "reference_measurement_id", "verification",
                   "eligible", "mode", "confidence_scope", "selection_bias_possible", "excluded_reference_slots",
                   "coverage", "noise", "scopes", "method", "unit", "calibration", "caveats", "bound_fallback")
CALIBRATION_KEYS = ("source", "measurement_id", "prefix", "retained", "method", "level", "seed_input", "allowance",
                    "degenerate", "result_digest")
PREFIX_KEYS = ("version", "kind", "run_id", "locator", "content_digest")
FALLBACK_KEYS = ("reason",)
FALLBACK_REASONS = ("focus_unattainable", "causal_unconfirmable")
COMPARISON_CAVEATS = ("intervals_empirical", "exact_contrast", "noise_allowance_empirical", "noise_not_observed")
COVERAGE_SIDE_KEYS = ("required", "settled", "admissible")
NOISE_KEYS = ("kind", "allowance", "source", "source_kind", "variation", "units", "observed_cases",
              "extra_measurements")
REPEAT_VARIANCE_KEYS = ("minimum", "maximum", "unavailable_counts")
VARIANCE_UNAVAILABLE = ("insufficient_repeats", "incomplete")
ROUND_PROGRESS_KEYS = ("round", "outcome", "reason", "attempts_opened", "candidates_sealed", "attempts_measured", "reflections")
ATTEMPT_PROGRESS_KEYS = ("editor", "editor_failures", "candidate_sealed", "measurement")
ROLE_FAILURE_KEYS = ("category", "reason")
REFLECTION_PROGRESS_KEYS = ("status", "category", "reason", "suspended")
SUSPENSION_KEYS = ("reason", "owner")
SCOPE_KEYS = ("name", "rule", "status", "unavailable", "display", "point", "threshold", "strict", "confidence",
              "enclosure", "declared_count", "unit_count", "mass", "excluded_mass", "fallback", "lower", "exact", "pairing")
ENCLOSURE_KEYS = ("lower", "upper", "steps")
SCOPE_QUANTITIES = ("point", "threshold", "confidence", "mass", "excluded_mass", "lower")
EXPLANATION_KEYS = ("status", "basis", "profile_digest", "interpretation", "scopes", "bound_fallback")
PREFLIGHT_SCOPE_KEYS = ("name", "status", "rule", "threshold", "strict", "unit_count", "passes", "confidence",
                        "delta_max", "c_max", "e_max", "fallback")
PREFLIGHT_QUANTITIES = ("threshold", "confidence", "delta_max", "c_max")
E_MAX_KEYS = ("kind", "value")
ATTAINABILITY_KEYS = ("preflight", "local_guidance")
ROUND_PREFLIGHT_KEYS = ("round", "explanation", "refresh")
WORK_KEYS = ("planned", "committed", "remaining")
# Blocks copied whole; family15_report.py at v2.0.0b11, cc9c19e85fb4 (unchanged since v2.0.0b10; dev59 Task 1 audit).
COST_KEYS = ("basis", "baseline_slots", "candidate_gate_slots", "candidate_final_slots", "candidate_total_slots",
             "parent_slots", "champion_slots", "champion_basis", "slots_per_refresh", "connection_preflight_slots",
             "probes_and_retries", "noise_diagnostic_slots", "simplest_passing_path_slots")             # :951-965
REFRESH_KEYS = ("scenario", "remaining_refreshes", "slots_per_refresh", "scenario_slots", "retry_ceiling_separate",
                "connection_preflight_credit")                                                       # :1223-1234
GUIDANCE_KEYS = ("status", "minimum_units", "assumptions", "interpretation")                         # :1205-1216
PLANNED_KEYS = ("basis", "baseline_slots", "admitted_baseline_imports", "affected_cases",
                "candidate_final_slots_if_gate_passes", "parent_gate_slots", "champion_slots_per_scope",
                "champion_scope", "slots_per_refresh", "connection_preflight", "probes_and_retries",
                "noise_diagnostic_slots")                                                            # :1182-1204
COMMITTED_KEYS = ("measurement", "connection_preflight", "role_requests", "reflector_probe_requests",
                  "interpretation")                                                                  # :1307-1330
COMMITTED_WORK_KEYS = ("scenario_slots", "imported_slots", "target_turns", "transport_retries",
                       "scorer_executions")                                                          # :1323-1330
REMAINING_KEYS = ("basis", "scope", "baseline_slots", "candidate_gate_slots", "parent_gate_slots",
                  "candidate_final_slots_if_gates_pass", "champion_slots_for_reached_finalists",
                  "remaining_refreshes", "refresh_scenario_slots", "probes_and_retries",
                  "noise_diagnostic_slots")                                                          # :1283-1306

#: Family-15 attempt reasons outside family 14's ``ATTEMPT_GROUP``: the comparison reasons
#: (``family15_schema.REASONS``) and the preflight closure ``structurally_unattainable`` (campaign.py:8593-8604).
_REASON15_GROUP: Mapping[str, str] = {
    "threshold_not_passed": "agent_worse", "critical_failed": "agent_worse",
    "empty_support": "not_enough_data", "structurally_unattainable": "not_enough_data",
    "candidate_incomplete": "infrastructure", "reference_incomplete": "infrastructure",
    "provenance_unavailable": "infrastructure", "provenance_failed": "infrastructure",
}
#: An outcome's group when its reason has none. ``finalist``, ``promoted`` and null are decided before this table.
#: ``declined``/``failed``/``promoted``/``retained`` are admitted by the projection (family15_report.py:1242-1253).
_OUTCOME15_GROUP: Mapping[str, str] = {
    "rejected": "agent_worse", "retained": "agent_worse", "abandoned": "stopped", "failed": "stopped",
    "insufficient_evidence": "not_enough_data", "declined": "no_candidate",
}
#: A global comparison's group by its outcome when its reason has none.
_COMPARISON_GROUP: Mapping[str, str] = {"passed": "accepted", "comparison_not_passed": "agent_worse",
                                        "comparison_unavailable": "not_enough_data"}
#: The attempt reasons that close an attempt before its candidate is measured (preflight, campaign.py:8593-8604).
PREFLIGHT_CLOSES = frozenset({"structurally_unattainable"})


def reason_group(reason: str | None) -> str | None:
    """The group an explicit table gives a family-15 attempt reason (family-15 reasons, then family 14's
    ``ATTEMPT_GROUP``), else None. ``attempt15_group`` falls back to the outcome's group at runtime; the vocabulary
    test requires every Core reason except ``passed`` (a finalist's) to be mapped here, so a new one is not read
    through that fallback unnoticed."""
    from .campaign_views import ATTEMPT_GROUP

    if reason is None:
        return None
    return _REASON15_GROUP.get(reason) or ATTEMPT_GROUP.get(reason)


def closed_rounds(rounds) -> frozenset:
    """The numbers of the rounds Core has closed (a round outcome is set): the view's own knowledge of a round's
    state, read from the rounds it already carries. Anything unreadable is no closed round."""
    return frozenset(r.get("round") for r in rounds or () if isinstance(r, Mapping) and r.get("outcome") is not None)


def attempt15_group(outcome: str | None, reason: str | None, *, global_comparison: Mapping | None = None,
                    round_closed: bool = False, campaign_finished: bool = False) -> str:
    """A family-15 attempt's group (``campaign_views.GROUPS``), by outcome first, refined by its reason.

    Null is in progress and ``promoted`` accepted. A ``finalist`` passed the gate: it stays in progress until Core
    records its ``global_comparison``, then takes that comparison's
    group, as a family-14 finalist takes its final decision's. Core cannot close a round before every finalist has a
    global result (family15_lifecycle.py:136-143), so a finalist with no visible global comparison in a closed round
    (``round_closed``) is ``unknown``: Lab cannot see it, and "in progress" would be false. Core also admits
    ``campaign_finished`` ``stopped``/``cancelled`` with the current round open (campaign_schema.py
    ``_check_finished``: only ``completed`` needs every round closed) and reconciles no Attempt for it, so in a
    finished campaign (``campaign_finished``) nothing is in progress: an unresolved finalist or open attempt is
    ``unknown``. A recorded global comparison still gives its group. Otherwise a known reason gives the group
    (family-15 reasons, then family 14's ``ATTEMPT_GROUP``), and the outcome's own group stands in for a reason Lab
    does not know. An outcome Core's projection does not admit is ``unknown``."""
    if outcome is None:
        return "unknown" if campaign_finished else "in_progress"
    if outcome == "promoted":
        return "accepted"
    if outcome == "finalist":
        if global_comparison is None:
            return "unknown" if round_closed or campaign_finished else "in_progress"
        if global_comparison["outcome"] == "passed":
            return "accepted"
        return reason_group(global_comparison["reason"]) or _COMPARISON_GROUP.get(global_comparison["outcome"], "unknown")
    return reason_group(reason) or _OUTCOME15_GROUP.get(outcome, "unknown")


def _vocabulary() -> dict:
    return core_raw.family15_scope_vocabulary()


def _choice(value, allowed) -> object:
    if value not in allowed:
        raise ValueError(f"value not described by this pin: {value!r}")
    return value


def _list(value) -> list:
    if not isinstance(value, list):
        raise ValueError("not a list")
    return value


def _plain(value: Mapping, keys: Sequence[str]) -> dict:
    """A block of counts and codes copied whole: exactly Core's keys, lists copied."""
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise ValueError("block keys not described by this pin")
    return {key: list(value[key]) if isinstance(value[key], list) else value[key] for key in keys}


def _keys(value: Mapping, required: Sequence[str], optional: Sequence[str] = ()) -> None:
    if not isinstance(value, Mapping) or not set(required) <= set(value) <= set(required) | set(optional):
        raise ValueError("block keys not described by this pin")


def _fallback(value: Mapping | None) -> dict | None:
    if value is None:
        return None
    row = _plain(value, FALLBACK_KEYS)
    _choice(row["reason"], FALLBACK_REASONS)
    return row


def binding_row(value: Mapping | None) -> dict | None:
    if value is None:
        return None
    _keys(value, BINDING_KEYS)
    _choice(value["source"], ("core_raw",))
    scopes = []
    for scope in _list(value["scopes"]):
        _keys(scope, BINDING_SCOPE_KEYS)
        _choice(scope["name"], ("focus", "causal", "global"))
        _choice(scope["rule"], ("bound", "point", "off", "bootstrap"))
        scopes.append(dict(scope))
    return {"source": "core_raw", "scopes": scopes}


def _calibration(value: Mapping | None) -> dict | None:
    if value is None:
        return None
    _keys(value, CALIBRATION_KEYS)
    row = {key: value[key] for key in CALIBRATION_KEYS}
    row["prefix"] = _plain(value["prefix"], PREFIX_KEYS)
    for key in ("level", "allowance"):
        row[key] = statistical(value[key])
    return row


def comparison_layer(comparison: Mapping) -> str:
    """``global`` iff Core's ``deciding_layer`` is ``global``, else ``gate`` (focus or causal decided; Q1)."""
    layer = _choice(comparison["deciding_layer"], ("focus", "causal", "global"))
    return "global" if layer == "global" else "gate"


def comparison_reference(comparison: Mapping) -> str:
    """The measurement of the round's champion the comparison used: ``stored`` for Core's
    ``stored_non_contemporaneous``, ``fresh`` for ``fresh``, ``unknown`` when Core recorded none."""
    return _REFERENCE_OF[_choice(comparison["reference"], tuple(_REFERENCE_OF))]


def scope_display(row: Mapping) -> str:
    """By the scope's own recorded status, rule and threshold, never by the campaign mode: not ``available`` ->
    ``status``; ``bound``/``bootstrap`` keep their own display; ``point`` with a threshold -> ``point``; otherwise (``off``,
    or no threshold) -> ``no_threshold``. It names a statement; it decides nothing about the gate."""
    vocabulary = _vocabulary()
    status = _choice(row["status"], vocabulary["statuses"])
    rule = _choice(row["rule"], vocabulary["rules"])
    if status != "available":
        return "status"
    if rule == "bound":
        return "bound"
    if rule == "bootstrap":
        return "bootstrap"
    if rule == "point" and row["threshold"] is not None:
        return "point"
    return "no_threshold"


def scope_row(row: Mapping) -> dict:
    """One comparison scope (family15_report.py:1063-1111): counts and codes copied, quantities wrapped."""
    _keys(row, ("name", "rule", "status", "unavailable", "point", "threshold", "strict", "confidence", "enclosure",
                "declared_count", "unit_count", "mass", "excluded_mass"),
          ("fallback", "lower", "exact", "pairing") if row["rule"] == "bootstrap" else ("fallback",))
    enclosure = row["enclosure"]
    if enclosure is not None:
        _keys(enclosure, ENCLOSURE_KEYS)
    return {"name": row["name"], "rule": row["rule"], "status": row["status"], "unavailable": row["unavailable"],
            "display": scope_display(row),
            **{name: statistical(row[name]) for name in ("point", "threshold")}, "strict": row["strict"],
            "confidence": statistical(row["confidence"]),
            "enclosure": None if enclosure is None else {
                "lower": statistical(enclosure["lower"]), "upper": statistical(enclosure["upper"]),
                "steps": enclosure["steps"]},
            "declared_count": row["declared_count"], "unit_count": row["unit_count"],
            "mass": statistical(row["mass"]), "excluded_mass": statistical(row["excluded_mass"]),
            "fallback": _fallback(row.get("fallback")), "lower": statistical(row.get("lower")),
            "exact": row.get("exact"), "pairing": row.get("pairing")}


def _comparison(value: Mapping) -> dict:
    _keys(value, ("outcome", "reason", "deciding_layer", "reference", "reference_measurement_id", "verification",
                  "eligible", "mode", "confidence_scope", "selection_bias_possible", "excluded_reference_slots",
                  "coverage", "noise", "scopes", "method", "unit"), ("calibration", "caveats", "bound_fallback"))
    _choice(value["outcome"], core_raw.family15_comparison_vocabulary()["outcomes"])
    reference_id = value["reference_measurement_id"]
    if reference_id is not None and not isinstance(reference_id, str):
        raise ValueError("reference_measurement_id is not a string or null")
    noise, coverage = value["noise"], value["coverage"]
    _keys(coverage, ("candidate", "reference"))
    _keys(noise, (*NOISE_KEYS, "diagnostic"), ("repeat_variance",))
    variance = noise.get("repeat_variance")
    return {"outcome": value["outcome"], "reason": value["reason"],
            "layer": comparison_layer(value), "deciding_layer": value["deciding_layer"],
            "reference": comparison_reference(value), "reference_kind": value["reference"],
            "reference_measurement_id": reference_id,
            "verification": value["verification"], "eligible": value["eligible"], "mode": value["mode"],
            "confidence_scope": value["confidence_scope"],
            "selection_bias_possible": list(_list(value["selection_bias_possible"])),
            "excluded_reference_slots": value["excluded_reference_slots"],
            "coverage": {side: _plain(coverage[side], COVERAGE_SIDE_KEYS) for side in ("candidate", "reference")},
            "noise": {"kind": noise["kind"], "allowance": statistical(noise["allowance"]), "source": noise["source"],
                      "source_kind": noise["source_kind"], "variation": noise["variation"], "units": noise["units"],
                      "observed_cases": noise["observed_cases"], "extra_measurements": noise["extra_measurements"],
                      **({"repeat_variance": {"minimum": statistical(variance["minimum"]),
                                             "maximum": statistical(variance["maximum"]),
                                             "unavailable_counts": dict(variance["unavailable_counts"])}}
                         if variance is not None else {})},
            "scopes": [scope_row(row) for row in _list(value["scopes"])],
            "method": value["method"], "unit": value["unit"], "calibration": _calibration(value.get("calibration")),
            "caveats": None if "caveats" not in value else [
                _choice(code, COMPARISON_CAVEATS) for code in _list(value["caveats"])],
            "bound_fallback": _choice(value.get("bound_fallback"), (None, "point"))}


def _preflight_scope(row: Mapping) -> dict:
    """A preflight scope: Core omits what does not apply (a skipped scope has only name and status,
    family15_report.py:983-1014); Lab's closed row has every key, null where Core has none."""
    _keys(row, ("name", "status"), tuple(k for k in PREFLIGHT_SCOPE_KEYS if k not in ("name", "status")))
    _choice(row["status"], _vocabulary()["preflight_statuses"])
    e_max = row.get("e_max")
    if e_max is not None:
        _choice(e_max["kind"], ("finite", "positive_infinity"))
        _keys(e_max, ("kind", "value") if e_max["kind"] == "finite" else ("kind",))
    return {"name": row["name"], "status": row["status"], "rule": row.get("rule"),
            "threshold": statistical(row.get("threshold")), "strict": row.get("strict"),
            "unit_count": row.get("unit_count"), "passes": row.get("passes"),
            "confidence": statistical(row.get("confidence")), "delta_max": statistical(row.get("delta_max")),
            "c_max": statistical(row.get("c_max")),
            "e_max": None if e_max is None else {"kind": e_max["kind"], "value": statistical(e_max.get("value"))},
            "fallback": _fallback(row.get("fallback"))}


def _explanation(value: Mapping) -> dict:
    """Core's preflight explanation (family15_report.py:967-981): score-range ceilings, not a prediction."""
    _keys(value, ("status", "basis", "profile_digest", "interpretation", "scopes"), ("bound_fallback",))
    return {"status": value["status"], "basis": value["basis"], "profile_digest": value["profile_digest"],
            "interpretation": value["interpretation"],
            "scopes": [_preflight_scope(row) for row in _list(value["scopes"])],
            "bound_fallback": _choice(value.get("bound_fallback"), (None, "point"))}


def slots_row(value: Mapping) -> dict:
    """Copy Core's conditional dispatch accounting without deriving totals."""
    _keys(value, ("candidate",), ("parent",))
    result = {}
    for side, counts in value.items():
        row = _plain(counts, ("scheduled", "dispatched", "not_owed"))
        if any(type(count) is not int or count < 0 for count in row.values()):
            raise ValueError("invalid slot count")
        result[side] = row
    return result


def stages_row(value: list) -> list[dict]:
    """The closed two-cell Lab stage form, in focus/causal order."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("invalid stages")
    for row, scope in zip(value, ("focus", "causal"), strict=True):
        _keys(row, ("scope", "state"))
        if row["scope"] != scope or row["state"] not in STATES:
            raise ValueError("invalid stages")
    return [dict(row) for row in value]


def _attempt(value: Mapping, closed: frozenset = frozenset(), finished: bool = False,
             bindings: Mapping | None = None, *, focus_first: bool = False, stages: Mapping | None = None) -> dict:
    _keys(value, tuple(k for k in ATTEMPT_KEYS if k not in ("group", "gate_binding", "global_binding")), ("slots", "progress"))
    if bindings is not None:
        _keys(bindings, ("gate_binding", "global_binding"))
    comparisons = {name: None if value[name] is None else _comparison(value[name])
                   for name in ("comparison", "gate_comparison", "global_comparison")}
    return {"round": value["round"], "attempt_id": value["attempt_id"], "outcome": value["outcome"],
            "reason": value["reason"],
            "group": attempt15_group(value["outcome"], value["reason"], global_comparison=value["global_comparison"],
                                    round_closed=value["round"] in closed, campaign_finished=finished),
            "eligible_final_score": statistical(value["eligible_final_score"]),
            **{name: binding_row((bindings or {}).get(name)) for name in ("gate_binding", "global_binding")},
            **comparisons,
            **({"slots": slots_row(value["slots"])} if "slots" in value else {}),
            **({"stages": attempt_stages(value, stages)} if focus_first and "slots" in value else {}),
            **({"progress": {**value["progress"], "editor_failures": [dict(f) for f in value["progress"]["editor_failures"]]}}
               if "progress" in value else {}),
            "preflight": None if value["preflight"] is None else _explanation(value["preflight"]),
            "conditional_cost": None if value["conditional_cost"] is None
            else _plain(value["conditional_cost"], COST_KEYS)}


def _committed(value: Mapping | None) -> dict | None:
    if value is None:
        return None
    row = _plain(value, COMMITTED_KEYS)
    for name in ("measurement", "connection_preflight"):
        if row[name] is not None:
            row[name] = _plain(row[name], COMMITTED_WORK_KEYS)
    return row


def independent_view(block: Mapping, *, closed_rounds: frozenset = frozenset(),
                     campaign_finished: bool = False, bindings: Mapping | None = None,
                     stages: Mapping | None = None) -> dict:
    """Core's ``independent`` block -> Lab's closed block (D4). Raises on anything this pin does not describe.
    ``closed_rounds`` are the rounds the caller knows Core has closed (``closed_rounds()`` of the rounds);
    ``campaign_finished`` is Core's recorded terminal (``campaign_finished()`` of the same payload)."""
    core_raw.validate_family15_projection(block)
    _keys(block, ("next_action", "settings", "planned_work", "local_guidance", "preflight", "attempts", "diagnostics",
                  "committed_work", "remaining_work", "evidence"),
          ("check_modes", "shadow_hits", "probe_availability", "round_progress", "suspension"))
    _choice(block["evidence"], _vocabulary()["evidence"])
    settings = block["settings"]
    _keys(settings, ("mode", "verification", "stability", "fresh_parent", "global_rule", "train_cases", "repeats",
                     "train_slots", "execution_noise", "allowance", "allowance_source", "allowance_source_kind",
                     "method", "unit"), ("bound_fallback", "stages"))
    vocabulary = core_raw.family15_attempt_vocabulary()
    for attempt in _list(block["attempts"]):
        _choice(attempt["outcome"], vocabulary["outcomes"] | {None})
        _choice(attempt["reason"], vocabulary["reasons"] | {None})
    guidance, remaining = block["local_guidance"], block["remaining_work"]
    return {
        "evidence": block["evidence"], "next_action": block["next_action"],
        **({"suspension": dict(block["suspension"])} if "suspension" in block else {}),
        **({"round_progress": [{**row, "reflections": [dict(r) for r in row["reflections"]]}
                              for row in block["round_progress"]]} if "round_progress" in block else {}),
        "settings": {
            **({"stages": _choice(settings["stages"], ("focus_first",))} if "stages" in settings else {}),
            "mode": settings["mode"], "repeats": settings["repeats"], "verification": settings["verification"],
            "stability": settings["stability"], "fresh_parent": settings["fresh_parent"],
            "global_rule": settings["global_rule"], "train_cases": settings["train_cases"],
            "train_slots": settings["train_slots"],
            "execution_noise": {"kind": settings["execution_noise"], "allowance": statistical(settings["allowance"]),
                                "source": settings["allowance_source"],
                                "source_kind": settings["allowance_source_kind"]},
            "statistics": {"method": settings["method"], "unit": settings["unit"]},
            "bound_fallback": _choice(settings.get("bound_fallback"), (None, "point"))},
        "attempts": [_attempt(value, closed_rounds, campaign_finished, (bindings or {}).get(value["attempt_id"]),
                              focus_first=settings.get("stages") == "focus_first",
                              stages=None if stages is None else stages.get(value["attempt_id"]))
                     for value in _list(block["attempts"])],
        "attainability": {
            "preflight": [{"round": item["round"], "explanation": _explanation(item["explanation"]),
                           "refresh": _plain(item["refresh"], REFRESH_KEYS)} for item in _list(block["preflight"])],
            "local_guidance": None if guidance is None else _plain(guidance, GUIDANCE_KEYS)},
        "work": {"planned": _plain(block["planned_work"], PLANNED_KEYS),
                 "committed": _committed(block["committed_work"]),
                 "remaining": None if remaining is None else _plain(remaining, REMAINING_KEYS)},
    }
