// lib/views/confidence.js — family 14 blocks of lab-campaign-view/8: an
// attempt's confidence (core's latest evaluated look, folded by Lab). Codes as core gave
// them (tenet one-campaign-authority); nothing is recomputed (PDR-0017). The view carries no `unavailable` reason for a
// missing bound: the text says the view has none, the attempt read of wave 2b gives the reason.
import { label } from "../labels.js";
import { number } from "../format.js";

// Keep the supplied kind beside its label, including unknown codes through the usual label fallback.
export const caveatOf = (c) => ({ code: c.kind, text: label("caveat", c.kind).text });

export function intervalText(i) {
  if (!i) return "—";
  const lowerMissing = i.lower === null || i.lower === undefined;
  const upperMissing = i.upper === null || i.upper === undefined;
  if (lowerMissing && upperMissing) return `${number(i.point)} · no bounds in the view`;
  const bounds = `${number(i.point)} [${number(i.lower)}, ${number(i.upper)}]`;
  return lowerMissing || upperMissing ? `${bounds} · ${lowerMissing ? "no lower bound" : "no upper bound"} in the view` : bounds;
}

// Spec family14 §3.5: recorded numbers stay numbers; a missing bound is null, never 0; one `exact` per interval (§4.1).
const num = (x) => (typeof x === "number" && Number.isFinite(x) ? x : null);
export const intervalOf = (i) => (i ? { point: num(i.point), lower: num(i.lower), upper: num(i.upper),
  exact: typeof i.exact === "boolean" ? i.exact : null, text: intervalText(i) } : null);
// Only an interval with both bounds is drawn.
export const bounded = (i) => i !== null && i !== undefined && i.lower !== null && i.upper !== null;

// One scale for the drawn rows (arithmetic only): min(lower, 0) .. max(upper, 0), 10% of the span on each side.
// Rows without both bounds are not drawn and do not widen it; no drawn row, no scale.
export function scaleOf(intervals) {
  const drawn = intervals.filter(bounded);
  if (!drawn.length) return null;
  const lo = Math.min(0, ...drawn.map((i) => i.lower)), hi = Math.max(0, ...drawn.map((i) => i.upper));
  const pad = (hi - lo) * 0.1 || 0.1;
  return { min: lo - pad, max: hi + pad };
}

// Lab's calibration block as recorded: degenerate false is shown, never dropped.
function calibrationText(c) {
  if (c === null || c === undefined) return "—";
  if (c.unavailable) return c.unavailable;
  return `allowance ${number(c.allowance)} · degenerate ${c.degenerate === true ? "yes" : c.degenerate === false ? "no" : "—"}`;
}

export function confidenceOf(block) {
  if (!block) return null;
  const base = { mode: block.mode, observe: block.mode === "observe", repeats: block.repeats,
                 cases: { focus: block.focus_cases, causal: block.causal_cases },
                 margin: number(block.margin), marginValue: num(block.margin), calibration: calibrationText(block.calibration) };
  const l = block.latest;
  if (!l) return { ...base, pending: true, focus: null, causal: null, scale: null };
  const focus = intervalOf(l.focus), causal = intervalOf(l.causal);
  return { ...base, pending: false, look: l.look, final: l.final === true,
           decision: label("confidence_decision", l.decision), reason: l.reason ?? "",
           focus, causal, scale: scaleOf([focus, causal]) };
}
