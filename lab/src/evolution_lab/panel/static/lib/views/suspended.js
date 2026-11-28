// lib/views/suspended.js — the suspended-campaign screen's model (spec 2026-09-30 §3.2 screen 7; prototype
// lines 160–177). Joins answers only: the cause is the campaign view's last suspension (never result.core),
// the verdict and continuation line are Lab's (same resumeCard as the Project screen), the starts are the
// operation's own, the strip is the current round's baseline measurement.
import { label } from "../labels.js";
import { href } from "../route.js";
import { okEntry, readOf } from "../resource.js";
import { measurementOpen, primaryMetric, sampleOrder, stripRow } from "../samples.js";
import { campaignModel, resultOf, campaignSchema, supportedCampaign } from "./campaign.js";
import { resumeCard } from "./project.js";

export function causeOf(campaign) {
  const last = (campaign?.suspensions ?? []).at(-1);
  return last ? { owner: last.owner, reason: last.reason,
    label: last.reason != null && label("suspension_reason", last.reason).known ? label("suspension_reason", last.reason) : null,
    evidence: last.evidence_kind ?? null, seq: last.seq ?? null } : null;
}

function startsOf(operation) {
  return (operation?.starts ?? []).map((s) => ({
    start: s.start,
    target: s.target_ref?.id ?? null,
    exit: s.process?.exit_code ?? "—",
    cleanup: s.target_cleanup?.state ?? "—",
    settlement: label("settlement", s.settlement),
    failure: s.failure?.code ?? null,
  }));
}

// The current round's baseline row. `read`: the measurement's own; without a campaign answer — loading;
// `none`: the campaign answered but names no baseline for the current round.
function stripOf(operation, campaign, baseline, campaignRead) {
  const opId = operation?.id ?? campaign?.operation_id;
  const current = operation?.progress?.rounds?.current ?? campaign?.progress?.rounds?.current ?? null;
  const round = (campaign?.rounds ?? []).find((r) => r.round === current);
  const id = round?.baseline_measurement_id ?? null;
  const base = { round: current, id, breakdown: "lab", metric: primaryMetric("campaign", campaign) };
  if (!id) return { ...base, read: campaignRead, href: null, none: Boolean(campaign) };
  const read = readOf(baseline);
  const row = { ...base, href: href.measurement(opId, id), read, none: false };
  if (read.state !== "ok") return row;
  const cells = stripRow(baseline.data, sampleOrder([baseline.data]), base.metric, measurementOpen(operation, campaign, id));
  return { ...row, cells: cells.cells, stat: `${cells.scored}/${cells.total} scored` };
}

// campaignRead: the /campaign read's own state (lib/resource.js readOf); default: derived from `campaign`.
// Another version of the campaign view is checked first and read as unavailable (unsupported_schema), whatever
// the caller passed as campaignRead: none of its fields is read (codex r1 #3).
export function suspendedModel({ operation, campaign: body, report, baseline, campaignRead = readOf(body ? okEntry(body) : undefined) }) {
  const schema = campaignSchema(body);
  const campaign = supportedCampaign(body);
  if (!schema.ok) campaignRead = { state: "unavailable", code: schema.code, stale: false };
  const view = campaignModel({ operation, campaign, measurements: {} });
  return {
    schema,
    cause: causeOf(campaign),
    card: resumeCard({ title: "Resume verdict", href: null }, operation, operation.resume, report),
    starts: startsOf(operation),
    strip: stripOf(operation, campaign, baseline, campaignRead),
    details: { state: campaign?.details_state ?? "present", reason: campaign?.details_reason ?? null },
    result: campaignRead.state === "ok" ? view.result : resultOf(undefined, operation),
    setup: view.setup,
    lineage: view.lineage,
    rounds: view.rounds,
  };
}
