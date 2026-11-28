// ui/independent.js — family 15 on the Campaign screen (spec 2026-10-19 D8): the settings summary, then per attempt
// its outcome and reason, one statement per scope (lib/views/independent.js) with Core's outcome after the deciding
// scope, and the reference label. An attempt without a comparison shows its reason and Core's preflight ceilings.
// Read-only (PDR-0017, ADR-0018); no interval is drawn and no upper bound is shown (Q5).
import { html } from "../vendor/htm-preact.js";
import { StatusPill, Unavailable } from "./pill.js";

const Code = ({ v }) => (v === null || v === undefined || v === "" ? null : html`<span class="code">${v}</span>`);

function Comparison({ c }) {
  return html`<div class="small">
    <div><${StatusPill} ...${c.outcome} />${c.reason.code !== c.outcome.code && html` <${Code} v=${c.reason.code} />`}
      <span class="text-2"> ${c.layer.text}${c.decidingLayer && ` · decided by ${c.decidingLayer}`} · ${c.reference.href ? html`<a class="link" href=${c.reference.href}>${c.reference.text}</a>` : c.reference.text}</span></div>
    ${c.method && html`<div class="mono">method ${c.method} · unit ${c.unit ?? "—"}</div>`}
    ${c.boundFallback !== null && html`<div class="mono">bound_fallback=${c.boundFallback}</div>`}
    ${c.calibration && html`<div class="mono">calibration ${c.calibration.source} ${c.calibration.method} level=${c.calibration.level} allowance=${c.calibration.allowance} · retained ${c.calibration.retained} · degenerate ${String(c.calibration.degenerate)} · measurement ${c.calibration.measurementId}</div>`}
    ${c.caveats.length > 0 && html`<div class="mono">caveats: ${c.caveats.join(", ")}</div>`}
    ${c.scopes.map((s, i) => html`<div class="mono" key=${i}><span class="strong">${s.name}</span>: result rule=${s.rule} · ${s.text}${s.explanation && `; ${s.explanation}`}${s.exact !== null && ` · exact=${s.exact}`}${s.pairing !== null && ` · pairing=${s.pairing}`}${s.outcome && ` → ${s.outcome.text}`}</div>`)}
  </div>`;
}

function Binding({ binding, layer }) {
  return html`<div class="small mono">${layer} binding ${binding === null ? "unavailable" : `(${binding.source}): ${binding.scopes.map((s) => `${s.name}=${s.rule}`).join(", ")}`}</div>`;
}

const PreflightFallback = ({ p }) => p?.boundFallback == null ? null : html`<div class="mono">bound_fallback=${p.boundFallback}</div>`;

function NoComparison({ a }) {
  return html`<div class="small"><span class="muted">No comparison</span> <${Code} v=${a.reason} />
    ${a.preflight && html`<div class="mono">preflight ${a.preflight.status ?? "—"}${a.preflight.ceilings.length > 0 && `: ${a.preflight.ceilings.join("; ")}`}</div>`}
    <${PreflightFallback} p=${a.preflight} />
  </div>`;
}

// One attempt's entry; `link: false` where the page is that attempt's measurement already.
export function IndependentAttempt({ a, link = true, gateOnly = false }) {
  return html`<div class="pad" data-attempt=${a.id}>
    <div class="small"><span class="mono strong">${a.id}</span>${a.round !== null && html` <span class="faint">round ${a.round}</span>`}
      ${" "}<${StatusPill} ...${a.group} /> <span class="text-2">${a.outcome.text}</span> <${Code} v=${a.reason} />
      ${link && a.measurementHref && html` <a class="link" href=${a.measurementHref}>measurement</a>`}</div>
    ${a.stages && html`<div class="stage-strip small">${a.stages.map((s, i) => html`<div class=${`stage-cell${s.state === "not_run" ? " muted" : ""}`} data-stage=${s.scope} data-state=${s.state} key=${s.scope}><span class="strong">${i + 1} · ${s.scope}</span><${StatusPill} ...${s} /></div>`)}</div>`}
    <${Binding} binding=${a.gateBinding} layer="gate" />
    ${!gateOnly && html`<${Binding} binding=${a.globalBinding} layer="global" />`}
    ${a.gate && html`<${Comparison} c=${a.gate} />`}
    ${!gateOnly && a.global && html`<${Comparison} c=${a.global} />`}
    ${!a.gate && !a.global && html`<${NoComparison} a=${a} />`}
    ${a.eligibleFinalScore !== null && html`<div class="small">eligible final score <span class="mono">${a.eligibleFinalScore}</span></div>`}
    ${a.slotProgress && html`<div class="tiny muted">${a.slotProgress}</div>`}
    ${a.cost && html`<div class="tiny muted">Conditional cost: ${a.cost}</div>`}
  </div>`;
}

// stale: shown from the campaign view's kept previous answer — dimmed (spec family14 §3.9).
export function IndependentBlock({ model, stale = false }) {
  const cls = `panel${stale ? " is-stale" : ""}`;
  if (!model) return null;
  if (model.state !== "present") return html`<div class=${cls}><div class="pad small"><${Unavailable} kind="state" reason=${model.reason} compact /></div></div>`;
  const att = model.attainability;
  return html`<div class=${cls}>
    <div class="pad small">${model.settings.map((s) => html`<span key=${s.name} class="setup-field"><span class="k">${s.name}</span> <span class="mono">${s.value}</span> </span>`)}</div>
    ${model.evidence.text && html`<div class="pad small"><${StatusPill} ...${model.evidence.label} /> <span class="mono">${model.evidence.text}</span></div>`}
    ${att.rounds.map((r) => html`<div class="pad small mono" key=${`p${r.round}`}>round ${r.round} preflight ${r.status ?? "—"}${r.basis && ` (${r.basis})`}${r.ceilings?.length > 0 && `: ${r.ceilings.join("; ")}`}<${PreflightFallback} p=${r} /></div>`)}
    ${att.minimumUnits !== null && html`<div class="pad tiny muted">Core's guidance: at least ${att.minimumUnits} units under its stated assumptions; not an admission or power claim</div>`}
    ${model.attempts.length === 0 && !model.evidence.text && html`<div class="pad small muted">No attempts yet</div>`}
    ${model.attempts.map((a) => html`<${IndependentAttempt} a=${a} key=${a.id} />`)}
  </div>`;
}

// The Measurement screen of a family-15 attempt: its entry, the block's state, unauthenticated evidence (its kind), or that Core recorded none for it.
export function IndependentEntry({ independent }) {
  if (independent.state !== "present") return html`<div class="pad small"><${Unavailable} kind="state" reason=${independent.reason} compact /></div>`;
  if (independent.evidence?.text) return html`<div class="pad small"><${StatusPill} ...${independent.evidence.label} /> <span class="mono">${independent.evidence.text}</span></div>`;
  if (!independent.attempt) return html`<div class="pad small muted">No independent comparison entry recorded for this attempt</div>`;
  return html`<${IndependentAttempt} a=${independent.attempt} link=${false} gateOnly=${true} />`;
}
