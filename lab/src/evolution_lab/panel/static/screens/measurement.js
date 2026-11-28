// screens/measurement.js — one measurement of a campaign (spec 2026-09-30 §3.2 screen 6; prototype lines
// 196–284): an attempt's or a round baseline's. The route reaches the operation first: an evaluation's
// measurement (c-<O>) is the evaluation screen. Every read (operation, campaign, measurement, diff) shows its
// own state — loading, Unavailable (<code>), stale — never an empty block.
import { html, useState } from "../vendor/htm-preact.js";
import { paths } from "../lib/api.js";
import { href } from "../lib/route.js";
import { shortId } from "../lib/format.js";
import { readOf } from "../lib/resource.js";
import { attemptFor, diffPath, measurementModel } from "../lib/views/measurement.js";
import { mergeLive, overlayFor } from "../lib/views/shell.js";
import { useCampaign, useLiveRefresh, useOnDemand, useResource } from "../ui/hooks.js";
import { BlockReadState, ReadState, StaleMark, StatusPill, Unavailable } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { StatCells } from "../ui/stats.js";
import { ConfidencePanel, DecisionPanel, LimitList } from "../ui/confidence.js";
import { SampleStrip } from "../ui/samples.js";
import { IndependentEntry } from "../ui/independent.js";
import { CaseBrowser, casesCaption, selectedCase } from "../ui/cases.js";
import { DiffSummary } from "../ui/diff.js";
import { EditorBlock, ReflectorBlock } from "../ui/reasoning.js";
import { RoleCard, SampleDetail } from "../ui/content.js";
import { baselinePairOf, looksOf, roleOf } from "../lib/views/content.js";
import { EvaluationScreen } from "./evaluation.js";

// What a measurement without an attempt row is, from the served body's role (never assumed to be a baseline).
const roleText = (model) => model.role === "baseline" ? `baseline${model.round !== null ? ` of round ${model.round}` : ""}`
  : `${model.role ?? "measurement"}${model.round !== null ? ` · round ${model.round}` : ""}`;

// H1 (spec family14 §3.5, ruling R3): "Attempt <id>" with its group and reason, no gate pill — the gate mark is a
// limit in Confidence; the meta names the measurement, the baseline it is measured against and its start time.
// Family 15 (spec 2026-10-19 D8): the meta names the comparison's reference instead, and the attempt's independent
// entry stands where family 14's Decision and Confidence panels are.
// A baseline or other measurement keeps "Measurement <id>" and its role text.
function Header({ id, m, model, stale }) {
  const a = model?.attempt;
  const crumbs = [{ text: "Experiments", href: href.experiments() }, { text: `Campaign ${shortId(id)}`, href: href.operation(id) }];
  if (model?.round !== null && model?.round !== undefined) crumbs.push({ text: `Round ${model.round}`, href: null });
  crumbs.push({ text: a ? a.id : m, href: null });
  return html`<${Crumbs} items=${crumbs} />
    <div class="title">${a ? html`<h1>Attempt <span class="mono">${a.id}</span></h1>
        <${StatusPill} ...${a.group} />${a.reason && html`<span class="title-code">${a.reason}</span>`}`
      : html`<h1>Measurement <span class="mono">${m}</span></h1>`}
      ${stale && html`<${StaleMark} />`}
      <span class="meta title-meta">
        ${a ? html`<span>measurement <span class="mono">${model.id}</span></span>
            ${model.header.against && html`<span>${model.header.against}</span>`}<span>${model.header.at}</span>`
          : model && html`<span>${roleText(model)}</span>`}</span></div>
    ${(model?.labels ?? []).map((text, i) => html`<div class="small text-2" key=${i}>${text}</div>`)}`;
}

