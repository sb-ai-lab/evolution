// Labels and tones for the service's closed code sets (spec 2026-09-30 §2.4).
// The object between the markers is strict JSON: tests/test_panel_labels.py parses it and compares
// every set with Lab's own constants, so a new core pin cannot drift from the panel unnoticed.
// Tones: blue = in progress, green = done/accepted/present, amber = not enough data / suspended /
// outdated / unavailable, violet = infrastructure, neutral = planned / not better / stopped / unknown /
// gate not applied (the observe mark of lab-campaign-view family 14: never green, it is not evidence of quality).
// The statistical gate mark is neutral as well: the gate ran, the decision is core's.
// Family 15 (lab-campaign-view/8 `independent`): a comparison passed is green, not passed neutral, unavailable amber;
// provenance_failed is violet like Lab's infrastructure group; Lab's reference words read inside "vs round champion ·".
/* LABELS-JSON-BEGIN */
export const LABELS = {
  "phase": {
    "baseline": ["Measuring baseline", "blue"], "reflection": ["Reflecting", "blue"],
    "generation": ["Generating candidate", "blue"], "evaluation": ["Measuring candidate", "blue"],
    "selection": ["Selecting", "blue"], "finishing": ["Finishing", "blue"],
    "suspended": ["Suspended", "amber"], "finished": ["Finished", "green"],
    "waiting": ["Waiting for core", "neutral"], "unknown": ["Unknown phase", "neutral"]
  },
  "attempt_group": {
    "accepted": ["Accepted", "green"], "agent_worse": ["Not better", "neutral"],
    "not_enough_data": ["Not enough data", "amber"], "infrastructure": ["Infrastructure", "violet"],
    "no_candidate": ["No candidate", "neutral"], "stopped": ["Stopped", "neutral"],
    "in_progress": ["In progress", "blue"], "unknown": ["Unknown", "neutral"]
  },
  "reflection_state": {
    "present": ["Reflected", "green"], "declined": ["Declined", "neutral"], "failed": ["Failed", "violet"],
    "unsettled": ["Not settled", "amber"], "in_progress": ["Reflecting", "blue"],
    "absent": ["No reflection", "neutral"]
  },
  "failure_stage": {
    "editor": ["Editor", "neutral"], "seal": ["Seal", "neutral"], "measurement": ["Measurement", "violet"],
    "lifecycle": ["Lifecycle", "neutral"], "unknown": ["Unknown stage", "neutral"]
  },
  "stale_code": {
    "inputs_changed": ["Inputs changed", "amber"], "digest_changed": ["Digest changed", "amber"],
    "catalogue_changed": ["Catalogue changed", "amber"], "place_changed": ["Place changed", "amber"],
    "other_pair": ["Other pair", "amber"]
  },
  "target_state": {
    "starting": ["Starting", "blue"], "ready": ["Ready", "green"], "stopping": ["Stopping", "blue"],
    "stopped": ["Stopped", "neutral"], "unknown": ["Unknown", "neutral"],
    "cleanup_required": ["Cleanup required", "violet"], "cleanup_first": ["Cleanup first", "amber"],
    "busy": ["Busy", "blue"]
  },
  "control_state": {
    "reserved": ["Reserved", "blue"], "running": ["Running", "blue"], "stopping": ["Stopping", "blue"],
    "finished": ["Finished", "green"], "released": ["Released", "neutral"],
    "cleanup_required": ["Cleanup required", "violet"], "orphaned": ["Orphaned", "violet"]
  },
  "results_state": {
    "not_created": ["No results yet", "neutral"], "present": ["Results present", "neutral"],
    "unavailable": ["Unavailable", "amber"], "deleting": ["Deleting", "neutral"],
    "deleted": ["Deleted", "neutral"], "delete_failed": ["Delete failed", "violet"]
  },
  "resume_state": {
    "ready": ["Ready to continue", "green"], "cleanup_first": ["Cleanup first", "amber"],
    "busy": ["Busy", "blue"], "refused": ["Refused", "neutral"], "not_applicable": ["Nothing to continue", "neutral"]
  },
  "round_outcome": {
    "promoted": ["Promoted", "green"], "stalled": ["Stalled", "neutral"], "clean": ["No improvement", "neutral"],
    "unscored": ["Not scored", "amber"], "unmeasurable": ["Not measurable", "amber"],
    "unplanned": ["Not planned", "neutral"], "condition_changed": ["Condition changed", "neutral"]
  },
  "step_state": {
    "done": ["Done", "green"], "stale": ["Outdated", "amber"], "ready": ["Ready", "blue"],
    "blocked": ["Blocked", "neutral"]
  },
  "scenario_state": {
    "unavailable": ["Unavailable", "amber"], "available": ["Available", "blue"], "produced": ["Produced", "green"]
  },
  "blocked_code": {
    "results_deleted": ["Results were deleted", "neutral"], "artifact_invalid": ["Artifact fails its schema", "amber"],
    "schema_unusable": ["Schema cannot check", "violet"], "campaign_suspended": ["Campaign suspended", "amber"],
    "campaign_state_unknown": ["Campaign state unknown", "amber"]
  },
  "fact_holds": {
    "true": ["Holds", "green"], "false": ["Does not hold", "amber"]
  },
  "project_state": {
    "creating": ["Creating", "blue"], "ready": ["Ready", "neutral"], "failed": ["Failed", "violet"]
  },
  "section_state": {
    "not_started": ["Not started", "neutral"], "present": ["Present", "green"], "unavailable": ["Unavailable", "amber"]
  },
  "operation_state": {
    "present": ["Present", "green"], "other_pair": ["Other pair", "neutral"], "unavailable": ["Unavailable", "amber"]
  },
  "unavailable_reason": {
    "core_raw_unsupported": ["Core version not supported", "amber"], "unreadable": ["Unreadable", "amber"]
  },
  "settlement": {
    "positive": ["Settled cleanly", "green"], "not_positive": ["Settled with cleanup", "amber"],
    "unknown": ["Not settled yet", "neutral"]
  },
  "outcome": {
    "completed": ["Completed", "green"], "stopped": ["Stopped", "neutral"],
    "budget_exhausted": ["Budget exhausted", "amber"], "infrastructure_failed": ["Infrastructure failed", "violet"]
  },
  "comparison": {
    "better": ["Better", "green"], "same": ["Same", "neutral"], "worse": ["Worse", "neutral"],
    "unpaired": ["Unpaired", "amber"], "unknown": ["Unknown", "neutral"]
  },
  "nontext_kind": { "binary": ["Binary", "neutral"], "mode": ["Mode change", "neutral"] },
  "target_requirement": { "fresh_instance": ["Fresh instance", "amber"] },
  "gate_mark": { "observe_gate_not_enforced": ["Gate not applied — not evidence of quality", "neutral"],
                 "unknown": ["Gate unknown — not evidence of quality", "neutral"],
                 "statistical": ["Statistical gate", "neutral"] },
  "confidence_decision": { "continue": ["Next look", "blue"],
                           "rejected": ["Rejected", "neutral"],
                           "finalist": ["Finalist (entered final)", "neutral"],
                           "insufficient_evidence": ["Insufficient evidence", "amber"] },
  "selection_reason": { "finalist_selected": ["Finalist selected", "neutral"],
                        "no_accepted_finalist": ["No accepted finalist", "neutral"],
                        "global_point_not_improved": ["Global point not improved", "neutral"],
                        "global_bound_not_positive": ["Global bound not positive", "neutral"],
                        "no_finalists": ["No finalists", "neutral"] },
  "caveat": {
    "intervals_empirical": ["Empirical intervals", "neutral"],
    "noise_allowance_empirical": ["Empirical noise allowance", "neutral"],
    "causal_parent_not_contemporaneous": ["Causal parent not contemporaneous", "neutral"],
    "observe_gate_not_enforced": ["Observe gate not enforced", "neutral"],
    "uncalibrated_point_estimates": ["Uncalibrated point estimates", "neutral"],
    "noise_allowance_unavailable": ["Noise allowance unavailable", "neutral"],
    "condition_changed": ["Condition changed", "neutral"],
    "predecessor_unfinished": ["Predecessor unfinished", "neutral"],
    "baseline_inherited": ["Baseline inherited", "neutral"],
    "baseline_imported": ["Baseline imported", "neutral"],
    "imported_provenance_unknown": ["Imported provenance unknown", "neutral"],
    "condition_changed_in_place": ["Condition changed in place", "neutral"],
    "grouping_fallback": ["Grouping fallback", "neutral"],
    "focus_selection_bias": ["Focus selection bias", "neutral"],
    "provenance_drift": ["Provenance drift", "neutral"],
    "components_changed_by_condition": ["Components changed by condition", "neutral"],
    "exact_contrast": ["Exact contrast", "neutral"],
    "noise_not_observed": ["Noise not observed", "neutral"],
    "champion_not_contemporaneous": ["Champion not contemporaneous", "neutral"],
    "stored_pairs_excluded": ["Stored pairs excluded", "neutral"]
  },
  "cohort_role": { "champion": ["Champion", "neutral"], "finalist": ["Finalist", "neutral"] },
  "suspension_reason": {
    "champion_incomplete": ["Champion measurement incomplete", "amber"],
    "connection_incompatible": ["Connection incompatible", "amber"],
    "connection_preflight_inconclusive": ["Connection preflight inconclusive", "amber"],
    "connection_preflight_unavailable": ["Connection preflight unavailable", "amber"],
    "editor_failing": ["Editor failing", "amber"],
    "editor_provider_failed": ["Editor provider failed", "amber"],
    "editor_unresponsive": ["Editor unresponsive", "amber"],
    "environment_unstable": ["Environment unstable", "amber"],
    "final_incomplete": ["Final incomplete", "amber"],
    "grouper_failing": ["Grouper failing", "amber"],
    "grouper_provider_failed": ["Grouper provider failed", "amber"],
    "grouping_failed": ["Grouping failed", "amber"],
    "probe_unavailable": ["Probe unavailable", "amber"],
    "reflector_failing": ["Reflector failing", "amber"],
    "reflector_provider_failed": ["Reflector provider failed", "amber"],
    "reflector_unresponsive": ["Reflector unresponsive", "amber"],
    "rollout_time_budget_exhausted": ["Rollout time budget exhausted", "amber"],
    "scorer_metric_missing": ["Scorer metric missing", "amber"],
    "scorer_metric_out_of_range": ["Scorer metric out of range", "amber"],
    "scorer_unstable": ["Scorer unstable", "amber"],
    "target_changed": ["Target changed", "amber"],
    "target_components_changed": ["Target components changed", "amber"],
    "target_contract_violated": ["Target contract violated", "amber"],
    "target_unstable": ["Target unstable", "amber"]
  },
  "attempt15_outcome": {
    "promoted": ["Promoted", "green"], "finalist": ["Finalist", "neutral"], "rejected": ["Rejected", "neutral"],
    "retained": ["Retained", "neutral"], "abandoned": ["Abandoned", "neutral"], "failed": ["Failed", "neutral"],
    "declined": ["Declined", "neutral"], "insufficient_evidence": ["Insufficient evidence", "amber"]
  },
  "comparison15_outcome": {
    "passed": ["Passed", "green"], "comparison_not_passed": ["Not passed", "neutral"],
    "comparison_unavailable": ["Comparison unavailable", "amber"]
  },
  "comparison15_reason": {
    "passed": ["Passed", "green"], "threshold_not_passed": ["Threshold not passed", "neutral"],
    "critical_failed": ["Critical case failed", "neutral"], "provenance_failed": ["Provenance failed", "violet"],
    "empty_support": ["Empty support", "amber"], "candidate_incomplete": ["Candidate incomplete", "amber"],
    "reference_incomplete": ["Reference incomplete", "amber"],
    "provenance_unavailable": ["Provenance unavailable", "amber"],
    "insufficient_repeats": ["Insufficient repeats", "amber"],
    "insufficient_calibration": ["Insufficient calibration", "amber"]
  },
  "scope_status": {
    "not_measured": ["Not measured", "neutral"],
    "available": ["Available", "neutral"], "skipped": ["Skipped", "neutral"], "unavailable": ["Unavailable", "amber"]
  },
  "stage_state": {
    "waiting": ["waiting", "neutral"], "open": ["open", "blue"], "passed": ["passed", "green"],
    "rejected": ["rejected", "neutral"], "unavailable": ["unavailable", "amber"],
    "not_run": ["not run", "neutral"], "incomplete": ["incomplete", "amber"], "unknown": ["—", "neutral"]
  },
  "scope_rule": {
    "bound": ["Confidence bound", "neutral"], "point": ["Point estimate", "neutral"], "off": ["No threshold", "neutral"],
    "bootstrap": ["Bootstrap", "neutral"]
  },
  "comparison_reference": {
    "stored": ["stored measurement", "neutral"], "fresh": ["fresh measurement", "neutral"],
    "unknown": ["measurement not recorded", "amber"]
  },
  "comparison_layer": { "gate": ["Gate comparison", "neutral"], "global": ["Global comparison", "neutral"] },
  "independent_evidence": {
    "authenticated_publications": ["Authenticated publications", "green"],
    "snapshot_only": ["Snapshot only", "amber"], "unavailable": ["Unavailable", "amber"]
  },
  "gate15_mode": {
    "confidence": ["Confidence", "neutral"], "point": ["Point", "neutral"], "unfiltered": ["Unfiltered", "neutral"],
    "statistical": ["Statistical", "neutral"]
  }
}
/* LABELS-JSON-END */;

