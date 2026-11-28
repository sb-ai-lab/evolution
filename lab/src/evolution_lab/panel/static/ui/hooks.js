// ui/hooks.js — service data for components (spec §2.3): one path or a map of paths, polled, with the
// last good answer kept (stale) when a read fails or the shell sees an outage; the panel's own health and
// the service health.
import { useEffect, useRef, useState } from "../vendor/htm-preact.js";
import { ACTIVE, HEALTH_POLL_MS, THROTTLE_MS, paths } from "../lib/api.js";
import { createThrottle } from "../lib/throttle.js";
import { readPlan, SAMPLE_CONCURRENCY } from "../lib/samples.js";
import { createReadQueue } from "../lib/queue.js";
import { createPoller } from "../lib/poll.js";
import { errorCode, errorEntry, LOADING, okEntry, settleEntries, summarise, withOutage } from "../lib/resource.js";
import { serviceState } from "../lib/views/shell.js";
import { campaignEntry } from "../lib/views/campaign.js";
import { createSuspensionResource, suspensionSeq } from "../lib/views/suspensions.js";

const LOADING_ONE = { ...LOADING, epoch: -1 };
const INVALID_ID = Object.freeze({ path: null, ...LOADING, state: "error", code: "invalid_id" });

// One poller per effect run (lib/poll.js): every pollMs (0 = once), a slow read is never overtaken;
// a new `key` or `epoch` closes the old poller, whose late answers are then dropped; `refresh` pokes it.
function usePolled(key, load, pollMs, epoch, refresh = 0) {
  const poller = useRef(null);
  useEffect(() => {
    if (key === null) return undefined;
    poller.current = createPoller(load, pollMs);
    return () => { poller.current?.close(); poller.current = null; };
  }, [key, pollMs, epoch]);
  // A `refresh` change pokes the running poller (no new poller: a read in flight is not dropped, reads never
  // stack); the initial value does not poke.
  const seen = useRef(refresh);
  useEffect(() => {
    if (seen.current === refresh) return;
    seen.current = refresh;
    poller.current?.poke();
  }, [refresh]);
}

// `refresh` re-reads (poke) without a new poller, and is not recorded as the answer's epoch (the shell's finalEpoch
// comparison stays intact).
// useResource(api, path, {pollMs, epoch, outage, refresh}) -> {state: "loading"|"ok"|"error", data, code, stale, epoch}.
// `epoch` is the shell epoch of the read that produced `data` (-1: none yet), so a caller can tell
// whether data already reflects a re-read it asked for. A null path (an id outside the segment
// grammar) is never requested. `outage`: the shell sees the panel or the service down — data read
// before it is marked stale (lib/resource.js withOutage), even when this resource is read only once.
export function useResource(api, path, { pollMs = 0, epoch = 0, outage = false, refresh = 0 } = {}) {
  const [result, setResult] = useState({ path, ...LOADING_ONE, data: path ? api.last(path) : undefined });
  usePolled(path, async (current) => {
    try {
      const data = await api.get(path);
      if (current()) setResult({ path, state: "ok", data, code: null, stale: false, epoch });
    } catch (e) {
      if (!current()) return;
      setResult((old) => {
        const same = old.path === path;
        const data = (same ? old.data : undefined) ?? api.last(path);
        return { path, state: "error", data, code: errorCode(e), stale: data !== undefined, epoch: same ? old.epoch : -1 };
      });
    }
  }, pollMs, epoch, refresh);
  if (path === null) return { ...LOADING_ONE, state: "error", code: "invalid_id" };
  if (result.path !== path) return withOutage({ ...LOADING_ONE, data: api.last(path) }, outage);
  const { path: _, ...rest } = result;
  return withOutage(rest, outage);
}

// One read on demand (a click, a screen open): no poll, no SSE refresh (spec 2026-10-13 §3.4).
// A null path reads nothing.
export const useOnDemand = (api, path) => useResource(api, path, { pollMs: 0 });

// Suspension history has its own identity and open state, independent of campaign polling and shell epochs.
export function useSuspensions(api, operationId, campaign, { open, campaignSettled = true, deleted = false, outage = false } = {}) {
  const [, repaint] = useState(0);
  const store = useRef(null);
  if (store.current === null) store.current = createSuspensionResource(api, () => repaint((n) => n + 1));
  const seq = suspensionSeq(campaign);
  const config = { operationId, seq, open, campaignSettled, deleted };
  useEffect(() => { store.current.update(config); }, [operationId, seq, open, campaignSettled, deleted]);
  useEffect(() => () => store.current.close(), []);
  return withOutage(store.current.snapshotFor(config), outage);
}

