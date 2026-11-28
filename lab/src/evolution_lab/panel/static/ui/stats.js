// ui/stats.js — a row of stat cards (prototype lines 63–68): label, value, small sub-line.
// `note`: a caption on its own 11px line under the value (family14 Measurement strip: the token caption).
import { html } from "../vendor/htm-preact.js";

export const StatCells = ({ cells, wide = false }) => html`<div class=${wide ? "stat-cells wide" : "stat-cells"}>${cells.map((c) => html`
  <div class="stat-cell" key=${c.name}><div class="service-label">${c.name}</div>
    <div class="stat-value">${c.value}${c.sub && html` <span class="stat-sub">${c.sub}</span>`}</div>
    ${c.note && html`<div class="stat-note">${c.note}</div>`}</div>`)}</div>`;
