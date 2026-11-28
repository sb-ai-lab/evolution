// lib/views/experiments.js — All experiments (spec §3.1 screen 4): /v1/operations?view=summary, each row
// joined to the project whose /v1/projects/<P>/operations?view=summary lists it. Arithmetic and joins only
// (spec §2.1): no "needs attention" judgment, no mean in Result — the outcome and the champion version.
import { ACTIVE } from "../api.js";
import { label } from "../labels.js";
import { date, duration } from "../format.js";
import { href } from "../route.js";
import { readOf, worstRead } from "../resource.js";
import { operationTone, progressSummary, sectionText } from "./shell.js";

const KINDS = { campaigns: "campaign", evaluations: "evaluation" };

// operation id -> project id. A project summary row names its operation by `operation.id` when the
// operation is of this pair, and by `link.object_id` in every case.
function projectIndex(projectOps) {
  const index = new Map();
  for (const [project, entry] of Object.entries(projectOps ?? {})) {
    for (const row of entry?.data?.operations ?? []) {
      const id = row.operation?.id ?? row.link?.object_id;
      if (id && !index.has(id)) index.set(id, project);
    }
  }
  return index;
}

// A resume verdict reads as suspended, amber, whatever control_state says (the Projects "Evolve" rule).
function status(op) {
  const tone = operationTone(op.control_state, op.resume);
  return { text: op.resume ? label("phase", "suspended").text : label("control_state", op.control_state).text, tone };
}

// Phase and round of a campaign, coverage of either kind; without them, the section's own state.
function progress(op) {
  return progressSummary(op.progress) ?? label("section_state", op.progress_state).text;
}


function result(outcome) {
  if (!outcome) return "—";
  const text = label("outcome", outcome.outcome).text;
  return outcome.champion ? `${text} · champion ${outcome.champion}` : text;
}

// Whether "no project" is an answer: only when the project list and every project's summary were read.
// Otherwise the Project cell of a row no summary names says why it is unknown (loading / unavailable).
function joinRead(projects, projectOps) {
  const list = readOf(projects);
  if (list.state !== "ok") return list;
  return worstRead((projects.data?.projects ?? []).map((p) => readOf(projectOps?.[p.id])));
}

const KNOWN = { state: "ok", code: null, stale: false };

function row(op, index, unmatched) {
  const project = index.get(op.id) ?? null;
  const times = op.times_state === "present" ? op.times : null;
  const active = ACTIVE.has(op.control_state);
  return {
    id: op.id,
    href: href.operation(op.id),
    kind: op.kind,
    status: status(op),
    project,
    projectRead: project === null ? unmatched : KNOWN,
    progress: progress(op),
    result: result(op.outcome),
    results: label("results_state", op.results_state),
    started: times ? date(times.first_event_at) : sectionText(op.times_state, op.times_reason),
    took: !active && times ? duration(times.first_event_at, times.last_event_at) : "—",
  };
}

// operations: the /v1/operations body; projects: the /v1/projects entry and projectOps: {project id: entry}
// of the summaries (lib/resource.js). filter: "all" | "campaigns" | "evaluations" (the screen's chips).
// `empty`: no experiments at all; `counts` are over the whole list, whatever the filter (`projects`: null
// while the join is incomplete, with `projectsRead`). A row's
// `projectRead` is the state of the join: ok (`project` null — no summary lists it), or loading /
// unavailable with the code of the read that could have named it.
export function experimentsModel({ operations, projects, projectOps }, filter = "all") {
  const all = operations?.operations ?? [];
  const index = projectIndex(projectOps);
  const unmatched = joinRead(projects, projectOps);
  const kind = KINDS[filter] ?? null;
  const rows = all.filter((op) => kind === null || op.kind === kind).map((op) => row(op, index, unmatched));
  const joined = new Set(all.map((op) => index.get(op.id)).filter(Boolean));
  // How many projects the experiments span is exact only when the join is complete; otherwise null, and
  // `projectsRead` says why (loading / unavailable with the code of the read that is missing).
  const complete = unmatched.state === "ok";
  return {
    rows,
    empty: all.length === 0,
    counts: { all: all.length, active: all.filter((op) => ACTIVE.has(op.control_state)).length,
              projects: complete ? joined.size : null, projectsRead: unmatched },
  };
}
