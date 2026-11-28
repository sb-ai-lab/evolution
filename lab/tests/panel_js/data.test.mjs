import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const { createApi, ApiError, paths } = await staticModule("lib/api.js");
const { createThrottle } = await staticModule("lib/throttle.js");
const { openLive, BACKOFF_MS } = await staticModule("lib/live.js");
const { createPoller } = await staticModule("lib/poll.js");

// Throttled tasks start on a promise chain: let queued microtasks and immediates run.
const flush = () => new Promise((resolve) => setImmediate(resolve));

function fakeClock() {
  let now = 0; const timers = [];
  return {
    now: () => now,
    setTimeout: (fn, ms) => { const t = { at: now + ms, fn }; timers.push(t); return t; },
    clearTimeout: (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); },
    async advance(ms) {
      now += ms;
      for (const t of timers.filter((x) => x.at <= now).sort((a, b) => a.at - b.at)) {
        timers.splice(timers.indexOf(t), 1); await t.fn();
      }
    },
    pending: () => timers.length,
  };
}

test("api errors carry the service code or http status", async () => {
  const api = createApi({ fetch: async (p) => (p === "/bad"
    ? { ok: false, status: 503, json: async () => ({ error: { code: "service_unavailable" } }) }
    : p === "/html" ? { ok: false, status: 502, json: async () => { throw new SyntaxError("x"); } }
    : { ok: true, status: 200, json: async () => ({ v: 1 }) }) });
  await assert.rejects(api.get("/bad"), (e) => e instanceof ApiError && e.code === "service_unavailable");
  await assert.rejects(api.get("/html"), (e) => e.code === "http_502");
  assert.deepEqual(await api.get("/v1/x"), { v: 1 });
  assert.deepEqual(api.last("/v1/x"), { v: 1 });
  api.forget("/v1/");
  assert.equal(api.last("/v1/x"), undefined);
});

test("paths never encode ids and refuse ids outside the segment grammar", () => {
  assert.equal(paths.events("op:1"), "/v1/operations/op:1/events");
  assert.equal(paths.measurement("o", "m-r01@e1"), "/v1/operations/o/measurements/m-r01@e1");
  assert.equal(paths.operation("a/b"), null);
  assert.equal(paths.projectOperations("demo"), "/v1/projects/demo/operations?view=summary");
});

test("content paths encode core ids once and refuse others", async () => {
  const { paths } = await staticModule("lib/api.js");
  assert.equal(paths.sample("o", "m-r01@e1", "кейс+1@a", "00"),
               "/v1/operations/o/measurements/m-r01@e1/cases/%D0%BA%D0%B5%D0%B9%D1%81%2B1%40a/samples/00");
  assert.equal(paths.sample("o", "m", "c", "00", true), "/v1/operations/o/measurements/m/cases/c/samples/00?disclose=private");
  assert.equal(paths.sample("o", "m", "a/b", "00"), null);
  assert.equal(paths.sample("o", "m", "..", "00"), null);
  assert.equal(paths.attempt("o", "r01-a02"), "/v1/operations/o/attempts/r01-a02");
  assert.equal(paths.attempt("o", "r01-x01"), null);
  assert.equal(paths.reflection("o", "r01-x01", true), "/v1/operations/o/reflections/r01-x01?disclose=private");
});

test("throttle merges requests and final runs once more after the in-flight one", async () => {
  const clock = fakeClock(); const runs = [];
  const t = createThrottle(async () => { runs.push(clock.now()); }, 3000, clock);
  t.request(); t.request(); t.request();
  await flush();
  assert.deepEqual(runs, [0]);
  await clock.advance(3000);
  assert.deepEqual(runs, [0, 3000]);
  t.request();
  await t.final();
  assert.deepEqual(runs, [0, 3000, 3000]);
  t.request(); await clock.advance(10000); await flush();
  assert.equal(runs.length, 3);
});

