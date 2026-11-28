// Real screen/component trees with deterministic settled reads; no DOM, timers or service.
// Only hooks are replaced. Stateful children render their actual initial content.
import assert from "node:assert/strict";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { after } from "node:test";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
const copies = new Map();
after(() => { for (const root of copies.values()) rmSync(root, { recursive: true, force: true }); });

export async function settledOperation(staticRoot) {
  const fileUrl = (rel) => pathToFileURL(join(staticRoot, rel)).href;
  if (!copies.has(staticRoot)) {
  const root = mkdtempSync("/tmp/lab-panel-settled-");
  cpSync(staticRoot, root, { recursive: true });
  writeFileSync(join(root, "vendor/htm-preact.js"), `export { html } from ${JSON.stringify(fileUrl("vendor/htm-preact.js"))};
    export const useState = (initial) => [typeof initial === 'function' ? initial() : initial, () => {}];
    export const useRef = (initial) => ({ current: initial });
    export const useEffect = () => {};`);
  writeFileSync(join(root, "ui/hooks.js"), `
    import { campaignEntry } from ${JSON.stringify(fileUrl("lib/views/campaign.js"))};
    import { okEntry, LOADING } from ${JSON.stringify(fileUrl("lib/resource.js"))};
    const s = () => globalThis.__settledOperation;
    export const useResource = (_api, path, opts) => {
      s().reads.push({ path, opts });
      if (path?.endsWith('/routing')) return s().routing ?? LOADING;
      return path && Object.hasOwn(s().routes, path) ? okEntry(s().routes[path].body) : LOADING;
    };
    export const useCampaign = (api, id, opts) => campaignEntry(useResource(api, '/v1/operations/' + id + '/campaign', opts));
    export const useOnDemand = useResource;
    export const useLiveRefresh = () => s().refresh ?? 0;
    export const useAttemptFailures = () => ({});
    export const useMeasurements = (_api, id, ids) => ({ items: Object.fromEntries(ids.map(mid =>
      [mid, s().routes['/v1/operations/' + id + '/measurements/' + mid]
        ? okEntry(s().routes['/v1/operations/' + id + '/measurements/' + mid].body) : LOADING])),
      read: { state: 'ok', stale: false } });
    export const useSuspensions = (_api, id) => useResource(_api, '/v1/operations/' + id + '/suspensions');
  `);
  copies.set(staticRoot, root);
  }
  const { OperationScreen } = await import(pathToFileURL(join(copies.get(staticRoot), "screens/operation.js")));
  return ({ routes, id, routing, epoch = 0, outage = false, refresh = 0 }) => {
    const state = { routes, routing, reads: [], refresh };
    globalThis.__settledOperation = state;
    try {
      return { tree: renderTree(OperationScreen({ api: {}, epoch, outage, live: null,
        route: { params: { operation: id } } })), reads: state.reads };
    } finally { delete globalThis.__settledOperation; }
  };
}

// Compact DOM-equivalent structure, including all tags, attributes and text. Functions are event
// handlers, not rendered attributes; Preact internals/key bookkeeping are deliberately omitted.
export function renderTree(node) {
  if (node == null || typeof node === "boolean") return [];
  if (Array.isArray(node)) return node.flatMap(renderTree);
  if (typeof node !== "object") return [String(node)];
  if (typeof node.type === "function") return renderTree(node.type(node.props));
  assert.equal(node.props?.dangerouslySetInnerHTML, undefined);
  const attrs = Object.fromEntries(Object.entries(node.props ?? {}).filter(([key, value]) =>
    key !== "children" && value !== undefined && typeof value !== "function"));
  return [{ tag: node.type, attrs, children: renderTree(node.props?.children) }];
}
