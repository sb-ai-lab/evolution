// ui/content.js — the served content, without inferred judgments or implicit private reads.
import { html, useRef, useState } from "../vendor/htm-preact.js";
import { paths } from "../lib/api.js";
import { criticalText, isTooLarge, REDACTED_HINT, revealOf, saidLabel, sampleOf } from "../lib/views/content.js";
import { useOnDemand } from "./hooks.js";
import { BlockReadState, StaleMark, Unavailable } from "./pill.js";
import { Disclosure } from "./disclosure.js";

const text = (value) => value === null || value === undefined ? "—"
  : typeof value === "string" ? value : JSON.stringify(value, null, 2);
const Text = ({ value }) => html`<pre class="content-text" title=${REDACTED_HINT}>${text(value)}</pre>`;

// Only pass section values from the closed content forms, never arbitrary user JSON.
const Section = ({ title, value, children }) => html`<div class="content-event">
  <div class="reason-line"><span class="strong small">${title}</span>
    ${isTooLarge(value) && html`<${Unavailable} kind="state" reason=${value.reason} compact />`}</div>
  ${!isTooLarge(value) && children}</div>`;

const Marks = ({ model }) => html`<div class="tiny muted" title=${REDACTED_HINT}>
  ${model.masking && html`<span>masking: ${model.masking}</span>`}
  ${model.truncated && html`<span> · truncated</span>`}
  ${(model.truncation ?? []).map((t) => html`<div key=${t.path}>truncated · ${t.path} · size ${t.size} characters before truncation</div>`)}</div>`;

export const PrivateToggle = ({ shown, onShow }) => shown
  ? html`<div class="small muted">Showing private content (disclose=private)</div>`
  : html`<button type="button" class="command-copy" onClick=${onShow}>Show private content</button>`;

// The explicit private reveal of one case (spec family14 §3.7): keyed by measurement|case|sample, so selecting
// another case or measurement — and coming back — starts withheld again, and never sends one private read.
function usePrivateReveal(key) {
  const state = useRef({ key, shown: false });
  const [, repaint] = useState(0);
  if (state.current.key !== key) state.current = { key, shown: false };
  return [state.current.shown, () => { state.current = { key, shown: true }; repaint((n) => n + 1); }];
}

const scalar = (v) => (v === null ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v));
const passedText = (v) => (v === true ? "passed" : v === false ? "failed" : scalar(v));
const reasonText = (r) => (r !== null && typeof r === "object" ? Object.values(r).map(scalar).join(" · ") : scalar(r));
const metricsText = (scoring) => isTooLarge(scoring) || scoring.metrics === null || typeof scoring.metrics !== "object"
  ? null : Object.entries(scoring.metrics).map(([k, v]) => `${k} ${scalar(v)}`).join(" · ");

// "Technical events (n)" (additions mock block 7): message.fields and every non-assistant event in recorded
// order, then the partial usage row. Opening it reads nothing.
function TurnTechnical({ turn }) {
  return html`<div class="trace-technical"><${Disclosure} summary=${`Technical events (${turn.count})`}>
    <div class="disclosure-body">
      ${turn.fields !== undefined && html`<div class="tech-row"><span class="who">message.fields</span><${Text} value=${turn.fields === null ? "null" : turn.fields} /></div>`}
      ${turn.technical.map((e, i) => html`<div class="tech-row" key=${i}>
        <span class="who">${e.type}${e.name ? ` · ${e.name}` : ""}${e.note ? html` · <span class="caution">${e.note}</span>` : ""}</span>
        ${e.text !== "" && html`<${Text} value=${e.text} />`}</div>`)}
    </div></${Disclosure}></div>`;
}

