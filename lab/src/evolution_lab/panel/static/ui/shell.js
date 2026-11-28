// ui/shell.js — the frame around every screen (README "Global layout"; prototype lines 9–35).
import { html } from "../vendor/htm-preact.js";
import { href } from "../lib/route.js";
import { shortId } from "../lib/format.js";
import { readOf } from "../lib/resource.js";
import { ReadState, StaleMark, StatusDot } from "./pill.js";

const cls = (...names) => names.filter(Boolean).join(" ");

// health: the /v1/health entry (lib/resource.js): versions from an answer, else the read's own state.
function HealthCard({ service, health }) {
  const body = health.data;
  const [text, tone] = service.ok
    ? [`Service ${body?.service_state ?? "ok"}`, "green"]
    : service.state === "unknown" ? ["Checking the service…", "neutral"] : ["Service unavailable", "amber"];
  const read = readOf(health);
  return html`<div class="health-card">
    <div class="health-state"><${StatusDot} tone=${tone} />${text}</div>
    ${read.state === "ok"
      ? html`<div class="mono">lab ${body.lab_version} · core ${body.core_version}${read.stale ? html` <${StaleMark} />` : ""}</div>`
      : html`<${ReadState} read=${read} loading="Checking versions…" compact />`}
  </div>`;
}

function Experiment({ e, selected }) {
  const inner = html`<${StatusDot} tone=${e.dot} /><span class="mono exp-id">${shortId(e.id)}</span>
    <span class="exp-label">${e.short}</span>`;
  return e.href
    ? html`<a class=${cls("nav-exp", selected && "active")} href=${e.href} title=${e.label}>${inner}</a>`
    : html`<div class="nav-exp dim" title=${e.label}>${inner}</div>`;
}

function Project({ p, route }) {
  const on = (screen) => route.screen === screen && route.params.project === p.id;
  return html`<div class="nav-project-group">
    <a class=${cls("nav-item nav-project", on("project") && "active")} href=${p.href}>
      <span class="nav-project-name">${p.name}</span><span class=${`nav-project-state fg-${p.state.tone}`}>${p.state.text}</span>
    </a>
    <a class=${cls("nav-item nav-sub", on("facts") && "active")} href=${p.factsHref}>Facts & confirmations</a>
    <div class="nav-sub faint">Experiments</div>
    <div class="nav-tree" data-experiments=${p.read.state}>
      <${ReadState} read=${p.read} compact />
      ${p.read.state === "ok" && p.experiments.length === 0 && html`<div class="nav-exp dim">none yet</div>`}
      ${p.read.stale && html`<${StaleMark} />`}
      ${p.experiments.map((e) => html`<${Experiment} key=${e.id} e=${e} selected=${route.params.operation === e.id} />`)}
    </div>
  </div>`;
}

export const ThemeToggle = ({ theme, onToggle }) =>
  html`<button type="button" class="theme-toggle" onClick=${onToggle}>${theme === "dark" ? "Light theme" : "Dark theme"}</button>`;

// sidebar: sidebarModel(...); projectsRead: readOf(the /v1/projects read) — the list's own state;
// service: serviceState(panel health); health: the /v1/health entry (lib/resource.js).
export function Sidebar({ sidebar, projectsRead, service, health, route, theme, onToggleTheme }) {
  const top = (screen) => cls("nav-item", route.screen === screen && "active");
  return html`<aside class="sidebar">
    <a class="brand" href=${href.projects()}><i class="brand-mark"></i><span class="brand-name">Lab</span></a>
    <${HealthCard} service=${service} health=${health} />
    <nav class="nav-group">
      <a class=${top("projects")} href=${href.projects()}>All projects</a>
      <a class=${top("experiments")} href=${href.experiments()}>All experiments</a>
      <a class=${cls(top("states"), "muted")} href=${href.states()}>State reference</a>
    </nav>
    <nav class="nav-group">
      <div class="nav-h">Projects</div>
      <${ReadState} read=${projectsRead} compact />
      ${sidebar.map((p) => html`<${Project} key=${p.id} p=${p} route=${route} />`)}
    </nav>
    <div class="sidebar-foot"><${ThemeToggle} theme=${theme} onToggle=${onToggleTheme} /></div>
  </aside>`;
}

// items: [{text, href|null}] — the last one is usually the current screen (href null).
export const Crumbs = ({ items }) => html`<nav class="crumbs">${items.map((item, i) => html`
  ${i > 0 && html`<span class="sep">/</span>`}${item.href ? html`<a href=${item.href}>${item.text}</a>` : html`<span>${item.text}</span>`}`)}</nav>`;

// state: serviceState().state, or "unreachable" when the panel itself did not answer.
export function ServiceBanner({ state, hint }) {
  if (state === "unreachable")
    return html`<div class="banner warn">The panel is not answering — is <code>lab panel</code> still running?</div>`;
  if (hint) return html`<div class="banner warn">Lab service unavailable — start it with <code>${hint}</code></div>`;
  return html`<div class="banner warn">Lab service unavailable (<code>${state}</code>)</div>`;
}
