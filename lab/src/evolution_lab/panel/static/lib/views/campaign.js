// lib/views/campaign.js — the campaign screen's model (spec 2026-09-30 §3.2 screen 5): joins
// lab-operation, lab-campaign-view/15 and lab-measurement/3 answers; arithmetic only (token sums, counts).
// Family 14's Core content is unchanged; family 15's `independent` block includes b10 gate options
// (lib/views/independent.js, dev58 spec §3), and has no final records.
import { label, phrase } from "../labels.js";
import { number, shortId, signed, time, tokens } from "../format.js";
import { href } from "../route.js";
import { readOf } from "../resource.js";
import { sectionText } from "./shell.js";
import { measurementOpen, primaryMetric, sampleOrder, stripRow } from "../samples.js";
import { caveatOf } from "./confidence.js";
import { checksModel, independentModel } from "./independent.js";

// Family 14 of lab-campaign-view/15 (tenet one-campaign-authority): core ran the gate in `observe` and
// accepted without applying it. The mark is neutral, never green, and comes from Lab's fields only (gate_mode,
// caveats), never from result.core (PDR-0017). A campaign without a gate mode (its caveats were unreadable) is an
// unknown gate and fails closed (final review I1): every campaign, round, attempt and promoted champion is marked.
const OBSERVE = "observe_gate_not_enforced";
const caveatsOf = (campaign) => campaign?.caveats ?? [];
// One code, one text (final review item 2): the synthesised observe mark reads as the caveat it stands for, in
// limits and on the lineage pill alike; the unknown gate keeps its gate_mark text.
const observeMark = () => ({ ...label("caveat", OBSERVE), tone: "neutral" });
// Family 15's mode comes from its own settings and its scopes carry their own rules: these marks are family 14's only.
const GATED = (campaign) => supportedCampaign(campaign)?.family === 14;
const gateUnknown = (campaign) => GATED(campaign) && campaign?.gate_mode == null;
const gateMark = (campaign) => (gateUnknown(campaign) ? label("gate_mark", "unknown") : observeMark());
export const observed = (campaign) => GATED(campaign) && (gateUnknown(campaign) || campaign.gate_mode === "observe"
  || caveatsOf(campaign).some((c) => c.kind === OBSERVE));
export const attemptMarkOf = (campaign, attemptId) => (!GATED(campaign) ? null : gateUnknown(campaign) ? gateMark(campaign)
  : caveatsOf(campaign).some((c) => c.kind === OBSERVE && c.attempt_id === attemptId) ? observeMark() : null);
const roundMarkOf = (campaign, round) => (gateUnknown(campaign) ? gateMark(campaign)
  : caveatsOf(campaign).some((c) => c.kind === OBSERVE && c.round === round) ? observeMark() : null);
// The campaign's own observe or unknown gate mark.
export const campaignMarkOf = (campaign) => (observed(campaign) ? gateMark(campaign) : null);
// Not in the H1 (spec family14 §4.2): the observe/unknown mark is a Result limit; a statistical gate is the
// confidence block's own `mode`, a neutral mark in the Measurement's Confidence panel (ui/confidence.js).

// The schema/family check of the campaign view: made before reading campaign fields. Every consumer of
// paths.campaign reads it through campaignEntry (ui/hooks.js useCampaign), so another schema/family is an unavailable read
// (unsupported_schema) — no field of it reaches a model, a title pill or a measurement fetch; the models below
// check again for callers that pass the body itself.
export const CAMPAIGN_SCHEMA = "lab-campaign-view/15";
export const UNSUPPORTED = Object.freeze({ ok: false, code: "unsupported_schema" });
// Scalar field list (consumed fields, not gate validation; new b10 object guards are closed below). Producers:
// owner/campaign_views.py copies ids/codes, flags, counts and family-14 native statistics; progress.py and
// measurements.py supply plain counts/means. independent_view.py wraps ONLY designated family-15 statistics
// through exact_numbers.statistical: display value finite number|null, exact opaque object|null (not decoded).
// Missing keys remain outside this type guard; explicit null is allowed only by the predicates below.
// Shared: operation_id, champions_state, details_state/reason; policy.metric_primary; progress.phase,
// rounds.{completed,current,planned}, attempts.{total,current_round.{closed,running,planned}}, coverage.{scored,expected};
// outcome.{outcome,reason}; champions[].{version,origin,attempt_id,measurement_id};
// caveats[].{kind,round,attempt_id}; suspensions[].{owner,reason,evidence_kind,seq};
// lab_notices[].{code,rounds,by_reason.*,reflection_errors.*}; roles.{editor,reflector,grouper}.kind;
// settings.{stability,round_stop,baseline_refresh.{count,every_rounds,performed[].{round,measurement_id,
// replaced_measurement_id,completed}}}; connection.{state,outcome,attempts,detail.{code,cause_code,metric,
// manifest_path,observed_path}}; rounds[].{round,outcome,baseline_measurement_id,baseline_version,
// reflection.{state,reflection_id,error_class,reason,hypotheses[].{index,root_cause,suggested_fix,affected_cases[],
// target_paths[],counterevidence,regression_risks,why_this_layer}},attempts[].{attempt_id,outcome,reason,group,
// measurement_id,has_tree,failure.{stage,role,error_class,role_reason,code}}};
// measurements[].{measurement_id,role,round,attempt_id,usage_state,usage.{input_tokens,output_tokens,unknown_calls},
// metrics.*.mean,baseline.delta.*.mean}.
// Family 14: gate_mode; rounds[].attempts[].{look,final_decision.{state,reason},confidence.{mode,repeats,
// focus_cases,causal_cases,margin,calibration.{allowance,degenerate,unavailable},latest.{look,final,decision,reason,
// focus/causal.{point,lower,upper,exact}}}}; rounds[].final[].{ordinal,stability,champion_side,stored_measurement_id,
// mode,global,listed,next_baseline_measurement_id,condition_changed_in_place,members[].{measurement_id,role,
// attempt_id,complete,point,missing,critical},contrasts[].{attempt_id,point,lower,level,unavailable,pairing,accepted,
// reason,exact,removed_pairs,removed_cases,condition_changed_in_place},selection.{reason,attempt_id,measurement_id}}.
// Family 15 independent: evidence; settings.{mode,repeats,verification,execution_noise.{kind,source_kind,allowance}};
// attainability.{local_guidance.minimum_units,preflight[].{round,explanation}}; attempts[].{round,attempt_id,group,
// outcome,reason,eligible_final_score,conditional_cost.{candidate_total_slots,candidate_gate_slots,
// candidate_final_slots,simplest_passing_path_slots},preflight,comparison/gate_comparison/global_comparison}.
// Each comparison: outcome,reason,layer,deciding_layer,reference,reference_measurement_id,scopes[].{name,rule,
// status,unavailable,display,strict,point,threshold,confidence,enclosure.lower,lower,exact,pairing,fallback.reason}.
// b10: settings/comparison/preflight.bound_fallback; comparison.{method,unit,calibration,caveats};
// attempts[].{gate_binding,global_binding} with source/scopes[].{name,rule} (independent of result rules).
// Each preflight/explanation: status,basis,scopes[].{name,status,rule,strict,passes,threshold,confidence,c_max,delta_max,fallback}.
const object = (v) => typeof v === "object" && v !== null && !Array.isArray(v);
const optionalObject = (v, check = () => true) => v == null || (object(v) && check(v));
const rows = (v, check = () => true) => Array.isArray(v) && v.every((r) => object(r) && check(r));
const optionalRows = (v, check) => v == null || rows(v, check);
const text = (v) => typeof v === "string";
const flag = (v) => typeof v === "boolean";
const finite = (v) => typeof v === "number" && Number.isFinite(v);
const nullable = (check) => (v) => v === null || check(v);
const fields = (v, check, names) => names.split(" ").every((key) => v[key] === undefined || check(v[key]));
const strings = (v, names) => fields(v, text, names);
const maybeStrings = (v, names) => fields(v, nullable(text), names);
const counts = (v, names) => fields(v, Number.isInteger, names);
const maybeCounts = (v, names) => fields(v, nullable(Number.isInteger), names);
const numbers = (v, names) => fields(v, nullable(finite), names);
const flags = (v, names) => fields(v, flag, names);
const maybeFlags = (v, names) => fields(v, nullable(flag), names);
const stringList = (v) => v === undefined || (Array.isArray(v) && v.every(text));
const countMap = (v) => optionalObject(v, (m) => Object.values(m).every(Number.isInteger));
const quantity = (v) => v == null || (object(v) && nullable(finite)(v.value)
  && (v.exact === null || object(v.exact)));
