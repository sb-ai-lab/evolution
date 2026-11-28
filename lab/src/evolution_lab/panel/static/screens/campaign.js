// screens/campaign.js — a campaign (spec 2026-09-30 §3.2 screen 5; prototype lines 60–99): header, stat cells,
// champion lineage, rounds & attempts, the sample strip. Family 15 (spec 2026-10-19 D8) adds its independent
// acceptance block under the setup; its rounds carry no final records. Every block shows its own read's state (loading /
// Unavailable (<code>) / stale) — never an empty block for a read that did not answer.
import { html } from "../vendor/htm-preact.js";
import { ReportDownload } from "../ui/download.js";
import { href } from "../lib/route.js";
import { shortId } from "../lib/format.js";
import { readOf, worstRead } from "../lib/resource.js";
import { measurementOpen } from "../lib/samples.js";
import { campaignModel } from "../lib/views/campaign.js";
import { statusCardOf } from "../lib/views/status-card.js";
import { useAttemptFailures, useCampaign, useMeasurements } from "../ui/hooks.js";
import { checksText, reflectorProbeText } from "../lib/labels.js";
import { BlockReadState, StaleMark, Unavailable } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { StatCells } from "../ui/stats.js";
import { RoundTree } from "../ui/rounds.js";
import { StatusCard } from "../ui/status-card.js";
import { Disclosure } from "../ui/disclosure.js";
import { Lineage } from "../ui/lineage.js";
import { CampaignSetup } from "../ui/setup.js";
import { SampleStrip } from "../ui/samples.js";
import { IndependentBlock } from "../ui/independent.js";
import { SuspensionHistory } from "../ui/suspensions.js";
import { RoutingSection } from "../ui/routing.js";

function ChecksBlock({ model, stale }) {
  if (!model) return null;
  return html`<section class="block"><div class="section-h"><h2>${checksText.title}</h2></div>
    <div class=${`panel${stale ? " is-stale" : ""}`}>
      ${model.state === "unavailable" ? html`<div class="pad small"><${Unavailable} kind="state" reason=${model.reason} compact /></div>`
        : html`<div class="pad small mono">
          ${model.modes.length > 0 && html`<div>checks: ${model.modes.map(([name, mode]) => `${name}=${mode}`).join(", ")}</div>`}
          ${model.hits.map(([name, count]) => html`<div key=${name}>shadow hits: ${name} · count ${count}</div>`)}</div>
          <div class="pad tiny muted">${checksText.hint}</div>`}
    </div></section>`;
}

function ReflectorProbeBlock({ model, stale }) {
  if (!model) return null;
  return html`<section class="block"><div class="section-h"><h2>${reflectorProbeText.title}</h2></div>
    <div class=${`panel${stale ? " is-stale" : ""}`}>
      ${model.availability === "unavailable" ? html`<div class="pad small"><${Unavailable} kind="state" reason=${model.reason} compact /></div>`
        : html`<div class="pad small">
            <div>Launched by: ${model.launched_by}</div><div>State: ${model.state}</div>
            <div>Restarts: ${model.restarts}</div><div>Last exit: ${model.last_exit ?? "—"}</div></div>
          <div class="pad tiny muted">${reflectorProbeText.hint}</div>`}
    </div></section>`;
}

