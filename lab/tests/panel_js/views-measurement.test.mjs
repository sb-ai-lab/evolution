import { test } from "node:test";
import assert from "node:assert/strict";
import { body, canonical, inspect, snapshot, staticModule } from "./support.mjs";

const { measurementModel, diffPath } = await staticModule("lib/views/measurement.js");
const { evaluationModel } = await staticModule("lib/views/evaluation.js");
const { okEntry } = await staticModule("lib/resource.js");
const { label } = await staticModule("lib/labels.js");
const ids = (name) => snapshot(name).meta.ids;
const get = (name, path) => body(name, path);

function attemptModel(name, opId, pick = (a) => a.measurement_id, patch = (c) => c) {
  const campaign = patch(structuredClone(get(name, `/v1/operations/${opId}/campaign`)));
  const attempt = campaign.rounds.flatMap((r) => r.attempts).find((a) => pick(a));
  const mid = attempt.measurement_id;
  const path = diffPath(campaign, attempt, opId);
  const entry = path ? snapshot(name).routes[canonical(path)] : null;
  const diff = entry?.status === 200 ? okEntry(entry.body) : null;
  return { attempt, model: measurementModel({ operation: get(name, `/v1/operations/${opId}`), campaign,
    measurement: get(name, `/v1/operations/${opId}/measurements/${mid}`), diff, measurementId: mid }) };
}

test("measurement /4 with unanswered evidence remains usable by the measurement screen", () => {
  const opId = ids("details").campaign_mixed;
  const operation = get("details", `/v1/operations/${opId}`);
  const campaign = get("details", `/v1/operations/${opId}/campaign`);
  const attempt = campaign.rounds.flatMap((round) => round.attempts).find((row) => row.measurement_id);
  const mid = attempt.measurement_id;
  const original = get("details", `/v1/operations/${opId}/measurements/${mid}`);
  const measurement = structuredClone(original);
  measurement.schema = "lab-measurement/4";
  Object.assign(measurement.measurement, { unanswered: 0, unanswered_state: "present",
    answered_reported: measurement.measurement.coverage.scored });
  const model = measurementModel({ operation, campaign, measurement, measurementId: mid });
  const prior = measurementModel({ operation, campaign, measurement: original, measurementId: mid });
  assert.equal(model.schema.ok, true);
  assert.equal(model.id, mid);
  assert.ok(model.cases.rows.length > 0);
  assert.deepEqual(model.cases, prior.cases);
  assert.deepEqual(model.cards, prior.cards);
});

test("accepted attempt: promoted version, diff summary against its round baseline", () => {
  const opId = ids("details").campaign_mixed;
  const { attempt, model } = attemptModel("details", opId, (a) => a.group === "accepted");
  assert.equal(model.attempt.id, attempt.attempt_id);
  assert.equal(model.decision.champion.kind, "promoted");
  assert.match(model.decision.champion.versions[0] ?? "", /^v\d+$/);
  assert.equal(model.diff.files.length > 0, true);
  assert.equal(model.diff.added > 0, true);
});

test("reflector hypotheses are shown as written, with the disclosure caution and the missing link", () => {
  const opId = ids("details").campaign_mixed;
  const view = get("details", `/v1/operations/${opId}/campaign`);
  const { model } = attemptModel("details", opId);
  assert.deepEqual(model.reflector.hypotheses, view.rounds[0].reflection.hypotheses);
  assert.match(model.reflector.caution, /ADR-0019/);
  assert.equal(model.reflector.link, "core");
});

test("details unavailable: the reflector block says so, cards stay", () => {
  const opId = ids("details").campaign_mixed;
  const { model } = attemptModel("details", opId, (a) => a.measurement_id,
    (c) => ({ ...c, details_state: "unavailable", details_reason: "unreadable" }));
  assert.deepEqual(model.reflector, { unavailable: "unreadable" });
  assert.equal(model.cards.length >= 4, true);
});

