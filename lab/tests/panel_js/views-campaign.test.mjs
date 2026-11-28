import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";
import { body, currentForm, inspect, snapshot, staticModule } from "./support.mjs";

const { campaignEntry, campaignModel, contrastDecision, screenOf } = await staticModule("lib/views/campaign.js");
const { attemptFor, diffPath } = await staticModule("lib/views/measurement.js");
const { pairsState } = await staticModule("lib/views/diff.js");
const { readOf } = await staticModule("lib/resource.js");
const { label } = await staticModule("lib/labels.js");
const { tokens } = await staticModule("lib/format.js");
const { okEntry } = await staticModule("lib/resource.js");
const { StatusCard } = await staticModule("ui/status-card.js");
const { statusCardOf } = await staticModule("lib/views/status-card.js");
const ids = (name) => snapshot(name).meta.ids;
const attemptRows = (r) => r.rows.filter((x) => x.kind === "attempt");
const finalRows = (r) => r.rows.filter((x) => x.kind === "final");
const shape = (r) => r.rows.map((x) => (x.kind === "attempt" ? `a:${x.id}` : x.kind === "final" ? `f:${x.ordinal}` : `p:${x.attemptId}`));

test("notice fixture adaptation preserves campaign rows and current /3 evidence", () => {
  for (const schema of ["lab-campaign-view/15", "lab-measurement/3", "lab-measurement-list/3"]) {
    const input = schema === "lab-measurement/3" ? {schema, measurement: {role: "baseline"}}
      : {schema, measurements: [{role: "baseline"}]};
    assert.deepEqual(currentForm(input), input);
  }
  for (const schema of ["lab-measurement/2", "lab-measurement-list/2"]) {
    const input = schema === "lab-measurement/2" ? {schema, measurement: {role: "evaluation"}}
      : {schema, measurements: [{role: "evaluation"}]};
    const original = structuredClone(input), adapted = currentForm(input);
    assert.equal(adapted.schema, schema.replace("/2", "/3"));
    assert.deepEqual(adapted.measurement ?? adapted.measurements[0],
      {role: "evaluation", lab_notices: [], lab_notices_state: "unavailable"});
    assert.deepEqual(input, original);
  }
});

function load(name, id, patch = (x) => x) {
  const operation = body(name, `/v1/operations/${id}`);
  const campaign = patch({ ...structuredClone(body(name, `/v1/operations/${id}/campaign`)), lab_notices_state: "present" });
  const measurements = {};
  for (const m of campaign.measurements) {
    const key = `/v1/operations/${id}/measurements/${m.measurement_id}`;
    if (snapshot(name).routes[key]?.status === 200) measurements[m.measurement_id] = okEntry(body(name, key));
  }
  return campaignModel({ operation, campaign, measurements });
}

test("routing: evaluation, suspended campaign, campaign", () => {
  const s = ids("suspended-role");
  assert.equal(screenOf(body("suspended-role", `/v1/operations/${s.evaluation}`)), "evaluation");
  assert.equal(screenOf(body("suspended-role", `/v1/operations/${s.campaign_suspended_role}`)), "suspended");
  assert.equal(screenOf(body("suspended-role", `/v1/operations/${s.campaign_resumed}`)), "campaign");
});

test("mixed round: one row per attempt, groups from Lab, failure codes shown", () => {
  const id = ids("details").campaign_mixed;
  const view = body("details", `/v1/operations/${id}/campaign`);
  const model = load("details", id);
  const round = attemptRows(model.rounds[0]);
  assert.deepEqual(round.map((a) => a.id), view.rounds[0].attempts.map((a) => a.attempt_id));
  view.rounds[0].attempts.forEach((a, i) => assert.deepEqual(round[i].group, label("attempt_group", a.group)));
  const failed = view.rounds[0].attempts.findIndex((a) => a.failure);
  assert.equal(failed >= 0, true);
  assert.match(round[failed].failure, new RegExp(view.rounds[0].attempts[failed].failure.stage));
});

test("lineage: one row per champion, mean from its measurement, diff link except v0", () => {
  const id = ids("details").campaign_finished;
  const view = body("suspended-target", `/v1/operations/${id}/campaign`);
  const model = load("suspended-target", id);
  assert.deepEqual(model.lineage.rows.map((r) => r.version), view.champions.map((c) => c.version));
  assert.equal(model.lineage.rows[0].diffHref, null);
  assert.match(model.lineage.rows[1].diffHref, /from=initial&to=champion:v1$/);
});

test("tokens: a sum over measurements, labelled partial", () => {
  const id = ids("details").campaign_mixed;
  const view = body("details", `/v1/operations/${id}/campaign`);
  const sum = view.measurements.reduce((n, m) => n + (m.usage_state === "present" ? m.usage.input_tokens : 0), 0);
  const cell = load("details", id).cells.find((c) => c.name === "Target tokens");
  assert.equal(cell.value.startsWith(`${tokens(sum)} in`), true);
  assert.match(cell.sub, /Partial: target calls only/);
});

test("details unavailable: rounds stay, the reason is carried", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => { c.rounds.forEach((r) => r.attempts.forEach((a) => { a.failure = null; })); return { ...c, details_state: "unavailable", details_reason: "core_raw_unsupported" }; });
  assert.equal(model.rounds.length > 0, true);
  assert.deepEqual(model.details, { state: "unavailable", reason: "core_raw_unsupported" });
});

test("strip: a measurement whose read failed is shown with its code, others keep cells", () => {
  const id = ids("details").campaign_mixed;
  const operation = body("details", `/v1/operations/${id}`);
  const campaign = body("details", `/v1/operations/${id}/campaign`);
  const [first, ...rest] = campaign.measurements.map((m) => m.measurement_id);
  const measurements = { [first]: { state: "error", data: undefined, code: "results_deleted", stale: false } };
  for (const m of rest) measurements[m] = okEntry(body("details", `/v1/operations/${id}/measurements/${m}`));
  const model = campaignModel({ operation, campaign, measurements });
  assert.deepEqual(model.strip.rows[0].read, { state: "unavailable", code: "results_deleted", stale: false });
  assert.equal(model.strip.rows[1].cells.length > 0, true);
});

test("an unknown attempt group keeps its raw code", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => { c.rounds[0].attempts[0].group = "brand_new"; return c; });
  assert.equal(attemptRows(model.rounds[0])[0].group.text, "brand_new");
});

test("lineage: champions_state unavailable is carried, not an empty list of a present one", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => ({ ...c, champions: null, champions_state: "unavailable" }));
  assert.deepEqual(model.lineage, { state: "unavailable", rows: [] });
});

test("tokens: usage present on some measurements only says so", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => { c.measurements[0].usage_state = "unavailable"; c.measurements[0].usage = null; return c; });
  const cell = model.cells.find((c) => c.name === "Target tokens");
  const total = body("details", `/v1/operations/${id}/campaign`).measurements.length;
  assert.equal(cell.sub.includes(`usage from ${total - 1} of ${total} measurements`), true);
});

test("header last event: present -> time; not_started / unavailable -> section-state text with the reason", () => {
  const id = ids("details").campaign_mixed;
  const operation = body("details", `/v1/operations/${id}`);
  const campaign = body("details", `/v1/operations/${id}/campaign`);
  const header = (patch) => campaignModel({ operation: { ...operation, ...patch }, campaign, measurements: {} }).header.lastEvent;
  assert.notEqual(header({}), "Not started");
  assert.equal(header({ times_state: "not_started", times: null, times_reason: null }), "Not started");
  assert.equal(header({ times_state: "unavailable", times: null, times_reason: "unreadable" }), "Unavailable · Unreadable");
});

// Codex r1: baseline_version comes with the details read — while it is unavailable the title says so.
test("details unavailable: a round's baseline is named unavailable, not '—'", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => {
    c.details_state = "unavailable"; c.details_reason = "unreadable";
    for (const r of c.rounds) r.baseline_version = null;
    return c;
  });
  const text = label("section_state", "unavailable").text;
  assert.equal(model.rounds[0].header.title, `Round ${model.rounds[0].round}`);
  assert.equal(model.rounds[0].header.baseline, text);
  const served = load("details", id);
  assert.match(served.rounds[0].header.baseline, /^v\d+$/);
});