class FakeSource {
  static all = [];
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; FakeSource.all.push(this); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
  emit(name, payload) { this.listeners[name]?.({ data: JSON.stringify({ payload }) }); }
}

test("live: terminal frame closes and reports; error with a live service reconnects with backoff", async () => {
  FakeSource.all = []; const clock = fakeClock(); const seen = [];
  let healthy = true;
  openLive({ operationId: "op:1", EventSource: FakeSource, clock, checkHealth: async () => healthy,
    onStatus: (p) => seen.push(["status", p.control_state]), onTerminal: (p) => seen.push(["terminal", p.control_state]),
    onReconnected: () => seen.push(["reconnected"]), onServiceLost: () => seen.push(["lost"]) });
  const first = FakeSource.all[0];
  assert.equal(first.url, "/v1/operations/op:1/events");
  first.emit("status", { control_state: "running" });
  await first.listeners.error({});
  assert.ok(first.closed);
  await clock.advance(BACKOFF_MS[0]);
  const second = FakeSource.all[1];
  second.emit("status", { control_state: "running" });
  second.emit("status", { control_state: "finished" });
  assert.ok(second.closed);
  // After a reconnect the screen re-reads its resources before the header merges the first frame.
  // A terminal frame is still a status frame: the header merges it, then the terminal snapshot follows.
  assert.deepEqual(seen, [["status", "running"], ["reconnected"], ["status", "running"],
    ["status", "finished"], ["terminal", "finished"]]);
});

test("live: error with the service down reports lost and does not reopen", async () => {
  FakeSource.all = []; const clock = fakeClock(); const seen = [];
  openLive({ operationId: "o", EventSource: FakeSource, clock, checkHealth: async () => false,
    onStatus: () => {}, onTerminal: () => {}, onReconnected: () => {}, onServiceLost: () => seen.push("lost") });
  await FakeSource.all[0].listeners.error({});
  await clock.advance(60000);
  assert.deepEqual(seen, ["lost"]);
  assert.equal(FakeSource.all.length, 1);
});

test("live: consecutive failures walk the backoff up to its ceiling", async () => {
  FakeSource.all = []; const clock = fakeClock();
  openLive({ operationId: "o", EventSource: FakeSource, clock, checkHealth: async () => true,
    onStatus: () => {}, onTerminal: () => {}, onReconnected: () => {}, onServiceLost: () => {} });
  for (const wait of [1000, 2000, 5000, 10000, 10000]) {
    await FakeSource.all.at(-1).listeners.error({});
    const before = FakeSource.all.length;
    await clock.advance(wait - 1); assert.equal(FakeSource.all.length, before);
    await clock.advance(1); assert.equal(FakeSource.all.length, before + 1);
  }
});

test("live: close() while the health check is pending does not reopen or report", async () => {
  FakeSource.all = []; const clock = fakeClock(); const seen = [];
  let release; const gate = new Promise((resolve) => { release = resolve; });
  const live = openLive({ operationId: "o", EventSource: FakeSource, clock, checkHealth: () => gate,
    onStatus: () => {}, onTerminal: () => {}, onReconnected: () => {}, onServiceLost: () => seen.push("lost") });
  const pending = FakeSource.all[0].listeners.error({});
  live.close(); release(true); await pending;
  assert.equal(clock.pending(), 0);
  await clock.advance(60000);
  assert.deepEqual(seen, []);
  assert.equal(FakeSource.all.length, 1);
});

test("live: a failing health check counts as service lost", async () => {
  FakeSource.all = []; const clock = fakeClock(); const seen = [];
  openLive({ operationId: "o", EventSource: FakeSource, clock, checkHealth: async () => { throw new Error("net"); },
    onStatus: () => {}, onTerminal: () => {}, onReconnected: () => {}, onServiceLost: () => seen.push("lost") });
  await FakeSource.all[0].listeners.error({});
  assert.deepEqual(seen, ["lost"]);
});