const quantities = (v, names) => fields(v, quantity, names);
const intervalOk = (v) => numbers(v, "point lower upper") && maybeFlags(v, "exact");
const confidenceOk = (c) => strings(c, "mode") && counts(c, "repeats focus_cases causal_cases")
  && numbers(c, "margin") && optionalObject(c.calibration, (v) => numbers(v, "allowance")
    && maybeFlags(v, "degenerate") && maybeStrings(v, "unavailable"))
  && optionalObject(c.latest, (v) => counts(v, "look") && flags(v, "final") && strings(v, "decision")
    && maybeStrings(v, "reason") && optionalObject(v.focus, intervalOk) && optionalObject(v.causal, intervalOk));
const attemptOk = (a, family) => strings(a, "attempt_id group") && maybeStrings(a, "outcome reason measurement_id")
  && maybeFlags(a, "has_tree") && optionalObject(a.failure, (v) => strings(v, "stage")
    && maybeStrings(v, "role error_class role_reason code"))
  && (family !== 14 || (maybeCounts(a, "look") && optionalObject(a.final_decision,
    (v) => strings(v, "state") && maybeStrings(v, "reason")) && optionalObject(a.confidence, confidenceOk)));
const finalOk = (f) => counts(f, "ordinal listed") && maybeStrings(f, "stability") && strings(f, "champion_side mode global")
  && maybeStrings(f, "stored_measurement_id next_baseline_measurement_id") && maybeFlags(f, "condition_changed_in_place")
  && optionalRows(f.members, (v) => strings(v, "measurement_id role") && maybeStrings(v, "attempt_id")
    && maybeFlags(v, "complete critical") && maybeCounts(v, "missing") && numbers(v, "point"))
  && optionalRows(f.contrasts, (v) => strings(v, "attempt_id") && maybeStrings(v, "unavailable pairing reason")
    && numbers(v, "point lower level") && maybeFlags(v, "accepted exact") && flags(v, "condition_changed_in_place")
    && counts(v, "removed_pairs removed_cases"))
  && optionalObject(f.selection, (v) => maybeStrings(v, "reason attempt_id measurement_id"));
const hypothesisOk = (h) => counts(h, "index") && strings(h, "root_cause suggested_fix")
  && maybeStrings(h, "counterevidence regression_risks why_this_layer") && stringList(h.affected_cases) && stringList(h.target_paths);
const roundOk = (r, family) => counts(r, "round") && maybeStrings(r, "outcome baseline_measurement_id baseline_version")
  && rows(r.attempts, (a) => attemptOk(a, family)) && (family !== 14 || optionalRows(r.final, finalOk))
  && optionalObject(r.reflection, (v) => strings(v, "state") && maybeStrings(v, "reflection_id error_class reason")
    && optionalRows(v.hypotheses, hypothesisOk));
const metricRowsOk = (v) => optionalObject(v, (metrics) => Object.values(metrics).every((m) => optionalObject(m,
  (row) => numbers(row, "mean"))));
const measurementOk = (m) => strings(m, "measurement_id role usage_state") && maybeStrings(m, "attempt_id")
  && maybeCounts(m, "round") && optionalObject(m.usage, (v) => counts(v, "input_tokens output_tokens unknown_calls"))
  && metricRowsOk(m.metrics) && optionalObject(m.baseline, (b) => metricRowsOk(b.delta));
// Close new b10 objects/vocabularies at this boundary; exact quantities remain opaque, never decoded or recomputed.
const closed = (v, names) => object(v) && Object.keys(v).length === names.split(" ").length
  && names.split(" ").every((key) => Object.hasOwn(v, key));
