// Family 15 on the Campaign screen (spec 2026-10-19 D8, Q2 as corrected, Q3, Q5): literal, agent-neutral
// lab-campaign-view/14 bodies in the shape Owner serves (owner/independent_view.py; the values are those of
// tests/test_owner_independent_view.py). The panel reads each scope by its own `display`, never by the campaign mode.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { currentForm, inspect, staticModule } from "./support.mjs";

const { independentModel, quantityText, referenceText, scopeStatement } = await staticModule("lib/views/independent.js");
const { campaignEntry, campaignModel } = await staticModule("lib/views/campaign.js");
const { IndependentBlock, IndependentEntry } = await staticModule("ui/independent.js");
const { measurementModel, diffPath } = await staticModule("lib/views/measurement.js");
const { okEntry, readOf } = await staticModule("lib/resource.js");
const { independentText, label } = await staticModule("lib/labels.js");

const R = (numerator, denominator = "1") => ({ format: "rational-hex/1", numerator, denominator });
const S = (value, exact) => ({ value, exact });
const ZERO = S(0, R("0"));
// The enclosure's upper end brackets L's computation; it is never a confidence bound (Q5). Its display value is set
// apart from every other number here, so its absence from the rendered text is visible.
const UPPER = S(0.4711, R("6000000000000019", "c8000000000000000"));
const BOUND_SCOPE = {
  name: "focus", rule: "bound", status: "available", unavailable: null, display: "bound",
  point: S(0.12, R("3", "19")), threshold: ZERO, strict: false, confidence: S(0.9, R("9", "a")),
  enclosure: { lower: S(0.03, R("3", "64")), upper: UPPER, steps: 64 },
  declared_count: 6, unit_count: 6, mass: S(1, R("1")), excluded_mass: ZERO,
  fallback: null, lower: null, exact: null, pairing: null,
};
// A confidence campaign's global scope with rule `point`: Core records no confidence and no enclosure.
const POINT_SCOPE = {
  name: "global", rule: "point", status: "available", unavailable: null, display: "point",
  point: S(0.05, R("1", "14")), threshold: ZERO, strict: false, confidence: null, enclosure: null,
  declared_count: 8, unit_count: 8, mass: S(1, R("1")), excluded_mass: ZERO,
  fallback: null, lower: null, exact: null, pairing: null,
};
const OFF_SCOPE = { ...BOUND_SCOPE, rule: "off", display: "no_threshold", threshold: null, strict: null,
  confidence: null, enclosure: null };
const SKIPPED_SCOPE = { name: "causal", rule: "bound", status: "skipped", unavailable: null, display: "status",
  point: null, threshold: null, strict: null, confidence: null, enclosure: null, declared_count: 0, unit_count: 0,
  mass: null, excluded_mass: null, fallback: null, lower: null, exact: null, pairing: null };
const UNAVAILABLE_SCOPE = { ...POINT_SCOPE, status: "unavailable", unavailable: "empty_support", display: "status" };
const NOISE = { kind: "negligible", allowance: ZERO, source: null, source_kind: "assumed_negligible",
  variation: "not_requested", units: "score_squared", observed_cases: 0, extra_measurements: 0 };
const COMPARISON = {
  outcome: "passed", reason: "passed", layer: "gate", deciding_layer: "focus", reference: "stored",
  reference_kind: "stored_non_contemporaneous", reference_measurement_id: null, verification: null, eligible: true, mode: "confidence",
  confidence_scope: "conditional_fixed_scope_independent_bounded_units",
  selection_bias_possible: ["focus_selection", "stored_reference"], excluded_reference_slots: 0,
  coverage: { candidate: { required: 6, settled: 6, admissible: 6 }, reference: { required: 6, settled: 6, admissible: 6 } },
  noise: NOISE, scopes: [BOUND_SCOPE, POINT_SCOPE],
  method: "bounded-weighted-mean/1", unit: "case", calibration: null, caveats: null, bound_fallback: null,
};
const COST = { basis: "full_scenario_complete_scoring_without_retries", baseline_slots: 0, candidate_gate_slots: 6,
  candidate_final_slots: 2, candidate_total_slots: 8, parent_slots: 0, champion_slots: 0,
  champion_basis: "per_reached_finalist", slots_per_refresh: 8, connection_preflight_slots: 0,
  probes_and_retries: "separate", noise_diagnostic_slots: 0, simplest_passing_path_slots: 8 };
const EXPLANATION = {
  status: "impossible", basis: "stored_reference", profile_digest: null,
  interpretation: "score_range_ceiling_not_a_success_or_power_prediction",
  bound_fallback: null,
  scopes: [
    { name: "focus", status: "calculated", rule: "bound", threshold: ZERO, strict: false, unit_count: 1, passes: false,
      confidence: S(0.9, R("9", "a")), delta_max: S(1, R("1")), c_max: S(0.5, R("1", "2")),
      e_max: { kind: "finite", value: S(0.5, R("1", "2")) }, fallback: null },
    { name: "causal", status: "skipped", rule: null, threshold: null, strict: null, unit_count: null, passes: null,
      confidence: null, delta_max: null, c_max: null, e_max: null, fallback: null },
    { name: "global", status: "calculated", rule: "point", threshold: ZERO, strict: true, unit_count: 8, passes: true,
      confidence: null, delta_max: S(1, R("1")), c_max: null, e_max: { kind: "positive_infinity", value: null }, fallback: null },
  ],
};
const ATTEMPT = { round: 1, attempt_id: "r01-a01", outcome: "finalist", reason: "passed", group: "in_progress",
  eligible_final_score: null, comparison: COMPARISON, gate_comparison: COMPARISON, global_comparison: null,
  gate_binding: null, global_binding: null, preflight: null, conditional_cost: COST };
