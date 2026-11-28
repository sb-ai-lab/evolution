// Closed lab-operation-routing/1 read boundary. Counts/attribution are Owner observations;
// this reader never matches providers, adds counts, or judges model equivalence.
import { SEGMENT } from "../route.js";
import { readOf } from "../resource.js";

const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const closed = (v, fields) => object(v) && Object.keys(v).length === fields.length
  && fields.every((name) => Object.hasOwn(v, name));
const text = (v) => typeof v === "string" && v.trim().length > 0 && !v.includes("\0");
const count = (v) => Number.isInteger(v) && v >= 0;
const identifier = (v) => typeof v === "string" && SEGMENT.exec(v)?.[0] === v;
function endpoint(v) {
  if (!text(v)) return false;
  try {
    const url = new URL(v);
    return ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname)
      && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}
function rowOk(row) {
  if (!closed(row, ["endpoint", "upstream_model", "client_model", "requested", "reported_provider", "calls", "mismatched"])) return false;
  const requested = row.requested;
  return endpoint(row.endpoint) && text(row.upstream_model) && text(row.client_model)
    && (requested === null || text(requested) || (Array.isArray(requested) && requested.length > 0
      && requested.every(text) && new Set(requested).size === requested.length))
    && (row.reported_provider === null || text(row.reported_provider))
    && count(row.calls) && row.calls > 0 && count(row.mismatched) && row.mismatched <= row.calls;
}

export function routingShapeOk(body, operationId) {
  if (!closed(body, ["schema", "operation_id", "runs", "available", "reason", "roles", "shared", "outside"])
    || body.schema !== "lab-operation-routing/1" || !identifier(body.operation_id) || body.operation_id !== operationId
    || typeof body.available !== "boolean" || !Array.isArray(body.runs)) return false;
  let previous = 0, policy = false;
  for (const run of body.runs) {
    if (!closed(run, ["start", "policy_digest", "author", "channel", "confirmed_at", "lab_version"])
      || !count(run.start) || run.start <= previous || !text(run.lab_version)) return false;
    previous = run.start;
    if (run.policy_digest === null) {
      if ([run.author, run.channel, run.confirmed_at].some((v) => v !== null)) return false;
    } else {
      policy = true;
      if (typeof run.policy_digest !== "string" || !/^[0-9a-f]{64}(?![\s\S])/.test(run.policy_digest)
        || !text(run.author) || !text(run.channel) || !Number.isFinite(run.confirmed_at) || run.confirmed_at <= 0) return false;
    }
  }
  if (!body.available) return ["no_policy", "ledger_unreadable", "ledger_unsupported", "ledger_invalid"].includes(body.reason)
    && (body.reason === "no_policy") !== policy && [body.roles, body.shared, body.outside].every((v) => v === null);
  return policy && body.reason === null && object(body.roles) && count(body.shared) && count(body.outside)
    && Object.entries(body.roles).every(([role, rows]) => text(role) && Array.isArray(rows) && rows.every(rowOk));
}

export function routingModel({ operationId, entry } = {}) {
  const body = entry?.data;
  if (body === undefined) return { hidden: true, routing: null, read: readOf(entry) };
  if (!routingShapeOk(body, operationId)) return { hidden: false, routing: null,
    read: { state: "unavailable", code: "unsupported_schema", stale: false } };
  return { hidden: body.reason === "no_policy", routing: body, read: readOf(entry) };
}
