// Mirror of evolution_lab.cli.output.continuation_line — tests/test_panel_continuation.py keeps them equal.
// The same line the CLI prints for a suspended Owner campaign (PDR-0015, spec 2026-09-30 §3.3). The ready
// target comes from the operation report's owner_resume_target.ready_target: the panel never picks one.
const isDict = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

const RESUME = (id) => `lab resume ${id} --target <ready target of the same bundle> --generation <owner generation>`;
const STOP = (target, generation) =>
  `stop target ${target} if it is still running (lab agent stop ${target} --generation ${generation} --wait)`;
const FRESH = (id, target, generation) =>
  `on a fresh target: ${STOP(target, generation)}, start a new target of the same bundle (lab agent start <connection>), `
  + `then lab resume ${id} --target <new target> --generation <owner generation>`;
const READY = (id, target, generation, ready, readyGeneration) =>
  `${STOP(target, generation)}, then lab resume ${id} --target ${ready} --generation ${readyGeneration} `
  + "(a ready target of the same bundle the campaign has not used)";

// continuationLine(status, readyTarget|null) -> string|null: `status` is lab-operation (its id, resume, target_ref).
export function continuationLine(status, readyTarget = null) {
  const resume = status?.resume;
  if (!isDict(resume)) return null;
  const identifier = status.id ?? "";
  let line = RESUME(identifier);
  const target = resume.target;
  if (isDict(target) && Array.isArray(target.excluded) && target.excluded.length > 0) {
    // One running target at a time (spec 2026-09-29): the operation's current target is the one to stop;
    // the last excluded target is only a fallback when the status has no target_ref.
    const current = status.target_ref;
    const last = isDict(current) ? current : target.excluded[target.excluded.length - 1];
    line = readyTarget !== null && readyTarget !== undefined
      ? READY(identifier, last.id, last.owner_generation, readyTarget.id, readyTarget.owner_generation)
      : FRESH(identifier, last.id, last.owner_generation);
  }
  const { state, reason } = resume;
  if (state === "ready") return `continuation: ${line}`;
  if (state === "cleanup_first") return `continuation: lab cleanup operation ${identifier}, then ${line}`;
  if (state === "refused" && reason === "cleanup_unconfirmed")
    return "continuation: not available after manual release; the run directory is kept until results are deleted";
  return null;
}
