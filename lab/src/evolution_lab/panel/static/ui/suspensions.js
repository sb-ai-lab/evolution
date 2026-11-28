// Shared suspension evidence/history. All values are escaped text; locators never become links.
import { html, useState } from "../vendor/htm-preact.js";
import { meterOf, suspensionModel, suspensionSeq } from "../lib/views/suspensions.js";
import { useSuspensions } from "./hooks.js";
import { BlockReadState, StaleMark } from "./pill.js";
import { Disclosure } from "./disclosure.js";
const literal = (value) => value === null ? "null" : String(value);
function Measurement({ id, link }) {
  return link ? html`<a class="link mono" href=${link}>${id}</a>` : html`<span class="mono">${id}</span>`;
}
// Spec §3.6: header seq · reason · owner · kind, then count / digest (first 8) / completeness.
function Completeness({ r }) {
  if (r.rows === null) return null;
  const digest = html`digest <span class="mono" title=${r.evidence.rows_digest}>${r.evidence.rows_digest.slice(0, 8)}</span>`;
  return r.truncated
    ? html`<span class="ev-count muted"><span class="chip-warn">Showing ${r.shown} of ${r.count} · truncated</span> ${digest}</span>`
    : html`<span class="ev-count muted">${r.count} ${r.count === 1 ? "row" : "rows"} · ${digest} · complete</span>`;
}
function Rows({ r }) {
  const fault = r.evidence_kind === "fault_rows";
  return html`<table class="grid ev-table"><thead><tr><th>Measurement</th><th>Case</th><th>Sample</th><th>Retry</th>
      ${fault ? html`<th>Reason</th><th>Code</th>` : html`<th>Owner</th>`}</tr></thead>
    <tbody>${r.rows.map((row, i) => html`<tr key=${i}><td><${Measurement} id=${row.measurement_id} link=${row.measurementHref} /></td>
      <td><code>${row.case_id}</code></td><td><code>${row.sample_id}</code></td><td>${row.retry_ordinal}</td>
      ${fault ? html`<td><code>${row.reason}</code></td><td><code>${literal(row.code)}</code></td>` : html`<td><code>${row.owner}</code></td>`}</tr>`)}</tbody></table>
    <div class="muted small ev-note">Rows that caused this pause; this is not the full remaining sample list.</div>`;
}
// Observed against the limit (ruling R9): the bar is count / denominator, the tick is max_rate when it is a rate.
// SVG percentage attributes only: the CSP forbids inline style.
function Meter({ c }) {
  const m = meterOf(c);
  if (m.fill === null && m.tick === null) return null;
  return html`<svg class="meter" aria-hidden="true">
    <rect class="meter-track" x="0" y="4" width="100%" height="6" rx="3" />
    ${m.fill !== null && html`<rect class="meter-fill" x="0" y="4" width=${`${m.fill * 100}%`} height="6" rx="3" />`}
    ${m.tick !== null && html`<line class="meter-limit" x1=${`${m.tick * 100}%`} x2=${`${m.tick * 100}%`} y1="0" y2="14" />`}
  </svg>`;
}
function Threshold({ e }) {
  const c = e.crossing;
  return html`<div class="th-cmp">
    <div><div class="lbl">Observed <span class="faint">· kind <code>${c.kind}</code></span></div>
      <div class="val">${c.count} <small>of ${c.denominator}</small></div>
      <${Meter} c=${c} />
      <div class="small muted">rate <span class="mono">${c.count}/${c.denominator}</span></div></div>
    <div><div class="lbl">Limit</div><div class="val">${c.max_count} <small>max_count</small></div>
      <div class="small"><span class="muted">max_rate</span> <span class="mono">${c.max_rate}</span></div></div>
    <div><div class="lbl">Window</div><dl class="kv"><dt>Owner</dt><dd class="mono">${c.owner}</dd>
      <dt>Window start</dt><dd class="mono">${literal(e.window_start)}</dd></dl></div></div>`;
}
function Stage({ e }) {
  return html`<div class="ev-body"><dl class="kv"><dt>Stage seq</dt><dd>${e.seq}</dd>
    <dt>Operation</dt><dd class="mono">${literal(e.operation_id)}</dd>
    <dt>Locator</dt><dd>${Object.hasOwn(e, "prefix") ? html`<code>${e.prefix}</code>` : html`<span class="faint">none for this reason</span>`}</dd></dl></div>`;
}
export function PauseEvidence({ record: r, current = false, historical = false }) {
  const e = r.evidence;
  return html`<div class="panel ev" data-evidence=${r.evidence_kind}>
    <div class="ev-head"><b class="ev-title">Why it paused</b><span class="mono">seq ${r.seq}</span><code>${r.reason}</code>
      <span class="muted">owner <span class="mono">${r.owner}</span> · <code>${r.evidence_kind}</code></span>
      ${r.evidence_kind === "champion_rows" && html`<span class="muted">for <${Measurement} id=${e.measurement_id} link=${r.measurementHref} /></span>`}
      ${current && html`<span class="chip-progress">Current pause</span>`}
      ${!current && historical && html`<span class="chip-neutral">Historical</span>`}
      <${Completeness} r=${r} /></div>
    ${r.rows !== null && html`<${Rows} r=${r} />`}
    ${r.evidence_kind === "threshold" && html`<${Threshold} e=${e} />`}
    ${r.evidence_kind === "stage_record" && html`<${Stage} e=${e} />`}
  </div>`;
}
export function SuspensionEvidence({ model, current = false }) {
  const rec = (record, isCurrent) => html`<${PauseEvidence} key=${record.seq} record=${record} current=${isCurrent} historical=${!isCurrent} />`;
  // Read states (additions mock "Read states", spec §3.9): deleted is its own state; stale keeps the previous
  // answer dimmed and names the newer pause when the campaign view has one; empty only after a successful read.
  const deleted = model.read.state === "unavailable" && model.read.code === "results_deleted";
  return html`<div class="ev-list">
    <${BlockReadState} read=${model.read} loading="Loading suspension evidence…" />
    ${current && model.read.state === "unavailable" && !deleted && html`<div class="small muted">The cause still comes from the campaign view; no rows are shown.</div>`}
    ${model.read.stale && (model.newerSeq !== null
      ? html`<div class="reason-line"><span class="chip-warn">Stale · newer pause seq ${model.newerSeq}</span>
          <span class="small muted">previous evidence stays dimmed</span></div>`
      : html`<div class="reason-line"><${StaleMark} /><span class="small muted">Historical evidence may not include the latest pause.</span></div>`)}
    ${model.empty && html`<div class="panel pad small muted">No pauses were recorded</div>`}
    ${(model.latest || model.older.length > 0) && html`<div class=${`ev-list${model.read.stale ? " is-stale" : ""}`}>
      ${model.latest && rec(model.latest, current && model.current !== null)}
      ${model.older.length > 0 && html`<${Disclosure} summary="Earlier pauses" meta=${`${model.older.length} · newest first`}>
        <div class="ev-list ev-earlier">${model.older.map((record) => rec(record, false))}</div></${Disclosure}>`}</div>`}
  </div>`;
}
// Spec §3.4: the count in the summary is the campaign view's own list — no GET until the section opens.
const pausesOf = (campaign) => Array.isArray(campaign?.suspensions) ? campaign.suspensions.length : null;
// Final review item 6: "N pauses recorded" (the view's list, not only those before a resume); "loaded when opened"
// only while closed; 0 says so — the count is the campaign view's, nothing is read for it.
// Codex r1 (PDR-0017: unavailable/stale do not mean "no pauses"): the count and "No pauses recorded" only while the
// campaign read is fresh (ok, not stale, no outage); once open, also only while the history read `history` is ok
// and fresh — otherwise its own read state speaks in the body.
export function historyMeta({ n, open, campaignFresh = false, history = null }) {
  const trusted = campaignFresh === true && n !== null && (!open || (history?.state === "ok" && !history.stale));
  if (trusted && n === 0) return "No pauses recorded";
  const count = trusted ? `${n} ${n === 1 ? "pause" : "pauses"} recorded` : null;
  return [count, open ? null : "loaded when opened"].filter(Boolean).join(" · ");
}
// campaignFresh: the caller's campaign entry is ok, not stale, and the shell sees no outage (historyMeta).
export function SuspensionHistory({ api, operationId, campaign, campaignSettled = true, campaignFresh = false, deleted = false,
  outage = false, current = false }) {
  const [disclosure, setDisclosure] = useState({ operationId, open: false });
  const open = current || (disclosure.operationId === operationId && disclosure.open);
  const entry = useSuspensions(api, operationId, campaign, { open, campaignSettled, deleted, outage });
  const model = suspensionModel({ operationId, seq: suspensionSeq(campaign), entry });
  const meta = historyMeta({ n: pausesOf(campaign), open, campaignFresh, history: model.read });
  return html`<section class="block">
    ${current ? html`<div class="section-h"><h2>Latest pause evidence</h2></div><${SuspensionEvidence} model=${model} current />`
      : html`<${Disclosure} summary="Suspension history" meta=${meta} open=${open}
          onToggle=${(isOpen) => setDisclosure({ operationId, open: isOpen })}>
        <div class="disclosure-body">${open && html`<${SuspensionEvidence} model=${model} />`}</div></${Disclosure}>`}
  </section>`;
}
