// ui/confidence.js — family 14 on the Measurement screen: the Decision panel (gate · final decision · champion
// transition), the Confidence panel with interval rows, and scoped caveats as limits.
// Codes stay as core gave them; labels for closed sets come from the view model.
import { html } from "../vendor/htm-preact.js";
import { number, signed } from "../lib/format.js";
import { isTooLarge } from "../lib/views/content.js";
import { bounded } from "../lib/views/confidence.js";
import { Disclosure } from "./disclosure.js";
import { ReadState, StatusPill, Unavailable } from "./pill.js";

// "Limits on this result": text · code · scope, kept next to the result they limit.
// An unknown kind falls back to its raw code (text === code), shown once.
export function LimitList({ limits }) {
  const rows = limits ?? [];
  if (!rows.length) return null;
  return html`<div class="limits"><div class="limits-head">Limits on this result</div>
    ${rows.map((c, i) => html`<div class="limit" key=${i}><span>${c.text}</span>
      ${c.text !== c.code && html`<span class="code">${c.code}</span>`}<span class="scope">${c.scope}</span></div>`)}</div>`;
}

const Tag = ({ text, tone }) => html`<span class=${`tag tone-${tone ?? "neutral"}`}>${text}</span>`;
const Code = ({ v }) => (v === null || v === undefined || v === "" ? null : html`<span class="code">${v}</span>`);

function GateFact({ gate }) {
  if (!gate) return html`<div class="decision-fact"><span class="muted">No gate decision yet</span></div>`;
  return html`<div class="decision-fact"><${Tag} ...${gate} /><${Code} v=${gate.code} /></div>`;
}
function FinalFact({ final }) {
  if (!final) return html`<div class="decision-fact"><span class="muted">No final decision recorded</span></div>`;
  if (final.missing !== undefined) return html`<div class="decision-fact"><${Unavailable} kind="state" reason=${final.missing} compact /></div>`;
  return html`<div class="decision-fact"><${Tag} text=${final.text} tone=${final.tone} /><${Code} v=${final.reason} /></div>
    ${final.entries.length > 0 && html`<div class="decision-sub">${final.entries.join(" · ")}</div>`}`;
}
// Only from champions[] (PDR-0017): promoted / no transition recorded / lineage unavailable. The caption names the
// lineage measurement; with several promoted versions, each version's own (final review item 12).
function LineageCaption({ measurements }) {
  if (!measurements.some((m) => m.measurementId)) return null;
  if (measurements.length === 1)
    return html`<div class="decision-sub">from lineage · <span class="mono">${measurements[0].measurementId}</span></div>`;
  return html`<div class="decision-sub">from lineage · ${measurements.map((m, i) => html`${i > 0 && " · "}<span class="mono" key=${m.version}>${m.version} ${m.measurementId ?? "—"}</span>`)}</div>`;
}
function ChampionFact({ champion }) {
  if (champion.kind === "promoted") return html`<div class="decision-fact">
      <${Tag} text=${`Promoted ${champion.versions.map((v) => `→ ${v}`).join(" · ")}`} tone="green" /></div>
    <${LineageCaption} measurements=${champion.measurements} />`;
  if (champion.kind === "unavailable") return html`<div class="decision-fact"><${Tag} text="Lineage unavailable" tone="amber" /><${Code} v=${champion.reason} /></div>`;
  return html`<div class="decision-fact"><span class="muted">No transition recorded</span></div>`;
}

export function DecisionPanel({ decision }) {
  if (!decision) return null;
  return html`<div class="decision">
    <div data-fact="gate"><div class="lbl">Gate</div><${GateFact} gate=${decision.gate} /></div>
    <div data-fact="final"><div class="lbl">Final decision</div><${FinalFact} final=${decision.final} /></div>
    <div data-fact="champion"><div class="lbl">Champion transition</div><${ChampionFact} champion=${decision.champion} /></div>
  </div>`;
}

// A bound as recorded: "—" when missing, never 0.
const bound = (x) => (x === null ? "—" : number(x).replace("-", "−"));
// Position on the shared scale, in percent of the drawing's width (SVG attributes: no inline style under the CSP).
const at = (scale, x) => Math.min(100, Math.max(0, ((x - scale.min) / (scale.max - scale.min)) * 100));
const pct = (scale, x) => `${at(scale, x)}%`;
const exactText = (e) => (e === true ? "exact" : e === false ? "not exact" : "—");

