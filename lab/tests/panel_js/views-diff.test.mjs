import { test } from "node:test";
import assert from "node:assert/strict";
import { body, canonical, snapshot, staticModule } from "./support.mjs";

const { diffModel, diffPairs, pairsState, refName } = await staticModule("lib/views/diff.js");
const ids = (name) => snapshot(name).meta.ids;

test("pairs: every link points at a captured route", () => {
  const id = ids("details").campaign_mixed;
  const campaign = body("details", `/v1/operations/${id}/campaign`);
  const pairs = diffPairs(campaign, id);
  assert.equal(pairs[0].to, null);
  for (const p of pairs) {
    const path = p.to === null ? `/v1/operations/${id}/diff` : `/v1/operations/${id}/diff?from=${p.from}&to=${p.to}`;
    assert.ok(snapshot("details").routes[canonical(path)], path);
  }
});

test("model: title from the answer's refs, files with +/-", () => {
  const id = ids("details").campaign_mixed;
  const diff = body("details", `/v1/operations/${id}/diff`);
  const model = diffModel({ diff });
  assert.equal(model.title, `${refName(diff.to)} against ${refName(diff.from)}`);
  assert.equal(model.files.length > 0, true);
});

test("model: nontext-only answer lists its files without +/-", () => {
  const model = diffModel({ diff: { from: "initial", to: "champion:v1", source_digest: "a".repeat(64), selected_digest: "b".repeat(64),
                                    state: "changed", text: "", nontext_changes: [{ path: "x.bin", kind: "binary" }] } });
  assert.deepEqual(model.files.map((f) => [f.path, f.added, f.markers]), [["x.bin", null, ["binary"]]]);
});

test("model: no_change says so", () => {
  const model = diffModel({ diff: { from: "initial", to: "champion:v0", source_digest: "a".repeat(64), selected_digest: "a".repeat(64),
                                    state: "no_change", text: "", nontext_changes: [] } });
  assert.equal(model.state, "no_change");
  assert.deepEqual(model.files, []);
});

test("pairsState: pairs present, champions unavailable, not started, present but empty", () => {
  const id = ids("details").campaign_mixed;
  const campaign = body("details", `/v1/operations/${id}/campaign`);
  assert.equal(pairsState(campaign, id).state, "present");
  const base = { ...campaign, champions: null, rounds: [] };
  assert.equal(pairsState({ ...base, champions_state: "unavailable" }, id).state, "unavailable");
  assert.equal(pairsState({ ...base, champions_state: "not_started" }, id).state, "not_started");
  assert.deepEqual(pairsState({ ...base, champions: [], champions_state: "present" }, id), { state: "empty", pairs: [], attempts: null });
});

// Codex r1: an attempt pair needs its round's baseline_version (the details read) — unknown, not absent.
test("pairsState: details unavailable -> attempt pairs are unavailable with the details reason", () => {
  const id = ids("details").campaign_mixed;
  const campaign = structuredClone(body("details", `/v1/operations/${id}/campaign`));
  assert.equal(pairsState(campaign, id).attempts, null);
  assert.equal(diffPairs(campaign, id).some((p) => p.to?.startsWith("attempt:")), true);
  campaign.details_state = "unavailable"; campaign.details_reason = "unreadable";
  for (const r of campaign.rounds) r.baseline_version = null;
  const state = pairsState(campaign, id);
  assert.equal(state.attempts, "unreadable");
  assert.equal(state.pairs.some((p) => p.to?.startsWith("attempt:")), false);
  assert.equal(pairsState({ ...campaign, details_reason: null }, id).attempts, "unavailable");
});
