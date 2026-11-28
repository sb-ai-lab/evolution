// lib/throttle.js — at most one refresh per interval, plus a final unthrottled one (spec §2.3).
// Errors: a failed throttled (request-driven) run is swallowed — nobody can receive it — and the chain stays
// usable. A second final() returns the first one's promise. final() waits for the run in flight (ignoring its failure) and then runs the task once more; it
// REJECTS with the task's error if that last run fails, so the caller knows the final snapshot is stale.
const systemClock = { now: () => Date.now(), setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) };

export function createThrottle(task, intervalMs, clock = systemClock) {
  let last = -Infinity, timer = null, inflight = Promise.resolve(), closed = false, finalRun = null;
  const run = () => {
    timer = null; last = clock.now();
    const result = inflight.then(() => task());
    inflight = result.catch(() => {});  // the chain itself never rejects
    return result;
  };
  const runQuietly = () => run().catch(() => {});
  return {
    request() {
      if (closed || timer) return;
      const wait = last + intervalMs - clock.now();
      if (wait <= 0) runQuietly(); else timer = clock.setTimeout(runQuietly, wait);
    },
    final() {
      if (finalRun) return finalRun;
      if (timer) { clock.clearTimeout(timer); timer = null; }
      closed = true;
      finalRun = (async () => { await inflight; await run(); })();
      return finalRun;
    },
    close() { closed = true; if (timer) clock.clearTimeout(timer); timer = null; },
  };
}
