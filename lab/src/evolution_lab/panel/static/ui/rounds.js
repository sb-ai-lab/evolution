// ui/rounds.js — "Rounds & attempts" (spec family14 §3.3; additions mock NEW 3): a round header, then the rows of
// roundRowsOf — attempts; each recorded final as an indented second step that expands to its full record; a dashed
// placeholder for a finalist without a record (pending / unavailable, ruling R8). details.state "unavailable":
// reflection and failure lines say so.
import { html, useState } from "../vendor/htm-preact.js";
import { ReadState, StatusPill, Unavailable } from "./pill.js";
import { readOf } from "../lib/resource.js";
import { contrastDecision, fieldText } from "../lib/views/campaign.js";
import { number, signed } from "../lib/format.js";
import { isTooLarge, REDACTED_HINT, roleOf } from "../lib/views/content.js";
import { Disclosure } from "./disclosure.js";

const Code = ({ v }) => (v === null || v === undefined || v === "" ? null : html`<span class="code">${v}</span>`);

// Row limits (spec family14 §4.2 "in their rows", ruling R7): an amber "N limits" chip in the row, a keyboard toggle
// whose list (label · code) opens right under the row, collapsed by default; the same text is the chip's title.
const limitLine = (l) => (l.text === l.code ? String(l.code) : `${l.text} · ${l.code}`);
function useLimits(limits) {
  const [open, setOpen] = useState(false);
  if (!limits.length) return [null, null];
  const n = limits.length;
  const chip = html`<button type="button" class="chip-warn limits-chip" aria-expanded=${open ? "true" : "false"}
    title=${limits.map(limitLine).join("\n")} onClick=${() => setOpen(!open)}>${n} ${n === 1 ? "limit" : "limits"}</button>`;
  const list = open && html`<div class="row-limits">${limits.map((l, i) => html`<div class="limit" key=${i}><span>${l.text}</span>
    ${l.text !== l.code && html`<span class="code">${l.code}</span>`}</div>`)}</div>`;
  return [chip, list];
}
const MeasurementLink = ({ id, link }) => (link ? html`<a class="link mono" href=${link}>${id}</a>` : html`<span class="mono">${fieldText(id)}</span>`);
const yesNo = (v) => (v === true ? "yes" : v === false ? "no" : fieldText(v));

// → vN only on an attempt row (§4.4); "—" and "unavailable" keep their reason in the title.
const Champion = ({ c }) => html`<span class=${`mono champion-cell${c.text.startsWith("→") ? " promoted" : " muted"}`} title=${c.title}>${c.text}</span>`;

function Attempt({ a, details, failureRead }) {
  const editor = failureRead?.data ? roleOf(failureRead.data, "editor") : null;
  const failure = editor?.failure ?? null;
  const id = a.href ? html`<a class="link mono" href=${a.href}>${a.id}</a>` : html`<span class="mono">${a.id}</span>`;
  const m = a.measurementId ?? "no measurement";
  const [chip, list] = useLimits(a.limits);
  return html`<div class="attempt">
    <div class="attempt-row">
      <span class="tree-mark"></span>${id}
      <span class="mono small muted">${m}</span>
      <span class="nums">${a.mean}</span><span class="nums">${a.delta}</span>
      <span class="nums text-2">${a.look}</span>
      <${Champion} c=${a.champion} />
      <span class="attempt-outcome"><${StatusPill} ...${a.group} /><${Code} v=${a.reason} />
        ${a.marks.map((k) => (k === "in joint final" ? html`<span class="joint" key=${k}>${k}</span>` : html`<span class="faint tiny" key=${k}>${k}</span>`))}${chip}</span>
    </div>
    ${list}
    ${details.state === "unavailable"
      ? html`<div class="attempt-failure"><${Unavailable} kind="state" reason=${details.reason} compact /></div>`
      : a.failure && html`<div class="attempt-failure mono small text-2">${a.failure}</div>`}
    ${a.failure && failureRead && html`<div class="attempt-failure">
      <${ReadState} read=${readOf(failureRead)} loading="Loading the failure…" compact />
      ${editor && (editor.state === "unavailable"
        ? html`<span class="tiny muted">Editor content</span><${Unavailable} kind="state" reason=${editor.reason} compact />`
        : editor.reason && html`<div class="small muted">Editor content · ${editor.state} · ${editor.reason}</div>`)}
      ${isTooLarge(failure) ? html`<span class="tiny muted">Failure</span><${Unavailable} kind="state" reason=${failure.reason} compact />`
        : failure?.text !== null && failure?.text !== undefined && html`<span class="tiny muted">${failure.source}</span>
          <pre class="content-text" title=${REDACTED_HINT}>${failure.text.slice(0, 400)}</pre>
          ${failure.text.length > 400 && html`<span class="tiny muted">${a.measurementId ? "First 400 characters · full text on the measurement screen" : "First 400 characters"}</span>`}
          ${failureRead.data.truncated && html`<span class="tiny muted">truncated</span>`}
          ${(failureRead.data.truncation ?? []).filter((t) => t.path.startsWith("/editor/failure/")).map((t) => html`<div class="tiny muted" key=${t.path}>truncated · ${t.path} · size ${t.size} characters before truncation</div>`)}`}
    </div>`}
  </div>`;
}

