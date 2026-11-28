// lib/live.js — one live stream per page (spec §2.3; 1b backlog: reconnect, terminal outcome).
import { ACTIVE, paths } from "./api.js";

export const BACKOFF_MS = [1000, 2000, 5000, 10000];
export const GONE_AFTER = 3;  // failures in a row without a frame, service alive: ask whether the operation is gone
const systemClock = { now: () => Date.now(), setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) };

export function openLive({ operationId, EventSource, checkHealth, onStatus, onTerminal, onReconnected, onServiceLost,
                           checkOperation, clock = systemClock }) {
  const url = paths.events(operationId);
  let source = null, timer = null, failures = 0, silent = 0, closed = url === null, reopened = false;

  function open() {
    if (closed) return;
    const current = source = new EventSource(url);
    current.addEventListener("status", (event) => {
      let payload;
      try { payload = JSON.parse(event.data).payload; } catch { return; }  // a malformed frame is skipped
      if (!payload) return;
      failures = 0; silent = 0;
      if (reopened) { reopened = false; onReconnected(); }
      onStatus(payload);
      if (!ACTIVE.has(payload.control_state)) { stop(); onTerminal(payload); }
    });
    current.addEventListener("error", async () => {
      current.close();
      if (closed) return;
      // Health unknown counts as lost; close() may also land while we wait for it.
      const healthy = await Promise.resolve().then(checkHealth).catch(() => false);
      if (closed) return;
      if (!healthy) { stop(); onServiceLost(); return; }
      silent += 1;
      // A stream that keeps failing while the service is fine: the operation may be gone (404) or over.
      if (silent >= GONE_AFTER && checkOperation) {
        const gone = await Promise.resolve().then(checkOperation).catch(() => false);
        if (closed) return;
        if (gone) { stop(); onTerminal(null); return; }
      }
      const wait = BACKOFF_MS[Math.min(failures, BACKOFF_MS.length - 1)];
      failures += 1; reopened = true;
      timer = clock.setTimeout(() => { timer = null; open(); }, wait);
    });
  }
  function stop() {
    closed = true;
    if (timer) clock.clearTimeout(timer);
    timer = null;
    source?.close();
  }
  open();
  return { close: stop };
}
