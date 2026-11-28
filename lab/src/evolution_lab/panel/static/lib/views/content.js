// lib/views/content.js — lab-sample/1, lab-attempt/1, lab-reflection/1 (spec 2026-10-13 §3.4) as screens show them.
// No judgments: states and reasons are Lab's codes; private parts are either withheld (the answer says so) or shown
// as given; "[REDACTED]" is core's or Lab's mask, shown as is with a hint.
import { label } from "../labels.js";
import { readOf } from "../resource.js";
import { intervalOf } from "./confidence.js";

// Only section unions listed in §3.5 accept this exact sentinel; user payloads are not inspected.
export const isTooLarge = (v) => v !== null && typeof v === "object" && !Array.isArray(v)
  && Object.keys(v).length === 2 && v.state === "unavailable" && v.reason === "too_large";
const withheld = (v) => v !== null && typeof v === "object" && v.withheld === "private";
const str = (v) => (v === null || v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v, null, 2));
export const REDACTED_HINT = "[REDACTED]: masked by core or Lab — credentials or hidden reasoning (ADR-0019 p. 4)";

// Every recorded key of an event but its type and name, in recorded order; a private-marked value stays "(private)".
const eventText = (e) => Object.entries(e).filter(([k]) => k !== "type" && k !== "name")
  .map(([k, v]) => (withheld(v) ? `${k}: (private)` : `${k}: ${str(v)}`)).join("\n");
// An assistant_message that records only its content reads as that content; anything more keeps its keys. Its
// name goes to the label, like a technical event's (final review item 9).
const saidText = (e) => {
  const keys = Object.keys(e).filter((k) => k !== "type" && k !== "name");
  if (keys.length === 1 && keys[0] === "content") return withheld(e.content) ? "(private)" : str(e.content);
  return eventText(e);
};
const scalar = (v) => (v === null ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v));

// The turn's usage and latency as one technical row: target calls only, so it is labelled partial (PDR-0017).
function usageRow(t) {
  const parts = [];
  if (t.usage !== null && typeof t.usage === "object") parts.push(...Object.entries(t.usage));
  else if (t.usage !== undefined) parts.push(["usage", t.usage]);
  if (t.latency_ms !== undefined) parts.push(["latency_ms", t.latency_ms]);
  return parts.length === 0 ? null
    : { type: "usage", name: null, text: parts.map(([k, v]) => `${k} ${scalar(v)}`).join(" · "), note: "Partial: target calls only" };
}

// Spec family14 §3.7: the boundary follows the recorded event type only. Main view: the turn input
// (message.content) and assistant_message events; technical: every other event in recorded order, then usage.
// `count` is everything the "Technical events (n)" disclosure holds (rows + message.fields), so nothing is
// hidden without a trace. lab-sample/1 turns carry no role: none is inferred.
function turnOf(t) {
  const events = t.events ?? [];
  const dialogue = events.filter((e) => e.type === "assistant_message").map((e) => ({ type: e.type, name: e.name ?? null, text: saidText(e) }));
  const technical = events.filter((e) => e.type !== "assistant_message")
    .map((e) => ({ type: e.type, name: e.name ?? null, text: eventText(e) }));
  const usage = usageRow(t);
  if (usage) technical.push(usage);
  // A recorded null is a value (shown "null" and counted); only an absent key is undefined (mock RULES).
  const fields = t.message !== null && typeof t.message === "object" && Object.hasOwn(t.message, "fields") ? t.message.fields : undefined;
  return { index: t.index, input: str(t.message?.content), dialogue, technical, fields,
    count: technical.length + (fields !== undefined ? 1 : 0), outcome: t.outcome,
    reason: Object.hasOwn(t, "reason") ? t.reason : undefined, noAssistant: dialogue.length === 0 };
}

// A dialogue entry's label: "Turn N · assistant_message", then its recorded name.
export const saidLabel = (index, d) => `Turn ${index} · ${d.type}${d.name !== null && d.name !== undefined ? ` · ${d.name}` : ""}`;

// One wording on the case head and the side (Task 10 item 11): "critical pass" / "critical fail" / "critical —".
export const criticalText = (v) => `critical ${v === true ? "pass" : v === false ? "fail" : "—"}`;

// "2 cases · click a row or use ↑ / ↓" (additions mock block 7); rows are samples, so repeats are counted apart.
export function casesCaption(rows) {
  const cases = new Set(rows.map((r) => r.caseId)).size;
  const counts = [`${cases} ${cases === 1 ? "case" : "cases"}`];
  if (rows.length !== cases) counts.push(`${rows.length} samples`);
  return `${counts.join(" · ")} · click a row or use ↑ / ↓`;
}

