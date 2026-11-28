// ui/pill.js — state marks (COMPONENTS.md: StatusPill, StatusDot) and the honest "unavailable" block (spec §2.5).
import { html } from "../vendor/htm-preact.js";

const tone = (t) => `tone-${t ?? "neutral"}`;

// {text, tone} is what label(kind, code) returns: an unknown code arrives as its raw text, neutral.
export const StatusPill = ({ text, tone: t }) => html`<span class=${`pill ${tone(t)}`}><i class="dot"></i>${text}</span>`;

export const StatusDot = ({ tone: t }) => html`<i class=${`dot ${tone(t)}`}></i>`;

// kind "core": core does not record it; "lab": Lab does not read it yet; "scope": outside panel v1
// (PDR-0017 non-goals); "state": a section whose *_state is unavailable, with the reason code from the answer.
// `compact`: the same line without the panel box, inside a table cell or the sidebar.
export function Unavailable({ kind, reason, compact = false }) {
  const cls = compact ? "unavailable compact" : "unavailable";
  if (kind === "core") return html`<div class=${cls}>Core does not record this</div>`;
  if (kind === "lab") return html`<div class=${cls}>Not read by Lab yet</div>`;
  if (kind === "scope") return html`<div class=${cls}>Not shown in panel v1</div>`;
  return html`<div class=${cls}>Unavailable (<code>${reason ?? "unknown"}</code>)</div>`;
}

// A read's state where its answer would be (lib/resource.js readOf): a loading note, or Unavailable with the
// read's code; nothing once there is an answer to show. `compact`: inside a table cell or the sidebar.
export function ReadState({ read, loading = "Loading…", compact = false }) {
  if (!read || read.state === "ok") return null;
  if (read.state === "loading") return html`<span class="small muted read-loading">${loading}</span>`;
  return html`<${Unavailable} kind="state" reason=${read.code} compact=${compact} />`;
}

// The read in place of a family14 block (additions mock "Read states"): as ReadState, except that a deletion is its
// own state, "Results deleted" with its code, not "Unavailable" (spec family14 §3.9).
export function BlockReadState({ read, loading = "Loading…", compact = false }) {
  if (read?.state === "unavailable" && read.code === "results_deleted")
    return html`<div class=${compact ? "unavailable compact" : "unavailable"}><span class="pill tone-neutral">Results deleted</span> <code>results_deleted</code></div>`;
  return ReadState({ read, loading, compact });
}

// Beside an item shown from its last answer: the read now fails, or the shell sees an outage.
export const StaleMark = () => html`<span class="small stale-mark">stale</span>`;