test("cases: baseline and candidate values side by side, comparison from Lab", () => {
  const opId = ids("details").campaign_mixed;
  const { model } = attemptModel("details", opId);
  const row = model.cases.rows[0];
  assert.equal(typeof row.case, "string");
  assert.equal(row.caseId, row.case);
  assert.equal(row.sampleId, row.sample);
  assert.equal(["Better", "Same", "Worse", "Unpaired", "Unknown"].includes(row.comparison.text), true);
  // Task 10 item 11: the case head says critical as the side does ("critical pass" / "critical fail" / "critical —").
  for (const r of model.cases.rows)
    assert.equal(r.criticalMeta, { yes: "critical pass", no: "critical fail", "—": "critical —" }[r.critical]);
});

test("evaluation: one metric colours the cells; several metrics do not", () => {
  const opId = ids("details").evaluation;
  const operation = get("details", `/v1/operations/${opId}`);
  const measurement = get("details", `/v1/operations/${opId}/measurements/c-${opId}`);
  const one = evaluationModel({ operation, measurement });
  assert.notEqual(one.strip.rows[0].cells[0].kind, "unranked");
  const two = structuredClone(measurement);
  two.measurement.metrics.other = { mean: 0.5, count: 1 };
  for (const s of two.measurement.samples) s.metrics.other = 0.5;
  const many = evaluationModel({ operation, measurement: two });
  assert.equal(many.strip.rows[0].cells.every((c) => c.kind === "unranked"), true);
  assert.deepEqual(many.metrics.sort(), [...Object.keys(measurement.measurement.metrics), "other"].sort());
});

test("evaluation: critical pass is a count over samples that carry it", () => {
  const opId = ids("details").evaluation;
  const measurement = get("details", `/v1/operations/${opId}/measurements/c-${opId}`);
  const samples = measurement.measurement.samples;
  const pass = samples.filter((s) => s.critical_pass === true).length;
  const known = samples.filter((s) => s.critical_pass !== null && s.critical_pass !== undefined).length;
  const card = evaluationModel({ operation: get("details", `/v1/operations/${opId}`), measurement }).cards.find((c) => c.name === "Critical pass");
  assert.equal(card.value, `${pass} / ${known}`);
});

// The gate-mark rows of a page's limits (observe or the fail-closed unknown), with their scope.
const gateLimits = (model) => model.caveats.filter((c) => c.code === "observe_gate_not_enforced" || c.code === "unknown")
  .map(({ code, scope }) => ({ code, scope }));

// Additions beyond the brief: the Editor-declined attempt has no diff path; baseline measurement has no attempt.
const FIELDS = (name, opId) => {
  const campaign = get(name, `/v1/operations/${opId}/campaign`);
  const attempts = campaign.rounds.flatMap((r) => r.attempts);
  return { campaign, attempts, operation: get(name, `/v1/operations/${opId}`) };
};
const modelFor = (name, opId, campaign, mid, diff = null) => measurementModel({ operation: get(name, `/v1/operations/${opId}`),
  campaign, measurement: get(name, `/v1/operations/${opId}/measurements/${mid}`), diff, measurementId: mid });

test("no candidate tree: has_tree false, no diff path, the model says so", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign, attempts } = FIELDS("details", opId);
  assert.equal(attempts.some((a) => a.has_tree === false), true);  // the view has declined attempts (no measurement row)
  const c = structuredClone(campaign);
  const served = c.rounds.flatMap((r) => r.attempts).find((a) => a.measurement_id);
  served.has_tree = false;  // a declined attempt that still has a served measurement body
  assert.equal(diffPath(c, served, opId), null);
  const model = modelFor("details", opId, c, served.measurement_id);
  assert.equal(model.diff.none, "No candidate tree for this attempt");
});

test("an attempt with a tree and no served diff yet is loading, not 'no tree'", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign, attempts } = FIELDS("details", opId);
  const worse = attempts.find((a) => a.has_tree === true && a.measurement_id);
  const model = modelFor("details", opId, campaign, worse.measurement_id);
  assert.equal(model.diff.none, undefined);
  assert.equal(model.diff.read.state, "loading");
});