test("observe: campaign, its caveated attempt and promoted champions carry the neutral mark", () => {
  const id = ids("details").campaign_mixed;
  const view = body("details", `/v1/operations/${id}/campaign`);
  const attempt = view.rounds[0].attempts.find((a) => a.measurement_id);
  const observed = load("details", id, (c) => ({ ...c, gate_mode: "observe", caveats: [
    { kind: "observe_gate_not_enforced", round: null, attempt_id: null },
    { kind: "observe_gate_not_enforced", round: view.rounds[0].round, attempt_id: attempt.attempt_id }] }));
  // Final review item 2: one code, one text — the synthesised round mark and the lineage pill read as the caveat.
  const mark = label("caveat", "observe_gate_not_enforced");
  assert.equal(mark.tone, "neutral");
  assert.equal(mark.text, "Observe gate not enforced");
  assert.equal(Object.hasOwn(observed.header, "gate"), false);
  assert.equal(observed.result.limits[0].code, "observe_gate_not_enforced");  // the campaign caveat itself, no second mark
  // round and attempt marks are limits in their own rows (spec family14 §4.2), not pills
  const r0 = observed.rounds[0];
  assert.deepEqual(r0.header.limits, [{ code: "observe_gate_not_enforced", text: mark.text, scope: `Round ${r0.round}` }]);
  attemptRows(r0).forEach((a) => assert.equal(a.limits.some((l) => l.code === "observe_gate_not_enforced"), a.id === attempt.attempt_id));
  assert.equal(Object.hasOwn(attemptRows(r0)[0], "gate"), false);
  assert.deepEqual(attemptRows(r0).find((a) => a.id === attempt.attempt_id).group, label("attempt_group", attempt.group));
  observed.lineage.rows.forEach((row, i) => assert.deepEqual(row.gate,
    view.champions[i].origin === "promoted_attempt" ? mark : null));
});

test("observe: the campaign-level caveat alone marks the campaign, not a round nor an attempt", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => ({ ...c, gate_mode: "observe",
    caveats: [{ kind: "observe_gate_not_enforced", round: null, attempt_id: null }] }));
  assert.deepEqual(model.result.limits.map((l) => l.code), ["observe_gate_not_enforced"]);
  assert.equal(model.rounds.every((r) => r.header.limits.length === 0 && attemptRows(r).every((a) => a.limits.length === 0)), true);
  const statistical = load("details", id, (c) => ({ ...c, gate_mode: "statistical",
    caveats: [{ kind: "uncalibrated_point_estimates", round: null, attempt_id: null }] }));
  assert.equal(statistical.result.limits.some((l) => l.code === "statistical" || l.text === label("gate_mark", "statistical").text), false);
  assert.equal(statistical.lineage.rows.every((r) => r.gate === null), true);
});

// Final review I1: family 14 with no gate mode (its caveats were unreadable) is an unknown gate — every campaign,
// round, attempt and promoted champion carries the neutral "gate unknown" mark, never a bare green pill.
test("gate unknown: family 14 without a gate mode marks campaign, rounds, attempts and promoted champions", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => ({ ...c, gate_mode: null, caveats: [],
    details_state: "unavailable", details_reason: "unreadable" }));
  const mark = label("gate_mark", "unknown");
  assert.equal(mark.tone, "neutral");
  assert.equal(mark.text, "Gate unknown — not evidence of quality");
  assert.equal(model.result.limits[0].text, mark.text);
  assert.equal(model.result.limits[0].code, "unknown");
  assert.equal(model.rounds.length > 0, true);
  const marked = (limits) => limits.some((l) => l.code === "unknown" && l.text === mark.text);
  assert.equal(model.rounds.every((r) => marked(r.header.limits) && attemptRows(r).every((a) => marked(a.limits))), true);
  const lineage = Object.fromEntries(model.lineage.rows.map((r) => [r.version, r.gate]));
  assert.equal(lineage.v0, null);
  assert.deepEqual(lineage.v1, mark);
  const unsupported = load("details", id, (c) => ({ ...c, family: 10, gate_mode: null, caveats: [] }));
  assert.deepEqual(unsupported.schema, { ok: false, code: "unsupported_schema" });
  assert.deepEqual(unsupported.result.limits, []);
});

test("view /7: a failed reflection shows its error class on the round pill", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => {
    c.rounds[0].reflection = { state: "failed", reflection_id: "x", error_class: "oracle_leak", reason: "oracle_leak", hypotheses: [] };
    return c;
  });
  assert.equal(model.rounds[0].header.reflection.text, "Failed · oracle_leak");
});

test("view /7: lab_notices become result notices", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => ({ ...c, lab_notices: [{ code: "no_candidate_measured", rounds: 2,
    by_reason: { reflection_failed: 2 }, reflection_errors: { oracle_leak: 1, reported: 1 } }] }));
  assert.deepEqual(model.result.notices, [{ code: "no_candidate_measured", known: true, text: "No candidate measured", rounds: 2,
    byReason: [["reflection_failed", 2]], reflectionErrors: [["oracle_leak", 1], ["reported", 1]] }]);
});

test("view /7: a notice with no rounds keeps its zero and empty counts", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => ({ ...c, lab_notices: [{ code: "no_candidate_measured", rounds: 0,
    by_reason: {}, reflection_errors: {} }] }));
  assert.equal(model.result.notices.length, 1);
  assert.deepEqual([model.result.notices[0].rounds, model.result.notices[0].byReason, model.result.notices[0].reflectionErrors], [0, [], []]);
});

for (const [rounds, reasons, explained] of [
  [5, { clean: 5 }, true],
  [4, { clean: 3, editor_failed: 1 }, false],
  [0, {}, false],
  [0, { clean: 0 }, false],
]) {
  test(`clean-focus explanation renders only with positive clean-only evidence: ${JSON.stringify(reasons)}`, () => {
    const id = ids("family14").campaign_final;
    const operation = body("family14", `/v1/operations/${id}`);
    const campaign = structuredClone(body("family14", `/v1/operations/${id}/campaign`));
    campaign.lab_notices = [{ code: "no_candidate_measured", rounds, by_reason: reasons, reflection_errors: {} }];
    const bytes = JSON.stringify(campaign);
    const model = campaignModel({ operation, campaign, measurements: {} });
    assert.deepEqual(model.schema, { ok: true, code: null });
    const card = statusCardOf(okEntry(operation), okEntry(campaign), { screen: "campaign" });
    const text = inspect(StatusCard({ card })).text;
    assert.equal(text.includes("the baseline had no failures"), explained);
    assert.equal(text.includes("the scorer supplied neither verdicts nor critical_pass"), explained);
    assert.equal(text.includes("Metrics do not build campaign focus"), explained);
    assert.equal(JSON.stringify(campaign), bytes);
    assert.equal(campaign.schema, "lab-campaign-view/15");
  });
}

test("view /7: core's outcome stands next to the notice (PDR-0025), not Owner's control state", () => {
  const id = ids("details").campaign_mixed;
  const model = load("details", id, (c) => ({ ...c, outcome: { outcome: "completed", reason: "rounds_exhausted" },
    lab_notices: [{ code: "no_candidate_measured", rounds: 1, by_reason: { reflection_failed: 1 }, reflection_errors: {} }] }));
  assert.deepEqual(model.result.outcome, { ...label("outcome", "completed"), code: "completed", reason: "rounds_exhausted" });
  assert.equal(model.result.notices.length, 1);
  const noReason = load("details", id, (c) => ({ ...c, outcome: { outcome: "stopped", reason: null } }));
  assert.deepEqual([noReason.result.outcome.text, noReason.result.outcome.reason], ["Stopped", null]);
  const none = campaignModel({ operation: undefined, campaign: undefined, measurements: {} }).result;
  assert.deepEqual([none.outcome, none.limits, none.notices], [null, [], []]);
});

