import { test } from "node:test";
import assert from "node:assert/strict";
import { body, inspect, snapshot, staticModule } from "./support.mjs";

const { suspendedModel } = await staticModule("lib/views/suspended.js");
const { continuationLine } = await staticModule("lib/continuation.js");
const { okEntry } = await staticModule("lib/resource.js");
const { label } = await staticModule("lib/labels.js");
const { Lineage } = await staticModule("ui/lineage.js");
const ids = (name) => snapshot(name).meta.ids;

function load(name, id) {
  const operation = body(name, `/v1/operations/${id}`);
  const campaign = body(name, `/v1/operations/${id}/campaign`);
  const report = okEntry(body(name, `/v1/operations/${id}/report`));
  const current = operation.progress?.rounds?.current ?? 1;
  const mid = campaign.rounds.find((r) => r.round === current)?.baseline_measurement_id;
  const baseline = mid ? okEntry(body(name, `/v1/operations/${id}/measurements/${mid}`)) : undefined;
  return { operation, campaign, report, model: suspendedModel({ operation, campaign, report, baseline }) };
}

test("target suspension: cause from the campaign view, fresh target required, the CLI's line", () => {
  const { operation, campaign, report, model } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  assert.deepEqual([model.cause.owner, model.cause.reason], [campaign.suspensions.at(-1).owner, campaign.suspensions.at(-1).reason]);
  assert.equal(model.card.requirement, "fresh_instance");
  assert.equal(model.card.command, continuationLine(operation, report.data.report.owner_resume_target?.ready_target ?? null));
});

test("role suspension: no target requirement, the same line as the CLI", () => {
  const { operation, model } = load("suspended-role", ids("suspended-role").campaign_suspended_role);
  assert.equal(model.card.requirement, null);
  assert.equal(model.card.command, continuationLine(operation, null));
});

test("starts: only real starts", () => {
  const { operation, model } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  assert.equal(model.starts.length, operation.starts.length);
  assert.equal(model.starts[0].start, operation.starts[0].start);
});

test("suspended lineage preserves recorded champions and marks a stale view", () => {
  const { campaign, model } = load("suspended-role", ids("suspended-role").campaign_suspended_role);
  assert.equal(model.lineage.rows.length, campaign.champions.length);
  assert.deepEqual(model.lineage.rows.map((row) => row.version), campaign.champions.map((champion) => champion.version));
  const fresh = inspect(Lineage({ lineage: model.lineage }));
  const stale = inspect(Lineage({ lineage: model.lineage, stale: true }));
  assert.equal(fresh.elements[0].props.class, "lineage");
  assert.equal(stale.elements[0].props.class, "lineage is-stale");
  assert.match(stale.text, /stale/i);
});

test("status /8 with auto-resume fields remains usable by the suspended screen", () => {
  const { operation, campaign, report } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  const status = structuredClone(operation);
  status.schema = "lab-operation-status/8";
  status.auto_resume = { policy_digest: null, state: "off", next_at: null, used: 0,
    max: null, blocked: null, detail: null };
  status.starts = status.starts.map((start) => ({ ...start, initiated_by: "human", auto_resume_attempt: null }));
  const model = suspendedModel({ operation: status, campaign, report });
  assert.equal(model.schema.ok, true);
  assert.equal(model.starts.length, status.starts.length);
  assert.equal(model.card.command, continuationLine(status, report.data.report.owner_resume_target?.ready_target ?? null));
});

test("the owner breakdown of the baseline is not read by Lab yet", () => {
  const { model } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  assert.equal(model.strip.breakdown, "lab");
  assert.equal(model.strip.read.state, "ok");
});