const PREFLIGHT_ATTEMPT = { round: 1, attempt_id: "r01-a02", outcome: "rejected", reason: "structurally_unattainable",
  group: "not_enough_data", eligible_final_score: null, comparison: null, gate_comparison: null, global_comparison: null,
  gate_binding: null, global_binding: null, preflight: EXPLANATION, conditional_cost: null };
const INDEPENDENT = {
  evidence: "authenticated_publications", next_action: "continue_evolve",
  settings: { mode: "confidence", repeats: 1, verification: "reuse", stability: "stable", fresh_parent: false,
    global_rule: "point", train_cases: 8, train_slots: 8,
    execution_noise: { kind: "negligible", allowance: ZERO, source: null, source_kind: "assumed_negligible" },
    statistics: { method: "bounded-weighted-mean/1", unit: "case" }, bound_fallback: null },
  attempts: [ATTEMPT, PREFLIGHT_ATTEMPT],
  attainability: { preflight: [{ round: 1, explanation: EXPLANATION, refresh: { scenario: "retain_champion_through_remaining_round_budget",
    remaining_refreshes: 0, slots_per_refresh: 8, scenario_slots: 0, retry_ceiling_separate: true, connection_preflight_credit: "separate" } }],
    local_guidance: { status: "conditional_guidance", minimum_units: 4,
      assumptions: ["equal_independent_unit_weights", "zero_threshold", "all_ones_contrast_envelope"],
      interpretation: "not_an_admission_or_power_claim" } },
  work: { planned: null, committed: null, remaining: null },
};
const VIEW15 = {
  schema: "lab-campaign-view/15", lab_notices_state: "present", operation_id: "op15", family: 15, gate_mode: "confidence", caveats: [],
  roles: { editor: { kind: "subprocess" }, reflector: { kind: "subprocess" }, grouper: { kind: "subprocess" } },
  settings: { stability: "stable", round_stop: "first_accepted", baseline_refresh: null },
  connection: { state: "compatible", outcome: "compatible", attempts: 1, binding_digest: null, operation_id: null, detail: null },
  policy: { rounds: 5, attempts_per_round: 4, metric_primary: "score" }, progress: null, outcome: null,
  champions: [{ version: "v0", digest: "d", origin: "initial", attempt_id: null, measurement_id: "m-r00-champion" }],
  champions_state: "present",
  rounds: [{ round: 1, outcome: null, baseline_measurement_id: "m-r00-champion", baseline_version: "v0",
    reflection: { state: "present", reflection_id: "r01-x01", error_class: null, reason: null, hypotheses: [] },
    attempts: [
      { attempt_id: "r01-a01", outcome: "finalist", reason: "passed", group: "in_progress", measurement_id: "m-r01-a01", has_tree: true, failure: null },
      { attempt_id: "r01-a02", outcome: "rejected", reason: "structurally_unattainable", group: "not_enough_data", measurement_id: null, has_tree: true, failure: null }] }],
  measurements: [], suspensions: [], details_state: "present", details_reason: null, details_source: "core_raw",
  reflector_probe: null, reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable",
  lab_notices: [], independent: INDEPENDENT, checks: null, checks_state: "present", checks_reason: null,
};
const view = (patch = (x) => x) => patch(structuredClone(VIEW15));
const rendered = (campaign) => inspect(IndependentBlock({ model: independentModel(campaign), stale: false })).text;

test("bound scope: the Q5 statement, no upper bound", () => {
  const s = scopeStatement(BOUND_SCOPE);
  assert.equal(s.display, "bound");
  assert.equal(s.text, "gain 0.12 (estimate); with 90% confidence not below 0.03; threshold ≥ 0.00");
  assert.deepEqual(s.numbers, { point: "0.12", confidence: "90%", lower: "0.03", threshold: "0.00" });
  assert.ok(!s.text.includes(quantityText(BOUND_SCOPE.enclosure.upper)));
  const text = rendered(view());
  assert.ok(text.includes("gain 0.12 (estimate); with 90% confidence not below 0.03; threshold ≥ 0.00"));
  assert.ok(!text.includes("0.4711") && !text.includes("0.47"), "enclosure.upper never reaches the screen");
});

test("point global in a confidence campaign is point + threshold", () => {
  const s = scopeStatement(POINT_SCOPE);
  assert.equal(s.text, "gain 0.05; threshold ≥ 0.00");
  assert.deepEqual(s.numbers, { point: "0.05", confidence: null, lower: null, threshold: "0.00" });
  assert.equal(scopeStatement({ ...POINT_SCOPE, strict: true }).text, "gain 0.05; threshold > 0.00");
  // The campaign mode never chooses the statement: the same row reads the same in every mode.
  for (const mode of ["confidence", "point", "unfiltered"]) {
    const m = independentModel(view((c) => { c.independent.settings.mode = mode; c.independent.attempts[0].gate_comparison.mode = mode; return c; }));
    assert.equal(m.attempts[0].gate.scopes[1].text, "gain 0.05; threshold ≥ 0.00");
    assert.equal(m.attempts[0].gate.scopes[0].text, "gain 0.12 (estimate); with 90% confidence not below 0.03; threshold ≥ 0.00");
  }
});

test("unfiltered and off scopes say no threshold", () => {
  const s = scopeStatement(OFF_SCOPE);
  assert.equal(s.text, "gain 0.12 · no threshold");
  assert.deepEqual(s.numbers, { point: "0.12", confidence: null, lower: null, threshold: null });
  const m = independentModel(view((c) => {
    c.independent.settings.mode = "unfiltered";
    Object.assign(c.independent.attempts[0].gate_comparison, { mode: "unfiltered", scopes: [OFF_SCOPE, { ...POINT_SCOPE, rule: "off", display: "no_threshold", threshold: null }] });
    return c;
  }));
  assert.deepEqual(m.attempts[0].gate.scopes.map((x) => x.text), ["gain 0.12 · no threshold", "gain 0.05 · no threshold"]);
});

