import { test } from "node:test";
import assert from "node:assert/strict";
import { inspect, snapshot, staticModule } from "./support.mjs";

const snap = snapshot("content");
const bodies = (schema) => Object.values(snap.routes).map((r) => r.body).filter((b) => b?.schema === schema);

test("sample model shows the dialog, the verdicts and hides private without the flag", async () => {
  const { sampleOf } = await staticModule("lib/views/content.js");
  const pub = bodies("lab-sample/1").find((b) => b.disclosure === "public" && b.scoring.state === "admitted");
  const model = sampleOf(pub);
  assert.equal(model.state, "available");
  assert.ok(model.turns.length > 0);
  assert.deepEqual(model.private, { withheld: true });
  // Spec family14 §3.7: the turn's input is message.content, the dialogue is its assistant_message events,
  // everything else (tool_call here) sits in technical, in recorded order; usage is a partial technical row.
  const raw = pub.turns[0], turn = model.turns[0];
  assert.equal(turn.index, raw.index);
  assert.equal(turn.input, raw.message.content);
  const said = raw.events.filter((e) => e.type === "assistant_message");
  assert.ok(said.length > 0 && raw.events.some((e) => e.type === "tool_call"));
  assert.deepEqual(turn.dialogue, said.map((e) => ({ type: "assistant_message", name: e.name ?? null, text: e.content })));
  const others = raw.events.filter((e) => e.type !== "assistant_message");
  assert.deepEqual(turn.technical.slice(0, others.length).map((e) => [e.type, e.name]), others.map((e) => [e.type, e.name ?? null]));
  const usage = turn.technical.at(-1);
  assert.equal(usage.type, "usage");
  assert.equal(usage.note, "Partial: target calls only");
  assert.equal(usage.text, `${Object.entries(raw.usage).map(([k, v]) => `${k} ${v}`).join(" · ")} · latency_ms ${raw.latency_ms}`);
  assert.equal(turn.technical.length, others.length + 1);
  assert.deepEqual(turn.fields, raw.message.fields);
  assert.equal(turn.count, turn.technical.length + 1); // + message.fields
  assert.equal(turn.noAssistant, false);
  assert.equal(turn.outcome, raw.outcome);
  assert.deepEqual(turn.reason, raw.reason);
  // A synthetic ordinary tool event may supply several fields at once; none supersedes the others.
  const tools = sampleOf({ ...pub, turns: [{ index: 0, events: [
    { type: "tool_call", name: "read_file", text: "request", arguments: { path: "a.md" },
      result: { ok: true }, content: "extra" },
    { type: "tool_result", name: "read_file", text: "body", result: { bytes: 4 } },
  ] }] }).turns[0].technical;
  assert.equal(tools[0].name, "read_file");
  assert.equal(tools[0].text, 'text: request\narguments: {\n  "path": "a.md"\n}\nresult: {\n  "ok": true\n}\ncontent: extra');
  assert.equal(tools[1].name, "read_file");
  assert.equal(tools[1].text, 'text: body\nresult: {\n  "bytes": 4\n}');
  const priv = sampleOf(bodies("lab-sample/1").find((b) => b.disclosure === "private" && b.case_id === pub.case_id
    && b.sample_id === pub.sample_id && b.measurement_id === pub.measurement_id));
  assert.ok("oracle" in priv.private);
});

const publicSample = () => structuredClone(bodies("lab-sample/1").find((b) => b.disclosure === "public" && b.scoring.state === "admitted"));

test("trace: unknown event type goes to technical and is counted", async () => {
  const { sampleOf } = await staticModule("lib/views/content.js");
  const body = publicSample();
  body.turns = [{ index: 3, message: { content: "hi" }, events: [
    { type: "subprocess.stdout", text: "line" },
    { type: "assistant_message", content: "answer" },
    { type: "future_kind", payload: { k: 1 }, extra: 0 },
  ], outcome: "ok" }];
  const turn = sampleOf(body).turns[0];
  assert.deepEqual(turn.dialogue, [{ type: "assistant_message", name: null, text: "answer" }]);
  assert.deepEqual(turn.technical.map((e) => e.type), ["subprocess.stdout", "future_kind"]);
  assert.equal(turn.technical[1].text, 'payload: {\n  "k": 1\n}\nextra: 0');
  assert.equal(turn.fields, undefined); // not recorded: no row, not counted
  assert.equal(turn.reason, undefined);
  assert.equal(turn.count, 2);
  assert.equal(turn.noAssistant, false);
});

