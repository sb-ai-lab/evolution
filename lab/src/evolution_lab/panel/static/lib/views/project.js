// lib/views/project.js — screen 2, Project (spec §3.1; prototype lines 101–118): /v1/projects/<P>, its status/6,
// /v1/operations/<O> of the cycles with a resume (or active), and /report where resume.target is set.
// Joins answers only: step states, reasons and the continuation line are Lab's (spec §2.1, §3.3).
import { ACTIVE } from "../api.js";
import { label } from "../labels.js";
import { date, shortId } from "../format.js";
import { href } from "../route.js";
import { continuationLine } from "../continuation.js";
import { implementation } from "./projects.js";
import { progressSummary } from "./shell.js";
import { readOf } from "../resource.js";

// A step source in short form: kind + id-8; a confirmed fact — the latest confirmation's author · channel;
// a computed fact — the kind and id-8 of the same fact's own source in the status, else its name.
function source(entry, factsById) {
  if (!entry || typeof entry !== "object") return "";
  if (typeof entry.fact === "string") {
    const confirmations = entry.confirmations ?? [];
    const latest = confirmations[confirmations.length - 1];
    if (latest) return `${latest.author} · ${latest.channel}`;
    const own = factsById.get(entry.fact)?.source;
    return own?.kind && typeof own.id === "string" ? `${own.kind} ${shortId(own.id)}` : entry.fact;
  }
  if (typeof entry.artifact === "string") return entry.artifact;
  for (const kind of ["bundle", "prepared", "operation", "export"]) {
    if (typeof entry[kind] === "string") return `${kind} ${shortId(entry[kind])}`;
  }
  return "";
}

// Amber note: stale codes, the blocked code (with the schema it names), the first invalid artifact and path, a deleted export's
// results, and what a blocked step misses.
function note(step) {
  const parts = [];
  const codes = step.stale_reason?.codes ?? [];
  if (codes.length) parts.push(codes.map((c) => label("stale_code", c).text).join(", "));
  const blocked = step.blocked_reason;
  if (blocked?.code) parts.push(label("blocked_code", blocked.code).text + (blocked.schema ? ` (${blocked.schema})` : ""));
  const invalid = step.invalid ?? [];
  if (invalid.length) {
    const first = invalid[0];
    const where = `${first.artifact ?? ""}${first.path ?? ""}` || "/";
    parts.push(`invalid: ${where}${invalid.length > 1 ? ` (+${invalid.length - 1})` : ""}`);
  }
  // An export whose results were deleted stays a `done` source, kept as a mark (docs/api/projects.md
  // "After delete-results"): the same fact as the blocked code, in the same words.
  if ((step.sources ?? []).some((s) => typeof s?.export === "string" && s.results_state === "deleted"))
    parts.push(label("blocked_code", "results_deleted").text);
  if (step.state === "blocked" && step.missing?.length) parts.push(`missing: ${step.missing.join(", ")}`);
  return parts.length ? parts.join(" · ") : null;
}

