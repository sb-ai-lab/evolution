import { test } from "node:test";
import assert from "node:assert/strict";
import { inspect, staticModule } from "./support.mjs";

const { createSuspensionResource, meterOf, suspensionModel, suspensionSeq } = await staticModule("lib/views/suspensions.js");
const { createApi, paths } = await staticModule("lib/api.js");
const { okEntry, errorEntry } = await staticModule("lib/resource.js");
const ids = { measurement_id: "m1", case_id: "case", sample_id: "sample", retry_ordinal: 2 };
const metadata = { count: 300, rows_digest: "digest", truncated: true };
const forms = [
  ["fault_rows", metadata, [{ ...ids, reason: "future_reason", code: null }]],
  ["final_rows", metadata, [{ ...ids, owner: "future_owner" }]],
  ["champion_rows", { ...metadata, measurement_id: "m1" }, [{ ...ids, owner: "target" }]],
  ["threshold", { crossing: { owner: "target", kind: "future_kind", count: 2, denominator: 3,
    max_count: 1, max_rate: "0.5" }, window_start: null }, null],
  ["stage_record", { seq: 2, operation_id: null, prefix: "measurements/m1/events.jsonl#bytes=0:129" }, null],
];
const history = forms.map(([kind, evidence, rows], i) => ({ seq: i + 3, reason: "future_pause",
  owner: "future_pause_owner", evidence_kind: kind, evidence, rows }));
const payload = (operation_id = "op1", suspensions = history) => ({ schema: "lab-suspension-evidence/1",
  operation_id, source: "core_raw", state: "available", reason: null, suspensions });
const flush = () => new Promise((resolve) => setImmediate(resolve));
const context = (seq = 7, open = true, operationId = "op1") => ({ operationId, seq, open });
function pendingApi() {
  const requests = [];
  const api = createApi({ fetch: (path) => new Promise((resolve, reject) => requests.push({ path,
    answer: (value) => resolve({ ok: true, json: async () => value }), reject,
    deleted: () => resolve({ ok: false, status: 409, json: async () => ({ error: { code: "results_deleted" } }) }) })) });
  return { api, requests };
}

test("five closed forms, latest first, links, nullable values and truncation remain structural", () => {
  const model = suspensionModel({ operationId: "op1", seq: 7, entry: okEntry(payload()) });
  assert.deepEqual(model.records.map((r) => r.seq), [7, 6, 5, 4, 3]);
  assert.equal(model.latest, model.records[0]);
  assert.equal(model.current.seq, 7);
  assert.equal(model.older.length, 4);
  const fault = model.records.at(-1);
  assert.equal(fault.rows[0].measurementHref, "#/operations/op1/measurements/m1");
  assert.equal(fault.rows[0].code, null);
  assert.deepEqual([fault.shown, fault.count, fault.truncated], [1, 300, true]);
  assert.equal(model.records[1].evidence.crossing.max_rate, "0.5");
  assert.equal(model.latest.evidence.operation_id, null);
  assert.equal(model.latest.evidence.prefix, forms[4][1].prefix);
});

test("missing, unavailable, empty and stale are distinct; old seq cannot identify the current cause", () => {
  assert.equal(suspensionModel({ operationId: "op1" }).read.state, "loading");
  const unavailable = { ...payload(), state: "unavailable", reason: "campaign_missing", suspensions: null };
  assert.equal(suspensionModel({ operationId: "op1", entry: okEntry(unavailable) }).read.code, "campaign_missing");
  assert.equal(suspensionModel({ operationId: "op1", entry: okEntry(payload("op1", [])) }).empty, true);
  const old = suspensionModel({ operationId: "op1", seq: 8, entry: okEntry(payload()) });
  assert.equal(old.read.stale, true);
  assert.equal(old.current, null);
  assert.equal(old.empty, false);
  assert.equal(suspensionModel({ operationId: "op1", seq: 7,
    entry: errorEntry("network_error", payload()) }).read.stale, true);
  assert.equal(suspensionModel({ operationId: "other", entry: okEntry(payload()) }).read.code, "unsupported_schema");
  const olderEmpty = suspensionModel({ operationId: "op1", seq: 8, entry: okEntry(payload("op1", [])) });
  assert.equal(olderEmpty.empty, false);
  assert.equal(olderEmpty.read.stale, true);
});

