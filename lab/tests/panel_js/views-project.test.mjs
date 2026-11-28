import { test } from "node:test";
import assert from "node:assert/strict";
import { body, snapshot, staticModule } from "./support.mjs";

const { cyclesToRead, projectModel } = await staticModule("lib/views/project.js");
const { factsModel } = await staticModule("lib/views/facts.js");
const { continuationLine } = await staticModule("lib/continuation.js");
const { okEntry } = await staticModule("lib/resource.js");
const failed = (code, data) => ({ state: "error", data, code, stale: data !== undefined });

// The screen's reads: project, status, the project's operation summary (the sidebar's), then /v1/operations/<O> per cycle (it reads only those with a
// resume or an active control_state; loading more here changes nothing) and /report where it exists.
const summaryOf = (name, projectId = "demo") => body(name, `/v1/projects/${projectId}/operations?view=summary`);

// Ids and hashes come from the fixture (meta.ids and the bodies), never literals: a re-capture changes all of them.
const short = (id) => id.replace(/^[a-z0-9]+:/, "").slice(0, 8);
const factOf = (name, id) => body(name, "/v1/projects/demo/facts").facts.find((f) => f.id === id);
const cycleOf = (name, opId) => body(name, "/v1/projects/demo/status").scenarios.flatMap((s) => s.cycles ?? []).find((c) => c.operation_id === opId);
const sourceOf = (cycle, stepId, key) => cycle.steps.find((s) => s.id === stepId).sources[0][key];

function load(name, projectId = "demo", status = body(name, `/v1/projects/${projectId}/status`)) {
  const operations = {}, reports = {};
  for (const cycle of status.scenarios.find((s) => s.id === "evolve")?.cycles ?? []) {
    const id = cycle.operation_id;
    const key = `/v1/operations/${id}`;
    if (snapshot(name).routes[key]) operations[id] = okEntry(body(name, key));
    if (snapshot(name).routes[`${key}/report`]) reports[id] = okEntry(body(name, `${key}/report`));
  }
  return projectModel({ project: body(name, `/v1/projects/${projectId}`), status, summary: summaryOf(name, projectId),
                       operations, reports });
}

// Lab's own number of each campaign: the trailing number of its summary label ("… · evolve · цикл N").
function labelNumbers(name) {
  return Object.fromEntries(summaryOf(name).operations.map((r) => [r.operation?.id ?? r.link.object_id, r.label.match(/(\d+)$/)?.[1]]));
}

const targetProject = () => snapshot("suspended-target").meta.ids.project_target;
const roleProject = () => snapshot("suspended-role").meta.ids.project_role ?? "demo";

test("pipelines: one per scenario, one per evolve cycle; step count follows the status", () => {
  const model = load("final");
  const status = body("final", "/v1/projects/demo/status");
  const connect = model.pipelines.find((p) => p.title === "connect");
  assert.equal(connect.steps.length, status.scenarios.find((s) => s.id === "connect").steps.length);
  assert.equal(connect.opId, null);
  assert.ok(model.pipelines.filter((p) => p.title.startsWith("evolve")).length >= 2);
});

test("evolve cycles: numbered by Lab's summary label, op id, next cycle last", () => {
  const model = load("final");
  const cycles = body("final", "/v1/projects/demo/status").scenarios.find((s) => s.id === "evolve").cycles;
  const evolve = model.pipelines.filter((p) => p.title.startsWith("evolve · cycle"));
  const numbers = labelNumbers("final");
  assert.deepEqual(evolve.map((p) => p.title), cycles.map((c) => `evolve · cycle ${numbers[c.operation_id]}`));
  assert.deepEqual(evolve.map((p) => p.title), ["evolve · cycle 2", "evolve · cycle 1"]);
  assert.deepEqual(evolve.map((p) => p.opId), cycles.map((c) => c.operation_id));
  assert.equal(evolve[0].href, `#/operations/${cycles[0].operation_id}`);
  // the scenarios follow the catalogue order (connect, evolve, restructure): the next cycle is the last evolve pipeline
  const next = model.pipelines.filter((p) => p.title.startsWith("evolve")).at(-1);
  assert.equal(next.title, "evolve · next cycle");
  assert.equal(next.opId, null);
});

