// Shared campaign status card: each column retains the freshness of its own read.
import { html } from "../vendor/htm-preact.js";
import { BlockReadState, StaleMark, StatusPill } from "./pill.js";

const show = (v) => v == null ? "—" : v;
function Read({ column, children }) {
  const ready = column.state === "ok" || column.state === "stale";
  return html`<div class=${column.state === "stale" ? "is-stale" : ""}>
    ${column.state === "stale" && html`<${StaleMark} />`}
    ${ready ? children : html`<${BlockReadState} read=${column} compact=${true} />`}</div>`;
}
function Entry({ entry, expanded = false, collapsible = false }) {
  const title = [entry.text, entry.code !== entry.text ? entry.code : null, entry.scope].filter(Boolean).join(" ");
  return html`<div class=${expanded ? "status-card-entry" : "status-card-entry status-card-entry-compact"}
    tabindex=${expanded ? undefined : 0} title=${expanded ? undefined : title}><span>${entry.text}</span>
    ${entry.code && entry.code !== entry.text && html` <code>${entry.code}</code>`}
    ${entry.scope && html` <span>${entry.scope}</span>`}
    ${(expanded || collapsible) && entry.known && entry.kind === "notice" && entry.byReason && html`<div class="status-card-entry-extra small text-2">
      ${entry.rounds != null && html`rounds ${entry.rounds} · `}by reason: ${(entry.byReason ?? []).map(([key, n]) => `${key} ${n}`).join(", ") || "—"}
      ${(entry.reflectionErrors ?? []).length > 0 && html` · reflection errors: ${entry.reflectionErrors.map(([key, n]) => `${key} ${n}`).join(", ")}`}</div>`}
    ${(expanded || collapsible) && entry.explanation && html`<div class="status-card-entry-extra small text-2">${entry.explanation}</div>`}</div>`;
}
export function StatusCard({ card, stale = false }) {
  const { now, progress, result } = card.columns;
  const expandable = result.more > 0 || result.entries?.some((e) => e.explanation || (e.known && e.kind === "notice"));
  return html`<section class="panel status-card" aria-label="Campaign status">
    <div class="status-card-column"><div class="service-label">Now</div><${Read} column=${now}>
      ${now.pill && html`<${StatusPill} ...${now.pill} />`}
      <div class="status-card-verdict">${now.verdict ?? now.neutral}</div>
      <div class="status-card-codes">${(now.codes ?? []).join(" · ")}</div>
    </${Read}></div>
    <div class="status-card-column"><div class="service-label">${progress.title ?? "Progress"}</div><${Read} column=${progress}>
      ${progress.missing ? html`<span class="small muted">${progress.missing}</span>` : html`
        <div class="status-card-round">Round ${show(progress.round)} <span>of ${show(progress.planned)}</span></div>
        <div class="small text-2">${show(progress.completed)} completed · ${show(progress.closed)} closed · ${show(progress.running)} running</div>
        ${progress.coverage && html`<div class="small text-2">${progress.coverage.scored} / ${progress.coverage.expected} scored</div>`}`}
    </${Read}></div>
    <div class="status-card-column"><div class="service-label">Result</div><${Read} column=${result}>
      ${result.champions && (result.champions.count !== null
        ? html`<button type="button" class="link status-card-link"
            onClick=${() => { const target = document.getElementById("champion-lineage");
              if (target) { target.tabIndex = -1; target.focus({ preventScroll: true }); target.scrollIntoView(); } }}>${result.champions.count} champions recorded</button>`
        : html`<div class="small muted">${result.champions.text}</div>`)}
      ${expandable ? html`<div class="status-card-result-entries">
        <div class="status-card-visible">${(result.visible ?? []).map((entry, i) => html`<${Entry} entry=${entry} collapsible=${true} key=${i} />`)}</div>
        <details class="status-card-more" onToggle=${(e) => e.currentTarget.parentElement.classList.toggle("is-expanded", e.currentTarget.open)}><summary>
          <span class="link small status-card-more-closed">${result.more ? `+${result.more} more` : "Details"}</span>
          <span class="link small status-card-more-open">Show less</span></summary>
          ${(result.entries ?? []).slice(2).map((entry, i) => html`<${Entry} entry=${entry} expanded=${true} key=${i} />`)}</details></div>`
        : (result.entries ?? []).map((entry, i) => html`<${Entry} entry=${entry} key=${i} />`)}
      ${result.empty && html`<div class="small muted">${result.empty}</div>`}
    </${Read}></div>
  </section>`;
}
