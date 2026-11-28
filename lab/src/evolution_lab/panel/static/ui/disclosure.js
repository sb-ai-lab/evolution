// ui/disclosure.js — shared expandable block of the family14 layout (native <details>, keyboard-accessible).
// Title text sits alone in .disclosure-title; meta and the warn chip are siblings. Uncontrolled unless `open` is given.
import { html } from "../vendor/htm-preact.js";

export function Disclosure({ summary, meta, warn, open, onToggle, children }) {
  const controlled = open !== undefined;
  return html`<details class="disclosure" open=${controlled ? open : undefined}
      onToggle=${onToggle ? (e) => onToggle(e.currentTarget.open, e) : undefined}>
    <summary><span class="chev" aria-hidden="true">›</span><span class="disclosure-title">${summary}</span>
      ${meta !== undefined && meta !== null && html`<span class="disclosure-meta muted">${meta}</span>`}
      ${warn !== undefined && warn !== null && html`<span class="chip-warn">${warn}</span>`}</summary>
    ${children}
  </details>`;
}