// One turn's main view: its input and assistant_message events, each "Turn N · <type>[ · <name>]" — N is the recorded
// index and no role is inferred (spec family14 §4.7); outcome and reason stay outside the disclosure.
const Turn = ({ turn }) => html`<div class="trace-turn">
  <div class="turn"><span class="who">Turn ${turn.index} · message.content</span>
    <div class="turn-text">${turn.input === "" ? html`<span class="faint">—</span>` : turn.input}</div></div>
  ${turn.dialogue.map((d, i) => html`<div class="turn said" key=${i}><span class="who">${saidLabel(turn.index, d)}</span>
    <div class="turn-text">${d.text}</div></div>`)}
  ${turn.noAssistant && html`<div class="small muted">No assistant_message in this turn</div>`}
  ${(turn.outcome !== undefined || turn.reason !== undefined) && html`<div class="tiny muted">${[
    turn.outcome !== undefined ? `outcome ${scalar(turn.outcome)}` : null,
    turn.reason === undefined ? null : turn.reason === null ? "reason null" : reasonText(turn.reason)].filter((x) => x !== null).join(" · ")}</div>`}
  <${TurnTechnical} turn=${turn} /></div>`;

// privateRead: the private read's own state (revealOf); until it answers, the public verdict stays and the
// private block shows that state only (Task 10 item 3). Codex r1: the public scoring and the private block are
// siblings, each with its own too-large guard — neither hides the other.
export function Verdict({ model, shown, privateRead }) {
  const s = model.scoring, p = model.private;
  const waiting = shown && privateRead && privateRead.state !== "ok";
  return html`<${Section} title="Judge verdict" value=${s}>
    ${!isTooLarge(s) && html`<div class="verdict small">
      <span class="muted">${s.state}</span>
      ${s.verdicts.map((v, i) => html`<div key=${i}><span class="mono">${v.criterion}</span> · ${passedText(v.passed)}</div>`)}
      ${s.feedback !== null && html`<div class="text-3">${text(s.feedback)}</div>`}
      ${!shown && html`<span class="faint">reasoning private</span>`}</div>`}
  </${Section}>
  ${waiting && html`<div class="content-private"><div class="small strong">Private · judge reasoning and diagnostics</div>
    <${BlockReadState} read=${privateRead} loading="Loading private content…" compact /></div>`}
  ${shown && !waiting && html`<div class="content-private"><${Section} title="Private · judge reasoning and diagnostics" value=${p}>
    ${!isTooLarge(p) && (p.withheld ? html`<span class="small muted">(private)</span>` : html`
      <div class="tiny muted">Verdict notes</div><${Text} value=${p.notes} />
      <div class="tiny muted">diagnostics</div><${Text} value=${p.diagnostics} />
      <div class="tiny muted">details</div><${Text} value=${p.details} />
      <div class="tiny muted">scorer_output</div><${Text} value=${p.scorerOutput} />`)}
  </${Section}></div>`}`;
}

// One side of the case: header (name, code, recorded scores), trace state, turns, terminal output in a
// trace-level disclosure, the judge verdict under it. Statuses, truncation and too-large stay outside disclosures.
// stale: shown from the sample's kept previous answer — dimmed, with the stale mark in the head (spec §3.9).
function Side({ head, model, shown, privateRead = null, stale = false }) {
  const score = metricsText(model.scoring);
  const terminalTooLarge = isTooLarge(model.terminal);
  return html`<div class=${`side-card${stale ? " is-stale" : ""}`}>
    <${SideHead} head=${head} score=${score} stale=${stale} />
    <div class="reason-line small"><span class="muted">Trace</span>
      ${model.state === "unavailable" ? html`<${Unavailable} kind="state" reason=${model.reason} compact />`
        : html`<span>${model.state}</span>`}
      ${!isTooLarge(model.scoring) && html`<span class="muted">${criticalText(model.scoring.critical)}</span>`}</div>
    <${Section} title=${isTooLarge(model.turns) ? "Turns" : `${model.turns.length} ${model.turns.length === 1 ? "turn" : "turns"}`} value=${model.turns}>
      ${!isTooLarge(model.turns) && model.turns.map((turn, i) => html`<${Turn} key=${i} turn=${turn} />`)}
      ${model.omitted > 0 && html`<div class="small caution">${model.omitted} turns omitted · truncated</div>`}
    </${Section}>
    <div class="trace-technical"><${Disclosure} summary="Terminal output" warn=${terminalTooLarge ? "too large" : null}>
      <div class="disclosure-body">${terminalTooLarge ? html`<${Unavailable} kind="state" reason=${model.terminal.reason} compact />`
        : html`<${Text} value=${model.terminal} />`}</div></${Disclosure}></div>
    <${Verdict} model=${model} shown=${shown} privateRead=${privateRead} />
    <${Marks} model=${model} />
  </div>`;
}