test("no suspension in the campaign view: null cause; no campaign answer: the strip read is loading", () => {
  const { operation, report } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  const none = suspendedModel({ operation, campaign: { schema: "lab-campaign-view/15", lab_notices_state: "present", family: 14, suspensions: [], rounds: [],
    reflector_probe: null, reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" }, report });
  assert.equal(none.cause, null);
  assert.equal(none.strip.none, true);
  const waiting = suspendedModel({ operation, campaign: undefined, report });
  assert.equal(waiting.strip.read.state, "loading");
});

test("a failed campaign read reaches the baseline strip as unavailable, with its code", () => {
  const { operation, report } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  const model = suspendedModel({ operation, campaign: undefined, report, campaignRead: { state: "unavailable", code: "campaign_refused", stale: false } });
  assert.deepEqual([model.strip.read.state, model.strip.read.code], ["unavailable", "campaign_refused"]);
  assert.equal(model.cause, null);
  assert.equal(model.details.state, "present");
});

// Codex r1: a report read that fails after an answer keeps the command, marked stale on the card and the page.
test("a stale report read marks the command stale; a fresh one does not; a role suspension reads no report", () => {
  const { operation, campaign, report } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  const fresh = suspendedModel({ operation, campaign, report });
  assert.equal(fresh.card.commandStale, false);
  const staleReport = { state: "error", data: report.data, code: "network_error", stale: true };
  const stale = suspendedModel({ operation, campaign, report: staleReport });
  assert.equal(stale.card.commandStale, true);
  assert.equal(stale.card.command, fresh.card.command);
  assert.equal(stale.card.commandRead, null);
  const role = load("suspended-role", ids("suspended-role").campaign_suspended_role);
  assert.equal(suspendedModel({ operation: role.operation, campaign: role.campaign, report: undefined }).card.commandStale, false);
});

test("observe: a suspended observe campaign carries the neutral gate mark", () => {
  const id = ids("suspended-role").campaign_suspended_role;
  const operation = body("suspended-role", `/v1/operations/${id}`);
  const campaign = body("suspended-role", `/v1/operations/${id}/campaign`);
  const report = okEntry(body("suspended-role", `/v1/operations/${id}/report`));
  const observed = { ...structuredClone(campaign), gate_mode: "observe", caveats: [] };
  assert.equal(suspendedModel({ operation, campaign: observed, report }).result.limits[0].text, label("caveat", "observe_gate_not_enforced").text);
  observed.caveats = [
    { kind: "uncalibrated_point_estimates", round: null, attempt_id: null },
    { kind: "future_caveat", round: null, attempt_id: null },
    { kind: "noise_allowance_unavailable", round: 1, attempt_id: null },
    { kind: "intervals_empirical", round: 1, attempt_id: "r01-a01" },
  ];
  for (const r of observed.rounds) {
    for (const a of r.attempts) a.confidence = null;
  }
  const paused = suspendedModel({ operation, campaign: observed, report });
  assert.deepEqual(paused.result.limits, [
    { code: "observe_gate_not_enforced", text: label("caveat", "observe_gate_not_enforced").text, scope: "Campaign" },
    { code: "uncalibrated_point_estimates", text: "Uncalibrated point estimates", scope: "Campaign" },
    { code: "future_caveat", text: "future_caveat", scope: "Campaign" },
  ]);
  for (const state of ["loading", "unavailable", "error"]) {
    assert.deepEqual(suspendedModel({ operation, campaign: observed, report,
      campaignRead: { state, code: "campaign_refused", stale: true } }).result.limits, []);
  }
  const statistical = suspendedModel({ operation, campaign: { ...observed, gate_mode: "statistical" }, report });
  assert.equal(Object.hasOwn(statistical, "gate"), false);
  assert.equal(statistical.result.limits.some((l) => l.text === label("gate_mark", "statistical").text), false);
  const unknown = { ...structuredClone(campaign), gate_mode: null, caveats: [],
                    details_state: "unavailable", details_reason: "unreadable" };
  assert.equal(suspendedModel({ operation, campaign: unknown, report }).result.limits[0].code, "unknown");
});

// Codex r1 #3: the suspended screen checks the schema before any field: an unsupported view of another shape is refused.
test("an unsupported view of another shape: unavailable (unsupported_schema), no field read, no throw", () => {
  const { operation, report } = load("suspended-target", ids("suspended-target").campaign_suspended_target);
  for (const [schema, family] of [["lab-campaign-view/8", 14], ["lab-campaign-view/9", 14], ["lab-campaign-view/10", 14], ["lab-campaign-view/14", 14], ["lab-campaign-view/15", 14], ["lab-campaign-view/6", 14], ["lab-campaign-view/7", 14],
    ["lab-campaign-view/15", 10], ["lab-campaign-view/15", 13], ["lab-campaign-view/15", null]]) {
    const campaign = { schema, family, suspensions: {}, rounds: {}, progress: 5, caveats: {} };
    const model = suspendedModel({ operation, campaign, report });
    assert.deepEqual(model.schema, { ok: false, code: "unsupported_schema" });
    assert.deepEqual([model.strip.read.state, model.strip.read.code], ["unavailable", "unsupported_schema"]);
    assert.equal(model.strip.none, false);
    assert.deepEqual([model.cause, model.rounds], [null, []]);
    assert.deepEqual(model.result.limits, []);
    const read = { state: "ok", code: null, stale: false };  // even when the caller says the read was fine
    assert.equal(suspendedModel({ operation, campaign, report, campaignRead: read }).strip.read.code, "unsupported_schema");
  }
});
test("a final pause carries its label next to the code", async () => {
  const { suspendedModel } = await staticModule("lib/views/suspended.js");
  const { label } = await staticModule("lib/labels.js");
  const s = snapshot("suspended-role").meta.ids;
  const operation = body("suspended-role", `/v1/operations/${s.campaign_suspended_role}`);
  const campaign = structuredClone(body("suspended-role", `/v1/operations/${s.campaign_suspended_role}/campaign`));
  campaign.suspensions = [...campaign.suspensions.slice(0, -1), { ...campaign.suspensions.at(-1), reason: "final_incomplete" }];
  const model = suspendedModel({ operation, campaign });
  assert.deepEqual(model.cause.label, label("suspension_reason", "final_incomplete"));
});