const choice = (...values) => (v) => values.includes(v);
const fallbackOk = (v) => closed(v, "reason") && choice("focus_unattainable", "causal_unconfirmable")(v.reason);
const fallbackFields = (v) => optionalObject(v.fallback, fallbackOk);
const boundFallbackOk = (v) => fields(v, nullable(choice("point")), "bound_fallback");
const statisticsOk = (v) => fields(v, nullable(choice("bounded-weighted-mean/1", "f13-bootstrap/1")), "method")
  && fields(v, nullable(choice("case", "group")), "unit");
const bindingOk = (v) => closed(v, "source scopes") && v.source === "core_raw" && rows(v.scopes,
  (s) => closed(s, "name rule") && choice("focus", "causal", "global")(s.name)
    && choice("bound", "point", "off", "bootstrap")(s.rule));
const calibrationOk = (v) => closed(v, "source measurement_id prefix retained method level seed_input allowance degenerate result_digest")
  && choice("stored", "fresh")(v.source) && v.method === "f13-bootstrap/1"
  && strings(v, "measurement_id seed_input result_digest") && counts(v, "retained") && flags(v, "degenerate")
  && quantities(v, "level allowance") && closed(v.prefix, "version kind run_id locator content_digest")
  && v.prefix.version === 1 && v.prefix.kind === "journal_prefix" && strings(v.prefix, "run_id locator content_digest");
const scopeOk = (s) => strings(s, "name status") && choice("available", "unavailable", "skipped", "not_measured")(s.status) && maybeStrings(s, "unavailable") && maybeFlags(s, "strict exact")
  && fields(s, choice("bound", "point", "off", "bootstrap"), "rule")
  && fields(s, choice("bound", "point", "bootstrap", "no_threshold", "status"), "display")
  && fields(s, nullable(choice("exact", "unpaired")), "pairing") && fallbackFields(s)
  && optionalObject(s.enclosure, (e) => quantity(e.lower)) && quantities(s, "point threshold confidence lower");
const preflightOk = (p) => strings(p, "status") && maybeStrings(p, "basis") && boundFallbackOk(p)
  && optionalRows(p.scopes, (s) => strings(s, "name") && fields(s, choice("calculated", "skipped", "uncalibrated"), "status")
    && maybeStrings(s, "rule")
    && maybeFlags(s, "strict passes") && fallbackFields(s) && quantities(s, "threshold confidence c_max delta_max"));
const comparisonOk = (c) => strings(c, "outcome layer deciding_layer reference")
  && maybeStrings(c, "reason reference_measurement_id") && optionalRows(c.scopes, scopeOk)
  && statisticsOk(c)
  && boundFallbackOk(c) && optionalObject(c.calibration, calibrationOk)
  && (c.caveats == null || (stringList(c.caveats) && c.caveats.every(choice(
    "intervals_empirical", "exact_contrast", "noise_allowance_empirical", "noise_not_observed"))));
const slotsOk = (v) => (closed(v, "candidate") || closed(v, "candidate parent"))
  && Object.values(v).every((side) => closed(side, "scheduled dispatched not_owed")
    && Object.values(side).every((n) => Number.isInteger(n) && n >= 0));
const stageStates = ["waiting", "open", "passed", "rejected", "unavailable", "not_run", "incomplete", "unknown"];
const stagesOk = (v) => Array.isArray(v) && v.length === 2
  && v.every((s, i) => closed(s, "scope state") && s.scope === ["focus", "causal"][i] && stageStates.includes(s.state));
const independentOk = (b) => strings(b, "evidence") && optionalObject(b.settings,
  (s) => fields(s, choice("confidence", "point", "unfiltered", "statistical"), "mode")
    && fields(s, choice("focus_first"), "stages")
    && boundFallbackOk(s) && optionalObject(s.statistics, statisticsOk)
    && maybeStrings(s, "verification") && counts(s, "repeats") && optionalObject(s.execution_noise,
      (n) => strings(n, "kind") && maybeStrings(n, "source_kind") && quantities(n, "allowance")))
  && optionalObject(b.attainability, (a) => optionalObject(a.local_guidance, (v) => maybeCounts(v, "minimum_units"))
    && optionalRows(a.preflight, (p) => counts(p, "round") && optionalObject(p.explanation, preflightOk)))
  && optionalRows(b.attempts, (a) => counts(a, "round") && strings(a, "attempt_id group")
    && maybeStrings(a, "outcome reason") && optionalObject(a.comparison, comparisonOk)
    && optionalObject(a.gate_comparison, comparisonOk) && optionalObject(a.global_comparison, comparisonOk)
    && optionalObject(a.gate_binding, bindingOk) && optionalObject(a.global_binding, bindingOk)
    && optionalObject(a.preflight, preflightOk) && optionalObject(a.conditional_cost,
      (v) => maybeCounts(v, "candidate_total_slots candidate_gate_slots candidate_final_slots simplest_passing_path_slots"))
    && (a.slots === undefined || slotsOk(a.slots))
    && (b.settings?.stages === "focus_first" && a.slots !== undefined
      ? stagesOk(a.stages) : a.stages === undefined)
    && quantities(a, "eligible_final_score"));

const checksOk = (v) => v.checks_state === "unavailable"
  ? v.checks === null && v.checks_reason === "unreadable"
  : v.checks_state === "present" && v.checks_reason === null && (v.checks === null
    || (closed(v.checks, "modes shadow_hits") && object(v.checks.modes) && Object.values(v.checks.modes).every(text)
      && object(v.checks.shadow_hits) && Object.values(v.checks.shadow_hits).every((n) => Number.isInteger(n) && n >= 1)));

function reflectorProbeOk(v) {
  if (v.reflector_probe_state === "unavailable") return v.reflector_probe === null && v.reflector_probe_reason === "unreadable";
  const p = v.reflector_probe;
  if (v.reflector_probe_state !== "present" || v.reflector_probe_reason !== null
    || !closed(p, "launched_by state restarts last_exit") || !Number.isInteger(p.restarts) || p.restarts < 0
    || !(p.last_exit === null || Number.isInteger(p.last_exit))) return false;
  return p.state === "not_declared" ? p.launched_by === null && p.restarts === 0 && p.last_exit === null
    : ["running", "stopped", "exhausted"].includes(p.state) && ["lab", "operator"].includes(p.launched_by);
}