test("skipped scope shows Core's status, no numbers", () => {
  const skipped = scopeStatement(SKIPPED_SCOPE);
  assert.equal(skipped.text, "Skipped; threshold not calibrated yet");
  assert.deepEqual(skipped.numbers, { point: null, confidence: null, lower: null, threshold: null });
  const unavailable = scopeStatement(UNAVAILABLE_SCOPE);
  assert.equal(unavailable.text, "Unavailable (empty_support)");
  assert.doesNotMatch(unavailable.text, /\d/);
  assert.deepEqual(unavailable.numbers, { point: null, confidence: null, lower: null, threshold: null });
});

test("fresh reference reads 'vs round champion · fresh measurement'; never 'Parent'", () => {
  assert.equal(referenceText({ ...COMPARISON, reference: "fresh" }), "vs round champion · fresh measurement");
  assert.equal(referenceText(COMPARISON), "vs round champion · stored measurement");
  assert.equal(referenceText({ ...COMPARISON, reference: "unknown" }), "vs round champion · measurement not recorded");
  const fresh = view((c) => { Object.assign(c.independent.attempts[0].gate_comparison, { reference: "fresh", reference_kind: "fresh" }); return c; });
  const text = rendered(fresh);
  assert.ok(text.includes("vs round champion · fresh measurement"));
  assert.doesNotMatch(text, /parent/i);
});

test("out-of-range threshold prints the label, never Infinity", () => {
  const out = { ...POINT_SCOPE, threshold: S(null, R("1", "1")) };
  assert.equal(quantityText(out.threshold), "outside display range (exact value kept)");
  assert.equal(scopeStatement(out).text, "gain 0.05; threshold ≥ outside display range (exact value kept)");
  assert.equal(quantityText(null), "—");
  assert.equal(quantityText(S(null, null)), "—");
  const text = rendered(view((c) => { c.independent.attempts[0].gate_comparison.scopes[1] = out; return c; }));
  assert.doesNotMatch(text, /Infinity|NaN/);
  // e_max positive_infinity of the preflight is not a number on screen either
  assert.doesNotMatch(rendered(view()), /Infinity|NaN/);
});

test("unavailable evidence is not 'no attempts'", () => {
  for (const kind of ["snapshot_only", "unavailable"]) {
    const c = view((x) => { x.independent.evidence = kind; x.independent.attempts = []; return x; });
    const m = independentModel(c);
    assert.equal(m.evidence.kind, kind);
    assert.equal(m.evidence.text, `comparisons unavailable (${kind})`);
    const text = rendered(c);
    assert.ok(text.includes(`comparisons unavailable (${kind})`));
    assert.doesNotMatch(text, /no attempts/i);
  }
  const none = view((x) => { x.independent.attempts = []; return x; });
  assert.equal(independentModel(none).evidence.text, null);
  assert.match(rendered(none), /No attempts yet/);
});

test("an attempt without a comparison shows its reason and the preflight ceilings", () => {
  const m = independentModel(view());
  const a = m.attempts[1];
  assert.equal(a.gate, null);
  assert.equal(a.reason, "structurally_unattainable");
  assert.deepEqual(a.preflight, { status: "impossible", boundFallback: null, ceilings: [
    "focus maximal confidence 50% vs requested 90% (cannot pass)",
    "global maximal gain 1.00 vs threshold > 0.00 (can pass)"] });
  const text = rendered(view());
  assert.ok(text.includes("No comparison"));
  assert.ok(text.includes("structurally_unattainable"));
  assert.ok(text.includes("focus maximal confidence 50% vs requested 90% (cannot pass)"));
});

test("statements are followed by Core's outcome on the deciding scope and the reference label", () => {
  const c = independentModel(view()).attempts[0].gate;
  assert.equal(c.outcome.text, "Passed");
  assert.equal(c.outcome.tone, "green");
  assert.deepEqual(c.scopes.map((s) => s.outcome?.text ?? null), ["Passed", null]);
  assert.deepEqual(c.reference, { text: "vs round champion · stored measurement", measurementId: null, href: null });
  assert.equal(c.layer.text, "Gate comparison");
  const text = rendered(view());
  assert.ok(text.includes("→ Passed"));
});

test("settings summary: R, mode, execution noise, verification; cost in measurement slots", () => {
  const m = independentModel(view());
  assert.deepEqual(m.settings.map((s) => s.name), ["R (repeats)", "Mode", "Execution noise", "Verification", "Statistics"]);
  assert.equal(m.settings[0].value, "1");
  assert.equal(m.settings[1].value, "Confidence");
  assert.equal(m.settings[2].value, "negligible · allowance 0.00 · assumed_negligible");
  assert.equal(m.settings[3].value, "reuse");
  assert.equal(m.attempts[0].cost, "8 measurement slots (gate 6 · final 2 · simplest passing path 8)");
  assert.equal(m.attempts[1].cost, null);
  assert.ok(rendered(view()).includes("8 measurement slots"));
});

