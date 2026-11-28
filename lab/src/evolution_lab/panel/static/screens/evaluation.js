// screens/evaluation.js — an evaluation (spec 2026-09-30 §3.2 screen 8; prototype lines 179–194): header, stat
// cells, the sample strip (one row), cases with every metric. The measurement read shows its own state.
import { html, useState } from "../vendor/htm-preact.js";
import { paths } from "../lib/api.js";
import { ReportDownload } from "../ui/download.js";
import { href } from "../lib/route.js";
import { shortId } from "../lib/format.js";
import { readOf } from "../lib/resource.js";
import { evaluationModel } from "../lib/views/evaluation.js";
import { label } from "../lib/labels.js";
import { useResource } from "../ui/hooks.js";
import { BlockReadState, StaleMark, StatusPill } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { StatCells } from "../ui/stats.js";
import { SampleStrip } from "../ui/samples.js";
import { SampleDetail } from "../ui/content.js";
import { CaseBrowser, selectedCase } from "../ui/cases.js";
import { RoutingSection } from "../ui/routing.js";

export function EvaluationScreen({ api, epoch, outage, operation, data, refresh, route, measurementId, routing }) {
  const id = route.params.operation;
  // The selected case is the screen's (spec family14 §3.7): the first row until another is picked.
  const [picked, setPicked] = useState(null);
  const measurement = useResource(api, paths.measurement(id, measurementId ?? `c-${id}`), { epoch, outage, refresh });
  const read = readOf(measurement);
  const model = evaluationModel({ operation: data, measurement: measurement.data });
  const times = model.meta.times;
  const stale = operation.stale || read.stale;
  const columns = [{ text: "Case" }, { text: "Execution" }, { text: "Scoring" },
    ...model.metrics.map((n) => ({ text: n, num: true })), { text: "Critical" }, { text: "Tries", num: true }];
  const rows = model.cases.rows.map((r) => ({ key: `${r.case}\u0000${r.sample}`, caseId: r.caseId, sampleId: r.sampleId,
    meta: `tries ${r.tries} · execution ${r.execution} · scoring ${r.scoring} · ${r.criticalMeta}`,
    cells: [`${r.case} · ${r.sample}`, r.execution, r.scoring, ...model.metrics.map((n) => r.values[n]), r.critical, String(r.tries)] }));
  const current = selectedCase(rows, picked);
  // The strip is the Cases panel's first row; its cells follow the table's order (spec family14 §3.8).
  // Ruling R10: "1 pass · 1 fail · 0 excluded", the cell-kind code words as in the mock.
  const counts = html`<span class="strip-counts" title=${model.strip.metric ? `score · ${model.strip.metric}` : "several metrics: cells are not coloured"}>${[
    ...model.cases.counts.map((c) => `${c.n} ${c.text}`),
    ...(model.cases.excluded !== null ? [`${model.cases.excluded} excluded`] : [])].join(" · ")}</span>`;
  const strip = model.strip.rows.map((r) => ({ ...r, label: "Samples", stat: counts, read }));
  const mid = measurement.data?.measurement?.measurement_id;
  // An evaluation has no baseline: one side per case.
  const detail = (r) => html`<${SampleDetail} api=${api} operationId=${id} measurementId=${mid} caseId=${r.caseId}
    sampleId=${r.sampleId} heads=${{ candidate: { title: "Sample", code: mid } }} />`;
  return html`<div class="screen" data-screen="evaluation">
    <${Crumbs} items=${[{ text: "Experiments", href: href.experiments() }, { text: `Evaluation ${shortId(id)}`, href: null }]} />
    <div class="title">
      <h1>Evaluation <span class="mono">${shortId(id)}</span></h1>
      <${StatusPill} ...${label("control_state", data.control_state)} />
      ${stale && html`<${StaleMark} />`}
      <span class="meta title-meta">
        <span>${model.meta.results.text}</span>
        ${times.note !== undefined ? html`<span>${times.note}</span>`
          : html`<span>${times.first} → ${times.last}</span>`}
        ${model.meta.exit !== null && html`<span>exit ${model.meta.exit}</span>`}</span>
    </div>
    <${ReportDownload} api=${api} operation=${data} />
    <${RoutingSection} operationId=${id} entry=${routing} />
    <${BlockReadState} read=${read} loading="Loading the measurement…" />
    ${read.state === "ok" && html`
      <${StatCells} cells=${model.cards} wide />
      <section class=${`block${read.stale ? " is-stale" : ""}`}><div class="section-h"><h2>Cases</h2><span class="cap">${model.cases.caption}</span></div>
        <${CaseBrowser} columns=${columns} rows=${rows} selected=${current} onSelect=${setPicked} detail=${detail}
          lead=${html`<${SampleStrip} bare rows=${strip} selected=${rows.findIndex((r) => r.key === current)}
            onSelect=${(i) => rows[i] && setPicked(rows[i].key)} />`} /></section>`}
  </div>`;
}
