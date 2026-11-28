// screens/projects.js — screen 1, Projects home (prototype lines 36–59; spec §3.1).
import { html } from "../vendor/htm-preact.js";
import { LIST_POLL_MS, paths } from "../lib/api.js";
import { href } from "../lib/route.js";
import { nowBlockOf, projectsModel, statusesToRead } from "../lib/views/projects.js";
import { activeOperationId, overlayFor } from "../lib/views/shell.js";
import { useResource, useResourceMap } from "../ui/hooks.js";
import { ReadState, StaleMark, StatusDot, Unavailable } from "../ui/pill.js";
import { SegmentBar } from "../ui/bars.js";

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The page's one live stream belongs to the active operation: its progress comes from `live`. The read is
// polled like the lists, so a failed first read recovers; its state stands where the progress would be.
function NowBlock({ api, id, live, sidebar, epoch, outage, serviceHealth, summaryReads }) {
  const operation = useResource(api, id ? paths.operation(id) : null, { pollMs: LIST_POLL_MS, epoch, outage });
  const project = sidebar.find((p) => p.experiments.some((e) => e.id === id));
  const model = nowBlockOf(serviceHealth, { ...operation, project: project?.name, live: overlayFor(live, operation.epoch) }, summaryReads);
  const run = model.running;
  const coverage = run?.coverage;
  const coverageText = coverage?.scored == null ? null : coverage.expected == null
    ? `${coverage.scored} scored` : `${coverage.scored} / ${coverage.expected} scored`;
  const kind = run?.kind ? run.kind[0].toUpperCase() + run.kind.slice(1) : "Operation";
  const percent = coverage?.expected > 0 && typeof coverage.scored === "number"
    ? Math.max(0, Math.min(100, 100 * coverage.scored / coverage.expected)) : 0;
  return html`<section data-section="now" class="now-block">
    ${model.state === "loading" || model.state === "unavailable"
      ? html`<div class="now-idle"><${ReadState} read=${model} loading="Loading service health…" compact /></div>`
      : model.state === "idle" ? html`<div class="now-idle">
        <div><${StatusDot} tone="amber" /> <span class="strong">Nothing is running</span> ${model.stale && html`<${StaleMark} />`}</div>
        ${model.lastClosed && html`<div class="small text-2">${model.lastClosed.kind ? model.lastClosed.kind[0].toUpperCase() + model.lastClosed.kind.slice(1) : "Operation"} <span class="mono">${model.lastClosed.shortId}</span>
          — ${model.lastClosed.label} at ${model.lastClosed.time}.</div>`}</div>`
      : html`<a class="active-bar now-running" href=${run.href}>
        <div class="now-running-content"><div class="service-label"><${StatusDot} tone="blue" /> Now running
          ${model.stale && html`<${StaleMark} />`}</div>
          <div class="strong">${kind} <span class="mono">${run.shortId}</span>${run.project && ` — project ${run.project}`}.</div>
          ${run.read.state !== "ok" ? html`<${ReadState} read=${run.read} compact />` : html`
            <div class="small text-2">${[run.phase, run.progressState, coverageText, run.lastEvent && `last event ${run.lastEvent}`].filter(Boolean).join(" · ")}</div>
            ${coverage?.expected != null && html`<div class="now-coverage"><div style=${`width:${percent}%`}></div></div>`}`}
        </div><span class="active-open">Open →</span></a>`}
  </section>`;
}

// Connect, Evolve and Champion come from the project's status: while it has no answer (loading, or failed
// with its code) the three cells say so; an answer shown from the last read carries the stale mark.
function StatusCells({ row }) {
  if (row.status && row.status.state !== "ok")
    return html`<div class="status-read"><${ReadState} read=${row.status} loading="Loading status…" compact /></div>`;
  return html`
    <div class="cell-stack gap5">
      ${row.connect.segments.length > 0 && html`<${SegmentBar} segments=${row.connect.segments} />`}
      ${row.failure ? html`<span class="small muted">Failed: <code>${row.failure}</code></span>`
                    : html`<span class="small muted">${row.connect.summary}${row.status?.stale ? html` <${StaleMark} />` : ""}</span>`}
    </div>
    <span class="small text-3">${row.evolve.cycles > 0 && `${plural(row.evolve.cycles, "cycle")} · `}<span
      class=${`fg-${row.evolve.tone}`}>${row.evolve.text}</span></span>
    <span class="small mono">${row.champion ?? "—"}</span>`;
}