test("cycle titles stay tied to the label when cycles[] is reordered (a resume moves a cycle up)", () => {
  const status = structuredClone(body("final", "/v1/projects/demo/status"));
  status.scenarios.find((s) => s.id === "evolve").cycles.reverse();
  const numbers = labelNumbers("final");
  const evolve = load("final", "demo", status).pipelines.filter((p) => p.opId !== null);
  assert.deepEqual(evolve.map((p) => p.opId), status.scenarios.find((s) => s.id === "evolve").cycles.map((c) => c.operation_id));
  for (const p of evolve) assert.equal(p.title, `evolve · cycle ${numbers[p.opId]}`);
  assert.deepEqual(evolve.map((p) => p.title), ["evolve · cycle 1", "evolve · cycle 2"]);
});

test("without the summary the cycle title is still the status's own order", () => {
  const status = body("final", "/v1/projects/demo/status");
  const model = projectModel({ project: body("final", "/v1/projects/demo"), status, summary: undefined, operations: {}, reports: {} });
  const cycles = status.scenarios.find((s) => s.id === "evolve").cycles;
  assert.deepEqual(model.pipelines.filter((p) => p.opId !== null).map((p) => p.title),
                   cycles.map((c) => `evolve · cycle ${c.order}`));
});

test("cycle state: a resume verdict reads as suspended (amber); otherwise control state and outcome", () => {
  const model = load("final");
  const [current, older] = model.pipelines.filter((p) => p.title.startsWith("evolve · cycle"));
  assert.equal(current.state.tone, "amber");
  assert.match(current.state.text, /^Suspended · Ready to continue$/);
  assert.equal(older.state.text, "Finished · Completed");
});

test("an active cycle shows its operation's progress (spec §3.1: cycle progress from /v1/operations/<O>)", () => {
  const model = load("running");
  const [cycle] = model.pipelines.filter((p) => p.title.startsWith("evolve · cycle"));
  assert.deepEqual(cycle.state, { text: "Running · Generating candidate · round 1/2", tone: "blue" });
});

test("cyclesToRead: this pair's cycles with a resume or an active control state", () => {
  assert.deepEqual(cyclesToRead(body("running", "/v1/projects/demo/status")), [snapshot("running").meta.ids.campaign_running]);
  assert.deepEqual(cyclesToRead(body("final", "/v1/projects/demo/status")), [snapshot("final").meta.ids.campaign_suspended_role]);
  assert.deepEqual(cyclesToRead(undefined), []);
});