function reflectorProbeModel(c) {
  if (!c) return null;
  if (c.reflector_probe_state === "unavailable") return { availability: "unavailable", reason: c.reflector_probe_reason };
  return c.reflector_probe.state === "not_declared" ? null : { availability: "present", ...c.reflector_probe };
}

export function campaignShapeOk(view) {
  return object(view) && ["present", "unavailable"].includes(view.lab_notices_state) && reflectorProbeOk(view) && strings(view, "operation_id champions_state details_state") && maybeStrings(view, "details_reason")
    && (view.family !== 14 || maybeStrings(view, "gate_mode")) && rows(view.rounds, (r) => roundOk(r, view.family))
    && optionalRows(view.measurements, measurementOk) && optionalRows(view.champions,
      (v) => strings(v, "version origin") && maybeStrings(v, "attempt_id measurement_id"))
    && optionalRows(view.suspensions, (v) => strings(v, "owner reason evidence_kind") && counts(v, "seq"))
    && optionalRows(view.caveats, (v) => strings(v, "kind") && maybeCounts(v, "round") && maybeStrings(v, "attempt_id"))
    && optionalRows(view.lab_notices, (v) => v.code === "baseline_provenance_unknown"
      ? Object.keys(v).sort().join(" ") === "code measurement_id round samples"
        && Number.isInteger(v.round) && v.round >= 0 && text(v.measurement_id) && v.measurement_id.length > 0
        && Number.isInteger(v.samples) && v.samples > 0
      : strings(v, "code") && maybeCounts(v, "rounds") && countMap(v.by_reason) && countMap(v.reflection_errors))
    && optionalObject(view.outcome, (v) => maybeStrings(v, "outcome reason"))
    && optionalObject(view.policy, (v) => maybeStrings(v, "metric_primary"))
    && optionalObject(view.progress, (p) => strings(p, "phase")
      && optionalObject(p.rounds, (v) => counts(v, "completed") && maybeCounts(v, "current planned"))
      && optionalObject(p.coverage, (v) => counts(v, "scored expected"))
      && optionalObject(p.attempts, (v) => counts(v, "total") && optionalObject(v.current_round,
        (a) => counts(a, "closed running") && maybeCounts(a, "planned"))))
    && optionalObject(view.settings, (s) => maybeStrings(s, "stability round_stop") && (s.baseline_refresh === "off" || optionalObject(s.baseline_refresh,
      (v) => counts(v, "count") && maybeCounts(v, "every_rounds") && optionalRows(v.performed,
        (r) => counts(r, "round") && maybeStrings(r, "measurement_id replaced_measurement_id") && flags(r, "completed")))))
    && optionalObject(view.roles, (r) => ["editor", "reflector", "grouper"].every((key) => optionalObject(r[key],
      (v) => strings(v, "kind"))))
    && optionalObject(view.connection, (c) => strings(c, "state") && maybeStrings(c, "outcome") && counts(c, "attempts")
      && optionalObject(c.detail, (v) => maybeStrings(v, "code cause_code metric manifest_path observed_path")))
    && (view.family !== 15 || (checksOk(view) && optionalObject(view.independent, independentOk)));
}
export const supportedCampaign = (c) => (c?.schema === CAMPAIGN_SCHEMA && (c?.family === 14 || c?.family === 15)
  && campaignShapeOk(c) ? c : undefined);
// Core's supplied scope, never inferred from confidence data or spread to a containing scope.
// Explicit nulls identify campaign/round entries; an attempt must match both its id and its round.
const scopeText = (round, attemptId) => (attemptId !== null ? `Attempt ${attemptId} · round ${round}` : round !== null ? `Round ${round}` : "Campaign");
export function scopedCaveats(campaign, round = null, attemptId = null) {
  if (!supportedCampaign(campaign)) return [];
  const scope = scopeText(round, attemptId);
  return caveatsOf(campaign).filter((c) => c.round === round && c.attempt_id === attemptId)
    .map((c) => ({ ...caveatOf(c), scope }));
}
// "Limits on this result" of one scope: its caveats, led by the observe/unknown gate mark unless the observe
// caveat is already among them (spec family14 §4.2: campaign in Result, round and attempt in their rows).
export function limitsAt(campaign, mark, round = null, attemptId = null) {
  const caveats = scopedCaveats(campaign, round, attemptId);
  if (!mark || caveats.some((c) => c.code === OBSERVE)) return caveats;
  return [{ code: gateUnknown(campaign) ? "unknown" : OBSERVE, text: mark.text, scope: scopeText(round, attemptId) }, ...caveats];
}
export const campaignSchema = (campaign) => (!campaign || supportedCampaign(campaign) ? { ok: true, code: null } : UNSUPPORTED);
export function campaignEntry(entry) {
  if (entry?.data === undefined || supportedCampaign(entry.data)) return entry;
  return { ...entry, state: "error", data: undefined, code: UNSUPPORTED.code, stale: false };
}

export const screenOf = (operation) =>
  operation?.kind === "evaluation" ? "evaluation" : operation?.resume ? "suspended" : "campaign";

const mean = (row, metric) => (row && metric ? number(row.metrics?.[metric]?.mean) : "—");
const delta = (row, metric) => (row && metric && row.baseline?.delta?.[metric] ? signed(row.baseline.delta[metric].mean) : "—");

export function failureText(failure) {
  if (!failure) return null;
  return [failure.stage, failure.role, failure.error_class, failure.role_reason, failure.code]
    .filter((x) => x !== null && x !== undefined).join(" · ");
}

// The round baseline's version as text: details_state carries baseline_version (owner/campaign_views.py), so
// while the details read is unavailable the version is unknown — said so, never "—" as if there were none.
export const baselineText = (campaign, round) => round?.baseline_version
  ?? (campaign?.details_state === "unavailable" ? label("section_state", "unavailable").text : "—");

