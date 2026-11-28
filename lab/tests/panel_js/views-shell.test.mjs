import { test } from "node:test";
import assert from "node:assert/strict";
import { body, snapshot, staticModule } from "./support.mjs";

const shell = await staticModule("lib/views/shell.js");
const { nowBlockOf, projectsModel, statusesToRead } = await staticModule("lib/views/projects.js");
const { LOADING, okEntry } = await staticModule("lib/resource.js");

// The models take entries of lib/resource.js ({state, data, code, stale}), as ui/hooks.js keeps them.
const failed = (code, data) => ({ state: "error", data, code, stale: data !== undefined });

function statuses(name) {
  const projects = body(name, "/v1/projects").projects;
  return Object.fromEntries(projects.map((p) => [p.id, okEntry(body(name, `/v1/projects/${p.id}/status`))]));
}
function summaries(name) {
  const projects = body(name, "/v1/projects").projects;
  return Object.fromEntries(projects.map((p) => [p.id, okEntry(body(name, `/v1/projects/${p.id}/operations?view=summary`))]));
}

test("empty home: no rows, explicit empty flag, service strip present", () => {
  const model = projectsModel({ projects: body("empty", "/v1/projects"), statuses: {}, health: okEntry(body("empty", "/v1/health")) });
  assert.equal(model.rows.length, 0);
  assert.equal(model.empty, true);
  assert.equal(model.service.length, 4);
});

test("final snapshot: connect segments follow step states; champion from the cycle outcome", () => {
  const model = projectsModel({ projects: body("final", "/v1/projects"), statuses: statuses("final"), health: okEntry(body("final", "/v1/health")) });
  const demo = model.rows.find((r) => r.id === "demo");
  const steps = body("final", "/v1/projects/demo/status").scenarios.find((s) => s.id === "connect").steps;
  assert.equal(demo.connect.segments.length, steps.length);
  steps.forEach((s, i) => assert.equal(demo.connect.segments[i], s.state === "done" ? "green" : s.state === "stale" ? "amber" : "neutral"));
  assert.match(demo.champion ?? "", /^v[0-9]+$/);
  assert.equal(demo.href, "#/projects/demo");
});

test("unknown step state renders neutral, not a crash", () => {
  const st = structuredClone(statuses("final"));
  st.demo.data.scenarios.find((s) => s.id === "connect").steps[0].state = "brand_new";
  const model = projectsModel({ projects: body("final", "/v1/projects"), statuses: st, health: okEntry(body("final", "/v1/health")) });
  assert.equal(model.rows.find((r) => r.id === "demo").connect.segments[0], "neutral");
});

test("sidebar lists each project's experiments from the project summary", () => {
  const projects = body("final", "/v1/projects");
  const side = shell.sidebarModel(projects, summaries("final"));
  const demo = side.find((p) => p.id === "demo");
  assert.deepEqual(demo.read, { state: "ok", code: null, stale: false });
  assert.ok(demo.experiments.length >= 3);
  for (const e of demo.experiments) assert.ok(e.href === null || e.href.startsWith("#/operations/"));
});

test("service state from the panel health", () => {
  assert.deepEqual(shell.serviceState({ service: { state: "ok" } }), { ok: true, state: "ok", hint: null });
  assert.deepEqual(shell.serviceState({ service: { state: "service_unavailable", hint: "lab serve --home /h" } }),
    { ok: false, state: "service_unavailable", hint: "lab serve --home /h" });
  assert.equal(shell.activeOperationId(body("running", "/v1/health")), snapshot("running").meta.ids.campaign_running);
});

// Beyond the brief's five: the rules the models apply that the screens rely on.