// One interval row (spec family14 §3.5): point, [lower, upper], and a drawing on the shared scale only when both
// bounds are recorded. The amber margin band around 0 is Focus's only (`margin` is the focus threshold).
export function IntervalRow({ name, interval, margin = null, scale, cases }) {
  const drawn = scale && bounded(interval);
  const band = drawn && margin !== null && margin > 0;
  return html`<div class="ci-row" data-interval=${name.toLowerCase()}><span class="strong">${name}</span>
    <span class="mono">${interval ? html`${signed(interval.point)} <span class="muted">[${bound(interval.lower)}, ${bound(interval.upper)}]</span>` : "—"}</span>
    ${drawn ? html`<svg class="ci" aria-hidden="true">
        <line class="ci-axis" x1="0" x2="100%" y1="9" y2="9" />
        ${band && html`<rect class="ci-marg" x=${pct(scale, -margin)} width=${`${at(scale, margin) - at(scale, -margin)}%`} y="4" height="10" />
          <line class="ci-marg-edge" x1=${pct(scale, -margin)} x2=${pct(scale, -margin)} y1="4" y2="14" />
          <line class="ci-marg-edge" x1=${pct(scale, margin)} x2=${pct(scale, margin)} y1="4" y2="14" />`}
        <line class="ci-zero" x1=${pct(scale, 0)} x2=${pct(scale, 0)} y1="2" y2="16" />
        <line class="ci-bar" x1=${pct(scale, interval.lower)} x2=${pct(scale, interval.upper)} y1="9" y2="9" />
        ${interval.point !== null && html`<circle class="ci-pt" cx=${pct(scale, interval.point)} cy="9" r="4" />`}
      </svg>` : html`<span></span>`}
    <span class="mono small">${exactText(interval?.exact)}</span><span>${cases ?? "—"}</span></div>`;
}

function LookHistory({ looks, read }) {
  const ready = Array.isArray(looks);
  const meta = ready ? `· ${looks.length} look${looks.length === 1 ? "" : "s"}` : null;
  return html`<div class="ci-looks"><${Disclosure} summary="Look history" meta=${meta}
      warn=${isTooLarge(looks) ? "too large" : null}>
    <div class="disclosure-body">
      ${looks === null || looks === undefined ? html`<${ReadState} read=${read} loading="Loading the look history…" />`
        : isTooLarge(looks) ? html`<${Unavailable} kind="state" reason=${looks.reason} />`
        : html`<div class="mini"><div class="mini-row mini-head look-cols"><span>Look</span><span>Final</span><span>Decision</span><span>Reason</span><span>Focus</span><span>Causal</span></div>
          ${looks.map((l, i) => html`<div class="mini-row look-cols" key=${i}><span>${l.look}</span><span>${l.final ? "yes" : "no"}</span>
            <span>${l.decision.text}</span><span class="code">${l.reason ?? "—"}</span>
            <span class="mono">${l.focus?.text ?? "—"}</span><span class="mono">${l.causal?.text ?? "—"}</span></div>`)}</div>`}
    </div></${Disclosure}></div>`;
}

// Confidence under the Decision (spec family14 §3.5, §4.2): the mode once — observe with its amber "bounds not
// computed" tag, any other mode as a neutral mark —, Focus and Causal rows, one secondary line, the limits, and
// the look history in a disclosure. Draws only recorded numbers.
export function ConfidencePanel({ c, looks, read, limits }) {
  const scaleText = c?.scale ? `${signed(c.scale.min, 2)} … 0 … ${signed(c.scale.max, 2)}` : "";
  return html`<div class="ci-panel">
    <div class="ci-head"><span class="strong">Confidence</span>
      ${!c ? html`<span class="small muted">not recorded for this attempt</span>`
        : c.observe ? html`<span class="tag tone-amber">observe · bounds not computed</span>`
        : html`<span class="tag tone-neutral mono">${c.mode}</span>`}
      ${c && (c.pending ? html`<span class="small muted">not evaluated yet</span>`
        : html`<span class="small muted">look ${c.look}${c.final ? " · final" : ""} · decision</span>
          <${StatusPill} ...${c.decision} /><${Code} v=${c.reason} />`)}
      ${c && html`<span class="small muted ci-margin">margin <span class="mono">${c.margin}</span></span>`}</div>
    ${c && html`<div class="ci-row ci-cols"><span></span><span>Point · bounds</span><span>${scaleText}</span><span>Exact</span><span>Cases</span></div>
      <${IntervalRow} name="Focus" interval=${c.focus} margin=${c.marginValue} scale=${c.scale} cases=${c.cases.focus} />
      <${IntervalRow} name="Causal" interval=${c.causal} scale=${c.scale} cases=${c.cases.causal} />
      <div class="ci-sub">repeats ${c.repeats} · calibration ${c.calibration}</div>`}
    ${(limits ?? []).length > 0 && html`<div class="ci-limits"><${LimitList} limits=${limits} /></div>`}
    <${LookHistory} looks=${looks} read=${read} />
  </div>`;
}
