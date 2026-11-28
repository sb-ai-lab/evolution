// lib/queue.js — the strip's read queue (spec 2026-09-30 §2.3; codex r1 panel-refresh-batch-overlap).
import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const { createReadQueue } = await staticModule("lib/queue.js");
const { SAMPLE_CONCURRENCY } = await staticModule("lib/samples.js");

// A fake get whose answers the test releases one by one; `peak` is the most gets ever pending at once.
function fakeGet() {
  const pending = new Map(), calls = [];
  let open = 0, peak = 0;
  const get = (path) => new Promise((resolve, reject) => {
    open += 1; peak = Math.max(peak, open); calls.push(path);
    pending.set(path, { resolve: (v) => { open -= 1; pending.delete(path); resolve(v); },
                        reject: (e) => { open -= 1; pending.delete(path); reject(e); } });
  });
  return { get, calls, pending, answer: (path, v = { path }) => pending.get(path).resolve(v),
           fail: (path, e = { code: "x" }) => pending.get(path).reject(e), get peak() { return peak; }, get open() { return open; } };
}
const flush = () => new Promise((r) => setTimeout(r, 0));
const items = (ids) => ids.map((id) => [id, `/m/${id}`]);

test("never more than the limit in flight, even when the list grows while reads are pending", async () => {
  const g = fakeGet(), settled = [];
  const q = createReadQueue(g.get, SAMPLE_CONCURRENCY, (item) => settled.push(item[0]));
  q.enqueue(items(["a", "b", "c", "d", "e", "f", "g", "h"]));
  assert.equal(g.open, SAMPLE_CONCURRENCY);
  q.enqueue(items(["a", "b", "c", "d", "e", "f", "g", "h", "i"]));   // a ninth measurement arrives
  assert.equal(g.open, SAMPLE_CONCURRENCY);
  while (g.pending.size) { g.answer([...g.pending.keys()][0]); await flush(); }
  assert.equal(g.peak, SAMPLE_CONCURRENCY);
  assert.deepEqual(g.calls.sort(), items(["a", "b", "c", "d", "e", "f", "g", "h", "i"]).map(([, p]) => p));
  assert.equal(settled.length, 9);
});

test("each read is committed as it lands, not after its batch", async () => {
  const g = fakeGet(), settled = [];
  const q = createReadQueue(g.get, SAMPLE_CONCURRENCY, (item, s) => settled.push([item[0], s.status, item[2]]));
  q.enqueue([["a", "/m/a", true], ["b", "/m/b", false]]);
  g.answer("/m/b"); await flush();
  assert.deepEqual(settled, [["b", "fulfilled", false]]);    // a is still pending
  g.fail("/m/a"); await flush();
  assert.deepEqual(settled, [["b", "fulfilled", false], ["a", "rejected", true]]);
});

test("an id queued or in flight is not read twice at once", async () => {
  const g = fakeGet();
  const q = createReadQueue(g.get, 1, () => {});
  q.enqueue(items(["a", "b"]));
  q.enqueue(items(["a", "b"]));
  assert.deepEqual(g.calls, ["/m/a"]);
  assert.equal(q.waiting, 1);
  g.answer("/m/a"); await flush();
  g.answer("/m/b"); await flush();
  assert.deepEqual(g.calls, ["/m/a", "/m/b"]);
  q.enqueue(items(["a"]));                                     // settled: may be read again
  assert.deepEqual(g.calls, ["/m/a", "/m/b", "/m/a"]);
});

test("after(): the final plan waits for every read already started, then reads what the plan selects then", async () => {
  const g = fakeGet();
  let final = null;
  const q = createReadQueue(g.get, SAMPLE_CONCURRENCY, () => {});
  q.enqueue(items(["a", "b"]));
  q.after(() => { final = "planned"; return items(["a", "c"]); });
  assert.equal(final, null);
  g.answer("/m/a"); await flush();
  assert.equal(final, null);                                   // b is still in flight
  assert.deepEqual(g.calls, ["/m/a", "/m/b"]);
  g.answer("/m/b"); await flush();
  assert.equal(final, "planned");
  assert.deepEqual(g.calls, ["/m/a", "/m/b", "/m/a", "/m/c"]);  // a is read again after its earlier read
  // Idle: the plan runs at once.
  g.answer("/m/a"); g.answer("/m/c"); await flush();
  q.after(() => items(["d"]));
  assert.deepEqual(g.calls.at(-1), "/m/d");
});

test("close(): nothing more starts, late answers and pending plans are dropped", async () => {
  const g = fakeGet(), settled = [];
  let planned = false;
  const q = createReadQueue(g.get, 1, (item) => settled.push(item[0]));
  q.enqueue(items(["a", "b"]));
  q.after(() => { planned = true; return items(["c"]); });
  q.close();
  g.answer("/m/a"); await flush();
  assert.deepEqual(settled, []);
  assert.deepEqual(g.calls, ["/m/a"]);
  assert.equal(planned, false);
  q.enqueue(items(["d"]));
  q.after(() => items(["e"]));
  assert.deepEqual(g.calls, ["/m/a"]);
});

test("a get that throws synchronously is a rejected read, not an exception", async () => {
  const settled = [];
  const q = createReadQueue(() => { throw new TypeError("boom"); }, 2, (item, s) => settled.push([item[0], s.status]));
  q.enqueue(items(["a"]));
  await flush();
  assert.deepEqual(settled, [["a", "rejected"]]);
  assert.equal(q.running, 0);
});
