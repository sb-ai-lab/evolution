// The real screen functions run with settled resource hooks, without a browser or live service.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { body, inspect, snapshot, staticModule } from "./support.mjs";

const campaignViews = await staticModule("lib/views/campaign.js");
const { campaignEntry, campaignModel, supportedCampaign } = campaignViews;
const { measurementModel } = await staticModule("lib/views/measurement.js");
const { suspendedModel } = await staticModule("lib/views/suspended.js");
const { pairsState } = await staticModule("lib/views/diff.js");
const { okEntry, readOf } = await staticModule("lib/resource.js");
const { DiffSummary } = await staticModule("ui/diff.js");

const fileUrl = (rel) => pathToFileURL(join(process.env.LAB_PANEL_STATIC, rel)).href;
const dataUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const hookUrl = dataUrl(`
  import { campaignEntry } from ${JSON.stringify(fileUrl("lib/views/campaign.js"))};
  import { okEntry } from ${JSON.stringify(fileUrl("lib/resource.js"))};
  const state = () => globalThis.__campaignShapeTest;
  export const useCampaign = () => campaignEntry(okEntry(state().campaign));
  export const useResource = (_api, path) => okEntry(path?.endsWith('/report') ? state().report
    : path?.includes('/measurements/') ? state().measurement : state().operation);
  export const useOnDemand = (_api, path) => { state().onDemand.push(path); return { state: 'loading' }; };
  export const useLiveRefresh = () => 0;
  export const useAttemptFailures = () => ({});
  export const useMeasurements = () => ({ items: {}, read: { state: 'ok', stale: false } });
`);
const vendorUrl = dataUrl(`export { html } from ${JSON.stringify(fileUrl("vendor/htm-preact.js"))};
  export const useState = (initial) => [initial, () => {}];`);
async function screen(name) {
  const filename = join(process.env.LAB_PANEL_STATIC, `screens/${name}.js`);
  const source = readFileSync(filename, "utf8").replace(/from "([^"]+)"/g, (_, rel) => {
    const url = rel === "../ui/hooks.js" ? hookUrl : rel === "../vendor/htm-preact.js" ? vendorUrl
      : new URL(rel, pathToFileURL(filename)).href;
    return `from ${JSON.stringify(url)}`;
  });
  return import(dataUrl(source));
}
const { CampaignScreen } = await screen("campaign");
const { MeasurementScreen } = await screen("measurement");
const { SuspendedScreen } = await screen("suspended");
const { DiffScreen } = await screen("diff");

// Stateful child controls have their own tests; here we inspect the screen's content and read states.
const controls = new Set(["ReportDownload", "SuspensionHistory", "RoundTree", "CaseBrowser", "CommandWell"]);
function screenText(node) {
  if (node == null || typeof node === "boolean") return "";
  if (Array.isArray(node)) return node.map(screenText).join("");
  if (typeof node !== "object") return String(node);
  if (typeof node.type === "function") return controls.has(node.type.name) ? "" : screenText(node.type(node.props));
  return screenText(node.props?.children);
}
const id = snapshot("family14").meta.ids.campaign_final;
const original = () => structuredClone(body("family14", `/v1/operations/${id}/campaign`));
const operation = body("family14", `/v1/operations/${id}`);
const suspendedId = snapshot("suspended-role").meta.ids.campaign_suspended_role;
const suspendedOperation = body("suspended-role", `/v1/operations/${suspendedId}`);
const measurement = body("family14", `/v1/operations/${id}/measurements/${original().measurements[0].measurement_id}`);
const props = { api: {}, epoch: 0, outage: false, operation: okEntry(operation), data: operation, refresh: 0,
  route: { params: { operation: id, measurement: measurement.measurement.measurement_id, from: "initial", to: null } } };
function renderScreen(Screen, campaign, patch = {}) {
  globalThis.__campaignShapeTest = { campaign, operation, measurement, onDemand: [], ...patch };
  const screenProps = Screen === SuspendedScreen
    ? { ...props, data: suspendedOperation, operation: okEntry(suspendedOperation) } : props;
  try { return screenText(Screen(screenProps)); }
  finally { delete globalThis.__campaignShapeTest; }
}

