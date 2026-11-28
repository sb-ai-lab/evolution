// lib/views/measurement.js — screen 6 (spec 2026-09-30 §3.2): one lab-measurement/3 answer joined with its
// attempt row, its round's reflection and the diff summary against the round baseline. No judgments: groups,
// comparisons and reasons are Lab's codes; the reflector's text is shown as written (ADR-0019 p. 4).
import { paths } from "../api.js";
import { diffFiles } from "../diff.js";
import { number, signed, time } from "../format.js";
import { label } from "../labels.js";
import { readOf } from "../resource.js";
import { href } from "../route.js";
import { baselineRow, measurementOpen, primaryMetric, sampleOrder, stripRow } from "../samples.js";
import { attemptMarkOf, baselineText, campaignMarkOf, campaignSchema, failureText, limitsAt, measurementLabelsOf, promotionOf, scopedCaveats, supportedCampaign, tokenCell } from "./campaign.js";
import { confidenceOf } from "./confidence.js";
import { independentModel } from "./independent.js";
import { criticalText } from "./content.js";

const CAUTION = "Shown as the Reflector wrote it; it may quote reference answers (ADR-0019 p. 4).";
const OK_READ = Object.freeze({ state: "ok", code: null, stale: false });

// The Editor's failure line: failure is null while the details read is unavailable (owner/campaign_views.py) —
// then the section's reason, never "No failure recorded".
function editorOf(campaign, attempt) {
  if (!attempt) return null;
  const unavailable = campaign?.details_state === "unavailable" ? (campaign.details_reason ?? "unavailable") : null;
  return { failure: unavailable === null ? failureText(attempt.failure) : null, unavailable, tokens: "scope", reasoning: "lab" };
}

const roundOfAttempt = (campaign, attempt) =>
  (campaign?.rounds ?? []).find((r) => (r.attempts ?? []).some((a) => a === attempt
    || (attempt?.measurement_id != null && a.measurement_id === attempt.measurement_id && a.attempt_id === attempt.attempt_id)));

// Why an attempt has no diff to read, or null when it has one. has_tree is null while the champions read is
// unavailable, and baseline_version is null while the details read is unavailable (owner/campaign_views.py):
// neither is "no tree". Only has_tree === false is.
function diffBlocker(campaign, attempt, round) {
  if (attempt?.has_tree === false) return { none: "No candidate tree for this attempt" };
  if (attempt?.has_tree === null || attempt?.has_tree === undefined) return { unavailable: campaign?.champions_state ?? "unavailable" };
  if (!round?.baseline_version) return { unavailable: campaign?.details_reason ?? "unavailable" };
  return null;
}

export function diffPath(campaign, attempt, operationId) {
  campaign = supportedCampaign(campaign);
  if (!campaign) return null;
  if (!attempt || diffBlocker(campaign, attempt, roundOfAttempt(campaign, attempt)) !== null) return null;
  return paths.diff(operationId, `champion:${roundOfAttempt(campaign, attempt).baseline_version}`, `attempt:${attempt.attempt_id}`);
}

function reflectorOf(campaign, round) {
  if (campaign?.details_state === "unavailable") return { unavailable: campaign.details_reason ?? "unknown" };
  const reflection = round?.reflection;
  if (!round || !reflection) return { pending: label("section_state", "not_started") };
  return { round: round.round, state: label("reflection_state", reflection.state), hypotheses: reflection.hypotheses ?? [],
           caution: CAUTION, link: "core" };
}

function diffOf(entry, campaign, attempt, round, operationId) {
  if (!attempt) return { none: "Baseline measurement: no candidate" };
  const blocker = diffBlocker(campaign, attempt, round);
  if (blocker) return blocker;
  if (entry === null || entry === undefined) return { read: { state: "loading", code: null, stale: false }, state: "changed",
    files: [], added: 0, removed: 0, error: null, baseline: round.baseline_version, href: null };
  const read = readOf(entry);
  const files = read.state === "ok" ? diffFiles(entry.data) : { state: "changed", files: [], added: 0, removed: 0, error: null };
  const from = `champion:${round?.baseline_version}`;
  return { read, state: files.state, files: files.files, added: files.added, removed: files.removed, error: files.error,
           baseline: round?.baseline_version ?? null,
           href: round?.baseline_version && attempt ? href.diff(operationId, from, `attempt:${attempt.attempt_id}`) : null };
}

const codeCounts = (by) => Object.entries(by ?? {}).map(([code, n]) => `${code} ${n}`).join(" · ");
const known = (v) => v !== null && v !== undefined;