test("family 15 campaign model: independent block, no gate marks, format /16", () => {
  const model = campaignModel({ operation: undefined, campaign: view(), measurements: {} });
  assert.deepEqual(model.schema, { ok: true, code: null });
  assert.equal(model.independent.attempts.length, 2);
  assert.deepEqual(model.result.limits, []);        // family 15 has no gate_mode: never the family-14 "unknown gate"
  assert.equal(model.setup.settings.find((s) => s.name === "Format").value, "manifest /16 · family 15");
  assert.equal(model.lineage.rows[0].gate, null);
  // its rounds have no final records: no "Final records: Unavailable", no "no final" mark, no final rows
  assert.equal(model.rounds[0].finalState, null);
  assert.deepEqual(model.rounds[0].rows.map((r) => [r.kind, r.marks, r.limits]), [["attempt", [], []], ["attempt", [], []]]);
  const entry = campaignEntry(okEntry(view()));
  assert.equal(readOf(entry).state, "ok");
  // the attempt links to its measurement through Core's round attempt
  assert.match(model.independent.attempts[0].measurementHref, /m-r01-a01/);
  assert.equal(model.independent.attempts[1].measurementHref, null);
});

test("an unreadable independent block is the section's state, not an empty list", () => {
  const model = campaignModel({ campaign: view((c) => ({ ...c, independent: null, details_state: "unavailable", details_reason: "unreadable" })), measurements: {} });
  assert.deepEqual(model.independent, { state: "unavailable", reason: "unreadable" });
  const text = inspect(IndependentBlock({ model: model.independent })).text;
  assert.match(text, /unreadable/);
});

// Fix round 1 (review I2, ruling R7): the panel prints Core's numbers digit for digit as the CLI does. Expected
// strings are `_independent_text.quantity` / `percent` outputs (Python, round half to even on the exact binary value);
// ties are exact binary ties, where toFixed/toExponential would round away from zero.
const CLI_QUANTITY = [[0.03125, "0.0312"], [-0.03125, "-0.0312"], [0.09375, "0.0938"], [0.15625, "0.1562"],
  [1.03125, "1.0312"], [0, "0.00"], [-0, "-0.00"], [0.12, "0.12"], [0.05, "0.05"], [1, "1.00"], [0.5, "0.50"],
  [0.123456, "0.1235"], [-0.000123, "-0.0001"], [0.00015625, "0.0002"], [3.0517578125e-05, "3.05e-05"],
  [-9.1552734375e-05, "-9.16e-05"], [0.0001, "0.0001"], [12.34565, "12.3456"], [123456.789, "123456.789"], [1e-07, "1.00e-07"]];
const CLI_PERCENT = [[0.12125, "12.12%"], [0.12625, "12.62%"], [0.010625, "1.062%"], [0.12375, "12.38%"], [0.9, "90%"],
  [0.95, "95%"], [0.5, "50%"], [0.123456, "12.35%"], [1, "100%"], [0, "0%"], [-0, "-0%"], [0.999999, "100%"],
  [1e-07, "1e-05%"], [0.0001, "0.01%"]];

test("numbers round half to even exactly as the CLI: ties and -0.0", async () => {
  const { percentText } = await staticModule("lib/views/independent.js");
  for (const [v, text] of CLI_QUANTITY) assert.equal(quantityText(S(v, R("1"))), text, String(v));
  for (const [v, text] of CLI_PERCENT) assert.equal(percentText(S(v, R("1"))), text, String(v));
  // L = 0.03125 is never overstated as 0.0313
  const tie = { ...BOUND_SCOPE, enclosure: { ...BOUND_SCOPE.enclosure, lower: S(0.03125, R("1", "20")) } };
  assert.equal(scopeStatement(tie).text, "gain 0.12 (estimate); with 90% confidence not below 0.0312; threshold ≥ 0.00");
});

// Fix round 1 (review I1): a family-15 attempt's Measurement screen shows its independent entry, never family 14's
// Decision/Confidence panels nor the round baseline as its reference.
const MEASUREMENT = { schema: "lab-measurement/3", measurement: { measurement_id: "m-r01-a01", role: "attempt", round: 1,
  attempt_id: "r01-a01", samples: [], metrics: {}, baseline: null, coverage: null, times: {},
  lab_notices: [], lab_notices_state: "present" } };
const measured = (campaign) => measurementModel({ operation: undefined, campaign, measurement: MEASUREMENT, diff: null,
  measurementId: "m-r01-a01" });

test("family-15 measurement: no decision or confidence, the independent entry, the reference not the baseline", () => {
  const model = measured(view());
  assert.equal(model.attempt.id, "r01-a01");
  assert.equal(model.decision, null);
  assert.equal(model.confidence, null);
  assert.equal(model.independent.state, "present");
  assert.equal(model.independent.attempt.id, "r01-a01");
  assert.equal(model.independent.attempt.gate.reference.text, "vs round champion · stored measurement");
  assert.equal(model.header.against, "vs round champion · stored measurement");
  const fresh = measured(view((c) => { Object.assign(c.independent.attempts[0].gate_comparison, { reference: "fresh", reference_kind: "fresh" }); return c; }));
  assert.equal(fresh.header.against, "vs round champion · fresh measurement");
  for (const m of [model, fresh]) {
    assert.doesNotMatch(m.header.against, /against|v0|m-r00-champion|baseline/);
  }
  const entry = inspect(IndependentEntry({ independent: model.independent }));
  assert.ok(entry.text.includes("focus: result rule=bound · gain 0.12 (estimate); with 90% confidence not below 0.03; threshold ≥ 0.00 → Passed"));
  assert.ok(entry.text.includes("vs round champion · stored measurement"));
  assert.equal(entry.elements.some((e) => e.type === "a"), false, "no link to the page itself");
  // no comparison recorded: no reference at all, never the round baseline
  const none = measured(view((c) => { c.independent.attempts[0].gate_comparison = null; return c; }));
  assert.equal(none.header.against, null);
  // an attempt the block does not list, and an unreadable block
  const missing = measured(view((c) => { c.independent.attempts = []; return c; }));
  assert.deepEqual([missing.independent.state, missing.independent.attempt, missing.header.against], ["present", null, null]);
  assert.match(inspect(IndependentEntry({ independent: missing.independent })).text, /No independent comparison entry/);
  const unreadable = measured(view((c) => ({ ...c, independent: null, details_state: "unavailable", details_reason: "unreadable" })));
  assert.deepEqual([unreadable.independent.state, unreadable.independent.reason, unreadable.decision], ["unavailable", "unreadable", null]);
});