function Row({ row }) {
  return html`<a class="ptable-row" href=${row.href} data-project=${row.id}>
    <div class="cell-stack"><span class="strong">${row.name}</span><span class="mono faint small">${row.impl}</span></div>
    <span class=${`small fg-${row.state.tone}`}>${row.state.text}</span>
    <${StatusCells} row=${row} />
    <span class="small muted">${row.created}</span>
  </a>`;
}

// Without a /v1/health answer the strip says why (loading / Unavailable with the code), not four "—".
function ServiceStrip({ cells, read }) {
  if (read.state !== "ok")
    return html`<div class="service-strip"><div class="service-cell"><span class="service-label">Service</span>
      <${ReadState} read=${read} loading="Loading service health…" compact /></div></div>`;
  return html`<div class="service-strip">${cells.map((c, i) => html`<div class="service-cell">
    <span class="service-label">${c.label}${i === 0 && read.stale ? html` <${StaleMark} />` : ""}</span>
    <span class=${i === 0 ? "" : "mono"}>${c.value}</span>
    ${c.sub && html`<span class="faint small mono">${c.sub}</span>`}
  </div>`)}</div>`;
}

// serviceHealth: the shell's /v1/health entry (stale during an observed outage).
export function ProjectsScreen({ api, epoch, outage, projects, health, serviceHealth, live, sidebar, projectOperations }) {
  const list = projects.data?.projects ?? [];
  const statusPaths = Object.fromEntries(statusesToRead(projects.data).map((id) => [id, paths.projectStatus(id)]));
  const statuses = useResourceMap(api, statusPaths, { pollMs: LIST_POLL_MS, epoch, outage });
  const model = projectsModel({ projects: projects.data, statuses: statuses.items, health: serviceHealth });
  const activeId = activeOperationId(serviceHealth?.data);
  const panelFailure = health.error ?? (health.panel && health.panel.service?.state !== "ok" ? "service_unavailable" : null);
  const nowHealth = panelFailure ? (serviceHealth?.data === undefined
    ? { state: "error", code: panelFailure, stale: false } : { ...serviceHealth, stale: true }) : serviceHealth;
  const summaryReads = Object.fromEntries(list.map((p) => [p.id, projectOperations?.[p.id]]));
  if (projects.data === undefined || projects.stale) summaryReads.__projects_read__ = { state: "loading" };

  let body;
  if (projects.data === undefined) {
    body = projects.state === "error" ? html`<${Unavailable} kind="state" reason=${projects.code} />`
                                      : html`<div class="panel pad muted">Loading projects…</div>`;
  } else if (model.empty) {
    body = html`<div class="panel pad empty">No projects yet — create one with <code>${"lab project demo <id>"}</code> or <code>${"lab project create <id> …"}</code></div>`;
  } else {
    body = html`<div class="panel ptable">
      <div class="ptable-head"><span>Project</span><span>State</span><span>Connect</span><span>Evolve</span>
        <span>Champion</span><span>Created</span></div>
      ${model.rows.map((row) => html`<${Row} key=${row.id} row=${row} />`)}
    </div>`;
  }
  return html`<div class="screen" data-screen="projects">
    <div class="title">
      <h1>Projects</h1>
      ${projects.data && html`<span class="cap">${plural(list.length, "project")} in this Lab home</span>`}
      ${(projects.stale || statuses.stale) && html`<span class="cap stale">stale · last answer shown</span>`}
    </div>
    <${NowBlock} api=${api} id=${activeId} live=${live} sidebar=${sidebar} epoch=${epoch} outage=${outage} serviceHealth=${nowHealth} summaryReads=${summaryReads} />
    ${body}
    <${ServiceStrip} cells=${model.service} read=${model.serviceRead} />
  </div>`;
}
