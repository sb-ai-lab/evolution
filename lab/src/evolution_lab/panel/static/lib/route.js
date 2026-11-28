// lib/route.js — hash routes (spec §2.2). Ids are allowlist path segments: never encoded, except core case/sample ids in lib/api.js.
export const SEGMENT = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
export const MEASUREMENT_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}(?:@e[1-9][0-9]*)?$/;
export const REF = /^(?:initial|champion:v[0-9]+|attempt:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$/;
// (?![\s\S]) requires the actual end, including rejection of a final newline.
export const CORE_ID = /^[\p{L}\p{N}_][\p{L}\p{N}_.:@+\-]{0,255}(?![\s\S])/u;
export const ATTEMPT_ID = /^r[0-9]{2,}-a[0-9]{2,}(?![\s\S])/;
export const REFLECTION_ID = /^r[0-9]{2,}-x[0-9]{2,}(?![\s\S])/;
const ok = (value, pattern = SEGMENT) => typeof value === "string" && pattern.test(value);

export function parse(hash) {
  const raw = (hash || "").replace(/^#/, "");
  const [path, query = ""] = raw.split("?", 2);
  const parts = path.split("/").filter(Boolean);
  const notFound = { screen: "not_found", params: { hash: hash || "" } };
  if (parts.length === 0 || (parts.length === 1 && parts[0] === "projects")) return { screen: "projects", params: {} };
  if (parts[0] === "experiments" && parts.length === 1) return { screen: "experiments", params: {} };
  if (parts[0] === "states" && parts.length === 1) return { screen: "states", params: {} };
  if (parts[0] === "projects" && ok(parts[1])) {
    if (parts.length === 2) return { screen: "project", params: { project: parts[1] } };
    if (parts.length === 3 && parts[2] === "facts") return { screen: "facts", params: { project: parts[1] } };
  }
  if (parts[0] === "operations" && ok(parts[1])) {
    if (parts.length === 2) return { screen: "operation", params: { operation: parts[1] } };
    if (parts.length === 4 && parts[2] === "measurements" && ok(parts[3], MEASUREMENT_SEGMENT))
      return { screen: "measurement", params: { operation: parts[1], measurement: parts[3] } };
    if (parts.length === 3 && parts[2] === "diff") {
      const q = Object.fromEntries(query.split("&").filter(Boolean).map((p) => p.split("=", 2)));
      const from = q.from ?? "initial", to = q.to ?? null;
      if (REF.test(from) && (to === null || REF.test(to))) return { screen: "diff", params: { operation: parts[1], from, to } };
    }
  }
  return notFound;
}

export const href = {
  projects: () => "#/projects",
  project: (id) => (ok(id) ? `#/projects/${id}` : null),
  facts: (id) => (ok(id) ? `#/projects/${id}/facts` : null),
  experiments: () => "#/experiments",
  operation: (id) => (ok(id) ? `#/operations/${id}` : null),
  measurement: (op, m) => (ok(op) && ok(m, MEASUREMENT_SEGMENT) ? `#/operations/${op}/measurements/${m}` : null),
  diff: (op, from = "initial", to = null) => (!ok(op) ? null : to === null ? `#/operations/${op}/diff`
    : REF.test(from) && REF.test(to) ? `#/operations/${op}/diff?from=${from}&to=${to}` : null),
  states: () => "#/states",
};