const Decision = ({ d }) => (d ? html`<${StatusPill} text=${d.text} tone=${d.tone} /><${Code} v=${d.code} />` : html`<span class="muted">—</span>`);

// The expanded record: Members, Contrasts, then Selection / Champion side / Mode / Listed / Next baseline.
export function FinalDetail({ d }) {
  return html`<div class="final-body">
    <div class="mini-cap">Members</div>
    <div class="mini">
      <div class="mini-row mini-head mem-cols"><span>Role</span><span>Attempt</span><span>Measurement</span><span>Point</span><span>Complete</span><span>Missing</span><span>Critical</span></div>
      ${d.members.length === 0 && html`<div class="mini-row muted">No members</div>`}
      ${d.members.map((m, i) => html`<div class="mini-row mem-cols" key=${i}><span class="mono">${fieldText(m.role)}</span>
        <span class=${m.attempt_id ? "mono" : "faint"}>${fieldText(m.attempt_id)}</span><${MeasurementLink} id=${m.measurement_id} link=${m.measurementHref} />
        <span class="nums">${number(m.point)}</span><span>${yesNo(m.complete)}</span><span>${fieldText(m.missing)}</span><span>${yesNo(m.critical)}</span></div>`)}
    </div>
    <div class="mini-cap">Contrasts</div>
    <div class="mini">
      <div class="mini-row mini-head con-cols"><span>Attempt</span><span>Point</span><span>Bound · lower / level / exact</span><span>Decision</span><span>Pairing</span><span>Removed pairs · cases</span><span>Condition changed</span></div>
      ${d.contrasts.length === 0 && html`<div class="mini-row muted">No contrasts</div>`}
      ${d.contrasts.map((c, i) => html`<div class="mini-row con-cols" key=${i}><span class="mono">${fieldText(c.attempt_id)}</span>
        <span class="nums">${signed(c.point)}</span><span class="mono">${number(c.lower)} / ${fieldText(c.level)} / ${yesNo(c.exact)}</span>
        <span class="final-decision">${c.accepted === true || c.accepted === false ? html`<${StatusPill} text=${contrastDecision(c).text} tone=${contrastDecision(c).tone} />` : html`<span class="muted">${fieldText(c.accepted)}</span>`}
          <${Code} v=${c.reason} /><${Code} v=${c.unavailable} /></span>
        <span class="mono">${fieldText(c.pairing)}</span><span>${fieldText(c.removed_pairs)} · ${fieldText(c.removed_cases)}</span>
        <span>${c.condition_changed_in_place === true ? html`<span class="chip-warn">yes</span>` : yesNo(c.condition_changed_in_place)}</span></div>`)}
    </div>
    <div class="final-foot">
      ${d.selection
        ? html`<span><span class="muted">Core selection</span> · <span class="mono">${fieldText(d.selection.attempt_id)}</span> · <${MeasurementLink} id=${d.selection.measurement_id} link=${d.selection.measurementHref} /> · <span class="code" title=${d.selection.text}>${fieldText(d.selection.reason)}</span></span>`
        : html`<span class="muted">No selection recorded</span>`}
      <span><span class="muted">Champion side</span> <span class="mono">${fieldText(d.championSide)}</span>${d.storedMeasurementId && html` · <${MeasurementLink} id=${d.storedMeasurementId} link=${d.storedHref} />`}</span>
      <span><span class="muted">Mode</span> <span class="mono">${fieldText(d.mode)}</span> · <span class="muted">global</span> <span class="mono">${fieldText(d.global)}</span></span>
      <span><span class="muted">Stability</span> <span class="mono">${fieldText(d.stability)}</span> · <span class="muted">Condition changed in place</span> ${d.conditionChangedInPlace === true ? html`<span class="chip-warn">yes</span>` : yesNo(d.conditionChangedInPlace)}</span>
      <span><span class="muted">Listed</span> ${fieldText(d.listed)}</span>
      <span><span class="muted">Next baseline</span> <${MeasurementLink} id=${d.nextBaseline.id} link=${d.nextBaseline.href} /></span>
    </div>
  </div>`;
}

