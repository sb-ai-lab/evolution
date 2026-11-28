import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const { nowBlockOf } = await staticModule("lib/views/projects.js");
const ok = (data) => ({ state: "ok", data, stale: false });
const fail = (code, data) => ({ state: "error", code, data, stale: data !== undefined });
const loading = { state: "loading" };
const idle = ok({ active_operation_id: null });
const row = (id, at, over = {}) => ({ operation_state: "present", operation: {
  id, kind: "evaluation", control_state: "finished", resume: null, outcome: null,
  times_state: "present", times: { last_event_at: at }, ...over,
} });
const summary = (...operations) => ok({ operations });

test("health read determines loading, unavailable, idle and stale idle", () => {
  assert.equal(nowBlockOf(loading, null, {}).state, "loading");
  assert.deepEqual([nowBlockOf(fail("network_error"), null, {}).state, nowBlockOf(fail("network_error"), null, {}).code], ["unavailable", "network_error"]);
  assert.deepEqual([nowBlockOf(idle, null, {}).state, nowBlockOf(idle, null, {}).stale], ["idle", false]);
  const stale = nowBlockOf(fail("network_error", idle.data), null, {});
  assert.deepEqual([stale.state, stale.stale], ["idle", true]);
});

test("running reflects its independently read operation and last known health", () => {
  const h = ok({ active_operation_id: "abc123" });
  const op = ok({ id: "abc123", kind: "campaign", control_state: "running", progress: {
    kind: "campaign", phase: "evaluating", coverage: { scored: 2, expected: 5 },
  }, times_state: "present", times: { last_event_at: "2026-11-08T10:23:00Z" } });
  const m = nowBlockOf(h, op, {});
  assert.equal(m.state, "running"); assert.equal(m.running.id, "abc123");
  assert.equal(m.running.kind, "campaign"); assert.deepEqual(m.running.coverage, { scored: 2, expected: 5 });
  assert.equal(nowBlockOf(fail("network_error", h.data), op, {}).stale, true);
  assert.equal(nowBlockOf(h, fail("missing"), {}).running.read.state, "unavailable");
});

test("only eligible complete summary entries participate in latest closed selection", () => {
  const a = summary(row("older", "2026-11-08T10:00:00Z", { kind: "campaign", outcome: { outcome: "completed" } }),
    row("active", "2026-11-08T15:00:00Z", { control_state: "running" }),
    row("paused", "2026-11-08T16:00:00Z", { resume: { state: "ready" } }),
    row("notimes", "2026-11-08T17:00:00Z", { times_state: "unavailable" }),
    row("nokey", null), { operation_state: "other_pair", operation: { id: "other" } });
  const b = summary(row("newer", "2026-11-08T11:00:00Z"));
  const result = nowBlockOf(idle, null, { a, b });
  assert.equal(result.lastClosed.shortId, "newer");
  assert.equal(result.lastClosed.kind, "evaluation");
  assert.equal(result.lastClosed.label, "Finished");
  assert.equal(nowBlockOf(idle, null, {}).lastClosed, null);
  for (const entry of [loading, fail("network_error"), fail("network_error", b.data)])
    assert.equal(nowBlockOf(idle, null, { a, b: entry }).lastClosed, null);
});

test("outcome label is preferred to control state for selected campaign", () => {
  const result = nowBlockOf(idle, null, { a: summary(row("c", "2026-11-08T10:00:00Z", {
    kind: "campaign", outcome: { outcome: "completed" }, control_state: "finished",
  })) });
  assert.equal(result.lastClosed.label, "Completed");
});