test("result: core outcome with campaign limits and the observe mark", () => {
  const id = ids("family14").campaign_final;
  const { result } = load("family14", id);
  assert.equal(result.decided, true);
  assert.equal(result.pending, null);
  assert.equal(result.limits[0].code, "observe_gate_not_enforced");
  assert.equal(result.limits.length > 1, true);
  assert.equal(result.limits.every((l) => l.scope === "Campaign"), true);
  // no duplicate when the observe caveat is itself a campaign caveat
  assert.equal(result.limits.filter((l) => l.code === "observe_gate_not_enforced").length, 1);
});

test("result: running campaign is not decided yet and keeps its limits", () => {
  const id = ids("family14").campaign_final;
  const campaign = structuredClone(body("family14", `/v1/operations/${id}/campaign`));
  campaign.outcome = null;
  const resultFor = (patch) => campaignModel({ operation: { ...body("family14", `/v1/operations/${id}`), ...patch },
    campaign, measurements: {} }).result;
  const running = resultFor({ control_state: "running", resume: null });
  assert.deepEqual([running.decided, running.outcome, running.pending], [false, null, "Not decided yet · running"]);
  assert.equal(running.limits.length > 0, true);
  assert.equal(resultFor({ resume: { state: "ready" } }).pending, "Not decided yet · suspended");
  // Final review item 1: "running" only while Owner's control state is active. Owner sets outcome null whenever
  // Core has no campaign_outcome, so a closed operation without one says so, never "running".
  for (const state of ["reserved", "running", "stopping"])
    assert.equal(resultFor({ control_state: state, resume: null }).pending, "Not decided yet · running", state);
  for (const state of ["orphaned", "finished", "cleanup_required"]) {
    const closed = resultFor({ control_state: state, resume: null });
    assert.deepEqual([closed.decided, closed.outcome, closed.pending], [false, null, "No outcome recorded by Core"], state);
    assert.equal(closed.limits.length > 0, true);
  }
});

test("result: unknown notice code shown raw", () => {
  const id = ids("family14").campaign_final;
  const { result } = load("family14", id, (c) => ({ ...c, lab_notices: [{ code: "future_notice_code" }] }));
  assert.equal(result.notices.length, 1);
  assert.equal(result.notices[0].known, false);
  assert.equal(result.notices[0].text, "future_notice_code");
  assert.equal(load("family14", id, (c) => ({ ...c, lab_notices: [] })).result.notices.length, 0);
});

test("result: no_candidate_measured keeps its counts as recorded", () => {
  const id = ids("family14").campaign_final;
  const { result } = load("family14", id, (c) => ({ ...c, lab_notices: [{ code: "no_candidate_measured", rounds: 3,
    by_reason: { reflection_failed: 2, editor_error: 0 }, reflection_errors: { oracle_leak: 1 } }] }));
  const [n] = result.notices;
  assert.deepEqual([n.known, n.rounds, n.byReason, n.reflectionErrors],
    [true, 3, [["reflection_failed", 2], ["editor_error", 0]], [["oracle_leak", 1]]]);
});

test("result: observe without the campaign caveat leads its limits with the gate mark", () => {
  const id = ids("family14").campaign_final;
  const { result } = load("family14", id, (c) => ({ ...c, gate_mode: "observe",
    caveats: [{ kind: "uncalibrated_point_estimates", round: null, attempt_id: null }] }));
  assert.deepEqual(result.limits.map((l) => [l.code, l.text]), [
    ["observe_gate_not_enforced", label("caveat", "observe_gate_not_enforced").text],
    ["uncalibrated_point_estimates", "Uncalibrated point estimates"]]);
});

test("header: only Owner's status, one pill", () => {
  const id = ids("family14").campaign_final;
  const operation = body("family14", `/v1/operations/${id}`);
  const campaign = body("family14", `/v1/operations/${id}/campaign`);
  const status = (patch) => campaignModel({ operation: { ...operation, ...patch }, campaign, measurements: {} }).header.status;
  assert.equal(status({ control_state: "finished" }).text, "Finished");
  assert.equal(status({ control_state: "running", progress: { phase: "reflection" } }).text, "Running · Reflecting");
  assert.equal(status({ control_state: "running", progress: { phase: "reflection" } }).tone, "blue");
});

test("only /14 with family 14 or 15 is supported", () => {
  const id = ids("details").campaign_mixed;
  for (const schema of ["lab-campaign-view/4", "lab-campaign-view/5", "lab-campaign-view/6", "lab-campaign-view/7", "lab-campaign-view/8", "lab-campaign-view/9", "lab-campaign-view/10", "lab-campaign-view/11", "lab-campaign-view/12", "lab-campaign-view/13", "lab-campaign-view/14"]) {
    const model = load("details", id, (c) => ({ ...c, schema }));
    assert.deepEqual(model.schema, { ok: false, code: "unsupported_schema" });
  }
});

// Codex r1 #3: the schema is checked once, before any field is read. A newer view whose fields changed shape
// (here `measurements` is an object, `rounds` a map) must give Unavailable (unsupported_schema), never a throw.
const V13 = { schema: "lab-campaign-view/15", lab_notices_state: "present", family: 14, operation_id: "x", measurements: {}, rounds: {}, champions: {},
  lab_notices: {}, caveats: {}, outcome: "completed", progress: 5 };

test("a /13 view of another shape: the model reads none of its fields and does not throw", () => {
  const id = ids("details").campaign_mixed;
  const operation = body("details", `/v1/operations/${id}`);
  const model = campaignModel({ operation, campaign: structuredClone(V13), measurements: {} });
  assert.deepEqual(model.schema, { ok: false, code: "unsupported_schema" });
  assert.deepEqual([model.result.notices, model.rounds, model.strip.rows, model.lineage.rows], [[], [], [], []]);
  assert.equal(model.result.outcome, null);
  assert.deepEqual(model.result.limits, []);
  assert.equal(model.header.id, operation.id);
});

test("another schema or family is unavailable before any consumer sees it, including stale reads", () => {
  for (const campaign of [V13, { ...V13, schema: "lab-campaign-view/7" },
    ...[10, 13, 16, null].map((family) => ({ ...V13, schema: "lab-campaign-view/15", lab_notices_state: "present", family }))]) {
    const entry = campaignEntry(okEntry(campaign));
    assert.equal(entry.data, undefined);
    assert.deepEqual(readOf(entry), { state: "unavailable", code: "unsupported_schema", stale: false });
    // every campaign-view consumer reads `entry.data`: none gets the rejected object or fetches measurements
    assert.deepEqual(entry.data?.measurements?.map((m) => m.measurement_id) ?? [], []);
    assert.equal(attemptFor(entry.data, "m-1"), null);
    assert.equal(attemptFor(campaign, "m-1"), null);
    assert.equal(diffPath(campaign, { has_tree: true }, "x"), null);
    assert.deepEqual(pairsState(entry.data, "x").pairs.length >= 0, true);
    assert.equal(readOf(campaignEntry({ state: "error", data: campaign, code: "network_error", stale: true })).code,
      "unsupported_schema");
  }
  // loading and /10 family-14 answers pass unchanged
  const id = ids("details").campaign_mixed;
  const ok = okEntry(body("details", `/v1/operations/${id}/campaign`));
  assert.equal(campaignEntry(ok), ok);
  assert.equal(readOf(campaignEntry(undefined)).state, "loading");
});

