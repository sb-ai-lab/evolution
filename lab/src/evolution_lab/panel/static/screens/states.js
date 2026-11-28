// screens/states.js — screen 10, State reference (prototype lines 119–139; spec §2.4, §3.1): drawn from
// lib/labels.js, so it is the living documentation of every code the panel labels. No service reads.
import { html } from "../vendor/htm-preact.js";
import { LABELS, label } from "../lib/labels.js";
import { SkeletonStrip } from "../ui/bars.js";
import { ResumeVerdictCard } from "../ui/command.js";
import { StatusPill } from "../ui/pill.js";

const VERDICTS = ["ready", "cleanup_first", "busy", "refused"];
// The progress cells an operation card shows once core events arrive (prototype sample 09).
const PROGRESS_CELLS = ["Rounds", "Attempts", "Coverage", "Target tokens"];

function CodeTable({ kind }) {
  return html`<section class="panel stable">
    <div class="stable-head mono">${kind}</div>
    ${Object.keys(LABELS[kind]).map((code) => {
      const { text, tone } = label(kind, code);
      return html`<div class="stable-row" key=${code}>
        <code class="small muted">${code}</code><${StatusPill} text=${text} tone=${tone} /><span class="small faint">${tone}</span>
      </div>`;
    })}
  </section>`;
}

// A campaign launched whose journal has no core event yet: progress_state not_started, progress null.
function NotStartedSample() {
  const progress = label("section_state", "not_started");
  return html`<section class="panel skeleton-card">
    <div class="skeleton-head">
      <span class="skeleton-title">Campaign</span>
      <${StatusPill} ...${label("control_state", "running")} />
      <span class="small muted">progress <code>not_started</code> · ${progress.text}</span>
    </div>
    <${SkeletonStrip} cells=${PROGRESS_CELLS} />
    <span class="small muted">Process launched. Waiting for the first core event; progress appears once the journal starts.</span>
  </section>`;
}

export function StatesScreen() {
  return html`<div class="screen" data-screen="states">
    <div class="title">
      <h1>State reference</h1>
      <span class="cap">every code Lab can send, with its label and colour · blue = in progress · green = done ·
        amber = not enough data · violet = infrastructure</span>
    </div>
    <div class="states-grid">${Object.keys(LABELS).map((kind) => html`<${CodeTable} key=${kind} kind=${kind} />`)}</div>
    <div class="section-h">Resume verdict<span class="cap">resume.state of a suspended campaign</span></div>
    <div class="verdict-grid">${VERDICTS.map((code) => html`<${ResumeVerdictCard} key=${code} title=${code}
      verdict=${label("resume_state", code)} />`)}</div>
    <div class="section-h">Campaign just started · no core events yet</div>
    <${NotStartedSample} />
  </div>`;
}
