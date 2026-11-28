// screens/project.js — screen 2, Project (prototype lines 101–118; spec §3.1, §3.3; spec 2026-10-19
// project-history): header, a resume banner with the CLI's continuation line per suspended cycle, the current
// (else latest) run as a full pipeline, then the other runs newest first, each opened on demand.
import { html } from "../vendor/htm-preact.js";
import { LIST_POLL_MS, paths } from "../lib/api.js";
import { href } from "../lib/route.js";
import { cyclesToRead, projectModel } from "../lib/views/project.js";
import { mergeLive, overlayFor } from "../lib/views/shell.js";
import { useResource, useResourceMap } from "../ui/hooks.js";
import { StatusPill, Unavailable } from "../ui/pill.js";
import { Crumbs } from "../ui/shell.js";
import { Disclosure } from "../ui/disclosure.js";
import { StepPipeline } from "../ui/pipeline.js";
import { ResumeVerdictCard } from "../ui/command.js";

const pathsFor = (ids, path) => Object.fromEntries(ids.map((id) => [id, path(id)]));

// projectOperations: the shell's per-project summary entries (the sidebar's read), for Lab's cycle labels.
export function ProjectScreen({ api, epoch, outage, route, live, projectOperations }) {
  const id = route.params.project;
  const opts = { pollMs: LIST_POLL_MS, epoch, outage };
  const project = useResource(api, paths.project(id), opts);
  // A project that is not ready has no status to read (its record carries the failure instead).
  const ready = project.data?.state === "ready";
  const status = useResource(api, ready ? paths.projectStatus(id) : null, opts);
  const statusData = ready ? status.data : undefined;
  const operations = useResourceMap(api, pathsFor(cyclesToRead(statusData), paths.operation), opts);
  // The page's one live stream repaints the active cycle; after its terminal frame the fresh reads rule.
  const overlay = overlayFor(live, operations.epoch);
  const merged = Object.fromEntries(Object.entries(operations.items).map(([k, e]) => [k, { ...e, data: mergeLive(e.data, overlay) }]));
  // The ready target is the report's (owner_resume_target.ready_target), read only under a target requirement.
  const withTarget = Object.entries(operations.data).filter(([, op]) => op?.resume?.target).map(([k]) => k);
  const reports = useResourceMap(api, pathsFor(withTarget, paths.report), opts);
  const model = projectModel({ project: project.data, status: statusData, summary: projectOperations?.[id]?.data,
                             operations: merged, reports: reports.items });

  const stale = project.stale || status.stale || operations.stale || reports.stale;
  let body;
  if (project.data === undefined) {
    body = project.state === "error" ? html`<${Unavailable} kind="state" reason=${project.code} />`
                                     : html`<div class="panel pad muted">Loading…</div>`;
  } else if (!ready) {
    body = html`<div class="panel pad muted">No scenarios while the project is ${model.header.state.text.toLowerCase()}${
      project.data.failure ? html`: <code>${project.data.failure}</code>` : "."}</div>`;
  } else if (statusData === undefined) {
    body = status.state === "error" ? html`<${Unavailable} kind="state" reason=${status.code} />`
                                    : html`<div class="panel pad muted">Loading scenarios…</div>`;
  } else {
    body = html`
      ${model.resume.map((r) => html`<${ResumeVerdictCard} key=${r.opId} ...${r} title=${`${r.title} · suspended`} tone="amber" />`)}
      ${model.focus ? html`<div class="section-h">${model.focus.label}</div><${StepPipeline} ...${model.focus.pipeline} />`
        : html`<div class="panel pad muted">Nothing has run in this project yet</div>`}
      ${model.history.length > 0 && html`<div class="section-h">History<span class="cap">newest first</span></div>
        <div class="panel run-history">${model.history.map((p) => html`<${Disclosure} key=${p.opId ?? p.title} summary=${p.title}
          meta=${html`<span class=${`fg-${p.state.tone}`}>${p.state.text}</span> · ${p.progress}`}>
          <div class="disclosure-body"><${StepPipeline} ...${p} bare /></div></${Disclosure}>`)}</div>`}`;
  }
  const h = model.header;
  return html`<div class="screen" data-screen="project">
    <${Crumbs} items=${[{ text: "Projects", href: href.projects() }, { text: project.data ? h.name : id, href: null }]} />
    <div class="title">
      <h1>${project.data ? h.name : id}</h1>
      ${project.data && html`<${StatusPill} ...${h.state} />`}
      ${stale && html`<span class="cap stale">stale · last answer shown</span>`}
      ${project.data && html`<span class="meta project-meta">
        <span>implementation <span class="mono text">${h.impl}</span></span>
        <span>working copy <span class="mono">${h.workingCopy}</span></span>
        <span>created ${h.created}</span>
      </span>`}
    </div>
    ${body}
    ${href.facts(id) && html`<a class="link small" href=${href.facts(id)}>Facts & confirmations →</a>`}
  </div>`;
}
