import { staticModule } from "./support.mjs";
const { parseUnified } = await staticModule("lib/diff.js");
let input = "";
for await (const chunk of process.stdin) input += chunk;
const out = JSON.parse(input).map((text) => parseUnified(text).map((f) => ({ path: f.path, status: f.status, added: f.added, removed: f.removed })));
process.stdout.write(JSON.stringify(out));
