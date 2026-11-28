// ui/pipeline.js — COMPONENTS.md StepRing, StepPipeline (prototype lines 107–113): a scenario's or a cycle's
// steps left to right: ring + 2px connector, id (mono 12/500), state in its tone, source (11 muted),
// note (11 amber) on a line of its own.
import { html } from "../vendor/htm-preact.js";
import { shortId } from "../lib/format.js";
import { ReadState, StaleMark } from "./pill.js";

// 12px ring, 2px border in the tone's colour; filled for a done step, hollow otherwise.
export const StepRing = ({ filled, tone }) => html`<i class=${`ring fg-${tone ?? "neutral"}${filled ? " filled" : ""}`}></i>`;

function Step({ step, last }) {
  return html`<div class="step">
    <div class="step-rail"><${StepRing} filled=${step.filled} tone=${step.state.tone} />${!last && html`<i class="step-link"></i>`}</div>
    <span class="mono step-id">${step.id}</span>
    <span class="tiny text-3">${step.title}</span>
    ${step.marks && html`<span class="tiny faint">${step.marks}</span>`}
    <span class=${`tiny fg-${step.state.tone}`}>${step.state.text}${step.doneAt ? html` <span class="faint">· ${step.doneAt}</span>` : ""}</span>
    ${step.source && html`<span class="tiny muted step-text">${step.source}</span>`}
    ${step.note && html`<span class="tiny fg-amber step-text">${step.note}</span>`}
  </div>`;
}

// {title, opId|null, href|null, state: {text, tone}, read|null, steps} — projectModel().pipelines[i]. `read`:
// the cycle's operation read, whose state (loading / Unavailable with the code / stale) stands where its
// progress would be. `bare`: inside a History row, whose summary already carries the title — no panel, no title.
export function StepPipeline({ title, opId, href, state, read, steps, bare = false }) {
  return html`<section class=${bare ? "pipeline pipeline-bare" : "panel pipeline"} data-pipeline=${title}>
    <div class="pipeline-head">
      ${!bare && html`<span class="strong">${title}</span>`}
      ${opId && (href ? html`<a class="mono small link" href=${href}>${shortId(opId)}</a>`
                      : html`<span class="mono small faint">${shortId(opId)}</span>`)}
      <span class=${`pipeline-state small fg-${state.tone}`}>${state.text}</span>
      <${ReadState} read=${read} loading="Loading progress…" compact />
      ${read?.stale && html`<${StaleMark} />`}
    </div>
    <div class="steps">${steps.map((s, i) => html`<${Step} key=${s.id} step=${s} last=${i === steps.length - 1} />`)}</div>
  </section>`;
}
