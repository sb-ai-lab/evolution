// ui/bars.js — SegmentBar (COMPONENTS.md): 5px segments with a 3px gap, one per item, coloured by tone.
import { html } from "../vendor/htm-preact.js";

export const SegmentBar = ({ segments }) =>
  html`<div class="segbar">${segments.map((t) => html`<i class=${`seg seg-${t ?? "neutral"}`}></i>`)}</div>`;

// SkeletonStrip (COMPONENTS.md): the progress cells of an operation before its first core event (sample 09) —
// each cell's name over a grey placeholder bar, no value.
export const SkeletonStrip = ({ cells }) =>
  html`<div class="skeleton-strip">${cells.map((name) => html`<div class="skeleton-cell">
    <span class="service-label">${name}</span><i class="skeleton-bar"></i>
  </div>`)}</div>`;