test("trace: a turn without assistant_message says so", async () => {
  const { sampleOf } = await staticModule("lib/views/content.js");
  const body = publicSample();
  body.turns = [{ index: 0, message: { content: "question", fields: null }, events: [{ type: "tool_call", name: "lookup" }],
    outcome: "failed", reason: { category: "agent_outcome", code: "no_answer" } }];
  const turn = sampleOf(body).turns[0];
  assert.equal(turn.input, "question");
  assert.deepEqual(turn.dialogue, []);
  assert.equal(turn.noAssistant, true);
  assert.deepEqual(turn.technical, [{ type: "tool_call", name: "lookup", text: "" }]);
  assert.equal(turn.fields, null);
  assert.equal(turn.count, 2); // the tool_call and the recorded message.fields: null
});

// Final review item 9: an assistant_message's recorded name stays in its label; its text is still the content alone.
test("trace: an assistant_message keeps its name in the label", async () => {
  const { sampleOf, saidLabel } = await staticModule("lib/views/content.js");
  const body = publicSample();
  body.turns = [{ index: 4, message: { content: "q" }, events: [
    { type: "assistant_message", name: "planner", content: "answer" },
    { type: "assistant_message", content: "plain" },
  ] }];
  const turn = sampleOf(body).turns[0];
  assert.deepEqual(turn.dialogue, [{ type: "assistant_message", name: "planner", text: "answer" },
    { type: "assistant_message", name: null, text: "plain" }]);
  assert.deepEqual(turn.dialogue.map((d) => saidLabel(turn.index, d)),
    ["Turn 4 · assistant_message · planner", "Turn 4 · assistant_message"]);
});

// Global constraint (spec family14 §2; mock RULES): a recorded null is shown and counted, never dropped by `?? null`.
test("trace: a recorded null message.fields and turn reason are kept; absent ones are not invented", async () => {
  const { sampleOf } = await staticModule("lib/views/content.js");
  const body = publicSample();
  body.turns = [{ index: 1, message: { content: "q", fields: null }, events: [], reason: null },
    { index: 2, events: [] }];
  const [recorded, absent] = sampleOf(body).turns;
  assert.equal(recorded.fields, null);
  assert.equal(recorded.reason, null);
  assert.equal(recorded.count, 1);
  assert.equal(absent.fields, undefined);
  assert.equal(absent.reason, undefined);
  assert.equal(absent.count, 0);
});

// Task 10 item 3: a private read that fails or is still loading never replaces the public answer already shown;
// its own state goes to the private block only.
test("reveal: the public answer stays while the private read loads or fails", async () => {
  const { revealOf } = await staticModule("lib/views/content.js");
  const { LOADING, errorEntry, okEntry } = await staticModule("lib/resource.js");
  const pub = okEntry({ disclosure: "public", private: { withheld: "private" } });
  const priv = okEntry({ disclosure: "private", private: { oracle: "o" } });
  assert.deepEqual(revealOf(pub, LOADING, false), { read: { state: "ok", code: null, stale: false }, body: pub.data, privateRead: null });
  const loading = revealOf(pub, LOADING, true);
  assert.equal(loading.body, pub.data);
  assert.equal(loading.read.state, "ok");
  assert.equal(loading.privateRead.state, "loading");
  const failed = revealOf(pub, errorEntry("not_found", undefined), true);
  assert.equal(failed.body, pub.data);
  assert.deepEqual(failed.privateRead, { state: "unavailable", code: "not_found", stale: false });
  const shown = revealOf(pub, priv, true);
  // Codex r1: only the private sections come from the private read; the body stays the public answer.
  assert.deepEqual(shown.body.private, priv.data.private);
  assert.equal(shown.body.disclosure, "public");
  assert.equal(shown.read.state, "ok");
  assert.equal(shown.privateRead.state, "ok");
  // without a public answer the case body keeps the public read's own state
  assert.equal(revealOf(errorEntry("sample_missing", undefined), LOADING, true).read.code, "sample_missing");
});