const SideHead = ({ head, score, stale = false }) => html`<div class="side-head"><span class="strong">${head.title}</span>
  ${head.code && html`<span class="code">${head.code}</span>`}${stale && html`<${StaleMark} />`}
  <span class="side-tags">${score !== null && html`<span class="tag tone-neutral">${score}</span>`}
    ${head.comparison && html`<span class=${`tag tone-${head.comparison.tone ?? "neutral"}`}>${head.comparison.text}</span>`}</span></div>`;

// The baseline side: its public read and, once asked, its private read (revealOf), like the candidate.
function SideRead({ head, entry, privateEntry, shown }) {
  const view = revealOf(entry, privateEntry, shown);
  if (view.read.state !== "ok") return html`<div class="side-card"><${SideHead} head=${head} score=${null} />
    <${BlockReadState} read=${view.read} loading="Loading the sample…" /></div>`;
  return html`<${Side} head=${head} model=${sampleOf(view.body)} shown=${shown} privateRead=${view.privateRead} stale=${view.read.stale} />`;
}

// The case detail (additions mock block 7): case input and the private reference on top, then baseline |
// candidate. `baseline`: {measurementId, sampleId} reads that sample (a verified pair); {unpaired: code} is a
// neutral placeholder and no read; absent — evaluation or a baseline measurement — one side only.
// heads: {candidate: {title, code, comparison?}, baseline?: {title, code}}.
export function SampleDetail({ api, operationId, measurementId, caseId, sampleId, baseline, heads }) {
  const [shown, show] = usePrivateReveal(`${measurementId}|${caseId}|${sampleId}`);
  const pair = baseline && baseline.unpaired === undefined ? baseline : null;
  // The public read stays mounted; the private one is a second read made only once asked (Task 10 item 3), so a
  // failed or slow private read never replaces the public trace already shown.
  const entry = useOnDemand(api, paths.sample(operationId, measurementId, caseId, sampleId, false));
  const privateEntry = useOnDemand(api, shown ? paths.sample(operationId, measurementId, caseId, sampleId, true) : null);
  const baseEntry = useOnDemand(api, pair ? paths.sample(operationId, pair.measurementId, caseId, pair.sampleId, false) : null);
  const basePrivate = useOnDemand(api, pair && shown ? paths.sample(operationId, pair.measurementId, caseId, pair.sampleId, true) : null);
  const view = revealOf(entry, privateEntry, shown);
  if (view.read.state !== "ok") return html`<div class="case-body"><${BlockReadState} read=${view.read} loading="Loading the sample…" /></div>`;
  const model = sampleOf(view.body);
  const p = model.private;
  const waiting = shown && view.privateRead.state !== "ok";
  const candidate = html`<${Side} head=${heads?.candidate ?? { title: "Sample", code: measurementId }} model=${model} shown=${shown}
    privateRead=${view.privateRead} stale=${view.read.stale} />`;
  const baseHead = heads?.baseline ?? { title: "Baseline", code: pair?.measurementId ?? null };
  return html`<div class="case-body">
    <div class="cmpcols">
      <div class="side-card"><${Section} title="Case input · public" value=${model.input}><${Text} value=${model.input?.public} /></${Section}></div>
      <div class="side-card"><${Section} title="Reference" value=${waiting ? null : p}>
        ${shown ? html`<${PrivateToggle} shown=${true} />
            ${waiting ? html`<${BlockReadState} read=${view.privateRead} loading="Loading private content…" compact />`
              : !isTooLarge(p) && (p.withheld ? html`<span class="small muted">(private)</span>` : html`<${Text} value=${p.oracle} />`)}`
          : html`<div class="reason-line"><span class="small faint">Private. Shown only on request.</span>
            <span class="case-private"><${PrivateToggle} shown=${false} onShow=${show} /></span></div>`}
      </${Section}></div>
    </div>
    ${baseline ? html`<div class="cmpcols">
        ${pair ? html`<${SideRead} head=${baseHead} entry=${baseEntry} privateEntry=${basePrivate} shown=${shown} />`
          : html`<div class="side-card"><${SideHead} head=${baseHead} score=${null} />
            <div class="reason-line"><span class="tag tone-neutral">Baseline not paired</span><span class="code">${baseline.unpaired}</span></div></div>`}
        ${candidate}</div>`
      : candidate}
  </div>`;
}

