// lib/views/evaluation.js — screen 8 (spec 2026-09-30 §3.2): a standalone evaluation. One lab-measurement/3
// answer (id c-<operation>) beside the operation itself; counts and means only, no judgments.
import { date, duration, number, shortId, time } from "../format.js";
import { label } from "../labels.js";
import { KIND_LABELS, measurementOpen, primaryMetric, sampleOrder, stripRow } from "../samples.js";
import { sectionText } from "./shell.js";
import { tokenCell } from "./campaign.js";
import { casesCaption, criticalText } from "./content.js";

// Counters over the strip's own cells (spec family14 §3.8): pass and fail always, even at 0; other kinds only when
// present. Ruling R10: each counter reads as its cell-kind code word ("1 pass · 1 fail"); the legend keeps its captions.
export function stripCountsOf(cells) {
  return Object.keys(KIND_LABELS)
    .map((kind) => ({ kind, text: kind, n: cells.filter((c) => c.kind === kind).length }))
    .filter((c) => c.n > 0 || c.kind === "pass" || c.kind === "fail");
}

// "2 cases · 1 sample each" when every case has the same count, else the case browser's caption (additions mock
// block 8). Codex r1: one linear pass over a Map, so a case id such as `constructor` is counted like any other.
export function casesCaptionOf(rows) {
  const perCase = new Map();
  for (const r of rows) perCase.set(r.caseId, (perCase.get(r.caseId) ?? 0) + 1);
  const counts = [...perCase.values()];
  if (counts.length === 0 || counts.some((n) => n !== counts[0])) return casesCaption(rows);
  const k = counts[0];
  return `${counts.length} ${counts.length === 1 ? "case" : "cases"} · ${k} ${k === 1 ? "sample" : "samples"} each`;
}

const OK_READ = Object.freeze({ state: "ok", code: null, stale: false });

export function evaluationModel({ operation, measurement }) {
  const m = measurement?.measurement ?? {};
  const samples = m.samples ?? [];
  const metrics = Object.keys(m.metrics ?? {});
  const known = samples.filter((s) => s.critical_pass !== null && s.critical_pass !== undefined);
  const pass = known.filter((s) => s.critical_pass === true).length;
  const times = operation?.times;
  const cards = [
    ...metrics.map((name) => ({ name: `Mean ${name}`, value: number(m.metrics[name]?.mean), sub: `${m.metrics[name]?.count ?? "—"} samples` })),
    { name: "Coverage", value: m.coverage ? `${m.coverage.scored} / ${m.coverage.expected}` : "—",
      // A recorded 0 is shown (spec family14 §3.8; mock RULES): only a missing count leaves the caption empty.
      sub: m.coverage?.excluded_pairs !== null && m.coverage?.excluded_pairs !== undefined ? `${m.coverage.excluded_pairs} excluded` : "" },
    { name: "Critical pass", value: `${pass} / ${known.length}`, sub: known.length === 0 ? "no sample carries it" : "samples with a critical check" },
    { name: "Retries · call failures", value: m.retries ?? "—",
      sub: m.call_failures_by_code ? Object.entries(m.call_failures_by_code).map(([c, n]) => `${c} ${n}`).join(" · ") : "" },
    { ...tokenCell([m]), name: "Target tokens · partial" },
  ];
  const order = sampleOrder([measurement]);
  const metric = primaryMetric("evaluation", null, measurement);
  const row = stripRow(measurement, order, metric, measurementOpen(operation, null, m.measurement_id));
  const rows = samples.map((s) => ({
    case: s.case_id, sample: s.sample_id, caseId: s.case_id, sampleId: s.sample_id, execution: s.execution_state, scoring: s.scoring_state,
    values: Object.fromEntries(metrics.map((n) => [n, number(s.metrics?.[n])])),
    critical: s.critical_pass === true ? "yes" : s.critical_pass === false ? "no" : "—", criticalMeta: criticalText(s.critical_pass),
    tries: s.tries ?? "—" }));
  return {
    title: `Evaluation ${shortId(operation?.id)}`,
    meta: {
      results: label("results_state", operation?.results_state),
      times: operation?.times_state !== "present" ? { note: sectionText(operation?.times_state ?? "not_started", operation?.times_reason) }
        : { first: time(times?.first_event_at), last: time(times?.last_event_at), date: date(times?.first_event_at),
            took: duration(times?.first_event_at, times?.last_event_at) },
      exit: operation?.process?.exit_code ?? null,
    },
    cards, metrics,
    strip: { metric, rows: [{ id: m.measurement_id ?? "evaluation", label: "evaluation", href: null, read: OK_READ,
                              cells: row.cells, stat: `${row.scored}/${row.total} scored` }] },
    cases: { metrics, counts: stripCountsOf(row.cells), excluded: m.coverage?.excluded_pairs ?? null, rows,
             caption: casesCaptionOf(rows) },
  };
}