// Codex r1 private-disclosure-section-coupling: Owner budgets each response's sections on its own, so the public
// body comes from the public read and only `private` (with its truncation marks) from the private one.
const TOO_LARGE = Object.freeze({ state: "unavailable", reason: "too_large" });
const revealPair = () => {
  const pub = publicSample();
  const priv = structuredClone(bodies("lab-sample/1").find((b) => b.disclosure === "private" && b.case_id === pub.case_id
    && b.sample_id === pub.sample_id && b.measurement_id === pub.measurement_id && b.operation_id === pub.operation_id));
  return { pub, priv };
};
test("reveal: readable public turns stay when the private answer's turns are too large", async () => {
  const { revealOf, sampleOf } = await staticModule("lib/views/content.js");
  const { okEntry } = await staticModule("lib/resource.js");
  const { pub, priv } = revealPair();
  assert.ok(Array.isArray(pub.turns) && pub.turns.length > 0);
  priv.turns = { ...TOO_LARGE };
  priv.scoring = { ...TOO_LARGE };
  priv.terminal = { ...TOO_LARGE };
  priv.private.diagnostics = "judge diagnostics";
  priv.truncated = true;
  priv.truncation = [{ path: "/private/details", size: 9 }, { path: "/input/public", size: 5 }];
  const before = structuredClone(pub);
  const view = revealOf(okEntry(pub), okEntry(priv), true);
  const model = sampleOf(view.body);
  assert.equal(model.turns.length, pub.turns.length);
  assert.equal(model.turns[0].input, sampleOf(pub).turns[0].input);
  assert.equal(model.scoring.state, pub.scoring.state);
  assert.equal(model.terminal, sampleOf(pub).terminal);
  assert.equal(model.private.diagnostics, "judge diagnostics");
  assert.deepEqual(model.private.oracle, priv.private.oracle);
  // the private sections' truncation is kept; the private answer's other marks are not the public body's
  assert.deepEqual(model.truncation, [{ path: "/private/details", size: 9 }]);
  assert.equal(model.truncated, true);
  assert.deepEqual(pub, before);
  // A too-large private section is shown as such; the public sections are untouched.
  priv.private = { ...TOO_LARGE };
  const large = sampleOf(revealOf(okEntry(pub), okEntry(priv), true).body);
  assert.deepEqual(large.private, TOO_LARGE);
  assert.equal(large.turns.length, pub.turns.length);
});

test("verdict: public scoring and private diagnostics render as siblings with their own guards", async () => {
  const { revealOf, sampleOf } = await staticModule("lib/views/content.js");
  const { LOADING, okEntry } = await staticModule("lib/resource.js");
  const { Verdict } = await staticModule("ui/content.js");
  const PRIVATE = "Private · judge reasoning and diagnostics";
  const privateBlocks = (node) => inspect(node).elements.filter((n) => n.props?.class === "content-private");
  const render = (pub, privEntry) => {
    const view = revealOf(okEntry(pub), privEntry, true);
    return Verdict({ model: sampleOf(view.body), shown: true, privateRead: view.privateRead });
  };
  // a too-large public scoring keeps the private diagnostics that were returned
  let { pub, priv } = revealPair();
  pub.scoring = { ...TOO_LARGE };
  priv.private.diagnostics = "judge diagnostics";
  const out = render(pub, okEntry(priv));
  const text = inspect(out).text;
  assert.ok(text.includes("Judge verdict") && text.includes("too_large"));
  assert.ok(text.includes(PRIVATE) && text.includes("judge diagnostics"));
  // not nested: the public scoring Section holds no private block
  assert.ok(Array.isArray(out));
  assert.equal(privateBlocks(out[0]).length, 0);
  assert.equal(privateBlocks(out.slice(1)).length, 1);
  // ... and while the private read loads, its state shows in the private block only
  const loading = inspect(render(pub, LOADING)).text;
  assert.ok(loading.includes("Loading private content…") && loading.includes("too_large"));
  // the reverse: a too-large private section keeps the public verdict
  ({ pub, priv } = revealPair());
  pub.scoring.public_feedback = "public feedback";
  priv.private = { ...TOO_LARGE };
  const reverse = render(pub, okEntry(priv));
  const reverseText = inspect(reverse).text;
  assert.ok(reverseText.includes("public feedback") && reverseText.includes(pub.scoring.verdicts[0].criterion));
  assert.ok(reverseText.includes(PRIVATE) && reverseText.includes("too_large"));
  assert.equal(privateBlocks(reverse[0]).length, 0);
  // judge notes stay private (spec §4.6): without the reveal, no private block and no notes
  const hidden = inspect(Verdict({ model: sampleOf(pub), shown: false, privateRead: null }));
  assert.ok(hidden.text.includes("reasoning private") && !hidden.text.includes(PRIVATE));
});