// Raw role events and the stderr tail sit in "Technical events (n)" (additions mock, block 5); too-large and
// truncation markers stay visible in its summary. Opening it reads nothing: the private toggle stays outside.
function TechnicalEvents({ model }) {
  const tooLarge = isTooLarge(model.events);
  const warn = [tooLarge ? "events too large" : null, isTooLarge(model.stderrTail) ? "stderr too large" : null].filter(Boolean).join(" · ");
  return html`<div class="role-technical"><${Disclosure} summary=${tooLarge ? "Technical events" : `Technical events (${model.events.length})`}
      meta=${model.omitted > 0 ? `${model.omitted} events omitted · truncated` : null} warn=${warn || null}>
    <div class="disclosure-body">
      <${Section} title="Events" value=${model.events}>
        ${!tooLarge && model.events.map((event, i) => html`<div class="content-event" key=${i}>
          <div class="small strong">${event.title}${event.turn !== null ? ` · turn ${event.turn}` : ""}${event.name ? ` · ${event.name}` : ""}</div>
          ${event.withheld ? html`<span class="small muted">(private)</span>` : html`<${Text} value=${event.text} />`}</div>`)}
        ${model.omitted > 0 && html`<div class="small muted">${model.omitted} events omitted · truncated</div>`}
      </${Section}>
      <${Section} title="stderr tail" value=${model.stderrTail}>
        ${model.stderrTail?.withheld ? html`<span class="small muted">(private)</span>` : html`<${Text} value=${model.stderrTail} />`}
      </${Section}>
    </div></${Disclosure}></div>`;
}

export function RoleCard({ model, role, shown, onShow }) {
  const hidden = (!isTooLarge(model.events) && model.events.some((e) => e.withheld)) || model.stderrTail?.withheld;
  return html`<div class="content-event">
    <div class="reason-line"><span class="strong">${role === "editor" ? "Editor" : "Reflector"} · ${model.kind}</span>
      ${model.state === "unavailable" ? html`<${Unavailable} kind="state" reason=${model.reason} compact />`
        : html`<span class="small">${model.state}${model.reason ? ` · ${model.reason}` : ""}</span>`}</div>
    ${model.toolCalls === "not_recorded" && html`<div class="small muted">Tool calls are not recorded for this role kind</div>`}
    ${role === "editor" && html`<${Section} title="Changed" value=${model.changed}>
      ${model.changed !== null ? html`<${Text} value=${model.changed} />`
        : model.changedCount !== null && html`<div class="small">changed count · ${model.changedCount}</div>`}
    </${Section}>
    <${Section} title="Failure" value=${model.failure}>
      ${model.failure && !isTooLarge(model.failure) && html`<div class="small mono">${model.failure.codes}</div>
        ${model.failure.text !== null && html`<div class="tiny muted">${model.failure.source}</div><${Text} value=${model.failure.text} />`}`}
    </${Section}>`}
    <${TechnicalEvents} model=${model} />
    ${role === "reflector" && (hidden || shown) && html`<${PrivateToggle} shown=${shown} onShow=${onShow} />`}
    <${Marks} model=${model} />
  </div>`;
}
