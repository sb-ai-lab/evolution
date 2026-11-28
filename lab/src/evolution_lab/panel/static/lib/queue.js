// lib/queue.js — the sample strip's reads (spec 2026-09-30 §2.3 "параллельно не больше 4", final snapshot "после
// предыдущих запросов"): one queue per screen and operation, never replaced while it lives. Pure: `get` is
// injected, no DOM, no timers.
//
// - at most `limit` gets run at once, whatever asks for reads (a longer list, a refresh, an epoch);
// - each settled read is committed at once through onSettle(item, {status, value|reason}) — a batch never
//   holds finished answers back, and a new list does not discard them;
// - an id already queued or in flight is not queued again;
// - after(plan): once every read queued or in flight now has settled, enqueue plan() — the final snapshot
//   reads what the plan selects then, never overtaken by a read that started before it;
// - close(): nothing more starts, answers that land later are dropped, pending after() plans never run.
// Items are [id, path, ...extra]; onSettle gets the item back as enqueued.
export function createReadQueue(get, limit, onSettle) {
  let closed = false;
  const waiting = [];                 // items not started yet, in order
  const known = new Set();            // ids waiting or in flight
  let running = 0;
  let barriers = [];                  // [{left: Set of ids still outstanding, plan}]

  function release(id) {
    const ready = [];
    barriers = barriers.filter((b) => {
      b.left.delete(id);
      if (b.left.size === 0) { ready.push(b.plan); return false; }
      return true;
    });
    for (const plan of ready) enqueue(plan());
  }

  function start(item) {
    const path = item[1];
    running += 1;
    new Promise((resolve) => resolve(get(path))).then(
      (value) => settle(item, { status: "fulfilled", value }),
      (reason) => settle(item, { status: "rejected", reason }));
  }

  function settle(item, result) {
    running -= 1;
    known.delete(item[0]);
    if (closed) return;
    onSettle(item, result);
    release(item[0]);
    pump();
  }

  function pump() {
    while (!closed && running < limit && waiting.length) start(waiting.shift());
  }

  function enqueue(items) {
    if (closed) return;
    for (const item of items ?? []) {
      if (known.has(item[0])) continue;
      known.add(item[0]);
      waiting.push(item);
    }
    pump();
  }

  return {
    enqueue,
    after(plan) {
      if (closed) return;
      if (known.size === 0) { enqueue(plan()); return; }
      barriers.push({ left: new Set(known), plan });
    },
    close() { closed = true; waiting.length = 0; barriers = []; },
    get running() { return running; },
    get waiting() { return waiting.length; },
  };
}
