// lib/diff.js — Lab's diff dialect (spec 2026-09-30 §2.1): difflib.unified_diff per file (owner/results.py
// _unified), no `diff --git`. A file opens with `--- a/<path>` | `--- /dev/null` and `+++ b/<path>` |
// `+++ /dev/null` outside any hunk; a hunk `@@ -s[,l] +s[,l] @@` owns exactly as many following lines as its
// left/right lengths say, so a content line `--- …` inside a hunk is never a header. `\ No newline at end of
// file` is a note, not a change. Arithmetic over the served text only (PDR-0017 amendment). No DOM.
export class DiffError extends Error {}

const HUNK = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/;
const side = (name) => (name === "/dev/null" ? null : name.replace(/^[ab]\//, ""));
const KIND = { " ": "context", "+": "add", "-": "remove" };

export function parseUnified(text) {
  const lines = (text ?? "").split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  const files = [];
  let i = 0;
  while (i < lines.length) {
    const minus = lines[i], plus = lines[i + 1];
    if (!minus?.startsWith("--- ") || !plus?.startsWith("+++ ")) throw new DiffError(`line ${i + 1}: expected a file header`);
    const before = side(minus.slice(4)), after = side(plus.slice(4));
    const file = { path: after ?? before, status: before === null ? "add" : after === null ? "delete" : "modify",
                   added: 0, removed: 0, hunks: [] };
    i += 2;
    while (i < lines.length && lines[i].startsWith("@@")) {
      const match = HUNK.exec(lines[i]);
      if (!match) throw new DiffError(`line ${i + 1}: bad hunk header`);
      let left = match[1] === undefined ? 1 : Number(match[1]);
      let right = match[2] === undefined ? 1 : Number(match[2]);
      const hunk = { header: lines[i], lines: [] };
      i += 1;
      while (left > 0 || right > 0 || lines[i]?.startsWith("\\")) {
        const line = lines[i];
        if (line === undefined) throw new DiffError(`line ${i + 1}: hunk ends early`);
        if (line.startsWith("\\")) { hunk.lines.push({ kind: "note", text: line }); i += 1; continue; }
        const kind = KIND[line[0]];
        if (!kind) throw new DiffError(`line ${i + 1}: unexpected line in a hunk`);
        if (kind !== "add") left -= 1;
        if (kind !== "remove") right -= 1;
        if (left < 0 || right < 0) throw new DiffError(`line ${i + 1}: hunk is longer than its header`);
        if (kind === "add") file.added += 1;
        if (kind === "remove") file.removed += 1;
        hunk.lines.push({ kind, text: line.slice(1) });
        i += 1;
      }
      file.hunks.push(hunk);
    }
    files.push(file);
  }
  return files;
}

export function diffFiles(body) {
  const nontext = Array.isArray(body?.nontext_changes) ? body.nontext_changes : [];
  let parsed = [], error = null;
  if (body?.state === "changed" && body.text) {
    try { parsed = parseUnified(body.text); } catch (e) { if (!(e instanceof DiffError)) throw e; error = "diff_unparsed"; }
  }
  const files = parsed.map((f) => ({ ...f, markers: [] }));
  const byPath = new Map(files.map((f) => [f.path, f]));
  for (const row of nontext) {
    let file = byPath.get(row.path);
    if (!file) {
      file = { path: row.path, status: null, added: null, removed: null, hunks: [], markers: [] };
      byPath.set(row.path, file);
      files.push(file);
    }
    file.markers.push(row.kind);
  }
  const sum = (key) => files.reduce((n, f) => n + (f[key] ?? 0), 0);
  return { state: body?.state === "no_change" ? "no_change" : "changed", files, added: sum("added"),
           removed: sum("removed"), error };
}
