// screens/not-found.js — a hash that names no screen, or an id outside the segment grammar.
import { html } from "../vendor/htm-preact.js";
import { href } from "../lib/route.js";
import { Crumbs } from "../ui/shell.js";

export const NotFoundScreen = ({ route }) => html`<div class="screen" data-screen="not_found">
  <${Crumbs} items=${[{ text: "Projects", href: href.projects() }, { text: "Not found", href: null }]} />
  <div class="title"><h1>Nothing here</h1></div>
  <p class="muted">No screen answers <code>${route.params.hash || "#"}</code>. <a class="link" href=${href.projects()}>Go to projects →</a></p>
</div>`;
