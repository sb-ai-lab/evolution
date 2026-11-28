// lib/views/independent.js — family 15 on the Campaign screen (spec 2026-10-19 D8): the `independent` block of
// lab-campaign-view/15 in words. The English statements mirror src/evolution_lab/_independent_text.py, so the panel
// and the CLI say the same thing.
// - Each scope reads by its own `display` (bound | bootstrap | point | no_threshold | status, computed once in Owner,
//   owner/independent_view.py), never by the campaign mode (codex r2).
// - Q5: the bound rule's L is `enclosure.lower`; bootstrap has its own `lower`. `enclosure.upper` brackets
//   L's computation and is never shown, and no two-sided interval is drawn.
// - Q2 as corrected: every comparison is against the round's champion; only its measurement differs, so no
//   text names a Parent.
// - Numbers are `{value, exact}`: only `value` is read, for display. Lab compares and recomputes nothing
//   (tenet one-campaign-authority).
import { independentText, label } from "../labels.js";
import { shortId } from "../format.js";
import { href } from "../route.js";

const OUT_OF_RANGE = "outside display range (exact value kept)";
const MISSING = "—";
const NO_NUMBERS = Object.freeze({ point: null, confidence: null, lower: null, threshold: null });

// Python's float formatting, digit for digit (`_independent_text.quantity` and `percent`): the exact binary value is
// rounded half to even, and a negative sign, -0.0 included, is kept. `toFixed` and `toExponential` round an exact tie
// away from zero (L = 0.03125 would read 0.0313, the CLI 0.0312), so the rounding is done here on exact integers.
const DATA = new DataView(new ArrayBuffer(8));
function bits(x) {  // a finite x as sign, mantissa m and exponent e: |x| = m * 2^e exactly
  DATA.setFloat64(0, x);
  const hi = DATA.getUint32(0), lo = DATA.getUint32(4), biased = (hi >>> 20) & 0x7ff;
  const m = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
  return biased === 0 ? { negative: hi >>> 31 === 1, m, e: -1074 }
    : { negative: hi >>> 31 === 1, m: m | (1n << 52n), e: biased - 1075 };
}
// |x| * 10^d rounded half to even, as an integer.
function scaled(x, d) {
  const { m, e } = bits(x);
  let num = m, den = 1n;
  if (e >= 0) num <<= BigInt(e); else den <<= BigInt(-e);
  if (d >= 0) num *= 10n ** BigInt(d); else den *= 10n ** BigInt(-d);
  const q = num / den, twice = 2n * (num % den);
  return twice > den || (twice === den && q % 2n === 1n) ? q + 1n : q;
}
const sign = (x) => (bits(x).negative ? "-" : "");
// `f"{x:.{d}f}"`
function fixed(x, d) {
  const digits = scaled(x, d).toString().padStart(d + 1, "0");
  return `${sign(x)}${d > 0 ? `${digits.slice(0, -d)}.${digits.slice(-d)}` : digits}`;
}
// The p significant digits and the decimal exponent of `f"{x:.{p - 1}e}"`, x finite and not 0.
function scientific(x, p) {
  let e = Math.floor(Math.log10(Math.abs(x)));
  let n = scaled(x, p - 1 - e);
  if (n >= 10n ** BigInt(p)) n = scaled(x, p - 1 - ++e);
  else if (n < 10n ** BigInt(p - 1)) n = scaled(x, p - 1 - --e);
  return { digits: n.toString(), e };
}
const exponent = (e) => `e${e < 0 ? "-" : "+"}${String(Math.abs(e)).padStart(2, "0")}`;
// At least two and at most four decimals; `.2e` below 1e-4 (`_independent_text.quantity`).
function decimals(v) {
  if (v !== 0 && Math.abs(v) < 0.0001) {
    const { digits, e } = scientific(v, 3);
    return `${sign(v)}${digits[0]}.${digits.slice(1)}${exponent(e)}`;
  }
  const [whole, frac] = fixed(v, 4).split(".");
  return `${whole}.${frac.replace(/0+$/, "").padEnd(2, "0")}`;
}
// `f"{x:.4g}"` (`_independent_text.percent`).
function significant4(x) {
  if (x === 0) return `${sign(x)}0`;
  const { digits, e } = scientific(x, 4);
  if (e < -4 || e >= 4) return `${sign(x)}${`${digits[0]}.${digits.slice(1)}`.replace(/\.?0+$/, "")}${exponent(e)}`;
  const s = fixed(x, 3 - e);
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

// The number a quantity shows, or the words for its absence: a null `value` beside Core's exact value lies outside
// binary64's display range; nothing at all is "—". Never Infinity or NaN.
function shown(q) {
  if (q === null || q === undefined || typeof q !== "object") return { text: MISSING };
  if (q.value === null) return { text: q.exact !== null && q.exact !== undefined ? OUT_OF_RANGE : MISSING };
  return typeof q.value === "number" && Number.isFinite(q.value) ? { value: q.value } : { text: MISSING };
}
export function quantityText(q) {
  const s = shown(q);
  return s.text ?? decimals(s.value);
}
export function percentText(q) {
  const s = shown(q);
  return s.text ?? (Number.isFinite(s.value * 100) ? `${significant4(s.value * 100)}%` : OUT_OF_RANGE);
}
const thresholdOf = (scope, lang = "en") => scope?.threshold == null && scope?.rule !== "off"
  ? independentText("uncalibrated", lang)
  : `threshold ${scope?.strict === true ? ">" : "≥"} ${quantityText(scope?.threshold)}`;
const fallbackOf = (scope, lang = "en") => scope?.fallback == null ? ""
  : `; ${independentText("fallback", lang)} (${scope.fallback.reason})`;

// One scope's statement, chosen by its `display` alone.
export function scopeStatement(scope, lang = "en") {
  const statement = scopeStatementOf(scope, lang);
  return { ...statement, text: statement.text + fallbackOf(scope, lang) };
}
function scopeStatementOf(scope, lang) {
  const display = scope?.display ?? null;
  if (display === "status") {
    const code = scope.unavailable;
    return { display, text: `${label("scope_status", scope.status).text}${code ? ` (${code})` : ""}`
      + (scope.threshold == null && scope.rule !== "off" ? `; ${thresholdOf(scope, lang)}` : ""), numbers: NO_NUMBERS };
  }
  const point = quantityText(scope?.point);
  if (display === "bound" || display === "bootstrap") {
    const confidence = percentText(scope.confidence), lower = quantityText(display === "bootstrap" ? scope.lower : scope.enclosure?.lower);
    const bound = display === "bootstrap" ? `bootstrap ${confidence}, lower ${lower}` : `with ${confidence} confidence not below ${lower}`;
    return { display, text: `gain ${point} (estimate); ${bound}; ${thresholdOf(scope, lang)}`,
             numbers: { point, confidence, lower, threshold: quantityText(scope.threshold) } };
  }
  if (display === "point" || (display === "no_threshold" && scope.rule !== "off" && scope.threshold == null))
    return { display, text: `gain ${point}; ${thresholdOf(scope, lang)}`,
             numbers: { point, confidence: null, lower: null, threshold: quantityText(scope.threshold) } };
  if (display === "no_threshold")
    return { display, text: `gain ${point} · no threshold`, numbers: { ...NO_NUMBERS, point } };
  return { display, text: display === null ? MISSING : String(display), numbers: NO_NUMBERS };
}

// "vs round champion · <stored | fresh measurement | measurement not recorded>", from Lab's `reference`.
export function referenceText(comparison) {
  const ref = comparison?.reference;
  const words = ref !== null && ref !== undefined && label("comparison_reference", ref).known
    ? label("comparison_reference", ref) : label("comparison_reference", "unknown");
  return `vs round champion · ${words.text}`;
}

// Core's preflight ceilings per calculated scope, with Core's own `passes` (`_independent_text.preflight_parts`):
// a score-range ceiling, not a prediction. Skipped scopes have none.
const VERDICT = { true: "can pass", false: "cannot pass" };
function preflightOf(explanation) {
  if (!explanation || typeof explanation !== "object") return null;
  const ceilings = (explanation.scopes ?? []).filter((s) => ["calculated", "uncalibrated"].includes(s?.status)).map((s) => {
    if (s.status === "uncalibrated") return `${s.name}: ${independentText("uncalibrated")}`;
    const verdict = VERDICT[s.passes] ?? MISSING;
    return (s.rule === "bound"
      ? `${s.name} maximal confidence ${percentText(s.c_max)} vs requested ${percentText(s.confidence)} (${verdict})`
      : `${s.name} maximal gain ${quantityText(s.delta_max)} vs ${thresholdOf(s)} (${verdict})`) + fallbackOf(s);
  });
  return { status: explanation.status ?? null, ceilings,
    boundFallback: explanation.bound_fallback ?? null };
}

const count = (n) => (n === null || n === undefined ? MISSING : String(n));
// Core's conditional cost, in measurement slots (scenario slots of complete scoring, without retries).
function costOf(cost) {
  if (!cost || typeof cost !== "object") return null;
  return `${count(cost.candidate_total_slots)} measurement slots (gate ${count(cost.candidate_gate_slots)} · final `
    + `${count(cost.candidate_final_slots)} · simplest passing path ${count(cost.simplest_passing_path_slots)})`;
}

function comparisonOf(c, campaign, measurements, focusState) {
  if (!c || typeof c !== "object") return null;
  const outcome = { ...label("comparison15_outcome", c.outcome), code: c.outcome ?? null };
  return {
    outcome, reason: { ...label("comparison15_reason", c.reason), code: c.reason ?? null },
    layer: label("comparison_layer", c.layer), decidingLayer: c.deciding_layer ?? null,
    reference: { text: `${referenceText(c)}${c.reference_measurement_id ? ` · ${shortId(c.reference_measurement_id)}` : ""}`,
      measurementId: c.reference_measurement_id ?? null,
      href: c.reference_measurement_id && measurements.has(c.reference_measurement_id)
        ? href.measurement(campaign.operation_id, c.reference_measurement_id) : null },
    // Core's outcome follows the scope that decided it only, so a passing scope never reads as the failing one.
    scopes: (c.scopes ?? []).map((s) => ({ name: s.name, rule: s.rule, ...scopeStatement(s),
      explanation: s.name === "causal" && s.status === "not_measured"
        ? ({ rejected: "focus rejected, causal cases were not run",
             unavailable: "focus could not be assessed, causal cases were not run" }[focusState] ?? null) : null,
      exact: s.exact ?? null, pairing: s.pairing ?? null,
      outcome: s.name === c.deciding_layer ? outcome : null })),
    boundFallback: c.bound_fallback ?? null,
    method: c.method ?? null, unit: c.unit ?? null,
    calibration: c.calibration ? { source: c.calibration.source, method: c.calibration.method,
      level: quantityText(c.calibration.level), allowance: quantityText(c.calibration.allowance),
      retained: c.calibration.retained, degenerate: c.calibration.degenerate,
      measurementId: c.calibration.measurement_id } : null,
    caveats: c.caveats ?? [],
  };
}

// Recorded binding publications, not reconstructed from a result. Null stays explicitly unavailable.
const bindingOf = (b) => b == null ? null : { source: b.source, scopes: b.scopes.map((s) => ({ ...s })) };

function settingsOf(s) {
  if (!s || typeof s !== "object") return [];
  const noise = s.execution_noise ?? {};
  return [
    { name: "R (repeats)", value: count(s.repeats) },
    { name: "Mode", value: label("gate15_mode", s.mode).text },
    ...(s.stages === "focus_first" ? [{ name: "Stages", value: "focus first — causal measured only after focus passes" }] : []),
    { name: "Execution noise", value: `${noise.kind ?? MISSING} · allowance ${quantityText(noise.allowance)} · ${noise.source_kind ?? MISSING}` },
    { name: "Verification", value: s.verification ?? MISSING },
    ...(s.bound_fallback == null ? [] : [{ name: "bound_fallback", value: s.bound_fallback }]),
    ...(s.statistics?.method == null ? [] : [{ name: "Statistics", value: `${s.statistics.method} · unit ${s.statistics.unit ?? MISSING}` }]),
  ];
}

const EVIDENCE_OK = "authenticated_publications";

// Check modes and counts remain visible independently of the binding-backed acceptance block.
export function checksModel(campaign) {
  if (campaign?.family !== 15) return null;
  if (campaign.checks_state === "unavailable") return { state: "unavailable", reason: campaign.checks_reason };
  if (campaign.checks === null) return null;
  return { state: "present", modes: Object.entries(campaign.checks.modes), hits: Object.entries(campaign.checks.shadow_hits) };
}

// The block's model; null for family 14. A block Owner could not read is the section's state with its reason.
export function independentModel(campaign) {
  if (campaign?.family !== 15) return null;
  const block = campaign.independent;
  if (!block || typeof block !== "object") return { state: "unavailable", reason: campaign.details_reason ?? "unavailable" };
  const rounds = new Map((campaign.rounds ?? []).map((r) => [r.round,
    new Map((r.attempts ?? []).map((a) => [a.attempt_id, a]))]));
  const kind = block.evidence ?? null;
  const measurements = new Set((campaign.measurements ?? []).map((m) => m.measurement_id));
  return {
    state: "present",
    settings: settingsOf(block.settings),
    // Comparisons Lab could not authenticate are a state of their own, never "no attempts".
    evidence: { kind, text: kind === EVIDENCE_OK ? null : `comparisons unavailable (${kind ?? MISSING})`,
                label: label("independent_evidence", kind) },
    attainability: {
      rounds: (block.attainability?.preflight ?? []).map((p) => ({ round: p.round, basis: p.explanation?.basis ?? null,
        ...preflightOf(p.explanation) })),
      minimumUnits: block.attainability?.local_guidance?.minimum_units ?? null,
    },
    attempts: (block.attempts ?? []).map((a) => {
      const measurementId = rounds.get(a.round)?.get(a.attempt_id)?.measurement_id ?? null;
      const focusState = a.stages?.find((s) => s.scope === "focus")?.state;
      return {
        id: a.attempt_id, round: a.round ?? null,
        group: label("attempt_group", a.group), outcome: label("attempt15_outcome", a.outcome), reason: a.reason ?? null,
        measurementId, measurementHref: measurementId ? href.measurement(campaign.operation_id, measurementId) : null,
        gate: comparisonOf(a.gate_comparison, campaign, measurements, focusState),
        global: comparisonOf(a.global_comparison, campaign, measurements, focusState),
        gateBinding: bindingOf(a.gate_binding), globalBinding: bindingOf(a.global_binding),
        preflight: a.gate_comparison || a.global_comparison ? null : preflightOf(a.preflight),
        cost: costOf(a.conditional_cost),
        stages: a.stages?.map((s) => ({ scope: s.scope, state: s.state, ...label("stage_state", s.state) })) ?? null,
        // Core's open attempt and its recorded counts only: no stage inference or slot arithmetic.
        slotProgress: a.outcome === null && a.slots ? Object.entries(a.slots).map(([side, s]) =>
          `${side} dispatched ${s.dispatched} of ${s.scheduled} slots`).join(" · ") : null,
        eligibleFinalScore: a.eligible_final_score ? quantityText(a.eligible_final_score) : null,
      };
    }),
  };
}
