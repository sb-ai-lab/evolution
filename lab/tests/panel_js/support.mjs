import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const STATIC = process.env.LAB_PANEL_STATIC;
const FIXTURES = process.env.LAB_PANEL_FIXTURES;
if (!STATIC || !FIXTURES) throw new Error("run through tests/test_panel_js.py (LAB_PANEL_STATIC, LAB_PANEL_FIXTURES)");

export const staticModule = (rel) => import(pathToFileURL(join(STATIC, rel)).href);

export function canonical(pathAndQuery) {
  const [rawPath, query = ""] = pathAndQuery.split("?", 2);
  const path = decodeURIComponent(rawPath);
  if (!query) return path;
  const pairs = query.split("&").map((p) => p.split("=", 2).map(decodeURIComponent));
  pairs.sort((a, b) => (a[0] + "\u0000" + a[1] < b[0] + "\u0000" + b[1] ? -1 : 1));
  return `${path}?${pairs.map(([k, v = ""]) => `${k}=${v}`).join("&")}`;
}

// Test-only adaptation of historical captures; producing bytes/metadata are never changed.
function historicalForm(body) {
  if (!["lab-campaign-view/8", "lab-campaign-view/9", "lab-owner-report/5", "lab-owner-report/6"].includes(body?.schema)) return body;
  const result = structuredClone(body);
  const campaign = body.schema.startsWith("lab-campaign-view/");
  const oneLayer = ["lab-campaign-view/8", "lab-owner-report/5"].includes(body.schema);
  result.schema = campaign ? "lab-campaign-view/10" : "lab-owner-report/7";
  const block = campaign ? (result.family === 15 ? result.independent : null) : result.report?.lab_independent;
  if (block && typeof block === "object" && !Array.isArray(block)) {
    if (block.settings) block.settings.bound_fallback ??= null;
    const explanation = (p) => {
      if (!p) return;
      p.bound_fallback ??= null;
      for (const s of p.scopes ?? []) s.fallback ??= null;
    };
    for (const p of block.attainability?.preflight ?? []) explanation(p.explanation);
    for (const a of block.attempts ?? []) {
      if (oneLayer) {
        if (a.comparison !== null) a.comparison.reference_measurement_id = null;
        const global = a.comparison !== null && a.comparison.deciding_layer === "global";
        a.gate_comparison = global ? null : structuredClone(a.comparison);
        a.global_comparison = global ? structuredClone(a.comparison) : null;
      }
      // Old captures contain no authenticated binding DTO; never infer one from a result.
      a.gate_binding = a.global_binding = null;
      explanation(a.preflight);
      for (const c of [a.comparison, a.gate_comparison, a.global_comparison]) {
        if (!c) continue;
        for (const key of ["method", "unit", "calibration", "caveats", "bound_fallback"]) c[key] ??= null;
        for (const s of c.scopes ?? []) for (const key of ["fallback", "lower", "exact", "pairing"]) s[key] ??= null;
      }
    }
  }
  return result;
}

