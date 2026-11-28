// Shared status model. Reads are resource entries: their data and freshness are independent.
import { label, verdictText } from "../labels.js";
import { readOf } from "../resource.js";
import { resultOf, statusOf, campaignEntry } from "./campaign.js";
import { causeOf } from "./suspended.js";

const ACTIVE = new Set(["reserved", "running", "stopping"]);
// Only pairs covered by the verdict table and the pinned Core's terminal vocabulary.
const OUTCOMES = {
  stopped: { cancelled: "cancelled", condition_changed: "stopped" },
  completed: { rounds_exhausted: "completed" },
  budget_exhausted: { rollout_time_budget_exhausted: "budget" },
  infrastructure_failed: Object.fromEntries([
    "target_unstable", "environment_unstable", "scorer_unstable", "editor_unresponsive",
    "reflector_unresponsive", "grouping_failed", "probe_unavailable", "scorer_metric_missing", "infrastructure_failed",
  ].map((code) => [code, "infrastructure"])),
};
const fill = (template, fields) => template.replace(/\{(\w+)\}/g, (_, name) => fields[name] ?? "");
const stateOf = (entry) => {
  const read = readOf(entry);
  return { state: read.stale ? "stale" : read.state, ...(read.state === "unavailable" || read.stale ? { code: entry?.code ?? "unknown" } : {}) };
};
const one = (name, value) => value == null ? null : `${name} ${value}`;

export function statusCardOf(operationRead, campaignRead, { screen = "campaign" } = {}) {
  const operationState = stateOf(operationRead);
  const viewEntry = campaignEntry(campaignRead);
  const campaignState = stateOf(viewEntry);
  const readState = { operation: operationState, campaign: campaignState };
  const base = { pill: null, verdict: null, neutral: null, codes: [], columns: {
    now: { ...operationState }, progress: { ...operationState }, result: { ...campaignState },
  }, readState };
  if (!["ok", "stale"].includes(operationState.state)) return base;
  const operation = operationRead.data;
  const viewFresh = campaignState.state === "ok";
  const campaign = viewFresh ? viewEntry.data : null;
  const progress = campaign?.progress ?? operation?.progress ?? null;
  const rounds = progress?.rounds, attempts = progress?.attempts?.current_round;
  const current = rounds?.current ?? rounds?.completed ?? null;
  const planned = rounds?.planned ?? null;
  const round = current == null ? "" : ` in round ${current}${planned == null ? "" : ` of ${planned}`}`;
  const fields = { round, allRounds: planned == null ? " all rounds" : ` all ${planned} rounds`,
    readCode: campaignState.code ?? (campaignState.state === "loading" ? "loading" : "unknown") };
  const control = operation?.control_state;
  const phase = progress?.phase;
  base.pill = screen === "suspended" ? label("phase", "suspended") : statusOf(operation, label("control_state", control), phase);
  const outcome = campaign?.outcome;
  const cause = causeOf(campaign);
  const diag = operation?.refusal ? ["refusal", operation.refusal]
    : operation?.core_terminal ? ["terminal", operation.core_terminal] : operation?.failure ? ["failure", operation.failure] : null;
  let key = null;
  if (outcome) {
    key = OUTCOMES[outcome.outcome]?.[outcome.reason] ?? null;
    fields.outcome = outcome.outcome ?? "—"; fields.reason = outcome.reason ?? "—";
    if (!key) base.neutral = fill(verdictText.neutral, fields);
  } else if (operation?.resume) {
    if (diag) { key = "pauseDiagnostic"; fields.code = diag[1].code ?? "—"; }
    else if (!viewFresh) key = "pauseUnread";
    else { key = "pause"; fields.reason = cause?.label?.text ?? cause?.reason ?? "unknown"; }
  } else if (diag) {
    [key] = diag; fields.code = diag[1].code ?? "—"; fields.terminal = diag[1].terminal ?? "";
  } else if (control === "running") {
    if (phase) {
      key = "runningPhase"; fields.phase = label("phase", phase).text;
      const bits = [];
      if (current != null) bits.push(`round ${current}${planned == null ? "" : ` of ${planned}`}`);
      if (attempts?.closed != null) bits.push(fill(verdictText.attemptsClosed, { closed: attempts.closed,
        plannedAttempts: attempts.planned == null ? "" : fill(verdictText.attemptsPlanned, { planned: attempts.planned }),
        runningAttempts: attempts.running ? fill(verdictText.attemptsRunning, { running: attempts.running }) : "" }));
      fields.runningProgress = bits.length ? ` — ${bits.join(", ")}` : "";
    } else key = "running";
  } else if (control === "reserved" || control === "stopping") key = control;
  else key = viewFresh ? "noOutcome" : "outcomeUnknown";
  if (key) base.verdict = fill(verdictText[key], fields);
  base.codes = viewFresh ? (outcome ? [one("outcome", outcome.outcome), one("reason", outcome.reason)].filter(Boolean)
    : [resultOf(campaign, operation).pending]) : [`outcome unknown — campaign view ${fields.readCode}`];
  if (diag) base.codes.push(...[one(diag[0] === "terminal" ? "core_terminal" : diag[0], diag[1].code), one("stage", diag[1].stage), one("class", diag[1].class), one("owner", diag[1].owner), one("terminal", diag[1].terminal)].filter(Boolean));
  if (screen === "suspended" && cause) base.codes.push(...[one("owner", cause.owner), one("reason", cause.reason), one("evidence_kind", cause.evidence), one("seq", cause.seq)].filter(Boolean));
  Object.assign(base.columns.now, { pill: base.pill, verdict: base.verdict, neutral: base.neutral, codes: base.codes });
  Object.assign(base.columns.progress, { title: ACTIVE.has(control) ? "Progress" : "Where it stopped",
    round: current, planned, completed: rounds?.completed ?? null, closed: attempts?.closed ?? null, running: attempts?.running ?? null,
    coverage: ACTIVE.has(control) && screen !== "suspended" ? progress?.coverage ?? null : null,
    missing: progress ? null : label("section_state", operation?.progress_state ?? "not_started").text });
  const resultCampaign = campaignState.state === "stale" ? viewEntry.data : campaign;
  if (resultCampaign) {
    const result = resultOf(resultCampaign, operation);
    const champState = resultCampaign?.champions_state ?? "not_started";
    const champs = { state: champState, count: champState === "present" ? (resultCampaign?.champions ?? []).length : null,
      href: champState === "present" ? "#champion-lineage" : null, text: champState === "present" ? null : label("section_state", champState).text };
    const entries = [...result.limits.map((x) => ({ ...x, kind: "limit" })), ...result.notices.map((x) => ({ ...x, kind: "notice" }))];
    Object.assign(base.columns.result, { champions: champs, entries, visible: entries.slice(0, 2), more: Math.max(0, entries.length - 2),
      empty: !champs.count && champState === "present" && !entries.length ? "No limits recorded" : null });
  }
  return base;
}
