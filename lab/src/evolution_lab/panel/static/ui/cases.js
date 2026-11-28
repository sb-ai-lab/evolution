// ui/cases.js — the approved case detail (base mock screen 6; additions mock block 7): the selected case sits
// above the table with ↑/↓ (buttons, and the arrow keys while focus is inside), the selected row is highlighted.
// Selection is controlled by the screen (`selected` is a row key, `onSelect(key)` asks for another), so other
// blocks — the evaluation strip — can select a case too.
import { html } from "../vendor/htm-preact.js";

// lead: an optional first row of the Cases block, above the case detail (evaluation: the sample strip, spec family14 §3.8).
// columns: [{text, num?}]; rows: [{key, cells: [string], meta?: string}]; detail(row) renders the selected case,
// keyed by the row: another case mounts a fresh detail, so its disclosures start closed.
export function CaseBrowser({ columns, rows, selected, onSelect, detail, lead }) {
  const at = rows.findIndex((r) => r.key === selected);
  const current = at >= 0 ? rows[at] : null;
  const step = (d) => { const next = rows[at + d]; if (at >= 0 && next) onSelect(next.key); };
  const onKeyDown = (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    step(e.key === "ArrowDown" ? 1 : -1);
  };
  const grid = `cases-grid cols-${columns.length}`;
  return html`<div class="case-browser" onKeyDown=${onKeyDown}>
    ${lead && html`<div class="panel case-lead">${lead}</div>`}
    ${current && html`<div class="panel case-detail">
      <div class="case-head"><span class="mono strong case-id">${current.cells[0]}</span>
        ${current.meta && html`<span class="small muted">${current.meta}</span>`}
        <span class="case-steps">
          <button type="button" class="case-step" aria-label="Previous case" disabled=${at <= 0} onClick=${() => step(-1)}>↑</button>
          <button type="button" class="case-step" aria-label="Next case" disabled=${at >= rows.length - 1} onClick=${() => step(1)}>↓</button>
        </span></div>
      <div class="case-detail-body" key=${current.key}>${detail(current)}</div></div>`}
    <div class="panel cases">
      <div class=${grid + " cases-head"}>${columns.map((c) => html`<span key=${c.text} class=${c.num ? "nums" : ""}>${c.text}</span>`)}</div>
      ${rows.length === 0 && html`<div class="pad muted small">No samples in this measurement</div>`}
      ${rows.map((r) => html`<button type="button" key=${r.key} class=${grid + " cases-row" + (r.key === selected ? " selected" : "")}
          aria-current=${r.key === selected ? "true" : undefined} onClick=${() => onSelect(r.key)}>
        ${r.cells.map((c, i) => html`<span key=${i} class=${columns[i]?.num ? "nums" : i === 0 ? "mono" : ""}>${c}</span>`)}</button>`)}
    </div>
  </div>`;
}

// The screen's default (additions mock: the first case is selected): the asked-for key while it is a row,
// else the first row's.
export const selectedCase = (rows, key) => (rows.some((r) => r.key === key) ? key : rows[0]?.key ?? null);

// The caption is plain text over the rows: it lives with the view-models (lib/views/content.js).
export { casesCaption } from "../lib/views/content.js";
