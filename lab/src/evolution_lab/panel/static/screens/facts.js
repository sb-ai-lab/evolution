// screens/facts.js — screen 3, Facts & confirmations (prototype lines 140–150; spec §3.1).
import { html } from "../vendor/htm-preact.js";
import { LIST_POLL_MS, paths } from "../lib/api.js";
import { href } from "../lib/route.js";
import { factsModel } from "../lib/views/facts.js";
import { useResource } from "../ui/hooks.js";
import { StatusDot, Unavailable } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function FactRow({ row }) {
  return html`<div class="ftable-row">
    <div class="cell-stack"><span class="mono strong small">${row.id}</span>
      ${row.subject === null && html`<span class="tiny faint">project</span>`}</div>
    <span class=${`small fact-state fg-${row.state.tone}`}><${StatusDot} tone=${row.state.tone} />${row.state.text}</span>
    <span class="small text-3">${row.knownFrom}</span>
    <span class="small mono muted cell-text">${row.detail}</span>
    <span class="small fg-amber">${row.whyOutdated ?? ""}</span>
  </div>`;
}

function Confirmation({ c }) {
  return html`<div class="ctable-row">
    <span class="mono small">${c.id}</span>
    <span class="small">${c.claim}${c.implementation === null ? html` <span class="faint">· project</span>` : ""}</span>
    <span class="small mono muted cell-text">${c.path} · ${c.digest}</span>
    <span class="small text-3">${c.author} · via ${c.channel}</span>
    <span class="small muted">${c.revoked ? `revoked ${c.revoked}` : c.created}</span>
  </div>`;
}

export function FactsScreen({ api, epoch, outage, route }) {
  const id = route.params.project;
  const project = useResource(api, paths.project(id), { pollMs: LIST_POLL_MS, epoch, outage });
  const facts = useResource(api, paths.projectFacts(id), { pollMs: LIST_POLL_MS, epoch, outage });
  const confirmations = useResource(api, paths.projectConfirmations(id), { pollMs: LIST_POLL_MS, epoch, outage });
  const model = factsModel({ facts: facts.data, confirmations: confirmations.data });
  const name = project.data?.name ?? id;
  const c = model.counts;

  let table;
  if (facts.data === undefined) {
    table = facts.state === "error" ? html`<${Unavailable} kind="state" reason=${facts.code} />`
                                    : html`<div class="panel pad muted">Loading facts…</div>`;
  } else {
    table = html`<div class="panel ftable">
      <div class="ftable-head"><span>Fact</span><span>State</span><span>Known from</span><span>Source</span><span>Why outdated</span></div>
      ${model.rows.map((row, i) => html`<${FactRow} key=${`${row.id}:${i}`} row=${row} />`)}
    </div>`;
  }
  let list;
  if (confirmations.data === undefined) {
    list = confirmations.state === "error" ? html`<${Unavailable} kind="state" reason=${confirmations.code} />`
                                           : html`<div class="panel pad muted">Loading confirmations…</div>`;
  } else if (model.confirmations.length === 0) {
    list = html`<div class="panel pad empty">No confirmations — give one with <code>${`lab project confirm ${id} --claim …`}</code></div>`;
  } else {
    list = html`<div class="panel ctable">${model.confirmations.map((x) => html`<${Confirmation} key=${x.id} c=${x} />`)}</div>`;
  }
  return html`<div class="screen" data-screen="facts">
    <${Crumbs} items=${[{ text: "Projects", href: href.projects() }, { text: name, href: href.project(id) },
                        { text: "Facts", href: null }]} />
    <div class="title">
      <h1>Facts & confirmations</h1>
      ${facts.data && html`<span class="cap">${plural(c.total, "fact")} · ${c.holding} hold${c.outdated ? ` · ${c.outdated} outdated` : ""}${
        model.digest ? html` · materials <span class="mono">${model.digest}</span>` : ""}</span>`}
      ${(project.stale || facts.stale || confirmations.stale) && html`<span class="cap stale">stale · last answer shown</span>`}
    </div>
    ${table}
    <div class="section-h">Confirmations</div>
    ${list}
  </div>`;
}
