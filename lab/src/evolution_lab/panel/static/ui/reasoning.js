// ui/reasoning.js — "How this candidate was made" (prototype lines 229–246): the Reflector's hypotheses as
// written, then the Editor. What Core does not record and Lab does not read yet is named, not left out; an
// Editor failure the details read could not give is Unavailable with its reason, not "No failure recorded".
import { html } from "../vendor/htm-preact.js";
import { StatusPill, Unavailable } from "./pill.js";

const FIELDS = [["root_cause", "Root cause"], ["suggested_fix", "Suggested fix"], ["affected_cases", "Affected cases"],
  ["target_paths", "Target paths"], ["counterevidence", "Counterevidence"], ["regression_risks", "Regression risks"],
  ["why_this_layer", "Why this layer"]];
const show = (v) => (Array.isArray(v) ? v.join(", ") : v === null || v === undefined ? "—" : String(v));

const Hypothesis = ({ h }) => html`<div class="hypothesis">
  <div class="mono small strong">#${h.index}</div>
  ${FIELDS.map(([key, text]) => html`<div class="hyp-field" key=${key}><span class="tiny muted">${text}</span>
    <span class="hyp-text">${show(h[key])}</span></div>`)}</div>`;

export function ReflectorBlock({ reflector, content }) {
  if (reflector.unavailable !== undefined) return html`<div class="panel pad reason-block">
    <div class="strong">Reflector</div><${Unavailable} kind="state" reason=${reflector.unavailable} compact /></div>`;
  if (reflector.pending !== undefined) return html`<div class="panel pad reason-block">
    <div class="strong">Reflector</div><div class="small muted">${reflector.pending.text}</div></div>`;
  return html`<div class="panel pad reason-block">
    <div class="reason-head"><span class="strong">Reflector · round ${reflector.round}</span><${StatusPill} ...${reflector.state} /></div>
    ${reflector.hypotheses.length === 0 && html`<div class="small muted">No hypotheses recorded</div>`}
    ${reflector.hypotheses.map((h) => html`<${Hypothesis} key=${h.index} h=${h} />`)}
    <div class="caution small">${reflector.caution}</div>
    <div class="reason-line"><span class="tiny muted">Which hypothesis this attempt used</span><${Unavailable} kind="core" compact /></div>
    ${content}
  </div>`;
}

export const EditorBlock = ({ editor, content }) => html`<div class="panel pad reason-block">
  <div class="strong">Editor</div>
  ${editor.unavailable !== null
    ? html`<div class="reason-line"><span class="tiny muted">Failure</span><${Unavailable} kind="state" reason=${editor.unavailable} compact /></div>`
    : html`<div class="small mono text-2">${editor.failure ?? "No failure recorded"}</div>`}
  <div class="reason-line"><span class="tiny muted">Editor tokens</span><${Unavailable} kind="scope" compact /></div>
  ${content}
</div>`;
