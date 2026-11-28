import { test } from "node:test";
import assert from "node:assert/strict";
import { body, staticModule } from "./support.mjs";

const { experimentsModel } = await staticModule("lib/views/experiments.js");
const { LOADING, okEntry } = await staticModule("lib/resource.js");

// The shell's reads as entries of lib/resource.js: the project list and each project's summary.
function input(name) {
  const projects = body(name, "/v1/projects");
  return { operations: body(name, "/v1/operations?view=summary"), projects: okEntry(projects),
    projectOps: Object.fromEntries(projects.projects.map((p) => [p.id, okEntry(body(name, `/v1/projects/${p.id}/operations?view=summary`))])) };
}

test("every operation is a row, joined to its project; filters by kind", () => {
  const all = experimentsModel(input("final"), "all");
  assert.equal(all.rows.length, body("final", "/v1/operations?view=summary").operations.length);
  const projects = new Set(input("final").projects.data.projects.map((p) => p.id));
  assert.ok(all.rows.every((r) => projects.has(r.project) || r.project === null));
  const campaigns = experimentsModel(input("final"), "campaigns");
  assert.ok(campaigns.rows.every((r) => r.kind === "campaign"));
  assert.ok(experimentsModel(input("final"), "evaluations").rows.every((r) => r.kind === "evaluation"));
});

test("a live campaign shows its phase and round, no duration", () => {
  const row = experimentsModel(input("running"), "all").rows.find((r) => r.status.tone === "blue");
  assert.match(row.progress, /round/i);
  assert.equal(row.took, "—");
});

test("empty home", () => {
  assert.equal(experimentsModel({ operations: body("empty", "/v1/operations?view=summary"),
                                  projects: okEntry(body("empty", "/v1/projects")), projectOps: {} }, "all").empty, true);
});

test("an unreadable progress section shows its state, the row stays", () => {
  const data = structuredClone(input("final"));
  const row = data.operations.operations[0];
  row.progress = null; row.progress_state = "unavailable";
  const model = experimentsModel(data, "all");
  assert.equal(model.rows.length, data.operations.operations.length);
  assert.match(model.rows[0].progress, /unavailable/i);
});

test("final rows: suspended campaign amber, outcome with champion, duration of a finished run", () => {
  const rows = experimentsModel(input("final"), "all").rows;
  const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
  const ops = body("final", "/v1/operations?view=summary").operations;
  const suspended = ops.find((o) => o.resume);
  assert.deepEqual(byId[suspended.id].status, { text: "Suspended", tone: "amber" });
  const completed = ops.find((o) => o.outcome?.champion);
  assert.equal(byId[completed.id].result, `Completed · champion ${completed.outcome.champion}`);
  assert.equal(byId[completed.id].status.tone, "green");
  assert.notEqual(byId[completed.id].took, "—");
  assert.equal(byId[completed.id].href, `#/operations/${completed.id}`);
  const evaluation = ops.find((o) => o.kind === "evaluation");
  assert.equal(byId[evaluation.id].progress, "1/1 scored");
  assert.equal(byId[evaluation.id].result, "—");
  assert.equal(byId[evaluation.id].results.text, "Results present");
});

test("times unavailable: started names the section state and its reason code, no duration", () => {
  const ops = body("final", "/v1/operations?view=summary").operations;
  const deleted = ops.find((o) => o.times_state === "unavailable");
  const row = experimentsModel(input("final"), "all").rows.find((r) => r.id === deleted.id);
  assert.equal(deleted.times_reason, "unreadable");
  assert.equal(row.started, "Unavailable · Unreadable");
  assert.equal(row.took, "—");
  assert.equal(row.results.text, "Deleted");
});

