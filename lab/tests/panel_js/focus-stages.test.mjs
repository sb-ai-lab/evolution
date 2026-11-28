import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { staticModule, inspect } from "./support.mjs";

const { supportedCampaign, campaignModel } = await staticModule("lib/views/campaign.js");
const { independentModel } = await staticModule("lib/views/independent.js");
const { IndependentBlock } = await staticModule("ui/independent.js");
const { label } = await staticModule("lib/labels.js");
const campaign = JSON.parse(readFileSync(process.env.LAB_FOCUS_FIRST_CAMPAIGN, "utf8"));
const rendered = (c) => inspect(IndependentBlock({ model: independentModel(c) }));
const changed = (mutate) => { const c = structuredClone(campaign); mutate(c.independent.attempts[0], c); return c; };

for (const [state, text, tone] of [
  ["waiting", "waiting", "neutral"], ["open", "open", "blue"], ["passed", "passed", "green"],
  ["rejected", "rejected", "neutral"], ["unavailable", "unavailable", "amber"],
  ["not_run", "not run", "neutral"], ["incomplete", "incomplete", "amber"], ["unknown", "—", "neutral"],
]) test(`stage ${state} renders its shared state label`, () => {
  const c = changed((a) => { a.stages[0].state = state; });
  assert.ok(supportedCampaign(c));
  assert.deepEqual(label("stage_state", state), { text, tone, known: true });
  const cell = rendered(c).elements.find((n) => n.props["data-stage"] === "focus");
  assert.ok(cell, "focus stage cell");
  assert.equal(cell.props["data-state"], state);
  assert.ok(inspect(cell).text.includes(`1 · focus${text}`));
});

test("not-run causal cell is muted and explains the focus result", () => {
  const result = rendered(campaign);
  const cell = result.elements.find((n) => n.props["data-stage"] === "causal" && n.props["data-state"] === "not_run");
  assert.match(cell.props.class, /\bmuted\b/);
  assert.ok(result.text.includes("2 · causalnot run"));
  assert.ok(result.text.includes("focus rejected, causal cases were not run"));
  const unavailable = rendered(changed((a) => { a.stages[0].state = "unavailable"; })).text;
  assert.ok(unavailable.includes("focus could not be assessed, causal cases were not run"));
  assert.ok(!unavailable.includes("focus rejected, causal cases were not run"));
});

test("causal explanation requires not_measured and the shared focus result", () => {
  for (const state of ["waiting", "open", "passed", "not_run", "incomplete", "unknown"]) {
    const result = rendered(changed((a) => { a.stages[0].state = state; })).text;
    assert.doesNotMatch(result, /causal cases were not run/);
  }
  const result = rendered(changed((a) => { a.gate_comparison.scopes[1].status = "unavailable"; })).text;
  assert.doesNotMatch(result, /causal cases were not run/);
});

test("open attempts show only Core candidate and optional parent dispatch progress", () => {
  for (const parent of [true, false]) {
    const c = changed((a, c) => {
      c.independent.attempts = [a];
      a.outcome = null; a.group = "in_progress";
      a.slots.candidate = { scheduled: 11, dispatched: 3, not_owed: 8 };
      if (parent) a.slots.parent = { scheduled: 7, dispatched: 2, not_owed: 5 };
      else delete a.slots.parent;
    });
    assert.ok(supportedCampaign(c));
    const result = rendered(c).text;
    assert.ok(result.includes("candidate dispatched 3 of 11 slots"));
    assert.equal(result.includes("parent dispatched 2 of 7 slots"), parent);
    assert.doesNotMatch(result, /not_owed|scheduled|slots:/);
    assert.equal(independentModel(c).attempts[0].slots, undefined);
  }
});

test("an open stage never claims running for suspended or stopped operations", () => {
  for (const status of ["suspended", "stopped"]) {
    const c = changed((a, c) => {
      a.outcome = null;
      a.stages[0].state = "open"; a.stages[1].state = "waiting";
    });
    const model = campaignModel({ operation: { control_state: status === "suspended" ? "released" : "finished",
      resume: status === "suspended" ? { state: "ready" } : null }, campaign: c, measurements: {} });
    const result = inspect(IndependentBlock({ model: model.independent }));
    assert.doesNotMatch(model.header.status.text + result.text, /running/i);
    assert.ok(result.elements.some((n) => n.props["data-stage"] === "focus" && n.props["data-state"] === "open"));
  }
});

test("stages remain a closed conditional two-cell form", () => {
  for (const mutate of [
    (a) => { a.stages[1].state = "running"; },
    (a) => { a.stages[1].extra = true; },
    (a) => { a.stages.push({ scope: "global", state: "waiting" }); },
    (a) => { delete a.stages; },
    (a) => { a.stages[0].scope = "causal"; },
    (a, c) => { delete c.independent.settings.stages; },
    (a) => { delete a.slots; },
  ]) assert.equal(supportedCampaign(changed(mutate)), undefined);
});

test("current default reads render exactly the dev63 reference trees", () => {
  const defaults = JSON.parse(readFileSync(process.env.LAB_DEFAULT_CAMPAIGNS, "utf8"));
  for (const name of ["agent-failed", "single-stage"]) {
    const c = defaults[name];
    assert.ok(supportedCampaign(c));
    assert.equal(c.independent.settings.stages, undefined);
    const result = spawnSync(process.execPath, [fileURLToPath(new URL("../dev63_default_panel.mjs", import.meta.url))],
      { input: JSON.stringify(c), encoding: "utf8", env: process.env });
    assert.equal(result.status, 0, result.stderr);
    const reference = JSON.parse(readFileSync(new URL(`../data/dev63-default-outputs/${name}/panel-independent.json`, import.meta.url), "utf8"));
    assert.deepEqual(JSON.parse(result.stdout), reference, name);
  }
});
