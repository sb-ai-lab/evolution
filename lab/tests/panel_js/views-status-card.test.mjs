import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const { statusCardOf } = await staticModule("lib/views/status-card.js");
const ok = (data) => ({ state: "ok", data, stale: false, code: null });
const failed = (code = "network_error") => ({ state: "error", code, stale: false });
const loading = { state: "loading" };
const progress = { phase: "evaluation", rounds: { current: 2, completed: 1, planned: 4 },
  attempts: { total: 4, current_round: { closed: 3, running: 1, planned: 5 } }, coverage: { scored: 4, expected: 9 } };
const op = (extra = {}) => ({ control_state: "finished", progress, ...extra });
const campaign = (extra = {}) => ({ schema: "lab-campaign-view/15", lab_notices_state: "present", family: 14, operation_id: "test", details_state: "present", gate_mode: "statistical", rounds: [], reflector_probe: null, reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable", outcome: null, progress, champions: [], champions_state: "present", caveats: [],
  lab_notices: [], suspensions: [], ...extra });
const card = (o = op(), c = campaign(), screen = "campaign") => statusCardOf(ok(o), ok(c), { screen });

for (const [outcome, reason, expected] of [
  ["stopped", "cancelled", "The campaign was cancelled in round 2 of 4."],
  ["stopped", "condition_changed", "The campaign stopped in round 2 of 4."],
  ["completed", "rounds_exhausted", "The campaign finished all 4 rounds."],
  ["budget_exhausted", "rollout_time_budget_exhausted", "The campaign ran out of its evaluation budget in round 2 of 4."],
  ["infrastructure_failed", "editor_unresponsive", "The campaign stopped on an infrastructure failure in round 2 of 4."],
]) test(`outcome ${outcome}/${reason}`, () => {
  const c = card(op(), campaign({ outcome: { outcome, reason } }));
  assert.equal(c.verdict, expected); assert.ok(c.codes.join(" ").includes(outcome)); assert.ok(c.codes.join(" ").includes(reason));
});

test("unknown outcome or reason has no template and preserves codes", () => {
  for (const outcome of [{ outcome: "future", reason: "cancelled" }, { outcome: "stopped", reason: "future" }]) {
    const c = card(op(), campaign({ outcome }));
    assert.equal(c.verdict, null); assert.equal(c.neutral, `Core recorded ${outcome.outcome} · ${outcome.reason}`);
    assert.ok(c.codes.join(" ").includes(outcome.outcome)); assert.ok(c.codes.join(" ").includes(outcome.reason));
  }
});

test("recorded outcome wins over stopping", () => {
  assert.match(card(op({ control_state: "stopping" }), campaign({ outcome: { outcome: "stopped", reason: "cancelled" } })).verdict, /cancelled/);
});

test("campaign codes do not present an earlier suspension as the reason for a later outcome", () => {
  const previousPause = { owner: "scorer", reason: "scorer_metric_missing", evidence_kind: "fault_rows", seq: 9 };
  const c = card(op(), campaign({ outcome: { outcome: "completed", reason: "rounds_exhausted" },
    suspensions: [previousPause] }));
  assert.equal(c.verdict, "The campaign finished all 4 rounds.");
  assert.deepEqual(c.codes, ["outcome completed", "reason rounds_exhausted"]);
});

test("campaign codes omit earlier suspension while a resumed campaign is running", () => {
  const c = card(op({ control_state: "running" }), campaign({ suspensions: [
    { owner: "scorer", reason: "scorer_metric_missing", evidence_kind: "fault_rows", seq: 9 },
  ] }));
  assert.match(c.verdict, /Measuring candidate/);
  assert.ok(!c.codes.some((code) => /scorer_metric_missing|fault_rows|owner scorer|seq 9/.test(code)));
});

for (const [diagnostic, expected] of [
  [{ refusal: { code: "invalid_input", class: "hard" } }, "Core refused this campaign — invalid_input."],
  [{ core_terminal: { terminal: "persistence_failed", code: "record_matches_secret", owner: "persistence" } }, "Core ended the campaign in round 2 of 4 — persistence_failed record_matches_secret."],
  [{ failure: { code: "worker_failed", stage: "worker" } }, "The campaign failed in round 2 of 4 — worker_failed."],
]) test(`diagnostic ${Object.keys(diagnostic)[0]}`, () => assert.equal(card(op(diagnostic)).verdict, expected));

test("diagnostic order refusal, core terminal, failure", () => {
  const o = op({ refusal: { code: "first" }, core_terminal: { code: "second" }, failure: { code: "third" } });
  assert.match(card(o).verdict, /first/); delete o.refusal; assert.match(card(o).verdict, /second/);
});

for (const [field, diagnostic] of Object.entries({ failure: { code: "worker_failed" }, refusal: { code: "denied" },
  core_terminal: { owner: "campaign_projection", terminal: "suspended", code: "projection_invalid" } })) {
  test(`pause retains ${field} and suspension codes`, () => {
    const c = card(op({ resume: { state: "ready" }, [field]: diagnostic }), campaign({ suspensions: [
      { owner: "scorer", reason: "scorer_metric_missing", evidence_kind: "fault_rows", seq: 9 }] }), "suspended");
    assert.match(c.verdict, /paused in round 2 of 4; the last start recorded/); assert.ok(c.verdict.includes(diagnostic.code));
    assert.doesNotMatch(c.verdict, /did not run/);
    for (const value of [diagnostic.code, "scorer", "scorer_metric_missing", "fault_rows", "9"]) assert.ok(c.codes.join(" ").includes(value));
    assert.equal(c.pill.text, "Suspended");
  });
}