test("family 14: statistical and observe header marks, confidence and caveats keep their supplied scope", () => {
  const id = ids("family14").campaign_final;
  const stat = load("family14", id, (c) => ({ ...c, gate_mode: "statistical", caveats: [] }));
  assert.equal(Object.hasOwn(stat.header, "gate"), false);
  // The format is a Campaign setup value (R6), not a header field (final review item 4).
  assert.equal(Object.hasOwn(stat.header, "family"), false);
  assert.equal(stat.setup.settings.find((s) => s.name === "Format").value, "manifest /15 · family 14");
  assert.equal(stat.rounds.some((r) => Object.hasOwn(r, "cohort")), false);
  // ruling R2: attempt rows carry no confidence (it lives on the Measurement screen)
  assert.equal(stat.rounds.flatMap(attemptRows).length > 0, true);
  assert.equal(stat.rounds.flatMap(attemptRows).some((a) => Object.hasOwn(a, "confidence")), false);
  const obs = load("family14", id);
  assert.equal(obs.result.limits[0].code, "observe_gate_not_enforced");
  const view = body("family14", `/v1/operations/${id}/campaign`);
  assert.deepEqual(obs.result.limits.map((c) => c.code), view.caveats.filter((c) => c.round === null && c.attempt_id === null).map((c) => c.kind));
  assert.equal(obs.result.limits.every((c) => c.scope === "Campaign"), true);

  const bound = view.rounds.flatMap((r) => r.attempts).find((a) => a.confidence).confidence;
  for (const confidence of [null, { ...bound, latest: null }]) {
    const model = load("family14", id, (c) => {
      const r = c.rounds.find((r) => r.attempts.length > 0);
      const a = r.attempts[0];
      a.confidence = confidence;
      r.attempts.push({ ...a, attempt_id: "other-attempt", measurement_id: null });
      c.caveats = [
        { kind: "noise_allowance_unavailable", round: r.round, attempt_id: null },
        { kind: "intervals_empirical", round: r.round, attempt_id: a.attempt_id },
        { kind: "future_caveat", round: r.round, attempt_id: a.attempt_id },
        { kind: "wrong_round", round: r.round + 1, attempt_id: a.attempt_id }];
      return c;
    });
    const r = model.rounds.find((r) => attemptRows(r).length > 0);
    const rows = attemptRows(r);
    assert.deepEqual(model.result.limits.filter((l) => l.code !== "observe_gate_not_enforced"), []);
    assert.deepEqual(r.header.limits.map((c) => c.code), ["noise_allowance_unavailable"]);
    assert.equal(r.header.limits[0].scope, `Round ${r.round}`);
    assert.equal(Object.hasOwn(r, "cohort"), false);
    assert.deepEqual(rows[0].limits.map((c) => c.code), ["intervals_empirical", "future_caveat"]);
    assert.equal(rows[0].limits[1].text, "future_caveat");
    assert.equal(rows[0].limits[1].scope, `Attempt ${rows[0].id} · round ${r.round}`);
    assert.deepEqual(rows.at(-1).limits, []);
  }
  for (const family of [10, 13, 16, null]) {
    const unsupported = campaignModel({ campaign: { schema: "lab-campaign-view/15", lab_notices_state: "present", family,
      rounds: {}, measurements: {}, caveats: {}, lab_notices: {} }, measurements: {} });
    assert.deepEqual(unsupported.schema, { ok: false, code: "unsupported_schema" });
    assert.deepEqual([unsupported.result.limits, unsupported.rounds, unsupported.strip.rows], [[], [], []]);
  }
});

test("failure text carries the role's own reason", async () => {
  const { failureText } = await staticModule("lib/views/campaign.js");
  assert.equal(failureText({ stage: "editor", role: "editor", error_class: "provider", role_reason: "http_402", code: null }),
               "editor · editor · provider · http_402");
});


test("rounds: a final row never shows a champion transition", () => {
  const id = ids("family14").campaign_final;
  const model = load("family14", id, (c) => {
    const r = c.rounds.find((r) => r.final?.length);
    r.final.push({ ...structuredClone(r.final[0]), ordinal: 9, selection: null });
    return c;
  });
  const r = model.rounds.find((r) => finalRows(r).length);
  const finals = finalRows(r);
  assert.deepEqual(finals.map((f) => f.ordinal), [1, 9]);
  assert.equal(Object.hasOwn(r, "finalStub"), false);
  const raw = body("family14", `/v1/operations/${id}/campaign`).rounds.find((r) => r.final?.length).final[0];
  const d = finals[0].detail;
  for (const [k, v] of Object.entries({ stability: raw.stability, championSide: raw.champion_side, storedMeasurementId: raw.stored_measurement_id,
    mode: raw.mode, global: raw.global, listed: raw.listed, conditionChangedInPlace: raw.condition_changed_in_place }))
    assert.deepEqual(d[k], v, k);
  assert.equal(d.nextBaseline.id, raw.next_baseline_measurement_id);
  assert.deepEqual(d.members.map(({ measurementHref, ...m }) => m), raw.members);
  assert.deepEqual(d.contrasts, raw.contrasts);
  assert.match(d.members[0].measurementHref, /measurement/);
  // the lineage promoted this finalist, yet only its attempt row says → v1 (spec family14 §4.4, PDR-0017)
  for (const f of finals) {
    assert.equal(Object.hasOwn(f, "champion"), false);
    assert.equal(JSON.stringify(f).includes("→"), false);
  }
  assert.equal(attemptRows(r)[0].champion.text, "→ v1");
});

test("rounds: an opened final shows the entry's stability and condition-changed flag, false and null included", async () => {
  const { FinalDetail } = await staticModule("ui/rounds.js");
  const id = ids("family14").campaign_final;
  const text = (patch) => {
    const model = load("family14", id, (c) => { Object.assign(c.rounds.find((r) => r.final?.length).final[0], patch); return c; });
    return inspect(FinalDetail({ d: finalRows(model.rounds.find((r) => finalRows(r).length))[0].detail })).text.replace(/\s+/g, " ");
  };
  assert.match(text({ stability: "stable", condition_changed_in_place: false }), /Stability stable · Condition changed in place no/);
  assert.match(text({ stability: null, condition_changed_in_place: null }), /Stability — · Condition changed in place —/);
  assert.match(text({ condition_changed_in_place: true }), /Condition changed in place yes/);
});

test("setup: summary carries every field and no warning when compatible", () => {
  const id = ids("family14").campaign_final;
  const raw = body("family14", `/v1/operations/${id}/campaign`);
  const s = load("family14", id).setup;
  assert.equal(s.state, "present");
  assert.equal(s.warn, null);
  assert.deepEqual(s.summary.map((x) => x.name), ["Stability", "Round stop", "Baseline refresh", "Roles", "Connection"]);
  assert.equal(s.summary[0].value, raw.settings.stability);
  assert.equal(s.summary[1].value, raw.settings.round_stop);
  assert.equal(s.summary[2].value, `every ${raw.settings.baseline_refresh.every_rounds} rounds · count ${raw.settings.baseline_refresh.count} · 0 recorded · 0 completed`);
  assert.equal(s.summary[3].value, `editor ${raw.roles.editor.kind} · reflector ${raw.roles.reflector.kind} · grouper ${raw.roles.grouper.kind}`);
  assert.equal(s.summary[4].value, `${raw.connection.state} · ${raw.connection.attempts} ${raw.connection.attempts === 1 ? "attempt" : "attempts"}`);
  // the campaign format is the first Settings row (ruling R6)
  assert.deepEqual(s.settings[0], { name: "Format", value: "manifest /15 · family 14" });
  assert.deepEqual(s.roles.map((r) => r.name), ["editor", "reflector", "grouper"]);
  assert.equal(s.connection.state, raw.connection.state);
  assert.deepEqual(s.connection.detail, []);
  assert.deepEqual(s.refresh.rows, []);
  assert.equal(s.refresh.count, raw.settings.baseline_refresh.count);
  assert.equal(s.refresh.records, 0);
  // null is shown as recorded, not dropped
  const nulled = load("family14", id, (c) => ({ ...c, settings: { ...c.settings, stability: null } })).setup;
  assert.equal(nulled.summary[0].value, "null");
});

