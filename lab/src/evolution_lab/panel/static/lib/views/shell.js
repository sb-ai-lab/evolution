// lib/views/shell.js — models of the shell: service banner, sidebar tree, the one live operation.
// Pure functions over service answers (spec §2.2, §2.3); no fetch, no DOM.
import { label } from "../labels.js";
import { href } from "../route.js";
import { readOf } from "../resource.js";

// Panel health lab-panel-health/1: {service: {state: "ok"} | {state: "service_unavailable", hint}}.
export function serviceState(panelHealth) {
  const service = panelHealth?.service;
  if (!service) return { ok: false, state: "unknown", hint: null };
  return { ok: service.state === "ok", state: service.state, hint: service.hint ?? null };
}

// lab-service-health/2: Owner runs at most one operation, and this is it (spec §2.3).
export const activeOperationId = (serviceHealth) => serviceHealth?.active_operation_id ?? null;

// The same rule as the Projects "Evolve" column: an operation with a resume verdict is a suspended
// campaign (PDR-0015), whatever its control_state says.
export function operationTone(controlState, resume) {
  return resume ? "amber" : label("control_state", controlState).tone;
}

// Experiments of a project are the rows of /v1/projects/<P>/operations?view=summary. Only rows of
// this pair (`operation_state: present`) open a screen; another pair's operation is never read.
// `short` is the row's label without the "<display name> · " prefix Lab puts on every label.
function experiment(row, projectName, live) {
  const operation = row.operation_state === "present" ? mergeLive(row.operation, live) : null;
  const id = operation?.id ?? row.link?.object_id ?? null;
  const text = row.label ?? "";
  const prefix = `${projectName} · `;
  return {
    id,
    label: text,
    short: text.startsWith(prefix) ? text.slice(prefix.length) : text,
    href: operation ? href.operation(id) : null,
    dot: operation ? operationTone(operation.control_state, operation.resume) : "neutral",
  };
}

// projectOperations: {project id: entry} (lib/resource.js) of each project's summary read. `read` is that
// read's own state: experiments come only from an answer, and an empty list only from an empty answer —
// a summary not read yet or failed is "loading" / "unavailable" with its code, never "none yet".
// `live` ({id, payload} of the page's one stream, or null) repaints the active operation's row.
export function sidebarModel(projectsBody, projectOperations, live = null) {
  return (projectsBody?.projects ?? []).map((project) => {
    const name = project.name ?? project.id;
    const entry = projectOperations?.[project.id];
    return {
      id: project.id,
      name,
      state: label("project_state", project.state),
      href: href.project(project.id),
      factsHref: href.facts(project.id),
      read: readOf(entry),
      experiments: (entry?.data?.operations ?? []).map((row) => experiment(row, name, live)),
    };
  });
}

// A live status frame carries the operation's small summary, not its id: merge it over the operation
// it belongs to (live.id), leave any other operation untouched.
const LIVE_FIELDS = ["control_state", "progress", "progress_state", "times", "times_state", "times_source",
                     "times_reason", "results_state"];
export function mergeLive(operation, live) {
  if (!operation || !live || operation.id !== live.id || !live.payload) return operation;
  const merged = { ...operation };
  for (const key of LIVE_FIELDS) if (Object.hasOwn(live.payload, key)) merged[key] = live.payload[key];
  return merged;
}

// lab-operation progress in one line: phase label, round N/M, coverage (campaign or evaluation).
export function progressSummary(progress) {
  if (!progress) return null;
  const parts = [];
  if (progress.kind === "campaign") {
    if (progress.phase) parts.push(label("phase", progress.phase).text);
    const rounds = progress.rounds;
    const round = rounds ? (rounds.current ?? rounds.completed) : null;
    if (round !== null && round !== undefined) parts.push(`round ${round}/${rounds.planned ?? "?"}`);
  }
  if (progress.coverage) parts.push(`${progress.coverage.scored}/${progress.coverage.expected} scored`);
  return parts.length ? parts.join(" · ") : null;
}

// The live overlay ({id, payload, finalEpoch?}) is authoritative only while its stream is: for the active
// operation, and not while the stream is held closed after a service loss. Otherwise fresh reads rule.
export function liveOverlay(live, { activeId, lost }) {
  return live && !lost && live.id === activeId ? live : null;
}

// After a terminal frame the shell re-reads everything at `live.finalEpoch`; once a resource's data comes
// from that re-read (dataEpoch >= finalEpoch), the final snapshot — not the last frame — is the truth.
export function overlayFor(overlay, dataEpoch) {
  return overlay && overlay.finalEpoch !== undefined && dataEpoch >= overlay.finalEpoch ? null : overlay;
}

// One health observation — {panel, error} of useHealth: the panel's answer, or the code of its failed
// request — over the previous {outage}. An outage is observed when the panel does not answer or says the
// service is not ok; it is not the initial "no answer yet" (panel and error both null: returns prev).
// `recover` (reopen the stream, re-read every resource) when the service is ok again after an observed
// outage, from the first poll on, or after the stream gave up on it (lost).
export function healthStep(prev, { panel, error }, lost = false) {
  if (panel === null && error === null) return { outage: prev.outage, recover: false };
  if (error !== null || !serviceState(panel).ok) return { outage: true, recover: false };
  return { outage: false, recover: prev.outage || lost };
}

// A section that is not present: its state, plus Lab's reason code when it gives one ("Unavailable · Unreadable").
export function sectionText(state, reason) {
  const text = label("section_state", state).text;
  return reason ? `${text} · ${label("unavailable_reason", reason).text}` : text;
}
