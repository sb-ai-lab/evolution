import { html } from "../vendor/htm-preact.js";
import { label } from "../lib/labels.js";
import { StaleMark, StatusPill, Unavailable } from "./pill.js";

export function Lineage({ lineage, stale = false }) {
  return html`<div class=${`lineage${stale ? " is-stale" : ""}`}>
    ${stale && html`<${StaleMark} />`}
    ${lineage.state === "unavailable" && html`<${Unavailable} kind="state" reason="unavailable" />`}
    ${lineage.state === "not_started" && html`<span class="muted small">${label("section_state", "not_started").text}</span>`}
    ${lineage.state === "present" && lineage.rows.length === 0 && html`<span class="muted small">No champions yet</span>`}
    ${lineage.rows.map((c) => html`<div class="champion" key=${c.version}>
      <div class="champion-top"><span class="mono strong">${c.version}</span>${c.gate && html`<${StatusPill} ...${c.gate} />`}<span class="champion-mean">${c.mean}</span>
        <span class="small text-2">${c.delta}</span></div>
      <div class="mono tiny muted">${c.origin} · ${c.attempt}</div>
      <div class="tiny">${c.measurementHref && html`<a class="link" href=${c.measurementHref}>measurement</a>`}
        ${c.diffHref && html` <a class="link" href=${c.diffHref}>diff against original</a>`}</div>
    </div>`)}</div>`;
}