test("unsupported old entry or nested content refuses the whole history", () => {
  for (const mutate of [
    (p) => { p.suspensions[0].rows[0].diagnostics = "PRIVATE"; },
    (p) => { p.suspensions[0].evidence_kind = "cohort_rows"; },
    (p) => { p.suspensions[0].evidence_kind = ["fault_rows"]; },
    (p) => { p.suspensions[3].evidence.crossing.max_rate = 0.5; },
    (p) => { p.suspensions[0].rows[0].code = { content: "PRIVATE" }; },
    (p) => { p.suspensions[0].evidence.extra = "PRIVATE"; },
    (p) => { p.schema = "lab-suspension-evidence/2"; },
  ]) {
    const p = structuredClone(payload()); mutate(p);
    const model = suspensionModel({ operationId: "op1", entry: okEntry(p) });
    assert.equal(model.read.code, "unsupported_schema");
    assert.deepEqual(model.records, []);
  }
  assert.equal(suspensionSeq({ schema: "other", family: 14, suspensions: history }), null);
  assert.equal(suspensionSeq({ schema: "lab-campaign-view/15", lab_notices_state: "present", family: 14, rounds: [], suspensions: history,
    reflector_probe: null, reflector_probe_state: "unavailable", reflector_probe_reason: "unreadable" }), 7);
});

test("on-demand request counts: closed, open, same seq ticks, new seq, reopen and operation", async () => {
  const { api, requests } = pendingApi();
  const resource = createSuspensionResource(api);
  resource.update(context(7, false)); assert.equal(requests.length, 0);
  resource.update(context()); assert.equal(requests.length, 1);
  resource.update(context()); resource.update({ ...context(), epoch: 20, refresh: 40 });
  assert.equal(requests.length, 1);
  requests[0].answer(payload()); await flush();
  assert.equal(resource.snapshot().data.operation_id, "op1");
  resource.update(context(8)); assert.equal(requests.length, 2);
  assert.equal(resource.snapshot().stale, true);
  requests[1].answer(payload("op1", [...history, { ...history.at(-1), seq: 8 }])); await flush();
  resource.update(context(8, false)); resource.update(context(9, false)); assert.equal(requests.length, 2);
  resource.update(context(9)); assert.equal(requests.length, 3);
  resource.update(context(9, false)); resource.update(context(9)); assert.equal(requests.length, 3); // in-flight read shared
  resource.update(context(4, true, "op2")); assert.equal(requests.length, 4);
  assert.equal(resource.snapshot().data, undefined);
  requests[2].answer(payload()); await flush(); assert.equal(resource.snapshot().data, undefined);
  requests[3].answer(payload("op2", [])); await flush();
  assert.equal(resource.snapshot().data.operation_id, "op2");
  resource.close();
});

test("late seq answer is discarded; a failed new read keeps historical data stale", async () => {
  const { api, requests } = pendingApi(); const resource = createSuspensionResource(api);
  resource.update(context(6)); requests[0].answer(payload("op1", history.slice(0, -1))); await flush();
  resource.update(context(7)); resource.update(context(8));
  requests[1].answer(payload()); await flush();
  assert.equal(resource.snapshot().data.suspensions.at(-1).seq, 6);
  requests[2].reject(new TypeError("offline")); await flush();
  assert.equal(resource.snapshot().code, "network_error");
  assert.equal(resource.snapshot().stale, true);
  assert.equal(suspensionModel({ operationId: "op1", seq: 8, entry: resource.snapshot() }).current, null);
});

test("results_deleted clears local history and common cache including older in-flight successes", async () => {
  const { api, requests } = pendingApi(); const resource = createSuspensionResource(api);
  resource.update(context()); requests[0].answer(payload()); await flush();
  assert.ok(api.last(paths.suspensions("op1")));
  resource.update(context(8));
  resource.update({ ...context(8), deleted: true });
  assert.equal(resource.snapshot().data, undefined);
  assert.equal(resource.snapshot().code, "results_deleted");
  assert.equal(api.last(paths.suspensions("op1")), undefined);
  requests[1].answer(payload()); await flush();
  assert.equal(resource.snapshot().data, undefined);
  assert.equal(api.last(paths.suspensions("op1")), undefined);
  resource.update(context(9)); assert.equal(requests.length, 2); // tombstone stays for this operation
});

