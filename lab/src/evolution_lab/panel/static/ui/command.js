// ui/command.js — COMPONENTS.md CommandWell, ResumeVerdictCard. A command is text to copy: the panel never
// runs anything (PDR-0017, spec §3.1 "Все команды на экранах — CommandWell с «Copy»").
import { html, useEffect, useRef, useState } from "../vendor/htm-preact.js";
import { ReadState, StaleMark, StatusPill } from "./pill.js";

// Mono command on a sunk well with "Copy". Without clipboard access (a refusal, an older browser) the text
// is selected instead, for Ctrl+C.
export function CommandWell({ command }) {
  const text = useRef(null);
  const [done, setDone] = useState(null);
  useEffect(() => {
    if (!done) return undefined;
    const timer = setTimeout(() => setDone(null), 2000);
    return () => clearTimeout(timer);
  }, [done]);
  const select = () => {
    const range = document.createRange();
    range.selectNodeContents(text.current);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setDone("Copied");
    } catch {
      select();
      setDone("Selected — press Ctrl+C");
    }
  };
  return html`<div class="command-well">
    <code class="command-text" ref=${text}>${command}</code>
    <button type="button" class="command-copy" onClick=${copy}>${done ?? "Copy"}</button>
  </div>`;
}

// One resume verdict (projectModel().resume[i]): the verdict and its reason code, the target line the model
// chose for the verdict state, and the CLI's continuation line when the CLI prints one (spec §3.3). `tone`
// tints the panel (default: the verdict's own tone). Without a verdict (`read`: the operation read is
// loading or failed) the card says so; `commandRead`: the command waits on the report read; `stale`: the
// verdict is the last answer; `commandStale`: the command's ready target is the report's last answer.
export function ResumeVerdictCard({ title, href, verdict, reason, targetLine, command, commandRead, commandStale, read, stale, tone }) {
  return html`<section class=${`panel resume-card tint-${tone ?? verdict?.tone ?? "neutral"}`} data-resume=${title}>
    <div class="resume-head">
      <span class="strong">${title}</span>
      ${verdict && html`<${StatusPill} text=${verdict.text} tone=${verdict.tone} />`}
      ${reason && html`<code class="small muted">${reason}</code>`}
      ${stale && html`<${StaleMark} />`}
      ${href && html`<a class="resume-open small link" href=${href}>Details →</a>`}
    </div>
    <${ReadState} read=${read} loading="Loading the resume verdict…" />
    ${targetLine && html`<div class="small text-2">${targetLine}</div>`}
    ${command && commandStale && html`<div class="reason-line command-stale"><span class="tiny muted">Ready target from the last report read</span><${StaleMark} /></div>`}
    ${command ? html`<${CommandWell} command=${command} />`
              : html`<${ReadState} read=${commandRead} loading="Loading the ready target…" />`}
  </section>`;
}