test("setup: incompatible connection and an incomplete refresh raise the warning", () => {
  const id = ids("family14").campaign_final;
  const s = load("family14", id, (c) => {
    c.connection.state = "incompatible";
    c.settings.baseline_refresh.performed = [
      { round: 3, measurement_id: "m-a", replaced_measurement_id: "m-0", completed: true },
      { round: 6, measurement_id: "m-b", replaced_measurement_id: "m-a", completed: false }];
    return c;
  }).setup;
  assert.equal(s.warn, "connection not compatible · 1 refresh not completed");
  assert.equal(s.refresh.records, 2);
  assert.equal(s.refresh.completed, 1);
  assert.equal(s.refresh.count, 1);  // as recorded, apart from the records
  assert.deepEqual(s.refresh.rows.map((r) => [r.round, r.measurementId, r.replacedId, r.completed]),
    [[3, "m-a", "m-0", true], [6, "m-b", "m-a", false]]);
  assert.match(s.refresh.rows[0].measurementHref, /measurements\/m-a/);
  const only = load("family14", id, (c) => {
    c.settings.baseline_refresh.performed = [{ round: 6, measurement_id: "m-b", replaced_measurement_id: null, completed: false }];
    return c;
  }).setup;
  assert.equal(only.warn, "1 refresh not completed");
  assert.equal(only.refresh.rows[0].replacedHref, null);
});

test("setup: one connection attempt reads in the singular", () => {
  const id = ids("family14").campaign_final;
  const value = (n) => load("family14", id, (c) => { c.connection.attempts = n; return c; }).setup.summary.at(-1).value;
  assert.match(value(1), / · 1 attempt$/);
  assert.match(value(2), / · 2 attempts$/);
  assert.match(value(0), / · 0 attempts$/);
});

// Task 10 item 7: a round Core has not decided shows "In progress" only while the operation runs; otherwise no
// pill at all, never an empty "—".
test("rounds: a round without an outcome is in progress while running, else carries no outcome pill", () => {
  const id = ids("family14").campaign_final;
  const operation = body("family14", `/v1/operations/${id}`);
  const campaign = structuredClone(body("family14", `/v1/operations/${id}/campaign`));
  campaign.rounds.at(-1).outcome = null;
  const header = (state) => campaignModel({ operation: { ...operation, control_state: state }, campaign, measurements: {} }).rounds.at(-1).header;
  assert.deepEqual(header("running").outcome, { text: "In progress", tone: "blue", known: true });
  for (const state of ["finished", "suspended", undefined]) assert.equal(header(state).outcome, null, state);
  const decided = campaignModel({ operation: { ...operation, control_state: "running" },
    campaign: body("family14", `/v1/operations/${id}/campaign`), measurements: {} }).rounds.at(-1).header;
  assert.equal(decided.outcome.text, label("round_outcome", body("family14", `/v1/operations/${id}/campaign`).rounds.at(-1).outcome).text);
});

// Read states (additions mock "Read states"; spec §3.9): a block shown from a kept previous answer is dimmed with
// .is-stale; a fresh one is not. Result and Campaign setup read the campaign view.
test("result and setup: a stale campaign read dims the block, a fresh one does not", async () => {
  const { CampaignSetup } = await staticModule("ui/setup.js");
  const model = load("family14", ids("family14").campaign_final);
  const rootClass = (node) => String(inspect(node).elements[0].props.class).split(" ");
  const id = ids("family14").campaign_final;
  const operation = body("family14", `/v1/operations/${id}`);
  const campaign = body("family14", `/v1/operations/${id}/campaign`);
  const render = (entry) => inspect(StatusCard({ card: statusCardOf(okEntry(operation), entry, { screen: "campaign" }) }));
  assert.ok(render({ ...okEntry(campaign), stale: true }).elements.some((node) => node.props?.class === "is-stale"));
  assert.ok(!render(okEntry(campaign)).elements.some((node) => node.props?.class === "is-stale"));
  assert.ok(rootClass(CampaignSetup({ setup: model.setup, stale: true })).includes("is-stale"));
  assert.ok(!rootClass(CampaignSetup({ setup: model.setup, stale: false })).includes("is-stale"));
});

// The page-level read in place of the new blocks: deletion is its own state, not "Unavailable".
test("block read state: loading, unavailable with its code, deleted", async () => {
  const { BlockReadState } = await staticModule("ui/pill.js");
  const text = (read) => inspect(BlockReadState({ read, loading: "Loading the campaign…" })).text;
  assert.equal(text({ state: "loading", code: null, stale: false }), "Loading the campaign…");
  assert.equal(text({ state: "unavailable", code: "campaign_unreadable", stale: false }), "Unavailable (campaign_unreadable)");
  assert.ok(text({ state: "unavailable", code: "results_deleted", stale: false }).startsWith("Results deleted"));
  assert.ok(text({ state: "unavailable", code: "results_deleted", stale: false }).includes("results_deleted"));
  assert.equal(BlockReadState({ read: { state: "ok", code: null, stale: true } }), null);
});

test("setup: details unavailable says so", () => {
  const id = ids("family14").campaign_final;
  const s = load("family14", id, (c) => ({ ...c, details_state: "unavailable", details_reason: "unreadable",
    settings: null, roles: null, connection: null })).setup;
  assert.equal(s.state, "unavailable");
  assert.equal(s.reason, "unreadable");
  assert.equal(s.warn, null);
  assert.deepEqual(s.summary, []);
});

test("setup: connection detail is allowlisted, both codes kept under their names", () => {
  const id = ids("family14").campaign_final;
  const s = load("family14", id, (c) => ({ ...c,
    connection: { state: "new_state", outcome: "new_outcome", attempts: 2, detail: {
      code: "outer", cause_code: "inner", metric: "score", manifest_path: "/one", observed_path: "/two", secret: "hidden" } } })).setup;
  assert.deepEqual(s.connection.detail.map((d) => d.name), ["code", "cause_code", "metric", "manifest_path", "observed_path"]);
  assert.deepEqual(s.connection.detail.slice(0, 2).map((d) => d.value), ["outer", "inner"]);
  assert.equal(s.warn, "connection not compatible");
});


test("finalist decision and lineage transitions remain separate; missing differs from no match", () => {
  const id = ids("family14").campaign_final;
  const patch = (c) => {
    const a = c.rounds[0].attempts[0];
    a.outcome = "finalist"; a.reason = "observe_finalist"; a.final_decision = null;
    c.details_state = "unavailable"; c.details_reason = "unreadable";
    c.rounds[0].final = null;
    c.champions = [{ origin: "promoted_attempt", attempt_id: a.attempt_id, version: "v8" },
      { origin: "other", attempt_id: a.attempt_id, version: "v9" },
      { origin: "promoted_attempt", attempt_id: a.attempt_id, version: "v2" }];
    c.champions_state = "present";
    return c;
  };
  const r = load("family14", id, patch).rounds[0];
  const a = attemptRows(r)[0];
  // the final step is unreadable: said so, neither a pending row nor a decision of our own
  assert.deepEqual(r.finalState, { state: "unavailable", reason: "unreadable" });
  assert.deepEqual(shape(r), [`a:${a.id}`]);
  assert.deepEqual(a.champion.text, "→ v8 · → v2");
  assert.equal(attemptRows(load("family14", id, (c) => ({ ...patch(c), champions_state: "unavailable" })).rounds[0])[0].champion.text, "unavailable");
  assert.deepEqual(attemptRows(load("family14", id, (c) => ({ ...patch(c), champions: [] })).rounds[0])[0].champion,
    { text: "—", title: "transition not recorded" });
});

test("final read availability differs from an empty final, and Core decisions keep unknown codes", () => {
  const id = ids("family14").campaign_final;
  const missing = load("family14", id, (c) => {
    c.rounds[0].final = null;
    c.details_state = "unavailable"; c.details_reason = "unreadable";
    return c;
  }).rounds[0];
  assert.deepEqual(missing.finalState, { state: "unavailable", reason: "unreadable" });
  assert.deepEqual(finalRows(missing), []);
  const empty = load("family14", id, (c) => { c.rounds[0].final = []; return c; }).rounds[0];
  assert.deepEqual(empty.finalState, { state: "present", reason: null });
  assert.deepEqual(finalRows(empty), []);
  const model = load("family14", id, (c) => {
    c.champions_state = "present"; c.champions = [];
    c.rounds[0].final[0].contrasts[0].exact = false;
    c.rounds[0].final[0].contrasts[0].accepted = null;
    c.rounds[0].final[0].contrasts[0].reason = null;
    c.rounds[0].final[0].contrasts[0].unavailable = "future_unavailable";
    c.rounds[0].final[0].champion_side = "future_side";
    return c;
  });
  const f = finalRows(model.rounds[0])[0];
  // Owner's rule (owner/campaign_views.py final decisions): neither accepted nor a recorded rejection is
  // unavailable, its code kept — the Measurement Decision panel's word (final review item 8).
  assert.deepEqual(f.decision, { text: "Unavailable", tone: "amber", code: "future_unavailable" });
  assert.equal(f.side, "vs v0 · future_side");
  assert.equal(attemptRows(model.rounds[0])[0].champion.text, "—");
  const contrast = f.detail.contrasts[0];
  assert.equal(contrast.exact, false);
  assert.equal(contrast.accepted, null);
  assert.equal(contrast.unavailable, "future_unavailable");
});