// Failed attempts only, once per screen opening; new ids join the same four-read queue.
export function useAttemptFailures(api, operationId, attemptIds) {
  const pairs = attemptIds.map((id) => [id, paths.attempt(operationId, id)]);
  const key = JSON.stringify(pairs);
  const store = useRef({ operationId, entries: {} });
  if (store.current.operationId !== operationId) store.current = { operationId, entries: {} };
  const [, repaint] = useState(0);
  const queue = useRef(null);
  useEffect(() => {
    const q = createReadQueue((path) => api.get(path), SAMPLE_CONCURRENCY, ([id, path], settled) => {
      if (store.current.operationId !== operationId) return;
      const fresh = settleEntries(store.current.entries, [[id, path]], [settled], api.last)[id];
      store.current.entries = { ...store.current.entries, [id]: fresh };
      repaint((n) => n + 1);
    });
    queue.current = q;
    return () => { q.close(); if (queue.current === q) queue.current = null; };
  }, [api, operationId]);
  useEffect(() => {
    queue.current?.enqueue(pairs.filter(([id, path]) => path !== null && !store.current.entries[id]));
  }, [api, operationId, key]);
  return Object.fromEntries(pairs.map(([id, path]) => [id, path === null ? INVALID_ID
    : store.current.entries[id] ?? { path, ...LOADING, data: api.last(path) }]));
}

// The campaign view of an operation, checked before any consumer reads fields: only lab-campaign-view/15,
// family 14 or 15; another schema or family is an unavailable read (unsupported_schema) with no data.
export const useCampaign = (api, operationId, opts) => campaignEntry(useResource(api, paths.campaign(operationId), opts));

// useResourceMap(api, {key: path|null}, opts) -> {state, code, stale, epoch, items, data}: several paths read
// together (per-project status and summaries). `items` is one entry per key (lib/resource.js) — loading,
// ok, or error with its own code and its last answer — so a view shows each key's own state; a key whose
// path is null (an id outside the segment grammar) is an error "invalid_id", never requested. `data` is
// {key: body|undefined}. state/code/stale sum up the map; `epoch` is the epoch of the last read in which
// every path answered.
export function useResourceMap(api, pathsByKey, { pollMs = 0, epoch = 0, outage = false, refresh = 0 } = {}) {
  const all = Object.entries(pathsByKey);
  const pairs = all.filter(([, p]) => p !== null);
  const key = JSON.stringify(pairs);
  const [result, setResult] = useState({ key, entries: {}, epoch: -1 });
  usePolled(key, async (current) => {
    const settled = await Promise.allSettled(pairs.map(([, p]) => api.get(p)));
    if (!current()) return;
    setResult((old) => {
      const entries = settleEntries(old.entries, pairs, settled, api.last);
      const failed = settled.some((s) => s.status === "rejected");
      return { key, entries, epoch: failed ? (old.key === key ? old.epoch : -1) : epoch };
    });
  }, pollMs, epoch, refresh);
  // Until the new set of paths answers, a key keeps its entry of the same path (a project added to the
  // list does not blank the others); a new path starts loading, from the cached answer if there is one.
  const items = {};
  for (const [k, p] of all) {
    const old = result.entries[k];
    const entry = p === null ? INVALID_ID : old?.path === p ? old : { path: p, ...LOADING, data: api.last(p) };
    items[k] = withOutage(entry, outage);
  }
  const same = result.key === key;
  const data = Object.fromEntries(Object.entries(items).map(([k, e]) => [k, e.data]));
  return { ...summarise(items, pairs.map(([k]) => k)), epoch: same ? result.epoch : -1, items, data };
}

// A status frame of this operation asks for a re-read of the screen's resources, at most once per
// THROTTLE_MS (spec §2.3); the terminal frame closes the throttle — the shell epoch makes the final snapshot.
export function useLiveRefresh(live, operationId) {
  const [count, setCount] = useState(0);
  const throttle = useRef(null);
  const make = () => { throttle.current = createThrottle(async () => setCount((n) => n + 1), THROTTLE_MS); return throttle.current; };
  useEffect(() => { const t = make(); return () => t.close(); }, [operationId]);
  const closedByTerminal = useRef(false);
  const payload = live && live.id === operationId ? live.payload : null;
  useEffect(() => {
    if (!payload) return;
    if (!ACTIVE.has(payload.control_state)) { throttle.current?.close(); closedByTerminal.current = true; return; }
    // A resumed campaign keeps its operation id: an active frame after a terminal one needs a fresh throttle.
    if (closedByTerminal.current) { closedByTerminal.current = false; make(); }
    throttle.current?.request();
  }, [payload]);
  return count;
}

