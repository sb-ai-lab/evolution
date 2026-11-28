// Shared read-only routing section on every operation surface.
import { html } from "../vendor/htm-preact.js";
import { routingModel } from "../lib/views/routing.js";
import { routingText } from "../lib/labels.js";
import { BlockReadState, StaleMark, StatusPill, Unavailable } from "./pill.js";

const confirmedTime = (seconds) => {
  const date = new Date(seconds * 1000);
  return Number.isNaN(date.getTime()) ? `${seconds} (Unix seconds)` : date.toISOString();
};
const requestedText = (value) => value === null ? "Not specified" : Array.isArray(value) ? value.join(" → ") : value;

export function RoutingSection({ operationId, entry }) {
  const model = routingModel({ operationId, entry });
  if (model.hidden) return null;
  const { routing, read } = model;
  return html`<section class=${`block routing${read.stale ? " is-stale" : ""}`} data-section="routing">
    <div class="section-h"><h2>${routingText.title}</h2>${read.stale && html`<${StaleMark} />`}</div>
    <${BlockReadState} read=${read} />
    ${routing && html`
      <div class="panel pad routing-history"><h3>Start confirmations</h3>
        ${routing.runs.map((run) => html`<div class="routing-run" key=${run.start}>
          <div class="strong">Start #${run.start} · Lab ${run.lab_version}</div>
          ${run.policy_digest === null ? html`<div class="muted small">No execution policy</div>`
            : html`<div class="small">Confirmed by ${run.author} · channel ${run.channel} · <time>${confirmedTime(run.confirmed_at)}</time></div>
              <div class="mono small">Policy ${run.policy_digest}</div>`}
        </div>`)}</div>
      ${!routing.available ? html`<${Unavailable} kind="state" reason=${routing.reason} />`
        : html`<div class="panel routing-observations">
          <div class="pad small">Shared calls: ${routing.shared} · Outside a use: ${routing.outside}</div>
          <div class="routing-scroll"><table class="grid"><thead><tr>
            <th>Role</th><th>Endpoint</th><th>Upstream model</th><th>Client model</th>
            <th>Requested provider</th><th>Reported provider</th><th class="num">Calls</th><th class="num">Mismatched</th>
          </tr></thead><tbody>${Object.entries(routing.roles).flatMap(([role, rows]) => rows.map((row, index) => html`<tr key=${`${role}:${index}`}>
            <td class="mono">${role}</td><td class="mono">${row.endpoint}</td><td class="mono">${row.upstream_model}</td><td class="mono">${row.client_model}</td>
            <td>${requestedText(row.requested)}</td><td>${row.reported_provider ?? "Not reported"}</td><td class="num">${row.calls}</td>
            <td class="num">${row.mismatched > 0 ? html`<${StatusPill} tone="amber" text=${String(row.mismatched)} />` : row.mismatched}</td>
          </tr>`))}</tbody></table></div>
          ${Object.keys(routing.roles).length === 0 && html`<div class="pad small muted">No attributed role calls recorded</div>`}
        </div>`}
      <div class="cap">${routingText.hint}</div>`}
  </section>`;
}
