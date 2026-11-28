import { test } from "node:test";
import assert from "node:assert/strict";
import { body, snapshot, staticModule } from "./support.mjs";

const { confidenceOf, intervalOf, intervalText, scaleOf, caveatOf } = await staticModule("lib/views/confidence.js");
const { label } = await staticModule("lib/labels.js");
const ids = snapshot("family14").meta.ids;
const view = (key) => body("family14", `/v1/operations/${ids[key]}/campaign`);
const attempts = (v) => v.rounds.flatMap((r) => r.attempts);

test("null block: no confidence model before core binds a comparison", () => {
  assert.equal(confidenceOf(null), null);
});

test("latest null: pending, never an empty interval", () => {
  const a = attempts(view("campaign_final")).find((x) => x.confidence);
  const model = confidenceOf({ ...a.confidence, latest: null });
  assert.equal(model.pending, true);
  assert.equal(model.repeats, a.confidence.repeats);
});

test("evaluated look: decision label, reason code, look number, intervals as text", () => {
  const a = attempts(view("campaign_final")).find((x) => x.confidence?.latest);
  const l = a.confidence.latest;
  const model = confidenceOf(a.confidence);
  assert.deepEqual(model.decision, label("confidence_decision", l.decision));
  assert.equal(model.reason, l.reason ?? "");
  assert.equal(model.look, l.look);
  assert.equal(model.final, l.final === true);
  assert.equal(model.focus.text, intervalText(l.focus));
  assert.deepEqual(caveatOf({ kind: "intervals_empirical" }),
    { code: "intervals_empirical", text: "Empirical intervals" });
  assert.deepEqual(caveatOf({ kind: "future_caveat" }),
    { code: "future_caveat", text: "future_caveat" });
});

test("interval text: point and bounds; missing bounds say so", () => {
  assert.equal(intervalText({ point: 0.5, lower: 0.1, upper: 0.9 }), "0.500 [0.100, 0.900]");
  assert.equal(intervalText({ point: 1, lower: null, upper: null }), "1.000 · no bounds in the view");
  assert.equal(intervalText({ point: 1, lower: 1, upper: null }), "1.000 [1.000, —] · no upper bound in the view");
  assert.equal(intervalText(null), "—");
});

test("margin and exact are explicit beside confidence bounds, including missing and false", () => {
  const c = attempts(view("campaign_final")).find((a) => a.confidence?.latest).confidence;
  const model = confidenceOf({ ...c, margin: 0.1, latest: { ...c.latest,
    focus: { point: 1, lower: 0, upper: 2, exact: false }, causal: { point: 1, lower: null, upper: null, exact: true } } });
  assert.equal(model.margin, "0.100");
  assert.equal(model.focus.exact, false);
  assert.equal(model.causal.exact, true);
  assert.equal(confidenceOf({ ...c, margin: null }).margin, "—");
  assert.equal(confidenceOf({ ...c, latest: { ...c.latest, focus: null } }).focus, null);
});

// Spec family14 §3.5: the view model gives recorded numbers as numbers; a missing bound is null, never 0.
test("interval: numbers kept, missing bound is null not 0", () => {
  assert.deepEqual(intervalOf({ point: 0.18, lower: 0.09, upper: 0.27, exact: false }),
    { point: 0.18, lower: 0.09, upper: 0.27, exact: false, text: "0.180 [0.090, 0.270]" });
  assert.deepEqual(intervalOf({ point: 0, lower: 0, upper: null }),
    { point: 0, lower: 0, upper: null, exact: null, text: intervalText({ point: 0, lower: 0, upper: null }) });
  const none = intervalOf({ point: 1, lower: null, exact: true });
  assert.equal(none.lower, null);
  assert.equal(none.upper, null);
  assert.equal(none.exact, true);
  assert.equal(intervalOf(null), null);
  assert.equal(intervalOf(undefined), null);
  // One shared scale for the drawn rows: min(lower, 0) .. max(upper, 0) with a 10% margin; no bounds, no scale.
  const scale = scaleOf([intervalOf({ point: 0.18, lower: 0.09, upper: 0.27 }), intervalOf({ point: 0.04, lower: -0.03, upper: 0.11 })]);
  assert.ok(Math.abs(scale.min - (-0.03 - 0.03)) < 1e-12 && Math.abs(scale.max - (0.27 + 0.03)) < 1e-12, scale);
  assert.equal(scaleOf([intervalOf({ point: 1, lower: null, upper: null }), null]), null);
  assert.equal(scaleOf([intervalOf({ point: 1, lower: 0.5, upper: null })]), null);
});

test("confidence: observe flag and focus-only margin", () => {
  const c = attempts(view("campaign_final")).find((a) => a.confidence?.latest).confidence;
  const observe = confidenceOf(c);
  assert.equal(c.mode, "observe");
  assert.equal(observe.observe, true);
  assert.equal(observe.mode, "observe");
  assert.equal(observe.marginValue, c.margin);
  assert.deepEqual(observe.cases, { focus: c.focus_cases, causal: c.causal_cases });
  assert.equal(observe.focus.lower, null);
  assert.equal(observe.scale, null);                              // observe: no bounds, nothing drawn
  const stat = confidenceOf({ ...c, mode: "statistical", margin: 0.05, latest: { ...c.latest,
    focus: { point: 0.18, lower: 0.09, upper: 0.27, exact: true }, causal: { point: 0.04, lower: -0.03, upper: 0.11, exact: null } } });
  assert.equal(stat.observe, false);
  assert.equal(stat.marginValue, 0.05);
  assert.equal(Object.hasOwn(stat.causal, "margin"), false);
  assert.equal(Object.hasOwn(stat.focus, "margin"), false);
  assert.ok(stat.scale && stat.scale.min < 0 && stat.scale.max > 0.27);
  assert.equal(confidenceOf({ ...c, margin: null }).marginValue, null);
  const pending = confidenceOf({ ...c, latest: null });
  assert.equal(pending.pending, true);
  assert.equal(pending.observe, true);
  assert.equal(pending.focus, null);
});
