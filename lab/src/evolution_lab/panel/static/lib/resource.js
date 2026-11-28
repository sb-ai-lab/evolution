// lib/resource.js — one read's state as the hooks keep it and the models take it (spec §2.3, §2.5; tenet
// inspectable-experiments: what is missing is shown, not left empty). Pure; no fetch, no DOM.
//
// An entry: {state: "loading"|"ok"|"error", data, code, stale}. `data` is the last good answer (undefined:
// none yet); an error with data keeps it, stale. A model never reads a missing entry as an empty answer.

export const LOADING = Object.freeze({ state: "loading", data: undefined, code: null, stale: false });
export const okEntry = (data) => ({ state: "ok", data, code: null, stale: false });
// A failed read that keeps `data`, its last answer (if any), stale.
export const errorEntry = (code, data) => ({ state: "error", data, code, stale: data !== undefined });

// api.get rejects with ApiError {code}, or with a raw TypeError (network) / SyntaxError (a 200 that is
// not JSON): all three are an error state of the resource, never an exception of the screen.
export const errorCode = (e) => e?.code ?? (e instanceof SyntaxError ? "invalid_json" : "network_error");

// What a view can say about one read: "ok" — data to show (stale: the last good answer, not the current
// one); "loading" — no answer yet; "unavailable" — the read failed and nothing was read before (code).
export function readOf(entry) {
  const e = entry ?? LOADING;
  if (e.data !== undefined) return { state: "ok", code: null, stale: Boolean(e.stale) };
  if (e.state === "error") return { state: "unavailable", code: e.code ?? "unknown", stale: false };
  return { state: "loading", code: null, stale: false };
}

// The first read that is not "ok" among several (unavailable before loading), else ok; stale if any is.
export function worstRead(reads) {
  const bad = reads.find((r) => r.state === "unavailable") ?? reads.find((r) => r.state === "loading");
  return bad ?? { state: "ok", code: null, stale: reads.some((r) => r.stale) };
}

// While the shell observes an outage (the panel unreachable or the service down), an answer read before
// it is the last known one, not the current one: stale, even for a resource read once and not polled.
export function withOutage(entry, outage) {
  return outage && entry.data !== undefined && !entry.stale ? { ...entry, stale: true } : entry;
}

// One settled read of several paths ([[key, path]], Promise.allSettled results) over the previous entries
// (with their `path`): each key keeps its own state — one failed path never blanks another. A failed key
// keeps its previous answer of the same path, else the last cached one (`last(path)`), stale.
export function settleEntries(previous, pairs, settled, last = () => undefined) {
  const entries = {};
  settled.forEach((s, i) => {
    const [key, path] = pairs[i];
    if (s.status === "fulfilled") { entries[key] = { path, ...okEntry(s.value) }; return; }
    const before = previous?.[key]?.path === path ? previous[key].data : undefined;
    const data = before ?? last(path);
    entries[key] = { path, ...errorEntry(errorCode(s.reason), data) };
  });
  return entries;
}

// The whole map at a glance: the first error code, stale if any entry is, loading until every key answered.
export function summarise(entries, keys) {
  const list = keys.map((k) => entries[k] ?? LOADING);
  const failed = list.find((e) => e.state === "error");
  const state = failed ? "error" : list.every((e) => e.state === "ok") ? "ok" : "loading";
  return { state, code: failed?.code ?? null, stale: list.some((e) => e.stale) };
}
