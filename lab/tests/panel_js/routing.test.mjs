import { test } from "node:test";
import assert from "node:assert/strict";
import { inspect, snapshot, staticModule } from "./support.mjs";
import { settledOperation } from "./settled-screen.mjs";
const { okEntry, errorEntry, LOADING } = await staticModule("lib/resource.js");
const id = "operation-neutral";
const row = (patch = {}) => ({ endpoint: "https://router.example/api/v1", upstream_model: "upstream-a",
  client_model: "client-a", requested: ["provider-a", "provider-b"], reported_provider: "provider-c",
  calls: 7, mismatched: 7, ...patch });
const routing = (operationId = id) => ({ schema: "lab-operation-routing/1", operation_id: operationId,
  runs: [{ start: 1, policy_digest: "a".repeat(64), author: "operator-a", channel: "terminal",
    confirmed_at: 1793386800, lab_version: "0.0.3.dev65" },
  { start: 2, policy_digest: "b".repeat(64), author: "operator-b", channel: "review",
    confirmed_at: 1793390400.25, lab_version: "0.0.3.dev65" }],
  available: true, reason: null, roles: { target: [row()], scorer: [row({ requested: null,
    reported_provider: null, calls: 3, mismatched: 0 })] }, shared: 11, outside: 13 });
const noPolicy = (operationId = id) => ({ ...routing(operationId), runs: [{ start: 1, policy_digest: null,
  author: null, channel: null, confirmed_at: null, lab_version: "0.0.3.dev65" }],
  available: false, reason: "no_policy", roles: null, shared: null, outside: null });
const model = async (entry, operationId = id) => (await staticModule("lib/views/routing.js")).routingModel({ operationId, entry });
const section = async (entry, operationId = id) => inspect((await staticModule("ui/routing.js"))
  .RoutingSection({ operationId, entry }));

test("routing renders both immutable confirmations and each returned role row without totals", async () => {
  const payload = routing(), before = structuredClone(payload);
  const rendered = await section(okEntry(payload));
  for (const value of ["Routing", "operator-a", "terminal", "operator-b", "review", "a".repeat(64), "b".repeat(64),
    "2026-10-30T19:00:00.000Z", "2026-10-30T20:00:00.250Z", "0.0.3.dev65", "target", "scorer", "upstream-a", "client-a",
    "provider-a → provider-b", "provider-c", "Shared calls: 11", "Outside a use: 13"]) assert.ok(rendered.text.includes(value), value);
  assert.equal(rendered.elements.filter(e => e.type === "tbody").flatMap(e => e.props.children).filter(Boolean).length, 2);
  assert.ok(rendered.elements.some(e => e.props.class?.includes("tone-amber")));
  assert.doesNotMatch(rendered.text, /total|cost|quantization|equivalent|provider matched/i);
  assert.deepEqual(payload, before);
});

test("unknown provider and zero observed mismatches have neutral presentation", async () => {
  const payload = routing(); payload.roles = { target: [row({ reported_provider: null, mismatched: 0 })] };
  const rendered = await section(okEntry(payload));
  assert.match(rendered.text, /Not reported/);
  assert.ok(!rendered.elements.some(e => /tone-(green|amber)/.test(e.props.class ?? "")));
});

for (const reason of ["ledger_unreadable", "ledger_unsupported", "ledger_invalid"]) test(`routing ${reason} retains confirmations`, async () => {
  const payload = { ...routing(), available: false, reason, roles: null, shared: null, outside: null };
  const rendered = await section(okEntry(payload));
  assert.match(rendered.text, new RegExp(`Unavailable.*${reason}`));
  assert.match(rendered.text, /operator-a.*operator-b/);
  assert.doesNotMatch(rendered.text, /Shared calls:|Outside a use:|Calls0/);
});

test("stale routing history stays visible and is explicitly marked", async () => {
  const rendered = await section(errorEntry("network_error", routing()));
  assert.match(rendered.text, /stale/);
  assert.match(rendered.text, /operator-a/);
});

test("absent routing and explicit no-policy routing render nothing", async () => {
  for (const entry of [undefined, LOADING, okEntry(undefined), okEntry(noPolicy())]) {
    assert.equal((await section(entry)).elements.length, 0);
    assert.equal((await section(entry)).text, "");
  }
});