test("transport results_deleted purges last value instead of showing stale history", async () => {
  const { api, requests } = pendingApi(); const resource = createSuspensionResource(api);
  resource.update(context()); requests[0].answer(payload()); await flush();
  resource.update(context(8)); requests[1].deleted(); await flush();
  assert.equal(resource.snapshot().code, "results_deleted");
  assert.equal(resource.snapshot().data, undefined);
  assert.equal(api.last(paths.suspensions("op1")), undefined);
});

test("rendered evidence preserves closed fields, collapsed older history and plain-text locator", async () => {
  const { SuspensionEvidence } = await staticModule("ui/suspensions.js");
  const model = suspensionModel({ operationId: "op1", seq: 7, entry: okEntry(payload()) });
  const rendered = inspect(SuspensionEvidence({ model, current: true }));
  for (const token of ["Current pause", "Historical", "future_reason", "future_owner", "null",
    "Showing 1 of 300", "truncated", "digest", "Observed", "Limit", "Window", "2 of 3", "2/3", "max_rate 0.5",
    "Window start", "Earlier pauses", forms[4][1].prefix]) assert.ok(rendered.text.includes(token), token);
  const older = rendered.elements.filter((node) => node.type === "details");
  assert.equal(older.length, 1); assert.equal(older[0].props.open, undefined);
  // digest: first 8 characters shown, full value in title
  const long = structuredClone(payload()); long.suspensions[0].evidence.rows_digest = "0123456789abcdef";
  const full = inspect(SuspensionEvidence({ model: suspensionModel({ operationId: "op1", seq: 7, entry: okEntry(long) }), current: true }));
  assert.ok(full.text.includes("01234567") && !full.text.includes("012345678"));
  assert.ok(full.elements.some((node) => node.props?.title === "0123456789abcdef"));
  // locator is text; links are only the measurement ids of rows
  const links = rendered.elements.filter((node) => node.type === "a");
  assert.equal(links.length, 4);
  assert.ok(links.every((node) => node.props.href === "#/operations/op1/measurements/m1"));
  assert.ok(!links.some((node) => String(node.props.href).includes("events.jsonl")));
  // Normal campaign history after resume and a mismatching seq never claim a current pause.
  const historic = inspect(SuspensionEvidence({ model }));
  assert.ok(!historic.text.includes("Current pause") && historic.text.includes("Historical"));
  const stale = suspensionModel({ operationId: "op1", seq: 8, entry: okEntry(payload()) });
  const old = inspect(SuspensionEvidence({ model: stale, current: true }));
  assert.ok(/stale/i.test(old.text)); assert.ok(!old.text.includes("Current pause")); assert.ok(old.text.includes("Historical"));
  for (const entry of [undefined, errorEntry("campaign_refused", undefined)]) {
    const read = inspect(SuspensionEvidence({ model: suspensionModel({ operationId: "op1", entry }) }));
    assert.ok(!read.text.includes("No pauses were recorded"));
    assert.ok(read.text.includes(entry ? "campaign_refused" : "Loading suspension evidence"));
  }
});

test("evidence: champion_rows and final_rows tables, stage_record without prefix", async () => {
  const { PauseEvidence } = await staticModule("ui/suspensions.js");
  const only = (i) => suspensionModel({ operationId: "op1", seq: history[i].seq, entry: okEntry(payload("op1", [history[i]])) }).latest;
  const champion = inspect(PauseEvidence({ record: only(2), current: true, historical: false }));
  for (const token of ["champion_rows", "Owner", "target", "Showing 1 of 300", "Retry"]) assert.ok(champion.text.includes(token), token);
  assert.ok(champion.elements.some((node) => node.type === "table"));
  const finals = inspect(PauseEvidence({ record: only(1), current: false, historical: true }));
  assert.ok(finals.text.includes("future_owner") && finals.text.includes("Historical") && !finals.text.includes("Current pause"));
  const bare = structuredClone(history[4]); delete bare.evidence.prefix;
  const record = suspensionModel({ operationId: "op1", seq: bare.seq, entry: okEntry(payload("op1", [bare])) }).latest;
  const stage = inspect(PauseEvidence({ record, current: true, historical: false }));
  assert.ok(stage.text.includes("Stage seq") && stage.text.includes("Operation") && stage.text.includes("none for this reason"));
  assert.ok(!stage.elements.some((node) => node.type === "a"));
  const complete = structuredClone(history[0]); complete.evidence.truncated = false; complete.evidence.count = 1;
  const whole = suspensionModel({ operationId: "op1", seq: 3, entry: okEntry(payload("op1", [complete])) }).latest;
  const text = inspect(PauseEvidence({ record: whole, current: true, historical: false })).text;
  assert.ok(text.includes("complete") && !text.includes("Showing"));
});