export function tokenCell(campaignMeasurements) {
  const present = campaignMeasurements.filter((m) => m.usage_state === "present" && m.usage);
  if (!present.length) {
    const state = campaignMeasurements[0]?.usage_state ?? null;
    return { name: "Target tokens", value: "—", sub: state ? label("section_state", state).text : "No measurements yet" };
  }
  const total = (key) => present.reduce((n, m) => n + (m.usage[key] ?? 0), 0);
  const unknown = total("unknown_calls");
  const partial = present.length < campaignMeasurements.length;
  return { name: "Target tokens", value: `${tokens(total("input_tokens"))} in · ${tokens(total("output_tokens"))} out`,
           sub: `Partial: target calls only · editor & scorer not counted${unknown ? ` · ${unknown} calls unknown` : ""}${partial ? ` · usage from ${present.length} of ${campaignMeasurements.length} measurements` : ""}` };
}

const FAMILY_TEXT = { 14: "manifest /15 · family 14", 15: "manifest /16 · family 15" };

// H1 shows only Owner's status: "Running · <phase>" in one pill, else the control state itself (spec family14 §3.1).
export function statusOf(operation, control, phase) {
  if (!operation) return control;
  return operation.control_state === "running" && phase ? { ...control, text: `${control.text} · ${label("phase", phase).text}` } : control;
}

function header(operation, campaign, id) {
  const control = label("control_state", operation?.control_state);
  const phase = operation?.progress?.phase ?? campaign?.progress?.phase ?? null;
  return {
    id,
    status: statusOf(operation, control, phase),
    starts: (operation?.starts ?? []).length,
    target: shortId(operation?.target_ref?.id),
    lastEvent: !operation ? "—" : operation.times_state === "present" ? time(operation.times?.last_event_at)
      : sectionText(operation.times_state ?? "not_started", operation.times_reason),
  };
}

function statCells(operation, campaign, ms) {
  const progress = operation?.progress ?? campaign?.progress;
  const missing = label("section_state", operation?.progress_state ?? "not_started").text;
  const rounds = progress?.rounds, attempts = progress?.attempts, coverage = progress?.coverage;
  const roundNow = rounds ? (rounds.current ?? rounds.completed) : null;
  const cur = attempts?.current_round;
  return [
    { name: "Rounds", value: rounds ? `Round ${roundNow ?? "—"}` : missing,
      sub: rounds ? `of ${rounds.planned ?? "?"} · ${rounds.completed ?? 0} completed` : "" },
    { name: "Attempts this round", value: cur ? `${cur.closed} closed · ${cur.running} running` : missing,
      sub: cur ? `of ${cur.planned ?? "?"} · ${attempts.total ?? 0} total` : "" },
    { name: "Coverage, current measurement", value: coverage ? `${coverage.scored}` : missing,
      sub: coverage ? `/ ${coverage.expected} scored` : "" },
    tokenCell(ms),
  ];
}

function lineageOf(operationId, campaign, byId, metric) {
  const rows = (campaign?.champions ?? []).map((c) => {
    const m = byId.get(c.measurement_id);
    return {
      version: c.version, mean: mean(m, metric), delta: delta(m, metric),
      origin: c.origin ?? "—", attempt: c.attempt_id ?? "—",
      gate: c.origin === "promoted_attempt" ? campaignMarkOf(campaign) : null,
      measurementHref: c.measurement_id ? href.measurement(operationId, c.measurement_id) : null,
      diffHref: c.version === "v0" ? null : href.diff(operationId, "initial", `champion:${c.version}`),
    };
  });
  return { state: campaign?.champions_state ?? "not_started", rows };
}

// A failed or unsettled reflection names its error class on the round pill (spec dev46 §4.4, PDR-0025).
function reflectionPill(reflection) {
  const pill = label("reflection_state", reflection?.state);
  const code = reflection?.error_class ?? reflection?.reason;
  return (reflection?.state === "failed" || reflection?.state === "unsettled") && code
    ? { ...pill, text: `${pill.text} · ${code}` } : pill;
}

// Result panel (spec family14 §3.1, §4.2). Core's outcome and Lab's notices are separate facts (PDR-0025): codes as
// given, neutral for unknown ones; nothing is recomputed (PDR-0017).
const pairs = (d) => Object.entries(d ?? {});
// Core b11 closes these rounds with outcome "clean", reason null; Lab counts that outcome.
const allRoundsClean = (n) => n.code === "no_candidate_measured" && n.rounds > 0
  && Object.keys(n.by_reason ?? {}).length === 1 && n.by_reason.clean > 0;
const CLEAN_ROUNDS_EXPLANATION = "All rounds were clean: the baseline had no failures, or the scorer supplied neither verdicts nor critical_pass for completed samples. Metrics do not build campaign focus.";
// Every lab_notices[] entry retains its recorded counts and scope.
function noticesOf(campaign) {
  return (campaign?.lab_notices ?? []).map((n) => n.code === "baseline_provenance_unknown"
    ? { code: n.code, known: true, text: "No stored baseline provenance", scope: `round ${n.round} · measurement ${n.measurement_id} · ${n.samples} samples`, explanation: "Core has no stored provenance for any baseline sample: the target may not have reported it or may not have reported it on every turn. With gate.provenance.kind: reported, each attempt treats all cases as affected and causal checks run on all train." }
    : n.code === "no_candidate_measured"
    ? { code: n.code, known: true, text: "No candidate measured", rounds: n.rounds,
        byReason: pairs(n.by_reason), reflectionErrors: pairs(n.reflection_errors),
        ...(allRoundsClean(n) ? { explanation: CLEAN_ROUNDS_EXPLANATION } : {}) }
    : { code: n.code, known: false, text: String(n.code), rounds: n.rounds ?? null, byReason: [], reflectionErrors: [] });
}
// The campaign's own limits; round and attempt scopes live in their rows. Statistical is not a limit.
const limitsOf = (campaign) => limitsAt(campaign, campaignMarkOf(campaign));
// No outcome (final review item 1): Owner sets it null whenever Core has no campaign_outcome, so "running" only
// while Owner's control state is active; a closed operation without one says that Core recorded none.
const ACTIVE = new Set(["reserved", "running", "stopping"]);
const pendingOf = (operation) => (operation?.resume ? "Not decided yet · suspended"
  : ACTIVE.has(operation?.control_state) ? "Not decided yet · running" : "No outcome recorded by Core");
