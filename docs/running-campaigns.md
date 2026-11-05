# Running campaigns

Use this guide after the [Quickstart](../QUICKSTART.md). It covers the operator's job: prepare an experiment, run it, understand the result and keep useful work when something fails. The scaffold uses manifest `/15`, the stable family; opt-in `/16` provides independent acceptance controls. File loaders accept only these two schemas, and campaign readers support only families 14 and 15. Read removed-family runs with `2.0.0b9`, or seed a new unlinked `/15` campaign from the Champion copy ([migration](#move-a-14-run-to-15), [handle a refusal](#handle-a-refusal)).

For opt-in `/16`, start with [independent acceptance](#choose-independent-acceptance-manifest-16). For the scaffold's `/15` workflow, start with [comparison and stopping settings](#judge-a-candidate-with-the-stable-family-manifest-15), then use [watch](#watch-a-running-campaign) for progress and [trace](#export-what-each-case-did) for case-level evidence. Historical headings remain as links to current guidance, not instructions to use retired schemas.

## Prepare your first real experiment

Start with a small corpus and one attempt at a time. This makes it easier to tell an integration problem from a weak edit. First confirm that the unchanged agent can run and be scored, then enable the improvement loop.

Keep inputs and run output separate:

```text
my-experiment/
├── manifest.json
├── seed/                 # files the target loads; only selected paths may change
├── corpus.jsonl
├── scorer/               # scoring program, outside the seed
├── editor/               # optional subprocess editor
├── reflector/            # optional subprocess Reflector
└── runs/                 # separate directories for campaigns and evaluations
```

Generate a starting manifest with `evo-target init`, then adapt it before creating a run. Its demo scorer and role scripts are intentionally specific to the demo; they do not evaluate or improve an arbitrary agent. Use [Connecting an agent](connecting-an-agent.md) for the target and [the role reference](manifest-v2.md#editor) for the editor and Reflector.

### Decide what may change

`seed_root` is your starting tree. `artifacts.writable_paths` declares which paths the editor may change. `artifacts.dependency_paths` declares files whose contents identify the dependencies used to evaluate the tree. Start with a narrow writable set, such as `skills/**`, and keep the scorer and role programs outside the seed.

The target must load the exact candidate snapshot it is given. If it always reads the original checkout, Evolution would be testing the same agent after every edit. The [backend guide](connecting-an-agent.md#load-the-files-evolution-gives-you) explains this boundary.

### Define cases and quality

The corpus is JSON Lines: one object per line. For example:

```json
{"case_id":"route-01","group_id":"routing","stratum":"short","public":{"text":"Where do I file an expense?"},"oracle":{"expected":"expenses"}}
```

Give cases unique IDs. `public` contains inputs visible to the agent and editor. `private` is reserved for the Environment. `oracle` holds the information only the scorer should read, such as expected answers. `group_id` and `stratum` describe your evaluation groups; they are part of the comparison, not decorative labels.

The scorer must return the metric named by `campaign.metric.primary` for every scored case. It should also return criterion `verdicts` so the Reflector can identify failures. If a case declares `oracle.critical: true`, the scorer must return `critical_pass` for it. See the [scorer protocol](manifest-v2.md#what-the-scorer-program-reads-and-writes).

Both current schemas accept train/test labels: only train participates in improvement and promotion, and test is measured separately with `evo tree evaluate`. See [train/test corpora](manifest-v2.md#train-and-test-corpora-manifest-12) and [explicit evaluation](cli.md#measure-a-tree-on-train-or-test).

### Set budgets deliberately

| Setting | What you are choosing |
|---|---|
| `campaign.rounds` | Number of rounds this campaign may run |
| `attempts_per_round`, `parallel_attempts` | How many candidate edits to try and how many can run together |
| `budget.max_turns`, `budget.timeout_seconds` | Turn and time limits for one agent interaction |
| `budget.max_failed_turns` | Consecutive agent-owned failed turns allowed before ending the interaction |
| `call_retries.max_attempts` | Total attempts for a transient target call |
| `call_retries.turn_max_attempts` | Total attempts for a failed agent turn; `1` means no resend |
| `sample_retries` | Additional tries of a whole indeterminate sample, separate from call retries |
| `indeterminate` | Per-owner failure counts and rates that suspend the campaign |
| `editor_timeout_seconds`, `reflection_timeout_seconds` | Time limits for the improvement roles |
| `evaluation`, `gate`, `round_stop`, `baseline_refresh_after_rounds` | Repeats, how a Candidate and finalist are judged, when a round stops and how often the stored baseline is measured again ([stable `/15`](#judge-a-candidate-with-the-stable-family-manifest-15), [independent `/16`](#choose-independent-acceptance-manifest-16)) |

The keys in the table are under `campaign`. Do not raise `turn_max_attempts` above `1` unless retrying a failed turn is safe for your agent's session. A failed call may already have changed its state. More retries are not a substitute for that guarantee.

For a real model, allow enough time for role calls to read evidence and finish an edit. The demo's small budgets and its zero focus margin are an example, not tuning guidance for a noisy agent. See [campaign settings](manifest-v2.md#campaign) for the exact rules.

### Choose how failures are grouped

Before the Reflector runs, the Grouper groups the baseline's failing observations, and the Reflector investigates one group per round. The scaffold uses a `fields` Grouper, which puts observations with equal failed criteria and terminal status together. It needs no provider, but it cannot tell two different mistakes apart when they fail the same criterion.

For a real experiment, use a `chat` Grouper, `grouper.kind: chat`, in either current schema. A dedicated model groups failing answers by observed failure mode; a case whose repeats fail in two ways can appear in two groups. It does not diagnose causes; that remains the Reflector's job. Declare a `fields` fallback for unusable answers. A provider outage suspends rather than falling back, and the same command resumes it ([when the Grouper suspends](#when-the-grouper-suspends)). The [`grouper` reference](manifest-v2.md#grouper) has every kind, key and retry setting.

The grouping model's key is read from the environment of the `evo tree evolve` process, like a `chat` role's key:

```bash
export GROUPING_API_KEY=...
evo tree evolve --manifest ./my-experiment/manifest.json --run-dir ./my-experiment/runs/run-1
```

Without the variable, the run is refused with `grouper_env_missing at grouper.api_key_env` and exit `2`, before anything is written. The grouping model receives the declared observation projection, which excludes scorer verdicts and raw scorer `diagnostics`. The Reflector can receive scorer diagnostics while investigating failures, so choose a reflection provider that may see that data.

If the endpoint needs no credential, for example a local OpenAI-compatible server, leave `api_key_env` out of the role's section, or set it to `null`. This works for the Grouper, the Reflector and the Editor. Do not export a dummy key. The role then sends no `Authorization` header and reads no credential from the process environment, and `evo tree evolve` prints one note for it before any role runs:

```text
evo tree evolve: grouper calls http://127.0.0.1:8000/v1 without credentials (no api_key_env declared)
```

The note is not a refusal. If the endpoint does want a credential, it answers `401` or `403`, the campaign suspends, and the [provider-attempt line](#read-provider-attempt-lines) says so. A declared `api_key_env` whose variable is missing or empty is still refused as above. Adding or removing `api_key_env` on a running campaign changes the role's identity: the next run is refused once as `run_identity_changed`, and one `evo tree authorize` with the printed digest accepts it ([handle a refusal](#handle-a-refusal)). Release `2.0.0b9` refuses a manifest whose `chat` role has no `api_key_env` (`missing_key`). The [manifest reference](manifest-v2.md#endpoints-without-credentials) has the exact rules.

A model endpoint inside a closed network can use plain HTTP, for example `"base_url": "http://llm.internal:8000/v1"` for the Grouper, the Reflector or the Editor; for each such role, `evo tree evolve` prints `evo tree evolve: WARNING: grouper sends its credential unencrypted to http://llm.internal:8000/v1` (or `talks to http://llm.internal:8000/v1 without encryption` when the role declares no `api_key_env`) before any role runs, on every run, and continues without asking.

## Improve on train and measure on test

Train is the set Evolution uses to find failures, edit the tree and decide whether to promote an edit. Test is a separate set for measuring a fixed tree. Test results never enter the framework's improvement loop.

| Operation | Cases it uses | Result |
|---|---|---|
| `evo tree evolve`, manifest `/15` | Train only | Campaign report and selected tree |
| `evo tree evaluate`, `split: train` | Train only | Separate measurement report |
| `evo tree evaluate`, `split: test` | Test only | Separate measurement report |

### Prepare the partition

Before starting a campaign, use a complete `/15` or `/16` manifest. In the JSONL corpus, give every row a top-level `"split": "train"` or `"split": "test"`. The labels belong to corpus rows, not the campaign manifest. Keep all cases with the same `group_id` in one split; choose the split before inspecting evaluation results. See the [row examples and validation rules](manifest-v2.md#train-and-test-corpora-manifest-12).

An entirely unlabeled corpus is treated as train only. It cannot produce a test evaluation until you supply test cases. Mixed labeled and unlabeled rows, a `validation` label and an empty selected split are refused.

### Measure before and after improvement

Prepare two evaluation manifests using the [complete evaluation example](cli.md#measure-a-tree-on-train-or-test). Both select `"split": "test"` and the same corpus, scorer, metric, repeats and Target settings. The before manifest selects `"snapshot": {"root": "seed"}`. Paths inside either manifest are relative to that manifest.

Start your Target Service, create the parent `runs` directory, then measure the seed and start the campaign:

```bash
mkdir -p runs
evo tree evaluate --manifest evaluation-before.json --run-dir runs/before-test
evo tree evolve --manifest manifest.json --run-dir runs/campaign
```

Read the campaign report to choose a published version. In `evaluation-after.json`, set `snapshot` to `{"run_dir": "runs/campaign", "version": "v1"}`, replacing `v1` with that version. Use `v0` if the campaign made no promotion. Start the second measurement explicitly:

```bash
evo tree evaluate --manifest evaluation-after.json --run-dir runs/after-test
```

Evaluation copies the chosen tree into its own immutable artifact store. It leaves the campaign unchanged. No test measurement runs automatically at campaign completion.

### Read or resume the measurement

Check `state`, `completed_samples` and `requested_samples` before comparing `metrics`. A complete report averages repeats within each case, cases within each correlation group, then gives every group equal weight. It provides pointwise scalar metrics, without a promotion verdict or a confidence interval. A low score is a completed measurement, not an execution failure.

Run the same evaluation command with the same manifest and run directory after interruption. Already scored samples are preserved; missing samples are resumed. If the run is complete, the command verifies and returns its report without new Target calls. To deliberately remeasure a tree, use a new run directory.

Changing bound inputs requires a new run. For a `/12` or later campaign this includes test rows, even though the campaign executes only train. Keep corpora and evaluation outputs outside seed and role working directories. `evo tree evolve` refuses a corpus inside the seed, the run directory, a role's working directory or a declared prompt file with `corpus_inside_role_workspace`. If you start campaigns from Python, `check_corpus_placement` in `evolution.tree.evolve` runs the same check on the paths the manifest declares, without starting a run; the run directory is optional. `evo tree evolve` also refuses a corpus that is the agent CLI found on `PATH`. The framework controls its role inputs; external programs still have the operator's filesystem permissions. If you use test results to guide further edits, the test set is no longer an unseen final check.

## Run and save the report

Start your Target Service in its own terminal, then run:

```bash
evo tree conformance --manifest ./my-experiment/manifest.json
evo tree evolve --manifest ./my-experiment/manifest.json \
  --run-dir ./my-experiment/runs/run-1 \
  > ./my-experiment/run-1-result.json
```

The manifest resolves its relative paths from its own directory. The shell resolves `--manifest`, `--run-dir` and the report path from your current working directory. Keep the run directory outside both the seed and the declared program directories.

A new or empty run directory starts a campaign. A directory with a campaign journal continues or reports that campaign. An unrelated non-empty directory is refused. The report redirection is optional; it saves stdout for later reading. Do not reuse the same report filename if you want to keep every invocation's report.

## Read the outcome

Normal stdout contains one JSON document. Start with `terminal`, `finished`, `suspended` and `status`. `terminal: finished` means the campaign wrote its final record; read `finished.outcome` to learn whether it completed, was stopped or exhausted its budget.

| Exit code | Meaning | Next step |
|---|---|---|
| `0` | Completed, with or without a promotion | Inspect the Champion and evaluation evidence |
| `1` | Suspended, or a storage/persistence problem | Read the cause, repair it and resume the same run |
| `2` | Refused to start or continue | Read stderr; no JSON result is written |
| `3` | Stopped, including an orderly Ctrl+C | Inspect the saved Champion; use a linked run to continue |
| `4` | Campaign budget exhausted | Inspect what was completed; use a linked run if you want more work |

This table describes both current workflows. `--quiet` reduces stdout, so omit it when you want the complete `status` and `result` projections.

`status.final_champion` identifies the selected version and its measurement. Once the campaign finishes, `status.campaign_outcome.champion_path` gives its path relative to the run directory. `status.caveats` records qualifications such as changed conditions across a link or inherited baseline evidence. Read them before comparing results from different campaigns.

Current campaigns spend no alpha, and their `alpha` is `null`. Interpret confidence only under the declared comparison assumptions; neither workflow promises a campaign-wide false-promotion rate.

A `grouping_fallback` caveat means the Grouper used the declared `fields` policy or singleton groups because its answer was unusable; provider outages suspend instead. The caveat changes no score or decision, but reflection used coarser groups. An `unplanned` round closed without an Editor Attempt because reflection declined (`reflection_declined`), claimed nothing (`reflection_empty`) or failed with class `content` (`reflection_failed`). Provider and unknown failures, including timeouts, suspend immediately without closing that round. Repeated content failures on one group can skip it under the resilience limit; read `reflector-logs/` and [unplanned rounds](manifest-v2.md#unplanned-rounds-family-9).

`resilience: {rounds_skipped, continuation_turns, attempts_saved}` at the top of `status` and `result` counts skipped error groups, Editor continuation turns after an empty diff, and those continuations that finished `edited`. The CLI prints "Skipped rounds: N · Continuation turns: M, led to an edit: K". These counters change no score or gate decision.

In `/16`, a `check shadowed` line means a policy detector found a match and allowed work to continue. Read its check name, code and count alongside the declared mode; the line never includes the matched value. Status, the report and `watch` list non-enforce modes and counts. An `off` check produces no hits, so a zero count does not prove that its input was clean. See [check modes and consequences](manifest-v2.md#checks-manifest-16) before using the result.

The selected tree is an output, not a deployment. Review its diff, run your own acceptance tests and copy it into your agent's deployment through your normal release process. Keep the run directory so the evidence behind that choice remains available.

## Judge a Candidate on the cases its edit affects

Current campaigns measure a Candidate on the train cases its edit can change, then require full-train evidence before Promotion. Both schemas declare the provenance source and comparison controls. The scaffold's `/15` manifest includes these gate keys:

```json
"gate": {
  "provenance": {"kind": "reported"},
  "fresh_parent": false,
  "global": "point"
}
```

- `provenance` says how the campaign learns which files a case depends on. `reported` reads the `provenance` your Target Service reports on each turn. `metadata` reads a label you put in each corpus row. `none` treats every case as affected. Under `reported`, a Target that reports nothing makes every case affected; [Report what the agent used](connecting-an-agent.md#report-what-the-agent-used) shows what to report and how to check it.
- `fresh_parent: true` measures the Parent again on the focus and causal cases, at the same time as the Candidate. It costs one more Parent run for each such case and repeat.
- `global: point` requires a strictly positive full-train contrast; `off` imposes no numerical global filter, and `bound` requires a positive lower bound under the declared mode. A boolean is not accepted.

See [provenance and affected cases](manifest-v2.md#provenance-and-affected-cases) for the shared rules and fallback reasons, and the [stable `/15`](#judge-a-candidate-with-the-stable-family-manifest-15) or [independent `/16`](#choose-independent-acceptance-manifest-16) controls for acceptance.

### Read the `affected` block

Every Attempt in `result.rounds[].attempts[]` carries an `affected` block. Read it from the saved report:

```bash
jq '.result.rounds[].attempts[].affected' ./my-experiment/run-1-result.json
```

For the cheap smoke campaign below, it prints:

```json
{
  "alpha_statement": null,
  "fallback_reason": null,
  "fresh_parent": false,
  "layer_one": 2,
  "layer_two": 0,
  "mode": "path",
  "source": "reported"
}
```

- `layer_one` is the number of affected cases in the round's focus (its `gate_scope`). The Promotion gate decides on these cases.
- `layer_two` is the number of other affected cases, the causal scope. The gate checks them against `causal.tolerance`.
- `fallback_reason` is `null` when the campaign chose a smaller set of cases. Otherwise it names why the campaign measured every case, for example `unaccounted_path` or `none`. If most Attempts fall back, your provenance source cannot narrow the measurement; see the [list of reasons](manifest-v2.md#what-family-12-measures).
- `alpha_statement` is `null`: neither current schema spends alpha. Read the declared mode and caveats to interpret the comparison.
- `source` and `mode` say which provenance source was used, and whether cases were matched by component or by file path.

The block is `null` until the campaign has chosen the Attempt's cases. An edit that affects no focus case closes its Attempt `abandoned` with reason `no_exam_impact`; nothing is measured and no alpha is spent. The [report reference](cli.md#affected-case-reports-family-12) lists every key.

### Read the family-12 caveats

Read historical family-12 reports with `2.0.0b9`; use the [current `/15` caveats](#read-the-family-14-caveats) for supported runs.

### The completion of a winner

Read historical post-Promotion completions with `2.0.0b9`; current `/15` runs require a [final measurement before Promotion](#read-the-final-roles-settings-and-connection-blocks), while `/16` uses its declared [reuse or fresh verification](#choose-independent-acceptance-manifest-16).

### When the Target renders another component map

A Target must render the same component map for a tree while its attested identity stays the same. When an Attempt's cases were chosen by component and a Target instance for its Candidate or fresh Parent reports another map under the same condition, the campaign suspends. `evo tree evolve` prints on stderr:

```text
evo tree evolve: suspended: target_components_changed — the Target rendered a component map other than the one this condition already rendered for the tree
evo tree evolve: continue: evo tree evolve --manifest <MANIFEST> --run-dir <RUN_DIR>
```

It exits `1`, and nothing is closed. Look at how your Target builds the component map for the loaded tree, then choose one of two ways on:

- If the rendering should not have changed, repair the Target so that it renders the same map again, and rerun the printed `continue` command. The campaign creates the instance again and goes on. It does not inspect the Candidate again.
- If the rendering changed on purpose, deploy the new Target with a new attested identity, for example a new `settings_hash` (see [the constructor](target-server.md#constructor)). The next `evo tree evolve` is refused with `run_identity_changed`. Accept it with the effect `continue`, as [Change the provider mid-campaign](#change-the-provider-mid-campaign) shows. After that, the different map does not suspend the campaign, and the report adds the caveat `components_changed_by_condition` to the Attempt.

An instance that reports no component map at all is not compared and does not suspend the campaign. The check covers only Attempts whose cases were chosen by component without a fallback; an Attempt matched by path, or one that measured every case, relied on no map.

### A cheap smoke campaign

Read historical `/13` smoke runs with `2.0.0b9`; use the [current `/15` smoke campaign](#a-15-smoke-campaign) for an offline integration check.

## Choose independent acceptance (manifest /16)

Use `/16` for one observation per case with explicit point acceptance and reuse of the Candidate's gate evidence. Start from a complete scaffold manifest and apply the [required `/16` campaign settings](manifest-v2.md#independent-controls-manifest-16) to a new `manifest-16.json`. Keep the original seed and choose a new run directory; existing runs are not migrated.

```bash
mkdir -p ./runs
evo tree evolve --manifest ./manifest-16.json --run-dir ./runs/run-16
evo tree watch --run-dir ./runs/run-16 --once
evo tree trace --run-dir ./runs/run-16 --out ./run-16.jsonl
```

Promotion is always automatic, so `evolve` runs to the ordinary campaign boundary and prints its result. `watch` shows independent acceptance settings, reuse versus fresh evidence, coverage and cost. The JSON result and status documents show the gate and global comparisons separately. `trace` exports case-level records. A nonpassing or unavailable numerical comparison keeps the Champion and continues within budget; it does not pause for statistics or buy more repeats. The report's full scenario cost and conditional remaining work are different quantities; unknown costs stay unknown. Read its frozen scope, reference and noise assumptions before interpreting a gain.

Point acceptance is recommended for direct fixed-basket comparisons. Negative focus margins permit local regression. A bounded execution-noise allowance reserves deterministic aggregate uncertainty once; causal tolerance permits a separate amount of regression. `execution_noise: observed` is diagnostic, unavailable at R=1, and changes no threshold. Causal tolerance is calibrated only when you choose `measured_noise`. See the [manifest reference](manifest-v2.md#independent-controls-manifest-16) for every mode and its required fields.

### Choose a gate for a noisy agent

Choose `statistical` when you want acceptance to use observed repeat noise. In the complete `/16` manifest, replace `campaign.evaluation` and `campaign.gate` with these values; keep the other required campaign controls and top-level sections:

```json
{
  "evaluation": {"repeats_per_question": 10},
  "gate": {
    "mode": "statistical",
    "stability": "stable",
    "focus": {"margin": 0, "confidence": 0.9},
    "causal": {"tolerance": "measured_noise", "confidence": 0.9},
    "execution_noise": {"kind": "negligible"},
    "verification": "reuse",
    "provenance": {"kind": "reported"},
    "fresh_parent": false,
    "global": "point"
  }
}
```

Each local comparison now uses one fixed stage of 10 repeats, not `/15`'s interim looks. At least two repeats are required; there is no `gate.statistics` block and `global: bound` is refused in this mode. The 20,000-draw `f13-bootstrap/1` method resamples repeats with provenance-based exact pairing or unpaired draws. Focus must pass `L_F - e > margin`; nonempty causal must pass `L_C - e >= -q` here, where e is the execution-noise allowance and q is Parent-measured tolerance. `negligible` is recommended because the bootstrap already measures repeat noise; a bounded allowance still subtracts once. The early global point preflight stays, but there is no local preflight to avoid an unpromising Candidate measurement.

Read `exact_contrast` as a point decision, not a useful-width interval: no within-case contrast variation was observed. It is common at small R, especially R=2, so choose a larger R for informative repeat intervals. `intervals_empirical` means empirical repeat uncertainty, not guaranteed coverage or generalization to unseen tasks. Fewer than two usable observations on either side give `insufficient_repeats`. The Champion stays when a comparison is nonpassing or unavailable.

Choose `causal.tolerance: "measured_noise"` independently in `point`, `statistical` or `confidence`, with R at least 2. It resamples only retained causal Parent observations at fixed level 0.995, never Candidate outcomes, and buys no calibration measurement. Stored Parent calibration is fixed in the binding; fresh Parent calibration is recorded with the decision. Empty C skips it. No Parent variation means q=0 and `noise_not_observed`, not an automatic rejection; positive q carries `noise_allowance_empirical`. Fewer than two usable Parent observations give `insufficient_calibration`. Even if a stored Parent's insufficiency is already known at binding, the Candidate stage still costs its declared calls before the comparison becomes unavailable.

### Choose a gate for small scopes

Use point acceptance for a direct small-basket decision, or explicitly choose `confidence` for its conservative `bounded-weighted-mean/1` bound. That bound's width depends on independent units, their weights and range, not repeat count R or repeat variance. More repeats do not narrow it, constant small gains can fail, and an unchanged small causal scope can fail even with `measured_noise`.

To judge such scopes without requiring an unattainable bound, keep the complete confidence configuration from the [manifest reference](manifest-v2.md#independent-controls-manifest-16) and add `gate.bound_fallback: "point"`. Focus falls back if the bound cannot pass even at its reference-aware ceilings; nonempty causal falls back if an all-zero contrast cannot pass. These tests never inspect Candidate outcomes. Each substituted scope uses the same threshold and strictness with a weaker point rule, visibly labelled `fallback: focus_unattainable` or `fallback: causal_unconfirmable`. The other scope and global rule stay unchanged. A point threshold that is itself impossible still closes in preflight without Candidate dispatch.

With fresh Parent measured noise, causal fallback waits for q in the deciding record; unknown q gives `insufficient_calibration`, never a causal fallback. A separately bound focus fallback remains visible. In the pending report, `calibration will be computed from fresh Parent at decision` is not the stored-Parent failure `Parent calibration unavailable`. These choices are optional: without new gate options, behavior and policy digests stay unchanged. Older `2.0.0b9` rejects the new options and cannot read runs that use them.

### Choose linked baseline evidence

For a linked run, choose `linked_from.baseline: inherit` to reuse compatible scores and available bounded diagnostics. Reports state diagnostic coverage and gaps. Missing diagnostics can leave a verdict-only baseline with no reconstructable failures; `baseline_refresh_after_rounds: 0` may then stall without new calls. Choose `baseline: fresh` in the new linked manifest to pay for a baseline, or explicitly configure positive refresh rounds. No hidden refresh repairs a diagnostic gap. A committed bundle remains usable when the source moves, but historical stdout is not authenticated merely because its scalar scores match.

A new `/16` link admits only a finished family-14 or family-15 run. A `statistical` successor refuses a family-14 predecessor that ever ran `observe` (`lineage_mode_unsupported`); start unlinked or choose a non-statistical mode instead.

## Tell Evolution which oracle values are secret

Use [`secret_oracle_paths`](manifest-v2.md#secretoraclepaths) in a `/16` manifest when your oracle contains both private reference answers and ordinary scorer metadata. For example, if `/reference` holds the answer while `/reference_spec/field` holds the public column name `unit_name`, add:

```json
"secret_oracle_paths": {
  "version": "secret_oracle_paths/1",
  "pointers": ["/reference"]
}
```

Evolution then guards string values of at least eight characters under `/reference` and permits `unit_name` elsewhere. Omit the key to protect every oracle string. `pointers: []` disables oracle leak protection; unprotected answers can reach the Editor and be hard-coded into the skill. Run secrets remain protected. `/15` refuses the key.

| Refusal | What to do |
|---|---|
| `secret_oracle_path_unmatched at secret_oracle_paths.pointers[i]` | Correct or remove the pointer: it must select a string of at least eight characters in a train case. Test-only matches do not count. |
| `record_matches_secret at secret_oracle_paths.pointers[i]` | Remove the run secret from the pointer text. |
| `run_identity_changed` naming `identity.secret_oracle_paths` | Accept the printed authorization with the offered `rebase`, then repeat the same evolve command. Frozen inputs and earlier verdicts stay unchanged. |

An oracle refusal can add `leak` to its `reflection_failed`, `grouping_failed` or seal-refusal `attempt_closed` record in `campaign.jsonl`. For example, `{"case_id": "c3", "oracle_pointer": "/reference"}` locates the matched value without repeating it. The pointer is omitted when it contains a run secret or exceeds 1,024 UTF-8 bytes. `watch` shows only `case c3`, because oracle keys are task data. This evidence never enters the roles' inputs. Release `2.0.0b11` cannot read runs carrying `leak`, `detail` or `identity.secret_oracle_paths`.

## Judge a Candidate with the stable family (manifest /15)

Manifest `/15` creates a family-14 campaign, the stable family that every 2.x release keeps. The scaffold writes it; use it for the stable-family workflow, or choose the explicit `/16` controls above. Family 14 keeps family 13's choice of affected cases and its focus (F) and causal (C) cases, then adds what this section explains: a choice of how a finalist is compared with the Champion and when a round stops, a refresh of the stored baseline, a focus margin, exact comparisons, the Grouper role, a connection check before the baseline, and role visibility. [The `/15` keys](manifest-v2.md#stable-family-keys-manifest-15) are the full reference.

A Candidate that passes the focus and causal checks is a finalist. It is accepted, and can be promoted, only after its final measurement: a fresh measurement over the whole corpus, compared with the Champion under `global`. Family 14 says "final measurement" where family 13 says "cohort".

### Choose the comparison and the round stop

Two required keys decide what a round costs and what it compares:

- `campaign.gate.stability: stable` compares each finalist with the stored Champion measurement, the round's baseline. The final measurement does not remeasure the Champion; a scheduled baseline refresh still does. Use it when the model, the judge and the Target keep the same behavior during the campaign; Evolution does not check this, and every result says so with the caveat `champion_not_contemporaneous`. `unstable` measures the Champion again in every final measurement, at the cost of one more measurement over the whole corpus each time.
- `campaign.round_stop: first_accepted` opens one Attempt at a time and stops the round at the first accepted finalist, so Attempts that are not needed are never opened. It requires `parallel_attempts: 1`. `all_attempts` runs every Attempt of the round and then measures all finalists together.

The scaffold's `stable`, `first_accepted` and `fresh_parent: false` load one tree at a time and add no Champion rollouts to a final measurement. On the scaffold's demo they cost 18 rollouts, where `unstable` costs 24. [The four cells and their cost](manifest-v2.md#the-four-cells-and-their-cost) explains each combination with this example.

Three more keys have recommended values in the scaffold:

- `campaign.baseline_refresh_after_rounds: 3` measures the Champion again over the whole corpus after three rounds closed on the same stored measurement, so a stale baseline does not set the bar for long. `0` turns the refresh off.
- `campaign.gate.focus.margin: 0` is the smallest focus effect that counts. A negative margin can promote an edit that made the focus worse.
- `campaign.gate.fresh_parent: false` compares the Candidate with the stored baseline instead of measuring its Parent again.

When the repeats of a comparison show no variation inside any case, family 14 decides it on the observed point instead of closing the Attempt `insufficient_evidence`, and reports `exact_contrast`. [Exact comparisons](manifest-v2.md#exact-comparisons) explains what that does and does not claim.

### Move a /14 run to /15

Read the family-13 run with `2.0.0b9`, or seed a new unlinked `/15` campaign from its Champion copy; use the [current manifest reference](manifest-v2.md#stable-family-keys-manifest-15), not a schema edit or a new link to family 13.

### Continue an existing current-family link

A family-14 or family-15 run that already records a family-13 predecessor still inspects, verifies, watches, traces and resumes from its own journal. If it owes the baseline import, reopening that predecessor reports `BaselineImportUnavailable` (`baseline_import_unavailable`). Accept the printed authorization to measure a fresh baseline (`fresh_baseline`). Do not remove the recorded link or edit journals. New `/15` links admit only finished family 14; `/16` links admit finished families 14 and 15.

For a new linked `/15` run, choose `baseline: fresh` or keep a positive refresh schedule when inherited failures are verdict-only; scalar-only inherited rows can otherwise leave no reconstructable failure to diagnose. Point `seed_root` at a byte-identical Champion copy outside the predecessor if that predecessor may move: a missing seed is `bad_path at seed_root`, not the authorizable import refusal.

Before any upgrade, finish or stop (Ctrl+C) every family-14 run. A suspended run must first be resumed under the old release, then finished or stopped. Continue its work with a linked 14 → 14 successor. A run that is already caught must go back to the previous release to finish that Attempt.

A run is caught when it was suspended or crashed after an Attempt froze its inputs, and the new release changes how the files the roles see are selected or digested. Its version change can only be accepted with a rebase, and every resume after that rebase fails with exit `1`. Going back to the previous release and accepting the version change there finishes the same Attempt.

There is also no way back from release `2.0.0b3` for some runs. A run into which `2.0.0b3` wrote `changed_file_budget` or `written_byte_budget` cannot be read by `2.0.0b1` or `2.0.0b2`, and neither can a run that declares `verbatim_answers`. Finish or stop such a run with `2.0.0b3` or later.

### Declare canned answers

Declare [`verbatim_answers`](manifest-v2.md#verbatimanswers) when rounds close `oracle_leak` on a reply that your agent must give word for word. An example is a fixed "not supported" message. Evolution guards every oracle string of at least eight characters unless `/16` declares `secret_oracle_paths`, so a Reflector that quotes what the agent answered, or a Candidate that writes the same text, is refused as a leak. The signs are a round that closes `unplanned` with `reflection_failed` and the reason `oracle_leak`, or an Attempt that closes `seal_refused` with the code `oracle_leak`, both on text that is public in your agent anyway.

Add the key to the manifest, with the exact strings as they appear in the `oracle` of your train cases:

```json
"verbatim_answers": {
  "version": "verbatim_answers/1",
  "strings": ["This request is not supported. Please contact support."]
}
```

A string that matches no oracle value is refused `verbatim_answer_unmatched at verbatim_answers.strings[0]` before anything is written, so a typo cannot hide a real answer. Do not declare a reference answer that is private. A declared string reaches the Editor like any other text, and a Candidate may then hard-code it.

On an existing run, the same command is refused `run_identity_changed` and names `identity.verbatim_answers`. The change only needs a rebase. Accept it with the command that the refusal prints, which has this form, and run the same `evo tree evolve` command again:

```bash
evo tree authorize ./demo/run-1 run_identity_changed --digest <digest> --reason "declare the canned answers"
```

The campaign journals `identity_rebased` and uses the new list from then on. Frozen inputs and earlier verdicts do not change. A run that declares `verbatim_answers` cannot be read or resumed by release `2.0.0b1` or `2.0.0b2`, so this change is one-way for that run.

### Read the final, roles, settings and connection blocks

Run the scaffold as in the [Quickstart](../QUICKSTART.md), saving the report as `./demo/result.json`. Each round of a `/15` report carries a `final` list, one entry per final measurement:

```bash
jq -c '.result.rounds[] | {round, outcome, final: [.final[] | {ordinal, champion_side, stored_measurement_id, members: [.members[].measurement_id], next_baseline_measurement_id}]}' ./demo/result.json
jq -c '.result.rounds[].final[].contrasts[] | {attempt_id, accepted, reason, exact, removed_pairs, removed_cases}' ./demo/result.json
```

Expected output:

```text
{"round":1,"outcome":"promoted","final":[{"ordinal":1,"champion_side":"stored","stored_measurement_id":"m-r00-champion","members":["m-r01-final-01-a01"],"next_baseline_measurement_id":"m-r01-final-01-a01"}]}
{"round":2,"outcome":"clean","final":[]}
{"attempt_id":"r01-a01","accepted":true,"reason":"global_point_improved","exact":true,"removed_pairs":0,"removed_cases":0}
```

- Round 1's one final measurement measured the finalist `m-r01-final-01-a01` over the whole corpus. Under `stable` its Champion side is `stored`: the round's baseline `m-r00-champion`, which was not measured again.
- The contrast is `accepted`, so the finalist was promoted, and its final measurement is the next baseline. `exact: true` says that no within-case variation was observed in the three repeats.
- `removed_pairs` and `removed_cases` count what left a stored comparison: the baseline's excluded pairs and cases without a usable observation.
- Round 2 found no failing case, so it has no final measurement: `final` is `[]`.

Three top-level blocks say how the run was configured and connected:

```bash
jq -c '.result.roles, .result.settings' ./demo/result.json
jq -c '.result.connection | {state, attempts, outcome, detail}' ./demo/result.json
```

Expected output:

```text
{"editor":{"kind":"subprocess"},"grouper":{"kind":"fields"},"reflector":{"kind":"subprocess"}}
{"baseline_refresh":{"count":1,"every_rounds":3,"performed":[]},"round_stop":"first_accepted","stability":"stable"}
{"state":"compatible","attempts":1,"outcome":"compatible","detail":null}
```

- `roles` names the kind of the Grouper, the Reflector and the Editor.
- `settings` repeats `stability` and `round_stop`. `baseline_refresh` shows the period, how many rounds have closed on the current stored measurement (`count`) and the refreshes performed. Round 2 closed on the promoted finalist's measurement, so the count is 1. With `baseline_refresh_after_rounds: 0` it reads `"off"`.
- `connection` is the latest connection preflight: one probe, compatible. A failed probe shows its outcome and a `detail` ([when the connection preflight suspends](#when-the-connection-preflight-suspends)).

The [report reference](cli.md#final-measurement-reports-family-14) lists every key.

### Read the family-14 caveats

```bash
jq -c '.result.caveats[] | [.kind, .round, .attempt_id]' ./demo/result.json
```

For the scaffold it prints:

```text
["champion_not_contemporaneous",null,null]
["focus_selection_bias",1,"r01-a01"]
["intervals_empirical",1,"r01-a01"]
["exact_contrast",1,"r01-a01"]
```

None of these changes a score, a decision or the exit code. They tell you how much to trust a decision:

- `champion_not_contemporaneous` belongs to the whole campaign and appears whenever it ran `stable`: the Champion side of each comparison was measured earlier than the finalist, so drift between the two moments is not controlled.
- `stored_pairs_excluded` names a finalist whose stored comparison left out pairs that the baseline had excluded, with their `count`. The comparison ran on fewer cases than the corpus has.
- `exact_contrast` means that a comparison had no within-case variation in its repeats and was decided on its point. It does not mean that the agent is deterministic; more repeats may show variation.
- `noise_not_observed` means that the Parent showed no variation, so the measured causal tolerance is exactly 0.
- `grouping_fallback` names a round whose Grouper fell back. With `reason: turns_exhausted`, an `agent_cli` Grouper's answer used more than its `max_turns`; raise the limit or simplify the prompt.

`focus_selection_bias` and `intervals_empirical` keep their family-13 meaning ([family-13 caveats](#read-the-family-13-caveats)).

### When the connection preflight suspends

Before the first rollout, and again after each accepted condition change, the campaign runs one probe: the Champion on the first train case, through your Target and your scorer. It checks the fields that `scorer.metric_contract` declares and the primary metric ([`scorer.metric_contract`](manifest-v2.md#scorermetriccontract)). A failed probe measures nothing and suspends the campaign with one of three reasons:

| Reason | Meaning | What to do |
|---|---|---|
| `connection_incompatible` | The Target's events or the scorer's output do not match the contract, or the instance's `describe()` result holds the value of a run secret (detail code `connection_description_secret`). | Fix the Target or the scorer, or the contract if it is wrong. For a secret in the description, remove the value from `describe()` and run the same command again. |
| `connection_preflight_unavailable` | The probe could not reach a conclusion, for example because the Target or the scorer failed. | Repair the named cause. |
| `connection_preflight_inconclusive` | The probe's rollout ended without a scoreable terminal: `turn_budget`, `time_budget` or `candidate_failed`. | Check the budgets and the agent on that case. |

For example, change the scaffold's `pointer` to `/text`, which the reference target does not fill, save the manifest as `demo/manifest-badcontract.json` and run it with `--run-dir demo/run-bad`, saving the report as `demo/result-bad.json`. The run prints:

```text
evo tree evolve: suspended: connection_incompatible — the connection probe found the Target or Scorer incompatible: connection_field_missing at scorer.metric_contract.target_fields[0] (observed events[event=assistant_message,last]/text); fix the Target or the Scorer, then run the same command (a changed Scorer or metric_contract is a comparable change: accept it with evo tree authorize)
evo tree evolve: continue: evo tree evolve --manifest demo/manifest-badcontract.json --run-dir demo/run-bad
```

A description that holds a secret is one more case of `connection_incompatible`. The probe creates its instance, tests the instance's `description` with the same check the journal applies to every record, and stops before any rollout if the description holds the value of a name the run forwards (`scorer.allowed_env`, a role's `allowed_env`, `api_key_env` or `proxy_env`). The detail then has `code: connection_description_secret`, `manifest_path` naming where the manifest declares the matched name (for example `scorer.allowed_env.JUDGE_MODEL`) and `observed_path: target.description`. It never holds the value. Repair it in the Target: remove the value from `describe()`, for example report a hash of the model id, and run the same command again. Removing the name from `allowed_env` instead changes the Scorer's identity and needs `evo tree authorize`.

A Candidate can describe itself differently from the Champion, so the same check runs on every instance when it is created, before any row runs on it. A match there ends the invocation as `persistence_failed` with owner `campaign_journal` and code `record_matches_secret`, exit `1`; stderr says `evo tree evolve: instance_attested.description.<path> holds the value of <manifest_path>; the measurement journal is intact; remove the value from the target's describe() and run the same command again`. No `instance_attested` record is written for that instance; the measurement journal still records its creation and release. After you repair `describe()`, the same command resumes and finishes.

The other two reasons print `the connection probe could not reach a conclusion (<cause>); repair the cause and run the same command` and `the connection probe's rollout ended without a scoreable terminal (<cause>); repair the cause and run the same command`. The status shows the same facts in its `connection` block, with `state: suspended`:

```bash
jq -c '.status.connection | {state, outcome, detail}' ./demo/result-bad.json
```

```text
{"state":"suspended","outcome":"connection_incompatible","detail":{"code":"connection_field_missing","manifest_path":"scorer.metric_contract.target_fields[0]","metric":"score","observed_path":"events[event=assistant_message,last]/text"}}
```

Repair the Target or the scorer, then run the same command with the same run directory; the resume probes again. If the contract itself was wrong, correct it in the manifest. That is a comparable change, so the next run is refused `run_identity_changed` with `differs: identity.metric_contract`, and accepting it with `rebaseline` closes the open round as `condition_changed` ([change the provider mid-campaign](#change-the-provider-mid-campaign)). The detail never holds an observed value; the owner-only trace under `connection-logs/` holds the expected and observed types.

Two rare cases look odd in the audit but change nothing else:

- If storage fails exactly while the first rollout after a preflight is being recorded, and the other side of a joint stage has already started, the resume runs the preflight again and the audit reports that the binding's executed starts carry no admission. The audit finding records that storage fault; the run's evidence is otherwise unchanged.
- If the campaign faults right after a preflight has started, a second preflight may run before the fault is reported. This costs Target work only and never starts a second rollout.

### When the Grouper suspends

A `chat` or `agent_cli` Grouper suspends the campaign when its provider fails or when it fails for a reason the campaign cannot explain, and also after two full fallbacks in a row ([when the Grouper fails](manifest-v2.md#when-the-grouper-fails)):

```text
evo tree evolve: suspended: grouper_provider_failed — the Grouper's provider failed after retries
evo tree evolve: suspended: grouper_failing — the Grouper failed for an unexplained reason or fell back for every failure twice in a row
```

For `grouper_provider_failed`, the [provider-attempt line](#read-provider-attempt-lines) after the suspension shows what the provider answered; check the Grouper's key, `base_url` and network path. For `grouper_failing`, read `reflector-logs/grouping-<operation>.jsonl`, which names the class of an unexpected exception, and check the prompt, `max_turns` and the fallback. Then run the same command with the same run directory. The campaign groups again on the same baseline; no authorization is needed. If you accept a changed condition while that suspension is still owed, the next invocation applies the decision, writes the owed suspension and exits `1` again; run the same command once more.

### A /15 smoke campaign

The scaffold is itself the smoke campaign for `/15`: it runs offline, with the recommended values. Run it as in the [Quickstart](../QUICKSTART.md), then check the rounds, the Champion and the alpha:

```bash
jq -c '[.result.rounds[] | {round, outcome}]' ./demo/result.json
jq -c '.result.final_champion | {version, measurement_id}' ./demo/result.json
jq -c '.result.alpha' ./demo/result.json
```

Expected output:

```text
[{"round":1,"outcome":"promoted"},{"round":2,"outcome":"clean"}]
{"version":"v1","measurement_id":"m-r01-final-01-a01"}
null
```

Round 1 promotes the sample edit through its final measurement, and round 2 starts from that measurement, which fails no case, so it closes `clean`. A `/15` campaign spends no alpha. The commands in [Read the final, roles, settings and connection blocks](#read-the-final-roles-settings-and-connection-blocks) and [Read the family-14 caveats](#read-the-family-14-caveats) print the values shown there for this run.

This proves the wiring: admission, the connection preflight, grouping, case selection, the Candidate gate, the final measurement and the Promotion. It does not prove that the Promotion is a real improvement: two cases cannot, and the sample scorer rewards a marker. Do not use this run to choose a tree to deploy.

## Judge a Candidate with explicit confidence (manifest /14)

Read historical family-13 runs with `2.0.0b9`; current campaigns use the [stable `/15` controls](#judge-a-candidate-with-the-stable-family-manifest-15) or [independent `/16` controls](#choose-independent-acceptance-manifest-16).

## Read stable-family confidence

The focus cases (F) are the round's exam cases that the edit affects. The causal cases (C) are the other affected train cases. A Candidate must exceed the focus margin on F and must not lose more than the causal tolerance on C. Passing the local gate makes a finalist, not a Promotion; the [final measurement](#read-the-final-roles-settings-and-connection-blocks) still decides acceptance.

### Choose `statistical` or `observe`

`campaign.gate.mode` is required and has no default:

- Use `statistical` for numerical evidence under `/15`'s empirical confidence assumptions. It needs at least two repeats per case. No observed within-case variation uses the exact point contrast; a Parent with no variation gives a measured allowance of zero and `noise_not_observed`. A numeric `causal.tolerance` instead declares an absolute allowance explicitly.
- Use `observe` only to check the pipeline. It records point values and enforces no numerical gate, so it can promote a worse Candidate. Set `global: off`, omit both confidence fields and keep the required margin, tolerance, stability and round controls. Do not use it to choose a tree to deploy.

A `statistical` campaign cannot link from an `observe` one: the successor is refused with `lineage_mode_unsupported at linked_from`, because an observe Champion is not quality evidence. An `observe` campaign can follow either mode.

### Move a `/13` manifest to `/14`

Read removed-family runs with `2.0.0b9`; seed a new unlinked campaign using the [current `/15` reference](manifest-v2.md#stable-family-keys-manifest-15), not another retired schema.

### Read the `confidence` block

Every Attempt in `result.rounds[].attempts[]` carries a `confidence` block. It is `null` until the Attempt's comparison is recorded. Read its summary and its looks from the saved report:

```bash
jq -c '.result.rounds[].attempts[].confidence | {mode, focus_cases, causal_cases, calibration}' ./my-experiment/run-1-result.json
jq -c '.result.rounds[].attempts[].confidence.evaluated[] | {look, decision, reason, would_pass_point}' ./my-experiment/run-1-result.json
```

For the scaffold's `/15` smoke campaign, it prints:

```text
{"mode":"statistical","focus_cases":2,"causal_cases":0,"calibration":null}
{"look":3,"decision":"finalist","reason":"gate_passed","would_pass_point":null}
```

- `focus_cases` and `causal_cases` are the sizes of F and C. Here the edit affects both sample cases and both are in the focus, so C is empty.
- `calibration` is the measured noise allowance. It is `null` here because there are no causal cases to calibrate on.
- `evaluated` lists every look in order. A `statistical` campaign looks after 3, 5 and 10 repeats when each is below `repeats_per_question`, and then at that number. An `observe` campaign looks once.
- `decision` and `reason` are what the gate decided. An `observe` look is `finalist` with `observe_finalist` unless a critical failure rejects it; it has point estimates and no bounds.
- `would_pass_point` is what the point values would have decided. It is `null` in `statistical`.

In a `statistical` look, `focus`, `causal` and `overall` carry `point`, `lower` and `upper`. The [report reference](cli.md#confidence-and-cohort-reports-family-13) lists every key and reason.

### Read the `cohort` block

Read historical cohorts with `2.0.0b9`; current `/15` reports use the [`final` list](#read-the-final-roles-settings-and-connection-blocks), not `cohort`.

### Read the family-13 caveats

Read historical family-13 reports with `2.0.0b9`; current `/15` campaigns retain the shared method caveats below and add the [family-14 caveats](#read-the-family-14-caveats).

### Shared method caveats in /15

Method caveats appear in the top-level `caveats` list. An `observe` campaign also has two campaign items, naming no round and no Attempt. None changes a score, decision or exit code.

```bash
jq -c '.result.caveats[] | {kind, round, attempt_id}' ./my-experiment/run-1-result.json
```

- `observe_gate_not_enforced` and `uncalibrated_point_estimates` mean that no numerical gate decided the Promotion. An `observe` campaign lists them once for the whole campaign, with `round` and `attempt_id` set to `null`, even when no Attempt evaluated a look. They also appear on every `observe` Attempt that evaluated a look.
- `intervals_empirical` appears on every `statistical` look: the bounds come from a bootstrap, so the confidence is not a proven guarantee.
- `noise_allowance_empirical` means the tolerance was estimated from the Parent's repeat noise (`measured_noise`). `noise_not_observed` means no Parent variation was observed and the allowance is zero. `noise_allowance_unavailable` means the estimate could not be made.
- `causal_parent_not_contemporaneous` appears with `fresh_parent: false` when C is not empty: C was compared with stored baseline rows, not with a Parent measured at the same time.

`focus_selection_bias` appears with `fresh_parent: false`, whatever `global` is: the focus was selected from the stored evidence used as its reference. `provenance_drift` names finalist final-measurement cases outside the local measured set that used a changed part. `components_changed_by_condition` names sides whose map changed after accepted condition drift. Current campaigns never report `completion_regression`; full-train evidence precedes Promotion. See the [current report fields](cli.md#final-measurement-reports-family-14).

### When the cohort suspends

Read historical suspensions with `2.0.0b9`; current `/15` gaps use `final_incomplete`, repaired by the [ordinary resume](#resume-after-a-failure).

### After a `retained` round

Each round works on a portion of failing cases in a visitation cycle. A `retained` round keeps the Champion and carries visited cases by id, so the next round continues rather than starting over. Under `/15` `stable`, the stored baseline stays; `unstable` uses the latest fresh Champion measurement.

### An observe smoke campaign

Read historical `/14` smoke runs with `2.0.0b9`; use the [current `/15` smoke campaign](#a-15-smoke-campaign) or the [current observe settings](#choose-statistical-or-observe) for new checks.

## A run failed: start here

If you started the command without a terminal, first read the newest `logs/evolve-*.log` in its run directory (`logs/evaluate-*.log` for standalone evaluation). It keeps the invocation's stdout, stderr and any unhandled traceback. A refusal or a crash before admission leaves no log; start with the launcher's stderr in that case.

Use these steps for `evo tree evolve`. Start with the terminal where you ran it: stderr contains the failure explanation and, for a suspension, the continuation command. If you saved stdout as a JSON report, read `terminal`, `code`, `owner` and `suspended`. A refused invocation exits `2` and explains the refusal on stderr without writing a JSON result. See [Read the outcome](#read-the-outcome) for the other exit codes.

First, inspect the saved run without starting it again:

```bash
evo tree watch --run-dir ./demo/run-1 --once
```

Replace `./demo/run-1` with the directory passed to the failed command's `--run-dir`. The command prints a snapshot of the recorded stage, incomplete measurements and current status, where available. It does not resume the run or contact the Target. A successful snapshot exits `0` even when the campaign it describes is suspended; read the snapshot itself. If the launch failed before creating a journal, start with stderr instead. For a standalone `evo tree evaluate`, use its separate run directory with the same watch command.

Use the reported component (`owner`) and reason (`code`) to choose the next file. Paths in this table are relative to the campaign's run directory; a directory may be absent if that stage never started.

| What you need to understand | First place to look |
|---|---|
| The invocation's own output, a crash traceback or a fatal signal | `logs/` |
| The last recorded stage, decision or suspension | `campaign.jsonl` |
| What happened while a case was running | `measurements/*/events.jsonl` |
| A scorer failure or missing metric | `scorer-traces/`, read with [`read_scorer_trace`](#read-a-scorer-trace-from-python) |
| An Editor failure | `editor-logs/` |
| A Reflector failure | `reflector-logs/` |
| A Grouper failure | `reflector-logs/grouping-<operation>.jsonl` |
| A connection preflight failure | `connection-logs/` |
| A Target or its provider failing | The separately started Target Service's terminal or service logs, plus the measurement events |

[Run storage layout](run-storage-layout.md#the-v2-run-directory) describes the files. For a verified check of saved campaign evidence, use [Inspect a run without running it again](#inspect-a-run-without-running-it-again). To inspect case inputs, replies and scores, use [trace export](#export-what-each-case-did); a failure before scoring may have no stored message to export.

If the process disappeared without a final report, read the process log's tail. A normal command exit leaves an `exit` line; a fatal signal or default `SIGHUP` can leave a stack dump without that line. A missing closing line is not proof of a crash: the log may have disabled itself while the process continued. Journal records newer than its last line show that recording stopped first. The process id in the file name helps match a kill, out-of-memory event or host restart in container, service or operating-system logs (`journalctl` or `dmesg`, where available). The last journal entry shows recorded work, not necessarily the cause of death. Preserve the evidence; do not edit journals to make the run continue.

For `suspended`, repair the named cause and follow [Resume after a failure](#resume-after-a-failure) with the same manifest and run directory. For `refused`, follow [Handle a refusal](#handle-a-refusal). Do not authorize changes merely to clear an error: authorization accepts the specific changes the refusal reports.

When asking a colleague for help, include the installed Evolution version, the command, the exit code if available, the last stderr lines and the watch snapshot. Add a small relevant log excerpt with the measurement or operation id. Remove credentials and private task data before sharing; scorer traces can contain unredacted data. You do not need to send the whole run directory.

## Inspect a run without running it again

Print a quick status snapshot without contacting Target or resuming the run:

```bash
evo tree watch --run-dir ./demo/run-1 --once
```

This prints the run's current state and exits `0`. For verified inspection of the stored campaign evidence, use the read-only Python API:

```bash
python - <<'PY'
import json
from pathlib import Path
from evolution.tree import inspect_campaign

report = inspect_campaign(Path("demo/run-1"), verify=True).to_payload()
print(json.dumps(report, indent=2))
PY
```

Replace `demo/run-1` with your run directory. `verify=True` checks the stored evidence; inspect `verification.mismatches` for inconsistencies. Inspection does not call the target, resume the campaign or repair any files. An empty mismatch list checks recorded evidence, not whether your scorer's definition of quality is appropriate.

For diagnosis, start with `campaign.jsonl` for decisions, `measurements/*/events.jsonl` for interactions, and the corresponding `scorer-traces/`, `editor-logs/` or `reflector-logs/` directory. [Run storage layout](run-storage-layout.md#the-v2-run-directory) describes each file. Share a small relevant excerpt with colleagues rather than copying a full log that may contain task data.

## Watch a running campaign

To see progress in the terminal that starts the campaign, add `--progress`:

```bash
evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-1 --progress
```

Start the scaffold's Target Service first, as in the [Quickstart](../QUICKSTART.md). For a new scaffold run, the first progress lines name the created campaign and its baseline measurement; later lines show scored-pair counts, rounds, Attempts and decisions. A resumed run first shows a one-line snapshot, including any previous suspension. Progress goes to stderr and the usual final JSON report stays on stdout. The flag is off by default and changes no evidence, role input or exit code. Standalone evaluation also accepts `--progress`.

To follow the same run from a second terminal, use:

```bash
evo tree watch --run-dir ./demo/run-1
```

The first lines give a snapshot: the campaign's creation, current round and stage, open Attempts, incomplete measurements, last decision, suspension count and current status, where available. Only records appended after the watch starts produce event lines. If the directory already exists but has no journal yet, the first line says it is waiting for the campaign journal. An absent directory is refused.

A completed campaign or evaluation ends the watch with exit `0`; a new suspension or infrastructure ending uses `1`; a stopped campaign uses `3`; exhausted campaign budget uses `4`. A refused watch exits `2`, an unreadable or rewritten journal exits `1`, and Ctrl-C stops only the watch with `130`. A run finished before you start returns its ending's code after the snapshot. A run suspended before you start keeps being followed until it resumes and reaches a new ending. After a suspension, repair the cause and run the same `evolve` or `evaluate` command; start another watch to follow it.

Measurement counts appear at most once per measurement every 10 seconds, plus at completion. After a minute without a record, idle lines reassure you that the watch is still waiting; they recur every five minutes. These lines contain ids, counts and codes, with no case content or other task data. Their wording is not a parsing contract. See the [watch reference](cli.md#evo-tree-watch) for the arguments and endings.

In `/16`, a failed reflection can show a value-free `detail`, such as `unknown_evidence_id at groups[0].evidence_ids[0]`. An oracle refusal shows only `case <case_id>`; read its `leak.oracle_pointer` in `campaign.jsonl`. A failed Editor can show `last protocol error: plan_path_not_writable (editor-logs/<attempt_id>.jsonl)`. Read that private file for the full message. If no valid closed category can be read, the line shows only `log editor-logs/<attempt_id>.jsonl`. Neither protocol text nor oracle pointers appear in `watch` or `--progress`.

## Export what each case did

Export a finished or in-flight run to a new file outside its run directory:

```bash
evo tree trace --run-dir ./demo/run-1 --out ./demo/run-1-trace.jsonl \
  --manifest ./demo/manifest.json
```

The command creates an owner-only file with a header and one JSON line per try whose rollout has ended, including retries, repairs and imported rows. It prints summary counts on stderr. In-flight rollouts are counted but omitted; unfinished scoring is labelled `unsettled`. Nothing is written into the run directory and no Target call is made. For an evaluation, use its own run directory and evaluation manifest.

Use `--manifest` with a full `/15` or `/16` campaign manifest, or the evaluation manifest for an evaluation. It checks the corpus digest and applies the run kind's secret redaction from your current environment. `/15` selects the train corpus for request authentication. Known limitation: `/16` trace currently uses the reflection corpus dispatch rather than that train-selection path; do not assume authentication with held-out rows succeeds. Read the actual corpus/request labels. If you no longer have the manifest or corpus, you can still export to a different new file:

```bash
evo tree trace --run-dir ./demo/run-1 --out ./demo/run-1-without-manifest.jsonl
```

Read `request.check` on each try. `authenticated` means the scorer request agrees with the journaled input digest and verified corpus; `unauthenticated` means only its own hash was checked, without a corpus. `mismatch` suppresses the public input, turns and oracle. `corpus` means input came directly from the verified corpus because no scorer trace was available; a null request means it was unavailable. A failure before scoring has no stored message or agent events to export.

Read `scoring.output.check` separately. `unbound` means stdout metrics agree with the journal, but verdicts, feedback, details and diagnostics are not bound by it. `metrics_differ` warns that the stdout metrics differ. These labels do not verify the tree on disk; use the [verified inspection](#inspect-a-run-without-running-it-again) for that. The [trace reference](cli.md#evo-tree-trace) lists every record key, measurement filtering, string caps and refusal cases.

Keep the file private. It can contain oracle, private case data, messages and scorer details even after secret redaction. Without `--manifest`, the header says `redaction: none`. With a manifest, unset secret variables cannot be redacted, and the summary counts those variables. Scorer traces in the run directory are currently unredacted; export redaction does not sanitize the originals.

### Read a scorer trace from Python

To read one try's scorer request and output in your own tool, use `evolution.tree.read_scorer_trace` or `ScorerTraceReader`. [Run storage layout](run-storage-layout.md#the-v2-run-directory) shows the bounded 32 MiB example, explains strict path handling and describes empty and missing streams. The bounded options introduced in `2.0.0b6` retain their defaults of unlimited reads and resolved paths. From `2.0.0b7`, new traces use one pack per measurement. Use the public reader for both layouts instead of opening files under `scorer-traces/` by path.

## Move, copy or delete a run directory

This describes supported family-14/15 run directories. Removed-family evidence must be read with `2.0.0b9`.

Evolution freezes every published tree copy in a run directory: the Candidate snapshots, the Champion versions under `artifacts/champions/vN` and the frozen Parent. It removes the write bits from every file and directory inside such a copy. Later steps verify these copies again by digest, so a change to one becomes visible. A frozen file keeps the read bits of its source, so a seed file with mode `0644` becomes `-r--r--r--` and one with mode `0600` becomes `-r--------`. The read bits for other users expose nothing, because the run directory and its `artifacts/` directory are `drwx------`.

Directories inside a frozen copy are `dr-x------`, so a plain `rm -rf` fails with "Permission denied", even for the owner. For the Quickstart's run:

```bash
rm -rf ./demo/run-1
```

```text
rm: cannot remove './demo/run-1/artifacts/champions/v0/uv.lock': Permission denied
rm: cannot remove './demo/run-1/artifacts/champions/v0/skills/SKILL.md': Permission denied
rm: cannot remove './demo/run-1/artifacts/champions/v1/uv.lock': Permission denied
rm: cannot remove './demo/run-1/artifacts/champions/v1/skills/SKILL.md': Permission denied
rm: cannot remove './demo/run-1/artifacts/candidates/r01-a01/uv.lock': Permission denied
rm: cannot remove './demo/run-1/artifacts/candidates/r01-a01/skills/SKILL.md': Permission denied
```

It exits `1` after removing everything else, journals included, and leaves the frozen copies behind. To delete a run directory, give the owner write permission back first, then delete it:

```bash
chmod -R u+w ./demo/run-1 && rm -rf ./demo/run-1
```

Moving or renaming the whole run directory on the same filesystem needs nothing extra, for example `mv ./demo/run-1 ./archive/run-1`. Across filesystems `mv` copies and then fails with "Permission denied" when it deletes the read-only source; use `cp -a`, then delete the source as above. A copy made with `cp -a` keeps the read-only modes, so delete a copy the same way. A run you will resume must stay under the Target's allowed root.

Do not change, add or remove evidence inside a run directory that you still intend to resume, inspect with `verify=True`, evaluate or link from. The audit then reports mismatches, and such a run cannot be repaired by hand. Process logs under `logs/` are the exception: readers never use them as evidence, so you may delete those files after their invocation ends.

## Keep a run alive after the terminal closes

Start the Target Service first and keep it running separately, as in the [Quickstart](../QUICKSTART.md). To start a campaign that should outlive this terminal, use `nohup`:

```bash
nohup evo tree evolve --manifest ./demo/manifest.json --run-dir ./demo/run-2 > run.out 2>&1 &
```

The example uses a new run directory, `./demo/run-2`, because `./demo/run-1` from the Quickstart has already finished, and reopening a finished run writes no new log. The shell prints a job id and process id. The final JSON report and terminal diagnostics go to `run.out`; the invocation also creates its private `./demo/run-2/logs/evolve-*.log` after admission. With the packaged demo, expect a completed campaign and a closing `exit 0` line. You can also use `setsid` or a service manager; arrange for the Target Service to outlive the terminal too.

Evolution keeps the default action of `SIGHUP`: an attached process still ends when that signal arrives. Once its log exists, an Evolution-owned fault handler leaves a stack dump there; an operator-enabled handler keeps its own destination. `nohup` ignores that signal, and Evolution leaves that disposition alone. A closed stdout or stderr pipe is handled separately: it no longer ends a logged invocation, and later output remains in the log.

## Resume after a failure

A current campaign that encounters infrastructure trouble can print:

```text
evo tree evolve: suspended: target_unstable — ...
evo tree evolve: continue: evo tree evolve --manifest ... --run-dir ...
```

Repair the named cause, keep the manifest and inputs unchanged, then rerun the printed command. You need no new run directory or authorization merely because it suspended. Removed-family runs cannot resume here: read them with `2.0.0b9`, or seed a new unlinked `/15` campaign from the Champion copy. The journal tells Evolution where to continue; faulted samples receive a bounded repair try.

Suppose you stopped a current campaign with Ctrl-C, and the process then died before writing its stopped record. The Champion measurement, such as `m-r00-champion`, can be left with unscored pairs. The next identical `evolve` finishes the stage in progress, then suspends `champion_incomplete`, listing unfinished pairs and their owner. Run the same command again: each listed pair gets one more try and the campaign continues.

A stop can also leave a Champion measurement unfinished without a kill. If the stage that stopped also decided a fault or threshold suspension, the run reports that suspension first. Repair the cause it names and run the same command. A later resume then lists the unfinished pairs as `champion_incomplete`, and the next plain resume tries each one.

A campaign also suspends when too many tries of one component ended without a usable result: the reasons `target_unstable`, `environment_unstable`, `scorer_unstable` and `rollout_time_budget_exhausted`. Each case/sample pair has a limited number of retries. When you resume such a campaign, it first runs one more try of each case/sample pair whose retries that component used up. If you repaired the component, those pairs are measured. If it is still broken, the campaign suspends again. If the Evolution process itself dies in the middle of a measurement (killed, out of memory, host restart), every try that was open at that moment counts as an indeterminate try of the `rollout` component, including tries that were being scored. Several such tries can cross the `rollout` threshold, and the campaign then suspends with `rollout_time_budget_exhausted` even though no time budget ran out. Resume with the same `evo tree evolve` command; you do not need `evo tree authorize`. A campaign suspended under `0.3.0a23` or earlier is resumed by `0.3.0a24` only after you accept the version change. The refusal is `run_identity_changed`, and its `differs:` line names `producing_version`. Run the `evo tree authorize` command that refusal prints, with its own digest, as [Handle a refusal](#handle-a-refusal) describes, then rerun the same `evo tree evolve`.

When a threshold is crossed while an Attempt's samples failed, the campaign suspends at the round barrier and the Attempt stays open. When you resume, the failed samples of that Attempt get one repair try each, as the champion's do. The Attempt is then judged on the repaired evidence. From `0.3.0a27` this also holds when the campaign suspends for another reason first, for example when a later Attempt's Editor fails. A scorer fix over a baseline that was already completed changes a comparison condition, so it needs `evo tree authorize` with `rebaseline`.

Use `owner` and `code` in the result to find the component to inspect:

| Cause | First place to look |
|---|---|
| `target_unstable` | Target logs, provider availability and call failure codes |
| `target_contract_violated` | Backend reply shape, sessions and seed handling |
| `target_changed` | The target's loaded files and declared identity |
| `target_components_changed` | The Target's component map for the tree: restore the rendering, or deploy the change with a new attested identity and accept `continue` ([when the Target renders another component map](#when-the-target-renders-another-component-map)) |
| `scorer_metric_missing` | Scorer output and the configured primary metric |
| `editor_unresponsive`, `reflector_unresponsive` | Role logs, provider response times and declared deadlines |
| `persistence_failed` with owner `campaign_journal` and code `record_matches_secret` | The Target's `describe()` result, not the disk: an instance's description holds the value of a run secret; remove the value and run the same command again ([when the connection preflight suspends](#when-the-connection-preflight-suspends)) |
| `storage_capacity_exhausted`, `persistence_failed` | Free space, free inodes, permissions and filesystem health ([when the disk or its inodes run out](#when-the-disk-or-its-inodes-run-out)) |
| `reflector_provider_failed`, `editor_provider_failed` | The role's own provider — credentials, base URL, network path |
| `champion_incomplete` | The owner the report names for the listed pairs; the stop was cut before the Champion measurement finished, so repair that owner (if any) and rerun the same command |
| `reflector_failing`, `editor_failing` | Role logs for an unexplained failure, or a Reflector prompt/`max_turns` too small for the corpus |
| `final_incomplete` | The owner the report names for the listed pairs of a `/15` final measurement; repair it and rerun the same command, as for `cohort_incomplete` ([when the cohort suspends](#when-the-cohort-suspends)) |
| `grouper_provider_failed`, `grouper_failing` | The Grouper's provider, credential, prompt or `max_turns` ([when the Grouper suspends](#when-the-grouper-suspends)) |
| `connection_incompatible`, `connection_preflight_unavailable`, `connection_preflight_inconclusive` | The Target's events, the scorer's output, `scorer.metric_contract` and a secret in the Target's `describe()` result ([when the connection preflight suspends](#when-the-connection-preflight-suspends)) |

Before any upgrade, finish or stop (Ctrl+C) every family-14 run. A suspended run must first be resumed under the old release, then finished or stopped. Continue its work with a linked 14 → 14 successor. A run that is already caught must go back to the previous release to finish that Attempt. A run is caught when it was suspended or crashed after an Attempt froze its inputs, and the new release changes how the files the roles see are selected or digested; every resume after the accepted version rebase then fails with exit `1` ([move a `/14` run to `/15`](#move-a-14-run-to-15)).

The rule has no way back for two more cases. A run into which release `2.0.0b3` wrote `changed_file_budget` or `written_byte_budget`, or that declares `verbatim_answers`, cannot be resumed by `2.0.0b1` or `2.0.0b2`.

An ordinary role failure is not always a suspension; read the reported campaign outcome rather than treating every failed edit as a broken run. A repair that changes a bound program or comparison condition may trigger a refusal on resume. Follow the next section in that case.

### When the disk or its inodes run out

An inode is a filesystem entry for a file or directory. From `2.0.0b7`, new scorer traces use packs and inode estimates are advisory (#211). Earlier b5/b6 releases write directory traces and stop a low one-round campaign forecast with `storage_capacity_exhausted` / `storage_inodes_insufficient`, or refuse a low remaining-evaluation forecast with exit `2`. Historical inode-capacity records stay readable.

At campaign start/resume and evaluation creation/incomplete resume, `2.0.0b7` compares free inodes with its estimate for requested next work. A low forecast prints one warning immediately, before store writes, initial tree copy or new dispatch, including with `--progress`. It states free and estimated counts and continues requested work without an acknowledgement or override. Scripts should surface the warning and handle actual storage faults; they must no longer rely on forecast-only `storage_inodes_insufficient`, a capacity terminal or evaluation exit `2` as a pre-measurement stop. Human warning prose is not a machine API. Library callers receive no new result field or typed warning API; an absent sink or ordinary exception from the inode-warning sink does not block work.

The estimate is neither a reservation nor an exact footprint or completion guarantee. Tree growth, other writers and the [known accounting limits](#storage-forecast-limits) can exhaust storage later. A filesystem reporting no fixed inode limit produces no shortage warning. When only the whole-run forecast is low, the campaign keeps its existing rounds-coverage note; it does not also print the next-work warning. Completed evaluations skip estimation. Byte admission, actual write failures, unsafe-entry and identity checks, privacy and measurement/promotion gates retain their behavior.

To make room, remove run directories you no longer need (`chmod -R u+w` first), or [move the run](#move-copy-or-delete-a-run-directory) to a filesystem with more capacity. Continuing despite a forecast can fail Evolution or an external scorer, Target or role program. External failures keep their existing scoring, retry or suspension classifications. Calls may incur cost before a real write fails, and recovery can repeat some paid work.

When an Evolution-owned scorer-trace write fails, the campaign returns resumable `storage_capacity_exhausted` for full storage or `persistence_failed` for another storage fault, with a stderr line naming the pack. A standalone evaluation prints the pack and `journal unavailable` (exit `1`). Repair the storage cause and repeat the same command. An interrupted try is closed and its sample is scored again under a fresh id; it is never turned into a quality score by the storage fault.

A normal packed trace uses `scorer-traces/<measurement_id>.pack`. The request is durable before spawn and output before the scoring terminal. A timeout keeps output; an Evolution crash during scoring loses partial in-flight output. A torn final record can be truncated on adoption. A corrupt segment is preserved, and new records can use `.s1.pack`, `.s2.pack` and so on, with a stderr/process-log diagnostic. Resume can still require the damaged evidence: restore it from a trusted copy first. If resume permits continuation, keep the corrupt segment so intact evidence and the segment chain stay available. If required evidence cannot be restored, start a new campaign. Deleting a segment is not a repair. Use the [public reader](run-storage-layout.md#the-v2-run-directory) for either layout; old directory traces stay in place.

### When the campaign suspends on a role failure

Current campaigns classify every settled Reflector and Editor failure and let only a failure proven to be about diagnosis or edit content close a round or Attempt. Everything else — the role's own provider down, or an unexplained failure — suspends like a target outage rather than silently consuming rounds or Attempts:

- `reflector_provider_failed` / `editor_provider_failed`: the role's `retry` policy (`reflector.kind: chat` / `editor.kind: chat`) exhausted its attempts or budget, or hit a rejected credential. The [provider-attempt line](#read-provider-attempt-lines) after the suspension shows what the provider answered. Fix the credential, endpoint or network path, then resume with the same command; the round the failure interrupted is not closed and reflection or editing runs again.
- `reflector_failing` / `editor_failing`: the role failed for a reason the campaign cannot attribute to content — a timeout, an agent-CLI crash, a protocol collision — or, for the Reflector only, two consecutive **skipped** rounds in a row (below). Read `reflector-logs/` or `editor-logs/` for the round; a misconfigured prompt or too small a `max_turns` for your corpus is the usual cause.

In every case the repair is the same: fix the cause, keep the manifest and run directory unchanged, and run the printed `evo tree evolve` command again. No authorization and no new baseline are needed for these four reasons alone.

**The skip rule.** Only a Reflector failure classified `content` closes a round as `unplanned/reflection_failed`. `reflector.max_consecutive_failures` consecutive content failures on the *same* error group make that round a **skip**: `round_closed.visitation` is `"skipped"`, and the campaign advances past the group. The group stays eligible in the next cycle, with a fresh count. A second skip with only content failures and no campaign suspension since the previous skip suspends `reflector_failing`; any other settled reflection or suspension breaks the chain. Provider and unknown failures do not count toward this threshold: they suspend immediately. See [Reflector and Editor resilience](manifest-v2.md#reflector-and-editor-resilience-family-10) for the full tables, recovery distinctions and worked example.

### Read provider-attempt lines

A chat role's provider can fail and then answer on a retry, so a failure can be hidden by the retry that followed it. When one role invocation, that is one Editor Attempt, one reflection or one grouping operation, had at least one failed provider attempt, `evo tree evolve` prints one line about it on stderr, and the process log keeps it. The journal, the stdout document and the report do not change.

When the invocation went on, the line appears as soon as it returns:

```text
evo tree evolve: reflector r01 needed provider retries (2 failed attempts: http 503, http 503); it continued
```

The first word is the role and the second is the operation: the round for the Reflector and the Grouper (`r01`), the Attempt for the Editor (`r01-a01`). Then come the failed attempts in order: `http <status>`, followed by the provider's error code when it sent one (`http 429 rate_limited`), or the failure class when there was no response (`timeout`, `connection`). A `401` or `403` reads `http 401 (credential)`, or `http 401 <code> (credential)` when the provider sent a code (`http 401 invalid_api_key (credential)`). The line lists at most eight attempts and then `…`. It never contains request content or the provider's message text.

If every failed attempt was the tool-dialect preflight's, the line ends with `(tool-dialect preflight)`. The preflight is a small first request that checks the tool format; its success shows that the format works with the endpoint, not that the full request will. A preflight request that the endpoint refuses only because it wants another tool format is not a failure, because Evolution then tries the next format: it prints no line and stays in the role log.

When the invocation ended in a provider failure, the line comes after the failure report. A provider failure normally suspends the campaign, and then the line comes right after the suspension line, before the `continue:` line:

```text
evo tree evolve: suspended: reflector_provider_failed — the Reflector's provider failed after retries
evo tree evolve: reflector r01 provider attempts: http 503, http 503, http 503 (provider_unavailable)
evo tree evolve: continue: evo tree evolve --manifest manifest.json --run-dir run-1
```

The reason in parentheses is the retry layer's: `provider_unavailable` (transient failures used up `max_attempts` or `total_budget_seconds`), `deadline_exhausted` (the role's deadline ran out) or `provider_rejected` (a failure that is not retried, such as `401`, `403` or another `4xx`). The journal records the first two as `provider_exhausted` and the third as `provider_rejected` ([role resilience](manifest-v2.md#reflector-and-editor-resilience-family-10)). For a role without `api_key_env`, a `401` or `403` adds a sentence that tells you which repair to make:

```text
evo tree evolve: grouper r01 provider attempts: http 401 (credential) (provider_rejected); the endpoint asked for a credential; this role declares no api_key_env
```

Each invocation prints one line at most, recovered or failed. Every attempt, the successful ones and the preflight's included, is in the role's private log as a `provider_attempt` record with its `http_status` and `provider_code` ([provider attempts](manifest-v2.md#provider-attempts)).

### A Reflector's first request is too large

In a `/15` run, `reflection_failed` with `reason: input_too_large` means that a `chat` Reflector's first request still exceeds its budget after history has been fully reduced. Evolution refuses it before any probe health check, mediator or provider call, including the provider preflight. The failure has class `unknown` and is followed by `campaign_suspended` with `reason: reflector_failing`; the round stays open. Resuming unchanged refuses again without calling the provider.

Set `RUN_DIR` to your suspended run directory, then inspect its status:

```bash
evo tree watch --run-dir "$RUN_DIR" --once
```

It exits `0`; expect a line ending with:

```text
suspended: reflector_failing (reflector) · waiting for a resume
```

The snapshot names the suspension, not its underlying reflection reason. Read the `reflection_failed` record in `campaign.jsonl` to distinguish `input_too_large` from another Reflector failure. Its private size file is `reflector-logs/budget/<reflection_id>.json` under the run directory. It contains component sizes, limits, round counts, aggregate size/form and the sent/refused decision, without prompts, case text or other content.

Raise `reflector.context_compact_chars` to fit the model's actual input window, accept the printed identity `rebase` with `evo tree authorize`, then run the same `evo tree evolve` command with the same run directory again. Alternatively, shrink the fixed parts with fewer environment documents or a shorter system prompt, accepting the reported bound-input changes before resuming. The [compaction-key reference](manifest-v2.md#compaction-keys-in-15) explains the reserve and first-request limits.

A run whose fixed parts alone are too large could work before `2.0.0b5` and learns of this refusal at its next reflection. Unfinished `/15` runs from `2.0.0b3` or `2.0.0b4` need the usual producing-version `rebase`; a `chat` Reflector also has an identity change because its history tools changed. `agent_cli` and `subprocess` Reflectors have no extra identity change from the helpers. Once the journal contains the new reason, `2.0.0b3` and `2.0.0b4` cannot read it.

### A Reflector group field is too long

A Reflector group has five text fields, such as `root_cause`, and each has a character limit: 512 in `/15` and 4,096 in `/16` unless `reflector.group_text_chars` says otherwise. Models often ignore the limit while they write. A `chat` Reflector whose answer breaks it gets a protocol repair that names the bound, for example `root_cause exceeds 512 characters`. When its repairs run out, the reflection fails as `protocol_exhausted`; a `subprocess` Reflector that breaks the limit fails with `invalid_result`. Both are `content` failures: the round closes `unplanned` without an Attempt, and the failure counts toward `reflector.max_consecutive_failures`. A `chat` Reflector's private log under `reflector-logs/` holds the rejected answer as `finish_reflection_rejected`.

To let longer diagnoses through, raise the limit in the manifest's `reflector` section, or set it to `null` for no per-field limit:

```json
{"max_consecutive_failures": 2, "group_text_chars": 4096}
```

Then run the same `evo tree evolve` command. It is refused with exit `2`, because the limit is part of the campaign's adaptation identity:

```text
evo tree evolve: refused: run_identity_changed
evo tree evolve: differs: adaptation  journaled <digest>  observed <digest>
evo tree evolve: class: authorizable
evo tree evolve: to accept this difference and continue, run:
  evo tree authorize run-1 run_identity_changed --digest <digest> --reason "<why>"
```

Run the `evo tree authorize` command that the refusal prints, with its own digest and the default `rebase` effect, then run the same `evo tree evolve` again. The run continues in the same directory. The new limit applies from the next round: the open round keeps its limit, also when it retries an interrupted reflection. Lowering the limit later works the same way and never invalidates groups that are already stored. The whole groups answer stays capped at 65,536 bytes, so a larger limit lets about three groups of five full 4,096-character fields through, not eight. See [the group text limit](manifest-v2.md#the-reflector-group-text-limit-15-and-16) for every layer the limit reaches.

A `/16` run created by `2.0.0b9` or earlier ran at 512. Its first resume by a later release is refused once with `run_identity_changed`, listing `producing_version` and `adaptation`. One `evo tree authorize` accepts both, and the next round uses 4,096. To keep 512, add `"group_text_chars": 512` to the `reflector` section before you resume; you still accept the one printed `rebase`.

Adding `"group_text_chars": 512` to an unfinished `/15` run also changes its adaptation identity, so it needs one `evo tree authorize`, although the limit stays 512. In `/16`, declaring the value the run already uses (4,096 for a run that this release created) changes nothing.

### A `/16` chat Reflector's answer is rejected and corrected

In a `/16` campaign with `reflector.kind: chat`, the Reflector's final answer is checked before the reflection ends. The check is the same one the campaign applies before it records the answer. When the answer has a fixable mistake, the Reflector is told what is wrong and can answer again in the same conversation. Two mistakes are common. The answer puts a catalog `case_handle` in `affected_cases`, which needs the `case_id`. Or it claims a case through an observation of another case: every affected case needs a cited observation of that same case. A cited observation does not have to be read in full; reading activity is recorded, but it is not part of the check.

The Reflector receives a short message that names the field and the problem, and never repeats the rejected value or lists valid answers:

```text
finish_reflection was rejected: groups[0].affected_cases[0]: expected case_id. Revise the result and call finish_reflection again.
```

Nothing is written to `campaign.jsonl` for a rejected answer. Only the accepted answer becomes `reflection_finished`, so no Editor, Target or Scorer work starts before it. To see the rejections, read the Reflector's private log, `reflector-logs/r<NN>-x<NN>.jsonl`: each one is a `finish_reflection_rejected` line with the rejected arguments, the `error` and the `correction` the model was shown. The log is redacted like every role log.

An answer that holds an oracle string or a run secret is never corrected. The reflection ends at once, without a message to the model, and the campaign records the same content failure as before: `reflection_failed` with `oracle_leak` or `invalid_result`, and the round closes `unplanned`.

Corrections and protocol repairs have separate budgets. A malformed call, an unknown tool, invalid JSON or a field over its length limit ([above](#a-reflector-group-field-is-too-long)) still uses one of the loop's protocol repairs. A corrected mistake uses none of them; it uses only the model turn that produced the rejected answer.

Corrections have no separate cap. A weak model can use all of the Reflector's turns revising its answer, so budget for that cost in provider calls and wall time. The turn limit is `reflector.max_turns` (64 by default) without a probe, and `max(reflector.max_turns, 2 * probe.budget + 8)` with a configured probe, whether or not the model calls it. With the default `probe.budget` of 32 that is at least 72 turns. When the turns run out without an accepted answer, the reflection fails as `turns_exhausted`, a `content` failure, and the round closes `unplanned`. `campaign.reflection_timeout_seconds` still bounds the reflection's time, and the provider retry policy still governs failed requests; a timeout or a provider fault keeps its own failure class and is never treated as a mistake in the answer. To allow more revisions, raise `reflector.max_turns`, raise `probe.budget` when probes are enabled, or raise `campaign.reflection_timeout_seconds`, and accept the printed identity change as usual.

`/15` campaigns, and `/16` campaigns with an `agent_cli` or `subprocess` Reflector, keep their behavior: the answer is checked once, after the Reflector has ended. The `/16` chat Reflector's packaged prompt (`prompts/tree_reflector_v4.md`) and its `finish_reflection` description state this contract, so its identity differs from the previous release. An unfinished `/16` chat run, including one interrupted during a reflection, is refused once with `run_identity_changed`, listing `producing_version` and `identity.reflector.digest`. One `evo tree authorize` with the default `rebase` effect accepts both; then run the same `evo tree evolve` command again.

### Read an Editor budget failure

An Editor that changes more files than `editor.max_changed_files`, or writes more bytes than `editor.max_written_bytes`, is refused when it finishes. In a `/15` run the journal names the budget. The `editor_failed` record carries `reason: changed_file_budget` or `reason: written_byte_budget`, with class `content`. The campaign does not suspend. The Attempt closes `abandoned` with `editor_failed`, and no Candidate is measured, so the hypothesis was not tested.

Find the code in the Attempt feedback and in the round history that the next roles read. The next Attempt of the round sees it only under `campaign.attempt_feedback: sequential`. The Reflector and the Editor of later rounds always see it in the round history. Each finds the value in the `reason` field, with a short explanation in its opening. To see it yourself, read the `editor_failed` records of `campaign.jsonl` and the Attempt's file in `editor-logs/`.

To change the outcome:

- `changed_file_budget`: the diagnosis asked for more files than the limit. Raise `editor.max_changed_files` if the change really needs them, or let the Editor try again, because its opening now states the limit and asks it to implement the highest-mass hypothesis within it. A `chat` Editor is refused a plan or an edit that would go over, as a tool error; each refused call still uses one turn of `max_turns`. Writing an updated file's original bytes back frees its slot, but a deleted file cannot be restored this way. An `agent_cli` Editor gets its one repair call, with the count and the changed files in `.evolution/VALIDATION.json`.
- `written_byte_budget`: the Editor wrote more than `editor.max_written_bytes`. A `chat` Editor's written bytes are checked as it writes; the other kinds are checked when they finish. Raise it, or narrow the files the diagnosis names.

Changing either limit changes the Editor's identity, so the resume is refused `run_identity_changed` naming `identity.editor.digest`, and accepting it takes a `rebase` authorization ([Handle a refusal](#handle-a-refusal)). Reading this value needs a reader that knows it: a tool with a closed table of `reason` values must add both codes.

## Handle a refusal

### Accept a reported input change

A refusal exits `2` and explains the problem on stderr. Correct malformed settings or missing files first.

Every campaign schema except `/15` and `/16` is refused `manifest_schema` with `expected evolution-v2-manifest/15 or /16`, including CLI conformance, trace with a campaign manifest and `evo-target serve`. Removed run families are refused by every campaign reader and writer as `campaign_family_unsupported at family/N` (`family/absent` for unversioned creation), exit `2`, before writes or target contact. Missing or illegible creation lines keep their missing/unreadable outcomes. The shared guidance is `this release reads campaign families 14 and 15; read this run with evolution 2.0.0b9 or seed a new /15 campaign from its champion copy`. No authorization can bypass this boundary, and no converter exists. New links to removed families are `lineage_family_unsupported`; existing current-family recorded links retain the [owed-import recovery](#continue-an-existing-current-family-link). For other changed inputs on supported runs, the refusal lists differences as `authorizable` or `hard`.

For an authorizable finding, the CLI prints the exact `evo tree authorize` command, including a digest of the change. Read the effect it describes, supply a meaningful reason and then rerun `evolve`. Copy the actual digest from your refusal; an example digest from documentation will not work.

Some accepted changes close the old campaign so you can continue under the new conditions in a linked run. Authorization does not always mean “keep this campaign running”. A changed comparison condition on an unfinished campaign offers a choice; the next section shows it. A hard finding has no override through `authorize`. See the [authorization reference](cli.md#evo-tree-authorize).

An authorization binds the exact reported finding, its digest and its effect. It does not approve all future changes. List stored decisions with `evo tree authorize RUN_DIR --list`; rerun the original command after recording the decision. A new finding or different digest requires its own decision. Do not edit authorization files or journals to bypass validation.

A non-comparable input change normally uses `rebase`. A changed comparison condition may offer `continue`, `rebaseline` or `close`, as described below. Rebaselining closes an open round and measures a new baseline; it does not carry that round forward. A finished campaign needs a linked successor. Corpus content and train/test assignments cannot be relabeled in place through authorization.

If a resumed linked run owes an inherited baseline whose source is unavailable, the reported `fresh_baseline` authorization releases that import and measures a fresh baseline. It does not restore missing diagnostics or authenticate the lost source. New links require a finished supported-family predecessor; a new directory does not reset inherited statistical spend.

## Change the provider mid-campaign

A campaign can run for hours. If your agent's LLM provider fails during that time, you may move the agent to another provider: the same model from another host, or a replacement model. This section shows how to keep the campaign and its measurements when you do that. It applies to an unfinished campaign that a `/15` manifest created.

Evolution sees the switch only if your Target Service reports it. The target's identity includes a `settings_hash`; put the provider and model into the settings that hash covers, and restart the target after the switch. See [the target server reference](target-server.md#constructor). A switch the target does not report is invisible, and the campaign simply continues.

In this example the provider failed, so the campaign suspended:

```text
evo tree evolve: suspended: target_unstable — 12 of 18 tries in this window, threshold 2 tries
evo tree evolve: continue: evo tree evolve --manifest manifest.json --run-dir run-1
```

The operator restarted the target with the new provider and ran the printed command. The run refused, exit `2`, and offered three choices, the default first:

```text
evo tree evolve: refused: run_identity_changed
evo tree evolve: differs: identity.target.settings_hash  journaled provider-a-settings  observed provider-b-settings
evo tree evolve: class: authorizable
evo tree evolve: to continue with the existing measurements, run:
  evo tree authorize run-1 --accept run_identity_changed:1f652bd08bc230833586620a0d595d6e9ab0e87c42e48dcc009a13f06fd21021 --reason "<why>"
evo tree evolve: to start the next round from a fresh baseline under the new condition, run:
  evo tree authorize run-1 --accept run_identity_changed:1f652bd08bc230833586620a0d595d6e9ab0e87c42e48dcc009a13f06fd21021 --effect rebaseline --reason "<why>"
evo tree evolve: to close the campaign and continue with a linked run, run:
  evo tree authorize run-1 --accept run_identity_changed:1f652bd08bc230833586620a0d595d6e9ab0e87c42e48dcc009a13f06fd21021 --effect close --reason "<why>"
```

Each choice is an *effect*. It decides what the campaign keeps:

| Effect | Choose it when | What happens |
|---|---|---|
| `continue` | The provider does not matter to your question, and you want to keep what was measured. | Every measurement stays. A measurement that was cut off in the middle keeps its scored rows, and only its missing rows are measured under the new provider. The open round goes on from where it stopped. |
| `rebaseline` | You want every comparison from now on made under the new provider. | Closed rounds stay as history. The open round closes without a promotion. The next round starts by measuring the current Champion again under the new provider. This spends a round of `campaign.rounds`. |
| `close` | You want a clean new line of work. | The campaign stops as `stopped` (`condition_changed`). Continue it with a [linked run](#continue-a-completed-campaign). |

Copy the command for your choice from your own refusal, replace `<why>` with your reason, and run it. It prints the id of the decision it wrote:

```bash
evo tree authorize run-1 --accept run_identity_changed:1f652bd08bc230833586620a0d595d6e9ab0e87c42e48dcc009a13f06fd21021 --reason "provider A is down; provider B serves the same model"
```

```text
a47e747906f9e0191b595c9e85da20fb5d38134adc6c736a1b977e6ede7ab84c
```

Then run the same `evo tree evolve` command again. It applies the decision and continues in the same process and the same run directory. In the example the campaign then finished its two rounds and exited `0`.

The report records the change. `status.condition_changes` lists every accepted change with its effect, the changed fields and the round that was open; `status.caveats` gains a `condition_changed_in_place` entry for each; each measurement in `status.measurements` names its `condition_epoch`: `0` for a measurement started before the first change, `1` for one started after it, and so on. In the stdout report the `authorization_id` of a change reads `[REDACTED]`, like other authorization fields; the inspection API shows the id:

```bash
python - <<'PY'
import json
from pathlib import Path
from evolution.tree import inspect_campaign

report = inspect_campaign(Path("run-1"), verify=True).to_payload()
print(report["verification"]["mismatches"])
print(json.dumps(report["condition_changes"], indent=2))
print([(m["measurement_id"], m["condition_epoch"]) for m in report["measurements"]])
PY
```

```text
[]
[
  {
    "authorization_id": "a47e747906f9e0191b595c9e85da20fb5d38134adc6c736a1b977e6ede7ab84c",
    "effect": "continue",
    "fields": [
      "target"
    ],
    "identity_digest": "23956dddf043f04a24fbb7759639516e440ef5508f89e14b2d70edd9bf09b698",
    "round": 1,
    "seq": 36
  }
]
[('m-r00-champion', 0), ('m-r01-a01', 0), ('m-r02-a01', 1)]
```

The empty list means the stored evidence verified. The Champion's baseline and round 1's Attempt were measured under the old provider; round 2's Attempt under the new one, compared against the kept baseline. The same run accepted with `--effect rebaseline` instead closes round 1 with the outcome `condition_changed` and measures the Champion again, as `m-r02-champion@e1`, before round 2's Attempt.

A caveat changes no statistic, gate decision or exit code. Whether a result obtained across the change counts is your decision; the report tells you where the change happened.

Not every change offers all three effects:

- `continue` is offered only when nothing but the agent's side changed: the target, the Environment, or the materialization strategy. If the scorer, the metric, the corpus or the campaign policy also changed, old scores are not on the new scale. The refusal then prints `the scorer, metric, corpus or policy changed; existing scores cannot be compared on the new scale inside a round` and offers `rebaseline` (the default) and `close`.
- `rebaseline` needs a round left in `campaign.rounds`. When none is left, it is not offered. A scale change with no round left offers only `close`, and the refusal prints the ordinary close sentence without saying why the other effects are missing.
- A new policy with fewer `campaign.rounds` than this run has begun offers only `close`. Current `/15` and `/16` campaigns have no alpha-budget setting.
- A change of the artifact policy or `role_visibility` offers only `close`, because it changes the stored-copy contract or the roles' declared view.
- These effects apply to current `/15` and `/16` campaigns. Removed schemas and run families are hard refusals, not candidates for an accepted close.
- `continue` and `rebaseline` write the rebuilt identity and current case-family membership. If the record would exceed the journal's 1 MiB limit or contain a forwarded secret value, the effect is not offered. If no admissible effect remains, or a decision selects a withheld effect, the refusal is `corpus_too_large` or `record_matches_secret`, before writes. Use smaller input, shorter non-secret family labels or `close` when offered.

A decision whose effect the change does not offer is refused with `authorization_invalid at effect`. Two decisions for one refusal with different effects are refused with `authorization_conflict`; remove all but one from the run directory's `authorizations/` folder. If a run stops while it is applying a decision, the next run finishes the change without a new decision. If you edited the manifest again in between, or upgraded the package, it refuses `authorized_identity_changed`: restore the manifest you accepted and the package version that applied it, let the run finish that change, then present the new change on its own. The [CLI reference](cli.md#a-changed-condition-on-an-unfinished-campaign) lists every refusal.

## Continue a completed campaign

Re-running a finished campaign reports its result; it does not add rounds. To continue from its selected files, create a successor manifest and use a new run directory. This example follows the Quickstart and leaves the original manifest untouched:

```bash
python - <<'PY'
import json
from pathlib import Path
from evolution.tree import inspect_campaign

run = Path("demo/run-1").resolve()
status = inspect_campaign(run, verify=True).to_payload()
assert status["verification"]["mismatches"] == []
assert status["campaign_outcome"] is not None, "Finish or resume the old run first"
champion = status["campaign_outcome"]["champion_path"]
manifest = json.loads(Path("demo/manifest.json").read_text())
manifest["seed_root"] = str(run / champion)
manifest["linked_from"] = {"run_dir": str(run), "baseline": "fresh"}
Path("demo/manifest-next.json").write_text(json.dumps(manifest, indent=2) + "\n")
PY

evo tree evolve --manifest ./demo/manifest-next.json --run-dir ./demo/run-next
```

Keep the original demo service running: its allowed root already contains these Champion and Candidate copies. A custom target must also allow the predecessor's Champion and the successor's run artifacts. Keeping the successor manifest beside the original preserves the meaning of other relative paths.

`baseline: fresh` measures the starting tree again. `baseline: inherit` reuses eligible Champion rows and reports the reuse as a caveat. A `/15` successor carries scalar rows without verdicts; if its failures are all verdict-only, it may diagnose nothing until a refresh (#208), so choose `fresh` or a positive refresh schedule. `/16` can also carry bounded private diagnostics with explicit gaps; absent usable diagnostics can leave the same limitation. A link carries lineage and round numbering without reopening predecessor rounds. Neither current schema spends alpha or declares a per-round alpha allowance. See [linked runs](manifest-v2.md#linkedfrom) before changing conditions or selecting inheritance.

### Evaluation recovery and metric coverage

An evaluation reads its run ID from its journal on resume, so its run directory may be moved while its declared inputs remain available at the same locations. Changing a declared input still requires a new evaluation. A damaged owner journal, artifact ledger or child measurement refuses verification; a valid prefix alone is not a completed evaluation.

The primary metric requires every selected case and repeat. Optional scalar metrics are averaged over their observed repeats, then cases within each observed group, then groups equally. The report's `metric_coverage` gives the contributing samples, cases and groups for each metric. Compare these counts with the requested coverage before comparing optional metrics between evaluations.

## Understand incomplete evidence

A timeout, interrupted sample or failed infrastructure call is missing evidence, not a Candidate quality score. `campaign.sample_retries` gives an indeterminate sample up to that many additional tries in its measurement stage. Infrastructure suspensions resume with the same manifest and run directory after the cause is repaired; the report identifies the unfinished pairs and their owner. Keep the scorer's deterministic `candidate_failed` verdict distinct from an unavailable Target.

Baseline and winner-completion measurements can exclude exhausted case/repeat pairs. A comparison uses the remaining matched pairs on both sides, and the report shows exclusions. A Candidate Attempt does not hide an exhausted sample by excluding it: insufficient evidence prevents its look from being evaluated and does not spend alpha. A critical failure is checked against the Candidate evidence before baseline exclusions can hide it. If no evaluable focus remains, the round can finish without proposing an edit.

Linked baseline evidence carries provenance and the inherited statistical history. The successor checks that imported rows fit its schedule and metric; it does not independently reproduce the predecessor's measurements. Trust the source run you select, preserve it with its evidence, and inspect the resulting caveats. Missing inherited diagnostic text and usable scalar measurements are separate conditions.

## Process-log limits

A run process log starts only after admission. A refusal or a crash before that point can leave no log, so capture terminal stderr when investigating startup failures. Completed runs reopened without a new journal append normally create no log. Logs are diagnostic copies; they do not drive resume, verification, promotion or role inputs.

The early buffer is bounded at 1 MiB and copied stderr at 64 MiB; omissions are reported. Standard output and raw fault-handler dumps are not covered by that stderr bound. Secret redaction works on each individual write, so a secret split across writes can escape it, and raw fault dumps bypass redaction. Review logs before sharing them. The explicit `--log-file` output of conformance and the Target Service has no run secret set and is not redacted. Log wording and a recorded closing exit line are not a machine-readable status API; inspect the command's actual exit status.

## Storage forecast limits

An inode forecast is advisory. It does not account exactly for tree growth, concurrent writers, additional corrupt-pack segments, every lazy directory on resume or transient anonymous buffers. A low forecast warns and continues; a filesystem without a fixed inode count gives no shortage warning. Keep enough free bytes and inodes for the agent and its subprocesses, and preserve durable run evidence when freeing space. A later storage failure can require repeated work on resume.