export function label(kind, code) {
  if (code === null || code === undefined) return { text: "—", tone: "neutral", known: true };
  const table = Object.hasOwn(LABELS, kind) ? LABELS[kind] : null;
  const entry = table && Object.hasOwn(table, code) ? table[code] : null;
  return entry ? { text: entry[0], tone: entry[1], known: true } : { text: String(code), tone: "neutral", known: false };
}

// Word phrases for a few core codes (family14 layout); outside the JSON block, not a label table.
const PHRASES = {
  round_stop: { first_accepted: "stops at the first accepted finalist", all_attempts: "all attempts run, then joint final" },
  champion_side: { stored: "Champion side: stored measurement", fresh: "Champion re-measured in the final" },
  stability: { stable: "stable" },
};
export function phrase(kind, code) {
  const table = Object.hasOwn(PHRASES, kind) ? PHRASES[kind] : null;
  return table && Object.hasOwn(table, code) ? { text: table[code], known: true } : { text: String(code), known: false };
}

// Same two statements as _independent_text.py; the panel's current language is English.
const INDEPENDENT_TEXT = {
  en: { uncalibrated: "threshold not calibrated yet", fallback: "weaker: point decision instead of a bound" },
  ru: { uncalibrated: "порог ещё не откалиброван", fallback: "слабее: точечное решение вместо границы" },
};
export const independentText = (code, lang = "en") => INDEPENDENT_TEXT[lang]?.[code] ?? INDEPENDENT_TEXT.en[code];