// Codex r2 (tenet inspectable-experiments): the Measurement screen keeps the evidence kind. Comparisons Core's block
// could not authenticate are unavailable with that kind, exactly as on the Campaign screen, never "no entry recorded".
test("family-15 measurement: unavailable or snapshot-only evidence says comparisons are unavailable, not that no entry exists", () => {
  for (const kind of ["unavailable", "snapshot_only"]) {
    const model = measured(view((c) => { Object.assign(c.independent, { evidence: kind, attempts: [] }); return c; }));
    assert.equal(model.independent.evidence.kind, kind);
    const text = inspect(IndependentEntry({ independent: model.independent })).text;
    assert.ok(text.includes(`comparisons unavailable (${kind})`), text);
    assert.doesNotMatch(text, /No independent comparison entry/);
  }
  const ok = measured(view((c) => { c.independent.attempts = []; return c; }));
  assert.equal(ok.independent.evidence.kind, "authenticated_publications");
  assert.match(inspect(IndependentEntry({ independent: ok.independent })).text, /No independent comparison entry recorded for this attempt/);
});

// Codex r2: a finalist/passed attempt whose global comparison failed on a critical case shows the comparison's reason
// beside its outcome, separately from the attempt's own reason (CLI and report.md say the same).
test("family-15 panel: the comparison's reason is shown beside its outcome, apart from the attempt's reason", () => {
  const model = measured(view((c) => { Object.assign(c.independent.attempts[0].gate_comparison, { outcome: "comparison_not_passed", reason: "critical_failed" }); return c; }));
  const text = inspect(IndependentEntry({ independent: model.independent })).text;
  assert.ok(text.includes("Not passed") && text.includes("critical_failed"), text);
  assert.ok(text.includes("passed"), "the attempt's own reason stays");
});

test("family 15 measurement links use both the round and attempt id", () => {
  const campaign = view((c) => {
    c.rounds.push({ ...structuredClone(c.rounds[0]), round: 2, attempts: [
      { ...structuredClone(c.rounds[0].attempts[0]), measurement_id: "m-r02-a01" }] });
    c.independent.attempts = [structuredClone(ATTEMPT), { ...structuredClone(ATTEMPT), round: 2 }];
    return c;
  });
  const attempts = independentModel(campaign).attempts;
  assert.deepEqual(attempts.map((a) => [a.round, a.measurementId, a.measurementHref]), [
    [1, "m-r01-a01", "#/operations/op15/measurements/m-r01-a01"],
    [2, "m-r02-a01", "#/operations/op15/measurements/m-r02-a01"],
  ]);
});

test("family 15 malformed independent containers and rows are unavailable", () => {
  const mutations = [
    (b) => { b.attempts = {}; }, (b) => { b.attempts = [null]; },
    (b) => { b.settings = []; }, (b) => { b.attainability = "bad"; },
    (b) => { b.attainability.preflight = {}; }, (b) => { b.attainability.preflight = [null]; },
    (b) => { b.attainability.preflight[0].explanation.scopes = {}; },
    (b) => { b.attempts[0].comparison = []; },
    (b) => { b.attempts[0].gate_comparison = []; },
    (b) => { b.attempts[0].global_comparison = []; },
    (b) => { b.attempts[0].global_comparison = { scopes: [null] }; },
    (b) => { b.attempts[0].gate_comparison.scopes = {}; },
    (b) => { b.attempts[0].gate_comparison.scopes = [null]; },
    (b) => { b.attempts[0].preflight = { scopes: {} }; b.attempts[0].gate_comparison = null; },
    (b) => { b.attempts[0].conditional_cost = []; },
  ];
  for (const mutate of mutations) {
    const campaign = view(); mutate(campaign.independent);
    const entry = campaignEntry(okEntry(campaign));
    assert.equal(readOf(entry).state, "unavailable", String(mutate));
    assert.equal(entry.data, undefined);
    assert.equal(campaignModel({ campaign, measurements: {} }).schema.ok, false);
  }
  for (const independent of [null, undefined]) {
    const campaign = view((c) => ({ ...c, independent }));
    assert.equal(campaignModel({ campaign, measurements: {} }).schema.ok, true);
  }
});


test("family 15 measured attempt keeps its own round's comparison and diff reference", () => {
  const campaign = view((c) => {
    c.rounds.push({ ...structuredClone(c.rounds[0]), round: 2, baseline_version: "v2", attempts: [
      { ...structuredClone(c.rounds[0].attempts[0]), measurement_id: "m-r02-a01" }] });
    c.independent.attempts = [structuredClone(ATTEMPT), { ...structuredClone(ATTEMPT), round: 2,
      gate_comparison: { ...structuredClone(COMPARISON), reference: "fresh" } }];
    return c;
  });
  const measurement = { measurement: { measurement_id: "m-r02-a01", round: 2, samples: [] } };
  const model = measurementModel({ campaign, measurement });
  assert.equal(model.independent.attempt.round, 2);
  assert.match(model.header.against, /fresh measurement/);
  assert.match(diffPath(campaign, campaign.rounds[1].attempts[0], "op15"), /from=champion:v2/);
});


