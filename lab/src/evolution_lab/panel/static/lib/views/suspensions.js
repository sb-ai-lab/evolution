// Closed lab-suspension-evidence/1 reader and on-demand resource. No status/verdict authority.
import { paths } from "../api.js";
import { href } from "../route.js";
import { errorCode, errorEntry, LOADING, okEntry, readOf } from "../resource.js";
import { supportedCampaign } from "./campaign.js";

export const suspensionSeq = (campaign) => supportedCampaign(campaign)?.suspensions?.at(-1)?.seq ?? null;
const text = (v) => typeof v === "string" && v.length > 0;
const nullable = (test) => (v) => v === null || test(v);
const integer = Number.isInteger;
const keys = (v, required, optional = []) => v !== null && typeof v === "object" && !Array.isArray(v)
  && required.every((k) => Object.hasOwn(v, k)) && Object.keys(v).every((k) => required.includes(k) || optional.includes(k));
const rowIds = ["measurement_id", "case_id", "sample_id", "retry_ordinal"];
const metadata = ["count", "rows_digest", "truncated"];
const kinds = {
  fault_rows: metadata, final_rows: metadata, champion_rows: [...metadata, "measurement_id"],
  threshold: ["crossing", "window_start"], stage_record: ["seq", "operation_id"],
};
const crossingFields = ["owner", "kind", "count", "denominator", "max_count", "max_rate"];
const unavailableReasons = new Set(["not_campaign", "core_raw_unsupported", "campaign_family_unsupported",
  "campaign_missing", "campaign_unreadable", "unsupported_schema"]);
function validRecord(v) {
  if (!keys(v, ["seq", "reason", "owner", "evidence_kind", "evidence", "rows"])
    || !integer(v.seq) || !text(v.reason) || !text(v.owner)
    || !text(v.evidence_kind) || !Object.hasOwn(kinds, v.evidence_kind)) return false;
  const kind = v.evidence_kind, e = v.evidence;
  if (!keys(e, kinds[kind], kind === "stage_record" ? ["prefix"] : [])) return false;
  if (kind.endsWith("_rows")) return integer(e.count) && text(e.rows_digest) && typeof e.truncated === "boolean"
    && (kind !== "champion_rows" || text(e.measurement_id)) && Array.isArray(v.rows) && v.rows.every((r) =>
      keys(r, [...rowIds, ...(kind === "fault_rows" ? ["reason", "code"] : ["owner"])])
      && ["measurement_id", "case_id", "sample_id"].every((k) => text(r[k])) && integer(r.retry_ordinal)
      && (kind === "fault_rows" ? text(r.reason) && nullable(text)(r.code) : text(r.owner)));
  if (v.rows !== null) return false;
  if (kind === "threshold") return nullable(integer)(e.window_start) && keys(e.crossing, crossingFields)
    && ["owner", "kind", "max_rate"].every((k) => text(e.crossing[k]))
    && ["count", "denominator", "max_count"].every((k) => integer(e.crossing[k]));
  return integer(e.seq) && nullable(text)(e.operation_id) && (!Object.hasOwn(e, "prefix") || text(e.prefix));
}
function supported(body, operationId) {
  return keys(body, ["schema", "operation_id", "state", "source", "reason", "suspensions"])
    && body.schema === "lab-suspension-evidence/1" && body.operation_id === operationId && body.source === "core_raw"
    && (body.state === "available" ? body.reason === null && Array.isArray(body.suspensions)
      && body.suspensions.every(validRecord) : body.state === "unavailable"
      && unavailableReasons.has(body.reason) && body.suspensions === null);
}

