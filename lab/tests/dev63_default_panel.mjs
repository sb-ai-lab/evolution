// Deterministic render of the hook-free independent acceptance surface. Import
// current package bytes, never historical saved Lab projections or an adapter.
import { readFileSync } from "node:fs";
import { staticModule } from "./panel_js/support.mjs";

const { independentModel } = await staticModule("lib/views/independent.js");
const { IndependentBlock } = await staticModule("ui/independent.js");

function tree(node) {
  if (node == null || typeof node === "boolean") return null;
  if (Array.isArray(node)) return node.flatMap((n) => {
    const rendered = tree(n);
    return rendered === null ? [] : Array.isArray(rendered) ? rendered : [rendered];
  });
  if (typeof node !== "object") return String(node);
  if (typeof node.type === "function") return tree(node.type(node.props));
  if (typeof node.type !== "string") throw new Error("Unexpected VNode type");
  const attrs = Object.fromEntries(Object.entries(node.props ?? {})
    .filter(([key]) => key !== "children" && key !== "key")
    .sort(([a], [b]) => a.localeCompare(b)));
  if (Object.values(attrs).some((v) => typeof v === "function") || "dangerouslySetInnerHTML" in attrs)
    throw new Error("Expected hook-free, literal DOM properties");
  return { tag: node.type, attrs, children: tree(node.props?.children) };
}

const campaign = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(tree(IndependentBlock({ model: independentModel(campaign), stale: false }))) + "\n");