test("explicit gate and global comparisons render once each; summary is never rendered", () => {
  const campaign = view((c) => {
    const a = c.independent.attempts[0];
    a.comparison = { ...structuredClone(COMPARISON), reference: "unknown", reason: "summary_only" };
    a.global_comparison = { ...structuredClone(COMPARISON), layer: "global", reference: "fresh", deciding_layer: "global",
      outcome: "comparison_not_passed", reason: "critical_failed" };
    return c;
  });
  const a = independentModel(campaign).attempts[0];
  assert.equal(a.gate.layer.text, "Gate comparison");
  assert.equal(a.global.layer.text, "Global comparison");
  const text = rendered(campaign);
  assert.equal(text.split("Gate comparison").length - 1, 1);
  assert.equal(text.split("Global comparison").length - 1, 1);
  assert.match(text, /critical_failed/);
  assert.doesNotMatch(text, /summary_only|measurement not recorded/);
  for (const layer of ["gate_comparison", "global_comparison"]) {
    const single = structuredClone(campaign); single.independent.attempts[0][layer] = null;
    assert.equal(independentModel(single).attempts[0][layer === "gate_comparison" ? "gate" : "global"], null);
    assert.doesNotMatch(rendered(single), layer === "gate_comparison" ? /Gate comparison/ : /Global comparison/);
  }
});

test("reference links only to measurements listed in this view and otherwise shows a short id", () => {
  const referenceId = "0123456789abcdef0123456789abcdef";
  for (const listed of [false, true]) {
    const campaign = view((c) => {
      c.independent.attempts[0].gate_comparison.reference_measurement_id = referenceId;
      c.measurements = listed ? [{ measurement_id: referenceId }] : [];
      return c;
    });
    const ref = independentModel(campaign).attempts[0].gate.reference;
    assert.equal(ref.measurementId, referenceId);
    assert.equal(ref.text, "vs round champion · stored measurement · 01234567");
    assert.equal(ref.href, listed ? `#/operations/op15/measurements/${referenceId}` : null);
    const dom = inspect(IndependentBlock({ model: independentModel(campaign) }));
    const links = dom.elements.filter((e) => e.type === "a" && e.props.href === `#/operations/op15/measurements/${referenceId}`);
    assert.equal(links.length, listed ? 1 : 0);
    assert.match(dom.text, /01234567/);
    assert.doesNotMatch(dom.text, /0123456789abcdef/);
  }
});

test("Measurement header uses gate stored reference even when summary/global says fresh", () => {
  const campaign = view((c) => {
    const a = c.independent.attempts[0];
    a.global_comparison = { ...structuredClone(COMPARISON), layer: "global", reference: "fresh", deciding_layer: "global" };
    a.comparison = structuredClone(a.global_comparison);
    return c;
  });
  const model = measured(campaign);
  assert.equal(model.header.against, "vs round champion · stored measurement");
  const entry = inspect(IndependentEntry({ independent: model.independent }));
  assert.match(entry.text, /Gate comparison/);
  assert.doesNotMatch(entry.text, /Global comparison/);
  assert.match(entry.text, /vs round champion · stored measurement/);
  assert.doesNotMatch(entry.text, /vs round champion · fresh measurement/);
  const final = measurementModel({ campaign, measurement: { measurement: { measurement_id: "m-final", role: "other", round: 1, samples: [] } } });
  assert.equal(final.attempt, null);
  assert.equal(final.independent, null);
  assert.equal(final.header.against, null);
});

// Neutral in-memory b10 DTO extensions, not recaptured or hand-edited Core/capture files.
const CALIBRATION = { source: "fresh", measurement_id: "m-parent", prefix: { version: 1, kind: "journal_prefix",
  run_id: "run-neutral", locator: "measurement.jsonl#bytes=0:10", content_digest: "prefix-digest" }, retained: 2,
  method: "f13-bootstrap/1", level: S(0.995, R("c7", "c8")), seed_input: "seed-digest",
  allowance: S(0.02, R("1", "32")), degenerate: false, result_digest: "result-digest" };
const BINDING = { source: "core_raw", scopes: [{ name: "causal", rule: "bound" }] };
const b10 = () => view((c) => {
  c.gate_mode = "confidence";
  c.independent.settings.bound_fallback = "point";
  const a = c.independent.attempts[0];
  a.gate_binding = structuredClone(BINDING);
  a.global_binding = { source: "core_raw", scopes: [{ name: "global", rule: "bound" }] };
  Object.assign(a.gate_comparison, { bound_fallback: "point", reference: "fresh", deciding_layer: "causal",
    calibration: structuredClone(CALIBRATION), caveats: ["noise_allowance_empirical", "noise_not_observed"],
    scopes: [{ ...structuredClone(POINT_SCOPE), name: "causal", threshold: null, display: "no_threshold",
      fallback: { reason: "causal_unconfirmable" } }] });
  a.global_comparison = { ...structuredClone(COMPARISON), layer: "global", deciding_layer: "global",
    scopes: [{ ...structuredClone(BOUND_SCOPE), name: "global" }] };
  return c;
});

test("b10 fresh reference: binding bound and fallback result point are separate recorded rules", () => {
  const campaign = b10(), before = structuredClone(campaign);
  const model = campaignModel({ campaign, measurements: {} });
  assert.equal(model.schema.ok, true);
  const text = rendered(campaign);
  assert.match(text, /gate binding \(core_raw\): causal=bound/);
  assert.match(text, /causal: result rule=point · gain 0.05; threshold not calibrated yet; weaker: point decision instead of a bound \(causal_unconfirmable\)/);
  assert.match(text, /global binding \(core_raw\): global=bound/);
  assert.match(text, /global: result rule=bound/);
  assert.match(text, /bound_fallback=point/);
  assert.match(text, /calibration fresh f13-bootstrap\/1 level=0.995 allowance=0.02 · retained 2 · degenerate false · measurement m-parent/);
  assert.match(text, /caveats: noise_allowance_empirical, noise_not_observed/);
  assert.equal(model.independent.settings.find((s) => s.name === "bound_fallback").value, "point");
  const measurementText = inspect(IndependentEntry({ independent: measured(campaign).independent })).text;
  assert.match(measurementText, /gate binding \(core_raw\): causal=bound/);
  assert.doesNotMatch(measurementText, /global binding|Global comparison|global: result rule/);
  assert.deepEqual(campaign, before, "rendering never modifies Owner's evidence");
});

