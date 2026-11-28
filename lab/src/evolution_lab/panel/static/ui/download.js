// ui/download.js — the report button of campaign, suspended and evaluation screens; the error code next to it.
import { html, useState } from "../vendor/htm-preact.js";
import { reportDownload, saveReport } from "../lib/download.js";

export function ReportDownload({ api, operation }) {
  const [code, setCode] = useState(null);
  const offer = reportDownload(operation);
  if (offer === null) return null;
  const save = async () => {
    const result = await saveReport({ api, offer, doc: document, url: URL });
    setCode(result.ok ? null : result.code);
  };
  return html`<span class="report-download"><button type="button" class="command-copy" onClick=${save}>Download report (.md)</button>${code && html` <code class="small">${code}</code>`}</span>`;
}