test("safe snapshots hide previous operation/deletion immediately; closing drops late local answer", async () => {
  const { api, requests } = pendingApi(); const resource = createSuspensionResource(api);
  resource.update(context()); requests[0].answer(payload()); await flush();
  assert.equal(resource.snapshotFor(context(8)).stale, true);
  assert.equal(resource.snapshotFor(context(7, true, "op2")).data, undefined);
  assert.equal(resource.snapshotFor({ ...context(), deleted: true }).data, undefined);
  resource.update(context(7, false)); resource.update(context());
  assert.equal(requests.length, 2); // reopening after a settled answer reads once
  resource.close(); requests[1].answer(payload()); await flush();
  assert.equal(resource.snapshot().data, undefined);
});

test("an initial failed history read stays unavailable; deleted transport notifies the mounted consumer", async () => {
  const { api, requests } = pendingApi(); let notifications = 0;
  const resource = createSuspensionResource(api, () => notifications++);
  resource.update(context()); requests[0].reject(new TypeError("offline")); await flush();
  assert.equal(suspensionModel({ operationId: "op1", seq: 7, entry: resource.snapshot() }).read.state, "unavailable");
  resource.update(context(7, false)); resource.update(context());
  const before = notifications; requests[1].deleted(); await flush();
  assert.ok(notifications > before);
  assert.equal(resource.snapshot().data, undefined);
});

test("superseded same-operation deletion clears a newer successful history and remains tombstoned", async () => {
  const { api, requests } = pendingApi(); const resource = createSuspensionResource(api);
  resource.update(context(7));
  resource.update(context(8));
  const newer = payload("op1", [...history, { ...history.at(-1), seq: 8 }]);
  requests[1].answer(newer); await flush();
  assert.equal(resource.snapshot().data.suspensions.at(-1).seq, 8);
  assert.deepEqual(api.last(paths.suspensions("op1")), newer);
  requests[0].deleted(); await flush();
  assert.equal(resource.snapshot().code, "results_deleted");
  assert.equal(resource.snapshot().data, undefined);
  assert.equal(api.last(paths.suspensions("op1")), undefined);
  resource.update(context(9));
  assert.equal(requests.length, 2);
});

test("a late deletion from another operation leaves current history and cache intact", async () => {
  const { api, requests } = pendingApi(); const resource = createSuspensionResource(api);
  resource.update(context(7));
  resource.update(context(8, true, "op2"));
  const other = payload("op2", [{ ...history.at(-1), seq: 8 }]);
  requests[1].answer(other); await flush();
  requests[0].deleted(); await flush();
  assert.equal(resource.snapshot().code, null);
  assert.deepEqual(resource.snapshot().data, other);
  assert.deepEqual(api.last(paths.suspensions("op2")), other);
  resource.update(context(8, false, "op2")); resource.update(context(8, true, "op2"));
  assert.equal(requests.length, 3); // another operation's tombstone cannot block this operation
  resource.close();
});


test("open history waits for campaign settlement, including a null seq after failure", async () => {
  for (const seq of [null, 7]) {
    const { api, requests } = pendingApi();
    const resource = createSuspensionResource(api);
    resource.update({ ...context(null), campaignSettled: false });
    assert.equal(requests.length, 0);
    resource.update({ ...context(seq), campaignSettled: true });
    assert.equal(requests.length, 1);
    resource.update({ ...context(seq), campaignSettled: true });
    assert.equal(requests.length, 1);
    requests[0].answer(payload()); await flush();
    assert.equal(resource.snapshot().data.operation_id, "op1");
    resource.close();
  }
});