test("pause reason is labelled if known, raw if unknown, unavailable if view unread", () => {
  for (const [reason, expected] of [["champion_incomplete", "Champion measurement incomplete"], ["future_reason", "future_reason"]]) {
    assert.ok(card(op({ resume: {} }), campaign({ suspensions: [{ owner: "scorer", reason, evidence_kind: "fault_rows", seq: 1 }] }), "suspended").verdict.includes(expected));
  }
  assert.match(statusCardOf(ok(op({ resume: {} })), failed("unsupported_schema"), { screen: "suspended" }).verdict,
    /pause reason is unavailable \(campaign view unsupported_schema\)/);
});

test("running phase, running without phase, reserved, stopping and no outcome", () => {
  assert.match(card(op({ control_state: "running" })).verdict, /Measuring candidate.*round 2 of 4/);
  assert.equal(card(op({ control_state: "running", progress: null }), campaign({ progress: null })).verdict, "The campaign is running.");
  assert.equal(card(op({ control_state: "reserved" })).verdict, "The campaign is reserved and has not started.");
  assert.equal(card(op({ control_state: "stopping" })).verdict, "The campaign is stopping.");
  assert.equal(card().verdict, "Core recorded no outcome for this campaign.");
});

test("unread or stale campaign cannot prove absence of outcome and operation progress is fallback", () => {
  for (const read of [failed(), failed("unsupported_schema"), { state: "error", code: "network_error", stale: true, data: campaign() }]) {
    const c = statusCardOf(ok(op()), read, { screen: "campaign" });
    assert.match(c.verdict, /Outcome unknown — campaign view unavailable/); assert.doesNotMatch(c.verdict, /no outcome/);
    assert.equal(c.columns.progress.round, 2);
  }
});

test("missing progress does not insert placeholders into verdict", () => {
  const c = card(op({ progress: null }), campaign({ progress: null, outcome: { outcome: "stopped", reason: "cancelled" } }));
  assert.equal(c.verdict, "The campaign was cancelled.");
});

test("operation loading and unavailable block the verdict", () => {
  for (const read of [loading, failed()]) { const c = statusCardOf(read, ok(campaign()), { screen: "campaign" });
    assert.equal(c.verdict, null); assert.equal(c.readState.operation.state, read === loading ? "loading" : "unavailable"); }
});

test("each column marks only its own source stale", () => {
  const stale = (data) => ({ state: "error", data, code: "network_error", stale: true });
  let c = statusCardOf(stale(op()), ok(campaign()), { screen: "campaign" });
  assert.deepEqual([c.columns.now.state, c.columns.progress.state, c.columns.result.state], ["stale", "stale", "ok"]);
  c = statusCardOf(ok(op()), stale(campaign()), { screen: "campaign" });
  assert.deepEqual([c.columns.now.state, c.columns.progress.state, c.columns.result.state], ["ok", "ok", "stale"]);
});

test("progress and result columns preserve counts, read states and disclosure entries", () => {
  const c = card(op({ control_state: "running" }), campaign({ champions: [{ version: "v0", origin: "baseline" }, { version: "v1", origin: "baseline" }],
    lab_notices: [{ code: "no_candidate_measured", rounds: 2, by_reason: { clean: 2 } }, { code: "future" }, { code: "later" }] }));
  assert.equal(c.columns.progress.title, "Progress"); assert.equal(c.columns.progress.round, 2);
  assert.equal(c.columns.progress.planned, 4); assert.equal(c.columns.progress.completed, 1);
  assert.equal(c.columns.progress.closed, 3); assert.equal(c.columns.progress.running, 1);
  assert.deepEqual(c.columns.progress.coverage, { scored: 4, expected: 9 });
  assert.equal(c.columns.result.champions.count, 2); assert.equal(c.columns.result.champions.href, "#champion-lineage");
  assert.equal(c.columns.result.entries.length, 3); assert.equal(c.columns.result.visible.length, 2); assert.equal(c.columns.result.more, 1);
  assert.ok(c.columns.result.entries[0].explanation);
  assert.equal(card().columns.progress.title, "Where it stopped"); assert.equal(card().columns.progress.coverage, null);
  assert.equal(card().columns.result.empty, "No limits recorded");
  for (const state of ["unavailable", "not_started"]) assert.equal(card(op(), campaign({ champions_state: state })).columns.result.champions.state, state);
});

test("running verdict uses only recorded attempt counts", () => {
  const running = (attempts) => card(op({ control_state: "running" }), campaign({ progress: { ...progress, attempts } })).verdict;
  assert.match(running({ total: 4, current_round: { closed: 3, running: 1, planned: 5 } }), /3 of 5 attempts closed, 1 running/);
  assert.match(running({ total: 4, current_round: { closed: 3, running: 0, planned: 5 } }), /3 of 5 attempts closed\.$/);
  assert.match(running({ total: 4, current_round: { closed: 3, running: 1, planned: null } }), /3 attempts closed, 1 running/);
  assert.doesNotMatch(running({ total: 4, current_round: null }), /attempts|running/);
});