// family14 round 1 rebuilt from its own attempt and final record: attempts [id, outcome, group] in order,
// final records [ordinal, finalist attempt ids, fields] in Core order (spec family14 §3.3, §5 variants by patch).
function withRound(c, attempts, finals, stop) {
  const r = c.rounds[0];
  const a0 = r.attempts[0], f0 = r.final[0];
  if (stop !== undefined) c.settings.round_stop = stop;
  r.attempts = attempts.map(([attempt_id, outcome, group]) => ({ ...structuredClone(a0), attempt_id, outcome, group, measurement_id: `m-${attempt_id}` }));
  r.final = finals.map(([ordinal, members, extra = {}]) => ({ ...structuredClone(f0), ordinal,
    members: members.map((aid) => ({ ...structuredClone(f0.members[0]), attempt_id: aid, measurement_id: `m-r01-final-0${ordinal}-${aid.slice(-3)}` })),
    contrasts: members.map((aid) => ({ ...structuredClone(f0.contrasts[0]), attempt_id: aid })), ...extra }));
  return c;
}
const F14 = () => ids("family14").campaign_final;

test("rounds: first_accepted puts each final under its finalist", () => {
  const plain = load("family14", F14()).rounds[0];
  assert.deepEqual(shape(plain), ["a:r01-a01", "f:1"]);
  const raw = body("family14", `/v1/operations/${F14()}/campaign`).rounds[0].final[0];
  const f = finalRows(plain)[0];
  assert.deepEqual({ joint: f.joint, label: f.label, members: f.members, measurementId: f.measurementId, point: f.point, delta: f.delta, side: f.side, warns: f.warns },
    { joint: false, label: "Final · entry 1", members: ["r01-a01"], measurementId: raw.members[0].measurement_id, point: "1.000", delta: "+1.000",
      side: "vs v0 · stored", warns: [] });
  assert.deepEqual(f.decision, { text: "Accepted", tone: "green", code: "global_off" });
  assert.match(f.href, /measurements\/m-r01-final-01-a01$/);
  assert.equal(plain.header.title, "Round 1");
  assert.equal(plain.header.how, "stops at the first accepted finalist · stable · Champion side: stored measurement");

  const r = load("family14", F14(), (c) => withRound(c,
    [["r01-a00", "rejected", "agent_worse"], ["r01-a01", "finalist", "accepted"], ["r01-a02", "finalist", "accepted"]],
    [[1, ["r01-a01"]], [2, ["r01-a02"], { condition_changed_in_place: true, listed: 2 }], [3, ["r01-a09"]]])).rounds[0];
  // a record whose finalist is not among the attempts goes last, in Core order; none is dropped
  assert.deepEqual(shape(r), ["a:r01-a00", "a:r01-a01", "f:1", "a:r01-a02", "f:2", "f:3"]);
  assert.equal(finalRows(r).every((x) => x.joint === false), true);
  assert.deepEqual(attemptRows(r).map((a) => a.champion.text), ["—", "→ v1", "—"]);
  assert.equal(attemptRows(r)[0].champion.title, "transition not recorded");
  assert.deepEqual(attemptRows(r).map((a) => a.marks), [["no final"], [], []]);
  assert.deepEqual(finalRows(r)[1].warns, ["condition changed in place", "listed 2"]);
});

test("rounds: all_attempts keeps two recorded finals as two joint rows", () => {
  const model = load("family14", F14(), (c) => {
    withRound(c, [["r01-a01", "finalist", "accepted"], ["r01-a02", "rejected", "agent_worse"], ["r01-a03", "finalist", "accepted"]],
      [[1, ["r01-a01", "r01-a03"]], [2, ["r01-a03"]]], "all_attempts");
    const f1 = c.rounds[0].final[0];
    f1.members.unshift({ ...structuredClone(f1.members[0]), role: "champion", attempt_id: null, measurement_id: "m-r01-final-01-champion" });
    Object.assign(f1.contrasts[0], { accepted: false, reason: "global_bound_not_positive" });
    f1.champion_side = "fresh";
    return c;
  });
  const r = model.rounds[0];
  assert.deepEqual(shape(r), ["a:r01-a01", "a:r01-a02", "a:r01-a03", "f:1", "f:2"]);
  // the unit is the final[] record: two records, two rows, neither merged (spec family14 §3.3)
  assert.equal(finalRows(r).length, 2);
  assert.deepEqual(finalRows(r).map((f) => [f.joint, f.label]), [[true, "Joint final · entry 1"], [true, "Joint final · entry 2"]]);
  assert.deepEqual(finalRows(r).map((f) => f.members), [["r01-a01", "r01-a03"], ["r01-a03"]]);
  assert.deepEqual(attemptRows(r).map((a) => a.marks), [["in joint final"], ["no final"], ["in joint final"]]);
  const [joint, single] = finalRows(r);
  assert.deepEqual([joint.finalists, joint.measurementId, joint.point, joint.delta], [2, null, null, null]);
  assert.deepEqual(joint.decision, { text: "1 accepted · 1 not accepted", tone: "green", code: null });
  assert.equal(joint.side, "vs v0 · re-measured");
  assert.equal(joint.detail.members.length, 3);
  assert.deepEqual([single.finalists, single.point, single.decision.text], [1, "1.000", "Accepted"]);
  // the records disagree (fresh, stored): the header names no side, each final row states its own (M1)
  assert.equal(r.header.how, "all attempts run, then joint final · stable");
  assert.deepEqual(finalRows(r).map((f) => f.side), ["vs v0 · re-measured", "vs v0 · stored"]);
});

// Final review item 8: Owner's rule (owner/campaign_views.py) — accepted false with reason final_incomplete is not a
// rejection but unavailable, as the Measurement Decision panel reads it; in a joint record it is counted so.
test("rounds: a final_incomplete contrast reads Unavailable · final_incomplete, not Not accepted", () => {
  const single = finalRows(load("family14", F14(), (c) => {
    Object.assign(c.rounds[0].final[0].contrasts[0], { accepted: false, reason: "final_incomplete" }); return c;
  }).rounds[0])[0];
  assert.deepEqual(single.decision, { text: "Unavailable", tone: "amber", code: "final_incomplete" });
  // the expanded record's Contrasts table reads the same word (ui/rounds.js uses contrastDecision)
  assert.deepEqual(contrastDecision(single.detail.contrasts[0]), single.decision);
  const rejected = finalRows(load("family14", F14(), (c) => {
    Object.assign(c.rounds[0].final[0].contrasts[0], { accepted: false, reason: "global_bound_not_positive" }); return c;
  }).rounds[0])[0];
  assert.deepEqual(rejected.decision, { text: "Not accepted", tone: "neutral", code: "global_bound_not_positive" });
  const joint = finalRows(load("family14", F14(), (c) => {
    withRound(c, [["r01-a01", "finalist", "accepted"], ["r01-a03", "finalist", "accepted"]], [[1, ["r01-a01", "r01-a03"]]], "all_attempts");
    Object.assign(c.rounds[0].final[0].contrasts[0], { accepted: false, reason: "final_incomplete" });
    return c;
  }).rounds[0])[0];
  assert.deepEqual(joint.decision, { text: "1 accepted · 1 unavailable", tone: "green", code: null });
});

