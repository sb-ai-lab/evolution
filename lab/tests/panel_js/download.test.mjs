import { test } from "node:test";
import assert from "node:assert/strict";
import { staticModule } from "./support.mjs";

const { REPORT_TYPE, reportDownload, saveReport } = await staticModule("lib/download.js");
const { ApiError } = await staticModule("lib/api.js");

function fakeDom() {
  const clicked = [], revoked = [], blobs = [];
  const doc = {
    body: { appendChild() {} },
    createElement(tag) {
      const el = { tag, href: null, download: null, remove() {},
                   click() { clicked.push({ href: el.href, download: el.download }); } };
      return el;
    },
  };
  const url = { createObjectURL(blob) { blobs.push(blob); return `blob:${blobs.length}`; },
                revokeObjectURL(href) { revoked.push(href); } };
  return { doc, url, clicked, revoked, blobs };
}

const offer = reportDownload({ id: "op1", results_state: "present" });

test("the report is offered only while the operation's results are present", () => {
  assert.deepEqual(offer, { path: "/v1/operations/op1/report.md", filename: "report-op1.md" });
  for (const state of ["deleted", "deleting", "delete_failed", "unavailable", "not_created", undefined]) {
    assert.equal(reportDownload({ id: "op1", results_state: state }), null);
  }
  assert.equal(reportDownload({ id: "a/b", results_state: "present" }), null);
  assert.equal(reportDownload(null), null);
});

test("a 200 answer is saved as report-<id>.md", async () => {
  const dom = fakeDom();
  const api = { async getText(path, type) {
    assert.equal(path, "/v1/operations/op1/report.md"); assert.equal(type, REPORT_TYPE); return "# Кампания\n"; } };
  const result = await saveReport({ api, offer, doc: dom.doc, url: dom.url, later: (fn) => fn() });
  assert.deepEqual(result, { ok: true, code: null });
  assert.deepEqual(dom.clicked, [{ href: "blob:1", download: "report-op1.md" }]);
  assert.equal(await dom.blobs[0].text(), "# Кампания\n");
  assert.deepEqual(dom.revoked, ["blob:1"]);
});

test("an error answer saves nothing and gives its code", async () => {
  const dom = fakeDom();
  const api = { async getText() { throw new ApiError("report_unavailable", 409, null); } };
  const result = await saveReport({ api, offer, doc: dom.doc, url: dom.url, later: (fn) => fn() });
  assert.deepEqual(result, { ok: false, code: "report_unavailable" });
  assert.deepEqual(dom.clicked, []);
  assert.equal(dom.blobs.length, 0);
});
