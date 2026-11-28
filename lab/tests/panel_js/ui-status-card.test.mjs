import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule, inspect } from "./support.mjs";
const { StatusCard } = await staticModule("ui/status-card.js");
const { statusCardOf } = await staticModule("lib/views/status-card.js");
const ok = (data) => ({ state: "ok", data, stale: false });
const op = { control_state: "finished", progress: { rounds: { current: 1, completed: 1, planned: 2 } } };
const failed = { state: "error", code: "network_error" };

test("card retains operation verdict when campaign view is unavailable", () => {
  const card = statusCardOf(ok(op), failed, { screen: "campaign" });
  const rendered = inspect(StatusCard({ card }));
  assert.match(rendered.text, /Outcome unknown.*network_error/);
  assert.match(rendered.text, /Unavailable.*network_error/);
  assert.ok(rendered.elements.some((e) => e.props?.class === "status-card-codes"));
});

test("card presents champions and entries with expandable notice text", () => {
  const card = { columns: {
    now: { state: "ok", pill: { text: "Finished" }, verdict: "Finished.", codes: ["outcome completed"] },
    progress: { state: "ok", title: "Where it stopped", round: 1, planned: 2, completed: 1, closed: 2, running: 0 },
    result: { state: "ok", champions: { count: 2, href: "#champion-lineage" },
      entries: [{ text: "First", code: "first" }, { text: "Second", code: "second" },
        { text: "Third", code: "third", explanation: "Full explanation" }],
      visible: [{ text: "First", code: "first" }, { text: "Second", code: "second" }], more: 1 }
  } };
  const rendered = inspect(StatusCard({ card }));
  assert.match(rendered.text, /2 champions recorded/);
  assert.match(rendered.text, /\+1 more/);
  const details = rendered.elements.find((e) => e.type === "details");
  assert.ok(details);
  assert.equal(details.props.open, undefined);
  assert.match(inspect(details.props.children[1]).text, /Full explanation/);
  assert.doesNotMatch(inspect(details.props.children[0]).text, /Full explanation/);
  assert.ok(rendered.elements.some((e) => e.type === "button" && e.props.class === "link status-card-link"));
});

test("baseline notice presents its round, measurement and samples without count labels", async () => {
  const { resultOf } = await staticModule("lib/views/campaign.js");
  for (const state of ["present", "unavailable"]) {
    const result = resultOf({ lab_notices_state: state, lab_notices: state === "present"
      ? [{ code: "baseline_provenance_unknown", round: 2, measurement_id: "m-r01-baseline", samples: 17 }] : [] }, op);
    const entries = result.notices.map((n) => ({ ...n, kind: "notice" }));
    const card = { columns: { now: { state: "ok", codes: [] }, progress: { state: "ok", missing: true },
      result: { state: "ok", entries, visible: entries, more: 0 } } };
    const text = inspect(StatusCard({ card })).text;
    assert.doesNotMatch(text, /by reason|rounds 2/);
    if (state === "present") {
      assert.match(text, /No stored baseline provenance/);
      assert.match(text, /round 2/);
      assert.match(text, /m-r01-baseline/);
      assert.match(text, /17 samples/);
    } else assert.match(text, /notices are unavailable/);
  }
});