export function suspensionModel({ operationId, seq = null, entry } = {}) {
  let read = readOf(entry);
  const body = entry?.data;
  const base = { records: [], latest: null, older: [], current: null, empty: false, newerSeq: null };
  if (body === undefined) return { ...base, read };
  if (!supported(body, operationId)) return { ...base, read: { state: "unavailable", code: "unsupported_schema", stale: false } };
  if (body.state === "unavailable") return { ...base, read: { state: "unavailable", code: body.reason, stale: read.stale } };
  const records = [...body.suspensions].reverse().map((r) => ({ ...r,
    measurementHref: r.evidence_kind === "champion_rows" ? href.measurement(operationId, r.evidence.measurement_id) : null,
    rows: r.rows?.map((row) => ({ ...row, measurementHref: href.measurement(operationId, row.measurement_id) })) ?? null,
    shown: r.rows?.length ?? null, count: r.evidence.count ?? null, truncated: r.evidence.truncated ?? null,
  }));
  const latest = records[0] ?? null;
  const behind = seq !== null && latest?.seq !== seq;
  read = { ...read, stale: read.stale || behind };
  return { read, records, latest, older: records.slice(1), current: seq !== null && latest?.seq === seq ? latest : null,
    empty: records.length === 0 && !read.stale,
    // "Stale · newer pause seq N" (mock "Read states"): the campaign view names a pause this answer does not have yet.
    newerSeq: behind && (latest === null || latest.seq < seq) ? seq : null };
}

// The threshold's Observed meter (ruling R9): fill = count / denominator, drawn within the bar; the limit tick only
// when max_rate parses to a finite rate in [0, 1]. Plain arithmetic over the recorded numbers (PDR-0017).
export function meterOf(crossing) {
  const fill = crossing.denominator > 0 ? Math.min(1, Math.max(0, crossing.count / crossing.denominator)) : null;
  const rate = typeof crossing.max_rate === "string" && crossing.max_rate.trim() !== "" ? Number(crossing.max_rate) : NaN;
  return { fill, tick: Number.isFinite(rate) && rate >= 0 && rate <= 1 ? rate : null };
}

// `update` is driven by disclosure/screen state, campaign settlement, operation and latest pause seq.
// No shell epoch, status tick or ordinary refresh can schedule a read here.
export function createSuspensionResource(api, changed = () => {}) {
  let config = { operationId: null, seq: null, open: false, campaignSettled: false }, entry = LOADING;
  let generation = 0, flight = null, closed = false, deleted = false;
  const purge = () => {
    deleted = true; generation++; flight = null;
    api.forget(paths.suspensions(config.operationId));
    entry = errorEntry("results_deleted", undefined);
    changed();
  };
  const snapshotFor = ({ operationId = config.operationId, seq = config.seq, deleted: gone = false } = {}) => {
    if (gone || (deleted && operationId === config.operationId)) return errorEntry("results_deleted", undefined);
    if (operationId !== config.operationId) return LOADING;
    return seq !== config.seq && entry.data !== undefined ? { ...entry, stale: true } : entry;
  };
  return {
    snapshot: () => entry,
    snapshotFor,
    update(next) {
      if (closed) return;
      const sameOp = next.operationId === config.operationId;
      const sameKey = sameOp && next.seq === config.seq;
      const campaignSettled = next.campaignSettled !== false;
      const opening = next.open && (!config.open || (campaignSettled && !config.campaignSettled));
      if (!sameKey) {
        generation++; flight = null;
        if (!sameOp) { entry = LOADING; deleted = false; }
        else {
          api.forget(paths.suspensions(next.operationId));
          entry = entry.data === undefined ? LOADING : { ...entry, stale: true };
        }
      }
      config = { operationId: next.operationId, seq: next.seq, open: next.open, campaignSettled };
      if (next.deleted || deleted) { purge(); return; }
      if (!campaignSettled || !next.open || (!opening && sameKey) || flight !== null) { changed(); return; }
      const path = paths.suspensions(next.operationId);
      if (!path) { entry = errorEntry("invalid_id", undefined); changed(); return; }
      const ticket = ++generation;
      flight = ticket;
      if (entry.data !== undefined) entry = { ...entry, stale: true };
      changed();
      api.get(path).then((body) => {
        if (closed || ticket !== generation) return;
        entry = supported(body, next.operationId) ? okEntry(body) : errorEntry("unsupported_schema", undefined);
      }, (error) => {
        if (closed || next.operationId !== config.operationId) return;
        // Deletion belongs to the operation, even when a newer seq read has already answered.
        if (errorCode(error) === "results_deleted") purge();
        else if (ticket === generation) entry = errorEntry(errorCode(error), entry.data);
      }).finally(() => {
        if (closed || ticket !== generation) return;
        flight = null; changed();
      });
    },
    close() { closed = true; generation++; flight = null; entry = LOADING; },
  };
}
