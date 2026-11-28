// lib/views/diff.js — screen 9, the full diff (spec 2026-09-30 §3.2): the version pairs a campaign offers and
// one lab-operation-diff/2 answer as files with +/− or nontext markers and hunks. Arithmetic over the served
// text only (§2.1); no judgments.
import { diffFiles } from "../diff.js";
import { href } from "../route.js";
import { supportedCampaign } from "./campaign.js";

const short = (digest) => (typeof digest === "string" ? digest.slice(0, 8) : "—");

export function refName(ref) {
  if (ref === "initial") return "the original agent";
  if (typeof ref === "string" && ref.startsWith("champion:")) return ref.slice("champion:".length);
  if (typeof ref === "string" && ref.startsWith("attempt:")) return `attempt ${ref.slice("attempt:".length)}`;
  return String(ref ?? "—");
}

const pair = (operationId, from, to, link) => ({
  from, to, text: `${refName(to ?? from)} against ${refName(from)}`, href: link ?? href.diff(operationId, from, to) });

// Which comparisons the campaign offers. `to: null` is the bare /diff (the service names the final champion).
export function diffPairs(campaign, operationId) {
  campaign = supportedCampaign(campaign);
  const champions = campaign?.champions ?? [];
  const pairs = [];
  const last = champions[champions.length - 1];
  if (last) pairs.push({ from: "initial", to: null, text: `${last.version} against the original agent (final champion)`, href: href.diff(operationId) });
  for (const c of champions) if (c.version !== "v0") pairs.push(pair(operationId, "initial", `champion:${c.version}`));
  for (let i = 1; i < champions.length; i += 1)
    pairs.push(pair(operationId, `champion:${champions[i - 1].version}`, `champion:${champions[i].version}`));
  for (const round of campaign?.rounds ?? [])
    for (const a of round.attempts ?? [])
      if (a.has_tree && round.baseline_version)
        pairs.push(pair(operationId, `champion:${round.baseline_version}`, `attempt:${a.attempt_id}`));
  return pairs;
}

// Why the selector may be empty: "present" (pairs), "unavailable" / "not_started" (the campaign's champions_state,
// where champions is null or not yet written) or "empty" (present, but nothing to compare yet). `attempts`: the
// details reason while details_state is unavailable — an attempt pair needs its round's baseline_version, which
// the details read carries, so attempt pairs are then unknown, not absent (null otherwise).
export function pairsState(campaign, operationId) {
  if (campaign && !supportedCampaign(campaign)) return { state: "unavailable", pairs: [], attempts: null };
  const pairs = diffPairs(campaign, operationId);
  const state = pairs.length > 0 ? "present"
    : campaign?.champions_state === "unavailable" || campaign?.champions_state === "not_started" ? campaign.champions_state : "empty";
  const attempts = campaign?.details_state === "unavailable" ? (campaign.details_reason ?? "unavailable") : null;
  return { state, pairs, attempts };
}

export function diffModel({ diff, from = null, to = null }) {
  const parsed = diffFiles(diff);
  const hasRefs = diff?.from !== undefined && diff?.to !== undefined;
  return {
    title: hasRefs ? `${refName(diff.to)} against ${refName(diff.from)}`
      : `${refName(to ?? "the selected version")} against ${refName(from ?? "initial")}`,
    digests: diff ? `${short(diff.source_digest)} → ${short(diff.selected_digest)}` : "",
    files: parsed.files, added: parsed.added, removed: parsed.removed, state: parsed.state, error: parsed.error,
  };
}
