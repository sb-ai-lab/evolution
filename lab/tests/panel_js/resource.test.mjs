import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const r = await staticModule("lib/resource.js");
const { ApiError } = await staticModule("lib/api.js");

const ok = (value) => ({ status: "fulfilled", value });
const no = (reason) => ({ status: "rejected", reason });

test("readOf: loading, unavailable with the code, ok, stale; a missing entry is loading, not empty", () => {
  assert.deepEqual(r.readOf(undefined), { state: "loading", code: null, stale: false });
  assert.deepEqual(r.readOf(r.LOADING), { state: "loading", code: null, stale: false });
  assert.deepEqual(r.readOf({ state: "error", data: undefined, code: "not_found", stale: false }),
                   { state: "unavailable", code: "not_found", stale: false });
  assert.deepEqual(r.readOf(r.okEntry({ operations: [] })), { state: "ok", code: null, stale: false });
  assert.deepEqual(r.readOf({ state: "error", data: { x: 1 }, code: "network_error", stale: true }),
                   { state: "ok", code: null, stale: true });
  // A cached answer shown while the new path loads is data, not a loading note.
  assert.deepEqual(r.readOf({ ...r.LOADING, data: { x: 1 } }), { state: "ok", code: null, stale: false });
});

test("settleEntries: per-key isolation — one failed path neither blanks nor taints another", () => {
  const pairs = [["a", "/pa"], ["b", "/pb"], ["c", "/pc"]];
  const first = r.settleEntries({}, pairs, [ok({ n: 1 }), no(new ApiError("ambiguous_implementation", 409)), no(new TypeError("x"))]);
  assert.deepEqual(first.a, { path: "/pa", state: "ok", data: { n: 1 }, code: null, stale: false });
  assert.deepEqual(first.b, { path: "/pb", state: "error", data: undefined, code: "ambiguous_implementation", stale: false });
  assert.deepEqual(first.c, { path: "/pc", state: "error", data: undefined, code: "network_error", stale: false });
  // The next read: a fails and keeps its own last answer, stale; b answers; c's cached answer (api.last) is used.
  const next = r.settleEntries(first, pairs, [no(new ApiError("service_unavailable", 503)), ok({ n: 2 }), no(new SyntaxError("x"))],
                               (path) => (path === "/pc" ? { cached: true } : undefined));
  assert.deepEqual(next.a, { path: "/pa", state: "error", data: { n: 1 }, code: "service_unavailable", stale: true });
  assert.deepEqual(next.b, { path: "/pb", state: "ok", data: { n: 2 }, code: null, stale: false });
  assert.deepEqual(next.c, { path: "/pc", state: "error", data: { cached: true }, code: "invalid_json", stale: true });
});

test("settleEntries: a key whose path changed does not keep the other path's answer", () => {
  const before = r.settleEntries({}, [["a", "/old"]], [ok({ n: 1 })]);
  const after = r.settleEntries(before, [["a", "/new"]], [no(new ApiError("not_found", 404))]);
  assert.deepEqual(after.a, { path: "/new", state: "error", data: undefined, code: "not_found", stale: false });
});

test("summarise: first error code, stale if any, loading until every key answered", () => {
  const entries = r.settleEntries({}, [["a", "/pa"], ["b", "/pb"]], [ok(1), no(new ApiError("not_found", 404))]);
  assert.deepEqual(r.summarise(entries, ["a", "b"]), { state: "error", code: "not_found", stale: false });
  assert.deepEqual(r.summarise(entries, ["a", "x"]), { state: "loading", code: null, stale: false });
  assert.deepEqual(r.summarise(entries, ["a"]), { state: "ok", code: null, stale: false });
  assert.deepEqual(r.summarise({}, []), { state: "ok", code: null, stale: false });
});

test("withOutage: an answer read before an observed outage is stale; nothing to mark without one", () => {
  const read = r.okEntry({ id: "o1" });
  assert.equal(r.withOutage(read, false), read);
  assert.deepEqual(r.withOutage(read, true), { ...read, stale: true });
  assert.equal(r.withOutage(r.LOADING, true), r.LOADING);
  const failed = { state: "error", data: undefined, code: "network_error", stale: false };
  assert.equal(r.withOutage(failed, true), failed);
});

test("worstRead: unavailable before loading; ok (stale if any) when all answered", () => {
  const OK = { state: "ok", code: null, stale: false };
  const unavailable = { state: "unavailable", code: "not_found", stale: false };
  const loading = { state: "loading", code: null, stale: false };
  assert.deepEqual(r.worstRead([OK, loading, unavailable]), unavailable);
  assert.deepEqual(r.worstRead([OK, loading]), loading);
  assert.deepEqual(r.worstRead([OK, { ...OK, stale: true }]), { ...OK, stale: true });
  assert.deepEqual(r.worstRead([]), OK);
});

test("errorCode: Lab's code, invalid_json, network_error", () => {
  assert.equal(r.errorCode(new ApiError("not_found", 404)), "not_found");
  assert.equal(r.errorCode(new SyntaxError("x")), "invalid_json");
  assert.equal(r.errorCode(new TypeError("fetch failed")), "network_error");
});

test("API forget evicts present values and suppresses matching in-flight cache writes only", async () => {
  const { createApi } = await staticModule("lib/api.js");
  const waiting = [];
  const api = createApi({ fetch: (path) => new Promise((resolve) => waiting.push({ path,
    answer: (value) => resolve({ ok: true, json: async () => value }) })) });
  const deleted = api.get("/op/suspensions"); const other = api.get("/other");
  api.forget("/op/");
  waiting[0].answer({ private: "old" }); waiting[1].answer({ safe: true });
  await Promise.all([deleted, other]);
  assert.equal(api.last("/op/suspensions"), undefined);
  assert.deepEqual(api.last("/other"), { safe: true });
  const again = api.get("/op/suspensions"); waiting[2].answer({ fresh: true }); await again;
  assert.deepEqual(api.last("/op/suspensions"), { fresh: true });
  const all = api.get("/late"); api.forget(); waiting[3].answer({ stale: true }); await all;
  assert.equal(api.last("/late"), undefined); assert.equal(api.last("/other"), undefined);
});