test("b10 binding rules do not depend on the presence or rule of a result", () => {
  const campaign = b10(), a = campaign.independent.attempts[0];
  a.gate_comparison = a.global_comparison = null;
  assert.match(rendered(campaign), /gate binding \(core_raw\): causal=bound/);
  assert.match(rendered(campaign), /global binding \(core_raw\): global=bound/);
  assert.match(rendered(campaign), /No comparison/);
  const missing = b10(); missing.independent.attempts[0].gate_binding = null;
  const text = rendered(missing);
  assert.match(text, /gate binding unavailable/);
  assert.doesNotMatch(text, /gate binding \(core_raw\)/);
  assert.match(text, /causal: result rule=point/);
});

test("b10 bootstrap uses its own confidence/lower/exact/pairing, not the enclosure or campaign caveats", () => {
  const campaign = view((c) => {
    c.gate_mode = "statistical";
    Object.assign(c.independent.settings, { mode: "statistical", repeats: 2, statistics: { method: "f13-bootstrap/1", unit: null } });
    const comparison = c.independent.attempts[0].gate_comparison;
    Object.assign(comparison, { mode: "statistical", method: "f13-bootstrap/1", unit: null,
      caveats: ["intervals_empirical", "exact_contrast"], scopes: [{ ...structuredClone(BOUND_SCOPE),
        rule: "bootstrap", display: "bootstrap", lower: S(-0.07, R("-7", "64")), exact: false, pairing: "unpaired" }] });
    return c;
  });
  assert.equal(campaignModel({ campaign, measurements: {} }).schema.ok, true);
  const model = independentModel(campaign), s = model.attempts[0].gate.scopes[0];
  assert.equal(model.settings.find((s) => s.name === "Mode").value, "Statistical");
  assert.equal(s.text, "gain 0.12 (estimate); bootstrap 90%, lower -0.07; threshold ≥ 0.00");
  assert.equal(s.numbers.lower, "-0.07");
  const text = rendered(campaign);
  assert.match(text, /exact=false · pairing=unpaired/);
  assert.match(text, /method f13-bootstrap\/1 · unit —/);
  assert.match(text, /caveats: intervals_empirical, exact_contrast/);
  assert.doesNotMatch(s.text, /0.03|0.4711/);
  assert.equal(label("scope_rule", "bootstrap").known, true);
  for (const code of ["insufficient_repeats", "insufficient_calibration"])
    assert.equal(label("comparison15_reason", code).known, true);
});

test("b10 null threshold is uncalibrated for every active rule; off still has no threshold", () => {
  for (const [rule, display] of [["bound", "bound"], ["bootstrap", "bootstrap"], ["point", "point"], ["point", "no_threshold"]]) {
    const s = { ...BOUND_SCOPE, rule, display, threshold: null };
    assert.match(scopeStatement(s).text, /threshold not calibrated yet/);
    assert.doesNotMatch(scopeStatement(s).text, /no threshold|threshold [≥>] 0/);
    assert.match(scopeStatement(s, "ru").text, /порог ещё не откалиброван/);
  }
  assert.equal(scopeStatement(OFF_SCOPE).text, "gain 0.12 · no threshold");
  assert.equal(independentText("fallback", "ru"), "слабее: точечное решение вместо границы");
  assert.match(scopeStatement({ ...POINT_SCOPE, fallback: { reason: "focus_unattainable" } }, "ru").text,
    /слабее: точечное решение вместо границы \(focus_unattainable\)/);
});

test("b10 preflight shows uncalibrated and per-scope fallback, both for round and attempt", () => {
  const campaign = view((c) => {
    for (const p of [c.independent.attempts[1].preflight, c.independent.attainability.preflight[0].explanation]) {
      p.bound_fallback = "point";
      p.scopes[0] = { ...structuredClone(p.scopes[0]), rule: "point", fallback: { reason: "focus_unattainable" } };
      p.scopes[1] = { ...structuredClone(p.scopes[1]), status: "uncalibrated" };
    }
    return c;
  });
  const text = rendered(campaign);
  assert.equal(text.split("causal: threshold not calibrated yet").length - 1, 2);
  assert.equal(text.split("weaker: point decision instead of a bound (focus_unattainable)").length - 1, 2);
  assert.equal(text.split("bound_fallback=point").length - 1, 2);
  assert.doesNotMatch(text, /NaN|Infinity/);
});

test("b10 quantities never coerce null, nonnumeric or exact-only lower/level/allowance into a decision", () => {
  for (const value of [null, S(null, R("1")), S(null, null), S("0.9", null), S(false, null), S(Infinity, null), S(NaN, null)]) {
    const scope = { ...BOUND_SCOPE, rule: "bootstrap", display: "bootstrap", lower: value, confidence: value };
    const text = scopeStatement(scope).text;
    assert.doesNotMatch(text, /NaN|Infinity|bootstrap 0%|lower 0.00/);
    assert.match(text, /—|outside display range/);
  }
  const campaign = b10();
  campaign.independent.attempts[0].gate_comparison.calibration.source = "stored";
  campaign.independent.attempts[0].gate_comparison.calibration.level = S(null, R("1"));
  campaign.independent.attempts[0].gate_comparison.calibration.allowance = null;
  const text = rendered(campaign);
  assert.match(text, /calibration stored f13-bootstrap\/1 level=outside display range \(exact value kept\) allowance=—/);
});

