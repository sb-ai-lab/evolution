// lib/poll.js — one polled read (spec §2.3 polling): run now, then every intervalMs, but never two at once.
// A tick that finds the previous read still in flight is skipped, so a read slower than the interval
// still lands. `task(current)` gets `current()`: false once the poller is closed (a new path, a new
// epoch, unmount) — its answer then belongs to nobody and must be dropped.
// poke(): read again now — at once when idle, else exactly one more read right after the one in flight
// (any number of pokes during one read collapse into one); a closed poller ignores it.
const systemClock = { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) };

export function createPoller(task, intervalMs, clock = systemClock) {
  let closed = false, busy = false, pending = false, timer = null;
  const current = () => !closed;
  async function tick() {
    if (closed || busy) return;
    busy = true;
    try { await task(current); } catch { /* the task reports its own errors */ } finally { busy = false; }
    if (pending && !closed) { pending = false; tick(); }
  }
  function arm() {
    if (closed || !intervalMs) return;
    timer = clock.setTimeout(() => { timer = null; arm(); tick(); }, intervalMs);
  }
  tick();
  arm();
  return {
    poke() { if (closed) return; if (busy) pending = true; else tick(); },
    close() { closed = true; if (timer) clock.clearTimeout(timer); timer = null; },
  };
}