test("live: an id outside the segment grammar opens nothing", () => {
  FakeSource.all = [];
  const live = openLive({ operationId: "a/b", EventSource: FakeSource, clock: fakeClock(), checkHealth: async () => true,
    onStatus: () => {}, onTerminal: () => {}, onReconnected: () => {}, onServiceLost: () => {} });
  assert.equal(FakeSource.all.length, 0);
  live.close();
});

test("throttle: final() rejects with the task's error when its own run fails", async () => {
  const clock = fakeClock(); let fail = false;
  const t = createThrottle(async () => { if (fail) throw new Error("stale"); }, 3000, clock);
  t.request(); await flush();
  fail = true;
  await assert.rejects(t.final(), (e) => e.message === "stale");
});

test("throttle: a failing timer-driven run is swallowed and does not break later runs", async () => {
  const clock = fakeClock(); const runs = []; let fail = true;
  const t = createThrottle(async () => { runs.push(clock.now()); if (fail) throw new Error("boom"); }, 3000, clock);
  t.request(); await flush();          // immediate run, fails, swallowed
  t.request();                         // queued for 3000
  await clock.advance(3000);           // timer-driven run, fails, swallowed
  assert.deepEqual(runs, [0, 3000]);
  fail = false;
  t.request();
  await clock.advance(3000);
  assert.deepEqual(runs, [0, 3000, 6000]);
  await t.final();                     // resolves: preceding failures are ignored, its own run succeeds
  assert.deepEqual(runs, [0, 3000, 6000, 6000]);
});

test("poller: a read slower than the interval is not overtaken, and its answer lands", async () => {
  const clock = fakeClock();
  const reads = [], answers = [];
  const task = (current) => new Promise((resolve) => reads.push(() => { if (current()) answers.push(reads.length); resolve(); }));
  const poller = createPoller(task, 10000, clock);
  assert.equal(reads.length, 1);                 // runs at once
  await clock.advance(10000); await clock.advance(10000);
  assert.equal(reads.length, 1);                 // two ticks skipped while the first read is in flight
  reads[0](); await flush();
  assert.deepEqual(answers, [1]);                // the slow answer is kept
  await clock.advance(10000);
  assert.equal(reads.length, 2);                 // polling resumes
  poller.close();
  reads[1](); await flush();
  assert.deepEqual(answers, [1]);                // after close the answer belongs to nobody
  await clock.advance(30000);
  assert.equal(reads.length, 2);
  assert.equal(clock.pending(), 0);
});

test("poller: interval 0 reads once; a failing task does not stop the polling", async () => {
  const clock = fakeClock();
  let once = 0;
  createPoller(async () => { once += 1; }, 0, clock);
  assert.equal(once, 1); assert.equal(clock.pending(), 0);
  let calls = 0;
  const poller = createPoller(async () => { calls += 1; throw new Error("x"); }, 5000, clock);
  await flush(); await clock.advance(5000); await flush();
  assert.equal(calls, 2);
  poller.close();
});

test("throttle: a second final() does not run the task again", async () => {
  let runs = 0;
  const t = createThrottle(async () => { runs += 1; }, 3000, fakeClock());
  await Promise.all([t.final(), t.final()]);
  assert.equal(runs, 1);
});

test("live: a malformed frame is skipped, the stream stays open", () => {
  FakeSource.all = []; const seen = [];
  openLive({ operationId: "o", EventSource: FakeSource, clock: fakeClock(), checkHealth: async () => true,
    onStatus: (p) => seen.push(["status", p.control_state]), onTerminal: () => seen.push(["terminal"]),
    onReconnected: () => {}, onServiceLost: () => {} });
  const source = FakeSource.all[0];
  source.listeners.status({ data: "{not json" });
  source.listeners.status({ data: JSON.stringify({}) });  // no payload: skipped too
  source.emit("status", { control_state: "running" });
  assert.deepEqual(seen, [["status", "running"]]);
  assert.equal(source.closed, false);
});