const malformed = [
  ["rounds object", (c) => { c.rounds = {}; }],
  ["null attempts", (c) => { c.rounds[0].attempts = null; }],
  ["champions object", (c) => { c.champions = {}; }],
];
for (const [name, mutate] of malformed) {
  for (const [screenName, Screen] of [["campaign", CampaignScreen], ["measurement", MeasurementScreen],
    ["suspended", SuspendedScreen], ["diff", DiffScreen]]) {
    test(`${screenName}: /12 with ${name} renders Unavailable without throwing`, () => {
      const campaign = original(); mutate(campaign);
      const text = renderScreen(Screen, campaign);
      assert.match(text, /Unavailable/);
      assert.match(text, /unsupported_schema/);
    });
  }
  test(`direct view consumers: /12 with ${name} is unavailable without throwing`, () => {
    const campaign = original(); mutate(campaign);
    assert.equal(supportedCampaign(campaign), undefined);
    assert.equal(campaignModel({ operation, campaign, measurements: {} }).schema.ok, false);
    assert.equal(measurementModel({ operation, campaign, measurement }).schema.ok, false);
    assert.equal(suspendedModel({ operation: suspendedOperation, campaign }).strip.read.state, "unavailable");
    const state = pairsState(campaign, id);
    assert.equal(state.state, "unavailable");
    assert.deepEqual(state.pairs, []);
  });
}

test("guard covers iterated containers and dereferenced rows before reading scalar values", () => {
  const mutations = [
    (c) => { c.rounds[0] = null; },
    (c) => { c.rounds[0].attempts[0] = null; },
    (c) => { c.measurements = {}; }, (c) => { c.measurements[0] = null; },
    (c) => { c.champions[0] = null; }, (c) => { c.suspensions = {}; },
    (c) => { c.suspensions = [null]; }, (c) => { c.lab_notices = {}; },
    (c) => { c.lab_notices = [null]; }, (c) => { c.caveats = {}; },
    (c) => { c.caveats = [null]; }, (c) => { c.outcome = []; },
    (c) => { c.settings = "bad"; }, (c) => { c.roles = []; },
    (c) => { c.connection = "bad"; }, (c) => { c.progress = []; },
    (c) => { c.policy = "bad"; },
    (c) => { c.settings.baseline_refresh = { performed: {} }; },
    (c) => { c.settings.baseline_refresh = { performed: [null] }; },
    (c) => { c.rounds[0].reflection = { hypotheses: {} }; },
    (c) => { c.rounds[0].reflection = { hypotheses: [null] }; },
    (c) => { c.rounds[0].attempts[0].failure = []; },
    (c) => { c.rounds[0].attempts[0].confidence = "bad"; },
    (c) => { c.rounds[0].final = [{ members: {}, contrasts: [] }]; },
    (c) => { c.rounds[0].final = [{ members: [null], contrasts: [] }]; },
    (c) => { c.rounds[0].final = [{ members: [], contrasts: {} }]; },
    (c) => { c.rounds[0].final = [{ members: [], contrasts: [null] }]; },
  ];
  for (const mutate of mutations) {
    const campaign = original(); mutate(campaign);
    assert.equal(supportedCampaign(campaign), undefined, String(mutate));
    assert.equal(readOf(campaignEntry(okEntry(campaign))).state, "unavailable");
  }
});

test("current snapshots and intentional partial/null fields remain supported", () => {
  assert.equal(typeof campaignViews.campaignShapeOk, "function");
  for (const name of ["running", "suspended-target", "suspended-role", "final", "details", "family14", "family15"]) {
    for (const [path, entry] of Object.entries(snapshot(name).routes)) {
      if (!path.endsWith("/campaign") || entry.status !== 200) continue;
      assert.equal(supportedCampaign(entry.body), entry.body, `${name}: ${path}`);
    }
  }
  const partial = original();
  Object.assign(partial, { champions: null, champions_state: "unavailable", details_state: "unavailable",
    settings: null, roles: null, connection: null, outcome: null, progress: null, caveats: null });
  for (const r of partial.rounds) { r.final = null; r.reflection = null; for (const a of r.attempts) {
    a.failure = null; a.confidence = null; a.final_decision = null; a.has_tree = null;
  } }
  assert.equal(supportedCampaign(partial), partial);
  const model = campaignModel({ operation, campaign: partial, measurements: {} });
  assert.equal(model.schema.ok, true);
  assert.equal(model.lineage.state, "unavailable");
  assert.equal(model.setup.state, "unavailable");
});