// Approved synthetic dev61/dev62 copies, never current-pair capture evidence.
// Native /10 bindings stay intact; no Owner probe facts are invented from historical captures.
function measurementForms(body) {
  const schema = body?.schema ?? "";
  const measurement = ["lab-measurement/2", "lab-measurement-list/2"].includes(schema);
  const oldReport = schema.startsWith("lab-owner-report/") && body.report?.operation === "evaluation"
    && !Object.hasOwn(body.report, "lab_notices_state");
  if (!measurement && !oldReport) return body;
  const result = structuredClone(body);
  if (measurement) result.schema = schema.replace("/2", "/3");
  const rows = schema === "lab-measurement/2" ? [result.measurement]
    : schema === "lab-measurement-list/2" ? result.measurements ?? [] : [];
  for (const row of rows) {
    row.lab_notices ??= [];
    row.lab_notices_state ??= row.role === "evaluation" ? "unavailable" : "present";
  }
  if (oldReport) Object.assign(result.report, {lab_notices: [], lab_notices_state: "unavailable"});
  return result;
}
export function currentForm(body) {
  const historical = measurementForms(historicalForm(body));
  if (["lab-campaign-view/12", "lab-campaign-view/13", "lab-owner-report/10", "lab-owner-report/11"].includes(historical?.schema))
    return { ...structuredClone(historical), schema: historical.schema.startsWith("lab-campaign-view/")
      ? "lab-campaign-view/15" : "lab-owner-report/15", ...(historical.schema.startsWith("lab-campaign-view/") ? {lab_notices_state: "unavailable"} : {}) };
  if (!["lab-campaign-view/10", "lab-owner-report/7", "lab-owner-report/8"].includes(historical?.schema)) return historical;
  const result = structuredClone(historical);
  Object.assign(result, { reflector_probe: null, reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" });
  if (result.schema === "lab-campaign-view/10") {
    result.schema = "lab-campaign-view/15";
    result.lab_notices_state = "unavailable";
    if (result.family === 15) Object.assign(result, { checks: null, checks_state: "present", checks_reason: null });
  } else {
    result.schema = "lab-owner-report/15";
    result.core_terminal ??= null;
    if (result.report?.operation === "campaign") Object.assign(result.report, { reflector_probe: null,
      reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" });
    for (const start of result.starts ?? []) start.core_terminal ??= null;
    if (result.report && Object.hasOwn(result.report, "lab_independent"))
      Object.assign(result.report, { lab_checks: null, lab_checks_state: "present", lab_checks_reason: null });
  }
  return result;
}

function currentPayload(value) {
  if (Array.isArray(value)) return value.map(currentPayload);
  if (value && typeof value === "object")
    return Object.fromEntries(Object.entries(currentForm(value)).map(([k, v]) => [k, currentPayload(v)]));
  return value;
}
function currentEvent(frame) {
  const lines = frame.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  const data = lines.filter((line) => line.startsWith("data:"));
  if (!data.length) return frame;
  const payload = JSON.parse(data.map((line) => line.slice(5).replace(/^ +/, "").replace(/[\r\n]+$/, "")).join("\n"));
  const adapted = currentPayload(payload);
  if (JSON.stringify(adapted) === JSON.stringify(payload)) return frame;
  const first = lines.findIndex((line) => line.startsWith("data:"));
  return lines.map((line, i) => i === first ? `data: ${JSON.stringify(adapted)}\n` : line.startsWith("data:") ? "" : line).join("");
}

const cache = new Map();
export function snapshot(name) {
  if (!cache.has(name)) {
    const read = (part) => JSON.parse(readFileSync(join(FIXTURES, name, `${part}.json`), "utf-8"));
    const routes = read("routes");
    let events = read("events");
    for (const entry of Object.values(routes)) entry.body = currentPayload(entry.body);
    events = Object.fromEntries(Object.entries(events).map(([op, frames]) => [op, frames.map(currentEvent)]));
    cache.set(name, { routes, events, meta: read("meta") });
  }
  return cache.get(name);
}

export function body(name, path) {
  const entry = snapshot(name).routes[canonical(path)];
  if (!entry) throw new Error(`no fixture route ${path} in ${name}`);
  return entry.body;
}

// Renders a hook-free component tree to {text, elements}; never through innerHTML.
export function inspect(node, result = { text: "", elements: [] }) {
  if (node == null || typeof node === "boolean") return result;
  if (Array.isArray(node)) { node.forEach((child) => inspect(child, result)); return result; }
  if (typeof node !== "object") { result.text += String(node); return result; }
  if (typeof node.type === "function") return inspect(node.type(node.props), result);
  result.elements.push(node);
  assert.equal(node.props?.dangerouslySetInnerHTML, undefined);
  inspect(node.props?.children, result);
  return result;
}
