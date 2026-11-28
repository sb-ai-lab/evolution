// lib/samples.js — the sample strip (spec 2026-09-30 §2.3): which measurement is open, one cell per sample,
// which measurements to (re)read; SAMPLE_CONCURRENCY is the queue's limit (lib/queue.js). Joins served fields only (§2.1).
import { ACTIVE } from "./api.js";

export const SAMPLE_CONCURRENCY = 4;
const FINISHED = "finished";

// Open: the operation is active AND (evaluation: its measurement | campaign: an in_progress attempt's
// measurement, or the current round's baseline in the baseline phase). Anything else is closed.
export function measurementOpen(operation, campaign, measurementId) {
  if (!operation || !ACTIVE.has(operation.control_state)) return false;
  if (operation.kind === "evaluation") return true;
  const rounds = campaign?.rounds ?? [];
  if (rounds.some((r) => (r.attempts ?? []).some((a) => a.group === "in_progress" && a.measurement_id === measurementId))) return true;
  const progress = operation.progress ?? campaign?.progress;
  if (progress?.phase !== "baseline") return false;
  const current = progress.rounds?.current;
  return rounds.some((r) => r.round === current && r.baseline_measurement_id === measurementId);
}

// Cell kinds in strip order with the legend's words (ui/samples.js draws the legend, the evaluation counters reuse
// them). `value` and `unranked` have no legend entry: their code is their label.
export const KIND_LABELS = Object.freeze({ pass: "score 1", partial: "partial", fail: "score 0", value: "value", unranked: "unranked",
  none: "no score", pending: "pending" });

export const sampleKey = (s) => `${s.case_id}\u0000${s.sample_id}`;
const samplesOf = (b) => b?.measurement?.samples ?? [];

export function sampleOrder(bodies) {
  const seen = new Set(), order = [];
  for (const b of bodies) for (const s of samplesOf(b)) {
    const key = sampleKey(s);
    if (!seen.has(key)) { seen.add(key); order.push(key); }
  }
  return order;
}

function valueCell(value, metric, metrics) {
  if (metric === null) return { kind: "unranked", title: Object.entries(metrics ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "no score" };
  if (typeof value !== "number" || !Number.isFinite(value)) return { kind: "none", title: "no score" };
  if (value < 0 || value > 1) return { kind: "value", title: `${metric} ${value}` };
  return { kind: value === 1 ? "pass" : value === 0 ? "fail" : "partial", title: `${metric} ${value}` };
}

export function cellOf(sample, metric, open) {
  if (!sample) return { kind: "absent", title: "not in this measurement" };
  const title = `${sample.case_id} · ${sample.sample_id}`;
  if (open && (sample.execution_state !== FINISHED || sample.scoring_state !== FINISHED)) return { kind: "pending", title: `${title} · pending` };
  const cell = valueCell(sample.metrics?.[metric], metric, sample.metrics);
  return { ...cell, title: `${title} · ${cell.title}` };
}

function row(samples, order, cellFor) {
  const byKey = new Map(samples.map((s) => [sampleKey(s), s]));
  const cells = order.map((key) => cellFor(byKey.get(key)));
  return { cells, scored: samples.filter((s) => s.scoring_state === FINISHED).length, total: samples.length };
}

export const stripRow = (b, order, metric, open) => row(samplesOf(b), order, (s) => cellOf(s, metric, open));

// The baseline partners Lab serves beside a candidate's samples (sample.baseline: a scored baseline sample of
// the same case and sample id). Its count is theirs: scored partners out of the candidate's samples.
export const baselineRow = (b, order, metric) => ({
  ...row(samplesOf(b), order, (s) => (s?.baseline
    ? { ...valueCell(s.baseline.metrics?.[metric], metric, s.baseline.metrics), title: `${s.case_id} · ${s.sample_id} · baseline` }
    : { kind: "absent", title: "no baseline sample" })),
  scored: samplesOf(b).filter((s) => s.baseline).length });

export const readPlan = (pairs, entries, isOpen) =>
  pairs.filter(([id]) => { const e = entries?.[id]; return !e || e.state !== "ok" || e.readOpen || isOpen(id); });

export function primaryMetric(kind, campaign, measurementBody) {
  if (kind === "campaign") return campaign?.policy?.metric_primary ?? null;
  const names = Object.keys(measurementBody?.measurement?.metrics ?? {});
  return names.length === 1 ? names[0] : null;
}