// Task 10 (ruling R9): the Observed meter is plain arithmetic over the recorded numbers; the limit tick only for a
// max_rate that parses to a finite rate in [0, 1].
test("threshold meter: fill is count/denominator, limit tick only for a parseable rate", () => {
  const crossing = forms[3][1].crossing;
  assert.deepEqual(meterOf(crossing), { fill: 2 / 3, tick: 0.5 });
  assert.deepEqual(meterOf({ ...crossing, max_rate: "0.500000000000" }), { fill: 2 / 3, tick: 0.5 });
  for (const max_rate of ["future", "1.5", "-0.1", "", "Infinity"]) assert.equal(meterOf({ ...crossing, max_rate }).tick, null, max_rate);
  assert.equal(meterOf({ ...crossing, denominator: 0 }).fill, null);
  assert.equal(meterOf({ ...crossing, count: 0 }).fill, 0);
  assert.equal(meterOf({ ...crossing, count: 5 }).fill, 1); // drawn within the bar; the numbers stay as recorded
});

test("evidence header: Why it paused, a blue current tag, the meter drawn without inline style", async () => {
  const { PauseEvidence } = await staticModule("ui/suspensions.js");
  const record = suspensionModel({ operationId: "op1", seq: history[3].seq, entry: okEntry(payload("op1", [history[3]])) }).latest;
  const current = inspect(PauseEvidence({ record, current: true }));
  assert.ok(current.text.startsWith("Why it paused"));
  assert.ok(current.elements.some((n) => n.props?.class === "chip-progress" && n.props.children === "Current pause"));
  assert.ok(!current.elements.some((n) => n.props?.class === "chip-ok"));
  const svg = current.elements.find((n) => n.type === "svg");
  assert.ok(svg && String(svg.props.class).includes("meter"));
  const fill = current.elements.find((n) => n.props?.class === "meter-fill");
  assert.equal(fill.props.width, `${(2 / 3) * 100}%`);
  const tick = current.elements.find((n) => n.props?.class === "meter-limit");
  assert.equal(tick.props.x1, "50%");
  assert.ok(current.elements.every((n) => n.props?.style === undefined));
  const noTick = structuredClone(history[3]); noTick.evidence.crossing.max_rate = "future";
  const bare = suspensionModel({ operationId: "op1", seq: noTick.seq, entry: okEntry(payload("op1", [noTick])) }).latest;
  assert.ok(!inspect(PauseEvidence({ record: bare, current: true })).elements.some((n) => n.props?.class === "meter-limit"));
  const historic = inspect(PauseEvidence({ record, historical: true }));
  assert.ok(historic.text.startsWith("Why it paused") && historic.text.includes("Historical"));
});

// Read states of the evidence block (additions mock, "Read states"): empty only after a successful empty read,
// stale names the newer pause seq and dims the previous content, deletion is its own state.
const dimmed = (r) => r.elements.some((n) => String(n.props?.class ?? "").split(" ").includes("is-stale"));
test("evidence read states: empty, stale with the newer seq, deleted", async () => {
  const { SuspensionEvidence } = await staticModule("ui/suspensions.js");
  const empty = inspect(SuspensionEvidence({ model: suspensionModel({ operationId: "op1", entry: okEntry(payload("op1", [])) }) }));
  assert.ok(empty.text.includes("No pauses were recorded"));
  const behind = suspensionModel({ operationId: "op1", seq: 8, entry: okEntry(payload()) });
  assert.equal(behind.newerSeq, 8);
  assert.equal(suspensionModel({ operationId: "op1", seq: 7, entry: okEntry(payload()) }).newerSeq, null);
  assert.equal(suspensionModel({ operationId: "op1", seq: 7, entry: errorEntry("network_error", payload()) }).newerSeq, null);
  const stale = inspect(SuspensionEvidence({ model: behind, current: true }));
  assert.ok(stale.text.includes("Stale · newer pause seq 8"));
  assert.ok(dimmed(stale));
  const failed = inspect(SuspensionEvidence({ model: suspensionModel({ operationId: "op1", seq: 7, entry: errorEntry("network_error", payload()) }) }));
  assert.ok(failed.text.includes("stale") && dimmed(failed));
  const fresh = inspect(SuspensionEvidence({ model: suspensionModel({ operationId: "op1", seq: 7, entry: okEntry(payload()) }), current: true }));
  assert.ok(!dimmed(fresh));
  const deleted = inspect(SuspensionEvidence({ model: suspensionModel({ operationId: "op1", entry: errorEntry("results_deleted", undefined) }) }));
  assert.ok(deleted.text.includes("Results deleted") && deleted.text.includes("results_deleted"));
  assert.ok(!deleted.text.includes("Unavailable (") && !deleted.text.includes("No pauses were recorded"));
});