test("rounds: unknown round_stop lists finals after attempts in core order", () => {
  for (const stop of ["future_stop", null]) {
    const r = load("family14", F14(), (c) => withRound(c, [["r01-a01", "finalist", "accepted"], ["r01-a02", "finalist", "accepted"]],
      [[2, ["r01-a02"]], [1, ["r01-a01"]]], stop)).rounds[0];
    assert.deepEqual(shape(r), ["a:r01-a01", "a:r01-a02", "f:2", "f:1"]);
    assert.equal(finalRows(r).every((f) => f.joint === false), true);
    assert.deepEqual(attemptRows(r).map((a) => a.marks), [[], []]);
    assert.equal(r.header.how.startsWith(stop === null ? "stable" : "future_stop · stable"), true);
  }
});

// Ruling R8: the placeholder follows Owner's final_decision of the finalist (owner/campaign_views.py
// final_decisions) — pending while running, unavailable once the round/campaign closed; a decided one has none.
const finalDecision = (state, reason = null) => (c) => {
  c.rounds[0].final = [];
  c.rounds[0].attempts[0].final_decision = { state, reason };
  return c;
};
test("rounds: finalist without a final shows a pending row", () => {
  const r = load("family14", F14(), finalDecision("pending")).rounds[0];
  assert.deepEqual(shape(r), ["a:r01-a01", "p:r01-a01"]);
  assert.deepEqual(r.rows.at(-1), { kind: "pending-final", attemptId: "r01-a01", state: "pending", reason: null });
  assert.deepEqual(attemptRows(r)[0].marks, []);
  const closed = load("family14", F14(), finalDecision("unavailable", "rounds_exhausted")).rounds[0];
  assert.deepEqual(closed.rows.at(-1), { kind: "pending-final", attemptId: "r01-a01", state: "unavailable", reason: "rounds_exhausted" });
  // the served finished campaign: final_decision accepted, no record left -> no placeholder of our own
  for (const state of ["accepted", "rejected", "future_state"]) {
    const decided = load("family14", F14(), finalDecision(state, "x")).rounds[0];
    assert.deepEqual(shape(decided), ["a:r01-a01"], state);
  }
  const plain = load("family14", F14(), (c) => { c.rounds[0].final = []; return c; }).rounds[0];
  assert.deepEqual(shape(plain), ["a:r01-a01"]);
  const rejected = load("family14", F14(), (c) => withRound(c, [["r01-a00", "rejected", "agent_worse"]], [])).rounds[0];
  assert.deepEqual(shape(rejected), ["a:r01-a00"]);
  assert.deepEqual(attemptRows(rejected)[0].marks, ["no final"]);
});

test("rounds: all_attempts and unknown round_stop put placeholders after the attempts, in attempt order", () => {
  for (const stop of ["all_attempts", "future_stop"]) {
    const r = load("family14", F14(), (c) => {
      withRound(c, [["r01-a01", "finalist", "accepted"], ["r01-a02", "rejected", "agent_worse"], ["r01-a03", "finalist", "accepted"],
        ["r01-a04", "finalist", "accepted"]], [[1, ["r01-a04"]]], stop);
      const [a1, , a3] = c.rounds[0].attempts;
      a1.final_decision = { state: "pending", reason: null };
      a3.final_decision = { state: "unavailable", reason: "final_incomplete" };
      return c;
    }).rounds[0];
    assert.deepEqual(shape(r), ["a:r01-a01", "a:r01-a02", "a:r01-a03", "a:r01-a04", "p:r01-a01", "p:r01-a03", "f:1"], stop);
    assert.deepEqual(r.rows.filter((x) => x.kind === "pending-final").map((p) => [p.state, p.reason]),
      [["pending", null], ["unavailable", "final_incomplete"]]);
  }
});

test("rounds: champion column says unavailable when lineage is not present", () => {
  for (const state of ["unavailable", "not_started"]) {
    const rows = load("family14", F14(), (c) => ({ ...c, champions: null, champions_state: state })).rounds.flatMap(attemptRows);
    assert.equal(rows.length > 0, true);
    for (const a of rows) assert.deepEqual(a.champion, { text: "unavailable", title: `champions_state: ${state}` });
  }
});

test("rounds: selection is shown on every entry where core recorded it, null otherwise", () => {
  const r = load("family14", F14(), (c) => withRound(c, [["r01-a01", "finalist", "accepted"], ["r01-a02", "finalist", "accepted"], ["r01-a03", "finalist", "accepted"]],
    [[1, ["r01-a01"], { selection: { reason: "no_accepted_finalist", attempt_id: null, measurement_id: null } }],
     [2, ["r01-a02"], { selection: { reason: "finalist_selected", attempt_id: "r01-a02", measurement_id: "m-r01-final-02-a02" } }],
     [3, ["r01-a03"], { selection: null }]])).rounds[0];
  const [f1, f2, f3] = finalRows(r);
  assert.deepEqual([f1.selection.reason, f1.selection.text, f1.selection.attempt_id, f1.selection.measurementHref],
    ["no_accepted_finalist", "No accepted finalist", null, null]);
  assert.deepEqual([f2.selection.reason, f2.selection.attempt_id], ["finalist_selected", "r01-a02"]);
  assert.match(f2.selection.measurementHref, /measurements\/m-r01-final-02-a02$/);
  assert.equal(f3.selection, null);
  assert.equal(f2.detail.selection, f2.selection);
  assert.equal(f3.detail.selection, null);
});

test("strip: final measurements are labelled by their attempt", () => {
  const rows = load("family14", F14()).strip.rows;
  const final = rows.find((r) => r.id === "m-r01-final-01-a01");
  assert.equal(final.label, "r01-a01 · final 1 · baseline r2");
  assert.equal(final.title, final.label);
  const champion = rows.find((r) => r.id === "m-r00-champion");
  assert.deepEqual([champion.label, champion.title], ["baseline r1", "baseline r1"]);
});