// The 5-cell strip (spec family14 §3.5): repairs live in the Coverage caption, the token caption is its own line.
function cardsOf(m, metric) {
  const baseline = m.baseline, cases = baseline?.cases;
  const mean = metric ? m.metrics?.[metric]?.mean : undefined;
  const delta = metric ? baseline?.delta?.[metric]?.mean : undefined;
  const excluded = m.coverage?.excluded_pairs;
  const coverageSub = [known(excluded) ? `${excluded} excluded` : null,
    m.repairs ? `repairs ${m.repairs.tries} tries · ${m.repairs.pairs_scored} pairs scored` : null].filter(known).join(" · ");
  const usage = tokenCell([m]);
  return [
    { name: `Mean ${metric ?? "score"} vs baseline`, value: number(mean),
      sub: baseline ? (baseline.state === "present" ? signed(delta) : label("section_state", baseline.state).text) : "no baseline for this measurement" },
    { name: "Cases better · same · worse", value: cases ? `${cases.better} · ${cases.same} · ${cases.worse}` : "—",
      sub: cases?.unpaired > 0 ? `${cases.unpaired} unpaired` : "" },
    { name: "Coverage · excluded pairs", value: m.coverage ? `${m.coverage.scored} / ${m.coverage.expected}` : "—", sub: coverageSub },
    { name: "Retries · call failures", value: m.retries ?? "—", sub: m.call_failures_by_code ? codeCounts(m.call_failures_by_code) : "" },
    { name: "Target tokens · partial", value: usage.value, sub: "", note: usage.sub },
  ];
}

// Decision (spec family14 §3.5, PDR-0017): the gate, core's final decision (as Owner reads it) and the lineage
// transition are three facts with their own empty states; none is inferred from another. Words for the gate's
// outcome and Owner's final_decision state (owner/campaign_views.py final_decisions) only; an unknown code is its
// raw code, neutral. "Not enough data" is amber, as attempt_group's not_enough_data in lib/labels.js.
const GATE = { finalist: ["Finalist", "blue"], rejected: ["Rejected", "neutral"], abandoned: ["Abandoned", "neutral"],
  insufficient_evidence: ["Not enough data", "amber"] };
const FINAL = { accepted: ["Accepted", "green"], rejected: ["Not accepted", "neutral"], pending: ["Pending", "blue"],
  unavailable: ["Unavailable", "amber"] };
const words = (table, code) => table[code] ?? [String(code), "neutral"];
function gateOf(a) {
  if (!known(a.outcome)) return null;
  const [text, tone] = words(GATE, a.outcome);
  return { text, tone, code: a.reason ?? null };
}
function finalOf(campaign, a) {
  const d = a.final_decision;
  if (!d) return a.outcome === "finalist" ? { missing: campaign?.details_reason ?? "unavailable" } : null;
  const [text, tone] = words(FINAL, d.state);
  return { state: d.state, text, tone, reason: d.reason ?? null, entries: finalEntries(campaign, a) };
}
const finalEntries = (campaign, a) => (campaign?.rounds ?? []).flatMap((r) => (r.final ?? [])
  .filter((f) => (f.members ?? []).some((m) => m.attempt_id === a.attempt_id))
  .map((f) => `Final of round ${r.round}, entry ${f.ordinal}`));
export function decisionOf(campaign, a) {
  if (!a) return null;
  const p = promotionOf(campaign, a);
  // Each promoted version with its own lineage measurement, in lineage order; none is chosen (final review item 12).
  const champion = p?.unavailable ? { kind: "unavailable", reason: p.unavailable }
    : p?.versions ? { kind: "promoted", versions: p.versions,
        measurements: campaign.champions.filter((c) => c.origin === "promoted_attempt" && c.attempt_id === a.attempt_id)
          .map((c) => ({ version: c.version, measurementId: c.measurement_id ?? null })) }
      : { kind: "none" };
  return { gate: gateOf(a), final: finalOf(campaign, a), champion };
}

// The H1's meta (spec family14 §3.5): the baseline an attempt is measured against, and the start time. Family 15
// compares against the round's champion, by a stored or a fresh measurement (spec 2026-10-19 Q2 as corrected): its
// meta is the comparison's reference label, or nothing when Core recorded no comparison — never the round baseline.
function headerOf(campaign, m, a, round, independent) {
  const against = campaign?.family === 15 ? independent?.attempt?.gate?.reference?.text ?? null
    : a && round ? `against ${baselineText(campaign, round)} · ${round.baseline_measurement_id ?? "—"}` : null;
  return { against, at: time(m.times?.started_at) };
}

// Family 15: the attempt's entry of the independent block (lib/views/independent.js), in place of family 14's
// Decision and Confidence panels; an unreadable block is its state with the reason, and unauthenticated evidence its kind.
function independentOf(campaign, a, round) {
  if (campaign?.family !== 15 || !a) return null;
  const block = independentModel(campaign);
  if (block.state !== "present") return { state: block.state, reason: block.reason, evidence: null, attempt: null };
  // The evidence kind travels with the entry: comparisons Lab could not authenticate are not "no entry recorded".
  const attempt = block.attempts.find((x) => x.round === round?.round && x.id === a.attempt_id);
  // This is the attempt's gate measurement; the global comparison belongs to a separate measurement.
  return { state: "present", reason: null, evidence: block.evidence, attempt: attempt ? { ...attempt, global: null } : null };
}