function CampaignMeasurement({ api, epoch, outage, operation, data, refresh, route }) {
  const id = route.params.operation, m = route.params.measurement;
  const campaign = useCampaign(api, id, { epoch, outage, refresh });
  const measurement = useResource(api, paths.measurement(id, m), { epoch, outage, refresh });
  const attempt = attemptFor(campaign.data, measurement.data?.measurement?.measurement_id ?? m);
  const [shownE, setShownE] = useState(false);
  const [shownR, setShownR] = useState(false);
  const [picked, setPicked] = useState(null);
  const round = campaign.data?.rounds?.find((r) => r.attempts?.some((a) => a === attempt));
  const attemptRead = useOnDemand(api, attempt ? paths.attempt(id, attempt.attempt_id, shownE) : null);
  const reflectionId = round?.reflection?.reflection_id;
  const reflectionRead = useOnDemand(api, reflectionId ? paths.reflection(id, reflectionId, shownR) : null);
  const path = diffPath(campaign.data, attempt, id);
  // A null path is never read: the model then says why there is no diff (no tree / a baseline).
  const diffRead = useResource(api, path, { epoch, outage, refresh });
  const cRead = readOf(campaign), mRead = readOf(measurement);
  if (cRead.state !== "ok" || mRead.state !== "ok") return html`<div class="screen" data-screen="measurement">
    <${Header} id=${id} m=${m} model=${null} stale=${false} />
    <${BlockReadState} read=${cRead.state !== "ok" ? cRead : mRead} loading="Loading the measurement…" /></div>`;
  const model = measurementModel({ operation: data, campaign: campaign.data, measurement: measurement.data,
    diff: path ? diffRead : null, measurementId: m });
  const stale = operation.stale || cRead.stale || mRead.stale;
  const shown = model.strip.metric ? `score · ${model.strip.metric}` : "several metrics: cells are not coloured";
  const baselineName = model.baselineVersion ? `Baseline ${model.baselineVersion}` : "Baseline";
  const columns = [{ text: "Case" }, { text: model.attempt ? baselineName : "Value", num: true },
    ...(model.attempt ? [{ text: "Candidate", num: true }] : []), { text: "Comparison" },
    { text: "Execution" }, { text: "Scoring" }, { text: "Critical" }, { text: "Tries", num: true }];
  const rows = model.cases.rows.map((r) => ({ key: `${r.case}\u0000${r.sample}`, caseId: r.caseId, sampleId: r.sampleId,
    comparison: r.comparison, meta: `tries ${r.tries} · execution ${r.execution} · scoring ${r.scoring} · ${r.criticalMeta}`,
    cells: [`${r.case} · ${r.sample}`, model.attempt ? r.baseline : r.value, ...(model.attempt ? [r.value] : []),
      r.comparison.text, r.execution, r.scoring, r.critical, String(r.tries)] }));
  // Spec family14 §3.7: an attempt's case is baseline | candidate, the baseline side read only for a verified
  // pair (baselinePairOf); a baseline or other measurement shows its own side only.
  const detail = (r) => {
    const baseline = model.attempt ? baselinePairOf(measurement.data.measurement, r.caseId, r.sampleId) : undefined;
    const heads = model.attempt
      ? { candidate: { title: "Candidate", code: `${model.attempt.id} · ${model.id}`, comparison: r.comparison },
          baseline: { title: baselineName, code: baseline.measurementId ?? null } }
      : { candidate: { title: "Sample", code: model.id } };
    return html`<${SampleDetail} api=${api} operationId=${id} measurementId=${model.id} caseId=${r.caseId}
      sampleId=${r.sampleId} baseline=${baseline} heads=${heads} />`;
  };
  return html`<div class="screen" data-screen="measurement">
    <${Header} id=${id} m=${m} model=${model} stale=${stale} />
    ${model.attempt && model.independent ? html`<div class=${`panel${cRead.stale || mRead.stale ? " is-stale" : ""}`}>
        <${IndependentEntry} independent=${model.independent} />
        ${model.caveats.length > 0 && html`<div class="pad"><${LimitList} limits=${model.caveats} /></div>`}</div>`
      : model.attempt ? html`<div class=${`panel measurement-decision${cRead.stale || mRead.stale ? " is-stale" : ""}`}>
        <${DecisionPanel} decision=${model.decision} />
        <${ConfidencePanel} c=${model.confidence} limits=${model.caveats} read=${readOf(attemptRead)}
          looks=${attemptRead.data ? looksOf(attemptRead.data.confidence) : null} /></div>`
      : html`<${LimitList} limits=${model.caveats} />`}
    <${StatCells} cells=${model.cards} wide />
    <section class="block"><div class="section-h"><h2>Changes</h2></div><${DiffSummary} diff=${model.diff} family=${campaign.data.family} /></section>
    <section class="block"><div class="section-h"><h2>Samples</h2><span class="cap">${shown}</span></div>
      <${SampleStrip} rows=${model.strip.rows} /></section>
    <section class="block"><div class="section-h"><h2>${model.attempt ? "How this candidate was made" : "Reflection this baseline opens"}</h2></div>
      <${ReflectorBlock} reflector=${model.reflector} content=${reflectionId && (reflectionRead.data
        ? html`<${RoleCard} model=${{ ...roleOf(reflectionRead.data, "reflector"), masking: reflectionRead.data.masking }} role="reflector" shown=${shownR} onShow=${() => setShownR(true)} />`
        : html`<${ReadState} read=${readOf(reflectionRead)} loading="Loading the reflector…" />`)} />
      ${model.editor && html`<${EditorBlock} editor=${model.editor} content=${attemptRead.data
        ? html`<${RoleCard} model=${{ ...roleOf(attemptRead.data, "editor"), masking: attemptRead.data.masking }} role="editor" shown=${shownE} onShow=${() => setShownE(true)} />`
        : html`<${ReadState} read=${readOf(attemptRead)} loading="Loading the editor…" />`} />`}</section>
    <section class="block"><div class="section-h"><h2>Cases</h2><span class="cap">${casesCaption(rows)}</span></div>
      <${CaseBrowser} columns=${columns} rows=${rows} selected=${selectedCase(rows, picked)} onSelect=${setPicked}
        detail=${detail} /></section>
  </div>`;
}

export function MeasurementScreen(props) {
  const { api, epoch, outage, live, route } = props;
  const id = route.params.operation;
  const refresh = useLiveRefresh(live, id);
  const operation = useResource(api, paths.operation(id), { epoch, outage, refresh });
  const data = mergeLive(operation.data, overlayFor(live, operation.epoch));
  if (data === undefined) return html`<div class="screen" data-screen="measurement">
    <${Crumbs} items=${[{ text: "Experiments", href: href.experiments() }, { text: shortId(id), href: null }]} />
    <${ReadState} read=${readOf(operation)} loading="Loading the experiment…" /></div>`;
  const Screen = data.kind === "evaluation" ? EvaluationScreen : CampaignMeasurement;
  return html`<${Screen} ...${props} operation=${operation} data=${data} refresh=${refresh}
    measurementId=${route.params.measurement} />`;
}