test("steps: labels, filled only when done, done_at formatted, sources in short form", () => {
  const model = load("final");
  const connect = model.pipelines.find((p) => p.title === "connect");
  const spec = connect.steps.find((s) => s.id === "spec");
  assert.deepEqual([spec.state.text, spec.state.tone, spec.filled, spec.source, spec.doneAt], ["Done", "green", true, "SPEC.md, spec.answers.json", null]);
  const confirm = connect.steps.find((s) => s.id === "spec-confirm");
  assert.equal(confirm.source, "tester · cli");
  assert.match(confirm.doneAt, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(confirm.title, "Confirm the spec");
  // A computed fact is shown by the source of the same fact in the status (task / operation id-8).
  assert.equal(connect.steps.find((s) => s.id === "check").source, `task ${short(factOf("final", "check.passed").source.id)}`);
  assert.equal(connect.steps.find((s) => s.id === "baseline").source, `operation ${short(snapshot("final").meta.ids.evaluation)}`);
  const cycle = model.pipelines.find((p) => p.title.startsWith("evolve · cycle"));
  const suspended = cycleOf("final", snapshot("final").meta.ids.campaign_suspended_role);
  assert.deepEqual(cycle.steps.map((s) => s.source), [`bundle ${short(sourceOf(suspended, "plan", "bundle"))}`,
    `prepared ${short(sourceOf(suspended, "prepare", "prepared"))}`, "", ""]);
  const run = cycle.steps.find((s) => s.id === "run");
  assert.deepEqual([run.state.text, run.note, run.marks], ["Blocked", "Campaign suspended", "Human only"]);
  const result = cycle.steps.find((s) => s.id === "result");
  assert.deepEqual([result.state.text, result.filled, result.note, result.marks], ["Blocked", false, "missing: run", "Optional"]);
  const next = model.pipelines.find((p) => p.title === "evolve · next cycle");
  assert.equal(next.steps.find((s) => s.id === "result").note, "missing: run");
});

test("deleted results show as a step note (export kept as a mark, docs/api/projects.md)", () => {
  const notes = load("final").pipelines.flatMap((p) => p.steps.map((s) => s.note)).filter(Boolean);
  assert.ok(notes.some((n) => /deleted/i.test(n)), notes.join(" | "));
});

test("stale and blocked reasons become notes through their labels", () => {
  const project = body("final", "/v1/projects/demo");
  const step = (over) => ({ id: "x", title: "X", state: "done", missing: [], blocked_reason: null, stale_reason: null,
                            carried_over: [], sources: [], done_at: null, ...over });
  const status = { facts: [], scenarios: [{ id: "connect", state: "available", missing: [], steps: [
    step({ id: "check", state: "stale", stale_reason: { codes: ["inputs_changed", "other_pair"], message: "…", changes: {} } }),
    step({ id: "result", state: "blocked", blocked_reason: { code: "results_deleted" } }),
    step({ id: "odd", state: "stale", stale_reason: { codes: ["brand_new"] } }),
  ] }] };
  const [check, result, odd] = projectModel({ project, status, operations: {}, reports: {} }).pipelines[0].steps;
  assert.equal(check.note, "Inputs changed, Other pair");
  assert.deepEqual([check.state.text, check.state.tone, check.filled], ["Outdated", "amber", false]);
  assert.equal(result.note, "Results were deleted");
  assert.equal(odd.note, "brand_new");
});

test("header: name, state, implementation, working copy, created", () => {
  const { header } = load("final");
  assert.equal(header.name, "Demo (demo)");
  assert.deepEqual(header.state, { text: "Ready", tone: "neutral", known: true });
  assert.equal(header.impl, "demo · agent");
  assert.equal(header.workingCopy, "/tmp/lab-fixture-home/clones/demo");
  assert.match(header.created, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
});

test("target suspension: fresh requirement and a command with the fresh-target wording or a ready id", () => {
  const id = snapshot("suspended-target").meta.ids.campaign_suspended_target;
  const excluded = short(body("suspended-target", "/v1/health").blockers.find((b) => b.kind === "target").id);
  const card = load("suspended-target", targetProject()).resume.find((r) => r.opId === id);
  assert.equal(card.requirement, "fresh_instance");
  assert.deepEqual(card.excluded, [excluded]);
  assert.equal(card.targetLine, `Needs a fresh target (fresh_instance); excluded: ${excluded}`);
  assert.match(card.command, /lab resume /);
  const status = body("suspended-target", `/v1/operations/${id}`);
  const ready = body("suspended-target", `/v1/operations/${id}/report`).report.owner_resume_target.ready_target;
  assert.equal(card.command, continuationLine(status, ready));
  assert.match(card.command, ready ? new RegExp(`--target ${ready.id} `) : /on a fresh target/);
});

test("a ready target from the report goes into the command", () => {
  const id = snapshot("suspended-target").meta.ids.campaign_suspended_target;
  const status = body("suspended-target", `/v1/projects/${targetProject()}/status`);
  const operation = body("suspended-target", `/v1/operations/${id}`);
  const report = structuredClone(body("suspended-target", `/v1/operations/${id}/report`));
  report.report.owner_resume_target.ready_target = { id: "fresh0001", owner_generation: "g2" };
  const model = projectModel({ project: body("suspended-target", `/v1/projects/${targetProject()}`), status,
                               operations: { [id]: okEntry(operation) }, reports: { [id]: okEntry(report) } });
  const card = model.resume.find((r) => r.opId === id);
  assert.match(card.command, /--target fresh0001 --generation g2 /);
  assert.equal(card.command, continuationLine(operation, { id: "fresh0001", owner_generation: "g2" }));
});

test("role suspension: no target requirement, the same-bundle wording", () => {
  const model = load("suspended-role", roleProject());
  const card = model.resume.find((r) => r.opId === snapshot("suspended-role").meta.ids.campaign_suspended_role);
  assert.equal(card.requirement, null);
  assert.deepEqual(card.excluded, []);
  assert.equal(card.targetLine, "No target requirement: any ready target of the same bundle");
  assert.deepEqual([card.verdict.text, card.verdict.tone, card.reason], ["Ready to continue", "green", null]);
  assert.match(card.command, /ready target of the same bundle/);
  assert.doesNotMatch(card.command, /fresh/);
});

test("target line only for verdicts that continue (ready, cleanup_first); never beside 'not available'", () => {
  const id = snapshot("suspended-role").meta.ids.campaign_suspended_role;
  const operation = body("suspended-role", `/v1/operations/${id}`);
  const card = (resume) => projectModel({ project: body("suspended-role", "/v1/projects/demo"),
    status: body("suspended-role", "/v1/projects/demo/status"), summary: summaryOf("suspended-role"),
    operations: { [id]: okEntry({ ...operation, resume }) }, reports: {} }).resume.find((r) => r.opId === id);
  const refused = card({ state: "refused", reason: "cleanup_unconfirmed", target: null });
  assert.equal(refused.targetLine, null);
  assert.match(refused.command, /not available after manual release/);
  assert.equal(card({ state: "busy", reason: "resource_busy", target: null }).targetLine, null);
  assert.equal(card({ state: "refused", reason: "results_deleted", target: null }).targetLine, null);
  const cleanup = card({ state: "cleanup_first", reason: "cleanup_required", target: null });
  assert.equal(cleanup.targetLine, "No target requirement: any ready target of the same bundle");
  assert.match(cleanup.command, /^continuation: lab cleanup operation /);
});

test("resume verdicts come only from operation reads; a suspended cycle not read yet keeps a loading card", () => {
  const status = body("suspended-role", "/v1/projects/demo/status");
  const id = snapshot("suspended-role").meta.ids.campaign_suspended_role;
  const model = projectModel({ project: body("suspended-role", "/v1/projects/demo"), status, operations: {}, reports: {} });
  const card = model.resume.find((r) => r.opId === id);
  assert.deepEqual(card.read, { state: "loading", code: null, stale: false });
  assert.deepEqual([card.verdict, card.command, card.targetLine], [null, null, null]);
});

// Tenet inspectable-experiments: a failed read is shown with its code, not by a card that disappears.
test("a suspended cycle whose operation read failed shows Unavailable with the code instead of disappearing", () => {
  const status = body("suspended-role", "/v1/projects/demo/status");
  const id = snapshot("suspended-role").meta.ids.campaign_suspended_role;
  const model = projectModel({ project: body("suspended-role", "/v1/projects/demo"), status,
                               operations: { [id]: failed("service_unavailable") }, reports: {} });
  const card = model.resume.find((r) => r.opId === id);
  assert.deepEqual(card.read, { state: "unavailable", code: "service_unavailable", stale: false });
  assert.equal(card.verdict, null);
  assert.equal(card.title, model.pipelines.find((p) => p.opId === id).title);
  // The same read failing after an answer keeps the verdict from the last answer, marked stale.
  const operation = body("suspended-role", `/v1/operations/${id}`);
  const stale = projectModel({ project: body("suspended-role", "/v1/projects/demo"), status,
                               operations: { [id]: failed("network_error", operation) }, reports: {} }).resume.find((r) => r.opId === id);
  assert.deepEqual([stale.read, stale.stale, stale.verdict.text], [null, true, "Ready to continue"]);
});

test("a fresh read with no resume verdict means no card (the answer rules, not the status copy)", () => {
  const status = body("suspended-role", "/v1/projects/demo/status");
  const id = snapshot("suspended-role").meta.ids.campaign_suspended_role;
  const operation = { ...body("suspended-role", `/v1/operations/${id}`), resume: null };
  const model = projectModel({ project: body("suspended-role", "/v1/projects/demo"), status,
                               operations: { [id]: okEntry(operation) }, reports: {} });
  assert.equal(model.resume.find((r) => r.opId === id), undefined);
});

test("under a target requirement the command waits on the report read; a failed one says why", () => {
  const id = snapshot("suspended-target").meta.ids.campaign_suspended_target;
  const project = body("suspended-target", `/v1/projects/${targetProject()}`);
  const status = body("suspended-target", `/v1/projects/${targetProject()}/status`);
  const operations = { [id]: okEntry(body("suspended-target", `/v1/operations/${id}`)) };
  const card = (reports) => projectModel({ project, status, operations, reports }).resume.find((r) => r.opId === id);
  const loading = card({});
  assert.deepEqual([loading.command, loading.commandRead], [null, { state: "loading", code: null, stale: false }]);
  assert.match(loading.targetLine, /^Needs a fresh target/);          // the verdict itself is known
  const failing = card({ [id]: failed("not_found") });
  assert.deepEqual([failing.command, failing.commandRead], [null, { state: "unavailable", code: "not_found", stale: false }]);
  const answered = card({ [id]: okEntry(body("suspended-target", `/v1/operations/${id}/report`)) });
  assert.match(answered.command, /lab resume /);
  assert.equal(answered.commandRead, null);
  assert.equal(answered.commandStale, false);
  // Codex r1: the report read failing after an answer keeps the command, marked stale (the card is shared
  // with the Suspended screen).
  const stale = card({ [id]: failed("network_error", body("suspended-target", `/v1/operations/${id}/report`)) });
  assert.deepEqual([stale.command, stale.commandRead, stale.commandStale], [answered.command, null, true]);
  assert.equal(card({ [id]: failed("not_found") }).commandStale, false);
});

test("an active cycle's progress: loading, Unavailable with the code, stale — never silently nothing", () => {
  const id = snapshot("running").meta.ids.campaign_running;
  const project = body("running", "/v1/projects/demo");
  const status = body("running", "/v1/projects/demo/status");
  const operation = body("running", `/v1/operations/${id}`);
  const cycle = (operations) => projectModel({ project, status, operations, reports: {} }).pipelines.find((p) => p.opId === id);
  assert.deepEqual(cycle({}).read, { state: "loading", code: null, stale: false });
  const failing = cycle({ [id]: failed("service_unavailable") });
  assert.deepEqual(failing.read, { state: "unavailable", code: "service_unavailable", stale: false });
  assert.equal(failing.state.text, "Running");                         // the status copy, without progress
  const stale = cycle({ [id]: failed("network_error", operation) });
  assert.deepEqual(stale.read, { state: "ok", code: null, stale: true });
  assert.equal(stale.state.text, "Running · Generating candidate · round 1/2");
  assert.deepEqual(cycle({ [id]: okEntry(operation) }).read, { state: "ok", code: null, stale: false });
  // A cycle that is neither suspended nor active is not read: no read state at all.
  const final = projectModel({ project: body("final", "/v1/projects/demo"), status: body("final", "/v1/projects/demo/status"),
                               operations: {}, reports: {} });
  const unread = new Set(cyclesToRead(body("final", "/v1/projects/demo/status")));
  for (const p of final.pipelines.filter((x) => x.opId && !unread.has(x.opId))) assert.equal(p.read, null);
});

test("a cycle not read (neither suspended nor active) is not a loading card", () => {
  const model = projectModel({ project: body("final", "/v1/projects/demo"), status: body("final", "/v1/projects/demo/status"),
                               operations: {}, reports: {} });
  const read = new Set(cyclesToRead(body("final", "/v1/projects/demo/status")));
  assert.ok(model.resume.every((r) => read.has(r.opId)));
});

test("project without a status yet: header only, no pipelines", () => {
  const model = projectModel({ project: body("final", "/v1/projects/demo"), status: undefined, operations: {}, reports: {} });
  assert.deepEqual([model.pipelines, model.resume], [[], []]);
});

test("facts: rows and confirmations with author and channel", () => {
  const model = factsModel({ facts: body("final", "/v1/projects/demo/facts"), confirmations: body("final", "/v1/projects/demo/confirmations") });
  assert.ok(model.rows.length > 0);
  assert.ok(model.confirmations.some((c) => c.author === "tester"));
});

test("facts: digest id-8, counts, known-from kinds and details", () => {
  const facts = body("final", "/v1/projects/demo/facts");
  const model = factsModel({ facts, confirmations: body("final", "/v1/projects/demo/confirmations") });
  assert.equal(model.digest, short(facts.revision.digest));
  assert.deepEqual(model.counts, { total: facts.facts.length, holding: facts.facts.filter((f) => f.holds).length, outdated: 0 });
  const row = (id) => model.rows.find((r) => r.id === id);
  const check = factOf("final", "check.passed");
  assert.deepEqual([row("check.passed").knownFrom, row("check.passed").detail], ["task", `${short(check.source.id)} · lab ${check.source.pair.lab} / core ${check.source.pair.core}`]);
  assert.equal(row("connected").detail, "check.passed + conformance.passed + baseline.measured");
  const confirmation = body("final", "/v1/projects/demo/confirmations").confirmations[0];
  assert.equal(row("spec.confirmed").detail,
    `${confirmation.id} · ${confirmation.attests.path} ${short(confirmation.attests.digest)} · ${confirmation.author} · ${confirmation.channel}`);
  const campaigns = factOf("final", "campaigns").value.map((c) => short(c.operation_id));
  assert.equal(row("campaigns").detail, `${campaigns.length} campaigns: ${campaigns.join(", ")}`);
  assert.deepEqual(row("check.passed").state, { text: "Holds", tone: "green", known: true });
  assert.equal(row("check.passed").whyOutdated, null);
  const [c] = model.confirmations;
  assert.deepEqual([c.id, c.claim, c.path, c.digest, c.channel, c.revoked], [confirmation.id, "spec.confirmed", confirmation.attests.path, short(confirmation.attests.digest), "cli", null]);
});

test("facts: an outdated fact names its codes; a false one does not hold", () => {
  const model = factsModel({ facts: { revision: null, facts: [
    { id: "check.passed", subject: { implementation: "a" }, holds: false, value: true, source: { kind: "task", id: "t" },
      stale_reason: { codes: ["inputs_changed"], message: "…", changes: {} }, carried_over: [] },
    { id: "baseline.measured", subject: { implementation: "a" }, holds: false, value: false,
      source: { kind: "operation", id: "o", failed: "coverage" }, stale_reason: null, carried_over: [] },
  ] }, confirmations: undefined });
  const [stale, no] = model.rows;
  assert.deepEqual([stale.state.text, stale.state.tone, stale.whyOutdated], ["Outdated", "amber", "Inputs changed"]);
  assert.deepEqual([no.state.text, no.state.tone], ["Does not hold", "amber"]);
  assert.match(no.detail, /failed: coverage/);
  assert.deepEqual([model.digest, model.confirmations, model.counts.outdated], [null, [], 1]);
});

test("status/6 step fields: marks, the new blocked codes, the schema name and invalid paths", () => {
  const project = body("final", "/v1/projects/demo");
  const step = (over) => ({ id: "x", title: "X", state: "done", optional: false, human_only: false, missing: [],
                            blocked_reason: null, invalid: [], stale_reason: null, carried_over: [], sources: [],
                            done_at: null, ...over });
  const status = { facts: [], scenarios: [{ id: "restructure", state: "available", missing: [], steps: [
    step({ id: "map", state: "blocked", blocked_reason: { code: "artifact_invalid" },
           invalid: [{ artifact: "map.json", digest: "sha256:" + "a".repeat(64), path: "/behaviors/0", message: "m" },
                     { artifact: "map.json", digest: "sha256:" + "a".repeat(64), path: "/behaviors/0/id", message: "m" }] }),
    step({ id: "whole", state: "blocked", blocked_reason: { code: "artifact_invalid" },
           invalid: [{ artifact: "routes.json", digest: null, path: "", message: "m" }] }),
    step({ id: "check", state: "blocked", blocked_reason: { code: "schema_unusable", schema: "behavior-map/1" } }),
    step({ id: "unknown", state: "blocked", blocked_reason: { code: "campaign_state_unknown" } }),
    step({ id: "map-confirm", state: "ready", human_only: true }),
    step({ id: "notes", state: "ready", optional: true }),
  ] }] };
  const [map, whole, check, unknown, confirm, notes] = projectModel({ project, status, operations: {}, reports: {} }).pipelines[0].steps;
  assert.equal(map.note, "Artifact fails its schema · invalid: map.json/behaviors/0 (+1)");
  assert.match(whole.note, /invalid: routes\.json$/);
  assert.equal(check.note, "Schema cannot check (behavior-map/1)");
  assert.equal(unknown.note, "Campaign state unknown");
  assert.deepEqual([confirm.marks, notes.marks, map.marks], ["Human only", "Optional", null]);
});

// Project history (spec 2026-10-19 project-history): an observation panel shows what ran, newest first. On top
// Owner's current cycle ("Current"), else the newest entry ("Latest"); below, the rest. Never shown: next_cycle
// and one-off scenarios that were not produced (connect, restructure).
const titles = (list) => list.map((p) => p.title);
const statusOf = (name, projectId = "demo") => structuredClone(body(name, `/v1/projects/${projectId}/status`));

test("history: Owner's current cycle on top; the rest below; next cycle and unproduced scenarios absent", () => {
  const model = load("final");
  assert.equal(model.focus.label, "Current");
  assert.equal(model.focus.pipeline.title, "evolve · cycle 2");
  assert.deepEqual(new Set(titles(model.history)), new Set(["evolve · cycle 1", "connect"]));
  const shown = titles([model.focus.pipeline, ...model.history]);
  for (const t of ["evolve · next cycle", "restructure"]) assert.equal(shown.includes(t), false, t);
});

test("history: without a current cycle the newest one is on top as Latest; connect not produced is not shown", () => {
  const model = load("family14", "demo-final");
  assert.equal(model.focus.label, "Latest");
  assert.equal(model.focus.pipeline.title, "evolve · cycle 1");
  assert.deepEqual(model.history, []);
});

test("history: a running cycle is current; produced connect goes below", () => {
  const model = load("running");
  assert.equal(model.focus.label, "Current");
  assert.match(model.focus.pipeline.title, /^evolve · cycle 1$/);
  assert.deepEqual(titles(model.history), ["connect"]);
});

test("history: cycles keep Owner's order; a produced one-off scenario is placed by its latest done_at, unknown last", () => {
  const status = statusOf("final");
  const evolve = status.scenarios.find((s) => s.id === "evolve");
  const connect = status.scenarios.find((s) => s.id === "connect");
  const older = structuredClone(evolve.cycles[1]);
  evolve.cycles.forEach((c) => { c.current = false; });
  evolve.cycles.push({ ...older, operation_id: "f".repeat(32), order: 0 });
  const at = (iso) => (c) => c.steps.forEach((s) => { s.done_at = s.state === "done" ? iso : null; });
  at("2026-10-17T10:00:00+00:00")(evolve.cycles[0]);
  at("2026-10-17T08:00:00+00:00")(evolve.cycles[1]);
  at("2026-10-17T06:00:00+00:00")(evolve.cycles[2]);
  connect.steps.forEach((s) => { s.done_at = s.state === "done" ? "2026-10-17T09:00:00+00:00" : null; });
  const ops = (m) => [m.focus.pipeline, ...m.history].map((p) => p.opId ?? p.title);
  const ids = evolve.cycles.map((c) => c.operation_id);
  assert.deepEqual(ops(load("final", "demo", status)), [ids[0], "connect", ids[1], ids[2]]);
  connect.steps.forEach((s) => { s.done_at = null; });
  assert.deepEqual(ops(load("final", "demo", status)), [ids[0], ids[1], ids[2], "connect"]);
  // an untimed cycle is skipped in the comparison, not treated as oldest
  at(null)(evolve.cycles[1]);
  connect.steps.forEach((s) => { s.done_at = s.state === "done" ? "2026-10-17T07:00:00+00:00" : null; });
  assert.deepEqual(ops(load("final", "demo", status)), [ids[0], ids[1], "connect", ids[2]]);
});

test("history: a cycle of another pair is a run that happened and stays in the history", () => {
  const status = statusOf("final");
  status.scenarios.find((s) => s.id === "evolve").cycles[1].detail = "unavailable";
  const row = load("final", "demo", status).history.find((p) => p.opId !== null);
  assert.equal(row.href, null);
});

test("history: nothing ran — no focus, empty history; a produced connect alone is Latest", () => {
  const status = statusOf("final");
  status.scenarios.find((s) => s.id === "evolve").cycles = [];
  status.scenarios.find((s) => s.id === "connect").state = "available";
  const none = load("final", "demo", status);
  assert.deepEqual([none.focus, none.history], [null, []]);
  status.scenarios.find((s) => s.id === "connect").state = "produced";
  const only = load("final", "demo", status);
  assert.deepEqual([only.focus.label, only.focus.pipeline.title, only.history], ["Latest", "connect", []]);
});

test("history rows: done steps counted out of all", () => {
  const model = load("final");
  const connect = model.history.find((p) => p.title === "connect");
  assert.equal(connect.progress, `${connect.steps.filter((s) => s.filled).length} of ${connect.steps.length} steps done`);
});

test("history: an untimed produced scenario stays last when a timed one follows it (codex r1)", () => {
  const status = statusOf("final");
  status.scenarios.find((s) => s.id === "evolve").cycles = [];
  const connect = status.scenarios.find((s) => s.id === "connect");
  connect.steps.forEach((s) => { s.done_at = null; });
  const restructure = status.scenarios.find((s) => s.id === "restructure");
  restructure.state = "produced";
  restructure.steps[0].state = "done";
  restructure.steps[0].done_at = "2026-10-17T09:00:00+00:00";
  const model = load("final", "demo", status);
  assert.deepEqual([model.focus.label, model.focus.pipeline.title, titles(model.history)], ["Latest", "restructure", ["connect"]]);
});