test("family 15 uses the round champion caption; family 14 keeps the baseline caption", () => {
  const campaign14 = original();
  const campaign15 = { ...original(), family: 15, independent: null,
    checks: null, checks_state: "present", checks_reason: null,
    reflector_probe: null, reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" };
  assert.match(renderScreen(CampaignScreen, campaign14), /Δ against the round baseline/);
  const text = renderScreen(CampaignScreen, campaign15);
  assert.match(text, /Δ vs round champion/);
  assert.doesNotMatch(text, /Δ against the round baseline/);
  const diff = { read: { state: "ok" }, baseline: "v1", files: [], added: 0, removed: 0 };
  assert.match(inspect(DiffSummary({ diff })).text, /Changes against round baseline v1/);
  assert.match(inspect(DiffSummary({ diff, family: 15 })).text, /Δ vs round champion/);
});


test("measurement screen finds the reflection for the measured attempt's own round", () => {
  const campaign = original();
  const first = campaign.rounds[0];
  const attempt = first.attempts.find((a) => a.measurement_id);
  campaign.rounds = [{ ...structuredClone(first), round: 1, attempts: [{ ...attempt, measurement_id: "m-round1" }],
    reflection: { ...first.reflection, reflection_id: "r01-x01" } },
    { ...structuredClone(first), round: 2, attempts: [{ ...attempt, measurement_id: measurement.measurement.measurement_id }],
      reflection: { ...first.reflection, reflection_id: "r02-x01" } }];
  globalThis.__campaignShapeTest = { campaign, operation, measurement, onDemand: [] };
  try {
    screenText(MeasurementScreen(props));
    assert.ok(globalThis.__campaignShapeTest.onDemand.some((path) => path?.includes("/reflections/r02-x01")));
    assert.ok(!globalThis.__campaignShapeTest.onDemand.some((path) => path?.includes("/reflections/r01-x01")));
  } finally { delete globalThis.__campaignShapeTest; }
});

// Start from the adapted family-15 producer body; extend preflight with its own valid scope record.
const family15 = () => structuredClone(Object.values(snapshot("family15").routes)
  .find((r) => r.status === 200 && r.body?.schema === "lab-campaign-view/15").body);
// Synthetic current form only; the historical producer's bytes remain unchanged.
const checksCampaign = (checks, state = "present") => ({ ...family15(), schema: "lab-campaign-view/15", lab_notices_state: "present",
  checks, checks_state: state, checks_reason: state === "unavailable" ? "unreadable" : null,
  independent: null, details_state: "unavailable", details_reason: "unreadable" });

test("checks follow independent acceptance with each recorded mode and count", () => {
  const campaign = checksCampaign({ modes: { seal_oracle_leak: "off", connection_preflight: "shadow" },
    shadow_hits: { connection_preflight: 2, seal_oracle_leak: 7 } });
  const before = structuredClone(campaign);
  const text = renderScreen(CampaignScreen, campaign);
  assert.match(text, /Check modes/);
  assert.match(text, /seal_oracle_leak=off/);
  assert.match(text, /connection_preflight=shadow/);
  assert.match(text, /shadow hits: connection_preflight · count 2/);
  assert.match(text, /shadow hits: seal_oracle_leak · count 7/);
  assert.match(text, /Counts of different checks are not comparable\./);
  assert.ok(text.indexOf("Independent acceptance") < text.indexOf("Check modes"));
  assert.doesNotMatch(text, /count 9|total 9/);
  assert.deepEqual(campaign, before);
});

test("present null checks hide the block while independent acceptance remains unavailable", () => {
  const campaign = checksCampaign(null);
  assert.equal(campaignModel({ campaign, measurements: {} }).schema.ok, true);
  const text = renderScreen(CampaignScreen, campaign);
  assert.doesNotMatch(text, /Check modes|Counts of different checks/);
  assert.match(text, /Independent acceptance.*Unavailable.*unreadable/s);
});

test("unavailable checks show their own unavailable state after independent acceptance", () => {
  const text = renderScreen(CampaignScreen, checksCampaign(null, "unavailable"));
  assert.match(text, /Independent acceptance.*Check modes.*Unavailable.*unreadable/s);
  assert.doesNotMatch(text, /Counts of different checks|shadow hits:/);
});

test("checks guards reject malformed display values before the screen reads them", () => {
  const valid = checksCampaign({ modes: { neutral_check: "shadow" }, shadow_hits: { neutral_check: 2 } });
  assert.equal(supportedCampaign(valid), valid);
  for (const mutate of [
    (c) => { c.checks.modes.neutral_check = {}; },
    (c) => { c.checks.shadow_hits.neutral_check = "2"; },
    (c) => { c.checks.shadow_hits.neutral_check = -1; },
    (c) => { c.checks.extra = true; },
    (c) => { c.checks_state = {}; },
    (c) => { c.checks_reason = []; },
  ]) {
    const campaign = structuredClone(valid); mutate(campaign);
    assert.equal(readOf(campaignEntry(okEntry(campaign))).code, "unsupported_schema");
    assert.match(renderScreen(CampaignScreen, campaign), /unsupported_schema/);
  }
});
const quantityCampaign = () => {
  const campaign = family15();
  const b = campaign.independent;
  b.attempts[0].comparison.scopes[0].enclosure = { lower: { value: 0, exact: { format: "rational-hex/1", numerator: "0", denominator: "1" } }, upper: null, steps: 1 };
  b.attempts[0].preflight = structuredClone(b.attempts[1].preflight);
  b.attainability.preflight = [{ round: 1, explanation: structuredClone(b.attempts[1].preflight), refresh: null }];
  return campaign;
};
const quantitySlots = [
  ["comparison lower", (b, value) => { b.attempts[0].comparison.scopes[0].enclosure.lower = value; }],
  ["round preflight c_max", (b, value) => { b.attainability.preflight[0].explanation.scopes[0].c_max = value; }],
  ["round preflight delta_max", (b, value) => { b.attainability.preflight[0].explanation.scopes[0].delta_max = value; }],
  ["attempt preflight c_max", (b, value) => { b.attempts[0].preflight.scopes[0].c_max = value; }],
  ["attempt preflight delta_max", (b, value) => { b.attempts[0].preflight.scopes[0].delta_max = value; }],
  ["execution noise allowance", (b, value) => { b.settings.execution_noise.allowance = value; }],
  ["eligible final score", (b, value) => { b.attempts[0].eligible_final_score = value; }],
];
for (const [name, replace] of quantitySlots) {
  test(`family15 ${name}: malformed quantity container makes the campaign unavailable`, () => {
    for (const value of [[], "malformed", 1, false, {}, { value: "uninterpreted", exact: [] }, { value: Infinity, exact: null }]) {
      const campaign = quantityCampaign(); replace(campaign.independent, value);
      const entry = campaignEntry(okEntry(campaign));
      assert.equal(readOf(entry).state, "unavailable");
      assert.equal(readOf(entry).code, "unsupported_schema");
      assert.equal(supportedCampaign(campaign), undefined);
    }
  });
  test(`family15 ${name}: null and valid object quantities retain the supported view`, () => {
    for (const value of [null, undefined, { value: null, exact: null }, { value: 0, exact: { format: "rational-hex/1", numerator: "0", denominator: "1" } }]) {
      const campaign = quantityCampaign(); replace(campaign.independent, value);
      assert.equal(supportedCampaign(campaign), campaign);
      assert.equal(readOf(campaignEntry(okEntry(campaign))).state, "ok");
    }
  });
}

for (const [family, baseline] of [[14, original], [15, family15]]) {
  const mutations = [
    ["string", (c) => { c.champions[0].version = {}; }],
    ["integer", (c) => { c.progress.rounds.completed = "2"; }],
    ["boolean", (c) => { c.rounds[0].attempts[0].has_tree = 1; }],
    ["native number", (c) => { Object.values(c.measurements[0].metrics)[0].mean = "1"; }],
    ...(family === 15 ? [
      ["statistic wrapper", (c) => { c.independent.attempts[0].global_comparison.scopes[0].point = 1; }],
      ["reference id", (c) => { c.independent.attempts[0].global_comparison.reference_measurement_id = {}; }],
    ] : [["native margin", (c) => { c.rounds[0].attempts[0].confidence.margin = { value: 0, exact: null }; }]]),
  ];
  for (const [name, Screen] of [["campaign", CampaignScreen], ["measurement", MeasurementScreen],
    ["suspended", SuspendedScreen], ["diff", DiffScreen]]) {
    test(`${name}: valid family ${family} /12 renders`, () => {
      const text = renderScreen(Screen, baseline());
      assert.doesNotMatch(text, /unsupported_schema/);
      assert.ok(text.length > 0);
    });
    for (const [kind, mutate] of mutations) test(`${name}: wrong family ${family} ${kind} renders Unavailable`, () => {
      const c = baseline(); mutate(c);
      const text = renderScreen(Screen, c);
      assert.match(text, /Unavailable/);
      assert.match(text, /unsupported_schema/);
    });
  }
}

for (const [name, Screen] of [["campaign", CampaignScreen], ["measurement", MeasurementScreen],
  ["suspended", SuspendedScreen], ["diff", DiffScreen]]) {
  test(`${name}: pending final member with null completion facts keeps /12 supported`, () => {
    const c = original();
    Object.assign(c.rounds[0].final[0].members[0], { complete: null, point: null, missing: null, critical: null });
    assert.equal(readOf(campaignEntry(okEntry(c))).state, "ok");
    const text = renderScreen(Screen, c);
    assert.doesNotMatch(text, /unsupported_schema/);
    assert.ok(text.length > 0);
  });
}

// Synthetic Owner facts layered over an in-memory copy; captures remain producing evidence.
test("reflector probe Owner record renders even when independent acceptance is unavailable", () => {
  const campaign = { ...checksCampaign(null, "unavailable"),
    schema: "lab-campaign-view/15", lab_notices_state: "present", reflector_probe_state: "present", reflector_probe_reason: null,
    reflector_probe: { launched_by: "operator", state: "stopped", restarts: 0, last_exit: null } };
  assert.equal(supportedCampaign(campaign), campaign);
  const text = renderScreen(CampaignScreen, campaign);
  assert.match(text, /Reflector probe/);
  assert.match(text, /Launched by: operator/);
  assert.match(text, /State: stopped/);
  assert.match(text, /Restarts: 0/);
  assert.match(text, /Last exit: —/);
  assert.match(renderScreen(CampaignScreen, { ...campaign, reflector_probe: { ...campaign.reflector_probe,
    launched_by: "lab", last_exit: 0 } }), /Last exit: 0/);
  assert.match(text, /Owner record/);
  const family14 = { ...original(), reflector_probe: campaign.reflector_probe, reflector_probe_state: "present", reflector_probe_reason: null };
  assert.equal(supportedCampaign(family14), family14);
  assert.match(renderScreen(CampaignScreen, family14), /Reflector probe.*Launched by: operator/);
  for (const patch of [{ restarts: true }, { restarts: -1 }, { last_exit: false }, { launched_by: null }, { extra: 1 },
    { state: "not_declared", launched_by: null, restarts: 1 }, { state: [] }]) {
    assert.equal(supportedCampaign({ ...campaign, reflector_probe: { ...campaign.reflector_probe, ...patch } }), undefined);
  }
  assert.equal(supportedCampaign({ ...campaign, schema: "lab-campaign-view/11" }), undefined);
  const absent = { ...campaign, reflector_probe: { launched_by: null, state: "not_declared", restarts: 0, last_exit: null } };
  assert.doesNotMatch(renderScreen(CampaignScreen, absent), /Reflector probe/);
  assert.match(renderScreen(CampaignScreen, { ...campaign, reflector_probe: null,
    reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" }), /Reflector probe.*Unavailable/);
});

test("baseline notice requires its exact typed evidence fields", () => {
  const good = { code: "baseline_provenance_unknown", round: 1, measurement_id: "m-r00-champion", samples: 2 };
  const valid = original(); valid.lab_notices = [good];
  assert.notEqual(supportedCampaign(valid), undefined);
  for (const notice of [
    {code: good.code, round: 1, samples: 2},
    {...good, extra: true}, {...good, round: -1}, {...good, round: true},
    {...good, measurement_id: ""}, {...good, samples: 0}, {...good, samples: "2"},
  ]) {
    const campaign = original(); campaign.lab_notices = [notice];
    assert.equal(supportedCampaign(campaign), undefined, JSON.stringify(notice));
  }
});