test("details unavailable: a tree exists but the baseline is unknown -> Unavailable with the details reason", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign, attempts } = FIELDS("details", opId);
  const c = structuredClone(campaign);
  c.details_state = "unavailable"; c.details_reason = "unreadable";
  for (const r of c.rounds) { r.baseline_version = null; r.reflection = null; }
  const withTree = attempts.find((a) => a.has_tree === true && a.measurement_id);
  const attempt = c.rounds.flatMap((r) => r.attempts).find((a) => a.attempt_id === withTree.attempt_id);
  assert.equal(diffPath(c, attempt, opId), null);
  const model = modelFor("details", opId, c, attempt.measurement_id);
  assert.deepEqual(model.diff, { unavailable: "unreadable" });
  c.details_reason = null;
  assert.deepEqual(modelFor("details", opId, c, attempt.measurement_id).diff, { unavailable: "unavailable" });
});

test("champions unavailable: has_tree unknown -> Unavailable with the champions state, never 'no tree'", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign, attempts } = FIELDS("details", opId);
  const c = structuredClone(campaign);
  c.champions = null; c.champions_state = "unavailable";
  for (const a of c.rounds.flatMap((r) => r.attempts)) a.has_tree = null;
  const attempt = c.rounds.flatMap((r) => r.attempts).find((a) => a.measurement_id === attempts.find((x) => x.measurement_id).measurement_id);
  assert.equal(diffPath(c, attempt, opId), null);
  assert.deepEqual(modelFor("details", opId, c, attempt.measurement_id).diff, { unavailable: "unavailable" });
});

test("baseline measurement: no attempt, no diff, the reflector of the round it opens; role and round from the body", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign } = FIELDS("details", opId);
  const round = campaign.rounds.find((r) => r.baseline_measurement_id);
  const mid = round.baseline_measurement_id;
  const served = get("details", `/v1/operations/${opId}/measurements/${mid}`).measurement;
  const model = modelFor("details", opId, campaign, mid);
  assert.equal(model.attempt, null);
  assert.equal(model.role, served.role);
  assert.equal(model.round, served.round);
  assert.equal(model.diff.none, "Baseline measurement: no candidate");
  assert.equal(model.reflector.hypotheses.length, round.reflection.hypotheses.length);
  assert.equal(model.strip.rows.length, 1);
});

test("a round the view does not have yet: section-state wording, no invented code", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign } = FIELDS("details", opId);
  const mid = campaign.rounds.find((r) => r.baseline_measurement_id).baseline_measurement_id;
  const c = structuredClone(campaign);
  c.rounds = [];
  const model = modelFor("details", opId, c, mid);
  assert.equal(model.round, get("details", `/v1/operations/${opId}/measurements/${mid}`).measurement.round);
  assert.equal(model.role, "baseline");
  assert.deepEqual(model.reflector, { pending: label("section_state", "not_started") });
});

test("evaluation header: times_state not present -> section-state text, with the reason code when given", () => {
  const opId = ids("details").evaluation;
  const operation = get("details", `/v1/operations/${opId}`);
  const measurement = get("details", `/v1/operations/${opId}/measurements/c-${opId}`);
  const at = (patch) => evaluationModel({ operation: { ...operation, ...patch }, measurement }).meta.times;
  assert.deepEqual(at({ times_state: "not_started", times: null, times_reason: null }), { note: "Not started" });
  assert.deepEqual(at({ times_state: "unavailable", times: null, times_reason: "unreadable" }), { note: "Unavailable · Unreadable" });
  assert.equal(at({}).note, undefined);
});