// Final review item 6: the closed history's summary counts the campaign view's own list ("N pauses recorded"); it
// says "loaded when opened" only while closed, and 0 reads "No pauses recorded" (still nothing read).
const OK = { state: "ok", code: null, stale: false };
test("suspension history summary: recorded count, loaded when opened only while closed", async () => {
  const { historyMeta } = await staticModule("ui/suspensions.js");
  const fresh = (n, open, history = OK) => historyMeta({ n, open, campaignFresh: true, history });
  assert.equal(fresh(3, false), "3 pauses recorded · loaded when opened");
  assert.equal(fresh(2, false), "2 pauses recorded · loaded when opened");
  assert.equal(fresh(1, false), "1 pause recorded · loaded when opened");
  assert.equal(fresh(3, true), "3 pauses recorded");
  assert.equal(fresh(0, false), "No pauses recorded");
  assert.equal(fresh(0, true), "No pauses recorded");
  assert.equal(fresh(null, false), "loaded when opened");
  assert.equal(fresh(null, true), "");
});

// Codex r1 pause-summary-confirms-stale-emptiness (PDR-0017: unavailable/stale do not mean "no pauses"): the count
// and "No pauses recorded" only from a fresh campaign read; once open, only while the history read is ok and fresh.
test("suspension history summary: no count and no emptiness claim from a read that is not fresh", async () => {
  const { historyMeta } = await staticModule("ui/suspensions.js");
  const claims = (meta) => /pause/.test(meta);
  for (const n of [0, 2]) {
    // stale, unavailable, loading or outage: the caller's campaignFresh is false
    assert.equal(historyMeta({ n, open: false, campaignFresh: false }), "loaded when opened");
    assert.equal(historyMeta({ n, open: true, campaignFresh: false, history: OK }), "");
    // open: the history read's own state speaks
    for (const history of [{ state: "unavailable", code: "network_error", stale: false }, { ...OK, stale: true },
      { state: "loading", code: null, stale: false }]) {
      assert.ok(!claims(historyMeta({ n, open: true, campaignFresh: true, history })), JSON.stringify(history));
    }
  }
  // Missing freshness is not fresh.
  assert.equal(historyMeta({ n: 0, open: false }), "loaded when opened");
  // The open history's read is the model's: an answer behind the campaign's seq is stale.
  const behind = suspensionModel({ operationId: "op1", seq: 8, entry: okEntry(payload()) });
  assert.equal(historyMeta({ n: 2, open: true, campaignFresh: true, history: behind.read }), "");
  const empty = suspensionModel({ operationId: "op1", entry: errorEntry("network_error", payload("op1", [])) });
  assert.equal(historyMeta({ n: 0, open: true, campaignFresh: true, history: empty.read }), "");
});

// Final review item 7: the "cause still comes from the campaign view" line belongs to the current pause only; the
// Campaign screen's history has no cause line to point to.
test("evidence: the cause line only for the current pause", async () => {
  const { SuspensionEvidence } = await staticModule("ui/suspensions.js");
  const CAUSE = "The cause still comes from the campaign view; no rows are shown.";
  const failed = suspensionModel({ operationId: "op1", seq: 7, entry: errorEntry("campaign_refused", undefined) });
  assert.ok(inspect(SuspensionEvidence({ model: failed, current: true })).text.includes(CAUSE));
  assert.ok(!inspect(SuspensionEvidence({ model: failed })).text.includes(CAUSE));
});
