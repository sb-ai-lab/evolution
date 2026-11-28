// screens/suspended.js — a suspended campaign (spec 2026-09-30 §3.2 screen 7; prototype lines 160–177): the
// cause (Lab's codes only), the resume verdict with the CLI's continuation line, the starts, the current
// round's baseline strip, rounds & attempts. Every read shows its own state; no explanation of our own.
import { html } from "../vendor/htm-preact.js";
import { ReportDownload } from "../ui/download.js";
import { paths } from "../lib/api.js";
import { href } from "../lib/route.js";
import { shortId } from "../lib/format.js";
import { readOf } from "../lib/resource.js";
import { suspendedModel } from "../lib/views/suspended.js";
import { statusCardOf } from "../lib/views/status-card.js";
import { useCampaign, useResource } from "../ui/hooks.js";
import { ResumeVerdictCard } from "../ui/command.js";
import { BlockReadState, ReadState, StaleMark, StatusPill, Unavailable } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { RoundTree } from "../ui/rounds.js";
import { StatusCard } from "../ui/status-card.js";
import { Disclosure } from "../ui/disclosure.js";
import { Lineage } from "../ui/lineage.js";
import { CampaignSetup } from "../ui/setup.js";
import { SampleStrip } from "../ui/samples.js";
import { SuspensionHistory } from "../ui/suspensions.js";
import { RoutingSection } from "../ui/routing.js";

function Starts({ starts }) {
  if (!starts.length) return html`<div class="panel pad muted small">No starts recorded</div>`;
  return html`<div class="panel starts">${starts.map((s) => html`<div class="starts-row" key=${s.start}>
    <span class="strong">#${s.start}</span><span class="mono">${s.target ? shortId(s.target) : "—"}</span>
    <span class="text-2">exit ${s.exit} · target cleanup ${s.cleanup}${s.failure && html` · <code>${s.failure}</code>`}</span>
    <${StatusPill} ...${s.settlement} /></div>`)}</div>`;
}

export function SuspendedScreen({ api, epoch, outage, operation, data, refresh, route, routing }) {
  const id = route.params.operation;
  const campaign = useCampaign(api, id, { epoch, outage, refresh });
  const needsReport = Boolean(data.resume?.target);
  const report = useResource(api, needsReport ? paths.report(id) : null, { epoch, outage, refresh });
  const current = data.progress?.rounds?.current ?? campaign.data?.progress?.rounds?.current ?? null;
  const baselineId = (campaign.data?.rounds ?? []).find((r) => r.round === current)?.baseline_measurement_id ?? null;
  const baseline = useResource(api, baselineId ? paths.measurement(id, baselineId) : null, { epoch, outage, refresh });
  const model = suspendedModel({ operation: data, campaign: campaign.data, report: needsReport ? report : undefined,
                                 baseline: baselineId ? baseline : undefined, campaignRead: readOf(campaign) });
  const read = readOf(campaign);
  // Defensive parity with CampaignScreen: a rejected schema is an unavailable read in every section.
  const campaignRead = read.state === "ok" && !model.schema.ok
    ? { state: "unavailable", code: model.schema.code } : read;
  const ready = campaignRead.state === "ok" && model.schema.ok;
  const statusCard = statusCardOf({ ...operation, data }, campaign, { screen: "suspended" });
  // The resume card's command comes from the report read: its staleness is the page's too (as on Project).
  const stale = operation.stale || read.stale || model.card.commandStale;
  const strip = model.strip;
  return html`<div class="screen" data-screen="suspended">
    <${Crumbs} items=${[{ text: "Experiments", href: href.experiments() }, { text: `Campaign ${shortId(id)}`, href: null }]} />
    <div class="title">
      <h1>Campaign <span class="mono">${shortId(id)}</span></h1>
      ${stale && html`<${StaleMark} />`}
    </div>
    <${StatusCard} card=${statusCard} />
    <${ResumeVerdictCard} ...${model.card} />
    <${SuspensionHistory} api=${api} operationId=${id} campaign=${campaign.data} campaignSettled=${campaign.state !== "loading"}
      campaignFresh=${campaign.state === "ok" && !campaign.stale && !outage} current outage=${outage}
      deleted=${data.results_state === "deleted" || campaign.code === "results_deleted"} />
    <section class="block"><div class="section-h"><h2>Rounds & attempts</h2></div>
      ${ready
        ? html`<${RoundTree} rounds=${model.rounds} details=${model.details} stale=${read.stale} />`
        : html`<${BlockReadState} read=${campaignRead} loading="Loading the campaign…" />`}</section>
    <section class="block" id="champion-lineage"><div class="section-h"><h2>Champion lineage</h2>
      ${ready && html`<span class="cap">mean score · ${strip.metric ?? "no primary metric"}</span>`}
      <a class="link cap block-link" href=${href.diff(id)}>Full diff →</a></div>
      <${BlockReadState} read=${campaignRead} loading="Loading the campaign…" />
      ${ready && html`<${Lineage} lineage=${model.lineage} stale=${read.stale} />`}</section>
    <section class="block"><div class="section-h"><h2>Starts</h2></div><${Starts} starts=${model.starts} /></section>
    <section class="block"><div class="section-h">
      <h2>${strip.round ? `Round ${strip.round} baseline` : "Baseline"}${strip.id && html` · <span class="mono">${strip.id}</span>`}</h2>
      ${strip.read.stale && html`<${StaleMark} />`}</div>
      ${strip.none
        ? html`<div class="panel pad muted small">No baseline measurement recorded for this round</div>`
        : strip.id
          ? html`<${SampleStrip} legend=${false} rows=${[{ id: strip.id, href: strip.href, read: strip.read,
              label: strip.id, cells: strip.cells, stat: strip.stat }]} />`
          : html`<${ReadState} read=${strip.read} loading="Loading the campaign…" />`}
      <div class="cap">Baseline breakdown by owner</div><${Unavailable} kind="lab" /></section>
    <div class="block"><${ReportDownload} api=${api} operation=${data} /></div>
    <${Disclosure} summary="Campaign setup & routing">
      <div class="disclosure-body"><${BlockReadState} read=${campaignRead} loading="Loading the campaign…" />
        ${ready && html`<${CampaignSetup} setup=${model.setup} stale=${read.stale} />`}
        <${RoutingSection} operationId=${id} entry=${routing} /></div>
    </${Disclosure}>
  </div>`;
}