test("cards: repairs move into the coverage caption", () => {
  const opId = ids("details").campaign_mixed;
  const { model } = attemptModel("details", opId);
  assert.deepEqual(model.cards.map((c) => c.name), ["Mean score vs baseline", "Cases better · same · worse",
    "Coverage · excluded pairs", "Retries · call failures", "Target tokens · partial"]);
  const coverageCard = (patch) => {
    const opId2 = ids("details").campaign_mixed;
    const campaign = get("details", `/v1/operations/${opId2}/campaign`);
    const mid = campaign.rounds.flatMap((r) => r.attempts).find((a) => a.measurement_id).measurement_id;
    const m = structuredClone(get("details", `/v1/operations/${opId2}/measurements/${mid}`));
    Object.assign(m.measurement, patch);
    return measurementModel({ operation: get("details", `/v1/operations/${opId2}`), campaign, measurement: m,
      diff: null, measurementId: mid }).cards.find((c) => c.name === "Coverage · excluded pairs");
  };
  const coverage = { expected: 4, scored: 3, excluded_pairs: 1 };
  assert.deepEqual(coverageCard({ coverage, repairs: null }), { name: "Coverage · excluded pairs", value: "3 / 4", sub: "1 excluded" });
  assert.deepEqual(coverageCard({ coverage, repairs: { tries: 3, pairs_scored: 1 } }),
    { name: "Coverage · excluded pairs", value: "3 / 4", sub: "1 excluded · repairs 3 tries · 1 pairs scored" });
  // 0 is a recorded count, shown, never dropped (constraint: null/false/0 are shown).
  assert.equal(coverageCard({ coverage: { ...coverage, excluded_pairs: 0 }, repairs: { tries: 0, pairs_scored: 0 } }).sub,
    "0 excluded · repairs 0 tries · 0 pairs scored");
  assert.equal(coverageCard({ coverage: { ...coverage, excluded_pairs: null }, repairs: { tries: 2, pairs_scored: 2 } }).sub,
    "repairs 2 tries · 2 pairs scored");
  // The token caption is its own line (note), not the value's sub.
  const tokensCard = model.cards.at(-1);
  assert.equal(tokensCard.sub, "");
  assert.equal(typeof tokensCard.note, "string");
  assert.ok(tokensCard.note.length > 0);
  assert.equal(model.strip.rows.length, 2);
  assert.equal(model.strip.rows[0].label, "baseline v0");
  assert.equal(model.strip.rows[0].cells.length, model.strip.rows[1].cells.length);
});

// Codex r1 (tenet inspectable-experiments): a value derived from a section keeps the section's availability.
const unavailableDetails = (c) => {
  c.details_state = "unavailable"; c.details_reason = "unreadable";
  for (const r of c.rounds) { r.baseline_version = null; r.reflection = null; for (const a of r.attempts) a.failure = null; }
  return c;
};

test("details unavailable: the Editor failure is Unavailable with the details reason, never 'no failure'", () => {
  const opId = ids("details").campaign_mixed;
  const { model } = attemptModel("details", opId, (a) => a.measurement_id, unavailableDetails);
  assert.deepEqual([model.editor.unavailable, model.editor.failure], ["unreadable", null]);
  const noReason = attemptModel("details", opId, (a) => a.measurement_id, (c) => ({ ...unavailableDetails(c), details_reason: null })).model;
  assert.equal(noReason.editor.unavailable, "unavailable");
  // Details present: the served failure (copied from the fixture's declined attempt onto a measured one).
  const withFailure = (c) => {
    const attempts = c.rounds.flatMap((r) => r.attempts);
    attempts.find((a) => a.measurement_id).failure = attempts.find((a) => a.failure).failure;
    return c;
  };
  const present = attemptModel("details", opId, (a) => a.measurement_id, withFailure).model;
  assert.equal(present.editor.unavailable, null);
  assert.match(present.editor.failure, /editor/);
  assert.equal(attemptModel("details", opId, (a) => a.measurement_id).model.editor.failure, null);  // none served
});

test("details unavailable: the baseline row names the version as unavailable, not '—'", () => {
  const opId = ids("details").campaign_mixed;
  const { model } = attemptModel("details", opId, (a) => a.measurement_id, unavailableDetails);
  assert.equal(model.strip.rows[0].label, `baseline ${label("section_state", "unavailable").text}`);
});

test("champions unavailable: the promotion mark is unknown, not silently absent; a known non-promotion is null", () => {
  const opId = ids("details").campaign_mixed;
  const accepted = (a) => a.group === "accepted";
  const unknown = attemptModel("details", opId, accepted, (c) => ({ ...c, champions: null, champions_state: "unavailable" })).model;
  assert.deepEqual(unknown.decision.champion, { kind: "unavailable", reason: "unavailable" });
  const unpromoted = attemptModel("details", opId, accepted, (c) => ({ ...c,
    champions: c.champions.filter((ch) => ch.origin !== "promoted_attempt") })).model;
  assert.deepEqual(unpromoted.decision.champion, { kind: "none" });
  const { campaign } = FIELDS("details", opId);
  const baseline = campaign.rounds.find((r) => r.baseline_measurement_id).baseline_measurement_id;
  const c = { ...structuredClone(campaign), champions: null, champions_state: "unavailable" };
  assert.equal(modelFor("details", opId, c, baseline).decision, null);   // a baseline is never promoted: no Decision
});