// Task 10 item 11: the case head and the side say critical the same way.
test("critical wording is one on the case head and the side", async () => {
  const { criticalText } = await staticModule("lib/views/content.js");
  assert.deepEqual([true, false, null, undefined].map(criticalText), ["critical pass", "critical fail", "critical —", "critical —"]);
});

// Task 10 item 4: the case detail is keyed by the selected row, so its disclosures start closed on another case.
test("case browser keys the detail by the selected row", async () => {
  const { CaseBrowser } = await staticModule("ui/cases.js");
  const rows = [{ key: "a", cells: ["a · 1"] }, { key: "b", cells: ["b · 1"] }];
  const detail = (r) => ({ type: "i", props: { "data-row": r.key, children: [] } });
  const rendered = inspect(CaseBrowser({ columns: [{ text: "Case" }], rows, selected: "b", onSelect: () => {}, detail }));
  const holder = rendered.elements.find((n) => n.props?.class === "case-detail-body");
  assert.equal(holder.key, "b");
  assert.ok(rendered.elements.some((n) => n.props?.["data-row"] === "b"));
});

test("trace: private parts stay withheld without disclose", async () => {
  const { sampleOf } = await staticModule("lib/views/content.js");
  const body = publicSample();
  // Only criterion, passed and public_feedback are public (PDR-0017); a stray note never reaches the view.
  body.scoring.verdicts = [{ criterion: "improved", passed: false, note: "judge reasoning" }];
  body.scoring.public_feedback = "first line missing";
  const model = sampleOf(body);
  assert.deepEqual(model.private, { withheld: true });
  assert.deepEqual(model.scoring.verdicts, [{ criterion: "improved", passed: false }]);
  assert.equal(model.scoring.feedback, "first line missing");
  assert.ok(!JSON.stringify(model).includes("judge reasoning"));
  // A private-marked event value is shown as withheld, not as its content.
  body.turns = [{ index: 0, events: [{ type: "assistant_message", content: { withheld: "private" } },
    { type: "tool_call", arguments: { withheld: "private" } }] }];
  const turn = sampleOf(body).turns[0];
  assert.deepEqual(turn.dialogue, [{ type: "assistant_message", name: null, text: "(private)" }]);
  assert.equal(turn.technical[0].text, "arguments: (private)");
});

test("baseline side is paired only by the measurement row of the same case and sample", async () => {
  const { baselinePairOf } = await staticModule("lib/views/content.js");
  const m = structuredClone(Object.values(snap.routes).map((r) => r.body)
    .find((b) => b?.schema === "lab-measurement/3" && b.measurement?.role === "attempt" && b.measurement.samples?.some((s) => s.baseline))).measurement;
  const s = m.samples.find((x) => x.baseline);
  assert.deepEqual(baselinePairOf(m, s.case_id, s.sample_id), { measurementId: m.baseline.measurement_id, sampleId: s.sample_id });
  // No baseline measurement named by the answer: nothing to read, whatever the row says.
  assert.deepEqual(baselinePairOf({ ...m, baseline: null }, s.case_id, s.sample_id), { unpaired: s.comparison });
  s.baseline = null;
  s.comparison = "unpaired";
  assert.deepEqual(baselinePairOf(m, s.case_id, s.sample_id), { unpaired: "unpaired" });
  assert.deepEqual(baselinePairOf(m, "no-such-case", s.sample_id), { unpaired: "unknown" });
});

test("reflector events stay withheld in the public answer", async () => {
  const { roleOf } = await staticModule("lib/views/content.js");
  const pub = bodies("lab-reflection/1").find((b) => b.disclosure === "public" && b.kind === "chat");
  const model = roleOf(pub, "reflector");
  assert.ok(model.events.some((e) => e.type === "input" && e.withheld));
  assert.ok(model.events.every((e) => e.type !== "model_message" || e.withheld));
});