export function CampaignScreen({ api, epoch, outage, operation, data, refresh, route, routing }) {
  const id = route.params.operation;
  const campaign = useCampaign(api, id, { epoch, outage, refresh });
  const failedIds = (campaign.data?.rounds ?? []).flatMap((r) => r.attempts ?? []).filter((a) => a.failure).map((a) => a.attempt_id);
  const failures = useAttemptFailures(api, id, failedIds);
  const ids = campaign.data?.measurements?.map((m) => m.measurement_id) ?? [];
  const measured = useMeasurements(api, id, ids, { epoch, refresh, outage,
    isOpen: (m) => measurementOpen(data, campaign.data, m) });
  const model = campaignModel({ operation: data, campaign: campaign.data, measurements: measured.items });
  const read = readOf(campaign);
  const h = model.header;
  const stale = operation.stale || read.stale;
  const ready = read.state === "ok" && model.schema.ok;
  const card = statusCardOf({ ...operation, data }, campaign, { screen: "campaign" });
  const campaignRead = read.state === "ok" && !model.schema.ok
    ? { state: "unavailable", code: model.schema.code } : read;
  const token = model.cells.find((cell) => cell.name === "Target tokens");
  return html`<div class="screen" data-screen="campaign">
    <${Crumbs} items=${[{ text: "Experiments", href: href.experiments() }, { text: `Campaign ${shortId(id)}`, href: null }]} />
    <div class="title">
      <h1>Campaign <span class="mono">${shortId(id)}</span></h1>
      ${stale && html`<${StaleMark} />`}
      <span class="meta title-meta"><span>start ${h.starts}</span><span>target <span class="mono">${h.target}</span></span>
        <span>last event ${h.lastEvent}</span></span>
    </div>
    <${StatusCard} card=${card} />
    <${SuspensionHistory} api=${api} operationId=${id} campaign=${campaign.data} campaignSettled=${campaign.state !== "loading"}
      campaignFresh=${campaign.state === "ok" && !campaign.stale && !outage} outage=${outage}
      deleted=${data.results_state === "deleted" || campaign.code === "results_deleted"} />
    <section class="block"><div class="section-h"><h2>Rounds & attempts</h2>
      ${ready && html`<span class="cap">${campaign.data.family === 15 ? "Δ vs round champion" : "Δ against the round baseline"} · Champion = transition recorded in lineage</span>`}</div>
      <${BlockReadState} read=${campaignRead} loading="Loading the campaign…" />
      ${ready && html`<${RoundTree} rounds=${model.rounds} details=${model.details} failures=${failures} stale=${read.stale} />`}</section>
    <section class="block" id="champion-lineage"><div class="section-h"><h2>Champion lineage</h2>
      ${ready && html`<span class="cap">mean score · ${model.strip.metric ?? "no primary metric"}</span>`}
      <a class="link cap block-link" href=${href.diff(id)}>Full diff →</a></div>
      <${BlockReadState} read=${campaignRead} />
      ${ready && html`<${Lineage} lineage=${model.lineage} stale=${read.stale} />`}</section>
    ${(!ready || model.independent) && html`<section class="block"><div class="section-h"><h2>Independent acceptance</h2>
      <span class="cap">each scope by its own rule · against the round's champion · Core's numbers as recorded</span></div>
      <${BlockReadState} read=${campaignRead} />
      ${ready && html`<${IndependentBlock} model=${model.independent} stale=${read.stale} />`}</section>`}
    <section class="block"><div class="section-h"><h2>Samples</h2>
      <span class="cap">measurement × cases, same case order in every row</span>
      ${measured.read.stale && html`<${StaleMark} />`}</div>
      <${BlockReadState} read=${campaignRead} />
      ${ready && html`${model.strip.rows.length === 0 && html`<div class="panel pad muted small">No measurements yet</div>`}
        <${SampleStrip} rows=${model.strip.rows} />`}</section>
    <section class="block" data-section="target-tokens">
      <${BlockReadState} read=${campaignRead} />
      ${ready && html`<${StatCells} cells=${[token]} />`}</section>
    <div class="block"><${ReportDownload} api=${api} operation=${data} /></div>
    <${Disclosure} summary="Campaign setup & routing">
      <div class="disclosure-body"><${BlockReadState} read=${campaignRead} />
        ${ready && html`<${CampaignSetup} setup=${model.setup} stale=${read.stale} />`}
        <${RoutingSection} operationId=${id} entry=${routing} /></div>
    </${Disclosure}>
    <${Disclosure} summary="Checks & reflector probe">
      <div class="disclosure-body"><${BlockReadState} read=${campaignRead} />
        ${ready && html`<${ChecksBlock} model=${model.checks} stale=${read.stale} />
          <${ReflectorProbeBlock} model=${model.reflectorProbe} stale=${read.stale} />`}</div>
    </${Disclosure}>
  </div>`;
}