test("live: an operation that is gone stops the reconnects", async () => {
  FakeSource.all = []; const clock = fakeClock(); const seen = [];
  openLive({ operationId: "o", EventSource: FakeSource, clock, checkHealth: async () => true, checkOperation: async () => true,
    onStatus: () => {}, onTerminal: (p) => seen.push(["terminal", p]), onReconnected: () => {}, onServiceLost: () => {} });
  for (let n = 0; n < 3; n += 1) { await FakeSource.all.at(-1).listeners.error({}); await clock.advance(10000); }
  assert.deepEqual(seen, [["terminal", null]]);
  assert.equal(FakeSource.all.length, 3);  // no fourth EventSource
  assert.equal(clock.pending(), 0);
});

test("live: a frame resets the silent-failure count; a still-active operation keeps reconnecting", async () => {
  FakeSource.all = []; const clock = fakeClock(); const seen = []; let asked = 0;
  openLive({ operationId: "o", EventSource: FakeSource, clock, checkHealth: async () => true,
    checkOperation: async () => { asked += 1; return false; },
    onStatus: () => {}, onTerminal: () => seen.push("terminal"), onReconnected: () => {}, onServiceLost: () => {} });
  for (let n = 0; n < 2; n += 1) { await FakeSource.all.at(-1).listeners.error({}); await clock.advance(10000); }
  FakeSource.all.at(-1).emit("status", { control_state: "running" });
  for (let n = 0; n < 2; n += 1) { await FakeSource.all.at(-1).listeners.error({}); await clock.advance(10000); }
  assert.equal(asked, 0);  // never three in a row
  await FakeSource.all.at(-1).listeners.error({}); await clock.advance(10000);
  assert.equal(asked, 1);
  assert.equal(FakeSource.all.length, 6);  // not gone: reopened
  assert.deepEqual(seen, []);
});

test("poller poke: immediate tick when idle; N pokes during one read give exactly one more; none after close", async () => {
  const clock = fakeClock(); const reads = [];
  const task = () => new Promise((resolve) => reads.push(resolve));
  const poller = createPoller(task, 0, clock);
  reads[0](); await flush();
  poller.poke(); assert.equal(reads.length, 2);      // idle: at once
  poller.poke(); poller.poke(); poller.poke();       // in flight: collapse into one
  assert.equal(reads.length, 2);
  reads[1](); await flush();
  assert.equal(reads.length, 3);                     // exactly one extra tick
  reads[2](); await flush();
  assert.equal(reads.length, 3);
  poller.poke(); poller.poke();
  poller.close(); reads[3]?.(); await flush();
  poller.poke();
  assert.equal(reads.length, 4);                     // the poke before close ran; the one in flight left no extra; none after close
});

test("getText returns a body of the expected type, refuses another and reads the error envelope", async () => {
  const seen = [];
  const answer = (status, type, body) => ({
    ok: status < 300, status, headers: { get: (name) => (name.toLowerCase() === "content-type" ? type : null) },
    text: async () => body, json: async () => JSON.parse(body),
  });
  const api = createApi({ fetch: async (path, init) => {
    seen.push([path, init.headers.accept]);
    return path === "/md" ? answer(200, "text/markdown; charset=utf-8", "# Кампания\n")
      : path === "/json" ? answer(200, "application/json", "{}")
      : answer(409, "application/json", JSON.stringify({ error: { code: "report_unavailable" } }));
  } });
  assert.equal(await api.getText("/md", "text/markdown"), "# Кампания\n");
  assert.deepEqual(seen[0], ["/md", "text/markdown"]);
  assert.equal(api.last("/md"), undefined);
  await assert.rejects(api.getText("/json", "text/markdown"), (e) => e instanceof ApiError && e.code === "unexpected_content_type");
  await assert.rejects(api.getText("/err", "text/markdown"), (e) => e.code === "report_unavailable" && e.status === 409);
  assert.equal(paths.reportMarkdown("op:1"), "/v1/operations/op:1/report.md");
  assert.equal(paths.reportMarkdown("a/b"), null);
});
