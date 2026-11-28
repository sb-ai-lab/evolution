// app.js — the shell (spec §2.2, §2.3): hash router, sidebar, service banner, theme, error boundary around
// the screen, and the page's one live stream (the active operation's; 1b backlog items 1 and 2).
import { html, render, useEffect, useErrorBoundary, useMemo, useRef, useState } from "./vendor/htm-preact.js";
import { ACTIVE, createApi, LIST_POLL_MS, paths } from "./lib/api.js";
import { openLive } from "./lib/live.js";
import { parse } from "./lib/route.js";
import { readOf, withOutage } from "./lib/resource.js";
import { activeOperationId, healthStep, liveOverlay, overlayFor, serviceState, sidebarModel } from "./lib/views/shell.js";
import { useHealth, useResource, useResourceMap } from "./ui/hooks.js";
import { ServiceBanner, Sidebar } from "./ui/shell.js";
import { ProjectsScreen } from "./screens/projects.js";
import { ProjectScreen } from "./screens/project.js";
import { FactsScreen } from "./screens/facts.js";
import { ExperimentsScreen } from "./screens/experiments.js";
import { StatesScreen } from "./screens/states.js";
import { OperationScreen } from "./screens/operation.js";
import { MeasurementScreen } from "./screens/measurement.js";
import { DiffScreen } from "./screens/diff.js";
import { NotFoundScreen } from "./screens/not-found.js";

const THEME_KEY = "lab-panel-theme";

// Storage may be absent or refuse (private mode, blocked cookies): the theme is then not remembered.
function storedTheme() {
  try { return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark"; } catch { return "dark"; }
}
function storeTheme(theme) {
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* not remembered */ }
}

const SCREENS = {
  projects: ProjectsScreen,
  project: ProjectScreen,
  facts: FactsScreen,
  experiments: ExperimentsScreen,
  states: StatesScreen,
  operation: OperationScreen,
  measurement: MeasurementScreen,
  diff: DiffScreen,
  not_found: NotFoundScreen,
};

// A render error of the screen stays inside it: the sidebar and the banner keep working.
function ScreenBoundary({ children }) {
  const [error] = useErrorBoundary();
  if (error) return html`<div class="screen-error" data-screen-error>${String(error?.message ?? error)}</div>`;
  return children;
}

function useHash(api) {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => {
      // The cache is per screen (spec §2.3). Clear before the next screen starts reading.
      api.forget();
      setHash(location.hash);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, [api]);
  return hash;
}

function App() {
  const api = useMemo(() => createApi({ fetch: (...args) => window.fetch(...args) }), []);
  const hash = useHash(api);
  const route = useMemo(() => parse(hash), [hash]);
  const [theme, setTheme] = useState(storedTheme);
  // epoch: bumped to re-read every resource now — a terminal frame, a reconnect, the service back.
  const [epoch, setEpoch] = useState(0);
  const epochRef = useRef(0);
  const bump = () => { epochRef.current += 1; setEpoch(epochRef.current); return epochRef.current; };

  useEffect(() => { document.documentElement.setAttribute("data-theme", theme); storeTheme(theme); }, [theme]);

  const health = useHealth(api);
  const service = serviceState(health.panel);
  const activeId = activeOperationId(health.service);

  // The one live stream. After onServiceLost live.js does not reopen it: `lost` holds it closed until the
  // panel health says the service is ok again. Then the stream reopens and every resource is re-read
  // (1b backlog item 1) — the same re-read as after any outage the health poll observed: the panel not
  // answering or the service down, from the first poll on (healthStep). While it lasts, `outage` marks
  // every answer read before it stale, including those read once and not polled.
  const [live, setLive] = useState(null);
  const [lost, setLost] = useState(false);
  const [outage, setOutage] = useState(false);
  useEffect(() => {
    const step = healthStep({ outage }, health, lost);
    if (step.recover) { setLost(false); bump(); }
    setOutage(step.outage);
  }, [health.panel, health.error]);
  useEffect(() => {
    if (!activeId || lost) return undefined;
    setLive(null);  // a (re)opened stream starts from the fresh reads, not from an earlier frame
    const handle = openLive({
      operationId: activeId,
      EventSource: window.EventSource,
      checkHealth: health.check,
      onStatus: (payload) => setLive({ id: activeId, payload }),
      // Terminal frame: the outcome, the last attempt and the lists are only in a fresh read (1b item 2);
      // the frame stays on top only until that final re-read lands (overlayFor).
      onTerminal: () => { const finalEpoch = bump(); setLive((l) => (l && l.id === activeId ? { ...l, finalEpoch } : l)); },
      onReconnected: bump,
      onServiceLost: () => setLost(true),
      checkOperation: () => api.get(paths.operation(activeId)).then((o) => !ACTIVE.has(o.control_state), (e) => e?.status === 404),
    });
    return handle.close;
  }, [activeId, lost]);
  // What screens see: the frame only while its stream is authoritative (liveOverlay).
  const current = liveOverlay(live, { activeId, lost });

  const down = outage || lost;  // answers read before now are the last known ones
  const serviceHealth = withOutage(health.serviceEntry, down);  // the /v1/health read, stale in an outage
  const projects = useResource(api, paths.projects, { pollMs: LIST_POLL_MS, epoch, outage: down });
  const opsPaths = Object.fromEntries((projects.data?.projects ?? []).map((p) => [p.id, paths.projectOperations(p.id)]));
  const projectOps = useResourceMap(api, opsPaths, { pollMs: LIST_POLL_MS, epoch, outage: down });
  const sidebar = sidebarModel(projects.data, projectOps.items, overlayFor(current, projectOps.epoch));

  const Screen = SCREENS[route.screen] ?? NotFoundScreen;
  return html`<div class="layout">
    <${Sidebar} sidebar=${sidebar} projectsRead=${readOf(projects)} service=${service} health=${serviceHealth} route=${route} theme=${theme}
      onToggleTheme=${() => setTheme((t) => (t === "dark" ? "light" : "dark"))} />
    <main class="main">
      ${health.error ? html`<${ServiceBanner} state="unreachable" />`
        : health.panel && !service.ok && html`<${ServiceBanner} state=${service.state} hint=${service.hint} />`}
      <${ScreenBoundary} key=${hash}>
        <${Screen} api=${api} epoch=${epoch} outage=${down} route=${route} projects=${projects} health=${health}
          serviceHealth=${serviceHealth} live=${current} sidebar=${sidebar} projectOperations=${projectOps.items} />
      <//>
    </main>
  </div>`;
}

// Preact renders next to the static "Loading…" line rather than over it: clear it first.
const root = document.getElementById("root");
root.replaceChildren();
// The stored theme goes on <html> before the first paint; the effect in App keeps it in step with the toggle.
document.documentElement.setAttribute("data-theme", storedTheme());
render(html`<${App} />`, root);