// What the descriptor declares about a step (status/6): shown, never enforced (PDR-0018 p. 5).
function marks(step) {
  const parts = [step.optional === true && "Optional", step.human_only === true && "Human only"].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function steps(list, factsById) {
  return (list ?? []).map((s) => ({
    id: s.id,
    title: s.title ?? s.id,
    state: label("step_state", s.state),
    filled: s.state === "done",
    marks: marks(s),
    source: (s.sources ?? []).map((e) => source(e, factsById)).filter(Boolean).join(", "),
    note: note(s),
    doneAt: s.done_at ? date(s.done_at) : null,
  }));
}

// A pipeline with its steps, "k of n steps done", and its latest done_at in ms (null when no step has one).
function run(base, list, factsById) {
  const mapped = steps(list, factsById);
  const times = (list ?? []).map((s) => Date.parse(s.done_at ?? "")).filter((t) => !Number.isNaN(t));
  return { ...base, steps: mapped, at: times.length ? Math.max(...times) : null,
           progress: `${mapped.filter((s) => s.filled).length} of ${mapped.length} steps done` };
}

// Spec 2026-10-19 project-history: an observation panel shows what ran, newest first — cycles in Owner's order
// (by latest start), and one-off scenarios only once produced, placed before the first cycle with an older
// done_at (unknown time: last). On top: Owner's `current` cycle ("Current"), else the newest run ("Latest").
// next_cycle and unproduced scenarios are what could run, not what ran: never shown.
function historyOf(pipelines) {
  const runs = pipelines.filter((p) => p.kind === "cycle");
  for (const p of pipelines.filter((p) => p.kind === "scenario" && p.produced)) {
    // untimed scenarios already placed stay last: a timed one goes before them too
    const i = p.at === null ? -1 : runs.findIndex((r) => (r.at !== null && r.at < p.at) || (r.kind === "scenario" && r.at === null));
    runs.splice(i < 0 ? runs.length : i, 0, p);
  }
  const current = runs.find((p) => p.current) ?? null;
  const top = current ?? runs[0] ?? null;
  return { focus: top && { label: current ? "Current" : "Latest", pipeline: top }, history: runs.filter((p) => p !== top) };
}

// A resume verdict reads as suspended, amber, whatever control_state says (the Projects "Evolve" rule).
// `operation` (read for a suspended or active cycle, live frame merged) is fresher than the status's copy.
function cycleState(cycle, operation) {
  const control = operation?.control_state ?? cycle.control_state;
  const resume = operation ? operation.resume : cycle.resume;
  if (resume) {
    const suspended = label("phase", "suspended");
    return { text: `${suspended.text} · ${label("resume_state", resume.state).text}`, tone: suspended.tone };
  }
  const state = label("control_state", control);
  const parts = [state.text];
  const progress = operation ? progressSummary(operation.progress) : null;
  if (progress && ACTIVE.has(control)) parts.push(progress);
  if (cycle.outcome?.outcome) parts.push(label("outcome", cycle.outcome.outcome).text);
  if (cycle.detail === "unavailable") parts.push(label("operation_state", "other_pair").text);
  return { text: parts.join(" · "), tone: state.tone };
}

function header(project, status) {
  return {
    name: project?.name ?? project?.id ?? "—",
    state: label("project_state", project?.state),
    impl: project ? implementation(project, status) : "—",
    workingCopy: project?.working_copy?.path ?? "—",
    created: date(project?.created_at),
  };
}

// The cycle number is Lab's own: the project-operations summary labels a campaign link
// "<name> · evolve · цикл N", N numbering the project's campaign links oldest first (docs/api/projects.md
// "operation summary"). It is never derived from the order of cycles[] (by latest start: a resume moves a
// cycle up). Since status/5 the cycle's own `order` is read
// first; the label stays the fallback. Rows join by operation id as in the sidebar (lib/views/shell.js `experiment`).
const TRAILING_NUMBER = /(?:^|\s)([1-9][0-9]*)$/;
function cycleNumbers(summary) {
  const numbers = new Map();
  for (const row of summary?.operations ?? []) {
    const id = row.operation?.id ?? row.link?.object_id;
    const match = typeof row.label === "string" ? TRAILING_NUMBER.exec(row.label) : null;
    if (id && match) numbers.set(id, match[1]);
  }
  return numbers;
}

// The target line of a verdict that continues (ready, cleanup_first): a fresh-target requirement, or none.
// Busy and refused verdicts get no line: the CLI prints no continuation for them, or says it is unavailable.
function targetLine(verdict) {
  if (verdict.state !== "ready" && verdict.state !== "cleanup_first") return null;
  const target = verdict.target;
  if (!target?.requirement) return "No target requirement: any ready target of the same bundle";
  const excluded = (target.excluded ?? []).map((t) => shortId(t.id));
  return `Needs a fresh target (${target.requirement})${excluded.length ? `; excluded: ${excluded.join(", ")}` : ""}`;
}

// A resume card from a fresh operation read (verdict, target line, continuation line). The ready target is
// the report's, read only under a target requirement: until that read answers, the command waits on it
// (`commandRead`) rather than falling back to the "no ready target" wording. `commandStale`: the command's
// ready target is the report's last answer (the read now fails, or the shell sees an outage).
export function resumeCard(base, operation, verdict, reportEntry) {
  const report = verdict.target ? readOf(reportEntry) : null;
  const known = report === null || report.state === "ok";
  const ready = reportEntry?.data?.report?.owner_resume_target?.ready_target ?? null;
  return {
    commandStale: known && report !== null && report.stale,
    ...base,
    verdict: label("resume_state", verdict.state),
    reason: verdict.reason ?? null,
    requirement: verdict.target?.requirement ?? null,
    excluded: (verdict.target?.excluded ?? []).map((t) => shortId(t.id)),
    targetLine: targetLine(verdict),
    command: known ? continuationLine(operation, ready) : null,
    commandRead: known ? null : report,
  };
}

// summary: lab-project-operation-list/2 of the project (the sidebar's read), for the cycle numbers.
// operations, reports: {operation id: entry} (lib/resource.js) of the reads cyclesToRead asks for and of
// the reports of verdicts with a target. A cycle the status shows suspended whose operation read has no
// answer yet (or failed) keeps its card, with `read` saying why there is no verdict.
export function projectModel({ project, status, summary, operations = {}, reports = {} }) {
  const factsById = new Map((status?.facts ?? []).map((f) => [f.id, f]));
  const numbers = cycleNumbers(summary);
  const toRead = new Set(cyclesToRead(status));
  const pipelines = [], resume = [];
  for (const scenario of status?.scenarios ?? []) {
    if (!Array.isArray(scenario.cycles)) {
      pipelines.push(run({ kind: "scenario", produced: scenario.state === "produced", title: scenario.id, opId: null, href: null,
                           state: label("scenario_state", scenario.state) }, scenario.steps, factsById));
      continue;
    }
    for (const cycle of scenario.cycles) {
      const opId = cycle.operation_id;
      // The cycle number is Lab's own: status/5 `order` (W68); the summary label is the fallback for order 0.
      const n = Number.isInteger(cycle.order) && cycle.order > 0 ? cycle.order : numbers.get(opId);
      const title = n ? `${scenario.id} · cycle ${n}` : `${scenario.id} · ${shortId(opId)}`;
      const read = toRead.has(opId) ? readOf(operations[opId]) : null;
      const operation = operations[opId]?.data;
      const opHref = cycle.detail === "available" ? href.operation(opId) : null;
      // `read`: the operation read behind the cycle's progress (null: not read — neither suspended nor active),
      // so a loading or failed read says so where the progress would be, instead of showing nothing.
      pipelines.push(run({ kind: "cycle", current: cycle.current === true, title, opId, href: opHref,
                           state: cycleState(cycle, operation), read }, cycle.steps, factsById));
      const base = { opId, title, href: opHref, read: null, stale: read?.stale ?? false };
      if (read && read.state !== "ok") {
        if (cycle.resume) resume.push({ ...base, read, verdict: null, reason: null, requirement: null, excluded: [],
                                        targetLine: null, command: null, commandRead: null, commandStale: false });
        continue;
      }
      if (operation?.resume) resume.push(resumeCard(base, operation, operation.resume, reports[opId]));
    }
    if (scenario.next_cycle) {
      pipelines.push(run({ kind: "next", title: `${scenario.id} · next cycle`, opId: null, href: null,
                           state: label("scenario_state", scenario.state) }, scenario.next_cycle.steps, factsById));
    }
  }
  return { header: header(project, status), pipelines, resume, ...historyOf(pipelines) };
}

// Which operations the screen reads: cycles of this pair with a resume verdict or an active control state.
export function cyclesToRead(status) {
  return (status?.scenarios ?? []).flatMap((s) => s.cycles ?? [])
    .filter((c) => c.detail === "available" && (c.resume || ACTIVE.has(c.control_state)))
    .map((c) => c.operation_id);
}