export function resultOf(campaign, operation) {
  const o = campaign?.outcome;
  const decided = o !== null && o !== undefined;
  return {
    decided,
    outcome: decided ? { ...label("outcome", o.outcome), code: o.outcome ?? null, reason: o.reason ?? null } : null,
    pending: decided ? null : pendingOf(operation),
    limits: limitsOf(campaign),
    notices: [...noticesOf(campaign), ...(campaign?.lab_notices_state === "unavailable"
      ? [{ code: "baseline_provenance_unavailable", known: true, text: "Baseline provenance notices are unavailable" }] : [])],
  };
}

// These display models keep Core records in their supplied order, without choosing a final or a Champion.
export const fieldText = (value) => value === null || value === undefined ? "—" : String(value);
const measurementHref = (id, m) => m ? href.measurement(id, m) : null;
const detailFields = (detail) => ["code", "cause_code", "metric", "manifest_path", "observed_path"]
  .filter((name) => detail?.[name] !== null && detail?.[name] !== undefined)
  .map((name) => ({ name, value: fieldText(detail[name]) }));
function headerDetails(campaign, id) {
  const refresh = campaign?.settings?.baseline_refresh;
  const rows = (refresh?.performed ?? []).map((r) => ({
    round: r.round, label: `Baseline refresh before round ${r.round}`,
    measurement_id: r.measurement_id, replaced_measurement_id: r.replaced_measurement_id,
    completed: r.completed, measurementHref: measurementHref(id, r.measurement_id),
    replacedMeasurementHref: measurementHref(id, r.replaced_measurement_id),
  }));
  return {
    settings: campaign?.settings ?? null,
    roles: campaign?.roles ? ["editor", "reflector", "grouper"].map((name) => ({ name, kind: fieldText(campaign.roles[name]?.kind) })) : null,
    connection: campaign?.connection ? { state: campaign.connection.state, outcome: campaign.connection.outcome,
      attempts: campaign.connection.attempts, detail: detailFields(campaign.connection.detail) } : null,
    refresh: object(refresh) ? { count: refresh.count, every_rounds: refresh.every_rounds, entries: rows.length,
      completed: rows.filter((r) => r.completed === true).length, rows } : null,
  };
}

// Campaign setup (spec family14 §3.2, §4.8): values as recorded (`null` shows as "null"); the refresh `count` is
// kept apart from the records (`performed[]`) and the completed ones. The amber warn is the only derived text.
const recorded = (v) => v === undefined ? "—" : String(v);
export function setupOf(campaign, operationId) {
  const d = headerDetails(campaign, operationId);
  const reason = campaign?.details_reason ?? "unavailable";
  if (campaign?.details_state === "unavailable" || (!d.settings && !d.roles && !d.connection))
    return { state: "unavailable", reason, summary: [], warn: null, settings: [], roles: [], connection: null, refresh: null };
  const r = d.refresh, c = d.connection;
  const incomplete = r ? r.rows.filter((x) => x.completed === false).length : 0;
  const warn = [c && c.state !== "compatible" ? "connection not compatible" : null,
    incomplete > 0 ? `${incomplete} refresh not completed` : null].filter(Boolean).join(" · ");
  const format = FAMILY_TEXT[campaign?.family];
  const summary = [];
  if (d.settings) summary.push({ name: "Stability", value: recorded(d.settings.stability) },
    { name: "Round stop", value: recorded(d.settings.round_stop) });
  if (r) summary.push({ name: "Baseline refresh",
    value: `every ${recorded(r.every_rounds)} rounds · count ${recorded(r.count)} · ${r.entries} recorded · ${r.completed} completed` });
  if (d.roles) summary.push({ name: "Roles", value: d.roles.map((x) => `${x.name} ${x.kind}`).join(" · ") });
  if (c) summary.push({ name: "Connection", value: `${recorded(c.state)} · ${recorded(c.attempts)} ${c.attempts === 1 ? "attempt" : "attempts"}` });
  return {
    state: "present", reason: null, summary, warn: warn || null,
    settings: [...(format ? [{ name: "Format", value: format }] : []),
      ...(d.settings ? [{ name: "Stability", value: recorded(d.settings.stability) }, { name: "Round stop", value: recorded(d.settings.round_stop) }] : []),
      ...(r ? [{ name: "Refresh every", value: `${recorded(r.every_rounds)} rounds` }, { name: "Refresh count", value: recorded(r.count) }] : [])],
    roles: d.roles ?? [],
    connection: c ? { state: recorded(c.state), outcome: recorded(c.outcome), attempts: recorded(c.attempts), detail: c.detail } : null,
    refresh: r ? { everyRounds: r.every_rounds, count: r.count, records: r.entries, completed: r.completed,
      rows: r.rows.map((x) => ({ round: x.round, measurementId: x.measurement_id, measurementHref: x.measurementHref,
        replacedId: x.replaced_measurement_id, replacedHref: x.replacedMeasurementHref, completed: x.completed })) } : null,
  };
}

// Promotion is a history of all matching lineage entries. A present lineage without a match says nothing about rejection.
export function promotionOf(campaign, attempt) {
  if (!attempt) return null;
  if (campaign?.champions_state !== "present" || !Array.isArray(campaign?.champions))
    return { unavailable: campaign?.champions_state === "present" ? "unavailable" : campaign?.champions_state ?? "unavailable" };
  const versions = campaign.champions.filter((c) => c.origin === "promoted_attempt" && c.attempt_id === attempt.attempt_id).map((c) => c.version);
  return versions.length ? { versions } : null;
}

export function measurementLabelsOf(campaign, measurementId) {
  const labels = [];
  for (const r of campaign?.rounds ?? []) for (const f of r.final ?? []) {
    if (f.stored_measurement_id === measurementId || (f.members ?? []).some((m) => m.measurement_id === measurementId))
      labels.push(`Final of round ${r.round}, entry ${f.ordinal}`);
  }
  for (const r of campaign?.settings?.baseline_refresh?.performed ?? []) if (r.measurement_id === measurementId)
    labels.push(`Baseline refresh before round ${r.round} · completed: ${fieldText(r.completed)}`);
  return labels;
}
// Rounds & attempts (spec family14 §3.3, §4.2–§4.5). A final is an attempt's second step; the unit shown is one
// final[] record, linked by members[].attempt_id: every record is one row, in Core order, never merged — the rows of
// kind "final" are exactly round.final.length. Values as recorded; no phrase without a field (§4.5).
const known = (v) => v !== null && v !== undefined;
const finalistIds = (f) => (f.members ?? []).map((m) => m.attempt_id).filter(known);
const SIDE = { stored: "stored", fresh: "re-measured" };