test("connect summary counts steps by state; evolve reads the newest cycle", () => {
  const st = structuredClone(statuses("final"));
  const steps = st.demo.data.scenarios.find((s) => s.id === "connect").steps;
  steps[1].state = "stale"; steps[2].state = "ready";
  const model = projectsModel({ projects: body("final", "/v1/projects"), statuses: st, health: okEntry(body("final", "/v1/health")) });
  const demo = model.rows.find((r) => r.id === "demo");
  assert.deepEqual(demo.connect.segments.slice(0, 3), ["green", "amber", "neutral"]);
  assert.equal(demo.connect.summary, `${steps.length - 2}/${steps.length} done · 1 outdated`);
  // cycles[0] of the final snapshot is the role-suspended campaign: finished + resume -> Suspended, amber.
  assert.deepEqual({ text: demo.evolve.text, tone: demo.evolve.tone }, { text: "Suspended", tone: "amber" });
  assert.equal(demo.evolve.cycles, 2);
});

test("running snapshot: evolve follows control_state; no champion yet", () => {
  const model = projectsModel({ projects: body("running", "/v1/projects"), statuses: statuses("running"), health: okEntry(body("running", "/v1/health")) });
  const demo = model.rows.find((r) => r.id === "demo");
  assert.deepEqual({ text: demo.evolve.text, tone: demo.evolve.tone }, { text: "Running", tone: "blue" });
  assert.equal(demo.champion, null);
  assert.equal(demo.impl, "demo · agent");
});

test("a project whose status is not read yet keeps its row, marked loading", () => {
  const model = projectsModel({ projects: body("final", "/v1/projects"), statuses: {}, health: undefined });
  const demo = model.rows.find((r) => r.id === "demo");
  assert.deepEqual(demo.status, { state: "loading", code: null, stale: false });
  assert.deepEqual(demo.connect.segments, []);
  assert.equal(demo.champion, null);
  assert.equal(model.service.length, 4);
  assert.ok(model.service.every((cell) => cell.value === "—"));
  assert.deepEqual(model.serviceRead, { state: "loading", code: null, stale: false });   // not "—" as an answer
});

test("service strip reads lab-service-health/2", () => {
  const health = body("final", "/v1/health");
  const [service, versions, generation, env] = projectsModel({ projects: body("final", "/v1/projects"), statuses: {}, health: okEntry(health) }).service;
  assert.equal(service.value, "running");
  assert.equal(service.sub, "1 blocker");
  assert.equal(versions.value, `lab ${health.lab_version} · core ${health.core_version}`);
  assert.equal(generation.value, health.owner_generation.slice(0, 8));
  assert.equal(env.value, `Python ${health.service_environment.python_version} · ${health.service_environment.platform.system} ${health.service_environment.platform.machine}`);
  assert.equal(env.sub, health.service_environment.digest.slice(0, 8));
});

test("sidebar: rows of another pair have no link and a neutral dot; suspended campaigns are amber", () => {
  const projects = body("final", "/v1/projects");
  const ops = structuredClone(body("final", "/v1/projects/demo/operations?view=summary"));
  const other = ops.operations[0];
  other.operation_state = "other_pair"; other.operation = null;
  const demo = shell.sidebarModel(projects, { demo: okEntry(ops) }).find((p) => p.id === "demo");
  assert.deepEqual({ id: demo.experiments[0].id, href: demo.experiments[0].href, dot: demo.experiments[0].dot },
    { id: other.link.object_id, href: null, dot: "neutral" });
  const suspended = demo.experiments.find((e) => e.id === snapshot("final").meta.ids.campaign_suspended_role);
  assert.equal(suspended.dot, "amber");
  assert.equal(demo.factsHref, "#/projects/demo/facts");
  assert.equal(demo.experiments[1].short, "evolve · цикл 1");
});

test("sidebar: the live frame repaints the active operation's dot", () => {
  const id = snapshot("running").meta.ids.campaign_running;
  const projects = body("running", "/v1/projects");
  const ops = { demo: okEntry(body("running", "/v1/projects/demo/operations?view=summary")) };
  const dot = (live) => shell.sidebarModel(projects, ops, live)[0].experiments.find((e) => e.id === id).dot;
  assert.equal(dot(null), "blue");
  assert.equal(dot({ id, payload: { control_state: "finished" } }), "green");
  assert.equal(dot({ id: "other", payload: { control_state: "finished" } }), "blue");
});

