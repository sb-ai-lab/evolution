// ui/samples.js — the sample strip (prototype lines 92–99): one row per measurement, one cell per sample in the
// same case order in every row. A row whose read did not answer shows that state instead of cells.
import { html } from "../vendor/htm-preact.js";
import { KIND_LABELS } from "../lib/samples.js";
import { ReadState, StaleMark } from "./pill.js";

const LEGEND = ["pass", "partial", "fail", "none", "pending"].map((k) => [k, KIND_LABELS[k]]);

export const SampleLegend = () => html`<div class="sample-legend">
  ${LEGEND.map(([kind, text]) => html`<span key=${kind}><i class=${`cell cell-${kind}`}></i>${text}</span>`)}
  <span class="muted">Undetermined and infrastructure cells: reason not read by Lab</span></div>`;

// onSelect(index) turns the cells into buttons (evaluation: a cell picks its case); `selected` is a cell index.
// bare: no panel of its own (the strip is a row inside another panel, no legend).
export const SampleStrip = ({ rows, legend = true, onSelect, selected, bare = false }) => html`<div class=${bare ? "sample-strip sample-strip-bare" : "panel pad sample-strip"}>
  ${legend && !bare && html`<${SampleLegend} />`}
  ${rows.map((r) => html`<div class="sample-row" key=${r.id}>
    ${r.href ? html`<a class="link mono small sample-label" href=${r.href} title=${r.title}>${r.label}</a>` : html`<span class="mono small sample-label" title=${r.title}>${r.label}</span>`}
    ${r.read.state === "ok"
      ? html`<div class="cells">${r.cells.map((c, i) => onSelect
          ? html`<button type="button" key=${i} class=${`cell cell-${c.kind} cell-pick` + (i === selected ? " selected" : "")} title=${c.title}
              aria-label=${c.title} aria-current=${i === selected ? "true" : undefined} onClick=${() => onSelect(i)}></button>`
          : html`<i key=${i} class=${`cell cell-${c.kind}`} title=${c.title}></i>`)}</div>
          <span class="tiny muted nums">${r.stat}</span>${r.read.stale && html`<${StaleMark} />`}`
      : html`<${ReadState} read=${r.read} loading="Reading this measurement…" compact />`}
  </div>`)}
</div>`;