// Champion column (replaces α): → vN only from a present lineage's promoted_attempt entry (PDR-0017).
function championCell(campaign, attempt) {
  const p = promotionOf(campaign, attempt);
  if (p?.unavailable) return { text: "unavailable", title: `champions_state: ${p.unavailable}` };
  if (p?.versions) return { text: p.versions.map((v) => `→ ${v}`).join(" · "), title: "transition recorded in lineage" };
  return { text: "—", title: "transition not recorded" };
}

// Core's decision on one contrast, by Owner's rule (owner/campaign_views.py final decisions; final review item 8):
// accepted; accepted false is a rejection unless its reason is final_incomplete; anything else is unavailable with
// its reason or unavailable code — the Measurement Decision panel's words.
const contrastState = (c) => (c.accepted === true ? "accepted"
  : c.accepted === false && c.reason !== "final_incomplete" ? "rejected" : "unavailable");
export function contrastDecision(c) {
  if (!c) return null;
  const state = contrastState(c);
  if (state === "accepted") return { text: "Accepted", tone: "green", code: c.reason ?? null };
  if (state === "rejected") return { text: "Not accepted", tone: "neutral", code: c.reason ?? null };
  return { text: "Unavailable", tone: "amber", code: c.reason || (c.unavailable ?? null) };
}
// Several finalists in one record: counts of Core's decisions, none chosen among them.
function jointDecision(contrasts) {
  if (!contrasts.length) return null;
  const n = (s) => contrasts.filter((c) => contrastState(c) === s).length;
  const acc = n("accepted"), rej = n("rejected"), open = n("unavailable");
  const text = [`${acc} accepted`, rej ? `${rej} not accepted` : null, open ? `${open} unavailable` : null].filter(Boolean).join(" · ");
  return { text, tone: acc ? "green" : "neutral", code: null };
}

// Selection as recorded on this record (§4.3); it is not a promotion (§4.4).
const selectionOf = (operationId, s) => (s
  ? { ...s, text: label("selection_reason", s.reason).text, measurementHref: measurementHref(operationId, s.measurement_id) } : null);

function finalRow(operationId, campaign, round, f, joint) {
  const members = finalistIds(f);
  const contrasts = f.contrasts ?? [];
  const single = members.length === 1 ? (f.members ?? []).find((m) => m.attempt_id === members[0]) : null;
  const contrast = single ? contrasts.find((c) => c.attempt_id === single.attempt_id) ?? null : null;
  const selection = selectionOf(operationId, f.selection);
  return {
    kind: "final", joint, ordinal: f.ordinal, label: `${joint ? "Joint final" : "Final"} · entry ${fieldText(f.ordinal)}`,
    members, finalists: members.length,
    measurementId: single?.measurement_id ?? null, href: measurementHref(operationId, single?.measurement_id),
    point: single ? number(single.point) : null, delta: single ? signed(contrast?.point) : null,
    decision: single ? contrastDecision(contrast) : jointDecision(contrasts),
    side: `vs ${baselineText(campaign, round)} · ${SIDE[f.champion_side] ?? fieldText(f.champion_side)}`,
    warns: [f.condition_changed_in_place === true ? "condition changed in place" : null,
      typeof f.listed === "number" && f.listed > 0 ? `listed ${f.listed}` : null].filter(Boolean),
    selection,
    detail: {
      members: (f.members ?? []).map((m) => ({ ...m, measurementHref: measurementHref(operationId, m.measurement_id) })),
      contrasts, selection, listed: f.listed,
      nextBaseline: { id: f.next_baseline_measurement_id ?? null, href: measurementHref(operationId, f.next_baseline_measurement_id) },
      championSide: f.champion_side, storedMeasurementId: f.stored_measurement_id ?? null,
      storedHref: measurementHref(operationId, f.stored_measurement_id),
      mode: f.mode, global: f.global, stability: f.stability, conditionChangedInPlace: f.condition_changed_in_place,
    },
  };
}

// Ruling R2: no confidence here — it lives on the Measurement screen.
function attemptRow(operationId, campaign, round, a, byId, metric) {
  const m = byId.get(a.measurement_id);
  return {
    kind: "attempt", id: a.attempt_id,
    href: a.measurement_id ? href.measurement(operationId, a.measurement_id) : null,
    measurementId: a.measurement_id ?? null,
    mean: mean(m, metric), delta: delta(m, metric),
    look: a.look ?? "—",
    champion: championCell(campaign, a),
    group: label("attempt_group", a.group),
    reason: a.reason ?? "",
    marks: [],
    limits: limitsAt(campaign, attemptMarkOf(campaign, a.attempt_id), round.round, a.attempt_id),
    failure: failureText(a.failure),
  };
}

// A finalist without a record (ruling R8): the placeholder follows Owner's final_decision — `pending` (running) or
// `unavailable` (round/campaign closed, final_incomplete); a decided or unrecorded one gets none.
function placeholderOf(a, inFinal) {
  const state = a.final_decision?.state;
  if (a.outcome !== "finalist" || inFinal.has(a.attempt_id) || (state !== "pending" && state !== "unavailable")) return null;
  return { kind: "pending-final", attemptId: a.attempt_id, state, reason: a.final_decision.reason ?? null };
}