test("progress summary: phase, round N/M, coverage", () => {
  const running = body("running", `/v1/operations/${snapshot("running").meta.ids.campaign_running}`).progress;
  assert.equal(shell.progressSummary(running), "Generating candidate · round 1/2");
  assert.equal(shell.progressSummary({ kind: "evaluation", coverage: { expected: 200, scored: 118 } }), "118/200 scored");
  assert.equal(shell.progressSummary({ kind: "campaign", phase: "rebasing", rounds: { completed: 2, current: null, planned: 2 },
                                       coverage: { expected: 4, scored: 1 } }), "rebasing · round 2/2 · 1/4 scored");
  assert.equal(shell.progressSummary(null), null);
});

test("live frames merge over the operation; other operations are untouched", () => {
  const op = body("running", `/v1/operations/${snapshot("running").meta.ids.campaign_running}`);
  const frame = { control_state: "finished", progress: { kind: "campaign", phase: "finished" }, progress_state: "present",
                  times: null, times_state: "unavailable", results_state: "present" };
  const merged = shell.mergeLive(op, { id: op.id, payload: frame });
  assert.equal(merged.control_state, "finished");
  assert.equal(merged.progress.phase, "finished");
  assert.equal(merged.kind, "campaign");
  assert.equal(shell.mergeLive(op, { id: "other", payload: frame }), op);
  assert.equal(shell.mergeLive(undefined, { id: op.id, payload: frame }), undefined);
});

test("live overlay: authoritative only while its stream is", () => {
  const live = { id: "o1", payload: { control_state: "running" } };
  assert.equal(shell.liveOverlay(live, { activeId: "o1", lost: false }), live);           // same id, stream live: kept
  assert.equal(shell.liveOverlay(live, { activeId: null, lost: false }), null);           // active id gone: dropped
  assert.equal(shell.liveOverlay(live, { activeId: "o2", lost: false }), null);           // another operation active
  assert.equal(shell.liveOverlay(live, { activeId: "o1", lost: true }), null);            // stream lost: dropped
  assert.equal(shell.liveOverlay(null, { activeId: "o1", lost: false }), null);
  // The outage case of the review: a stale "running" frame never repaints a finished re-read.
  const finished = { id: "o1", kind: "campaign", control_state: "finished", progress: null };
  assert.equal(shell.mergeLive(finished, shell.liveOverlay(live, { activeId: null, lost: false })).control_state, "finished");
});

test("terminal overlay steps aside once the final re-read has landed", () => {
  const terminal = { id: "o1", payload: { control_state: "finished" }, finalEpoch: 4 };
  assert.equal(shell.overlayFor(terminal, 3), terminal);   // data read before the terminal frame
  assert.equal(shell.overlayFor(terminal, -1), terminal);  // nothing read yet
  assert.equal(shell.overlayFor(terminal, 4), null);       // the final snapshot is in
  assert.equal(shell.overlayFor(terminal, 5), null);
  const running = { id: "o1", payload: { control_state: "running" } };
  assert.equal(shell.overlayFor(running, 99), running);    // no terminal frame yet: frames stay on top
  assert.equal(shell.overlayFor(null, 0), null);
});

// Health observations as useHealth gives them: the panel's answer, or the code of its failed request.
const HEALTHY = { panel: { service: { state: "ok" } }, error: null };
const UNREACHABLE = { panel: null, error: "network_error" };
const SERVICE_DOWN = { panel: { service: { state: "service_unavailable", hint: "lab serve --home /h" } }, error: null };
const NOT_YET = { panel: null, error: null };

// Runs observations through healthStep from the initial {outage: false}; returns [outage, recover] per step.
function walk(observations, lost = []) {
  let state = { outage: false };
  return observations.map((o, i) => {
    const step = shell.healthStep(state, o, lost[i] ?? false);
    state = { outage: step.outage };
    return [step.outage, step.recover];
  });
}