// Native dev58/b10 capture: preserve producing /10 and copy only its schema for current family14 tests.
test("family 14 historical /12 renders through a synthetic current-schema copy", () => {
  const id = ids("family14").campaign_final;
  const raw = JSON.parse(readFileSync(join(process.env.LAB_PANEL_FIXTURES, "family14", "routes.json"), "utf-8"))[`/v1/operations/${id}/campaign`].body;
  assert.equal(raw.schema, "lab-campaign-view/10");
  assert.equal(raw.family, 14);
  assert.deepEqual(body("family14", `/v1/operations/${id}/campaign`), { ...raw,
    schema: "lab-campaign-view/15", lab_notices_state: "unavailable", reflector_probe: null,
    reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" });
  const model = load("family14", id);
  assert.deepEqual(model.schema, { ok: true, code: null });
  assert.equal(model.independent, null);
  assert.equal(model.setup.settings.find((s) => s.name === "Format").value, "manifest /15 · family 14");
  assert.equal(model.rounds.flatMap(finalRows).length, raw.rounds.reduce((n, r) => n + (r.final?.length ?? 0), 0));
  assert.equal(model.result.limits[0].code, "observe_gate_not_enforced");
});

// Adapted producer-backed /10 bodies: corrupt one consumed scalar at a time. Container-only guards let these
// values through to labels, arithmetic, confidence rendering or reference links.
for (const family of [14, 15]) {
  const baseline = () => structuredClone(Object.values(snapshot(`family${family}`).routes)
    .find((r) => r.status === 200 && r.body?.schema === "lab-campaign-view/15").body);
  const mutations = [
    ["string", (c) => { c.rounds[0].attempts[0].group = {}; }],
    ["integer", (c) => { c.rounds[0].round = 1.5; }],
    ["boolean", (c) => { c.rounds[0].attempts[0].has_tree = "true"; }],
    ["plain number", (c) => { Object.values(c.measurements[0].metrics)[0].mean = { value: 1, exact: null }; }],
    ...(family === 14 ? [
      ["native confidence margin", (c) => { c.rounds[0].attempts[0].confidence.margin = "0"; }],
      ["native confidence point", (c) => { c.rounds[0].attempts[0].confidence.latest.focus.point = Infinity; }],
      ["native final point", (c) => { c.rounds[0].final[0].members[0].point = { value: 1 }; }],
    ] : [
      ["statistic wrapper", (c) => { c.independent.attempts[0].global_comparison.scopes[0].point = 1; }],
      ["statistic display scalar", (c) => { c.independent.settings.execution_noise.allowance.value = "0"; }],
      ["statistic exact container", (c) => { c.independent.attempts[0].eligible_final_score.exact = []; }],
      ["reference id", (c) => { c.independent.attempts[0].global_comparison.reference_measurement_id = 7; }],
    ]),
  ];
  test(`family ${family}: the adapted producer body renders with native measurement numbers`, () => {
    const c = baseline();
    assert.equal(readOf(campaignEntry(okEntry(c))).state, "ok");
    const model = campaignModel({ campaign: c, measurements: {} });
    assert.equal(model.schema.ok, true);
    assert.ok(model.rounds.length > 0);
    if (family === 14) {
      assert.equal(typeof c.rounds[0].attempts[0].confidence.margin, "number");
      assert.ok(finalRows(model.rounds[0]).length > 0);
    } else assert.equal(model.independent.state, "present");
  });
  for (const [kind, mutate] of mutations) test(`family ${family}: wrong ${kind} makes /13 unsupported`, () => {
    const c = baseline(); mutate(c);
    const read = readOf(campaignEntry(okEntry(c)));
    assert.equal(read.state, "unavailable");
    assert.equal(read.code, "unsupported_schema");
    assert.equal(campaignModel({ campaign: c, measurements: {} }).schema.ok, false);
  });
}

// Separate nested producers must retain their scalar predicates when the guard evolves.
test("consumed nested scalar paths reject the wrong kind before models read them", () => {
  const baseline = (family) => structuredClone(Object.values(snapshot(`family${family}`).routes)
    .find((r) => r.status === 200 && r.body?.schema === "lab-campaign-view/15").body);
  const mutations = [
    [14, (c) => { c.operation_id = {}; }],
    [14, (c) => { c.details_reason = []; }],
    [14, (c) => { c.policy.metric_primary = 1; }],
    [14, (c) => { c.progress.phase = false; }],
    [14, (c) => { c.progress.attempts.current_round.closed = 0.5; }],
    [14, (c) => { c.progress.coverage = { scored: "1", expected: 2 }; }],
    [14, (c) => { c.settings.baseline_refresh.performed = [{ round: 3, completed: "false" }]; }],
    [14, (c) => { c.roles.editor.kind = {}; }],
    [14, (c) => { c.connection.detail = { observed_path: [] }; }],
    [14, (c) => { c.connection.attempts = 1.5; }],
    [14, (c) => { c.caveats = [{ kind: "future", round: "1", attempt_id: null }]; }],
    [14, (c) => { c.suspensions = [{ owner: "measurement", reason: {}, evidence_kind: "fault_rows", seq: 1 }]; }],
    [14, (c) => { c.lab_notices = [{ code: "future", by_reason: { future: "1" } }]; }],
    [14, (c) => { c.outcome = { outcome: false, reason: null }; }],
    [14, (c) => { c.champions[0].measurement_id = []; }],
    [14, (c) => { c.rounds[0].reflection.hypotheses[0].index = "1"; }],
    [14, (c) => { c.rounds[0].reflection.hypotheses[0].affected_cases = [false]; }],
    [14, (c) => { c.rounds[0].reflection.hypotheses[0].root_cause = {}; }],
    [14, (c) => { c.rounds[0].attempts[0].failure = { stage: "editor", code: {} }; }],
    [14, (c) => { c.rounds[0].attempts[0].final_decision.state = false; }],
    [14, (c) => { c.rounds[0].attempts[0].confidence.calibration = { allowance: "0", degenerate: false }; }],
    [14, (c) => { c.rounds[0].attempts[0].confidence.latest.focus.exact = "false"; }],
    [14, (c) => { c.rounds[0].attempts[0].confidence.latest.focus.lower = NaN; }],
    [14, (c) => { c.rounds[0].final[0].contrasts[0].lower = "0"; }],
    [14, (c) => { c.rounds[0].final[0].contrasts[0].accepted = 1; }],
    [14, (c) => { c.rounds[0].final[0].members[0].missing = "0"; }],
    [14, (c) => { c.rounds[0].final[0].selection.measurement_id = {}; }],
    [14, (c) => { c.measurements[0].usage.input_tokens = "1"; }],
    [14, (c) => { c.measurements[1].baseline = { delta: { score: { mean: "0" } } }; }],
    [15, (c) => { c.independent.settings.repeats = true; }],
    [15, (c) => { c.independent.settings.execution_noise.kind = {}; }],
    [15, (c) => { c.independent.attainability.local_guidance.minimum_units = 0.5; }],
    [15, (c) => { c.independent.attempts[0].conditional_cost.candidate_total_slots = "2"; }],
    [15, (c) => { c.independent.attempts[0].comparison.reference_measurement_id = false; }],
    [15, (c) => { c.independent.attempts[0].gate_comparison = { scopes: [{ display: {} }] }; }],
    [15, (c) => { c.independent.attempts[0].global_comparison.scopes[0].strict = "true"; }],
    [15, (c) => { c.independent.attempts[1].preflight.scopes[0].passes = 1; }],
    [15, (c) => { c.independent.attempts[1].preflight.scopes[0].delta_max.value = NaN; }],
    [15, (c) => { c.independent.attainability.preflight = [{ round: "1", explanation: null }]; }],
  ];
  for (const [family, mutate] of mutations) {
    const c = baseline(family); mutate(c);
    assert.equal(readOf(campaignEntry(okEntry(c))).code, "unsupported_schema", String(mutate));
  }
});

test("scalar guard retains unknown words, nullable statistics and integer values without added policy", () => {
  const c = structuredClone(body("family14", `/v1/operations/${ids("family14").campaign_final}/campaign`));
  c.rounds[0].attempts[0].group = "future_group";
  c.rounds[0].attempts[0].confidence.margin = -0.125;
  c.rounds[0].attempts[0].confidence.latest.focus.lower = null;
  c.progress.rounds.completed = -1;
  const model = campaignModel({ campaign: c, measurements: {} });
  assert.equal(model.schema.ok, true);
  assert.equal(attemptRows(model.rounds[0])[0].group.text, "future_group");
});

// Core b9 final_report._member14 emits all completion facts as null until cohort completion;
// confidence_report._validate_member explicitly admits that pending form. Lab final_of copies it unchanged.
test("pending final member remains supported before completion facts arrive", () => {
  const id = ids("family14").campaign_final;
  const c = structuredClone(body("family14", `/v1/operations/${id}/campaign`));
  const member = c.rounds[0].final[0].members[0];
  Object.assign(member, { complete: null, point: null, missing: null, critical: null });
  assert.equal(readOf(campaignEntry(okEntry(c))).state, "ok");
  const model = campaignModel({ campaign: c, measurements: {} });
  assert.equal(model.schema.ok, true);
  const rendered = finalRows(model.rounds[0])[0].detail.members[0];
  for (const key of ["complete", "point", "missing", "critical"]) assert.equal(rendered[key], null);
  for (const [key, value] of [["complete", "false"], ["critical", 0], ["missing", "0"]]) {
    const malformed = structuredClone(c);
    malformed.rounds[0].final[0].members[0][key] = value;
    assert.equal(readOf(campaignEntry(okEntry(malformed))).code, "unsupported_schema", key);
  }
});

test("baseline notice explains stored evidence without claiming the target never reports", () => {
  const { result } = load("family14", ids("family14").campaign_final, (c) => ({ ...c, lab_notices: [{ code: "baseline_provenance_unknown", round: 1, measurement_id: "m", samples: 2 }] }));
  assert.match(result.notices[0].explanation, /no stored provenance/);
  assert.match(result.notices[0].explanation, /all train/);
});
