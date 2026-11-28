import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { staticModule, currentForm } from "./support.mjs";

const { supportedCampaign, setupOf } = await staticModule("lib/views/campaign.js");
const { independentModel } = await staticModule("lib/views/independent.js");

test("real b15 insufficient-evidence closure is accepted by the panel", () => {
  const path = process.env.LAB_B15_CAMPAIGN || join(process.env.LAB_PANEL_FIXTURES,
    "b15/insufficient-evidence/campaign.json");
  const campaign = currentForm(JSON.parse(readFileSync(path, "utf8")));
  assert.ok(supportedCampaign(campaign), "genuine Core closure must remain readable");
  assert.equal(independentModel(campaign).state, "present");
  assert.equal(campaign.independent.attempts[0].outcome, "insufficient_evidence");
  assert.equal(campaign.independent.attempts[0].reason, "incomplete_sample");
});

test("recorded b14 refresh-off form stays readable; unknown refresh strings are refused", () => {
  const campaign = currentForm(JSON.parse(readFileSync(new URL(
    "../data/dev62-default-outputs/agent-failed/campaign.json", import.meta.url), "utf8")));
  assert.equal(campaign.settings.baseline_refresh, "off");
  assert.ok(supportedCampaign(campaign), "Core's existing off value is a supported form");
  const setup = setupOf(campaign, campaign.operation_id);
  assert.equal(setup.refresh, null, "off does not create a cadence with missing values");
  assert.ok(!setup.summary.some((row) => row.name === "Baseline refresh"));
  campaign.settings.baseline_refresh = "unknown";
  assert.equal(supportedCampaign(campaign), undefined);
});
