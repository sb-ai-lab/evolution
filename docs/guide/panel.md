# The Lab panel

[Русский](ru/panel.md)

**Goal:** open a read-only web page on your own machine that shows what the
Lab service is doing — projects, their scenario steps and cycles, facts,
every experiment and the live progress of a running campaign — and shut it
down again. **You are done when** the page opened from the link `lab panel`
printed shows the service as healthy, you have opened the screens listed in
[Step 4](#step-4-open-the-screens), and `lab panel stop` reports the panel
stopped.

The panel only *reads*. Every action (starting an evaluation, a campaign,
accepting a change) stays in `lab …` commands or in your own tool. The
panel has the shell, the overview screens and the experiment screens
(campaign, measurement, evaluation, suspended campaign, diff), served through
a small local gateway.

The page has two parts. **[Steps](#steps)** is the checklist to follow.
**[Reference material](#reference-material)** below it explains what the
panel serves, its security model and what to do when the service is down.
The exact wire shapes are in the [panel API reference](../../lab/docs/api/panel.md).

## Before you start

- Lab is installed ([Install](install.md)) with the optional panel
  dependencies (Step 1).
- You know your Lab home (`--home`, `LAB_HOME`, or the default). The panel
  finds the service through `<home>/service.sock`, exactly like every other
  `lab` command; pass the same `--home` everywhere.

## Steps

1. [Install the panel extra](#step-1-install-the-panel-extra)
2. [Start the service](#step-2-start-the-service)
3. [Start the panel and open the link](#step-3-start-the-panel-and-open-the-link)
4. [Open the screens](#step-4-open-the-screens)
5. [Check the panel](#step-5-check-the-panel)
6. [Stop the panel](#step-6-stop-the-panel)

### Step 1. Install the panel extra

**Do:** install Lab with the `panel` extra (it adds `fastapi` and `uvicorn`;
the `service` and `dev` extras already include them).

```bash
pip install 'evolution-lab[panel]'
```

Working from a built wheel, install it the same way with the extra:
`pip install './evolution_lab-<version>-py3-none-any.whl[panel]'`.

**Done when:** `python -c "import fastapi, uvicorn"` succeeds in the same
environment as `lab` (`lab panel --help` runs even without the extra, so it
cannot tell). Without the extra, a foreground `lab panel` exits with
`panel_dependencies_missing`; with `--background` the detached child fails
instead, the command reports `panel_start_failed`, and the details are in
`<home>/logs/panel.log`. This holds for any start error of the detached
child (missing extra, port in use, ...): with `--background` the parent reports
`panel_start_failed`, and `panel_start_timeout` (the child did not become ready
in time; it is stopped) carries the child's `pid`.

### Step 2. Start the service

**Do:** in one terminal, start the service for your home
([CLI and local service](cli.md#start-and-stop-the-service)).

```bash
lab serve --home ~/lab-home --state ~/lab-state/dev81-b22 \
  --execution-wheel /path/to/evolution_lab-….whl --execution-wheel /path/to/evo_skills-….whl
```

**Done when:** `lab serve` printed its ready line and `<home>/service.sock`
exists. (The panel starts without the service, but shows
`service_unavailable` until it is up — see [below](#when-the-service-is-down).)

### Step 3. Start the panel and open the link

**Do:** in a second terminal, start the panel. Either in the foreground:

```bash
lab panel --home ~/lab-home --open
```

or detached, so the terminal stays free:

```bash
lab panel --home ~/lab-home --background --open
```

`--open` launches your default browser at the printed link; without it, copy
the link yourself. `--port N` picks a fixed port (default: any free one).

The output is one line, `panel: http://p<random>.localhost:<port>/?token=<token>`
(with `--json`, a `lab-panel-runtime/1` object). The host name is new on every
start (see [Security](#security)); browsers resolve any `*.localhost` name to
this machine, and the panel itself listens on `127.0.0.1` only.

**Done when:** the browser shows the panel page with the service state `ok`
and the project list (empty if you have connected nothing). Start a campaign
in the service and its row updates live.

### Step 4. Open the screens

**Do:** walk through the screens from the sidebar (each one is a `#/…`
address of the same page; the [table below](#what-the-panel-shows) says what
each shows).

1. **Projects** (`#/projects`, the home page): read *Now* above the projects
   table, then click *All projects* and read one row per project.
2. **Project** (`#/projects/<id>`): click a project name.
3. **Facts & confirmations** (`#/projects/<id>/facts`): use the link at the
   bottom of the project screen.
4. **Experiments** (`#/experiments`): click *All experiments* in the sidebar,
   then click a row.
5. **State reference** (`#/states`): click *State reference* in the sidebar.
6. Press *Light theme* (or *Dark theme*) at the bottom of the sidebar.
7. **An experiment** — follow one campaign down (see
   [the experiment screens](#step-4a-open-an-experiment) below).

**Done when:** every screen above shows data, or says plainly why it does not
(a loading note, an explicit empty-home or `No experiments yet` line,
`Unavailable (<code>)`); if a campaign is suspended, the project screen shows
its resume verdict and, when Lab allows continuing, the exact `lab resume …`
line to copy; the theme switch changes the page at once and is still set after
you reload it.

### Step 4a. Open an experiment

**Do:** on *Experiments*, click a campaign row.

1. **Campaign** (`#/operations/<id>`): start with the status card: *Now*,
   *Progress* (or *Where it stopped*) and *Result*. Below it are suspension
   history, rounds and attempts, Champion lineage, independent acceptance,
   samples, Target Service token totals and the report download. Setup, routing,
   checks and the Reflector probe are folded below. A running campaign shows
   *In progress* attempts and updates by itself.
2. Click an attempt id in an attempt row: **Measurement**
   (`#/operations/<id>/measurements/<measurement>`) — the attempt's *Decision*
   and *Confidence* panels, its changes against the round baseline, the
   Reflector's hypotheses as Lab read them, the Editor's failure codes and the
   per-case results.
3. Click *Open full diff* (or *diff against original* on the lineage):
   **Diff** (`#/operations/<id>/diff`) — pick a pair of versions, read the
   files with their `+`/`−` counts and hunks.
4. Click an evaluation row instead: **Evaluation** shows the same cards and
   cases for one measurement.
5. For a campaign that stopped and waits (*Suspended*): **Suspended**
   shows the shared status card, the resume verdict and latest pause evidence,
   followed by rounds, Champion lineage and starts. When Lab allows continuing,
   it shows the `lab resume …` line to copy (and, if the
   target must be fresh, `lab agent stop …` for the old one). Copy and run
   it in your own terminal; the panel runs nothing.

**Done when:** each screen above shows data, or says in words why it does
not (see the [three wordings](#what-the-panel-shows) below); a *Copy* button
sits on every command; the diff of a campaign whose results were deleted
shows `results_deleted` instead of files.

### Step 5. Check the panel

**Do:** ask whether a panel runs for this home.

```bash
lab panel status --home ~/lab-home
```

**Done when:** it prints `panel: running <link> (pid …, since …)`.
`panel: not running` means no panel holds the home.

### Step 6. Stop the panel

**Do:** stop it (a foreground panel also stops on Ctrl-C).

```bash
lab panel stop --home ~/lab-home
```

**Done when:** it prints `panel: stopped (pid …)`, and `lab panel status`
then prints `panel: not running`. The link is dead from now on.

## Reference material

### What the panel is

- **Read-only.** Only `GET` (and `HEAD`) is accepted; anything else answers
  `405 method_not_allowed`. Actions live in `lab …`.
- **Nothing of its own.** The panel does not read the Lab home or interpret
  service JSON: it forwards an allowlist of the service's `GET` routes over
  the service's Unix socket ([API: what is forwarded](../../lab/docs/api/panel.md#what-is-forwarded))
  and serves a static page.
- **One panel per Lab home**, enforced by a lock on `<home>/panel.lock`. A
  second `lab panel` for the same home fails with `panel_already_running`
  and names the running one's link when it can.
- **The link lives for one run.** Each start makes a fresh token and a fresh
  host name; a link from an earlier run stays dead. Open the link once: the panel answers with a
  redirect to `/` and a session cookie (`lab_panel_<port>`, so panels of
  several homes on one machine do not overwrite each other's cookie).

### What the panel shows

One line per screen. Everything comes from the service's `GET` routes the
panel forwards; the panel adds arithmetic over what Lab returned (counts,
durations, joining an operation to its project) but no judgements of its own
and never reads Core's raw files. A screen or a section whose data is
missing says so in words, not with an empty area. Three wordings exist:

- `Core does not record this` — the Core version in use does not write it (the
  link between an attempt and the Reflector's hypothesis; token counts of a
  subprocess Editor).
- `Not read by Lab yet — next version` — the data exists in Core's files,
  but this Lab version does not read it yet.
- `Unavailable (<code>)` — a read failed. The code is Lab's own error code
  (`not_found`, `service_unavailable`, …), or `http_<status>` when the answer
  has none, `invalid_json`, `network_error`, `invalid_id` for an id the
  panel refuses to request, or `diff_unparsed` for a diff whose text the panel
  cannot parse.

Where each one appears:

- `Core does not record this` — on *Measurement*: which hypothesis of the
  Reflector an attempt used, and the Editor's token counts.
- `Not read by Lab yet — next version` — the Reflector's and Editor's input
  and reasoning, a case's detail (input, reference, dialogue, judge verdict),
  a changed file's description, and the baseline breakdown by owner (*Suspended
  campaign*).
- The Reflector's hypotheses are shown as written. Per ADR-0019 item 4 their
  text may quote parts of the reference answers; treat a shared screenshot of
  that block like the data it came from.

`Unavailable (<code>)` marks a failed read — per item where the panel
joins several reads: a sidebar project's experiments, a Projects row's
*Connect*/*Evolve*/*Champion* cells, an Experiments row's project, a suspended
cycle's resume card (so `none yet` or `—` always means Lab answered and had
nothing; an item shown from its last answer carries *stale*), and — where Lab marks a
whole section `unavailable` or `not_started` (progress, times, results) — the
plain label *Unavailable* or *Not started*. Where Lab also gives a section
reason code (the start time of an experiment: `unreadable`,
`core_raw_unsupported`), the Experiments list appends it: *Unavailable ·
Unreadable*.

| Screen | What it shows | Not shown |
|---|---|---|
| Sidebar and banner | In the sidebar: service health card, every project with its experiments (state dot, short id), the theme switch. In the page: a banner when the service or the panel stops answering | — |
| **Projects** (home) | An always-present *Now* block for the running experiment or confirmed idle state, then one row per project: the six *connect* steps, *evolve* cycles and Champion version; the service strip | The Champion's mean score (only its version) |
| **Project** | What ran in the project, newest first: on top the current cycle (as Core's Owner marks it), else the latest run, as a pipeline; below, the history — other cycles and produced one-off scenarios such as `connect`, one row each, opened on demand. What could run but has not (the next cycle, an unproduced or unavailable scenario) is not shown. Each step with its state, when it was done, why it is outdated or blocked; for a suspended cycle the resume verdict and, where Lab allows continuing, the copyable continuation line — the same one `lab resume` prints | A one-line description of each step (Lab returns only the title) |
| **Facts & confirmations** | The project's facts (whether each holds, where it is known from, why it is outdated) and the confirmations recorded for them | — |
| **Experiments** | Every campaign and evaluation of the home, filterable by kind: project, status, progress, result (outcome and Champion version), results state, start time, duration. A row opens the experiment's screen (campaign, suspended campaign or evaluation) | A *Needs attention* filter (a judgement); a mean in the *Result* column |
| **Campaign** | The status card, suspension history, rounds and attempts, Champion lineage, independent acceptance, samples, target token totals (*partial: target calls only*) and report download; setup, routing and diagnostics are folded; live while it runs | Hatched or *infrastructure* cells; which hypothesis an attempt used; the Editor's own token counts; confidence (it is on the attempt's Measurement screen) |
| **Measurement** | One measurement: for an attempt the *Decision* panel (gate, final decision, Champion transition) and the *Confidence* panel with its look history; cards (metrics, coverage, retries, call failures, tokens), the comparison with the round baseline, a summary of the attempt's diff, the Reflector's hypotheses of that round as written, the Editor's failure, role event cards, and the selected case's detail above the cases table | The description of each changed file; reasoning removed by Core; unrecorded role content |
| **Evaluation** | The same cards and case detail for the single measurement of an evaluation; the sample strip is the first row of *Cases* (a cell selects its case), with the critical-pass count | Unrecorded case content |
| **Suspended campaign** | The shared status card, resume verdict and copyable continuation line, *Latest pause evidence*, rounds, Champion lineage, starts, baseline strip and report download; setup and routing are folded | The baseline breakdown by owner and hatched cells |
| **Diff** | Pairs of versions (original to Champion, Champion to Champion, attempt to its round baseline); files from the patch plus binary and mode changes with their own marker; `+`/`−` counted from the patch text | — |
| **State reference** | Every state code the panel knows with its label and colour, the four resume verdicts, and how a campaign that has just started (no Core events yet) looks | — |

A campaign (also a suspended one) and an evaluation screen have **Download report (.md)** while the operation's results are present: the panel reads
`report.md` and saves it as `report-<operation id>.md` through the browser; on an error it shows the code next to the
button and saves nothing. Where the file lands and its permissions are the browser's choice.

The three honest outcomes stay apart: *Not better* is neutral, *Not enough data* is amber, *Infrastructure*
is violet. The **State reference** is where to look up any label you meet.

Lab dev79 reads the campaign view `lab-campaign-view/15` and refuses any other version (`Unavailable (unsupported_schema)`) on every screen that reads it — campaign, suspended campaign, measurement and diff — before reading any of its fields, so nothing from it is shown and no measurement it lists is fetched. The same
Unavailable result applies when a scalar the panel reads has the wrong type: family-14 numeric confidence margins
remain numbers, whereas family-15 statistical quantities must retain `{value, exact}`; nullable reference ids are
strings or null. This is a guard for fields the panel reads, not a second full closed-form decoder.
The published dev72 pair retains campaign `/14`; its runs remain with their producing wheels.

On campaign and suspended screens the status card gives three views of the same
recorded facts. *Now* contains the status, a sentence templated from fields Lab
returned, and the underlying codes; an unknown outcome or reason remains a raw
code. A pause and a diagnostic from the last start can appear together, without
asserting whether that start ran. *Progress* or *Where it stopped* shows the
round and attempt counts; coverage is shown only for an active campaign. *Result*
counts recorded champions and links to their lineage, then lists limits and Lab
notices. It never selects a Champion. The first two entries are visible; expand
the list to read the rest and the full notice text. When a campaign view cannot
be read, its outcome is unknown, not a confirmed absence; read states and stale
markers identify which answers remain available.

The home *Now* block uses the service health read to distinguish running, idle,
loading and unavailable. A last known answer carries *stale*. The last-closed
line appears only when every project summary needed for selection is fresh and
contains an eligible operation; an incomplete selection is not presented as the
latest. A *Failed* reflection pill on a round names the error class (*Failed · oracle_leak*). A campaign on manifest `/15` run in `observe` mode carries a
neutral limit, *Observe gate not enforced*: passing the gate makes an attempt a finalist without a statistical test; acceptance requires Core’s final contrast. The
*Accepted* and *Promoted* pills keep their colour; the limit is listed in the status card,
in the limits of each round and attempt the view marks, as a small neutral mark on the promoted champions' lineage
cards, and on a Champion's measurement page. When the view could not read the caveats of a `/15` campaign
(`gate_mode: null`), the gate is unknown: the same places, and every round and attempt, carry a neutral *Gate unknown —
not evidence of quality* limit instead. A `statistical` campaign has no such limit: the gate applies, its mode is a
neutral mark in the attempt's Confidence panel, and Core's decision is shown on each attempt.

The folded **Campaign setup & routing** block sums up stability,
round-stop policy, baseline refresh, roles and the connection check in one line,
amber when the connection is not compatible or a refresh did not complete. Opened,
it shows Settings, Roles, Connection and every recorded baseline refresh. Each refresh
names the replaced measurement, the new measurement and whether it completed; a record
is not itself a completed refresh, and the refresh `count` is shown apart from the records.
Connection detail shows only code, cause code, metric and recorded paths as text.

Every final entry of every round appears once, in Core order, as the attempt's second
step: a *Final · entry K* row under the finalist it measured, or, with
`round_stop: all_attempts`, a *Joint final · entry K* row after the attempts. The row
shows the measurement, point, Δ against the Champion, Core's decision and
*vs vN · stored* or *re-measured*. The decision is *Accepted*, *Not accepted*, or
*Unavailable* with its code: a contrast Core left incomplete (`final_incomplete`) is
unavailable, not rejected — the same word as on the Measurement screen. Opening the
row shows ordinal, stability, condition changed in place, Champion side, stored
measurement, mode/global setting,
members and their completion/missing/critical values, contrasts with bounds,
acceptance or unavailable reason, exact/pairing and removed-pair/case counts,
selection, listed pairs and next baseline. A finalist whose final has not started
yet has a dashed *Final measurement · not started* row. Measurement identifiers link
to their read-only screens. Those screens label all recorded final and
baseline-refresh relations.

A *Finalist* entered the final. Its final decision is shown separately; a final
selection or an accepted contrast does not establish promotion. *Champion
transition* comes only from matching `promoted_attempt` records in available
Champion lineage. All recorded transitions are shown in lineage order; they do
not assert that the version is still the current Champion. Missing lineage is
unavailable; available lineage without a match says *No transition recorded*.
In the rounds, an attempt row's *Champion* column shows `→ vN` from the same
lineage, `—` when none is recorded, and *unavailable* when lineage is. On the
Measurement screen these are three facts of the *Decision* panel — *Gate*, *Final
decision*, *Champion transition* — each with its own empty state; with several
promoted versions, the lineage caption names each version's measurement.
The round's outcome remains a separate fact.

Confidence is not shown in the campaign's rounds; it is on the attempt's Measurement
screen. Under the Decision panel, the **Confidence** panel shows the mode once
(`observe` with an amber *bounds not computed*), Core's latest evaluated look: *Accepted*, *Rejected*,
*Insufficient evidence* or *Next look*, the unchanged Core reason code, the look number (and whether it is final),
and the margin. *Focus* and *Causal* rows show `point [lower, upper]`, the interval's exact indicator and case
count, and a drawing on a shared scale only when both bounds are recorded; a missing bound is `—`, never 0.
A secondary line shows repeats and calibration. The *Look history* disclosure lists every evaluated look with
its reason; there a missing bound reads *no lower bound in the view* or *no upper bound in the view*, and a
look without bounds shows the point with *no bounds in the view*.
For Core’s per-estimate `unavailable` codes, read `lab attempt OPERATION ATTEMPT`.
A null estimate or calibration is `—`;
`confidence: null` says *not recorded for this attempt*, while `latest: null` says *not evaluated yet* with repeats and case counts.

**Family 15 (`/16` campaigns).** Under the settings summary (R, mode, noise, verification) each attempt shows its outcome
and reason and, per scope, the statement for that scope's own rule: "gain; with c confidence not below L; threshold"
(`bound`), "gain; threshold" (`point`), "gain" with "no threshold" stated, or Core's status with no numbers. No two-sided
interval is drawn. Each comparison is labelled "vs round Champion" with its stored or fresh measurement, or "measurement
not recorded"; there is no "vs Parent". An attempt with no comparison shows its reason and the attainability facts. The
panel stays read-only. Both recorded levels appear in separate gate and global blocks with their own outcome,
reason, deciding layer, reference and scopes; absent levels are omitted. A `reference_measurement_id` matching
a `measurements[].measurement_id` in this view links to that Measurement screen; otherwise it is short plain text,
with no extra request. The attempt's Measurement comparison and campaign/diff delta labels use the gate comparison.
The Candidate's separate global final measurement has no attempt comparison label. JSON `comparison` is global
when present, otherwise gate; Lab copies all three comparison fields independently. Only a one-sided lower bound is shown.

The family14 view's `caveats` appear with a plain label and the unchanged code.
Campaign caveats appear among the limits in the status card; round and
attempt caveats sit in their own rows as an amber *N limits* chip that opens the list
(the chip's title carries the same lines); on a measurement they appear in the
Confidence panel, or alone when there is none. Each line names its scope: *Campaign*,
*Round N*, or *Attempt ID · round N*. A measurement shows campaign caveats,
its round's caveats and only its own attempt's caveats; final measurements do
not inherit member attempts' caveats. Caveats remain visible without confidence
or a latest look. Unknown kinds keep their raw code. These lines do not change
Core's decision or the gate marks.

`/15` carries `family: 14 | 15`; for family 14 it omits `cohort` and attempt `alpha_total` entirely.
The Confidence panel shows margin and one exact indicator per interval beside its
point/bound presentation. Missing numbers stay `—`; unknown codes remain
literal. The panel makes no new statistical-bound claims. Older views are refused rather than converted;
old runs and state must be viewed with their producing wheels.

For family 15, the folded checks block shows the view
`checks.modes` and each `checks.shadow_hits` count. Modes and counts remain
visible when independent acceptance is unavailable. A present null checks
value omits the block; unavailable checks show their unreadable state.
Counts from different checks are not comparable and are never summed.
The panel copies Core data from the view; check modes are separate from Lab notices.

### Current suspension and history

A suspended campaign reads its **Latest pause evidence** when its screen opens;
the block sits right under the cause and the resume verdict. Its header names the
pause seq, reason, owner and evidence kind, then the row count, the digest's first
8 characters and whether the rows are complete. The latest matching event is
*Current pause*; earlier events sit collapsed under *Earlier pauses*, each marked
*Historical*. When the evidence is unavailable there, the panel says that the cause
still comes from the campaign view and shows no rows. A resumed or completed
campaign offers a collapsed **Suspension history** below the status card: its
summary counts the pauses the campaign view lists (*N pauses recorded*, or *No pauses
recorded*) without reading anything, and says *loaded when opened* until you open it.
The count and *No pauses recorded* appear only while the campaign read is fresh; when
it is stale, unavailable or still loading, or the service is out, the summary makes no
claim about pauses (a stale or failed read does not mean there were none). Once open,
the count stays only while the history read itself is fresh; otherwise the history
body shows its own read state.
Current status comes from the operation, so an old pause does not turn a resumed
campaign into a currently suspended one.

Rows show measurement/case/sample identifiers and retry ordinal, with reason and
nullable code for scorer faults or owner for final/Champion rows. Measurement
identifiers link to their screens. Threshold and stage evidence show only their
closed fields; byte-prefix locators remain plain text. These are the rows that
caused a pause, not the full remaining worklist. *Showing N of count* and
`truncated` describe the returned prefix; Lab cannot fetch the missing tail.

Loading, unavailable and stale apply independently to history; a failed read
keeps the rest of the screen usable. Retained evidence is marked stale after a
failure or newer known suspension sequence and cannot confirm the current cause
or completeness. Sequence changes invalidate the history; an open block rereads,
while a closed block waits until the next opening. *No pauses were recorded*
requires a successful empty result. Operation changes clear previous data, and
result deletion clears retained history rather than displaying it stale.

### Reading case and role content

On **Measurement** and **Evaluation** the selected case's detail sits above the cases table (the first
row until you pick another); click a row, or use ↑/↓, to move between cases. On an attempt's measurement
the baseline and Candidate sides stand next to each other when the pair is verified (same case and
sample); otherwise the baseline side says *Baseline not paired* with its code. Each side shows public
input, dialogue, scoring verdicts and feedback. A turn shows its input (`message.content`) and its
`assistant_message` events, each labelled *Turn N · assistant_message* (followed by the event's name when
recorded); every other event, `message.fields` and the turn's usage sit in *Technical events (n)* under
the turn, and a turn without an assistant message says *No assistant_message in this turn*. A failed
scorer can leave the dialogue readable; its raw output is private diagnostics, not a verdict. A missing
or incomplete trace has an explicit state and reason. The detail starts public whenever another case or
measurement is selected.

**Show private content** rereads the content with `?disclose=private`: reference/oracle,
scorer diagnostics/details and verdict notes then appear. Only these private parts come from
the private read: turns, input, scoring and terminal output stay as the public read gave them, so
a part too large in one answer does not hide what the other answer returned. The choice is not remembered. The
Reflector card uses the same explicit button for its private text and rereads the reflection.
**Do not pass `--disclose-private`, `?disclose=private`, or revealed reference/diagnostics into the
editing model's context. Core keeps the reference away from that model; disclosure is for human
inspection**.

An attempt's measurement has **Reflector** and **Editor** cards: ordered events, tool calls/results
when recorded, changed files or their recorded count, and failure codes/text. `agent_cli` does not
record tool calls; its card shows a summary. Subprocess roles have only a stderr tail when recorded.
`[REDACTED]` is shown as supplied, with a hint about hidden reasoning and key masking. Role usage,
Editor/scorer tokens and money are not shown. `agent_cli` is covered by unit examples only, not a
real-run contract test.

**Look history** lists all evaluated looks of the attempt, with decision, Core reason and partial
intervals; it supplements the Confidence panel's latest look. The campaign shows at most the first
400 characters of a failure. When the attempt has a measurement, its Editor card shows the full
served failure text (subject to the response limits). An `editor_failed` attempt without a measurement
has no measurement destination; use `lab attempt O ATTEMPT` for its served content.

Content sections show unavailable state and reason (including `too_large`) beside that section.
`masking: keys`, `truncated`, omitted-entry counts and clipped field paths with their original character
sizes are visible. Key masking does not compare text with actual secret values. Original sizes are
after masking, before clipping. The whole response is capped at 1 MiB, using text/list tiers
16,384/200 → 4,096/100 → 1,024/40, then replacing largest permitted sections with explicit
unavailable/too_large objects. Public private-fields stay withheld even on overflow; see the
[exact API variants and metadata](../../lab/docs/api/owner-s4.md#sample-attempt-and-reflection-content).
Limits remain provisional until the track E pilot examines live traces; the small demo snapshot
establishes wiring, not live-trace suitability. No scoring-try history or incomplete role content is shown.

<a id="scorer-content-on-exact-dev55b8"></a>
### Scorer content on exact dev61/b13

These reader guarantees originated with dev61/b13. Schema names below describe the current dev79 source;
historical runs keep their producing pair.

Lab uses Core's public `ScorerTraceReader` for directory and pack traces,
addressed by the selected measurement and scoring request. Completed journal
records still gate disclosure and determine scoring state; identity/hash checks,
`lab-sample/1`, campaign `/15` with families 14 and 15, private disclosure and key
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
pass `max_section_bytes=33554432`. Request/stdout metadata and returned bytes are
also checked before parsing/disclosure. Oversize rejects the whole trace as
`too_large`, without partial input. Typed `too_large` maps to that code; other
typed read failures map to `unreadable`. No stderr is requested.

Strict no-follow traversal and bounded payload reads replace the prior b5
root-swap/file-growth exceptions. The section limit does not cap the whole pack
index or process RSS and does not make files immutable to their owner. This
residual limit is explicitly accepted for exact dev54/b6 (separately, as for local dev53/b6 before it); no successor, including a
Lab-only repin, inherits acceptance automatically. For exact dev56/b8 the user accepted it separately on 2026-10-20 (not inherited). For exact dev57/b9 the user separately accepted it on 2026-10-20; dev56 acceptance did not transfer. For exact dev58/b10 the user separately accepted it on 2026-10-22; dev57 acceptance did not transfer. For exact dev59/b11 the user separately accepted it on 2026-10-23; dev58 acceptance did not transfer. Core reader modules are unchanged: nofollow and 32 MiB per selected section remain, without an aggregate pack-index/RSS cap or owner-file immutability. For exact dev60/b12 the user separately accepted this same residual limit on
2026-10-25; b11 acceptance did not transfer. Every next pair needs its own assessment. Old pairs/runs remain with
their producing environments; there is no automatic shutdown or migration.

For exact dev61/b13, the user conditionally accepted this residual on
2026-10-27 under ADR-0019. The exact-tag audit confirmed unchanged reader/storage
modules, so that condition is satisfied: nofollow and 32 MiB per selected
section remain, with no aggregate pack-index/RSS cap or owner-file immutability.
This is a separate acceptance; dev60/b12 acceptance did not transfer.
It does not authorize release, migration or state creation.

Content still awaiting Lab readers is marked *Not read by Lab yet*. Editor token counts carry *Not shown in panel v1*,
as required by PDR-0017.

The theme is dark by default. The switch at the bottom of the sidebar changes
it and remembers the choice in the browser (`localStorage`); if the browser
refuses storage, the page still works and starts dark next time. The fonts
(Geist) are served from the panel itself, so the page needs no internet.

### Files in the Lab home

| Path | What |
|---|---|
| `<home>/panel.json` | The running panel's record (`lab-panel-runtime/1`), mode `0600`. Written once the panel listens; removed on a clean stop. |
| `<home>/panel.lock` | The lock that says a panel is alive. A free lock means no panel, whatever `panel.json` says. |
| `<home>/logs/panel.log` | Output of a `--background` panel (directory `0700`, file `0600`). It contains the launch link **with the token** — treat it like `panel.json`. |

These three are the only things the panel writes in the home. If the home
directory does not exist it creates just that directory (`0700`); it never
creates `clones/`, `workspaces/` or anything else, and it follows no symbolic
links: a home path through a symlink, a `logs` that is a symlink, or a
`panel.log` that is a symlink is refused with `home_invalid`. `lab panel status` and
`lab panel stop` follow the same rule (and never create the home; at most the panel's own `panel.lock`), so `stop --home <symlink>` never
reaches a panel behind the link.

### When the service is down

If the panel cannot reach the service — not started, restarted, or too slow —
a forwarded request answers `503 service_unavailable` with the hint
`lab serve --home <home>`. The page shows it two ways: the health card in the
sidebar turns amber (*Service unavailable*), and a banner over the screen reads
*Lab service unavailable — start it with `lab serve --home <home>`*. If the
panel itself stops answering, the banner asks whether `lab panel` is still
running. The open page re-checks the service every 5 seconds and at once when
the live stream breaks, so an outage after load is shown rather than a frozen
page; meanwhile each screen keeps its last answer, marked *stale · last answer
shown* (also an answer read only once, such as the operation card). Start (or
restart) `lab serve` for that home: within 5 seconds the banner goes, the page
re-reads every screen's data and reopens the live stream; the panel needs no
restart. The same re-read follows when the panel itself answers again after it
stopped, including when it did not answer the page's first check.

### Errors from the CLI

`lab panel` failures print a JSON error to stderr and exit 1, for example
`panel_already_running`, `panel_port_in_use`, `panel_dependencies_missing`,
`panel_start_failed` (details in the log), `panel_start_timeout` (carries the child's `pid`), `panel_identity_unconfirmed`
(`stop` refuses to signal a process that did not prove it is this panel).
The full list is in the [API reference](../../lab/docs/api/panel.md#cli-errors).

### Security

- The panel listens on `127.0.0.1` only.
- Every request must carry the right `Host` — exactly this launch's
  `p<random>.localhost:<port>`; `127.0.0.1:<port>` and `localhost:<port>` are
  refused with 421 — and the session cookie obtained from the link with the
  token. This blocks other machines and DNS-rebinding pages.
- The cookie is `HttpOnly; SameSite=Strict`; every response carries
  `Cache-Control: no-store`, a `default-src 'self'` Content Security Policy,
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- Why a random host name: browsers do not isolate cookies by port, so a cookie
  set for `127.0.0.1` would be sent to every other HTTP server on
  `127.0.0.1` you visit, handing it the token. The cookie is host-only (no
  `Domain` attribute) on a name no other server uses, so it goes back to this
  panel alone (ADR-0018 p. 3, amended).

### Remote access

Not supported. The panel refuses any `Host` other than its loopback name and
port. If you forward the port over an SSH tunnel anyway, that is at your own
risk, and the browser must still address it as `p<random>.localhost:<port>`
(the printed link) with the same port number on your side.
On an SELinux-enforcing host, `sshd` may forward only to ports whose SELinux
type it is allowed to connect to; the panel's default free port usually is
not one of them. Start the panel with `--port <an allowed port>` instead of
changing the policy.

### Running as a module

`python -m evolution_lab.cli panel …` is the same command as `lab panel …`
(the background start uses it to launch its detached child).

## Next

- [Panel API reference](../../lab/docs/api/panel.md) — forms, errors, the route allowlist.
- [CLI and local service](cli.md) — the service the panel reads.
- [Read the results](results.md) — what the data on the page means.