test("family 14: promotion is found by the attempt, not by the champion's final measurement; observe mark", () => {
  const opId = ids("details").campaign_mixed;
  const patch = (c) => ({ ...c, gate_mode: "observe",
    caveats: [{ kind: "observe_gate_not_enforced", round: 1, attempt_id: "r01-a02" }],
    champions: c.champions.map((ch) => (ch.origin === "promoted_attempt" ? { ...ch, measurement_id: "m-r01-final-a03" } : ch)) });
  const { model } = attemptModel("details", opId, (a) => a.attempt_id === "r01-a02", patch);
  assert.deepEqual(model.decision.champion.versions, ["v1"]);
  assert.deepEqual(model.decision.champion.measurements, [{ version: "v1", measurementId: "m-r01-final-a03" }]);
  assert.deepEqual(gateLimits(model), [{ code: "observe_gate_not_enforced", scope: "Campaign" },
    { code: "observe_gate_not_enforced", scope: "Attempt r01-a02 · round 1" }]);
  const plain = attemptModel("details", opId, (a) => a.attempt_id === "r01-a02",
    (c) => ({ ...c, gate_mode: "statistical", caveats: [] })).model;
  assert.deepEqual(gateLimits(plain), []);                                                    // a statistical gate is no limit
});

// Final review M3 / I1, ruling R3: the H1 has no gate pill, so the fail-closed gate marks reach the page only as
// limits. The campaign's observe/unknown mark is on every measurement page (as in Result); an attempt adds its own.
test("family 14: gate marks are limits on every measurement page; an unknown gate fails closed", () => {
  const opId = ids("details").campaign_mixed;
  const { campaign } = FIELDS("details", opId);
  const baseline = campaign.rounds.find((r) => r.baseline_measurement_id).baseline_measurement_id;
  const asChampion = (c, patch) => ({ ...structuredClone(c), ...patch,
    champions: c.champions.map((ch) => (ch.origin === "promoted_attempt" ? { ...ch, measurement_id: baseline } : ch)) });
  const unknownGate = { gate_mode: null, caveats: [], details_state: "unavailable", details_reason: "unreadable" };
  const unknownMark = label("gate_mark", "unknown").text;
  // A promoted champion's measurement (no attempt): observe campaign, then unknown gate.
  const observe = modelFor("details", opId, asChampion(campaign, { gate_mode: "observe", caveats: [] }), baseline);
  assert.equal(observe.attempt, null);
  assert.deepEqual(gateLimits(observe), [{ code: "observe_gate_not_enforced", scope: "Campaign" }]);
  const unknown = modelFor("details", opId, asChampion(campaign, unknownGate), baseline);
  assert.deepEqual(gateLimits(unknown), [{ code: "unknown", scope: "Campaign" }]);
  assert.equal(unknown.caveats.find((c) => c.code === "unknown").text, unknownMark);
  // The attempt page of a family-14 campaign with a null gate mode: Campaign and Attempt scopes both fail closed.
  const attempt = attemptModel("details", opId, (a) => a.attempt_id === "r01-a02", (c) => ({ ...c, ...unknownGate })).model;
  assert.deepEqual(gateLimits(attempt), [{ code: "unknown", scope: "Campaign" }, { code: "unknown", scope: "Attempt r01-a02 · round 1" }]);
  // A statistical gate is no limit, on a champion's page or an attempt's.
  assert.deepEqual(gateLimits(modelFor("details", opId, asChampion(campaign, { gate_mode: "statistical", caveats: [] }), baseline)), []);
  // M1: the same campaign limit on a plain baseline and an attempt, with or without a campaign-scope caveat (deduplicated).
  for (const caveats of [[], [{ kind: "observe_gate_not_enforced", round: null, attempt_id: null }]]) {
    const plainBaseline = modelFor("details", opId, { ...structuredClone(campaign), gate_mode: "observe", caveats }, baseline);
    assert.deepEqual(gateLimits(plainBaseline), [{ code: "observe_gate_not_enforced", scope: "Campaign" }]);
  }
  const { model } = attemptModel("details", opId, (a) => a.attempt_id === "r01-a02", (c) => ({ ...c,
    gate_mode: "observe", caveats: [{ kind: "observe_gate_not_enforced", round: 1, attempt_id: "r01-a02" }] }));
  assert.deepEqual(gateLimits(model), [{ code: "observe_gate_not_enforced", scope: "Campaign" },
    { code: "observe_gate_not_enforced", scope: "Attempt r01-a02 · round 1" }]);
  // Dead fields are gone: nothing renders a gate pill.
  assert.equal(Object.hasOwn(model, "gate"), false);
  assert.equal(Object.hasOwn(model.attempt, "gate"), false);
});
test("family 14: confidence and campaign/round/attempt caveats keep their scopes without cohort roles", () => {
  const op = ids("family14").campaign_final;
  const campaign = structuredClone(body("family14", `/v1/operations/${op}/campaign`));
  const operation = body("family14", `/v1/operations/${op}`);
  const round = campaign.rounds.find((r) => r.attempts.some((a) => a.confidence?.latest && a.measurement_id));
  const own = round.attempts.find((a) => a.confidence?.latest && a.measurement_id);
  const measurement = body("family14", `/v1/operations/${op}/measurements/${own.measurement_id}`);
  const original = measurementModel({ operation, campaign, measurement, diff: null, measurementId: own.measurement_id });
  assert.equal(original.confidence.pending, false);
  assert.equal(original.confidence.observe, own.confidence.mode === "observe");
  assert.equal(original.confidence.focus.point, own.confidence.latest.focus.point);
  assert.equal(original.confidence.look, own.confidence.latest.look);
  assert.deepEqual(original.confidence.cases, { focus: own.confidence.focus_cases, causal: own.confidence.causal_cases });
  assert.equal(original.attempt.look, own.look ?? "—");
  assert.deepEqual(original.header, {
    against: `against ${round.baseline_version} · ${round.baseline_measurement_id}`, at: original.header.at });
  assert.match(original.header.at, /^\d\d:\d\d:\d\d$/);
  assert.equal(Object.hasOwn(original.attempt, "alpha"), false);
  assert.equal(Object.hasOwn(original, "cohortMember"), false);
  campaign.caveats = [
    { kind: "observe_gate_not_enforced", round: null, attempt_id: null },
    { kind: "uncalibrated_point_estimates", round: null, attempt_id: null },
    { kind: "noise_allowance_unavailable", round: round.round, attempt_id: null },
    { kind: "future_caveat", round: round.round, attempt_id: own.attempt_id },
    { kind: "other_attempt", round: round.round, attempt_id: "different" },
    { kind: "wrong_round", round: round.round + 1, attempt_id: own.attempt_id }];
  const bound = own.confidence;
  for (const confidence of [null, { ...bound, latest: null }]) {
    own.confidence = confidence;
    const model = measurementModel({ operation, campaign, measurement, diff: null, measurementId: own.measurement_id });
    assert.deepEqual(model.caveats.map((c) => c.code), ["observe_gate_not_enforced", "uncalibrated_point_estimates",
      "noise_allowance_unavailable", "future_caveat"]);
    assert.equal(model.caveats[2].scope, `Round ${round.round}`);
    assert.equal(model.caveats.at(-1).text, "future_caveat");
    assert.equal(model.caveats.at(-1).scope, `Attempt ${own.attempt_id} · round ${round.round}`);
    assert.equal(model.confidence?.pending ?? null, confidence === null ? null : true);
  }
  const baselineId = round.baseline_measurement_id;
  const baseline = modelFor("family14", op, campaign, baselineId);
  assert.equal(baseline.header.against, null);
  assert.equal(baseline.decision, null);
  assert.deepEqual(baseline.caveats.map((c) => c.code),
    ["observe_gate_not_enforced", "uncalibrated_point_estimates", "noise_allowance_unavailable"]);
  assert.equal(baseline.attempt, null);
  assert.equal(Object.hasOwn(baseline, "cohortMember"), false);
  for (const [schema, family] of [["lab-campaign-view/8", 14], ["lab-campaign-view/9", 14], ["lab-campaign-view/10", 14], ["lab-campaign-view/14", 14], ["lab-campaign-view/15", 14], ["lab-campaign-view/6", 14], ["lab-campaign-view/7", 14],
    ["lab-campaign-view/15", 10], ["lab-campaign-view/15", 13], ["lab-campaign-view/15", null]]) {
    const unsupported = { schema, family, operation_id: "wrong", rounds: {}, champions: {}, caveats: {}, progress: 5 };
    const model = measurementModel({ operation, campaign: unsupported, measurement, measurementId: own.measurement_id });
    assert.deepEqual(model.schema, { ok: false, code: "unsupported_schema" });
    assert.deepEqual(model.caveats, []);
    assert.equal(model.attempt, null);
    assert.equal(model.confidence, null);
    assert.equal(diffPath(unsupported, own, op), null);
  }
});