test("editor failure text and source", async () => {
  const { roleOf } = await staticModule("lib/views/content.js");
  const failed = bodies("lab-attempt/1").find((b) => b.editor.failure);
  const model = roleOf(failed, "editor");
  assert.ok(model.failure.text.length > 0);
  assert.match(model.failure.source, /editor_log|stderr/);
});

test("looks list is the whole evaluated history, newest last", async () => {
  const { looksOf } = await staticModule("lib/views/content.js");
  const withLooks = bodies("lab-attempt/1").find((b) => b.confidence?.looks?.length);
  // If the real snapshot has no evaluated history, use a clearly synthetic public block in this model test.
  // Do not modify snapshot data or return without assertions. Keep full-order and newest-last assertions.
  const confidence = withLooks?.confidence ?? { looks: [
    { look: 1, final: false, decision: "continue", reason: "insufficient_evidence" },
    { look: 2, final: true, decision: "reject", reason: "insufficient_evidence" },
  ] };
  const looks = looksOf(confidence);
  assert.deepEqual(looks.map((look) => look.look), confidence.looks.map((look) => look.look));
  assert.equal(looks.at(-1).look, confidence.looks.at(-1).look);
  // Intervals come as numbers with their text (spec family14 §3.5); a missing causal stays null.
  const { intervalOf } = await staticModule("lib/views/confidence.js");
  assert.deepEqual(looks.map((look) => look.focus), confidence.looks.map((look) => intervalOf(look.focus)));
  assert.deepEqual(looks.map((look) => look.causal), confidence.looks.map((look) => intervalOf(look.causal)));
  const synthetic = looksOf({ looks: [{ look: 2, final: false, decision: "continue", reason: "r",
    focus: { point: 0.15, lower: -0.02, upper: 0.32, exact: false }, causal: null }] })[0];
  assert.deepEqual(synthetic.focus, { point: 0.15, lower: -0.02, upper: 0.32, exact: false, text: "0.150 [-0.020, 0.320]" });
  assert.equal(synthetic.causal, null);
});

test("unavailable states keep their reason", async () => {
  const { sampleOf, roleOf, looksOf } = await staticModule("lib/views/content.js");
  assert.deepEqual(sampleOf({ trace: { state: "unavailable", reason: "imported" }, scoring: { state: "not_scored" },
    turns: [], private: { withheld: "private" }, truncated: false, masking: "keys" }).reason, "imported");
  assert.equal(roleOf({ editor: { kind: "subprocess", state: "unavailable", reason: "not_recorded", events: [],
    tool_calls: "not_recorded", changed: null, changed_count: null, failure: null, stderr_tail: "x" } }, "editor").reason,
    "not_recorded");
  const unavailable = { state: "unavailable", reason: "too_large" };
  const truncation = [{ path: "/input/public/text", size: 20000 }];
  const sample = sampleOf({ trace: { state: "available" }, input: unavailable, turns: unavailable,
    terminal: unavailable, scoring: unavailable, private: unavailable, truncated: true, truncation });
  for (const section of ["input", "turns", "terminal", "scoring", "private"]) {
    assert.deepEqual(sample[section], unavailable);
  }
  assert.deepEqual(sample.truncation, truncation);
  const attempt = roleOf({ confidence: unavailable, editor: { kind: "chat", state: "available", events: unavailable,
    changed: unavailable, failure: unavailable, stderr_tail: unavailable }, truncated: true, truncation }, "editor");
  for (const section of ["confidence", "events", "changed", "failure", "stderrTail"]) {
    assert.deepEqual(attempt[section], unavailable);
  }
  assert.deepEqual(attempt.truncation, truncation);
  const reflection = roleOf({ kind: "chat", state: "available", events: unavailable,
    stderr_tail: unavailable, truncated: true, truncation }, "reflector");
  assert.deepEqual(reflection.events, unavailable);
  assert.deepEqual(reflection.stderrTail, unavailable);
  assert.deepEqual(reflection.truncation, truncation);
  assert.deepEqual(looksOf(unavailable), unavailable);
});
