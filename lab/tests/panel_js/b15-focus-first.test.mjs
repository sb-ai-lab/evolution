import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { staticModule, inspect } from "./support.mjs";
const { supportedCampaign } = await staticModule("lib/views/campaign.js");
const { independentModel } = await staticModule("lib/views/independent.js");
const { IndependentBlock } = await staticModule("ui/independent.js");
const { label } = await staticModule("lib/labels.js");
const campaign = JSON.parse(readFileSync(process.env.LAB_FOCUS_FIRST_CAMPAIGN ||
  join(process.env.LAB_PANEL_FIXTURES, "b15/focus-first/campaign.json"), "utf8"));
test("genuine focus-first stages render without closed slot accounting", () => {
  assert.ok(supportedCampaign(campaign));
  const model = independentModel(campaign);
  assert.equal(model.state, "present");
  const rendered = inspect(IndependentBlock({model})).text;
  assert.doesNotMatch(rendered, /slots:|not_owed|dispatched/);
  assert.ok(rendered.includes("Stages focus first — causal measured only after focus passes"));
  assert.ok(rendered.includes("focus rejected, causal cases were not run"));
  assert.ok(rendered.includes("Not measured"));
  for (const attempt of model.attempts) assert.equal(attempt.slotProgress, null);
  assert.deepEqual(label("scope_status", "not_measured"), {text: "Not measured", tone: "neutral", known: true});
});
test("unknown scope status and malformed slot accounting are refused", () => {
  for (const mutate of [
    c => { c.independent.attempts[0].gate_comparison.scopes[1].status = "unknown"; },
    c => { c.independent.attempts[0].slots.candidate.not_owed = -1; },
    c => { c.independent.attempts[0].slots.candidate.extra = 1; },
    c => { c.independent.settings.stages = "unknown"; },
    c => { delete c.independent.attempts[0].stages; },
    c => { c.independent.attempts[0].stages = null; },
    c => { c.independent.attempts[0].stages.reverse(); },
    c => { c.independent.attempts[0].stages[0].state = "future"; },
    c => { c.independent.attempts[0].stages[0].extra = 1; },
    c => { delete c.independent.attempts[0].slots; },
    c => { delete c.independent.settings.stages; },
  ]) {
    const changed = structuredClone(campaign); mutate(changed);
    assert.equal(supportedCampaign(changed), undefined);
  }
});
