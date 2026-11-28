// ui/setup.js — Campaign setup (spec family14 §3.2, §4.8): a collapsed block whose summary line always carries every
// field and an amber chip for a problem; expanded: Settings | Roles | Connection and the Baseline refresh table.
import { html } from "../vendor/htm-preact.js";
import { Disclosure } from "./disclosure.js";
import { Unavailable } from "./pill.js";

function Rows({ rows }) {
  return html`<dl class="kv">${rows.map((r) => html`<dt>${r.name}</dt><dd class="mono">${r.value}</dd>`)}</dl>`;
}

function MeasurementLink({ id, href }) {
  if (id === null || id === undefined) return html`<span class="mono">null</span>`;
  return href ? html`<a class="link mono" href=${href}>${id}</a>` : html`<span class="mono">${id}</span>`;
}

function Refresh({ refresh }) {
  if (refresh.rows.length === 0) return html`<div class="muted small">No refresh records</div>`;
  return html`<table class="setup-refresh"><thead><tr><th>Before round</th><th>Measurement</th><th>Replaced</th><th>Completed</th></tr></thead>
    <tbody>${refresh.rows.map((r, i) => html`<tr key=${i}><td>${String(r.round)}</td>
      <td><${MeasurementLink} id=${r.measurementId} href=${r.measurementHref} /></td>
      <td><${MeasurementLink} id=${r.replacedId} href=${r.replacedHref} /></td>
      <td>${r.completed === false ? html`<span class="chip-warn">no · not completed</span>` : html`<span class="mono">${String(r.completed)}</span>`}</td></tr>`)}</tbody></table>`;
}

// stale: shown from the campaign view's kept previous answer — dimmed (spec family14 §3.9).
export function CampaignSetup({ setup, stale = false }) {
  const cls = `panel setup${stale ? " is-stale" : ""}`;
  if (setup.state !== "present") return html`<div class=${cls}><div class="pad small"><b>Campaign setup</b> <${Unavailable} kind="state" reason=${setup.reason ?? "unavailable"} compact /></div></div>`;
  const c = setup.connection;
  return html`<div class=${cls}><${Disclosure} summary="Campaign setup" warn=${setup.warn}
      meta=${html`${setup.summary.map((s) => html`<span key=${s.name} class="setup-field"><span class="k">${s.name}</span> <span class="mono">${s.value}</span></span>`)}`}>
    <div class="setup-grid">
      <div><div class="lbl">Settings</div><${Rows} rows=${setup.settings} /></div>
      <div><div class="lbl">Roles</div>${setup.roles.length ? html`<${Rows} rows=${setup.roles.map((r) => ({ name: r.name, value: r.kind }))} />`
        : html`<span class="muted small">not recorded</span>`}</div>
      <div><div class="lbl">Connection check</div>${c ? html`<${Rows} rows=${[{ name: "State", value: c.state }, { name: "Outcome", value: c.outcome },
        { name: "Attempts", value: c.attempts }, ...c.detail]} />${c.detail.length === 0 && html`<div class="faint small">No detail recorded</div>`}`
        : html`<span class="muted small">not recorded</span>`}</div>
    </div>
    ${setup.refresh && html`<div class="setup-refresh-block"><div class="lbl">Baseline refresh</div><${Refresh} refresh=${setup.refresh} /></div>`}
  <//></div>`;
}
