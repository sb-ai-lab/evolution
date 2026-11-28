// ui/diff.js — the diff summary on the measurement screen (prototype lines 218–228) and the full view (286–303).
import { html } from "../vendor/htm-preact.js";
import { label } from "../lib/labels.js";
import { ReadState, StaleMark, Unavailable } from "./pill.js";

const marks = (f) => f.markers.map((k) => label("nontext_kind", k).text).join(" · ");
// Counts, then markers, apart: "+1 −1 · Mode change".
const stat = (f) => [f.added === null ? "" : `+${f.added} −${f.removed}`, marks(f)].filter(Boolean).join(" · ");

export function DiffSummary({ diff, family }) {
  if (diff.none) return html`<div class="panel pad muted small">${diff.none}</div>`;
  if (diff.unavailable !== undefined) return html`<div class="panel pad"><${Unavailable} kind="state" reason=${diff.unavailable} compact /></div>`;
  if (diff.read.state !== "ok") return html`<div class="panel pad"><${ReadState} read=${diff.read} loading="Reading the diff…" /></div>`;
  return html`<div class="panel pad diff-summary">
    <div class="diff-head"><span class="strong">${family === 15 ? "Δ vs round champion" : `Changes against round baseline ${diff.baseline}`}</span>
      ${diff.error ? html`<${Unavailable} kind="state" reason=${diff.error} compact />`
        : html`<span class="small muted">${diff.files.length} files · +${diff.added} −${diff.removed}</span>`}
      ${diff.read.stale && html`<${StaleMark} />`}</div>
    ${!diff.error && diff.files.length === 0 && html`<div class="small muted">No changes</div>`}
    ${diff.files.map((f) => html`<div class="diff-file" key=${f.path}><span class="mono small">${f.path}</span>
      <span class="mono small nums text-2">${stat(f)}</span></div>`)}
    ${diff.href && html`<a class="link small" href=${diff.href}>Open full diff →</a>`}
    <div class="reason-line"><span class="tiny muted">File description</span><${Unavailable} kind="lab" compact /></div>
  </div>`;
}

const MARK = { add: "+", remove: "−", context: " ", note: "" };

// The full diff: a file list, then each file's hunks. Line text is a text node, never markup.
export function DiffView({ model }) {
  if (model.error) return html`<${Unavailable} kind="state" reason=${model.error} />`;
  if (model.state === "no_change") return html`<div class="panel pad muted small">No changes between these versions</div>`;
  return html`<div class="diff-layout">
    <div class="panel diff-files">${model.files.map((f) => html`<a class="diff-file-row" key=${f.path} href=${`#diff-${f.path}`}
      onClick=${(e) => { e.preventDefault(); document.getElementById(`diff-${f.path}`)?.scrollIntoView(); }}>
      <span class="mono small">${f.path}</span>
      <span class="mono small nums text-2">${stat(f)}</span></a>`)}</div>
    <div class="diff-bodies">${model.files.map((f) => html`<div class="panel diff-body" key=${f.path} id=${`diff-${f.path}`}>
      <div class="diff-body-head mono small">${f.path}</div>
      ${f.hunks.length === 0 && html`<div class="pad small muted">${f.markers.length ? marks(f) : "No text changes"}</div>`}
      ${f.hunks.map((h, i) => html`<div class="diff-hunk" key=${i}>
        <div class="diff-hunk-head mono">${h.header}</div>
        ${h.lines.map((l, j) => html`<div class=${`diff-line diff-${l.kind}`} key=${j}>${MARK[l.kind]}${l.text}</div>`)}</div>`)}
    </div>`)}</div></div>`;
}