// The strip's measurements (spec §2.3): one GET per measurement through one read queue per operation
// (lib/queue.js) — SAMPLE_CONCURRENCY at once whatever asks, each answer committed as it lands, an id in
// flight never read twice at once. A closed answer is kept for the screen; an open one (or one read while
// open) is read again (lib/samples.js readPlan). A longer list reads only what the plan selects; a live
// `refresh` reads the plan now; a new `epoch` (the final snapshot, a recovery) reads it after every read
// already started. A new operation (or unmount) closes the queue: its late answers are dropped.
export function useMeasurements(api, operationId, ids, { epoch = 0, refresh = 0, outage = false, isOpen = () => false } = {}) {
  const pairs = ids.map((m) => [m, paths.measurement(operationId, m)]);
  const valid = pairs.filter(([, p]) => p !== null);
  const key = JSON.stringify(valid);
  // The committed entries live in a ref (the plan reads them between renders); the state only repaints.
  const store = useRef({ operationId, entries: {} });
  if (store.current.operationId !== operationId) store.current = { operationId, entries: {} };
  const [, repaint] = useState(0);
  const latest = useRef(null);
  latest.current = { valid, isOpen };
  const plan = () => readPlan(latest.current.valid, store.current.entries, latest.current.isOpen)
    .map(([m, p]) => [m, p, latest.current.isOpen(m)]);
  const queue = useRef(null);
  useEffect(() => {
    const q = createReadQueue((p) => api.get(p), SAMPLE_CONCURRENCY, ([m, p, open], settled) => {
      if (store.current.operationId !== operationId) return;
      const fresh = settleEntries(store.current.entries, [[m, p]], [settled], api.last)[m];
      store.current = { operationId, entries: { ...store.current.entries, [m]: { ...fresh, readOpen: open } } };
      repaint((n) => n + 1);
    });
    queue.current = q;
    return () => { q.close(); if (queue.current === q) queue.current = null; };
  }, [api, operationId]);
  useEffect(() => { queue.current?.enqueue(plan()); }, [api, operationId, key]);
  const seen = useRef({ refresh, epoch });
  useEffect(() => {
    if (seen.current.refresh === refresh) return;
    seen.current.refresh = refresh;
    queue.current?.enqueue(plan());
  }, [refresh]);
  useEffect(() => {
    if (seen.current.epoch === epoch) return;
    seen.current.epoch = epoch;
    queue.current?.after(plan);
  }, [epoch]);
  const items = {};
  for (const [m, p] of pairs) {
    const old = store.current.entries[m];
    items[m] = p === null ? INVALID_ID : withOutage(old?.path === p ? old : { path: p, ...LOADING, data: api.last(p) }, outage);
  }
  return { items, read: summarise(items, valid.map(([m]) => m)) };
}

// useHealth(api) -> {panel, service, serviceEntry, error, check}: /api/health every HEALTH_POLL_MS and, while
// the panel says the service is ok, /v1/health too. `serviceEntry` is the /v1/health read (lib/resource.js):
// loading until it answers, an error with its code (keeping the last answer, stale) when it fails; while the
// service is down it is not read and keeps its last state (the shell marks it stale). `service` is its body
// or null. `check()` polls now and resolves to "service ok".
export function useHealth(api) {
  const [health, setHealth] = useState({ panel: null, serviceEntry: LOADING, error: null });
  const poll = useRef(null);
  useEffect(() => {
    let alive = true;
    poll.current = async () => {
      let panel;
      try {
        panel = await api.get(paths.health);
      } catch (e) {
        if (alive) setHealth((old) => ({ ...old, panel: null, error: errorCode(e) }));
        return false;
      }
      const ok = serviceState(panel).ok;
      let read = null;  // null: not read this time (the service is down)
      if (ok) read = await api.get(paths.serviceHealth).then(okEntry, (e) => ({ code: errorCode(e) }));
      if (alive) setHealth((old) => ({ panel, error: null, serviceEntry: read === null ? old.serviceEntry
        : read.state === "ok" ? read
        : errorEntry(read.code, old.serviceEntry.data ?? api.last(paths.serviceHealth)) }));
      return ok;
    };
    // Ticks skip while a poll is in flight (lib/poll.js); check() from the live stream may add one.
    const poller = createPoller(() => poll.current(), HEALTH_POLL_MS);
    return () => { alive = false; poller.close(); };
  }, [api]);
  return { ...health, service: health.serviceEntry.data ?? null,
           check: () => (poll.current ? poll.current() : Promise.resolve(false)) };
}