const critical = (v) => (v === true ? "yes" : v === false ? "no" : "—");

function casesOf(m, metric) {
  return { metric, rows: (m.samples ?? []).map((s) => ({
    case: s.case_id, sample: s.sample_id, caseId: s.case_id, sampleId: s.sample_id,
    baseline: s.baseline ? number(metric ? s.baseline.metrics?.[metric] : undefined) : "—",
    value: number(metric ? s.metrics?.[metric] : undefined),
    comparison: label("comparison", s.comparison),
    execution: s.execution_state, scoring: s.scoring_state,
    critical: critical(s.critical_pass), criticalMeta: criticalText(s.critical_pass), tries: s.tries ?? "—" })) };
}

export function measurementModel({ operation, campaign: body, measurement, diff, measurementId }) {
  const schema = campaignSchema(body);
  const campaign = supportedCampaign(body);
  const m = measurement?.measurement ?? {};
  const id = m.measurement_id ?? measurementId;
  const operationId = operation?.id ?? campaign?.operation_id;
  const attempt = (campaign?.rounds ?? []).flatMap((r) => (r.attempts ?? []).map((a) => [r, a])).find(([, a]) => a.measurement_id === id);
  const round = attempt ? attempt[0]
    : (campaign?.rounds ?? []).find((r) => r.baseline_measurement_id === id)
      ?? (m.round === null || m.round === undefined ? undefined : (campaign?.rounds ?? []).find((r) => r.round === m.round));
  const a = attempt?.[1] ?? null;
  const caveatRound = round?.round ?? m.round ?? null;
  const metric = primaryMetric("campaign", campaign);
  const order = sampleOrder([measurement]);
  const open = measurementOpen(operation, campaign, id);
  const family15 = campaign?.family === 15;
  const independent = independentOf(campaign, a, round);
  const rows = [];
  if (a) rows.push({ id: "baseline", label: `baseline ${baselineText(campaign, round)}`, href: null, read: OK_READ,
                     ...(({ cells, scored, total }) => ({ cells, stat: `${scored}/${total} scored` }))(baselineRow(measurement, order, metric)) });
  rows.push({ id, label: a ? a.attempt_id : m.role === "baseline" ? `baseline ${baselineText(campaign, round)}` : (m.role ?? id), href: null, read: OK_READ,
              ...(({ cells, scored, total }) => ({ cells, stat: `${scored}/${total} scored` }))(stripRow(measurement, order, metric, open)) });
  return {
    schema,
    header: headerOf(campaign, m, a, round, independent), id, operationId, role: m.role ?? null, round: m.round ?? round?.round ?? null, baselineVersion: round?.baseline_version ?? null,
    attempt: a ? { id: a.attempt_id, group: label("attempt_group", a.group), reason: a.reason ?? "", look: a.look ?? "—",
                   outcome: a.outcome ?? null } : null,
    // Family 14's gate, final decision and confidence do not exist in family 15: those panels are not shown.
    decision: family15 ? null : decisionOf(campaign, a),
    labels: measurementLabelsOf(campaign, id),
    confidence: a && !family15 ? confidenceOf(a.confidence) : null,
    independent,
    // "Limits on this result" (ruling R3: no gate pill in the H1) carry the fail-closed gate marks (final review I1):
    // the campaign's observe/unknown mark on every measurement page, as in Result, and the attempt's own mark.
    caveats: [...limitsAt(campaign, campaignMarkOf(campaign)),
      ...(caveatRound === null ? [] : scopedCaveats(campaign, caveatRound)),
      ...(a && caveatRound !== null ? limitsAt(campaign, attemptMarkOf(campaign, a.attempt_id), caveatRound, a.attempt_id) : [])],
    cards: cardsOf(m, metric),
    diff: diffOf(a ? diff : null, campaign, a, round, operationId),
    reflector: reflectorOf(campaign, round),
    editor: editorOf(campaign, a),
    strip: { metric, rows },
    cases: casesOf(m, metric),
    details: { state: campaign?.details_state ?? "present", reason: campaign?.details_reason ?? null },
  };
}

// The attempt row a measurement belongs to (null: a baseline or evaluation measurement, or none found).
export const attemptFor = (campaign, measurementId) =>
  (supportedCampaign(campaign)?.rounds ?? []).flatMap((r) => r.attempts ?? []).find((a) => a.measurement_id === measurementId) ?? null;