export const checksText = {
  title: "Check modes",
  hint: "Counts of different checks are not comparable.",
};

export const reflectorProbeText = {
  title: "Reflector probe",
  hint: "Owner record for this worker start. Operator state describes participation; Lab checks its health only at start and never stops the operator server.",
};

export const routingText = {
  title: "Routing",
  hint: "Observed routing from Lab. Shared calls and calls outside a use are separate from role rows. An unreported provider is unknown.",
};

// Status-card sentences are templates over delivered fields, not interpretations of outcomes.
export const verdictText = {
  cancelled: "The campaign was cancelled{round}.",
  stopped: "The campaign stopped{round}.",
  completed: "The campaign finished{allRounds}.",
  budget: "The campaign ran out of its evaluation budget{round}.",
  infrastructure: "The campaign stopped on an infrastructure failure{round}.",
  refusal: "Core refused this campaign — {code}.",
  terminal: "Core ended the campaign{round} — {terminal} {code}.",
  failure: "The campaign failed{round} — {code}.",
  pauseDiagnostic: "The campaign is paused{round}; the last start recorded {code}.",
  pause: "The campaign is paused{round} — {reason}.",
  pauseUnread: "The campaign is paused{round}; the pause reason is unavailable (campaign view {readCode}).",
  runningPhase: "{phase}{runningProgress}.",
  attemptsClosed: "{closed}{plannedAttempts} attempts closed{runningAttempts}",
  attemptsPlanned: " of {planned}",
  attemptsRunning: ", {running} running",
  running: "The campaign is running.",
  reserved: "The campaign is reserved and has not started.",
  stopping: "The campaign is stopping.",
  noOutcome: "Core recorded no outcome for this campaign.",
  outcomeUnknown: "Outcome unknown — campaign view unavailable ({readCode}).",
  neutral: "Core recorded {outcome} · {reason}",
};