test("measurement labels preserve every final and refresh relationship without changing role", () => {
  const op = ids("family14").campaign_final;
  const c = structuredClone(get("family14", `/v1/operations/${op}/campaign`));
  const mid = c.rounds[0].attempts.find((a) => a.measurement_id).measurement_id;
  const rawRole = get("family14", `/v1/operations/${op}/measurements/${mid}`).measurement.role;
  const entry = structuredClone(c.rounds.find((r) => r.final?.length).final[0]);
  entry.members[0].measurement_id = mid; entry.stored_measurement_id = mid;
  c.rounds[0].final = [{ ...entry, ordinal: 4 }, { ...entry, ordinal: 8 }];
  c.settings.baseline_refresh.performed = [{ round: 3, measurement_id: mid, completed: false }, { round: 6, measurement_id: mid, completed: true }];
  const m = modelFor("family14", op, c, mid);
  assert.equal(m.role, rawRole);
  assert.deepEqual(m.labels, ["Final of round 1, entry 4", "Final of round 1, entry 8",
    "Baseline refresh before round 3 · completed: false", "Baseline refresh before round 6 · completed: true"]);
  c.rounds.forEach((r) => { r.final = []; }); c.settings.baseline_refresh.performed = [];
  assert.deepEqual(modelFor("family14", op, c, mid).labels, []);
});

