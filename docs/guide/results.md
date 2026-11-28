# Read the results

[Русский](ru/results.md)

**Goal:** after an evaluation or campaign, find out what really happened —
whether it finished cleanly, what was measured, what changed — and tidy up
afterwards. **You are done when** you have read the status, the report and
its coverage, taken out any campaign change you want, and nothing is left in
`cleanup_required`.

The page has two parts. **[Steps](#steps)** lists what to do in each
situation. **[Reference material](#reference-material)** below it explains
every state and field. The steps link into it when you need detail.

A run in Lab produces several separate pieces of evidence, not one "it
worked" flag: process, Core result, cleanup, persistence and coverage. Read
each one; none stands in for another
([why](#the-rule-separate-facts-are-read-separately)).

In a project campaign, the readout and `report.md` name the baseline source. For an evaluation import, check the source run, imported pairs, pairs to measure, the reasons source rows were left out (including `seed_differs`), condition fields and recorded decisions. `imported_provenance_unknown` marks imported rows with unknown provenance. The readout says when Lab used the saved evaluation summary because it could not read the older pair state, and when Core abandoned the import and measures the full baseline. The optional `baseline` field of `summary.json` records those facts and the counts from Core. See [baseline choices](campaign.md#choose-the-first-baseline).

## Before you start

- You have a state directory with at least one task or operation in it, for
  example from [Run your first evaluation](first-evaluation.md) or
  [Run a campaign](campaign.md).
- You can open an Owner on it (`Owner.open(state)`), or you use the
  [CLI](cli.md) against a running service.
- Keep each task and Target Service ID together with its `owner_generation`. An
  operation you have lost track of is found again with
  `await owner.list_operations()`.
- Terms: *Bundle*, *prepared execution*, *Owner*, *target* and *state
  directory* are defined in [How Lab works](concepts.md).

## Steps

Steps 1–3 apply to every run. Steps 4–6 are for when you need them.

1. [Read a finished task](#step-1-read-a-finished-task)
2. [Check that the operation finished cleanly](#step-2-check-that-the-operation-finished-cleanly)
3. [Read the report and its coverage](#step-3-read-the-report-and-its-coverage)
4. [Get a campaign's changes](#step-4-get-a-campaigns-changes)
5. [Resolve anything left uncertain](#step-5-resolve-anything-left-uncertain)
6. [Delete results you no longer need](#step-6-delete-results-you-no-longer-need)

### Step 1. Read a finished task

Target start and stop, check, conformance, export, cleanup and deletion run
as *tasks*.

**Do:** wait for the task and read three fields, in this order:

- [ ] `state`: `succeeded`, `failed` (read `failure`), or `unknown`
  (see [step 5](#step-5-resolve-anything-left-uncertain)). Details:
  [Tasks, and why `unknown` exists](#tasks-and-why-unknown-exists).
- [ ] `persistence.state`: must be `saved` before you use the payload
  ([Persistence](#persistence-was-the-result-saved)).
- [ ] `result.payload`: the task's own answer. For conformance, also read
  `core_report.ok` and both cleanup facts
  ([Check and conformance results](#check-and-conformance-results)).

```python
task = await owner.wait(task_ref)
print(task.state, task.persistence.state, task.failure)
payload = task.result.payload if task.result else None
```

CLI: `lab job status TASK --generation GENERATION`, or add `--wait` to the
command that started the task.

**Done when:** `state` is `succeeded`, `persistence.state` is `saved`, and
you have read the payload — or you know why not.

### Step 2. Check that the operation finished cleanly

**Do:** read the operation's status (CLI: `lab status OPERATION`):

```python
status = await owner.status(operation_ref)
print(status.control_state, status.results_state, status.process.exit_code,
      status.target_cleanup.state, status.result.persistence.state)
```

On the offline campaign example this printed `finished present 0 confirmed
saved`.

- [ ] `control_state` is `finished`. `reserved`, `running` or `stopping`: it
  is still going, keep polling. `cleanup_required` or `orphaned`: go to
  [step 5](#step-5-resolve-anything-left-uncertain).
- [ ] `results_state` is `present`.
- [ ] `target_cleanup.state` is `confirmed`.
- [ ] `result.persistence.state` is `saved`.

An exit code of `0` says nothing about quality; the next step does. For a
**campaign**, an exit code of `1` with `control_state: "finished"` can also
mean the campaign is **suspended**, not failed: check `result.core.suspended`
before reading it as a failure. Details: [Operations: control state and
results state](#operations-control-state-and-results-state) and [Suspended
campaigns](#suspended-campaigns).

**Done when:** all four boxes are ticked.

### Step 3. Read the report and its coverage

**Do:** fetch the report (CLI: `lab report OPERATION`) and check that
`report.state` is `available`. Then read it together with coverage:

- **Evaluation.** `report.report["core"]["samples"]` lists every sample with its
  `execution.outcome` and `metrics`; `report.report["measurements"]` summarizes
  scored-sample metrics and coverage. Read `status.result.coverage`
  (`{expected, scored}`), `status.result.stopped_reason`, `status.result.unmeasured`
  and `status.result.min_coverage` alongside it. The report's `lab_result` carries
  the same evaluation facts and persistence. Details:
  [Evaluation reports](#evaluation-reports).
- **Campaign.** `report.report["core"]` holds `campaign_outcome`, the `rounds`
  and the `final_champion`; each entry of `report.report["measurements"]` has
  its own `coverage` (`expected`, `scored`, `state`) and `metrics`. In a
  campaign's status, `result.coverage` is `null`; coverage lives in the
  measurements. Details: [Campaign reports](#campaign-reports).

**Save the report as a file.** The same report is available as one Markdown file, `report.md`: the text Lab builds for
every run (Core's outcome, the gate, Lab's notices, rounds and attempts, the corpus, the final of family 14).

- Python: `(await owner.report_markdown(operation_ref)).markdown` when `state` is `available`; otherwise `reason` is
  `results_deleted`, `io_error` or `unavailable`, as for `owner.report`.
- CLI: `lab report OPERATION --markdown` prints it; `--output /home/you/out/report.md` writes a new file (mode `0600`)
  and refuses to overwrite an existing one.
- Panel: **Download report (.md)** on a campaign or evaluation screen, while the operation's results are present.

It is a snapshot at the moment you ask: a running campaign gives the report of the journal read so far. The text is
technical (Core's codes, in Russian); the Lab operation id is only in the file name. The money line says the cost is
unknown: Owner does not pass accounting data to the report.

- [ ] Scored fewer samples than expected? Look for `indeterminate` and
  `exclude` samples: those are infrastructure problems, not agent quality
  ([how samples are sorted](#evaluation-reports)).
- [ ] `campaign_outcome` is `null`? The outcome is *unknown*, not a failure.
- [ ] Final Champion unchanged? A legitimate result: nothing beat the seed
  under your conditions.

**Done when:** you can state the score (or the campaign outcome) *together
with* its coverage, for example "6 of 6 samples scored, mean 1.0".

### Step 4. Get a campaign's changes

A fixed evaluation has no Champion, so this step is for campaigns only.

**Do:**

1. Read the diff: `await owner.diff(operation_ref)` (CLI: `lab diff
   OPERATION`). `state` is `changed` or `no_change`; `text` is a unified diff
   and `nontext_changes` lists binary and permission changes.
2. Start the export once, read its task status, and save a ready ZIP to a
   new file. A `no_change` payload creates no ZIP:

```python
export = await owner.wait(await owner.export(operation_ref))
if (export.state != "succeeded" or export.persistence.state != "saved"
        or export.result is None):
    raise RuntimeError("Export unavailable; inspect task failure and persistence")
payload = export.result.payload
if payload["state"] == "ready":
    data = await owner.read_export(operation_ref, payload["export_id"])
    with open("champion.zip", "xb") as stream:  # new file in your current directory
        stream.write(data)
```

CLI: first `lab export OPERATION --wait`, then use the ready payload's
`export_id` in `lab export-content OPERATION EXPORT_ID --output /abs/new-file.zip`
(checks the SHA-256, refuses to overwrite).

3. Open `manifest.json` in the ZIP and check `campaign_outcome`,
   `campaign_complete` and `measurement_complete` before you apply anything.
   Lab never applies the change for you.

Details and limits: [Diff](#diff-from-seed-to-champion) and
[Export](#export-the-champion-as-a-zip).

**Done when:** you have the diff and, for a changed Champion, the ZIP file,
and you have read its completeness flags.

### Step 5. Resolve anything left uncertain

Use this step when a task is `unknown`, or an operation or target is
`cleanup_required` or `orphaned`, or `owner.health()` (CLI: `lab health`)
lists `blockers`. While cleanup is required, `diff`, `export` and
`delete_results` refuse and the target cannot be reused.

**Do:**

- [ ] **A task is `unknown`.** Nobody knows how it ended; it is neither
  success nor failure. Look at the related target and operation status and
  decide yourself. An interrupted export or deletion: run `export` or
  `delete_results` again. Details: [Tasks](#tasks-and-why-unknown-exists).
- [ ] **An operation is still running.** Request Stop first
  (`await owner.stop(operation_ref)`, CLI `lab stop OPERATION`) and poll;
  otherwise cleanup answers `busy`.
- [ ] **Run cleanup:** `await owner.wait(await owner.cleanup(ref))`, CLI
  `lab cleanup KIND ID --wait` (`KIND` is `operation`, `target` or `task`).
  The target ends up stopped; start a new one for later work.
- [ ] **Only if cleanup cannot prove it,** check yourself that no leftover
  processes remain and the target's port is free, then record a manual
  release: `await owner.release_after_manual_cleanup(ref, reason="...")`,
  CLI `lab release-operation OPERATION --reason TEXT` (non-empty, at most 256
  characters, no secrets). This is your *attestation*, not proof: it kills
  nothing and releases only the linked reservations.

Details: [Cleanup, orphans and manual release](#cleanup-orphans-and-manual-release).

**Done when:** the operation is `finished` or `released`, and
`owner.health()` shows no `blockers`.

### Step 6. Delete results you no longer need

**Do:** make sure the operation is released (finished with confirmed
cleanup, cleaned up, or manually released), keep any copies you want (a ZIP,
the report), then delete:

```python
deleted = await owner.wait(await owner.delete_results(operation_ref))
print(deleted.state, deleted.result.payload)
```

CLI: `lab delete-results OPERATION --wait`. If the deletion fails or is
interrupted, run it again; repeating is safe.

**Done when:** the payload says `"state": "deleted"` and the status shows
`results_state: "deleted"`. Afterwards `report` is `unavailable`
(`results_deleted`) and `diff` and `export` refuse. Details:
[Deleting results](#deleting-results).

**Next:** [Pitfalls](pitfalls.md) — the traps that most often spoil a
result — and [CLI and local service](cli.md) for the same reads from the
command line.

---

### Export private campaign traces

After a campaign, export its per-case evidence from the project's saved inputs:

```sh
lab trace ~/my-agent-lab --out campaign-trace.jsonl --disclose-private
```

An optional operation ID after the directory selects another campaign from the same project state. Without it, Lab selects the latest campaign. The service form is `lab trace OPERATION --out FILE --disclose-private`; its output path is on the service host.

The file includes case oracles and scorer diagnostics, so `--disclose-private` is required. Choose a new output file outside the Core run directory; Core creates it owner-only (`0600`). Lab requires a file path for `--out`. Fixed evaluations use `lab sample` and `lab measurements` instead.

The bytes are Core's `evolution-v2-trace/1` export, including authentication labels and field limits. Repeat `--measurement ID` to select measurements, and use a positive `--max-field-bytes N` to set the field cap. The DIR form resolves declared target and role values from the saved Bundle and caller environment, as continuation does. `--secrets-file FILE` or `--secrets-fd N` overrides those values; missing required names refuse before output is created. The service form receives its explicit secret map. The summary preserves Core's unset-variable count. Current editable project files do not replace the saved manifest during this read.

Keep this file outside the Candidate files you let the editor change.

## Reference material

The examples below show real output from an offline run of
`examples/owner/evaluation.py`, captured on `evolution-lab 0.0.3.dev32` with
`evo-skills 0.3.0a21`. These are historical producing-pair examples, not
captures of the current dev61/b13 pair; measurement rows gained `repairs` in dev36.
Long digests are shortened with `…`.

### The rule: separate facts are read separately

Lab records several independent facts about every piece of work. None of
them stands in for another:

| Fact | Question it answers | Where you see it |
|---|---|---|
| Process | Did the worker process start and exit, and with what code? | `process` |
| Core result | What did the Evolution Core measure or decide? | `result.core`, `report` |
| Cleanup | Is it confirmed that no processes or target sessions are left behind? | `target_cleanup`, `cleanup` |
| Persistence | Did Lab manage to save the result? | `persistence` |
| Coverage | How many scheduled samples were actually scored? | `result.coverage` |

Why so strict? Each shortcut hides a real failure. An exit code of `0` says
nothing about quality; a score says nothing about a stray agent process
still holding a port; a successful task may still have failed to save its
result. A zero exit code or an HTTP 200 is never a claim about quality,
cost or cleanup.

### Check and conformance results

These are the two optional readiness tests
([Check and conformance](concepts.md#check-and-conformance-two-readiness-tests)).
You always start them yourself. Both return a *task* that you read like any
other task (next section).

- **Check** tests the connection to a started target: health, capabilities,
  identity and compatibility. While it runs it holds the target. A passing
  check is not conformance, not proof that your seed is correct, and not
  permission for a paid run.
- **Conformance** runs Core's public protocol test suite against your
  target. Its result payload holds four separate facts:

```json
{"process":        {"launched": true, "exit_code": 0, "cleanup_complete": true, "forced": false},
 "cleanup":        {"state": "confirmed", "complete": true},
 "target_cleanup": {"state": "confirmed", "complete": true},
 "core_report":    {"ok": true, "version": "conformance/v2", "leaked": [], "checks": [
     {"suite": "G", "name": "health", "ok": true, "got": "ok", "expected": "…"}, "…16 more…"]}}
```

A `core_report` with `"ok": false` means conformance *ran to completion and
found problems*. It is not a process failure. Read the failing entries in
`checks`. The full report is private: it can contain details of your agent.

After conformance, Lab always stops the target. Before you start a real run,
make sure both `cleanup` and `target_cleanup` are `confirmed` and the target
state is `stopped`, then start a **new** target and use its new reference.

### Tasks, and why `unknown` exists

Short actions (target start and stop, check, conformance, cleanup, export,
delete) run as *tasks*. A task has a `state`:

| State | Meaning |
|---|---|
| `pending`, `running` | Still in progress. |
| `succeeded` | The action finished. Still check `persistence` and the result payload. |
| `failed` | The action ended with a `failure` (`code`, `stage`, `message`). |
| `unknown` | Nobody knows how it ended. |

`unknown` appears when an Owner opens a state directory and finds tasks
from an older Owner *generation* (an earlier run of the Owner or service)
that were still pending or running. The process that was watching them is
gone, so Lab cannot know whether the action happened. It refuses to guess:
`unknown` is **never** success and **never** failure. Look at the related
resources (target status, operation status) and decide yourself. Lab does
not resume or repeat such work. Two recoveries are built in: repeat an
interrupted export with an explicit new `export`, and repeat an interrupted
deletion with an explicit new `delete_results`.

Keep every task and target ID together with the `owner_generation` that
produced it; asking with another generation gives `not_found`.

### Persistence: was the result saved?

Every task has a `persistence` object, separate from its `state`:

- `not_attempted` — no result was recorded (for example, the task never
  reached a terminal state).
- `saved` — Lab recorded the result.
- `failed` — the result could not be recorded, even if the action itself
  ended. `persistence.failure` says why.

Check `persistence.state == "saved"` before you use `result.payload`. (The
Core's campaign outcome `persistence_failed` is a different fact: Core
could not write its own journal. It is an infrastructure outcome, not a
score.)

### Operations: control state and results state

An evaluation or campaign is an *operation*. Its status has two state
fields that answer different questions.

**`control_state`** — where the operation is in its life:
`reserved`, `running`, `stopping`, `finished`, `orphaned`,
`cleanup_required`, `released`.

**`results_state`** — what happened to its saved results:
`present`, `not_created` (no journal was ever created), `unavailable`
(a journal exists but cannot be read), `deleting`, `delete_failed`,
`deleted`.

Status is `lab-operation-status/7` (illustrative, trimmed; values are not from a real run): the top-level fields
below are the campaign or evaluation's own facts (`result.core` is Core's
projection over the *whole* run), and `starts` lists every start in order —
one element for an evaluation, one or more for a campaign that was
`resume`d. The top-level `owner_generation`, `target_ref`, `process` and
`target_cleanup`, `refusal` and `core_terminal` mirror the *last* start, so code written for `/1` keeps
reading them the same way:

```json
{"schema": "lab-operation-status/7", "id": "d0b514d5…", "kind": "evaluation",
 "entrypoint": "inputs/project/lab/manifest.json",
 "progress": {"kind": "evaluation", "coverage": {"expected": 4, "scored": 4}, "observed_at": "…"},
 "progress_state": "present",
 "times": {"first_event_at": "…", "last_event_at": "…"}, "times_state": "present",
 "control_state": "finished", "results_state": "present", "failure": null,
 "refusal": null, "core_terminal": null,
 "owner_generation": "3e2e5cde…",
 "process": {"launched": true, "exit_code": 0, "cleanup_complete": true, "forced": false},
 "target_cleanup": {"state": "confirmed", "complete": true},
 "result": {"schema": "lab-operation-result/2", "core": {"…": "see Evaluation reports"}, "coverage": {"expected": 4, "scored": 4},
            "stopped_reason": null, "unmeasured": [], "min_coverage": 0.95,
            "persistence": {"state": "saved", "failure": null}},
 "starts": [{"start": 1, "scope_id": "…", "owner_generation": "3e2e5cde…",
             "target_ref": {"id": "…", "owner_generation": "3e2e5cde…"},
             "process": {"launched": true, "exit_code": 0, "cleanup_complete": true, "forced": false},
             "target_cleanup": {"state": "confirmed", "complete": true},
             "failure": null, "settlement": "positive", "refusal": null, "core_terminal": null}],
 "resume": null}
```

For a campaign, `settlement` is `"positive"`, `"not_positive"`, or
`"unknown"` (no settlement ran for that start — it is still running, or it
failed before dispatch). `resume` says whether a suspended campaign can be
continued (`null` here: an evaluation);
[details](../../lab/docs/api/service-s5.md#resume-readiness). `lab list`
(`lab-operation-list/7`) carries the same `/7` statuses.

If Core refuses the campaign, `failure` is `core_refused` and `refusal` is
`{code, path, class, instance_digest}` (`class`: `hard` or `authorizable`) —
at the top level (a copy of the latest start) and in the refused `starts[]`
entry; a start opened by an accepted `resume` has `refusal: null`. The
operation then goes through the usual cleanup, which keeps `failure` and
`refusal`. The class does not say whether the campaign can continue; `resume`
does. `lab status` prints `refused: <code>[ at <path>] (<class>)`.

`progress` and `times` are fields of the status itself, not of `result`. Each is
`null` unless its `*_state` is `present`: `not_started` means there is nothing to read
yet (a campaign's journal is not created; this is why `result` of a just-started
campaign is empty) and `unavailable` means the read failed. For a campaign,
`progress.phase` is Lab's reading of what Core is doing now (`baseline`,
`reflection`, `generation`, `evaluation`, `selection`, `finishing`), or `suspended`,
`finished`, `waiting`, `unknown`; `rounds` and `attempts` count the whole
campaign, not one start.

### Cleanup, orphans and manual release

Lab reuses a target, or lets you delete results, only after cleanup is
*proven*: the worker returned normally, a helper receipt confirms the
process tree is empty, the target was not restarted underneath, and the
reserved resources were released.

When that proof is missing, the operation or target moves to
`cleanup_required`. Typical causes: a timeout, a lost response to a call that
changes something, a forced stop, a missing or invalid receipt, a target that
restarted, a failed delete, a persistence failure, or an Owner that died.
Resources left by an older Owner generation become `orphaned` or
`cleanup_required` in the same way. `health` lists anything still in the way
under `blockers`.

While cleanup is required, `diff`, `export` and `delete_results` refuse
with `cleanup_required`, and the target cannot be reused. You have two ways
out:

1. **Cleanup** (`owner.cleanup(ref)`, CLI `lab cleanup KIND ID --wait`) — a
   task that cleans the operation and its target. If the operation is still
   running, request Stop first, otherwise cleanup answers `busy`. The target
   ends up stopped; start a new one for later work.
2. **Manual release** (`owner.release_after_manual_cleanup(ref, reason=...)`,
   CLI `lab release-operation OPERATION --reason TEXT`) — a statement by a human that
   they checked the leftover processes and the target's port and found them
   clear. The reason must be non-empty, at most 256 characters, and free of
   secrets.

Manual release is an **attestation, not proof**. It releases only the linked
reservations. It kills no process, creates no cleanup receipt, does not make
a busy port safe, and does not turn an unknown campaign outcome into a
success. The disposition is recorded as `manual` with your reason.

### Evaluation reports

`owner.report(ref)` (CLI `lab report OPERATION`) returns a
`lab-owner-report/9` object with `operation_id`, `state` (`available` or
`unavailable`), `report`, a `reason` when unavailable (`unavailable`,
`invalid`, `io_error`, `unknown` or `results_deleted`), and `starts`, the
operation's start history (one entry per start, kept after
`delete-results`). The report is independent of the process status, and it
is private.

For an evaluation, `report` wraps Core's measurement projection with Lab's
evaluation facts:

| Field | Meaning |
|---|---|
| `core` | The public Core measurement projection, with per-sample outcomes and scores. |
| `lab_result` | `coverage`, `stopped_reason`, `unmeasured`, the applied `min_coverage`, and Lab persistence. |
| `measurements` | Coverage and scored-sample metrics for the measurement; partial coverage accepted at the threshold has `coverage.state: "accepted"`. |

The historical dev32/a21 Core projection below is unchanged. In the current
wrapper, read this projection under `report["core"]`:

```json
{"kind": "result", "complete": true, "journal_integrity": "complete",
 "candidate_digest": "fbd7fa5c…", "run_id": "c-d0b514d5…",
 "samples": [
   {"case_id": "q2", "sample_id": "case-1-repeat-1",
    "execution": {"kind": "score", "outcome": "completed", "owner": null, "reason": null},
    "execution_state": "finished", "scoring_state": "finished",
    "metrics": {"score": 0.0}, "critical_pass": true},
   "…3 more samples…"],
 "diagnostics": [], "final_champion": null, "initial_champion": null}
```

Core projection lists samples one by one; `measurements` summarizes metrics
over scored samples. Read them together with `result.coverage`,
`result.stopped_reason`, `result.unmeasured` and `result.min_coverage` from the
operation status:

- **Coverage** `{expected, scored}` compares the confirmed schedule (cases ×
  repeat seeds) with samples that were actually scored. `4 / 4` above means
  every scheduled sample got a score.
- **`unmeasured`** lists samples left without a score, with their case/sample
  IDs, outcome and number of tries. Outcomes such as `target_failed`,
  `time_budget` or `scorer_failed` are retried according to Core's
  `sample_retries`; after the last attempt, evaluation continues. Exhausted
  target-call retries are also a sample outcome (see [Evaluation
  budget](evaluation-inputs.md#the-fixed-evaluation-budget)). They do not alone
  set `stopped_reason`.
- **`stopped_reason`** records a global stop: cancellation, `budget_exhausted`,
  a Core `exclude` outcome (`cancelled` or `infrastructure_failed`), a Core
  failure without a closed try (`core_failed`), or the configured consecutive
  unmeasured limit (`consecutive_unmeasured`, default 3; `"off"` disables that stop). It remains `null` when evaluation
  continues through individual sample failures and finishes without a stop.
  Missing stop facts give `"unknown"`.
- **`min_coverage`** is the acceptance threshold (default 0.95; prepared
  evaluations from before dev60 retain 1.0). A baseline is accepted only when
  the evaluation is finished or released without a stop, Lab persistence is
  saved, expected is positive and `scored / expected` meets the threshold. The report cross-checks this coverage
  against Core's projection: accepted partial coverage has state `accepted`,
  full coverage is `complete`, and partial coverage that is not accepted is
  `incomplete`. Coverage below the threshold or a stop cannot qualify a baseline. Standalone
  reports lack Owner persistence and do not establish an Owner baseline fact.
- Missing things stay missing. Before the run identity is confirmed,
  coverage is `null`. None of these becomes a zero score.

**Infrastructure errors never count as agent quality.** Core sorts
every sample by who is responsible (`execution.owner`) into one of three
kinds:

| Kind | Sample outcomes | Effect |
|---|---|---|
| `score` | `completed`, `turn_budget` (agent ran out of turns), `candidate_failed` (agent's own error) | Scored; counts toward the agent's metric. |
| `indeterminate` | `time_budget`, `target_failed`, `environment_failed`, `scorer_failed`, `interrupted` | Not scored; it is unknown how the agent would have done. |
| `exclude` | `cancelled`, `infrastructure_failed` | Removed from the measurement. |

So a crashed scorer or a dead target lowers *coverage*, not the agent's
score. Always read the score together with coverage.

<a id="scorer-content-on-exact-dev55b8"></a>
### Scorer content on exact dev61/b13

Lab uses Core's public `ScorerTraceReader` for directory and pack traces,
addressed by the selected measurement and scoring request. Completed journal
records still gate disclosure and determine scoring state; identity/hash checks,
`lab-sample/1`, campaign `/11` with families 14 and 15, private disclosure and key
masking retain the current contract. Missing records are `trace_missing`; unknown
request sizes are
`trace_missing` for packs and conservatively `unreadable` for directories.
Verified input/dialogue survives missing stdout with `trace_missing` (pack) or
`unreadable` (directory), even for failed scoring. Empty failed stdout remains
available; admitted scoring still requires valid JSON. A failed payload read
preserves no unverified input. Existing private failed-scorer `exit_status` can
carry Core's returncode; directory traces give null. No stderr is read.

One Core `ScorerTraceReader(root, run_boundary="nofollow")` is opened per
request. Metadata (`sections=()`) and payload use that same reader; payload reads
pass `max_section_bytes=33554432` (32 MiB per selected section). Request/stdout
metadata and returned bytes are also checked before parsing/disclosure. Oversize
rejects the whole trace as `too_large`, without partial input. Typed `too_large`
maps to that code; other typed read failures map to `unreadable`.

Strict no-follow traversal and 32 MiB payload limits apply per selected section; they do not cap the aggregate pack index or process RSS and do not make files immutable to their owner.

### Campaign reports

For a campaign, `report` is Lab's campaign report built from Core's
public projections. Its top-level fields:

| Field | Meaning |
|---|---|
| `operation` | `"campaign"`. |
| `core` | Core's campaign projection: `campaign_outcome`, `initial_champion`, `final_champion`, the `rounds` with their attempts, and any `rebases`. |
| `measurements` | One entry per readable measurement: `coverage` (`expected`, `scored`, `state`: `complete`/`incomplete`/`unknown`), `metrics` (mean and count over scored samples), `samples`, `tries`, `excluded_pairs`, `journal_integrity`. |
| `unavailable_measurements` | Measurements the campaign declared but Lab could not read. |
| `campaign_journal_integrity` | Whether the campaign journal was read completely. The outcome refers to the part that was read. |
| `lab_result`, `lab_starts` | Process facts from a legacy-runner or H1 `-lab-logs` directory; an Owner run has none, so these stay `null`/empty. |
| `lab_rounds` | Per Core round `{round, reflection: {state, error_class, reason}, source: "core_raw"}` (codes only, no hypotheses or texts); `null` when the round details were not read. |
| `lab_rounds_state`, `lab_rounds_reason` | Whether the round details were read: `present` with reason `null`, or `unavailable` with the code of the failed read (for example `unreadable`). `report.md` then says `Подробности рефлексии недоступны (<reason>)` next to the rounds without attempts or the notice. |
| `lab_notices` | Lab notices, for example `no_candidate_measured` with the rounds by reason and the reflection error classes. The campaign outcome stays Core's word. |
| `distributions`, `producing_version` | Which versions produced the run. |
| `cost` | `null` in Owner reports (see below). |

A `/15` campaign (Core family14) carries Core's gate and confidence facts in
`core` and in `Report.markdown()` (the existing report is in Russian):

- The gate line names `observe` or `statistical` and family14. Observe carries
  `observe_gate_not_enforced` and `uncalibrated_point_estimates`; its result is
  not evidence of quality. A finalist still needs Core's final contrast to be promoted.
- Caveats retain their campaign/round/attempt scope; corpus split counts come from Core.
- Attempts carry the latest evaluated look, Core's decision/reason and estimates;
  Lab computes no interval or statistical verdict.
- Settings, roles and the connection check appear as `Настройки`, `Роли` and
  `Проверка подключения`; each round has final measurement members, point values,
  exact contrasts, selection and unmeasured pairs.

Family14 `Report.markdown()` content is unchanged. The service `report.md` route
and panel download deliver this markdown as is, without a Lab header or translation.

**Checks on `/16`.** The view `/11` has `checks`, `checks_state` and
`checks_reason`; the nested report `/9` has `lab_checks`, `lab_checks_state`
and `lab_checks_reason`. An admitted projection has state `present`, null
reason, and either null checks (neither Core map exists) or
`{modes: {...}, shadow_hits: {...}}` (a missing map becomes `{}`).
Unreadable projections have null checks, state `unavailable`, reason `unreadable`.
These fields remain available when binding reads make `independent` unavailable.
Family 14 has no checks keys. CLI, Markdown and panel show Core's non-`enforce`
modes and individual shadow counts without recalculation or totals.
Counts from different checks are not comparable.

**A `/16` campaign (Core family 15).** The nested `report` in the current
`lab-owner-report/9` envelope carries `lab_independent`, Lab's projection of Core's
independent-acceptance block (introduced with Owner report `/5`), with
`lab_independent_state` and `lab_independent_reason` beside it, in the words of the
view's `details_state`/`details_reason`. All three are absent for a family-14 campaign. For family 15 the block comes with
state `present` and reason `null`; when this Lab cannot read the family-15 block it is `null` with state `unavailable`
and reason `unreadable`, which `report.md` and `lab report` say rather than guess. Its attempt groups are the view's: an
attempt the campaign's recorded end (Core's `complete`/`campaign_outcome`) left unresolved, such as a gate finalist of a
round a cancellation left open, is `unknown`, never "in progress". The report section and `lab report` show the settings line (R, mode, noise, verification) and, per attempt
and per scope, the statement for **that scope's own rule**:

| Scope state | Statement |
|---|---|
| `bound` | gain, "with confidence c not below L", and the threshold |
| `bootstrap` | gain, empirical one-sided repeat-bootstrap lower bound, and the threshold |
| `point` | gain and the threshold |
| `no_threshold` | gain, with "no threshold" stated |
| `status` | Core's recorded status or unavailable code, no numbers |

There is never a two-sided interval, and the enclosure's upper end is not a bound. Each comparison is against the
**round's Champion**, labelled "stored measurement", "fresh measurement" or "measurement not recorded"; family 15 has no
"Parent" reference. An attempt without a comparison shows its reason and Core's attainability facts. For a Candidate that passed Core's global comparison, the eligible final score is shown. Lab recomputes nothing and puts no scores from other measurements side
by side. Gate and global comparisons are shown as separate lines, each with its own scopes and
short `reference_measurement_id`; an absent level is omitted, and if neither exists the report says there is no
comparison and shows preflight facts. JSON attempts retain three nullable fields with one closed comparison shape:
`comparison`, `gate_comparison` and `global_comparison`. Core's `comparison` selects global when present, otherwise gate;
Lab copies each independently and never infers the nullable reference id. See the [closed API shape](../../lab/docs/api/owner-s4.md#campaign-view).
The view shows a one-sided lower bound. Promotion is automatic and L3 is unavailable. The 2026-10-19 amendments of PDR-0015 and PDR-0017 record the decision. See [Continue and link](campaign.md#continue-and-link) for starting a successor.

The round summary reports Attempts opened, Candidates sealed and Attempts with at least one recorded Candidate score. A bound measurement has assigned work. A started measurement has dispatched work. A scored measurement can still have pending slots. Check the Attempt outcome, the Candidate and fresh reference measurement states, and any campaign suspension to see where work stopped. The report uses public Reflection and Editor reason codes. A focus rejection under `focus_first` leaves the causal stage `not_measured`.

With `execution_noise.kind: observed`, each comparison reports the range of sample variances across complete sets of Candidate repeats, in score-squared units. If a case has incomplete evidence or fewer than two repeats, it contributes to the count for that reason. The empirical repeat-bootstrap lower bound comes from the contrast in a `statistical` comparison and has its own availability. `report.md` lists the primary and any package-defined scalar metrics from scored measurement rows, with the mean and count for each. Older readers can omit newer detail fields.

Every campaign `report.md` also lists the rounds that have no attempts (`Раунды без попыток`): round, outcome,
reason and the reflection error class. When no Candidate was measured at all, a line `Ни один кандидат не замерен`
follows the outcome: it is a Lab notice, the outcome itself is unchanged. `lab report` prints Core's outcome as
`outcome: <outcome>/<reason>` (`outcome: -` when Core has none) and after it the same notice as
`no candidate was measured: <reason> <count>`.

`owner.report(ref)`'s answer, `lab-owner-report/9`, carries the Owner's own
per-start history as a sibling of `report`: a top-level `starts` list, the
same shape as `status.starts` (one entry per start, in order, with
`settlement`, `refusal` and `core_terminal`). The top-level `core_terminal`
copies the last start, with null on success, suspension or refusal; earlier
start terminals remain in the list. That is the field to read for "which start did what" on an
Owner run; `lab_result`/`lab_starts` inside `report` stay tied to the
legacy/H1 on-disk log format.

The **Champion** is the version Core currently accepts. Core, not
Lab, decides promotion. `initial_champion` is where the campaign started
(your seed) and `final_champion` is where it ended.

`campaign_outcome` is Core's object with `outcome` (`completed`,
`stopped`, `budget_exhausted` or `infrastructure_failed`), a `reason`, and a
`continuation`. It is `null` when Core recorded no terminal outcome;
that means *unknown*, not failure. A **suspended** campaign is one such case
([Suspended campaigns](#suspended-campaigns) below). A campaign that ends
with the Champion unchanged is a legitimate, informative result: nothing the
Editor proposed beat the seed under your conditions.

**Tokens.** The `/3` report has a top-level `usage` list, one row per Core measurement:
`{measurement_id, usage, usage_state}`. `usage` counts **only the target's own calls**
(`coverage: "target_calls_only"`): Editor, scorer and Reflector tokens are not in it, and a
call the target did not report numbers for is counted in `unknown_calls`, not as zero.
`usage` is `null` with `usage_state: "unavailable"` when the journal could not be read.
Treat the totals as a lower bound of what the campaign spent.

**Costs.** Lab does not calculate cost, and missing cost evidence is never
treated as zero. The Owner's campaign report always has `cost: null`. If
you run the model calls through the [budget gateway](budget.md), you can
build a report with its accounting yourself (see that page).

<a id="suspended-campaigns"></a>
### Suspended campaigns

On dev61/b13, reflection `input_too_large` remains the journal's exact
`error_class` and `reason`; Core's internal `unknown` classification does not
replace those codes. The existing suspension is `reflector_failing`.
Retrying the same oversized Bundle does not fix it. Authorization or editing
sealed inputs is not a remedy; use a corrected package and a new campaign under
the existing continuation rules.

The campaign worker preserves Core's result codes separately from Attempt and
suspension reasons: `storage_inodes_insufficient` accompanies terminal
`storage_capacity_exhausted`, and `reflector_budget_unavailable` accompanies
`persistence_failed`, both with owner `artifact_store`. This does not add a new
panel/CLI fault screen or infer successful settlement from a normal library
return. Fixed evaluation uses the library measurement path, without copying
Core CLI's inode preflight; its existing generic `lab_error`, cleanup and
settlement behavior remain.

For an unexpected campaign or evaluation worker exception, human `lab status OP` adds the qualified exception class and, when available, a typed Core error code. Its `traceback:` line points to `scopes/<scope_id>/stderr.log` inside the service's state directory. The traceback redacts environment values; before redactions are ready, the log contains only the class name. Exception messages stay in that private log. `diagnostic.json` stores only the class and nullable code, and JSON status output stays unchanged. Older diagnostics may have no exception details.

An infrastructure failure inside a rollout no longer closes a campaign as
`infrastructure_failed`. Core suspends it instead: the run is not finished,
`campaign_outcome` stays `null`, and the run directory is kept.

- **Status.** `status.control_state` reaches `"finished"` and
  `status.process.exit_code` is `1` — read like an ordinary failure at a
  glance. Check `status.result.core.suspended` (`true`/`false`) before you
  conclude anything: `status.result.core.suspensions` then lists every
  suspension, each with `seq`, `reason`, `owner` and `evidence_kind`.
- **CLI `lab status`.** Two extra lines when suspended:

  ```text
  suspended: <reason> (owner=<owner>, evidence=<evidence_kind>)
  continuation: lab resume OPERATION --target <ready target of the same bundle> --generation <owner generation>
  ```

  The `continuation:` line changes with the operation's state: `lab cleanup
  operation OPERATION, then lab resume …` while it is
  `cleanup_required`/`orphaned`, or `not available after manual release; the
  run directory is kept until results are deleted` after a manual `lab
  release`. After a `target_unstable` suspension the line asks for a fresh
  target: `on a fresh target: stop target ID if it is still running (lab
  target stop ID --generation GENERATION --wait), start a new target of the same
  bundle (lab agent start <connection>), then lab resume OPERATION --target <new target> --generation <owner
  generation>` (`ID` is the operation's current `target_ref`, the latest start's target). It is present only when the operation can currently be told apart
  from one that will never continue (see [Cleanup, orphans and manual
  release](#cleanup-orphans-and-manual-release)).
- **Role suspensions.** A campaign on manifest `/11` (Core family 10) can
  also suspend because a role failed: `reflector_provider_failed`,
  `reflector_failing`, `editor_provider_failed` or `editor_failing`. After the
  `suspended:` line `lab status` prints a `hint:` line saying what to repair
  first, and the report shows the same hint in Russian after the suspension
  line. What each reason means:
  - `*_provider_failed`: the role's provider failed or refused after the
    role's retries. Repair the provider's availability, key and quota.
  - `editor_failing`: the Editor failed for a reason outside its edit (a
    timeout, a crash, a program or agent error, a failed preflight, a changed
    workspace). Repair the external cause: network, host resources, an agent
    CLI's login, or wait out a transient crash.
  - `reflector_failing` has two triggers: the Reflector failed outside its
    diagnosis in the same way (timeout, crash, agent error, preflight), or
    two error groups in a row were skipped, each after
    `reflector.max_consecutive_failures` content failures on that group.
    After an external or transient failure (the first trigger, or a skip
    caused by one), repair it and continue. Repeated content skips may have no
    external cause: continuing helps only if the failures were transient;
    otherwise the Reflector's program, prompt, limits or
    `max_consecutive_failures` must change, which needs a new Bundle and a new
    campaign (below).
  - A content failure of a role (an unparseable answer, turns used up, a failed
    self-check) is not a suspension. For the Reflector it closes the round
    `unplanned` and counts toward that group's skip; for the Editor it closes
    the Attempt (`abandoned` / `editor_failed`), and the campaign goes on.

  Only causes outside the Bundle are repairable before `lab resume`: the
  network, the provider, an agent CLI's login, host resources. The interpreter,
  installed tools and runtime files are part of the Bundle's captured execution
  environment; Lab re-captures it on every start and refuses a change with
  `environment_invalid`. So changing a role's program, prompt or limits
  (including `max_consecutive_failures`) or the execution environment needs a
  new Bundle and a new campaign; `lab resume` continues the same Bundle. These
  reasons put no requirement on the target: `resume` is `ready` with `target:
  null`, so the `continuation:` line is the ordinary `lab resume …` one.
- **Family14 (`/15`) reasons.** `final_incomplete` means the round's final
  measurement is unfinished (evidence `final_rows`): continue explicitly on a
  ready target of the same Bundle, and Core measures the missing pairs. It
  puts no fresh-target requirement on resume. `connection_incompatible`,
  `connection_preflight_unavailable` and `connection_preflight_inconclusive` come from Core's connection check; the
  details are codes and paths, never values. `grouper_provider_failed` and `grouper_failing` are role pauses of the
  Grouper: fix the provider or the host and continue; a changed Grouper program, prompt or limit needs a new Bundle.
  None needs a fresh target. `connection_incompatible` and `connection_preflight_inconclusive` have two exits: a cause
  outside the Bundle (the agent's provider, the target's environment) is fixed and the campaign continues; an error in the agent, scorer or `metric_contract` needs corrected prepared inputs. Use `lab campaign DIR --continue --reprepare` with the wheel arguments, or select an existing preparation with `--prepared ID`; Core decides whether a finding can be accepted or requires a linked successor. `connection_preflight_unavailable` has one exit: fix the target or the scorer and
  continue. `lab status` and `lab campaign` print the matching `hint:`.
- **`target_contract_violated`.** Since Core a29 a turn that exceeds the target's declared limits inside a
  campaign always suspends it with `target_contract_violated` (owner `target`, evidence `fault_rows`). It asks for no
  fresh target, so continuing on the same target suspends again: fix the target or its declared limits first.
- **Report.** `report.report["core"]["suspensions"]` (same rows as above),
  `report.report["core"]["indeterminate"]["window"]`, and, per measurement,
  `call_failures_by_code` and `call_retries`.

**Continuation.** Owner, service and CLI runs continue explicitly, on any
`ready` target of the same Bundle — even one started after an Owner/service
restart: `Owner.resume(ResourceRef("operation", operation_id), target_ref=...,
secrets=...)` (CLI `lab resume OPERATION --target … --generation …`). This
needs the operation to be `finished`, or `released` with confirmed target
cleanup; a `cleanup_required`/`orphaned` operation needs
[`cleanup`](#cleanup-orphans-and-manual-release) first, and a `released`
operation after a manual `release` has no continuation — only
[deletion](#step-6-delete-results-you-no-longer-need). After a
`target_unstable` suspension the target must also be a fresh one: the status's
`resume.target` is `{requirement: "fresh_instance", reason:
"target_unstable_fresh_required", excluded: [...]}`, listing every target the
campaign has used, and `resume` on any of them is refused with
`target_unstable_fresh_required` (stop the running target first, then start a
new one). Since Core 0.3.0a24 a threshold suspension gives each pair whose tries the instability's owner (target, environment, scorer or rollout) exhausted one repair
try on resume, visible as `repairs` on the [measurement](#measurements). After a role suspension (the four reasons above) `lab resume` needs
no particular target: any `ready` target of the same Bundle, the one the
campaign used included. Whether the target
really belongs to this campaign is Core's call at worker start, not Lab's;
a mismatch comes back as an ordinary worker refusal, not a Lab error. Full
admission rule and error codes: [Owner API
reference](../../lab/docs/api/owner-s4.md#resume). A legacy-runner run
(`<run>-lab-logs/supervisor.lock` exists) instead continues in the same run
folder with `runner.resume_native`, once the target works again, without this
admission check — see the [legacy runner reference](../../lab/docs/api/legacy-runner.md).
H1 runs have no continuation.

**Resume adds a start, not a new operation.** The operation, its `id` and its
run directory stay the same; each call to `resume` (or the original `start`)
becomes one more entry in `status.starts` and `report.starts` — see
[Operations: control state and results state](#operations-control-state-and-results-state)
and [Campaign reports](#campaign-reports) below.

<a id="campaign-view"></a>
### Campaign view: rounds, attempts and outcome groups

`lab campaign OPERATION` (`lab-campaign-view/11`, campaigns only; `/10` and earlier views are no longer served; an evaluation answers
`invalid_argument`) puts the campaign on one page: the policy (`rounds`,
`attempts_per_round`, `metric_primary`), `progress`, the campaign `outcome`, the chain of
`champions` (`v0` is the seed), each round with its baseline measurement and its attempts, the
measurement summaries and any `suspensions`. Since dev33 it also says which version each round
started from, what the Reflector concluded and where a stopped attempt stopped (below).

Every attempt has Core's own `outcome` and `reason` and, beside them, a `group`. The group
sorts the attempt reasons of exact b13 family14 into a few kinds so infrastructure trouble is
not mistaken for a weak agent:

| Group | Meaning | Reasons |
|---|---|---|
| `accepted` | The Candidate passed Core's final contrast; observe is not quality evidence. | `gate_passed`, `observe_finalist` (finalists follow final decision below) |
| `agent_worse` | The evidence says the Candidate was not better. | `critical_failure`, `causal_below_tolerance`; rejected final contrast; `focus_below_margin` as below |
| `not_enough_data` | The measurement could not decide. | `no_evaluable_pairs`, `no_evaluable_focus`, `insufficient_repeats`, `insufficient_calibration`; unavailable final; `focus_below_margin` as below |
| `infrastructure` | The measurement itself was incomplete or failed. | `incomplete_sample`, `scorer_metric_missing`, `infrastructure_failed` |
| `no_candidate` | The Editor produced nothing to test. | `editor_declined`, `editor_no_change`, `no_exam_impact` |
| `stopped` | The attempt was cut short. | `editor_failed`, `editor_unresponsive`, `changed_file_budget`, `written_byte_budget`, `seal_refused`, `interrupted`, `cancelled`, `condition_changed` |
| `in_progress` | The attempt is not closed yet. | — |
| `unknown` | A reason this Lab does not know (a newer Core). | — |

The group never replaces Core's decision: read `outcome`/`reason` for what Core decided.
Passing a contrast does not by itself select the Candidate as Champion; the round selects separately.

**Family 14 (`/15`).** A finalist is not a promoted attempt: Core's outcome `finalist` means the attempt passed the gate,
and the decision is taken by the round's final measurement. Lab takes the group from Core's own contrast for that
attempt: a passed contrast is `accepted`; a rejected one (Core's reason, for example `global_point_not_improved`)
is `agent_worse`; a final not yet measured while the campaign runs is `in_progress`; a final that did not happen
(`final_incomplete`, or the campaign ended without a contrast) is `not_enough_data`. A finalist with no contrast
in the projection is `pending` (round open, campaign running) or `unavailable` (closed round, `final_incomplete`
pause, or finished campaign); `unknown` is only when Lab could not read the decision. `focus_below_margin` is `agent_worse` on an early look (the upper bound does not
exceed the margin) and `not_enough_data` on the final look when the point is above the margin (positive but not
confirmed); the look and the point come from the attempt's `confidence`, and without them the group is `unknown`.

**When no Candidate was measured (`/5` and later).** A campaign can end "completed" without any Candidate ever being
measured — for example when the Reflector failed in every round, so the Editor never got a brief. Core's outcome
stays exactly what Core said; next to it the view carries `lab_notices` with the code `no_candidate_measured`. A
Candidate counts as measured when its attempt's group is `accepted`, `agent_worse` or `not_enough_data`; the notice
appears only when Core has an outcome, no attempt was measured and none is `in_progress` or `unknown`. It counts why:
`by_reason` (each attempt's Core reason, or the round's own reason when a round had no attempts) and, when the journal
was read, `reflection_errors` (the error class of each failed reflection). `lab campaign` always prints Core's outcome first
(`outcome: <outcome>/<reason>`, or `outcome: -` while Core has none), then the notice, before the rounds; a failed
reflection shows its error class and reason:

```text
outcome: completed/rounds_exhausted
no candidate was measured: reflection_failed 2; reflection errors: oracle_leak 1, reported 1
round 1 unplanned baseline=v0 reflection=failed(oracle_leak: oracle_leak)
round 2 unplanned baseline=v0 reflection=failed(reported: invalid_result)
```

The notice holds codes and counts only; see the [Owner API reference](../../lab/docs/api/owner-s4.md#campaign-view) for the shape.

**Current `/11` contract.** `/11` carries `family: 14 | 15`. Family 14 keeps the `/7` content: compared with `/6`, `/7`
removes `rounds[].cohort` and `rounds[].attempts[].alpha_total` completely,
even null keys. Other family14 fields stay: gate/caveats, roles, settings,
connection, policy/progress/outcome, champions, measurements, suspension,
details, notices, baseline/reflection, final, attempt look/confidence,
final decision, measurement/tree mapping and failure. Family 15 replaces the family-14 confidence, cohort and final
blocks with an `independent` block (settings, attempts with per-scope comparisons, attainability facts, work, evidence);
reading it is described under [Campaign reports](#campaign-reports).
`confidence.margin` and latest focus/causal `exact` remain. Core owns all
contrasts and decisions. `lab campaign` prints `settings:`, `connection:`,
`final=`, `exact=`, `margin=` and `decision=` ([CLI](cli.md)).
The panel shows every recorded final entry in Core order, settings, roles,
connection and baseline refreshes, plus confidence `margin` and estimate `exact`.
Final entries include members, contrasts, selection, `listed` and next baseline.
Finalist, final decision and Champion transition are separate facts: only present
`champions[]` lineage with `origin: promoted_attempt` and the matching attempt ID
confirms promotion. A recorded transition does not assert the current Champion.

`Owner.suspensions`, GET `/v1/operations/{id}/suspensions` and
`lab suspensions OPERATION [--json]` share the closed `lab-suspension-evidence/1`
read. Its identifiers and codes explain recorded pauses, not all remaining work;
counts, digests and truncation remain explicit. The panel reads current evidence
on the suspended screen and loads history after resume when opened. New sequence
values invalidate old answers; failures mark retained evidence stale, and result
deletion clears it. Unavailable or stale history neither proves there were no
pauses nor confirms a current cause. See [Panel](panel.md) for the display details.
The exact shapes are in the [Owner API reference](../../lab/docs/api/owner-s4.md#campaign-view).

**Unavailable reads and old runs.** dev61 / exact b13 reads family 14 and family 15.
A confirmed foreign family is a typed `CampaignFamilyUnsupported`; empty/torn
creation without validated identity is `CampaignUnreadable` before family
classification. A wrong exact pin is separately `core_raw_unsupported`.
Owner status keeps process/cleanup/persistence facts while fresh results are
unavailable; report has its existing unavailable form and campaign/content
reads use `not_found` without usable Core. A saved family14 result may survive a
temporary reread failure, including after restart, but never a confirmed foreign
family and never as evidence of fresh readability. H1 wrong-pin status is
`unknown` / `lab_error`, with a null projection; report reason is `unknown`.
Pre-creation `not_created` and deleted `results_deleted` remain distinct.

Owner schema v8 adds only nullable `core_terminal_json TEXT` columns in
`operations` and `operation_starts`. Earlier Owner schemas are refused without
migration. Use fresh state for this pair; there is no runtime wrong-pair gate
for another producing pair's v8 state.
Use new state `~/lab-state/dev61-b13`. Owner SQLite schema8 refuses earlier Owner schemas
with `unsupported_schema`; a matching schema does not make another pair's state reusable.
No migration or folder copying: read, resume and clean up old runs with their
producing wheels and state. Local unreadable family14 details remain null with
availability/reason; readable observe caveats survive another block's failure,
and unknown gate never becomes statistical.

**Where an attempt stopped.** An Attempt with outcome `abandoned` or `insufficient_evidence` carries a
`failure` with a `stage`: `editor` (the Editor failed, did not answer, declined or changed nothing),
`seal` (the Candidate could not be sealed), `measurement` (the evaluation was incomplete or found
nothing to compare) or `lifecycle` (interrupted, cancelled or the conditions changed). For an Editor
failure it also gives the Editor's `error_class` and `role_reason`. `lab campaign` prints it after the
Core's reason, for example `r01-a02 stopped editor_failed editor:turns_exhausted`. A failure holds codes
only; the text of an error message is not available. Other Attempt outcomes have no `failure`.

**Reflector hypotheses.** Each round has a `reflection` with a `state` (`present`, `declined`, `failed`,
`unsettled`, `in_progress` or `absent`) and, when `present`, the `hypotheses` the Reflector recorded. They
belong to the round, not to one attempt: every attempt of the round saw all of them. A hypothesis's
`index` is its position in Core's journal, not the `hK` number in the Editor's brief, so do not match
them by number. The text is shown as Core journaled it. Exact pinned family14 Core
owns disclosure and refusal checks; a hypothesis **may** still contain fragments
or paraphrases of private data. Treat this output as private.

**Diff against the round's baseline.** A round's `baseline_version` names the Champion its attempts were
compared with. To see what an attempt changed relative to it:

```sh
lab diff OPERATION --from champion:<baseline_version> --to attempt:<attempt_id>
```

An attempt without a saved tree answers `tree_unavailable`. When the journal cannot be read,
`details_state` is `unavailable` (with `details_reason`) and these fields are `null`; the rest of the view
is unchanged, and `lab campaign` prints `details unavailable: <reason>`.

<a id="measurements"></a>
### Measurements

`lab measurements OPERATION` lists the measurements of a campaign or an evaluation
(`lab-measurement-list/2`); `lab measurements OPERATION MEASUREMENT` shows one with its
per-sample rows (`lab-measurement/2`). To inspect a sample’s input, dialogue and verdict, an attempt’s
Editor/failure and look history, or a round’s Reflector events, use
[`lab sample`, `lab attempt`, `lab reflection`](cli.md#content-reads). Private content requires explicit
disclosure; unavailable sections and truncation remain visible. A measurement’s `role` is `baseline`
(the Champion at the
start of a round), `attempt` (a Candidate), `evaluation` (a fixed evaluation) or `other` (one no
round refers to). Read it with its coverage: `coverage.scored` of `coverage.expected`, and
`excluded_pairs` for pairs left out.

`repairs` (`{tries, pairs_scored}`, for example `{tries: 3, pairs_scored: 1}`) says how many repair tries the
measurement started over all its continuations (`tries`) and for how many distinct `(case, sample)` pairs the
last repair try was scored (`pairs_scored`). It is informational: it changes neither the statistics nor the
gate. It is `null` for a fixed evaluation and when Core does not report it.

For an attempt, `baseline` compares it with the round's baseline over the pairs both sides scored:
`delta` (mean difference of each metric, over `pairs`) and `cases` (`better`, `same`, `worse`,
`unpaired`). This is a view for you; it is not the gate's decision. Where Core listed too many
excluded pairs to trust (`baseline.state: "exclusions_truncated"`), the delta and cases are `null`
and samples read `unknown` rather than guessed. `usage` is the target-calls-only token count
described above. Measurement ids are Core's (`m-r00-champion`, `m-r01-a01`); an evaluation's is
`c-<operation id>`. After `delete-results` these reads answer `results_deleted`.

<a id="diff-from-seed-to-champion"></a>

### Diff: from seed to Champion

For a finished campaign, `owner.diff(ref)` (CLI `lab diff OPERATION`) is a
direct read that compares your saved seed with the final Champion chosen by
Core. The shape looks like this (the paths are illustrative):

```json
{"operation_id": "…", "source_digest": "…", "selected_digest": "…",
 "state": "changed", "text": "--- a/prompt.md\n+++ b/prompt.md\n…",
 "nontext_changes": [{"path": "tools/run.sh", "kind": "mode"}]}
```

`text` is a unified diff of text files; binary and permission changes are
listed in `nontext_changes`. `state` is `changed` or `no_change`.
`no_change` means Lab verified that the Champion is identical to the seed.
It is a real answer, not an error. If the Champion evidence is missing or
unreliable, you get the error `export_unavailable` instead, never a false
`no_change`.

`lab diff OPERATION --from REF --to REF` compares any two saved trees of the campaign
(`lab-operation-diff/2`, which adds `from` and `to` to the shape above). A `REF` is `initial`,
`champion:vN` or `attempt:<attempt id>`; without flags it is `initial` to the final Champion. An
attempt that has no saved tree (for example a cancelled one) answers `tree_unavailable`, a
malformed `REF` answers `invalid_argument`.

A fixed evaluation has no Champion, so `diff` and `export` on it return
`export_unavailable`. Verified on the example evaluation:

```text
$ lab --socket /run/lab/lab.sock --json diff d0b514d5…
{"error":{"code":"export_unavailable","stage":"diff","message":"The requested export is unavailable."}}
```

<a id="export-the-champion-as-a-zip"></a>

### Export: the Champion as a ZIP

`owner.export(ref)` (CLI `lab export OPERATION --wait`) starts a task. On
success its payload is one of:

- `{"state": "ready", "export_id": "<sha256>", "sha256": "<same>", "manifest": {…}}`
- `{"state": "no_change", "export_id": null, "sha256": null, "manifest": null}` —
  no ZIP is created.

Read a ready archive with `owner.read_export(ref, export_id)`, which returns
the whole ZIP as `bytes`, or with the CLI `lab export-content OPERATION
EXPORT_ID --output /abs/new-file.zip`. The CLI checks the SHA-256 and
refuses to overwrite an existing file.

The archive (format `lab-change-set/1`) contains:

- `manifest.json` — the operation, prepared and Bundle identities, the
  producing versions with wheel hashes, the seed and Champion digests, the
  known `campaign_outcome` (or `null`), separate `campaign_complete` and
  `measurement_complete` flags, and one row per changed file with `path`,
  `kind` (`add`, `modify`, `delete`, `mode`), before/after hashes and an
  executable flag.
- `payload/<n>` — the new bytes for added or modified files.

Limits worth knowing:

- **Files only.** Changed empty directories and a file replaced by a
  directory cannot be represented (`export_unavailable`). Permissions are
  reduced to an executable flag `0` or `1`.
- **No project around it:** no dependencies, virtual environment, secrets or
  apply instructions. Lab never applies changes to your project.
- **In memory, private.** Diff, export and `read_export` hold the whole
  content in memory (no streaming), and the ZIP can contain your agent's
  content. Protect copies.
- **Not a verdict.** A ZIP says the files differ, not that the agent
  improved or what it cost. Read the outcome and completeness flags.

Repeating an export after success reuses the same archive and hash.

### Deleting results

When you no longer need an operation's results, delete them explicitly with
`owner.delete_results(ref)` (CLI `lab delete-results OPERATION --wait`). The
operation must be released first (finished with confirmed cleanup, or
cleaned up or manually released). The finished task's payload is
`{"operation_id": "…", "state": "deleted", "failure": null}`, or
`"state": "failed"` with a failure.

Deletion removes the operation's run tree (including exports and staging)
and its scope log and result files. It keeps the Bundle, the prepared
execution, target snapshots, cleanup receipts and any copies you made. A
small summary survives in the operation status (`results_state: "deleted"`,
the coverage, and a `retained_summary` with the campaign outcome and cleanup
dispositions), so you can still tell what happened.

Afterwards `report` is `unavailable` (`results_deleted`) and `diff`,
`export` and `read_export` refuse with `results_deleted`. Repeating the
deletion is safe. A failed or interrupted deletion stays `delete_failed` or
`deleting`; nothing resumes after a restart, so run `delete_results` again.

### Where files live

Everything sits in your private state directory: the Owner's database
(`owner.sqlite3`), Bundles (`bundles/`), prepared executions (`prepared/`),
target working directories and snapshots, per-process logs and cleanup
receipts (`scopes/<scope-id>/`), and each
operation's run directory with Core's journals and exports
(`artifacts/<operation-id>/`). Use the API or CLI rather than editing these
files. All of it is private: logs and journals can contain agent output.

### Results belong to their version pair

A state directory belongs to one producing version pair. For a new pair,
create a **new** state directory and never copy an old database, receipt,
run tree or virtual environment into it: there is no migration. Old results
stay readable only with the environment and state directory that produced
them, so keep that environment as long as you need them.

## Reference

- [Diagnostics guide](../../lab/src/evolution_lab/guides/diagnostics/SKILL.md) — compact reading rules.
- [Owner guide](../../lab/src/evolution_lab/guides/owner/README.md) — cleanup, release and deletion conditions.
- [Owner results API](../../lab/docs/api/owner-s4.md) — resume, diff, export and deletion shapes, the ZIP manifest, refusal codes.
- [Service and CLI API](../../lab/docs/api/service-s5.md) — the HTTP routes behind the CLI.
- Code: `src/evolution_lab/owner/models.py` (states), `src/evolution_lab/report.py` (campaign report).

## Authorized and linked continuation

For a project, use `lab campaign DIR --continue`; for a finished campaign, use `lab campaign DIR --from OPERATION`. Both take the installed wheel arguments. Refusals list every Core finding and the exact Lab acceptance syntax. `--accept CODE:DIGEST --reason TEXT` accepts named latest-start findings, with an optional Core-offered `--effect`. Use `--continue --reprepare` to prepare the edited project explicitly, or `--prepared ID` to select an existing preparation; status shows the current digest and changed per-start history, and report Markdown keeps that history. The service forms are `lab resume OPERATION --accept ... --reason ...` and `lab start PREPARED --linked-from OPERATION`, with target/generation/secrets as usual. Existing JSON status remains `/8`. See [Continue and link](campaign.md#continue-and-link).