test("recovery: re-read after any observed outage — the panel unreachable or the service down", () => {
  const table = [
    ["healthy -> panel unreachable -> healthy", [HEALTHY, UNREACHABLE, UNREACHABLE, HEALTHY],
     [[false, false], [true, false], [true, false], [false, true]]],
    ["initial failure -> healthy", [NOT_YET, UNREACHABLE, HEALTHY], [[false, false], [true, false], [false, true]]],
    ["initial service_unavailable -> healthy", [SERVICE_DOWN, HEALTHY], [[true, false], [false, true]]],
    ["healthy -> service_unavailable -> healthy", [HEALTHY, SERVICE_DOWN, HEALTHY], [[false, false], [true, false], [false, true]]],
    ["healthy -> healthy: no re-read", [NOT_YET, HEALTHY, HEALTHY, HEALTHY], [[false, false], [false, false], [false, false], [false, false]]],
    ["unreachable -> service down -> healthy: one re-read once the service is ok", [HEALTHY, UNREACHABLE, SERVICE_DOWN, HEALTHY],
     [[false, false], [true, false], [true, false], [false, true]]],
    ["no answer yet keeps the observed outage", [UNREACHABLE, NOT_YET, HEALTHY], [[true, false], [true, false], [false, true]]],
    ["a panel answer without a service state is not ok", [HEALTHY, { panel: {}, error: null }, HEALTHY],
     [[false, false], [true, false], [false, true]]],
  ];
  for (const [name, observations, expected] of table) assert.deepEqual(walk(observations), expected, name);
});

test("recovery: the stream gave up (lost) while health stayed ok — re-read on the next ok answer", () => {
  assert.deepEqual(walk([HEALTHY, HEALTHY, HEALTHY], [false, true, false]), [[false, false], [false, true], [false, false]]);
  assert.deepEqual(walk([SERVICE_DOWN], [true]), [[true, false]]);  // still down: nothing to re-read yet
});

// Tenet inspectable-experiments: a joined read that failed or has not answered is shown as such, never as
// an empty list; "none yet" / "—" only after a successful empty answer.

test("sidebar: each project's summary read has its own state; empty only after an empty answer", () => {
  const projects = structuredClone(body("final", "/v1/projects"));
  projects.projects = [projects.projects[0], { ...projects.projects[0], id: "second", name: "Second" },
                       { ...projects.projects[0], id: "third", name: "Third" }];
  const demoOps = body("final", "/v1/projects/demo/operations?view=summary");
  const side = shell.sidebarModel(projects, {
    demo: okEntry(demoOps),
    second: failed("ambiguous_implementation"),
    // third: not answered yet
  });
  const [demo, second, third] = side;
  assert.deepEqual(demo.read, { state: "ok", code: null, stale: false });
  assert.equal(demo.experiments.length, demoOps.operations.length);        // one failure does not blank another
  assert.deepEqual(second.read, { state: "unavailable", code: "ambiguous_implementation", stale: false });
  assert.deepEqual(second.experiments, []);
  assert.deepEqual(third.read, { state: "loading", code: null, stale: false });
  const empty = shell.sidebarModel(projects, { demo: okEntry({ ...demoOps, operations: [] }) })[0];
  assert.deepEqual([empty.read.state, empty.experiments], ["ok", []]);      // the only "none yet"
});

test("sidebar: a failed re-read keeps the last answer, marked stale", () => {
  const projects = body("final", "/v1/projects");
  const ops = body("final", "/v1/projects/demo/operations?view=summary");
  const [demo] = shell.sidebarModel(projects, { demo: failed("service_unavailable", ops) });
  assert.deepEqual(demo.read, { state: "ok", code: null, stale: true });
  assert.equal(demo.experiments.length, ops.operations.length);
});

