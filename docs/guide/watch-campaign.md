# Watch a campaign

[Русский](ru/watch-campaign.md)

A campaign can run for a day or more. This page is the cheat sheet for following one from a terminal: the command to
leave running, what each line means, and what to do when it stops.

## The commands

| Command | Use it for |
|---|---|
| `lab watch OPERATION` | The main way: a snapshot, then one line per change; it exits when the operation needs you or has ended. |
| `lab status OPERATION` | One look at the operation: control state, `progress:`, a pause with its `hint:` and `continuation:`. |
| `lab campaign OPERATION` | One look at the campaign view: outcome, gate, rounds and attempts. |
| `lab list --summary` | One line per operation: id, kind, state, phase, outcome. |
| `lab report OPERATION --markdown [--output FILE]` | The run's report as one Markdown file (`report.md`), to keep or forward. |

`lab watch` reads status `lab-operation-status/7` and campaign view `lab-campaign-view/11`: both family 14
(manifest `/15`) and family 15 (manifest `/16`). The feed uses the same text as `lab campaign` and `lab status`.

All of them take `--socket` or `--home` as usual ([CLI](cli.md#global-flags)).

## Leave it running

```bash
lab watch OPERATION                                # in a terminal you keep open
nohup lab watch OPERATION >> watch.log 2>&1 &      # in the background; follow it with: tail -f watch.log
lab watch OPERATION | tee -a watch.log             # on screen and in a file
```

Every line is written at once, so `tail -f` and `tee` show it live. `--interval SECONDS` (default `10`) is how often at
most the campaign view is re-read; status changes are printed immediately. If a status arrives inside that
interval, one deferred read runs when the interval expires, even if the event stream stays silent; further status
changes before then share that read. `lab watch` only reads: stopping it
(`Ctrl-C`, exit `130`) leaves the operation running. After `lab resume`, start `lab watch` again.

## Reading the feed

Family-15 lines have stable keys `ind:<round>:<attempt_id>:gate` and
`ind:<round>:<attempt_id>:global`; each scope uses
`ind:<round>:<attempt_id>:<gate|global>:scope:<name>`. The old `…:comparison`
key is gone. A level prints only when recorded, with its own reason, deciding
scope and round-Champion reference (stored/fresh/unknown plus short
`reference_measurement_id` when present). JSON `comparison` is global when
present, otherwise gate; Lab copies all three fields independently.

The first line names the operation, its kind, the start number and the local date and time. Below it, without a time,
come the lines of `lab campaign` as they are now (for an evaluation there are none), then the `progress:` line. From
then on every line starts with the local time `HH:MM:SS`, and only lines that are new or changed are printed; a line
that disappears is not reported.

| Line | Meaning |
|---|---|
| `progress: …` | As in `lab status`: the phase, round current/planned, attempts closed+running/planned, scored/expected samples. An evaluation shows `progress: scored s/e`. |
| `outcome:`, `gate:`, `settings:`, `connection:` | The header of the campaign view, as in `lab campaign`; `gate:` is family 14 only. `outcome: -` means Core has no terminal outcome yet. |
| `round N …`, `  <attempt id> …` | A round or an attempt whose line is new or changed: a new attempt, its group and Core's reason, a gate look, a final decision. |
| `independent: …`, `  gate …`, `  global …`, `  <scope>: …`, `round N preflight …`, `  preflight …`, `  eligible final score …` | Family 15 settings and separate recorded gate/global comparison, scope, preflight and final-score lines; only the line that changed is printed. |
| `refused: <code> …` | Core refused the observed start. The final status supplies the code, path and class; a status from a different start cannot supply this line. |
| `no candidate was measured: …` | Lab's notice next to Core's outcome. |
| `start N` | The operation was resumed: a new start. |
| `--- YYYY-MM-DD` | A new local day begins. |
| `campaign view unavailable: <code>` | The campaign view cannot be read now; right after the start, before Core's journal is readable, it is `not_found`. The watch goes on from the status and retries after `--interval` seconds even without another status update, on reconnection, or in the final read. |
| `reconnecting: <code>` … `reconnected` | The connection to the service broke (a restart, `service_stopping`, the stream ended) and came back. What changed during the gap shows as one difference. After ten minutes without success the watch exits `1` (`interrupted_observation`); run it again. |
| `start M began after start N ended — run lab watch again` | Someone resumed the operation right after it ended; this watch reports start N only. |
| `final read unavailable: <code>` | The last status read failed, so the summary has no pause or outcome lines; read `lab status`. |
| `watch ended: <class> start N exit <code>` | The last line when a summary is emitted. The class is `finished`, `suspended`, `abnormal`, or `unknown` (exit `1` when a failed final status read leaves the class unknown). Permanent refusal, exhausted reconnection, timeout, or `Ctrl-C` can exit without a summary. |

## When it stops

| Exit | `watch ended:` | What to do |
|---|---|---|
| `0` | `finished` | Read the results: `lab campaign`, `lab report --markdown`, [Read the results](results.md). |
| `3` | `suspended` | Core paused the campaign. The lines `suspended: <reason> (owner=…, evidence=…)`, `hint: …` and `continuation: …` say what to repair and how to continue. Repair it, run the `continuation:` command (`lab resume …`), then `lab watch` again. More: [Suspension](campaign.md#suspension). |
| `4` | `abnormal` | The run ended abnormally. The `abnormal:` line lists the causes and the `hint:` line gives the next command; see below. |
| `1` | `unknown`, or no line | The service refused a read (the operation is unknown or its results were deleted; the error is on stderr), ten minutes passed without a connection, or the last read failed and the class is unknown. |
| `2` | — | A usage error: `--json` (use `lab events` for JSON frames) or `--interval` below 1. |
| `124`, `130` | — | A request timed out (`--timeout`), or you pressed `Ctrl-C`. The operation goes on. |

### Exit 4, by cause

The line reads `abnormal: control_state=<state> target_cleanup=<state> failure=<code|-> persistence=<state>`. The
`hint:` line follows the first cause in this order:

- **Target Service cleanup is not confirmed** (`control_state=orphaned` or `cleanup_required`, or `released` with cleanup not
  confirmed): `hint: run lab cleanup operation <id>, then read lab status <id>`. Cleanup stops what the run left
  behind; while it is not confirmed the campaign cannot be resumed. Details:
  [Resolve anything left uncertain](results.md#step-5-resolve-anything-left-uncertain).
- **The operation failed** (`failure=<code>`): `hint: the operation failed (<code>); read lab status <id> and lab report <id>`.
  The status names the stage that failed; the report shows what Core recorded before it.
  For `failure=core_refused`, `refused: corpus_inside_role_workspace …` (or another recorded refusal code)
  gives Core's cause. Read that code and path in `lab status` and repair the refused input before continuing.
  If the control state is also `cleanup_required`, the cleanup hint above takes precedence; the `refused:`
  line still explains the refusal.
- **The result was not saved** (`persistence=failed`): `hint: the result was not saved (persistence failed); read lab status <id>`.
  The run may have finished, but Lab could not record its result:
  [Persistence: was the result saved?](results.md#persistence-was-the-result-saved).

## Save the report

`lab report OPERATION --markdown --output /home/you/out/report-OPERATION.md` writes the run's report (Core's outcome,
the gate, Lab's notices, rounds and attempts, the corpus) to a new file with mode `0600`; it never overwrites. Without
`--output` it prints the text. The panel offers the same file as **Download report (.md)**. For a running campaign the
report covers what has been read so far.

## Authorized and linked continuation

For a project, use `lab campaign DIR --continue`; for a finished campaign, use `lab campaign DIR --from OPERATION`. Both take the installed wheel arguments. Refusals list every Core finding and the exact Lab acceptance syntax. `--accept CODE:DIGEST --reason TEXT` accepts named latest-start findings, with an optional Core-offered `--effect`. A different prepared execution is explicit (`--prepared ID`); status shows the current digest and changed per-start history, and report Markdown keeps that history. The service forms are `lab resume OPERATION --accept ... --reason ...` and `lab start PREPARED --linked-from OPERATION`, with target/generation/secrets as usual. Existing JSON status remains `/8`. See [Continue and link](campaign.md#continue-and-link).