test("b10 malformed new DTO fields fail closed before rendering (including independent layers)", () => {
  const mutations = [
    (b) => { b.settings.mode = {}; }, (b) => { b.settings.bound_fallback = "bound"; },
    (b) => { b.settings.statistics.method = {}; }, (b) => { b.settings.statistics.method = "future"; },
    (b) => { b.attempts[0].gate_binding = []; },
    (b) => { b.attempts[0].gate_binding.source = "core_public"; },
    (b) => { b.attempts[0].gate_binding.scopes = [null]; },
    (b) => { b.attempts[0].gate_binding.scopes[0].rule = "future"; },
    (b) => { b.attempts[0].global_binding.scopes[0].extra = true; },
    (b) => { b.attempts[0].gate_comparison.bound_fallback = {}; },
    (b) => { b.attempts[0].gate_comparison.method = "future"; },
    (b) => { b.attempts[0].gate_comparison.caveats = [{ kind: "intervals_empirical" }]; },
    (b) => { b.attempts[0].global_comparison.caveats = ["future"]; },
    (b) => { b.attempts[0].gate_comparison.scopes[0].fallback = { reason: "future" }; },
    (b) => { b.attempts[0].gate_comparison.scopes[0].fallback.extra = true; },
    (b) => { b.attempts[0].gate_comparison.scopes[0].lower = { value: "0", exact: null }; },
    (b) => { b.attempts[0].gate_comparison.scopes[0].exact = 1; },
    (b) => { b.attempts[0].gate_comparison.scopes[0].pairing = {}; },
    (b) => { b.attempts[0].gate_comparison.calibration.level = { value: Infinity, exact: null }; },
    (b) => { b.attempts[0].gate_comparison.calibration.allowance = { value: 0, exact: [] }; },
    (b) => { b.attempts[0].gate_comparison.calibration.prefix = []; },
    (b) => { b.attempts[0].gate_comparison.calibration.retained = "2"; },
    (b) => { b.attempts[0].gate_comparison.calibration.degenerate = "false"; },
    (b) => { b.attempts[0].gate_comparison.calibration.extra = true; },
    (b) => { b.attempts[1].preflight.bound_fallback = "future"; },
    (b) => { b.attainability.preflight[0].explanation.scopes[0].fallback = []; },
  ];
  for (const mutate of mutations) {
    const campaign = b10(); mutate(campaign.independent);
    assert.equal(readOf(campaignEntry(okEntry(campaign))).code, "unsupported_schema", String(mutate));
    assert.equal(campaignModel({ campaign, measurements: {} }).schema.ok, false, String(mutate));
    assert.equal(measured(campaign).schema.ok, false, String(mutate));
  }
});

test("historical test adapter maps views/reports only in memory, never derives binding rules", () => {
  for (const [schema, expected] of [["lab-campaign-view/8", "lab-campaign-view/15"], ["lab-campaign-view/9", "lab-campaign-view/15"],
    ["lab-owner-report/5", "lab-owner-report/15"], ["lab-owner-report/6", "lab-owner-report/15"]]) {
    const campaign = schema.startsWith("lab-campaign-view/");
    const body = campaign ? { ...view(), schema } : { schema, report: { lab_independent: structuredClone(INDEPENDENT) } };
    const before = structuredClone(body), adapted = currentForm(body);
    assert.equal(adapted.schema, expected);
    assert.deepEqual([adapted.reflector_probe, adapted.reflector_probe_state, adapted.reflector_probe_reason],
      [null, "unavailable", "unreadable"]);
    const block = campaign ? adapted.independent : adapted.report.lab_independent;
    assert.equal(block.attempts[0].gate_binding, null);
    assert.equal(block.attempts[0].global_binding, null);
    assert.deepEqual(body, before);
    assert.equal(currentForm(adapted), adapted, "current forms bypass the test adapter");
  }
});

test("native historical view/report copies preserve authenticated bindings and producing bytes", () => {
  const filename = join(process.env.LAB_PANEL_FIXTURES, "family15", "routes.json");
  const bytes = readFileSync(filename, "utf8"), routes = JSON.parse(bytes);
  for (const [schema, expected] of [["lab-campaign-view/10", "lab-campaign-view/15"],
    ["lab-owner-report/7", "lab-owner-report/15"]]) {
    const raw = Object.values(routes).find((r) => r.body?.schema === schema).body;
    assert.equal(raw.schema, schema);
    const before = structuredClone(raw), adapted = currentForm(raw);
    assert.equal(adapted.schema, expected);
    assert.deepEqual([adapted.reflector_probe, adapted.reflector_probe_state, adapted.reflector_probe_reason],
      [null, "unavailable", "unreadable"]);
    const campaign = schema === "lab-campaign-view/10";
    const block = campaign ? adapted.independent : adapted.report.lab_independent;
    assert.deepEqual(block, campaign ? raw.independent : raw.report.lab_independent);
    assert.ok(block.attempts.some((a) => a.gate_binding !== null));
    assert.deepEqual(campaign ? [adapted.checks, adapted.checks_state, adapted.checks_reason]
      : [adapted.report.lab_checks, adapted.report.lab_checks_state, adapted.report.lab_checks_reason], [null, "present", null]);
    assert.deepEqual(raw, before);
    assert.equal(currentForm(adapted), adapted);
  }
  assert.equal(readFileSync(filename, "utf8"), bytes);
});