test("projects: status cells follow each project's own read — loading, unavailable with the code, ok, stale", () => {
  const projects = structuredClone(body("final", "/v1/projects"));
  const ready = projects.projects[0];
  projects.projects = [ready, { ...ready, id: "busy" }, { ...ready, id: "slow" }, { ...ready, id: "old" },
                       { ...ready, id: "fresh", state: "creating" }];
  assert.deepEqual(statusesToRead(projects), ["demo", "busy", "slow", "old"]);   // a creating project has none
  const status = body("final", "/v1/projects/demo/status");
  const model = projectsModel({ projects, health: undefined, statuses: {
    demo: okEntry(status), busy: failed("ambiguous_implementation"), slow: LOADING, old: failed("network_error", status) } });
  const [demo, busy, slow, old, fresh] = model.rows;
  assert.deepEqual(demo.status, { state: "ok", code: null, stale: false });
  assert.match(demo.champion ?? "", /^v[0-9]+$/);
  assert.deepEqual(busy.status, { state: "unavailable", code: "ambiguous_implementation", stale: false });
  assert.deepEqual(slow.status, { state: "loading", code: null, stale: false });
  assert.deepEqual(old.status, { state: "ok", code: null, stale: true });
  assert.equal(old.champion, demo.champion);
  assert.equal(fresh.status, null);                                            // nothing to read: not a failure
});

test("projects: '—' and 'No cycles yet' only from an answer that has no such rows", () => {
  const projects = body("final", "/v1/projects");
  const status = { ...body("final", "/v1/projects/demo/status"), scenarios: [{ id: "evolve", cycles: [] }] };
  const [row] = projectsModel({ projects, statuses: { demo: okEntry(status) } }).rows;
  assert.deepEqual([row.status.state, row.connect.summary, row.evolve.text, row.champion], ["ok", "—", "No cycles yet", null]);
});

// Codex r2 residual (tenet inspectable-experiments): summaries and aggregates carry the read state too.

test("service strip: loading, unavailable with the code, stale, ok — never four '—' for a failed read", () => {
  const projects = body("final", "/v1/projects");
  const health = body("final", "/v1/health");
  const strip = (entry) => projectsModel({ projects, statuses: {}, health: entry });
  assert.deepEqual(strip(LOADING).serviceRead, { state: "loading", code: null, stale: false });
  assert.deepEqual(strip(failed("service_unavailable")).serviceRead, { state: "unavailable", code: "service_unavailable", stale: false });
  const stale = strip(failed("network_error", health));
  assert.deepEqual(stale.serviceRead, { state: "ok", code: null, stale: true });
  assert.equal(stale.service[0].value, health.service_state);                   // the last answer, marked stale
  assert.deepEqual(strip(okEntry(health)).serviceRead, { state: "ok", code: null, stale: false });
});

test("Now running preserves operation reads, progress state and live overlay", () => {
  const id = snapshot("running").meta.ids.campaign_running;
  const op = body("running", `/v1/operations/${id}`);
  const health = okEntry(body("running", "/v1/health"));
  const model = (entry) => nowBlockOf(health, entry, {});
  assert.equal(model(LOADING).running.read.state, "loading");
  assert.deepEqual([model(failed("service_unavailable")).running.read.state, model(failed("service_unavailable")).running.read.code], ["unavailable", "service_unavailable"]);
  assert.equal(model(okEntry(op)).running.phase, "Generating candidate");
  assert.equal(model(okEntry({ ...op, progress: null, progress_state: "unavailable" })).running.progressState, "Unavailable");
  assert.equal(model(okEntry({ ...op, progress: null, progress_state: "not_started" })).running.progressState, "Not started");
  assert.equal(model(failed("network_error", op)).stale, true);
  assert.equal(nowBlockOf({ ...health, stale: true }, okEntry(op), {}).stale, true);
  const live = model({ ...okEntry(op), live: { id, payload: { progress: null, progress_state: "not_started" } } });
  assert.equal(live.running.progressState, "Not started");
});