test("join by the project summary: operation.id, else link.object_id; unknown operation has no project", () => {
  const data = structuredClone(input("final"));
  const summary = data.projectOps.demo.data.operations;
  const [first, second] = summary;
  const secondId = second.operation.id;
  second.operation = null; second.operation_state = "other_pair";
  data.operations.operations.push({ ...data.operations.operations[0], id: "f".repeat(32) });
  const byId = Object.fromEntries(experimentsModel(data, "all").rows.map((r) => [r.id, r]));
  assert.equal(byId[first.operation.id].project, "demo");
  assert.equal(byId[secondId].project, "demo");
  assert.equal(byId["f".repeat(32)].project, null);
  assert.deepEqual(byId["f".repeat(32)].projectRead, { state: "ok", code: null, stale: false });  // "—" is an answer here
});

test("project column: unknown, not '—', while a summary that could name the row failed or has not answered", () => {
  const data = structuredClone(input("final"));
  data.projects.data.projects.push({ ...data.projects.data.projects[0], id: "second" });
  const stray = "f".repeat(32);
  data.operations.operations.push({ ...data.operations.operations[0], id: stray });
  const demoOp = data.projectOps.demo.data.operations.find((r) => r.operation).operation.id;
  const row = (projectOps, projects = data.projects) =>
    experimentsModel({ ...data, projects, projectOps }, "all").rows.find((r) => r.id === stray);
  const failedSecond = { state: "error", data: undefined, code: "ambiguous_implementation", stale: false };
  assert.deepEqual(row({ ...data.projectOps, second: failedSecond }).projectRead,
                   { state: "unavailable", code: "ambiguous_implementation", stale: false });
  assert.deepEqual(row({ ...data.projectOps, second: LOADING }).projectRead, { state: "loading", code: null, stale: false });
  assert.equal(row({ ...data.projectOps, second: okEntry({ operations: [] }) }).projectRead.state, "ok");
  // The project list itself failed: no join is possible, every unmatched row says why.
  const noList = { state: "error", data: undefined, code: "service_unavailable", stale: false };
  assert.deepEqual(row({}, noList).projectRead, { state: "unavailable", code: "service_unavailable", stale: false });
  // A row a summary did name keeps its project whatever the other reads do.
  const named = experimentsModel({ ...data, projectOps: { ...data.projectOps, second: failedSecond } }, "all")
    .rows.find((r) => r.id === demoOp);
  assert.deepEqual([named.project, named.projectRead.state], ["demo", "ok"]);
});

test("counts cover the whole list, filters do not change them", () => {
  const model = experimentsModel(input("running"), "evaluations");
  assert.deepEqual(model.counts, { all: 2, active: 1, projects: 1, projectsRead: { state: "ok", code: null, stale: false } });
  assert.equal(model.rows.length, 1);
  assert.equal(model.empty, false);
});

test("project count: exact only when the join is complete; unknown with the read state otherwise", () => {
  const data = input("final");
  const count = (projectOps, projects = data.projects) => experimentsModel({ ...data, projects, projectOps }, "all").counts;
  assert.equal(count(data.projectOps).projects, 2);
  const failing = count({ ...data.projectOps, demo: { state: "error", data: undefined, code: "ambiguous_implementation", stale: false } });
  assert.deepEqual([failing.projects, failing.projectsRead], [null, { state: "unavailable", code: "ambiguous_implementation", stale: false }]);
  assert.equal(failing.all, data.operations.operations.length);                  // the list itself is known
  const loading = count({ ...data.projectOps, demo: LOADING });
  assert.deepEqual([loading.projects, loading.projectsRead.state], [null, "loading"]);
  const noList = count({}, { state: "error", data: undefined, code: "service_unavailable", stale: false });
  assert.deepEqual([noList.projects, noList.projectsRead.code], [null, "service_unavailable"]);
  const stale = count({ ...data.projectOps, demo: { ...data.projectOps.demo, state: "error", code: "network_error", stale: true } });
  assert.deepEqual([stale.projects, stale.projectsRead.stale], [2, true]);       // the last answer, marked stale
});