const malformed = [
  ["null", () => null], ["array", () => []], ["extra field", p => ({ ...p, cost: 1 })],
  ["unsupported schema", p => ({ ...p, schema: "lab-operation-routing/2" })],
  ["wrong operation", p => ({ ...p, operation_id: "other" })], ["invalid id", p => ({ ...p, operation_id: "../bad" })],
  ["boolean count", p => { p.shared = true; return p; }], ["negative count", p => { p.outside = -1; return p; }],
  ["fractional calls", p => { p.roles.target[0].calls = 1.5; return p; }],
  ["zero calls", p => { p.roles.target[0].calls = 0; return p; }],
  ["too many mismatches", p => { p.roles.target[0].mismatched = 8; return p; }],
  ["extra row field", p => { p.roles.target[0].price = 1; return p; }],
  ["null roles", p => { p.roles = null; return p; }], ["rows object", p => { p.roles.target = {}; return p; }],
  ["empty role", p => { p.roles[" "] = []; return p; }],
  ["duplicate providers", p => { p.roles.target[0].requested = ["p", "p"]; return p; }],
  ["empty providers", p => { p.roles.target[0].requested = []; return p; }],
  ["invalid endpoint", p => { p.roles.target[0].endpoint = "file:///tmp/private"; return p; }],
  ["endpoint credentials", p => { p.roles.target[0].endpoint = "https://key@example.test/v1"; return p; }],
  ["endpoint query", p => { p.roles.target[0].endpoint += "?key=private"; return p; }],
  ["null run", p => { p.runs[0] = null; return p; }],
  ["unordered runs", p => { p.runs.reverse(); return p; }],
  ["bad digest", p => { p.runs[0].policy_digest = "bad"; return p; }],
  ["missing author", p => { p.runs[0].author = null; return p; }],
  ["nonfinite time", p => { p.runs[0].confirmed_at = Infinity; return p; }],
  ["extra run field", p => { p.runs[0].note = "bad"; return p; }],
  ["no-policy with policy history", p => ({ ...p, available: false, reason: "no_policy", roles: null, shared: null, outside: null })],
  ["unavailable partial counts", p => ({ ...p, available: false, reason: "ledger_invalid", roles: null })],
];
for (const [name, mutate] of malformed) test(`routing rejects ${name} before rendering fields`, async () => {
  const value = mutate(routing());
  assert.equal((await model(okEntry(value))).read.code, "unsupported_schema");
  const rendered = await section(okEntry(value));
  assert.match(rendered.text, /Unavailable.*unsupported_schema/);
  assert.doesNotMatch(rendered.text, /operator-a|private/);
});

test("routing retains ordered, single and unpinned providers and does not calculate attribution", async () => {
  const payload = routing(); payload.roles.target.push(row({ requested: "provider-c", calls: 5, mismatched: 0 }));
  const value = await model(okEntry(payload));
  assert.deepEqual(value.routing, payload);
  assert.equal(value.read.state, "ok");
});

test("routing validates the whole identifier and every required field", async () => {
  const { routingShapeOk } = await staticModule("lib/views/routing.js");
  for (const operationId of ["op-a.b:c_1", "7"]) assert.ok(routingShapeOk(routing(operationId), operationId));
  for (const operationId of ["../bad", "op/child", "op\n", "", ".", "x".repeat(129)])
    assert.equal(routingShapeOk(routing(operationId), operationId), false, operationId);
  for (const key of Object.keys(routing())) {
    const payload = routing(); delete payload[key];
    assert.equal(routingShapeOk(payload, id), false, key);
  }
});

test("a finite confirmation outside the date range stays readable", async () => {
  const payload = routing(); payload.runs[0].confirmed_at = 1e100;
  assert.match((await section(okEntry(payload))).text, /1e\+100 \(Unix seconds\)/);
});

// These exercise OperationScreen's actual choice of screen and all nested components.
for (const [fixture, key, expected] of [["family14", "campaign_final", "campaign"],
  ["family15", "campaign_confidence", "campaign"], ["suspended-role", "campaign_suspended_role", "suspended"],
  ["suspended-target", "campaign_suspended_target", "suspended"], ["details", "evaluation", "evaluation"]]) {
  test(`${fixture}: ${expected} operation surface renders routing and passes existing refresh options`, async () => {
    const captured = snapshot(fixture), operationId = captured.meta.ids[key];
    assert.ok(operationId, key);
    const render = await settledOperation(process.env.LAB_PANEL_STATIC);
    const result = render({ routes: captured.routes, id: operationId, routing: okEntry(routing(operationId)), epoch: 3, refresh: 4, outage: true });
    const text = JSON.stringify(result.tree);
    assert.match(text, new RegExp(`"data-screen":"${expected}"`));
    assert.match(text, /operator-a/);
    assert.match(text, /provider-c/);
    const read = result.reads.find(r => r.path === `/v1/operations/${operationId}/routing`);
    assert.deepEqual(read?.opts, { epoch: 3, outage: true, refresh: 4 });
    const absent = render({ routes: captured.routes, id: operationId });
    const noPolicyTree = render({ routes: captured.routes, id: operationId, routing: okEntry(noPolicy(operationId)) });
    assert.equal(JSON.stringify(absent.tree), JSON.stringify(noPolicyTree.tree));
    assert.doesNotMatch(JSON.stringify(absent.tree), /Routing/);
  });

  test(`${fixture}: ${expected} absent/default operation tree is byte-equal to released dev64`, {
    skip: !process.env.LAB_PANEL_BASELINE_STATIC && "set LAB_PANEL_BASELINE_STATIC to the released dev64 static directory",
  }, async () => {
    const captured = snapshot(fixture), operationId = captured.meta.ids[key];
    const current = await settledOperation(process.env.LAB_PANEL_STATIC);
    const baseline = await settledOperation(process.env.LAB_PANEL_BASELINE_STATIC);
    // dev64's report major alone differs; no report field/rendering is changed.
    const baselineRoutes = structuredClone(captured.routes);
    for (const r of Object.values(baselineRoutes)) if (r.body?.schema === "lab-owner-report/15") r.body.schema = "lab-owner-report/12";
    const oldTree = baseline({ routes: baselineRoutes, id: operationId }).tree;
    for (const entry of [undefined, okEntry(noPolicy(operationId))]) {
      assert.equal(JSON.stringify(current({ routes: captured.routes, id: operationId, routing: entry }).tree), JSON.stringify(oldTree));
    }
  });
}