// Spec family14 §3.5, PDR-0017: gate, final decision and champion transition are three facts, each with its own empty state.
const f14 = (patch = (c) => c) => {
  const op = ids("family14").campaign_final;
  const campaign = patch(structuredClone(get("family14", `/v1/operations/${op}/campaign`)));
  const a = campaign.rounds[0].attempts[0];
  return { a, campaign, model: modelFor("family14", op, campaign, a.measurement_id) };
};

test("decision: three separate facts", () => {
  const { a, campaign, model } = f14();
  const promoted = campaign.champions.find((c) => c.origin === "promoted_attempt" && c.attempt_id === a.attempt_id);
  assert.equal(a.outcome, "finalist");
  assert.deepEqual(model.decision.gate, { text: "Finalist", tone: "blue", code: a.reason });
  assert.deepEqual(model.decision.final, { state: a.final_decision.state, text: "Accepted", tone: "green",
    reason: a.final_decision.reason, entries: ["Final of round 1, entry 1"] });
  assert.deepEqual(model.decision.champion, { kind: "promoted", versions: [promoted.version],
    measurements: [{ version: promoted.version, measurementId: promoted.measurement_id }] });
  // A finalist that core accepted but lineage does not record: no transition, the final decision unchanged.
  const none = f14((c) => ({ ...c, champions: c.champions.filter((ch) => ch.origin !== "promoted_attempt") })).model.decision;
  assert.deepEqual(none.champion, { kind: "none" });
  assert.equal(none.final.state, "accepted");
  // An unknown gate outcome: its raw code in a neutral pill; an undecided gate is empty.
  const odd = f14((c) => { c.rounds[0].attempts[0].outcome = "future_outcome"; return c; }).model.decision;
  assert.deepEqual(odd.gate, { text: "future_outcome", tone: "neutral", code: a.reason });
  const open = f14((c) => { Object.assign(c.rounds[0].attempts[0], { outcome: null, reason: null, final_decision: null }); return c; }).model.decision;
  assert.equal(open.gate, null);
  assert.equal(open.final, null);
});

