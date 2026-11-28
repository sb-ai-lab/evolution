// screens/diff.js — the full diff of a campaign's versions (spec 2026-09-30 §3.2 screen 9; prototype lines
// 286–303). Two reads, each with its own state: the diff (loading / Unavailable (<code>) / stale) and the
// campaign the version pairs come from. A diff that does not parse is Unavailable, never an empty list.
import { html } from "../vendor/htm-preact.js";
import { paths } from "../lib/api.js";
import { href } from "../lib/route.js";
import { label } from "../lib/labels.js";
import { shortId } from "../lib/format.js";
import { readOf } from "../lib/resource.js";
import { diffModel, pairsState } from "../lib/views/diff.js";
import { useCampaign, useLiveRefresh, useResource } from "../ui/hooks.js";
import { ReadState, StaleMark, Unavailable } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { DiffView } from "../ui/diff.js";

export function DiffScreen({ api, epoch, outage, live, route }) {
  const { operation: id, from, to } = route.params;
  const refresh = useLiveRefresh(live, id);
  const diff = useResource(api, paths.diff(id, to === null ? null : from, to), { epoch, outage, refresh });
  const campaign = useCampaign(api, id, { epoch, outage, refresh });
  const dRead = readOf(diff), cRead = readOf(campaign);
  const crumbs = [{ text: "Experiments", href: href.experiments() }, { text: `Campaign ${shortId(id)}`, href: href.operation(id) },
                  { text: "Diff", href: null }];
  const model = dRead.state === "ok" ? diffModel({ diff: diff.data, from, to }) : null;
  const { state: pairsNote, pairs, attempts } = cRead.state === "ok" ? pairsState(campaign.data, id) : { state: null, pairs: [], attempts: null };
  const current = (p) => p.to === to && (to === null || p.from === from);
  return html`<div class="screen" data-screen="diff">
    <${Crumbs} items=${crumbs} />
    <div class="title"><h1>${model ? model.title : "Diff"}</h1>
      ${dRead.stale && html`<${StaleMark} />`}
      ${model && html`<span class="meta title-meta"><span class="mono">${model.digests}</span>
        ${!model.error && model.state === "changed" && html`<span>${model.files.length} files · +${model.added} −${model.removed}</span>`}</span>`}</div>
    <div class="diff-pairs">
      ${pairs.map((p) => html`<a class=${`chip${current(p) ? " active" : ""}`} key=${p.href} href=${p.href}
        aria-current=${current(p) ? "page" : undefined}>${p.text}</a>`)}
      ${pairsNote === "unavailable" && html`<${Unavailable} kind="state" reason="unavailable" compact />`}
      ${pairsNote === "not_started" && html`<span class="small muted">${label("section_state", "not_started").text}</span>`}
      ${pairsNote === "empty" && attempts === null && html`<span class="small muted">No versions to compare yet</span>`}
      ${attempts !== null && html`<div class="reason-line"><span class="tiny muted">Attempt versions</span>
        <${Unavailable} kind="state" reason=${attempts} compact /></div>`}
      ${cRead.state !== "ok" && html`<${ReadState} read=${cRead} loading="Loading the versions…" compact />`}
      ${cRead.stale && html`<${StaleMark} />`}</div>
    ${model ? html`<${DiffView} model=${model} />` : html`<${ReadState} read=${dRead} loading="Reading the diff…" />`}
  </div>`;
}
