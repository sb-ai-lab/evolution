// screens/experiments.js — screen 4, All experiments (prototype lines 151–159; spec §3.1). No "Needs attention"
// chip: which experiment needs attention is a judgment Lab does not send (spec §2.1).
import { html, useState } from "../vendor/htm-preact.js";
import { LIST_POLL_MS, paths } from "../lib/api.js";
import { shortId } from "../lib/format.js";
import { experimentsModel } from "../lib/views/experiments.js";
import { mergeLive, overlayFor } from "../lib/views/shell.js";
import { useResource } from "../ui/hooks.js";
import { ReadState, Unavailable } from "../ui/pill.js";

const FILTERS = [["all", "All"], ["campaigns", "Campaigns"], ["evaluations", "Evaluations"]];
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
// Why the project count is unknown: the join's read state (experimentsModel counts.projectsRead).
const projectsNote = (read) => (read.state === "loading" ? "loading…" : html`Unavailable (<code>${read.code}</code>)`);

function Row({ row }) {
  return html`<a class="etable-row" href=${row.href}>
    <span class=${`status fg-${row.status.tone}`}><i class="dot"></i>${row.status.text}</span>
    <span class="mono" title=${row.id}>${shortId(row.id)}</span>
    <span class="text-3">${row.kind}</span>
    <span>${row.projectRead.state === "ok" ? row.project ?? "—" : html`<${ReadState} read=${row.projectRead} compact />`}</span>
    <span class="text-3 nums">${row.progress}</span>
    <span>${row.result}</span>
    <span class=${`small fg-${row.results.tone}`}>${row.results.text}</span>
    <span class="small muted">${row.started}</span>
    <span class="small muted">${row.took}</span>
  </a>`;
}

// projects, projectOperations: the shell's project list and per-project summary entries (the sidebar's
// reads), for the Project column: "—" only where every summary answered and none lists the row.
export function ExperimentsScreen({ api, epoch, outage, live, projects, projectOperations }) {
  const [filter, setFilter] = useState("all");
  const operations = useResource(api, paths.operations, { pollMs: LIST_POLL_MS, epoch, outage });
  // The page's one live stream repaints the active row; after its terminal frame the fresh read rules.
  const overlay = overlayFor(live, operations.epoch);
  const body = operations.data && { ...operations.data,
    operations: (operations.data.operations ?? []).map((op) => mergeLive(op, overlay)) };
  const model = experimentsModel({ operations: body, projects, projectOps: projectOperations }, filter);
  const stale = operations.stale || projects.stale || Object.values(projectOperations).some((e) => e.stale);

  let content;
  if (operations.data === undefined) {
    content = operations.state === "error" ? html`<${Unavailable} kind="state" reason=${operations.code} />`
                                           : html`<div class="panel pad muted">Loading experiments…</div>`;
  } else if (model.empty) {
    content = html`<div class="panel pad empty">No experiments yet</div>`;
  } else {
    content = html`<div class="panel etable">
      <div class="etable-head"><span>Status</span><span>ID</span><span>Kind</span><span>Project</span><span>Progress</span>
        <span>Result</span><span>Results</span><span>Started</span><span>Took</span></div>
      ${model.rows.map((row) => html`<${Row} key=${row.id} row=${row} />`)}
      ${model.rows.length === 0 && html`<div class="pad small muted">None of this kind</div>`}
    </div>`;
  }
  const c = model.counts;
  return html`<div class="screen" data-screen="experiments">
    <div class="title">
      <h1>Experiments</h1>
      ${operations.data && html`<span class="cap">${plural(c.all, "experiment")} across ${
        c.projects === null ? html`? projects (${projectsNote(c.projectsRead)})` : plural(c.projects, "project")} · ${c.active} active</span>`}
      ${stale && html`<span class="cap stale">stale · last answer shown</span>`}
      <div class="chips">${FILTERS.map(([key, text]) => html`<button type="button" key=${key}
        class=${key === filter ? "chip active" : "chip"} aria-pressed=${key === filter} onClick=${() => setFilter(key)}>${text}</button>`)}</div>
    </div>
    ${content}
  </div>`;
}