// The case body while private content is asked for (spec family14 §3.7, Task 10 item 3): the public answer stays
// shown while the private read loads or fails; `privateRead` (null unless asked) is the private block's own state.
// Codex r1: Owner budgets each response's sections on its own (a readable public section can be too_large in the
// private answer), so the body and its read are always the public ones; a successful private read adds only
// `private` and the truncation marks under /private.
const underPrivate = (t) => t?.path === "/private" || String(t?.path).startsWith("/private/");
function withPrivate(pub, priv) {
  const marks = (priv.truncation ?? []).filter(underPrivate);
  return { ...pub, private: priv.private, truncated: pub.truncated === true || marks.length > 0,
    truncation: [...(pub.truncation ?? []).filter((t) => !underPrivate(t)), ...marks] };
}
export function revealOf(publicEntry, privateEntry, shown) {
  const pub = readOf(publicEntry), priv = shown ? readOf(privateEntry) : null;
  const both = pub.state === "ok" && priv?.state === "ok";
  return { read: pub, body: both ? withPrivate(publicEntry.data, privateEntry.data) : publicEntry?.data, privateRead: priv };
}

export function sampleOf(body) {
  const trace = body.trace ?? { state: "unavailable", reason: "unknown" };
  return {
    state: trace.state, reason: trace.reason ?? null, masking: body.masking, truncated: body.truncated === true,
    truncation: body.truncation ?? [],
    // Keep the wrapper: a public payload may itself look exactly like the section sentinel.
    input: body.input ?? null,
    turns: isTooLarge(body.turns) ? body.turns : (body.turns ?? []).map(turnOf),
    omitted: body.turns_omitted ?? 0, terminal: body.terminal ?? null,
    // Public scoring is criterion, passed and public_feedback (PDR-0017); notes are private only.
    scoring: isTooLarge(body.scoring) ? body.scoring : { state: body.scoring?.state ?? "not_scored", metrics: body.scoring?.metrics ?? null,
      verdicts: (body.scoring?.verdicts ?? []).map((v) => ({ criterion: v.criterion, passed: v.passed })),
      feedback: body.scoring?.public_feedback ?? null, critical: body.scoring?.critical_pass ?? null },
    private: isTooLarge(body.private) ? body.private : withheld(body.private) ? { withheld: true }
      : { oracle: body.private?.oracle ?? null, diagnostics: body.private?.diagnostics ?? null,
          details: body.private?.details ?? null, notes: body.private?.verdict_notes ?? [],
          scorerOutput: body.private?.scorer_output ?? null },
  };
}

// The baseline side of a case (spec family14 §3.7): read only for a verified pair — the measurement row of this
// case and sample carries `baseline` (Owner sets it only when the baseline measurement scored the same case_id
// and sample_id) and the answer names the baseline measurement. Otherwise the row's comparison code, no read.
export function baselinePairOf(measurement, caseId, sampleId) {
  const row = (measurement?.samples ?? []).find((s) => s.case_id === caseId && s.sample_id === sampleId);
  const base = measurement?.baseline?.measurement_id;
  if (row && row.baseline !== null && typeof row.baseline === "object" && typeof base === "string")
    return { measurementId: base, sampleId: row.sample_id };
  return { unpaired: row?.comparison ?? "unknown" };
}

const TITLES = { input: "Input", model_message: "Model", tool_call: "Tool call", tool_result: "Tool result",
  published: "Published", reflected: "Reflected", rejected: "Rejected", failed: "Failed", agent_summary: "Agent summary" };

export function roleOf(body, role) {
  const part = role === "editor" ? body.editor : body;
  const events = isTooLarge(part.events) ? part.events : (part.events ?? []).map((e) => withheld(e) || e.withheld === "private"
    ? { type: e.type, title: TITLES[e.type] ?? e.type, turn: e.turn ?? null, withheld: true, text: "" }
    : { type: e.type, title: TITLES[e.type] ?? e.type, turn: e.turn ?? null, name: e.name ?? null, withheld: false,
        text: Object.entries(e).filter(([k]) => !["type", "turn", "id", "name"].includes(k))
          .map(([k, v]) => (withheld(v) ? `${k}: (private)` : `${k}: ${str(v)}`)).join("\n") });
  return {
    kind: part.kind, state: part.state, reason: part.reason ?? null, events, omitted: part.events_omitted ?? 0,
    toolCalls: part.tool_calls, changed: part.changed ?? null, changedCount: part.changed_count ?? null,
    failure: isTooLarge(part.failure) ? part.failure : part.failure ? { text: part.failure.text ?? null, source: part.failure.text_source ?? null,
      codes: [part.failure.error_class, part.failure.reason].filter(Boolean).join(" · ") } : null,
    stderrTail: withheld(part.stderr_tail) ? { withheld: true } : part.stderr_tail ?? null,
    truncated: body.truncated === true, truncation: body.truncation ?? [], disclosure: body.disclosure,
    confidence: role === "editor" ? body.confidence ?? null : null,
  };
}

export const looksOf = (confidence) => isTooLarge(confidence) ? confidence : (confidence?.looks ?? []).map((l) => ({
  look: l.look, final: l.final === true, decision: label("confidence_decision", l.decision), reason: l.reason,
  focus: intervalOf(l.focus), causal: intervalOf(l.causal),
}));
