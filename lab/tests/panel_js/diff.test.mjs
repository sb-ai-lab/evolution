import { test } from "node:test";
import assert from "node:assert/strict";
import { body, snapshot, staticModule } from "./support.mjs";

const { parseUnified, diffFiles, DiffError } = await staticModule("lib/diff.js");

test("a content line that looks like a header stays inside its hunk", () => {
  const text = "--- a/x.md\n+++ b/x.md\n@@ -1,2 +1,1 @@\n--- not a header\n keep\n";
  const [file] = parseUnified(text);
  assert.equal(file.path, "x.md");
  assert.deepEqual([file.added, file.removed], [0, 1]);
});

test("addition, deletion and an empty added file", () => {
  const text = "--- /dev/null\n+++ b/new.md\n@@ -0,0 +1 @@\n+hello\n"
    + "--- a/old.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    + "--- /dev/null\n+++ b/empty.md\n";
  const files = parseUnified(text);
  assert.deepEqual(files.map((f) => [f.path, f.status, f.added, f.removed]),
    [["new.md", "add", 1, 0], ["old.md", "delete", 0, 1], ["empty.md", "add", 0, 0]]);
});

test("no-newline notes are not counted", () => {
  const text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n";
  const [file] = parseUnified(text);
  assert.deepEqual([file.added, file.removed], [1, 1]);
  assert.equal(file.hunks[0].lines.filter((l) => l.kind === "note").length, 2);
});

test("a truncated hunk is an error, not a guess", () => {
  assert.throws(() => parseUnified("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n"), DiffError);
});

test("nontext-only change and a file with text plus a mode change", () => {
  const onlyBinary = diffFiles({ state: "changed", text: "", nontext_changes: [{ path: "img.bin", kind: "binary" }] });
  assert.deepEqual(onlyBinary.files.map((f) => [f.path, f.added, f.markers]), [["img.bin", null, ["binary"]]]);
  const both = diffFiles({ state: "changed", text: "--- a/run.sh\n+++ b/run.sh\n@@ -1 +1 @@\n-a\n+b\n",
                           nontext_changes: [{ path: "run.sh", kind: "mode" }] });
  assert.equal(both.files.length, 1);
  assert.deepEqual([both.files[0].added, both.files[0].removed, both.files[0].markers], [1, 1, ["mode"]]);
  assert.deepEqual([both.added, both.removed], [1, 1]);
});

test("no_change and an unparsable text", () => {
  assert.deepEqual(diffFiles({ state: "no_change", text: "", nontext_changes: [] }).files, []);
  const broken = diffFiles({ state: "changed", text: "garbage\n", nontext_changes: [] });
  assert.equal(broken.error, "diff_unparsed");
});

test("every captured diff parses", () => {
  for (const name of ["suspended-target", "final", "details"]) {
    for (const [key, entry] of Object.entries(snapshot(name).routes)) {
      if (!/\/diff(\?|$)/.test(key) || entry.status !== 200) continue;
      const result = diffFiles(entry.body);
      assert.equal(result.error, null, `${name} ${key}`);
    }
  }
});
