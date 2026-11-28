import { test } from "node:test";
import assert from "node:assert/strict";
import { body, snapshot, staticModule } from "./support.mjs";

const S = await staticModule("lib/samples.js");
const ids = (name) => snapshot(name).meta.ids;
const op = (name, id) => body(name, `/v1/operations/${id}`);
const view = (name, id) => body(name, `/v1/operations/${id}/campaign`);
const active = (operation, progress) => ({ ...operation, control_state: "running", ...(progress ? { progress } : {}) });

test("campaign: an in_progress attempt's measurement is open only while the operation is active", () => {
  const id = ids("details").campaign_mixed;
  const campaign = structuredClone(view("details", id));
  const attempt = campaign.rounds[0].attempts[0];
  attempt.group = "in_progress";
  const m = attempt.measurement_id ?? campaign.measurements.at(-1).measurement_id;  // ids from the fixture, never literals
  attempt.measurement_id = m;
  assert.equal(S.measurementOpen(active(op("details", id)), campaign, m), true);
  assert.equal(S.measurementOpen(op("details", id), campaign, m), false);  // finished
});

test("campaign: the current round's baseline is open only in the baseline phase", () => {
  const id = ids("details").campaign_mixed;
  const campaign = view("details", id);
  const base = campaign.rounds[0].baseline_measurement_id;
  const progress = (phase) => ({ ...op("details", id).progress, phase, rounds: { completed: 0, current: 1, planned: 2 } });
  assert.equal(S.measurementOpen(active(op("details", id), progress("baseline")), campaign, base), true);
  assert.equal(S.measurementOpen(active(op("details", id), progress("generation")), campaign, base), false);
});

test("evaluation: its measurement is open while the operation is active", () => {
  const id = ids("details").evaluation;
  assert.equal(S.measurementOpen(active(op("details", id)), undefined, `c-${id}`), true);
  assert.equal(S.measurementOpen(op("details", id), undefined, `c-${id}`), false);
});

test("cells: pending only when open; no score is not zero; values outside [0, 1] are neutral", () => {
  const unfinished = { case_id: "q", sample_id: "1", execution_state: "running", scoring_state: "pending", metrics: {} };
  assert.equal(S.cellOf(unfinished, "score", true).kind, "pending");
  assert.equal(S.cellOf(unfinished, "score", false).kind, "none");
  const done = (v) => ({ case_id: "q", sample_id: "1", execution_state: "finished", scoring_state: "finished", metrics: { score: v } });
  assert.deepEqual([1, 0.5, 0, 3].map((v) => S.cellOf(done(v), "score", false).kind), ["pass", "partial", "fail", "value"]);
  assert.equal(S.cellOf(done(0.4), null, false).kind, "unranked");
  assert.equal(S.cellOf(undefined, "score", false).kind, "absent");
});

test("rows keep the first (baseline) measurement's case order", () => {
  const id = ids("details").campaign_mixed;
  const campaign = view("details", id);
  const bodies = campaign.measurements.map((m) => body("details", `/v1/operations/${id}/measurements/${m.measurement_id}`));
  const order = S.sampleOrder(bodies);
  assert.deepEqual(order.slice(0, bodies[0].measurement.samples.length), bodies[0].measurement.samples.map(S.sampleKey));
  const row = S.stripRow(bodies[0], order, "score", false);
  assert.equal(row.cells.length, order.length);
  assert.equal(row.total, bodies[0].measurement.samples.length);
});

test("read plan: unread, failed, open, and read-while-open measurements are read; closed answers are kept", () => {
  const pairs = [["a", "/a"], ["b", "/b"], ["c", "/c"], ["d", "/d"], ["e", "/e"]];
  const entries = { a: { state: "ok", data: {}, readOpen: false }, b: { state: "error", code: "x" },
                    c: { state: "ok", data: {}, readOpen: false }, d: { state: "ok", data: {}, readOpen: true } };
  const plan = S.readPlan(pairs, entries, (id) => id === "c");
  assert.deepEqual(plan.map(([id]) => id), ["b", "c", "d", "e"]);
});

test("primary metric: campaign policy; evaluation only with a single metric", () => {
  const id = ids("details").evaluation;
  const m = body("details", `/v1/operations/${id}/measurements/c-${id}`);
  assert.equal(S.primaryMetric("evaluation", undefined, m), Object.keys(m.measurement.metrics)[0]);
  const two = structuredClone(m); two.measurement.metrics.other = { mean: 1, count: 1 };
  assert.equal(S.primaryMetric("evaluation", undefined, two), null);
  const cid = ids("details").campaign_mixed;
  assert.equal(S.primaryMetric("campaign", view("details", cid), undefined), view("details", cid).policy.metric_primary);
});

// Codex r1 minor panel-baseline-scored-count: the baseline row counts its own partners, not the candidate's scoring.
test("baseline row: scored count from the served baseline partners", () => {
  const sample = (i, scoring, baseline) => ({ case_id: "q", sample_id: String(i), execution_state: "finished",
    scoring_state: scoring, metrics: {}, baseline });
  const pending = { measurement: { samples: [0, 1, 2].map((i) => sample(i, "pending", { metrics: { score: 1 } })) } };
  const order = S.sampleOrder([pending]);
  assert.deepEqual([S.baselineRow(pending, order, "score").scored, S.stripRow(pending, order, "score", false).scored], [3, 0]);
  const unpaired = { measurement: { samples: [0, 1].map((i) => sample(i, "finished", null)) } };
  assert.deepEqual([S.baselineRow(unpaired, S.sampleOrder([unpaired]), "score").scored,
                    S.stripRow(unpaired, S.sampleOrder([unpaired]), "score", false).scored], [0, 2]);
});

// Final review item 10: with onSelect the cells are buttons and a click picks that index; without it, plain cells.
test("SampleStrip: onSelect turns cells into buttons that report their index", async () => {
  const { SampleStrip } = await staticModule("ui/samples.js");
  const { inspect } = await import("./support.mjs");
  const cells = ["pass", "fail", "none"].map((kind, i) => ({ kind, title: `case ${i}` }));
  const rows = [{ id: "m1", href: null, title: "m1", label: "m1", read: { state: "ok", stale: false }, cells, stat: "2/3 scored" }];
  const picked = [];
  const pick = inspect(SampleStrip({ rows, onSelect: (i) => picked.push(i), selected: 1 }));
  const buttons = pick.elements.filter((n) => n.type === "button");
  assert.equal(buttons.length, 3);
  assert.deepEqual(buttons.map((b) => b.props["aria-current"]), [undefined, "true", undefined]);
  buttons[2].props.onClick();
  buttons[0].props.onClick();
  assert.deepEqual(picked, [2, 0]);
  const plain = inspect(SampleStrip({ rows, legend: false }));
  assert.equal(plain.elements.some((n) => n.type === "button"), false);
  assert.equal(plain.elements.filter((n) => n.type === "i" && String(n.props.class).startsWith("cell cell-")).length, 3);
});
