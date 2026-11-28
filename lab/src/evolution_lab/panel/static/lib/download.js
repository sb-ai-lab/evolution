// lib/download.js — "Download report (.md)" (spec 2026-10-14 report-markdown §5, PDR-0026): offered while the
// operation's results are present; the text read of report.md saved through a Blob link. The panel stays read-only.
import { paths } from "./api.js";

export const REPORT_TYPE = "text/markdown";

export function reportDownload(operation) {
  if (operation?.results_state !== "present") return null;
  const path = paths.reportMarkdown(operation.id);
  return path === null ? null : { path, filename: `report-${operation.id}.md` };
}

// `doc` and `url` are the page's document and URL (fakes in tests); `later` defers revoking the Blob URL.
export async function saveReport({ api, offer, doc, url, later = (fn) => setTimeout(fn, 1000) }) {
  let text;
  try {
    text = await api.getText(offer.path, REPORT_TYPE);
  } catch (error) {
    return { ok: false, code: error?.code ?? "unknown" };
  }
  const href = url.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const link = doc.createElement("a");
  link.href = href;
  link.download = offer.filename;
  doc.body.appendChild(link);
  link.click();
  link.remove();
  later(() => url.revokeObjectURL(href));
  return { ok: true, code: null };
}