// One final[] record as an expandable row aligned with the attempt columns; no → vN here (§4.4).
function FinalRow({ f }) {
  // A joint record of several finalists has no single measurement: "N finalists · side" spans Mean..Looks (mock B);
  // with one finalist it keeps the Measurement, Mean and Δ cells (spec §3.3).
  const many = f.joint && f.finalists !== 1;
  const lead = many ? html`<span class="final-joint text-3">${f.finalists} finalists · ${f.side}</span>`
    : html`<${MeasurementLink} id=${f.measurementId} link=${f.href} /><span class="nums">${f.point ?? "—"}</span>
      <span class="nums">${f.delta ?? "—"}</span><span></span>`;
  const outcome = html`<span class="attempt-outcome"><${Decision} d=${f.decision} />
    ${!many && html`<span class="faint tiny">${f.side}</span>`}
    ${f.selection && html`<span class="tiny muted">Core selection <span class="mono">${fieldText(f.selection.attempt_id)}</span></span>`}
    ${f.warns.map((w) => html`<span class="chip-warn" key=${w}>${w}</span>`)}</span>`;
  return html`<div class=${`final-entry${f.joint ? " joint-final" : ""}${many ? " joint-many" : ""}`}>
    <${Disclosure} summary=${f.label} meta=${html`${lead}<span></span>${outcome}`}><${FinalDetail} d=${f.detail} /></${Disclosure}>
  </div>`;
}

// Owner's final_decision of a finalist without a record: pending -> not started; unavailable -> its reason (R8).
const PendingFinal = ({ p }) => html`<div class="attempt-row final-pending"><span class="final-label muted">Final measurement</span>
  <div class="faint small final-rest">${p.state === "pending" ? "not started"
    : html`<${Unavailable} kind="state" reason=${p.reason} compact />`}</div></div>`;

function Row({ row, details, failures }) {
  if (row.kind === "attempt") return html`<${Attempt} a=${row} details=${details} failureRead=${failures[row.id]} />`;
  if (row.kind === "final") return html`<${FinalRow} f=${row} />`;
  return html`<${PendingFinal} p=${row} />`;
}

function Round({ r, details, failures }) {
  const [chip, list] = useLimits(r.header.limits);
  return html`<div class="round">
    <div class="round-head"><span class="strong">${r.header.title}</span>
      <span class="code">baseline ${r.header.baseline}${r.header.baselineMeasurementId && ` · ${r.header.baselineMeasurementId}`}</span>
      ${r.header.how && html`<span class="small text-3">${r.header.how}</span>`}
      <span class="round-pills">${chip}${r.header.outcome && html`<${StatusPill} ...${r.header.outcome} />`}
        ${details.state === "unavailable"
          ? html`<${Unavailable} kind="state" reason=${details.reason} compact />`
          : html`<${StatusPill} ...${r.header.reflection} />`}</span></div>
    ${list}
    ${!r.rows.some((x) => x.kind === "attempt") && html`<div class="attempt-row muted small"><span></span><span class="round-empty">No attempts in this round</span></div>`}
    ${r.rows.map((row, i) => html`<${Row} key=${`${rowKey(row)}:${i}`} row=${row} details=${details} failures=${failures} />`)}
    ${r.finalState && r.finalState.state !== "present" && html`<div class="pad small">Final records: <${Unavailable} kind="state" reason=${r.finalState.reason} compact /></div>`}
  </div>`;
}
const rowKey = (row) => (row.kind === "attempt" ? `a:${row.id}` : row.kind === "final" ? `f:${row.ordinal}:${row.label}` : `p:${row.attemptId}`);

// stale: shown from the campaign view's kept previous answer — dimmed (spec family14 §3.9).
export const RoundTree = ({ rounds, details, failures = {}, stale = false }) => html`<div class=${`panel rounds${stale ? " is-stale" : ""}`}>
  <div class="attempt-row attempt-head"><span></span><span>Attempt</span><span>Measurement</span><span>Mean</span><span>Δ</span>
    <span>Looks</span><span>Champion</span><span>Outcome</span></div>
  ${rounds.map((r) => html`<${Round} key=${r.round} r=${r} details=${details} failures=${failures} />`)}
</div>`;
