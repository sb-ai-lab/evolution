// lib/api.js — the panel's only way to the service (spec §2.3). No DOM.
import { ATTEMPT_ID, CORE_ID, MEASUREMENT_SEGMENT, REF, REFLECTION_ID, SEGMENT } from "./route.js";

export const ACTIVE = new Set(["reserved", "running", "stopping"]);
export const HEALTH_POLL_MS = 5000, LIST_POLL_MS = 10000, THROTTLE_MS = 3000;

export class ApiError extends Error {
  constructor(code, status, body) { super(code); this.code = code; this.status = status; this.body = body; }
}

export function createApi({ fetch }) {
  const memory = new Map();
  const pending = new Set();
  return {
    async get(path) {
      const ticket = { path, keep: true };
      pending.add(ticket);
      try {
        const response = await fetch(path, { headers: { accept: "application/json" } });
        if (!response.ok) {
          // A proxy or crash page may not be JSON: fall back to the status code (1b, codex r1).
          const body = await response.json().catch(() => null);
          throw new ApiError(body?.error?.code ?? `http_${response.status}`, response.status, body);
        }
        const value = await response.json();
        if (ticket.keep) memory.set(path, value);
        return value;
      } finally { pending.delete(ticket); }
    },
    // A non-JSON body of one expected type (spec 2026-10-14 report-markdown §5, ADR-0022 p. 3); not remembered.
    async getText(path, type) {
      const response = await fetch(path, { headers: { accept: type } });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new ApiError(body?.error?.code ?? `http_${response.status}`, response.status, body);
      }
      const got = (response.headers?.get?.("content-type") ?? "").split(";")[0].trim().toLowerCase();
      if (got !== type) throw new ApiError("unexpected_content_type", response.status, null);
      return response.text();
    },
    last: (path) => memory.get(path),
    forget(prefix) {
      const matches = (path) => prefix === undefined || path.startsWith(prefix);
      for (const key of memory.keys()) if (matches(key)) memory.delete(key);
      // Even a path with no cached value may have a read in flight. Eviction must survive its answer.
      for (const ticket of pending) if (matches(ticket.path)) ticket.keep = false;
    },
  };
}

const seg = (id) => typeof id === "string" && SEGMENT.test(id);
// Core case/sample ids are wider than Lab's segment grammar: the one place the panel percent-encodes (spec §3.1).
const coreSeg = (id) => (typeof id === "string" && CORE_ID.test(id) ? encodeURIComponent(id) : null);
const flag = (disclose) => (disclose ? "?disclose=private" : "");
const op = (id, tail = "") => (seg(id) ? `/v1/operations/${id}${tail}` : null);
const pj = (id, tail = "") => (seg(id) ? `/v1/projects/${id}${tail}` : null);
export const paths = {
  health: "/api/health",
  serviceHealth: "/v1/health",
  projects: "/v1/projects",
  operations: "/v1/operations?view=summary",
  operation: (id) => op(id),
  campaign: (id) => op(id, "/campaign"),
  routing: (id) => op(id, "/routing"),
  suspensions: (id) => op(id, "/suspensions"),
  report: (id) => op(id, "/report"),
  reportMarkdown: (id) => op(id, "/report.md"),
  measurements: (id) => op(id, "/measurements"),
  measurement: (id, m) => (typeof m === "string" && MEASUREMENT_SEGMENT.test(m) ? op(id, `/measurements/${m}`) : null),
  sample: (id, m, c, s, disclose = false) => (typeof m === "string" && MEASUREMENT_SEGMENT.test(m)
    && coreSeg(c) !== null && coreSeg(s) !== null
    ? op(id, `/measurements/${m}/cases/${coreSeg(c)}/samples/${coreSeg(s)}${flag(disclose)}`) : null),
  attempt: (id, a, disclose = false) => (typeof a === "string" && ATTEMPT_ID.test(a) ? op(id, `/attempts/${a}${flag(disclose)}`) : null),
  reflection: (id, x, disclose = false) => (typeof x === "string" && REFLECTION_ID.test(x) ? op(id, `/reflections/${x}${flag(disclose)}`) : null),
  diff: (id, from = null, to = null) => (from === null && to === null ? op(id, "/diff")
    : REF.test(from ?? "") && REF.test(to ?? "") ? op(id, `/diff?from=${from}&to=${to}`) : null),
  events: (id) => op(id, "/events"),
  project: (id) => pj(id),
  projectStatus: (id) => pj(id, "/status"),
  projectFacts: (id) => pj(id, "/facts"),
  projectConfirmations: (id) => pj(id, "/confirmations"),
  projectOperations: (id) => pj(id, "/operations?view=summary"),
};