// first_accepted: each record under its finalist (a record without a matching attempt goes last), a placeholder
// under its finalist. all_attempts: placeholders, then every record after the attempts, joint. Another or unknown
// round_stop: the same, records unlinked. Core order everywhere.
export function roundRowsOf(operationId, campaign, round, byId, metric) {
  const raw = round.attempts ?? [];
  const attempts = raw.map((a) => attemptRow(operationId, campaign, round, a, byId, metric));
  const read = Array.isArray(round.final);
  const finals = read ? round.final : [];
  const stop = campaign?.settings?.round_stop;
  const inFinal = new Set(finals.flatMap(finalistIds));
  raw.forEach((a, i) => {
    if (stop === "all_attempts" && inFinal.has(a.attempt_id)) attempts[i].marks.push("in joint final");
    if (read && !inFinal.has(a.attempt_id) && a.outcome !== "finalist" && a.group !== "in_progress") attempts[i].marks.push("no final");
  });
  const placeholders = read ? raw.map((a) => placeholderOf(a, inFinal)) : raw.map(() => null);
  if (stop !== "first_accepted")
    return [...attempts, ...placeholders.filter(Boolean),
      ...finals.map((f) => finalRow(operationId, campaign, round, f, stop === "all_attempts"))];
  const placed = new Set();
  const rows = [];
  raw.forEach((a, i) => {
    rows.push(attempts[i]);
    finals.forEach((f, j) => {
      if (placed.has(j) || !finalistIds(f).includes(a.attempt_id)) return;
      placed.add(j);
      rows.push(finalRow(operationId, campaign, round, f, false));
    });
    if (placeholders[i]) rows.push(placeholders[i]);
  });
  finals.forEach((f, j) => { if (!placed.has(j)) rows.push(finalRow(operationId, campaign, round, f, false)); });
  return rows;
}

// The round header in words (§3.3, §4.5): round_stop, stability when known, and the champion side only when every
// record of the round agrees on it (otherwise each final row states stored / re-measured).
function howOf(campaign, round) {
  const s = campaign?.settings;
  const stability = known(s?.stability) ? phrase("stability", s.stability) : null;
  const sides = new Set((Array.isArray(round.final) ? round.final : []).map((f) => f.champion_side));
  const side = sides.size === 1 ? [...sides][0] : null;
  return [known(s?.round_stop) ? phrase("round_stop", s.round_stop).text : null, stability?.known ? stability.text : null,
    known(side) ? phrase("champion_side", side).text : null].filter(Boolean).join(" · ");
}

// A round Core has not decided (outcome null): "In progress" while the operation runs, else no pill — never an
// empty "—" (Task 10 item 7; mock round 3).
const IN_PROGRESS = Object.freeze({ text: "In progress", tone: "blue", known: true });
const roundOutcomeOf = (r, running) => r.outcome === null || r.outcome === undefined
  ? (running ? IN_PROGRESS : null) : label("round_outcome", r.outcome);

function roundsOf(operationId, campaign, byId, metric, running) {
  return (campaign?.rounds ?? []).map((r) => ({
    round: r.round,
    header: {
      title: `Round ${r.round}`,
      baseline: baselineText(campaign, r),
      baselineMeasurementId: r.baseline_measurement_id ?? null,
      how: howOf(campaign, r),
      outcome: roundOutcomeOf(r, running),
      reflection: reflectionPill(r.reflection),
      limits: limitsAt(campaign, roundMarkOf(campaign, r.round), r.round),
    },
    // Family 15 has no final records (its comparisons are in the independent block): no final state at all.
    finalState: campaign?.family === 15 ? null
      : r.final === null || r.final === undefined ? { state: "unavailable", reason: campaign?.details_reason ?? "unavailable" } : { state: "present", reason: null },
    rows: roundRowsOf(operationId, campaign, r, byId, metric),
  }));
}

// Strip label (§3.3): a final measurement by its attempt and entry ("r01-a03 · final 2"), then its other roles;
// the full text is the title, the label may truncate.
function stripLabelOf(campaign, m) {
  const finals = [];
  for (const r of campaign?.rounds ?? []) for (const f of (Array.isArray(r.final) ? r.final : []))
    for (const x of f.members ?? []) if (x.measurement_id === m.measurement_id)
      finals.push(`${x.attempt_id ?? `${x.role} r${r.round}`} · final ${fieldText(f.ordinal)}`);
  const own = m.role === "baseline" ? `baseline r${m.round}` : finals.length ? null : (m.attempt_id ?? m.measurement_id);
  const refresh = (campaign?.settings?.baseline_refresh?.performed ?? []).filter((r) => r.measurement_id === m.measurement_id)
    .map((r) => `Baseline refresh before round ${r.round} · completed: ${fieldText(r.completed)}`);
  return [...finals, own, ...refresh].filter(Boolean).join(" · ");
}

function stripOf(operation, campaign, measurements, metric) {
  const list = campaign?.measurements ?? [];
  const opId = campaign?.operation_id ?? operation?.id;
  const bodies = list.map((m) => measurements?.[m.measurement_id]).filter((e) => e && e.data !== undefined).map((e) => e.data);
  const order = sampleOrder(bodies);
  const rows = list.map((m) => {
    const entry = measurements?.[m.measurement_id];
    const read = readOf(entry);
    const text = stripLabelOf(campaign, m);
    const base = { id: m.measurement_id, href: href.measurement(opId, m.measurement_id), read, label: text, title: text };
    if (read.state !== "ok") return base;
    const row = stripRow(entry.data, order, metric, measurementOpen(operation, campaign, m.measurement_id));
    return { ...base, cells: row.cells, stat: `${row.scored}/${row.total} scored` };
  });
  return { metric, rows };
}

export function campaignModel({ operation, campaign: body, measurements }) {
  const schema = campaignSchema(body);
  const campaign = supportedCampaign(body);  // another version: no field of it is read (codex r1 #3)
  const id = operation?.id ?? campaign?.operation_id;
  const list = campaign?.measurements ?? [];
  const byId = new Map(list.map((m) => [m.measurement_id, m]));
  const metric = primaryMetric("campaign", campaign);
  return {
    schema,
    result: resultOf(campaign, operation),
    header: header(operation, campaign, id),
    cells: statCells(operation, campaign, list),
    setup: setupOf(campaign, id),
    lineage: lineageOf(id, campaign, byId, metric),
    rounds: roundsOf(id, campaign, byId, metric, operation?.control_state === "running"),
    details: { state: campaign?.details_state ?? "present", reason: campaign?.details_reason ?? null },
    checks: checksModel(campaign),
    reflectorProbe: reflectorProbeModel(campaign),
    independent: independentModel(campaign),
    strip: stripOf(operation, campaign, measurements, metric),
  };
}
