import { test } from "node:test";
import assert from "node:assert/strict";
import { body, snapshot, staticModule } from "./support.mjs";

const { evaluationModel } = await staticModule("lib/views/evaluation.js");
const ids = (name) => snapshot(name).meta.ids;
const get = (name, path) => body(name, path);

// Spec family14 §3.8: counters are the number of strip cells of each existing kind, not a new classification.
const input = (patch = (m) => m) => {
  const opId = ids("details").evaluation;
  const measurement = patch(structuredClone(get("details", `/v1/operations/${opId}/measurements/c-${opId}`)));
  return { operation: get("details", `/v1/operations/${opId}`), measurement };
};

test("evaluation: strip counts by cell kind with zeros shown", () => {
  const { cases, strip } = evaluationModel(input());
  const cells = strip.rows[0].cells;
  const n = (kind) => cells.filter((c) => c.kind === kind).length;
  assert.deepEqual(cases.counts.map((c) => c.kind).slice(0, 2), ["pass", "fail"]);
  assert.equal(cases.counts.find((c) => c.kind === "pass").n, n("pass"));
  assert.equal(cases.counts.find((c) => c.kind === "fail").n, n("fail"));
  assert.equal(cases.counts.reduce((sum, c) => sum + c.n, 0), cells.length);
  // Ruling R10: the counters use the cell-kind code words ("1 pass · 1 fail"), the legend keeps its captions.
  assert.deepEqual(cases.counts.find((c) => c.kind === "pass"), { kind: "pass", text: "pass", n: n("pass") });
  assert.ok(cases.counts.every((c) => c.text === c.kind));
  const none = evaluationModel(input((m) => { for (const s of m.measurement.samples) s.metrics = {}; return m; })).cases.counts;
  assert.equal(none.find((c) => c.kind === "pass").n, 0);
  assert.equal(none.find((c) => c.kind === "fail").n, 0);
  assert.equal(none.find((c) => c.kind === "none").n > 0, true);
});

test("evaluation: unranked cells are counted as unranked", () => {
  const { cases } = evaluationModel(input((m) => {
    m.measurement.metrics.other = { mean: 0.5, count: 1 };
    for (const s of m.measurement.samples) s.metrics.other = 0.5;
    return m;
  }));
  const total = cases.rows.length;
  assert.deepEqual(cases.counts.map(({ kind, n }) => ({ kind, n })),
    [{ kind: "pass", n: 0 }, { kind: "fail", n: 0 }, { kind: "unranked", n: total }]);
  assert.equal(cases.counts[2].text, "unranked");
});

test("evaluation: excluded 0 is kept", () => {
  assert.equal(evaluationModel(input((m) => { m.measurement.coverage.excluded_pairs = 0; return m; })).cases.excluded, 0);
  assert.equal(evaluationModel(input((m) => { m.measurement.coverage.excluded_pairs = 3; return m; })).cases.excluded, 3);
  // The Coverage card keeps a recorded 0 too (mock: "excluded 0 is shown in the caption").
  const coverage = (n) => evaluationModel(input((m) => { m.measurement.coverage.excluded_pairs = n; return m; }))
    .cards.find((c) => c.name === "Coverage").sub;
  assert.equal(coverage(0), "0 excluded");
  assert.equal(coverage(3), "3 excluded");
  assert.equal(coverage(undefined), "");
});

test("evaluation: case rows carry the critical wording of the case head", () => {
  const rows = evaluationModel(input((m) => {
    m.measurement.samples.forEach((s, i) => { s.critical_pass = [true, false, null][i % 3]; });
    return m;
  })).cases.rows;
  assert.deepEqual(rows.slice(0, 3).map((r) => [r.critical, r.criticalMeta]).slice(0, Math.min(3, rows.length)),
    [["yes", "critical pass"], ["no", "critical fail"], ["—", "critical —"]].slice(0, Math.min(3, rows.length)));
});

// Codex r1 evaluation-case-count-accumulator: "N cases · K samples each" when every case has the same count, else
// the case browser's caption; counted with a Map, so ids such as `constructor` and `__proto__` are plain case ids.
const withCases = (pairs) => input((m) => {
  const [first] = m.measurement.samples;
  m.measurement.samples = pairs.map(([caseId, sampleId]) => ({ ...structuredClone(first), case_id: caseId, sample_id: sampleId }));
  return m;
});
test("evaluation: the cases caption counts samples per case", async () => {
  const { casesCaptionOf } = await staticModule("lib/views/evaluation.js");
  const { casesCaption } = await staticModule("ui/cases.js");
  const caption = (pairs) => evaluationModel(withCases(pairs)).cases.caption;
  assert.equal(caption([["q1", "s1"]]), "1 case · 1 sample each");
  assert.equal(caption([["q1", "s1"], ["q1", "s2"], ["q2", "s1"], ["q2", "s2"]]), "2 cases · 2 samples each");
  const mixed = evaluationModel(withCases([["q1", "s1"], ["q1", "s2"], ["q2", "s1"]])).cases.rows;
  assert.equal(casesCaptionOf(mixed), casesCaption(mixed));
  assert.equal(casesCaptionOf(mixed), "2 cases · 3 samples · click a row or use ↑ / ↓");
  assert.equal(casesCaptionOf([]), casesCaption([]));
  // Prototype keys are case ids like any other.
  assert.equal(caption([["constructor", "s1"], ["__proto__", "s1"], ["constructor", "s2"], ["__proto__", "s2"]]),
    "2 cases · 2 samples each");
  assert.equal(caption([["constructor", "s1"], ["toString", "s1"]]), "2 cases · 1 sample each");
  const proto = evaluationModel(withCases([["__proto__", "s1"], ["__proto__", "s2"], ["q1", "s1"]])).cases.rows;
  assert.equal(casesCaptionOf(proto), "2 cases · 3 samples · click a row or use ↑ / ↓");
});
