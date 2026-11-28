// lib/views/facts.js — screen 3, Facts & confirmations (spec §3.1; prototype lines 140–150):
// lab-project-facts/4 and lab-project-confirmation-list/1 as Lab sends them. `holds` is read, not derived.
import { label } from "../labels.js";
import { date, shortId } from "../format.js";

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Outdated — the stale step's label (the prototype's "outdated"); otherwise Lab's own `holds`, read as sent.
function state(fact) {
  if (fact.stale_reason) return label("step_state", "stale");
  return label("fact_holds", String(fact.holds === true));
}

// The source in one line, by its kind (docs/api/projects.md "Computed facts", "Confirmed facts").
function detail(fact) {
  const src = fact.source ?? {};
  const parts = [];
  switch (src.kind) {
    case "task": case "operation":
      if (src.id) parts.push(shortId(src.id));
      if (src.pair) parts.push(`lab ${src.pair.lab} / core ${src.pair.core}`);
      break;
    case "confirmation":
      parts.push(src.id, `${src.attests?.path ?? "?"} ${shortId(src.attests?.digest)}`, src.author, src.channel);
      break;
    case "derived":
      parts.push((src.facts ?? []).join(" + "));
      break;
    case "links":
      if (Array.isArray(fact.value))
        parts.push(`${plural(fact.value.length, "campaign")}${fact.value.length ? ": " : ""}${fact.value.map((c) => shortId(c.operation_id)).join(", ")}`);
      break;
    case "revision":
      if (fact.value?.digest) parts.push(shortId(fact.value.digest));
      break;
    case "package_entry":
      if (fact.value?.scope) parts.push(`scope: ${fact.value.scope}`);
      if (Array.isArray(src.errors)) parts.push(plural(src.errors.length, "error"));
      break;
    default:
      if (typeof src.id === "string") parts.push(shortId(src.id));
  }
  if (src.failed) parts.push(`failed: ${src.failed}`);
  if (fact.carried_over?.length) parts.push("carried over");
  return parts.filter(Boolean).join(" · ");
}

export function factsModel({ facts, confirmations }) {
  const list = facts?.facts ?? [];
  const rows = list.map((f) => ({
    id: f.id,
    subject: f.subject?.implementation ?? null,  // null: a project-level fact
    state: state(f),
    // Raw: Lab keeps no closed set of fact source kinds (literals in projects/facts.py), so no label kind.
    knownFrom: f.source?.kind ?? "—",
    detail: detail(f),
    whyOutdated: f.stale_reason ? (f.stale_reason.codes ?? []).map((c) => label("stale_code", c).text).join(", ") : null,
  }));
  return {
    digest: facts?.revision?.digest ? shortId(facts.revision.digest) : null,
    counts: { total: list.length, holding: list.filter((f) => f.holds).length,
              outdated: list.filter((f) => f.stale_reason).length },
    rows,
    confirmations: (confirmations?.confirmations ?? []).map((c) => ({
      id: c.id,
      claim: c.claim,
      implementation: c.implementation ?? null,
      path: c.attests?.path ?? "—",
      digest: shortId(c.attests?.digest),
      author: c.author,
      channel: c.channel,
      created: date(c.created_at),
      revoked: c.revoked ? date(c.revoked.at) : null,
    })),
  };
}
