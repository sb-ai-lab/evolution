// lib/views/projects.js — the Projects home (spec §3.1): /v1/projects + one /status per project + /v1/health.
// Arithmetic only (spec §2.1): step counts by state; the champion is the cycle outcome's own field.
import { label } from "../labels.js";
import { date, shortId, time } from "../format.js";
import { href } from "../route.js";
import { readOf } from "../resource.js";
import { activeOperationId, mergeLive } from "./shell.js";

const SEGMENT_TONE = { done: "green", stale: "amber" };  // anything else — not done yet, or unknown

function connect(status) {
  const steps = status?.scenarios?.find((s) => s.id === "connect")?.steps ?? [];
  if (steps.length === 0) return { segments: [], summary: "—" };
  const done = steps.filter((s) => s.state === "done").length;
  const outdated = steps.filter((s) => s.state === "stale").length;
  const summary = `${done}/${steps.length} done` + (outdated ? ` · ${outdated} outdated` : "");
  return { segments: steps.map((s) => SEGMENT_TONE[s.state] ?? "neutral"), summary };
}

// cycles[] are listed current first, then by latest start, newest first (docs/api/projects.md).
function cyclesOf(status) {
  return status?.scenarios?.find((s) => s.id === "evolve")?.cycles ?? null;
}

function evolve(cycles) {
  if (cycles === null) return { text: "—", tone: "neutral", cycles: 0 };
  if (cycles.length === 0) return { text: "No cycles yet", tone: "neutral", cycles: 0 };
  const newest = cycles[0];
  const state = newest.resume ? label("phase", "suspended") : label("control_state", newest.control_state);
  return { text: state.text, tone: state.tone, cycles: cycles.length };
}

function champion(cycles) {
  return (cycles ?? []).find((c) => c.outcome?.champion != null)?.outcome.champion ?? null;
}

// "<id> · <place>" of the implementation the status is about (or the only active one); also the Project header.
export function implementation(project, status) {
  const active = (project?.implementations ?? []).filter((i) => !i.archived);
  const chosen = active.find((i) => i.id === status?.implementation) ?? (active.length === 1 ? active[0] : null);
  if (chosen) return `${chosen.id} · ${chosen.place}`;
  return active.length ? active.map((i) => i.id).join(", ") : "—";
}

// Which projects have a status to read: a project that is not ready has none (its record carries the failure).
export const statusesToRead = (projectsBody) =>
  (projectsBody?.projects ?? []).filter((p) => p.state === "ready").map((p) => p.id);

// `entry`: the project's status entry (lib/resource.js), or null when there is none to read. The Connect,
// Evolve and Champion cells come from an answer only; the row's `status` (readOf, or null) says whether
// there is one — loading, unavailable with its code, or ok (stale: the last answer).
function row(project, entry) {
  const status = entry === null ? null : readOf(entry);
  const cycles = cyclesOf(entry?.data);
  return {
    id: project.id,
    name: project.name ?? project.id,
    href: href.project(project.id),
    impl: implementation(project, entry?.data),
    state: label("project_state", project.state),
    failure: project.failure ?? null,
    status,
    connect: connect(entry?.data),
    evolve: evolve(cycles),
    champion: champion(cycles),
    created: date(project.created_at),
  };
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Four cells of lab-service-health/2 (docs/api/service-s5.md) from an answer; "—" is a field the answer
// lacks. Without an answer the model's `serviceRead` says why (loading / unavailable with the code).
function service(health) {
  const env = health?.service_environment ?? null;
  const blockers = health?.blockers;
  return [
    { label: "Service", value: health?.service_state ?? "—",
      sub: Array.isArray(blockers) ? (blockers.length ? plural(blockers.length, "blocker") : "no blockers") : null },
    { label: "Versions", value: health ? `lab ${health.lab_version} · core ${health.core_version}` : "—", sub: null },
    { label: "Generation", value: health?.owner_generation ? shortId(health.owner_generation) : "—", sub: null },
    { label: "Environment",
      value: env ? `Python ${env.python_version} · ${env.platform?.system ?? "?"} ${env.platform?.machine ?? "?"}` : "—",
      sub: env?.digest ? shortId(env.digest) : null },
  ];
}

// statuses: {project id: entry} of the status reads (statusesToRead); health: the /v1/health entry.
export function projectsModel({ projects, statuses, health }) {
  const list = projects?.projects ?? [];
  const read = new Set(statusesToRead(projects));
  return {
    rows: list.map((p) => row(p, read.has(p.id) ? statuses?.[p.id] : null)),
    service: service(health?.data),
    serviceRead: readOf(health),
    empty: list.length === 0,
  };
}

const ACTIVE = new Set(["reserved", "running", "stopping"]);

// Only complete, fresh project summaries prove which closed operation is latest.
export function nowBlockOf(healthRead, activeEntry, summaryReads) {
  const health = readOf(healthRead);
  if (health.state !== "ok") return { state: health.state, stale: false, code: health.code };
  const id = activeOperationId(healthRead.data);
  if (id) {
    const read = readOf(activeEntry);
    const data = mergeLive(activeEntry?.data, activeEntry?.live);
    const progress = data?.progress;
    return { state: "running", stale: health.stale || read.stale, running: {
      id, shortId: shortId(id), href: href.operation(id), kind: data?.kind ?? null,
      project: activeEntry?.project ?? null, read, phase: progress?.phase ? label("phase", progress.phase).text : null,
      progressState: !progress && data?.progress_state && data.progress_state !== "present"
        ? label("section_state", data.progress_state).text : null,
      coverage: progress?.coverage ?? null,
      lastEvent: data?.times_state === "present" && data.times?.last_event_at ? time(data.times.last_event_at) : null,
    } };
  }
  let latest = null;
  const reads = Object.values(summaryReads ?? {});
  if (reads.every((entry) => { const read = readOf(entry); return read.state === "ok" && !read.stale; })) {
    for (const entry of reads) for (const row of entry.data?.operations ?? []) {
      const op = row.operation;
      if (row.operation_state !== "present" || !op || ACTIVE.has(op.control_state) || op.resume || op.times_state !== "present") continue;
      const at = op.times?.last_event_at;
      const stamp = at ? Date.parse(at) : NaN;
      if (!Number.isFinite(stamp) || (latest && stamp <= latest.stamp)) continue;
      latest = { stamp, kind: op.kind, shortId: shortId(op.id),
        label: op.outcome?.outcome ? label("outcome", op.outcome.outcome).text : label("control_state", op.control_state).text,
        time: time(at).slice(0, 5) };
    }
  }
  if (latest) delete latest.stamp;
  return { state: "idle", stale: health.stale, lastClosed: latest };
}