// Final review item 12: several promoted versions of one attempt — each version keeps its own lineage measurement
// (none is chosen as "the" champion measurement); one version keeps the plain caption.
test("decision: several promoted versions show each version's measurement", async () => {
  const { DecisionPanel } = await staticModule("ui/confidence.js");
  const { a, model } = f14((c) => {
    const p = c.champions.find((ch) => ch.origin === "promoted_attempt" && ch.attempt_id === c.rounds[0].attempts[0].attempt_id);
    c.champions.push({ ...structuredClone(p), version: "v9", measurement_id: "m-later" });
    return c;
  });
  const first = f14().campaign.champions.find((ch) => ch.origin === "promoted_attempt" && ch.attempt_id === a.attempt_id);
  assert.deepEqual(model.decision.champion.measurements,
    [{ version: first.version, measurementId: first.measurement_id }, { version: "v9", measurementId: "m-later" }]);
  const many = inspect(DecisionPanel({ decision: model.decision })).text;
  assert.ok(many.includes(`from lineage · ${first.version} ${first.measurement_id} · v9 m-later`), many);
  const one = inspect(DecisionPanel({ decision: f14().model.decision })).text;
  assert.ok(one.includes(`from lineage · ${first.measurement_id}`) && !one.includes(`${first.version} ${first.measurement_id}`), one);
});

test("decision: lineage unavailable", () => {
  for (const state of ["unavailable", "not_started"]) {
    const { model } = f14((c) => ({ ...c, champions: null, champions_state: state }));
    assert.deepEqual(model.decision.champion, { kind: "unavailable", reason: state });
    assert.equal(model.decision.final.state, "accepted");                   // the other facts stay
  }
});

test("decision: finalist without final decision shows the reason", () => {
  const without = (reason) => f14((c) => { c.rounds[0].attempts[0].final_decision = null; c.details_reason = reason; return c; }).model.decision;
  assert.deepEqual(without("unreadable").final, { missing: "unreadable" });
  assert.deepEqual(without(null).final, { missing: "unavailable" });
  const pending = f14((c) => { c.rounds[0].attempts[0].final_decision = { state: "pending", reason: null }; c.rounds[0].final = []; return c; });
  assert.deepEqual(pending.model.decision.final, { state: "pending", text: "Pending", tone: "blue", reason: null, entries: [] });
  // Owner's other states, and an unknown one as its raw code.
  const finalWith = (state) => f14((c) => { c.rounds[0].attempts[0].final_decision = { state, reason: "r" }; return c; })
    .model.decision.final;
  assert.deepEqual([finalWith("rejected").text, finalWith("rejected").tone], ["Not accepted", "neutral"]);
  assert.deepEqual([finalWith("unavailable").text, finalWith("unavailable").tone], ["Unavailable", "amber"]);
  assert.deepEqual([finalWith("future_state").text, finalWith("future_state").tone], ["future_state", "neutral"]);
  const rejected = f14((c) => { Object.assign(c.rounds[0].attempts[0], { outcome: "rejected", reason: "point_not_improved", final_decision: null }); return c; });
  assert.equal(rejected.model.decision.final, null);                         // not a finalist: no final step
  assert.deepEqual(rejected.model.decision.gate, { text: "Rejected", tone: "neutral", code: "point_not_improved" });
  // As attempt_group not_enough_data (lib/labels.js): amber, with the raw reason still shown.
  const thin = f14((c) => { Object.assign(c.rounds[0].attempts[0], { outcome: "insufficient_evidence", reason: "insufficient_repeats",
    final_decision: null }); return c; });
  assert.deepEqual(thin.model.decision.gate, { text: "Not enough data", tone: "amber", code: "insufficient_repeats" });
});
